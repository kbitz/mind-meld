"""Unit tests for ``syncthing.py``: discovery, read-only REST client and folder lint."""

from __future__ import annotations

import ast
import base64
import dataclasses
import hashlib
import http.server
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from mind_meld import initflow, safety, syncthing
from tests.syncthing_folder import FakeSyncthing, make_syncthing_like_certificate

_ORIGINAL_CONFIG_DIRS = syncthing.config_dirs


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.mark.parametrize("part", ["headers", "body", "chunked-body"])
def test_slow_progress_cannot_extend_the_response_deadline(part):
    stopped = threading.Event()

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = False

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"padding":"abcdefghijklmnopqrstuv"}'
            header = b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
            if part == "chunked-body":
                header += b"Transfer-Encoding: chunked\r\n\r\n"
                pieces = [b"1\r\n" + bytes([char]) + b"\r\n" for char in body]
                tail = b"0\r\n\r\n"
            else:
                header += f"Content-Length: {len(body)}\r\n".encode()
                if part == "headers":
                    header += b"X-Slow: "
                    pieces = [b"x"] * 36
                    tail = b"\r\n\r\n" + body
                else:
                    header += b"\r\n"
                    pieces = [bytes([char]) for char in body]
                    tail = b""
            try:
                self.wfile.write(header)
                self.wfile.flush()
                for piece in pieces:
                    if stopped.wait(0.025):
                        return
                    self.wfile.write(piece)
                    self.wfile.flush()
                self.wfile.write(tail)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = Server(("127.0.0.1", 0), Handler)
    runner = threading.Thread(target=server.serve_forever)
    runner.start()
    try:
        started = time.monotonic()
        outcome, _, cause = syncthing._get_json(
            f"127.0.0.1:{server.server_port}",
            "/rest/system/status",
            header_val="synthetic",
            tls_context=None,
            timeout=0.1,
            deadline=started + 0.2,
        )
        elapsed = time.monotonic() - started
        assert (outcome, cause) == ("unreachable", "timeout")
        assert elapsed < 0.6
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        runner.join()


