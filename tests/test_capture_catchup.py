"""Track 165A proving tests: phase timings, concurrent per-provider deadlines,
detached single-flight background catch-up, status suppression, and doc pins."""

from __future__ import annotations

import gc
import json
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mind_meld import (
    attemptlog,
    capturecatchup,
    config,
    core,
    crypto,
    events,
    events_tail,
    host_registry,
    host_usage,
    lockedjson,
    lockfile,
    manifest,
    memoryindex,
    token_usage,
)
from mind_meld import cli as cli_module
from mind_meld.cli import app
from mind_meld.errors import ConfigError, CryptoError
from mind_meld.hosts import codex as codex_host
from mind_meld.hosts import grok as grok_host
from mind_meld.storage import keys as storage_keys
from mind_meld.storage.local import LocalBackend
from tests.test_agy_usage import _create_agy_db
from tests.test_events import _init_git_repo

runner = CliRunner()


real_launcher = capturecatchup.start_detached_catchup


def _make_usage(n: int = 1) -> dict[str, int]:
    return {"input": n, "cache_create": 0, "cache_read": 0, "output": n}


def _complete(hosts: dict | None = None) -> host_usage.HostUsageResult:
    return host_usage.HostUsageResult(hosts if hosts is not None else {}, complete=True)


def _incomplete(reason: str) -> host_usage.HostUsageResult:
    return host_usage.HostUsageResult({}, complete=False, reason=reason)


