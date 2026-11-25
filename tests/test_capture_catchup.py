"""Tests for Track 165C: single-flight detached capture-only background catch-up."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mind_meld import (
    capturecatchup,
    cli,
    config,
    core,
    crypto,
    devices,
    events,
    events_tail,
    host_usage,
    lockfile,
    manifest,
    pushtimings,
    sidecar,
    token_usage,
)
from mind_meld.hosts import codex as codex_host
from mind_meld.hosts import grok as grok_host
from mind_meld.storage import keys as storage_keys
from mind_meld.storage.local import LocalBackend

runner = CliRunner()


def _make_git_repo(path: Path, *, email: str = "user@example.com") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "User",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "User",
        "GIT_COMMITTER_EMAIL": email,
    }
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, env=env
    )
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://example.com/user/{path.name}.git"],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )
    (path / "README.md").write_text(f"# {path.name}\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, env=env
    )
    return path


def _setup_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, include_codex: bool = False):
    """Create a minimal initialized single-device fleet in tmp_path."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "hello.md").write_text("hello world\n")
    events_root = tmp_path / "mm_events"
    (events_root / "events").mkdir(parents=True, exist_ok=True)

    sources = [
        {
            "name": "notes",
            "path": str(notes_dir),
            "type": "generic",
            "include_dirs": ["."],
            "exclude_patterns": [],
        },
        {
            "name": "mm-events",
            "path": str(events_root),
            "type": "generic",
            "include_dirs": ["events"],
            "exclude_patterns": [],
        },
    ]
    if include_codex:
        codex_dir = tmp_path / "codex_src"
        (codex_dir / "skills").mkdir(parents=True, exist_ok=True)
        sources.append(
            {
                "name": "codex",
                "path": str(codex_dir),
                "type": "generic",
                "include_dirs": ["skills"],
                "exclude_patterns": [],
            }
        )

    device_id = "dev-catchup-1"
    cfg = {
        "device": {"id": device_id, "name": "mac-a"},
        "storage": {"backend": "local", "path": str(storage_dir)},
        "sync": {"sources": sources},
        "retro": {"author_emails": ["user@example.com"]},
    }

    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_dir / "config.toml")
    monkeypatch.setattr(config, "LOCK_PATH", cfg_dir / "mind-meld.lock")
    monkeypatch.setattr(lockfile, "LOCK_PATH", cfg_dir / "mind-meld.lock")
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", cfg_dir)
    monkeypatch.setattr(capturecatchup, "CATCHUP_LOCK_PATH", cfg_dir / "capture-catchup.lock")
    monkeypatch.setattr(capturecatchup, "CATCHUP_STATE_PATH", cfg_dir / "capture-catchup.json")
    monkeypatch.setattr(pushtimings, "PUSH_TIMINGS_PATH", cfg_dir / "push-timings.json")
    monkeypatch.setattr(token_usage, "CACHE_PATH", cfg_dir / "session-tokens.json")
    monkeypatch.setattr(codex_host, "CACHE_PATH", cfg_dir / "host-tokens.json")
    monkeypatch.setattr(grok_host, "CACHE_PATH", cfg_dir / "grok-host-tokens.json")
    monkeypatch.setattr(host_usage, "MUSE_CACHE_PATH", cfg_dir / "muse-host-tokens.json")
    monkeypatch.setattr(host_usage, "AGY_CACHE_PATH", cfg_dir / "agy-host-tokens.json")
    monkeypatch.setattr(crypto, "get_passphrase", lambda **kw: "test-passphrase")
    monkeypatch.setattr(cli, "get_passphrase", lambda **kw: "test-passphrase")
    monkeypatch.setattr(core, "get_passphrase", lambda **kw: "test-passphrase")

    config.save_config(cfg)
    backend = LocalBackend(storage_dir)
    crypto.bootstrap_crypto_init(backend, "test-passphrase", 1024)
    devices.register_device(backend, device_id, "mac-a")

    return {
        "cfg": cfg,
        "cfg_dir": cfg_dir,
        "storage_dir": storage_dir,
        "notes_dir": notes_dir,
        "events_root": events_root,
        "events_dir": events_root / "events",
        "device_id": device_id,
        "backend": backend,
    }


