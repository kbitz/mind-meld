"""Tests for ``mind_meld.identity`` — the cached, fleet-shared author email
trust set introduced in v0.11.17.

The module replaces the per-machine ``aggregator.gather_author_emails`` walk
that produced different retros on each fleet machine. Push tail emits the
locally-known emails into the ``mm-push`` event row's ``local_emails`` field;
the aggregator unions across peers at retro time. Both use the cache here
to share state, with a 7-day TTL and explicit ``mm refresh-identity`` knob.

Coverage groups:

* ``TestCacheLifecycle`` — fresh / fresh-fast-path / stale / corrupt / TTL.
* ``TestGatherSources`` — global / per-repo / config / gh-noreply, each
  source's success + failure path.
* ``TestRefreshSemantics`` — force=True vs False, notice-emit-on-stale.
* ``TestPersistence`` — round-trip + lowercase + dedup invariants.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mind_meld import events, identity, lockedjson
from tests.test_events import (
    _git_env_repos,
    _init_git_repo,
    _make_git_worktree,
    _recapture_test_sources,
    _stub_discovery_to,
)

# ---------------------------------------------------------------------------
# Helpers — fake subprocess.run dispatch by command prefix.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _stub_subprocess(monkeypatch, handlers: dict) -> None:
    """Build a fake ``subprocess.run`` that dispatches by cmd prefix.

    ``handlers`` maps a tuple cmd prefix to either ``(rc, stdout)`` or an
    exception class to raise. Unmatched returns rc=1 / empty.
    """
    import subprocess as _subprocess

    def fake_run(cmd, **_kw):
        for prefix, response in handlers.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                if isinstance(response, type) and issubclass(response, Exception):
                    raise response("simulated")
                rc, out = response
                return _FakeResult(rc, out)
        return _FakeResult(1, "")

    monkeypatch.setattr(_subprocess, "run", fake_run)


def _stub_repos(monkeypatch, roots: list[Path]) -> None:
    """Stub discover_git_roots + load_config + the per-repo helpers so the
    gatherer operates on synthetic paths."""
    from mind_meld import config as config_module
    from mind_meld import events as events_module

    monkeypatch.setattr(events_module, "discover_git_roots", lambda _cfg: (roots, []))
    monkeypatch.setattr(config_module, "load_config", lambda _p: {})


def _isoformat(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Cache lifecycle.
# ---------------------------------------------------------------------------


class TestCacheLifecycle:
    def test_cold_cache_triggers_refresh_and_emits_notice(self, monkeypatch, capsys):
        """Cold cache + allow_refresh=True → emit single notice + refresh
        synchronously. This is the D1 contract from /plan-eng-review:
        synchronous refresh, no autopush-budget contortions, one-off
        slow path is acceptable."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.gather_local_identities(allow_refresh=True)
        assert "user@example.com" in emails
        captured = capsys.readouterr()
        assert "refreshing identity cache (one-off)" in captured.err

    def test_fresh_cache_fast_path_no_notice(self, monkeypatch, capsys):
        """A fresh cache (refreshed_at within TTL) returns the cached
        list with NO subprocess calls and NO notice emitted."""
        # Pre-populate cache with a fresh refresh.
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "refreshed_at": _isoformat(datetime.now(timezone.utc)),
                    "emails": ["cached@example.com"],
                }
            )
        )
        # Fail any subprocess call so a refresh attempt would be visible.
        _stub_subprocess(monkeypatch, {("git",): SystemError, ("gh",): SystemError})

        emails = identity.gather_local_identities(allow_refresh=True)
        assert emails == ["cached@example.com"]
        captured = capsys.readouterr()
        assert "refreshing identity cache" not in captured.err

    def test_stale_cache_triggers_refresh(self, monkeypatch, capsys):
        """Cache with refreshed_at older than TTL_SECONDS triggers a
        refresh on next read."""
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=identity.TTL_SECONDS + 60)
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "refreshed_at": _isoformat(old_ts),
                    "emails": ["old@example.com"],
                }
            )
        )
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "fresh@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.gather_local_identities(allow_refresh=True)
        assert "fresh@example.com" in emails
        assert "old@example.com" not in emails  # stale cache replaced
        captured = capsys.readouterr()
        assert "refreshing identity cache (one-off)" in captured.err

    def test_corrupt_cache_silently_rebuilds(self, monkeypatch):
        """**Critical gap pin (failure mode #1).** A cache file with
        garbage JSON / wrong version / non-list emails MUST silently
        rebuild on next read. Visible-failure contract for retro
        rendering: degradation, not crash."""
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text("{not json at all]")
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.gather_local_identities(allow_refresh=True)
        assert emails == ["user@example.com"]

    def test_wrong_version_in_cache_rebuilds(self, monkeypatch):
        """A different version is treated as stale; a complete gather replaces it."""
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": 999,
                    "refreshed_at": _isoformat(datetime.now(timezone.utc)),
                    "emails": ["should-not-appear@example.com"],
                }
            )
        )
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.gather_local_identities(allow_refresh=True)
        assert "user@example.com" in emails
        assert "should-not-appear@example.com" not in emails

    def test_disable_refresh_returns_cached_or_empty(self, monkeypatch):
        """``allow_refresh=False`` returns whatever is on disk without
        a refresh — even when the cache is stale or empty. Used by any
        caller that explicitly opts out of the slow-path notice."""
        # Cold cache, refresh disabled → empty.
        emails = identity.gather_local_identities(allow_refresh=False)
        assert emails == []


# ---------------------------------------------------------------------------
# Gather sources.
# ---------------------------------------------------------------------------


