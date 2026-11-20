"""Tests for `mm devices retire <name|id> [--yes]` and retirement records (Track 154A)."""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import config, core, crypto, devices, fsutil, lockfile, syncthing
from mind_meld.cli import app
from mind_meld.devices import register_device
from mind_meld.errors import LockError
from mind_meld.storage.keys import (
    blob_key,
    device_key,
    manifest_key,
    parse_retired_record_key,
    retired_record_key,
    syncthing_record_key,
)
from mind_meld.storage.local import LocalBackend

runner = CliRunner()
PASSPHRASE = "retire-test-passphrase"
TEST_MEMORY_KB = 1024


def _valid_syncthing_id(seed_char: str) -> str:
    chunk13 = seed_char * 13
    group14 = chunk13 + syncthing._luhn32(chunk13)
    full56 = group14 * 4
    return "-".join(full56[i : i + 7] for i in range(0, 56, 7))


def _setup_retire_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBackend:
    cfg_path = tmp_path / "cfg" / "config.toml"
    lock_path = tmp_path / "cfg" / "mm.lock"
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "LOCK_PATH", lock_path)
    monkeypatch.setattr(lockfile, "LOCK_PATH", lock_path)
    monkeypatch.setattr(crypto, "MIN_ARGON2_MEMORY_KB", TEST_MEMORY_KB, raising=False)

    storage_dir = tmp_path / "sync"
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects").mkdir(parents=True, exist_ok=True)
    backend = LocalBackend(str(storage_dir))
    init_fetch = crypto.bootstrap_crypto_init(backend, PASSPHRASE, TEST_MEMORY_KB)
    assert init_fetch.root_salt is not None
    crypto.set_crypto_session(init_fetch.root_salt, TEST_MEMORY_KB)

    register_device(backend, "aaaa1111", "mac-a")
    backend.put(
        device_key("aaaa1111"),
        json.dumps(
            {
                "device_id": "aaaa1111",
                "device_name": "mac-a",
                "registered": "2026-01-01T00:00:00+00:00",
                "last_seen": "2026-03-01T00:00:00+00:00",
                "last_seen_version": core.__version__,
            }
        ).encode("utf-8"),
    )
    devices.publish_record(
        backend, "aaaa1111", _valid_syncthing_id("A"), PASSPHRASE, TEST_MEMORY_KB
    )

    config.save_config(
        {
            "device": {"id": "aaaa1111", "name": "mac-a"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "sources": [{"name": "claude", "type": "claude", "path": str(claude_dir)}],
                "max_file_size": 10 * 1024 * 1024,
            },
            "crypto": {
                "argon2_memory_kb": TEST_MEMORY_KB,
                "root_salt_fp": crypto.root_salt_fingerprint(init_fetch.root_salt),
            },
        }
    )
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    return backend


def test_retired_record_key_validation_and_generate_unique_id_avoids_retired(tmp_path, monkeypatch):
    assert retired_record_key("abc12345") == "retired/abc12345.enc"
    assert parse_retired_record_key("retired/abc12345.enc") == "abc12345"
    for bad in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
        with pytest.raises(ValueError):
            retired_record_key(bad)
    for bad_key in (
        "",
        "syncthing/abc12345.enc",
        "retired/abc12345.json",
        "retired/.enc",
        "retired/sub/abc.enc",
        "retired/abc.sync-conflict-20261119-120000-AAAAAAA.enc",
        "retired/abc 2.enc",
    ):
        assert parse_retired_record_key(bad_key) is None

    backend = _setup_retire_fleet(tmp_path, monkeypatch)
    st_b = _valid_syncthing_id("B")
    devices.write_retirement_record(
        backend, "deadbeef", "old-mac", st_b, PASSPHRASE, TEST_MEMORY_KB
    )
    draws = iter(["deadbeef", "cafebabe"])
    monkeypatch.setattr(
        devices.uuid,
        "uuid4",
        lambda: type("U", (), {"hex": next(draws) + "0" * 24})(),
    )
    picked = devices.generate_unique_short_device_id([], backend=backend)
    assert picked == "cafebabe"


