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

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from mind_meld import host_usage, retention
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
        assert "## Skills used" not in out

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
        # 1.1: degradations are health-payload entries, not rendered prose.
        # The body carries one summary line; the detail is machine-readable
        # so the skill can decide whether it deserves the reader's attention.
        assert "## Notes" not in out
        assert "Data health:" in out
        entry = _health_entry(out, "parse_errors")
        assert entry is not None and "1 event(s) skipped" in entry["detail"]

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
        assert "1 of 3 machines" in out
        # Notes section copy (post-v0.11.12 consolidation).
        assert "2 registered machine(s)" in _health_entry(out, "fleet_incomplete")["detail"]

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
        assert "Activity across 2 of 2 machines" in out
        # Old noisy banner must NOT fire (the user complaint).
        assert "currently registered" not in out
        assert "Fleet inconsistency" not in out
        # Phantom-event count surfaces in the health payload so the user
        # knows the disk still has stale files reaping naturally.
        entry = _health_entry(out, "unregistered_devices")
        assert entry is not None
        assert "3 unregistered device id(s)" in entry["detail"]

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

        assert aggregator.EVENTS_RETENTION_DAYS == retention.EVENTS_RETENTION_DAYS


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
        assert "## Agents" in out
        # 1.1: the Skills heading is omitted entirely when nothing was
        # invoked, rather than printing a caveat about why it is empty.
        assert "this machine only" not in out
        assert "## Fleet" in out
        # Eureka section was removed in v0.11.12 (always 0 in practice).
        assert "## Eureka moments" not in out

    def test_zero_events_renders_without_crashing(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        data = _aggregate(events_dir)
        out = aggregator.format_retro(data)
        # All sections present; substantive content gracefully degrades.
        assert "0 commits" in out
        assert "No agent usage observed in this window." in out


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
        assert result.token_missing_projects == 1
        assert result.tokens_input == 0

    def test_present_empty_tokens_by_day_is_not_missing_coverage(self):
        from mind_meld.skills.retro_fleet.aggregator import aggregate_sessions

        ev = self._make_sessions_event(
            "dev-a",
            "2026-05-01T12:00:00+00:00",
            tokens_by_day={},
            sessions=3,
        )
        result, _skills_unused = aggregate_sessions(
            [ev],
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
        assert "dev-a" not in result.pre_token_peers
        assert result.token_missing_projects == 0
        assert result.token_missing_sessions == 0

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
                # Unresolvable id: "future" is not a known family, so it
                # survives family-tier fallback and stays genuinely
                # unpriced.  (Pre-v0.12.13 this fixture used
                # claude-sonnet-3-7, which family-tier now prices.)
                "claude-future-9-9": {
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
        # + output of the unpriced claude-future-9-9 entry).
        assert "unpriced" in out
        assert "1 unpriced model(s)" in out
        assert "excluded from pricing" in _health_entry(out, "unpriced_models")["detail"]

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
        assert _health_entry(out, "unpriced_models") is None

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
        assert _health_entry(out, "unpriced_models") is None


class TestCostLineHonesty:
    """v0.12.13. The card printed ``~$3.37`` for a window the corrected
    table prices at ~$11,015 — the entire Claude 5 family resolved to no
    price at all and silently costed $0. These pin the properties that
    make that impossible to repeat quietly."""

    def _data(self, tokens_by_model):
        """Top-level totals are derived from ``by_model`` rather than
        hand-set: ``_render_token_block`` hides the whole block when they
        sum to zero, so a fixture that omits them makes every assertion
        pass vacuously."""
        from mind_meld.skills.retro_fleet.aggregator import RetroData, SessionsAggregate

        def _sum(field):
            return sum(b.get(field, 0) for b in tokens_by_model.values())

        data = RetroData(
            window_days=7,
            since=datetime(2026, 4, 24, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        data.sessions = SessionsAggregate(
            total_sessions=3,
            projects=1,
            tokens_input=_sum("input"),
            tokens_cache_create=_sum("cache_create"),
            tokens_cache_read=_sum("cache_read"),
            tokens_output=_sum("output"),
            tokens_by_model=tokens_by_model,
        )
        return data

    def test_cost_line_and_notes_line_never_contradict(self):
        """REGRESSION (IRON RULE). Before v0.12.13 the cost path and the
        unpriced-Notes path each ran their own ``model in PRICING`` test.
        Family-tier fallback landing in only one of them would make the
        card price a model on one line and call it unpriced two lines
        later. Both now share ``resolve_prices``."""
        from mind_meld.skills.retro_fleet.aggregator import (
            _unpriced_token_summary,
            format_retro,
        )
        from mind_meld.token_usage import estimate_cost

        by_model = {
            # Not in PRICING; resolves via family tier.
            "claude-opus-6": {
                "input": 1_000_000,
                "cache_create": 0,
                "cache_read": 0,
                "output": 0,
            },
        }
        _, per_model = estimate_cost(by_model)
        unpriced_tokens, unpriced_models, _ids = _unpriced_token_summary(by_model)

        # Priced by one path => must NOT be counted unpriced by the other.
        assert "claude-opus-6" in per_model
        assert unpriced_models == 0
        assert unpriced_tokens == 0
        out = format_retro(self._data(by_model))
        assert _health_entry(out, "unpriced_models") is None

    def test_unpriced_volume_downgrades_estimate_to_lower_bound(self):
        """A confident ``~`` over incomplete data is the v0.12.13 bug.
        Any unresolvable model flips the marker to ``>=``."""
        from mind_meld.skills.retro_fleet.aggregator import format_retro

        out = format_retro(
            self._data(
                {
                    "claude-opus-5": {
                        "input": 40_000_000,
                        "cache_create": 0,
                        "cache_read": 0,
                        "output": 0,
                    },
                    "claude-future-9-9": {
                        "input": 1_000_000,
                        "cache_create": 0,
                        "cache_read": 0,
                        "output": 0,
                    },
                }
            )
        )
        assert "| ≥$200 |" in out
        assert "1 unpriced model(s)" in _health_entry(out, "unpriced_models")["detail"]

    def test_fully_priced_window_keeps_tilde_and_drops_cents(self):
        from mind_meld.skills.retro_fleet.aggregator import format_retro

        out = format_retro(
            self._data(
                {
                    "claude-opus-5": {
                        "input": 40_000_000,
                        "cache_create": 0,
                        "cache_read": 0,
                        "output": 0,
                    },
                }
            )
        )
        assert "| ~$200 |" in out
        assert "~$200.00" not in out
        assert _health_entry(out, "unpriced_models") is None

    def test_all_models_unpriced_says_so_explicitly(self):
        """When nothing resolves, total_cost is 0. Dropping the cost line
        entirely reads as "no cost data" when the truth is "we could not
        price ANY of it" — so the line says that instead of vanishing."""
        from mind_meld.skills.retro_fleet.aggregator import format_retro

        out = format_retro(
            self._data(
                {
                    "claude-future-9-9": {
                        "input": 40_000_000,
                        "cache_create": 0,
                        "cache_read": 0,
                        "output": 0,
                    },
                }
            )
        )
        claude = next(line for line in out.splitlines() if line.startswith("| Claude |"))
        assert "| — |" in claude
        detail = _health_entry(out, "unpriced_models")["detail"]
        assert "1 unpriced model(s)" in detail and "40.0M tokens" in detail

    def test_bedrock_style_ids_are_unpriced_not_silent(self):
        """A fleet running Claude Code through Bedrock sends ids like
        `us.anthropic.claude-opus-4-5-v1:0`, which fail the `claude-`
        prefix check. Every model goes unpriced — the card must say so
        rather than omit the cost line."""
        from mind_meld.skills.retro_fleet.aggregator import format_retro

        out = format_retro(
            self._data(
                {
                    "us.anthropic.claude-opus-4-5-v1:0": {
                        "input": 1_000_000_000,
                        "cache_create": 0,
                        "cache_read": 0,
                        "output": 0,
                    },
                }
            )
        )
        claude = next(line for line in out.splitlines() if line.startswith("| Claude |"))
        assert "| — |" in claude
        assert "1 unpriced model(s)" in _health_entry(out, "unpriced_models")["detail"]

    def test_caveat_carries_verification_date(self):
        """mm has no network, so the table cannot self-update. The card
        shows when rates were last checked instead of asserting a
        freshness verdict it cannot earn."""
        from mind_meld.skills.retro_fleet.aggregator import format_retro
        from mind_meld.token_usage import PRICING_LAST_UPDATED

        out = format_retro(
            self._data(
                {
                    "claude-opus-5": {
                        "input": 1_000_000,
                        "cache_create": 0,
                        "cache_read": 0,
                        "output": 0,
                    },
                }
            )
        )
        block = out.split("<!-- MM_HEALTH -->", 1)[1].split("```json", 1)[1].split("```", 1)[0]
        assert json.loads(block)["pricing"]["anthropic_verified"] == PRICING_LAST_UPDATED


class TestShortModelName:
    """v0.12.13: the 4-segment requirement left every 3-segment Claude 5
    id rendering raw next to prettified 4-segment names."""

    @pytest.mark.parametrize(
        "model,expected",
        [
            ("claude-opus-5", "Opus 5"),
            ("claude-sonnet-5", "Sonnet 5"),
            ("claude-fable-5", "Fable 5"),
            ("claude-opus-4-8", "Opus 4.8"),
            ("claude-sonnet-4-6", "Sonnet 4.6"),
            ("claude-haiku-4-5", "Haiku 4.5"),
            ("<synthetic>", "synthetic"),
        ],
    )
    def test_renders_both_id_shapes(self, model, expected):
        from mind_meld.skills.retro_fleet.aggregator import _short_model_name

        assert _short_model_name(model) == expected

    @pytest.mark.parametrize(
        "model",
        [
            "claude-3-opus",  # from claude-3-opus-20240229 after normalize
            "claude-3-haiku",
            "claude-3-5-sonnet",
            "claude-2-1",
        ],
    )
    def test_legacy_id_shapes_are_not_mangled(self, model):
        """REGRESSION. Relaxing the segment count from >=4 to >=3 without a
        family gate rendered `claude-3-opus` as "3 opus" and
        `claude-3-5-sonnet` as "3 5.sonnet" — the version segment was read
        as a family. These ids are reachable: _normalize_model_id strips
        the -YYYYMMDD suffix, so claude-3-opus-20240229 arrives 3-segment.
        Unrecognized families must fall through to the raw string."""
        from mind_meld.skills.retro_fleet.aggregator import _short_model_name

        assert _short_model_name(model) == model

    def test_peer_controlled_escape_is_defanged(self):
        from mind_meld.skills.retro_fleet.aggregator import _short_model_name

        out = _short_model_name("claude-opus-5\x1b[2J")
        assert "\x1b" not in out

    def test_absurdly_long_id_is_truncated(self):
        """Peer-controlled ids land in rendered markdown and the ASCII card,
        which then goes into an LLM context. _safe_prose caps length for
        exactly this reason; _safe_short had no cap."""
        from mind_meld.skills.retro_fleet.aggregator import _SHORT_LEN_CAP, _short_model_name

        assert len(_short_model_name("claude-" + "z" * 500_000)) <= _SHORT_LEN_CAP


class TestFormatUsd:
    @pytest.mark.parametrize(
        "amount,expected",
        [
            (0.0, "$0.00"),
            (3.37, "$3.37"),
            (99.99, "$99.99"),
            # Rounds to 100.00, so it takes the whole-dollar branch —
            # otherwise the same displayed amount prints two ways.
            (99.996, "$100"),
            (100.0, "$100"),
            (1234.5, "$1,234"),
            (11015.0, "$11,015"),
        ],
    )
    def test_threshold(self, amount, expected):
        from mind_meld.skills.retro_fleet.aggregator import _format_usd

        assert _format_usd(amount) == expected


class TestPeerTokenClamping:
    """Token counts arrive from peer machines; `_safe_int` is the trust
    boundary every one of them crosses."""

    def test_absurd_int_cannot_overflow_the_cost_multiply(self):
        """A 400-digit integer survives json.loads, and estimate_cost
        multiplies token counts by a float rate — unclamped that raises
        OverflowError deep inside the sum and kills `mm retro-fleet` with a
        traceback. Family-tier fallback widened the reachable id set from 5
        hardcoded ids to any claude-<family>-*."""
        from mind_meld.skills.retro_fleet.aggregator import _MAX_SAFE_TOKENS, _safe_int
        from mind_meld.token_usage import estimate_cost

        clamped = _safe_int(10**400)
        assert clamped == _MAX_SAFE_TOKENS
        total, _ = estimate_cost(
            {
                "claude-opus-5": {
                    "input": clamped,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                }
            }
        )
        assert total > 0  # the point is that this does not raise

    def test_negative_counts_clamp_to_zero(self):
        """Left alone a negative count subtracts from the fleet total, so
        one bad peer could shrink the cost line or push it to zero and
        suppress it entirely."""
        from mind_meld.skills.retro_fleet.aggregator import _safe_int

        assert _safe_int(-999_999) == 0
        assert _safe_int("-42") == 0


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

    def test_skills_incomplete_breadcrumb_admits_cold_cache_ambiguity(self, tmp_path):
        """Track 11B revised (Option C, v0.12.4): the rendered "Skills
        incomplete" notes line names BOTH populations that land in
        ``pre_skills_peers`` — pre-v0.11.27 mm peers (code never emits
        the field) AND v0.11.27+ peers whose latest snapshot was emitted
        from a cold-cache push (skill walk skipped, field absent on the
        wire). The wire can't tell apart the two; the breadcrumb mirrors
        the existing ``pre_token_peers`` "OR with cold token cache"
        phrasing so the user sees both possibilities and the right
        recovery (`mm push` interactively, or upgrade)."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj = _proj_without_skills_field(claude_dir="-tmp-x")
        _write_events(events_dir, "dev-x", "2026-04-28", [_sessions_event("dev-x", 0.5, [proj])])
        data = _aggregate(events_dir)
        assert data.skills.pre_skills_peers == {"dev-x"}
        out = aggregator.format_retro(data)
        # Both populations named — "pre-v0.11.27" AND "cold token cache".
        assert "pre-v0.11.27" in _health_entry(out, "skills_incomplete")["detail"]
        assert "pre-v0.11.27" in out
        assert "cold token cache" in out
        # Recovery action named.
        assert "mm push" in out
        # Regression gate: pre-v0.12.4 tail must not reappear. A half-
        # revert that re-introduces the old wording while keeping the
        # new substrings would still pass the asserts above; this pins
        # the absence of the deprecated phrasing.
        assert "upgrade for accurate skill totals" not in out

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
        the section is omitted entirely instead of printing "0 invocations"."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        proj = _proj_without_skills_field()
        _write_events(events_dir, "dev-a", "2026-04-28", [_sessions_event("dev-a", 0.5, [proj])])
        data = _aggregate(events_dir)
        assert data.skills.available is False
        out = aggregator.format_retro(data)
        assert "## Skills used" not in out

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


class TestPullRequestAggregation:
    def test_detects_supported_squash_and_merge_subjects(self):
        out = _agg_git_only(
            [
                _commit("a" * 7, 1, subject="docs: regenerate roadmap (#114)"),
                _commit("b" * 7, 1, subject="Merge pull request #115 from kb/topic"),
            ]
        )
        assert out.pull_requests == 2
        assert out.pull_request_identities == {
            ("github.com/kb/mm", 114),
            ("github.com/kb/mm", 115),
        }

    def test_same_pr_in_distinct_accepted_commits_counts_once(self):
        out = _agg_git_only(
            [
                _commit("a" * 7, 1, subject="feat: first part (#114)"),
                _commit("b" * 7, 1, subject="fix: follow-up (#114)"),
            ]
        )
        assert out.commits == 2
        assert out.pull_requests == 1

    def test_same_number_in_different_repositories_counts_twice(self):
        events = [
            _git_event(
                "dev-a",
                0,
                [_commit("a" * 7, 1, subject="feat: repo one (#114)")],
                remote="github.com/kb/one",
            ),
            _git_event(
                "dev-b",
                0,
                [_commit("b" * 7, 1, subject="feat: repo two (#114)")],
                remote="github.com/kb/two",
            ),
        ]
        out = aggregator.aggregate_git(
            events,
            since=NOW - timedelta(days=7),
            until=NOW,
            author_emails=frozenset({"kb@example.com"}),
        )
        assert out.pull_requests == 2

    def test_canonical_remote_equivalence_deduplicates_pr_identity(self):
        event_a = _git_event(
            "dev-a",
            0,
            [_commit("a" * 7, 1, subject="feat: first capture (#114)")],
        )
        event_b = _git_event(
            "dev-b",
            0,
            [_commit("b" * 7, 1, subject="fix: second capture (#114)")],
        )
        event_b["projects"][0]["remote"] = "git@github.com:kb/mm.git"
        out = aggregator.aggregate_git(
            [event_a, event_b],
            since=NOW - timedelta(days=7),
            until=NOW,
            author_emails=frozenset({"kb@example.com"}),
        )
        assert out.commits == 2
        assert out.pull_requests == 1

    def test_author_window_and_commit_dedup_gates_apply_before_pr_extraction(self):
        events = [
            _git_event(
                "dev-a",
                0,
                [
                    _commit("a" * 7, 1, subject="feat: accepted (#114)"),
                    _commit("b" * 7, 1, author_email="bot@example.com", subject="feat: bot (#115)"),
                    _commit("c" * 7, 30, subject="feat: old (#116)"),
                    _commit("a" * 7, 1, subject="feat: duplicate fleet copy (#117)"),
                ],
            )
        ]
        out = aggregator.aggregate_git(
            events,
            since=NOW - timedelta(days=7),
            until=NOW,
            author_emails=frozenset({"kb@example.com"}),
        )
        assert out.commits == 1
        assert out.pull_requests == 1
        assert out.pull_request_identities == {("github.com/kb/mm", 114)}

    @pytest.mark.parametrize(
        "subject",
        [
            None,
            114,
            "",
            "feat: loose #114",
            "feat: zero (#0)",
            "feat: leading zero (#0114)",
            "feat: unicode digits (#١١٤)",
            "feat: newline (#114)\n",
            "Merge Pull Request #114 from kb/topic",
            "Merge pull request #114",
            "Merge pull request #114 from ",
            "x" * 252 + " (#114)",
        ],
    )
    def test_unsupported_or_untrusted_subjects_contribute_no_pr(self, subject):
        out = _agg_git_only([_commit("a" * 7, 1, subject=subject)])
        assert out.commits == 1
        assert out.pull_requests == 0

    def test_exactly_capped_valid_subject_is_accepted(self):
        subject = "x" * 249 + " (#114)"
        assert len(subject) == 256
        out = _agg_git_only([_commit("a" * 7, 1, subject=subject)])
        assert out.pull_request_identities == {("github.com/kb/mm", 114)}

    @pytest.mark.parametrize("remote", ["", "github.com/", "not a remote", "not/a remote"])
    def test_invalid_remote_does_not_create_pr_identity(self, remote):
        event = _git_event(
            "dev-a",
            0,
            [_commit("a" * 7, 1, subject="feat: remote absent (#114)")],
        )
        event["projects"][0]["remote"] = remote
        out = aggregator.aggregate_git(
            [event],
            since=NOW - timedelta(days=7),
            until=NOW,
            author_emails=frozenset({"kb@example.com"}),
        )
        assert out.commits == 1
        assert out.pull_requests == 0


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

    def test_mix_feeds_synthesis_rather_than_the_body(self):
        out = _agg_git_only(
            [
                _commit("a" * 7, 1, subject="feat: x"),
                _commit("b" * 7, 1, subject="fix: y"),
            ]
        )
        data = aggregator.RetroData(window_days=7, since=NOW - timedelta(days=7), until=NOW)
        data.git = out
        markdown = aggregator.format_retro(data)
        # 1.1: the mix moved to MM_THEMES_PROMPT. Its largest bucket is
        # routinely "other" — a breakdown whose top category is "could not
        # classify" does not earn a body line, but it still feeds synthesis.
        assert "Mix:" not in markdown
        raw = markdown.split("<!-- MM_THEMES_PROMPT -->", 1)[1]
        payload = json.loads(raw.split("```json", 1)[1].split("```", 1)[0])
        assert payload["commit_types"]["feat"] == 1


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


def _pair(
    events: list[dict],
    *,
    window_days: int = 7,
    emails: frozenset[str] | None = None,
) -> tuple[aggregator.PriorPeriod, aggregator.PriorPeriod]:
    since = NOW - timedelta(days=window_days)
    return aggregator._aggregate_git_period_pair(
        events,
        since - timedelta(days=window_days),
        since,
        NOW,
        emails if emails is not None else frozenset({"kb@example.com"}),
    )


def _agg_with_floor(
    tmp_path: Path,
    monkeypatch,
    events: list[dict],
    *,
    floor: str,
    window_days: int = 7,
    now: datetime = NOW,
) -> aggregator.RetroData:
    events_dir = tmp_path / "events"
    _write_events(events_dir, "dev-a", floor, events)
    monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
    monkeypatch.setattr(aggregator, "gather_author_emails", lambda: frozenset({"kb@example.com"}))
    return aggregator.aggregate(
        events_dir=events_dir,
        window_days=window_days,
        author_emails=frozenset({"kb@example.com"}),
        now=now,
    )


class TestPriorPeriodComparison:
    """Track 24B — computed prior equal period, no snapshot file."""

    def test_commit_at_exactly_since_counts_once(self):
        since = NOW - timedelta(days=7)
        events = [_git_event("dev-a", 0, [_commit("a" * 7, 7)])]
        prior, current = aggregator._aggregate_git_period_pair(
            events,
            since - timedelta(days=7),
            since,
            NOW,
            frozenset({"kb@example.com"}),
        )
        assert current.commits + prior.commits == 1
        assert current.commits == 1
        assert prior.commits == 0

    def test_boundary_day_not_counted_in_both_windows(self):
        since = NOW - timedelta(days=7)
        in_current = _commit("c" * 7, 7)
        in_prior = _commit("p" * 7, 7)
        in_prior["date"] = (since - timedelta(microseconds=1)).isoformat()
        prior, current = aggregator._aggregate_git_period_pair(
            [_git_event("dev-a", 0, [in_current, in_prior])],
            since - timedelta(days=7),
            since,
            NOW,
            frozenset({"kb@example.com"}),
        )
        assert current.commits == 1
        assert prior.commits == 1

    def test_same_sha_conflicting_dates_enters_one_period(self):
        sha = "deadbee"
        events = [
            _git_event("dev-a", 10, [_commit(sha, 10)]),
            _git_event("dev-b", 1, [_commit(sha, 1)]),
        ]
        prior, current = _pair(events)
        assert prior.commits + current.commits == 1

    def test_out_of_window_duplicate_does_not_hide_current_commit(self):
        sha = "deadbee"
        events = [
            _git_event("dev-a", 20, [_commit(sha, 20)]),
            _git_event("dev-b", 1, [_commit(sha, 1)]),
        ]
        prior, current = _pair(events)
        assert prior.commits == 0
        assert current.commits == 1

    def test_active_days_use_utc_not_machine_local_timezone(self, monkeypatch):
        def _unexpected_local_day(_dt):
            raise AssertionError("trends must not use the machine's local timezone")

        monkeypatch.setattr(aggregator, "_local_day_iso", _unexpected_local_day)
        first = _commit("a" * 7, 1)
        first["date"] = "2026-04-27T23:30:00+00:00"
        second = _commit("b" * 7, 0)
        second["date"] = "2026-04-28T00:30:00+00:00"
        _prior, current = _pair([_git_event("dev-a", 0, [first, second])])
        assert current.active_days == 2

    def test_trends_day_labels_normalize_to_utc(self):
        offset = timezone(timedelta(hours=2))
        dt = datetime(2026, 4, 28, 0, 30, tzinfo=offset)
        assert aggregator._trend_day_iso(dt) == "2026-04-27"

    def test_prior_window_before_coverage_floor_is_unavailable(self, tmp_path, monkeypatch):
        data = _agg_with_floor(
            tmp_path,
            monkeypatch,
            [_git_event("dev-a", 1, [_commit("a" * 7, 1)]), _push_event("dev-a", 1)],
            floor="2026-04-20",
        )
        assert data.comparison.status == "unavailable"
        out = aggregator.format_retro(data)
        assert "## Trends vs prior 7d" in out
        assert "Unavailable:" in out
        assert "event log starts 2026-04-20" in out
        assert "| Commits" not in out

    def test_45d_window_refused_at_retention_boundary(self):
        prior_start = NOW - timedelta(days=90)
        floor = date(2026, 1, 29)
        assert (NOW.date() - floor).days == 89
        assert aggregator._coverage_allows_prior(floor, prior_start) is False
        # The arithmetic guard this replaced would have PASSED: 2*45 > 90 is False.
        assert not (2 * 45 > aggregator.EVENTS_RETENTION_DAYS)
        data = aggregator.RetroData(
            window_days=45,
            since=NOW - timedelta(days=45),
            until=NOW,
            comparison=aggregator.PeriodComparison(status="gated"),
        )
        assert "Trends vs prior" not in aggregator.format_retro(data)

    def test_prior_window_with_no_snapshot_is_unavailable_not_zero(self, tmp_path, monkeypatch):
        data = _agg_with_floor(
            tmp_path,
            monkeypatch,
            [_git_event("dev-a", 1, [_commit("a" * 7, 1, add=99, dlt=3)])],
            floor="2026-04-25",
        )
        assert data.comparison.status == "unavailable"
        out = aggregator.format_retro(data)
        assert "Unavailable:" in out
        assert "Prior 7d" not in out
        assert "↑" not in out

    def test_unreadable_event_records_make_trends_unavailable(self, tmp_path, monkeypatch):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        (events_dir / "dev-old-2026-04-01.jsonl").write_text("not json\n", encoding="utf-8")
        _write_events(
            events_dir,
            "dev-current",
            "2026-04-28",
            [_git_event("dev-current", 1, [_commit("a" * 7, 1)])],
        )
        monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
        data = aggregator.aggregate(
            events_dir=events_dir,
            window_days=7,
            author_emails=frozenset({"kb@example.com"}),
            now=NOW,
        )
        assert data.skipped_per_source == {"events": 1}
        assert data.comparison.status == "unavailable"
        out = aggregator.format_retro(data)
        assert "event log contains unreadable records" in out
        assert "| Commits" not in out

    def test_prior_zero_current_active_renders_zero_not_dash(self, tmp_path, monkeypatch):
        data = _agg_with_floor(
            tmp_path,
            monkeypatch,
            [_git_event("dev-a", 1, [_commit("a" * 7, 1, add=10, dlt=2)])],
            floor="2026-04-01",
        )
        assert data.comparison.status == "ok"
        assert data.comparison.prior.commits == 0
        out = aggregator.format_retro(data)
        assert "| Commits" in out
        assert "—" not in out.split("## Trends vs prior 7d")[1].split("## ")[0]

    def test_current_zero_suppresses_section(self, tmp_path, monkeypatch):
        data = _agg_with_floor(
            tmp_path,
            monkeypatch,
            [_git_event("dev-a", 10, [_commit("a" * 7, 10)])],
            floor="2026-04-01",
        )
        assert data.comparison.status == "suppressed"
        assert "Trends vs prior" not in aggregator.format_retro(data)

    def test_identical_nonzero_columns_still_render(self):
        since = NOW - timedelta(days=7)
        data = aggregator.RetroData(
            window_days=7,
            since=since,
            until=NOW,
            comparison=aggregator.PeriodComparison(
                status="ok",
                prior=aggregator.PriorPeriod(commits=1, additions=4, deletions=1, active_days=1),
                current=aggregator.PriorPeriod(commits=1, additions=4, deletions=1, active_days=1),
                prior_start=since - timedelta(days=7),
                prior_end=since - timedelta(microseconds=1),
            ),
        )
        data.git = aggregator.GitAggregate(commits=1, additions=4, deletions=1)
        out = aggregator.format_retro(data)
        block = out.split("## Trends vs prior 7d")[1]
        assert "| Commits" in block
        assert "1" in block

    def test_window_days_14_omits_section(self, tmp_path, monkeypatch):
        data = _agg_with_floor(
            tmp_path,
            monkeypatch,
            [_git_event("dev-a", 1, [_commit("a" * 7, 1)])],
            floor="2026-04-01",
            window_days=14,
        )
        assert data.comparison.status == "gated"
        assert "Trends vs prior" not in aggregator.format_retro(data)

    def test_device_coverage_mismatch_is_disclosed(self, tmp_path, monkeypatch):
        data = _agg_with_floor(
            tmp_path,
            monkeypatch,
            [
                _git_event("dev-a", 1, [_commit("a" * 7, 1)]),
                _push_event("dev-a", 1),
                _push_event("dev-b", 10),
            ],
            floor="2026-04-01",
        )
        assert data.comparison.status == "ok"
        assert data.comparison.fleet_changed is True
        out = aggregator.format_retro(data)
        assert _health_entry(out, "fleet_changed") is not None

    def test_prior_window_sees_rows_outside_the_current_window(self, tmp_path, monkeypatch):
        data = _agg_with_floor(
            tmp_path,
            monkeypatch,
            [
                _git_event("dev-a", 10, [_commit("p" * 7, 10, add=5, dlt=1)]),
                _git_event("dev-a", 1, [_commit("c" * 7, 1, add=8, dlt=2)]),
            ],
            floor="2026-04-01",
        )
        assert data.comparison.status == "ok"
        assert data.comparison.prior.commits == 1
        assert data.comparison.current.commits == 1
        assert data.git.commits == 1

    def test_sessions_row_absent_from_prior_period(self):
        names = {f.name for f in dataclasses.fields(aggregator.PriorPeriod)}
        assert "sessions" not in names
        assert "tokens_total" not in names
        assert "push_events" not in names

    def test_streak_row_absent_from_prior_period(self):
        names = {f.name for f in dataclasses.fields(aggregator.PriorPeriod)}
        assert "streak_days" not in names

    def test_prior_period_holds_only_integers(self):
        hints = get_type_hints(aggregator.PriorPeriod)
        assert hints
        for name, typ in hints.items():
            assert typ is int, f"{name} is {typ}, not int"

    def test_host_tokens_do_not_reach_prior_period(self):
        git = _git_event("dev-a", 1, [_commit("a" * 7, 1)])
        host = _host_event("dev-a", NOW.isoformat())
        since = NOW - timedelta(days=7)
        emails = frozenset({"kb@example.com"})
        with_host = aggregator._aggregate_git_period_pair(
            [git, host], since - timedelta(days=7), since, NOW, emails
        )
        without = aggregator._aggregate_git_period_pair(
            [git], since - timedelta(days=7), since, NOW, emails
        )
        assert with_host == without

    def test_weekly_buckets_not_computed_for_prior_window(self, tmp_path, monkeypatch):
        calls: list[int] = []
        real = aggregator.aggregate_git

        def spy(*args, **kwargs):
            calls.append(kwargs.get("window_days", -1))
            return real(*args, **kwargs)

        monkeypatch.setattr(aggregator, "aggregate_git", spy)
        _agg_with_floor(
            tmp_path,
            monkeypatch,
            [_git_event("dev-a", 1, [_commit("a" * 7, 1)])],
            floor="2026-04-01",
        )
        assert calls == [7]

    def test_aggregate_reads_events_dir_once_and_shells_out_once(self, tmp_path, monkeypatch):
        events_dir = tmp_path / "events"
        _write_events(
            events_dir,
            "dev-a",
            "2026-04-01",
            [_git_event("dev-a", 1, [_commit("a" * 7, 1)]), _push_event("dev-a", 1)],
        )
        n = {"list": 0, "devices": 0}
        real_list = aggregator._list_event_files

        def counting_list(path, *, skip_counter):
            n["list"] += 1
            return real_list(path, skip_counter=skip_counter)

        def counting_devices():
            n["devices"] += 1
            return None, []

        monkeypatch.setattr(aggregator, "_list_event_files", counting_list)
        monkeypatch.setattr(aggregator, "get_known_devices", counting_devices)
        aggregator.aggregate(
            events_dir=events_dir,
            window_days=7,
            author_emails=frozenset({"kb@example.com"}),
            now=NOW,
        )
        assert n["list"] == 1
        assert n["devices"] == 1

    def test_snapshot_subsystem_is_gone(self):
        assert not hasattr(aggregator, "_save_snapshot")
        assert not hasattr(aggregator, "_load_prior_snapshot")
        assert not hasattr(aggregator, "_retro_to_snapshot")
        assert not hasattr(aggregator, "PriorRetroDelta")

    def test_trends_sit_below_code_shipped(self, tmp_path, monkeypatch):
        data = _agg_with_floor(
            tmp_path,
            monkeypatch,
            [_git_event("dev-a", 1, [_commit("a" * 7, 1)])],
            floor="2026-04-01",
        )
        out = aggregator.format_retro(data)
        assert out.index("## Code shipped") < out.index("## Trends vs prior 7d")


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
        data.sessions = aggregator.SessionsAggregate(
            tokens_by_model={
                "claude-sonnet-4-6": {
                    "input": 2**53,
                    "cache_create": 2**53,
                    "cache_read": 2**53,
                    "output": 2**53,
                }
            },
            pre_token_peers={f"dev-{n}" for n in range(10)},
        )
        out = aggregator.format_retro(
            data,
            name="kb",
            themes=["short", "longer theme line", "x"],
            noteworthy="medium length",
        )
        card_lines = [line for line in out.splitlines() if line.startswith(("╔", "╠", "╚", "║"))]
        assert card_lines, "card not present"
        # Every card border and interior line is exactly CARD_WIDTH chars wide.
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

    def test_agent_rows_survive_hostile_peer_buckets(self):
        """Peer-controlled model maps reach pricing and rendering. Negative
        ints, bools, numeric strings, blank ids and non-dict buckets must not
        crash the render or leak into a figure."""
        hostile: dict = {
            "claude-sonnet-4-6": {
                "input": 100,
                "cache_create": 7,
                "cache_read": 1_000,
                "output": 3,
            },
            "CLAUDE-OPUS-4-7": {
                "input": 10,
                "cache_create": 3,
                "cache_read": 4,
                "output": 5,
            },
            "gpt-5": {"input": 1, "cache_create": 2, "cache_read": 3, "output": 4},
            "grok-3": {"input": 20, "cache_create": 0, "cache_read": 0, "output": 1},
            "anthropic.claude-bedrock": {
                "input": -1,
                "cache_create": True,
                "cache_read": "9",
                "output": "not-a-number",
            },
            "<synthetic>": {
                "input": 9_999,
                "cache_create": 9_999,
                "cache_read": 9_999,
                "output": 9_999,
            },
            "": {"input": 99, "cache_create": 0, "cache_read": 0, "output": 0},
            "   ": {"input": 99, "cache_create": 0, "cache_read": 0, "output": 0},
            "zero": {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0},
            "malformed": "not-a-bucket",
        }
        data = self._baseline()
        data.sessions = aggregator.SessionsAggregate(tokens_by_model=hostile)
        out = aggregator.format_retro(data, name="kb")
        card = [line for line in out.splitlines() if line.startswith("║")]
        assert card, "card must still render"
        for line in card:
            assert len(line) == aggregator.CARD_WIDTH
        assert "not-a-number" not in out
        assert "not-a-bucket" not in out

    def test_agent_rows_preserve_accumulated_safe_peer_totals(self):
        """``_merge_token_window`` caps each peer contribution before summing,
        so two valid maximum-size peers can leave an aggregate field above the
        per-peer cap. The row must carry it, not re-clamp it."""
        data = self._baseline()
        data.sessions = aggregator.SessionsAggregate(
            tokens_by_model={
                "claude-sonnet-4-6": {
                    "input": 2**54,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                }
            },
        )
        usage = aggregator.aggregate_agent_usage(data, machines_known=None)
        claude = next(r for r in usage.rows if r.key == "claude")
        assert claude.tokens == 2**54

    def test_models_block_golden_layout_and_global_pr_reference(self):
        data = self._baseline()
        data.git.pull_request_identities = {
            ("github.com/kb/mm", 114),
            ("github.com/kb/bolt", 115),
        }
        data.sessions = aggregator.SessionsAggregate(
            tokens_by_model={
                "claude-sonnet-4-6": {
                    "input": 100,
                    "cache_create": 0,
                    "cache_read": 1_000,
                    "output": 0,
                },
                "gpt-5": {"input": 10, "cache_create": 0, "cache_read": 0, "output": 0},
                "unknown-model": {"input": 9, "cache_create": 0, "cache_read": 0, "output": 0},
            },
            pre_token_peers={"dev-b", "dev-a"},
        )
        out = aggregator.format_retro(
            data,
            name="kb",
            themes=["theme one", "theme two"],
            noteworthy="something noteworthy",
        )
        card_contents = [line[3:-3].rstrip() for line in out.splitlines() if line.startswith("║")]

        assert card_contents == [
            "kb · 2026-04-21 → 2026-04-28",
            "42 commits · 2 repos · 2 PRs · 2 machines",
            "+1.0k / -200 LOC · 37-day streak",
            "",
            # 1.1: ONE block. The pre-1.1 card carried MODELS (Claude tokens)
            # above AGENT LOGS (host day-counts) — two units, two sources, and
            # a standing prohibition on comparing them. Claude is a row here.
            "AGENTS (0 machines)",
            "Claude  1.1k tokens · 0 days · ≥$0",
            "",
            "NOTEWORTHY",
            "something noteworthy",
            "",
            "TOP WORK",
            "• theme one",
            "• theme two",
        ]
        # Coverage gaps no longer need their own card line: an incomplete
        # Claude corpus makes the row a floor, and the ≥ sits on the number.
        assert "Model-token coverage incomplete" not in out
        # Once on the card, once in the body headline — the same treatment
        # commits and LOC get. What must not happen is a second card line.
        assert out.count("2 PRs") == 2
        assert sum(1 for line in out.splitlines() if line.startswith("║") and "PRs" in line) == 1
        assert "merged" not in out
        # Second pass carries no health block; read the payload from pass one.
        first = aggregator.format_retro(data)
        assert "dev-a, dev-b" in _health_entry(first, "tokens_incomplete")["detail"]

    def test_agents_block_renders_for_name_only_second_pass(self):
        data = self._baseline()
        out = aggregator.format_retro(data, name="kb")

        assert "AGENTS (" in out
        # One empty state for every agent. The pre-1.1 card needed a
        # Claude-scoped string here ("No Claude Code model usage observed")
        # precisely because a sibling block could contradict it.
        assert "No agent usage observed this window" in out
        assert "MODELS (Claude Code sessions)" not in out
        assert "AGENT LOGS" not in out
        assert "0 PRs" in out
        assert "MM_THEMES_PROMPT" not in out

    def test_incomplete_claude_corpus_makes_the_row_a_floor(self):
        data = self._baseline()
        data.sessions = aggregator.SessionsAggregate(
            tokens_by_model={
                "claude-sonnet-4-6": {
                    "input": 100,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                }
            },
            pre_v2_peers={"old-host"},
        )
        # Second pass is the shareable artifact and carries no health block;
        # read the payload from a first-pass render.
        assert "≥$" in aggregator.format_retro(data, name="kb")
        detail = _health_entry(aggregator.format_retro(data), "tokens_incomplete")["detail"]
        assert "old-host" in detail and "pre-v0.11.0 session schema" in detail

    def test_token_coverage_notes_bound_peer_names(self):
        data = self._baseline()
        data.sessions.pre_token_peers = {f"dev-{n}" for n in range(6)}
        out = aggregator.format_retro(data)

        assert (
            "dev-0, dev-1, dev-2, dev-3, dev-4 (+1 more)"
            in _health_entry(out, "tokens_incomplete")["detail"]
        )
        assert "dev-5" not in out


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
    def test_no_save_is_accepted_as_deprecated_noop(self, tmp_path, monkeypatch, capsys):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))
        monkeypatch.setattr(aggregator, "gather_author_emails", lambda: frozenset(), raising=True)
        monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
        rc = aggregator.main(["7d", "--no-save"])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out.startswith("# Retro")
        assert "mm: notice: --no-save is a no-op as of v0.12.39" in captured.err
        assert "will be removed in 2.0" in captured.err
        assert "warning:" not in captured.err

    def test_bare_integer_window_suggests_nd(self, capsys):
        with pytest.raises(SystemExit) as exc:
            aggregator.main(["7"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "did you mean '7d'?" in err
        assert "mm retro-fleet" in err
        assert "--dump-host-usage" not in err
        assert "--no-save" not in err

    def test_theme_args_render_card(self, tmp_path, monkeypatch, capsys):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))
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
        captured = capsys.readouterr()
        out = captured.out
        assert "╔" in out
        assert "kb · " in out
        assert "alpha" in out
        assert "AGENTS (" in out
        assert "0 PRs" in out
        assert "MM_THEMES_PROMPT" not in out
        assert "mm: notice: --no-save is a no-op" in captured.err


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


# ---------------------------------------------------------------------------
# Track 22A — host-usage-snapshot consumer
# ---------------------------------------------------------------------------


def _health(out: str) -> list[dict]:
    """Parse the MM_HEALTH payload out of a first-pass render.

    Tests assert on health CODES, not on rendered prose. That is the 1.1
    contract: the body carries one summary line and the detail lives in a
    machine-readable block the skill reads with judgment.
    """
    if "<!-- MM_HEALTH -->" not in out:
        return []
    block = out.split("<!-- MM_HEALTH -->", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)["issues"]


def _health_entry(out: str, code: str) -> dict | None:
    return next((e for e in _health(out) if e["code"] == code), None)


def _health_codes(out: str) -> set[str]:
    return {e["code"] for e in _health(out)}


def _usage(n: int = 1) -> dict:
    return {"input": n, "cache_create": 0, "cache_read": 0, "output": 0}


def _sibling(hosts: dict, model: str = "gpt-5") -> dict:
    """Build a reconciling ``tokens_by_day`` for ``hosts`` using one model id."""
    by_day: dict = {}
    for family_days in hosts.values():
        for day, usage in family_days.items():
            bucket = by_day.setdefault(
                day, {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0, "by_model": {}}
            )
            for field in ("input", "cache_create", "cache_read", "output"):
                bucket[field] += usage[field]
            model_bucket = bucket["by_model"].setdefault(model, _usage(0))
            for field in ("input", "cache_create", "cache_read", "output"):
                model_bucket[field] += usage[field]
    return by_day


def _host_event(
    device: str,
    ts: str,
    *,
    token_sources: tuple[str, ...] = ("codex",),
    hosts: dict | None = None,
    extra: dict | None = None,
) -> dict:
    if hosts is None:
        hosts = {"codex": {"2026-04-20": _usage(5)}}
    days = sorted({day for family in hosts.values() for day in family})
    ev = {
        "v": 2,
        "type": "host-usage-snapshot",
        "ts": ts,
        "device": device,
        "token_sources": list(token_sources),
        "hosts": hosts,
        "active_days": days,
        "counter_semantics": "disjoint-v1",
    }
    if extra:
        ev.update(extra)
        if extra.get("counter_semantics", "disjoint-v1") is None:
            ev.pop("counter_semantics", None)
    return ev


def _accepted(ev: dict):
    result = aggregator._accept_host_usage_snapshot(ev)
    assert isinstance(result, aggregator._AcceptedHostRow), result
    return result


def test_stale_host_capture_note_is_one_class_for_two_devices():
    since = datetime(2026, 4, 25, tzinfo=timezone.utc)
    until = datetime(2026, 4, 28, tzinfo=timezone.utc)
    rows = [_host_event(device, "2026-04-20T12:00:00+00:00") for device in ("dev-a", "dev-b")]
    rows.append(_host_event("dev-c", "2026-04-27T12:00:00+00:00"))
    inventory = aggregator.aggregate_host_usage(rows, since=since, until=until, registered_ids=None)
    notes = aggregator._host_reader_coverage_notes(list(inventory.by_device.values()))
    stale = [note for note in notes if "predate this window" in note]
    assert len(stale) == 1
    assert "dev-a" in stale[0] and "dev-b" in stale[0] and "dev-c" not in stale[0]
    assert "2026-04-20" in stale[0] and "mm push" in stale[0]


class TestHostSnapshotAcceptance:
    TS = "2026-04-28T12:00:00+00:00"

    def test_valid_complete_row_accepted(self):
        row = _accepted(_host_event("dev-a", self.TS))
        assert row.device == "dev-a"
        assert row.consulted == ("codex",)
        assert "2026-04-20" in row.lifetime_by_family["codex"]

    def test_empty_hosts_and_sources_accepted(self):
        ev = _host_event("dev-a", self.TS, token_sources=(), hosts={})
        ev["active_days"] = []
        row = _accepted(ev)
        assert row.lifetime_by_family == {}
        assert row.consulted == ()

    def test_naive_ts_rejected(self):
        ev = _host_event("dev-a", "2026-04-28T12:00:00")
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "naive_timestamp"

    def test_token_sources_out_of_order_rejected(self):
        ev = _host_event("dev-a", self.TS, token_sources=("grok", "codex"))
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_token_sources"

    def test_token_sources_duplicate_rejected(self):
        ev = _host_event("dev-a", self.TS, token_sources=("codex", "codex"))
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_token_sources"

    def test_unknown_token_source_retained_not_rejected(self):
        """Unknown source names are version skew, not writer corruption.

        A closed vocabulary rejected the whole row, which dropped valid Codex
        totals from a legacy peer that still named a retired reader. Retain
        the unknown name (so degraded_sources evidence survives) and keep
        the row. Duplicates and known-name-out-of-order stay fatal.
        """
        ev = _host_event("dev-a", self.TS, token_sources=("windsurf",))
        row = _accepted(ev)
        assert row.consulted == ("windsurf",)
        assert "codex" in row.lifetime_by_family

    def test_legacy_peer_row_with_opencode_is_accepted_whole(self):
        """36A deletes the OpenCode reader; 36B dropped the wire name.

        A peer still emitting ``opencode`` in any of the three source lists
        must be accepted WHOLE — the aggregator retains unknown names.
        Dropping the field (or the row) would erase that device's host view
        for the 90-day window. All three route through
        ``_token_sources_subsequence``.
        """
        ts = self.TS
        token_row = _accepted(_host_event("dev-a", ts, token_sources=("codex", "grok", "opencode")))
        assert token_row.consulted == ("codex", "grok", "opencode")

        degraded_row = _accepted(
            _host_event(
                "dev-a",
                ts,
                token_sources=("codex",),
                extra={"degraded_sources": ["opencode"]},
            )
        )
        assert degraded_row.degraded == ("opencode",)
        assert degraded_row.degraded_reason is None
        assert degraded_row.consulted == ("codex",)

        partial_row = _accepted(
            _host_event(
                "dev-a",
                ts,
                token_sources=("codex", "opencode"),
                extra={"partial_sources": ["opencode"]},
            )
        )
        assert partial_row.partial == ("opencode",)
        assert partial_row.partial_reason is None
        assert partial_row.consulted == ("codex", "opencode")

    def test_nonempty_hosts_empty_sources_rejected(self):
        ev = _host_event("dev-a", self.TS, token_sources=())
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_token_sources"

    def test_unknown_family_rejected(self):
        ev = _host_event("dev-a", self.TS, hosts={"windsurf": {"2026-04-20": _usage(1)}})
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_counter"

    def test_invalid_days_rejected(self):
        for day in ("2026-02-30", "20260815", "2026-W33-5", "2026-1-02"):
            ev = _host_event("dev-a", self.TS, hosts={"codex": {day: _usage(1)}})
            result = aggregator._accept_host_usage_snapshot(ev)
            assert isinstance(result, aggregator.HostReject), day
            assert result.reason == "invalid_day", day

    def test_bool_token_field_rejected_not_clamped(self):
        bucket = {"input": True, "cache_create": 0, "cache_read": 0, "output": 0}
        ev = _host_event("dev-a", self.TS, hosts={"codex": {"2026-04-20": bucket}})
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_counter"

    def test_max_counter_accepted_overflow_rejected(self):
        ok = _host_event(
            "dev-a",
            self.TS,
            hosts={"codex": {"2026-04-20": {**_usage(0), "input": 2**53}}},
        )
        assert isinstance(aggregator._accept_host_usage_snapshot(ok), aggregator._AcceptedHostRow)
        bad = _host_event(
            "dev-a",
            self.TS,
            hosts={"codex": {"2026-04-20": {**_usage(0), "input": 2**53 + 1}}},
        )
        result = aggregator._accept_host_usage_snapshot(bad)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_counter"

    def test_zero_bucket_accepted(self):
        ev = _host_event("dev-a", self.TS, hosts={"codex": {"2026-04-20": _usage(0)}})
        assert isinstance(aggregator._accept_host_usage_snapshot(ev), aggregator._AcceptedHostRow)

    def test_empty_family_map_rejected(self):
        ev = _host_event("dev-a", self.TS, hosts={"codex": {}})
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_counter"

    def test_float_schema_rejected(self):
        ev = _host_event("dev-a", self.TS)
        ev["v"] = 2.0
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "unsupported_schema"

    def test_active_days_mismatch_rejected(self):
        ev = _host_event("dev-a", self.TS)
        ev["active_days"] = ["2026-04-21"]
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "active_days_mismatch"

    def test_unknown_top_level_field_ignored(self):
        ev = _host_event("dev-a", self.TS, extra={"note": "peer additive"})
        assert isinstance(aggregator._accept_host_usage_snapshot(ev), aggregator._AcceptedHostRow)

    def test_wrong_schema_rejected(self):
        ev = _host_event("dev-a", self.TS)
        ev["v"] = 1
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "unsupported_schema"

    def test_host_counter_predicate_matches_reader(self):
        from mind_meld import host_usage

        samples = [0, 1, 2**53, 2**53 + 1, -1, True, False, 1.0, "10", None]
        for sample in samples:
            assert aggregator._host_counter_ok(sample) == host_usage._is_valid_counter(sample)


# Frozen history of the host-usage wire vocabulary as shipped through
# v0.12.52. Never edit this: it is the compatibility contract, not the
# live reader set. Deleting a name here would silently drop the
# parametrized coverage of that shape.
_SHIPPED_WIRE_SOURCE_NAMES = ("codex", "grok", "opencode")


def _ordered_nonempty_subsequences(names: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    out = []
    for r in range(1, len(names) + 1):
        for combo in combinations(range(len(names)), r):
            out.append(tuple(names[i] for i in combo))
    return tuple(out)


class TestHostSnapshotWireCompat:
    """Acceptor-side compatibility for the shipped wire vocabulary.

    The pin lives on the acceptor, never the writer. ``make_host_usage_snapshot``
    echoes ``token_sources`` verbatim, so a writer-side assertion passes at
    100% while the acceptor rejects the row.
    """

    TS = "2026-04-28T12:00:00+00:00"

    def test_observed_live_shape_codex_opencode_accepted(self):
        """Device 3a6c7dc9's latest row (2026-08-30): token_sources=['codex','opencode']."""
        ev = _host_event("dev-a", self.TS, token_sources=("codex", "opencode"))
        row = _accepted(ev)
        assert row.consulted == ("codex", "opencode")
        assert "codex" in row.lifetime_by_family
        rendered = aggregator.format_retro(_econ_data([ev]))
        # 1.1 removed per-machine reader reporting with the rest of the
        # per-machine inventory, so a HEALTHY retired reader name no longer
        # reaches any surface. What must still hold — and is the whole point
        # of the retained-but-unmapped vocabulary — is that the raw
        # peer-controlled string never leaks into rendered output.
        assert "opencode" not in rendered

    def test_shipped_three_name_shape_accepted_on_acceptor(self):
        """The carded ['codex','grok','opencode'] shape, asserted on the acceptor."""
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=("codex", "grok", "opencode"),
            hosts={
                "codex": {"2026-04-20": _usage(5)},
                "grok": {"2026-04-20": _usage(3)},
            },
        )
        row = _accepted(ev)
        assert row.consulted == ("codex", "grok", "opencode")
        assert "codex" in row.lifetime_by_family
        assert "grok" in row.lifetime_by_family

    @pytest.mark.parametrize("sources", _ordered_nonempty_subsequences(_SHIPPED_WIRE_SOURCE_NAMES))
    def test_every_ordered_subsequence_of_shipped_vocabulary_accepted(self, sources):
        ev = _host_event("dev-a", self.TS, token_sources=sources)
        row = _accepted(ev)
        assert row.consulted == sources
        assert "codex" in row.lifetime_by_family

    def test_degraded_opencode_is_accepted_and_renders_visible_note(self):
        """The 2am test. Device 889e42c0 (2026-09-01): token_sources=['codex'],
        degraded_sources=['opencode']. Skipping the unknown name would erase
        the failure signal and report healthy coverage."""
        hosts = _priced_hosts()
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=("codex",),
            hosts=hosts,
            extra={
                "tokens_by_day": _sibling(hosts, "gpt-5.6-terra"),
                "degraded_sources": ["opencode"],
            },
        )
        row = _accepted(ev)
        assert row.consulted == ("codex",)
        assert row.degraded == ("opencode",)
        rendered = aggregator.format_retro(_econ_data([ev]))
        assert "failed" in rendered
        assert aggregator._UNKNOWN_READER_LABEL in rendered
        assert "opencode" not in rendered
        assert "mm diag" in rendered

    def test_known_name_out_of_order_still_rejected(self):
        ev = _host_event("dev-a", self.TS, token_sources=("grok", "codex"))
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_token_sources"

    def test_unknown_name_does_not_mask_known_name_out_of_order(self):
        """A naive continue-on-unknown would accept this by dropping grok's
        successor check. Membership is tested before the positional scan,
        and an unknown name must not advance the known-name cursor."""
        ev = _host_event("dev-a", self.TS, token_sources=("grok", "opencode", "codex"))
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_token_sources"

    def test_retired_name_first_then_known_is_accepted(self):
        ev = _host_event("dev-a", self.TS, token_sources=("opencode", "codex"))
        row = _accepted(ev)
        assert row.consulted == ("opencode", "codex")

    def test_duplicate_still_rejected(self):
        ev = _host_event("dev-a", self.TS, token_sources=("codex", "codex"))
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_token_sources"

    def test_duplicate_unknown_name_still_rejected(self):
        ev = _host_event("dev-a", self.TS, token_sources=("opencode", "opencode"))
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator.HostReject)
        assert result.reason == "invalid_token_sources"

    def test_unknown_token_source_failing_identifier_bound_rejected(self):
        for bad in ("foo/bar", "x" * 200, "codex\n", "a:b"):
            ev = _host_event("dev-a", self.TS, token_sources=(bad,))
            result = aggregator._accept_host_usage_snapshot(ev)
            assert isinstance(result, aggregator.HostReject), bad
            assert result.reason == "invalid_token_sources", bad

    def test_degraded_sources_failing_identifier_bound_drops_field_keeps_row(self):
        ev = _host_event("dev-a", self.TS, extra={"degraded_sources": ["foo/bar"]})
        row = _accepted(ev)
        assert row.degraded == ()
        assert row.degraded_reason == "invalid_token_sources"
        assert "codex" in row.lifetime_by_family


class TestCoverageAcceptor:
    TS = "2026-04-28T12:00:00+00:00"

    def test_e1_three_way_on_key_presence(self):
        absent = _accepted(_host_event("dev-a", self.TS))
        assert absent.degraded == ()
        assert absent.degraded_reason is None
        assert absent.partial == ()
        assert absent.partial_reason is None

        empty = _host_event("dev-a", self.TS, extra={"degraded_sources": [], "partial_sources": []})
        present_empty = _accepted(empty)
        assert present_empty.degraded == ()
        assert present_empty.degraded_reason is None

        invalid = _host_event("dev-a", self.TS, extra={"degraded_sources": "grok"})
        row = aggregator._accept_host_usage_snapshot(invalid)
        assert isinstance(row, aggregator._AcceptedHostRow)
        assert row.degraded == ()
        assert row.degraded_reason == "invalid_token_sources"

    def test_e2_unknown_reader_name_is_retained_on_degraded_sources(self):
        """Valid unknown names are version skew. RETAIN them so a failed
        retired reader still shows up as degraded coverage, not as healthy."""
        ev = _host_event("dev-a", self.TS, extra={"degraded_sources": ["gemini"]})
        row = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(row, aggregator._AcceptedHostRow)
        assert row.degraded == ("gemini",)
        assert row.degraded_reason is None

    def test_e3_duplicates_non_list_and_oversize_drop_the_field(self):
        for raw in (["grok", "grok"], {"grok": True}, ["codex", "grok", "opencode", "codex"]):
            ev = _host_event("dev-a", self.TS, extra={"partial_sources": raw})
            row = aggregator._accept_host_usage_snapshot(ev)
            assert isinstance(row, aggregator._AcceptedHostRow)
            assert row.partial == ()
            assert row.partial_reason == "invalid_token_sources"

    def test_e4_drop_reason_is_recorded(self):
        ev = _host_event("dev-a", self.TS, extra={"degraded_sources": ["foo/bar"]})
        row = _accepted(ev)
        assert row.degraded == ()
        assert row.degraded_reason == "invalid_token_sources"

    def test_e5_partial_beside_empty_hosts_is_rejected(self):
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=(),
            hosts={},
            extra={"partial_sources": ["grok"]},
        )
        ev["active_days"] = []
        row = _accepted(ev)
        assert row.lifetime_by_family == {}
        assert row.partial == ()
        assert row.partial_reason == "invalid_coverage"

    def test_e6_overlap_drops_all_partial_disjoint_pair_survives(self):
        ts = self.TS
        overlap = _host_event(
            "dev-a",
            ts,
            extra={"degraded_sources": ["grok"], "partial_sources": ["grok"]},
        )
        row = _accepted(overlap)
        assert row.degraded == ("grok",)
        assert row.partial == ()
        assert row.partial_reason == "invalid_coverage"
        both = _host_event(
            "dev-a",
            ts,
            extra={"degraded_sources": ["grok"], "partial_sources": ["codex"]},
        )
        kept = _accepted(both)
        assert kept.degraded == ("grok",)
        assert kept.partial == ("codex",)

    def test_e6c_degraded_naming_a_contributor_is_dropped(self):
        """Found by Greptile on PR #151.

        The writer keeps ``degraded_sources`` DISJOINT from
        ``token_sources``. A peer that violates it made the card say a
        reader that plainly contributed "failed on the latest push", and
        sent the user to ``mm diag`` for a reader that is fine.
        """
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=("codex",),
            extra={"degraded_sources": ["codex"]},
        )
        row = _accepted(ev)
        assert row.degraded == ()
        assert row.degraded_reason == "invalid_coverage"

    def test_e6d_partial_outside_token_sources_is_dropped(self):
        """``partial_sources`` is a SUBSET of ``token_sources`` by contract.

        A reader nobody consulted cannot have "reported incomplete totals".
        """
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=("codex",),
            extra={"partial_sources": ["grok"]},
        )
        row = _accepted(ev)
        assert row.partial == ()
        assert row.partial_reason == "invalid_coverage"

    def test_e6e_contradicting_field_drops_alone(self):
        """Drop the contradicting field, keep the row and its valid sibling."""
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=("codex",),
            extra={"degraded_sources": ["codex"], "partial_sources": ["codex"]},
        )
        row = _accepted(ev)
        assert row.lifetime_by_family["codex"]
        assert row.degraded == ()
        assert row.degraded_reason == "invalid_coverage"
        assert row.partial == ()
        assert row.partial_reason == "invalid_coverage"

    def test_e6f_writer_output_survives_the_contract_checks(self):
        """The real writer never trips them — a round trip stays intact."""
        from mind_meld import events

        row = events.make_host_usage_snapshot(
            device="dev-a",
            token_sources=("codex",),
            hosts={"codex": {"2026-04-20": _usage(4)}},
            ts=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
            degraded_sources=("grok",),
            partial_days={"codex": ["2026-04-20"]},
        )
        accepted = _accepted(json.loads(json.dumps(row)))
        assert accepted.degraded == ("grok",)
        assert accepted.degraded_reason is None
        assert accepted.partial == ("codex",)
        assert accepted.partial_reason is None

    def test_e6b_valid_partial_sources_are_accepted(self):
        ev = _host_event(
            "laptop",
            self.TS,
            token_sources=("grok",),
            hosts={"grok": {"2026-04-20": _usage(5)}},
            extra={"partial_sources": ["grok"]},
        )
        row = _accepted(ev)
        assert row.partial == ("grok",)
        assert row.partial_reason is None
        inv = aggregator.aggregate_host_usage(
            [ev],
            since=datetime(2026, 4, 21, tzinfo=timezone.utc),
            until=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
            registered_ids=None,
        )
        dumped = json.loads(aggregator._dump_host_inventory(inv))["by_device"]["laptop"]
        assert dumped["partial"] == ["grok"]
        assert "incomplete totals" in dumped["partial_phrase"]

    def test_e7_genuinely_absent_coverage_selects_identically_in_both_orders(self):
        a = _host_event("dev-a", self.TS, hosts={"codex": {"2026-04-20": _usage(4)}})
        b = _host_event("dev-a", self.TS, hosts={"codex": {"2026-04-20": _usage(4)}})
        b["hosts"]["codex"]["2026-04-20"]["output"] = 0
        b["hosts"]["codex"]["2026-04-20"]["input"] = 4
        # Same family totals, no coverage fields. Selection must match reversed input.
        first = aggregator.aggregate_host_usage(
            [a, b],
            since=datetime(2026, 4, 21, tzinfo=timezone.utc),
            until=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
            registered_ids=None,
        )
        second = aggregator.aggregate_host_usage(
            [b, a],
            since=datetime(2026, 4, 21, tzinfo=timezone.utc),
            until=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
            registered_ids=None,
        )
        assert (
            first.by_device["dev-a"].lifetime_by_family
            == second.by_device["dev-a"].lifetime_by_family
        )
        ra = aggregator._accept_host_usage_snapshot(a)
        rb = aggregator._accept_host_usage_snapshot(b)
        assert aggregator._sibling_tie_key(ra) == aggregator._sibling_tie_key(rb)

    def test_e8_present_but_invalid_may_change_the_winner(self):
        ts = self.TS
        plain = _host_event("dev-a", ts)
        malformed = _host_event("dev-a", ts, extra={"degraded_sources": ["foo/bar"]})
        ra = _accepted(plain)
        rb = aggregator._accept_host_usage_snapshot(malformed)
        assert isinstance(rb, aggregator._AcceptedHostRow)
        assert ra.tie_key == rb.tie_key
        assert aggregator._sibling_tie_key(ra) != aggregator._sibling_tie_key(rb)


