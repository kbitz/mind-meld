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
    fake_syncthing.bodies.clear()
    res_eq = runner.invoke(app, ["push"])
    assert res_eq.exit_code == 0, res_eq.output
    assert backend.get(rec_key) == cipher_before
    # Managed pass runs GET snapshot even without other vouched peers, sending zero writes
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []

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


def test_owner_managed_machine_only_probes_folder_delivery_on_pull(
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
        fake_syncthing.requests.clear()
        fake_syncthing.bodies.clear()
        res = runner.invoke(app, [cmd])
        assert res.exit_code == 0, res.output
        if cmd in ("pull", "autopull"):
            # Track 158A: pull and autopull probe folder delivery read-only
            # even on owner-managed machines, never querying pending devices.
            assert all(req[0] == "GET" for req in fake_syncthing.requests), (
                f"{cmd} sent non-GET requests: {fake_syncthing.requests}"
            )
            assert "/rest/cluster/pending/devices" not in [
                req[1] for req in fake_syncthing.requests
            ]
            assert (
                sum(req[1].startswith("/rest/db/status?") for req in fake_syncthing.requests) == 1
            )
        else:
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
                "introducer": False,
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
    # Steady state: 5 GETs for Track 158A's pre-pull delivery probe + 4 GETs
    # for mesh_snapshot, and 0 non-GET writes.
    assert [r[0] for r in fake_syncthing.requests] == ["GET"] * 9
    assert fake_syncthing.bodies == []


# Value: protects=invalid folder options prevent every mesh write even at the correct path;
# fails_when=the snapshot lets add writes run before the share guard;
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


@pytest.mark.parametrize("lost_step", [0, 1], ids=["add", "share"])
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
        ("PATCH", "/rest/config/folders/mind-meld"),
    ]
    step_names = ("adding device", "sharing the fleet folder")

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
        "mm: notice: Syncthing: 1 device is asking to join. Run: mm devices approve"
    ) in res.output


# Value: protects=managed REST and config failures surface distinct remedies without any write;
# fails_when=mesh refusal drops the attended notice or silent degraded hook detail;
# why_new=the prior unauthorized CLI test covered publication on an owner-managed host; seam=none;
# no_claim=live Syncthing configuration (loopback stand-in and malformed test config).
def test_dx4_cause_table_rows_status_rendering_and_unexpected_exception(
    fake_syncthing, tmp_path, monkeypatch
):
    storage_dir, backend, _my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer_b = _valid_syncthing_id("B")
    peer_c = _valid_syncthing_id("C")
    register_device(backend, "dev-b", "mac-b")
    register_device(backend, "dev-c", "linux-c")
    devices.publish_record(backend, "dev-b", peer_b, PASSPHRASE, TEST_MEMORY_KB)
    devices.publish_record(backend, "dev-c", peer_c, PASSPHRASE, TEST_MEMORY_KB)

    # 1. Hook adds peer_b and hits an unconfirmed clear write on shared peer_c -> both
    # share and degradation appear in rendered `mm status`, with degradation first
    ok_folder = {
        **fake_syncthing.stock_folder(storage_dir, shared_with=[peer_c]),
        "id": "mind-meld",
    }
    fake_syncthing.set_config(
        devices=[
            {
                "deviceID": peer_c,
                "name": "linux-c",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            }
        ],
        folders=[ok_folder],
    )
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        peer_b: {"name": "mac-b"},
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
    deg_idx = crumb["detail"].index("Syncthing did not confirm clearing the introducer flag")
    share_idx = crumb["detail"].index("Syncthing now shares the fleet folder with 1 fleet machine")
    assert deg_idx < share_idx

    res_status = runner.invoke(app, ["status"])
    assert res_status.exit_code == 0, res_status.output
    flat_status = " ".join(res_status.output.split())
    assert "Syncthing did not confirm clearing the introducer flag" in flat_status
    assert "Syncthing now shares the fleet folder with 1 fleet machine" in flat_status

    # 2. Hook that finishes clearing peer_c without degradation -> status shows success detail
    fake_syncthing.write_responses.clear()
    res_hook_ok = runner.invoke(app, ["autopull"])
    assert res_hook_ok.exit_code == 0
    assert res_hook_ok.output == ""
    res_status_ok = runner.invoke(app, ["status"])
    assert res_status_ok.exit_code == 0, res_status_ok.output
    flat_status_ok = " ".join(res_status_ok.output.split())
    assert f"Syncthing cleared the introducer flag on linux-c ({peer_c[:7]})" in flat_status_ok

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


