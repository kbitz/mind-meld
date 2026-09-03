"""Tests for mind_meld.manifest — walking, hashing, diffing."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from mind_meld.errors import ManifestError
from mind_meld.manifest import (
    CONFLICT_PATTERN,
    MARKER_SKIP_NAME,
    TOMBSTONE_TTL_DAYS,
    _is_active_tombstone,
    _is_excluded,
    _record_file,
    build_manifest_v2,
    deserialize_manifest,
    diff_files,
    generate_tombstones,
    hash_file,
    is_conflict_filename,
    is_pre_inversion_conflict_filename,
    is_v1_conflict_filename,
    load_manifest,
    marker_skip_globs,
    normalize_manifest,
    parse_conflict_created_at,
    parse_conflict_device_short,
    read_and_hash,
    serialize_manifest,
    walk_claude_source,
    walk_generic_source,
    walk_grok_source,
    walk_source,
)


class TestIsExcluded:
    def test_excludes_node_modules(self):
        assert _is_excluded("projects/-foo/node_modules/package.json")

    def test_excludes_git(self):
        assert _is_excluded("projects/-foo/.git/HEAD")

    def test_excludes_extend_root_marker(self):
        assert _is_excluded("skills/roadmap/.extend-root")

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
        assert _is_excluded("projects/-foo/memory/notes.sync-conflict-20260422-120000-abc12345.md")

    def test_does_not_exclude_user_sync_conflict_log(self):
        # User file containing the infix without a timestamp is NOT excluded.
        assert not _is_excluded("projects/-foo/memory/notes.sync-conflict-log.md")


class TestIsExcludedWithPerSourcePatterns:
    """5C: per-source `exclude_patterns` extend the global EXCLUDED list.

    Matched against the FULL relative path (not just basename), so users
    can scope to subtrees like `projects/*/repo-mode.json`.
    """

    def test_per_source_glob_drops_path(self):
        assert _is_excluded(
            "projects/myapp/repo-mode.json",
            exclude_patterns=["projects/*/repo-mode.json"],
        )

    def test_per_source_glob_does_not_drop_non_match(self):
        assert not _is_excluded(
            "projects/myapp/role.md",
            exclude_patterns=["projects/*/repo-mode.json"],
        )

    def test_per_source_glob_path_scoped_not_basename(self):
        """A glob like `cache.json` matches basename only because fnmatch
        evaluates against the full relative path here. To scope to a
        subtree you spell it out: `**/cache.json` matches at any depth."""
        assert not _is_excluded(
            "projects/myapp/cache.json",
            exclude_patterns=["cache.json"],  # no path glob → only matches root
        )
        assert _is_excluded(
            "projects/myapp/cache.json",
            exclude_patterns=["**/cache.json"],
        )

    def test_global_excluded_still_applies_when_per_source_set(self):
        """exclude_patterns extends — does not replace — the EXCLUDED list."""
        assert _is_excluded(
            "projects/myapp/.env",
            exclude_patterns=["projects/*/repo-mode.json"],
        )

    def test_empty_exclude_patterns_is_no_op(self):
        assert not _is_excluded("projects/myapp/role.md", exclude_patterns=[])

    def test_none_exclude_patterns_is_no_op(self):
        assert not _is_excluded("projects/myapp/role.md", exclude_patterns=None)


class TestWalkerExcludePatterns:
    """5C: walker honors per-source exclude_patterns end-to-end.

    Generic walker + claude walker both route through `_record_file`,
    which threads exclude_patterns into `_is_excluded`.
    """

    def test_walk_generic_source_drops_excluded_paths(self, tmp_path):
        base = tmp_path / "gstack"
        projects = base / "projects" / "myapp"
        projects.mkdir(parents=True)
        (projects / "repo-mode.json").write_text("{}")
        (projects / "role.md").write_text("kept")

        files = walk_generic_source(
            {
                "name": "gstack",
                "path": str(base),
                "type": "generic",
                "include_dirs": ["projects"],
                "exclude_patterns": ["projects/*/repo-mode.json"],
            }
        )
        rels = set(files.keys())
        assert "projects/myapp/role.md" in rels
        assert "projects/myapp/repo-mode.json" not in rels

    def test_walk_generic_source_no_excludes_keeps_all(self, tmp_path):
        base = tmp_path / "gstack"
        projects = base / "projects" / "myapp"
        projects.mkdir(parents=True)
        (projects / "repo-mode.json").write_text("{}")
        (projects / "role.md").write_text("kept")

        files = walk_generic_source(
            {
                "name": "gstack",
                "path": str(base),
                "type": "generic",
                "include_dirs": ["projects"],
            }
        )
        rels = set(files.keys())
        assert "projects/myapp/repo-mode.json" in rels
        assert "projects/myapp/role.md" in rels

    def test_walk_source_threads_exclude_patterns_for_claude_type(self, tmp_path):
        """The claude walker also accepts exclude_patterns via walk_source."""
        base = tmp_path / "claude"
        memory = base / "projects" / "myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "private-notes.md").write_text("kept-locally")
        (memory / "role.md").write_text("synced")

        _, files = walk_source(
            {
                "name": "claude",
                "path": str(base),
                "type": "claude",
                "exclude_patterns": ["projects/*/memory/private-*.md"],
            }
        )
        rels = set(files.keys())
        assert "projects/myapp/memory/role.md" in rels
        assert "projects/myapp/memory/private-notes.md" not in rels

    def test_build_manifest_v2_carries_exclude_patterns_via_source_dict(self, tmp_path):
        base = tmp_path / "gstack"
        projects = base / "projects" / "myapp"
        projects.mkdir(parents=True)
        (projects / "repo-mode.json").write_text("{}")
        (projects / "role.md").write_text("kept")

        manifest = build_manifest_v2(
            "dev-a",
            "Mac A",
            [
                {
                    "name": "gstack",
                    "path": str(base),
                    "type": "generic",
                    "include_dirs": ["projects"],
                    "exclude_patterns": ["projects/*/repo-mode.json"],
                }
            ],
        )
        files = manifest["sources"]["gstack"]["files"]
        assert "projects/myapp/role.md" in files
        assert "projects/myapp/repo-mode.json" not in files


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
        files = walk_claude_source(
            claude, max_file_size=100, on_skip=lambda p, r: skipped.append((p, r))
        )
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
        files = walk_generic_source(
            config, max_file_size=100, on_skip=lambda p, r: skipped.append((p, r))
        )
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


class TestWalkGenericSourceDedup:
    """Group 7 preflight #2 + D6: dedup collected_paths by (st_dev, st_ino).

    When an `include_files` entry sits inside an `include_dirs` directory,
    the same on-disk file lands in `collected_paths` twice. Pre-fix, that
    double-hashed the file (wasted CPU). On case-insensitive volumes
    (APFS default) with case-mismatched config, it produced two distinct
    rel keys for one inode (a real correctness bug — manifest invariant
    violation). Filesystem identity dedup closes both.
    """

    def test_overlap_via_include_files_inside_include_dirs(self, tmp_path):
        base = tmp_path / "src"
        base.mkdir()
        projects = base / "projects"
        projects.mkdir()
        (projects / "notes.md").write_text("hello")

        config = {
            "path": str(base),
            "include_dirs": ["projects"],
            "include_files": ["projects/notes.md"],
        }
        files = walk_generic_source(config)
        assert list(files.keys()) == ["projects/notes.md"]

    def test_overlap_records_file_only_once(self, tmp_path):
        """Confirm dedup happens at the inode level via on_skip side-effect."""
        base = tmp_path / "src"
        base.mkdir()
        projects = base / "projects"
        projects.mkdir()
        (projects / "notes.md").write_text("hello")

        skip_calls: list[tuple[str, str]] = []
        config = {
            "path": str(base),
            "include_dirs": ["projects"],
            "include_files": ["projects/notes.md"],
        }
        files = walk_generic_source(config, on_skip=lambda p, r: skip_calls.append((p, r)))
        # Dedup short-circuits BEFORE _record_file is called twice.
        # No skip should fire (file is below default max_file_size).
        assert files == {
            "projects/notes.md": files["projects/notes.md"],
        }
        assert skip_calls == []

    def test_no_overlap_no_change(self, tmp_path):
        """Dedup is a no-op when there's no overlap."""
        base = tmp_path / "src"
        base.mkdir()
        projects = base / "projects"
        projects.mkdir()
        (projects / "a.md").write_text("a")
        (base / "config.yaml").write_text("x")

        config = {
            "path": str(base),
            "include_dirs": ["projects"],
            "include_files": ["config.yaml"],
        }
        files = walk_generic_source(config)
        assert "projects/a.md" in files
        assert "config.yaml" in files

    def test_symlink_to_already_walked_file_is_not_published(self, tmp_path):
        """A symlink is local routing, never a manifest entry.

        The real file remains eligible even though the symlink sorts before
        it. Publishing the alias would make another machine write through or
        replace a local link during pull.
        """
        base = tmp_path / "src"
        base.mkdir()
        projects = base / "projects"
        projects.mkdir()
        (projects / "real.md").write_text("hello")
        # Symlink in same dir pointing at the file.
        (projects / "alias.md").symlink_to(projects / "real.md")

        config = {
            "path": str(base),
            "include_dirs": ["projects"],
            "include_files": [],
        }
        files = walk_generic_source(config)
        rels = [k for k in files if k.endswith((".md",))]
        assert len(rels) == 1
        assert rels[0] == "projects/real.md"

    def test_symlinked_include_file_is_not_published(self, tmp_path):
        base = tmp_path / "src"
        base.mkdir()
        target = tmp_path / "managed-agents.md"
        target.write_text("managed")
        (base / "AGENTS.md").symlink_to(target)

        files = walk_generic_source(
            {"path": str(base), "include_dirs": [], "include_files": ["AGENTS.md"]}
        )

        assert files == {}

    def test_symlinked_include_directory_is_not_walked(self, tmp_path):
        base = tmp_path / "src"
        base.mkdir()
        managed = tmp_path / "managed-skills"
        managed.mkdir()
        (managed / "SKILL.md").write_text("managed")
        (base / "skills").symlink_to(managed, target_is_directory=True)

        files = walk_generic_source(
            {"path": str(base), "include_dirs": ["skills"], "include_files": []}
        )

        assert files == {}

    def test_symlink_omission_does_not_mint_a_tombstone(self, tmp_path):
        """The push consumer filter preserves a peer's real-file entry.

        This models an existing explicit source configuration, before its
        default exclude globs have been migrated.
        """
        from mind_meld.cli import _filter_symlinked_paths

        base = tmp_path / "codex"
        base.mkdir()
        target = tmp_path / "managed-agents.md"
        target.write_text("managed")
        (base / "AGENTS.md").symlink_to(target)
        remote = {
            "sources": {"codex": {"files": {"AGENTS.md": {"sha256": "a"}}}},
            "tombstones": {
                "codex:AGENTS.md": {"deleted_at": "2026-08-15T00:00:00+00:00"},
                "codex:real.md": {"deleted_at": "2026-08-15T00:00:00+00:00"},
                "legacy.md": {"deleted_at": "2026-08-15T00:00:00+00:00"},
            },
        }

        filtered = _filter_symlinked_paths(
            remote, [{"name": "codex", "path": str(base), "type": "generic"}]
        )
        tombstones = generate_tombstones({"sources": {"codex": {"files": {}}}}, filtered, "me")

        assert "codex:AGENTS.md" not in tombstones
        assert "codex:real.md" in tombstones
        assert "legacy.md" in tombstones
        assert "codex:AGENTS.md" not in filtered["tombstones"]
        assert "codex:real.md" in filtered["tombstones"]
        assert "legacy.md" in filtered["tombstones"]


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

    def test_grok_type_dispatches_to_walk_grok_source(self, tmp_path):
        grok = tmp_path / ".grok"
        skill = grok / "skills" / "my-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("review")
        (grok / "auth.json").write_text("secret")

        config = {"name": "grok", "path": str(grok), "type": "grok"}
        base_path, files = walk_source(config)
        assert "skills/my-review/SKILL.md" in files
        assert "auth.json" not in files
        assert base_path == str(grok.resolve())