class TestGatherSources:
    def test_global_email_in_set(self, monkeypatch):
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "user@example.com" in emails

    def test_per_repo_overrides_unioned(self, monkeypatch):
        _stub_repos(monkeypatch, [Path("/fake/repo-a"), Path("/fake/repo-b")])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("git", "-C", "/fake/repo-a", "config"): (0, "user@example.com\n"),
                ("git", "-C", "/fake/repo-b", "config"): (0, "user-work@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "user@example.com" in emails
        assert "user-work@example.com" in emails

    def test_gh_noreply_added_when_authenticated(self, monkeypatch):
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (0, '{"id": 99999, "login": "fakeuser"}'),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "99999+fakeuser@users.noreply.github.com" in emails

    def test_gh_unavailable_falls_back_silently(self, monkeypatch):
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): FileNotFoundError,
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "user@example.com" in emails
        assert all("noreply" not in e for e in emails)

    def test_gh_malformed_json_returns_none(self, monkeypatch):
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (0, "<<<not json>>>"),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "user@example.com" in emails
        assert all("noreply" not in e for e in emails)

    def test_gh_unexpected_shape_rejected(self, monkeypatch):
        """``gh api user`` returning JSON with wrong types for id/login
        must not poison the trust set."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (0, '{"id": "not-an-int", "login": "fakeuser"}'),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert all("noreply" not in e for e in emails)

    def test_gh_unauthenticated_adds_no_noreply(self, monkeypatch):
        """`gh api user` rc != 0 (typical of unauthenticated gh): no noreply entry."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "user@example.com" in emails
        assert all("noreply" not in e for e in emails)

    def test_per_repo_failure_skipped_silently(self, monkeypatch):
        """One repo's `git config user.email` failing skips that repo only."""
        _stub_repos(monkeypatch, [Path("/fake/good"), Path("/fake/bad")])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("git", "-C", "/fake/good", "config"): (0, "user-personal@example.com\n"),
                ("git", "-C", "/fake/bad", "config"): (128, ""),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "user@example.com" in emails
        assert "user-personal@example.com" in emails

    def test_config_load_failure_falls_back_to_other_sources(self, monkeypatch):
        """An unreadable config.toml drops only the per-repo walk; global stays."""
        from mind_meld import config as config_module

        def boom(_p):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(config_module, "load_config", boom)
        monkeypatch.setattr(identity, "_gather_config_author_emails", lambda: [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        assert set(identity.refresh_identity_cache(force=True)) == {"user@example.com"}

    def test_gh_string_encoded_uid_accepted(self, monkeypatch):
        """v0.11.19: GitHub Enterprise instances may return ``id`` as a
        decimal-digit string (large numeric values that would otherwise
        overflow JS Number safely-representable bounds). Accept it."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (
                    0,
                    '{"id": "12345678901234567890", "login": "ghuser"}',
                ),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "12345678901234567890+ghuser@users.noreply.github.com" in emails

    def test_gh_bool_uid_rejected(self, monkeypatch):
        """``bool`` is a subclass of ``int`` in Python; reject explicitly
        so a hostile / malformed response can't smuggle ``True`` (=1) or
        ``False`` (=0) into the email form."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (0, '{"id": true, "login": "ghuser"}'),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert all("noreply" not in e for e in emails)

    def test_gh_negative_uid_rejected(self, monkeypatch):
        """Negative ``id`` is nonsensical for a GitHub user — reject."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (0, '{"id": -1, "login": "ghuser"}'),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert all("noreply" not in e for e in emails)

    def test_gh_string_with_non_digits_rejected(self, monkeypatch):
        """A string ``id`` containing anything beyond ``[0-9]`` is
        rejected — guards against ``"99999\\nINJECTED"`` and similar."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (0, '{"id": "99999abc", "login": "ghuser"}'),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert all("noreply" not in e for e in emails)

    def test_config_author_emails_unioned(self, monkeypatch):
        """``[retro].author_emails`` from mm config.toml is additive
        (D4 from /plan-eng-review — backwards compat with existing
        users who set the knob pre-v0.11.17)."""
        from mind_meld import config as config_module

        # Order matters: _stub_repos sets load_config to lambda _p: {}.
        # Override it AFTER so the retro section is what the gatherer reads.
        _stub_repos(monkeypatch, [])
        monkeypatch.setattr(
            config_module,
            "load_config",
            lambda _p: {"retro": {"author_emails": ["historical@example.com"]}},
        )
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "historical@example.com" in emails
        assert "user@example.com" in emails

    def test_per_repo_walk_respects_budget(self, monkeypatch):
        """Tiny budget exhausts after ~one repo; remaining repos are
        skipped without raising. Keeps the refresh from becoming a
        multi-second wait on slow filesystems."""
        roots = [Path(f"/fake/repo-{i}") for i in range(100)]
        _stub_repos(monkeypatch, roots)
        monkeypatch.setattr(identity, "_PER_REPO_BUDGET_S", 0.001)

        scanned: list[str] = []

        import subprocess as _subprocess

        def fake_run(cmd, **_kw):
            if tuple(cmd[:3]) == ("git", "config", "--global"):
                return _FakeResult(0, "user@example.com\n")
            if tuple(cmd[:2]) == ("gh", "api"):
                return _FakeResult(1, "")
            if tuple(cmd[:2]) == ("git", "-C"):
                scanned.append(cmd[2])
                import time as _time

                _time.sleep(0.005)
                return _FakeResult(0, "user-personal@example.com\n")
            return _FakeResult(1, "")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        identity.refresh_identity_cache(force=True)
        assert len(scanned) < 100, (
            f"budget enforcement failed: scanned {len(scanned)} repos out of 100"
        )

    def test_slow_worktree_list_still_collects_first_repo_email(self, monkeypatch):
        """Listing worktrees happens after that root's email, so a slow list cannot drop it."""
        import subprocess as _subprocess

        roots = [Path(f"/fake/repo-{i}") for i in range(8)]
        monkeypatch.setattr(identity, "_PER_REPO_BUDGET_S", 0.05)

        def fake_run(cmd, **_kwargs):
            if cmd[3:5] == ["worktree", "list"]:
                time.sleep(0.01)
                return _subprocess.CompletedProcess(cmd, 0, f"worktree {cmd[2]}\n", "")
            if cmd[3:] == ["config", "user.email"]:
                return _subprocess.CompletedProcess(cmd, 0, "dev@example.com\n", "")
            return _subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        found = identity._gather_per_repo_emails(
            root_discovery=events.GitRootDiscovery(tuple(roots), (), False)
        )
        assert "dev@example.com" in found

    def test_budget_truncated_gather_does_not_refresh_cache(self, monkeypatch):
        """A 5s window that never reads every repo must not replace the trust set."""
        import subprocess as _subprocess

        refreshed_at = datetime.now(timezone.utc).isoformat()
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps({"version": 2, "refreshed_at": refreshed_at, "emails": ["kept@example.com"]})
        )
        roots = [Path(f"/fake/repo-{i}") for i in range(8)]
        monkeypatch.setattr(identity, "_PER_REPO_BUDGET_S", 0.01)

        def fake_run(cmd, **_kwargs):
            if cmd[3:5] == ["worktree", "list"]:
                time.sleep(0.02)
                return _subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[3:] == ["config", "user.email"]:
                return _subprocess.CompletedProcess(cmd, 0, "dev@example.com\n", "")
            return _subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        monkeypatch.setattr(identity, "_gather_global_email", lambda: None)
        monkeypatch.setattr(identity, "_gather_config_author_emails", lambda: [])
        monkeypatch.setattr(identity, "_gather_gh_noreply_email", lambda: None)
        identity.refresh_identity_cache(
            force=True,
            root_discovery=events.GitRootDiscovery(tuple(roots), (), False),
        )
        saved = json.loads(identity.CACHE_PATH.read_text())
        assert saved["refreshed_at"] == refreshed_at
        assert saved["emails"] == ["kept@example.com"]

    def test_concurrent_complete_gather_cannot_persist_a_truncated_one(self, monkeypatch):
        """Overlapping gathers keep their own completeness bit."""
        refreshed_at = datetime.now(timezone.utc).isoformat()
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps({"version": 2, "refreshed_at": refreshed_at, "emails": ["kept@example.com"]})
        )
        slow_id: list[int] = []
        slow_started = threading.Event()
        slow_ready = threading.Event()
        fast_persisted = threading.Event()
        real_note = identity._note_gather_complete
        slow_notes = {"n": 0}

        def note(_complete: bool) -> None:
            # Call one is the pre-gather reset. Call two is the finish. Force
            # that finish incomplete, then wait until the other gather has
            # persisted, so a shared flag would already say complete.
            if threading.get_ident() == slow_id[0]:
                slow_notes["n"] += 1
                if slow_notes["n"] == 1:
                    real_note(True)
                    return
                real_note(False)
                slow_ready.set()
                assert fast_persisted.wait(3)
                return
            assert slow_ready.wait(3)
            real_note(True)

        def read_email(checkout: Path) -> str:
            if threading.get_ident() == slow_id[0]:
                return "truncated@example.com"
            return "complete@example.com"

        monkeypatch.setattr(identity, "_note_gather_complete", note)
        monkeypatch.setattr(identity, "_read_checkout_email", read_email)
        monkeypatch.setattr(identity, "_worktree_checkouts", lambda root: [root])
        monkeypatch.setattr(identity, "_gather_global_email", lambda: None)
        monkeypatch.setattr(identity, "_gather_config_author_emails", lambda: [])
        monkeypatch.setattr(identity, "_gather_gh_noreply_email", lambda: None)
        discovery = events.GitRootDiscovery((Path("/fake/repo"),), (), False)
        errors: list[BaseException] = []

        def slow() -> None:
            slow_id.append(threading.get_ident())
            slow_started.set()
            try:
                identity.refresh_identity_cache(force=True, root_discovery=discovery)
            except BaseException as exc:
                errors.append(exc)

        def fast() -> None:
            try:
                assert slow_started.wait(3)
                identity.refresh_identity_cache(force=True, root_discovery=discovery)
            except BaseException as exc:
                errors.append(exc)
            finally:
                fast_persisted.set()

        threads = [threading.Thread(target=slow), threading.Thread(target=fast)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert errors == []
        assert all(not thread.is_alive() for thread in threads)
        saved = json.loads(identity.CACHE_PATH.read_text())
        assert saved["emails"] == ["complete@example.com"]
        assert saved["refreshed_at"] != refreshed_at

    def test_stale_cache_remembers_one_noreply_lookup(self, monkeypatch):
        """An old completed cache records the check so the next push skips ``gh``."""
        stale = datetime.now(timezone.utc) - timedelta(seconds=identity.TTL_SECONDS + 60)
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": 2,
                    "refreshed_at": stale.isoformat(),
                    "emails": ["owner@example.com"],
                }
            )
        )
        calls = {"n": 0}

        def fake() -> str:
            calls["n"] += 1
            return "1+owner@users.noreply.github.com"

        monkeypatch.setattr(identity, "_gather_gh_noreply_email", fake)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        first = identity.union_checked_github_noreply(("owner@example.com",))
        saved = json.loads(identity.CACHE_PATH.read_text())
        # The next capture reads the cache, which now carries the noreply address.
        second = identity.union_checked_github_noreply(tuple(saved["emails"]))
        identity.union_checked_github_noreply(("owner@example.com",))
        assert calls["n"] == 1
        assert "1+owner@users.noreply.github.com" in first
        assert "1+owner@users.noreply.github.com" in second
        assert saved["refreshed_at"] == stale.isoformat()
        assert isinstance(saved["github_noreply_checked_at"], str)

    def test_never_refreshed_cache_does_not_record_noreply_lookup(self, monkeypatch):
        """A default cache must not become a completed baseline just to remember ``gh``."""
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps({"version": 2, "refreshed_at": None, "emails": ["owner@example.com"]})
        )
        monkeypatch.setattr(
            identity,
            "_gather_gh_noreply_email",
            lambda: "1+owner@users.noreply.github.com",
        )
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        found = identity.union_checked_github_noreply(("owner@example.com",))
        saved = json.loads(identity.CACHE_PATH.read_text())
        assert "1+owner@users.noreply.github.com" in found
        assert saved["emails"] == ["owner@example.com"]
        assert "github_noreply_checked_at" not in saved

    def test_collaborator_email_in_repo_history_NOT_included(self, monkeypatch, tmp_path):
        """**Trust-rooted regression pin (carried from aggregator).**
        A shared repo where a collaborator has commits in the local
        history must NOT leak their email into the trust set. The
        gather reads only ``git config user.email`` — never walks
        ``git log``."""
        import subprocess as real_subprocess

        repo = tmp_path / "shared-repo"
        repo.mkdir()
        real_subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        real_subprocess.run(
            ["git", "config", "user.email", "user@example.com"], cwd=repo, check=True
        )
        real_subprocess.run(["git", "config", "user.name", "KB"], cwd=repo, check=True)
        real_subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
        (repo / "a.txt").write_text("a")
        real_subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        real_subprocess.run(["git", "commit", "-q", "-m", "user"], cwd=repo, check=True)

        env = {
            **__import__("os").environ,
            "GIT_AUTHOR_EMAIL": "alice@collaborator.com",
            "GIT_AUTHOR_NAME": "Alice",
            "GIT_COMMITTER_EMAIL": "alice@collaborator.com",
            "GIT_COMMITTER_NAME": "Alice",
        }
        (repo / "b.txt").write_text("b")
        real_subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        real_subprocess.run(["git", "commit", "-q", "-m", "alice"], cwd=repo, env=env, check=True)

        _stub_repos(monkeypatch, [repo])
        original_run = real_subprocess.run

        def fake_run(cmd, **kw):
            if tuple(cmd[:3]) == ("git", "config", "--global"):
                return _FakeResult(0, "user@example.com\n")
            if tuple(cmd[:2]) == ("gh", "api"):
                return _FakeResult(1, "")
            return original_run(cmd, **kw)

        monkeypatch.setattr(real_subprocess, "run", fake_run)
        emails = identity.refresh_identity_cache(force=True)
        assert "user@example.com" in emails
        assert "alice@collaborator.com" not in emails


