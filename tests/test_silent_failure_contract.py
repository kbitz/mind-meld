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
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import cli as cli_module
from mind_meld import events as _mm_events
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


# ─── Config-surface regressions ──────────────────────────────────────────


def test_autopull_silent_exit_when_config_missing(tmp_path, monkeypatch):
    """REGRESSION: only FileNotFoundError-equivalent silences autopull."""
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "nope.toml")
    result = runner.invoke(app, ["autopull"])
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
    assert not (iso / "autopull.log").exists()


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


def test_autopull_writes_breadcrumb_on_success(tmp_path, monkeypatch):
    """Success path writes `{verb: 'pull', outcome: 'success'}`."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    crumb = iso / "last-autorun.json"
    assert crumb.exists()
    data = json.loads(crumb.read_text())
    assert data["verb"] == "pull"
    assert data["outcome"] == "success"
    assert "timestamp" in data


def test_autopull_writes_breadcrumb_on_lock_held(tmp_path, monkeypatch):
    """LockError still silent-exits but now leaves a 'lock-held' breadcrumb."""
    _setup_real_config(tmp_path, monkeypatch)
    iso = _redirect_sidecar(monkeypatch, tmp_path)

    def boom(*a, **kw):
        from mind_meld.errors import LockError

        raise LockError("already held by PID 12345")

    monkeypatch.setattr("mind_meld.cli.acquire_lock", boom)

    r = runner.invoke(app, ["autopull"])
    assert r.exit_code == 0
    assert r.stdout == ""  # silent to Claude
    crumb = iso / "last-autorun.json"
    assert crumb.exists()
    assert json.loads(crumb.read_text())["outcome"] == "lock-held"


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
    assert "mm pull --verbose" in (r.stderr or "")


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
    assert json.loads(crumb.read_text())["outcome"] == "no-sources"


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
    payload = json.loads((iso / "last-autorun.json").read_text())
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
    payload = json.loads((iso / "last-autorun.json").read_text())
    assert payload["outcome"] == "degraded", payload
    assert "budget" in payload.get("detail", "")


def test_autopush_breadcrumb_degraded_when_token_cache_cold(tmp_path, monkeypatch):
    """Third degradation site. The cache is cold by default under conftest
    isolation, so this is the no-warm counterpart of the control pin."""
    iso, _claude_root = _setup_events_tail_config(tmp_path, monkeypatch)

    r = runner.invoke(app, ["autopush"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    payload = json.loads((iso / "last-autorun.json").read_text())
    assert payload["outcome"] == "degraded", payload
    assert "tokens and skills are missing" in payload.get("detail", "")


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
    payload = json.loads((iso / "last-autorun.json").read_text())
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
    payload = json.loads((iso / "last-autorun.json").read_text())
    assert payload["outcome"] == "degraded", payload
    assert "locked" in payload.get("detail", "")


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
    payload = json.loads((iso / "last-autorun.json").read_text())
    assert payload["outcome"] == "degraded", payload
    detail = payload.get("detail", "")
    assert "budget" in detail
    assert "tokens and skills are missing" in detail
    assert detail.count("; ") == 1, f"expected exactly one separator, got {detail!r}"


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
    payload = json.loads(crumb.read_text())
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
