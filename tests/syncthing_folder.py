"""A storage folder that behaves like a Syncthing share mid-sync (Track 87A).

One shared helper for every test of the Syncthing-tolerant read path, so a
scenario is written once and reused: content that arrives late, a manifest that
arrives before its blobs, a file removed right after it was listed, and a file
that is gone or replaced at the moment it is opened.

`SyncthingFolder` is a real `LocalBackend` over a real directory. Nothing in
mind-meld is mocked; the helper only changes what the filesystem looks like at
the instant a read happens. Its retry clock is injected, so no test sleeps: each
requested delay is appended to `sleeps`, and an `on_sleep` hook can make a file
reappear during a retry.

    folder = SyncthingFolder(tmp_path / "storage")
    folder.put(key, data)
    folder.hide(key)                 # exists on the peer, not delivered yet
    folder.arrive(key)               # delivered
    folder.vanish_after_listing(key) # listed, then removed before the open
    folder.vanish_at_open(key, reappear_after=2)  # gone for two retries, then back
    folder.swap_at_open(key, new)    # replaced atomically just before the open
    folder.vanish_copy_at_validation(name)  # a conflict copy gone when it is checked
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mind_meld.storage.local import LocalBackend


class SyncthingFolder(LocalBackend):
    def __init__(self, root: str | Path) -> None:
        self.sleeps: list[float] = []
        self.listings: list[str] = []
        self._hidden: dict[str, bytes] = {}
        self._remove_after_listing: set[str] = set()
        self._gone_at_open: dict[str, int | None] = {}
        self._reappear: dict[str, list] = {}
        self._swap_at_open: dict[str, bytes] = {}
        self._vanish_at_validation: set[str] = set()
        super().__init__(root, sleep=self._record_sleep)

    # ── scenario controls ────────────────────────────────────────────

    def hide(self, key: str) -> None:
        """Take `key` out of the folder: it exists on a peer, not here yet."""
        path = self.root / key
        self._hidden[key] = path.read_bytes()
        path.unlink()

    def arrive(self, key: str) -> None:
        """Deliver a hidden `key`."""
        self.put(key, self._hidden.pop(key))

    def vanish_after_listing(self, key: str) -> None:
        """Remove `key` right after `list_keys` returns it."""
        self._remove_after_listing.add(key)

    def vanish_at_open(self, key: str, *, reappear_after: int | None = None) -> None:
        """Make `key` absent when `get` opens it. With `reappear_after=n` it is
        back after n retry sleeps (a replacement that finishes); with None it
        stays gone (a removal)."""
        self._gone_at_open[key] = reappear_after

    def swap_at_open(self, key: str, data: bytes) -> None:
        """Replace `key` atomically just before `get` opens it."""
        self._swap_at_open[key] = data

    def vanish_copy_at_validation(self, file_name: str) -> None:
        """Remove the conflict copy named `file_name` as the conflict scan
        validates it (the file is gone by the time anyone reads it)."""
        self._vanish_at_validation.add(file_name)

    # ── hooks into the backend ───────────────────────────────────────

    def find_conflict_copies(
        self, key: str, is_valid: Callable[[Path], bool] | None = None
    ) -> list[Path]:
        if is_valid is not None and self._vanish_at_validation:
            inner = is_valid

            def validate(path: Path) -> bool:
                if path.name in self._vanish_at_validation:
                    self._vanish_at_validation.discard(path.name)
                    path.unlink(missing_ok=True)
                return inner(path)

            return super().find_conflict_copies(key, validate)
        return super().find_conflict_copies(key, is_valid)

    def list_keys(self, prefix: str) -> list[str]:
        self.listings.append(prefix)
        keys = super().list_keys(prefix)
        for key in keys:
            if key in self._remove_after_listing:
                self._remove_after_listing.discard(key)
                (self.root / key).unlink(missing_ok=True)
        return keys

    def get(self, key: str, *, expected: bool = False) -> bytes:
        if key in self._swap_at_open:
            self.put(key, self._swap_at_open.pop(key))
        if key in self._gone_at_open:
            reappear_after = self._gone_at_open.pop(key)
            path = self.root / key
            if path.exists():
                if reappear_after is not None:
                    self._reappear[key] = [path.read_bytes(), reappear_after]
                path.unlink()
        return super().get(key, expected=expected)

    def _record_sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        for key in list(self._reappear):
            data, remaining = self._reappear[key]
            if remaining <= 1:
                del self._reappear[key]
                self.put(key, data)
            else:
                self._reappear[key][1] = remaining - 1


FAKE_SYNCTHING_PLACEHOLDER = "test-syncthing-placeholder"
FAKE_LOCAL_SYNCTHING_ID = "LOCALAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA"
FAKE_PEER_SYNCTHING_ID = "PEERBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB"
FAKE_PEER2_SYNCTHING_ID = "PEERCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC"


class FakeSyncthing:
    """In-process fake Syncthing REST server and ``config.xml`` fixture (Track 92A)."""

    def __init__(self, config_dir: Path, storage_path: Path | None = None) -> None:
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "config.xml"
        self.storage_path = (
            Path(storage_path) if storage_path is not None else (self.config_dir.parent / "storage")
        )
        self.credential = FAKE_SYNCTHING_PLACEHOLDER
        self.my_id = FAKE_LOCAL_SYNCTHING_ID
        self.peer_id = FAKE_PEER_SYNCTHING_ID
        self.peer2_id = FAKE_PEER2_SYNCTHING_ID
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.responses: dict[str, Any] = {}
        self.holds: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _handle(self) -> None:
                headers = {k: v for k, v in self.headers.items()}
                with outer._lock:
                    outer.requests.append((self.command, self.path, headers))
                    hold_event = outer.holds.get(self.path) or outer.holds.get("*")
                if hold_event is not None:
                    hold_event.wait(timeout=5.0)
                auth_header = headers.get("X-API-Key") or headers.get("x-api-key")
                if auth_header != outer.credential:
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"CSRF Error")
                    return
                with outer._lock:
                    spec = outer.responses.get(self.path)
                    if spec is None and "?" in self.path:
                        spec = outer.responses.get(self.path.split("?", 1)[0])
                if spec is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if isinstance(spec, int):
                    self.send_response(spec)
                    self.end_headers()
                    return
                if isinstance(spec, tuple) and len(spec) == 2:
                    code, payload = spec
                    extra_headers: dict[str, str] = {}
                elif isinstance(spec, tuple) and len(spec) == 3:
                    code, payload, extra_headers = spec
                else:
                    code, payload, extra_headers = 200, spec, {}
                if isinstance(payload, bytes):
                    body = payload
                    ctype = "application/octet-stream"
                elif isinstance(payload, str):
                    body = payload.encode("utf-8")
                    ctype = "text/plain; charset=utf-8"
                else:
                    body = json.dumps(payload).encode("utf-8")
                    ctype = "application/json"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for h_name, h_val in extra_headers.items():
                    self.send_header(h_name, h_val)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_port
        self.address = f"127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.write_config()
        self.set_folder(self.storage_path)

    def write_config(
        self,
        *,
        address: str | None = None,
        tls: bool = False,
        credential: str | None = None,
    ) -> Path:
        addr = self.address if address is None else address
        cred = self.credential if credential is None else credential
        tls_str = "true" if tls else "false"
        xml = (
            '<configuration version="37">\n'
            f'  <gui enabled="true" tls="{tls_str}">\n'
            f"    <address>{addr}</address>\n"
            f"    <apikey>{cred}</apikey>\n"
            "  </gui>\n"
            "</configuration>\n"
        )
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(xml, encoding="utf-8")
        return self.config_path

    def set_folder(
        self,
        path: Path | str | None = None,
        *,
        folder_id: str = "mind-meld",
        label: str = "Mind Meld",
        folder_type: str = "sendreceive",
        versioning_type: str = "",
        max_conflicts: int = 10,
        ignore_perms: bool = False,
        order: str = "alphabetic",
        paused: bool = False,
        peers: list[tuple[str, str, bool]] | None = None,
        state: str = "idle",
        need_files: int = 0,
        need_bytes: int = 0,
        pull_errors: int = 0,
        write_stignore: bool = True,
    ) -> None:
        if path is not None:
            self.storage_path = Path(path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        if write_stignore:
            (self.storage_path / ".stignore").write_text("(?d)tmp*.tmp\n", encoding="utf-8")
        peer_rows = (
            [(self.peer_id, "mac-b", True), (self.peer2_id, "linux-a", True)]
            if peers is None
            else peers
        )
        folder_devices = [{"deviceID": self.my_id}] + [{"deviceID": pid} for pid, _, _ in peer_rows]
        config_devices = [{"deviceID": self.my_id, "name": "mac-a"}] + [
            {"deviceID": pid, "name": name} for pid, name, _ in peer_rows
        ]
        connections = {pid: {"connected": connected} for pid, _, connected in peer_rows}
        folder_obj = {
            "id": folder_id,
            "label": label,
            "path": str(self.storage_path),
            "type": folder_type,
            "paused": paused,
            "ignorePerms": ignore_perms,
            "order": order,
            "maxConflicts": max_conflicts,
            "versioning": {"type": versioning_type, "params": {}},
            "devices": folder_devices,
        }
        db_status = {
            "state": state,
            "needFiles": need_files,
            "needBytes": need_bytes,
            "pullErrors": pull_errors,
        }
        with self._lock:
            self.responses["/rest/system/status"] = {"myID": self.my_id}
            self.responses["/rest/system/connections"] = {"connections": connections}
            self.responses["/rest/config/folders"] = [folder_obj]
            self.responses["/rest/config/devices"] = config_devices
            self.responses[f"/rest/db/status?folder={folder_id}"] = db_status
            self.responses["/rest/db/status"] = db_status

    def hold(self, path: str = "*") -> threading.Event:
        event = threading.Event()
        with self._lock:
            self.holds[path] = event
        return event

    def close(self) -> None:
        with self._lock:
            events = list(self.holds.values())
        for ev in events:
            ev.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)