# ---------------------------------------------------------------------------
# Refresh semantics.
# ---------------------------------------------------------------------------


class TestRefreshSemantics:
    def test_force_true_always_refreshes(self, monkeypatch):
        """force=True invalidates a fresh cache and rewrites it."""
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "refreshed_at": _isoformat(datetime.now(timezone.utc)),
                    "emails": ["old@example.com"],
                }
            )
        )
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "new@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "new@example.com" in emails
        assert "old@example.com" not in emails

    def test_force_false_skips_when_fresh(self, monkeypatch):
        """force=False is a no-op when cache is fresh — preserves
        the cached value verbatim."""
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "refreshed_at": _isoformat(datetime.now(timezone.utc)),
                    "emails": ["preserved@example.com"],
                }
            )
        )
        # Subprocess would fail noisily if invoked.
        _stub_subprocess(monkeypatch, {("git",): SystemError, ("gh",): SystemError})
        emails = identity.refresh_identity_cache(force=False)
        assert emails == ["preserved@example.com"]

    def test_refresh_persists_to_disk(self, monkeypatch):
        """After refresh, the on-disk cache file is the source of
        truth — subsequent reads return the same data."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails1 = identity.refresh_identity_cache(force=True)
        # Independent read using disable-refresh path; should hit disk.
        emails2 = identity.gather_local_identities(allow_refresh=False)
        assert emails1 == emails2 == ["user@example.com"]


# ---------------------------------------------------------------------------
# Persistence invariants.
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_emails_lowercased(self, monkeypatch):
        """Mixed-case emails normalize to lowercase so ``set`` dedup
        works against case-variant input from peers."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "USER@Example.COM\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "user@example.com" in emails
        assert "USER@Example.COM" not in emails

    def test_emails_deduplicated(self, monkeypatch):
        """Same email reported by multiple sources appears once."""
        _stub_repos(monkeypatch, [Path("/fake/repo-a")])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("git", "-C", "/fake/repo-a", "config"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert emails.count("user@example.com") == 1

    def test_emails_sorted(self, monkeypatch):
        """Output is deterministically sorted so the cache file diff
        is stable across runs (helpful for forensic comparison)."""
        _stub_repos(monkeypatch, [Path("/fake/r1")])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "z@example.com\n"),
                ("git", "-C", "/fake/r1", "config"): (0, "a@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert emails == sorted(emails)

    def test_cache_file_mode_0600(self, monkeypatch):
        """Cache file is created with mode 0600 (lockedjson contract).
        Identity data isn't secret but is per-user — match the existing
        token_usage / upgrade-state cache permissions."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "user@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        identity.refresh_identity_cache(force=True)
        st = identity.CACHE_PATH.stat()
        assert (st.st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# REGRESSION pin — gather + cache integration sanity for fleet sync.
# ---------------------------------------------------------------------------


class TestFleetIntegration:
    def test_two_machines_share_via_cache(self, monkeypatch):
        """Two machines with different local identities both refresh
        their caches, then ``gather_local_identities`` returns the
        per-machine cached set on each. The aggregator's union step
        (test_retro_fleet_aggregator) is what merges them at retro
        time — this test only pins the per-machine cache path."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "machine-a@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails_a = identity.refresh_identity_cache(force=True)
        assert emails_a == ["machine-a@example.com"]

        # Simulate machine B with its own subprocess fixture.
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "machine-b@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails_b = identity.refresh_identity_cache(force=True)
        assert emails_b == ["machine-b@example.com"]


# ---------------------------------------------------------------------------
# Lock discipline (v0.11.19) — flock released during slow subprocess gather.
# ---------------------------------------------------------------------------


class TestIncompleteDiscoveryIdentityCache:
    def _write_stale_cache(self, emails: list[str]) -> dict:
        payload = {
            "version": identity.CACHE_VERSION,
            "refreshed_at": _isoformat(
                datetime.now(timezone.utc) - timedelta(seconds=identity.TTL_SECONDS + 60)
            ),
            "emails": emails,
        }
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(json.dumps(payload))
        return payload

    def test_incomplete_result_returns_union_without_refreshing_cache(self, monkeypatch):
        before = self._write_stale_cache(["old@example.com"])
        result = events.GitRootDiscovery((), ("budget",), True)
        received: list[object] = []

        def gather(*, root_discovery=None):
            received.append(root_discovery)
            return ["new@example.com"]

        monkeypatch.setattr(identity, "_do_full_gather", gather)
        emails = identity.gather_local_identities(root_discovery=result)

        assert emails == ["new@example.com", "old@example.com"]
        assert received == [result]
        assert json.loads(identity.CACHE_PATH.read_text()) == before

    def test_later_complete_result_prunes_after_incomplete_union(self, monkeypatch):
        before = self._write_stale_cache(["old@example.com"])
        incomplete = events.GitRootDiscovery((), ("budget",), True)
        complete = events.GitRootDiscovery((), (), False)

        monkeypatch.setattr(
            identity,
            "_do_full_gather",
            lambda *, root_discovery=None: ["new@example.com"],
        )
        assert identity.gather_local_identities(root_discovery=incomplete) == [
            "new@example.com",
            "old@example.com",
        ]
        assert json.loads(identity.CACHE_PATH.read_text()) == before

        assert identity.refresh_identity_cache(root_discovery=complete) == ["new@example.com"]
        stored = json.loads(identity.CACHE_PATH.read_text())
        assert stored["emails"] == ["new@example.com"]
        assert stored["refreshed_at"] != before["refreshed_at"]

    def test_incomplete_force_refresh_does_not_overwrite_cache(self, monkeypatch):
        before = self._write_stale_cache(["old@example.com"])
        incomplete = events.GitRootDiscovery((), ("budget",), True)
        monkeypatch.setattr(
            identity,
            "_do_full_gather",
            lambda *, root_discovery=None: ["new@example.com"],
        )

        emails = identity.refresh_identity_cache(force=True, root_discovery=incomplete)
        assert emails == ["new@example.com", "old@example.com"]
        assert json.loads(identity.CACHE_PATH.read_text()) == before

    def test_supplied_result_skips_a_second_discovery(self, monkeypatch):
        self._write_stale_cache([])
        supplied = events.GitRootDiscovery((Path("/repo"),), (), False)
        monkeypatch.setattr(
            identity,
            "_do_full_gather",
            lambda *, root_discovery=None: ["new@example.com"],
        )

        emails = identity.gather_local_identities(root_discovery=supplied)
        assert emails == ["new@example.com"]


class TestGitEnvironment:
    def test_per_repo_gather_expands_linked_worktrees_of_elected_root(self, tmp_path, monkeypatch):
        """Discovery dedup elects one checkout per object store; identity
        gathering must still see a sibling worktree's own config.worktree
        email or the retro trust set drops that worktree's commits."""
        target, _, _ = _git_env_repos(tmp_path, monkeypatch)
        subprocess.run(
            ["git", "-C", str(target), "config", "extensions.worktreeConfig", "true"],
            check=True,
        )
        sibling = tmp_path / "sibling"
        subprocess.run(
            ["git", "-C", str(target), "worktree", "add", "-b", "sib", str(sibling)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(sibling),
                "config",
                "--worktree",
                "user.email",
                "sibling@example.com",
            ],
            check=True,
        )
        assert identity._gather_per_repo_emails(root_discovery=([target], [])) == {
            "target@example.com",
            "sibling@example.com",
        }

    @pytest.mark.parametrize("override", ["dir", "parameters"])
    def test_repo_identity_ignores_inherited_overrides(self, tmp_path, monkeypatch, override):
        target, decoy, _ = _git_env_repos(tmp_path, monkeypatch)
        if override == "dir":
            monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        else:
            monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'user.email=injected@example.com'")
        raw = subprocess.run(
            ["git", "-C", str(target), "config", "user.email"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert raw.stdout.strip() != "target@example.com"
        assert identity._gather_per_repo_emails(root_discovery=([target], [])) == {
            "target@example.com"
        }

    def test_global_config_survives_while_repo_config_override_is_scrubbed(
        self, tmp_path, monkeypatch
    ):
        _, decoy, _ = _git_env_repos(tmp_path, monkeypatch)
        global_config = tmp_path / "user.gitconfig"
        global_config.write_text("[user]\n\temail = global@example.com\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        monkeypatch.setenv("GIT_CONFIG", str(decoy / ".git/config"))
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'user.email=injected@example.com'")
        assert identity._gather_global_email() == "global@example.com"

    def test_undecodable_identifiers_are_skipped(self, monkeypatch, tmp_path):
        def run(cmd, **kwargs):
            assert kwargs["encoding"] == "utf-8"
            assert kwargs["errors"] == "strict"
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

        monkeypatch.setattr(subprocess, "run", run)
        assert identity._gather_global_email() is None
        assert identity._gather_per_repo_emails(root_discovery=([tmp_path], [])) == set()


@pytest.mark.parametrize("force", [False, True])
def test_v1_cache_survives_incomplete_discovery_then_is_replaced(monkeypatch, force):
    identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    identity.CACHE_PATH.write_text(
        json.dumps(
            {
                "version": 1,
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "emails": ["old@example.com"],
            }
        )
    )
    monkeypatch.setattr(identity, "_do_full_gather", lambda **kwargs: ["new@example.com"])
    incomplete = events.GitRootDiscovery((), ("budget",), True)
    gather = identity.refresh_identity_cache if force else identity.gather_local_identities
    kwargs = {"force": True} if force else {}
    assert gather(root_discovery=incomplete, **kwargs) == ["new@example.com", "old@example.com"]
    assert json.loads(identity.CACHE_PATH.read_text()) == {
        "version": 2,
        "refreshed_at": None,
        "emails": ["old@example.com"],
    }
    assert gather(root_discovery=events.GitRootDiscovery((), (), False), **kwargs) == [
        "new@example.com"
    ]
    stored = json.loads(identity.CACHE_PATH.read_text())
    assert stored["emails"] == ["new@example.com"]
    assert stored["refreshed_at"] is not None


@pytest.mark.parametrize("force", [False, True])
def test_v1_cache_is_stale_even_with_a_fresh_timestamp(monkeypatch, force):
    identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    identity.CACHE_PATH.write_text(
        json.dumps(
            {
                "version": 1,
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "emails": ["old"],
            }
        )
    )
    monkeypatch.setattr(identity, "_do_full_gather", lambda **kwargs: ["new"])
    complete = events.GitRootDiscovery((), (), False)
    result = (
        identity.refresh_identity_cache(force=True, root_discovery=complete)
        if force
        else identity.gather_local_identities(root_discovery=complete)
    )
    assert result == ["new"]
    assert json.loads(identity.CACHE_PATH.read_text())["emails"] == ["new"]


@pytest.mark.parametrize("payload", [[], {"emails": "bad"}, {"emails": ["good", 5]}])
def test_malformed_identity_cache_is_reset(monkeypatch, payload):
    identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    identity.CACHE_PATH.write_text(json.dumps(payload))
    assert identity.gather_local_identities(allow_refresh=False) == []
    assert json.loads(identity.CACHE_PATH.read_text()) == identity._default_cache()


@pytest.mark.parametrize("version", [1, 2, 999])
def test_read_cached_identities_accepts_any_version_without_writing(version):
    identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": version, "emails": ["z@example.com", "a@example.com"]})
    identity.CACHE_PATH.write_text(payload)
    identity.CACHE_PATH.chmod(0o640)
    before = identity.CACHE_PATH.stat()
    assert identity.read_cached_identities() == ["a@example.com", "z@example.com"]
    after = identity.CACHE_PATH.stat()
    assert identity.CACHE_PATH.read_text() == payload
    assert (after.st_mtime_ns, after.st_mode) == (before.st_mtime_ns, before.st_mode)


def test_read_cached_identities_empty_list_is_known_baseline():
    identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "emails": []})
    identity.CACHE_PATH.write_text(payload)
    assert identity.read_cached_identities() == []
    assert identity.CACHE_PATH.read_text() == payload


@pytest.mark.parametrize("payload", [None, "{bad", "[]", '{"emails": [1]}', "{}"])
def test_read_cached_identities_unknown_does_not_repair(payload):
    if payload is not None:
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(payload)
    assert identity.read_cached_identities() is None
    if payload is None:
        assert not identity.CACHE_PATH.exists()
    else:
        assert identity.CACHE_PATH.read_text() == payload


def test_read_cached_identities_lock_failure_is_unknown(monkeypatch):
    identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    identity.CACHE_PATH.write_text('{"emails": ["cached"]}')

    def fail_lock(*args):
        raise OSError("lock unavailable")

    monkeypatch.setattr(lockedjson.fcntl, "flock", fail_lock)
    assert identity.read_cached_identities() is None
    assert identity.CACHE_PATH.read_text() == '{"emails": ["cached"]}'


class TestLockDiscipline:
    """Pin v0.11.19's release-acquire lock pattern: the flock on
    ``identity-cache.json`` MUST NOT be held during the subprocess walk
    in ``_do_full_gather``. Holding it would block any concurrent autopush
    hook for the duration of the walk (~10s worst case)."""

    def _probe_lock_during_gather(
        self, monkeypatch, refresh_emails: list[str]
    ) -> tuple[list[str], bool]:
        """Patch ``_do_full_gather`` to attempt a non-blocking flock on
        ``CACHE_PATH``. Return (emails, lock_held_during_gather).
        ``lock_held=False`` means the production code released the flock
        before invoking the gather — the desired behavior."""
        import fcntl
        import os

        # Ensure the file exists so the probe can open it.
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.touch()

        observed: dict[str, bool] = {}

        def probing_gather() -> list[str]:
            fd = os.open(str(identity.CACHE_PATH), os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed["held_during_gather"] = False
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    observed["held_during_gather"] = True
            finally:
                os.close(fd)
            return refresh_emails

        monkeypatch.setattr(identity, "_do_full_gather", probing_gather)
        return observed

    def test_gather_local_identities_releases_flock_during_gather(self, monkeypatch):
        """Stale cache + ``allow_refresh=True`` exercises the slow path.
        Probe inside ``_do_full_gather`` confirms the flock is NOT held."""
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=identity.TTL_SECONDS + 60)
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "refreshed_at": _isoformat(old_ts),
                    "emails": ["old@example.com"],
                }
            )
        )
        observed = self._probe_lock_during_gather(monkeypatch, ["fresh@example.com"])
        emails = identity.gather_local_identities(allow_refresh=True)
        assert emails == ["fresh@example.com"]
        assert observed["held_during_gather"] is False, (
            "flock must be released before the slow subprocess gather"
        )

    def test_refresh_identity_cache_releases_flock_during_gather(self, monkeypatch):
        """``force=True`` always exercises the slow path. Same probe
        confirms the flock is NOT held during ``_do_full_gather``."""
        observed = self._probe_lock_during_gather(monkeypatch, ["forced@example.com"])
        emails = identity.refresh_identity_cache(force=True)
        assert emails == ["forced@example.com"]
        assert observed["held_during_gather"] is False, (
            "flock must be released before the slow subprocess gather"
        )

    def test_concurrent_writer_freshness_wins(self, monkeypatch):
        """Phase-3 freshness re-check: if a peer writer landed a fresh
        cache while we were gathering, ``gather_local_identities`` must
        defer to their result instead of overwriting with ours. Idempotent
        in practice (same machine, same identities) but the contract
        prevents stale-but-newer overwrites."""
        # Stale cache to enter the slow path.
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=identity.TTL_SECONDS + 60)
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "refreshed_at": _isoformat(old_ts),
                    "emails": ["old@example.com"],
                }
            )
        )

        # Inside the gather (lock released), simulate a peer writing a
        # fresh cache before we re-acquire to write.
        def gather_then_peer_writes() -> list[str]:
            identity.CACHE_PATH.write_text(
                json.dumps(
                    {
                        "version": identity.CACHE_VERSION,
                        "refreshed_at": _isoformat(datetime.now(timezone.utc)),
                        "emails": ["peer-wrote@example.com"],
                    }
                )
            )
            return ["our-gather@example.com"]

        monkeypatch.setattr(identity, "_do_full_gather", gather_then_peer_writes)

        emails = identity.gather_local_identities(allow_refresh=True)
        # Peer's value wins because it landed fresh first.
        assert emails == ["peer-wrote@example.com"]

    def test_force_refresh_overwrites_concurrent_writer(self, monkeypatch):
        """``refresh_identity_cache(force=True)`` IS the explicit override
        knob (e.g. ``mm refresh-identity``). It always overwrites even if
        a concurrent writer landed a fresh cache during our gather."""

        def gather_then_peer_writes() -> list[str]:
            identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            identity.CACHE_PATH.write_text(
                json.dumps(
                    {
                        "version": identity.CACHE_VERSION,
                        "refreshed_at": _isoformat(datetime.now(timezone.utc)),
                        "emails": ["peer-wrote@example.com"],
                    }
                )
            )
            return ["force-overwrites@example.com"]

        monkeypatch.setattr(identity, "_do_full_gather", gather_then_peer_writes)

        emails = identity.refresh_identity_cache(force=True)
        # Force wins over the peer's fresh write.
        assert emails == ["force-overwrites@example.com"]


