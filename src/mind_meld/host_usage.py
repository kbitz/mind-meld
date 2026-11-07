"""Private, local-only host-usage readers.

Track 17C supports Codex rollout logs at
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``, plus the flat
``~/.codex/archived_sessions/rollout-*.jsonl`` that Codex moves a rollout to
when a thread is archived. Track 18D adds a consented
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
bucket (Claude; historically OpenCode) would clamp real billable
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
"""

from __future__ import annotations

import errno
import gc
import hashlib
import json
import os
import pwd
import re
import stat
import sys
import time
from contextlib import closing, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Literal, get_args

from mind_meld.errors import StorageError
from mind_meld.lockedjson import (
    InvalidJsonCache,
    LockContended,
    locked_json_rmw,
    locked_json_snapshot,
)
from mind_meld.token_usage import (
    TOKEN_FIELDS,
    DayBucket,
    Usage,
    iter_bounded_lines,
    merge_usage_bucket,
    zero_day_bucket,
    zero_model_bucket,
)

_DIAGNOSTIC = ContextVar("host_usage_diagnostic", default=None)


@contextmanager
def diagnostic_scope(sink):
    """Invocation-local rendering only; direct callers retain stderr notices."""
    token = _DIAGNOSTIC.set(sink)
    try:
        yield
    finally:
        _DIAGNOSTIC.reset(token)


def _diagnostic(message: str, degraded: bool = True) -> None:
    # degraded=False when the caller's returned result already records the loss.
    sink = _DIAGNOSTIC.get()
    try:
        if sink is None:
            sys.stderr.write(message)
        else:
            sink(message, degraded)
    except (Exception, SystemExit):
        pass


MUSE_SESSIONS_PATH = Path.home() / ".local" / "share" / "muse" / "sessions"
MUSE_CACHE_PATH = Path.home() / ".config" / "mind-meld" / "muse-host-tokens.json"
_SQLITE_DETAIL_CODE = re.compile(r"[A-Z_]{1,40}")
# SQLite primary result codes, stable across releases.
_SQLITE_BUSY, _SQLITE_LOCKED, _SQLITE_INTERRUPT = 5, 6, 9
_SQLITE_CORRUPT, _SQLITE_CANTOPEN, _SQLITE_TOOBIG = 11, 14, 18
_SQLITE_AUTH, _SQLITE_NOTADB = 23, 26
CACHE_VERSION = 1
DEFAULT_READ_BUDGET_S = 5.0

_HEAD_PROBE_BYTES = 4096
_TAIL_PROBE_BYTES = 4096
_MAX_MODEL_ID_BYTES = 256
_MAX_PROMPT_ID_BYTES = 256
_MAX_REASON_SINCE_CHARS = 40
_MAX_COUNTER = 2**53
_MUSE_REQUIRED_USAGE_KEYS = frozenset(
    {"input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"}
)
_MUSE_ALLOWED_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "reasoning_tokens",
    }
)
_MUSE_EVENT_REQUIRED_KEYS = frozenset({"kind", "usage"})
_MUSE_RECORDED_AT_MAX_DIGITS = 20
MUSE_USAGE_CENSUS_HOST_VERSION = "1.4.14"
"""Host version of the current Muse usage-reader record contract.

Bound to ``tests/fixtures/host_sessions/muse/CONTRACT.md`` by
``test_muse_contract_census_pin_matches_src_constant``."""
_CANONICAL_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_YEAR_PART = re.compile(r"^\d{4}$")
_MONTH_OR_DAY_PART = re.compile(r"^\d{2}$")

HostFamily = Literal["claude", "codex", "grok", "muse", "agy", "other"]
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
    pricing it. This labels, not suppresses, counters.
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
    three-term-formula tripwire).
    Day-scoped on purpose: a lifetime boolean would let one two-year-old
    incomplete turn mark every future snapshot partial forever, while the
    90-day cap had already dropped that day.
    """

    @property
    def empty(self) -> bool:
        return not self.hosts


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
    """A read that cannot complete. ``cause`` and the related fields are set only
    by the Antigravity SQLite adapter; every other reader persists no detail."""

    def __init__(
        self,
        reason: Reason,
        *,
        cause: str | None = None,
        store: str | None = None,
        file: str | None = None,
        code: str | None = None,
    ) -> None:
        self.reason = reason
        self.cause = cause
        self.store = store
        self.file = file
        self.code = code

    @property
    def detail(self) -> dict[str, str | None] | None:
        if self.cause is None:
            return None
        detail: dict[str, str | None] = {"cause": self.cause, "store": self.store}
        if self.file is not None:
            detail["file"] = self.file
        if self.code is not None and _SQLITE_DETAIL_CODE.fullmatch(self.code):
            detail["code"] = self.code
        return detail


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
    if (
        normalized.startswith(("gpt-", "codex-"))
        or normalized in {"o1", "o3"}
        or normalized.startswith("o4-")
    ):
        return "codex"
    if normalized.startswith("grok-"):
        return "grok"
    if normalized.startswith("muse-"):
        return "muse"
    if normalized.startswith("gemini-"):
        return "agy"
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


def _sqlite_error_name(exc: BaseException) -> str | None:
    name = getattr(exc, "sqlite_errorname", None)
    return name if isinstance(name, str) and _SQLITE_DETAIL_CODE.fullmatch(name) else None


def muse_usage_diag(*, count_logs: bool = False) -> dict[str, Any]:
    """Muse cache inventory and session-log path count. Never opens host logs.

    Cache-only reads plus a path count, so ``mm diag`` runs without a
    passphrase or a valid config.
    The path count walks the sessions tree, so it is off unless the caller
    holds consent (``count_logs=True``); otherwise ``files_on_disk`` is None
    and an unconsented Muse tree is never walked.
    """
    on_disk = _count_muse_ledgers(muse_sessions_root()) if count_logs else None
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
        with locked_json_snapshot(MUSE_CACHE_PATH, blocking=False) as snap:
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
    if data.get("muse_format") != _MUSE_CACHE_FORMAT:
        files = {}  # a pre-release layout; the next read rebuilds it
    raw_skip = data.get("usage_less_skipped", 0)
    skipped = raw_skip if _is_nonnegative_int(raw_skip) else 0
    frozen = _validated_muse_frozen(data.get("frozen"))
    # Frozen days keep per-model totals, not turns; list their models too.
    frozen_models = {
        f"\0frozen:{rel}": {
            "turns": [{"model": model} for models in record["days"].values() for model in models]
        }
        for rel, record in frozen.items()
    }
    models = _diag_turn_model_ids({**files, **frozen_models})
    return {
        "complete_once": data.get("complete_once") is True,
        "usage_less_skipped": skipped,
        **_cached_read_timing(data),
        "last_reason": _cached_last_reason(data),
        "last_reason_since": _cached_reason_since(data),
        "cache_state": "ok",
        "model_count": models["model_count"],
        "models": models["models"],
        "files_cached": len(files) + sum(record["logs"] for record in frozen.values()),
        "files_on_disk": on_disk,
    }


_DIAG_MODEL_CAP = 32
"""How many interned model ids the ``mm diag`` PAYLOAD carries. Counts stay
exact, so a truncated list never reads as the whole set. The plain-text render
applies a second, smaller bound of its own (``cli._DIAG_MODELS_SHOWN``)."""


def _diag_turn_model_ids(files: dict[str, Any]) -> dict[str, Any]:
    """Distinct model ids on cache turns. Cache-only."""
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


def _empty_turn_cache() -> dict[str, Any]:
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
        _diagnostic(f"mm: notice: host token cache write failed: {name}\n")


def _turn_resumable_entry(
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


def _turn_file_entry(
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


def _validated_turn_entry(value: Any) -> dict[str, Any] | None:
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
    partial_days = _validated_turn_partial_days(value.get("partial_days"))
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


def _validated_turn_partial_days(value: Any) -> list[str] | None:
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


def _turn_partial_days(entries: Any) -> frozenset[str]:
    days: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for day in entry.get("partial_days") or ():
            if isinstance(day, str):
                days.add(day)
    return frozenset(days)


def _aggregate_turns(entries: Any) -> HostUsageBuckets:
    """Turns reduction using `_aggregate`'s ``turns`` branch."""
    return _aggregate(entry for entry in entries if isinstance(entry, dict))


