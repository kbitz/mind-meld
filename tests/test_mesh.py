"""Tests for encrypted Syncthing device-ID records and the managed mesh pass (Track 152A)."""

from __future__ import annotations

import errno
import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mind_meld import attemptlog, cli, config, crypto, devices, meshflow, sidecar, syncthing
from mind_meld.cli import app
from mind_meld.devices import register_device
from mind_meld.errors import StorageError, StorageNotFound
from mind_meld.storage.keys import (
    device_key,
    parse_syncthing_record_key,
    syncthing_record_key,
)
from mind_meld.storage.local import LocalBackend
from tests.syncthing_folder import make_syncthing_like_certificate

runner = CliRunner()
PASSPHRASE = "fleet-mesh-passphrase"
TEST_MEMORY_KB = 1024


def _valid_syncthing_id(seed_char: str) -> str:
    chunk13 = seed_char * 13
    group14 = chunk13 + syncthing._luhn32(chunk13)
    full56 = group14 * 4
    return "-".join(full56[i : i + 7] for i in range(0, 56, 7))


def _bad_check_digit_id(valid_id: str) -> str:
    last = "B" if valid_id[-1] != "B" else "C"
    return valid_id[:-1] + last


def _setup_fleet(
    fake_syncthing: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    managed: bool = True,
    publish_own: bool = True,
) -> tuple[Path, LocalBackend, str]:
    cfg_path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(crypto, "MIN_ARGON2_MEMORY_KB", TEST_MEMORY_KB, raising=False)

    my_id = _valid_syncthing_id("A")
    fake_syncthing.my_id = my_id
    storage_dir = tmp_path / "sync"
    fake_syncthing.write_config()
    fake_syncthing.set_folder(storage_dir, peers=[])
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {}
    if managed:
        config.save_syncthing_record(my_id)

    claude_dir = tmp_path / "claude"
    (claude_dir / "projects").mkdir(parents=True, exist_ok=True)
    events_dir = tmp_path / "mm-events"
    (events_dir / "events").mkdir(parents=True, exist_ok=True)

    backend = LocalBackend(str(storage_dir))
    init_fetch = crypto.bootstrap_crypto_init(backend, PASSPHRASE, TEST_MEMORY_KB)
    root_salt = init_fetch.root_salt
    memory_kb = TEST_MEMORY_KB
    assert root_salt is not None
    crypto.set_crypto_session(root_salt, memory_kb)
    register_device(backend, "dev-a", "mac-a")
    if publish_own:
        devices.publish_record(backend, "dev-a", my_id, PASSPHRASE, memory_kb)

    config.save_config(
        {
            "device": {"id": "dev-a", "name": "mac-a"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "sources": [
                    {"name": "claude", "type": "claude", "path": str(claude_dir)},
                    {
                        "name": "mm-events",
                        "type": "generic",
                        "path": str(events_dir),
                        "include_dirs": ["events"],
                    },
                ],
                "max_file_size": 10 * 1024 * 1024,
            },
            "crypto": {
                "argon2_memory_kb": memory_kb,
                "root_salt_fp": crypto.root_salt_fingerprint(root_salt),
            },
        }
    )
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    fake_syncthing.requests.clear()
    fake_syncthing.bodies.clear()
    return storage_dir, backend, my_id


def test_syncthing_record_key_validation_and_parsing():
    assert syncthing_record_key("abc12345") == "syncthing/abc12345.enc"
    assert parse_syncthing_record_key("syncthing/abc12345.enc") == "abc12345"

    for bad_id in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
        with pytest.raises(ValueError):
            syncthing_record_key(bad_id)

    for bad_key in (
        "",
        "devices/abc12345.json",
        "syncthing/abc12345.json",
        "syncthing/.enc",
        "syncthing/./.enc",
        "syncthing/../abc.enc",
        "syncthing/sub/abc.enc",
        "syncthing/abc.sync-conflict-20261119-120000-AAAAAAA.enc",
        "syncthing/abc 2.enc",
    ):
        assert parse_syncthing_record_key(bad_key) is None