def test_refresh_diagnostic_failure_does_not_cancel_gather(monkeypatch):
    """Presentation cannot skip the cold-cache work or its durable result."""
    from mind_meld import identity

    monkeypatch.setattr(identity, "_do_full_gather", lambda: ["owner@example.test"])
    calls = []

    def broken(message, degraded):
        calls.append((message, degraded))
        raise OSError("closed pipe")

    assert identity.gather_local_identities(diagnostic=broken) == ["owner@example.test"]
    assert calls and calls[0][1] is False
    assert identity.read_cached_identities() == ["owner@example.test"]


class TestInputsDigestStaleness:
    """Track 104A: a cache written under other inputs is stale before its TTL,
    but only for a caller that passes a digest (attended push, init)."""

    def _write_fresh(self, emails: list[str], digest: str | None) -> None:
        payload: dict = {
            "version": identity.CACHE_VERSION,
            "refreshed_at": _isoformat(datetime.now(timezone.utc)),
            "emails": emails,
        }
        if digest is not None:
            payload["inputs_digest"] = digest
        identity.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        identity.CACHE_PATH.write_text(json.dumps(payload))

    def _counting_gather(self, monkeypatch, emails: list[str]) -> list[int]:
        calls: list[int] = []

        def gather(*, root_discovery=None):
            calls.append(1)
            return list(emails)

        monkeypatch.setattr(identity, "_do_full_gather", gather)
        return calls

    def test_mismatched_digest_refreshes_once_and_stores_the_new_digest(self, monkeypatch):
        self._write_fresh(["old@example.com"], "d1")
        calls = self._counting_gather(monkeypatch, ["new@example.com"])

        assert identity.gather_local_identities(inputs_digest="d2") == ["new@example.com"]
        assert len(calls) == 1
        stored = json.loads(identity.CACHE_PATH.read_text())
        assert stored["inputs_digest"] == "d2"
        assert stored["emails"] == ["new@example.com"]

        assert identity.gather_local_identities(inputs_digest="d2") == ["new@example.com"]
        assert len(calls) == 1

    def test_no_digest_keeps_the_ttl_only_test(self, monkeypatch):
        self._write_fresh(["old@example.com"], "d1")
        calls = self._counting_gather(monkeypatch, ["new@example.com"])

        assert identity.gather_local_identities() == ["old@example.com"]
        assert identity.refresh_identity_cache() == ["old@example.com"]
        assert calls == []
        assert json.loads(identity.CACHE_PATH.read_text())["inputs_digest"] == "d1"

    def test_cache_written_before_the_field_refreshes_for_a_digest_caller(self, monkeypatch):
        self._write_fresh(["old@example.com"], None)
        calls = self._counting_gather(monkeypatch, ["new@example.com"])

        assert identity.gather_local_identities(inputs_digest="d1") == ["new@example.com"]
        assert len(calls) == 1

    def test_no_refresh_allowed_returns_the_cache_despite_a_mismatch(self, monkeypatch):
        self._write_fresh(["old@example.com"], "d1")
        calls = self._counting_gather(monkeypatch, ["new@example.com"])

        assert identity.gather_local_identities(allow_refresh=False, inputs_digest="d2") == [
            "old@example.com"
        ]
        assert calls == []

    def test_refresh_identity_cache_honors_the_digest(self, monkeypatch):
        self._write_fresh(["old@example.com"], "d1")
        calls = self._counting_gather(monkeypatch, ["new@example.com"])

        assert identity.refresh_identity_cache(inputs_digest="d1") == ["old@example.com"]
        assert calls == []
        assert identity.refresh_identity_cache(inputs_digest="d2") == ["new@example.com"]
        assert len(calls) == 1
        assert json.loads(identity.CACHE_PATH.read_text())["inputs_digest"] == "d2"

    def test_forced_refresh_without_a_digest_drops_the_stored_one(self, monkeypatch):
        self._write_fresh(["old@example.com"], "d1")
        self._counting_gather(monkeypatch, ["new@example.com"])

        assert identity.refresh_identity_cache(force=True) == ["new@example.com"]
        stored = json.loads(identity.CACHE_PATH.read_text())
        assert "inputs_digest" not in stored
        assert stored["emails"] == ["new@example.com"]

    def test_forced_refresh_with_a_digest_stores_it(self, monkeypatch):
        self._write_fresh(["old@example.com"], None)
        self._counting_gather(monkeypatch, ["new@example.com"])

        identity.refresh_identity_cache(force=True, inputs_digest="d9")
        assert json.loads(identity.CACHE_PATH.read_text())["inputs_digest"] == "d9"

    def test_concurrent_fresh_cache_with_another_digest_does_not_swallow_the_change(
        self, monkeypatch
    ):
        """Phase 3 uses phase 1's test: a cache that landed mid-gather, fresh
        by TTL but under ``d1``, must not replace a ``d2`` refresh."""
        self._write_fresh(["old@example.com"], "d0")

        def gather(*, root_discovery=None):
            self._write_fresh(["racer@example.com"], "d1")
            return ["new@example.com"]

        monkeypatch.setattr(identity, "_do_full_gather", gather)

        assert identity.gather_local_identities(inputs_digest="d2") == ["new@example.com"]
        stored = json.loads(identity.CACHE_PATH.read_text())
        assert stored["inputs_digest"] == "d2"
        assert stored["emails"] == ["new@example.com"]

    def test_concurrent_cache_with_the_same_digest_wins(self, monkeypatch):
        self._write_fresh(["old@example.com"], "d0")

        def gather(*, root_discovery=None):
            self._write_fresh(["racer@example.com"], "d2")
            return ["new@example.com"]

        monkeypatch.setattr(identity, "_do_full_gather", gather)

        assert identity.gather_local_identities(inputs_digest="d2") == ["racer@example.com"]

    def test_incomplete_discovery_with_a_mismatch_leaves_cache_bytes_alone(self, monkeypatch):
        self._write_fresh(["old@example.com"], "d1")
        before = json.loads(identity.CACHE_PATH.read_text())
        incomplete = events.GitRootDiscovery((), ("budget",), True)
        monkeypatch.setattr(
            identity,
            "_do_full_gather",
            lambda *, root_discovery=None: ["new@example.com"],
        )

        emails = identity.gather_local_identities(root_discovery=incomplete, inputs_digest="d2")
        assert emails == ["new@example.com", "old@example.com"]
        assert json.loads(identity.CACHE_PATH.read_text()) == before

        emails = identity.refresh_identity_cache(
            force=True, root_discovery=incomplete, inputs_digest="d2"
        )
        assert emails == ["new@example.com", "old@example.com"]
        assert json.loads(identity.CACHE_PATH.read_text()) == before


