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
        # Linux fcntl has no F_FULLFSYNC; supply Darwin's value (Track 73A).
        monkeypatch.setattr(fsutil.fcntl, "F_FULLFSYNC", 51, raising=False)
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
        monkeypatch.setattr(fsutil.fcntl, "F_FULLFSYNC", 51, raising=False)
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
        monkeypatch.setattr(fsutil.fcntl, "F_FULLFSYNC", 51, raising=False)

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
    assert repr(str(path)) in message
    assert repr(os.environ["PYTEST_CURRENT_TEST"]) in message
    assert cause in message
    if cause.startswith("configured-root"):
        assert "tmp_path" not in message and "real config" in message
    else:
        assert "tmp_path" in message
    assert (
        "No write occurred. Fix the destination/config and rerun; nothing to clean up." in message
    )
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
        import builtins

        original_import = builtins.__import__

        def no_toml(name, *args, **kwargs):
            assert name != "tomllib", "inactive guard imported tomllib"
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_toml)
        monkeypatch.setattr(
            fsutil, "_pytest_config_stamp", lambda *a: pytest.fail("inactive config lookup")
        )
        original_open = Path.open

        def no_config_open(path, *args, **kwargs):
            if path.name == "config.toml":
                pytest.fail("inactive config open")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", no_config_open)
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
        before = sorted(synthetic_account_home.iterdir())
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(path, b"refused")
        _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias under")
        assert calls == [] and sorted(synthetic_account_home.iterdir()) == before

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
        _assert_guard_diagnostic(caught.value, path, f"classifying protected root {str(icloud)!r}")
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
            caught.value, path, f"protected account-state path or alias under {str(events)!r}"
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
            caught.value,
            path,
            f"protected account-state path under {str(home / '.config/mind-meld')!r}",
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


def _plant_guard_config(home, text):
    path = home / ".config/mind-meld/config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _assert_synthetic_destination(path, home):
    roots, _ = fsutil._pytest_refused_roots(home)
    assert any(path.is_relative_to(root) and root.is_relative_to(home) for root, _ in roots)
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        fsutil.refuse_real_state_write_under_pytest(path)
    _assert_guard_diagnostic(caught.value, path, "protected account-state path")


@pytest.mark.parametrize(
    ("toml", "remedy"),
    [
        ("storage = 1", "must be a table"),
        ("sync = 1", "must be a table"),
        ("[sync]\nsources = 1", "must be a list"),
        ("[sync]\nsources = [1]", "add or fix"),
        ('[[sync.sources]]\nname = "custom"', "add or fix"),
        ("[[sync.sources]]\npath = 1", "add or fix"),
        ('[storage]\npath="one"\npath="two"', "duplicate keys"),
        ("[broken", "TOML syntax"),
        ("[storage]\npath=" + "[" * 2000 + "0" + "]" * 2000, "excessive nesting"),
        ("[sync]\nclaude_dir=1", "legacy key"),
    ],
    ids=[
        "storage-shape",
        "sync-shape",
        "sources-shape",
        "source-shape",
        "missing-path",
        "non-string-path",
        "duplicate-key",
        "parse-error",
        "excessive-nesting",
        "legacy-shape",
    ],
)
def test_configured_root_malformed_refuses_closed(
    toml, remedy, synthetic_account_home, tmp_path, forbid_fs_mutations
):
    config_path = _plant_guard_config(synthetic_account_home, toml)
    target = tmp_path / "otherwise-safe"
    calls = forbid_fs_mutations()
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        fsutil.atomic_write_bytes(target, b"never")
    _assert_guard_diagnostic(caught.value, target, "configured-root lookup failed")
    assert repr(str(config_path)) in str(caught.value) and remedy in str(caught.value)
    assert caught.value.__cause__ is not None and calls == []


@pytest.mark.parametrize("toml", ["", "[storage]", "[storage]\npath=1", '[storage]\npath=""'])
def test_configured_root_empty_storage_tmp_control(toml, synthetic_account_home, tmp_path):
    _plant_guard_config(synthetic_account_home, toml)
    fsutil.atomic_write_bytes(tmp_path / "allowed", b"allowed")
    roots, _ = fsutil._pytest_refused_roots(synthetic_account_home)
    assert all(key != "storage.path" for _, key in roots)


