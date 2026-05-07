"""Group 8 / Track 8A: retro-fleet aggregator pins.

Pins all the load-bearing aggregation rules from /plan-eng-review:

* CQ#3 — tolerant reader: empty fleet, missing gstack, torn JSONL line,
  unknown fields, non-dict objects don't crash.
* CQ#2 — `MM_EVENTS_DIR` env override.
* Architecture #1 / Cross-model #1 — sessions-snapshot dedup-per-(device,
  claude_dir): v=2 latest-per-tuple wins; v=1 surfaces as pre-v0.11.0
  peer breadcrumb but is NOT summed.
* Architecture #3 — `mm devices --format=json` failure degrades to
  "events from N devices" (no "of M known" tail).
* TODO#1 — skipped-events visible-failure breadcrumb in tail.
* TODO#2 — window > retention warning.
* Author filter via git config + optional [retro].author_emails.
* Cherry-pick informational counted but not deduped (acknowledged
  v1 limitation).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mind_meld.skills.retro_fleet import aggregator

# ---------------------------------------------------------------------------
# Fixtures — synthetic events for the table-tested aggregations.
# ---------------------------------------------------------------------------


NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: float = 0.0) -> str:
    """ISO 8601 UTC timestamp ``days_ago`` before NOW."""
    return (NOW - timedelta(days=days_ago)).isoformat()


def _git_event(
    device: str,
    days_ago: float,
    commits: list[dict],
    remote: str = "github.com/kb/mm",
) -> dict:
    return {
        "v": 2,
        "type": "git-snapshot",
        "ts": _ts(days_ago),
        "device": device,
        "projects": [
            {
                "remote": f"https://{remote}.git",  # raw form — aggregator canonicalizes
                "local_path": f"/Users/kb/{remote.split('/')[-1]}",
                "commits": commits,
            }
        ],
    }


def _commit(
    sha: str,
    days_ago: float,
    *,
    author_email: str = "kb@example.com",
    subject: str = "fix: thing",
    add: int = 10,
    dlt: int = 2,
    files: int = 1,
) -> dict:
    return {
        "sha": sha,
        "date": _ts(days_ago),
        "author_email": author_email,
        "subject": subject,
        "files": files,
        "add": add,
        "del": dlt,
    }


def _sessions_event(device: str, days_ago: float, projects: list[dict], v: int = 2) -> dict:
    return {
        "v": v,
        "type": "sessions-snapshot",
        "ts": _ts(days_ago),
        "device": device,
        "projects": projects,
    }


def _push_event(
    device: str,
    days_ago: float,
    sources: list[str] | None = None,
    discovery_errors: list[str] | None = None,
) -> dict:
    return {
        "v": 2,
        "type": "mm-push",
        "ts": _ts(days_ago),
        "device": device,
        "mm_version": "0.11.0",
        "sources": sources or ["claude", "gstack"],
        "discovery_errors": discovery_errors or [],
    }


def _write_events(events_dir: Path, device: str, day_iso: str, events: list[dict]) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / f"{device}-{day_iso}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _aggregate(
    events_dir: Path,
    *,
    window_days: int = 7,
    author_emails: frozenset[str] = frozenset(),
    skill_usage_path: Path | None = None,  # vestigial; ignored post-v0.11.27
    now: datetime = NOW,
) -> aggregator.RetroData:
    # ``skill_usage_path`` was removed from ``aggregate()`` when the gstack-
    # analytics reader was deleted. Kept on this helper's signature so older
    # tests that pass it through continue to call the helper unchanged;
    # value is ignored.
    return aggregator.aggregate(
        events_dir=events_dir,
        window_days=window_days,
        author_emails=author_emails,
        now=now,
    )


# ---------------------------------------------------------------------------
# T1 — Tolerant reader (CQ#3).
# ---------------------------------------------------------------------------


class TestTolerantReader:
    def test_empty_fleet_renders_day_one_output(self, tmp_path):
        """Day-1 fleet: zero events on disk → renders without crashing."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        data = _aggregate(events_dir)
        assert data.git.commits == 0
        assert data.sessions.total_sessions == 0
        assert data.pushes.push_events == 0
        # Skills unavailable when path doesn't exist.
        assert data.skills.available is False
        out = aggregator.format_retro(data)
        assert "# Retro:" in out

    def test_events_dir_absent_silently(self, tmp_path):
        """Path that doesn't exist → empty data, no exception."""
        data = _aggregate(tmp_path / "nonexistent-events")
        assert data.git.commits == 0
        assert data.skipped_lines == 0

    def test_torn_jsonl_line_skipped_and_counted(self, tmp_path):
        """Mid-line crash leaves a half-written record. Reader skips it,
        counts it in skipped_lines, continues parsing the rest."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        path = events_dir / "dev-a-2026-04-28.jsonl"
        # First line is fine, second is torn JSON, third is fine.
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_push_event("dev-a", 0)) + "\n")
            f.write('{"v":2,"type":"mm-push","ts":"2026-04-28T12:00:00Z",\n')  # torn
            f.write(json.dumps(_push_event("dev-a", 0)) + "\n")
        data = _aggregate(events_dir)
        assert data.skipped_lines == 1
        assert data.pushes.push_events == 2

    def test_non_dict_lines_skipped(self, tmp_path):
        """JSONL with array / scalar values is invalid for our schema —
        skipped, counted."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        path = events_dir / "dev-a-2026-04-28.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps([1, 2, 3]) + "\n")  # array, not dict
            f.write(json.dumps("just a string") + "\n")
            f.write(json.dumps(_push_event("dev-a", 0)) + "\n")
        data = _aggregate(events_dir)
        assert data.skipped_lines == 2
        assert data.pushes.push_events == 1

    def test_invalid_utf8_bytes_replaced_not_crash(self, tmp_path):
        """Adversarial-review regression: a JSONL file with invalid UTF-8
        bytes used to crash with UnicodeDecodeError because the reader
        opened with default ``errors="strict"``. Fix: open with
        ``errors="replace"`` so a corrupt-byte run becomes U+FFFD and the
        line still parses (and is skipped by JSONDecodeError, counted)."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        path = events_dir / "dev-a-2026-04-28.jsonl"
        # Mix valid + invalid-UTF-8 bytes.
        with open(path, "wb") as f:
            f.write((json.dumps(_push_event("dev-a", 0)) + "\n").encode("utf-8"))
            # Invalid UTF-8 sequence (lone continuation byte).
            f.write(b"\x80\xff\x80 not valid json or utf-8\n")
            f.write((json.dumps(_push_event("dev-a", 0)) + "\n").encode("utf-8"))
        # Must not raise.
        data = _aggregate(events_dir)
        # Two valid push events parsed; one corrupt-line skipped.
        assert data.pushes.push_events == 2
        assert data.skipped_lines >= 1

    def test_file_open_failure_counted_as_skipped(self, tmp_path, monkeypatch):
        """Adversarial-review regression: per-file open failures used to
        be silently ignored — a permission error or transient EIO on one
        events file produced an incomplete retro with no breadcrumb. Fix:
        bump skip_counter on file-open failure too. Visible-failure
        contract: user sees 'N events skipped' in the tail."""
        import builtins

        events_dir = tmp_path / "events"
        events_dir.mkdir()
        path = events_dir / "dev-a-2026-04-28.jsonl"
        path.write_text(json.dumps(_push_event("dev-a", 0)) + "\n")

        # Force open() to raise OSError for our specific file.
        original_open = builtins.open

        def fake_open(p, *args, **kwargs):
            if "dev-a-2026-04-28.jsonl" in str(p):
                raise PermissionError("simulated EACCES")
            return original_open(p, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fake_open)
        data = _aggregate(events_dir)
        # File couldn't be opened → counted as a skip.
        assert data.skipped_lines >= 1
        out = aggregator.format_retro(data)
        assert "skipped" in out

    def test_unknown_fields_in_event_tolerated(self, tmp_path):
        """Unknown fields don't crash — forward-compat with future schema."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        ev = _push_event("dev-a", 0)
        ev["future_field_we_dont_know_about"] = {"nested": "data"}
        _write_events(events_dir, "dev-a", "2026-04-28", [ev])
        data = _aggregate(events_dir)
        assert data.pushes.push_events == 1
        assert data.skipped_lines == 0  # Not a parse error — just unknown fields.

    def test_missing_gstack_skill_usage_renders_section_omitted(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(events_dir, "dev-a", "2026-04-28", [_push_event("dev-a", 0)])
        data = _aggregate(events_dir, skill_usage_path=tmp_path / "no-gstack.jsonl")
        out = aggregator.format_retro(data)
        assert "section omitted" in out

    # NOTE (v0.11.27): the gstack-analytics skill-usage.jsonl reader was
    # retired with the fleet-skill-counts pivot. The "unknown field
    # tolerated" behavior was load-bearing only for that reader; once
    # we read skills out of v=2 sessions-snapshot events, the field
    # surface is the SessionMetadata TypedDict (total=False), and
    # tolerance for unknown peer fields is covered by the per-key
    # ``isinstance`` guards in ``aggregate_sessions``. Test removed.


# ---------------------------------------------------------------------------
# T2 — Architecture #1 / Cross-model #1: sessions-snapshot dedup semantics.
# ---------------------------------------------------------------------------


class TestSessionsAggregation:
    def test_v2_latest_snapshot_per_device_claude_dir_wins(self, tmp_path):
        """The core regression for cross-model #1: 3 v=2 snapshots from the
        same machine on the same day must NOT triple-count sessions. The
        latest snapshot's sessions count is the truth."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj_old = {"claude_dir": "-tmp-x", "sessions": 5, "total_kb": 100, "ephemeral": False}
        proj_mid = {"claude_dir": "-tmp-x", "sessions": 7, "total_kb": 150, "ephemeral": False}
        proj_new = {"claude_dir": "-tmp-x", "sessions": 9, "total_kb": 200, "ephemeral": False}
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _sessions_event("dev-a", days_ago=2.0, projects=[proj_old]),
                _sessions_event("dev-a", days_ago=1.0, projects=[proj_mid]),
                _sessions_event("dev-a", days_ago=0.5, projects=[proj_new]),
            ],
        )
        data = _aggregate(events_dir)
        # Latest wins → sessions=9, NOT 5+7+9=21.
        assert data.sessions.total_sessions == 9

    def test_v2_aggregation_sums_across_devices(self, tmp_path):
        """Two devices, same project → sum across (device, claude_dir).
        Distinct devices = distinct chats."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj = {"claude_dir": "-tmp-x", "sessions": 5, "total_kb": 100, "ephemeral": False}
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _sessions_event("dev-a", 1.0, [proj]),
            ],
        )
        _write_events(
            events_dir,
            "dev-b",
            "2026-04-28",
            [
                _sessions_event("dev-b", 1.0, [proj]),
            ],
        )
        data = _aggregate(events_dir)
        # Sum across (dev-a, -tmp-x) + (dev-b, -tmp-x) = 5 + 5 = 10.
        assert data.sessions.total_sessions == 10
        assert data.sessions.projects == 2

    def test_v1_sessions_snapshot_NOT_summed_into_totals(self, tmp_path):
        """Cross-model #1 fix: pre-v0.11.0 v=1 snapshots have delta semantics
        that don't reconcile with v=2's full inventory. Aggregator MUST NOT
        sum them — it surfaces the device as a pre-v2 peer instead."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj_v1 = {"claude_dir": "-tmp-x", "sessions": 99, "total_kb": 9999, "ephemeral": False}
        _write_events(
            events_dir,
            "dev-old",
            "2026-04-28",
            [
                _sessions_event("dev-old", 1.0, [proj_v1], v=1),
            ],
        )
        data = _aggregate(events_dir)
        assert data.sessions.total_sessions == 0  # NOT 99
        assert "dev-old" in data.sessions.pre_v2_peers
        out = aggregator.format_retro(data)
        assert "pre-v0.11.0" in out

    def test_ephemeral_split(self, tmp_path):
        """Conductor workspaces marked ephemeral are split out in the totals
        AND in the most-active section."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj_normal = {"claude_dir": "-tmp-x", "sessions": 5, "total_kb": 100, "ephemeral": False}
        proj_eph = {"claude_dir": "-conductor-y", "sessions": 3, "total_kb": 50, "ephemeral": True}
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _sessions_event("dev-a", 1.0, [proj_normal, proj_eph]),
            ],
        )
        data = _aggregate(events_dir)
        assert data.sessions.total_sessions == 8
        assert data.sessions.ephemeral_sessions == 3
        assert data.sessions.ephemeral_projects == 1

    def test_window_scoped_by_last_session_at(self, tmp_path):
        """Adversarial-review regression: pre-fix, walk_session_metadata
        ignored ``since``, so a 7d retro could include a 60d-old session
        as long as the device pushed today (snapshot's `ts` is `now`, but
        the underlying jsonl mtimes are old).

        The fix: stage-2 filter by ``last_session_at`` falling inside the
        window. Projects with NO recent activity are excluded from totals.
        Projects active inside the window are included (their full count
        — accepting some historical-session inflation — but at least the
        '0 sessions in 7 days' lie is closed).
        """
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Project A: last activity 60 days ago — well outside a 7d window.
        proj_inactive = {
            "claude_dir": "-tmp-inactive",
            "sessions": 999,
            "total_kb": 99999,
            "ephemeral": False,
            "last_session_at": _ts(60.0),
        }
        # Project B: last activity 2 days ago — inside the 7d window.
        proj_active = {
            "claude_dir": "-tmp-active",
            "sessions": 5,
            "total_kb": 100,
            "ephemeral": False,
            "last_session_at": _ts(2.0),
        }
        # Snapshot is fresh (ts=now), so it passes the snapshot-ts filter.
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_sessions_event("dev-a", 0.0, [proj_inactive, proj_active])],
        )
        data = _aggregate(events_dir, window_days=7)
        # Inactive project's 999 sessions MUST NOT be included.
        assert data.sessions.total_sessions == 5
        assert data.sessions.projects == 1

    def test_sessions_no_last_session_at_field_kept(self, tmp_path):
        """When a snapshot lacks `last_session_at` (older fixture, fresh
        bootstrap with zero jsonls), the project is INCLUDED — empty data
        is honest. The fail-open here is a v1 trade-off: rather than
        silently drop ambiguous data, surface it."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj_no_last = {
            "claude_dir": "-tmp-x",
            "sessions": 3,
            "total_kb": 50,
            "ephemeral": False,
        }
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_sessions_event("dev-a", 0.0, [proj_no_last])],
        )
        data = _aggregate(events_dir, window_days=7)
        assert data.sessions.total_sessions == 3
        assert data.sessions.projects == 1

    def test_sessions_outside_window_excluded(self, tmp_path):
        """A session-snapshot with ts older than `since` is excluded."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        old_proj = {"claude_dir": "-tmp-old", "sessions": 999, "total_kb": 9999, "ephemeral": False}
        _write_events(
            events_dir,
            "dev-a",
            "2026-03-01",
            [
                _sessions_event("dev-a", days_ago=60.0, projects=[old_proj]),
            ],
        )
        data = _aggregate(events_dir, window_days=7)
        assert data.sessions.total_sessions == 0


# ---------------------------------------------------------------------------
# T3 — Git aggregation.
# ---------------------------------------------------------------------------


