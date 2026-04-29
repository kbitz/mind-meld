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


class TestLookupDeviceByShortId:
    """Pins for the conflict-prompt-ux device-name lookup helper.

    Single source of truth for attribution rendering on the REMOTE banner
    line of `mm pull` / `mm resolve`. Pure function over the existing
    devices list -- callers `list_devices(backend)` once at the start of
    an interactive walk and pass the same list in per-conflict.
    """

    def test_zero_matches_returns_none_and_zero(self) -> None:
        from mind_meld.devices import lookup_device_by_short_id

        devices = [
            {"device_id": "aaaa1111", "device_name": "Mac A"},
            {"device_id": "bbbb2222", "device_name": "Mac B"},
        ]
        assert lookup_device_by_short_id(devices, "deadbeef") == (None, 0)

    def test_one_match_returns_dict_and_count_one(self) -> None:
        from mind_meld.devices import lookup_device_by_short_id

        a = {"device_id": "aaaa1111", "device_name": "Mac A"}
        b = {"device_id": "bbbb2222", "device_name": "Mac B"}
        result, count = lookup_device_by_short_id([a, b], "aaaa1111")
        assert result is a
        assert count == 1

    def test_multiple_matches_returns_none_and_count(self, monkeypatch, capsys) -> None:
        # Reset the one-shot notice cache to make the test deterministic.
        from mind_meld import devices as _devices

        monkeypatch.setattr(_devices, "_AMBIGUOUS_PREFIX_NOTICED", set())
        from mind_meld.devices import lookup_device_by_short_id

        devs = [
            {"device_id": "aaaa1111", "device_name": "Mac A"},
            {"device_id": "aaaa2222", "device_name": "Mac B"},
            {"device_id": "bbbb3333", "device_name": "Mac C"},
        ]
        result, count = lookup_device_by_short_id(devs, "aaaa")
        assert result is None
        assert count == 2
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err
        assert "matches 2 peers" in captured.err
        assert "aaaa1111" in captured.err
        assert "aaaa2222" in captured.err

    def test_multiple_matches_notice_is_one_shot_per_prefix(self, monkeypatch, capsys) -> None:
        from mind_meld import devices as _devices

        monkeypatch.setattr(_devices, "_AMBIGUOUS_PREFIX_NOTICED", set())
        from mind_meld.devices import lookup_device_by_short_id

        devs = [
            {"device_id": "aaaa1111", "device_name": "Mac A"},
            {"device_id": "aaaa2222", "device_name": "Mac B"},
        ]
        # First call: notice fires.
        lookup_device_by_short_id(devs, "aaaa")
        first = capsys.readouterr().err
        assert "mm: notice:" in first
        # Second call with the same prefix: no notice.
        lookup_device_by_short_id(devs, "aaaa")
        second = capsys.readouterr().err
        assert second == ""

    def test_multiple_matches_distinct_prefixes_each_notice_once(self, monkeypatch, capsys) -> None:
        from mind_meld import devices as _devices

        monkeypatch.setattr(_devices, "_AMBIGUOUS_PREFIX_NOTICED", set())
        from mind_meld.devices import lookup_device_by_short_id

        devs = [
            {"device_id": "aaaa1111", "device_name": "Mac A"},
            {"device_id": "aaaa2222", "device_name": "Mac B"},
            {"device_id": "bbbb3333", "device_name": "Mac C"},
            {"device_id": "bbbb4444", "device_name": "Mac D"},
        ]
        lookup_device_by_short_id(devs, "aaaa")
        lookup_device_by_short_id(devs, "bbbb")
        err = capsys.readouterr().err
        # Two distinct prefix notices; not deduped against each other.
        assert err.count("mm: notice:") == 2

    def test_short_id_shorter_than_8_chars_uses_prefix(self) -> None:
        # Forward-defense: caller may pass a sub-8-char prefix if the
        # filename convention ever changes.
        from mind_meld.devices import lookup_device_by_short_id

        devs = [
            {"device_id": "aaaa1111", "device_name": "Mac A"},
            {"device_id": "bbbb2222", "device_name": "Mac B"},
        ]
        result, count = lookup_device_by_short_id(devs, "aa")
        assert result is devs[0]
        assert count == 1


class TestGenerateUniqueShortDeviceId:
    """Pins for init-time device-id collision prevention.

    UUID4 prefix collisions on 32 bits are extremely unlikely under healthy
    RNG (~1 in 4 billion per draw) but a cloned-from-snapshot peer or a
    deterministic-RNG bug could collide reproducibly. The runtime
    `lookup_device_by_short_id` helper still defends in depth via its
    multi-match path.
    """

    def test_returns_id_when_no_existing_devices(self) -> None:
        from mind_meld.devices import generate_unique_short_device_id

        out = generate_unique_short_device_id([])
        assert isinstance(out, str)
        assert len(out) == 8

    def test_returns_id_when_no_collision(self) -> None:
        from mind_meld.devices import generate_unique_short_device_id

        existing = [{"device_id": "deadbeef", "device_name": "Mac A"}]
        out = generate_unique_short_device_id(existing)
        assert out != "deadbeef"

    def test_retries_on_collision_then_succeeds(self, monkeypatch) -> None:
        # Force the first uuid4 draw to collide; the second draw is unique.
        from mind_meld import devices as _devices

        draws = iter(["aaaa1111", "bbbb2222", "cccc3333"])

        class _FakeUUID:
            def __init__(self, hex_value: str) -> None:
                self.hex = hex_value + "0" * (32 - len(hex_value))

        def _fake_uuid4() -> _FakeUUID:
            return _FakeUUID(next(draws))

        monkeypatch.setattr(_devices.uuid, "uuid4", _fake_uuid4)

        existing = [{"device_id": "aaaa1111", "device_name": "Mac A"}]
        out = _devices.generate_unique_short_device_id(existing)
        assert out == "bbbb2222"

    def test_warns_when_retry_budget_exhausted(self, monkeypatch, capsys) -> None:
        from mind_meld import devices as _devices

        # Every draw collides with an existing device.
        class _FakeUUID:
            hex = "aaaa1111" + "0" * 24

        monkeypatch.setattr(_devices.uuid, "uuid4", lambda: _FakeUUID())

        existing = [{"device_id": "aaaa1111", "device_name": "Mac A"}]
        out = _devices.generate_unique_short_device_id(existing, max_retries=3)
        assert out == "aaaa1111"
        captured = capsys.readouterr()
        assert "mm: warning:" in captured.err
        assert "could not generate" in captured.err
        assert "3 attempts" in captured.err