class TestHostSnapshotSelection:
    SINCE = datetime(2026, 4, 21, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)

    def _agg(self, events, registered=None):
        return aggregator.aggregate_host_usage(
            events,
            since=self.SINCE,
            until=self.UNTIL,
            registered_ids=registered,
        )

    def test_later_row_replaces_whole_view(self):
        t1 = _host_event(
            "dev-a",
            "2026-04-22T12:00:00+00:00",
            token_sources=("codex",),
            hosts={"codex": {"2026-04-20": _usage(9)}},
        )
        t2 = _host_event(
            "dev-a",
            "2026-04-27T12:00:00+00:00",
            token_sources=("opencode",),
            hosts={"codex": {"2026-04-26": _usage(1)}},
        )
        inv = self._agg([t1, t2])
        snap = inv.by_device["dev-a"]
        assert snap.consulted == ("opencode",)
        assert "2026-04-20" not in snap.lifetime_by_family.get("codex", {})
        assert "2026-04-26" in snap.lifetime_by_family["codex"]

    def test_invalid_later_row_keeps_earlier(self):
        t1 = _host_event("dev-a", "2026-04-22T12:00:00+00:00")
        t2 = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        t2["v"] = 1
        inv = self._agg([t1, t2])
        assert inv.by_device["dev-a"].as_of.day == 22
        assert inv.rejected_rows == 1

    def test_equal_ts_uses_lex_greatest_core_json(self):
        ts = "2026-04-27T12:00:00+00:00"
        a = _host_event(
            "dev-a",
            ts,
            token_sources=("codex",),
            hosts={"codex": {"2026-04-20": _usage(1)}},
        )
        b = _host_event(
            "dev-a",
            ts,
            token_sources=("codex", "grok"),
            hosts={"codex": {"2026-04-20": _usage(1)}},
        )
        key_a = _accepted(a).tie_key
        key_b = _accepted(b).tie_key
        expected = ("codex",) if key_a > key_b else ("codex", "grok")
        assert key_a != key_b
        assert self._agg([a, b]).by_device["dev-a"].consulted == expected
        assert self._agg([b, a]).by_device["dev-a"].consulted == expected

    def test_additive_field_does_not_change_tie(self):
        """Unknown top-level fields still cannot change a winner.

        ``tokens_by_day`` is different: once the field carries semantics, a
        quality rank (valid > absent > invalid) re-breaks equal-``tie_key``
        rows. That rank sits strictly BELOW ``tie_key``, so this test of
        unknown-field exclusion stays true.
        """
        ts = "2026-04-27T12:00:00+00:00"
        a = _host_event("dev-a", ts)
        b = _host_event("dev-a", ts, extra={"zzz": "noise"})
        assert self._agg([a, b]).by_device["dev-a"].consulted == ("codex",)
        assert self._agg([b, a]).by_device["dev-a"].consulted == ("codex",)

    def test_clock_backdated_later_in_file_loses(self):
        newer = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        older = _host_event("dev-a", "2026-04-22T12:00:00+00:00")
        inv = self._agg([newer, older])
        assert inv.by_device["dev-a"].as_of.day == 27

    def test_older_than_since_is_stale_not_missing(self):
        ev = _host_event("dev-a", "2026-04-10T12:00:00+00:00")
        inv = self._agg([ev], registered=frozenset({"dev-a"}))
        assert inv.by_device["dev-a"].stale is True
        assert "dev-a" not in inv.devices_without_accepted_row