# --- T1: Primitives ---


def test_is_lock_held_read_only_and_cross_process(tmp_path):
    lock_path = tmp_path / "probe.lock"
    assert not lockfile.is_lock_held(lock_path)
    assert not lock_path.exists()

    lock_path.write_text("12345")
    assert not lockfile.is_lock_held(lock_path)

    lockfile.acquire_lock(lock_path)
    try:
        assert lockfile.is_lock_held(lock_path)
    finally:
        lockfile.release_lock(lock_path)
    assert not lockfile.is_lock_held(lock_path)

    # Cross-process probe
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time; "
                "f = open(sys.argv[1], 'a+'); "
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX); "
                "print('LOCKED', flush=True); "
                "sys.stdin.read()"
            ),
            str(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "LOCKED"
        assert lockfile.is_lock_held(lock_path)
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=5)
    assert not lockfile.is_lock_held(lock_path)


def test_degradations_and_deadline_stopped_phases():
    d1 = events_tail.Degradations()
    d1.append("notice one")
    d1.deadline_notices = 1
    d1.deadline_phases = ("host:codex", "git_walk")

    d2 = events_tail.Degradations()
    d2.append("notice two")
    d2.deadline_notices = 2
    d2.deadline_phases = ("git_walk", "session_scan")

    d1.extend(d2)
    assert d1.deadline_notices == 3
    assert d1.deadline_phases == ("host:codex", "git_walk", "session_scan")

    capture = events_tail.CaptureResult(
        git_rows=[],
        session_rows=[],
        host_rows=[],
        host_capture=events_tail.HostUsageCapture(
            None,
            "codex",
            "deadline",
            dropped=(("codex", "deadline"), ("muse", "malformed")),
        ),
        root_discovery=events.GitRootDiscovery((), (events.GIT_ROOT_DISCOVERY_BUDGET_ERROR,), True),
        discovery_errors=(events.GIT_ROOT_DISCOVERY_BUDGET_ERROR,),
        session_walk_exceeded_budget=True,
        warn_lock_unavailable=False,
        token_cache_requested=True,
        walk_budget_aborts=2,
    )
    assert events_tail.deadline_stopped_phases(capture) == (
        "host:codex",
        "git_discovery",
        "git_walk",
        "session_scan",
    )

    sweep_veto = events_tail.CaptureResult(
        git_rows=[],
        session_rows=[],
        host_rows=[],
        host_capture=events_tail.HostUsageCapture(None, None, "deadline", dropped=()),
        root_discovery=events.GitRootDiscovery((), (), False),
        discovery_errors=(),
        session_walk_exceeded_budget=False,
        warn_lock_unavailable=False,
        token_cache_requested=True,
    )
    assert events_tail.deadline_stopped_phases(sweep_veto) == ("host:all",)


