"""Frozen oracle copied from v0.14.15 at 70abdee. Do not edit.

When the reducer's contract changes intentionally, delete this file and
re-freeze it from the new release. No imports from src are allowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypedDict

TOKEN_FIELDS: tuple[str, ...] = ("input", "cache_create", "cache_read", "output")


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


_MAX_MODEL_ID_BYTES = 256


_MAX_PROMPT_ID_BYTES = 256


_MAX_COUNTER = 2**53


_CANONICAL_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


HostFamily = Literal["claude", "codex", "grok", "other"]


Reason = Literal[
    "deadline",
    "io_error",
    "locked",
    "malformed",
    "no_metadata_ledger",
    "partial",
    "stale",
    "unsupported",
]


HostTokens = dict[str, dict[str, Usage]]


@dataclass
class HostUsageBuckets:
    """The two views of one reduction, built atomically.

    ``by_family`` is the existing ``{host_family: {UTC-day: Usage}}`` wire
    shape. ``by_day`` is the same work as ``{UTC-day: DayBucket}``, with
    per-model totals nested under each day's ``by_model``. ``_add_usage``
    updates both in one call so they cannot drift; do not derive one from
    the other later.
    """

    by_family: HostTokens = field(default_factory=dict)
    by_day: dict[str, DayBucket] = field(default_factory=dict)
    unattributable_days: set[str] = field(default_factory=set)
    """UTC days whose inclusive source emitted a nonzero ``cache_create``.

    Inclusive ``input`` may or may not contain cache-*created* tokens;
    every measured live bucket has ``cache_create == 0``, so the three-term
    formula is correct under both hypotheses today. A nonzero write from
    Codex or Grok marks the day unattributable rather than silently
    pricing it. A disjoint extractor never writes this set — its cache
    write is already a real priced field.
    """


class _CacheEntry(TypedDict, total=False):
    """One rollout's cached parse. Two shapes share this map.

    A LEDGER entry carries ``states`` plus the resume carry-set described
    below. A NO-LEDGER entry sets ``no_ledger`` instead and carries none of
    them: the file was parsed in full and provably contained no usage (an
    abandoned or response-less session). Both carry the same identity +
    fingerprint fields.

    The no-ledger shape exists for convergence, not tidiness. Without it those
    files are re-parsed on EVERY scan forever, so a corpus whose ledger-less
    files alone outcost the caller's 250ms budget can never reach a complete
    pass — the cache warms and the scan still expires, permanently. Pinned by
    ``test_uncacheable_rollouts_do_not_block_convergence``.

    **The ledger shape stores observed CUMULATIVE STATES, not a total.** A
    rollout is not the unit of accounting: 195 ``turn_id`` values span 244 of
    746 files on a real corpus (fork / retry / resume), sharing 85% of their
    ledger before diverging. Per-file totals therefore double-count roughly
    half the corpus. ``states`` keeps every distinct ``(turn, cumulative)``
    pair this file observed so ``_aggregate`` can union them across files and
    difference the union once. See ``_aggregate``.

    Entries written before this Track carried ``day`` / ``model`` / ``usage``
    instead. The ABSENCE of ``states`` is the version discriminator and forces
    one full re-walk of that file — deliberately NOT a ``CACHE_VERSION`` bump,
    which shares a constant with the Grok namespace and would discard it
    too. Same call this repo made twice already, for
    ``skills_by_day`` (v0.11.27) and ``offset``/``head`` (v0.12.15), both times
    because a bump throws away valid data that is expensive to rebuild.

    The four resume fields exist because a bucket alone cannot continue a
    walk: ``last_total`` is what an appended ledger must be differenced
    AGAINST, ``last_model`` / ``last_turn`` are the attribution context a
    resumed segment inherits, and ``pending`` holds ledgers observed before any
    ``turn_context`` — which a segment boundary can otherwise strand.

    **This entry is much larger than the terminal it replaced, and that cost is
    the reason the string columns are interned.** Measured on a 747-rollout /
    694 MB corpus: 72,654 states, median 17 per file and 1,234 at the maximum.
    Spelled out, the cache was 23.4 MB and its json round-trip alone was 95 ms
    of the 250 ms autopush host budget. Interning ``turn_ids`` / ``days`` /
    ``models`` and keeping ``last`` only on a file's FIRST state (the only place
    ``_aggregate`` reads it) brings that to 13.5 MB and 56 ms. Still 34x the
    v0.12.47 cache, and it grows with the corpus. Encoding work is deferred
    until "Host cache encoding trigger" in ``docs/roadmap-future.md`` fires:
    25 MB or 100 ms json round-trip. Measured 2026-09-04
    at 4.11 MB / 23.3 ms / 20,047 states / 716 rollouts, about 6x headroom.
    """

    dev: int
    ino: int
    size: int
    mtime_ns: int
    head: str
    head_len: int
    tail: str
    tail_len: int
    offset: int
    turn_ids: list[str]
    days: list[str]
    models: list[str]
    states: list[list[Any]]
    last_total: list[int]
    last_model: str
    last_turn: str
    pending: list[list[Any]]
    no_ledger: bool


@dataclass(frozen=True)
class _TurnState:
    """One observed CUMULATIVE reading inside a turn.

    ``total`` is the host's own running counter, in ``TOKEN_FIELDS`` order, at
    the moment ``day`` / ``model`` observed it. Two rollout files that forked
    from one conversation report the SAME ``(turn, total)`` for every shared
    event and diverge afterwards, which is what makes the pair a usable
    identity: equal states are the same work, unequal states are not.

    ``last`` is that record's ``last_token_usage`` — the host's own statement
    of what THIS reading added. It is consulted only for the lowest state in a
    turn's union, where differencing has nothing to difference against: the
    cumulative counter there already includes every earlier turn of the session
    (and, on a resumed rollout, a parent session's history too). Every state
    carries it because the lowest state of a turn is not generally the first
    record of a file — a file holds 2.89 turns on average, so gating this on
    the file's first record would start every later turn's chain from the whole
    cumulative total. ``None`` means the record carried no ``last_token_usage``
    (no real record does; fixtures do), and the fallback is the cumulative,
    which is correct only for a session's very first reading.
    """

    turn: str
    total: tuple[int, ...]
    day: str
    model: str
    last: tuple[int, ...] | None


class _ReadFailure(RuntimeError):
    def __init__(self, reason: Reason) -> None:
        self.reason = reason


def host_family(model: str) -> HostFamily:
    """Return Mind Meld's canonical model-family bucket.

    Classification is by model-id prefix, not by which reader produced
    the id — a reader is not a row of its own. Case-insensitive and
    intentionally small so renderers never grow their own incompatible
    predicates.
    """
    normalized = model.casefold() if isinstance(model, str) else ""
    if normalized.startswith("claude-"):
        return "claude"
    if normalized.startswith("gpt-") or normalized in {"o1", "o3"} or normalized.startswith("o4-"):
        return "codex"
    if normalized.startswith("grok-"):
        return "grok"
    return "other"


def _normalize_inclusive_usage(usage: Usage) -> Usage:
    """Convert inclusive host counters into mutually exclusive billable buckets.

    Inclusive schema (Codex CLI, Grok CLI): ``input`` already contains
    ``cache_read`` and, if the host ever emits it, ``cache_create``. The
    three-term formula is correct under both "create is inside input" and
    "create is not" while ``cache_create == 0`` — the live corpus. Do not
    clamp a negative uncached value to zero and publish it: that destroys
    the evidence. ``cache_read + cache_create > input`` is a reader-level
    failure and raises ``_ReadFailure("malformed")`` so Track 31A isolates
    that reader for the capture.

    Never call this on an already-disjoint bucket. A second normalization
    would destroy the valid ``cache_read > input`` shape. See "Share host-reader
    filesystem resume primitives only after measuring duplication cost" in
    ``docs/roadmap-future.md`` before extracting shared reader code.
    """
    input_tokens = usage["input"]
    cache_create = usage["cache_create"]
    cache_read = usage["cache_read"]
    if cache_read + cache_create > input_tokens:
        raise _ReadFailure("malformed")
    return {
        "input": input_tokens - cache_read - cache_create,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "output": usage["output"],
    }


def _aggregate(entries: Any) -> HostUsageBuckets:
    """Reduce cached entries to family totals AND per-model day buckets.

    Grok contributes pre-deduped, disjoint ``turns``; Codex contributes
    cumulative ``states`` that must be deduped HERE, across files. Do not
    normalize the Grok turns again. See "Share host-reader filesystem resume
    primitives only after measuring duplication cost" in ``docs/roadmap-future.md``.

    **Why Codex dedup cannot live in the per-file walk.** A rollout file is not
    the unit of accounting. Measured on a real corpus: 195 ``turn_id`` values
    appear in more than one file, spanning 244 of 746 files, sharing 85% of
    their ledger before diverging (fork / retry / resume). Summing per file
    double-counts the shared prefix — over half the reported total. Keeping
    only the longest file per turn instead DISCARDS the divergent branches,
    which is real work. Neither is correct, and neither is visible from inside
    one file.

    **The unit of identity is the TRANSITION, not the reading.** Deduping
    readings looks right and is not: two branches that fork at 100 and reach
    130 and 150 have four distinct readings, and treating 130 as a waypoint on
    the way to 150 reports 150 when the real spend is 180. Their TRANSITIONS
    differ — ``100 -> 130`` and ``100 -> 150`` — so keying on
    ``(lineage, previous, current)`` counts the shared prefix once and both
    tails in full.

    It also subsumes two other shapes for free, which is the argument for
    preferring it over a special case per shape:

    * A repeated ``token_count`` (183 files / 414 records on that corpus) is a
      ``t -> t`` transition, so its increment is 0 rather than a re-added
      ``last_token_usage``.
    * Codex re-emits a turn's final reading as the next turn's first, which is
      the same ``t -> t`` shape across a turn boundary.

    The ``max(0, ...)`` clamp is a guard, not a live case: zero non-monotonic
    steps were observed across 1,041 turns and 34,313 readings. Monotonicity is
    an observation about today's Codex, not a documented guarantee, and a
    negative bucket would reach the wire as an unsigned counter.

    **Known limitation, stated because it cannot be fixed at this layer.** A
    transition identity is numeric, so two branches of one lineage that each
    spend the EXACT same four counters from the exact same cumulative are
    indistinguishable from one branch seen twice, and collapse to one. A
    deterministic retry of an identical prompt is the realistic way to hit it.
    Closing it needs a stable per-record id, and `token_count` has none: its
    payload carries only ``type`` / ``info`` / ``rate_limits``, and ``turn_id``
    lives on ``turn_context``, which is why a turn is a lineage link here rather
    than a key. The alternative — not deduping — restores a measured 55%
    over-count across 244 of 747 files. Under-counting an exact-duplicate retry
    is the smaller and rarer error, but it IS an error; do not describe this
    reduction as exact.
    """
    buckets = HostUsageBuckets()
    lineages = _Lineages()
    staged: list[tuple[list[str], list[tuple[Any, ...]]]] = []
    for entry in entries:
        # No-ledger entries exist only to make an unchanged ledger-less file a
        # cheap cache hit. They carry nothing attributable and must never reach
        # `host_family`, which would bucket "" as the `other` family.
        if entry.get("no_ledger"):
            continue
        for turn in entry.get("turns") or ():
            _add_usage(buckets, turn["day"], turn["model"], turn["usage"])
        parsed = _entry_states(entry)
        if not parsed:
            continue
        # Every turn observed in ONE file belongs to one session, so its
        # cumulative counter is one shared number line. A synthetic per-file id
        # keeps a file with no named turn in its own lineage instead of pooling
        # unrelated files together.
        # dev+ino alone is NOT unique across a cache: a rollout deleted but not
        # yet pruned can have its inode reused by a new file, and both entries
        # then claim one id and merge into one lineage. Size and mtime_ns are
        # already on the entry and make that collision unreachable in practice.
        own = (
            f"\x00{entry.get('dev')}:{entry.get('ino')}:{entry.get('size')}:{entry.get('mtime_ns')}"
        )
        turn_ids = [own] + [p[0] for p in parsed if p[0]]
        lineages.union(turn_ids)
        staged.append((turn_ids, parsed))
    # (lineage, previous cumulative, this cumulative) -> (day, model, increment)
    seen: dict[tuple[str, Any, tuple[int, ...]], tuple[str, str, tuple[int, ...], int]] = {}
    # (lineage, cumulative) -> the entries that reached it BY a transition.
    # Used to disarm an opening another FILE already accounted for. Tracking
    # which entry did the reaching is load-bearing: a file whose own second
    # reading repeats its first would otherwise suppress its own opening and
    # lose that increment.
    reached: dict[tuple[str, tuple[int, ...]], set[int]] = {}
    for position, (turn_ids, parsed) in enumerate(staged):
        root = lineages.find(turn_ids[0])
        previous: tuple[int, ...] | None = None
        for _turn, total, day, model, last in parsed:
            if previous is None:
                # Opening reading. Nothing to difference against: the counter
                # already includes whatever came before, which on a resumed
                # rollout is a PARENT session's history (4 files on the corpus,
                # 65,262,198 input tokens). `last_token_usage` is the host's
                # own statement of what this reading alone added.
                increment = last if last is not None else total
            else:
                increment = tuple(max(0, total[i] - previous[i]) for i in range(len(total)))
                reached.setdefault((root, total), set()).add(position)
            seen.setdefault((root, previous, total), (day, model, increment, position))
            previous = total
    for (root, previous, total), (day, model, increment, position) in seen.items():
        if previous is None and (reached.get((root, total), set()) - {position}):
            # A resumed or forked file OPENS at a cumulative that some other
            # file in this lineage already arrived at by spending tokens. That
            # arrival transition already counted the work, so charging this
            # file's `last_token_usage` on top double-counts it. Measured: 1 of
            # 747 rollouts on a real corpus, so rare, but an opening has no
            # predecessor and therefore no transition identity of its own —
            # this is the only thing that can disambiguate it.
            continue
        usage = _normalize_inclusive_usage(dict(zip(TOKEN_FIELDS, increment)))
        if usage["cache_create"] > 0:
            buckets.unattributable_days.add(day)
        _add_usage(buckets, day, model, usage)
    return buckets


class _Lineages:
    """Union-find over turn ids. A turn is a LINK, not a bucket key.

    Turns cannot be scoped independently, and the reason is a real shape in the
    log: Codex re-emits a turn's final ``token_count`` verbatim as the first
    record of the NEXT turn (same cumulative total, same ``last_token_usage``).
    Scoping transitions per turn makes that re-emission an OPENING reading of a
    fresh scope, which then claims its ``last_token_usage`` again — measured at
    473,932 input tokens on a single 71-record rollout before this was folded
    in.

    A connected component is the right scope because it is exactly the set of
    readings that share one cumulative number line. Two files that forked from
    one conversation share turn ids, so they land in one lineage: their common
    transitions collapse and their divergent ones both survive.
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        root = self._parent.setdefault(item, item)
        while root != self._parent[root]:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, items: list[str]) -> None:
        if not items:
            return
        root = self.find(items[0])
        for other in items[1:]:
            self._parent[self.find(other)] = root