class TestGitAggregation:
    def test_cross_device_same_sha_dedup(self, tmp_path):
        """The same commit captured by two devices counts ONCE thanks to
        canonical-URL + sha dedup."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        c = _commit("abc123", 1.0, add=50, dlt=10)
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event("dev-a", 1.0, [c], remote="github.com/kb/mm"),
            ],
        )
        _write_events(
            events_dir,
            "dev-b",
            "2026-04-28",
            [
                _git_event("dev-b", 1.0, [c], remote="github.com/kb/mm"),
            ],
        )
        data = _aggregate(events_dir)
        assert data.git.commits == 1
        assert data.git.additions == 50
        assert data.git.deletions == 10

    def test_canonicalization_unifies_url_forms(self, tmp_path):
        """Two devices with different URL forms (https vs scp) of the same
        repo dedup correctly."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        c = _commit("abc123", 1.0)
        ev_a = _git_event("dev-a", 1.0, [c])
        ev_a["projects"][0]["remote"] = "https://github.com/kb/mm.git"
        ev_b = _git_event("dev-b", 1.0, [c])
        ev_b["projects"][0]["remote"] = "git@github.com:kb/mm.git"
        _write_events(events_dir, "dev-a", "2026-04-28", [ev_a])
        _write_events(events_dir, "dev-b", "2026-04-28", [ev_b])
        data = _aggregate(events_dir)
        assert data.git.commits == 1

    def test_author_filter(self, tmp_path):
        """Author email filter applies — only matching commits counted."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        mine = _commit("abc", 1.0, author_email="kb@example.com")
        not_mine = _commit("xyz", 1.0, author_email="bot@noreply.com")
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event("dev-a", 1.0, [mine, not_mine]),
            ],
        )
        data = _aggregate(events_dir, author_emails=frozenset({"kb@example.com"}))
        assert data.git.commits == 1
        assert data.git.additions == 10  # _commit default

    def test_no_author_filter_renders_all(self, tmp_path):
        """Empty filter → all commits in window."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        c1 = _commit("aaa", 1.0, author_email="kb@example.com")
        c2 = _commit("bbb", 1.0, author_email="someone@else.com")
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event("dev-a", 1.0, [c1, c2]),
            ],
        )
        data = _aggregate(events_dir, author_emails=frozenset())
        assert data.git.commits == 2

    def test_commits_outside_window_excluded(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        old = _commit("aaa", 30.0)
        recent = _commit("bbb", 1.0)
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event("dev-a", 1.0, [old, recent]),
            ],
        )
        data = _aggregate(events_dir, window_days=7)
        assert data.git.commits == 1


# ---------------------------------------------------------------------------
# T3.5 — Commit streak counter.
#
# Streak = consecutive local-day stretch ending at (or one day before) `until`
# with at least one author-matched commit. Window-independent so a 7d retro
# on a 30d streak shows 30 (capped by the 90d events retention).
# ---------------------------------------------------------------------------


class TestCommitStreak:
    def test_no_commits_zero_streak(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        data = _aggregate(events_dir)
        assert data.git.streak_days == 0
        out = aggregator.format_retro(data)
        assert "commit streak" not in out  # hidden when zero

    def test_single_day_today(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_git_event("dev-a", 0.0, [_commit("a", 0.0)])],
        )
        data = _aggregate(events_dir)
        assert data.git.streak_days == 1
        out = aggregator.format_retro(data)
        assert "1-day commit streak" in out

    def test_three_consecutive_days_ending_today(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event(
                    "dev-a",
                    0.0,
                    [
                        _commit("a", 0.0),
                        _commit("b", 1.0),
                        _commit("c", 2.0),
                    ],
                ),
            ],
        )
        data = _aggregate(events_dir)
        assert data.git.streak_days == 3
        assert "3-day commit streak" in aggregator.format_retro(data)

    def test_grace_day_today_empty_yesterday_counts(self, tmp_path):
        """GitHub-style: today not yet committed but yesterday was. Streak
        starts from yesterday so an in-progress day doesn't break it."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event(
                    "dev-a",
                    0.5,
                    [
                        _commit("a", 1.0),  # yesterday
                        _commit("b", 2.0),  # day before
                    ],
                ),
            ],
        )
        data = _aggregate(events_dir)
        assert data.git.streak_days == 2

    def test_two_day_gap_breaks_streak(self, tmp_path):
        """No commit today AND no commit yesterday → streak = 0 even if a
        long run preceded it."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event(
                    "dev-a",
                    3.0,
                    [_commit("a", 3.0), _commit("b", 4.0), _commit("c", 5.0)],
                ),
            ],
        )
        data = _aggregate(events_dir)
        assert data.git.streak_days == 0

    def test_only_most_recent_run_counts(self, tmp_path):
        """5-day run, then gap, then 3-day run ending today → streak = 3."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        recent = [_commit(f"r{i}", float(i)) for i in range(3)]  # 0,1,2 days ago
        old = [_commit(f"o{i}", float(i)) for i in range(10, 15)]  # 10..14 days ago
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_git_event("dev-a", 0.0, recent + old)],
        )
        data = _aggregate(events_dir, window_days=30)
        assert data.git.streak_days == 3

    def test_multiple_commits_same_day_count_once(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event(
                    "dev-a",
                    0.0,
                    [_commit("a", 0.0), _commit("b", 0.1), _commit("c", 0.2)],
                ),
            ],
        )
        data = _aggregate(events_dir)
        # Three commits, all "today" → 1 streak day.
        assert data.git.streak_days == 1

    def test_cross_device_same_sha_counts_once(self, tmp_path):
        """Streak dedups across machines via (remote, sha) — two devices
        capturing the same commit shouldn't multiply streak days."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        c = _commit("abc", 0.0)
        _write_events(events_dir, "dev-a", "2026-04-28", [_git_event("dev-a", 0.0, [c])])
        _write_events(events_dir, "dev-b", "2026-04-28", [_git_event("dev-b", 0.0, [c])])
        data = _aggregate(events_dir)
        assert data.git.streak_days == 1

    def test_streak_outlasts_retro_window(self, tmp_path):
        """7d retro on a 14-day streak shows 14 — streak is window-independent
        (capped only by events retention)."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        commits = [_commit(f"s{i}", float(i)) for i in range(14)]  # 0..13 days ago
        _write_events(events_dir, "dev-a", "2026-04-28", [_git_event("dev-a", 0.0, commits)])
        data = _aggregate(events_dir, window_days=7)
        assert data.git.streak_days == 14
        # Windowed commit count is still bounded by the 7d retro.
        assert data.git.commits <= 8

    def test_streak_respects_author_filter(self, tmp_path):
        """A bot's daily commit shouldn't keep the user's streak alive."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event(
                    "dev-a",
                    0.0,
                    [
                        _commit("u0", 0.0, author_email="kb@example.com"),
                        # 1, 2, 3 days ago: only bot — gap from kb's
                        # perspective.
                        _commit("b1", 1.0, author_email="bot@noreply.com"),
                        _commit("b2", 2.0, author_email="bot@noreply.com"),
                        _commit("b3", 3.0, author_email="bot@noreply.com"),
                    ],
                ),
            ],
        )
        data = _aggregate(events_dir, author_emails=frozenset({"kb@example.com"}))
        # Only the user's "today" commit; yesterday belongs to the bot.
        assert data.git.streak_days == 1


# ---------------------------------------------------------------------------
# T4 — Visible-failure breadcrumbs (TODO#1, TODO#2).
# ---------------------------------------------------------------------------