class TestHostSnapshotNoWindowSpend:
    SINCE = datetime(2026, 4, 21, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)

    def test_out_of_window_day_keys_retained(self):
        ev = _host_event(
            "dev-a",
            "2026-04-27T12:00:00+00:00",
            hosts={
                "codex": {
                    "2026-03-01": _usage(9),
                    "2026-04-22": _usage(1),
                }
            },
        )
        inv = aggregator.aggregate_host_usage(
            [ev], since=self.SINCE, until=self.UNTIL, registered_ids=None
        )
        days = set(inv.by_device["dev-a"].lifetime_by_family["codex"])
        assert days == {"2026-03-01", "2026-04-22"}

    def test_two_devices_not_summed(self):
        a = _host_event(
            "dev-a",
            "2026-04-27T12:00:00+00:00",
            hosts={"codex": {"2026-04-22": _usage(3)}},
        )
        b = _host_event(
            "dev-b",
            "2026-04-27T13:00:00+00:00",
            hosts={"codex": {"2026-04-22": _usage(4)}},
        )
        inv = aggregator.aggregate_host_usage(
            [a, b], since=self.SINCE, until=self.UNTIL, registered_ids=None
        )
        assert inv.by_device["dev-a"].lifetime_by_family["codex"]["2026-04-22"]["input"] == 3
        assert inv.by_device["dev-b"].lifetime_by_family["codex"]["2026-04-22"]["input"] == 4
        assert not hasattr(inv, "consulted_sources")
        assert not hasattr(inv, "hosts")

    def test_writer_round_trip_accepted(self):
        from mind_meld import events

        row = events.make_host_usage_snapshot(
            device="dev-a",
            token_sources=("codex",),
            hosts={"codex": {"2026-04-20": _usage(4)}},
            ts=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
        )
        raw = json.loads(json.dumps(row))
        assert isinstance(aggregator._accept_host_usage_snapshot(raw), aggregator._AcceptedHostRow)

    def test_acceptor_carries_degraded_sources_and_tie_key_excludes_it(self):
        """T2: degraded joins ``_sibling_tie_key``, never ``_tie_break_key``.

        A degradation must not make a row *win*. The ``tie_key`` equality
        assertion is a real invariant and stays. Present-but-invalid MAY
        change the sibling-key winner, which is intended — the same
        argument ``_sibling_tie_key`` already makes for ``detail_reason``.
        """
        from mind_meld import events

        ts = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)
        plain = events.make_host_usage_snapshot(
            device="dev-a",
            token_sources=("codex",),
            hosts={"codex": {"2026-04-20": _usage(4)}},
            ts=ts,
        )
        degraded = events.make_host_usage_snapshot(
            device="dev-a",
            token_sources=("codex",),
            hosts={"codex": {"2026-04-20": _usage(4)}},
            ts=ts,
            degraded_sources=("grok",),
        )
        accepted_plain = aggregator._accept_host_usage_snapshot(json.loads(json.dumps(plain)))
        accepted_degraded = aggregator._accept_host_usage_snapshot(json.loads(json.dumps(degraded)))
        assert isinstance(accepted_plain, aggregator._AcceptedHostRow)
        assert isinstance(accepted_degraded, aggregator._AcceptedHostRow)
        assert accepted_plain.tie_key == accepted_degraded.tie_key
        assert accepted_degraded.degraded == ("grok",)
        assert aggregator._sibling_tie_key(accepted_plain) != aggregator._sibling_tie_key(
            accepted_degraded
        )

    def test_mutating_input_does_not_change_view(self):
        ev = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        inv = aggregator.aggregate_host_usage(
            [ev], since=self.SINCE, until=self.UNTIL, registered_ids=None
        )
        ev["hosts"]["codex"]["2026-04-20"]["input"] = 99
        assert inv.by_device["dev-a"].lifetime_by_family["codex"]["2026-04-20"]["input"] == 5

    def test_host_rows_do_not_change_format_or_cost(self, tmp_path, monkeypatch):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        host = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        session = {
            "v": 2,
            "type": "sessions-snapshot",
            "ts": "2026-04-27T12:00:00+00:00",
            "device": "dev-a",
            "projects": [
                {
                    "claude_dir": "-tmp-proj",
                    "source_root": "/Users/kb/.claude",
                    "sessions": 1,
                    "last_session_at": "2026-04-27T12:00:00+00:00",
                    "tokens_by_day": {
                        "2026-04-27": {
                            "input": 10,
                            "cache_create": 0,
                            "cache_read": 0,
                            "output": 2,
                            "by_model": {
                                "claude-opus-5": {
                                    "input": 10,
                                    "cache_create": 0,
                                    "cache_read": 0,
                                    "output": 2,
                                }
                            },
                        }
                    },
                }
            ],
        }
        (events_dir / "dev-a-2026-04-27.jsonl").write_text(
            json.dumps(session) + "\n" + json.dumps(host) + "\n"
        )
        monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
        with_host = aggregator.aggregate(
            events_dir=events_dir,
            window_days=14,
            author_emails=frozenset(),
            now=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
        )
        (events_dir / "dev-a-2026-04-27.jsonl").write_text(json.dumps(session) + "\n")
        without = aggregator.aggregate(
            events_dir=events_dir,
            window_days=14,
            author_emails=frozenset(),
            now=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
        )

        with_text = aggregator.format_retro(with_host)
        without_text = aggregator.format_retro(without)
        assert with_text != without_text, "host inventory should render something"
        # 1.1: host data is a ROW in the shared Agents table rather than its own
        # section, so the isolation contract is stated on the row. Adding a
        # Codex row must not move one number on the Claude row.
        assert "| Codex |" in with_text
        assert "| Codex |" not in without_text

        def _claude_row(data):
            usage = aggregator.aggregate_agent_usage(data, machines_known=None)
            return next(r for r in usage.rows if r.key == "claude")

        assert _claude_row(with_host) == _claude_row(without)
        # Host data never reaches the prior-period integers either.
        assert with_host.comparison.prior == without.comparison.prior
        assert with_host.comparison.current == without.comparison.current
        assert with_host.sessions.tokens_by_model == without.sessions.tokens_by_model
        assert with_host.host_inventory.by_device
        assert not without.host_inventory.by_device