def test_read_fleet_records_classifications_and_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "MIN_ARGON2_MEMORY_KB", TEST_MEMORY_KB, raising=False)
    backend = LocalBackend(str(tmp_path / "store"))
    init_fetch = crypto.bootstrap_crypto_init(backend, PASSPHRASE, TEST_MEMORY_KB)
    assert init_fetch.root_salt is not None
    crypto.set_crypto_session(init_fetch.root_salt, TEST_MEMORY_KB)

    id_valid = _valid_syncthing_id("B")
    id_retired = _valid_syncthing_id("C")
    id_copied = _valid_syncthing_id("D")
    id_bad_check = _bad_check_digit_id(_valid_syncthing_id("E"))

    for mm_id, name in (
        ("dev-valid", "mac-b"),
        ("dev-tampered", "mac-t"),
        ("dev-newer", "mac-n"),
        ("dev-oversized", "mac-o"),
        ("dev-malformed", "mac-m"),
        ("dev-badcheck", "mac-c"),
        ("dev-copied", "mac-copy"),
        ("dev-vanished", "mac-v"),
    ):
        register_device(backend, mm_id, name)

    # 1. Valid record for registered device
    assert devices.publish_record(backend, "dev-valid", id_valid, PASSPHRASE, TEST_MEMORY_KB)
    # Invalid Syncthing IDs are refused by publish_record
    assert not devices.publish_record(backend, "dev-valid", "not-an-id", PASSPHRASE, TEST_MEMORY_KB)
    assert not devices.publish_record(
        backend, "dev-valid", id_bad_check, PASSPHRASE, TEST_MEMORY_KB
    )

    # 2. Unregistered (retired) device record -> skipped with zero degradation
    devices.publish_record(backend, "dev-retired", id_retired, PASSPHRASE, TEST_MEMORY_KB)
    retired_bad = syncthing_record_key("dev-retired-bad")
    backend.put(retired_bad, b"unreadable retired record")

    # 3. Tampered record (GCM tag failure)
    devices.publish_record(backend, "dev-tampered", id_valid, PASSPHRASE, TEST_MEMORY_KB)
    tampered_bytes = bytearray(backend.get(syncthing_record_key("dev-tampered")))
    tampered_bytes[-1] ^= 0xFF
    backend.put(syncthing_record_key("dev-tampered"), bytes(tampered_bytes))

    # 4. Newer-format record (version byte 0x03)
    valid_cipher = backend.get(syncthing_record_key("dev-valid"))
    backend.put(syncthing_record_key("dev-newer"), b"\x03" + valid_cipher[1:])

    # 5. Oversized record (> 4 KiB) skipped before reading
    backend.put(
        syncthing_record_key("dev-oversized"),
        b"x" * (devices.MAX_SYNCTHING_RECORD_BYTES + 1),
    )

    # 6. Malformed JSON payload
    backend.put(
        syncthing_record_key("dev-malformed"),
        crypto.encrypt(b"not-json", PASSPHRASE, TEST_MEMORY_KB),
    )

    # 7. Bad Luhn-32 check digit inside encrypted payload
    bad_check_payload = json.dumps(
        {"mm_device_id": "dev-badcheck", "syncthing_device_id": id_bad_check}
    ).encode("utf-8")
    backend.put(
        syncthing_record_key("dev-badcheck"),
        crypto.encrypt(bad_check_payload, PASSPHRASE, TEST_MEMORY_KB),
    )

    # 8. Valid record of dev-valid copied under dev-copied's filename
    backend.put(syncthing_record_key("dev-copied"), valid_cipher)

    # 9. Conflict copy of a record -> ignored without degradation
    backend.put(
        "syncthing/dev-valid.sync-conflict-20261119-120000-AAAAAAA.enc",
        b"corrupt-conflict-copy",
    )

    # 10. Vanished record mid-read -> ignored without degradation
    devices.publish_record(backend, "dev-vanished", id_copied, PASSPHRASE, TEST_MEMORY_KB)
    real_get = backend.get
    get_calls: list[str] = []

    def spy_get(key: str, *, expected: bool = False) -> bytes:
        get_calls.append(key)
        if key == syncthing_record_key("dev-vanished"):
            raise StorageNotFound(key, vanished=True)
        return real_get(key, expected=expected)

    monkeypatch.setattr(backend, "get", spy_get)

    records = devices.read_fleet_records(
        backend, PASSPHRASE, TEST_MEMORY_KB, own_device_id="dev-valid"
    )
    assert records.vouched == {id_valid: ("dev-valid", "mac-b")}
    assert records.by_mm_id == {"dev-valid": id_valid}
    assert records.own_syncthing_id == id_valid
    assert records.newer_format == ["dev-newer"]
    assert sorted(records.unreadable) == [
        "dev-badcheck",
        "dev-copied",
        "dev-malformed",
        "dev-oversized",
        "dev-tampered",
    ]
    assert set(records.skipped_unregistered) == {"dev-retired", "dev-retired-bad"}
    assert records.vanished == ["dev-vanished"]
    # Oversized file was rejected by stat() before backend.get()
    assert syncthing_record_key("dev-oversized") not in get_calls
    assert retired_bad not in get_calls


