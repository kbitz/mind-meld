"""Tests for the init-time event backfill (v0.11.8).

`_run_events_backfill` runs at the END of ``mm init`` and writes a 30-day
git-snapshot + sessions-snapshot to the local events file, but NO mm-push
event (push counts stay honest; first real push sets the cursor).

These tests pin the helper directly. The init wiring itself is exercised
by the existing TestInitFlow integration suite — adding a second runner-
based path here would fight ``~/.claude`` defaults that the existing tests
already navigate around.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from mind_meld import events, events_tail
from mind_meld import events as _mm_events
from mind_meld import host_usage as _mm_host_usage
from mind_meld import identity as _mm_identity
from mind_meld import token_usage as _mm_token_usage


def _read_events(events_file: Path) -> list[dict]:
    return [json.loads(ln) for ln in events_file.read_text().splitlines() if ln.strip()]


def _make_git_repo(repo_dir: Path) -> None:
    """Create a minimal local git repo with one commit so walk_git_projects
    has something real to capture."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _seed_claude_with_repo(claude_dir: Path, repo_dir: Path) -> None:
    """Write a session jsonl whose ``cwd`` field points at a real git repo
    so ``_probe_claude`` returns the repo as a discoverable root."""
    proj_dir = claude_dir / "projects" / "-test-repo"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "session.jsonl").write_text(
        json.dumps({"cwd": str(repo_dir), "type": "user"}) + "\n"
    )


def _make_sources(
    events_root: Path,
    claude_dir: Path | None,
    *,
    hosts: tuple[str, ...] = (),
) -> list[dict]:
    """Construct the resolved-sources list shape that `_run_events_backfill`
    expects (output of ``get_sources(config)``).

    ``hosts`` names host sources to enable — the host readers are consent-gated
    on them, so a test that expects the codex/opencode readers to run must
    enable them here."""
    sources: list[dict] = []
    if claude_dir is not None:
        sources.append({"name": "claude", "path": str(claude_dir), "type": "claude"})
    for host in hosts:
        sources.append({"name": host, "path": str(events_root / host), "type": "generic"})
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


