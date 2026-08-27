"""Track 30A: git-walk visibility, cursor gate, recapture, diag projection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import cli, events, events_tail, retention
from mind_meld.cli import app
from mind_meld.skills.retro_fleet import aggregator

runner = CliRunner()


def _write_push(
    events_dir: Path,
    device: str,
    *,
    ts: datetime,
    discovery: str | None = "complete",
    since: datetime | None = None,
    walk_budget_aborts: int = 0,
    walk_errors: int = 0,
    extra: dict | None = None,
) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    row: dict = {
        "v": 2,
        "type": "mm-push",
        "ts": ts.isoformat(),
        "device": device,
        "mm_version": "0.12.46",
        "sources": ["claude"],
        "discovery_errors": [],
    }
    if discovery is not None:
        row["git_capture"] = {
            "since": (since or ts).isoformat(),
            "discovery": discovery,
            "walk_budget_aborts": walk_budget_aborts,
            "walk_errors": walk_errors,
        }
    if extra:
        row.update(extra)
    path = events_dir / f"{device}-{ts.date().isoformat()}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


class TestCaptureAdvancesCursor:
    @pytest.mark.parametrize(
        "payload, advances",
        [
            (None, True),
            ("not-a-dict", True),
            ({}, True),
            ({"discovery": "complete"}, True),
            ({"discovery": "not-run"}, True),
            ({"discovery": "unknown-future-value"}, True),
            ({"discovery": 1}, True),
            ({"discovery": "partial"}, False),
            ({"discovery": "empty"}, False),
            (
                {
                    "discovery": "complete",
                    "walk_budget_aborts": 9,
                    "walk_errors": 9,
                },
                True,
            ),
        ],
    )
    def test_truth_table(self, payload, advances):
        assert events.capture_advances_cursor(payload) is advances


class TestResolvePushCursor:
    def test_fresh_install_is_floor_not_degraded(self, tmp_path):
        now = datetime.now(timezone.utc)
        res = events.resolve_push_cursor(tmp_path / "events", "dev-a", now=now)
        assert res.held is False
        assert res.floored_after_incomplete is False
        assert res.used_floor is True
        assert abs((now - res.since).total_seconds() - 30 * 86400) < 2

    def test_walk_abort_with_clean_discovery_advances(self, tmp_path):
        events_dir = tmp_path / "events"
        ts = datetime.now(timezone.utc) - timedelta(hours=1)
        _write_push(
            events_dir,
            "dev-a",
            ts=ts,
            discovery="complete",
            walk_budget_aborts=2,
            walk_errors=1,
        )
        res = events.resolve_push_cursor(events_dir, "dev-a")
        assert res.held is False
        assert abs((res.since - ts).total_seconds()) < 1

    def test_partial_holds_at_older_complete(self, tmp_path):
        events_dir = tmp_path / "events"
        older = datetime.now(timezone.utc) - timedelta(days=2)
        newer = datetime.now(timezone.utc) - timedelta(hours=1)
        _write_push(events_dir, "dev-a", ts=older, discovery="complete")
        _write_push(events_dir, "dev-a", ts=newer, discovery="partial", since=older)
        res = events.resolve_push_cursor(events_dir, "dev-a")
        assert res.held is True
        assert abs((res.since - older).total_seconds()) < 1

    def test_45_day_complete_is_found_not_floored(self, tmp_path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        older = now - timedelta(days=45)
        _write_push(events_dir, "dev-a", ts=older, discovery="complete")
        res = events.resolve_push_cursor(events_dir, "dev-a", now=now)
        assert res.used_floor is False
        assert abs((res.since - older).total_seconds()) < 2
        floor = now - timedelta(days=30)
        assert res.since < floor

    def test_hold_since_older_than_retention_is_not_orphaned(self, tmp_path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=100)
        _write_push(
            events_dir,
            "dev-a",
            ts=now,
            discovery="partial",
            since=since,
        )
        res = events.resolve_push_cursor(events_dir, "dev-a", now=now)
        assert res.held is True
        assert abs((res.since - since).total_seconds()) < 2

    def test_future_ts_cannot_move_cursor_forward(self, tmp_path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        good = now - timedelta(days=1)
        future = now + timedelta(days=365)
        _write_push(events_dir, "dev-a", ts=good, discovery="complete")
        _write_push(events_dir, "dev-a", ts=future, discovery="complete")
        res = events.resolve_push_cursor(events_dir, "dev-a", now=now)
        assert abs((res.since - good).total_seconds()) < 2

    def test_near_future_ts_cannot_move_cursor_forward(self, tmp_path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        good = now - timedelta(days=1)
        future = now + timedelta(seconds=1)
        _write_push(events_dir, "dev-a", ts=good, discovery="complete")
        _write_push(events_dir, "dev-a", ts=future, discovery="complete")
        res = events.resolve_push_cursor(events_dir, "dev-a", now=now)
        assert abs((res.since - good).total_seconds()) < 2

    def test_future_capture_since_cannot_move_cursor_forward(self, tmp_path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        _write_push(
            events_dir,
            "dev-a",
            ts=now - timedelta(minutes=1),
            discovery="partial",
            since=now + timedelta(seconds=1),
        )
        res = events.resolve_push_cursor(events_dir, "dev-a", now=now)
        assert res.used_floor is True
        assert res.since <= now

    def test_capture_since_after_its_row_cannot_move_cursor_forward(self, tmp_path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        _write_push(
            events_dir,
            "dev-a",
            ts=now - timedelta(hours=2),
            discovery="partial",
            since=now - timedelta(hours=1),
        )
        res = events.resolve_push_cursor(events_dir, "dev-a", now=now)
        assert res.used_floor is True
        assert res.since < now - timedelta(days=29)

    def test_naive_ts_cannot_move_cursor_forward(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        now = datetime.now(timezone.utc)
        path = events_dir / f"dev-a-{now.date().isoformat()}.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "mm-push",
                    "ts": "2099-01-01T00:00:00",
                    "git_capture": {"discovery": "complete"},
                }
            )
            + "\n"
        )
        res = events.resolve_push_cursor(events_dir, "dev-a", now=now)
        assert res.used_floor is True

    def test_malformed_ts_cannot_move_cursor_forward(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        now = datetime.now(timezone.utc)
        path = events_dir / f"dev-a-{now.date().isoformat()}.jsonl"
        path.write_text(
            json.dumps({"type": "mm-push", "ts": "not-a-timestamp", "git_capture": {}}) + "\n"
        )
        res = events.resolve_push_cursor(events_dir, "dev-a", now=now)
        assert res.used_floor is True

    def test_alternating_rows_cursor_is_monotonic(self, tmp_path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        floor = now - timedelta(days=30)
        cursors = []
        for i in range(40):
            ts = now - timedelta(minutes=40 - i)
            discovery = "complete" if i % 2 == 0 else "partial"
            _write_push(events_dir, "dev-a", ts=ts, discovery=discovery)
            cursors.append(events.last_push_ts(events_dir, "dev-a"))
        for earlier, later in zip(cursors, cursors[1:]):
            assert later >= earlier
        assert all(c >= floor - timedelta(seconds=5) for c in cursors)


def test_cursor_predicate_does_not_reference_walk_skips() -> None:
    src = Path("src/mind_meld/events.py").read_text(encoding="utf-8")
    start = src.index("def capture_advances_cursor")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    forbidden = (
        "budget_abort",
        "walk_errors",
        "walk_budget_aborts",
        "WALK_SKIP",
        "skipped",
        "no_commits",
    )
    for needle in forbidden:
        assert needle not in body, (
            f"{needle!r} in capture_advances_cursor would hold the cursor on a "
            "walk abort and wedge unattended autopush. Git-walk cost is monotone "
            "in cursor age (48.6 ms @1d → 251.3 ms @30d vs a 250 ms budget)."
        )


def test_cursor_scan_matches_retention() -> None:
    assert events.CURSOR_SCAN_DAYS == retention.EVENTS_RETENTION_DAYS


def test_window_pattern_is_shared() -> None:
    assert aggregator.WINDOW_PATTERN is events.WINDOW_PATTERN


def test_git_walk_budget_escalates_when_cursor_is_old() -> None:
    now = datetime.now(timezone.utc)
    fresh = events.git_walk_budget_ms(quiet=True, since=now - timedelta(hours=1), now=now)
    old = events.git_walk_budget_ms(quiet=True, since=now - timedelta(days=30), now=now)
    assert fresh == events.WALK_TIME_BUDGET_AUTOPUSH_MS
    assert old == events.WALK_TIME_BUDGET_INTERACTIVE_MS
    interactive = events.git_walk_budget_ms(quiet=False, since=now - timedelta(hours=1), now=now)
    assert interactive == events.WALK_TIME_BUDGET_INTERACTIVE_MS


class TestProjectRecordedCapture:
    def test_legacy_row_without_key_advances(self):
        projected = events.project_recorded_capture(
            {"type": "mm-push", "ts": "2026-08-01T00:00:00+00:00", "mm_version": "0.12.45"}
        )
        assert projected is not None
        assert projected["discovery"] is None
        assert projected["advances_cursor"] is True
        assert "local_emails" not in projected

    def test_non_push_is_none(self):
        assert events.project_recorded_capture({"type": "git-snapshot"}) is None


class TestRecaptureWindowCli:
    def test_help_states_idempotence_and_commit_date_rule(self):
        r = runner.invoke(app, ["recapture", "--help"])
        assert r.exit_code == 0, r.output
        assert "Safe to re-run" in r.output
        assert "remote, sha" in r.output
        assert "COMMIT" in r.output

    @pytest.mark.parametrize(
        "window, needle",
        [
            ("30", "did you mean '30d'"),
            ("7", "did you mean '7d'"),
            ("nope", "must be of the form Nd"),
            ("365d", "between 1d and 90d"),
            ("0d", "between 1d and 90d"),
            ("91d", "between 1d and 90d"),
        ],
    )
    def test_rejects_bad_windows(self, window, needle):
        r = runner.invoke(app, ["recapture", window])
        assert r.exit_code != 0
        text = (r.stdout or "") + (r.stderr or "")
        assert needle.replace("\n", "") in text.replace("\n", "")


def test_commit_date_contract_recapture_today_does_not_change_7d_card(tmp_path, monkeypatch):
    """A 30-day-old commit recaptured today is invisible on 7d and visible on 30d."""
    monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
    events_dir = tmp_path / "events"
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    commit = {
        "sha": "deadbeef30",
        "date": (now - timedelta(days=30)).isoformat(),
        "author_email": "kb@example.com",
        "subject": "fix: old",
        "files": 1,
        "add": 1,
        "del": 0,
    }
    row = {
        "v": 2,
        "type": "git-snapshot",
        "ts": now.isoformat(),
        "device": "dev-a",
        "origin": events.GIT_SNAPSHOT_ORIGIN_RECAPTURE,
        "projects": [
            {
                "remote": "https://github.com/kb/mm.git",
                "local_path": "/tmp/mm",
                "commits": [commit],
            }
        ],
    }
    events_dir.mkdir()
    (events_dir / f"dev-a-{now.date().isoformat()}.jsonl").write_text(json.dumps(row) + "\n")
    data7 = aggregator.aggregate(
        events_dir=events_dir,
        window_days=7,
        author_emails=frozenset({"kb@example.com"}),
        now=now,
    )
    data30 = aggregator.aggregate(
        events_dir=events_dir,
        window_days=30,
        author_emails=frozenset({"kb@example.com"}),
        now=now,
    )
    assert data7.git.commits == 0
    assert data30.git.commits == 1


def test_ordinary_push_still_noops_on_a_clean_tree(tmp_path, monkeypatch):
    from mind_meld import token_usage
    from tests.test_silent_failure_contract import _setup_events_tail_config

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    first = runner.invoke(app, ["push"])
    assert first.exit_code == 0, (first.stdout, first.stderr)
    second = runner.invoke(app, ["push"])
    assert second.exit_code == 0, (second.stdout, second.stderr)
    assert "Nothing to push" in second.stdout
    assert "recapture" not in second.stdout.lower()


def test_recapture_dry_run_writes_nothing(tmp_path, monkeypatch):
    from tests.test_silent_failure_contract import _setup_events_tail_config

    _setup_events_tail_config(tmp_path, monkeypatch)
    events_dir = tmp_path / "mm-events" / "events"
    before = sorted(p.name for p in events_dir.glob("*.jsonl"))
    r = runner.invoke(app, ["recapture", "1d", "--dry-run"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "dry-run" in r.stdout.lower() or "nothing written" in r.stdout.lower()
    after = sorted(p.name for p in events_dir.glob("*.jsonl"))
    assert after == before
    assert "Push complete" not in r.stdout


def test_recapture_dry_run_does_not_create_events_dir(tmp_path, monkeypatch):
    from tests.test_silent_failure_contract import _setup_events_tail_config

    _setup_events_tail_config(tmp_path, monkeypatch)
    events_dir = tmp_path / "mm-events" / "events"
    events_dir.rmdir()
    monkeypatch.setattr(
        cli,
        "_push_core",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not enter _push_core"),
    )
    r = runner.invoke(app, ["recapture", "1d", "--dry-run"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert not events_dir.exists()


def test_recapture_zero_roots_does_not_create_events_dir(tmp_path, monkeypatch):
    from tests.test_silent_failure_contract import _setup_events_tail_config

    _setup_events_tail_config(tmp_path, monkeypatch)
    events_dir = tmp_path / "mm-events" / "events"
    events_dir.rmdir()
    monkeypatch.setattr(
        events,
        "discover_git_roots",
        lambda _config, **_kw: events.GitRootDiscovery((), (), False, (), (), (), ("claude",)),
    )
    r = runner.invoke(app, ["recapture", "1d"])
    assert r.exit_code == 1, (r.stdout, r.stderr)
    assert not events_dir.exists()


def test_recapture_stages_snapshots_before_ordinary_push(tmp_path, monkeypatch):
    from tests.test_silent_failure_contract import _setup_events_tail_config

    _setup_events_tail_config(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    events_dir = tmp_path / "mm-events" / "events"
    repo = tmp_path / "repo"
    repo.mkdir()
    row = {
        "v": events.EVENTS_SCHEMA_VERSION,
        "type": "git-snapshot",
        "ts": now.isoformat(),
        "device": "dev-deg",
        "origin": events.GIT_SNAPSHOT_ORIGIN_RECAPTURE,
        "projects": [
            {
                "remote": "https://github.com/kbitz/mind-meld.git",
                "local_path": str(repo),
                "commits": [
                    {
                        "sha": "recaptured",
                        "date": now.isoformat(),
                        "author_email": "kb@example.com",
                        "subject": "fix: recovered",
                        "files": 1,
                        "add": 1,
                        "del": 0,
                    }
                ],
            }
        ],
        "skipped": [],
    }
    prepared = events_tail.RecaptureCapture(
        git_rows=[row],
        root_discovery=events.GitRootDiscovery((repo,), (), False),
        walk_budget_aborts=0,
        walk_errors=0,
        events_dir=events_dir,
        since=now - timedelta(days=1),
        until=now,
    )
    calls: list[tuple[str, list[dict] | None]] = []
    monkeypatch.setattr(
        events_tail,
        "_prepare_recapture",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        events,
        "write_push_event",
        lambda _events_dir, _device_id, rows: calls.append(("snapshot", rows)),
    )
    monkeypatch.setattr(
        cli,
        "_push_core",
        lambda *_args, **_kwargs: calls.append(("push", None)) or object(),
    )

    r = runner.invoke(app, ["recapture", "1d"])

    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert [kind for kind, _rows in calls] == ["snapshot", "push"]
    snapshot_rows = calls[0][1]
    assert snapshot_rows is not None
    assert [item["type"] for item in snapshot_rows] == ["git-snapshot"]
    assert snapshot_rows[0]["origin"] == events.GIT_SNAPSHOT_ORIGIN_RECAPTURE


def test_recapture_unresolved_mm_events_exits_nonzero(tmp_path, monkeypatch):
    from mind_meld.config import load_config, save_config
    from tests.test_silent_failure_contract import _setup_events_tail_config

    _setup_events_tail_config(tmp_path, monkeypatch)
    cfg = load_config()
    cfg.setdefault("sync", {})["disabled_sources"] = ["mm-events"]
    save_config(cfg)
    r = runner.invoke(app, ["recapture", "7d"])
    assert r.exit_code != 0
    text = (r.stdout or "") + (r.stderr or "")
    assert "mm-events" in text
    assert "mm enable-source mm-events" in text


def test_recapture_partial_walk_exits_4(tmp_path, monkeypatch):
    from tests.test_silent_failure_contract import _setup_events_tail_config

    _setup_events_tail_config(tmp_path, monkeypatch)
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    def fake_walk(roots, since, total_budget_ms):
        return [
            {
                "v": 2,
                "type": "git-snapshot",
                "ts": datetime.now(timezone.utc).isoformat(),
                "device": "",
                "projects": [{"remote": "", "local_path": str(repo_a), "commits": []}],
                "skipped": [{"path": str(repo_b), "reason": events.WALK_SKIP_BUDGET_ABORT}],
            }
        ]

    monkeypatch.setattr(events, "walk_git_projects", fake_walk)
    monkeypatch.setattr(
        events,
        "discover_git_roots",
        lambda _cfg, **_kw: events.GitRootDiscovery(
            (repo_a, repo_b), (), False, (), (), (), ("claude",)
        ),
    )
    r = runner.invoke(app, ["recapture", "7d"])
    assert r.exit_code == 4, (r.stdout, r.stderr)
    assert "Push complete" not in (r.stdout or "").splitlines()[-1:]
    assert "incomplete" in (r.stdout or "").lower() or "skipped" in (r.stdout or "").lower()


def test_successful_recapture_clears_held_cursor(tmp_path, monkeypatch):
    from mind_meld import token_usage
    from tests.test_silent_failure_contract import _setup_events_tail_config

    _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([tmp_path / "claude"])
    events_dir = tmp_path / "mm-events" / "events"
    now = datetime.now(timezone.utc)
    older = now - timedelta(days=2)
    _write_push(events_dir, "dev-deg", ts=older, discovery="complete")
    _write_push(events_dir, "dev-deg", ts=now, discovery="partial", since=older)
    held = events.resolve_push_cursor(events_dir, "dev-deg")
    assert held.held is True
    r = runner.invoke(app, ["recapture", "1d"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    after = events.resolve_push_cursor(events_dir, "dev-deg")
    assert after.held is False
    assert after.since > held.since


def test_timeout_skip_degrades_the_tail(tmp_path, monkeypatch):
    sources = [
        {
            "name": "mm-events",
            "type": "generic",
            "path": str(tmp_path / "mm-events"),
            "include_dirs": ["events"],
        }
    ]
    (tmp_path / "mm-events" / "events").mkdir(parents=True)
    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.setattr(
        events,
        "discover_git_roots",
        lambda _cfg, **_kw: events.GitRootDiscovery((repo,), (), False, (), (), (), ("claude",)),
    )
    monkeypatch.setattr(
        events,
        "walk_git_projects",
        lambda roots, since, total_budget_ms: [
            {
                "v": 2,
                "type": "git-snapshot",
                "ts": datetime.now(timezone.utc).isoformat(),
                "device": "",
                "projects": [],
                "skipped": [{"path": str(repo), "reason": events.WALK_SKIP_TIMEOUT}],
            }
        ],
    )
    degradations = events_tail._run_events_tail({}, sources, "dev-a", dry_run=False, quiet=True)
    assert any("git walk dropped" in d for d in degradations)
    assert all("git repository discovery" not in d for d in degradations)
    assert all("; " not in d for d in degradations)


def test_no_commits_skip_does_not_degrade_the_tail(tmp_path, monkeypatch):
    sources = [
        {
            "name": "mm-events",
            "type": "generic",
            "path": str(tmp_path / "mm-events"),
            "include_dirs": ["events"],
        }
    ]
    (tmp_path / "mm-events" / "events").mkdir(parents=True)
    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.setattr(
        events,
        "discover_git_roots",
        lambda _cfg, **_kw: events.GitRootDiscovery((repo,), (), False, (), (), (), ("claude",)),
    )
    monkeypatch.setattr(
        events,
        "walk_git_projects",
        lambda roots, since, total_budget_ms: [
            {
                "v": 2,
                "type": "git-snapshot",
                "ts": datetime.now(timezone.utc).isoformat(),
                "device": "",
                "projects": [],
                "skipped": [{"path": str(repo), "reason": events.WALK_SKIP_NO_COMMITS}],
            }
        ],
    )
    degradations = events_tail._run_events_tail({}, sources, "dev-a", dry_run=False, quiet=True)
    assert not any("git walk dropped" in d for d in degradations)


def test_autopush_never_reaches_recapture_path() -> None:
    import inspect

    from mind_meld import cli

    src = inspect.getsource(cli.autopush)
    assert "_prepare_recapture" not in src
    assert "_run_events_recapture" not in src
    assert "recapture(" not in src
