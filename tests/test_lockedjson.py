"""Tests for mind_meld.lockedjson — the locked JSON R/M/W context manager.

Pinning behaviors that the upgrade.py retrofit + the new token_usage.py
both depend on. See src/mind_meld/lockedjson.py for the contract.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

import pytest

from mind_meld.lockedjson import LockContended, LockedJson, locked_json_rmw


class TestLockedJsonHappyPath:
    def test_empty_file_yields_default_factory(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        with locked_json_rmw(path) as ljson:
            assert ljson.is_locked is True
            assert ljson.data == {}

    def test_custom_default_factory(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        with locked_json_rmw(path, default_factory=lambda: {"version": 1}) as ljson:
            assert ljson.data == {"version": 1}

    def test_mutation_persisted_on_exit(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        with locked_json_rmw(path) as ljson:
            ljson.data["foo"] = "bar"
            ljson.data["count"] = 42
        # Read back via second context.
        with locked_json_rmw(path) as ljson:
            assert ljson.data == {"foo": "bar", "count": 42}

    def test_reading_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text('{"existing": "value"}')
        with locked_json_rmw(path) as ljson:
            assert ljson.data == {"existing": "value"}

    def test_no_mutation_still_writes_normalized_json(self, tmp_path: Path) -> None:
        """A no-mutation context normalizes the file. Track 10A briefly
        added a skip-write optimization but reverted it after measuring
        net-negative perf (sha256(json.dumps(...)) twice cost more than
        a single os.write since _write_json doesn't fsync)."""
        path = tmp_path / "cache.json"
        path.write_text('{"a":1}')  # not pretty-printed
        with locked_json_rmw(path):
            pass
        # After context, file has been rewritten in pretty-printed form.
        assert json.loads(path.read_text()) == {"a": 1}

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "cache.json"
        with locked_json_rmw(path) as ljson:
            ljson.data["created"] = True
        assert path.exists()
        assert json.loads(path.read_text()) == {"created": True}

    def test_default_mode_is_0o600(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        with locked_json_rmw(path) as ljson:
            ljson.data["x"] = 1
        # Mask off filetype bits; check rwx for owner/group/other.
        actual_mode = path.stat().st_mode & 0o777
        assert actual_mode == 0o600


class TestLockedJsonCorruption:
    def test_corrupt_json_recovers_to_default(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text("not valid {{{ json")
        with locked_json_rmw(path) as ljson:
            assert ljson.is_locked is True
            assert ljson.data == {}  # silently recovered

    def test_non_dict_top_level_recovers_to_default(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text("[1, 2, 3]")
        with locked_json_rmw(path) as ljson:
            assert ljson.data == {}

    def test_corrupt_json_overwritten_on_exit(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text("garbage")
        with locked_json_rmw(path) as ljson:
            ljson.data["recovered"] = True
        # Corrupt content has been replaced with valid JSON.
        assert json.loads(path.read_text()) == {"recovered": True}

    def test_invalid_utf8_recovers_to_default(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_bytes(b"\xff\xfe invalid utf8")
        with locked_json_rmw(path) as ljson:
            assert ljson.data == {}


class TestLockedJsonExceptionPath:
    def test_caller_exception_does_not_persist_mutations(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text('{"original": true}')
        with pytest.raises(RuntimeError, match="boom"):
            with locked_json_rmw(path) as ljson:
                ljson.data["mutation"] = "should not persist"
                raise RuntimeError("boom")
        # Original content preserved (lock was released without write).
        assert json.loads(path.read_text()) == {"original": True}


class TestLockedJsonContentionRaise:
    def test_contended_raises_with_short_retry_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text("{}")
        # Hold the lock from another file descriptor.
        blocker_fd = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(blocker_fd, fcntl.LOCK_EX)
            with pytest.raises(LockContended):
                with locked_json_rmw(
                    path,
                    on_contention="raise",
                    retry_intervals=(0.01, 0.01),
                ):
                    pass
        finally:
            fcntl.flock(blocker_fd, fcntl.LOCK_UN)
            os.close(blocker_fd)


class TestLockedJsonContentionWarn:
    def test_contended_yields_unlocked_with_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "cache.json"
        path.write_text('{"existing": true}')
        blocker_fd = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(blocker_fd, fcntl.LOCK_EX)
            with locked_json_rmw(
                path,
                on_contention="warn",
                retry_intervals=(0.01, 0.01),
                contention_warning="token cache contended",
            ) as ljson:
                assert ljson.is_locked is False
                # data is the placeholder default (empty), NOT the real file
                # contents — caller is meant to check is_locked and skip.
                assert ljson.data == {}
                ljson.data["mutation"] = "should not persist"
        finally:
            fcntl.flock(blocker_fd, fcntl.LOCK_UN)
            os.close(blocker_fd)
        captured = capsys.readouterr()
        assert "mm: warning: token cache contended" in captured.err
        # File on disk is untouched.
        assert json.loads(path.read_text()) == {"existing": True}

    def test_uncontended_warn_mode_works_normally(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        with locked_json_rmw(path, on_contention="warn") as ljson:
            assert ljson.is_locked is True
            ljson.data["worked"] = True
        assert json.loads(path.read_text()) == {"worked": True}


class TestLockedJsonContentionBlock:
    def test_two_threads_serialize_via_block(self, tmp_path: Path) -> None:
        """Default 'block' mode queues concurrent waiters. Pin that
        existing upgrade.py-equivalent behavior survives the extraction."""
        path = tmp_path / "cache.json"
        path.write_text('{"counter": 0}')
        results: list[int] = []

        def worker() -> None:
            with locked_json_rmw(path) as ljson:
                cur = ljson.data.get("counter", 0)
                # Hold lock briefly so the other thread queues.
                time.sleep(0.05)
                ljson.data["counter"] = cur + 1
                results.append(ljson.data["counter"])

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # Both increments saw a serialized view; no lost update.
        assert sorted(results) == [1, 2]
        assert json.loads(path.read_text()) == {"counter": 2}


class TestLockedJsonReturnType:
    def test_yielded_is_LockedJson_dataclass(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        with locked_json_rmw(path) as ljson:
            assert isinstance(ljson, LockedJson)
            assert hasattr(ljson, "data")
            assert hasattr(ljson, "is_locked")
