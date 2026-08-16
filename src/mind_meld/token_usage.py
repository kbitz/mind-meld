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

Incremental resume (v0.12.15): a cache MISS no longer implies a full
re-parse. Session jsonls are append-only and routinely reach 10 MB, so
re-reading one end-to-end because it grew by a few hundred bytes made
the events tail cost O(total bytes on disk) — the real cause of the
recurring ``mm: notice: events tail budget exceeded``. Entries carry
``offset`` (resume point), ``head`` and ``head_len`` (the fingerprint
proving the file is still the same file), and ``tail_msg_ids``
(cross-boundary dedup seed);
``_resume_plan`` gates their use and falls back to a full walk on any
doubt. A warm walk is now O(bytes appended since the last push).

Pricing is module-level and resolved through ``resolve_prices`` — the
single predicate for "is this model priced, and at what rates." Exact
model IDs win; anything else falls back to its FAMILY tier so a model
released after this table was written still prices in the right
ballpark instead of silently costing $0. Only genuinely unparseable IDs
stay unpriced; they count toward token totals but are excluded from cost
(the renderer surfaces this fact). ``PRICING_LAST_UPDATED`` is rendered
onto the retro card so a human can judge staleness — mm has no network
by design, so the table can never self-update and stale is the steady
state, not the exception.

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

import copy
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, NamedTuple, TypedDict

from mind_meld.lockedjson import (
    LockedJsonSnapshot,
    locked_json_rmw,
    locked_json_snapshot,
)
from mind_meld.safety import safe_str

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

# Chunk size for draining past an oversize line. Small enough that the
# drain never rivals MAX_JSONL_LINE_BYTES in resident memory.
_DRAIN_CHUNK_BYTES = 1024 * 1024

# Per-process state for the line-size warn-once breadcrumb.
_WARNED_OVERSIZE_PATHS: set[str] = set()

# --- Incremental resume (v0.12.15) ----------------------------------------
# Claude Code session jsonls are append-only and routinely reach 10 MB.
# Re-parsing one end-to-end because it grew by 200 bytes made the events
# tail O(total bytes on disk) and was the real cause of the recurring
# `mm: notice: events tail budget exceeded`. Cache entries carry a resume
# offset so a warm walk is O(bytes appended since the last push).

_HEAD_PROBE_BYTES = 4096
"""Upper bound on the head window hashed to prove the file on disk is
still the one that produced a cache entry's ``offset``. A pure
size-grew check can't tell an append from a rewrite that happens to be
longer. Claude Code's first line carries sessionId + cwd + timestamp,
so 4 KiB is comfortably discriminating; the cost is one extra read per
cache miss.

The ACTUAL window is ``head_probe_len(offset)`` and is persisted per
entry as ``head_len`` — see that function for why a fixed window is
wrong for short files.

This is a forensic cache — a false match costs a wrong retro number,
not data. It bounds identity, not integrity: a rewrite that preserves
the probed prefix and lands at or above the cached size still passes,
and the ordinary size+mtime cache hit never consults the probe at all.
That is accepted, not overlooked."""

TAIL_MSG_ID_LOOKBACK = 8
"""How many trailing assistant ``message.id`` values a cache entry
carries forward to seed the next segment's dedup set.

No line is ever parsed by two segments, so the ONLY cross-boundary
double-count risk is a message whose repeated iterations straddle the
resume point (iterations 1-2 in this push, 3-4 in the next; each
iteration re-states the same cumulative usage).

The bound is MEASURED, not guessed. Across 358 live session jsonls:
26,989 assistant lines repeat an already-seen ``message.id``, and ZERO
of those repeats are separated by even one other distinct id — every
repeated run is strictly contiguous. A lookback of 1 would therefore
suffice today; 8 is 8x headroom, costs ~320 bytes per cache entry, and
``_carry_tail_ids`` keeps the window in recency order so a re-seen id
can't be evicted while its message is still in flight.

Residual risk, stated rather than engineered away: a duplicate id
separated by more than 8 distinct messages (a compaction artifact or
retry pattern not present in any measured file) over-counts that one
message's usage once. Bounded, forensic, and self-healing on the next
full walk."""

_MAX_TAIL_MSG_ID_LEN = 128
"""Length cap on a carried-forward ``message.id``.

``message.id`` is read out of a jsonl and a single line may be up to
``MAX_JSONL_LINE_BYTES``. Without a cap, eight near-16 MiB ids would
serialize into a cache larger than ``lockedjson``'s 64 MiB read
ceiling — every later read then sees oversized JSON, resets the cache,
and the fleet pays a cold walk on every push, permanently. Real
Anthropic ids measure 36 chars, so 128 is generous.

Over-long ids are DROPPED from the seed rather than truncated:
truncation could alias two distinct ids into one and silently
under-count. Dropping degrades to the pre-existing over-count-once
behaviour for that one message. Mirrors the peer-controlled-string
clamping convention in ``safety.py`` and the aggregator's
``_safe_short``."""

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

PRICING_LAST_UPDATED = "2026-08-11"
"""Date the rates below were last verified against Anthropic's public
pricing page. Rendered onto the retro card (aggregator's caveat line) so
the reader can judge staleness themselves. Deliberately NOT a threshold:
mm has no network by design, so this table cannot self-update and a
"warn after N months" rule would be a verdict the code has not earned.
The v0.12.13 miss was three months inside a six-month window."""

# Cache read/write multipliers. Anthropic prices both as fixed multiples
# of a model's input rate, uniformly across every tier — a cache read at
# 0.1x input, a cache write at 1.25x (5-minute TTL) or 2x (1-hour TTL).
# Both verified against the published pricing table on
# PRICING_LAST_UPDATED.
#
# On the write multiplier specifically: Claude Code writes at
# the 1h TTL by default (sessions dropped to 5m only under usage
# overage); a sample of local session jsonls measured 83% of
# cache_create tokens at 1h. The synced wire format carries ONE
# ``cache_create`` total with no TTL split, so 2x is the closest
# available approximation — it overstates the 5m slice by 0.75x
# (~+3.5% of a typical window's total) where the old 1.25x understated
# the whole line by ~11%. Exact per-TTL pricing needs a wire-format
# change; see TODOS.md.
_CACHE_WRITE_MULT = 2.0
_CACHE_READ_MULT = 0.1