@pytest.mark.parametrize("kind", ["unreadable", "unsearchable", "directory", "oversize"])
def test_configured_root_file_refuses_closed(
    kind, synthetic_account_home, tmp_path, monkeypatch, forbid_fs_mutations
):
    path = _plant_guard_config(synthetic_account_home, "")
    restore_mode = os.chmod
    if kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "oversize":
        path.write_bytes(b" " * (fsutil._PYTEST_CONFIG_MAX_BYTES + 1))
    elif kind == "unsearchable":
        # Value: protects=a config whose stat fails with EACCES refuses closed;
        # fails_when=non-ENOENT stat errors count as "no config" and configured roots drop;
        # why_new=the unreadable row fails at open after stat succeeds; seam=none;
        # no_claim=skipped as root, which ignores directory search permission.
        if os.geteuid() == 0:
            pytest.skip("root ignores directory search permission")
        restore_mode(path.parent, 0)
    else:
        original = Path.open

        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError("synthetic read denial")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    target = tmp_path / "safe"
    try:
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            fsutil.atomic_write_bytes(target, b"never")
    finally:
        if kind == "unsearchable":
            restore_mode(path.parent, 0o700)
    _assert_guard_diagnostic(caught.value, target, "configured-root lookup failed")
    assert repr(str(path)) in str(caught.value) and calls == []
    assert {
        "unreadable": "permissions",
        "unsearchable": "permissions",
        "directory": "regular",
        "oversize": "cap",
    }[kind] in str(caught.value)


@pytest.mark.parametrize(
    "spelling", ["absolute", "tilde", "named-user", "relative", "legacy", "disabled"]
)
def test_configured_root_refuses_expanded_destination(
    spelling, synthetic_account_home, monkeypatch, forbid_fs_mutations
):
    home = synthetic_account_home
    monkeypatch.chdir(home)
    custom = home / "custom"
    raw = {
        "absolute": str(custom),
        "tilde": "~/custom",
        "named-user": "~guard-user/custom",
        "relative": "custom",
        "legacy": str(custom),
        "disabled": str(custom),
    }[spelling]
    if spelling == "legacy":
        text = f"[sync]\nclaude_dir={json.dumps(raw)}"
        key = "sync.claude_dir"
    elif spelling == "disabled":
        text = (
            '[sync]\ndisabled_sources=["custom"]\n[[sync.sources]]\n'
            f'name="custom"\npath={json.dumps(raw)}'
        )
        key = "sync.sources[0].path"
    else:
        text = f"[storage]\npath={json.dumps(raw)}"
        key = "storage.path"
    config_path = _plant_guard_config(home, text)
    path = custom / "missing/file"
    calls = forbid_fs_mutations()
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        fsutil.atomic_write_bytes(path, b"never")
    _assert_guard_diagnostic(caught.value, path, "protected account-state path")
    message = str(caught.value)
    for value in (str(custom), key, str(config_path), str(home)):
        assert repr(value) in message
    assert "$HOME is ignored" in message and calls == []


@pytest.mark.parametrize("spelling", ["/", "home", "ancestor", "~", "~missing-user/store"])
def test_configured_root_unsafe_or_unknown_refuses_lookup(
    spelling, synthetic_account_home, tmp_path, forbid_fs_mutations
):
    home = synthetic_account_home
    raw = {"home": str(home), "ancestor": str(home.parent)}.get(spelling, spelling)
    config_path = _plant_guard_config(home, f"[storage]\npath={json.dumps(raw)}")
    calls = forbid_fs_mutations()
    target = tmp_path / "otherwise-safe"
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        fsutil.atomic_write_bytes(target, b"never")
    _assert_guard_diagnostic(caught.value, target, "configured-root lookup failed")
    assert repr(raw) in str(caught.value) and repr(str(config_path)) in str(caught.value)
    assert "edit that path" in str(caught.value) and caught.value.__cause__.__cause__ is not None
    assert calls == []


def test_configured_root_cache_same_size_rewrite_and_chdir(synthetic_account_home, monkeypatch):
    home = synthetic_account_home
    path = _plant_guard_config(home, '[storage]\npath="alpha"')
    first = home / "cwd-one"
    second = home / "cwd-two"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    roots, stamp = fsutil._pytest_refused_roots(home)
    assert dict((key, root) for root, key in roots)["storage.path"] == first / "alpha"
    before = fsutil._pytest_config_strings.cache_info()
    fsutil.refuse_real_state_write_under_pytest(home / "safe")
    fsutil.refuse_real_state_write_under_pytest(home / "safe")
    assert fsutil._pytest_config_strings.cache_info().hits >= before.hits + 2
    monkeypatch.chdir(second)
    fsutil.refuse_real_state_write_under_pytest(first / "alpha/file")
    with pytest.raises(fsutil.PytestRealStateWriteError):
        fsutil.refuse_real_state_write_under_pytest(second / "alpha/file")
    path.write_text('[storage]\npath="bravo"')
    os.utime(path, ns=(stamp[1] + 1_000_000, stamp[1] + 1_000_000))
    assert path.stat().st_size == stamp[2]
    fsutil.refuse_real_state_write_under_pytest(second / "alpha/file")
    with pytest.raises(fsutil.PytestRealStateWriteError):
        fsutil.refuse_real_state_write_under_pytest(second / "bravo/file")


