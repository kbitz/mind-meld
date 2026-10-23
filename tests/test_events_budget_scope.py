"""The events-tail/backfill wall-clock budget is WALK-scoped (v0.12.9).

Regression pin for the misleading `events tail budget exceeded` notice: the
budget bounds — and the notice reports on — the git+session walk only. The
self-bounded identity gather (cold path ~10s, 7d TTL) runs AFTER the
`walk_done` snapshot, so a routine cold identity refresh must NOT trip the
walk-budget notice. See docs/invariants/events-retro.md invariant 4.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

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
        lambda roots, since, total_budget_ms, **kwargs: [],
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

        def fake_git_walk(roots, since, total_budget_ms, **kwargs):
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
    def test_old_cursor_escalates_only_git_budget_on_autopush(self, tmp_path, monkeypatch):
        """An old cursor raises the git budget without widening the session budget."""
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root)
        config = {"sync": {"sources": sources}}
        since = datetime.now(timezone.utc) - timedelta(days=2)
        captured_kwargs: dict = {}
        result = events_tail.CaptureResult(
            git_rows=[],
            session_rows=[],
            host_rows=[],
            host_capture=events_tail.HostUsageCapture({}),
            root_discovery=_mm_events.GitRootDiscovery((), (), False),
            discovery_errors=[],
            session_walk_exceeded_budget=False,
            warn_lock_unavailable=False,
            token_cache_requested=False,
        )
        monkeypatch.setattr(
            _mm_events,
            "resolve_push_cursor",
            lambda *_args, **_kwargs: _mm_events.CursorResolution(since=since),
        )
        monkeypatch.setattr(
            events_tail,
            "_capture_event_snapshots",
            lambda *_args, **kwargs: captured_kwargs.update(kwargs) or result,
        )
        monkeypatch.setattr(
            _mm_identity,
            "gather_local_identities",
            lambda *, allow_refresh=True, root_discovery=None: [],
        )
        monkeypatch.setattr(_mm_events, "write_push_event", lambda *_args, **_kwargs: None)

        assert (
            events_tail._run_events_tail(config, sources, "dev-a", dry_run=False, quiet=True) == []
        )
        assert captured_kwargs["budget_ms"] == _mm_events.WALK_TIME_BUDGET_AUTOPUSH_MS
        assert captured_kwargs["git_budget_ms"] == _mm_events.WALK_TIME_BUDGET_INTERACTIVE_MS

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
            lambda _events_dir, _device, rows, **kwargs: writes.append(rows),
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
            lambda _events_dir, _device, rows, **kwargs: writes.append(rows),
        )

        degradations = events_tail._run_events_tail(
            config, sources, "dev-a", dry_run=False, quiet=True
        )
        assert degradations == [events_tail._ROOT_DISCOVERY_DEGRADATION]
        assert "mm: notice: " + events_tail._ROOT_DISCOVERY_DEGRADATION in capsys.readouterr().err
        mm_push = writes[0][-1]
        assert mm_push["type"] == "mm-push"
        assert mm_push["discovery_errors"] == [_mm_events.GIT_ROOT_DISCOVERY_BUDGET_ERROR]
        assert mm_push["git_capture"]["discovery"] == "partial"
        assert mm_push["git_capture"]["walk_budget_aborts"] == 0
        assert mm_push["git_capture"]["walk_errors"] == 0
        assert datetime.fromisoformat(mm_push["git_capture"]["since"]).tzinfo is not None

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
            lambda _events_dir, _device, rows, **kwargs: writes.append(rows),
        )

        events_tail._run_events_backfill(config, sources, "dev-a")
        assert received == [discovery]
        # The healthy (completed-empty) host row is the only thing to write
        # here — git returned nothing and there is no claude source. The
        # load-bearing half of this pin is the absence of `mm-push`, not the
        # absence of writes.
        assert [row["type"] for rows in writes for row in rows] == ["host-usage-snapshot"]
        assert "initial retro capture may omit repositories" in capsys.readouterr().err


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    "site,interactive",
    [
        ("snapshot", False),
        ("snapshot", True),
        ("tail", False),
        ("tail", True),
        ("backfill", True),
        ("requested", True),
    ],
)
def test_all_host_budget_sites_use_resolver(site, interactive, configured, tmp_path, monkeypatch):
    from mind_meld import cli

    sources = _make_sources(tmp_path / "events")
    config = {"device": {"id": "dev-a"}, "sync": {"sources": sources}}
    if configured:
        config["retro"] = {
            "host_usage_autopush_budget_ms": 350,
            "host_usage_interactive_budget_ms": 700,
        }
    expected = (700 if interactive else 350) if configured else (500 if interactive else 250)
    assert events_tail.resolve_host_read_budget(config, interactive=interactive) == (
        expected,
        "config" if configured else "default",
    )
    seen = []

    def capture(*args, **kwargs):
        seen.append(kwargs["host_budget_ms"])
        return events_tail.HostUsageCapture(None), []

    monkeypatch.setattr(events_tail, "_capture_host_snapshot", capture)
    _stub_fast_walks(monkeypatch)
    monkeypatch.setattr(_mm_identity, "gather_local_identities", lambda **kwargs: [])
    monkeypatch.setattr(_mm_identity, "refresh_identity_cache", lambda **kwargs: [])
    if site == "snapshot":
        events_tail._capture_event_snapshots(
            config,
            [],
            "dev-a",
            since=datetime.now(timezone.utc),
            budget_ms=1000 if interactive else 100,
            prepare_token_cache=lambda: None,
            host_readers=(),
        )
    elif site == "tail":
        events_tail._run_events_tail(config, sources, "dev-a", dry_run=False, quiet=not interactive)
    elif site == "backfill":
        events_tail._run_events_backfill(config, sources, "dev-a")
    else:
        monkeypatch.setattr(
            events_tail, "_default_host_readers", lambda *a, **kw: (("codex", None),)
        )
        outcome = cli._capture_attended_usage(config, sources, sources, verbose=False)
        assert outcome.appended is None
        assert outcome.kind == "no-row"
    assert seen == [expected]


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=0), "0 s ago"),
        (timedelta(seconds=59), "59 s ago"),
        (timedelta(seconds=60), "1 m ago"),
        (timedelta(minutes=59, seconds=59), "59 m ago"),
        (timedelta(hours=1), "1 h ago"),
        (timedelta(hours=23, minutes=59, seconds=59), "23 h ago"),
        (timedelta(days=1), "1 d ago"),
        (timedelta(days=5, hours=2), "5 d ago"),
    ],
)
def test_host_read_age_boundaries(delta, expected):
    """Only the hours branch had indirect coverage (via a diag integration
    test's "3 h ago" assertion); the day/minute/second branches and the exact
    86400/3600/60-second boundaries had none."""
    when = datetime.now(timezone.utc) - delta
    assert events_tail.host_read_age(when.isoformat(timespec="seconds")) == expected


def test_whole_walk_failure_preserves_collected_and_marks_outstanding(monkeypatch, tmp_path):
    from concurrent.futures import Future
    from datetime import datetime, timezone

    from mind_meld import events

    roots = [tmp_path / "one", tmp_path / "two"]
    futures = []

    class Executor:
        def __init__(self, **kwargs):
            pass

        def submit(self, fn, root, *args):
            future = Future()
            future.set_result(({"path": str(root)}, None))
            futures.append(future)
            return future

        def shutdown(self, **kwargs):
            pass

    def pump(*args, **kwargs):
        yield futures[0]
        raise RuntimeError("private sentinel")

    monkeypatch.setattr(events, "ThreadPoolExecutor", Executor)
    monkeypatch.setattr(events, "as_completed", pump)
    notices = []

    def broken_notice(message, degraded):
        notices.append((message, degraded))
        raise OSError("closed pipe")

    row = events.walk_git_projects(
        roots, datetime.now(timezone.utc), 500, diagnostic=broken_notice
    )[0]
    assert row["projects"] == [{"path": str(roots[0])}]
    assert row["skipped"] == [{"path": str(roots[1]), "reason": events.WALK_SKIP_RAISED}]
    assert events.walk_skip_counts(row["skipped"]) == (0, 1)
    assert notices[0][1] is False  # walk_errors (WALK_SKIP_RAISED) carries the loss
    assert "private sentinel" not in notices[0][0]


def test_diagnostic_retention_is_bounded_and_counts_repetitions():
    from mind_meld import events_tail

    details = events_tail.Degradations()
    for _ in range(10000):
        details.append("token walk skipped, so tokens and skills are missing")
    for n in range(100):
        details.append(f"unknown {n}")
    assert len(details) == 8
    assert details.total == 10100
    assert details.omitted == 10092


def test_diagnostic_candidates_drop_escapes_and_overlong_text():
    # Value: protects=candidates stay sanitized, at most 384 characters, with honest extend counts;
    # fails_when=an escape or a 385-character string is stored, or extend counts an omission twice;
    # why_new=the retention test uses only short plain duplicates;
    # seam=none; no_claim=which status line shows a candidate
    details = events_tail.Degradations()
    details.append("kept\x1b[2J")
    details.append("y" * 385)
    assert details == ["kept"]
    assert "\x1b" not in details[0]
    assert details.total == 2
    assert details.omitted == 1
    source = events_tail.Degradations()
    source.append("alpha")
    source.append("alpha")
    source.append("beta")
    merged = events_tail.Degradations()
    merged.extend(source)
    assert list(merged) == ["alpha", "beta"]
    assert merged.total == source.total == 3
    assert merged.omitted == source.omitted == 1


def _cursor_repo(tmp_path, name):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def _write_git_cursor_cache(cache):
    from mind_meld import config, lockedjson

    with lockedjson.locked_json_rmw(config.GIT_CAPTURE_CACHE_PATH) as state:
        state.data.update(_mm_events._default_git_capture_cache())
        state.data.update(cache)


def test_repo_cursor_walk_orders_stalest_and_preserves_read_only_cache(tmp_path, monkeypatch):
    roots = [_cursor_repo(tmp_path, name) for name in ("newest", "tie-b", "tie-a")]
    since = datetime.now(timezone.utc) - timedelta(days=1)
    older = since - timedelta(days=2)
    _write_git_cursor_cache(
        {
            "repo_cursors": {
                str(roots[0]): since.isoformat(),
                **{str(root): older.isoformat() for root in roots[1:]},
            },
        }
    )
    before = _mm_events.read_git_capture_cache()
    calls = []

    def walk(root, cutoff, timeout):
        calls.append((root, cutoff))
        return {"local_path": str(root), "commits": []}, None

    monkeypatch.setattr(_mm_events, "MAX_GIT_WORKERS", 1)
    monkeypatch.setattr(_mm_events, "_walk_one_repo", walk)
    _mm_events.walk_git_projects(roots, since, 1000, use_repo_cursors=True)
    assert calls == [
        (roots[2], older.isoformat()),
        (roots[1], older.isoformat()),
        (roots[0], since.isoformat()),
    ]
    assert _mm_events.read_git_capture_cache() == before
    assert _mm_events.effective_capture_since([], since) == older
    calls.clear()
    _mm_events.walk_git_projects(roots, since, 1000, use_repo_cursors=False)
    assert {cutoff for root, cutoff in calls} == {since.isoformat()}


def test_repo_cursor_commit_keeps_failure_interval_and_completes_empty_repo(tmp_path):
    complete, empty, failed, gone = [
        _cursor_repo(tmp_path, name) for name in ("complete", "empty", "failed", "gone")
    ]
    since = datetime.now(timezone.utc) - timedelta(days=3)
    old = since - timedelta(days=1)
    cursor = datetime.now(timezone.utc) - timedelta(seconds=1)
    recorded = datetime.now(timezone.utc)
    _write_git_cursor_cache(
        {
            "repo_cursors": {str(failed): old.isoformat(), str(gone): old.isoformat()},
            "projects": {"future": {"git_root": str(complete), "track83": {"keep": True}}},
            "future": "preserved",
        }
    )
    (gone / ".git" / "HEAD").unlink()
    rows = [
        {
            "projects": [{"local_path": str(complete), "commits": []}],
            "skipped": [
                {"path": str(empty), "reason": _mm_events.WALK_SKIP_NO_COMMITS},
                {"path": str(failed), "reason": _mm_events.WALK_SKIP_BUDGET_ABORT},
            ],
        }
    ]
    _mm_events.commit_repo_cursors(
        rows, since=since, cursor_ts=cursor, recorded_ts=recorded.isoformat(), complete=False
    )
    cache = _mm_events.read_git_capture_cache()
    assert cache["repo_cursors"] == {
        str(complete): recorded.isoformat(),
        str(empty): recorded.isoformat(),
        str(failed): old.isoformat(),
    }
    assert cache["incomplete_streak"] == 1
    assert cache["future"] == "preserved"
    assert cache["projects"]["future"]["track83"] == {"keep": True}
    assert _mm_events.count_pending_repositories([str(empty), failed], recorded) == 1
    _mm_events.commit_repo_cursors(
        rows, since=since, cursor_ts=cursor, recorded_ts=recorded.isoformat(), complete=False
    )
    assert _mm_events.read_git_capture_cache() == cache


def test_repo_cursor_pending_unions_index_and_cache_without_writes(tmp_path, monkeypatch):
    from mind_meld import config

    explicit, indexed, retained = [
        _cursor_repo(tmp_path, name) for name in ("explicit", "indexed", "retained")
    ]
    since = datetime.now(timezone.utc)
    _write_git_cursor_cache(
        {
            "projects": {"entry": {"git_root": str(indexed)}},
            "repo_cursors": {
                str(retained): (since - timedelta(days=1)).isoformat(),
                str(tmp_path / "gone"): since.isoformat(),
            },
        }
    )
    before = config.GIT_CAPTURE_CACHE_PATH.read_bytes()
    assert _mm_events.count_pending_repositories([explicit, str(retained)], since) == 3
    assert config.GIT_CAPTURE_CACHE_PATH.read_bytes() == before
    missing = tmp_path / "missing" / "cache.json"
    monkeypatch.setattr(config, "GIT_CAPTURE_CACHE_PATH", missing)
    assert _mm_events.count_pending_repositories([explicit], since) == 1
    assert not missing.parent.exists()


def test_repo_cursor_reconcile_recovers_skips_after_append_before_cache_commit(tmp_path):
    root = _cursor_repo(tmp_path, "skipped")
    completed = _cursor_repo(tmp_path, "completed")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    ts = datetime.now(timezone.utc) - timedelta(seconds=2)
    events_dir = tmp_path / "events"
    cap = _mm_events.make_git_capture(
        since=since, discovery="complete", walk_budget_aborts=1, walk_errors=0
    )
    rows = [
        {
            "v": 1,
            "type": "git-snapshot",
            "ts": ts.isoformat(),
            "device": "dev-a",
            "projects": [{"local_path": str(completed), "commits": []}],
            "skipped": [{"path": str(root), "reason": _mm_events.WALK_SKIP_BUDGET_ABORT}],
        },
        {"v": 1, "type": "mm-push", "ts": ts.isoformat(), "device": "dev-a", "git_capture": cap},
    ]
    _mm_events.write_push_event(events_dir, "dev-a", rows)
    _mm_events.reconcile_uncommitted_push_cursors(events_dir, "dev-a")
    cache = _mm_events.read_git_capture_cache()
    assert cache["repo_cursors"][str(root)] == since.isoformat()
    assert cache["repo_cursors"][str(completed)] == ts.isoformat()
    assert cache["last_committed_push_ts"] == ts.isoformat()
    assert cache["incomplete_streak"] == 1
    _mm_events.reconcile_uncommitted_push_cursors(events_dir, "dev-a")
    assert _mm_events.read_git_capture_cache() == cache


def test_probe_claude_preserves_cursor_when_changed_project_keeps_same_git_root(
    tmp_path, monkeypatch
):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repo = _cursor_repo(tmp_path, "repo")
    proj = tmp_path / ".claude" / "projects" / "p1"
    proj.mkdir(parents=True)
    log = proj / "session.jsonl"
    log.write_text(json.dumps({"cwd": str(repo)}) + "\n")
    cfg = {"sync": {"sources": [{"name": "claude"}]}}

    res1 = _mm_events.discover_git_roots(cfg, persist=True)
    assert res1.errors == ()
    advanced_ts = datetime.now(timezone.utc).isoformat()
    cache = _mm_events.read_git_capture_cache()
    cache["repo_cursors"][str(repo.resolve())] = advanced_ts
    cache["pending_refresh_since"] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    _write_git_cursor_cache(cache)

    log.write_text(json.dumps({"cwd": str(repo)}) + "\n" + json.dumps({"cwd": str(repo)}) + "\n")
    os.utime(proj, None)
    res2 = _mm_events.discover_git_roots(cfg, persist=True)
    assert res2.errors == ()
    after = _mm_events.read_git_capture_cache()
    assert after["repo_cursors"][str(repo.resolve())] == advanced_ts


def test_side_channel_recapture_commit_does_not_increment_streak_or_seed_failed_cursor(tmp_path):
    ok_repo = _cursor_repo(tmp_path, "ok")
    fail_repo = _cursor_repo(tmp_path, "fail")
    since = datetime.now(timezone.utc) - timedelta(days=90)
    cursor_ts = datetime.now(timezone.utc)
    rows = [
        {
            "v": 1,
            "type": "git-snapshot",
            "ts": cursor_ts.isoformat(),
            "device": "dev-a",
            "origin": "recapture",
            "projects": [{"local_path": str(ok_repo), "commits": []}],
            "skipped": [{"path": str(fail_repo), "reason": _mm_events.WALK_SKIP_BUDGET_ABORT}],
        }
    ]
    _mm_events.commit_repo_cursors(
        rows, since=since, cursor_ts=cursor_ts, recorded_ts=None, complete=False
    )
    cache = _mm_events.read_git_capture_cache()
    assert cache["repo_cursors"] == {str(ok_repo): cursor_ts.isoformat()}
    assert cache["incomplete_streak"] == 0
    assert "last_committed_push_ts" not in cache
    older = cursor_ts - timedelta(days=20)
    _write_git_cursor_cache({"repo_cursors": {str(ok_repo): older.isoformat()}})
    _mm_events.commit_repo_cursors(
        rows,
        since=cursor_ts - timedelta(days=1),
        cursor_ts=cursor_ts,
        recorded_ts=None,
        complete=True,
    )
    assert _mm_events.read_git_capture_cache()["repo_cursors"][str(ok_repo)] == older.isoformat()


def test_latest_event_rows_ignores_future_clock_skew_for_mm_push_and_git_snapshot(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    real_ts = (now - timedelta(seconds=5)).isoformat()
    future_ts = (now + timedelta(hours=12)).isoformat()
    today_file = events_dir / f"dev-a-{now.strftime('%Y-%m-%d')}.jsonl"
    tomorrow_file = events_dir / f"dev-a-{(now + timedelta(days=1)).strftime('%Y-%m-%d')}.jsonl"
    today_file.write_text(
        json.dumps({"v": 1, "type": "git-snapshot", "ts": future_ts, "device": "dev-a"})
        + "\n"
        + json.dumps({"v": 1, "type": "mm-push", "ts": future_ts, "device": "dev-a"})
        + "\n"
        + json.dumps({"v": 1, "type": "git-snapshot", "ts": real_ts, "device": "dev-a"})
        + "\n"
        + json.dumps({"v": 1, "type": "mm-push", "ts": real_ts, "device": "dev-a"})
        + "\n"
    )
    tomorrow_file.write_text(
        json.dumps({"v": 1, "type": "git-snapshot", "ts": future_ts, "device": "dev-a"})
        + "\n"
        + json.dumps({"v": 1, "type": "mm-push", "ts": future_ts, "device": "dev-a"})
        + "\n"
    )
    scan = _mm_events.latest_event_rows(events_dir, "dev-a", {"mm-push", "git-snapshot"})
    assert scan.rows["mm-push"]["ts"] == real_ts
    assert scan.rows["git-snapshot"]["ts"] == real_ts