class TestWalkGrokSource:
    def _mixed_home(self, tmp_path: Path) -> Path:
        home = tmp_path / ".grok"
        (home / "sessions" / "encoded").mkdir(parents=True)
        (home / "sessions" / "encoded" / "updates.jsonl").write_text("{}\n")
        (home / "sessions" / "encoded" / "chat_history.jsonl").write_text("prompt\n")
        (home / "auth.json").write_text("secret")
        (home / "config.toml").write_text("key = 1")
        (home / "trusted_folders.toml").write_text("")
        (home / "logs").mkdir()
        (home / "logs" / "app.log").write_text("log")
        (home / "worktrees").mkdir()
        (home / "marketplace-cache").mkdir()
        vendor = home / "bundled" / "skills" / "vendor"
        vendor.mkdir(parents=True)
        (vendor / "SKILL.md").write_text("shipped")
        skill = home / "skills" / "my-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("mine")
        (home / "commands").mkdir()
        (home / "commands" / "ship.md").write_text("ship")
        (home / "rules").mkdir()
        (home / "rules" / "house.md").write_text("rule")
        generated = home / "skills" / "gstack-review"
        generated.mkdir()
        (generated / "SKILL.md").write_text("generated")
        return home

    def test_mixed_fixture_uploads_only_allowlisted_files(self, tmp_path):
        from mind_meld.config import get_default_source

        home = self._mixed_home(tmp_path)
        src = get_default_source("grok")
        assert src is not None
        src["path"] = str(home)
        files = walk_grok_source(src)
        assert set(files) == {
            "skills/my-review/SKILL.md",
            "commands/ship.md",
            "rules/house.md",
        }

    def test_nested_dir_symlink_into_sessions_is_not_walked(self, tmp_path):
        home = tmp_path / ".grok"
        sessions = home / "sessions" / "encoded"
        sessions.mkdir(parents=True)
        (sessions / "chat_history.jsonl").write_text("prompt\n")
        skills = home / "skills"
        skills.mkdir()
        (skills / "evil").symlink_to(sessions, target_is_directory=True)
        files = walk_grok_source({"path": str(home), "type": "grok"})
        assert files == {}

    def test_symlinked_include_dir_is_not_walked(self, tmp_path):
        home = tmp_path / ".grok"
        home.mkdir()
        target = tmp_path / "sessions"
        target.mkdir()
        (target / "chat_history.jsonl").write_text("prompt\n")
        (home / "skills").symlink_to(target, target_is_directory=True)
        files = walk_grok_source({"path": str(home), "type": "grok"})
        assert files == {}

    @pytest.mark.parametrize("forbidden_rel", ["auth.json", "sessions/encoded/updates.jsonl"])
    def test_hardlink_to_forbidden_file_is_not_walked(self, tmp_path, forbidden_rel):
        home = tmp_path / ".grok"
        target = home / forbidden_rel
        target.parent.mkdir(parents=True)
        target.write_text("secret")
        skills = home / "skills"
        skills.mkdir()
        (skills / "copied-secret").hardlink_to(target)

        skipped: list[tuple[str, str]] = []
        files = walk_grok_source(
            {"path": str(home), "type": "grok"},
            on_skip=lambda path, reason: skipped.append((path, reason)),
        )

        assert files == {}
        assert skipped == [(str(skills / "copied-secret"), "hardlink")]

    def test_missing_dirs_are_a_noop(self, tmp_path):
        home = tmp_path / ".grok"
        home.mkdir()
        files = walk_grok_source({"path": str(home), "type": "grok"})
        assert files == {}


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
        assert (
            result["sources"]["claude"]["files"]["projects/-foo/memory/role.md"]["sha256"] == "aaa"
        )

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


