"""Integration tests for Mind Meld — full push/pull round-trips."""

import json
import os

import pytest
from typer.testing import CliRunner

from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import (
    bootstrap_crypto_init,
    decrypt,
    encrypt,
    root_salt_fingerprint,
)
from mind_meld.devices import list_devices, register_device
from mind_meld.manifest import (
    build_manifest_v2,
    deserialize_manifest,
    diff_files,
    normalize_manifest,
    serialize_manifest,
)
from mind_meld.merge import merge_jsonl
from mind_meld.storage.local import LocalBackend

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
        manifest_a = build_manifest_v2(
            device_a, "Machine A",
            [{"name": "claude", "type": "claude", "path": str(claude_dir_a)}],
        )
        files_a = manifest_a["sources"]["claude"]["files"]
        assert len(files_a) == 2

        # Upload blobs
        for rel_path, info in files_a.items():
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
        remote_files = remote_manifest["sources"]["claude"]["files"]

        # Download and decrypt files
        for rel_path, info in remote_files.items():
            blob_key = f"data/{device_a}/{info['sha256']}.enc"
            enc_blob = storage.get(blob_key)
            plain = decrypt(enc_blob, PASSPHRASE, memory_kb=MEMORY_KB)

            target = claude_dir_b / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plain)

        # ── Verify ──
        for rel_path in files_a:
            original = (claude_dir_a / rel_path).read_bytes()
            pulled = (claude_dir_b / rel_path).read_bytes()
            assert original == pulled, f"Mismatch: {rel_path}"

    def test_deletion_propagation(self, storage, claude_dir_a, claude_dir_b, tmp_path):
        """Delete a file on A, push, pull to B — file should be gone."""
        device_a = "device-a"
        register_device(storage, device_a, "Machine A")

        # Initial push with 2 files
        manifest_1 = build_manifest_v2(
            device_a, "A",
            [{"name": "claude", "type": "claude", "path": str(claude_dir_a)}],
        )
        files_1 = manifest_1["sources"]["claude"]["files"]
        assert len(files_1) == 2

        for rel_path, info in files_1.items():
            data = (claude_dir_a / rel_path).read_bytes()
            enc = encrypt(data, PASSPHRASE, memory_kb=MEMORY_KB)
            storage.put(f"data/{device_a}/{info['sha256']}.enc", enc)

        enc_m1 = encrypt(serialize_manifest(manifest_1), PASSPHRASE, memory_kb=MEMORY_KB)
        storage.put(f"manifests/{device_a}/manifest.json.enc", enc_m1)

        # Pull to B first (so B has both files)
        enc_m = storage.get(f"manifests/{device_a}/manifest.json.enc")
        remote = deserialize_manifest(decrypt(enc_m, PASSPHRASE, memory_kb=MEMORY_KB))
        for rel_path, info in remote["sources"]["claude"]["files"].items():
            enc_blob = storage.get(f"data/{device_a}/{info['sha256']}.enc")
            plain = decrypt(enc_blob, PASSPHRASE, memory_kb=MEMORY_KB)
            target = claude_dir_b / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plain)

        # Delete user_role.md on A
        role_path = claude_dir_a / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md"
        role_path.unlink()

        # Push again — manifest should reflect deletion
        manifest_2 = build_manifest_v2(
            device_a, "A",
            [{"name": "claude", "type": "claude", "path": str(claude_dir_a)}],
        )
        files_2 = manifest_2["sources"]["claude"]["files"]
        assert len(files_2) == 1  # Only feedback.md remains

        enc_m2 = encrypt(serialize_manifest(manifest_2), PASSPHRASE, memory_kb=MEMORY_KB)
        storage.put(f"manifests/{device_a}/manifest.json.enc", enc_m2)

        # Pull to B — in the additive model, diff still computes "deleted" but
        # pull never acts on it. B keeps user_role.md even though A deleted it.
        enc_m = storage.get(f"manifests/{device_a}/manifest.json.enc")
        remote = deserialize_manifest(decrypt(enc_m, PASSPHRASE, memory_kb=MEMORY_KB))

        b_files = {}
        projects_dir = claude_dir_b / "projects"
        if projects_dir.exists():
            for path in projects_dir.rglob("*"):
                if path.is_file():
                    from mind_meld.manifest import hash_file
                    rel = str(path.relative_to(claude_dir_b))
                    b_files[rel] = {"sha256": hash_file(path)}

        # diff_files still computes deleted (for push context),
        # but the additive pull model ignores it.
        diff = diff_files(remote["sources"]["claude"]["files"], b_files)
        assert len(diff.deleted) == 1
        assert "user_role.md" in diff.deleted[0]

        # Verify B's local copy is preserved (additive model)
        role_b = claude_dir_b / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md"
        assert role_b.exists(), "Additive pull must preserve local-only files"


