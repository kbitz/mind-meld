"""Private, local-only host-usage readers.

Track 17C supports Codex rollout logs at
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``. Track 18D adds a consented
Grok reader for ``updates.jsonl`` terminal records under ``GROK_HOME/sessions``
(else ``~/.grok/sessions``). A ``token_count`` record is a CUMULATIVE reading
of the host's own counter. The reader differences consecutive readings and
sums the transitions; summing every token-count record would double-count
the running total. The model is the most recent preceding
``turn_context.payload.model``. Counters map onto Mind Meld's four token
fields: ``input_tokens`` → input, ``cache_write_input_tokens`` →
cache-create, ``cached_input_tokens`` → cache-read, and ``output_tokens`` →
output. ``reasoning_output_tokens`` is already part of output and is never
added a second time.

Codex and Grok CLI counters are **inclusive**: the host's ``input`` already
contains ``cache_read`` (and, if ever nonzero, ``cache_create``). Claude
session jsonl is **disjoint**. Counter semantics is a property of the
READER, not the model id — the same ``grok-4.6`` id can arrive both ways.
Inclusive extractors therefore emit disjoint buckets via
``_normalize_inclusive_usage`` (``uncached = input - cache_read -
cache_create``). Do **not** normalize in ``_add_usage``: that is where
readers converge, and subtracting ``cache_read`` from an already-disjoint
bucket (Claude and Cursor; historically OpenCode) would clamp real billable
tokens to zero. Keep this boundary in any future extraction; see
"Share host-reader filesystem resume primitives only after measuring
duplication cost" in ``docs/roadmap-future.md``. Malformed inclusive counters
(``cache_read + cache_create > input``) raise ``_ReadFailure("malformed")``
so Track 31A isolates that reader.

Two ordinary Codex shapes are tolerated rather than refused, because one
unreadable file still fails that WHOLE reader. Track 31A isolates that
failure to the reader: the caller publishes the survivors rather than
omitting the snapshot. Measured on a 452-rollout machine, refusing them
cost 167 files — 37% — and the reader returned ``unsupported`` in 5ms
having died on the first one:

* a ``token_count`` whose ``payload.info`` is null — Codex's start-of-turn
  marker, carrying no ledger (33% of rollouts had one), and
* a ledger that precedes the first ``turn_context`` and so has no model yet;
  it is buffered via ``walk.pending`` / ``_flush_pending`` and attributed
  to the first model the file names.

Refusal is still correct for a ledger we saw and could NOT attribute to any
model, and for a malformed (present but non-dict) ``info``. A rollout with no
ledger at all simply contributes nothing.

The reader is read-only with respect to host logs. Its private 0600 cache,
``~/.config/mind-meld/host-tokens.json``, stores only opaque path digests,
file fingerprints, bounded model IDs, and aggregate totals—never transcript
content, raw paths, prompts, or tool output. ``complete=False`` is a safety
signal: a caller must omit the host snapshot rather than serialize a partial
or invented zero. Track 19A owns that caller policy.

Cache persistence is DECOUPLED from result validity. "May this scan be
published?" and "did we learn something durable about individual files?" are
different questions, and conflating them left a large corpus unable to
bootstrap under the caller's 250ms/500ms budget: every bounded scan re-parsed
the same prefix, expired in the same place, and discarded it — measured as six
consecutive scans and zero bytes cached. For Codex/Grok, a COMPLETE pass replaces the map
(that is what prunes deleted rollouts); a PARTIAL pass MERGES, because
replacing would delete entries it never reached and pruning on a listing it
never finished would drop files that were never absent. ``warm_host_cache_inline``
is the attended-command escape hatch for a deadline miss. Attended callers
publish that warm read's result; unattended callers keep their short budget.

Track 67A reads only Conductor's Cursor ``runs.ndjson``. Its disjoint counters
need no normalization. Unlike the forensic caches above, Cursor history is
authoritative after Conductor pruning: durable atomic writes retain run IDs
for 90 days. Every file is reparsed, and short passes need not converge.
"""

from __future__ import annotations

import errno
import gc
import hashlib
import json
import os
import re
import stat
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Literal, TypedDict, get_args

from mind_meld.errors import StorageError
from mind_meld.lockedjson import (
    InvalidJsonCache,
    LockContended,
    locked_json_durable_rmw,
    locked_json_rmw,
    locked_json_snapshot,
)
from mind_meld.token_usage import (
    MAX_JSONL_LINE_BYTES,
    TOKEN_FIELDS,
    DayBucket,
    Usage,
    iter_bounded_lines,
    merge_usage_bucket,
    zero_day_bucket,
    zero_model_bucket,
)

CACHE_PATH = Path.home() / ".config" / "mind-meld" / "host-tokens.json"
"""Forensic host-reader cache. Deliberately separate from Claude's cache."""

CODEX_SESSIONS_PATH = Path.home() / ".codex" / "sessions"
GROK_SESSIONS_PATH = Path.home() / ".grok" / "sessions"
GROK_CACHE_PATH = Path.home() / ".config" / "mind-meld" / "grok-host-tokens.json"
CURSOR_STORE_PATH = (
    Path.home() / "Library" / "Application Support" / "com.conductor.app" / "cursor-sdk-store"
)
CURSOR_CACHE_PATH = Path.home() / ".config" / "mind-meld" / "cursor-host-tokens.json"
CURSOR_HOST_CACHE_RETENTION_DAYS = 90
"""Reader-owned retention, unrelated to events.CURSOR_SCAN_DAYS (read position)."""
CURSOR_USAGE_CENSUS_HOST_VERSION = "2026.09.18-9a7762b"
CURSOR_USAGE_CENSUS_CONDUCTOR_VERSION = "0.87.3"
"""CLI generating the sessions and app producing the persisted schema, respectively."""
_CURSOR_ENDED_AT_MIN_MS = 1_577_836_800_000
"""2020-01-01 UTC. A seconds-scale clock cannot pass; older millisecond days still reap."""
CACHE_VERSION = 1
DEFAULT_READ_BUDGET_S = 5.0

_HEAD_PROBE_BYTES = 4096
_TAIL_PROBE_BYTES = 4096
_MAX_MODEL_ID_BYTES = 256
_MAX_PROMPT_ID_BYTES = 256
_MAX_REASON_SINCE_CHARS = 40
_MAX_COUNTER = 2**53
_GROK_STOPS = frozenset({"end_turn", "cancelled"})
_GROK_CONTENT_FIELDS = frozenset({"content", "rawInput", "rawOutput"})
_GROK_REQUIRED_KEYS = frozenset({"prompt_id", "sessionUpdate", "stop_reason", "usage"})
_GROK_IGNORABLE_KEYS = frozenset({"elapsed_ms"})
GROK_USAGE_CENSUS_HOST_VERSION = "1.0.30"
"""Host version of the last Grok usage-reader wire census.

Bound to ``tests/fixtures/host_sessions/grok/CONTRACT.md`` by
``test_contract_census_pin_matches_src_constant``. Not the skill-discovery
pin in README.md (that is a different census, ``grok inspect --json``)."""
GrokUpdateClass = Literal["terminal", "usage_less", "ignore", "drift"]
_CANONICAL_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_YEAR_PART = re.compile(r"^\d{4}$")
_MONTH_OR_DAY_PART = re.compile(r"^\d{2}$")
_ROLLOUT_NAME = re.compile(r"^rollout-.*\.jsonl$")

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
"""Why a read did not complete.

``no_metadata_ledger`` is categorically different from every sibling and the
distinction is load-bearing. It means "this store, by design, exposes no
metadata-only usage ledger, so there is nothing here to read and there never
will be" — a standing property of the SOURCE. Every other reason, including
``unsupported``, means "I found data and could not safely interpret it", which
is a FAILURE of this reader (the whole reader fails; Track 31A isolates that
to this reader so a caller never silently omits it from coverage). A caller
may treat ``no_metadata_ledger`` as "this source is not installed"; it must
not do that with any other reason."""
HostTokens = dict[str, dict[str, Usage]]

PERMANENT_REASONS: frozenset[Reason] = frozenset({"unsupported"})
"""Standing blockers a retry alone cannot fix; distinct from source absence."""
PERSISTABLE_REASONS: frozenset[Reason] = frozenset(get_args(Reason)) - {
    "locked",
    "no_metadata_ledger",
}


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
    pricing it. Cursor's disjoint extractor also marks nonzero writes because
    its cache-write semantics and price have no nonzero census evidence; its
    arithmetic identity must still hold. This labels, not suppresses, counters.
    """


@dataclass(frozen=True)
class HostUsageResult:
    """Aggregate totals as ``{host_family: {UTC-day: Usage}}``.

    Empty ``hosts`` with ``complete=True`` is a real completed empty scan.
    Empty ``hosts`` with ``complete=False`` is intentionally *not* a zero.
    ``tokens_by_day`` is the same work as a ``{UTC-day: DayBucket}`` map,
    built atomically with ``hosts`` so the two views cannot drift.
    """

    hosts: HostTokens
    complete: bool
    reason: Reason | None = None
    tokens_by_day: dict[str, DayBucket] = field(default_factory=dict)
    partial_days: frozenset[str] = frozenset()
    """UTC days whose totals this reader declared unattributable.

    Grok writes it for ``usageIsIncomplete`` turns; Codex writes it when
    an inclusive increment carried a nonzero ``cache_create`` (the
    three-term-formula tripwire). Cursor marks nonzero writes and unresolved
    usageRef days too; a day with no known tokens refuses the whole reader.
    Day-scoped on purpose: a lifetime boolean would let one two-year-old
    incomplete turn mark every future snapshot partial forever, while the
    90-day cap had already dropped that day.
    """

    @property
    def empty(self) -> bool:
        return not self.hosts


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

    **String columns stay interned because this graph is large.** Track 63A
    measured 84,910 states across 1,066 rollouts on device 3a6c7dc9,
    2026-09-17, Python 3.14.7. Compact JSON was 4,040,684 bytes; the prior
    1,053-entry cache was 15,870,126 bytes indented / 4,027,507 compact.
    Warm reads took 166.66–168.26 ms before serialization. The separate
    encoding follow-up's 25 MB trigger now refers to compact bytes; see
    docs/invariants/events-retro.md for the phase split and interpreter.
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
class _Fingerprint:
    head: str
    head_len: int
    tail: str
    tail_len: int


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


class _NoCacheCommit(RuntimeError):
    """Escape a locked-json context without its unconditional write."""

    def __init__(self, result: HostUsageResult) -> None:
        self.result = result


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


@contextmanager
def _pause_gc() -> Iterator[None]:
    """Avoid repeated cyclic-GC scans of short-lived reader graphs."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


