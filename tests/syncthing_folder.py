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

import datetime
import http.server
import json
import os
import shutil
import ssl
import threading
import time
import urllib.parse
import xml.sax.saxutils as saxutils
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

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


def make_syncthing_like_certificate(
    directory: Path, *, common_name: str = "syncthing"
) -> tuple[Path, Path]:
    """Generate a Syncthing-shaped ECDSA P-256 certificate and private file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    priv = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Syncthing"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Automatically Generated"),
        ]
    )
    not_before = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    not_after = not_before + datetime.timedelta(days=365)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(priv.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(priv, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    priv_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    priv_path.write_bytes(
        priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return (cert_path, priv_path)


class FakeSyncthing:
    """In-process fake Syncthing REST server and ``config.xml`` fixture (Track 92A)."""

    def __init__(
        self,
        config_dir: Path,
        storage_path: Path | None = None,
        *,
        tls_cert: tuple[Path, Path] | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "config.xml"
        self.storage_path = (
            Path(storage_path) if storage_path is not None else (self.config_dir.parent / "storage")
        )
        self.gui_value = FAKE_SYNCTHING_PLACEHOLDER
        self.my_id = FAKE_LOCAL_SYNCTHING_ID
        self.peer_id = FAKE_PEER_SYNCTHING_ID
        self.peer2_id = FAKE_PEER2_SYNCTHING_ID
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.bodies: list[tuple[str, str, Any]] = []
        self.responses: dict[str, Any] = {}
        self.write_responses: dict[tuple[str, str], Any] = {}
        self.apply_writes: bool = True
        self.on_write: Callable[[str, str, Any], None] | None = None
        self.holds: dict[str, threading.Event] = {}
        self.delays: dict[str, float] = {}
        self.stopped: bool = False
        self._stop_after: tuple[str, str] | None = None
        self._lock = threading.Lock()

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_spec(self, spec: Any) -> None:
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

            def _handle(self) -> None:
                with outer._lock:
                    if outer.stopped:
                        self.close_connection = True
                        return
                headers = {k: v for k, v in self.headers.items()}
                parsed_body: Any = None
                if self.command != "GET":
                    clen = int(
                        headers.get("Content-Length") or headers.get("content-length") or "0"
                    )
                    raw_body = self.rfile.read(clen) if clen > 0 else b""
                    if raw_body:
                        try:
                            parsed_body = json.loads(raw_body.decode("utf-8"))
                        except Exception:
                            parsed_body = raw_body
                with outer._lock:
                    outer.requests.append((self.command, self.path, headers))
                    if self.command != "GET":
                        outer.bodies.append((self.command, self.path, parsed_body))
                    hold_event = outer.holds.get(self.path) or outer.holds.get("*")
                    delay_sec = outer.delays.get(self.path) or outer.delays.get("*") or 0.0
                if hold_event is not None:
                    hold_event.wait(timeout=5.0)
                if delay_sec > 0:
                    time.sleep(delay_sec)
                auth_header = headers.get("X-API-Key") or headers.get("x-api-key")
                if auth_header != outer.gui_value:
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"CSRF Error")
                    return
                if self.command != "GET":
                    with outer._lock:
                        w_spec = outer.write_responses.get((self.command, self.path))
                    if w_spec is not None:
                        self._send_spec(w_spec)
                        return
                    if outer.apply_writes:
                        code = outer._apply_write_locked(self.command, self.path, parsed_body)
                        self._send_spec((code, b""))
                        with outer._lock:
                            if outer._stop_after == (self.command, self.path):
                                outer.stopped = True
                                outer._stop_after = None
                        return
                    self.send_response(404)
                    self.end_headers()
                    return
                with outer._lock:
                    spec = outer.responses.get(self.path)
                    if spec is None and "?" in self.path:
                        spec = outer.responses.get(self.path.split("?", 1)[0])
                if spec is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self._send_spec(spec)

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def do_PATCH(self) -> None:
                self._handle()

            def do_DELETE(self) -> None:
                self._handle()

            def do_PUT(self) -> None:
                self._handle()

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_port
        self.address = f"127.0.0.1:{self.port}"
        if tls_cert is not None:
            cert_file, priv_file = tls_cert
            shutil.copyfile(cert_file, self.config_dir / "https-cert.pem")
            srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            srv_ctx.load_cert_chain(os.fspath(cert_file), os.fspath(priv_file))
            self._server.socket = srv_ctx.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if tls_cert is not None:
            self.write_config(tls=True)
        else:
            self.write_config()
        self.set_folder(self.storage_path)

    def _apply_write_locked(self, method: str, path: str, parsed_body: Any) -> int:
        with self._lock:
            if method == "PUT":
                return 405
            if method == "POST" and path == "/rest/config/folders":
                if not isinstance(parsed_body, dict) or not parsed_body.get("id"):
                    return 400
                folders = list(self.responses.get("/rest/config/folders") or [])
                fid = parsed_body["id"]
                replaced = False
                for idx, item in enumerate(folders):
                    if isinstance(item, dict) and item.get("id") == fid:
                        folders[idx] = dict(parsed_body)
                        replaced = True
                        break
                if not replaced:
                    folders.append(dict(parsed_body))
                self.responses["/rest/config/folders"] = folders
                shared_ids = {entry["deviceID"] for entry in parsed_body.get("devices", [])}
                for route, pending in self.responses.items():
                    if not route.startswith("/rest/cluster/pending/folders") or not isinstance(
                        pending, dict
                    ):
                        continue
                    offer = pending.get(fid)
                    if not isinstance(offer, dict) or not isinstance(offer.get("offeredBy"), dict):
                        continue
                    remaining = {
                        d: value for d, value in offer["offeredBy"].items() if d not in shared_ids
                    }
                    if remaining:
                        pending[fid] = {**offer, "offeredBy": remaining}
                    else:
                        del pending[fid]
                if self.on_write is not None:
                    self.on_write(method, path, parsed_body)
                return 200
            if method == "POST" and path == "/rest/config/devices":
                if not isinstance(parsed_body, dict) or not parsed_body.get("deviceID"):
                    return 400
                devices = list(self.responses.get("/rest/config/devices") or [])
                did = parsed_body["deviceID"]
                replaced = False
                for idx, item in enumerate(devices):
                    if isinstance(item, dict) and item.get("deviceID") == did:
                        devices[idx] = dict(parsed_body)
                        replaced = True
                        break
                if not replaced:
                    devices.append(dict(parsed_body))
                self.responses["/rest/config/devices"] = devices
                pending_devs = self.responses.get("/rest/cluster/pending/devices")
                if isinstance(pending_devs, dict):
                    pending_devs.pop(did, None)
                if self.on_write is not None:
                    self.on_write(method, path, parsed_body)
                return 200
            if method == "PATCH" and path.startswith("/rest/config/folders/"):
                if not isinstance(parsed_body, dict):
                    return 400
                target_id = urllib.parse.unquote(path[len("/rest/config/folders/") :])
                folders = list(self.responses.get("/rest/config/folders") or [])
                for idx, item in enumerate(folders):
                    if isinstance(item, dict) and item.get("id") == target_id:
                        merged = dict(item)
                        merged.update(parsed_body)
                        folders[idx] = merged
                        self.responses["/rest/config/folders"] = folders
                        if self.on_write is not None:
                            self.on_write(method, path, parsed_body)
                        return 200
                return 404
            if method == "PATCH" and path.startswith("/rest/config/devices/"):
                if not isinstance(parsed_body, dict):
                    return 400
                target_id = urllib.parse.unquote(path[len("/rest/config/devices/") :])
                devices = list(self.responses.get("/rest/config/devices") or [])
                for idx, item in enumerate(devices):
                    if isinstance(item, dict) and item.get("deviceID") == target_id:
                        merged = dict(item)
                        merged.update(parsed_body)
                        devices[idx] = merged
                        self.responses["/rest/config/devices"] = devices
                        if self.on_write is not None:
                            self.on_write(method, path, parsed_body)
                        return 200
                return 404
            if method == "DELETE" and path.startswith("/rest/config/folders/"):
                target_id = urllib.parse.unquote(path[len("/rest/config/folders/") :])
                folders = list(self.responses.get("/rest/config/folders") or [])
                kept = [
                    item
                    for item in folders
                    if not (isinstance(item, dict) and item.get("id") == target_id)
                ]
                if len(kept) == len(folders):
                    return 400
                self.responses["/rest/config/folders"] = kept
                if self.on_write is not None:
                    self.on_write(method, path, parsed_body)
                return 200
            return 404

    def write_config(
        self,
        *,
        address: str | None = None,
        tls: bool = False,
        gui_value: str | None = None,
        devices: Iterable[dict[str, Any]] | None = None,
        folders: Iterable[dict[str, Any]] | None = None,
        defaults: bool = False,
    ) -> Path:
        addr = self.address if address is None else address
        cred = self.gui_value if gui_value is None else gui_value
        tls_str = "true" if tls else "false"
        if devices is None and folders is None and not defaults:
            xml = (
                '<configuration version="37">\n'
                f'  <gui enabled="true" tls="{tls_str}">\n'
                f"    <address>{addr}</address>\n"
                f"    <apikey>{cred}</apikey>\n"
                "  </gui>\n"
                "</configuration>\n"
            )
        else:
            lines = [
                '<configuration version="37">',
                f'  <gui enabled="true" tls="{tls_str}">',
                f"    <address>{addr}</address>",
                f"    <apikey>{cred}</apikey>",
                "  </gui>",
            ]
            dev_rows = [{"deviceID": self.my_id, "name": "mac-a"}, *(devices or ())]
            for dev in dev_rows:
                did = saxutils.quoteattr(str(dev.get("deviceID", "")))
                dname = saxutils.quoteattr(str(dev.get("name", "")))
                lines.append(f"  <device id={did} name={dname}></device>")
            for fld in folders or ():
                fid = saxutils.quoteattr(str(fld.get("id", "")))
                flabel = saxutils.quoteattr(str(fld.get("label", "")))
                fpath = saxutils.quoteattr(str(fld.get("path", "")))
                lines.append(f"  <folder id={fid} label={flabel} path={fpath}>")
                for fdev in fld.get("devices") or ():
                    fdev_id = fdev.get("deviceID", "") if isinstance(fdev, dict) else str(fdev)
                    lines.append(f"    <device id={saxutils.quoteattr(str(fdev_id))}></device>")
                lines.append("  </folder>")
            if defaults:
                lines.extend(
                    [
                        "  <defaults>",
                        '    <folder id="default" label="Template Folder" path="~">',
                        f'      <device id="{self.peer_id}"></device>',
                        "    </folder>",
                        f'    <device id="{self.peer2_id}" name="template-dev"></device>',
                        "  </defaults>",
                    ]
                )
            lines.append("</configuration>\n")
            xml = "\n".join(lines)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(xml, encoding="utf-8")
        return self.config_path

    def set_config(
        self,
        *,
        devices: Iterable[dict[str, Any]] = (),
        folders: Iterable[dict[str, Any]] = (),
    ) -> None:
        cfg_devices = [{"deviceID": self.my_id, "name": "mac-a"}, *devices]
        cfg_folders = list(folders)
        with self._lock:
            self.responses["/rest/system/status"] = {"myID": self.my_id}
            self.responses["/rest/config/devices"] = cfg_devices
            self.responses["/rest/config/folders"] = cfg_folders

    def set_shape(
        self,
        *,
        devices: Iterable[dict[str, Any]] = (),
        folders: Iterable[dict[str, Any]] = (),
        defaults: bool = False,
    ) -> None:
        dev_list = list(devices)
        fld_list = list(folders)
        self.set_config(devices=dev_list, folders=fld_list)
        self.write_config(devices=dev_list, folders=fld_list, defaults=defaults)

    def stock_folder(
        self,
        path: Path | str,
        *,
        shared_with: Iterable[str] = (),
        label: str = "Default Folder",
    ) -> dict[str, Any]:
        return {
            "id": "default",
            "label": label,
            "path": str(path),
            "type": "sendreceive",
            "paused": False,
            "ignorePerms": False,
            "order": "alphabetic",
            "maxConflicts": 10,
            "versioning": {"type": "", "params": {}},
            "devices": [{"deviceID": self.my_id}, *({"deviceID": d} for d in shared_with)],
        }

    def delay(self, path: str, seconds: float) -> None:
        with self._lock:
            self.delays[path] = seconds

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
        need_files: int | None = 0,
        need_bytes: int = 0,
        pull_errors: int | None = 0,
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

    def stop_after(self, method: str, path: str) -> None:
        with self._lock:
            self._stop_after = (method, path)

    def start(self) -> None:
        with self._lock:
            self.stopped = False
            self._stop_after = None

    def close(self) -> None:
        with self._lock:
            events = list(self.holds.values())
        for ev in events:
            ev.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)
