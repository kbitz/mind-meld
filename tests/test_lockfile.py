"""Tests for mind_meld.lockfile — PID-based locking."""

import os

import pytest

from mind_meld.errors import LockError
from mind_meld.lockfile import acquire_lock, release_lock


class TestLockfile:
    @pytest.fixture
    def lock_path(self, tmp_path):
        return tmp_path / "mind-meld.lock"

    def test_acquire_and_release(self, lock_path):
        acquire_lock(lock_path)
        assert lock_path.exists()
        assert lock_path.read_text().strip() == str(os.getpid())
        release_lock(lock_path)
        assert not lock_path.exists()

    def test_acquire_twice_raises(self, lock_path):
        acquire_lock(lock_path)
        # Same PID, same process — should raise since PID is running
        with pytest.raises(LockError, match="Another mm operation"):
            acquire_lock(lock_path)
        release_lock(lock_path)

    def test_stale_lock_cleaned_up(self, lock_path):
        # Write a PID that doesn't exist
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("999999999")  # Very unlikely to be a real PID
        # Should succeed because the PID is not running
        acquire_lock(lock_path)
        assert lock_path.read_text().strip() == str(os.getpid())
        release_lock(lock_path)

    def test_corrupt_lock_overwritten(self, lock_path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("not-a-pid")
        # Should succeed — corrupt lock is overwritten
        acquire_lock(lock_path)
        release_lock(lock_path)

    def test_release_only_own_lock(self, lock_path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("999999999")
        # Release should NOT delete if PID doesn't match
        release_lock(lock_path)
        assert lock_path.exists()  # Still there — not our lock

    def test_release_nonexistent_is_noop(self, lock_path):
        release_lock(lock_path)  # Should not raise
