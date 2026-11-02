"""Best-effort local sync observations for passphrase-free diagnostics.

These records describe prior command observations, never current peer files.
Neither the pull rule nor deletion authorization reads this cache.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from mind_meld import config, lockedjson, safety

_MAX_ROWS = 4096
_MAX_TEXT = 4096


def cache_path() -> Path:
    return config.CONFIG_PATH.parent / "sync-observations.json"


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT:
        return None
    return safety.safe_terminal_str(value)


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative(value: object, *, root: bool = False) -> str | None:
    if root and value == "":
        return ""
    text = _text(value)
    if text is None or PurePosixPath(text).is_absolute() or ".." in text.split("/"):
        return None
    return text if root or text != "." else None


def _older(row: object) -> dict | None:
    if not isinstance(row, dict):
        return None
    peer, source, path = (
        _text(row.get("peer")),
        _text(row.get("source")),
        _relative(row.get("path")),
    )
    if peer is None or source is None or path is None:
        return None
    return {
        "peer": peer,
        "source": source,
        "path": path,
        "local_mtime": _timestamp(row.get("local_mtime")),
        "peer_mtime": _timestamp(row.get("peer_mtime")),
    }


def _anchor(row: object, *, stored: bool = True) -> dict | None:
    if not isinstance(row, dict):
        return None
    source, root, folder = (
        _text(row.get("source")),
        _relative(row.get("root"), root=True),
        _text(row.get("folder")),
    )
    state = row.get("state")
    if (
        source is None
        or root is None
        or folder is None
        or state not in ("mismatch", "stable", "absent")
    ):
        return None
    result = {"source": source, "root": root, "folder": folder, "state": state}
    if stored:
        count, when = row.get("count"), _timestamp(row.get("observed_at"))
        if type(count) is not int or not 0 <= count <= _MAX_ROWS or when is None:
            return None
        result.update(count=count, observed_at=when)
    return result


def _rows(value: object, validate: Callable) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [valid for row in value[:_MAX_ROWS] if (valid := validate(row)) is not None]


def _normalized(data: dict) -> dict:
    pull = data.get("pull")
    observed = _timestamp(pull.get("observed_at")) if isinstance(pull, dict) else None
    clean_pull = None
    if observed:
        rows = _rows(pull.get("older"), _older)
        count = pull.get("older_count")
        clean_pull = {
            "observed_at": observed,
            "complete": pull.get("complete") is True,
            "may_be_stale": True,
            "older": rows,
            "older_count": count if type(count) is int and len(rows) <= count else len(rows),
        }
    push = data.get("push")
    return {
        "version": 1,
        "pull": clean_pull,
        "push": {"records": _rows(push.get("records"), _anchor) if isinstance(push, dict) else []},
    }


def read_observations() -> dict:
    """No-create, no-repair snapshot; contention reports unknown immediately."""
    path = cache_path()
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return {"state": "unreadable", **_normalized({})}
    except FileNotFoundError:
        return {"state": "missing", **_normalized({})}
    except OSError:
        return {"state": "unreadable", **_normalized({})}
    with lockedjson.locked_json_snapshot(path, blocking=False) as snapshot:
        if snapshot.state != "valid":
            return {"state": snapshot.state, **_normalized({})}
        data = snapshot.data or {}
        if data.get("version") != 1:
            return {"state": "unsupported", **_normalized({})}
        return {"state": "recorded", **_normalized(data)}


def _update(update: Callable[[dict], None]) -> str | None:
    path = cache_path()
    try:
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                return "unavailable"
        except FileNotFoundError:
            pass
        with lockedjson.locked_json_rmw(path, on_contention="raise", retry_intervals=()) as cache:
            if cache.data.get("version", 1) != 1:
                cache.write_on_exit = False
                return "unsupported"
            data = _normalized(cache.data)
            update(data)
            if data == cache.data:
                cache.write_on_exit = False
            cache.data = data
        return "unavailable" if cache.write_error is not None else None
    except lockedjson.LockContended:
        return "busy"
    except OSError:
        return "unavailable"


def record_pull_observations(
    older: list[dict], *, complete: bool = True, observed_at: str | None = None
) -> str | None:
    """Replace the prior pull observation after a real pull, including an empty one."""

    def update(data: dict) -> None:
        data["pull"] = {
            "observed_at": _timestamp(observed_at) or _now(),
            "complete": complete,
            "may_be_stale": True,
            "older": _rows(older, _older),
            "older_count": len(older),
        }

    return _update(update)


def record_push_anchor_observations(
    observations: list[dict], *, observed_at: str | None = None
) -> tuple[list[dict], str | None]:
    """Count consecutive accepted-push mismatches; reset stable/absent selected roots.

    Omitted roots retain their observations. The caller supplies only mismatches
    against valid published tokens with no absent files, before token renewal.
    """
    repeated: list[dict] = []

    def update(data: dict) -> None:
        records = {(r["source"], r["root"]): r for r in data["push"]["records"]}
        for row in observations[:_MAX_ROWS]:
            if (clean := _anchor(row, stored=False)) is None:
                continue
            key = (clean["source"], clean["root"])
            prior = records.get(key, {})
            if (
                clean["state"] != "mismatch"
                and prior.get("count") == 0
                and all(prior.get(key) == value for key, value in clean.items())
            ):
                continue
            count = min(prior.get("count", 0) + 1, _MAX_ROWS) if clean["state"] == "mismatch" else 0
            clean.update(count=count, observed_at=_timestamp(observed_at) or _now())
            records[key] = clean
            if count >= 2:
                repeated.append(clean)
        data["push"] = {"records": list(records.values())[-_MAX_ROWS:]}

    error = _update(update)
    return (repeated if error is None else []), error
