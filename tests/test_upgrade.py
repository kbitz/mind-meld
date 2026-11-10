"""Tests for mind_meld.upgrade — auto-upgrade nudge.

Covers:
  - check_for_upgrade: short-circuits, cache freshness, network failure, parse
    failure, nudge gate (version + 24h), tag selection (prerelease / local /
    invalid / peeled filtered across >100 tags).
  - detect_self_version_transition: dev-build no-op, first-run seed,
    upgrade/downgrade detection, within-invocation idempotency,
    cross-invocation idempotency.
  - run_transition_hook: writes a self-upgrade row to pullhistory.
  - format_upgrade_message: plain str, no Rich markup interpretation.
  - emit_nudge_if_due: stderr-not-stdout, gate respect.
  - Race pin (Codex F4): two concurrent subprocesses detect transition
    simultaneously → exactly ONE pullhistory row written.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mind_meld import pullhistory, statedb, upgrade

# ── Isolation fixtures ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_upgrade_state(monkeypatch, tmp_path: Path):
    """Redirect the upgrade cache + pullhistory log to tmp paths so tests
    don't touch the real ~/.config/mind-meld state. Resets module-level
    flags before every test.
    """
    cache_dir = tmp_path / "config-mm"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(upgrade, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(upgrade, "CACHE_PATH", cache_dir / "upgrade-state.json")
    monkeypatch.setattr(pullhistory, "HISTORY_DIR", cache_dir)
    upgrade._reset_for_tests()
    yield
    upgrade._reset_for_tests()


def _set_version(monkeypatch, version: str) -> None:
    """Patch __version__ in both modules that re-import it."""
    monkeypatch.setattr("mind_meld.__version__", version)
    monkeypatch.setattr("mind_meld.upgrade.__version__", version)


def _stub_tags(monkeypatch, tag_names: list[str]) -> None:
    """Replace _fetch_tags with a static list of tag-name dicts."""

    def _fake(*_a):
        return [{"name": n} for n in tag_names]

    monkeypatch.setattr(upgrade, "_fetch_tags", _fake)


def _stub_tags_raises(monkeypatch, exc: Exception) -> None:
    def _fake(*_a):
        raise exc

    monkeypatch.setattr(upgrade, "_fetch_tags", _fake)


# ── _pick_latest_tag ──────────────────────────────────────────────────────


class TestPickLatestTag:
    def test_picks_max_semver(self):
        tags = [{"name": "v0.9.3"}, {"name": "v0.9.4"}, {"name": "v0.9.2"}]
        result = upgrade._pick_latest_tag(tags)
        assert result is not None
        assert result[0] == "v0.9.4"

    def test_strips_v_prefix(self):
        tags = [{"name": "v0.9.4"}, {"name": "0.9.5"}]
        result = upgrade._pick_latest_tag(tags)
        assert result is not None
        assert result[0] == "0.9.5"

    def test_skips_prerelease(self):
        # rc/alpha/beta filtered via Version.is_prerelease
        tags = [{"name": "v1.0.0-rc1"}, {"name": "v0.9.4"}, {"name": "v1.0.0-alpha"}]
        result = upgrade._pick_latest_tag(tags)
        assert result is not None
        assert result[0] == "v0.9.4"

    def test_skips_local_version(self):
        # `+local` would sort > base per packaging; must be filtered
        tags = [{"name": "v0.9.4"}, {"name": "v0.9.4+wip"}]
        result = upgrade._pick_latest_tag(tags)
        assert result is not None
        assert result[0] == "v0.9.4"

    def test_skips_invalid(self):
        # Non-semver names silently dropped
        tags = [{"name": "main"}, {"name": "v0.9.4"}, {"name": "not-a-version"}]
        result = upgrade._pick_latest_tag(tags)
        assert result is not None
        assert result[0] == "v0.9.4"

    def test_empty_returns_none(self):
        assert upgrade._pick_latest_tag([]) is None

    def test_all_filtered_returns_none(self):
        # Only prerelease + local tags → no valid candidate
        tags = [{"name": "v1.0.0-rc1"}, {"name": "v0.9.4+wip"}]
        assert upgrade._pick_latest_tag(tags) is None

    def test_handles_non_dict_entries(self):
        tags = ["v0.9.4", {"name": "v0.9.5"}, None]  # type: ignore[list-item]
        result = upgrade._pick_latest_tag(tags)  # type: ignore[arg-type]
        assert result is not None
        assert result[0] == "v0.9.5"


# ── check_for_upgrade short-circuits ──────────────────────────────────────


class TestCheckShortCircuits:
    def test_dev_build_skips(self, monkeypatch):
        _set_version(monkeypatch, "0.0.0+dev")
        # Even if _fetch_tags would be hit, dev-build short-circuits first
        called = []
        monkeypatch.setattr(upgrade, "_fetch_tags", lambda *_a, **_kw: called.append(1) or [])
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "skip"
        assert called == []

    def test_invocation_skip_short_circuits(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        upgrade.set_invocation_skip(True)
        called = []
        monkeypatch.setattr(upgrade, "_fetch_tags", lambda *_a, **_kw: called.append(1) or [])
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "skip"
        assert called == []

    def test_config_opt_out_skips(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        called = []
        monkeypatch.setattr(upgrade, "_fetch_tags", lambda *_a, **_kw: called.append(1) or [])
        config = {"upgrade": {"auto_check": False}}
        result = upgrade.check_for_upgrade(config=config)
        assert result.state == "skip"
        assert called == []


# ── check_for_upgrade happy / network paths ───────────────────────────────


class TestCheckHappy:
    def test_upgrade_available_first_run(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.3", "v0.9.4"])
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "upgrade-available"
        assert result.local == "0.9.3"
        assert result.latest == "0.9.4"
        # Command tracks the moving `latest` branch — version-independent, no
        # `@vX.Y.Z` pin (the pin is what froze `pipx upgrade`).
        assert result.install_cmd == upgrade.INSTALL_CMD
        assert "@latest" in result.install_cmd
        assert "@v0.9.4" not in result.install_cmd
        assert result.should_nudge is True

    def test_current_when_local_equals_latest(self, monkeypatch):
        _set_version(monkeypatch, "0.9.4")
        _stub_tags(monkeypatch, ["v0.9.3", "v0.9.4"])
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "current"
        assert result.latest == "0.9.4"

    def test_current_when_local_ahead(self, monkeypatch):
        # /ship's pyproject-bump-then-tag window: pyproject says 0.9.5, tag
        # still v0.9.4. Don't nudge user to install OLDER version.
        _set_version(monkeypatch, "0.9.5")
        _stub_tags(monkeypatch, ["v0.9.3", "v0.9.4"])
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "current"

    def test_unknown_when_empty_tags(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, [])
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "unknown"

    def test_unknown_when_all_tags_filtered(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v1.0.0-rc1", "v0.9.4+local"])
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "unknown"

    def test_url_error_uses_cached_state(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        # Seed cache with prior successful check
        cache = {
            "latest_version": "0.9.4",
            "checked_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "attempted_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "last_nudged_version": None,
            "last_nudged_at": None,
            "last_seen_self_version": "0.9.3",
        }
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        _stub_tags_raises(monkeypatch, urllib.error.URLError("network down"))
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "upgrade-available"
        assert result.latest == "0.9.4"

    def test_url_error_first_run_returns_unknown(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags_raises(monkeypatch, urllib.error.URLError("dns fail"))
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "unknown"
        # attempted_at gets stamped so subsequent calls within backoff don't refetch
        cache = json.loads(upgrade.CACHE_PATH.read_text())
        assert cache["attempted_at"] is not None

    def test_failure_backoff_prevents_refetch(self, monkeypatch):
        """After a network failure, attempted_at is set; next call within
        4h does NOT trigger another fetch even though checked_at is stale.
        """
        _set_version(monkeypatch, "0.9.3")
        # First call: fail
        _stub_tags_raises(monkeypatch, urllib.error.URLError("fail"))
        upgrade.check_for_upgrade(config={})
        # Second call: stub raises if hit, but should NOT be called
        called = []

        def _trip(*_a, **_kw):
            called.append(1)
            raise urllib.error.URLError("should not be called")

        monkeypatch.setattr(upgrade, "_fetch_tags", _trip)
        result = upgrade.check_for_upgrade(config={})
        assert called == []
        assert result.state == "unknown"

    def test_fresh_cache_skips_http(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        # Pre-populate cache with very-recent checked_at
        now = datetime.now(timezone.utc).isoformat()
        cache = {
            "latest_version": "0.9.4",
            "checked_at": now,
            "attempted_at": now,
            "last_nudged_version": None,
            "last_nudged_at": None,
            "last_seen_self_version": "0.9.3",
        }
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        called = []
        monkeypatch.setattr(upgrade, "_fetch_tags", lambda *_a, **_kw: called.append(1) or [])
        result = upgrade.check_for_upgrade(config={})
        assert called == []
        assert result.state == "upgrade-available"

    def test_corrupt_cache_treated_as_first_run(self, monkeypatch):
        upgrade.CACHE_PATH.write_text("not json {{{")
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.4"])
        result = upgrade.check_for_upgrade(config={})
        assert result.state == "upgrade-available"


# ── nudge gate ────────────────────────────────────────────────────────────


class TestNudgeGate:
    def test_no_renudge_within_24h_same_version(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.4"])
        # First call: nudges
        result1 = upgrade.check_for_upgrade(config={})
        assert result1.should_nudge is True
        upgrade.record_nudge(result1.latest)  # type: ignore[arg-type]
        # Second call shortly after: should NOT renudge
        result2 = upgrade.check_for_upgrade(config={})
        assert result2.state == "upgrade-available"
        assert result2.should_nudge is False

    def test_renudges_when_latest_changes(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.4"])
        result1 = upgrade.check_for_upgrade(config={})
        upgrade.record_nudge(result1.latest)  # type: ignore[arg-type]

        # Force checked_at to be stale so we re-fetch
        cache = json.loads(upgrade.CACHE_PATH.read_text())
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        cache["checked_at"] = old
        cache["attempted_at"] = old
        upgrade.CACHE_PATH.write_text(json.dumps(cache))

        # Latest is now 0.9.5 — should re-nudge despite recent last_nudged_at
        _stub_tags(monkeypatch, ["v0.9.5"])
        result2 = upgrade.check_for_upgrade(config={})
        assert result2.latest == "0.9.5"
        assert result2.should_nudge is True

    def test_renudges_after_24h(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.4"])
        upgrade.check_for_upgrade(config={})
        upgrade.record_nudge("0.9.4")
        # Backdate last_nudged_at by 25h
        cache = json.loads(upgrade.CACHE_PATH.read_text())
        cache["last_nudged_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        result = upgrade.check_for_upgrade(config={})
        assert result.should_nudge is True


# ── transition detection ──────────────────────────────────────────────────


class TestTransitionDetection:
    def test_dev_build_noop(self, monkeypatch):
        _set_version(monkeypatch, "0.0.0+dev")
        result = upgrade.detect_self_version_transition(config={})
        assert result is None
        # Cache file should not exist (or be empty)
        if upgrade.CACHE_PATH.exists():
            cache = json.loads(upgrade.CACHE_PATH.read_text() or "{}")
            assert cache.get("last_seen_self_version") is None

    def test_first_run_seeds_no_transition(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        result = upgrade.detect_self_version_transition(config={})
        assert result is None
        cache = json.loads(upgrade.CACHE_PATH.read_text())
        assert cache["last_seen_self_version"] == "0.9.3"

    def test_no_transition_same_version(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        upgrade.detect_self_version_transition(config={})
        # Reset within-process flag so we can call again
        upgrade._TRANSITION_DETECTED_THIS_INVOCATION = False
        result = upgrade.detect_self_version_transition(config={})
        assert result is None

    def test_upgrade_transition_detected(self, monkeypatch):
        # Seed cache as if last run was 0.9.3
        cache = upgrade._empty_cache()
        cache["last_seen_self_version"] = "0.9.3"
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        _set_version(monkeypatch, "0.9.4")
        result = upgrade.detect_self_version_transition(config={})
        assert result == ("0.9.3", "0.9.4")
        # Cache updated
        cache_after = json.loads(upgrade.CACHE_PATH.read_text())
        assert cache_after["last_seen_self_version"] == "0.9.4"

    def test_downgrade_transition_logged(self, monkeypatch):
        cache = upgrade._empty_cache()
        cache["last_seen_self_version"] = "1.0.0"
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        _set_version(monkeypatch, "0.9.4")
        result = upgrade.detect_self_version_transition(config={})
        assert result == ("1.0.0", "0.9.4")

    def test_within_invocation_idempotent(self, monkeypatch):
        cache = upgrade._empty_cache()
        cache["last_seen_self_version"] = "0.9.3"
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        _set_version(monkeypatch, "0.9.4")
        result1 = upgrade.detect_self_version_transition(config={})
        result2 = upgrade.detect_self_version_transition(config={})
        assert result1 == ("0.9.3", "0.9.4")
        assert result2 is None  # second call within process: no double-log

    def test_invocation_skip_returns_none(self, monkeypatch):
        cache = upgrade._empty_cache()
        cache["last_seen_self_version"] = "0.9.3"
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        _set_version(monkeypatch, "0.9.4")
        upgrade.set_invocation_skip(True)
        result = upgrade.detect_self_version_transition(config={})
        assert result is None
        # Cache NOT mutated when --no-check-version is set
        cache_after = json.loads(upgrade.CACHE_PATH.read_text())
        assert cache_after["last_seen_self_version"] == "0.9.3"


# ── run_transition_hook → pullhistory ─────────────────────────────────────


class TestTransitionHook:
    def test_logs_self_upgrade_row(self, monkeypatch):
        cache = upgrade._empty_cache()
        cache["last_seen_self_version"] = "0.9.3"
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        _set_version(monkeypatch, "0.9.4")
        config = {"device": {"id": "dev-test"}}
        upgrade.run_transition_hook(config)

        records = list(pullhistory.read_records())
        upgrade_rows = [r for r in records if r.get("verb") == "self-upgrade"]
        assert len(upgrade_rows) == 1
        row = upgrade_rows[0]
        assert row["device"] == "dev-test"
        assert row["old_version"] == "0.9.3"
        assert row["new_version"] == "0.9.4"
        # No source/rel_path/action on self-upgrade rows
        assert "source" not in row
        assert "rel_path" not in row
        assert "action" not in row

    def test_no_log_on_no_transition(self, monkeypatch):
        _set_version(monkeypatch, "0.9.3")
        upgrade.run_transition_hook({"device": {"id": "dev-test"}})
        records = list(pullhistory.read_records())
        assert [r for r in records if r.get("verb") == "self-upgrade"] == []

    def test_no_log_on_dev_build(self, monkeypatch):
        _set_version(monkeypatch, "0.0.0+dev")
        upgrade.run_transition_hook({"device": {"id": "dev-test"}})
        records = list(pullhistory.read_records())
        assert [r for r in records if r.get("verb") == "self-upgrade"] == []


# ── format_upgrade_message ────────────────────────────────────────────────


class TestFormatMessage:
    def test_plain_str_no_rich_markup(self):
        msg = upgrade.format_upgrade_message(
            "0.9.3", "0.9.4", "pipx install --force git+https://github.com/x/y.git@v0.9.4"
        )
        assert isinstance(msg, str)
        # Backticks and version arrows must appear literally — Rich brackets
        # would have been stripped if routed through Console.print
        assert "`pipx install" in msg
        assert "0.9.3 → 0.9.4" in msg
        assert msg.startswith("mm: notice:")


# ── emit_nudge_if_due (stderr-not-stdout pin) ─────────────────────────────


class TestEmitNudge:
    def test_emits_to_stderr(self, monkeypatch, capsys):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.4"])
        upgrade.emit_nudge_if_due(config={})
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err
        assert "0.9.3 → 0.9.4" in captured.err
        # Stdout untouched
        assert captured.out == ""

    def test_silent_when_current(self, monkeypatch, capsys):
        _set_version(monkeypatch, "0.9.4")
        _stub_tags(monkeypatch, ["v0.9.4"])
        upgrade.emit_nudge_if_due(config={})
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_silent_when_nudge_gate_active(self, monkeypatch, capsys):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.4"])
        # First emit fires
        upgrade.emit_nudge_if_due(config={})
        capsys.readouterr()  # drain
        # Second emit suppressed by 24h gate
        upgrade.emit_nudge_if_due(config={})
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_silent_when_dev_build(self, monkeypatch, capsys):
        _set_version(monkeypatch, "0.0.0+dev")
        _stub_tags(monkeypatch, ["v0.9.4"])
        upgrade.emit_nudge_if_due(config={})
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_silent_when_invocation_skip(self, monkeypatch, capsys):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.4"])
        upgrade.set_invocation_skip(True)
        upgrade.emit_nudge_if_due(config={})
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_silent_when_config_opt_out(self, monkeypatch, capsys):
        _set_version(monkeypatch, "0.9.3")
        _stub_tags(monkeypatch, ["v0.9.4"])
        upgrade.emit_nudge_if_due(config={"upgrade": {"auto_check": False}})
        captured = capsys.readouterr()
        assert captured.err == ""


# ── pullhistory contract violation: silent skip ──────────────────────────


class TestPullhistoryContract:
    def test_self_upgrade_missing_old_version_silently_skips(self):
        pullhistory.append_self_upgrade(device="dev-test", old_version="", new_version="0.9.4")
        records = list(pullhistory.read_records())
        assert records == []

    def test_self_upgrade_missing_new_version_silently_skips(self):
        pullhistory.append_self_upgrade(device="dev-test", old_version="0.9.3", new_version="")
        records = list(pullhistory.read_records())
        assert records == []

    def test_self_upgrade_valid_writes_row(self):
        pullhistory.append_self_upgrade(device="dev-test", old_version="0.9.3", new_version="0.9.4")
        records = list(pullhistory.read_records())
        assert len(records) == 1
        assert records[0]["verb"] == "self-upgrade"


# ── _fetch_tags git adapter + no-prompt / failure recording (Track 124B) ──


class TestFetchTagsGitAdapter124B:
    @pytest.mark.parametrize("seed", [124, 999])
    def test_218_tags_any_order_picks_highest_stable_without_http(self, monkeypatch, seed):
        _set_version(monkeypatch, "1.0.0")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: pytest.fail("urlopen must not be called"),
        )
        monkeypatch.setattr(upgrade, "find_git", lambda: "/usr/bin/git")

        # 214 stable tags v1.0.0 .. v1.2.13 (highest stable: v1.2.13), plus 4
        # non-qualifying lines (prerelease, local-version, peeled tag above all
        # stable tags, and a non-tag branch ref) = 218 lines total.
        lines: list[str] = []
        for idx in range(214):
            minor = idx // 100
            patch = idx % 100
            lines.append(f"{idx:040x}\trefs/tags/v1.{minor}.{patch}")
        lines.extend(
            [
                f"{'a' * 40}\trefs/tags/v2.0.0-rc1",
                f"{'b' * 40}\trefs/tags/v1.9.9+local",
                f"{'c' * 40}\trefs/tags/v9.9.9^{{}}",
                f"{'d' * 40}\trefs/heads/latest",
            ]
        )
        assert len(lines) == 218
        rng = random.Random(seed)
        rng.shuffle(lines)
        payload = ("\n".join(lines) + "\n").encode("utf-8")

        def _fake_run_git(argv, *, env, timeout):
            if "config" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")
            assert argv == ["/usr/bin/git", "ls-remote", "--tags", "--refs", upgrade.REPO_URL]
            assert env.get("LC_ALL") == "C"
            assert timeout == upgrade.CHECK_TIMEOUT_SECONDS
            return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=b"")

        monkeypatch.setattr(upgrade, "_run_git", _fake_run_git)
        result = upgrade.check_for_upgrade(force=True)
        assert result.state == "upgrade-available"
        assert result.latest == "1.2.13"
        assert result.check_failure is None
        saved = json.loads(upgrade.CACHE_PATH.read_text())
        assert saved["latest_version"] == "1.2.13"
        assert saved["check_failure_cause"] is None
        assert saved["check_failure_at"] is None

    @pytest.mark.parametrize(
        ("stderr_text", "expected_cause"),
        [
            (
                "fatal: could not read Username for 'https://github.com': "
                "terminal prompts disabled",
                "not-readable",
            ),
            (
                "fatal: could not read Password for 'https://user@github.com': No such device",
                "not-readable",
            ),
            ("fatal: terminal prompts disabled", "not-readable"),
            ("remote: Invalid username or token.\nfatal: Authentication failed", "not-readable"),
            ("remote: Repository not found.\nfatal: repository not found", "not-readable"),
            ("git@github.com: Permission denied (publickey).", "not-readable"),
            ("Host key verification failed.", "not-readable"),
            (
                "fatal: unable to access '...': The requested URL returned error: 401",
                "not-readable",
            ),
            (
                "fatal: unable to access '...': The requested URL returned error: 403",
                "not-readable",
            ),
            ("fatal: unable to access '...': Could not resolve host: github.com", "network"),
            ("ssh: connect to host github.com port 22: Connection timed out", "network"),
        ],
    )
    def test_classification_table_stderr_samples(self, monkeypatch, stderr_text, expected_cause):
        _set_version(monkeypatch, "1.0.0")
        monkeypatch.setattr(upgrade, "find_git", lambda: "/usr/bin/git")

        def _fake_run_git(argv, *, env, timeout):
            if "config" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(
                argv, 128, stdout=b"", stderr=stderr_text.encode("utf-8")
            )

        monkeypatch.setattr(upgrade, "_run_git", _fake_run_git)
        res = upgrade.check_for_upgrade(force=True)
        assert res.state == "unknown"
        assert res.check_failure == expected_cause
        saved = json.loads(upgrade.CACHE_PATH.read_text())
        assert saved["check_failure_cause"] == expected_cause

    def test_classification_empty_tags_git_missing_oserror_and_timeout(self, monkeypatch):
        _set_version(monkeypatch, "1.0.0")

        # 1) exit 0 with no tags -> unparseable
        monkeypatch.setattr(upgrade, "find_git", lambda: "/usr/bin/git")
        monkeypatch.setattr(
            upgrade,
            "_run_git",
            lambda argv, *, env, timeout: subprocess.CompletedProcess(
                argv, 0, stdout=b"", stderr=b""
            ),
        )
        res = upgrade.check_for_upgrade(force=True)
        assert res.check_failure == "unparseable"

        # 2) find_git returning None -> git-missing
        monkeypatch.setattr(upgrade, "find_git", lambda: None)
        res = upgrade.check_for_upgrade(force=True)
        assert res.check_failure == "git-missing"

        # 3) PermissionError from seam -> unexpected:PermissionError
        monkeypatch.setattr(upgrade, "find_git", lambda: "/usr/bin/git")

        def _raise_perm(argv, *, env, timeout):
            if "config" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")
            raise PermissionError("blocked by sandbox")

        monkeypatch.setattr(upgrade, "_run_git", _raise_perm)
        res = upgrade.check_for_upgrade(force=True)
        assert res.check_failure == "unexpected:PermissionError"

        # 4) TimeoutExpired -> network
        def _raise_timeout(argv, *, env, timeout):
            if "config" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")
            raise subprocess.TimeoutExpired(argv, timeout)

        monkeypatch.setattr(upgrade, "_run_git", _raise_timeout)
        res = upgrade.check_for_upgrade(force=True)
        assert res.check_failure == "network"

    def test_fake_git_script_records_argv_cwd_env_closed_stdin_and_new_session(
        self, monkeypatch, tmp_path
    ):
        _set_version(monkeypatch, "1.0.0")
        monkeypatch.setattr(upgrade, "_refuse_git_under_pytest", lambda: None)
        monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
        monkeypatch.delenv("GIT_SSH", raising=False)
        monkeypatch.delenv("GIT_HTTP_LOW_SPEED_LIMIT", raising=False)
        monkeypatch.delenv("GIT_HTTP_LOW_SPEED_TIME", raising=False)

        record_file = tmp_path / "git-invocation.json"
        fake_git = tmp_path / "fake-git"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                f"""
                import json, os, sys
                if sys.argv[1:2] == ["config"]:
                    sys.exit(1)
                stdin_data = sys.stdin.read()
                info = {{
                    "argv": sys.argv[1:],
                    "cwd": os.getcwd(),
                    "stdin_eof": stdin_data == "",
                    "new_session": os.getsid(0) == os.getpid(),
                    "env": {{
                        k: os.environ.get(k)
                        for k in (
                            "GIT_TERMINAL_PROMPT",
                            "GIT_ASKPASS",
                            "SSH_ASKPASS",
                            "SSH_ASKPASS_REQUIRE",
                            "GCM_INTERACTIVE",
                            "GIT_HTTP_LOW_SPEED_LIMIT",
                            "GIT_HTTP_LOW_SPEED_TIME",
                            "GIT_SSH_COMMAND",
                            "LC_ALL",
                        )
                    }},
                }}
                with open({str(record_file)!r}, "w", encoding="utf-8") as fh:
                    json.dump(info, fh)
                sys.stdout.write("0123456789abcdef0123456789abcdef01234567\\trefs/tags/v1.3.0\\n")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        monkeypatch.setattr(upgrade, "find_git", lambda: str(fake_git))

        res = upgrade.check_for_upgrade(force=True)
        assert res.state == "upgrade-available"
        assert res.latest == "1.3.0"

        recorded = json.loads(record_file.read_text(encoding="utf-8"))
        assert recorded["argv"] == ["ls-remote", "--tags", "--refs", upgrade.REPO_URL]
        assert Path(recorded["cwd"]).resolve() == Path(tempfile.gettempdir()).resolve()
        assert recorded["stdin_eof"] is True
        assert recorded["new_session"] is True
        env = recorded["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == ""
        assert env["SSH_ASKPASS"] == ""
        assert env["SSH_ASKPASS_REQUIRE"] == "never"
        assert env["GCM_INTERACTIVE"] == "never"
        assert env["GIT_HTTP_LOW_SPEED_LIMIT"] == "1000"
        assert env["GIT_HTTP_LOW_SPEED_TIME"] == "60"
        assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
        assert env["LC_ALL"] == "C"

    def test_fake_git_reading_stdin_and_failing_auth_returns_not_readable_promptly(
        self, monkeypatch, tmp_path
    ):
        _set_version(monkeypatch, "1.0.0")
        monkeypatch.setattr(upgrade, "_refuse_git_under_pytest", lambda: None)
        fake_git = tmp_path / "fake-git-auth"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                """
                import sys
                if sys.argv[1:2] == ["config"]:
                    sys.exit(1)
                _ = sys.stdin.read()
                sys.stderr.write(
                    "fatal: could not read Username for 'https://github.com': "
                    "terminal prompts disabled\\n"
                )
                sys.exit(128)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        monkeypatch.setattr(upgrade, "find_git", lambda: str(fake_git))

        start = time.monotonic()
        res = upgrade.check_for_upgrade(force=True)
        elapsed = time.monotonic() - start
        # gstack-timing-ok: closed stdin must prevent an authentication prompt from waiting.
        assert elapsed < 5.0
        assert res.state == "unknown"
        assert res.check_failure == "not-readable"

    def test_timeout_kills_sleeping_grandchild_and_records_network(self, monkeypatch, tmp_path):
        _set_version(monkeypatch, "1.0.0")
        monkeypatch.setattr(upgrade, "_refuse_git_under_pytest", lambda: None)
        monkeypatch.setattr(upgrade, "CHECK_TIMEOUT_SECONDS", 1)
        pid_file = tmp_path / "grandchild.pid"
        fake_git = tmp_path / "fake-git-sleep"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                f"""
                import subprocess, sys, time
                if sys.argv[1:2] == ["config"]:
                    sys.exit(1)
                child = subprocess.Popen(
                    [{sys.executable!r}, "-c", "import time; time.sleep(60)"]
                )
                with open({str(pid_file)!r}, "w", encoding="utf-8") as fh:
                    fh.write(str(child.pid))
                time.sleep(60)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        monkeypatch.setattr(upgrade, "find_git", lambda: str(fake_git))

        start = time.monotonic()
        res = upgrade.check_for_upgrade(force=True)
        elapsed = time.monotonic() - start
        # gstack-timing-ok: forced 1s subprocess timeout must bound descendant cleanup too.
        assert elapsed < 5.0
        assert res.state == "unknown"
        assert res.check_failure == "network"
        grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)

    def test_no_prompt_environment_ssh_command_precedence(self, monkeypatch):
        # 1) Existing GIT_SSH_COMMAND gets -o BatchMode=yes appended
        env = upgrade._no_prompt_environment({"GIT_SSH_COMMAND": "ssh -p 2222"})
        assert env["GIT_SSH_COMMAND"] == "ssh -p 2222 -o BatchMode=yes"

        # 2) GIT_SSH alone leaves GIT_SSH_COMMAND unset
        env = upgrade._no_prompt_environment({"GIT_SSH": "/usr/local/bin/custom-ssh"})
        assert "GIT_SSH_COMMAND" not in env
        assert env["GIT_SSH"] == "/usr/local/bin/custom-ssh"

        # 3) core.sshCommand from git config is preserved with -o BatchMode=yes appended
        monkeypatch.setattr(upgrade, "find_git", lambda: "/usr/bin/git")
        monkeypatch.setattr(
            upgrade,
            "_run_git",
            lambda argv, *, env, timeout: subprocess.CompletedProcess(
                argv, 0, stdout=b"ssh -i /Users/user/.ssh/id_test\n", stderr=b""
            ),
        )
        env = upgrade._no_prompt_environment({})
        assert env["GIT_SSH_COMMAND"] == "ssh -i /Users/user/.ssh/id_test -o BatchMode=yes"

        # 4) Failing config read falls back to ssh -o BatchMode=yes
        def _fail_git(argv, *, env, timeout):
            raise OSError("git config failed")

        monkeypatch.setattr(upgrade, "_run_git", _fail_git)
        env = upgrade._no_prompt_environment({})
        assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"

    def test_pytest_guard_refuses_before_spawning_git(self, monkeypatch, tmp_path):
        marker = tmp_path / "ran-git"
        fake_git = tmp_path / "fake-git-marker"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        monkeypatch.setattr(upgrade, "find_git", lambda: str(fake_git))

        with pytest.raises(OSError, match="refusing to run git under pytest"):
            upgrade._run_git([str(fake_git), "version"], env={}, timeout=1)
        assert not marker.exists()