def _state_parts(
    state: Any,
    turn_ids: list[str] | None = None,
    days: list[str] | None = None,
    models: list[str] | None = None,
) -> tuple[str, tuple[int, ...], str, str, tuple[int, ...] | None]:
    """Resolve one stored state row, de-interning its string columns.

    The tables are optional so an in-memory ``_TurnState`` still round-trips
    through the same helper.
    """
    if isinstance(state, _TurnState):
        return state.turn, state.total, state.day, state.model, state.last
    turn, total, day, model, last = state
    return (
        turn_ids[turn] if turn_ids is not None else turn,
        tuple(total),
        days[day] if days is not None else day,
        models[model] if models is not None else model,
        (tuple(last) if last is not None else None),
    )


_StateParts = tuple[str, tuple[int, ...], str, str, "tuple[int, ...] | None"]


def _entry_states(entry: Any) -> list[_StateParts]:
    """Every stored state of one entry, in document order, de-interned."""
    # NOTE `turn_ids`, not `turns`: `turns` is Grok's per-turn record list and
    # both shapes flow through the same `_aggregate`.
    turn_ids = entry.get("turn_ids") or []
    days = entry.get("days") or []
    models = entry.get("models") or []
    return [_state_parts(row, turn_ids, days, models) for row in entry.get("states") or ()]