class TestParseConflictCreatedAt:
    """A1–A12: filename birth-time parser and v1 era marker."""

    EXPECTED = datetime(2026, 9, 3, 15, 17, 47, tzinfo=timezone.utc)

    def test_unprefixed_post_inversion(self):
        got = parse_conflict_created_at("a.sync-conflict-20260903-151747-deadbeef.md")
        assert got == self.EXPECTED

    def test_v0_prefix_tolerated(self):
        got = parse_conflict_created_at("a.sync-conflict-v0-20260903-151747-deadbeef.md")
        assert got == self.EXPECTED

    def test_v1_after_timestamp(self):
        name = "a.sync-conflict-20260903-151747-v1-deadbeef.md"
        assert parse_conflict_created_at(name) == self.EXPECTED
        assert is_v1_conflict_filename(name)

    def test_v1_with_rand4_suffix(self):
        name = "a.sync-conflict-20260903-151747-v1-deadbeef-ab12.md"
        assert parse_conflict_created_at(name) == self.EXPECTED
        assert is_v1_conflict_filename(name)

    def test_extensionless(self):
        assert (
            parse_conflict_created_at("README.sync-conflict-20260903-151747-deadbeef")
            == self.EXPECTED
        )

    def test_digit_shaped_invalid_date_returns_none(self):
        assert parse_conflict_created_at("a.sync-conflict-20261345-999999-deadbeef.md") is None

    def test_garbage_digits_return_none(self):
        assert parse_conflict_created_at("a.sync-conflict-99999999-999999-x.md") is None

    def test_non_conflict_returns_none(self):
        assert parse_conflict_created_at("notes.md") is None

    def test_double_infix_parses_last(self):
        name = "notes.sync-conflict-log.sync-conflict-20260101-000000-abcd1234.md"
        assert parse_conflict_created_at(name) == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_parsed_as_utc_not_local(self):
        got = parse_conflict_created_at("a.sync-conflict-20260903-151747-deadbeef.md")
        assert got is not None
        assert got.tzinfo is timezone.utc
        assert got == datetime(2026, 9, 3, 15, 17, 47, tzinfo=timezone.utc)

    def test_v1_shape_stays_excluded_and_device_parsable(self):
        name = "a.sync-conflict-20260903-151747-v1-deadbeef.md"
        assert is_conflict_filename(name)
        assert not is_pre_inversion_conflict_filename(name)
        assert _is_excluded("memory/" + name)
        assert parse_conflict_device_short(name) == "deadbeef"

    def test_v1_as_prefix_is_not_a_conflict_filename(self):
        """Regression: a v0-style prefix would un-exclude the copy."""
        prefix = "a.sync-conflict-v1-20260903-151747-deadbeef.md"
        assert not is_conflict_filename(prefix)
        assert not _is_excluded("memory/" + prefix)
        assert not is_v1_conflict_filename(prefix)


