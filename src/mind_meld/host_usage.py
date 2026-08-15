"""Private, local-only host-usage readers.

Track 17C currently supports Codex rollout logs at
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``. A rollout's last supported
``event_msg`` / ``payload.type == "token_count"`` record is cumulative, so it
*replaces* the previous total for that file; summing every token-count record
would double-count. The model is the most recent preceding
``turn_context.payload.model``. Counters map onto Mind Meld's four token
fields: ``input_tokens`` → input, ``cache_write_input_tokens`` →
cache-create, ``cached_input_tokens`` → cache-read, and ``output_tokens`` →
output. ``reasoning_output_tokens`` is already part of output and is never
added a second time.

The reader is read-only with respect to host logs. Its private 0600 cache,
``~/.config/mind-meld/host-tokens.json``, stores only opaque path digests,
file fingerprints, bounded model IDs, and aggregate totals—never transcript
content, raw paths, prompts, or tool output. ``complete=False`` is a safety
signal: a caller must omit the host snapshot rather than serialize a partial
or invented zero. Track 19A owns that caller policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

from mind_meld.lockedjson import locked_json_rmw
from mind_meld.token_usage import (
    TOKEN_FIELDS,
    Usage,
    iter_bounded_lines,
    merge_usage_bucket,
    zero_model_bucket,
)

CACHE_PATH = Path.home() / ".config" / "mind-meld" / "host-tokens.json"
"""Forensic host-reader cache. Deliberately separate from Claude's cache."""

CODEX_SESSIONS_PATH = Path.home() / ".codex" / "sessions"
CACHE_VERSION = 1
DEFAULT_READ_BUDGET_S = 5.0

_HEAD_PROBE_BYTES = 4096
_TAIL_PROBE_BYTES = 4096
_MAX_MODEL_ID_BYTES = 256
_MAX_COUNTER = 2**53
_YEAR_PART = re.compile(r"^\d{4}$")
_MONTH_OR_DAY_PART = re.compile(r"^\d{2}$")
_ROLLOUT_NAME = re.compile(r"^rollout-.*\.jsonl$")

HostFamily = Literal["claude", "codex", "grok", "other"]
Reason = Literal["deadline", "io_error", "locked", "malformed", "partial", "stale", "unsupported"]
HostTokens = dict[str, dict[str, Usage]]


@dataclass(frozen=True)
class HostUsageResult:
    """Aggregate totals as ``{host_family: {UTC-day: Usage}}``.

    Empty ``hosts`` with ``complete=True`` is a real completed empty scan.
    Empty ``hosts`` with ``complete=False`` is intentionally *not* a zero.
    """

    hosts: HostTokens
    complete: bool
    reason: Reason | None = None

    @property
    def empty(self) -> bool:
        return not self.hosts


class _CacheEntry(TypedDict):
    dev: int
    ino: int
    size: int
    mtime_ns: int
    head: str
    head_len: int
    tail: str
    tail_len: int
    offset: int
    day: str
    model: str
    usage: Usage


@dataclass(frozen=True)
class _Fingerprint:
    head: str
    head_len: int
    tail: str
    tail_len: int


@dataclass(frozen=True)
class _Terminal:
    day: str
    model: str
    usage: Usage


class _ReadFailure(RuntimeError):
    def __init__(self, reason: Reason) -> None:
        self.reason = reason


class _NoCacheCommit(RuntimeError):
    """Escape a locked-json context without its unconditional write."""

    def __init__(self, result: HostUsageResult) -> None:
        self.result = result


def host_family(model: str) -> HostFamily:
    """Return Mind Meld's canonical model-family bucket.

    OpenCode's later reader must pass its model ID here; OpenCode is not a
    row of its own. Classification is case-insensitive and intentionally
    small so renderers never grow their own incompatible predicates.
    """
    normalized = model.casefold() if isinstance(model, str) else ""
    if normalized.startswith("claude-"):
        return "claude"
    if (
        normalized.startswith("gpt-")
        or normalized in {"o1", "o3"}
        or normalized.startswith("o4-")
        or "codex" in normalized
    ):
        return "codex"
    if normalized.startswith("grok-"):
        return "grok"
    return "other"