def test_mesh_pass_retirement_removals_holds_introducer_and_device_renames(
    fake_syncthing, tmp_path, monkeypatch
):
    """Track 154A AC9, AC10, C7, C8, C9, C24, C27, C30, E2, E3, E7, E8, E13, E16."""
    from mind_meld.storage.keys import retired_record_key

    storage_dir, backend, my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    ret_st_id = _valid_syncthing_id("R")
    peer_b_st_id = _valid_syncthing_id("B")

    # Configure ret_st_id in local Syncthing (disconnected, sharing mind-meld)
    # and set own Syncthing name to stale "old-mac-a" while config.toml has "mac-a"
    ok_folder = {
        **fake_syncthing.stock_folder(storage_dir, shared_with=[ret_st_id, peer_b_st_id]),
        "id": "mind-meld",
    }
    fake_syncthing.set_config(
        devices=[
            {
                "deviceID": ret_st_id,
                "name": "mac-retired",
                "addresses": ["dynamic"],
                "introducer": False,
                "autoAcceptFolders": False,
                "paused": False,
            },
            {
                "deviceID": peer_b_st_id,
                "name": "",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            },
        ],
        folders=[ok_folder],
    )
    fake_syncthing.responses["/rest/config/devices"][0]["name"] = "old-mac-a"
    fake_syncthing.responses["/rest/system/connections"] = {
        "connections": {
            ret_st_id: {"connected": False},
            peer_b_st_id: {"connected": True},
        }
    }

    # Write retirement record for dev-ret -> ret_st_id
    devices.write_retirement_record(
        backend, "dev-ret", "mac-retired", ret_st_id, PASSPHRASE, TEST_MEMORY_KB
    )

    # 1. Removal hold when an unpublished live peer exists (dev-unpub has no syncthing record)
    register_device(backend, "dev-unpub", "cachyos")
    register_device(backend, "dev-b", "mac-b-renamed")
    devices.publish_record(backend, "dev-b", peer_b_st_id, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.bodies.clear()

    res_hold = runner.invoke(app, ["pull"])
    assert res_hold.exit_code == 0, res_hold.output
    assert (
        "mm: notice: Syncthing device removals wait for cachyos (dev-unpub) to publish "
        "its Syncthing ID; run 'mm update' and 'mm push' on cachyos"
    ) in res_hold.output
    # Clears and renames still run even while removals hold!
    assert (
        "PATCH",
        f"/rest/config/devices/{peer_b_st_id}",
        {"introducer": False},
    ) in fake_syncthing.bodies
    assert (
        f"Pull: Syncthing cleared the introducer flag on mac-b-renamed ({peer_b_st_id[:7]})."
        in res_hold.output
    )
    assert "Pull: Syncthing named this device mac-a" in res_hold.output
    assert "Pull: Syncthing renamed mac-b-renamed" in res_hold.output
    assert not any(m == "DELETE" for m, _, _ in fake_syncthing.bodies)

    # 2. Removal hold when a superseded unretired registration exists
    from mind_meld import core

    backend.put(
        device_key("dev-unpub"),
        json.dumps(
            {
                "device_id": "dev-unpub",
                "device_name": "mac-b-renamed",
                "registered": "2026-01-01T00:00:00+00:00",
                "last_seen": "2026-01-02T00:00:00+00:00",
                "last_seen_version": core.__version__,
            }
        ).encode("utf-8"),
    )
    backend.put(
        device_key("dev-b"),
        json.dumps(
            {
                "device_id": "dev-b",
                "device_name": "mac-b-renamed",
                "registered": "2026-02-01T00:00:00+00:00",
                "last_seen": "2026-02-02T00:00:00+00:00",
                "last_seen_version": core.__version__,
            }
        ).encode("utf-8"),
    )
    fake_syncthing.bodies.clear()
    res_sup_hold = runner.invoke(app, ["pull"])
    assert res_sup_hold.exit_code == 0, res_sup_hold.output
    assert (
        "mm: notice: Syncthing device removals wait for superseded registration "
        "mac-b-renamed (dev-unpub). Run: mm devices retire dev-unpub"
    ) in res_sup_hold.output
    assert not any(m == "DELETE" for m, _, _ in fake_syncthing.bodies)
    # Also zero rename writes on second run since names now match
    assert fake_syncthing.bodies == []

    # Remove the superseded registration so holds clear
    backend.delete(device_key("dev-unpub"))

    # 3a. Narrowed introducer rule: ret_st_id has historical introducedBy = peer_b_st_id,
    # whose introducer flag was cleared in step 1 -> removal proceeds without hold.
    fake_syncthing.responses["/rest/config/devices"][1]["introducedBy"] = peer_b_st_id
    fake_syncthing.bodies.clear()
    res_cleared_intro = runner.invoke(app, ["pull"])
    assert res_cleared_intro.exit_code == 0, res_cleared_intro.output
    assert ("DELETE", f"/rest/config/devices/{ret_st_id}", None) in fake_syncthing.bodies
    assert (
        f"Pull: Syncthing removed retired device mac-retired ({ret_st_id[:7]})."
        in res_cleared_intro.output
    )

    # 3b. Active introducer sharing another folder (not cleared by mm): hold stays in force.
    extra_peer_folder = {
        **fake_syncthing.stock_folder(tmp_path / "other-peer", shared_with=[peer_b_st_id]),
        "id": "other-peer-folder",
    }
    fake_syncthing.set_config(
        devices=[
            {
                "deviceID": ret_st_id,
                "name": "mac-retired",
                "addresses": ["dynamic"],
                "introducer": False,
                "autoAcceptFolders": False,
                "paused": False,
                "introducedBy": peer_b_st_id,
            },
            {
                "deviceID": peer_b_st_id,
                "name": "mac-b-renamed",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            },
        ],
        folders=[ok_folder, extra_peer_folder],
    )
    fake_syncthing.responses["/rest/config/devices"][0]["name"] = "mac-a"
    fake_syncthing.bodies.clear()
    res_intro = runner.invoke(app, ["pull"])
    assert res_intro.exit_code == 0, res_intro.output
    assert (
        f"mm: notice: mac-retired ({ret_st_id[:7]}) stays until mac-b-renamed drops it"
        in res_intro.output
    )
    assert fake_syncthing.bodies == []

    # Reset peer_b to non-introducer and single fleet folder for subcases 4-6.
    fake_syncthing.responses["/rest/config/devices"][1]["introducedBy"] = ""
    fake_syncthing.responses["/rest/config/devices"][2]["introducer"] = False
    fake_syncthing.responses["/rest/config/folders"] = [ok_folder]

    # 4. Connected device refusal (device-connected)
    fake_syncthing.responses["/rest/system/connections"]["connections"][ret_st_id] = {
        "connected": True
    }
    res_conn = runner.invoke(app, ["pull"])
    assert res_conn.exit_code == 0, res_conn.output
    assert (
        f"mm: notice: mac-retired ({ret_st_id[:7]}) is retired but still connected; "
        "shut it down or stop Syncthing there, or remove it in Syncthing's web UI"
    ) in res_conn.output
    assert fake_syncthing.bodies == []

    # 5. Multi-folder device refusal (shared-in-other-folder)
    fake_syncthing.responses["/rest/system/connections"]["connections"][ret_st_id] = {
        "connected": False
    }
    extra_folder = {
        **fake_syncthing.stock_folder(tmp_path / "other", shared_with=[ret_st_id]),
        "id": "other-folder",
    }
    fake_syncthing.responses["/rest/config/folders"] = [ok_folder, extra_folder]
    res_multi = runner.invoke(app, ["pull"])
    assert res_multi.exit_code == 0, res_multi.output
    assert (
        f"mm: notice: mac-retired ({ret_st_id[:7]}) is retired from Mind Meld but also "
        "shares another Syncthing folder on this machine; remove it in Syncthing's web UI"
    ) in res_multi.output
    assert fake_syncthing.bodies == []

    # 6. Unreadable retirement record surfaces degradation without blocking valid removal
    backend.put(retired_record_key("badret11"), b"corrupt-ciphertext")
    fake_syncthing.responses["/rest/config/folders"] = [ok_folder]
    res_remove = runner.invoke(app, ["pull"])
    assert res_remove.exit_code == 0, res_remove.output
    assert (
        "mm: notice: Retirement file retired/badret11.enc is unreadable. "
        "Remove it by hand if it stays unreadable." in res_remove.output
    )
    assert (
        f"Pull: Syncthing removed retired device mac-retired ({ret_st_id[:7]})."
        in res_remove.output
    )
    assert ("DELETE", f"/rest/config/devices/{ret_st_id}", None) in fake_syncthing.bodies
    assert ret_st_id not in {
        d["deviceID"] for d in fake_syncthing.responses["/rest/config/devices"]
    }
    assert ret_st_id not in {
        d["deviceID"] for d in fake_syncthing.responses["/rest/config/folders"][0]["devices"]
    }
    # An active introducer sharing another folder can still reinsert a retired device.
    fake_syncthing.set_config(
        devices=[
            {
                "deviceID": ret_st_id,
                "name": "mac-retired",
                "addresses": ["dynamic"],
                "introducer": False,
                "autoAcceptFolders": False,
                "paused": False,
                "introducedBy": peer_b_st_id,
            },
            {
                "deviceID": peer_b_st_id,
                "name": "mac-b-renamed",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            },
        ],
        folders=[ok_folder, extra_peer_folder],
    )
    fake_syncthing.responses["/rest/config/devices"][0]["name"] = "mac-a"
    fake_syncthing.bodies.clear()
    res_again = runner.invoke(app, ["pull"])
    assert res_again.exit_code == 0, res_again.output
    assert "stays until mac-b-renamed drops it" in res_again.output
    assert not any(method == "DELETE" for method, _, _ in fake_syncthing.bodies)


@pytest.mark.parametrize("remove_fails", [False, True])
def test_attended_retirement_leaves_budget_for_both_renames(
    fake_syncthing, tmp_path, monkeypatch, remove_fails
):
    root, backend, my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    retired_id = _valid_syncthing_id("R")
    current_id = _valid_syncthing_id("B")
    fake_syncthing.set_folder(
        root, peers=[(retired_id, "retired", False), (current_id, "old name", True)]
    )
    fake_syncthing.responses["/rest/config/devices"][0]["name"] = "old own name"
    fake_syncthing.responses["/rest/system/connections"] = {
        "connections": {retired_id: {"connected": False}}
    }
    devices.write_retirement_record(
        backend, "dev-ret", "retired", retired_id, PASSPHRASE, TEST_MEMORY_KB
    )
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", current_id, PASSPHRASE, TEST_MEMORY_KB)
    now = 1000.0
    real_get = syncthing._get_json
    real_send = syncthing._send_json

    def slow_get(*args, **kwargs):
        nonlocal now
        now += 0.55
        return real_get(*args, **kwargs)

    def slow_send(endpoint, method, *args, **kwargs):
        nonlocal now
        now += 0.55
        if method == "DELETE" and remove_fails:
            return "error", "http-500"
        return real_send(endpoint, method, *args, **kwargs)

    monkeypatch.setattr(syncthing.time, "monotonic", lambda: now)
    monkeypatch.setattr(syncthing, "_get_json", slow_get)
    monkeypatch.setattr(syncthing, "_send_json", slow_send)
    result = meshflow.run(
        config.load_config(),
        PASSPHRASE,
        TEST_MEMORY_KB,
        verb="pull",
        attended=True,
        backend=backend,
    )

    assert result.stop_cause is None
    assert result.renamed == [("mac-a", my_id, True), ("mac-b", current_id, False)]
    assert result.removed == ([] if remove_fails else [("retired", retired_id)])
    if remove_fails:
        assert any("http-500" in detail for detail in result.degradations)
    else:
        assert ("DELETE", f"/rest/config/devices/{retired_id}", None) in fake_syncthing.bodies


@pytest.mark.parametrize(
    "late_change",
    ["folder", "introducer", "registration", "listing-error", "retirement", "connection"],
)
def test_retirement_rechecks_eligibility_after_initial_snapshot(
    fake_syncthing, tmp_path, monkeypatch, late_change
):
    root, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    retired_id = _valid_syncthing_id("R")
    current_id = _valid_syncthing_id("B")
    fake_syncthing.set_folder(
        root, peers=[(retired_id, "retired", False), (current_id, "mac-b", True)]
    )
    fake_syncthing.responses["/rest/system/connections"] = {
        "connections": {retired_id: {"connected": False}}
    }
    devices.write_retirement_record(
        backend, "dev-ret", "retired", retired_id, PASSPHRASE, TEST_MEMORY_KB
    )
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", current_id, PASSPHRASE, TEST_MEMORY_KB)
    take_snapshot = syncthing.SyncthingWriter.mesh_snapshot

    def snapshot_then_change(writer, folder_id):
        snapshot = take_snapshot(writer, folder_id)
        if late_change == "folder":
            fake_syncthing.responses["/rest/config/folders"] = []
        elif late_change == "introducer":
            fake_syncthing.responses["/rest/config/devices"][1]["introducedBy"] = current_id
            for dev in fake_syncthing.responses["/rest/config/devices"]:
                if dev["deviceID"] == current_id:
                    dev["introducer"] = True
        elif late_change == "registration":
            register_device(backend, "dev-ret", "retired")
            devices.publish_record(backend, "dev-ret", retired_id, PASSPHRASE, TEST_MEMORY_KB)
        elif late_change == "retirement":
            from mind_meld.storage.keys import retired_record_key

            backend.delete(retired_record_key("dev-ret"))
        elif late_change == "connection":
            read_retirements = devices.read_retirement_records

            def reconnect_during_read(*args, **kwargs):
                if kwargs.get("strict"):
                    conns = fake_syncthing.responses["/rest/system/connections"]["connections"]
                    conns[retired_id]["connected"] = True
                return read_retirements(*args, **kwargs)

            monkeypatch.setattr(devices, "read_retirement_records", reconnect_during_read)
        else:
            real_list = LocalBackend.list_keys

            def list_keys(self, prefix, *, strict=False):
                if prefix == "devices/" and strict:
                    raise PermissionError(errno.EACCES, "Cannot list registry")
                return real_list(self, prefix, strict=strict)

            monkeypatch.setattr(LocalBackend, "list_keys", list_keys)
        return snapshot

    monkeypatch.setattr(syncthing.SyncthingWriter, "mesh_snapshot", snapshot_then_change)
    result = meshflow.run(
        config.load_config(),
        PASSPHRASE,
        TEST_MEMORY_KB,
        verb="pull",
        attended=True,
        backend=backend,
    )
    assert result.removed == []
    assert not any(method == "DELETE" for method, _, _ in fake_syncthing.bodies)


@pytest.mark.parametrize("inventory", ["clean", "dropped", "vanished"])
def test_retirement_with_no_vouched_peer_requires_complete_registry(
    fake_syncthing, tmp_path, monkeypatch, inventory
):
    root, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch, publish_own=False)
    retired_id = _valid_syncthing_id("R")
    fake_syncthing.set_folder(root, peers=[(retired_id, "retired", False)])
    fake_syncthing.responses["/rest/system/connections"] = {
        "connections": {retired_id: {"connected": False}}
    }
    devices.write_retirement_record(
        backend, "dev-ret", "retired", retired_id, PASSPHRASE, TEST_MEMORY_KB
    )
    if inventory != "clean":
        peer_key = device_key("dev-b")
        backend.put(peer_key, b"unreadable")
        if inventory == "vanished":
            read = LocalBackend.get

            def vanish(self, key, *, expected=False):
                if key == peer_key:
                    raise StorageNotFound(key, vanished=True)
                return read(self, key, expected=expected)

            monkeypatch.setattr(LocalBackend, "get", vanish)
    result = meshflow.run(
        config.load_config(),
        PASSPHRASE,
        TEST_MEMORY_KB,
        verb="pull",
        attended=False,
        backend=backend,
    )
    if inventory == "clean":
        assert result.removed == [("retired", retired_id)]
    else:
        assert result.removed == []
        assert any(
            "removals wait for" in detail and "dev-b" in detail for detail in result.degradations
        )


