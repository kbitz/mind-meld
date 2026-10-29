"""Tests for mind_meld.crypto (v2 format).

Covers: encryption round-trip, version handling, Argon2 wiring, HKDF
determinism, master-key cache, session state / drift, mm-crypto-init
bootstrap + fetch + verify, deterministic conflict resolution, and the
critical v1-blob regression (must fail loud post-Track-1C).
"""

from __future__ import annotations

import os
import sys
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
from mind_meld.errors import CryptoError, NewerFormatError, StorageError
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "test-passphrase-123"
MEMORY_KB = 1024  # matches conftest autouse fixture


class TestNewerFormats66A:
    @pytest.mark.parametrize("version", [0x03, 0x0F])
    @pytest.mark.parametrize("length", [1, 5, 100])
    def test_version_precedes_layout(self, version, length):
        raw = bytes([version]) + bytes(length - 1)
        fetched = crypto._parse_crypto_init(raw)
        assert fetched.status == "corrupt"
        assert fetched.newer_version == version
        with pytest.raises(NewerFormatError, match=f"unsupported format version 0x{version:02x}"):
            crypto._decrypt_with_master_key(raw, bytes(32))

    @pytest.mark.parametrize("raw", [b"", b"\x02", b"\x02" * 10, b"\x10", b"\xff", b"garbage"])
    def test_garbage_is_not_newer(self, raw):
        fetched = crypto._parse_crypto_init(raw)
        assert fetched.status == "corrupt"
        assert fetched.newer_version is None
        with pytest.raises(CryptoError) as caught:
            crypto._decrypt_with_master_key(raw, bytes(32))
        assert not isinstance(caught.value, NewerFormatError)

    def test_newer_keycheck_precedes_current_layout_bounds(self):
        raw = b"\x02" + bytes(4 + 16) + b"\x03"
        assert crypto._parse_crypto_init(raw).newer_version == 3

    @pytest.mark.parametrize("canonical_newer", [True, False])
    def test_any_newer_copy_refuses_without_winner_or_mutation(self, tmp_path, canonical_newer):
        """CRITICAL: an older valid copy must never revert a newer canonical."""
        tmp_path = tmp_path / "storage"
        backend = LocalBackend(tmp_path)
        bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
        canonical = tmp_path / CRYPTO_INIT_KEY
        older = canonical.read_bytes()
        canonical.write_bytes(b"\x03" if canonical_newer else older)
        (tmp_path / "mm-crypto-init 2").write_bytes(older if canonical_newer else b"\x03")
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        fetched = fetch_crypto_init(backend)
        assert fetched.status == "corrupt"
        assert fetched.newer_version == 3
        assert fetched.canonical_newer is canonical_newer
        assert fetched.winner_bytes is fetched.root_salt is fetched.repair_plan is None
        with pytest.raises(NewerFormatError):
            crypto.apply_crypto_init_repair(backend, fetched)
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
        assert not list(tmp_path.glob("*.preserved-*"))

    def test_highest_version_across_copies(self, tmp_path):
        tmp_path = tmp_path / "storage"
        backend = LocalBackend(tmp_path)
        backend.put(CRYPTO_INIT_KEY, b"\x03")
        (tmp_path / "mm-crypto-init 2").write_bytes(b"\x0f")
        assert fetch_crypto_init(backend).newer_version == 15

    @pytest.mark.parametrize("pending_repair", [False, True])
    def test_newer_copy_arriving_before_repair_blocks_all_mutation(
        self, tmp_path, monkeypatch, pending_repair
    ):
        tmp_path = tmp_path / "storage"
        backend = LocalBackend(tmp_path)
        bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
        if pending_repair:
            (tmp_path / "mm-crypto-init 2").write_bytes(b"corrupt")
        fetched = fetch_crypto_init(backend)
        (tmp_path / "mm-crypto-init 3").write_bytes(b"\x03")
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        monkeypatch.setattr(backend, "put", lambda *a: pytest.fail("wrote after newer evidence"))
        monkeypatch.setattr(Path, "unlink", lambda *a: pytest.fail("unlinked after newer evidence"))
        with pytest.raises(NewerFormatError, match="changed while the command ran"):
            crypto.apply_crypto_init_repair(backend, fetched)
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before

    def test_newer_canonical_arriving_during_preserve_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        tmp_path = tmp_path / "storage"
        backend = LocalBackend(tmp_path)

        def init_bytes(salt: bytes) -> bytes:
            master = crypto.load_master_key(PASSPHRASE, salt, MEMORY_KB)
            return crypto._serialize_crypto_init(
                MEMORY_KB,
                salt,
                crypto._encrypt_with_master_key(crypto._KEYCHECK_PLAINTEXT, master),
            )

        canonical = tmp_path / CRYPTO_INIT_KEY
        backend.put(CRYPTO_INIT_KEY, init_bytes(b"\xff" * 16))
        (tmp_path / "mm-crypto-init 2").write_bytes(init_bytes(b"\x00" * 16))
        fetched = fetch_crypto_init(backend)
        assert fetched.repair_plan is not None and fetched.repair_plan.replace_canonical
        original_put = backend.put

        def put(key, data):
            if str(key).startswith(f"{CRYPTO_INIT_KEY}.preserved-"):
                canonical.write_bytes(b"\x03")
            return original_put(key, data)

        monkeypatch.setattr(backend, "put", put)
        with pytest.raises(NewerFormatError, match="changed while the command ran"):
            crypto.apply_crypto_init_repair(backend, fetched)
        assert canonical.read_bytes() == b"\x03"

    def test_current_canonical_arriving_during_preserve_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        tmp_path = tmp_path / "storage"
        backend = LocalBackend(tmp_path)

        def init_bytes(salt: bytes) -> bytes:
            master = crypto.load_master_key(PASSPHRASE, salt, MEMORY_KB)
            return crypto._serialize_crypto_init(
                MEMORY_KB,
                salt,
                crypto._encrypt_with_master_key(crypto._KEYCHECK_PLAINTEXT, master),
            )

        canonical = tmp_path / CRYPTO_INIT_KEY
        backend.put(CRYPTO_INIT_KEY, init_bytes(b"\xff" * 16))
        conflict = tmp_path / "mm-crypto-init 2"
        conflict.write_bytes(init_bytes(b"\x00" * 16))
        fetched = fetch_crypto_init(backend)
        replacement = init_bytes(b"\xaa" * 16)
        original_put = backend.put

        def put(key, data):
            if str(key).startswith(f"{CRYPTO_INIT_KEY}.preserved-"):
                canonical.write_bytes(replacement)
            return original_put(key, data)

        monkeypatch.setattr(backend, "put", put)
        with pytest.raises(CryptoError, match="changed while the command ran"):
            crypto.apply_crypto_init_repair(backend, fetched)
        assert canonical.read_bytes() == replacement
        assert conflict.exists()


