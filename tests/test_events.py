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

    def test_budget_abort_emits_each_completed_repo_exactly_once(self, tmp_path, monkeypatch):
        """v0.12.16 T4: on budget abort, a repo that finished before the
        timeout must appear ONCE.

        Pre-fix, block 1 drained `as_completed` and appended each result,
        then the `FuturesTimeoutError` handler iterated ALL of
        `futures.items()` and re-appended every `fut.done()` — so every
        completed repo's full commit list was serialised twice into the
        git-snapshot row, then gzipped, encrypted, uploaded, and replicated
        to every peer. Measured pre-fix with 4 roots / 2 slow: 4 rows,
        2 unique. The card survived it only because `aggregate_git` dedups
        on (canonical_remote, sha).

        `test_budget_abort_marks_pending_as_skipped` above cannot catch it:
        it makes ALL repos slow, so `projects == []` and there is nothing to
        double-collect. This one needs a MIXED fast/slow set.
        """

        def _mixed_walk(root, since_iso, timeout_ms):
            if root.name in ("slow0", "slow1"):
                time.sleep(1.5)
            return {"remote": "", "local_path": str(root), "commits": []}, None

        monkeypatch.setattr(events, "_walk_one_repo", _mixed_walk)
        roots = []
        for name in ("fast0", "fast1", "slow0", "slow1"):
            r = tmp_path / name
            r.mkdir()
            roots.append(r)

        out = events.walk_git_projects(roots, datetime.now(timezone.utc), 300)
        paths = [p["local_path"] for p in out[0]["projects"]]
        assert len(paths) == len(set(paths)), f"repos double-collected: {sorted(paths)}"
        assert {Path(p).name for p in paths} == {"fast0", "fast1"}
        # budget_abort accounting must survive the dedup — the two except
        # sites are NOT interchangeable and only the pump handler marks it.
        assert {s["reason"] for s in out[0]["skipped"]} == {"budget_abort"}

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

    def test_no_cwd_anywhere_scans_the_project_exactly_once(self, tmp_path, monkeypatch):
        """v0.12.16 T1: the cwd read is hoisted OUT of the per-file loop.

        Pre-fix, `_scan_one_project` called `_read_cwd_from_latest_jsonl`
        inside the per-file loop guarded by `if cwd is None`. The helper
        takes the PROJECT dir, so when no jsonl carries a `cwd` the guard
        never flipped and the helper rescanned the whole directory once per
        jsonl: N calls x N files. Measured 400 opens for 20 files, and 13.2s
        for a single 300-file project against a 250ms budget.

        `test_pathological_session_walk_no_cwd_anywhere` above cannot catch
        this: it uses ONE jsonl, so N=1 and the quadratic collapses.
        """
        proj = tmp_path / "projects" / "-tmp-no-cwd-many"
        proj.mkdir(parents=True)
        body = "\n".join(json.dumps({"type": "user"}) for _ in range(50)) + "\n"
        for i in range(20):
            (proj / f"s{i}.jsonl").write_text(body)

        calls: list[Path] = []
        real = events._read_cwd_from_latest_jsonl
        monkeypatch.setattr(
            events,
            "_read_cwd_from_latest_jsonl",
            lambda d: (calls.append(d), real(d))[1],
        )
        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        assert len(calls) == 1, f"cwd scanned {len(calls)}x for one project (pre-fix: 20)"
        assert out[0]["projects"][0]["sessions"] == 20

    def test_bad_utf8_before_cwd_does_not_kill_the_walk(self, tmp_path):
        """v0.12.16 T2: one invalid UTF-8 byte must cost its own line, not
        the events tail.

        Pre-fix `_read_cwd_from_latest_jsonl` read text-mode and caught only
        OSError, so UnicodeDecodeError (a ValueError) escaped through
        `_scan_one_project` and `walk_session_metadata` into
        `_run_events_tail`'s wrapper — the whole tail was lost on every push.

        Bad byte FIRST, valid `cwd` line SECOND. The reverse ordering passes
        trivially post-fix (the helper returns before reaching the bad byte)
        and would prove only "didn't crash", not per-line tolerance. Note
        text mode decoded in ~8KB chunks, so even a `cwd` on line 1 did not
        protect against a bad byte a few lines later.
        """
        proj = tmp_path / "projects" / "-tmp-badbyte"
        proj.mkdir(parents=True)
        (proj / "session.jsonl").write_bytes(
            b'{"type":"user","note":"\xe9 invalid utf-8"}\n'
            + json.dumps({"cwd": "/tmp/real/path", "type": "user"}).encode()
            + b"\n"
        )
        # Assert the reader CONTINUED PAST the bad byte and found the cwd.
        # "no exception + 1 project" is not enough: mutation-tested during
        # /review by swapping the per-line `continue` for a per-file `break`
        # (i.e. abandon the file on first bad byte) — all 84 events tests
        # still passed. Per-line tolerance is the contract; pin the value.
        assert events._read_cwd_from_latest_jsonl(proj) == "/tmp/real/path", (
            "reader abandoned the file on the bad byte instead of skipping one line"
        )
        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        assert len(out[0]["projects"]) == 1
        assert out[0]["projects"][0]["sessions"] == 1

    def test_bad_utf8_project_does_not_cost_the_other_projects(self, tmp_path):
        """A corrupt project must not take healthy siblings down with it.

        Pre-fix this raised out of the whole walk, so EVERY project's row
        was lost — not just the corrupt one's.
        """
        bad = tmp_path / "projects" / "-tmp-bad"
        bad.mkdir(parents=True)
        (bad / "s.jsonl").write_bytes(b'{"type":"user","x":"\xff\xfe"}\n')
        good = tmp_path / "projects" / "-tmp-good"
        good.mkdir(parents=True)
        (good / "s.jsonl").write_text(json.dumps({"cwd": "/tmp/good", "type": "user"}) + "\n")

        out = events.walk_session_metadata(
            tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
        )
        names = {p["claude_dir"] for p in out[0]["projects"]}
        assert names == {"-tmp-bad", "-tmp-good"}

    def test_oversize_line_is_bounded_not_slurped(self, tmp_path, capsys):
        """The binary port must route through `token_usage.iter_bounded_lines`
        so a pathological single line is capped at MAX_JSONL_LINE_BYTES
        rather than pulled whole into memory. A naive `open(path, "rb")` +
        `for line in fp:` port would let Python extend its buffer to
        newline-or-EOF — the exact OOM that primitive exists to prevent on
        this corpus.

        Asserting only "the cwd is still found" would be a pin that proves
        NOTHING: the pre-fix text-mode reader also slurped the giant line,
        failed json.loads on it, and moved on to find the cwd. Verified —
        that weaker assertion passed against pre-fix code. The load-bearing
        observable is the oversize NOTICE, which only the bounded reader
        emits, and which also names this call site rather than "token
        walker". `_WARNED_OVERSIZE_PATHS` is reset per-test by conftest's
        `_isolate_token_cache`, so the warn-once state can't leak in.
        """
        from mind_meld import token_usage

        proj = tmp_path / "projects" / "-tmp-oversize"
        proj.mkdir(parents=True)
        cap = token_usage.MAX_JSONL_LINE_BYTES
        (proj / "s.jsonl").write_bytes(
            b'{"junk":"'
            + (b"x" * (cap + 1024))
            + b'"}\n'
            + json.dumps({"cwd": "/tmp/after/oversize"}).encode()
            + b"\n"
        )
        assert events._read_cwd_from_latest_jsonl(proj) == "/tmp/after/oversize"
        err = capsys.readouterr().err
        assert "skipping oversize line" in err, "bounded reader not in the cwd read path"
        # The notice is deduped by PATH only, so in production the label is
        # whichever site reached the file first. Here conftest's
        # `_isolate_token_cache` resets `_WARNED_OVERSIZE_PATHS` per test and
        # only this reader runs, so the label is deterministic.
        assert "session cwd reader" in err, "oversize notice misattributes the call site"

    def test_unterminated_final_line_is_still_read(self, tmp_path):
        """v0.12.16 REGRESSION PIN: a complete record with no trailing
        newline must still be parsed by this one-shot reader.

        `iter_bounded_lines` defaults to treating a trailing chunk with no
        newline as a PARTIAL WRITE and discarding it — correct for
        `walk_jsonl_segment`, which persists a resume offset and re-reads it
        next push. This reader has no next push. Porting it to the shared
        primitive without `yield_final_partial=True` made it return None for
        a session whose only line wasn't newline-terminated yet, where the
        old text-mode reader returned the cwd. Caught by Codex adversarial
        review during /review, after the first fix had already landed.
        """
        proj = tmp_path / "projects" / "-tmp-unterminated"
        proj.mkdir(parents=True)
        (proj / "session.jsonl").write_bytes(
            json.dumps({"cwd": "/tmp/mid/write", "type": "user"}).encode()  # no \n
        )
        assert events._read_cwd_from_latest_jsonl(proj) == "/tmp/mid/write"

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
    def test_bad_utf8_preserves_the_cursor(self, tmp_path):
        """v0.12.16 T3: a bad byte must cost its line, NOT the cursor.

        Asserting "doesn't raise" is not enough here and would be a pin that
        proves nothing: a bare `return None` also doesn't raise, and it
        silently rewinds the cursor to now - INITIAL_CURSOR_LOOKBACK_DAYS,
        making every subsequent push re-walk 30 days of git history forever.
        So assert the timestamp comes BACK.

        Bad byte first, valid mm-push line second. These files live under the
        synced mm-events source, so their bytes can arrive via the pull apply
        path and `merge.merge_jsonl`, not only from this device's writer.
        """
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        day = datetime.now(timezone.utc).date().isoformat()
        (events_dir / f"dev-a-{day}.jsonl").write_bytes(
            b'{"type":"mm-push","note":"\xe9 invalid","ts":"2020-01-01T00:00:00+00:00"}\n'
            + json.dumps({"type": "mm-push", "ts": "2026-08-14T12:00:00+00:00"}).encode()
            + b"\n"
        )
        ts = events.last_push_ts(events_dir, "dev-a")
        assert ts == datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc), (
            "bad byte rewound the cursor instead of skipping one line"
        )

    def test_oversize_line_is_bounded(self, tmp_path, monkeypatch, capsys):
        """The cursor reader goes through `iter_bounded_lines` too.

        These files live under the SYNCED mm-events source, so their bytes
        can arrive from a peer via the pull apply path. A bare
        `for raw in f` would let one oversized line be slurped whole on
        every push.

        Assert the NOTICE, not just the returned timestamp. The unbounded
        reader also returns the right timestamp — it slurps the giant line,
        fails `json.loads` on it, and moves on — so a value-only assertion
        passes either way. Verified: that weaker form passed against the
        unbounded implementation. The notice is emitted only from the
        bounded primitive, so it is the observable that proves the bound.
        """
        from mind_meld import token_usage

        monkeypatch.setattr(token_usage, "MAX_JSONL_LINE_BYTES", 512)
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        day = datetime.now(timezone.utc).date().isoformat()
        (events_dir / f"dev-a-{day}.jsonl").write_bytes(
            b'{"junk":"'
            + (b"x" * 4096)
            + b'"}\n'
            + json.dumps({"type": "mm-push", "ts": "2026-08-14T12:00:00+00:00"}).encode()
            + b"\n"
        )
        assert events.last_push_ts(events_dir, "dev-a") == datetime(
            2026, 8, 14, 12, 0, tzinfo=timezone.utc
        )
        err = capsys.readouterr().err
        assert "skipping oversize line" in err, "cursor reader is not bounded"
        assert "events cursor reader" in err

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