class TestSyncLog:
    def test_writes_log_per_project(self, tmp_path):
        """Sync log should be written to each affected project dir."""
        from mind_meld.synclog import write_sync_log

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
        log_path = project_dir / ".mind-meld-log.md"
        assert log_path.exists()
        content = log_path.read_text()
        assert "MacBook Pro" in content
        assert "abc123" in content
        assert "memory/user_role.md" in content
        assert "memory/feedback.md" in content

    def test_no_log_without_changes(self, tmp_path):
        from mind_meld.synclog import write_sync_log

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

    def test_sync_log_routes_through_fsutil_with_fsync_false(
        self, tmp_path, monkeypatch
    ):
        """Sync log writes must go through fsutil with fsync=False —
        .mind-meld-log.md is cosmetic; per-file fsync would add pull latency."""
        from mind_meld import synclog

        claude_dir = tmp_path / ".claude"
        (claude_dir / "projects" / "-foo").mkdir(parents=True)

        calls: list[dict] = []
        real_write = synclog.fsutil.atomic_write_bytes

        def spy_write(path, data, *, fsync=False, mode=None):
            calls.append({"path": path, "fsync": fsync, "mode": mode})
            real_write(path, data, fsync=fsync, mode=mode)

        monkeypatch.setattr(synclog.fsutil, "atomic_write_bytes", spy_write)
        synclog.write_sync_log(
            claude_dir=str(claude_dir),
            device_name="Other",
            device_id="xyz",
            new_files=["projects/-foo/memory/x.md"],
            modified_files=[],
            deleted_files=[],
        )
        assert len(calls) == 1
        assert calls[0]["fsync"] is False