def test_push_publishes_missing_and_stale_skips_equal_and_autopush_no_change_publishes(
    fake_syncthing, tmp_path, monkeypatch
):
    storage_dir, backend, my_id = _setup_fleet(
        fake_syncthing, tmp_path, monkeypatch, managed=True, publish_own=False
    )
    rec_key = syncthing_record_key("dev-a")
    dev_json_before = backend.get(device_key("dev-a"))
    assert not backend.exists(rec_key)

    # 1. Dry-run writes nothing and sends no Syncthing request
    res_dry = runner.invoke(app, ["push", "--dry-run"])
    assert res_dry.exit_code == 0, res_dry.output
    assert not backend.exists(rec_key)
    assert fake_syncthing.requests == []

    # 2. autopush with no content change publishes the missing record
    res_auto = runner.invoke(app, ["autopush"])
    assert res_auto.exit_code == 0, res_auto.output
    assert backend.exists(rec_key)
    records = devices.read_fleet_records(backend, PASSPHRASE, TEST_MEMORY_KB, own_device_id="dev-a")
    assert records.own_syncthing_id == my_id
    # devices/<id>.json never contains the Syncthing ID
    assert my_id.encode("utf-8") not in backend.get(device_key("dev-a"))
    assert json.loads(backend.get(device_key("dev-a")).decode("utf-8"))["device_id"] == "dev-a"

    # 3. Equal record on next push is not rewritten (no churn)
    cipher_before = backend.get(rec_key)
    fake_syncthing.requests.clear()
    res_eq = runner.invoke(app, ["push"])
    assert res_eq.exit_code == 0, res_eq.output
    assert backend.get(rec_key) == cipher_before
    # With no other vouched device and an equal record, zero Syncthing requests are sent
    assert fake_syncthing.requests == []

    # 4. Stale record is rewritten on next push
    stale_id = _valid_syncthing_id("Z")
    devices.publish_record(backend, "dev-a", stale_id, PASSPHRASE, TEST_MEMORY_KB)
    assert backend.get(rec_key) != cipher_before
    res_stale = runner.invoke(app, ["push"])
    assert res_stale.exit_code == 0, res_stale.output
    records_after = devices.read_fleet_records(
        backend, PASSPHRASE, TEST_MEMORY_KB, own_device_id="dev-a"
    )
    assert records_after.own_syncthing_id == my_id
    assert set(json.loads(dev_json_before.decode("utf-8"))) == {
        "device_id",
        "device_name",
        "registered",
    }

    # A new local certificate changes the identity even before peers publish.
    make_syncthing_like_certificate(fake_syncthing.config_dir)
    new_id = syncthing.local_device_id_offline()
    assert new_id is not None and new_id != my_id
    fake_syncthing.my_id = new_id
    fake_syncthing.set_folder(storage_dir, peers=[])
    refreshed = runner.invoke(app, ["push"])
    assert refreshed.exit_code == 0, refreshed.output
    records = devices.read_fleet_records(backend, PASSPHRASE, TEST_MEMORY_KB, own_device_id="dev-a")
    assert records.own_syncthing_id == new_id
    assert fake_syncthing.bodies == []


def test_owner_managed_machine_sends_zero_writes_and_zero_requests_when_record_is_current(
    fake_syncthing, tmp_path, monkeypatch
):
    storage_dir, backend, _ = _setup_fleet(
        fake_syncthing, tmp_path, monkeypatch, managed=False, publish_own=False
    )
    make_syncthing_like_certificate(fake_syncthing.config_dir)
    cert_id = syncthing.local_device_id_offline()
    assert cert_id is not None
    fake_syncthing.my_id = cert_id
    fake_syncthing.set_folder(storage_dir, peers=[])
    devices.publish_record(backend, "dev-a", cert_id, PASSPHRASE, TEST_MEMORY_KB)

    peer_id = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", peer_id, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {peer_id: {"name": "mac-b"}}
    fake_syncthing.requests.clear()
    fake_syncthing.bodies.clear()

    for cmd in ("push", "pull", "autopush", "autopull"):
        res = runner.invoke(app, [cmd])
        assert res.exit_code == 0, res.output
        assert fake_syncthing.requests == [], f"{cmd} sent unexpected requests"
        assert fake_syncthing.bodies == []