class TestRunEventsBackfill:
    def test_writes_git_and_sessions_snapshots_no_mm_push(self, tmp_path):
        """Happy path: backfill writes one git-snapshot + one sessions-
        snapshot row to the events file. NO mm-push row — the cursor stays
        at "no prior push" so the first real push re-walks the same window
        (aggregator dedups via canonical_remote_url + sha)."""
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        repo = tmp_path / "myrepo"
        _make_git_repo(repo)
        _seed_claude_with_repo(claude_dir, repo)

        sources = _make_sources(events_root, claude_dir)
        config = {"sync": {"sources": sources}}

        events_tail._run_events_backfill(config, sources, "dev-a")

        files = sorted((events_root / "events").glob("*.jsonl"))
        assert len(files) == 1, "exactly one events file expected"
        rows = _read_events(files[0])

        types = [r["type"] for r in rows]
        assert "git-snapshot" in types
        assert "sessions-snapshot" in types
        assert "mm-push" not in types, (
            "backfill must not write an mm-push row — keeps push counts honest "
            "and lets the first real push set the cursor"
        )
        for r in rows:
            assert r["device"] == "dev-a"

    def test_skipped_when_mm_events_source_absent(self, tmp_path):
        """An un-migrated config (pre-v0.10.1, no mm-events source) must
        no-op silently. No events_root materialized."""
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)

        # mm-events source NOT in the list.
        sources = [{"name": "claude", "path": str(claude_dir), "type": "claude"}]
        config: dict = {"sync": {"sources": sources}}

        events_tail._run_events_backfill(config, sources, "dev-a")

        assert not (events_root / "events").exists(), (
            "backfill must not create an events tree when mm-events is absent from sources"
        )

    def test_failure_breadcrumb_does_not_raise(self, tmp_path, monkeypatch, capsys):
        """Forensic-only invariant (mirrors `_run_events_tail`): an exception
        from ``discover_git_roots`` (or any inner walk) is swallowed and a
        single ``mm: notice:`` line goes to stderr. The caller (``mm init``)
        proceeds."""
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        sources = _make_sources(events_root, claude_dir)
        config = {"sync": {"sources": sources}}

        def boom(_config, **_kwargs):
            raise RuntimeError("synthetic walk failure")

        monkeypatch.setattr(_mm_events, "discover_git_roots", boom)

        # Must not raise.
        events_tail._run_events_backfill(config, sources, "dev-a")

        captured = capsys.readouterr()
        assert "mm: notice: events backfill failed" in captured.err
        assert "RuntimeError" in captured.err
        # No events file written when discovery itself blew up.
        assert not (events_root / "events").exists()

    def test_no_claude_sources_writes_git_only(self, tmp_path):
        """A config with only mm-events (no claude) → backfill writes a
        git-snapshot row but NO sessions-snapshot row. Mirrors the existing
        ``_run_events_tail`` shape."""
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root, claude_dir=None)
        config = {"sync": {"sources": sources}}

        events_tail._run_events_backfill(config, sources, "dev-a")

        files = sorted((events_root / "events").glob("*.jsonl"))
        assert len(files) == 1
        rows = _read_events(files[0])
        types = [r["type"] for r in rows]
        assert "sessions-snapshot" not in types
        # walk_git_projects always returns one snapshot (possibly with empty
        # projects list) — matches the tail's behavior.
        assert "git-snapshot" in types
        assert "mm-push" not in types

    def test_backfill_uses_30_day_window_for_git_and_sessions(self, tmp_path, monkeypatch):
        """Both capture paths receive the same explicit 30-day window.

        The session walker currently keeps ``since`` for API stability, but
        passing it remains part of the backfill's legible semantic contract.
        This must not silently drift to ``last_push_ts`` while the git path
        stays correct.
        """
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        sources = _make_sources(events_root, claude_dir)
        config = {"sync": {"sources": sources}}

        captured: dict = {}

        def fake_walk(roots, since, total_budget_ms):
            captured["git_since"] = since
            return [
                {
                    "v": events.EVENTS_SCHEMA_VERSION,
                    "type": "git-snapshot",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "device": "",
                    "projects": [],
                    "skipped": [],
                }
            ]

        monkeypatch.setattr(_mm_events, "walk_git_projects", fake_walk)
        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda _c, **_kwargs: _mm_events.GitRootDiscovery((), (), False),
        )
        monkeypatch.setattr(_mm_token_usage, "warm_token_cache_inline", lambda paths: None)

        @contextmanager
        def fake_lock(mode):
            assert mode == "block"
            yield {}

        monkeypatch.setattr(_mm_token_usage, "lock_and_get_files", fake_lock)

        def fake_session_walk(path, since, **kwargs):
            assert path == claude_dir
            captured["session_since"] = since
            return []

        monkeypatch.setattr(_mm_events, "walk_session_metadata", fake_session_walk)

        events_tail._run_events_backfill(config, sources, "dev-a")

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        since = captured["git_since"]
        # Within a few seconds of now-30d.
        delta = (now - since).total_seconds()
        target = events.INITIAL_CURSOR_LOOKBACK_DAYS * 86400
        assert abs(delta - target) < 60, (
            f"since must be ~now-30d, got delta={delta}s vs target={target}s"
        )
        assert captured["session_since"] == since


