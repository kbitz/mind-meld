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


class TestV1TombstoneMigrationEndToEnd:
    """A v1-shaped manifest with bare-path tombstones (defensive: no shipped
    mm version emits this) loaded via load_manifest emerges with claude:-
    prefixed keys, and is_tombstoned correctly reports the deletion under
    source='claude'. Guards against silent deletion-resurrection if such a
    manifest ever lands in storage (manual edit, external tooling, future
    format)."""

    def test_load_then_is_tombstoned(self):
        from mind_meld.manifest import load_manifest

        v1_blob = serialize_manifest({
            "device_id": "peer1",
            "files": {},
            "tombstones": {
                "memory/deleted.md": {
                    "deleted_at": "2026-04-22T10:00:00+00:00",
                    "device_id": "peer1",
                },
            },
        })
        loaded = load_manifest(v1_blob)
        # is_tombstoned uses src:path keys; if migration didn't fire, this
        # would silently return False and the deleted file would re-download.
        assert is_tombstoned("claude", "memory/deleted.md", loaded["tombstones"])
        # Cross-source check still safe: gstack source is unaffected.
        assert not is_tombstoned("gstack", "memory/deleted.md", loaded["tombstones"])

    def test_migrated_key_carries_forward_through_generate_tombstones(self):
        """The whole point of migrating bare keys at v1→v2 promotion is that
        the migrated `claude:foo.md` tombstone is treated as native v2 by
        downstream code. Specifically: when load_manifest produces a migrated
        tombstone and we then call generate_tombstones with that as the prior
        remote, the migrated key must carry forward into the new manifest,
        keeping the deletion record alive across the next push.
        """
        from mind_meld.manifest import load_manifest

        # A v1 prior manifest someone hand-created (or external tooling did).
        v1_prior = serialize_manifest({
            "device_id": "peer1",
            "files": {},
            "tombstones": {
                "memory/deleted.md": {
                    "deleted_at": (
                        datetime.now(timezone.utc) - timedelta(days=1)
                    ).isoformat(),
                    "device_id": "peer1",
                },
            },
        })
        prior_remote = load_manifest(v1_prior)

        # Local has no record of the deleted file (matches the "deleted" state).
        local_manifest = {
            "device_id": "this-device",
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {},
        }

        next_tombstones = generate_tombstones(
            local_manifest, prior_remote, "this-device"
        )
        # The migrated `claude:memory/deleted.md` must survive carry-forward
        # so the next push propagates the deletion to other devices.
        assert "claude:memory/deleted.md" in next_tombstones, (
            "carry-forward dropped the migrated tombstone — silent un-delete"
        )


class TestMergeManifestsAfterLoadRefactor:
    """_merge_manifests no longer normalizes inputs in-loop (relies on
    load_manifest at the fetch boundary). Pin the contract: when called
    with two pre-normalized v2 manifests that overlap on a tombstone,
    newest-timestamp wins."""

    def test_pre_normalized_inputs_merge_correctly(self):
        from mind_meld.cli import _merge_manifests
        from mind_meld.manifest import load_manifest

        old_blob = serialize_manifest({
            "device_id": "peer1",
            "device_name": "old",
            "timestamp": "2026-04-20T10:00:00+00:00",
            "sources": {
                "claude": {
                    "base_path": "",
                    "files": {"a.md": {"sha256": "old", "size": 1, "mtime": "2026-04-20T10:00:00+00:00"}},
                },
            },
            "tombstones": {
                "claude:b.md": {"deleted_at": "2026-04-20T10:00:00+00:00", "device_id": "peer1"},
            },
        })
        new_blob = serialize_manifest({
            "device_id": "peer1",
            "device_name": "new",
            "timestamp": "2026-04-22T10:00:00+00:00",
            "sources": {
                "claude": {
                    "base_path": "",
                    "files": {"a.md": {"sha256": "new", "size": 2, "mtime": "2026-04-22T10:00:00+00:00"}},
                },
            },
            "tombstones": {
                "claude:b.md": {"deleted_at": "2026-04-22T10:00:00+00:00", "device_id": "peer1"},
            },
        })
        old = load_manifest(old_blob)
        new = load_manifest(new_blob)

        merged = _merge_manifests([old, new])
        # File entry: union with newer-timestamp manifest winning per-key.
        assert merged["sources"]["claude"]["files"]["a.md"]["sha256"] == "new"
        # Tombstone: newest-timestamp wins (this is the load-bearing
        # asymmetry from SPEC.md "Merge invariants").
        assert (
            merged["tombstones"]["claude:b.md"]["deleted_at"]
            == "2026-04-22T10:00:00+00:00"
        )

    def test_content_hash_tiebreak_is_deterministic(self):
        """REGRESSION (Group 2 pre-flight 1): two conflict copies with
        identical ISO-second timestamps but different contents must merge
        to the SAME result regardless of input list order.

        Before the fix, Python's stable sort preserved insertion order on
        equal timestamps, and `find_conflict_copies` returns Path.glob
        order (filesystem-dependent, not sorted cross-device). Two Macs
        could briefly see different merged states for the same pair of
        conflict copies.
        """
        from mind_meld.cli import _merge_manifests, _manifest_content_hash
        from mind_meld.manifest import load_manifest

        same_ts = "2026-04-22T10:00:00+00:00"

        a_blob = serialize_manifest({
            "device_id": "peer1",
            "device_name": "hostA",
            "timestamp": same_ts,
            "sources": {
                "claude": {
                    "base_path": "",
                    "files": {"x.md": {"sha256": "aaaa", "size": 1, "mtime": same_ts}},
                },
            },
            "tombstones": {},
        })
        b_blob = serialize_manifest({
            "device_id": "peer1",
            "device_name": "hostB",
            "timestamp": same_ts,
            "sources": {
                "claude": {
                    "base_path": "",
                    "files": {"y.md": {"sha256": "bbbb", "size": 1, "mtime": same_ts}},
                },
            },
            "tombstones": {},
        })
        a = load_manifest(a_blob)
        b = load_manifest(b_blob)

        merged_ab = _merge_manifests([a, b])
        merged_ba = _merge_manifests([b, a])

        # Both inputs contribute files (UNION semantic).
        assert set(merged_ab["sources"]["claude"]["files"].keys()) == {"x.md", "y.md"}
        assert set(merged_ba["sources"]["claude"]["files"].keys()) == {"x.md", "y.md"}

        # Base (device_name, the non-union field from sorted_manifests[-1])
        # is identical regardless of input order — this is the determinism
        # guarantee the tiebreak fix establishes.
        assert merged_ab["device_name"] == merged_ba["device_name"]

        # The winner is the manifest with the lexicographically LARGER
        # content hash (per the sort key). Pin this explicitly so a future
        # "oh let's flip to min instead of max" refactor gets caught.
        hash_a = _manifest_content_hash(a)
        hash_b = _manifest_content_hash(b)
        winner = a if hash_a > hash_b else b
        assert merged_ab["device_name"] == winner["device_name"]


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

        conflicts = backend.find_conflict_copies("manifests/abc/manifest.json.enc")
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