class TestHostSnapshotCoverage:
    SINCE = datetime(2026, 4, 21, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)

    def _agg(self, events, registered):
        return aggregator.aggregate_host_usage(
            events,
            since=self.SINCE,
            until=self.UNTIL,
            registered_ids=registered,
        )

    def test_missing_registered_device_listed(self):
        ev = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        inv = self._agg([ev], frozenset({"dev-a", "dev-b"}))
        assert "dev-b" in inv.devices_without_accepted_row
        assert "dev-b" not in inv.by_device

    def test_codex_only_does_not_imply_grok(self):
        ev = _host_event("dev-a", "2026-04-27T12:00:00+00:00", token_sources=("codex",))
        inv = self._agg([ev], None)
        assert "grok" not in inv.by_device["dev-a"].consulted

    def test_unregistered_dropped_when_registry_up(self):
        ev = _host_event("ghost", "2026-04-27T12:00:00+00:00")
        inv = self._agg([ev], frozenset({"dev-a"}))
        assert "ghost" not in inv.by_device
        assert "dev-a" in inv.devices_without_accepted_row

    def test_one_grok_device_is_not_fleet_flag(self):
        ev = _host_event(
            "dev-a",
            "2026-04-27T12:00:00+00:00",
            token_sources=("codex", "grok"),
        )
        inv = self._agg([ev], frozenset({"dev-a", "dev-b"}))
        assert not hasattr(inv, "consulted_sources")
        assert inv.by_device["dev-a"].consulted == ("codex", "grok")
        assert "dev-b" in inv.devices_without_accepted_row

    def test_two_valid_rows_zero_rejects(self):
        a = _host_event("dev-a", "2026-04-22T12:00:00+00:00")
        b = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        inv = self._agg([a, b], None)
        assert inv.rejected_rows == 0

    def test_non_host_events_are_not_rejects(self):
        push = {
            "v": 2,
            "type": "mm-push",
            "ts": "2026-04-27T12:00:00+00:00",
            "device": "dev-a",
        }
        inv = self._agg([push], None)
        assert inv.rejected_rows == 0

    def test_registry_none_vs_empty(self):
        ev = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        keep = self._agg([ev], None)
        drop = self._agg([ev], frozenset())
        assert "dev-a" in keep.by_device
        assert keep.devices_without_accepted_row == frozenset()
        assert drop.by_device == {}
        assert drop.devices_without_accepted_row == frozenset()

    def test_exactly_at_since_is_not_stale(self):
        ev = _host_event("dev-a", "2026-04-21T00:00:00+00:00")
        inv = self._agg([ev], None)
        assert inv.by_device["dev-a"].stale is False
        assert inv.by_device["dev-a"].current is True

    def test_near_future_is_future_dated_not_fresh(self):
        ev = _host_event("dev-a", "2026-04-28T18:00:00+00:00")
        inv = self._agg([ev], None)
        snap = inv.by_device["dev-a"]
        assert snap.future_dated is True
        assert snap.current is False

    def test_far_future_rejected(self):
        ev = _host_event("dev-a", "2099-01-01T00:00:00+00:00")
        inv = self._agg([ev], None)
        assert "dev-a" not in inv.by_device
        assert inv.rejected[0].reason == "future_timestamp"


