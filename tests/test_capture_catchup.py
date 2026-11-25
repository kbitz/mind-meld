"""Tests for Track 165C: single-flight detached capture-only background catch-up."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mind_meld import (
    attemptlog,
    capturecatchup,
    cli,
    config,
    core,
    crypto,
    devices,
    events,
    events_tail,
    host_registry,
    host_usage,
    identity,
    lockedjson,
    lockfile,
    manifest,
    pushtimings,
    sidecar,
    statusflow,
    token_usage,
)
from mind_meld.hosts import codex as codex_host
from mind_meld.hosts import grok as grok_host
from mind_meld.storage import keys as storage_keys
from mind_meld.storage.local import LocalBackend

runner = CliRunner()
_START_DETACHED_CATCHUP = capturecatchup.start_detached_catchup


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


def test_start_detached_catchup_spawns_detached_process(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_proc = MagicMock()
    popen_mock = MagicMock(return_value=mock_proc)
    monkeypatch.setattr(capturecatchup.subprocess, "Popen", popen_mock)

    assert _START_DETACHED_CATCHUP([]) is False
    popen_mock.assert_not_called()

    assert _START_DETACHED_CATCHUP(["host:codex", "git_walk", "host:codex"]) is True
    popen_mock.assert_called_once()
    argv = popen_mock.call_args.args[0]
    kwargs = popen_mock.call_args.kwargs
    (lock_fd,) = kwargs["pass_fds"]
    assert argv == [
        sys.executable,
        "-P",
        "-m",
        "mind_meld.capturecatchup",
        f"--lock-fd={lock_fd}",
        "--phase=host:codex",
        "--phase=git_walk",
    ]
    assert "cwd" not in kwargs
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    mock_proc.wait.assert_not_called()
    mock_proc.communicate.assert_not_called()
    assert not lockfile.is_lock_held(capturecatchup.CATCHUP_LOCK_PATH)


def test_launch_reserves_lock_before_child_and_hands_off_descriptor(monkeypatch):
    inherited = None

    def launch(*args, pass_fds, **kwargs):
        nonlocal inherited
        assert lockfile.is_lock_held(capturecatchup.CATCHUP_LOCK_PATH)
        assert _START_DETACHED_CATCHUP(["session_scan"]) is False
        inherited = os.dup(pass_fds[0])

    monkeypatch.setattr(capturecatchup.subprocess, "Popen", launch)
    try:
        assert _START_DETACHED_CATCHUP(["git_walk"]) is True
        assert lockfile.is_lock_held(capturecatchup.CATCHUP_LOCK_PATH)
        assert capturecatchup.read_catchup_state()["retrigger_phases"] == ["session_scan"]
    finally:
        if inherited is not None:
            os.close(inherited)
    assert not lockfile.is_lock_held(capturecatchup.CATCHUP_LOCK_PATH)


def test_single_flight_retrigger_and_bounded_followup(monkeypatch):
    """F28: concurrent trigger records retrigger; active worker runs at most 1 follow-up."""
    real_mod = capturecatchup
    popen_mock = MagicMock()
    monkeypatch.setattr(real_mod.subprocess, "Popen", popen_mock)

    # Hold L1: start_detached_catchup records retrigger and does not spawn; run_catchup exits None.
    lockfile.acquire_lock(real_mod.CATCHUP_LOCK_PATH)
    try:
        assert _START_DETACHED_CATCHUP(["git_walk"]) is False
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
    assert real_mod.read_catchup_state()["retrigger_phases"] == ["phase-2"]
    assert not lockfile.is_lock_held(real_mod.CATCHUP_LOCK_PATH)


@pytest.mark.parametrize("git_identity_due", [False, True])
def test_catchup_retrigger_during_step1_warms_newly_requested_host(
    tmp_path, monkeypatch, git_identity_due
):
    env = _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    repo = _make_git_repo(tmp_path / "repo-retrigger")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    if git_identity_due:
        refreshed = datetime.now(timezone.utc) - timedelta(seconds=identity.TTL_SECONDS + 1)
        identity.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "refreshed_at": refreshed.isoformat(),
                    "emails": ["user@example.com"],
                }
            )
        )

    warmed = []

    def read_codex(**kwargs):
        if not warmed:
            return host_usage.HostUsageResult(hosts=None, complete=False, reason="deadline")
        return host_usage.HostUsageResult(hosts={}, complete=True)

    def warm_codex(*, reader, **kwargs):
        assert not lockfile.is_lock_held(config.LOCK_PATH)
        warmed.append(reader)
        return host_usage.HostUsageResult(hosts={}, complete=True)

    monkeypatch.setattr(host_registry, "reader_cache_cold", lambda *args, **kwargs: False)
    monkeypatch.setattr(codex_host, "read_codex_usage", read_codex)
    monkeypatch.setattr(host_registry, "warm_host_cache_inline", warm_codex)
    warm_step1 = capturecatchup._warm_step1_caches
    sent = []

    def concurrent_trigger(phases, timer):
        result = warm_step1(phases, timer)
        if not sent:
            assert not _START_DETACHED_CATCHUP(["host:codex"])
            sent.append(True)
        return result

    monkeypatch.setattr(capturecatchup, "_warm_step1_caches", concurrent_trigger)
    result = capturecatchup.run_catchup(["git_walk"])
    assert result["result"] == ("deferred" if git_identity_due else "ok"), result
    assert warmed == ["codex"]


def test_catchup_reservation_keeps_trigger_through_shared_status_probe(tmp_path, monkeypatch):
    _setup_fleet(tmp_path, monkeypatch)
    lock = capturecatchup.CATCHUP_LOCK_PATH
    lock.touch()
    with lock.open("r") as reader:
        fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert not _START_DETACHED_CATCHUP(["git_walk"])
    assert capturecatchup.read_catchup_state().get("retrigger_phases") == ["git_walk"]


def test_catchup_launch_survives_removed_cwd_for_absolute_sources(tmp_path, monkeypatch):
    _setup_fleet(tmp_path, monkeypatch)
    popen_calls = []

    def cwd_removed():
        raise FileNotFoundError("fixture working directory was removed")

    monkeypatch.setattr(
        capturecatchup.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append(args)
    )
    with monkeypatch.context() as patch:
        patch.setattr(Path, "cwd", cwd_removed)
        started = _START_DETACHED_CATCHUP(["git_walk"])
    assert started
    assert len(popen_calls) == 1


def test_worker_final_state_retries_after_a_concurrent_inspection(monkeypatch):
    held = None

    def capture(phases, timer):
        nonlocal held
        held = os.open(capturecatchup.CATCHUP_STATE_PATH, os.O_RDONLY)
        fcntl.flock(held, fcntl.LOCK_SH)
        started = datetime.now(timezone.utc)
        return {
            "result": "ok",
            "detail": None,
            "event_file": None,
            "event_sha256": None,
            "finished_at": started.isoformat(),
            "step2_started_at": started,
        }

    def let_inspection_finish(_delay):
        nonlocal held
        if held is not None:
            os.close(held)
            held = None

    monkeypatch.setattr(capturecatchup, "_run_catchup_pass", capture)
    monkeypatch.setattr(lockedjson.time, "sleep", let_inspection_finish)

    try:
        assert capturecatchup.run_catchup(["git_walk"])["result"] == "ok"
        state, running = capturecatchup.catchup_observation()
        assert not running
        assert state["state"] == "finished"
        assert state["result"] == "ok"
    finally:
        let_inspection_finish(0)


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


@pytest.mark.parametrize("phase", ["git_walk", "session_scan", "git_discovery", "host:codex"])
def test_real_event_tail_marks_pure_deadline_breadcrumb(tmp_path, monkeypatch, phase):
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-deadline")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    original_capture = events_tail._capture_event_snapshots

    def deadline_capture(*args, **kwargs):
        captured = original_capture(*args, **kwargs)
        if phase == "git_discovery":
            errors = (events.GIT_ROOT_DISCOVERY_BUDGET_ERROR,)
            return replace(
                captured,
                root_discovery=events.GitRootDiscovery((repo,), errors, True),
                discovery_errors=errors,
            )
        if phase == "host:codex":
            return replace(
                captured,
                host_capture=events_tail.HostUsageCapture(
                    None, "codex", "deadline", dropped=(("codex", "deadline"),)
                ),
            )
        return replace(
            captured,
            walk_budget_aborts=int(phase == "git_walk"),
            session_walk_exceeded_budget=phase == "session_scan",
        )

    launched = []
    monkeypatch.setattr(events_tail, "_capture_event_snapshots", deadline_capture)
    monkeypatch.setattr(
        capturecatchup, "start_detached_catchup", lambda phases: launched.extend(phases)
    )
    result = runner.invoke(cli.app, ["autopush"])
    assert result.exit_code == 0
    assert result.stdout == result.stderr == ""
    assert launched == [phase]
    bread_crumb = json.loads((env["cfg_dir"] / "last-autorun.json").read_text())["push"]
    assert bread_crumb["outcome"] == "degraded"
    assert bread_crumb["capture_deadline_only"] is True


def test_end_to_end_capture_only_and_next_push_drain(tmp_path, monkeypatch):
    """Capture-only run under L1 -> L2, then next autopush with zero user edits drains mm-events."""
    env = _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    repo = _make_git_repo(tmp_path / "repo-e2e")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    claude_dir = tmp_path / "claude"
    _add_claude_source(env, claude_dir, "claude")
    project = claude_dir / "projects" / "-Users-user-repo-e2e"
    project.mkdir()
    (project / "session.jsonl").write_text(
        json.dumps(
            {
                "cwd": str(repo),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {
                    "role": "assistant",
                    "id": "msg-test",
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                },
            }
        )
        + "\n"
    )
    config.save_config(env["cfg"])

    warmed = []
    warming_in_progress = False

    def read_codex(**kwargs):
        if warming_in_progress and not lockfile.is_lock_held(config.LOCK_PATH) and not warmed:
            return host_usage.HostUsageResult(hosts=None, complete=False, reason="deadline")
        return host_usage.HostUsageResult(hosts={}, complete=True)

    def warm_host(*, reader, **kwargs):
        assert not lockfile.is_lock_held(config.LOCK_PATH)
        warmed.append(reader)
        return host_usage.HostUsageResult(hosts={}, complete=True)

    monkeypatch.setattr(codex_host, "read_codex_usage", read_codex)
    monkeypatch.setattr(host_registry, "warm_host_cache_inline", warm_host)
    warm_claude_cache = token_usage.warm_token_cache_inline

    def warm_claude(*args, **kwargs):
        assert not lockfile.is_lock_held(config.LOCK_PATH)
        warmed.append("claude")
        return warm_claude_cache(*args, **kwargs)

    monkeypatch.setattr(token_usage, "warm_token_cache_inline", warm_claude)

    # Initial push publishes notes/hello.md so remote manifest exists
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    monkeypatch.setattr(
        identity, "_gather_gh_noreply_email", lambda: "5+user@users.noreply.github.com"
    )
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    mkey = storage_keys.manifest_key(env["device_id"])
    before_manifest = env["backend"].get(mkey)
    before_cursors = events.read_git_capture_cache()["repo_cursors"]
    assert token_usage.is_cache_cold()
    before_rows = events.latest_event_rows(env["events_dir"], env["device_id"], {"mm-push"}).rows

    # Ensure catch-up never reads keyring/passphrase
    def forbid_passphrase(**kw):
        raise AssertionError("catch-up must not access passphrase/keyring")

    monkeypatch.setattr(crypto, "get_passphrase", forbid_passphrase)
    monkeypatch.setattr(cli, "get_passphrase", forbid_passphrase)
    monkeypatch.setattr(core, "get_passphrase", forbid_passphrase)

    released_at = []
    release = lockfile.release_lock

    def observe_release(path=None, **kwargs):
        if path == config.LOCK_PATH:
            released_at.append(datetime.now(timezone.utc))
        release(path, **kwargs)

    warming_in_progress = True
    with monkeypatch.context() as patch:
        patch.setattr(lockfile, "release_lock", observe_release)
        res = capturecatchup.run_catchup(["git_walk", "host:codex"])
    warming_in_progress = False
    assert res is not None
    assert res["result"] == "ok", res
    assert res["event_file"] and res["event_file"].startswith("events/")
    assert res["event_sha256"]
    assert events.parse_aware_ts(res["finished_at"]) <= released_at[0]
    assert warmed == ["codex", "claude"]
    assert not token_usage.is_cache_cold()
    assert env["backend"].get(mkey) == before_manifest
    after_cursors = events.read_git_capture_cache()["repo_cursors"]
    assert after_cursors[str(repo)] > before_cursors[str(repo)]
    assert (
        events.latest_event_rows(env["events_dir"], env["device_id"], {"mm-push"}).rows
        == before_rows
    )

    # Verify catch-up wrote git-snapshot(origin="catchup") and NO mm-push row
    event_path = env["events_root"] / res["event_file"]
    rows = [json.loads(line) for line in event_path.read_text().splitlines() if line.strip()]
    catchup_git_rows = [
        r
        for r in rows
        if r.get("type") == "git-snapshot" and r.get("origin") == events.GIT_SNAPSHOT_ORIGIN_CATCHUP
    ]
    assert len(catchup_git_rows) == 1
    catchup_rows = rows[-3:]
    assert [r["type"] for r in catchup_rows] == [
        "git-snapshot",
        "sessions-snapshot",
        "host-usage-snapshot",
    ]
    assert catchup_rows[0] == catchup_git_rows[0]
    assert len(catchup_rows[1]["projects"]) == 1
    assert catchup_rows[2]["token_sources"] == ["codex"]
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

    # The next push may append its own rows to the same file. Read the
    # accepted manifest's encrypted blob to verify the exact catch-up rows survived.
    remote_manifest = json.loads(
        crypto.decrypt_manifest(env["backend"].get(mkey), "test-passphrase", memory_kb=1024)
    )
    remote_events = remote_manifest["sources"]["mm-events"]["files"]
    digest = remote_events[res["event_file"]]["sha256"]
    published_blob = env["backend"].get(storage_keys.blob_key(env["device_id"], digest))
    published = crypto.decrypt(published_blob, "test-passphrase", memory_kb=1024)
    published_rows = [json.loads(line) for line in published.splitlines()]
    index = published_rows.index(catchup_rows[0])
    assert published_rows[index : index + len(catchup_rows)] == catchup_rows


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


def _add_claude_source(env, path, name):
    (path / "projects").mkdir(parents=True)
    env["cfg"]["sync"]["sources"].append({"name": name, "path": str(path), "type": "claude"})


def test_catchup_warm_rechecks_claude_selection_inside_cache_lock(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    revoked = tmp_path / "claude-revoked"
    retained = tmp_path / "claude-retained"
    _add_claude_source(env, revoked, "claude-revoked")
    _add_claude_source(env, retained, "claude-retained")
    config.save_config(env["cfg"])

    open_cache = token_usage.lock_and_get_files

    @contextmanager
    def revoke_while_locking(mode):
        env["cfg"]["sync"]["sources"].pop(-2)
        config.save_config(env["cfg"])
        with open_cache(mode) as files:
            yield files

    scanned = []
    monkeypatch.setattr(token_usage, "lock_and_get_files", revoke_while_locking)
    monkeypatch.setattr(token_usage, "_iter_all_jsonls", lambda root: scanned.append(root) or ())
    capturecatchup._warm_step1_caches(["session_scan"], pushtimings.PhaseTimer())
    assert revoked / "projects" not in scanned


@pytest.mark.parametrize("revoke_at", ["session_scan", "pending_repositories"])
def test_catchup_rechecks_consent_after_git_work(tmp_path, monkeypatch, revoke_at):
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-consent")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    _add_claude_source(env, tmp_path / "claude", "claude")
    config.save_config(env["cfg"])

    def revoke():
        env["cfg"]["sync"]["sources"].pop()
        config.save_config(env["cfg"])

    if revoke_at == "session_scan":
        capture_git = events_tail._capture_git_rows
        walk_sessions = events.walk_session_metadata
        scanned = []

        def capture_and_revoke(*args, **kwargs):
            result = capture_git(*args, **kwargs)
            revoke()
            return result

        def record_scan(path, **kwargs):
            scanned.append(path)
            return walk_sessions(path, **kwargs)

        monkeypatch.setattr(events_tail, "_capture_git_rows", capture_and_revoke)
        monkeypatch.setattr(events, "walk_session_metadata", record_scan)
    else:
        capture = events_tail._capture_event_snapshots
        preview = events.preview_pending_repositories

        def capture_with_pending_repos(*args, **kwargs):
            return replace(capture(*args, **kwargs), walk_budget_aborts=1)

        def preview_and_revoke(*args, **kwargs):
            result = preview(*args, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(events_tail, "_capture_event_snapshots", capture_with_pending_repos)
        monkeypatch.setattr(events, "preview_pending_repositories", preview_and_revoke)

    result = capturecatchup.run_catchup(["git_walk"])

    if revoke_at == "session_scan":
        assert scanned == []
    assert result["result"] == "deferred"
    assert list(env["events_dir"].glob("*.jsonl")) == []
    assert events.read_git_capture_cache()["repo_cursors"] == {}


def test_discovery_rechecks_before_probing_manual_roots(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path / "repo-manual")
    probed = []
    classify = events._classify_git_root

    def observe_probe(root):
        probed.append(root)
        return classify(root)

    def revoke():
        raise capturecatchup._ConfigChanged

    monkeypatch.setattr(events, "_classify_git_root", observe_probe)
    with pytest.raises(capturecatchup._ConfigChanged):
        events.discover_git_roots({"retro": {"repo_roots": [str(repo)]}}, before_read=revoke)
    assert probed == []


def test_cancelled_first_discovery_leaves_a_reusable_index(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-first-use")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    locked_json_rmw = lockedjson.locked_json_rmw
    changed = False

    @contextmanager
    def change_after_opening(path, **kwargs):
        nonlocal changed
        with locked_json_rmw(path, **kwargs) as state:
            if path == config.GIT_CAPTURE_CACHE_PATH and not changed:
                changed = True
                env["cfg"]["retro"]["author_emails"] = ["other@example.com"]
                config.save_config(env["cfg"])
            yield state

    monkeypatch.setattr(lockedjson, "locked_json_rmw", change_after_opening)
    assert not config.GIT_CAPTURE_CACHE_PATH.exists()
    result = capturecatchup.run_catchup(["git_discovery"])
    assert result["result"] == "deferred"
    assert not list(env["events_dir"].glob("*.jsonl"))
    assert capturecatchup.run_catchup(["git_discovery"])["result"] == "ok"


@pytest.mark.parametrize("failure", ["write", "lock"])
def test_catchup_reports_cursor_write_failure_after_append(tmp_path, monkeypatch, failure):
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-cursor")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])

    write_json = lockedjson._write_json

    def fail_cursor_write(fd, data, **kwargs):
        if data.get("repo_cursors"):
            return OSError("cursor storage unavailable")
        return write_json(fd, data, **kwargs)

    acquire = lockedjson._acquire_lock

    def fail_cursor_lock(fd, **kwargs):
        if kwargs["on_contention"] == "block" and os.path.samestat(
            os.fstat(fd), config.GIT_CAPTURE_CACHE_PATH.stat()
        ):
            return False
        return acquire(fd, **kwargs)

    if failure == "write":
        monkeypatch.setattr(lockedjson, "_write_json", fail_cursor_write)
    else:
        monkeypatch.setattr(lockedjson, "_acquire_lock", fail_cursor_lock)
    result = capturecatchup.run_catchup(["git_walk"])

    assert result["result"] == "failed"
    assert "cursor update" in result["detail"]
    assert events.read_git_capture_cache()["repo_cursors"] == {}
    row = events.latest_event_rows(env["events_dir"], env["device_id"], {"git-snapshot"}).rows
    assert row["git-snapshot"]["origin"] == events.GIT_SNAPSHOT_ORIGIN_CATCHUP


def test_adversarial_busy_sync_lock_defers_and_keeps_warmed_caches(tmp_path, monkeypatch):
    _setup_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(capturecatchup, "_SYNC_LOCK_BACKOFFS_S", (0.01,))
    warm_marker = tmp_path / "warmed"

    def fake_step1(phases, timer):
        warm_marker.write_text("yes")
        return capturecatchup._WarmedCaches(timed_hosts=frozenset({"codex"}))

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
    monkeypatch.setattr(
        capturecatchup, "_warm_step1_caches", lambda p, t: capturecatchup._WarmedCaches()
    )
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


def test_catchup_does_not_inventory_codex_logs_under_sync_lock(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    repo = _make_git_repo(tmp_path / "repo-inventory")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    codex_host.CACHE_PATH.write_text(json.dumps({"files": {}}))
    sessions = tmp_path / "codex-sessions"
    sessions.mkdir()
    monkeypatch.setattr(codex_host, "CODEX_SESSIONS_PATH", sessions)

    rglob = Path.rglob
    inventoried = []

    def observe_inventory(path, *args, **kwargs):
        if path == sessions:
            inventoried.append(lockfile.is_lock_held(config.LOCK_PATH))
        return rglob(path, *args, **kwargs)

    def read_codex(**kwargs):
        if lockfile.is_lock_held(config.LOCK_PATH):
            return host_usage.HostUsageResult(hosts=None, complete=False, reason="deadline")
        return host_usage.HostUsageResult(hosts={}, complete=True)

    monkeypatch.setattr(Path, "rglob", observe_inventory)
    monkeypatch.setattr(codex_host, "read_codex_usage", read_codex)
    evidence = events_tail.HostUsageCapture(
        None,
        "codex",
        "deadline",
        dropped=(("codex", "deadline"),),
        deadline_evidence={"codex": events_tail.HostReadEvidence(files_on_disk=10, files_cached=2)},
    )
    events_tail._record_host_progress(evidence, device_id=env["device_id"], attended=True)

    assert capturecatchup.run_catchup(["host:codex"])["result"] == "partial"
    assert inventoried == []
    assert events_tail._read_capture_progress(env["device_id"]).host == {"codex": 8}


def test_catchup_carries_fresh_deadline_counts_from_before_sync_lock(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    device_id = env["device_id"]

    def stop(**kwargs):
        return host_usage.HostUsageResult(hosts=None, complete=False, reason="deadline")

    def read(**kwargs):
        return host_usage.HostUsageResult(hosts={}, complete=True)

    capture, rows = events_tail._capture_host_snapshot(
        device_id,
        (("codex", stop), ("muse", read)),
        host_budget_ms=1000,
        warm_deadline_evidence={
            "codex": events_tail.HostReadEvidence(files_on_disk=10, files_cached=3)
        },
    )
    assert rows[0]["pending_left"] == {"codex": 7}
    events_tail._record_host_progress(capture, device_id=device_id, attended=False)
    assert events_tail._read_capture_progress(device_id).host == {"codex": 7}


@pytest.mark.parametrize("lookup_current", [True, False])
def test_catchup_keeps_git_attribution_local(tmp_path, monkeypatch, lookup_current):
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-identity")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    (repo / "README.md").write_text("collaborator update\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "collaborator commit"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Collaborator",
            "GIT_AUTHOR_EMAIL": "peer@example.com",
            "GIT_COMMITTER_NAME": "Collaborator",
            "GIT_COMMITTER_EMAIL": "peer@example.com",
        },
    )

    now = datetime.now(timezone.utc).isoformat()
    identity_cache = {
        "version": identity.CACHE_VERSION,
        "refreshed_at": now,
        "emails": ["user@example.com"],
    }
    if lookup_current:
        identity_cache["github_noreply_checked_at"] = now
    else:
        identity_cache["refreshed_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=identity.TTL_SECONDS + 1)
        ).isoformat()
    identity.CACHE_PATH.write_text(json.dumps(identity_cache))

    def fail_remote_lookup(*args, **kwargs):
        pytest.fail("Detached capture must not perform a GitHub identity lookup")

    monkeypatch.setattr(identity, "_gather_gh_noreply_email", fail_remote_lookup)
    result = capturecatchup.run_catchup(["git_walk"])
    git = events.latest_event_rows(env["events_dir"], env["device_id"], {"git-snapshot"})
    if lookup_current:
        assert result["result"] == "ok"
        commit_records = [
            commit
            for project in git.rows["git-snapshot"]["projects"]
            for commit in project["commits"]
        ]
        assert [commit["subject"] for commit in commit_records] == ["init"]
        assert events.read_git_capture_cache()["repo_cursors"]
    else:
        assert result["result"] == "deferred"
        assert "Run: mm push" in result["detail"]
        assert "git-snapshot" not in git.rows
        assert not events.read_git_capture_cache()["repo_cursors"]


@pytest.mark.parametrize("refresh", ["overlap", "lookup-failed", "old-version", "older-check"])
def test_catchup_preserves_cursor_until_identity_is_resolved(tmp_path, monkeypatch, refresh):
    env = _setup_fleet(tmp_path, monkeypatch)
    noreply = "5+user@users.noreply.github.com"
    repo = _make_git_repo(tmp_path / "repo-identity-refresh", email=noreply)
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])

    now = datetime.now(timezone.utc)
    previous_cursor = (now - timedelta(days=1)).isoformat()
    with lockedjson.locked_json_rmw(
        config.GIT_CAPTURE_CACHE_PATH, default_factory=events._default_git_capture_cache
    ) as state:
        state.data["repo_cursors"] = {str(repo): previous_cursor}

    cache = {
        "version": identity.CACHE_VERSION,
        "refreshed_at": (now - timedelta(seconds=identity.TTL_SECONDS + 1)).isoformat(),
        "emails": ["user@example.com"],
    }
    if refresh == "old-version":
        cache.update(version=identity.CACHE_VERSION - 1, refreshed_at=now.isoformat())
    if refresh == "older-check":
        cache.update(refreshed_at=now.isoformat(), github_noreply_checked_at=previous_cursor)
    identity.CACHE_PATH.write_text(json.dumps(cache))

    monkeypatch.setattr(identity, "_gather_global_email", lambda: "user@example.com")
    monkeypatch.setattr(identity, "_gather_per_repo_emails", lambda **kwargs: [])
    monkeypatch.setattr(identity, "_gather_config_author_emails", lambda: [])
    lookups = []
    lookup_result = None if refresh == "lookup-failed" else noreply

    def check_github():
        lookups.append(lookup_result)
        return lookup_result

    monkeypatch.setattr(identity, "_gather_gh_noreply_email", check_github)
    if refresh == "lookup-failed":
        assert identity.refresh_identity_cache(force=True) == ["user@example.com"]
    elif refresh == "overlap":
        configured_author_spellings = events._configured_author_spellings

        def refresh_while_building_filter(config, roots):
            if not lookups:
                assert noreply in identity.refresh_identity_cache(force=True)
            return configured_author_spellings(config, roots)

        monkeypatch.setattr(events, "_configured_author_spellings", refresh_while_building_filter)

    lookups_expected = {"lookup-failed": [None], "overlap": [noreply]}.get(refresh, [])
    first = capturecatchup.run_catchup(["git_walk"])
    git = events.latest_event_rows(env["events_dir"], env["device_id"], {"git-snapshot"})
    assert lookups == lookups_expected
    assert events.read_git_capture_cache()["repo_cursors"][str(repo)] == previous_cursor
    assert first["result"] == "deferred"
    assert "git-snapshot" not in git.rows
    if refresh == "older-check":
        assert identity.github_noreply_check_due(("user@example.com",))

    if refresh != "overlap":
        lookup_result = noreply
        assert noreply in identity.refresh_identity_cache(force=True)
        lookups_expected.append(noreply)
    second = capturecatchup.run_catchup(["git_walk"])
    git = events.latest_event_rows(env["events_dir"], env["device_id"], {"git-snapshot"})
    assert lookups == lookups_expected
    assert second["result"] == "ok"
    assert events.read_git_capture_cache()["repo_cursors"][str(repo)] > previous_cursor
    assert [
        commit["subject"]
        for project in git.rows["git-snapshot"]["projects"]
        for commit in project["commits"]
    ] == ["init"]


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
    """The detached entry point adopts the descriptor and records a result."""
    lock_path = tmp_path / ".config" / "mind-meld" / "capture-catchup.lock"
    fd = lockfile.acquire_lock(lock_path)
    env = {**os.environ, "HOME": str(tmp_path)}
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-P",
                "-m",
                "mind_meld.capturecatchup",
                f"--lock-fd={fd}",
                "--phase=git_walk",
            ],
            cwd="/",
            env=env,
            pass_fds=(fd,),
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        lockfile.release_lock(lock_path)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    state = json.loads(lock_path.with_suffix(".json").read_text())
    assert state["state"] == "finished"
    assert state["trigger_phases"] == ["git_walk"]


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
    repo = _make_git_repo(tmp_path / "repo-status")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    # (d) Failed catch-up surfaces action unless superseded by newer healthy push
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(now - timedelta(seconds=1)).isoformat(),
        finished_at=now.isoformat(),
        trigger_phases=["git_walk"],
        detail="Git capture index unreadable; run mm diag",
    )
    res_fail = runner.invoke(cli.app, ["status"])
    assert res_fail.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_fail.stdout

    # A no-op hook succeeds without starting capture.
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    res_noop = runner.invoke(cli.app, ["status"])
    assert "Background capture catch-up failed. Run: mm diag" in res_noop.stdout

    # A degraded capture may record a newer row. The no-op still cannot borrow it.
    capture = events_tail._capture_event_snapshots

    def stop_session_scan(*args, **kwargs):
        return replace(capture(*args, **kwargs), session_walk_exceeded_budget=True)

    (env["notes_dir"] / "hello.md").write_text("degraded capture\n")
    with monkeypatch.context() as patch:
        patch.setattr(events_tail, "_capture_event_snapshots", stop_session_scan)
        assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    after_degraded = runner.invoke(cli.app, ["status"])
    assert "Background capture catch-up failed. Run: mm diag" in after_degraded.stdout

    # A newer successful capture supersedes the old failure.
    (env["notes_dir"] / "hello.md").write_text("capture again\n")
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    res_superseded = runner.invoke(cli.app, ["status"])
    assert res_superseded.exit_code == 0
    assert "In sync with this device's last push." in res_superseded.stdout
    assert "Background capture catch-up failed. Run: mm diag" not in res_superseded.stdout

    # A later change-gated no-op must not bring back a superseded failure.
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    res_after_noop = runner.invoke(cli.app, ["status"])
    assert res_after_noop.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res_after_noop.stdout

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
        finished_at=datetime.now(timezone.utc).isoformat(),
        trigger_phases=["git_walk"],
        detail="capture still stopped at deadline; run mm push",
    )
    res_partial = runner.invoke(cli.app, ["status"])
    assert res_partial.exit_code == 0
    assert "capture deadline backlog covered" not in res_partial.stdout
    partial_text = " ".join(res_partial.stdout.split())
    assert partial_text.startswith("Latest push needs attention")
    assert "capture still stopped at deadline; run mm push" in partial_text


def test_delayed_hook_breadcrumb_keeps_newer_capture_recovery(tmp_path, monkeypatch):
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-delayed-breadcrumb")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *args, **kwargs: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    capturecatchup.record_catchup_state(
        state="finished", result="failed", finished_at=datetime.now(timezone.utc).isoformat()
    )

    real_rmw = cli.locked_json_rmw
    later_results = []

    @contextmanager
    def delay_once(path, *args, **kwargs):
        if path == core._autorun_breadcrumb_path() and not later_results:
            (env["notes_dir"] / "hello.md").write_text("newer capture\n")
            later_results.append(None)
            later_results[0] = runner.invoke(cli.app, ["autopush"])
        with real_rmw(path, *args, **kwargs) as state:
            yield state

    monkeypatch.setattr(cli, "locked_json_rmw", delay_once)
    cli._write_autorun_breadcrumb("push", "lock-held")

    assert later_results and later_results[0].exit_code == 0
    res = runner.invoke(cli.app, ["status"])
    assert res.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res.stdout


def test_status_reports_finished_partial_before_worker_releases_custody(tmp_path, monkeypatch):
    _setup_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    cli._write_autorun_breadcrumb(
        "push", "degraded", "git walk hit budget", capture_deadline_only=True
    )
    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        capturecatchup.record_catchup_state(
            state="finished",
            result="partial",
            finished_at=datetime.now(timezone.utc).isoformat(),
            detail="capture still stopped at deadline; run mm push",
        )
        res = runner.invoke(cli.app, ["status"])
        assert capturecatchup.is_catchup_running()
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)

    assert res.exit_code == 0
    output = " ".join(res.stdout.split())
    assert "Background capture catch-up: partial" in output
    assert "Latest push needs attention" in output


def test_status_keeps_reader_deadline_newer_than_catchup(tmp_path, monkeypatch):
    _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    now = datetime.now(timezone.utc)
    finished = now - timedelta(minutes=2)
    capturecatchup.record_catchup_state(
        state="finished", result="ok", finished_at=finished.isoformat()
    )
    monkeypatch.setattr(
        host_registry,
        "reader_usage_diag",
        lambda *a, **kw: {
            "last_reason": "deadline",
            "last_reason_since": (now - timedelta(minutes=1)).isoformat(),
        },
    )
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "Usage capture needs attention. Run: mm diag" in result.stdout


@pytest.mark.parametrize("fault", ["discovery", "walk", "streak"])
def test_retro_discovery_errors_stay_visible_while_catchup_runs(
    tmp_path, monkeypatch, capsys, fault
):
    env = _setup_fleet(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    row = events.make_mm_push_event(
        device=env["device_id"],
        mm_version="1.0.0",
        sources=["mm-events"],
        discovery_errors=["Git discovery prober failed"] if fault == "discovery" else [],
        git_capture=events.make_git_capture(
            since=now - timedelta(hours=1),
            discovery="partial" if fault == "discovery" else "complete",
            walk_budget_aborts=1,
            walk_errors=int(fault == "walk"),
        ),
    )
    sources = env["cfg"]["sync"]["sources"]
    events.write_push_event(env["events_dir"], env["device_id"], [row], strict=True)
    if fault == "streak":
        with lockedjson.locked_json_rmw(config.GIT_CAPTURE_CACHE_PATH) as cache:
            cache.data.update(events._default_git_capture_cache())
            cache.data["incomplete_streak"] = 3
    statusflow._print_retro_capture_status(
        sources,
        env["device_id"],
        catchup_covering=True,
        catchup_running=True,
    )
    output = " ".join(capsys.readouterr().out.split())
    assert (
        "Continue capture on this machine: mm push" if fault == "discovery" else "mm recapture"
    ) in output
    assert "background catch-up is handling" not in output


@pytest.mark.parametrize(
    "fault, expected",
    [
        ("attended", "Latest push needs attention"),
        ("unsupported", "unsupported"),
        ("held", "held, not yet synced"),
    ],
)
def test_status_keeps_independent_degradation_while_catchup_runs(
    tmp_path, monkeypatch, fault, expected
):
    _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0
    cli._write_autorun_breadcrumb(
        "push", "degraded", events_tail._ROOT_DISCOVERY_DEGRADATION, capture_deadline_only=True
    )
    if fault == "attended":
        attemptlog.write_run(
            "push",
            attemptlog.make_run(
                outcome="degraded", cause="result-degraded", counts={"events_degradations": 1}
            ),
        )
    elif fault == "unsupported":
        monkeypatch.setattr(
            host_registry, "reader_usage_diag", lambda *a, **kw: {"last_reason": "unsupported"}
        )
    else:
        detail = cli._held_breadcrumb_detail([("notes", "hello.md", "changed")], budget=1024)
        cli._write_autorun_breadcrumb("push", "degraded", detail)

    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        result = runner.invoke(cli.app, ["status"])
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)
    assert result.exit_code == 0
    assert "Background capture catch-up: running" in " ".join(result.stdout.split())
    assert expected in " ".join(result.stdout.split())


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


def test_attended_push_skipping_git_capture_does_not_supersede_failed_catchup(
    tmp_path, monkeypatch
):
    """Verify an attended push that skips Git capture does not resolve a failed catch-up."""
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-attended-skip")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(now - timedelta(seconds=1)).isoformat(),
        finished_at=now.isoformat(),
        trigger_phases=["git_walk"],
        detail="Git capture index unreadable; run mm diag",
    )
    res_fail = runner.invoke(cli.app, ["status"])
    assert res_fail.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_fail.stdout

    # Successful attended push with no content change skips Git capture.
    res_push = runner.invoke(cli.app, ["push"])
    assert res_push.exit_code == 0
    res_after_skip = runner.invoke(cli.app, ["status"])
    assert res_after_skip.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_after_skip.stdout

    # A subsequent attended push that actually completes Git capture supersedes the failure.
    (env["notes_dir"] / "hello.md").write_text("trigger attended git capture\n")
    res_capture_push = runner.invoke(cli.app, ["push"])
    assert res_capture_push.exit_code == 0
    res_recovered = runner.invoke(cli.app, ["status"])
    assert res_recovered.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res_recovered.stdout


def test_no_change_attended_push_preserves_catchup_recovery(tmp_path, monkeypatch):
    """Simulate failed catch-up, repairing attended push, then a no-change attended push."""
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-attended-recovery-stays")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(now - timedelta(seconds=1)).isoformat(),
        finished_at=now.isoformat(),
        trigger_phases=["git_walk"],
        detail="Git capture index unreadable; run mm diag",
    )
    res_fail = runner.invoke(cli.app, ["status"])
    assert res_fail.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_fail.stdout

    # Repairing attended push completes Git capture and clears the catch-up warning.
    (env["notes_dir"] / "hello.md").write_text("repair git capture\n")
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    res_recovered = runner.invoke(cli.app, ["status"])
    assert res_recovered.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res_recovered.stdout

    # A later no-change attended push must not bring the cleared warning back.
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    res_after_noop = runner.invoke(cli.app, ["status"])
    assert res_after_noop.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res_after_noop.stdout


def test_push_timing_out_on_sessions_does_not_resolve_catchup_failure(tmp_path, monkeypatch):
    """Verify a push that completes Git capture but hits the session deadline does not resolve."""
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-session-timeout")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(now - timedelta(seconds=1)).isoformat(),
        finished_at=now.isoformat(),
        trigger_phases=["session_scan"],
        detail="Claude token cache unavailable",
    )

    real_capture = events_tail._capture_event_snapshots

    def timeout_sessions(*args, **kwargs):
        return replace(real_capture(*args, **kwargs), session_walk_exceeded_budget=True)

    (env["notes_dir"] / "hello.md").write_text("git ok but session deadline\n")
    with monkeypatch.context() as patch:
        patch.setattr(events_tail, "_capture_event_snapshots", timeout_sessions)
        assert runner.invoke(cli.app, ["push"]).exit_code == 0

    # Even after a subsequent no-change attended push, the session catch-up failure stays visible.
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    res = runner.invoke(cli.app, ["status"])
    assert res.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res.stdout
    state = capturecatchup.read_catchup_state()
    assert state["resolved_at"] is None
    assert state["resolved_by"] is None


def test_successful_later_catchup_resolves_prior_failure(tmp_path, monkeypatch):
    """Verify a subsequent successful catch-up run resolves an earlier catch-up failure."""
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-catchup-resolves")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    monkeypatch.setattr(
        identity, "_gather_gh_noreply_email", lambda: "5+user@users.noreply.github.com"
    )
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(now - timedelta(seconds=1)).isoformat(),
        finished_at=now.isoformat(),
        trigger_phases=["git_walk"],
        detail="Git capture index unreadable; run mm diag",
    )
    assert (
        "Background capture catch-up failed. Run: mm diag"
        in runner.invoke(cli.app, ["status"]).stdout
    )

    res_catchup = capturecatchup.run_catchup(["git_walk"])
    assert res_catchup is not None
    assert res_catchup["result"] == "ok"
    state = capturecatchup.read_catchup_state()
    assert state["resolved_by"] == "catchup"
    assert state["resolved_at"] == res_catchup["finished_at"]
    res_status = runner.invoke(cli.app, ["status"])
    assert res_status.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res_status.stdout


def test_codex_failure_with_session_deadline_not_resolved_by_host_only_push(tmp_path, monkeypatch):
    """Verify a host-only push does not resolve a Codex failure combined with a session deadline."""
    env = _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    repo = _make_git_repo(tmp_path / "repo-codex-session-mixed")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    monkeypatch.setattr(
        identity, "_gather_gh_noreply_email", lambda: "5+user@users.noreply.github.com"
    )
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    real_capture = events_tail._capture_event_snapshots

    def codex_fail_and_session_deadline(*args, **kwargs):
        return replace(real_capture(*args, **kwargs), session_walk_exceeded_budget=True)

    with monkeypatch.context() as patch:
        patch.setattr(
            codex_host,
            "read_codex_usage",
            lambda *a, **k: host_usage.HostUsageResult(hosts={}, complete=False, reason="error"),
        )
        patch.setattr(
            host_registry,
            "warm_host_cache_inline",
            lambda *a, **k: host_usage.HostUsageResult(hosts={}, complete=False, reason="error"),
        )
        patch.setattr(events_tail, "_capture_event_snapshots", codex_fail_and_session_deadline)
        res_catchup = capturecatchup.run_catchup(["host:codex", "session_scan"])
    assert res_catchup is not None
    assert res_catchup["result"] == "failed"
    assert res_catchup["detail"] == "host usage reader failed"

    # A later attended push with only a complete Codex read (no content change) must NOT resolve.
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda *a, **k: host_usage.HostUsageResult(hosts={}, complete=True),
    )
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    state = capturecatchup.read_catchup_state()
    assert state["resolved_at"] is None
    assert state["resolved_by"] is None
    res_status = runner.invoke(cli.app, ["status"])
    assert res_status.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_status.stdout

    # Only a fully complete capture push resolves the failure.
    (env["notes_dir"] / "hello.md").write_text("complete capture after mixed failure\n")
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    resolved = capturecatchup.read_catchup_state()
    assert resolved["resolved_by"] == "attended"
    assert resolved["resolved_at"] is not None
    assert (
        "Background capture catch-up failed. Run: mm diag"
        not in runner.invoke(cli.app, ["status"]).stdout
    )


def test_host_usage_catchup_failure_resolves_only_on_full_capture(tmp_path, monkeypatch):
    """Verify a host-usage-only catch-up failure resolves only on a fully complete capture."""
    env = _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    repo = _make_git_repo(tmp_path / "repo-host-only-full-capture")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda *a, **k: host_usage.HostUsageResult(hosts={}, complete=False, reason="deadline"),
    )
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(now - timedelta(seconds=1)).isoformat(),
        finished_at=now.isoformat(),
        trigger_phases=["host:codex"],
        detail="host usage reader failed",
    )

    # Complete Codex read alone (no-change push skipping Git/session capture) must NOT resolve.
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda *a, **k: host_usage.HostUsageResult(hosts={}, complete=True),
    )
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    assert capturecatchup.read_catchup_state()["resolved_at"] is None
    res_still_failed = runner.invoke(cli.app, ["status"])
    assert res_still_failed.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_still_failed.stdout

    # A fully complete capture push resolves the failure.
    (env["notes_dir"] / "hello.md").write_text("full capture resolves host failure\n")
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    state = capturecatchup.read_catchup_state()
    assert state["resolved_by"] == "attended"
    assert state["resolved_at"] is not None
    res_ok = runner.invoke(cli.app, ["status"])
    assert res_ok.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res_ok.stdout
    assert "Background capture catch-up: failed" not in res_ok.stdout


def test_new_catchup_failure_after_resolution_shows_again(tmp_path, monkeypatch):
    """Verify a new catch-up failure after resolution makes the warning show again."""
    env = _setup_fleet(tmp_path, monkeypatch)
    repo = _make_git_repo(tmp_path / "repo-new-failure")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    t1 = datetime.now(timezone.utc)
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(t1 - timedelta(seconds=1)).isoformat(),
        finished_at=t1.isoformat(),
        trigger_phases=["git_walk"],
        detail="Git capture index unreadable; run mm diag",
    )
    (env["notes_dir"] / "hello.md").write_text("resolve first failure\n")
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    assert capturecatchup.read_catchup_state()["resolved_by"] == "attended"
    assert (
        "Background capture catch-up failed. Run: mm diag"
        not in runner.invoke(cli.app, ["status"]).stdout
    )

    # Record a newer failure after resolution -> warning must show again.
    t2 = datetime.now(timezone.utc) + timedelta(seconds=2)
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(t2 - timedelta(seconds=1)).isoformat(),
        finished_at=t2.isoformat(),
        trigger_phases=["git_walk"],
        detail="Git capture index unreadable; run mm diag",
    )
    state = capturecatchup.read_catchup_state()
    assert state["resolved_at"] is None
    assert state["resolved_by"] is None
    res = runner.invoke(cli.app, ["status"])
    assert res.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res.stdout


def test_status_and_diag_never_write_catchup_resolution(tmp_path, monkeypatch):
    """Verify read-only status, diag, and push --dry-run never modify catch-up state."""
    env = _setup_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        started_at=(now - timedelta(seconds=1)).isoformat(),
        finished_at=now.isoformat(),
        trigger_phases=["git_walk"],
        detail="Git capture index unreadable; run mm diag",
    )
    state_path = env["cfg_dir"] / "capture-catchup.json"
    before_bytes = state_path.read_bytes()

    assert runner.invoke(cli.app, ["status"]).exit_code == 0
    assert runner.invoke(cli.app, ["diag"]).exit_code == 0
    assert runner.invoke(cli.app, ["diag", "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["push", "--dry-run"]).exit_code == 0
    assert state_path.read_bytes() == before_bytes


def test_interrupted_catchup_worker_resolved_only_by_complete_push(tmp_path, monkeypatch):
    """Verify an interrupted catch-up worker resolves on a complete push, not a partial push."""
    env = _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    repo = _make_git_repo(tmp_path / "repo-interrupted-worker")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda *a, **k: host_usage.HostUsageResult(hosts={}, complete=True),
    )
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    t1 = datetime.now(timezone.utc)
    capturecatchup.record_catchup_state(
        state="running",
        result=None,
        started_at=t1.isoformat(),
        finished_at=None,
        trigger_phases=["git_walk", "session_scan"],
    )
    state_path = env["cfg_dir"] / "capture-catchup.json"
    before_bytes = state_path.read_bytes()

    # Read-only commands surface the interrupted worker without writing state.
    res_interrupted = runner.invoke(cli.app, ["status"])
    assert res_interrupted.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_interrupted.stdout
    assert "worker interrupted" in res_interrupted.stdout
    assert runner.invoke(cli.app, ["diag", "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["push", "--dry-run"]).exit_code == 0
    assert state_path.read_bytes() == before_bytes

    # A partial push (session deadline hit) must NOT resolve the interrupted worker.
    real_capture = events_tail._capture_event_snapshots

    def timeout_sessions(*args, **kwargs):
        return replace(real_capture(*args, **kwargs), session_walk_exceeded_budget=True)

    (env["notes_dir"] / "hello.md").write_text("partial push after interrupted worker\n")
    with monkeypatch.context() as patch:
        patch.setattr(events_tail, "_capture_event_snapshots", timeout_sessions)
        assert runner.invoke(cli.app, ["push"]).exit_code == 0
    assert state_path.read_bytes() == before_bytes
    res_after_partial = runner.invoke(cli.app, ["status"])
    assert res_after_partial.exit_code == 0
    assert "Background capture catch-up: failed" in res_after_partial.stdout
    assert "worker interrupted" in res_after_partial.stdout

    # A no-change push (skipping Git/session capture) must NOT resolve it either.
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    assert state_path.read_bytes() == before_bytes

    # A fully complete push (Git + Claude session + Codex all clean) resolves it.
    (env["notes_dir"] / "hello.md").write_text("complete push resolves interrupted worker\n")
    assert runner.invoke(cli.app, ["push"]).exit_code == 0
    resolved = capturecatchup.read_catchup_state()
    assert resolved["state"] == "finished"
    assert resolved["result"] == "failed"
    assert resolved["detail"] == "worker interrupted"
    assert resolved["resolved_by"] == "attended"
    assert resolved["resolved_at"] is not None
    res_resolved = runner.invoke(cli.app, ["status"])
    assert res_resolved.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" not in res_resolved.stdout
    assert "worker interrupted" not in res_resolved.stdout

    # A newer interrupted start supersedes the earlier resolution and shows again.
    t2 = datetime.now(timezone.utc) + timedelta(seconds=2)
    capturecatchup.record_catchup_state(
        state="running",
        result=None,
        started_at=t2.isoformat(),
        finished_at=None,
        trigger_phases=["git_walk"],
    )
    reopened = capturecatchup.read_catchup_state()
    assert reopened["resolved_at"] is None
    assert reopened["resolved_by"] is None
    res_again = runner.invoke(cli.app, ["status"])
    assert res_again.exit_code == 0
    assert "Background capture catch-up failed. Run: mm diag" in res_again.stdout
    assert "worker interrupted" in res_again.stdout


def test_unreadable_lock_never_finalizes_a_live_catchup_worker(tmp_path, monkeypatch):
    """Verify an unreadable lock file never finalizes a live catch-up worker on push."""
    env = _setup_fleet(tmp_path, monkeypatch, include_codex=True)
    repo = _make_git_repo(tmp_path / "repo-unreadable-lock")
    env["cfg"]["retro"]["repo_roots"] = [str(repo)]
    config.save_config(env["cfg"])
    monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda *a, **k: host_usage.HostUsageResult(hosts={}, complete=True),
    )
    assert runner.invoke(cli.app, ["autopush"]).exit_code == 0

    lock_path = capturecatchup.CATCHUP_LOCK_PATH
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys; "
                f"fp = open({str(lock_path)!r}, 'a+'); "
                "fcntl.flock(fp.fileno(), fcntl.LOCK_EX); "
                "print('locked', flush=True); "
                "sys.stdin.readline()"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        assert lockfile.probe_lock(lock_path) == "held"

        capturecatchup.record_catchup_state(
            state="running",
            result=None,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            trigger_phases=["git_walk"],
        )
        state_path = env["cfg_dir"] / "capture-catchup.json"
        before_bytes = state_path.read_bytes()

        os.chmod(lock_path, 0o000)
        assert lockfile.probe_lock(lock_path) == "unknown"

        # A fully complete push must leave the live worker's state untouched.
        (env["notes_dir"] / "hello.md").write_text("complete push while lock is unreadable\n")
        assert runner.invoke(cli.app, ["push"]).exit_code == 0
        assert state_path.read_bytes() == before_bytes

        res_status = runner.invoke(cli.app, ["status"])
        assert res_status.exit_code == 0
        assert "Background capture catch-up: running" in res_status.stdout
        assert "worker interrupted" not in res_status.stdout
        assert "Background capture catch-up failed. Run: mm diag" not in res_status.stdout
    finally:
        if lock_path.exists():
            os.chmod(lock_path, 0o644)
        if holder.stdin is not None:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.stdin.close()
        holder.wait(timeout=5)