def test_discovery_checks_each_platform_directory_in_order(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(syncthing, "config_dirs", _ORIGINAL_CONFIG_DIRS)

    monkeypatch.setattr(sys, "platform", "darwin")
    assert syncthing.config_dirs() == [home / "Library" / "Application Support" / "Syncthing"]

    monkeypatch.setattr(sys, "platform", "linux")
    xdg_cfg = tmp_path / "xdg_cfg"
    xdg_state = tmp_path / "xdg_state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    dirs = syncthing.config_dirs()
    assert dirs == [
        xdg_cfg / "syncthing",
        home / ".config" / "syncthing",
        xdg_state / "syncthing",
        home / ".local" / "state" / "syncthing",
    ]

    # Put a config in the 3rd directory and a later one in the 4th; the 3rd wins.
    for idx in (3, 2):
        dirs[idx].mkdir(parents=True, exist_ok=True)
        (dirs[idx] / "config.xml").write_text(
            f'<configuration><gui tls="false"><address>unix:///sock{idx}</address>'
            "<apikey>placeholder</apikey></gui></configuration>",
            encoding="utf-8",
        )
    state = syncthing.read_state(tmp_path / "storage", stignore_line=initflow.STIGNORE_LINE)
    assert state.config_dir == dirs[2]
    assert state.gui_address == "unix:///sock2"


def test_missing_config_is_reported_not_raised(tmp_path):
    state = syncthing.read_state(tmp_path / "storage", stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "not-found"
    assert state.cause is None
    assert state.config_dir is None
    assert state.problems == ()
    assert state.stignore is None


def test_unreadable_config_or_unix_address_is_reported(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "st_cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(syncthing, "config_dirs", lambda: [cfg_dir])
    cfg_path = cfg_dir / "config.xml"

    # Malformed XML
    cfg_path.write_text("<not-xml", encoding="utf-8")
    state = syncthing.read_state(tmp_path / "storage", stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "config-unreadable"
    assert state.config_dir == cfg_dir

    # Missing credential element
    cfg_path.write_text(
        "<configuration><gui><address>127.0.0.1:8384</address></gui></configuration>",
        encoding="utf-8",
    )
    state = syncthing.read_state(tmp_path / "storage", stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "config-unreadable"

    # Symlinked config.xml refused by lstat regular-file check
    target = tmp_path / "other.xml"
    target.write_text(
        "<configuration><gui><address>127.0.0.1:8384</address>"
        "<apikey>placeholder</apikey></gui></configuration>",
        encoding="utf-8",
    )
    cfg_path.unlink()
    cfg_path.symlink_to(target)
    state = syncthing.read_state(tmp_path / "storage", stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "config-unreadable"
    cfg_path.unlink()

    # Unix domain socket address
    cfg_path.write_text(
        "<configuration><gui><address>unix:///run/syncthing.sock</address>"
        "<apikey>placeholder</apikey></gui></configuration>",
        encoding="utf-8",
    )
    state = syncthing.read_state(tmp_path / "storage", stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "address-unsupported"
    assert state.gui_address == "unix:///run/syncthing.sock"


def test_reads_peers_folder_state_and_options_with_get_only(fake_syncthing):
    fake_syncthing.set_folder(
        fake_syncthing.storage_path,
        peers=[
            (fake_syncthing.peer_id, "mac-b", True),
            (fake_syncthing.peer2_id, "linux-a", False),
        ],
        state="syncing",
        need_files=3,
        need_bytes=4096,
        pull_errors=0,
    )
    before_bytes = fake_syncthing.config_path.read_bytes()
    os.utime(fake_syncthing.config_path, (1_700_000_000, 1_700_000_000))
    before_mtime = fake_syncthing.config_path.stat().st_mtime_ns

    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "ok"
    assert state.cause is None
    assert state.folder_id == "mind-meld"
    assert state.folder_label == "Mind Meld"
    assert state.folder_path == fake_syncthing.storage_path.resolve()
    assert state.folder_state == "syncing"
    assert state.files_to_fetch == 3
    assert state.bytes_to_fetch == 4096
    assert state.pull_errors == 0
    assert state.connected_peers == 1
    assert state.total_peers == 2
    assert state.disconnected_peers == ("linux-a",)
    assert state.options == {
        "type": "sendreceive",
        "versioning": "",
        "maxConflicts": 10,
        "ignorePerms": False,
        "order": "alphabetic",
    }
    assert state.problems == ()
    assert state.stignore == "ok"

    assert [req[0] for req in fake_syncthing.requests] == ["GET"] * 5
    assert [req[1] for req in fake_syncthing.requests] == [
        "/rest/system/status",
        "/rest/system/connections",
        "/rest/config/folders",
        "/rest/config/devices",
        "/rest/db/status?folder=mind-meld",
    ]
    for _, _, headers in fake_syncthing.requests:
        header_val = headers.get("X-API-Key") or headers.get("x-api-key")
        assert header_val == fake_syncthing.gui_value

    assert fake_syncthing.config_path.read_bytes() == before_bytes
    assert fake_syncthing.config_path.stat().st_mtime_ns == before_mtime


def test_refused_and_held_requests_are_unreachable_within_the_budget(fake_syncthing):
    # Held request times out within budget and stops at the first request
    fake_syncthing.hold("/rest/system/status")
    t0 = time.monotonic()
    state = syncthing.read_state(
        fake_syncthing.storage_path,
        stignore_line=initflow.STIGNORE_LINE,
        timeout=0.1,
        budget=0.25,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0
    assert state.state == "unreachable"
    assert state.cause == "timeout"
    assert len(fake_syncthing.requests) == 1

    # Refused connection
    fake_syncthing.close()
    t0 = time.monotonic()
    refused = syncthing.read_state(
        fake_syncthing.storage_path,
        stignore_line=initflow.STIGNORE_LINE,
        timeout=0.1,
        budget=0.25,
    )
    assert time.monotonic() - t0 < 1.0
    assert refused.state == "unreachable"
    assert refused.cause == "refused"


def test_rejected_credential_is_unauthorized(fake_syncthing):
    fake_syncthing.write_config(gui_value="wrong-credential")
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "unauthorized"
    assert state.cause == "http-403"
    assert len(fake_syncthing.requests) == 1


def test_a_missing_endpoint_leaves_only_its_fields_unknown(fake_syncthing):
    fake_syncthing.responses["/rest/db/status?folder=mind-meld"] = 404
    fake_syncthing.responses["/rest/db/status"] = 404
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "ok"
    assert state.folder_id == "mind-meld"
    assert state.connected_peers == 2
    assert state.total_peers == 2
    assert state.folder_state is None
    assert state.files_to_fetch is None
    assert state.bytes_to_fetch is None

    # Unparseable connections body leaves connected_peers None while folder state works
    fake_syncthing.set_folder(fake_syncthing.storage_path, state="idle", need_files=0)
    fake_syncthing.responses["/rest/system/connections"] = b"{not-json"
    state2 = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state2.state == "ok"
    assert state2.total_peers == 2
    assert state2.connected_peers is None
    assert state2.folder_state == "idle"


@pytest.mark.parametrize(
    "kwargs,expected_problems",
    [
        ({}, ()),
        ({"max_conflicts": -1}, ()),
        ({"folder_type": "sendonly"}, ("type",)),
        ({"versioning_type": "trashcan"}, ("versioning",)),
        ({"max_conflicts": 0}, ("maxConflicts",)),
        ({"ignore_perms": True}, ("ignorePerms",)),
        ({"order": "random"}, ("order",)),
        ({"paused": True}, ("paused",)),
    ],
)
def test_each_folder_option_is_linted(fake_syncthing, kwargs, expected_problems):
    fake_syncthing.set_folder(fake_syncthing.storage_path, **kwargs)
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "ok"
    assert state.problems == expected_problems


def test_stignore_lint_states(fake_syncthing):
    st_file = fake_syncthing.storage_path / ".stignore"

    # Present and matching
    st_file.write_text("# comment\n  (?d)tmp*.tmp  \n", encoding="utf-8")
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.stignore == "ok"
    assert state.problems == ()

    # Present without the required line
    st_file.write_text("*.bak\n", encoding="utf-8")
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.stignore == "missing-line"
    assert state.problems == ("stignore-line",)

    # Missing .stignore
    st_file.unlink()
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.stignore == "missing"
    assert state.problems == ("stignore-missing",)

    # Unreadable (directory or symlink instead of regular file)
    st_file.mkdir()
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.stignore == "unreadable"
    assert state.problems == ("stignore-unreadable",)


def test_folder_matching_root_parent_and_none(fake_syncthing, tmp_path):
    parent = tmp_path / "sync_root"
    nested = parent / "sub" / "mind-meld"
    nested.mkdir(parents=True)

    # Storage nested inside a shared parent folder matches the parent and lints parent/.stignore
    fake_syncthing.set_folder(parent, folder_id="shared-parent", write_stignore=True)
    state = syncthing.read_state(nested, stignore_line=initflow.STIGNORE_LINE)
    assert state.folder_id == "shared-parent"
    assert state.folder_path == parent.resolve()
    assert state.stignore == "ok"
    assert state.problems == ()

    # Deepest containing folder wins when multiple contain storage_path
    deeper = parent / "sub"
    (deeper / ".stignore").write_text("(?d)tmp*.tmp\n", encoding="utf-8")
    fake_syncthing.responses["/rest/config/folders"] = [
        {
            "id": "outer",
            "label": "Outer",
            "path": str(parent),
            "type": "sendreceive",
            "paused": False,
            "ignorePerms": False,
            "order": "alphabetic",
            "maxConflicts": 10,
            "versioning": {"type": ""},
            "devices": [{"deviceID": fake_syncthing.my_id}],
        },
        {
            "id": "deeper",
            "label": "Deeper",
            "path": str(deeper),
            "type": "sendreceive",
            "paused": False,
            "ignorePerms": False,
            "order": "alphabetic",
            "maxConflicts": 10,
            "versioning": {"type": ""},
            "devices": [{"deviceID": fake_syncthing.my_id}],
        },
    ]
    fake_syncthing.responses["/rest/db/status?folder=deeper"] = {
        "state": "idle",
        "needFiles": 0,
        "needBytes": 0,
        "pullErrors": 0,
    }
    state2 = syncthing.read_state(nested, stignore_line=initflow.STIGNORE_LINE)
    assert state2.folder_id == "deeper"
    assert state2.folder_path == deeper.resolve()

    # Unrelated folder -> problem "no-folder"
    other = tmp_path / "unrelated"
    other.mkdir()
    (other / ".stignore").write_text("(?d)tmp*.tmp\n", encoding="utf-8")
    state3 = syncthing.read_state(other, stignore_line=initflow.STIGNORE_LINE)
    assert state3.folder_id is None
    assert "no-folder" in state3.problems


def test_proxy_environment_is_ignored(fake_syncthing, monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:1")
    # Also verify 0.0.0.0 in config.xml normalizes to 127.0.0.1 loopback
    fake_syncthing.write_config(address=f"0.0.0.0:{fake_syncthing.port}")
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    assert state.state == "ok"
    assert state.gui_address == f"127.0.0.1:{fake_syncthing.port}"


def test_every_request_is_a_get():
    src_path = Path(syncthing.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    top_defs: dict[str, ast.AST] = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }

    request_sites: list[tuple[str, ast.Call]] = []
    for fn_name, fn_node in top_defs.items():
        for node in ast.walk(fn_node):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "request"
            ):
                request_sites.append((fn_name, node))

    assert [name for name, _ in request_sites] == ["_get_json", "_send_json"]
    get_call = request_sites[0][1]
    assert isinstance(get_call.args[0], ast.Constant) and get_call.args[0].value == "GET"
    assert "body" not in {kw.arg: kw.value for kw in get_call.keywords}

    send_fn = top_defs["_send_json"]
    assert isinstance(send_fn, ast.FunctionDef)
    first_stmt = send_fn.body[0]
    if (
        isinstance(first_stmt, ast.Expr)
        and isinstance(first_stmt.value, ast.Constant)
        and isinstance(first_stmt.value.value, str)
    ):
        first_stmt = send_fn.body[1]
    assert isinstance(first_stmt, ast.If)
    guard_calls = [
        n.func.id
        for n in ast.walk(first_stmt.test)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "_write_route_ok" in guard_calls
    send_call = request_sites[1][1]
    assert isinstance(send_call.args[0], ast.Name) and send_call.args[0].id == "method"

    def call_closure(roots: tuple[str, ...]) -> set[str]:
        seen: set[str] = set()
        queue = list(roots)
        while queue:
            cur = queue.pop()
            if cur in seen or cur not in top_defs:
                continue
            seen.add(cur)
            for sub in ast.walk(top_defs[cur]):
                if isinstance(sub, ast.Call):
                    if isinstance(sub.func, ast.Name):
                        callee = sub.func.id
                    elif isinstance(sub.func, ast.Attribute):
                        callee = sub.func.attr
                    else:
                        continue
                    if callee in top_defs and callee not in seen:
                        queue.append(callee)
        return seen

    ro_closure = call_closure(("read_state", "_read_state_inner", "classify_config"))
    assert "_send_json" not in ro_closure
    assert "SyncthingWriter" not in ro_closure


def test_credential_never_reaches_the_state(fake_syncthing):
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    cred = fake_syncthing.gui_value
    assert cred not in repr(state)
    for f in dataclasses.fields(state):
        assert cred not in repr(getattr(state, f.name))
    for val in vars(state).values():
        assert cred not in repr(val)


# ── Track 117A T1: default-configuration classifier ──────────────────────


def test_classify_config_rest_fresh_default_and_managed_record(fake_syncthing, isolated_home):
    fake_syncthing.set_config(devices=(), folders=())
    check = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check.state == "default"
    assert check.source == "rest"
    assert check.reach == "ok"
    assert check.cause is None
    assert check.reasons == ()
    assert check.local_device_id == fake_syncthing.my_id
    rec = check.managed_record()
    assert rec == syncthing.ManagedRecord(device_id=fake_syncthing.my_id)
    assert [(r[0], r[1]) for r in fake_syncthing.requests] == [
        ("GET", "/rest/system/status"),
        ("GET", "/rest/config/devices"),
        ("GET", "/rest/config/folders"),
    ]


@pytest.mark.parametrize(
    "shape",
    ["absent", "stfolder_dir", "stfolder_empty_file", "stfolder_and_ds_store"],
)
def test_classify_config_stock_default_folder_empty_shapes(fake_syncthing, isolated_home, shape):
    stock_dir = isolated_home / "Sync"
    if shape == "stfolder_dir":
        marker_dir = stock_dir / ".stfolder"
        marker_dir.mkdir(parents=True)
        (marker_dir / "syncthing-folder-abc123.txt").write_text("marker\n", encoding="utf-8")
    elif shape == "stfolder_empty_file":
        stock_dir.mkdir(parents=True)
        (stock_dir / ".stfolder").write_bytes(b"")
    elif shape == "stfolder_and_ds_store":
        marker_dir = stock_dir / ".stfolder"
        marker_dir.mkdir(parents=True)
        (marker_dir / "syncthing-folder-abc123.txt").write_text("marker\n", encoding="utf-8")
        (stock_dir / ".DS_Store").write_bytes(b"\x00\x01")

    fake_syncthing.set_config(
        devices=(),
        folders=[fake_syncthing.stock_folder(stock_dir)],
    )
    check = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check.state == "default"
    assert check.source == "rest"
    assert check.reasons == ()


def test_classify_config_ignores_gui_and_options_changes(fake_syncthing, isolated_home):
    fake_syncthing.set_config(devices=(), folders=[])
    fake_syncthing.responses["/rest/config/gui"] = {"theme": "dark", "user": "owner"}
    fake_syncthing.responses["/rest/config/options"] = {"relaysEnabled": False}
    fake_syncthing.config_path.write_text(
        '<configuration version="37">\n'
        f'  <gui enabled="true" tls="false" theme="dark">\n'
        f"    <address>{fake_syncthing.address}</address>\n"
        f"    <apikey>{fake_syncthing.gui_value}</apikey>\n"
        "    <user>owner</user>\n"
        "  </gui>\n"
        "  <options><relaysEnabled>false</relaysEnabled></options>\n"
        f'  <device id="{fake_syncthing.my_id}" name="mac-a"></device>\n'
        "</configuration>\n",
        encoding="utf-8",
    )
    check = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check.state == "default"
    assert check.reasons == ()
    requested_paths = [r[1] for r in fake_syncthing.requests]
    assert "/rest/config" not in requested_paths
    assert "/rest/config/gui" not in requested_paths
    assert "/rest/config/options" not in requested_paths


@pytest.mark.parametrize(
    "kind",
    [
        "named_device",
        "unnamed_device",
        "mm_folder",
        "default_non_stock_path",
        "stock_shared_configured",
        "stock_shared_unconfigured",
        "stock_notes_txt",
        "stock_subdir",
        "stock_stignore",
        "stock_stversions",
        "stock_symlink_stfolder",
        "stock_extra_in_stfolder",
    ],
)
def test_classify_config_rest_pre_existing_shapes(fake_syncthing, isolated_home, tmp_path, kind):
    stock_dir = isolated_home / "Sync"
    devices: list[dict] = []
    folders: list[dict] = []

    if kind == "named_device":
        devices = [{"deviceID": fake_syncthing.peer_id, "name": "mac-b"}]
        expected = (syncthing.PreExisting("device", "mac-b", "paired"),)
    elif kind == "unnamed_device":
        devices = [{"deviceID": fake_syncthing.peer_id, "name": ""}]
        expected = (syncthing.PreExisting("device", "", "paired"),)
    elif kind == "mm_folder":
        folders = [{"id": "mind-meld", "label": "Mind Meld", "path": str(stock_dir / "mind-meld")}]
        expected = (syncthing.PreExisting("folder", "Mind Meld", "folder"),)
    elif kind == "default_non_stock_path":
        folders = [fake_syncthing.stock_folder(tmp_path / "other-default")]
        expected = (syncthing.PreExisting("folder", "Default Folder", "folder"),)
    elif kind == "stock_shared_configured":
        devices = [{"deviceID": fake_syncthing.peer_id, "name": "mac-b"}]
        folders = [fake_syncthing.stock_folder(stock_dir, shared_with=[fake_syncthing.peer_id])]
        expected = (
            syncthing.PreExisting("device", "mac-b", "paired"),
            syncthing.PreExisting("folder", "Default Folder", "shared"),
        )
    elif kind == "stock_shared_unconfigured":
        folders = [fake_syncthing.stock_folder(stock_dir, shared_with=[fake_syncthing.peer_id])]
        expected = (syncthing.PreExisting("folder", "Default Folder", "shared"),)
    else:
        stock_dir.mkdir(parents=True, exist_ok=True)
        if kind == "stock_notes_txt":
            (stock_dir / "notes.txt").write_text("hi\n", encoding="utf-8")
        elif kind == "stock_subdir":
            (stock_dir / "mind-meld").mkdir()
        elif kind == "stock_stignore":
            (stock_dir / ".stignore").write_text("(?d)tmp*.tmp\n", encoding="utf-8")
        elif kind == "stock_stversions":
            (stock_dir / ".stversions").mkdir()
        elif kind == "stock_symlink_stfolder":
            real_marker = tmp_path / "real-marker"
            real_marker.mkdir()
            (stock_dir / ".stfolder").symlink_to(real_marker)
        elif kind == "stock_extra_in_stfolder":
            marker_dir = stock_dir / ".stfolder"
            marker_dir.mkdir()
            (marker_dir / "extra.txt").write_text("x\n", encoding="utf-8")
        folders = [fake_syncthing.stock_folder(stock_dir)]
        expected = (syncthing.PreExisting("folder", "Default Folder", "not-empty"),)

    fake_syncthing.set_config(devices=devices, folders=folders)
    check = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check.state == "pre-existing"
    assert check.source == "rest"
    assert check.reasons == expected
    with pytest.raises(ValueError):
        check.managed_record()


def test_classify_config_sanitizes_every_name_on_rest_and_offline(fake_syncthing, isolated_home):
    seen_rest: list[str] = []

    def rec_rest(val: str) -> str:
        seen_rest.append(val)
        return safety.safe_terminal_str(val)

    raw_esc = "peer\x1b]0;x\x07name"
    fake_syncthing.set_config(
        devices=[{"deviceID": fake_syncthing.peer_id, "name": raw_esc}],
        folders=[{"id": "custom", "label": "lbl\x1b[31m", "path": str(isolated_home / "custom")}],
    )
    rest_check = syncthing.classify_config(sanitize=rec_rest)
    assert seen_rest == [raw_esc, "lbl\x1b[31m"]
    assert rest_check.reasons == (
        syncthing.PreExisting("device", safety.safe_terminal_str(raw_esc), "paired"),
        syncthing.PreExisting("folder", safety.safe_terminal_str("lbl\x1b[31m"), "folder"),
    )
    assert "\x1b" not in repr(rest_check)

    # Offline with C1 control character U+009B
    raw_c1 = "peer\u009bname"
    cert_path, _ = make_syncthing_like_certificate(fake_syncthing.config_dir)
    derived_id = syncthing._device_id_from_cert(fake_syncthing.config_dir)
    assert derived_id is not None
    fake_syncthing.my_id = derived_id
    fake_syncthing.set_shape(
        devices=[{"deviceID": fake_syncthing.peer_id, "name": raw_c1}],
        folders=[{"id": "custom", "label": "lbl\u009b", "path": str(isolated_home / "custom")}],
    )
    fake_syncthing.close()

    seen_off: list[str] = []

    def rec_off(val: str) -> str:
        seen_off.append(val)
        return safety.safe_terminal_str(val)

    off_check = syncthing.classify_config(sanitize=rec_off)
    assert off_check.source == "config"
    assert seen_off == [raw_c1, "lbl\u009b"]
    assert off_check.reasons == (
        syncthing.PreExisting("device", safety.safe_terminal_str(raw_c1), "paired"),
        syncthing.PreExisting("folder", safety.safe_terminal_str("lbl\u009b"), "folder"),
    )
    assert "\u009b" not in repr(off_check)


def test_classify_config_offline_stopped_variants(fake_syncthing, isolated_home):
    stock_dir = isolated_home / "Sync"
    # 1. Fresh offline config
    fake_syncthing.set_shape(devices=(), folders=())
    fake_syncthing.close()
    check = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check.state == "default"
    assert check.source == "config"
    assert check.reach == "unreachable"
    assert check.cause == "refused"
    assert check.local_device_id is None
    assert check.reasons == ()
    with pytest.raises(ValueError):
        check.managed_record()

    # 2. Stock unshared empty Default Folder + <defaults> template block
    fake_syncthing.set_shape(
        devices=(),
        folders=[fake_syncthing.stock_folder(stock_dir)],
        defaults=True,
    )
    check2 = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check2.state == "default"
    assert check2.source == "config"
    assert check2.reasons == ()

    # 3. Second device without cert.pem -> pre-existing with both devices reported
    fake_syncthing.set_shape(
        devices=[{"deviceID": fake_syncthing.peer_id, "name": "linux-b"}],
        folders=(),
    )
    check3 = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check3.state == "pre-existing"
    assert check3.source == "config"
    assert check3.reasons == (
        syncthing.PreExisting("device", "mac-a", "paired"),
        syncthing.PreExisting("device", "linux-b", "paired"),
    )

    # 4. unix:/// address and missing https-cert.pem with tls="true" also read offline
    fake_syncthing.write_config(address="unix:///run/st.sock", devices=(), folders=())
    check_unix = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check_unix.state == "default"
    assert check_unix.source == "config"
    assert check_unix.reach == "address-unsupported"

    fake_syncthing.write_config(tls=True, devices=(), folders=())
    check_tls = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check_tls.state == "default"
    assert check_tls.source == "config"
    assert check_tls.reach == "unreachable"
    assert check_tls.cause == "tls"


@pytest.mark.parametrize(
    "shape",
    [
        "default_no_folders",
        "default_stock_empty",
        "default_stock_marker_file",
        "default_stock_marker_dir",
        "default_stock_marker_and_ds_store",
        "pre_second_device",
        "pre_unnamed_device",
        "pre_extra_folder",
        "pre_default_other_path",
        "pre_stock_shared",
        "pre_stock_shared_unconfigured",
        "pre_stock_non_empty",
        "pre_stock_subdir",
        "pre_stock_stignore",
        "pre_stock_stversions",
        "pre_stock_symlink_stfolder",
        "pre_stock_extra_in_stfolder",
    ],
)
def test_rest_and_offline_classification_agree(fake_syncthing, isolated_home, shape):
    stock_dir = isolated_home / "Sync"
    make_syncthing_like_certificate(fake_syncthing.config_dir)
    derived_id = syncthing._device_id_from_cert(fake_syncthing.config_dir)
    assert derived_id is not None
    fake_syncthing.my_id = derived_id

    if shape == "default_no_folders":
        fake_syncthing.set_shape(devices=(), folders=())
    elif shape.startswith("default_stock"):
        if shape == "default_stock_marker_file":
            stock_dir.mkdir(parents=True)
            (stock_dir / ".stfolder").touch()
        elif shape in ("default_stock_marker_dir", "default_stock_marker_and_ds_store"):
            marker = stock_dir / ".stfolder"
            marker.mkdir(parents=True)
            (marker / "syncthing-folder-abc123.txt").write_text("marker", encoding="utf-8")
            if shape == "default_stock_marker_and_ds_store":
                (stock_dir / ".DS_Store").touch()
        fake_syncthing.set_shape(devices=(), folders=[fake_syncthing.stock_folder(stock_dir)])
    elif shape in ("pre_second_device", "pre_unnamed_device"):
        fake_syncthing.set_shape(
            devices=[
                {
                    "deviceID": fake_syncthing.peer_id,
                    "name": "linux-b" if shape == "pre_second_device" else "",
                }
            ],
            folders=(),
        )
    elif shape == "pre_extra_folder":
        fake_syncthing.set_shape(
            devices=(),
            folders=[{"id": "notes", "label": "Notes", "path": str(isolated_home / "notes")}],
        )
    elif shape == "pre_default_other_path":
        fake_syncthing.set_shape(
            folders=[fake_syncthing.stock_folder(isolated_home / "other-default")]
        )
    elif shape in ("pre_stock_shared", "pre_stock_shared_unconfigured"):
        fake_syncthing.set_shape(
            devices=[{"deviceID": fake_syncthing.peer_id, "name": "linux-b"}]
            if shape == "pre_stock_shared"
            else [],
            folders=[fake_syncthing.stock_folder(stock_dir, shared_with=[fake_syncthing.peer_id])],
        )
    else:
        stock_dir.mkdir(parents=True, exist_ok=True)
        if shape == "pre_stock_non_empty":
            (stock_dir / "data.txt").write_text("content\n", encoding="utf-8")
        elif shape == "pre_stock_subdir":
            (stock_dir / "mind-meld").mkdir()
        elif shape == "pre_stock_stignore":
            (stock_dir / ".stignore").touch()
        elif shape == "pre_stock_stversions":
            (stock_dir / ".stversions").mkdir()
        elif shape == "pre_stock_symlink_stfolder":
            real_marker = isolated_home / "real-marker"
            real_marker.mkdir()
            (stock_dir / ".stfolder").symlink_to(real_marker)
        elif shape == "pre_stock_extra_in_stfolder":
            (stock_dir / ".stfolder").mkdir()
            (stock_dir / ".stfolder" / "extra.txt").touch()
        else:
            raise AssertionError(shape)
        fake_syncthing.set_shape(devices=(), folders=[fake_syncthing.stock_folder(stock_dir)])

    rest_res = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert rest_res.source == "rest"

    fake_syncthing.close()
    off_res = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert off_res.source == "config"
    assert (rest_res.state, rest_res.reasons) == (off_res.state, off_res.reasons)


@pytest.mark.parametrize(
    "case",
    [
        "malformed_xml",
        "symlink",
        "oversized",
        "non_utf8",
        "missing_gui_cred",
        "device_without_id",
        "folder_without_path",
        "folder_device_without_id",
        "zero_devices",
    ],
)
def test_malformed_or_unreadable_config_xml_is_unknown(tmp_path, monkeypatch, isolated_home, case):
    cfg_dir = tmp_path / "st_cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(syncthing, "config_dirs", lambda: [cfg_dir])
    cfg_path = cfg_dir / "config.xml"

    gui_block = (
        '<gui enabled="true" tls="false">'
        "<address>127.0.0.1:1</address>"
        "<apikey>placeholder</apikey>"
        "</gui>"
    )
    if case == "malformed_xml":
        cfg_path.write_text("<configuration><gui>", encoding="utf-8")
        expected_cause = "config-unreadable"
    elif case == "symlink":
        other = tmp_path / "target.xml"
        other.write_text(f"<configuration>{gui_block}</configuration>", encoding="utf-8")
        cfg_path.symlink_to(other)
        expected_cause = "config-unreadable"
    elif case == "oversized":
        cfg_path.write_bytes(b"a" * (syncthing._MAX_CONFIG_BYTES + 1))
        expected_cause = "config-unreadable"
    elif case == "non_utf8":
        cfg_path.write_bytes(b"\xff\xfe\x00")
        expected_cause = "config-unreadable"
    elif case == "missing_gui_cred":
        cfg_path.write_text(
            "<configuration><gui><address>127.0.0.1:1</address></gui></configuration>",
            encoding="utf-8",
        )
        expected_cause = "config-unreadable"
    elif case == "device_without_id":
        cfg_path.write_text(
            f'<configuration>{gui_block}<device name="mac-a"></device></configuration>',
            encoding="utf-8",
        )
        expected_cause = "bad-config"
    elif case == "folder_without_path":
        cfg_path.write_text(
            f'<configuration>{gui_block}<device id="A" name="mac-a"></device>'
            '<folder id="default" label="Default"></folder></configuration>',
            encoding="utf-8",
        )
        expected_cause = "bad-config"
    elif case == "folder_device_without_id":
        cfg_path.write_text(
            f'<configuration>{gui_block}<device id="A" name="mac-a"></device>'
            '<folder id="default" path="/tmp/x"><device></device></folder></configuration>',
            encoding="utf-8",
        )
        expected_cause = "bad-config"
    elif case == "zero_devices":
        cfg_path.write_text(
            f"<configuration>{gui_block}</configuration>",
            encoding="utf-8",
        )
        expected_cause = "bad-config"

    check = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check.state == "unknown"
    assert check.source is None
    assert check.cause == expected_cause
    with pytest.raises(ValueError):
        check.managed_record()


def test_no_config_vs_identity_files_without_config(tmp_path, monkeypatch, isolated_home):
    cfg_dir = tmp_path / "st_cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(syncthing, "config_dirs", lambda: [cfg_dir])

    check0 = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check0.state == "no-config"
    assert check0.source is None
    assert check0.reach == "not-found"
    with pytest.raises(ValueError):
        check0.managed_record()

    # cert.pem without config.xml -> unknown / config-missing
    cert_file = cfg_dir / "cert.pem"
    cert_file.write_text("dummy\n", encoding="utf-8")
    check1 = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check1.state == "unknown"
    assert check1.reach == "not-found"
    assert check1.cause == "config-missing"
    cert_file.unlink()

    # key.pem with mode 000 is checked via lstat only and never opened
    priv_file = cfg_dir / "key.pem"
    priv_file.write_text("unreadable\n", encoding="utf-8")
    priv_file.chmod(0)
    try:
        check2 = syncthing.classify_config(sanitize=safety.safe_terminal_str)
        assert check2.state == "unknown"
        assert check2.reach == "not-found"
        assert check2.cause == "config-missing"
    finally:
        priv_file.chmod(0o600)


def test_luhn32_and_device_id_from_cert(tmp_path):
    assert syncthing._luhn32("P56IOI7MZJNU2") == "Y"
    assert syncthing._luhn32("IQGDREYDM2MGT") == "I"
    assert syncthing._luhn32("MGL3BXNPQ6W5B") == "M"
    assert syncthing._luhn32("TBBZ4TJXZWICQ") == "2"

    cert_dir = tmp_path / "cert_dir"
    cert_path, _ = make_syncthing_like_certificate(cert_dir)
    got_id = syncthing._device_id_from_cert(cert_dir)
    assert got_id is not None
    assert syncthing.is_device_id(got_id)

    # Independent computation from cryptography's DER bytes
    loaded = x509.load_pem_x509_certificate(cert_path.read_bytes())
    der = loaded.public_bytes(serialization.Encoding.DER)
    b32 = base64.b32encode(hashlib.sha256(der).digest()).decode("ascii").rstrip("=")
    blocks = [b32[i : i + 13] + syncthing._luhn32(b32[i : i + 13]) for i in range(0, 52, 13)]
    full = "".join(blocks)
    expected_id = "-".join(full[i : i + 7] for i in range(0, 56, 7))
    assert got_id == expected_id

    # Garbage or oversized cert.pem returns None
    cert_path.write_text("not a pem certificate\n", encoding="utf-8")
    assert syncthing._device_id_from_cert(cert_dir) is None
    cert_path.write_bytes(b"x" * (syncthing._MAX_CONFIG_BYTES + 1))
    assert syncthing._device_id_from_cert(cert_dir) is None


@pytest.mark.parametrize(
    "err_case,expected_reach,expected_cause",
    [
        ("wrong_cred", "unauthorized", "http-403"),
        ("status_500", "error", "http-500"),
        ("devices_404", "error", "http-404"),
        ("folders_non_json", "error", "bad-response"),
        ("missing_my_id", "error", "bad-response"),
        ("device_not_dict", "error", "bad-response"),
    ],
)
def test_classify_config_rest_errors_return_unknown(
    fake_syncthing, isolated_home, err_case, expected_reach, expected_cause
):
    fake_syncthing.set_shape(devices=(), folders=())
    if err_case == "wrong_cred":
        fake_syncthing.write_config(gui_value="wrong-cred", devices=(), folders=())
    elif err_case == "status_500":
        fake_syncthing.responses["/rest/system/status"] = 500
    elif err_case == "devices_404":
        fake_syncthing.responses["/rest/config/devices"] = 404
    elif err_case == "folders_non_json":
        fake_syncthing.responses["/rest/config/folders"] = b"{bad-json"
    elif err_case == "missing_my_id":
        fake_syncthing.responses["/rest/system/status"] = {}
    elif err_case == "device_not_dict":
        fake_syncthing.responses["/rest/config/devices"] = ["not-a-dict"]

    check = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check.state == "unknown"
    assert check.source is None
    assert check.reach == expected_reach
    assert check.cause == expected_cause
    with pytest.raises(ValueError):
        check.managed_record()


def test_classify_config_rest_unreachable_falls_back_offline_and_hides_credential(
    fake_syncthing, isolated_home
):
    fake_syncthing.set_shape(devices=(), folders=())
    fake_syncthing.hold("/rest/system/status")
    t0 = time.monotonic()
    held_check = syncthing.classify_config(
        sanitize=safety.safe_terminal_str, timeout=0.1, budget=0.25
    )
    assert time.monotonic() - t0 < 1.0
    assert held_check.state == "default"
    assert held_check.source == "config"
    assert held_check.reach == "unreachable"
    assert held_check.cause == "timeout"
    assert fake_syncthing.gui_value not in repr(held_check)
    assert fake_syncthing.my_id not in repr(held_check)

    fake_syncthing.close()
    closed_check = syncthing.classify_config(
        sanitize=safety.safe_terminal_str, timeout=0.1, budget=0.25
    )
    assert closed_check.state == "default"
    assert closed_check.source == "config"
    assert closed_check.reach == "unreachable"
    assert closed_check.cause == "refused"
    assert fake_syncthing.gui_value not in repr(closed_check)
    assert fake_syncthing.my_id not in repr(closed_check)


# ── Track 117A T3: the four recorded Syncthing client test gaps ──────────


def test_tls_acceptance_rejection_missing_and_slow_response(tmp_path, monkeypatch, isolated_home):
    cert_dir = tmp_path / "certs"
    cert_pair = make_syncthing_like_certificate(cert_dir)
    cfg_dir = tmp_path / "st_tls_cfg"
    monkeypatch.setattr(syncthing, "config_dirs", lambda: [cfg_dir])
    srv = FakeSyncthing(cfg_dir, tmp_path / "tls_storage", tls_cert=cert_pair)
    try:
        # 1. Acceptance over TLS
        state = syncthing.read_state(srv.storage_path, stignore_line=initflow.STIGNORE_LINE)
        assert state.state == "ok"
        assert state.folder_id == "mind-meld"
        assert len(srv.requests) == 5

        # classify_config over TLS on a fresh config
        srv.set_config(devices=(), folders=())
        srv.requests.clear()
        chk = syncthing.classify_config(sanitize=safety.safe_terminal_str)
        assert chk.state == "default"
        assert chk.source == "rest"

        # 2. Rejection when https-cert.pem is replaced with a different certificate
        other_cert, _ = make_syncthing_like_certificate(tmp_path / "other_cert")
        (cfg_dir / "https-cert.pem").write_bytes(other_cert.read_bytes())
        srv.requests.clear()
        rejected = syncthing.read_state(srv.storage_path, stignore_line=initflow.STIGNORE_LINE)
        assert rejected.state == "unreachable"
        assert rejected.cause == "tls"
        assert srv.requests == []

        # 3. Missing https-cert.pem gives tls with zero contact
        (cfg_dir / "https-cert.pem").unlink()
        srv.requests.clear()
        missing = syncthing.read_state(srv.storage_path, stignore_line=initflow.STIGNORE_LINE)
        assert missing.state == "unreachable"
        assert missing.cause == "tls"
        assert srv.requests == []

        # 4. Slow response over TLS times out within budget
        (cfg_dir / "https-cert.pem").write_bytes(cert_pair[0].read_bytes())
        srv.hold("/rest/system/status")
        t0 = time.monotonic()
        slow = syncthing.read_state(
            srv.storage_path,
            stignore_line=initflow.STIGNORE_LINE,
            timeout=0.1,
            budget=0.25,
        )
        assert time.monotonic() - t0 < 1.0
        assert slow.state == "unreachable"
        assert slow.cause == "timeout"
    finally:
        srv.close()


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_redirects_are_refused_with_zero_destination_contact(fake_syncthing, code):
    dest_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dest_sock.bind(("127.0.0.1", 0))
    dest_sock.listen(5)
    dest_sock.setblocking(False)
    dest_port = dest_sock.getsockname()[1]
    try:
        fake_syncthing.responses["/rest/system/status"] = (
            code,
            b"",
            {"Location": f"http://127.0.0.1:{dest_port}/rest/system/status"},
        )
        state = syncthing.read_state(
            fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE
        )
        assert state.state == "unreachable"
        assert state.cause == "redirect"
        time.sleep(0.2)
        with pytest.raises(BlockingIOError):
            dest_sock.accept()
    finally:
        dest_sock.close()


def test_budget_shorter_than_timeout_and_exhausted_across_endpoints(fake_syncthing):
    # 1. Budget shorter than single-request timeout
    hold_ev = fake_syncthing.hold("/rest/system/status")
    t0 = time.monotonic()
    state = syncthing.read_state(
        fake_syncthing.storage_path,
        stignore_line=initflow.STIGNORE_LINE,
        timeout=2.0,
        budget=0.2,
    )
    assert time.monotonic() - t0 < 1.0
    assert state.state == "unreachable"
    assert state.cause == "timeout"
    assert len(fake_syncthing.requests) == 1
    hold_ev.set()
    fake_syncthing.holds.clear()

    # 2. Exhausted across endpoints
    fake_syncthing.requests.clear()
    fake_syncthing.delay("*", 0.15)
    t1 = time.monotonic()
    state2 = syncthing.read_state(
        fake_syncthing.storage_path,
        stignore_line=initflow.STIGNORE_LINE,
        timeout=1.0,
        budget=0.4,
    )
    assert time.monotonic() - t1 < 1.5
    assert state2.state == "unreachable"
    assert state2.cause == "timeout"
    assert len(fake_syncthing.requests) < 5
    fake_syncthing.delays.clear()

    # 3. Expired deadline in _get_json makes zero contact
    fake_syncthing.requests.clear()
    out, parsed, cause = syncthing._get_json(
        fake_syncthing.address,
        "/rest/system/status",
        header_val=fake_syncthing.gui_value,
        tls_context=None,
        timeout=1.0,
        deadline=time.monotonic() - 1.0,
    )
    assert (out, parsed, cause) == ("unreachable", None, "timeout")
    assert fake_syncthing.requests == []


def test_byte_ceilings_for_config_response_and_stignore_and_stdlib_only(fake_syncthing):
    # 1. config.xml at exact ceiling is ok; +1 byte is config-unreadable with zero requests
    base_xml = (
        '<configuration version="37">\n'
        '  <gui enabled="true" tls="false">\n'
        f"    <address>{fake_syncthing.address}</address>\n"
        f"    <apikey>{fake_syncthing.gui_value}</apikey>\n"
        "  </gui>\n"
        "<!-- "
    )
    tail_xml = " -->\n</configuration>\n"
    pad_len = (
        syncthing._MAX_CONFIG_BYTES - len(base_xml.encode("utf-8")) - len(tail_xml.encode("utf-8"))
    )
    exact_xml = (base_xml + ("x" * pad_len) + tail_xml).encode("utf-8")
    assert len(exact_xml) == syncthing._MAX_CONFIG_BYTES
    fake_syncthing.config_path.write_bytes(exact_xml)
    state_ok = syncthing.read_state(
        fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE
    )
    assert state_ok.state == "ok"

    fake_syncthing.requests.clear()
    over_xml = (base_xml + ("x" * (pad_len + 1)) + tail_xml).encode("utf-8")
    assert len(over_xml) == syncthing._MAX_CONFIG_BYTES + 1
    fake_syncthing.config_path.write_bytes(over_xml)
    state_over = syncthing.read_state(
        fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE
    )
    assert state_over.state == "config-unreadable"
    assert fake_syncthing.requests == []
    fake_syncthing.write_config()

    # 2. /rest/system/connections body at exact _MAX_RESPONSE_BYTES vs +1 byte
    conn_prefix = (
        f'{{"connections":{{"{fake_syncthing.peer_id}":{{"connected":true}},'
        f'"{fake_syncthing.peer2_id}":{{"connected":true}}}},"pad":"'
    ).encode("utf-8")
    conn_suffix = b'"}'
    resp_pad = syncthing._MAX_RESPONSE_BYTES - len(conn_prefix) - len(conn_suffix)
    exact_body = conn_prefix + (b"p" * resp_pad) + conn_suffix
    assert len(exact_body) == syncthing._MAX_RESPONSE_BYTES
    assert isinstance(json.loads(exact_body.decode("utf-8")), dict)
    fake_syncthing.responses["/rest/system/connections"] = (
        200,
        exact_body,
        {"Content-Type": "application/json"},
    )
    state_resp_ok = syncthing.read_state(
        fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE
    )
    assert state_resp_ok.state == "ok"
    assert state_resp_ok.connected_peers == 2

    over_body = conn_prefix + (b"p" * (resp_pad + 1)) + conn_suffix
    assert len(over_body) == syncthing._MAX_RESPONSE_BYTES + 1
    fake_syncthing.responses["/rest/system/connections"] = (
        200,
        over_body,
        {"Content-Type": "application/json"},
    )
    state_resp_over = syncthing.read_state(
        fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE
    )
    assert state_resp_over.state == "ok"
    assert state_resp_over.connected_peers is None

    # 3. .stignore of _MAX_CONFIG_BYTES + 1 bytes gives stignore == "unreadable"
    st_file = fake_syncthing.storage_path / ".stignore"
    st_file.write_bytes(b"(?d)tmp*.tmp\n" + b"#" * syncthing._MAX_CONFIG_BYTES)
    state_st = syncthing.read_state(
        fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE
    )
    assert state_st.stignore == "unreadable"

    # 4. Every import in syncthing.py is in sys.stdlib_module_names
    tree = ast.parse(Path(syncthing.__file__).read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots <= sys.stdlib_module_names
