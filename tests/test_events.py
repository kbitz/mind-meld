"""Tests for mind_meld.events — Track 7A surface (per the revised plan at
docs/designs/track-7a-revised-plan.md).

Coverage buckets:
  T0 — design's original 6 cases (URL canon table, append+parse, flock
       under autopush, conductor-pattern, empty/merge commits, schema)
  T1 — new surface from review decisions (discover_git_roots, last_push_ts,
       fsutil helper retrofit regression already covered in
       tests/test_pullhistory.py)
  T2 — correctness pins (timeout formula, budget abort, write-order)
  T3 — codex CT corrections + hardening
"""

from __future__ import annotations

import json
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from mind_meld import events

# ---------------------------------------------------------------------------
# T0/T3 — canonicalize_remote_url table + adversarial credentials
# ---------------------------------------------------------------------------


class TestCanonicalizeRemoteUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            # T0: 5 canonical forms
            ("https://github.com/foo/bar.git", "github.com/foo/bar"),
            ("git://github.com/foo/bar.git", "github.com/foo/bar"),
            ("ssh://git@github.com:22/foo/bar.git", "github.com/foo/bar"),
            ("git@github.com:foo/bar.git", "github.com/foo/bar"),
            (
                "https://x-access-token:T0KEN@github.com/foo/bar.git",
                "github.com/foo/bar",
            ),
        ],
    )
    def test_five_canonical_forms(self, url, expected):
        assert events.canonicalize_remote_url(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            # CT-10: adversarial credential strip
            (
                "https://user:pass@github.com/foo/bar.git",
                "github.com/foo/bar",
            ),
            (
                "https://github.com/foo/bar.git?token=abc123",
                "github.com/foo/bar",
            ),
            (
                "https://github.com/foo/bar.git?access_token=secret",
                "github.com/foo/bar",
            ),
            (
                "https://github.com/foo/bar.git#fragment",
                "github.com/foo/bar",
            ),
            (
                "https://x-access-token:T0KEN@github.com/foo/bar.git?token=abc",
                "github.com/foo/bar",
            ),
            # GitLab subgroup paths preserved (not collapsed)
            (
                "https://gitlab.com/group/subgroup/repo.git",
                "gitlab.com/group/subgroup/repo",
            ),
            # Trailing slash
            ("https://github.com/foo/bar/", "github.com/foo/bar"),
            # Host case-folded; path case preserved
            ("https://GITHUB.COM/Foo/Bar.git", "github.com/Foo/Bar"),
        ],
    )
    def test_adversarial_and_edge_cases(self, url, expected):
        assert events.canonicalize_remote_url(url) == expected

    def test_credentials_never_appear_in_output(self):
        """Hard guarantee: no input variant produces output containing the
        token, password, or query value."""
        for url in [
            "https://x-access-token:SECRET_TOKEN_VALUE@github.com/foo/bar.git",
            "https://user:SECRET_PASSWORD@github.com/foo/bar.git",
            "https://github.com/foo/bar.git?token=SECRET_QUERY",
            "https://github.com/foo/bar.git?access_token=SECRET_QUERY",
        ]:
            out = events.canonicalize_remote_url(url)
            assert "SECRET" not in out, f"credential leaked from {url!r} -> {out!r}"

    @pytest.mark.parametrize("url", ["", "  ", None, 42, "foo"])
    def test_malformed_input_returns_empty_or_safe(self, url):
        # Malformed input must not crash; returns "" or a safe canonical form.
        # Non-string inputs (None, 42) are also tolerated.
        out = events.canonicalize_remote_url(url)
        assert isinstance(out, str)

    def test_bare_host_path_passes_through(self):
        assert events.canonicalize_remote_url("github.com/foo/bar") == "github.com/foo/bar"


# ---------------------------------------------------------------------------
# T1 — discover_git_roots multi-prober
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo at `path` so `git rev-parse --show-toplevel`
    succeeds. Uses subprocess so the worktree-vs-dir invariant (CT-1) is
    actually exercised."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


def _make_git_worktree(repo: Path, worktree: Path, branch: str = "wt") -> None:
    """Create a worktree at `worktree` (where `.git` is a FILE, not a dir).
    This is what Conductor workspaces actually look like (CT-1)."""
    # Need at least one commit before we can branch a worktree.
    (repo / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree)],
        check=True,
    )