class TestBackfillHostSnapshot:
    """Track 19A — init shares the tail's host capture, but keeps its own
    failure policy: one safe notice, no ``mm-push`` row, no autorun
    breadcrumb (init has neither)."""

    def _stub(self, monkeypatch, *, codex=None, grok=None, opencode=None, calls=None):
        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda _c, **_kw: _mm_events.GitRootDiscovery((), (), False),
        )
        monkeypatch.setattr(
            _mm_events,
            "walk_git_projects",
            lambda roots, since, total_budget_ms: [
                {
                    "v": _mm_events.EVENTS_SCHEMA_VERSION,
                    "type": "git-snapshot",
                    "ts": "2026-08-15T00:00:00+00:00",
                    "device": "",
                    "projects": [],
                    "skipped": [],
                }
            ],
        )
        monkeypatch.setattr(
            _mm_identity,
            "refresh_identity_cache",
            lambda *, force=False, root_discovery=None: [],
        )
        monkeypatch.setattr(_mm_token_usage, "warm_token_cache_inline", lambda paths: None)
        monkeypatch.setattr(_mm_events, "walk_session_metadata", lambda *a, **kw: [])

        def reader(name, result):
            def read(*, deadline, consented=False):
                if calls is not None:
                    calls.append(name)
                return result

            return read

        empty = _mm_host_usage.HostUsageResult({}, complete=True)
        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", reader("codex", codex or empty))
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", reader("grok", grok or empty))
        monkeypatch.setattr(
            _mm_host_usage, "read_opencode_usage", reader("opencode", opencode or empty)
        )

    def test_row_order_is_git_sessions_host_and_never_mm_push(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        sources = _make_sources(events_root, claude_dir, hosts=("codex",))
        self._stub(
            monkeypatch,
            codex=_mm_host_usage.HostUsageResult(
                {
                    "codex": {
                        "2026-08-15": {"input": 5, "cache_create": 0, "cache_read": 0, "output": 1}
                    }
                },
                complete=True,
            ),
        )

        events_tail._run_events_backfill({"sync": {"sources": sources}}, sources, "dev-a")

        rows = _read_events(sorted((events_root / "events").glob("*.jsonl"))[0])
        assert [r["type"] for r in rows] == [
            "git-snapshot",
            "sessions-snapshot",
            "host-usage-snapshot",
        ]
        assert rows[-1]["hosts"] == {
            "codex": {"2026-08-15": {"input": 5, "cache_create": 0, "cache_read": 0, "output": 1}}
        }
        assert rows[-1]["device"] == "dev-a"

    def test_host_row_ships_without_any_claude_source(self, tmp_path, monkeypatch):
        """A Codex-or-OpenCode-only machine still contributes host activity,
        even though it has no Claude sessions to snapshot."""
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root, claude_dir=None)
        self._stub(monkeypatch)

        events_tail._run_events_backfill({"sync": {"sources": sources}}, sources, "dev-a")

        rows = _read_events(sorted((events_root / "events").glob("*.jsonl"))[0])
        types = [r["type"] for r in rows]
        assert types == ["git-snapshot", "host-usage-snapshot"]
        assert rows[-1]["hosts"] == {}, "a completed empty scan is a fact, not a failure"

    def test_incomplete_scan_omits_the_row_with_one_safe_notice(
        self, tmp_path, monkeypatch, capsys
    ):
        events_root = tmp_path / "events_root"
        sources = _make_sources(events_root, claude_dir=None)
        self._stub(
            monkeypatch,
            grok=_mm_host_usage.HostUsageResult({}, complete=False, reason="unsupported"),
        )

        events_tail._run_events_backfill(
            {"sync": {"sources": sources}, "retro": {"grok_host_usage": True}},
            sources,
            "dev-a",
        )

        rows = _read_events(sorted((events_root / "events").glob("*.jsonl"))[0])
        assert [r["type"] for r in rows] == ["git-snapshot"]
        err = capsys.readouterr().err
        assert err.count("host-usage snapshot skipped") == 1
        assert (
            "mm: notice: host-usage snapshot skipped (grok unsupported) — "
            "content sync and git/session capture unaffected. "
            "grok's log format changed in a way this version cannot read. "
            "Upgrade mm, or run `mm disable-source grok` to stop retrying.\n" in err
        )
        assert "A later substantive push will retry" not in err, (
            "unsupported storage is permanent — never promise a retry"
        )
        assert "events backfill failed" not in err

    def test_absent_mm_events_source_touches_no_host_reader(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        sources = [{"name": "claude", "path": str(claude_dir), "type": "claude"}]
        calls: list[str] = []
        self._stub(monkeypatch, calls=calls)

        events_tail._run_events_backfill({"sync": {"sources": sources}}, sources, "dev-a")

        assert calls == []


class TestInitWiring:
    """Smoke test that ``init`` actually calls ``_run_events_backfill``.

    The full TestInitFlow suite already exercises init end-to-end; we just
    need to confirm the wiring is present so a refactor that drops the
    call site fails loudly."""

    def test_init_calls_run_events_backfill(self, tmp_path, monkeypatch):
        """Stub the backfill helper, drive ``mm init`` via the runner,
        assert the helper was called once with the right device_id."""
        from typer.testing import CliRunner

        from mind_meld.cli import app

        runner = CliRunner()

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False)

        calls: list[tuple] = []

        def stub_backfill(config, sources, device_id):
            calls.append((config["device"]["id"], device_id, len(sources)))

        monkeypatch.setattr("mind_meld.events_tail._run_events_backfill", stub_backfill)
        # Stub the skill link installer too — irrelevant to this test and
        # avoids touching ~/.claude.
        monkeypatch.setattr(
            "mind_meld.skill_link._ensure_retro_skill_links",
            lambda *, dry_run=False, allow_mutate=True, explicit=False, may_create: (),
        )

        storage = tmp_path / "icloud"
        # storage path, device name, passphrase x2, claude=Y, all other sources=n
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output

        assert len(calls) == 1, "init must call _run_events_backfill exactly once"
        cfg_dev_id, called_dev_id, n_sources = calls[0]
        assert cfg_dev_id == called_dev_id, "device_id must match config"
        assert n_sources >= 1, "sources list must not be empty"

    def test_init_passes_derived_skill_consent_to_installer(self, tmp_path, monkeypatch):
        """The init hook receives the same source-derived consent as push."""
        from typer.testing import CliRunner

        from mind_meld.cli import app

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False)
        received: list[frozenset[str] | None] = []
        monkeypatch.setattr(
            "mind_meld.skill_link._ensure_retro_skill_links",
            lambda *, dry_run=False, allow_mutate=True, explicit=False, may_create: (
                received.append(may_create) or ()
            ),
        )
        monkeypatch.setattr("mind_meld.events_tail._run_events_backfill", lambda *_args: None)

        # The consent helper deliberately receives *resolved* sources.  Avoid
        # deriving this test's source from the developer or CI runner's home.
        claude_dir = tmp_path / "claude"
        claude_dir.mkdir()
        monkeypatch.setattr(
            "mind_meld.cli._prompt_sources",
            lambda: [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
        )

        storage = tmp_path / "icloud"
        stdin = f"{storage}\nMac A\npw123\npw123\n"
        result = CliRunner().invoke(app, ["init"], input=stdin)

        assert result.exit_code == 0, result.output
        assert received == [frozenset({"claude"})]

    def test_init_continues_when_skill_installer_raises(self, tmp_path, monkeypatch):
        """The optional installer must not abort init or suppress backfill."""
        from typer.testing import CliRunner

        from mind_meld.cli import app

        runner = CliRunner()
        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False)

        def installer_failure(*, dry_run=False, allow_mutate=True, explicit=False, may_create):
            raise RuntimeError("simulated installer regression")

        backfill_calls: list[str] = []
        monkeypatch.setattr("mind_meld.skill_link._ensure_retro_skill_links", installer_failure)
        monkeypatch.setattr(
            "mind_meld.events_tail._run_events_backfill",
            lambda _config, _sources, device_id: backfill_calls.append(device_id),
        )

        storage = tmp_path / "icloud"
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)

        assert result.exit_code == 0, result.output
        assert backfill_calls, "init must continue to the events backfill"
        assert "retro-fleet skill installation failed" in result.output
        # Pin the REAL exception, not whatever a drifted stub signature
        # would raise from the argument binder.
        # Rich wraps the console line, so match a substring that cannot span
        # the wrap. The point is that it is the REAL RuntimeError, not a
        # TypeError from a drifted stub signature.
        assert "RuntimeError: simulated" in result.output