def _write_global_gitconfig(tmp_path: Path, monkeypatch, *emails: str) -> Path:
    """Point git at a temporary global config: HOME is not moved suite-wide."""
    path = tmp_path / "global.gitconfig"
    path.write_text("".join(f"[user]\n\temail = {email}\n" for email in emails))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return path


class TestIdentityInputsDigest:
    """``events.identity_inputs_digest``: three inputs, file reads only."""

    def test_each_input_changes_the_digest(self, tmp_path, monkeypatch):
        glob = _write_global_gitconfig(tmp_path, monkeypatch, "g@example.com")
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        _init_git_repo(repo_a)
        _init_git_repo(repo_b)
        base = events.identity_inputs_digest({}, (repo_a,))

        assert events.identity_inputs_digest({}, (repo_a,)) == base
        assert (
            events.identity_inputs_digest(
                {"retro": {"author_emails": ["x@example.com"]}}, (repo_a,)
            )
            != base
        )
        glob.write_text("[user]\n\temail = other@example.com\n")
        assert events.identity_inputs_digest({}, (repo_a,)) != base
        glob.write_text("[user]\n\temail = g@example.com\n")
        assert events.identity_inputs_digest({}, (repo_a,)) == base
        assert events.identity_inputs_digest({}, (repo_a, repo_b)) != base

    def test_case_order_and_linked_worktrees_do_not_change_it(self, tmp_path, monkeypatch):
        _write_global_gitconfig(tmp_path, monkeypatch, "G@Example.com")
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        _init_git_repo(repo_a)
        _init_git_repo(repo_b)
        worktree = tmp_path / "wt"
        _make_git_worktree(repo_a, worktree)

        first = events.identity_inputs_digest(
            {"retro": {"author_emails": ["X@example.com", "y@example.com"]}}, (repo_a, repo_b)
        )
        reordered = events.identity_inputs_digest(
            {"retro": {"author_emails": ["y@EXAMPLE.com", "x@example.com"]}},
            (repo_b, repo_a, worktree),
        )
        assert first == reordered

    def test_a_repository_local_email_is_not_an_input(self, tmp_path, monkeypatch):
        """The card names three inputs; publication carries local additions anyway."""
        _write_global_gitconfig(tmp_path, monkeypatch, "g@example.com")
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        before = events.identity_inputs_digest({}, (repo,))
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "new@example.com"], check=True
        )
        assert events.identity_inputs_digest({}, (repo,)) == before

    def test_configured_author_emails_union_all_three_sources(self, tmp_path, monkeypatch):
        _write_global_gitconfig(tmp_path, monkeypatch, "g@example.com")
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "Peer@Example.com"], check=True
        )
        config = {"retro": {"author_emails": ["61af450b-c4a6-4d8c-acf2-70f6534f4b91"]}}

        assert events.configured_author_emails(config, (repo,)) == (
            "61af450b-c4a6-4d8c-acf2-70f6534f4b91",
            "g@example.com",
            "peer@example.com",
        )

    def test_walk_filter_keeps_its_exact_output(self, tmp_path, monkeypatch):
        """Extracting the shared helper must not change the walk's patterns."""
        _write_global_gitconfig(tmp_path, monkeypatch, "G@Example.com")
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "Peer@Example.com"], check=True
        )
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "refreshed_at": _isoformat(datetime.now(timezone.utc)),
                    "emails": ["cached@example.com", "peer@example.com"],
                }
            )
        )
        patterns = events.resolve_walk_author_emails(
            {"retro": {"author_emails": ["Cfg@Example.com"]}}, (repo,)
        )
        assert patterns == (
            "Cfg@Example.com",
            "G@Example.com",
            "cached@example.com",
            "cfg@example.com",
            "g@example.com",
            "peer@example.com",
        )