def test_publish_check_skips_no_syncthing_and_records_unauthorized_or_write_failure(
    fake_syncthing, tmp_path, monkeypatch
):
    _storage_dir, backend, _my_id = _setup_fleet(
        fake_syncthing, tmp_path, monkeypatch, managed=False, publish_own=False
    )

    # 1. Syncthing not running -> silent and not degraded by Syncthing
    fake_syncthing.stopped = True
    res_down = runner.invoke(app, ["push"])
    assert res_down.exit_code == 0, res_down.output
    assert "Syncthing" not in res_down.output
    runs_down = attemptlog.read_runs()["runs"]["push"]
    assert not any("Syncthing" in d for d in runs_down["degradations"])

    # 2. Missing record + Syncthing answers unauthorized -> notice + degraded detail
    fake_syncthing.start()
    fake_syncthing.responses["/rest/system/status"] = 403
    res_unauth = runner.invoke(app, ["push"])
    assert res_unauth.exit_code == 0, res_unauth.output
    assert (
        "mm: notice: Syncthing ID not published (unauthorized). Run: mm diag" in res_unauth.output
    )
    runs_unauth = attemptlog.read_runs()["runs"]["push"]
    assert runs_unauth["outcome"] == "degraded"
    assert "Syncthing ID not published (unauthorized). Run: mm diag" in runs_unauth["degradations"]

    # Hook version of unauthorized publish check is silent on stderr and records degraded
    res_auto_unauth = runner.invoke(app, ["autopush"])
    assert res_auto_unauth.exit_code == 0
    assert res_auto_unauth.output == ""
    crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["push"]
    assert crumb["outcome"] == "degraded"
    assert "Syncthing ID not published (unauthorized). Run: mm diag" in crumb["detail"]

    # A malformed live identity is a bad response even when HTTP succeeded.
    fake_syncthing.responses["/rest/system/status"] = {"myID": "invalid"}
    res_bad = runner.invoke(app, ["push"])
    assert res_bad.exit_code == 0, res_bad.output
    assert "Syncthing ID not published (bad-response). Run: mm diag" in res_bad.output
    assert not backend.exists(syncthing_record_key("dev-a"))

    # 3. Publish write failure on push -> notice + degraded detail, exit 0
    fake_syncthing.responses["/rest/system/status"] = {"myID": fake_syncthing.my_id}

    def fail_put(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left")

    monkeypatch.setattr(devices, "publish_record", fail_put)
    res_fail = runner.invoke(app, ["push"])
    assert res_fail.exit_code == 0, res_fail.output
    assert (
        "mm: notice: could not publish this machine's Syncthing ID (ENOSPC); the next push retries"
    ) in res_fail.output
    runs_fail = attemptlog.read_runs()["runs"]["push"]
    assert (
        "could not publish this machine's Syncthing ID (ENOSPC); the next push retries"
        in runs_fail["degradations"]
    )


def test_mesh_pass_steady_state_request_ceiling_and_unvouched_or_own_or_paused_skipped(
    fake_syncthing, tmp_path, monkeypatch
):
    storage_dir, backend, my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    vouched_shared = _valid_syncthing_id("B")
    vouched_paused = _valid_syncthing_id("C")
    unvouched_pending = _valid_syncthing_id("D")
    tampered_pending = _valid_syncthing_id("E")
    retired_pending = _valid_syncthing_id("F")

    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", vouched_shared, PASSPHRASE, TEST_MEMORY_KB)
    register_device(backend, "dev-c", "mac-c")
    devices.publish_record(backend, "dev-c", vouched_paused, PASSPHRASE, TEST_MEMORY_KB)
    register_device(backend, "dev-e", "mac-e")
    devices.publish_record(backend, "dev-e", tampered_pending, PASSPHRASE, TEST_MEMORY_KB)
    bad_bytes = bytearray(backend.get(syncthing_record_key("dev-e")))
    bad_bytes[-1] ^= 0xFF
    backend.put(syncthing_record_key("dev-e"), bytes(bad_bytes))
    # Unregistered device record
    devices.publish_record(backend, "dev-retired", retired_pending, PASSPHRASE, TEST_MEMORY_KB)

    # Configure vouched_shared as already shared, and vouched_paused as paused & unshared
    ok_folder = {
        **fake_syncthing.stock_folder(storage_dir, shared_with=[vouched_shared]),
        "id": "mind-meld",
    }
    fake_syncthing.set_config(
        devices=[
            {
                "deviceID": vouched_shared,
                "name": "mac-b",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            },
            {
                "deviceID": vouched_paused,
                "name": "mac-c",
                "addresses": ["dynamic"],
                "introducer": False,
                "autoAcceptFolders": False,
                "paused": True,
            },
        ],
        folders=[ok_folder],
    )
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        my_id: {"name": "self"},
        vouched_paused: {"name": "stale pending entry"},
        unvouched_pending: {"name": "stranger"},
        tampered_pending: {"name": "tampered"},
        retired_pending: {"name": "retired"},
    }
    fake_syncthing.requests.clear()
    fake_syncthing.bodies.clear()

    res = runner.invoke(app, ["autopull"])
    assert res.exit_code == 0
    assert res.output == ""
    # Steady state: exactly 4 GETs (mesh_snapshot) and 0 non-GET writes
    assert [r[0] for r in fake_syncthing.requests] == ["GET", "GET", "GET", "GET"]
    assert fake_syncthing.bodies == []


# Value: protects=invalid folder options prevent every mesh write even at the correct path;
# fails_when=the snapshot lets add and introducer writes run before the share guard;
# why_new=the existing wrong-path case cannot catch an omitted option check; seam=none;
# no_claim=option enforcement by real Syncthing (loopback stand-in).
def test_mesh_pass_folder_binding_and_managed_id_mismatch_gates(
    fake_syncthing, tmp_path, monkeypatch
):
    storage_dir, backend, _my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    vouched_id = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", vouched_id, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {vouched_id: {"name": "mac-b"}}

    # 1. Folder path != active storage root -> zero non-GET, not-mm-folder degradation
    wrong_folder = {
        **fake_syncthing.stock_folder(tmp_path / "wrong-path"),
        "id": "mind-meld",
    }
    fake_syncthing.set_config(devices=[], folders=[wrong_folder])
    fake_syncthing.bodies.clear()
    res_path = runner.invoke(app, ["pull"])
    assert res_path.exit_code == 0, res_path.output
    assert fake_syncthing.bodies == []
    assert (
        "mm: notice: Syncthing mesh step stopped (not-mm-folder). Run: mm diag" in res_path.output
    )

    # 2. Invalid options at the exact storage root still prevent the first write.
    fake_syncthing.set_folder(storage_dir, peers=[], order="random")
    res_options = runner.invoke(app, ["pull"])
    assert res_options.exit_code == 0, res_options.output
    assert fake_syncthing.bodies == []
    assert "mm: notice: Syncthing mesh step stopped (not-mm-folder). Run: mm diag" in (
        res_options.stderr
    )

    # 3. Missing mind-meld folder -> zero non-GET, no-folder degradation
    fake_syncthing.set_config(devices=[], folders=[])
    res_nofld = runner.invoke(app, ["pull"])
    assert res_nofld.exit_code == 0, res_nofld.output
    assert fake_syncthing.bodies == []
    assert "mm: notice: Syncthing mesh step stopped (no-folder). Run: mm diag" in res_nofld.output

    # 4. Managed ID in syncthing.toml != live REST myID -> zero writes, not-managed
    fake_syncthing.set_folder(storage_dir, peers=[])
    config.save_syncthing_record(_valid_syncthing_id("F"))
    fake_syncthing.requests.clear()
    fake_syncthing.bodies.clear()
    res_mismatch = meshflow.run(
        config.load_config(),
        PASSPHRASE,
        TEST_MEMORY_KB,
        verb="pull",
        attended=False,
        backend=backend,
    )
    assert res_mismatch.stop_cause == "not-managed"
    assert fake_syncthing.bodies == []
    assert [(r[0], r[1]) for r in fake_syncthing.requests] == [("GET", "/rest/system/status")]


