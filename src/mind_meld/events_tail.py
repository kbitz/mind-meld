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

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Sequence, get_args

from mind_meld import __version__, events, host_usage, identity, token_usage, upgrade
from mind_meld.config import grok_host_usage_enabled
from mind_meld.safety import safe_str

CacheLockMode = Literal["warn", "block"] | None
HostReader = Callable[..., host_usage.HostUsageResult]

HOST_USAGE_READ_BUDGET_INTERACTIVE_MS = 500
HOST_USAGE_READ_BUDGET_AUTOPUSH_MS = 250
"""Host reads get their OWN absolute deadline, deliberately separate from the
git/session walk budgets. It starts after the ``walk_done`` snapshot so host
time can never trigger or redefine the session-walk notice, and it is passed
explicitly to every reader — no caller may fall through to ``host_usage``'s
5-second default, which is ~20x an autopush's entire walk budget."""

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
    "Run mm diag, then mm recapture 30d to recover the omitted commits"
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
consent, OpenCode's legacy message files) can never produce data on this
machine. That is a known, permanent ABSENCE — categorically unlike a failed
read — so the sweep drops the reader from ``token_sources`` and carries on
with the rest rather than vetoing the whole snapshot.

This is deliberately NOT keyed on ``unsupported``. Codex returns ``unsupported``
when it finds a ledger it cannot attribute, and OpenCode when a row is
malformed: those mean "real usage exists here that I could not read". Track
31A isolates that to the failing reader (dropped and declared); it is still
not treated as absence.

Pinned as a SUBSET of ``_HOST_READ_REASONS`` by
``test_absent_reasons_are_real_reader_reasons``: a rename on the
``host_usage.Reason`` side would otherwise empty this set silently and treat
an honest absence as a failed read."""

_HOST_PERMANENT_REASONS = frozenset({"unsupported"})
"""Failure reasons a later push cannot fix, so the notice must not promise a
retry. Distinct from ``_HOST_ABSENT_REASONS``: these drop the reader (declared)
rather than remaining silent, and they never claim a retry will help."""

HOST_READER_SOURCE_GATE: dict[str, str | None] = {
    "codex": "codex",
    # Grok is a real scoped sync source (Track 22B). Source-enabled is
    # consent, matching Codex. The 21A [retro].grok_host_usage bit remains
    # an OR so a prior usage-only opt-in does not go dark.
    "grok": "grok",
    "opencode": "opencode",
}
"""Which enabled sync source each reader's consent derives from.