class TestTailPublishesAndRefreshes:
    """Track 104A at the tail: attended push refreshes on an input change,
    autopush keeps its budget, and the published row names every configured
    identity even when a fresh cache predates it."""

    @staticmethod
    def _seed_cache(emails: list[str], digest: str | None) -> None:
        payload: dict = {
            "version": identity.CACHE_VERSION,
            "refreshed_at": _isoformat(datetime.now(timezone.utc)),
            "emails": emails,
        }
        if digest is not None:
            payload["inputs_digest"] = digest
        identity.CACHE_PATH.write_text(json.dumps(payload))

    @staticmethod
    def _latest_push(tmp_path: Path) -> dict:
        rows = []
        for path in (tmp_path / "mm-events" / "events").glob("*.jsonl"):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row.get("type") == "mm-push":
                    rows.append(row)
        return max(rows, key=lambda row: row["ts"])

    @pytest.fixture
    def tail(self, tmp_path, monkeypatch):
        from mind_meld import events_tail

        _write_global_gitconfig(tmp_path, monkeypatch, "g@example.com")
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        sources = _recapture_test_sources(tmp_path)
        _stub_discovery_to(monkeypatch, repo)
        calls: list[object] = []

        def gather(*, root_discovery=None):
            calls.append(root_discovery)
            return ["gathered@example.com"]

        monkeypatch.setattr(identity, "_do_full_gather", gather)

        def run(config: dict, *, quiet: bool, roots: tuple[Path, ...] | None = None) -> dict:
            if roots is not None:
                _stub_discovery_to(monkeypatch, *roots)
            events_tail._run_events_tail(
                config, sources, "dev-a", dry_run=False, quiet=quiet, suppress_host_capture=True
            )
            return self._latest_push(tmp_path)

        return run, calls, repo

    def test_init_warms_digest_so_first_attended_push_reuses_cache(self, tail, tmp_path):
        # Value: protects=init warms identities for the first attended push;
        # fails_when=init omits the digest and the first push gathers again;
        # why_new=refresh unit tests do not exercise init's digest forwarding;
        # seam=none; no_claim=live identity discovery (temporary repo and fake gather).
        from mind_meld import events_tail

        run, calls, repo = tail
        config: dict = {}

        events_tail._run_events_backfill(config, _recapture_test_sources(tmp_path), "dev-a")

        assert len(calls) == 1
        assert json.loads(identity.CACHE_PATH.read_text())["inputs_digest"] == (
            events.identity_inputs_digest(config, (repo,))
        )
        row = run(config, quiet=False)
        assert len(calls) == 1
        assert "gathered@example.com" in row["local_emails"]

    def test_attended_push_refreshes_once_per_input_change(self, tail, tmp_path, monkeypatch):
        run, calls, repo = tail
        config: dict = {}
        self._seed_cache(["g@example.com"], events.identity_inputs_digest(config, (repo,)))

        run(config, quiet=False)
        assert calls == []

        config = {"retro": {"author_emails": ["61af450b-c4a6-4d8c-acf2-70f6534f4b91"]}}
        row = run(config, quiet=False)
        assert len(calls) == 1
        assert "gathered@example.com" in row["local_emails"]
        assert json.loads(identity.CACHE_PATH.read_text())["inputs_digest"] == (
            events.identity_inputs_digest(config, (repo,))
        )
        run(config, quiet=False)
        assert len(calls) == 1

        _write_global_gitconfig(tmp_path, monkeypatch, "g2@example.com")
        run(config, quiet=False)
        assert len(calls) == 2
        run(config, quiet=False)
        assert len(calls) == 2

        second = tmp_path / "second-repo"
        _init_git_repo(second)
        run(config, quiet=False, roots=(repo, second))
        assert len(calls) == 3
        run(config, quiet=False, roots=(repo, second))
        assert len(calls) == 3

    def test_autopush_gathers_nothing_but_still_publishes_the_new_identity(self, tail):
        run, calls, repo = tail
        self._seed_cache(["g@example.com"], "stale-digest")
        config = {"retro": {"author_emails": ["61af450b-c4a6-4d8c-acf2-70f6534f4b91"]}}

        row = run(config, quiet=True)

        assert calls == []
        assert "61af450b-c4a6-4d8c-acf2-70f6534f4b91" in row["local_emails"]
        assert json.loads(identity.CACHE_PATH.read_text())["inputs_digest"] == "stale-digest"

    def test_partial_discovery_passes_no_digest(self, tail, monkeypatch):
        run, _calls, repo = tail
        seen: list[dict] = []

        def spy(*, allow_refresh=True, root_discovery=None, inputs_digest=None, diagnostic=None):
            seen.append({"inputs_digest": inputs_digest})
            return ["g@example.com"]

        monkeypatch.setattr(identity, "gather_local_identities", spy)
        monkeypatch.setattr(
            events,
            "discover_git_roots",
            lambda _cfg, **_kw: events.GitRootDiscovery((repo,), ("budget",), True),
        )
        run({}, quiet=False)
        assert seen == [{"inputs_digest": None}]

        _stub_discovery_to(monkeypatch, repo)
        run({}, quiet=False)
        assert seen[-1]["inputs_digest"] == events.identity_inputs_digest({}, (repo,))
        run({}, quiet=True)
        assert seen[-1]["inputs_digest"] is None

    def test_strict_signature_gather_fakes_still_work(self, tail, monkeypatch):
        """Older fakes accept no ``inputs_digest``; the tail must not pass it."""
        run, _calls, _repo = tail
        monkeypatch.setattr(
            identity,
            "gather_local_identities",
            lambda *, allow_refresh=True, root_discovery=None: ["fake@example.com"],
        )
        row = run({}, quiet=False)
        assert "fake@example.com" in row["local_emails"]