def _init_push_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Minimal isolated environment for push / autopush / status / diag tests."""
    storage = tmp_path / "storage"
    storage.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.toml"
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "-Users-user-app" / "memory").mkdir(parents=True)
    (claude_dir / "projects" / "-Users-user-app" / "memory" / "note.md").write_text("hello")
    events_root = tmp_path / "mm-events"
    events_root.mkdir()
    codex_dir = tmp_path / "codex_src"
    codex_dir.mkdir()
    (codex_dir / "AGENTS.md").write_text("rules")

    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "LOCK_PATH", cfg_dir / "mind-meld.lock")
    monkeypatch.setattr(lockfile, "LOCK_PATH", cfg_dir / "mind-meld.lock")
    monkeypatch.setattr(
        cli_module,
        "acquire_lock",
        lambda path=None: lockfile.acquire_lock(path or (cfg_dir / "mind-meld.lock")),
    )
    monkeypatch.setattr(
        cli_module,
        "release_lock",
        lambda path=None: lockfile.release_lock(path or (cfg_dir / "mind-meld.lock")),
    )
    monkeypatch.setattr(
        core,
        "release_lock",
        lambda path=None: lockfile.release_lock(path or (cfg_dir / "mind-meld.lock")),
    )
    monkeypatch.setattr(core, "_autorun_breadcrumb_path", lambda: cfg_dir / "last-autorun.json")
    monkeypatch.setattr(crypto, "get_passphrase", lambda **kw: "test-pass")
    monkeypatch.setattr(cli_module, "get_passphrase", lambda **kw: "test-pass")
    monkeypatch.setattr(core, "get_passphrase", lambda **kw: "test-pass")
    monkeypatch.setattr(core, "_get_passphrase_or_exit", lambda: "test-pass")
    monkeypatch.setattr(codex_host, "CACHE_PATH", cfg_dir / "host-tokens.json")
    monkeypatch.setattr(grok_host, "CACHE_PATH", cfg_dir / "grok-host-tokens.json")
    monkeypatch.setattr(host_usage, "MUSE_CACHE_PATH", cfg_dir / "muse-host-tokens.json")
    monkeypatch.setattr(host_usage, "AGY_CACHE_PATH", cfg_dir / "agy-host-tokens.json")

    backend = LocalBackend(str(storage))
    crypto.bootstrap_crypto_init(backend, "test-pass", 1024)
    cfg = {
        "device": {"id": "dev-165a", "name": "test-mac"},
        "storage": {"path": str(storage)},
        "sync": {
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
                {"name": "codex", "path": str(codex_dir), "type": "generic"},
                {
                    "name": "mm-events",
                    "path": str(events_root),
                    "type": "generic",
                    "include_dirs": ["events"],
                },
            ]
        },
    }
    config.save_config(cfg)
    return {
        "storage": storage,
        "cfg_dir": cfg_dir,
        "claude_dir": claude_dir,
        "events_root": events_root,
        "codex_dir": codex_dir,
        "backend": backend,
        "config": cfg,
    }


# ── T1: Per-item progress callbacks & phase timing persistence ────────────


def test_t1_per_item_progress_callbacks_for_all_readers_and_walkers(tmp_path, monkeypatch):
    # 1. Codex
    codex_root = tmp_path / "codex_sessions" / "2026" / "11" / "24"
    codex_root.mkdir(parents=True)
    for i in range(2):
        (codex_root / f"rollout-2026-11-24T00-00-0{i}-id{i}.jsonl").write_text("")
    monkeypatch.setattr(codex_host, "CACHE_PATH", tmp_path / "codex-cache.json")
    codex_calls: list[tuple[int, int]] = []
    res_codex = codex_host.read_codex_usage(
        tmp_path / "codex_sessions",
        deadline=time.monotonic() + 5.0,
        progress=lambda done, total: codex_calls.append((done, total)),
    )
    assert res_codex.complete
    assert codex_calls == [(0, 2), (1, 2), (2, 2)]

    # 2. Grok Build
    grok_root = tmp_path / "grok_sessions"
    for i in range(2):
        sdir = grok_root / "cwd-a" / f"sess-{i}"
        sdir.mkdir(parents=True)
    monkeypatch.setattr(grok_host, "CACHE_PATH", tmp_path / "grok-cache.json")
    grok_calls: list[tuple[int, int]] = []
    res_grok = grok_host.read_grok_usage(
        grok_root,
        deadline=time.monotonic() + 5.0,
        consented=True,
        progress=lambda done, total: grok_calls.append((done, total)),
    )
    assert res_grok.complete
    assert grok_calls == [(0, 2), (1, 2), (2, 2)]

    # 3. Muse
    muse_root = tmp_path / "muse_sessions"
    for day in ("2026/11/23", "2026/11/24"):
        (muse_root / day).mkdir(parents=True)
    monkeypatch.setattr(host_usage, "MUSE_CACHE_PATH", tmp_path / "muse-cache.json")
    muse_calls: list[tuple[int, int]] = []
    res_muse = host_usage.read_muse_usage(
        muse_root,
        deadline=time.monotonic() + 5.0,
        consented=True,
        progress=lambda done, total: muse_calls.append((done, total)),
    )
    assert res_muse.complete
    assert muse_calls == [(0, 2), (1, 2), (2, 2)]

    # 4. Antigravity
    agy_root = tmp_path / "agy_convs"
    agy_root.mkdir()
    for i in range(2):
        _create_agy_db(agy_root / f"conv-{i}.db", {}, [])
    monkeypatch.setattr(host_usage, "AGY_CACHE_PATH", tmp_path / "agy-cache.json")
    agy_calls: list[tuple[int, int]] = []
    agy = host_usage.read_agy_usage(
        agy_root,
        deadline=time.monotonic() + 5.0,
        consented=True,
        progress=lambda done, total: agy_calls.append((done, total)),
    )
    assert agy.complete
    assert agy_calls == [(0, 2), (1, 2), (2, 2)]

    # 5. discover_git_roots
    disc_calls: list[tuple[int, int]] = []
    events.discover_git_roots(
        {"sync": {"sources": [{"name": "claude", "path": str(tmp_path), "type": "claude"}]}},
        deadline_monotonic=time.monotonic() + 5.0,
        progress=lambda done, total: disc_calls.append((done, total)),
    )
    assert disc_calls and disc_calls[0][0] == 0 and disc_calls[-1][0] == disc_calls[-1][1]

    # 6. walk_session_metadata
    claude_dir = tmp_path / "claude_walk"
    (claude_dir / "projects" / "-Users-user-p1").mkdir(parents=True)
    (claude_dir / "projects" / "-Users-user-p2").mkdir(parents=True)
    sess_calls: list[tuple[int, int]] = []
    events.walk_session_metadata(
        claude_dir,
        deadline_monotonic=time.monotonic() + 5.0,
        progress=lambda done, total: sess_calls.append((done, total)),
    )
    assert sess_calls == [(0, 2), (1, 2), (2, 2)]


def test_t1_verbose_timings_and_diag_persistence(tmp_path, monkeypatch):
    env = _init_push_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda root=None, *, deadline=None, progress=None: (
            (progress(0, 3), progress(3, 3)) if progress else None,
            _complete({"codex": {"2026-11-24": _make_usage(10)}}),
        )[1],
    )
    monkeypatch.setattr(
        events,
        "discover_git_roots",
        lambda cfg, **kw: (
            kw["progress"](1, 1) if kw.get("progress") else None,
            events.GitRootDiscovery(roots=[], errors=[]),
        )[1],
    )
    monkeypatch.setattr(events, "walk_git_projects", lambda *a, **kw: [])

    # 1. Attended push --verbose prints Phase timings before closing Push: line
    res = runner.invoke(app, ["push", "--verbose"])
    assert res.exit_code == 0, res.output
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert "Phase timings:" in res.stdout
    assert "Codex usage:" in res.stdout
    assert "Manifest scan:" in res.stdout
    assert lines[-1].startswith("Push:"), f"last line must stay O8 outcome: {lines[-1]!r}"

    # 2. push-timings.json records attended run, exposed in mm diag (text + JSON)
    timings = capturecatchup.read_push_timings()
    phases = timings["by_kind"]["attended"]["phases"]
    host_phase = next(phase for phase in phases if phase["phase"] == "host:codex")
    assert host_phase["budget_ms"] == 3000
    assert (host_phase["completed"], host_phase["total"]) == (3, 3)
    assert f"Codex usage: {host_phase['elapsed_ms']} ms / 3000 ms" in res.stdout
    assert timings["latest"]["kind"] == "attended"

    # 3. Autopush records autopush timings
    (env["claude_dir"] / "projects" / "-Users-user-app" / "memory" / "note.md").write_text("v2")
    res_auto = runner.invoke(app, ["autopush"])
    assert res_auto.exit_code == 0, res_auto.output
    timings_after = capturecatchup.read_push_timings()
    assert "autopush" in timings_after["by_kind"]

    diag_json = runner.invoke(app, ["diag", "--json"])
    assert diag_json.exit_code == 0, diag_json.output
    payload = json.loads(diag_json.stdout)
    assert "push_timings" in payload
    assert "attended" in payload["push_timings"]["by_kind"]
    assert "autopush" in payload["push_timings"]["by_kind"]

    diag_plain = runner.invoke(app, ["diag"])
    assert diag_plain.exit_code == 0, diag_plain.output
    assert "Push timings" in diag_plain.output
    assert "Background capture catch-up" in diag_plain.output


# ── T2: Equal per-provider deadlines & concurrent execution ───────────────


def test_t2_four_readers_run_concurrently_and_merge_in_canonical_order():
    rendezvous = threading.Barrier(4)
    clock = threading.local()

    def now() -> float:
        return getattr(clock, "value", 10.0)

    def slow_reader(name: str):
        def _read(*, deadline: float):
            assert deadline - now() == pytest.approx(1.0)
            rendezvous.wait(timeout=4)
            clock.value = now() + 0.12
            return _complete({name: {"2026-11-24": _make_usage(1)}})

        return _read

    names = ("codex", "grok", "muse", "agy")
    readers = tuple((name, slow_reader(name)) for name in names)
    cap = events_tail._capture_host_usage(readers, deadline=now() + 1, now=now, concurrent=True)

    assert cap.complete is True
    assert cap.token_sources == names
    assert {sample.elapsed_ms for sample in cap.reader_timings.values()} == {120}
    assert {sample.budget_ms for sample in cap.reader_timings.values()} == {1000}


def test_t2_early_finishing_reader_returns_immediately_without_holding_sweep():
    def fast_reader(*, deadline: float):
        time.sleep(0.005)
        return _complete({"codex": {"2026-11-24": _make_usage(2)}})

    t0 = time.monotonic()
    cap = events_tail._capture_host_usage(
        (("codex", fast_reader),), deadline=t0 + 1.0, concurrent=True
    )
    elapsed = time.monotonic() - t0
    assert cap.complete is True
    assert elapsed < 0.2


def test_t2_pause_gc_is_thread_safe():
    assert gc.isenabled()
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=2.0)
            for _ in range(20):
                with host_usage._pause_gc():
                    time.sleep(0.001)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors
    assert gc.isenabled() is True


def test_t2_base_exception_in_worker_propagates():
    class _GuardError(BaseException):
        pass

    def bad_reader(*, deadline: float):
        raise _GuardError("write guard tripped")

    def ok_reader(*, deadline: float):
        return _complete()

    with pytest.raises(_GuardError, match="write guard tripped"):
        events_tail._capture_host_usage(
            (("codex", ok_reader), ("muse", bad_reader)),
            deadline=time.monotonic() + 1.0,
            concurrent=True,
        )


# ── T3: Detached single-flight background catch-up ────────────────────────


def test_t3_autopush_triggers_catchup_only_on_deadline_stop_and_dry_run_never(
    tmp_path, monkeypatch
):
    env = _init_push_env(tmp_path, monkeypatch)
    triggered: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        capturecatchup,
        "start_detached_catchup",
        lambda phases: triggered.append(tuple(phases)) or True,
    )
    monkeypatch.setattr(
        events,
        "discover_git_roots",
        lambda cfg, **kw: events.GitRootDiscovery(roots=[], errors=[]),
    )
    monkeypatch.setattr(events, "walk_git_projects", lambda *a, **kw: [])

    # 1. Healthy autopush does NOT trigger catch-up
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda root=None, *, deadline=None, progress=None: _complete(),
    )
    r_ok = runner.invoke(app, ["autopush"])
    assert r_ok.exit_code == 0, r_ok.output
    assert r_ok.stdout == "" and r_ok.stderr == ""
    assert triggered == []

    # 2. Deadline-stopped host reader triggers catch-up
    (env["claude_dir"] / "projects" / "-Users-user-app" / "memory" / "note.md").write_text("v2")
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda root=None, *, deadline=None, progress=None: _incomplete("deadline"),
    )
    r_dl = runner.invoke(app, ["autopush"])
    assert r_dl.exit_code == 0, r_dl.output
    assert r_dl.stdout == "" and r_dl.stderr == ""
    assert triggered == [("host:codex",)]

    # 3. push --dry-run never triggers catch-up
    triggered.clear()
    r_dry = runner.invoke(app, ["push", "--dry-run"])
    assert r_dry.exit_code == 0, r_dry.output
    assert triggered == []


def test_t3_single_flight_lock_and_end_to_end_catchup(tmp_path, monkeypatch):
    env = _init_push_env(tmp_path, monkeypatch)
    cfg = config.load_config()
    cfg.setdefault("retro", {}).update(
        capture_budget_ms=5000,
        attended_capture_cap_ms=12000,
        host_usage_interactive_budget_ms=4200,
        host_usage_autopush_budget_ms=1000,
    )
    config.save_config(cfg)
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda root=None, *, deadline=None, progress=None: _complete(
            {"codex": {"2026-11-24": _make_usage(5)}}
        ),
    )
    monkeypatch.setattr(
        events,
        "discover_git_roots",
        lambda cfg, **kw: events.GitRootDiscovery(roots=[], errors=[]),
    )
    monkeypatch.setattr(events, "walk_git_projects", lambda *a, **kw: [])

    launched = []
    proc = capturecatchup.subprocess
    monkeypatch.setattr(
        capturecatchup,
        "subprocess",
        SimpleNamespace(DEVNULL=proc.DEVNULL, Popen=lambda *a, **k: launched.append((a, k))),
    )
    # 1. Holding CATCHUP_LOCK_PATH makes start_detached_catchup and autopush --catchup no-op
    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        assert capturecatchup.is_catchup_running() is True
        assert real_launcher(("host:codex",)) is False
        assert launched == []
        r_busy = runner.invoke(app, ["autopush", "--catchup"])
        assert r_busy.exit_code == 0
        assert r_busy.stdout == ""
        assert capturecatchup.read_catchup_state()["state"] == "idle"
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)

    # 2. End-to-end autopush --catchup publishes encrypted host row + records timings & ok state
    assert real_launcher(("host:codex", "git_walk")) is True
    (argv,), options = launched[0]
    assert argv[:4] == [sys.executable, "-P", "-m", "mind_meld.cli"]
    assert options == {
        "cwd": "/",
        "stdin": capturecatchup.subprocess.DEVNULL,
        "stdout": capturecatchup.subprocess.DEVNULL,
        "stderr": capturecatchup.subprocess.DEVNULL,
        "start_new_session": True,
    }
    r_run = runner.invoke(app, argv[4:])
    assert r_run.exit_code == 0, r_run.output
    assert r_run.stdout == "" and r_run.stderr == ""
    st = capturecatchup.read_catchup_state()
    assert st["state"] == "finished"
    assert st["result"] == "ok", st
    assert st["published"] is True
    assert st["trigger_phases"] == ["host:codex", "git_walk"]
    diag = runner.invoke(app, ["diag", "--json"])
    assert diag.exit_code == 0, diag.output
    assert json.loads(diag.stdout)["capture_catchup"]["trigger_phases"] == st["trigger_phases"]

    device = env["config"]["device"]["id"]
    backend = env["backend"]
    cipher = backend.get(storage_keys.manifest_key(device))
    accepted = manifest.load_manifest(crypto.decrypt_manifest(cipher, "test-pass", memory_kb=1024))
    (event_file,) = accepted["sources"]["mm-events"]["files"].values()
    payload = backend.get(storage_keys.blob_key(device, event_file["sha256"]))
    rows = [
        json.loads(line)
        for line in crypto.decrypt(payload, "test-pass", memory_kb=1024).splitlines()
    ]
    assert {"host-usage-snapshot", "mm-push"} <= {row["type"] for row in rows}
    assert rows[-1]["type"] == "mm-push"
    phases = capturecatchup.read_push_timings()["by_kind"]["catchup"]["phases"]
    budgets = {phase["phase"]: phase["budget_ms"] for phase in phases}
    assert 3800 <= budgets["host:codex"] <= 4200
    assert budgets["git_discovery"] > events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS
    assert budgets["git_walk"] > events.WALK_TIME_BUDGET_AUTOPUSH_MS

    # 3. Missing passphrase records no-passphrase without prompting
    def unavailable(*, non_interactive):
        assert non_interactive
        raise CryptoError("fixture keyring empty")

    monkeypatch.setattr(cli_module, "get_passphrase", unavailable)
    r_np = runner.invoke(app, ["autopush", "--catchup"])
    assert r_np.exit_code == 0
    assert r_np.stdout == ""
    assert capturecatchup.read_catchup_state()["result"] == "no-passphrase"

    # 4. Contended mind-meld.lock records deferred
    monkeypatch.setattr(cli_module, "_CATCHUP_LOCK_BACKOFFS_S", (0.01,))
    monkeypatch.setattr(cli_module, "_acquire_sync_lock_bounded", lambda *a, **k: False)
    r_def = runner.invoke(app, ["autopush", "--catchup"])
    assert r_def.exit_code == 0
    assert capturecatchup.read_catchup_state()["result"] == "deferred"

    # 5. Crash inside catch-up prints nothing, records failed, and releases lock
    monkeypatch.setattr(
        events_tail,
        "_prepare_events_tail",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r_err = runner.invoke(app, ["autopush", "--catchup"])
    assert r_err.exit_code == 0
    assert r_err.stdout == "" and r_err.stderr == ""
    assert capturecatchup.read_catchup_state()["result"] == "failed"
    assert capturecatchup.is_catchup_running() is False


def test_t3_newer_complete_push_not_overwritten_by_stale_prepared_tail(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=10)
    newer_ts = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    newer_push = {
        "v": events.EVENTS_SCHEMA_VERSION,
        "type": "mm-push",
        "ts": newer_ts,
        "device": "dev-165a",
        "mm_version": "1.12.84",
        "sources": ["mm-events"],
        "git_capture": {
            "since": t0.isoformat(),
            "discovery": "complete",
            "walk_budget_aborts": 0,
            "walk_errors": 0,
            "advances_cursor": True,
        },
    }
    events.write_push_event(events_dir, "dev-165a", [newer_push])
    prepared = events_tail.PreparedEventsTail(
        started_at=t0,
        events_dir=events_dir,
        since=t0,
        capture=events_tail.CaptureResult(
            git_rows=[],
            session_rows=[],
            host_rows=[],
            host_capture=events_tail.HostUsageCapture({}),
            root_discovery=events.GitRootDiscovery(roots=[], errors=[]),
            discovery_errors=[],
            session_walk_exceeded_budget=False,
            warn_lock_unavailable=False,
            token_cache_requested=False,
        ),
        mm_event={**newer_push, "ts": t0.isoformat()},
    )
    committed, _degs = events_tail._commit_prepared_tail(prepared, {}, "dev-165a")
    assert committed is None


@pytest.mark.parametrize("phase", ["git_discovery", "git_walk", "session_scan"])
def test_autopush_defers_each_deadline_after_releasing_sync_lock(tmp_path, monkeypatch, phase):
    _init_push_env(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_host, "read_codex_usage", lambda **kw: _complete())
    monkeypatch.setattr(
        events, "discover_git_roots", lambda *a, **kw: events.GitRootDiscovery([], [])
    )
    real_capture = events_tail._capture_event_snapshots

    def run_capture(*args, **kwargs):
        captured = real_capture(*args, **kwargs)
        if phase == "git_discovery":
            return replace(captured, root_discovery=events.GitRootDiscovery([], [], exceeded=True))
        if phase == "git_walk":
            return replace(captured, walk_budget_aborts=1)
        return replace(captured, session_walk_exceeded_budget=True)

    launched = []

    def launcher(phases):
        assert not lockfile.is_lock_held(config.LOCK_PATH)
        launched.append(tuple(phases))
        return True

    monkeypatch.setattr(events_tail, "_capture_event_snapshots", run_capture)
    monkeypatch.setattr(capturecatchup, "start_detached_catchup", launcher)
    result = runner.invoke(app, ["autopush"])
    assert result.exit_code == 0, result.output
    assert result.stdout == result.stderr == ""
    assert launched == [(phase,)]


@pytest.fixture
def capture_store(tmp_path, monkeypatch):
    setup = _init_push_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda **kw: _complete(
            {"codex": {datetime.now(timezone.utc).date().isoformat(): _make_usage(5)}}
        ),
    )
    monkeypatch.setattr(
        events, "discover_git_roots", lambda *a, **kw: events.GitRootDiscovery([], [])
    )
    monkeypatch.setattr(events, "walk_git_projects", lambda *a, **kw: [])
    return setup


def test_skipped_catchup_append_is_not_publication(capture_store, monkeypatch):
    monkeypatch.setattr(
        events,
        "write_push_event",
        lambda directory, *a, **k: events.EventAppendSkipped(directory / "fixture.jsonl"),
    )
    result = runner.invoke(app, ["autopush", "--catchup"])
    assert result.exit_code == 0
    assert result.stdout == result.stderr == ""
    assert capturecatchup.read_catchup_state()["published"] is False


def test_catchup_respects_consent_changed_during_preparation(capture_store, monkeypatch):
    prepare = events_tail._prepare_events_tail

    def prepare_then_disable(*args):
        prepared = prepare(*args)
        updated = config.load_config()
        updated["sync"]["disabled_sources"] = ["codex"]
        config.save_config(updated)
        return prepared

    monkeypatch.setattr(events_tail, "_prepare_events_tail", prepare_then_disable)
    result = runner.invoke(app, ["autopush", "--catchup"])
    assert result.exit_code == 0
    events_dir = capture_store["events_root"] / "events"
    rows = events.latest_event_rows(events_dir, "dev-165a", {"host-usage-snapshot"}).rows
    assert "host-usage-snapshot" not in rows


def test_unchanged_autopush_records_manifest_scan(capture_store):
    assert runner.invoke(app, ["push"]).exit_code == 0
    result = runner.invoke(app, ["autopush"])
    assert result.exit_code == 0
    timings = capturecatchup.read_push_timings()["by_kind"].get("autopush", {})
    assert any(phase["phase"] == "manifest_scan" for phase in timings.get("phases", []))


def test_concurrent_partial_push_does_not_rewind_cursor(capture_store):
    started = datetime.now(timezone.utc) - timedelta(seconds=3)
    later = started + timedelta(seconds=2)
    device = "dev-165a"
    directory = capture_store["events_root"] / "events"
    partial = events.make_mm_push_event(
        device=device,
        mm_version="1.12.84",
        ts=later,
        git_capture=events.make_git_capture(
            since=started, discovery="complete", walk_budget_aborts=1, walk_errors=0
        ),
    )
    events.write_push_event(directory, device, [partial])
    prepared = events_tail.PreparedEventsTail(
        started_at=started,
        events_dir=directory,
        since=started,
        capture=events_tail.CaptureResult(
            git_rows=[],
            session_rows=[],
            host_rows=[],
            host_capture=events_tail.HostUsageCapture({}),
            root_discovery=events.GitRootDiscovery(roots=[], errors=[]),
            discovery_errors=[],
            session_walk_exceeded_budget=False,
            warn_lock_unavailable=False,
            token_cache_requested=False,
        ),
        mm_event={**partial, "ts": started.isoformat()},
    )
    before = events.resolve_push_cursor(directory, device)
    events_tail._commit_prepared_tail(prepared, capture_store["config"], device)
    assert events.resolve_push_cursor(directory, device).since >= before.since


def test_interrupted_catchup_is_reported_read_only(capture_store):
    capturecatchup.record_catchup_state(
        state="running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    original = capturecatchup.CATCHUP_STATE_PATH.read_bytes()
    diag = runner.invoke(app, ["diag", "--json"])
    assert diag.exit_code == 0, diag.output
    state = json.loads(diag.stdout)["capture_catchup"]
    assert state["running"] is False
    assert state["result"] == "failed"
    assert state["detail"] == "worker interrupted"
    assert capturecatchup.CATCHUP_STATE_PATH.read_bytes() == original


def test_running_catchup_does_not_hide_git_walk_errors(capture_store, monkeypatch):
    real_capture = events_tail._capture_event_snapshots

    def mixed_capture(*args, **kwargs):
        return replace(real_capture(*args, **kwargs), walk_budget_aborts=1, walk_errors=1)

    monkeypatch.setattr(events_tail, "_capture_event_snapshots", mixed_capture)
    assert runner.invoke(app, ["autopush"]).exit_code == 0
    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        status = runner.invoke(app, ["status"])
        assert status.exit_code == 0, status.output
        assert "Latest push needs attention" in status.output
        assert "git walk dropped 2 repositories" in " ".join(status.output.split())
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)


def test_catchup_cannot_report_complete_if_claude_cache_is_unavailable(capture_store, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("fixture cache unreadable")

    monkeypatch.setattr(token_usage, "is_cache_cold", unavailable)
    result = runner.invoke(app, ["autopush", "--catchup"])
    assert result.exit_code == 0, result.output
    state = capturecatchup.read_catchup_state()
    assert state["published"] is True, state
    assert state["result"] == "failed", state


def test_catchup_does_not_clear_an_older_independent_attended_failure(capture_store):
    now = datetime.now(timezone.utc)
    attemptlog.write_run(
        "push",
        attemptlog.make_run(
            outcome="degraded",
            cause="result-degraded",
            degradations=["event append skipped (max-file-size)"],
            completed_at=(now - timedelta(seconds=2)).isoformat(),
        ),
    )
    core._autorun_breadcrumb_path().write_text(
        '{"push":{"outcome":"degraded","capture_deadline_only":true,"timestamp":"'
        + (now - timedelta(seconds=1)).isoformat()
        + '"}}'
    )
    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        result = runner.invoke(app, ["status"])
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)
    assert result.exit_code == 0, result.output
    assert "Latest push needs attention" in result.output


def test_catchup_and_source_revocation_cannot_commit_at_the_same_time(capture_store, monkeypatch):
    read_newer = events_tail._newer_capture_recorded
    changes = []

    def interleave(*args):
        changes.append(runner.invoke(app, ["disable-source", "codex"]))
        return read_newer(*args)

    monkeypatch.setattr(events_tail, "_newer_capture_recorded", interleave)
    result = runner.invoke(app, ["autopush", "--catchup"])
    assert result.exit_code == 0, result.output
    assert changes and changes[0].exit_code == 1, changes[0].output
    assert "codex" not in config.load_config()["sync"].get("disabled_sources", [])
    assert runner.invoke(app, ["disable-source", "codex"]).exit_code == 0


def test_phase_one_discovery_does_not_persist_shared_cursor_state(capture_store, monkeypatch):
    persist_options = []

    def probe(*args, persist, **kw):
        persist_options.append(persist)
        return events.GitRootDiscovery([], [])

    monkeypatch.setattr(events, "discover_git_roots", probe)
    cfg = config.load_config()
    sources = list(config.resolve_sources(cfg, strict=True, bootstrap=False).available)
    assert events_tail._prepare_events_tail(cfg, sources, cfg["device"]["id"]) is not None
    assert persist_options == [False]


def test_deferred_worker_preserves_warmed_provider_cache(tmp_path, monkeypatch):
    env = _init_push_env(tmp_path, monkeypatch)
    sessions = tmp_path / "codex-sessions" / "2026" / "11" / "24"
    sessions.mkdir(parents=True)
    (sessions / "rollout-2026-11-24T00-00-00-fixture.jsonl").write_text("")
    original_read = codex_host.read_codex_usage
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda **kw: original_read(tmp_path / "codex-sessions", **kw),
    )
    original_lock = cli_module._acquire_sync_lock_bounded
    monkeypatch.setattr(
        cli_module, "_acquire_sync_lock_bounded", lambda: original_lock(backoffs=())
    )
    lockfile.acquire_lock(config.LOCK_PATH)
    try:
        result = runner.invoke(app, ["autopush", "--catchup"])
        assert result.exit_code == 0, result.output
        assert lockfile.is_lock_held(config.LOCK_PATH)
        assert capturecatchup.read_catchup_state()["result"] == "deferred"
        warmed = json.loads(codex_host.CACHE_PATH.read_bytes())
        assert warmed["files"]
        assert not env["backend"].exists(storage_keys.manifest_key("dev-165a"))
    finally:
        lockfile.release_lock(config.LOCK_PATH)


# ── T4: Status suppression & read-only guarantees ─────────────────────────


def test_t4_covers_deadline_backlog_predicate_and_readonly_diag_status(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    recent_ok = {
        "result": "ok",
        "published": True,
        "finished_at": (now - timedelta(minutes=5)).isoformat(),
    }
    assert capturecatchup.covers_deadline_backlog(recent_ok, running=False, now=now) is True
    assert (
        capturecatchup.covers_deadline_backlog(
            recent_ok, running=False, backlog_at=now - timedelta(minutes=10), now=now
        )
        is True
    )
    assert (
        capturecatchup.covers_deadline_backlog(
            recent_ok, running=False, backlog_at=now - timedelta(minutes=1), now=now
        )
        is False
    )
    assert (
        capturecatchup.covers_deadline_backlog({"result": "failed"}, running=True, now=now) is True
    )
    assert (
        capturecatchup.covers_deadline_backlog(
            {"result": "failed", "published": False}, running=False, now=now
        )
        is False
    )
    assert (
        capturecatchup.covers_deadline_backlog(
            {"result": "deferred", "published": False, "finished_at": now.isoformat()},
            running=False,
            now=now,
        )
        is False
    )

    # Read-only guarantee: mm status and mm diag create no catchup/timings files
    _init_push_env(tmp_path, monkeypatch)
    assert not capturecatchup.CATCHUP_LOCK_PATH.exists()
    assert not capturecatchup.CATCHUP_STATE_PATH.exists()
    assert not capturecatchup.PUSH_TIMINGS_PATH.exists()
    r_diag = runner.invoke(app, ["diag", "--json"])
    assert r_diag.exit_code == 0, r_diag.output
    r_status = runner.invoke(app, ["status"])
    assert r_status.exit_code == 0, r_status.output
    assert not capturecatchup.CATCHUP_LOCK_PATH.exists()
    assert not capturecatchup.CATCHUP_STATE_PATH.exists()
    assert not capturecatchup.PUSH_TIMINGS_PATH.exists()


def test_t4_status_suppresses_deadline_nags_and_surfaces_catchup_failures(tmp_path, monkeypatch):
    from mind_meld import token_usage

    env = _init_push_env(tmp_path, monkeypatch)
    monkeypatch.setattr(token_usage, "is_cache_cold", lambda: False)
    # Seed an initial push so device isn't "has not pushed yet"
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda root=None, *, deadline=None, progress=None: _complete(),
    )
    monkeypatch.setattr(
        events,
        "discover_git_roots",
        lambda cfg, **kw: events.GitRootDiscovery(roots=[], errors=[]),
    )
    monkeypatch.setattr(events, "walk_git_projects", lambda *a, **kw: [])
    assert runner.invoke(app, ["push"]).exit_code == 0

    # Run a real deadline-stopped autopush so the latest push breadcrumb is auto/degraded
    (env["claude_dir"] / "projects" / "-Users-user-app" / "memory" / "note.md").write_text("v2")
    monkeypatch.setattr(
        codex_host,
        "read_codex_usage",
        lambda root=None, *, deadline=None, progress=None: _incomplete("deadline"),
    )
    assert runner.invoke(app, ["autopush"]).exit_code == 0

    now = datetime.now(timezone.utc)
    crumb_ts = now
    finish_ts = now
    reader_cache = env["cfg_dir"] / "host-tokens.json"
    reader_state = {
        "version": 1,
        "last_reason": "deadline",
        "last_reason_since": crumb_ts.isoformat(),
        "last_deadline_allotted_ms": 1000,
        "files": {},
    }
    reader_cache.write_text(json.dumps(reader_state))

    # 1. Running catch-up suppresses manual push nag and shows running line
    capturecatchup.record_catchup_state(
        state="running",
        started_at=crumb_ts.isoformat(),
    )
    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        r_run = runner.invoke(app, ["status"])
        assert r_run.exit_code == 0, r_run.output
        assert "Background capture catch-up: running" in r_run.output
        assert "Latest push needs attention" not in r_run.output
        assert "Usage capture needs attention" not in r_run.output
        assert "An attended mm push can warm cold readers" not in " ".join(r_run.output.split())
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)

    # 2. Recently published ok catch-up suppresses manual push nag and shows ok line
    capturecatchup.record_catchup_state(
        state="finished",
        result="ok",
        published=True,
        started_at=crumb_ts.isoformat(),
        finished_at=finish_ts.isoformat(),
    )
    r_ok = runner.invoke(app, ["status"])
    assert r_ok.exit_code == 0, r_ok.output
    assert "Background capture catch-up: ok" in r_ok.output
    assert "published" in r_ok.output
    assert "Latest push needs attention" not in r_ok.output

    reader_state["last_reason_since"] = (finish_ts + timedelta(seconds=1)).isoformat()
    reader_cache.write_text(json.dumps(reader_state))
    newer = runner.invoke(app, ["status"])
    assert newer.exit_code == 0, newer.output
    assert "Usage capture needs attention" in newer.output

    # 3. Failed catch-up surfaces owner-action degradation and does NOT suppress
    capturecatchup.record_catchup_state(
        state="finished",
        result="failed",
        published=False,
        started_at=crumb_ts.isoformat(),
        finished_at=finish_ts.isoformat(),
        detail="RuntimeError",
    )
    r_fail = runner.invoke(app, ["status"])
    assert r_fail.exit_code == 0, r_fail.output
    assert "Background capture catch-up: failed" in r_fail.output
    assert (
        "Latest push needs attention" in r_fail.output
        or "Background capture catch-up failed" in r_fail.output
    )

    capturecatchup.record_catchup_state(
        state="finished",
        result="no-passphrase",
        started_at=crumb_ts.isoformat(),
        finished_at=finish_ts.isoformat(),
        detail="passphrase unavailable in keyring",
    )
    no_passphrase = runner.invoke(app, ["status"])
    assert no_passphrase.exit_code == 0, no_passphrase.output
    assert "passphrase unavailable in keyring. Run: mm push" in " ".join(
        no_passphrase.output.split()
    )
    assert "Latest push needs attention" in no_passphrase.output

    # 4. Non-deadline degradation (unsupported) still surfaces even while catch-up is running
    (env["cfg_dir"] / "host-tokens.json").write_text(
        json.dumps({"version": 1, "last_reason": "unsupported", "files": {}})
    )
    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        r_unsup = runner.invoke(app, ["status"])
        assert r_unsup.exit_code == 0, r_unsup.output
        assert "needs a newer mm" in r_unsup.output or "Usage not captured" in r_unsup.output
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)


@pytest.mark.parametrize("phase", ["git_discovery", "git_walk"])
def test_running_catchup_handles_only_the_git_deadline(phase, capture_store, monkeypatch):
    monkeypatch.setattr(events_tail, "_decide_token_walk_policy", lambda *a, **k: True)
    assert runner.invoke(app, ["push"]).exit_code == 0
    real_capture = events_tail._capture_event_snapshots

    def with_deadline(*args, **kwargs):
        capture = real_capture(*args, **kwargs)
        if phase == "git_walk":
            return replace(capture, walk_budget_aborts=1)
        errors = [events.GIT_ROOT_DISCOVERY_BUDGET_ERROR]
        return replace(
            capture,
            root_discovery=events.GitRootDiscovery([], errors, exceeded=True),
            discovery_errors=errors,
        )

    monkeypatch.setattr(events_tail, "_capture_event_snapshots", with_deadline)
    note = next(capture_store["claude_dir"].rglob("note.md"))
    note.write_text("updated fixture")
    assert runner.invoke(app, ["autopush"]).exit_code == 0
    crumb = json.loads(core._autorun_breadcrumb_path().read_bytes())["push"]
    assert crumb.get("capture_deadline_only") is True, crumb
    lockfile.acquire_lock(capturecatchup.CATCHUP_LOCK_PATH)
    try:
        status = runner.invoke(app, ["status"])
    finally:
        lockfile.release_lock(capturecatchup.CATCHUP_LOCK_PATH)
    assert status.exit_code == 0, status.output
    assert "Latest push needs attention" not in status.output
    assert "Continue capture on this machine" not in status.output
    assert "Background capture catch-up: running" in status.output


@pytest.mark.parametrize("index_changed", [False, True])
def test_detached_discovery_keeps_and_commits_the_pinned_refresh_window(
    index_changed, tmp_path, monkeypatch
):
    capture_store = _init_push_env(tmp_path, monkeypatch)
    projects = capture_store["claude_dir"] / "projects"
    monkeypatch.setattr(events, "_claude_projects_dir", lambda: projects)
    original_root = tmp_path / "first-repository"
    refreshed_root = tmp_path / "refreshed-repository"
    _init_git_repo(original_root)
    _init_git_repo(refreshed_root)
    session = next(projects.iterdir()) / "session.jsonl"
    session.write_text(json.dumps({"cwd": str(original_root)}))

    cfg = config.load_config()
    device = cfg["device"]["id"]
    sources = list(config.resolve_sources(cfg, strict=True, bootstrap=False).available)
    now = datetime.now(timezone.utc)
    pinned = now - timedelta(days=20)
    assert original_root in events.discover_git_roots(cfg, persist=True, since=pinned).roots

    cache = events.read_git_capture_cache()
    cache["pending_refresh_since"] = pinned.isoformat()
    config.GIT_CAPTURE_CACHE_PATH.write_text(json.dumps(cache))
    session.write_text(json.dumps({"cwd": str(refreshed_root)}))
    directory = capture_store["events_root"] / "events"
    prior = events.make_mm_push_event(
        device=device, mm_version="1.12.85", ts=now - timedelta(days=1)
    )
    events.write_push_event(directory, device, [prior])

    observed = {}

    def capture_repo(root, since_iso, timeout_ms, author_emails=None):
        observed[root] = events.parse_aware_ts(since_iso)
        return {
            "project": root.name,
            "local_path": str(root),
            "remote_url": None,
            "commits": [],
            "branches": ["main"],
        }, None

    monkeypatch.setattr(events, "_walk_one_repo", capture_repo)
    prepared = events_tail._prepare_events_tail(cfg, sources, device)
    assert prepared is not None
    unchanged = events.read_git_capture_cache()
    assert unchanged["pending_refresh_since"] == pinned.isoformat()
    assert unchanged["projects"][str(session.parent)]["git_root"] == str(original_root)
    if index_changed:
        cache["discovery_resume_after"] = str(projects)
        config.GIT_CAPTURE_CACHE_PATH.write_text(json.dumps(cache))
    lockfile.acquire_lock(config.LOCK_PATH)
    try:
        path, problems = events_tail._commit_prepared_tail(prepared, cfg, device)
    finally:
        lockfile.release_lock(config.LOCK_PATH)
    committed = events.read_git_capture_cache()
    if index_changed:
        assert path is None and not problems
        assert committed["pending_refresh_since"] == pinned.isoformat()
        assert committed["discovery_resume_after"] == str(projects)
        assert events.latest_mm_push_row(directory, device) == prior
    else:
        assert path is not None, problems
        assert committed["pending_refresh_since"] is None
        assert committed["projects"][str(session.parent)]["git_root"] == str(refreshed_root)
        assert "session_meta" in committed["projects"][str(session.parent)]
    assert observed[refreshed_root] <= pinned


@pytest.mark.parametrize("days_ago", [0, 1])
def test_detached_staleness_only_considers_day_files_that_could_be_newer(
    days_ago, capture_store, monkeypatch
):
    device = "dev-165a"
    directory = capture_store["events_root"] / "events"
    directory.mkdir()
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=days_ago)).date().isoformat()
    (directory / f"{device}-{day}.jsonl").write_bytes(b"x" * 8192 + b"\n")
    monkeypatch.setattr(token_usage, "MAX_JSONL_LINE_BYTES", 4096)
    prepared = SimpleNamespace(started_at=now, events_dir=directory)

    assert events_tail._newer_capture_recorded(prepared, device) is (days_ago == 0)


@pytest.mark.parametrize("recovery_write_fails", [False, True])
def test_detached_replays_failed_cursor_before_accepting_prepared_scan(
    recovery_write_fails, tmp_path, monkeypatch
):
    env = _init_push_env(tmp_path, monkeypatch)
    cfg = config.load_config()
    device = cfg["device"]["id"]
    sources = list(config.resolve_sources(cfg, strict=True, bootstrap=False).available)
    repo = tmp_path / "journal-repository"
    _init_git_repo(repo)
    monkeypatch.setattr(
        events, "discover_git_roots", lambda *a, **k: events.GitRootDiscovery((repo,), ())
    )
    directory = env["events_root"] / "events"
    row_time = datetime.now(timezone.utc) - timedelta(days=1)
    since = row_time - timedelta(days=2)
    prior = events.make_mm_push_event(
        device=device,
        mm_version="1.12.85",
        ts=row_time,
        git_capture=events.make_git_capture(
            since=since, discovery="complete", walk_budget_aborts=1, walk_errors=0
        ),
    )
    skipped = {
        "v": events.EVENTS_SCHEMA_VERSION,
        "type": "git-snapshot",
        "ts": row_time.isoformat(),
        "device": device,
        "projects": [],
        "skipped": [{"path": str(repo), "reason": events.WALK_SKIP_BUDGET_ABORT}],
    }
    events.record_uncommitted_push_roots([skipped], push_ts=prior["ts"])
    events.write_push_event(directory, device, [skipped, prior])
    assert str(repo) not in events.read_git_capture_cache()["repo_cursors"]

    observed = []

    def walk(root, since_iso, timeout_ms, author_emails=None):
        observed.append(events.parse_aware_ts(since_iso))
        return {"local_path": str(root), "remote": "example.com/user/repo", "commits": []}, None

    monkeypatch.setattr(events, "_walk_one_repo", walk)
    prepared = events_tail._prepare_events_tail(cfg, sources, device)
    assert prepared is not None
    assert observed == [row_time]
    write_attempts = 0
    write_json = lockedjson._write_json

    def fail_first_write(*args, **kwargs):
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            return OSError("fixture recovery write failed")
        return write_json(*args, **kwargs)

    lockfile.acquire_lock(config.LOCK_PATH)
    try:
        with monkeypatch.context() as active:
            if recovery_write_fails:
                active.setattr(lockedjson, "_write_json", fail_first_write)
            path, problems = events_tail._commit_prepared_tail(prepared, cfg, device)
    finally:
        lockfile.release_lock(config.LOCK_PATH)
    assert path is None
    assert events.latest_mm_push_row(directory, device) == prior
    if recovery_write_fails:
        assert problems == ["could not recover Git capture cursors"]
        cache = events.read_git_capture_cache()
        assert str(repo) not in cache["repo_cursors"]
        assert prior["ts"] in cache["uncommitted_push_roots"]
        return
    assert not problems
    assert events.read_git_capture_cache()["repo_cursors"][str(repo)] == since.isoformat()
    again = events_tail._prepare_events_tail(cfg, sources, device)
    assert again is not None
    assert observed[-1] == since


@pytest.mark.parametrize("during", ["session", "warm"])
def test_detached_reader_does_not_begin_after_revocation(during, capture_store, monkeypatch):
    cfg = config.load_config()
    cfg.setdefault("retro", {})["grok_host_usage"] = True
    config.save_config(cfg)
    reads = []
    real_walk = events.walk_session_metadata

    def revoke():
        stopped = runner.invoke(app, ["disable-source", "grok", "--force"])
        assert stopped.exit_code == 0, stopped.output
        assert not config.grok_usage_consented(config.load_config())

    def walk_and_revoke(*args, **kwargs):
        if during == "session":
            revoke()
        return real_walk(*args, **kwargs)

    def read_grok(**kwargs):
        reads.append("initial")
        if during == "warm":
            revoke()
        return _incomplete("deadline")

    def warm_reader(**kwargs):
        reads.append("warm")
        return _incomplete("deadline")

    monkeypatch.setattr(events, "walk_session_metadata", walk_and_revoke)
    monkeypatch.setattr(grok_host, "read_grok_usage", read_grok)
    monkeypatch.setattr(host_registry, "warm_host_cache_inline", warm_reader)

    outcome, published, detail, _ = cli_module._capture_catchup_work(capturecatchup.PhaseTimer())

    assert outcome == "deferred" and not published, detail
    assert reads == (["initial"] if during == "warm" else [])
    directory = capture_store["events_root"] / "events"
    assert events.latest_mm_push_row(directory, cfg["device"]["id"]) is None


@pytest.mark.parametrize("command", ["push", "autopush"])
def test_push_refreshes_source_consent_after_unlocking(command, capture_store, monkeypatch):
    cfg = config.load_config()
    cfg.setdefault("retro", {})["grok_host_usage"] = True
    config.save_config(cfg)
    reads = []

    def revoke_during_unlock(**kwargs):
        stopped = runner.invoke(app, ["disable-source", "grok", "--force"])
        assert stopped.exit_code == 0, stopped.output
        assert not config.grok_usage_consented(config.load_config())
        return "test-pass"

    def read_grok(**kwargs):
        reads.append("grok")
        return _complete({"grok": {"2026-11-24": _make_usage(1)}})

    if command == "push":
        monkeypatch.setattr(core, "_get_passphrase_or_exit", revoke_during_unlock)
    else:
        monkeypatch.setattr(cli_module, "get_passphrase", revoke_during_unlock)
    monkeypatch.setattr(grok_host, "read_grok_usage", read_grok)

    pushed = runner.invoke(app, [command])

    assert pushed.exit_code == 0, pushed.output
    assert reads == []


def test_autopush_crypto_backfill_holds_sync_lock(capture_store, monkeypatch):
    checked = []
    patch_config = core.patch_config_on_disk

    def observe(updates):
        checked.append(lockfile.is_lock_held(config.LOCK_PATH))
        patch_config(updates)

    monkeypatch.setattr(core, "patch_config_on_disk", observe)

    pushed = runner.invoke(app, ["autopush"])

    assert pushed.exit_code == 0, pushed.output
    assert checked == [True]


def test_catchup_publication_does_not_cover_a_later_hook(capture_store, monkeypatch):
    def newer_deadline(_cfg):
        assert not lockfile.is_lock_held(config.LOCK_PATH)
        assert capturecatchup.is_catchup_running()
        cli_module._write_autorun_breadcrumb(
            "push", "degraded", "Git walk deadline", capture_deadline_only=True
        )

    monkeypatch.setattr(memoryindex, "start_detached_refresh", newer_deadline)
    ran = runner.invoke(app, ["autopush", "--catchup"])

    assert ran.exit_code == 0, ran.output
    state = capturecatchup.read_catchup_state()
    assert state["result"] == "ok" and state["published"]
    crumb = json.loads(core._autorun_breadcrumb_path().read_bytes())["push"]
    started = events.parse_aware_ts(state["started_at"])
    backlog = events.parse_aware_ts(crumb["timestamp"])
    assert backlog > started
    assert not capturecatchup.covers_deadline_backlog(state, running=False, backlog_at=backlog)


@pytest.mark.parametrize("command", ["autopush", "autopull"])
def test_auto_hook_handles_config_error_before_yield(command, capture_store, monkeypatch):
    original = cli_module.load_config
    calls = 0

    def read_config():
        nonlocal calls
        calls += 1
        if calls == 1:
            return original()
        raise ConfigError("fixture config became invalid")

    monkeypatch.setattr(cli_module, "load_config", read_config)
    ran = runner.invoke(app, [command])

    assert ran.exit_code == 0, repr(ran.exception)
    assert ran.stdout == ""
    assert ran.stderr.strip().endswith("fixture config became invalid")
    assert len(ran.stderr.splitlines()) == 1
    assert not lockfile.is_lock_held(config.LOCK_PATH)
    verb = command.removeprefix("auto")
    crumb = json.loads(core._autorun_breadcrumb_path().read_bytes())[verb]
    assert crumb["outcome"] == "config-error"


def test_detached_discovery_rechecks_before_each_prober(tmp_path, monkeypatch):
    _init_push_env(tmp_path, monkeypatch)
    probes = []

    def revoke_during_claude_probe(**kwargs):
        disabled = runner.invoke(app, ["disable-source", "codex", "--force"])
        assert disabled.exit_code == 0, disabled.output
        return []

    def read_host_records(prober, **kwargs):
        probes.append(prober.name)
        return []

    codex = next(prober for prober in events.rootprobes.PROBERS if prober.name == "codex")
    monkeypatch.setattr(events, "_enabled_root_probers", lambda *_: [codex])
    monkeypatch.setattr(events, "_probe_claude", revoke_during_claude_probe)
    monkeypatch.setattr(events, "_probe_host_records", read_host_records)

    outcome, published, detail, _ = cli_module._capture_catchup_work(capturecatchup.PhaseTimer())

    assert outcome == "deferred" and not published, detail
    assert probes == []


def test_detached_token_warm_rechecks_after_lock_wait(capture_store, monkeypatch):
    reads = []
    real_lock = token_usage.lock_and_get_files
    revoked = False

    @contextmanager
    def lock_with_revocation(mode):
        nonlocal revoked
        if mode == "block" and not revoked:
            revoked = True
            disabled = runner.invoke(app, ["disable-source", "claude", "--force"])
            assert disabled.exit_code == 0, disabled.output
        with real_lock(mode) as files:
            yield files

    def read_jsonls(root):
        reads.append(root)
        return iter(())

    monkeypatch.setattr(token_usage, "is_cache_cold", lambda: True)
    monkeypatch.setattr(token_usage, "lock_and_get_files", lock_with_revocation)
    monkeypatch.setattr(token_usage, "_iter_all_jsonls", read_jsonls)

    outcome, published, detail, _ = cli_module._capture_catchup_work(capturecatchup.PhaseTimer())

    assert revoked and outcome == "deferred" and not published, detail
    assert reads == []


# ── T5: No stale 750 ms shared sweep / 250 ms grace claims in docs ────────


def test_t5_no_stale_shared_sweep_or_grace_claims_in_docs():
    repo_root = Path(__file__).resolve().parents[1]
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    invariants = (repo_root / "docs" / "invariants" / "events-retro.md").read_text(encoding="utf-8")
    assert "host_usage_autopush_budget_ms = 750" not in readme
    assert "250 ms later-reader grace" not in readme
    assert "HOST_READER_GRACE_MS" not in invariants
    assert "HOST_READER_RESERVE_MS" not in invariants


def test_retired_source_migration_preserves_a_disable_during_confirmation(tmp_path, monkeypatch):
    _init_push_env(tmp_path, monkeypatch)
    cfg = config.load_config()
    retired = tmp_path / "retired"
    retired.mkdir()
    cfg["sync"]["sources"].append({"name": "opencode", "path": str(retired), "type": "generic"})
    config.save_config(cfg)

    def confirm(*args, **kwargs):
        disabled = runner.invoke(app, ["disable-source", "codex", "--force"])
        assert disabled.exit_code == 0, disabled.output
        return True

    monkeypatch.setattr(core, "ask_yes_no", confirm)
    migrated = runner.invoke(app, ["migrate-config"])

    assert migrated.exit_code == 0, migrated.output
    sync = config.load_config()["sync"]
    assert "opencode" not in {source["name"] for source in sync["sources"]}
    assert {"opencode", "codex"} <= set(sync["disabled_sources"])
