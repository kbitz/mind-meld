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


@pytest.fixture
def test_root_salt() -> bytes:
    """Exported for tests that need the fixture's root_salt explicitly."""
    return _TEST_ROOT_SALT


@pytest.fixture
def test_memory_kb() -> int:
    """Exported for tests that need the fixture's memory_kb explicitly."""
    return _TEST_MEMORY_KB
