"""Integration tests for MemSync — full push/pull round-trips."""

import json
import os

import pytest
from typer.testing import CliRunner

from memsync.cli import app
from memsync.config import save_config
from memsync.crypto import decrypt, encrypt
from memsync.devices import list_devices, register_device
from memsync.manifest import (
    build_manifest,
    build_manifest_v2,
    deserialize_manifest,
    diff_manifests,
    normalize_manifest,
    serialize_manifest,
)
from memsync.merge import merge_jsonl
from memsync.storage.local import LocalBackend

PASSPHRASE = "integration-test-passphrase"
MEMORY_KB = 1024  # Low for fast tests
runner = CliRunner()


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(tmp_path / "storage")


@pytest.fixture
def claude_dir_a(tmp_path):
    """Simulate Machine A's ~/.claude."""
    d = tmp_path / "machine_a" / ".claude"
    memory = d / "projects" / "-Users-kb-myapp" / "memory"
    memory.mkdir(parents=True)
    (memory / "user_role.md").write_text("---\nname: role\n---\nData scientist")
    (memory / "feedback.md").write_text("---\nname: feedback\n---\nNo mocks")
    return d


@pytest.fixture
def claude_dir_b(tmp_path):
    """Simulate Machine B's ~/.claude (starts empty)."""
    d = tmp_path / "machine_b" / ".claude"
    d.mkdir(parents=True)
    return d


class TestPushPullRoundTrip:
    def test_push_then_pull(self, storage, claude_dir_a, claude_dir_b):
        """Push from device A, pull to device B — files should match."""
        device_a = "device-a"
        device_b = "device-b"

        # Register both devices
        register_device(storage, device_a, "Machine A")
        register_device(storage, device_b, "Machine B")

        # ── Push from A ──
        manifest_a = build_manifest(
            device_a, "Machine A", str(claude_dir_a)
        )
        assert len(manifest_a["files"]) == 2

        # Upload blobs
        for rel_path, info in manifest_a["files"].items():
            file_path = claude_dir_a / rel_path
            data = file_path.read_bytes()
            enc = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
            storage.put(f"data/{device_a}/{info['sha256']}.enc", enc)

        # Upload manifest
        manifest_bytes = serialize_manifest(manifest_a)
        enc_manifest = encrypt(manifest_bytes, PASSPHRASE, memory_kb=MEMORY_KB)
        storage.put(f"manifests/{device_a}/manifest.json.enc", enc_manifest)

        # ── Pull to B ──
        # Download and decrypt manifest
        enc_manifest_b = storage.get(f"manifests/{device_a}/manifest.json.enc")
        manifest_data = decrypt(enc_manifest_b, PASSPHRASE, memory_kb=MEMORY_KB)
        remote_manifest = deserialize_manifest(manifest_data)

        # Download and decrypt files
        for rel_path, info in remote_manifest["files"].items():
            blob_key = f"data/{device_a}/{info['sha256']}.enc"
            enc_blob = storage.get(blob_key)
            plain = decrypt(enc_blob, PASSPHRASE, memory_kb=MEMORY_KB)

            target = claude_dir_b / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plain)

        # ── Verify ──
        for rel_path in manifest_a["files"]:
            original = (claude_dir_a / rel_path).read_bytes()
            pulled = (claude_dir_b / rel_path).read_bytes()
            assert original == pulled, f"Mismatch: {rel_path}"

    def test_deletion_propagation(self, storage, claude_dir_a, claude_dir_b, tmp_path):
        """Delete a file on A, push, pull to B — file should be gone."""
        device_a = "device-a"
        register_device(storage, device_a, "Machine A")

        # Initial push with 2 files
        manifest_1 = build_manifest(device_a, "A", str(claude_dir_a))
        assert len(manifest_1["files"]) == 2

        for rel_path, info in manifest_1["files"].items():
            data = (claude_dir_a / rel_path).read_bytes()
            enc = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
            storage.put(f"data/{device_a}/{info['sha256']}.enc", enc)

        enc_m1 = encrypt(serialize_manifest(manifest_1), PASSPHRASE, memory_kb=MEMORY_KB)
        storage.put(f"manifests/{device_a}/manifest.json.enc", enc_m1)

        # Pull to B first (so B has both files)
        enc_m = storage.get(f"manifests/{device_a}/manifest.json.enc")
        remote = deserialize_manifest(decrypt(enc_m, PASSPHRASE, memory_kb=MEMORY_KB))
        for rel_path, info in remote["files"].items():
            enc_blob = storage.get(f"data/{device_a}/{info['sha256']}.enc")
            plain = decrypt(enc_blob, PASSPHRASE, memory_kb=MEMORY_KB)
            target = claude_dir_b / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plain)

        # Delete user_role.md on A
        role_path = claude_dir_a / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md"
        role_path.unlink()

        # Push again — manifest should reflect deletion
        manifest_2 = build_manifest(device_a, "A", str(claude_dir_a))
        assert len(manifest_2["files"]) == 1  # Only feedback.md remains

        enc_m2 = encrypt(serialize_manifest(manifest_2), PASSPHRASE, memory_kb=MEMORY_KB)
        storage.put(f"manifests/{device_a}/manifest.json.enc", enc_m2)

        # Pull to B — diff should show session1 as deleted
        enc_m = storage.get(f"manifests/{device_a}/manifest.json.enc")
        remote = deserialize_manifest(decrypt(enc_m, PASSPHRASE, memory_kb=MEMORY_KB))

        # Build B's full local state for diffing (B has both session1 and session2)
        b_files = {}
        projects_dir = claude_dir_b / "projects"
        if projects_dir.exists():
            for path in projects_dir.rglob("*"):
                if path.is_file():
                    from memsync.manifest import hash_file
                    rel = str(path.relative_to(claude_dir_b))
                    b_files[rel] = {"sha256": hash_file(path)}

        # Remote manifest (source of truth) has only session2.
        # diff_manifests(local=remote_manifest, remote=B_local_state):
        #   deleted = files in B's local but NOT in remote manifest → need to delete on B
        diff = diff_manifests({"files": remote["files"]}, {"files": b_files})
        assert len(diff.deleted) == 1
        assert "user_role.md" in diff.deleted[0]


