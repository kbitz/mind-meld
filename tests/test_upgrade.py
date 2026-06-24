"""Tests for mind_meld.upgrade — auto-upgrade nudge.

Covers:
  - check_for_upgrade: short-circuits, cache freshness, network failure, parse
    failure, nudge gate (version + 24h), tag selection (prerelease / local /
    invalid filtered), pagination cap behavior.
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
import subprocess
import sys
import textwrap
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mind_meld import pullhistory, upgrade

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

    def _fake(_url: str = upgrade.TAGS_API_URL):
        return [{"name": n} for n in tag_names]

    monkeypatch.setattr(upgrade, "_fetch_tags", _fake)


def _stub_tags_raises(monkeypatch, exc: Exception) -> None:
    def _fake(_url: str = upgrade.TAGS_API_URL):
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


# ── _fetch_tags adapter (tested directly per Codex finding #3) ───────────


class TestFetchTagsAdapter:
    def test_url_includes_per_page_100(self, monkeypatch):
        captured: dict = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self):
                return b"[]"

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["timeout"] = timeout
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        result = upgrade._fetch_tags()
        assert result == []
        assert "per_page=100" in captured["url"]
        assert captured["timeout"] == upgrade.HTTP_TIMEOUT_SECONDS
        # Headers normalize to title-case in urllib's Request
        assert any("user-agent" in k.lower() for k in captured["headers"])
        assert any("accept" in k.lower() for k in captured["headers"])

    def test_non_array_response_raises(self, monkeypatch):
        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self):
                return b'{"not": "an array"}'

        monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: _FakeResp())
        with pytest.raises(json.JSONDecodeError):
            upgrade._fetch_tags()


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

    def test_two_processes_exactly_one_row(self, tmp_path):
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
            from mind_meld import upgrade, pullhistory
            cache_dir = Path({str(cache_dir)!r})
            upgrade.CACHE_DIR = cache_dir
            upgrade.CACHE_PATH = cache_dir / "upgrade-state.json"
            pullhistory.HISTORY_DIR = cache_dir

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

        # Load pullhistory rows from the shared dir
        history = cache_dir / "pull-history.jsonl"
        if not history.exists():
            pytest.fail("pull-history.jsonl was never written")

        rows = []
        with open(history) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        upgrade_rows = [r for r in rows if r.get("verb") == "self-upgrade"]
        assert len(upgrade_rows) == 1, (
            f"expected exactly 1 self-upgrade row under flock-protected "
            f"read-compare-write, got {len(upgrade_rows)}: {upgrade_rows}"
        )
