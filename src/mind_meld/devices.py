"""Device registration and listing for Mind Meld."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from mind_meld.errors import StorageError
from mind_meld.storage.local import LocalBackend


def register_device(
    backend: LocalBackend,
    device_id: str,
    device_name: str,
) -> None:
    """Write device metadata to storage."""
    data = {
        "device_id": device_id,
        "device_name": device_name,
        "registered": datetime.now(timezone.utc).isoformat(),
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }
    key = f"devices/{device_id}.json"
    backend.put(key, json.dumps(data, indent=2).encode("utf-8"))


def update_last_seen(
    backend: LocalBackend,
    device_id: str,
) -> None:
    """Update the last_seen timestamp for a device."""
    key = f"devices/{device_id}.json"
    try:
        data = json.loads(backend.get(key))
    except StorageError:
        return  # Device not registered yet — skip silently
    data["last_seen"] = datetime.now(timezone.utc).isoformat()
    backend.put(key, json.dumps(data, indent=2).encode("utf-8"))


def list_devices(backend: LocalBackend) -> list[dict[str, Any]]:
    """List all registered devices from storage."""
    keys = backend.list_keys("devices/")
    devices = []
    for key in keys:
        if not key.endswith(".json"):
            continue
        try:
            data = json.loads(backend.get(key))
            devices.append(data)
        except (StorageError, json.JSONDecodeError):
            continue  # Skip corrupt device files
    return devices
