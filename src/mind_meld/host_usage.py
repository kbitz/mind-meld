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
bucket (Claude today; historically OpenCode) would clamp real billable
tokens to zero. Track 42A merges extractors into this path; the prohibition
is load-bearing for that merge, not a comment about the current two
inclusive survivors. Malformed inclusive counters
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
consecutive scans and zero bytes cached. A COMPLETE pass replaces the map
(that is what prunes deleted rollouts); a PARTIAL pass MERGES, because
replacing would delete entries it never reached and pruning on a listing it
never finished would drop files that were never absent. ``warm_host_cache_inline``
is the attended-command escape hatch for the first cold scan; the bounded
callers still publish only from a bounded read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

from mind_meld.lockedjson import locked_json_rmw, locked_json_snapshot
from mind_meld.token_usage import (
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
CACHE_VERSION = 1
DEFAULT_READ_BUDGET_S = 5.0

_HEAD_PROBE_BYTES = 4096
_TAIL_PROBE_BYTES = 4096
_MAX_MODEL_ID_BYTES = 256
_MAX_PROMPT_ID_BYTES = 256
_MAX_COUNTER = 2**53
_GROK_STOPS = frozenset({"end_turn", "cancelled"})
_GROK_CONTENT_FIELDS = frozenset({"content", "rawInput", "rawOutput"})
_GROK_TERMINAL_KEYS = frozenset({"prompt_id", "sessionUpdate", "stop_reason", "usage"})
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
    three-term-formula tripwire). A disjoint extractor leaves it empty.
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

    **This entry is much larger than the terminal it replaced, and that cost is
    the reason the string columns are interned.** Measured on a 747-rollout /
    694 MB corpus: 72,654 states, median 17 per file and 1,234 at the maximum.
    Spelled out, the cache was 23.4 MB and its json round-trip alone was 95 ms
    of the 250 ms autopush host budget. Interning ``turn_ids`` / ``days`` /
    ``models`` and keeping ``last`` only on a file's FIRST state (the only place
    ``_aggregate`` reads it) brings that to 13.5 MB and 56 ms. Still 34x the
    v0.12.47 cache, and it grows with the corpus — see the scaling note in
    ``docs/TODOS.md`` for the trigger to revisit the encoding.
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
class _Terminal:
    """One already-reduced ``(day, model, usage)`` contribution.

    The unit for a reader whose rows are already per-turn and disjoint:
    nothing to dedup and no cumulative counter to difference. Codex stopped
    using this in favour of ``_TurnState``; do not reintroduce it there.
    ``_terminal_from_record`` is the remaining producer (test-only after
    Track 32A); Track 42A owns its fate.
    """

    day: str
    model: str
    usage: Usage


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
            result, staged_files, learned = _scan_codex_root(
                source_root, cached_files, read_deadline
            )
            if not learned and not result.complete:
                # Nothing NEW was learned about any file — escape without
                # rewriting (and re-permissioning) the cache. Gating on
                # `learned` rather than on `staged_files` matters: cache hits
                # are staged too, so an already-warm machine with one
                # permanently unreadable rollout would otherwise pay a full
                # read/modify/write of the whole cache on every push to
                # persist content identical to what it just read.
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
                "version": CACHE_VERSION,
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
            if not result.complete:
                return result
            if _expired(read_deadline):
                # The scan finished but overran its budget: refuse to publish
                # (unchanged), yet keep the cache above so the work counts.
                return _incomplete("deadline")
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
    """Populate the host cache under a generous one-off budget.

    The push tail's per-capture budget (250ms autopush / 500ms interactive) is
    sized for a WARM read, and a cold scan of a large corpus does not fit —
    573ms measured across 452 rollouts. Partial commits make a cold machine
    converge over a few pushes on their own; this makes it happen once,
    visibly, on an attended command instead. Mirrors
    ``token_usage.warm_token_cache_inline``.

    ``reader`` selects which incremental cache to warm. Only names in
    ``events_tail.WARMABLE_HOST_READERS`` have one.

    The result is returned for tests and callers that want it, but the point is
    the side effect. Callers must still publish from a bounded capture, so this
    never becomes a back door around the explicit-deadline rule.
    """
    deadline = time.monotonic() + budget_s
    if reader == "grok":
        return read_grok_usage(root, deadline=deadline, consented=True)
    return read_codex_usage(root, deadline=deadline)