def _refresh_identity_with_cold_index(tmp_path, monkeypatch, *, recapture_budget_ms=None):
    """Run ``mm refresh-identity --json`` over a cold Codex index ahead of Paseo.

    Paseo names a real repository whose local email is ``capsule@example.com``;
    the configured author emails hold only ``other@example.com``. Each cold unit
    costs one fake second, so a 100 ms discovery never reaches Paseo.
    """
    import dataclasses

    from typer.testing import CliRunner

    from mind_meld import cli, rootprobes
    from tests.test_root_probers import _cfg, _synthetic

    target, _, _ = _git_env_repos(tmp_path, monkeypatch)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "capsule@example.com"], check=True
    )
    home = tmp_path / "home"
    projects = home / ".paseo" / "projects" / "projects.json"
    projects.parent.mkdir(parents=True)
    projects.write_text(json.dumps([{"rootPath": str(target)}]))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(rootprobes, "_discovery_home", lambda: home)

    clock = [1000.0]
    cold = _synthetic({f"u{i}": [] for i in range(3)}, clock=clock)
    paseo = next(p for p in rootprobes.PROBERS if p.name == "paseo")
    monkeypatch.setattr(rootprobes, "PROBERS", (cold, dataclasses.replace(paseo)))
    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    if recapture_budget_ms is not None:
        monkeypatch.setattr(events, "RECAPTURE_DISCOVERY_BUDGET_MS", recapture_budget_ms)
    monkeypatch.setattr(cli, "load_config", lambda: _cfg("codex"))
    monkeypatch.setattr(identity, "_gather_config_author_emails", lambda: ["other@example.com"])
    monkeypatch.setattr(identity, "_gather_gh_noreply_email", lambda: None)
    result = CliRunner().invoke(cli.app, ["refresh-identity", "--json"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def test_refresh_identity_reads_local_email_of_every_root_a_completed_discovery_holds(
    tmp_path, monkeypatch
):
    emails = _refresh_identity_with_cold_index(tmp_path, monkeypatch)
    assert "capsule@example.com" in emails and "other@example.com" in emails
    assert "capsule@example.com" in (identity.read_cached_identities(require_refresh=True) or [])


def test_refresh_identity_misses_that_email_when_discovery_runs_out_of_budget(
    tmp_path, monkeypatch
):
    """The field failure: a 100 ms discovery stops in the cold Codex index."""
    emails = _refresh_identity_with_cold_index(tmp_path, monkeypatch, recapture_budget_ms=100)
    assert "capsule@example.com" not in emails
