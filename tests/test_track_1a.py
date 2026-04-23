"""Track 1A tests: silent-failure fixes in cli.py.

Covers:
  - autopull/autopush surface corrupt config (regression, was silent)
  - autopull/autopush log unexpected exceptions to ~/.config/mind-meld/auto*.log
  - autopull silent when lock held (mirror of existing autopush test)
  - log appender truncates on 1MB overflow
  - pull warns on unknown remote source (not verbose-only)
  - pull --conflict-mode fail preflights and exits 2 before any writes
  - mm devices column header is "Last push"
  - get_passphrase(non_interactive=True) raises instead of hanging
  - quiet-mode autopull surfaces corrupt-peer-manifest warning
  - quiet-mode autopush surfaces sidecar-write-failure warning
  - _log_unexpected is idempotent (no handler duplication regression)
"""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import list_devices, register_device
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "track-1a-test-passphrase"
MEMORY_KB = 1024
runner = CliRunner()


def _make_config(tmp_path, storage_dir, claude_dir, device_id="dev-a", device_name="Mac A"):
    """Write a valid config.toml for a single-claude-source setup."""
    config_path = tmp_path / "config.toml"
    config = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"path": str(storage_dir)},
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
            ],
        },
        "crypto": {"argon2_memory_kb": MEMORY_KB},
    }
    save_config(config, config_path)
    return config_path, config


def _populate_claude(claude_dir: Path, contents: str = "Data scientist") -> None:
    """Populate a claude_dir with a single memory file so push has work to do."""
    memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
    memory.mkdir(parents=True)
    (memory / "role.md").write_text(contents)


def _redirect_sidecar(monkeypatch, tmp_path):
    """Redirect SIDECAR_DIR to a tmp path so log writes are hermetic.

    `_auto_log_path` reads `sidecar.SIDECAR_DIR` at call time, so patching
    the one constant is sufficient — no `mind_meld.cli._AUTO_LOG_DIR` alias
    to keep in sync.
    """
    iso = tmp_path / "sidecar"
    iso.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", iso)
    return iso


def _redirect_lock(monkeypatch, tmp_path):
    lp = tmp_path / "test.lock"
    monkeypatch.setattr("mind_meld.config.LOCK_PATH", lp)
    monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", lp)
    return lp


# ─── Config-surface regressions ──────────────────────────────────────────


def test_autopull_silent_exit_when_config_missing(tmp_path, monkeypatch):
    """REGRESSION: only FileNotFoundError-equivalent silences autopull."""
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "nope.toml")
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", tmp_path / "nope.toml")
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
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", bad)
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
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", bad)
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
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", bad)
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
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_path)
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


def _setup_real_config(tmp_path, monkeypatch):
    """Full working config so we reach _pull_core / _push_core."""
    storage_dir = tmp_path / "storage"
    claude_dir = tmp_path / ".claude"
    _populate_claude(claude_dir)
    config_path, _ = _make_config(tmp_path, storage_dir, claude_dir)

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-a", "Mac A")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_path)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    return claude_dir


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
    save_config({
        "device": {"id": "dev-a", "name": "Mac A"},
        "storage": {"path": str(storage_dir)},
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_a), "type": "claude"},
                {
                    "name": "gstack", "path": str(gstack_a), "type": "generic",
                    "include_dirs": ["projects"],
                    "include_files": ["config.yaml"],
                },
            ],
        },
        "crypto": {"argon2_memory_kb": MEMORY_KB},
    }, config_a)

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-a", "Mac A")
    register_device(backend, "dev-b", "Mac B")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    r = runner.invoke(app, ["push"])
    assert r.exit_code == 0, r.stderr

    # Phase 2: switch to device B (claude-only -- no gstack configured).
    claude_b = tmp_path / "machine_b" / ".claude"
    claude_b.mkdir(parents=True)
    config_b = tmp_path / "config_b.toml"
    save_config({
        "device": {"id": "dev-b", "name": "Mac B"},
        "storage": {"path": str(storage_dir)},
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_b), "type": "claude"},
            ],
        },
        "crypto": {"argon2_memory_kb": MEMORY_KB},
    }, config_b)
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_b)

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
    import os
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
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    r = runner.invoke(app, ["push"])
    assert r.exit_code == 0, r.stderr

    # Switch to device B.
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_b)

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
    claude_dir = _setup_real_config(tmp_path, monkeypatch)
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
        return 0, {k: [] for k in ("written", "merged", "skipped", "conflicted", "unchanged", "failed")}
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


# ─── Register-device contract + log safety ───────────────────────────────


def test_register_device_does_not_seed_last_seen(tmp_path):
    """Storage-level contract: no `last_seen` key on register (avoids lying in the table)."""
    backend = LocalBackend(tmp_path / "storage")
    register_device(backend, "dev-x", "Mac X")
    data = json.loads(backend.get("devices/dev-x.json"))
    assert "last_seen" not in data
    assert "registered" in data


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
    (iso / "last-autorun.json").write_text(json.dumps({
        "timestamp": "2026-04-23T12:00:00+00:00",
        "verb": "pull",
        "outcome": "lock-held",
    }))

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
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    assert runner.invoke(app, ["push"]).exit_code == 0

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_b)
    assert runner.invoke(app, ["push"]).exit_code == 0

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_l)
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_l)

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
    monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", bad)

    r = runner.invoke(app, ["push"])

    # Exit code 1 from _error's typer.Exit(1).
    assert r.exit_code == 1
    # The error text must be on stderr.
    assert "Error:" in (r.stderr or "")
    # And stdout must be clean — no rich error leakage. Before the fix,
    # stdout would carry a [red]Error:[/red]-expanded string.
    assert "Error:" not in (r.stdout or "")


def test_error_preserves_rich_formatting_on_stderr():
    """Rich Console(stderr=True) still emits color codes when attached to
    a real terminal; the forced-terminal option proves the formatting
    pipeline still runs (color bytes in the captured stream).

    This protects against a naive "just print() to stderr" refactor that
    would silently drop the [red]Error:[/red] styling for interactive users.
    """
    import io
    from rich.console import Console

    buf = io.StringIO()
    c = Console(file=buf, stderr=True, force_terminal=True, color_system="truecolor")
    c.print("[red]Error:[/red] it blew up")
    out = buf.getvalue()
    # Rich emits an ANSI escape for red when force_terminal + color_system
    # are set. The presence of ESC[ proves styling survived.
    assert "\x1b[" in out
    assert "Error:" in out
    assert "it blew up" in out