class TestRecordedFailureCauses124B:
    @pytest.mark.parametrize(
        "outcome,cause", [("failed", None), ("failed", "unexpected:RuntimeError")]
    )
    def test_retry_gate_preserves_explicit_outcome_and_cause(self, outcome, cause):
        now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        cache = upgrade._empty_cache()
        cache.update(
            install_attempt_version="1.1.0",
            install_attempt_at=now.isoformat(),
            install_attempt_outcome=outcome,
            install_failure_cause=cause,
            install_failure_at=now.isoformat() if cause else None,
        )
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        before = upgrade.CACHE_PATH.read_bytes()
        assert upgrade._claim_install_attempt("1.1.0", now=now) == "failed"
        assert json.loads(upgrade.CACHE_PATH.read_text())["install_failure_cause"] == cause
        assert upgrade.CACHE_PATH.read_bytes() == before

    @pytest.mark.parametrize("cause", upgrade.INSTALL_FAILURE_CAUSES + ("unexpected:RuntimeError",))
    def test_current_view_hides_obsolete_install_failure_without_writing(self, monkeypatch, cause):
        _set_version(monkeypatch, "1.1.0")
        now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        cache = upgrade._empty_cache()
        cache.update(
            latest_version="1.1.0",
            checked_at=now.isoformat(),
            install_failure_cause=cause,
            install_failure_at=now.isoformat(),
        )
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        before = upgrade.CACHE_PATH.read_bytes()
        view = upgrade.cached_upgrade_view({}, now=now)
        assert view.state == "current"
        assert view.install_failure is None
        assert view.install_failure_at is None
        assert upgrade.CACHE_PATH.read_bytes() == before

    @pytest.mark.parametrize(
        ("cause", "exc_or_tags"),
        [
            ("not-readable", upgrade._CheckFailed("not-readable")),
            ("network", OSError("network down")),
            ("unparseable", ValueError("bad tag data")),
            ("git-missing", upgrade._CheckFailed("git-missing")),
            ("unexpected:RuntimeError", RuntimeError("secret message /Users/user/path")),
        ],
    )
    def test_each_check_cause_is_recorded_and_cleared_by_next_success(
        self, monkeypatch, cause, exc_or_tags
    ):
        _set_version(monkeypatch, "1.0.0")
        t0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        _stub_tags_raises(monkeypatch, exc_or_tags)

        res = upgrade.check_for_upgrade(now=t0)
        assert res.state == "unknown"
        assert res.check_failure == cause
        assert res.check_failure_at == t0

        raw = json.loads(upgrade.CACHE_PATH.read_text(encoding="utf-8"))
        assert raw["check_failure_cause"] == cause
        assert raw["check_failure_at"] == t0.isoformat()

        # Fresh-cache and backoff paths leave the recorded check failure untouched
        t_backoff = t0 + timedelta(hours=1)
        _stub_tags(monkeypatch, ["v1.1.0"])
        res_backoff = upgrade.check_for_upgrade(now=t_backoff)
        assert res_backoff.state == "unknown"
        raw_backoff = json.loads(upgrade.CACHE_PATH.read_text(encoding="utf-8"))
        assert raw_backoff["check_failure_cause"] == cause

        # Next fetch after backoff clears check_failure_cause and check_failure_at
        t_retry = t0 + upgrade.DEFAULT_FAILURE_BACKOFF + timedelta(minutes=1)
        res_ok = upgrade.check_for_upgrade(now=t_retry)
        assert res_ok.state == "upgrade-available"
        assert res_ok.check_failure is None
        raw_ok = json.loads(upgrade.CACHE_PATH.read_text(encoding="utf-8"))
        assert raw_ok["check_failure_cause"] is None
        assert raw_ok["check_failure_at"] is None

        # And a fresh-cache call also leaves check_failure_cause untouched when set
        raw_ok["check_failure_cause"] = "network"
        raw_ok["check_failure_at"] = t_retry.isoformat()
        upgrade.CACHE_PATH.write_text(json.dumps(raw_ok), encoding="utf-8")
        res_fresh = upgrade.check_for_upgrade(now=t_retry + timedelta(minutes=5))
        assert res_fresh.state == "upgrade-available"
        raw_after_fresh = json.loads(upgrade.CACHE_PATH.read_text(encoding="utf-8"))
        assert raw_after_fresh["check_failure_cause"] == "network"

    def test_stderr_and_exception_secrets_never_enter_cache_json(self, monkeypatch):
        _set_version(monkeypatch, "1.0.0")
        monkeypatch.setattr(upgrade, "find_git", lambda: "/usr/bin/git")
        sensitive_stderr = (
            "fatal: Authentication failed for "
            "'https://user:pw-placeholder@github.com/kbitz/mind-meld.git/' "
            "from /Users/user/.gitconfig\n"
        )
        monkeypatch.setattr(
            upgrade,
            "_run_git",
            lambda argv, *, env, timeout: subprocess.CompletedProcess(
                argv, 128, stdout=b"", stderr=sensitive_stderr.encode("utf-8")
            ),
        )
        upgrade.check_for_upgrade(force=True)
        raw_text = upgrade.CACHE_PATH.read_text(encoding="utf-8")
        assert "pw-placeholder" not in raw_text
        assert "github.com" not in raw_text
        assert "/Users/user" not in raw_text
        assert "Authentication failed" not in raw_text
        assert json.loads(raw_text)["check_failure_cause"] == "not-readable"

        # Unexpected exception stores only the class name, never message text
        _stub_tags_raises(
            monkeypatch,
            KeyError("https://user:pw-placeholder@github.com /Users/user/secret"),
        )
        upgrade.check_for_upgrade(force=True)
        raw_text_exc = upgrade.CACHE_PATH.read_text(encoding="utf-8")
        assert "pw-placeholder" not in raw_text_exc
        assert "/Users/user" not in raw_text_exc
        assert json.loads(raw_text_exc)["check_failure_cause"] == "unexpected:KeyError"

    def test_pre_124b_cache_and_unknown_values_read_as_no_record(self, monkeypatch):
        _set_version(monkeypatch, "1.0.0")
        now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        pre_124b = {
            "latest_version": "1.0.0",
            "checked_at": now.isoformat(),
            "attempted_at": now.isoformat(),
            "last_nudged_version": None,
            "last_nudged_at": None,
            "last_seen_self_version": "1.0.0",
        }
        normalized = upgrade._normalize_cache(pre_124b)
        assert normalized["check_failure_cause"] is None
        assert normalized["check_failure_at"] is None
        assert normalized["install_failure_cause"] is None
        assert normalized["install_failure_at"] is None

        upgrade.CACHE_PATH.write_text(json.dumps(pre_124b), encoding="utf-8")
        view = upgrade.cached_upgrade_view({}, now=now)
        assert view.check_failure is None
        assert view.install_failure is None

        # Unknown / hand-edited cause strings validate to None
        bogus = dict(pre_124b)
        bogus["check_failure_cause"] = "custom-message: /Users/user"
        bogus["check_failure_at"] = now.isoformat()
        bogus["install_failure_cause"] = "unexpected:not valid identifier!"
        bogus["install_failure_at"] = now.isoformat()
        upgrade.CACHE_PATH.write_text(json.dumps(bogus), encoding="utf-8")
        view_bogus = upgrade.cached_upgrade_view({}, now=now)
        assert view_bogus.check_failure is None
        assert view_bogus.check_failure_at is None
        assert view_bogus.install_failure is None
        assert view_bogus.install_failure_at is None


