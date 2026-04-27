"""Shared pytest fixtures for Mind Meld tests.

All tests that call crypto.encrypt / crypto.decrypt need an active crypto
session. Rather than hand-wire one in every test, an autouse fixture here
sets a fixed root_salt and a low Argon2 memory cost so unit/integration tests
run fast.

Tests that specifically want to test session behavior (drift detection,
missing session, bootstrap) can call crypto.clear_crypto_session() inside
their test body and then re-configure.
"""

from __future__ import annotations

import pytest

from mind_meld import crypto

# Fixed, arbitrary 16-byte root_salt for the default test session. Not secret.
_TEST_ROOT_SALT = bytes(range(16))
# Low Argon2 cost so tests run in <1s per call. Production default is 65_536.
_TEST_MEMORY_KB = 1024


@pytest.fixture(autouse=True)
def _default_crypto_session() -> None:
    """Pin a default crypto session before each test; clear after.

    Ensures encrypt/decrypt work without per-test boilerplate. Tests that
    assert on fresh-session behavior should call crypto.clear_crypto_session()
    inside their test body to reset.
    """
    crypto.clear_crypto_session()
    crypto.set_crypto_session(_TEST_ROOT_SALT, _TEST_MEMORY_KB)
    yield
    crypto.clear_crypto_session()


@pytest.fixture(autouse=True)
def _isolate_keyring(monkeypatch) -> None:
    """Prevent tests from reading the real OS Keychain.

    get_passphrase() checks keyring first; a stale passphrase stored from a
    real mm session would override test env vars and make integration tests
    nondeterministic. Force keyring to report "not available" in every test.
    """
    monkeypatch.setattr("keyring.get_password", lambda *a, **kw: None)
    monkeypatch.setattr("keyring.set_password", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _isolate_devices_write_lock(monkeypatch, tmp_path) -> None:
    """Redirect devices/<id>.json write-lock to a per-test path.

    `update_last_seen` (and any future RMW mutator) routes through
    `mind_meld.devices._devices_write_lock()`, which flocks
    `DEVICES_WRITE_LOCK`. Without redirection, every test creates the
    sentinel in the real `~/.config/mind-meld/`. The flock itself is
    process-local so cross-test contention isn't a concern, but leaking
    files into the user's home dir is bad hygiene.

    Import devices explicitly so monkeypatch can resolve the dotted path
    even in tests (test_version, test_wheel) that don't otherwise touch it.
    """
    from mind_meld import devices as _devices

    monkeypatch.setattr(_devices, "DEVICES_WRITE_LOCK", tmp_path / "devices-write.lock")


@pytest.fixture
def test_root_salt() -> bytes:
    """Exported for tests that need the fixture's root_salt explicitly."""
    return _TEST_ROOT_SALT


@pytest.fixture
def test_memory_kb() -> int:
    """Exported for tests that need the fixture's memory_kb explicitly."""
    return _TEST_MEMORY_KB


# ─── Shared CLI integration helpers ──────────────────────────────────────
#
# Plain module-level functions imported by tests that drive the CLI via
# CliRunner (test_track_1a.py, test_track_1c.py). Previously lived in
# test_track_1a.py and were cross-imported — moved here so both test
# modules pull from a stable, canonical location.

PASSPHRASE = "track-1a-test-passphrase"
MEMORY_KB = 1024


def _make_config(tmp_path, storage_dir, claude_dir, device_id="dev-a", device_name="Mac A"):
    """Write a valid config.toml for a single-claude-source setup."""
    from mind_meld.config import save_config

    config_path = tmp_path / "config.toml"
    config = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"path": str(storage_dir)},
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
            ],
        },
        "crypto": {"argon2_memory_kb": MEMORY_KB},
    }
    save_config(config, config_path)
    return config_path, config


def _populate_claude(claude_dir, contents: str = "Data scientist") -> None:
    """Populate a claude_dir with a single memory file so push has work to do."""
    memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
    memory.mkdir(parents=True)
    (memory / "role.md").write_text(contents)


def _redirect_sidecar(monkeypatch, tmp_path):
    """Redirect SIDECAR_DIR to a tmp path so log writes are hermetic.

    `_auto_log_path` reads `sidecar.SIDECAR_DIR` at call time, so patching
    the one constant is sufficient — no `mind_meld.cli._AUTO_LOG_DIR` alias
    to keep in sync.
    """
    iso = tmp_path / "sidecar"
    iso.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", iso)
    return iso


def _redirect_lock(monkeypatch, tmp_path):
    lp = tmp_path / "test.lock"
    monkeypatch.setattr("mind_meld.config.LOCK_PATH", lp)
    monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", lp)
    return lp


def _setup_real_config(tmp_path, monkeypatch):
    """Full working config so we reach _pull_core / _push_core."""
    from mind_meld.crypto import bootstrap_crypto_init
    from mind_meld.devices import register_device
    from mind_meld.storage.local import LocalBackend

    storage_dir = tmp_path / "storage"
    claude_dir = tmp_path / ".claude"
    _populate_claude(claude_dir)
    config_path, _ = _make_config(tmp_path, storage_dir, claude_dir)

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-a", "Mac A")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    return claude_dir