class TestVisibleFailures:
    def test_skipped_lines_breadcrumb_in_output(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        path = events_dir / "dev-a-2026-04-28.jsonl"
        path.write_text("not json at all\n" + json.dumps(_push_event("dev-a", 0)) + "\n")
        data = _aggregate(events_dir)
        assert data.skipped_lines == 1
        out = aggregator.format_retro(data)
        assert "1 event(s) skipped" in out
        # Notes-section consolidation post-v0.11.12: the skip breadcrumb
        # must live in the Notes block, not a tail aside.
        assert "## Notes" in out

    # NOTE (v0.11.27): the five gstack-analytics tests that previously
    # lived here — skip-categories-tracked-separately, per-source-
    # breadcrumbs-name-the-file, pretty-printed-json-in-skill-usage-
    # recovered, breadcrumb-names-actual-path-not-hardcoded, and
    # oversized-gstack-file-skipped-without-slurp — all exercised the
    # ~/.gstack/analytics/skill-usage.jsonl reader. That reader was
    # retired with the fleet-skill-counts pivot. The events-side
    # parse-error tolerance is still covered by the remaining
    # ``test_torn_event_line_skipped`` / ``test_unreadable_file_continues``
    # tests in TestTolerantReader. Fleet-skill-coverage tests live in
    # the new TestFleetSkillsAggregation class.

    def test_legacy_retrodata_with_skipped_lines_only_renders_breadcrumb(self):
        """Backward-compat: a manually-constructed RetroData with
        skipped_lines but no skipped_per_source still surfaces a breadcrumb
        — preserves the visible-failure contract for foreign callers."""
        data = aggregator.RetroData(
            window_days=7,
            since=NOW - timedelta(days=7),
            until=NOW,
            skipped_lines=4,
        )
        out = aggregator.format_retro(data)
        assert "4 record(s) skipped" in out

    def test_window_exceeds_retention_breadcrumb(self, tmp_path):
        """`/retro-fleet 365d` → output warns that 90d retention truncates."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        data = _aggregate(events_dir, window_days=365)
        assert data.window_exceeds_retention is True
        out = aggregator.format_retro(data)
        assert "exceeds the 90-day events retention" in out

    def test_window_within_retention_no_breadcrumb(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        data = _aggregate(events_dir, window_days=7)
        assert data.window_exceeds_retention is False
        out = aggregator.format_retro(data)
        assert "exceeds" not in out


# ---------------------------------------------------------------------------
# T5 — Fleet count via `mm devices --format=json` (Architecture #3).
# ---------------------------------------------------------------------------


class TestFleetCount:
    def test_mm_devices_failure_degrades_gracefully(self, tmp_path, monkeypatch):
        """`mm devices --format=json` not on PATH → "events from N machine(s)"
        without the "of M known" tail."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(events_dir, "dev-a", "2026-04-28", [_push_event("dev-a", 0)])

        # Force the subprocess call to fail.
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("mm not found")

        monkeypatch.setattr(aggregator.subprocess, "run", fake_run)
        data = _aggregate(events_dir)
        assert data.fleet.devices_known is None
        out = aggregator.format_retro(data)
        # Surfaces in the Notes section, not inline in the header (post-v0.11.12).
        assert "Known-fleet count unavailable" in out

    def test_mm_devices_success_renders_n_of_m(self, tmp_path, monkeypatch):
        """JSON list with M devices + only N have events → "N of M"."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(events_dir, "dev-a", "2026-04-28", [_push_event("dev-a", 0)])

        # Fake `mm devices --format=json` returning a 3-device fleet.
        class FakeResult:
            returncode = 0
            stdout = json.dumps(
                [
                    {
                        "device_id": "dev-a",
                        "device_name": "Mac A",
                        "last_seen": None,
                        "last_seen_version": None,
                        "is_self": True,
                    },
                    {
                        "device_id": "dev-b",
                        "device_name": "Mac B",
                        "last_seen": None,
                        "last_seen_version": None,
                        "is_self": False,
                    },
                    {
                        "device_id": "dev-c",
                        "device_name": "Mac C",
                        "last_seen": None,
                        "last_seen_version": None,
                        "is_self": False,
                    },
                ]
            )
            stderr = ""

        def fake_run(*args, **kwargs):
            return FakeResult()

        monkeypatch.setattr(aggregator.subprocess, "run", fake_run)
        data = _aggregate(events_dir)
        assert data.fleet.devices_known == 3
        assert len(data.fleet.devices_in_events) == 1
        out = aggregator.format_retro(data)
        assert "1 of 3 known machines" in out
        # Notes section copy (post-v0.11.12 consolidation).
        assert "Fleet incomplete: 2 registered device(s)" in out

    def test_mm_devices_returncode_failure_degrades(self, tmp_path, monkeypatch):
        """Non-zero returncode (mm not initialized) degrades to None."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()

        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "Error: not initialized"

        monkeypatch.setattr(aggregator.subprocess, "run", lambda *a, **k: FakeResult())
        data = _aggregate(events_dir)
        assert data.fleet.devices_known is None

    def test_phantom_event_devices_filtered_to_registered(self, tmp_path, monkeypatch):
        """Phantom event files (from de-registered devices or pre-v0.11.10
        test leaks) MUST drop out of the rendered count when `mm devices
        --format=json` succeeds. The user complaint that drove this filter
        was a "33 machine(s) (3 currently registered)" header that read
        as broken data — the inconsistency surfaces as a Notes-section
        line, not as a noisy header banner. Stale event files age out via
        the 90-day TTL.

        REGRESSION pin: the un-registered IDs MUST count toward the
        Notes-section unregistered-device breadcrumb so the user knows
        phantom files are still on disk and reaping naturally."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # 5 distinct event-producing devices.
        for i in range(5):
            _write_events(
                events_dir,
                f"phantom-{i}",
                "2026-04-28",
                [_push_event(f"phantom-{i}", 0)],
            )

        # mm devices reports only 2 currently registered.
        class FakeResult:
            returncode = 0
            stdout = json.dumps(
                [
                    {"device_id": "phantom-0", "device_name": "A", "is_self": True},
                    {"device_id": "phantom-1", "device_name": "B", "is_self": False},
                ]
            )
            stderr = ""

        monkeypatch.setattr(aggregator.subprocess, "run", lambda *a, **k: FakeResult())
        data = _aggregate(events_dir)
        assert data.fleet.devices_known == 2
        # Filter intersects → only 2 of the 5 event-producing IDs survive.
        assert len(data.fleet.devices_in_events) == 2
        # 3 unregistered IDs remembered for the Notes-section breadcrumb.
        assert data.fleet.unregistered_event_devices == 3

        out = aggregator.format_retro(data)
        # Header reads honestly against the registered fleet.
        assert "Activity across 2 of 2 known machines" in out
        # Old noisy banner must NOT fire (the user complaint).
        assert "currently registered" not in out
        assert "Fleet inconsistency" not in out
        # Phantom-event count surfaces as a Notes-section line so the
        # user knows the disk still has stale files reaping naturally.
        assert "## Notes" in out
        assert "3 unregistered device id(s)" in out

    def test_phantom_filter_falls_back_to_raw_when_devices_unavailable(self, tmp_path, monkeypatch):
        """If `mm devices --format=json` fails, the filter MUST fall back
        to the raw event-producing set (not zero out the retro). This
        defends against transient failures wiping the entire activity
        view."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        for i in range(3):
            _write_events(
                events_dir,
                f"phantom-{i}",
                "2026-04-28",
                [_push_event(f"phantom-{i}", 0)],
            )

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("mm not found")

        monkeypatch.setattr(aggregator.subprocess, "run", fake_run)
        data = _aggregate(events_dir)
        assert data.fleet.devices_known is None
        # Falls back to raw set — all 3 IDs counted.
        assert len(data.fleet.devices_in_events) == 3
        # No phantom-count breadcrumb because we couldn't determine which
        # were unregistered.
        assert data.fleet.unregistered_event_devices == 0


# ---------------------------------------------------------------------------
# T6 — MM_EVENTS_DIR env override (CQ#2).
# ---------------------------------------------------------------------------


class TestEnvOverride:
    def test_mm_events_dir_env_overrides_default(self, tmp_path, monkeypatch):
        custom_dir = tmp_path / "custom-events"
        custom_dir.mkdir()
        monkeypatch.setenv("MM_EVENTS_DIR", str(custom_dir))
        resolved = aggregator._resolve_events_dir()
        assert resolved == custom_dir

    def test_no_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("MM_EVENTS_DIR", raising=False)
        resolved = aggregator._resolve_events_dir()
        assert resolved == aggregator.DEFAULT_EVENTS_DIR


# ---------------------------------------------------------------------------
# T7 — Retention constant matches cli.
# ---------------------------------------------------------------------------


class TestRetentionConstant:
    def test_retention_constant_matches_cli(self):
        """Aggregator hardcodes 90; mm cli hardcodes 90. If either drifts,
        the window-exceeds breadcrumb is wrong. Pin the agreement."""
        from mind_meld import cli as cli_module

        assert aggregator.EVENTS_RETENTION_DAYS == cli_module.EVENTS_RETENTION_DAYS


# ---------------------------------------------------------------------------
# T8 — Window arg parsing.
# ---------------------------------------------------------------------------


class TestWindowParsing:
    def test_valid_windows(self):
        assert aggregator._parse_window("7d") == 7
        assert aggregator._parse_window("30d") == 30
        assert aggregator._parse_window("1d") == 1

    def test_invalid_windows_raise(self):
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            aggregator._parse_window("7w")
        with pytest.raises(argparse.ArgumentTypeError):
            aggregator._parse_window("0d")
        with pytest.raises(argparse.ArgumentTypeError):
            aggregator._parse_window("-1d")
        with pytest.raises(argparse.ArgumentTypeError):
            aggregator._parse_window("seven days")


# ---------------------------------------------------------------------------
# T9 — Output rendering smoke tests.
# ---------------------------------------------------------------------------


class TestRendering:
    def test_renders_locked_format_headers(self, tmp_path):
        """Every locked-format section must appear in output."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _push_event("dev-a", 1.0),
                _git_event("dev-a", 1.0, [_commit("abc", 1.0)]),
                _sessions_event(
                    "dev-a",
                    1.0,
                    [
                        {
                            "claude_dir": "-tmp-x",
                            "sessions": 3,
                            "total_kb": 100,
                            "ephemeral": False,
                        },
                    ],
                ),
            ],
        )
        data = _aggregate(events_dir)
        out = aggregator.format_retro(data)
        assert "## Code shipped" in out
        assert "## Claude Code activity" in out
        assert "## Skills used" in out
        # D5#5 (v0.11.27): Skills section is fleet-wide, not this-machine-only.
        # Lock against accidental reintroduction of the old caveat string.
        assert "this machine only" not in out
        assert "## mm sync activity" in out
        # Eureka section was removed in v0.11.12 (always 0 in practice).
        assert "## Eureka moments" not in out

    def test_zero_events_renders_without_crashing(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        data = _aggregate(events_dir)
        out = aggregator.format_retro(data)
        # All sections present; substantive content gracefully degrades.
        assert "0 commits" in out
        assert "No Claude Code sessions captured" in out


# ---------------------------------------------------------------------------
# T10 — Custom-path notice (Group 8 hotfix #1).
# ---------------------------------------------------------------------------


class TestCustomPathNotice:
    """``mm: notice:`` for the silent-empty-retro hazard when a power user has
    a non-default ``mm-events`` source path configured but ``MM_EVENTS_DIR``
    isn't set. Library callers of ``aggregate()`` never see the notice — it
    fires only from ``main()``."""

    def test_env_set_suppresses_notice(self, monkeypatch, capsys):
        monkeypatch.setenv("MM_EVENTS_DIR", "/tmp/whatever")
        aggregator._emit_custom_path_notice_if_due(aggregator.DEFAULT_EVENTS_DIR)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_config_matches_default_no_notice(self, monkeypatch, capsys):
        monkeypatch.delenv("MM_EVENTS_DIR", raising=False)
        monkeypatch.setattr(
            aggregator,
            "_read_mm_events_config_path",
            lambda: aggregator.DEFAULT_EVENTS_DIR.parent,
        )
        aggregator._emit_custom_path_notice_if_due(aggregator.DEFAULT_EVENTS_DIR)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_config_differs_emits_notice(self, monkeypatch, capsys, tmp_path):
        """REGRESSION: the actual hotfix — non-default config path with no env
        override surfaces the notice pointing at the env var."""
        monkeypatch.delenv("MM_EVENTS_DIR", raising=False)
        custom_path = tmp_path / "custom-mm"
        monkeypatch.setattr(
            aggregator,
            "_read_mm_events_config_path",
            lambda: custom_path,
        )
        aggregator._emit_custom_path_notice_if_due(aggregator.DEFAULT_EVENTS_DIR)
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err
        assert str(custom_path) in captured.err
        assert "MM_EVENTS_DIR=" in captured.err

    def test_config_unreadable_no_notice(self, monkeypatch, capsys):
        """Tolerant reader returning None (config absent, malformed, no
        mm-events source) means the notice stays silent — no crash."""
        monkeypatch.delenv("MM_EVENTS_DIR", raising=False)
        monkeypatch.setattr(aggregator, "_read_mm_events_config_path", lambda: None)
        aggregator._emit_custom_path_notice_if_due(aggregator.DEFAULT_EVENTS_DIR)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_read_mm_events_config_path_handles_malformed_config(self, monkeypatch):
        """Direct exercise of the tolerant reader: a load_config raise
        returns None, never propagates."""

        def boom(*args, **kwargs):
            raise RuntimeError("simulated load_config failure")

        # Patch import-site so any access to load_config in the helper raises.
        import mind_meld.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "load_config", boom)
        result = aggregator._read_mm_events_config_path()
        assert result is None

    def test_disabled_mm_events_returns_none(self, monkeypatch):
        """REGRESSION (codex adversarial 2026-04-29): when
        ``[sync].disabled_sources`` contains ``mm-events``, the user has
        opted out per-machine. The reader returns None so the notice
        stays silent — nudging a user to set MM_EVENTS_DIR for a source
        they disabled fails the visible-failure contract."""
        import mind_meld.config as cfg_mod

        def fake_load_config(*args, **kwargs):
            return {
                "sync": {
                    "disabled_sources": ["mm-events"],
                    "sources": [
                        {"name": "mm-events", "path": "/Users/kb/custom-events"},
                    ],
                }
            }

        monkeypatch.setattr(cfg_mod, "load_config", fake_load_config)
        result = aggregator._read_mm_events_config_path()
        assert result is None


# ---------------------------------------------------------------------------
# T11 — source_root dedup + coalesce (Group 8 hotfix #4).
# ---------------------------------------------------------------------------


class TestSessionsSourceRoot:
    """Pin the encoded-name-collision fix: snapshots carry ``source_root`` and
    the aggregator keys ``(device, source_root, claude_dir)``. Coalesce
    drops legacy empty-source_root records when a populated sibling exists
    on the same device.

    test_two_distinct_source_roots_kept_separate is a REGRESSION-class
    pin: the actual silent-data-loss bug being fixed. Pre-fix, the same
    encoded ``claude_dir`` from two source roots silently overwrote each
    other in ``latest``."""

    def test_two_distinct_source_roots_kept_separate(self, tmp_path):
        """REGRESSION (Group 8 hotfix #4): two ``type: claude`` source roots
        that both contain a project encoded as ``-Users-kb-Documents-foo``
        must be kept as distinct entries; sessions counts must SUM across
        the two source roots, not silently overwrite."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj_a = {
            "claude_dir": "-Users-kb-Documents-foo",
            "source_root": "/Users/kb/.claude",
            "sessions": 5,
            "total_kb": 100,
            "ephemeral": False,
        }
        proj_b = {
            "claude_dir": "-Users-kb-Documents-foo",
            "source_root": "/Users/kb/work-claude",
            "sessions": 7,
            "total_kb": 200,
            "ephemeral": False,
        }
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_sessions_event("dev-a", 1.0, [proj_a, proj_b])],
        )
        data = _aggregate(events_dir)
        # Both source roots contribute distinct projects; totals sum.
        assert data.sessions.total_sessions == 12
        assert data.sessions.projects == 2

    def test_legacy_empty_only_records_kept(self, tmp_path):
        """Pre-fix records (no ``source_root`` field) without any populated
        sibling are kept — pre-upgrade fleet data must still render."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # No source_root field at all (legacy record shape)
        proj_legacy = {
            "claude_dir": "-tmp-x",
            "sessions": 5,
            "total_kb": 100,
            "ephemeral": False,
        }
        _write_events(
            events_dir,
            "dev-old",
            "2026-04-28",
            [_sessions_event("dev-old", 1.0, [proj_legacy])],
        )
        data = _aggregate(events_dir)
        assert data.sessions.total_sessions == 5
        assert data.sessions.projects == 1

    def test_legacy_empty_dropped_when_populated_sibling_for_same_device(self, tmp_path):
        """Rollout-window scenario: same device has an old record (no
        source_root) and a new record (populated source_root) for the same
        encoded project. Coalesce drops the empty key; only the populated
        record contributes."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj_legacy = {
            "claude_dir": "-tmp-x",
            "sessions": 99,  # high count to make double-counting visible if it leaks
            "total_kb": 1000,
            "ephemeral": False,
        }
        proj_populated = {
            "claude_dir": "-tmp-x",
            "source_root": "/Users/kb/.claude",
            "sessions": 5,
            "total_kb": 100,
            "ephemeral": False,
        }
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _sessions_event("dev-a", 2.0, [proj_legacy]),
                _sessions_event("dev-a", 1.0, [proj_populated]),
            ],
        )
        data = _aggregate(events_dir)
        # Legacy 99 is dropped; only the populated 5 counts.
        assert data.sessions.total_sessions == 5
        assert data.sessions.projects == 1

    def test_legacy_empty_kept_when_no_populated_sibling_anywhere(self, tmp_path):
        """Mixed-fleet, distinct projects: peer-old has only empty records
        for project X; peer-new has populated records for project Y. Empty
        record for X must NOT be dropped — there's no sibling that would
        override it."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj_old = {
            "claude_dir": "-tmp-x",
            "sessions": 3,
            "total_kb": 50,
            "ephemeral": False,
        }
        proj_new = {
            "claude_dir": "-tmp-y",
            "source_root": "/Users/kb/.claude",
            "sessions": 7,
            "total_kb": 150,
            "ephemeral": False,
        }
        _write_events(
            events_dir,
            "dev-old",
            "2026-04-28",
            [_sessions_event("dev-old", 1.0, [proj_old])],
        )
        _write_events(
            events_dir,
            "dev-new",
            "2026-04-28",
            [_sessions_event("dev-new", 1.0, [proj_new])],
        )
        data = _aggregate(events_dir)
        # Both records contribute (different projects, different devices).
        assert data.sessions.total_sessions == 10
        assert data.sessions.projects == 2

    def test_legacy_kept_when_populated_sibling_is_older(self, tmp_path):
        """REGRESSION (codex adversarial 2026-04-29): coalesce must NOT drop
        a legacy record when its populated sibling has an OLDER ts. Without
        the freshness guard, a downgrade or interleaved-fleet push leaves
        the newer data in the legacy key; an unconditional drop erases the
        active sessions and the populated sibling may itself be window-
        filtered out (last_session_at older than `since`), returning zero
        for an active project.

        Scenario: same device, same encoded project. Populated record
        pushed Tuesday 09:00 with stale last_session_at (60d ago). Legacy
        record pushed Tuesday 10:00 with active last_session_at (now).
        Pre-fix coalesce: dropped legacy → populated filtered out by
        last_session_at gate → 0 sessions despite active activity.
        Post-fix: coalesce only drops when populated is at least as fresh,
        so legacy survives and contributes its 99 active sessions.
        """
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj_legacy = {
            "claude_dir": "-tmp-x",
            "sessions": 99,
            "total_kb": 1000,
            "ephemeral": False,
            "last_session_at": _ts(0.0),  # active
        }
        proj_populated_stale = {
            "claude_dir": "-tmp-x",
            "source_root": "/Users/kb/.claude",
            "sessions": 5,
            "total_kb": 100,
            "ephemeral": False,
            "last_session_at": _ts(60.0),  # outside any reasonable window
        }
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                # Populated record OLDER than legacy
                _sessions_event("dev-a", 1.5, [proj_populated_stale]),
                # Legacy record NEWER than populated (interleaved-fleet
                # or downgrade scenario)
                _sessions_event("dev-a", 1.0, [proj_legacy]),
            ],
        )
        data = _aggregate(events_dir, window_days=7)
        # Legacy survives the coalesce; its 99 sessions contribute.
        # Populated stale sibling is window-filtered out.
        assert data.sessions.total_sessions == 99
        assert data.sessions.projects == 1


# ---------------------------------------------------------------------------
# v0.11.9 — author email broadening via per-repo committer scan.
# ---------------------------------------------------------------------------


class TestGatherAuthorEmails:
    """`gather_author_emails` is trust-rooted: it ONLY returns emails
    from configured identities on machines the user controls (global git
    config, per-repo overrides, manual config, gh-derived noreply form).
    It deliberately does NOT walk `git log` to harvest emails — that
    would pull in collaborator emails from shared repos and silently
    inflate retros with their work as the user's.

    Pre-v0.11.9 only the global + manual sources were considered; PR-
    merged commits authored as `<id>+<login>@users.noreply.github.com`
    silently fell out of the filter. v0.11.9 adds per-repo overrides
    (for users with multiple identities configured locally) and the
    gh-derived noreply form (for the bulk of PR-merge activity).
    """

    def _stub_subprocess(self, monkeypatch, handlers):
        """Build a fake `subprocess.run` that dispatches by cmd prefix.

        ``handlers`` is a dict mapping a tuple cmd prefix (e.g.
        ``("git", "config")``) to either a (returncode, stdout) tuple
        or an exception class to raise. Unmatched commands return
        rc=1 / empty stdout.
        """
        import subprocess as _subprocess

        class FakeResult:
            def __init__(self, returncode, stdout):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = ""

        def fake_run(cmd, **_kw):
            for prefix, response in handlers.items():
                if tuple(cmd[: len(prefix)]) == prefix:
                    if isinstance(response, type) and issubclass(response, Exception):
                        raise response("simulated")
                    rc, out = response
                    return FakeResult(rc, out)
            return FakeResult(1, "")

        monkeypatch.setattr(_subprocess, "run", fake_run)

    def _stub_repos(self, monkeypatch, roots):
        """Stub discover_git_roots + load_config so the gatherer
        operates on synthetic repo paths instead of the real filesystem."""
        from mind_meld import config as config_module
        from mind_meld import events as events_module
        from mind_meld import identity as identity_module

        monkeypatch.setattr(events_module, "discover_git_roots", lambda _cfg: (roots, []))
        monkeypatch.setattr(config_module, "load_config", lambda _p: {})
        # v0.11.17: gather logic moved from aggregator.py to identity.py.
        # The mm config.toml [retro].author_emails read lives here now.
        monkeypatch.setattr(identity_module, "_gather_config_author_emails", lambda: [])

    def test_per_repo_overrides_unioned_with_global(self, monkeypatch):
        """Per-repo `git config user.email` overrides land in the trust
        set alongside the global. Captures the case where a user
        configures a different identity for specific repos (e.g.,
        dotfiles repo using a personal email where global is work)."""
        self._stub_repos(monkeypatch, [Path("/fake/repo-a"), Path("/fake/repo-b")])
        self._stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("git", "-C", "/fake/repo-a", "config"): (0, "kb@example.com\n"),
                ("git", "-C", "/fake/repo-b", "config"): (0, "kb-work@example.com\n"),
                # `gh api user` returns auth error → no noreply form.
                ("gh", "api"): (1, ""),
            },
        )
        emails = aggregator.gather_author_emails()
        assert "kb@example.com" in emails
        assert "kb-work@example.com" in emails

    def test_collaborator_email_in_shared_repo_history_NOT_included(self, monkeypatch, tmp_path):
        """**Trust-rooted regression pin.** A shared repo where a
        collaborator has commits in the local history must NOT leak
        their email into the trust set. Pre-v0.11.9-rc2 a broad
        `git log --format=%ae%n%ce` walk did exactly this — and an
        intermediate v0.11.9-rc design retained the walk before being
        reverted to this trust-rooted shape.

        Setup: a real local git repo with two commits, one by the user
        and one by a collaborator. The walk MUST NOT scan log output;
        it MUST only read configured `git config user.email`.
        """
        import subprocess as real_subprocess

        # Use a real git repo so we'd actually pick up alice's email if
        # the implementation regressed to walking commits.
        repo = tmp_path / "shared-repo"
        repo.mkdir()
        real_subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        real_subprocess.run(["git", "config", "user.email", "kb@example.com"], cwd=repo, check=True)
        real_subprocess.run(["git", "config", "user.name", "KB"], cwd=repo, check=True)
        real_subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
        (repo / "a.txt").write_text("a")
        real_subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        real_subprocess.run(["git", "commit", "-q", "-m", "kb commit"], cwd=repo, check=True)

        # Collaborator's commit in the same repo (e.g., pulled from upstream).
        env = {
            **__import__("os").environ,
            "GIT_AUTHOR_EMAIL": "alice@collaborator.com",
            "GIT_AUTHOR_NAME": "Alice",
            "GIT_COMMITTER_EMAIL": "alice@collaborator.com",
            "GIT_COMMITTER_NAME": "Alice",
        }
        (repo / "b.txt").write_text("b")
        real_subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        real_subprocess.run(
            ["git", "commit", "-q", "-m", "alice commit"], cwd=repo, env=env, check=True
        )

        # Sanity: alice IS in the log.
        log = real_subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%ae"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "alice@collaborator.com" in log.stdout

        # Now stub the gatherer's repo discovery to return our shared repo.
        self._stub_repos(monkeypatch, [repo])
        # Don't stub subprocess — let real git run. Stub only `gh api`
        # (no auth in the test env) and the `--global` lookup so the
        # test machine's real global doesn't bleed in.
        original_run = real_subprocess.run

        def fake_run(cmd, **kw):
            if tuple(cmd[:3]) == ("git", "config", "--global"):
                # Return a synthetic global so the trust set is well-defined.

                class R:
                    returncode = 0
                    stdout = "kb@example.com\n"
                    stderr = ""

                return R()
            if tuple(cmd[:2]) == ("gh", "api"):

                class R:
                    returncode = 1
                    stdout = ""
                    stderr = ""

                return R()
            return original_run(cmd, **kw)

        monkeypatch.setattr(real_subprocess, "run", fake_run)

        emails = aggregator.gather_author_emails()
        # User's identity is in the set.
        assert "kb@example.com" in emails
        # Collaborator's email IS in the local git log — and MUST NOT
        # leak into the trust set. This is the load-bearing assertion.
        assert "alice@collaborator.com" not in emails

    def test_gh_noreply_email_added_when_authenticated(self, monkeypatch):
        """`gh api user` returning {"id": 99999, "login": "fakeuser"}
        derives `99999+fakeuser@users.noreply.github.com` and unions it
        into the trust set. Critical for users who land most work via
        PR-merge through GitHub's web UI (where author = the per-user
        noreply form regardless of local git config)."""
        self._stub_repos(monkeypatch, [])
        self._stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (0, '{"id": 99999, "login": "fakeuser"}'),
            },
        )
        emails = aggregator.gather_author_emails()
        assert "kb@example.com" in emails
        assert "99999+fakeuser@users.noreply.github.com" in emails

    def test_gh_unavailable_falls_back_silently(self, monkeypatch):
        """Missing `gh` binary → FileNotFoundError → no noreply entry,
        rest of the trust set still populated. No noisy stderr."""
        self._stub_repos(monkeypatch, [])

        class FakeResult:
            def __init__(self, rc, out):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        import subprocess as _subprocess

        def fake_run(cmd, **_kw):
            if tuple(cmd[:2]) == ("gh", "api"):
                raise FileNotFoundError("gh: command not found")
            if tuple(cmd[:3]) == ("git", "config", "--global"):
                return FakeResult(0, "kb@example.com\n")
            return FakeResult(1, "")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        emails = aggregator.gather_author_emails()
        assert "kb@example.com" in emails
        assert all("noreply.github.com" not in e for e in emails)

    def test_gh_unauthenticated_falls_back_silently(self, monkeypatch):
        """`gh api user` rc != 0 (typical of unauthenticated gh) →
        no noreply entry, no exception."""
        self._stub_repos(monkeypatch, [])
        self._stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = aggregator.gather_author_emails()
        assert "kb@example.com" in emails
        assert all("noreply.github.com" not in e for e in emails)

    def test_gh_malformed_json_returns_none(self, monkeypatch):
        """A `gh` binary that returns non-JSON to `gh api user` (auth
        warning printed to stdout, etc.) must not crash the gather."""
        self._stub_repos(monkeypatch, [])
        self._stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (0, "<<<not json>>>"),
            },
        )
        emails = aggregator.gather_author_emails()
        assert "kb@example.com" in emails
        assert all("noreply.github.com" not in e for e in emails)

    def test_gh_unexpected_shape_returns_none(self, monkeypatch):
        """Missing/wrong-typed `id` or `login` fields in the gh response
        → no noreply entry. Defends against gh API shape drift."""
        self._stub_repos(monkeypatch, [])
        self._stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (0, '{"id": "not-an-int", "login": "fakeuser"}'),
            },
        )
        emails = aggregator.gather_author_emails()
        assert "kb@example.com" in emails
        assert all("noreply.github.com" not in e for e in emails)

    def test_no_repos_discovered_returns_global_plus_gh(self, monkeypatch):
        """Empty discover_git_roots → only the global + gh sources
        contribute. Falls back cleanly without exception."""
        self._stub_repos(monkeypatch, [])
        self._stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = aggregator.gather_author_emails()
        assert emails == frozenset({"kb@example.com"})

    def test_per_repo_failure_skipped_silently(self, monkeypatch):
        """A single repo's `git config user.email` failing skips that
        repo and continues. No noisy stderr."""
        self._stub_repos(monkeypatch, [Path("/fake/good"), Path("/fake/bad")])
        self._stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("git", "-C", "/fake/good", "config"): (0, "kb-personal@example.com\n"),
                ("git", "-C", "/fake/bad", "config"): (128, ""),
                ("gh", "api"): (1, ""),
            },
        )
        emails = aggregator.gather_author_emails()
        assert "kb@example.com" in emails
        assert "kb-personal@example.com" in emails

    def test_per_repo_scan_respects_wall_clock_budget(self, monkeypatch):
        """When the budget is exhausted partway through the walk, the
        function returns what was collected so far instead of running
        unbounded. Bounded scan keeps the retro from becoming a
        multi-second wait when the user has many discovered repos on
        a slow filesystem."""
        from mind_meld import identity as identity_module

        roots = [Path(f"/fake/repo-{i}") for i in range(100)]
        self._stub_repos(monkeypatch, roots)
        # Force a tiny budget so the loop bails after ~one iteration.
        # v0.11.17: budget constant moved with gather logic to identity.py.
        monkeypatch.setattr(identity_module, "_PER_REPO_BUDGET_S", 0.001)

        scanned: list[str] = []

        class FakeResult:
            def __init__(self, rc, stdout):
                self.returncode = rc
                self.stdout = stdout
                self.stderr = ""

        def fake_run(cmd, **_kw):
            if tuple(cmd[:3]) == ("git", "config", "--global"):
                return FakeResult(0, "kb@example.com\n")
            if tuple(cmd[:2]) == ("gh", "api"):
                return FakeResult(1, "")
            if tuple(cmd[:2]) == ("git", "-C"):
                scanned.append(cmd[2])
                import time as _time

                _time.sleep(0.005)
                return FakeResult(0, "kb-personal@example.com\n")
            return FakeResult(1, "")

        import subprocess as _subprocess

        monkeypatch.setattr(_subprocess, "run", fake_run)

        emails = aggregator.gather_author_emails()
        assert "kb@example.com" in emails
        assert len(scanned) < 100, f"budget enforcement failed: scanned all {len(scanned)} repos"

    def test_config_load_failure_falls_back_silently(self, monkeypatch):
        """Missing / malformed config.toml in
        ``identity._gather_per_repo_emails`` → return empty set, no
        exception. The other gather sources still contribute."""
        from mind_meld import config as config_module
        from mind_meld import identity as identity_module

        def boom(_p):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(config_module, "load_config", boom)
        monkeypatch.setattr(identity_module, "_gather_config_author_emails", lambda: [])
        self._stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = aggregator.gather_author_emails()
        assert emails == frozenset({"kb@example.com"})


class TestGitAggregationWithBroadenedFilter:
    """End-to-end check that PR-merge commits authored under the noreply
    form pass the filter when the email set includes the noreply alias
    (which `gather_author_emails` now picks up automatically from per-repo
    committer scans)."""

    def test_noreply_commits_counted_when_alias_in_filter(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        direct = _commit("aaa", 1.0, author_email="kb@example.com")
        merged = _commit("bbb", 1.0, author_email="99999+fakeuser@users.noreply.github.com")
        unrelated = _commit("ccc", 1.0, author_email="bot@example.com")
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _git_event("dev-a", 1.0, [direct, merged, unrelated]),
            ],
        )
        # Filter set includes both forms — PR-merge commits stop falling out.
        data = _aggregate(
            events_dir,
            author_emails=frozenset(
                {
                    "kb@example.com",
                    "99999+fakeuser@users.noreply.github.com",
                }
            ),
        )
        assert data.git.commits == 2
        assert data.git.additions == 20  # 2 commits × 10 each (default)


# ---------------------------------------------------------------------------
# Token aggregation + rendering (v0.11.14+)
# ---------------------------------------------------------------------------


class TestTokenAggregation:
    def _make_sessions_event(
        self,
        device,
        ts,
        *,
        tokens_by_day=None,
        last_session_at=None,
        sessions=1,
    ):
        proj = {
            "claude_dir": "-tmp-proj",
            "source_root": "/Users/kb/.claude",
            "sessions": sessions,
            "total_kb": 100,
            "last_session_at": last_session_at or ts,
        }
        if tokens_by_day is not None:
            proj["tokens_by_day"] = tokens_by_day
        return {
            "v": 2,
            "type": "sessions-snapshot",
            "ts": ts,
            "device": device,
            "projects": [proj],
        }

    def test_no_tokens_means_pre_token_peer(self):
        from mind_meld.skills.retro_fleet.aggregator import aggregate_sessions

        ev = self._make_sessions_event(
            "dev-a",
            "2026-05-01T12:00:00+00:00",
            tokens_by_day=None,
            sessions=1,
        )
        result, _skills_unused = aggregate_sessions(
            [ev],
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
        assert "dev-a" in result.pre_token_peers
        assert result.tokens_input == 0

    def test_tokens_summed_across_window_days(self):
        from mind_meld.skills.retro_fleet.aggregator import aggregate_sessions

        ev = self._make_sessions_event(
            "dev-a",
            "2026-05-01T12:00:00+00:00",
            tokens_by_day={
                "2026-04-29": {
                    "input": 10,
                    "cache_create": 0,
                    "cache_read": 100,
                    "output": 5,
                    "by_model": {
                        "claude-opus-4-7": {
                            "input": 10,
                            "cache_create": 0,
                            "cache_read": 100,
                            "output": 5,
                        }
                    },
                },
                "2026-05-01": {
                    "input": 30,
                    "cache_create": 0,
                    "cache_read": 300,
                    "output": 15,
                    "by_model": {
                        "claude-sonnet-4-6": {
                            "input": 30,
                            "cache_create": 0,
                            "cache_read": 300,
                            "output": 15,
                        }
                    },
                },
            },
        )
        result, _skills_unused = aggregate_sessions(
            [ev],
            since=datetime(2026, 4, 29, tzinfo=timezone.utc),
            until=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
        assert result.tokens_input == 40  # 10 + 30
        assert result.tokens_cache_read == 400
        assert "claude-opus-4-7" in result.tokens_by_model
        assert "claude-sonnet-4-6" in result.tokens_by_model

    def test_tokens_outside_window_excluded(self):
        from mind_meld.skills.retro_fleet.aggregator import aggregate_sessions

        ev = self._make_sessions_event(
            "dev-a",
            "2026-05-01T12:00:00+00:00",
            tokens_by_day={
                "2026-01-01": {  # way outside window
                    "input": 999,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                    "by_model": {},
                },
            },
            last_session_at="2026-05-01T12:00:00+00:00",  # in window
        )
        result, _skills_unused = aggregate_sessions(
            [ev],
            since=datetime(2026, 4, 29, tzinfo=timezone.utc),
            until=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
        assert result.tokens_input == 0  # outside-window day excluded
        # Project still counted as session (last_session_at is in window).
        # The tokens_by_day field IS present (just no in-window day matches),
        # so this device does NOT get flagged as pre_token_peer — that's the
        # right call for a token-aware peer with zero in-window activity.
        assert "dev-a" not in result.pre_token_peers


class TestTokenBlockRender:
    def _data_with_tokens(self, **overrides):
        from mind_meld.skills.retro_fleet.aggregator import (
            RetroData,
            SessionsAggregate,
        )

        data = RetroData(
            window_days=7,
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        data.sessions = SessionsAggregate(
            total_sessions=17,
            projects=4,
            tokens_input=12_400_000,
            tokens_cache_read=87_300_000,
            tokens_cache_create=10_000_000,
            tokens_output=142_000,
            tokens_by_model={
                "claude-sonnet-4-6": {
                    "input": 4_000_000,
                    "cache_create": 0,
                    "cache_read": 50_000_000,
                    "output": 100_000,
                },
                "claude-opus-4-7": {
                    "input": 8_400_000,
                    "cache_create": 10_000_000,
                    "cache_read": 37_300_000,
                    "output": 42_000,
                },
            },
            **overrides,
        )
        return data

    def test_render_includes_token_lines(self):
        from mind_meld.skills.retro_fleet.aggregator import format_retro

        data = self._data_with_tokens()
        out = format_retro(data)
        assert "Tokens this window:" in out
        assert "12.4M in" in out
        assert "87.3M cache_read" in out
        assert "Cache hit ratio:" in out
        assert "Estimated cost:" in out
        assert "Per-model:" in out
        assert "Sonnet 4.6" in out
        assert "Opus 4.7" in out
        # Subscription caveat as italicized footer.
        assert "Cost estimates do not account for subscription plan pricing." in out

    def test_render_hidden_when_no_tokens(self):
        from mind_meld.skills.retro_fleet.aggregator import (
            RetroData,
            SessionsAggregate,
            format_retro,
        )

        data = RetroData(
            window_days=7,
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        data.sessions = SessionsAggregate(total_sessions=5, projects=1)
        out = format_retro(data)
        # Token block lines absent.
        assert "Tokens this window:" not in out
        assert "Cache hit ratio:" not in out
        # But the section is still present with sessions count.
        assert "5 sessions" in out
        # Projects-count line dropped (worktrees + Conductor workspaces
        # inflated it) — repo count under Code shipped covers the signal.
        assert "across 1 projects" not in out

    def test_pre_token_peers_breadcrumb_in_notes(self):
        from mind_meld.skills.retro_fleet.aggregator import format_retro

        data = self._data_with_tokens()
        data.sessions.pre_token_peers = {"dev-mac-mini"}
        out = format_retro(data)
        assert "Tokens incomplete: 1 peer(s)" in out


class TestSyntheticAndUnpricedTokens:
    """v0.11.22: displayed token totals share the cost-estimate basis.

    Synthetic tool-execution turns are excluded from both totals and
    cost. Unpriced models stay in the displayed total but are surfaced
    in Notes so the cost line is honestly an under-estimate."""

    def _make_sessions_event(self, *, tokens_by_day):
        return {
            "v": 2,
            "type": "sessions-snapshot",
            "ts": "2026-05-01T12:00:00+00:00",
            "device": "dev-a",
            "projects": [
                {
                    "claude_dir": "-tmp-proj",
                    "source_root": "/Users/kb/.claude",
                    "sessions": 1,
                    "total_kb": 100,
                    "last_session_at": "2026-05-01T12:00:00+00:00",
                    "tokens_by_day": tokens_by_day,
                }
            ],
        }

    def test_synthetic_excluded_from_top_level_totals(self):
        from mind_meld.skills.retro_fleet.aggregator import aggregate_sessions

        ev = self._make_sessions_event(
            tokens_by_day={
                "2026-05-01": {
                    "input": 0,  # top-level intentionally wrong to prove we
                    "cache_create": 0,  # derive totals from by_model now
                    "cache_read": 0,
                    "output": 0,
                    "by_model": {
                        "claude-sonnet-4-6": {
                            "input": 100,
                            "cache_create": 0,
                            "cache_read": 1000,
                            "output": 50,
                        },
                        "<synthetic>": {
                            "input": 999_999,
                            "cache_create": 0,
                            "cache_read": 999_999,
                            "output": 999_999,
                        },
                    },
                }
            }
        )
        result, _skills_unused = aggregate_sessions(
            [ev],
            since=datetime(2026, 4, 29, tzinfo=timezone.utc),
            until=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
        # Synthetic must NOT contribute to the displayed totals — they
        # represent Claude Code's internal tool-execution turns that don't
        # bill against the API.
        assert result.tokens_input == 100
        assert result.tokens_cache_read == 1000
        assert result.tokens_output == 50
        # tokens_by_model still retains synthetic so the per-render filter
        # at format_retro can see it (and so cost/unpriced summaries can
        # operate on the full set).
        assert "<synthetic>" in result.tokens_by_model
        assert result.tokens_by_model["<synthetic>"]["input"] == 999_999

    def test_unpriced_model_note_surfaces_in_render(self):
        from mind_meld.skills.retro_fleet.aggregator import (
            RetroData,
            SessionsAggregate,
            format_retro,
        )

        data = RetroData(
            window_days=7,
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        data.sessions = SessionsAggregate(
            total_sessions=3,
            projects=1,
            tokens_input=4_000_000,
            tokens_cache_read=50_000_000,
            tokens_output=100_000,
            tokens_by_model={
                "claude-sonnet-4-6": {
                    "input": 4_000_000,
                    "cache_create": 0,
                    "cache_read": 50_000_000,
                    "output": 100_000,
                },
                # Hypothetical older / newer model not in PRICING.
                "claude-sonnet-3-7": {
                    "input": 1_000_000,
                    "cache_create": 0,
                    "cache_read": 5_000_000,
                    "output": 50_000,
                },
            },
        )
        out = format_retro(data)
        # The note names the volume + model count and explains the cost
        # gap.  Compact-format token count surfaces (6.0M from input + cr
        # + output of the unpriced sonnet-3-7 entry).
        assert "unpriced" in out
        assert "1 unpriced model(s)" in out
        assert "excluded from cost estimate" in out

    def test_no_unpriced_note_when_all_models_priced(self):
        from mind_meld.skills.retro_fleet.aggregator import (
            RetroData,
            SessionsAggregate,
            format_retro,
        )

        data = RetroData(
            window_days=7,
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        data.sessions = SessionsAggregate(
            total_sessions=3,
            projects=1,
            tokens_input=4_000_000,
            tokens_cache_read=50_000_000,
            tokens_output=100_000,
            tokens_by_model={
                "claude-sonnet-4-6": {
                    "input": 4_000_000,
                    "cache_create": 0,
                    "cache_read": 50_000_000,
                    "output": 100_000,
                },
            },
        )
        out = format_retro(data)
        assert "unpriced" not in out

    def test_synthetic_alone_does_not_trigger_unpriced_note(self):
        """Synthetic is cost-excluded by design, not unpriced. A fleet whose
        only non-priced model is ``<synthetic>`` must NOT surface an
        unpriced-model note."""
        from mind_meld.skills.retro_fleet.aggregator import (
            RetroData,
            SessionsAggregate,
            format_retro,
        )

        data = RetroData(
            window_days=7,
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        data.sessions = SessionsAggregate(
            total_sessions=1,
            projects=1,
            tokens_input=100,
            tokens_by_model={
                "claude-sonnet-4-6": {
                    "input": 100,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                },
                "<synthetic>": {
                    "input": 999,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                },
            },
        )
        out = format_retro(data)
        assert "unpriced" not in out


class TestTrack10ASafeIntRetention:
    """REGRESSION pin (cross-model tension #3, /plan-eng-review 2026-05-06):
    Track 10A's helper extraction must NOT leak the token_usage helpers
    into aggregator's peer-controlled merge loop. The aggregator keeps
    its bespoke loop with `_safe_int` hardening; a shared helper from
    token_usage would do raw `target[k] += src.get(k, 0)` and crash on
    a string-typed token field from a malformed peer event."""

    def _make_sessions_event(self, device, ts, tokens_by_day):
        return {
            "v": 2,
            "type": "sessions-snapshot",
            "ts": ts,
            "device": device,
            "projects": [
                {
                    "claude_dir": "-tmp-proj",
                    "source_root": "/Users/kb/.claude",
                    "sessions": 1,
                    "total_kb": 100,
                    "last_session_at": ts,
                    "tokens_by_day": tokens_by_day,
                }
            ],
        }

    def test_string_typed_token_field_does_not_crash(self):
        """A peer's malformed event with a string-typed token field
        (e.g. `"input": "abc"` instead of `100`) must NOT crash
        aggregation. `_safe_int` coerces non-int values to 0."""
        from mind_meld.skills.retro_fleet.aggregator import aggregate_sessions

        ev = self._make_sessions_event(
            "evil-peer",
            "2026-05-01T12:00:00+00:00",
            tokens_by_day={
                "2026-05-01": {
                    "input": 0,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                    "by_model": {
                        "claude-opus-4-7": {
                            "input": "not-a-number",  # malformed
                            "cache_create": [1, 2, 3],  # malformed
                            "cache_read": None,  # malformed
                            "output": 50,  # legit
                        }
                    },
                }
            },
        )
        result, _skills_unused = aggregate_sessions(
            [ev],
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
        # Malformed fields coerce to 0; legitimate field counted.
        assert result.tokens_input == 0
        assert result.tokens_cache_create == 0
        assert result.tokens_cache_read == 0
        assert result.tokens_output == 50
        # Per-model bucket retains the model with the partial data.
        assert "claude-opus-4-7" in result.tokens_by_model
        assert result.tokens_by_model["claude-opus-4-7"]["output"] == 50


class TestTrack10AFleetRetroDeterminism:
    """REGRESSION pin (D6, /plan-eng-review 2026-05-06): the Track 10A
    refactor must not change retro output for fixed inputs. Pinned with
    BOTH a per-total numerical assertion (catches arithmetic drift) AND
    a small synthetic events fixture exercising 2 devices × 2 models ×
    multiple days."""

    def test_per_total_aggregation_byte_identical(self):
        """Two devices, two models, three days. Assert exact totals
        across `tokens_input` / `tokens_cache_create` / `tokens_cache_read`
        / `tokens_output` and the per-model breakdown. Updates here ARE
        meaningful semantic changes; the test should be updated
        deliberately."""
        from mind_meld.skills.retro_fleet.aggregator import aggregate_sessions

        # Device A — Opus
        ev_a = {
            "v": 2,
            "type": "sessions-snapshot",
            "ts": "2026-05-01T12:00:00+00:00",
            "device": "dev-a",
            "projects": [
                {
                    "claude_dir": "-proj-a",
                    "source_root": "/Users/kb/.claude",
                    "sessions": 5,
                    "total_kb": 1000,
                    "last_session_at": "2026-05-01T12:00:00+00:00",
                    "tokens_by_day": {
                        "2026-04-29": {
                            "input": 100,
                            "cache_create": 50,
                            "cache_read": 1000,
                            "output": 200,
                            "by_model": {
                                "claude-opus-4-7": {
                                    "input": 100,
                                    "cache_create": 50,
                                    "cache_read": 1000,
                                    "output": 200,
                                }
                            },
                        },
                        "2026-04-30": {
                            "input": 200,
                            "cache_create": 0,
                            "cache_read": 2000,
                            "output": 100,
                            "by_model": {
                                "claude-opus-4-7": {
                                    "input": 200,
                                    "cache_create": 0,
                                    "cache_read": 2000,
                                    "output": 100,
                                }
                            },
                        },
                    },
                }
            ],
        }
        # Device B — Sonnet + Haiku, partial overlap day
        ev_b = {
            "v": 2,
            "type": "sessions-snapshot",
            "ts": "2026-05-01T12:00:00+00:00",
            "device": "dev-b",
            "projects": [
                {
                    "claude_dir": "-proj-b",
                    "source_root": "/Users/kb/.claude",
                    "sessions": 3,
                    "total_kb": 500,
                    "last_session_at": "2026-05-01T12:00:00+00:00",
                    "tokens_by_day": {
                        "2026-04-30": {
                            "input": 50,
                            "cache_create": 25,
                            "cache_read": 500,
                            "output": 75,
                            "by_model": {
                                "claude-sonnet-4-6": {
                                    "input": 50,
                                    "cache_create": 25,
                                    "cache_read": 500,
                                    "output": 75,
                                }
                            },
                        },
                        "2026-05-01": {
                            "input": 25,
                            "cache_create": 0,
                            "cache_read": 250,
                            "output": 50,
                            "by_model": {
                                "claude-haiku-4-5": {
                                    "input": 25,
                                    "cache_create": 0,
                                    "cache_read": 250,
                                    "output": 50,
                                }
                            },
                        },
                    },
                }
            ],
        }

        result, _skills_unused = aggregate_sessions(
            [ev_a, ev_b],
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )

        # Per-total assertions — these are the load-bearing numbers.
        assert result.tokens_input == 100 + 200 + 50 + 25  # 375
        assert result.tokens_cache_create == 50 + 0 + 25 + 0  # 75
        assert result.tokens_cache_read == 1000 + 2000 + 500 + 250  # 3750
        assert result.tokens_output == 200 + 100 + 75 + 50  # 425

        # Per-model breakdown.
        assert result.tokens_by_model["claude-opus-4-7"] == {
            "input": 300,
            "cache_create": 50,
            "cache_read": 3000,
            "output": 300,
        }
        assert result.tokens_by_model["claude-sonnet-4-6"] == {
            "input": 50,
            "cache_create": 25,
            "cache_read": 500,
            "output": 75,
        }
        assert result.tokens_by_model["claude-haiku-4-5"] == {
            "input": 25,
            "cache_create": 0,
            "cache_read": 250,
            "output": 50,
        }

    def test_cost_excluded_synthetic_filters_top_level_only(self):
        """REGRESSION pin: the aggregator's bespoke filtered loop
        (D1, /plan-eng-review) keeps `<synthetic>` out of the
        top-level totals BUT preserves it in `tokens_by_model` so the
        unpriced-tokens breadcrumb can surface volume."""
        from mind_meld.skills.retro_fleet.aggregator import aggregate_sessions

        ev = {
            "v": 2,
            "type": "sessions-snapshot",
            "ts": "2026-05-01T12:00:00+00:00",
            "device": "dev-a",
            "projects": [
                {
                    "claude_dir": "-proj-a",
                    "source_root": "/Users/kb/.claude",
                    "sessions": 1,
                    "total_kb": 100,
                    "last_session_at": "2026-05-01T12:00:00+00:00",
                    "tokens_by_day": {
                        "2026-04-30": {
                            "input": 1000,
                            "cache_create": 0,
                            "cache_read": 0,
                            "output": 500,
                            "by_model": {
                                "claude-opus-4-7": {
                                    "input": 100,
                                    "cache_create": 0,
                                    "cache_read": 0,
                                    "output": 50,
                                },
                                "<synthetic>": {
                                    "input": 900,
                                    "cache_create": 0,
                                    "cache_read": 0,
                                    "output": 450,
                                },
                            },
                        },
                    },
                }
            ],
        }
        result, _skills_unused = aggregate_sessions(
            [ev],
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
        # Top-level totals exclude synthetic.
        assert result.tokens_input == 100
        assert result.tokens_output == 50
        # tokens_by_model retains synthetic for unpriced breadcrumb.
        assert "<synthetic>" in result.tokens_by_model
        assert result.tokens_by_model["<synthetic>"]["input"] == 900
        assert result.tokens_by_model["claude-opus-4-7"]["input"] == 100


class TestShortenRepoUrl:
    """Render-only compression of long canonical URLs."""

    def test_short_url_passthrough(self):
        # Typical github URL stays untouched.
        assert aggregator._shorten_repo_url("github.com/foo/bar") == "github.com/foo/bar"

    def test_three_segment_under_threshold_passthrough(self):
        # gitlab-style nested group, still well under threshold.
        url = "gitlab.com/group/subgroup/repo"
        assert aggregator._shorten_repo_url(url) == url

    def test_github_long_url_passthrough(self):
        # Worst-case canonical GitHub URL — long org + long repo, well over the
        # 60-char length threshold. A 2-path-segment host has nothing
        # meaningful to compress between host and repo, so it must stay intact
        # regardless of length.
        url = "github.com/really-long-organization-name/really-long-repository-name-here"
        assert len(url) > aggregator._REPO_URL_MAX_LEN  # gate the test against the constant
        assert aggregator._shorten_repo_url(url) == url

    def test_bitbucket_long_url_passthrough(self):
        # Same shape as GitHub — host/org/repo. Pin so a future tightening of
        # the rule doesn't accidentally start trimming bitbucket URLs.
        url = "bitbucket.org/some-organization-name/some-fairly-long-repository-name"
        assert len(url) > aggregator._REPO_URL_MAX_LEN
        assert aggregator._shorten_repo_url(url) == url

    def test_gitlab_long_nested_group_compresses(self):
        # GitLab's 3-path-segment nested-group shape (group/subgroup/repo) IS
        # eligible for compression once it crosses the length threshold —
        # documented behavior, called out so a reader of this test sees the
        # boundary explicitly.
        url = "gitlab.example.com/very-long-group-name/very-long-subgroup-name/repo-name"
        assert len(url) > aggregator._REPO_URL_MAX_LEN
        assert aggregator._shorten_repo_url(url) == "gitlab.example.com/[...]/repo-name"

    def test_long_url_with_uuid_segment_compresses_middle(self):
        # Enterprise-style URL: 5 parts (host + 4 path segments), one of which
        # is a UUID-shaped repository identifier. Synthetic host/path —
        # exercises the rule without leaking any specific tenant or repo.
        url = "git.example.com/org/team/00000000-0000-0000-0000-000000000000/Some-Long-Repo-Name"
        assert aggregator._shorten_repo_url(url) == "git.example.com/[...]/Some-Long-Repo-Name"

    def test_empty_string_passthrough(self):
        assert aggregator._shorten_repo_url("") == ""

    def test_two_segment_long_passthrough(self):
        # Single-path-segment URL has nothing meaningful to compress.
        url = "host.example.com/" + "x" * 200
        assert aggregator._shorten_repo_url(url) == url

    def test_format_retro_renders_shortened_url(self):
        long_url = (
            "git.example.com/org/team/00000000-0000-0000-0000-000000000000/Some-Long-Repo-Name"
        )
        data = aggregator.RetroData(
            window_days=7,
            since=NOW - timedelta(days=7),
            until=NOW,
        )
        data.git = aggregator.GitAggregate(
            commits=3,
            additions=10,
            deletions=2,
            repos_by_count={long_url: 3},
        )
        out = aggregator.format_retro(data)
        assert "git.example.com/[...]/Some-Long-Repo-Name" in out
        # The UUID-shaped middle segment must NOT survive into the rendered output.
        assert "00000000-0000-0000-0000-000000000000" not in out
        # But the dedup key in the data is preserved (canonical, not shortened).
        assert long_url in data.git.repos_by_count


class TestSafeRepoUrl:
    """Render-time defang of peer-controlled canonical_remote_url.

    Same trust-boundary class as v0.11.14's model-string sanitization
    (residual gap that was missed during that sweep). ``canonicalize_remote_url``
    preserves ANSI / OSC / DCS escape sequences and bell characters; without
    render-time defang they survive into the LLM-consumed retro markdown.
    """

    def test_clean_url_passthrough(self):
        assert aggregator._safe_repo_url("github.com/foo/bar") == "github.com/foo/bar"

    def test_csi_escape_stripped(self):
        # ANSI color CSI sequence — most common terminal-escape vector.
        evil = "github.com/\x1b[31mfoo/\x1b[0mbar"
        out = aggregator._safe_repo_url(evil)
        assert "\x1b" not in out
        assert out == "github.com/foo/bar"

    def test_osc_52_clipboard_escape_stripped(self):
        # OSC 52 is the clipboard-write escape — silent clipboard takeover
        # vector. strip_terminal_escapes handles the full OSC grammar.
        evil = "github.com/foo/\x1b]52;c;ZXZpbA==\x07bar"
        out = aggregator._safe_repo_url(evil)
        assert "\x1b" not in out
        assert "\x07" not in out

    def test_bell_and_control_bytes_bucketed(self):
        # Bare control bytes (no ESC prefix) survive strip_terminal_escapes
        # but get bucketed to "_" by the whitelist.
        evil = "github.com/foo\x07bar/baz"
        out = aggregator._safe_repo_url(evil)
        assert "\x07" not in out
        assert "_" in out

    def test_markdown_breakers_bucketed(self):
        # Newlines, backticks, brackets, pipes — all break markdown table /
        # list rendering and could confuse an LLM consumer.
        evil = "github.com/foo`evil`/bar\n## INJECTED"
        out = aggregator._safe_repo_url(evil)
        assert "`" not in out
        assert "\n" not in out
        assert "#" not in out

    def test_format_retro_strips_escapes_from_top_repos(self):
        # End-to-end pin: peer-controlled canonical with embedded ANSI flows
        # through repos_by_count, _shorten_repo_url, and format_retro without
        # planting escape sequences in the rendered markdown.
        evil_url = "github.com/\x1b[31mevil/\x1b[0muser-repo"
        data = aggregator.RetroData(
            window_days=7,
            since=NOW - timedelta(days=7),
            until=NOW,
        )
        data.git = aggregator.GitAggregate(
            commits=2,
            additions=5,
            deletions=1,
            repos_by_count={evil_url: 2},
        )
        out = aggregator.format_retro(data)
        # No raw escape bytes in the rendered output.
        assert "\x1b" not in out
        # Canonical URL itself (the dedup key) is preserved on the data
        # struct — defang is render-only.
        assert evil_url in data.git.repos_by_count


# ---------------------------------------------------------------------------
# v0.11.17 — Fleet-wide author email trust set via local_emails union.
# ---------------------------------------------------------------------------


def _push_event_with_emails(
    device: str,
    days_ago: float,
    local_emails: list[str] | None,
    *,
    sources: list[str] | None = None,
) -> dict:
    """Variant of ``_push_event`` that controls the ``local_emails`` field.

    ``local_emails=None`` produces a row with NO ``local_emails`` key —
    representing pre-v0.11.17 peers. Empty list / non-empty list emit
    the field explicitly."""
    ev = {
        "v": 2,
        "type": "mm-push",
        "ts": _ts(days_ago),
        "device": device,
        "mm_version": "0.11.17",
        "sources": sources or ["claude", "gstack"],
        "discovery_errors": [],
    }
    if local_emails is not None:
        ev["local_emails"] = list(local_emails)
    return ev


class TestAggregateLocalEmailsFromEvents:
    """``aggregate_local_emails_from_events`` is the union primitive.
    Walks every mm-push row, accumulates ``local_emails`` into a single
    set, lowercased + deduped."""

    def test_unions_across_peers(self):
        events = [
            _push_event_with_emails("a", 1.0, ["a@example.com"]),
            _push_event_with_emails("b", 1.0, ["b@example.com"]),
        ]
        union = aggregator.aggregate_local_emails_from_events(events)
        assert union == {"a@example.com", "b@example.com"}

    def test_dedups_same_email_across_peers(self):
        events = [
            _push_event_with_emails("a", 1.0, ["shared@example.com"]),
            _push_event_with_emails("b", 1.0, ["shared@example.com"]),
        ]
        union = aggregator.aggregate_local_emails_from_events(events)
        assert union == {"shared@example.com"}

    def test_lowercases_case_variant_input(self):
        """Defense in depth: a peer that emitted mixed-case (shouldn't,
        but might from a buggy / pre-fix version) gets normalized."""
        events = [_push_event_with_emails("a", 1.0, ["KB@Example.COM"])]
        union = aggregator.aggregate_local_emails_from_events(events)
        assert union == {"kb@example.com"}

    def test_skips_pre_v0_11_17_rows(self):
        """A row WITHOUT ``local_emails`` key (pre-v0.11.17 peer) is
        silently skipped — contributes nothing to the union."""
        events = [
            _push_event_with_emails("a", 1.0, None),  # legacy
            _push_event_with_emails("b", 1.0, ["b@example.com"]),
        ]
        union = aggregator.aggregate_local_emails_from_events(events)
        assert union == {"b@example.com"}

    def test_ignores_non_mm_push_events(self):
        """git-snapshot / sessions-snapshot rows have no local_emails;
        the function must filter by event type."""
        events = [
            _git_event("a", 1.0, [_commit("aaa", 1.0)]),
            _sessions_event("a", 1.0, []),
            _push_event_with_emails("a", 1.0, ["mine@example.com"]),
        ]
        union = aggregator.aggregate_local_emails_from_events(events)
        assert union == {"mine@example.com"}

    def test_tolerates_malformed_local_emails_field(self):
        """Non-list / non-string entries don't crash the union."""
        events = [
            {
                "v": 2,
                "type": "mm-push",
                "ts": _ts(1),
                "device": "a",
                "local_emails": "not-a-list",  # malformed
            },
            {
                "v": 2,
                "type": "mm-push",
                "ts": _ts(1),
                "device": "b",
                "local_emails": [None, 42, "valid@example.com", ""],  # mixed
            },
        ]
        union = aggregator.aggregate_local_emails_from_events(events)
        assert union == {"valid@example.com"}


class TestAggregateUnionWiring:
    """End-to-end: ``aggregate()`` builds the fleet-wide trust set by
    unioning every peer's ``local_emails`` and the running machine's
    locally-passed ``author_emails``, then filters commits with the
    result. This is the user-facing contract: identical retros across
    machines after sync."""

    def test_local_set_unions_with_fleet(self, tmp_path):
        """Machine A passes its own emails; the fleet has machine B's
        emails too. Effective filter = union. Commits authored by
        either email are counted."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Machine B's mm-push event (peer-emitted, contains its emails).
        _write_events(
            events_dir,
            "dev-b",
            "2026-04-28",
            [_push_event_with_emails("dev-b", 0.5, ["b@example.com"])],
        )
        # Commits in window: one by machine A's user, one by machine B's user.
        c_a = _commit("aaa", 1.0, author_email="a@example.com")
        c_b = _commit("bbb", 1.0, author_email="b@example.com")
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_git_event("dev-a", 1.0, [c_a, c_b])],
        )
        data = _aggregate(events_dir, author_emails=frozenset({"a@example.com"}))
        # Both commits pass the union filter.
        assert data.git.commits == 2

    def test_none_author_emails_disables_filter_entirely(self, tmp_path):
        """``--no-author-filter`` (passed as ``None``) renders ALL
        commits regardless of fleet ``local_emails``. Explicit user
        intent survives the union step."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-b",
            "2026-04-28",
            [_push_event_with_emails("dev-b", 0.5, ["b@example.com"])],
        )
        c_a = _commit("aaa", 1.0, author_email="a@example.com")
        c_strange = _commit("ccc", 1.0, author_email="random@third-party.com")
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_git_event("dev-a", 1.0, [c_a, c_strange])],
        )
        data = _aggregate(events_dir, author_emails=None)
        # Filter disabled; both commits including the third-party one
        # are rendered.
        assert data.git.commits == 2

    def test_fleet_emails_alone_filter_when_local_empty(self, tmp_path):
        """Running machine has no local identities (gather returned
        empty frozenset) but fleet has peers with ``local_emails``.
        The union still narrows the filter — fleet emails apply."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-b",
            "2026-04-28",
            [_push_event_with_emails("dev-b", 0.5, ["b@example.com"])],
        )
        c_b = _commit("bbb", 1.0, author_email="b@example.com")
        c_other = _commit("ccc", 1.0, author_email="someone-else@example.com")
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_git_event("dev-a", 1.0, [c_b, c_other])],
        )
        # Empty author_emails (running machine had no identities) +
        # fleet has b@example.com → union is {b@example.com} →
        # filter applies; only b@example.com commits pass.
        data = _aggregate(events_dir, author_emails=frozenset())
        assert data.git.commits == 1


class TestMixedFleetRegression:
    """**REGRESSION pin (mandatory per /plan-eng-review test review).**

    During the v0.11.17 rollout window, peers will be on a mix of
    v0.11.16 (no ``local_emails`` key on mm-push rows) and v0.11.17
    (emits the field). The aggregator MUST handle both shapes
    cleanly and union what's available without crashing."""

    def test_pre_v0_11_17_rows_aggregate_cleanly(self, tmp_path):
        """Mixed shapes in the same events dir → union picks up only
        the v0.11.17 rows' emails; pre-v0.11.17 rows contribute zero
        without raising. Aggregator output is honest about what it
        could see."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Pre-v0.11.17 peer: NO local_emails key.
        _write_events(
            events_dir,
            "dev-old",
            "2026-04-28",
            [_push_event_with_emails("dev-old", 0.5, None)],
        )
        # Post-v0.11.17 peer: emits the field.
        _write_events(
            events_dir,
            "dev-new",
            "2026-04-28",
            [_push_event_with_emails("dev-new", 0.5, ["new@example.com"])],
        )
        # Both peers committed in-window; only one's email is in the union.
        _write_events(
            events_dir,
            "dev-old",
            "2026-04-28",
            [_git_event("dev-old", 1.0, [_commit("aaa", 1.0, author_email="old@example.com")])],
        )
        _write_events(
            events_dir,
            "dev-new",
            "2026-04-28",
            [_git_event("dev-new", 1.0, [_commit("bbb", 1.0, author_email="new@example.com")])],
        )
        # Filter on running machine's local set (empty) ∪ fleet emails =
        # {new@example.com}. old@example.com falls out of the filter
        # because the old peer didn't publish its identities.
        data = _aggregate(events_dir, author_emails=frozenset())
        assert data.git.commits == 1
        # No crash, no traceback. Aggregator surfaces honest output.

    def test_running_machine_local_covers_pre_v0_11_17_self(self, tmp_path):
        """The running machine's own identity (passed as
        ``author_emails``) ALWAYS covers itself, regardless of what
        peers emit. So a pre-v0.11.17 peer's commits still count IF
        the running machine has that identity locally configured."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Pre-v0.11.17 peer with NO local_emails.
        _write_events(
            events_dir,
            "dev-old",
            "2026-04-28",
            [_push_event_with_emails("dev-old", 0.5, None)],
        )
        # Pre-v0.11.17 peer's git commit under "shared@example.com".
        _write_events(
            events_dir,
            "dev-old",
            "2026-04-28",
            [
                _git_event(
                    "dev-old",
                    1.0,
                    [_commit("aaa", 1.0, author_email="shared@example.com")],
                )
            ],
        )
        # Running machine knows about shared@example.com via its own
        # identity gather.
        data = _aggregate(events_dir, author_emails=frozenset({"shared@example.com"}))
        assert data.git.commits == 1

    def test_empty_fleet_falls_back_to_local(self, tmp_path):
        """No mm-push events on disk at all → fleet union is empty;
        running machine's local set is the only filter. Preserves
        single-machine retro behavior on a fresh fleet."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [_git_event("dev-a", 1.0, [_commit("aaa", 1.0, author_email="kb@example.com")])],
        )
        # No mm-push events emitted yet; only git-snapshot.
        data = _aggregate(events_dir, author_emails=frozenset({"kb@example.com"}))
        assert data.git.commits == 1


class TestFleetDeterminism:
    """**The user-facing invariant.** Two machines that have pushed-and-
    pulled produce identical retros. Pin it: same events on disk on both
    machines → same aggregator output bytes."""

    def test_identical_events_produce_identical_retro(self, tmp_path):
        """Build a synthetic fleet of events. Run aggregate twice with
        different ``author_emails`` (simulating two machines, each
        passing its own gather). Output should be byte-identical for
        the git/sessions/pushes sections — only thing that varies is
        the input identity, which the union absorbs."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Two peers both publish their identities + a commit.
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-28",
            [
                _push_event_with_emails("dev-a", 0.5, ["kb-machine-a@example.com"]),
                _git_event(
                    "dev-a",
                    1.0,
                    [_commit("aaa", 1.0, author_email="kb-machine-a@example.com")],
                ),
            ],
        )
        _write_events(
            events_dir,
            "dev-b",
            "2026-04-28",
            [
                _push_event_with_emails("dev-b", 0.5, ["kb-machine-b@example.com"]),
                _git_event(
                    "dev-b",
                    1.0,
                    [_commit("bbb", 1.0, author_email="kb-machine-b@example.com")],
                ),
            ],
        )

        # Machine A's view: passes its own gather.
        data_a = _aggregate(events_dir, author_emails=frozenset({"kb-machine-a@example.com"}))
        # Machine B's view: passes its own gather.
        data_b = _aggregate(events_dir, author_emails=frozenset({"kb-machine-b@example.com"}))
        # Both retros count both commits (union filter applies on both).
        assert data_a.git.commits == 2
        assert data_b.git.commits == 2
        # Render them and assert byte-identical output.
        out_a = aggregator.format_retro(data_a)
        out_b = aggregator.format_retro(data_b)
        assert out_a == out_b


