"""Device registration and listing for Mind Meld."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from mind_meld import __version__
from mind_meld.errors import StorageError
from mind_meld.storage.keys import DEVICES_PREFIX, device_key
from mind_meld.storage.local import LocalBackend

# Local sentinel for serializing read-modify-write of devices/<id>.json
# entries across concurrent autopush + interactive push (Group 7 preflight
# D10 — codex outside-voice finding #6). Lives in the per-machine config
# dir, not on synced storage — fcntl.flock is a local-process primitive.
DEVICES_WRITE_LOCK = Path.home() / ".config" / "mind-meld" / "devices-write.lock"


# Brief retry budget on lock contention. The critical section is one
# storage GET + one storage PUT (typically <100ms on local FS, longer on
# iCloud cold-cache), so total acquire wait stays well under 1 second.
_LOCK_RETRY_INTERVALS_S: tuple[float, ...] = (0.05, 0.1, 0.2, 0.4)


@contextmanager
def _devices_write_lock() -> Iterator[None]:
    """Serialize read-modify-write of any devices/<id>.json mutator.

    All RMW callers (today: `update_last_seen`; future field-adders inherit
    safety by routing through this lock) must hold the flock for the read
    AND write so an interleaved read can't observe a partial state. Today's
    fields (`last_seen`, `last_seen_version`) are deterministic per-process,
    so concurrent writers without this lock would not lose data — the lock
    is forward-defense for the moment a non-deterministic field lands.

    Lock-file dir creation / open failures propagate to the caller. The
    lock file lives under ~/.config/mind-meld/ which mm controls; a
    permission failure here indicates a broken install and surfacing it
    via exception is more diagnostic than silently degrading the lock.

    Acquire is non-blocking (LOCK_EX | LOCK_NB) with a brief retry budget.
    On exhausted retries, degrade to executing without the lock and emit
    one `mm: warning:` line to stderr (visible-failure contract). Today's
    deterministic fields are safe under degraded operation; the warning
    lets the user catch a stuck-process scenario before a future non-
    deterministic field starts losing data.
    """
    DEVICES_WRITE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(DEVICES_WRITE_LOCK), os.O_WRONLY | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            for delay in _LOCK_RETRY_INTERVALS_S:
                time.sleep(delay)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    continue
        if not locked:
            sys.stderr.write(
                "mm: warning: device write lock contended; "
                "skipping last_seen update for this push\n"
            )
        try:
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def register_device(
    backend: LocalBackend,
    device_id: str,
    device_name: str,
) -> None:
    """Register a device entry on storage. No-op if the entry already exists.

    Idempotent on re-call: ensures the entry exists, never bumps the
    `registered:` timestamp. Self-heal callers (`_ensure_device_registered`)
    can re-register safely knowing that an existing first-registration time
    is preserved.

    Uses `LocalBackend.put_exclusive` (atomic os.link with EEXIST detection)
    so the create-only invariant holds even when iCloud `.icloud` placeholder
    state hides the existing entry from `backend.exists()` (Group 7 preflight
    D8 + codex outside-voice finding #6).

    Does NOT seed `last_seen` at registration; `last_seen` means "time of
    last push" (not "time of last activity"). Callers display missing
    `last_seen` as em-dash so a registered-but-never-pushed device doesn't
    look like it pushed at registration time.
    """
    key = device_key(device_id)
    data = {
        "device_id": device_id,
        "device_name": device_name,
        "registered": datetime.now(timezone.utc).isoformat(),
    }
    try:
        backend.put_exclusive(key, json.dumps(data, indent=2).encode("utf-8"))
    except StorageError:
        # Entry exists. Preserve the existing `registered:` timestamp.
        return


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

    Concurrency: read-modify-write is wrapped in `_devices_write_lock()`
    so concurrent autopush + interactive push can't lose interleaved
    field updates (Group 7 preflight D10).
    """
    key = device_key(device_id)
    with _devices_write_lock():
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
