"""Silent-failure contract for autopull/autopush + related cli.py paths.

Pins the visible-failure contract for Claude Code's hook-driven sync:
silent on missing config, loud on corrupt config, breadcrumb on lock-held,
unknown peer sources surface as warnings (not verbose-only), --conflict-mode
fail preflights and exits 3 before any writes, etc. Originally landed as
"Track 1A" (v0.7.0); kept as a behavioral pin against regressions.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import cli as cli_module
from mind_meld import events as _mm_events
from mind_meld import host_usage as _mm_host_usage
from mind_meld import token_usage as _mm_token_usage
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend
from tests.conftest import (  # noqa: E402
    MEMORY_KB,
    PASSPHRASE,
    _make_config,
    _populate_claude,
    _redirect_lock,
    _redirect_sidecar,
    _setup_real_config,
)

runner = CliRunner()


def test_git_walk_degradation_names_current_failure_check():
    from mind_meld import events_tail

    phrase = events_tail._git_walk_degradation(1, 2)
    assert phrase == (
        "git walk dropped 3 repositories this push. "
        "Run mm recapture --dry-run to check current repository failures, "
        "then mm recapture 30d to recover the omitted commits"
    )
    assert "; " not in phrase


def test_status_retro_capture_nag_names_current_failure_check(tmp_path, monkeypatch):
    _setup_events_tail_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module.console, "_width", 240)
    now = datetime.now(timezone.utc).isoformat()
    _mm_events.write_push_event(
        tmp_path / "mm-events" / "events",
        "dev-deg",
        [
            {
                "type": "mm-push",
                "ts": now,
                "git_capture": {
                    "since": now,
                    "discovery": "complete",
                    "walk_errors": 1,
                    "walk_budget_aborts": 0,
                },
            }
        ],
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Check current repository failures: mm recapture --dry-run" in result.stdout
    assert result.stdout.index("Check current repository failures:") < result.stdout.index(
        "Details: mm diag"
    )


def _verb_crumb(payload: dict, verb: str) -> dict:
    """Read a per-verb last-autorun entry (Track 29A keyed shape)."""
    entry = payload.get(verb)
    if isinstance(entry, dict):
        return entry
    return payload


def _enable_test_host_source(tmp_path, reader, *, disabled=False):
    from mind_meld.config import CONFIG_PATH, load_config

    source = tmp_path / f"{reader}-customizations"
    (source / "skills").mkdir(parents=True)
    (source / "skills" / "test.md").write_text("substantive customization\n")
    cfg = load_config(CONFIG_PATH)
    cfg["sync"]["sources"].append(
        {
            "name": reader,
            "type": "grok" if reader == "grok" else "generic",
            "path": str(source),
            **({"include_dirs": ["skills"]} if reader == "codex" else {}),
        }
    )
    events_root = tmp_path / "events-root"
    events_root.mkdir(exist_ok=True)
    if not any(s["name"] == "mm-events" for s in cfg["sync"]["sources"]):
        cfg["sync"]["sources"].append(
            {
                "name": "mm-events",
                "type": "generic",
                "path": str(events_root),
                "include_dirs": ["events"],
            }
        )
    if disabled:
        cfg["sync"]["disabled_sources"] = [reader]
    save_config(cfg, CONFIG_PATH)


@pytest.mark.parametrize("reader", ["codex", "grok"])
@pytest.mark.parametrize("reason", sorted(_mm_host_usage.PERSISTABLE_REASONS))
def test_status_prioritizes_every_standing_reader_blocker(tmp_path, monkeypatch, reader, reason):
    _setup_real_config(tmp_path, monkeypatch)
    _enable_test_host_source(tmp_path, reader)
    cache = _mm_host_usage.CACHE_PATH if reader == "codex" else _mm_host_usage.GROK_CACHE_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "version": 1,
                "complete_once": True,
                "last_reason": reason,
                "last_reason_since": "2026-09-04T00:00:00+00:00",
                "files": {"pre-track": {"day": "2026-09-04"}},
            }
        )
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert f"{reader.title()} usage capture:" in text
    assert f"({reader} {reason})" in text
    assert "(first observed 2026-09-04 UTC)" in text
    assert "awaiting re-walk" not in text
    assert "prior scan completed successfully" not in text
    assert "no successful scan yet" not in text
    if reason == "unsupported":
        assert "mm update" in text
        # The device-wide publication block may name the explicit refresh.
        # The standing unsupported blocker must still prescribe upgrade,
        # never promise that a retry alone fixes the record.
        blocker = text.split(f"{reader.title()} usage capture:", 1)[1].split("Source '", 1)[0]
        assert "mm push" not in blocker


def test_status_ready_codex_with_deadline_names_interactive_warm(tmp_path, monkeypatch):
    _setup_real_config(tmp_path, monkeypatch)
    _enable_test_host_source(tmp_path, "codex")
    cache = _mm_host_usage.CACHE_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "version": 1,
                "last_reason": "deadline",
                "last_deadline_allotted_ms": 250,
                "last_complete_ms": 300,
                "last_complete_at": "2999-01-01T00:00:00+00:00",
                "files": {"warm": {"states": []}},
            }
        )
    )
    assert _mm_host_usage.codex_usage_diag()["state"] == "ready"
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "(codex deadline)" in text
    assert "Last read allowed 250 ms; last complete read 300 ms (in the future)" in text
    assert "not yet scanned" not in text


@pytest.mark.parametrize("state", ["disabled", "unreadable", "healthy"])
def test_status_codex_silence_for_disabled_unreadable_or_healthy_cache(
    tmp_path, monkeypatch, state
):
    _setup_real_config(tmp_path, monkeypatch)
    _enable_test_host_source(tmp_path, "codex", disabled=state == "disabled")
    cache = _mm_host_usage.CACHE_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "last_reason": "unsupported", "files": {"old": {}}}
    if state == "healthy":
        data = {"version": 1, "files": {"warm": {"states": []}}}
    cache.write_text("{broken" if state == "unreadable" else json.dumps(data))
    if state == "disabled":
        monkeypatch.setattr(
            _mm_host_usage, "codex_usage_diag", lambda: pytest.fail("disabled probe")
        )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Codex usage capture:" not in result.output


def test_status_and_diag_inventory_nags_when_no_blocker(tmp_path, monkeypatch):
    from tests.test_host_usage import _context, _token, _write_rollout

    _setup_real_config(tmp_path, monkeypatch)
    _enable_test_host_source(tmp_path, "codex")
    cache = _mm_host_usage.CACHE_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"version": 1, "files": {"old": {"day": "2026-09-04"}}}))
    rebuilding = runner.invoke(app, ["status"])
    assert rebuilding.exit_code == 0, rebuilding.output
    assert "rebuilding" in rebuilding.output
    assert "awaiting re-walk" in rebuilding.output
    diag = runner.invoke(app, ["diag"])
    assert diag.exit_code == 0, diag.output
    assert "codex awaiting re-walk" in diag.output
    cache.write_text(
        json.dumps(
            {
                "version": 1,
                "last_reason": "unsupported",
                "files": {"old": {"day": "2026-09-04"}},
            }
        )
    )
    blocked = runner.invoke(app, ["diag"])
    assert blocked.exit_code == 0, blocked.output
    assert "codex awaiting re-walk" not in blocked.output
    cache.write_text(json.dumps({"version": 1, "files": {"warm": {"states": []}}}))
    _write_rollout(_mm_host_usage.CODEX_SESSIONS_PATH, "rollout-a.jsonl", [_context(), _token(1)])
    _write_rollout(_mm_host_usage.CODEX_SESSIONS_PATH, "rollout-b.jsonl", [_context(), _token(2)])
    assert _mm_host_usage.codex_usage_diag()["state"] == "migrating"
    warming = runner.invoke(app, ["status"])
    assert warming.exit_code == 0, warming.output
    text = " ".join(warming.output.split())
    assert "warming" in text
    assert "not yet scanned" in text


def test_autopush_failed_codex_read_reaches_status_after_noop_success(tmp_path, monkeypatch):
    from tests.test_host_usage import (
        _context,
        _grok_turn,
        _token,
        _write_grok_session,
        _write_rollout,
    )

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    _mm_token_usage.warm_token_cache_inline([claude_root])
    _enable_test_host_source(tmp_path, "codex")
    _enable_test_host_source(tmp_path, "grok")
    bad = _token(100)
    bad["payload"]["info"]["total_token_usage"]["input_tokens"] = "unreadable"
    _write_rollout(_mm_host_usage.CODEX_SESSIONS_PATH, "rollout-failing.jsonl", [_context(), bad])
    _write_grok_session(_mm_host_usage.GROK_SESSIONS_PATH, lines=[_grok_turn()])

    first = runner.invoke(app, ["autopush"])
    assert first.exit_code == 0, first.output
    crumb_path = iso / "last-autorun.json"
    crumb = _verb_crumb(json.loads(crumb_path.read_text()), "push")
    assert crumb["outcome"] == "degraded"
    assert "(codex unsupported)" in crumb["detail"]
    rows = [
        json.loads(line)
        for path in (tmp_path / "mm-events" / "events").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    snapshot = next(row for row in rows if row["type"] == "host-usage-snapshot")
    assert snapshot["token_sources"] == ["grok"]
    assert snapshot["degraded_sources"] == ["codex"]
    for attempt in range(2):
        status = runner.invoke(app, ["status"])
        assert status.exit_code == 0, status.output
        assert "Codex usage capture:" in status.output
        assert "(codex unsupported)" in " ".join(status.output.split())
        if attempt == 0:
            monkeypatch.setattr(
                _mm_host_usage, "read_codex_usage", lambda **k: pytest.fail("no-op read")
            )
            second = runner.invoke(app, ["autopush"])
            assert second.exit_code == 0, second.output
            crumb = _verb_crumb(json.loads(crumb_path.read_text()), "push")
            assert crumb["outcome"] == "success"


# ─── Config-surface regressions ──────────────────────────────────────────


@pytest.mark.parametrize("command", ["autopull", "autopush"])
def test_auto_command_silent_exit_when_config_missing(tmp_path, monkeypatch, command):
    """REGRESSION: only FileNotFoundError-equivalent silences auto commands."""
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "nope.toml")
    result = runner.invoke(app, [command])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert (result.stderr or "") == ""


def test_autopull_surfaces_corrupt_config(tmp_path, monkeypatch):
    """REGRESSION: corrupt TOML must not silent-exit (was bare except Exception).

    `ConfigError` from `load_config` is raised via `from tomllib.TOMLDecodeError`,
    so `__cause__` is set -- the _should_log_cause path triggers and the
    original tomllib traceback is preserved in the log. Pure validation errors
    (no `from`) would be stderr-only; covered separately below.
    """
    bad = tmp_path / "config.toml"
    bad.write_text("@@not-valid-toml@@")
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", bad)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    result = runner.invoke(app, ["autopull"])

    assert result.exit_code == 0  # silent-contract: hook never crashes
    assert "pull failed" in (result.stderr or "")
    # Corrupt TOML wraps tomllib.TOMLDecodeError -> log the cause chain.
    log = iso / "autopull.log"
    assert log.exists()
    assert "TOMLDecodeError" in log.read_text() or "tomllib" in log.read_text()


@pytest.mark.parametrize("command", ["autopull", "autopush"])
def test_auto_command_typed_error_strips_terminal_escapes(tmp_path, monkeypatch, command):
    """Typed config errors are plain stderr sinks, not Rich renderers."""
    from mind_meld.errors import ConfigError

    config_path = tmp_path / "config.toml"
    config_path.write_text("[device]\n")
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    _redirect_sidecar(monkeypatch, tmp_path)
    evil = "bad\x1b]52;c;ZXZpbA==\x07config"

    def fail_load_config():
        raise ConfigError(evil)

    monkeypatch.setattr(cli_module, "load_config", fail_load_config)

    result = runner.invoke(app, [command])

    assert result.exit_code == 0
    assert "\x1b" not in (result.stderr or "")
    assert "ZXZpbA==" not in (result.stderr or "")
    assert "badconfig" in (result.stderr or "")


def test_autopush_surfaces_corrupt_config(tmp_path, monkeypatch):
    """REGRESSION: mirror of the above for autopush."""
    bad = tmp_path / "config.toml"
    bad.write_text("!! definitely not toml !!")
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", bad)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    result = runner.invoke(app, ["autopush"])

    assert result.exit_code == 0
    assert "push failed" in (result.stderr or "")
    log = iso / "autopush.log"
    assert log.exists()


def test_autopull_typed_error_without_cause_does_not_log(tmp_path, monkeypatch):
    """Pure validation errors (no __cause__) stay stderr-only -- noise floor."""
    # Write a valid-TOML config that's missing a required field so
    # _validate() raises ConfigError WITHOUT `from` (no cause chain).
    bad = tmp_path / "config.toml"
    bad.write_text("# missing all required sections\n")
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", bad)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    result = runner.invoke(app, ["autopull"])
    assert result.exit_code == 0
    assert "pull failed" in (result.stderr or "")
    # No cause chain -> no log spam.
    assert not (iso / "autopull.log").exists()


def test_autopull_logs_traceback_on_unexpected_config_error(tmp_path, monkeypatch):
    """Non-MindMeldError from load_config MUST log a traceback.

    Drops the `pragma: no cover` on the defensive branch in _auto_command_setup.
    Exercises the path where config.py's wrapper misses a case and lets
    something through unwrapped.
    """
    storage_dir = tmp_path / "storage"
    claude_dir = tmp_path / ".claude"
    _populate_claude(claude_dir)
    config_path, _ = _make_config(tmp_path, storage_dir, claude_dir)
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("simulated unwrapped error from load_config")

    monkeypatch.setattr("mind_meld.cli.load_config", boom)

    result = runner.invoke(app, ["autopull"])

    assert result.exit_code == 0
    assert "unexpected config error" in (result.stderr or "")
    log = iso / "autopull.log"
    assert log.exists()
    assert "RuntimeError: simulated unwrapped error from load_config" in log.read_text()


# ─── Unexpected-exception logging ────────────────────────────────────────


def test_autopull_logs_traceback_on_unexpected_exception(tmp_path, monkeypatch):
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise AttributeError("NoneType has no attribute 'bogus'")

    monkeypatch.setattr("mind_meld.cli._pull_core", boom)

    result = runner.invoke(app, ["autopull"])

    assert result.exit_code == 0
    assert "pull failed" in (result.stderr or "")
    assert "unexpected error" in (result.stderr or "")
    log = iso / "autopull.log"
    assert log.exists()
    body = log.read_text()
    assert "AttributeError" in body
    assert "NoneType has no attribute 'bogus'" in body


def test_autopush_logs_traceback_on_unexpected_exception(tmp_path, monkeypatch):
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("wat")

    monkeypatch.setattr("mind_meld.cli._push_core", boom)

    result = runner.invoke(app, ["autopush"])

    assert result.exit_code == 0
    assert "push failed" in (result.stderr or "")
    log = iso / "autopush.log"
    assert log.exists()
    assert "RuntimeError: wat" in log.read_text()


# ─── Lock contention ─────────────────────────────────────────────────────


def test_autopull_silent_when_lock_held(tmp_path, monkeypatch):
    """autopull mirrors autopush: silent on LockError, no crash, no traceback."""
    import subprocess
    import sys
    import textwrap
    import time

    _setup_real_config(tmp_path, monkeypatch)
    from mind_meld.config import LOCK_PATH

    repo_src = str(Path(__file__).parent.parent / "src")
    ready_marker = tmp_path / "child-ready"
    child_script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {repo_src!r})
        from pathlib import Path
        from mind_meld.lockfile import acquire_lock, release_lock

        lp = Path({str(LOCK_PATH)!r})
        acquire_lock(lp)
        Path({str(ready_marker)!r}).write_text("ready")
        sys.stdin.read()
        release_lock(lp)
    """).strip()

    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdin=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if ready_marker.exists():
                break
            time.sleep(0.02)
        else:
            child.kill()
            pytest.fail("child did not become ready")

        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0
        assert result.stdout == ""
        assert "Traceback" not in (result.stderr or "")
    finally:
        if child.stdin:
            child.stdin.close()
        child.wait(timeout=5)