def test_commit_recorded_git_rows_returns_bool(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    now = datetime.now(timezone.utc)
    assert (
        events_tail._commit_recorded_git_rows(events_dir, "dev-1", [], since=now, complete=True)
        is True
    )

    row = {
        "v": events.EVENTS_SCHEMA_VERSION,
        "type": "git-snapshot",
        "ts": now.isoformat(),
        "device": "dev-1",
        "origin": events.GIT_SNAPSHOT_ORIGIN_CATCHUP,
        "projects": [],
        "skipped": [],
        "git_capture": events.make_git_capture(
            since=now, discovery="complete", walk_budget_aborts=0, walk_errors=0
        ),
    }
    # Not written yet -> False
    assert (
        events_tail._commit_recorded_git_rows(events_dir, "dev-1", [row], since=now, complete=True)
        is False
    )

    # Write and confirm -> True
    events.write_push_event(events_dir, "dev-1", [row], strict=True)
    assert (
        events_tail._commit_recorded_git_rows(events_dir, "dev-1", [row], since=now, complete=True)
        is True
    )


def test_discover_git_roots_before_read_cancellation(tmp_path, monkeypatch):
    """F36: revoking a source during discovery cancels before next prober and before persist."""
    repo = _make_git_repo(tmp_path / "repo-a")
    cfg = {
        "sync": {"sources": [{"name": "claude", "path": str(tmp_path / "claude")}]},
        "retro": {"repo_roots": [str(repo)]},
    }
    calls = []

    def cancel():
        calls.append("checked")
        raise RuntimeError("config changed")

    with pytest.raises(RuntimeError, match="config changed"):
        events.discover_git_roots(cfg, persist=True, before_read=cancel)
    assert calls == ["checked"]
    assert not config.GIT_CAPTURE_CACHE_PATH.exists()


def test_warm_token_cache_inline_before_read_cancellation(tmp_path, monkeypatch):
    """F37: revoking Claude during token-cache lock wait cancels before scanning claude_dir."""
    monkeypatch.setattr(token_usage, "CACHE_PATH", tmp_path / "session-tokens.json")
    claude_dir = tmp_path / "claude"
    proj = claude_dir / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    (proj / "sess.jsonl").write_text('{"type":"assistant"}\n')

    def cancel():
        raise RuntimeError("claude disabled")

    with pytest.raises(RuntimeError, match="claude disabled"):
        token_usage.warm_token_cache_inline([claude_dir], before_read=cancel)


def test_pushtimings_catchup_kind_round_trip():
    phases = (
        pushtimings.PhaseTiming("host:codex", "Codex usage", 120, budget_ms=18000, status="ok"),
        pushtimings.PhaseTiming("git_walk", "Git walk", 80, budget_ms=2000, status="ok"),
    )
    pushtimings.record_push_timings("catchup", phases, total_ms=200)
    data = pushtimings.read_push_timings()
    assert "catchup" in data["by_kind"]
    assert data["by_kind"]["catchup"]["total_ms"] == 200
    assert data["latest"]["kind"] == "catchup"


def test_preserve_budget_for_keeps_step1_allowance():
    """F27: Step 2 re-sweep keeps Step 1's shared host allowance while summing elapsed_ms."""
    timer = pushtimings.PhaseTimer()
    timer.record("host:codex", "Codex usage", 400, budget_ms=18000, status="ok")

    def fake_reader(*, deadline: float):
        return host_usage.HostUsageResult(hosts={}, complete=True)

    with events_tail.phase_timer_scope(timer):
        events_tail._capture_host_usage(
            [("codex", fake_reader)],
            deadline=100.0,
            now=lambda: 99.9,
            concurrent=False,
            preserve_budget_for=frozenset({"codex"}),
        )
    by_phase = {p.phase: p for p in timer.phases()}
    assert by_phase["host:codex"].budget_ms == 18000
    assert by_phase["host:codex"].elapsed_ms >= 400


# --- T2: Launcher, single-flight, autopush hand-off, capture-only worker ---


def test_start_detached_catchup_spawns_detached_process(monkeypatch):
    monkeypatch.setattr(
        capturecatchup, "start_detached_catchup", capturecatchup.start_detached_catchup
    )
    # Re-bind real function directly from module source (conftest stubs it)
    import importlib

    real_mod = importlib.reload(capturecatchup)
    mock_proc = MagicMock()
    popen_mock = MagicMock(return_value=mock_proc)
    monkeypatch.setattr(real_mod.subprocess, "Popen", popen_mock)
    monkeypatch.setattr(real_mod, "is_catchup_running", lambda: False)

    assert real_mod.start_detached_catchup([]) is False
    popen_mock.assert_not_called()

    assert real_mod.start_detached_catchup(["host:codex", "git_walk", "host:codex"]) is True
    popen_mock.assert_called_once()
    argv = popen_mock.call_args.args[0]
    kwargs = popen_mock.call_args.kwargs
    assert argv == [
        sys.executable,
        "-P",
        "-m",
        "mind_meld.capturecatchup",
        "--phase=host:codex",
        "--phase=git_walk",
    ]
    assert kwargs["cwd"] == "/"
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    mock_proc.wait.assert_not_called()
    mock_proc.communicate.assert_not_called()


def test_single_flight_retrigger_and_bounded_followup(tmp_path, monkeypatch):
    """F28: concurrent trigger records retrigger; active worker runs at most 1 follow-up."""
    import importlib

    real_mod = importlib.reload(capturecatchup)
    monkeypatch.setattr(real_mod, "CATCHUP_LOCK_PATH", tmp_path / "capture-catchup.lock")
    monkeypatch.setattr(real_mod, "CATCHUP_STATE_PATH", tmp_path / "capture-catchup.json")

    popen_mock = MagicMock()
    monkeypatch.setattr(real_mod.subprocess, "Popen", popen_mock)

    # Hold L1: start_detached_catchup records retrigger and does not spawn; run_catchup exits None.
    lockfile.acquire_lock(real_mod.CATCHUP_LOCK_PATH)
    try:
        assert real_mod.start_detached_catchup(["git_walk"]) is False
        popen_mock.assert_not_called()
        assert real_mod.run_catchup(["git_walk"]) is None
        st = real_mod.read_catchup_state()
        assert st.get("retrigger_phases") == ["git_walk"]
        assert st.get("retrigger_at") is not None
    finally:
        lockfile.release_lock(real_mod.CATCHUP_LOCK_PATH)

    # Clear the prior retrigger marker before testing bounded follow-up pass
    real_mod.CATCHUP_STATE_PATH.unlink()

    # Now run real run_catchup with a fake _run_catchup_pass that injects a retrigger on pass 1
    # and another on pass 2 (proving at-most-one follow-up pass).
    pass_calls: list[list[str]] = []

    def fake_pass(phases, timer):
        pass_calls.append(list(phases))
        started = datetime.now(timezone.utc)
        # Simulate concurrent hook recording a newer retrigger during pass 1 and pass 2
        real_mod._record_retrigger([f"phase-{len(pass_calls)}"])
        return {
            "result": "ok",
            "detail": None,
            "event_file": "events/dev-2026-11-24.jsonl",
            "event_sha256": "abc123",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "step2_started_at": started - timedelta(seconds=1),
        }

    monkeypatch.setattr(real_mod, "_run_catchup_pass", fake_pass)
    res = real_mod.run_catchup(["host:codex"])
    assert res is not None
    assert res["result"] == "ok"
    assert len(pass_calls) == 2
    assert pass_calls[0] == ["host:codex"]
    assert pass_calls[1] == ["phase-1"]
    assert not lockfile.is_lock_held(real_mod.CATCHUP_LOCK_PATH)


@pytest.mark.parametrize(
    "phase_kind",
    ["host:codex", "git_discovery", "git_walk", "session_scan"],
)
def test_autopush_triggers_catchup_after_lock_release(tmp_path, monkeypatch, phase_kind):
    env = _setup_fleet(tmp_path, monkeypatch)
    launched: list[tuple[tuple[str, ...], bool]] = []

    def fake_launcher(phases):
        launched.append((tuple(phases), lockfile.is_lock_held(config.LOCK_PATH)))
        return True

    monkeypatch.setattr(capturecatchup, "start_detached_catchup", fake_launcher)

    def fake_tail(cfg, sources, device_id, *, dry_run, quiet, **kwargs):
        deg = events_tail.Degradations()
        deg.append(f"stopped at {phase_kind}")
        deg.deadline_notices = 1
        deg.deadline_phases = (phase_kind,)
        return deg

    monkeypatch.setattr(events_tail, "_run_events_tail", fake_tail)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["autopush"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert launched == [((phase_kind,), False)]

    crumb_path = env["cfg_dir"] / "last-autorun.json"
    crumb = json.loads(crumb_path.read_text())["push"]
    assert crumb["outcome"] == "degraded"
    assert crumb.get("capture_deadline_only") is True


def test_autopush_non_deadline_degradation_clears_capture_deadline_only(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    launched: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        capturecatchup,
        "start_detached_catchup",
        lambda phases: launched.append(tuple(phases)) or True,
    )

    def fake_tail(cfg, sources, device_id, *, dry_run, quiet, **kwargs):
        deg = events_tail.Degradations()
        deg.append("stopped at git_walk")
        deg.deadline_notices = 1
        deg.deadline_phases = ("git_walk",)
        deg.append("git walk had 1 error")  # non-deadline degradation
        return deg

    monkeypatch.setattr(events_tail, "_run_events_tail", fake_tail)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["autopush"])
    assert result.exit_code == 0
    assert launched == [("git_walk",)]
    crumb = json.loads((env["cfg_dir"] / "last-autorun.json").read_text())["push"]
    assert crumb.get("capture_deadline_only") is not True


def test_healthy_autopush_and_dry_run_never_start_catchup(tmp_path, monkeypatch):
    _setup_fleet(tmp_path, monkeypatch)
    launched: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        capturecatchup,
        "start_detached_catchup",
        lambda phases: launched.append(tuple(phases)) or True,
    )
    monkeypatch.setattr(events_tail, "_run_events_tail", lambda *a, **k: events_tail.Degradations())
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)

    res_dry = runner.invoke(cli.app, ["push", "--dry-run"])
    assert res_dry.exit_code == 0
    assert launched == []

    res_auto = runner.invoke(cli.app, ["autopush"])
    assert res_auto.exit_code == 0
    assert launched == []


