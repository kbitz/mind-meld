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

MANIFESTS_PREFIX = "manifests/"
DATA_PREFIX = "data/"
DEVICES_PREFIX = "devices/"

# Storage-root bootstrap blob (not device-scoped). Plaintext root_salt +
# keycheck blob. See crypto.py for the byte layout.
CRYPTO_INIT_KEY = "mm-crypto-init"


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
    _validate_component(sha, "sha")
    return f"{DATA_PREFIX}{device_id}/{sha}.enc"


def device_key(device_id: str) -> str:
    """Key for a device's metadata JSON."""
    _validate_component(device_id, "device_id")
    return f"{DEVICES_PREFIX}{device_id}.json"


def parse_blob_key(key: str) -> tuple[str, str] | None:
    """Parse a blob key into (device_id, sha256). Returns None on malformed.

    Depth-only validation: must start with `data/`, end with `.enc`, and
    have exactly 3 segments. Does NOT validate hex shape of sha or the
    format of device_id — shape validation is a separate concern (see
    TODOS.md 'GC: validate blob shape, not just depth').
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
    if not sha:
        return None
    return device_id, sha
