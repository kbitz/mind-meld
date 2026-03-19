"""Encryption and key derivation for MemSync.

Blob format (v1):
    [version:1][salt:16][nonce:12][gzip_compressed_ciphertext + GCM_auth_tag]

    - Byte 0:     format version (0x01)
    - Bytes 1-16: random salt (unique per encryption)
    - Bytes 17-28: random nonce (unique per encryption)
    - Bytes 29+:  AES-256-GCM ciphertext of gzip-compressed plaintext, with 16-byte auth tag

Key derivation: Argon2id(passphrase, salt) → 256-bit key
Passphrase retrieval: keyring → MEMSYNC_PASSPHRASE env var → interactive prompt
"""

from __future__ import annotations

import gzip
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from memsync.errors import CryptoError

FORMAT_VERSION = 0x01
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32  # 256 bits

# Argon2id defaults
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 1

KEYRING_SERVICE = "memsync"
KEYRING_USERNAME = "passphrase"
ENV_VAR = "MEMSYNC_PASSPHRASE"


def derive_key(
    passphrase: str,
    salt: bytes,
    memory_kb: int = 65_536,
) -> bytes:
    """Derive a 256-bit key from a passphrase using Argon2id."""
    try:
        from argon2.low_level import Type, hash_secret_raw
    except ImportError:
        raise CryptoError(
            "init: argon2-cffi not installed. Run: pip install argon2-cffi"
        )

    if not passphrase:
        raise CryptoError("init: passphrase cannot be empty.")

    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=memory_kb,
        parallelism=ARGON2_PARALLELISM,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def encrypt(plaintext: bytes, passphrase: str, memory_kb: int = 65_536) -> bytes:
    """Compress, then encrypt with AES-256-GCM. Returns versioned blob."""
    compressed = gzip.compress(plaintext)
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = derive_key(passphrase, salt, memory_kb)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, compressed, None)
    return bytes([FORMAT_VERSION]) + salt + nonce + ciphertext


def decrypt(blob: bytes, passphrase: str, memory_kb: int = 65_536) -> bytes:
    """Decrypt and decompress a versioned blob."""
    if len(blob) < 1 + SALT_LEN + NONCE_LEN + 1:
        raise CryptoError("decrypt: blob too short — corrupt or truncated data.")

    version = blob[0]
    if version != FORMAT_VERSION:
        raise CryptoError(
            f"decrypt: unsupported format version 0x{version:02x}. "
            f"Expected 0x{FORMAT_VERSION:02x}. Update msync?"
        )

    salt = blob[1 : 1 + SALT_LEN]
    nonce = blob[1 + SALT_LEN : 1 + SALT_LEN + NONCE_LEN]
    ciphertext = blob[1 + SALT_LEN + NONCE_LEN :]

    key = derive_key(passphrase, salt, memory_kb)
    aesgcm = AESGCM(key)

    try:
        compressed = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception:
        raise CryptoError(
            "decrypt: GCM tag mismatch — wrong passphrase or corrupt data."
        )

    try:
        return gzip.decompress(compressed)
    except Exception:
        raise CryptoError("decrypt: decompression failed — data may be corrupt.")


def get_passphrase() -> str:
    """Retrieve passphrase via keyring → env var → prompt fallback chain.

    Returns the passphrase string. All commands call this — no duplication.
    """
    # Try keyring first
    try:
        import keyring as kr

        stored = kr.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if stored is not None:
            return stored
    except Exception:
        pass

    # Try environment variable
    env_passphrase = os.environ.get(ENV_VAR)
    if env_passphrase:
        return env_passphrase

    # Interactive prompt
    try:
        import getpass

        passphrase = getpass.getpass("MemSync passphrase: ")
        if not passphrase:
            raise CryptoError("init: passphrase cannot be empty.")
        return passphrase
    except (EOFError, KeyboardInterrupt):
        raise CryptoError("init: passphrase input cancelled.")


def store_passphrase_in_keyring(passphrase: str) -> bool:
    """Store the passphrase in the OS keyring. Returns False if unavailable."""
    try:
        import keyring as kr

        kr.set_password(KEYRING_SERVICE, KEYRING_USERNAME, passphrase)
        return True
    except Exception:
        return False