class TestSyncLog:
    def test_writes_log_per_project(self, tmp_path):
        """Sync log should be written to each affected project dir."""
        from memsync.synclog import write_sync_log

        claude_dir = tmp_path / ".claude"
        project_dir = claude_dir / "projects" / "-Users-kb-myapp"
        project_dir.mkdir(parents=True)

        logs = write_sync_log(
            claude_dir=str(claude_dir),
            device_name="MacBook Pro",
            device_id="abc123",
            new_files=["projects/-Users-kb-myapp/memory/user_role.md"],
            modified_files=["projects/-Users-kb-myapp/memory/feedback.md"],
            deleted_files=[],
        )

        assert len(logs) == 1
        log_path = project_dir / ".memsync-log.md"
        assert log_path.exists()
        content = log_path.read_text()
        assert "MacBook Pro" in content
        assert "abc123" in content
        assert "memory/user_role.md" in content
        assert "memory/feedback.md" in content

    def test_no_log_without_changes(self, tmp_path):
        from memsync.synclog import write_sync_log

        claude_dir = tmp_path / ".claude"
        (claude_dir / "projects" / "-foo").mkdir(parents=True)

        logs = write_sync_log(
            claude_dir=str(claude_dir),
            device_name="Other",
            device_id="xyz",
            new_files=[],
            modified_files=[],
            deleted_files=[],
        )
        assert len(logs) == 0