# ─── Log appender truncation ─────────────────────────────────────────────


def test_auto_log_truncates_when_over_1mb(tmp_path, monkeypatch):
    """_log_unexpected keeps the tail when the log grows past 1MB."""
    from mind_meld import cli as cli_mod

    iso = _redirect_sidecar(monkeypatch, tmp_path)
    logp = iso / "autopull.log"
    # Pre-fill with > 1MB of garbage.
    logp.write_text("x" * 1_100_000)
    assert logp.stat().st_size > cli_mod._AUTO_LOG_MAX_BYTES

    cli_mod._log_unexpected("pull", RuntimeError("fresh"))

    size = logp.stat().st_size
    body = logp.read_text()
    assert size < cli_mod._AUTO_LOG_MAX_BYTES
    assert "fresh" in body  # the new entry survived
    # Most of the original padding was dropped.
    assert body.count("x") < 1_000_000


def test_log_appender_idempotent_across_repeat_invocations(tmp_path, monkeypatch):
    """Calling _log_unexpected N times writes N blocks (no hidden global state)."""
    from mind_meld import cli as cli_mod

    iso = _redirect_sidecar(monkeypatch, tmp_path)
    logp = iso / "autopull.log"

    for i in range(5):
        cli_mod._log_unexpected("pull", RuntimeError(f"err-{i}"))

    body = logp.read_text()
    # Five distinct blocks, each with its own header.
    assert body.count("--- ") == 5
    for i in range(5):
        assert f"err-{i}" in body


# ─── Unknown-source warning on pull ──────────────────────────────────────


