"""Per-jsonl Claude Code token-usage measurement for fleet-aware retro.

Walks ``~/.claude/projects/<encoded>/<uuid>.jsonl`` (parent sessions) and
``<uuid>/subagents/agent-*.jsonl`` (subagent invocations), summing
``message.usage`` from each assistant message into per-day buckets keyed
by the message's ``timestamp`` field. Per-jsonl cache at
``~/.config/mind-meld/session-tokens.json`` (flock-guarded via
``mind_meld.lockedjson``) skips re-walks when ``size`` and ``mtime``
haven't drifted.

Pricing dict is module-level. ``PRICING_LAST_UPDATED`` flags refresh
cadence — if more than 6 months stale, verify against Anthropic's pricing
page before trusting cost numbers. Unknown models count toward token
totals but are excluded from cost (the renderer surfaces this fact).

Subagent attribution: tokens contributed to the parent session's
``claude_dir``. Subagent walks do NOT bump ``sessions``, ``total_kb``, or
``last_session_at`` — those preserve parent-session semantics. Wired
into ``events._scan_one_project``.

Concurrent-append safety: the walk re-stats the file after reading and
treats a size/mtime drift during the walk as a cache miss for next push.
Today's wire-up reads the whole file in one pass; the re-stat catches
the case where Claude Code is appending while we read.

Message-level dedup: assistant messages carry per-message UUIDs at
``message.id``. We dedup by UUID at sum time so retries, compaction
artifacts, and interrupted turns that emit duplicate usage rows count
once.

Schema graduation: cache file has ``"version": 1`` at top level. Any
mismatch on read → ignore the cache + walk fresh. Forensic file; cheap
to rebuild.

Lock-order rule (inherited from lockedjson): NEVER acquire mm lockfile
while holding the token-cache flock. Release before any ``pullhistory``
append.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, TypedDict

from mind_meld.lockedjson import locked_json_rmw

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_PATH = Path.home() / ".config" / "mind-meld" / "session-tokens.json"
CACHE_VERSION = 1

# Hard cap on by_day buckets retained per cache entry. Trim every push
# regardless of whether the underlying jsonl is still living, so long-
# lived sessions don't grow the cache file unbounded.
MAX_BY_DAY_DAYS = 90

# Hard cap on per-line size when walking jsonls. Claude Code can write
# very large messages (giant pasted blobs, recursive tool-call payloads),
# but a corrupted or maliciously-crafted line could still OOM the whole
# warm. 16 MiB is well above any realistic Claude session line and well
# below the 64 MiB cache-file read cap.
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024

# Per-process state for the line-size warn-once breadcrumb.
_WARNED_OVERSIZE_PATHS: set[str] = set()

# Time-budget knobs (mirror events.py's interactive/autopush split).
DEFAULT_WARM_BUDGET_S = 5.0
"""Inline warm budget at mm init / first interactive push / detected
upgrade transition. ~2.4s on a 642-jsonl Mac with hot OS cache; capped
generously. Slow disks degrade gracefully via the budget."""

SUBSCRIPTION_CAVEAT = "Cost estimates do not account for subscription plan pricing."
"""Single source of truth — both this module's cost helpers and the
aggregator's render side reference this string."""

# ---------------------------------------------------------------------------
# Pricing (per-million-token rates in USD, list price)
# ---------------------------------------------------------------------------

PRICING_LAST_UPDATED = "2026-05-01"
"""Date this pricing dict was last verified against Anthropic's public
pricing page. If more than ~6 months old, refresh before trusting cost
numbers — pricing for subsequent generations may have shifted."""

