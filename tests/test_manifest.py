"""Tests for mind_meld.manifest — walking, hashing, diffing."""

import hashlib
from pathlib import Path
from typing import Any

import pytest

from mind_meld.errors import ManifestError
from mind_meld.manifest import (
    CONFLICT_PATTERN,
    DiffResult,
    _is_excluded,
    build_manifest_v2,
    deserialize_manifest,
    diff_files,
    hash_file,
    is_conflict_filename,
    load_manifest,
    normalize_manifest,
    read_and_hash,
    serialize_manifest,
    walk_claude_source,
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

    def test_excludes_sync_conflict_file(self):
        assert _is_excluded(
            "projects/-foo/memory/notes.sync-conflict-20260422-120000-abc12345.md"
        )

    def test_does_not_exclude_user_sync_conflict_log(self):
        # User file containing the infix without a timestamp is NOT excluded.
        assert not _is_excluded("projects/-foo/memory/notes.sync-conflict-log.md")


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


class TestWalkClaudeSource:
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
        files = walk_claude_source(claude)
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
        files = walk_claude_source(claude)
        paths = set(files.keys())
        assert "projects/-Users-kb-myapp/todos/tasks.json" in paths

    def test_excludes_patterns(self, tmp_path):
        claude = self._setup_claude_dir(tmp_path)
        files = walk_claude_source(claude)
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
        files = walk_claude_source(claude, max_file_size=100, on_skip=lambda p, r: skipped.append((p, r)))
        assert len(files) == 1
        assert len(skipped) == 1
        assert "big.bin" in skipped[0][0]

    def test_empty_projects_dir(self, tmp_path):
        claude = tmp_path / ".claude"
        (claude / "projects").mkdir(parents=True)
        files = walk_claude_source(claude)
        assert files == {}

    def test_no_projects_dir(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        files = walk_claude_source(claude)
        assert files == {}

    def test_file_info_structure(self, tmp_path):
        claude = self._setup_claude_dir(tmp_path)
        files = walk_claude_source(claude)
        for info in files.values():
            assert "sha256" in info
            assert "size" in info
            assert "mtime" in info
            assert len(info["sha256"]) == 64


class TestDiffFiles:
    def test_all_new(self):
        local = {"a.json": {"sha256": "aaa"}, "b.json": {"sha256": "bbb"}}
        diff = diff_files(local)
        assert len(diff.new) == 2
        assert len(diff.modified) == 0
        assert len(diff.deleted) == 0

    def test_all_new_explicit_none(self):
        local = {"a.json": {"sha256": "aaa"}}
        diff = diff_files(local, None)
        assert len(diff.new) == 1

    def test_no_changes(self):
        files = {"a.json": {"sha256": "aaa"}}
        diff = diff_files(files, files)
        assert not diff.has_changes
        assert len(diff.unchanged) == 1

    def test_modified(self):
        local = {"a.json": {"sha256": "new-hash"}}
        remote = {"a.json": {"sha256": "old-hash"}}
        diff = diff_files(local, remote)
        assert len(diff.modified) == 1
        assert "a.json" in diff.modified

    def test_deleted(self):
        local = {}
        remote = {"a.json": {"sha256": "aaa"}}
        diff = diff_files(local, remote)
        assert len(diff.deleted) == 1
        assert "a.json" in diff.deleted

    def test_mixed(self):
        local = {
            "new.json": {"sha256": "nnn"},
            "modified.json": {"sha256": "new-m"},
            "same.json": {"sha256": "sss"},
        }
        remote = {
            "modified.json": {"sha256": "old-m"},
            "same.json": {"sha256": "sss"},
            "deleted.json": {"sha256": "ddd"},
        }
        diff = diff_files(local, remote)
        assert len(diff.new) == 1
        assert len(diff.modified) == 1
        assert len(diff.deleted) == 1
        assert len(diff.unchanged) == 1

    def test_repr_is_count_formatted(self):
        # Track 1B invariant: DiffResult.__repr__ must remain count-based,
        # not the dataclass default dict-dump. A 500-file manifest would
        # produce a 50KB wall of noise under the default repr.
        local = {"a.json": {"sha256": "aaa"}, "b.json": {"sha256": "bbb"}}
        remote = {"b.json": {"sha256": "bbb"}, "c.json": {"sha256": "ccc"}}
        diff = diff_files(local, remote)
        rep = repr(diff)
        assert rep == "DiffResult(new=1, modified=0, deleted=1, unchanged=1)"
        # No dict contents should leak into the repr.
        assert "sha256" not in rep
        assert "aaa" not in rep


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

    def test_produces_sources_key_only(self, tmp_path):
        # Track 1B: v2 writers no longer emit a redundant top-level "files"
        # mirror. Sources-only is the contract.
        claude = self._make_claude_dir(tmp_path)
        sources = [{"name": "claude", "path": str(claude), "type": "claude"}]
        m = build_manifest_v2("dev1", "Mac", sources)
        assert "files" not in m
        assert "sources" in m

    def test_sources_are_per_source(self, tmp_path):
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
        assert "files" not in m
        assert "claude" in m["sources"]
        assert "gstack" in m["sources"]
        # Claude source contains only synced-subdir paths.
        for key in m["sources"]["claude"]["files"]:
            assert key.startswith("projects/")

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
        assert len(m["sources"]["claude"]["files"]) == 1

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


class TestIsConflictFilename:
    def test_happy_path_with_extension(self):
        assert is_conflict_filename("notes.sync-conflict-20260422-120000-abc12345.md")

    def test_happy_path_extensionless(self):
        # conflict_filename() emits no trailing extension when canonical has none
        # (e.g., a top-level README). Pattern must still match.
        assert is_conflict_filename("README.sync-conflict-20260422-120000-abc12345")

    def test_false_positive_guard_no_digits(self):
        # User file that happens to contain ".sync-conflict-" but no timestamp.
        assert not is_conflict_filename("notes.sync-conflict-log.md")

    def test_false_positive_guard_word_after_infix(self):
        assert not is_conflict_filename("foo.sync-conflict-abc.md")

    def test_canonical_file_not_excluded(self):
        assert not is_conflict_filename("foo.md")

    def test_empty_string(self):
        assert is_conflict_filename("") is False

    def test_infix_alone_without_suffix(self):
        # ".sync-conflict-" with nothing after
        assert not is_conflict_filename("foo.sync-conflict-")

    def test_false_positive_guard_short_digits(self):
        # Cross-model adversarial catch: previous loose pattern matched
        # `notes.sync-conflict-2024-summary.md` (only 4 digits before dash).
        # Strict 8+6-digit timestamp pattern eliminates this class.
        assert not is_conflict_filename("notes.sync-conflict-2024-summary.md")
        assert not is_conflict_filename("notes.sync-conflict-1-a.md")

    def test_pattern_constant_matches_documented_format(self):
        # Lock the pattern so accidental edits to CONFLICT_PATTERN that drop
        # the strict timestamp anchor fail loudly.
        assert CONFLICT_PATTERN == (
            "*.sync-conflict-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-"
            "[0-9][0-9][0-9][0-9][0-9][0-9]-*"
        )


class TestWalkerExcludesConflictFiles:
    """Walker MUST exclude Syncthing-style conflict copies in synced subdirs.

    Regression: v0.4.0 shipped conflict-copy creation but missed the walker
    exclusion. Result: next push uploaded the conflict file fleet-wide, with
    other devices receiving it as a regular source file.
    """

    def test_claude_source_skips_conflict_in_memory(self, tmp_path):
        claude = tmp_path / ".claude"
        memory = claude / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "notes.md").write_text("canonical")
        (memory / "notes.sync-conflict-20260422-120000-abc12345.md").write_text(
            "local divergent"
        )

        files = walk_claude_source(claude)
        paths = set(files.keys())
        assert "projects/-Users-kb-myapp/memory/notes.md" in paths
        # The conflict copy must NOT propagate.
        assert all(".sync-conflict-" not in p for p in paths)

    def test_claude_source_skips_conflict_in_todos(self, tmp_path):
        claude = tmp_path / ".claude"
        todos = claude / "projects" / "-Users-kb-myapp" / "todos"
        todos.mkdir(parents=True)
        (todos / "tasks.json").write_text("[]")
        (todos / "tasks.sync-conflict-20260422-120000-abc12345.json").write_text(
            "[1, 2]"
        )

        files = walk_claude_source(claude)
        paths = set(files.keys())
        assert "projects/-Users-kb-myapp/todos/tasks.json" in paths
        assert all(".sync-conflict-" not in p for p in paths)

    def test_claude_source_does_not_exclude_user_false_positive(self, tmp_path):
        claude = tmp_path / ".claude"
        memory = claude / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        # User-named file containing the infix but no timestamp digits.
        (memory / "notes.sync-conflict-log.md").write_text("legitimate user file")

        files = walk_claude_source(claude)
        assert "projects/-Users-kb-myapp/memory/notes.sync-conflict-log.md" in files

    def test_generic_source_skips_conflict_files(self, tmp_path):
        # Same exclusion contract MUST hold for the gstack/generic source path
        # (regression guard if EXCLUDED handling ever forks between walkers).
        base = tmp_path / "generic_source"
        memory = base / "memory"
        memory.mkdir(parents=True)
        (memory / "good.md").write_text("ok")
        (memory / "good.sync-conflict-20260422-120000-abc12345.md").write_text(
            "should not propagate"
        )
        config = {
            "name": "gstack",
            "path": str(base),
            "type": "generic",
            "include_dirs": ["memory"],
            "include_files": [],
        }

        files = walk_generic_source(config)
        paths = set(files.keys())
        assert "memory/good.md" in paths
        assert all(".sync-conflict-" not in p for p in paths)


class TestNormalizeManifestTombstoneMigration:
    """Bare-key tombstone migration MUST fire only at v1→v2 promotion.

    For manifests already shaped as v2, unknown bare keys are preserved
    verbatim — we don't speculate on adversarial/external data. `is_tombstoned`
    returning False is the safe default for adversarial keys.
    """

    def test_v1_promotion_migrates_bare_tombstone_to_claude(self):
        m = {
            "device_id": "a",
            "files": {},
            "tombstones": {
                "memory/foo.md": {"deleted_at": "2026-04-22T10:00:00+00:00", "device_id": "a"},
            },
        }
        out = normalize_manifest(m)
        assert "claude:memory/foo.md" in out["tombstones"]
        assert "memory/foo.md" not in out["tombstones"]
        assert out["tombstones"]["claude:memory/foo.md"]["device_id"] == "a"

    def test_v1_promotion_leaves_already_normalized_keys_alone(self):
        m = {
            "device_id": "a",
            "files": {},
            "tombstones": {
                "claude:memory/already.md": {"deleted_at": "2026-04-22T10:00:00+00:00", "device_id": "a"},
            },
        }
        out = normalize_manifest(m)
        assert "claude:memory/already.md" in out["tombstones"]
        # No double-prefix.
        assert "claude:claude:memory/already.md" not in out["tombstones"]

    def test_v1_promotion_with_empty_tombstones_is_noop(self):
        m = {"device_id": "a", "files": {}, "tombstones": {}}
        out = normalize_manifest(m)
        assert out["tombstones"] == {}

    def test_v1_promotion_with_missing_tombstones_synthesizes_empty(self):
        m = {"device_id": "a", "files": {}}
        out = normalize_manifest(m)
        assert out["tombstones"] == {}

    def test_v2_manifest_with_bare_keys_preserves_them(self):
        # Manifest has explicit `sources` → not a v1 promotion. We do NOT
        # migrate ambiguous keys here. is_tombstoned will return False for
        # this bare key under any source — safe default.
        m = {
            "device_id": "a",
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {
                "memory/bare.md": {"deleted_at": "2026-04-22T10:00:00+00:00", "device_id": "a"},
            },
        }
        out = normalize_manifest(m)
        assert "memory/bare.md" in out["tombstones"]
        assert "claude:memory/bare.md" not in out["tombstones"]

    def test_v1_promotion_tolerates_malformed_value(self):
        # A non-dict value (string, None, integer) must not crash the loop;
        # entry passes through with the new key.
        m = {
            "device_id": "a",
            "files": {},
            "tombstones": {
                "memory/foo.md": "not-a-dict",
                "memory/bar.md": None,
            },
        }
        out = normalize_manifest(m)
        # Migration tolerates the malformed values; downstream `is_tombstoned`
        # only checks key presence so the entries are still effective markers.
        assert "claude:memory/foo.md" in out["tombstones"]
        assert "claude:memory/bar.md" in out["tombstones"]

    def test_normalize_is_idempotent_on_v1_input(self):
        m = {
            "device_id": "a",
            "files": {},
            "tombstones": {
                "memory/foo.md": {"deleted_at": "2026-04-22T10:00:00+00:00", "device_id": "a"},
            },
        }
        once = normalize_manifest(dict(m))
        twice = normalize_manifest(dict(once))
        assert once == twice


class TestLoadManifest:
    def test_loads_v2_manifest(self):
        m = {
            "device_id": "a",
            "device_name": "laptop",
            "timestamp": "2026-04-22T10:00:00+00:00",
            "files": {},
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {},
        }
        loaded = load_manifest(serialize_manifest(m))
        assert loaded["device_id"] == "a"
        assert isinstance(loaded["sources"], dict)
        assert isinstance(loaded["tombstones"], dict)

    def test_promotes_v1_with_tombstone_migration(self):
        m = {
            "device_id": "a",
            "files": {"memory/x.md": {"sha256": "x", "size": 1, "mtime": "2026-04-22T10:00:00+00:00"}},
            "tombstones": {
                "memory/deleted.md": {"deleted_at": "2026-04-22T10:00:00+00:00", "device_id": "a"},
            },
        }
        loaded = load_manifest(serialize_manifest(m))
        assert "claude" in loaded["sources"]
        assert "claude:memory/deleted.md" in loaded["tombstones"]

    def test_raises_on_bad_json(self):
        with pytest.raises(ManifestError):
            load_manifest(b"{not valid json")

    def test_raises_on_empty_bytes(self):
        with pytest.raises(ManifestError):
            load_manifest(b"")

    def test_raises_on_non_dict_top_level(self):
        with pytest.raises(ManifestError):
            load_manifest(b'["array", "not", "object"]')

    def test_round_trip_preserves_keys(self):
        original = {
            "device_id": "z",
            "sources": {"claude": {"base_path": "/p", "files": {"a.md": {"sha256": "h", "size": 1, "mtime": "t"}}}},
            "tombstones": {"claude:b.md": {"deleted_at": "t", "device_id": "z"}},
        }
        loaded = load_manifest(serialize_manifest(original))
        assert loaded["sources"] == original["sources"]
        assert loaded["tombstones"] == original["tombstones"]

    def test_rejects_non_dict_sources(self):
        # Hardening: enforce the load-boundary contract. A tampered or
        # bit-corrupted peer manifest with `{"sources": "x"}` must fail
        # at the front door instead of crashing downstream consumers.
        with pytest.raises(ManifestError, match="sources"):
            load_manifest(b'{"device_id":"a","sources":"x","tombstones":{}}')

    def test_rejects_non_dict_tombstones(self):
        with pytest.raises(ManifestError, match="tombstones"):
            load_manifest(
                b'{"device_id":"a","sources":{"claude":{"base_path":"","files":{}}},"tombstones":[]}'
            )

    def test_rejects_v1_with_non_dict_tombstones(self):
        # v1 promotion path must not crash either. load_manifest catches.
        with pytest.raises(ManifestError, match="tombstones"):
            load_manifest(b'{"device_id":"a","files":{},"tombstones":"not-a-dict"}')

    def test_rejects_non_dict_source_value(self):
        # Cross-model adversarial catch: inner shape was not validated.
        # `{"sources": {"claude": "x"}}` would survive load and crash deep
        # in _merge_manifests / generate_tombstones with AttributeError.
        with pytest.raises(ManifestError, match=r"sources\['claude'\]"):
            load_manifest(
                b'{"device_id":"a","sources":{"claude":"not-a-dict"},"tombstones":{}}'
            )

    def test_rejects_non_dict_source_files(self):
        with pytest.raises(ManifestError, match=r"\['files'\]"):
            load_manifest(
                b'{"device_id":"a","sources":{"claude":{"base_path":"","files":"x"}},"tombstones":{}}'
            )

    def test_rejects_non_dict_tombstone_value(self):
        with pytest.raises(ManifestError, match=r"tombstones\['claude:a.md'\]"):
            load_manifest(
                b'{"device_id":"a","sources":{"claude":{"base_path":"","files":{}}},"tombstones":{"claude:a.md":"x"}}'
            )