def test_pull_warns_on_unknown_source(tmp_path, monkeypatch):
    """Peer advertises a source the local config doesn't know about."""
    # Phase 1: device A (has both claude+gstack) pushes.
    storage_dir = tmp_path / "storage"
    claude_a = tmp_path / ".claude"
    gstack_a = tmp_path / ".gstack"
    _populate_claude(claude_a)
    (gstack_a / "projects").mkdir(parents=True)
    (gstack_a / "projects" / "state.yaml").write_text("active: true")
    (gstack_a / "config.yaml").write_text("version: 1")

    config_a = tmp_path / "config_a.toml"
    save_config(
        {
            "device": {"id": "dev-a", "name": "Mac A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_a), "type": "claude"},
                    {
                        "name": "gstack",
                        "path": str(gstack_a),
                        "type": "generic",
                        "include_dirs": ["projects"],
                        "include_files": ["config.yaml"],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_a,
    )

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-a", "Mac A")
    register_device(backend, "dev-b", "Mac B")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    r = runner.invoke(app, ["push"])
    assert r.exit_code == 0, r.stderr

    # Phase 2: switch to device B (claude-only -- no gstack configured).
    claude_b = tmp_path / "machine_b" / ".claude"
    claude_b.mkdir(parents=True)
    config_b = tmp_path / "config_b.toml"
    save_config(
        {
            "device": {"id": "dev-b", "name": "Mac B"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_b), "type": "claude"},
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_b,
    )
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)

    r = runner.invoke(app, ["pull"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    # Warning prints regardless of --verbose (the whole point of the fix).
    assert "unknown source 'gstack'" in (r.stdout + (r.stderr or ""))
    assert "unknown source(s) skipped" in r.stdout


# ─── --conflict-mode fail preflight ──────────────────────────────────────


def test_pull_conflict_mode_fail_exits_3_before_writes(tmp_path, monkeypatch):
    """Preflight detects a would-be conflict and exits 3 with no writes.

    Exit 3 (not 2) avoids colliding with typer/click's usage-error exit code,
    so CI scripts that still pass the removed --no-prompt flag get exit 2
    (usage error) distinct from "conflict refusal" (exit 3).
    """
    # Setup: device A pushes a file; device B has a LOCAL edit to the same
    # path. Pull on B should preflight, notice the conflict, and bail.
    # Force a deterministic mtime gap with `os.utime` so the test doesn't
    # depend on filesystem mtime resolution (APFS is sub-second but slow CI
    # runners or NFS can collapse two close writes to the same second).
    storage_dir = tmp_path / "storage"
    claude_a = tmp_path / "machine_a" / ".claude"
    claude_b = tmp_path / "machine_b" / ".claude"
    _populate_claude(claude_b, contents="from B -- locally edited")
    _populate_claude(claude_a, contents="from A")
    b_file = claude_b / "projects" / "-Users-kb-myapp" / "memory" / "role.md"
    a_file = claude_a / "projects" / "-Users-kb-myapp" / "memory" / "role.md"
    os.utime(b_file, (1_700_000_000, 1_700_000_000))
    os.utime(a_file, (1_700_000_100, 1_700_000_100))

    config_a, _ = _make_config(tmp_path / "a", storage_dir, claude_a, "dev-a", "Mac A")
    config_b, _ = _make_config(tmp_path / "b", storage_dir, claude_b, "dev-b", "Mac B")

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-a", "Mac A")
    register_device(backend, "dev-b", "Mac B")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    r = runner.invoke(app, ["push"])
    assert r.exit_code == 0, r.stderr

    # Switch to device B.
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)

    role = claude_b / "projects" / "-Users-kb-myapp" / "memory" / "role.md"
    snapshot_before = role.read_text()

    r = runner.invoke(app, ["pull", "--conflict-mode", "fail"])
    assert r.exit_code == 3, (r.stdout, r.stderr)
    # No writes: local file unchanged, no .sync-conflict-* created.
    assert role.read_text() == snapshot_before
    siblings = list(role.parent.iterdir())
    assert all(".sync-conflict-" not in p.name for p in siblings)
    # Conflict is listed by name.
    assert "role.md" in r.stdout


# ─── Devices table header ────────────────────────────────────────────────


def test_devices_table_shows_last_push_header(tmp_path, monkeypatch):
    """Column header is 'Last Push' and registered-no-push devices render em-dash."""
    _setup_real_config(tmp_path, monkeypatch)
    backend = LocalBackend(tmp_path / "storage")
    register_device(backend, "dev-b", "Mac B")  # never pushed

    r = runner.invoke(app, ["devices"])
    assert r.exit_code == 0
    assert "Last Push" in r.stdout
    # dev-b has no last_seen, so the column shows an em-dash.
    assert "—" in r.stdout


# ─── get_passphrase non_interactive ──────────────────────────────────────


def test_get_passphrase_non_interactive_raises_instead_of_hanging(monkeypatch):
    """non_interactive=True must raise instead of calling getpass.getpass()."""
    from mind_meld.crypto import get_passphrase
    from mind_meld.errors import CryptoError

    # Guarantee neither keyring nor env provides a passphrase.
    monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)

    # Poison getpass so if we ever fall through, we'd see a clear failure
    # instead of a hang.
    def explode(*a, **kw):
        raise AssertionError("getpass.getpass was reached under non_interactive=True")

    import getpass

    monkeypatch.setattr(getpass, "getpass", explode)

    # And stub keyring to return None (so keyring doesn't fulfil the request).
    try:
        import keyring as kr

        monkeypatch.setattr(kr, "get_password", lambda *a, **kw: None)
    except ImportError:
        pass

    with pytest.raises(CryptoError, match="no passphrase available"):
        get_passphrase(non_interactive=True)


# ─── quiet-mode load-bearing warnings ────────────────────────────────────


def test_autopull_surfaces_corrupt_peer_manifest_in_quiet_mode(tmp_path, monkeypatch):
    """autopull (quiet=True) must still print corrupt-peer-manifest warning."""
    _setup_real_config(tmp_path, monkeypatch)
    # Register a peer and plant a bogus manifest blob that won't decrypt.
    backend = LocalBackend(tmp_path / "storage")
    register_device(backend, "dev-b", "Mac B")
    backend.put("manifests/dev-b/manifest.json.enc", b"not-a-real-blob")

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    # Warning must be in stderr (the autopull contract).
    assert "corrupt" in (r.stderr or "")
    assert "Mac B" in (r.stderr or "")


def test_autopush_surfaces_sidecar_write_failure_in_quiet_mode(tmp_path, monkeypatch):
    """autopush (quiet=True) must still warn when sidecar.write fails."""
    _setup_real_config(tmp_path, monkeypatch)

    def exploding_sidecar_write(*a, **kw):
        raise OSError("simulated disk full")

    monkeypatch.setattr("mind_meld.sidecar.write", exploding_sidecar_write)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "sidecar" in (r.stderr or "")
    assert "simulated disk full" in (r.stderr or "")
    crumb = cli_module._read_autorun_breadcrumbs()["push"]
    assert crumb["outcome"] == "degraded"
    assert "1 post-publication maintenance failure(s)" in crumb["detail"]


# ─── Typed-error branches in autopull/autopush (contract tests) ──────────


def test_autopull_mindmelderror_stderr_no_log(tmp_path, monkeypatch):
    """MindMeldError from _pull_core -> typed stderr, no traceback log.

    Expected failures shouldn't spam autopull.log; only unexpected ones should.
    """
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    from mind_meld.errors import CryptoError

    def typed_boom(*a, **kw):
        raise CryptoError("decrypt failed on peer blob")

    monkeypatch.setattr("mind_meld.cli._pull_core", typed_boom)

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    assert "pull failed" in (r.stderr or "")
    assert "decrypt failed on peer blob" in (r.stderr or "")
    assert "unexpected error" not in (r.stderr or "")
    assert not (iso / "autopull.log").exists()


def test_interactive_snapshoterror_exits_1_no_traceback(tmp_path, monkeypatch):
    _setup_real_config(tmp_path, monkeypatch)
    from mind_meld.errors import SnapshotError

    def boom(*a, **kw):
        raise SnapshotError(
            "Cannot publish: source claude, a.md could not be read (EACCES 13). "
            "Previous snapshot kept. Restore read access, then run mm push."
        )

    monkeypatch.setattr("mind_meld.cli._push_core", boom)
    r = runner.invoke(app, ["push"])
    assert r.exit_code == 1
    combined = r.output + (r.stderr or "")
    assert "Cannot publish" in combined
    assert "Traceback" not in combined
    assert "Push complete" not in combined


def test_autopush_snapshoterror_exit_0_failed_breadcrumb(tmp_path, monkeypatch):
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    from mind_meld.errors import SnapshotError

    def boom(*a, **kw):
        raise SnapshotError(
            "Cannot publish: source claude, a.md changed while being read."
        ) from None

    monkeypatch.setattr("mind_meld.cli._push_core", boom)
    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0
    assert "push failed" in (r.stderr or "")
    assert "Cannot publish" in (r.stderr or "")
    assert "unexpected error" not in (r.stderr or "")
    crumb_path = iso / "last-autorun.json"
    assert crumb_path.exists()
    entry = _verb_crumb(json.loads(crumb_path.read_text()), "push")
    assert entry.get("outcome") == "failed"
    assert not (iso / "autopush.log").exists()


def test_autopush_snapshoterror_with_os_cause_logs_traceback(tmp_path, monkeypatch):
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    from mind_meld.errors import SnapshotError

    def boom(*a, **kw):
        raise SnapshotError(
            "Cannot publish: source claude, a.md could not be read."
        ) from PermissionError("denied")

    monkeypatch.setattr("mind_meld.cli._push_core", boom)
    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0
    assert "push failed" in (r.stderr or "")
    assert (iso / "autopush.log").exists()


def test_autopush_mindmelderror_stderr_no_log(tmp_path, monkeypatch):
    """Mirror: MindMeldError from _push_core -> typed stderr, no log."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    from mind_meld.errors import StorageError

    def typed_boom(*a, **kw):
        raise StorageError("backend refused")

    monkeypatch.setattr("mind_meld.cli._push_core", typed_boom)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0
    assert "push failed" in (r.stderr or "")
    assert "backend refused" in (r.stderr or "")
    assert "unexpected error" not in (r.stderr or "")
    assert not (iso / "autopush.log").exists()


def test_autopull_no_passphrase_prints_skipped_line(tmp_path, monkeypatch):
    """End-to-end: keyring empty + env unset -> 'pull skipped - no passphrase'."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
    try:
        import keyring

        monkeypatch.setattr(keyring, "get_password", lambda *a, **kw: None)
    except ImportError:
        pass

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    assert "pull skipped" in (r.stderr or "")
    assert "no passphrase available" in (r.stderr or "")
    assert "(backend keyring.backends.fail.Keyring)" in (r.stderr or "")
    assert not (iso / "autopull.log").exists()
    # Track 73A: the detail records the backend this hook process saw.
    entry = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "pull")
    assert entry.get("outcome") == "no-passphrase"
    assert entry.get("detail") == "keyring.backends.fail.Keyring"


# ─── Track 73A: keyring-locked auto-command skips (end-to-end) ──────────
#
# Portable seam: a real keyring Secret Service backend object (it
# constructs on macOS) over a fake secretstorage module, with
# keyring.get_password patched to raise if called. Runs in every CI job.

_KEYRING_URL = "https://github.com/kbitz/mind-meld#auto-commands-skip-keyring-locked-or-unavailable"


def _keyring_skip_scenario(harness, reason: str) -> None:
    from tests.conftest import _FakeDBusError

    if reason == "locked":
        harness.locked = True
    elif reason == "no-default-collection":
        harness.collection_error = harness.ItemNotFoundException("no alias")
    elif reason == "locked-race":
        harness.items = [harness.make_item(error=harness.LockedException("locked"))]
    elif reason == "is-locked":
        harness.items = [
            harness.make_item(error=_FakeDBusError("org.freedesktop.Secret.Error.IsLocked"))
        ]
    else:  # pragma: no cover - parametrize guard
        raise AssertionError(reason)


@pytest.mark.parametrize("verb", ["pull", "push"])
@pytest.mark.parametrize("reason", ["locked", "no-default-collection", "locked-race", "is-locked"])
def test_auto_command_skips_when_keyring_cannot_answer(
    tmp_path, monkeypatch, ss_harness, verb, reason
):
    from tests.conftest import _ss_member, _use_backend

    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
    _keyring_skip_scenario(ss_harness, reason)
    _use_backend(monkeypatch, _ss_member())

    r = runner.invoke(app, [f"auto{verb}"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    if reason == "no-default-collection":
        expected = (
            f"mm: {verb} skipped - keyring has no default collection; "
            "create one and store the passphrase with secret-tool, "
            f"then rerun mm auto{verb}. See {_KEYRING_URL}"
        )
    else:
        expected = (
            f"mm: {verb} skipped - keyring locked "
            "(Secret Service collection is locked); "
            "unlock your login keyring in the desktop session, "
            f"then rerun mm auto{verb}. See {_KEYRING_URL}"
        )
    assert expected in (r.stderr or "").splitlines()
    entry = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), verb)
    if reason == "no-default-collection":
        assert entry.get("outcome") == "keyring-no-collection"
        assert entry.get("detail") is None
    else:
        assert entry.get("outcome") == "keyring-locked"
        assert entry.get("detail") == "locked"
    assert not (iso / f"auto{verb}.log").exists()
    assert ss_harness.prompt_calls == []


@pytest.mark.parametrize("verb", ["pull", "push"])
@pytest.mark.parametrize("kind", ["keychain", "other"])
def test_auto_command_skips_when_keyring_denies(tmp_path, monkeypatch, verb, kind):
    """KeyringLocked from a non-Secret-Service backend -> keyring-locked/denied."""
    from keyring.errors import KeyringLocked

    from tests.conftest import _FakeBackend, _use_backend

    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
    module = "keyring.backends.macOS" if kind == "keychain" else "keyring.backends.kwallet"
    backend_cls = type("Keyring", (_FakeBackend,), {"__module__": module})
    _use_backend(monkeypatch, backend_cls(error=KeyringLocked("denied")))

    r = runner.invoke(app, [f"auto{verb}"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    if kind == "keychain":
        expected = (
            f"mm: {verb} skipped - keyring locked (Keychain access was denied); "
            f"run mm {verb} in a terminal in your desktop session and choose "
            f"Always Allow, then rerun mm auto{verb}. See {_KEYRING_URL}"
        )
    else:
        expected = (
            f"mm: {verb} skipped - keyring locked (desktop keyring refused access); "
            "unlock or allow access in your desktop keyring, "
            f"then rerun mm auto{verb}. See {_KEYRING_URL}"
        )
    assert expected in (r.stderr or "").splitlines()
    entry = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), verb)
    assert (entry.get("outcome"), entry.get("detail")) == ("keyring-locked", "denied")
    assert not (iso / f"auto{verb}.log").exists()


def test_autopull_no_passphrase_records_unknown_backend(tmp_path, monkeypatch):
    """A backend lookup that raises records detail `unknown` (D1 label)."""
    import keyring
    from keyring.errors import NoKeyringError

    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)

    def no_backend():
        raise NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_keyring", no_backend)
    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    assert "(backend unknown)" in (r.stderr or "")
    entry = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "pull")
    assert (entry.get("outcome"), entry.get("detail")) == ("no-passphrase", "unknown")


@pytest.mark.parametrize(
    "outcome,detail",
    [
        ("keyring-no-collection", ""),
        ("keyring-locked", ""),
        ("keyring-locked", "denied"),
        ("keyring-locked", "bogus"),
        ("no-passphrase", "keyring.backends.fail.Keyring"),  # control: no hint
    ],
)
def test_status_hint_for_every_keyring_outcome(tmp_path, monkeypatch, outcome, detail):
    """Spec 3.4: both outcomes hint, with or without a (known) detail."""
    _setup_real_config(tmp_path, monkeypatch)
    _redirect_sidecar(monkeypatch, tmp_path)
    # Private size fields, not the width property: the setter's undo pins
    # the computed size for the rest of the worker. Rich ignores a
    # width-only pin when TERM=dumb and wraps at 80 columns.
    monkeypatch.setattr(cli_module.console, "_width", 240)
    monkeypatch.setattr(cli_module.console, "_height", 50)
    cli_module._write_autorun_breadcrumb("pull", outcome, detail)

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    hint = f"The last auto-pull was skipped: keyring locked or unavailable; see {_KEYRING_URL}"
    assert (hint in r.stdout) is outcome.startswith("keyring-")


def test_status_hint_after_keyring_skip(tmp_path, monkeypatch, ss_harness):
    import keyring

    from tests.conftest import _ss_member, _use_backend

    _setup_real_config(tmp_path, monkeypatch)
    _redirect_sidecar(monkeypatch, tmp_path)
    # Private size fields, not the width property: the setter's undo pins
    # the computed size for the rest of the worker. Rich ignores a
    # width-only pin when TERM=dumb and wraps at 80 columns.
    monkeypatch.setattr(cli_module.console, "_width", 240)
    monkeypatch.setattr(cli_module.console, "_height", 50)

    control = runner.invoke(app, ["status"])
    assert control.exit_code == 0, control.output
    assert "was skipped: keyring locked or unavailable" not in control.stdout

    monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
    ss_harness.locked = True
    _use_backend(monkeypatch, _ss_member())
    skipped = runner.invoke(app, ["autopull"])
    assert skipped.exit_code == 0

    # status reads the passphrase interactively first; give it the env
    # value and a non-exploding keyring.get_password again.
    monkeypatch.setattr(keyring, "get_password", lambda *a, **kw: None)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert (
        f"The last auto-pull was skipped: keyring locked or unavailable; see {_KEYRING_URL}"
    ) in result.stdout


def test_pull_conflict_mode_prompt_threads_interactive_flag(tmp_path, monkeypatch):
    """--conflict-mode prompt must thread interactive_resolve=True into _download_and_apply.

    Replacement for the removed --resolve-interactive flag. If the translation at
    `interactive_resolve_flag = (conflict_mode == 'prompt')` regresses, nothing else
    catches it.
    """
    _setup_real_config(tmp_path, monkeypatch)
    seen = {"called": False, "interactive_resolve": None}

    def spy_download(*a, **kw):
        seen["called"] = True
        seen["interactive_resolve"] = kw.get("interactive_resolve")
        return 0, {
            k: [] for k in ("written", "merged", "skipped", "conflicted", "unchanged", "failed")
        }

    monkeypatch.setattr("mind_meld.cli._download_and_apply", spy_download)

    # Push something so there's work for pull to do on a second device.
    r = runner.invoke(app, ["push"])
    assert r.exit_code == 0

    # Register a peer so _pull_core actually enters the source loop.
    backend = LocalBackend(tmp_path / "storage")
    register_device(backend, "dev-b", "Mac B")
    # Fake a peer manifest by copying dev-a's (A's perspective pulling B).
    src_key = "manifests/dev-a/manifest.json.enc"
    dst_key = "manifests/dev-b/manifest.json.enc"
    backend.put(dst_key, backend.get(src_key))

    r = runner.invoke(app, ["pull", "--conflict-mode", "prompt", "--from", "dev-b"])
    # Even with no actual changes, spy may or may not be called depending on diff.
    # What we care about: if it IS called, it got interactive_resolve=True.
    if seen["called"]:
        assert seen["interactive_resolve"] is True


# ─── Log safety ──────────────────────────────────────────────────────────


def test_log_unexpected_swallows_write_failure(tmp_path, monkeypatch):
    """Load-bearing safety: a broken log file must never crash the hook."""
    from mind_meld import cli as cli_mod

    _redirect_sidecar(monkeypatch, tmp_path)

    # Make the log write attempt fail at open().
    real_open = open

    def selective_boom(path, *a, **kw):
        if str(path).endswith("autopull.log"):
            raise PermissionError("simulated perms denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", selective_boom)

    # Must not raise.
    cli_mod._log_unexpected("pull", RuntimeError("real error"))