class TestMarkerSkipGlobs:
    """Track 47A: directories containing `.extend-root` drop out of sync."""

    def test_discovers_globs_for_marked_dirs(self, tmp_path: Path):
        base = tmp_path / "codex"
        marked = base / "skills" / "new-skill"
        marked.mkdir(parents=True)
        (marked / MARKER_SKIP_NAME).write_text("gstack-extend")
        (marked / "SKILL.md").write_text("generated")
        unmarked = base / "skills" / "my-own"
        unmarked.mkdir(parents=True)
        (unmarked / "SKILL.md").write_text("hand-authored")
        cfg = {
            "name": "codex",
            "path": str(base),
            "type": "generic",
            "include_dirs": ["skills"],
        }
        assert marker_skip_globs(cfg) == ["skills/new-skill"]

    def test_walker_skips_marked_dir_keeps_unmarked(self, tmp_path: Path):
        base = tmp_path / "codex"
        marked = base / "skills" / "new-skill"
        marked.mkdir(parents=True)
        (marked / MARKER_SKIP_NAME).write_text("gstack-extend")
        (marked / "SKILL.md").write_text("generated")
        unmarked = base / "skills" / "my-own"
        unmarked.mkdir(parents=True)
        (unmarked / "SKILL.md").write_text("hand-authored")
        files = walk_generic_source(
            {
                "name": "codex",
                "path": str(base),
                "type": "generic",
                "include_dirs": ["skills"],
            }
        )
        paths = set(files.keys())
        assert "skills/my-own/SKILL.md" in paths
        assert "skills/new-skill/SKILL.md" not in paths
        assert "skills/new-skill/.extend-root" not in paths

    def test_no_include_dirs_returns_empty(self):
        assert marker_skip_globs({"name": "claude", "path": "~/.claude", "type": "claude"}) == []

    def test_marker_on_include_dir_root_does_not_skip_the_whole_tree(self, tmp_path: Path):
        base = tmp_path / "codex"
        skills = base / "skills"
        skills.mkdir(parents=True)
        (skills / MARKER_SKIP_NAME).write_text("misplaced")
        keep = skills / "my-own"
        keep.mkdir()
        (keep / "SKILL.md").write_text("hand-authored")
        cfg = {
            "name": "codex",
            "path": str(base),
            "type": "generic",
            "include_dirs": ["skills"],
        }
        assert marker_skip_globs(cfg) == []
        files = walk_generic_source(cfg)
        assert "skills/my-own/SKILL.md" in files

    @pytest.mark.parametrize("configured", ["skills/", "./skills", "skills"])
    def test_include_root_exemption_survives_unnormalized_config(
        self, tmp_path: Path, configured: str
    ):
        """`include_dirs` entries are never validated or normalized.

        A trailing slash or a `./` prefix must not defeat the include-root
        exemption: `rel_dir` comes from `as_posix()`, `dir_name` is raw
        config, and a mismatch turns the whole configured tree into a skip
        prefix — silently dropping hand-authored files out of sync.
        (Greptile review, PR #161.)
        """
        base = tmp_path / "codex"
        skills = base / "skills"
        skills.mkdir(parents=True)
        (skills / MARKER_SKIP_NAME).write_text("misplaced")
        mine = skills / "my-own"
        mine.mkdir()
        (mine / "SKILL.md").write_text("hand-authored")
        cfg = {
            "name": "codex",
            "path": str(base),
            "type": "generic",
            "include_dirs": [configured],
        }
        assert marker_skip_globs(cfg) == []
        assert "skills/my-own/SKILL.md" in walk_generic_source(cfg)

    def test_literal_star_directory_is_a_prefix_not_a_glob(self, tmp_path: Path):
        """A marker under a directory named ``*`` must not exclude siblings."""
        from mind_meld.manifest import _under_skip_prefix

        base = tmp_path / "gstack"
        starred = base / "projects" / "*"
        starred.mkdir(parents=True)
        (starred / MARKER_SKIP_NAME).write_text("planted")
        (base / "projects" / "myapp").mkdir(parents=True)
        (base / "projects" / "myapp" / "role.md").write_text("keep")
        cfg = {
            "name": "gstack",
            "path": str(base),
            "type": "generic",
            "include_dirs": ["projects"],
        }
        prefixes = marker_skip_globs(cfg)
        assert prefixes == ["projects/*"]
        assert _under_skip_prefix("projects/*/SKILL.md", prefixes)
        assert not _under_skip_prefix("projects/myapp/role.md", prefixes)
        files = walk_generic_source(cfg)
        assert "projects/myapp/role.md" in files
        assert "projects/*/SKILL.md" not in files


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
        (memory / "notes.sync-conflict-20260422-120000-abc12345.md").write_text("local divergent")

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
        (todos / "tasks.sync-conflict-20260422-120000-abc12345.json").write_text("[1, 2]")

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
                "claude:memory/already.md": {
                    "deleted_at": "2026-04-22T10:00:00+00:00",
                    "device_id": "a",
                },
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
            "files": {
                "memory/x.md": {"sha256": "x", "size": 1, "mtime": "2026-04-22T10:00:00+00:00"}
            },
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
            "sources": {
                "claude": {
                    "base_path": "/p",
                    "files": {"a.md": {"sha256": "h", "size": 1, "mtime": "t"}},
                }
            },
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
            load_manifest(b'{"device_id":"a","sources":{"claude":"not-a-dict"},"tombstones":{}}')

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