# ── derive_key (Argon2 primitive) ─────────────────────────────────────


class TestDeriveKey:
    def test_explicit_parameters_and_unchanged_defaults(self, monkeypatch):
        """Explicit KDF kwargs participate; the defaults stay byte-identical."""
        salt = bytes(range(16))
        default = derive_key(PASSPHRASE, salt, memory_kb=MEMORY_KB)
        assert default == derive_key(
            PASSPHRASE, salt, memory_kb=MEMORY_KB, time_cost=3, parallelism=1
        )
        assert default != derive_key(
            PASSPHRASE, salt, memory_kb=MEMORY_KB, time_cost=1, parallelism=1
        )
        assert default != derive_key(
            PASSPHRASE, salt, memory_kb=MEMORY_KB, time_cost=3, parallelism=2
        )
        calls = []

        def primitive(**kwargs):
            calls.append(kwargs)
            return bytes(32)

        monkeypatch.setattr(crypto, "hash_secret_raw", primitive)
        derive_key(PASSPHRASE, salt)
        derive_key(PASSPHRASE, salt, 262144, time_cost=10, parallelism=16)
        assert [(c["memory_cost"], c["time_cost"], c["parallelism"]) for c in calls] == [
            (65536, 3, 1),
            (262144, 10, 16),
        ]

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

        assert (tmp_path / CRYPTO_INIT_KEY).read_bytes()[5:21] == salt_higher
        master = load_master_key(PASSPHRASE, fetch.root_salt, fetch.argon2_memory_kb)
        verify_passphrase(master, fetch.keycheck_blob)
        crypto.apply_crypto_init_repair(backend, fetch)

        # Canonical now holds the winner.
        assert (tmp_path / CRYPTO_INIT_KEY).read_bytes()[5:21] == salt_lower
        # Conflict copy was cleaned up.
        assert not (tmp_path / f"{CRYPTO_INIT_KEY} 2").exists()
        assert next(tmp_path.glob("mm-crypto-init.preserved-*")).read_bytes()[5:21] == salt_higher

    def test_syncthing_conflict_copy_is_a_candidate_and_can_win(self, tmp_path):
        """Track 87A: a Syncthing-named mm-crypto-init copy takes part in the winner
        selection exactly like an iCloud one. (Repairing the fleet is Track 89A's.)"""
        crypto.clear_crypto_session()
        backend = LocalBackend(tmp_path)
        salt_higher = bytes([0xFF] * 16)
        salt_lower = bytes([0x00] * 16)
        self._write_raw_init_blob(tmp_path / CRYPTO_INIT_KEY, 1024, salt_higher)
        self._write_raw_init_blob(
            tmp_path / f"{CRYPTO_INIT_KEY}.sync-conflict-20260101-101010-ABCDEFG", 1024, salt_lower
        )

        fetch = fetch_crypto_init(backend)

        assert fetch.status == "ok"
        assert fetch.root_salt == salt_lower
        assert (tmp_path / CRYPTO_INIT_KEY).read_bytes()[
            5:21
        ] == salt_higher  # a read repairs nothing

    def test_concurrent_canonical_replacement_retains_conflict_copies(self, tmp_path):
        """Codex adversarial finding (PR #177): if a concurrent writer
        replaces canonical again AFTER this process's own overwrite, the
        conflict copy holding the verified winner must survive -- deleting
        it would destroy the only remaining copy of that winner anywhere."""
        crypto.clear_crypto_session()
        backend = LocalBackend(tmp_path)
        salt_higher = bytes([0xFF] * 16)  # canonical, loses
        salt_lower = bytes([0x00] * 16)  # conflict copy, wins
        salt_third = bytes([0x77] * 16)  # a concurrent racer's write
        self._write_raw_init_blob(tmp_path / CRYPTO_INIT_KEY, 1024, salt_higher)
        self._write_raw_init_blob(tmp_path / f"{CRYPTO_INIT_KEY} 2", 1024, salt_lower)

        fetch = fetch_crypto_init(backend)
        assert fetch.root_salt == salt_lower
        master = load_master_key(PASSPHRASE, fetch.root_salt, fetch.argon2_memory_kb)
        verify_passphrase(master, fetch.keycheck_blob)

        racer_path = tmp_path / "racer.tmp"
        self._write_raw_init_blob(racer_path, 1024, salt_third)
        racer_raw = racer_path.read_bytes()
        racer_path.unlink()

        real_put = backend.put

        def racing_put(key, data):
            real_put(key, data)
            if key == CRYPTO_INIT_KEY:
                # Simulate another device's write landing immediately after
                # ours -- e.g. iCloud delivering a concurrent repair.
                real_put(key, racer_raw)

        backend.put = racing_put
        crypto.apply_crypto_init_repair(backend, fetch)

        # Canonical now holds the racer's value, not our winner.
        assert (tmp_path / CRYPTO_INIT_KEY).read_bytes()[5:21] == salt_third
        # The conflict copy -- the only remaining place the verified winner
        # lived -- must NOT have been deleted.
        assert (tmp_path / f"{CRYPTO_INIT_KEY} 2").exists()
        assert (tmp_path / f"{CRYPTO_INIT_KEY} 2").read_bytes()[5:21] == salt_lower

    def test_counts_include_displaced_canonical_when_replaced(self, tmp_path):
        """crypto_init_repair_counts must count a displaced (losing) canonical
        under 'preserve', not silently drop it. Every existing convergence
        test seeds canonical as the winner, so this branch was untested."""
        crypto.clear_crypto_session()
        backend = LocalBackend(tmp_path)
        salt_higher = bytes([0xFF] * 16)
        salt_lower = bytes([0x00] * 16)
        # Canonical loses (higher salt); the conflict copy wins.
        self._write_raw_init_blob(tmp_path / CRYPTO_INIT_KEY, 1024, salt_higher)
        self._write_raw_init_blob(tmp_path / f"{CRYPTO_INIT_KEY} 2", 1024, salt_lower)

        fetch = fetch_crypto_init(backend)
        counts = crypto.crypto_init_repair_counts(fetch)
        assert counts["replace_canonical"] is True
        assert counts["delete"] == 1  # the winning conflict copy itself
        assert counts["preserve"] == 1  # the displaced canonical

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
        """Non-keyring exceptions (OSError etc.) must escape — no silent swallow.

        Track 73A moved the non-interactive seam from `keyring.get_password`
        to the walked backend member, so the raise is injected there.
        """
        import keyring

        backend = _FakeBackend(error=OSError("locked keychain gave us something unexpected"))
        monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        with pytest.raises(OSError, match="locked keychain"):
            get_passphrase(non_interactive=True)

    def test_runtime_error_propagates(self, monkeypatch):
        """A broken backend that raises a plain RuntimeError must not be swallowed."""
        import keyring

        backend = _FakeBackend(error=RuntimeError("dbus session bus went away"))
        monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
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