def test_mesh_rechecks_folder_path_when_sharing(fake_syncthing, tmp_path, monkeypatch):
    _, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", peer, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {peer: {}}

    def move_folder_after_add(method, path, body):
        if method == "POST" and path == "/rest/config/devices":
            fake_syncthing.responses["/rest/config/folders"][0]["path"] = str(
                tmp_path / "another-folder"
            )

    fake_syncthing.on_write = move_folder_after_add
    res = runner.invoke(app, ["pull"])
    assert res.exit_code == 0, res.output
    assert "Syncthing mesh step stopped (not-mm-folder). Run: mm diag" in res.output
    assert [path for _, path, _ in fake_syncthing.bodies] == [
        "/rest/config/devices",
        f"/rest/config/devices/{peer}",
    ]


# Value: protects=a blocked REST publication ends the hook quietly before the mesh snapshot;
# fails_when=the request cap is lost, another REST request starts, or evidence omits the timeout;
# why_new=the synthetic timeout exercised classification without blocking transport; seam=none;
# no_claim=latency on a host with real Syncthing (loopback stand-in).
def test_publish_timeout_stops_before_mesh_snapshot(fake_syncthing, tmp_path, monkeypatch):
    _, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch, publish_own=False)
    peer = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", peer, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {peer: {}}
    status_route = "/rest/system/status"
    release = fake_syncthing.hold(status_route)
    started = time.monotonic()
    try:
        res = runner.invoke(app, ["autopush"])
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert res.exit_code == 0
    assert res.output == ""
    # gstack-timing-ok: A held loopback response exercises the hook's real deadline.
    assert elapsed < 1.8, elapsed
    requests = [(method, path) for method, path, _ in fake_syncthing.requests]
    assert requests == [("GET", status_route)]
    assert fake_syncthing.bodies == []
    crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["push"]
    assert crumb["outcome"] == "degraded"
    assert "Syncthing mesh step stopped (timeout)" in crumb["detail"]


def test_no_sources_autopush_retains_mesh_result(fake_syncthing, tmp_path, monkeypatch):
    _, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    cfg = config.load_config()
    cfg["sync"]["sources"] = []
    config.save_config(cfg)
    peer = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", peer, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {peer: {}}

    res = runner.invoke(app, ["autopush"])
    assert res.exit_code == 0
    crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["push"]
    assert crumb["outcome"] == "no-sources"
    assert "Syncthing now shares the fleet folder with 1 fleet machine" in crumb["detail"]


def test_breadcrumb_uses_only_remaining_space_for_complete_mesh_details():
    detail = "Syncthing mesh step stopped (unauthorized). Run: mm diag"
    result = meshflow.MeshResult(degradations=[detail])
    for existing in ("x" * 300, "x" * 298):
        assert result.compose_breadcrumb([existing]) == ("degraded", existing)
    existing = "x" * 280
    assert result.compose_breadcrumb([existing]) == ("degraded", f"{existing}; +1 more")
    assert result.compose_breadcrumb(()) == ("degraded", detail)