class TestDumpHostUsage:
    def test_dump_flag_skips_markdown(self, tmp_path, monkeypatch, capsys):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        ev = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        (events_dir / "dev-a.jsonl").write_text(json.dumps(ev) + "\n")
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))
        monkeypatch.setattr(aggregator, "gather_author_emails", lambda: frozenset())
        monkeypatch.setattr(aggregator, "get_known_devices", lambda: (None, []))
        rc = aggregator.main(["7d", "--dump-host-usage"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "dev-a" in payload["by_device"]
        assert payload["by_device"]["dev-a"]["detail"] == "absent"
        assert "detail_phrase" in payload["by_device"]["dev-a"]
        assert "degraded" in payload["by_device"]["dev-a"]
        assert "partial" in payload["by_device"]["dev-a"]
        assert "degraded_phrase" in payload["by_device"]["dev-a"]
        assert "partial_phrase" in payload["by_device"]["dev-a"]
        assert "# Retro" not in out
        assert "MM_THEMES_PROMPT" not in out

    def test_f7_dump_carries_drop_reason_for_malformed_coverage(self):
        ev = _host_event(
            "dev-a", "2026-04-27T12:00:00+00:00", extra={"degraded_sources": ["foo/bar"]}
        )
        inv = aggregator.aggregate_host_usage(
            [ev],
            since=datetime(2026, 4, 21, tzinfo=timezone.utc),
            until=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
            registered_ids=None,
        )
        payload = json.loads(aggregator._dump_host_inventory(inv))
        snap = payload["by_device"]["dev-a"]
        assert snap["degraded"] == []
        assert snap["degraded_reason"] == "invalid_token_sources"
        assert "invalid coverage metadata" in snap["degraded_phrase"]


class TestHostTokensByDayAcceptance:
    TS = "2026-04-28T12:00:00+00:00"
    SINCE = datetime(2026, 4, 21, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)

    def _agg(self, events, registered=None):
        return aggregator.aggregate_host_usage(
            events, since=self.SINCE, until=self.UNTIL, registered_ids=registered
        )

    def test_absent_sibling_is_accepted_as_pre_33a_peer(self):
        ev = _host_event("dev-a", self.TS)
        assert "tokens_by_day" not in ev
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason is None
        assert row.tokens_by_day is None
        inv = self._agg([ev])
        assert inv.by_device["dev-a"].lifetime_by_family["codex"]
        assert inv.by_device["dev-a"].detail == "absent"

    def test_present_empty_sibling_is_valid_when_hosts_empty(self):
        ev = _host_event("dev-a", self.TS, token_sources=(), hosts={})
        ev["active_days"] = []
        ev["tokens_by_day"] = {}
        row = _accepted(ev)
        assert row.detail == "present"
        assert row.tokens_by_day == {}

    def test_present_invalid_drops_detail_keeps_row(self):
        ev = _host_event("dev-a", self.TS, extra={"tokens_by_day": "nope"})
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "unsupported_schema"
        assert row.lifetime_by_family["codex"]
        inv = self._agg([ev])
        assert "dev-a" in inv.by_device
        assert inv.rejected_rows == 0

    def test_invalid_sibling_never_rejects_the_row(self):
        ev = _host_event("dev-a", self.TS, extra={"tokens_by_day": {"bad": 1}})
        result = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(result, aggregator._AcceptedHostRow)
        bad_hosts = _host_event("dev-b", self.TS)
        bad_hosts["hosts"] = {"codex": {"2026-04-20": {"input": "nope"}}}
        rejected = aggregator._accept_host_usage_snapshot(bad_hosts)
        assert isinstance(rejected, aggregator.HostReject)

    def test_day_set_equality_against_active_days(self):
        hosts = {"codex": {"2026-04-20": _usage(5)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": {"2026-04-21": {**_usage(5), "by_model": {"gpt-5": _usage(5)}}}
            },
        )
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "active_days_mismatch"

    def test_day_level_sum_reconciliation(self):
        hosts = {
            "codex": {"2026-04-20": _usage(4)},
            "grok": {"2026-04-20": _usage(3)},
        }
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=("codex", "grok"),
            hosts=hosts,
            extra={"tokens_by_day": _sibling(hosts, "gpt-5")},
        )
        # One model carrying both families' totals still reconciles day-wise.
        ev["tokens_by_day"]["2026-04-20"]["by_model"] = {
            "gpt-5": _usage(4),
            "grok-4": _usage(3),
        }
        row = _accepted(ev)
        assert row.detail == "present"
        assert row.tokens_by_day["2026-04-20"]["input"] == 7

    def test_model_this_consumer_would_classify_differently_still_reconciles(self):
        """T13c: do NOT reconcile per family via host_family().

        A newer peer classified this model as ``other``; this consumer's
        ``host_family("gpt-5")`` is ``codex``. Day-level sums still match.
        """
        assert host_usage.host_family("gpt-5") == "codex"
        hosts = {"other": {"2026-04-20": _usage(5)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {**_usage(5), "by_model": {"gpt-5": _usage(5)}},
                }
            },
        )
        row = _accepted(ev)
        assert row.detail == "present"
        assert row.tokens_by_day["2026-04-20"]["by_model"]["gpt-5"]["input"] == 5

    def test_non_str_model_id_drops_detail(self):
        ev = _host_event(
            "dev-a",
            self.TS,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {**_usage(5), "by_model": {1: _usage(5)}},
                }
            },
        )
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "unsupported_schema"

    def test_empty_model_id_drops_detail(self):
        ev = _host_event(
            "dev-a",
            self.TS,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {**_usage(5), "by_model": {"": _usage(5)}},
                }
            },
        )
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "unsupported_schema"

    def test_overlong_model_id_drops_detail(self):
        model = "g" * (aggregator.MAX_HOST_MODEL_ID_BYTES + 1)
        ev = _host_event(
            "dev-a",
            self.TS,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {**_usage(5), "by_model": {model: _usage(5)}},
                }
            },
        )
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "unsupported_schema"

    def test_too_many_models_per_day_drops_detail(self):
        models = {f"gpt-{i}": _usage(0) for i in range(aggregator.MAX_HOST_MODELS_PER_DAY + 1)}
        models["gpt-0"] = _usage(5)
        ev = _host_event(
            "dev-a",
            self.TS,
            extra={"tokens_by_day": {"2026-04-20": {**_usage(5), "by_model": models}}},
        )
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "unsupported_schema"

    def test_too_many_models_per_row_drops_detail(self):
        days = ("2026-04-18", "2026-04-19", "2026-04-20")
        hosts = {"codex": {day: _usage(1) for day in days}}
        sibling: dict = {}
        idx = 0
        for day in days:
            models = {}
            for _ in range(aggregator.MAX_HOST_MODELS_PER_DAY):
                models[f"m{idx}"] = _usage(0)
                idx += 1
            first = next(iter(models))
            models[first] = _usage(1)
            sibling[day] = {**_usage(1), "by_model": models}
        ev = _host_event("dev-a", self.TS, hosts=hosts, extra={"tokens_by_day": sibling})
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "unsupported_schema"

    def test_reconciliation_overflow_drops_detail(self):
        almost = (2**53) - 1
        hosts = {
            "codex": {
                "2026-04-20": {
                    "input": almost,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                }
            },
            "grok": {
                "2026-04-20": {
                    "input": almost,
                    "cache_create": 0,
                    "cache_read": 0,
                    "output": 0,
                }
            },
        }
        ev = _host_event("dev-a", self.TS, token_sources=("codex", "grok"), hosts=hosts)
        ev["tokens_by_day"] = {
            "2026-04-20": {
                "input": almost,
                "cache_create": 0,
                "cache_read": 0,
                "output": 0,
                "by_model": {
                    "gpt-5": {
                        "input": almost,
                        "cache_create": 0,
                        "cache_read": 0,
                        "output": 0,
                    }
                },
            }
        }
        row = aggregator._accept_host_usage_snapshot(ev)
        assert isinstance(row, aggregator._AcceptedHostRow)
        assert row.detail == "absent"
        assert row.detail_reason == "invalid_counter"

    def test_equal_ts_quality_rank_prefers_valid_in_both_orders(self):
        ts = "2026-04-27T12:00:00+00:00"
        hosts = {"codex": {"2026-04-20": _usage(5)}}
        absent = _host_event("dev-a", ts, hosts=hosts)
        valid = _host_event("dev-a", ts, hosts=hosts, extra={"tokens_by_day": _sibling(hosts)})
        invalid = _host_event("dev-a", ts, hosts=hosts, extra={"tokens_by_day": "nope"})
        assert self._agg([absent, valid]).by_device["dev-a"].detail == "present"
        assert self._agg([valid, absent]).by_device["dev-a"].detail == "present"
        assert self._agg([invalid, absent]).by_device["dev-a"].detail == "absent"
        assert self._agg([invalid, absent]).by_device["dev-a"].detail_reason is None
        assert self._agg([absent, invalid]).by_device["dev-a"].detail_reason is None

    def test_oversized_by_model_is_rejected_without_copying_values(self, monkeypatch):
        """Bound before you copy.

        Copying a peer-chosen map and rejecting it one line later does the
        attacker's allocation for them. Patching the value copier to raise
        proves the cardinality check runs FIRST.

        Calls ``_accept_tokens_by_day`` DIRECTLY rather than going through a
        whole event: ``_copy_usage_bucket`` is shared with
        ``_accept_hosts_payload``, so a module-level patch would trip on the
        family payload and prove nothing about the sibling path.
        """

        def boom(_bucket):
            raise AssertionError("cardinality must be checked before copying values")

        monkeypatch.setattr(aggregator, "_copy_usage_bucket", boom)
        models = {f"gpt-{i}": _usage(0) for i in range(aggregator.MAX_HOST_MODELS_PER_DAY + 1)}
        copied, reason = aggregator._accept_tokens_by_day(
            {"2026-04-20": {**_usage(5), "by_model": models}},
            {"codex": {"2026-04-20": _usage(5)}},
            ("2026-04-20",),
        )
        assert copied == {}
        assert reason == "unsupported_schema"

    def test_bad_model_id_is_rejected_before_its_bucket_is_copied(self, monkeypatch):
        """Per-KEY validation also precedes the per-value copy."""

        def boom(_bucket):
            raise AssertionError("model id must be validated before copying its bucket")

        monkeypatch.setattr(aggregator, "_copy_usage_bucket", boom)
        copied, reason = aggregator._accept_tokens_by_day(
            {"2026-04-20": {**_usage(5), "by_model": {"": _usage(5)}}},
            {"codex": {"2026-04-20": _usage(5)}},
            ("2026-04-20",),
        )
        assert copied == {}
        assert reason == "unsupported_schema"

    def test_day_count_mismatch_short_circuits_before_the_day_loop(self, monkeypatch):
        """The outer loop is bounded the same way the inner one is.

        `active_days` is already capped at MAX_BY_DAY_DAYS by the `hosts`
        acceptor, and the two day sets must match exactly, so unequal sizes
        can never reconcile. Reject on the count and the loop never runs over
        a peer-chosen number of days.
        """

        def boom(_bucket):
            raise AssertionError("day count must be checked before the loop")

        monkeypatch.setattr(aggregator, "_copy_day_bucket", boom)
        hosts = {"codex": {"2026-04-20": _usage(5)}}
        sibling = {
            f"2026-04-{d:02d}": {**_usage(1), "by_model": {"gpt-5": _usage(1)}}
            for d in range(1, 20)
        }
        ev = _host_event("dev-a", self.TS, hosts=hosts, extra={"tokens_by_day": sibling})
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "active_days_mismatch"
        assert row.lifetime_by_family["codex"], "the row itself still survives"

    def test_partial_by_model_is_accepted_because_the_writer_caps_it(self):
        """``by_model`` reconciles with ``<=``, not ``==``.

        The writer truncates the breakdown at the protocol caps and leaves the
        day totals whole, so a capped machine's sibling has an unattributed
        residual BY DESIGN. An equality check here would drop exactly the rows
        the writer-side cap exists to make deliverable.
        """
        hosts = {"codex": {"2026-04-20": _usage(10)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {**_usage(10), "by_model": {"gpt-5": _usage(4)}},
                }
            },
        )
        row = _accepted(ev)
        assert row.detail == "present"
        assert row.tokens_by_day["2026-04-20"]["input"] == 10
        assert row.tokens_by_day["2026-04-20"]["by_model"]["gpt-5"]["input"] == 4

    def test_by_model_over_claiming_the_day_total_drops_detail(self):
        """The bound that matters: per-model was otherwise unlimited.

        Without it a peer attributes 2**53 tokens to one model on a day whose
        whole family total is 5, the row still reconciles at the day level,
        and the number flows straight into Group 35 pricing.
        """
        hosts = {"codex": {"2026-04-20": _usage(5)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {**_usage(5), "by_model": {"gpt-5": _usage(2**53)}},
                }
            },
        )
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "invalid_counter"

    def test_by_model_over_claim_across_two_models_drops_detail(self):
        """Each model is legal alone; their SUM is not."""
        hosts = {"codex": {"2026-04-20": _usage(6)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {
                        **_usage(6),
                        "by_model": {"gpt-5": _usage(4), "gpt-4": _usage(4)},
                    }
                }
            },
        )
        row = _accepted(ev)
        assert row.detail == "absent"
        assert row.detail_reason == "invalid_counter"

    def test_dump_disambiguates_model_ids_that_sanitize_alike(self):
        """A dict-key collision would silently drop a model from the FORENSIC
        dump — the one surface whose job is showing what actually arrived.

        ``_safe_short`` truncates at 128 chars while the acceptor admits 256
        bytes, so two distinct accepted ids can share a sanitized key."""
        stem = "g" * aggregator._SHORT_LEN_CAP
        hosts = {"codex": {"2026-04-20": _usage(3)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {
                        **_usage(3),
                        "by_model": {f"{stem}-one": _usage(1), f"{stem}-two": _usage(2)},
                    }
                }
            },
        )
        row = _accepted(ev)
        assert row.detail == "present"
        dumped = json.loads(aggregator._dump_host_inventory(self._agg([ev])))
        models = dumped["by_device"]["dev-a"]["tokens_by_day"]["2026-04-20"]["by_model"]
        assert len(models) == 2, "a collision must not swallow a model"
        assert sorted(u["input"] for u in models.values()) == [1, 2]
        assert f"{stem}~2" in models

    def test_dump_aliases_are_stable_across_days(self):
        """Alias assignment is row-global, not per-day.

        Per-day assignment off insertion order lets `a/b` own `a_b` on Monday
        and `a?b` own it on Tuesday, so a reader comparing days compares two
        different models and sees per-day movement that never happened —
        worse than the collision it replaced. Found by Codex adversarial
        review.
        """
        raw = {
            "2026-04-20": {**_usage(3), "by_model": {"a/b": _usage(1), "a?b": _usage(2)}},
            "2026-04-21": {**_usage(3), "by_model": {"a?b": _usage(2), "a/b": _usage(1)}},
        }
        out = aggregator._sanitize_tokens_by_day(raw)
        day1 = out["2026-04-20"]["by_model"]
        day2 = out["2026-04-21"]["by_model"]
        assert set(day1) == set(day2) == {"a_b", "a_b~2"}
        # Same alias, same underlying model, both days.
        assert day1["a_b"] == day2["a_b"]
        assert day1["a_b~2"] == day2["a_b~2"]

    def test_selection_is_total_across_differing_valid_siblings(self):
        """`tie_key` excludes the sibling and `_detail_rank` only grades
        present/absent/invalid, so two rows with DIFFERENT valid siblings
        compared equal all the way down and file order picked the winner.
        Deterministic selection has to be total. Found by Codex."""
        ts = "2026-04-27T12:00:00+00:00"
        hosts = {"codex": {"2026-04-20": _usage(5)}}

        def row(model: str) -> dict:
            return _host_event(
                "dev-a",
                ts,
                hosts=hosts,
                extra={
                    "tokens_by_day": {"2026-04-20": {**_usage(5), "by_model": {model: _usage(5)}}}
                },
            )

        a_row, b_row = row("gpt-a"), row("gpt-b")
        assert _accepted(a_row).tie_key == _accepted(b_row).tie_key
        forward = self._agg([a_row, b_row]).by_device["dev-a"]
        reverse = self._agg([b_row, a_row]).by_device["dev-a"]
        assert forward.tokens_by_day == reverse.tokens_by_day

    def test_selection_is_total_across_differing_invalid_siblings(self):
        """Two INVALID siblings both collapse to None + rank 0, so keying on
        the payload alone left them tied — and `detail_reason` is what the dump
        renders as the user's remedy. File order must not decide whether a peer
        is told `invalid_counter` or `active_days_mismatch`. Found by Codex
        structured review."""
        ts = "2026-04-27T12:00:00+00:00"
        hosts = {"codex": {"2026-04-20": _usage(5)}}
        bad_schema = _host_event("dev-a", ts, hosts=hosts, extra={"tokens_by_day": "nope"})
        bad_days = _host_event(
            "dev-a",
            ts,
            hosts=hosts,
            extra={"tokens_by_day": {"2026-04-21": {**_usage(5), "by_model": {"m": _usage(5)}}}},
        )
        forward = self._agg([bad_schema, bad_days]).by_device["dev-a"]
        reverse = self._agg([bad_days, bad_schema]).by_device["dev-a"]
        assert forward.detail_reason == reverse.detail_reason
        assert forward.detail == reverse.detail == "absent"

    def test_writer_capped_row_round_trips_into_the_dump(self):
        """End-to-end for the cap: >32 models in one day still ships."""
        from mind_meld import events

        n = events.MAX_HOST_MODELS_PER_DAY + 4
        by_model = {f"m{i:03d}": _usage(i + 1) for i in range(n)}
        total = sum(i + 1 for i in range(n))
        row = events.make_host_usage_snapshot(
            device="dev-a",
            token_sources=("codex",),
            hosts={"codex": {"2026-04-20": _usage(total)}},
            tokens_by_day={"2026-04-20": {**_usage(total), "by_model": by_model}},
            ts=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
        )
        accepted = aggregator._accept_host_usage_snapshot(row)
        assert isinstance(accepted, aggregator._AcceptedHostRow)
        assert accepted.detail == "present", accepted.detail_reason
        day = accepted.tokens_by_day["2026-04-20"]
        assert len(day["by_model"]) == events.MAX_HOST_MODELS_PER_DAY
        assert sum(u["input"] for u in day["by_model"].values()) < day["input"]

    def test_quality_rank_cannot_change_a_rendered_number(self):
        """The rank sits strictly BELOW ``tie_key``, and that is load-bearing.

        ``_tie_break_key`` projects ``hosts`` and ``active_days`` verbatim, so
        two rows only reach ``_detail_rank`` when their family totals are
        already identical. The rank therefore picks which SIBLING survives and
        can never move a ``## Agent activity`` number — which is why a
        v0.12.48 and a v0.12.49 Mac render the same card from one corpus.
        Hoist the rank above ``tie_key`` and this fails.
        """
        ts = "2026-04-27T12:00:00+00:00"
        hosts = {"codex": {"2026-04-20": _usage(5)}}
        absent = _host_event("dev-a", ts, hosts=hosts)
        valid = _host_event("dev-a", ts, hosts=hosts, extra={"tokens_by_day": _sibling(hosts)})
        assert _accepted(absent).tie_key == _accepted(valid).tie_key
        forward = self._agg([absent, valid]).by_device["dev-a"]
        reverse = self._agg([valid, absent]).by_device["dev-a"]
        assert forward.lifetime_by_family == reverse.lifetime_by_family == hosts
        assert forward.detail == reverse.detail == "present"

    def test_zero_only_day_survives_acceptor(self):
        hosts = {"codex": {"2026-04-20": _usage(0)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={"tokens_by_day": _sibling(hosts)},
        )
        row = _accepted(ev)
        assert row.detail == "present"
        assert row.tokens_by_day["2026-04-20"]["input"] == 0

    def test_round_trip_writer_to_dump(self, tmp_path):
        from mind_meld import events

        hosts = {
            "codex": {"2026-04-20": _usage(4)},
            "grok": {"2026-04-20": _usage(3)},
        }
        sibling = {
            "2026-04-20": {
                **_usage(7),
                "by_model": {"gpt-5": _usage(4), "grok-4": _usage(3)},
            }
        }
        row = events.make_host_usage_snapshot(
            device="dev-a",
            token_sources=("codex", "grok"),
            hosts=hosts,
            tokens_by_day=sibling,
            ts=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
        )
        events_dir = tmp_path / "events"
        events.write_push_event(events_dir, "dev-a", [row])
        raw = json.loads(next(events_dir.glob("*.jsonl")).read_text().strip())
        inv = aggregator.aggregate_host_usage(
            [raw], since=self.SINCE, until=self.UNTIL, registered_ids=None
        )
        dumped = json.loads(aggregator._dump_host_inventory(inv))
        snap = dumped["by_device"]["dev-a"]
        assert snap["detail"] == "present"
        assert snap["tokens_by_day"]["2026-04-20"]["by_model"]["gpt-5"]["input"] == 4
        assert "per-model host tokens present" in snap["detail_phrase"]

    def test_dump_sanitizes_hostile_model_ids(self):
        hosts = {"codex": {"2026-04-20": _usage(5)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": {
                    "2026-04-20": {
                        **_usage(5),
                        "by_model": {"gpt-5\n|hack": _usage(5)},
                    }
                }
            },
        )
        inv = self._agg([ev])
        dumped = json.loads(aggregator._dump_host_inventory(inv))
        models = dumped["by_device"]["dev-a"]["tokens_by_day"]["2026-04-20"]["by_model"]
        assert "gpt-5\n|hack" not in models
        json.dumps(dumped)

    def test_detail_phrase_names_a_fix_command(self):
        phrase = aggregator._host_detail_phrase("absent", "unsupported_schema")
        assert "unsupported_schema" in phrase
        assert "mm push" in phrase

    def test_every_detail_phrase_branch_names_a_remedy(self):
        """All seven branches, not just the one.

        These strings ARE the diagnostic interface for a dropped sibling —
        a branch that reaches the user without a fix clause is the
        "absence as the diagnostic interface" failure this subsystem keeps
        legislating against. Cheap to pin, invisible when it rots.
        """
        assert aggregator._host_detail_phrase("present", None) == "per-model host tokens present"

        absent = aggregator._host_detail_phrase("absent", None)
        assert "v0.12.49" in absent
        assert "mm push" in absent

        for reason in (
            "active_days_mismatch",
            "invalid_counter",
            "unsupported_schema",
            "invalid_day",
        ):
            phrase = aggregator._host_detail_phrase("absent", reason)
            assert reason in phrase, reason
            assert "mm push" in phrase, reason

        # Unknown reason falls through to the generic branch, still with a fix.
        fallback = aggregator._host_detail_phrase("absent", "some_future_reason")
        assert "some_future_reason" in fallback
        assert "mm push" in fallback
        assert "mm diag" in fallback

    @pytest.mark.parametrize(
        "reason",
        [
            None,
            "active_days_mismatch",
            "invalid_counter",
            "unsupported_schema",
            "invalid_day",
            "some_future_reason",
        ],
    )
    def test_every_dropped_detail_phrase_carries_the_attended_floor(self, reason):
        """The bare "mm push" check above passes for a phrase with no floor at all."""
        phrase = aggregator._host_detail_phrase("absent", reason)
        assert aggregator._attended_usage_remedy() in phrase
        assert "mm diag" in phrase and "capture-usage" not in phrase

    @pytest.mark.parametrize("device", [None, "dev-a"])
    def test_legacy_counters_phrase_names_the_floor_with_or_without_a_device(self, device):
        phrase = aggregator._host_detail_phrase("absent", "legacy_counters", device=device)
        assert "older format" in phrase
        assert aggregator._attended_usage_remedy() in phrase
        assert "then re-run `mm retro-fleet`" in phrase
        assert ("`dev-a`" in phrase) == (device is not None)
        assert "capture-usage" not in phrase

    def test_wire_row_fixture_measurement(self):
        """Deterministic size of a 90-day 4-family 2-model-per-day row."""
        import gzip

        from mind_meld import events

        # Unique ISO dates: use January-April. 90 == MAX_BY_DAY_DAYS, so the
        # writer's cap does not trim this row and the measurement is taken at
        # the largest shape the wire can carry.
        days: list[str] = []
        for month in (1, 2, 3, 4):
            for d in range(1, 29):
                days.append(f"2026-{month:02d}-{d:02d}")
                if len(days) == 90:
                    break
            if len(days) == 90:
                break
        hosts = {
            family: {day: _usage(1) for day in days}
            for family in ("claude", "codex", "grok", "other")
        }
        sibling = {
            day: {
                **_usage(4),
                "by_model": {"gpt-5": _usage(2), "grok-4": _usage(2)},
            }
            for day in days
        }
        row = events.make_host_usage_snapshot(
            device="dev-a",
            token_sources=("codex", "grok", "opencode"),
            hosts=hosts,
            tokens_by_day=sibling,
            ts=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
        )
        payload = json.dumps(row, separators=(",", ":")).encode("utf-8")
        gz = gzip.compress(payload)
        envelope = len(gz) + 12 + 16  # AES-GCM nonce + tag
        assert len(payload) < 80_000
        assert len(gz) < 20_000
        assert envelope < 21_000


def _snap(
    device: str,
    as_of: datetime,
    *,
    families: dict | None = None,
    consulted: tuple[str, ...] = ("codex",),
    since: datetime | None = None,
    until: datetime | None = None,
    degraded: tuple[str, ...] = (),
    partial: tuple[str, ...] = (),
    tokens_by_day: dict | None = None,
    counter_semantics: str | None = "disjoint-v1",
) -> aggregator.HostDeviceSnapshot:
    """Build a HostDeviceSnapshot directly, so rhythm tests never touch a clock."""
    ref_since = since or datetime(2026, 4, 21, tzinfo=timezone.utc)
    ref_until = until or datetime(2026, 4, 28, 12, tzinfo=timezone.utc)
    return aggregator.HostDeviceSnapshot(
        device=device,
        as_of=as_of,
        consulted=consulted,
        lifetime_by_family={} if families is None else families,
        stale=as_of < ref_since,
        future_dated=as_of > ref_until,
        degraded=degraded,
        partial=partial,
        tokens_by_day=tokens_by_day,
        counter_semantics=counter_semantics,
        detail="present" if tokens_by_day is not None else "absent",
    )


