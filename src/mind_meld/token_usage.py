"""Per-jsonl Claude Code token-usage + skill-invocation measurement for
fleet-aware retro.

Walks ``~/.claude/projects/<encoded>/<uuid>.jsonl`` (parent sessions) and
``<uuid>/subagents/agent-*.jsonl`` (subagent invocations), producing two
views in ONE I/O pass:

  1. Token usage: sum ``message.usage`` from each assistant message into
     per-day buckets keyed by ``timestamp``. Per-model breakdown nested
     under ``by_model``.
  2. Skill invocations: detect each assistant ``tool_use`` block with
     ``name == "Skill"``, count by ``input.skill`` per day. Subagent skill
     invocations roll into the parent project's bucket via the same
     attribution rule used for tokens.

Per-jsonl cache at ``~/.config/mind-meld/session-tokens.json`` (flock-
guarded via ``mind_meld.lockedjson``) skips re-walks when ``size`` and
``mtime`` haven't drifted. v0.11.27+ entries carry both ``by_day`` and
``skills_by_day`` fields. Pre-v0.11.27 entries lack ``skills_by_day``;
the shape-upgrade gate in ``get_or_compute`` re-walks any such entry
once to populate both views (D2 from /plan-eng-review 2026-05-06).
Token data is preserved unchanged on the rebuild.

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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, TypedDict

from mind_meld.lockedjson import locked_json_rmw

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_PATH = Path.home() / ".config" / "mind-meld" / "session-tokens.json"
CACHE_VERSION = 1

# Schema fields that bucket-merge helpers walk and zero-bucket factories
# initialize. Adding a 5th token field (e.g. `cache_anthropic`) requires
# updating this tuple AND the `Usage`/`DayBucket` TypedDicts below; the
# helpers and factories pick up the change automatically. Keep frozen as
# a tuple so future contributors can't mutate it at runtime.
TOKEN_FIELDS: tuple[str, ...] = ("input", "cache_create", "cache_read", "output")

# Lower bound on a populated cache file's serialized size. An empty cache
# `{"version": 1, "files": {}}` rendered with `sort_keys=True, indent=2`
# is 32 bytes; even a single-entry populated cache exceeds 64. The stat
# heuristic in `is_cache_cold` treats files at or below this threshold
# as cold without parsing JSON. Documented constant so future schema
# additions revisit the threshold deliberately.
_MIN_WARM_CACHE_BYTES = 64

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


SkillBuckets = dict[str, dict[str, int]]
"""Per-day skill invocation counts: ``{YYYY-MM-DD: {skill_name: count}}``.
Bounded by ``MAX_BY_DAY_DAYS``. Skill names are stored verbatim (the raw
bytes from peer jsonls); sanitization happens at render time so cross-
machine aggregation matches byte-for-byte."""


class CacheEntry(TypedDict, total=False):
    """One per-jsonl entry in session-tokens.json."""

    size: int
    mtime: float
    by_day: dict[str, DayBucket]
    skills_by_day: SkillBuckets


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

_DATE_SUFFIX = re.compile(r"-\d{8}$")
"""Strip a trailing 8-digit YYYYMMDD date suffix from a model id.
Confirmed against real subagent jsonls on this Mac
(``claude-haiku-4-5-20251001`` → ``claude-haiku-4-5``)."""


def zero_model_bucket() -> Usage:
    """Return a fresh zero-valued ``Usage`` (per-model bucket — no
    ``by_model`` nesting). Used at every "create empty per-model entry"
    site across token_usage, events, and aggregator."""
    return {k: 0 for k in TOKEN_FIELDS}  # type: ignore[return-value]


def zero_day_bucket() -> DayBucket:
    """Return a fresh zero-valued ``DayBucket`` (per-day bucket — includes
    empty ``by_model`` map). Day buckets carry the per-model breakdown;
    model buckets do not. Used at every "create empty per-day entry"
    site."""
    bucket: DayBucket = {k: 0 for k in TOKEN_FIELDS}  # type: ignore[assignment]
    bucket["by_model"] = {}
    return bucket


def merge_usage_bucket(target: dict[str, Any], src: dict[str, Any]) -> None:
    """Sum ``TOKEN_FIELDS`` from ``src`` into ``target`` in place.

    Both dicts are treated as ``Usage``-shaped (or ``DayBucket``-shaped —
    the helper only touches the four flat fields, never ``by_model``).
    Missing keys in ``src`` contribute 0; missing keys in ``target`` are
    seeded to 0 then summed.

    NOT trust-boundary safe: assumes ``src`` values are int-coerced
    upstream (parse_usage handles peer-controlled jsonl input via
    ``_coerce_int``). The aggregator side keeps its bespoke loop with
    ``_safe_int`` because it walks peer-controlled events directly."""
    for k in TOKEN_FIELDS:
        target[k] = target.get(k, 0) + src.get(k, 0)


def merge_by_model(
    target_by_model: dict[str, Usage],
    src_by_model: dict[str, Usage],
) -> None:
    """Merge ``src_by_model`` into ``target_by_model`` in place.

    For each model in src, ``setdefault(zero_model_bucket())`` then
    delegates to ``merge_usage_bucket``. Same trust-boundary caveat as
    ``merge_usage_bucket`` — for trusted local data only."""
    for model, mbucket in src_by_model.items():
        mtarget = target_by_model.setdefault(model, zero_model_bucket())
        merge_usage_bucket(mtarget, mbucket)


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


def walk_jsonl_buckets(path: Path) -> tuple[dict[str, DayBucket], SkillBuckets]:
    """Walk a single jsonl in ONE I/O pass, producing both views:

      - Token by_day: ``{YYYY-MM-DD: DayBucket}`` summed from
        ``message.usage`` on assistant messages.
      - Skill skills_by_day: ``{YYYY-MM-DD: {skill_name: count}}`` from
        each assistant ``tool_use`` block with ``name == "Skill"``.

    Both views capped to the most recent ``MAX_BY_DAY_DAYS`` days.

    Token dedup is by ``message.id`` — Claude Code logs each model
    iteration as a separate jsonl line under the same ``message.id``,
    and the ``usage`` field on each iteration is the SAME cumulative
    total. Walking each iteration would double-count tokens.

    Skill dedup is by ``tool_use.id`` (independently of message dedup).
    Each iteration of an assistant message produces DIFFERENT content
    blocks: the first iteration may be text-only, the second may carry
    the Skill tool_use block. They share ``message.id``, so deduping
    skills by message.id would drop the second iteration entirely
    (the bug we caught at smoke-test time on real Claude Code data).
    Tool-use ids are Anthropic's ``toolu_*`` format and unique across
    the session.

    Skips (both views):
      - non-JSON lines (``json.JSONDecodeError``)
      - non-assistant messages
      - messages without a parseable ``timestamp``

    Skill detection ignores blocks where ``input`` is not a dict, or
    ``input.skill`` is not a non-empty string. Tool_use blocks for
    other tools (Edit, Bash, etc.) are not counted.

    Returns ``({}, {})`` on any I/O failure — caller treats as cache
    miss for next push.
    """
    by_day: dict[str, DayBucket] = {}
    skills_by_day: SkillBuckets = {}
    seen_msg_ids: set[str] = set()
    seen_tool_ids: set[str] = set()
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
                day = _extract_day(obj.get("timestamp"))
                if day is None:
                    continue
                # Token side: dedup by message.id (each iteration carries
                # the same cumulative usage; counting twice would double).
                if msg_id is None or msg_id not in seen_msg_ids:
                    if msg_id is not None:
                        seen_msg_ids.add(msg_id)
                    _accumulate(by_day, day, usage, model)
                # Skill side: dedup by tool_use.id (each iteration may
                # contribute DIFFERENT tool_use blocks under the same
                # message.id; deduping by message.id loses them).
                _accumulate_skills(skills_by_day, day, msg, seen_tool_ids)
    except OSError:
        return {}, {}
    return _trim_by_day(by_day, MAX_BY_DAY_DAYS), _trim_skills_by_day(
        skills_by_day, MAX_BY_DAY_DAYS
    )


def walk_jsonl_token_buckets(path: Path) -> dict[str, DayBucket]:
    """Backwards-compat shim for callers that only want the token view.

    Pre-v0.11.27 this was the canonical walker. Now ``walk_jsonl_buckets``
    is canonical and returns both views; this shim drops the skill view.
    Tests + external callers that pre-date the skill view continue to
    work unchanged. New code should call ``walk_jsonl_buckets`` directly.
    """
    by_day, _ = walk_jsonl_buckets(path)
    return by_day


def _accumulate_skills(
    skills_by_day: SkillBuckets,
    day: str,
    msg: Any,
    seen_tool_ids: set[str],
) -> None:
    """Walk an assistant ``message.content`` list for ``tool_use`` blocks
    where ``name == "Skill"`` and bump ``skills_by_day[day][skill_name]``.

    Dedup by ``tool_use.id`` (Anthropic ``toolu_*`` format) — each
    iteration of an assistant message may emit different content blocks
    under the same ``message.id``, so message-id dedup drops legitimate
    skill calls. Tool-use ids are unique per call; deduping by them
    handles both the streaming-iteration case (different tool_use ids
    per iteration → all counted) and the rare retry case (same tool_use
    id → counted once).

    Tolerant of every shape:
      - non-dict message → skip
      - non-list content → skip
      - non-dict block → skip
      - block.type != "tool_use" → skip (ignore, no error)
      - block.name != "Skill" → skip (other tools, not counted)
      - block.id missing/non-string → still count, just no dedup possible
        (degrade to over-count rather than under-count if the id format
        ever changes)
      - input not a dict, input.skill non-string or empty → skip silently
    """
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    bucket = skills_by_day.setdefault(day, {})
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") != "tool_use" or blk.get("name") != "Skill":
            continue
        inp = blk.get("input")
        if not isinstance(inp, dict):
            continue
        skill = inp.get("skill")
        if not isinstance(skill, str) or not skill:
            continue
        tool_id = blk.get("id")
        if isinstance(tool_id, str) and tool_id:
            if tool_id in seen_tool_ids:
                continue
            seen_tool_ids.add(tool_id)
        bucket[skill] = bucket.get(skill, 0) + 1


def _trim_skills_by_day(skills_by_day: SkillBuckets, max_days: int) -> SkillBuckets:
    """Cap skills_by_day to the most recent ``max_days`` keys.

    Iso-8601 dates lex-sort identically to date-sort; safe without parsing.
    Empty per-day buckets (set up by ``_accumulate_skills`` for days with
    assistant messages but no Skill blocks) are dropped — empty maps are
    noise both in the cache and in the rendered output."""
    pruned = {k: v for k, v in skills_by_day.items() if v}
    if len(pruned) <= max_days:
        return pruned
    keep_keys = sorted(pruned.keys(), reverse=True)[:max_days]
    return {k: pruned[k] for k in keep_keys}


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
    bucket = by_day.setdefault(day, zero_day_bucket())
    merge_usage_bucket(bucket, usage)
    # zero_day_bucket() guarantees `by_model` is present; existing
    # buckets were created the same way. No setdefault needed.
    model_bucket = bucket["by_model"].setdefault(model, zero_model_bucket())
    merge_usage_bucket(model_bucket, usage)


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
) -> tuple[dict[str, DayBucket], SkillBuckets]:
    """Return ``(by_day, skills_by_day)`` for ``path``, hitting the cache
    when possible.

    Mutates ``cache_files`` in place: on a miss, the new entry is stored.
    Caller passes the ``files`` sub-dict of the locked cache; the helper's
    one job is to populate it consistently.

    Cache hit semantics: same ``size`` AND same ``mtime`` (within 1µs)
    AND ``skills_by_day`` field present → reuse cached views. The
    field-presence requirement is the v0.11.27 shape-upgrade gate (D2
    from /plan-eng-review 2026-05-06): pre-v0.11.27 cache entries lack
    the field; we re-walk those once to populate both views in one I/O
    pass. Token data is preserved across the rebuild because
    ``walk_jsonl_buckets`` re-derives both from the same source.

    Concurrent append safety: stat once, walk, stat again. If size or
    mtime drifted during the walk, treat as a miss and DO NOT persist
    the (potentially partial) walk — let the next push pick up the
    settled file.

    Deadline: if ``deadline_monotonic`` is set and we hit it BEFORE
    starting the walk, return whatever's cached (or empty) without
    touching the cache. Pre-v0.11.27 entries (no skills field) return
    cached tokens + ``{}`` skills under deadline pressure — first
    push under budget completes the upgrade.
    """
    if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
        # Out of budget — return cached value if present, else empty.
        key = _resolve_path(path)
        existing = cache_files.get(key)
        if isinstance(existing, dict):
            by_day = existing.get("by_day")
            sk = existing.get("skills_by_day")
            return (
                by_day if isinstance(by_day, dict) else {},
                sk if isinstance(sk, dict) else {},
            )
        return {}, {}

    key = _resolve_path(path)
    try:
        st_pre = path.stat()
    except OSError:
        return {}, {}
    size_pre = st_pre.st_size
    mtime_pre = st_pre.st_mtime

    existing = cache_files.get(key)
    if isinstance(existing, dict):
        # D2 shape-upgrade gate: ``"skills_by_day" in existing`` is the
        # version discriminator. Pre-v0.11.27 entries match size/mtime
        # but lack the field → fall through to walk. Token data on
        # those entries is identical to what the walk re-derives.
        if (
            existing.get("size") == size_pre
            and existing.get("mtime") == mtime_pre
            and "skills_by_day" in existing
        ):
            by_day = existing.get("by_day")
            sk = existing.get("skills_by_day")
            if isinstance(by_day, dict) and isinstance(sk, dict):
                # Trim again on read: prevents unbounded growth in long
                # sessions whose cache entry was written before the cap
                # logic existed (forward-defense).
                return (
                    _trim_by_day(by_day, MAX_BY_DAY_DAYS),
                    _trim_skills_by_day(sk, MAX_BY_DAY_DAYS),
                )

    # Miss: walk + maybe persist.
    by_day, skills_by_day = walk_jsonl_buckets(path)

    # Re-stat: detect concurrent append. If drift, skip persistence.
    try:
        st_post = path.stat()
    except OSError:
        return by_day, skills_by_day
    if st_post.st_size != size_pre or st_post.st_mtime != mtime_pre:
        # File grew or was rewritten while we walked. Don't trust this
        # entry; let the next push re-walk a stable file.
        return by_day, skills_by_day

    cache_files[key] = {
        "size": size_pre,
        "mtime": mtime_pre,
        "by_day": by_day,
        "skills_by_day": skills_by_day,
    }
    return by_day, skills_by_day


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
    out: DayBucket = zero_day_bucket()
    for day_key, bucket in by_day.items():
        if not (since_d <= day_key <= until_d):
            continue
        merge_usage_bucket(out, bucket)
        # zero_day_bucket() guarantees `by_model` is present.
        merge_by_model(out["by_model"], bucket.get("by_model") or {})
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
        cost = sum(usage.get(k, 0) * prices[k] for k in TOKEN_FIELDS) / 1_000_000.0
        per_model[model] = cost
        total += cost
    return total, per_model


# ---------------------------------------------------------------------------
# Cache lifecycle (load / persist / warm / gc)
# ---------------------------------------------------------------------------


CacheMode = Literal["block", "warn"]


@contextmanager
def lock_and_get_files(on_contention: CacheMode) -> Iterator[dict[str, Any] | None]:
    """Open the token cache under flock and yield the ``files`` dict
    (or ``None`` on warn-mode contention).

    Replaces the ``locked_json_rmw + version-check + isinstance-check +
    ljson.data['files']`` boilerplate at every cache call site (cli.py's
    ``_run_events_tail`` / ``_run_events_backfill``,
    ``warm_token_cache_inline``, ``gc_cache_entries``). Owner of cache-
    shape invariants is this module, not the caller.

    ``None`` semantics: yielded ONLY for ``on_contention="warn"`` after
    retry budget exhaustion. Version mismatch and malformed ``files``
    dict are normalized in place to empty INSIDE the lock — the caller
    still gets a (now-empty) dict back, NOT None. This is load-bearing:
    callers branch on ``files is None`` to mean "couldn't get the lock,
    skip token aggregation," NOT "cache was empty/corrupt."

    Block mode never yields None (the flock blocks until acquired).
    Raise mode propagates ``LockContended`` to the caller; we don't
    catch it here.

    Inherits ``skip_unchanged_write=True`` from ``locked_json_rmw``'s
    default — no-op contexts skip the disk write."""
    with locked_json_rmw(
        CACHE_PATH,
        default_factory=_empty_cache,
        on_contention=on_contention,
        contention_warning="token cache contended; skipping token aggregation",
    ) as ljson:
        if not ljson.is_locked:
            yield None
            return
        # Normalize cache shape under the lock. The version-check + files
        # isinstance check used to live inline at every caller site; lift
        # both here so the cache-shape invariant has one owner.
        if ljson.data.get("version") != CACHE_VERSION:
            ljson.data.clear()
            ljson.data.update(_empty_cache())
        else:
            # Strict canonical-root shape: drop unknown top-level keys.
            # The v1 schema defines exactly two top-level keys, ``version``
            # and ``files``. Pre-v0.12.4 ``gc_cache_entries`` did this via
            # ``ljson.data.clear() + update({"version": ..., "files": ...})``
            # at the end of every gc pass; lifting that root-sanitization
            # into the wrapper closes the gap where a current-version cache
            # with extra top-level junk (e.g. ``"padding": "<huge>"``) would
            # otherwise survive every read AND every gc forever.
            extras = [k for k in ljson.data if k not in ("version", "files")]
            for k in extras:
                del ljson.data[k]
        if not isinstance(ljson.data.get("files"), dict):
            ljson.data["files"] = {}
        yield ljson.data["files"]


def is_cache_cold() -> bool:
    """Return True if the cache file is missing, too small, corrupt,
    wrong-version, or has an empty ``files`` dict.

    Used by ``_decide_token_walk_policy`` to gate the cold-cache warm
    path. Cost: ~µs on missing/small files (early stat shortcut) and
    ~1.5ms on a populated 320KB cache (full ``json.loads`` parse, same
    cost as the pre-Track-10A baseline).

    Returns True when:
      * the cache file does not exist, OR
      * ``stat()`` raises ``OSError`` (e.g. EACCES on chmod-restricted
        ``~/.config/mind-meld/``) — degrade to "cold," safe default
        that triggers a warm attempt, OR
      * the file size is below ``_MIN_WARM_CACHE_BYTES`` (64) — even
        the empty-cache JSON exceeds this, so smaller is always cold,
        OR
      * read or JSON parse fails (corrupt content / bad UTF-8 /
        non-dict top-level), OR
      * the parsed ``"version"`` field doesn't match
        ``CACHE_VERSION``, OR
      * the parsed ``"files"`` field is missing, non-dict, or empty.

    Returns False only when the file is structurally valid AND has
    matching version AND has a non-empty files dict.

    Why structural parsing: Track 10A briefly tried a regex byte-scan
    optimization (~6× faster) but Codex adversarial review caught two
    correctness bugs: (1) a corrupt cache with the substring
    ``"version": 1`` would scan as warm, and (2) a wrong-version cache
    with a nested ``"version"`` field would also scan as warm. Both
    re-enable the thinned-snapshot autopush path the cold-cache gate
    is supposed to prevent. ``json.loads`` is the only sound approach.

    Race tolerance: this read is unlocked. Between the writer's
    ``ftruncate(0)`` and the subsequent ``write(payload)`` of a real
    update, an unlocked read could observe size 0 or partial bytes. We
    treat that as cold; the caller then attempts warm, blocks on the
    writer's flock, and eventually walks the freshly-written cache.
    Idempotent."""
    try:
        st = CACHE_PATH.stat()
    except OSError:
        # FileNotFoundError is a subclass of OSError; both → cold.
        return True
    if st.st_size < _MIN_WARM_CACHE_BYTES:
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
    with lock_and_get_files("block") as files:
        if files is None:
            # Block mode never yields None in practice (flock blocks until
            # acquired). Defensive zero return preserves the prior contract.
            return 0, 0
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

    Routes through ``lock_and_get_files`` so the version-check + ``files``-
    isinstance-check normalization lives in ONE place. ``mm gc`` is a
    user-invoked maintenance command — ``"block"`` mode is the right
    choice (wait for contention rather than skip cleanup); a future
    contributor copy-pasting from ``_run_events_tail``'s ``"warn"`` mode
    would silently make ``mm gc`` a no-op under contention.
    """
    cutoff_iso = (datetime.now(timezone.utc).date()).isoformat()
    # max_age_s converted to days for the by_day comparison.
    max_days = int(max_age_s / 86400)
    reaped = 0
    # block: mm gc waits for contention by design (see docstring above).
    with lock_and_get_files("block") as files:
        if files is None:
            # Block mode never yields None in practice (flock blocks until
            # acquired). Defensive zero return preserves the prior contract.
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
        # In-place mutation: lock_and_get_files yields the `files` sub-dict,
        # not the cache root. Replacing via assignment would not persist —
        # the wrapper writes back ljson.data, of which `files` is a
        # reference. clear()+update() preserves the reference identity.
        files.clear()
        files.update(keep)
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
    "SkillBuckets",
    "TOKEN_FIELDS",
    "Usage",
    "estimate_cost",
    "gc_cache_entries",
    "get_or_compute",
    "is_cache_cold",
    "lock_and_get_files",
    "merge_by_model",
    "merge_usage_bucket",
    "parse_usage",
    "slice_window",
    "walk_jsonl_buckets",
    "walk_jsonl_token_buckets",
    "warm_token_cache_inline",
    "zero_day_bucket",
    "zero_model_bucket",
]
