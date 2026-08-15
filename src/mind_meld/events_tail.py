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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mind_meld import __version__, events, identity, token_usage, upgrade
from mind_meld.safety import safe_str


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

    See CLAUDE.md "Events tail in _push_core (load-bearing, v0.10.3)" for
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
        deadline = time.monotonic() + budget_ms / 1000.0
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"

        roots, errs = events.discover_git_roots(config)
        since = events.last_push_ts(events_dir, device_id)

        g_rows = events.walk_git_projects(roots, since=since, total_budget_ms=budget_ms)
        for r in g_rows:
            r["device"] = device_id

        claude_paths = _enabled_claude_paths(sources)
        # Token cache wiring (v0.11.14+).
        # Step 1: decide whether to aggregate tokens this push (handles cold-
        # cache warm internally). Returns False on autopush + cold + no
        # detected upgrade transition — caller skips the token aggregation.
        do_token_walk = _decide_token_walk_policy(claude_paths, quiet=quiet)
        # Warm may have consumed several seconds. Refresh the deadline so
        # the session-metadata walk gets its full advertised budget instead
        # of an already-expired one (Codex outside-voice review caught this
        # — pre-fix, first interactive push / upgrade autopush could emit
        # an empty `projects: []` snapshot when warm ate the original
        # deadline).
        deadline = time.monotonic() + budget_ms / 1000.0

        agg_projects: list[dict] = []
        if do_token_walk:
            # Step 2: hold the token cache flock across the walk so
            # walk_session_metadata's per-file mutations to files dict are
            # captured atomically. "warn" mode under autopush degrades
            # gracefully on contention (`files is None`, no token
            # aggregation this push); "block" under interactive (user is
            # waiting anyway). Cache-shape invariants (version + files
            # isinstance) are owned by token_usage.lock_and_get_files.
            mode = "warn" if quiet else "block"
            with token_usage.lock_and_get_files(mode) as files_dict:
                if files_dict is None:
                    # Warn-mode contention. `do_token_walk` stays True, so
                    # the gate below cannot see this — but the user-visible
                    # outcome is IDENTICAL to the cold-cache case: every
                    # project ships without tokens_by_day or skills_by_day,
                    # and latest-snapshot-wins then replaces the prior
                    # complete data with it. Caught by Codex + the
                    # maintainability specialist during /review; the
                    # invariant this function documents says every
                    # degradation MUST reach the returned list, and this
                    # one previously reached only a stderr warning.
                    degradations.append("token cache was locked, so tokens and skills are missing")
                for claude_dir in claude_paths:
                    for row in events.walk_session_metadata(
                        claude_dir,
                        since=since,
                        deadline_monotonic=deadline,
                        token_cache_files=files_dict,
                    ):
                        agg_projects.extend(row.get("projects", []))
        else:
            for claude_dir in claude_paths:
                for row in events.walk_session_metadata(
                    claude_dir,
                    since=since,
                    deadline_monotonic=deadline,
                    token_cache_files=None,
                ):
                    agg_projects.extend(row.get("projects", []))
        # Budget check covers the session-metadata WALK — snapshot the clock
        # HERE, before the self-bounded identity gather (≤10s worst case, 7d
        # TTL) and the event write. (The git walk above self-bounds via its
        # own total_budget_ms; this deadline was reset after it.) Pre-v0.12.9
        # the check sat after gather_local_identities, so a cold identity
        # refresh masqueraded as "events tail budget exceeded" even when the
        # walk finished in ~200ms. The gather announces itself separately via
        # `refreshing identity cache (one-off)`. See events-retro.md inv. 4.
        walk_done = time.monotonic()
        s_rows: list[dict] = []
        if claude_paths:
            s_rows.append(
                {
                    "v": events.EVENTS_SCHEMA_VERSION,
                    "type": "sessions-snapshot",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "device": device_id,
                    "projects": agg_projects,
                }
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
            discovery_errors=errs,
            local_emails=local_emails,
        )
        # CT-4 invariant: mm-push event LAST so a partial write doesn't
        # advance the next-push cursor.
        events.write_push_event(events_dir, device_id, [*g_rows, *s_rows, mm_event])

        if walk_done > deadline:
            sys.stderr.write("mm: notice: events tail budget exceeded\n")
            degradations.append(f"events walk exceeded its {budget_ms}ms budget")
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
        if claude_paths and not do_token_walk:
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
        deadline = time.monotonic() + budget_ms / 1000.0
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"

        # Explicit 30-day window. last_push_ts() returns the same value
        # on first run, but stating intent at the call site makes the
        # backfill semantics legible without chasing a default.
        since = datetime.now(timezone.utc) - timedelta(days=events.INITIAL_CURSOR_LOOKBACK_DAYS)

        roots, _errs = events.discover_git_roots(config)
        g_rows = events.walk_git_projects(roots, since=since, total_budget_ms=budget_ms)
        for r in g_rows:
            r["device"] = device_id

        claude_paths = _enabled_claude_paths(sources)

        # Warm the token cache inline at init (v0.11.14+). One-time cost
        # at init time — kb already accepts init takes a few seconds.
        # Subsequent pushes inherit a warm cache.
        if claude_paths:
            try:
                token_usage.warm_token_cache_inline(claude_paths)
            except Exception as e:
                sys.stderr.write(
                    f"mm: notice: token cache warm at init failed: "
                    f"{type(e).__name__}: {safe_str(e)}\n"
                )

        # Refresh deadline after warm — the warm can spend ~5s, which
        # would otherwise leave an already-expired deadline for the
        # session-metadata walk and produce an empty `projects: []`
        # backfill on fresh installs (Codex outside-voice review caught
        # this; matches the same fix in `_run_events_tail`).
        deadline = time.monotonic() + budget_ms / 1000.0

        agg_projects: list[dict] = []
        # Hold the token cache lock across the walk so per-jsonl mutations
        # persist as part of the same R/M/W. Init is interactive, so use
        # blocking mode. Cache-shape invariants are owned by
        # token_usage.lock_and_get_files.
        if claude_paths:
            with token_usage.lock_and_get_files("block") as files_dict:
                for claude_dir in claude_paths:
                    for row in events.walk_session_metadata(
                        claude_dir,
                        since=since,
                        deadline_monotonic=deadline,
                        token_cache_files=files_dict,
                    ):
                        agg_projects.extend(row.get("projects", []))
        # Budget check covers the session-metadata WALK (mirrors
        # _run_events_tail; the git walk above self-bounds via total_budget_ms)
        # — snapshot the clock HERE, before the deliberate
        # identity.refresh_identity_cache(force=True) warm below. That refresh
        # ALWAYS runs at init and can spend ~10s on a cold gather; counting it
        # against the walk budget made "events backfill budget exceeded" fire
        # on essentially every init. See docs/invariants/events-retro.md inv. 4.
        walk_done = time.monotonic()
        s_rows: list[dict] = []
        if claude_paths:
            s_rows.append(
                {
                    "v": events.EVENTS_SCHEMA_VERSION,
                    "type": "sessions-snapshot",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "device": device_id,
                    "projects": agg_projects,
                }
            )

        rows_to_write = [*g_rows, *s_rows]
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

        if walk_done > deadline:
            sys.stderr.write("mm: notice: events backfill budget exceeded\n")
    except Exception as e:
        sys.stderr.write(f"mm: notice: events backfill failed: {type(e).__name__}: {safe_str(e)}\n")


__all__ = [
    "_decide_token_walk_policy",
    "_enabled_claude_paths",
    "_run_events_backfill",
    "_run_events_tail",
]