def _tier(input_rate: float, output_rate: float) -> dict[str, float]:
    """Build a full four-field rate card from the two published rates.

    Anthropic publishes input and output per-MTok; cache read and cache
    write are fixed multiples of input. Deriving them here means a new
    model is two numbers, not four, and the multiples can never drift
    apart between entries."""
    return {
        "input": input_rate,
        "cache_read": input_rate * _CACHE_READ_MULT,
        "cache_create": input_rate * _CACHE_WRITE_MULT,
        "output": output_rate,
    }


# Per-model rate OVERRIDES (per-million-token, USD, list price). Checked
# FIRST by ``resolve_prices``; ``MODEL_FAMILY_TIERS`` below is the normal
# path. Only add an entry here when a model's rates genuinely DIFFER from
# its family tier.
#
# Entries here must GENUINELY differ from their family tier. A duplicate
# would recreate the multi-site drift this release removes (an Opus rate
# change needing N identical edits, one forgotten, some models silently
# on the old rate) — `test_pricing_holds_no_redundant_entries` fails the
# build on one.
#
# The Opus family is NOT rate-uniform across generations: 4.0 and 4.1
# billed $15/$75, and the tier dropped to $5/$25 at 4.5. Without these
# overrides a `claude-opus-4-1` record resolves to the modern tier and
# prices 3x LOW under a confident `~` — where before v0.12.13 it was
# unpriced and raised a loud `>=` plus a Notes line. That is a downgrade
# in signal, not an upgrade, and it is reachable: Opus 4.1 retired
# 2026-08-05 and `MAX_BY_DAY_DAYS` keeps 90 days of history.
# (Caught by the /review adversarial pass; the claim that retired models
# never appear in live data was asserted, not checked.)
#
# Claude 3-era ids need no entry: `claude-3-opus-20240229` normalizes to
# `claude-3-opus`, whose segment 1 is "3" — not a family — so it stays
# unpriced and surfaces honestly.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-1": _tier(15.0, 75.0),
    "claude-opus-4-0": _tier(15.0, 75.0),
}

# Family-tier fallback. Keyed on the FAMILY segment of a
# ``claude-<family>-<version>`` id. A model released after this table was
# written resolves to its family's current-generation rates rather than
# silently costing nothing.
#
# Known inaccuracy, accepted deliberately: tiers reflect CURRENT-
# generation rates, so a retired model would be priced wrong (Opus 3 was
# $15/$75, not $5/$25). That's fine because retired models don't appear
# in live session data, and being ~3x wrong on a model nobody runs beats
# being infinitely wrong on the model everybody runs.
#
# SOURCE OF THE NUMBERS (input/output per MTok, list price), all verified
# against Anthropic's published pricing table on PRICING_LAST_UPDATED.
# Recording the provenance is the point: the bug being fixed here was a
# rate nobody had ever re-checked, so a tier with no citation is the same
# failure waiting to recur.
#   fable / mythos  $10 / $50   (Claude Fable 5, Claude Mythos 5)
#   opus            $5  / $25   (Opus 5, 4.8, 4.7, 4.6, 4.5 — NOT 4.1/4.0,
#                                which billed $15/$75; see PRICING above)
#   sonnet          $3  / $15   (Sonnet 5 list; its $2/$10 introductory
#                                rate through 2026-08-31 is not list price)
#   haiku           $1  / $5    (Haiku 4.5)
MODEL_FAMILY_TIERS: dict[str, dict[str, float]] = {
    "fable": _tier(10.0, 50.0),
    "mythos": _tier(10.0, 50.0),
    "opus": _tier(5.0, 25.0),
    "sonnet": _tier(3.0, 15.0),
    "haiku": _tier(1.0, 5.0),
}

# Models we deliberately count tokens for but exclude from cost. Today
# only the synthetic placeholder Claude Code uses for internal /
# tool-execution turns.
COST_EXCLUDED_MODELS: frozenset[str] = frozenset({"<synthetic>"})