def test_autopush_neither_removes_connected_retired_device_nor_readds_knocker(
    fake_syncthing, tmp_path, monkeypatch
):
    root, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    retired_id = _valid_syncthing_id("R")
    fake_syncthing.set_folder(root, peers=[(retired_id, "retired", False)])
    fake_syncthing.responses["/rest/system/connections"] = {
        "connections": {retired_id: {"connected": True}}
    }
    devices.write_retirement_record(
        backend, "dev-ret", "retired", retired_id, PASSPHRASE, TEST_MEMORY_KB
    )
    first = runner.invoke(app, ["autopush"])
    assert first.exit_code == 0 and not first.output
    crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["push"]
    assert "is retired but still connected" in crumb["detail"]
    assert not any(method == "DELETE" for method, _, _ in fake_syncthing.bodies)

    fake_syncthing.set_folder(root, peers=[])
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {retired_id: {"name": "retired"}}
    fake_syncthing.bodies.clear()
    fake_syncthing.requests.clear()
    second = runner.invoke(app, ["autopush"])
    assert second.exit_code == 0 and not second.output
    assert any(
        method == "GET" and path == "/rest/cluster/pending/devices"
        for method, path, _ in fake_syncthing.requests
    )
    assert fake_syncthing.bodies == []


def test_corrupt_own_record_preserves_diagnostic_without_holding_peer_retirement(
    fake_syncthing, tmp_path, monkeypatch
):
    root, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    backend.put(syncthing_record_key("dev-a"), b"corrupt")
    retired_id = _valid_syncthing_id("R")
    fake_syncthing.set_folder(root, peers=[(retired_id, "old-box", False)])
    devices.write_retirement_record(
        backend, "dev-ret", "old-box", retired_id, PASSPHRASE, TEST_MEMORY_KB
    )
    res = runner.invoke(app, ["pull"])
    assert res.exit_code == 0, res.output
    assert "the Syncthing record of device dev-a is unreadable" in res.output
    assert ("DELETE", f"/rest/config/devices/{retired_id}", None) in fake_syncthing.bodies


