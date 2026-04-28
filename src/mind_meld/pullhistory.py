"""Pull/push history JSONL log for `mm log` query and audit trail.

Append-only at ~/.config/mind-meld/pull-history.jsonl, mode 0600. One JSON
object per line. Two row classes share the file:

  Pull/push row (per-file outcome — `append`):
    {
      "ts": "<ISO-8601 UTC>",
      "verb": "pull" | "push",
      "device": "<this device's id>",
      "source": "<source name>",
      "rel_path": "<file's relative path within source>",
      "action": "written"|"merged"|"skipped"|"conflicted"|"excluded"|
                "uploaded"|"failed",
      "local_sha": "<optional sha256 hex>",
      "remote_sha": "<optional sha256 hex>",
      "sidecar": "<optional sidecar filename if action=conflicted>"
    }

  Self-upgrade row (one per detected version transition — `append_self_upgrade`):
    {
      "ts": "<ISO-8601 UTC>",
      "verb": "self-upgrade",
      "device": "<this device's id>",
      "old_version": "<prior __version__>",
      "new_version": "<current __version__>"
    }

  Self-upgrade rows have NO source/rel_path/action — `mm log` table renderer
  detects verb=="self-upgrade" and renders an "extra" column ("OLD → NEW")
  instead of the source/path columns. Machine consumers of `--format jsonl`
  must tolerate the missing fields.

Concurrency: every append acquires `fcntl.flock(LOCK_EX)` on the file fd.
mm itself serializes push/pull/gc through the mm lockfile, but a user
running `mm log` in another shell can still read the file concurrently —
the flock guards against torn writes from a future code path that drops
the mm lock (e.g. background telemetry).

Rotation: when the live file passes `_ROTATE_BYTES`, atomically rename to
`<path>.1` (overwriting any prior `.1`). Rotation happens at LINE boundary
— i.e. AFTER the append that pushed us over the cap. We never byte-tail-
truncate. Reader (`mm log`) tolerates a partially-written first line in
`.1` because crash mid-rotate is observable here, even though our normal
write path is line-atomic.

Path/dir-creation failures degrade silently: history is a forensic aid,
not load-bearing — we MUST NOT crash a successful pull because the log
directory was wiped.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from mind_meld import fsutil

HISTORY_DIR = Path.home() / ".config" / "mind-meld"
HISTORY_PATH = HISTORY_DIR / "pull-history.jsonl"
ROTATED_SUFFIX = ".1"
_ROTATE_BYTES = 1_000_000  # 1MB cap; rotate at line boundary on next write

Action = Literal["written", "merged", "skipped", "conflicted", "excluded", "uploaded", "failed"]
Verb = Literal["pull", "push", "self-upgrade"]


def history_path() -> Path:
    """Canonical history file location. Resolved at call time so tests
    that monkeypatch `HISTORY_DIR` get full isolation (mirrors sidecar.py).
    """
    return HISTORY_DIR / "pull-history.jsonl"


def _rotated_path() -> Path:
    return history_path().with_name(history_path().name + ROTATED_SUFFIX)


def append(
    *,
    verb: Verb,
    device: str,
    source: str,
    rel_path: str,
    action: Action,
    local_sha: str | None = None,
    remote_sha: str | None = None,
    sidecar: str | None = None,
    ts: datetime | None = None,
) -> None:
    """Append one pull/push record to pull-history.jsonl. Best-effort.

    Failures are swallowed: history is a forensic aid, not data integrity.
    A crashed FS / permission flip / disk-full must not break the calling
    pull or push.
    """
    payload: dict[str, Any] = {
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "verb": verb,
        "device": device,
        "source": source,
        "rel_path": rel_path,
        "action": action,
    }
    if local_sha is not None:
        payload["local_sha"] = local_sha
    if remote_sha is not None:
        payload["remote_sha"] = remote_sha
    if sidecar is not None:
        payload["sidecar"] = sidecar

    _append_payload(payload)


def append_self_upgrade(
    *,
    device: str,
    old_version: str,
    new_version: str,
    ts: datetime | None = None,
) -> None:
    """Append one self-upgrade transition record. Best-effort.

    Distinct from `append` because self-upgrade rows have NO
    source/rel_path/action — they're a different event class. Contract is
    enforced inline as silent skip (NOT assertion): if old_version or
    new_version is empty, drop the row rather than raise. Codex outside
    voice flagged that assertions disable under `python -O` and would
    crash callers — neither acceptable for a forensic-only log.
    """
    if not old_version or not new_version:
        return  # silent skip on contract violation
    payload: dict[str, Any] = {
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "verb": "self-upgrade",
        "device": device,
        "old_version": old_version,
        "new_version": new_version,
    }
    _append_payload(payload)


def _append_payload(payload: dict[str, Any]) -> None:
    """Internal: write one JSONL row under flock, rotate at line boundary if
    over cap. Shared by `append` (pull/push rows) and `append_self_upgrade`
    (transition rows) so flock + rotation logic stays single-sourced.

    Routes through `fsutil.flock_append_jsonl` so the flock+chmod+append
    plumbing is shared with `events.write_push_event`. Rotation lives here
    as an `on_locked` closure — the helper stays unaware of pullhistory's
    rotation semantics.
    """
    line = json.dumps(payload, sort_keys=True).encode("utf-8")
    path = history_path()

    def _maybe_rotate(fd: int) -> None:
        # Rotate AFTER the write so we never truncate mid-line. Re-stat
        # under the lock so a racing rotate from another process is visible
        # (we'd just no-op the rename below).
        try:
            size = os.fstat(fd).st_size
        except OSError:
            size = 0
        if size > _ROTATE_BYTES:
            _rotate_under_lock(path)

    fsutil.flock_append_jsonl(path, [line], mode=0o600, on_locked=_maybe_rotate)


def _rotate_under_lock(live_path: Path) -> None:
    """Move the live file to `<path>.1`, overwriting any prior `.1`.

    Caller holds the flock on the live file's fd. Rotation is best-effort:
    failure leaves the live file intact (next append will retry rotation
    when the size is still over cap). Posix rename is atomic; the file's
    open fd in the caller is unaffected (the fd refers to the inode, not
    the path), so concurrent writers' subsequent appends land on the
    rotated `.1` until they reopen — acceptable because mm's lockfile
    serializes push/pull/gc in practice.
    """
    rotated = live_path.with_name(live_path.name + ROTATED_SUFFIX)
    try:
        os.replace(str(live_path), str(rotated))
    except OSError:
        return


def read_records(
    path: Path | None = None,
    *,
    include_rotated: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield records from the live file (and optionally the rotated `.1`).

    Order: rotated `.1` first (older), then live file. Tolerates a
    partially-written or non-JSON first line in `.1` — that's the documented
    crash-mid-rotate fingerprint and we'd rather lose one ghost record than
    refuse to read the whole file. Per-line JSON failures elsewhere are
    skipped silently (corrupt entries are forensic noise, not failure
    conditions for the reader).
    """
    live = path or history_path()
    rotated = live.with_name(live.name + ROTATED_SUFFIX)

    if include_rotated and rotated.exists():
        yield from _yield_lines(rotated, tolerate_first_line=True)

    if live.exists():
        yield from _yield_lines(live, tolerate_first_line=False)


def _yield_lines(path: Path, *, tolerate_first_line: bool) -> Iterator[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    if tolerate_first_line and i == 0:
                        continue  # crash-mid-rotate fingerprint
                    continue  # other corrupt lines: skip silently
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return