class TestCryptoInitRepairPreview56A:
    @pytest.mark.parametrize("canonical", ["winner", "loser", "missing", "corrupt"])
    def test_preview_same_winner_no_storage_mutation(self, tmp_path, monkeypatch, canonical):
        backend = LocalBackend(tmp_path)
        writer = TestConflictConvergence()._write_raw_init_blob
        copy = tmp_path / "mm-crypto-init 2"
        writer(copy, MEMORY_KB, b"\x00" * 16)
        if canonical in {"winner", "loser"}:
            if canonical == "winner":
                (tmp_path / CRYPTO_INIT_KEY).write_bytes(copy.read_bytes())
            else:
                writer(tmp_path / CRYPTO_INIT_KEY, MEMORY_KB, b"\xff" * 16)
        elif canonical == "corrupt":
            (tmp_path / CRYPTO_INIT_KEY).write_bytes(b"corrupt")
        unreadable = tmp_path / "mm-crypto-init 3"
        unreadable.write_bytes(b"unreadable candidate")
        real_read = Path.read_bytes
        real_nofollow = crypto._read_regular_nofollow

        def read_nofollow(path):
            if path == unreadable:
                return None
            return real_nofollow(path)

        before = {
            p.name: (real_read(p), p.stat().st_mtime_ns) for p in tmp_path.iterdir() if p.is_file()
        }
        with monkeypatch.context() as patch:
            patch.setattr(crypto, "_read_regular_nofollow", read_nofollow)
            patch.setattr(backend, "put", lambda *a: pytest.fail("preview put"))
            patch.setattr(
                backend, "delete_conflict_copies", lambda *a: pytest.fail("preview delete")
            )
            preview = fetch_crypto_init(backend)
        assert preview.status == "ok"
        assert preview.root_salt == b"\x00" * 16
        assert preview.repair_plan.replace_canonical == (canonical != "winner")
        assert {c.name: c.disposition for c in preview.repair_plan.copies} == {
            copy.name: "delete",
            unreadable.name: "preserve",
        }
        unreadable_plan = next(c for c in preview.repair_plan.copies if c.name == unreadable.name)
        assert unreadable_plan.content_hash is None
        assert {
            p.name: (real_read(p), p.stat().st_mtime_ns) for p in tmp_path.iterdir() if p.is_file()
        } == before

        crypto.apply_crypto_init_repair(backend, preview)
        assert not copy.exists()
        # Unreadable at planning time means no digest authorizes its removal.
        assert unreadable.exists()

    @pytest.mark.parametrize("state", ["missing", "corrupt"])
    def test_unusable_storage_never_repairs(self, tmp_path, monkeypatch, state):
        backend = LocalBackend(tmp_path)
        if state == "corrupt":
            (tmp_path / CRYPTO_INIT_KEY).write_bytes(b"bad")
        monkeypatch.setattr(backend, "put", lambda *a: pytest.fail("preview put"))
        monkeypatch.setattr(
            backend, "delete_conflict_copies", lambda *a: pytest.fail("preview delete")
        )
        result = fetch_crypto_init(backend)
        assert result.status == state
        assert result.repair_plan is None