def test_mesh_still_renames_when_pairing_loses_fleet_folder(fake_syncthing, tmp_path, monkeypatch):
    root, backend, own_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    joining_id = _valid_syncthing_id("B")
    peer_id = _valid_syncthing_id("C")
    for mm_id, name, st_id in (("dev-b", "linux-a", joining_id), ("dev-c", "linux-b", peer_id)):
        register_device(backend, mm_id, name)
        devices.publish_record(backend, mm_id, st_id, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.set_folder(root, peers=[(peer_id, "old-peer-name", True)])
    fake_syncthing.responses["/rest/config/devices"][0]["name"] = "old-own-name"
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {joining_id: {}}
    attemptlog.write_mesh_waits(
        ["Syncthing device removals wait for a complete fleet registry read. Run: mm diag"]
    )
    previous_waits = attemptlog.read_mesh_waits()

    def move_folder_after_add(method, path, body):
        if method == "POST" and path == "/rest/config/devices":
            fake_syncthing.responses["/rest/config/folders"][0]["path"] = str(tmp_path / "rebound")

    fake_syncthing.on_write = move_folder_after_add
    res = runner.invoke(app, ["pull"])
    assert res.exit_code == 0 and "not-mm-folder" in res.output
    assert attemptlog.read_mesh_waits() == previous_waits
    assert not any(path.startswith("/rest/config/folders/") for _, path, _ in fake_syncthing.bodies)
    assert ("PATCH", f"/rest/config/devices/{own_id}", {"name": "mac-a"}) in fake_syncthing.bodies
    assert (
        "PATCH",
        f"/rest/config/devices/{peer_id}",
        {"name": "linux-b"},
    ) in fake_syncthing.bodies


@pytest.mark.parametrize("clear_cmd", ["push", "pull", "autopush", "autopull"])
def test_mesh_removal_wait_refreshes_and_clears_across_all_sync_commands(
    fake_syncthing, tmp_path, monkeypatch, clear_cmd
):
    """Track 156A AC3 & AC7: removal-wait lines in mm status come from the latest
    evaluated mesh pass (push, pull, autopush or autopull), clear once a later pass
    evaluates removals with nothing waiting, and are never cleared by an unevaluated
    stopped pass."""
    storage_dir, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    ret_st_id = _valid_syncthing_id("R")
    peer_b_st_id = _valid_syncthing_id("B")
    fake_syncthing.set_folder(
        storage_dir,
        peers=[(ret_st_id, "mac-retired", False), (peer_b_st_id, "mac-b", True)],
    )
    fake_syncthing.responses["/rest/system/connections"] = {
        "connections": {
            ret_st_id: {"connected": False},
            peer_b_st_id: {"connected": True},
        }
    }
    devices.write_retirement_record(
        backend, "dev-ret", "mac-retired", ret_st_id, PASSPHRASE, TEST_MEMORY_KB
    )
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", peer_b_st_id, PASSPHRASE, TEST_MEMORY_KB)
    # Unpublished live peer holds removal
    register_device(backend, "dev-unpub", "linux-c")

    res_attended = runner.invoke(app, ["pull"])
    assert res_attended.exit_code == 0, res_attended.output
    wait_line = (
        "Syncthing device removals wait for linux-c (dev-unpub) to publish "
        "its Syncthing ID; run 'mm update' and 'mm push' on linux-c"
    )
    assert f"mm: notice: {wait_line}" in res_attended.output
    # New attended run record does not carry wait-prefixed degradations
    assert all(
        not d.startswith("Syncthing device removals wait for ")
        for d in attemptlog.read_runs()["runs"]["pull"]["degradations"]
    )
    assert attemptlog.read_mesh_waits()["waits"] == [wait_line]

    status_waiting = runner.invoke(app, ["status"])
    assert status_waiting.exit_code == 0, status_waiting.output
    assert wait_line in " ".join(status_waiting.output.split())

    hook_wait = runner.invoke(app, ["autopull"])
    assert hook_wait.exit_code == 0 and hook_wait.output == ""
    crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["pull"]
    assert crumb["outcome"] == "degraded"
    assert not crumb.get("detail")
    status_hook_waiting = runner.invoke(app, ["status"])
    assert status_hook_waiting.exit_code == 0, status_hook_waiting.output
    verdict = status_hook_waiting.stdout.splitlines()[0]
    assert "Latest pull needs attention" not in verdict
    assert "Sync run evidence unknown" not in verdict
    assert wait_line in " ".join(status_hook_waiting.stdout.split())

    # An unevaluated stopped pass (e.g. unauthorized) must NOT clear the standing wait
    fake_syncthing.responses["/rest/system/status"] = 403
    stopped = runner.invoke(app, ["autopull"])
    assert stopped.exit_code == 0 and stopped.output == ""
    assert attemptlog.read_mesh_waits()["waits"] == [wait_line]
    status_after_stop = runner.invoke(app, ["status"])
    assert wait_line in " ".join(status_after_stop.output.split())
    fake_syncthing.responses["/rest/system/status"] = {"myID": fake_syncthing.my_id}

    def fail_retirement_listing(*args, **kwargs):
        raise OSError(errno.EIO, "retirement records unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(devices, "read_retirement_records", fail_retirement_listing)
        unavailable = runner.invoke(app, ["autopull"])
        assert unavailable.exit_code == 0 and unavailable.output == ""
        assert attemptlog.read_mesh_waits()["waits"] == [wait_line]

    # Resolve the hold by publishing the peer's Syncthing ID; run clear_cmd
    unpub_st_id = _valid_syncthing_id("C")
    devices.publish_record(backend, "dev-unpub", unpub_st_id, PASSPHRASE, TEST_MEMORY_KB)
    if clear_cmd in ("push", "autopush"):
        notes = tmp_path / "claude" / "projects" / "p" / "memory"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / f"note-{clear_cmd}.md").write_text("change\n", encoding="utf-8")

    cleared = runner.invoke(app, [clear_cmd])
    assert cleared.exit_code == 0, cleared.output
    if clear_cmd.startswith("auto"):
        assert cleared.output == ""
    assert attemptlog.read_mesh_waits()["state"] == "valid"
    assert attemptlog.read_mesh_waits()["waits"] == []

    status_cleared = runner.invoke(app, ["status"])
    assert status_cleared.exit_code == 0, status_cleared.output
    assert "Syncthing device removals wait for" not in " ".join(status_cleared.output.split())


@pytest.mark.parametrize("failure", ["write", "read"])
def test_hook_wait_with_unavailable_mesh_record_keeps_degraded_verdict(
    fake_syncthing, tmp_path, monkeypatch, failure
):
    _setup_fleet(fake_syncthing, tmp_path, monkeypatch)

    def wait_for_peer(result, **_kwargs):
        result.removals_evaluated = True
        result.degradations.append(
            "Syncthing device removals wait for a complete fleet registry read. Run: mm diag"
        )
        return result

    def fail_write(*_args, **_kwargs):
        raise OSError(errno.EIO, "mesh record unavailable")

    monkeypatch.setattr(meshflow, "_run_inner", wait_for_peer)
    if failure == "write":
        attemptlog.write_mesh_waits([])
        monkeypatch.setattr(attemptlog, "write_mesh_waits", fail_write)
    hook = runner.invoke(app, ["autopull"])
    assert hook.exit_code == 0 and hook.output == ""
    if failure == "read":
        assert attemptlog.read_mesh_waits()["state"] == "valid"
        monkeypatch.setattr(attemptlog, "read_mesh_waits", lambda: {"state": "contended"})

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert "Latest pull needs attention" in status.stdout.splitlines()[0]


def test_compose_hook_breadcrumb_several_omitted_details_keeps_priority_and_status_display(
    fake_syncthing, tmp_path, monkeypatch
):
    """Track 156A T5: mesh breadcrumbs with several omitted details and long device
    names preserve priority (existing non-mesh degradations intact, non-wait mesh
    degradations before share/removal/rename summaries, +N more within 300 chars,
    wait lines excluded from detail text while still marking outcome degraded) and
    display cleanly in mm status."""
    _, backend, _ = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)

    # 1. Pure wait degradation marks outcome degraded without putting wait text in crumb
    wait_only = meshflow.MeshResult(
        degradations=[
            "Syncthing device removals wait for linux-c (dev-c) to publish "
            "its Syncthing ID; run 'mm update' and 'mm push' on linux-c"
        ]
    )
    wait_outcome, wait_detail = meshflow.compose_hook_breadcrumb([], wait_only)
    assert wait_outcome == "degraded"
    assert "Syncthing device removals wait for" not in wait_detail

    # 2. Several complete diagnostic clauses + long device names overflowing 300-char budget
    id_a = _valid_syncthing_id("B")
    id_b = _valid_syncthing_id("C")
    id_c = _valid_syncthing_id("D")
    id_d = _valid_syncthing_id("E")
    long_a, long_b, long_c, long_d = (letter * 128 for letter in "abcd")
    diagnostics = [
        f"Syncthing did not confirm clearing the introducer flag for {long_a} ({id_a[:7]}); "
        "it may have applied; the next push or pull reconciles it",
        f"Syncthing mesh step did not finish for {long_b} ({id_b[:7]}); "
        "the next push or pull reconciles it",
        "the Syncthing record of device dev-newer-peer is from a newer mm. Run: mm update",
        "the Syncthing record of device dev-corrupt-peer is unreadable. Run: mm diag",
    ]

    rich_mesh = meshflow.MeshResult(
        degradations=[*wait_only.degradations, *diagnostics],
        shared=[(long_a, id_a), (long_b, id_b), (long_c, id_c)],
        removed=[(long_d, id_d)],
        renamed=[(long_c, id_c, False)],
    )
    existing = ["events walk hit 250ms budget"]
    outcome, detail = meshflow.compose_hook_breadcrumb(existing, rich_mesh)
    assert outcome == "degraded"
    assert len(detail) <= meshflow.BREADCRUMB_BUDGET_CHARS
    assert detail == "; ".join([*existing, diagnostics[0], "+3 more"])

    prefix = "; ".join([*existing, *diagnostics])
    share_lead = "Syncthing now shares the fleet folder with 3 fleet machines"
    for summary in (
        f"{share_lead}: {long_a} ({id_a[:7]}), +2 more",
        f"{share_lead} (+3 more)",
        "3 Syncthing shares",
        "",
    ):
        limited = meshflow.compose_hook_breadcrumb(
            existing, rich_mesh, budget=len(prefix) + 2 + len(summary)
        )
        assert limited == ("degraded", f"{prefix}; {summary}" if summary else prefix)

    shared = f"{share_lead}: {long_a} ({id_a[:7]}), {long_b} ({id_b[:7]}), {long_c} ({id_c[:7]})"
    removed = f"Syncthing removed retired device {long_d} ({id_d[:7]})"
    renamed = f"Syncthing renamed {long_c} ({id_c[:7]})"
    up_to_remove = f"{prefix}; {shared}; {removed}"
    assert meshflow.compose_hook_breadcrumb(
        existing, rich_mesh, budget=len(f"{up_to_remove}; +1 more")
    ) == ("degraded", f"{up_to_remove}; +1 more")
    assert meshflow.compose_hook_breadcrumb(
        existing, rich_mesh, budget=len(f"{up_to_remove}; {renamed}")
    ) == ("degraded", f"{up_to_remove}; {renamed}")

    # Exercise the actual hook writer and the status renderer with an overflowing mesh result.
    for suffix in "abcdef":
        device_id = f"dev-corrupt-{suffix}"
        register_device(backend, device_id, f"mac-{suffix}")
        backend.put(syncthing_record_key(device_id), b"corrupt record")
    hook = runner.invoke(app, ["autopull"])
    assert hook.exit_code == 0 and hook.output == ""
    crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["pull"]
    assert crumb["outcome"] == "degraded"
    assert crumb["detail"] == "; ".join(
        [
            f"the Syncthing record of device dev-corrupt-{suffix} is unreadable. Run: mm diag"
            for suffix in "abc"
        ]
        + ["+3 more"]
    )
    res_status = runner.invoke(app, ["status"])
    assert res_status.exit_code == 0, res_status.output
    flat = " ".join(res_status.output.split())
    assert "Last auto-pull:" in flat
    assert f"(degraded: {crumb['detail']})" in flat