# --- Muse reader -----------------------------------------------------------
# Muse session logs are append-only event streams at
# ``sessions/YYYY/MM/DD/<session-id>/session.jsonl``. The only usage
# carrier this reader accepts is the terminal ``model_completed`` event
# (``payload.event`` under a ``runtime.session`` envelope): per-step
# counters plus the model that ran the step. ``goal_usage_attribution``
# records restate the same steps split by purpose and are never read —
# counting both would double every total (measured: per-file sums of the
# two carriers are equal whenever attribution has caught up). The cache
# entry shape is the shared turns shape, so the entry validation, fingerprint,
# resume and aggregate helpers above are shared; the lock/commit
# orchestration in ``read_muse_usage`` mirrors the standard turn pattern. The walk,
# the record projection, cross-log dedup and counter validation are Muse's
# own. The fixture contract is ``tests/fixtures/host_sessions/muse/CONTRACT.md``.


def muse_sessions_root() -> Path:
    """Resolve the Muse sessions directory at call time."""
    return MUSE_SESSIONS_PATH


_MUSE_CACHE_FORMAT = 2
"""Root marker of the Muse cache layout (envelope-only turn keys, ``frozen``).

A cache without it (a pre-release build keyed turns by session and model) is
discarded and rebuilt rather than mixing key schemes."""
_MUSE_SETTLE_DAYS = 2
"""A day directory may freeze once its date is this many UTC days old.

Sessions run past midnight: at the 2026-10-14 census 37 of 215 logs had
been appended the day after their directory date. That census spans two
days, so it cannot show later in-place resumes; the idle rule below and the
frozen-day check exist for them."""
_MUSE_SETTLE_IDLE_NS = 24 * 60 * 60 * 1_000_000_000
"""...and only when every log in it has been idle this long."""
_MUSE_TAIL_RESERVE_S = 0.01
"""Fixed part of the budget kept for dedup, aggregation and freezing."""
_MUSE_TAIL_STEP_COST_S = 4e-6
"""Per live step on top of that (measured ~2.7 µs): with ~1,000 steps a day
and three to four days live, the tail alone is 8-13 ms."""
_MUSE_VERIFY_LOG_COST_S = 50e-6
"""Estimated stat-check cost per frozen log (measured ~40 µs on APFS).

Frozen days are checked, least recently checked first, while their summed
estimate fits the budget left after ``_MUSE_TAIL_RESERVE_S``; a day that
does not fit is skipped, not waited on, and a day whose check runs out of
time moves to the back of the queue. With enough budget every frozen day is
re-checked (~400 ms for 90 days of ~100 logs); an attended push checks what
its share of the sweep budget allows (500 ms when measured, 3000 ms since
Track 101A), an autopush what fits in its grace."""
_MUSE_STEP_LEAD_DAYS = 1
"""How many UTC days a step may precede its session directory's date.

A directory date may be local while ``recorded_at`` is UTC, so a session
opened just after local midnight east of UTC records the previous UTC day.
Anything earlier is history copied into a new session."""
_MUSE_KEY_PREFIX = 16
"""Hex characters of a step key a frozen day keeps for copy detection (64 bits;
a collision only costs an unfreeze and re-read, repeated each pass while it
lasts, never a wrong total)."""
_MUSE_MAX_FROZEN_LOGS = 1_000_000


def _muse_dir_date(rel: str) -> date | None:
    """Calendar date of a ``YYYY/MM/DD`` session directory, or None."""
    parts = rel.split("/")
    if len(parts) != 3 or not all(part.isascii() and part.isdigit() for part in parts):
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _muse_now() -> datetime:
    """The reader's clock for window, settle and idle decisions; tests pin it."""
    return datetime.now(timezone.utc)


SETTLE_DAYS = _MUSE_SETTLE_DAYS
"""Shared settled-day age: a ``YYYY/MM/DD`` directory may freeze once its
date is this many UTC days old (Track 85A; same census basis as Muse)."""
SETTLE_IDLE_NS = _MUSE_SETTLE_IDLE_NS
"""...and only when every file in it has been idle this long. The same idle
window lets a per-file cache hit skip its content fingerprint."""


def _day_dir_date(rel: str) -> date | None:
    """Calendar date of a ``YYYY/MM/DD`` directory key, or None. Pure."""
    return _muse_dir_date(rel)


def _settle_date_eligible(dir_date: date | None, *, today: date) -> bool:
    """True when ``dir_date`` is at least ``SETTLE_DAYS`` before ``today``."""
    return dir_date is not None and (today - dir_date).days >= SETTLE_DAYS


def _stat_digest(
    dir_mtime_ns: int | None, stats: list[tuple[int, int]]
) -> tuple[int | None, int, int, int]:
    """Directory stat digest ``(dir_mtime_ns, count, sum_size, sum_mtime_ns)``.

    Pure: callers supply ``(size, mtime_ns)`` pairs from either live stats or
    cached entry identity fields; both must produce the same digest for an
    unchanged directory.
    """
    return (
        dir_mtime_ns,
        len(stats),
        sum(size for size, _mtime in stats),
        sum(mtime for _size, mtime in stats),
    )


def _count_muse_ledgers(root: Path) -> int | None:
    """Count reader-visible session logs under ``root``.

    Mirrors ``_count_two_level_ledgers``: a missing root is 0, a symlink
    or non-directory root is unknown, and scan errors stay unknown rather
    than rendering as empty. The walk is the reader's own, so the count can
    never disagree with the reader about visibility.
    """
    try:
        if not root.exists():
            return 0
        if root.is_symlink() or not root.is_dir():
            return None
    except OSError:
        return None
    try:
        deadline = time.monotonic() + DEFAULT_READ_BUDGET_S
        return sum(
            len(_muse_day_logs(day, deadline)) for _, day in _iter_muse_day_dirs(root, deadline)
        )
    except (_ReadFailure, OSError):
        return None


