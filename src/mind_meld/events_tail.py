"""The mm-events push tail: what every push and `mm init` record for the retro.

Extracted from ``cli.py`` in Track 16A.

``_run_events_tail`` runs at the end of ``_push_core`` and writes the
``git-snapshot`` / ``sessions-snapshot`` / ``mm-push`` rows the retro-fleet
aggregator later stitches across the fleet. ``_run_events_backfill`` is its
``mm init`` counterpart. Both are FORENSIC-ONLY: they must never fail a push,
and every degradation they detect is APPENDED TO THE RETURNED LIST, not merely
printed — an unattended `mm autopush` hook's stderr goes nowhere, which is why
`autopush` writes a `degraded` breadcrumb from these reasons.

Imports nothing from ``cli`` — pinned by ``tests/test_module_boundaries.py``.

Read ``docs/invariants/events-retro.md`` BEFORE editing: the wall-clock budget
is scoped to the walk (the ``walk_done`` snapshot precedes the identity gather
on purpose), and `_decide_token_walk_policy` returning False does NOT by itself
mean degradation — it also returns False when no `claude` source is enabled,
which is a config shape, so the append is gated on `claude_paths`.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from mind_meld import __version__, events, identity, token_usage, upgrade
from mind_meld.safety import safe_str

CacheLockMode = Literal["warn", "block"] | None


@dataclass
class CaptureResult:
    """Data produced by the shared event-capture path.

    This contract intentionally excludes token-cache warming, identity work,
    notices, terminal events, and writes. Push and init have different policy
    for each of those concerns; sharing them here would reintroduce the
    coupling this helper is meant to remove.
    """

    git_rows: list[dict]
    session_rows: list[dict]
    discovery_errors: list[str]
    walk_exceeded_budget: bool
    warn_lock_unavailable: bool
    token_cache_requested: bool


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


def _capture_event_snapshots(
    config: dict,
    claude_paths: list[Path],
    device_id: str,
    *,
    since: datetime,
    budget_ms: int,
    prepare_token_cache: Callable[[], CacheLockMode],
) -> CaptureResult:
    """Capture device-stamped git and session snapshot rows without writing.

    Callers retain token-cache policy in ``prepare_token_cache``; it runs after
    the bounded git walk and returns whether token data is available and, when
    it is, whether contention should warn or block. The cache context stays
    open for every Claude root, preserving the token cache's read/modify/write
    lock contract without holding it across git subprocess work. The session
    deadline begins after caller-owned preparation.
    """
    roots, discovery_errors = events.discover_git_roots(config)
    git_rows = events.walk_git_projects(roots, since=since, total_budget_ms=budget_ms)
    for row in git_rows:
        row["device"] = device_id

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

    return CaptureResult(
        git_rows=git_rows,
        session_rows=session_rows,
        discovery_errors=discovery_errors,
        walk_exceeded_budget=walk_done > deadline,
        warn_lock_unavailable=warn_lock_unavailable,
        token_cache_requested=token_cache_mode is not None,
    )


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
    migrated configs uniformly, Codex C1), and the autopush 250ms /
    interactive 500ms wall-clock budget. The "budget exceeded" notice
    reports on the session-metadata walk (the git walk self-bounds via its
    own total_budget_ms); the snapshot is taken before the self-bounded
    identity gather so a cold 7d-TTL identity refresh no longer masquerades
    as a slow walk (v0.12.9).

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
        budget_ms = (
            events.WALK_TIME_BUDGET_AUTOPUSH_MS if quiet else events.WALK_TIME_BUDGET_INTERACTIVE_MS
        )
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"
        since = events.last_push_ts(events_dir, device_id)

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
            prepare_token_cache=prepare_tail_token_cache,
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
        local_emails = identity.gather_local_identities(allow_refresh=True)
        mm_event = events.make_mm_push_event(
            device=device_id,
            mm_version=__version__,
            sources=source_names,
            discovery_errors=capture.discovery_errors,
            local_emails=local_emails,
        )
        # CT-4 invariant: mm-push event LAST so a partial write doesn't
        # advance the next-push cursor.
        events.write_push_event(
            events_dir,
            device_id,
            [*capture.git_rows, *capture.session_rows, mm_event],
        )

        if capture.walk_exceeded_budget:
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
    except Exception as e:
        sys.stderr.write(f"mm: notice: events tail failed: {type(e).__name__}: {safe_str(e)}\n")
        degradations.append(f"events tail failed ({type(e).__name__})")
    return degradations


def _run_events_backfill(
    config: dict,
    sources: list[dict],
    device_id: str,
) -> None:
    """Init-time backfill of git+sessions events for the past 30 days.

    Mirrors ``_run_events_tail`` but writes only ``git-snapshot`` and
    ``sessions-snapshot`` rows — NO ``mm-push`` row. Two consequences:

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
            prepare_token_cache=prepare_backfill_token_cache,
        )

        rows_to_write = [*capture.git_rows, *capture.session_rows]
        if rows_to_write:
            events.write_push_event(events_dir, device_id, rows_to_write)

        # Warm the identity cache at init (v0.11.17, D5 from /plan-eng-review).
        # First push after init then has hot identity data and emits no
        # slow-path notice. Failure is forensic-only — backfill proceeds.
        try:
            identity.refresh_identity_cache(force=True)
        except Exception as e:
            sys.stderr.write(
                f"mm: notice: identity cache warm at init failed: "
                f"{type(e).__name__}: {safe_str(e)}\n"
            )

        if capture.walk_exceeded_budget:
            sys.stderr.write("mm: notice: events backfill budget exceeded\n")
    except Exception as e:
        sys.stderr.write(f"mm: notice: events backfill failed: {type(e).__name__}: {safe_str(e)}\n")


__all__ = [
    "_decide_token_walk_policy",
    "_enabled_claude_paths",
    "_run_events_backfill",
    "_run_events_tail",
]