@pytest.mark.parametrize("has_unreadable_record", [False, True])
def test_retire_interactive_by_name_and_id_three_outcome_branches_and_idempotence(
    tmp_path, monkeypatch, has_unreadable_record
):
    backend = _setup_retire_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)

    st_b = _valid_syncthing_id("B")
    st_shared = _valid_syncthing_id("C")

    # Seed peer B (with published Syncthing ID, manifest, and blob)
    backend.put(
        device_key("bbbb2222"),
        json.dumps(
            {
                "device_id": "bbbb2222",
                "device_name": "cachyos",
                "registered": "2026-01-10T00:00:00+00:00",
                "last_seen": "2026-03-01T00:00:00+00:00",
                "last_seen_version": core.__version__,
            }
        ).encode("utf-8"),
    )
    devices.publish_record(backend, "bbbb2222", st_b, PASSPHRASE, TEST_MEMORY_KB)
    sha = "a" * 64
    backend.put(manifest_key("bbbb2222"), b"encrypted-manifest")
    backend.put(blob_key("bbbb2222", sha), b"encrypted-blob")

    # 1. Decline confirmation (Enter = n) -> Nothing was changed, exit 0, 0 mutations
    res_decline = runner.invoke(app, ["devices", "retire", "cachyos"], input="\n")
    assert res_decline.exit_code == 0, res_decline.output
    assert "Retire cachyos (bbbb2222)? [y/n] (Enter = n)" in res_decline.output
    assert "Retire: Nothing was changed." in res_decline.output
    assert backend.exists(device_key("bbbb2222"))
    assert backend.exists(syncthing_record_key("bbbb2222"))
    assert not backend.exists(retired_record_key("bbbb2222"))

    # 2. Confirm retirement by name -> Branch 3 outcome, manifest & blob preserved
    res_ok = runner.invoke(app, ["devices", "retire", "cachyos"], input="y\n")
    assert res_ok.exit_code == 0, res_ok.output
    assert (
        f"Retire: retired cachyos (bbbb2222); each machine where mm manages Syncthing "
        f"removes {st_b[:7]} from Syncthing at its first push or pull after the device "
        "stops connecting"
    ) in " ".join(res_ok.output.split())
    assert not backend.exists(device_key("bbbb2222"))
    assert not backend.exists(syncthing_record_key("bbbb2222"))
    assert backend.exists(retired_record_key("bbbb2222"))
    assert backend.exists(manifest_key("bbbb2222"))
    assert backend.exists(blob_key("bbbb2222", sha))
    ret_records = devices.read_retirement_records(backend, PASSPHRASE, TEST_MEMORY_KB)
    assert ret_records.records["bbbb2222"] == devices.RetirementRecord(
        mm_device_id="bbbb2222",
        device_name="cachyos",
        syncthing_device_id=st_b,
    )

    # 3. Rerun on already-retired device (by ID or name) -> State 2 of E1, exit 0
    for target in ("bbbb2222", "cachyos"):
        res_again = runner.invoke(app, ["devices", "retire", target])
        assert res_again.exit_code == 0, res_again.output
        assert (
            f"Retire: cachyos (bbbb2222) is already retired; each machine where mm manages "
            f"Syncthing removes {st_b[:7]}"
        ) in " ".join(res_again.output.split())

    # 4. Branch 1 outcome: retiring a device with no published Syncthing ID
    backend.put(
        device_key("cccc3333"),
        json.dumps(
            {
                "device_id": "cccc3333",
                "device_name": "no-st-box",
                "registered": "2026-01-15T00:00:00+00:00",
                "last_seen": "2026-03-01T00:00:00+00:00",
                "last_seen_version": core.__version__,
            }
        ).encode("utf-8"),
    )
    if has_unreadable_record:
        backend.put(syncthing_record_key("cccc3333"), b"unreadable")
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: False)
    res_b1 = runner.invoke(app, ["devices", "retire", "cccc3333", "--yes"])
    assert res_b1.exit_code == 0, res_b1.output
    assert (
        "Retire: retired no-st-box (cccc3333); mm has no Syncthing ID on record for it, "
        "so no machine will remove it from Syncthing automatically; remove it in Syncthing's "
        "web UI wherever Syncthing still lists it."
    ) in " ".join(res_b1.output.split())

    # 5. Branch 2 outcome: retiring a device whose Syncthing ID is still vouched
    # by another registration
    for did in ("dddd4444", "eeee5555"):
        backend.put(
            device_key(did),
            json.dumps(
                {
                    "device_id": did,
                    "device_name": "reinit-box",
                    "registered": "2026-02-01T00:00:00+00:00",
                    "last_seen": "2026-03-01T00:00:00+00:00",
                    "last_seen_version": core.__version__,
                }
            ).encode("utf-8"),
        )
        devices.publish_record(backend, did, st_shared, PASSPHRASE, TEST_MEMORY_KB)
    res_b2 = runner.invoke(app, ["devices", "retire", "dddd4444", "--yes"])
    assert res_b2.exit_code == 0, res_b2.output
    assert (
        f"Retire: retired reinit-box (dddd4444); Syncthing device {st_shared[:7]} stays "
        "because registration reinit-box (eeee5555) still uses it."
    ) in " ".join(res_b2.output.split())