# ── Race pin (Codex F4): two concurrent processes, exactly ONE row ───────


class TestRacePin:
    """Two concurrent mm processes detect transition simultaneously → exactly
    ONE pullhistory self-upgrade row written. Pins the single-cache-file
    flock-protected read-compare-write design.

    fcntl.flock is process-level (not thread-level), so we MUST use real
    subprocesses. multiprocessing.Process behavior on macOS (spawn vs fork)
    is fiddly under pytest; subprocess.Popen of `python -c` is the simplest
    correct shape.
    """

    def test_two_processes_exactly_one_row(self, tmp_path, monkeypatch):
        # Use a distinct subdir from the autouse fixture's `config-mm` so we
        # don't collide with its mkdir.
        cache_dir = tmp_path / "race-cache"
        cache_dir.mkdir()
        # Seed cache with 0.9.3 as last_seen so both processes see a transition
        seed = upgrade._empty_cache()
        seed["last_seen_self_version"] = "0.9.3"
        (cache_dir / "upgrade-state.json").write_text(json.dumps(seed))

        barrier = tmp_path / "barrier"

        repo_root = Path(__file__).resolve().parent.parent
        src_root = repo_root / "src"

        worker = textwrap.dedent(f"""
            import os, sys, time
            sys.path.insert(0, {str(src_root)!r})
            os.environ["MM_TEST_CACHE_DIR"] = {str(cache_dir)!r}

            from pathlib import Path
            from mind_meld import upgrade, pullhistory, statedb
            cache_dir = Path({str(cache_dir)!r})
            upgrade.CACHE_DIR = cache_dir
            upgrade.CACHE_PATH = cache_dir / "upgrade-state.json"
            pullhistory.HISTORY_DIR = cache_dir
            statedb.DB_DIR = cache_dir

            # Force __version__ to 0.9.4 so transition fires (subprocess imports
            # might otherwise read 0.0.0+dev from a source-tree install).
            import mind_meld
            mind_meld.__version__ = "0.9.4"
            upgrade.__version__ = "0.9.4"

            # Barrier: both workers wait until the file's content reaches "GO".
            # Coordinator writes "GO" once both have created their pre-files.
            barrier = Path({str(barrier)!r})
            (barrier.parent / f"ready-{{os.getpid()}}").write_text("ready")
            deadline = time.time() + 10
            while time.time() < deadline:
                if barrier.exists() and barrier.read_text() == "GO":
                    break
                time.sleep(0.005)

            upgrade.run_transition_hook(config={{"device": {{"id": "dev-test"}}}})
        """).strip()

        p1 = subprocess.Popen([sys.executable, "-c", worker], stderr=subprocess.PIPE)
        p2 = subprocess.Popen([sys.executable, "-c", worker], stderr=subprocess.PIPE)

        # Wait for both to be ready
        deadline = time.time() + 10
        while time.time() < deadline:
            ready_files = list(tmp_path.glob("ready-*"))
            if len(ready_files) >= 2:
                break
            time.sleep(0.01)

        # Release both at once
        barrier.write_text("GO")

        p1.wait(timeout=15)
        p2.wait(timeout=15)

        if p1.returncode != 0 or p2.returncode != 0:
            err1 = p1.stderr.read().decode() if p1.stderr else ""
            err2 = p2.stderr.read().decode() if p2.stderr else ""
            pytest.fail(
                f"subprocess exited non-zero: p1={p1.returncode} err={err1!r} "
                f"p2={p2.returncode} err={err2!r}"
            )

        monkeypatch.setattr(statedb, "DB_DIR", cache_dir)
        rows = pullhistory.read_records()
        upgrade_rows = [r for r in rows if r.get("verb") == "self-upgrade"]
        assert len(upgrade_rows) == 1, (
            f"expected exactly 1 self-upgrade row under flock-protected "
            f"read-compare-write, got {len(upgrade_rows)}: {upgrade_rows}"
        )