class TestGCSafety:
    def test_gc_never_deletes_referenced_blobs(self, storage):
        """GC must check ALL manifests before deleting.

        Manifests use the v2 shape (sources dict) that production emits, and
        the ref-counting loop reads via `load_manifest` so it exercises the
        same normalization path `_do_gc` hits (see cli.py:_do_gc).
        """
        from mind_meld.manifest import load_manifest

        # Device A has hash1 and hash2
        manifest_a = {
            "device_id": "a",
            "device_name": "A",
            "timestamp": "2026-01-01T00:00:00Z",
            "sources": {
                "claude": {
                    "base_path": "/tmp",
                    "files": {
                        "file1.json": {"sha256": "hash1", "size": 100, "mtime": "2026-01-01T00:00:00Z"},
                        "file2.json": {"sha256": "hash2", "size": 200, "mtime": "2026-01-01T00:00:00Z"},
                    },
                },
            },
            "tombstones": {},
        }

        # Device B has only hash1
        manifest_b = {
            "device_id": "b",
            "device_name": "B",
            "timestamp": "2026-01-01T00:00:00Z",
            "sources": {
                "claude": {
                    "base_path": "/tmp",
                    "files": {
                        "file1.json": {"sha256": "hash1", "size": 100, "mtime": "2026-01-01T00:00:00Z"},
                    },
                },
            },
            "tombstones": {},
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

        # Collect referenced hashes from ALL manifests — same shape _do_gc uses.
        all_devices = list_devices(storage)
        referenced = set()
        for d in all_devices:
            did = d["device_id"]
            key = f"manifests/{did}/manifest.json.enc"
            if storage.exists(key):
                enc = storage.get(key)
                plain = decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB)
                m = load_manifest(plain)
                for src_data in m["sources"].values():
                    for info in src_data["files"].values():
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
        """autopull should exit silently when mm is not initialized."""
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "nonexistent.toml")
        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_autopush_no_config_exits_silently(self, tmp_path, monkeypatch):
        """autopush should exit silently when mm is not initialized."""
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "nonexistent.toml")
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_autopull_bad_config_prints_stderr_and_exits_zero(self, tmp_path, monkeypatch):
        """Regression for eager validation: a config file that exists but has
        invalid sync.sources must NOT be silently swallowed — autopull should
        emit a one-line stderr and exit cleanly (Claude Code hook must see the
        failure instead of sync just stopping forever)."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[device]\n'
            'id = "abc"\n'
            'name = "Mac"\n'
            '[storage]\n'
            f'path = "{tmp_path / "storage"}"\n'
            '[[sync.sources]]\n'
            'name = "claude"\n'
            'type = "claude"\n'
            # no path — eager validation catches it
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_path)

        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0
        assert "mm: pull failed" in result.stderr
        assert "missing required field" in result.stderr

    def test_autopush_bad_config_prints_stderr_and_exits_zero(self, tmp_path, monkeypatch):
        """Regression for eager validation: autopush must surface bad-config errors
        on stderr rather than silently swallowing them."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[device]\n'
            'id = "abc"\n'
            'name = "Mac"\n'
            '[storage]\n'
            f'path = "{tmp_path / "storage"}"\n'
            '[[sync.sources]]\n'
            'name = "claude"\n'
            'type = "claude"\n'
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_path)

        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert "mm: push failed" in result.stderr
        assert "missing required field" in result.stderr

    def test_autopush_silent_when_lock_held(self, tmp_path, monkeypatch):
        """autopush must exit silently if another mm process holds the lock.

        Simulates the Claude Code hot-path where `mm autopush` and
        `mm autopull` can fire simultaneously — exactly one acquires
        the flock; the loser must not crash, bubble a traceback, or
        write junk to stdout."""
        import subprocess
        import sys
        import textwrap
        import time
        from pathlib import Path
        from mind_meld.config import LOCK_PATH

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory").mkdir(parents=True)
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md").write_text("x")

        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "dev-a", "name": "Mac A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "claude_dir": str(claude_dir),
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_dir), "type": "claude"},
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)

        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")

        # Redirect the lockfile to a per-test path so this doesn't
        # contaminate the user's real ~/.config/mind-meld/mind-meld.lock.
        test_lock = tmp_path / "test.lock"
        monkeypatch.setattr("mind_meld.config.LOCK_PATH", test_lock)
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", test_lock)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # Spawn a child that grabs the lock and holds it until stdin closes.
        repo_src = str(Path(__file__).parent.parent / "src")
        ready_marker = tmp_path / "child-ready"
        child_script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {repo_src!r})
            from pathlib import Path
            from mind_meld.lockfile import acquire_lock, release_lock

            lp = Path({str(test_lock)!r})
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

            # Now try autopush — must exit cleanly, not crash.
            result = runner.invoke(app, ["autopush"])
            assert result.exit_code == 0, (
                f"autopush must handle lock contention gracefully, "
                f"got exit={result.exit_code} output={result.output!r}"
            )
            # autopush is a silent command; a LockError must surface only
            # as a short stderr line, never a traceback.
            assert "Traceback" not in result.output
            assert "Traceback" not in (result.stderr or "")
        finally:
            if child.stdin:
                child.stdin.close()
            child.wait(timeout=5)

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
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")

        # Monkeypatch config path and passphrase
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # Run autopush
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert "mm: pushed" in result.output
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
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert "mm: pushed" in result.output

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

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_b_path)

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

    def test_pull_calls_fsync_dir_per_unique_parent(self, tmp_path, monkeypatch):
        """End-of-pull deferred durability: fsync_dir called once per
        unique parent directory that received a write. Verifies the
        durability is actually deferred (not per-file) AND actually
        happens (not dropped entirely)."""
        from pathlib import Path
        from mind_meld import fsutil
        from mind_meld import cli as cli_module

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Seed 3 distinct parent dirs on the "sending" side.
        (claude_dir / "projects" / "-app-a" / "memory").mkdir(parents=True)
        (claude_dir / "projects" / "-app-a" / "memory" / "a.md").write_text("A")
        (claude_dir / "projects" / "-app-b" / "memory").mkdir(parents=True)
        (claude_dir / "projects" / "-app-b" / "memory" / "b.md").write_text("B")
        (gstack_dir / "projects").mkdir(parents=True)
        (gstack_dir / "projects" / "state.yaml").write_text("x")
        (gstack_dir / "config.yaml").write_text("v: 1")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )
        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        runner.invoke(app, ["autopush"])

        # Wipe local dirs to simulate Machine B.
        import shutil
        shutil.rmtree(str(claude_dir)); claude_dir.mkdir(parents=True)
        shutil.rmtree(str(gstack_dir)); gstack_dir.mkdir(parents=True)
        (gstack_dir / "projects").mkdir()

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_b_path)

        # Spy: capture every fsync_dir call Pull triggers.
        calls: list[Path] = []
        real_fsync_dir = fsutil.fsync_dir

        def spy_fsync_dir(path):
            calls.append(Path(path))
            real_fsync_dir(path)

        # cli.py does `from mind_meld import fsutil` at import time and
        # calls `fsutil.fsync_dir(...)` through that alias, so patch the
        # attribute on cli.fsutil (which IS the same module object).
        monkeypatch.setattr(cli_module.fsutil, "fsync_dir", spy_fsync_dir)

        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0

        # At least 4 unique parent dirs should have been written to during pull:
        #   <claude>/projects/-app-a/memory  (a.md)
        #   <claude>/projects/-app-b/memory  (b.md)
        #   <gstack>/projects                (state.yaml)
        #   <gstack>                         (config.yaml)
        # Synclog may add ".mind-meld-log.md" writes but those are internal.
        unique_parents = set(calls)
        expected = {
            claude_dir / "projects" / "-app-a" / "memory",
            claude_dir / "projects" / "-app-b" / "memory",
            gstack_dir / "projects",
            gstack_dir,
        }
        missing = expected - unique_parents
        assert not missing, (
            f"expected fsync_dir calls for {expected}, missing {missing}. "
            f"actual: {unique_parents}"
        )
        # Deferred-durability invariant: fsync_dir called exactly once per
        # unique parent (not per file).
        assert len(calls) == len(unique_parents)

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
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
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

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_b_path)
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
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
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

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_b_path)
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
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Phase 2: Machine B has both claude and gstack files locally
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_b_path)
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
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")

        # Push (creates v2 manifest with both sources)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Plant an orphan blob. Post-1C: path must match the 64-hex sha
        # shape or _do_gc routes it through the malformed-count path.
        orphan_sha = "de" * 32  # 64-hex
        orphan_data = encrypt(b"orphan content", PASSPHRASE, memory_kb=MEMORY_KB)
        backend.put(f"data/dev-a/{orphan_sha}.enc", orphan_data)

        # Run GC
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0
        assert "1" in result.output  # 1 orphan deleted

        # Verify the orphan is gone
        assert not backend.exists(f"data/dev-a/{orphan_sha}.enc")

        # Verify referenced blobs still exist
        all_blobs = backend.list_keys("data/")
        assert len(all_blobs) >= 3  # at least role.md, state.yaml, config.yaml

    def test_backward_compat_v1_manifest(self, tmp_path):
        """V1 manifests (no "sources") should work via normalize_manifest."""
        v1_manifest = {
            "device_id": "old-device",
            "device_name": "Old Mac",
            "timestamp": "2026-01-01T00:00:00Z",
            "base_path": "/Users/alice/.claude",
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
        assert claude_src["base_path"] == "/Users/alice/.claude"
        assert len(claude_src["files"]) == 2
        assert claude_src["files"]["projects/-myapp/memory/role.md"]["sha256"] == "abc123"

        # Track 1B: normalize_manifest strips the top-level "files" key on
        # all paths (v1 promotion and v2 passthrough). The payload moves
        # into sources.claude.files — nothing is lost.
        assert "files" not in loaded


class TestInitFlow:
    """Integration tests for mm init paths: first-device, second-device,
    wrong-passphrase, bootstrap race, and convergence.

    Uses CliRunner with monkeypatched CONFIG_PATH. Passphrases are provided
    via stdin input rather than MINDMELD_PASSPHRASE env var, because init
    needs to actually prompt to exercise the double/single-prompt branching.
    """

    def _setup_monkeypatch(self, tmp_path, monkeypatch):
        """Isolate CONFIG_PATH and disable keyring."""
        from pathlib import Path as _P

        cfg_path = tmp_path / "config_test.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", cfg_path)
        # Make keyring a no-op so tests don't pollute the real Keychain.
        monkeypatch.setattr(
            "mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False
        )
        # get_passphrase falls back to env; tests set MINDMELD_PASSPHRASE as needed.
        return cfg_path

    def test_first_device_init_bootstraps(self, tmp_path, monkeypatch):
        """Fresh storage: init prompts twice, bootstraps mm-crypto-init."""
        from mind_meld.storage.local import LocalBackend

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        # Inputs: storage path, device name, passphrase, confirm passphrase.
        # If ~/.gstack exists (it does in a dev env), there's also a y/n prompt
        # for gstack sync — answer "n" to keep the test minimal.
        stdin = f"{storage}\nMac A\npw123\npw123\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        assert "bootstrapped" in result.output

        # mm-crypto-init exists at storage root.
        backend = LocalBackend(storage)
        assert backend.exists("mm-crypto-init")

        # Config has crypto.root_salt_fp populated.
        import tomllib
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        assert "root_salt_fp" in cfg["crypto"]
        assert cfg["crypto"]["argon2_memory_kb"] == 65_536

    def test_first_device_passphrase_mismatch_aborts(self, tmp_path, monkeypatch):
        """Passphrases don't match → abort, no state written."""
        from mind_meld.storage.local import LocalBackend

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        stdin = f"{storage}\nMac A\npw123\npw456\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0
        assert "don't match" in result.output or "don" in result.output

        # Storage was created (mkdir), but mm-crypto-init was NOT written.
        backend = LocalBackend(storage)
        assert not backend.exists("mm-crypto-init")
        # Config was NOT written.
        assert not cfg_path.exists()

    def test_second_device_init_verifies(self, tmp_path, monkeypatch):
        """Existing mm-crypto-init: init prompts once, verifies keycheck."""
        from mind_meld.storage.local import LocalBackend

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()

        # Pre-seed storage with mm-crypto-init bootstrapped at MEMORY_KB.
        backend = LocalBackend(storage)
        # Use reduced memory_kb for test speed; bootstrap writes it into the blob.
        bootstrap_crypto_init(backend, "pw-shared", argon2_memory_kb=MEMORY_KB)

        # Second-device init: only 1 passphrase prompt (single, no confirm).
        stdin = f"{storage}\nMac B\npw-shared\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        assert "Verified passphrase against existing mm-crypto-init" in result.output

        # Config's memory_kb comes from storage, not from 65_536 default.
        import tomllib
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        assert cfg["crypto"]["argon2_memory_kb"] == MEMORY_KB

    def test_second_device_wrong_passphrase_aborts_cleanly(self, tmp_path, monkeypatch):
        """Wrong passphrase on second-device: abort, NO config or device registered."""
        from mind_meld.storage.local import LocalBackend

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()

        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, "correct-pw", argon2_memory_kb=MEMORY_KB)

        # Second device uses WRONG passphrase.
        stdin = f"{storage}\nMac B\nwrong-pw\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0
        assert "does not match" in result.output

        # No local state should have been written.
        assert not cfg_path.exists()
        # Storage devices/ should NOT have Mac B registered.
        devices_dir = storage / "devices"
        if devices_dir.exists():
            # Only the one device from bootstrap_crypto_init helper (which doesn't
            # register a device) — so there should be zero entries actually.
            assert list(devices_dir.iterdir()) == []

    def test_convergence_cross_device_via_conflict_copy(self, tmp_path, monkeypatch):
        """Simulate post-iCloud-reconciliation state: canonical + 'mm-crypto-init 2'.

        fetch_crypto_init picks the deterministic winner and canonicalizes.
        Subsequent commands interoperate as if only one init had happened.
        """
        from mind_meld.storage.local import LocalBackend

        self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)

        # Device A bootstraps with one passphrase.
        bootstrap_crypto_init(backend, "shared-pw", argon2_memory_kb=MEMORY_KB)
        # Simulate device B losing the race: its mm-crypto-init landed as
        # "mm-crypto-init 2". Build that blob manually.
        from mind_meld import crypto as _crypto

        _crypto.clear_crypto_session()
        other_salt = bytes([0xFF] * 16)
        _crypto.set_crypto_session(other_salt, MEMORY_KB)
        other_mk = _crypto.load_master_key("shared-pw", other_salt, MEMORY_KB)
        other_keycheck = _crypto._encrypt_with_master_key(
            _crypto._KEYCHECK_PLAINTEXT, other_mk
        )
        other_blob = (
            bytes([_crypto.FORMAT_VERSION])
            + MEMORY_KB.to_bytes(4, "big")
            + other_salt
            + other_keycheck
        )
        (storage / "mm-crypto-init 2").write_bytes(other_blob)

        # Now fetch_crypto_init is called (as if we just started a command).
        # Lex-smallest salt wins. Our canonical salt is random; other_salt is 0xFF*16.
        fetched = _crypto.fetch_crypto_init(backend)
        assert fetched.status == "ok"
        # Conflict copy is gone after canonicalization.
        assert not (storage / "mm-crypto-init 2").exists()
        # Canonical now holds the winner (whichever of the two had the smaller salt).
        canonical_bytes = (storage / "mm-crypto-init").read_bytes()
        winner_salt_in_canonical = canonical_bytes[5:21]
        assert winner_salt_in_canonical == fetched.root_salt


