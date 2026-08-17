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
from datetime import datetime, timezone

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
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda _c, **_kwargs: _mm_events.GitRootDiscovery((), (), False),
    )
    monkeypatch.setattr(
        _mm_events,
        "walk_git_projects",
        lambda roots, since, total_budget_ms: [],
    )


class TestSharedCapturePath:
    def test_captures_all_claude_roots_under_one_post_git_lock(self, tmp_path, monkeypatch):
        """The shared core is data-only and preserves the cache lock's
        lifetime: discover + git complete first, then one lock covers every
        Claude root's mutable token-cache walk."""
        claude_a = tmp_path / "claude-a"
        claude_b = tmp_path / "claude-b"
        claude_a.mkdir()
        claude_b.mkdir()
        trace: list[str] = []
        cache_files: dict = {}

        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda _config, **_kwargs: (
                trace.append("discover")
                or _mm_events.GitRootDiscovery((), ("probe failed",), False)
            ),
        )

        def fake_git_walk(roots, since, total_budget_ms):
            trace.append("git")
            return [
                {
                    "v": _mm_events.EVENTS_SCHEMA_VERSION,
                    "type": "git-snapshot",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "device": "",
                    "projects": [],
                    "skipped": [],
                }
            ]

        monkeypatch.setattr(_mm_events, "walk_git_projects", fake_git_walk)

        @contextmanager
        def fake_lock(mode):
            assert mode == "block"
            trace.append("lock-enter")
            yield cache_files
            trace.append("lock-exit")

        monkeypatch.setattr(_mm_token_usage, "lock_and_get_files", fake_lock)

        def fake_session_walk(claude_dir, since, **kwargs):
            assert kwargs["token_cache_files"] is cache_files
            trace.append(f"session-{claude_dir.name}")
            return [{"projects": [{"name": claude_dir.name}]}]

        monkeypatch.setattr(_mm_events, "walk_session_metadata", fake_session_walk)

        def should_not_run(*args, **kwargs):
            raise AssertionError("the capture core must not write or gather identities")

        monkeypatch.setattr(_mm_events, "write_push_event", should_not_run)
        monkeypatch.setattr(_mm_identity, "gather_local_identities", should_not_run)
        monkeypatch.setattr(_mm_identity, "refresh_identity_cache", should_not_run)

        result = events_tail._capture_event_snapshots(
            {},
            [claude_a, claude_b],
            "dev-a",
            since=datetime.now(timezone.utc),
            budget_ms=500,
            prepare_token_cache=lambda: trace.append("prepare") or "block",
            # No host sources enabled → no host readers consented to, so the
            # sweep touches nothing and this stays a git+session pin.
            host_readers=(),
        )

        assert trace == [
            "discover",
            "git",
            "prepare",
            "lock-enter",
            "session-claude-a",
            "session-claude-b",
            "lock-exit",
        ]
        assert result.discovery_errors == ["probe failed"]
        assert result.git_rows[0]["device"] == "dev-a"
        assert result.session_rows[0]["device"] == "dev-a"
        assert result.session_rows[0]["projects"] == [
            {"name": "claude-a"},
            {"name": "claude-b"},
        ]


class TestEventsTailBudgetScope:
    def test_slow_token_warm_does_not_consume_session_budget(self, tmp_path, monkeypatch, capsys):
        """Interactive cache warm can take seconds, but the advertised
        session-walk budget starts after that caller-owned preparation."""
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        sources = _make_sources(events_root, claude_dir)
        config = {"sync": {"sources": sources}}

        _stub_fast_walks(monkeypatch)
        monkeypatch.setattr(_mm_events, "WALK_TIME_BUDGET_INTERACTIVE_MS", 10)
        monkeypatch.setattr(_mm_events, "walk_session_metadata", lambda *args, **kwargs: [])
        monkeypatch.setattr(_mm_token_usage, "is_cache_cold", lambda: True)

        def slow_warm(paths):
            time.sleep(0.12)

        monkeypatch.setattr(_mm_token_usage, "warm_token_cache_inline", slow_warm)

        @contextmanager
        def fake_lock(mode):
            assert mode == "block"
            yield {}

        monkeypatch.setattr(_mm_token_usage, "lock_and_get_files", fake_lock)
        monkeypatch.setattr(
            _mm_identity,
            "gather_local_identities",
            lambda *, allow_refresh=True, root_discovery=None: [],
        )

        events_tail._run_events_tail(config, sources, "dev-a", dry_run=False, quiet=False)

        err = capsys.readouterr().err
        assert "events tail failed" not in err
        assert "events tail budget exceeded" not in err

    def test_slow_identity_gather_does_not_trip_notice(self, tmp_path, monkeypatch, capsys):
        """A cold identity refresh that outlasts the interactive budget must
        NOT emit `events tail budget exceeded` — the walk itself was fast.
        Pre-v0.12.9 the post-gather `time.monotonic()` check fired here."""
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root, claude_dir=None)  # no claude → no session walk
        config = {"sync": {"sources": sources}}

        _stub_fast_walks(monkeypatch)

        def slow_gather(*, allow_refresh=True, root_discovery=None):
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
            _mm_identity,
            "gather_local_identities",
            lambda *, allow_refresh=True, root_discovery=None: [],
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

        def slow_refresh(*, force=False, root_discovery=None):
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
        monkeypatch.setattr(
            _mm_identity,
            "refresh_identity_cache",
            lambda *, force=False, root_discovery=None: [],
        )

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