# ─── Breadcrumb (last-autorun.json) tests ────────────────────────────────


def _seed_conflict_pair(tmp_path: Path) -> tuple[Path, Path]:
    local = tmp_path / "doc.md"
    local.write_bytes(b"local")
    old = tmp_path / "doc.sync-conflict-20260421-120000-v1-devAAAA1.md"
    old.write_bytes(b"peer R1")
    return local, old


def test_conflict_path_build_error_is_sanitized_stderr(tmp_path, monkeypatch, capsys):
    from mind_meld.cli import _apply_conflict

    local, _old = _seed_conflict_pair(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "conflict_filename",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad id \x1b[31mRED")),
    )
    assert _apply_conflict(local, "doc.md", b"peer R2", "devAAAA1234") == "failed"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "conflict path build failed" in captured.err
    assert "\x1b[31m" not in captured.err
    assert "[31m" not in captured.err


def test_conflict_write_error_is_sanitized_stderr(tmp_path, monkeypatch, capsys):
    from mind_meld.cli import _apply_conflict

    local, _old = _seed_conflict_pair(tmp_path)
    monkeypatch.setattr(
        cli_module.fsutil,
        "atomic_write_bytes",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full \x1b[31mRED")),
    )
    assert _apply_conflict(local, "doc.md", b"peer R2", "devAAAA1234") == "failed"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "sidecar write failed" in captured.err
    assert "\x1b[31m" not in captured.err


def test_conflict_cleanup_error_is_sanitized_stderr(tmp_path, monkeypatch, capsys):
    from mind_meld.cli import _apply_conflict

    local, old = _seed_conflict_pair(tmp_path)
    real_unlink = Path.unlink

    def fail_old(self, missing_ok=False):
        if self == old:
            raise OSError("unlink \x1b[31mRED")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_old)
    assert _apply_conflict(local, "doc.md", b"peer R2", "devAAAA1234") == "conflicted"
    captured = capsys.readouterr()
    assert captured.out == "" or "conflict:" not in captured.out
    assert "replacement saved" in captured.err
    assert "mm resolve" in captured.err
    assert "\x1b[31m" not in captured.err


def test_autopull_cleanup_only_conflict_keeps_success_breadcrumb(tmp_path, monkeypatch):
    """Cleanup leftovers are conflicted extras, not a failed/degraded pull."""
    from mind_meld.cli import PullResult

    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    fake_result = PullResult(
        total_written=1,
        total_conflicted=1,
        total_failed=0,
        device_names=["Mac B"],
        elapsed=0.1,
    )
    monkeypatch.setattr("mind_meld.cli._pull_core", lambda *a, **kw: fake_result)
    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "conflicts" in (r.stderr or "")
    data = json.loads((iso / "last-autorun.json").read_text())
    pull = _verb_crumb(data, "pull")
    assert pull["outcome"] == "success"


def test_autopull_writes_breadcrumb_on_success(tmp_path, monkeypatch):
    """Success path writes `{verb: 'pull', outcome: 'success'}`."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    crumb = iso / "last-autorun.json"
    assert crumb.exists()
    data = json.loads(crumb.read_text())
    pull = _verb_crumb(data, "pull")
    assert pull["outcome"] == "success"
    assert "timestamp" in pull
    assert "push" not in data


@pytest.mark.parametrize(
    ("command", "verb"),
    [("autopull", "pull"), ("autopush", "push")],
)
def test_auto_command_writes_breadcrumb_on_lock_held(tmp_path, monkeypatch, command, verb):
    """LockError still silent-exits but now leaves a 'lock-held' breadcrumb."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    def boom(*a, **kw):
        from mind_meld.errors import LockError

        raise LockError("already held by PID 12345")

    monkeypatch.setattr("mind_meld.cli.acquire_lock", boom)

    r = runner.invoke(app, [command])
    assert r.exit_code == 0
    assert r.stdout == ""  # silent to Claude
    crumb = iso / "last-autorun.json"
    assert crumb.exists()
    data = json.loads(crumb.read_text())
    entry = _verb_crumb(data, verb)
    assert entry["outcome"] == "lock-held"


def test_autopull_does_not_erase_autopush_crumb(tmp_path, monkeypatch):
    """Documented lifecycle is autopull at conversation start, autopush at
    end. Last-write-wins on a single verb field erased every degraded push."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    data = json.loads((iso / "last-autorun.json").read_text())
    assert "push" in data
    assert "pull" in data
    assert data["push"]["outcome"]
    assert data["pull"]["outcome"] == "success"


def test_concurrent_autorun_breadcrumbs_preserve_each_verb(tmp_path, monkeypatch):
    """A lock-held pull must not erase a concurrent degraded push crumb."""
    from mind_meld import lockedjson

    iso = _redirect_sidecar(monkeypatch, tmp_path)
    first_write_entered = threading.Event()
    second_lock_attempted = threading.Event()
    release_first_write = threading.Event()
    original_acquire_lock = lockedjson._acquire_lock
    original_write_json = lockedjson._write_json
    acquire_count = 0
    write_count = 0

    def note_second_lock_attempt(fd, **kwargs):
        nonlocal acquire_count
        acquire_count += 1
        if acquire_count == 2:
            second_lock_attempted.set()
        return original_acquire_lock(fd, **kwargs)

    def pause_first_write(fd, data, compact=False):
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            first_write_entered.set()
            assert release_first_write.wait(timeout=2), "test did not release first writer"
        return original_write_json(fd, data, compact=compact)

    monkeypatch.setattr(lockedjson, "_acquire_lock", note_second_lock_attempt)
    monkeypatch.setattr(lockedjson, "_write_json", pause_first_write)
    push = threading.Thread(
        target=cli_module._write_autorun_breadcrumb,
        args=("push", "degraded", "events tail failed"),
    )
    pull = threading.Thread(
        target=cli_module._write_autorun_breadcrumb,
        args=("pull", "lock-held"),
    )

    try:
        push.start()
        assert first_write_entered.wait(timeout=2), "writer did not use the shared JSON lock"
        pull.start()
        assert second_lock_attempted.wait(timeout=2), "second writer did not contend on the lock"
    finally:
        release_first_write.set()

    push.join(timeout=2)
    pull.join(timeout=2)
    assert not push.is_alive()
    assert not pull.is_alive()
    payload = json.loads((iso / "last-autorun.json").read_text())
    assert payload["push"]["outcome"] == "degraded"
    assert payload["push"]["detail"] == "events tail failed"
    assert payload["push"]["timestamp"]
    assert payload["pull"]["outcome"] == "lock-held"
    assert payload["pull"]["timestamp"]


def test_mm_status_surfaces_breadcrumb(tmp_path, monkeypatch):
    """`mm status` shows the last autopull/autopush attempt when a breadcrumb exists."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    # Plant a breadcrumb.
    (iso / "last-autorun.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-23T12:00:00+00:00",
                "verb": "pull",
                "outcome": "lock-held",
            }
        )
    )

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    assert "Last auto-pull" in r.stdout
    assert "lock-held" in r.stdout
    assert "2026-04-23" in r.stdout


@pytest.mark.parametrize(
    "outcome,detail,expect_nag",
    [
        ("degraded", "1 file(s) failed", True),
        ("degraded", "fsync failed on 1 parent dir(s)", False),
        ("failed", "1 file(s) failed", False),
    ],
)
def test_status_file_failure_nag_is_detail_gated(
    tmp_path, monkeypatch, outcome, detail, expect_nag
):
    """The Track 53A nag is keyed on pull + degraded + 'file(s) failed'."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    (iso / "last-autorun.json").write_text(
        json.dumps(
            {
                "pull": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "outcome": outcome,
                    "detail": detail,
                }
            }
        )
    )

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    nag = "Files failed to apply on the last auto-pull"
    if expect_nag:
        assert nag in r.stdout
    else:
        assert nag not in r.stdout


# ─── Cross-peer preflight test ───────────────────────────────────────────


def test_pull_conflict_mode_fail_catches_cross_peer_conflict(tmp_path, monkeypatch):
    """Two peers target the same path with different content.

    Starting local state is empty. Preflight walking peer A alone predicts
    'write' (no conflict). Peer B would conflict against peer A's fresh
    write. The overlay-based preflight MUST catch this before any writes
    land -- otherwise --conflict-mode fail can still produce .sync-conflict-*.
    """
    storage_dir = tmp_path / "storage"
    claude_a = tmp_path / "machine_a" / ".claude"
    claude_b = tmp_path / "machine_b" / ".claude"
    claude_local = tmp_path / "machine_local" / ".claude"
    _populate_claude(claude_a, contents="from A")
    _populate_claude(claude_b, contents="from B (different)")
    # Local starts empty at the target path (dir exists, file absent).
    (claude_local / "projects" / "-Users-kb-myapp" / "memory").mkdir(parents=True)

    config_a, _ = _make_config(tmp_path / "a", storage_dir, claude_a, "dev-a", "Mac A")
    config_b, _ = _make_config(tmp_path / "b", storage_dir, claude_b, "dev-b", "Mac B")
    config_l, _ = _make_config(tmp_path / "l", storage_dir, claude_local, "dev-l", "Mac Local")

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    for did, name in [("dev-a", "Mac A"), ("dev-b", "Mac B"), ("dev-l", "Mac Local")]:
        register_device(backend, did, name)

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    assert runner.invoke(app, ["push"]).exit_code == 0

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
    assert runner.invoke(app, ["push"]).exit_code == 0

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_l)

    r = runner.invoke(app, ["pull", "--conflict-mode", "fail"])
    assert r.exit_code == 3, (r.stdout, r.stderr)
    # Cross-peer conflict was detected; local file never written.
    target = claude_local / "projects" / "-Users-kb-myapp" / "memory" / "role.md"
    assert not target.exists()


# ─── Log concurrency (flock) test ────────────────────────────────────────


def test_log_unexpected_survives_concurrent_writers(tmp_path, monkeypatch):
    """Two threads hammering _log_unexpected don't lose each other's entries.

    Without flock, truncate-tail could race; this test exercises the lock.
    """
    import threading

    from mind_meld import cli as cli_mod

    _redirect_sidecar(monkeypatch, tmp_path)
    iso = tmp_path / "sidecar"

    def hammer(idx):
        for i in range(10):
            cli_mod._log_unexpected("pull", RuntimeError(f"t{idx}-{i}"))

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    body = (iso / "autopull.log").read_text()
    # 40 total writes -> 40 headers if no writer got clobbered.
    assert body.count("--- ") == 40
    # Every entry survives.
    for idx in range(4):
        for i in range(10):
            assert f"t{idx}-{i}" in body


def test_autopull_unexpected_crypto_error_logs(tmp_path, monkeypatch):
    """Non-MindMeldError from _init_crypto_session -> logged traceback."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("unexpected crypto boom")

    monkeypatch.setattr("mind_meld.cli._init_crypto_session", boom)

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    assert "unexpected crypto error" in (r.stderr or "")
    log = iso / "autopull.log"
    assert log.exists()
    assert "RuntimeError: unexpected crypto boom" in log.read_text()


# ─── Group 2 Pre-flight + 2A.2: _error() stderr routing ──────────────────