class TestLoadManifestRelPathTraversal:
    """Path-traversal defense at the manifest load boundary.

    A peer with the storage passphrase can mint an authenticated manifest
    whose ``sources[*].files`` keys are free-form UTF-8. Without this guard,
    `_download_and_apply` would build ``base_path / rel_path`` and write
    decrypted bytes outside the source root — Python's `Path /` follows
    `..` segments and lets an absolute right-hand side override the base.
    Mirrors `storage/keys.py`'s sibling defense for the sha256 component.
    """

    def _wrap(self, files: dict[str, Any], tombstones: dict[str, Any] | None = None) -> bytes:
        m: dict[str, Any] = {
            "device_id": "a",
            "sources": {"claude": {"base_path": "", "files": files}},
            "tombstones": tombstones or {},
        }
        return serialize_manifest(m)

    def test_rejects_parent_dir_segment(self):
        with pytest.raises(ManifestError, match=r"'\.\.' segments"):
            load_manifest(
                self._wrap({"../../etc/passwd": {"sha256": "h", "size": 1, "mtime": "t"}})
            )

    def test_rejects_parent_dir_in_middle(self):
        with pytest.raises(ManifestError, match=r"'\.\.' segments"):
            load_manifest(self._wrap({"a/../../etc/x": {"sha256": "h", "size": 1, "mtime": "t"}}))

    def test_rejects_bare_parent_dir(self):
        with pytest.raises(ManifestError, match=r"'\.\.' segments"):
            load_manifest(self._wrap({"..": {"sha256": "h", "size": 1, "mtime": "t"}}))

    def test_rejects_backslash_parent_dir(self):
        # Mixed/back-slash spelled traversal — Python's Path / on POSIX
        # treats backslash as a literal char, but on case-insensitive
        # filesystems with cross-platform tooling we still refuse to
        # let any ".." spelling through the load boundary.
        with pytest.raises(ManifestError, match=r"'\.\.' segments"):
            load_manifest(self._wrap({"a\\..\\etc\\x": {"sha256": "h", "size": 1, "mtime": "t"}}))

    def test_rejects_absolute_posix_path(self):
        # Path('/base') / '/etc/passwd' returns Path('/etc/passwd') —
        # absolute RHS overrides base entirely.
        with pytest.raises(ManifestError, match="must not be absolute"):
            load_manifest(
                self._wrap({"/etc/cron.d/mm-pwn": {"sha256": "h", "size": 1, "mtime": "t"}})
            )

    def test_rejects_absolute_backslash_path(self):
        with pytest.raises(ManifestError, match="must not be absolute"):
            load_manifest(
                self._wrap({"\\Windows\\System32\\evil": {"sha256": "h", "size": 1, "mtime": "t"}})
            )

    def test_rejects_drive_letter(self):
        with pytest.raises(ManifestError, match="drive letter"):
            load_manifest(self._wrap({"C:Windows\\evil": {"sha256": "h", "size": 1, "mtime": "t"}}))

    def test_rejects_null_byte(self):
        with pytest.raises(ManifestError, match="null bytes"):
            load_manifest(self._wrap({"a\x00b": {"sha256": "h", "size": 1, "mtime": "t"}}))

    def test_rejects_empty_key(self):
        with pytest.raises(ManifestError, match="non-empty"):
            load_manifest(self._wrap({"": {"sha256": "h", "size": 1, "mtime": "t"}}))

    def test_rejects_traversal_in_tombstone_path_part(self):
        # Tombstones don't drive deletion (pull is additive-only), but a
        # tombstone keyed on a `..` path could still mask legitimate files
        # via `is_tombstoned` checks. Reject at the load boundary to keep
        # the invariant uniform.
        with pytest.raises(ManifestError, match=r"'\.\.' segments"):
            load_manifest(
                self._wrap(
                    {},
                    tombstones={
                        "claude:../../etc/passwd": {
                            "deleted_at": "2026-04-22T10:00:00+00:00",
                            "device_id": "a",
                        }
                    },
                )
            )

    def test_accepts_normal_relative_paths(self):
        # Confirm the validator does NOT false-positive on legitimate
        # paths that contain dots in extensions or single-dot hidden
        # filenames — ".env.local", "a.b.c.md", "memory/.gitkeep" all
        # contain `.` segments that are NOT `..`.
        loaded = load_manifest(
            self._wrap(
                {
                    "memory/user_role.md": {"sha256": "h", "size": 1, "mtime": "t"},
                    "todos/.env.local": {"sha256": "h", "size": 1, "mtime": "t"},
                    "memory/a.b.c.md": {"sha256": "h", "size": 1, "mtime": "t"},
                    "deeply/nested/path/file.txt": {"sha256": "h", "size": 1, "mtime": "t"},
                }
            )
        )
        assert "memory/user_role.md" in loaded["sources"]["claude"]["files"]
        assert "todos/.env.local" in loaded["sources"]["claude"]["files"]