# ---------------------------------------------------------------------------
# Fleet skills aggregation (v0.11.27 plan tests #11-#17 + D4 + D5#5).
# Drawn from the test diagram in /plan-eng-review §3 / 2026-05-06.
# ---------------------------------------------------------------------------


def _proj_with_skills(
    *,
    claude_dir: str = "-tmp-x",
    sessions: int = 1,
    skills_by_day: dict | None = None,
    last_session_at: str | None = None,
) -> dict:
    """Build a v=2 sessions-snapshot project with the new skills field
    explicitly present (KEY-PRESENT semantics)."""
    proj = {
        "claude_dir": claude_dir,
        "source_root": "/tmp/claude",
        "sessions": sessions,
        "total_kb": 100,
        "ephemeral": False,
        "last_session_at": last_session_at or _ts(0.5),
    }
    if skills_by_day is not None:
        proj["skills_by_day"] = skills_by_day
    return proj


def _proj_without_skills_field(
    *,
    claude_dir: str = "-tmp-x",
    sessions: int = 1,
    last_session_at: str | None = None,
) -> dict:
    """Build a v=2 project from a pre-v0.11.27 peer — KEY ABSENT."""
    return {
        "claude_dir": claude_dir,
        "source_root": "/tmp/claude",
        "sessions": sessions,
        "total_kb": 100,
        "ephemeral": False,
        "last_session_at": last_session_at or _ts(0.5),
    }


