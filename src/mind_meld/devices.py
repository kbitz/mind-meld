"""Device registration and listing for Mind Meld."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from mind_meld import __version__
from mind_meld.errors import StorageError
from mind_meld.storage.keys import DEVICES_PREFIX, device_key
from mind_meld.storage.local import LocalBackend


def register_device(
    backend: LocalBackend,
    device_id: str,
    device_name: str,
) -> None:
    """Write device metadata to storage.

    Does NOT seed `last_seen` at registration; `last_seen` means "time of
    last push" (not "time of last activity"). Callers display missing
    `last_seen` as em-dash so a registered-but-never-pushed device doesn't
    look like it pushed at registration time.
    """
    data = {
        "device_id": device_id,
        "device_name": device_name,
        "registered": datetime.now(timezone.utc).isoformat(),
    }
    key = device_key(device_id)
    backend.put(key, json.dumps(data, indent=2).encode("utf-8"))


def update_last_seen(
    backend: LocalBackend,
    device_id: str,
) -> None:
    """Update the `last_seen` timestamp + `last_seen_version` for a device.

    Semantic: `last_seen` records the time of this device's LAST PUSH.
    Pull does NOT update it -- a read-only device is correctly shown as
    "never pushed" rather than appearing active via pulls.

    `last_seen_version` (v0.9.2+) records the mm version that performed
    the last push. Used by Track 5E's strict pull-start fleet-version
    refusal — without it, a v0.9.2 puller can't tell whether a peer's
    `.sync-conflict-*` files were produced under pre-inversion or post-
    inversion semantics. Forward-compatible: older mm reads ignore
    unknown keys.
    """
    key = device_key(device_id)
    try:
        data = json.loads(backend.get(key))
    except StorageError:
        return  # Device not registered yet -- skip silently
    data["last_seen"] = datetime.now(timezone.utc).isoformat()
    data["last_seen_version"] = __version__
    backend.put(key, json.dumps(data, indent=2).encode("utf-8"))


def list_devices(backend: LocalBackend) -> list[dict[str, Any]]:
    """List all registered devices from storage.

    Drops entries that fail to load OR fail shape validation. Callers index
    `d["device_id"]` and `d["device_name"]` directly (e.g. cli.py's pull and
    `mm devices` table), so a JSON-valid but shape-invalid entry (non-dict
    top level, missing `device_id`, etc.) would crash the CLI without this
    guard.

    Dropped entries emit a per-entry warning via `stderr_console` at the
    call site — see `_list_devices_with_warnings` in cli.py. Direct callers
    that import this function get silent drops (unchanged default) so
    library-mode consumers can read `devices` without side-effect output.
    """
    return _list_devices_impl(backend, on_drop=None)


def list_devices_with_drops(
    backend: LocalBackend,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """List valid devices AND collect dropped (key, reason) pairs.

    Variant of `list_devices` for Track 5E's strict pull-start fleet-version
    refusal: a corrupt/shape-invalid peer device.json must REFUSE the pull
    (not silently skip), because we can't read its `last_seen_version` and
    so can't tell if its conflict files are pre- or post-inversion. The
    refusal gate names the storage key.

    Returns `(valid_devices, dropped_entries)` where each dropped entry is
    `(storage_key, reason_string)`. Reason strings come from `_list_devices_impl`'s
    on_drop callback verbatim (e.g. "unreadable (StorageError)", "not a
    JSON object", "missing or invalid device_id", "missing or invalid
    device_name").
    """
    drops: list[tuple[str, str]] = []
    valid = _list_devices_impl(backend, on_drop=lambda k, r: drops.append((k, r)))
    return valid, drops


def _list_devices_impl(
    backend: LocalBackend,
    on_drop: "Any | None" = None,
) -> list[dict[str, Any]]:
    """Shared implementation. `on_drop`, if provided, is called with
    (key, reason) for each dropped entry — cli.py uses this to emit warnings.
    """
    keys = backend.list_keys(DEVICES_PREFIX)
    devices = []
    for key in keys:
        if not key.endswith(".json"):
            continue
        try:
            raw = json.loads(backend.get(key))
        except (StorageError, json.JSONDecodeError) as e:
            if on_drop is not None:
                on_drop(key, f"unreadable ({type(e).__name__})")
            continue
        # Shape validation: top-level must be a dict with a non-empty string
        # device_id and a string device_name. Anything else would crash
        # downstream `d["device_id"]` / `d["device_name"]` indexing.
        if not isinstance(raw, dict):
            if on_drop is not None:
                on_drop(key, "not a JSON object")
            continue
        did = raw.get("device_id")
        dname = raw.get("device_name")
        if not isinstance(did, str) or not did:
            if on_drop is not None:
                on_drop(key, "missing or invalid device_id")
            continue
        if not isinstance(dname, str):
            if on_drop is not None:
                on_drop(key, "missing or invalid device_name")
            continue
        devices.append(raw)
    return devices
