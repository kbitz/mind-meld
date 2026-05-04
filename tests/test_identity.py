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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mind_meld import identity

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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.gather_local_identities(allow_refresh=True)
        assert "kb@example.com" in emails
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.gather_local_identities(allow_refresh=True)
        assert emails == ["kb@example.com"]

    def test_wrong_version_in_cache_rebuilds(self, monkeypatch):
        """A cache with ``version != CACHE_VERSION`` is treated as
        invalid — rebuilds from scratch instead of silently using a
        stale schema."""
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.gather_local_identities(allow_refresh=True)
        assert "kb@example.com" in emails
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "kb@example.com" in emails

    def test_per_repo_overrides_unioned(self, monkeypatch):
        _stub_repos(monkeypatch, [Path("/fake/repo-a"), Path("/fake/repo-b")])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("git", "-C", "/fake/repo-a", "config"): (0, "kb@example.com\n"),
                ("git", "-C", "/fake/repo-b", "config"): (0, "kb-work@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "kb@example.com" in emails
        assert "kb-work@example.com" in emails

    def test_gh_noreply_added_when_authenticated(self, monkeypatch):
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): FileNotFoundError,
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "kb@example.com" in emails
        assert all("noreply" not in e for e in emails)

    def test_gh_malformed_json_returns_none(self, monkeypatch):
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (0, "<<<not json>>>"),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "kb@example.com" in emails
        assert all("noreply" not in e for e in emails)

    def test_gh_unexpected_shape_rejected(self, monkeypatch):
        """``gh api user`` returning JSON with wrong types for id/login
        must not poison the trust set."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (0, '{"id": "not-an-int", "login": "fakeuser"}'),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert all("noreply" not in e for e in emails)

    def test_gh_string_encoded_uid_accepted(self, monkeypatch):
        """v0.11.19: GitHub Enterprise instances may return ``id`` as a
        decimal-digit string (large numeric values that would otherwise
        overflow JS Number safely-representable bounds). Accept it."""
        _stub_repos(monkeypatch, [])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "historical@example.com" in emails
        assert "kb@example.com" in emails

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
                return _FakeResult(0, "kb@example.com\n")
            if tuple(cmd[:2]) == ("gh", "api"):
                return _FakeResult(1, "")
            if tuple(cmd[:2]) == ("git", "-C"):
                scanned.append(cmd[2])
                import time as _time

                _time.sleep(0.005)
                return _FakeResult(0, "kb-personal@example.com\n")
            return _FakeResult(1, "")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        identity.refresh_identity_cache(force=True)
        assert len(scanned) < 100, (
            f"budget enforcement failed: scanned {len(scanned)} repos out of 100"
        )

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
        real_subprocess.run(["git", "config", "user.email", "kb@example.com"], cwd=repo, check=True)
        real_subprocess.run(["git", "config", "user.name", "KB"], cwd=repo, check=True)
        real_subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
        (repo / "a.txt").write_text("a")
        real_subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        real_subprocess.run(["git", "commit", "-q", "-m", "kb"], cwd=repo, check=True)

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
                return _FakeResult(0, "kb@example.com\n")
            if tuple(cmd[:2]) == ("gh", "api"):
                return _FakeResult(1, "")
            return original_run(cmd, **kw)

        monkeypatch.setattr(real_subprocess, "run", fake_run)
        emails = identity.refresh_identity_cache(force=True)
        assert "kb@example.com" in emails
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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails1 = identity.refresh_identity_cache(force=True)
        # Independent read using disable-refresh path; should hit disk.
        emails2 = identity.gather_local_identities(allow_refresh=False)
        assert emails1 == emails2 == ["kb@example.com"]


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
                ("git", "config", "--global"): (0, "KB@Example.COM\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert "kb@example.com" in emails
        assert "KB@Example.COM" not in emails

    def test_emails_deduplicated(self, monkeypatch):
        """Same email reported by multiple sources appears once."""
        _stub_repos(monkeypatch, [Path("/fake/repo-a")])
        _stub_subprocess(
            monkeypatch,
            {
                ("git", "config", "--global"): (0, "kb@example.com\n"),
                ("git", "-C", "/fake/repo-a", "config"): (0, "kb@example.com\n"),
                ("gh", "api"): (1, ""),
            },
        )
        emails = identity.refresh_identity_cache(force=True)
        assert emails.count("kb@example.com") == 1

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
                ("git", "config", "--global"): (0, "kb@example.com\n"),
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
