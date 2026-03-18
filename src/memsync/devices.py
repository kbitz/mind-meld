"""Device registration and listing for MemSync."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from memsync.errors import StorageError
from memsync.storage.base import StorageBackend


def register_device(
    backend: StorageBackend,
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
    backend: StorageBackend,
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


def list_devices(backend: StorageBackend) -> list[dict[str, Any]]:
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


def get_device(backend: StorageBackend, device_id: str) -> dict[str, Any] | None:
    """Get a specific device's metadata."""
    key = f"devices/{device_id}.json"
    try:
        return json.loads(backend.get(key))
    except StorageError:
        return None
