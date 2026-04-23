"""Tests for additive sync model: tombstones, conflict resolution, no-delete pull.

These tests cover the behavioral changes in the additive-only sync model:
- Pull never deletes local files
- Tombstones propagate intentional deletes with 30-day expiry
- Conflict manifest copies are merged additively
- Auto GC runs on interactive push only
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mind_meld.crypto import decrypt, encrypt
from mind_meld.manifest import (
    TOMBSTONE_TTL_DAYS,
    build_manifest,
    collect_tombstones,
    deserialize_manifest,
    diff_manifests,
    generate_tombstones,
    is_tombstoned,
    normalize_manifest,
    serialize_manifest,
)
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "test-passphrase"
MEMORY_KB = 1024


# ── Tombstone tests ──────────────────────────────────────────────────


class TestGenerateTombstones:
    def test_new_tombstone_for_deleted_file(self):
        """File in remote but not local → tombstone generated."""
        local = {
            "sources": {"claude": {"files": {"a.md": {"sha256": "aaa"}}}},
            "tombstones": {},
        }
        remote = {
            "sources": {"claude": {"files": {
                "a.md": {"sha256": "aaa"},
                "b.md": {"sha256": "bbb"},
            }}},
            "tombstones": {},
        }
        tombstones = generate_tombstones(local, remote, "dev1")
        assert "claude:b.md" in tombstones
        assert tombstones["claude:b.md"]["device_id"] == "dev1"

    def test_no_tombstone_when_file_still_exists(self):
        """File in both local and remote → no tombstone."""
        local = {
            "sources": {"claude": {"files": {"a.md": {"sha256": "aaa"}}}},
            "tombstones": {},
        }
        remote = {
            "sources": {"claude": {"files": {"a.md": {"sha256": "aaa"}}}},
            "tombstones": {},
        }
        tombstones = generate_tombstones(local, remote, "dev1")
        assert "claude:a.md" not in tombstones

    def test_carry_forward_non_expired(self):
        """Non-expired tombstones from remote carry forward."""
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        local = {
            "sources": {"claude": {"files": {}}},
            "tombstones": {},
        }
        remote = {
            "sources": {"claude": {"files": {}}},
            "tombstones": {"claude:old.md": {"deleted_at": recent, "device_id": "dev2"}},
        }
        tombstones = generate_tombstones(local, remote, "dev1")
        assert "claude:old.md" in tombstones

    def test_expired_tombstone_dropped(self):
        """Tombstones older than TTL are dropped."""
        expired = (datetime.now(timezone.utc) - timedelta(days=TOMBSTONE_TTL_DAYS + 1)).isoformat()
        local = {
            "sources": {"claude": {"files": {}}},
            "tombstones": {},
        }
        remote = {
            "sources": {"claude": {"files": {}}},
            "tombstones": {"claude:old.md": {"deleted_at": expired, "device_id": "dev2"}},
        }
        tombstones = generate_tombstones(local, remote, "dev1")
        assert "claude:old.md" not in tombstones

    def test_per_source_tombstones(self):
        """Tombstones are source-scoped, don't cross sources."""
        local = {
            "sources": {
                "claude": {"files": {"a.md": {"sha256": "aaa"}}},
                "gstack": {"files": {}},  # gstack has no files
            },
            "tombstones": {},
        }
        remote = {
            "sources": {
                "claude": {"files": {"a.md": {"sha256": "aaa"}}},
                "gstack": {"files": {"g.md": {"sha256": "ggg"}}},
            },
            "tombstones": {},
        }
        tombstones = generate_tombstones(local, remote, "dev1")
        assert "gstack:g.md" in tombstones
        assert "claude:g.md" not in tombstones  # no cross-source leak

    def test_no_remote_manifest(self):
        """No remote manifest → no tombstones (first push)."""
        local = {
            "sources": {"claude": {"files": {"a.md": {"sha256": "aaa"}}}},
            "tombstones": {},
        }
        tombstones = generate_tombstones(local, None, "dev1")
        assert tombstones == {}


