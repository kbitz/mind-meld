"""Pins for devices.py contract changes (Group 7 preflight items 4 + 5).

Item 4: register_device is create-only — no-op if entry exists. Preserves
the original `registered:` first-registration timestamp under self-heal /
re-init / iCloud-placeholder TOCTOU. Backed by LocalBackend.put_exclusive.

Item 5: update_last_seen wraps its read-modify-write in
_devices_write_lock() so concurrent autopush + interactive push can't
lose interleaved field updates.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from mind_meld.devices import register_device, update_last_seen
from mind_meld.errors import StorageError
from mind_meld.storage.keys import device_key
from mind_meld.storage.local import LocalBackend


@pytest.fixture
def backend(tmp_path) -> LocalBackend:
    return LocalBackend(tmp_path / "storage")


class TestRegisterDeviceCreateOnly:
    def test_first_call_writes_entry(self, backend: LocalBackend) -> None:
        register_device(backend, "dev-a", "Mac A")
        raw = backend.get(device_key("dev-a"))
        data = json.loads(raw)
        assert data["device_id"] == "dev-a"
        assert data["device_name"] == "Mac A"
        assert "registered" in data

    def test_second_call_preserves_registered_timestamp(self, backend: LocalBackend) -> None:
        """The core regression for D8: re-registering must NOT bump `registered:`.

        Pre-D8, register_device was an unconditional overwrite — self-heal
        on the iCloud placeholder TOCTOU path silently bumped the field's
        semantic meaning from "first registration" to "last self-heal."
        """
        register_device(backend, "dev-a", "Mac A")
        original = json.loads(backend.get(device_key("dev-a")))
        original_registered = original["registered"]

        # Sleep so a literal datetime.now().isoformat() would differ.
        time.sleep(0.01)
        register_device(backend, "dev-a", "Mac A")
        after = json.loads(backend.get(device_key("dev-a")))
        assert after["registered"] == original_registered

    def test_second_call_with_different_name_is_noop(self, backend: LocalBackend) -> None:
        """Create-only means the entry is preserved EXACTLY — even if the
        caller passed a different device_name. No partial overwrite.
        """
        register_device(backend, "dev-a", "Mac A")
        original = json.loads(backend.get(device_key("dev-a")))

        register_device(backend, "dev-a", "Mac A renamed")
        after = json.loads(backend.get(device_key("dev-a")))
        assert after["device_name"] == "Mac A"
        assert after == original

    def test_does_not_seed_last_seen(self, backend: LocalBackend) -> None:
        """Documented contract from the original docstring."""
        register_device(backend, "dev-a", "Mac A")
        data = json.loads(backend.get(device_key("dev-a")))
        assert "last_seen" not in data
        assert "last_seen_version" not in data


class TestRegisterDevicePreservesAcrossUpdate:
    def test_register_after_update_preserves_last_seen(self, backend: LocalBackend) -> None:
        """Self-heal must not erase a peer's recently-recorded last_seen.

        Codex outside-voice finding #6: pre-D8, a concurrent register_device
        called by `_ensure_device_registered` would clobber `last_seen` set
        by an in-progress update_last_seen on another thread/process. The
        create-only contract closes this even without the flock.
        """
        register_device(backend, "dev-a", "Mac A")
        update_last_seen(backend, "dev-a")
        before = json.loads(backend.get(device_key("dev-a")))
        assert "last_seen" in before
        assert "last_seen_version" in before

        register_device(backend, "dev-a", "Mac A")
        after = json.loads(backend.get(device_key("dev-a")))
        assert after["last_seen"] == before["last_seen"]
        assert after["last_seen_version"] == before["last_seen_version"]
        assert after["registered"] == before["registered"]


class TestUpdateLastSeenFlock:
    def test_update_writes_last_seen_and_version(self, backend: LocalBackend) -> None:
        register_device(backend, "dev-a", "Mac A")
        update_last_seen(backend, "dev-a")
        data = json.loads(backend.get(device_key("dev-a")))
        assert "last_seen" in data
        # Parses as ISO-8601 UTC.
        datetime.fromisoformat(data["last_seen"])
        assert "last_seen_version" in data

    def test_update_silent_when_device_not_registered(self, backend: LocalBackend) -> None:
        update_last_seen(backend, "dev-a")
        with pytest.raises(StorageError):
            backend.get(device_key("dev-a"))

    def test_concurrent_updates_produce_valid_json(self, backend: LocalBackend) -> None:
        """50 concurrent update_last_seen calls — no torn writes, no field loss.

        The flock serializes RMW so each writer reads a complete entry,
        modifies, and writes a complete entry. Without the flock, an
        interleaved read could observe a partial state (e.g., during a
        future field-add transition) and write back missing keys.
        """
        register_device(backend, "dev-a", "Mac A")

        def worker() -> None:
            update_last_seen(backend, "dev-a")

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Final state is well-formed JSON with all expected fields.
        data = json.loads(backend.get(device_key("dev-a")))
        assert data["device_id"] == "dev-a"
        assert data["device_name"] == "Mac A"
        assert "registered" in data
        assert "last_seen" in data
        assert "last_seen_version" in data
        # last_seen parses as ISO-8601.
        ts = datetime.fromisoformat(data["last_seen"])
        assert ts.tzinfo == timezone.utc

    def test_concurrent_register_and_update(self, backend: LocalBackend) -> None:
        """register_device interleaved with update_last_seen — no field loss.

        Pre-D8, register_device's overwrite would clobber update's last_seen
        write. With create-only, register no-ops on existing entries; final
        state retains last_seen.
        """
        register_device(backend, "dev-a", "Mac A")

        def register_worker() -> None:
            for _ in range(20):
                register_device(backend, "dev-a", "Mac A")

        def update_worker() -> None:
            for _ in range(20):
                update_last_seen(backend, "dev-a")

        threads = [
            threading.Thread(target=register_worker),
            threading.Thread(target=update_worker),
            threading.Thread(target=register_worker),
            threading.Thread(target=update_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = json.loads(backend.get(device_key("dev-a")))
        assert "last_seen" in data, "register_device must not clobber update_last_seen's write"
        assert "last_seen_version" in data