def test_retire_refusals_multi_match_chooser_partial_completion_and_old_peer_gates(
    tmp_path, monkeypatch
):
    backend = _setup_retire_fleet(tmp_path, monkeypatch)

    # Seed two registrations sharing the name "dup-box"
    for did, reg_ts in (
        ("dup11111", "2026-01-01T00:00:00+00:00"),
        ("dup22222", "2026-02-01T00:00:00+00:00"),
    ):
        backend.put(
            device_key(did),
            json.dumps(
                {
                    "device_id": did,
                    "device_name": "dup-box",
                    "registered": reg_ts,
                    "last_seen": reg_ts,
                    "last_seen_version": core.__version__,
                }
            ).encode("utf-8"),
        )

    # 1. Self-retirement refused by own ID or own name
    for self_arg in ("aaaa1111", "mac-a"):
        res_self = runner.invoke(app, ["devices", "retire", self_arg])
        assert res_self.exit_code == 1
        assert "cannot retire this machine from itself" in res_self.output

    # 2. Unknown device refused
    res_unk = runner.invoke(app, ["devices", "retire", "no-such-device"])
    assert res_unk.exit_code == 1
    assert "unknown device 'no-such-device'" in res_unk.output
    assert "Run: mm devices" in res_unk.output

    # 3. Non-TTY or --yes with a device name refuses and prints matching IDs
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: False)
    res_nontty_name = runner.invoke(app, ["devices", "retire", "dup-box"])
    assert res_nontty_name.exit_code == 1
    assert "dup11111" in res_nontty_name.output
    assert "dup22222" in res_nontty_name.output
    assert res_nontty_name.output.rstrip().splitlines()[-1] == "Run: mm devices retire <id> --yes"

    # Non-TTY with exact ID but no --yes refuses with --yes fix
    res_nontty_id = runner.invoke(app, ["devices", "retire", "dup11111"])
    assert res_nontty_id.exit_code == 1
    assert "confirming needs a terminal" in res_nontty_id.output
    assert "Run: mm devices retire dup11111 --yes" in res_nontty_id.output

    # TTY with --yes and a device name also refuses
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)
    res_yes_name = runner.invoke(app, ["devices", "retire", "dup-box", "--yes"])
    assert res_yes_name.exit_code == 1
    assert "require an exact device ID" in res_yes_name.output
    assert res_yes_name.output.rstrip().splitlines()[-1] == "Run: mm devices retire <id> --yes"
    assert all(backend.exists(device_key(device_id)) for device_id in ("dup11111", "dup22222"))

    # 4. TTY multi-match chooser: lists details, selects ID, then asks Retire confirm
    res_multi = runner.invoke(app, ["devices", "retire", "dup-box"], input="bad-id\ndup11111\ny\n")
    assert res_multi.exit_code == 0, res_multi.output
    assert "Multiple registered devices are named 'dup-box':" in res_multi.output
    assert "Enter one of the listed device IDs" in res_multi.output
    assert "Retire: retired dup-box (dup11111)" in res_multi.output
    assert not backend.exists(device_key("dup11111"))
    assert backend.exists(device_key("dup22222"))

    # 5. Storage write/delete failures at each step and partial-state completion (E1)
    backend_cls = type(backend)
    real_put = backend_cls.put
    real_delete = backend_cls.delete

    def fail_retired_put(self, key: str, data: bytes) -> None:
        if key.startswith("retired/"):
            raise OSError(errno.ENOSPC, "No space left")
        real_put(self, key, data)

    monkeypatch.setattr(backend_cls, "put", fail_retired_put)
    res_f1 = runner.invoke(app, ["devices", "retire", "dup22222", "--yes"])
    assert res_f1.exit_code == 1
    assert "could not write retirement record" in res_f1.output
    assert "nothing was deleted" in res_f1.output
    assert backend.exists(device_key("dup22222"))
    monkeypatch.setattr(backend_cls, "put", real_put)

    retirement_id = _valid_syncthing_id("R")
    devices.publish_record(backend, "dup22222", retirement_id, PASSPHRASE, TEST_MEMORY_KB)

    def fail_syncthing_delete(self, key: str, **kwargs) -> None:
        if key == syncthing_record_key("dup22222"):
            raise OSError(errno.EACCES, "Permission denied")
        real_delete(self, key, **kwargs)

    monkeypatch.setattr(backend_cls, "delete", fail_syncthing_delete)
    res_f2 = runner.invoke(app, ["devices", "retire", "dup22222", "--yes"])
    assert res_f2.exit_code == 1
    assert "could not remove its Syncthing record" in res_f2.output
    assert backend.exists(syncthing_record_key("dup22222"))
    assert backend.exists(device_key("dup22222"))

    def fail_dev_delete(self, key: str, **kwargs) -> None:
        if key == device_key("dup22222"):
            raise OSError(errno.EACCES, "Permission denied")
        real_delete(self, key, **kwargs)

    monkeypatch.setattr(backend_cls, "delete", fail_dev_delete)
    res_f3 = runner.invoke(app, ["devices", "retire", "dup22222", "--yes"])
    assert res_f3.exit_code == 1
    assert "wrote the retirement record and removed the Syncthing record" in res_f3.output
    assert backend.exists(retired_record_key("dup22222"))
    assert not backend.exists(syncthing_record_key("dup22222"))
    assert backend.exists(device_key("dup22222"))

    # Rerun finishes the partial retirement (State 1 of E1)
    monkeypatch.setattr(backend_cls, "delete", real_delete)
    res_finish = runner.invoke(app, ["devices", "retire", "dup22222", "--yes"])
    assert res_finish.exit_code == 0, res_finish.output
    assert not backend.exists(device_key("dup22222"))
    record = devices.read_retirement_records(backend, PASSPHRASE, TEST_MEMORY_KB).records
    assert record["dup22222"].syncthing_device_id == retirement_id

    # 6. Old-peer check (X10, E9, E14): --yes refuses when another peer has
    # last_seen_version <= 1.7.1
    backend.put(
        device_key("oldpeer1"),
        json.dumps(
            {
                "device_id": "oldpeer1",
                "device_name": "mac-old",
                "registered": "2026-01-01T00:00:00+00:00",
                "last_seen": "2026-01-02T00:00:00+00:00",
                "last_seen_version": "1.7.1",
            }
        ).encode("utf-8"),
    )
    backend.put(
        device_key("target99"),
        json.dumps(
            {
                "device_id": "target99",
                "device_name": "target-box",
                "registered": "2026-01-01T00:00:00+00:00",
                "last_seen": "2026-01-02T00:00:00+00:00",
                "last_seen_version": core.__version__,
            }
        ).encode("utf-8"),
    )
    res_old_yes = runner.invoke(app, ["devices", "retire", "target99", "--yes"])
    assert res_old_yes.exit_code == 1
    assert "retiring with --yes is refused until they are updated" in " ".join(
        res_old_yes.output.split()
    )
    assert "Run: mm update" in res_old_yes.output
    assert backend.exists(device_key("target99"))

    # Interactive retirement with old peer emits warning and still allows retirement on 'y'
    res_old_tty = runner.invoke(app, ["devices", "retire", "target99"], input="y\n")
    assert res_old_tty.exit_code == 0, res_old_tty.output
    assert "mm: warning:" in res_old_tty.output
    assert "mac-old (oldpeer1)" in res_old_tty.output
    assert not backend.exists(device_key("target99"))

    # 7. Lock contention refuses before any write (E5)
    register_device(backend, "locked11", "locked-box")
    monkeypatch.setattr(
        lockfile, "acquire_lock", lambda: (_ for _ in ()).throw(LockError("lock held"))
    )
    res_lock = runner.invoke(app, ["devices", "retire", "locked11", "--yes"])
    assert res_lock.exit_code == 1
    assert "lock held" in res_lock.output
    assert backend.exists(device_key("locked11"))