def test_end_to_end_capture_only_and_next_push_drain(tmp_path, monkeypatch):
    """Capture-only run under L1 -> L2, then next autopush with zero user edits drains mm-events."""
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-e2e")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])

    # Initial push publishes notes/hello.md so remote manifest exists
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    # Ensure catch-up never reads keyring/passphrase
    def forbid_passphrase(**kw):
        raise AssertionError("catch-up must not access passphrase/keyring")

    monkeypatch.setattr(crypto, "get_passphrase", forbid_passphrase)
    monkeypatch.setattr(cli, "get_passphrase", forbid_passphrase)
    monkeypatch.setattr(core, "get_passphrase", forbid_passphrase)

    res = capturecatchup.run_catchup(["git_walk"])
    assert res is not None
    assert res["result"] == "ok", res
    assert res["event_file"] and res["event_file"].startswith("events/")
    assert res["event_sha256"]
    assert res["finished_at"] is not None

    # Verify catch-up wrote git-snapshot(origin="catchup") and NO mm-push row
    event_path = env["events_root"] / res["event_file"]
    rows = [json.loads(line) for line in event_path.read_text().splitlines() if line.strip()]
    catchup_git_rows = [
        r
        for r in rows
        if r.get("type") == "git-snapshot" and r.get("origin") == events.GIT_SNAPSHOT_ORIGIN_CATCHUP
    ]
    assert len(catchup_git_rows) == 1
    assert manifest.hash_file(event_path) == res["event_sha256"]

    # Verify catchup timings persisted
    timings = pushtimings.read_push_timings()
    assert "catchup" in timings["by_kind"]

    # Verify state persisted cleanly
    state, running = capturecatchup.catchup_observation()
    assert not running
    assert state["state"] == "finished"
    assert state["result"] == "ok"
    assert state["event_file"] == res["event_file"]
    assert state["event_sha256"] == res["event_sha256"]

    # Restore passphrase and run autopush with zero user edits: sees mm-events diff and publishes
    monkeypatch.setattr(crypto, "get_passphrase", lambda **kw: "test-passphrase")
    monkeypatch.setattr(cli, "get_passphrase", lambda **kw: "test-passphrase")
    monkeypatch.setattr(core, "get_passphrase", lambda **kw: "test-passphrase")
    res_drain = runner.invoke(cli.app, ["autopush"])
    assert res_drain.exit_code == 0
    assert res_drain.stdout == ""

    # Verify remote manifest now contains the catch-up event file with matching sha256
    mkey = storage_keys.manifest_key(env["device_id"])
    remote_manifest = json.loads(
        crypto.decrypt_manifest(env["backend"].get(mkey), "test-passphrase", memory_kb=1024)
    )
    remote_events = remote_manifest["sources"]["mm-events"]["files"]
    assert res["event_file"] in remote_events


