"""Local sidecar: this device's last successfully-pushed manifest.

Written at the end of every successful push; read during push recovery when
`_fetch_remote_manifest` returns CORRUPT. Unlike peer-fallback recovery, the
sidecar preserves THIS device's fresh deletions (files deleted locally since
the last good push but not yet propagated to peers).

Plaintext JSON on the local filesystem. Same trust boundary as `~/.claude/`
itself — the source files this tool syncs are plaintext on disk, so a
plaintext manifest snapshot of them does not widen the threat model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mind_meld import fsutil

SIDECAR_DIR = Path.home() / ".config" / "mind-meld"


def sidecar_path() -> Path:
    """Canonical sidecar location."""
    return SIDECAR_DIR / "last-push.json"


def write(manifest: dict[str, Any]) -> None:
    """Atomically write the manifest as the last-successful-push sidecar.

    Writes via fsutil.atomic_write_bytes with fsync=True — local crash
    durability matters here because the sidecar is consulted on the next
    push when peer manifests are corrupt (see cli.py corrupt-manifest
    recovery chain). A sidecar that was renamed but not fsynced would
    silently vanish on crash, defeating TODOS #1.

    Raises StorageError on filesystem failures. Caller handles
    (push writes best-effort; a failure warns but does not abort push).
    """
    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    target = sidecar_path()
    data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    # Sidecar holds this device's deletion record for corrupt-manifest
    # recovery — 0600 because it's internal state, not user content.
    fsutil.atomic_write_bytes(target, data, fsync=True, mode=0o600)


def read(expected_device_id: str) -> dict[str, Any] | None:
    """Read the sidecar manifest if present, parseable, and device-matched.

    Returns None in any of these cases, all treated as "no sidecar" for
    recovery purposes (caller falls through to peer fallback):
      - file doesn't exist (first-run)
      - file is unreadable / truncated JSON / not a JSON object
      - sidecar's `device_id` doesn't match `expected_device_id` (stale
        sidecar from a previous `mm init`, or a different mm config sharing
        the same home directory — blindly trusting it would bulk-tombstone
        files the current device never had)
      - required structural keys missing (`sources` and `tombstones` must
        both be dicts) — guards against tampering that would otherwise
        inject fake tombstones on the next push

    Explicit device_id scoping is load-bearing: the sidecar is plaintext
    local state, and its contents flow directly into `generate_tombstones`
    as the prior-state comparison basis. A stale or tampered sidecar would
    become a fleet-wide delete primitive.
    """
    target = sidecar_path()
    if not target.exists():
        return None
    try:
        with open(target) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # Structural shape check: sources and tombstones must be dicts.
    sources = data.get("sources")
    tombstones = data.get("tombstones")
    if not isinstance(sources, dict) or not isinstance(tombstones, dict):
        return None
    # Device-id scope check: refuse a sidecar from a different device/config.
    if data.get("device_id") != expected_device_id:
        return None
    return data