class TestVerifiedRepair60A:
    def _seed(self, tmp_path):
        backend = LocalBackend(tmp_path)
        writer = TestConflictConvergence()._write_raw_init_blob
        canonical = tmp_path / CRYPTO_INIT_KEY
        writer(canonical, MEMORY_KB, b"\x00" * 16)
        same = tmp_path / "mm-crypto-init 2"
        same.write_bytes(canonical.read_bytes())
        distinct = tmp_path / "mm-crypto-init 3"
        writer(distinct, MEMORY_KB, b"\xff" * 16)
        return backend, canonical, same, distinct

    def test_repair_preserves_distinct_lineage_and_durable_canonical(self, tmp_path, monkeypatch):
        from mind_meld import fsutil

        backend, canonical, same, distinct = self._seed(tmp_path)
        winner = canonical.read_bytes()
        other = distinct.read_bytes()
        synced = []
        real_fsync = fsutil._fsync_fd

        def fsync(fd):
            synced.append(True)
            assert same.exists() and distinct.exists()
            real_fsync(fd)

        monkeypatch.setattr(fsutil, "_fsync_fd", fsync)
        fetch = fetch_crypto_init(backend)
        assert fetch.repair_plan.winner_bytes == winner
        crypto.apply_crypto_init_repair(backend, fetch)
        assert len(synced) >= 4  # file and directory for canonical and preserved copy
        assert canonical.read_bytes() == winner
        assert not same.exists() and not distinct.exists()
        preserved = list(tmp_path.glob("mm-crypto-init.preserved-*"))
        assert [p.read_bytes() for p in preserved] == [other]
        assert len(preserved[0].name.split(".preserved-", 1)[1].split("-", 1)[0]) == 8
        assert backend.find_conflict_copies(CRYPTO_INIT_KEY) == []

    @pytest.mark.parametrize("change", ["replacement", "new"])
    def test_late_copies_are_left_alone(self, tmp_path, change):
        backend, _, same, distinct = self._seed(tmp_path)
        fetch = fetch_crypto_init(backend)
        path = same if change == "replacement" else tmp_path / "mm-crypto-init 4"
        replacement = b"new bytes arriving after fetch"
        path.write_bytes(replacement)
        crypto.apply_crypto_init_repair(backend, fetch)
        assert path.read_bytes() == replacement
        assert not distinct.exists()

    @pytest.mark.parametrize("field", ["memory", "keycheck"])
    def test_same_salt_different_bytes_are_preserved(self, tmp_path, field):
        backend, canonical, same, distinct = self._seed(tmp_path)
        raw = canonical.read_bytes()
        if field == "memory":
            raw = raw[:1] + (2048).to_bytes(4, "big") + raw[5:]
        else:
            raw = raw[:-1] + bytes([raw[-1] ^ 1])
        distinct.write_bytes(raw)
        crypto.apply_crypto_init_repair(backend, fetch_crypto_init(backend))
        assert not same.exists() and not distinct.exists()
        assert [p.read_bytes() for p in tmp_path.glob("mm-crypto-init.preserved-*")] == [raw]

    @pytest.mark.parametrize("failure_at", [1, 2, 3, 4])
    def test_fsync_failure_leaves_every_candidate(self, tmp_path, monkeypatch, failure_at):
        from mind_meld import fsutil

        backend, canonical, same, distinct = self._seed(tmp_path)
        candidates = {p: p.read_bytes() for p in (canonical, same, distinct)}
        count = 0

        def fsync(fd):
            nonlocal count
            count += 1
            if count == failure_at:
                raise OSError("fsync denied")

        monkeypatch.setattr(fsutil, "_fsync_fd", fsync)
        with pytest.raises(StorageError, match="fsync"):
            crypto.apply_crypto_init_repair(backend, fetch_crypto_init(backend))
        assert {p: p.read_bytes() for p in candidates} == candidates

    def test_winner_bytes_change_aborts_even_without_a_pending_repair(self, tmp_path, monkeypatch):
        backend, canonical, same, distinct = self._seed(tmp_path)
        same.unlink()
        distinct.unlink()
        fetch = fetch_crypto_init(backend)
        raw = canonical.read_bytes()
        canonical.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        monkeypatch.setattr(backend, "put", lambda *a: pytest.fail("stale winner wrote storage"))
        with pytest.raises(
            CryptoError, match="changed while the command ran; nothing was published"
        ):
            crypto.apply_crypto_init_repair(backend, fetch)

    def test_plan_names_are_not_joined_to_storage(self, tmp_path):
        import hashlib
        from dataclasses import replace

        backend, _, _, _ = self._seed(tmp_path)
        fetch = fetch_crypto_init(backend)
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        outside.write_bytes(b"untouched")
        fake = crypto.CryptoInitCandidate(
            "../" + outside.name, hashlib.sha256(b"untouched").hexdigest(), "delete"
        )
        fetch = replace(fetch, repair_plan=replace(fetch.repair_plan, copies=(fake,)))
        crypto.apply_crypto_init_repair(backend, fetch)
        assert outside.read_bytes() == b"untouched"

    def test_symlink_conflict_is_unreadable_and_not_copied(self, tmp_path):
        backend, canonical, same, distinct = self._seed(tmp_path)
        secret = tmp_path.parent / (tmp_path.name + "-secret")
        secret.write_bytes(b"credential-bytes")
        same.unlink()
        same.symlink_to(secret)
        fetch = fetch_crypto_init(backend)
        copy = next(c for c in fetch.repair_plan.copies if c.name == same.name)
        assert copy.content_hash is None
        assert copy.disposition == "preserve"
        crypto.apply_crypto_init_repair(backend, fetch)
        assert same.is_symlink()
        assert secret.read_bytes() == b"credential-bytes"
        assert not any(
            p.read_bytes() == b"credential-bytes"
            for p in tmp_path.glob("mm-crypto-init.preserved-*")
        )

    def test_symlink_canonical_aborts_repair_without_copying_target(self, tmp_path):
        backend, canonical, same, distinct = self._seed(tmp_path)
        secret = tmp_path.parent / (tmp_path.name + "-secret")
        secret.write_bytes(b"credential-bytes")
        canonical.unlink()
        canonical.symlink_to(secret)
        fetch = fetch_crypto_init(backend)
        assert fetch.status == "ok"
        with pytest.raises(CryptoError, match="cannot preserve unreadable canonical"):
            crypto.apply_crypto_init_repair(backend, fetch)
        assert canonical.is_symlink()
        assert secret.read_bytes() == b"credential-bytes"
        assert not any(
            p.read_bytes() == b"credential-bytes"
            for p in tmp_path.glob("mm-crypto-init.preserved-*")
        )

    def test_unreadable_canonical_aborts_repair_without_writes(self, tmp_path, monkeypatch):
        backend, canonical, same, distinct = self._seed(tmp_path)
        before = {p: p.read_bytes() for p in (canonical, same, distinct)}
        fetch = fetch_crypto_init(backend)
        real = crypto._read_regular_nofollow

        def boom(path):
            if path == canonical:
                return None
            return real(path)

        monkeypatch.setattr(crypto, "_read_regular_nofollow", boom)
        with pytest.raises(CryptoError, match="cannot preserve unreadable canonical"):
            crypto.apply_crypto_init_repair(backend, fetch)
        assert {p: p.read_bytes() for p in before} == before


def test_crypto_repair_has_one_mutating_owner():
    import ast

    root = Path(__file__).parents[1] / "src" / "mind_meld"
    owners = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", "")
            if name not in {"put", "put_exclusive", "delete", "delete_conflict_copies"}:
                continue
            if not any(
                isinstance(arg, ast.Name) and arg.id == "CRYPTO_INIT_KEY" for arg in node.args
            ):
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(owner, ast.FunctionDef):
                owner = parents.get(owner)
            owners.add((path.name, owner.name))
    assert owners == {
        ("crypto.py", "apply_crypto_init_repair"),
        ("crypto.py", "bootstrap_crypto_init"),
    }


# ── non-interactive keyring read (Track 73A) ─────────────────────────
#
# The hook path must never open a desktop prompt. These tests drive
# crypto's reader with a fake `secretstorage` module in sys.modules and
# injected keyring backends; prompt surfaces (unlock, create_collection,
# get_default_collection, exec_prompt, keyring.get_password) are spies
# that fail the test if reached. No test here touches a real keyring:
# conftest pins get_keyring to the fail backend and every test that
# needs a backend injects its own.


from tests.conftest import (  # noqa: E402
    _chainer,
    _FakeBackend,
    _FakeDBusError,
    _ss_member,
    _use_backend,
)