class TestDiscoverGitRoots:
    def test_empty_config_no_sources_returns_empty(self, tmp_path, monkeypatch):
        # Fully empty config — no sources, no manual roots.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        roots, errors = events.discover_git_roots({"sync": {"sources": []}})
        assert roots == []
        assert errors == []

    def test_manual_repo_roots_additive(self, tmp_path):
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        config = {
            "sync": {"sources": []},  # no probers
            "retro": {"repo_roots": [str(repo)]},
        }
        roots, errors = events.discover_git_roots(config)
        assert roots == [repo.resolve()]
        assert errors == []

    def test_filter_drops_non_git_paths(self, tmp_path):
        not_a_repo = tmp_path / "plain-dir"
        not_a_repo.mkdir()
        config = {
            "sync": {"sources": []},
            "retro": {"repo_roots": [str(not_a_repo)]},
        }
        roots, errors = events.discover_git_roots(config)
        assert roots == []  # filtered by git-toplevel check
        assert errors == []

    def test_worktree_passes_filter_ct1(self, tmp_path):
        """CT-1: Conductor workspaces are git worktrees with `.git` as a
        FILE. The filter MUST NOT silently exclude them."""
        repo = tmp_path / "main-repo"
        _init_git_repo(repo)
        worktree = tmp_path / "wt"
        _make_git_worktree(repo, worktree)
        # Sanity: .git in the worktree is a FILE, not a dir
        assert (worktree / ".git").is_file()
        assert not (worktree / ".git").is_dir()
        config = {
            "sync": {"sources": []},
            "retro": {"repo_roots": [str(worktree)]},
        }
        roots, errors = events.discover_git_roots(config)
        assert worktree.resolve() in roots, "worktree was silently excluded (CT-1 regression)"

    def test_dedup_via_resolve(self, tmp_path):
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        link = tmp_path / "alias"
        link.symlink_to(repo)
        config = {
            "sync": {"sources": []},
            "retro": {"repo_roots": [str(repo), str(link)]},
        }
        roots, errors = events.discover_git_roots(config)
        # Same inode via .resolve() — only one entry survives
        assert len(roots) == 1

    def test_gstack_disabled_skips_gstack_prober(self, tmp_path, monkeypatch):
        # If gstack source isn't in resolved sources, prober doesn't run.
        # We verify by setting up a fake gstack registry under tmp HOME and
        # confirming an empty sources list ignores it.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        gstack_proj = tmp_path / ".gstack" / "projects" / "myproj"
        gstack_proj.mkdir(parents=True)
        repo = tmp_path / "real-repo"
        _init_git_repo(repo)
        (gstack_proj / "repo-mode.json").write_text(json.dumps({"repo_root": str(repo)}))
        # Empty explicit sources → gstack not in enabled set
        roots, errors = events.discover_git_roots({"sync": {"sources": []}})
        assert roots == []  # gstack prober was not invoked

    def test_gstack_enabled_reads_repo_mode_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        gstack_proj = tmp_path / ".gstack" / "projects" / "myproj"
        gstack_proj.mkdir(parents=True)
        repo = tmp_path / "real-repo"
        _init_git_repo(repo)
        (gstack_proj / "repo-mode.json").write_text(json.dumps({"repo_root": str(repo)}))
        config = {"sync": {"sources": [{"name": "gstack"}]}}
        roots, errors = events.discover_git_roots(config)
        assert repo.resolve() in roots

    def test_claude_prober_reads_cwd_field(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        claude_proj = tmp_path / ".claude" / "projects" / "-tmp-foo-bar"
        claude_proj.mkdir(parents=True)
        repo = tmp_path / "decoded"
        _init_git_repo(repo)
        # Most-recent jsonl contains cwd pointing at the real path
        jsonl = claude_proj / "session.jsonl"
        jsonl.write_text(json.dumps({"cwd": str(repo), "type": "user"}) + "\n")
        config = {"sync": {"sources": [{"name": "claude"}]}}
        roots, errors = events.discover_git_roots(config)
        assert repo.resolve() in roots

    def test_prober_failure_appends_to_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Create a malformed gstack registry — JSONDecodeError gets caught
        # PER-FILE inside _probe_gstack (forensic), so the prober itself
        # doesn't raise. We force a top-level error by patching the prober
        # to raise. This pins the contract that prober failures are
        # captured in `errors`.
        config = {"sync": {"sources": [{"name": "gstack"}]}}
        with mock.patch.object(events, "_probe_gstack", side_effect=RuntimeError("boom")):
            roots, errors = events.discover_git_roots(config)
        assert roots == []
        assert any("gstack prober" in e for e in errors)


# ---------------------------------------------------------------------------
# T0/T3 — _parse_git_log_numstat (empty, merge, binary, rename)
# ---------------------------------------------------------------------------


class TestParseGitLogNumstat:
    def test_empty_input(self):
        assert events._parse_git_log_numstat("") == []

    def test_single_commit_text_files(self):
        out = (
            "\x1eabc123\t2026-04-28T10:00:00+00:00\tme@x.com\tfix bug\n"
            "5\t3\tfile.py\n"
            "10\t0\tnewfile.py\n"
        )
        commits = events._parse_git_log_numstat(out)
        assert len(commits) == 1
        c = commits[0]
        assert c["sha"] == "abc123"
        assert c["author_email"] == "me@x.com"
        assert c["subject"] == "fix bug"
        assert c["files"] == 2
        assert c["add"] == 15
        assert c["del"] == 3

    def test_binary_numstat_treated_as_zero(self):
        out = "\x1edeadbeef\t2026-04-28T10:00:00+00:00\tme@x.com\tadd image\n-\t-\timage.png\n"
        commits = events._parse_git_log_numstat(out)
        assert commits[0]["files"] == 1
        assert commits[0]["add"] == 0
        assert commits[0]["del"] == 0

    def test_rename_row_is_one_file(self):
        out = "\x1ec0ffee\t2026-04-28T10:00:00+00:00\tme@x.com\trename\n0\t0\told.py => new.py\n"
        commits = events._parse_git_log_numstat(out)
        assert commits[0]["files"] == 1

    def test_merge_or_empty_commit_files_zero(self):
        # Format line with NO numstat rows between separators
        out = "\x1edead00\t2026-04-28T10:00:00+00:00\tm@x.com\tmerge\n"
        commits = events._parse_git_log_numstat(out)
        assert len(commits) == 1
        assert commits[0]["files"] == 0
        assert commits[0]["add"] == 0
        assert commits[0]["del"] == 0

    def test_del_field_serializes_as_del_not_del_underscore(self):
        """CT-8: 'del' is a Python reserved word. Functional TypedDict ensures
        the JSON field name stays 'del'."""
        out = "\x1ex\t2026-04-28T10:00:00+00:00\tx@x.com\tx\n5\t3\tf.py\n"
        commits = events._parse_git_log_numstat(out)
        as_json = json.loads(json.dumps(commits[0]))
        assert "del" in as_json
        assert "del_" not in as_json
        assert as_json["del"] == 3

    def test_commit_date_field_named_date(self):
        """CT-13: we record commit date (%cI), not author date. Field name
        is `date` and it matches what `git log --since` filters on."""
        out = "\x1ex\t2026-04-28T12:34:56+00:00\tx@x.com\tx\n"
        commits = events._parse_git_log_numstat(out)
        assert commits[0]["date"] == "2026-04-28T12:34:56+00:00"


# ---------------------------------------------------------------------------
# T2 — walk_git_projects timeout formula + budget abort
# ---------------------------------------------------------------------------


class TestWalkGitProjects:
    def test_empty_roots_returns_empty_snapshot(self):
        out = events.walk_git_projects([], datetime.now(timezone.utc), 250)
        assert len(out) == 1
        assert out[0]["type"] == "git-snapshot"
        assert out[0].get("projects") == []
        assert out[0].get("skipped") == []

    def test_single_repo_captures_commits(self, tmp_path):
        repo = tmp_path / "r"
        _init_git_repo(repo)
        (repo / "a.txt").write_text("hello")
        subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "first"], check=True)
        since = datetime.now(timezone.utc) - timedelta(days=1)
        out = events.walk_git_projects([repo], since, 2000)
        proj = out[0]["projects"][0]
        assert proj["local_path"] == str(repo)
        assert len(proj["commits"]) == 1
        assert proj["commits"][0]["subject"] == "first"

    def test_repo_failure_folds_into_skipped(self, tmp_path):
        # Path that exists but isn't a git repo → walker returns it as
        # skipped with a non-zero git rc reason.
        not_repo = tmp_path / "x"
        not_repo.mkdir()
        # Pre-filter would normally drop this; we pass it directly to
        # walk_git_projects to exercise the failure path.
        out = events.walk_git_projects([not_repo], datetime.now(timezone.utc), 2000)
        # Either the repo's git log fails (rc != 0) AND lands in skipped, OR
        # we get an empty projects list with the failure forensic-trailed.
        assert out[0]["projects"] == [] or len(out[0]["skipped"]) >= 1

    def test_budget_abort_marks_pending_as_skipped(self, tmp_path, monkeypatch):
        """CT-6: as_completed pump budget enforcement. With a fake-slow
        per-repo walker, a tiny total budget should cancel pending work."""

        # Inject a slow worker so the budget actually trips.
        def _slow_walk(root, since_iso, timeout_ms):
            time.sleep(0.4)
            return None, "should-not-reach"

        monkeypatch.setattr(events, "_walk_one_repo", _slow_walk)
        roots = [tmp_path / f"r{i}" for i in range(3)]
        for r in roots:
            r.mkdir()
        out = events.walk_git_projects(roots, datetime.now(timezone.utc), 50)
        # Some or all repos should land in `skipped` with a budget_abort or
        # timeout reason. Empty projects list is the usual outcome.
        assert out[0]["projects"] == []
        assert len(out[0]["skipped"]) >= 1

    def test_per_repo_timeout_formula(self, monkeypatch):
        """A2: per-repo timeout = max(200, (budget * 8) // n_repos), cap 2000.
        Verify by inspecting what gets passed into _walk_one_repo."""
        captured = []

        def _capture(root, since_iso, timeout_ms):
            captured.append(timeout_ms)
            return None, None

        monkeypatch.setattr(events, "_walk_one_repo", _capture)
        roots = [Path(f"/tmp/r{i}") for i in range(30)]
        events.walk_git_projects(roots, datetime.now(timezone.utc), 250)
        # 30 repos, budget 250, workers 8 → (250 * 8) // 30 = 66 → floored to 200
        assert all(t == events.PER_REPO_TIMEOUT_FLOOR_MS for t in captured)

    def test_per_repo_timeout_caps_at_max(self, monkeypatch):
        captured = []

        def _capture(root, since_iso, timeout_ms):
            captured.append(timeout_ms)
            return None, None

        monkeypatch.setattr(events, "_walk_one_repo", _capture)
        roots = [Path("/tmp/r0")]
        # Single repo, generous budget → would compute huge per-repo, cap wins
        events.walk_git_projects(roots, datetime.now(timezone.utc), 100_000)
        assert all(t == events.PER_REPO_TIMEOUT_CAP_MS for t in captured)