# Models we've already warned about as "unknown" — meaning unresolvable
# by ``resolve_prices``, i.e. absent from BOTH ``PRICING`` and
# ``MODEL_FAMILY_TIERS``. A model merely missing from ``PRICING`` but
# covered by its family tier is priced normally and never warned about.
# One-shot per process to avoid spamming repeat pushes. Reset on
# interpreter exit; next mm invocation re-warns once.
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

    NOT trust-boundary safe for MAGNITUDE: assumes ``src`` values are
    int-coerced upstream (parse_usage handles peer-controlled jsonl input
    via ``_coerce_int``). The aggregator side keeps its bespoke loop with
    ``_safe_int`` because it walks peer-controlled events directly.

    It IS defensive about TYPE, because v0.12.15 made the on-disk cache a
    merge SOURCE (the incremental resume path) rather than only a return
    value. A single non-int value in one cached bucket would otherwise
    raise ``TypeError`` out of ``get_or_compute``, which ``_run_events_tail``
    catches as `events tail failed` on EVERY push while the poisoned entry
    survives — a permanent outage from one bad key. Non-ints contribute 0."""
    for k in TOKEN_FIELDS:
        base = target.get(k, 0)
        add = src.get(k, 0)
        if not isinstance(base, int) or isinstance(base, bool):
            base = 0
        if not isinstance(add, int) or isinstance(add, bool):
            add = 0
        target[k] = base + add


def merge_by_model(
    target_by_model: dict[str, Usage],
    src_by_model: dict[str, Usage],
) -> None:
    """Merge ``src_by_model`` into ``target_by_model`` in place.

    For each model in src, ``setdefault(zero_model_bucket())`` then
    delegates to ``merge_usage_bucket``. Same trust-boundary caveat as
    ``merge_usage_bucket`` — for trusted local data only. Non-dict model
    buckets are skipped for the same reason as ``merge_token_days``: a
    malformed cache entry must not raise through the events tail."""
    for model, mbucket in src_by_model.items():
        if not isinstance(mbucket, dict):
            continue
        mtarget = target_by_model.setdefault(model, zero_model_bucket())
        merge_usage_bucket(mtarget, mbucket)


def merge_token_days(
    target: dict[str, DayBucket],
    src: dict[str, Any],
) -> None:
    """Merge a whole ``{YYYY-MM-DD: DayBucket}`` map into ``target`` in place.

    The day-level counterpart to ``merge_usage_bucket`` / ``merge_by_model``,
    which work on a SINGLE bucket. Every caller that merges two per-day maps
    was hand-rolling the same three lines; the v0.12.15 incremental-resume
    merge in ``get_or_compute`` would have been the fifth copy. See the
    ``mirrored-predicate-drifts-when-one-side-gains-logic`` pitfall — this
    module has already shipped that bug twice.

    Same VALUE trust-boundary caveat as ``merge_usage_bucket`` (ints are
    assumed coerced upstream), but SHAPE is handled defensively: a
    non-dict day bucket is skipped rather than raising. Both callers read
    from the on-disk cache, and a single malformed entry raising here
    would take down the whole events tail on EVERY push while the
    poisoned entry survives — a permanent outage from one bad key."""
    for day, bucket in src.items():
        if not isinstance(bucket, dict):
            continue
        day_target = target.setdefault(day, zero_day_bucket())
        merge_usage_bucket(day_target, bucket)
        # zero_day_bucket() guarantees `by_model` is present.
        by_model = bucket.get("by_model")
        if isinstance(by_model, dict):
            merge_by_model(day_target["by_model"], by_model)


def merge_skill_days(target: SkillBuckets, src: SkillBuckets) -> None:
    """Merge a whole ``{YYYY-MM-DD: {skill: count}}`` map into ``target``
    in place. The skills-side counterpart to ``merge_token_days`` — before
    v0.12.15 the skills half had no helper at all and was hand-rolled at
    every site. Same defensive-shape rule: malformed buckets are skipped,
    never raised."""
    for day, sbucket in src.items():
        if not isinstance(sbucket, dict):
            continue
        day_target = target.setdefault(day, {})
        for skill, count in sbucket.items():
            if not isinstance(count, int) or isinstance(count, bool):
                continue
            day_target[skill] = day_target.get(skill, 0) + count


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


def model_family(s: str) -> str | None:
    """Extract the family segment from a ``claude-<family>-<version>`` id.

    Returns the family (``"opus"``, ``"sonnet"``, ...) only for ids that
    parse structurally AND name a family in ``MODEL_FAMILY_TIERS``.
    Returns ``None`` for everything else, including ``<synthetic>``.

    NOT a priced-predicate. ``model_family(m) is not None`` looks like
    one and is subtly wrong — it disagrees with ``resolve_prices`` for
    any id carried by a ``PRICING`` override rather than a family tier.
    ``resolve_prices`` is the only correct answer to "is this priced";
    see Invariant 1 in ``docs/invariants/events-retro.md``. This is
    exported because ``aggregator._short_model_name`` needs the family
    allowlist for RENDERING (so token_usage stays the single owner of
    what a family is), not to make pricing decisions.

    TRUST BOUNDARY — this reads structure out of a peer-controlled
    string. Model ids cross the sync boundary (peer's Claude Code jsonl
    -> mm-events -> this machine's aggregator), and as of v0.12.13 they
    drive a *pricing* decision rather than display alone. The match is
    therefore POSITIONAL against a literal allowlist, never a substring
    test: a substring match would let a planted ``claude-haiku-opus-4-5``
    bill at Opus rates. Mirrors the validate-at-construction convention
    in ``storage/keys.py``.

    A future id scheme that doesn't fit ``claude-<family>-...`` degrades
    to unpriced (surfaced in the retro's Notes line), which is the safe
    direction — silence is what v0.12.13 was fixing.
    """
    if not isinstance(s, str) or not s:
        return None
    parts = s.split("-")
    if len(parts) < 3 or parts[0] != "claude":
        return None
    # Segment 2 must be non-empty: a truncated id like "claude-opus-"
    # splits to ["claude", "opus", ""], which would otherwise satisfy the
    # length check and bill garbage at full Opus rates instead of
    # degrading to unpriced.
    if not parts[2]:
        return None
    family = parts[1]
    return family if family in MODEL_FAMILY_TIERS else None


def resolve_prices(model: str) -> dict[str, float] | None:
    """Return the per-MTok rate card for ``model``, or ``None`` if unpriced.

    THE single predicate for "is this model priced." Both consumers go
    through it — ``estimate_cost`` for the rates, the aggregator's
    ``_unpriced_token_summary`` for the ``is None`` count — so the cost
    line and the "N unpriced model(s)" Notes line can never disagree.
    Two independent ``model in PRICING`` tests are what made that
    contradiction possible before v0.12.13; do NOT reintroduce one.

    Resolution order: ``COST_EXCLUDED_MODELS`` (``<synthetic>``) is not
    this function's concern and is filtered by the caller; exact
    ``PRICING`` entry wins; else the family tier; else ``None``.

    Returns a COPY. The rate cards are module-level and shared; handing
    out the live dict lets one careless caller mutate pricing for the
    whole process.
    """
    prices = PRICING.get(model)
    if prices is not None:
        return dict(prices)
    family = model_family(model)
    if family is None:
        return None
    return dict(MODEL_FAMILY_TIERS[family])


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


class JsonlSegment(NamedTuple):
    """One contiguous byte range of a jsonl, parsed into both views.

    ``end_offset`` is the byte offset one past the last COMPLETE line
    consumed — a partially-written trailing line is left unparsed and
    unaccounted for, so the next walk re-reads it once it has settled.

    ``tail_msg_ids`` carries the trailing ``message.id`` values forward
    so the next segment can dedup a message whose iterations straddle
    the boundary. See ``TAIL_MSG_ID_LOOKBACK``.

    ``ok`` is False when the read failed outright (``OSError`` on open).
    Callers MUST NOT persist a cache entry built from a failed read: the
    stat calls that bracket the walk would still agree, so the entry
    would record the file's CURRENT size/mtime against buckets that
    never saw its current bytes — a permanent cache hit that silently
    stops accounting for the session forever.
    """

    by_day: dict[str, DayBucket]
    skills_by_day: SkillBuckets
    end_offset: int
    tail_msg_ids: tuple[str, ...]
    ok: bool = True


def walk_jsonl_segment(
    path: Path,
    *,
    start_offset: int = 0,
    seed_msg_ids: Iterable[str] = (),
) -> JsonlSegment:
    """Parse ``path`` from ``start_offset`` to EOF in ONE I/O pass,
    producing both views:

      - Token by_day: ``{YYYY-MM-DD: DayBucket}`` summed from
        ``message.usage`` on assistant messages.
      - Skill skills_by_day: ``{YYYY-MM-DD: {skill_name: count}}`` from
        each assistant ``tool_use`` block with ``name == "Skill"``.

    Buckets are returned UNTRIMMED — the caller trims after merging with
    any cached prefix, so a day that falls out of the ``MAX_BY_DAY_DAYS``
    window can't be resurrected half-populated. ``walk_jsonl_buckets`` is
    the trimming full-file shim over this function.

    Token dedup is by ``message.id`` — Claude Code logs each model
    iteration as a separate jsonl line under the same ``message.id``,
    and the ``usage`` field on each iteration is the SAME cumulative
    total. Walking each iteration would double-count tokens. On a
    resumed walk the dedup set is seeded from ``seed_msg_ids`` (the
    prior segment's ``tail_msg_ids``) — no line is ever parsed twice,
    but a message whose ITERATIONS straddle the boundary would
    otherwise be counted once per segment.

    Skill dedup is by ``tool_use.id`` (independently of message dedup).
    Each iteration of an assistant message produces DIFFERENT content
    blocks: the first iteration may be text-only, the second may carry
    the Skill tool_use block. They share ``message.id``, so deduping
    skills by message.id would drop the second iteration entirely
    (the bug we caught at smoke-test time on real Claude Code data).
    Tool-use ids are Anthropic's ``toolu_*`` format and unique across
    the session.

    Tool ids get NO cross-segment seed, and that is a measured call, not
    a proof. Re-reading is not the risk (no line is read by two
    segments); a genuine RETRY re-emitting the same ``tool_use.id`` on a
    LATER line is, and such a retry split across a resume boundary would
    count twice incrementally where a full walk counts once. Across 358
    live session jsonls there are ZERO duplicate ``tool_use.id`` values,
    so the seed would be pure cache weight for a case that does not
    occur. If duplicates ever show up, seed these the same way
    ``tail_msg_ids`` seeds the token side.

    Skips (both views):
      - non-JSON and non-UTF-8 lines (both surface as ``ValueError``
        from ``json.loads`` on bytes)
      - non-assistant messages
      - messages without a parseable ``timestamp``

    Skill detection ignores blocks where ``input`` is not a dict, or
    ``input.skill`` is not a non-empty string. Tool_use blocks for
    other tools (Edit, Bash, etc.) are not counted.

    On I/O failure returns an empty segment pinned at ``start_offset``
    so the caller advances nothing.
    """
    by_day: dict[str, DayBucket] = {}
    skills_by_day: SkillBuckets = {}
    seed = tuple(seed_msg_ids)
    seen_msg_ids: set[str] = set(seed)
    seen_tool_ids: set[str] = set()
    recent_msg_ids: list[str] = []
    path_str = str(path)
    offset = start_offset
    try:
        # Binary mode: `tell()`-free arithmetic on real byte offsets (a
        # text-mode `tell()` cookie is opaque and not comparable against
        # `st_size`), a genuinely byte-bounded line cap, and per-line
        # tolerance for invalid UTF-8 — under text mode a single bad
        # byte raised UnicodeDecodeError out through the whole events
        # tail. `json.loads` accepts bytes directly.
        with open(path, "rb") as fp:
            if start_offset:
                fp.seek(start_offset)
            for raw, end in iter_bounded_lines(fp, path_str, start_offset):
                offset = end
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    # JSONDecodeError (malformed) and UnicodeDecodeError
                    # (invalid utf-8) are both ValueError subclasses.
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
                if msg_id is not None and msg_id not in recent_msg_ids:
                    recent_msg_ids.append(msg_id)
    except OSError:
        return JsonlSegment({}, {}, start_offset, seed, ok=False)
    return JsonlSegment(by_day, skills_by_day, offset, _carry_tail_ids(seed, recent_msg_ids))


def _carry_tail_ids(seed: tuple[str, ...], seen: list[str]) -> tuple[str, ...]:
    """Merge the prior segment's tail ids with this segment's, keeping the
    ``TAIL_MSG_ID_LOOKBACK`` most-recently-seen in recency order.

    An id already in the seed is MOVED to the end rather than left where
    it was. Recency is the whole point of the window: an id we just saw
    again is one whose message may still be mid-flight, and leaving it at
    its stale position lets the tail trim evict exactly the id most likely
    to straddle the next boundary.

    The seed is preserved when this segment parsed no assistant messages
    at all (an append of pure user turns), so a quiet push can't drop the
    straddle guard for the message that is still mid-flight.

    Ids longer than ``_MAX_TAIL_MSG_ID_LEN`` are dropped — see that
    constant for why a jsonl-sourced string must be length-bounded before
    it reaches the cache file.

    The result is ALWAYS unique, enforced here rather than assumed of the
    caller. ``_resume_plan`` rejects any entry whose ``tail_msg_ids``
    contain a duplicate, so emitting one is equivalent to disabling resume
    for that file — permanently, silently, and on exactly the
    actively-streaming sessions this feature exists to speed up."""

    def _dedup(ids: Iterable[str]) -> list[str]:
        out: list[str] = []
        for mid in ids:
            if len(mid) <= _MAX_TAIL_MSG_ID_LEN and mid not in out:
                out.append(mid)
        return out

    fresh = _dedup(seen)
    # Seed entries re-seen this segment are dropped from their stale
    # position and inherit the recency of `fresh` below.
    carried = [mid for mid in _dedup(seed) if mid not in fresh]
    return tuple((carried + fresh)[-TAIL_MSG_ID_LOOKBACK:])


def walk_jsonl_buckets(path: Path) -> tuple[dict[str, DayBucket], SkillBuckets]:
    """Full-file walk returning both trimmed views. Thin shim over
    ``walk_jsonl_segment`` — the canonical parser. Returns ``({}, {})``
    on any I/O failure."""
    seg = walk_jsonl_segment(path)
    return _trim_by_day(seg.by_day, MAX_BY_DAY_DAYS), _trim_skills_by_day(
        seg.skills_by_day, MAX_BY_DAY_DAYS
    )


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


def iter_bounded_lines(
    fp,
    path_str: str,
    start_offset: int,
    *,
    label: str = "token walker",
    yield_final_partial: bool = False,
) -> Iterable[tuple[bytes, int]]:
    """Yield ``(line_bytes, end_offset)`` for each COMPLETE line in the
    binary stream ``fp``, capped at ``MAX_JSONL_LINE_BYTES`` per line.
    A skipped oversize line is yielded as ``b""`` so the caller's offset
    still advances past it.

    PUBLIC (v0.12.16). This is the canonical bounded reader for the Claude
    Code session-jsonl corpus and has consumers outside this module:
    ``events._read_cwd_from_latest_jsonl`` and ``events._last_mm_push_ts``.

    ``label`` names a caller in the oversize notice. Note the notice is
    deduped by PATH ONLY (``_WARNED_OVERSIZE_PATHS``), so when two call
    sites read the same file the label shown is whichever one reached it
    first in this process — the label makes the message *plausible* from
    either site, not authoritative about which one hit it. Do NOT hardcode
    "token walker" back into the string.

    ``yield_final_partial`` (v0.12.16) governs a trailing chunk with no
    newline. Default False is the RESUMABLE contract: such a chunk is a
    partial write, so it is neither yielded nor counted toward the offset
    and the next walk re-reads it once it settles. That is load-bearing for
    ``walk_jsonl_segment`` and must not change. **One-shot readers must
    pass True.** They have no "next walk" — a complete-but-unterminated
    final record is simply data they would silently drop. Regression caught
    by Codex adversarial review: porting the cwd reader to this primitive
    without the flag made ``_read_cwd_from_latest_jsonl`` return None for a
    session whose only line had not been newline-terminated yet, where the
    old text-mode reader returned the cwd. The yielded partial still
    advances ``pos``, but a one-shot reader has no resume point to corrupt.

    ``end_offset`` is an absolute byte offset one past the line's
    terminating newline, so the caller can persist a resume point.

    ``fp.readline(N)`` reads at most N bytes before returning, so a
    pathological line with no embedded newline can NEVER pull more than
    ``MAX_JSONL_LINE_BYTES`` into memory before we make the keep/skip
    decision. (Codex outside-voice review caught the prior `for line in
    fp:` form: it lets Python extend its buffer until newline-or-EOF, so
    a single multi-GB line could OOM the whole walk.)

    A trailing chunk with no newline is a PARTIAL line — Claude Code was
    mid-write. It is neither yielded nor counted toward ``end_offset``,
    so the next walk re-reads it from the start once it has settled.
    That is what makes the resume point safe to persist.

    When an oversize line is detected, drain forward to the next newline
    in bounded chunks and emit a one-shot ``mm: notice:`` for the file
    path so the user can investigate."""
    cap = MAX_JSONL_LINE_BYTES
    pos = start_offset
    while True:
        chunk = fp.readline(cap)
        if not chunk:
            return
        if chunk.endswith(b"\n"):
            pos += len(chunk)
            yield chunk, pos
            continue
        if len(chunk) < cap:
            # Short read with no newline == EOF mid-line.
            if yield_final_partial:
                # One-shot reader: no next walk, so an unterminated final
                # record is data, not a partial write to re-read later.
                pos += len(chunk)
                yield chunk, pos
            # Resumable reader: partial write. Stop without advancing past it.
            return
        if path_str not in _WARNED_OVERSIZE_PATHS:
            # `safe_str` because the path is a FILENAME from an agent-writable
            # tree (`~/.codex/sessions/**/rollout-*.jsonl` matches `.*`, and
            # macOS permits control bytes in filenames), so it can smuggle an
            # OSC/CSI escape into the terminal. Track 19A's relaxed Codex
            # refusal made this reachable on far more files than before —
            # the scan used to die on the first ledger-less rollout.
            sys.stderr.write(
                f"mm: notice: {label} skipping oversize line in {safe_str(path_str)}\n"
            )
            _WARNED_OVERSIZE_PATHS.add(path_str)
        drained = _drain_to_newline(fp)
        if drained is None:
            # EOF inside an oversize line — still a partial write.
            return
        pos += len(chunk) + drained
        # Yield the skipped line as EMPTY rather than swallowing it: the
        # caller advances its resume offset on every yield, and the parse
        # loop already skips blank lines. Without this a TRAILING oversize
        # line would pin the offset behind itself forever, re-draining the
        # same megabytes on every push.
        yield b"", pos


def _drain_to_newline(fp) -> int | None:
    """Consume bytes from the binary stream up to and including the next
    newline, in bounded chunks. Returns the number of bytes consumed, or
    ``None`` if EOF arrived first (nothing is consumed past EOF).

    Over-read is rewound so the stream is positioned exactly one byte
    past the newline — the caller's offset arithmetic depends on it."""
    consumed = 0
    while True:
        chunk = fp.read(_DRAIN_CHUNK_BYTES)
        if not chunk:
            return None
        nl = chunk.find(b"\n")
        if nl == -1:
            consumed += len(chunk)
            continue
        over = len(chunk) - (nl + 1)
        if over:
            fp.seek(-over, os.SEEK_CUR)
        return consumed + nl + 1


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

    # Miss. Resume from the cached offset when the file merely GREW;
    # fall back to a full walk otherwise.
    resume = _resume_plan(path, existing, size_pre)
    if resume is None:
        seg = walk_jsonl_segment(path)
        by_day, skills_by_day = seg.by_day, seg.skills_by_day
    else:
        seg = walk_jsonl_segment(
            path,
            start_offset=resume.offset,
            seed_msg_ids=resume.tail_msg_ids,
        )
        by_day, skills_by_day = resume.by_day, resume.skills_by_day
        merge_token_days(by_day, seg.by_day)
        merge_skill_days(skills_by_day, seg.skills_by_day)

    by_day = _trim_by_day(by_day, MAX_BY_DAY_DAYS)
    skills_by_day = _trim_skills_by_day(skills_by_day, MAX_BY_DAY_DAYS)

    # Read failed outright. The stats below would still agree, so
    # persisting here would pin the CURRENT size/mtime to buckets that
    # never saw the current bytes — a permanent hit that silently stops
    # counting this session. Return what we have, persist nothing.
    if not seg.ok:
        return by_day, skills_by_day

    # Fingerprint BEFORE the stability stat, so the pre/post stat pair
    # BRACKETS the probe read. Reading it after the stat leaves a window
    # in which the file is replaced and we persist the old buckets under
    # the REPLACEMENT's fingerprint — an entry that then licenses a
    # resume into a file none of its buckets ever saw.
    probe_len = head_probe_len(seg.end_offset)
    head = head_fingerprint(path, probe_len)

    # Re-stat: detect concurrent append. If drift, skip persistence.
    try:
        st_post = path.stat()
    except OSError:
        return by_day, skills_by_day
    if st_post.st_size != size_pre or st_post.st_mtime != mtime_pre:
        # File grew or was rewritten while we walked. Don't trust this
        # entry; let the next push re-walk a stable file. `resume` holds
        # COPIES of the cached buckets, so the surviving entry is
        # untouched and its offset still points at settled bytes.
        return by_day, skills_by_day

    entry: dict[str, Any] = {
        "size": size_pre,
        "mtime": mtime_pre,
        "by_day": by_day,
        "skills_by_day": skills_by_day,
    }
    # Resume fields are best-effort: without a readable head fingerprint
    # we can't prove a later file is the same file, so persist the legacy
    # shape and let the next miss walk in full. The probe was recomputed
    # against the NEW offset even on a resume — the window grows with the
    # accounted region until it reaches _HEAD_PROBE_BYTES.
    if head is not None:
        entry["offset"] = seg.end_offset
        entry["head"] = head
        entry["head_len"] = probe_len
        entry["tail_msg_ids"] = list(seg.tail_msg_ids)
    cache_files[key] = entry
    return by_day, skills_by_day


class _ResumePlan(NamedTuple):
    offset: int
    tail_msg_ids: tuple[str, ...]
    head: str
    by_day: dict[str, DayBucket]
    skills_by_day: SkillBuckets


def _resume_plan(path: Path, existing: Any, size_now: int) -> _ResumePlan | None:
    """Decide whether ``existing`` licenses an incremental walk of ``path``.

    Returns ``None`` — meaning "walk the whole file" — unless EVERY
    condition holds:

    * the entry carries the v0.12.15 resume fields (``offset`` + ``head``).
      Pre-v0.12.15 entries lack them and upgrade shape naturally on their
      next miss; deliberately NOT a ``CACHE_VERSION`` bump, which would
      throw away every peer's token history (same reasoning as the D2
      shape gate above);
    * the file did not SHRINK and the offset lands inside BOTH the
      recorded size and the current one — a truncation or rewrite
      invalidates every byte we've accounted for, and an offset past the
      size it was recorded against is a corrupt entry that would skip
      bytes no bucket has ever seen;
    * the head fingerprint still matches — catches a same-or-larger
      rewrite that a size check alone would read as an append;
    * the cached buckets are well-formed dicts;
    * ``tail_msg_ids`` is a well-formed list of strings. A malformed one
      is treated as a corrupt entry, NOT as an empty seed — silently
      degrading to no seed is what would double-count the straddling
      message this field exists to protect.

    The returned buckets are deep COPIES. Merging into the live cached
    dicts would mutate the entry even on the concurrent-append path that
    deliberately declines to persist.
    """
    if not isinstance(existing, dict):
        return None
    offset = existing.get("offset")
    prev_size = existing.get("size")
    head = existing.get("head")
    head_len = existing.get("head_len")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return None
    if not isinstance(prev_size, int) or isinstance(prev_size, bool):
        return None
    if not isinstance(head, str) or not head:
        return None
    # head_len must be EXACTLY what persistence would have written. A
    # merely-in-range check (0 < head_len <= offset) lets a corrupted
    # entry shrink the probe to one byte and make identity checking
    # meaningless — the check would still "pass" while proving nothing.
    if not isinstance(head_len, int) or isinstance(head_len, bool):
        return None
    if head_len <= 0 or head_len != head_probe_len(offset):
        return None
    if size_now < prev_size or offset > size_now or offset > prev_size:
        return None
    by_day = existing.get("by_day")
    skills_by_day = existing.get("skills_by_day")
    if not isinstance(by_day, dict) or not isinstance(skills_by_day, dict):
        return None
    # tail_msg_ids must match the CANONICAL persisted shape exactly:
    # at most TAIL_MSG_ID_LOOKBACK unique strings, each within the length
    # cap. Silently filtering an over-long or duplicated id here would
    # contradict this function's whole contract ("malformed → full walk")
    # and quietly hand back a weaker seed than the entry claims.
    raw_ids = existing.get("tail_msg_ids")
    if not isinstance(raw_ids, list) or len(raw_ids) > TAIL_MSG_ID_LOOKBACK:
        return None
    if not all(isinstance(x, str) and 0 < len(x) <= _MAX_TAIL_MSG_ID_LEN for x in raw_ids):
        return None
    if len(set(raw_ids)) != len(raw_ids):
        return None
    tail_ids = tuple(raw_ids)
    if head_fingerprint(path, head_len) != head:
        return None
    return _ResumePlan(
        offset=offset,
        tail_msg_ids=tail_ids,
        head=head,
        by_day=copy.deepcopy(by_day),
        skills_by_day=copy.deepcopy(skills_by_day),
    )


def head_probe_len(offset: int) -> int:
    """How many head bytes to fingerprint for an entry resuming at
    ``offset``.

    MUST NOT exceed ``offset``. Hashing a fixed 4 KiB window looks
    simpler but is wrong for any file shorter than the window: the read
    returns "whole file", so every append changes the digest and the
    entry never resumes — it silently degrades to a full walk forever.
    Clamping to ``offset`` keeps the probe inside bytes already
    accounted for, which are stable under append by definition."""
    return min(_HEAD_PROBE_BYTES, max(offset, 0))


def head_fingerprint(path: Path, probe_len: int) -> str | None:
    """Hex digest of the first ``probe_len`` bytes of ``path``, or
    ``None`` if it can't be read (or fewer bytes are present than
    claimed — a file that shrank is not the file we fingerprinted).

    Truncated to 16 hex chars: this identifies a file across pushes, it
    does not authenticate one."""
    if probe_len <= 0:
        return None
    try:
        with open(path, "rb") as fp:
            head = fp.read(probe_len)
    except OSError:
        return None
    if len(head) != probe_len:
        return None
    return hashlib.sha256(head).hexdigest()[:16]


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

    Returns ``(total_usd, per_model_usd)``. Rates come from
    ``resolve_prices`` (exact id, else family tier). Models that resolve
    to neither contribute to raw token counts elsewhere but DO NOT
    contribute here. Same for ``COST_EXCLUDED_MODELS`` (today only
    ``<synthetic>``).

    Unknown-model breadcrumb: emit one ``mm: notice:`` per process per
    unknown model. Caller-facing text mirrors the existing
    ``upgrade.py`` notice prefix. NOTE this breadcrumb is not sufficient
    on its own — it went to stderr for four unpriced models across the
    whole v0.12.x line and nobody saw it. The load-bearing signal is the
    aggregator's Notes line plus the ``>=`` prefix on the cost line.
    """
    total = 0.0
    per_model: dict[str, float] = {}
    for model, usage in (tokens_by_model or {}).items():
        if model in COST_EXCLUDED_MODELS:
            continue
        prices = resolve_prices(model)
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
        # BINARY read (v0.12.16). `read_text` decoded eagerly and was guarded
        # only by OSError, so one bad byte in the cache raised
        # UnicodeDecodeError out of here — and this runs on the events-tail
        # path via `_decide_token_walk_policy`, so it killed the tail exactly
        # like the session readers did. The `except UnicodeDecodeError` on the
        # json.loads below used to sit here looking like the guard; it was
        # DEAD (json.loads on a `str` cannot raise it). Feeding bytes to
        # json.loads makes that arm live: ValueError now covers both
        # malformed JSON and invalid utf-8.
        raw = CACHE_PATH.read_bytes()
    except OSError:
        return True
    try:
        # Decode STRICTLY as utf-8 before parsing, rather than handing bytes
        # to json.loads. json.loads on bytes also accepts a utf-8 BOM and
        # utf-16/32, but `lockedjson` — the reader that actually loads this
        # cache — decodes strict utf-8 and would reject those as corrupt.
        # Pre-fix the two disagreed: a BOM-prefixed cache reported WARM here,
        # so the inline warm was skipped, and then the real read reset it as
        # corrupt — the walk got no token data and the cache never warmed.
        # UnicodeDecodeError is a ValueError, so one guard covers both.
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError:
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


@dataclass(frozen=True)
class TokenCacheGcPlan:
    """Immutable stale-entry and normalization plan for session-token cache GC."""

    stale_keys: tuple[str, ...] = ()
    reset_root: bool = False
    reset_files: bool = False
    extra_root_keys: tuple[str, ...] = ()
    skipped_reason: str | None = None

    @property
    def repairs(self) -> int:
        return int(self.reset_root or self.reset_files) + len(self.extra_root_keys)

    @property
    def has_changes(self) -> bool:
        return bool(self.stale_keys or self.repairs)


@dataclass(frozen=True)
class TokenCacheGcResult:
    """Actual outcome of applying a ``TokenCacheGcPlan``."""

    plan: TokenCacheGcPlan
    write_error: OSError | None = None

    @property
    def candidates(self) -> int:
        return len(self.plan.stale_keys)

    @property
    def deleted(self) -> int:
        return 0 if self.write_error is not None else self.candidates

    @property
    def failed(self) -> int:
        return self.candidates if self.write_error is not None else 0

    @property
    def repairs_applied(self) -> int:
        return 0 if self.write_error is not None else self.plan.repairs

    @property
    def repairs_failed(self) -> int:
        return self.plan.repairs if self.write_error is not None else 0


def plan_cache_entries(
    *,
    max_age_s: float = 90 * 24 * 3600,
    now: datetime | None = None,
) -> TokenCacheGcPlan:
    """Read the token cache without mutation and return its GC plan.

    A missing cache is intentionally a no-op: preview must not create it, and
    apply has nothing useful to repair. Other malformed cache states become an
    explicit repair plan so callers can report them before applying anything.
    """
    now = now or datetime.now(timezone.utc)
    with locked_json_snapshot(CACHE_PATH) as snapshot:
        return _plan_cache_entries(snapshot, max_age_s=max_age_s, now=now)


def reap_cache_entries(
    *,
    max_age_s: float = 90 * 24 * 3600,
    now: datetime | None = None,
) -> TokenCacheGcResult:
    """Apply a token-cache GC plan and report actual persistence success.

    The initial read-only plan avoids opening a missing/fresh cache for R/M/W.
    If work is needed, the cache is re-planned under the exclusive lock so a
    concurrent update cannot turn a preview's stale plan into a blind write.
    """
    now = now or datetime.now(timezone.utc)
    initial_plan = plan_cache_entries(max_age_s=max_age_s, now=now)
    if not initial_plan.has_changes:
        return TokenCacheGcResult(plan=initial_plan)

    with locked_json_rmw(
        CACHE_PATH,
        default_factory=_empty_cache,
        on_contention="block",
        contention_warning="token cache contended; skipping token GC",
    ) as ljson:
        if not ljson.is_locked:
            return TokenCacheGcResult(plan=TokenCacheGcPlan(skipped_reason="lock_failed"))
        snapshot = LockedJsonSnapshot(
            data=ljson.data if ljson.read_state == "valid" else None,
            state=ljson.read_state,
            error=ljson.read_error,
        )
        plan = _plan_cache_entries(snapshot, max_age_s=max_age_s, now=now)
        if not plan.has_changes:
            ljson.write_on_exit = False
            return TokenCacheGcResult(plan=plan)
        _apply_cache_gc_plan(ljson.data, plan)

    return TokenCacheGcResult(plan=plan, write_error=ljson.write_error)


def gc_cache_entries(*, max_age_s: float = 90 * 24 * 3600) -> int:
    """Compatibility wrapper returning successful stale-entry deletions.

    Callers that need candidate, repair, and persistence details should use
    ``plan_cache_entries`` / ``reap_cache_entries`` instead.
    """
    return reap_cache_entries(max_age_s=max_age_s).deleted


def _plan_cache_entries(
    snapshot: LockedJsonSnapshot,
    *,
    max_age_s: float,
    now: datetime,
) -> TokenCacheGcPlan:
    if snapshot.state == "missing":
        return TokenCacheGcPlan()
    if snapshot.state in ("empty", "malformed", "non_dict"):
        return TokenCacheGcPlan(reset_root=True)
    if snapshot.state != "valid" or snapshot.data is None:
        return TokenCacheGcPlan(skipped_reason=snapshot.state)

    root = snapshot.data
    if root.get("version") != CACHE_VERSION:
        return TokenCacheGcPlan(reset_root=True)
    extra_root_keys = tuple(k for k in root if k not in ("version", "files"))
    files = root.get("files")
    if not isinstance(files, dict):
        return TokenCacheGcPlan(extra_root_keys=extra_root_keys, reset_files=True)

    cutoff_iso = now.date().isoformat()
    max_days = int(max_age_s / 86400)
    stale_keys: list[str] = []
    for key, entry in files.items():
        if not isinstance(entry, dict):
            stale_keys.append(key)
            continue
        if not Path(key).exists():
            stale_keys.append(key)
            continue
        by_day = entry.get("by_day") or {}
        if not isinstance(by_day, dict) or not by_day:
            stale_keys.append(key)
            continue
        most_recent = max(by_day.keys())
        if _days_between(most_recent, cutoff_iso) > max_days:
            stale_keys.append(key)
    return TokenCacheGcPlan(stale_keys=tuple(stale_keys), extra_root_keys=extra_root_keys)


def _apply_cache_gc_plan(root: dict[str, Any], plan: TokenCacheGcPlan) -> None:
    if plan.reset_root:
        root.clear()
        root.update(_empty_cache())
        return
    for key in plan.extra_root_keys:
        root.pop(key, None)
    if plan.reset_files:
        root["files"] = {}
        return
    files = root.get("files")
    if not isinstance(files, dict):
        return
    for key in plan.stale_keys:
        files.pop(key, None)


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
    "DEFAULT_WARM_BUDGET_S",
    "DayBucket",
    "MAX_BY_DAY_DAYS",
    "MODEL_FAMILY_TIERS",
    "PRICING",
    "PRICING_LAST_UPDATED",
    "SUBSCRIPTION_CAVEAT",
    "SkillBuckets",
    "TAIL_MSG_ID_LOOKBACK",
    "TOKEN_FIELDS",
    "TokenCacheGcPlan",
    "TokenCacheGcResult",
    "Usage",
    "JsonlSegment",
    "estimate_cost",
    "gc_cache_entries",
    "get_or_compute",
    "head_fingerprint",
    "head_probe_len",
    "is_cache_cold",
    "iter_bounded_lines",
    "lock_and_get_files",
    "merge_by_model",
    "merge_skill_days",
    "merge_token_days",
    "merge_usage_bucket",
    "plan_cache_entries",
    "reap_cache_entries",
    "model_family",
    "parse_usage",
    "resolve_prices",
    "slice_window",
    "walk_jsonl_buckets",
    "walk_jsonl_segment",
    "warm_token_cache_inline",
    "zero_day_bucket",
    "zero_model_bucket",
]