class TestKeyringUnavailableRender:
    """Each (reason, kind) cell rendered by constructing the exception."""

    URL = "https://github.com/kbitz/mind-meld#auto-commands-skip-keyring-locked-or-unavailable"

    @pytest.mark.parametrize("verb", ["pull", "push"])
    def test_locked_secret_service(self, verb):
        from mind_meld.errors import KeyringUnavailableError

        exc = KeyringUnavailableError("locked", "secret-service")
        assert exc.render(verb) == (
            "keyring locked (Secret Service collection is locked); "
            "unlock your login keyring in the desktop session, "
            f"then rerun mm auto{verb}. See {self.URL}"
        )

    @pytest.mark.parametrize("verb", ["pull", "push"])
    def test_no_default_collection_secret_service(self, verb):
        from mind_meld.errors import KeyringUnavailableError

        exc = KeyringUnavailableError("no-default-collection", "secret-service")
        assert exc.render(verb) == (
            "keyring has no default collection; "
            "create one and store the passphrase with secret-tool, "
            f"then rerun mm auto{verb}. See {self.URL}"
        )

    @pytest.mark.parametrize("verb", ["pull", "push"])
    def test_denied_keychain(self, verb):
        from mind_meld.errors import KeyringUnavailableError

        exc = KeyringUnavailableError("denied", "keychain")
        assert exc.render(verb) == (
            "keyring locked (Keychain access was denied); "
            f"run mm {verb} in a terminal in your desktop session "
            f"and choose Always Allow, then rerun mm auto{verb}. See {self.URL}"
        )

    @pytest.mark.parametrize("verb", ["pull", "push"])
    def test_denied_other(self, verb):
        from mind_meld.errors import KeyringUnavailableError

        exc = KeyringUnavailableError("denied", "other")
        assert exc.render(verb) == (
            "keyring locked (desktop keyring refused access); "
            "unlock or allow access in your desktop keyring, "
            f"then rerun mm auto{verb}. See {self.URL}"
        )

    def test_str_has_no_rerun_hint_and_is_a_crypto_error(self):
        from mind_meld.errors import KeyringUnavailableError

        exc = KeyringUnavailableError("locked", "secret-service")
        assert isinstance(exc, CryptoError)
        assert exc.reason == "locked"
        assert exc.backend_kind == "secret-service"
        assert str(exc) == exc.render()
        assert "rerun" not in str(exc)
        assert str(exc).endswith(self.URL)

    def test_crypto_reexports_the_error(self):
        from mind_meld.errors import KeyringUnavailableError

        assert crypto.KeyringUnavailableError is KeyringUnavailableError
        assert "KeyringUnavailableError" in crypto.__all__


class TestKeyringLibraryContract:
    """Pin the keyring/secretstorage surface the reader relies on.

    No version pins: if a keyring upgrade renames these, the reader's
    assumptions break here first, loudly, instead of in a hook.
    """

    def test_secret_service_scheme_surface(self):
        member = _ss_member()
        assert member.schemes[member.scheme] == {
            "username": "username",
            "service": "service",
        }
        assert member.schemes["KeePassXC"] == {
            "username": "UserName",
            "service": "Title",
        }

    def test_chainer_and_locked_error_exist(self):
        from keyring.backends.chainer import ChainerBackend
        from keyring.errors import KeyringError, KeyringLocked

        assert hasattr(ChainerBackend, "backends")
        assert issubclass(KeyringLocked, KeyringError)

    def test_secretstorage_surface_where_importable(self):
        ss = pytest.importorskip("secretstorage")
        assert hasattr(ss, "dbus_init")
        assert hasattr(ss, "Collection")
        assert hasattr(ss.Collection, "search_items")
        assert hasattr(ss.Collection, "is_locked")
        assert hasattr(ss.Item, "get_secret")
        assert hasattr(ss, "ItemNotFoundException")
        assert hasattr(ss, "LockedException")

    def test_secretstorage_paths_the_reader_uses_where_importable(self):
        """Module paths crypto imports, the >=3.4 UnknownObject mapping, and
        jeepney's `.name` on DBusErrorResponse (the IsLocked check reads it)."""
        pytest.importorskip("secretstorage")
        import secretstorage.collection
        import secretstorage.defines
        import secretstorage.exceptions
        from jeepney import DBusAddress, DBusErrorResponse, new_error, new_method_call

        assert secretstorage.collection.DEFAULT_COLLECTION == (
            "/org/freedesktop/secrets/aliases/default"
        )
        for name in ("SecretStorageException", "ItemNotFoundException", "LockedException"):
            assert hasattr(secretstorage.exceptions, name)
        # secretstorage >= 3.4 maps a missing collection's UnknownObject to
        # ItemNotFoundException (pyproject floor); older ones raise raw D-Bus.
        unknown_object = "org.freedesktop.DBus.Error.UnknownObject"
        assert secretstorage.defines.DBUS_UNKNOWN_OBJECT == unknown_object
        call = new_method_call(
            DBusAddress("/item", "org.freedesktop.secrets", "org.freedesktop.Secret.Item"),
            "GetSecret",
            "o",
            ("/session",),
        )
        err = DBusErrorResponse(new_error(call, "org.freedesktop.Secret.Error.IsLocked"))
        assert err.name == "org.freedesktop.Secret.Error.IsLocked"

    def test_unknown_object_reaches_the_reader_as_no_collection_where_importable(self):
        """Behavior behind the >=3.4 floor, through the reader's own entry
        point: a missing collection's D-Bus UnknownObject reply becomes
        no-default-collection (3.3.x raised a raw DBusErrorResponse, so the
        hook reported keyring-error)."""
        pytest.importorskip("secretstorage")
        from jeepney import new_error

        from mind_meld.errors import KeyringUnavailableError

        class _Conn:
            def send_and_get_reply(self, msg):
                return new_error(msg, "org.freedesktop.DBus.Error.UnknownObject")

        with pytest.raises(KeyringUnavailableError) as ei:
            crypto._open_secret_service_collection(_Conn(), _ss_member())
        assert ei.value.reason == "no-default-collection"