PRICING: dict[str, dict[str, float]] = {
    # Sonnet family — confirmed in real session jsonls on this Mac.
    "claude-sonnet-4-6": {
        "input": 3.0,
        "cache_read": 0.30,
        "cache_create": 3.75,
        "output": 15.0,
    },
    # Opus family — confirmed via subagent + parent jsonl scans.
    "claude-opus-4-7": {
        "input": 15.0,
        "cache_read": 1.50,
        "cache_create": 18.75,
        "output": 75.0,
    },
    "claude-opus-4-6": {
        "input": 15.0,
        "cache_read": 1.50,
        "cache_create": 18.75,
        "output": 75.0,
    },
    "claude-opus-4-5": {
        "input": 15.0,
        "cache_read": 1.50,
        "cache_create": 18.75,
        "output": 75.0,
    },
    # Haiku family — fast subagent default.
    "claude-haiku-4-5": {
        "input": 1.0,
        "cache_read": 0.10,
        "cache_create": 1.25,
        "output": 5.0,
    },
}

# Models we deliberately count tokens for but exclude from cost. Today
# only the synthetic placeholder Claude Code uses for internal /
# tool-execution turns.
COST_EXCLUDED_MODELS: frozenset[str] = frozenset({"<synthetic>"})

# Models we've already warned about as "unknown — present in jsonls but
# missing from PRICING." One-shot per process to avoid spamming repeat
# pushes. Reset on interpreter exit; next mm invocation re-warns once.
_WARNED_UNKNOWN_MODELS: set[str] = set()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class Usage(TypedDict, total=False):
    input: int
    cache_create: int
    cache_read: int
    output: int


class DayBucket(TypedDict, total=False):
    """One day of token totals, with per-model breakdown nested under
    ``by_model``. Top-level numbers sum across all models for that day."""

    input: int
    cache_create: int
    cache_read: int
    output: int
    by_model: dict[str, Usage]


class CacheEntry(TypedDict, total=False):
    """One per-jsonl entry in session-tokens.json."""

    size: int
    mtime: float
    by_day: dict[str, DayBucket]


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

_DATE_SUFFIX = re.compile(r"-\d{8}$")
"""Strip a trailing 8-digit YYYYMMDD date suffix from a model id.
Confirmed against real subagent jsonls on this Mac
(``claude-haiku-4-5-20251001`` → ``claude-haiku-4-5``)."""


def _normalize_model_id(s: str) -> str:
    """Strip a trailing 8-digit date suffix.

    Parent session jsonls record family ids like ``claude-opus-4-7``;
    subagent jsonls often record dated snapshots like
    ``claude-haiku-4-5-20251001``. Pricing keys live on the family ID,
    so dated subagent values map onto family pricing via this normalizer.

    ``<synthetic>`` and other non-conforming strings pass through unchanged.
    """
    if not isinstance(s, str) or not s:
        return s
    return _DATE_SUFFIX.sub("", s)


def parse_usage(message: Any) -> tuple[Usage, str, str | None] | None:
    """Extract ``(usage, model, message_id)`` from an assistant message dict.

    Returns ``None`` for any non-conforming shape — narrow types only:
    ``KeyError``, ``TypeError``, ``ValueError``, ``AttributeError``.

    Skips:
      - non-dict messages
      - role != ``assistant``
      - ``usage`` missing or non-dict
      - ``model`` missing or empty string
    """
    if not isinstance(message, dict):
        return None
    if message.get("role") != "assistant":
        return None
    raw_usage = message.get("usage")
    if not isinstance(raw_usage, dict):
        return None
    raw_model = message.get("model")
    if not isinstance(raw_model, str) or not raw_model:
        return None
    model = _normalize_model_id(raw_model)
    usage: Usage = {
        "input": _coerce_int(raw_usage.get("input_tokens")),
        "cache_create": _coerce_int(raw_usage.get("cache_creation_input_tokens")),
        "cache_read": _coerce_int(raw_usage.get("cache_read_input_tokens")),
        "output": _coerce_int(raw_usage.get("output_tokens")),
    }
    msg_id = message.get("id") if isinstance(message.get("id"), str) else None
    return usage, model, msg_id


def _coerce_int(v: Any) -> int:
    """Tolerant int coercion. Negative or non-numeric → 0."""
    if isinstance(v, bool):  # bool is a subclass of int; reject
        return 0
    if isinstance(v, int):
        return v if v >= 0 else 0
    return 0


# ---------------------------------------------------------------------------
# Walk + bucket aggregation
# ---------------------------------------------------------------------------


