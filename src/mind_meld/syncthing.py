"""Local Syncthing discovery, REST client, folder lint, default-config check and gated writer.

Leaf module: standard library only and never imports from ``mind_meld``.
``read_state`` and ``classify_config`` send only ``GET`` (``classify_config``
falls back to reading ``config.xml`` when the API is unreachable). Only
``SyncthingWriter`` writes, only through ``_WRITE_ROUTES``, and only with a
``ManagedRecord`` that matches the live local device.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import re
import socket
import ssl
import stat
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

_MAX_CONFIG_BYTES = 1024 * 1024  # 1 MiB ceiling for config.xml and .stignore
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB ceiling per REST response
_DEFAULT_STIGNORE_LINE = "(?d)tmp*.tmp"
_AUTH_HEADER = "X-API-" + "Key"
_UNSPECIFIED_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]"})
_LUHN32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_DEVICE_ID_RE = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}\Z")
_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

STOCK_DEFAULT_FOLDER_ID = "default"

# (option name, wanted display value, reason)
FOLDER_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("type", "sendreceive", "every machine both sends and receives fleet files"),
    ("versioning", "none", "mm keeps its own conflict and deleted-file copies"),
    ("maxConflicts", "not 0", "0 deletes losing conflict copies silently"),
    ("ignorePerms", "false", "mode 0600 on synced files must survive"),
    ("order", "alphabetic", "blobs under data/ arrive before the manifests/ that name them"),
)

_MM_FOLDER_VALUES: dict[str, Any] = {
    "type": "sendreceive",
    "versioning": {"type": ""},
    "maxConflicts": 10,
    "ignorePerms": False,
    "order": "alphabetic",
}

_WRITE_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/rest/config/folders"),
    ("POST", "/rest/config/devices"),
    ("PATCH", "/rest/config/folders/{id}"),
    ("PATCH", "/rest/config/devices/{id}"),
    ("DELETE", "/rest/config/folders/default"),
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


def _load_config() -> tuple[str, Path | None, bool, str | None, str | None, ET.Element | None]:
    """Return ``(status, config_dir, use_tls, raw_address, credential, root)``."""
    for cand_dir in config_dirs():
        cfg_path = cand_dir / "config.xml"
        try:
            info = cfg_path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return ("config-unreadable", cand_dir, False, None, None, None)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
            return ("config-unreadable", cand_dir, False, None, None, None)
        try:
            raw_bytes = cfg_path.read_bytes()
        except OSError:
            return ("config-unreadable", cand_dir, False, None, None, None)
        if len(raw_bytes) > _MAX_CONFIG_BYTES:
            return ("config-unreadable", cand_dir, False, None, None, None)
        try:
            root = ET.fromstring(raw_bytes.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ET.ParseError):
            return ("config-unreadable", cand_dir, False, None, None, None)
        gui = root.find("gui")
        if gui is None:
            return ("config-unreadable", cand_dir, False, None, None, None)
        tls_attr = (gui.attrib.get("tls") or "").strip().lower()
        use_tls = tls_attr == "true"
        addr_elem = gui.find("address")
        hdr_elem = gui.find("api" + "key")
        address = (addr_elem.text or "").strip() if addr_elem is not None else ""
        header_val = (hdr_elem.text or "").strip() if hdr_elem is not None else ""
        if not address or not header_val:
            return ("config-unreadable", cand_dir, use_tls, None, None, None)
        return ("ok", cand_dir, use_tls, address, header_val, root)
    return ("not-found", None, False, None, None, None)


def _discover_config() -> tuple[str, Path | None, bool, str | None, str | None]:
    """Return ``(status, config_dir, use_tls, raw_address, credential)``."""
    status, cand_dir, use_tls, address, header_val, _ = _load_config()
    return (status, cand_dir, use_tls, address, header_val)


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


@dataclass(frozen=True)
class _Endpoint:
    config_dir: Path
    norm_addr: str
    header_val: str = field(repr=False)
    tls_context: ssl.SSLContext | None = field(repr=False)


def _open_endpoint() -> tuple[_Endpoint | None, str, str | None, Path | None, ET.Element | None]:
    """Open local Syncthing endpoint metadata: ``(endpoint, reach, cause, config_dir, root)``."""
    disc_state, cfg_dir, use_tls, raw_addr, header_val, root = _load_config()
    if disc_state == "not-found" or cfg_dir is None:
        return (None, "not-found", None, None, None)
    if disc_state != "ok" or raw_addr is None or header_val is None or root is None:
        return (None, "config-unreadable", None, cfg_dir, None)
    norm_addr = _normalize_address(raw_addr)
    if norm_addr is None:
        return (None, "address-unsupported", None, cfg_dir, root)
    try:
        tls_context = _build_tls_context(cfg_dir, use_tls=use_tls)
    except (OSError, ssl.SSLError):
        return (None, "unreachable", "tls", cfg_dir, root)
    return (
        _Endpoint(
            config_dir=cfg_dir,
            norm_addr=norm_addr,
            header_val=header_val,
            tls_context=tls_context,
        ),
        "ok",
        None,
        cfg_dir,
        root,
    )


def is_device_id(value: object) -> bool:
    """Return whether ``value`` is a canonical Syncthing device ID."""
    return isinstance(value, str) and bool(_DEVICE_ID_RE.fullmatch(value))


def stock_default_folder_path() -> Path:
    """Return Syncthing's stock Default Folder path (``~/Sync``) at call time."""
    return Path.home() / "Sync"


