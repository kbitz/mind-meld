"""The mm-events push tail: what every push and `mm init` record for the retro.

Extracted from ``cli.py`` in Track 16A.

``_run_events_tail`` runs at the end of ``_push_core`` and writes the
``git-snapshot`` / ``sessions-snapshot`` / optional ``host-usage-snapshot`` /
``mm-push`` rows the retro-fleet aggregator later stitches across the fleet.
``_run_events_backfill`` is its ``mm init`` counterpart. Both are
FORENSIC-ONLY: they must never fail a push, and every degradation they detect
is APPENDED TO THE RETURNED LIST, not merely printed — an unattended
`mm autopush` hook's stderr goes nowhere, which is why `autopush` writes a
`degraded` breadcrumb from these reasons.

Imports nothing from ``cli`` — pinned by ``tests/test_module_boundaries.py``.
The one new dependency edge is ``events_tail -> host_usage``: this module is
the sole event producer and ``host_usage`` is the canonical local-reader and
model-family authority. There is no reverse edge.

Read ``docs/invariants/events-retro.md`` BEFORE editing: the wall-clock budget
is scoped to the walk (the ``walk_done`` snapshot precedes the identity gather
AND the host capture on purpose), `_decide_token_walk_policy` returning False
does NOT by itself mean degradation — it also returns False when no `claude`
source is enabled, which is a config shape, so the append is gated on
`claude_paths` — and the host snapshot is reader-scoped: a failed reader is
dropped and declared, other readers still publish. A row is omitted only when
no consulted reader completed, or the sweep expired before any reader ran.
Each reader stays all-or-nothing internally.
"""

from __future__ import annotations

import inspect
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Sequence, get_args

from mind_meld import (
    __version__,
    events,
    host_registry,
    host_usage,
    identity,
    token_usage,
    upgrade,
)
from mind_meld import config as mm_config
from mind_meld.config import (
    DEFAULT_CAPTURE_BUDGET_MS,
    DEFAULT_HOST_USAGE_AUTOPUSH_BUDGET_MS,
    DEFAULT_HOST_USAGE_INTERACTIVE_BUDGET_MS,
)
from mind_meld.errors import HOST_USAGE_CAPTURE_URL
from mind_meld.hosts import codex as codex_host
from mind_meld.safety import safe_str, safe_terminal_str

CacheLockMode = Literal["warn", "block"] | None
HostReader = Callable[..., host_usage.HostUsageResult]


_DIAGNOSTIC = ContextVar("events_diagnostic", default=None)


@contextmanager
def diagnostic_scope(sink):
    token = _DIAGNOSTIC.set(sink)
    try:
        with token_usage.diagnostic_scope(sink), host_usage.diagnostic_scope(sink):
            yield
    finally:
        _DIAGNOSTIC.reset(token)


def _diagnostic(message: str, degraded: bool = False) -> None:
    sink = _DIAGNOSTIC.get()
    try:
        if sink is None:
            sys.stderr.write(message)
        else:
            sink(message, degraded)
    except (Exception, SystemExit):
        pass


def _exception_detail(error: Exception) -> str:
    # Preserve legacy direct/init diagnostics, but never copy exception messages
    # into a sync invocation's newly scoped diagnostic surface.
    return (
        f"{type(error).__name__}: {safe_str(error)}"
        if _DIAGNOSTIC.get() is None
        else type(error).__name__
    )


class Degradations(list[str]):
    """Bound candidate text while keeping full occurrence and omission counts."""

    def __init__(self):
        super().__init__()
        self.total = 0
        self.omitted = 0

    def append(self, detail):
        if isinstance(detail, str):
            detail = safe_terminal_str(detail)
        self.total = min(2**53, self.total + 1)
        if len(self) < 8 and isinstance(detail, str) and len(detail) <= 384 and detail not in self:
            super().append(detail)
        else:
            self.omitted = min(2**53, self.omitted + 1)

    def extend(self, details):
        for detail in details:
            self.append(detail)
        self.total = min(2**53, self.total + getattr(details, "omitted", 0))
        self.omitted = min(2**53, self.omitted + getattr(details, "omitted", 0))


HOST_USAGE_READ_BUDGET_INTERACTIVE_MS = DEFAULT_HOST_USAGE_INTERACTIVE_BUDGET_MS
HOST_USAGE_READ_BUDGET_AUTOPUSH_MS = DEFAULT_HOST_USAGE_AUTOPUSH_BUDGET_MS
"""Host reads get their OWN absolute deadline, deliberately separate from the
git/session walk budgets. It starts after the ``walk_done`` snapshot so host
time can never trigger or redefine the session-walk notice, and it is passed
explicitly to every reader — no unattended caller may fall through to ``host_usage``'s
5-second default, which is ~20x an autopush's entire walk budget."""


_pending_capture_budget_ms: int | None = None
"""Capture total resolved for the next attended host snapshot (Track 85A).

``cli._capture_attended_usage`` passes only ``host_budget_ms`` into
``_capture_host_snapshot``; the ``resolve_host_read_budget`` call that
produced it stashes the configured capture total here so the snapshot can
partition host+warm inside half of it. Consumed (read and cleared) by the
next ``_capture_host_snapshot`` call; ``None`` keeps legacy warm allowance."""

_attended_host_elapsed_s: float | None = None
"""Elapsed attended host+warm time, handed to the next ``_run_events_tail``.

Set by ``_capture_host_snapshot`` when an attended warm hook is present and
consumed by the attended tail in the same push process, so unused host
budget flows to discovery/git/session capture (Track 85A d5)."""


@contextmanager
def preserve_pending_capture_budget():
    """Keep a display-only resolve from planting/wiping the capture stash."""
    global _pending_capture_budget_ms
    saved = _pending_capture_budget_ms
    try:
        yield
    finally:
        _pending_capture_budget_ms = saved


def resolve_capture_budget(config: dict) -> tuple[int, str]:
    """Total attended capture budget and its source (Track 85A).

    ``[retro] capture_budget_ms`` bounds host capture + inline warm plus the
    events-tail discovery/git/session capture of one attended push
    (``build_manifest_v2`` scan time is outside it). Unset keeps the 5 s
    default from docs/SPEC.md O8.2.
    """
    retro = config.get("retro")
    if isinstance(retro, dict) and "capture_budget_ms" in retro:
        return retro["capture_budget_ms"], "config"
    return DEFAULT_CAPTURE_BUDGET_MS, "default"


