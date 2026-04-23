"""Encryption and key derivation for Mind Meld.

Blob format (v2):
    [version:1=0x02][salt:16][nonce:12][gzip_compressed_ciphertext + GCM_auth_tag]

Key derivation:
    Bootstrap (first device on fresh storage):
        root_salt = os.urandom(16)
        mm-crypto-init = [version=0x02][argon2_memory_kb:4 BE][root_salt:16][keycheck_blob:*]
        keycheck_blob = encrypt(b"mm-keycheck-v1", master_key)  # v2 format

    Per process (once, cached in _MASTER_KEY_CACHE):
        master_key = Argon2id(passphrase, root_salt, time=3, memory_kb, parallelism=1)

    Per file (microseconds):
        file_key = HKDF-SHA256(master_key, salt=per_file_salt, info=b"mm-file-v2", L=32)
        blob = AES-256-GCM(file_key, nonce, gzip(plaintext))

Why this shape:
    v1 (pre-Track-1C) derived a new Argon2 key per file with a random salt. A 1000-file
    push burned ~2-4 minutes of crypto-only time. An LRU on (passphrase, salt) has 0%
    hit rate because per-file salts are random. The fix is to cache a master_key per
    process and derive per-file keys via HKDF (microseconds). Matches age, restic,
    rclone patterns.

mm-crypto-init lives at the storage root, unencrypted (the salt is not secret).
argon2_memory_kb is stored alongside root_salt because it's a crypto input: different
devices with different local config values would derive different master_keys from
the same passphrase + salt, making blobs unreadable across devices.
"""

from __future__ import annotations

import gzip
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Literal

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mind_meld.errors import CryptoError, StorageError

FORMAT_VERSION = 0x02
FORMAT_VERSION_LEGACY_V1 = 0x01  # recognized to fail loud; no back-compat decryption

SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32  # 256 bits
ROOT_SALT_LEN = 16
MEMORY_KB_FIELD_LEN = 4  # big-endian uint32

ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 1

HKDF_INFO = b"mm-file-v2"
_KEYCHECK_PLAINTEXT = b"mm-keycheck-v1"

CRYPTO_INIT_KEY = "mm-crypto-init"

KEYRING_SERVICE = "mind-meld"
KEYRING_USERNAME = "passphrase"
ENV_VAR = "MINDMELD_PASSPHRASE"

# Module-level process-scoped state.
# _MASTER_KEY_CACHE is keyed by sha256(passphrase) || root_salt || memory_kb.to_bytes(4,"big").
# _SESSION holds the fetched root_salt + memory_kb for the current process; set by
# set_crypto_session() at each command's start after fetch_crypto_init().
_MASTER_KEY_CACHE: dict[bytes, bytes] = {}
_SESSION: dict[str, Any] = {}


# ── session state ────────────────────────────────────────────────────


def set_crypto_session(root_salt: bytes, memory_kb: int) -> None:
    """Pin root_salt + memory_kb for the current process.

    Called by cli.py at the start of each command after fetch_crypto_init().
    Replace semantics: calling twice with different values overwrites. Real
    cross-process drift detection lives in cli._init_crypto_session via the
    local config's root_salt_fp field; in-process refuse would fire spuriously
    when the same process runs CliRunner tests or chains commands.
    """
    if len(root_salt) != ROOT_SALT_LEN:
        raise CryptoError(
            f"crypto: root_salt wrong length ({len(root_salt)}, expected {ROOT_SALT_LEN})."
        )
    _SESSION["root_salt"] = root_salt
    _SESSION["memory_kb"] = memory_kb


def clear_crypto_session() -> None:
    """Reset session + cache. For tests only."""
    _SESSION.clear()
    _MASTER_KEY_CACHE.clear()


def root_salt_fingerprint(root_salt: bytes) -> str:
    """Short hex fingerprint for drift detection and display. 8 bytes = 16 hex chars."""
    return hashlib.sha256(root_salt).hexdigest()[:16]


# ── key derivation ───────────────────────────────────────────────────


def derive_key(passphrase: str, salt: bytes, memory_kb: int = 65_536) -> bytes:
    """Derive a 256-bit key from a passphrase using Argon2id.

    Low-level primitive used by load_master_key(). Tests call this directly to
    validate Argon2 wiring.
    """
    if not passphrase:
        raise CryptoError("init: passphrase cannot be empty.")
    try:
        return hash_secret_raw(
            secret=passphrase.encode("utf-8"),
            salt=salt,
            time_cost=ARGON2_TIME_COST,
            memory_cost=memory_kb,
            parallelism=ARGON2_PARALLELISM,
            hash_len=KEY_LEN,
            type=Type.ID,
        )
    except Exception as e:
        # argon2-cffi raises from native code on OOM and similar. Translate to a
        # user-actionable CryptoError so callers don't see a cryptic native trace.
        raise CryptoError(
            f"key derivation: argon2 failed at memory_kb={memory_kb} — {e}. "
            f"Reduce [crypto].argon2_memory_kb in config.toml if this machine "
            f"cannot allocate {memory_kb}KB."
        ) from e


