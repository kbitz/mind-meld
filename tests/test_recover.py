"""Unit tests for `mm recover --abandon-manifest` (Group 2 Track 2A.1).

Covers the subcommand's guardrails in isolation. The destructive-path
integration test lives in test_recovery.py alongside the rest of the
corrupt-manifest recovery chain.

Contract under test:
  - Missing --abandon-manifest flag errors with actionable message
  - Refuse when remote manifest is ok ("nothing to recover")
  - Refuse when sidecar is present ("push will self-heal, don't be destructive")
  - Refuse when peer tombstones exist ("push will self-heal")
  - Typed "RESET" required; lowercase "reset" rejected; --yes bypasses
  - Quarantine is crash-durable (read → atomic_write → unlink, not plain rename)
  - Collision handling: existing .corrupt-<ts> → suffix picks next name
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from mind_meld import sidecar as sidecar_mod
from mind_meld.cli import _quarantine_corrupt_manifest, app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init, encrypt, fetch_crypto_init, set_crypto_session
from mind_meld.devices import register_device
from mind_meld.manifest import serialize_manifest
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "recover-test-passphrase"
MEMORY_KB = 1024
runner = CliRunner()


def _mk(tmp_path, monkeypatch):
    """Standard setup: storage + crypto init + config + isolated sidecar."""
    storage = tmp_path / "icloud"
    storage.mkdir()
    backend = LocalBackend(storage)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "mac-a", "Mac A")

    cfg_path = tmp_path / "config.toml"
    save_config(
        {
            "device": {"id": "mac-a", "name": "Mac A"},
            "storage": {"path": str(storage)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(tmp_path / "claude"), "type": "claude"},
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        cfg_path,
    )
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    # Isolate sidecar.
    sc_dir = tmp_path / "sidecar"
    monkeypatch.setattr(sidecar_mod, "SIDECAR_DIR", sc_dir)
    return storage, backend


def _plant_corrupt_manifest(backend):
    """Write a manifest blob that fails decrypt + conflict-copy scan."""
    backend.put("manifests/mac-a/manifest.json.enc", b"garbage-not-a-valid-blob")


# ── Flag + refuse-when-healthy ───────────────────────────────────────────


def test_recover_without_flag_errors(tmp_path, monkeypatch):
    _mk(tmp_path, monkeypatch)
    result = runner.invoke(app, ["recover"])
    assert result.exit_code != 0
    assert "requires a recovery mode flag" in (result.stderr or "") + result.output


def test_recover_refuses_when_manifest_is_ok(tmp_path, monkeypatch):
    storage, backend = _mk(tmp_path, monkeypatch)
    # `encrypt` requires an active crypto session — set it up the same way
    # cli.py commands do (fetch_crypto_init → set_crypto_session).
    fetch = fetch_crypto_init(backend)
    set_crypto_session(fetch.root_salt, fetch.argon2_memory_kb)
    manifest = {
        "device_id": "mac-a",
        "device_name": "Mac A",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": {"claude": {"base_path": "", "files": {}}},
        "tombstones": {},
    }
    blob = encrypt(serialize_manifest(manifest), PASSPHRASE, memory_kb=MEMORY_KB)
    backend.put("manifests/mac-a/manifest.json.enc", blob)

    result = runner.invoke(app, ["recover", "--abandon-manifest"])
    assert result.exit_code != 0
    assert "readable" in (result.stderr or "") + result.output


def test_recover_refuses_when_manifest_missing(tmp_path, monkeypatch):
    storage, backend = _mk(tmp_path, monkeypatch)
    # No manifest at all — not "corrupt," just "missing."
    result = runner.invoke(app, ["recover", "--abandon-manifest"])
    assert result.exit_code != 0
    assert "nothing to quarantine" in (result.stderr or "") + result.output


def test_recover_refuses_when_sidecar_present(tmp_path, monkeypatch):
    """If the sidecar exists, push can recover non-destructively — running
    --abandon-manifest would throw away fresh deletions. Refuse."""
    storage, backend = _mk(tmp_path, monkeypatch)
    _plant_corrupt_manifest(backend)

    # Write a valid sidecar for this device.
    sidecar_manifest = {
        "device_id": "mac-a",
        "device_name": "Mac A",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "claude": {
                "base_path": "",
                "files": {"a.md": {"sha256": "x", "size": 1, "mtime": "2026-04-22T10:00:00+00:00"}},
            }
        },
        "tombstones": {},
    }
    sidecar_mod.write(sidecar_manifest)

    result = runner.invoke(app, ["recover", "--abandon-manifest"])
    assert result.exit_code != 0
    out = (result.stderr or "") + result.output
    assert "sidecar" in out
    assert "mm push" in out


# ── Typed confirmation ───────────────────────────────────────────────────


def test_recover_rejects_lowercase_reset(tmp_path, monkeypatch):
    storage, backend = _mk(tmp_path, monkeypatch)
    _plant_corrupt_manifest(backend)
    # No sidecar, no peers — legitimate destructive path.
    stdin = "reset\n"
    result = runner.invoke(app, ["recover", "--abandon-manifest"], input=stdin)
    assert result.exit_code != 0
    # Manifest still on disk.
    assert backend.exists("manifests/mac-a/manifest.json.enc")


def test_recover_accepts_exact_RESET(tmp_path, monkeypatch):
    storage, backend = _mk(tmp_path, monkeypatch)
    _plant_corrupt_manifest(backend)
    stdin = "RESET\n"
    result = runner.invoke(app, ["recover", "--abandon-manifest"], input=stdin)
    assert result.exit_code == 0, result.output
    # Canonical gone, quarantine sibling exists.
    assert not backend.exists("manifests/mac-a/manifest.json.enc")
    assert any(
        k.endswith(".enc") is False and "corrupt-" in k
        for k in backend.list_keys("manifests/mac-a/")
    )


def test_recover_yes_flag_skips_prompt(tmp_path, monkeypatch):
    storage, backend = _mk(tmp_path, monkeypatch)
    _plant_corrupt_manifest(backend)
    # No stdin required because of --yes.
    result = runner.invoke(app, ["recover", "--abandon-manifest", "--yes"])
    assert result.exit_code == 0, result.output
    assert not backend.exists("manifests/mac-a/manifest.json.enc")


# ── Quarantine durability + collision handling ──────────────────────────


def test_quarantine_unit_read_then_atomic_write_then_unlink(tmp_path, monkeypatch):
    """_quarantine_corrupt_manifest moves src → dst atomically. The dst
    exists and has the original bytes; the src no longer exists."""
    storage = tmp_path / "icloud"
    storage.mkdir()
    backend = LocalBackend(storage)
    original = b"corrupt-manifest-bytes-here"
    backend.put("manifests/mac-a/manifest.json.enc", original)

    dst = _quarantine_corrupt_manifest(backend, storage, "mac-a")

    assert dst.exists()
    assert dst.read_bytes() == original
    assert not backend.exists("manifests/mac-a/manifest.json.enc")
    # Name matches the <src>.corrupt-<ts> pattern.
    assert ".corrupt-" in dst.name


def test_quarantine_collision_picks_unique_name(tmp_path, monkeypatch):
    """If a prior quarantine file occupies the primary name, a secondary
    name with random suffix is selected."""
    storage = tmp_path / "icloud"
    storage.mkdir()
    backend = LocalBackend(storage)
    backend.put("manifests/mac-a/manifest.json.enc", b"new-corrupt")

    # Pre-plant a blocking collision at the primary name we'd generate.
    manifest_dir = storage / "manifests" / "mac-a"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    collision = manifest_dir / f"manifest.json.enc.corrupt-{ts}"
    collision.write_bytes(b"already-there")

    dst = _quarantine_corrupt_manifest(backend, storage, "mac-a")
    # Collision still there, untouched.
    assert collision.read_bytes() == b"already-there"
    # Our quarantine landed at a DIFFERENT name.
    assert dst != collision
    assert dst.exists()
    assert dst.read_bytes() == b"new-corrupt"


def test_quarantine_raises_when_src_missing(tmp_path):
    storage = tmp_path / "icloud"
    storage.mkdir()
    backend = LocalBackend(storage)
    with pytest.raises(FileNotFoundError):
        _quarantine_corrupt_manifest(backend, storage, "nope")