class TestCollectTombstones:
    def test_collects_from_multiple_devices(self):
        """Tombstones from all devices are collected."""
        recent = datetime.now(timezone.utc).isoformat()
        manifests = {
            "dev1": {"tombstones": {"claude:a.md": {"deleted_at": recent, "device_id": "dev1"}}},
            "dev2": {"tombstones": {"claude:b.md": {"deleted_at": recent, "device_id": "dev2"}}},
        }
        result = collect_tombstones(
            list(manifests.keys()),
            lambda did: manifests.get(did),
        )
        assert "claude:a.md" in result
        assert "claude:b.md" in result

    def test_latest_tombstone_wins(self):
        """For same path, most recent tombstone wins."""
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        manifests = {
            "dev1": {"tombstones": {"claude:a.md": {"deleted_at": old, "device_id": "dev1"}}},
            "dev2": {"tombstones": {"claude:a.md": {"deleted_at": new, "device_id": "dev2"}}},
        }
        result = collect_tombstones(
            list(manifests.keys()),
            lambda did: manifests.get(did),
        )
        assert result["claude:a.md"]["device_id"] == "dev2"

    def test_expired_tombstones_filtered(self):
        """Expired tombstones are not collected."""
        expired = (datetime.now(timezone.utc) - timedelta(days=TOMBSTONE_TTL_DAYS + 1)).isoformat()
        manifests = {
            "dev1": {"tombstones": {"claude:a.md": {"deleted_at": expired, "device_id": "dev1"}}},
        }
        result = collect_tombstones(
            ["dev1"],
            lambda did: manifests.get(did),
        )
        assert "claude:a.md" not in result

    def test_missing_manifest_skipped(self):
        """Devices with no manifest are skipped."""
        result = collect_tombstones(
            ["dev1", "dev2"],
            lambda did: None,
        )
        assert result == {}


class TestIsTombstoned:
    def test_tombstoned_path(self):
        tombstones = {"claude:a.md": {"deleted_at": "2026-01-01T00:00:00Z", "device_id": "dev1"}}
        assert is_tombstoned("claude", "a.md", tombstones) is True

    def test_non_tombstoned_path(self):
        tombstones = {"claude:a.md": {"deleted_at": "2026-01-01T00:00:00Z", "device_id": "dev1"}}
        assert is_tombstoned("claude", "b.md", tombstones) is False

    def test_cross_source_not_tombstoned(self):
        """Tombstone in claude source should not affect gstack source."""
        tombstones = {"claude:a.md": {"deleted_at": "2026-01-01T00:00:00Z", "device_id": "dev1"}}
        assert is_tombstoned("gstack", "a.md", tombstones) is False


# ── Normalize manifest with tombstones ────────────────────────────


class TestNormalizeManifestTombstones:
    def test_adds_tombstones_key(self):
        """Old manifests without tombstones get an empty dict."""
        manifest = {"files": {"a.md": {"sha256": "aaa"}}}
        normalize_manifest(manifest)
        assert "tombstones" in manifest
        assert manifest["tombstones"] == {}

    def test_preserves_existing_tombstones(self):
        """Manifests with tombstones are preserved."""
        manifest = {
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {"a.md": {"deleted_at": "2026-01-01", "device_id": "d1"}},
        }
        normalize_manifest(manifest)
        assert "a.md" in manifest["tombstones"]


# ── Conflict manifest resolution ─────────────────────────────────