def grok_completed_once() -> bool:
    """True after a consented scan finished and saw at least one ledger file.

    Missing, corrupt, or lock-contended cache is pre-success (fail safe).
    Diagnostic only: the host-sweep no longer keys publication policy on this
    latch (Track 31A). ``mm status`` / ``mm diag`` still do.
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
        "cache_state": "missing",
        "state": "cold",
        "files_cached": 0,
        "files_migrated": 0,
        "files_pre_track": 0,
        "files_on_disk": None,
        "pending": None,
        "model_count": 0,
        "models": [],
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
    }


def grok_usage_diag() -> dict[str, Any]:
    """On-disk Grok usage-reader state. Does not open the host store.

    ``mm diag`` must run without a passphrase and without a valid config, so
    this reads only the private cache. Absence, lock contention, and
    unreadable files are reported as ``cache_state``, never raised.
    """
    try:
        with locked_json_snapshot(GROK_CACHE_PATH) as snap:
            data = snap.data
            state = snap.state
    except OSError:
        return {
            "complete_once": False,
            "usage_less_skipped": 0,
            "cache_state": "unreadable",
            "model_count": 0,
            "models": [],
        }
    if state != "valid" or not isinstance(data, dict):
        cache_state = "missing" if state in {"missing", "empty"} else "unreadable"
        return {
            "complete_once": False,
            "usage_less_skipped": 0,
            "cache_state": cache_state,
            "model_count": 0,
            "models": [],
        }
    raw_skip = data.get("usage_less_skipped", 0)
    skipped = raw_skip if _is_nonnegative_int(raw_skip) else 0
    files = data.get("files")
    models = _diag_grok_model_ids(files if isinstance(files, dict) else {})
    return {
        "complete_once": data.get("complete_once") is True,
        "usage_less_skipped": skipped,
        "cache_state": "ok",
        "model_count": models["model_count"],
        "models": models["models"],
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
    if not consented:
        return _incomplete("no_metadata_ledger")
    source_root = root if root is not None else grok_sessions_root()
    read_deadline = deadline if deadline is not None else time.monotonic() + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")

    try:
        with locked_json_rmw(
            GROK_CACHE_PATH,
            mode=0o600,
            default_factory=_empty_grok_cache,
            retry_intervals=(),
            on_contention="warn",
            contention_warning="host token adapter cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            if _expired(read_deadline):
                raise _NoCacheCommit(_incomplete("deadline"))

            cached_files = _cached_files(locked.data)
            prior_complete = locked.data.get("complete_once") is True
            result, staged_files, learned, saw_files = _scan_grok_root(
                source_root, cached_files, read_deadline
            )
            if not learned and not result.complete:
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
                "version": CACHE_VERSION,
                "complete_once": complete_once,
                "usage_less_skipped": skip_total,
                "files": files,
            }
            if not result.complete:
                return result
            if _expired(read_deadline):
                return _incomplete("deadline")
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
        "files": {},
    }


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

    staged: dict[str, Any] = {}
    learned = False
    for workspace, session_id, path in ledgers:
        if _expired(deadline):
            return _incomplete("deadline"), staged, learned, True
        key = _cache_key(path)
        try:
            before = _regular_stat(path)
            existing = cached_files.get(key)
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
    existing: Any,
    deadline: float,
) -> dict[str, Any] | None:
    entry = _validated_grok_entry(existing)
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
    existing: Any,
    deadline: float,
) -> dict[str, Any] | None:
    entry = _validated_grok_entry(existing)
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
    # Content-bearing turns short-circuit BEFORE the key-set check. A
    # content field on a terminal is "not this projection", not "unknown
    # wire" — load-bearing: the carve-out below would otherwise never
    # fire for a usage-less record that also carried content.
    if _GROK_CONTENT_FIELDS & update.keys():
        return None
    # Usage-less `turn_completed` is a zero-token skip, not unsupported.
    # MUST precede the exact-match key check: a record without `usage`
    # fails `set(update) != _GROK_TERMINAL_KEYS` first, so a carve-out at
    # the `usage = update.get("usage")` site is dead code.
    if set(update) == _GROK_TERMINAL_KEYS - {"usage"}:
        return [], frozenset()
    if set(update) != _GROK_TERMINAL_KEYS:
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

    Never call this on an already-disjoint bucket. Track 42A merges
    extractors into ``_add_usage``; normalizing there would clamp a
    disjoint reader's ``cache_read > input`` shape to zero. The live
    survivors (Codex, Grok CLI) are inclusive — the prohibition is for
    the merge, not a description of today's readers.
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


def _model_id(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value and len(value.encode("utf-8")) <= _MAX_MODEL_ID_BYTES:
            return value
    raise _ReadFailure("unsupported")


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

    staged: dict[str, _CacheEntry] = {}
    learned = False
    for path in rollouts:
        if _expired(deadline):
            return _incomplete("deadline"), staged, learned
        key = _cache_key(path)
        try:
            before = _regular_stat(path)
            existing = cached_files.get(key)
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
    ``continue`` runs before ``_terminal_from_record``, so a broken ledger
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


def _terminal_from_record(record: dict[str, Any], model: str) -> _Terminal:
    payload = record.get("payload")
    info = payload.get("info") if isinstance(payload, dict) else None
    totals = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(totals, dict):
        raise _ReadFailure("unsupported")
    usage: Usage = _normalize_inclusive_usage(
        {
            "input": _counter(totals, "input_tokens", required=True),
            "cache_create": _counter(totals, "cache_write_input_tokens", required=False),
            "cache_read": _counter(totals, "cached_input_tokens", required=True),
            "output": _counter(totals, "output_tokens", required=True),
        }
    )
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


def _aggregate(entries: Any) -> HostUsageBuckets:
    """Reduce cached entries to family totals AND per-model day buckets.

    Handles every reader's entry shape, which is why there is one of these and
    not one per reader: ``_Terminal`` rows are already-reduced (day, model,
    usage) and already disjoint, Grok contributes pre-deduped ``turns``, and
    Codex contributes ``states`` that must be deduped HERE, across files.
    Do not call ``_normalize_inclusive_usage`` on the ``_Terminal`` branch —
    those buckets are already exclusive. Track 42A merges extractors here.

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
        if not isinstance(entry, _Terminal) and entry.get("no_ledger"):
            continue
        if isinstance(entry, _Terminal):
            _add_usage(buckets, entry.day, entry.model, entry.usage)
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
    "GROK_CACHE_PATH",
    "GROK_SESSIONS_PATH",
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