class TestGCSafety:
    def test_gc_never_deletes_referenced_blobs(self, storage):
        """GC must check ALL manifests before deleting."""
        # Device A has hash1 and hash2
        manifest_a = {
            "device_id": "a",
            "device_name": "A",
            "timestamp": "2026-01-01T00:00:00Z",
            "base_path": "/tmp",
            "files": {
                "file1.json": {"sha256": "hash1", "size": 100, "mtime": "2026-01-01T00:00:00Z"},
                "file2.json": {"sha256": "hash2", "size": 200, "mtime": "2026-01-01T00:00:00Z"},
            },
        }

        # Device B has only hash1
        manifest_b = {
            "device_id": "b",
            "device_name": "B",
            "timestamp": "2026-01-01T00:00:00Z",
            "base_path": "/tmp",
            "files": {
                "file1.json": {"sha256": "hash1", "size": 100, "mtime": "2026-01-01T00:00:00Z"},
            },
        }

        register_device(storage, "a", "A")
        register_device(storage, "b", "B")

        # Store manifests
        for did, manifest in [("a", manifest_a), ("b", manifest_b)]:
            enc = encrypt(serialize_manifest(manifest), PASSPHRASE, memory_kb=MEMORY_KB)
            storage.put(f"manifests/{did}/manifest.json.enc", enc)

        # Store blobs
        storage.put("data/a/hash1.enc", encrypt(b"blob1", PASSPHRASE, memory_kb=MEMORY_KB))
        storage.put("data/a/hash2.enc", encrypt(b"blob2", PASSPHRASE, memory_kb=MEMORY_KB))
        storage.put("data/a/hash3.enc", encrypt(b"orphan", PASSPHRASE, memory_kb=MEMORY_KB))

        # Collect referenced hashes from ALL manifests
        all_devices = list_devices(storage)
        referenced = set()
        for d in all_devices:
            did = d["device_id"]
            key = f"manifests/{did}/manifest.json.enc"
            if storage.exists(key):
                enc = storage.get(key)
                plain = decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB)
                m = deserialize_manifest(plain)
                for info in m["files"].values():
                    referenced.add(info["sha256"])

        # hash1 and hash2 are referenced, hash3 is not
        assert "hash1" in referenced
        assert "hash2" in referenced
        assert "hash3" not in referenced

        # GC should only delete hash3
        all_blobs = storage.list_keys("data/")
        orphans = []
        for blob_key in all_blobs:
            if not blob_key.endswith(".enc"):
                continue
            parts = blob_key.split("/")
            if len(parts) == 3:
                sha = parts[2].removesuffix(".enc")
                if sha not in referenced:
                    orphans.append(blob_key)

        assert len(orphans) == 1
        assert "hash3" in orphans[0]


class TestAutoCommands:
    def test_autopull_no_config_exits_silently(self, tmp_path, monkeypatch):
        """autopull should exit silently when msync is not initialized."""
        monkeypatch.setattr("memsync.config.CONFIG_PATH", tmp_path / "nonexistent.toml")
        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_autopush_no_config_exits_silently(self, tmp_path, monkeypatch):
        """autopush should exit silently when msync is not initialized."""
        monkeypatch.setattr("memsync.config.CONFIG_PATH", tmp_path / "nonexistent.toml")
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_autopush_round_trip(self, tmp_path, monkeypatch):
        """autopush should push changes and print a one-line summary."""
        storage_dir = tmp_path / "storage"
        claude_dir_a = tmp_path / "machine_a" / ".claude"
        claude_dir_b = tmp_path / "machine_b" / ".claude"

        # Create memory files on machine A
        memory = claude_dir_a / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        # Write config for device A
        config_a_path = tmp_path / "config_a.toml"
        config_a = {
            "device": {"id": "dev-a", "name": "Mac A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "claude_dir": str(claude_dir_a),
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_dir_a), "type": "claude"},
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config_a, config_a_path)

        # Register device A
        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")

        # Monkeypatch config path and passphrase
        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MEMSYNC_PASSPHRASE", PASSPHRASE)

        # Run autopush
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert "msync: pushed" in result.output
        assert "1 new" in result.output