A reader that parses a host's local store only runs when the user enabled that
host as a source. The Claude session walk has always been gated this way
(``_enabled_claude_paths``); the host readers shipped without it, which meant a
user who declined the ``codex`` source still had `~/.codex/sessions` parsed and
their totals published to the fleet. Only aggregates ever crossed the boundary,
but "we read it unless you read the README" is the wrong default for a tool
whose whole premise is scoped, opt-in sync."""

WARMABLE_HOST_READERS: frozenset[str] = frozenset({"codex", "grok"})
"""Readers with an incremental cache a warm can populate. OpenCode's
adapter cache stores no totals and is not warmable."""


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


def _decide_token_walk_policy(
    claude_paths: list[Path],
    *,
    quiet: bool,
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
        sys.stderr.write("mm: notice: token cache not warm; run 'mm push' to populate\n")
        return False
    if not quiet:
        sys.stderr.write("mm: warming token cache (one-time, ~3s)...\n")
    try:
        token_usage.warm_token_cache_inline(claude_paths)
    except Exception as e:
        sys.stderr.write(
            f"mm: notice: token cache warm failed: {type(e).__name__}: {safe_str(e)}\n"
        )
        return False
    return True


def _default_host_readers(
    sources: list[dict],
    *,
    grok_consented: bool = False,
) -> tuple[tuple[str, HostReader], ...]:
    """The built-in readers the user has CONSENTED to, in their fixed order.

    A reader whose host is not an enabled sync source is not invoked at all —
    see ``HOST_READER_SOURCE_GATE``. Grok is included when the grok source is
    enabled or when ``grok_consented`` is true (21A bit), bound to
    ``consented=True``.

    Module-qualified lookups on purpose (CLAUDE.md's dead-alias rule in
    reverse): a from-import would bind this module's own global, so a test
    patching ``host_usage.read_codex_usage`` would never reach it.
    """
    enabled = {s.get("name") for s in sources if isinstance(s.get("name"), str)}
    chosen: list[tuple[str, HostReader]] = []
    if HOST_READER_SOURCE_GATE["codex"] in enabled:
        chosen.append(("codex", host_usage.read_codex_usage))
    if HOST_READER_SOURCE_GATE["grok"] in enabled or grok_consented:

        def _read_grok(*, deadline: float) -> host_usage.HostUsageResult:
            return host_usage.read_grok_usage(deadline=deadline, consented=True)

        chosen.append(("grok", _read_grok))
    if HOST_READER_SOURCE_GATE["opencode"] in enabled:
        chosen.append(("opencode", host_usage.read_opencode_usage))
    return tuple(chosen)


def _capture_host_usage(
    readers: Sequence[tuple[str, HostReader]],
    *,
    deadline: float,
    now: Callable[[], float] = time.monotonic,
) -> HostUsageCapture:
    """Read the consented host sources under ONE explicit deadline.

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
    contributed: list[str] = []
    dropped: list[tuple[str, str]] = []
    remaining = list(readers)
    invoked = False
    while remaining:
        name, read = remaining[0]
        if now() >= deadline:
            if invoked:
                dropped.extend((rest_name, "deadline") for rest_name, _ in remaining)
                if contributed:
                    return HostUsageCapture(
                        merged,
                        token_sources=tuple(contributed),
                        dropped=tuple(dropped),
                    )
                first_reader, first_reason = dropped[0]
                return HostUsageCapture(
                    None,
                    first_reader,
                    first_reason,
                    dropped=tuple(dropped),
                )
            return HostUsageCapture(None, name, "deadline", invoked=False)
        remaining.pop(0)
        invoked = True
        try:
            result = read(deadline=deadline)
        except Exception as e:
            # The breadcrumb reason stays a closed vocabulary, but the TYPE
            # goes to stderr like every other swallow in this module. Without
            # it a `TypeError` regression inside a reader is indistinguishable
            # from a benign transient and can crash-loop on every push while
            # the breadcrumb calmly promises a retry.
            sys.stderr.write(
                f"mm: notice: host reader {name} raised: {type(e).__name__}: {safe_str(e)}\n"
            )
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
        # Codex and OpenCode both classify into the `codex` family, so a
        # collision on (family, UTC day) is ordinary rather than an error.
        # Sum the four TOKEN_FIELDS through the shared helper — a shallow map
        # update would silently drop whichever reader landed first.
        for family, days in result.hosts.items():
            target = merged.setdefault(family, {})
            for day, usage in days.items():
                bucket = target.setdefault(day, token_usage.zero_model_bucket())
                token_usage.merge_usage_bucket(bucket, usage)
    if contributed or not dropped:
        return HostUsageCapture(merged, token_sources=tuple(contributed), dropped=tuple(dropped))
    first_reader, first_reason = dropped[0]
    return HostUsageCapture(None, first_reader, first_reason, dropped=tuple(dropped))


def _warm_host_cache_with_notice(reader: str = "codex") -> bool:
    """Telegraph and run the one-off host-cache warm. Never raises.

    Returns whether the warm actually COMPLETED. That return value is the
    retry backstop: if the warm could not finish inside its own (much larger)
    budget, the bounded read that follows cannot finish either, and paying for
    it just adds latency to a push that will publish nothing anyway. Without
    it, a corpus that outgrows even the warm budget makes every interactive
    push pay bounded-attempt + warm + bounded-retry, forever.

    Wrapper policy, kept out of the capture core so that core stays notice-free
    (it is shared by push and init, which report differently).
    """
    # States the real ceiling rather than a measured-once estimate: the warm is
    # bounded by `DEFAULT_READ_BUDGET_S`, not by the ~600ms one corpus happened
    # to take.
    sys.stderr.write(
        f"mm: warming host usage cache (one-time, up to "
        f"{host_usage.DEFAULT_READ_BUDGET_S:.0f}s)...\n"
    )
    try:
        return host_usage.warm_host_cache_inline(reader=reader).complete
    except Exception as e:
        sys.stderr.write(
            f"mm: notice: host usage cache warm failed: {type(e).__name__}: {safe_str(e)}\n"
        )
        return False


