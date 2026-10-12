"""Tests for mind_meld.fsutil — atomic write + directory fsync primitives."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from mind_meld import fsutil
from mind_meld.errors import StorageError


class TestAtomicWriteBytes:
    def test_round_trip_no_fsync(self, tmp_path):
        target = tmp_path / "file.bin"
        fsutil.atomic_write_bytes(target, b"hello")
        assert target.read_bytes() == b"hello"

    def test_round_trip_with_fsync(self, tmp_path):
        target = tmp_path / "file.bin"
        fsutil.atomic_write_bytes(target, b"durable", fsync=True)
        assert target.read_bytes() == b"durable"

    def test_overwrite_existing(self, tmp_path):
        target = tmp_path / "file.bin"
        target.write_bytes(b"original")
        fsutil.atomic_write_bytes(target, b"replaced")
        assert target.read_bytes() == b"replaced"

    def test_empty_bytes(self, tmp_path):
        target = tmp_path / "empty.bin"
        fsutil.atomic_write_bytes(target, b"")
        assert target.read_bytes() == b""

    def test_1mb_round_trip(self, tmp_path):
        data = b"x" * 1_000_000
        target = tmp_path / "big.bin"
        fsutil.atomic_write_bytes(target, data)
        assert target.read_bytes() == data

    def test_new_file_uses_umask_default_mode(self, tmp_path):
        """New file with mode=None uses (0o666 & ~umask), matching
        Path.write_bytes() — prevents silent 0600 downgrade regression."""
        target = tmp_path / "new.txt"
        fsutil.atomic_write_bytes(target, b"x")
        mode = stat.S_IMODE(target.stat().st_mode)
        expected = fsutil._default_new_file_mode()
        assert mode == expected, (
            f"new file should honor umask (expected {oct(expected)}, got {oct(mode)})"
        )

    def test_overwrite_preserves_existing_mode(self, tmp_path):
        """Overwrite with mode=None preserves the target's existing mode.
        CRITICAL: without this, mm-written user files would silently
        downgrade to 0o600 on every pull-apply."""
        target = tmp_path / "existing.md"
        target.write_bytes(b"original")
        os.chmod(target, 0o644)
        fsutil.atomic_write_bytes(target, b"updated")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o644, f"existing file mode must be preserved, got {oct(mode)}"

    def test_explicit_mode_overrides_default(self, tmp_path):
        """Explicit mode=0o600 (for secret-class writes) is respected."""
        target = tmp_path / "secret.bin"
        fsutil.atomic_write_bytes(target, b"private", mode=0o600)
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600

    def test_explicit_mode_overrides_existing(self, tmp_path):
        """Explicit mode beats existing-mode preservation."""
        target = tmp_path / "existing.bin"
        target.write_bytes(b"old")
        os.chmod(target, 0o644)
        fsutil.atomic_write_bytes(target, b"new", mode=0o600)
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600

    def test_mkstemp_failure_raises_storage_error(self, tmp_path, monkeypatch):
        def fail_mkstemp(*args, **kwargs):
            raise OSError(errno.ENOSPC, "no space")

        monkeypatch.setattr(fsutil.tempfile, "mkstemp", fail_mkstemp)
        target = tmp_path / "file.bin"
        with pytest.raises(StorageError, match="atomic_write_bytes"):
            fsutil.atomic_write_bytes(target, b"data")
        # No stale tmp files left behind.
        assert list(tmp_path.glob("tmp*")) == []
        assert not target.exists()

    def test_write_failure_unlinks_tmp(self, tmp_path, monkeypatch):
        """If write() raises mid-write, tmp must be unlinked and target untouched."""
        target = tmp_path / "file.bin"
        target.write_bytes(b"original")  # pre-existing

        real_fdopen = os.fdopen

        def bad_fdopen(fd, mode, *args, **kwargs):
            f = real_fdopen(fd, mode, *args, **kwargs)

            def failing_write(data):
                raise OSError(errno.EIO, "simulated disk error")

            f.write = failing_write  # type: ignore[method-assign]
            return f

        monkeypatch.setattr(fsutil.os, "fdopen", bad_fdopen)
        with pytest.raises(StorageError, match="atomic_write_bytes"):
            fsutil.atomic_write_bytes(target, b"new")
        # Target is unchanged.
        assert target.read_bytes() == b"original"
        # No stale tmp.
        assert list(tmp_path.glob("tmp*")) == []

    def test_replace_failure_unlinks_tmp(self, tmp_path, monkeypatch):
        def bad_replace(src, dst):
            raise OSError(errno.EXDEV, "cross-device link")

        monkeypatch.setattr(fsutil.os, "replace", bad_replace)
        target = tmp_path / "file.bin"
        with pytest.raises(StorageError, match="atomic_write_bytes"):
            fsutil.atomic_write_bytes(target, b"data")
        assert list(tmp_path.glob("tmp*")) == []
        assert not target.exists()

    def test_fd_fsync_failure_unlinks_tmp(self, tmp_path, monkeypatch):
        """fsync failure before replace must unlink tmp and leave target untouched."""
        target = tmp_path / "file.bin"
        target.write_bytes(b"original")

        def bad_fsync(fd):
            raise OSError(errno.EIO, "fsync failed")

        monkeypatch.setattr(fsutil, "_fsync_fd", bad_fsync)
        with pytest.raises(StorageError, match="atomic_write_bytes"):
            fsutil.atomic_write_bytes(target, b"new", fsync=True)
        assert target.read_bytes() == b"original"
        assert list(tmp_path.glob("tmp*")) == []

    def test_parent_fsync_failure_is_fatal(self, tmp_path, monkeypatch):
        """Parent dir fsync failure raises AFTER rename; file exists but error propagates."""
        target = tmp_path / "file.bin"
        call_count = {"n": 0}

        real_fsync = fsutil._fsync_fd

        def selective_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: fd fsync on the file — allow it.
                real_fsync(fd)
            else:
                # Second call: parent dir fsync — fail.
                raise OSError(errno.EIO, "parent fsync failed")

        monkeypatch.setattr(fsutil, "_fsync_fd", selective_fsync)
        with pytest.raises(StorageError, match="fsync_dir"):
            fsutil.atomic_write_bytes(target, b"data", fsync=True)
        # File is written (rename succeeded) but durability not guaranteed.
        assert target.read_bytes() == b"data"

    def test_no_fsync_does_not_call_fsync_fd(self, tmp_path, monkeypatch):
        """fsync=False must not invoke _fsync_fd at all (perf + correctness)."""
        calls: list[int] = []

        def spy_fsync(fd):
            calls.append(fd)

        monkeypatch.setattr(fsutil, "_fsync_fd", spy_fsync)
        fsutil.atomic_write_bytes(tmp_path / "f.bin", b"data", fsync=False)
        assert calls == []

    def test_fsync_true_calls_fsync_fd_twice(self, tmp_path, monkeypatch):
        """fsync=True calls _fsync_fd once for file, once for parent dir."""
        calls: list[int] = []
        real_fsync = fsutil._fsync_fd

        def spy_fsync(fd):
            calls.append(fd)
            real_fsync(fd)

        monkeypatch.setattr(fsutil, "_fsync_fd", spy_fsync)
        fsutil.atomic_write_bytes(tmp_path / "f.bin", b"data", fsync=True)
        assert len(calls) == 2


class TestFsyncFd:
    def test_darwin_uses_fullfsync(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fsutil, "_IS_DARWIN", True)
        fcntl_calls: list[tuple[int, int]] = []
        fsync_calls: list[int] = []

        def spy_fcntl(fd, op, *args):
            fcntl_calls.append((fd, op))
            return 0

        def spy_os_fsync(fd):
            fsync_calls.append(fd)

        monkeypatch.setattr(fsutil.fcntl, "fcntl", spy_fcntl)
        monkeypatch.setattr(fsutil.os, "fsync", spy_os_fsync)

        target = tmp_path / "f.bin"
        fsutil.atomic_write_bytes(target, b"data", fsync=True)

        # F_FULLFSYNC used for both file and parent dir, os.fsync never called.
        assert len(fcntl_calls) == 2
        assert all(op == fsutil.fcntl.F_FULLFSYNC for _fd, op in fcntl_calls)
        assert fsync_calls == []

    @pytest.mark.parametrize("code", sorted({errno.ENOTSUP, errno.EINVAL, errno.EOPNOTSUPP}))
    def test_darwin_fullfsync_unsupported_falls_back_to_fsync(self, tmp_path, monkeypatch, code):
        """On Darwin, each documented "unsupported" F_FULLFSYNC errno falls back to os.fsync."""
        monkeypatch.setattr(fsutil, "_IS_DARWIN", True)
        fsync_calls: list[int] = []

        def unsupported_fcntl(fd, op, *args):
            raise OSError(code, "not supported")

        def spy_os_fsync(fd):
            fsync_calls.append(fd)

        monkeypatch.setattr(fsutil.fcntl, "fcntl", unsupported_fcntl)
        monkeypatch.setattr(fsutil.os, "fsync", spy_os_fsync)

        target = tmp_path / "f.bin"
        fsutil.atomic_write_bytes(target, b"data", fsync=True)
        assert len(fsync_calls) == 2  # file + parent dir

    def test_darwin_fullfsync_real_io_error_propagates(self, tmp_path, monkeypatch):
        """On Darwin, a non-ENOTSUP OSError from F_FULLFSYNC must NOT fall back."""
        monkeypatch.setattr(fsutil, "_IS_DARWIN", True)

        def real_io_error(fd, op, *args):
            raise OSError(errno.EIO, "real I/O error")

        monkeypatch.setattr(fsutil.fcntl, "fcntl", real_io_error)
        target = tmp_path / "f.bin"
        with pytest.raises(StorageError):
            fsutil.atomic_write_bytes(target, b"data", fsync=True)

    def test_non_darwin_uses_plain_fsync(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fsutil, "_IS_DARWIN", False)
        fcntl_calls: list[tuple[int, int]] = []
        fsync_calls: list[int] = []

        def spy_fcntl(fd, op, *args):
            fcntl_calls.append((fd, op))

        def spy_os_fsync(fd):
            fsync_calls.append(fd)

        monkeypatch.setattr(fsutil.fcntl, "fcntl", spy_fcntl)
        monkeypatch.setattr(fsutil.os, "fsync", spy_os_fsync)

        target = tmp_path / "f.bin"
        fsutil.atomic_write_bytes(target, b"data", fsync=True)
        # fcntl.fcntl NEVER called on non-Darwin.
        assert fcntl_calls == []
        # os.fsync called twice (file + parent).
        assert len(fsync_calls) == 2


class TestFsyncDir:
    def test_happy(self, tmp_path):
        # No crash means success; no return value to check.
        fsutil.fsync_dir(tmp_path)

    def test_nonexistent_path_raises(self, tmp_path):
        with pytest.raises(StorageError, match="fsync_dir open"):
            fsutil.fsync_dir(tmp_path / "does-not-exist")

    def test_fsync_failure_raises(self, tmp_path, monkeypatch):
        def bad_fsync(fd):
            raise OSError(errno.EIO, "fsync failed")

        monkeypatch.setattr(fsutil, "_fsync_fd", bad_fsync)
        with pytest.raises(StorageError, match="fsync_dir fsync"):
            fsutil.fsync_dir(tmp_path)

    def test_closes_fd_on_fsync_failure(self, tmp_path, monkeypatch):
        """On fsync failure, the opened fd must still be closed (no leak)."""
        closed: list[int] = []
        real_close = os.close

        def spy_close(fd):
            closed.append(fd)
            real_close(fd)

        def bad_fsync(fd):
            raise OSError(errno.EIO, "fsync failed")

        monkeypatch.setattr(fsutil, "_fsync_fd", bad_fsync)
        monkeypatch.setattr(fsutil.os, "close", spy_close)

        with pytest.raises(StorageError):
            fsutil.fsync_dir(tmp_path)
        # Exactly one close call for the dir fd we opened.
        assert len(closed) == 1


class TestFlockAppendJsonl:
    """Helper extracted in C1; pullhistory and events both use it."""

    def test_appends_single_line_with_trailing_newline(self, tmp_path):
        path = tmp_path / "log.jsonl"
        fsutil.flock_append_jsonl(path, [b'{"a":1}'])
        assert path.read_bytes() == b'{"a":1}\n'

    def test_appends_multiple_lines_in_one_window(self, tmp_path):
        path = tmp_path / "log.jsonl"
        fsutil.flock_append_jsonl(path, [b'{"a":1}', b'{"a":2}', b'{"a":3}'])
        assert path.read_text().splitlines() == ['{"a":1}', '{"a":2}', '{"a":3}']

    def test_empty_lines_no_op(self, synthetic_state_root, monkeypatch, forbid_fs_mutations):
        path = synthetic_state_root / "missing" / "log.jsonl"
        monkeypatch.setattr(fsutil.pwd, "getpwuid", lambda uid: pytest.fail("unneeded lookup"))
        calls = forbid_fs_mutations()
        fsutil.flock_append_jsonl(path, [])
        assert not path.exists(), "empty input must not create the file"
        assert not path.parent.exists()
        assert calls == []

    def test_creates_parent_dir(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "log.jsonl"
        fsutil.flock_append_jsonl(path, [b'{"a":1}'])
        assert path.exists()

    def test_mode_0600_default(self, tmp_path):
        path = tmp_path / "log.jsonl"
        fsutil.flock_append_jsonl(path, [b'{"a":1}'])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_mode_override(self, tmp_path):
        path = tmp_path / "log.jsonl"
        fsutil.flock_append_jsonl(path, [b'{"a":1}'], mode=0o644)
        # mode applies to both creation AND any existing file (fchmod runs every call)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

    def test_on_locked_callback_runs_under_flock(self, tmp_path):
        path = tmp_path / "log.jsonl"
        captured: list[int] = []

        def cb(fd):
            captured.append(fd)
            # If the callback truly runs under flock, the fd is open and writable
            assert os.fstat(fd).st_size > 0  # data already written

        fsutil.flock_append_jsonl(path, [b'{"a":1}'], on_locked=cb)
        assert len(captured) == 1

    def test_on_locked_exception_swallowed(self, tmp_path):
        """Forensic-only contract: callback errors don't break callers."""
        path = tmp_path / "log.jsonl"

        def boom(fd):
            raise RuntimeError("nope")

        # MUST NOT raise
        fsutil.flock_append_jsonl(path, [b'{"a":1}'], on_locked=boom)
        assert path.read_bytes() == b'{"a":1}\n'

    def test_oserror_swallowed(self, tmp_path, monkeypatch):
        """Forensic-only contract: OSError on the underlying open / write
        path is swallowed so a wedged FS never breaks the calling sync."""
        path = tmp_path / "log.jsonl"

        def bad_open(*args, **kwargs):
            raise OSError(errno.EIO, "io error")

        monkeypatch.setattr(fsutil.os, "open", bad_open)
        # MUST NOT raise
        fsutil.flock_append_jsonl(path, [b'{"a":1}'])

    def test_append_preserves_existing_content(self, tmp_path):
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":0}\n')
        fsutil.flock_append_jsonl(path, [b'{"a":1}', b'{"a":2}'])
        assert path.read_text().splitlines() == ['{"a":0}', '{"a":1}', '{"a":2}']

    def test_size_limit_does_not_create_missing_file(self, tmp_path):
        path = tmp_path / "log.jsonl"
        with pytest.raises(fsutil.AppendSizeLimit):
            fsutil.flock_append_jsonl(path, [b'{"a":1}'], max_bytes=1)
        assert not path.exists()

    def test_size_limit_does_not_change_existing_bytes(self, tmp_path):
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":0}\n')
        with pytest.raises(fsutil.AppendSizeLimit):
            fsutil.flock_append_jsonl(path, [b'{"a":1}'], max_bytes=len(b'{"a":0}\n'))
        assert path.read_bytes() == b'{"a":0}\n'

    def test_excl_collision_retries_existing_open(self, tmp_path, monkeypatch):
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":0}\n')
        real_open = os.open
        state = {"n": 0}

        def open_once_missing(p, flags, *a):
            state["n"] += 1
            if state["n"] == 1 and not (flags & os.O_CREAT):
                raise FileNotFoundError
            if flags & os.O_EXCL:
                raise FileExistsError
            return real_open(p, flags, *a)

        monkeypatch.setattr(fsutil.os, "open", open_once_missing)
        fsutil.flock_append_jsonl(path, [b'{"a":1}'])
        assert path.read_text().splitlines() == ['{"a":0}', '{"a":1}']

    def test_symlink_target_is_not_followed(self, tmp_path):
        target = tmp_path / "secret"
        target.write_bytes(b"keep\n")
        path = tmp_path / "log.jsonl"
        path.symlink_to(target)
        with pytest.raises(OSError):
            fsutil.flock_append_jsonl(path, [b'{"a":1}'], strict=True)
        fsutil.flock_append_jsonl(path, [b'{"a":1}'], strict=False)
        assert target.read_bytes() == b"keep\n"
        assert path.is_symlink()

    def test_non_regular_target_is_refused_before_it_is_chmodded(self, tmp_path):
        """A pipe opens and locks fine, so only the fstat guard stops it, and that
        guard must run before fchmod can rewrite the mode of what it refuses."""
        target = tmp_path / "day.jsonl"
        os.mkfifo(target, 0o666)
        os.chmod(target, 0o666)  # mkfifo applies the umask
        with pytest.raises(OSError) as excinfo:
            fsutil.flock_append_jsonl(target, [b'{"a":1}'], strict=True)
        assert excinfo.value.errno == errno.EINVAL
        assert "not a regular file" in str(excinfo.value)
        # Forensic callers keep the never-break-the-sync contract.
        fsutil.flock_append_jsonl(target, [b'{"a":1}'], strict=False)
        assert stat.S_IMODE(target.stat().st_mode) == 0o666