def test_attended_partial_pull_reports_confirmed_introducer_clear(
    fake_syncthing, tmp_path, monkeypatch
):
    storage_dir, backend, _my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer_id = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-b", peer_id, PASSPHRASE, TEST_MEMORY_KB)
    fake_syncthing.set_folder(storage_dir, peers=[(peer_id, "mac-b", True)])
    fake_syncthing.responses["/rest/config/devices"][1]["introducer"] = True
    monkeypatch.setattr(
        cli.pullflow, "_pull_core", lambda *args, **kwargs: cli.pullflow.PullResult(total_failed=1)
    )

    result = runner.invoke(app, ["pull"])

    assert result.exit_code == 1
    assert fake_syncthing.bodies == [
        ("PATCH", f"/rest/config/devices/{peer_id}", {"introducer": False})
    ]
    clear_line = f"Pull: Syncthing cleared the introducer flag on mac-b ({peer_id[:7]})."
    assert result.stdout.splitlines().count(clear_line) == 1
    assert result.output.index(clear_line) < result.output.index("Pull incomplete")
    assert result.output.splitlines()[-1] == "Run: mm diag"


def test_authenticated_init_pull_initiates_only_vouched_active_peers(
    fake_syncthing, tmp_path, monkeypatch
):
    _storage_dir, backend, _my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    eligible = _valid_syncthing_id("B")
    ignored = _valid_syncthing_id("C")
    retired = _valid_syncthing_id("D")
    paused = _valid_syncthing_id("E")
    unvouched = _valid_syncthing_id("F")
    for label, peer_id in (
        ("dev-b", eligible),
        ("dev-c", ignored),
        ("dev-d", retired),
        ("dev-e", paused),
    ):
        register_device(backend, label, label)
        devices.publish_record(backend, label, peer_id, PASSPHRASE, TEST_MEMORY_KB)
    devices.write_retirement_record(backend, "dev-d", "dev-d", retired, PASSPHRASE, TEST_MEMORY_KB)
    configured = fake_syncthing.responses["/rest/config/devices"]
    configured.append({"deviceID": paused, "name": "dev-e", "paused": True})
    fake_syncthing.responses["/rest/config"] = {
        "devices": configured,
        "folders": fake_syncthing.responses["/rest/config/folders"],
        "remoteIgnoredDevices": [{"deviceID": ignored}],
    }
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {unvouched: {"name": "unvouched"}}

    normal = runner.invoke(app, ["pull"])
    assert normal.exit_code == 0, normal.output
    assert fake_syncthing.bodies == []

    def verified_join(*, new_fleet, pull_join):
        assert not new_fleet
        assert pull_join is not None
        pull_join(PASSPHRASE, lambda incomplete: None)

    monkeypatch.setattr(cli.initflow, "init", verified_join)
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    (method, route, body), (share_method, share_route, share) = fake_syncthing.bodies
    assert (method, route) == ("POST", "/rest/config/devices")
    assert body["deviceID"] == eligible and body["introducer"] is False
    assert (share_method, share_route) == ("PATCH", "/rest/config/folders/mind-meld")
    assert share["devices"][-1] == {"deviceID": eligible}
    assert "Syncthing now shares" in result.output