class TestCachedUpgradeView60A:
    @pytest.mark.parametrize("state", ["missing", "malformed", "stale", "due", "current"])
    def test_cache_only_view(self, monkeypatch, state):
        _set_version(monkeypatch, "0.14.12")
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        if state == "malformed":
            upgrade.CACHE_PATH.write_text("bad json")
        elif state != "missing":
            age = timedelta(days=3) if state == "stale" else timedelta(hours=1)
            upgrade.CACHE_PATH.write_text(
                json.dumps(
                    {
                        "latest_version": "0.14.12" if state == "current" else "0.15.0",
                        "checked_at": (now - age).isoformat(),
                        "last_nudged_at": "2000-01-01T00:00:00+00:00",
                    }
                )
            )
        before = (
            (upgrade.CACHE_PATH.read_bytes(), upgrade.CACHE_PATH.stat().st_mtime_ns)
            if upgrade.CACHE_PATH.exists()
            else None
        )
        monkeypatch.setattr(upgrade, "_fetch_tags", lambda *a: pytest.fail("inspection fetched"))
        result = upgrade.cached_upgrade_view({}, now=now)
        after = (
            (upgrade.CACHE_PATH.read_bytes(), upgrade.CACHE_PATH.stat().st_mtime_ns)
            if upgrade.CACHE_PATH.exists()
            else None
        )
        assert before == after
        assert result.state == (
            "unknown"
            if state in ("missing", "malformed")
            else "current"
            if state == "current"
            else "upgrade-available"
        )
        assert result.stale == (state == "stale")
        assert result.should_nudge is False
        if state == "stale":
            assert now - result.checked_at == timedelta(days=3)

    @pytest.mark.parametrize("skip", ["dev", "flag", "config"])
    def test_skip_modes_do_not_read_cache(self, monkeypatch, skip):
        _set_version(monkeypatch, upgrade.DEV_BUILD_SENTINEL if skip == "dev" else "0.14.12")
        upgrade.set_invocation_skip(skip == "flag")
        monkeypatch.setattr(
            upgrade, "locked_json_snapshot", lambda *a, **kw: pytest.fail("skip read cache")
        )
        result = upgrade.cached_upgrade_view({"upgrade": {"auto_check": skip != "config"}})
        assert result.state == "skip"

    def test_contended_cache_returns_unknown_immediately(self, monkeypatch):
        from mind_meld.lockedjson import locked_json_rmw

        _set_version(monkeypatch, "0.14.12")
        with locked_json_rmw(upgrade.CACHE_PATH):
            start = time.monotonic()
            result = upgrade.cached_upgrade_view({})
            assert time.monotonic() - start < 1
            assert result.state == "unknown"
            assert result.cache_state == "lock_failed"

    @pytest.mark.parametrize(
        "data",
        [
            [],
            {},
            {"latest_version": 123, "checked_at": "bad"},
            {"latest_version": "bogus", "checked_at": "2026-09-15"},
        ],
    )
    def test_malformed_cache_shapes_are_unknown(self, monkeypatch, data):
        _set_version(monkeypatch, "0.14.12")
        upgrade.CACHE_PATH.write_text(json.dumps(data))
        assert upgrade.cached_upgrade_view({}).state == "unknown"
