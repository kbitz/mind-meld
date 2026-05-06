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
    skill_usage_path: Path | None = None,
    now: datetime = NOW,
) -> aggregator.RetroData:
    return aggregator.aggregate(
        events_dir=events_dir,
        window_days=window_days,
        author_emails=author_emails,
        skill_usage_path=skill_usage_path or (Path("/nonexistent/skill-usage.jsonl")),
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

    def test_gstack_skill_usage_unknown_field_tolerated(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        skill_path = tmp_path / "skill-usage.jsonl"
        skill_path.write_text(
            json.dumps({"skill": "ship", "ts": _ts(0), "future_unknown_field": True}) + "\n"
        )
        data = _aggregate(events_dir, skill_usage_path=skill_path)
        assert data.skills.available is True
        assert data.skills.invocations == 1


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

    def test_skip_categories_tracked_separately(self, tmp_path):
        """Per-source skip counters discriminate mm events vs gstack files
        — a torn gstack file shouldn't read as 'mm events skipped'."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # One torn line in mm events.
        ev_path = events_dir / "dev-a-2026-04-28.jsonl"
        ev_path.write_text("not json\n" + json.dumps(_push_event("dev-a", 0)) + "\n")
        # Two malformed records in skill-usage.
        skill_path = tmp_path / "skill-usage.jsonl"
        skill_path.write_text("not json line 1\nalso not json\n")
        data = _aggregate(events_dir, skill_usage_path=skill_path)
        assert data.skipped_per_source.get(aggregator.SKIP_CATEGORY_EVENTS) == 1
        assert data.skipped_per_source.get(aggregator.SKIP_CATEGORY_SKILL_USAGE) == 2
        assert data.skipped_lines == 3  # backward-compat sum

    def test_per_source_breadcrumbs_name_the_file(self, tmp_path):
        """format_retro renders one breadcrumb per affected file so the user
        knows where to look — pre-fix all skips were lumped under
        'event(s) skipped due to parse errors' regardless of source."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        skill_path = tmp_path / "skill-usage.jsonl"
        skill_path.write_text("garbage\n")
        data = _aggregate(events_dir, skill_usage_path=skill_path)
        out = aggregator.format_retro(data)
        assert "skill-usage.jsonl" in out
        assert "gstack file format issue, not mm" in out
        # No mm-events skips in this scenario — the mm-events breadcrumb
        # must NOT fire (the user's bug report was the inverse: gstack
        # parse errors masquerading as mm event corruption).
        assert "skipped due to parse errors in mm event log" not in out

    def test_pretty_printed_json_in_skill_usage_recovered(self, tmp_path):
        """Symmetric to the eureka case — skill-usage.jsonl uses the same
        tolerant reader, so multi-line pretty JSON must recover cleanly
        there too. Without this test, a regression in the skills parser
        could ship unnoticed."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        skill_path = tmp_path / "skill-usage.jsonl"
        skill_path.write_text(
            '{\n  "skill": "ship",\n  "ts": "2026-04-26T00:00:00Z"\n}\n'
            '{\n  "skill": "review",\n  "ts": "2026-04-27T00:00:00Z"\n}\n'
        )
        data = _aggregate(events_dir, skill_usage_path=skill_path)
        assert data.skills.invocations == 2
        assert data.skipped_per_source.get(aggregator.SKIP_CATEGORY_SKILL_USAGE, 0) == 0

    def test_breadcrumb_names_actual_path_not_hardcoded(self, tmp_path):
        """Breadcrumbs must name the actual path passed to aggregate(), not
        a hardcoded ~/.gstack/analytics/... pointer that misleads callers
        using custom analytics paths."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        skill_path = tmp_path / "custom-skills.jsonl"
        skill_path.write_text("garbage\n")
        data = _aggregate(events_dir, skill_usage_path=skill_path)
        out = aggregator.format_retro(data)
        assert str(skill_path) in out
        assert "~/.gstack/analytics/skill-usage.jsonl" not in out

    def test_oversized_gstack_file_skipped_without_slurp(self, tmp_path, monkeypatch):
        """A runaway gstack analytics file beyond JSON_STREAM_MAX_BYTES
        must surface as a skip rather than spike aggregator memory. We
        simulate by lowering the cap and writing a small file just above it.
        """
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        skill_path = tmp_path / "skill-usage.jsonl"
        skill_path.write_text('{"skill":"x","ts":"2026-04-27T00:00:00Z"}\n' * 50)
        monkeypatch.setattr(aggregator, "JSON_STREAM_MAX_BYTES", 100)
        data = _aggregate(events_dir, skill_usage_path=skill_path)
        assert data.skills.invocations == 0
        assert data.skipped_per_source.get(aggregator.SKIP_CATEGORY_SKILL_USAGE, 0) == 1

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
        assert "this machine only" in out
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
        result = aggregate_sessions(
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
        result = aggregate_sessions(
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
        result = aggregate_sessions(
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
        assert "5 sessions across 1 projects" in out

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
        result = aggregate_sessions(
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