# ---------------------------------------------------------------------------
# T3 — walk_session_metadata edge cases
# ---------------------------------------------------------------------------


class TestWalkSessionMetadata:
    def test_empty_claude_dir_returns_empty(self, tmp_path):
        since = datetime.now(timezone.utc) - timedelta(days=30)
        out = events.walk_session_metadata(tmp_path, since)
        assert out[0].get("projects") == []

    def test_non_jsonl_files_ignored(self, tmp_path):
        proj = tmp_path / "projects" / "-tmp-x"
        proj.mkdir(parents=True)
        (proj / "notes.md").write_text("not a session")
        (proj / "session.jsonl").write_text(json.dumps({"cwd": "/tmp/x", "type": "user"}) + "\n")
        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        assert len(out[0]["projects"]) == 1
        assert out[0]["projects"][0]["sessions"] == 1

    def test_total_kb_summed_across_files(self, tmp_path):
        proj = tmp_path / "projects" / "-tmp-x"
        proj.mkdir(parents=True)
        (proj / "a.jsonl").write_bytes(b"x" * 2048)
        (proj / "b.jsonl").write_bytes(b"y" * 2048)
        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        assert out[0]["projects"][0]["total_kb"] == 4

    def test_conductor_workspace_marked_ephemeral(self, tmp_path):
        proj = tmp_path / "projects" / "-Users-kb-conductor-workspaces-foo"
        proj.mkdir(parents=True)
        # cwd points at a path that matches the conductor pattern
        (proj / "session.jsonl").write_text(
            json.dumps({"cwd": "/Users/kb/conductor/workspaces/foo", "type": "user"}) + "\n"
        )
        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        assert out[0]["projects"][0]["ephemeral"] is True

    def test_non_conductor_workspace_not_ephemeral(self, tmp_path):
        proj = tmp_path / "projects" / "-tmp-x"
        proj.mkdir(parents=True)
        (proj / "session.jsonl").write_text(
            json.dumps({"cwd": "/Users/kb/code/foo", "type": "user"}) + "\n"
        )
        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        assert out[0]["projects"][0]["ephemeral"] is False

    def test_deadline_monotonic_aborts_per_project_loop(self, tmp_path):
        """Track 7B / Codex C4: ``deadline_monotonic`` aborts the scandir
        loop on a per-project boundary so a pathological project (large
        jsonls, no `cwd` field) can't blow past the wall-clock budget.

        Set up multiple project dirs and pass a deadline that's already
        expired — the loop must break before scanning any project."""
        for i in range(20):
            proj = tmp_path / "projects" / f"-tmp-{i}"
            proj.mkdir(parents=True)
            (proj / "session.jsonl").write_text(
                json.dumps({"cwd": f"/tmp/{i}", "type": "user"}) + "\n"
            )

        past_deadline = time.monotonic() - 1.0
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            deadline_monotonic=past_deadline,
        )
        # Loop broke before the first iteration completed any work.
        assert out[0]["projects"] == []

    def test_deadline_monotonic_none_scans_all_projects(self, tmp_path):
        """``deadline_monotonic=None`` is the no-deadline contract — the
        wall-clock check is bypassed and every project is scanned."""
        for i in range(5):
            proj = tmp_path / "projects" / f"-tmp-{i}"
            proj.mkdir(parents=True)
            (proj / "session.jsonl").write_text(
                json.dumps({"cwd": f"/tmp/{i}", "type": "user"}) + "\n"
            )
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            deadline_monotonic=None,
        )
        assert len(out[0]["projects"]) == 5

    def test_v2_full_inventory_ignores_since(self, tmp_path):
        """Group 8 cross-model #1 fix: sessions-snapshot is v=2 full inventory.

        Pre-v0.11.0 semantics filtered jsonls by ``mtime >= since_ts``, making
        each snapshot a delta. Aggregating across snapshots double-counted any
        session touched in multiple windows; latest-only-wins undercounted by
        dropping older windows. v=2 ignores ``since`` and counts EVERY jsonl,
        so the aggregator's latest-per-(device, claude_dir) rule produces a
        truthful point-in-time count.

        Pin: a jsonl with mtime older than `since` must STILL be counted.
        """
        import os

        proj = tmp_path / "projects" / "-tmp-old"
        proj.mkdir(parents=True)
        old_jsonl = proj / "old.jsonl"
        old_jsonl.write_text(json.dumps({"cwd": "/tmp/old", "type": "user"}) + "\n")
        # Make the file mtime far older than `since`.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
        os.utime(old_jsonl, (old_ts, old_ts))

        # `since` is just 1 day ago — pre-v0.11.0 (v=1) would have filtered
        # this file out.
        since = datetime.now(timezone.utc) - timedelta(days=1)
        out = events.walk_session_metadata(tmp_path, since)
        assert out[0]["v"] == events.EVENTS_SCHEMA_VERSION
        assert out[0]["v"] == 2
        assert len(out[0]["projects"]) == 1
        assert out[0]["projects"][0]["sessions"] == 1, (
            "v=2 full-inventory must count every jsonl regardless of mtime"
        )

    def test_pathological_session_walk_no_cwd_anywhere(self, tmp_path):
        """T2: walks must not crash when no project has a `cwd` field —
        decoded path falls back to the encoded directory name."""
        proj = tmp_path / "projects" / "-tmp-no-cwd"
        proj.mkdir(parents=True)
        (proj / "session.jsonl").write_text(
            # Many lines, none with a `cwd` field.
            "\n".join(json.dumps({"type": "user", "msg": f"x{i}"}) for i in range(500))
        )
        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        assert len(out[0]["projects"]) == 1
        # No crash, ephemeral derived from the encoded fallback name.
        assert out[0]["projects"][0]["ephemeral"] is False

    def test_source_root_field_emitted(self, tmp_path):
        """Group 8 hotfix #4: every emitted SessionMetadata carries a
        ``source_root`` field equal to the str of the claude_dir argument
        passed to ``walk_session_metadata``. The aggregator keys
        ``(device, source_root, claude_dir)`` to avoid silent overwrite
        when two configured ``type: claude`` source roots share an encoded
        project name."""
        proj = tmp_path / "projects" / "-Users-kb-Documents-foo"
        proj.mkdir(parents=True)
        (proj / "session.jsonl").write_text(
            json.dumps({"cwd": "/Users/kb/Documents/foo", "type": "user"}) + "\n"
        )
        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        assert len(out[0]["projects"]) == 1
        assert out[0]["projects"][0]["source_root"] == str(tmp_path)