def test_error_writes_to_stderr_not_stdout(tmp_path, monkeypatch):
    """REGRESSION (Group 2 Track 2A.2): _error() must route to stderr.

    Before the fix, `console.print(f"[red]Error:[/red] {msg}")` hit stdout.
    In autopush/autopull quiet mode this violated the one-line-stderr
    contract (README.md "Claude Code Integration"): failures emitted a
    rich stdout line AND the outer plain-text stderr line, confusing
    Claude Code integration.

    We invoke push on a broken config so _error() fires deterministically.
    """
    # Point config path at a non-TOML file so load_config raises ConfigError,
    # which _get_config converts to _error("<ConfigError msg>").
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not = valid toml [[[")
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", bad)

    r = runner.invoke(app, ["push"])

    # Exit code 1 from _error's typer.Exit(1).
    assert r.exit_code == 1
    # The error text must be on stderr.
    assert "Error:" in (r.stderr or "")
    # And stdout must be clean — no rich error leakage. Before the fix,
    # stdout would carry a [red]Error:[/red]-expanded string.
    assert "Error:" not in (r.stdout or "")


def test_error_preserves_rich_formatting_on_stderr(monkeypatch):
    """Rich Console(stderr=True) still emits color codes when attached to
    a real terminal; the forced-terminal option proves the formatting
    pipeline still runs (color bytes in the captured stream).

    This protects against a naive "just print() to stderr" refactor that
    would silently drop the [red]Error:[/red] styling for interactive users.
    """
    import io

    from rich.console import Console

    # The test explicitly verifies ANSI output. The host's NO_COLOR choice is
    # correct for normal CLI output but would otherwise override Rich's forced
    # terminal setup and turn this into an environment-dependent assertion.
    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()
    c = Console(file=buf, stderr=True, force_terminal=True, color_system="truecolor")
    c.print("[red]Error:[/red] it blew up")
    out = buf.getvalue()
    # Rich emits an ANSI escape for red when force_terminal + color_system
    # are set. The presence of ESC[ proves styling survived.
    assert "\x1b[" in out
    assert "Error:" in out
    assert "it blew up" in out


# ─── Group 1 / Track 1A: quiet-path audit fixes ──────────────────────────


def test_autopush_surfaces_corrupt_manifest_sidecar_recovery_in_quiet_mode(tmp_path, monkeypatch):
    """When THIS device's remote manifest is corrupt and we recover from the
    local sidecar during autopush, the recovery must surface to stderr.
    Silently swallowing this leaves the user blind to storage degradation.
    """
    _setup_real_config(tmp_path, monkeypatch)

    # First push to produce a valid sidecar + remote manifest.
    r = runner.invoke(app, ["push"])
    assert r.exit_code == 0, r.stderr

    # Corrupt this device's remote manifest so the next push triggers recovery.
    backend = LocalBackend(tmp_path / "storage")
    backend.put("manifests/dev-a/manifest.json.enc", b"definitely-not-a-real-blob")

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    # Sidecar recovery branch fired — must be visible on stderr in quiet mode.
    assert "corrupt" in (r.stderr or "")
    assert "sidecar" in (r.stderr or "")


def test_autopull_surfaces_total_failed_count_on_stderr(tmp_path, monkeypatch):
    """REGRESSION (adversarial review finding): autopull surfaces total_conflicted
    and total_skipped_unknown_source on stderr, but used to silently swallow
    total_failed. Per-file failures (decrypt, write, ValueError on corrupted
    device_id) incremented the count without ever reaching the user.
    """
    from mind_meld.cli import PullResult

    _setup_real_config(tmp_path, monkeypatch)

    # Stub _pull_core to return a PullResult that simulates per-file failures.
    fake_result = PullResult(
        total_written=2,
        total_failed=3,
        device_names=["Mac B"],
        elapsed=0.1,
    )
    monkeypatch.setattr("mind_meld.cli._pull_core", lambda *a, **kw: fake_result)

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "3 file(s) failed" in (r.stderr or "")
    assert (
        "mm: 3 file(s) failed to apply - see the warnings above; they retry on the next pull"
        in r.stderr
    )


def test_recover_prior_manifest_surfaces_peer_fallback_in_quiet_mode(tmp_path, monkeypatch, capsys):
    """When the remote manifest is corrupt AND no local sidecar exists, recovery
    falls through to peer-fallback. That branch carries the riskiest semantics
    (recent local deletions can be lost) and must surface to stderr in quiet
    mode. Unit-tested at the helper boundary because reproducing the full
    multi-device, multi-tombstone push-side scenario via CliRunner is heavy.
    """
    from mind_meld.cli import ManifestFetch, _recover_prior_manifest

    fetch = ManifestFetch(status="corrupt", manifest=None)
    monkeypatch.setattr("mind_meld.sidecar.read", lambda *a, **kw: None)
    fake_tombstones = {
        "claude:projects/x/memory/foo.md": {"deleted_at": "2026-04-23T00:00:00+00:00"}
    }
    monkeypatch.setattr(
        "mind_meld.cli._collect_peer_tombstones",
        lambda *a, **kw: fake_tombstones,
    )

    result = _recover_prior_manifest(
        fetch,
        backend=None,
        device_id="dev-a",
        passphrase="x",
        memory_kb=1024,
        quiet=True,
    )

    captured = capsys.readouterr()
    # Returned the synthetic manifest with the peer tombstones intact.
    assert result is not None
    assert result["tombstones"] == fake_tombstones
    # Quiet-mode warning landed on stderr (load-bearing path).
    assert "corrupt" in captured.err
    assert "tombstone" in captured.err
    assert "may be lost" in captured.err


def test_autopush_breadcrumb_no_sources_distinguishes_from_success(tmp_path, monkeypatch):
    """REGRESSION (codex adversarial): empty-sources autopush used to write a
    'success' breadcrumb, masking the wedge from monitoring (mm status). The
    stderr warning helps a human; the breadcrumb downgrade catches automation.
    """
    storage_dir = tmp_path / "storage"
    config_path = tmp_path / "config.toml"
    save_config(
        {
            "device": {"id": "dev-empty", "name": "Empty"},
            "storage": {"path": str(storage_dir)},
            "sync": {"max_file_size": 52_428_800, "sources": []},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_path,
    )

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-empty", "Empty")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    _redirect_lock(monkeypatch, tmp_path)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    crumb = iso / "last-autorun.json"
    assert crumb.exists()
    assert _verb_crumb(json.loads(crumb.read_text()), "push")["outcome"] == "no-sources"


def _setup_events_tail_config(tmp_path, monkeypatch):
    """Config with BOTH a claude source (so the tail has projects to walk)
    and an mm-events source (so the tail doesn't no-op). Returns
    (sidecar_dir, claude_root).

    Two details the degradation pins depend on:

    * A real session jsonl with `message.usage` is written, not just the
      `memory/role.md` that `_populate_claude` makes. Without token data
      `warm_token_cache_inline` produces a cache under
      `_MIN_WARM_CACHE_BYTES` (64), so `is_cache_cold()` stays True and the
      "healthy" control pin can never reach `success`.
    * `upgrade.last_transition_seen` is forced to None. A version bump in
      the same process makes `_decide_token_walk_policy` take policy 3
      (warm silently, return True) even on a cold autopush, which masks the
      cold-cache branch entirely. Steady state is transition-free, so None
      is the honest default for these tests.
    """
    storage_dir = tmp_path / "storage"
    # Automatic root discovery intentionally looks under the user's home.
    # Keep these behavior tests independent of the developer's real corpus.
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setattr(_mm_events.Path, "home", classmethod(lambda cls: isolated_home))
    # Plant a discoverable git root so complete-zero is not the default.
    # `_probe_claude` reads Path.home()/.claude/projects, not the configured
    # source path, so the token-walk jsonl below is a different corpus.
    app_repo = tmp_path / "app-repo"
    app_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(app_repo)], check=True)
    claude_home = isolated_home / ".claude" / "projects" / "-app"
    claude_home.mkdir(parents=True)
    (claude_home / "session.jsonl").write_text(
        json.dumps({"cwd": str(app_repo), "type": "user"}) + "\n"
    )
    config_path = tmp_path / "config.toml"
    src = tmp_path / "claude"
    _populate_claude(src)
    sess = src / "projects" / "-Users-kb-myapp"
    sess.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (sess / "session.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": day,
                    "message": {
                        "id": f"msg_{i}",
                        "model": "claude-sonnet-4-5",
                        "role": "assistant",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    },
                }
            )
            for i in range(12)
        )
        + "\n"
    )
    monkeypatch.setattr(cli_module.upgrade, "last_transition_seen", lambda: None)
    save_config(
        {
            "device": {"id": "dev-deg", "name": "Deg"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "type": "claude", "path": str(src)},
                    {
                        "name": "mm-events",
                        "type": "generic",
                        "path": str(tmp_path / "mm-events"),
                        "include_dirs": ["events"],
                        "exclude_patterns": [],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_path,
    )
    (tmp_path / "mm-events" / "events").mkdir(parents=True)
    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-deg", "Deg")
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    _redirect_lock(monkeypatch, tmp_path)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    return iso, src


def test_autopush_breadcrumb_success_when_events_tail_is_healthy(tmp_path, monkeypatch):
    """CONTROL PIN for the `degraded` breadcrumb.

    Without this, `degraded` could become autopush's constant output and CI
    would stay green — conftest's `_isolate_token_cache` hands every test a
    fresh COLD cache, so the cold-cache degradation fires by default unless
    the test warms it. A signal that is always on is not a signal. This pin
    is what makes the sibling `degraded` tests meaningful.
    """
    from mind_meld import token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    assert token_usage.is_cache_cold() is False, "warm failed; the control pin proves nothing"

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "success", payload


def test_autopush_breadcrumb_degraded_when_walk_budget_exceeded(tmp_path, monkeypatch):
    """Second of the three degradation sites. Previously unpinned, so the
    whole budget-exceeded phrase could be deleted without a failure."""
    from mind_meld import token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    monkeypatch.setattr(_mm_events, "WALK_TIME_BUDGET_AUTOPUSH_MS", 0)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert "budget" in payload.get("detail", "")


def test_autopush_breadcrumb_degraded_when_root_discovery_is_partial(tmp_path, monkeypatch):
    """Partial root discovery is forensic, but must reach mm status safely."""
    from mind_meld import token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda _config, **_kwargs: _mm_events.GitRootDiscovery(
            (), (_mm_events.GIT_ROOT_DISCOVERY_BUDGET_ERROR,), True
        ),
    )

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert payload["detail"] == (
        "git repository discovery hit its time budget: this push captured an incomplete "
        "repository set. Run mm diag, then mm recapture 30d to recover the omitted commits"
    )
    assert str(tmp_path) not in payload["detail"]


def test_budget_phrase_unchanged_when_exceeded(tmp_path, monkeypatch):
    """The exceeded phrase must stay byte-identical to the constant."""
    from mind_meld import events_tail, token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda _config, **_kwargs: _mm_events.GitRootDiscovery(
            (), (_mm_events.GIT_ROOT_DISCOVERY_BUDGET_ERROR,), True
        ),
    )
    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["detail"] == events_tail._ROOT_DISCOVERY_DEGRADATION


def test_prober_exception_reaches_the_degraded_breadcrumb(tmp_path, monkeypatch):
    from mind_meld import events_tail, token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda _config, **_kwargs: _mm_events.GitRootDiscovery(
            (), ("claude prober: RuntimeError: boom",), False, (), (), (), ("claude",)
        ),
    )
    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert payload["detail"] == events_tail._ROOT_DISCOVERY_ERROR_DEGRADATION
    assert "time budget" not in payload["detail"]


def test_ordinary_rejected_candidate_does_not_degrade_the_push(tmp_path, monkeypatch):
    from mind_meld import token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    repo = tmp_path / "kept"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda _config, **_kwargs: _mm_events.GitRootDiscovery(
            (repo,),
            (),
            False,
            (("claude", repo),),
            (("gone", tmp_path / "dead"),),
            (("gone", 1),),
            ("claude",),
        ),
    )
    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "success", payload


def test_complete_zero_discovery_degrades_the_push(tmp_path, monkeypatch):
    from mind_meld import events_tail, token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda _config, **_kwargs: _mm_events.GitRootDiscovery(
            (), (), False, (), (), (), ("claude",)
        ),
    )
    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert payload["detail"] == events_tail._ROOT_DISCOVERY_EMPTY_DEGRADATION


def test_incomplete_discovery_still_triggers_tail_degradation(tmp_path, monkeypatch):
    """Regression: exceeded discovery must still degrade even with some roots."""
    from mind_meld import events_tail, token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    repo = tmp_path / "kept"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda _config, **_kwargs: _mm_events.GitRootDiscovery(
            (repo,), (_mm_events.GIT_ROOT_DISCOVERY_BUDGET_ERROR,), True
        ),
    )
    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert payload["detail"] == events_tail._ROOT_DISCOVERY_DEGRADATION


def test_autopush_breadcrumb_degraded_when_token_cache_cold(tmp_path, monkeypatch):
    """Third degradation site. The cache is cold by default under conftest
    isolation, so this is the no-warm counterpart of the control pin."""
    iso, _claude_root = _setup_events_tail_config(tmp_path, monkeypatch)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert "tokens and skills are missing" in payload.get("detail", "")


def test_autopush_breadcrumb_degraded_when_event_append_is_skipped(tmp_path, monkeypatch):
    """A day file that cannot take the activity batch loses rows, not the push.

    The size guard prints to a hook's stderr, which nobody reads, so the skip
    must also reach the breadcrumb (AGENTS.md: append, never merely print).
    """
    from mind_meld import token_usage
    from mind_meld.config import load_config

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    config_path = tmp_path / "config.toml"
    cfg = load_config(config_path)
    cfg["sync"]["max_file_size"] = 100  # smaller than any mm-push batch
    save_config(cfg, config_path)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert "event append skipped (max-file-size)" in payload["detail"]
    assert "Event append (max-file-size)" in r.stderr
    assert not list((tmp_path / "mm-events" / "events").glob("*.jsonl"))