class TestConflictManifestMerge:
    """Tests for _merge_manifests and conflict-aware _fetch_remote_manifest."""

    def test_dropbox_regex_checks_stem(self, tmp_path):
        """Dropbox conflict regex must check base filename, not just extension."""
        backend = LocalBackend(tmp_path / "storage")
        backend.put("manifests/abc/manifest.json.enc", b"data")

        # Create an unrelated .enc file with Dropbox conflict pattern
        unrelated = tmp_path / "storage" / "manifests" / "abc" / "other.json (conflicted copy 2026-03-18).enc"
        unrelated.write_bytes(b"unrelated")

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            lambda _: True,  # validator doesn't matter — regex already excludes
        )
        assert len(conflicts) == 0, "Should not match unrelated .enc files"

    def test_conflict_copy_read_error_skipped(self, tmp_path):
        """Conflict copies that can't be read are skipped gracefully."""
        from mind_meld.cli import _fetch_remote_manifest

        backend = LocalBackend(tmp_path / "storage")

        # Write a valid canonical manifest
        manifest = {"device_id": "abc", "timestamp": "2026-01-01T00:00:00Z", "files": {"a.md": {"sha256": "aaa", "size": 10, "mtime": "2026-01-01"}}}
        enc = encrypt(serialize_manifest(manifest), PASSPHRASE, memory_kb=MEMORY_KB)
        backend.put("manifests/abc/manifest.json.enc", enc)

        # Create a corrupt conflict copy
        conflict = tmp_path / "storage" / "manifests" / "abc" / "manifest.json 2.enc"
        conflict.write_bytes(b"not valid encrypted data")

        # Should still return the canonical manifest (status == "ok")
        result = _fetch_remote_manifest(backend, "abc", PASSPHRASE, MEMORY_KB)
        assert result.is_ok
        assert "a.md" in result.manifest.get("files", {})

    def test_all_copies_corrupt_returns_corrupt_status(self, tmp_path):
        """If all manifest copies are corrupt, return status='corrupt'."""
        from mind_meld.cli import _fetch_remote_manifest

        backend = LocalBackend(tmp_path / "storage")
        backend.put("manifests/abc/manifest.json.enc", b"corrupt data")

        result = _fetch_remote_manifest(backend, "abc", PASSPHRASE, MEMORY_KB)
        assert result.status == "corrupt"
        assert result.manifest is None

    def test_no_manifest_at_all_returns_missing_status(self, tmp_path):
        """If no manifest exists (first push / fresh device), return
        status='missing' — NOT corrupt. Conflating the two would refuse
        every first push."""
        from mind_meld.cli import _fetch_remote_manifest

        backend = LocalBackend(tmp_path / "storage")
        # no put — device has never pushed

        result = _fetch_remote_manifest(backend, "fresh-device", PASSPHRASE, MEMORY_KB)
        assert result.status == "missing"
        assert result.manifest is None

    def test_validator_magic_byte_shortcut_avoids_argon2(self, tmp_path, monkeypatch):
        """Cheap shortcut: validator bails on first byte if it isn't the
        Mind Meld format version (0x01) — avoids Argon2 per non-manifest
        sibling. Without this, a user with 20 stale iCloud conflict
        siblings would see 4-10s hang in recovery."""
        from mind_meld import cli as cli_module
        from mind_meld.cli import _make_manifest_validator

        decrypt_calls: list[int] = []
        real_decrypt = cli_module.decrypt

        def spy_decrypt(data, passphrase, memory_kb):
            decrypt_calls.append(len(data))
            return real_decrypt(data, passphrase, memory_kb)

        monkeypatch.setattr(cli_module, "decrypt", spy_decrypt)
        validator = _make_manifest_validator(PASSPHRASE, MEMORY_KB)

        # Candidate with wrong magic byte: validator rejects WITHOUT decrypt.
        bogus = tmp_path / "bogus.enc"
        bogus.write_bytes(b"\xff" + b"garbage payload" * 100)
        assert validator(bogus) is False
        assert decrypt_calls == [], (
            "validator must NOT call decrypt for non-0x01 candidates"
        )

        # Empty file also rejected without decrypt.
        empty = tmp_path / "empty.enc"
        empty.write_bytes(b"")
        assert validator(empty) is False
        assert decrypt_calls == []

    def test_bogus_sibling_does_not_flip_missing_to_corrupt(self, tmp_path):
        """CRITICAL: a stray file in manifests/<device>/ whose name matches
        the iCloud pattern but doesn't decrypt as a manifest must NOT flip
        status=missing into status=corrupt. Without the validator, the
        conflict-copy regex alone sets had_any_source=True and the caller
        mis-routes to corrupt-recovery when storage is actually fine."""
        from mind_meld.cli import _fetch_remote_manifest

        backend = LocalBackend(tmp_path / "storage")
        manifests_dir = tmp_path / "storage" / "manifests" / "fresh-device"
        manifests_dir.mkdir(parents=True)
        # No canonical manifest. User (or another tool) left a file whose
        # name matches the iCloud conflict pattern but with random bytes.
        (manifests_dir / "manifest.json 2.enc").write_bytes(
            b"not a Mind Meld blob"
        )

        result = _fetch_remote_manifest(backend, "fresh-device", PASSPHRASE, MEMORY_KB)
        assert result.status == "missing", (
            "Bogus sibling must not flip missing → corrupt"
        )
        assert result.manifest is None

    def test_cleanup_only_from_mutating_ops(self, tmp_path):
        """_cleanup_conflict_copies deletes only validator-approved conflicts."""
        from mind_meld.cli import _cleanup_conflict_copies

        backend = LocalBackend(tmp_path / "storage")
        backend.put("manifests/abc/manifest.json.enc", b"original")

        # Write a REAL encrypted manifest as the conflict — validator approves.
        real_manifest = {
            "device_id": "abc",
            "timestamp": "2026-01-01T00:00:00Z",
            "files": {},
        }
        enc = encrypt(serialize_manifest(real_manifest), PASSPHRASE, memory_kb=MEMORY_KB)
        real_conflict = tmp_path / "storage" / "manifests" / "abc" / "manifest.json 2.enc"
        real_conflict.write_bytes(enc)

        # Also write a BOGUS file matching the same pattern — validator rejects.
        bogus = tmp_path / "storage" / "manifests" / "abc" / "manifest.json 3.enc"
        bogus.write_bytes(b"not a real manifest")

        count = _cleanup_conflict_copies(backend, "abc", PASSPHRASE, MEMORY_KB)
        assert count == 1
        assert not real_conflict.exists(), "Real conflict copy must be deleted"
        assert bogus.exists(), "Bogus sibling must be left alone"