class TestRecordFile:
    """Direct coverage of the _record_file pipeline helper.

    Closes the long-standing gap where stat-PermissionError and hash-OSError
    branches were unreachable via the walker-level tests (which operate on
    real filesystems). Also pins the exact on_skip reason strings — those
    are surfaced in cli.py verbose walker output, so their shape is
    load-bearing user-visible contract.
    """

    def test_happy_path_returns_rel_and_info(self, tmp_path):
        base = tmp_path
        sub = base / "sub"
        sub.mkdir()
        f = sub / "a.md"
        f.write_text("hello")
        result = _record_file(f, base, max_file_size=1_000_000)
        assert result is not None
        rel, info = result
        assert rel == "sub/a.md"
        assert info["sha256"] == hashlib.sha256(b"hello").hexdigest()
        assert info["size"] == 5
        assert "mtime" in info

    def test_excluded_returns_none_without_on_skip(self, tmp_path):
        """_is_excluded path returns None and does NOT invoke on_skip —
        excluded files are silent (by design; they're filtered, not skipped)."""
        base = tmp_path
        f = base / ".DS_Store"
        f.write_text("")
        skipped: list[tuple[str, str]] = []
        result = _record_file(
            f,
            base,
            max_file_size=1_000_000,
            on_skip=lambda p, r: skipped.append((p, r)),
        )
        assert result is None
        assert skipped == []  # exclusion is silent

    def test_stat_permission_error_emits_on_skip(self, tmp_path, monkeypatch):
        """Permission-denied on stat() surfaces the exact reason string.

        Scope the stat patch to the target path only, so that any incidental
        .stat() call elsewhere in the pipeline (future symlink sniffing,
        etc.) isn't silently absorbed by this test.
        """
        base = tmp_path
        f = base / "a.md"
        f.write_text("x")

        real_stat = Path.stat

        def scoped_raise(self, *args, **kwargs):
            if self == f:
                raise PermissionError("simulated")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", scoped_raise)
        skipped: list[tuple[str, str]] = []
        result = _record_file(
            f,
            base,
            max_file_size=1_000_000,
            on_skip=lambda p, r: skipped.append((p, r)),
        )
        assert result is None
        assert skipped == [("a.md", "permission denied")]

    def test_size_exceeded_emits_formatted_on_skip(self, tmp_path):
        """Size-cap skip pins the exact reason format: `exceeds max_file_size (<N.N>MB)`."""
        base = tmp_path
        f = base / "big.bin"
        f.write_bytes(b"x" * 2048)  # 2 KB
        skipped: list[tuple[str, str]] = []
        result = _record_file(
            f,
            base,
            max_file_size=1024,
            on_skip=lambda p, r: skipped.append((p, r)),
        )
        assert result is None
        assert len(skipped) == 1
        rel, reason = skipped[0]
        assert rel == "big.bin"
        # 2048 bytes / (1024 * 1024) = 0.001953125 MB, rounded to 0.0MB
        assert reason == "exceeds max_file_size (0.0MB)"

    def test_hash_os_error_emits_on_skip(self, tmp_path, monkeypatch):
        """hash_file raising OSError (e.g. mid-walk unlink) surfaces the `read error` reason."""
        import mind_meld.manifest as m

        base = tmp_path
        f = base / "a.md"
        f.write_text("x")

        def raise_os(path):
            raise OSError("simulated read error")

        monkeypatch.setattr(m, "hash_file", raise_os)
        skipped: list[tuple[str, str]] = []
        result = _record_file(
            f,
            base,
            max_file_size=1_000_000,
            on_skip=lambda p, r: skipped.append((p, r)),
        )
        assert result is None
        assert skipped == [("a.md", "read error")]


