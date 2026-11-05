"""Read-only local Syncthing configuration discovery, REST client and folder lint.

Leaf module: standard library only, never imports from ``mind_meld`` and never
mutates Syncthing's configuration or state.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import ssl
import stat
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

_MAX_CONFIG_BYTES = 1024 * 1024  # 1 MiB ceiling for config.xml and .stignore
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB ceiling per REST response
_DEFAULT_STIGNORE_LINE = "(?d)tmp*.tmp"
_AUTH_HEADER = "X-API-" + "Key"
_UNSPECIFIED_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]"})

# (option name, wanted display value, reason)
FOLDER_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("type", "sendreceive", "every machine both sends and receives fleet files"),
    ("versioning", "none", "mm keeps its own conflict and deleted-file copies"),
    ("maxConflicts", "not 0", "0 deletes losing conflict copies silently"),
    ("ignorePerms", "false", "mode 0600 on synced files must survive"),
    ("order", "alphabetic", "blobs under data/ arrive before the manifests/ that name them"),
)


@dataclass(frozen=True)
class SyncthingState:
    """Read-only snapshot of local Syncthing reachability, folder state and lint."""

    state: str
    cause: str | None = None
    config_dir: Path | None = None
    gui_address: str | None = None
    folder_id: str | None = None
    folder_label: str | None = None
    folder_path: Path | None = None
    folder_paused: bool | None = None
    folder_state: str | None = None
    files_to_fetch: int | None = None
    bytes_to_fetch: int | None = None
    pull_errors: int | None = None
    connected_peers: int | None = None
    total_peers: int | None = None
    disconnected_peers: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    stignore: str | None = None


def config_dirs() -> list[Path]:
    """Candidate Syncthing configuration directories in search order."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Syncthing"]
    dirs: list[Path] = []
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        dirs.append(Path(xdg_config).expanduser() / "syncthing")
    default_config = home / ".config" / "syncthing"
    if default_config not in dirs:
        dirs.append(default_config)
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state:
        state_dir = Path(xdg_state).expanduser() / "syncthing"
        if state_dir not in dirs:
            dirs.append(state_dir)
    default_state = home / ".local" / "state" / "syncthing"
    if default_state not in dirs:
        dirs.append(default_state)
    return dirs


def _discover_config() -> tuple[str, Path | None, bool, str | None, str | None]:
    """Return ``(status, config_dir, use_tls, raw_address, credential)``."""
    for cand_dir in config_dirs():
        cfg_path = cand_dir / "config.xml"
        try:
            info = cfg_path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return ("config-unreadable", cand_dir, False, None, None)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
            return ("config-unreadable", cand_dir, False, None, None)
        try:
            raw_bytes = cfg_path.read_bytes()
        except OSError:
            return ("config-unreadable", cand_dir, False, None, None)
        if len(raw_bytes) > _MAX_CONFIG_BYTES:
            return ("config-unreadable", cand_dir, False, None, None)
        try:
            root = ET.fromstring(raw_bytes.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ET.ParseError):
            return ("config-unreadable", cand_dir, False, None, None)
        gui = root.find("gui")
        if gui is None:
            return ("config-unreadable", cand_dir, False, None, None)
        tls_attr = (gui.attrib.get("tls") or "").strip().lower()
        use_tls = tls_attr == "true"
        addr_elem = gui.find("address")
        hdr_elem = gui.find("api" + "key")
        address = (addr_elem.text or "").strip() if addr_elem is not None else ""
        header_val = (hdr_elem.text or "").strip() if hdr_elem is not None else ""
        if not address or not header_val:
            return ("config-unreadable", cand_dir, use_tls, None, None)
        return ("ok", cand_dir, use_tls, address, header_val)
    return ("not-found", None, False, None, None)