class TestNonInteractiveSecretServiceRead:
    """Single Secret Service backend through get_passphrase(non_interactive)."""

    def test_unlocked_item_is_returned_without_prompting(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"stored-pw")]
        _use_backend(monkeypatch, _ss_member())
        assert get_passphrase(non_interactive=True) == "stored-pw"
        assert ss_harness.prompt_calls == []
        assert ss_harness.searches == [{"username": "passphrase", "service": "mind-meld"}]
        assert ss_harness.collection_paths == ["/org/freedesktop/secrets/aliases/default"]

    def test_keepassxc_scheme_query(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"kpxc-pw")]
        _use_backend(monkeypatch, _ss_member(scheme="KeePassXC"))
        assert get_passphrase(non_interactive=True) == "kpxc-pw"
        assert ss_harness.searches == [{"UserName": "passphrase", "Title": "mind-meld"}]

    def test_preferred_collection_path_is_used(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"login-pw")]
        _use_backend(
            monkeypatch,
            _ss_member(preferred_collection="/org/freedesktop/secrets/collection/login"),
        )
        assert get_passphrase(non_interactive=True) == "login-pw"
        assert ss_harness.collection_paths == ["/org/freedesktop/secrets/collection/login"]

    def test_first_of_multiple_items_wins_and_empty_string_is_kept(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"first"), ss_harness.make_item(b"second")]
        _use_backend(monkeypatch, _ss_member())
        assert get_passphrase(non_interactive=True) == "first"
        ss_harness.items = [ss_harness.make_item(b"")]
        import keyring

        assert crypto._non_interactive_keyring_read(keyring) == ""

    def test_locked_collection_raises_unavailable(self, ss_harness, monkeypatch):
        from mind_meld.errors import KeyringUnavailableError

        ss_harness.locked = True
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert (ei.value.reason, ei.value.backend_kind) == ("locked", "secret-service")
        assert ss_harness.prompt_calls == []
        assert ss_harness.searches == []

    def test_locked_collection_uses_env_when_set(self, ss_harness, monkeypatch):
        ss_harness.locked = True
        _use_backend(monkeypatch, _ss_member())
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-pw")
        assert get_passphrase(non_interactive=True) == "env-pw"

    def test_no_default_collection_raises_unavailable(self, ss_harness, monkeypatch):
        from mind_meld.errors import KeyringUnavailableError

        ss_harness.collection_error = ss_harness.ItemNotFoundException("no alias")
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert (ei.value.reason, ei.value.backend_kind) == (
            "no-default-collection",
            "secret-service",
        )
        assert ss_harness.prompt_calls == []

    def test_no_default_collection_uses_env_when_set(self, ss_harness, monkeypatch):
        ss_harness.collection_error = ss_harness.ItemNotFoundException("no alias")
        _use_backend(monkeypatch, _ss_member())
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-pw")
        assert get_passphrase(non_interactive=True) == "env-pw"

    def test_locked_item_race_maps_to_locked(self, ss_harness, monkeypatch):
        """Unlocked at check, LockedException at get_secret -> locked."""
        from mind_meld.errors import KeyringUnavailableError

        ss_harness.items = [ss_harness.make_item(error=ss_harness.LockedException("locked"))]
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert ei.value.reason == "locked"

    def test_islocked_dbus_error_name_maps_to_locked(self, ss_harness, monkeypatch):
        from mind_meld.errors import KeyringUnavailableError

        ss_harness.items = [
            ss_harness.make_item(error=_FakeDBusError("org.freedesktop.Secret.Error.IsLocked"))
        ]
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert ei.value.reason == "locked"

    def test_other_dbus_error_propagates(self, ss_harness, monkeypatch):
        ss_harness.items = [
            ss_harness.make_item(error=_FakeDBusError("org.freedesktop.DBus.Error.NoReply"))
        ]
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(_FakeDBusError, match="NoReply"):
            get_passphrase(non_interactive=True)

    def test_decode_error_and_os_error_propagate(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"\xff\xfe not utf-8")]
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(UnicodeDecodeError):
            get_passphrase(non_interactive=True)
        ss_harness.items = [ss_harness.make_item(error=OSError("socket died"))]
        with pytest.raises(OSError, match="socket died"):
            get_passphrase(non_interactive=True)

    def test_dbus_init_failure_propagates(self, ss_harness, monkeypatch):
        """Parity: keyring calls dbus_init outside its InitError wrapper."""
        ss_harness.dbus_init_error = ss_harness.NotAvailable("no session bus")
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(ss_harness.NotAvailable, match="no session bus"):
            get_passphrase(non_interactive=True)

    def test_is_locked_failure_propagates(self, ss_harness, monkeypatch):
        ss_harness.is_locked_error = RuntimeError("property read failed")
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(RuntimeError, match="property read failed"):
            get_passphrase(non_interactive=True)

    def test_is_locked_storage_error_propagates_even_with_env(self, ss_harness, monkeypatch):
        """A SecretStorageException from is_locked() is a keyring error, not a skip."""
        ss_harness.is_locked_error = ss_harness.NotAvailable("NoReply")
        _use_backend(monkeypatch, _ss_member())
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-pw")
        with pytest.raises(ss_harness.NotAvailable):
            get_passphrase(non_interactive=True)

    def test_get_secret_storage_error_propagates_even_with_env(self, ss_harness, monkeypatch):
        """A SecretStorageException from the item read propagates (spec 3.1 step 6)."""
        ss_harness.items = [ss_harness.make_item(error=ss_harness.NotAvailable("NoReply"))]
        _use_backend(monkeypatch, _ss_member())
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-pw")
        with pytest.raises(ss_harness.NotAvailable):
            get_passphrase(non_interactive=True)

    def test_collection_storage_error_falls_through_like_today(self, ss_harness, monkeypatch):
        """Non-ItemNotFound SecretStorageException -> env, else no-passphrase."""
        ss_harness.collection_error = ss_harness.NotAvailable("daemon gone")
        _use_backend(monkeypatch, _ss_member())
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-pw")
        assert get_passphrase(non_interactive=True) == "env-pw"
        monkeypatch.delenv("MINDMELD_PASSPHRASE")
        with pytest.raises(CryptoError, match="no passphrase available"):
            get_passphrase(non_interactive=True)

    def test_no_items_falls_through_and_names_backend(self, ss_harness, monkeypatch):
        _use_backend(monkeypatch, _ss_member())
        with pytest.raises(CryptoError) as ei:
            get_passphrase(non_interactive=True)
        assert str(ei.value) == (
            "no passphrase available: the keyring returned none or could "
            "not be read (backend keyring.backends.SecretService.Keyring) and "
            "MINDMELD_PASSPHRASE is "
            "unset; run mm diag"
        )

    def test_env_used_when_no_items(self, ss_harness, monkeypatch):
        _use_backend(monkeypatch, _ss_member())
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-pw")
        assert get_passphrase(non_interactive=True) == "env-pw"


