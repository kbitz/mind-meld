"""The events-tail/backfill wall-clock budget is WALK-scoped (v0.12.9).

Regression pin for the misleading `events tail budget exceeded` notice: the
budget bounds — and the notice reports on — the git+session walk only. The
self-bounded identity gather (cold path ~10s, 7d TTL) runs AFTER the
`walk_done` snapshot, so a routine cold identity refresh must NOT trip the
walk-budget notice. See docs/invariants/events-retro.md invariant 4.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from mind_meld import events as _mm_events
from mind_meld import events_tail
from mind_meld import identity as _mm_identity
from mind_meld import token_usage as _mm_token_usage


def _make_sources(events_root, claude_dir=None) -> list[dict]:
    sources: list[dict] = []
    if claude_dir is not None:
        sources.append({"name": "claude", "path": str(claude_dir), "type": "claude"})
    sources.append(
        {
            "name": "mm-events",
            "path": str(events_root),
            "type": "generic",
            "include_dirs": ["events"],
            "exclude_patterns": [],
        }
    )
    return sources


def _stub_fast_walks(monkeypatch):
    """git discovery + git walk return instantly with nothing."""
    monkeypatch.setattr(_mm_events, "discover_git_roots", lambda _c: ([], []))
    monkeypatch.setattr(
        _mm_events,
        "walk_git_projects",
        lambda roots, since, total_budget_ms: [],
    )


class TestEventsTailBudgetScope:
    def test_slow_identity_gather_does_not_trip_notice(self, tmp_path, monkeypatch, capsys):
        """A cold identity refresh that outlasts the interactive budget must
        NOT emit `events tail budget exceeded` — the walk itself was fast.
        Pre-v0.12.9 the post-gather `time.monotonic()` check fired here."""
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root, claude_dir=None)  # no claude → no session walk
        config = {"sync": {"sources": sources}}

        _stub_fast_walks(monkeypatch)

        def slow_gather(*, allow_refresh=True):
            # 0.7s > the 500ms interactive budget. Happens AFTER walk_done.
            time.sleep(0.7)
            return []

        monkeypatch.setattr(_mm_identity, "gather_local_identities", slow_gather)

        events_tail._run_events_tail(config, sources, "dev-a", dry_run=False, quiet=False)

        err = capsys.readouterr().err
        # Guard against the notice being absent for the WRONG reason: an early
        # exception would divert to the `events tail failed` breadcrumb and
        # also leave the budget notice absent. Assert the function ran clean.
        assert "events tail failed" not in err
        assert "events tail budget exceeded" not in err

    def test_slow_walk_still_trips_notice(self, tmp_path, monkeypatch, capsys):
        """The notice MUST still fire when the session walk genuinely overruns
        — proves we narrowed the check's scope, not deleted it."""
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        sources = _make_sources(events_root, claude_dir)
        config = {"sync": {"sources": sources}}

        _stub_fast_walks(monkeypatch)
        monkeypatch.setattr(_mm_events, "WALK_TIME_BUDGET_INTERACTIVE_MS", 10)
        # else-branch (no flock); identity gather instant so only the walk is slow.
        monkeypatch.setattr(events_tail, "_decide_token_walk_policy", lambda paths, *, quiet: False)
        monkeypatch.setattr(
            _mm_identity, "gather_local_identities", lambda *, allow_refresh=True: []
        )

        def slow_walk(claude_dir, **kwargs):
            time.sleep(0.12)  # >> 10ms budget
            return
            yield  # pragma: no cover — makes this a generator

        monkeypatch.setattr(_mm_events, "walk_session_metadata", slow_walk)

        events_tail._run_events_tail(config, sources, "dev-a", dry_run=False, quiet=False)

        assert "events tail budget exceeded" in capsys.readouterr().err


class TestEventsBackfillBudgetScope:
    def test_slow_identity_refresh_does_not_trip_notice(self, tmp_path, monkeypatch, capsys):
        """Backfill's `refresh_identity_cache(force=True)` ALWAYS runs and can
        spend ~10s cold — it must NOT trip `events backfill budget exceeded`."""
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root, claude_dir=None)
        config = {"sync": {"sources": sources}}

        _stub_fast_walks(monkeypatch)

        def slow_refresh(*, force=False):
            time.sleep(0.7)  # > 500ms backfill budget
            return []

        monkeypatch.setattr(_mm_identity, "refresh_identity_cache", slow_refresh)

        events_tail._run_events_backfill(config, sources, "dev-a")

        err = capsys.readouterr().err
        # Same wrong-reason guard as the tail negative test (see above).
        assert "events backfill failed" not in err
        assert "events backfill budget exceeded" not in err

    def test_slow_walk_still_trips_notice(self, tmp_path, monkeypatch, capsys):
        """Backfill notice still fires on a genuine session-walk overrun."""
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        sources = _make_sources(events_root, claude_dir)
        config = {"sync": {"sources": sources}}

        _stub_fast_walks(monkeypatch)
        monkeypatch.setattr(_mm_events, "WALK_TIME_BUDGET_INTERACTIVE_MS", 10)
        monkeypatch.setattr(_mm_token_usage, "warm_token_cache_inline", lambda paths: None)
        monkeypatch.setattr(_mm_identity, "refresh_identity_cache", lambda *, force=False: [])

        @contextmanager
        def fake_lock(mode):
            yield {}

        monkeypatch.setattr(_mm_token_usage, "lock_and_get_files", fake_lock)

        def slow_walk(claude_dir, **kwargs):
            time.sleep(0.12)
            return
            yield  # pragma: no cover

        monkeypatch.setattr(_mm_events, "walk_session_metadata", slow_walk)

        events_tail._run_events_backfill(config, sources, "dev-a")

        assert "events backfill budget exceeded" in capsys.readouterr().err