class TestMmPushEventLocalEmails:
    """v0.11.17 — ``local_emails`` field on ``MmPushEvent``. Aggregator
    unions across peers' rows to build a fleet-wide author-email trust
    set, replacing the per-machine gather that produced different retros
    on each machine.

    Forward-compat: caller passing ``None`` (or omitting) yields an event
    row with no ``local_emails`` key — wire-compatible with pre-v0.11.17
    peers reading it."""

    def test_emails_present_when_supplied(self):
        ev = events.make_mm_push_event(
            device="dev-a",
            mm_version="0.11.17",
            local_emails=["a@example.com", "b@example.com"],
        )
        assert ev["local_emails"] == ["a@example.com", "b@example.com"]

    def test_field_omitted_when_none(self):
        """Absent ``local_emails`` arg → key NOT in the event row.
        Distinguishable from explicit empty-list at the aggregator."""
        ev = events.make_mm_push_event(device="dev-a", mm_version="0.11.17")
        assert "local_emails" not in ev

    def test_empty_list_emitted_explicitly(self):
        """Empty list is preserved on the wire — distinguishable from
        absent. Pre-v0.11.17 peers omit the field entirely; v0.11.17+
        peers with cold cache emit ``[]``. The aggregator can fall back
        to local gather for the former, take the latter at face value."""
        ev = events.make_mm_push_event(device="dev-a", mm_version="0.11.17", local_emails=[])
        assert ev["local_emails"] == []

    def test_emails_jsonl_round_trip(self, tmp_path):
        """Write event → read jsonl → parse → assert ``local_emails``
        survives. Pins the wire format for forensic comparison."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        push = events.make_mm_push_event(
            device="dev-a",
            mm_version="0.11.17",
            local_emails=["kb@example.com", "kb-work@example.com"],
        )
        events.write_push_event(events_dir, "dev-a", [push])
        files = list(events_dir.glob("*.jsonl"))
        assert len(files) == 1
        line = files[0].read_text(encoding="utf-8").strip()
        parsed = json.loads(line)
        assert parsed["type"] == "mm-push"
        assert parsed["local_emails"] == ["kb@example.com", "kb-work@example.com"]


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


# ---------------------------------------------------------------------------
# Token aggregation hook (v0.11.14+)
# ---------------------------------------------------------------------------


class TestTokenAggregationHook:
    """Pin events.py's wiring into token_usage.get_or_compute.

    Subagent contribution rule: tokens-only. Subagent jsonls MUST NOT
    bump sessions/total_kb/last_session_at — those preserve parent-only
    semantics."""

    def _write_session_jsonl(self, path, msg_id="m1", model="claude-opus-4-7"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "message": {
                        "id": msg_id,
                        "role": "assistant",
                        "model": model,
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 1000,
                            "output_tokens": 50,
                        },
                    },
                    "timestamp": "2026-05-01T12:00:00.000Z",
                }
            )
            + "\n"
        )

    def test_no_token_cache_means_no_tokens_field(self, tmp_path):
        """v0.12.4 invariant (regression pin): cold-cache path
        (`token_cache_files=None`) must omit BOTH `tokens_by_day` AND
        `skills_by_day`. The `skills_by_day` absence is load-bearing —
        the rejected Track 11B Option B fix (drop the gate, always set
        `meta["skills_by_day"] = {}`) would let latest-snapshot-wins at
        `aggregator.aggregate_sessions` silently overwrite a warm T1
        snapshot's populated skills with a cold T2 synthetic `{}`. See
        docs/invariants/events-retro.md "Why not always set" section.
        """
        proj = tmp_path / "projects" / "-tmp-proj"
        proj.mkdir(parents=True)
        self._write_session_jsonl(proj / "session.jsonl")
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            token_cache_files=None,
        )
        meta = out[0]["projects"][0]
        assert "tokens_by_day" not in meta
        assert "skills_by_day" not in meta

    def test_token_cache_populates_tokens_by_day(self, tmp_path):
        proj = tmp_path / "projects" / "-tmp-proj"
        proj.mkdir(parents=True)
        self._write_session_jsonl(proj / "session.jsonl")
        cache_files: dict = {}
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            token_cache_files=cache_files,
        )
        meta = out[0]["projects"][0]
        assert "tokens_by_day" in meta
        assert "2026-05-01" in meta["tokens_by_day"]
        assert meta["tokens_by_day"]["2026-05-01"]["input"] == 100

    def test_subagent_jsonls_contribute_tokens_not_sessions(self, tmp_path):
        proj = tmp_path / "projects" / "-tmp-proj"
        proj.mkdir(parents=True)
        # Parent jsonl
        self._write_session_jsonl(proj / "session.jsonl", msg_id="parent")
        # Subagent jsonls under <session-uuid>/subagents/
        sub_dir = proj / "abc-uuid" / "subagents"
        sub_dir.mkdir(parents=True)
        self._write_session_jsonl(sub_dir / "agent-1.jsonl", msg_id="sub-1")
        self._write_session_jsonl(sub_dir / "agent-2.jsonl", msg_id="sub-2")

        cache_files: dict = {}
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            token_cache_files=cache_files,
        )
        meta = out[0]["projects"][0]
        # sessions counts ONLY parent jsonls — NOT subagents.
        assert meta["sessions"] == 1
        # tokens_by_day INCLUDES all 3 messages (parent + 2 subagents).
        # Each is 100 input tokens, deduped by id (different ids), so 300 total.
        assert meta["tokens_by_day"]["2026-05-01"]["input"] == 300

    def test_subagent_only_dir_yields_no_session(self, tmp_path):
        """A project dir with ONLY subagent jsonls and no parent jsonl
        should still return None — `sessions == 0` triggers the existing
        skip path, preserving parent-only semantics for the meta."""
        proj = tmp_path / "projects" / "-tmp-proj"
        proj.mkdir(parents=True)
        sub_dir = proj / "abc-uuid" / "subagents"
        sub_dir.mkdir(parents=True)
        self._write_session_jsonl(sub_dir / "agent-1.jsonl", msg_id="orphan")
        cache_files: dict = {}
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            token_cache_files=cache_files,
        )
        # No sessions (no parent jsonls) → no project rendered.
        assert out[0]["projects"] == []


class TestSkillsAggregationHook:
    """Pin events.py's wiring of skill detection (v0.11.27 fleet-skill plan,
    tests #8 / #9 / #10 from /plan-eng-review 2026-05-06).

    Same parent-project attribution as tokens. KEY-PRESENT-VALUE-EMPTY is
    the discriminator the aggregator's mixed-fleet flag relies on.
    """

    def _write_session_jsonl_with_skills(self, path, *, skills, msg_id="m1"):
        path.parent.mkdir(parents=True, exist_ok=True)
        content = [
            {
                "type": "tool_use",
                "id": f"toolu_{i}",
                "name": "Skill",
                "input": {"skill": s, "args": ""},
            }
            for i, s in enumerate(skills)
        ]
        path.write_text(
            json.dumps(
                {
                    "message": {
                        "id": msg_id,
                        "role": "assistant",
                        "model": "claude-opus-4-7",
                        "content": content,
                        "usage": {
                            "input_tokens": 1,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 1,
                        },
                    },
                    "timestamp": "2026-05-01T12:00:00.000Z",
                }
            )
            + "\n"
        )

    def _write_session_jsonl_no_skills(self, path, msg_id="m1"):
        """A normal token-only assistant message — no Skill blocks."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "message": {
                        "id": msg_id,
                        "role": "assistant",
                        "model": "claude-opus-4-7",
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 50,
                        },
                    },
                    "timestamp": "2026-05-01T12:00:00.000Z",
                }
            )
            + "\n"
        )

    def test_skills_by_day_populated_when_blocks_present(self, tmp_path):
        """Plan test #8: parent jsonl has 2 Skill blocks → meta has
        ``skills_by_day`` populated."""
        proj = tmp_path / "projects" / "-tmp-proj"
        proj.mkdir(parents=True)
        self._write_session_jsonl_with_skills(proj / "session.jsonl", skills=["ship", "review"])
        cache_files: dict = {}
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            token_cache_files=cache_files,
        )
        meta = out[0]["projects"][0]
        assert "skills_by_day" in meta
        assert meta["skills_by_day"] == {"2026-05-01": {"ship": 1, "review": 1}}

    def test_skills_by_day_empty_dict_when_no_skill_blocks(self, tmp_path):
        """Plan test #9 (D4 correctness gate): project with sessions but no
        Skill blocks emits ``skills_by_day == {}`` — KEY PRESENT, value
        empty. The aggregator's mixed-fleet flag uses ``"skills_by_day"
        not in proj`` to discriminate pre-v0.11.27 peers from no-skill-
        activity sessions; this test pins that discriminator."""
        proj = tmp_path / "projects" / "-tmp-proj"
        proj.mkdir(parents=True)
        self._write_session_jsonl_no_skills(proj / "session.jsonl")
        cache_files: dict = {}
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            token_cache_files=cache_files,
        )
        meta = out[0]["projects"][0]
        assert "skills_by_day" in meta  # KEY PRESENT
        assert meta["skills_by_day"] == {}  # VALUE EMPTY

    def test_subagent_skill_attribution_to_parent_project(self, tmp_path):
        """Plan test #10 (D5#3): parent jsonl has 2 Skill blocks; subagent
        jsonl at ``<encoded>/<uuid>/subagents/agent-X.jsonl`` has 1 Skill
        block. ``_scan_one_project`` must attribute all 3 invocations to
        the parent project's ``skills_by_day`` (mirrors token attribution
        rule). Refactor footgun gate: if a future structural change in
        ``_aggregate_jsonl_views_for_project`` breaks subagent attribution
        for skills, this test fails."""
        proj = tmp_path / "projects" / "-tmp-proj"
        proj.mkdir(parents=True)
        # Parent: 2 Skill blocks, 1 message.
        self._write_session_jsonl_with_skills(
            proj / "parent-session.jsonl",
            skills=["ship", "plan-eng-review"],
            msg_id="parent_m1",
        )
        # Subagent: 1 Skill block, different message.id (must not dedup
        # against parent).
        sub_dir = proj / "parent-uuid" / "subagents"
        sub_dir.mkdir(parents=True)
        self._write_session_jsonl_with_skills(
            sub_dir / "agent-X.jsonl",
            skills=["review"],
            msg_id="sub_m1",
        )
        cache_files: dict = {}
        out = events.walk_session_metadata(
            tmp_path,
            datetime.now(timezone.utc) - timedelta(days=30),
            token_cache_files=cache_files,
        )
        meta = out[0]["projects"][0]
        # All 3 invocations attributed to the parent project's bucket.
        assert meta["skills_by_day"] == {
            "2026-05-01": {"ship": 1, "plan-eng-review": 1, "review": 1}
        }
        # Sessions count is parent-only (subagent doesn't bump).
        assert meta["sessions"] == 1
