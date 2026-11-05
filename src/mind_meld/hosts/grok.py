"""Local Grok Build usage reader and diagnostic adapter (Track 110A).

Reads ``~/.grok/sessions/<cwd-dir>/<session-id>/usage.json`` — the per-turn
metadata envelope Grok Build writes beside each session — and nothing else.
The conversation files (``updates.jsonl``, ``chat_history.jsonl``) are never
opened, so a session without a ``usage.json`` (an empty or Paseo-left session)
costs one ``lstat``. The record shapes, the census behind the source choice and
the counter semantics are pinned in
``tests/fixtures/host_sessions/grok/CONTRACT.md``; the reader's invariants are
in ``docs/invariants/events-retro.md`` ("Grok Build usage reader").

Rows are per turn; ``inputTokens`` is inclusive of ``cachedReadTokens`` and is
normalized through the shared inclusive rule; ``reasoningTokens`` is inside
``outputTokens`` and never added again. A fork repeats its parent's rows
byte-identically, so each turn is keyed by ``(endedAt, turnNumber, model)`` and
counted once across files. Only family, model, UTC day and token counters are
stored or published: never ``costUsdTicks``, paths or session ids.

Calls ``host_usage`` helpers module-qualified so test monkeypatches on the
shared kit reach this adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mind_meld import host_usage
from mind_meld.errors import StorageError
from mind_meld.lockedjson import (
    InvalidJsonCache,
    LockContended,
    locked_json_rmw,
    locked_json_snapshot,
)
from mind_meld.token_usage import TOKEN_FIELDS

GROK_SESSIONS_PATH = Path.home() / ".grok" / "sessions"
CACHE_PATH = Path.home() / ".config" / "mind-meld" / "grok-host-tokens.json"
"""Isolated Grok Build cache (the pre-v1.11.8 retired reader used this path too)."""

GROK_USAGE_CENSUS_HOST_VERSION = "1.0.70"
"""Host version of the current ``usage.json`` record contract.

Bound to ``tests/fixtures/host_sessions/grok/CONTRACT.md`` by
``test_contract_census_pin_matches_src_constant``."""

_GROK_CACHE_FORMAT = 2
"""Root marker of this cache layout. A pre-v1.11.8 cache at ``CACHE_PATH`` lacks
it, so it is discarded on this reader's first write."""

_MAX_USAGE_BYTES = 16 * 1024 * 1024
"""Largest ``usage.json`` the reader will parse (a row is under 1 KiB)."""

_ENDED_AT_MIN = datetime(2020, 1, 1, tzinfo=timezone.utc)
_ENDED_AT_FUTURE_SLACK = timedelta(days=1)

_COUNTER_KEYS = (
    "inputTokens",
    "outputTokens",
    "cachedReadTokens",
    "cacheCreationTokens",
    "reasoningTokens",
    "totalTokens",
)
"""Counters the reader needs on a turn row and on every ``modelUsage`` entry.

Required keys are exactly the ones read. Every other key (``modelCalls``,
``costUsdTicks``, ``turnCount``, ``primaryModelId``, ``session`` and anything
Grok Build adds later) is ignored: refusing unknown keys blacked out the retired
reader for months after one additive field."""

_ABSENT: dict[str, Any] = {"absent": True}


def grok_sessions_root() -> Path:
    """Resolve the Grok Build sessions directory at call time."""
    return GROK_SESSIONS_PATH


# --- Record validation ------------------------------------------------------


def _day_of(value: Any) -> str:
    """UTC day of a timezone-aware ``endedAt``; naive, pre-2020 or far-future is ``malformed``."""
    if not isinstance(value, str) or not value.isascii() or len(value) > 64:
        raise host_usage._ReadFailure("malformed")
    try:
        when = datetime.fromisoformat(value)
        if when.tzinfo is None:
            raise host_usage._ReadFailure("malformed")
        when = when.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise host_usage._ReadFailure("malformed") from exc
    if when < _ENDED_AT_MIN or when > datetime.now(timezone.utc) + _ENDED_AT_FUTURE_SLACK:
        raise host_usage._ReadFailure("malformed")
    return when.date().isoformat()


