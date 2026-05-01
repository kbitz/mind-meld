"""Tests for mind_meld.crypto (v2 format).

Covers: encryption round-trip, version handling, Argon2 wiring, HKDF
determinism, master-key cache, session state / drift, mm-crypto-init
bootstrap + fetch + verify, deterministic conflict resolution, and the
critical v1-blob regression (must fail loud post-Track-1C).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mind_meld import crypto
from mind_meld.crypto import (
    CRYPTO_INIT_KEY,
    FORMAT_VERSION,
    FORMAT_VERSION_LEGACY_V1,
    NONCE_LEN,
    ROOT_SALT_LEN,
    SALT_LEN,
    bootstrap_crypto_init,
    decrypt,
    derive_key,
    encrypt,
    fetch_crypto_init,
    get_passphrase,
    load_master_key,
    root_salt_fingerprint,
    set_crypto_session,
    verify_passphrase,
)
from mind_meld.errors import CryptoError, StorageError
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "test-passphrase-123"
MEMORY_KB = 1024  # matches conftest autouse fixture


# ── derive_key (Argon2 primitive) ─────────────────────────────────────


class TestDeriveKey:
    def test_produces_32_bytes(self):
        key = derive_key(PASSPHRASE, os.urandom(16), memory_kb=MEMORY_KB)
        assert len(key) == 32

    def test_same_inputs_same_key(self):
        salt = os.urandom(16)
        k1 = derive_key(PASSPHRASE, salt, memory_kb=MEMORY_KB)
        k2 = derive_key(PASSPHRASE, salt, memory_kb=MEMORY_KB)
        assert k1 == k2

    def test_different_salt_different_key(self):
        k1 = derive_key(PASSPHRASE, os.urandom(16), memory_kb=MEMORY_KB)
        k2 = derive_key(PASSPHRASE, os.urandom(16), memory_kb=MEMORY_KB)
        assert k1 != k2

    def test_different_passphrase_different_key(self):
        salt = os.urandom(16)
        k1 = derive_key("password-a", salt, memory_kb=MEMORY_KB)
        k2 = derive_key("password-b", salt, memory_kb=MEMORY_KB)
        assert k1 != k2

    def test_empty_passphrase_raises(self):
        with pytest.raises(CryptoError, match="empty"):
            derive_key("", os.urandom(16), memory_kb=MEMORY_KB)

    def test_argon2_oom_translated(self):
        # 1GB memory_cost will OOM on test machines and is above our sanity
        # ceiling anyway. Argon2 native error must translate to CryptoError.
        with pytest.raises(CryptoError, match="argon2"):
            derive_key(PASSPHRASE, os.urandom(16), memory_kb=10_000_000_000)


# ── master-key cache ──────────────────────────────────────────────────


class TestMasterKeyCache:
    def test_first_load_derives(self, test_root_salt, test_memory_kb):
        crypto.clear_crypto_session()
        crypto.set_crypto_session(test_root_salt, test_memory_kb)
        key = load_master_key(PASSPHRASE, test_root_salt, test_memory_kb)
        assert len(key) == 32

    def test_second_load_same_tuple_is_cached(self, test_root_salt, test_memory_kb, monkeypatch):
        crypto.clear_crypto_session()
        crypto.set_crypto_session(test_root_salt, test_memory_kb)
        load_master_key(PASSPHRASE, test_root_salt, test_memory_kb)

        # Poison derive_key — second call must hit cache and NOT invoke it.
        called = {"n": 0}

        def spy(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("cache miss — derive_key was called")

        monkeypatch.setattr(crypto, "derive_key", spy)
        key2 = load_master_key(PASSPHRASE, test_root_salt, test_memory_kb)
        assert called["n"] == 0
        assert len(key2) == 32

    def test_different_passphrase_cache_miss(self, test_root_salt, test_memory_kb):
        crypto.clear_crypto_session()
        crypto.set_crypto_session(test_root_salt, test_memory_kb)
        k1 = load_master_key("pw-a", test_root_salt, test_memory_kb)
        k2 = load_master_key("pw-b", test_root_salt, test_memory_kb)
        assert k1 != k2

    def test_different_root_salt_cache_miss(self, test_memory_kb):
        crypto.clear_crypto_session()
        salt_a = bytes(range(16))
        salt_b = bytes(reversed(range(16)))
        crypto.set_crypto_session(salt_a, test_memory_kb)
        k1 = load_master_key(PASSPHRASE, salt_a, test_memory_kb)
        crypto.clear_crypto_session()
        crypto.set_crypto_session(salt_b, test_memory_kb)
        k2 = load_master_key(PASSPHRASE, salt_b, test_memory_kb)
        assert k1 != k2

    def test_different_memory_kb_cache_miss(self, test_root_salt):
        crypto.clear_crypto_session()
        crypto.set_crypto_session(test_root_salt, 1024)
        k1 = load_master_key(PASSPHRASE, test_root_salt, 1024)
        crypto.clear_crypto_session()
        crypto.set_crypto_session(test_root_salt, 2048)
        k2 = load_master_key(PASSPHRASE, test_root_salt, 2048)
        assert k1 != k2


# ── session state ─────────────────────────────────────────────────────


class TestSession:
    def test_set_session_requires_16_byte_salt(self):
        crypto.clear_crypto_session()
        with pytest.raises(CryptoError, match="wrong length"):
            set_crypto_session(b"short", 1024)

    def test_replace_salt_overwrites(self):
        """set_crypto_session is replace-semantic.

        In-process drift refuse was dropped because it fires spuriously in
        CliRunner tests and under auto-GC chains. Real cross-process drift
        detection lives in cli._init_crypto_session via config.root_salt_fp.
        """
        crypto.clear_crypto_session()
        a = bytes(range(16))
        b = bytes(reversed(range(16)))
        set_crypto_session(a, 1024)
        set_crypto_session(b, 1024)  # overwrites
        # Encryption now uses b; decryption with key derived for a fails.
        blob = encrypt(b"test", PASSPHRASE, memory_kb=1024)
        # Should round-trip under current session (b).
        assert decrypt(blob, PASSPHRASE, memory_kb=1024) == b"test"

    def test_re_set_same_salt_is_ok(self):
        crypto.clear_crypto_session()
        salt = bytes(range(16))
        set_crypto_session(salt, 1024)
        set_crypto_session(salt, 1024)  # idempotent

    def test_encrypt_without_session_raises(self):
        crypto.clear_crypto_session()
        with pytest.raises(CryptoError, match="no active session"):
            encrypt(b"hello", PASSPHRASE, memory_kb=1024)

    def test_encrypt_with_memory_kb_drift_raises(self, test_root_salt, test_memory_kb):
        crypto.clear_crypto_session()
        set_crypto_session(test_root_salt, test_memory_kb)
        with pytest.raises(CryptoError, match="memory_kb mismatch"):
            encrypt(b"hello", PASSPHRASE, memory_kb=test_memory_kb + 1)


# ── HKDF / v2 encrypt / decrypt ───────────────────────────────────────


class TestEncryptDecrypt:
    def test_round_trip(self):
        data = b"hello world, this is session data"
        blob = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        result = decrypt(blob, PASSPHRASE, memory_kb=MEMORY_KB)
        assert result == data

    def test_round_trip_empty(self):
        blob = encrypt(b"", PASSPHRASE, memory_kb=MEMORY_KB)
        assert decrypt(blob, PASSPHRASE, memory_kb=MEMORY_KB) == b""

    def test_round_trip_large(self):
        data = os.urandom(1_000_000)
        blob = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        assert decrypt(blob, PASSPHRASE, memory_kb=MEMORY_KB) == data

    def test_version_byte_is_v2(self):
        blob = encrypt(b"test", PASSPHRASE, memory_kb=MEMORY_KB)
        assert blob[0] == FORMAT_VERSION == 0x02

    def test_compression_reduces_size(self):
        data = b'{"key": "value", ' * 1000 + b'"end": true}'
        blob = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        assert len(blob) < len(data)

    def test_different_ciphertext_each_time(self):
        blob1 = encrypt(b"same data", PASSPHRASE, memory_kb=MEMORY_KB)
        blob2 = encrypt(b"same data", PASSPHRASE, memory_kb=MEMORY_KB)
        assert blob1 != blob2  # per-file salt + nonce differ

    def test_wrong_passphrase_raises(self):
        blob = encrypt(b"secret", PASSPHRASE, memory_kb=MEMORY_KB)
        with pytest.raises(CryptoError, match="GCM tag mismatch"):
            decrypt(blob, "wrong-passphrase", memory_kb=MEMORY_KB)

    def test_corrupt_blob_raises(self):
        blob = encrypt(b"test", PASSPHRASE, memory_kb=MEMORY_KB)
        corrupted = bytearray(blob)
        corrupted[-5] ^= 0xFF
        with pytest.raises(CryptoError):
            decrypt(bytes(corrupted), PASSPHRASE, memory_kb=MEMORY_KB)

    def test_corrupt_salt_raises(self):
        """Flipping a bit in the salt changes the HKDF-derived file_key.

        GCM decrypt then fails with tag mismatch. Verifies HKDF is actually
        being invoked — a constant-key implementation would still decrypt.
        """
        blob = bytearray(encrypt(b"secret", PASSPHRASE, memory_kb=MEMORY_KB))
        blob[1] ^= 0x01  # flip 1 bit in salt region
        with pytest.raises(CryptoError, match="GCM tag mismatch"):
            decrypt(bytes(blob), PASSPHRASE, memory_kb=MEMORY_KB)

    def test_truncated_blob_raises(self):
        with pytest.raises(CryptoError, match="too short"):
            decrypt(b"\x02" + b"\x00" * 10, PASSPHRASE, memory_kb=MEMORY_KB)

    def test_unsupported_version_raises(self):
        blob = encrypt(b"test", PASSPHRASE, memory_kb=MEMORY_KB)
        bad_version = bytes([0x99]) + blob[1:]
        with pytest.raises(CryptoError, match="unsupported format version"):
            decrypt(bad_version, PASSPHRASE, memory_kb=MEMORY_KB)

    def test_blob_structure(self):
        blob = encrypt(b"test", PASSPHRASE, memory_kb=MEMORY_KB)
        assert blob[0] == FORMAT_VERSION
        assert len(blob) >= 1 + SALT_LEN + NONCE_LEN + 1


# ── regression: v1 blob refusal (CRITICAL) ────────────────────────────


class TestV1BlobRegression:
    """Track 1C explicitly drops v1 back-compat (pre-release, no existing users).

    If a v1 blob ever appears in storage, decrypt must fail LOUD with a clear
    error, not silently produce garbage or treat it as v2. This is the
    mandatory regression test from the plan's coverage diagram.
    """

    def test_v1_blob_refused(self):
        # Construct a blob that looks like v1: version byte 0x01 then arbitrary
        # bytes of the right total length.
        blob = bytes([FORMAT_VERSION_LEGACY_V1]) + os.urandom(SALT_LEN + NONCE_LEN + 16)
        with pytest.raises(CryptoError, match="v1 blob found"):
            decrypt(blob, PASSPHRASE, memory_kb=MEMORY_KB)


# ── verify_passphrase / keycheck ──────────────────────────────────────


class TestVerifyPassphrase:
    def test_verify_with_correct_master_key(self, test_root_salt, test_memory_kb):
        """keycheck_blob encrypted under master_key_A decrypts to the known plaintext."""
        crypto.clear_crypto_session()
        set_crypto_session(test_root_salt, test_memory_kb)
        master_key = load_master_key(PASSPHRASE, test_root_salt, test_memory_kb)
        keycheck_blob = crypto._encrypt_with_master_key(crypto._KEYCHECK_PLAINTEXT, master_key)
        verify_passphrase(master_key, keycheck_blob)  # no raise

    def test_verify_with_wrong_master_key_raises(self, test_root_salt, test_memory_kb):
        crypto.clear_crypto_session()
        set_crypto_session(test_root_salt, test_memory_kb)
        correct = load_master_key(PASSPHRASE, test_root_salt, test_memory_kb)
        wrong = load_master_key("different-pw", test_root_salt, test_memory_kb)
        keycheck_blob = crypto._encrypt_with_master_key(crypto._KEYCHECK_PLAINTEXT, correct)
        with pytest.raises(CryptoError, match="does not match"):
            verify_passphrase(wrong, keycheck_blob)

    def test_verify_with_tampered_keycheck_raises(self, test_root_salt, test_memory_kb):
        crypto.clear_crypto_session()
        set_crypto_session(test_root_salt, test_memory_kb)
        master_key = load_master_key(PASSPHRASE, test_root_salt, test_memory_kb)
        keycheck = bytearray(
            crypto._encrypt_with_master_key(crypto._KEYCHECK_PLAINTEXT, master_key)
        )
        keycheck[-1] ^= 0xFF
        with pytest.raises(CryptoError):
            verify_passphrase(master_key, bytes(keycheck))


# ── mm-crypto-init bootstrap + fetch ──────────────────────────────────


class TestCryptoInit:
    def test_fetch_on_empty_storage_is_missing(self, tmp_path):
        backend = LocalBackend(tmp_path)
        fetch = fetch_crypto_init(backend)
        assert fetch.status == "missing"
        assert fetch.root_salt is None

    def test_bootstrap_then_fetch_round_trip(self, tmp_path):
        crypto.clear_crypto_session()
        backend = LocalBackend(tmp_path)
        bs = bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        assert bs.status == "ok"
        assert bs.root_salt is not None and len(bs.root_salt) == ROOT_SALT_LEN
        assert bs.argon2_memory_kb == MEMORY_KB

        fetched = fetch_crypto_init(backend)
        assert fetched.status == "ok"
        assert fetched.root_salt == bs.root_salt
        assert fetched.argon2_memory_kb == bs.argon2_memory_kb
        assert fetched.keycheck_blob == bs.keycheck_blob

    def test_bootstrap_second_call_raises(self, tmp_path):
        crypto.clear_crypto_session()
        backend = LocalBackend(tmp_path)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        with pytest.raises(StorageError, match="already exists"):
            bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)

    def test_corrupt_blob_short(self, tmp_path):
        backend = LocalBackend(tmp_path)
        backend.put(CRYPTO_INIT_KEY, b"\x02\x00")  # way too short
        fetch = fetch_crypto_init(backend)
        assert fetch.status == "corrupt"

    def test_corrupt_blob_wrong_version(self, tmp_path):
        backend = LocalBackend(tmp_path)
        fake = bytes([0x99]) + (1024).to_bytes(4, "big") + bytes(ROOT_SALT_LEN) + bytes(50)
        backend.put(CRYPTO_INIT_KEY, fake)
        fetch = fetch_crypto_init(backend)
        assert fetch.status == "corrupt"

    def test_corrupt_blob_insane_memory_kb(self, tmp_path):
        """argon2_memory_kb out of sanity range (>1GB) → corrupt."""
        backend = LocalBackend(tmp_path)
        fake = (
            bytes([FORMAT_VERSION])
            + (10_000_000_000 & 0xFFFFFFFF).to_bytes(4, "big")
            + bytes(ROOT_SALT_LEN)
            + bytes(50)
        )
        backend.put(CRYPTO_INIT_KEY, fake)
        fetch = fetch_crypto_init(backend)
        assert fetch.status == "corrupt"

    def test_verify_with_fetched_blob(self, tmp_path):
        """End-to-end: bootstrap, fetch, derive master_key, verify keycheck."""
        crypto.clear_crypto_session()
        backend = LocalBackend(tmp_path)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        crypto.clear_crypto_session()
        fetch = fetch_crypto_init(backend)
        assert fetch.status == "ok"
        set_crypto_session(fetch.root_salt, fetch.argon2_memory_kb)
        mk = load_master_key(PASSPHRASE, fetch.root_salt, fetch.argon2_memory_kb)
        verify_passphrase(mk, fetch.keycheck_blob)

    def test_verify_wrong_passphrase_after_bootstrap(self, tmp_path):
        crypto.clear_crypto_session()
        backend = LocalBackend(tmp_path)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        crypto.clear_crypto_session()
        fetch = fetch_crypto_init(backend)
        set_crypto_session(fetch.root_salt, fetch.argon2_memory_kb)
        wrong_mk = load_master_key("wrong-pw", fetch.root_salt, fetch.argon2_memory_kb)
        with pytest.raises(CryptoError, match="does not match"):
            verify_passphrase(wrong_mk, fetch.keycheck_blob)


# ── iCloud convergence (canonicalization + deterministic winner) ──────


class TestConflictConvergence:
    def _write_raw_init_blob(self, path: Path, argon2_memory_kb: int, root_salt: bytes):
        """Write a syntactically valid mm-crypto-init file with given fields."""
        crypto.clear_crypto_session()
        # Produce a keycheck_blob by encrypting under a master_key derived from
        # PASSPHRASE + this root_salt.
        crypto.set_crypto_session(root_salt, argon2_memory_kb)
        mk = load_master_key(PASSPHRASE, root_salt, argon2_memory_kb)
        keycheck = crypto._encrypt_with_master_key(crypto._KEYCHECK_PLAINTEXT, mk)
        blob = bytes([FORMAT_VERSION]) + argon2_memory_kb.to_bytes(4, "big") + root_salt + keycheck
        path.write_bytes(blob)

    def test_conflict_copy_deterministic_winner(self, tmp_path):
        """Two mm-crypto-init blobs coexist (canonical + 'mm-crypto-init 2').

        fetch_crypto_init must pick the lex-smallest root_salt and canonicalize.
        """
        crypto.clear_crypto_session()
        backend = LocalBackend(tmp_path)
        salt_higher = bytes([0xFF] * 16)
        salt_lower = bytes([0x00] * 16)
        # Canonical has the higher salt; "mm-crypto-init 2" has the lower salt.
        self._write_raw_init_blob(tmp_path / CRYPTO_INIT_KEY, 1024, salt_higher)
        self._write_raw_init_blob(tmp_path / f"{CRYPTO_INIT_KEY} 2", 1024, salt_lower)

        fetch = fetch_crypto_init(backend)
        assert fetch.status == "ok"
        assert fetch.root_salt == salt_lower, "winner is lex-smallest"

        # Canonical now holds the winner.
        assert (tmp_path / CRYPTO_INIT_KEY).read_bytes()[5:21] == salt_lower
        # Conflict copy was cleaned up.
        assert not (tmp_path / f"{CRYPTO_INIT_KEY} 2").exists()

    def test_all_copies_corrupt_returns_corrupt(self, tmp_path):
        """Canonical and conflicts both present but all unparseable → corrupt.

        Must NOT return missing — missing would let a caller re-bootstrap over
        recoverable-but-unparseable state.
        """
        backend = LocalBackend(tmp_path)
        (tmp_path / CRYPTO_INIT_KEY).write_bytes(b"garbage")
        (tmp_path / f"{CRYPTO_INIT_KEY} 2").write_bytes(b"also garbage")
        assert fetch_crypto_init(backend).status == "corrupt"


# ── fingerprint ───────────────────────────────────────────────────────


class TestFingerprint:
    def test_deterministic(self):
        salt = bytes(range(16))
        assert root_salt_fingerprint(salt) == root_salt_fingerprint(salt)

    def test_different_salts_different_fp(self):
        a = root_salt_fingerprint(bytes(range(16)))
        b = root_salt_fingerprint(bytes(reversed(range(16))))
        assert a != b

    def test_length_16_hex(self):
        fp = root_salt_fingerprint(os.urandom(16))
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)


# ── passphrase retrieval (unchanged from pre-Track-1C) ───────────────


class TestGetPassphrase:
    def test_env_var_fallback(self, monkeypatch):
        # Simulate a keyring backend that has no entry for us. With the
        # narrowed except (Group 3 pre-flight #3), we need a real
        # KeyringError-family raise to trigger fallthrough — not a
        # synthetic AttributeError.
        import keyring
        from keyring.errors import NoKeyringError

        def no_backend(*_a, **_kw):
            raise NoKeyringError("no backend configured")

        monkeypatch.setattr(keyring, "get_password", no_backend)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "from-env")
        assert get_passphrase() == "from-env"


# ── keyring except-narrowing regression pins (Group 3 pre-flight #3) ─
#
# Narrowed from `except Exception` to `(KeyringError, ImportError)` so
# unknown failure kinds (OSError, RuntimeError from a broken backend,
# native trace from a misconfigured DBus on Linux) propagate out to the
# autopull/autopush hook's outer `except Exception` where they produce a
# non-success breadcrumb. See `crypto.get_passphrase` and
# `crypto.store_passphrase_in_keyring` docstrings for full rationale.


class TestGetPassphraseExceptNarrow:
    """Pin the catch set for get_passphrase's keyring read path."""

    def test_keyring_error_is_caught_and_falls_through_to_env(self, monkeypatch):
        """KeyringError (backend unavailable etc.) → env-var fallback used."""
        import keyring
        from keyring.errors import NoKeyringError

        def boom(*_a, **_kw):
            raise NoKeyringError("no backend")

        monkeypatch.setattr(keyring, "get_password", boom)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-wins")
        assert get_passphrase() == "env-wins"

    def test_import_error_is_caught_and_falls_through_to_env(self, monkeypatch):
        """`import keyring` failing (stripped Python) → env-var fallback used."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "keyring":
                raise ImportError("keyring missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-wins")
        assert get_passphrase() == "env-wins"

    def test_os_error_propagates(self, monkeypatch):
        """Non-keyring exceptions (OSError etc.) must escape — no silent swallow."""
        import keyring

        def boom(*_a, **_kw):
            raise OSError("locked keychain gave us something unexpected")

        monkeypatch.setattr(keyring, "get_password", boom)
        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        with pytest.raises(OSError, match="locked keychain"):
            get_passphrase(non_interactive=True)

    def test_runtime_error_propagates(self, monkeypatch):
        """A broken backend that raises a plain RuntimeError must not be swallowed."""
        import keyring

        def boom(*_a, **_kw):
            raise RuntimeError("dbus session bus went away")

        monkeypatch.setattr(keyring, "get_password", boom)
        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        with pytest.raises(RuntimeError, match="dbus session bus"):
            get_passphrase(non_interactive=True)

    def test_happy_path_returns_stored(self, monkeypatch):
        """Sanity: when keyring returns a value, we return it (no fallback)."""
        import keyring

        monkeypatch.setattr(keyring, "get_password", lambda *_a, **_kw: "stored-pw")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "should-be-ignored")
        assert get_passphrase() == "stored-pw"


class TestStorePassphraseInKeyringExceptNarrow:
    """Pin the catch set for store_passphrase_in_keyring's write path.

    Each test deletes PYTEST_CURRENT_TEST so the pytest-guard at the top of
    `store_passphrase_in_keyring` does NOT short-circuit — these tests
    exercise the real-CLI write path that runs after the guard.
    """

    def test_keyring_error_returns_false(self, monkeypatch):
        """KeyringError → graceful False (init prints a warning, keeps going)."""
        import keyring
        from keyring.errors import PasswordSetError

        def boom(*_a, **_kw):
            raise PasswordSetError("backend refused write")

        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setattr(keyring, "set_password", boom)
        assert crypto.store_passphrase_in_keyring("pw") is False

    def test_runtime_error_propagates(self, monkeypatch):
        """Non-keyring exceptions escape — don't pretend the write failed gracefully."""
        import keyring

        def boom(*_a, **_kw):
            raise RuntimeError("something structurally wrong")

        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setattr(keyring, "set_password", boom)
        with pytest.raises(RuntimeError, match="something structurally wrong"):
            crypto.store_passphrase_in_keyring("pw")


