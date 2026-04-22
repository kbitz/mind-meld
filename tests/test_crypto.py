"""Tests for mind_meld.crypto — encryption, decryption, versioning, compression."""

import os

import pytest

from mind_meld.crypto import (
    FORMAT_VERSION,
    NONCE_LEN,
    SALT_LEN,
    decrypt,
    derive_key,
    encrypt,
    get_passphrase,
)
from mind_meld.errors import CryptoError


PASSPHRASE = "test-passphrase-123"
# Use low memory for fast tests
MEMORY_KB = 1024


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


class TestEncryptDecrypt:
    def test_round_trip(self):
        data = b"hello world, this is session data"
        blob = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        result = decrypt(blob, PASSPHRASE, memory_kb=MEMORY_KB)
        assert result == data

    def test_round_trip_empty(self):
        data = b""
        blob = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        result = decrypt(blob, PASSPHRASE, memory_kb=MEMORY_KB)
        assert result == data

    def test_round_trip_large(self):
        data = os.urandom(1_000_000)
        blob = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        result = decrypt(blob, PASSPHRASE, memory_kb=MEMORY_KB)
        assert result == data

    def test_version_byte(self):
        blob = encrypt(b"test", PASSPHRASE, memory_kb=MEMORY_KB)
        assert blob[0] == FORMAT_VERSION

    def test_compression_reduces_size(self):
        # Highly compressible JSON
        data = b'{"key": "value", ' * 1000 + b'"end": true}'
        blob = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        # Blob should be significantly smaller than plaintext
        # (version + salt + nonce + compressed + tag overhead)
        assert len(blob) < len(data)

    def test_different_ciphertext_each_time(self):
        data = b"same data"
        blob1 = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        blob2 = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
        # Different salt + nonce → different ciphertext
        assert blob1 != blob2

    def test_wrong_passphrase_raises(self):
        blob = encrypt(b"secret", PASSPHRASE, memory_kb=MEMORY_KB)
        with pytest.raises(CryptoError, match="GCM tag mismatch"):
            decrypt(blob, "wrong-passphrase", memory_kb=MEMORY_KB)

    def test_corrupt_blob_raises(self):
        blob = encrypt(b"test", PASSPHRASE, memory_kb=MEMORY_KB)
        # Flip a byte in the ciphertext
        corrupted = bytearray(blob)
        corrupted[-5] ^= 0xFF
        with pytest.raises(CryptoError):
            decrypt(bytes(corrupted), PASSPHRASE, memory_kb=MEMORY_KB)

    def test_truncated_blob_raises(self):
        with pytest.raises(CryptoError, match="too short"):
            decrypt(b"\x01" + b"\x00" * 10, PASSPHRASE, memory_kb=MEMORY_KB)

    def test_unsupported_version_raises(self):
        blob = encrypt(b"test", PASSPHRASE, memory_kb=MEMORY_KB)
        bad_version = bytes([0x99]) + blob[1:]
        with pytest.raises(CryptoError, match="unsupported format version"):
            decrypt(bad_version, PASSPHRASE, memory_kb=MEMORY_KB)

    def test_blob_structure(self):
        blob = encrypt(b"test", PASSPHRASE, memory_kb=MEMORY_KB)
        assert blob[0] == 0x01  # version
        assert len(blob) >= 1 + SALT_LEN + NONCE_LEN + 1


class TestGetPassphrase:
    def test_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "from-env")
        # Disable keyring by making import fail
        monkeypatch.delattr("keyring.get_password", raising=False)
        result = get_passphrase()
        assert result == "from-env"