def test_autopush_no_claude_source_is_not_a_degradation(tmp_path, monkeypatch):
    """`_decide_token_walk_policy` returns False for FOUR reasons, and one of
    them is `not claude_paths` — a config shape, not a failure.

    Reproduced during /review before the guard existed: a gstack-only or
    codex-only machine wrote `degraded` on EVERY autopush, blaming a token
    cache that was not the cause. That is the exact class of misleading
    signal this release exists to remove, so it must not be reintroduced by
    the fix for it.
    """
    storage_dir = tmp_path / "storage"
    config_path = tmp_path / "config.toml"
    gstack = tmp_path / "gstack"
    (gstack / "projects").mkdir(parents=True)
    (gstack / "projects" / "state.yaml").write_text("active: true")
    save_config(
        {
            "device": {"id": "dev-nc", "name": "NoClaude"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "gstack",
                        "type": "generic",
                        "path": str(gstack),
                        "include_dirs": ["projects"],
                        "exclude_patterns": [],
                    },
                    {
                        "name": "mm-events",
                        "type": "generic",
                        "path": str(tmp_path / "mm-events"),
                        "include_dirs": ["events"],
                        "exclude_patterns": [],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_path,
    )
    (tmp_path / "mm-events" / "events").mkdir(parents=True)
    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-nc", "NoClaude")
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    _redirect_lock(monkeypatch, tmp_path)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "success", (
        f"no claude source is a config shape, not a degradation: {payload}"
    )


def test_autopush_breadcrumb_degraded_when_token_cache_is_locked(tmp_path, monkeypatch):
    """Fourth degradation site: warn-mode flock contention.

    `lock_and_get_files("warn")` yields None when another process holds the
    token cache lock. `do_token_walk` stays True, so the cold-cache gate
    cannot see it — but every project ships without tokens_by_day and
    skills_by_day, and latest-snapshot-wins then replaces the prior complete
    data with it. Identical user-visible outcome to the cold-cache case.

    Shipped with zero coverage in the first review round: the ship coverage
    audit deleted the whole block and all 1795 tests still passed.
    """
    from contextlib import contextmanager

    from mind_meld import token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])

    @contextmanager
    def _contended(_mode):
        yield None  # what warn-mode contention actually hands back

    monkeypatch.setattr(_mm_token_usage, "lock_and_get_files", _contended)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert "locked" in payload.get("detail", "")
    assert "tokens and skills are missing" in payload.get("detail", "")


def test_autopush_breadcrumb_degraded_when_host_snapshot_is_withheld(tmp_path, monkeypatch):
    """Fifth degradation site (Track 19A): an incomplete host-usage scan.

    The unattended-hook user never sees the `mm: notice:` line, so the
    omission has to reach `mm status`. The detail must also make clear WHICH
    subsystem lost data — a bare `degraded` on a machine whose Grok store is
    (correctly) refused would read as content-sync failure, which is exactly
    the misleading-signal class v0.12.16 exists to remove.
    """
    from mind_meld import token_usage

    iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    token_usage.warm_token_cache_inline([claude_root])
    from mind_meld.config import CONFIG_PATH, load_config, save_config

    cfg = load_config(CONFIG_PATH)
    cfg["retro"] = {"grok_host_usage": True}
    save_config(cfg, CONFIG_PATH)
    monkeypatch.setattr(
        _mm_host_usage,
        "read_grok_usage",
        lambda **_kw: _mm_host_usage.HostUsageResult({}, complete=False, reason="unsupported"),
    )

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    assert payload["detail"] == (
        "host-usage snapshot skipped (grok unsupported) — "
        "content sync and git/session capture unaffected. "
        "grok wrote a record this version cannot read. A newer mm may read it: "
        "run `mm update`, or run `mm disable-source grok` to stop retrying."
    )
    # Safe by construction: no path, transcript, query, or exception text.
    assert str(tmp_path) not in payload["detail"]
    assert str(Path.home()) not in payload["detail"]


def test_autopush_breadcrumb_joins_multiple_degradations(tmp_path, monkeypatch):
    """`"; ".join(...)` must survive: two degradations co-occur regularly
    (a cold cache and a blown budget on the same slow push), and reporting
    only the first would hide the other. Replacing the join with `[0]` left
    all 1795 tests green before this pin.

    Also guards the separator contract: no individual degradation phrase may
    contain `; `, or the joined detail becomes ambiguous to split.
    """
    iso, _claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
    monkeypatch.setattr(_mm_events, "WALK_TIME_BUDGET_AUTOPUSH_MS", 0)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")
    assert payload["outcome"] == "degraded", payload
    detail = payload.get("detail", "")
    assert "budget" in detail
    assert "tokens and skills are missing" in detail
    assert detail.count("; ") == 1, f"expected exactly one separator, got {detail!r}"


