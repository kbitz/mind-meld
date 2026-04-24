"""Storage key helpers for Mind Meld.

Every storage key used by the CLI is constructed through one of these
helpers. The prefixes are exposed so `backend.list_keys(...)` callers
don't re-invent them either. Keep this module pure: no imports from
other mind_meld modules, no I/O.

Key shapes:
    manifests/{device_id}/manifest.json.enc   — per-device manifest blob
    data/{device_id}/{sha256}.enc             — content-addressed encrypted blob
    devices/{device_id}.json                  — per-device metadata JSON
    mm-crypto-init                            — storage-root bootstrap blob
"""

from __future__ import annotations

import re

MANIFESTS_PREFIX = "manifests/"
DATA_PREFIX = "data/"
DEVICES_PREFIX = "devices/"

# Storage-root bootstrap blob (not device-scoped). Plaintext root_salt +
# keycheck blob. See crypto.py for the byte layout.
CRYPTO_INIT_KEY = "mm-crypto-init"

# SHA-256 hex shape: exactly 64 lowercase hex chars. Used by both blob_key()
# construction and parse_blob_key() parsing so a corrupt peer manifest
# shipping `data/{dev}/not-a-sha.enc` can't be smuggled through GC as an
# "orphan" (it was reaped in v0.8.x). device_id is intentionally NOT
# shape-validated because historical/test fixtures use short non-hex IDs.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _validate_hex_sha(value: str) -> None:
    """Reject a sha component that isn't exactly 64 lowercase hex chars.

    Threat model: corrupt or malicious peer manifests ship sha values. A
    non-hex sha landing in `data/{dev}/{sha}.enc` was previously reaped by
    `mm gc` as an orphan (since it can't match anything in
    `referenced_hashes`). Reject at construction and parse to route those
    through the malformed-count path in `_do_gc` instead.

    Guards against non-string input (e.g. `{"sha256": null}` or a numeric
    JSON value from a corrupt peer manifest) by raising ValueError, not
    TypeError — callers catching ValueError stay uniform. Without the
    isinstance guard, fullmatch() would raise TypeError on non-strings and
    escape per-file error handling in _download_and_apply().
    """
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(
            f"storage key: sha must be 64 lowercase hex chars, got {value!r}"
        )


def _validate_component(value: str, name: str) -> None:
    """Reject attacker-controlled path components.

    Defense-in-depth: a corrupt or malicious peer manifest could ship a
    sha256 like "../../../etc/passwd" that, when fed through blob_key(),
    produces a key that escapes the storage root under backend.get().
    Reject path separators, parent-dir refs, null bytes, and empties at
    the constructor boundary.
    """
    if not value:
        raise ValueError(f"storage key: {name} must be non-empty")
    if value in (".", ".."):
        raise ValueError(f"storage key: {name} must not be '.' or '..'")
    if "/" in value or "\\" in value:
        raise ValueError(f"storage key: {name} must not contain path separators")
    if "\x00" in value:
        raise ValueError(f"storage key: {name} must not contain null bytes")


def manifest_key(device_id: str) -> str:
    """Canonical manifest key for a device."""
    _validate_component(device_id, "device_id")
    return f"{MANIFESTS_PREFIX}{device_id}/manifest.json.enc"


def blob_key(device_id: str, sha: str) -> str:
    """Key for a content-addressed encrypted file blob."""
    _validate_component(device_id, "device_id")
    # _validate_hex_sha strictly subsumes _validate_component for sha: every
    # value the latter would reject (empty, '.', '..', path separator, null
    # byte) is already non-hex and fails the 64-lowercase-hex fullmatch.
    _validate_hex_sha(sha)
    return f"{DATA_PREFIX}{device_id}/{sha}.enc"


def device_key(device_id: str) -> str:
    """Key for a device's metadata JSON."""
    _validate_component(device_id, "device_id")
    return f"{DEVICES_PREFIX}{device_id}.json"


def parse_blob_key(key: str) -> tuple[str, str] | None:
    """Parse a blob key into (device_id, sha256). Returns None on malformed.

    Depth + sha-shape validation: must start with `data/`, end with `.enc`,
    have exactly 3 segments, and the leaf sha must match `[0-9a-f]{64}`
    (fullmatch). device_id is NOT shape-validated — historical registrations
    and test fixtures use short non-hex IDs. The sha check is what keeps a
    corrupt peer manifest's `data/{dev}/not-a-sha.enc` out of `_do_gc`'s
    orphan-reaping set — such entries land in the malformed-count path.
    """
    if not key.startswith(DATA_PREFIX) or not key.endswith(".enc"):
        return None
    parts = key.split("/")
    if len(parts) != 3:
        return None
    _, device_id, leaf = parts
    if not device_id:
        return None
    sha = leaf[: -len(".enc")]
    if not _SHA256_RE.fullmatch(sha):
        return None
    return device_id, sha