class TestRootDiscoveryHandoffAndDegradation:
    def test_tail_passes_exact_discovery_to_identity_once(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root)
        config = {"sync": {"sources": sources}}
        discovery = _mm_events.GitRootDiscovery((), (), False)
        received: list[object] = []
        writes: list[list[dict]] = []

        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda _config, **_kwargs: discovery,
        )
        monkeypatch.setattr(_mm_events, "walk_git_projects", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            _mm_identity,
            "gather_local_identities",
            lambda *, allow_refresh=True, root_discovery=None: (
                received.append(root_discovery) or []
            ),
        )
        monkeypatch.setattr(
            _mm_events,
            "write_push_event",
            lambda _events_dir, _device, rows: writes.append(rows),
        )

        assert (
            events_tail._run_events_tail(config, sources, "dev-a", dry_run=False, quiet=False) == []
        )
        assert received == [discovery]
        assert len(writes) == 1

    def test_partial_tail_is_forensic_and_status_visible(self, tmp_path, monkeypatch, capsys):
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root)
        config = {"sync": {"sources": sources}}
        discovery = _mm_events.GitRootDiscovery(
            (), (_mm_events.GIT_ROOT_DISCOVERY_BUDGET_ERROR,), True
        )
        writes: list[list[dict]] = []

        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda _config, **_kwargs: discovery,
        )
        monkeypatch.setattr(_mm_events, "walk_git_projects", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            _mm_identity,
            "gather_local_identities",
            lambda *, allow_refresh=True, root_discovery=None: [],
        )
        monkeypatch.setattr(
            _mm_events,
            "write_push_event",
            lambda _events_dir, _device, rows: writes.append(rows),
        )

        degradations = events_tail._run_events_tail(
            config, sources, "dev-a", dry_run=False, quiet=True
        )
        assert degradations == [events_tail._ROOT_DISCOVERY_DEGRADATION]
        assert "mm: notice: " + events_tail._ROOT_DISCOVERY_DEGRADATION in capsys.readouterr().err
        assert writes[0][-1]["discovery_errors"] == [_mm_events.GIT_ROOT_DISCOVERY_BUDGET_ERROR]

    def test_partial_backfill_notices_without_creating_mm_push(self, tmp_path, monkeypatch, capsys):
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root)
        config = {"sync": {"sources": sources}}
        discovery = _mm_events.GitRootDiscovery(
            (), (_mm_events.GIT_ROOT_DISCOVERY_BUDGET_ERROR,), True
        )
        writes: list[list[dict]] = []
        received: list[object] = []

        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda _config, **_kwargs: discovery,
        )
        monkeypatch.setattr(_mm_events, "walk_git_projects", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            _mm_identity,
            "refresh_identity_cache",
            lambda *, force=False, root_discovery=None: received.append(root_discovery) or [],
        )
        monkeypatch.setattr(
            _mm_events,
            "write_push_event",
            lambda _events_dir, _device, rows: writes.append(rows),
        )

        events_tail._run_events_backfill(config, sources, "dev-a")
        assert received == [discovery]
        # The healthy (completed-empty) host row is the only thing to write
        # here — git returned nothing and there is no claude source. The
        # load-bearing half of this pin is the absence of `mm-push`, not the
        # absence of writes.
        assert [row["type"] for rows in writes for row in rows] == ["host-usage-snapshot"]
        assert "initial retro capture may omit repositories" in capsys.readouterr().err