def walk_jsonl_token_buckets(path: Path) -> dict[str, DayBucket]:
    """Walk a single jsonl, sum per-message ``usage`` into per-day buckets.

    Returns a dict keyed by ``YYYY-MM-DD`` (UTC), capped at the most recent
    ``MAX_BY_DAY_DAYS`` days. Day bucket has top-level totals plus a
    ``by_model`` map.

    Skips:
      - non-JSON lines (``json.JSONDecodeError``)
      - non-assistant messages
      - duplicate ``message.id`` UUIDs (retries / compaction artifacts)
      - messages without a parseable ``timestamp``

    Returns ``{}`` on any I/O failure — caller treats as cache miss for
    next push.
    """
    by_day: dict[str, DayBucket] = {}
    seen_ids: set[str] = set()
    path_str = str(path)
    try:
        with open(path, encoding="utf-8") as fp:
            for line in _iter_bounded_lines(fp, path_str):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                # Walk into wrapper or accept top-level message dict.
                # Real Claude Code jsonls wrap as {"message": {...}, "timestamp": ...}.
                msg = obj.get("message") if "message" in obj else obj
                parsed = parse_usage(msg)
                if parsed is None:
                    continue
                usage, model, msg_id = parsed
                if msg_id is not None:
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                day = _extract_day(obj.get("timestamp"))
                if day is None:
                    continue
                _accumulate(by_day, day, usage, model)
    except OSError:
        return {}
    return _trim_by_day(by_day, MAX_BY_DAY_DAYS)


def _iter_bounded_lines(fp, path_str: str) -> Iterable[str]:
    """Yield each line from ``fp`` capped at ``MAX_JSONL_LINE_BYTES`` chars.

    ``fp.readline(N)`` reads at most N characters before returning, so a
    pathological line with no embedded newline can NEVER pull more than
    ``MAX_JSONL_LINE_BYTES`` into memory before we make the keep/skip
    decision. (Codex outside-voice review caught the prior `for line in
    fp:` form: it lets Python extend its buffer until newline-or-EOF, so
    a single multi-GB line could OOM the whole walk.)

    When an oversize line is detected, drain forward to the next newline
    in bounded chunks and emit a one-shot ``mm: notice:`` for the file
    path so the user can investigate."""
    cap = MAX_JSONL_LINE_BYTES
    while True:
        chunk = fp.readline(cap + 1)
        if not chunk:
            return
        if len(chunk) > cap:
            if path_str not in _WARNED_OVERSIZE_PATHS:
                sys.stderr.write(f"mm: notice: token walker skipping oversize line in {path_str}\n")
                _WARNED_OVERSIZE_PATHS.add(path_str)
            # Drain the rest of this oversize line in bounded chunks.
            while chunk and not chunk.endswith("\n"):
                chunk = fp.readline(cap + 1)
            continue
        yield chunk


def _extract_day(raw_ts: Any) -> str | None:
    """Pull ``YYYY-MM-DD`` from an ISO 8601 ``timestamp`` field. Returns
    None on any malformed shape so the caller skips the message."""
    if not isinstance(raw_ts, str) or not raw_ts:
        return None
    try:
        # Tolerate "Z" suffix and offsets uniformly.
        dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def _accumulate(by_day: dict[str, DayBucket], day: str, usage: Usage, model: str) -> None:
    bucket = by_day.setdefault(
        day,
        {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0, "by_model": {}},
    )
    for k in ("input", "cache_create", "cache_read", "output"):
        bucket[k] = bucket.get(k, 0) + usage.get(k, 0)
    by_model = bucket.setdefault("by_model", {})
    model_bucket = by_model.setdefault(
        model, {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}
    )
    for k in ("input", "cache_create", "cache_read", "output"):
        model_bucket[k] = model_bucket.get(k, 0) + usage.get(k, 0)