def _folder_is_empty(path: Path) -> bool:
    """Return whether ``path`` is absent or holds only ``.stfolder`` and ``.DS_Store``."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name == ".DS_Store":
                    st = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode):
                        return False
                elif entry.name == ".stfolder":
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        if st.st_size != 0:
                            return False
                    elif stat.S_ISDIR(st.st_mode):
                        with os.scandir(entry.path) as sub_it:
                            for sub in sub_it:
                                if not (
                                    sub.name.startswith("syncthing-folder-")
                                    and sub.name.endswith(".txt")
                                    and len(sub.name) > len("syncthing-folder-.txt")
                                ):
                                    return False
                                sub_st = sub.stat(follow_symlinks=False)
                                if not stat.S_ISREG(sub_st.st_mode):
                                    return False
                    else:
                        return False
                else:
                    return False
    except OSError:
        return False
    return True


def _folder_device_ids(folder: dict[str, Any]) -> list[str]:
    """Extract stripped device IDs from a folder dict's ``devices`` entry."""
    raw_devs = folder.get("devices")
    if not isinstance(raw_devs, list):
        return []
    out: list[str] = []
    for dev in raw_devs:
        if isinstance(dev, dict):
            did = dev.get("deviceID")
        elif isinstance(dev, str):
            did = dev
        else:
            did = None
        if isinstance(did, str) and did.strip():
            out.append(did.strip())
    return out


def _stock_default_problem(folder: dict[str, Any], my_id: str | None) -> str | None:
    """Return ``"shared"``, ``"not-empty"`` or ``None`` for the stock Default Folder."""
    for did in _folder_device_ids(folder):
        if my_id is None or did != my_id:
            return "shared"
    raw_path = folder.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return "not-empty"
    if not _folder_is_empty(Path(raw_path.strip()).expanduser()):
        return "not-empty"
    return None


@dataclass(frozen=True)
class PreExisting:
    """One sanitized reason a Syncthing configuration is pre-existing."""

    kind: str
    name: str
    why: str


@dataclass(frozen=True)
class ManagedRecord:
    """Proof that mm found the default Syncthing configuration on this machine."""

    device_id: str


@dataclass(frozen=True)
class ConfigCheck:
    """Result of classifying local Syncthing configuration."""

    state: str
    source: str | None = None
    reach: str = "ok"
    cause: str | None = None
    reasons: tuple[PreExisting, ...] = ()
    local_device_id: str | None = field(default=None, repr=False)

    def managed_record(self) -> ManagedRecord:
        if (
            self.state != "default"
            or self.source != "rest"
            or not isinstance(self.local_device_id, str)
            or not self.local_device_id
        ):
            raise ValueError("ManagedRecord requires a live REST default configuration")
        return ManagedRecord(device_id=self.local_device_id)


def _classify_shapes(
    local_id: str | None,
    devices: list[dict[str, Any]],
    folders: list[dict[str, Any]],
    sanitize: Callable[[str], str],
) -> tuple[PreExisting, ...]:
    """Classify REST or offline device and folder shapes with one shared rule."""
    reasons: list[PreExisting] = []
    for dev in devices:
        dev_id = (dev.get("deviceID") or "").strip()
        if local_id is not None and dev_id == local_id:
            continue
        raw_name = dev.get("name")
        name_str = raw_name if isinstance(raw_name, str) else ""
        reasons.append(PreExisting("device", sanitize(name_str) or "", "paired"))

    stock_resolved = stock_default_folder_path().resolve(strict=False)
    for folder in folders:
        fid = (folder.get("id") or "").strip()
        raw_label = folder.get("label")
        label_str = raw_label if isinstance(raw_label, str) and raw_label else fid
        raw_path = folder.get("path")
        path_str = raw_path.strip() if isinstance(raw_path, str) else ""
        is_stock = False
        if fid == STOCK_DEFAULT_FOLDER_ID and path_str:
            try:
                is_stock = _expand_folder_path(path_str) == stock_resolved
            except OSError:
                is_stock = False
        if is_stock:
            why = _stock_default_problem(folder, local_id)
            if why is not None:
                reasons.append(PreExisting("folder", sanitize(label_str) or "", why))
        else:
            reasons.append(PreExisting("folder", sanitize(label_str) or "", "folder"))
    return tuple(reasons)