def resolve_host_read_budget(config: dict, *, interactive: bool) -> tuple[int, str]:
    """Effective per-Mac budget and its source, from an already validated config.

    Attended: a configured ``capture_budget_ms`` takes precedence over the
    legacy interactive key and the host phase (sweep + inline warm) gets at
    most half of it; the tail keeps the rest plus whatever the host phase
    leaves unused. The return value is the host SWEEP budget: the legacy
    interactive value (or its 500 ms default) capped at that half. With only
    the legacy key configured, legacy sweep/warm behavior applies unchanged;
    with neither key, the 5000 ms capture default partitions the phase.
    Autopush always keeps its own small fixed share.
    """
    global _pending_capture_budget_ms
    retro = config.get("retro")
    if not interactive:
        # An autopush resolve never feeds an attended snapshot; do not let a
        # stale stash survive into an unrelated later capture.
        _pending_capture_budget_ms = None
    if interactive and isinstance(retro, dict) and "capture_budget_ms" in retro:
        capture_ms = retro["capture_budget_ms"]
        _pending_capture_budget_ms = capture_ms
        sweep_ms = retro.get(
            "host_usage_interactive_budget_ms", HOST_USAGE_READ_BUDGET_INTERACTIVE_MS
        )
        return max(1, min(sweep_ms, capture_ms // 2)), "config"
    if interactive:
        _pending_capture_budget_ms = (
            None
            if isinstance(retro, dict) and "host_usage_interactive_budget_ms" in retro
            else DEFAULT_CAPTURE_BUDGET_MS
        )
    setting_name = (
        "host_usage_interactive_budget_ms" if interactive else "host_usage_autopush_budget_ms"
    )
    if isinstance(retro, dict) and setting_name in retro:
        return retro[setting_name], "config"
    return (
        HOST_USAGE_READ_BUDGET_INTERACTIVE_MS
        if interactive
        else HOST_USAGE_READ_BUDGET_AUTOPUSH_MS,
        "default",
    )


HOST_READER_GRACE_MS = 50
"""Minimum time for each reader after the first, beyond the sweep deadline.
Track 57A evidence E16 measured warm Grok reads at 19–20 ms while Codex
used 73–80% of the autopush budget. This floor gives Grok about 2.5x that
read time. Deadlines are cooperative, not filesystem-interrupt ceilings."""

_ROOT_DISCOVERY_DEGRADATION = (
    "git repository discovery hit its time budget: this push captured an incomplete "
    "repository set. Run mm diag, then mm recapture 30d to recover the omitted commits"
)
_ROOT_DISCOVERY_ERROR_DEGRADATION = (
    "git repository discovery failed: probe error(s) recorded. Run mm diag"
)
_ROOT_DISCOVERY_EMPTY_DEGRADATION = (
    "git repository discovery completed and found 0 repositories — "
    "no commits will be captured from this Mac. Run mm diag"
)
_GIT_WALK_DEGRADATION = (
    "git walk dropped {n} repositories this push. "
    "Run mm recapture --dry-run to check current repository failures, "
    "then mm recapture 30d to recover the omitted commits"
)
_CURSOR_HOLD_DEGRADATION = (
    "retro cursor held at last complete capture. "
    "Run mm diag, then mm recapture 30d to recover the omitted commits"
)
_CURSOR_COVERAGE_DEGRADATION = (
    "retro cursor floored: no complete capture found in the retained event log. "
    "Run mm diag, then mm recapture 30d to recover older commits"
)

_HOST_READ_REASONS = frozenset(get_args(host_usage.Reason))
"""Reason classes a reader may report, taken from ``host_usage`` so the two
cannot drift. Anything outside the set is reported as ``_HOST_UNKNOWN_REASON``
— the reason lands in a user-visible notice and breadcrumb, so it stays a
closed vocabulary rather than a pass-through string."""

_HOST_UNKNOWN_REASON = "unavailable"

_HOST_ABSENT_REASONS = frozenset({"no_metadata_ledger"})
"""Reasons that mean "this source is not installed", not "this read failed".

A store that exposes no metadata-only usage ledger (closed-default Grok
consent) can never produce data on this machine. That is a known, permanent
ABSENCE — categorically unlike a failed read — so the sweep drops the
reader from ``token_sources`` and carries on with the rest rather than
vetoing the whole snapshot.

This is deliberately NOT keyed on ``unsupported``. Codex returns
``unsupported`` when it finds a ledger it cannot attribute: that means
"real usage exists here that I could not read". Track 31A isolates that
to the failing reader (dropped and declared); it is still not treated as
absence.

Pinned as a SUBSET of ``_HOST_READ_REASONS`` by
``test_absent_reasons_are_real_reader_reasons``: a rename on the
``host_usage.Reason`` side would otherwise empty this set silently and treat
an honest absence as a failed read."""

_HOST_PERMANENT_REASONS = frozenset(host_usage.PERMANENT_REASONS)
"""Derived from the reader's standing-blocker policy."""

HOST_READER_SOURCE_GATE: dict[str, str | None] = {
    "codex": "codex",
    # Muse is a scoped sync source too (``type: "muse"``, hardcoded
    # ``memory/``). Source-enabled is consent, matching Codex;
    # there is no separate retro bit.
    "muse": "muse",
    "agy": "agy",
    **dict.fromkeys(mm_config.HOST_USAGE_ONLY_CONSENT),
}
"""Which enabled sync source each reader's consent derives from.

A reader that parses a host's local store only runs when the user enabled that
host as a source. The Claude session walk has always been gated this way
(``_enabled_claude_paths``); the host readers shipped without it, which meant a
user who declined the ``codex`` source still had `~/.codex/sessions` parsed and
their totals published to the fleet. Only aggregates ever crossed the boundary,
but "we read it unless you read the README" is the wrong default for a tool
whose whole premise is scoped, opt-in sync."""

WARMABLE_HOST_READERS: frozenset[str] = frozenset({"codex", "cursor", "muse", "agy"})
"""Readers with cached totals an attended warm can populate.

A future reader whose cache stores no totals must not be added here.
Every current live reader is warmable; that is not the contract — the
useful pin is that every warmable name is a live reader."""


@dataclass(frozen=True)
class HostUsageCapture:
    """The outcome of one host-reader sweep.

    ``hosts is None`` means NO consulted reader completed (or the sweep expired
    before any reader was invoked) and the caller must omit the whole snapshot:
    no invented zero. An empty dict (``complete`` True) is a real completed
    empty scan and is healthy — a machine with no host data, or whose enabled
    readers were all absent sources.

    A reader that failed is listed in ``dropped`` as ``(reader, reason)`` and
    is not in ``token_sources``. Other readers still contribute. ``reader`` /
    ``reason`` remain the sweep-level labels for the no-row case (pre-invoke
    deadline, or every reader failed — then they mirror the first dropped
    pair). Never a path, transcript excerpt, SQL fragment, or raw exception.
    """

    hosts: dict[str, dict[str, token_usage.Usage]] | None
    reader: str = ""
    reason: str = ""
    token_sources: tuple[str, ...] = ()
    """The readers that actually CONTRIBUTED to ``hosts`` this sweep.

    Per-push, not a constant: a host the user has not enabled as a source is
    never invoked, and one whose store can never hold a ledger is dropped. Both
    are honest absences, and the row says so rather than implying every built-in
    reader was consulted."""
    dropped: tuple[tuple[str, str], ...] = ()
    """``(reader, reason)`` pairs for readers that failed this sweep.

    Order is invocation order so Codex-first vs Grok-first does not change
    what is observable. Reasons are the closed vocabulary; never a path.
    Absent sources (``no_metadata_ledger``) are not listed here.
    """
    invoked: bool = True
    """False when the sweep expired before calling any reader."""
    tokens_by_day: dict[str, token_usage.DayBucket] | None = None
    """Per-model day buckets, same work as ``hosts``. None iff ``hosts`` is None."""
    partial_days: dict[str, frozenset[str]] = field(default_factory=dict)
    """Reader name → UTC days that reader flagged incomplete this sweep.

    Accumulated beside ``contributed`` in ``_capture_host_usage``, never
    inside ``_merge_host_usage_maps`` (that helper never sees reader
    identity). Day-scoped so ``make_host_usage_snapshot`` can intersect
    with the same ``keep`` set ``hosts`` uses.
    """
    partial: tuple[str, ...] = ()
    """Canonical-order reader names with a nonempty partial-day set.

    Set-union then ``readers``-order rebuild, copying the shipped
    ``token_sources`` idiom. A naive concatenation produces duplicates,
    the acceptor drops the field, and the card silently renders nothing.
    """
    usage_days: dict[str, frozenset[str]] = field(default_factory=dict)
    """Each reader's own day keys, including all-zero buckets, before merging.

    The writer intersects these with its retained days to prove emptiness.
    Model-family membership cannot identify a reader's contribution.
    """

    @property
    def complete(self) -> bool:
        return self.hosts is not None


@dataclass
class CaptureResult:
    """Data produced by the shared event-capture path.

    This contract intentionally excludes token-cache warming, identity work,
    notices, terminal events, and writes. Push and init have different policy
    for each of those concerns; sharing them here would reintroduce the
    coupling this helper is meant to remove. ``host_capture`` is carried as
    data for the same reason: the tail turns an incomplete sweep into a notice
    plus a returned degradation, init into a notice alone.
    """

    git_rows: list[dict]
    session_rows: list[dict]
    host_rows: list[dict]
    host_capture: HostUsageCapture
    root_discovery: events.GitRootDiscovery
    discovery_errors: list[str]
    session_walk_exceeded_budget: bool
    warn_lock_unavailable: bool
    token_cache_requested: bool
    walk_budget_aborts: int = 0
    walk_errors: int = 0


def _enabled_claude_paths(sources: list[dict]) -> list[Path]:
    """Return the base directory of each ``type=claude`` source resolved by
    ``get_sources()``. Used by ``_run_events_tail`` to feed Track 7B's
    ``walk_session_metadata`` once per claude dir; aggregated into a single
    sessions-snapshot row so pull-merge set-union semantics stay stable
    regardless of how many claude sources are configured."""
    return [Path(s["path"]).expanduser() for s in sources if s.get("type") == "claude"]


def _accepted_kwargs(fn: Callable, **kwargs: object) -> dict[str, object]:
    """Subset of ``kwargs`` the (possibly test-patched) ``fn`` accepts.

    Warmer budget kwargs (Track 85A d9) must not break one-arg monkeypatches
    of ``warm_host_cache_inline`` / ``warm_token_cache_inline``.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in params}


def _decide_token_walk_policy(
    claude_paths: list[Path],
    *,
    quiet: bool,
    warm_deadline_s: float | None = None,
) -> bool:
    """Return True if the events tail should aggregate token data this push.

    Side effect: when cold cache + (interactive OR detected upgrade
    transition), runs ``warm_token_cache_inline`` to populate the cache
    BEFORE the tail walk starts. False return means cold cache + no warm
    eligibility (autopush, no transition) — emit a notice and skip.

    Four policies:

    1. **Cache already warm**: return True. Tail walk picks up cache hits
       on every existing jsonl, walks only newly-touched ones.

    2. **Cold + interactive (``quiet=False``)**: telegraph one-time warm
       cost, run ``warm_token_cache_inline``, return True.

    3. **Cold + autopush + transition fired**: warm silently using the
       ``warm_token_cache_inline`` default budget. The transition flag is
       set by the call to ``upgrade.run_transition_hook`` earlier in the
       same process — its presence (not a budget bump) is what unlocks
       this path on autopush.

    4. **Cold + autopush + no transition**: emit ``mm: notice: token cache
       not warm; run 'mm push' to populate`` and return False (skip token
       aggregation this push).
    """
    if not claude_paths:
        return False
    try:
        is_cold = token_usage.is_cache_cold()
    except OSError:
        return False
    if not is_cold:
        return True
    transition = upgrade.last_transition_seen()
    if quiet and transition is None:
        # The tail records the corresponding token-walk omission below; this
        # explanatory notice must not count the same skipped work a second time.
        _diagnostic("mm: notice: token cache not warm; run 'mm push' to populate\n")
        return False
    if not quiet:
        _diagnostic("mm: warming token cache (one-time, ~3s)...\n")
    try:
        warm_kwargs = (
            _accepted_kwargs(token_usage.warm_token_cache_inline, deadline_s=warm_deadline_s)
            if warm_deadline_s is not None
            else {}
        )
        token_usage.warm_token_cache_inline(claude_paths, **warm_kwargs)
    except Exception as e:
        _diagnostic(f"mm: notice: token cache warm failed: {_exception_detail(e)}\n")
        return False
    return True


def _default_host_readers(
    sources: list[dict],
    *,
    cursor_consented: bool = False,
) -> tuple[tuple[str, HostReader], ...]:
    """The built-in readers the user has CONSENTED to, in their fixed order.

    A reader whose host is not an enabled sync source is not invoked at all —
    see ``HOST_READER_SOURCE_GATE``. Cursor has no sync source: ``cursor_consented`` is its
    only gate (``HOST_USAGE_ONLY_CONSENT``).

    Module-qualified lookups on purpose (docs/invariants/README.md's
    dead-alias rule in reverse): a from-import would bind this module's own global, so a test
    patching ``host_usage.read_codex_usage`` would never reach it.
    """
    enabled = {s.get("name") for s in sources if isinstance(s.get("name"), str)}
    chosen: list[tuple[str, HostReader]] = []
    if HOST_READER_SOURCE_GATE["codex"] in enabled:
        chosen.append(("codex", codex_host.read_codex_usage))
    if cursor_consented:

        def _read_cursor(*, deadline: float) -> host_usage.HostUsageResult:
            return host_usage.read_cursor_usage(deadline=deadline, consented=True)

        chosen.append(("cursor", _read_cursor))
    if HOST_READER_SOURCE_GATE["muse"] in enabled:

        def _read_muse(*, deadline: float) -> host_usage.HostUsageResult:
            return host_usage.read_muse_usage(deadline=deadline, consented=True)

        chosen.append(("muse", _read_muse))
    if HOST_READER_SOURCE_GATE["agy"] in enabled:

        def _read_agy(*, deadline: float) -> host_usage.HostUsageResult:
            return host_usage.read_agy_usage(deadline=deadline, consented=True)

        chosen.append(("agy", _read_agy))
    return tuple(chosen)


def _capture_host_usage(
    readers: Sequence[tuple[str, HostReader]],
    *,
    deadline: float,
    now: Callable[[], float] = time.monotonic,
) -> HostUsageCapture:
    """Read consented hosts under a sweep deadline plus later-reader grace.

    Reader-scoped for FAILURES (Track 31A): a file/record failure still fails
    that whole reader, but a reader failure no longer discards the others.
    The original "partial totals are worse than no totals" argument predates
    ``token_sources``; with per-reader coverage on the wire, deleting known
    Codex tokens because Grok failed is the less truthful behaviour. Publish
    no row only when no reader completed, or the sweep expired before any
    reader was invoked.

    A source that can never hold a ledger is NOT a failure (see
    ``_HOST_ABSENT_REASONS``). It is omitted from ``token_sources`` and from
    ``dropped`` — a standing property of the source, correctly silent.

    A reader that RAISES is caught here rather than at the tail's outer guard,
    because that guard would also discard the git and session rows already
    captured and the terminal ``mm-push`` row with them — an unreadable host
    store must not cost the retro its actual content. The raise is a reader
    failure (``unavailable``) and the sweep continues.

    ``readers`` and ``now`` are injected so ordering, isolation, gating and
    deadline behavior are table-testable without touching a real host store.
    """
    merged: dict[str, dict[str, token_usage.Usage]] = {}
    merged_by_day: dict[str, token_usage.DayBucket] = {}
    contributed: list[str] = []
    usage_days: dict[str, frozenset[str]] = {}
    dropped: list[tuple[str, str]] = []
    partial_days: dict[str, frozenset[str]] = {}
    names_in_order = tuple(name for name, _ in readers)
    for index, (name, read) in enumerate(readers):
        start = now()
        if index == 0 and start >= deadline:
            return HostUsageCapture(None, name, "deadline", invoked=False)
        reader_deadline = (
            max(deadline, start + HOST_READER_GRACE_MS / 1000.0) if index else deadline
        )
        try:
            result = read(deadline=reader_deadline)
        except Exception as e:
            # The breadcrumb reason stays a closed vocabulary, but the TYPE
            # goes to stderr like every other swallow in this module. Without
            # it a `TypeError` regression inside a reader is indistinguishable
            # from a benign transient and can crash-loop on every push while
            # the breadcrumb calmly promises a retry.
            _diagnostic(f"mm: notice: host reader {name} raised: {_exception_detail(e)}\n")
            dropped.append((name, _HOST_UNKNOWN_REASON))
            continue
        if not result.complete:
            reason = result.reason if result.reason in _HOST_READ_REASONS else _HOST_UNKNOWN_REASON
            if reason in _HOST_ABSENT_REASONS:
                # Not installed, in effect. Drop it silently and keep going —
                # the row will name only the sources that actually contributed.
                continue
            dropped.append((name, reason))
            continue
        contributed.append(name)
        usage_days[name] = frozenset(day for days in result.hosts.values() for day in days)
        if result.partial_days:
            partial_days[name] = frozenset(result.partial_days)
        _merge_host_usage_maps(merged, result.hosts, merged_by_day, result.tokens_by_day)
    if contributed or not dropped:
        return HostUsageCapture(
            merged,
            token_sources=tuple(contributed),
            dropped=tuple(dropped),
            tokens_by_day=merged_by_day,
            partial_days=dict(partial_days),
            partial=_canonical_partial(names_in_order, partial_days),
            usage_days=usage_days,
        )
    first_reader, first_reason = dropped[0]
    return HostUsageCapture(None, first_reader, first_reason, dropped=tuple(dropped))


def _merge_host_usage_maps(
    target: dict[str, dict[str, token_usage.Usage]],
    source: dict[str, dict[str, token_usage.Usage]],
    target_by_day: dict[str, token_usage.DayBucket],
    source_by_day: dict[str, token_usage.DayBucket] | None,
) -> None:
    """Add a completed reader's buckets to ``target`` without aliasing it.

    A (family, UTC day) collision is possible whenever two readers classify
    into the same canonical family — historically Codex and OpenCode both
    landed GPT in ``codex``. After the OpenCode reader was removed, no two
    surviving readers collide in production, but the merge still sums
    TOKEN_FIELDS at every level: a shallow map update would silently drop
    whichever reader landed first, and a synthetic or future reader that
    shares a family would regress without this. The same collision exists
    one level down on model id.
    """
    for family, days in source.items():
        target_days = target.setdefault(family, {})
        for day, usage in days.items():
            bucket = target_days.setdefault(day, token_usage.zero_model_bucket())
            token_usage.merge_usage_bucket(bucket, usage)
    if source_by_day:
        token_usage.merge_token_days(target_by_day, source_by_day)


def _merge_partial_days(
    *maps: dict[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """Set-union per reader, copying so mutating an input cannot alias out."""
    days: dict[str, set[str]] = {}
    for mapping in maps:
        for name, values in mapping.items():
            days.setdefault(name, set()).update(values)
    return {name: frozenset(values) for name, values in days.items() if values}


def _canonical_partial(
    names_in_order: Sequence[str],
    partial_days: dict[str, frozenset[str]],
) -> tuple[str, ...]:
    """Rebuild the partial reader list in ``readers`` order. No duplicates."""
    return tuple(name for name in names_in_order if name in partial_days)


def _merge_warm_retry_capture(
    initial: HostUsageCapture,
    retry: HostUsageCapture,
    *,
    readers: Sequence[tuple[str, HostReader]],
    retried_names: set[str],
) -> HostUsageCapture:
    """Preserve completed first-pass readers while replacing retried outcomes.

    A warm retry only re-runs readers that were dropped for ``deadline``. Its
    output is therefore disjoint from the first pass's contributors, so the two
    host maps can be safely added. Replacing the entire capture would let a
    flaky already-completed reader erase truthful first-pass totals.
    """
    merged: dict[str, dict[str, token_usage.Usage]] = {}
    merged_by_day: dict[str, token_usage.DayBucket] = {}
    if initial.hosts is not None:
        _merge_host_usage_maps(merged, initial.hosts, merged_by_day, initial.tokens_by_day)
    if retry.hosts is not None:
        _merge_host_usage_maps(merged, retry.hosts, merged_by_day, retry.tokens_by_day)

    names_in_order = tuple(name for name, _ in readers)
    contributed = set(initial.token_sources) | set(retry.token_sources)
    dropped_by_name = {
        name: reason
        for name, reason in initial.dropped
        if name not in retried_names or not retry.invoked
    }
    dropped_by_name.update(retry.dropped)
    token_sources = tuple(name for name in names_in_order if name in contributed)
    dropped = tuple(
        (name, dropped_by_name[name])
        for name in names_in_order
        if name in dropped_by_name and name not in contributed
    )
    partial_days = _merge_partial_days(initial.partial_days, retry.partial_days)
    partial = _canonical_partial(names_in_order, partial_days)
    usage_days = {**initial.usage_days, **retry.usage_days}

    if initial.complete or retry.complete:
        return HostUsageCapture(
            merged,
            token_sources=token_sources,
            dropped=dropped,
            invoked=True,
            tokens_by_day=merged_by_day,
            partial_days=partial_days,
            partial=partial,
            usage_days=usage_days,
        )
    if dropped:
        first_reader, first_reason = dropped[0]
        return HostUsageCapture(
            None,
            first_reader,
            first_reason,
            dropped=dropped,
            invoked=True,
        )
    return HostUsageCapture(None, invoked=True)


def _warm_host_cache_with_notice(reader: str = "codex") -> None:
    """Attended notice only; the capture core contains and publishes the read."""
    _diagnostic(f"mm: reading {reader} usage (bounded by the capture budget)...\n")


@dataclass(frozen=True)
class HostReadEvidence:
    last_deadline_allotted_ms: int | None = None
    last_complete_ms: int | None = None
    last_complete_at: str | None = None
    files_cached: int | None = None
    files_on_disk: int | None = None


def host_read_age(timestamp: str | None) -> str:
    """Age of validated local cache metadata; future clocks are not fresh reads."""
    if timestamp is None:
        return "unknown"
    try:
        when = datetime.fromisoformat(timestamp)
        seconds = (datetime.now(timezone.utc) - when).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if seconds < 0:
        return "in the future"
    for unit, width in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= width:
            return f"{int(seconds // width)} {unit} ago"
    return f"{int(seconds)} s ago"


def _host_skip_phrase(
    reader: str,
    reason: str,
    *,
    readiness: mm_config.UsageCaptureReadiness,
    upgrade_required: bool = False,
    verb: Literal["push", "init"] = "push",
    evidence: HostReadEvidence | None = None,
    in_diag: bool = False,
    attended: bool = False,
) -> str:
    """Closed-vocabulary reader remedy; callers escape at their display sink."""
    phrase = (
        f"host-usage snapshot skipped ({reader} {reason}) — "
        "content sync and git/session capture unaffected"
    )
    if readiness != "ready":
        return f"{phrase}. {mm_config.usage_capture_remedy(readiness, reader=reader)}"
    if upgrade_required and reason not in _HOST_PERMANENT_REASONS:
        return (
            f"{phrase}. A consented reader has an unsupported record format; "
            "run mm update before refreshing usage."
        )
    if reason in _HOST_PERMANENT_REASONS:
        stop = (
            mm_config.host_usage_consent_remedy(reader, enabled=False).removesuffix(".").lower()
            if HOST_READER_SOURCE_GATE.get(reader) is None
            else f"run `mm disable-source {reader}`"
        )
        return (
            f"{phrase}. {reader} wrote a record this version cannot read. "
            f"A newer mm may read it: run `mm update`, or {stop} to stop retrying."
        )
    if reason == "deadline":
        if attended:
            return (
                f"{phrase}. Attended warming also exhausted its allowance. "
                "Check mm diag for cached progress and the last complete read; the next "
                f"attended mm push retries automatically. See {HOST_USAGE_CAPTURE_URL}."
            )
        if evidence is not None:

            def shown(value: int | None) -> str:
                return "unknown" if value is None else str(value)

            unit = {"muse": "session logs", "agy": "conversation DBs"}.get(reader, "rollouts")
            if reader == "cursor":
                progress = (
                    f"{shown(evidence.files_cached)} retained runs; "
                    "Conductor files still on disk are not counted"
                )
            else:
                progress = (
                    f"{shown(evidence.files_cached)} of "
                    f"{shown(evidence.files_on_disk)} {unit} cached"
                )
            return (
                f"{phrase}. Last read allowed {shown(evidence.last_deadline_allotted_ms)} ms; "
                f"last complete read {shown(evidence.last_complete_ms)} ms "
                f"({host_read_age(evidence.last_complete_at)}); "
                f"{progress}. "
                "If the last complete read is over your autopush budget, raise "
                "`[retro] host_usage_autopush_budget_ms`; attended mm push refreshes usage "
                "and can warm cold readers; "
                f"see {HOST_USAGE_CAPTURE_URL}."
            )
        compare = (
            "; compare the last complete read with the budget"
            if in_diag
            else ", then `mm diag` to compare the last complete read with the budget"
        )
        return (
            f"{phrase}. The {reader} read did not finish inside this {verb}'s read budget. "
            f"An attended mm push can warm cold readers{compare}; "
            f"see {HOST_USAGE_CAPTURE_URL}."
        )
    diag = "" if in_diag else "; `mm diag` shows the reader's state"
    return f"{phrase}. Attended mm push refreshes usage automatically{diag}."


def _capture_host_snapshot(
    device_id: str,
    host_readers: Sequence[tuple[str, HostReader]],
    *,
    host_budget_ms: int,
    warm_host_cache: Callable[[str], None] | None = None,
) -> tuple[HostUsageCapture, list[dict]]:
    """One bounded sweep, one warm/retry, at most one row. No writes.

    tail ─────┐
    backfill ─┼─> shared event capture ─> this helper
    attended push ────────────────────> this helper

    Wrappers own notices and persistence; autopush supplies no warm callback.

    Track 85A: with a configured ``capture_budget_ms`` (stashed by
    ``resolve_host_read_budget``), sweep + inline warm share one phase
    deadline of at most half the capture budget; without it (legacy key
    only) each warm retry keeps its legacy fresh allowance. Elapsed
    attended time is recorded for the events tail.
    """
    global _attended_host_elapsed_s, _pending_capture_budget_ms
    started = time.monotonic()
    _attended_host_elapsed_s = None
    capture_ms = _pending_capture_budget_ms
    _pending_capture_budget_ms = None
    phase_deadline = started + max(1, capture_ms // 2) / 1000.0 if capture_ms is not None else None
    host_capture = _capture_host_usage(
        host_readers, deadline=time.monotonic() + host_budget_ms / 1000.0
    )
    if not host_capture.invoked and host_capture.reason == "deadline":
        # No reader ran: declare every omission so a failed warm cannot vanish
        # when a sibling's retry succeeds. Non-warmable readers stay declared.
        host_capture = HostUsageCapture(
            None,
            host_capture.reader,
            "deadline",
            dropped=tuple((name, "deadline") for name, _ in host_readers),
            invoked=False,
        )
    deadline_names = {name for name, reason in host_capture.dropped if reason == "deadline"}
    if warm_host_cache is not None:
        # The attended warm IS the retry. Contain it through the same reader
        # boundary, then replace only that reader's first-pass outcome.
        for name, _read in host_readers:
            if name not in deadline_names or name not in WARMABLE_HOST_READERS:
                continue
            warm_host_cache(name)

            def warm_read(*, deadline: float) -> host_usage.HostUsageResult:
                remaining = max(0.0, deadline - time.monotonic())
                return host_registry.warm_host_cache_inline(
                    reader=name,
                    **_accepted_kwargs(host_registry.warm_host_cache_inline, budget_s=remaining),
                )

            retry_capture = _capture_host_usage(
                ((name, warm_read),),
                deadline=(
                    phase_deadline
                    if phase_deadline is not None
                    else time.monotonic() + host_usage.DEFAULT_READ_BUDGET_S
                ),
            )
            host_capture = _merge_warm_retry_capture(
                host_capture,
                retry_capture,
                readers=host_readers,
                retried_names={name},
            )
    host_rows: list[dict] = []
    if host_capture.hosts is not None:
        host_rows.append(
            events.make_host_usage_snapshot(
                device=device_id,
                hosts=host_capture.hosts,
                token_sources=host_capture.token_sources,
                degraded_sources=tuple(name for name, _ in host_capture.dropped),
                tokens_by_day=host_capture.tokens_by_day or {},
                partial_days=host_capture.partial_days,
                usage_days=host_capture.usage_days,
            )
        )

    if warm_host_cache is not None:
        _attended_host_elapsed_s = time.monotonic() - started
    return host_capture, host_rows


def _capture_event_snapshots(
    config: dict,
    claude_paths: list[Path],
    device_id: str,
    *,
    since: datetime,
    budget_ms: int,
    prepare_token_cache: Callable[[], CacheLockMode],
    host_readers: Sequence[tuple[str, HostReader]],
    root_discovery_budget_ms: int | None = None,
    host_budget_ms: int | None = None,
    git_budget_ms: int | None = None,
    warm_host_cache: Callable[[str], None] | None = None,
    suppress_host_capture: bool = False,
    origin: str | None = None,
    persist: bool = False,
    use_repo_cursors: bool = False,
    session_deadline: float | None = None,
) -> CaptureResult:
    """Capture device-stamped snapshots; persist discovery only when requested.

    Callers retain token-cache policy in ``prepare_token_cache``; it runs after
    the bounded git walk and returns whether token data is available and, when
    it is, whether contention should warn or block. The cache context stays
    open for every Claude root, preserving the token cache's read/modify/write
    lock contract without holding it across git subprocess work. The session
    deadline begins after caller-owned preparation.

    Host capture runs last, on its own ``host_budget_ms`` deadline started
    after the session walk (``walk_done`` is taken before the token-cache
    write, Track 85A), and yields at most one
    optional row. Its outcome is returned as data — the notice and the
    ``autopush`` breadcrumb are wrapper policy. ``warm_host_cache`` is the
    attended notice hook only: its presence gates the core warm, which always
    calls ``host_usage.warm_host_cache_inline``. Autopush omits it so an
    unattended hook never warms. The attended warm result is published
    directly. On 2026-09-17, device 3a6c7dc9 / Python 3.14.7 measured warm
    Codex at 166.66–168.26 ms before serialization and an empty-cache parse
    at 2,949.68 ms; see events-retro.md for the full interpreter/phase split.
    """
    global _pending_capture_budget_ms
    if root_discovery_budget_ms is None:
        root_discovery_budget_ms = (
            events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS
            if budget_ms <= events.WALK_TIME_BUDGET_AUTOPUSH_MS
            else events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS
        )
    if host_budget_ms is None:
        # A suppressed snapshot never consumes the resolver's capture stash.
        host_budget_ms = (
            0
            if suppress_host_capture
            else resolve_host_read_budget(
                config, interactive=budget_ms > events.WALK_TIME_BUDGET_AUTOPUSH_MS
            )[0]
        )
    git_walk_budget_ms = git_budget_ms if git_budget_ms is not None else budget_ms
    # No staged state from an earlier in-process capture may ride along.
    events.discard_pending_session_state()
    try:
        git_rows, root_discovery, walk_budget_aborts, walk_errors = _capture_git_rows(
            config,
            device_id,
            since=since,
            persist=persist,
            use_repo_cursors=use_repo_cursors,
            walk_budget_ms=git_walk_budget_ms,
            root_discovery_budget_ms=root_discovery_budget_ms,
            origin=origin,
            deadline_monotonic=session_deadline,
        )
    except BaseException:
        _pending_capture_budget_ms = None
        raise
    _roots, discovery_errors = root_discovery

    try:
        token_cache_mode = prepare_token_cache()
    except BaseException:
        _pending_capture_budget_ms = None
        raise
    # Track 85A: an attended tail passes ONE absolute deadline shared by
    # git walk, token warm (in prepare) and this session walk, so the tail
    # share is spent once, not once per phase. Legacy callers keep the
    # fresh post-preparation deadline.
    deadline = (
        session_deadline if session_deadline is not None else time.monotonic() + budget_ms / 1000.0
    )
    projects: list[dict] = []
    warn_lock_unavailable = False

    def walk_sessions(token_cache_files: dict | None) -> None:
        for claude_dir in claude_paths:
            for row in events.walk_session_metadata(
                claude_dir,
                since=since,
                deadline_monotonic=deadline,
                token_cache_files=token_cache_files,
                **_accepted_kwargs(events.walk_session_metadata, persist=persist),
            ):
                projects.extend(row.get("projects", []))

    # Track 85A d9: walk_done is bound in every branch BEFORE the token
    # cache context writes on exit, so cache serialization never masquerades
    # as walk time; staged session state flushes only after that exit.
    try:
        if token_cache_mode is None:
            walk_sessions(None)
            walk_done = time.monotonic()
        elif claude_paths:
            with token_usage.lock_and_get_files(token_cache_mode) as files_dict:
                # Only warn-mode contention is a caller-visible fact. A blocking
                # lock normally cannot yield None, and init has no degradation
                # breadcrumb to return even if a defensive implementation does.
                warn_lock_unavailable = token_cache_mode == "warn" and files_dict is None
                walk_sessions(files_dict)
                walk_done = time.monotonic()
        else:
            walk_done = time.monotonic()
    except BaseException:
        # An unconsumed capture stash must not leak into a later capture.
        # Staged session records are complete-only by construction, so
        # flush them (the token lock has unwound by here) instead of
        # discarding earlier roots' finished work.
        _pending_capture_budget_ms = None
        if persist:
            try:
                events.flush_pending_session_state()
            except Exception:
                events.discard_pending_session_state()
        raise
    if persist:
        events.flush_pending_session_state()
    session_rows: list[dict] = []
    if claude_paths:
        session_rows.append(
            {
                "v": events.EVENTS_SCHEMA_VERSION,
                "type": "sessions-snapshot",
                "ts": datetime.now(timezone.utc).isoformat(),
                "device": device_id,
                "projects": projects,
            }
        )

    # Host capture starts only AFTER the `walk_done` snapshot, on a fresh
    # absolute deadline of its own. Both halves matter: reading the hosts
    # before the snapshot would let a slow host store trip the session-walk
    # notice, and reusing `deadline` would silently spend whatever the walk
    # left over (usually nothing on a busy machine, so the row would vanish
    # exactly when it is most interesting).
    host_capture, host_rows = (
        (HostUsageCapture({}), [])
        if suppress_host_capture
        else _capture_host_snapshot(
            device_id,
            host_readers,
            host_budget_ms=host_budget_ms,
            warm_host_cache=warm_host_cache,
        )
    )

    return CaptureResult(
        git_rows=git_rows,
        session_rows=session_rows,
        host_rows=host_rows,
        host_capture=host_capture,
        root_discovery=root_discovery,
        discovery_errors=discovery_errors,
        session_walk_exceeded_budget=walk_done > deadline,
        warn_lock_unavailable=warn_lock_unavailable,
        token_cache_requested=token_cache_mode is not None,
        walk_budget_aborts=walk_budget_aborts,
        walk_errors=walk_errors,
    )


def _capture_git_rows(
    config: dict,
    device_id: str,
    *,
    since: datetime,
    walk_budget_ms: int,
    root_discovery_budget_ms: int,
    origin: str | None = None,
    persist: bool = False,
    use_repo_cursors: bool = False,
    deadline_monotonic: float | None = None,
) -> tuple[list[dict], events.GitRootDiscovery, int, int]:
    """Discover roots and walk git, optionally persisting the local project index.

    Shared by the push tail, init backfill, and recapture. ``origin`` marks
    non-push rows (init and recapture) so the aggregator can exclude them
    from push counts.
    """
    discovery_deadline = time.monotonic() + root_discovery_budget_ms / 1000.0
    if deadline_monotonic is not None:
        discovery_deadline = min(discovery_deadline, deadline_monotonic)
    root_discovery = events.discover_git_roots(
        config,
        deadline_monotonic=discovery_deadline,
        persist=persist,
        since=since,
    )
    root_discovery = events.dedup_git_root_discovery(root_discovery)
    roots, _discovery_errors = root_discovery
    if use_repo_cursors:
        since = events.effective_capture_since(roots, since)
    walk_kwargs = {} if _DIAGNOSTIC.get() is None else {"diagnostic": _diagnostic}
    if use_repo_cursors and roots:
        walk_kwargs["use_repo_cursors"] = True
    if roots:
        author_emails = events.resolve_walk_author_emails(config, roots)
        author_emails = identity.union_checked_github_noreply(author_emails)
        walk_kwargs.update(_accepted_kwargs(events.walk_git_projects, author_emails=author_emails))
    if deadline_monotonic is not None:
        # The git walk gets what discovery actually left of the tail
        # deadline; at <=0 walk_git_projects records budget_abort skips
        # without spawning worker threads.
        walk_budget_ms = min(
            walk_budget_ms, max(0, int((deadline_monotonic - time.monotonic()) * 1000))
        )
    git_rows = events.walk_git_projects(
        roots,
        since=since,
        total_budget_ms=walk_budget_ms,
        **walk_kwargs,
    )
    skipped = git_rows[0].get("skipped") or [] if git_rows else []
    aborts, errs = events.walk_skip_counts(skipped)
    capture = events.make_git_capture(
        since=since,
        discovery=events.classify_discovery(root_discovery),
        walk_budget_aborts=aborts,
        walk_errors=errs,
    )
    for row in git_rows:
        row["device"] = device_id
        row["git_capture"] = capture
        if origin is not None:
            row["origin"] = origin
    return git_rows, root_discovery, aborts, errs


def _git_walk_degradation(aborts: int, errors: int) -> str | None:
    n = aborts + errors
    if n <= 0:
        return None
    return _GIT_WALK_DEGRADATION.format(n=n)


def _run_events_tail(
    config: dict,
    sources: list[dict],
    device_id: str,
    *,
    dry_run: bool,
    quiet: bool,
    suppress_host_capture: bool = False,
    capture_activity: bool = True,
) -> list[str]:
    """Capture fleet-retro events after ``_push_core``'s substantive-change gate.

    Returns bounded human-readable candidates with full occurrence/omission
    counts on Degradations. Automatic callers retain the existing degraded
    breadcrumb. A scoped diagnostic sink controls rendering only; init and
    direct callers keep stderr defaults. Record facts before optional output.

    See docs/invariants/events-retro.md "Events tail in `_push_core`" for
    the load-bearing invariants: head-position single-call-site (Codex C4
    — branch-fragility-free, one-push-lag-free), dry_run no-op (preview
    contract), mm-events-resolved gate (covers fresh / migrated / un-
    migrated configs uniformly, Codex C1), the independent 50ms autopush /
    100ms interactive root-discovery budget, and (Track 85A) the one
    ``[retro] capture_budget_ms`` partition for attended pushes with autopush
    keeping its fixed 250ms walk/share budgets. The "budget exceeded"
    notice reports on the session-metadata walk (the git walk self-bounds via
    its own total_budget_ms); the snapshot is taken before the self-bounded
    identity gather AND before host capture, so neither a cold 7d-TTL identity
    refresh nor a slow host store masquerades as a slow walk (v0.12.9).

    Forensic-only invariant: any failure in this block is swallowed and
    breadcrumbed via ``mm: notice:``. The push proceeds.
    Attended push captures first through ``_capture_host_snapshot`` with strict
    append, then suppresses this tail's host capture even on capture failure. A usage-only
    refresh also skips all activity work before any walk or cache lock. A
    content-carrying refresh records one push and can advance the Git cursor.
    Unattended no-op pushes still do zero host work.
    """
    global _attended_host_elapsed_s
    degradations: list[str] = Degradations()
    if dry_run or not capture_activity:
        _attended_host_elapsed_s = None
        return degradations
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        _attended_host_elapsed_s = None
        return degradations
    try:
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"
        events.reconcile_uncommitted_push_cursors(events_dir, device_id)
        cursor = events.resolve_push_cursor(events_dir, device_id)
        since = events.effective_capture_since([], cursor.since)
        # Session-walk budget stays the quiet/interactive pair. Git-walk
        # budget escalates independently when the cursor is old — mixing
        # the two would make a first-run 30-day cursor silently raise the
        # session-walk notice threshold (and break the AUTOPUSH=0 pin).
        # Attended (Track 85A): one capture budget partitions across the
        # pre-scan host phase and this tail. The tail keeps at least half,
        # plus whatever the host phase left unused; autopush keeps its
        # fixed 250 ms walk / 50 ms discovery shares.
        tail_deadline: float | None = None
        if quiet:
            budget_ms = events.WALK_TIME_BUDGET_AUTOPUSH_MS
            git_budget_ms = events.git_walk_budget_ms(quiet=True, since=since)
            _attended_host_elapsed_s = None
        else:
            retro = config.get("retro")
            legacy_only = (
                isinstance(retro, dict)
                and "host_usage_interactive_budget_ms" in retro
                and "capture_budget_ms" not in retro
            )
            if legacy_only:
                # Only the legacy key configured: legacy tail budgets and
                # legacy warm allowance apply unchanged (Track 85A d6).
                budget_ms = events.WALK_TIME_BUDGET_INTERACTIVE_MS
                git_budget_ms = events.git_walk_budget_ms(quiet=False, since=since)
                _attended_host_elapsed_s = None
            else:
                capture_ms, _capture_source = resolve_capture_budget(config)
                elapsed_ms = int((_attended_host_elapsed_s or 0.0) * 1000)
                _attended_host_elapsed_s = None
                if suppress_host_capture:
                    budget_ms = max(capture_ms // 2, capture_ms - elapsed_ms)
                else:
                    budget_ms = max(1, capture_ms // 2)
                git_budget_ms = min(events.git_walk_budget_ms(quiet=False, since=since), budget_ms)
                # One absolute deadline for git + warm + session walk.
                tail_deadline = time.monotonic() + budget_ms / 1000.0

        claude_paths = _enabled_claude_paths(sources)

        def prepare_tail_token_cache() -> CacheLockMode:
            # Token cache wiring (v0.11.14+). This remains wrapper policy:
            # cold-cache warming and its notices must happen after the git
            # walk, just as they did before extraction. Partitioned attended
            # warming gets only the time left before the tail deadline
            # (Track 85A d4/d9); legacy/autopush keep their former behavior.
            warm_kwargs = (
                _accepted_kwargs(
                    _decide_token_walk_policy,
                    warm_deadline_s=max(0.0, tail_deadline - time.monotonic()),
                )
                if tail_deadline is not None
                else {}
            )
            do_token_walk = _decide_token_walk_policy(
                claude_paths,
                quiet=quiet,
                **warm_kwargs,
            )
            if not do_token_walk:
                return None
            return "warn" if quiet else "block"

        capture = _capture_event_snapshots(
            config,
            claude_paths,
            device_id,
            since=since,
            budget_ms=budget_ms,
            git_budget_ms=git_budget_ms,
            persist=True,
            use_repo_cursors=True,
            root_discovery_budget_ms=(
                events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS
                if quiet
                else events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS
            ),
            host_budget_ms=(
                0
                if suppress_host_capture
                else resolve_host_read_budget(config, interactive=not quiet)[0]
            ),
            session_deadline=tail_deadline,
            # Only the attended path may spend seconds warming each cold reader. On autopush a
            # cold corpus instead converges across pushes, because an aborted
            # scan now keeps its per-file progress.
            warm_host_cache=None if quiet else _warm_host_cache_with_notice,
            suppress_host_capture=suppress_host_capture,
            prepare_token_cache=prepare_tail_token_cache,
            host_readers=_default_host_readers(
                sources,
                cursor_consented=mm_config.cursor_host_usage_enabled(config),
            ),
        )

        if capture.git_rows:
            since = datetime.fromisoformat(capture.git_rows[0]["git_capture"]["since"])
        source_names = [s["name"] for s in sources if isinstance(s.get("name"), str)]
        # Fleet-wide author-email trust set (v0.11.17). gather_local_identities
        # is cache-first: hot path is ~1ms; cold/stale path emits a single
        # `mm: notice: refreshing identity cache (one-off)` line and runs a
        # synchronous refresh inline (D1 from /plan-eng-review — the user
        # accepted the one-off slow path over budget contortions). Emitted
        # as `local_emails: []` (explicit empty) when this machine has no
        # configured identities — distinguishable from "pre-v0.11.17 peer
        # with no field at all" so the aggregator can choose its fallback.
        local_emails = identity.gather_local_identities(
            allow_refresh=True,
            root_discovery=capture.root_discovery,
            **({"diagnostic": _DIAGNOSTIC.get()} if _DIAGNOSTIC.get() is not None else {}),
        )
        mm_event = events.make_mm_push_event(
            device=device_id,
            mm_version=__version__,
            sources=source_names,
            discovery_errors=capture.discovery_errors,
            local_emails=local_emails,
            git_capture=events.make_git_capture(
                since=since,
                discovery=events.classify_discovery(capture.root_discovery),
                walk_budget_aborts=capture.walk_budget_aborts,
                walk_errors=capture.walk_errors,
            ),
        )
        # CT-4 invariant: mm-push event LAST so a partial write doesn't
        # advance the next-push cursor. The optional host row sits between the
        # session rows and it — it is capture data like the others, and must
        # not displace the terminal row.
        append_result = events.write_push_event(
            events_dir,
            device_id,
            [
                *capture.git_rows,
                *capture.session_rows,
                *capture.host_rows,
                mm_event,
            ],
            max_file_size=config.get("sync", {}).get(
                "max_file_size", mm_config.DEFAULT_MAX_FILE_SIZE
            ),
        )
        if isinstance(append_result, events.EventAppendSkipped):
            degradations.append("event append skipped (max-file-size)")
        elif events.latest_mm_push_row(events_dir, device_id) == mm_event:
            events.commit_repo_cursors(
                capture.git_rows,
                since=since,
                cursor_ts=datetime.fromisoformat(mm_event["ts"]),
                recorded_ts=mm_event["ts"],
                complete=(
                    events.classify_discovery(capture.root_discovery) not in {"partial", "empty"}
                    and not capture.walk_budget_aborts
                    and not capture.walk_errors
                ),
            )

        if cursor.held:
            degradations.append(_CURSOR_HOLD_DEGRADATION)
            _diagnostic(f"mm: notice: {_CURSOR_HOLD_DEGRADATION}\n")
        elif cursor.floored_after_incomplete:
            degradations.append(_CURSOR_COVERAGE_DEGRADATION)
            _diagnostic(f"mm: notice: {_CURSOR_COVERAGE_DEGRADATION}\n")
        if capture.root_discovery.exceeded:
            degradations.append(_ROOT_DISCOVERY_DEGRADATION)
            _diagnostic(f"mm: notice: {_ROOT_DISCOVERY_DEGRADATION}\n")
        elif capture.root_discovery.errors:
            degradations.append(_ROOT_DISCOVERY_ERROR_DEGRADATION)
            _diagnostic(f"mm: notice: {_ROOT_DISCOVERY_ERROR_DEGRADATION}\n")
        elif not capture.root_discovery.roots and capture.root_discovery.probers_ran:
            degradations.append(_ROOT_DISCOVERY_EMPTY_DEGRADATION)
            _diagnostic(f"mm: notice: {_ROOT_DISCOVERY_EMPTY_DEGRADATION}\n")
        walk_phrase = _git_walk_degradation(capture.walk_budget_aborts, capture.walk_errors)
        if walk_phrase is not None:
            degradations.append(walk_phrase)
            _diagnostic(f"mm: notice: {walk_phrase}\n")
        if capture.session_walk_exceeded_budget:
            degradations.append(f"events walk exceeded its {budget_ms}ms budget")
            _diagnostic("mm: notice: events tail budget exceeded\n")
        if capture.warn_lock_unavailable:
            degradations.append("token cache was locked, so tokens and skills are missing")
        # `claude_paths` gate is load-bearing: `_decide_token_walk_policy`
        # ALSO returns False when there is no enabled claude source at all
        # (`if not claude_paths: return False`). That is a config shape, not
        # a degradation — a gstack-only or codex-only machine has no tokens
        # or skills to collect, and reporting it would pin `mm status` at
        # `degraded` forever while blaming a cache that isn't the cause.
        # Reproduced during /review before this guard existed. The remaining
        # False causes (cold cache on autopush, cache stat/parse OSError,
        # inline warm raised) all mean the same thing to the user: this
        # push published no token or skill data.
        if claude_paths and not capture.token_cache_requested:
            degradations.append("token walk skipped, so tokens and skills are missing")
        # A COMPLETED empty host scan is healthy and stays silent. A dropped
        # reader is declared (one degradation per dropped reader) even when
        # other readers still published a row — AGENTS.md: any new degradation
        # detected in the tail MUST be appended to this list, not merely
        # printed. `no_metadata_ledger` is never in `dropped`. Sweep-level
        # veto (no reader completed, or expired before any ran) still uses
        # reader/reason when `dropped` is empty.
        if capture.host_capture.dropped:
            for dropped_reader, dropped_reason in capture.host_capture.dropped:
                phrase = _host_skip_phrase(
                    dropped_reader, dropped_reason, readiness="ready", attended=not quiet
                )
                degradations.append(phrase)
                _diagnostic(f"mm: notice: {phrase}\n")
        elif not capture.host_capture.complete:
            phrase = _host_skip_phrase(
                capture.host_capture.reader,
                capture.host_capture.reason,
                readiness="ready",
                attended=not quiet,
            )
            degradations.append(phrase)
            _diagnostic(f"mm: notice: {phrase}\n")
    except Exception as e:
        _attended_host_elapsed_s = None
        degradations.append(f"events tail failed ({type(e).__name__})")
        _diagnostic(f"mm: notice: events tail failed: {_exception_detail(e)}\n")
    return degradations


def _run_events_backfill(
    config: dict,
    sources: list[dict],
    device_id: str,
) -> None:
    """Init-time backfill of git+sessions events for the past 30 days, plus
    the optional host-usage row.

    Mirrors ``_run_events_tail`` but writes only ``git-snapshot``,
    ``sessions-snapshot`` and (when the host sweep completed)
    ``host-usage-snapshot`` rows — NO ``mm-push`` row. The host row is the one
    exception to "for the past 30 days": the readers aggregate the whole local
    corpus, so it carries the most recent ``MAX_BY_DAY_DAYS`` days no matter
    what window the git and session walks used. Two consequences:

    * Push-count semantics stay honest: an init-counted-as-push would
      inflate the per-window mm-push count in the retro by 1 on every
      fresh-install machine.
    * The cursor (``last_push_ts``) stays at "no prior mm-push" so the
      first real push walks the same 30-day range. Aggregator dedups via
      ``(canonical_remote_url, sha)`` so retro output is unchanged; cost
      is one extra ~500ms ``git log`` walk on the first push, paid once
      per machine.

    Idempotent at the aggregator layer (commits dedup; sessions latest-
    per-tuple wins). Forensic-only on failure: stderr breadcrumb, init
    proceeds.
    """
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return
    try:
        budget_ms = events.WALK_TIME_BUDGET_INTERACTIVE_MS
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"

        # Explicit 30-day window. last_push_ts() returns the same value
        # on first run, but stating intent at the call site makes the
        # backfill semantics legible without chasing a default.
        since = datetime.now(timezone.utc) - timedelta(days=events.INITIAL_CURSOR_LOOKBACK_DAYS)

        claude_paths = _enabled_claude_paths(sources)

        def prepare_backfill_token_cache() -> CacheLockMode:
            # Warm the token cache inline at init (v0.11.14+). One-time cost
            # at init time — kb already accepts init takes a few seconds.
            # Subsequent pushes inherit a warm cache. Keep this after the git
            # walk so a discovery failure retains its former no-warm behavior.
            if not claude_paths:
                return None
            try:
                token_usage.warm_token_cache_inline(claude_paths)
            except Exception as e:
                _diagnostic(
                    f"mm: notice: token cache warm at init failed: {_exception_detail(e)}\n"
                )
            return "block"

        capture = _capture_event_snapshots(
            config,
            claude_paths,
            device_id,
            since=since,
            budget_ms=budget_ms,
            root_discovery_budget_ms=events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS,
            host_budget_ms=resolve_host_read_budget(config, interactive=True)[0],
            # Init already warms the token cache inline and is attended; paying
            # the host warm here means the first push after install inherits a
            # hot cache, exactly as it does for tokens.
            warm_host_cache=_warm_host_cache_with_notice,
            prepare_token_cache=prepare_backfill_token_cache,
            origin=events.GIT_SNAPSHOT_ORIGIN_INIT,
            persist=True,
            host_readers=_default_host_readers(
                sources,
                cursor_consented=mm_config.cursor_host_usage_enabled(config),
            ),
        )

        rows_to_write = [*capture.git_rows, *capture.session_rows, *capture.host_rows]
        if rows_to_write:
            appended = events.write_push_event(
                events_dir,
                device_id,
                rows_to_write,
                max_file_size=config.get("sync", {}).get(
                    "max_file_size", mm_config.DEFAULT_MAX_FILE_SIZE
                ),
            )
            if not isinstance(appended, events.EventAppendSkipped):
                _commit_recorded_git_rows(
                    events_dir,
                    device_id,
                    capture.git_rows,
                    since=since,
                    complete=(
                        events.classify_discovery(capture.root_discovery)
                        not in {"partial", "empty"}
                        and not capture.walk_budget_aborts
                        and not capture.walk_errors
                    ),
                )

        # Warm the identity cache at init (v0.11.17, D5 from /plan-eng-review).
        # First push after init then has hot identity data and emits no
        # slow-path notice. Failure is forensic-only — backfill proceeds.
        try:
            identity.refresh_identity_cache(
                force=True,
                root_discovery=capture.root_discovery,
            )
        except Exception as e:
            _diagnostic(f"mm: notice: identity cache warm at init failed: {_exception_detail(e)}\n")

        if capture.session_walk_exceeded_budget:
            _diagnostic("mm: notice: events backfill budget exceeded\n")
        if capture.root_discovery.exceeded:
            _diagnostic(
                "mm: notice: git repository discovery hit its time budget: "
                "initial retro capture may omit repositories. Run mm diag\n"
            )
        # Init has no `mm-push` row and no autorun breadcrumb, so the notice is
        # the only surface available — and init IS attended, so it lands.
        if capture.host_capture.dropped:
            for dropped_reader, dropped_reason in capture.host_capture.dropped:
                _diagnostic(
                    "mm: notice: "
                    + _host_skip_phrase(
                        dropped_reader,
                        dropped_reason,
                        verb="init",
                        readiness="ready",
                        attended=True,
                    )
                    + "\n"
                )
        elif not capture.host_capture.complete:
            _diagnostic(
                "mm: notice: "
                + _host_skip_phrase(
                    capture.host_capture.reader,
                    capture.host_capture.reason,
                    verb="init",
                    readiness="ready",
                    attended=True,
                )
                + "\n"
            )
    except Exception as e:
        _diagnostic(f"mm: notice: events backfill failed: {_exception_detail(e)}\n")


def _commit_recorded_git_rows(
    events_dir: Path,
    device_id: str,
    git_rows: list[dict],
    *,
    since: datetime,
    complete: bool,
) -> None:
    """Advance local progress only after observing the exact persisted snapshot."""
    if not git_rows:
        return
    scan = events.latest_event_rows(events_dir, device_id, {"git-snapshot"})
    if scan.rows.get("git-snapshot") != git_rows[-1]:
        return
    events.commit_repo_cursors(
        git_rows,
        since=since,
        cursor_ts=datetime.fromisoformat(git_rows[-1]["ts"]),
        complete=complete,
    )


@dataclass
class RecaptureCapture:
    """Git-only recapture observation. No writes. No mm-push row."""

    git_rows: list[dict]
    root_discovery: events.GitRootDiscovery
    walk_budget_aborts: int
    walk_errors: int
    events_dir: Path
    since: datetime
    until: datetime


def _prepare_recapture(
    config: dict,
    sources: list[dict],
    device_id: str,
    *,
    since: datetime,
    persist: bool = False,
) -> RecaptureCapture | None:
    """Git-only capture at interactive budgets. ``None`` if mm-events is unresolved."""
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return None
    events_dir = Path(mm_events_src["path"]).expanduser() / "events"
    # ``write_push_event`` creates this parent only when there are rows to
    # append. Preparing a dry-run or a zero-root recapture is read-only.
    until = datetime.now(timezone.utc)
    git_rows, root_discovery, aborts, errors = _capture_git_rows(
        config,
        device_id,
        since=since,
        walk_budget_ms=events.WALK_TIME_BUDGET_INTERACTIVE_MS,
        root_discovery_budget_ms=events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS,
        origin=events.GIT_SNAPSHOT_ORIGIN_RECAPTURE,
        persist=persist,
    )
    return RecaptureCapture(
        git_rows=git_rows,
        root_discovery=root_discovery,
        walk_budget_aborts=aborts,
        walk_errors=errors,
        events_dir=events_dir,
        since=since,
        until=until,
    )


__all__ = [
    "_decide_token_walk_policy",
    "_enabled_claude_paths",
    "_run_events_backfill",
    "_run_events_tail",
]