@pytest.mark.parametrize("interruption", ["config-unavailable", "record-in-flight"])
def test_initialized_joiner_retries_known_peers_on_later_pull(
    fake_syncthing, tmp_path, monkeypatch, interruption
):
    _storage, backend, my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    peer_id = _valid_syncthing_id("B")
    register_device(backend, "dev-b", "mac-b")
    config.patch_config_on_disk(
        {"device": {"syncthing_join_device": my_id, "syncthing_join_peers": ["dev-b"]}}
    )
    if interruption == "config-unavailable":
        devices.publish_record(backend, "dev-b", peer_id, PASSPHRASE, TEST_MEMORY_KB)
        fake_syncthing.responses["/rest/config"] = 503

    first = runner.invoke(app, ["pull"])

    assert first.exit_code == 0, first.output
    assert fake_syncthing.bodies == []
    assert config.load_config()["device"]["syncthing_join_peers"] == ["dev-b"]
    if interruption == "record-in-flight":
        devices.publish_record(backend, "dev-b", peer_id, PASSPHRASE, TEST_MEMORY_KB)
    else:
        del fake_syncthing.responses["/rest/config"]

    retry = runner.invoke(app, ["pull"])

    assert retry.exit_code == 0, retry.output
    assert [(method, path) for method, path, _ in fake_syncthing.bodies] == [
        ("POST", "/rest/config/devices"),
        ("PATCH", "/rest/config/folders/mind-meld"),
    ]
    assert fake_syncthing.bodies[0][2]["deviceID"] == peer_id
    assert config.load_config()["device"]["syncthing_join_peers"] == []