def test_retire_command_intent_and_read_only_policy():
    from tests.test_integration import (
        COMMAND_INTENTS62,
        _check_command_intents62,
        _check_read_only_policy62,
    )

    assert _check_command_intents62(app) == set(COMMAND_INTENTS62)
    src_root = Path(__file__).resolve().parents[1] / "src" / "mind_meld"
    for py_file in sorted(src_root.rglob("*.py")):
        _check_read_only_policy62(py_file.read_text(encoding="utf-8"))


def test_retire_reactivation_resolves_live_name_and_flushes_before_ordered_deletes(
    tmp_path, monkeypatch
):
    backend = _setup_retire_fleet(tmp_path, monkeypatch)
    old_id, live_id = "bbbb2222", "cccc3333"
    old_syncthing, live_syncthing = _valid_syncthing_id("B"), _valid_syncthing_id("C")
    devices.write_retirement_record(
        backend, old_id, "mac-b", old_syncthing, PASSPHRASE, TEST_MEMORY_KB
    )
    register_device(backend, live_id, "mac-b")
    devices.publish_record(backend, live_id, live_syncthing, PASSPHRASE, TEST_MEMORY_KB)

    calls = []
    fsync_dir = fsutil.fsync_dir
    delete = LocalBackend.delete
    import os
    import stat

    real_fsync = fsutil._fsync_fd

    def observe_fsync(fd):
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        calls.append(("fsync", kind))
        return real_fsync(fd)

    def observe_flush(path):
        calls.append(("flush", path.name))
        return fsync_dir(path)

    def observe_delete(self, key, **kwargs):
        calls.append(("delete", key))
        return delete(self, key, **kwargs)

    monkeypatch.setattr(fsutil, "fsync_dir", observe_flush)
    monkeypatch.setattr(fsutil, "_fsync_fd", observe_fsync)
    monkeypatch.setattr(LocalBackend, "delete", observe_delete)
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)
    result = runner.invoke(app, ["devices", "retire", "mac-b"], input="y\n")
    assert result.exit_code == 0, result.output
    assert calls[:3] == [
        ("fsync", "file"),
        ("fsync", "dir"),
        ("flush", backend.root.name),
    ]
    assert [key for kind, key in calls if kind == "delete"] == [
        syncthing_record_key(live_id),
        device_key(live_id),
    ]
    records = devices.read_retirement_records(backend, PASSPHRASE, TEST_MEMORY_KB).records
    assert records[live_id].syncthing_device_id == live_syncthing
    assert records[old_id].syncthing_device_id == old_syncthing

    # Reactivation and retirement again prefer the present authenticated binding.
    register_device(backend, old_id, "mac-b")
    devices.publish_record(backend, old_id, live_syncthing, PASSPHRASE, TEST_MEMORY_KB)
    result = runner.invoke(app, ["devices", "retire", old_id, "--yes"])
    assert result.exit_code == 0, result.output
    records = devices.read_retirement_records(backend, PASSPHRASE, TEST_MEMORY_KB).records
    assert records[old_id].syncthing_device_id == live_syncthing