@_pause_gc()
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
    started = time.monotonic()
    source_root = root if root is not None else CODEX_SESSIONS_PATH
    read_deadline = deadline if deadline is not None else started + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")

    try:
        with locked_json_rmw(
            CACHE_PATH,
            mode=0o600,
            compact=True,
            default_factory=_empty_cache,
            retry_intervals=(),
            on_contention="warn",
            contention_warning="host token cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            prior = (_cached_last_reason(locked.data), _cached_reason_since(locked.data))
            cached_files = _cached_files(locked.data)
            if _expired(read_deadline):
                result, staged_files, learned = _incomplete("deadline"), {}, False
            else:
                result, staged_files, learned = _scan_codex_root(
                    source_root, cached_files, read_deadline
                )
            ready = time.monotonic()
            over_budget = result.complete and _expired(read_deadline)
            now = datetime.now(timezone.utc)
            carried = _carry_reason(*prior, result, now, over_budget=over_budget)
            timing = _carry_read_timing(
                locked.data,
                result,
                carried[0],
                started,
                ready,
                read_deadline,
                now,
                over_budget=over_budget,
            )
            if _skip_failed_cache_write(learned, result, prior, carried, locked.data, timing):
                # Cache hits are staged too. Only newly learned files, a
                # changed (reason, since) pair, or changed timing evidence
                # justify rewriting a failed pass. The pair comparison dates
                # a migrated blocker exactly once.
                raise _NoCacheCommit(result)
            # Cache persistence is DECOUPLED from result validity. Whether the
            # scan may be published is one question; whether we learned
            # something durable about individual files is another. Conflating
            # them is what made a large corpus unable to bootstrap: every
            # bounded scan re-parsed the same prefix, hit the deadline in the
            # same place, and discarded it, so attempt 100 stood exactly where
            # attempt 1 did. Measured on a 452-rollout Mac: six consecutive
            # bounded scans, zero bytes cached.
            locked.data = {
                **timing,
                "version": CACHE_VERSION,
                "last_reason": carried[0],
                "last_reason_since": carried[1],
                # A complete pass observed every rollout on disk, so REPLACING
                # the map is what prunes entries for deleted files. A partial
                # pass must MERGE: replacing would delete the entries for every
                # file it never reached, and the cache would thrash between
                # prefixes instead of converging. Entries for files deleted
                # during a run of partial passes survive until the next
                # complete pass prunes them; they are inert either way, because
                # every entry is revalidated against dev/ino/size/mtime and a
                # head+tail fingerprint before it is trusted.
                "files": staged_files if result.complete else {**cached_files, **staged_files},
            }
            if over_budget:
                # The scan finished but overran its budget: refuse to publish
                # (unchanged), yet keep the cache above so the work counts.
                result = _incomplete("deadline")
        _notice_cache_write_failure(locked.write_error)
        return result
    except _NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return _incomplete("io_error")


def warm_host_cache_inline(
    root: Path | None = None,
    *,
    budget_s: float = DEFAULT_READ_BUDGET_S,
    reader: str = "codex",
) -> HostUsageResult:
    """Read host usage under the attended caller's generous one-off budget.

    The result is the retry: attended capture publishes it through the usual
    reader failure boundary, without a second short-budget read. Autopush
    never calls this helper. Codex/Grok can resume partial progress across
    pushes; Cursor reparses rewritten files and need not converge under the
    same short allowance.
    ``reader`` selects a name in ``events_tail.WARMABLE_HOST_READERS``.

    2026-09-17, device 3a6c7dc9, Python 3.14.7: an empty-cache parse of
    1,066 rollouts took 2,949.68 ms before serialization; warm reads took
    166.66–168.26 ms. The 5 s allowance is cooperative, not an end-to-end
    ceiling. Full interpreter and phase measurements: events-retro.md.
    """
    deadline = time.monotonic() + budget_s
    if reader == "grok":
        return read_grok_usage(root, deadline=deadline, consented=True)
    if reader == "cursor":
        return read_cursor_usage(root, deadline=deadline, consented=True)
    return read_codex_usage(root, deadline=deadline)


def _empty_cursor_cache() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "runs": {}, "complete_once": False}


def _cursor_cached_runs(data: dict[str, Any]) -> dict[str, Any]:
    """Validate authoritative history in full; never salvage/reset a corrupt root."""
    if data.get("version") != CACHE_VERSION:
        raise _ReadFailure("unsupported")
    runs = data.get("runs")
    if not isinstance(runs, dict) or type(data.get("complete_once")) is not bool:
        raise _ReadFailure("malformed")
    for key, run in runs.items():
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise _ReadFailure("malformed")
        if not isinstance(run, dict) or set(run) != {"day", "model", "usage", "partial"}:
            raise _ReadFailure("malformed")
        if not _validated_day(run["day"]) or type(run["partial"]) is not bool:
            raise _ReadFailure("malformed")
        if _validated_table([run["model"]], _MAX_MODEL_ID_BYTES) is None:
            raise _ReadFailure("malformed")
        usage = run["usage"]
        if usage is None and run["partial"]:
            continue  # unresolved usageRef; never a fabricated zero bucket
        if (
            not isinstance(usage, dict)
            or set(usage) != set(TOKEN_FIELDS)
            or not all(_is_valid_counter(n) for n in usage.values())
            or sum(usage.values()) > _MAX_COUNTER
            or (usage["cache_create"] > 0 and not run["partial"])
        ):
            raise _ReadFailure("malformed")
    return runs


def _cursor_day(value: Any) -> str:
    if not _is_nonnegative_int(value) or value < _CURSOR_ENDED_AT_MIN_MS:
        raise _ReadFailure("malformed")
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise _ReadFailure("malformed") from exc


def _cursor_run(row: Any) -> tuple[str, dict[str, Any] | None]:
    """Project only run identity, model, completion day and disjoint counters."""
    if not isinstance(row, dict) or "usage" not in row:
        raise _ReadFailure("malformed")
    run_id = row.get("runId")
    if _validated_table([run_id], _MAX_PROMPT_ID_BYTES) is None:
        raise _ReadFailure("malformed")
    key = hashlib.sha256(run_id.encode()).hexdigest()
    status = row.get("status")
    if not isinstance(status, str) or status not in {"running", "finished"}:
        # Includes renamed terminal states and billable error/cancelled rows.
        raise _ReadFailure("unsupported")
    raw = row["usage"]
    if status == "running":
        if raw is not None or row.get("usageRef") is not None:
            raise _ReadFailure("unsupported")
        return key, None
    day = _cursor_day(row.get("endedAt"))
    model = row.get("model")
    if (
        not isinstance(model, dict)
        or _validated_table([model.get("id")], _MAX_MODEL_ID_BYTES) is None
    ):
        raise _ReadFailure("malformed")
    model_id = model["id"]
    params = model.get("params")
    if not isinstance(params, list):
        raise _ReadFailure("malformed")
    fast = [p.get("value") for p in params if isinstance(p, dict) and p.get("id") == "fast"]
    if len(fast) > 1 or (fast and fast[0] not in ("true", "false")):
        raise _ReadFailure("unsupported")
    # Only the observed normalized Grok id receives this deliberate, UNPRICED
    # pseudo-id. No params axis or new verified rate is implied.
    if model_id == "grok-4.7":
        if not fast:
            raise _ReadFailure("unsupported")
        if fast[0] == "true":
            model_id = "grok-4.7-fast"
    if raw is None:
        if row.get("usageRef") is None:
            raise _ReadFailure("malformed")
        return key, {"day": day, "model": model_id, "usage": None, "partial": True}
    if not isinstance(raw, dict):
        raise _ReadFailure("malformed")
    names = ("inputTokens", "cacheWriteTokens", "cacheReadTokens", "outputTokens")
    if not all(_is_valid_counter(raw.get(n)) for n in (*names, "totalTokens", "reasoningTokens")):
        raise _ReadFailure("malformed")
    if sum(raw[n] for n in names) != raw["totalTokens"]:
        raise _ReadFailure("malformed")
    if raw["reasoningTokens"] > raw["outputTokens"]:
        raise _ReadFailure("malformed")
    usage: Usage = {
        "input": raw["inputTokens"],
        "cache_create": raw["cacheWriteTokens"],
        "cache_read": raw["cacheReadTokens"],
        "output": raw["outputTokens"],
    }
    return key, {
        "day": day,
        "model": model_id,
        "usage": usage,
        "partial": raw["cacheWriteTokens"] > 0,
    }


def _iter_cursor_ledgers(root: Path, deadline: float) -> Iterator[Path]:
    """Only immediate workspace runs.ndjson files; never any content-bearing sibling."""
    for directory in _sorted_children(root, deadline):
        if _expired(deadline):
            raise _ReadFailure("deadline")
        if _is_directory(directory):
            path = directory / "runs.ndjson"
            if _is_regular_non_symlink(path):
                yield path