# ── Additive pull behavior ───────────────────────────────────────


class TestAdditivePull:
    def test_pull_preserves_local_only_files(self, tmp_path):
        """Files that exist locally but not remotely must be preserved."""
        # This is the core additive model test
        local_files = {
            "a.md": {"sha256": "aaa"},
            "b.md": {"sha256": "bbb"},
        }
        remote_files = {
            "a.md": {"sha256": "aaa"},
            # b.md is local-only
        }

        diff = diff_manifests(
            {"files": remote_files},  # remote is "local" arg (source of truth for what to download)
            {"files": local_files},   # local is "remote" arg (what we compare against)
        )

        # diff.deleted contains local-only files, but additive pull ignores them
        assert len(diff.deleted) == 1
        assert "b.md" in diff.deleted

        # The pull code uses only diff.new and diff.modified, NOT diff.deleted
        to_download = {**diff.new, **diff.modified}
        assert len(to_download) == 0  # nothing to download, everything in sync


# ── Auto GC ──────────────────────────────────────────────────────


class TestAutoGC:
    def test_gc_returns_count(self, tmp_path):
        """_do_gc should return the number of orphaned blobs deleted."""
        from mind_meld.cli import _do_gc
        from mind_meld.config import save_config
        from mind_meld.devices import register_device

        storage = LocalBackend(tmp_path / "storage")
        config = {
            "device": {"id": "dev1", "name": "Test"},
            "storage": {"path": str(tmp_path / "storage")},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
            "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        }

        register_device(storage, "dev1", "Test")

        # Create a manifest referencing hash1
        manifest = {
            "device_id": "dev1",
            "device_name": "Test",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "files": {"a.md": {"sha256": "hash1", "size": 10, "mtime": "2026-01-01"}},
            "sources": {"claude": {"base_path": "", "files": {"a.md": {"sha256": "hash1", "size": 10, "mtime": "2026-01-01"}}}},
            "tombstones": {},
        }
        enc = encrypt(serialize_manifest(manifest), PASSPHRASE, memory_kb=MEMORY_KB)
        storage.put("manifests/dev1/manifest.json.enc", enc)

        # Create referenced + orphaned blobs
        storage.put("data/dev1/hash1.enc", b"referenced")
        storage.put("data/dev1/hash_orphan.enc", b"orphaned")

        count = _do_gc(config, PASSPHRASE, MEMORY_KB, dry_run=False, verbose=False)
        assert count == 1
        assert storage.exists("data/dev1/hash1.enc")
        assert not storage.exists("data/dev1/hash_orphan.enc")


class TestTmpSweep:
    """`mm gc` sweeps stale tmp*.tmp files this device left behind."""

    def _make_config(self, tmp_path, device_id: str) -> dict:
        return {
            "device": {"id": device_id, "name": f"dev-{device_id}"},
            "storage": {"path": str(tmp_path / "storage")},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
            "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        }

    def test_sweeps_this_device_tmp_files(self, tmp_path):
        """Orphan tmp*.tmp under this device's subtrees (data/, manifests/)
        are reaped. devices/ is intentionally excluded — see docstring."""
        from mind_meld.cli import _sweep_local_tmp_files

        storage = LocalBackend(tmp_path / "storage")
        (storage.root / "data" / "dev1").mkdir(parents=True)
        (storage.root / "manifests" / "dev1").mkdir(parents=True)
        (storage.root / "data" / "dev1" / "tmpabc.tmp").write_bytes(b"x")
        (storage.root / "manifests" / "dev1" / "tmpdef.tmp").write_bytes(b"y")

        count = _sweep_local_tmp_files(storage, "dev1", dry_run=False, verbose=False)
        assert count == 2
        assert not (storage.root / "data" / "dev1" / "tmpabc.tmp").exists()
        assert not (storage.root / "manifests" / "dev1" / "tmpdef.tmp").exists()

    def test_never_sweeps_devices_dir(self, tmp_path):
        """devices/ is a flat shared directory — tmp files there could
        be a peer's in-flight write. Never touched."""
        from mind_meld.cli import _sweep_local_tmp_files

        storage = LocalBackend(tmp_path / "storage")
        (storage.root / "devices").mkdir(parents=True)
        stranded = storage.root / "devices" / "tmpxyz.tmp"
        stranded.write_bytes(b"z")

        count = _sweep_local_tmp_files(storage, "dev1", dry_run=False, verbose=False)
        assert count == 0
        assert stranded.exists(), "devices/ tmp must never be touched"

    def test_never_sweeps_peer_subtrees(self, tmp_path):
        """CRITICAL: peer device subtrees must not be touched.

        iCloud may be mid-uploading a peer's tmp file; reaping it would
        corrupt a peer's in-flight write."""
        from mind_meld.cli import _sweep_local_tmp_files

        storage = LocalBackend(tmp_path / "storage")
        # This device's tmp AND peer's tmp
        (storage.root / "data" / "dev1").mkdir(parents=True)
        (storage.root / "data" / "dev2-peer").mkdir(parents=True)
        (storage.root / "manifests" / "dev1").mkdir(parents=True)
        (storage.root / "manifests" / "dev2-peer").mkdir(parents=True)
        mine_data = storage.root / "data" / "dev1" / "tmpA.tmp"
        peer_data = storage.root / "data" / "dev2-peer" / "tmpB.tmp"
        mine_manifest = storage.root / "manifests" / "dev1" / "tmpC.tmp"
        peer_manifest = storage.root / "manifests" / "dev2-peer" / "tmpD.tmp"
        for p in (mine_data, peer_data, mine_manifest, peer_manifest):
            p.write_bytes(b"x")

        count = _sweep_local_tmp_files(storage, "dev1", dry_run=False, verbose=False)
        assert count == 2
        assert not mine_data.exists()
        assert not mine_manifest.exists()
        assert peer_data.exists(), "peer subtree must never be touched"
        assert peer_manifest.exists(), "peer subtree must never be touched"

    def test_does_not_sweep_non_tmp_files(self, tmp_path):
        """Normal .enc files in this device's subtree survive the sweep."""
        from mind_meld.cli import _sweep_local_tmp_files

        storage = LocalBackend(tmp_path / "storage")
        (storage.root / "data" / "dev1").mkdir(parents=True)
        (storage.root / "manifests" / "dev1").mkdir(parents=True)
        normal_blob = storage.root / "data" / "dev1" / "abcdef123.enc"
        normal_manifest = storage.root / "manifests" / "dev1" / "manifest.json.enc"
        normal_blob.write_bytes(b"data")
        normal_manifest.write_bytes(b"manifest")

        count = _sweep_local_tmp_files(storage, "dev1", dry_run=False, verbose=False)
        assert count == 0
        assert normal_blob.exists()
        assert normal_manifest.exists()

    def test_dry_run_previews_without_deleting(self, tmp_path):
        from mind_meld.cli import _sweep_local_tmp_files

        storage = LocalBackend(tmp_path / "storage")
        (storage.root / "data" / "dev1").mkdir(parents=True)
        stranded = storage.root / "data" / "dev1" / "tmp123.tmp"
        stranded.write_bytes(b"x")

        count = _sweep_local_tmp_files(storage, "dev1", dry_run=True, verbose=False)
        assert count == 1
        assert stranded.exists(), "dry_run must not actually delete"