@pytest.mark.parametrize("kind", ["agent", "agent-live", "configured", "configured-dotdot"])
def test_cached_roots_revalidate_retargeted_dangling_root(kind, synthetic_account_home, tmp_path):
    outside = tmp_path / "outside"
    (outside / "A").mkdir(parents=True)
    (outside / "B").mkdir()
    if kind == "agent-live":
        # Value: protects=retargeting a link inside an existing root's chain refreshes
        # its cached resolution; fails_when=the cache key ignores the followed root
        # identity and B/state writes pass; why_new=other rows dangle and bypass the
        # cache; seam=none; no_claim=not a concurrent symlink-swap sandbox.
        (outside / "A/state").mkdir()
        (outside / "B/state").mkdir()
    current = outside / "current"
    current.symlink_to(outside / "A")
    root = synthetic_account_home / (".claude" if kind.startswith("agent") else "store-link")
    root.symlink_to(current / "state")
    if kind.startswith("configured"):
        # "missing/.." keeps a lexically missing component ahead of the link.
        raw = "~/missing/../store-link" if kind == "configured-dotdot" else str(root)
        _plant_guard_config(synthetic_account_home, f"[storage]\npath={json.dumps(raw)}")
    with pytest.raises(fsutil.PytestRealStateWriteError):
        fsutil.refuse_real_state_write_under_pytest(outside / "A/state/file")
    current.unlink()
    current.symlink_to(outside / "B")
    path = outside / "B/state/file"
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        fsutil.refuse_real_state_write_under_pytest(path)
    _assert_guard_diagnostic(caught.value, path, "protected account-state path or alias")
    fsutil.refuse_real_state_write_under_pytest(outside / "A/state/file")


@pytest.mark.parametrize("raw", ["absolute", "~guard-user/store", "~/store", "~//store"])
def test_configured_root_production_parity(raw, synthetic_account_home, monkeypatch):
    from mind_meld import config

    home = synthetic_account_home
    monkeypatch.setenv("HOME", str(home))
    (home / "store").mkdir()
    path_value = str(home / "store") if raw == "absolute" else raw
    text = '[device]\nid="dev"\nname="Mac"\n[storage]\npath=' + json.dumps(path_value)
    text += "\n[sync]\nclaude_dir=" + json.dumps(path_value)
    text += '\n[[sync.sources]]\nname="custom"\ntype="generic"\npath=' + json.dumps(path_value)
    path = _plant_guard_config(home, text)
    loaded = config.load_config(path)
    roots, _ = fsutil._pytest_refused_roots(home)
    extracted = {key: root.resolve() for root, key in roots}
    assert extracted["storage.path"] == Path(loaded["storage"]["path"])
    assert extracted["sync.claude_dir"] == Path(loaded["sync"]["claude_dir"])
    assert (
        extracted["sync.sources[0].path"] == Path(config.get_sources(loaded)[0]["path"]).resolve()
    )


@pytest.mark.no_mm_events_isolation
@pytest.mark.owns_skill_paths
def test_default_source_and_agent_roots_pin(synthetic_account_home):
    from mind_meld import config, skill_link

    values = [source["path"] for source in config.DEFAULT_SOURCES]
    values += [row.skills_root for row in skill_link.AGENT_ROWS]
    _assert_root_coverage(values, synthetic_account_home)
    with pytest.raises(AssertionError, match="uncovered.*fsutil.*#test-write-guard"):
        _assert_root_coverage(["~/.new-agent/skills"], synthetic_account_home)
    with pytest.raises(AssertionError, match="not home-anchored.*#test-write-guard"):
        _assert_root_coverage(["/opt/new-agent/skills"], synthetic_account_home)


def _assert_root_coverage(values, home):
    roots, _ = fsutil._pytest_refused_roots(home)
    for value in values:
        assert value == "~" or value.startswith("~/"), (
            f"default destination {value!r} is not home-anchored: classify it in "
            "fsutil._pytest_refused_roots and this pin; "
            "see docs/invariants/README.md#test-write-guard"
        )
        path = fsutil._expand_pytest_root(value, home)
        assert any(path.is_relative_to(root) for root, _ in roots), (
            f"uncovered path {str(path)!r}: edit fsutil._pytest_refused_roots; "
            "see docs/invariants/README.md#test-write-guard"
        )
        _assert_synthetic_destination(path, home)


def test_guard_config_diagnostic_escapes_paths_only(synthetic_account_home):
    path = synthetic_account_home / "custom\n\x1b[2J"
    config_path = _plant_guard_config(
        synthetic_account_home,
        f'[storage]\npath={json.dumps(str(path))}\n[credentials]\ntoken="never-display-this"',
    )
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        fsutil.refuse_real_state_write_under_pytest(path / "file")
    message = str(caught.value)
    assert "\x1b" not in message and "\n" not in message
    assert repr(str(path)) in message and repr(str(config_path)) in message
    assert "never-display-this" not in message