def test_retire_does_not_backfill_config_or_follow_namespace_symlink(tmp_path, monkeypatch):
    backend = _setup_retire_fleet(tmp_path, monkeypatch)
    target = "bbbb2222"
    register_device(backend, target, "mac-b")
    legacy = config.load_config()
    legacy["crypto"].pop("root_salt_fp")
    config.save_config(legacy)
    before = config.CONFIG_PATH.read_bytes()

    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: False)
    result = runner.invoke(app, ["devices", "retire", target])
    assert result.exit_code == 1
    assert "confirming needs a terminal" in result.output
    assert config.CONFIG_PATH.read_bytes() == before

    # A synced link cannot turn a retirement's unlink into an external deletion.
    external = tmp_path / "unrelated"
    (backend.root / "syncthing").rename(external)
    sentinel = external / f"{target}.enc"
    sentinel.write_bytes(b"unrelated data")
    (backend.root / "syncthing").symlink_to(external, target_is_directory=True)
    result = runner.invoke(app, ["devices", "retire", target, "--yes"])
    assert result.exit_code == 1
    assert sentinel.read_bytes() == b"unrelated data"
    assert backend.exists(device_key(target))
    assert not backend.exists(retired_record_key(target))
    assert config.CONFIG_PATH.read_bytes() == before