class TestIsActiveTombstone:
    """Direct coverage of _is_active_tombstone — the shared predicate for
    `generate_tombstones` (carry-forward) and `collect_tombstones` (fleet
    aggregation). Pins the tz-naive → UTC guard (load-bearing: naive vs
    tz-aware comparison raises TypeError) and the (ValueError, TypeError)
    fallthrough.
    """

    def _cutoff_now(self):
        from datetime import datetime, timedelta, timezone

        return datetime.now(timezone.utc) - timedelta(days=TOMBSTONE_TTL_DAYS)

    def test_tz_aware_non_expired_is_active(self):
        from datetime import datetime, timezone

        info = {"deleted_at": datetime.now(timezone.utc).isoformat()}
        assert _is_active_tombstone(info, self._cutoff_now()) is True

    def test_tz_naive_non_expired_is_active(self):
        """Naive `deleted_at` string must be treated as UTC, not raise on
        the cutoff comparison. Older mm versions or external tooling could
        write naive timestamps; the guard here prevents a fleet-wide
        TypeError crash."""
        from datetime import datetime, timezone

        # Drop tzinfo to get a naive datetime in UTC without using the
        # deprecated datetime.utcnow().
        naive_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        info = {"deleted_at": naive_iso}
        assert _is_active_tombstone(info, self._cutoff_now()) is True

    def test_expired_is_not_active(self):
        from datetime import datetime, timedelta, timezone

        expired = (datetime.now(timezone.utc) - timedelta(days=TOMBSTONE_TTL_DAYS + 1)).isoformat()
        info = {"deleted_at": expired}
        assert _is_active_tombstone(info, self._cutoff_now()) is False

    def test_unparseable_returns_false(self):
        """Empty string, garbage string, and non-string all return False —
        conservative: a corrupt `deleted_at` drops the tombstone rather than
        living forever."""
        cutoff = self._cutoff_now()
        assert _is_active_tombstone({"deleted_at": ""}, cutoff) is False
        assert _is_active_tombstone({"deleted_at": "not-a-date"}, cutoff) is False
        assert _is_active_tombstone({}, cutoff) is False  # missing key
        assert _is_active_tombstone({"deleted_at": 12345}, cutoff) is False  # non-string