def _read_cursor_file(path: Path, deadline: float) -> dict[str, Any]:
    before = _regular_stat(path)
    staged: dict[str, Any] = {}
    # O_NONBLOCK closes the check-then-open window where a FIFO would block
    # the push lock. A non-regular descriptor is refused before any read.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise _ReadFailure("stale") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _ReadFailure("stale")
        fp = os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise
    with fp:
        if not _same_source(before, os.fstat(fp.fileno())):
            raise _ReadFailure("stale")
        while True:
            if _expired(deadline):
                raise _ReadFailure("deadline")
            line = fp.readline(MAX_JSONL_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_JSONL_LINE_BYTES or not line.endswith(b"\n"):
                raise _ReadFailure("malformed")
            try:
                key, run = _cursor_run(json.loads(line))
            except ValueError as exc:
                raise _ReadFailure("malformed") from exc
            if key in staged:
                raise _ReadFailure("malformed")
            staged[key] = run
        after_fd = os.fstat(fp.fileno())
    after = _regular_stat(path)
    if (
        not _same_source(before, after_fd)
        or not _same_source(before, after)
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise _ReadFailure("stale")
    return staged


def _cursor_buckets(runs: dict[str, Any]) -> HostUsageResult:
    buckets = HostUsageBuckets()
    for run in runs.values():
        if run["usage"] is not None:
            _add_usage(buckets, run["day"], run["model"], run["usage"])
        if run["partial"]:
            buckets.unattributable_days.add(run["day"])
    if buckets.unattributable_days - buckets.by_day.keys():
        # The writer trims partial_days to actual token days. Refuse rather
        # than letting an unresolved-only day disappear as completed-empty.
        return _incomplete("partial")
    return _result_from_buckets(buckets, partial_days=frozenset(buckets.unattributable_days))


@_pause_gc()
def read_cursor_usage(
    root: Path | None = None, *, deadline: float | None = None, consented: bool = False
) -> HostUsageResult:
    """Cursor via Conductor only. Bare CLI has no persisted billing ledger.

    runs.ndjson is rewritten in place: reparse whole files, replace by hashed
    runId, then reduce once (reverses old day/model/counter contributions).
    Stable whole-file progress commits even on a later deadline, never a torn
    file's prefix. Repeated short passes need NOT converge: cached ids do not
    avoid reparsing. Attended warming / a larger budget is the escape hatch.
    Pruned runs survive for 90 days; runs pruned before any read are unrecoverable.
    """
    if not consented:
        return _incomplete("no_metadata_ledger")
    started = time.monotonic()
    read_deadline = deadline if deadline is not None else started + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")
    source_root = root if root is not None else CURSOR_STORE_PATH
    try:
        with locked_json_durable_rmw(
            CURSOR_CACHE_PATH, default_factory=_empty_cursor_cache
        ) as locked:
            # Shape failures propagate without writing the sole surviving copy.
            runs = dict(_cursor_cached_runs(locked.data))
            prior = (_cached_last_reason(locked.data), _cached_reason_since(locked.data))
            now = datetime.now(timezone.utc)
            cutoff = (now - timedelta(days=CURSOR_HOST_CACHE_RETENTION_DAYS)).date().isoformat()
            runs = {key: run for key, run in runs.items() if run["day"] >= cutoff}
            learned: dict[str, Any] = {}
            removed: set[str] = set()

            def _commit_learned() -> None:
                for key in removed:
                    runs.pop(key, None)
                for key, run in learned.items():
                    runs[key] = run

            try:
                if _expired(read_deadline):
                    raise _ReadFailure("deadline")
                try:
                    root_stat = source_root.lstat()
                except FileNotFoundError:
                    # A finished scan's retained history is authoritative.
                    # A prefix from a deadline is not: publishing it would
                    # latch complete_once on an undercount.
                    if not locked.data["complete_once"]:
                        if not runs and locked.read_state == "missing":
                            locked.write_on_exit = False
                            return _incomplete("no_metadata_ledger")
                        raise _ReadFailure(prior[0] or "stale")
                else:
                    if not stat.S_ISDIR(root_stat.st_mode):
                        raise _ReadFailure("unsupported")
                    seen: dict[str, Any] = {}
                    for path in _iter_cursor_ledgers(source_root, read_deadline):
                        staged = _read_cursor_file(path, read_deadline)
                        if any(key in seen and seen[key] != run for key, run in staged.items()):
                            raise _ReadFailure("malformed")
                        seen.update(staged)
                        for key, run in staged.items():
                            # A running revision invalidates a previous terminal
                            # contribution, just as a changed completion day does.
                            removed.add(key)
                            if run is not None and run["day"] >= cutoff:
                                learned[key] = run
                            else:
                                learned.pop(key, None)
                    # A rejected file must not erase history already learned.
                    _commit_learned()
                result = _cursor_buckets(runs)
            except _ReadFailure as exc:
                result = _incomplete(exc.reason)
                if exc.reason == "deadline":
                    _commit_learned()
            except OSError:
                result = _incomplete("io_error")
            ready = time.monotonic()
            over_budget = result.complete and _expired(read_deadline)
            carried = _carry_reason(*prior, result, now, over_budget=over_budget)
            timing = _carry_read_timing(
                locked.data,
                result,
                carried[0],
                started,
                ready,
                read_deadline,
                now,
                over_budget=over_budget,
            )
            updated = {
                **timing,
                "version": CACHE_VERSION,
                "runs": runs,
                "complete_once": locked.data["complete_once"] or result.complete,
                "last_reason": carried[0],
                "last_reason_since": carried[1],
            }
            locked.write_on_exit = result.complete or updated != locked.data
            locked.data = updated
            if over_budget:
                result = _incomplete("deadline")
        return result
    except LockContended:
        return _incomplete("locked")
    except InvalidJsonCache:
        return _incomplete("malformed")
    except _ReadFailure as exc:
        return _incomplete(exc.reason)
    except (OSError, StorageError):
        # Authoritative history failed to become durable: do not publish success.
        return _incomplete("io_error")


def cursor_usage_diag() -> dict[str, Any]:
    """Inspect durable Cursor history only; never open Conductor run files."""
    blank = {
        **_cached_read_timing({}),
        "cache_state": "missing",
        "complete_once": False,
        "last_reason": None,
        "last_reason_since": None,
        "runs_cached": None,
        "model_count": 0,
        "models": [],
    }
    with locked_json_snapshot(CURSOR_CACHE_PATH, blocking=False) as snap:
        if snap.state == "missing":
            return blank
        if snap.state != "valid":
            reason = {"unreadable": "io_error", "lock_failed": "locked"}.get(
                snap.state, "malformed"
            )
            return {**blank, "cache_state": "unreadable", "last_reason": reason}
        data = snap.data
    try:
        runs = _cursor_cached_runs(data)
    except _ReadFailure as exc:
        return {**blank, "cache_state": "unreadable", "last_reason": exc.reason}
    models = sorted({r["model"] for r in runs.values()})
    return {
        **blank,
        **_cached_read_timing(data),
        "cache_state": "ok",
        "complete_once": data["complete_once"],
        "runs_cached": len(runs),
        "model_count": len(models),
        "models": models[:_DIAG_MODEL_CAP],
        "last_reason": _cached_last_reason(data),
        "last_reason_since": _cached_reason_since(data),
    }


@dataclass(frozen=True)
class HostReaderDiag:
    function: str
    ready_key: str
    ready_value: Any
    label: str


HOST_READER_DIAGS = {
    "codex": HostReaderDiag("codex_usage_diag", "state", "ready", "codex"),
    "grok": HostReaderDiag("grok_usage_diag", "complete_once", True, "grok"),
    "cursor": HostReaderDiag("cursor_usage_diag", "complete_once", True, "Cursor via Conductor"),
}


def reader_usage_diag(reader: str) -> dict[str, Any]:
    # Resolve at call time so patching the owner still reaches every consumer.
    return globals()[HOST_READER_DIAGS[reader].function]()


def reader_cache_cold(reader: str, state: dict[str, Any]) -> bool:
    descriptor = HOST_READER_DIAGS[reader]
    return state.get(descriptor.ready_key) != descriptor.ready_value


def grok_completed_once() -> bool:
    """True after a consented scan finished and saw at least one ledger file.

    Missing, corrupt, or lock-contended cache is pre-success (fail safe).
    Diagnostic only: the host-sweep no longer keys publication policy on this
    latch (Track 31A). ``mm status`` / ``mm diag`` prefer ``last_reason``
    whenever a standing blocker exists, then this latch.
    """
    return grok_usage_diag()["complete_once"] is True


def codex_usage_diag() -> dict[str, Any]:
    """On-disk Codex usage-reader state. Does not open the host store.

    Exists because the per-turn migration is otherwise undiagnosable. It forces
    one full re-walk of every rollout (absence of ``states`` is the version
    discriminator), which an autopush-only Mac completes over several bounded
    passes with nothing on any surface saying so. Before this, ``mm diag``'s
    ``host_usage`` block had exactly one key, ``grok``.

    Cache-only, like its Grok sibling: ``mm diag`` must run without a
    passphrase and without a valid config, so absence, lock contention, and
    unreadable files are reported as ``cache_state``, never raised.

    ``migrating`` is the state a user needs to recognise: some entries are
    already per-turn and some are still pre-Track, so the published Codex
    numbers are incomplete but converging. ``pending`` counts the rollouts on
    disk that no cache entry covers yet.
    """
    blank = {
        **_cached_read_timing({}),
        "cache_state": "missing",
        "state": "cold",
        "files_cached": 0,
        "files_migrated": 0,
        "files_pre_track": 0,
        "files_on_disk": None,
        "pending": None,
        "model_count": 0,
        "models": [],
        "last_reason": None,
        "last_reason_since": None,
    }
    try:
        with locked_json_snapshot(CACHE_PATH) as snap:
            data = snap.data
            state = snap.state
    except OSError:
        return {**blank, "cache_state": "unreadable"}
    if state != "valid" or not isinstance(data, dict):
        return {
            **blank,
            "cache_state": "missing" if state in {"missing", "empty"} else "unreadable",
        }
    files = _cached_files(data)
    migrated = 0
    pre_track = 0
    for value in files.values():
        if not isinstance(value, dict):
            continue
        if value.get("no_ledger") is True or isinstance(value.get("states"), list):
            migrated += 1
        else:
            pre_track += 1
    on_disk: int | None
    try:
        on_disk = sum(
            1 for path in CODEX_SESSIONS_PATH.rglob("*") if _ROLLOUT_NAME.fullmatch(path.name)
        )
    except OSError:
        on_disk = None
    cached = migrated + pre_track
    if not cached:
        phase = "cold"
    elif pre_track:
        phase = "migrating"
    elif on_disk is not None and cached < on_disk:
        phase = "migrating"
    else:
        phase = "ready"
    models = _diag_model_ids(files.values())
    return {
        "cache_state": "ok",
        "state": phase,
        "files_cached": cached,
        "files_migrated": migrated,
        "files_pre_track": pre_track,
        "files_on_disk": on_disk,
        "pending": None if on_disk is None else max(0, on_disk - cached),
        "model_count": models["model_count"],
        "models": models["models"],
        **_cached_read_timing(data),
        "last_reason": _cached_last_reason(data),
        "last_reason_since": _cached_reason_since(data),
    }


def grok_usage_diag() -> dict[str, Any]:
    """Grok cache inventory and ledger path count. Never opens host logs.

    ``mm diag`` must run without a passphrase and without a valid config, so
    this reads the private cache and counts only two-level ledger paths.
    Unknown cache states keep the count unknown, never a fabricated zero.
    """
    on_disk = _count_two_level_ledgers(grok_sessions_root())
    blank = {
        **_cached_read_timing({}),
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
        with locked_json_snapshot(GROK_CACHE_PATH, blocking=False) as snap:
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
    if data.get("version") != CACHE_VERSION or not isinstance(files, dict):
        return blank
    raw_skip = data.get("usage_less_skipped", 0)
    skipped = raw_skip if _is_nonnegative_int(raw_skip) else 0
    models = _diag_grok_model_ids(files)
    return {
        "complete_once": data.get("complete_once") is True,
        "usage_less_skipped": skipped,
        **_cached_read_timing(data),
        "last_reason": _cached_last_reason(data),
        "last_reason_since": _cached_reason_since(data),
        "cache_state": "ok",
        "model_count": models["model_count"],
        "models": models["models"],
        "files_cached": len(files),
        "files_on_disk": on_disk,
    }


_DIAG_MODEL_CAP = 32
"""How many interned model ids the ``mm diag`` PAYLOAD carries. Counts stay
exact, so a truncated list never reads as the whole set. The plain-text render
applies a second, smaller bound of its own (``cli._DIAG_MODELS_SHOWN``)."""


def _diag_model_ids(entries: Any) -> dict[str, Any]:
    """Distinct model ids interned on Codex cache entries. Cache-only.

    Reads the ``models`` string table ``_cache_entry`` already writes, which
    is why this field costs no re-walk.
    """
    found: set[str] = set()
    for value in entries:
        if not isinstance(value, dict):
            continue
        models = value.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if isinstance(model, str) and model:
                found.add(model)
    cleaned = sorted(found)
    return {"model_count": len(cleaned), "models": cleaned[:_DIAG_MODEL_CAP]}


def _diag_grok_model_ids(files: dict[str, Any]) -> dict[str, Any]:
    """Distinct model ids on Grok cache turns. Cache-only."""
    found: set[str] = set()
    for value in files.values():
        if not isinstance(value, dict):
            continue
        turns = value.get("turns")
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            model = turn.get("model")
            if isinstance(model, str) and model:
                found.add(model)
    cleaned = sorted(found)
    return {"model_count": len(cleaned), "models": cleaned[:_DIAG_MODEL_CAP]}


def grok_sessions_root() -> Path:
    """Resolve the Grok sessions directory at call time."""
    env = os.environ.get("GROK_HOME")
    if env:
        return Path(env).expanduser() / "sessions"
    return GROK_SESSIONS_PATH


def _count_two_level_ledgers(root: Path) -> int | None:
    """Count reader-visible ``*/*/updates.jsonl`` without swallowing scan errors.

    ``Path.glob`` on Python 3.13 suppresses directory-scan ``OSError``, so an
    unreadable Grok store would render as empty. A missing root is 0. A
    symlink or non-directory root is unknown (the reader refuses those as
    unsupported). Directory and file predicates match ``_iter_grok_ledgers``.
    """
    try:
        if not root.exists():
            return 0
        if root.is_symlink() or not root.is_dir():
            return None
    except OSError:
        return None
    try:
        count = 0
        with os.scandir(root) as workspaces:
            for workspace in workspaces:
                workspace_path = Path(workspace.path)
                if not _is_directory(workspace_path):
                    continue
                with os.scandir(workspace.path) as sessions:
                    for session in sessions:
                        session_path = Path(session.path)
                        if not _is_directory(session_path):
                            continue
                        if _is_regular_non_symlink(session_path / "updates.jsonl"):
                            count += 1
        return count
    except (_ReadFailure, OSError):
        return None


@_pause_gc()
def read_grok_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
    consented: bool = False,
) -> HostUsageResult:
    """Read completed Grok turn totals from ``updates.jsonl`` terminal records.

    Closed by default: ``consented=False`` does not stat or open the store.
    A caller that has the local opt-in must pass ``consented=True``.
    """
    started = time.monotonic()
    if not consented:
        return _incomplete("no_metadata_ledger")
    source_root = root if root is not None else grok_sessions_root()
    read_deadline = deadline if deadline is not None else started + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")

    try:
        with locked_json_rmw(
            GROK_CACHE_PATH,
            mode=0o600,
            compact=True,
            default_factory=_empty_grok_cache,
            retry_intervals=(),
            on_contention="warn",
            contention_warning="host token adapter cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            prior = (_cached_last_reason(locked.data), _cached_reason_since(locked.data))
            cached_files = _cached_files(locked.data)
            prior_complete = (
                locked.data.get("version") == CACHE_VERSION
                and locked.data.get("complete_once") is True
            )
            if _expired(read_deadline):
                result, staged_files, learned, saw_files = _incomplete("deadline"), {}, False, False
            else:
                result, staged_files, learned, saw_files = _scan_grok_root(
                    source_root, cached_files, read_deadline
                )
            ready = time.monotonic()
            over_budget = result.complete and _expired(read_deadline)
            now = datetime.now(timezone.utc)
            carried = _carry_reason(*prior, result, now, over_budget=over_budget)
            timing = _carry_read_timing(
                locked.data,
                result,
                carried[0],
                started,
                ready,
                read_deadline,
                now,
                over_budget=over_budget,
            )
            # Failed passes write only newly learned files, a changed pair,
            # or changed timing evidence, including the first observation of
            # a migrated, undated blocker.
            if _skip_failed_cache_write(learned, result, prior, carried, locked.data, timing):
                raise _NoCacheCommit(result)
            complete_once = prior_complete or (result.complete and saw_files)
            files = staged_files if result.complete else {**cached_files, **staged_files}
            # A partial scan merges durable per-file entries with the existing
            # cache. Derive the diagnostic tally from that merged view too;
            # retaining the old root total would make ``mm diag`` hide a
            # usage-less turn learned before the deadline.
            skip_total = sum(
                entry.get("usage_less_skipped", 0)
                for entry in files.values()
                if isinstance(entry, dict)
                and _is_nonnegative_int(entry.get("usage_less_skipped", 0))
            )
            locked.data = {
                **timing,
                "version": CACHE_VERSION,
                "complete_once": complete_once,
                "usage_less_skipped": skip_total,
                "last_reason": carried[0],
                "last_reason_since": carried[1],
                "files": files,
            }
            if over_budget:
                result = _incomplete("deadline")
        _notice_cache_write_failure(locked.write_error)
        return result
    except _NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return _incomplete("io_error")


def _empty_grok_cache() -> dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "complete_once": False,
        "usage_less_skipped": 0,
        "last_reason": None,
        "last_reason_since": None,
        "files": {},
    }


def _cached_last_reason(data: dict[str, Any]) -> Reason | None:
    """Read a standing blocker off either current-version cache root.

    Absence is documentary, not a migration gate. Garbage and non-persistable
    reasons are absent so a hand-edited cache cannot forge diagnostic text.
    """
    if data.get("version") != CACHE_VERSION:
        return None
    raw = data.get("last_reason")
    if isinstance(raw, str) and raw in PERSISTABLE_REASONS:
        return raw  # type: ignore[return-value]
    return None


def _cached_reason_since(data: dict[str, Any]) -> str | None:
    """Validated, normalized UTC first-observed time; never an orphan date.

    A bad date does not erase a valid reason. A subsequent failed read dates
    an undated blocker once, without inventing when the original failure began.
    """
    if _cached_last_reason(data) is None:
        return None
    return _cached_timestamp(data, "last_reason_since")


def _cached_timestamp(data: dict[str, Any], key: str) -> str | None:
    if data.get("version") != CACHE_VERSION:
        return None
    raw = data.get(key)
    if not isinstance(raw, str) or len(raw) > _MAX_REASON_SINCE_CHARS or not raw.isascii():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        pass
    return None


_MAX_READ_MS = 86_400_000


def _cached_read_ms(data: dict[str, Any], key: str) -> int | None:
    raw = data.get(key)
    if data.get("version") == CACHE_VERSION and type(raw) is int and 0 <= raw <= _MAX_READ_MS:
        return raw
    return None


def _cached_read_timing(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_complete_ms": _cached_read_ms(data, "last_complete_ms"),
        "last_complete_at": _cached_timestamp(data, "last_complete_at"),
        "last_deadline_allotted_ms": (
            _cached_read_ms(data, "last_deadline_allotted_ms")
            if _cached_last_reason(data) == "deadline"
            else None
        ),
    }


def _carry_read_timing(
    prior: dict[str, Any],
    result: HostUsageResult,
    reason: Reason | None,
    started: float,
    ready: float,
    deadline: float,
    now: datetime,
    *,
    over_budget: bool,
) -> dict[str, Any]:
    """Carry validated metadata on partial writes; measure before serialization."""
    timing = _cached_read_timing(prior)
    if result.complete:
        timing["last_complete_ms"] = min(_MAX_READ_MS, max(0, round((ready - started) * 1000)))
        timing["last_complete_at"] = now.isoformat(timespec="seconds")
    if reason != "deadline":
        timing.pop("last_deadline_allotted_ms", None)
    elif over_budget or result.reason == "deadline":
        timing["last_deadline_allotted_ms"] = min(
            _MAX_READ_MS, max(0, round((deadline - started) * 1000))
        )
    return {key: value for key, value in timing.items() if value is not None}


def _skip_failed_cache_write(
    learned: bool,
    result: HostUsageResult,
    prior: tuple[Reason | None, str | None],
    carried: tuple[Reason | None, str | None],
    prior_root: dict[str, Any],
    timing: dict[str, Any],
) -> bool:
    """Skip a failed rewrite only when files, blocker, and timing are unchanged."""
    if learned or result.complete or prior != carried:
        return False
    prior_timing = {
        key: value for key, value in _cached_read_timing(prior_root).items() if value is not None
    }
    return timing == prior_timing


def _carry_reason(
    prior_reason: Reason | None,
    prior_since: str | None,
    result: HostUsageResult,
    now: datetime,
    *,
    over_budget: bool = False,
) -> tuple[Reason | None, str | None]:
    """Carry the standing read blocker, not the latest publication outcome.

    A complete in-budget read clears it. A complete late read proves any
    permanent blocker gone, but records deadline as the current blocker.
    A permanent blocker survives later transient failures until a read completes.
    Locked/absent attempts cannot persist. ``now`` dates this version's first
    observation of the current reason, never the original onset of an outage.
    Callers validate prior fields and compare the pair when deciding to write.
    """
    if result.complete:
        if over_budget:
            return "deadline", (
                prior_since if prior_reason == "deadline" and prior_since else now.isoformat()
            )
        return None, None
    if result.reason not in PERSISTABLE_REASONS:
        return prior_reason, prior_since
    reason = prior_reason if prior_reason in PERMANENT_REASONS else result.reason
    since = prior_since if reason == prior_reason and prior_since is not None else now.isoformat()
    return reason, since


def _notice_cache_write_failure(error: OSError | None) -> None:
    if error is not None:
        name = errno.errorcode.get(error.errno, "OSError")
        sys.stderr.write(f"mm: notice: host token cache write failed: {name}\n")


def _scan_grok_root(
    root: Path,
    cached_files: dict[str, Any],
    deadline: float,
) -> tuple[HostUsageResult, dict[str, Any], bool, bool]:
    """Returns ``(result, staged, learned, saw_files)``."""
    try:
        ledgers = list(_iter_grok_ledgers(root, deadline))
    except _ReadFailure as failure:
        return _incomplete(failure.reason), {}, False, False

    canonical_root = root.resolve()
    staged: dict[str, Any] = {}
    learned = False
    for workspace, session_id, path in ledgers:
        if _expired(deadline):
            return _incomplete("deadline"), staged, learned, True
        key = _cache_key(path, root=root, canonical_root=canonical_root)
        try:
            before = _regular_stat(path)
            existing = _validated_grok_entry(cached_files.get(key))
            entry = _grok_cache_hit(path, before, existing, deadline)
            if entry is None:
                resume = _grok_resumable_entry(path, before, existing, deadline)
                entry = (
                    _resume_grok_file(path, workspace, session_id, before, resume, deadline)
                    if resume is not None
                    else _read_full_grok_file(path, workspace, session_id, before, deadline)
                )
                learned = True
            staged[key] = entry
        except _ReadFailure as failure:
            return _incomplete(failure.reason), staged, learned, True

    return (
        _result_from_buckets(
            _aggregate_grok(staged.values()),
            partial_days=_grok_partial_days(staged.values()),
        ),
        staged,
        learned,
        bool(ledgers),
    )


def _iter_grok_ledgers(root: Path, deadline: float):
    """Yield ``(workspace, session_id, updates.jsonl)`` under ``root``."""
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise _ReadFailure("unsupported")
        for workspace in _sorted_children(root, deadline):
            if not _is_directory(workspace):
                continue
            for session in _sorted_children(workspace, deadline):
                if not _is_directory(session):
                    continue
                candidate = session / "updates.jsonl"
                if _is_regular_non_symlink(candidate):
                    yield workspace.name, session.name, candidate
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _grok_cache_hit(
    path: Path,
    before: os.stat_result,
    entry: dict[str, Any] | None,
    deadline: float,
) -> dict[str, Any] | None:
    if entry is None:
        return None
    if not _same_cache_metadata(entry, before):  # type: ignore[arg-type]
        return None
    fingerprint = _fingerprint(path, before, deadline)
    if not _same_fingerprint(entry, fingerprint):  # type: ignore[arg-type]
        return None
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return entry


def _grok_resumable_entry(
    path: Path,
    source: os.stat_result,
    entry: dict[str, Any] | None,
    deadline: float,
) -> dict[str, Any] | None:
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


def _read_full_grok_file(
    path: Path,
    workspace: str,
    session_id: str,
    before: os.stat_result,
    deadline: float,
) -> dict[str, Any]:
    turns, skipped, new_partial = _read_grok_file(
        path, workspace, session_id, 0, {}, before, deadline
    )
    return _grok_file_entry(path, before, deadline, turns, skipped, new_partial=new_partial)


def _resume_grok_file(
    path: Path,
    workspace: str,
    session_id: str,
    before: os.stat_result,
    entry: dict[str, Any],
    deadline: float,
) -> dict[str, Any]:
    prior = {turn["key"]: turn for turn in entry["turns"]}
    turns, skipped, new_partial = _read_grok_file(
        path, workspace, session_id, entry["offset"], prior, before, deadline
    )
    prior_skip = entry.get("usage_less_skipped", 0)
    if not _is_nonnegative_int(prior_skip):
        prior_skip = 0
    prior_partial = entry.get("partial_days") or ()
    return _grok_file_entry(
        path,
        before,
        deadline,
        turns,
        prior_skip + skipped,
        prior_partial=prior_partial,
        new_partial=new_partial,
    )


def _grok_file_entry(
    path: Path,
    before: os.stat_result,
    deadline: float,
    turns: dict[str, dict[str, Any]],
    usage_less_skipped: int,
    *,
    prior_partial: Any = (),
    new_partial: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    after = _regular_stat(path)
    if not _same_source(before, after):
        raise _ReadFailure("stale")
    fingerprint = _fingerprint(path, after, deadline)
    if not _same_source(after, _regular_stat(path)):
        raise _ReadFailure("stale")
    # Days live on the file entry, never on stored turns. A turn-level
    # ``incomplete`` key made resume equality fail when jsonl restated the
    # same terminal: cache turns omit the key, live turns had it, and
    # ``existing == turn`` raised unsupported for the whole reader.
    partial_days: set[str] = set(new_partial)
    if isinstance(prior_partial, (list, tuple, set, frozenset)):
        partial_days.update(day for day in prior_partial if isinstance(day, str))
    stored_turns = [
        {
            "key": turns[key]["key"],
            "day": turns[key]["day"],
            "model": turns[key]["model"],
            "usage": turns[key]["usage"],
        }
        for key in sorted(turns)
    ]
    return {
        **_identity_fields(after, fingerprint),
        "turns": stored_turns,
        "usage_less_skipped": usage_less_skipped,
        # Always written, including as ``[]``. Absence is the pre-34A
        # discriminator and forces one re-walk; an empty list means this
        # file was walked post-34A and had no incomplete turns. NOT a
        # CACHE_VERSION bump: that constant is shared with the Codex
        # namespace.
        "partial_days": sorted(partial_days),
        # Key-absence is the pre-35A discriminator. Inclusive cached turns
        # would be published under a disjoint marker if we reused them;
        # force one re-walk. NOT a CACHE_VERSION bump (shared with Codex).
        # Same shape as the v0.12.50 ``partial_days`` gate.
        "counter_semantics": "disjoint-v1",
    }


def _read_grok_file(
    path: Path,
    workspace: str,
    session_id: str,
    start_offset: int,
    prior: dict[str, dict[str, Any]],
    before: os.stat_result,
    deadline: float,
) -> tuple[dict[str, dict[str, Any]], int, frozenset[str]]:
    turns = dict(prior)
    last_offset = start_offset
    usage_less_skipped = 0
    incomplete_days: set[str] = set()
    try:
        with path.open("rb") as fp:
            fp.seek(start_offset)
            for raw, end_offset in iter_bounded_lines(
                fp,
                _cache_key(path),
                start_offset,
                label="grok usage walker",
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
                parsed = _grok_turns_from_record(record, workspace, session_id)
                if parsed is None:
                    continue
                accepted, record_days = parsed
                if accepted == []:
                    usage_less_skipped += 1
                    continue
                incomplete_days.update(record_days)
                for key, turn in accepted:
                    existing = turns.get(key)
                    if existing is None:
                        turns[key] = turn
                        continue
                    if existing == turn:
                        continue
                    raise _ReadFailure("unsupported")
            if fp.tell() != last_offset:
                raise _ReadFailure("partial")
    except OSError as exc:
        raise _ReadFailure("io_error") from exc
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return turns, usage_less_skipped, frozenset(incomplete_days)


def _grok_turns_from_record(
    record: Any, workspace: str, session_id: str
) -> tuple[list[tuple[str, dict[str, Any]]], frozenset[str]] | None:
    if not isinstance(record, dict):
        raise _ReadFailure("malformed")
    params = record.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") != "turn_completed":
        return None
    kind = _classify_grok_update(update)
    if kind == "ignore":
        return None
    if kind == "usage_less":
        return [], frozenset()
    if kind == "drift":
        raise _ReadFailure("unsupported")
    if kind != "terminal":
        raise _ReadFailure("unsupported")
    day = _grok_outer_day(record.get("timestamp"))
    prompt_id = update.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise _ReadFailure("unsupported")
    if len(prompt_id.encode("utf-8")) > _MAX_PROMPT_ID_BYTES:
        raise _ReadFailure("unsupported")
    stop = update.get("stop_reason")
    if not isinstance(stop, str) or stop not in _GROK_STOPS:
        raise _ReadFailure("unsupported")
    usage = update.get("usage")
    if not isinstance(usage, dict):
        raise _ReadFailure("unsupported")
    models = usage.get("modelUsage")
    if not isinstance(models, dict) or not models:
        raise _ReadFailure("unsupported")
    _validate_grok_counters(usage)
    # Identity check, never truthiness: usageIsIncomplete is peer-controlled.
    # ``"yes"`` / ``1`` / ``null`` / ``"false"`` must not become a claim.
    # Do NOT stamp the flag onto the turn dict: stored turns are
    # {key, day, model, usage}, and resume compares live==cached.
    incomplete = usage.get("usageIsIncomplete") is True
    accepted: list[tuple[str, dict[str, Any]]] = []
    cache_create_nonzero = False
    for model, entry in models.items():
        if not isinstance(model, str) or not model:
            raise _ReadFailure("unsupported")
        if len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
            raise _ReadFailure("unsupported")
        if not isinstance(entry, dict):
            raise _ReadFailure("unsupported")
        counters = _validate_grok_counters(entry)
        if counters["cache_create"] > 0:
            cache_create_nonzero = True
        model_key = _grok_terminal_key(workspace, session_id, prompt_id, model)
        accepted.append(
            (
                model_key,
                {
                    "key": model_key,
                    "day": day,
                    "model": model,
                    "usage": counters,
                },
            )
        )
    days = frozenset({day} if (incomplete or cache_create_nonzero) and accepted else ())
    return accepted, days


def _classify_grok_update(update: dict[str, Any]) -> GrokUpdateClass:
    """Classify a ``turn_completed`` update object's key set.

    Three tiers, matching ``_GROK_CONTENT_FIELDS`` / ``_GROK_IGNORABLE_KEYS``
    / ``_GROK_REQUIRED_KEYS``. An ignorable key is dropped before the
    required-set comparison so a usage-less terminal that also carries
    ``elapsed_ms`` stays a skip, not a drift. Unknown extra keys are
    ``drift`` and refuse this reader. See "Reader-agnostic quarantine and
    drift classification" in ``docs/roadmap-future.md``. Sharing filesystem
    resume primitives is separately deferred there pending measured need.
    """
    keys = set(update)
    if _GROK_CONTENT_FIELDS & keys:
        return "ignore"
    projected = keys - _GROK_IGNORABLE_KEYS
    if projected == _GROK_REQUIRED_KEYS:
        return "terminal"
    if projected == _GROK_REQUIRED_KEYS - {"usage"}:
        return "usage_less"
    return "drift"


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
    return dict(
        zip(TOKEN_FIELDS, _normalize_inclusive_counters(tuple(usage[key] for key in TOKEN_FIELDS)))
    )


def _normalize_inclusive_counters(counters: tuple[int, ...]) -> tuple[int, ...]:
    """Single normalization rule for both the tuple and mapping consumers."""
    input_tokens, cache_create, cache_read, output = counters
    if cache_read + cache_create > input_tokens:
        raise _ReadFailure("malformed")
    return input_tokens - cache_read - cache_create, cache_create, cache_read, output


def _validate_grok_counters(usage: dict[str, Any]) -> Usage:
    output = _grok_counter(usage, "outputTokens")
    reasoning = _grok_counter(usage, "reasoningTokens")
    if reasoning > output:
        raise _ReadFailure("unsupported")
    _grok_counter(usage, "totalTokens")
    return _normalize_inclusive_usage(
        {
            "input": _grok_counter(usage, "inputTokens"),
            "cache_create": _grok_counter(usage, "cacheCreationTokens"),
            "cache_read": _grok_counter(usage, "cachedReadTokens"),
            "output": output,
        }
    )


def _grok_counter(usage: dict[str, Any], key: str) -> int:
    if key not in usage:
        raise _ReadFailure("unsupported")
    value = usage[key]
    if not _is_valid_counter(value):
        raise _ReadFailure("unsupported")
    return value


def _grok_outer_day(value: Any) -> str:
    return _utc_day(value)


def _grok_terminal_key(workspace: str, session_id: str, prompt_id: str, model: str) -> str:
    return hashlib.sha256(f"{workspace}\0{session_id}\0{prompt_id}\0{model}".encode()).hexdigest()


def _validated_grok_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    integer_keys = ("dev", "ino", "size", "mtime_ns", "head_len", "tail_len", "offset")
    if any(not _is_nonnegative_int(value.get(key)) for key in integer_keys):
        return None
    if value["offset"] != value["size"]:
        return None
    if not isinstance(value.get("head"), str) or not isinstance(value.get("tail"), str):
        return None
    turns = value.get("turns")
    if not isinstance(turns, list):
        return None
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            return None
        key = turn.get("key")
        if not isinstance(key, str) or len(key) != 64 or key in seen:
            return None
        seen.add(key)
        model = turn.get("model")
        day = turn.get("day")
        usage = turn.get("usage")
        if not isinstance(model, str) or not model:
            return None
        if len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
            return None
        if not isinstance(day, str) or not _CANONICAL_DAY.fullmatch(day):
            return None
        if not isinstance(usage, dict):
            return None
        if any(not _is_valid_counter(usage.get(field)) for field in TOKEN_FIELDS):
            return None
        normalized.append(
            {
                "key": key,
                "day": day,
                "model": model,
                "usage": {field: usage[field] for field in TOKEN_FIELDS},
            }
        )
    skip = value.get("usage_less_skipped", 0)
    if not _is_nonnegative_int(skip):
        return None
    # Key-absence is the pre-34A discriminator. A bump of CACHE_VERSION
    # would also invalidate the Codex namespace. Same shape
    # as the v0.12.15 offset/head gate and the D2 skills gate: force one
    # re-walk of this file, then persist the marker (possibly empty).
    if "partial_days" not in value:
        return None
    partial_days = _validated_grok_partial_days(value.get("partial_days"))
    if partial_days is None:
        return None
    # Pre-35A entries stored inclusive counters. Re-walk once.
    if value.get("counter_semantics") != "disjoint-v1":
        return None
    return {
        **_identity_fields_from(value),
        "turns": normalized,
        "usage_less_skipped": skip,
        "partial_days": partial_days,
        "counter_semantics": "disjoint-v1",
    }


def _validated_grok_partial_days(value: Any) -> list[str] | None:
    """Normalize a cached ``partial_days`` list, or reject the entry.

    Malformed is a re-walk, never a silent empty. Duplicates and
    non-canonical days are malformed, not coerced.
    """
    if not isinstance(value, list):
        return None
    seen: set[str] = set()
    out: list[str] = []
    for day in value:
        if not isinstance(day, str) or not _CANONICAL_DAY.fullmatch(day):
            return None
        if day in seen:
            return None
        seen.add(day)
        out.append(day)
    return sorted(out)


def _grok_partial_days(entries: Any) -> frozenset[str]:
    days: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for day in entry.get("partial_days") or ():
            if isinstance(day, str):
                days.add(day)
    return frozenset(days)


def _aggregate_grok(entries: Any) -> HostUsageBuckets:
    """Grok's reduction is `_aggregate`'s ``turns`` branch. Kept as a named
    alias because the Grok scan reads better with it, not because the reduction
    differs — it deliberately does not, and a second implementation here is how
    the two readers drifted apart in the first place."""
    return _aggregate(entry for entry in entries if isinstance(entry, dict))


def _utc_day(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _ReadFailure("unsupported") from exc
        if parsed.tzinfo is None:
            raise _ReadFailure("unsupported")
        return parsed.astimezone(timezone.utc).date().isoformat()
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise _ReadFailure("unsupported")
    seconds = value / 1000 if value >= 100_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise _ReadFailure("unsupported") from exc


def _scan_codex_root(
    root: Path,
    cached_files: dict[str, Any],
    deadline: float,
) -> tuple[HostUsageResult, dict[str, _CacheEntry], bool]:
    """Returns ``(result, staged, learned)``.

    ``learned`` is True only when at least one entry was NEWLY computed. Cache
    HITS are staged too, so a non-empty ``staged`` does not mean the scan
    discovered anything — on a fully-warm machine with one permanently
    unreadable rollout, committing on ``staged`` alone would rewrite the entire
    cache with byte-identical content on every single push.
    """
    try:
        rollouts = list(_iter_rollouts(root, deadline))
    except _ReadFailure as failure:
        return _incomplete(failure.reason), {}, False

    canonical_root = root.resolve()
    staged: dict[str, _CacheEntry] = {}
    learned = False
    for path in rollouts:
        if _expired(deadline):
            return _incomplete("deadline"), staged, learned
        key = _cache_key(path, root=root, canonical_root=canonical_root)
        try:
            before = _regular_stat(path)
            existing = _validated_entry(cached_files.get(key))
            entry = _cache_hit(path, before, existing, deadline)
            if entry is None:  # cache miss
                resume = _resumable_entry(path, before, existing, deadline)
                entry = (
                    _resume_rollout(path, before, resume, deadline)
                    if resume is not None
                    else _read_full_rollout(path, before, deadline)
                )
                learned = True
            staged[key] = entry
        except _ReadFailure as failure:
            # Hand back what was staged BEFORE the failure. Each entry is a
            # complete, fingerprinted parse of one stable file, so it stays
            # valid regardless of what a later file did. The result is still
            # incomplete — the caller publishes nothing — but the work is not
            # thrown away. See `read_codex_usage` for why that matters.
            return _incomplete(failure.reason), staged, learned

    try:
        buckets = _aggregate(staged.values())
    except _ReadFailure as failure:
        return _incomplete(failure.reason), staged, learned
    return (
        _result_from_buckets(buckets, partial_days=frozenset(buckets.unattributable_days)),
        staged,
        learned,
    )


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
    """True for a regular, non-symlink file. Absence is not an I/O error.

    Shared by the Grok walker (speculative ``session / "updates.jsonl"``)
    and the Codex walker (``iterdir()`` then ``lstat()``). ``FileNotFoundError``
    and ``NotADirectoryError`` mean the candidate is gone — skip it.
    Every other ``OSError`` (a permission error on a file that exists) stays
    fatal so the all-or-nothing reader contract still sees a real failure.
    """
    try:
        st = path.lstat()
        return not stat.S_ISLNK(st.st_mode) and stat.S_ISREG(st.st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _read_full_rollout(path: Path, before: os.stat_result, deadline: float) -> _CacheEntry:
    """Return this rollout's cache entry — ledger or no-ledger.

    A rollout with no usage ledger still gets an entry, so an unchanged one
    costs a stat + fingerprint on later scans instead of a full re-parse. It
    must NOT be given a synthetic model or day: ``_aggregate`` would fabricate
    a family bucket out of it. See ``_CacheEntry``.
    """
    walk = _read_rollout(path, 0, None, before, deadline)
    after = _regular_stat(path)
    if not _same_source(before, after):
        raise _ReadFailure("stale")
    fingerprint = _fingerprint(path, after, deadline)
    if not _same_source(after, _regular_stat(path)):
        raise _ReadFailure("stale")
    if walk is None:
        return _no_ledger_entry(after, fingerprint)
    return _cache_entry(after, fingerprint, walk)


def _cache_hit(
    path: Path,
    before: os.stat_result,
    entry: _CacheEntry | None,
    deadline: float,
) -> _CacheEntry | None:
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
    entry: _CacheEntry | None,
    deadline: float,
) -> _CacheEntry | None:
    """Return a prior entry only when this is a verified append.

    The prior head and the bytes that were the old tail must still match.
    This catches common same-path rewrites before using the complete-line
    offset. Any doubt falls back to a bounded full parse.
    """
    if entry is None:
        return None
    if entry.get("no_ledger"):
        # A no-ledger entry cannot seed a resume: it has no terminal to carry
        # forward AND no remembered `turn_context` model. Resuming from its
        # offset would meet the file's first ledger with `current_model=None`,
        # which `_read_rollout` then refuses as unattributable — turning a file
        # that just gained its first response into a whole-store refusal. Fall
        # back to a full parse, which re-reads the model context.
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
    walk = _read_rollout(path, entry["offset"], _walk_from_entry(entry), before, deadline)
    if walk is None:
        # Unreachable today: the carried walk already holds this file's states,
        # so a resumed read always returns them. Guarded rather than asserted
        # so a future change to `_read_rollout` degrades to a bounded refusal
        # instead of a TypeError inside `_cache_entry`.
        raise _ReadFailure("unsupported")
    after = _regular_stat(path)
    if not _same_source(before, after):
        raise _ReadFailure("stale")
    fingerprint = _fingerprint(path, after, deadline)
    if not _same_source(after, _regular_stat(path)):
        raise _ReadFailure("stale")
    return _cache_entry(after, fingerprint, walk)


def _walk_from_entry(entry: _CacheEntry) -> _Walk:
    """Rehydrate the carried walk state of an earlier segment.

    Every field matters. Dropping ``states`` would lose this file's share of a
    turn that other files also observed; dropping ``last_total`` would make the
    first appended ledger difference against nothing; dropping ``last_model`` /
    ``last_turn`` would meet it with no attribution and refuse the whole store;
    dropping ``pending`` would strand ledgers whose ``turn_context`` had not
    arrived when the previous segment ended.
    """
    walk = _Walk(
        states=[_TurnState(*parts) for parts in _entry_states(entry)],
        pending=[
            (tuple(total), day, (tuple(last) if last is not None else None))
            for total, day, last in (entry.get("pending") or ())
        ],
        turn=entry.get("last_turn", ""),
    )
    last_total = entry.get("last_total")
    if last_total is not None:
        walk.last_total = tuple(last_total)
    model = entry.get("last_model")
    if model:
        walk.model = model
    return walk


def _read_rollout(
    path: Path,
    start_offset: int,
    previous: _Walk | None,
    before: os.stat_result,
    deadline: float,
) -> _Walk | None:
    """Read a stable file from a complete-line offset, collecting turn states.

    Returns ``None`` when the rollout recorded no usage ledger at all — an
    abandoned or response-less session contributes nothing, which is a fact
    about that file rather than a reason to refuse the whole store. Refusal is
    reserved for a ledger we saw but could not attribute (see below).

    ``previous`` is the carried walk state of an earlier segment of the SAME
    file (see ``_resume_rollout``). It supplies the cumulative baseline the
    first appended ledger is differenced against, the inherited model and turn,
    and any pre-``turn_context`` ledgers that segment could not yet attribute.
    """
    walk = previous if previous is not None else _Walk()
    current_model = walk.model
    saw_usage_ledger = previous is not None and bool(walk.states)
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
                    walk.model = current_model
                    walk.turn = _context_turn(record)
                    # A `turn_context` is the first thing that can attribute a
                    # pre-context ledger, so flush the buffer HERE. Under the
                    # cumulative reading this buffer did not exist: the code
                    # dropped those records and a comment justified it with
                    # "totals are CUMULATIVE, so a later attributable record
                    # restates these tokens". Per-turn accounting deletes that
                    # premise, and on a real corpus the dropped prefix is
                    # 1,557 records across 7 rollouts worth 209,515,399 input
                    # tokens. Dropping them now would be silent loss.
                    _flush_pending(walk)
                    continue
                if record_type == "event_msg" and _is_token_count(record):
                    if not _carries_usage(record):
                        # Codex emits a `token_count` whose `payload.info` is
                        # null at the start of a turn, before the model has
                        # reported anything. It is a marker with no ledger
                        # attached, not a malformed record — 33% of rollouts
                        # on a real Codex machine carry one, and treating it
                        # as fatal refused the entire store.
                        continue
                    saw_usage_ledger = True
                    total, day, last = _reading_from_record(record)
                    if current_model is None:
                        # Not attributable YET. Buffer rather than drop; the
                        # next `turn_context` flushes it. If none ever arrives
                        # the file is refused below, never silently zeroed.
                        walk.pending.append((total, day, last))
                        continue
                    walk.states.append(_TurnState(walk.turn, total, day, current_model, last))
                    walk.last_total = total
            if fp.tell() != last_offset:
                # `iter_bounded_lines` intentionally leaves a trailing
                # unterminated write behind the persisted offset.
                raise _ReadFailure("partial")
    except OSError as exc:
        raise _ReadFailure("io_error") from exc

    if not walk.states and walk.pending:
        # We read real token counts and could not attribute a single one to a
        # model. That is a shape this reader does not understand, and silently
        # dropping it would under-report usage — refuse the store instead.
        # NOTE the buffer must NOT rescue this case: a file whose only ledgers
        # precede every `turn_context` is exactly the shape
        # `test_missing_model_before_token_is_incomplete` pins as fatal.
        raise _ReadFailure("unsupported")
    if not walk.states and saw_usage_ledger:
        raise _ReadFailure("unsupported")
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return walk if walk.states else None


@dataclass
class _Walk:
    """Mutable walk state for ONE rollout, carried across segment boundaries.

    Mutable on purpose: it is threaded through a resume, persisted to the cache
    entry, and rehydrated. These fields are exactly what a resumed segment
    cannot reconstruct from the appended bytes alone.
    """

    states: list[_TurnState] = field(default_factory=list)
    pending: list[tuple[tuple[int, ...], str, tuple[int, ...] | None]] = field(default_factory=list)
    last_total: tuple[int, ...] | None = None
    model: str | None = None
    turn: str = ""


def _flush_pending(walk: _Walk) -> None:
    """Attribute buffered pre-``turn_context`` ledgers to the now-known model.

    Called only from the ``turn_context`` branch, so ``walk.model`` is set.
    Attributing an EARLIER ledger to a LATER context is normally forbidden
    here — `test_model_context_after_token_is_not_retroactively_used` pins
    that — but this case is different in kind and the difference is why the
    buffer is safe: those records had no candidate model at all, and the
    established stance for an unattributable ledger (see
    `test_missing_model_before_token_is_incomplete`) is to refuse, never to
    drop. Attributing to the first model the file names is the only reading
    that neither refuses a routine shape nor loses real tokens.
    """
    if not walk.pending or walk.model is None:
        return
    for total, day, last in walk.pending:
        walk.states.append(_TurnState(walk.turn, total, day, walk.model, last))
        walk.last_total = total
    walk.pending.clear()


def _reading_from_record(
    record: dict[str, Any],
) -> tuple[tuple[int, ...], str, tuple[int, ...] | None]:
    """Extract ``(cumulative_total, utc_day, last_token_usage)`` from a ledger.

    Both counter maps go through `_counter`, so the required/optional split and
    the `_MAX_COUNTER` bound still apply per record. `last_token_usage` is
    optional at this layer: every record on a real corpus carries it, but the
    first-state rule tolerates its absence by falling back to the cumulative,
    which is correct for a rollout that inherited nothing.
    """
    payload = record.get("payload")
    info = payload.get("info") if isinstance(payload, dict) else None
    totals = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(totals, dict):
        raise _ReadFailure("unsupported")
    total = _counters(totals)
    # Deliberately validated but never summed: total_tokens omits the cache
    # counters and reasoning_output_tokens is already inside output_tokens.
    _counter(totals, "reasoning_output_tokens", required=False)
    _counter(totals, "total_tokens", required=False)
    raw_last = info.get("last_token_usage") if isinstance(info, dict) else None
    last = _counters(raw_last) if isinstance(raw_last, dict) else None
    return total, _record_day(record), last


def _counters(totals: dict[str, Any]) -> tuple[int, ...]:
    """Raw inclusive Codex cumulative reading, in ``TOKEN_FIELDS`` order.

    Do NOT normalize here. These tuples are the host's own running
    counter and the transition identity ``(previous, total)``. Inclusive
    → disjoint conversion happens on the INCREMENT in ``_aggregate``,
    after differencing, so a warm cache of pre-35A inclusive cumulatives
    still differences correctly against a new reading.
    """
    return (
        _counter(totals, "input_tokens", required=True),
        _counter(totals, "cache_write_input_tokens", required=False),
        _counter(totals, "cached_input_tokens", required=True),
        _counter(totals, "output_tokens", required=True),
    )


def _record_day(record: dict[str, Any]) -> str:
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        raise _ReadFailure("unsupported")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _ReadFailure("unsupported") from exc
    if parsed.tzinfo is None:
        raise _ReadFailure("unsupported")
    return parsed.astimezone(timezone.utc).date().isoformat()


def _context_turn(record: dict[str, Any]) -> str:
    """Return ``turn_context.turn_id``, or ``""`` when the host omits it.

    This is the Codex analogue of Claude's ``message.id`` (``tail_msg_ids``)
    and Grok's ``_grok_terminal_key``: the identity that makes the same work
    recognisable in two different files. Present on all 2,101 ``turn_context``
    records of a real corpus. An empty id degrades to per-file accounting for
    that turn rather than refusing, because a missing OPTIONAL identity is not
    evidence of a malformed ledger.
    """
    payload = record.get("payload")
    turn = payload.get("turn_id") if isinstance(payload, dict) else None
    if not isinstance(turn, str) or not turn:
        return ""
    if len(turn.encode("utf-8")) > _MAX_PROMPT_ID_BYTES:
        raise _ReadFailure("unsupported")
    return turn


def _context_model(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str) or not model or len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
        raise _ReadFailure("unsupported")
    return model


def _is_token_count(record: dict[str, Any]) -> bool:
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "token_count"


def _carries_usage(record: dict[str, Any]) -> bool:
    """True when a ``token_count`` event actually has a usage ledger attached.

    A null or absent ``payload.info`` is Codex's "nothing to report yet"
    marker. An ``info`` that is PRESENT but not a dict is malformed and is
    refused HERE rather than downstream: the caller's model-attribution
    buffering runs before counter validation, so a broken ledger
    arriving before the first ``turn_context`` used to slip past the refusal
    entirely. Do not widen this to "any info I can't parse is fine": the
    distinction between an empty marker and a broken ledger is the whole
    reason this reader can be trusted not to under-report.
    """
    payload = record.get("payload")
    info = payload.get("info") if isinstance(payload, dict) else None
    if info is None:
        return False
    if not isinstance(info, dict):
        raise _ReadFailure("unsupported")
    return True


def _counter(totals: dict[str, Any], key: str, *, required: bool) -> int:
    value = totals.get(key, 0)
    if key not in totals and required:
        raise _ReadFailure("unsupported")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _MAX_COUNTER:
        raise _ReadFailure("unsupported")
    return value


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
    # Normalize EACH increment before grouping; malformed must fail in the
    # same order as the ungrouped reducer. First-seen (day, model) order also
    # preserves insertion order in both output views, including zero buckets.
    accumulated: dict[tuple[str, str], list[int]] = {}
    for (root, previous, total), (day, model, increment, position) in seen.items():
        if previous is None:
            others = reached.get((root, total))
            if others and (others - {position}):
                # Another file already paid for this opening transition.
                continue
        values = _normalize_inclusive_counters(increment)
        _uncached, cache_create, _cache_read, _output = values
        if cache_create > 0:
            buckets.unattributable_days.add(day)
        key = (day, model)
        slot = accumulated.get(key)
        if slot is None:
            accumulated[key] = list(values)
        else:
            for index, value in enumerate(values):
                slot[index] += value
    for (day, model), values in accumulated.items():
        _add_usage(buckets, day, model, dict(zip(TOKEN_FIELDS, values)))
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


def _result_from_buckets(
    buckets: HostUsageBuckets,
    *,
    complete: bool = True,
    reason: Reason | None = None,
    partial_days: frozenset[str] = frozenset(),
) -> HostUsageResult:
    return HostUsageResult(
        buckets.by_family,
        complete=complete,
        reason=reason,
        tokens_by_day=buckets.by_day,
        partial_days=partial_days,
    )


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


def _identity_fields(source: os.stat_result, fingerprint: _Fingerprint) -> dict[str, Any]:
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
    }


def _identity_fields_from(value: dict[str, Any]) -> dict[str, Any]:
    """Project an already-validated cache dict down to identity fields only."""
    return {key: value[key] for key in _IDENTITY_KEYS}


def _no_ledger_entry(source: os.stat_result, fingerprint: _Fingerprint) -> _CacheEntry:
    """Cache "this file was parsed in full and held no usage".

    Deliberately carries no ``day`` / ``model`` / ``usage``: ``_aggregate``
    skips it, so it can never invent a family bucket.
    """
    entry: _CacheEntry = {**_identity_fields(source, fingerprint), "no_ledger": True}  # type: ignore[typeddict-item]
    return entry


def _cache_entry(source: os.stat_result, fingerprint: _Fingerprint, walk: _Walk) -> _CacheEntry:
    # Interned string tables. A 1,234-state rollout repeats one 36-byte turn id
    # and one model id on every row; spelling them out cost 59 bytes a row and
    # took the on-disk cache to 23.4 MB, whose json round-trip alone is 95 ms of
    # a 250 ms autopush budget. `_aggregate` never reads `last` except on a
    # file's FIRST state, so it is stored there and nowhere else.
    turn_ids: list[str] = []
    days: list[str] = []
    models: list[str] = []

    intern_index: dict[tuple[int, str], int] = {}

    def _intern(table: list[str], value: str) -> int:
        # Dict-backed, not `list.index`: a linear scan per row is quadratic in
        # a file's distinct turn ids, and nothing bounds that count.
        key = (id(table), value)
        found = intern_index.get(key)
        if found is None:
            table.append(value)
            found = intern_index[key] = len(table) - 1
        return found

    rows: list[list[Any]] = []
    for index, state in enumerate(walk.states):
        rows.append(
            [
                _intern(turn_ids, state.turn),
                list(state.total),
                _intern(days, state.day),
                _intern(models, state.model),
                (list(state.last) if (index == 0 and state.last) else None),
            ]
        )
    entry: _CacheEntry = {
        **_identity_fields(source, fingerprint),  # type: ignore[typeddict-item]
        "turn_ids": turn_ids,
        "days": days,
        "models": models,
        "states": rows,
        "last_turn": walk.turn,
    }
    if walk.last_total is not None:
        entry["last_total"] = list(walk.last_total)
    if walk.model is not None:
        entry["last_model"] = walk.model
    if walk.pending:
        entry["pending"] = [
            [list(total), day, (list(last) if last else None)] for total, day, last in walk.pending
        ]
    return entry


def _cached_files(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("version") != CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _empty_cache() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "last_reason": None, "last_reason_since": None, "files": {}}


def _validated_entry(value: Any) -> _CacheEntry | None:
    """Validate a cached rollout, preserving the v0.14.15 JSON contract."""
    if not isinstance(value, dict):
        return None
    for key in ("dev", "ino", "size", "mtime_ns", "head_len", "tail_len", "offset"):
        v = value.get(key)
        if type(v) is not int or v < 0:
            return None
    if value["offset"] != value["size"]:
        return None
    if not isinstance(value.get("head"), str) or not isinstance(value.get("tail"), str):
        return None
    if value.get("no_ledger") is True:
        return {**_identity_fields_from(value), "no_ledger": True}
    raw_states = value.get("states")
    if not isinstance(raw_states, list) or not raw_states:
        return None
    turn_ids = _validated_table(value.get("turn_ids"), _MAX_PROMPT_ID_BYTES, allow_empty=True)
    days = _validated_table(value.get("days"), 32)
    models = _validated_table(value.get("models"), _MAX_MODEL_ID_BYTES)
    if turn_ids is None or days is None or models is None:
        return None
    if any(not _validated_day(day) for day in days):
        return None
    nt, nd, nm = len(turn_ids), len(days), len(models)
    states = []
    append = states.append
    for raw in raw_states:
        if not isinstance(raw, list) or len(raw) != 5:
            return None
        turn, total, day, model, last = raw
        if type(turn) is not int or not 0 <= turn < nt:
            return None
        if type(day) is not int or not 0 <= day < nd:
            return None
        if type(model) is not int or not 0 <= model < nm:
            return None
        if not _counter_list_ok(total):
            return None
        if last is not None and not _counter_list_ok(last):
            return None
        append([turn, list(total), day, model, None if last is None else list(last)])
    pending = []
    for raw in value.get("pending") or ():
        parsed_pending = _validated_pending(raw)
        if parsed_pending is None:
            return None
        pending.append(parsed_pending)
    out = {
        **_identity_fields_from(value),
        "turn_ids": turn_ids,
        "days": days,
        "models": models,
        "states": states,
        "last_turn": _validated_turn_id(value.get("last_turn")),
    }
    if pending:
        out["pending"] = pending
    last_total = _validated_counter_list(value.get("last_total"))
    if last_total is not None:
        out["last_total"] = last_total
    model = value.get("last_model")
    if isinstance(model, str) and model and len(model.encode("utf-8")) <= _MAX_MODEL_ID_BYTES:
        out["last_model"] = model
    return out


def _validated_turn_id(value: Any) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_PROMPT_ID_BYTES:
        return ""
    return value


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


def _counter_list_ok(raw: Any) -> bool:
    # JSON-decoded counters and fresh parses contain only plain ints. bool is
    # deliberately rejected, even though it subclasses int.
    return (
        isinstance(raw, list)
        and len(raw) == len(TOKEN_FIELDS)
        and all(type(value) is int and 0 <= value <= _MAX_COUNTER for value in raw)
    )


def _validated_counter_list(raw: Any) -> list[int] | None:
    if not _counter_list_ok(raw):
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
    try:
        with path.open("rb") as fp:
            return _Fingerprint(
                _digest_open_range(fp, 0, head_len, deadline),
                head_len,
                _digest_open_range(fp, source.st_size - tail_len, tail_len, deadline),
                tail_len,
            )
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _digest_range(path: Path, offset: int, length: int, deadline: float) -> str:
    """Hash a bounded source range, preserving the reader's deadline."""
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        with path.open("rb") as fp:
            return _digest_open_range(fp, offset, length, deadline)
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _digest_open_range(fp: BinaryIO, offset: int, length: int, deadline: float) -> str:
    if _expired(deadline):
        raise _ReadFailure("deadline")
    fp.seek(offset)
    data = fp.read(length)
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


def _cache_key(path: Path, *, root: Path | None = None, canonical_root: Path | None = None) -> str:
    """Hash resolve(root) / relative for walker-verified non-symlink descendants.

    Resolve the root once per scan. The fallback supports standalone callers;
    raw paths never enter the cache. Descendant symlinks stay excluded by both
    walkers, and a symlinked Codex sessions root preserves the canonical key.
    """
    if root is not None and canonical_root is not None:
        return hashlib.sha256(os.fsencode(canonical_root / path.relative_to(root))).hexdigest()
    with suppress(OSError):
        return hashlib.sha256(os.fsencode(path.resolve())).hexdigest()
    return hashlib.sha256(os.fsencode(path.absolute())).hexdigest()


def _expired(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _incomplete(reason: Reason) -> HostUsageResult:
    return HostUsageResult({}, complete=False, reason=reason)


__all__ = [
    "CURSOR_STORE_PATH",
    "CURSOR_CACHE_PATH",
    "CURSOR_HOST_CACHE_RETENTION_DAYS",
    "CURSOR_USAGE_CENSUS_HOST_VERSION",
    "CURSOR_USAGE_CENSUS_CONDUCTOR_VERSION",
    "HOST_READER_DIAGS",
    "cursor_usage_diag",
    "reader_usage_diag",
    "reader_cache_cold",
    "read_cursor_usage",
    "CACHE_PATH",
    "CACHE_VERSION",
    "CODEX_SESSIONS_PATH",
    "DEFAULT_READ_BUDGET_S",
    "GROK_CACHE_PATH",
    "GROK_SESSIONS_PATH",
    "GROK_USAGE_CENSUS_HOST_VERSION",
    "grok_sessions_root",
    "HostFamily",
    "HostTokens",
    "HostUsageResult",
    # `Reason` is a cross-module contract since Track 19A: events_tail derives
    # its entire user-visible reason vocabulary from `get_args(Reason)`.
    "Reason",
    "host_family",
    "codex_usage_diag",
    "grok_completed_once",
    "grok_usage_diag",
    "read_codex_usage",
    "read_grok_usage",
    "warm_host_cache_inline",
]