class TestNonInteractiveOtherBackends:
    """Non-Secret-Service members: values, KeyringLocked mapping, parity."""

    def test_single_backend_value(self, monkeypatch):
        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        backend = _FakeBackend(value="backend-pw")
        _use_backend(monkeypatch, backend)
        assert get_passphrase(non_interactive=True) == "backend-pw"
        assert backend.seen == [("mind-meld", "passphrase")]

    def test_keyringlocked_from_keychain_backend_is_denied_keychain(self, monkeypatch):
        from keyring.errors import KeyringLocked

        from mind_meld.errors import KeyringUnavailableError

        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        macos_cls = type("Keyring", (), {"__module__": "keyring.backends.macOS"})
        backend = macos_cls()
        backend.get_password = _FakeBackend(error=KeyringLocked("denied")).get_password
        _use_backend(monkeypatch, backend)
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert (ei.value.reason, ei.value.backend_kind) == ("denied", "keychain")

    @pytest.mark.skipif(sys.platform != "darwin", reason="real macOS keyring backend class")
    def test_real_macos_backend_class_maps_to_keychain(self, monkeypatch):
        """Pins the module-prefix check to keyring's actual macOS class."""
        from keyring.backends.macOS import Keyring as MacKeyring
        from keyring.errors import KeyringLocked

        from mind_meld.errors import KeyringUnavailableError

        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        backend = MacKeyring()
        # Instance attribute shadows the method: the real Keychain is never read.
        backend.get_password = _FakeBackend(error=KeyringLocked("denied")).get_password
        _use_backend(monkeypatch, backend)
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert (ei.value.reason, ei.value.backend_kind) == ("denied", "keychain")

    def test_keyringlocked_from_other_backend_is_denied_other(self, monkeypatch):
        from keyring.errors import KeyringLocked

        from mind_meld.errors import KeyringUnavailableError

        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        _use_backend(monkeypatch, _FakeBackend(error=KeyringLocked("kwallet denied")))
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert (ei.value.reason, ei.value.backend_kind) == ("denied", "other")

    def test_denied_uses_env_when_set(self, monkeypatch):
        from keyring.errors import KeyringLocked

        _use_backend(monkeypatch, _FakeBackend(error=KeyringLocked("denied")))
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "env-pw")
        assert get_passphrase(non_interactive=True) == "env-pw"

    def test_get_keyring_keyring_error_is_no_value(self, monkeypatch):
        import keyring
        from keyring.errors import NoKeyringError

        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)

        def no_backend():
            raise NoKeyringError("no backend")

        monkeypatch.setattr(keyring, "get_keyring", no_backend)
        with pytest.raises(CryptoError, match=r"no passphrase available.*\(backend unknown\)"):
            get_passphrase(non_interactive=True)

    # Generated by the /ship test coverage audit (pass 1).
    # Value: protects=backend discovery failures outside the KeyringError family
    # propagate on the hook path, env set or not; fails_when=the get_keyring catch
    # in _non_interactive_keyring_read widens past KeyringError; why_new=only
    # NoKeyringError at discovery and member-level errors are tested; seam=none;
    # no_claim=which real backends raise during discovery
    @pytest.mark.parametrize("env", [None, "env-pw"])
    def test_get_keyring_unexpected_error_propagates(self, monkeypatch, env):
        import keyring

        def boom():
            raise RuntimeError("backend discovery exploded")

        monkeypatch.setattr(keyring, "get_keyring", boom)
        if env is None:
            monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        else:
            monkeypatch.setenv("MINDMELD_PASSPHRASE", env)
        with pytest.raises(RuntimeError, match="backend discovery exploded"):
            get_passphrase(non_interactive=True)

    # Generated by the /ship test coverage audit (pass 1).
    # Value: protects=without the keyring package the hook's no-passphrase error
    # and breadcrumb label say backend unavailable; fails_when=backend_name or
    # _keyring_backend_label loses its ImportError default; why_new=only the
    # interactive path and the diag probe run without keyring; seam=none;
    # no_claim=a real stripped Python install
    def test_missing_keyring_package_names_backend_unavailable(self, monkeypatch):
        import builtins

        from mind_meld.errors import KeyringUnavailableError

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "keyring" or name.startswith("keyring."):
                raise ImportError("keyring missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
        assert crypto._keyring_backend_label() == "unavailable"
        with pytest.raises(CryptoError) as ei:
            get_passphrase(non_interactive=True)
        assert not isinstance(ei.value, KeyringUnavailableError)
        assert str(ei.value) == (
            "no passphrase available: the keyring returned none or could "
            "not be read (backend unavailable) and MINDMELD_PASSPHRASE is "
            "unset; run mm diag"
        )

    def test_interactive_path_unchanged(self, monkeypatch):
        """Attended reads still use keyring.get_password and never the walk."""
        import keyring

        def no_walk():
            raise AssertionError("interactive read consulted get_keyring()")

        monkeypatch.setattr(keyring, "get_keyring", no_walk)
        monkeypatch.setattr(keyring, "get_password", lambda *a, **kw: "interactive-pw")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "ignored")
        assert get_passphrase() == "interactive-pw"


class TestNonInteractiveChainerWalk:
    """ChainerBackend member walk: order, stop rules, single backends read."""

    def test_secret_service_value_wins_other_not_consulted(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"ss-pw")]
        other = _FakeBackend(value="other-pw")
        chainer, calls = _chainer([_ss_member(), other])
        _use_backend(monkeypatch, chainer)
        assert get_passphrase(non_interactive=True) == "ss-pw"
        assert other.calls == 0
        assert calls == [1]  # backends read exactly once

    def test_other_value_wins_secret_service_not_consulted(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"ss-pw")]
        other = _FakeBackend(value="other-pw")
        chainer, _calls = _chainer([other, _ss_member()])
        _use_backend(monkeypatch, chainer)
        assert get_passphrase(non_interactive=True) == "other-pw"
        assert ss_harness.dbus_init_calls == 0

    def test_secret_service_none_moves_to_next_member(self, ss_harness, monkeypatch):
        other = _FakeBackend(value="other-pw")
        chainer, _calls = _chainer([_ss_member(), other])
        _use_backend(monkeypatch, chainer)
        assert get_passphrase(non_interactive=True) == "other-pw"
        assert other.calls == 1

    def test_other_none_moves_to_secret_service(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"ss-pw")]
        other = _FakeBackend(value=None)
        chainer, _calls = _chainer([other, _ss_member()])
        _use_backend(monkeypatch, chainer)
        assert get_passphrase(non_interactive=True) == "ss-pw"

    def test_locked_secret_service_stops_the_walk(self, ss_harness, monkeypatch):
        from mind_meld.errors import KeyringUnavailableError

        ss_harness.locked = True
        other = _FakeBackend(value="other-pw")
        chainer, _calls = _chainer([_ss_member(), other])
        _use_backend(monkeypatch, chainer)
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert ei.value.reason == "locked"
        assert other.calls == 0

    def test_no_collection_stops_the_walk(self, ss_harness, monkeypatch):
        from mind_meld.errors import KeyringUnavailableError

        ss_harness.collection_error = ss_harness.ItemNotFoundException("no alias")
        other = _FakeBackend(value="other-pw")
        chainer, _calls = _chainer([_ss_member(), other])
        _use_backend(monkeypatch, chainer)
        with pytest.raises(KeyringUnavailableError) as ei:
            get_passphrase(non_interactive=True)
        assert ei.value.reason == "no-default-collection"
        assert other.calls == 0

    def test_member_keyring_error_moves_to_next_member(self, ss_harness, monkeypatch):
        """A swallowed-family KeyringError yields no value from that member."""
        from keyring.errors import NoKeyringError

        ss_harness.items = [ss_harness.make_item(b"ss-pw")]
        broken = _FakeBackend(error=NoKeyringError("backend unavailable"))
        chainer, _calls = _chainer([broken, _ss_member()])
        _use_backend(monkeypatch, chainer)
        assert get_passphrase(non_interactive=True) == "ss-pw"

    def test_member_unexpected_exception_propagates(self, ss_harness, monkeypatch):
        ss_harness.items = [ss_harness.make_item(b"ss-pw")]
        broken = _FakeBackend(error=RuntimeError("backend exploded"))
        chainer, _calls = _chainer([broken, _ss_member()])
        _use_backend(monkeypatch, chainer)
        with pytest.raises(RuntimeError, match="backend exploded"):
            get_passphrase(non_interactive=True)

    def test_walk_exhausted_is_no_value(self, ss_harness, monkeypatch):
        other = _FakeBackend(value=None)
        chainer, _calls = _chainer([_ss_member(), other])
        _use_backend(monkeypatch, chainer)
        with pytest.raises(CryptoError, match="no passphrase available"):
            get_passphrase(non_interactive=True)