def load_master_key(passphrase: str, root_salt: bytes, memory_kb: int) -> bytes:
    """Return the master_key for this (passphrase, root_salt, memory_kb) tuple.

    Argon2id once per unique tuple per process; cached in _MASTER_KEY_CACHE. Subsequent
    calls with the same tuple are a dict lookup (microseconds).
    """
    if not passphrase:
        raise CryptoError("init: passphrase cannot be empty.")
    if len(root_salt) != ROOT_SALT_LEN:
        raise CryptoError(
            f"crypto: root_salt wrong length ({len(root_salt)}, expected {ROOT_SALT_LEN})."
        )
    cache_key = (
        hashlib.sha256(passphrase.encode("utf-8")).digest()
        + root_salt
        + memory_kb.to_bytes(MEMORY_KB_FIELD_LEN, "big")
    )
    cached = _MASTER_KEY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    master_key = derive_key(passphrase, root_salt, memory_kb=memory_kb)
    _MASTER_KEY_CACHE[cache_key] = master_key
    return master_key


def _derive_file_key(master_key: bytes, salt: bytes) -> bytes:
    """HKDF-SHA256(master_key, per_file_salt, info=b'mm-file-v2', L=32)."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        info=HKDF_INFO,
    )
    return hkdf.derive(master_key)


# ── encrypt / decrypt (v2) ───────────────────────────────────────────


def _current_session_or_raise() -> tuple[bytes, int]:
    root_salt = _SESSION.get("root_salt")
    memory_kb = _SESSION.get("memory_kb")
    if root_salt is None or memory_kb is None:
        raise CryptoError(
            "crypto: no active session. Call set_crypto_session() after "
            "fetch_crypto_init() at the start of the command."
        )
    return root_salt, memory_kb


def encrypt(plaintext: bytes, passphrase: str, memory_kb: int = 65_536) -> bytes:
    """Compress, then encrypt with AES-256-GCM using a HKDF-derived per-file key.

    Produces a v2-format blob. Requires an active session (see set_crypto_session).

    The `memory_kb` arg is kept for backward-compatible call sites but must match the
    session's memory_kb — mismatches indicate config drift and are refused loudly.
    """
    session_root_salt, session_memory_kb = _current_session_or_raise()
    if memory_kb != session_memory_kb:
        raise CryptoError(
            f"crypto: memory_kb mismatch — caller passed {memory_kb}, session has "
            f"{session_memory_kb}. Config drifted from mm-crypto-init; re-derive "
            f"memory_kb from fetch_crypto_init()."
        )
    master_key = load_master_key(passphrase, session_root_salt, session_memory_kb)
    return _encrypt_with_master_key(plaintext, master_key)


def _encrypt_with_master_key(plaintext: bytes, master_key: bytes) -> bytes:
    """Encrypt with an already-derived master_key. Used by encrypt() and bootstrap."""
    compressed = gzip.compress(plaintext)
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    file_key = _derive_file_key(master_key, salt)
    aesgcm = AESGCM(file_key)
    ciphertext = aesgcm.encrypt(nonce, compressed, None)
    return bytes([FORMAT_VERSION]) + salt + nonce + ciphertext


def decrypt(blob: bytes, passphrase: str, memory_kb: int = 65_536) -> bytes:
    """Decrypt a v2-format blob. Requires an active session.

    v1 blobs (format byte 0x01) are recognized and refused loudly; Mind Meld is
    pre-release, has no v1 blobs in any user's storage, and dropping the v1
    decryption path keeps the code honest.
    """
    session_root_salt, session_memory_kb = _current_session_or_raise()
    if memory_kb != session_memory_kb:
        raise CryptoError(
            f"crypto: memory_kb mismatch — caller passed {memory_kb}, session has "
            f"{session_memory_kb}. Config drifted from mm-crypto-init."
        )
    master_key = load_master_key(passphrase, session_root_salt, session_memory_kb)
    return _decrypt_with_master_key(blob, master_key)


def _decrypt_with_master_key(blob: bytes, master_key: bytes) -> bytes:
    """Decrypt with an already-derived master_key. Used by decrypt() and verify."""
    if len(blob) < 1 + SALT_LEN + NONCE_LEN + 1:
        raise CryptoError("decrypt: blob too short — corrupt or truncated data.")

    version = blob[0]
    if version == FORMAT_VERSION_LEGACY_V1:
        raise CryptoError(
            "decrypt: v1 blob found (format 0x01). Mind Meld v0.6+ does not support "
            "v1 blobs. This should not appear in any user's storage; if you see it, "
            "please file a bug."
        )
    if version != FORMAT_VERSION:
        raise CryptoError(
            f"decrypt: unsupported format version 0x{version:02x}. "
            f"Expected 0x{FORMAT_VERSION:02x}. Update mm?"
        )

    salt = blob[1 : 1 + SALT_LEN]
    nonce = blob[1 + SALT_LEN : 1 + SALT_LEN + NONCE_LEN]
    ciphertext = blob[1 + SALT_LEN + NONCE_LEN :]

    file_key = _derive_file_key(master_key, salt)
    aesgcm = AESGCM(file_key)

    try:
        compressed = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception:
        raise CryptoError(
            "decrypt: GCM tag mismatch — wrong passphrase, wrong root_salt in "
            "mm-crypto-init, or corrupt blob. Verify mm-crypto-init integrity if "
            "other decrypts also fail."
        )

    try:
        return gzip.decompress(compressed)
    except Exception:
        raise CryptoError("decrypt: decompression failed — data may be corrupt.")


# ── mm-crypto-init: bootstrap + fetch + verify ───────────────────────


@dataclass(frozen=True)
class CryptoInitFetch:
    """Tri-state result of reading mm-crypto-init from storage.

    status:
        "ok"      — blob present and parsed; fields populated.
        "missing" — no canonical blob and no iCloud conflict copies. First device.
        "corrupt" — blob(s) present but failed to parse. Refuse; do not bootstrap.
    """

    status: Literal["ok", "missing", "corrupt"]
    root_salt: bytes | None = None
    argon2_memory_kb: int | None = None
    keycheck_blob: bytes | None = None


def _parse_crypto_init(data: bytes) -> CryptoInitFetch:
    """Parse an mm-crypto-init blob. Returns ok or corrupt; never missing."""
    minimum = 1 + MEMORY_KB_FIELD_LEN + ROOT_SALT_LEN + 1
    if len(data) < minimum:
        return CryptoInitFetch(status="corrupt")
    version = data[0]
    if version != FORMAT_VERSION:
        return CryptoInitFetch(status="corrupt")
    argon2_memory_kb = int.from_bytes(
        data[1 : 1 + MEMORY_KB_FIELD_LEN], "big"
    )
    # Sanity bounds: 1KB floor, 1GB ceiling (argon2 would OOM long before 1GB).
    if argon2_memory_kb < 1_024 or argon2_memory_kb > 1_048_576:
        return CryptoInitFetch(status="corrupt")
    root_salt = data[1 + MEMORY_KB_FIELD_LEN : 1 + MEMORY_KB_FIELD_LEN + ROOT_SALT_LEN]
    keycheck_blob = data[1 + MEMORY_KB_FIELD_LEN + ROOT_SALT_LEN :]
    # keycheck_blob must itself be a valid v2 blob header.
    if len(keycheck_blob) < 1 + SALT_LEN + NONCE_LEN + 1 or keycheck_blob[0] != FORMAT_VERSION:
        return CryptoInitFetch(status="corrupt")
    return CryptoInitFetch(
        status="ok",
        root_salt=root_salt,
        argon2_memory_kb=argon2_memory_kb,
        keycheck_blob=keycheck_blob,
    )


def _serialize_crypto_init(
    argon2_memory_kb: int, root_salt: bytes, keycheck_blob: bytes
) -> bytes:
    if len(root_salt) != ROOT_SALT_LEN:
        raise CryptoError("crypto: root_salt wrong length at serialize.")
    return (
        bytes([FORMAT_VERSION])
        + argon2_memory_kb.to_bytes(MEMORY_KB_FIELD_LEN, "big")
        + root_salt
        + keycheck_blob
    )


def fetch_crypto_init(backend: Any) -> CryptoInitFetch:
    """Read mm-crypto-init from storage with iCloud conflict handling.

    If the canonical path is missing but conflict copies exist, pick the
    deterministic winner (lex-smallest root_salt), canonicalize it atomically,
    and delete the losers.

    Tri-state: ok / missing / corrupt. Callers must not treat "corrupt" as
    "missing" — doing so would re-bootstrap over existing valid state.
    """
    canonical_exists = backend.exists(CRYPTO_INIT_KEY)
    conflicts = backend.find_conflict_copies(CRYPTO_INIT_KEY)

    if not canonical_exists and not conflicts:
        return CryptoInitFetch(status="missing")

    # Gather all readable candidates with their parsed view.
    candidates: list[tuple[bytes, CryptoInitFetch, Any]] = []  # (raw, parsed, path_or_none)
    if canonical_exists:
        try:
            raw = backend.get(CRYPTO_INIT_KEY)
            parsed = _parse_crypto_init(raw)
            candidates.append((raw, parsed, None))
        except StorageError:
            pass

    for conflict_path in conflicts:
        try:
            raw = conflict_path.read_bytes()
        except OSError:
            continue
        parsed = _parse_crypto_init(raw)
        candidates.append((raw, parsed, conflict_path))

    if not candidates:
        return CryptoInitFetch(status="corrupt")

    ok_candidates = [c for c in candidates if c[1].status == "ok"]
    if not ok_candidates:
        return CryptoInitFetch(status="corrupt")

    # Deterministic winner: lex-smallest root_salt across OK candidates.
    ok_candidates.sort(key=lambda c: c[1].root_salt)  # type: ignore[arg-type]
    winner_raw, winner_parsed, winner_path = ok_candidates[0]

    # Canonicalization:
    # - If canonical exists but its parsed content is not the winner, overwrite it.
    # - If canonical doesn't exist or is corrupt, write winner to canonical.
    # - Delete all iCloud conflict copies (regardless of which was winner).
    canonical_raw: bytes | None = None
    if canonical_exists:
        try:
            canonical_raw = backend.get(CRYPTO_INIT_KEY)
        except StorageError:
            canonical_raw = None

    if canonical_raw != winner_raw:
        backend.put(CRYPTO_INIT_KEY, winner_raw)

    # Delete any lingering conflict copies (iCloud reconciliation leftovers).
    backend.delete_conflict_copies(CRYPTO_INIT_KEY)

    return winner_parsed


def bootstrap_crypto_init(
    backend: Any, passphrase: str, argon2_memory_kb: int
) -> CryptoInitFetch:
    """Generate mm-crypto-init for a fresh storage root. Called only on first device.

    Raises StorageError("already exists") if another device won the bootstrap race.
    Caller should handle that by re-running fetch_crypto_init and taking the
    verify path.
    """
    root_salt = os.urandom(ROOT_SALT_LEN)
    master_key = derive_key(passphrase, root_salt, memory_kb=argon2_memory_kb)
    keycheck_blob = _encrypt_with_master_key(_KEYCHECK_PLAINTEXT, master_key)
    blob = _serialize_crypto_init(argon2_memory_kb, root_salt, keycheck_blob)

    # put_exclusive raises StorageError on EEXIST. Caller inspects and retries.
    backend.put_exclusive(CRYPTO_INIT_KEY, blob)

    # Prime the cache so the caller's subsequent load_master_key is a hit.
    cache_key = (
        hashlib.sha256(passphrase.encode("utf-8")).digest()
        + root_salt
        + argon2_memory_kb.to_bytes(MEMORY_KB_FIELD_LEN, "big")
    )
    _MASTER_KEY_CACHE[cache_key] = master_key

    return CryptoInitFetch(
        status="ok",
        root_salt=root_salt,
        argon2_memory_kb=argon2_memory_kb,
        keycheck_blob=keycheck_blob,
    )


def verify_passphrase(master_key: bytes, keycheck_blob: bytes) -> None:
    """Decrypt the keycheck blob and assert plaintext == _KEYCHECK_PLAINTEXT.

    Raises CryptoError if the passphrase (master_key) does not match the one used
    to bootstrap this storage root. Message is user-actionable.
    """
    try:
        plaintext = _decrypt_with_master_key(keycheck_blob, master_key)
    except CryptoError as e:
        raise CryptoError(
            "init: passphrase does not match the one used to initialize this "
            "storage. Did you use the same passphrase on your other Mac?"
        ) from e
    if plaintext != _KEYCHECK_PLAINTEXT:
        raise CryptoError(
            "init: keycheck plaintext mismatch — mm-crypto-init may be tampered "
            "with. Refuse to proceed."
        )


# ── passphrase retrieval ─────────────────────────────────────────────


def get_passphrase() -> str:
    """Retrieve passphrase via keyring → env var → prompt fallback chain."""
    try:
        import keyring as kr

        stored = kr.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if stored is not None:
            return stored
    except Exception:
        pass

    env_passphrase = os.environ.get(ENV_VAR)
    if env_passphrase:
        return env_passphrase

    try:
        import getpass

        passphrase = getpass.getpass("Mind Meld passphrase: ")
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
