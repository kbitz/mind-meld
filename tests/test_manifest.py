"""Tests for mind_meld.manifest — walking, hashing, diffing."""

import hashlib
from pathlib import Path
from typing import Any

import pytest

from mind_meld.errors import ManifestError
from mind_meld.manifest import (
    DiffResult,
    _is_excluded,
    build_manifest,
    build_manifest_v2,
    deserialize_manifest,
    diff_manifests,
    hash_file,
    normalize_manifest,
    read_and_hash,
    serialize_manifest,
    walk_directory,
    walk_generic_source,
    walk_source,
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

    def test_excludes_mind_meld_log(self):
        assert _is_excluded("projects/-foo/.mind-meld-log.md")

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
        from mind_meld.errors import ManifestError
        with pytest.raises(ManifestError):
            deserialize_manifest(b"not json")


class TestReadAndHash:
    def test_returns_consistent_bytes_and_sha256(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        data, digest = read_and_hash(f)
        assert data == b"hello world"
        assert len(digest) == 64
        assert isinstance(digest, str)

    def test_sha256_matches_hashlib(self, tmp_path):
        content = b"deterministic content for hashing"
        f = tmp_path / "check.bin"
        f.write_bytes(content)
        data, digest = read_and_hash(f)
        expected = hashlib.sha256(content).hexdigest()
        assert data == content
        assert digest == expected

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        data, digest = read_and_hash(f)
        assert data == b""
        assert digest == hashlib.sha256(b"").hexdigest()


class TestWalkGenericSource:
    def _make_source(self, tmp_path: Path) -> tuple[Path, dict[str, Any]]:
        """Create a generic source directory with include_dirs and include_files."""
        base = tmp_path / "generic_source"
        base.mkdir()
        # include_dirs: projects/
        projects = base / "projects"
        projects.mkdir()
        sub = projects / "myproject"
        sub.mkdir()
        (sub / "data.yaml").write_text("key: value")
        (projects / "top.txt").write_text("top-level in projects")
        # include_files at root
        (base / "config.yaml").write_text("setting: true")
        # A dir not in include_dirs
        other = base / "other"
        other.mkdir()
        (other / "secret.txt").write_text("should not appear")
        # A root-level file not in include_files
        (base / "random.txt").write_text("should not appear")

        config = {
            "name": "test_generic",
            "path": str(base),
            "type": "generic",
            "include_dirs": ["projects"],
            "include_files": ["config.yaml"],
        }
        return base, config

    def test_walks_include_dirs_recursively(self, tmp_path):
        base, config = self._make_source(tmp_path)
        files = walk_generic_source(config)
        paths = set(files.keys())
        assert "projects/myproject/data.yaml" in paths
        assert "projects/top.txt" in paths

    def test_picks_up_include_files_at_root(self, tmp_path):
        base, config = self._make_source(tmp_path)
        files = walk_generic_source(config)
        assert "config.yaml" in files

    def test_ignores_dirs_not_in_include_dirs(self, tmp_path):
        base, config = self._make_source(tmp_path)
        files = walk_generic_source(config)
        paths = set(files.keys())
        for p in paths:
            assert not p.startswith("other/")

    def test_ignores_root_files_not_in_include_files(self, tmp_path):
        base, config = self._make_source(tmp_path)
        files = walk_generic_source(config)
        assert "random.txt" not in files

    def test_applies_excluded_patterns(self, tmp_path):
        base = tmp_path / "src"
        base.mkdir()
        projects = base / "projects"
        projects.mkdir()
        (projects / "good.txt").write_text("ok")
        (projects / ".DS_Store").write_text("junk")

        config = {
            "path": str(base),
            "include_dirs": ["projects"],
            "include_files": [],
        }
        files = walk_generic_source(config)
        paths = set(files.keys())
        assert "projects/good.txt" in paths
        assert "projects/.DS_Store" not in paths

    def test_returns_empty_when_source_dir_missing(self, tmp_path):
        config = {
            "path": str(tmp_path / "nonexistent"),
            "include_dirs": ["projects"],
            "include_files": ["config.yaml"],
        }
        files = walk_generic_source(config)
        assert files == {}

    def test_respects_max_file_size(self, tmp_path):
        base = tmp_path / "src"
        base.mkdir()
        data_dir = base / "data"
        data_dir.mkdir()
        (data_dir / "small.txt").write_text("small")
        (data_dir / "big.bin").write_bytes(b"x" * 500)

        config = {
            "path": str(base),
            "include_dirs": ["data"],
            "include_files": [],
        }
        skipped: list[tuple[str, str]] = []
        files = walk_generic_source(config, max_file_size=100, on_skip=lambda p, r: skipped.append((p, r)))
        assert "data/small.txt" in files
        assert "data/big.bin" not in files
        assert len(skipped) == 1
        assert "big.bin" in skipped[0][0]

    def test_nested_subdirs(self, tmp_path):
        """Walks include_dirs recursively through multiple levels."""
        base = tmp_path / "src"
        base.mkdir()
        deep = base / "projects" / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_text("deep content")

        config = {
            "path": str(base),
            "include_dirs": ["projects"],
            "include_files": [],
        }
        files = walk_generic_source(config)
        assert "projects/a/b/c/deep.txt" in files


class TestWalkSource:
    def test_claude_type_dispatches_to_walk_claude_source(self, tmp_path):
        claude = tmp_path / ".claude"
        memory = claude / "projects" / "-foo" / "memory"
        memory.mkdir(parents=True)
        (memory / "note.md").write_text("hello")

        config = {"name": "claude", "path": str(claude), "type": "claude"}
        base_path, files = walk_source(config)
        assert "projects/-foo/memory/note.md" in files
        assert base_path == str(claude.resolve())

    def test_generic_type_dispatches_to_walk_generic_source(self, tmp_path):
        base = tmp_path / "generic"
        data = base / "data"
        data.mkdir(parents=True)
        (data / "file.txt").write_text("content")
        (base / "config.yaml").write_text("key: val")

        config = {
            "name": "test",
            "path": str(base),
            "type": "generic",
            "include_dirs": ["data"],
            "include_files": ["config.yaml"],
        }
        base_path, files = walk_source(config)
        assert "data/file.txt" in files
        assert "config.yaml" in files
        assert base_path == str(base.resolve())

    def test_unknown_type_raises_manifest_error(self, tmp_path):
        base = tmp_path / "src"
        base.mkdir()
        config = {"name": "bad", "path": str(base), "type": "unknown_type"}
        with pytest.raises(ManifestError, match="unknown source type"):
            walk_source(config)


class TestBuildManifestV2:
    def _make_claude_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / ".claude"
        memory = d / "projects" / "-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")
        return d

    def _make_generic_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / ".gstack"
        projects = d / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (d / "config.yaml").write_text("version: 1")
        return d

    def test_produces_files_and_sources_keys(self, tmp_path):
        claude = self._make_claude_dir(tmp_path)
        sources = [{"name": "claude", "path": str(claude), "type": "claude"}]
        m = build_manifest_v2("dev1", "Mac", sources)
        assert "files" in m
        assert "sources" in m

    def test_files_contains_only_claude_files(self, tmp_path):
        claude = self._make_claude_dir(tmp_path)
        gstack = self._make_generic_dir(tmp_path)
        sources = [
            {"name": "claude", "path": str(claude), "type": "claude"},
            {
                "name": "gstack",
                "path": str(gstack),
                "type": "generic",
                "include_dirs": ["projects"],
                "include_files": ["config.yaml"],
            },
        ]
        m = build_manifest_v2("dev1", "Mac", sources)
        # "files" should only have claude source files
        for key in m["files"]:
            assert key.startswith("projects/")
        # "sources" should have both
        assert "claude" in m["sources"]
        assert "gstack" in m["sources"]

    def test_sources_contain_base_path_and_files(self, tmp_path):
        claude = self._make_claude_dir(tmp_path)
        sources = [{"name": "claude", "path": str(claude), "type": "claude"}]
        m = build_manifest_v2("dev1", "Mac", sources)
        src = m["sources"]["claude"]
        assert "base_path" in src
        assert "files" in src
        assert isinstance(src["files"], dict)

    def test_single_source(self, tmp_path):
        claude = self._make_claude_dir(tmp_path)
        sources = [{"name": "claude", "path": str(claude), "type": "claude"}]
        m = build_manifest_v2("dev1", "Mac", sources)
        assert len(m["sources"]) == 1
        assert "claude" in m["sources"]
        assert len(m["files"]) == 1

    def test_multiple_sources(self, tmp_path):
        claude = self._make_claude_dir(tmp_path)
        gstack = self._make_generic_dir(tmp_path)
        sources = [
            {"name": "claude", "path": str(claude), "type": "claude"},
            {
                "name": "gstack",
                "path": str(gstack),
                "type": "generic",
                "include_dirs": ["projects"],
                "include_files": ["config.yaml"],
            },
        ]
        m = build_manifest_v2("dev1", "Mac", sources)
        assert len(m["sources"]) == 2
        assert "claude" in m["sources"]
        assert "gstack" in m["sources"]
        # gstack should have 2 files (projects/state.yaml + config.yaml)
        assert len(m["sources"]["gstack"]["files"]) == 2
        # claude should have 1 file
        assert len(m["sources"]["claude"]["files"]) == 1

    def test_manifest_has_device_info(self, tmp_path):
        claude = self._make_claude_dir(tmp_path)
        sources = [{"name": "claude", "path": str(claude), "type": "claude"}]
        m = build_manifest_v2("dev-x", "Laptop", sources)
        assert m["device_id"] == "dev-x"
        assert m["device_name"] == "Laptop"
        assert "timestamp" in m


class TestNormalizeManifest:
    def test_v2_manifest_passes_through(self):
        m = {
            "device_id": "a",
            "files": {"x.md": {"sha256": "aaa"}},
            "sources": {
                "claude": {
                    "base_path": "/home/.claude",
                    "files": {"x.md": {"sha256": "aaa"}},
                }
            },
        }
        result = normalize_manifest(m)
        assert result is m  # same object, not a copy
        assert result["sources"]["claude"]["base_path"] == "/home/.claude"

    def test_v1_manifest_wrapped_as_claude_source(self):
        m = {
            "device_id": "a",
            "base_path": "/home/.claude",
            "files": {
                "projects/-foo/memory/role.md": {"sha256": "aaa", "size": 10},
            },
        }
        result = normalize_manifest(m)
        assert "sources" in result
        assert "claude" in result["sources"]
        assert result["sources"]["claude"]["files"]["projects/-foo/memory/role.md"]["sha256"] == "aaa"

    def test_preserves_base_path_from_v1(self):
        m = {
            "device_id": "b",
            "base_path": "/custom/path/.claude",
            "files": {},
        }
        result = normalize_manifest(m)
        assert result["sources"]["claude"]["base_path"] == "/custom/path/.claude"

    def test_v1_without_base_path_defaults_to_empty(self):
        m = {"device_id": "c", "files": {"a.md": {"sha256": "x"}}}
        result = normalize_manifest(m)
        assert result["sources"]["claude"]["base_path"] == ""