def read_codex_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
) -> HostUsageResult:
    """Read complete Codex rollout totals, using the isolated local cache.

    ``deadline`` is an absolute ``time.monotonic()`` deadline. The reader
    checks it before discovery, per file, per bounded line, and persistence.
    Cache contention is a single non-blocking attempt, never the normal
    750ms locked-json retry budget.
    """
    source_root = root if root is not None else CODEX_SESSIONS_PATH
    read_deadline = deadline if deadline is not None else time.monotonic() + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")

    try:
        with locked_json_rmw(
            CACHE_PATH,
            mode=0o600,
            default_factory=_empty_cache,
            retry_intervals=(),
            on_contention="warn",
            contention_warning="host token cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            if _expired(read_deadline):
                raise _NoCacheCommit(_incomplete("deadline"))

            cached_files = _cached_files(locked.data)
            result, staged_files = _scan_codex_root(source_root, cached_files, read_deadline)
            if not result.complete:
                raise _NoCacheCommit(result)
            if _expired(read_deadline):
                raise _NoCacheCommit(_incomplete("deadline"))
            locked.data = {"version": CACHE_VERSION, "files": staged_files}
            return result
    except _NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return _incomplete("io_error")


def _scan_codex_root(
    root: Path,
    cached_files: dict[str, Any],
    deadline: float,
) -> tuple[HostUsageResult, dict[str, _CacheEntry]]:
    try:
        rollouts = list(_iter_rollouts(root, deadline))
    except _ReadFailure as failure:
        return _incomplete(failure.reason), {}

    staged: dict[str, _CacheEntry] = {}
    for path in rollouts:
        if _expired(deadline):
            return _incomplete("deadline"), {}
        key = _cache_key(path)
        try:
            before = _regular_stat(path)
            existing = cached_files.get(key)
            entry = _cache_hit(path, before, existing, deadline)
            if entry is None:
                resume = _resumable_entry(path, before, existing, deadline)
                entry = (
                    _resume_rollout(path, before, resume, deadline)
                    if resume is not None
                    else _read_full_rollout(path, before, deadline)
                )
            staged[key] = entry
        except _ReadFailure as failure:
            return _incomplete(failure.reason), {}

    return HostUsageResult(_aggregate(staged.values()), complete=True), staged


def _iter_rollouts(root: Path, deadline: float):
    """Yield only regular non-symlink date-nested Codex rollout files."""
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        if not root.exists():
            return
        for year in _sorted_children(root, deadline):
            if not _YEAR_PART.fullmatch(year.name) or not _is_directory(year):
                continue
            for month in _sorted_children(year, deadline):
                if not _MONTH_OR_DAY_PART.fullmatch(month.name) or not _is_directory(month):
                    continue
                for day in _sorted_children(month, deadline):
                    if not _MONTH_OR_DAY_PART.fullmatch(day.name) or not _is_directory(day):
                        continue
                    for candidate in _sorted_children(day, deadline):
                        if not _ROLLOUT_NAME.fullmatch(candidate.name):
                            continue
                        if _is_regular_non_symlink(candidate):
                            yield candidate
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _sorted_children(path: Path, deadline: float) -> list[Path]:
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        return sorted(path.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _is_directory(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _is_regular_non_symlink(path: Path) -> bool:
    try:
        st = path.lstat()
        return not stat.S_ISLNK(st.st_mode) and stat.S_ISREG(st.st_mode)
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _read_full_rollout(path: Path, before: os.stat_result, deadline: float) -> _CacheEntry:
    terminal = _read_rollout(path, 0, None, before, deadline)
    after = _regular_stat(path)
    if not _same_source(before, after):
        raise _ReadFailure("stale")
    fingerprint = _fingerprint(path, after, deadline)
    if not _same_source(after, _regular_stat(path)):
        raise _ReadFailure("stale")
    return _cache_entry(after, fingerprint, terminal)


def _cache_hit(
    path: Path,
    before: os.stat_result,
    existing: Any,
    deadline: float,
) -> _CacheEntry | None:
    entry = _validated_entry(existing)
    if entry is None:
        return None
    if not _same_cache_metadata(entry, before):
        return None
    fingerprint = _fingerprint(path, before, deadline)
    if not _same_fingerprint(entry, fingerprint):
        return None
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return entry


def _resumable_entry(
    path: Path,
    source: os.stat_result,
    existing: Any,
    deadline: float,
) -> _CacheEntry | None:
    """Return a prior entry only when this is a verified append.

    The prior head and the bytes that were the old tail must still match.
    This catches common same-path rewrites before using the complete-line
    offset. Any doubt falls back to a bounded full parse.
    """
    entry = _validated_entry(existing)
    if entry is None:
        return None
    if entry["dev"] != source.st_dev or entry["ino"] != source.st_ino:
        return None
    if source.st_size <= entry["size"]:
        return None
    if entry["head_len"] != min(entry["size"], _HEAD_PROBE_BYTES) or entry["tail_len"] != min(
        entry["size"], _TAIL_PROBE_BYTES
    ):
        return None
    if _digest_range(path, 0, entry["head_len"], deadline) != entry["head"]:
        return None
    old_tail_start = entry["size"] - entry["tail_len"]
    if _digest_range(path, old_tail_start, entry["tail_len"], deadline) != entry["tail"]:
        return None
    if not _same_source(source, _regular_stat(path)):
        raise _ReadFailure("stale")
    return entry


def _resume_rollout(
    path: Path,
    before: os.stat_result,
    entry: _CacheEntry,
    deadline: float,
) -> _CacheEntry:
    previous = _Terminal(entry["day"], entry["model"], entry["usage"])
    terminal = _read_rollout(path, entry["offset"], previous, before, deadline)
    after = _regular_stat(path)
    if not _same_source(before, after):
        raise _ReadFailure("stale")
    fingerprint = _fingerprint(path, after, deadline)
    if not _same_source(after, _regular_stat(path)):
        raise _ReadFailure("stale")
    return _cache_entry(after, fingerprint, terminal)


def _read_rollout(
    path: Path,
    start_offset: int,
    previous: _Terminal | None,
    before: os.stat_result,
    deadline: float,
) -> _Terminal:
    """Read a stable file from a complete-line offset and retain last total."""
    current_model = previous.model if previous is not None else None
    terminal = previous
    last_offset = start_offset
    try:
        with path.open("rb") as fp:
            fp.seek(start_offset)
            for raw, end_offset in iter_bounded_lines(
                fp,
                str(path),
                start_offset,
                label="host usage walker",
            ):
                if _expired(deadline):
                    raise _ReadFailure("deadline")
                last_offset = end_offset
                if raw == b"":
                    raise _ReadFailure("malformed")
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError, UnicodeDecodeError) as exc:
                    raise _ReadFailure("malformed") from exc
                if not isinstance(record, dict):
                    raise _ReadFailure("malformed")
                record_type = record.get("type")
                if record_type == "turn_context":
                    current_model = _context_model(record)
                    continue
                if record_type == "event_msg" and _is_token_count(record):
                    terminal = _terminal_from_record(record, current_model)
            if fp.tell() != last_offset:
                # `iter_bounded_lines` intentionally leaves a trailing
                # unterminated write behind the persisted offset.
                raise _ReadFailure("partial")
    except OSError as exc:
        raise _ReadFailure("io_error") from exc

    if terminal is None:
        raise _ReadFailure("unsupported")
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return terminal


def _context_model(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str) or not model or len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
        raise _ReadFailure("unsupported")
    return model


def _is_token_count(record: dict[str, Any]) -> bool:
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "token_count"


def _terminal_from_record(record: dict[str, Any], model: str | None) -> _Terminal:
    if model is None:
        raise _ReadFailure("unsupported")
    payload = record.get("payload")
    info = payload.get("info") if isinstance(payload, dict) else None
    totals = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(totals, dict):
        raise _ReadFailure("unsupported")
    usage: Usage = {
        "input": _counter(totals, "input_tokens", required=True),
        "cache_create": _counter(totals, "cache_write_input_tokens", required=False),
        "cache_read": _counter(totals, "cached_input_tokens", required=True),
        "output": _counter(totals, "output_tokens", required=True),
    }
    # These fields are deliberately validated but never summed: total_tokens
    # omits cache counters and reasoning_output_tokens is inside output_tokens.
    _counter(totals, "reasoning_output_tokens", required=False)
    _counter(totals, "total_tokens", required=False)
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        raise _ReadFailure("unsupported")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _ReadFailure("unsupported") from exc
    if parsed.tzinfo is None:
        raise _ReadFailure("unsupported")
    return _Terminal(parsed.astimezone(timezone.utc).date().isoformat(), model, usage)


def _counter(totals: dict[str, Any], key: str, *, required: bool) -> int:
    value = totals.get(key, 0)
    if key not in totals and required:
        raise _ReadFailure("unsupported")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _MAX_COUNTER:
        raise _ReadFailure("unsupported")
    return value


def _aggregate(entries: Any) -> HostTokens:
    hosts: HostTokens = {}
    for entry in entries:
        family = host_family(entry["model"])
        days = hosts.setdefault(family, {})
        bucket = days.setdefault(entry["day"], zero_model_bucket())
        merge_usage_bucket(bucket, entry["usage"])
    return hosts


def _cache_entry(
    source: os.stat_result, fingerprint: _Fingerprint, terminal: _Terminal
) -> _CacheEntry:
    return {
        "dev": source.st_dev,
        "ino": source.st_ino,
        "size": source.st_size,
        "mtime_ns": source.st_mtime_ns,
        "head": fingerprint.head,
        "head_len": fingerprint.head_len,
        "tail": fingerprint.tail,
        "tail_len": fingerprint.tail_len,
        "offset": source.st_size,
        "day": terminal.day,
        "model": terminal.model,
        "usage": dict(terminal.usage),
    }


def _cached_files(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("version") != CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _empty_cache() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "files": {}}


def _validated_entry(value: Any) -> _CacheEntry | None:
    if not isinstance(value, dict):
        return None
    integer_keys = ("dev", "ino", "size", "mtime_ns", "head_len", "tail_len", "offset")
    if any(not _is_nonnegative_int(value.get(key)) for key in integer_keys):
        return None
    if value["offset"] != value["size"]:
        return None
    if not isinstance(value.get("head"), str) or not isinstance(value.get("tail"), str):
        return None
    if not isinstance(value.get("day"), str) or not isinstance(value.get("model"), str):
        return None
    if not value["model"] or len(value["model"].encode("utf-8")) > _MAX_MODEL_ID_BYTES:
        return None
    try:
        datetime.fromisoformat(value["day"])
    except ValueError:
        return None
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return None
    if any(not _is_valid_counter(usage.get(key)) for key in TOKEN_FIELDS):
        return None
    return {
        "dev": value["dev"],
        "ino": value["ino"],
        "size": value["size"],
        "mtime_ns": value["mtime_ns"],
        "head": value["head"],
        "head_len": value["head_len"],
        "tail": value["tail"],
        "tail_len": value["tail_len"],
        "offset": value["offset"],
        "day": value["day"],
        "model": value["model"],
        "usage": {key: usage[key] for key in TOKEN_FIELDS},
    }


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_valid_counter(value: Any) -> bool:
    return _is_nonnegative_int(value) and value <= _MAX_COUNTER


def _same_cache_metadata(entry: _CacheEntry, source: os.stat_result) -> bool:
    return (
        entry["dev"] == source.st_dev
        and entry["ino"] == source.st_ino
        and entry["size"] == source.st_size
        and entry["mtime_ns"] == source.st_mtime_ns
    )


def _same_fingerprint(entry: _CacheEntry, fingerprint: _Fingerprint) -> bool:
    return (
        entry["head"] == fingerprint.head
        and entry["head_len"] == fingerprint.head_len
        and entry["tail"] == fingerprint.tail
        and entry["tail_len"] == fingerprint.tail_len
    )


def _fingerprint(path: Path, source: os.stat_result, deadline: float) -> _Fingerprint:
    if _expired(deadline):
        raise _ReadFailure("deadline")
    head_len = min(source.st_size, _HEAD_PROBE_BYTES)
    tail_len = min(source.st_size, _TAIL_PROBE_BYTES)
    return _Fingerprint(
        _digest_range(path, 0, head_len, deadline),
        head_len,
        _digest_range(path, source.st_size - tail_len, tail_len, deadline),
        tail_len,
    )


def _digest_range(path: Path, offset: int, length: int, deadline: float) -> str:
    """Hash a bounded source range, preserving the reader's deadline."""
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        with path.open("rb") as fp:
            fp.seek(offset)
            data = fp.read(length)
    except OSError as exc:
        raise _ReadFailure("io_error") from exc
    if _expired(deadline):
        raise _ReadFailure("deadline")
    if len(data) != length:
        raise _ReadFailure("stale")
    return hashlib.sha256(data).hexdigest()


def _regular_stat(path: Path) -> os.stat_result:
    try:
        source = path.lstat()
    except OSError as exc:
        raise _ReadFailure("io_error") from exc
    if stat.S_ISLNK(source.st_mode) or not stat.S_ISREG(source.st_mode):
        raise _ReadFailure("stale")
    return source


def _same_source(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _cache_key(path: Path) -> str:
    """Hash the canonical path; cache data must not retain a raw local path."""
    with suppress(OSError):
        return hashlib.sha256(os.fsencode(path.resolve())).hexdigest()
    return hashlib.sha256(os.fsencode(path.absolute())).hexdigest()


def _expired(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _incomplete(reason: Reason) -> HostUsageResult:
    return HostUsageResult({}, complete=False, reason=reason)


__all__ = [
    "CACHE_PATH",
    "CACHE_VERSION",
    "CODEX_SESSIONS_PATH",
    "DEFAULT_READ_BUDGET_S",
    "HostUsageResult",
    "host_family",
    "read_codex_usage",
]
