"""Atomic file-write + directory-fsync primitives.

Unifies the "mkstemp → write → fsync → os.replace → fsync parent" pattern
shared by sidecar.py, storage/local.py, config.py, synclog.py, and the
pull-apply paths in cli.py. Two invariants matter:

  1. On any failure (write, fsync, replace), the tmp file is unlinked
     before we raise. No orphan tmp*.tmp ever remains.
  2. When fsync=True, durability means BOTH the file contents AND the
     directory entry pointing at the file have been flushed to physical
     media. On macOS we use F_FULLFSYNC (Apple's documented primitive
     per fsync(2)); plain fsync(2) on Darwin only pushes to the disk
     controller, not through the disk cache. On non-Darwin (or when
     F_FULLFSYNC is not supported for a given fd), we fall back to
     os.fsync. FATAL on any fsync failure — "write succeeded but rename
     isn't durable" is silent data loss on crash.

fsync only guarantees LOCAL crash durability. It does not imply iCloud
upload, peer visibility, or protection from iCloud conflict generation.
iCloud sync is a separate concurrency boundary, handled elsewhere.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from mind_meld.errors import StorageError

_IS_DARWIN = sys.platform == "darwin"


def _default_new_file_mode() -> int:
    """Compute (0o666 & ~umask) — the mode Path.write_bytes() would use
    for a new file. Matches conventional Unix behavior so mm-written
    user-visible files (pull-applied session data, sync logs) inherit
    the same permissions as if the user's editor created them.
    """
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _fsync_fd(fd: int) -> None:
    """Durably flush fd to physical media.

    On Darwin, prefer fcntl(fd, F_FULLFSYNC) — plain fsync on macOS only
    pushes to the disk controller, not through the disk cache. Falls
    back to os.fsync when F_FULLFSYNC is not supported (e.g., on some
    directory fds) or on non-Darwin platforms.

    Raises OSError on real I/O failures; callers translate to StorageError.
    """
    if _IS_DARWIN:
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError as e:
            # Fall back only on "unsupported" errors. Real I/O errors propagate.
            if e.errno not in (errno.ENOTSUP, errno.EINVAL, errno.EOPNOTSUPP):
                raise
    os.fsync(fd)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    fsync: bool = False,
    mode: int | None = None,
) -> None:
    """Atomically write `data` to `path` via mkstemp + os.replace.

    Guarantees:
      - On success: `path` contains `data`. If fsync=True, both the file
        contents and the parent directory entry are durably flushed.
      - On any failure: the target is untouched if it existed, and no
        stale tmp file remains in the parent directory.

    Args:
        path: target file path. The parent directory must already exist.
        data: bytes to write.
        fsync: if True, flush file and parent directory durably before
               returning. Defaults to False (fast, crash-atomicity only:
               rename is atomic, but the rename is not durable against
               kernel crash or power loss without fsync).
        mode: explicit file permission bits (e.g., 0o600 for secrets,
              0o644 for user-visible text). If None (default):
                - If the target exists, preserves its current mode.
                - If the target is new, uses (0o666 & ~umask), matching
                  Path.write_bytes() behavior.
              The default exists because `tempfile.mkstemp` creates
              tmp files with 0o600 unconditionally, and `os.replace`
              preserves the SOURCE mode — which would silently
              downgrade every user-visible file this helper writes.

    Raises:
        StorageError: wrapping any OSError encountered. Tmp file is
        cleaned up before raising.
    """
    parent = path.parent

    # Resolve effective mode BEFORE opening the tmp — if the target
    # exists and we're preserving, we need its mode captured here.
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = _default_new_file_mode()

    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if fsync:
                f.flush()
                _fsync_fd(f.fileno())

        # chmod BEFORE the rename so the target atomically appears with
        # the correct mode (no window where a reader could see 0o600).
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        tmp_name = None  # consumed by replace; no longer ours to unlink

        if fsync:
            fsync_dir(parent)

    except OSError as e:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise StorageError(f"storage: atomic_write_bytes({path}) — {e}") from e


class AppendSizeLimit(ValueError):
    """The complete JSONL batch would cross the caller's file-size ceiling."""