class TestAgentRhythmView:
    SINCE = datetime(2026, 4, 21, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)

    def _view(self, snaps, *, machines_known=3, missing=frozenset(), rejected=()):
        inv = aggregator.HostUsageInventory(
            by_device={s.device: s for s in snaps},
            devices_without_accepted_row=missing,
            rejected=rejected,
        )
        return aggregator._agent_rhythm_view(
            inv, since=self.SINCE, until=self.UNTIL, machines_known=machines_known
        )

    def test_family_label_set_matches_the_canonical_families(self):
        """One registry now, not two. The pre-1.1 code carried MODEL_FAMILY_ROWS
        and AGENT_FAMILY_ROWS with deliberately different labels so two adjacent
        card blocks could not be confused; with a single AGENTS block that
        distinction has no referent, and a second label set is just drift
        waiting to happen."""
        keys = {key for key, _ in aggregator.AGENT_ROW_ORDER}
        assert keys == set(aggregator._HOST_FAMILIES)
        assert set(aggregator._HOST_FAMILIES) == set(get_args(host_usage.HostFamily))

    def test_counts_distinct_in_window_days_in_canonical_order(self):
        view = self._view(
            [
                _snap(
                    "dev-a",
                    self.UNTIL,
                    families={
                        "grok": {"2026-04-22": _usage(3)},
                        "codex": {"2026-04-22": _usage(1), "2026-04-24": _usage(1)},
                    },
                )
            ]
        )
        assert view.rows == (("Codex", 2), ("Grok", 1))
        assert view.machines_with_activity == 1
        assert view.any_activity is True

    def test_cross_machine_union_is_idempotent_under_duplicate_corpora(self):
        """Day-set union is idempotent under a duplicated corpus, which is why
        the Days column stays trustworthy even when a migrated home directory
        doubles the token column. The tokens are guarded separately, by
        ``_detect_duplicate_ledgers``."""
        day = {"codex": {"2026-04-22": _usage(7)}}
        one = self._view([_snap("dev-a", self.UNTIL, families=day)])
        two = self._view(
            [
                _snap("dev-a", self.UNTIL, families=day),
                _snap("dev-b", self.UNTIL, families=dict(day)),
            ]
        )
        assert one.rows == (("Codex", 1),)
        assert two.rows == (("Codex", 1),)
        assert two.machines_with_activity == 2

    def test_all_zero_bucket_is_omitted_not_rendered_as_zero(self):
        """Zero is a valid counter and the writer does not drop all-zero buckets,
        so key presence is not activity."""
        zero = {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}
        view = self._view([_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": zero}})])
        assert view.rows == ()
        assert view.machines_with_activity == 0
        assert view.any_activity is False

    def test_window_edges_are_inclusive(self):
        lo = self.SINCE.date().isoformat()
        hi = self.UNTIL.date().isoformat()
        for day in (lo, hi):
            view = self._view([_snap("dev-a", self.UNTIL, families={"codex": {day: _usage(1)}})])
            assert view.rows == (("Codex", 1),), day

    def test_days_outside_the_window_do_not_count(self):
        for day in ("2026-04-20", "2026-04-29"):
            view = self._view([_snap("dev-a", self.UNTIL, families={"codex": {day: _usage(1)}})])
            assert view.rows == (), day

    def test_stale_snapshot_with_in_window_day_contributes_nothing(self):
        """A backdated peer can ship as_of well before the window WITH in-window
        day keys: the acceptor validates day-key FORMAT and `ts` independently
        and never relates them. Verified constructible. The clamp to
        min(until, as_of) makes the property true by arithmetic."""
        stale = _snap(
            "dev-a",
            datetime(2026, 3, 1, tzinfo=timezone.utc),
            families={"codex": {"2026-04-22": _usage(500)}},
        )
        assert stale.stale is True
        view = self._view([stale])
        assert view.rows == ()
        assert view.machines_with_activity == 0

    def test_day_keys_after_as_of_are_clamped(self):
        snap = _snap(
            "dev-a",
            datetime(2026, 4, 23, tzinfo=timezone.utc),
            families={"codex": {"2026-04-22": _usage(1), "2026-04-26": _usage(1)}},
        )
        view = self._view([snap])
        assert view.rows == (("Codex", 1),)

    def test_resumed_session_collapse_makes_the_count_drop(self):
        """The residual imprecision, pinned. Resuming a session moves its whole
        total onto a new last-touch day and ERASES the old key, so the same
        window can report fewer days later. One-directional (only understates),
        which is why the copy says "seen on N days" rather than asserting a
        count."""
        before = self._view(
            [
                _snap(
                    "dev-a",
                    self.UNTIL,
                    families={
                        "codex": {
                            "2026-04-22": _usage(10),
                            "2026-04-23": _usage(10),
                            "2026-04-24": _usage(10),
                        }
                    },
                )
            ]
        )
        # Same real work, all three sessions resumed on the 24th.
        after = self._view(
            [_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-24": _usage(30)}})]
        )
        assert before.rows == (("Codex", 3),)
        assert after.rows == (("Codex", 1),)

    def test_unknown_family_on_the_wire_is_ignored(self):
        view = self._view(
            [_snap("dev-a", self.UNTIL, families={"gemini": {"2026-04-22": _usage(9)}})]
        )
        assert view.rows == ()

    def test_non_inventory_input_yields_no_activity(self):
        for bad in (None, {}, "nope", 42):
            view = aggregator._agent_rhythm_view(
                bad, since=self.SINCE, until=self.UNTIL, machines_known=3
            )
            assert view.rows == ()
            assert view.any_activity is False

    def test_view_carries_no_magnitude(self):
        """The rhythm view is a day-count union and nothing else. Since 1.1 it
        no longer reaches the card — magnitude lives on ``FleetAgentRow`` — but
        keeping it magnitude-free preserves its one job: answering "was there
        any agent activity" for the coverage diagnostics, idempotently under a
        duplicated corpus."""
        view = self._view(
            [_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": _usage(999_999)}})]
        )
        assert view.rows == (("Codex", 1),)
        assert set(view.__dataclass_fields__) == {
            "rows",
            "machines_with_activity",
            "machines_known",
            "snapshots_accepted",
        }
        assert all(isinstance(days, int) for _label, days in view.rows)
        assert 999_999 not in [days for _label, days in view.rows]


class TestAgentsCardBlock:
    """The card's unified AGENTS block. Successor to the split MODELS /
    AGENT LOGS pair, which reported two units from two sources and is the
    reason the card read Claude-first."""

    def _usage(self, rows, *, machines_known=3, with_activity=1, accepted=1):
        return aggregator.FleetAgentUsage(
            rows=tuple(rows),
            machines_with_activity=with_activity,
            machines_known=machines_known,
            snapshots_accepted=accepted,
        )

    def _row(self, key, label, tokens, days, *, machines=1, by_model=None, known=True, causes=()):
        return aggregator.FleetAgentRow(
            key=key,
            label=label,
            by_model=by_model or {},
            tokens=tokens,
            active_days=days,
            machines=machines,
            counters_known=known,
            floor_causes=tuple(causes),
        )

    def test_every_agent_gets_the_same_row_shape(self):
        rows = [
            self._row("claude", "Claude", 8_000_000_000, 29),
            self._row("codex", "Codex", 3_300_000_000, 28),
            self._row("grok", "Grok", 1_000_000_000, 11),
        ]
        lines = aggregator._render_agents_card_block(self._usage(rows))
        body = lines[1:]
        assert len(body) == 3
        for line in body:
            assert "tokens ·" in line and "days" in line

    def test_empty_but_covered_keeps_the_provenance_count(self):
        lines = aggregator._render_agents_card_block(self._usage([], with_activity=0))
        assert "AGENTS (0 of 3 machines)" in lines[0]
        assert "No agent usage observed this window" in lines[1]

    def test_registry_unavailable_drops_the_denominator(self):
        lines = aggregator._render_agents_card_block(
            self._usage([self._row("codex", "Codex", 5, 5)], machines_known=None)
        )
        assert "1 machine" in lines[0]
        assert "None" not in "".join(lines)

    def test_singular_grammar(self):
        lines = aggregator._render_agents_card_block(
            self._usage([self._row("codex", "Codex", 5, 1)], machines_known=1)
        )
        assert "1 day" in lines[1] and "1 days" not in lines[1]
        assert "1 of 1 machines" in lines[0]

    def test_legacy_counters_render_unavailable_not_a_number(self):
        """Inclusive counters run up to ~2x high. That is the one caveat that
        points the wrong way, so it stays a rendered `—`, never a health note."""
        lines = aggregator._render_agents_card_block(
            self._usage([self._row("codex", "Codex", 9_000, 3, known=False)])
        )
        assert "—" in lines[1]
        assert "9" not in lines[1].replace("9 ", "")

    def test_every_state_holds_the_card_width(self):
        states = [
            self._usage([], with_activity=0),
            self._usage([self._row("codex", "Codex", 5_000_000_000, 12)]),
            self._usage([self._row("codex", "Codex", 5, 5)], machines_known=None),
            self._usage(
                [
                    self._row(k, lbl, 999_000_000_000, 120, machines=9)
                    for k, lbl in aggregator.AGENT_ROW_ORDER
                ]
            ),
        ]
        for usage in states:
            for line in aggregator._render_agents_card_block(usage):
                assert len(line) == aggregator.CARD_WIDTH
                assert "…" not in line, f"content lost to truncation: {line}"

    def test_one_agent_per_line_never_truncates(self):
        """Four agents at three-digit day counts still fit; a joined line would
        blow the 58-char budget and `_card_line` would silently eat a metric."""
        usage = self._usage(
            [self._row(k, lbl, 123_400_000_000, 365) for k, lbl in aggregator.AGENT_ROW_ORDER]
        )
        lines = aggregator._render_agents_card_block(usage)
        assert len(lines) == 1 + len(aggregator.AGENT_ROW_ORDER)
        for _k, label in aggregator.AGENT_ROW_ORDER:
            assert any(label in line for line in lines), label


class TestAgentCoverageNotes:
    SINCE = datetime(2026, 4, 21, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)

    def _data(self, snaps, *, missing=frozenset(), rejected=(), devices_known=3):
        data = aggregator.RetroData(window_days=7, since=self.SINCE, until=self.UNTIL)
        data.fleet = aggregator.FleetState(devices_known=devices_known)
        data.host_inventory = aggregator.HostUsageInventory(
            by_device={s.device: s for s in snaps},
            devices_without_accepted_row=missing,
            rejected=rejected,
        )
        return data

    def test_no_reader_contribution_names_the_ambiguous_state_and_next_step(self):
        """An empty token_sources list means no reader contributed, not that no
        source is enabled: absent-ledger readers are deliberately omitted too."""
        notes = aggregator._agent_coverage_notes(
            self._data([_snap("dev-a", self.UNTIL, consulted=())])
        )
        assert any(
            "No agent-log reader contributed" in n
            and "If no source is enabled" in n
            and "mm enable-source codex" in n
            and "no attributable local ledger" in n
            and "opencode" not in n
            for n in notes
        )

    def test_configured_fleet_is_never_nagged(self):
        notes = aggregator._agent_coverage_notes(
            self._data([_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": _usage(1)}})])
        )
        assert not any("enable-source" in n for n in notes)

    def test_no_activity_names_the_lower_bound(self):
        notes = aggregator._agent_coverage_notes(self._data([_snap("dev-a", self.UNTIL)]))
        assert any("lower bounds" in n for n in notes)

    def test_all_stale_says_so(self):
        notes = aggregator._agent_coverage_notes(
            self._data([_snap("dev-a", datetime(2026, 3, 1, tzinfo=timezone.utc))])
        )
        assert any("predate this window" in n for n in notes)

    def test_missing_snapshot_is_unknown_not_zero(self):
        notes = aggregator._agent_coverage_notes(
            self._data(
                [_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": _usage(1)}})],
                missing=frozenset({"dev-z"}),
            )
        )
        assert any("unknown, not zero" in n for n in notes)

    def test_rejected_counts_devices_not_rows(self):
        """`aggregate_host_usage` applies no window filter to rejects, so a
        row-count breadcrumb from one broken writer would stay lit for the whole
        90-day retention."""
        rejected = tuple(
            aggregator.HostReject(device="dev-bad", reason="unsupported_schema") for _ in range(40)
        )
        data = self._data([_snap("dev-a", self.UNTIL)], rejected=rejected)
        assert data.host_inventory.rejected_rows == 40
        assert data.host_inventory.rejected_devices == 1
        notes = aggregator._agent_coverage_notes(data)
        assert any("from 1 machine(s) were rejected" in n for n in notes)

    def test_rejected_breadcrumb_fires_even_with_no_card_block(self):
        data = self._data(
            [], rejected=(aggregator.HostReject(device="dev-bad", reason="invalid_day"),)
        )
        notes = aggregator._agent_coverage_notes(data)
        assert any("were rejected" in n for n in notes)

    def test_f1_one_degraded_machine_names_machine_reader_and_diag(self):
        notes = aggregator._agent_coverage_notes(
            self._data(
                [
                    _snap(
                        "laptop",
                        self.UNTIL,
                        families={"codex": {"2026-04-22": _usage(1)}},
                        consulted=("codex",),
                        degraded=("grok",),
                    )
                ]
            )
        )
        hit = [n for n in notes if "failed on the latest push" in n]
        assert len(hit) == 1
        assert "laptop" in hit[0]
        assert "grok" in hit[0]
        assert "mm diag" in hit[0]
        assert "host_usage.grok" in hit[0]
        assert "mm push" not in hit[0]

    def test_f1b_one_partial_machine_names_machine_reader_and_diag(self):
        notes = aggregator._agent_coverage_notes(
            self._data(
                [
                    _snap(
                        "laptop",
                        self.UNTIL,
                        families={"grok": {"2026-04-22": _usage(1)}},
                        consulted=("grok",),
                        partial=("grok",),
                    )
                ]
            )
        )
        hit = [n for n in notes if "are incomplete" in n]
        assert len(hit) == 1
        assert "laptop" in hit[0]
        assert "grok" in hit[0]
        assert "mm diag" in hit[0]
        assert "host_usage.grok" in hit[0]
        assert "mm push" not in hit[0]

    def test_f2_n_degraded_machines_is_one_aggregated_note(self):
        notes = aggregator._agent_coverage_notes(
            self._data(
                [
                    _snap("laptop", self.UNTIL, degraded=("grok",)),
                    _snap("desktop", self.UNTIL, degraded=("grok",)),
                ]
            )
        )
        hits = [n for n in notes if "failed on the latest push" in n]
        assert len(hits) == 1
        assert "laptop" in hits[0]
        assert "desktop" in hits[0]

    def test_f3_zero_degraded_emits_no_coverage_note(self):
        notes = aggregator._agent_coverage_notes(
            self._data([_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": _usage(1)}})])
        )
        assert not any("failed on the latest push" in n for n in notes)
        assert not any("are incomplete" in n for n in notes)

    def test_f4_note_uses_safe_short_not_raw_peer_bytes(self):
        nasty = "dev\x1b[31m-evil"
        notes = aggregator._agent_coverage_notes(
            self._data([_snap(nasty, self.UNTIL, degraded=("grok",))])
        )
        joined = " ".join(notes)
        assert "\x1b" not in joined
        assert "[31m" not in joined

    def test_f5_absent_partial_sources_does_not_nag_peer_too_old(self):
        notes = aggregator._agent_coverage_notes(
            self._data([_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": _usage(1)}})])
        )
        joined = " ".join(notes)
        assert "too old" not in joined
        assert "partial_sources" not in joined

    def test_every_health_code_is_documented_in_skill_md(self):
        """The 1.1 doc-sync contract.

        Pre-1.1 this pinned a closed vocabulary of rendered Notes PROSE, which
        meant every copy edit was a doc change and the skill had to be told
        that an unlisted line "is reported verbatim and never interpreted".
        Codes are a real interface: stable, greppable, and safe to reword.
        """
        skill = Path(__file__).resolve().parents[1] / "src/mind_meld/skills/retro_fleet/SKILL.md"
        text = skill.read_text(encoding="utf-8")
        emitted = {
            "fleet_incomplete",
            "registry_unavailable",
            "duplicate_ledger",
            "legacy_counters",
            "cost_floor",
            "cost_unavailable",
            "sessions_incomplete",
            "tokens_incomplete",
            "pricing_extrapolated",
            "skills_incomplete",
            "unpriced_models",
            "agent_coverage",
            "unregistered_devices",
            "discovery_errors",
            "git_budget",
            "git_gap",
            "zero_repo_capture",
            "parse_errors",
            "window_exceeds_retention",
            "fleet_changed",
        }
        for code in sorted(emitted):
            assert f"`{code}`" in text, code
        # The skill must still refuse to invent meaning for a code it does not
        # know — a stale installed copy is the normal case, not an edge case.
        assert "reported verbatim" in text and "never interpreted" in text
        assert "git_capture.recorded.walk_budget_aborts" not in text

    def test_every_emitted_health_code_is_in_the_documented_set(self):
        """The other direction: no code reaches output undocumented."""
        skill = Path(__file__).resolve().parents[1] / "src/mind_meld/skills/retro_fleet/SKILL.md"
        text = skill.read_text(encoding="utf-8")
        data = self._data([_snap("dev-a", self.UNTIL, consulted=())])
        data.skipped_per_source = {aggregator.SKIP_CATEGORY_EVENTS: 1}
        data.git.zero_repo_captures = {"dev-a": (1, 2)}
        data.pushes.discovery_errors = ["boom"]
        data.window_exceeds_retention = True
        for entry in _health(aggregator.format_retro(data)):
            assert f"`{entry['code']}`" in text, entry["code"]

    @pytest.mark.parametrize(
        "case,marker",
        [
            ("registry-unavailable", "No agent-log snapshots were accepted from any machine"),
            ("no-reader", "No agent-log reader contributed on any machine"),
            ("all-stale", "Agent-log snapshots all predate this window"),
            ("some-missing", "have no agent-log snapshot (unknown, not zero)"),
        ],
    )
    def test_every_refresh_remedy_names_the_attended_floor(self, case, marker):
        """Each quiet-block cause repairs with an upgrade to the attended floor.

        Below v0.14.17 a converged ``mm push`` succeeds without refreshing usage,
        so a remedy that says only "run mm push" sends a stale Mac in a circle.
        """
        active = {"codex": {"2026-04-22": _usage(1)}}
        if case == "registry-unavailable":
            data = self._data([])
        elif case == "no-reader":
            data = self._data([_snap("dev-a", self.UNTIL, consulted=())])
        elif case == "all-stale":
            data = self._data([_snap("dev-a", datetime(2026, 3, 1, tzinfo=timezone.utc))])
        else:
            data = self._data(
                [_snap("dev-a", self.UNTIL, families=active)], missing=frozenset({"dev-z"})
            )
        notes = [n for n in aggregator._agent_coverage_notes(data) if marker in n]
        assert len(notes) == 1, notes
        assert aggregator._attended_usage_remedy() in notes[0]
        assert aggregator.ATTENDED_USAGE_MIN_VERSION in notes[0]
        assert "capture-usage" not in notes[0]


class TestUnifiedAgents:
    """The 1.1 ``## Agents`` table and its aggregate.

    Successor to three sections that reported one fleet three ways:
    ``Claude Code activity`` (Claude-only tokens + a per-model cost table),
    ``Agent activity`` (per-machine host families), and ``API list-rate
    equivalent`` (per-machine dollars behind a do-not-sum warning).
    """

    SINCE = datetime(2026, 4, 21, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)

    def _data(self, snaps, *, known_ids=(), names=None, missing=frozenset()):
        data = aggregator.RetroData(window_days=7, since=self.SINCE, until=self.UNTIL)
        names = names or {}
        data.fleet = aggregator.FleetState(
            devices_in_events={s.device for s in snaps},
            devices_known=len(known_ids) or None,
            devices_known_list=[
                {"device_id": d, "device_name": names.get(d, d)} for d in known_ids
            ],
        )
        data.host_inventory = aggregator.HostUsageInventory(
            by_device={s.device: s for s in snaps},
            devices_without_accepted_row=missing,
        )
        return data

    def _usage_for(self, data):
        return aggregator.aggregate_agent_usage(data, machines_known=data.fleet.devices_known)

    def _row(self, usage, key):
        return next((r for r in usage.rows if r.key == key), None)

    # --- summation -------------------------------------------------------

    def test_host_tokens_sum_across_machines(self):
        """The 1.1 inversion. Pre-1.1 this was three per-machine rows under a
        'do not sum these values' heading, while Claude was summed fleet-wide
        under the identical hazard."""
        hosts = {"codex": {"2026-04-22": _usage(100)}}
        data = self._data(
            [
                _snap("dev-a", self.UNTIL, families=hosts),
                _snap("dev-b", self.UNTIL, families={"codex": {"2026-04-23": _usage(50)}}),
            ],
            known_ids=("dev-a", "dev-b"),
        )
        codex = self._row(self._usage_for(data), "codex")
        assert codex.tokens == 150
        assert codex.active_days == 2
        assert codex.machines == 2

    def test_claude_and_hosts_share_one_row_shape(self):
        data = self._data(
            [_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": _usage(7)}})],
            known_ids=("dev-a",),
        )
        data.sessions.tokens_by_model = {
            "claude-sonnet-5": {"input": 11, "cache_create": 0, "cache_read": 0, "output": 0}
        }
        data.sessions.active_days = {"2026-04-22", "2026-04-23"}
        data.sessions.token_devices = {"dev-a": self.UNTIL}
        usage = self._usage_for(data)
        claude, codex = self._row(usage, "claude"), self._row(usage, "codex")
        assert (claude.tokens, claude.active_days, claude.machines) == (11, 2, 1)
        assert (codex.tokens, codex.active_days, codex.machines) == (7, 1, 1)
        body = "\n".join(aggregator._render_agents_table(usage))
        assert "| Claude | 11 | 2 | 1 |" in body
        assert "| Codex | 7 | 1 | 1 |" in body

    @pytest.mark.parametrize("host_device, expected", [(None, 1), ("dev-a", 1), ("dev-b", 2)])
    def test_card_counts_union_of_claude_and_host_machines(self, host_device, expected):
        snaps = (
            [_snap(host_device, self.UNTIL, families={"codex": {"2026-04-22": _usage(7)}})]
            if host_device
            else []
        )
        data = self._data(snaps, known_ids=("dev-a", "dev-b"))
        data.sessions.tokens_by_model = {"claude-sonnet-5": _usage(11)}
        data.sessions.active_days = {"2026-04-22"}
        data.sessions.token_devices = {"dev-a": self.UNTIL}
        assert self._usage_for(data).machines_with_activity == expected
        assert f"AGENTS ({expected} of 2 machines)" in aggregator.format_retro(data, name="test")

    @pytest.mark.parametrize("semantics", [None, "disjoint-v1"])
    def test_cross_version_classification_keeps_volume_without_phantom_pricing(self, semantics):
        hosts = {"other": {"2026-04-22": _usage(1_000_000)}}
        ev = _host_event(
            "dev-a",
            self.UNTIL.isoformat(),
            hosts=hosts,
            extra={
                "counter_semantics": semantics,
                "tokens_by_day": _sibling(hosts, "gpt-6-astra"),
            },
        )
        data = self._data([], known_ids=("dev-a",))
        data.host_inventory = aggregator.aggregate_host_usage(
            [ev], since=self.SINCE, until=self.UNTIL, registered_ids={"dev-a"}
        )
        assert data.host_inventory.by_device["dev-a"].detail == "present"
        usage = self._usage_for(data)
        assert [row.key for row in usage.rows] == ["other"]
        row = usage.rows[0]
        assert (row.tokens, row.active_days, row.machines) == (1_000_000, 1, 1)
        assert not row.by_model
        assert any("different family classification" in cause for cause in row.floor_causes)
        assert aggregator._agent_row_cost(row)[0] is None
        body = "\n".join(aggregator._render_agents_table(usage))
        assert "| Codex |" not in body
        assert " | — | — |" in body

    def test_classification_mismatch_cannot_cancel_across_days(self):
        hosts = {
            "other": {"2026-04-22": _usage(10)},
            "codex": {"2026-04-23": _usage(10)},
        }
        snap = _snap("dev-a", self.UNTIL, families=hosts)
        snap.tokens_by_day = {
            "2026-04-22": {**_usage(10), "by_model": {"gpt-6-astra": _usage(10)}},
            "2026-04-23": {**_usage(10), "by_model": {"unknown-model": _usage(10)}},
        }
        usage = self._usage_for(self._data([snap], known_ids=("dev-a",)))
        assert all(not row.by_model for row in usage.rows)

    def test_capped_model_detail_still_prices_a_floor(self):
        hosts = {"codex": {"2026-04-22": _usage(1_000_000)}}
        snap = _snap("dev-a", self.UNTIL, families=hosts, counter_semantics="disjoint-v1")
        snap.tokens_by_day = {
            "2026-04-22": {**_usage(1_000_000), "by_model": {"gpt-6-astra": _usage(500_000)}}
        }
        row = self._row(self._usage_for(self._data([snap])), "codex")
        assert row.tokens == 1_000_000
        assert aggregator._agent_row_cost(row) == (5.0, True)

    def test_zero_only_model_maps_are_not_agent_activity(self):
        hosts = {"codex": {"2026-04-22": _usage(0)}}
        ev = _host_event(
            "dev-a",
            self.UNTIL.isoformat(),
            hosts=hosts,
            extra={
                "counter_semantics": "disjoint-v1",
                "tokens_by_day": _sibling(hosts, "gpt-6-astra"),
            },
        )
        data = self._data([], known_ids=("dev-a",))
        data.sessions.tokens_by_model = {"claude-sonnet-5": _usage(0)}
        data.host_inventory = aggregator.aggregate_host_usage(
            [ev], since=self.SINCE, until=self.UNTIL, registered_ids={"dev-a"}
        )
        assert data.host_inventory.by_device["dev-a"].detail == "present"
        out = aggregator.format_retro(data, name="test")
        assert "| Codex |" not in out
        assert "| Claude |" not in out
        assert "No agent usage observed" in out

    def test_stale_snapshot_contributes_nothing_to_the_window(self):
        data = self._data(
            [
                _snap(
                    "dev-old",
                    datetime(2026, 3, 1, tzinfo=timezone.utc),
                    families={"codex": {"2026-02-28": _usage(999)}},
                )
            ],
            known_ids=("dev-old",),
        )
        assert self._row(self._usage_for(data), "codex") is None

    # --- duplicate-ledger detector ---------------------------------------

    def test_identical_counter_tuples_across_devices_are_flagged(self):
        """A migrated home directory copies the ledger byte for byte, so the
        shared days match exactly. That is the signal the pre-1.1 docstring
        claimed did not exist."""
        day = {"codex": {"2026-04-22": _usage(7)}}
        data = self._data(
            [
                _snap("dev-a", self.UNTIL, families=day),
                _snap("dev-b", self.UNTIL, families={"codex": {"2026-04-22": _usage(7)}}),
            ],
            known_ids=("dev-a", "dev-b"),
        )
        assert self._usage_for(data).duplicate_ledger == ("codex",)

    def test_genuine_concurrent_use_is_not_flagged(self):
        """Two Macs used the same day produce different numbers. Verified
        against a real two-Mac fleet: 14 shared (family, day) pairs, zero
        identical tuples."""
        data = self._data(
            [
                _snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": _usage(7)}}),
                _snap("dev-b", self.UNTIL, families={"codex": {"2026-04-22": _usage(9)}}),
            ],
            known_ids=("dev-a", "dev-b"),
        )
        assert self._usage_for(data).duplicate_ledger == ()

    def test_duplicate_ledger_reaches_health_with_a_remedy(self):
        day = {"codex": {"2026-04-22": _usage(7)}}
        data = self._data(
            [
                _snap("dev-a", self.UNTIL, families=day),
                _snap("dev-b", self.UNTIL, families={"codex": {"2026-04-22": _usage(7)}}),
            ],
            known_ids=("dev-a", "dev-b"),
        )
        out = aggregator.format_retro(data)
        entry = _health_entry(out, "duplicate_ledger")
        assert entry is not None
        assert "double-counted" in entry["detail"]
        assert "mm devices" in entry["remedy"]

    def test_days_stay_idempotent_even_when_tokens_double(self):
        day = {"codex": {"2026-04-22": _usage(7)}}
        data = self._data(
            [
                _snap("dev-a", self.UNTIL, families=day),
                _snap("dev-b", self.UNTIL, families={"codex": {"2026-04-22": _usage(7)}}),
            ],
            known_ids=("dev-a", "dev-b"),
        )
        codex = self._row(self._usage_for(data), "codex")
        assert codex.active_days == 1
        assert codex.tokens == 14

    # --- markers and floors ----------------------------------------------

    def test_legacy_counters_render_unavailable_never_a_number(self):
        data = self._data(
            [
                _snap(
                    "dev-a",
                    self.UNTIL,
                    families={"codex": {"2026-04-22": _usage(10)}},
                    counter_semantics=None,
                )
            ],
            known_ids=("dev-a",),
        )
        usage = self._usage_for(data)
        assert self._row(usage, "codex").counters_known is False
        body = "\n".join(aggregator._render_agents_table(usage))
        assert "| Codex | — |" in body
        raw = aggregator.format_retro(data).split("<!-- MM_THEMES_PROMPT -->", 1)[1]
        payload = json.loads(raw.split("```json", 1)[1].split("```", 1)[0])
        assert payload["agents"][0]["tokens"] is None
        assert payload["agents"][0]["counters_known"] is False

    def test_themes_skill_names_are_bounded_without_losing_colliding_entries(self):
        data = self._data([])
        data.skills.by_skill = {"a" * 2000 + "x": 2, "a" * 2000 + "y": 1}
        raw = "\n".join(aggregator._render_themes_prompt(data))
        payload = json.loads(raw.split("```json", 1)[1].split("```", 1)[0])
        assert [entry["invocations"] for entry in payload["skills"]] == [2, 1]
        assert all(len(entry["skill"]) <= 128 for entry in payload["skills"])
        assert len(raw) < 2000

    def test_fleet_names_are_bounded_in_both_passes(self):
        names = {f"dev-{i:04d}": f"machine-{i:04d}" for i in range(1000)}
        data = self._data([], known_ids=tuple(names), names=names)
        out = aggregator.format_retro(data)
        entry = _health_entry(out, "fleet_incomplete")
        assert len(entry["machines"]) == aggregator.MAX_TOKEN_COVERAGE_PEER_NAMES
        assert entry["machines_omitted"] == 995
        assert "(+995 more)" in entry["detail"]
        assert len(out) < 10_000
        data.pushes.devices_with_pushes = set(names)
        final = aggregator.format_retro(data, name="test")
        assert "(+995 more)" in final
        assert len(final) < 10_000

    def test_partial_host_preserves_unknown_reader_to_model_attribution(self):
        """A reader can serve another family's models; missing coverage cannot
        be assigned by reader name alone. Inherent tier causes stay per model."""
        hosts = {
            "codex": {"2026-04-22": _usage(10)},
            "grok": {"2026-04-22": _usage(10)},
        }
        data = self._data(
            [_snap("dev-a", self.UNTIL, families=hosts, partial=("grok",))],
            known_ids=("dev-a",),
        )
        usage = self._usage_for(data)
        codex_causes = self._row(usage, "codex").floor_causes
        grok_causes = self._row(usage, "grok").floor_causes
        assert any("grok" in c and "incomplete" in c for c in codex_causes), codex_causes
        assert any("grok" in c and "incomplete" in c for c in grok_causes), grok_causes

    def test_partial_codex_serving_claude_floors_claude_cost(self):
        counters = {**_usage(1_000_000), "cache_create": 1_000_000}
        hosts = {"claude": {"2026-04-22": counters}}
        ev = _host_event(
            "dev-a",
            self.UNTIL.isoformat(),
            hosts=hosts,
            token_sources=("codex",),
            extra={
                "counter_semantics": "disjoint-v1",
                "partial_sources": ["codex"],
                "tokens_by_day": _sibling(hosts, "claude-opus-5"),
            },
        )
        data = self._data([], known_ids=("dev-a",))
        data.host_inventory = aggregator.aggregate_host_usage(
            [ev], since=self.SINCE, until=self.UNTIL, registered_ids={"dev-a"}
        )
        assert data.host_inventory.rejected_rows == 0
        row = self._row(self._usage_for(data), "claude")
        assert any("incomplete" in cause for cause in row.floor_causes)
        assert aggregator._agent_row_cost(row) == (11.25, True)

    @pytest.mark.parametrize("reader", ["codex", "opencode"])
    def test_failed_reader_without_data_floors_observed_families(self, reader):
        good = _host_event(
            "dev-a",
            self.UNTIL.isoformat(),
            hosts={"codex": {"2026-04-22": _usage(1_000_000)}},
            extra={
                "counter_semantics": "disjoint-v1",
                "tokens_by_day": {
                    "2026-04-22": {
                        **_usage(1_000_000),
                        "by_model": {"gpt-6-astra": _usage(1_000_000)},
                    }
                },
            },
        )
        failed = _host_event(
            "dev-b",
            self.UNTIL.isoformat(),
            hosts={},
            token_sources=(),
            extra={"degraded_sources": [reader], "active_days": []},
        )
        data = self._data([], known_ids=("dev-a", "dev-b"))
        data.sessions.tokens_by_model = {"claude-opus-5": _usage(1_000_000)}
        data.host_inventory = aggregator.aggregate_host_usage(
            [good, failed], since=self.SINCE, until=self.UNTIL, registered_ids={"dev-a", "dev-b"}
        )
        assert data.host_inventory.rejected_rows == 0
        expected_reader = "codex" if reader == "codex" else "unknown/retired reader"
        for row in self._usage_for(data).rows:
            assert any(expected_reader in c for c in row.floor_causes)
            assert aggregator._agent_row_cost(row)[1] is True

    def test_per_machine_grok_bounds_remain_attributed_after_fleet_sum(self):
        snaps = []
        for device, n in (("dev-a", 1_000_000), ("dev-b", 2_000_000)):
            snap = _snap(
                device,
                self.UNTIL,
                families={"grok": {"2026-04-22": _usage(n)}},
                counter_semantics="disjoint-v1",
            )
            snap.tokens_by_day = {
                "2026-04-22": {**_usage(n), "by_model": {"grok-4.6-build": _usage(n)}}
            }
            snaps.append(snap)
        data = self._data(
            snaps, known_ids=("dev-a", "dev-b"), names={"dev-a": "laptop", "dev-b": "desktop"}
        )
        row = self._row(self._usage_for(data), "grok")
        assert aggregator._agent_row_cost(row) == (6.0, True)
        assert any(c.startswith("laptop:") and "at most $4.00" in c for c in row.floor_causes)
        assert any(c.startswith("desktop:") and "at most $8.00" in c for c in row.floor_causes)

    def test_degraded_reader_flips_its_family_to_a_floor(self):
        data = self._data(
            [
                _snap(
                    "dev-a",
                    self.UNTIL,
                    families={"codex": {"2026-04-22": _usage(10)}},
                    degraded=("codex",),
                )
            ],
            known_ids=("dev-a",),
        )
        causes = self._row(self._usage_for(data), "codex").floor_causes
        assert any("failed" in c for c in causes)

    def test_unknown_reader_widens_rather_than_misattributing(self):
        """A reader id that names no family cannot be narrowed to one; the wire
        carries no reader-to-family attribution."""
        hosts = {
            "codex": {"2026-04-22": _usage(10)},
            "grok": {"2026-04-22": _usage(10)},
        }
        data = self._data(
            [_snap("dev-a", self.UNTIL, families=hosts, degraded=("mystery",))],
            known_ids=("dev-a",),
        )
        usage = self._usage_for(data)
        for key in ("codex", "grok"):
            assert self._row(usage, key).floor_causes != ()

    # --- hostnames --------------------------------------------------------

    def test_hostnames_replace_device_ids_in_health(self):
        data = self._data(
            [_snap("3a6c7dc9", self.UNTIL, families={"codex": {"2026-04-22": _usage(1)}})],
            known_ids=("3a6c7dc9", "0356e8c7"),
            names={"3a6c7dc9": "kb-mbp", "0356e8c7": "3771H-kb-ms"},
        )
        out = aggregator.format_retro(data)
        entry = _health_entry(out, "fleet_incomplete")
        assert entry is not None
        assert "3771H-kb-ms" in entry["detail"]
        assert "0356e8c7" not in entry["detail"]

    def test_unknown_device_falls_back_to_the_short_id(self):
        labels = aggregator.device_labels(aggregator.FleetState())
        assert aggregator.device_label("3a6c7dc9ff", labels) == "3a6c7dc9"

    def test_blank_device_name_falls_back_rather_than_rendering_empty(self):
        fleet = aggregator.FleetState(
            devices_known_list=[{"device_id": "dev-a", "device_name": "   "}]
        )
        assert aggregator.device_label("dev-a", aggregator.device_labels(fleet)) == "dev-a"

    @pytest.mark.parametrize("names", [("kb-mbp", "kb-mbp"), ("kb|mbp", "kb`mbp")])
    def test_colliding_display_hostnames_keep_device_identity(self, names):
        devices = ("3a6c7dc9-full", "0356e8c7-full")
        data = self._data([], known_ids=devices, names=dict(zip(devices, names)))
        labels = aggregator.device_labels(data.fleet)
        assert labels[devices[0]] != labels[devices[1]]
        for device in devices:
            assert device[:8] in labels[device]
        entry = _health_entry(aggregator.format_retro(data), "fleet_incomplete")
        assert set(entry["machines"]) == set(labels.values())

    def test_empty_host_activity_does_not_deny_observed_claude_activity(self):
        data = self._data([_snap("dev-a", self.UNTIL)], known_ids=("dev-a",))
        data.sessions.tokens_by_model = {"claude-sonnet-5": _usage(1_000_000)}
        data.sessions.active_days = {"2026-04-22"}
        data.sessions.token_devices = {"dev-a": self.UNTIL}
        out = aggregator.format_retro(data)
        assert "| Claude | 1.0M |" in out
        notes = [entry["detail"] for entry in _health(out) if entry["code"] == "agent_coverage"]
        assert any("No host-ledger activity observed" in note for note in notes)
        assert not any("No agent activity observed" in note for note in notes)

    # --- removed surfaces --------------------------------------------------

    def test_removed_sections_are_gone(self):
        data = self._data(
            [_snap("dev-a", self.UNTIL, families={"codex": {"2026-04-22": _usage(1)}})],
            known_ids=("dev-a",),
        )
        out = aggregator.format_retro(data)
        for heading in (
            "## Claude Code activity",
            "## Agent activity",
            "## API list-rate equivalent",
            "## Notes",
            "### Do not sum these values",
        ):
            assert heading not in out, heading


class TestAcceptorSchemaConstant:
    def test_acceptor_follows_the_writer_constant(self, monkeypatch):
        """A hardcoded literal here would make mm reject its own freshly written
        rows fleet-wide on the first schema bump."""
        from mind_meld import events as mm_events

        monkeypatch.setattr(mm_events, "EVENTS_SCHEMA_VERSION", 3)
        ev = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        ev["v"] = 3
        assert isinstance(aggregator._accept_host_usage_snapshot(ev), aggregator._AcceptedHostRow)
        ev["v"] = 2
        assert aggregator._accept_host_usage_snapshot(ev).reason == "unsupported_schema"
        ev["v"] = True
        assert aggregator._accept_host_usage_snapshot(ev).reason == "unsupported_schema"

    def test_tie_break_projection_uses_the_same_constant(self, monkeypatch):
        from mind_meld import events as mm_events

        monkeypatch.setattr(mm_events, "EVENTS_SCHEMA_VERSION", 3)
        ev = _host_event("dev-a", "2026-04-27T12:00:00+00:00")
        ev["v"] = 3
        row = aggregator._accept_host_usage_snapshot(ev)
        assert '"v":3' in row.tie_key


class TestZeroRepoCaptureNotes:
    def test_notes_line_when_a_device_captured_zero_repositories(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        empty = {
            "v": 2,
            "type": "git-snapshot",
            "ts": _ts(1),
            "device": "dev-quiet",
            "projects": [],
        }
        full = _git_event("dev-quiet", 1, [_commit("aaa", 1)])
        _write_events(events_dir, "dev-quiet", "2026-04-27", [empty, full])
        data = _aggregate(events_dir)
        assert data.git.zero_repo_captures["dev-quiet"] == (1, 2)
        out = aggregator.format_retro(data)
        assert "captured 0 repositories on 1 of 2 pushes" in out
        assert _health_entry(out, "discovery_errors") is None

    def test_discovery_errors_point_at_mm_diag(self):
        data = aggregator.RetroData(
            window_days=7,
            since=NOW - timedelta(days=7),
            until=NOW,
        )
        data.pushes.discovery_errors.append("git root discovery exceeded its time budget")
        out = aggregator.format_retro(data)
        assert "mm diag" in _health_entry(out, "discovery_errors")["remedy"]
        assert "stderr breadcrumbs" not in out


def _capture_on(
    ev: dict,
    *,
    since_days: float,
    aborts: int = 0,
    errors: int = 0,
    discovery: str = "complete",
) -> dict:
    ev = dict(ev)
    ev["git_capture"] = {
        "since": _ts(since_days),
        "discovery": discovery,
        "walk_budget_aborts": aborts,
        "walk_errors": errors,
    }
    return ev


class TestGitCoverageAndRecapture:
    def test_g1_recapture_rows_excluded_from_push_tally(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        recapture = {
            "v": 2,
            "type": "git-snapshot",
            "ts": _ts(1),
            "device": "dev-a",
            "projects": [],
            "origin": "recapture",
        }
        recapture = _capture_on(recapture, since_days=30)
        empty_push = {
            "v": 2,
            "type": "git-snapshot",
            "ts": _ts(1),
            "device": "dev-a",
            "projects": [],
        }
        empty_push = _capture_on(empty_push, since_days=7)
        _write_events(events_dir, "dev-a", "2026-04-27", [recapture, empty_push])
        data = _aggregate(events_dir)
        assert data.git.zero_repo_captures["dev-a"] == (1, 1)

    def test_g2_missing_origin_key_counts_as_a_push(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        empty = {
            "v": 2,
            "type": "git-snapshot",
            "ts": _ts(1),
            "device": "dev-a",
            "projects": [],
        }
        _write_events(events_dir, "dev-a", "2026-04-27", [empty])
        data = _aggregate(events_dir)
        assert data.git.zero_repo_captures["dev-a"] == (1, 1)

    def test_g3_device_with_only_recapture_rows_has_no_zero_repo_note(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        recapture = _git_event("dev-a", 1, [])
        recapture["projects"] = []
        recapture["origin"] = "recapture"
        recapture = _capture_on(recapture, since_days=30)
        _write_events(events_dir, "dev-a", "2026-04-27", [recapture])
        data = _aggregate(events_dir)
        assert data.git.zero_repo_captures == {}
        out = aggregator.format_retro(data)
        assert "captured 0 repositories" not in out

    def test_h1_recapture_row_closes_an_interval(self, tmp_path):
        """T8 includes recapture as covering; T9 excludes it from the push tally."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        recapture = _git_event("dev-a", 0, [_commit("aaa", 1)])
        recapture["origin"] = "recapture"
        recapture = _capture_on(recapture, since_days=7)
        _write_events(events_dir, "dev-a", "2026-04-28", [recapture])
        data = _aggregate(events_dir)
        assert "dev-a" not in data.git.uncovered_git
        assert "dev-a" not in data.git.zero_repo_captures

    def test_h1b_init_row_leaves_the_push_tally_but_keeps_coverage_and_commits(self, tmp_path):
        """64A: init is not a push, yet its walked history is still observed history.

        The window's only coverage before the latest push comes from the init
        row, so dropping init rows wholesale (rather than from the tally) would
        reopen a gap and lose its commit.
        """
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        init = _capture_on(_git_event("dev-a", 0.1, [_commit("aaa", 3)]), since_days=7)
        init["origin"] = "init"
        # A repo-less init snapshot would read as a zero-repository push if init
        # rows were tallied, which is what the assertion below observes.
        repo_less_init = _capture_on(dict(_git_event("dev-a", 0.2, []), projects=[]), since_days=7)
        repo_less_init["origin"] = "init"
        latest = _capture_on(_git_event("dev-a", 0, [_commit("bbb", 0)]), since_days=0.1)
        _write_events(events_dir, "dev-a", "2026-04-21", [init, repo_less_init, latest])
        data = _aggregate(events_dir)
        assert "dev-a" not in data.git.uncovered_git
        assert data.git.commits == 2
        assert data.git.zero_repo_captures == {}

    def test_h2_budget_abort_renders_budget_note_not_gap_note(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        row = _capture_on(_git_event("dev-a", 0, [_commit("aaa", 1)]), since_days=7, aborts=3)
        _write_events(events_dir, "dev-a", "2026-04-28", [row])
        data = _aggregate(events_dir)
        assert data.git.git_budget_aborts["dev-a"] == 3
        assert "dev-a" not in data.git.uncovered_git
        out = aggregator.format_retro(data)
        assert "ran out of budget" in out
        assert "git_capture.recorded.walk_budget_aborts" in out
        assert "last_push.walk_budget_aborts" not in out
        assert "uncovered interval" not in out

    def test_h3_window_before_coverage_floor_clamps(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Filename date is 2026-04-27. A 7d window starts 2026-04-21.
        # Capture covers [2026-04-27, now], so without the floor there is a
        # 6-day gap; with the floor the gap is clamped away.
        row = _capture_on(_git_event("dev-a", 0, [_commit("aaa", 0)]), since_days=1)
        _write_events(events_dir, "dev-a", "2026-04-27", [row])
        data = _aggregate(events_dir)
        assert "dev-a" not in data.git.uncovered_git

    def test_h4_device_without_git_capture_is_unknown_never_a_gap(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        _write_events(
            events_dir, "dev-a", "2026-04-27", [_git_event("dev-a", 1, [_commit("aaa", 1)])]
        )
        data = _aggregate(events_dir)
        assert data.git.uncovered_git == {}
        out = aggregator.format_retro(data)
        assert "uncovered interval" not in out

    def test_h5_git_coverage_note_aggregates_across_machines(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Two devices, each with a capture that starts after the window
        # and so leaves a gap — except the coverage floor will clamp.
        # Force a gap by covering only the last instant, with a filename
        # date at the start of the window so the floor does not swallow it.
        a = _capture_on(_git_event("laptop", 0, [_commit("aaa", 0)]), since_days=0.1)
        b = _capture_on(_git_event("desktop", 0, [_commit("bbb", 0)]), since_days=0.1)
        _write_events(events_dir, "laptop", "2026-04-21", [a])
        _write_events(events_dir, "desktop", "2026-04-21", [b])
        data = _aggregate(events_dir)
        assert "laptop" in data.git.uncovered_git
        assert "desktop" in data.git.uncovered_git
        out = aggregator.format_retro(data)
        assert out.count("uncovered interval") == 1
        assert "laptop" in out
        assert "desktop" in out

    def test_h6_trailing_day_after_latest_capture_is_not_a_gap(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # Capture through yesterday. Until is NOW (today noon). Date math
        # without the clip treats today as uncovered and nags recapture.
        row = _capture_on(_git_event("dev-a", 1, [_commit("aaa", 1)]), since_days=7)
        _write_events(events_dir, "dev-a", "2026-04-21", [row])
        data = _aggregate(events_dir)
        assert "dev-a" not in data.git.uncovered_git
        out = aggregator.format_retro(data)
        assert "uncovered interval" not in out

    def test_h7_discovery_hold_does_not_paint_coverage(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # A complete capture of only the last instant leaves an early-window
        # gap. A HOLD (partial) capture of the whole window must not fill it.
        tail = _capture_on(_git_event("dev-a", 0, [_commit("aaa", 0)]), since_days=0.1)
        hold = _capture_on(
            _git_event("dev-a", 0, [_commit("bbb", 0)]),
            since_days=7,
            discovery="partial",
        )
        _write_events(events_dir, "dev-a", "2026-04-21", [tail, hold])
        data = _aggregate(events_dir)
        assert "dev-a" in data.git.uncovered_git

    def test_h8_partial_only_device_still_reports_a_gap(self, tmp_path):
        """A device whose every walk HELD must not vanish from the card.

        Found by Greptile on PR #151. The gap loop keyed on ``covered``,
        which a HOLD capture never joins, so a machine whose walks never
        landed produced no note at all — indistinguishable from a healthy
        machine. It is now keyed on the OBSERVATION map.
        """
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        rows = [
            _capture_on(
                _git_event("dev-a", day, [_commit("aaa", day)]),
                since_days=7,
                discovery="partial",
            )
            for day in (3, 0)
        ]
        _write_events(events_dir, "dev-a", "2026-04-21", rows)
        data = _aggregate(events_dir)
        assert "dev-a" in data.git.uncovered_git
        out = aggregator.format_retro(data)
        assert "uncovered interval" in out

    def test_h9_trailing_partial_run_after_a_good_capture_is_a_gap(self, tmp_path):
        """A held push is an observation, so it extends ``latest_end``.

        Clipping to the latest COVERED interval hid every held push after
        the last good one — the exact window the user needs to recapture.
        """
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        good = _capture_on(_git_event("dev-a", 6, [_commit("aaa", 6)]), since_days=7)
        held = _capture_on(
            _git_event("dev-a", 0, [_commit("bbb", 0)]),
            since_days=6,
            discovery="partial",
        )
        _write_events(events_dir, "dev-a", "2026-04-21", [good, held])
        data = _aggregate(events_dir)
        assert "dev-a" in data.git.uncovered_git

    def test_h10_hold_capture_alone_does_not_extend_past_the_window(self, tmp_path):
        """The trailing clip still holds: a fully covered window stays clean."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        good = _capture_on(_git_event("dev-a", 0, [_commit("aaa", 0)]), since_days=7)
        held = _capture_on(
            _git_event("dev-a", 0, [_commit("bbb", 0)]),
            since_days=7,
            discovery="partial",
        )
        _write_events(events_dir, "dev-a", "2026-04-21", [good, held])
        data = _aggregate(events_dir)
        assert "dev-a" not in data.git.uncovered_git

    def test_h11_empty_discovery_is_a_fact_not_a_gap(self, tmp_path):
        """A repo-less Mac must never be told to `mm recapture`.

        ``empty`` means a prober RAN and found zero git roots — there is
        no history to have missed. That machine already gets the zero-repo
        push note, whose copy is the right one. Counting it as an
        observation would nag every repo-less Mac on every retro forever.
        """
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        rows = []
        for day in (3, 0):
            ev = _git_event("dev-a", day, [])
            ev["projects"] = []
            rows.append(_capture_on(ev, since_days=7, discovery="empty"))
        _write_events(events_dir, "dev-a", "2026-04-21", rows)
        data = _aggregate(events_dir)
        assert "dev-a" not in data.git.uncovered_git
        out = aggregator.format_retro(data)
        assert "uncovered interval" not in out
        # The signal the user actually needs is still there.
        assert data.git.zero_repo_captures["dev-a"] == (2, 2)
        assert "captured 0 repositories" in out

    def test_h12_empty_run_after_a_good_capture_is_not_a_gap(self, tmp_path):
        """``empty`` does not extend ``latest_end`` either."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        good = _capture_on(_git_event("dev-a", 6, [_commit("aaa", 6)]), since_days=7)
        gone = _git_event("dev-a", 0, [])
        gone["projects"] = []
        gone = _capture_on(gone, since_days=6, discovery="empty")
        _write_events(events_dir, "dev-a", "2026-04-21", [good, gone])
        data = _aggregate(events_dir)
        assert "dev-a" not in data.git.uncovered_git


# ---------------------------------------------------------------------------
# Track 35A — counter semantics, per-device API list-rate equivalent
# ---------------------------------------------------------------------------


def _priced_hosts(n: int = 1_000_000, day: str = "2026-04-22") -> dict:
    return {"codex": {day: {"input": n, "cache_create": 0, "cache_read": 0, "output": 0}}}


def _econ_data(events, *, since=None, until=None) -> aggregator.RetroData:
    since = since or datetime(2026, 4, 21, tzinfo=timezone.utc)
    until = until or datetime(2026, 4, 28, 12, tzinfo=timezone.utc)
    data = aggregator.RetroData(window_days=7, since=since, until=until)
    data.host_inventory = aggregator.aggregate_host_usage(
        events, since=since, until=until, registered_ids=None
    )
    ids = list(data.host_inventory.by_device)
    data.fleet = aggregator.FleetState(
        devices_in_events=set(ids),
        devices_known=len(ids) or None,
        devices_known_list=[{"device_id": d, "device_name": d} for d in ids],
    )
    return data


class TestCounterSemanticsMarker:
    TS = "2026-04-28T12:00:00+00:00"

    def test_semantics_marker_absent_means_legacy_inclusive(self):
        ev = _host_event("dev-a", self.TS, extra={"counter_semantics": None})
        row = _accepted(ev)
        assert row.counter_semantics is None

    def test_semantics_marker_unknown_value_fails_closed(self):
        ev = _host_event("dev-a", self.TS, extra={"counter_semantics": "disjoint-v2"})
        row = _accepted(ev)
        assert row.counter_semantics is None

    def test_semantics_marker_wrong_type_or_hostile_length_rejected(self):
        for value in (True, 1, ["disjoint-v1"], "x" * 64):
            ev = _host_event("dev-a", self.TS, extra={"counter_semantics": value})
            row = _accepted(ev)
            assert row.counter_semantics is None, value

    def test_semantics_marker_participates_in_sibling_tie_key(self):
        plain = _host_event("dev-a", self.TS)
        legacy = _host_event("dev-a", self.TS, extra={"counter_semantics": None})
        a = _accepted(plain)
        b = _accepted(legacy)
        assert a.tie_key == b.tie_key
        assert aggregator._sibling_tie_key(a) != aggregator._sibling_tie_key(b)

    def test_equal_ts_legacy_and_disjoint_rows_select_deterministically(self):
        disjoint = _host_event("dev-a", self.TS)
        legacy = _host_event("dev-a", self.TS, extra={"counter_semantics": None})
        since = datetime(2026, 4, 21, tzinfo=timezone.utc)
        until = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)
        first = aggregator.aggregate_host_usage(
            [disjoint, legacy], since=since, until=until, registered_ids=None
        )
        second = aggregator.aggregate_host_usage(
            [legacy, disjoint], since=since, until=until, registered_ids=None
        )
        assert (
            first.by_device["dev-a"].counter_semantics
            == second.by_device["dev-a"].counter_semantics
        )
        assert first.by_device["dev-a"].counter_semantics == "disjoint-v1"

    def test_events_schema_version_is_not_bumped(self):
        from mind_meld import events as mm_events

        assert mm_events.EVENTS_SCHEMA_VERSION == 2
        ev = _host_event("dev-a", self.TS)
        assert ev["v"] == 2

    def test_marker_present_tokens_by_day_absent(self):
        ev = _host_event("dev-a", self.TS)
        assert "tokens_by_day" not in ev
        assert ev["counter_semantics"] == "disjoint-v1"
        row = _accepted(ev)
        assert row.counter_semantics == "disjoint-v1"
        assert row.tokens_by_day is None

    def test_disjoint_marker_plus_invalid_per_model_sibling(self):
        ev = _host_event(
            "dev-a",
            self.TS,
            extra={"tokens_by_day": "not-a-map"},
        )
        row = _accepted(ev)
        assert row.counter_semantics == "disjoint-v1"
        assert row.tokens_by_day is None
        assert row.detail_reason == "unsupported_schema"


class TestAgentEconomics:
    """Cost semantics on the unified Agents row.

    Successor to ``TestHostEconomics``, which pinned a per-machine dollar
    table plus a "do not sum these values" heading. The rules it protected —
    what makes a figure a floor, what makes it unavailable, and that an
    unreconstructable long-context tier names a MODEL rather than a reader —
    all still hold; they are asserted here against the surface that replaced
    it, including through the health payload where the causes now live.
    """

    TS = "2026-04-28T12:00:00+00:00"

    def _row(self, data, key="codex"):
        usage = aggregator.aggregate_agent_usage(data, machines_known=data.fleet.devices_known)
        return next((r for r in usage.rows if r.key == key), None)

    def test_priced_host_tokens_produce_an_estimate(self):
        hosts = _priced_hosts()
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={"tokens_by_day": _sibling(hosts, "gpt-5.6-terra")},
        )
        data = _econ_data([ev])
        total, floor = aggregator._agent_row_cost(self._row(data))
        assert total == pytest.approx(2.0, rel=1e-6)
        assert floor is False
        assert "~$2.00" in aggregator.format_retro(data)

    def test_pricing_provenance_survives_in_the_health_payload(self):
        """The rendered six-line vendor header is gone; the dates are not."""
        hosts = _priced_hosts()
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={"tokens_by_day": _sibling(hosts, "gpt-5.6-terra")},
        )
        out = aggregator.format_retro(_econ_data([ev]))
        block = out.split("<!-- MM_HEALTH -->", 1)[1].split("```json", 1)[1].split("```", 1)[0]
        pricing = json.loads(block)["pricing"]
        tu = aggregator.token_usage
        assert pricing["anthropic_verified"] == tu.PRICING_LAST_UPDATED
        assert pricing["openai_verified"] == tu.PRICING_OPENAI_LAST_UPDATED
        assert pricing["xai_verified"] == tu.PRICING_XAI_LAST_UPDATED
        assert "docs.x.ai" in pricing["xai_url"]
        assert "never zero" in pricing["basis"]

    @pytest.mark.parametrize("readers", [("grok",), ("codex", "grok")])
    def test_long_context_cause_names_the_model_not_the_reader(self, readers):
        """The cause is a property of the vendor's LOG FORMAT, keyed on the
        model id, so which reader happened to collect it is irrelevant."""
        hosts = {
            "grok": {
                "2026-04-22": {"input": 1_000_000, "cache_read": 0, "cache_create": 0, "output": 0}
            }
        }
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=readers,
            hosts=hosts,
            extra={"tokens_by_day": _sibling(hosts, "grok-4.6-build")},
        )
        joined = " ".join(self._row(_econ_data([ev]), "grok").floor_causes)
        assert "grok-4.6-build" in joined
        assert "per-request prompt sizes" in joined

    def test_coverage_loss_suppresses_the_long_context_ceiling(self):
        hosts = {
            "grok": {
                "2026-04-22": {"input": 1_000_000, "cache_read": 0, "cache_create": 0, "output": 0}
            }
        }
        ev = _host_event(
            "dev-a",
            self.TS,
            token_sources=("grok",),
            hosts=hosts,
            extra={
                "tokens_by_day": _sibling(hosts, "grok-4.6-build"),
                "partial_sources": ["grok"],
            },
        )
        causes = " ".join(self._row(_econ_data([ev]), "grok").floor_causes)
        assert "at most" not in causes
        assert "incomplete" in causes

    def test_partial_host_flips_the_row_to_a_floor(self):
        hosts = _priced_hosts()
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": _sibling(hosts, "gpt-5.6-terra"),
                "partial_sources": ["codex"],
            },
        )
        data = _econ_data([ev])
        _total, floor = aggregator._agent_row_cost(self._row(data))
        assert floor is True
        assert "incomplete" in " ".join(self._row(data).floor_causes)
        assert "≥$" in aggregator.format_retro(data)

    def test_failed_reader_elsewhere_floors_the_fleet_row(self):
        """``degraded_sources`` is disjoint from ``token_sources`` by contract —
        a failed reader contributed nothing — so the machine whose codex reader
        died reports no codex data at all. The fleet's Codex total is therefore
        missing that machine's share, which is precisely a lower bound."""
        codex_hosts = _priced_hosts()
        grok_hosts = {
            "grok": {
                "2026-04-22": {"input": 1_000, "cache_read": 0, "cache_create": 0, "output": 0}
            }
        }
        healthy = _host_event(
            "dev-a",
            self.TS,
            hosts=codex_hosts,
            extra={"tokens_by_day": _sibling(codex_hosts, "gpt-5.6-terra")},
        )
        broken = _host_event(
            "dev-b",
            self.TS,
            token_sources=("grok",),
            hosts=grok_hosts,
            extra={
                "tokens_by_day": _sibling(grok_hosts, "grok-4.6-build"),
                "degraded_sources": ["codex"],
            },
        )
        data = _econ_data([healthy, broken])
        codex = self._row(data, "codex")
        assert "failed" in " ".join(codex.floor_causes)
        assert aggregator._agent_row_cost(codex)[1] is True

    def test_unpriced_model_makes_cost_unavailable_and_names_the_id(self):
        hosts = _priced_hosts()
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={"tokens_by_day": _sibling(hosts, "gpt-not-a-real-model")},
        )
        data = _econ_data([ev])
        causes = aggregator.agent_row_floor_causes(self._row(data))
        assert any("unpriced" in c and "gpt-not-a-real-model" in c for c in causes)
        out = aggregator.format_retro(data)
        assert _health_entry(out, "cost_unavailable")["causes"] == list(causes)
        assert _health_entry(out, "cost_floor") is None

    @pytest.mark.parametrize(
        "state,expected_code",
        [
            ("priced", None),
            ("partial", "cost_floor"),
            ("unpriced", "cost_unavailable"),
            ("absent", "cost_unavailable"),
            ("dropped", "cost_unavailable"),
            ("zero", None),
        ],
    )
    def test_cost_health_matches_the_visible_row(self, state, expected_code):
        hosts = _priced_hosts(0 if state == "zero" else 1_000_000)
        model = "gpt-not-a-real-model" if state == "unpriced" else "gpt-5.6-terra"
        extra = {"tokens_by_day": _sibling(hosts, model)}
        if state in {"partial", "zero"}:
            extra["partial_sources"] = ["codex"]
        if state == "absent":
            extra.pop("tokens_by_day")
        if state == "dropped":
            extra["tokens_by_day"] = "not-a-map"
        data = _econ_data([_host_event("dev-a", self.TS, hosts=hosts, extra=extra)])
        out = aggregator.format_retro(data)
        cost_issues = [e for e in _health(out) if e["code"] in {"cost_floor", "cost_unavailable"}]
        assert [e["code"] for e in cost_issues] == ([expected_code] if expected_code else [])
        table_row = next((line for line in out.splitlines() if line.startswith("| Codex |")), "")
        if state == "zero":
            assert not table_row
        elif expected_code == "cost_unavailable":
            assert "| 1.0M | 1 | 1 | — |" in table_row
            assert cost_issues[0]["causes"]
            assert "unavailable" in cost_issues[0]["detail"]
            assert "is a floor" not in cost_issues[0]["detail"]
        elif expected_code == "cost_floor":
            assert "≥$" in table_row
        else:
            assert "~$2.00" in table_row

    @pytest.mark.parametrize("dropped", [False, True])
    def test_missing_model_detail_names_machine_reason_and_attended_remedy(self, dropped):
        extra = {"tokens_by_day": "not-a-map"} if dropped else {}
        data = _econ_data([_host_event("dev-a", self.TS, hosts=_priced_hosts(), extra=extra)])
        data.fleet.devices_known_list = [{"device_id": "dev-a", "device_name": "kb-mbp"}]
        snap = data.host_inventory.by_device["dev-a"]
        assert snap.detail_reason == ("unsupported_schema" if dropped else None)
        issue = _health_entry(aggregator.format_retro(data), "cost_unavailable")
        cause = next(c for c in issue["causes"] if "per-model host tokens" in c)
        assert "kb-mbp" in cause
        assert ("unsupported_schema" if dropped else "absent") in cause
        assert aggregator.ATTENDED_USAGE_MIN_VERSION in cause
        assert "mm --version" in cause
        assert "mm push" in cause

    def test_many_machine_detail_failures_preserve_provenance_but_bound_health_output(self):
        events = [
            _host_event(f"laptop-{i:04d}", self.TS, hosts=_priced_hosts(1_000_000 + i))
            for i in range(1_000)
        ]
        data = _econ_data(events)
        assert data.host_inventory.rejected_rows == 0
        row = self._row(data)
        assert len(row.floor_causes) == 1_000
        assert any(c.startswith("laptop-0999:") for c in row.floor_causes)
        out = aggregator.format_retro(data)
        entry = _health_entry(out, "cost_unavailable")
        assert len(entry["causes"]) <= aggregator.MAX_AGENT_FLOOR_CAUSES + 1
        assert any(
            "additional" in cause and "details omitted" in cause for cause in entry["causes"]
        )
        assert len(out) < 20_000

        hosts = _priced_hosts(5_000)
        events.append(
            _host_event(
                "unpriced-laptop",
                self.TS,
                hosts=hosts,
                extra={"tokens_by_day": _sibling(hosts, "gpt-not-a-real-model")},
            )
        )
        out = aggregator.format_retro(_econ_data(events))
        causes = _health_entry(out, "cost_unavailable")["causes"]
        assert any(
            "gpt-not-a-real-model" in cause and "do not estimate" in cause for cause in causes
        )
        assert len(out) < 20_000

    def test_missing_per_model_sibling_keeps_tokens_and_drops_cost(self):
        """`—` is unavailable, never a confident $0. The token volume is real
        and still reported."""
        ev = _host_event("dev-a", self.TS, hosts=_priced_hosts())
        data = _econ_data([ev])
        row = self._row(data)
        assert row.tokens == 1_000_000
        assert aggregator._agent_row_cost(row) == (None, False)
        out = aggregator.format_retro(data)
        assert "| Codex | 1.0M | 1 | 1 | — |" in out

    def test_legacy_inclusive_counters_render_unavailable_not_a_number(self):
        """Inclusive counters are a ceiling up to ~2x high — the one caveat
        that points the wrong way, so it never becomes a rendered figure."""
        hosts = _priced_hosts()
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "tokens_by_day": _sibling(hosts, "gpt-5.6-terra"),
                "counter_semantics": None,
            },
        )
        data = _econ_data([ev])
        assert self._row(data).counters_known is False
        out = aggregator.format_retro(data)
        assert "| Codex | — |" in out
        assert _health_entry(out, "legacy_counters") is not None

    def test_legacy_counters_do_not_leak_numeric_pricing_diagnostics_or_theme_tokens(self):
        hosts = {"claude": {"2026-04-22": _usage(1_000_000)}}
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={
                "counter_semantics": None,
                "tokens_by_day": {
                    "2026-04-22": {
                        **_usage(1_000_000),
                        "by_model": {
                            "claude-opus-6": _usage(500_000),
                            "claude-unknown-1": _usage(500_000),
                        },
                    }
                },
            },
        )
        data = _econ_data([ev])
        row = self._row(data, "claude")
        assert aggregator._unpriced_token_summary(row.by_model)[0] == 500_000
        assert aggregator._extrapolation_notes(row.by_model, scope="Claude")
        out = aggregator.format_retro(data)
        agent_issues = [e for e in _health(out) if e.get("agent") == "Claude"]
        assert [e["code"] for e in agent_issues] == ["legacy_counters"]
        assert "| Claude | — |" in out
        block = (
            out.split("<!-- MM_THEMES_PROMPT -->", 1)[1].split("```json", 1)[1].split("```", 1)[0]
        )
        agent = json.loads(block)["agents"][0]
        assert agent["tokens"] is None
        assert agent["counters_known"] is False

    def test_legacy_health_names_only_legacy_machines_with_bounded_provenance(self):
        count = aggregator.MAX_TOKEN_COVERAGE_PEER_NAMES + 2
        legacy_devices = [f"legacy-{i}" for i in range(count)]
        events = []
        for i, device in enumerate([*legacy_devices, "healthy"]):
            hosts = _priced_hosts(1_000_000 + i)
            extra = {"tokens_by_day": _sibling(hosts, "gpt-5.6-terra")}
            if device in legacy_devices:
                extra["counter_semantics"] = None
            events.append(_host_event(device, self.TS, hosts=hosts, extra=extra))
        data = _econ_data(events)
        data.fleet.devices_known_list = [
            {"device_id": d, "device_name": f"laptop-{i}"} for i, d in enumerate(legacy_devices)
        ] + [{"device_id": "healthy", "device_name": "healthy-desktop"}]
        assert self._row(data).legacy_devices == tuple(sorted(legacy_devices))
        entry = _health_entry(aggregator.format_retro(data), "legacy_counters")
        assert entry["machines"] == [
            f"laptop-{i}" for i in range(aggregator.MAX_TOKEN_COVERAGE_PEER_NAMES)
        ]
        assert entry["machines_omitted"] == 2
        assert "healthy-desktop" not in entry["detail"]
        assert aggregator.ATTENDED_USAGE_MIN_VERSION in entry["remedy"]
        assert "mm --version" in entry["remedy"]
        assert "mm push" in entry["remedy"]

    def test_stale_snapshot_is_absent_rather_than_zero(self):
        hosts = _priced_hosts(day="2026-03-02")
        ev = _host_event(
            "dev-a",
            "2026-03-03T12:00:00+00:00",
            hosts=hosts,
            extra={"tokens_by_day": _sibling(hosts, "gpt-5.6-terra")},
        )
        data = _econ_data([ev])
        assert self._row(data) is None
        out = aggregator.format_retro(data)
        assert "$0" not in out

    def test_duplicate_history_is_detected_rather_than_refused(self):
        """Pre-1.1 this rendered two per-machine floors and no fleet figure,
        because the aggregator believed the overlap was undetectable."""
        hosts = _priced_hosts()
        events = [
            _host_event(
                dev,
                self.TS,
                hosts=hosts,
                extra={"tokens_by_day": _sibling(hosts, "gpt-5.6-terra")},
            )
            for dev in ("dev-a", "dev-b")
        ]
        data = _econ_data(events)
        usage = aggregator.aggregate_agent_usage(data, machines_known=data.fleet.devices_known)
        assert usage.duplicate_ledger == ("codex",)
        out = aggregator.format_retro(data)
        assert _health_entry(out, "duplicate_ledger") is not None

    def test_claude_and_host_dollars_are_separate_rows_never_one_total(self):
        """The pre-1.1 invariant was 'host cost never enters the token block'.
        One table per agent makes the mistake structurally unavailable, but the
        property is still worth pinning."""
        hosts = _priced_hosts()
        ev = _host_event(
            "dev-a",
            self.TS,
            hosts=hosts,
            extra={"tokens_by_day": _sibling(hosts, "gpt-5.6-terra")},
        )
        data = _econ_data([ev])
        data.sessions.active_days = {"2026-04-22"}
        data.sessions.token_devices = {"dev-a": datetime(2026, 4, 28, tzinfo=timezone.utc)}
        data.sessions.tokens_by_model = {
            "claude-sonnet-5": {"input": 5_000, "cache_create": 0, "cache_read": 0, "output": 0}
        }
        usage = aggregator.aggregate_agent_usage(data, machines_known=data.fleet.devices_known)
        keys = [r.key for r in usage.rows]
        assert keys == ["claude", "codex"]
        assert "All models" not in aggregator.format_retro(data)


@pytest.mark.parametrize(
    "origin,count", [(None, 1), ("future-origin", 1), ("init", 0), ("recapture", 0)]
)
def test_non_push_origins_and_mixed_version_counting(origin, count):
    now = datetime.now(timezone.utc)
    row = {"type": "git-snapshot", "ts": now.isoformat(), "device": "dev-a", "projects": []}
    if origin is not None:
        row["origin"] = origin
    result = aggregator.aggregate_git(
        [row], since=now - timedelta(days=7), until=now, author_emails=None
    )
    # A pre-64A renderer knows only recapture, so it keeps counting init rows as
    # pushes until it upgrades; new init rows stay readable to it.
    assert result.zero_repo_captures == ({"dev-a": (1, 1)} if count else {})
