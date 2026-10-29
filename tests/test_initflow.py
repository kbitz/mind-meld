"""CLI-free init contract for Track 88A.

Drives `initflow.init` through a test-local Typer app, so exit codes are real
and `mind_meld.cli` is never imported. The folder state (empty versus a
bootstrapped fleet) is built with the crypto/devices/storage leaves; no init
decision is mocked. Stubbed because they act outside the storage folder:
`events_tail._run_events_backfill` and `skill_link._ensure_retro_skill_links`
(history walk and skill symlinks), and Argon2 drops to 1024 KiB for speed
(`TestInitFlow` keeps the real 65536 KiB). The keyring stays with conftest's
`_isolate_keyring` and the `PYTEST_CURRENT_TEST` guard on
`crypto.store_passphrase_in_keyring`. PASSPHRASE is a synthetic test credential.

Input order follows `initflow.init`: storage path, then the orphan or BRICK
prompt if any, device name, passphrase(s), then six source prompts.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import config, crypto, devices, events_tail, initflow, skill_link
from mind_meld.storage.keys import blob_key
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "initflow-contract-test-only"
MEMORY_KB = 1024
PEER = "bbbbbbbb"
SOURCES_CLAUDE_ONLY = "Y\nn\nn\nn\nn\nn\n"
SOURCES_NONE = "n\nn\nn\nn\nn\nn\n"
OUTCOME = "Mind Meld initialized. Run 'mm push' to sync."

# `init` becomes addressable as a subcommand once the app has a callback.
_app = typer.Typer()


@_app.callback()
def _root() -> None:
    """No-op group callback."""


_app.command("init")(initflow.init)


@pytest.fixture
def env(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(initflow, "DEFAULT_ARGON2_MEMORY_KB", MEMORY_KB)
    monkeypatch.setattr(events_tail, "_run_events_backfill", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_link, "_ensure_retro_skill_links", lambda **kwargs: ())
    return storage, LocalBackend(storage)


def _run(stdin: str):
    result = CliRunner().invoke(_app, ["init"], input=stdin)
    return result, " ".join(result.output.split())


def _populate(backend: LocalBackend) -> None:
    crypto.bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
    devices.register_device(backend, PEER, "Peer B")


def _device_keys(backend: LocalBackend) -> list[str]:
    return sorted(backend.list_keys("devices/"))


def test_empty_folder_first_device(env):
    storage, backend = env
    result, text = _run(f"{storage}\nMac A\npw\npw\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 0, result.output
    assert "mm-crypto-init bootstrapped" in text
    assert "Device registered: Mac A" in text
    assert text.endswith(OUTCOME)
    assert backend.exists("mm-crypto-init")
    assert len(_device_keys(backend)) == 1
    saved = tomllib.loads(config.CONFIG_PATH.read_text())
    stored_salt = crypto.fetch_crypto_init(backend).root_salt
    assert saved["crypto"]["root_salt_fp"] == crypto.root_salt_fingerprint(stored_salt)


def test_empty_folder_every_source_declined(env):
    storage, backend = env
    result, text = _run(f"{storage}\nMac A\npw\npw\n{SOURCES_NONE}")
    assert result.exit_code == 1, result.output
    assert "no sync sources enabled" in text
    assert not config.CONFIG_PATH.exists()
    assert _device_keys(backend) == []


def test_populated_folder_join_confirmed(env):
    storage, backend = env
    _populate(backend)
    before = (storage / "mm-crypto-init").read_bytes()
    result, text = _run(f"{storage}\ny\nMac B\n{PASSPHRASE}\n{SOURCES_CLAUDE_ONLY}")
    assert result.exit_code == 0, result.output
    assert "Verified passphrase against existing mm-crypto-init" in text
    assert text.endswith(OUTCOME)
    assert len(_device_keys(backend)) == 2
    assert (storage / "mm-crypto-init").read_bytes() == before
    saved = tomllib.loads(config.CONFIG_PATH.read_text())
    existing_salt = crypto.fetch_crypto_init(backend).root_salt
    assert saved["crypto"]["root_salt_fp"] == crypto.root_salt_fingerprint(existing_salt)


def test_populated_folder_join_declined(env):
    storage, backend = env
    _populate(backend)
    result, _text = _run(f"{storage}\nn\n")
    assert result.exit_code == 0, result.output
    assert not config.CONFIG_PATH.exists()
    assert len(_device_keys(backend)) == 1


def test_populated_folder_wrong_passphrase(env):
    storage, backend = env
    _populate(backend)
    result, text = _run(f"{storage}\ny\nMac B\nnot-the-passphrase\n")
    assert result.exit_code == 1, result.output
    assert "does not match" in text
    assert not config.CONFIG_PATH.exists()
    assert len(_device_keys(backend)) == 1


def test_populated_folder_without_key_header_refuses_without_brick(env):
    storage, backend = env
    backend.put(blob_key(PEER, "a" * 64), b"opaque")
    result, text = _run(f"{storage}\nnot-brick\n")
    assert result.exit_code == 1, result.output
    assert "Aborted." in text
    assert not backend.exists("mm-crypto-init")
    assert not config.CONFIG_PATH.exists()


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
