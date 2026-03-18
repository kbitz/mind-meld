"""PID-based lockfile for MemSync concurrency safety.

Prevents concurrent push/pull/gc operations on the same device.
Stale locks (PID no longer running) are cleaned up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

from memsync.config import LOCK_PATH
from memsync.errors import LockError


def _pid_is_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it
        return True


def acquire_lock(path: Path | None = None) -> None:
    """Acquire the lockfile. Raises LockError if held by a live process."""
    lock_path = path or LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            stored_pid = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            # Corrupt lock file — overwrite it
            stored_pid = -1

        if stored_pid > 0 and _pid_is_running(stored_pid):
            raise LockError(
                f"Another msync operation is running (PID {stored_pid}). "
                f"Wait for it to finish or remove {lock_path}"
            )
        # Stale lock — clean it up

    lock_path.write_text(str(os.getpid()))


def release_lock(path: Path | None = None) -> None:
    """Release the lockfile."""
    lock_path = path or LOCK_PATH
    if lock_path.exists():
        try:
            stored_pid = int(lock_path.read_text().strip())
            if stored_pid == os.getpid():
                lock_path.unlink(missing_ok=True)
        except (ValueError, OSError):
            lock_path.unlink(missing_ok=True)