class TestRotatableSpool:
    def test_append_never_lands_in_a_spool_rotated_while_it_waited(self, tmp_path, monkeypatch):
        import fcntl

        spool, taken = tmp_path / "spool.jsonl", tmp_path / "spool.jsonl.merging"
        fsutil.append_rotatable_jsonl(spool, b'{"n":1}', max_bytes=1024)
        real_flock = fcntl.flock
        rotated = []

        def reader_rotates_first(fd, op):
            if not rotated and op == fcntl.LOCK_EX:
                rotated.append(fsutil.rotate_jsonl(spool, taken))
            return real_flock(fd, op)

        monkeypatch.setattr(fsutil.fcntl, "flock", reader_rotates_first)
        fsutil.append_rotatable_jsonl(spool, b'{"n":2}', max_bytes=1024)
        assert rotated == [True]
        assert taken.read_bytes() == b'{"n":1}\n'
        assert spool.read_bytes() == b'{"n":2}\n'
        assert stat.S_IMODE(spool.stat().st_mode) == 0o600

    def test_append_reopens_when_a_new_spool_replaced_the_locked_one(self, tmp_path, monkeypatch):
        import fcntl

        spool, taken = tmp_path / "spool.jsonl", tmp_path / "spool.jsonl.merging"
        fsutil.append_rotatable_jsonl(spool, b'{"n":1}', max_bytes=1024)
        real_flock = fcntl.flock
        raced = []

        def rotate_then_another_writer(fd, op):
            if not raced and op == fcntl.LOCK_EX:
                raced.append(fsutil.rotate_jsonl(spool, taken))
                spool.write_bytes(b'{"n":3}\n')  # a second writer's fresh spool
            return real_flock(fd, op)

        monkeypatch.setattr(fsutil.fcntl, "flock", rotate_then_another_writer)
        fsutil.append_rotatable_jsonl(spool, b'{"n":2}', max_bytes=1024)
        assert taken.read_bytes() == b'{"n":1}\n'
        assert spool.read_bytes() == b'{"n":3}\n{"n":2}\n'

    def test_every_append_binds_the_directory_entry(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(fsutil, "fsync_dir", calls.append)
        spool = tmp_path / "spool.jsonl"
        fsutil.append_rotatable_jsonl(spool, b"{}", max_bytes=1024)
        fsutil.append_rotatable_jsonl(spool, b"{}", max_bytes=1024)
        assert calls == [tmp_path, tmp_path]

    def test_rotation_never_sleeps_past_its_deadline(self, tmp_path, monkeypatch):
        import fcntl
        import time

        spool, taken = tmp_path / "spool.jsonl", tmp_path / "spool.jsonl.merging"
        fsutil.append_rotatable_jsonl(spool, b"{}", max_bytes=1024)
        fd = os.open(spool, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        slept = []
        monkeypatch.setattr(fsutil.time, "sleep", slept.append)
        try:
            assert fsutil.rotate_jsonl(spool, taken, deadline=time.monotonic()) is False
        finally:
            os.close(fd)
        assert slept == [0.0]

    def test_append_isolates_a_torn_row_and_enforces_its_ceiling(self, tmp_path):
        spool = tmp_path / "spool.jsonl"
        spool.write_bytes(b'{"torn')
        fsutil.append_rotatable_jsonl(spool, b'{"n":1}', max_bytes=1024)
        assert spool.read_bytes() == b'{"torn\n{"n":1}\n'
        with pytest.raises(fsutil.AppendSizeLimit):
            fsutil.append_rotatable_jsonl(spool, b"x" * 2048, max_bytes=1024)
        assert spool.read_bytes() == b'{"torn\n{"n":1}\n'

    def test_rotation_skips_absent_busy_or_foreign_spools(self, tmp_path):
        import fcntl

        spool, taken = tmp_path / "spool.jsonl", tmp_path / "spool.jsonl.merging"
        assert fsutil.rotate_jsonl(spool, taken) is False
        fsutil.append_rotatable_jsonl(spool, b"{}", max_bytes=1024)
        fd = os.open(spool, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)  # an append in progress
        try:
            assert fsutil.rotate_jsonl(spool, taken, retry_intervals=(0.01,)) is False
        finally:
            os.close(fd)
        assert spool.exists() and not taken.exists()
        spool.unlink()
        os.mkfifo(spool)
        with pytest.raises(OSError):
            fsutil.rotate_jsonl(spool, taken)
        with pytest.raises(OSError):
            fsutil.append_rotatable_jsonl(spool, b"{}", max_bytes=1024)


def _assert_guard_diagnostic(error, path, cause):
    message = str(error)
    assert str(path) in message
    assert os.environ["PYTEST_CURRENT_TEST"] in message
    assert cause in message
    assert "tmp_path" in message
    assert "docs/invariants/README.md#test-write-guard" in message
    assert not isinstance(error, Exception), "forensic Exception handlers must not swallow refusal"
    if cause.startswith("protected account-state"):
        # A classifier crash also refuses; only a cause-free refusal proves protection.
        assert error.__cause__ is None
        assert "lookup failed" not in message and "path-resolution failed" not in message


class TestRealStateWriteGuard:
    @pytest.mark.parametrize("marker", [None, ""])
    def test_inactive_marker_does_no_account_or_path_lookup(self, marker, tmp_path, monkeypatch):
        if marker is None:
            monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        else:
            monkeypatch.setenv("PYTEST_CURRENT_TEST", marker)
        monkeypatch.setattr(fsutil.pwd, "getpwuid", lambda uid: pytest.fail("inactive pwd lookup"))
        monkeypatch.setattr(Path, "resolve", lambda *a, **kw: pytest.fail("inactive path lookup"))
        target = tmp_path / "allowed"
        fsutil.atomic_write_bytes(target, b"production control")
        assert target.read_bytes() == b"production control"

    def test_marker_is_checked_at_call_time(self, synthetic_state_root, monkeypatch):
        target = synthetic_state_root / "never-written"
        guard = fsutil.refuse_real_state_write_under_pytest
        for marker in ("", "guard-toggle (call)", "", "guard-toggle-again (call)"):
            monkeypatch.setenv("PYTEST_CURRENT_TEST", marker)
            if marker:
                with pytest.raises(fsutil.PytestRealStateWriteError):
                    guard(target)
            else:
                guard(target)
        assert not target.exists()

    @pytest.mark.parametrize("failure", ["lookup", "empty", "relative", "missing", "file", "type"])
    def test_unusable_account_home_refuses(
        self, failure, tmp_path, monkeypatch, forbid_fs_mutations
    ):
        home_file = tmp_path / "home-file"
        home_file.write_bytes(b"not a directory")

        def lookup(uid):
            assert uid == os.getuid()
            if failure == "lookup":
                raise KeyError("account not found")
            return SimpleNamespace(
                pw_dir={
                    "empty": "",
                    "relative": ".",
                    "missing": str(tmp_path / "absent-home"),
                    "file": str(home_file),
                    "type": None,
                }[failure]
            )

        monkeypatch.setattr(fsutil.pwd, "getpwuid", lookup)
        path = tmp_path / "otherwise-allowed"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        _assert_guard_diagnostic(caught.value, path, "account-home lookup failed")
        assert "pwd.getpwuid(os.getuid()).pw_dir" in str(caught.value)
        assert "Repair the account-home lookup" in str(caught.value)
        assert caught.value.__cause__ is not None
        assert calls == [] and not path.exists()

    @pytest.mark.parametrize("shape", ["root", "existing", "missing", "relative", "dot-dot"])
    def test_account_roots_and_normalized_paths(
        self, shape, synthetic_state_root, synthetic_account_home, monkeypatch, forbid_fs_mutations
    ):
        root = synthetic_state_root
        root.mkdir(parents=True, mode=0o750)
        existing = root / "sentinel"
        existing.write_bytes(b"keep")
        existing.chmod(0o640)
        before = (root.stat(), existing.stat())
        monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))
        monkeypatch.chdir(synthetic_account_home)
        path = {
            "root": root,
            "existing": existing,
            "missing": root / "absent" / "new",
            "relative": existing.relative_to(synthetic_account_home),
            "dot-dot": root.parent / "detour" / ".." / root.name / "sentinel",
        }[shape]
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        _assert_guard_diagnostic(caught.value, path, "protected account-state path")
        assert calls == []
        assert (root.stat(), existing.stat()) == before
        assert existing.read_bytes() == b"keep"
        assert not (root / "absent").exists()

    def test_similar_prefix_sibling_and_unrelated_path_are_allowed(
        self, synthetic_state_root, tmp_path
    ):
        sibling = synthetic_state_root.with_name(synthetic_state_root.name + "-copy")
        sibling.mkdir(parents=True)
        for path in (sibling / "allowed", tmp_path / "unrelated"):
            fsutil.atomic_write_bytes(path, b"allowed")
            assert path.read_bytes() == b"allowed"

    # Value: protects=pwd account home, so a redirected HOME/Path.home mind-meld
    # root stays writable; fails_when=the guard also classifies HOME or
    # Path.home() and raises PytestRealStateWriteError; why_new=refuse tests
    # catch replacing pwd, not unioning it with HOME; seam=none;
    # no_claim=does not pin the open C1 agent-directory holes.
    def test_redirected_home_env_is_not_an_account_root(self, synthetic_account_home, monkeypatch):
        redirected = Path(os.environ["HOME"])
        assert redirected != synthetic_account_home
        monkeypatch.setattr(Path, "home", lambda: redirected)
        path = redirected / ".config/mind-meld/state.json"
        path.parent.mkdir(parents=True)
        fsutil.atomic_write_bytes(path, b"allowed")
        assert path.read_bytes() == b"allowed"
        assert not (synthetic_account_home / ".config").exists()

    @pytest.mark.parametrize("alias", ["into", "out", "dangling", "root-out"])
    def test_stable_symlink_aliases_refuse_before_mutation(
        self, alias, synthetic_state_root, tmp_path, forbid_fs_mutations
    ):
        root = synthetic_state_root
        root.parent.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"keep")
        if alias == "root-out":
            root.symlink_to(outside, target_is_directory=True)
            link = root
        else:
            root.mkdir()
            link = root / "alias" if alias == "out" else tmp_path / "alias"
            link.symlink_to(
                outside if alias == "out" else root / ("absent" if alias == "dangling" else "")
            )
        path = link / "missing" / "new"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        lexically_inside = alias in ("out", "root-out")
        _assert_guard_diagnostic(
            caught.value,
            path,
            "protected account-state path under"
            if lexically_inside
            else "protected account-state path or alias under",
        )
        assert calls == [] and link.is_symlink()
        assert sentinel.read_bytes() == b"keep"
        assert not (outside / "missing").exists() and not (root / "absent").exists()

    @pytest.mark.parametrize("failure", ["loop", "permission", "resolve"])
    def test_path_lookup_failures_refuse(
        self, failure, synthetic_account_home, tmp_path, monkeypatch, forbid_fs_mutations
    ):
        path = tmp_path / "unclassifiable"
        locked = tmp_path / "locked"
        restore_mode = os.chmod
        if failure == "loop":
            path.symlink_to(path)
        elif failure == "permission":
            if os.geteuid() == 0:
                pytest.skip("root ignores directory search permission")
            (locked / "child").mkdir(parents=True)
            path = locked / "child" / "new"
            restore_mode(locked, 0)
        else:
            monkeypatch.setattr(
                Path,
                "resolve",
                lambda *a, **kw: (_ for _ in ()).throw(OSError("resolution failed")),
            )
        try:
            calls = forbid_fs_mutations()
            with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
                fsutil.atomic_write_bytes(path, b"refused")
        finally:
            if failure == "permission":
                restore_mode(locked, 0o700)
        _assert_guard_diagnostic(
            caught.value, path, "path-resolution failed while classifying the write destination"
        )
        assert "Repair the path lookup" in str(caught.value)
        assert caught.value.__cause__ is not None and calls == []
        if failure == "permission":
            assert isinstance(caught.value.__cause__, PermissionError)

    def test_case_alias_on_insensitive_filesystem(self, synthetic_state_root, forbid_fs_mutations):
        root = synthetic_state_root
        root.mkdir(parents=True)
        alternate = root.with_name(root.name.swapcase())
        if not alternate.exists() or not root.samefile(alternate):
            pytest.skip("temporary filesystem is case-sensitive; insensitive branch unavailable")
        calls = forbid_fs_mutations()
        path = alternate / "absent" / "new"
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias under")
        assert calls == [] and not path.parent.exists()

    @pytest.mark.parametrize("canonical_exists", [False, True])
    def test_distinct_case_variant_on_sensitive_filesystem(
        self, canonical_exists, synthetic_state_root
    ):
        root = synthetic_state_root
        root.parent.mkdir(parents=True, exist_ok=True)
        alternate = root.with_name(root.name.swapcase())
        alternate.mkdir()
        if root.exists():
            pytest.skip("temporary filesystem is case-insensitive; distinct variant unavailable")
        if canonical_exists:
            root.mkdir()
            assert not root.samefile(alternate)
        path = alternate / "allowed"
        fsutil.atomic_write_bytes(path, b"distinct path")
        assert path.read_bytes() == b"distinct path"

    def test_missing_case_variant_is_conservatively_refused(
        self, synthetic_state_root, forbid_fs_mutations
    ):
        root = synthetic_state_root
        root.parent.mkdir(parents=True, exist_ok=True)
        path = root.with_name(root.name.swapcase()) / "absent" / "new"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias under")
        assert calls == [] and not root.exists() and not path.parent.exists()

    def test_case_alias_with_every_root_component_missing(
        self, synthetic_state_root, synthetic_account_home, forbid_fs_mutations
    ):
        """With no root component present (a Mac without ~/Sync), the home is the anchor."""
        relative = synthetic_state_root.relative_to(synthetic_account_home)
        alias = synthetic_account_home.joinpath(*(part.swapcase() for part in relative.parts))
        path = alias / "absent" / "new"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias under")
        assert calls == [] and not any(synthetic_account_home.iterdir())

    @pytest.mark.parametrize("helper", ["atomic", "append", "strict-append", "spool"])
    @pytest.mark.parametrize("exists", [False, True])
    def test_helpers_refuse_before_first_mutation(
        self, helper, exists, synthetic_state_root, forbid_fs_mutations
    ):
        root = synthetic_state_root
        path = root / "parent" / "sentinel"
        if exists:
            path.parent.mkdir(parents=True, mode=0o750)
            path.write_bytes(b"keep\n")
            path.chmod(0o640)
        before = path.stat() if exists else None
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError):
            if helper == "atomic":
                fsutil.atomic_write_bytes(path, b"refused")
            elif helper == "spool":
                fsutil.append_rotatable_jsonl(path, b"{}", max_bytes=1024)
            else:
                fsutil.flock_append_jsonl(path, [b"{}"], strict=helper == "strict-append")
        assert calls == []
        if exists:
            assert path.stat() == before and path.read_bytes() == b"keep\n"
        else:
            assert not path.exists() and not path.parent.exists()

    @pytest.mark.parametrize("protected_side", ["source", "destination"])
    def test_rotation_checks_both_paths(
        self, protected_side, synthetic_state_root, tmp_path, forbid_fs_mutations
    ):
        synthetic_state_root.mkdir(parents=True)
        protected, allowed = synthetic_state_root / "spool", tmp_path / "allowed"
        protected.write_bytes(b"protected\n")
        allowed.write_bytes(b"allowed\n")
        before = (protected.stat(), allowed.stat())
        source, destination = (
            (protected, allowed) if protected_side == "source" else (allowed, protected)
        )
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError):
            fsutil.rotate_jsonl(source, destination)
        assert calls == [] and (protected.stat(), allowed.stat()) == before
        assert protected.read_bytes() == b"protected\n" and allowed.read_bytes() == b"allowed\n"

    def test_parent_alias_into_root_with_final_link_out_refuses(
        self, synthetic_state_root, tmp_path, forbid_fs_mutations
    ):
        """Helpers create temps and rename inside the resolved parent, not the final target."""
        nested = synthetic_state_root / "nested"
        nested.mkdir(parents=True)
        outside = tmp_path / "outside.json"
        outside.write_bytes(b"{}")
        (nested / "state.json").symlink_to(outside)
        alias = tmp_path / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        path = alias / "state.json"
        entries = sorted(nested.iterdir())
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias under")
        assert calls == [] and sorted(nested.iterdir()) == entries
        assert (nested / "state.json").is_symlink() and outside.read_bytes() == b"{}"

    @pytest.mark.parametrize("helper", ["append", "spool"])
    def test_dot_dot_out_of_missing_root_refuses_before_mkdir(
        self, helper, synthetic_state_root, forbid_fs_mutations
    ):
        """Recursive mkdir creates `<root>/missing` although `..` normalizes it away."""
        synthetic_state_root.parent.mkdir(parents=True, exist_ok=True)
        path = synthetic_state_root / "missing" / ".." / ".." / "escape" / "log.jsonl"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            if helper == "append":
                fsutil.flock_append_jsonl(path, [b"{}"])
            else:
                fsutil.append_rotatable_jsonl(path, b"{}", max_bytes=1024)
        _assert_guard_diagnostic(caught.value, path, "protected account-state path under")
        assert calls == [] and not synthetic_state_root.exists()
        assert not (synthetic_state_root.parent / "escape").exists()

    # Value: protects=mkdir through a live symlink into an account root cannot
    # create the in-root prefix when `..` normalizes the final path outside;
    # fails_when=flock_append_jsonl creates `<root>/missing` or the guard
    # returns; why_new=lexical dot-dot stays on the root spelling and
    # symlink-into tests do not use `..`; seam=none; no_claim=not a concurrent
    # symlink-swap sandbox.
    def test_symlink_dot_dot_refuses_before_in_root_mkdir(
        self, synthetic_account_home, forbid_fs_mutations
    ):
        root = synthetic_account_home / ".config/mind-meld"
        root.mkdir(parents=True)
        sentinel = root / "sentinel"
        sentinel.write_bytes(b"keep")
        alias = synthetic_account_home.parent / "alias"
        alias.symlink_to(root, target_is_directory=True)
        path = alias / "missing" / ".." / ".." / "escape" / "log.jsonl"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.flock_append_jsonl(path, [b"{}"])
        _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias under")
        assert calls == []
        assert sentinel.read_bytes() == b"keep"
        assert not (root / "missing").exists()
        assert not (synthetic_account_home / ".config" / "escape").exists()
        assert alias.is_symlink()

    def test_regular_file_ancestor_keeps_native_errors(self, synthetic_account_home, tmp_path):
        """Nothing exists beneath a regular file, so helpers keep their own ENOTDIR result."""
        blocker = tmp_path / "regular-file"
        blocker.write_bytes(b"x")
        path = blocker / "sub" / "log.jsonl"
        fsutil.flock_append_jsonl(path, [b"{}"])  # best-effort: OSError is swallowed
        with pytest.raises(NotADirectoryError):
            fsutil.atomic_write_bytes(path, b"{}")
        assert blocker.read_bytes() == b"x"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory search permission")
    def test_unreadable_protected_root_is_named(
        self, synthetic_account_home, tmp_path, forbid_fs_mutations
    ):
        library = synthetic_account_home / "Library"
        library.mkdir()
        icloud = library / "Mobile Documents/com~apple~CloudDocs/mind-meld"
        path = tmp_path / "unrelated" / "state.json"
        restore_mode = os.chmod
        restore_mode(library, 0)
        try:
            calls = forbid_fs_mutations()
            with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
                fsutil.atomic_write_bytes(path, b"refused")
        finally:
            restore_mode(library, 0o700)
        _assert_guard_diagnostic(caught.value, path, f"classifying protected root {icloud}")
        assert "Repair the path lookup" in str(caught.value)
        assert isinstance(caught.value.__cause__, PermissionError) and calls == []

    def test_dangling_root_link_target_case_alias_refuses(
        self, synthetic_state_root, tmp_path, forbid_fs_mutations
    ):
        """An absent volume behind a root link is reached by any spelling of its target."""
        root = synthetic_state_root
        root.parent.mkdir(parents=True, exist_ok=True)
        volume = tmp_path / "volume"
        volume.mkdir()
        root.symlink_to(volume / "mm-state", target_is_directory=True)
        path = volume / "MM-STATE" / "events.jsonl"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.flock_append_jsonl(path, [b"{}"])
        _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias under")
        assert calls == [] and list(volume.iterdir()) == []

    def test_symlinked_root_target_spelling_refuses(
        self, synthetic_state_root, tmp_path, forbid_fs_mutations
    ):
        root = synthetic_state_root
        root.parent.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "external-store"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
        path = target / "missing" / "new"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias under")
        assert calls == [] and not (target / "missing").exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory search permission")
    def test_protected_verdict_precedes_unrelated_root_lookup_failure(
        self, synthetic_account_home, tmp_path, forbid_fs_mutations
    ):
        events = synthetic_account_home / ".local/share/mind-meld"
        events.mkdir(parents=True)
        library = synthetic_account_home / "Library"
        library.mkdir()
        link = tmp_path / "link"
        link.symlink_to(events, target_is_directory=True)
        path = link / "new"
        restore_mode = os.chmod
        restore_mode(library, 0)
        try:
            calls = forbid_fs_mutations()
            with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
                fsutil.atomic_write_bytes(path, b"refused")
        finally:
            restore_mode(library, 0o700)
        _assert_guard_diagnostic(
            caught.value, path, f"protected account-state path or alias under {events}"
        )
        assert calls == [] and not (events / "new").exists()

    @pytest.mark.parametrize("ancestor", [".config", "Library", "Sync", ".local"])
    def test_regular_file_root_ancestor_keeps_unrelated_writes(
        self, ancestor, synthetic_account_home, tmp_path
    ):
        (synthetic_account_home / ancestor).write_bytes(b"not a directory")
        path = tmp_path / "unrelated" / "state.json"
        path.parent.mkdir()
        fsutil.atomic_write_bytes(path, b"{}")
        assert path.read_bytes() == b"{}"

    def test_real_account_path_is_classification_only(self, forbid_fs_mutations):
        home = Path(fsutil.pwd.getpwuid(os.getuid()).pw_dir)
        path = home / ".config/mind-meld/never-written-71b"
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.refuse_real_state_write_under_pytest(path)
        _assert_guard_diagnostic(
            caught.value, path, f"protected account-state path under {home / '.config/mind-meld'}"
        )
        assert calls == []

    def test_fsync_is_unguarded(self, synthetic_state_root, monkeypatch, forbid_fs_mutations):
        synthetic_state_root.mkdir(parents=True)
        monkeypatch.setattr(
            fsutil.pwd, "getpwuid", lambda uid: pytest.fail("flush is not a writer")
        )
        calls = forbid_fs_mutations()
        fsutil.fsync_dir(synthetic_state_root)
        assert calls == []

    def test_documented_diagnostic_anchor_and_safe_example(self):
        doc = (Path(__file__).parents[1] / "docs/invariants/README.md").read_text()
        assert "## Test write guard" in doc
        assert "fsutil.py:refuse_real_state_write_under_pytest" in doc
        assert 'fsutil.atomic_write_bytes(tmp_path / "state.json", b"{}")' in doc
        rows = [
            line
            for line in doc.splitlines()
            if line.startswith("|") and "#test-write-guard" in line
        ]
        for helper in (
            "atomic_write_bytes",
            "flock_append_jsonl",
            "append_rotatable_jsonl",
            "rotate_jsonl",
            "locked_json_rmw",
            "locked_json_durable_rmw",
        ):
            assert any(helper in row for row in rows), helper