def test_adversarial_empty_and_corrupt_git_capture_cache_refused(tmp_path, monkeypatch):
    """F42 & F41: zero-byte and corrupt git-capture-cache.json refused with explicit detail."""
    _setup_fleet(tmp_path, monkeypatch)

    # Zero-byte file -> "empty"
    config.GIT_CAPTURE_CACHE_PATH.write_bytes(b"")
    res = capturecatchup.run_catchup(["git_walk"])
    assert res is not None
    assert res["result"] == "failed"
    assert res["detail"] == "Git capture index unreadable; run mm diag"

    # Corrupt JSON -> "malformed"
    config.GIT_CAPTURE_CACHE_PATH.write_text("{not json")
    res2 = capturecatchup.run_catchup(["git_walk"])
    assert res2 is not None
    assert res2["result"] == "failed"
    assert res2["detail"] == "Git capture index unreadable; run mm diag"


def test_adversarial_config_changed_during_step2_defers(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-cfg")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])

    orig_discover = events.discover_git_roots

    def mutating_discover(cfg, **kwargs):
        # Mutate config mid-Step 2 before discovery finishes
        mutated = config.load_config()
        mutated["retro"]["author_emails"] = ["other@example.com"]
        config.save_config(mutated)
        return orig_discover(cfg, **kwargs)

    monkeypatch.setattr(events, "discover_git_roots", mutating_discover)
    res = capturecatchup.run_catchup(["git_walk"])
    assert res is not None
    assert res["result"] == "deferred"
    assert res["detail"] == "capture configuration changed; run mm push"
    assert res["event_file"] is None


def test_adversarial_busy_sync_lock_defers_and_keeps_warmed_caches(tmp_path, monkeypatch):
    _setup_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(capturecatchup, "_SYNC_LOCK_BACKOFFS_S", (0.01,))
    warm_marker = tmp_path / "warmed"

    def fake_step1(phases, timer):
        warm_marker.write_text("yes")
        return frozenset({"codex"}), None

    monkeypatch.setattr(capturecatchup, "_warm_step1_caches", fake_step1)

    lockfile.acquire_lock(config.LOCK_PATH)
    try:
        res = capturecatchup.run_catchup(["host:codex"])
    finally:
        lockfile.release_lock(config.LOCK_PATH)

    assert res is not None
    assert res["result"] == "deferred"
    assert warm_marker.read_text() == "yes"


def test_adversarial_cold_claude_token_cache_in_step2_fails(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects").mkdir(parents=True)
    env["cfg"]["sync"]["sources"].append(
        {
            "name": "claude",
            "path": str(claude_dir),
            "type": "claude",
            "include_dirs": ["projects"],
            "exclude_patterns": [],
        }
    )
    repo = _make_git_repo(tmp_path / "repo-tok")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])

    # Skip Step 1 warming so cache remains cold entering Step 2
    monkeypatch.setattr(capturecatchup, "_warm_step1_caches", lambda p, t: (frozenset(), None))
    monkeypatch.setattr(token_usage, "is_cache_cold", lambda: True)

    res = capturecatchup.run_catchup(["session_scan"])
    assert res is not None
    assert res["result"] == "failed"
    assert res["detail"] == "Claude token cache unavailable"