def _host_skip_phrase(reader: str, reason: str) -> str:
    """One stable, safe sentence for a dropped or omitted host reader.

    It names the affected optional subsystem so a `degraded` breadcrumb can't
    be misread as content-sync loss, and it names only the reader and reason
    class — never a path, transcript, query, or exception string. Permanent
    reasons carry a fix clause and never promise a retry. The phrase contains
    no ``; ``, which is the breadcrumb join separator.
    """
    phrase = (
        f"host-usage snapshot skipped ({reader} {reason}) — "
        "content sync and git/session capture unaffected"
    )
    if reason in _HOST_PERMANENT_REASONS:
        return (
            f"{phrase}. {reader}'s log format changed in a way this version "
            f"cannot read. Upgrade mm, or run `mm disable-source {reader}` "
            "to stop retrying."
        )
    return f"{phrase}. A later substantive push will retry"


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
    warm_host_cache: Callable[[str], bool] | None = None,
) -> CaptureResult:
    """Capture device-stamped git, session, and host snapshot rows without writing.

    Callers retain token-cache policy in ``prepare_token_cache``; it runs after
    the bounded git walk and returns whether token data is available and, when
    it is, whether contention should warn or block. The cache context stays
    open for every Claude root, preserving the token cache's read/modify/write
    lock contract without holding it across git subprocess work. The session
    deadline begins after caller-owned preparation.

    Host capture runs last, on its own ``host_budget_ms`` deadline started
    after the session walk's ``walk_done`` snapshot, and yields at most one
    optional row. Its outcome is returned as data — the notice and the
    ``autopush`` breadcrumb are wrapper policy. ``warm_host_cache`` is the
    attended-command escape hatch: supplied by callers that may spend a
    one-off multi-second warm (interactive push, init), omitted by ``autopush``
    so an unattended hook never does. Published rows always come from a bounded
    capture, warm or not.
    """
    if root_discovery_budget_ms is None:
        root_discovery_budget_ms = (
            events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS
            if budget_ms <= events.WALK_TIME_BUDGET_AUTOPUSH_MS
            else events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS
        )
    if host_budget_ms is None:
        host_budget_ms = (
            HOST_USAGE_READ_BUDGET_AUTOPUSH_MS
            if budget_ms <= events.WALK_TIME_BUDGET_AUTOPUSH_MS
            else HOST_USAGE_READ_BUDGET_INTERACTIVE_MS
        )
    git_rows, root_discovery, walk_budget_aborts, walk_errors = _capture_git_rows(
        config,
        device_id,
        since=since,
        walk_budget_ms=git_budget_ms if git_budget_ms is not None else budget_ms,
        root_discovery_budget_ms=root_discovery_budget_ms,
    )
    _roots, discovery_errors = root_discovery

    token_cache_mode = prepare_token_cache()
    deadline = time.monotonic() + budget_ms / 1000.0
    projects: list[dict] = []
    warn_lock_unavailable = False

    def walk_sessions(token_cache_files: dict | None) -> None:
        for claude_dir in claude_paths:
            for row in events.walk_session_metadata(
                claude_dir,
                since=since,
                deadline_monotonic=deadline,
                token_cache_files=token_cache_files,
            ):
                projects.extend(row.get("projects", []))

    if token_cache_mode is None:
        walk_sessions(None)
    elif claude_paths:
        with token_usage.lock_and_get_files(token_cache_mode) as files_dict:
            # Only warn-mode contention is a caller-visible fact. A blocking
            # lock normally cannot yield None, and init has no degradation
            # breadcrumb to return even if a defensive implementation does.
            warn_lock_unavailable = token_cache_mode == "warn" and files_dict is None
            walk_sessions(files_dict)

    walk_done = time.monotonic()
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
    host_capture = _capture_host_usage(
        host_readers, deadline=time.monotonic() + host_budget_ms / 1000.0
    )
    warm_reader = next(
        (
            name
            for name, reason in host_capture.dropped
            if reason == "deadline" and name in WARMABLE_HOST_READERS
        ),
        None,
    )
    if (
        warm_reader is None
        and host_capture.reason == "deadline"
        and host_capture.reader in WARMABLE_HOST_READERS
    ):
        warm_reader = host_capture.reader
    if warm_host_cache is not None and warm_reader is not None:
        # Warm-and-retry, and ONLY after a bounded attempt has already proven
        # the cache is too cold to fit. Gating on the failure instead of on a
        # "is it cold?" predicate costs nothing on the happy path, needs no
        # persisted marker, and cannot misfire on a machine that legitimately
        # has no host data — that machine's first attempt completes, so it
        # never warms. `deadline` is also the only reason a warm can fix.
        #
        # After Track 31A a Grok deadline no longer vetoes Codex, so the
        # warmable reader may live in `dropped` rather than `reader`/`reason`.
        # Retry ONLY if the warm finished. A corpus large enough to outgrow the
        # warm's own budget keeps reporting (deadline, reader) forever, so both
        # halves of the gate above keep passing and the reader gate alone does
        # not bound the repeat cost — the warm's own outcome does.
        if warm_host_cache(warm_reader):
            host_capture = _capture_host_usage(
                host_readers, deadline=time.monotonic() + host_budget_ms / 1000.0
            )
    host_rows: list[dict] = []
    if host_capture.hosts is not None:
        host_rows.append(
            events.make_host_usage_snapshot(
                device=device_id,
                hosts=host_capture.hosts,
                token_sources=host_capture.token_sources,
                degraded_sources=tuple(name for name, _ in host_capture.dropped),
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
) -> tuple[list[dict], events.GitRootDiscovery, int, int]:
    """Discover roots and walk git. No writes, no notices, no mm-push.

    Shared by the push tail, init backfill, and recapture. ``origin`` marks
    recapture rows so the aggregator can tell them from pushes.
    """
    root_discovery = events.discover_git_roots(
        config,
        deadline_monotonic=time.monotonic() + root_discovery_budget_ms / 1000.0,
    )
    roots, _discovery_errors = root_discovery
    git_rows = events.walk_git_projects(roots, since=since, total_budget_ms=walk_budget_ms)
    for row in git_rows:
        row["device"] = device_id
        if origin is not None:
            row["origin"] = origin
    skipped = git_rows[0].get("skipped") or [] if git_rows else []
    aborts, errs = events.walk_skip_counts(skipped)
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
) -> list[str]:
    """Capture per-push fleet-retro events at the HEAD of ``_push_core``.

    Returns a list of human-readable degradation phrases for this push
    (empty when healthy). ``autopush`` turns a non-empty list into a
    ``degraded`` autorun breadcrumb (v0.12.16). The stderr notices below
    stay exactly as they are — they are the interactive signal — but they
    are NOT the load-bearing one: this function runs from ``mm autopush``,
    which fires unattended from a Claude Code hook, so its stderr reaches
    nobody. Before the return value existed, ``mm status`` reported
    ``success`` no matter how badly this degraded. CHANGELOG v0.12.13
    records the same lesson from the unpriced-model breadcrumb, which
    "fired for four unpriced models across the whole v0.12.x line and
    nobody saw it." Any new degradation detected here MUST be appended to
    the returned list as well as printed.

    See docs/invariants/events-retro.md "Events tail in `_push_core`" for
    the load-bearing invariants: head-position single-call-site (Codex C4
    — branch-fragility-free, one-push-lag-free), dry_run no-op (preview
    contract), mm-events-resolved gate (covers fresh / migrated / un-
    migrated configs uniformly, Codex C1), the independent 50ms autopush /
    100ms interactive root-discovery budget, the 250ms / 500ms walk budgets,
    and the separate 250ms / 500ms host-read budget. The "budget exceeded"
    notice reports on the session-metadata walk (the git walk self-bounds via
    its own total_budget_ms); the snapshot is taken before the self-bounded
    identity gather AND before host capture, so neither a cold 7d-TTL identity
    refresh nor a slow host store masquerades as a slow walk (v0.12.9).

    Forensic-only invariant: any failure in this block is swallowed and
    breadcrumbed via ``mm: notice:``. The push proceeds.
    """
    degradations: list[str] = []
    if dry_run:
        return degradations
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return degradations
    try:
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"
        cursor = events.resolve_push_cursor(events_dir, device_id)
        since = cursor.since
        # Session-walk budget stays the quiet/interactive pair. Git-walk
        # budget escalates independently when the cursor is old — mixing
        # the two would make a first-run 30-day cursor silently raise the
        # session-walk notice threshold (and break the AUTOPUSH=0 pin).
        budget_ms = (
            events.WALK_TIME_BUDGET_AUTOPUSH_MS if quiet else events.WALK_TIME_BUDGET_INTERACTIVE_MS
        )
        git_budget_ms = events.git_walk_budget_ms(quiet=quiet, since=since)

        claude_paths = _enabled_claude_paths(sources)

        def prepare_tail_token_cache() -> CacheLockMode:
            # Token cache wiring (v0.11.14+). This remains wrapper policy:
            # cold-cache warming and its notices must happen after the git
            # walk, just as they did before extraction.
            do_token_walk = _decide_token_walk_policy(claude_paths, quiet=quiet)
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
            root_discovery_budget_ms=(
                events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS
                if quiet
                else events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS
            ),
            host_budget_ms=(
                HOST_USAGE_READ_BUDGET_AUTOPUSH_MS
                if quiet
                else HOST_USAGE_READ_BUDGET_INTERACTIVE_MS
            ),
            # Only the attended path may spend the one-off warm. On autopush a
            # cold corpus instead converges across pushes, because an aborted
            # scan now keeps its per-file progress.
            warm_host_cache=None if quiet else _warm_host_cache_with_notice,
            prepare_token_cache=prepare_tail_token_cache,
            host_readers=_default_host_readers(
                sources, grok_consented=grok_host_usage_enabled(config)
            ),
        )

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
        events.write_push_event(
            events_dir,
            device_id,
            [*capture.git_rows, *capture.session_rows, *capture.host_rows, mm_event],
        )

        if cursor.held:
            sys.stderr.write(f"mm: notice: {_CURSOR_HOLD_DEGRADATION}\n")
            degradations.append(_CURSOR_HOLD_DEGRADATION)
        elif cursor.floored_after_incomplete:
            sys.stderr.write(f"mm: notice: {_CURSOR_COVERAGE_DEGRADATION}\n")
            degradations.append(_CURSOR_COVERAGE_DEGRADATION)
        if capture.root_discovery.exceeded:
            sys.stderr.write(f"mm: notice: {_ROOT_DISCOVERY_DEGRADATION}\n")
            degradations.append(_ROOT_DISCOVERY_DEGRADATION)
        elif capture.root_discovery.errors:
            sys.stderr.write(f"mm: notice: {_ROOT_DISCOVERY_ERROR_DEGRADATION}\n")
            degradations.append(_ROOT_DISCOVERY_ERROR_DEGRADATION)
        elif not capture.root_discovery.roots and capture.root_discovery.probers_ran:
            sys.stderr.write(f"mm: notice: {_ROOT_DISCOVERY_EMPTY_DEGRADATION}\n")
            degradations.append(_ROOT_DISCOVERY_EMPTY_DEGRADATION)
        walk_phrase = _git_walk_degradation(capture.walk_budget_aborts, capture.walk_errors)
        if walk_phrase is not None:
            sys.stderr.write(f"mm: notice: {walk_phrase}\n")
            degradations.append(walk_phrase)
        if capture.session_walk_exceeded_budget:
            sys.stderr.write("mm: notice: events tail budget exceeded\n")
            degradations.append(f"events walk exceeded its {budget_ms}ms budget")
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
                phrase = _host_skip_phrase(dropped_reader, dropped_reason)
                sys.stderr.write(f"mm: notice: {phrase}\n")
                degradations.append(phrase)
        elif not capture.host_capture.complete:
            phrase = _host_skip_phrase(capture.host_capture.reader, capture.host_capture.reason)
            sys.stderr.write(f"mm: notice: {phrase}\n")
            degradations.append(phrase)
    except Exception as e:
        sys.stderr.write(f"mm: notice: events tail failed: {type(e).__name__}: {safe_str(e)}\n")
        degradations.append(f"events tail failed ({type(e).__name__})")
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
                sys.stderr.write(
                    f"mm: notice: token cache warm at init failed: "
                    f"{type(e).__name__}: {safe_str(e)}\n"
                )
            return "block"

        capture = _capture_event_snapshots(
            config,
            claude_paths,
            device_id,
            since=since,
            budget_ms=budget_ms,
            root_discovery_budget_ms=events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS,
            host_budget_ms=HOST_USAGE_READ_BUDGET_INTERACTIVE_MS,
            # Init already warms the token cache inline and is attended; paying
            # the host warm here means the first push after install inherits a
            # hot cache, exactly as it does for tokens.
            warm_host_cache=_warm_host_cache_with_notice,
            prepare_token_cache=prepare_backfill_token_cache,
            host_readers=_default_host_readers(
                sources, grok_consented=grok_host_usage_enabled(config)
            ),
        )

        rows_to_write = [*capture.git_rows, *capture.session_rows, *capture.host_rows]
        if rows_to_write:
            events.write_push_event(events_dir, device_id, rows_to_write)

        # Warm the identity cache at init (v0.11.17, D5 from /plan-eng-review).
        # First push after init then has hot identity data and emits no
        # slow-path notice. Failure is forensic-only — backfill proceeds.
        try:
            identity.refresh_identity_cache(
                force=True,
                root_discovery=capture.root_discovery,
            )
        except Exception as e:
            sys.stderr.write(
                f"mm: notice: identity cache warm at init failed: "
                f"{type(e).__name__}: {safe_str(e)}\n"
            )

        if capture.session_walk_exceeded_budget:
            sys.stderr.write("mm: notice: events backfill budget exceeded\n")
        if capture.root_discovery.exceeded:
            sys.stderr.write(
                "mm: notice: git repository discovery hit its time budget: "
                "initial retro capture may omit repositories. Run mm diag\n"
            )
        # Init has no `mm-push` row and no autorun breadcrumb, so the notice is
        # the only surface available — and init IS attended, so it lands.
        if capture.host_capture.dropped:
            for dropped_reader, dropped_reason in capture.host_capture.dropped:
                sys.stderr.write(
                    "mm: notice: " + _host_skip_phrase(dropped_reader, dropped_reason) + "\n"
                )
        elif not capture.host_capture.complete:
            sys.stderr.write(
                "mm: notice: "
                + _host_skip_phrase(capture.host_capture.reader, capture.host_capture.reason)
                + "\n"
            )
    except Exception as e:
        sys.stderr.write(f"mm: notice: events backfill failed: {type(e).__name__}: {safe_str(e)}\n")


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