class TestSecretServiceState:
    """_secret_service_state: diag probe, every state, never a prompt."""

    def test_no_secret_service_member_is_na(self, ss_harness, monkeypatch):
        # conftest default: fail backend, no Secret Service member.
        state = crypto._secret_service_state()
        assert state == {
            "backend": "keyring.backends.fail.Keyring",
            "member": None,
            "secret_service": "n/a",
            "item": "n/a",
            "error": None,
        }
        assert ss_harness.dbus_init_calls == 0

    def test_unlocked_absent_present_multiple(self, ss_harness, monkeypatch):
        _use_backend(monkeypatch, _ss_member())
        state = crypto._secret_service_state()
        assert (state["secret_service"], state["item"]) == ("unlocked", "absent")
        assert state["backend"] == "keyring.backends.SecretService.Keyring"
        assert state["member"] == "keyring.backends.SecretService.Keyring"
        assert state["error"] is None

        ss_harness.items = [ss_harness.make_item()]
        assert crypto._secret_service_state()["item"] == "present"

        ss_harness.items = [ss_harness.make_item(), ss_harness.make_item()]
        assert crypto._secret_service_state()["item"] == "multiple"
        assert ss_harness.prompt_calls == []

    def test_state_walks_chainer_members_once(self, ss_harness, monkeypatch):
        """keyring's usual Linux shape: a chainer holding the SS member."""
        ss_harness.items = [ss_harness.make_item()]
        chainer, calls = _chainer([_FakeBackend(value=None), _ss_member()])
        _use_backend(monkeypatch, chainer)
        state = crypto._secret_service_state()
        assert state["member"] == "keyring.backends.SecretService.Keyring"
        assert (state["secret_service"], state["item"]) == ("unlocked", "present")
        assert calls == [1]
        assert ss_harness.prompt_calls == []

    def test_locked_state(self, ss_harness, monkeypatch):
        ss_harness.locked = True
        _use_backend(monkeypatch, _ss_member())
        state = crypto._secret_service_state()
        assert state["secret_service"] == "locked"
        assert state["item"] == "unknown"
        assert state["error"] is None
        assert ss_harness.searches == []

    def test_no_default_collection_state(self, ss_harness, monkeypatch):
        ss_harness.collection_error = ss_harness.ItemNotFoundException("no alias")
        _use_backend(monkeypatch, _ss_member())
        state = crypto._secret_service_state()
        assert state["secret_service"] == "no-default-collection"
        assert state["item"] == "n/a"
        assert state["error"] is None

    def test_error_state_names_exception_class(self, ss_harness, monkeypatch):
        ss_harness.dbus_init_error = ss_harness.NotAvailable("no bus")
        _use_backend(monkeypatch, _ss_member())
        state = crypto._secret_service_state()
        assert state["secret_service"] == "unknown"
        assert state["item"] == "unknown"
        assert state["error"] == "SecretServiceNotAvailableException"

    def test_backend_lookup_failure_is_unknown_not_a_crash(self, monkeypatch):
        import keyring

        def boom():
            raise RuntimeError("discovery exploded")

        monkeypatch.setattr(keyring, "get_keyring", boom)
        state = crypto._secret_service_state()
        assert state["backend"] == "unknown"
        assert state["secret_service"] == "unknown"
        assert state["item"] == "unknown"
        assert state["error"] == "RuntimeError"

    def test_missing_keyring_package_is_unavailable(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "keyring":
                raise ImportError("keyring missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        state = crypto._secret_service_state()
        assert state["backend"] == "unavailable"
        assert state["secret_service"] == "n/a"

    def test_state_never_reads_the_secret(self, ss_harness, monkeypatch):
        read = []

        class _Item:
            def get_secret(self):
                read.append(True)
                return b"nope"

        ss_harness.items = [_Item()]
        _use_backend(monkeypatch, _ss_member())
        assert crypto._secret_service_state()["item"] == "present"
        assert read == []


class TestFakeSecretStorageHarness:
    """The fake must catch a regression before it can reach a real keyring."""

    def test_prompt_runner_is_a_spy(self, ss_harness):
        util = sys.modules["secretstorage.util"]
        with pytest.raises(AssertionError):
            util.exec_prompt(None, "/prompt")
        assert ss_harness.prompt_calls == ["exec_prompt"]

    def test_undo_restores_the_pre_harness_binding(self, monkeypatch):
        """The harness may be the first importer of keyring's SecretService
        module; undo must not leave that module bound to the dead fake."""
        import keyring.backends
        import keyring.backends.SecretService as original
        from keyring.backend import KeyringBackend

        from tests.conftest import _SecretStorageHarness

        # A fresh import registers another backend class and repoints the
        # package attribute; restore both so later tests see one module.
        monkeypatch.setattr(KeyringBackend, "_classes", set(KeyringBackend._classes))
        monkeypatch.setattr(keyring.backends, "SecretService", original)
        monkeypatch.delitem(sys.modules, "keyring.backends.SecretService")
        inner = pytest.MonkeyPatch()
        _SecretStorageHarness().install(inner)
        inner.undo()
        import keyring.backends.SecretService as keyring_ss

        assert vars(keyring_ss).get("secretstorage") is sys.modules.get("secretstorage")

    def test_keyrings_own_secret_service_read_hits_the_spies(self, ss_harness):
        """keyring's SecretService module is rebound to the fake."""
        import keyring.backends.SecretService as keyring_ss

        assert keyring_ss.secretstorage is sys.modules["secretstorage"]
        ss_harness.locked = True
        with pytest.raises(AssertionError):
            _ss_member().get_password("mind-meld", "passphrase")
        assert ss_harness.prompt_calls
        assert set(ss_harness.prompt_calls) <= {
            "get_default_collection",
            "collection.unlock",
            "create_collection",
        }
