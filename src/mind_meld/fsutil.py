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