class TestEventsDirIsolation:
    """Regression pin for the conftest `_isolate_mm_events_path` fixture.

    Pre-fixture, runner-driven `mm init` tests wrote backfilled events to
    the user's real `~/.local/share/mind-meld/events/<random-id>-<date>
    .jsonl` because `DEFAULT_SOURCES['mm-events'].path` was unmodified.
    Observed as 30+ phantom device-id files accumulating from local pytest
    runs after v0.11.8 shipped the init backfill — phantoms then inflated
    retro-fleet's `M of N known machines` header to absurd values.
    """

    def test_default_mm_events_path_is_redirected(self):
        """The autouse fixture must redirect DEFAULT_SOURCES['mm-events']
        away from `~/.local/share/mind-meld`. Bare assertion — the fixture
        runs before this test body."""
        from mind_meld.config import DEFAULT_SOURCES

        target = next(s for s in DEFAULT_SOURCES if s.get("name") == "mm-events")
        path = str(target["path"])
        assert "_isolated_mm_events" in path, (
            f"mm-events path should be redirected to per-test tmp; got {path}"
        )
        assert not path.startswith("~/"), "redirected path must already be expanded"

    def test_runner_init_does_not_touch_real_events_dir(self, tmp_path, monkeypatch):
        """Drive `mm init` end-to-end via the runner and verify no events
        file is written to `~/.local/share/mind-meld/events/`. Pinned
        without stubbing `_run_events_backfill` so the leak is caught
        at the fixture layer rather than the call-site layer."""
        from typer.testing import CliRunner

        from mind_meld.cli import app

        runner = CliRunner()

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False)
        monkeypatch.setattr(
            "mind_meld.skill_link._ensure_retro_skill_links",
            lambda *, dry_run=False, allow_mutate=True, explicit=False, may_create: (),
        )

        # Snapshot the real events dir pre-init so we can compare.
        real_dir = Path.home() / ".local" / "share" / "mind-meld" / "events"
        before = set(real_dir.glob("*.jsonl")) if real_dir.is_dir() else set()

        storage = tmp_path / "icloud"
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output

        after = set(real_dir.glob("*.jsonl")) if real_dir.is_dir() else set()
        leaked = after - before
        assert not leaked, (
            f"runner-driven init wrote {len(leaked)} events file(s) to the real "
            f"events dir: {sorted(p.name for p in leaked)}. The autouse "
            f"`_isolate_mm_events_path` fixture is not redirecting correctly."
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