def _run_events_recapture(
    config: dict,
    sources: list[dict],
    device_id: str,
    *,
    since: datetime,
) -> list[str]:
    """Write recapture git-snapshot rows (no mm-push). Returns degradations.

    Writes nothing when mm-events is unresolved or discovery found zero
    roots — an empty snapshot would bump the aggregator's zero-capture
    note. The ordinary push path is the caller's job: writing these rows
    first makes ``has_substantive`` true with no gate edit.
    """
    degradations: list[str] = []
    try:
        prepared = _prepare_recapture(config, sources, device_id, since=since)
        if prepared is None:
            return degradations
        if not prepared.root_discovery.roots:
            return degradations
        events.write_push_event(prepared.events_dir, device_id, prepared.git_rows)
        if prepared.root_discovery.exceeded:
            degradations.append(_ROOT_DISCOVERY_DEGRADATION)
        elif prepared.root_discovery.errors:
            degradations.append(_ROOT_DISCOVERY_ERROR_DEGRADATION)
        walk_phrase = _git_walk_degradation(prepared.walk_budget_aborts, prepared.walk_errors)
        if walk_phrase is not None:
            degradations.append(walk_phrase)
    except Exception as e:
        sys.stderr.write(
            f"mm: notice: events recapture failed: {type(e).__name__}: {safe_str(e)}\n"
        )
        degradations.append(f"events recapture failed ({type(e).__name__})")
    return degradations


__all__ = [
    "_decide_token_walk_policy",
    "_enabled_claude_paths",
    "_run_events_backfill",
    "_run_events_recapture",
    "_run_events_tail",
]
