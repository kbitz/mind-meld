"""TEMPORARY conflict-decision telemetry (CONFLICT-TELEMETRY).

Collects a labeled dataset of real conflict resolutions -- the features a future
auto-resolver would see at decision time, paired with the choice the user made --
so the deferred Phase 2 similarity classifier can be validated on real mind-meld
content before it gates any silent merge. See TODOS.md `[plan-eng-review]` Phase 2
and the plan at `~/.gstack/projects/kbitz-mind-meld/kb-kbitz-conflict-resolution-log-design-20260730.md`.

DISPOSABLE. Rip out once Phase 2 thresholds are validated: delete this module,
`grep -rn "CONFLICT-TELEMETRY"` and remove each call site + the
`conflict-log-backfill` command, then delete the jsonl. No schema migration,
no sync/config state to unwind.

Local-only, best-effort, append-only JSONL at
`~/.config/mind-meld/conflict-decisions.jsonl` (mode 0600). Stores DERIVED STATS
+ content hashes only -- never file contents. NOT synced (the config dir is
never synced), so cross-Mac analysis means copying each machine's file by hand.

`append_decision` does its OWN flock write and returns a bool so the backfill
command can report real written/skipped counts. It deliberately does NOT route
through `fsutil.flock_append_jsonl`, which returns None and swallows failures
(that helper's forensic-only contract can't tell "written" from "dropped").
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = 1
LOG_DIR = Path.home() / ".config" / "mind-meld"
LOG_NAME = "conflict-decisions.jsonl"


def log_path() -> Path:
    """Canonical log location, resolved at call time so tests can monkeypatch
    ``LOG_DIR`` for isolation (mirrors pullhistory / sidecar)."""
    return LOG_DIR / LOG_NAME


def append_decision(**fields: Any) -> bool:
    """Append one conflict-decision row. Returns True if it hit disk, False on
    any error. NEVER raises -- a telemetry failure must not break a resolve or
    backfill run.

    Contract differs from ``fsutil.flock_append_jsonl`` on purpose: this returns
    a real success bool (own open+flock+write) so callers can count writes.
    """
    try:
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "ts": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        line = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        return False  # non-serializable field slipped in

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass  # perms best-effort; some filesystems reject fchmod
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def read_records(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Yield decision rows. Corrupt / non-JSON lines are skipped silently
    (forensic reader stance). Used for analysis and for backfill dedup."""
    p = path or log_path()
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return