def _normalize_address(raw_address: str) -> str | None:
    """Normalize GUI ``host:port``; return ``None`` for ``unix://`` or invalid."""
    addr = raw_address.strip()
    if not addr or addr.startswith(("unix:", "unix://", "/")):
        return None
    if "://" in addr:
        return None
    if addr.startswith("["):
        bracket_end = addr.find("]")
        if bracket_end == -1 or bracket_end + 1 >= len(addr) or addr[bracket_end + 1] != ":":
            return None
        host = addr[: bracket_end + 1]
        port_str = addr[bracket_end + 2 :]
    else:
        if addr.count(":") != 1:
            return None
        host, port_str = addr.split(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        return None
    if port < 1 or port > 65535:
        return None
    if host in _UNSPECIFIED_HOSTS:
        host = "127.0.0.1"
    return f"{host}:{port}"


def _build_tls_context(config_dir: Path, *, use_tls: bool) -> ssl.SSLContext | None:
    if not use_tls:
        return None
    cert_path = config_dir / "https-cert.pem"
    info = cert_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
        raise ssl.SSLError("https-cert.pem is not a regular file")
    ctx = ssl.create_default_context(cafile=os.fspath(cert_path))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class _DeadlineReader(io.RawIOBase):
    """Bound every raw socket read, including reads hidden inside HTTP parsing."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._stream = sock.makefile("rb", buffering=0)
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Syncthing response deadline expired")
        self._sock.settimeout(remaining)
        return self._stream.readinto(buffer)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            super().close()


class _DeadlineResponse(http.client.HTTPResponse):
    def __init__(self, sock: socket.socket, *, deadline: float, **kwargs: Any) -> None:
        super().__init__(sock, **kwargs)
        # Keep a makefile reference: HTTPConnection may close its own socket
        # reference immediately after headers when the server sends close.
        reader = _DeadlineReader(sock, deadline)
        self.fp.close()
        self.fp = io.BufferedReader(reader)


def _get_json(
    norm_addr: str,
    path: str,
    *,
    header_val: str,
    tls_context: ssl.SSLContext | None,
    timeout: float,
    deadline: float,
) -> tuple[str, Any, str | None]:
    """Perform one direct GET request without consulting proxy environment variables.

    Returns ``(outcome, parsed_json, cause)`` where ``outcome`` is one of:
    - ``"ok"``: parsed JSON returned
    - ``"unknown"``: 404 or unparseable body (only this endpoint stays unknown)
    - ``"unauthorized"``: 401 or 403
    - ``"unreachable"``: transport/redirect/TLS/timeout failure
    - ``"error"``: other HTTP status error
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ("unreachable", None, "timeout")
    req_timeout = min(timeout, remaining)
    split = urllib.parse.urlsplit(f"http://{norm_addr}")
    host = split.hostname or "127.0.0.1"
    port = split.port or 8384
    if tls_context is not None:
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=req_timeout, context=tls_context
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=req_timeout)
    conn.response_class = partial(
        _DeadlineResponse, deadline=min(deadline, time.monotonic() + req_timeout)
    )
    try:
        conn.request(
            "GET",
            path,
            headers={
                _AUTH_HEADER: header_val,
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        resp = conn.getresponse()
        status = resp.status
        body = resp.read(_MAX_RESPONSE_BYTES + 1) if status == 200 else b""
    except ssl.SSLError:
        return ("unreachable", None, "tls")
    except TimeoutError:
        return ("unreachable", None, "timeout")
    except (OSError, http.client.HTTPException) as exc:
        if isinstance(exc, ssl.SSLError):
            return ("unreachable", None, "tls")
        if isinstance(exc, TimeoutError):
            return ("unreachable", None, "timeout")
        return ("unreachable", None, "refused")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if 300 <= status < 400:
        return ("unreachable", None, "redirect")
    if status in (401, 403):
        return ("unauthorized", None, f"http-{status}")
    if status == 404:
        return ("unknown", None, "http-404")
    if status != 200:
        return ("error", None, f"http-{status}")

    if len(body) > _MAX_RESPONSE_BYTES:
        return ("unknown", None, "bad-response")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ("unknown", None, "bad-response")
    return ("ok", parsed, None)


def _expand_folder_path(raw_path: str) -> Path:
    return Path(raw_path).expanduser().resolve(strict=False)


def _select_folder(
    folders: list[Any], storage_path: Path
) -> tuple[dict[str, Any] | None, Path | None]:
    """Match the folder equal to ``storage_path``, else the deepest ancestor."""
    target = Path(storage_path).expanduser().resolve(strict=False)
    exact: tuple[dict[str, Any], Path] | None = None
    containing: list[tuple[int, dict[str, Any], Path]] = []
    for entry in folders:
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        try:
            cand = _expand_folder_path(raw_path.strip())
        except OSError:
            continue
        if cand == target:
            exact = (entry, cand)
            break
        if target.is_relative_to(cand):
            containing.append((len(cand.parts), entry, cand))
    if exact is not None:
        return exact
    if containing:
        containing.sort(key=lambda item: item[0], reverse=True)
        _, best_entry, best_path = containing[0]
        return best_entry, best_path
    return None, None


def option_is_ok(name: str, value: Any) -> bool:
    """Return whether one folder option value satisfies ``FOLDER_OPTIONS``."""
    if name == "type":
        return value == "sendreceive"
    if name == "versioning":
        return value in ("", "none")
    if name == "maxConflicts":
        return isinstance(value, int) and not isinstance(value, bool) and value != 0
    if name == "ignorePerms":
        return value is False
    if name == "order":
        return value == "alphabetic"
    return False


def _extract_options(folder: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Extract the 5 ``FOLDER_OPTIONS`` values and return ``(options, problems)``."""
    options: dict[str, Any] = {}
    problems: list[str] = []

    folder_type = folder.get("type")
    options["type"] = folder_type if isinstance(folder_type, str) else None

    versioning_raw = folder.get("versioning")
    if isinstance(versioning_raw, dict):
        vtype = versioning_raw.get("type", "")
        options["versioning"] = vtype if isinstance(vtype, str) else None
    elif isinstance(versioning_raw, str):
        options["versioning"] = versioning_raw
    else:
        options["versioning"] = None

    max_conflicts = folder.get("maxConflicts")
    options["maxConflicts"] = (
        max_conflicts
        if isinstance(max_conflicts, int) and not isinstance(max_conflicts, bool)
        else None
    )

    ignore_perms = folder.get("ignorePerms")
    options["ignorePerms"] = ignore_perms if isinstance(ignore_perms, bool) else None

    order = folder.get("order")
    options["order"] = order if isinstance(order, str) else None

    for opt_name, _, _ in FOLDER_OPTIONS:
        if not option_is_ok(opt_name, options.get(opt_name)):
            problems.append(opt_name)

    return options, problems


def _lint_stignore(folder_root: Path, stignore_line: str) -> tuple[str, str | None]:
    """Check ``<folder_root>/.stignore`` for ``stignore_line``.

    Returns ``(stignore_state, problem_or_none)``.
    """
    st_path = Path(folder_root) / ".stignore"
    try:
        info = st_path.lstat()
    except FileNotFoundError:
        return ("missing", "stignore-missing")
    except OSError:
        return ("unreadable", "stignore-unreadable")
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
        return ("unreadable", "stignore-unreadable")
    try:
        raw = st_path.read_bytes()
    except OSError:
        return ("unreadable", "stignore-unreadable")
    if len(raw) > _MAX_CONFIG_BYTES:
        return ("unreadable", "stignore-unreadable")
    wanted = stignore_line.strip().encode("utf-8")
    if wanted in (line.strip() for line in raw.splitlines()):
        return ("ok", None)
    return ("missing-line", "stignore-line")


def read_state(
    storage_path: str | Path,
    *,
    stignore_line: str = _DEFAULT_STIGNORE_LINE,
    timeout: float = 2.0,
    budget: float = 5.0,
) -> SyncthingState:
    """Discover local Syncthing, query its REST API read-only and lint options."""
    try:
        return _read_state_inner(
            Path(storage_path),
            stignore_line=stignore_line,
            timeout=timeout,
            budget=budget,
        )
    except Exception:
        return SyncthingState(state="error", cause="bad-response")


def _read_state_inner(
    storage_path: Path,
    *,
    stignore_line: str,
    timeout: float,
    budget: float,
) -> SyncthingState:
    disc_state, cfg_dir, use_tls, raw_addr, header_val = _discover_config()
    if disc_state == "not-found" or cfg_dir is None:
        return SyncthingState(state="not-found")
    if disc_state != "ok" or raw_addr is None or header_val is None:
        return SyncthingState(state="config-unreadable", config_dir=cfg_dir)

    st_state, st_problem = _lint_stignore(storage_path, stignore_line)
    initial_problems = (st_problem,) if st_problem is not None else ()

    norm_addr = _normalize_address(raw_addr)
    if norm_addr is None:
        return SyncthingState(
            state="address-unsupported",
            config_dir=cfg_dir,
            gui_address=raw_addr,
            problems=initial_problems,
            stignore=st_state,
        )

    try:
        tls_context = _build_tls_context(cfg_dir, use_tls=use_tls)
    except (OSError, ssl.SSLError):
        return SyncthingState(
            state="unreachable",
            cause="tls",
            config_dir=cfg_dir,
            gui_address=norm_addr,
            problems=initial_problems,
            stignore=st_state,
        )

    deadline = time.monotonic() + max(0.0, float(budget))
    per_req_timeout = max(0.01, float(timeout))

    # 1. /rest/system/status
    out1, sys_status, cause1 = _get_json(
        norm_addr,
        "/rest/system/status",
        header_val=header_val,
        tls_context=tls_context,
        timeout=per_req_timeout,
        deadline=deadline,
    )
    if out1 in ("unreachable", "unauthorized", "error"):
        return SyncthingState(
            state=out1,
            cause=cause1,
            config_dir=cfg_dir,
            gui_address=norm_addr,
            problems=initial_problems,
            stignore=st_state,
        )
    my_id: str | None = None
    if out1 == "ok" and isinstance(sys_status, dict):
        raw_my_id = sys_status.get("myID")
        if isinstance(raw_my_id, str) and raw_my_id.strip():
            my_id = raw_my_id.strip()

    # 2. /rest/system/connections
    out2, sys_conns, cause2 = _get_json(
        norm_addr,
        "/rest/system/connections",
        header_val=header_val,
        tls_context=tls_context,
        timeout=per_req_timeout,
        deadline=deadline,
    )
    if out2 in ("unreachable", "unauthorized", "error"):
        return SyncthingState(
            state=out2,
            cause=cause2,
            config_dir=cfg_dir,
            gui_address=norm_addr,
            problems=initial_problems,
            stignore=st_state,
        )
    conns_map: dict[str, bool] | None = None
    if out2 == "ok" and isinstance(sys_conns, dict):
        raw_conns = sys_conns.get("connections")
        if isinstance(raw_conns, dict):
            conns_map = {}
            for dev_id, info in raw_conns.items():
                if isinstance(dev_id, str) and isinstance(info, dict):
                    conns_map[dev_id] = bool(info.get("connected", False))

    # 3. /rest/config/folders
    out3, cfg_folders, cause3 = _get_json(
        norm_addr,
        "/rest/config/folders",
        header_val=header_val,
        tls_context=tls_context,
        timeout=per_req_timeout,
        deadline=deadline,
    )
    if out3 in ("unreachable", "unauthorized", "error"):
        return SyncthingState(
            state=out3,
            cause=cause3,
            config_dir=cfg_dir,
            gui_address=norm_addr,
            problems=initial_problems,
            stignore=st_state,
        )
    folders_list: list[Any] | None = (
        cfg_folders if out3 == "ok" and isinstance(cfg_folders, list) else None
    )

    matched_folder: dict[str, Any] | None = None
    folder_path: Path | None = None
    if folders_list is not None:
        matched_folder, folder_path = _select_folder(folders_list, storage_path)

    # 4. /rest/config/devices
    out4, cfg_devices, cause4 = _get_json(
        norm_addr,
        "/rest/config/devices",
        header_val=header_val,
        tls_context=tls_context,
        timeout=per_req_timeout,
        deadline=deadline,
    )
    if out4 in ("unreachable", "unauthorized", "error"):
        return SyncthingState(
            state=out4,
            cause=cause4,
            config_dir=cfg_dir,
            gui_address=norm_addr,
            problems=initial_problems,
            stignore=st_state,
        )
    device_names: dict[str, str] | None = None
    if out4 == "ok" and isinstance(cfg_devices, list):
        device_names = {}
        for entry in cfg_devices:
            if not isinstance(entry, dict):
                continue
            dev_id = entry.get("deviceID")
            dev_name = entry.get("name")
            if isinstance(dev_id, str) and dev_id.strip():
                if isinstance(dev_name, str) and dev_name.strip():
                    device_names[dev_id.strip()] = dev_name.strip()

    problems: list[str] = []
    folder_id: str | None = None
    folder_label: str | None = None
    folder_paused: bool | None = None
    options: dict[str, Any] = {}
    folder_peer_ids: list[str] | None = None

    if folders_list is not None and matched_folder is None:
        problems.append("no-folder")
    elif matched_folder is not None:
        raw_fid = matched_folder.get("id")
        if isinstance(raw_fid, str) and raw_fid.strip():
            folder_id = raw_fid.strip()
        raw_label = matched_folder.get("label")
        if isinstance(raw_label, str) and raw_label.strip():
            folder_label = raw_label.strip()
        raw_paused = matched_folder.get("paused")
        if isinstance(raw_paused, bool):
            folder_paused = raw_paused
            if raw_paused:
                problems.append("paused")
        options, opt_problems = _extract_options(matched_folder)
        problems.extend(opt_problems)

        raw_devs = matched_folder.get("devices")
        if isinstance(raw_devs, list) and my_id is not None:
            folder_peer_ids = []
            for dev in raw_devs:
                if isinstance(dev, dict):
                    did = dev.get("deviceID")
                elif isinstance(dev, str):
                    did = dev
                else:
                    did = None
                if isinstance(did, str) and did.strip() and did.strip() != my_id:
                    if did.strip() not in folder_peer_ids:
                        folder_peer_ids.append(did.strip())

    stignore_root = folder_path if folder_path is not None else storage_path
    st_state, st_problem = _lint_stignore(stignore_root, stignore_line)
    if st_problem is not None:
        problems.append(st_problem)

    total_peers: int | None = None
    connected_peers: int | None = None
    disconnected_peers: list[str] = []
    if folder_peer_ids is not None:
        total_peers = len(folder_peer_ids)
        if conns_map is not None:
            connected_count = 0
            for pid in folder_peer_ids:
                if conns_map.get(pid, False):
                    connected_count += 1
                elif device_names is not None and pid in device_names:
                    disconnected_peers.append(device_names[pid])
            connected_peers = connected_count

    # 5. /rest/db/status?folder=<id>
    folder_state: str | None = "paused" if folder_paused else None
    files_to_fetch: int | None = None
    bytes_to_fetch: int | None = None
    pull_errors: int | None = None

    if folder_id is not None:
        query = urllib.parse.urlencode({"folder": folder_id})
        out5, db_status, cause5 = _get_json(
            norm_addr,
            f"/rest/db/status?{query}",
            header_val=header_val,
            tls_context=tls_context,
            timeout=per_req_timeout,
            deadline=deadline,
        )
        if out5 in ("unreachable", "unauthorized", "error"):
            return SyncthingState(
                state=out5,
                cause=cause5,
                config_dir=cfg_dir,
                gui_address=norm_addr,
                folder_id=folder_id,
                folder_label=folder_label,
                folder_path=folder_path,
                folder_paused=folder_paused,
                connected_peers=connected_peers,
                total_peers=total_peers,
                disconnected_peers=tuple(disconnected_peers),
                options=options,
                problems=tuple(problems),
                stignore=st_state,
            )
        if out5 == "ok" and isinstance(db_status, dict):
            raw_state = db_status.get("state")
            if isinstance(raw_state, str) and raw_state.strip():
                folder_state = "paused" if folder_paused else raw_state.strip()
            nf = db_status.get("needFiles")
            if isinstance(nf, int) and not isinstance(nf, bool):
                files_to_fetch = nf
            nb = db_status.get("needBytes")
            if isinstance(nb, int) and not isinstance(nb, bool):
                bytes_to_fetch = nb
            pe = db_status.get("pullErrors")
            if isinstance(pe, int) and not isinstance(pe, bool):
                pull_errors = pe

    return SyncthingState(
        state="ok",
        cause=None,
        config_dir=cfg_dir,
        gui_address=norm_addr,
        folder_id=folder_id,
        folder_label=folder_label,
        folder_path=folder_path,
        folder_paused=folder_paused,
        folder_state=folder_state,
        files_to_fetch=files_to_fetch,
        bytes_to_fetch=bytes_to_fetch,
        pull_errors=pull_errors,
        connected_peers=connected_peers,
        total_peers=total_peers,
        disconnected_peers=tuple(disconnected_peers),
        options=options,
        problems=tuple(problems),
        stignore=st_state,
    )