def flock_append_jsonl(
    path: Path,
    lines: Iterable[bytes],
    *,
    mode: int = 0o600,
    on_locked: Callable[[int], None] | None = None,
    strict: bool = False,
    max_bytes: int | None = None,
) -> None:
    """Append N JSONL rows to `path` atomically under fcntl.flock(LOCK_EX).

    Each element of `lines` is one JSON-encoded row WITHOUT a trailing newline;
    the helper appends a single `\\n` after each row and separates an
    unterminated prior row before appending. All N rows share one
    flock window — best-effort batching, NOT transactionality (a crash mid-batch
    leaves a prefix of the rows on disk).

    Contract:
      - Parent dir created with parents=True (mode 0o700) if missing.
      - File created with O_APPEND|O_EXCL|O_NOFOLLOW only when the batch will
        fit; existing paths are opened O_NOFOLLOW and must be regular files.
      - LOCK_EX is BLOCKING (no LOCK_NB / no retry budget). mm.lockfile already
        serializes push-vs-push at the higher layer; cross-process contention
        on this helper is rare in practice.
      - Best-effort: OSError swallowed silently. Callers are forensic logs
        (pullhistory, mm-events), not data integrity — a crashed FS or
        permission flip MUST NOT break the calling sync.
        Attended host captures opt into ``strict``: write errors and
        short appends raise OSError. Existing forensic callers are unchanged.
      - `max_bytes` checks the whole batch, including any separator, under
        flock; `AppendSizeLimit` reports a skipped append without changing
        bytes and without creating a missing file.
      - `on_locked(fd)` runs under flock AFTER the writes complete; pullhistory
        uses this for its line-boundary rotation closure. Exceptions raised
        from the callback are swallowed (same forensic-only stance).
    """
    payload = b"".join(line + b"\n" for line in lines)
    if not payload:
        return  # nothing to write; avoid creating the file

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags, mode)
        except FileNotFoundError:
            if max_bytes is not None and len(payload) > max_bytes:
                raise AppendSizeLimit("JSONL batch exceeds the file-size ceiling")
            try:
                fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, mode)
            except FileExistsError:
                fd = os.open(str(path), flags, mode)
        try:
            # Refuse a device or pipe before fchmod can rewrite its mode.
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "JSONL append target is not a regular file")
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass  # fchmod can fail on some filesystems; perms are best-effort
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                start = os.fstat(fd).st_size
                if start and os.pread(fd, 1, start - 1) != b"\n":
                    payload = b"\n" + payload
                # Decide under the same flock as the append, including the
                # separator byte. A concurrent writer cannot invalidate it. A file
                # created above and still empty already passed the pre-check.
                if max_bytes is not None and start + len(payload) > max_bytes:
                    raise AppendSizeLimit("JSONL batch exceeds the file-size ceiling")

                def _restore_prefix() -> None:
                    if not strict:
                        return
                    try:
                        os.ftruncate(fd, start)
                    except OSError:
                        pass

                try:
                    written = os.write(fd, payload)
                except OSError:
                    _restore_prefix()
                    raise
                if strict and written != len(payload):
                    _restore_prefix()
                    raise OSError(f"short JSONL append: wrote {written} of {len(payload)} bytes")
                if on_locked is not None:
                    try:
                        on_locked(fd)
                    except Exception:
                        pass  # forensic-only; never break the calling sync
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except OSError:
        if strict:
            raise
        return  # forensic aid only; never block the calling sync


def append_rotatable_jsonl(
    path: Path, line: bytes, *, max_bytes: int, mode: int = 0o600, attempts: int = 8
) -> None:
    """Durably append one JSONL row to a spool that a reader rotates by rename.

    ``flock_append_jsonl`` locks a file that is never renamed. Here a reader may
    rename the spool aside while this writer waits for the lock, so the row is
    written only once the flock confirms ``path`` still names the locked inode;
    otherwise the writer reopens. A row can therefore never land in a file the
    reader already took. Errors raise (``AppendSizeLimit`` for the ceiling):
    the caller holds the only copy of the row.
    """
    payload = line + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for _ in range(attempts):
        fd = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, mode)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "spool is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                current = os.lstat(path)
            except FileNotFoundError:
                continue  # rotated away; the next open creates a fresh spool
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                continue
            start = os.fstat(fd).st_size
            data = payload
            if start and os.pread(fd, 1, start - 1) != b"\n":
                data = b"\n" + payload  # isolate a torn earlier row
            if start + len(data) > max_bytes:
                raise AppendSizeLimit("spool exceeds its size ceiling")
            if os.write(fd, data) != len(data):
                os.ftruncate(fd, start)
                raise OSError("short spool append")
            _fsync_fd(fd)
            # Every append: an earlier writer may have died before binding the
            # spool's directory entry, and this caller is about to report success.
            fsync_dir(path.parent)
            return
        finally:
            os.close(fd)
    raise OSError(errno.EAGAIN, "spool kept rotating during the append")


def rotate_jsonl(
    path: Path,
    destination: Path,
    *,
    retry_intervals: Sequence[float] = (0.01, 0.05, 0.1),
    deadline: float | None = None,
) -> bool:
    """Rename a spool aside once no ``append_rotatable_jsonl`` holds it.

    Returns False when there is nothing to rotate or a writer still holds the
    lock after the short retries (rotate on a later pass). Non-blocking, so a
    stuck writer can never wedge the caller; no retry sleeps past ``deadline``
    (a ``time.monotonic()`` value).
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "spool is not a regular file")
        for delay in (0.0, *retry_intervals):
            if delay and deadline is not None and time.monotonic() + delay > deadline:
                return False
            time.sleep(delay)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                continue
        else:
            return False
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            return False
        os.rename(path, destination)
        fsync_dir(path.parent)
        return True
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Durably flush directory entries for `path` to physical media.

    After os.replace, the parent directory's new name→inode mapping lives
    in the kernel dcache. Without this call, a crash can roll back the
    rename or leave the directory with a stale entry. Used both internally
    by atomic_write_bytes(fsync=True) and externally by pull-apply
    end-of-batch durability (deferred-durability pattern: per-file writes
    skip fsync; one dir fsync at end of pull binds every rename in that
    directory).

    Raises:
        StorageError: wrapping any OSError. Directory fsync failure means
        recent renames into this directory may not survive a crash.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as e:
        raise StorageError(f"storage: fsync_dir open({path}) — {e}") from e
    try:
        _fsync_fd(fd)
    except OSError as e:
        raise StorageError(f"storage: fsync_dir fsync({path}) — {e}") from e
    finally:
        os.close(fd)