class TestMultiSourceSync:
    """Integration tests for multi-source (v2 manifest) sync."""

    def _make_claude_dir(self, base: "Path") -> "Path":
        from pathlib import Path
        d = base / ".claude"
        memory = d / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")
        return d

    def _make_gstack_dir(self, base: "Path") -> "Path":
        from pathlib import Path
        d = base / ".gstack"
        projects = d / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (d / "config.yaml").write_text("version: 1")
        return d

    def _make_config(self, tmp_path, storage_dir, claude_dir, device_id, device_name, gstack_dir=None):
        config_path = tmp_path / f"config_{device_id}.toml"
        sources = [
            {"name": "claude", "path": str(claude_dir), "type": "claude"},
        ]
        if gstack_dir is not None:
            sources.append({
                "name": "gstack",
                "path": str(gstack_dir),
                "type": "generic",
                "include_dirs": ["projects"],
                "include_files": ["config.yaml"],
            })
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": sources,
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path, config

    def test_push_pull_multi_source(self, tmp_path, monkeypatch):
        """Push with both claude and gstack from A, pull to B. Both arrive.

        Pull writes to the base_path from the remote manifest, so both
        machines must share the same logical paths for claude/gstack.
        We simulate this by: A populates the shared dirs, pushes, then we
        clear the dirs and pull (acting as B).
        """
        from pathlib import Path
        storage_dir = tmp_path / "storage"

        # Shared paths (simulating both machines having ~/.claude, ~/.gstack)
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Phase 1: Machine A populates and pushes
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        projects = gstack_dir / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (gstack_dir / "config.yaml").write_text("version: 1")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )

        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MEMSYNC_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert "msync: pushed" in result.output

        # Phase 2: Clear local dirs (simulating Machine B that starts empty)
        import shutil
        shutil.rmtree(str(claude_dir))
        claude_dir.mkdir(parents=True)
        shutil.rmtree(str(gstack_dir))
        gstack_dir.mkdir(parents=True)
        (gstack_dir / "projects").mkdir()

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )

        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_b_path)

        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0

        # Verify claude files arrived
        pulled_role = claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md"
        assert pulled_role.exists()
        assert pulled_role.read_text() == "Data scientist"

        # Verify gstack files arrived
        pulled_state = gstack_dir / "projects" / "state.yaml"
        assert pulled_state.exists()
        assert pulled_state.read_text() == "active: true"

        pulled_config = gstack_dir / "config.yaml"
        assert pulled_config.exists()
        assert pulled_config.read_text() == "version: 1"

    def test_jsonl_merge_on_pull(self, tmp_path, monkeypatch):
        """JSONL files are merged (not overwritten) on pull."""
        from pathlib import Path
        storage_dir = tmp_path / "storage"

        # Shared path (both machines see same ~/.claude)
        claude_dir = tmp_path / ".claude"
        memory = claude_dir / "projects" / "-app" / "memory"
        memory.mkdir(parents=True)

        # Phase 1: Machine A has lines 1-3, pushes
        lines_a = [
            '{"ts":"2026-01-01T00:00:00Z","key":"line1"}',
            '{"ts":"2026-01-02T00:00:00Z","key":"line2"}',
            '{"ts":"2026-01-03T00:00:00Z","key":"line3"}',
        ]
        (memory / "learnings.jsonl").write_text("\n".join(lines_a) + "\n")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A"
        )

        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MEMSYNC_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Phase 2: Machine B has lines 1-2 + line4 locally, pulls A's data
        lines_b = [
            '{"ts":"2026-01-01T00:00:00Z","key":"line1"}',
            '{"ts":"2026-01-02T00:00:00Z","key":"line2"}',
            '{"ts":"2026-01-04T00:00:00Z","key":"line4"}',
        ]
        (memory / "learnings.jsonl").write_text("\n".join(lines_b) + "\n")

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B"
        )

        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_b_path)
        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0

        # B should have all 4 lines merged
        merged_text = (memory / "learnings.jsonl").read_text()
        merged_lines = [l for l in merged_text.strip().splitlines() if l.strip()]
        keys = set()
        for line in merged_lines:
            obj = json.loads(line)
            keys.add(obj["key"])
        assert keys == {"line1", "line2", "line3", "line4"}
        assert len(merged_lines) == 4

    def test_source_filter_on_pull(self, tmp_path, monkeypatch):
        """Pull with --source gstack only downloads gstack files."""
        from pathlib import Path
        import shutil
        storage_dir = tmp_path / "storage"

        # Shared paths
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Phase 1: Machine A populates both sources, pushes
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        projects = gstack_dir / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (gstack_dir / "config.yaml").write_text("version: 1")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )

        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MEMSYNC_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Phase 2: Clear local dirs (Machine B starts empty)
        shutil.rmtree(str(claude_dir))
        claude_dir.mkdir(parents=True)
        shutil.rmtree(str(gstack_dir))
        gstack_dir.mkdir(parents=True)
        (gstack_dir / "projects").mkdir()

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )

        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_b_path)
        result = runner.invoke(app, ["pull", "--source", "gstack"])
        assert result.exit_code == 0

        # gstack files should be present
        assert (gstack_dir / "projects" / "state.yaml").exists()
        assert (gstack_dir / "config.yaml").exists()

        # claude files should NOT be present (source filter excluded them)
        assert not (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md").exists()

    def test_source_filter_deletion_scoping(self, tmp_path, monkeypatch):
        """Pulling --source gstack must not delete B's claude files.

        A has only gstack files (no claude). B has both. Pulling from A
        with --source gstack should update gstack but leave claude alone.
        """
        from pathlib import Path
        storage_dir = tmp_path / "storage"

        # Shared paths
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Phase 1: Machine A has only gstack (no claude files), pushes
        claude_dir.mkdir(parents=True)  # exists but empty
        projects = gstack_dir / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (gstack_dir / "config.yaml").write_text("version: 1")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )

        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MEMSYNC_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Phase 2: Machine B has both claude and gstack files locally
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )

        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_b_path)
        result = runner.invoke(app, ["pull", "--source", "gstack"])
        assert result.exit_code == 0

        # B's claude files must still exist (not deleted by gstack-only pull)
        assert (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md").exists()
        assert (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md").read_text() == "Data scientist"

    def test_gc_with_v2_manifest(self, tmp_path, monkeypatch):
        """GC collects hashes from all sources in v2 manifests."""
        from pathlib import Path
        storage_dir = tmp_path / "storage"

        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Populate both sources
        memory = claude_dir / "projects" / "-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        projects = gstack_dir / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (gstack_dir / "config.yaml").write_text("version: 1")

        config_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )

        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")

        # Push (creates v2 manifest with both sources)
        monkeypatch.setattr("memsync.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("memsync.cli.CONFIG_PATH", config_path)
        monkeypatch.setenv("MEMSYNC_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Plant an orphan blob
        orphan_data = encrypt(b"orphan content", PASSPHRASE, memory_kb=MEMORY_KB)
        backend.put("data/dev-a/deadbeef.enc", orphan_data)

        # Run GC
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0
        assert "1" in result.output  # 1 orphan deleted

        # Verify the orphan is gone
        assert not backend.exists("data/dev-a/deadbeef.enc")

        # Verify referenced blobs still exist
        all_blobs = backend.list_keys("data/")
        assert len(all_blobs) >= 3  # at least role.md, state.yaml, config.yaml

    def test_backward_compat_v1_manifest(self, tmp_path):
        """V1 manifests (no "sources") should work via normalize_manifest."""
        v1_manifest = {
            "device_id": "old-device",
            "device_name": "Old Mac",
            "timestamp": "2026-01-01T00:00:00Z",
            "base_path": "/Users/kb/.claude",
            "files": {
                "projects/-myapp/memory/role.md": {
                    "sha256": "abc123",
                    "size": 100,
                    "mtime": "2026-01-01T00:00:00Z",
                },
                "projects/-myapp/todos/tasks.json": {
                    "sha256": "def456",
                    "size": 200,
                    "mtime": "2026-01-01T00:00:00Z",
                },
            },
        }

        # Serialize, then deserialize (simulating storage round-trip)
        data = serialize_manifest(v1_manifest)
        loaded = deserialize_manifest(data)

        # normalize should add sources
        normalize_manifest(loaded)

        assert "sources" in loaded
        assert "claude" in loaded["sources"]
        claude_src = loaded["sources"]["claude"]
        assert claude_src["base_path"] == "/Users/kb/.claude"
        assert len(claude_src["files"]) == 2
        assert claude_src["files"]["projects/-myapp/memory/role.md"]["sha256"] == "abc123"

        # The original "files" key should still be there for backward compat
        assert "files" in loaded
        assert len(loaded["files"]) == 2