class TestFleetSkillsAggregation:
    def test_mixed_fleet_pre_skills_peers_flagged_correctly(self, tmp_path):
        """Plan test #11: three peers — two emit skills_by_day, one doesn't.
        ``pre_skills_peers`` contains the missing-field peer only. Top
        skills + invocation count match union of the two contributors."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        today = _ts(0).split("T")[0]
        for dev, has_skills, payload in [
            ("dev-a", True, {today: {"ship": 5, "review": 2}}),
            ("dev-b", True, {today: {"ship": 3}}),
            ("dev-c", False, None),  # no skills_by_day key on its proj rows
        ]:
            proj = (
                _proj_with_skills(claude_dir=f"-tmp-{dev}", skills_by_day=payload)
                if has_skills
                else _proj_without_skills_field(claude_dir=f"-tmp-{dev}")
            )
            _write_events(
                events_dir, dev, "2026-04-28", [_sessions_event(dev, days_ago=0.5, projects=[proj])]
            )
        data = _aggregate(events_dir)
        assert data.skills.pre_skills_peers == {"dev-c"}
        assert data.skills.invocations == 10  # 5+2 from a, 3 from b
        assert data.skills.by_skill == {"ship": 8, "review": 2}

    def test_d4_empty_skills_dict_does_not_flag_pre_skills_peer(self, tmp_path):
        """D4 correctness gate (locks in the false-positive fix). Project
        has sessions > 0 but ``skills_by_day == {}`` (KEY PRESENT, value
        empty — meaning "no Skill usage in window"). Must NOT appear in
        ``pre_skills_peers``."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj = _proj_with_skills(skills_by_day={})  # KEY PRESENT, value EMPTY
        _write_events(events_dir, "dev-a", "2026-04-28", [_sessions_event("dev-a", 0.5, [proj])])
        data = _aggregate(events_dir)
        assert "dev-a" not in data.skills.pre_skills_peers
        assert data.skills.invocations == 0
        assert data.skills.available is True  # field is present → fleet has rolled out

    def test_d5_no_skills_this_window_zero_invocations_no_flag(self, tmp_path):
        """Plan test #13 (D5#4): peer has skills_by_day populated but
        every day-key falls outside [since, until] → 0 invocations,
        pre_skills_peers EMPTY (peer is on new mm; just no activity)."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Skill activity 60 days ago — outside default 7d window.
        old_day = (NOW - timedelta(days=60)).date().isoformat()
        proj = _proj_with_skills(
            skills_by_day={old_day: {"ship": 100}},
            last_session_at=_ts(0.5),  # last session is recent so project survives stage-2 filter
        )
        _write_events(events_dir, "dev-a", "2026-04-28", [_sessions_event("dev-a", 0.5, [proj])])
        data = _aggregate(events_dir, window_days=7)
        assert data.skills.invocations == 0
        assert "dev-a" not in data.skills.pre_skills_peers
        assert data.skills.available is True

    def test_trust_boundary_skill_name_sanitized_at_render(self, tmp_path):
        """Plan test #14: peer plants ``skill = "evil\\x1b[2J\\nfake header"``.
        ``format_retro`` output strips terminal escapes + buckets non-
        whitelisted chars (including the embedded newline) to ``_``. No
        newline bleeds into a section header."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        evil = "evil\x1b[2J\nfake header"
        today = _ts(0).split("T")[0]
        proj = _proj_with_skills(skills_by_day={today: {evil: 1}})
        _write_events(events_dir, "dev-a", "2026-04-28", [_sessions_event("dev-a", 0.5, [proj])])
        data = _aggregate(events_dir)
        out = aggregator.format_retro(data)
        # No raw escape sequence survives.
        assert "\x1b" not in out
        # Embedded newline must NOT bleed into a section header — every
        # line that starts with the rendered skill chunk should appear in
        # the Skills section bullet, not as a fake header.
        assert "\n## fake header" not in out

    def test_phantom_event_filter_header_semantics_not_data_filter(self, tmp_path, monkeypatch):
        """Plan test #15 corrected: the phantom-event filter affects
        ``devices_in_events`` (header count) and ``unregistered_event_devices``
        (Notes breadcrumb), but does NOT filter session/token/skill DATA
        from the totals — same semantics as the existing token aggregation.
        Future work could tighten this if it becomes a problem; for now
        the test pins the actual existing behavior so a refactor that
        accidentally diverges (e.g. starts filtering skills data while
        leaving tokens unfiltered) trips this test."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        today = _ts(0).split("T")[0]
        proj = _proj_with_skills(skills_by_day={today: {"ship": 99}})
        _write_events(
            events_dir,
            "phantom-x",
            "2026-04-28",
            [
                _sessions_event("phantom-x", 0.5, [proj]),
                _push_event("phantom-x", 0.5),
            ],
        )
        # Mock `mm devices --format=json` to register zero devices —
        # phantom-x is unregistered.
        monkeypatch.setattr(
            aggregator,
            "get_known_devices",
            lambda: (0, []),
        )
        data = _aggregate(events_dir)
        # Header-level filter: device captured as unregistered.
        assert data.fleet.unregistered_event_devices == 1
        assert "phantom-x" not in data.fleet.devices_in_events
        # Data-level: phantom's skill data flows through (parity with token
        # aggregation behavior); ``unregistered_event_devices`` breadcrumb
        # is the user-facing surface that warns "stale data may be present".
        assert data.skills.invocations == 99

    def test_empty_fleet_no_peer_ships_skills_renders_omitted(self, tmp_path):
        """Plan test #16: every peer is on pre-v0.11.27 (no skills_by_day
        on any project) → ``available = False`` → renderer emits the
        "section omitted" caveat instead of "0 invocations"."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj = _proj_without_skills_field()
        _write_events(events_dir, "dev-a", "2026-04-28", [_sessions_event("dev-a", 0.5, [proj])])
        data = _aggregate(events_dir)
        assert data.skills.available is False
        out = aggregator.format_retro(data)
        assert "section omitted" in out

    def test_d5_5_format_retro_never_contains_this_machine_only(self, tmp_path):
        """D5#5 regression gate: assert the legacy "this machine only"
        caveat is absent across multiple fleet shapes (with skills,
        without, mixed, empty fleet)."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        today = _ts(0).split("T")[0]
        for dev, proj in [
            ("dev-a", _proj_with_skills(skills_by_day={today: {"ship": 1}})),
            ("dev-b", _proj_with_skills(skills_by_day={})),
            ("dev-c", _proj_without_skills_field()),
        ]:
            _write_events(events_dir, dev, "2026-04-28", [_sessions_event(dev, 0.5, [proj])])
        data = _aggregate(events_dir)
        out = aggregator.format_retro(data)
        assert "this machine only" not in out


# ---------------------------------------------------------------------------
# v0.12.0 — commit-type mix, hourly distribution, bursts, ship-of-the-week,
# weekly buckets, snapshot persistence, two-pass card.
# ---------------------------------------------------------------------------


def _agg_git_only(commits: list[dict], window_days: int = 7) -> aggregator.GitAggregate:
    """Run ``aggregate_git`` against a single synthetic event (no fleet
    machinery) to keep tests focused on the per-commit derivations."""
    return aggregator.aggregate_git(
        [_git_event("dev-a", 0, commits)],
        since=NOW - timedelta(days=window_days),
        until=NOW,
        author_emails=frozenset({"kb@example.com"}),
        window_days=window_days,
    )


class TestCommitTypeMix:
    def test_conventional_prefixes_bucket(self):
        commits = [
            _commit("a" * 7, 1, subject="feat: add thing"),
            _commit("b" * 7, 1, subject="fix: bug"),
            _commit("c" * 7, 1, subject="fix(scope): another bug"),
            _commit("d" * 7, 1, subject="docs: readme"),
            _commit("e" * 7, 1, subject="chore: bump"),
            _commit("f" * 7, 1, subject="just a sentence"),
        ]
        out = _agg_git_only(commits)
        assert out.commit_types.total == 6
        assert out.commit_types.counts["feat"] == 1
        assert out.commit_types.counts["fix"] == 2  # bare + scoped both classify as fix
        assert out.commit_types.counts["docs"] == 1
        assert out.commit_types.counts["chore"] == 1
        assert out.commit_types.counts["other"] == 1

    def test_breaking_change_marker_normalizes(self):
        out = _agg_git_only([_commit("a" * 7, 1, subject="feat!: breaking")])
        assert out.commit_types.counts.get("feat") == 1

    def test_render_emits_mix_line(self):
        out = _agg_git_only(
            [
                _commit("a" * 7, 1, subject="feat: x"),
                _commit("b" * 7, 1, subject="fix: y"),
            ]
        )
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.git = out
        markdown = aggregator.format_retro(data)
        assert "Mix:" in markdown
        assert "feat 1" in markdown


class TestHourlyDistribution:
    def test_local_hour_bucketing(self):
        # Use a fixed UTC timestamp; the aggregator converts to local hour.
        commits = [
            _commit("a" * 7, 0.5),
            _commit("b" * 7, 0.5),
            _commit("c" * 7, 1.5),
        ]
        out = _agg_git_only(commits)
        # Total of all hourly buckets equals the deduped commit count.
        assert sum(out.hourly.values()) == out.commits
        assert out.commits == 3

    def test_render_omits_section_when_empty(self):
        out = aggregator.GitAggregate(commits=0)
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.git = out
        markdown = aggregator.format_retro(data)
        assert "Peak hours" not in markdown


class TestCommitBursts:
    def test_single_commit_micro_burst(self):
        out = _agg_git_only([_commit("a" * 7, 1)])
        assert out.bursts.burst_count == 1
        assert out.bursts.micro == 1
        assert out.bursts.deep == 0

    def test_45min_gap_splits_burst(self):
        # Two commits 60 min apart → 2 bursts.
        from datetime import timedelta as _td

        cs = [
            _commit("a" * 7, 1.0),
            _commit("b" * 7, (NOW - (NOW - _td(days=1) - _td(minutes=60))).total_seconds() / 86400),
        ]
        out = _agg_git_only(cs)
        assert out.bursts.burst_count == 2

    def test_close_commits_form_one_burst(self):
        # Two commits 10 min apart → 1 burst.
        from datetime import timedelta as _td

        cs = [
            _commit("a" * 7, 1.0),
            _commit("b" * 7, (NOW - (NOW - _td(days=1) - _td(minutes=10))).total_seconds() / 86400),
        ]
        out = _agg_git_only(cs)
        assert out.bursts.burst_count == 1


class TestShipOfWeek:
    def test_picks_largest_by_loc(self):
        cs = [
            _commit("a" * 7, 1, subject="small", add=10, dlt=2),
            _commit("b" * 7, 1, subject="huge: refactor world", add=5000, dlt=300),
            _commit("c" * 7, 1, subject="medium", add=200, dlt=20),
        ]
        out = _agg_git_only(cs)
        assert out.ship.has_data is True
        assert out.ship.sha == "bbbbbbb"
        assert out.ship.additions == 5000
        assert out.ship.deletions == 300
        assert "huge" in out.ship.subject

    def test_ship_subject_punctuation_preserved_in_render(self):
        cs = [
            _commit(
                "a" * 7, 1, subject="feat(cli): add /retro-fleet --theme flag", add=999, dlt=10
            ),
        ]
        out = _agg_git_only(cs)
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.git = out
        markdown = aggregator.format_retro(data)
        # _safe_prose preserves these where _safe_short would mangle them.
        assert "feat(cli): add /retro-fleet --theme flag" in markdown

    def test_no_ship_when_zero_commits(self):
        out = _agg_git_only([])
        assert out.ship.has_data is False
        assert out.ship.subject == ""


class TestWeeklyBuckets:
    def test_buckets_emitted_for_14d_window(self):
        cs = [
            _commit("a" * 7, 1, add=100, dlt=10),  # this week
            _commit("b" * 7, 8, add=50, dlt=5),  # last week
        ]
        out = aggregator.aggregate_git(
            [_git_event("dev-a", 0, cs)],
            since=NOW - timedelta(days=14),
            until=NOW,
            author_emails=frozenset({"kb@example.com"}),
            window_days=14,
        )
        assert len(out.weekly) == 2
        # Sorted oldest -> newest.
        assert out.weekly[0].week_start <= out.weekly[1].week_start

    def test_buckets_skipped_for_7d_window(self):
        cs = [_commit("a" * 7, 1, add=100, dlt=10)]
        out = _agg_git_only(cs, window_days=7)
        assert out.weekly == []

    def test_active_days_counted_per_bucket(self):
        # Three commits same week, two distinct days.
        cs = [
            _commit("a" * 7, 1.0, add=10, dlt=1),
            _commit("b" * 7, 1.5, add=10, dlt=1),
            _commit("c" * 7, 2.0, add=10, dlt=1),
        ]
        out = aggregator.aggregate_git(
            [_git_event("dev-a", 0, cs)],
            since=NOW - timedelta(days=14),
            until=NOW,
            author_emails=frozenset({"kb@example.com"}),
            window_days=14,
        )
        assert len(out.weekly) >= 1
        total_active = sum(b.active_days for b in out.weekly)
        assert total_active >= 2


class TestSnapshotPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.git = aggregator.GitAggregate(commits=42, additions=1000, deletions=200)
        path = aggregator._save_snapshot(data, tmp_path)
        assert path is not None
        assert path.exists()
        prior = aggregator._load_prior_snapshot(tmp_path, window_days=7)
        assert prior is not None
        assert prior["window_days"] == 7
        assert prior["metrics"]["commits"] == 42

    def test_load_skips_window_mismatch(self, tmp_path):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        aggregator._save_snapshot(data, tmp_path)
        prior = aggregator._load_prior_snapshot(tmp_path, window_days=30)
        assert prior is None

    def test_load_picks_most_recent_matching(self, tmp_path):
        # Save two snapshots; loader returns the most recent.
        d1 = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        d1.git = aggregator.GitAggregate(commits=10)
        path1 = aggregator._save_snapshot(d1, tmp_path)
        # Force ascending filename order to simulate sequence.
        path1.rename(tmp_path / "2026-05-01-1.json")

        d2 = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        d2.git = aggregator.GitAggregate(commits=99)
        path2 = aggregator._save_snapshot(d2, tmp_path)
        path2.rename(tmp_path / "2026-05-07-1.json")

        prior = aggregator._load_prior_snapshot(tmp_path, window_days=7)
        assert prior is not None
        assert prior["metrics"]["commits"] == 99

    def test_corrupt_snapshot_skipped(self, tmp_path):
        (tmp_path / "2026-05-07-1.json").write_text("{ not json")
        d = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        d.git = aggregator.GitAggregate(commits=10)
        path = aggregator._save_snapshot(d, tmp_path)
        # Save still succeeds; load skips the corrupt file and finds ours.
        prior = aggregator._load_prior_snapshot(tmp_path, window_days=7)
        assert prior is not None
        assert path is not None

    def test_compute_prior_delta_from_dict(self):
        prior = {
            "window_days": 7,
            "until": "2026-05-01T00:00:00+00:00",
            "metrics": {
                "commits": 10,
                "additions": 100,
                "deletions": 20,
                "streak_days": 5,
                "sessions": 50,
                "tokens_total": 1000,
                "push_events": 3,
            },
        }
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.git = aggregator.GitAggregate(commits=15, additions=200, deletions=30, streak_days=12)
        delta = aggregator._compute_prior_delta(data, prior)
        assert delta.has_prior is True
        assert delta.commits == 5
        assert delta.additions == 100
        assert delta.streak_days == 7

    def test_compute_prior_delta_handles_none(self):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        delta = aggregator._compute_prior_delta(data, None)
        assert delta.has_prior is False

    def test_render_skips_section_when_no_changes(self):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.prior = aggregator.PriorRetroDelta(has_prior=True, prior_date="2026-05-01")
        markdown = aggregator.format_retro(data)
        assert "Trends vs last retro" not in markdown
        assert "No metric changed" not in markdown


class TestAsciiCard:
    def _baseline(self) -> aggregator.RetroData:
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.git = aggregator.GitAggregate(
            commits=42,
            additions=1000,
            deletions=200,
            repos_by_count={"github.com/kb/mm": 30, "github.com/kb/bolt": 12},
            streak_days=37,
        )
        # Simulate two-machine fleet for the card.
        data.fleet.devices_in_events = {"dev-a", "dev-b"}
        return data

    def test_card_rendered_when_themes_supplied(self):
        data = self._baseline()
        out = aggregator.format_retro(
            data,
            name="kb",
            themes=["theme one", "theme two"],
            noteworthy="something noteworthy",
        )
        assert "╔" in out
        assert "╝" in out
        assert "kb · " in out
        assert "NOTEWORTHY" in out
        assert "TOP WORK" in out
        assert "theme one" in out

    def test_card_lines_pad_to_fixed_width(self):
        data = self._baseline()
        out = aggregator.format_retro(
            data,
            name="kb",
            themes=["short", "longer theme line", "x"],
            noteworthy="medium length",
        )
        card_lines = [line for line in out.splitlines() if line.startswith("║")]
        assert card_lines, "card not present"
        # Every interior card line is exactly CARD_WIDTH chars wide.
        widths = {len(line) for line in card_lines}
        assert widths == {aggregator.CARD_WIDTH}, f"variable widths: {widths}"

    def test_card_truncates_overlong_theme(self):
        data = self._baseline()
        long_theme = "x" * 200
        out = aggregator.format_retro(data, themes=[long_theme], noteworthy="ok")
        # The truncated line still has the right border.
        for line in out.splitlines():
            if line.startswith("║"):
                assert line.endswith("║")
        assert "…" in out  # truncation marker present

    def test_no_card_without_inputs(self):
        data = self._baseline()
        out = aggregator.format_retro(data)
        assert "╔" not in out
        # First-pass output includes the themes-prompt sidecar instead.
        assert "MM_THEMES_PROMPT" in out

    def test_themes_prompt_omitted_on_second_pass(self):
        data = self._baseline()
        out = aggregator.format_retro(data, themes=["a"], noteworthy="b", name="kb")
        assert "MM_THEMES_PROMPT" not in out

    def test_card_strips_terminal_escapes_from_llm_inputs(self):
        data = self._baseline()
        # Hostile theme tries to inject ANSI red.
        out = aggregator.format_retro(
            data,
            themes=["\x1b[31mevil\x1b[0m"],
            noteworthy="\x1b[1mbold\x1b[0m",
        )
        assert "\x1b" not in out


class TestThemesPrompt:
    def test_first_pass_includes_json_payload(self, tmp_path):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.git = aggregator.GitAggregate(
            commits=5,
            additions=100,
            deletions=10,
            repos_by_count={"github.com/kb/foo": 3},
        )
        data.git.ship = aggregator.ShipOfWeek(
            repo="github.com/kb/foo",
            sha="abc1234",
            subject="feat: ship it",
            additions=99,
            deletions=1,
            has_data=True,
        )
        out = aggregator.format_retro(data)
        assert "MM_THEMES_PROMPT" in out
        # JSON block parses cleanly.
        block = out.split("```json", 1)[1].split("```", 1)[0]
        payload = json.loads(block)
        assert payload["commits"] == 5
        assert payload["ship"]["subject"] == "feat: ship it"
        assert payload["top_repos"] == ["github.com/kb/foo"]

    def test_long_repo_url_shortened_in_prompt(self):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        long_url = "git.example.com/org/team/" + "x" * 80 + "/repo"
        data.git = aggregator.GitAggregate(
            commits=1,
            additions=1,
            deletions=0,
            repos_by_count={long_url: 1},
        )
        out = aggregator.format_retro(data)
        # The original UUID-shaped middle segment must NOT survive into
        # the JSON sidecar — same defang-then-shorten as the body.
        assert "x" * 80 not in out


class TestMainCliFlags:
    def test_no_save_flag_skips_snapshot(self, tmp_path, monkeypatch, capsys):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        retros_dir = tmp_path / "retros"
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))
        monkeypatch.setenv("MM_RETROS_DIR", str(retros_dir))
        monkeypatch.setattr(aggregator, "gather_author_emails", lambda: frozenset(), raising=True)
        monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
        rc = aggregator.main(["7d", "--no-save"])
        assert rc == 0
        # No snapshot dir should have been created (no save attempted).
        assert not retros_dir.exists() or list(retros_dir.glob("*.json")) == []

    def test_theme_args_render_card(self, tmp_path, monkeypatch, capsys):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))
        monkeypatch.setenv("MM_RETROS_DIR", str(tmp_path / "retros"))
        monkeypatch.setattr(aggregator, "gather_author_emails", lambda: frozenset(), raising=True)
        monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
        rc = aggregator.main(
            [
                "7d",
                "--name",
                "kb",
                "--noteworthy",
                "did things",
                "--theme",
                "alpha",
                "--theme",
                "beta",
                "--no-save",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "╔" in out
        assert "kb · " in out
        assert "alpha" in out
        assert "MM_THEMES_PROMPT" not in out

    def test_first_pass_writes_snapshot(self, tmp_path, monkeypatch):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        retros_dir = tmp_path / "retros"
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))
        monkeypatch.setenv("MM_RETROS_DIR", str(retros_dir))
        monkeypatch.setattr(aggregator, "gather_author_emails", lambda: frozenset(), raising=True)
        monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
        rc = aggregator.main(["7d"])
        assert rc == 0
        snapshots = list(retros_dir.glob("*.json"))
        assert len(snapshots) == 1


class TestSafeProseHardening:
    """v0.12.0 review-gate fixes — peer-controlled prose strings get
    BiDi + line-separator stripping AND a 4 KiB length cap before regex
    sanitization. Pre-fix, ``_PROSE_CTRL_RE`` only stripped ASCII
    C0/DEL, leaving U+202E (RTL override) and U+2028 (line separator)
    free to flip downstream rendered text or smuggle line breaks past
    the single-line bullet contract."""

    def test_strips_rtl_override(self):
        out = aggregator._safe_prose("feat: ‮inject")
        assert "‮" not in out

    def test_strips_line_separators(self):
        for ch in ("", " ", " "):
            out = aggregator._safe_prose(f"a{ch}b")
            assert ch not in out

    def test_strips_bidi_isolates(self):
        for ch in ("‪", "‫", "‬", "‭", "⁦", "⁧", "⁨", "⁩"):
            out = aggregator._safe_prose(f"x{ch}y")
            assert ch not in out

    def test_caps_long_input_at_4kib(self):
        # Pathological 50KiB peer subject doesn't burn CPU on regex.
        s = "a" * 50_000
        out = aggregator._safe_prose(s)
        assert len(out) <= aggregator._PROSE_LEN_CAP

    def test_normal_punctuation_preserved(self):
        out = aggregator._safe_prose("feat(cli): /retro --theme — fix #194")
        assert out == "feat(cli): /retro --theme — fix #194"

    def test_classify_subject_caps_input(self):
        # 1MB subject doesn't burn CPU on .lower()/.strip()/regex —
        # classifier only inspects the prefix anyway.
        long = "feat: " + "x" * 1_000_000
        assert aggregator._classify_commit_subject(long) == "feat"


class TestSnapshotRaceSafety:
    """v0.12.0 review-gate fixes — snapshot save uses O_EXCL so two
    concurrent retros can't silently overwrite each other on the same
    sequence number; load + prune sort by parsed (date, seq) tuple so
    seq=10+ doesn't lex-shadow seq=9."""

    def test_filename_format_zero_padded(self, tmp_path):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        path = aggregator._save_snapshot(data, tmp_path)
        assert path is not None
        assert aggregator._SNAPSHOT_FILENAME_RE.match(path.name) is not None
        assert path.name.endswith("-001.json")

    def test_seq_advances_on_collision(self, tmp_path):
        # Pre-create a file with seq=001 then save; new save must pick
        # a fresh seq instead of overwriting.
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        today = NOW.astimezone().date().isoformat()
        squat = tmp_path / f"{today}-001.json"
        squat.write_text('{"window_days": 7, "metrics": {"commits": 999}}')
        path = aggregator._save_snapshot(data, tmp_path)
        assert path is not None
        # Squatter file is untouched; new save took seq 002.
        assert "999" in squat.read_text()
        assert path.name.endswith("-002.json")

    def test_load_returns_seq_ten_not_seq_nine(self, tmp_path):
        # Pre-fix bug: lex sort with reverse=True puts -9.json BEFORE
        # -10.json so loader returned seq=9 as "most recent."
        # With zero-pad + tuple sort, -010 sorts after -009 correctly.
        today = NOW.astimezone().date().isoformat()
        for seq, commits in [(9, 9), (10, 10)]:
            (tmp_path / f"{today}-{seq:03d}.json").write_text(
                json.dumps(
                    {
                        "window_days": 7,
                        "until": NOW.isoformat(),
                        "metrics": {"commits": commits},
                    }
                )
            )
        prior = aggregator._load_prior_snapshot(tmp_path, window_days=7)
        assert prior is not None
        assert prior["metrics"]["commits"] == 10

    def test_load_caps_oversized_files(self, tmp_path):
        today = NOW.astimezone().date().isoformat()
        big = tmp_path / f"{today}-001.json"
        big.write_text("[" + "0," * 1_000_000 + "0]")
        good = tmp_path / f"{today}-002.json"
        good.write_text('{"window_days": 7, "metrics": {"commits": 7}}')
        prior = aggregator._load_prior_snapshot(tmp_path, window_days=7)
        assert prior is not None
        assert prior["metrics"]["commits"] == 7