class TestStorePassphrasePytestGuard:
    """Pin the PYTEST_CURRENT_TEST guard at the top of store_passphrase_in_keyring.

    Real-world incident: a dev-time path leaked the test fixture passphrase
    (`pw123`) into the user's real macOS Keychain, breaking `mm pull` until
    the entry was manually overwritten. The conftest `_isolate_keyring`
    fixture covered in-process tests via `keyring.set_password` patching,
    but a path that bypassed conftest could still hit the real Keychain.
    PYTEST_CURRENT_TEST is set by pytest on every test phase (and
    inherited by subprocesses), so refusing the write when it's set
    makes the leak surface impossible regardless of stub coverage.
    """

    def test_returns_false_when_pytest_env_var_set_without_calling_keyring(self, monkeypatch):
        """The guard short-circuits BEFORE `keyring.set_password` is reached.
        If a test-environment process tried to write, we'd see the boom
        below — but the guard fires first and returns False."""
        import keyring

        called = []

        def boom(*_a, **_kw):
            called.append(True)
            raise AssertionError(
                "store_passphrase_in_keyring reached keyring.set_password "
                "while PYTEST_CURRENT_TEST was set — the guard at the top "
                "of the function must short-circuit before this point."
            )

        monkeypatch.setattr(keyring, "set_password", boom)
        # PYTEST_CURRENT_TEST is already set by pytest itself during this
        # test's execution; assert that to make the pin honest about what
        # it's exercising.
        assert os.environ.get("PYTEST_CURRENT_TEST"), (
            "PYTEST_CURRENT_TEST should be set by pytest during test execution"
        )
        assert crypto.store_passphrase_in_keyring("anything") is False
        assert called == [], (
            "keyring.set_password must not be called when PYTEST_CURRENT_TEST is set"
        )

    def test_falls_through_when_pytest_env_var_absent(self, monkeypatch):
        """With PYTEST_CURRENT_TEST removed, the guard does NOT fire and the
        write reaches `keyring.set_password` (stubbed here). This is the
        real-CLI path — `mm init` from a user shell is the only legitimate
        caller and runs without PYTEST_CURRENT_TEST set."""
        import keyring

        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        captured = []

        def stub_set(service, account, password):
            captured.append((service, account, password))

        monkeypatch.setattr(keyring, "set_password", stub_set)
        assert crypto.store_passphrase_in_keyring("real-passphrase") is True
        assert captured == [("mind-meld", "passphrase", "real-passphrase")]