def test_mesh_clears_stale_introducer_flags_on_fleet_peers_only(
    fake_syncthing, tmp_path, monkeypatch
):
    storage_dir, backend, my_id = _setup_fleet(fake_syncthing, tmp_path, monkeypatch)
    fleet_only = _valid_syncthing_id("B")
    multi_folder = _valid_syncthing_id("C")
    unvouched = _valid_syncthing_id("D")
    unshared_vouched = _valid_syncthing_id("E")

    register_device(backend, "dev-b", "mac-b")
    register_device(backend, "dev-c", "linux-c")
    register_device(backend, "dev-e", "mac-e")
    devices.publish_record(backend, "dev-b", fleet_only, PASSPHRASE, TEST_MEMORY_KB)
    devices.publish_record(backend, "dev-c", multi_folder, PASSPHRASE, TEST_MEMORY_KB)
    devices.publish_record(backend, "dev-e", unshared_vouched, PASSPHRASE, TEST_MEMORY_KB)

    ok_folder = {
        **fake_syncthing.stock_folder(
            storage_dir, shared_with=[fleet_only, multi_folder, unvouched]
        ),
        "id": "mind-meld",
    }
    other_folder = {
        **fake_syncthing.stock_folder(tmp_path / "other", shared_with=[multi_folder]),
        "id": "other-folder",
    }
    fake_syncthing.set_config(
        devices=[
            {
                "deviceID": fleet_only,
                "name": "mac-b",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            },
            {
                "deviceID": multi_folder,
                "name": "linux-c",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            },
            {
                "deviceID": unvouched,
                "name": "stranger",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            },
            {
                "deviceID": unshared_vouched,
                "name": "mac-e",
                "addresses": ["dynamic"],
                "introducer": True,
                "autoAcceptFolders": False,
                "paused": False,
            },
        ],
        folders=[ok_folder, other_folder],
    )
    fake_syncthing.responses["/rest/config/devices"][0]["name"] = "mac-a"
    fake_syncthing.bodies.clear()

    # Attended pull: clears fleet_only, shares mind-meld with unshared_vouched,
    # leaves multi_folder and unvouched untouched.
    res = runner.invoke(app, ["pull"])
    assert res.exit_code == 0, res.output
    clear_patches = [
        (m, p, b)
        for m, p, b in fake_syncthing.bodies
        if m == "PATCH" and p.startswith("/rest/config/devices/")
    ]
    assert clear_patches == [("PATCH", f"/rest/config/devices/{fleet_only}", {"introducer": False})]
    assert any(
        m == "PATCH"
        and p == "/rest/config/folders/mind-meld"
        and {"deviceID": unshared_vouched} in b["devices"]
        for m, p, b in fake_syncthing.bodies
    )
    clear_line = f"Pull: Syncthing cleared the introducer flag on mac-b ({fleet_only[:7]})."
    assert res.output.count(clear_line) == 1

    # Next pull clears unshared_vouched (now sharing mind-meld); after that, steady state is quiet.
    fake_syncthing.bodies.clear()
    res_unshared = runner.invoke(app, ["pull"])
    assert res_unshared.exit_code == 0, res_unshared.output
    assert fake_syncthing.bodies == [
        ("PATCH", f"/rest/config/devices/{unshared_vouched}", {"introducer": False})
    ]

    fake_syncthing.bodies.clear()
    res_steady = runner.invoke(app, ["pull"])
    assert res_steady.exit_code == 0, res_steady.output
    assert fake_syncthing.bodies == []
    assert "cleared the introducer flag" not in res_steady.output

    res_auto = runner.invoke(app, ["autopull"])
    assert res_auto.exit_code == 0, res_auto.output
    assert res_auto.output == ""
    assert fake_syncthing.bodies == []

    # 500 on clear PATCH (hook run): exits 0, records degradation in breadcrumb,
    # and the pass still reaches renames.
    for dev in fake_syncthing.responses["/rest/config/devices"]:
        if dev["deviceID"] == fleet_only:
            dev["introducer"] = True
        if dev["deviceID"] == my_id:
            dev["name"] = "stale-mac-a"
    fake_syncthing.write_responses[("PATCH", f"/rest/config/devices/{fleet_only}")] = 500
    fake_syncthing.bodies.clear()
    res_fail = runner.invoke(app, ["autopull"])
    assert res_fail.exit_code == 0
    assert res_fail.output == ""
    crumb = json.loads(cli.core._autorun_breadcrumb_path().read_text())["pull"]
    assert crumb["outcome"] == "degraded"
    assert (
        f"Syncthing did not confirm clearing the introducer flag for mac-b ({fleet_only[:7]})"
        in crumb["detail"]
    )
    assert ("PATCH", f"/rest/config/devices/{my_id}", {"name": "mac-a"}) in fake_syncthing.bodies