class TestAutopushHeldBreadcrumb75A:
    """Track 75A: files held because they are still changing reach the breadcrumb.

    Holds are not a failure: autopush exits 0 and prints nothing, but `mm status`
    must be able to name what lags, so the outcome is `degraded` with the count
    and a few paths. Degradations are never dropped when both exist.
    """

    ROLE = "projects/-Users-kb-myapp/memory/role.md"

    def _unstable(self, monkeypatch, *names):
        real = cli_module.manifest.read_file_revision

        def flaky(path, **kw):
            if Path(path).name in names:
                raise cli_module.SnapshotChanged("changed while being read", "changed")
            return real(path, **kw)

        monkeypatch.setattr(cli_module.manifest, "read_file_revision", flaky)

    def _crumb(self, iso):
        return _verb_crumb(json.loads((iso / "last-autorun.json").read_text()), "push")

    def test_held_file_degrades_the_breadcrumb_with_count_and_path_and_stays_silent(
        self, tmp_path, monkeypatch
    ):
        from mind_meld import token_usage

        iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
        token_usage.warm_token_cache_inline([claude_root])
        first = runner.invoke(app, ["autopush"])
        assert first.exit_code == 0, (first.stdout, first.stderr)
        assert self._crumb(iso)["outcome"] == "success"  # control: held is what changes it
        (claude_root / self.ROLE).write_text("still being written")
        self._unstable(monkeypatch, "role.md")
        r = runner.invoke(app, ["autopush"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        assert "role.md" not in r.stdout + r.stderr
        assert "not yet synced" not in r.stdout + r.stderr
        payload = self._crumb(iso)
        assert payload["outcome"] == "degraded", payload
        assert payload["detail"] == (
            f"1 file held, not yet synced (still changing): claude:{self.ROLE}"
        )
        status = runner.invoke(app, ["status"])
        assert "1 file held, not yet synced" in " ".join(status.output.split())

    def test_held_count_joins_events_degradations_without_dropping_either(
        self, tmp_path, monkeypatch
    ):
        iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
        assert runner.invoke(app, ["autopush"]).exit_code == 0
        memory = claude_root / "projects/-Users-kb-myapp/memory"
        (memory / "role.md").write_text("still being written")
        (memory / "fresh.md").write_text("a real change so the events tail runs")
        monkeypatch.setattr(_mm_events, "WALK_TIME_BUDGET_AUTOPUSH_MS", 0)
        self._unstable(monkeypatch, "role.md")
        r = runner.invoke(app, ["autopush"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        payload = self._crumb(iso)
        assert payload["outcome"] == "degraded", payload
        detail = payload["detail"]
        assert "budget" in detail
        # Degradations are never dropped; the held fragment sheds its paths first
        # when they leave no room, but always keeps its count.
        assert "1 file held, not yet synced (still changing)" in detail
        assert detail.index("budget") < detail.index("held")

    def test_many_held_files_cap_paths_and_keep_the_detail_bounded(self, tmp_path, monkeypatch):
        iso, claude_root = _setup_events_tail_config(tmp_path, monkeypatch)
        assert runner.invoke(app, ["autopush"]).exit_code == 0
        memory = claude_root / "projects/-Users-kb-myapp/memory"
        names = {f"hot{i}.md" for i in range(6)}
        for name in names:
            (memory / name).write_text(name)
        self._unstable(monkeypatch, *names)
        r = runner.invoke(app, ["autopush"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        detail = self._crumb(iso)["detail"]
        assert "6 files held, not yet synced" in detail
        assert detail.count("claude:projects/") == 3
        assert detail.endswith("+3 more")
        assert len(detail) <= 300
        assert "\n" not in detail


def test_status_sanitizes_breadcrumb_detail(tmp_path, monkeypatch, capsys):
    """`mm status` renders the breadcrumb `detail` into a Rich console.

    The `failed` / `config-error` / `crypto-error` writers feed it raw
    `str(e)`, and those exceptions can carry peer-derived text (device
    names, source names, rel_paths from a peer manifest). Rich interprets
    markup, so an unsanitized field lets a peer string paint the terminal
    or smuggle escapes. v0.12.16 hardened `mm diag` first and missed this
    site, which is the one the degradation signal is actually designed to
    reach.
    """
    storage_dir = tmp_path / "storage"
    config_path = tmp_path / "config.toml"
    src = tmp_path / "claude"
    _populate_claude(src)
    save_config(
        {
            "device": {"id": "dev-s", "name": "S"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [{"name": "claude", "type": "claude", "path": str(src)}],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_path,
    )
    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-s", "S")
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    iso.mkdir(parents=True, exist_ok=True)
    (iso / "last-autorun.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-08-15T00:00:00Z",
                "verb": "push",
                "outcome": "failed",
                "detail": "boom \x1b[31mESCAPED\x1b[0m",
            }
        )
    )

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "Last auto-push" in r.output, "breadcrumb line did not render at all"
    # The raw escape must NOT survive to the terminal. Assert on the actual
    # rendered output, not on safe_str's return value -- asserting the helper
    # works proves nothing about whether the render site calls it.
    assert "\x1b[31m" not in r.output, "raw ANSI escape from the breadcrumb reached the terminal"
    assert "ESCAPED" in r.output, "sanitizer ate the message text, not just the escape"


def test_autopush_breadcrumb_degraded_when_events_tail_fails(tmp_path, monkeypatch):
    """v0.12.16: an events-tail failure must downgrade the autopush
    breadcrumb to 'degraded' instead of reporting 'success'.

    The events tail is forensic-only: it swallows every failure behind a
    `mm: notice:` line and lets the push proceed. That is correct for the
    push, and useless as a signal — autopush runs unattended from a Claude
    Code hook, so its stderr reaches nobody. Pre-fix, `mm status` reported
    `success` no matter how badly the retro pipeline had degraded, which is
    exactly the wedge the sibling `no-sources` test above exists to prevent.
    Same argument, applied to the other silent path.
    """
    storage_dir = tmp_path / "storage"
    config_path = tmp_path / "config.toml"
    src = tmp_path / "claude"
    # A `claude` source walks projects/<encoded>/memory — a top-level
    # memory/ finds nothing, `_push_core` returns None at the
    # nothing-to-push gate, and the tail never runs.
    _populate_claude(src)
    save_config(
        {
            "device": {"id": "dev-deg", "name": "Deg"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "type": "claude", "path": str(src)},
                    # The tail no-ops without this source, so the degradation
                    # path is unreachable if it's omitted.
                    {
                        "name": "mm-events",
                        "type": "generic",
                        "path": str(tmp_path / "mm-events"),
                        "include_dirs": ["events"],
                        "exclude_patterns": [],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_path,
    )
    (tmp_path / "mm-events" / "events").mkdir(parents=True)

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-deg", "Deg")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    _redirect_lock(monkeypatch, tmp_path)
    iso = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    # Blow up inside the tail. The tail must still swallow it (push
    # succeeds) but must now REPORT it through the breadcrumb.
    def _boom(*_a, **_kw):
        raise RuntimeError("synthetic tail failure")

    monkeypatch.setattr(_mm_events, "discover_git_roots", _boom)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    crumb = iso / "last-autorun.json"
    assert crumb.exists()
    payload = _verb_crumb(json.loads(crumb.read_text()), "push")
    assert payload["outcome"] == "degraded", (
        "events-tail failure still reported as success to mm status"
    )
    assert "events tail failed" in payload.get("detail", "")
    assert "RuntimeError" in payload.get("detail", "")


def test_autopush_surfaces_no_sources_warning_in_quiet_mode(tmp_path, monkeypatch):
    """A config with an empty sources list silently no-ops every autopush
    forever. After the audit, autopush surfaces a one-line stderr warning so
    the user can fix the config.
    """
    storage_dir = tmp_path / "storage"
    config_path = tmp_path / "config.toml"
    save_config(
        {
            "device": {"id": "dev-empty", "name": "Empty"},
            "storage": {"path": str(storage_dir)},
            "sync": {"max_file_size": 52_428_800, "sources": []},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_path,
    )

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-empty", "Empty")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "no sync sources" in (r.stderr or "")


def test_autopull_surfaces_fsync_failure_in_quiet_mode(tmp_path, monkeypatch):
    """A durability fsync failure during pull means recently-pulled files may
    not survive crash/power loss. Silently suppressing this in autopull leaves
    the user thinking pulls are durable when they aren't.
    """
    from mind_meld.errors import StorageError

    # Two devices, one push, then pull on the second so there's something
    # to fsync.
    storage_dir = tmp_path / "storage"
    claude_a = tmp_path / "machine_a" / ".claude"
    claude_b = tmp_path / "machine_b" / ".claude"
    _populate_claude(claude_a)
    claude_b.mkdir(parents=True)

    config_a, _ = _make_config(tmp_path / "a", storage_dir, claude_a, "dev-a", "Mac A")
    config_b, _ = _make_config(tmp_path / "b", storage_dir, claude_b, "dev-b", "Mac B")

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-a", "Mac A")
    register_device(backend, "dev-b", "Mac B")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    assert runner.invoke(app, ["push"]).exit_code == 0

    # Switch to device B and force fsync to fail during the upcoming autopull.
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)

    # Path-aware mock: only raise on the deferred-durability fsync (which
    # targets the claude tree where pulled files landed). _init_crypto_session
    # does a one-time config backfill that uses fsync_dir too — leaving that
    # alone keeps the test focused on the warning we want to assert.
    import mind_meld.fsutil as _fsu

    real_fsync_dir = _fsu.fsync_dir

    def selective_boom(path):
        if "claude" in str(path) or "memory" in str(path):
            raise StorageError("simulated fsync failure")
        return real_fsync_dir(path)

    monkeypatch.setattr("mind_meld.fsutil.fsync_dir", selective_boom)

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "fsync failed" in (r.stderr or "")


class TestBreadcrumbStaleness:
    """`mm status` must mark an autorun breadcrumb that stopped being written.

    `_write_autorun_breadcrumb` is called from INSIDE the command, so a failure
    before typer's runner -- a module-scope ImportError being the obvious one,
    and the exact risk Track 16A's decomposition introduces -- writes no
    breadcrumb at all. Without an age check, `mm status` then reports the last
    `success` forever while sync is wedged. This is the one degradation neither
    the v0.8.1 `no-sources` nor the v0.12.16 `degraded` breadcrumb can cover,
    because both are written by code that never ran.
    """

    @staticmethod
    def _iso(hours_ago: float) -> str:
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

    def test_fresh_breadcrumb_has_no_marker(self) -> None:
        from mind_meld.cli import _breadcrumb_staleness_suffix

        assert _breadcrumb_staleness_suffix(self._iso(1)) == ""

    def test_just_under_the_threshold_is_not_stale(self) -> None:
        from mind_meld.cli import _breadcrumb_staleness_suffix

        assert _breadcrumb_staleness_suffix(self._iso(47.5)) == ""

    def test_past_the_threshold_is_marked_stale(self) -> None:
        from mind_meld.cli import _breadcrumb_staleness_suffix

        out = _breadcrumb_staleness_suffix(self._iso(72))
        assert "stale" in out
        assert "72h" in out

    def test_trailing_z_timestamps_parse(self) -> None:
        """The breadcrumb writer emits `...Z`, which `fromisoformat` rejects
        on 3.11 unless the suffix is normalized first."""
        from datetime import datetime, timedelta, timezone

        from mind_meld.cli import _breadcrumb_staleness_suffix

        z = (datetime.now(timezone.utc) - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert "stale" in _breadcrumb_staleness_suffix(z)

    def test_naive_timestamp_is_treated_as_utc_not_crashed_on(self) -> None:
        from datetime import datetime, timedelta

        from mind_meld.cli import _breadcrumb_staleness_suffix

        naive = (datetime.utcnow() - timedelta(hours=100)).isoformat()
        assert "stale" in _breadcrumb_staleness_suffix(naive)

    @pytest.mark.parametrize("bad", ["", "not-a-date", None, 12345, {"ts": 1}])
    def test_unparseable_input_degrades_to_no_marker(self, bad) -> None:
        """Diagnostics must never raise into `mm status`."""
        from mind_meld.cli import _breadcrumb_staleness_suffix

        assert _breadcrumb_staleness_suffix(bad) == ""

    def test_clock_skew_from_the_future_is_not_stale(self) -> None:
        """A breadcrumb written by a peer with a fast clock yields a NEGATIVE
        age. It must read as fresh, not render `stale — no autorun in -3h`."""
        from mind_meld.cli import _breadcrumb_staleness_suffix

        assert _breadcrumb_staleness_suffix(self._iso(-3)) == ""

    def _plant(self, tmp_path, monkeypatch, hours_ago: float):
        _setup_real_config(tmp_path, monkeypatch)
        iso = _redirect_sidecar(monkeypatch, tmp_path)
        (iso / "last-autorun.json").write_text(
            json.dumps(
                {
                    "timestamp": self._iso(hours_ago),
                    "verb": "push",
                    "outcome": "success",
                }
            )
        )
        return iso

    def test_status_renders_the_marker_for_a_stale_breadcrumb(self, tmp_path, monkeypatch) -> None:
        """The six unit tests above all call the helper DIRECTLY.

        Dropping the `f"{_breadcrumb_staleness_suffix(ts)}"` interpolation from
        `status`'s console.print leaves every one of them green while the
        feature — a wedged autopush that `mm status` should flag — ships dead.
        This is the only assertion that the marker reaches a user.
        """
        self._plant(tmp_path, monkeypatch, 100)
        r = runner.invoke(app, ["status"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        assert "Last auto-push" in r.output, "breadcrumb line did not render at all"
        assert "stale" in r.output
        assert "success" in r.output, "the staleness marker replaced the outcome"

    def test_status_omits_the_marker_for_a_fresh_breadcrumb(self, tmp_path, monkeypatch) -> None:
        """Complement: the marker must not fire on a healthy device, or it is
        noise on every `mm status` the fleet runs."""
        self._plant(tmp_path, monkeypatch, 2)
        r = runner.invoke(app, ["status"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        assert "Last auto-push" in r.output
        assert "stale" not in r.output


class TestSkillLinkStatusNag:
    """`mm status`'s skill-link line must fire only on states mm can act on.

    REGRESSION: the line originally used a DENYLIST (`status not in ("ok",
    "absent")`), so `live-checkout` -- a deliberate dogfood link the installer
    explicitly preserves and reports `unchanged` for -- and `foreign` -- the
    user's own file, which mm must never touch -- were both reported broken on
    every `mm status`, forever, with a remedy that would have migrated the
    dogfood link away. Same shape as the Grok refusal that pinned the autopush
    breadcrumb at `degraded` and destroyed it as a signal for real degradation.
    """

    def test_working_and_deliberate_states_are_not_broken(self) -> None:
        from mind_meld import skill_link

        for status in ("ok", "absent", "live-checkout", "foreign", "declined"):
            assert status not in skill_link.BROKEN_SKILL_STATUSES, status

    def test_wedged_states_are_broken(self) -> None:
        from mind_meld import skill_link

        for status in ("dangling-ours", "dangling-ours-legacy", "error"):
            assert status in skill_link.BROKEN_SKILL_STATUSES, status

    def test_status_filter_is_an_allowlist_not_a_denylist(self) -> None:
        """A status invented by a later track must default to NOT-broken.

        A denylist made every future status broken-by-default; Groups 25-27 add
        rows and consent states, so the default has to be the safe one.
        """
        from mind_meld import skill_link

        assert "some-future-status-26a-invents" not in skill_link.BROKEN_SKILL_STATUSES

    def _row(self, status: str) -> list[dict[str, str]]:
        return [
            {
                "key": "claude",
                "agent": "Claude Code",
                "target": "~/.claude/skills/retro-fleet",
                "store": "~/.local/share/mind-meld/agent-skills/retro-fleet",
                "store_state": "ok",
                "status": status,
            }
        ]

    @pytest.mark.parametrize(
        "status", ["ok", "absent", "removed-by-user", "live-checkout", "foreign"]
    )
    def test_mm_status_stays_quiet_for_working_states(self, status, tmp_path, monkeypatch) -> None:
        """BEHAVIORAL pin on the cli.py call site, not on the constant.

        The three tests above pass even if cli.py is reverted to the denylist --
        they only read the tuple. This one invokes `mm status` and would fail.
        """
        from mind_meld import cli as _cli

        monkeypatch.setattr(
            _cli.skill_link, "diagnose_skill_links", lambda **_kwargs: self._row(status)
        )
        _setup_real_config(tmp_path, monkeypatch)
        result = runner.invoke(app, ["status"])
        assert "Skill links broken" not in result.output, (status, result.output)

    @pytest.mark.parametrize(
        "status", ["dangling-ours", "dangling-ours-legacy", "foreign-dangling", "error"]
    )
    def test_mm_status_nags_for_wedged_states(self, status, tmp_path, monkeypatch) -> None:
        from mind_meld import cli as _cli

        monkeypatch.setattr(
            _cli.skill_link, "diagnose_skill_links", lambda **_kwargs: self._row(status)
        )
        _setup_real_config(tmp_path, monkeypatch)
        result = runner.invoke(app, ["status"])
        assert "Skill links broken" in result.output, (status, result.output)
        flat = " ".join(result.output.split())
        assert "restart the agent so it reloads SKILL.md" in flat
        if status != "error":
            assert "mm install-skills" in flat


def test_cursor_drift_survives_capture_and_attempt_outcomes(cursor_store, tmp_path, monkeypatch):
    from mind_meld import attemptlog, events_tail

    iso, _ = _setup_events_tail_config(tmp_path, monkeypatch)
    path = sorted(cursor_store.glob("*/runs.ndjson"))[0]
    row = json.loads(path.read_text().splitlines()[0])
    row["status"] = "completed"  # renamed terminal is drift, not source absence
    path.write_text(json.dumps(row) + "\n")
    sources = [
        {
            "name": "mm-events",
            "type": "generic",
            "path": str(tmp_path / "mm-events"),
            "include_dirs": ["events"],
        }
    ]
    cfg = {
        "device": {"id": "dev-deg"},
        "sync": {"sources": sources},
        "retro": {"cursor_host_usage": True},
    }
    outcome = cli_module._capture_attended_usage(cfg, sources, sources, verbose=False)
    assert outcome.readers["cursor"] == "dropped:unsupported"
    assert "dropped:unsupported" in attemptlog.READER_OUTCOMES
    assert "dropped:no_metadata_ledger" not in attemptlog.READER_OUTCOMES
    phrase = events_tail._host_skip_phrase("cursor", "unsupported", readiness="ready")
    assert "[retro] cursor_host_usage = false" in phrase
    assert "disable-source cursor" in phrase
    degradations = events_tail._run_events_tail(cfg, sources, "dev-deg", dry_run=False, quiet=True)
    assert any("cursor unsupported" in reason for reason in degradations)


def test_status77_stale_crypto_replaced_by_real_attended_push(tmp_path, monkeypatch):
    from datetime import timedelta

    from mind_meld import attemptlog

    _setup_events_tail_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module.console, "_width", 240)
    cli_module._write_autorun_breadcrumb("push", "crypto-error", "old-crypto-diagnostic")
    path = cli_module._autorun_breadcrumb_path()
    raw = json.loads(path.read_text())
    raw["push"]["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    path.write_text(json.dumps(raw))
    pushed = runner.invoke(app, ["push"])
    assert pushed.exit_code == 0, pushed.output
    assert attemptlog.read_runs()["runs"]["push"]["outcome"] in ("success", "degraded")
    shown = runner.invoke(app, ["status"])
    assert shown.exit_code == 0, shown.output
    assert "old-crypto-diagnostic" not in shown.output
    assert "Last auto-push:" not in shown.output
    assert "Last attended-push:" in shown.output
    diag = runner.invoke(app, ["diag", "--json"])
    payload = json.loads(diag.stdout)
    assert payload["last_autorun"]["push"]["detail"] == "old-crypto-diagnostic"
    assert payload["sync_run_projection"]["push"]["superseded"] == ["auto"]


@pytest.mark.parametrize("verb", ["push", "pull"])
@pytest.mark.parametrize(
    "stage,cause",
    [
        ("setup", "setup-error"),
        ("authentication", "authentication-error"),
        ("lock", "lock-held"),
        ("snapshot", "snapshot-refused"),
        ("storage", "storage-error"),
        ("crypto", "crypto-error"),
        ("exit", "command-refused"),
        ("interrupt", "interrupted"),
        ("abort", "interrupted"),
        ("unexpected", "unexpected-error"),
    ],
)
def test_attended77_callback_failure_matrix(tmp_path, monkeypatch, verb, stage, cause):
    import errno

    import typer

    from mind_meld import attemptlog
    from mind_meld.errors import CryptoError, LockError, SnapshotError

    _setup_events_tail_config(tmp_path, monkeypatch)
    released = []
    real_release = cli_module.release_lock
    monkeypatch.setattr(
        cli_module, "release_lock", lambda: (released.append(True), real_release())[1]
    )
    failures = {
        "setup": typer.Exit(1),
        "authentication": typer.Exit(1),
        "lock": LockError("held"),
        "snapshot": SnapshotError("refused"),
        "storage": OSError(errno.ENOSPC, "/secret"),
        "crypto": CryptoError("bad key"),
        "exit": typer.Exit(3),
        "interrupt": KeyboardInterrupt(),
        "abort": typer.Abort(),
        "unexpected": RuntimeError("unexpected"),
    }
    target = {
        "setup": "_get_config",
        "authentication": "_get_passphrase_or_exit",
        "lock": "acquire_lock",
    }.get(stage, f"_{verb}_core")

    def fail(*args, **kwargs):
        raise failures[stage]

    monkeypatch.setattr(cli_module, target, fail)
    result = runner.invoke(app, [verb])
    assert result.exit_code == {"exit": 3, "interrupt": 130}.get(stage, 1)
    recorded = attemptlog.read_runs()["runs"][verb]
    assert recorded["cause"] == cause
    assert recorded["outcome"] == ("skipped" if stage == "lock" else "failed")
    assert recorded["counts"] == {}
    assert recorded["content_accepted"] is not True
    assert bool(released) == (stage not in ("setup", "authentication", "lock"))
    if stage == "storage":
        assert recorded["errno"] == "ENOSPC"
    assert "/secret" not in json.dumps(recorded)


@pytest.mark.parametrize("verb", ["push", "pull"])
def test_attended77_lock_skip_preserves_real_holder(tmp_path, monkeypatch, verb):
    from mind_meld import attemptlog, lockfile
    from mind_meld.errors import LockError

    _setup_events_tail_config(tmp_path, monkeypatch)
    lockfile.acquire_lock()
    owned = dict(lockfile._LOCK_FDS)
    try:
        shown = runner.invoke(app, [verb])
        assert shown.exit_code == 1, shown.output
        assert lockfile._LOCK_FDS == owned
        with pytest.raises(LockError):
            lockfile.acquire_lock()
        recorded = attemptlog.read_runs()["runs"][verb]
        assert (recorded["outcome"], recorded["cause"]) == ("skipped", "lock-held")
    finally:
        lockfile.release_lock()
    lockfile.acquire_lock()
    lockfile.release_lock()


def test_attended77_auto_maintenance_failure_cannot_prove_recovery(tmp_path, monkeypatch):
    from mind_meld import attemptlog

    _setup_events_tail_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "_push_core",
        lambda *a, **k: cli_module.PushResult(content_accepted=True, maintenance_failures=1),
    )
    assert runner.invoke(app, ["push"]).exit_code == 0
    attended = attemptlog.read_runs()
    assert attended["runs"]["push"]["outcome"] == "degraded"
    assert runner.invoke(app, ["autopush"]).exit_code == 0
    auto = cli_module._read_autorun_evidence()
    assert auto["runs"]["push"]["outcome"] == "degraded"
    assert attemptlog.project_runs(auto, attended)["push"]["unresolved"] is True


@pytest.mark.parametrize("verb", ["push", "pull"])
@pytest.mark.parametrize("write_failure", ["disk", "contention", "torn"])
def test_attended77_evidence_failure_preserves_result_and_unlocks(
    tmp_path, monkeypatch, verb, write_failure
):
    import errno

    from mind_meld import attemptlog, lockedjson

    _setup_events_tail_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module,
        f"_{verb}_core",
        lambda *a, **k: (
            cli_module.PushResult(content_accepted=True)
            if verb == "push"
            else cli_module.PullResult(total_written=2)
        ),
    )

    def fail(*args, **kwargs):
        if write_failure == "contention":
            raise lockedjson.LockContended("private")
        raise OSError(errno.ENOSPC, "secret")

    if write_failure == "torn":
        monkeypatch.setattr(
            lockedjson, "_write_json", lambda *a, **k: OSError(errno.ENOSPC, "torn")
        )
    else:
        monkeypatch.setattr(attemptlog, "write_run", fail)
    result = runner.invoke(app, [verb])
    assert result.exit_code == 0, result.output
    assert "run-evidence-write-unavailable" in result.stderr
    assert "Do not repeat sync solely to record it" in result.stderr
    assert "secret" not in result.stderr
    assert ("content accepted" if verb == "push" else "content partial") in result.stderr
    cli_module.acquire_lock()
    cli_module.release_lock()


@pytest.mark.parametrize(
    "verb,result,counts,outcome",
    [
        ("push", cli_module.PushResult(), {"held": 0}, "success"),
        (
            "push",
            cli_module.PushResult(held=[("claude", "secret.md", "changed")]),
            {"held": 1},
            "degraded",
        ),
        (
            "push",
            cli_module.PushResult(events_degradations=["event append skipped (max-file-size)"]),
            {"events_degradations": 1},
            "degraded",
        ),
        (
            "pull",
            cli_module.PullResult(total_written=2, total_failed=1),
            {"total_failed": 1, "total_written": 2},
            "degraded",
        ),
        (
            "pull",
            cli_module.PullResult(
                corrupt_peer_count=2, durability_fsync_failures=3, total_skipped_unknown_source=4
            ),
            {
                "corrupt_peer_count": 2,
                "durability_fsync_failures": 3,
                "total_skipped_unknown_source": 4,
            },
            "degraded",
        ),
        ("pull", cli_module.PullResult(), {"total_written": 0}, "success"),
    ],
)
def test_attended77_result_counts(tmp_path, monkeypatch, verb, result, counts, outcome):
    from mind_meld import attemptlog

    _setup_events_tail_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, f"_{verb}_core", lambda *a, **k: result)
    finished = runner.invoke(app, [verb])
    assert finished.exit_code == 0, finished.output
    record = attemptlog.read_runs()["runs"][verb]
    assert record["outcome"] == outcome
    for key, value in counts.items():
        assert record["counts"][key] == value
    assert "secret.md" not in json.dumps(record)


def test_status77_pure_precedence_and_hostile_scope():
    # Value: protects=capture action priority below blockers and above pending;
    # fails_when=capture hides a blocker or pending hides actionable capture;
    # why_new=existing precedence checks omit capture_action; seam=none;
    # no_claim=real host reader or fleet acceptance.
    from mind_meld import attemptlog

    empty = attemptlog.project_runs(
        {"state": "missing", "runs": {}}, {"state": "missing", "runs": {}}
    )
    base = dict(
        source=None,
        blocking=None,
        conflicts=0,
        projection=empty,
        counts=(2, 3, 4),
        metadata=True,
        first_push=False,
    )
    assert (
        cli_module._status_verdict(**base)
        == "2 new, 3 modified and 4 deleted waiting to push. Run: mm push"
    )
    assert cli_module._status_verdict(**{**base, "conflicts": 2}).startswith(
        "2 unresolved conflicts"
    )
    assert cli_module._status_verdict(
        **{**base, "blocking": "storage failed. Run: mm diag", "conflicts": 2}
    ).startswith("storage failed")
    failure = attemptlog.make_run(outcome="failed", cause="storage-error")
    state = attemptlog.project_runs(
        {"state": "missing", "runs": {}}, {"state": "valid", "runs": {"pull": failure}}
    )
    assert "Latest pull needs attention" in cli_module._status_verdict(
        **{**base, "projection": state}
    )
    capture = {**base, "capture_action": "Usage capture needs attention. Run: mm diag"}
    assert cli_module._status_verdict(**capture) == capture["capture_action"]
    assert (
        cli_module._status_verdict(**{**capture, "first_push": True}) == capture["capture_action"]
    )
    assert cli_module._status_verdict(
        **{**capture, "blocking": "storage failed. Run: mm diag"}
    ).startswith("storage failed")
    assert cli_module._status_verdict(**{**capture, "conflicts": 2}).startswith(
        "2 unresolved conflicts"
    )
    assert "Latest pull needs attention" in cli_module._status_verdict(
        **{**capture, "projection": state}
    )
    assert "Metadata-only" in cli_module._status_verdict(**{**base, "counts": (0, 0, 0)})
    assert "has not pushed yet" in cli_module._status_verdict(**{**base, "first_push": True})
    clean = {**base, "counts": (0, 0, 0), "metadata": False}
    assert cli_module._status_verdict(**clean) == "In sync with this device's last push."
    text = cli_module._status_verdict(**{**clean, "source": "[red]evil\n\x1b[2Jname"})
    assert text.startswith("Source '[red]evil\\n") and "\n" not in text and "\x1b" not in text


@pytest.mark.parametrize("latest,earlier", [("lock-held", "failed"), ("success", "lock-held")])
def test_status77_skip_and_opposite_verb_regressions(tmp_path, monkeypatch, latest, earlier):
    from datetime import timedelta

    from mind_meld import attemptlog

    _setup_events_tail_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module.console, "_width", 240)
    assert runner.invoke(app, ["push"]).exit_code == 0
    now = datetime.now(timezone.utc)
    path = cli_module._autorun_breadcrumb_path()
    path.write_text(
        json.dumps(
            {
                "pull": {"timestamp": (now - timedelta(seconds=2)).isoformat(), "outcome": earlier},
                "push": {
                    "timestamp": (now - timedelta(seconds=3)).isoformat(),
                    "outcome": "crypto-error",
                    "detail": "push-older-forensic",
                },
            }
        )
    )
    attemptlog.write_run(
        "pull",
        attemptlog.make_run(
            outcome="skipped" if latest == "lock-held" else "success",
            cause=latest if latest == "lock-held" else "ok",
            completed_at=(now - timedelta(seconds=1)).isoformat(),
        ),
    )
    shown = runner.invoke(app, ["status"])
    assert shown.exit_code == 0, shown.output
    assert "Last attended-pull:" in shown.output
    assert "Last auto-pull:" not in shown.output
    if latest == "lock-held":
        assert "Latest pull needs attention" in shown.output.splitlines()[0]
        assert "recovery is unproven" in shown.output.splitlines()[0]
    assert "push-older-forensic" not in shown.output  # newer push is independent
    diag = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
    assert diag["last_autorun"]["push"]["detail"] == "push-older-forensic"


def test_status77_clean_attended_then_routine_auto_skip_can_be_healthy(tmp_path, monkeypatch):
    from mind_meld import attemptlog

    _setup_events_tail_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        _mm_events, "discover_git_roots", lambda *a, **k: _mm_events.GitRootDiscovery((), ())
    )
    assert runner.invoke(app, ["push"]).exit_code == 0
    written = attemptlog.read_runs()["runs"]["push"]
    assert written["outcome"] == "success"
    cli_module._write_autorun_breadcrumb("push", "lock-held")
    # Auto's later skip does no work and does not invent a prior failure.
    shown = runner.invoke(app, ["status"])
    assert shown.exit_code == 0, shown.output
    assert shown.output.splitlines()[0] == "In sync with this device's last push."
    assert "Last auto-push:" in shown.output and "Last attended-push:" not in shown.output


def test_attended77_accepted_content_survives_later_gc_failure(tmp_path, monkeypatch):
    import typer

    from mind_meld import attemptlog

    _setup_events_tail_config(tmp_path, monkeypatch)

    def published(*args, **kwargs):
        kwargs["attempt"].content_accepted = True
        return cli_module.PushResult(content_accepted=True, content_changed=True)

    monkeypatch.setattr(cli_module, "_push_core", published)

    def stopped(*a, **k):
        raise typer.Exit(1)

    monkeypatch.setattr(cli_module, "_do_gc", stopped)
    shown = runner.invoke(app, ["push"])
    assert shown.exit_code == 1
    record = attemptlog.read_runs()["runs"]["push"]
    assert record["content_accepted"] is True
    assert record["outcome"] == "failed" and record["cause"] == "command-refused"
    assert record["counts"]["maintenance_failures"] == 1
    cli_module.acquire_lock()
    cli_module.release_lock()
