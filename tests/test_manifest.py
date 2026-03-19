"""Tests for memsync.manifest — walking, hashing, diffing."""

from pathlib import Path

import pytest

from memsync.manifest import (
    DiffResult,
    _is_excluded,
    build_manifest,
    deserialize_manifest,
    diff_manifests,
    hash_file,
    serialize_manifest,
    walk_directory,
)


class TestIsExcluded:
    def test_excludes_node_modules(self):
        assert _is_excluded("projects/-foo/node_modules/package.json")

    def test_excludes_git(self):
        assert _is_excluded("projects/-foo/.git/HEAD")

    def test_excludes_ds_store(self):
        assert _is_excluded("projects/-foo/.DS_Store")

    def test_excludes_env(self):
        assert _is_excluded("projects/-foo/.env")

    def test_excludes_env_local(self):
        assert _is_excluded("projects/-foo/.env.local")

    def test_excludes_log(self):
        assert _is_excluded("projects/-foo/debug.log")

    def test_excludes_pyc(self):
        assert _is_excluded("projects/-foo/module.pyc")

    def test_excludes_pycache(self):
        assert _is_excluded("projects/-foo/__pycache__/module.cpython-311.pyc")

    def test_excludes_memsync_log(self):
        assert _is_excluded("projects/-foo/.memsync-log.md")

    def test_allows_normal_files(self):
        assert not _is_excluded("projects/-foo/memory/user_role.md")

    def test_allows_nested(self):
        assert not _is_excluded("projects/-foo/todos/tasks.json")


class TestHashFile:
    def test_hash_known_content(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h = hash_file(f)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("same content")
        f2.write_text("same content")
        assert hash_file(f1) == hash_file(f2)

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("content a")
        f2.write_text("content b")
        assert hash_file(f1) != hash_file(f2)


class TestWalkDirectory:
    def _setup_claude_dir(self, tmp_path: Path) -> Path:
        """Create a mock ~/.claude structure."""
        claude = tmp_path / ".claude"
        projects = claude / "projects" / "-Users-kb-myapp"
        memory = projects / "memory"
        memory.mkdir(parents=True)
        (memory / "user_role.md").write_text("---\nname: role\n---\nData scientist")
        (memory / "feedback.md").write_text("---\nname: feedback\n---\nNo mocks")
        # Excluded files
        (projects / ".DS_Store").write_text("")
        (projects / "debug.log").write_text("log data")
        (projects / "node_modules").mkdir()
        (projects / "node_modules" / "pkg.json").write_text("{}")
        # Sessions dir — should NOT be synced
        sessions = projects / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "transcript.json").write_text('{"huge": "data"}')
        return claude

    def test_walks_memory_and_todos_only(self, tmp_path):
        claude = self._setup_claude_dir(tmp_path)
        files = walk_directory(claude)
        assert len(files) == 2
        paths = set(files.keys())
        assert "projects/-Users-kb-myapp/memory/user_role.md" in paths
        assert "projects/-Users-kb-myapp/memory/feedback.md" in paths
        # Sessions should NOT be included
        for p in paths:
            assert "sessions" not in p

    def test_syncs_todos_subdir(self, tmp_path):
        claude = self._setup_claude_dir(tmp_path)
        todos = claude / "projects" / "-Users-kb-myapp" / "todos"
        todos.mkdir(parents=True)
        (todos / "tasks.json").write_text('{"task": "do stuff"}')
        files = walk_directory(claude)
        paths = set(files.keys())
        assert "projects/-Users-kb-myapp/todos/tasks.json" in paths

    def test_excludes_patterns(self, tmp_path):
        claude = self._setup_claude_dir(tmp_path)
        files = walk_directory(claude)
        paths = set(files.keys())
        for p in paths:
            assert ".DS_Store" not in p
            assert "node_modules" not in p
            assert ".log" not in p

    def test_skips_large_files(self, tmp_path):
        claude = tmp_path / ".claude"
        memory = claude / "projects" / "-foo" / "memory"
        memory.mkdir(parents=True)
        (memory / "small.md").write_text("small")
        (memory / "big.bin").write_bytes(b"x" * 200)

        skipped = []
        files = walk_directory(claude, max_file_size=100, on_skip=lambda p, r: skipped.append((p, r)))
        assert len(files) == 1
        assert len(skipped) == 1
        assert "big.bin" in skipped[0][0]

    def test_empty_projects_dir(self, tmp_path):
        claude = tmp_path / ".claude"
        (claude / "projects").mkdir(parents=True)
        files = walk_directory(claude)
        assert files == {}

    def test_no_projects_dir(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        files = walk_directory(claude)
        assert files == {}

    def test_file_info_structure(self, tmp_path):
        claude = self._setup_claude_dir(tmp_path)
        files = walk_directory(claude)
        for info in files.values():
            assert "sha256" in info
            assert "size" in info
            assert "mtime" in info
            assert len(info["sha256"]) == 64


class TestDiffManifests:
    def test_all_new(self):
        local = {"files": {"a.json": {"sha256": "aaa"}, "b.json": {"sha256": "bbb"}}}
        diff = diff_manifests(local, None)
        assert len(diff.new) == 2
        assert len(diff.modified) == 0
        assert len(diff.deleted) == 0

    def test_no_changes(self):
        files = {"a.json": {"sha256": "aaa"}}
        diff = diff_manifests({"files": files}, {"files": files})
        assert not diff.has_changes
        assert len(diff.unchanged) == 1

    def test_modified(self):
        local = {"files": {"a.json": {"sha256": "new-hash"}}}
        remote = {"files": {"a.json": {"sha256": "old-hash"}}}
        diff = diff_manifests(local, remote)
        assert len(diff.modified) == 1
        assert "a.json" in diff.modified

    def test_deleted(self):
        local = {"files": {}}
        remote = {"files": {"a.json": {"sha256": "aaa"}}}
        diff = diff_manifests(local, remote)
        assert len(diff.deleted) == 1
        assert "a.json" in diff.deleted

    def test_mixed(self):
        local = {"files": {
            "new.json": {"sha256": "nnn"},
            "modified.json": {"sha256": "new-m"},
            "same.json": {"sha256": "sss"},
        }}
        remote = {"files": {
            "modified.json": {"sha256": "old-m"},
            "same.json": {"sha256": "sss"},
            "deleted.json": {"sha256": "ddd"},
        }}
        diff = diff_manifests(local, remote)
        assert len(diff.new) == 1
        assert len(diff.modified) == 1
        assert len(diff.deleted) == 1
        assert len(diff.unchanged) == 1


class TestSerialize:
    def test_round_trip(self):
        manifest = {
            "device_id": "abc",
            "files": {"a.json": {"sha256": "xxx", "size": 100}},
        }
        data = serialize_manifest(manifest)
        result = deserialize_manifest(data)
        assert result["device_id"] == "abc"
        assert result["files"]["a.json"]["sha256"] == "xxx"

    def test_invalid_json_raises(self):
        from memsync.errors import ManifestError
        with pytest.raises(ManifestError):
            deserialize_manifest(b"not json")