class TestRenderHardening:
    """v0.12.0 review-gate fixes — card caps theme count at MAX_THEMES,
    aggregator window arg refuses pathological values, header date uses
    local timezone consistently with the card."""

    def test_card_caps_themes_at_max(self):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        out = aggregator.format_retro(
            data, themes=[f"theme {i}" for i in range(50)], noteworthy="ok"
        )
        bullet_lines = [line for line in out.splitlines() if line.startswith("║") and "•" in line]
        assert len(bullet_lines) == aggregator.MAX_THEMES

    def test_window_rejects_overflow(self):
        with pytest.raises(Exception):
            aggregator._parse_window("1000000000d")

    def test_window_accepts_max(self):
        assert (
            aggregator._parse_window(f"{aggregator._MAX_WINDOW_DAYS}d")
            == aggregator._MAX_WINDOW_DAYS
        )

    def test_header_date_matches_card_local_tz(self):
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.fleet.devices_in_events = {"dev-a"}
        local_until = NOW.astimezone().date().isoformat()
        local_since = (NOW - timedelta(days=7)).astimezone().date().isoformat()
        out = aggregator.format_retro(data, themes=["x"], noteworthy="y", name="kb")
        assert f"# Retro: {local_since} → {local_until}" in out


class TestSnapshotPruning:
    def test_old_snapshots_reaped(self, tmp_path):
        # Year-old snapshot file (filename date) — should be pruned.
        old_date = (datetime.now(timezone.utc) - timedelta(days=400)).date().isoformat()
        old = tmp_path / f"{old_date}-001.json"
        old.write_text('{"window_days": 7, "metrics": {}}')
        recent = tmp_path / "2026-05-07-001.json"
        recent.write_text('{"window_days": 7, "metrics": {}}')
        aggregator._prune_old_snapshots(tmp_path)
        assert not old.exists()
        assert recent.exists()

    def test_unparseable_filenames_left_alone(self, tmp_path):
        weird = tmp_path / "not-a-date-file.json"
        weird.write_text("{}")
        aggregator._prune_old_snapshots(tmp_path)
        assert weird.exists()
