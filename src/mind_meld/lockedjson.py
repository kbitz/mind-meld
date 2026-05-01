"""Locked JSON read-modify-write helper for ~/.config/mind-meld/*.json caches.

Two existing call sites had near-identical implementations of "open a JSON
file, flock it, read, let caller mutate, write back": ``upgrade.py``'s
``upgrade-state.json`` and (forthcoming) ``token_usage.py``'s
``session-tokens.json``. This module extracts the shared shape into a
single context manager so both cases use the same retry budget, file-mode
policy, and corruption-recovery rules. ``devices-write.lock`` (sibling
lock-file pattern guarding multi-key R/M/W) does NOT use this helper —
its shape is fundamentally different and lifting it would obscure load-
bearing lock-order rules.

Lock-order rule (caller's responsibility): NEVER acquire the mm lockfile
while holding a ``locked_json_rmw`` context. Release the JSON's flock
BEFORE appending to ``pullhistory.jsonl`` or any other locked resource.
This helper cannot enforce that — it cannot see which other locks the
caller holds — but the rule applies anywhere this helper is used.

Three contention modes:

* ``"block"`` (default, matches ``upgrade.py``'s pre-extraction behavior):
  blocking ``LOCK_EX``. Two concurrent processes queue. ``is_locked`` is
  always ``True`` on yield.

* ``"raise"``: non-blocking ``LOCK_EX | LOCK_NB`` with a brief retry budget.
  On exhausted retries, raises ``LockContended``. Caller handles.

* ``"warn"`` (forensic-cache pattern from ``devices.py``): non-blocking
  with retry budget. On exhausted retries, emits one ``mm: warning:`` line
  to stderr, yields ``is_locked=False`` (and a fresh empty dict — caller
  must check the flag to skip work). NO write happens on context exit.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Sequence

_DEFAULT_RETRY_INTERVALS: tuple[float, ...] = (0.05, 0.1, 0.2, 0.4)
"""Same shape as ``devices._LOCK_RETRY_INTERVALS_S``. Total ~750ms before
giving up — well under any single-push budget."""


class LockContended(RuntimeError):
    """Raised by ``on_contention="raise"`` when the retry budget exhausts."""


@dataclass
class LockedJson:
    """Yielded by ``locked_json_rmw``. Caller mutates ``data``; the helper
    persists ``data`` on context exit iff ``is_locked`` is True.

    ``is_locked=False`` only happens under ``on_contention="warn"`` mode
    after retry budget exhaustion. In that case ``data`` is a fresh empty
    dict (the caller's mutations are intentionally discarded — the warning
    is the visible-failure signal)."""

    data: dict[str, Any]
    is_locked: bool


@contextmanager
def locked_json_rmw(
    path: Path,
    *,
    mode: int = 0o600,
    default_factory: Callable[[], dict[str, Any]] = dict,
    retry_intervals: Sequence[float] = _DEFAULT_RETRY_INTERVALS,
    on_contention: Literal["block", "raise", "warn"] = "block",
    contention_warning: str = "lock contended; skipping update",
) -> Iterator[LockedJson]:
    """Open ``path`` with the given ``mode``, flock it, parse JSON, yield
    a ``LockedJson`` for caller mutation, write back atomically on context
    exit.

    Parent directory is created (``parents=True, exist_ok=True``) if missing.

    Corrupt JSON / unreadable / non-dict top-level all degrade to
    ``default_factory()`` — these caches are forensic and recoverable;
    refusing to start would be worse than rebuilding.

    Atomicity: the truncate + write happens under the same flock. Any
    other process that respects this protocol (also flocks the same path)
    cannot observe a torn write. We do NOT use a temp-file-rename pattern
    because the flock already provides the atomicity guarantee, and the
    rename pattern would invalidate other holders' fd-keyed locks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, mode)
    is_locked = False
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass  # best-effort on filesystems without fchmod
        is_locked = _acquire_lock(
            fd,
            on_contention=on_contention,
            retry_intervals=retry_intervals,
            contention_warning=contention_warning,
        )
        if is_locked:
            data = _read_json(fd, default_factory)
        else:
            # "warn" path with exhausted retries — yield empty placeholder.
            # No write will happen at context exit because is_locked is False.
            data = default_factory()
        ljson = LockedJson(data=data, is_locked=is_locked)
        try:
            yield ljson
        except BaseException:
            # Caller raised: do NOT persist mutations. Re-raise.
            raise
        else:
            if is_locked:
                _write_json(fd, ljson.data)
    finally:
        if is_locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def _acquire_lock(
    fd: int,
    *,
    on_contention: Literal["block", "raise", "warn"],
    retry_intervals: Sequence[float],
    contention_warning: str,
) -> bool:
    """Return True if the flock was acquired, False if ``on_contention="warn"``
    fell through. Raises ``LockContended`` for ``"raise"`` exhaustion."""
    if on_contention == "block":
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return True
        except OSError:
            return False  # truly unrecoverable; treat as cold cache
    # Non-blocking variants.
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        for delay in retry_intervals:
            time.sleep(delay)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                continue
    if on_contention == "raise":
        raise LockContended(contention_warning)
    sys.stderr.write(f"mm: warning: {contention_warning}\n")
    return False


def _read_json(fd: int, default_factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64 * 1024 * 1024)
    except OSError:
        return default_factory()
    if not raw:
        return default_factory()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default_factory()
    if not isinstance(parsed, dict):
        return default_factory()
    return parsed


def _write_json(fd: int, data: dict[str, Any]) -> None:
    payload = json.dumps(data, sort_keys=True, indent=2).encode("utf-8")
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
    except OSError:
        # Forensic caches: a write failure is degradation, not a crash.
        # The visible-failure contract: caller can detect zero-mtime
        # advance on next read and surface it then.
        return


__all__ = [
    "LockContended",
    "LockedJson",
    "locked_json_rmw",
]
