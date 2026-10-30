"""CLI-free init contract for Track 88A, extended for Track 89A.

Drives `initflow.init` through a test-local Typer app, so exit codes are real
and `mind_meld.cli` is never imported. The folder state (empty versus a
bootstrapped fleet) is built with the crypto/devices/storage leaves; no init
decision is mocked. Stubbed because they act outside the storage folder:
`events_tail._run_events_backfill` and `skill_link._ensure_retro_skill_links`
(history walk and skill symlinks), and Argon2 drops to 1024 KiB for speed
(`TestInitFlow` keeps the real 65536 KiB). The keyring stays with conftest's
`_isolate_keyring` and the `PYTEST_CURRENT_TEST` guard on
`crypto.store_passphrase_in_keyring`. PASSPHRASE is a synthetic test credential.

Input order follows `initflow.init`: storage path, then the re-init confirm
if a device is already configured, device name, passphrase(s), then six
source prompts. `--new-fleet` is the only flow that creates the key header;
plain `mm init` joins and stops when the header has not arrived. The join's
pull lives in the `cli.init` shell and is covered in tests/test_integration.py.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import subprocess
import sys
import tempfile
import tomllib

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import config, crypto, devices, events_tail, fsutil, initflow, skill_link
from mind_meld.errors import StorageError
from mind_meld.storage.keys import blob_key
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "initflow-contract-test-only"
MEMORY_KB = 1024
PEER = "bbbbbbbb"
SOURCES_CLAUDE_ONLY = "Y\nn\nn\nn\nn\nn\n"
SOURCES_NONE = "n\nn\nn\nn\nn\nn\n"
NEW_FLEET_OUTCOME = (
    "as the root of a Syncthing folder, run 'mm init' on the other machines, and run 'mm push'."
)
JOIN_OUTCOME = (
    "Pulling now; if the folder is still syncing, run 'mm pull' again when delivery finishes."
)
STIGNORE = f"{initflow.STIGNORE_LINE}\n"

# `init` becomes addressable as a subcommand once the app has a callback.
_app = typer.Typer()
_joined: list[str | None] = []


@_app.callback()
def _root() -> None:
    """No-op group callback."""


_app.command("init")(initflow.init)


@_app.command("capture")
def _capture(new_fleet: bool = False) -> None:
    _joined.append(initflow.init(new_fleet=new_fleet))


@pytest.fixture
def env(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(initflow, "DEFAULT_ARGON2_MEMORY_KB", MEMORY_KB)
    monkeypatch.setattr(events_tail, "_run_events_backfill", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_link, "_ensure_retro_skill_links", lambda **kwargs: ())
    _joined.clear()
    return storage, LocalBackend(storage)


def _run(stdin: str, *args: str, command: str = "init"):
    result = CliRunner().invoke(_app, [command, *args], input=stdin)
    return result, " ".join(result.output.split())


def _new_fleet(stdin: str):
    return _run(stdin, "--new-fleet")


def _populate(backend: LocalBackend) -> None:
    crypto.bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
    devices.register_device(backend, PEER, "Peer B")


def _device_keys(backend: LocalBackend) -> list[str]:
    return sorted(backend.list_keys("devices/"))


def _tree(root):
    """Every name under root with its bytes (None for a directory)."""
    if not root.exists():
        return None
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def _assert_nothing_written(storage, before):
    assert _tree(storage) == before
    assert not config.CONFIG_PATH.exists()


# ── --new-fleet ──────────────────────────────────────────────────────


def test_new_fleet_empty_folder(env):
    storage, backend = env
    result, text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 0, result.output
    assert "mm-crypto-init bootstrapped" in text
    assert "Device registered: Mac A" in text
    assert text.endswith(NEW_FLEET_OUTCOME)
    assert backend.exists("mm-crypto-init")
    assert (storage / ".stignore").read_text() == STIGNORE
    assert len(_device_keys(backend)) == 1
    saved = tomllib.loads(config.CONFIG_PATH.read_text())
    stored_salt = crypto.fetch_crypto_init(backend).root_salt
    assert saved["crypto"]["root_salt_fp"] == crypto.root_salt_fingerprint(stored_salt)


def test_new_fleet_creates_an_absent_folder(env, tmp_path):
    _storage, _backend = env
    target = tmp_path / "does" / "not" / "exist"
    result, _text = _new_fleet(f"{target}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 0, result.output
    assert (target / "mm-crypto-init").is_file()
    assert (target / ".stignore").read_text() == STIGNORE


def test_new_fleet_accepts_syncthing_bookkeeping_and_keeps_owner_lines(env):
    storage, backend = env
    (storage / ".stfolder").mkdir()
    (storage / ".stversions").mkdir()
    (storage / ".DS_Store").write_bytes(b"x")
    (storage / ".stignore").write_bytes(b"// owner rule\n*.log")
    result, _text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 0, result.output
    assert backend.exists("mm-crypto-init")
    assert (storage / ".stignore").read_bytes() == b"// owner rule\n*.log\n" + STIGNORE.encode()


@pytest.mark.parametrize(
    ("entry", "is_dir", "remedy"),
    [
        (".syncthing.mm-crypto-init.tmp", False, "Wait for delivery to finish, then run 'mm init'"),
        ("~syncthing~mm-crypto-init.tmp", False, "Wait for delivery to finish, then run 'mm init'"),
        ("tmpab12cd34.tmp", False, "Move them out or choose an empty folder"),
        ("mm-crypto-init", False, "run 'mm init' to join it"),
        ("mm-crypto-init 2", False, "run 'mm init' to join it"),
        ("mm-crypto-init.sync-conflict-20260101-101010-ABCDEFG", False, "run 'mm init' to join"),
        ("mm-crypto-init.preserved-ab12cd34-20260101T000000Z", False, "run 'mm init' to join"),
        ("data", True, "run 'mm init' to join it"),
        ("manifests", True, "run 'mm init' to join it"),
        ("devices", True, "run 'mm init' to join it"),
        ("notes.txt", False, "Move them out or choose an empty folder"),
    ],
)
def test_new_fleet_refuses_every_other_entry_and_writes_nothing(env, entry, is_dir, remedy):
    storage, _backend = env
    (storage / ".stfolder").mkdir()
    if is_dir:
        (storage / entry).mkdir()
    else:
        (storage / entry).write_bytes(b"not ours")
    before = _tree(storage)
    result, text = _new_fleet(f"{storage}\n")
    assert result.exit_code == 1, result.output
    assert "--new-fleet needs an empty folder" in text
    assert "Nothing was written" in text
    assert remedy in text
    assert "delete" not in text.replace("Nothing was deleted", "")
    _assert_nothing_written(storage, before)


def test_new_fleet_names_at_most_five_blockers(env):
    storage, _backend = env
    for i in range(8):
        (storage / f"note-{i}.txt").write_bytes(b"x")
    result, text = _new_fleet(f"{storage}\n")
    assert result.exit_code == 1
    assert "'note-0.txt'" in text and "'note-4.txt'" in text
    assert "'note-5.txt'" not in text
    assert "and 3 more" in text


def test_new_fleet_entry_names_cannot_forge_a_line(env):
    storage, _backend = env
    (storage / "evil\nRun: mm init").write_bytes(b"x")
    result, text = _new_fleet(f"{storage}\n")
    assert result.exit_code == 1
    assert "\\n" in text
    assert "\nRun: mm init" not in result.output


@pytest.mark.parametrize("flags", [(), ("--new-fleet",)])
def test_a_file_at_the_storage_path_is_reported_on_both_flows(env, tmp_path, flags):
    _storage, _backend = env
    target = tmp_path / "a-file"
    target.write_bytes(b"x")
    result, text = _run(f"{target}\n", *flags)
    assert result.exit_code == 1, result.output
    assert "cannot read the storage folder" in text
    assert "Nothing was written" in text
    assert target.read_bytes() == b"x"
    assert not config.CONFIG_PATH.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("flags", [(), ("--new-fleet",)])
def test_an_unreadable_folder_is_reported_on_both_flows(env, flags):
    storage, _backend = env
    storage.chmod(0o000)
    try:
        result, text = _run(f"{storage}\n", *flags)
    finally:
        storage.chmod(0o755)
    assert result.exit_code == 1, result.output
    assert "cannot read the storage folder" in text
    assert not config.CONFIG_PATH.exists()


def test_new_fleet_refuses_a_header_that_arrives_during_the_prompts(env, monkeypatch):
    storage, backend = env
    original_prompt = initflow._prompt_passphrase
    peer_header = {}

    def prompt_then_header_arrives(new_fleet):
        passphrase = original_prompt(new_fleet)
        crypto.bootstrap_crypto_init(backend, "peer-passphrase", MEMORY_KB)
        peer_header["bytes"] = (storage / "mm-crypto-init").read_bytes()
        return passphrase

    monkeypatch.setattr(initflow, "_prompt_passphrase", prompt_then_header_arrives)
    result, text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 1, result.output
    assert "run 'mm init'" in text
    assert (storage / "mm-crypto-init").read_bytes() == peer_header["bytes"]
    assert not (storage / ".stignore").exists()
    assert _device_keys(backend) == []
    assert not config.CONFIG_PATH.exists()


def test_new_fleet_refuses_when_the_exclusive_create_loses(env, monkeypatch):
    storage, backend = env
    real = initflow.bootstrap_crypto_init
    peer_header = {}

    def lose_the_race(be, passphrase, argon2_memory_kb):
        crypto.bootstrap_crypto_init(be, "peer-passphrase", MEMORY_KB)
        peer_header["bytes"] = (storage / "mm-crypto-init").read_bytes()
        return real(be, passphrase, argon2_memory_kb=argon2_memory_kb)

    monkeypatch.setattr(initflow, "bootstrap_crypto_init", lose_the_race)
    result, text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 1, result.output
    assert "first, so none was created here" in text
    assert "Run 'mm init' to join it" in text
    assert (storage / "mm-crypto-init").read_bytes() == peer_header["bytes"]
    assert (storage / ".stignore").read_text() == STIGNORE
    assert _device_keys(backend) == []
    assert not config.CONFIG_PATH.exists()


@pytest.mark.parametrize("failure", [errno.ENOSPC, errno.EACCES])
def test_an_oserror_during_the_exclusive_create_is_not_a_lost_race(env, monkeypatch, failure):
    storage, backend = env
    calls = []

    def fail(self, key, data):
        calls.append(key)
        raise OSError(failure, os.strerror(failure))

    # A context, never monkeypatch.undo(): undo would also strip conftest's autouse
    # isolation (upgrade cache, sidecar, keyring) and the rerun would reach real state.
    with monkeypatch.context() as patch:
        patch.setattr(LocalBackend, "put_exclusive", fail)
        result, text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 1, result.output
    assert calls == ["mm-crypto-init"]
    assert "could not create the key header" in text
    assert "No key header was created" in text
    assert "mm init --new-fleet" in text
    assert "first, so none was created here" not in text
    assert not backend.exists("mm-crypto-init")
    assert not config.CONFIG_PATH.exists()
    # A leftover .stignore never blocks the rerun.
    assert (storage / ".stignore").exists()
    rerun, _text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert rerun.exit_code == 0, rerun.output
    assert backend.exists("mm-crypto-init")


def test_new_fleet_stignore_that_is_a_directory_exits_cleanly(env):
    storage, _backend = env
    (storage / ".stignore").mkdir()
    result, text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 1, result.output
    assert "could not write .stignore" in text
    assert "No key header was created" in text
    assert "Traceback" not in result.output
    assert not (storage / "mm-crypto-init").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_new_fleet_stignore_in_a_read_only_folder_exits_cleanly(env):
    storage, _backend = env
    storage.chmod(0o555)
    try:
        result, text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    finally:
        storage.chmod(0o755)
    assert result.exit_code == 1, result.output
    assert "could not write .stignore" in text
    assert "Traceback" not in result.output
    assert not config.CONFIG_PATH.exists()


def test_new_fleet_every_source_declined_is_recoverable_by_plain_init(env):
    storage, backend = env
    result, text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_NONE}")
    assert result.exit_code == 1, result.output
    assert "no sync sources enabled" in text
    assert not config.CONFIG_PATH.exists()
    assert _device_keys(backend) == []
    # The header this machine wrote verifies for plain init with the same passphrase.
    joined, _text = _run(f"{storage}\nMac A\npw\n{SOURCES_CLAUDE_ONLY}")
    assert joined.exit_code == 0, joined.output


def test_new_fleet_on_a_configured_machine_names_the_store_it_would_replace(env):
    storage, backend = env
    first, _text = _new_fleet(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert first.exit_code == 0, first.output
    saved = config.CONFIG_PATH.read_text()
    fingerprint = tomllib.loads(saved)["crypto"]["root_salt_fp"]
    result, text = _new_fleet("n\n")
    assert result.exit_code == 0, result.output
    assert fingerprint in text
    assert "separate fleet" in text
    assert config.CONFIG_PATH.read_text() == saved
    assert len(_device_keys(backend)) == 1


# ── .stignore writer ─────────────────────────────────────────────────


def test_stignore_fresh_file_rerun_and_existing_lines(tmp_path):
    path = tmp_path / "folder" / ".stignore"
    initflow._write_stignore(path)
    assert path.read_text() == STIGNORE
    stat_before = path.stat()
    initflow._write_stignore(path)
    assert path.read_text() == STIGNORE
    assert path.stat().st_mtime_ns == stat_before.st_mtime_ns

    owned = tmp_path / "owned" / ".stignore"
    owned.parent.mkdir()
    owned.write_bytes(b"// mine\n#include shared\n")
    initflow._write_stignore(owned)
    assert owned.read_bytes() == b"// mine\n#include shared\n" + STIGNORE.encode()


def test_stignore_pattern_matches_the_temp_names_mm_creates(tmp_path):
    fd, name = tempfile.mkstemp(dir=tmp_path, suffix=".tmp")
    os.close(fd)
    assert fnmatch.fnmatch(os.path.basename(name), initflow.STIGNORE_PATTERN)
    assert initflow.STIGNORE_LINE == f"(?d){initflow.STIGNORE_PATTERN}"


def test_stignore_failures_are_oserror_or_storageerror(tmp_path):
    (tmp_path / ".stignore").mkdir()
    with pytest.raises((OSError, StorageError)):
        initflow._write_stignore(tmp_path / ".stignore")


def test_stignore_writer_guards_before_its_first_mutation(synthetic_account_home, monkeypatch):
    target = synthetic_account_home / "Sync" / "mind-meld" / ".stignore"
    with pytest.raises(fsutil.PytestRealStateWriteError):
        initflow._write_stignore(target)
    assert not target.parent.exists()


# ── plain init: join ─────────────────────────────────────────────────


def test_join_verifies_registers_and_hands_over_the_passphrase(env):
    storage, backend = env
    _populate(backend)
    before = (storage / "mm-crypto-init").read_bytes()
    result, text = _run(f"{storage}\nMac B\n{PASSPHRASE}\n{SOURCES_CLAUDE_ONLY}", command="capture")
    assert result.exit_code == 0, result.output
    assert _joined == [PASSPHRASE]
    assert "Verified passphrase against existing mm-crypto-init" in text
    assert text.endswith(JOIN_OUTCOME)
    assert len(_device_keys(backend)) == 2
    assert (storage / "mm-crypto-init").read_bytes() == before
    assert not (storage / ".stignore").exists()
    saved = tomllib.loads(config.CONFIG_PATH.read_text())
    existing_salt = crypto.fetch_crypto_init(backend).root_salt
    assert saved["crypto"]["root_salt_fp"] == crypto.root_salt_fingerprint(existing_salt)


def test_new_fleet_hands_over_no_passphrase(env):
    storage, _backend = env
    result, _text = _run(
        f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}", "--new-fleet", command="capture"
    )
    assert result.exit_code == 0, result.output
    assert _joined == [None]


def test_a_fresh_machine_joining_a_populated_folder_is_not_asked(env):
    storage, backend = env
    _populate(backend)
    # No confirm line in the input: any prompt would consume a wrong answer.
    result, text = _run(f"{storage}\nMac B\n{PASSPHRASE}\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 0, result.output
    assert "Proceed?" not in text


def test_a_configured_machine_declining_re_init_changes_nothing(env):
    storage, backend = env
    _populate(backend)
    first, _text = _run(f"{storage}\nMac B\n{PASSPHRASE}\n{SOURCES_CLAUDE_ONLY}")
    assert first.exit_code == 0, first.output
    saved = config.CONFIG_PATH.read_bytes()
    result, text = _run(f"y\n{storage}\nn\n")
    assert result.exit_code == 0, result.output
    assert "orphaning existing device" in text
    assert "Nothing changed." in text
    assert config.CONFIG_PATH.read_bytes() == saved
    assert len(_device_keys(backend)) == 2


def test_populated_folder_join_overwrite_declined(env):
    storage, backend = env
    _populate(backend)
    first, _text = _run(f"{storage}\nMac B\n{PASSPHRASE}\n{SOURCES_CLAUDE_ONLY}")
    assert first.exit_code == 0, first.output
    result, _text = _run("n\n")
    assert result.exit_code == 0, result.output
    assert len(_device_keys(backend)) == 2


def test_populated_folder_wrong_passphrase(env):
    storage, backend = env
    _populate(backend)
    result, text = _run(f"{storage}\nMac B\nnot-the-passphrase\n")
    assert result.exit_code == 1, result.output
    assert "does not match" in text
    assert "another machine" in text
    assert not config.CONFIG_PATH.exists()
    assert len(_device_keys(backend)) == 1


def test_a_corrupt_key_header_names_the_folder(env):
    storage, _backend = env
    (storage / "mm-crypto-init").write_bytes(b"garbage")
    result, text = _run(f"{storage}\n")
    assert result.exit_code == 1, result.output
    assert "is corrupt" in text
    assert "never reinitialize" in text
    assert "iCloud" not in text
    assert not config.CONFIG_PATH.exists()


# ── plain init: no key header ────────────────────────────────────────


def test_plain_init_on_an_absent_folder_stops_and_creates_nothing(env, tmp_path):
    _storage, _backend = env
    target = tmp_path / "not-yet"
    result, text = _run(f"{target}\n")
    assert result.exit_code == 1, result.output
    assert "does not exist" in text
    assert "Nothing was written" in text
    assert "Accept the shared folder in Syncthing" in text
    assert not target.exists()
    assert not config.CONFIG_PATH.exists()


def test_plain_init_on_a_bookkeeping_only_folder_reserves_new_fleet(env):
    storage, _backend = env
    (storage / ".stfolder").mkdir()
    before = _tree(storage)
    result, text = _run(f"{storage}\n")
    assert result.exit_code == 1, result.output
    assert "not in" in text and "yet" in text
    assert "Nothing was written" in text
    assert "wait for Syncthing to deliver it, then run 'mm init' again" in text
    assert "Only the fleet's first machine runs 'mm init --new-fleet'" in text
    _assert_nothing_written(storage, before)


def test_plain_init_on_a_folder_with_blobs_never_offers_new_fleet(env):
    storage, backend = env
    backend.put(blob_key(PEER, "a" * 64), b"opaque")
    before = _tree(storage)
    result, text = _run(f"{storage}\n")
    assert result.exit_code == 1, result.output
    assert "Aborted." not in text
    assert "holds fleet files" in text
    assert "restore it from another machine" in text
    assert "--new-fleet" not in text
    assert not backend.exists("mm-crypto-init")
    _assert_nothing_written(storage, before)


def test_plain_init_mid_delivery_never_offers_new_fleet(env):
    storage, _backend = env
    (storage / ".syncthing.mm-crypto-init.tmp").write_bytes(b"partial")
    before = _tree(storage)
    result, text = _run(f"{storage}\n")
    assert result.exit_code == 1, result.output
    assert "still receiving files from Syncthing" in text
    assert "--new-fleet" not in text
    _assert_nothing_written(storage, before)


def test_plain_init_with_unrelated_files_never_offers_new_fleet(env):
    storage, _backend = env
    (storage / "notes.txt").write_bytes(b"x")
    result, text = _run(f"{storage}\n")
    assert result.exit_code == 1, result.output
    assert "'notes.txt'" in text
    assert "--new-fleet" not in text


def test_initflow_source_has_no_icloud_wording():
    assert "iCloud" not in initflow.__spec__.loader.get_source("mind_meld.initflow")


def test_initflow_contract_import_does_not_register_cli():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tests.test_initflow; import sys; assert 'mind_meld.cli' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