def _counters(record: Any) -> dict[str, int]:
    """Required counters of one row or ``modelUsage`` entry, validated.

    A missing key or a broken ``totalTokens`` identity is ``unsupported`` (a format
    change); a wrong type, a negative or oversize value, or ``reasoning > output``
    is ``malformed``.
    """
    if not isinstance(record, dict):
        raise host_usage._ReadFailure("unsupported")
    if any(key not in record for key in _COUNTER_KEYS):
        raise host_usage._ReadFailure("unsupported")
    values: dict[str, int] = {}
    for key in _COUNTER_KEYS:
        value = record[key]
        if not host_usage._is_valid_counter(value):
            raise host_usage._ReadFailure("malformed")
        values[key] = value
    if values["totalTokens"] != values["inputTokens"] + values["outputTokens"]:
        raise host_usage._ReadFailure("unsupported")
    if values["reasoningTokens"] > values["outputTokens"]:
        raise host_usage._ReadFailure("malformed")
    return values


def _turn_key(ended_at: str, turn_number: int, model: str) -> str:
    return hashlib.sha256(f"grok\0{ended_at}\0{turn_number}\0{model}".encode()).hexdigest()


def _parse_usage(data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Project one ``usage.json`` to ``(turns, partial_days)``; fail closed on drift.

    Each turn is one ``(row, model)`` pair carrying DISJOINT counters. ``session`` is
    never read: it includes history inherited by resume or fork.
    """
    try:
        document = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise host_usage._ReadFailure("malformed") from exc
    if not isinstance(document, dict) or not isinstance(document.get("turns"), list):
        raise host_usage._ReadFailure("unsupported")
    turns: dict[str, dict[str, Any]] = {}
    partial: set[str] = set()
    for row in document["turns"]:
        row_counters = _counters(row)
        if "turnNumber" not in row or "endedAt" not in row or "modelUsage" not in row:
            raise host_usage._ReadFailure("unsupported")
        turn_number = row["turnNumber"]
        if not host_usage._is_nonnegative_int(turn_number) or turn_number > host_usage._MAX_COUNTER:
            raise host_usage._ReadFailure("malformed")
        day = _day_of(row["endedAt"])
        models = row["modelUsage"]
        if not isinstance(models, dict):
            raise host_usage._ReadFailure("unsupported")
        sums = dict.fromkeys(_COUNTER_KEYS, 0)
        for model, entry in models.items():
            if not isinstance(model, str) or not model:
                raise host_usage._ReadFailure("malformed")
            try:
                model_bytes = model.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise host_usage._ReadFailure("malformed") from exc
            if len(model_bytes) > host_usage._MAX_MODEL_ID_BYTES:
                raise host_usage._ReadFailure("malformed")
            values = _counters(entry)
            for key in _COUNTER_KEYS:
                sums[key] += values[key]
            disjoint = host_usage._normalize_inclusive_counters(
                (
                    values["inputTokens"],
                    values["cacheCreationTokens"],
                    values["cachedReadTokens"],
                    values["outputTokens"],
                )
            )
            if values["cacheCreationTokens"] > 0:
                partial.add(day)
            turn = {
                "key": _turn_key(row["endedAt"], turn_number, model),
                "day": day,
                "model": model,
                "usage": dict(zip(TOKEN_FIELDS, disjoint)),
            }
            prior = turns.setdefault(turn["key"], turn)
            if prior != turn:
                raise host_usage._ReadFailure("malformed")
        if sums != row_counters:
            raise host_usage._ReadFailure("malformed")
    return [turns[key] for key in sorted(turns)], sorted(partial)


def _identity(source: os.stat_result) -> dict[str, int]:
    return {
        "dev": source.st_dev,
        "ino": source.st_ino,
        "size": source.st_size,
        "mtime_ns": source.st_mtime_ns,
    }


def _validated_entry(value: Any) -> dict[str, Any] | None:
    """A cached session entry, or ``None`` to force one re-read. Never coerces."""
    if not isinstance(value, dict):
        return None
    if value.get("absent") is True:
        return dict(_ABSENT)
    if any(not host_usage._is_nonnegative_int(value.get(key)) for key in _identity_keys()):
        return None
    if value.get("counter_semantics") != "disjoint-v1":
        return None
    partial_days = host_usage._validated_turn_partial_days(value.get("partial_days"))
    turns = value.get("turns")
    if partial_days is None or not isinstance(turns, list):
        return None
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            return None
        key, model, day, usage = (turn.get(name) for name in ("key", "model", "day", "usage"))
        if not isinstance(key, str) or len(key) != 64 or key in seen:
            return None
        seen.add(key)
        if not isinstance(model, str) or not model:
            return None
        try:
            model_bytes = model.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if len(model_bytes) > host_usage._MAX_MODEL_ID_BYTES:
            return None
        if not host_usage._validated_day(day):
            return None
        if not isinstance(usage, dict) or any(
            not host_usage._is_valid_counter(usage.get(field)) for field in TOKEN_FIELDS
        ):
            return None
        normalized.append(
            {
                "key": key,
                "day": day,
                "model": model,
                "usage": {field: usage[field] for field in TOKEN_FIELDS},
            }
        )
    return {
        **{key: value[key] for key in _identity_keys()},
        "turns": normalized,
        "partial_days": partial_days,
        "counter_semantics": "disjoint-v1",
    }


def _identity_keys() -> tuple[str, ...]:
    return ("dev", "ino", "size", "mtime_ns")


# --- Scan -------------------------------------------------------------------


def _read_usage_file(path: Path, before: os.stat_result) -> bytes:
    """Bounded read of one regular ``usage.json``; a changed file is ``stale``."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise host_usage._ReadFailure("io_error") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not host_usage._same_source(before, opened):
            raise host_usage._ReadFailure("stale")
        chunks: list[bytes] = []
        remaining = _MAX_USAGE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise host_usage._ReadFailure("io_error") from exc
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > _MAX_USAGE_BYTES:
        raise host_usage._ReadFailure("malformed")
    return data


def _session_entry(usage_path: Path, cached: dict[str, Any] | None) -> dict[str, Any]:
    """One session's cache entry. Opens ``usage.json`` only when it changed."""
    try:
        before = usage_path.lstat()
    except FileNotFoundError:
        return dict(_ABSENT)
    except OSError as exc:
        raise host_usage._ReadFailure("io_error") from exc
    if not stat.S_ISREG(before.st_mode):
        # A symlink or directory in that slot is never followed or read.
        return dict(_ABSENT)
    if (
        cached is not None
        and "absent" not in cached
        and _identity(before) == {key: cached[key] for key in _identity_keys()}
    ):
        return cached
    if before.st_size > _MAX_USAGE_BYTES:
        raise host_usage._ReadFailure("malformed")
    data = _read_usage_file(usage_path, before)
    try:
        after = usage_path.lstat()
    except OSError as exc:
        raise host_usage._ReadFailure("stale") from exc
    if not host_usage._same_source(before, after):
        raise host_usage._ReadFailure("stale")
    turns, partial_days = _parse_usage(data)
    return {
        **_identity(after),
        "turns": turns,
        "partial_days": partial_days,
        "counter_semantics": "disjoint-v1",
    }


def _child_dirs(path: Path, *, deadline: float | None = None) -> list[tuple[str, Path]]:
    """Sorted real (non-symlink) child directories; Grok's ``.cwd`` files are skipped."""
    if deadline is not None and host_usage._expired(deadline):
        raise host_usage._ReadFailure("deadline")
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        raise host_usage._ReadFailure("io_error") from exc
    found: list[tuple[str, Path]] = []
    for name in names:
        if deadline is not None and host_usage._expired(deadline):
            raise host_usage._ReadFailure("deadline")
        child = path / name
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise host_usage._ReadFailure("io_error") from exc
        if stat.S_ISDIR(info.st_mode):
            found.append((name, child))
    return found


def _aggregate_files(files: dict[str, dict[str, Any]]) -> host_usage.HostUsageResult:
    unique: dict[str, dict[str, Any]] = {}
    partial: set[str] = set()
    for entry in files.values():
        if entry.get("absent"):
            continue
        partial.update(entry.get("partial_days", ()))
        for turn in entry["turns"]:
            prior = unique.setdefault(turn["key"], turn)
            if prior != turn:
                raise host_usage._ReadFailure("malformed")
    buckets = host_usage.HostUsageBuckets()
    for turn in unique.values():
        host_usage._add_usage(buckets, turn["day"], turn["model"], turn["usage"])
    return host_usage._result_from_buckets(buckets, complete=True, partial_days=frozenset(partial))


def _scan_grok_root(
    root: Path,
    cached_files: dict[str, dict[str, Any]],
    deadline: float,
) -> tuple[host_usage.HostUsageResult, dict[str, dict[str, Any]]]:
    """Walk ``root/*/*``; returns the result and every session entry staged so far."""
    if host_usage._expired(deadline):
        return host_usage._incomplete("deadline"), {}
    try:
        info = root.stat()
    except FileNotFoundError:
        return host_usage.HostUsageResult({}, complete=True), {}
    except OSError:
        return host_usage._incomplete("io_error"), {}
    if not stat.S_ISDIR(info.st_mode):
        return host_usage._incomplete("io_error"), {}
    try:
        canonical_root = root.resolve()
    except (OSError, RuntimeError):
        canonical_root = root
    staged: dict[str, dict[str, Any]] = {}
    try:
        for _cwd_name, cwd_path in _child_dirs(root, deadline=deadline):
            for _session_name, session_path in _child_dirs(cwd_path, deadline=deadline):
                if host_usage._expired(deadline):
                    raise host_usage._ReadFailure("deadline")
                key = host_usage._cache_key(session_path, root=root, canonical_root=canonical_root)
                staged[key] = _session_entry(session_path / "usage.json", cached_files.get(key))
        return _aggregate_files(staged), staged
    except host_usage._ReadFailure as failure:
        return host_usage._incomplete(failure.reason), staged


def _count_sessions(root: Path) -> int | None:
    """Number of session directories on disk; ``None`` on any scan error. Opens nothing."""
    try:
        return sum(len(_child_dirs(cwd_path)) for _name, cwd_path in _child_dirs(root))
    except host_usage._ReadFailure:
        return None


def _empty_cache() -> dict[str, Any]:
    return host_usage._empty_turn_cache()


@host_usage._pause_gc()
def read_grok_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
    consented: bool = False,
) -> host_usage.HostUsageResult:
    """Read completed Grok Build turn totals from ``usage.json`` files.

    Closed by default: ``consented=False`` does not stat or open the store. The
    sweep binds ``consented=True`` only when ``config.grok_usage_consented`` is
    true (``events_tail._default_host_readers``).
    """
    started = time.monotonic()
    if not consented:
        return host_usage._incomplete("no_metadata_ledger")
    source_root = root if root is not None else grok_sessions_root()
    read_deadline = deadline if deadline is not None else started + host_usage.DEFAULT_READ_BUDGET_S
    if host_usage._expired(read_deadline):
        return host_usage._incomplete("deadline")

    try:
        with locked_json_rmw(
            CACHE_PATH,
            mode=0o600,
            compact=True,
            default_factory=_empty_cache,
            retry_intervals=(),
            on_contention="raise" if host_usage._DIAGNOSTIC.get() is not None else "warn",
            contention_warning="host token adapter cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return host_usage._incomplete("locked")
            current_format = (
                locked.data.get("version") == host_usage.CACHE_VERSION
                and locked.data.get("grok_format") == _GROK_CACHE_FORMAT
            )
            cached = locked.data if current_format else {}
            prior = (
                host_usage._cached_last_reason(cached),
                host_usage._cached_reason_since(cached),
            )
            raw_files = host_usage._cached_files(cached)
            cached_files = {
                key: entry
                for key, raw in raw_files.items()
                if (entry := _validated_entry(raw)) is not None
            }
            if host_usage._expired(read_deadline):
                result, staged_files = host_usage._incomplete("deadline"), {}
            else:
                result, staged_files = _scan_grok_root(source_root, cached_files, read_deadline)
            ready = time.monotonic()
            over_budget = result.complete and host_usage._expired(read_deadline)
            now = datetime.now(timezone.utc)
            carried = host_usage._carry_reason(*prior, result, now, over_budget=over_budget)
            timing = host_usage._carry_read_timing(
                cached,
                result,
                carried[0],
                started,
                ready,
                read_deadline,
                now,
                over_budget=over_budget,
            )
            # A discarded old-format cache is a change worth one write.
            learned = not current_format or any(
                cached_files.get(key) != entry for key, entry in staged_files.items()
            )
            if host_usage._skip_failed_cache_write(learned, result, prior, carried, cached, timing):
                raise host_usage._NoCacheCommit(result)
            files = staged_files if result.complete else {**cached_files, **staged_files}
            complete_once = cached.get("complete_once") is True or result.complete
            locked.data = {
                **timing,
                "version": host_usage.CACHE_VERSION,
                "grok_format": _GROK_CACHE_FORMAT,
                "census_host_version": GROK_USAGE_CENSUS_HOST_VERSION,
                "complete_once": complete_once,
                "usage_less_skipped": sum(1 for entry in files.values() if entry.get("absent")),
                "last_reason": carried[0],
                "last_reason_since": carried[1],
                "files": files,
            }
            if over_budget:
                result = host_usage._incomplete("deadline")
        host_usage._notice_cache_write_failure(locked.write_error)
        return result
    except LockContended:
        host_usage._diagnostic(
            "mm: notice: host token adapter cache was locked; skipping host usage scan\n", False
        )
        return host_usage._incomplete("locked")
    except host_usage._NoCacheCommit as aborted:
        return aborted.result
    except (OSError, StorageError, InvalidJsonCache):
        return host_usage._incomplete("io_error")


def grok_usage_diag(*, count_logs: bool = False) -> dict[str, Any]:
    """Grok Build cache inventory and session count. Cache-only unless ``count_logs``.

    ``count_logs`` lists the two directory levels under the sessions root (no file
    is opened); callers pass it only while Grok usage capture is consented.
    """
    on_disk = _count_sessions(grok_sessions_root()) if count_logs else None
    blank = {
        **host_usage._cached_read_timing({}),
        "complete_once": False,
        "usage_less_skipped": 0,
        "last_reason": None,
        "last_reason_since": None,
        "cache_state": "unreadable",
        "model_count": 0,
        "models": [],
        "files_cached": None,
        "files_on_disk": on_disk,
    }
    try:
        with locked_json_snapshot(CACHE_PATH, blocking=False) as snap:
            data = snap.data
            state = snap.state
    except OSError:
        return blank
    if state != "valid" or not isinstance(data, dict):
        return {
            **blank,
            "cache_state": "missing" if state in {"missing", "empty"} else "unreadable",
        }
    files = data.get("files")
    if data.get("version") != host_usage.CACHE_VERSION or not isinstance(files, dict):
        return blank
    if data.get("grok_format") != _GROK_CACHE_FORMAT:
        return blank
    raw_skip = data.get("usage_less_skipped", 0)
    skipped = raw_skip if host_usage._is_nonnegative_int(raw_skip) else 0
    models = host_usage._diag_turn_model_ids(files)
    return {
        "complete_once": data.get("complete_once") is True,
        "usage_less_skipped": skipped,
        **host_usage._cached_read_timing(data),
        "last_reason": host_usage._cached_last_reason(data),
        "last_reason_since": host_usage._cached_reason_since(data),
        "cache_state": "ok",
        "model_count": models["model_count"],
        "models": models["models"],
        "files_cached": len(files),
        "files_on_disk": on_disk,
    }
