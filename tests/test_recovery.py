"""Tests for corrupt-manifest recovery during push.

Covers:
- tri-state _fetch_remote_manifest (ok / missing / corrupt) — additional coverage
  beyond test_additive_sync.py::TestConflictManifestMerge
- _recover_prior_manifest chain: sidecar → peer → refuse
- sidecar read/write
- integration: two device_ids on one LocalBackend, delete→corrupt→recover cycle
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
import typer

from mind_meld.cli import (
    ManifestFetch,
    _collect_peer_tombstones,
    _fetch_remote_manifest,
    _push_core,
    _recover_prior_manifest,
)
from mind_meld.crypto import encrypt
from mind_meld.devices import register_device
from mind_meld.manifest import serialize_manifest
from mind_meld.storage.local import LocalBackend
from mind_meld import sidecar

PASSPHRASE = "test-passphrase"
MEMORY_KB = 1024


# ── helpers ──────────────────────────────────────────────────────────


def _isolate_sidecar(monkeypatch, tmp_path):
    """Redirect the sidecar to a per-test tmp directory."""
    isolated_dir = tmp_path / "sidecar-home"
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", isolated_dir)
    return isolated_dir


def _put_manifest(backend: LocalBackend, device_id: str, manifest: dict) -> None:
    enc = encrypt(serialize_manifest(manifest), PASSPHRASE, memory_kb=MEMORY_KB)
    backend.put(f"manifests/{device_id}/manifest.json.enc", enc)


def _make_manifest(device_id: str, files: dict, tombstones: dict | None = None) -> dict:
    return {
        "device_id": device_id,
        "device_name": f"dev-{device_id}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "claude": {"base_path": "~/.claude", "files": files},
        },
        "tombstones": tombstones or {},
    }


# ── _recover_prior_manifest ──────────────────────────────────────────


class TestRecoverPriorManifest:
    def test_ok_passes_through(self, tmp_path):
        backend = LocalBackend(tmp_path / "storage")
        manifest = _make_manifest("abc", {"a.md": {"sha256": "aaa"}})
        fetch = ManifestFetch(status="ok", manifest=manifest)

        out = _recover_prior_manifest(
            fetch, backend, "abc", PASSPHRASE, MEMORY_KB, quiet=True
        )
        assert out is manifest

    def test_missing_returns_none(self, tmp_path):
        """First push: missing means 'no prior state', tombstones should be empty.
        Refusing here would break every first push."""
        backend = LocalBackend(tmp_path / "storage")
        fetch = ManifestFetch(status="missing")

        out = _recover_prior_manifest(
            fetch, backend, "abc", PASSPHRASE, MEMORY_KB, quiet=True
        )
        assert out is None

    def test_corrupt_recovers_from_sidecar(self, tmp_path, monkeypatch):
        """Corrupt + sidecar exists → use sidecar as prior state."""
        _isolate_sidecar(monkeypatch, tmp_path)
        backend = LocalBackend(tmp_path / "storage")
        prior = _make_manifest(
            "abc",
            {"a.md": {"sha256": "aaa"}},
            tombstones={"claude:gone.md": {"deleted_at": "2026-04-21T00:00:00+00:00", "device_id": "abc"}},
        )
        sidecar.write(prior)

        fetch = ManifestFetch(status="corrupt")
        out = _recover_prior_manifest(
            fetch, backend, "abc", PASSPHRASE, MEMORY_KB, quiet=True
        )
        assert out is not None
        assert out["tombstones"] == prior["tombstones"]
        assert out["sources"] == prior["sources"]

    def test_corrupt_falls_back_to_peers(self, tmp_path, monkeypatch):
        """Corrupt + no sidecar + peers with tombstones → peer fallback."""
        _isolate_sidecar(monkeypatch, tmp_path)
        backend = LocalBackend(tmp_path / "storage")
        register_device(backend, "me", "me-host")
        register_device(backend, "peer1", "peer-host")

        peer_manifest = _make_manifest(
            "peer1",
            {"a.md": {"sha256": "aaa"}},
            tombstones={"claude:deleted-by-peer.md": {
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "device_id": "peer1",
            }},
        )
        _put_manifest(backend, "peer1", peer_manifest)

        fetch = ManifestFetch(status="corrupt")
        out = _recover_prior_manifest(
            fetch, backend, "me", PASSPHRASE, MEMORY_KB, quiet=True
        )
        assert out is not None
        # Synthetic prior: empty sources, peer tombstones carried forward
        assert out["sources"] == {}
        assert "claude:deleted-by-peer.md" in out["tombstones"]

    def test_corrupt_no_sidecar_no_peers_refuses(self, tmp_path, monkeypatch):
        """Corrupt + no sidecar + no peers → typer.Exit (via _error)."""
        _isolate_sidecar(monkeypatch, tmp_path)
        backend = LocalBackend(tmp_path / "storage")
        register_device(backend, "me", "me-host")

        fetch = ManifestFetch(status="corrupt")
        with pytest.raises(typer.Exit):
            _recover_prior_manifest(
                fetch, backend, "me", PASSPHRASE, MEMORY_KB, quiet=True
            )

    def test_corrupt_peers_all_corrupt_refuses(self, tmp_path, monkeypatch):
        """Corrupt + no sidecar + peers exist but all corrupt → refuse."""
        _isolate_sidecar(monkeypatch, tmp_path)
        backend = LocalBackend(tmp_path / "storage")
        register_device(backend, "me", "me-host")
        register_device(backend, "peer1", "peer-host")
        backend.put("manifests/peer1/manifest.json.enc", b"garbage")

        fetch = ManifestFetch(status="corrupt")
        with pytest.raises(typer.Exit):
            _recover_prior_manifest(
                fetch, backend, "me", PASSPHRASE, MEMORY_KB, quiet=True
            )


# ── sidecar ──────────────────────────────────────────────────────────


class TestSidecar:
    def test_write_then_read_roundtrip(self, tmp_path, monkeypatch):
        _isolate_sidecar(monkeypatch, tmp_path)
        manifest = _make_manifest("abc", {"a.md": {"sha256": "aaa"}})
        sidecar.write(manifest)
        assert sidecar.read("abc") == manifest

    def test_read_missing_returns_none(self, tmp_path, monkeypatch):
        _isolate_sidecar(monkeypatch, tmp_path)
        assert sidecar.read("abc") is None

    def test_read_corrupt_returns_none(self, tmp_path, monkeypatch):
        _isolate_sidecar(monkeypatch, tmp_path)
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        sidecar.sidecar_path().write_text("not json {")
        assert sidecar.read("abc") is None

    def test_read_rejects_wrong_device_id(self, tmp_path, monkeypatch):
        """A sidecar written for device A must NOT be usable by device B.
        Cross-model critical finding: stale sidecar from a previous `mm init`
        could otherwise bulk-tombstone files the new device never had."""
        _isolate_sidecar(monkeypatch, tmp_path)
        manifest = _make_manifest("device-a", {"a.md": {"sha256": "aaa"}})
        sidecar.write(manifest)
        # Same file, read by the "wrong" device id:
        assert sidecar.read("device-b") is None, (
            "sidecar.read must refuse cross-device reuse"
        )
        # Same device id still works:
        assert sidecar.read("device-a") == manifest

    def test_read_rejects_missing_structural_keys(self, tmp_path, monkeypatch):
        """Sidecar without a `sources` or `tombstones` dict is treated as
        corrupt — guards against tampered sidecars injecting fake tombstones."""
        _isolate_sidecar(monkeypatch, tmp_path)
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        sidecar.sidecar_path().write_text(
            json.dumps({"device_id": "abc", "totally": "bogus"})
        )
        assert sidecar.read("abc") is None

    def test_read_rejects_non_dict_sources(self, tmp_path, monkeypatch):
        """sources must be a dict. A crafted sidecar with `sources: [...]`
        would otherwise slip past into generate_tombstones and crash."""
        _isolate_sidecar(monkeypatch, tmp_path)
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        sidecar.sidecar_path().write_text(
            json.dumps({
                "device_id": "abc",
                "sources": ["not a dict"],
                "tombstones": {},
            })
        )
        assert sidecar.read("abc") is None

    def test_write_is_atomic_no_stray_tmp(self, tmp_path, monkeypatch):
        _isolate_sidecar(monkeypatch, tmp_path)
        sidecar.write(_make_manifest("abc", {}))
        siblings = list(sidecar.SIDECAR_DIR.iterdir())
        # only the canonical file should remain; no leftover `.tmp` siblings
        names = [p.name for p in siblings]
        assert "last-push.json" in names
        tmps = [n for n in names if n.endswith(".tmp")]
        assert tmps == [], f"leftover tmp files: {tmps}"


# ── _collect_peer_tombstones ─────────────────────────────────────────


class TestCollectPeerTombstones:
    def test_skips_self_device(self, tmp_path):
        backend = LocalBackend(tmp_path / "storage")
        register_device(backend, "me", "me-host")
        register_device(backend, "peer1", "peer-host")

        my_manifest = _make_manifest(
            "me", {}, tombstones={"claude:mine.md": {"deleted_at": "2026-04-21", "device_id": "me"}},
        )
        peer_manifest = _make_manifest(
            "peer1", {}, tombstones={"claude:theirs.md": {
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "device_id": "peer1",
            }},
        )
        _put_manifest(backend, "me", my_manifest)
        _put_manifest(backend, "peer1", peer_manifest)

        out = _collect_peer_tombstones(backend, "me", PASSPHRASE, MEMORY_KB)
        # mine.md must not appear — we skip self
        assert "claude:mine.md" not in out
        assert "claude:theirs.md" in out

    def test_empty_when_no_peers(self, tmp_path):
        backend = LocalBackend(tmp_path / "storage")
        register_device(backend, "me", "me-host")
        out = _collect_peer_tombstones(backend, "me", PASSPHRASE, MEMORY_KB)
        assert out == {}


# ── Integration: delete → corrupt → recover cycle ────────────────────


class TestPushRewriteOnRecovery:
    """Regression: after recovering from a corrupt manifest, _push_core MUST
    rewrite the remote manifest even if local file diffs are zero. Otherwise
    the corrupt manifest stays in place and recovered tombstones never
    republish (Codex finding X1)."""

    def test_corrupt_remote_healed_when_no_file_changes(
        self, tmp_path, monkeypatch
    ):
        _isolate_sidecar(monkeypatch, tmp_path)

        storage_path = tmp_path / "icloud"
        backend = LocalBackend(storage_path)

        claude_a = tmp_path / "claude-a"
        (claude_a / "projects" / "p1" / "memory").mkdir(parents=True)
        (claude_a / "projects" / "p1" / "memory" / "keep.md").write_text("stays")

        register_device(backend, "mac-a", "mac-a")
        config_a = {
            "device": {"id": "mac-a", "name": "mac-a"},
            "storage": {"path": str(storage_path)},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
            "sync": {
                "claude_dir": str(claude_a),
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_a), "type": "claude"},
                ],
            },
        }

        # First push: legit state persisted to both remote and sidecar.
        _push_core(config_a, PASSPHRASE, MEMORY_KB, quiet=True)

        # Corrupt the remote manifest. Local files unchanged.
        backend.put("manifests/mac-a/manifest.json.enc", b"garbage")

        # Without the fix, this push would report "Nothing to push" and
        # leave the corrupt manifest in place. With the fix, recovery
        # kicks in and the manifest is rewritten.
        _push_core(config_a, PASSPHRASE, MEMORY_KB, quiet=True)

        fetch = _fetch_remote_manifest(backend, "mac-a", PASSPHRASE, MEMORY_KB)
        assert fetch.is_ok, (
            "corrupt manifest should have been healed by the recovery push "
            "even though local file diffs were zero"
        )


class TestGCRefusesOnCorruptPeer:
    """Regression: `mm gc` (dry-run AND write mode) must refuse when any
    peer has a corrupt manifest. Printing an incomplete orphan list in
    dry-run would mislead a user into deleting live blobs (Claude finding
    C3)."""

    def test_dry_run_refuses_when_corrupt_peer(self, tmp_path):
        from mind_meld.cli import _do_gc

        storage = LocalBackend(tmp_path / "storage")
        register_device(storage, "dev1", "Test")
        storage.put("manifests/dev1/manifest.json.enc", b"corrupt bytes")

        config = {
            "device": {"id": "dev1", "name": "Test"},
            "storage": {"path": str(tmp_path / "storage")},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
            "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        }

        with pytest.raises(typer.Exit):
            _do_gc(config, PASSPHRASE, MEMORY_KB, dry_run=True, verbose=False)

    def test_write_mode_refuses_when_corrupt_peer(self, tmp_path):
        from mind_meld.cli import _do_gc

        storage = LocalBackend(tmp_path / "storage")
        register_device(storage, "dev1", "Test")
        storage.put("manifests/dev1/manifest.json.enc", b"corrupt bytes")
        # Create a blob that would otherwise be reaped as orphan:
        storage.put("data/dev1/hash_orphan.enc", b"orphan-bytes")

        config = {
            "device": {"id": "dev1", "name": "Test"},
            "storage": {"path": str(tmp_path / "storage")},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
            "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        }

        with pytest.raises(typer.Exit):
            _do_gc(config, PASSPHRASE, MEMORY_KB, dry_run=False, verbose=False)

        # The blob MUST still exist — GC must never delete while corrupt.
        assert storage.exists("data/dev1/hash_orphan.enc"), (
            "GC must refuse to delete blobs while a manifest is corrupt; "
            "we cannot prove the blob is an orphan without the manifest"
        )


class TestPushRecoveryIntegration:
    """Two device_ids sharing one LocalBackend. Simulates the user-facing
    guarantee: 'delete on Mac A, Mac A's manifest corrupts, next push
    recovers the deletion from the sidecar so Mac B pull still respects
    the deletion.'"""

    def test_sidecar_recovery_preserves_deletion_across_corruption(
        self, tmp_path, monkeypatch
    ):
        _isolate_sidecar(monkeypatch, tmp_path)

        # Shared storage (simulates iCloud)
        storage_path = tmp_path / "icloud"
        backend = LocalBackend(storage_path)

        # Mac A config — we drive push directly
        claude_a = tmp_path / "claude-a"
        (claude_a / "projects" / "p1" / "memory").mkdir(parents=True)
        (claude_a / "projects" / "p1" / "memory" / "keep.md").write_text("stays")
        (claude_a / "projects" / "p1" / "memory" / "kill.md").write_text("doomed")

        register_device(backend, "mac-a", "mac-a-name")
        register_device(backend, "mac-b", "mac-b-name")

        config_a = {
            "device": {"id": "mac-a", "name": "mac-a-name"},
            "storage": {"path": str(storage_path)},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
            "sync": {
                "claude_dir": str(claude_a),
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_a), "type": "claude"},
                ],
            },
        }

        # First push: both files present.
        _push_core(config_a, PASSPHRASE, MEMORY_KB, quiet=True)

        # Sidecar was written — verify it captured the initial state.
        first_sidecar = sidecar.read("mac-a")
        assert first_sidecar is not None
        assert any(
            "kill.md" in path
            for path in first_sidecar["sources"]["claude"]["files"].keys()
        )

        # Delete kill.md locally, push again — tombstone should propagate.
        (claude_a / "projects" / "p1" / "memory" / "kill.md").unlink()
        _push_core(config_a, PASSPHRASE, MEMORY_KB, quiet=True)

        # Verify tombstone was recorded in the remote manifest.
        fetch = _fetch_remote_manifest(backend, "mac-a", PASSPHRASE, MEMORY_KB)
        assert fetch.is_ok
        tombstones = fetch.manifest.get("tombstones", {})
        assert any("kill.md" in key for key in tombstones), (
            f"expected a tombstone for kill.md, got {list(tombstones)}"
        )

        # CORRUPT the remote manifest (simulate iCloud glitch / bit rot).
        backend.put("manifests/mac-a/manifest.json.enc", b"completely-garbage")

        # Third push with no changes to local files — the recovery chain
        # should pull the sidecar and re-write a correct manifest.
        _push_core(config_a, PASSPHRASE, MEMORY_KB, quiet=True)

        # After recovery: remote manifest is readable AND still carries
        # the kill.md tombstone.
        fetch = _fetch_remote_manifest(backend, "mac-a", PASSPHRASE, MEMORY_KB)
        assert fetch.is_ok, "remote manifest should be readable after recovery push"
        tombstones_after = fetch.manifest.get("tombstones", {})
        assert any("kill.md" in key for key in tombstones_after), (
            f"tombstone for kill.md was lost during recovery; got {list(tombstones_after)}"
        )