def test_adversarial_still_deadline_stopped_records_partial(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-part")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])

    def fake_capture(*args, **kwargs):
        return events_tail.CaptureResult(
            git_rows=[],
            session_rows=[],
            host_rows=[],
            host_capture=events_tail.HostUsageCapture({}),
            root_discovery=events.GitRootDiscovery((repo,), (), False),
            discovery_errors=(),
            session_walk_exceeded_budget=False,
            warn_lock_unavailable=False,
            token_cache_requested=True,
            walk_budget_aborts=1,
            walk_errors=0,
        )

    monkeypatch.setattr(events_tail, "_capture_event_snapshots", fake_capture)
    res = capturecatchup.run_catchup(["git_walk"])
    assert res is not None
    assert res["result"] == "partial"


def test_adversarial_revoking_grok_before_step1_prevents_read(tmp_path, monkeypatch):
    """F22: revoking Grok consent before Step 1 read prevents Grok reader from running."""
    env = _setup_fleet(tmp_path, monkeypatch)
    env["cfg"]["retro"]["grok_host_usage"] = True
    config.save_config(env["cfg"])

    grok_called = []

    def fake_grok_reader(*args, **kwargs):
        grok_called.append(True)
        return host_usage.HostUsageResult(hosts={}, complete=True)

    monkeypatch.setattr(grok_host, "read_grok_usage", fake_grok_reader)

    # Intercept config.load_config: first call (Step 1 initial load) sees grok enabled;
    # subsequent calls (before_read consent recheck) see grok disabled.
    orig_load = config.load_config
    load_count = {"n": 0}

    def flaky_load():
        load_count["n"] += 1
        data = orig_load()
        if load_count["n"] >= 2:
            data = json.loads(json.dumps(data))
            data.setdefault("retro", {})["grok_host_usage"] = False
        return data

    monkeypatch.setattr(config, "load_config", flaky_load)
    capturecatchup._warm_step1_caches(["host:grok"], pushtimings.PhaseTimer())
    assert grok_called == []


def test_interrupted_worker_synthesizes_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(capturecatchup, "CATCHUP_LOCK_PATH", tmp_path / "capture-catchup.lock")
    monkeypatch.setattr(capturecatchup, "CATCHUP_STATE_PATH", tmp_path / "capture-catchup.json")
    capturecatchup.record_catchup_state(
        state="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        trigger_phases=["git_walk"],
    )
    state, running = capturecatchup.catchup_observation()
    assert not running
    assert state["state"] == "finished"
    assert state["result"] == "failed"
    assert state["detail"] == "worker interrupted"


def test_detached_module_subprocess_smoke(tmp_path):
    """Subprocess smoke of python -P -m mind_meld.capturecatchup in isolated HOME."""
    env = {**os.environ, "HOME": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-P", "-m", "mind_meld.capturecatchup", "--phase=git_walk"],
        cwd="/",
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


# --- T3: mm status suppression & mm diag reporting ---


def test_status_and_diag_create_zero_catchup_files(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    lock_p = env["cfg_dir"] / "capture-catchup.lock"
    state_p = env["cfg_dir"] / "capture-catchup.json"
    assert not lock_p.exists()
    assert not state_p.exists()

    res_st = runner.invoke(cli.app, ["status"])
    assert res_st.exit_code == 0
    assert not lock_p.exists()
    assert not state_p.exists()

    res_diag = runner.invoke(cli.app, ["diag", "--json"])
    assert res_diag.exit_code == 0
    payload = json.loads(res_diag.stdout)
    assert payload["capture_catchup"]["state"] == "idle"
    assert payload["capture_catchup"]["running"] is False
    assert not lock_p.exists()
    assert not state_p.exists()


def test_status_suppresses_covered_backlog_and_restores_on_user_edit(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    # Simulate a deadline-only degraded autopush breadcrumb
    now = datetime.now(timezone.utc)
    cli._write_autorun_breadcrumb(
        "push",
        "degraded",
        "git walk hit budget",
        capture_deadline_only=True,
    )

    # (a) Running catch-up suppresses manual push advice
    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        capturecatchup.record_catchup_state(
            state="running",
            started_at=now.isoformat(),
            trigger_phases=["git_walk"],
        )
        res_running = runner.invoke(cli.app, ["status"])
        assert res_running.exit_code == 0
        running_text = " ".join(res_running.stdout.split())
        assert "Background capture catch-up: running" in running_text
        assert "capture deadline backlog in progress" in running_text
        assert "Run: mm push" not in running_text
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)

    # (b) Finished ok catch-up with queued local mm-events file
    day_file = env["events_dir"] / f"{env['device_id']}-2026-11-24.jsonl"
    row = {
        "v": events.EVENTS_SCHEMA_VERSION,
        "type": "git-snapshot",
        "ts": now.isoformat(),
        "device": env["device_id"],
        "origin": events.GIT_SNAPSHOT_ORIGIN_CATCHUP,
        "projects": [],
        "skipped": [],
    }
    day_file.write_text(json.dumps(row) + "\n")
    sha = manifest.hash_file(day_file)
    finished = (now + timedelta(seconds=5)).isoformat()
    capturecatchup.record_catchup_state(
        state="finished",
        result="ok",
        started_at=now.isoformat(),
        finished_at=finished,
        trigger_phases=["git_walk"],
        event_file=f"events/{day_file.name}",
        event_sha256=sha,
    )

    res_ok = runner.invoke(cli.app, ["status"])
    assert res_ok.exit_code == 0
    ok_text = " ".join(res_ok.stdout.split())
    assert "In sync with this device's last push." in ok_text
    assert "Background capture catch-up: ok" in ok_text
    assert "ready for next push" in ok_text
    assert "background capture will publish on the next push." in ok_text
    assert "Run: mm push" not in ok_text

    # (c) Adding a user content edit immediately restores "waiting to push. Run: mm push"
    (env["notes_dir"] / "new_note.md").write_text("user edit\n")
    res_edit = runner.invoke(cli.app, ["status"])
    assert res_edit.exit_code == 0
    assert "waiting to push" in res_edit.stdout
    assert "Run: mm push" in res_edit.stdout


def test_status_failed_partial_and_non_deadline_degradations_surface(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    # (d) Failed catch-up surfaces action unless superseded by newer healthy push
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=now.isoformat(),
        finished_at=(now + timedelta(seconds=1)).isoformat(),
        trigger_phases=["git_walk"],
        detail="Git capture index unreadable; run mm diag",
    )
    res_fail = runner.invoke(cli.app, ["status"])
    assert res_fail.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_fail.stdout

    # Supersede with newer healthy autopush -> failure action clears
    cli._write_autorun_breadcrumb("push", "success")
    # Ensure auto_push timestamp is strictly after finished_at
    crumb_path = env["cfg_dir"] / "last-autorun.json"
    crumbs = json.loads(crumb_path.read_text())
    crumbs["push"]["timestamp"] = (now + timedelta(seconds=10)).isoformat()
    crumb_path.write_text(json.dumps(crumbs))

    res_superseded = runner.invoke(cli.app, ["status"])
    assert res_superseded.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res_superseded.stdout

    # (e) partial catch-up does NOT suppress manual advice on deadline-only degraded autopush
    cli._write_autorun_breadcrumb(
        "push",
        "degraded",
        "git walk hit budget",
        capture_deadline_only=True,
    )
    capturecatchup.record_catchup_state(
        state="finished",
        result="partial",
        started_at=now.isoformat(),
        finished_at=(now + timedelta(seconds=20)).isoformat(),
        trigger_phases=["git_walk"],
        detail="capture still stopped at deadline; run mm push",
    )
    res_partial = runner.invoke(cli.app, ["status"])
    assert res_partial.exit_code == 0
    assert "capture deadline backlog covered" not in res_partial.stdout


def test_diag_plain_text_and_json_render_catchup_and_timings(tmp_path, monkeypatch):
    _setup_fleet(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    capturecatchup.record_catchup_state(
        state="finished",
        result="ok",
        started_at=now,
        finished_at=now,
        trigger_phases=["host:codex", "git_walk"],
        event_file="events/dev-2026-11-24.jsonl",
        event_sha256="deadbeef",
    )
    pushtimings.record_push_timings(
        "catchup",
        (pushtimings.PhaseTiming("git_walk", "Git walk", 42, budget_ms=2000, status="ok"),),
        total_ms=42,
    )

    res_json = runner.invoke(cli.app, ["diag", "--json"])
    assert res_json.exit_code == 0
    payload = json.loads(res_json.stdout)
    assert payload["capture_catchup"]["result"] == "ok"
    assert payload["capture_catchup"]["trigger_phases"] == ["host:codex", "git_walk"]
    assert payload["push_timings"]["by_kind"]["catchup"]["total_ms"] == 42

    res_text = runner.invoke(cli.app, ["diag"])
    assert res_text.exit_code == 0
    assert "Background capture catch-up" in res_text.stdout
    assert "host:codex, git_walk" in res_text.stdout
    assert "catchup: 42 ms" in res_text.stdout
