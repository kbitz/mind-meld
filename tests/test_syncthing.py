"""Unit tests for ``syncthing.py``: discovery, read-only REST client and folder lint."""

from __future__ import annotations

import ast
import dataclasses
import os
import sys
import time
from pathlib import Path

import pytest

from mind_meld import initflow, syncthing

_ORIGINAL_CONFIG_DIRS = syncthing.config_dirs


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
        assert header_val == fake_syncthing.credential

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
    fake_syncthing.write_config(credential="wrong-credential")
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
    request_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "request"
    ]
    assert request_calls, "expected at least one conn.request(...) call in syncthing.py"
    for call in request_calls:
        assert len(call.args) >= 2
        method_val = call.args[0]
        assert isinstance(method_val, ast.Constant) and method_val.value == "GET"
        kw_map = {kw.arg: kw.value for kw in call.keywords}
        assert "body" not in kw_map


def test_credential_never_reaches_the_state(fake_syncthing):
    state = syncthing.read_state(fake_syncthing.storage_path, stignore_line=initflow.STIGNORE_LINE)
    cred = fake_syncthing.credential
    assert cred not in repr(state)
    for f in dataclasses.fields(state):
        assert cred not in repr(getattr(state, f.name))
    for val in vars(state).values():
        assert cred not in repr(val)
