"""seen-sources.json tracker — per-machine record of source names that
have been presented to the user.

Lives at ~/.config/mind-meld/seen-sources.json, mode 0600. Plain JSON:

    ["claude", "gstack"]

Used by `mm status` to surface "New source available: X" hints when
DEFAULT_SOURCES grows (e.g. codex ships in v0.11+) and the user hasn't
yet enabled or disabled X.

Migration invariant (load-bearing): on first post-v0.10.0 read the file
is missing. We atomically initialize it with the names of all currently-
resolved sources passed by the caller. Without this, every upgrader's
next `mm status` would surface spurious "New source: claude!" /
"New source: gstack!" hints for sources they are already syncing.
Pinned by `test_seen_sources_initialized_to_existing_on_upgrade`.

Concurrency: every read acquires `fcntl.flock(LOCK_EX)` on the file fd.
LOCK_EX (not LOCK_SH) because read may write (lazy init / corrupt
recovery). Concurrent callers either all see the same final content or
serialize behind the first writer. The os.replace inside
`atomic_write_bytes` swaps the inode under the lock — a second caller
holding an fd to the OLD inode will see size==0 and re-init, but with
the same caller-passed `initial`, so the rewrite is idempotent.

Failures degrade with a stderr breadcrumb (visible-failure contract):
corrupt JSON or write errors warn but never crash the calling command.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from mind_meld import fsutil
from mind_meld.errors import StorageError

SEEN_DIR = Path.home() / ".config" / "mind-meld"
SEEN_PATH = SEEN_DIR / "seen-sources.json"


def seen_path() -> Path:
    """Canonical location, resolved at call time so tests that
    monkeypatch `SEEN_DIR` get full isolation (mirrors pullhistory.py).
    """
    return SEEN_DIR / "seen-sources.json"


def read(initial: Iterable[str]) -> set[str]:
    """Return the set of source names already acknowledged on this machine.

    Lazy first-init invariant: if the file is missing or empty, atomically
    seed with `initial` (typically the names of currently-resolved sources
    at first call) and return that set. Prevents spurious "new source!"
    hints for already-shipped sources on first post-upgrade run.

    Corrupt JSON or wrong shape: stderr warn + reset to `initial`. The
    user just loses per-machine "seen" history; worst case is one round
    of redundant new-source hints.

    flock guards both the missing-file branch and the corrupt-recovery
    branch. Concurrent callers serialize.
    """
    initial_set = set(initial)
    path = seen_path()

    try:
        SEEN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Cannot mkdir the config dir — degrade gracefully. Caller's
        # diff degrades to "every default appears new" each run, but no
        # command crashes.
        return initial_set

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            try:
                size = os.fstat(fd).st_size
            except OSError:
                size = 0
            if size == 0:
                _seed_in_place_under_lock(fd, initial_set)
                return initial_set

            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, size)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                print(
                    f"mm: warning: seen-sources.json corrupt ({e}); "
                    f"resetting to currently-resolved sources",
                    file=sys.stderr,
                )
                _seed_in_place_under_lock(fd, initial_set)
                return initial_set

            if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
                print(
                    "mm: warning: seen-sources.json malformed "
                    "(expected list[str]); resetting to currently-resolved sources",
                    file=sys.stderr,
                )
                _seed_in_place_under_lock(fd, initial_set)
                return initial_set

            return set(parsed)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def acknowledge(names: Iterable[str], *, initial: Iterable[str]) -> set[str]:
    """Add `names` to the seen set under a single flock-guarded
    read-modify-write. Returns the new seen set.

    Codex 2026-04-25 caught the read-modify-write race: a caller doing
    `seen = read(...); seen.add(name); write(seen)` has an unprotected
    window between read's flock release and write's open. Two concurrent
    `mm enable-source X` invocations could lose one acknowledgment.
    `acknowledge` holds the flock across the whole RMW so concurrent
    callers serialize cleanly.

    Behaves like `read(initial)` for the missing-file branch (lazy seed),
    then unions `names` and atomically rewrites the file.
    """
    new_names = set(names)
    initial_set = set(initial)
    path = seen_path()

    try:
        SEEN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Cannot mkdir — return what we'd-have without persisting. Caller's
        # next read sees the missing-file path; tracker degrades gracefully.
        return initial_set | new_names

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            try:
                size = os.fstat(fd).st_size
            except OSError:
                size = 0
            if size == 0:
                current = initial_set
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, size)
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                        current = set(parsed)
                    else:
                        current = initial_set
                except json.JSONDecodeError:
                    current = initial_set

            updated = current | new_names
            payload = (json.dumps(sorted(updated)) + "\n").encode("utf-8")
            # In-place write under the flock — atomic_write_bytes uses
            # mkstemp + os.replace, which swaps the inode out from under
            # any concurrent fd holding the flock and breaks RMW
            # serialization. Since the file is tiny (< 1KB) and we already
            # tolerate corrupt JSON in the read path, single-syscall
            # in-place writes are the correct trade.
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, payload)
                os.ftruncate(fd, len(payload))
            except OSError as e:
                print(
                    f"mm: warning: failed to write seen-sources.json ({e}); "
                    f"new-source hints may repeat next run",
                    file=sys.stderr,
                )
            return updated
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write(seen: Iterable[str]) -> None:
    """Atomically write the seen set. Called after enable / disable /
    reconfigure to record that the user has now acknowledged a name.

    Sorted on disk for deterministic output / stable diffs in tests.

    fsync=False: non-load-bearing tracker. Crash-window loss at worst
    means one "new source!" hint shown twice. The config.toml mutation
    via patch_config_on_disk is the actual durability point.

    Race-prone with concurrent callers — prefer `acknowledge()` for
    enable/disable/reconfigure paths. `write()` remains for full-set
    overwrites (e.g. tests) where the caller already serializes externally.
    """
    seen_list = sorted(set(seen))
    data = (json.dumps(seen_list) + "\n").encode("utf-8")
    # Catch BOTH OSError (mkdir failure) AND StorageError (atomic_write_bytes
    # wraps OSError as StorageError). Codex 2026-04-25 caught this: without
    # the StorageError catch, a disk-full / permission flip / bad temp dir
    # would crash mm status / enable-source / disable-source through a
    # non-load-bearing tracker.
    try:
        SEEN_DIR.mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write_bytes(seen_path(), data, fsync=False, mode=0o600)
    except (OSError, StorageError) as e:
        print(
            f"mm: warning: failed to write seen-sources.json ({e}); "
            f"new-source hints may repeat next run",
            file=sys.stderr,
        )


def _seed_in_place_under_lock(fd: int, initial: set[str]) -> None:
    """Write the initial seed in place on `fd`. Caller holds the flock.

    Single-syscall in-place write (no inode swap). Earlier versions used
    `fsutil.atomic_write_bytes` here, but mkstemp + os.replace swaps the
    inode out from under any concurrent fd holding the flock, breaking
    RMW serialization for `acknowledge()`. Since the file is tiny (< 1KB)
    and the read path already tolerates corrupt JSON, in-place writes are
    the correct trade.
    """
    payload = (json.dumps(sorted(initial)) + "\n").encode("utf-8")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.ftruncate(fd, len(payload))
    except OSError as e:
        print(
            f"mm: warning: failed to seed seen-sources.json ({e})",
            file=sys.stderr,
        )


def compute_new_sources(
    seen: set[str],
    default_names: list[str],
    disabled: list[str],
    explicit_names: list[str],
) -> list[str]:
    """Return DEFAULT_SOURCES names the user has not yet acknowledged.

    Order matches `default_names` (canonical surfacing order: claude
    before gstack before any future codex). Excludes:
      - names in `seen`     (already acknowledged via init seed or
                             enable/disable/reconfigure)
      - names in `disabled` (explicitly opted out; double-filter for safety)
      - names in `explicit_names` (already in user's [[sync.sources]] —
                                   already syncing, no hint needed)

    Used by `mm status` to surface "New source available: X" once.
    """
    disabled_set = set(disabled)
    explicit_set = set(explicit_names)
    return [
        name
        for name in default_names
        if name not in seen and name not in disabled_set and name not in explicit_set
    ]