class TestInitTwoTierGuard:
    """Group 2 Pre-flight 3: storage-occupancy-based re-init guard.

    Two tiers:
      * ORPHAN — mm-crypto-init ok + any occupancy: typer.confirm warn.
      * BRICK — mm-crypto-init missing + blobs/manifests: require typed BRICK.
    """

    def _setup(self, tmp_path, monkeypatch):
        from pathlib import Path as _P
        cfg = tmp_path / "config_test.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr(
            "mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False
        )
        return cfg

    def test_orphan_case_warns_and_confirms(self, tmp_path, monkeypatch):
        """Existing mm-crypto-init + existing blob + we answer 'y' to orphan
        prompt → init proceeds on the second-device path."""
        from mind_meld.storage.local import LocalBackend

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, "pw-shared", argon2_memory_kb=MEMORY_KB)
        # Seed a blob so occupancy.has_any_blobs is True.
        backend.put("data/oldpeer/decafbad.enc", b"stub-blob")

        # Inputs: storage path, orphan-confirm y, device name, passphrase, gstack n
        stdin = f"{storage}\ny\nMac B\npw-shared\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        # existing_device_id is None (no prior config in this test), so the
        # orphan prompt takes the "alongside existing devices" form.
        assert "alongside the existing devices" in result.output
        # Second-device verify completed.
        assert "Verified passphrase against existing mm-crypto-init" in result.output

    def test_orphan_case_abort_on_n_leaves_state_clean(self, tmp_path, monkeypatch):
        from mind_meld.storage.local import LocalBackend

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, "pw-shared", argon2_memory_kb=MEMORY_KB)
        backend.put("data/oldpeer/decafbad.enc", b"stub-blob")

        # Answer 'n' to the orphan prompt.
        stdin = f"{storage}\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0  # typer.Exit() without code is 0
        # Config not written.
        assert not cfg.exists()

    def test_brick_case_refuses_without_exact_typed_token(self, tmp_path, monkeypatch):
        """mm-crypto-init missing + blobs exist + user types wrong token."""
        from mind_meld.storage.local import LocalBackend

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        # Seed blobs but NO mm-crypto-init (simulating deletion / iCloud loss).
        backend.put("data/peer/cafebabe.enc", b"stub-blob")

        # Inputs: storage path, then WRONG token for BRICK.
        stdin = f"{storage}\nwhatever\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0, result.output
        assert "DANGER" in (result.stderr or "") + result.output
        # Config not written.
        assert not cfg.exists()
        # mm-crypto-init still not created.
        assert not backend.exists("mm-crypto-init")

    def test_brick_case_rejects_lowercase_brick(self, tmp_path, monkeypatch):
        """Case-sensitive match: 'brick' is NOT accepted."""
        from mind_meld.storage.local import LocalBackend

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        backend.put("manifests/peer/manifest.json.enc", b"stub-manifest")

        stdin = f"{storage}\nbrick\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0
        assert not cfg.exists()
        assert not backend.exists("mm-crypto-init")

    def test_brick_case_accepts_exact_BRICK(self, tmp_path, monkeypatch):
        """Exact typed 'BRICK' proceeds to first-device bootstrap path."""
        from mind_meld.storage.local import LocalBackend

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        # Seed a manifest (no mm-crypto-init — our target scenario).
        backend.put("manifests/peer/manifest.json.enc", b"stub-manifest")

        # After BRICK, init continues on the first-device path:
        # device name, passphrase, confirm passphrase, gstack n.
        stdin = f"{storage}\nBRICK\nMac A\npw-new\npw-new\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        # New mm-crypto-init bootstrapped.
        assert backend.exists("mm-crypto-init")

    def test_first_device_path_not_gated_on_empty_storage(self, tmp_path, monkeypatch):
        """Empty storage: no guard triggers, first-device path works normally.

        Regression guard: the two-tier logic must not fire on fresh init.
        """
        from mind_meld.storage.local import LocalBackend

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        stdin = f"{storage}\nMac A\npw123\npw123\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        # No orphan or BRICK output polluted the happy path.
        assert "orphaning" not in result.output
        assert "DANGER" not in (result.stderr or "") + result.output

    def test_devices_only_occupancy_triggers_orphan_not_brick(
        self, tmp_path, monkeypatch
    ):
        """If only devices/ is populated (no blobs, no manifests, no
        mm-crypto-init), BRICK must NOT fire — no encrypted state is at risk.

        The guard should reach the orphan-case check, and since
        has_crypto_init is False, fall through to first-device path.
        """
        from mind_meld.storage.local import LocalBackend

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        # Seed ONLY a devices/ entry (no data/, no manifests/).
        import json as _json
        backend.put(
            "devices/stale.json",
            _json.dumps({"device_id": "stale", "device_name": "stale-dev"}).encode(),
        )

        stdin = f"{storage}\nMac A\npw123\npw123\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        # BRICK did NOT fire (no typed token consumed from stdin).
        assert result.exit_code == 0, result.output
        assert "DANGER" not in (result.stderr or "") + result.output