def _add_usage(buckets: HostUsageBuckets, day: str, model: str, usage: Any) -> None:
    """Update family totals and per-model day buckets in one call.

    Never prune a zero bucket: a ``t -> t`` transition still creates a day
    key, and an all-zero bucket is a real accepted shape. Renderers skip
    zeros at render time; the writer never does. Deriving one view from
    the other later is how the two maps drift.
    """
    family_days = buckets.by_family.setdefault(host_family(model), {})
    family_bucket = family_days.setdefault(day, zero_model_bucket())
    merge_usage_bucket(family_bucket, usage)

    day_bucket = buckets.by_day.setdefault(day, zero_day_bucket())
    merge_usage_bucket(day_bucket, usage)
    by_model = day_bucket.setdefault("by_model", {})
    model_bucket = by_model.setdefault(model, zero_model_bucket())
    merge_usage_bucket(model_bucket, usage)


_IDENTITY_KEYS = (
    "dev",
    "ino",
    "size",
    "mtime_ns",
    "head",
    "head_len",
    "tail",
    "tail_len",
    "offset",
)


def _identity_fields_from(value: dict[str, Any]) -> dict[str, Any]:
    """Project an already-validated cache dict down to identity fields only."""
    return {key: value[key] for key in _IDENTITY_KEYS}


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
    if value.get("no_ledger") is True:
        # Identity + fingerprint only, and normalized to exactly that: any
        # day/model/usage riding along on a no_ledger entry is dropped rather
        # than trusted, so a hand-edited cache cannot smuggle totals in behind
        # the flag `_aggregate` uses to skip it.
        entry: _CacheEntry = {
            **_identity_fields_from(value),  # type: ignore[typeddict-item]
            "no_ledger": True,
        }
        return entry
    raw_states = value.get("states")
    if not isinstance(raw_states, list) or not raw_states:
        # ABSENCE is the pre-Track discriminator: an entry written before
        # per-turn accounting carries `day`/`model`/`usage` and cannot seed a
        # resume (it has no cumulative baseline, turn id, or pending buffer).
        # Rejecting it forces exactly one full re-walk of that file. Measured
        # cost on a 746-file / 694 MB corpus: 801 ms cold, so 3 to 6 passes at
        # the 250 ms autopush budget — the same convergence v0.12.47 shipped.
        # NOT a CACHE_VERSION bump: that constant is shared with the Grok
        # namespace and would discard it too.
        return None
    turn_ids = _validated_table(value.get("turn_ids"), _MAX_PROMPT_ID_BYTES, allow_empty=True)
    days = _validated_table(value.get("days"), 32)
    models = _validated_table(value.get("models"), _MAX_MODEL_ID_BYTES)
    if turn_ids is None or days is None or models is None:
        return None
    if any(not _validated_day(day) for day in days):
        return None
    states: list[list[Any]] = []
    for raw in raw_states:
        parsed = _validated_state(raw, len(turn_ids), len(days), len(models))
        if parsed is None:
            return None
        states.append(parsed)
    pending: list[list[Any]] = []
    for raw in value.get("pending") or ():
        parsed_pending = _validated_pending(raw)
        if parsed_pending is None:
            return None
        pending.append(parsed_pending)
    entry_out: _CacheEntry = {
        **_identity_fields_from(value),  # type: ignore[typeddict-item]
        "turn_ids": turn_ids,
        "days": days,
        "models": models,
        "states": states,
        # Bounded like any other turn id: `last_turn` is carried into a resumed
        # walk and becomes a lineage key, so an unbounded string here would be
        # an unbounded key from a hand-edited cache.
        "last_turn": _validated_turn_id(value.get("last_turn")),
    }
    if pending:
        entry_out["pending"] = pending
    last_total = _validated_counter_list(value.get("last_total"))
    if last_total is not None:
        entry_out["last_total"] = last_total
    model = value.get("last_model")
    if isinstance(model, str) and model and len(model.encode("utf-8")) <= _MAX_MODEL_ID_BYTES:
        entry_out["last_model"] = model
    return entry_out