@pytest.mark.parametrize("failure", ["invalid", "invalid-unmarked", "unreadable"])
def test_session_config_preflight_names_file_once(
    failure, synthetic_account_home, capsys, monkeypatch
):
    from tests.conftest import pytest_sessionstart

    path = _plant_guard_config(synthetic_account_home, "[broken")
    if failure == "invalid-unmarked":
        # Value: protects=the real session-start preflight, which runs with no pytest
        # marker, still refuses a broken config and leaves no marker behind;
        # fails_when=it stops supplying its own marker (silent no-op) or leaks it;
        # why_new=other rows inherit this test's marker; seam=none; no_claim=xdist workers.
        monkeypatch.delenv("PYTEST_CURRENT_TEST")
    elif failure == "unreadable":
        original = Path.open

        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError("synthetic unreadable config")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    marker = os.environ.get("PYTEST_CURRENT_TEST")
    with pytest.raises(pytest.UsageError) as caught:
        pytest_sessionstart(SimpleNamespace(config=SimpleNamespace()))
    assert str(caught.value).count(str(path)) == 1
    assert "real account write-guard preflight" in str(caught.value)
    assert capsys.readouterr().out == "" and os.environ.get("PYTEST_CURRENT_TEST") == marker
    # Workers rely on the controller's single preflight, never duplicate errors.
    pytest_sessionstart(SimpleNamespace(config=SimpleNamespace(workerinput={})))


@pytest.mark.parametrize("failure", ["root-contains-home", "agent-root-loop", "protected-tmpdir"])
def test_session_preflight_names_root_failures_once(failure, synthetic_account_home, monkeypatch):
    import tempfile

    from tests.conftest import pytest_sessionstart

    if failure == "root-contains-home":
        _plant_guard_config(synthetic_account_home, '[storage]\npath="~"')
        expected = "contains pwd account home"
    elif failure == "protected-tmpdir":
        # Value: protects=a temp dir inside a protected root fails the run once at preflight;
        # fails_when=the probe stops living under gettempdir() or protected verdicts pass;
        # why_new=other rows only exercise lookup failures; seam=none (tempfile.tempdir is
        # documented stdlib config); no_claim=does not prove the TMPDIR remedy wording.
        temp = synthetic_account_home / ".claude/tmp"
        monkeypatch.setattr(tempfile, "tempdir", str(temp))
        expected = f"protected account-state path under {str(synthetic_account_home / '.claude')!r}"
    else:
        loop = synthetic_account_home / ".gstack"
        loop.symlink_to(loop)
        expected = repr(str(loop))
    with pytest.raises(pytest.UsageError) as caught:
        pytest_sessionstart(SimpleNamespace(config=SimpleNamespace()))
    assert "real account write-guard preflight" in str(caught.value)
    assert expected in str(caught.value)


def test_configured_root_resolution_failure_has_chained_remedy(
    synthetic_account_home, tmp_path, forbid_fs_mutations
):
    home = synthetic_account_home
    loop = home / "loop"
    loop.symlink_to(loop)
    config_path = _plant_guard_config(home, f"[storage]\npath={json.dumps(str(loop))}")
    calls = forbid_fs_mutations()
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        fsutil.atomic_write_bytes(tmp_path / "safe", b"never")
    _assert_guard_diagnostic(caught.value, tmp_path / "safe", "configured-root lookup failed")
    assert "resolution failed" in str(caught.value) and "symlink chain" in str(caught.value)
    assert repr(str(config_path)) in str(caught.value)
    assert isinstance(caught.value.__cause__.__cause__, (OSError, RuntimeError)) and calls == []


def test_configured_root_reexpands_named_user_and_root_alias(
    synthetic_account_home, tmp_path, monkeypatch
):
    home = synthetic_account_home
    first, second = home / "first", home / "second"
    first.mkdir()
    second.mkdir()
    _plant_guard_config(home, '[storage]\npath="~guard-user/store"')
    monkeypatch.setattr(fsutil.pwd, "getpwnam", lambda user: SimpleNamespace(pw_dir=str(first)))
    with pytest.raises(fsutil.PytestRealStateWriteError):
        fsutil.refuse_real_state_write_under_pytest(first / "store/file")
    monkeypatch.setattr(fsutil.pwd, "getpwnam", lambda user: SimpleNamespace(pw_dir=str(second)))
    fsutil.refuse_real_state_write_under_pytest(first / "store/file")
    with pytest.raises(fsutil.PytestRealStateWriteError):
        fsutil.refuse_real_state_write_under_pytest(second / "store/file")
    external = tmp_path / "external"
    external.mkdir()
    (second / "store").symlink_to(external)
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        fsutil.refuse_real_state_write_under_pytest(external / "missing/file")
    _assert_guard_diagnostic(
        caught.value, external / "missing/file", "protected account-state path or alias"
    )


