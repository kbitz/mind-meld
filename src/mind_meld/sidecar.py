"""Local sidecar: this device's last successfully-pushed manifest.

Written at the end of every successful push; read during push recovery when
`_fetch_remote_manifest` returns CORRUPT. Unlike peer-fallback recovery, the
sidecar preserves THIS device's fresh deletions (files deleted locally since
the last good push but not yet propagated to peers).

Plaintext JSON on the local filesystem. Same trust boundary as `~/.claude/`
itself — the source files this tool syncs are plaintext on disk, so a
plaintext manifest snapshot of them does not widen the threat model.

Read-path invariant: `read()` returns a v2-normalized manifest, the same shape
`_fetch_remote_manifest` returns. We deserialize first, run the structural-shape
check on the RAW dict (so a tampered sidecar missing keys is rejected, not
silently synthesized), then normalize. DO NOT add a new manifest-load path
that bypasses this sequence.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mind_meld.errors import ManifestError
from mind_meld.manifest import deserialize_manifest, normalize_manifest

SIDECAR_DIR = Path.home() / ".config" / "mind-meld"


def sidecar_path() -> Path:
    """Canonical sidecar location."""
    return SIDECAR_DIR / "last-push.json"


def write(manifest: dict[str, Any]) -> None:
    """Atomically write the manifest as the last-successful-push sidecar.

    Raises OSError on filesystem failures. Caller must handle (push writes
    a sidecar best-effort; a failure warns but does not abort push).
    """
    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    target = sidecar_path()

    fd, tmp_name = tempfile.mkstemp(
        dir=SIDECAR_DIR, prefix="last-push.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
        # Deserialize-then-validate-then-normalize: the structural-shape check
        # MUST run against the raw parsed dict, NOT a normalized one. If we
        # let `load_manifest` synthesize `sources`/`tombstones` first, a
        # tampered sidecar missing those keys would pass the structural check
        # and silently zero out tombstones on the next push.
        raw = deserialize_manifest(target.read_bytes())
    except (OSError, ManifestError):
        return None
    if not isinstance(raw, dict):
        return None
    # Structural shape check: sources and tombstones must be dicts.
    sources = raw.get("sources")
    tombstones = raw.get("tombstones")
    if not isinstance(sources, dict) or not isinstance(tombstones, dict):
        return None
    # Device-id scope check: refuse a sidecar from a different device/config.
    if raw.get("device_id") != expected_device_id:
        return None
    return normalize_manifest(raw)
