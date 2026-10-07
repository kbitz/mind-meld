"""Kernel-enforced lockfile for Mind Meld concurrency safety.

Prevents concurrent commands that change sync state or the installation,
plus the attended update tail, on the same device via fcntl.flock. The
lock is auto-released on process exit (or any fd close), so crashed
processes never strand the lock — no PID scanning, no stale-lock
detection needed.

The lockfile body carries the owning PID as a human-readable hint so
`LockError` can tell users "PID 12345 holds the lock". Correctness is
enforced by the kernel; PID is diagnostic only.

NOTE on forks: _LOCK_FDS is process-local module state. A subprocess
spawned via fork() (the default on Linux multiprocessing) will inherit
_LOCK_FDS entries referencing the parent's fds. Cross-process tests
must use `subprocess.Popen` (fork+exec) or import this module only
inside the child.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from mind_meld.config import LOCK_PATH
from mind_meld.errors import LockError

# Held locks keyed by resolved path string. One entry per path, one
# process holds at most one lock per path. Module-level by design:
# the kernel owns correctness; this is just a handle for release_lock.
_LOCK_FDS: dict[str, int] = {}


def _resolve_key(path: Path) -> str:
    """Canonical lookup key for `path`.

    Same physical lockfile reached via symlink, relative path, or
    absolute path MUST collide on the same key so a process cannot
    trick itself into acquiring the same lock twice. os.path.realpath
    resolves symlinks across the entire path (parent AND basename),
    whereas Path.resolve(strict=True) requires every component to
    exist. realpath handles missing terminal components gracefully.
    """
    return os.path.realpath(str(Path(path).expanduser()))


def _read_pid(path: Path) -> int | None:
    """Read the diagnostic PID hint from the lockfile body."""
    try:
        raw = path.read_text().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _contention_message(pid: int | None) -> str:
    """Text for a lock held by another process. The PID hint is diagnostic."""
    advice = (
        "Wait for it to finish, then run this command again. If that operation is "
        "waiting for input in another terminal, answer it there; if its output says "
        "it is updating mm, let it finish, which can take several minutes. Do not "
        "delete the mm lockfile or kill the process; the lock is released when it exits."
    )
    if isinstance(pid, int) and pid > 0:
        return (
            f"Another mm operation is running (PID {pid}). {advice} "
            f"To see the process, run:\n  ps -p {pid} -o etime=,tty=,command="
        )
    return f"Another mm operation is running (PID unknown). {advice}"


def acquire_lock(path: Path | None = None) -> None:
    """Acquire exclusive lock on `path` (default LOCK_PATH).

    Uses fcntl.flock for kernel-enforced, auto-released-on-process-exit
    locking. The lockfile body carries the owning PID for diagnostics
    only; correctness is enforced by the kernel.

    Raises:
        LockError: if another process holds the lock, if this process
        already holds a lock on the same resolved path, or on a
        filesystem error.
    """
    lock_path = path or LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = _resolve_key(lock_path)

    if key in _LOCK_FDS:
        raise LockError(f"This process (PID {os.getpid()}) already holds the lock at {lock_path}.")

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        raise LockError(f"Could not open lockfile {lock_path}: {e}") from e

    # Retry once on EINTR; any other failure releases fd and raises.
    for attempt in (1, 2):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except InterruptedError:
            if attempt == 2:
                os.close(fd)
                raise LockError(f"flock on {lock_path} interrupted repeatedly.")
            continue
        except BlockingIOError:
            # docs/invariants/auto-upgrade.md "The mm lockfile"
            pid = _read_pid(lock_path)
            os.close(fd)
            raise LockError(_contention_message(pid))
        except OSError as e:
            os.close(fd)
            raise LockError(f"flock on {lock_path} failed: {e}") from e

    # Write PID to body for diagnostics. If this fails, release the
    # flock cleanly so the kernel isn't holding a lock on a file whose
    # body says nothing about ownership.
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode())
    except OSError as e:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        raise LockError(f"flock acquired on {lock_path} but PID write failed: {e}") from e

    _LOCK_FDS[key] = fd


def release_lock(path: Path | None = None) -> None:
    """Release the lock on `path` (default LOCK_PATH). No-op if not held.

    Closing the fd implicitly releases the flock (kernel semantics); the
    explicit LOCK_UN is defensive.

    IMPORTANT: we do NOT unlink the lockfile here. Unlinking creates the
    classic advisory-lock race: after release but before unlink, process B
    opens the same path and flocks the live inode; we then unlink the path;
    process C opens with O_CREAT, gets a BRAND-NEW inode, flocks it, and
    now both B and C hold flocks on DIFFERENT inodes, both believing they
    own the lock. The lockfile body is diagnostic only — a stale body is
    harmless because the next acquire truncates it before writing the new
    PID.
    """
    lock_path = path or LOCK_PATH
    key = _resolve_key(lock_path)
    fd = _LOCK_FDS.pop(key, None)
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