@pytest.mark.parametrize("existing", [False, True])
def test_conftest_free_cli_breadcrumb_refusal_and_safe_control(tmp_path, existing):
    """The CLI breadcrumb refuses without conftest or the star-import-omitted `_isolate_*` names."""
    account = tmp_path / "account"
    protected = account / ".config/mind-meld/last-autorun.json"
    protected.parent.mkdir(parents=True, mode=0o750)
    if existing:
        protected.write_bytes(b'{"sentinel":"keep"}')
        protected.chmod(0o640)
    before = protected.stat() if existing else None
    parent_before = protected.parent.stat()
    cwd = tmp_path / "child"
    cwd.mkdir()
    (cwd / "fixture_shapes.py").write_text(
        "PUBLIC = True\ndef _isolate_state():\n    raise AssertionError('never loaded')\n"
    )
    code = textwrap.dedent("""\
        import os, pwd, sys
        from pathlib import Path
        from types import SimpleNamespace
        from fixture_shapes import *
        assert PUBLIC and '_isolate_state' not in globals()
        pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=sys.argv[1])
        from mind_meld import cli, sidecar
        assert 'tests.conftest' not in sys.modules and 'conftest' not in sys.modules
        sidecar.SIDECAR_DIR = Path(sys.argv[2]).parent
        print('CLI imported; underscored isolation omitted', flush=True)
        if sys.argv[3] == 'refuse':
            def mutation(*args, **kwargs):
                raise AssertionError('mutation sentinel reached')
            real_open = os.open
            def checked_open(path, flags, *args, **kwargs):
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    return mutation(path, flags, *args, **kwargs)
                return real_open(path, flags, *args, **kwargs)
            Path.mkdir = mutation
            os.mkdir = os.chmod = os.fchmod = os.write = os.ftruncate = mutation
            os.open = checked_open
        cli._write_autorun_breadcrumb('pull', 'success')
        print('breadcrumb completed', flush=True)
        """)
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).parents[1] / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        HOME=str(tmp_path / "different-home"),
        PYTEST_CURRENT_TEST="conftest-free-71b::breadcrumb (call)",
    )
    refused = subprocess.run(
        [sys.executable, "-c", code, str(account), str(protected), "refuse"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refused.returncode != 0
    assert "CLI imported; underscored isolation omitted" in refused.stdout
    assert "PytestRealStateWriteError" in refused.stderr
    assert "conftest-free-71b::breadcrumb (call)" in refused.stderr
    assert str(protected) in refused.stderr and "tmp_path" in refused.stderr
    assert "docs/invariants/README.md#test-write-guard" in refused.stderr
    assert "mutation sentinel reached" not in refused.stderr
    assert protected.parent.stat() == parent_before
    if existing:
        assert protected.stat() == before and protected.read_bytes() == b'{"sentinel":"keep"}'
    else:
        assert not protected.exists()
    safe = tmp_path / "safe/last-autorun.json"
    control = subprocess.run(
        [sys.executable, "-c", code, str(account), str(safe), "allow"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert control.returncode == 0, control.stderr
    assert "breadcrumb completed" in control.stdout
    assert json.loads(safe.read_bytes())["pull"]["outcome"] == "success"
    assert stat.S_IMODE(safe.stat().st_mode) == 0o600