@pytest.mark.parametrize("lost_step", [0, 1, 2], ids=["add", "introducer", "share"])
def test_mesh_pass_resume_lost_response_late_record_and_outage_convergence(
    fake_syncthing, tmp_path, monkeypatch, lost_step
):
    storage_dir, backend, my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer_b = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")

    # 1. Late-arriving record: pending device present before record arrives -> 0 requests
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        peer_b: {"name": "hostile-\x1b[31m-name"}
    }
    res1 = runner.invoke(app, ["autopull"])
    assert res1.exit_code == 0
    assert fake_syncthing.bodies == []

    # Record arrives, but Syncthing is down (outage)
    devices.publish_record(backend, "dev-b", peer_b, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.stopped = True
    res_outage = runner.invoke(app, ["autopull"])
    assert res_outage.exit_code == 0
    assert res_outage.output == ""
    crumb_outage = json.loads(cli.core._autorun_breadcrumb_path().read_text())["pull"]
    assert crumb_outage["outcome"] == "degraded"
    assert (
        "Syncthing mesh step stopped (unreachable) after 0 of 1 steps; "
        "nothing else changed; retries on the next push or pull"
    ) in crumb_outage["detail"]

    # 2. One write applies, but its response is lost.
    fake_syncthing.start()
    real_send = syncthing._send_json
    writes = [
        ("POST", "/rest/config/devices"),
        ("PATCH", f"/rest/config/devices/{peer_b}"),
        ("PATCH", "/rest/config/folders/mind-meld"),
    ]
    step_names = ("adding device", "marking as introducer", "sharing the fleet folder")

    def lose_response(endpoint, method, path, body, **kwargs):
        outcome, cause = real_send(endpoint, method, path, body, **kwargs)
        if (method, path) == writes[lost_step]:
            assert outcome == "ok"
            return "unreachable", "timeout"
        return outcome, cause

    monkeypatch.setattr(syncthing, "_send_json", lose_response)
    res_partial = runner.invoke(app, ["autopull"])
    assert res_partial.exit_code == 0
    assert res_partial.output == ""
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == writes[: lost_step + 1]
    crumb_partial = json.loads(cli.core._autorun_breadcrumb_path().read_text())["pull"]
    assert crumb_partial["outcome"] == "degraded"
    assert (
        f"Syncthing did not confirm {step_names[lost_step]} for mac-b ({peer_b[:7]}); "
        "it may have applied; the next push or pull reconciles it"
    ) in crumb_partial["detail"]
    assert "a write may have applied" in crumb_partial["detail"]

    # 3. The next run does only the steps still needed.
    fake_syncthing.bodies.clear()
    res_healed = runner.invoke(app, ["pull"])
    assert res_healed.exit_code == 0, res_healed.output
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == writes[lost_step + 1 :]
    folder = fake_syncthing.responses["/rest/config/folders"][0]
    assert peer_b in {device["deviceID"] for device in folder["devices"]}
    shared = "Pull: Syncthing now shares the fleet folder with 1 fleet machine"
    assert (shared in res_healed.output) == (lost_step < len(writes) - 1)
    assert "hostile-" not in res_healed.output


def test_mesh_pass_deadline_stops_and_repeated_slow_passes_converge(
    fake_syncthing, tmp_path, monkeypatch
):
    _storage_dir, backend, _my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer_b = _valid_syncthing_id("B")
    peer_c = _valid_syncthing_id("C")
    register_device(backend, "dev-b", "mac-b")
    register_device(backend, "dev-c", "linux-c")
    devices.publish_record(backend, "dev-b", peer_b, PASSPHRASE, TEST_MEMORY_KB)
    devices.publish_record(backend, "dev-c", peer_c, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        peer_b: {"name": "mac-b"},
        peer_c: {"name": "linux-c"},
    }

    # Each HTTP request advances the fake monotonic clock by 0.18 s.
    # Under the 2.0 s hook budget (~11 requests per pass), each pass makes
    # partial progress and stops at the deadline; repeated passes converge.
    clock = [1000.0]

    def fake_monotonic() -> float:
        return clock[0]

    def advance_on_request():
        clock[0] += 0.18

    real_get_json = syncthing._get_json
    real_send_json = syncthing._send_json

    def slow_get_json(*args, **kwargs):
        advance_on_request()
        return real_get_json(*args, **kwargs)

    def slow_send_json(*args, **kwargs):
        advance_on_request()
        return real_send_json(*args, **kwargs)

    monkeypatch.setattr(syncthing.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(syncthing, "_get_json", slow_get_json)
    monkeypatch.setattr(syncthing, "_send_json", slow_send_json)
    cfg = config.load_config()
    pass1 = meshflow.run(
        cfg, PASSPHRASE, TEST_MEMORY_KB, verb="pull", attended=False, backend=backend
    )
    assert pass1.stop_cause == "timeout"
    assert any("Syncthing mesh step stopped (timeout)" in d for d in pass1.degradations)

    # Run further bounded passes until both peers share the folder
    for _ in range(5):
        meshflow.run(cfg, PASSPHRASE, TEST_MEMORY_KB, verb="pull", attended=False, backend=backend)
        folder_devs = {
            d["deviceID"] for d in fake_syncthing.responses["/rest/config/folders"][0]["devices"]
        }
        if {peer_b, peer_c}.issubset(folder_devs):
            break
    folder_devs = {
        d["deviceID"] for d in fake_syncthing.responses["/rest/config/folders"][0]["devices"]
    }
    assert {peer_b, peer_c}.issubset(folder_devs)


def test_attended_pull_pending_notice_counts_only_unvouched_even_after_partial_pass(
    fake_syncthing, tmp_path, monkeypatch
):
    _storage_dir, backend, _my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    vouched_1 = _valid_syncthing_id("B")
    vouched_2 = _valid_syncthing_id("C")
    unvouched = _valid_syncthing_id("D")
    register_device(backend, "dev-b", "mac-b")
    register_device(backend, "dev-c", "linux-c")
    devices.publish_record(backend, "dev-b", vouched_1, PASSPHRASE, TEST_MEMORY_KB)
    devices.publish_record(backend, "dev-c", vouched_2, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        vouched_1: {"name": "mac-b"},
        vouched_2: {"name": "linux-c"},
        unvouched: {"name": "stranger"},
    }

    # Stop after the first write (POST /rest/config/devices for vouched_1), leaving
    # vouched_2 still pending in Syncthing; the notice must still report only 1 (unvouched).
    fake_syncthing.stop_after("POST", "/rest/config/devices")
    res = runner.invoke(app, ["pull"])
    assert res.exit_code == 0, res.output
    assert (
        "mm: notice: Syncthing: 1 device is asking to join; use the ID the new machine printed. "
        "Run: mm devices approve <device-id>"
    ) in res.output


# Value: protects=managed REST and config failures surface distinct remedies without any write;
# fails_when=mesh refusal drops the attended notice or silent degraded hook detail;
# why_new=the prior unauthorized CLI test covered publication on an owner-managed host; seam=none;
# no_claim=live Syncthing configuration (loopback stand-in and malformed test config).
def test_dx4_cause_table_rows_status_rendering_and_unexpected_exception(
    fake_syncthing, tmp_path, monkeypatch
):
    _storage_dir, backend, _my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer_b = _valid_syncthing_id("B")
    peer_c = _valid_syncthing_id("C")
    register_device(backend, "dev-b", "mac-b")
    register_device(backend, "dev-c", "linux-c")
    devices.publish_record(backend, "dev-b", peer_b, PASSPHRASE, TEST_MEMORY_KB)
    devices.publish_record(backend, "dev-c", peer_c, PASSPHRASE, TEST_MEMORY_KB)

    # 1. Hook adds peer_b and hits an unconfirmed write on peer_c -> both share and
    # degradation appear in rendered `mm status`, with degradation first
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        peer_b: {"name": "mac-b"},
        peer_c: {"name": "linux-c"},
    }
    for cause in ("unauthorized", "config-unreadable"):
        if cause == "unauthorized":
            fake_syncthing.responses["/rest/system/status"] = 403
        else:
            fake_syncthing.responses["/rest/system/status"] = {"myID": fake_syncthing.my_id}
            fake_syncthing.config_path.write_text("<configuration>", encoding="utf-8")

        detail = f"Syncthing mesh step stopped ({cause}). Run: mm diag"
        attended = runner.invoke(app, ["pull"])
        assert attended.exit_code == 0, attended.output
        assert f"mm: notice: {detail}" in attended.stderr.splitlines()
        attended_run = attemptlog.read_runs()["runs"]["pull"]
        assert attended_run["outcome"] == "degraded"
        assert detail in attended_run["degradations"]

        hook = runner.invoke(app, ["autopull"])
        assert hook.exit_code == 0, hook.output
        assert hook.output == ""
        refusal_crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["pull"]
        assert refusal_crumb["outcome"] == "degraded"
        assert detail in refusal_crumb["detail"]
        assert fake_syncthing.bodies == []
    fake_syncthing.write_config()

    fake_syncthing.write_responses[("PATCH", f"/rest/config/devices/{peer_c}")] = 500
    res_hook = runner.invoke(app, ["autopull"])
    assert res_hook.exit_code == 0
    assert res_hook.output == ""
    crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["pull"]
    assert crumb["outcome"] == "degraded"
    assert len(crumb["detail"]) <= 300
    deg_idx = crumb["detail"].index("Syncthing did not confirm marking as introducer")
    share_idx = crumb["detail"].index("Syncthing now shares the fleet folder with 1 fleet machine")
    assert deg_idx < share_idx

    res_status = runner.invoke(app, ["status"])
    assert res_status.exit_code == 0, res_status.output
    flat_status = " ".join(res_status.output.split())
    assert "Syncthing did not confirm marking as introducer" in flat_status
    assert "Syncthing now shares the fleet folder with 1 fleet machine" in flat_status

    # 2. Hook that finishes peer_c without degradation -> status shows success detail
    fake_syncthing.write_responses.clear()
    res_hook_ok = runner.invoke(app, ["autopull"])
    assert res_hook_ok.exit_code == 0
    assert res_hook_ok.output == ""
    res_status_ok = runner.invoke(app, ["status"])
    assert res_status_ok.exit_code == 0, res_status_ok.output
    flat_status_ok = " ".join(res_status_ok.output.split())
    assert (
        f"Syncthing now shares the fleet folder with 1 fleet machine: linux-c ({peer_c[:7]})"
        in flat_status_ok
    )

    # 3. Newer-format and unreadable records -> attended notice + rendered status
    valid_cipher = backend.get(syncthing_record_key("dev-b"))
    register_device(backend, "dev-newer", "mac-newer")
    register_device(backend, "dev-bad", "mac-bad")
    backend.put(syncthing_record_key("dev-newer"), b"\x03" + valid_cipher[1:])
    backend.put(syncthing_record_key("dev-bad"), b"corrupt-bytes")

    res_pull_deg = runner.invoke(app, ["pull"])
    assert res_pull_deg.exit_code == 0, res_pull_deg.output
    assert (
        "mm: notice: the Syncthing record of device dev-newer is from a newer mm. Run: mm update"
        in res_pull_deg.output
    )
    assert (
        "mm: notice: the Syncthing record of device dev-bad is unreadable. Run: mm diag"
        in res_pull_deg.output
    )
    res_status_att = runner.invoke(app, ["status"])
    assert res_status_att.exit_code == 0, res_status_att.output
    flat_status_att = " ".join(res_status_att.output.split())
    assert (
        "the Syncthing record of device dev-newer is from a newer mm. Run: mm update"
        in flat_status_att
    )
    assert "the Syncthing record of device dev-bad is unreadable. Run: mm diag" in flat_status_att
    backend.delete(syncthing_record_key("dev-newer"))
    backend.delete(syncthing_record_key("dev-bad"))

    # 4. Unexpected exception in pass -> hook logs to autopush.log, attended prints notice + trace
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic mesh crash")

    monkeypatch.setattr(devices, "read_fleet_records", boom)
    res_hook_exc = runner.invoke(app, ["autopush"])
    assert res_hook_exc.exit_code == 0
    assert res_hook_exc.output == ""
    log_file = sidecar.SIDECAR_DIR / "autopush.log"
    assert log_file.is_file()
    assert "RuntimeError: synthetic mesh crash" in log_file.read_text(encoding="utf-8")
    crumb_exc = json.loads(cli.core._autorun_breadcrumb_path().read_text())["push"]
    assert crumb_exc["outcome"] == "degraded"
    assert "Syncthing mesh step failed (RuntimeError, see autopush.log)" in crumb_exc["detail"]

    res_att_exc = runner.invoke(app, ["push", "--verbose"])
    assert res_att_exc.exit_code == 0, res_att_exc.output
    assert (
        "mm: notice: Syncthing mesh step failed (RuntimeError). Run: mm diag" in res_att_exc.output
    )
    assert "RuntimeError (no errno):" in res_att_exc.output


def test_core_sync_duration_does_not_eat_mesh_deadline_and_recapture_or_failed_core_skips_mesh(
    fake_syncthing, tmp_path, monkeypatch
):
    _storage_dir, backend, _my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer_b = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", peer_b, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {peer_b: {"name": "mac-b"}}

    # 1. Core sync advances monotonic clock by 30 s (well over the 10 s pass budget);
    # the mesh pass starts its deadline AFTER _push_core returns and still succeeds.
    clock = [500.0]
    monkeypatch.setattr(syncthing.time, "monotonic", lambda: clock[0])
    real_push_core = cli._push_core

    def slow_push_core(*args, **kwargs):
        clock[0] += 30.0
        return real_push_core(*args, **kwargs)

    monkeypatch.setattr(cli, "_push_core", slow_push_core)
    res_push = runner.invoke(app, ["push"])
    assert res_push.exit_code == 0, res_push.output
    assert (
        f"Push: Syncthing now shares the fleet folder with 1 fleet machine: mac-b ({peer_b[:7]})."
        in res_push.output
    )

    # 2. When core sync raises SnapshotError, the mesh pass does not run
    fake_syncthing.requests.clear()
    fake_syncthing.bodies.clear()

    def failing_push_core(*args, **kwargs):
        raise StorageError("synthetic core failure")

    monkeypatch.setattr(cli, "_push_core", failing_push_core)
    res_fail = runner.invoke(app, ["push"])
    assert res_fail.exit_code == 1
    assert fake_syncthing.requests == []
    assert fake_syncthing.bodies == []

    # 3. recapture neither publishes nor runs the mesh pass
    import subprocess

    repo_dir = tmp_path / "recap-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    (repo_dir / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "f.txt"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=user@example.com",
            "commit",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    cfg = config.load_config()
    cfg["retro"] = {"repo_roots": [str(repo_dir)]}
    config.save_config(cfg)

    monkeypatch.setattr(cli, "_push_core", real_push_core)
    backend.delete(syncthing_record_key("dev-a"))
    fake_syncthing.requests.clear()
    fake_syncthing.bodies.clear()
    res_recap = runner.invoke(app, ["recapture", "7d"])
    assert res_recap.exit_code == 0, res_recap.output
    assert not backend.exists(syncthing_record_key("dev-a"))
    assert fake_syncthing.requests == []
    assert fake_syncthing.bodies == []


def test_mesh_pass_reports_unpublished_names_and_vouched_ids_excluding_self_and_unreadable(
    fake_syncthing, tmp_path, monkeypatch
):
    _, backend, my_id = _setup_fleet(
        fake_syncthing,
        tmp_path,
        monkeypatch,
        publish_own=False,
    )
    peer_published = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", peer_published, PASSPHRASE, TEST_MEMORY_KB)

    hostile_unpublished = "linux-c\x1b]52;c;ZHVtbXk=\x07-[bold]tag[/bold]"
    register_device(backend, "dev-c", hostile_unpublished)

    register_device(backend, "dev-d", "unreadable-d")
    backend.put(syncthing_record_key("dev-d"), b"not-valid-ciphertext")

    cfg = config.load_config()
    mesh_res = meshflow.run(
        cfg,
        PASSPHRASE,
        TEST_MEMORY_KB,
        verb="pull",
        attended=True,
        backend=backend,
    )

    assert mesh_res.vouched_ids == frozenset({peer_published})
    assert len(mesh_res.unpublished_names) == 1
    only_name = mesh_res.unpublished_names[0]
    assert "linux-c" in only_name
    assert "\x1b" not in only_name
    assert "mac-a" not in mesh_res.unpublished_names
    assert "mac-b" not in mesh_res.unpublished_names
    assert "unreadable-d" not in mesh_res.unpublished_names


@pytest.mark.parametrize("attended", [False, True])
def test_mesh_pairs_vouched_peer_with_one_registry_read(
    fake_syncthing, tmp_path, monkeypatch, attended
):
    _, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    assert devices.publish_record(backend, "dev-b", peer, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {peer: {"name": "mac-b"}}

    inventory_devices = devices.inventory_devices
    budget = meshflow.ATTENDED_PASS_BUDGET_S if attended else meshflow.HOOK_PASS_BUDGET_S
    clock = [1000.0]
    reads = 0

    def read_inventory(backend):
        nonlocal reads
        reads += 1
        if reads == 1:
            clock[0] += budget - 0.5
            return inventory_devices(backend)
        clock[0] += 0.5
        raise StorageError("registry unavailable during second read")

    monkeypatch.setattr(meshflow.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(devices, "inventory_devices", read_inventory)
    result = meshflow.run(
        config.load_config(),
        PASSPHRASE,
        TEST_MEMORY_KB,
        verb="pull",
        attended=attended,
        backend=backend,
    )

    assert result.stop_cause is None
    assert result.shared == [("mac-b", peer)]
    assert reads == 1
