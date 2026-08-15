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

from mind_meld import lockedjson
from mind_meld.lockedjson import (
    LockContended,
    LockedJson,
    locked_json_rmw,
    locked_json_snapshot,
)


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


class TestLockedJsonSnapshot:
    def test_missing_snapshot_does_not_create_parent_or_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "cache.json"

        with locked_json_snapshot(path) as snapshot:
            assert snapshot.state == "missing"
            assert snapshot.data is None

        assert not path.parent.exists()
        assert not path.exists()

    def test_snapshot_preserves_existing_cache_bytes_and_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        original = b'{"files":{},"version":1,"unknown":"preserve me"}'
        path.write_bytes(original)
        path.chmod(0o640)
        before = path.stat()

        with locked_json_snapshot(path) as snapshot:
            assert snapshot.state == "valid"
            assert snapshot.data == {"files": {}, "version": 1, "unknown": "preserve me"}

        after = path.stat()
        assert path.read_bytes() == original
        assert after.st_mode & 0o777 == before.st_mode & 0o777
        assert after.st_mtime_ns == before.st_mtime_ns

    @pytest.mark.parametrize(
        ("contents", "expected_state"),
        [(b"not json", "malformed"), (b"[]", "non_dict"), (b"", "empty")],
    )
    def test_snapshot_reports_unrepairable_state_without_writing(
        self, tmp_path: Path, contents: bytes, expected_state: str
    ) -> None:
        path = tmp_path / "cache.json"
        path.write_bytes(contents)

        with locked_json_snapshot(path) as snapshot:
            assert snapshot.state == expected_state
            assert snapshot.data is None

        assert path.read_bytes() == contents


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

    def test_write_failure_is_reported_to_caller(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "cache.json"
        failure = OSError("disk full")
        monkeypatch.setattr(lockedjson, "_write_json", lambda _fd, _data: failure)

        with locked_json_rmw(path) as ljson:
            ljson.data["attempted"] = True

        assert ljson.write_attempted is True
        assert ljson.write_error is failure


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