def _trim_by_day(by_day: dict[str, DayBucket], max_days: int) -> dict[str, DayBucket]:
    """Cap by_day to the most recent ``max_days`` keys (lex-sorted).

    Iso-8601 dates lex-sort and date-sort identically, so this is safe
    without parsing each key back to a date."""
    if len(by_day) <= max_days:
        return by_day
    keep_keys = sorted(by_day.keys(), reverse=True)[:max_days]
    return {k: by_day[k] for k in keep_keys}


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _empty_cache() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "files": {}}


def _normalize_cache(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce parsed cache to canonical shape. Version mismatch → ignore +
    rebuild from empty (forensic file; cheap to rebuild). ``files`` not
    a dict → empty."""
    if parsed.get("version") != CACHE_VERSION:
        return _empty_cache()
    files = parsed.get("files")
    if not isinstance(files, dict):
        return _empty_cache()
    return {"version": CACHE_VERSION, "files": files}


def _resolve_path(path: Path) -> str:
    """Cache key for a jsonl: ``str(Path.resolve())`` to normalize APFS
    case, symlinks, and conductor workspace path drift. Falls back to
    ``str(path)`` if resolve fails (broken symlink, permission denied)."""
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def get_or_compute(
    path: Path,
    cache_files: dict[str, Any],
    *,
    deadline_monotonic: float | None = None,
) -> dict[str, DayBucket]:
    """Return ``by_day`` for ``path``, hitting the cache when possible.

    Mutates ``cache_files`` in place: on a miss, the new entry is stored.
    Caller passes the ``files`` sub-dict of the locked cache; the helper's
    one job is to populate it consistently.

    Cache hit semantics: same ``size`` AND same ``mtime`` (within 1µs)
    → reuse cached ``by_day``.

    Concurrent append safety: stat once, walk, stat again. If size or
    mtime drifted during the walk, treat as a miss and DO NOT persist
    the (potentially partial) walk — let the next push pick up the
    settled file.

    Deadline: if ``deadline_monotonic`` is set and we hit it BEFORE
    starting the walk, return whatever's cached (or empty) without
    touching the cache.
    """
    if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
        # Out of budget — return cached value if present, else empty.
        key = _resolve_path(path)
        existing = cache_files.get(key)
        if isinstance(existing, dict):
            by_day = existing.get("by_day")
            if isinstance(by_day, dict):
                return by_day
        return {}

    key = _resolve_path(path)
    try:
        st_pre = path.stat()
    except OSError:
        return {}
    size_pre = st_pre.st_size
    mtime_pre = st_pre.st_mtime

    existing = cache_files.get(key)
    if isinstance(existing, dict):
        if existing.get("size") == size_pre and existing.get("mtime") == mtime_pre:
            by_day = existing.get("by_day")
            if isinstance(by_day, dict):
                # Trim again on read: prevents unbounded growth in long
                # sessions whose cache entry was written before the cap
                # logic existed (forward-defense).
                return _trim_by_day(by_day, MAX_BY_DAY_DAYS)

    # Miss: walk + maybe persist.
    by_day = walk_jsonl_token_buckets(path)

    # Re-stat: detect concurrent append. If drift, skip persistence.
    try:
        st_post = path.stat()
    except OSError:
        return by_day
    if st_post.st_size != size_pre or st_post.st_mtime != mtime_pre:
        # File grew or was rewritten while we walked. Don't trust this
        # entry; let the next push re-walk a stable file.
        return by_day

    cache_files[key] = {"size": size_pre, "mtime": mtime_pre, "by_day": by_day}
    return by_day


# ---------------------------------------------------------------------------
# Window slicing
# ---------------------------------------------------------------------------


def slice_window(
    by_day: dict[str, DayBucket],
    *,
    since: datetime,
    until: datetime,
) -> DayBucket:
    """Sum the day-buckets whose YYYY-MM-DD key falls in ``[since, until]``.

    Returns a single rolled-up DayBucket with ``by_model`` aggregated.
    Empty input or non-overlapping window → zero-valued bucket.
    """
    since_d = since.astimezone(timezone.utc).date().isoformat()
    until_d = until.astimezone(timezone.utc).date().isoformat()
    out: DayBucket = {
        "input": 0,
        "cache_create": 0,
        "cache_read": 0,
        "output": 0,
        "by_model": {},
    }
    for day_key, bucket in by_day.items():
        if not (since_d <= day_key <= until_d):
            continue
        for k in ("input", "cache_create", "cache_read", "output"):
            out[k] = out.get(k, 0) + bucket.get(k, 0)
        for model, mbucket in (bucket.get("by_model") or {}).items():
            mout = out.setdefault("by_model", {}).setdefault(
                model, {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}
            )
            for k in ("input", "cache_create", "cache_read", "output"):
                mout[k] = mout.get(k, 0) + mbucket.get(k, 0)
    return out


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def estimate_cost(tokens_by_model: dict[str, Usage]) -> tuple[float, dict[str, float]]:
    """Compute total cost + per-model split from a ``by_model`` dict.

    Returns ``(total_usd, per_model_usd)``. Unknown models contribute to
    raw token counts elsewhere but DO NOT contribute here — they're
    excluded from cost. Same for ``COST_EXCLUDED_MODELS`` (today only
    ``<synthetic>``).

    Unknown-model breadcrumb: emit one ``mm: notice:`` per process per
    unknown model. Caller-facing text mirrors the existing
    ``upgrade.py`` notice prefix.
    """
    total = 0.0
    per_model: dict[str, float] = {}
    for model, usage in (tokens_by_model or {}).items():
        if model in COST_EXCLUDED_MODELS:
            continue
        prices = PRICING.get(model)
        if prices is None:
            if model not in _WARNED_UNKNOWN_MODELS:
                # Sanitize the model name before logging — strings come from
                # peer-controlled jsonls. A planted model like "x\x1b[2J" would
                # otherwise clear the user's terminal on stderr emit.
                from mind_meld.safety import safe_str

                sys.stderr.write(f"mm: notice: unknown model in pricing: {safe_str(model)}\n")
                _WARNED_UNKNOWN_MODELS.add(model)
            continue
        cost = (
            usage.get("input", 0) * prices["input"]
            + usage.get("cache_create", 0) * prices["cache_create"]
            + usage.get("cache_read", 0) * prices["cache_read"]
            + usage.get("output", 0) * prices["output"]
        ) / 1_000_000.0
        per_model[model] = cost
        total += cost
    return total, per_model


# ---------------------------------------------------------------------------
# Cache lifecycle (load / persist / warm / gc)
# ---------------------------------------------------------------------------


CacheMode = Literal["block", "warn"]


def is_cache_cold() -> bool:
    """Return True if the cache file is missing or its ``files`` dict is
    empty. Used by autopush to gate token-walk skip + by interactive mm
    push to detect the warm-needed state."""
    if not CACHE_PATH.exists():
        return True
    try:
        raw = CACHE_PATH.read_text(encoding="utf-8")
    except OSError:
        return True
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
    if not isinstance(parsed, dict) or parsed.get("version") != CACHE_VERSION:
        return True
    files = parsed.get("files")
    if not isinstance(files, dict) or len(files) == 0:
        return True
    return False


def warm_token_cache_inline(
    claude_dirs: Iterable[Path],
    *,
    deadline_s: float = DEFAULT_WARM_BUDGET_S,
) -> tuple[int, int]:
    """Walk every parent + subagent jsonl under each ``claude_dirs`` entry,
    populating the cache. Bounded by ``deadline_s`` wall-clock budget.

    Returns ``(walked, skipped_for_budget)``. Caller uses the counts to
    print a one-line user-facing notice when invoked from ``mm push``.

    Subagent walk: one level deeper than parent jsonls. Tokens-only
    contribution is enforced at the events.py side; this walker just
    populates the cache.
    """
    deadline = time.monotonic() + deadline_s
    walked = 0
    skipped = 0
    with locked_json_rmw(
        CACHE_PATH,
        default_factory=_empty_cache,
        on_contention="block",
    ) as ljson:
        if not ljson.is_locked:
            return 0, 0
        cache = _normalize_cache(ljson.data)
        ljson.data.clear()
        ljson.data.update(cache)
        files = ljson.data["files"]
        for claude_dir in claude_dirs:
            projects_root = claude_dir / "projects"
            if not projects_root.is_dir():
                continue
            for parent_jsonl in _iter_all_jsonls(projects_root):
                if time.monotonic() > deadline:
                    skipped += 1
                    continue
                get_or_compute(parent_jsonl, files, deadline_monotonic=deadline)
                walked += 1
    return walked, skipped


def _iter_all_jsonls(projects_root: Path) -> Iterable[Path]:
    """Yield parent + subagent jsonls under ``<projects_root>/<encoded>/``.

    Parent: ``<encoded>/*.jsonl`` (depth 1).
    Subagent: ``<encoded>/<session-uuid>/subagents/*.jsonl`` (depth 3).
    """
    try:
        with os.scandir(projects_root) as proj_iter:
            for proj_entry in proj_iter:
                if not proj_entry.is_dir(follow_symlinks=False):
                    continue
                # Parent jsonls.
                try:
                    with os.scandir(proj_entry.path) as f_iter:
                        for f_entry in f_iter:
                            if f_entry.name.endswith(".jsonl") and f_entry.is_file(
                                follow_symlinks=False
                            ):
                                yield Path(f_entry.path)
                            elif f_entry.is_dir(follow_symlinks=False):
                                # Look for subagents/ under this session dir.
                                yield from _iter_subagent_jsonls(Path(f_entry.path))
                except OSError:
                    continue
    except OSError:
        return


def _iter_subagent_jsonls(session_dir: Path) -> Iterable[Path]:
    sub = session_dir / "subagents"
    if not sub.is_dir():
        return
    try:
        with os.scandir(sub) as it:
            for entry in it:
                if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
    except OSError:
        return


def gc_cache_entries(*, max_age_s: float = 90 * 24 * 3600) -> int:
    """Reap cache entries whose underlying jsonl no longer exists OR whose
    most recent ``by_day`` key is older than ``max_age_s``.

    Returns the number of entries reaped. Called from ``mm gc`` (cli
    side wires this in).
    """
    cutoff_iso = (datetime.now(timezone.utc).date()).isoformat()
    # max_age_s converted to days for the by_day comparison.
    max_days = int(max_age_s / 86400)
    reaped = 0
    with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
        if not ljson.is_locked:
            return 0
        cache = _normalize_cache(ljson.data)
        files = cache["files"]
        if not isinstance(files, dict):
            ljson.data.clear()
            ljson.data.update(_empty_cache())
            return 0
        keep: dict[str, Any] = {}
        for key, entry in files.items():
            if not isinstance(entry, dict):
                reaped += 1
                continue
            if not Path(key).exists():
                reaped += 1
                continue
            by_day = entry.get("by_day") or {}
            if not isinstance(by_day, dict) or not by_day:
                reaped += 1
                continue
            most_recent = max(by_day.keys())
            if _days_between(most_recent, cutoff_iso) > max_days:
                reaped += 1
                continue
            keep[key] = entry
        cache["files"] = keep
        ljson.data.clear()
        ljson.data.update(cache)
    return reaped


def _days_between(a_iso: str, b_iso: str) -> int:
    try:
        a = datetime.fromisoformat(a_iso).date()
        b = datetime.fromisoformat(b_iso).date()
    except ValueError:
        return 0
    return abs((b - a).days)


__all__ = [
    "CACHE_PATH",
    "CACHE_VERSION",
    "COST_EXCLUDED_MODELS",
    "CacheEntry",
    "DEFAULT_WARM_BUDGET_S",
    "DayBucket",
    "MAX_BY_DAY_DAYS",
    "PRICING",
    "PRICING_LAST_UPDATED",
    "SUBSCRIPTION_CAVEAT",
    "Usage",
    "estimate_cost",
    "gc_cache_entries",
    "get_or_compute",
    "is_cache_cold",
    "parse_usage",
    "slice_window",
    "walk_jsonl_token_buckets",
    "warm_token_cache_inline",
]
