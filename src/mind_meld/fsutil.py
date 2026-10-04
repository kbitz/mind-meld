"""Atomic file-write, directory-flush and flock-append primitives.

Atomic writes publish at successful os.replace. Before that boundary this
invocation preserves the target; afterward, with fsync=True, a parent-directory
error can leave complete new bytes visible with durability unconfirmed. Failed
return does not mean rollback. Owned-temp cleanup for caught OSError is
best-effort.

With fsync=True, normal return confirms file and parent flushes as reported
by the platform. Darwin prefers F_FULLFSYNC, falling back to os.fsync on
unsupported operations; other platforms use os.fsync. This does not certify
physical power-loss survival, iCloud upload, peer visibility or freedom from
cloud conflicts. fsync=False provides replacement visibility without fsync calls.

See docs/invariants/sync.md#atomic-write-publication-failures for the
"Atomic write publication failures" contract and caller recovery audit.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable
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
    """Request the platform's flush for fd; return when it reports success.

    Darwin prefers F_FULLFSYNC to request flushing through the disk cache.
    ENOTSUP, EINVAL or EOPNOTSUPP falls back to os.fsync (including unsupported
    directory fds); non-Darwin uses os.fsync directly. Other F_FULLFSYNC errors
    and os.fsync errors raise OSError. The fallback does not promise the same
    physical-media guarantee as a supported F_FULLFSYNC.
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
    """Publish `data` at the `path` pathname via mkstemp + os.replace.

    Guarantees:
      - Successful replacement makes complete new bytes visible at `path`.
        Before it, this invocation leaves an existing target's bytes intact
        or an absent target absent. Concurrent external writers are not isolated.
      - With fsync=True, parent-directory open/flush/close runs after
        replacement and can fail with new bytes already published and
        durability unconfirmed. No rollback. With fsync=False no fallible
        step follows replacement, so OSError/StorageError from this call
        means it did not replace (asynchronous interrupts excepted).
      - Caught OSError triggers best-effort unlink of only the owned temp;
        successful replacement consumes that name. Cleanup is not guaranteed
        across process death, unlink failure or arbitrary uncaught exceptions.

    See docs/invariants/sync.md#atomic-write-publication-failures for caller
    handlers and next readers; exception types/messages do not identify phase.

    Args:
        path: pathname to replace, including a symlink itself, not its referent.
              The parent directory must already exist.
        data: bytes to write.
        fsync: if True, flush the temp file before replacement and the parent
               afterward. Normal return confirms these platform-reported local
               flushes, not cloud delivery or certified power-loss survival.
               Defaults to False: no fsync calls or crash-persistence guarantee.
        mode: explicit file permission bits (e.g., 0o600 for secrets,
              0o644 for user-visible text). If None (default):
                - If the target exists, preserves its stat-observed mode
                  (stat follows a symlink's referent).
                - If the target is new, uses (0o666 & ~umask), matching
                  Path.write_bytes() behavior.
              The default exists because `tempfile.mkstemp` creates
              tmp files with 0o600 unconditionally, and `os.replace`
              preserves the SOURCE mode — which would silently
              downgrade every user-visible file this helper writes.

    Raises:
        StorageError: wraps OSError inside the temp-write/replacement try,
            including directory-close errors after publication. StorageError
            from fsync_dir propagates directly; it is not an OSError subclass.
        OSError: mode lookup errors other than FileNotFoundError occur before
            that try and propagate raw. Other uncaught exceptions also escape.
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


def fsync_dir(path: Path) -> None:
    """Request a platform flush of directory entries for `path`.

    atomic_write_bytes(fsync=True) calls this after publishing; pull-apply
    also uses it at batch end. It does not flush file contents or undo any
    rename. Normal return confirms _fsync_fd's platform-reported flush,
    including its unsupported-F_FULLFSYNC fallback, not cloud delivery.
    See docs/invariants/sync.md#atomic-write-publication-failures.

    Raises:
        StorageError: wraps OSError from opening or flushing the directory.
            Recent renames may be visible with durability unconfirmed.
        OSError: closing the descriptor in finally can fail, including after
            a successful flush, or mask a flush error. atomic_write_bytes
            wraps this close error; neither type nor prefix proves rollback.
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