# ---------------------------------------------------------------------------
# T1 — last_push_ts (cursor derivation)
# ---------------------------------------------------------------------------


class TestLastPushTs:
    def test_first_run_returns_now_minus_30d(self, tmp_path):
        ts = events.last_push_ts(tmp_path / "events", "dev-a")
        now = datetime.now(timezone.utc)
        delta = (now - ts).total_seconds()
        # Within a few seconds of now-30d
        assert abs(delta - 30 * 86400) < 10

    def test_finds_latest_mm_push_in_today(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        today = datetime.now(timezone.utc).date().isoformat()
        path = events_dir / f"dev-a-{today}.jsonl"
        # Two mm-push events; the LATER one wins
        early = "2026-04-28T10:00:00+00:00"
        later = "2026-04-28T15:00:00+00:00"
        path.write_text(
            json.dumps({"v": 1, "type": "mm-push", "ts": early, "device": "dev-a"})
            + "\n"
            + json.dumps({"v": 1, "type": "git-snapshot", "ts": later, "device": "dev-a"})
            + "\n"
            + json.dumps({"v": 1, "type": "mm-push", "ts": later, "device": "dev-a"})
            + "\n"
        )
        ts = events.last_push_ts(events_dir, "dev-a")
        assert ts.isoformat() == later

    def test_walks_backward_when_today_empty(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        path = events_dir / f"dev-a-{yesterday}.jsonl"
        ts_iso = "2026-04-27T08:00:00+00:00"
        path.write_text(
            json.dumps({"v": 1, "type": "mm-push", "ts": ts_iso, "device": "dev-a"}) + "\n"
        )
        ts = events.last_push_ts(events_dir, "dev-a")
        assert ts.isoformat() == ts_iso

    def test_other_devices_files_ignored(self, tmp_path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        today = datetime.now(timezone.utc).date().isoformat()
        # Another device's file — must NOT influence dev-a's cursor
        other = events_dir / f"dev-b-{today}.jsonl"
        other.write_text(
            json.dumps(
                {
                    "v": 1,
                    "type": "mm-push",
                    "ts": "2026-04-28T15:00:00+00:00",
                    "device": "dev-b",
                }
            )
            + "\n"
        )
        ts = events.last_push_ts(events_dir, "dev-a")
        # No mm-push for dev-a → default
        delta = (datetime.now(timezone.utc) - ts).total_seconds()
        assert abs(delta - 30 * 86400) < 10


# ---------------------------------------------------------------------------
# T0/T3 — write_push_event: append round-trip, mode 0600, single-flock
# ---------------------------------------------------------------------------


class TestWritePushEvent:
    def test_append_round_trip(self, tmp_path):
        events_dir = tmp_path / "events"
        push = events.make_mm_push_event(device="dev-a", mm_version="0.11.0")
        snap: events.GitSnapshot = {
            "v": 1,
            "type": "git-snapshot",
            "ts": "2026-04-28T10:00:00+00:00",
            "device": "dev-a",
            "projects": [],
            "skipped": [],
        }
        # CT-4: mm-push event LAST
        events.write_push_event(events_dir, "dev-a", [snap, push])
        today = datetime.now(timezone.utc).date().isoformat()
        path = events_dir / f"dev-a-{today}.jsonl"
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
        # mm-push is the LAST line (CT-4 invariant)
        last = json.loads(lines[-1])
        assert last["type"] == "mm-push"

    def test_file_mode_0600(self, tmp_path):
        events_dir = tmp_path / "events"
        push = events.make_mm_push_event(device="dev-a", mm_version="0.11.0")
        events.write_push_event(events_dir, "dev-a", [push])
        today = datetime.now(timezone.utc).date().isoformat()
        path = events_dir / f"dev-a-{today}.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_per_day_naming(self, tmp_path):
        events_dir = tmp_path / "events"
        events.write_push_event(
            events_dir,
            "device-uuid-123",
            [events.make_mm_push_event(device="device-uuid-123", mm_version="0.11.0")],
        )
        today = datetime.now(timezone.utc).date().isoformat()
        assert (events_dir / f"device-uuid-123-{today}.jsonl").exists()

    def test_mkdir_parents_when_events_dir_missing(self, tmp_path):
        """CT-12: write_push_event creates events_dir if it doesn't exist."""
        events_dir = tmp_path / "deeply" / "nested" / "events"
        assert not events_dir.exists()
        events.write_push_event(
            events_dir,
            "dev-a",
            [events.make_mm_push_event(device="dev-a", mm_version="0.11.0")],
        )
        assert events_dir.exists()

    def test_empty_events_list_no_op(self, tmp_path):
        events_dir = tmp_path / "events"
        events.write_push_event(events_dir, "dev-a", [])
        # No file should have been created
        today = datetime.now(timezone.utc).date().isoformat()
        path = events_dir / f"dev-a-{today}.jsonl"
        assert not path.exists()

    def test_concurrent_appends_no_torn_writes(self, tmp_path):
        """T0: events file flock under simulated concurrent autopush.
        N threads each append M unique events; total line count is N*M."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        N_THREADS = 4
        N_EVENTS = 25

        def _writer(tid: int):
            evs = [
                events.make_mm_push_event(
                    device=f"d-{tid}",
                    mm_version="0.11.0",
                    ts=datetime.now(timezone.utc),
                )
                for _ in range(N_EVENTS)
            ]
            events.write_push_event(events_dir, "shared-dev", evs)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        today = datetime.now(timezone.utc).date().isoformat()
        path = events_dir / f"shared-dev-{today}.jsonl"
        lines = path.read_text().strip().split("\n")
        assert len(lines) == N_THREADS * N_EVENTS
        # Every line is parseable JSON (no torn writes)
        for ln in lines:
            obj = json.loads(ln)
            assert obj["type"] == "mm-push"


# ---------------------------------------------------------------------------
# T2 — budget abort + cursor non-advancement (CT-4 transactional pin)
# ---------------------------------------------------------------------------


class TestMakeMmPushEventInternalFilter:
    """Track 7B / Codex C7: ``MM_INTERNAL_SOURCE_NAMES`` (today: ``mm-events``)
    is mm-owned infrastructure, not user-meaningful fleet activity. The
    retro skill enumerates user-facing sources only — filter at
    ``make_mm_push_event`` so the event row never carries the internal
    name."""

    def test_filters_mm_events_from_sources_list(self):
        ev = events.make_mm_push_event(
            device="dev-a",
            mm_version="0.11.0",
            sources=["claude", "mm-events", "gstack"],
        )
        assert "mm-events" not in ev["sources"]
        assert ev["sources"] == ["claude", "gstack"]

    def test_empty_sources_stays_empty(self):
        ev = events.make_mm_push_event(device="dev-a", mm_version="0.11.0")
        assert ev["sources"] == []

    def test_only_internal_names_yields_empty(self):
        ev = events.make_mm_push_event(device="dev-a", mm_version="0.11.0", sources=["mm-events"])
        assert ev["sources"] == []

    def test_user_only_sources_pass_through(self):
        ev = events.make_mm_push_event(
            device="dev-a", mm_version="0.11.0", sources=["claude", "gstack"]
        )
        assert ev["sources"] == ["claude", "gstack"]


class TestWriteOrderTransactionalPin:
    def test_partial_write_does_not_advance_cursor(self, tmp_path):
        """CT-4: mm-push event MUST be LAST. If the caller crashes between
        writing git-snapshot and mm-push, cursor stays at the prior value
        and the next push re-walks (deduped at retro render via (remote,
        sha)). We pin this by writing only the git-snapshot row and
        verifying last_push_ts returns the default (now-30d), NOT a
        cursor advancement."""
        events_dir = tmp_path / "events"
        events.write_push_event(
            events_dir,
            "dev-a",
            [
                # ONLY a git-snapshot — no mm-push (simulates partial write)
                {
                    "v": 1,
                    "type": "git-snapshot",
                    "ts": "2026-04-28T10:00:00+00:00",
                    "device": "dev-a",
                    "projects": [],
                    "skipped": [],
                },
            ],
        )
        ts = events.last_push_ts(events_dir, "dev-a")
        # Cursor stayed at default (now-30d) — partial git-snapshot doesn't move it
        delta = (datetime.now(timezone.utc) - ts).total_seconds()
        assert abs(delta - 30 * 86400) < 10