def _validated_state(raw: Any, n_turns: int, n_days: int, n_models: int) -> list[Any] | None:
    """Validate one stored row. String columns are INDICES into the entry's
    interned tables, so the bound check is the trust boundary: an out-of-range
    index from a hand-edited cache would otherwise raise IndexError out of
    `_aggregate` rather than falling back to a re-parse."""
    if not isinstance(raw, list) or len(raw) != 5:
        return None
    turn, total, day, model, last = raw
    if not _index_in_range(turn, n_turns):
        return None
    if not _index_in_range(day, n_days) or not _index_in_range(model, n_models):
        return None
    counters = _validated_counter_list(total)
    if counters is None:
        return None
    last_counters = None if last is None else _validated_counter_list(last)
    if last is not None and last_counters is None:
        return None
    return [turn, counters, day, model, last_counters]


def _validated_turn_id(value: Any) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_PROMPT_ID_BYTES:
        return ""
    return value


def _index_in_range(value: Any, size: int) -> bool:
    return _is_nonnegative_int(value) and value < size


def _validated_table(raw: Any, max_bytes: int, *, allow_empty: bool = False) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        if not item and not allow_empty:
            return None
        if len(item.encode("utf-8")) > max_bytes:
            return None
        out.append(item)
    return out


def _validated_pending(raw: Any) -> list[Any] | None:
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    total, day, last = raw
    counters = _validated_counter_list(total)
    if counters is None or not _validated_day(day):
        return None
    last_counters = None if last is None else _validated_counter_list(last)
    if last is not None and last_counters is None:
        return None
    return [counters, day, last_counters]


def _validated_counter_list(raw: Any) -> list[int] | None:
    if not isinstance(raw, list) or len(raw) != len(TOKEN_FIELDS):
        return None
    if any(not _is_valid_counter(v) for v in raw):
        return None
    return list(raw)


def _validated_day(day: Any) -> bool:
    """Canonical YYYY-MM-DD only.

    `fromisoformat` alone accepts a full datetime with a local UTC offset,
    basic format, and week dates — and since Track 19A a cached day becomes a
    KEY in a synced event row, so a tampered or corrupted cache could otherwise
    put a per-machine timezone offset on the wire. Fresh parses always emit
    `date().isoformat()`; this bounds what a cache HIT can reintroduce.
    """
    if not isinstance(day, str) or not _CANONICAL_DAY.fullmatch(day):
        return False
    try:
        datetime.fromisoformat(day)
    except ValueError:
        return False
    return True


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_valid_counter(value: Any) -> bool:
    return _is_nonnegative_int(value) and value <= _MAX_COUNTER
