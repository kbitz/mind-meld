"""Unit tests for ``SyncthingWriter`` in ``syncthing.py`` (Track 117A T2)."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from mind_meld import initflow, safety, syncthing


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _call_op(writer: syncthing.SyncthingWriter, op_name: str, peer_id: str, folder_path: Path):
    if op_name == "create_folder":
        return writer.create_folder("mind-meld", "Mind Meld", folder_path)
    if op_name == "accept_folder_offer":
        return writer.accept_folder_offer("mind-meld", peer_id, "Mind Meld", folder_path)
    if op_name == "add_device":
        return writer.add_device(peer_id, "mac-b")
    if op_name == "share_folder":
        return writer.share_folder("mind-meld", peer_id)
    if op_name == "pending_devices":
        return writer.pending_devices()
    if op_name == "approval_state":
        return writer.approval_state("mind-meld", peer_id)
    if op_name == "mark_introducer":
        return writer.mark_introducer(peer_id)
    if op_name == "remove_default_folder":
        return writer.remove_default_folder()
    raise AssertionError(f"unknown op {op_name}")


_ALL_OPS = (
    "create_folder",
    "accept_folder_offer",
    "add_device",
    "share_folder",
    "pending_devices",
    "approval_state",
    "mark_introducer",
    "remove_default_folder",
)


@pytest.mark.parametrize("bad_rec", [None, "x", "dict_rec", "bad_device_id"])
@pytest.mark.parametrize("op_name", _ALL_OPS)
def test_writer_refuses_every_operation_without_valid_managed_record(
    fake_syncthing, isolated_home, bad_rec, op_name
):
    if bad_rec == "dict_rec":
        rec = {"device_id": fake_syncthing.my_id}
    elif bad_rec == "bad_device_id":
        rec = syncthing.ManagedRecord("not-a-valid-id")
    else:
        rec = bad_rec

    writer = syncthing.SyncthingWriter(rec)  # type: ignore[arg-type]
    res = _call_op(writer, op_name, fake_syncthing.peer_id, isolated_home / "Sync" / "mind-meld")
    assert res.outcome == "refused"
    assert res.cause == "not-managed"
    assert fake_syncthing.requests == []


def test_offline_default_cannot_supply_managed_record(fake_syncthing, isolated_home):
    fake_syncthing.set_shape(devices=(), folders=())
    fake_syncthing.close()
    check = syncthing.classify_config(sanitize=safety.safe_terminal_str)
    assert check.state == "default"
    assert check.source == "config"
    with pytest.raises(ValueError):
        check.managed_record()


@pytest.mark.parametrize(
    "device_fields",
    [
        {},
        {"devices": None},
        {"devices": {}},
        {"devices": [123]},
        {"devices": [{}]},
        {"devices": [{"deviceID": 123}]},
        {"devices": [{"deviceID": ""}]},
        {"devices": [" \t"]},
        {"devices": ["local-id", {"deviceID": 123}]},
    ],
)
@pytest.mark.parametrize(
    "op_name", ["create_folder", "accept_folder_offer", "share_folder", "remove_default_folder"]
)
def test_writer_malformed_folder_devices_never_authorize_a_write(
    fake_syncthing, isolated_home, device_fields, op_name
):
    target = isolated_home / "Sync"
    folder = fake_syncthing.stock_folder(target)
    if op_name != "remove_default_folder":
        folder["id"] = "mind-meld"
    del folder["devices"]
    folder.update(device_fields)
    if isinstance(folder.get("devices"), list):
        folder["devices"] = [
            fake_syncthing.my_id if entry == "local-id" else entry for entry in folder["devices"]
        ]
    fake_syncthing.set_config(devices=[{"deviceID": fake_syncthing.peer_id}], folders=[folder])
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    result = _call_op(writer, op_name, fake_syncthing.peer_id, target)
    expected = (
        syncthing.WriteResult("refused", "default-folder-in-use")
        if op_name == "remove_default_folder"
        else syncthing.WriteResult("error", "bad-response")
    )
    assert result == expected
    assert fake_syncthing.bodies == []


@pytest.mark.parametrize("op_name", _ALL_OPS)
def test_writer_refuses_mismatched_managed_record_after_single_status_get(
    fake_syncthing, isolated_home, op_name
):
    fake_syncthing.set_config(devices=(), folders=())
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.peer_id))
    res = _call_op(writer, op_name, fake_syncthing.peer2_id, isolated_home / "Sync" / "mind-meld")
    assert res.outcome == "refused"
    assert res.cause == "not-managed"
    assert [(r[0], r[1]) for r in fake_syncthing.requests] == [("GET", "/rest/system/status")]
    assert fake_syncthing.bodies == []


def test_create_folder_options_rerun_overlaps_and_unapplied_options(fake_syncthing, isolated_home):
    assert set(syncthing._MM_FOLDER_VALUES) == {n for n, _, _ in syncthing.FOLDER_OPTIONS}
    fake_syncthing.set_config(devices=(), folders=())
    managed = syncthing.ManagedRecord(fake_syncthing.my_id)
    writer = syncthing.SyncthingWriter(managed)

    target = isolated_home / "Sync" / "mind-meld"
    res = writer.create_folder("mind-meld", "Mind Meld", "~/Sync/mind-meld")
    assert res == syncthing.WriteResult("ok", None)
    assert len(fake_syncthing.bodies) == 1
    method, path, body = fake_syncthing.bodies[0]
    assert (method, path) == ("POST", "/rest/config/folders")
    assert body["path"] == str(target.resolve(strict=False))
    assert Path(body["path"]).is_absolute()
    assert body["devices"] == [{"deviceID": fake_syncthing.my_id}]
    opts, probs = syncthing._extract_options(body)
    assert probs == []
    for opt_name, _, _ in syncthing.FOLDER_OPTIONS:
        assert syncthing.option_is_ok(opt_name, opts[opt_name])

    # Verify read_state sees no option problems once .stignore is present
    target.mkdir(parents=True, exist_ok=True)
    (target / ".stignore").write_text("(?d)tmp*.tmp\n", encoding="utf-8")
    st_state = syncthing.read_state(target, stignore_line=initflow.STIGNORE_LINE)
    assert st_state.state == "ok"
    assert st_state.problems == ()

    # Rerun is unchanged with no second POST
    res_again = writer.create_folder("mind-meld", "Mind Meld", target)
    assert res_again == syncthing.WriteResult("unchanged", None)
    assert len(fake_syncthing.bodies) == 1

    # Same ID at a different path -> folder-exists
    res_diff_path = writer.create_folder("mind-meld", "Mind Meld", isolated_home / "other")
    assert res_diff_path == syncthing.WriteResult("refused", "folder-exists")

    # Nested path or containing path -> path-overlap
    res_nested = writer.create_folder("nested-id", "Nested", target / "sub")
    assert res_nested == syncthing.WriteResult("refused", "path-overlap")
    res_containing = writer.create_folder("parent-id", "Parent", isolated_home / "Sync")
    assert res_containing == syncthing.WriteResult("refused", "path-overlap")

    # Stock Default Folder at ~/Sync causes path-overlap for ~/Sync/mind-meld
    stock_dir = isolated_home / "Sync"
    fake_syncthing.set_config(devices=(), folders=[fake_syncthing.stock_folder(stock_dir)])
    res_stock_overlap = writer.create_folder("mind-meld", "Mind Meld", target)
    assert res_stock_overlap == syncthing.WriteResult("refused", "path-overlap")

    # Bad folder ID
    assert writer.create_folder("../bad", "Bad", target) == syncthing.WriteResult(
        "refused", "bad-folder-id"
    )
    assert writer.create_folder("", "Bad", target) == syncthing.WriteResult(
        "refused", "bad-folder-id"
    )

    # on_write hook that drops order -> error / options-not-applied
    fake_syncthing.set_config(devices=(), folders=())

    def drop_order(m: str, p: str, b: object) -> None:
        for f in fake_syncthing.responses["/rest/config/folders"]:
            f["order"] = "random"

    fake_syncthing.on_write = drop_order
    res_dropped = writer.create_folder("mind-meld", "Mind Meld", target)
    assert res_dropped == syncthing.WriteResult("error", "options-not-applied")


def test_add_device_validations_idempotence_and_body(fake_syncthing, isolated_home):
    fake_syncthing.set_config(devices=(), folders=())
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))

    assert writer.add_device("not-valid", "x") == syncthing.WriteResult("refused", "bad-device-id")
    assert writer.add_device(fake_syncthing.peer_id.lower(), "x") == syncthing.WriteResult(
        "refused", "bad-device-id"
    )
    assert writer.add_device(fake_syncthing.my_id, "self") == syncthing.WriteResult(
        "refused", "own-device"
    )
    assert fake_syncthing.bodies == []

    res = writer.add_device(fake_syncthing.peer_id, "mac-b")
    assert res == syncthing.WriteResult("ok", None)
    assert fake_syncthing.bodies == [
        (
            "POST",
            "/rest/config/devices",
            {
                "deviceID": fake_syncthing.peer_id,
                "name": "mac-b",
                "addresses": ["dynamic"],
                "introducer": False,
                "autoAcceptFolders": False,
                "paused": False,
            },
        )
    ]

    # Already configured -> unchanged with no new POST
    res2 = writer.add_device(fake_syncthing.peer_id, "mac-b")
    assert res2 == syncthing.WriteResult("unchanged", None)
    assert len(fake_syncthing.bodies) == 1


def test_share_folder_validations_and_byte_preserving_patch(fake_syncthing, isolated_home):
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    target = isolated_home / "Sync" / "mind-meld"

    # Missing folder
    fake_syncthing.set_config(
        devices=[{"deviceID": fake_syncthing.peer_id, "name": "mac-b"}],
        folders=(),
    )
    assert writer.share_folder("mind-meld", fake_syncthing.peer_id) == syncthing.WriteResult(
        "refused", "no-folder"
    )

    # Non-mm folder (wrong order option)
    bad_folder = {
        "id": "mind-meld",
        "label": "Mind Meld",
        "path": str(target),
        **syncthing._MM_FOLDER_VALUES,
        "order": "random",
        "devices": [{"deviceID": fake_syncthing.my_id}],
    }
    fake_syncthing.set_config(
        devices=[{"deviceID": fake_syncthing.peer_id, "name": "mac-b"}],
        folders=[bad_folder],
    )
    assert writer.share_folder("mind-meld", fake_syncthing.peer_id) == syncthing.WriteResult(
        "refused", "not-mm-folder"
    )

    # Device not configured
    existing_entry = {
        "deviceID": fake_syncthing.my_id,
        "introducedBy": "",
        "customField": {"nested": [1, 2, 3]},
    }
    good_folder = {
        "id": "mind-meld",
        "label": "Mind Meld",
        "path": str(target),
        **syncthing._MM_FOLDER_VALUES,
        "devices": [existing_entry],
    }
    fake_syncthing.set_config(devices=(), folders=[good_folder])
    assert writer.share_folder("mind-meld", fake_syncthing.peer_id) == syncthing.WriteResult(
        "refused", "device-not-configured"
    )

    # Configured -> PATCH preserves existing entries byte-for-byte
    fake_syncthing.set_config(
        devices=[{"deviceID": fake_syncthing.peer_id, "name": "mac-b"}],
        folders=[good_folder],
    )
    res = writer.share_folder("mind-meld", fake_syncthing.peer_id)
    assert res == syncthing.WriteResult("ok", None)
    assert fake_syncthing.bodies == [
        (
            "PATCH",
            "/rest/config/folders/mind-meld",
            {
                "devices": [
                    existing_entry,
                    {"deviceID": fake_syncthing.peer_id},
                ]
            },
        )
    ]

    # Rerun -> unchanged with no second PATCH
    res2 = writer.share_folder("mind-meld", fake_syncthing.peer_id)
    assert res2 == syncthing.WriteResult("unchanged", None)
    assert len(fake_syncthing.bodies) == 1


def test_accept_folder_offer_validations_and_success(fake_syncthing, isolated_home):
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    target = isolated_home / "Sync" / "mind-meld"
    pending_path = f"/rest/cluster/pending/folders?device={fake_syncthing.peer_id}"

    # Device not configured
    fake_syncthing.set_config(devices=(), folders=())
    assert writer.accept_folder_offer(
        "mind-meld", fake_syncthing.peer_id, "Mind Meld", target
    ) == syncthing.WriteResult("refused", "device-not-configured")

    # Device configured, but no offer
    fake_syncthing.set_config(
        devices=[{"deviceID": fake_syncthing.peer_id, "name": "mac-b"}],
        folders=(),
    )
    fake_syncthing.responses[pending_path] = {}
    assert writer.accept_folder_offer(
        "mind-meld", fake_syncthing.peer_id, "Mind Meld", target
    ) == syncthing.WriteResult("refused", "no-offer")

    # Offer only from another device
    fake_syncthing.responses[pending_path] = {
        "mind-meld": {
            "offeredBy": {
                fake_syncthing.peer2_id: {
                    "time": "2026-11-08T00:00:00Z",
                    "label": "Peer Label",
                    "receiveEncrypted": False,
                    "remoteEncrypted": False,
                }
            }
        }
    }
    assert writer.accept_folder_offer(
        "mind-meld", fake_syncthing.peer_id, "Mind Meld", target
    ) == syncthing.WriteResult("refused", "no-offer")

    # Encrypted offers refused for each flag
    for enc_flag in ("receiveEncrypted", "remoteEncrypted"):
        fake_syncthing.responses[pending_path] = {
            "mind-meld": {
                "offeredBy": {
                    fake_syncthing.peer_id: {
                        "time": "2026-11-08T00:00:00Z",
                        "label": "Untrusted Peer Label",
                        "receiveEncrypted": enc_flag == "receiveEncrypted",
                        "remoteEncrypted": enc_flag == "remoteEncrypted",
                    }
                }
            }
        }
        assert writer.accept_folder_offer(
            "mind-meld", fake_syncthing.peer_id, "Mind Meld", target
        ) == syncthing.WriteResult("refused", "encrypted-offer")

    # Valid unencrypted offer succeeds with mm's label and both devices
    fake_syncthing.responses[pending_path] = {
        "mind-meld": {
            "offeredBy": {
                fake_syncthing.peer_id: {
                    "time": "2026-11-08T00:00:00Z",
                    "label": "Untrusted Peer Label",
                    "receiveEncrypted": False,
                    "remoteEncrypted": False,
                }
            }
        }
    }
    res = writer.accept_folder_offer("mind-meld", fake_syncthing.peer_id, "Mind Meld", target)
    assert res == syncthing.WriteResult("ok", None)
    assert len(fake_syncthing.bodies) == 1
    method, path, body = fake_syncthing.bodies[0]
    assert (method, path) == ("POST", "/rest/config/folders")
    assert body["id"] == "mind-meld"
    assert body["label"] == "Mind Meld"
    assert body["devices"] == [
        {"deviceID": fake_syncthing.my_id},
        {"deviceID": fake_syncthing.peer_id},
    ]
    _, probs = syncthing._extract_options(body)
    assert probs == []

    # Syncthing removes an accepted offer; retry must use the configured folder.
    assert fake_syncthing.responses[pending_path] == {}
    res_rerun = writer.accept_folder_offer("mind-meld", fake_syncthing.peer_id, "Mind Meld", target)
    assert res_rerun == syncthing.WriteResult("unchanged", None)
    assert len(fake_syncthing.bodies) == 1


@pytest.mark.parametrize("op_name", _ALL_OPS)
@pytest.mark.parametrize("suffix", ["\ninvalid", "\rinvalid", "\u2603"])
def test_writer_malformed_header_is_safe_and_never_contacts_server(
    fake_syncthing, isolated_home, op_name, suffix, capsys
):
    fake_syncthing.write_config(gui_value=fake_syncthing.gui_value + suffix)
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    result = _call_op(writer, op_name, fake_syncthing.peer_id, isolated_home / "fleet")
    assert result.outcome == "config-unreadable"
    assert fake_syncthing.requests == []
    assert fake_syncthing.gui_value not in repr(result)
    captured = capsys.readouterr()
    assert fake_syncthing.gui_value not in captured.out + captured.err


@pytest.mark.parametrize(
    "op_name", ["create_folder", "accept_folder_offer", "remove_default_folder"]
)
@pytest.mark.parametrize("failure", [OSError, RuntimeError])
def test_writer_path_resolution_failure_returns_safe_outcome(
    fake_syncthing, isolated_home, monkeypatch, op_name, failure
):
    target = isolated_home / "Sync"
    fake_syncthing.set_config(
        devices=[{"deviceID": fake_syncthing.peer_id}],
        folders=[fake_syncthing.stock_folder(target)] if op_name == "remove_default_folder" else [],
    )
    pending_path = f"/rest/cluster/pending/folders?device={fake_syncthing.peer_id}"
    fake_syncthing.responses[pending_path] = {
        "mind-meld": {"offeredBy": {fake_syncthing.peer_id: {}}}
    }
    original_resolve = Path.resolve

    def fail_target(path, *args, **kwargs):
        if path == target:
            raise failure(fake_syncthing.gui_value)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_target)
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    result = _call_op(writer, op_name, fake_syncthing.peer_id, target)
    assert result == syncthing.WriteResult("error", "path-unreadable")
    assert fake_syncthing.gui_value not in repr(result)
    assert fake_syncthing.bodies == []


def test_pending_devices_parsing_sorting_and_errors(fake_syncthing, isolated_home):
    fake_syncthing.set_config(devices=(), folders=())
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))

    raw_peer_name = "peer-\x1b[31mraw"
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        fake_syncthing.peer2_id: {
            "name": raw_peer_name,
            "address": "192.0.2.2:22000",
            "time": "2026-11-08T01:00:00Z",
        },
        fake_syncthing.peer_id: {
            "name": None,
            "address": 123,
        },
        "not-a-device-id": {"name": "ignored"},
        fake_syncthing.my_id: "not-a-dict",
    }
    pending = writer.pending_devices()
    assert pending.outcome == "ok"
    assert pending.cause is None
    assert pending.devices == (
        syncthing.PendingDevice(
            device_id=fake_syncthing.peer_id,
            name="",
            address="",
            time="",
        ),
        syncthing.PendingDevice(
            device_id=fake_syncthing.peer2_id,
            name=raw_peer_name,
            address="192.0.2.2:22000",
            time="2026-11-08T01:00:00Z",
        ),
    )
    assert fake_syncthing.bodies == []

    # Non-dict response -> error / bad-response
    fake_syncthing.responses["/rest/cluster/pending/devices"] = ["not-a-dict"]
    bad = writer.pending_devices()
    assert bad == syncthing.PendingDevices("error", "bad-response", ())

    # 403 -> unauthorized
    fake_syncthing.responses["/rest/cluster/pending/devices"] = 403
    unauth = writer.pending_devices()
    assert unauth == syncthing.PendingDevices("unauthorized", "http-403", ())


def test_mark_introducer_validations_and_patch(fake_syncthing, isolated_home):
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    fake_syncthing.set_config(devices=(), folders=())

    assert writer.mark_introducer(fake_syncthing.peer_id) == syncthing.WriteResult(
        "refused", "device-not-configured"
    )

    fake_syncthing.set_config(
        devices=[{"deviceID": fake_syncthing.peer_id, "name": "mac-b", "introducer": False}],
        folders=(),
    )
    res = writer.mark_introducer(fake_syncthing.peer_id)
    assert res == syncthing.WriteResult("ok", None)
    assert fake_syncthing.bodies == [
        ("PATCH", f"/rest/config/devices/{fake_syncthing.peer_id}", {"introducer": True})
    ]

    # Already set -> unchanged
    res2 = writer.mark_introducer(fake_syncthing.peer_id)
    assert res2 == syncthing.WriteResult("unchanged", None)
    assert len(fake_syncthing.bodies) == 1


def test_remove_default_folder_guards_and_delete(fake_syncthing, isolated_home, tmp_path):
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    stock_dir = isolated_home / "Sync"

    # Absent -> unchanged
    fake_syncthing.set_config(devices=(), folders=())
    assert writer.remove_default_folder() == syncthing.WriteResult("unchanged", None)
    assert fake_syncthing.bodies == []

    # Non-stock path -> default-folder-in-use
    fake_syncthing.set_config(
        devices=(),
        folders=[fake_syncthing.stock_folder(tmp_path / "custom-default")],
    )
    assert writer.remove_default_folder() == syncthing.WriteResult(
        "refused", "default-folder-in-use"
    )

    # Shared -> default-folder-in-use
    fake_syncthing.set_config(
        devices=(),
        folders=[fake_syncthing.stock_folder(stock_dir, shared_with=[fake_syncthing.peer_id])],
    )
    assert writer.remove_default_folder() == syncthing.WriteResult(
        "refused", "default-folder-in-use"
    )

    # Non-empty -> default-folder-in-use
    stock_dir.mkdir(parents=True, exist_ok=True)
    user_file = stock_dir / "keep.txt"
    user_file.write_text("data\n", encoding="utf-8")
    fake_syncthing.set_config(
        devices=(),
        folders=[fake_syncthing.stock_folder(stock_dir)],
    )
    assert writer.remove_default_folder() == syncthing.WriteResult(
        "refused", "default-folder-in-use"
    )
    assert fake_syncthing.bodies == []

    # Empty (with .stfolder marker) -> DELETE succeeds and leaves ~/Sync on disk
    user_file.unlink()
    marker_dir = stock_dir / ".stfolder"
    marker_dir.mkdir(exist_ok=True)
    (marker_dir / "syncthing-folder-123456.txt").write_text("ok\n", encoding="utf-8")
    res = writer.remove_default_folder()
    assert res == syncthing.WriteResult("ok", None)
    assert fake_syncthing.bodies == [("DELETE", "/rest/config/folders/default", None)]
    assert stock_dir.is_dir()
    assert (marker_dir / "syncthing-folder-123456.txt").is_file()


def test_write_errors_redirects_and_stopped_server(fake_syncthing, isolated_home):
    fake_syncthing.set_config(devices=(), folders=())
    writer = syncthing.SyncthingWriter(
        syncthing.ManagedRecord(fake_syncthing.my_id),
        timeout=0.1,
        budget=0.25,
    )

    # 500 on POST
    fake_syncthing.write_responses[("POST", "/rest/config/devices")] = 500
    assert writer.add_device(fake_syncthing.peer_id, "mac-b") == syncthing.WriteResult(
        "error", "http-500"
    )

    # 403 on POST
    fake_syncthing.write_responses[("POST", "/rest/config/devices")] = 403
    assert writer.add_device(fake_syncthing.peer_id, "mac-b") == syncthing.WriteResult(
        "unauthorized", "http-403"
    )

    # 302 redirect on POST never contacts Location target
    dest_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dest_sock.bind(("127.0.0.1", 0))
    dest_sock.listen(5)
    dest_sock.setblocking(False)
    dest_port = dest_sock.getsockname()[1]
    try:
        fake_syncthing.write_responses[("POST", "/rest/config/devices")] = (
            302,
            b"",
            {"Location": f"http://127.0.0.1:{dest_port}/rest/config/devices"},
        )
        assert writer.add_device(fake_syncthing.peer_id, "mac-b") == syncthing.WriteResult(
            "unreachable", "redirect"
        )
        time.sleep(0.2)
        with pytest.raises(BlockingIOError):
            dest_sock.accept()
    finally:
        dest_sock.close()
    fake_syncthing.write_responses.clear()

    # Stopped server -> unreachable / refused with no offline fallback
    fake_syncthing.close()
    assert writer.add_device(fake_syncthing.peer_id, "mac-b") == syncthing.WriteResult(
        "unreachable", "refused"
    )


@pytest.mark.parametrize("op_name", ["create_folder", "accept_folder_offer"])
def test_applied_folder_write_timeout_rerun_is_unchanged(fake_syncthing, isolated_home, op_name):
    fake_syncthing.set_config(devices=[{"deviceID": fake_syncthing.peer_id}], folders=())
    pending_path = f"/rest/cluster/pending/folders?device={fake_syncthing.peer_id}"
    fake_syncthing.responses[pending_path] = {
        "mind-meld": {"offeredBy": {fake_syncthing.peer_id: {}}}
    }
    applied = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def hold_applied_write(method, path, body):
        assert (method, path) == ("POST", "/rest/config/folders")
        applied.set()
        try:
            release.wait(timeout=5.0)
        finally:
            finished.set()

    fake_syncthing.on_write = hold_applied_write
    writer = syncthing.SyncthingWriter(
        syncthing.ManagedRecord(fake_syncthing.my_id), timeout=0.1, budget=0.5
    )
    target = isolated_home / "Sync" / "mind-meld"
    started = time.monotonic()
    try:
        result = _call_op(writer, op_name, fake_syncthing.peer_id, target)
        assert time.monotonic() - started < 1.5
        assert result == syncthing.WriteResult("unreachable", "timeout")
        assert applied.is_set(), "the timeout must occur after the POST applies"
        assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [("POST", "/rest/config/folders")]
        assert fake_syncthing.responses["/rest/config/folders"][0]["id"] == "mind-meld"
        if op_name == "accept_folder_offer":
            assert fake_syncthing.responses[pending_path] == {}
    finally:
        release.set()
        assert finished.wait(timeout=2.0)
    fake_syncthing.on_write = None
    assert _call_op(writer, op_name, fake_syncthing.peer_id, target) == syncthing.WriteResult(
        "unchanged"
    )
    assert len(fake_syncthing.bodies) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/rest/config"),
        ("POST", "/rest/system/restart"),
        ("PATCH", "/rest/config/options"),
        ("PATCH", "/rest/config/gui"),
        ("DELETE", f"/rest/config/devices/{syncthing.STOCK_DEFAULT_FOLDER_ID}"),
        ("DELETE", "/rest/config/folders/mind-meld"),
        ("POST", "/rest/config/folders?x=1"),
    ],
)
def test_send_json_allowlist_rejects_disallowed_routes_before_connecting(
    fake_syncthing, method, path
):
    endpoint, _, _, _, _ = syncthing._open_endpoint()
    assert endpoint is not None
    with pytest.raises(ValueError) as exc_info:
        syncthing._send_json(
            endpoint,
            method,
            path,
            {"x": 1},
            timeout=1.0,
            deadline=time.monotonic() + 2.0,
        )
    assert fake_syncthing.gui_value not in str(exc_info.value)
    assert fake_syncthing.requests == []


def test_scripted_o2_flows_and_credential_never_exposed(fake_syncthing, isolated_home, capsys):
    stock_dir = isolated_home / "Sync"
    mm_dir = stock_dir / "mind-meld"
    managed = syncthing.ManagedRecord(fake_syncthing.my_id)
    writer = syncthing.SyncthingWriter(managed)
    results: list[object] = []

    # Flow 1: new fleet (remove_default_folder, create_folder)
    fake_syncthing.set_config(
        devices=(),
        folders=[fake_syncthing.stock_folder(stock_dir)],
    )
    r1 = writer.remove_default_folder()
    r2 = writer.create_folder("mind-meld", "Mind Meld", mm_dir)
    results.extend([r1, r2])
    assert (r1.outcome, r2.outcome) == ("ok", "ok")

    # Flow 2: join (add_device, mark_introducer, accept_folder_offer)
    fake_syncthing.set_config(devices=(), folders=())
    fake_syncthing.responses[f"/rest/cluster/pending/folders?device={fake_syncthing.peer_id}"] = {
        "mind-meld": {
            "offeredBy": {
                fake_syncthing.peer_id: {
                    "time": "2026-11-08T00:00:00Z",
                    "label": "Mind Meld",
                    "receiveEncrypted": False,
                    "remoteEncrypted": False,
                }
            }
        }
    }
    r3 = writer.add_device(fake_syncthing.peer_id, "mac-b")
    r4 = writer.mark_introducer(fake_syncthing.peer_id)
    r5 = writer.accept_folder_offer("mind-meld", fake_syncthing.peer_id, "Mind Meld", mm_dir)
    results.extend([r3, r4, r5])
    assert (r3.outcome, r4.outcome, r5.outcome) == ("ok", "ok", "ok")

    # Flow 3: approve (pending_devices, add_device, share_folder, mark_introducer)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        fake_syncthing.peer2_id: {
            "name": "linux-b",
            "address": "192.0.2.3:22000",
            "time": "2026-11-08T02:00:00Z",
        }
    }
    r6 = writer.pending_devices()
    r7 = writer.add_device(fake_syncthing.peer2_id, "linux-b")
    r8 = writer.share_folder("mind-meld", fake_syncthing.peer2_id)
    r9 = writer.mark_introducer(fake_syncthing.peer2_id)
    results.extend([r6, r7, r8, r9])
    assert (r6.outcome, r7.outcome, r8.outcome, r9.outcome) == ("ok", "ok", "ok", "ok")

    # Every non-GET request passed _write_route_ok and no PUT was sent
    assert fake_syncthing.bodies
    for method, path, body in fake_syncthing.bodies:
        assert method != "PUT"
        assert syncthing._write_route_ok(method, path)
        assert fake_syncthing.gui_value not in repr(body)

    # Also collect failure results for credential leak check
    bad_writer = syncthing.SyncthingWriter(None)
    results.append(bad_writer.create_folder("mind-meld", "Mind Meld", mm_dir))
    results.append(bad_writer.pending_devices())

    for res in results:
        assert fake_syncthing.gui_value not in repr(res)

    captured = capsys.readouterr()
    assert fake_syncthing.gui_value not in captured.out
    assert fake_syncthing.gui_value not in captured.err


def test_approval_state_fields_and_get_only(fake_syncthing, isolated_home):
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    target = isolated_home / "Sync" / "mind-meld"

    # Validation refusals send no config GETs and no writes
    assert writer.approval_state("../bad", fake_syncthing.peer_id) == syncthing.ApprovalState(
        "refused", "bad-folder-id"
    )
    assert writer.approval_state("mind-meld", "not-valid") == syncthing.ApprovalState(
        "refused", "bad-device-id"
    )
    assert writer.approval_state("mind-meld", fake_syncthing.my_id) == syncthing.ApprovalState(
        "refused", "own-device"
    )
    assert fake_syncthing.bodies == []

    # Missing folder and unconfigured device
    fake_syncthing.set_config(devices=(), folders=())
    assert writer.approval_state("mind-meld", fake_syncthing.peer_id) == syncthing.ApprovalState(
        outcome="ok",
        cause=None,
        folder="missing",
        configured=False,
        introducer=False,
        shared=False,
        writer_shape=False,
    )

    # Not-mm folder (bad option)
    bad_folder = {**fake_syncthing.stock_folder(target), "id": "mind-meld", "order": "random"}
    fake_syncthing.set_config(devices=(), folders=[bad_folder])
    assert writer.approval_state("mind-meld", fake_syncthing.peer_id) == syncthing.ApprovalState(
        outcome="ok",
        cause=None,
        folder="not-mm",
        configured=False,
        introducer=False,
        shared=False,
        writer_shape=False,
    )

    # Malformed folder device list -> error / bad-response
    malformed_folder = {
        **fake_syncthing.stock_folder(target),
        "id": "mind-meld",
        "devices": [123],
    }
    fake_syncthing.set_config(devices=(), folders=[malformed_folder])
    assert writer.approval_state("mind-meld", fake_syncthing.peer_id) == syncthing.ApprovalState(
        "error", "bad-response"
    )
    assert fake_syncthing.bodies == []

    # Configured via add_device -> writer_shape True, introducer False, shared False
    ok_folder = {**fake_syncthing.stock_folder(target), "id": "mind-meld"}
    fake_syncthing.set_config(devices=(), folders=[ok_folder])
    assert writer.add_device(fake_syncthing.peer_id, "linux-b") == syncthing.WriteResult("ok", None)
    fake_syncthing.bodies.clear()
    assert writer.approval_state("mind-meld", fake_syncthing.peer_id) == syncthing.ApprovalState(
        outcome="ok",
        cause=None,
        folder="ok",
        configured=True,
        introducer=False,
        shared=False,
        writer_shape=True,
    )
    assert fake_syncthing.bodies == []

    # After mark_introducer -> introducer True, writer_shape still True
    assert writer.mark_introducer(fake_syncthing.peer_id) == syncthing.WriteResult("ok", None)
    fake_syncthing.bodies.clear()
    assert writer.approval_state("mind-meld", fake_syncthing.peer_id) == syncthing.ApprovalState(
        outcome="ok",
        cause=None,
        folder="ok",
        configured=True,
        introducer=True,
        shared=False,
        writer_shape=True,
    )
    assert fake_syncthing.bodies == []

    # After share_folder -> shared True
    assert writer.share_folder("mind-meld", fake_syncthing.peer_id) == syncthing.WriteResult(
        "ok", None
    )
    fake_syncthing.bodies.clear()
    assert writer.approval_state("mind-meld", fake_syncthing.peer_id) == syncthing.ApprovalState(
        outcome="ok",
        cause=None,
        folder="ok",
        configured=True,
        introducer=True,
        shared=True,
        writer_shape=True,
    )
    assert fake_syncthing.bodies == []

    # writer_shape False for each non-writer deviation
    base_dev = {
        "deviceID": fake_syncthing.peer_id,
        "name": "linux-b",
        "addresses": ["dynamic"],
        "introducer": False,
        "autoAcceptFolders": False,
        "paused": False,
    }
    for override in (
        {"paused": True},
        {"autoAcceptFolders": True},
        {"addresses": ["tcp://192.0.2.1:22000"]},
        {"introducedBy": fake_syncthing.peer2_id},
    ):
        fake_syncthing.set_config(devices=[{**base_dev, **override}], folders=[ok_folder])
        state = writer.approval_state("mind-meld", fake_syncthing.peer_id)
        assert state.outcome == "ok"
        assert state.configured is True
        assert state.writer_shape is False, override
    assert fake_syncthing.bodies == []


def test_standin_drops_pending_device_on_add_and_stop_after_restarts(fake_syncthing, isolated_home):
    target = isolated_home / "Sync" / "mind-meld"
    ok_folder = {**fake_syncthing.stock_folder(target), "id": "mind-meld"}
    fake_syncthing.set_config(devices=(), folders=[ok_folder])
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        fake_syncthing.peer_id: {
            "name": "linux-b",
            "address": "192.0.2.4:22000",
            "time": "2026-11-08T03:00:00Z",
        }
    }
    fake_syncthing.stop_after("POST", "/rest/config/devices")

    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(fake_syncthing.my_id))
    assert writer.add_device(fake_syncthing.peer_id) == syncthing.WriteResult("ok", None)
    assert fake_syncthing.responses["/rest/cluster/pending/devices"] == {}

    reqs_after_add = list(fake_syncthing.requests)
    bodies_after_add = list(fake_syncthing.bodies)

    # Next writer call fails as unreachable and records no request or body
    assert writer.mark_introducer(fake_syncthing.peer_id) == syncthing.WriteResult(
        "unreachable", "refused"
    )
    assert fake_syncthing.requests == reqs_after_add
    assert fake_syncthing.bodies == bodies_after_add

    # Restarting serves the applied state on the same port
    fake_syncthing.start()
    state = writer.approval_state("mind-meld", fake_syncthing.peer_id)
    assert state.outcome == "ok"
    assert state.configured is True
    assert state.writer_shape is True
    assert state.introducer is False
