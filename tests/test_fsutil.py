"""Tests for mind_meld.fsutil — atomic write + directory fsync primitives."""

from __future__ import annotations

import errno
import os
import stat

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

    def test_darwin_fullfsync_enotsup_falls_back_to_fsync(self, tmp_path, monkeypatch):
        """On Darwin, F_FULLFSYNC returning ENOTSUP must fall back to os.fsync."""
        monkeypatch.setattr(fsutil, "_IS_DARWIN", True)
        fsync_calls: list[int] = []

        def unsupported_fcntl(fd, op, *args):
            raise OSError(errno.ENOTSUP, "not supported")

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

    def test_empty_lines_no_op(self, tmp_path):
        path = tmp_path / "log.jsonl"
        fsutil.flock_append_jsonl(path, [])
        assert not path.exists(), "empty input must not create the file"

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