@_pause_gc()
def read_muse_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
    consented: bool = False,
) -> HostUsageResult:
    """Read completed Muse model-step totals from ``session.jsonl`` logs.

    Closed by default: ``consented=False`` does not stat or open the
    store. The sweep binds ``consented=True`` only when the ``muse`` sync
    source is enabled (``HOST_READER_SOURCE_GATE``).
    """
    started = time.monotonic()
    if not consented:
        return _incomplete("no_metadata_ledger")
    source_root = root if root is not None else muse_sessions_root()
    read_deadline = deadline if deadline is not None else started + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")

    try:
        with locked_json_rmw(
            MUSE_CACHE_PATH,
            mode=0o600,
            compact=True,
            default_factory=_empty_turn_cache,
            retry_intervals=(),
            on_contention="raise" if _DIAGNOSTIC.get() is not None else "warn",
            contention_warning="host token adapter cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            prior = (_cached_last_reason(locked.data), _cached_reason_since(locked.data))
            current_format = locked.data.get("muse_format") == _MUSE_CACHE_FORMAT
            cached_files = _cached_files(locked.data) if current_format else {}
            prior_frozen = _cached_muse_frozen(locked.data) if current_format else {}
            prior_complete = (
                locked.data.get("version") == CACHE_VERSION
                and locked.data.get("complete_once") is True
            )
            scan_now = _muse_now()
            if _expired(read_deadline):
                result, staged_files, frozen, learned, saw_files = (
                    _incomplete("deadline"),
                    {},
                    prior_frozen,
                    False,
                    False,
                )
            else:
                result, staged_files, frozen, learned, saw_files = _scan_muse_root(
                    source_root, cached_files, prior_frozen, read_deadline, scan_now
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
                raise _NoCacheCommit(result)
            complete_once = prior_complete or (result.complete and saw_files)
            # Freezing, verification and eviction commit only with a complete
            # pass; a partial pass keeps the frozen days it started from.
            files = staged_files if result.complete else {**cached_files, **staged_files}
            locked.data = {
                **timing,
                "version": CACHE_VERSION,
                "muse_format": _MUSE_CACHE_FORMAT,
                "complete_once": complete_once,
                "usage_less_skipped": 0,
                "last_reason": carried[0],
                "last_reason_since": carried[1],
                "files": files,
                "frozen": frozen if result.complete else prior_frozen,
            }
            if over_budget:
                result = _incomplete("deadline")
        _notice_cache_write_failure(locked.write_error)
        return result
    except LockContended:
        _diagnostic(
            "mm: notice: host token adapter cache was locked; skipping host usage scan\n", False
        )
        return _incomplete("locked")
    except _NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return _incomplete("io_error")


def _scan_muse_root(
    root: Path,
    cached_files: dict[str, Any],
    frozen: dict[str, dict[str, Any]],
    deadline: float,
    now: datetime,
) -> tuple[HostUsageResult, dict[str, Any], dict[str, dict[str, Any]], bool, bool]:
    """Returns ``(result, live, frozen, learned, saw_files)``.

    Cost tracks the live window, not the whole history. Logs in day
    directories that have not frozen are read through Grok's entry, resume
    and aggregate helpers, with a stat-only cache hit (an append-only log
    whose dev/ino/size/mtime are unchanged is not reopened). Frozen days are
    then stat-checked, least recently checked first, while their estimated
    cost fits the budget; a mismatch unfreezes that day and re-reads it.
    Every day directory is listed, but a frozen one is only stat-checked, so a
    session resumed months later is still found. A frozen day keeps its step
    keys while a live session could accept a copy of its steps.
    """
    today = now.date()
    try:
        day_dirs = dict(_iter_muse_day_dirs(root, deadline))
    except _ReadFailure as failure:
        return _incomplete(failure.reason), {}, frozen, False, False

    # A frozen day whose directory is gone is evicted. Every other one stays
    # (a few hundred bytes a day): the wire trims to its own active-day window,
    # and dropping a record would turn its directory back into a live re-read.
    kept = {rel: record for rel, record in frozen.items() if rel in day_dirs}
    check_seq = max((record["check_seq"] for record in kept.values()), default=0) + 1
    canonical_root = root.resolve()
    staged: dict[str, Any] = {}
    live_dirs: dict[str, list[str]] = {}
    log_paths: dict[str, str] = {}
    learned = len(kept) != len(frozen)
    saw_files = any(record["logs"] for record in kept.values())

    def read_live(rel: str, logs: list[tuple[str, os.stat_result]]) -> None:
        nonlocal learned
        when = _muse_dir_date(rel) or today  # day_dirs keys are validated dates
        for log_rel, _ in logs:
            if _expired(deadline):
                raise _ReadFailure("deadline")
            path = day_dirs[rel] / log_rel
            key = _cache_key(path, root=root, canonical_root=canonical_root)
            before = _regular_stat(path)
            existing = _validated_turn_entry(cached_files.get(key))
            if existing is not None and _muse_identity(existing) == _muse_identity(before):
                entry = existing
            else:
                resume = _turn_resumable_entry(path, before, existing, deadline)
                entry = (
                    _resume_muse_file(path, before, resume, deadline, when)
                    if resume is not None
                    else _read_full_muse_file(path, before, deadline, when)
                )
                learned = True
            staged[key] = entry
            live_dirs.setdefault(rel, []).append(key)
            log_paths[key] = log_rel

    try:
        for rel, day in day_dirs.items():
            if rel in kept:
                continue
            logs = _muse_day_logs(day, deadline)
            saw_files = saw_files or bool(logs)
            read_live(rel, logs)
        # Least recently checked first (a just-frozen day is 0, so it is checked
        # on the next pass); newest first among equals, where resumes are likelier.
        stalest = sorted(
            (rel for rel in kept if rel in day_dirs),
            key=lambda rel: (kept[rel]["check_seq"], -(_muse_dir_date(rel) or today).toordinal()),
        )
        live_steps = sum(len(entry["turns"]) for entry in staged.values())
        verify_deadline = deadline - _MUSE_TAIL_RESERVE_S - live_steps * _MUSE_TAIL_STEP_COST_S
        budget = verify_deadline - time.monotonic()
        for rel in stalest:
            cost = kept[rel]["logs"] * _MUSE_VERIFY_LOG_COST_S
            if cost > budget:
                continue
            budget -= cost
            try:
                logs = _muse_day_logs(day_dirs[rel], verify_deadline)
            except _ReadFailure as failure:
                if failure.reason != "deadline":
                    raise
                # Out of checking budget: unchecked days stay frozen on trust,
                # and this one goes to the back so it cannot hog every pass.
                kept[rel] = {**kept[rel], "check_seq": check_seq}
                break
            identities = {log_rel: _muse_identity(st) for log_rel, st in logs}
            if _muse_logs_digest(identities) == kept[rel]["digest"]:
                kept[rel] = {**kept[rel], "check_seq": check_seq}
                continue
            # A frozen day changed (a resumed session, a pruned or moved log):
            # drop its totals and read it as live again.
            del kept[rel]
            learned = True
            read_live(rel, logs)
        # A live step whose key a frozen day still holds is a copy of that day's
        # history: unfreeze it so the dedup below sees both copies (once if
        # equal, refused if not).
        held = {prefix: rel for rel, record in kept.items() for prefix in record["keys"]}
        copied = {
            held[turn["key"][:_MUSE_KEY_PREFIX]]
            for keys in live_dirs.values()
            for key in keys
            for turn in staged[key]["turns"]
            if turn["key"][:_MUSE_KEY_PREFIX] in held
        }
        for rel in sorted(copied):
            del kept[rel]
            learned = True
            read_live(rel, _muse_day_logs(day_dirs[rel], deadline))
    except _ReadFailure as failure:
        return _incomplete(failure.reason), staged, frozen, learned, True

    # Turn keys are corpus-wide envelope ids, so a step restated in a second
    # live log (a parent echoing a subagent, a resumed session copying
    # history) counts once when identical and refuses when it disagrees.
    # Frozen days hold keys only while a live session could still accept a
    # copy (above); a copy of anything older breaks the step-lead rule in
    # ``_muse_turn_from_record`` and refuses.
    unique: dict[str, dict[str, Any]] = {}
    owners: dict[str, set[str]] = {}
    for rel, keys in live_dirs.items():
        for key in keys:
            for turn in staged[key]["turns"]:
                prior_turn = unique.setdefault(turn["key"], turn)
                if prior_turn is not turn and prior_turn != turn:
                    return _incomplete("unsupported"), staged, frozen, learned, True
                owners.setdefault(turn["key"], set()).add(rel)

    # A session dated D accepts steps from D - _MUSE_STEP_LEAD_DAYS on. Copies
    # land in sessions still being written: live days with a recent log, or a
    # session created later (dated today - 1 at the earliest, west of UTC).
    # An idle live day is no more open to copies than a frozen one, so it
    # does not hold keys open. A frozen day whose newest step is older than
    # every such session accepts drops its keys (and a day freezing below
    # stores none).
    idle_before = int(now.timestamp() * 1_000_000_000) - _MUSE_SETTLE_IDLE_NS
    active_dates = [
        _muse_dir_date(rel) or today
        for rel, keys in live_dirs.items()
        if any(staged[key]["mtime_ns"] > idle_before for key in keys)
    ]
    oldest_open = min([today - timedelta(days=1), *active_dates])
    copyable_from = (oldest_open - timedelta(days=_MUSE_STEP_LEAD_DAYS)).isoformat()
    for rel, record in list(kept.items()):
        if record["keys"] and max(record["days"], default="") < copyable_from:
            kept[rel] = {**record, "keys": []}
            learned = True

    frozen_turns = [
        {"day": day, "model": model, "usage": usage}
        for record in kept.values()
        for day, models in record["days"].items()
        for model, usage in models.items()
    ]
    partial = set(_turn_partial_days(staged.values()))
    for record in kept.values():
        partial.update(record["partial_days"])
    result = _result_from_buckets(
        _aggregate_turns([{"turns": [*unique.values(), *frozen_turns]}]),
        partial_days=frozenset(partial),
    )

    freezing: dict[str, None] = {}
    for rel, keys in live_dirs.items():
        when = _muse_dir_date(rel)
        if when is None or when > today - timedelta(days=_MUSE_SETTLE_DAYS):
            continue
        entries = [staged[key] for key in keys]
        if any(entry["mtime_ns"] > idle_before for entry in entries):
            continue
        # A step shared with a log in another live day stays live, so the
        # cross-log check above keeps seeing both copies.
        if any(owners[turn["key"]] != {rel} for entry in entries for turn in entry["turns"]):
            continue
        freezing[rel] = None
    for rel in freezing:
        keys = live_dirs[rel]
        entries = [staged[key] for key in keys]
        record = _freeze_muse_day(entries, {log_paths[key]: staged[key] for key in keys}, 0)
        if max(record["days"], default="") < copyable_from:
            record["keys"] = []
        kept[rel] = record
        for key in keys:
            del staged[key]
        learned = True
    return result, staged, kept, learned, saw_files


def _muse_identity(source: os.stat_result | dict[str, Any]) -> tuple[int, int, int, int]:
    if isinstance(source, dict):
        return (source["dev"], source["ino"], source["size"], source["mtime_ns"])
    return (source.st_dev, source.st_ino, source.st_size, source.st_mtime_ns)


def _muse_logs_digest(identities: dict[str, tuple[int, int, int, int]]) -> str:
    """One digest over a day directory's log paths (relative) and stat identities."""
    digest = hashlib.sha256()
    for log_rel in sorted(identities):
        line = f"{log_rel}\0{':'.join(map(str, identities[log_rel]))}\n"
        digest.update(os.fsencode(line))
    return digest.hexdigest()


def _freeze_muse_day(
    entries: list[dict[str, Any]],
    by_log: dict[str, dict[str, Any]],
    check_seq: int,
) -> dict[str, Any]:
    """Collapse one settled day directory to per-day/model totals."""
    days: dict[str, dict[str, Usage]] = {}
    seen: set[str] = set()
    partial: set[str] = set()
    for entry in entries:
        partial.update(entry.get("partial_days") or ())
        for turn in entry["turns"]:
            if turn["key"] in seen:
                continue
            seen.add(turn["key"])
            row = days.setdefault(turn["day"], {})
            prior_usage = row.get(turn["model"])
            row[turn["model"]] = (
                dict(turn["usage"])
                if prior_usage is None
                else {name: prior_usage[name] + turn["usage"][name] for name in TOKEN_FIELDS}
            )
    return {
        "digest": _muse_logs_digest({log: _muse_identity(e) for log, e in by_log.items()}),
        "logs": len(by_log),
        "check_seq": check_seq,
        "days": days,
        "partial_days": sorted(partial),
        "keys": sorted({key[:_MUSE_KEY_PREFIX] for key in seen}),
    }


def _cached_muse_frozen(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if data.get("version") != CACHE_VERSION:
        return {}
    return _validated_muse_frozen(data.get("frozen"))


def _validated_muse_frozen(value: Any) -> dict[str, dict[str, Any]]:
    """Keep each well-formed frozen day; a malformed one is re-read as live."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for rel, record in value.items():
        if not isinstance(rel, str) or _muse_dir_date(rel) is None or not isinstance(record, dict):
            continue
        digest = record.get("digest")
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        logs = record.get("logs")
        if not _is_nonnegative_int(logs) or logs > _MUSE_MAX_FROZEN_LOGS:
            continue
        if not _is_nonnegative_int(record.get("check_seq")):
            continue
        days = _validated_muse_frozen_days(record.get("days"))
        partial = _validated_turn_partial_days(record.get("partial_days"))
        keys = record.get("keys")
        if days is None or partial is None or not _valid_muse_key_prefixes(keys):
            continue
        out[rel] = {
            "digest": digest,
            "logs": logs,
            "check_seq": record["check_seq"],
            "days": days,
            "partial_days": partial,
            "keys": keys,
        }
    return out


def _valid_muse_key_prefixes(value: Any) -> bool:
    if not isinstance(value, list) or len(value) > _MUSE_MAX_FROZEN_LOGS:
        return False
    return len(set(value)) == len(value) and all(
        isinstance(item, str) and len(item) == _MUSE_KEY_PREFIX and _HEX_PREFIX.fullmatch(item)
        for item in value
    )


_HEX_PREFIX = re.compile(r"[0-9a-f]+")


def _validated_muse_frozen_days(value: Any) -> dict[str, dict[str, Usage]] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, dict[str, Usage]] = {}
    for day, models in value.items():
        if not isinstance(day, str) or not _CANONICAL_DAY.fullmatch(day):
            return None
        if not isinstance(models, dict):
            return None
        row: dict[str, Usage] = {}
        for model, usage in models.items():
            if not isinstance(model, str) or not model:
                return None
            if len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
                return None
            if not isinstance(usage, dict):
                return None
            if any(not _is_valid_counter(usage.get(name)) for name in TOKEN_FIELDS):
                return None
            row[model] = {name: usage[name] for name in TOKEN_FIELDS}
        out[day] = row
    return out


def _iter_muse_day_dirs(root: Path, deadline: float):
    """Yield ``("YYYY/MM/DD", path)`` for each valid day directory under ``root``."""
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise _ReadFailure("unsupported")
        for year in _sorted_children(root, deadline):
            if not _YEAR_PART.fullmatch(year.name) or not _is_directory(year):
                continue
            for month in _sorted_children(year, deadline):
                if not _MONTH_OR_DAY_PART.fullmatch(month.name) or not _is_directory(month):
                    continue
                for day in _sorted_children(month, deadline):
                    if not _MONTH_OR_DAY_PART.fullmatch(day.name) or not _is_directory(day):
                        continue
                    rel = f"{year.name}/{month.name}/{day.name}"
                    if _muse_dir_date(rel) is not None:
                        yield rel, day
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _muse_day_logs(day: Path, deadline: float) -> list[tuple[str, os.stat_result]]:
    """Every ``session.jsonl`` of one day directory, relative to it, with its lstat.

    Subagent sessions nest below a session at
    ``<session>/subagent/<child-id>/session.jsonl`` (142 of 179 files in
    the census corpus) and their model steps are real spend, so the walk
    descends through literal ``subagent`` segments and nothing else.
    Symlinks, and anything that is not a directory or regular file, are
    skipped. ``scandir`` file types and plain strings keep this near one
    directory read and one lstat per log, which is what lets frozen days be
    re-checked inside an attended budget.
    """
    base = os.fspath(day)
    found: list[tuple[str, os.stat_result]] = []

    def entries(rel: str) -> list[os.DirEntry[str]]:
        if _expired(deadline):
            raise _ReadFailure("deadline")
        try:
            with os.scandir(os.path.join(base, rel) if rel else base) as listing:
                return sorted(listing, key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            return []  # removed by Muse mid-walk: absent, like the old walker

    def session_tree(rel: str) -> None:
        children = {entry.name: entry for entry in entries(rel)}
        log = children.get("session.jsonl")
        if log is not None and log.is_file(follow_symlinks=False):
            try:
                st = log.stat(follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                st = None
            if st is not None:
                found.append((f"{rel}/session.jsonl", st))
        sub = children.get("subagent")
        if sub is not None and sub.is_dir(follow_symlinks=False):
            for child in entries(f"{rel}/subagent"):
                if child.is_dir(follow_symlinks=False):
                    session_tree(f"{rel}/subagent/{child.name}")

    try:
        for session in entries(""):
            if session.is_dir(follow_symlinks=False):
                session_tree(session.name)
    except OSError as exc:
        raise _ReadFailure("io_error") from exc
    return found


def _read_full_muse_file(
    path: Path,
    before: os.stat_result,
    deadline: float,
    dir_date: date,
) -> dict[str, Any]:
    turns, new_partial = _read_muse_file(path, 0, {}, before, deadline, dir_date)
    return _turn_file_entry(path, before, deadline, turns, 0, new_partial=new_partial)


def _resume_muse_file(
    path: Path,
    before: os.stat_result,
    entry: dict[str, Any],
    deadline: float,
    dir_date: date,
) -> dict[str, Any]:
    prior = {turn["key"]: turn for turn in entry["turns"]}
    turns, new_partial = _read_muse_file(path, entry["offset"], prior, before, deadline, dir_date)
    return _turn_file_entry(
        path,
        before,
        deadline,
        turns,
        0,
        prior_partial=entry.get("partial_days") or (),
        new_partial=new_partial,
    )


def _read_muse_file(
    path: Path,
    start_offset: int,
    prior: dict[str, dict[str, Any]],
    before: os.stat_result,
    deadline: float,
    dir_date: date,
) -> tuple[dict[str, dict[str, Any]], frozenset[str]]:
    turns = dict(prior)
    last_offset = start_offset
    partial_days: set[str] = set()
    try:
        with path.open("rb") as fp:
            fp.seek(start_offset)
            for raw, end_offset in iter_bounded_lines(
                fp,
                _cache_key(path),
                start_offset,
                label="muse usage walker",
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
                parsed = _muse_turn_from_record(record, dir_date)
                if parsed is None:
                    continue
                key, turn, partial = parsed
                if partial:
                    partial_days.add(turn["day"])
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
    return turns, frozenset(partial_days)


def _muse_turn_from_record(record: Any, dir_date: date) -> tuple[str, dict[str, Any], bool] | None:
    """Project one terminal ``model_completed`` event, or skip the record.

    Additive event metadata is ignored, but a recognized carrier with an
    unexpected usage key set or counter shape refuses the whole reader
    (``unsupported``) instead of guessing. Everything that is not the
    carrier — other event kinds, retained frames, attribution records —
    is skipped without being interpreted.
    """
    if not isinstance(record, dict):
        raise _ReadFailure("malformed")
    if record.get("record_type") != "event" or record.get("payload_type") != "runtime.session":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    if event.get("kind") != "model_completed":
        return None
    if not _MUSE_EVENT_REQUIRED_KEYS <= set(event):
        raise _ReadFailure("unsupported")
    envelope_id = record.get("id")
    if not isinstance(envelope_id, str) or not envelope_id:
        raise _ReadFailure("unsupported")
    if len(envelope_id.encode("utf-8")) > _MAX_PROMPT_ID_BYTES:
        raise _ReadFailure("unsupported")
    usage = event.get("usage")
    if not isinstance(usage, dict):
        raise _ReadFailure("unsupported")
    counters, partial = _validate_muse_counters(usage)
    day = _muse_recorded_day(record.get("recorded_at"))
    # A step recorded before its session began is history copied into a new
    # session (census: every step falls on its directory's date or the next
    # UTC day). Counting it would double spend already counted, possibly in
    # a frozen day whose keys are gone, so refuse rather than guess.
    if date.fromisoformat(day) < dir_date - timedelta(days=_MUSE_STEP_LEAD_DAYS):
        raise _ReadFailure("unsupported")
    model = event.get("model")
    if not isinstance(model, str) or not model:
        # Muse emits usage-less completion steps without a model. Validate
        # the envelope and counters first so zeroes cannot conceal drift.
        if not any(counters.values()):
            return None
        raise _ReadFailure("unsupported")
    if len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
        raise _ReadFailure("unsupported")
    # One envelope is one step, so the model stays OUT of the key: a repeated
    # id with a different model must reach the ``existing == turn`` check and
    # refuse, not count twice. Not session-scoped either; see _scan_muse_root.
    step_id = hashlib.sha256(f"muse\0{envelope_id}".encode()).hexdigest()
    return step_id, {"key": step_id, "day": day, "model": model, "usage": counters}, partial


def _validate_muse_counters(usage: dict[str, Any]) -> tuple[Usage, bool]:
    """Disjoint-normalize Muse's inclusive counters; flag unattributable days.

    ``input_tokens`` already contains ``cache_read_tokens`` (and, under
    the same three-term rule as Codex/Grok, ``cache_write_tokens``).
    ``cached_tokens`` duplicates ``cache_read_tokens`` in every censused
    record, so a disagreement is format drift, not a second bucket. Muse
    1.4.14 may omit cache reads/writes; default them to cached/zero.
    ``reasoning_tokens`` is a subset of ``output_tokens`` and is never
    added twice. A nonzero cache write marks the day partial, matching
    the shared ``unattributable_days`` rule.
    """
    if not _MUSE_REQUIRED_USAGE_KEYS <= set(usage) <= _MUSE_ALLOWED_USAGE_KEYS:
        raise _ReadFailure("unsupported")
    for value in usage.values():
        if not _is_valid_counter(value):
            raise _ReadFailure("unsupported")
    cache_read = usage.get("cache_read_tokens", usage["cached_tokens"])
    cache_write = usage.get("cache_write_tokens", 0)
    if usage["cached_tokens"] != cache_read:
        raise _ReadFailure("unsupported")
    if usage["reasoning_tokens"] > usage["output_tokens"]:
        raise _ReadFailure("unsupported")
    normalized = _normalize_inclusive_usage(
        {
            "input": usage["input_tokens"],
            "cache_create": cache_write,
            "cache_read": cache_read,
            "output": usage["output_tokens"],
        }
    )
    return normalized, cache_write > 0


_MUSE_RECORDED_AT_MIN_US = 1_577_836_800_000_000
"""2020-01-01 UTC in microseconds; below this the stamp is not Muse's clock."""


def _muse_recorded_day(value: Any) -> str:
    """UTC day of an envelope ``recorded_at`` (microsecond epoch, as text).

    Pre-2020 or more than a day in the future is ``malformed``, so a
    wrong-scale clock cannot be reaped as ancient history or squat in the
    shared 90-day window.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _ReadFailure("unsupported")
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit():
            raise _ReadFailure("unsupported")
        # int() refuses strings past 4,300 digits with a raw ValueError.
        if len(value) > _MUSE_RECORDED_AT_MAX_DIGITS:
            raise _ReadFailure("malformed")
        micros = int(value)
    else:
        micros = value
    if micros < _MUSE_RECORDED_AT_MIN_US:
        raise _ReadFailure("malformed")
    try:
        when = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise _ReadFailure("malformed") from exc
    if when > datetime.now(timezone.utc) + timedelta(days=1):
        raise _ReadFailure("malformed")
    return when.date().isoformat()


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


# --- Antigravity reader -----------------------------------------------------
# Google Antigravity (agy) session logs are SQLite databases stored under
# ``~/.gemini/antigravity-cli/conversations/<cid>.db``. Model metadata is
# stored in protobuf format in ``gen_metadata``; per-step metadata, timestamps,
# and token ledgers are stored in protobuf format in ``steps``.
# The only step type carrying billable token usage is step_type 15. Any completed
# step_type 15 row (status 3) lacking token usage increments usage_less_skipped.
# Any step_type != 15 row carrying nonzero token usage refuses the reader as
# unsupported.
# The cache entry shape is the shared turns shape keyed by <cid>.db, tracking
# both main-DB and WAL fingerprints. Pre-open and post-close fingerprints must
# match, and any hot journal refuses as stale. Deduplication across DBs is keyed
# on sha256("agy\0" + bot_id).

AGY_CONVERSATIONS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
AGY_CACHE_PATH = Path.home() / ".config" / "mind-meld" / "agy-host-tokens.json"
AGY_USAGE_CENSUS_HOST_VERSION = "1.4.5"
_AGY_CACHE_FORMAT = 1
_AGY_SQLITE_LENGTH_LIMIT = 16 * 1024 * 1024  # 16 MiB
_AGY_SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")

AGY_READER_CAUSES: dict[str, Reason] = {
    "sqlite_unavailable": "io_error",
    "cannot_open": "io_error",
    "sqlite_auth": "io_error",
    "sqlite_ioerr": "io_error",
    "sqlite_pragma": "io_error",
    "non_regular_path": "io_error",
    "hot_journal": "stale",
    "identity_changed": "stale",
    "corrupt_database": "malformed",
    "oversize_or_type": "malformed",
    "corrupt_protobuf": "malformed",
    "bad_counters": "malformed",
    "counter_invariant_violation": "malformed",
    "missing_timestamp": "malformed",
    "missing_bot_id": "malformed",
    "bot_id_conflict": "malformed",
    "missing_steps_table": "unsupported",
    "missing_steps_columns": "unsupported",
    "missing_gen_metadata_table": "unsupported",
    "unknown_model_enum": "unsupported",
    "token_usage_on_unexpected_step_type": "unsupported",
}


def agy_sessions_root(home: Path | None = None) -> Path:
    """Resolve the Antigravity conversations directory."""
    if home is not None:
        return home / ".gemini" / "antigravity-cli" / "conversations"
    return AGY_CONVERSATIONS_PATH


def _is_real_agy_store(path: Path) -> bool:
    """Compare against real home to prevent tests touching real Antigravity DBs."""
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        home = Path.home()
    real = home / ".gemini" / "antigravity-cli" / "conversations"
    candidate = str(path.expanduser().resolve()).casefold()
    root = str(real.resolve()).casefold()
    return candidate == root or candidate.startswith(root + os.sep)


def _decode_protobuf_varint(data: bytes | memoryview, offset: int) -> tuple[int, int]:
    val = 0
    shift = 0
    length = len(data)
    while offset < length:
        b = data[offset]
        offset += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, offset
        shift += 7
        if shift > 64:
            raise _ReadFailure("malformed", cause="corrupt_protobuf")
    raise _ReadFailure("malformed", cause="corrupt_protobuf")


def _iter_protobuf_fields(data: bytes | memoryview) -> Iterator[tuple[int, int, Any]]:
    # Unknown payload fields are skipped without copying conversation content.
    data = memoryview(data)
    offset = 0
    length = len(data)
    while offset < length:
        tag, offset = _decode_protobuf_varint(data, offset)
        fnum = tag >> 3
        wtype = tag & 0x7
        if fnum == 0:
            raise _ReadFailure("malformed", cause="corrupt_protobuf")
        if wtype == 0:  # varint
            val, offset = _decode_protobuf_varint(data, offset)
            yield (fnum, wtype, val)
        elif wtype == 1:  # 64-bit
            if offset + 8 > length:
                raise _ReadFailure("malformed", cause="corrupt_protobuf")
            val = data[offset : offset + 8]
            offset += 8
            yield (fnum, wtype, val)
        elif wtype == 2:  # length-delimited
            flen, offset = _decode_protobuf_varint(data, offset)
            if flen < 0 or offset + flen > length:
                raise _ReadFailure("malformed", cause="corrupt_protobuf")
            val = data[offset : offset + flen]
            offset += flen
            yield (fnum, wtype, val)
        elif wtype == 5:  # 32-bit
            if offset + 4 > length:
                raise _ReadFailure("malformed", cause="corrupt_protobuf")
            val = data[offset : offset + 4]
            offset += 4
            yield (fnum, wtype, val)
        else:
            raise _ReadFailure("malformed", cause="corrupt_protobuf")


def _lstat_agy_sqlite_files(db_path: Path) -> dict[str, os.stat_result]:
    present: dict[str, os.stat_result] = {}
    base_name = db_path.name
    parent = db_path.parent
    for suffix in _AGY_SQLITE_SUFFIXES:
        name = base_name + suffix
        path = parent / name
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            code = errno.errorcode.get(exc.errno or 0)
            raise _ReadFailure("io_error", cause="cannot_open", code=code) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _ReadFailure("io_error", cause="non_regular_path")
        present[name] = info
    return present


def _agy_fingerprint(
    present: dict[str, os.stat_result], db_name: str
) -> tuple[tuple[int, int, int, int], tuple[bool, int, int, int]]:
    db_stat = present.get(db_name)
    if db_stat is None:
        raise FileNotFoundError(f"{db_name} disappeared")
    main_fp = (db_stat.st_dev, db_stat.st_ino, db_stat.st_size, db_stat.st_mtime_ns)
    wal_name = db_name + "-wal"
    if wal_name in present:
        wal_stat = present[wal_name]
        wal_fp = (True, wal_stat.st_ino, wal_stat.st_size, wal_stat.st_mtime_ns)
    else:
        wal_fp = (False, 0, 0, 0)
    return (main_fp, wal_fp)


def _agy_entry_matches_stat(
    entry: dict[str, Any],
    fp: tuple[tuple[int, int, int, int], tuple[bool, int, int, int]],
) -> bool:
    main_fp, wal_fp = fp
    return (
        entry.get("dev") == main_fp[0]
        and entry.get("ino") == main_fp[1]
        and entry.get("size") == main_fp[2]
        and entry.get("mtime_ns") == main_fp[3]
        and entry.get("wal_present") == wal_fp[0]
        and entry.get("wal_ino") == wal_fp[1]
        and entry.get("wal_size") == wal_fp[2]
        and entry.get("wal_mtime_ns") == wal_fp[3]
    )


def _validated_agy_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    for k in ("dev", "ino", "size", "mtime_ns"):
        if not _is_nonnegative_int(value.get(k)):
            return None
    wal_present = value.get("wal_present")
    if not isinstance(wal_present, bool):
        return None
    for k in ("wal_ino", "wal_size", "wal_mtime_ns"):
        if not _is_nonnegative_int(value.get(k)):
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
    if "partial_days" not in value:
        return None
    partial_days = _validated_turn_partial_days(value.get("partial_days"))
    if partial_days is None:
        return None
    if value.get("counter_semantics") != "disjoint-v1":
        return None
    return {
        "dev": value["dev"],
        "ino": value["ino"],
        "size": value["size"],
        "mtime_ns": value["mtime_ns"],
        "wal_present": wal_present,
        "wal_ino": value["wal_ino"],
        "wal_size": value["wal_size"],
        "wal_mtime_ns": value["wal_mtime_ns"],
        "turns": normalized,
        "usage_less_skipped": skip,
        "partial_days": partial_days,
        "counter_semantics": "disjoint-v1",
    }


def _count_agy_dbs(root: Path) -> int | None:
    try:
        if not root.exists():
            return 0
        if root.is_symlink() or not root.is_dir():
            return None
        count = 0
        for entry in root.iterdir():
            if entry.name.endswith(".db"):
                try:
                    info = entry.lstat()
                    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        count += 1
                except OSError:
                    continue
        return count
    except OSError:
        return None


def _agy_reason_detail(data: dict[str, Any]) -> dict[str, str | None] | None:
    raw = data.get("last_error_detail")
    if not isinstance(raw, dict):
        return None
    cause = raw.get("cause")
    if not isinstance(cause, str) or cause not in AGY_READER_CAUSES:
        return None
    code = raw.get("code")
    detail: dict[str, str | None] = {"cause": cause}
    if isinstance(code, str) and _SQLITE_DETAIL_CODE.fullmatch(code):
        detail["code"] = code
    return detail


def _read_single_agy_db(
    db_path: Path,
    before_fp: tuple[tuple[int, int, int, int], tuple[bool, int, int, int]],
    deadline: float,
) -> dict[str, Any]:
    if os.environ.get("PYTEST_CURRENT_TEST") and _is_real_agy_store(db_path):
        raise RuntimeError(f"refusing to open the real Antigravity store {db_path} from a test")

    immutable = not before_fp[1][0]  # not wal_present
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ReadFailure("deadline")
    try:
        import sqlite3
    except ImportError as exc:
        raise _ReadFailure("io_error", cause="sqlite_unavailable") from exc

    uri = db_path.absolute().as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro")
    failure: BaseException | None = None
    turns: list[dict[str, Any]] = []
    seen_turn_keys: set[str] = set()
    usage_less_skipped = 0
    partial_days: set[str] = set()

    try:
        with closing(
            sqlite3.connect(uri, uri=True, isolation_level=None, timeout=min(remaining, 5.0))
        ) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _AGY_SQLITE_LENGTH_LIMIT)
            for pragma in (
                "trusted_schema=OFF",
                "query_only=ON",
                "cell_size_check=ON",
                "mmap_size=0",
            ):
                conn.execute(f"PRAGMA {pragma}").fetchall()
            if conn.execute("PRAGMA trusted_schema").fetchone() != (0,) or conn.execute(
                "PRAGMA query_only"
            ).fetchone() != (1,):
                raise _ReadFailure("io_error", cause="sqlite_pragma")
            conn.set_progress_handler(lambda: 1 if _expired(deadline) else 0, 1000)

            tables = {
                (
                    row[0].decode("utf-8", "replace") if isinstance(row[0], bytes) else row[0]
                ).casefold()
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "steps" not in tables:
                raise _ReadFailure("unsupported", cause="missing_steps_table")
            if "gen_metadata" not in tables:
                raise _ReadFailure("unsupported", cause="missing_gen_metadata_table")

            models: dict[int, str] = {}
            for (data,) in conn.execute("SELECT data FROM gen_metadata ORDER BY rowid"):
                if _expired(deadline):
                    raise _ReadFailure("deadline")
                if not data or not isinstance(data, (bytes, memoryview)):
                    continue
                data_bytes = bytes(data)
                for fnum, wtype, fval in _iter_protobuf_fields(data_bytes):
                    if fnum == 1 and wtype == 2:
                        enum_id = None
                        slug = None
                        for sf_num, sf_wtype, sf_val in _iter_protobuf_fields(fval):
                            if sf_num == 3 and sf_wtype == 0:
                                enum_id = sf_val
                            elif sf_num == 19 and sf_wtype == 2:
                                if len(sf_val) > _MAX_MODEL_ID_BYTES:
                                    raise _ReadFailure("malformed", cause="oversize_or_type")
                                try:
                                    slug = bytes(sf_val).decode("utf-8")
                                except UnicodeDecodeError as exc:
                                    raise _ReadFailure(
                                        "malformed", cause="corrupt_protobuf"
                                    ) from exc
                        if enum_id is not None and slug:
                            models[enum_id] = slug

            cols = {
                (
                    col[1].decode("utf-8", "replace") if isinstance(col[1], bytes) else col[1]
                ).casefold()
                for col in conn.execute("PRAGMA table_info(steps)").fetchall()
            }
            idx_col = "idx" if "idx" in cols else "step_index" if "step_index" in cols else None
            if idx_col is None or not {"step_type", "status", "metadata"} <= cols:
                raise _ReadFailure("unsupported", cause="missing_steps_columns")

            cursor = conn.execute(
                f"SELECT {idx_col}, step_type, status, metadata FROM steps ORDER BY {idx_col}"
            )
            for _idx, step_type, status, meta in cursor:
                if _expired(deadline):
                    raise _ReadFailure("deadline")
                if not meta or not isinstance(meta, (bytes, memoryview)):
                    if step_type == 15 and status == 3:
                        usage_less_skipped += 1
                    continue
                meta_bytes = bytes(meta)
                ts = None
                token_usage_bytes = None
                for fnum, wtype, fval in _iter_protobuf_fields(meta_bytes):
                    if fnum == 1 and wtype == 2:
                        for sf_num, sf_wtype, sf_val in _iter_protobuf_fields(fval):
                            if sf_num == 1 and sf_wtype == 0:
                                ts = sf_val
                                break
                    elif fnum == 9 and wtype == 2:
                        token_usage_bytes = fval

                if token_usage_bytes is None:
                    if step_type == 15 and status == 3:
                        usage_less_skipped += 1
                    continue

                model_enum = None
                input_tokens = 0
                output_tokens = 0
                cache_create = 0
                cache_read = 0
                bot_id = None
                thinking = None
                response = None
                for uf_num, _uf_wtype, uf_val in _iter_protobuf_fields(token_usage_bytes):
                    if uf_num in {2, 3, 4, 5, 9, 10} and _uf_wtype != 0:
                        raise _ReadFailure("malformed", cause="bad_counters")
                    if uf_num == 1 and _uf_wtype == 0:
                        model_enum = uf_val
                    elif uf_num == 2 and _uf_wtype == 0:
                        input_tokens = uf_val
                    elif uf_num == 3 and _uf_wtype == 0:
                        output_tokens = uf_val
                    elif uf_num == 4 and _uf_wtype == 0:
                        cache_create = uf_val
                    elif uf_num == 5 and _uf_wtype == 0:
                        cache_read = uf_val
                    elif uf_num == 7 and _uf_wtype == 2:
                        try:
                            bot_id = bytes(uf_val).decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise _ReadFailure("malformed", cause="corrupt_protobuf") from exc
                    elif uf_num == 9 and _uf_wtype == 0:
                        thinking = uf_val
                    elif uf_num == 10 and _uf_wtype == 0:
                        response = uf_val

                if bot_id is not None and bot_id.startswith("<synthetic>"):
                    continue
                for counter_val in (input_tokens, output_tokens, cache_create, cache_read):
                    if not _is_valid_counter(counter_val):
                        raise _ReadFailure("malformed", cause="bad_counters")
                if thinking is not None and not _is_valid_counter(thinking):
                    raise _ReadFailure("malformed", cause="bad_counters")
                if response is not None and not _is_valid_counter(response):
                    raise _ReadFailure("malformed", cause="bad_counters")
                if thinking is not None and response is not None:
                    if thinking + response != output_tokens:
                        raise _ReadFailure("malformed", cause="counter_invariant_violation")

                has_tokens = (
                    input_tokens > 0 or output_tokens > 0 or cache_read > 0 or cache_create > 0
                )
                if step_type != 15:
                    if has_tokens:
                        raise _ReadFailure(
                            "unsupported", cause="token_usage_on_unexpected_step_type"
                        )
                    continue

                if not has_tokens:
                    continue
                if bot_id is None:
                    raise _ReadFailure("malformed", cause="missing_bot_id")
                if model_enum is None or model_enum not in models:
                    raise _ReadFailure("unsupported", cause="unknown_model_enum")

                model_slug = models[model_enum]
                if ts is None or not _is_nonnegative_int(ts):
                    raise _ReadFailure("malformed", cause="missing_timestamp")
                try:
                    day_str = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
                except (ValueError, OSError, OverflowError) as exc:
                    raise _ReadFailure("malformed", cause="missing_timestamp") from exc

                if cache_create > 0:
                    partial_days.add(day_str)

                digest = hashlib.sha256(f"agy\0{bot_id}".encode("utf-8")).hexdigest()
                turn_data = {
                    "key": digest,
                    "day": day_str,
                    "model": model_slug,
                    "usage": {
                        "input": input_tokens,
                        "output": output_tokens,
                        "cache_read": cache_read,
                        "cache_create": cache_create,
                    },
                }
                if digest in seen_turn_keys:
                    existing = next(t for t in turns if t["key"] == digest)
                    if existing != turn_data:
                        raise _ReadFailure("malformed", cause="bot_id_conflict")
                    continue
                seen_turn_keys.add(digest)
                turns.append(turn_data)
    except (sqlite3.Error, _ReadFailure) as exc:
        failure = exc

    present_after = _lstat_agy_sqlite_files(db_path)
    if db_path.name not in present_after:
        raise _ReadFailure("stale", cause="identity_changed")
    after_fp = _agy_fingerprint(present_after, db_path.name)
    if before_fp != after_fp:
        raise _ReadFailure("stale", cause="identity_changed")

    if isinstance(failure, _ReadFailure):
        raise failure

    if failure is not None:
        code = getattr(failure, "sqlite_errorcode", None)
        primary = code & 0xFF if isinstance(code, int) else None
        error_name = _sqlite_error_name(failure)
        if primary in {_SQLITE_BUSY, _SQLITE_LOCKED}:
            raise _ReadFailure("locked") from failure
        if primary == _SQLITE_INTERRUPT:
            raise _ReadFailure("deadline") from failure
        if primary == _SQLITE_TOOBIG:
            raise _ReadFailure("malformed", cause="oversize_or_type") from failure
        if primary in {_SQLITE_CORRUPT, _SQLITE_NOTADB}:
            raise _ReadFailure("malformed", cause="corrupt_database") from failure
        if primary == _SQLITE_AUTH:
            raise _ReadFailure("io_error", cause="sqlite_auth", code=error_name) from failure
        if primary == _SQLITE_CANTOPEN:
            raise _ReadFailure("io_error", cause="cannot_open", code=error_name) from failure
        if primary == 10:  # SQLITE_IOERR
            raise _ReadFailure("io_error", cause="sqlite_ioerr", code=error_name) from failure
        raise _ReadFailure("io_error", cause="cannot_open", code=error_name) from failure

    main_fp, wal_fp = before_fp
    return {
        "dev": main_fp[0],
        "ino": main_fp[1],
        "size": main_fp[2],
        "mtime_ns": main_fp[3],
        "wal_present": wal_fp[0],
        "wal_ino": wal_fp[1],
        "wal_size": wal_fp[2],
        "wal_mtime_ns": wal_fp[3],
        "turns": turns,
        "usage_less_skipped": usage_less_skipped,
        "partial_days": sorted(partial_days),
        "counter_semantics": "disjoint-v1",
    }


def _scan_agy_root(
    root: Path,
    cached_files: dict[str, Any],
    deadline: float,
) -> tuple[HostUsageResult, dict[str, Any], dict[str, str | None] | None, int]:
    if _expired(deadline):
        return (_incomplete("deadline"), {}, None, 0)
    try:
        if root.is_symlink():
            return (_incomplete("io_error"), {}, {"cause": "non_regular_path"}, 1)
        if not root.exists():
            return (HostUsageResult({}, complete=True), {}, None, 0)
        if not root.is_dir():
            return (_incomplete("io_error"), {}, {"cause": "non_regular_path"}, 1)
        db_paths = sorted(
            [p for p in root.iterdir() if p.name.endswith(".db")], key=lambda p: p.name
        )
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0)
        return (_incomplete("io_error"), {}, {"cause": "cannot_open", "code": code}, 1)

    staged_files: dict[str, Any] = {}
    try:
        for db_path in db_paths:
            if _expired(deadline):
                raise _ReadFailure("deadline")
            present = _lstat_agy_sqlite_files(db_path)
            if db_path.name not in present:
                continue
            journal_name = db_path.name + "-journal"
            if journal_name in present and present[journal_name].st_size > 0:
                raise _ReadFailure("stale", cause="hot_journal")
            fp = _agy_fingerprint(present, db_path.name)
            cached = _validated_agy_entry(cached_files.get(db_path.name))
            if cached is not None and _agy_entry_matches_stat(cached, fp):
                staged_files[db_path.name] = cached
            else:
                entry = _read_single_agy_db(db_path, fp, deadline)
                staged_files[db_path.name] = entry

        unique: dict[str, dict[str, Any]] = {}
        for entry in staged_files.values():
            for turn in entry.get("turns", ()):
                tkey = turn["key"]
                prior = unique.setdefault(tkey, turn)
                if prior is not turn and prior != turn:
                    raise _ReadFailure("malformed", cause="bot_id_conflict")

        buckets = HostUsageBuckets()
        for turn in unique.values():
            _add_usage(buckets, turn["day"], turn["model"], turn["usage"])
        all_partial: set[str] = set()
        for entry in staged_files.values():
            all_partial.update(entry.get("partial_days", ()))
        result = _result_from_buckets(buckets, complete=True, partial_days=frozenset(all_partial))
        return (result, staged_files, None, 0)
    except _ReadFailure as failure:
        detail: dict[str, str | None] = (
            {"cause": failure.cause} if failure.cause else {"cause": failure.reason}
        )
        if failure.code:
            detail["code"] = failure.code
        return (_incomplete(failure.reason), staged_files, detail, 1)


@_pause_gc()
def read_agy_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
    consented: bool = False,
) -> HostUsageResult:
    """Read completed Antigravity model-turn totals from conversation SQLite DBs."""
    started = time.monotonic()
    if not consented:
        return _incomplete("no_metadata_ledger")
    source_root = root if root is not None else agy_sessions_root()
    read_deadline = deadline if deadline is not None else started + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")

    try:
        with locked_json_rmw(
            AGY_CACHE_PATH,
            mode=0o600,
            compact=True,
            default_factory=_empty_turn_cache,
            retry_intervals=(),
            on_contention="raise" if _DIAGNOSTIC.get() is not None else "warn",
            contention_warning="host token adapter cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            prior = (_cached_last_reason(locked.data), _cached_reason_since(locked.data))
            prior_detail = _agy_reason_detail(locked.data)
            prior_error_count = locked.data.get("error_db_count", 0)
            current_format = (
                locked.data.get("version") == CACHE_VERSION
                and locked.data.get("agy_format") == _AGY_CACHE_FORMAT
            )
            raw_files = _cached_files(locked.data) if current_format else {}
            cached_files = {
                name: entry
                for name, raw in raw_files.items()
                if (entry := _validated_agy_entry(raw)) is not None
            }
            if _expired(read_deadline):
                result, staged_files, last_error_detail, error_db_count = (
                    _incomplete("deadline"),
                    {},
                    None,
                    0,
                )
            else:
                result, staged_files, last_error_detail, error_db_count = _scan_agy_root(
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
            files = staged_files if result.complete else {**cached_files, **staged_files}
            complete_once = (locked.data.get("complete_once") is True) or result.complete
            total_skipped = sum(entry.get("usage_less_skipped", 0) for entry in files.values())

            if result.complete:
                final_detail = None
                final_error_count = 0
            elif (
                detail := _agy_reason_detail({"last_error_detail": last_error_detail})
            ) is not None:
                final_detail = detail
                final_error_count = error_db_count
            else:
                final_detail = prior_detail
                final_error_count = prior_error_count

            locked.data = {
                "version": CACHE_VERSION,
                "agy_format": _AGY_CACHE_FORMAT,
                "census_host_version": AGY_USAGE_CENSUS_HOST_VERSION,
                "complete_once": complete_once,
                "usage_less_skipped": total_skipped,
                **timing,
                "last_reason": carried[0],
                "last_reason_since": carried[1],
                "last_error_detail": final_detail,
                "error_db_count": final_error_count,
                "files": files,
            }
            if over_budget:
                result = _incomplete("deadline")
        _notice_cache_write_failure(locked.write_error)
        return result
    except LockContended:
        _diagnostic(
            "mm: notice: host token adapter cache was locked; skipping host usage scan\n", False
        )
        return _incomplete("locked")
    except (OSError, StorageError, InvalidJsonCache):
        return _incomplete("io_error")


def agy_usage_diag(*, count_logs: bool = False) -> dict[str, Any]:
    """Antigravity cache inventory and DB path count. Never opens host logs."""
    on_disk = _count_agy_dbs(agy_sessions_root()) if count_logs else None
    blank = {
        **_cached_read_timing({}),
        "complete_once": False,
        "usage_less_skipped": 0,
        "last_reason": None,
        "last_reason_since": None,
        "last_error_detail": None,
        "error_db_count": 0,
        "cache_state": "unreadable",
        "model_count": 0,
        "models": [],
        "files_cached": None,
        "files_on_disk": on_disk,
    }
    try:
        with locked_json_snapshot(AGY_CACHE_PATH, blocking=False) as snap:
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
    if data.get("agy_format") != _AGY_CACHE_FORMAT:
        files = {}
    raw_skip = data.get("usage_less_skipped", 0)
    skipped = raw_skip if _is_nonnegative_int(raw_skip) else 0
    models = _diag_turn_model_ids(files)
    return {
        "complete_once": data.get("complete_once") is True,
        "usage_less_skipped": skipped,
        **_cached_read_timing(data),
        "last_reason": _cached_last_reason(data),
        "last_reason_since": _cached_reason_since(data),
        "last_error_detail": _agy_reason_detail(data),
        "error_db_count": data.get("error_db_count", 0),
        "cache_state": "ok",
        "model_count": models["model_count"],
        "models": models["models"],
        "files_cached": len(files),
        "files_on_disk": on_disk,
    }


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


def _is_regular_non_symlink(path: Path, *, missing_ok: bool = True) -> bool:
    """True for a regular, non-symlink file. Absence is tolerated by default.

    Shared by the Grok walker (speculative ``session / "updates.jsonl"``)
    and the Codex walker (``iterdir()`` then ``lstat()``). ``FileNotFoundError``
    and ``NotADirectoryError`` mean the candidate is gone — skip it.
    Archive discovery and diag pass ``missing_ok=False``: a listed candidate
    that disappeared makes their inventory incomplete rather than silently empty.
    Every other ``OSError`` (a permission error on a file that exists) stays
    fatal so the all-or-nothing reader contract still sees a real failure.
    """
    try:
        st = path.lstat()
        return not stat.S_ISLNK(st.st_mode) and stat.S_ISREG(st.st_mode)
    except (FileNotFoundError, NotADirectoryError) as exc:
        if not missing_ok:
            raise _ReadFailure("io_error") from exc
        return False
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


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


def _cached_files(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("version") != CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


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
    "CACHE_VERSION",
    "DEFAULT_READ_BUDGET_S",
    "HostFamily",
    "HostTokens",
    "HostUsageResult",
    # `Reason` is a cross-module contract since Track 19A: events_tail derives
    # its entire user-visible reason vocabulary from `get_args(Reason)`.
    "Reason",
    "host_family",
    "MUSE_SESSIONS_PATH",
    "MUSE_CACHE_PATH",
    "muse_sessions_root",
    "read_muse_usage",
    "muse_usage_diag",
    "AGY_CONVERSATIONS_PATH",
    "AGY_CACHE_PATH",
    "AGY_USAGE_CENSUS_HOST_VERSION",
    "agy_sessions_root",
    "read_agy_usage",
    "agy_usage_diag",
]