def test_retire_refuses_if_syncthing_record_arrives_during_confirmation(tmp_path, monkeypatch):
    backend = _setup_retire_fleet(tmp_path, monkeypatch)
    target = "bbbb2222"
    register_device(backend, target, "mac-b")

    def publish_before_confirming(prompt, *, default):
        assert not default
        devices.publish_record(
            backend, target, _valid_syncthing_id("B"), PASSPHRASE, TEST_MEMORY_KB
        )
        return True

    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)
    monkeypatch.setattr(core, "ask_yes_no", publish_before_confirming)
    result = runner.invoke(app, ["devices", "retire", "mac-b"])
    assert result.exit_code == 1
    assert "retirement target changed during confirmation" in result.output
    assert not backend.exists(retired_record_key(target))
    assert backend.exists(syncthing_record_key(target))
    assert backend.exists(device_key(target))


def test_retire_by_saved_name_finishes_when_registry_entry_is_unreadable(tmp_path, monkeypatch):
    backend = _setup_retire_fleet(tmp_path, monkeypatch)
    retired_id = _valid_syncthing_id("B")
    devices.write_retirement_record(
        backend, "bbbb2222", "old-box", retired_id, PASSPHRASE, TEST_MEMORY_KB
    )
    backend.put(device_key("bbbb2222"), b"unreadable registry")
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)
    result = runner.invoke(app, ["devices", "retire", "old-box"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Retire old-box (bbbb2222)? [y/n] (Enter = n)" in result.output
    assert "Retire: retired old-box (bbbb2222)" in result.output
    assert not backend.exists(device_key("bbbb2222"))