def _config_shapes(
    root: ET.Element,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Convert direct ``<device>`` and ``<folder>`` children of ``<configuration>``."""
    if root.tag != "configuration":
        return None
    devices: list[dict[str, Any]] = []
    for dev_el in root.findall("device"):
        dev_id = (dev_el.get("id") or "").strip()
        if not dev_id:
            return None
        dev_name = dev_el.get("name") or ""
        devices.append({"deviceID": dev_id, "name": dev_name})
    if not devices:
        return None

    folders: list[dict[str, Any]] = []
    for fld_el in root.findall("folder"):
        fld_id = (fld_el.get("id") or "").strip()
        fld_path = (fld_el.get("path") or "").strip()
        if not fld_id or not fld_path:
            return None
        fld_label = fld_el.get("label") or ""
        fld_devs: list[dict[str, str]] = []
        for fdev_el in fld_el.findall("device"):
            fdev_id = (fdev_el.get("id") or "").strip()
            if not fdev_id:
                return None
            fld_devs.append({"deviceID": fdev_id})
        folders.append(
            {
                "id": fld_id,
                "label": fld_label,
                "path": fld_path,
                "devices": fld_devs,
            }
        )
    return (devices, folders)


def _luhn32(block: str) -> str:
    """Compute Syncthing's Luhn-32 check character for ``block``."""
    factor = 1
    total = 0
    for ch in block:
        code = _LUHN32_ALPHABET.index(ch)
        addend = factor * code
        factor = 2 if factor == 1 else 1
        addend = (addend // 32) + (addend % 32)
        total += addend
    rem = total % 32
    check = (32 - rem) % 32
    return _LUHN32_ALPHABET[check]


def _device_id_from_cert(config_dir: Path) -> str | None:
    """Derive this machine's Syncthing device ID from public ``cert.pem``."""
    try:
        cert_path = config_dir / "cert.pem"
        info = cert_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
            return None
        raw_bytes = cert_path.read_bytes()
        if len(raw_bytes) > _MAX_CONFIG_BYTES:
            return None
        pem_text = raw_bytes.decode("ascii", errors="strict")
        der = ssl.PEM_cert_to_DER_cert(pem_text)
        if not der:
            return None
        digest = hashlib.sha256(der).digest()
        b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
        if len(b32) != 52:
            return None
        with_check = "".join(
            b32[idx : idx + 13] + _luhn32(b32[idx : idx + 13]) for idx in range(0, 52, 13)
        )
        return "-".join(with_check[idx : idx + 7] for idx in range(0, 56, 7))
    except Exception:
        return None


def _identity_files_present() -> bool:
    """Check by ``lstat`` only whether ``cert.pem`` or ``key.pem`` exists in any candidate dir."""
    for cand_dir in config_dirs():
        for fname in ("cert.pem", "key.pem"):
            try:
                (cand_dir / fname).lstat()
                return True
            except OSError:
                continue
    return False


def classify_config(
    *,
    sanitize: Callable[[str], str],
    timeout: float = 2.0,
    budget: float = 5.0,
) -> ConfigCheck:
    """Classify local Syncthing configuration over REST or offline ``config.xml``."""
    try:
        return _classify_config_inner(sanitize=sanitize, timeout=timeout, budget=budget)
    except Exception:
        return ConfigCheck(state="unknown", source=None, reach="error", cause="bad-response")


def _classify_config_inner(
    *,
    sanitize: Callable[[str], str],
    timeout: float,
    budget: float,
) -> ConfigCheck:
    endpoint, reach, cause, cfg_dir, root = _open_endpoint()
    if reach == "not-found":
        if _identity_files_present():
            return ConfigCheck(
                state="unknown",
                source=None,
                reach="not-found",
                cause="config-missing",
            )
        return ConfigCheck(state="no-config", source=None, reach="not-found", cause=None)
    if reach == "config-unreadable":
        return ConfigCheck(
            state="unknown",
            source=None,
            reach="config-unreadable",
            cause="config-unreadable",
        )

    if endpoint is not None:
        deadline = time.monotonic() + max(0.0, float(budget))
        per_req_timeout = max(0.01, float(timeout))

        out1, sys_status, cause1 = _get_json(
            endpoint.norm_addr,
            "/rest/system/status",
            header_val=endpoint.header_val,
            tls_context=endpoint.tls_context,
            timeout=per_req_timeout,
            deadline=deadline,
        )
        if out1 == "unreachable":
            reach, cause = "unreachable", cause1
        elif out1 == "unauthorized":
            return ConfigCheck(state="unknown", source=None, reach="unauthorized", cause=cause1)
        elif out1 != "ok" or not isinstance(sys_status, dict):
            return ConfigCheck(
                state="unknown",
                source=None,
                reach="error",
                cause=cause1 or "bad-response",
            )
        else:
            raw_my_id = sys_status.get("myID")
            my_id = raw_my_id.strip() if isinstance(raw_my_id, str) else ""
            if not my_id:
                return ConfigCheck(
                    state="unknown", source=None, reach="error", cause="bad-response"
                )

            out2, cfg_devices, cause2 = _get_json(
                endpoint.norm_addr,
                "/rest/config/devices",
                header_val=endpoint.header_val,
                tls_context=endpoint.tls_context,
                timeout=per_req_timeout,
                deadline=deadline,
            )
            if out2 == "unreachable":
                reach, cause = "unreachable", cause2
            elif out2 == "unauthorized":
                return ConfigCheck(state="unknown", source=None, reach="unauthorized", cause=cause2)
            elif out2 != "ok" or not isinstance(cfg_devices, list):
                return ConfigCheck(
                    state="unknown",
                    source=None,
                    reach="error",
                    cause=cause2 or "bad-response",
                )
            else:
                for entry in cfg_devices:
                    if (
                        not isinstance(entry, dict)
                        or not isinstance(entry.get("deviceID"), str)
                        or not entry["deviceID"].strip()
                    ):
                        return ConfigCheck(
                            state="unknown",
                            source=None,
                            reach="error",
                            cause="bad-response",
                        )

                out3, cfg_folders, cause3 = _get_json(
                    endpoint.norm_addr,
                    "/rest/config/folders",
                    header_val=endpoint.header_val,
                    tls_context=endpoint.tls_context,
                    timeout=per_req_timeout,
                    deadline=deadline,
                )
                if out3 == "unreachable":
                    reach, cause = "unreachable", cause3
                elif out3 == "unauthorized":
                    return ConfigCheck(
                        state="unknown", source=None, reach="unauthorized", cause=cause3
                    )
                elif out3 != "ok" or not isinstance(cfg_folders, list):
                    return ConfigCheck(
                        state="unknown",
                        source=None,
                        reach="error",
                        cause=cause3 or "bad-response",
                    )
                else:
                    for entry in cfg_folders:
                        if (
                            not isinstance(entry, dict)
                            or not isinstance(entry.get("id"), str)
                            or not entry["id"].strip()
                            or not isinstance(entry.get("path"), str)
                            or not entry["path"].strip()
                        ):
                            return ConfigCheck(
                                state="unknown",
                                source=None,
                                reach="error",
                                cause="bad-response",
                            )
                    reasons = _classify_shapes(my_id, cfg_devices, cfg_folders, sanitize)
                    return ConfigCheck(
                        state="pre-existing" if reasons else "default",
                        source="rest",
                        reach="ok",
                        cause=None,
                        reasons=reasons,
                        local_device_id=my_id,
                    )

    # Offline fallback when REST is unreachable or GUI address is unsupported
    if root is None:
        return ConfigCheck(state="unknown", source=None, reach=reach, cause="bad-config")
    shapes = _config_shapes(root)
    if shapes is None:
        return ConfigCheck(state="unknown", source=None, reach=reach, cause="bad-config")
    devices, folders = shapes
    if len(devices) == 1:
        offline_local_id: str | None = devices[0]["deviceID"]
    else:
        cert_id = _device_id_from_cert(cfg_dir) if cfg_dir is not None else None
        offline_local_id = (
            cert_id
            if cert_id is not None and any(d["deviceID"] == cert_id for d in devices)
            else None
        )
    reasons = _classify_shapes(offline_local_id, devices, folders, sanitize)
    return ConfigCheck(
        state="pre-existing" if reasons else "default",
        source="config",
        reach=reach,
        cause=cause,
        reasons=reasons,
        local_device_id=None,
    )


def _write_route_ok(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is one of the 5 allowlisted write routes."""
    if not isinstance(method, str) or not isinstance(path, str):
        return False
    if "?" in path or "#" in path:
        return False
    if (method, path) in (
        ("POST", "/rest/config/folders"),
        ("POST", "/rest/config/devices"),
        ("DELETE", "/rest/config/folders/default"),
    ):
        return True
    if method == "PATCH" and path.startswith("/rest/config/folders/"):
        seg = path[len("/rest/config/folders/") :]
        if not seg or "/" in seg:
            return False
        unquoted = urllib.parse.unquote(seg)
        return bool(_FOLDER_ID_RE.fullmatch(unquoted))
    if method == "PATCH" and path.startswith("/rest/config/devices/"):
        seg = path[len("/rest/config/devices/") :]
        return is_device_id(seg)
    return False


def _send_json(
    endpoint: _Endpoint,
    method: str,
    path: str,
    body: Any,
    *,
    timeout: float,
    deadline: float,
) -> tuple[str, str | None]:
    """Send one allowlisted non-GET JSON request to local Syncthing."""
    if not _write_route_ok(method, path):
        raise ValueError(f"Disallowed Syncthing write route: {method} {path}")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ("unreachable", "timeout")
    req_timeout = min(timeout, remaining)
    split = urllib.parse.urlsplit(f"http://{endpoint.norm_addr}")
    host = split.hostname or "127.0.0.1"
    port = split.port or 8384
    if endpoint.tls_context is not None:
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=req_timeout, context=endpoint.tls_context
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=req_timeout)
    conn.response_class = partial(
        _DeadlineResponse, deadline=min(deadline, time.monotonic() + req_timeout)
    )
    headers: dict[str, str] = {
        _AUTH_HEADER: endpoint.header_val,
        "Connection": "close",
    }
    payload_bytes: bytes | None = None
    if method != "DELETE" and body is not None:
        payload_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
    try:
        conn.request(method, path, body=payload_bytes, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp.read(_MAX_RESPONSE_BYTES + 1)
    except ssl.SSLError:
        return ("unreachable", "tls")
    except TimeoutError:
        return ("unreachable", "timeout")
    except (OSError, http.client.HTTPException) as exc:
        if isinstance(exc, ssl.SSLError):
            return ("unreachable", "tls")
        if isinstance(exc, TimeoutError):
            return ("unreachable", "timeout")
        return ("unreachable", "refused")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if status == 200:
        return ("ok", None)
    if 300 <= status < 400:
        return ("unreachable", "redirect")
    if status in (401, 403):
        return ("unauthorized", f"http-{status}")
    return ("error", f"http-{status}")


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a gated Syncthing write operation."""

    outcome: str
    cause: str | None = None


@dataclass(frozen=True)
class PendingDevice:
    """Pending device offer; values are raw and peer-controlled — sanitize before display."""

    device_id: str
    name: str
    address: str
    time: str


@dataclass(frozen=True)
class PendingDevices:
    """Result of reading pending devices from Syncthing."""

    outcome: str
    cause: str | None = None
    devices: tuple[PendingDevice, ...] = ()


class SyncthingWriter:
    """Gated Syncthing REST writer limited to O2.2 and O2.3 operations."""

    def __init__(
        self,
        record: ManagedRecord | None,
        *,
        timeout: float = 2.0,
        budget: float = 5.0,
    ) -> None:
        self._record = record
        self._timeout = timeout
        self._budget = budget

    def _begin(
        self,
    ) -> tuple[_Endpoint | None, str | None, float, float, WriteResult | None]:
        deadline = time.monotonic() + max(0.0, float(self._budget))
        per_req_timeout = max(0.01, float(self._timeout))
        if not isinstance(self._record, ManagedRecord) or not is_device_id(self._record.device_id):
            return (
                None,
                None,
                per_req_timeout,
                deadline,
                WriteResult("refused", "not-managed"),
            )
        endpoint, reach, cause, _, _ = _open_endpoint()
        if endpoint is None:
            return (
                None,
                None,
                per_req_timeout,
                deadline,
                WriteResult(reach, cause),
            )
        out, sys_status, req_cause = _get_json(
            endpoint.norm_addr,
            "/rest/system/status",
            header_val=endpoint.header_val,
            tls_context=endpoint.tls_context,
            timeout=per_req_timeout,
            deadline=deadline,
        )
        if out in ("unreachable", "unauthorized", "error"):
            return (None, None, per_req_timeout, deadline, WriteResult(out, req_cause))
        if out != "ok" or not isinstance(sys_status, dict):
            return (
                None,
                None,
                per_req_timeout,
                deadline,
                WriteResult("error", req_cause or "bad-response"),
            )
        raw_my_id = sys_status.get("myID")
        my_id = raw_my_id.strip() if isinstance(raw_my_id, str) else ""
        if not my_id:
            return (
                None,
                None,
                per_req_timeout,
                deadline,
                WriteResult("error", "bad-response"),
            )
        if my_id != self._record.device_id:
            return (
                None,
                None,
                per_req_timeout,
                deadline,
                WriteResult("refused", "not-managed"),
            )
        return (endpoint, my_id, per_req_timeout, deadline, None)

    def _get_list(
        self,
        endpoint: _Endpoint,
        path: str,
        *,
        timeout: float,
        deadline: float,
    ) -> tuple[list[dict[str, Any]] | None, WriteResult | None]:
        out, data, cause = _get_json(
            endpoint.norm_addr,
            path,
            header_val=endpoint.header_val,
            tls_context=endpoint.tls_context,
            timeout=timeout,
            deadline=deadline,
        )
        if out in ("unreachable", "unauthorized", "error"):
            return (None, WriteResult(out, cause))
        if out != "ok" or not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
            return (None, WriteResult("error", cause or "bad-response"))
        return (data, None)

    def _create(
        self,
        endpoint: _Endpoint,
        my_id: str,
        folder_id: str,
        label: str,
        path: str | Path,
        *,
        peers: tuple[str, ...],
        timeout: float,
        deadline: float,
    ) -> WriteResult:
        if not isinstance(folder_id, str) or not _FOLDER_ID_RE.fullmatch(folder_id):
            return WriteResult("refused", "bad-folder-id")
        target_path = Path(path).expanduser().resolve(strict=False)
        folders, err = self._get_list(
            endpoint, "/rest/config/folders", timeout=timeout, deadline=deadline
        )
        if err is not None or folders is None:
            return err or WriteResult("error", "bad-response")

        for entry in folders:
            if entry.get("id") == folder_id:
                raw_p = entry.get("path")
                if not isinstance(raw_p, str) or not raw_p.strip():
                    return WriteResult("refused", "folder-exists")
                cand_path = _expand_folder_path(raw_p.strip())
                _, opt_problems = _extract_options(entry)
                dev_ids = set(_folder_device_ids(entry))
                if (
                    cand_path == target_path
                    and not opt_problems
                    and {my_id, *peers}.issubset(dev_ids)
                ):
                    return WriteResult("unchanged", None)
                return WriteResult("refused", "folder-exists")

        for entry in folders:
            raw_p = entry.get("path")
            if not isinstance(raw_p, str) or not raw_p.strip():
                continue
            cand_path = _expand_folder_path(raw_p.strip())
            if (
                cand_path == target_path
                or target_path.is_relative_to(cand_path)
                or cand_path.is_relative_to(target_path)
            ):
                return WriteResult("refused", "path-overlap")

        post_body: dict[str, Any] = {
            "id": folder_id,
            "label": label,
            "path": str(target_path),
            "paused": False,
            "devices": [{"deviceID": my_id}, *({"deviceID": pid} for pid in peers)],
            **_MM_FOLDER_VALUES,
        }
        w_out, w_cause = _send_json(
            endpoint,
            "POST",
            "/rest/config/folders",
            post_body,
            timeout=timeout,
            deadline=deadline,
        )
        if w_out != "ok":
            return WriteResult(w_out, w_cause)

        folders_after, err2 = self._get_list(
            endpoint, "/rest/config/folders", timeout=timeout, deadline=deadline
        )
        if err2 is not None or folders_after is None:
            return err2 or WriteResult("error", "bad-response")
        for entry in folders_after:
            if entry.get("id") == folder_id:
                raw_p = entry.get("path")
                if not isinstance(raw_p, str) or not raw_p.strip():
                    break
                cand_path = _expand_folder_path(raw_p.strip())
                _, opt_problems = _extract_options(entry)
                if cand_path == target_path and not opt_problems:
                    return WriteResult("ok", None)
                break
        return WriteResult("error", "options-not-applied")

    def create_folder(self, folder_id: str, label: str, path: str | Path) -> WriteResult:
        endpoint, my_id, per_req_timeout, deadline, err = self._begin()
        if err is not None or endpoint is None or my_id is None:
            return err or WriteResult("refused", "not-managed")
        return self._create(
            endpoint,
            my_id,
            folder_id,
            label,
            path,
            peers=(),
            timeout=per_req_timeout,
            deadline=deadline,
        )

    def accept_folder_offer(
        self, folder_id: str, device_id: str, label: str, path: str | Path
    ) -> WriteResult:
        endpoint, my_id, per_req_timeout, deadline, err = self._begin()
        if err is not None or endpoint is None or my_id is None:
            return err or WriteResult("refused", "not-managed")
        if not isinstance(folder_id, str) or not _FOLDER_ID_RE.fullmatch(folder_id):
            return WriteResult("refused", "bad-folder-id")
        if not is_device_id(device_id):
            return WriteResult("refused", "bad-device-id")
        if device_id == my_id:
            return WriteResult("refused", "own-device")

        devices, dev_err = self._get_list(
            endpoint, "/rest/config/devices", timeout=per_req_timeout, deadline=deadline
        )
        if dev_err is not None or devices is None:
            return dev_err or WriteResult("error", "bad-response")
        if not any(d.get("deviceID") == device_id for d in devices):
            return WriteResult("refused", "device-not-configured")

        query = urllib.parse.urlencode({"device": device_id})
        out, pending, cause = _get_json(
            endpoint.norm_addr,
            f"/rest/cluster/pending/folders?{query}",
            header_val=endpoint.header_val,
            tls_context=endpoint.tls_context,
            timeout=per_req_timeout,
            deadline=deadline,
        )
        if out in ("unreachable", "unauthorized", "error"):
            return WriteResult(out, cause)
        if out != "ok" or not isinstance(pending, dict):
            return WriteResult("error", cause or "bad-response")

        folder_entry = pending.get(folder_id)
        offered_by = folder_entry.get("offeredBy") if isinstance(folder_entry, dict) else None
        peer_offer = offered_by.get(device_id) if isinstance(offered_by, dict) else None
        if not isinstance(peer_offer, dict):
            return WriteResult("refused", "no-offer")
        if bool(peer_offer.get("receiveEncrypted")) or bool(peer_offer.get("remoteEncrypted")):
            return WriteResult("refused", "encrypted-offer")

        return self._create(
            endpoint,
            my_id,
            folder_id,
            label,
            path,
            peers=(device_id,),
            timeout=per_req_timeout,
            deadline=deadline,
        )

    def add_device(self, device_id: str, name: str = "") -> WriteResult:
        endpoint, my_id, per_req_timeout, deadline, err = self._begin()
        if err is not None or endpoint is None or my_id is None:
            return err or WriteResult("refused", "not-managed")
        if not is_device_id(device_id):
            return WriteResult("refused", "bad-device-id")
        if device_id == my_id:
            return WriteResult("refused", "own-device")

        devices, dev_err = self._get_list(
            endpoint, "/rest/config/devices", timeout=per_req_timeout, deadline=deadline
        )
        if dev_err is not None or devices is None:
            return dev_err or WriteResult("error", "bad-response")
        if any(d.get("deviceID") == device_id for d in devices):
            return WriteResult("unchanged", None)

        post_body = {
            "deviceID": device_id,
            "name": name,
            "addresses": ["dynamic"],
            "introducer": False,
            "autoAcceptFolders": False,
            "paused": False,
        }
        w_out, w_cause = _send_json(
            endpoint,
            "POST",
            "/rest/config/devices",
            post_body,
            timeout=per_req_timeout,
            deadline=deadline,
        )
        return WriteResult(w_out, w_cause)

    def share_folder(self, folder_id: str, device_id: str) -> WriteResult:
        endpoint, my_id, per_req_timeout, deadline, err = self._begin()
        if err is not None or endpoint is None or my_id is None:
            return err or WriteResult("refused", "not-managed")
        if not isinstance(folder_id, str) or not _FOLDER_ID_RE.fullmatch(folder_id):
            return WriteResult("refused", "bad-folder-id")
        if not is_device_id(device_id):
            return WriteResult("refused", "bad-device-id")
        if device_id == my_id:
            return WriteResult("refused", "own-device")

        folders, fld_err = self._get_list(
            endpoint, "/rest/config/folders", timeout=per_req_timeout, deadline=deadline
        )
        if fld_err is not None or folders is None:
            return fld_err or WriteResult("error", "bad-response")
        target_folder = next((f for f in folders if f.get("id") == folder_id), None)
        if target_folder is None:
            return WriteResult("refused", "no-folder")
        _, opt_problems = _extract_options(target_folder)
        if opt_problems:
            return WriteResult("refused", "not-mm-folder")

        devices, dev_err = self._get_list(
            endpoint, "/rest/config/devices", timeout=per_req_timeout, deadline=deadline
        )
        if dev_err is not None or devices is None:
            return dev_err or WriteResult("error", "bad-response")
        if not any(d.get("deviceID") == device_id for d in devices):
            return WriteResult("refused", "device-not-configured")

        if device_id in _folder_device_ids(target_folder):
            return WriteResult("unchanged", None)
        existing_entries = target_folder.get("devices")
        if not isinstance(existing_entries, list):
            return WriteResult("error", "bad-response")

        quoted_id = urllib.parse.quote(folder_id, safe="")
        patch_body = {"devices": [*existing_entries, {"deviceID": device_id}]}
        w_out, w_cause = _send_json(
            endpoint,
            "PATCH",
            f"/rest/config/folders/{quoted_id}",
            patch_body,
            timeout=per_req_timeout,
            deadline=deadline,
        )
        return WriteResult(w_out, w_cause)

    def pending_devices(self) -> PendingDevices:
        endpoint, my_id, per_req_timeout, deadline, err = self._begin()
        if err is not None or endpoint is None or my_id is None:
            res = err or WriteResult("refused", "not-managed")
            return PendingDevices(res.outcome, res.cause, ())

        out, data, cause = _get_json(
            endpoint.norm_addr,
            "/rest/cluster/pending/devices",
            header_val=endpoint.header_val,
            tls_context=endpoint.tls_context,
            timeout=per_req_timeout,
            deadline=deadline,
        )
        if out in ("unreachable", "unauthorized", "error"):
            return PendingDevices(out, cause, ())
        if out != "ok" or not isinstance(data, dict):
            return PendingDevices("error", cause or "bad-response", ())

        items: list[PendingDevice] = []
        for dev_id, info in data.items():
            if not is_device_id(dev_id) or not isinstance(info, dict):
                continue
            raw_name = info.get("name")
            raw_addr = info.get("address")
            raw_time = info.get("time")
            items.append(
                PendingDevice(
                    device_id=dev_id,
                    name=raw_name if isinstance(raw_name, str) else "",
                    address=raw_addr if isinstance(raw_addr, str) else "",
                    time=raw_time if isinstance(raw_time, str) else "",
                )
            )
        items.sort(key=lambda d: d.device_id)
        return PendingDevices("ok", None, tuple(items))

    def mark_introducer(self, device_id: str) -> WriteResult:
        endpoint, my_id, per_req_timeout, deadline, err = self._begin()
        if err is not None or endpoint is None or my_id is None:
            return err or WriteResult("refused", "not-managed")
        if not is_device_id(device_id):
            return WriteResult("refused", "bad-device-id")
        if device_id == my_id:
            return WriteResult("refused", "own-device")

        devices, dev_err = self._get_list(
            endpoint, "/rest/config/devices", timeout=per_req_timeout, deadline=deadline
        )
        if dev_err is not None or devices is None:
            return dev_err or WriteResult("error", "bad-response")
        target_dev = next((d for d in devices if d.get("deviceID") == device_id), None)
        if target_dev is None:
            return WriteResult("refused", "device-not-configured")
        if target_dev.get("introducer") is True:
            return WriteResult("unchanged", None)

        w_out, w_cause = _send_json(
            endpoint,
            "PATCH",
            f"/rest/config/devices/{device_id}",
            {"introducer": True},
            timeout=per_req_timeout,
            deadline=deadline,
        )
        return WriteResult(w_out, w_cause)

    def remove_default_folder(self) -> WriteResult:
        endpoint, my_id, per_req_timeout, deadline, err = self._begin()
        if err is not None or endpoint is None or my_id is None:
            return err or WriteResult("refused", "not-managed")

        folders, fld_err = self._get_list(
            endpoint, "/rest/config/folders", timeout=per_req_timeout, deadline=deadline
        )
        if fld_err is not None or folders is None:
            return fld_err or WriteResult("error", "bad-response")
        default_entry = next((f for f in folders if f.get("id") == STOCK_DEFAULT_FOLDER_ID), None)
        if default_entry is None:
            return WriteResult("unchanged", None)

        raw_path = default_entry.get("path")
        stock_resolved = stock_default_folder_path().resolve(strict=False)
        if (
            not isinstance(raw_path, str)
            or not raw_path.strip()
            or _expand_folder_path(raw_path.strip()) != stock_resolved
            or _stock_default_problem(default_entry, my_id) is not None
        ):
            return WriteResult("refused", "default-folder-in-use")

        w_out, w_cause = _send_json(
            endpoint,
            "DELETE",
            "/rest/config/folders/default",
            None,
            timeout=per_req_timeout,
            deadline=deadline,
        )
        return WriteResult(w_out, w_cause)