class TestGenerateTombstonesContract:
    """Pins the post-Track-1B caller contract on generate_tombstones:
    `remote_manifest` MUST be v2-normalized (or None).

    Previously, generate_tombstones defensively called normalize_manifest
    on its input at line 607. Cross-model adversarial review (Claude + Codex,
    2026-04-24) found that this call did DOUBLE duty: migrate bare-path
    tombstone keys (cosmetic) AND promote v1 `"files"` → v2 `"sources"`
    right before the new-tombstone detection loop. Dropping it silently
    turned v1-shaped input into a zero-tombstone result — a silent
    delete-propagation loss.

    Fix: enforce the contract at the function boundary. A v1-shaped dict
    (no `"sources"` key) now raises ManifestError instead of silently
    producing wrong output. Every internal caller already routes through
    load_manifest or hand-builds v2 shape, so this is a loud-fail guard
    for future-caller bugs, not a behavior break for current code.

    The happy-path carry-forward semantics (v1 → load_manifest → migrated
    `claude:<path>` tombstones survive through generate_tombstones) is
    covered by test_additive_sync.py::test_migrated_key_carries_forward_through_generate_tombstones.
    """

    def test_raises_on_v1_shaped_input(self):
        """A v1-shaped dict (with `files` but no `sources`) fed DIRECTLY
        raises ManifestError — rather than silently producing zero
        tombstones, which would be silent delete-propagation loss."""
        from datetime import datetime, timedelta, timezone

        recent_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        raw_v1 = {
            "device_id": "peer1",
            "files": {
                "memory/foo.md": {
                    "sha256": "a" * 64,
                    "size": 1,
                    "mtime": "2026-04-20T00:00:00+00:00",
                },
            },
            "tombstones": {
                "memory/foo.md": {
                    "deleted_at": recent_iso,
                    "device_id": "peer1",
                },
            },
        }
        local_manifest = {
            "device_id": "this-device",
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {},
        }

        with pytest.raises(ManifestError, match="v2-normalized"):
            generate_tombstones(local_manifest, raw_v1, "this-device")

    def test_none_remote_still_allowed(self):
        """None remote (first-push case) is still valid — the guard only
        triggers on a non-None dict that's missing the `sources` key."""
        local_manifest = {
            "device_id": "this-device",
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {},
        }
        # Must not raise.
        result = generate_tombstones(local_manifest, None, "this-device")
        assert result == {}

    def test_empty_first_grok_push_does_not_tombstone_unknown_files(self):
        """This device never had grok files. Empty local grok must not mint
        tombstones for a peer's skills — generate_tombstones only diffs
        this device's prior manifest."""
        local_manifest = {
            "device_id": "this-device",
            "sources": {"grok": {"base_path": "", "files": {}}},
            "tombstones": {},
        }
        prior = {
            "device_id": "this-device",
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {},
        }
        result = generate_tombstones(local_manifest, prior, "this-device")
        assert not any(key.startswith("grok:") for key in result)