def test_marker_inheriting_child_refuses_configured_writer(synthetic_account_home, tmp_path):
    home = synthetic_account_home
    protected = home / "custom-store/autopull.log"
    config_path = _plant_guard_config(home, f"[storage]\npath={json.dumps(str(protected.parent))}")
    protected.parent.mkdir()
    protected.write_bytes(b"keep")
    before = protected.stat()
    code = textwrap.dedent("""
        import pwd, sys
        from pathlib import Path
        from types import SimpleNamespace
        pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=sys.argv[1])
        from mind_meld import cli, sidecar
        sidecar.SIDECAR_DIR = Path(sys.argv[2])
        cli._log_unexpected('pull', RuntimeError('child incident'))
    """)
    env = dict(
        os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"), PYTHONDONTWRITEBYTECODE="1"
    )
    # No marker is assigned here: the child inherits pytest's active marker.
    refused = subprocess.run(
        [sys.executable, "-c", code, str(home), str(protected.parent)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refused.returncode != 0 and "PytestRealStateWriteError" in refused.stderr
    assert "protected account-state path" in refused.stderr
    assert os.environ["PYTEST_CURRENT_TEST"] in refused.stderr
    assert repr(str(config_path)) in refused.stderr
    assert protected.stat() == before and protected.read_bytes() == b"keep"
    safe = tmp_path / "child-safe"
    allowed = subprocess.run(
        [sys.executable, "-c", code, str(home), str(safe)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert "RuntimeError: child incident" in (safe / "autopull.log").read_text()


def _raw_writer_case(writer, base, monkeypatch, tmp_path, exists=False):
    """Retarget after autouse; return the *actual* destination and native call."""
    from typer.testing import CliRunner

    from mind_meld import (
        attemptlog,
        cli,
        config,
        devices,
        lockfile,
        resolveflow,
        seen_sources,
        sidecar,
        upgrade,
    )
    from mind_meld.storage.local import LocalBackend

    monkeypatch.setattr(sidecar, "SIDECAR_DIR", base)
    monkeypatch.setattr(seen_sources, "SEEN_DIR", base)
    monkeypatch.setattr(upgrade, "CACHE_DIR", base)
    monkeypatch.setattr(lockfile, "LOCK_PATH", base / "mm.lock")
    monkeypatch.setattr(devices, "DEVICES_WRITE_LOCK", base / "devices.lock")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "init-config.toml")
    monkeypatch.setattr(config, "_is_default_mm_events_path", lambda path: True)
    monkeypatch.setattr(cli, "DEFAULT_ARGON2_MEMORY_KB", 1024)
    monkeypatch.setattr(
        cli,
        "_prompt_sources",
        lambda: [{"name": "custom", "path": str(tmp_path), "type": "generic", "include_dirs": []}],
    )
    monkeypatch.setattr(cli.events_tail, "_run_events_backfill", lambda *args: None)
    monkeypatch.setattr(cli.skill_link, "_ensure_retro_skill_links", lambda **kw: None)
    names = {
        "log_unexpected": "autopull.log",
        "migration_write": "migration-state.json",
        "migration_unlink": "migration-state.json",
        "pull_apply": "nested/note.md",
        "init_storage": "storage",
        "seen_read": "seen-sources.json",
        "seen_acknowledge": "seen-sources.json",
        "seen_write": "seen-sources.json",
        "pipx_install_lock": upgrade.INSTALL_LOCK_NAME,
        "update_log": upgrade.UPDATE_LOG_NAME,
        "acquire_lock": "mm.lock",
        "devices_write_lock": "devices.lock",
        "save_config": "saved.toml",
        "events_bootstrap": "events-root",
        "sidecar_write": "last-push.json",
        "attemptlog_write": "last-attended-capture.json",
        "inversion_marker": "inversion-installed-at",
    }
    path = base / ("data/sentinel" if writer.startswith("local_") else names[writer])
    if exists:
        if writer in ("init_storage", "events_bootstrap"):
            path.mkdir(parents=True, exist_ok=True, mode=0o755)
            path.chmod(0o755)
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            path.write_bytes(b"keep")
            path.chmod(0o640)
        if writer == "local_delete_conflict_copies":
            for name in ("sentinel 2", "sentinel 3"):
                (path.parent / name).write_bytes(b"keep-copy")

    def invoke():
        if writer.startswith("local_"):
            backend = LocalBackend(base)
            method = getattr(backend, writer.removeprefix("local_"))
            if writer in ("local_put", "local_put_exclusive"):
                return method("data/sentinel", b"new")
            return method("data/sentinel")
        if writer == "log_unexpected":
            return cli._log_unexpected("pull", RuntimeError("original incident"))
        if writer.startswith("migration_"):
            return cli._write_migration_breadcrumb(["custom"] if writer.endswith("write") else [])
        if writer == "pull_apply":
            return cli._apply_incoming_file(path, "note.md", b"new", {}, "peer")
        if writer == "init_storage":
            # CliRunner catches Exception only; the guard's BaseException escapes.
            result = CliRunner().invoke(cli.app, ["init"], input=f"{path}\nTest Mac\npw\npw\n")
            assert result.exit_code == 0, (result.output, result.exception)
            return result
        if writer == "seen_read":
            return seen_sources.read(["new"])
        if writer == "seen_acknowledge":
            return seen_sources.acknowledge(["new"], initial=["old"])
        if writer == "seen_write":
            return seen_sources.write(["new"])
        if writer == "pipx_install_lock":
            with upgrade._pipx_install_lock():
                return upgrade._install_is_running()
        if writer == "update_log":
            return upgrade._write_update_log(["pipx", "upgrade", "fixture"], "fixture output")
        if writer == "acquire_lock":
            lockfile.acquire_lock()
            lockfile.release_lock()
            return None
        if writer == "devices_write_lock":
            with devices._devices_write_lock():
                return None
        if writer == "save_config":
            return config.save_config({"device": {"id": "fixture"}}, path)
        if writer == "events_bootstrap":
            return config._bootstrap_mm_events_path(str(path), strict=True)
        if writer == "sidecar_write":
            return sidecar.write({"device_id": "fixture", "sources": {}, "tombstones": {}})
        if writer == "attemptlog_write":
            return attemptlog.write(attemptlog.CaptureOutcome(kind="no-row"))
        if writer == "inversion_marker":
            return resolveflow._ensure_inversion_marker()
        raise AssertionError(writer)

    return path, invoke


def _tree_bytes_modes(root):
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (
            stat.S_IMODE(path.stat().st_mode),
            path.read_bytes() if path.is_file() else None,
        )
        for path in (root, *root.rglob("*"))
    }


@pytest.fixture(params=(False, True), ids=("missing", "existing"))
def raw_writer_exists(request):
    return request.param


@pytest.fixture
def raw_writer_refusal(
    synthetic_state_root, synthetic_account_home, monkeypatch, tmp_path, forbid_fs_mutations
):
    def check(writer, exists):
        path, invoke = _raw_writer_case(writer, synthetic_state_root, monkeypatch, tmp_path, exists)
        _assert_synthetic_destination(path, synthetic_account_home)
        before = _tree_bytes_modes(synthetic_state_root)
        calls = forbid_fs_mutations()
        with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
            invoke()
        _assert_guard_diagnostic(caught.value, path, "protected account-state path")
        assert calls == [], f"denied mutation attempts: {calls!r}"
        assert _tree_bytes_modes(synthetic_state_root) == before

    return check


@pytest.fixture
def raw_writer_control(tmp_path, monkeypatch):
    # The real pwd account stays guarded: an unisolated real-root write refuses.
    monkeypatch.setenv("HOME", str(tmp_path / "control-home"))

    def check(writer, exists):
        path, invoke = _raw_writer_case(writer, tmp_path / "safe", monkeypatch, tmp_path, exists)
        if writer == "local_put_exclusive" and exists:
            with pytest.raises(StorageError, match="already exists"):
                invoke()
            assert path.read_bytes() == b"keep"
        elif writer == "inversion_marker" and exists:
            assert invoke() is None  # invalid existing marker: native read/parse no-op
            assert path.read_bytes() == b"keep"
        else:
            result = invoke()
            if writer in ("local_delete", "migration_unlink"):
                assert not path.exists()
            elif writer == "local_delete_conflict_copies":
                assert result == (2 if exists else 0)
                assert not list(path.parent.glob("sentinel *"))
            elif writer in ("init_storage", "events_bootstrap"):
                assert path.is_dir()
                if writer == "init_storage":
                    assert (path / "mm-crypto-init").is_file()
                else:
                    assert stat.S_IMODE(path.stat().st_mode) == 0o700
            else:
                assert path.is_file()
                if writer == "pull_apply" and exists:
                    assert result == "conflicted" and path.read_bytes() == b"keep"
                    copies = list(path.parent.glob("*.sync-conflict-*"))
                    assert len(copies) == 1 and copies[0].read_bytes() == b"new"
                elif writer in ("local_put", "local_put_exclusive", "pull_apply"):
                    assert path.read_bytes() == b"new"
                elif writer == "log_unexpected":
                    assert b"RuntimeError: original incident" in path.read_bytes()
                elif writer == "update_log":
                    assert path.read_text().endswith("fixture output")
                elif writer.startswith("seen_"):
                    assert "new" in json.loads(path.read_bytes())
                    assert stat.S_IMODE(path.stat().st_mode) == 0o600
                elif writer == "migration_write":
                    assert json.loads(path.read_bytes())["missing_sources"] == ["custom"]
                elif writer in ("sidecar_write", "attemptlog_write", "save_config"):
                    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    return check


def test_log_unexpected_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("log_unexpected", raw_writer_exists)


def test_log_unexpected_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("log_unexpected", raw_writer_exists)


def test_migration_write_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("migration_write", raw_writer_exists)


def test_migration_write_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("migration_write", raw_writer_exists)


def test_migration_unlink_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("migration_unlink", raw_writer_exists)


def test_migration_unlink_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("migration_unlink", raw_writer_exists)


def test_pull_apply_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("pull_apply", raw_writer_exists)


def test_pull_apply_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("pull_apply", raw_writer_exists)


def test_init_storage_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("init_storage", raw_writer_exists)


def test_init_storage_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("init_storage", raw_writer_exists)


def test_local_put_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("local_put", raw_writer_exists)


def test_local_put_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("local_put", raw_writer_exists)


def test_local_put_exclusive_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("local_put_exclusive", raw_writer_exists)


def test_local_put_exclusive_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("local_put_exclusive", raw_writer_exists)


def test_local_delete_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("local_delete", raw_writer_exists)


def test_local_delete_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("local_delete", raw_writer_exists)


def test_local_delete_conflict_copies_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("local_delete_conflict_copies", raw_writer_exists)


def test_local_delete_conflict_copies_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("local_delete_conflict_copies", raw_writer_exists)


def test_seen_read_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("seen_read", raw_writer_exists)


def test_seen_read_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("seen_read", raw_writer_exists)


def test_seen_acknowledge_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("seen_acknowledge", raw_writer_exists)


def test_seen_acknowledge_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("seen_acknowledge", raw_writer_exists)


def test_seen_write_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("seen_write", raw_writer_exists)


def test_seen_write_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("seen_write", raw_writer_exists)


def test_pipx_install_lock_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("pipx_install_lock", raw_writer_exists)


def test_pipx_install_lock_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("pipx_install_lock", raw_writer_exists)


def test_update_log_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("update_log", raw_writer_exists)


def test_update_log_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("update_log", raw_writer_exists)


def test_spawn_pipx_refuses_protected_root(synthetic_account_home, monkeypatch, tmp_path):
    """test_self_update disables the pipx guard; the log-path guard must still refuse."""
    from mind_meld import upgrade

    monkeypatch.setattr(upgrade, "_refuse_under_pytest", lambda: None)
    monkeypatch.setattr(upgrade, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(upgrade.subprocess, "Popen", lambda *a, **kw: pytest.fail("spawned"))
    root = synthetic_account_home / ".config/mind-meld"
    log = root / upgrade.UPDATE_LOG_NAME
    _assert_synthetic_destination(log, synthetic_account_home)
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        upgrade._spawn_pipx(["pipx", "upgrade", "fixture"], log)
    _assert_guard_diagnostic(caught.value, log, "protected account-state path")
    assert not root.exists()


def test_acquire_lock_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("acquire_lock", raw_writer_exists)


def test_acquire_lock_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("acquire_lock", raw_writer_exists)


def test_devices_write_lock_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("devices_write_lock", raw_writer_exists)


def test_devices_write_lock_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("devices_write_lock", raw_writer_exists)


def test_save_config_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("save_config", raw_writer_exists)


def test_save_config_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("save_config", raw_writer_exists)


def test_events_bootstrap_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("events_bootstrap", raw_writer_exists)


def test_events_bootstrap_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("events_bootstrap", raw_writer_exists)


def test_sidecar_write_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("sidecar_write", raw_writer_exists)


def test_sidecar_write_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("sidecar_write", raw_writer_exists)


def test_attemptlog_write_refuses_protected_root(raw_writer_refusal, raw_writer_exists):
    raw_writer_refusal("attemptlog_write", raw_writer_exists)


def test_attemptlog_write_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("attemptlog_write", raw_writer_exists)


def test_inversion_marker_refuses_protected_root(raw_writer_refusal):
    raw_writer_refusal("inversion_marker", False)


def test_inversion_marker_tmp_control(raw_writer_control, raw_writer_exists):
    raw_writer_control("inversion_marker", raw_writer_exists)


def test_seen_sources_read_hit_and_inversion_marker_are_unguarded(
    synthetic_account_home, monkeypatch, forbid_fs_mutations
):
    from mind_meld import resolveflow, seen_sources, sidecar

    root = synthetic_account_home / ".config/mind-meld"
    root.mkdir(parents=True)
    monkeypatch.setattr(seen_sources, "SEEN_DIR", root)
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", root)
    seen_sources.seen_path().write_text('["existing"]')
    resolveflow._inversion_marker_path().write_text("123.0\n")
    monkeypatch.setattr(
        fsutil,
        "refuse_real_state_write_under_pytest",
        lambda path: pytest.fail("pure read called guard"),
    )
    calls = forbid_fs_mutations()
    assert seen_sources.read(["new"]) == {"existing"}
    assert resolveflow._ensure_inversion_marker() == 123.0
    assert calls == []


@pytest.mark.no_mm_events_isolation
def test_events_bootstrap_0700_and_inspection_are_unguarded(
    synthetic_account_home, monkeypatch, forbid_fs_mutations
):
    from mind_meld import config, upgrade

    root = synthetic_account_home / ".local/share/mind-meld"
    root.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(Path, "home", lambda: synthetic_account_home)
    monkeypatch.setattr(upgrade, "CACHE_DIR", synthetic_account_home / ".config/mind-meld")
    monkeypatch.setattr(
        fsutil,
        "refuse_real_state_write_under_pytest",
        lambda path: pytest.fail("pure read called guard"),
    )
    calls = forbid_fs_mutations()
    assert config._bootstrap_mm_events_path(str(root)) == []
    assert not upgrade._install_is_running()
    assert calls == []


def test_log_unexpected_refuses_incident_through_auto_command_setup(
    synthetic_account_home, monkeypatch, tmp_path, capsys, forbid_fs_mutations
):
    from mind_meld import cli, config, lockfile, sidecar

    root = synthetic_account_home / ".config/mind-meld"
    target = root / "autopull.log"
    root.mkdir(parents=True, mode=0o750)
    target.write_bytes(b"original log\n")
    target.chmod(0o640)
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", root)
    config_path = tmp_path / "local-config.toml"
    config_path.write_text("")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    original = RuntimeError("2026-10-08 original crypto incident")

    def crash():
        raise original

    monkeypatch.setattr(cli, "load_config", crash)
    _assert_synthetic_destination(target, synthetic_account_home)
    before = _tree_bytes_modes(root)
    held_before = dict(lockfile._LOCK_FDS)
    calls = forbid_fs_mutations()
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        with cli._auto_command_scope("pull", typer_exit_outcome="fleet-refused"):
            pytest.fail("setup should refuse before yielding")
    _assert_guard_diagnostic(caught.value, target, "protected account-state path")
    assert caught.value.__context__ is original
    assert "unexpected config error" in capsys.readouterr().err
    assert calls == [] and _tree_bytes_modes(root) == before
    assert not cli._autorun_breadcrumb_path().exists()
    assert lockfile._LOCK_FDS == held_before


def test_log_unexpected_refusal_releases_held_auto_command_lock(
    synthetic_account_home, monkeypatch, tmp_path, forbid_fs_mutations
):
    from mind_meld import cli, lockfile, sidecar

    root = synthetic_account_home / ".config/mind-meld"
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", root)
    monkeypatch.setattr(cli, "_auto_command_setup", lambda verb: SimpleNamespace(config={}))
    monkeypatch.setattr(cli, "_write_migration_breadcrumb", lambda missing: None)
    monkeypatch.setattr(cli, "_config_missing_recommended_excludes", lambda config: [])
    lock_path = tmp_path / "safe-lock"
    monkeypatch.setattr(lockfile, "LOCK_PATH", lock_path)
    original = RuntimeError("core failure")
    with pytest.raises(fsutil.PytestRealStateWriteError) as caught:
        with cli._auto_command_scope("pull", typer_exit_outcome="fleet-refused"):
            held_fd = lockfile._LOCK_FDS[lockfile._resolve_key(lock_path)]
            calls = forbid_fs_mutations()
            raise original
    assert caught.value.__context__ is original
    assert lockfile._resolve_key(lock_path) not in lockfile._LOCK_FDS and calls == []
    with pytest.raises(OSError) as closed:
        os.fstat(held_fd)
    assert closed.value.errno == errno.EBADF
    assert not (root / "last-autorun.json").exists()


@pytest.mark.no_mm_events_isolation
def test_events_bootstrap_inspection_missing_root_is_unguarded(
    synthetic_account_home, monkeypatch, forbid_fs_mutations
):
    from mind_meld import config

    root = synthetic_account_home / ".local/share/mind-meld"
    monkeypatch.setattr(Path, "home", lambda: synthetic_account_home)
    monkeypatch.setattr(
        fsutil,
        "refuse_real_state_write_under_pytest",
        lambda path: pytest.fail("inspection called guard"),
    )
    calls = forbid_fs_mutations()
    source = {"name": "mm-events", "path": str(root), "type": "generic", "include_dirs": ["events"]}
    resolution = config.resolve_sources({"sync": {"sources": [source]}}, bootstrap=False)
    assert resolution.would_create == ("mm-events",)
    assert calls == [] and not root.exists()
