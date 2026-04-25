"""Tests for conflict-copy preservation + mtime-skip on pull.

Covers _apply_incoming_file's decision tree:

    local missing                           -> WRITE
    local hash == remote hash               -> UNCHANGED
    should_merge(rel_path)                  -> MERGED
    local mtime > remote mtime              -> SKIPPED
    local mtime <= remote mtime             -> CONFLICTED
        rename canonical -> .sync-conflict-<ts>-<device>.<ext>
        write  remote    -> canonical

Plus the conflict_filename helper, manifest mtime helpers, and the new
mm conflicts / resolve / gc --conflicts commands.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from mind_meld.cli import (
    CONFLICT_INFIX,
    _apply_incoming_file,
    _canonical_for_conflict,
    _find_conflict_files,
    _gc_old_conflict_files,
    _predict_pull_outcome,
    _resolve_interactive_loop,
    app,
    conflict_filename,
)
from mind_meld.config import save_config
from mind_meld.manifest import (
    hash_file,
    mtime_from_manifest,
    mtime_from_path,
    walk_claude_source,
)

# ── helpers ──────────────────────────────────────────────────────────


def _set_mtime(path: Path, dt: datetime) -> None:
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def _remote_info(sha: str, mtime: datetime) -> dict:
    return {
        "sha256": sha,
        "size": 0,
        "mtime": mtime.isoformat(),
    }


# ── _apply_incoming_file branches ────────────────────────────────────


class TestApplyIncomingFile:
    def test_write_when_local_missing(self, tmp_path: Path) -> None:
        """[W] Local file doesn't exist -> remote written to canonical path."""
        rel = "memory/user_role.md"
        local = tmp_path / rel
        remote_data = b"remote content"
        info = _remote_info("deadbeef", datetime.now(timezone.utc))

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=remote_data,
            remote_info=info,
            remote_device_id="devA1234",
        )

        assert outcome == "written"
        assert local.read_bytes() == remote_data
        # No conflict file created
        conflicts = list(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert conflicts == []

    def test_unchanged_when_local_matches_remote(self, tmp_path: Path) -> None:
        """[U] Local hash == remote hash -> no-op, idempotent."""

        rel = "memory/unchanged.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"same content")
        sha = hash_file(local)
        info = _remote_info(sha, datetime.now(timezone.utc))

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"same content",
            remote_info=info,
            remote_device_id="devA1234",
        )

        assert outcome == "unchanged"
        assert local.read_bytes() == b"same content"

    def test_merge_wins_for_jsonl_even_when_local_newer(self, tmp_path: Path) -> None:
        """[M] precedes [S]: a newer local .jsonl still merges, not skips."""
        rel = "projects/p1/learnings.jsonl"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b'{"ts":"2026-01-01","key":"a"}\n')

        # Local is "newer" than remote, but merge is the correct operation.
        _set_mtime(local, datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))

        remote_data = b'{"ts":"2026-02-01","key":"b"}\n'
        info = _remote_info("bbb", datetime(2026, 1, 1, tzinfo=timezone.utc))

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=remote_data,
            remote_info=info,
            remote_device_id="devA1234",
        )

        assert outcome == "merged"
        merged = local.read_bytes().decode()
        # Both lines present (order is ts-sorted)
        assert "a" in merged and "b" in merged

    def test_merge_wins_for_memory_md(self, tmp_path: Path) -> None:
        """[M] MEMORY.md is line-union merged, not conflict-copied."""
        rel = "projects/p1/memory/MEMORY.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"- [alpha](a.md)\n")
        remote_data = b"- [beta](b.md)\n"
        info = _remote_info("bbb", datetime.now(timezone.utc))

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=remote_data,
            remote_info=info,
            remote_device_id="devA1234",
        )

        assert outcome == "merged"
        conflicts = list(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert conflicts == []

    def test_skip_when_local_is_newer(self, tmp_path: Path) -> None:
        """[S] Local mtime > remote mtime -> skip. No conflict file."""
        rel = "memory/user_role.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local content")

        # Local mtime AFTER remote
        _set_mtime(local, datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))

        remote_mtime = datetime(2026, 4, 21, 11, 0, tzinfo=timezone.utc)
        info = _remote_info("remotehash", remote_mtime)

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
        )

        assert outcome == "skipped"
        assert local.read_bytes() == b"local content"  # untouched
        conflicts = list(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert conflicts == []

    def test_conflict_flips_local_to_sibling(self, tmp_path: Path) -> None:
        """[C] Local older than remote -> local renamed to .sync-conflict-*,
        remote written to canonical path."""
        rel = "memory/user_role.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))

        remote_mtime = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
        info = _remote_info("remotehash", remote_mtime)

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
        )

        assert outcome == "conflicted"
        # Canonical now has remote
        assert local.read_bytes() == b"remote content"
        # Conflict file has original local
        conflicts = list(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert len(conflicts) == 1
        assert conflicts[0].read_bytes() == b"local content"
        assert conflicts[0].name.endswith(".md")

    def test_empty_remote_device_id_returns_failed_not_raises(self, tmp_path: Path) -> None:
        """REGRESSION: a corrupted peer manifest with empty device_id used to
        crash the entire pull as 'unexpected error' (ValueError from
        conflict_filename propagating uncaught). Per-file isolation now
        catches it: the bad file fails, the local file is preserved at
        canonical, and the pull walk continues for other files.
        """
        rel = "memory/user_role.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))

        remote_mtime = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
        info = _remote_info("remotehash", remote_mtime)

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="",  # corrupted peer manifest
        )

        assert outcome == "failed"
        # Local file is preserved unchanged at the canonical path.
        assert local.read_bytes() == b"local content"
        # No conflict file was created (the build failed before rename).
        assert list(local.parent.glob(f"*{CONFLICT_INFIX}*")) == []

    def test_pull_is_idempotent_after_conflict(self, tmp_path: Path) -> None:
        """Second apply of the same remote data should be unchanged, not a
        second conflict. This is the critical convergence property."""
        rel = "memory/user_role.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))

        # Simulated remote info that _apply_incoming_file reaches via diff.
        # Apply #1 creates a conflict.
        remote_sha = hashlib.sha256(b"remote content").hexdigest()
        info = _remote_info(remote_sha, datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))

        first = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
        )
        assert first == "conflicted"

        # Apply #2: local now matches remote (we just wrote remote). diff_files
        # upstream would have filtered this out as unchanged, but if it somehow
        # still reaches _apply_incoming_file, the re-read hash branch catches it.
        second = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
        )
        assert second == "unchanged"

        # Critically: only ONE conflict file, not two.
        conflicts = list(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert len(conflicts) == 1


class TestConflictFilename:
    def test_syncthing_format(self) -> None:
        """Format: <stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device_short>.<ext>"""
        canonical = Path("/tmp/fake/user_role.md")
        now = datetime(2026, 4, 21, 14, 30, 55, tzinfo=timezone.utc)
        path = conflict_filename(canonical, "a1b2c3d4-e5f6-7890", now=now)
        assert path.name == "user_role.sync-conflict-20260421-143055-a1b2c3d4.md"

    def test_short_device_id_padded(self, tmp_path: Path) -> None:
        """Device id shorter than 8 chars is used as-is (no padding)."""
        canonical = tmp_path / "f.md"
        path = conflict_filename(canonical, "dev")
        assert "-dev." in path.name

    def test_collision_suffix_when_same_second(self, tmp_path: Path) -> None:
        """Same-second double conflict on same device gets 4-char random suffix."""
        canonical = tmp_path / "x.md"
        now = datetime(2026, 4, 21, 14, 30, 55, tzinfo=timezone.utc)
        first = conflict_filename(canonical, "devA1234", now=now)
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"seed")
        second = conflict_filename(canonical, "devA1234", now=now)
        assert second != first
        # Suffix pattern: <base>-<4hex>.<ext>
        assert second.stem.endswith(tuple("0123456789abcdef"))
        assert second.suffix == ".md"

    def test_preserves_multidot_stems(self, tmp_path: Path) -> None:
        """file.config.yaml keeps its middle dot in the stem portion."""
        canonical = tmp_path / "config.local.yaml"
        now = datetime(2026, 4, 21, 14, 30, 55, tzinfo=timezone.utc)
        path = conflict_filename(canonical, "devA1234", now=now)
        assert path.name.startswith("config.local")
        assert path.suffix == ".yaml"

    def test_empty_device_id_raises(self, tmp_path: Path) -> None:
        """Empty device_id used to silently fall back to literal "unknown",
        causing cross-device filename collisions when two peers hit the same
        path. Now raises so the caller surfaces the corruption.
        """
        canonical = tmp_path / "f.md"
        with pytest.raises(ValueError, match="device_id must be non-empty"):
            conflict_filename(canonical, "")

    def test_none_device_id_raises(self, tmp_path: Path) -> None:
        """None falls into the same trap as empty string under the old fallback."""
        canonical = tmp_path / "f.md"
        with pytest.raises(ValueError, match="device_id must be non-empty"):
            conflict_filename(canonical, None)  # type: ignore[arg-type]


# ── manifest mtime helpers ───────────────────────────────────────────


class TestMtimeHelpers:
    def test_roundtrip_matches_walker_output(self, tmp_path: Path) -> None:
        """mtime_from_path produces the same form that manifest walkers emit."""
        f = tmp_path / "x.md"
        f.write_bytes(b"hi")
        target = datetime(2026, 4, 21, 14, 30, 0, tzinfo=timezone.utc)
        _set_mtime(f, target)

        from_path = mtime_from_path(f)
        from_manifest_form = mtime_from_manifest(target.isoformat())

        # Same instant, both tz-aware UTC
        assert from_path.tzinfo is not None
        assert from_manifest_form.tzinfo is not None
        assert abs((from_path - from_manifest_form).total_seconds()) < 1

    def test_manifest_parses_z_suffix(self) -> None:
        """mtime_from_manifest handles `Z` suffix."""
        dt = mtime_from_manifest("2026-04-21T14:30:00+00:00")
        assert dt.year == 2026 and dt.hour == 14


# ── _find_conflict_files / _canonical_for_conflict ────────────────────


class TestConflictDiscovery:
    def test_canonical_for_conflict_strips_infix(self) -> None:
        """Converts .sync-conflict-<stuff>.ext back to the original filename."""
        cpath = Path("/x/user_role.sync-conflict-20260421-143055-a1b2c3d4.md")
        assert _canonical_for_conflict(cpath).name == "user_role.md"

    def test_canonical_for_conflict_with_random_suffix(self) -> None:
        """Random 4-char collision suffix is part of the conflict metadata."""
        cpath = Path("/x/user_role.sync-conflict-20260421-143055-a1b2c3d4-7f9a.md")
        assert _canonical_for_conflict(cpath).name == "user_role.md"

    def test_find_conflict_files_walks_sources(self, tmp_path: Path, monkeypatch) -> None:
        """_find_conflict_files walks configured sources and finds .sync-conflict-* files."""
        src = tmp_path / "src1"
        (src / "memory").mkdir(parents=True)
        (src / "memory" / "user.md").write_bytes(b"canonical")
        conflict = src / "memory" / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict")

        config = {
            "device": {"id": "me"},
            "sync": {
                "sources": [
                    {
                        "name": "s1",
                        "path": str(src),
                        "type": "generic",
                        "include_dirs": ["memory"],
                        "include_files": [],
                    }
                ]
            },
        }

        hits = _find_conflict_files(config)
        assert len(hits) == 1
        src_name, cpath, canonical = hits[0]
        assert src_name == "s1"
        assert cpath == conflict
        assert canonical == src / "memory" / "user.md"


# ── _predict_pull_outcome ────────────────────────────────────────────


# NOTE: cli.py:_atomic_write was deleted in Track 1D. Its tmp-cleanup
# guarantees now live in fsutil.atomic_write_bytes — see tests/test_fsutil.py
# (test_write_failure_unlinks_tmp, test_replace_failure_unlinks_tmp).


class TestGcOldConflictFiles:
    def test_reaps_files_older_than_cutoff(self, tmp_path: Path, monkeypatch) -> None:
        """_gc_old_conflict_files deletes .sync-conflict-* files older than
        CONFLICT_AGE_DAYS. Fresh files are preserved."""

        src = tmp_path / "src"
        (src / "memory").mkdir(parents=True)

        old_conflict = src / "memory" / "a.sync-conflict-20000101-000000-devA1234.md"
        old_conflict.write_bytes(b"old")
        ancient = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(old_conflict, (ancient, ancient))

        new_conflict = src / "memory" / "b.sync-conflict-20260421-000000-devA1234.md"
        new_conflict.write_bytes(b"new")
        # Fresh mtime (now) so it falls inside the retention window

        config = {
            "sync": {
                "sources": [
                    {
                        "name": "s1",
                        "path": str(src),
                        "type": "generic",
                        "include_dirs": ["memory"],
                        "include_files": [],
                    }
                ]
            },
        }

        reaped = _gc_old_conflict_files(config, dry_run=False, verbose=False)
        assert reaped == 1
        assert not old_conflict.exists(), "old conflict should be reaped"
        assert new_conflict.exists(), "fresh conflict should survive"

    def test_dry_run_does_not_delete(self, tmp_path: Path) -> None:
        """dry_run=True lists but preserves everything."""

        src = tmp_path / "src"
        (src / "memory").mkdir(parents=True)
        old = src / "memory" / "a.sync-conflict-20000101-000000-devA1234.md"
        old.write_bytes(b"old")
        ancient = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(old, (ancient, ancient))

        config = {
            "sync": {
                "sources": [
                    {
                        "name": "s1",
                        "path": str(src),
                        "type": "generic",
                        "include_dirs": ["memory"],
                        "include_files": [],
                    }
                ]
            },
        }
        _gc_old_conflict_files(config, dry_run=True, verbose=False)
        assert old.exists(), "dry-run must not delete"


class TestCanonicalForConflictEdgeCases:
    def test_no_infix_returns_path_unchanged(self) -> None:
        """File without .sync-conflict- in its name is returned as-is."""
        path = Path("/x/plain.md")
        assert _canonical_for_conflict(path) == path


class TestFindConflictFilesClaudeType:
    def test_walks_claude_projects_subtree(self, tmp_path: Path) -> None:
        """claude-type sources walk projects/*/memory and projects/*/todos."""
        src = tmp_path / "claude"
        (src / "projects" / "proj1" / "memory").mkdir(parents=True)
        (src / "projects" / "proj1" / "todos").mkdir(parents=True)
        (src / "projects" / "proj1" / "sessions").mkdir(parents=True)

        in_scope = (
            src / "projects" / "proj1" / "memory" / "a.sync-conflict-20260421-143055-devA1234.md"
        )
        in_scope.write_bytes(b"conflict")

        out_of_scope = (
            src / "projects" / "proj1" / "sessions" / "b.sync-conflict-20260421-143055-devA1234.md"
        )
        out_of_scope.write_bytes(b"should not be listed")

        config = {
            "sync": {"sources": [{"name": "claude", "path": str(src), "type": "claude"}]},
        }

        hits = _find_conflict_files(config)
        hit_paths = [h[1] for h in hits]
        assert in_scope in hit_paths
        assert out_of_scope not in hit_paths, "scope should exclude sessions/"


class TestFindConflictFilesIncludeFiles:
    """Regression for kb-mbp 2026-04-24 first-pull: a 286-file pull produced 6
    conflict copies, but `mm conflicts` listed only 5. The missing one was
    `~/.gstack/config.sync-conflict-...yaml` — a sibling of an `include_files`
    entry. `_synced_scan_dirs` only returned `include_dirs` for generic
    sources, so depth-0 conflict siblings of `include_files` entries were
    invisible to mm conflicts / mm resolve / mm gc --conflicts.

    Track 5A Task 2: _find_conflict_files now adds a depth-0 sibling-glob
    path for each generic include_files entry. is_conflict_filename's strict
    pattern still keeps user files like notes.sync-conflict-log.md filtered.
    """

    def _config(self, src: Path, include_files: list[str]) -> dict:
        return {
            "sync": {
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(src),
                        "type": "generic",
                        "include_dirs": ["projects"],
                        "include_files": include_files,
                    }
                ]
            }
        }

    def test_top_level_include_file_conflict_is_listed(self, tmp_path: Path) -> None:
        """Conflict on a top-level include_files entry (e.g. config.yaml) appears
        in the hits list. Pre-fix this was the 6-of-6 vs 5-of-6 regression."""
        src = tmp_path / "gstack"
        src.mkdir()
        (src / "config.yaml").write_bytes(b"canonical")
        conflict = src / "config.sync-conflict-20260424-233316-889e42c0.yaml"
        conflict.write_bytes(b"divergent")

        hits = _find_conflict_files(self._config(src, ["config.yaml"]))
        hit_paths = [h[1] for h in hits]
        assert conflict in hit_paths

    def test_dotfile_include_file_conflict_is_listed(self, tmp_path: Path) -> None:
        """Conflict on a leading-dot top-level include_files entry (e.g.
        `.completeness-intro-seen` — no extension). Path.stem treats the full
        name as the stem and the suffix is empty, so the glob pattern is
        `.completeness-intro-seen.sync-conflict-*`. Verifies the empty-suffix
        edge case."""
        src = tmp_path / "gstack"
        src.mkdir()
        (src / ".completeness-intro-seen").write_bytes(b"")
        conflict = src / ".completeness-intro-seen.sync-conflict-20260424-233316-889e42c0"
        conflict.write_bytes(b"divergent")

        hits = _find_conflict_files(self._config(src, [".completeness-intro-seen"]))
        hit_paths = [h[1] for h in hits]
        assert conflict in hit_paths

    def test_canonical_resolution_for_include_file_conflict(self, tmp_path: Path) -> None:
        """Each hit must report the canonical path correctly. mm resolve relies
        on this to write the chosen bytes to the right destination."""
        src = tmp_path / "gstack"
        src.mkdir()
        (src / "config.yaml").write_bytes(b"canonical")
        conflict = src / "config.sync-conflict-20260424-233316-889e42c0.yaml"
        conflict.write_bytes(b"divergent")

        hits = _find_conflict_files(self._config(src, ["config.yaml"]))
        # Find the hit corresponding to the include_files conflict.
        match = [h for h in hits if h[1] == conflict]
        assert len(match) == 1
        _src_name, _cpath, canonical = match[0]
        assert canonical == src / "config.yaml"

    def test_user_file_with_infix_at_base_not_listed(self, tmp_path: Path) -> None:
        """is_conflict_filename strictness must apply to the new sibling-glob
        path too: a user file like `notes.sync-conflict-log.md` at the source
        base (matching neither stem nor strict pattern) must not be listed."""
        src = tmp_path / "gstack"
        src.mkdir()
        (src / "config.yaml").write_bytes(b"canonical")
        # User-created file at base with .sync-conflict- substring but no timestamp.
        user_file = src / "notes.sync-conflict-log.md"
        user_file.write_bytes(b"legitimate user file")

        hits = _find_conflict_files(self._config(src, ["config.yaml"]))
        hit_paths = [h[1] for h in hits]
        assert user_file not in hit_paths

    def test_unrelated_include_file_stem_does_not_collide(self, tmp_path: Path) -> None:
        """Glob is stem-anchored: a conflict for `other.yaml` is not picked up
        when looking for `config.yaml` siblings."""
        src = tmp_path / "gstack"
        src.mkdir()
        (src / "config.yaml").write_bytes(b"a")
        (src / "other.yaml").write_bytes(b"b")
        # Only register config.yaml as include_files; conflict belongs to other.yaml.
        other_conflict = src / "other.sync-conflict-20260424-233316-889e42c0.yaml"
        other_conflict.write_bytes(b"divergent")

        hits = _find_conflict_files(self._config(src, ["config.yaml"]))
        hit_paths = [h[1] for h in hits]
        assert other_conflict not in hit_paths

    def test_include_files_and_include_dirs_both_scanned(self, tmp_path: Path) -> None:
        """End-to-end: a generic source with conflicts in BOTH include_dirs
        (recursive) and include_files (sibling-glob) surfaces both. Pre-fix
        only the include_dirs ones appeared."""
        src = tmp_path / "gstack"
        (src / "projects").mkdir(parents=True)
        # Conflict inside include_dir (recursive scan path).
        (src / "projects" / "proj.md").write_bytes(b"a")
        dir_conflict = src / "projects" / "proj.sync-conflict-20260424-100000-devA1234.md"
        dir_conflict.write_bytes(b"divergent")
        # Conflict at base for include_files entry (sibling-glob path).
        (src / "config.yaml").write_bytes(b"b")
        file_conflict = src / "config.sync-conflict-20260424-200000-devB5678.yaml"
        file_conflict.write_bytes(b"divergent")

        hits = _find_conflict_files(self._config(src, ["config.yaml"]))
        hit_paths = [h[1] for h in hits]
        assert dir_conflict in hit_paths
        assert file_conflict in hit_paths
        assert len(hits) == 2

    def test_gc_reaps_stale_include_files_conflict(self, tmp_path: Path) -> None:
        """End-to-end downstream: `mm gc --conflicts` consumes _find_conflict_files,
        so the scope fix transitively unblocks reaping stale include_files conflicts."""
        src = tmp_path / "gstack"
        src.mkdir()
        (src / "config.yaml").write_bytes(b"canonical")
        old = src / "config.sync-conflict-20000101-000000-devA1234.yaml"
        old.write_bytes(b"old")
        ancient = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(old, (ancient, ancient))

        reaped = _gc_old_conflict_files(
            self._config(src, ["config.yaml"]), dry_run=False, verbose=False
        )
        assert reaped == 1
        assert not old.exists()


class TestFindConflictFilesFalsePositiveGuard:
    """Latent bug fix: _find_conflict_files used a substring check that
    matched user files like `notes.sync-conflict-log.md`, so the reaper
    would silently delete them after CONFLICT_AGE_DAYS. After the
    is_conflict_filename refactor, the strict pattern guards them."""

    def test_user_file_with_infix_no_timestamp_is_not_listed(self, tmp_path: Path) -> None:
        src = tmp_path / "claude"
        memory = src / "projects" / "proj1" / "memory"
        memory.mkdir(parents=True)

        # Real conflict file: matched.
        real_conflict = memory / "notes.sync-conflict-20260421-143055-devA1234.md"
        real_conflict.write_bytes(b"divergent")
        # User file containing the infix but no timestamp: NOT matched.
        user_file = memory / "notes.sync-conflict-log.md"
        user_file.write_bytes(b"legitimate user file")

        config = {
            "sync": {"sources": [{"name": "claude", "path": str(src), "type": "claude"}]},
        }

        hits = _find_conflict_files(config)
        hit_paths = [h[1] for h in hits]
        assert real_conflict in hit_paths
        assert user_file not in hit_paths, (
            "user file with .sync-conflict- but no timestamp must NOT be listed/reapable"
        )


class TestWalkerExcludesConflictRegression:
    """Regression: v0.4.0 shipped conflict-copy creation in cli but the
    walker EXCLUDED list missed the pattern. Result: next push uploaded
    the local conflict file fleet-wide. End-to-end check at the walker
    layer that resolves the regression."""

    def test_resolved_conflict_file_does_not_propagate_on_next_walk(self, tmp_path: Path) -> None:

        # Simulate what _apply_incoming_file leaves on disk after preserving
        # a local divergent version (Syncthing convention).
        claude = tmp_path / ".claude"
        memory = claude / "projects" / "-myapp" / "memory"
        memory.mkdir(parents=True)
        canonical = memory / "notes.md"
        canonical.write_text("remote-resolved content")
        # Use the same helper the CLI uses to ensure naming alignment.
        conflict = conflict_filename(
            canonical, device_id="abc12345dead", now=datetime(2026, 4, 22, 12, tzinfo=timezone.utc)
        )
        conflict.write_text("local divergent content")

        files = walk_claude_source(claude)
        paths = set(files.keys())
        assert "projects/-myapp/memory/notes.md" in paths
        assert all(".sync-conflict-" not in p for p in paths), (
            f"walker uploaded a conflict file: {paths}"
        )


class TestResolveInteractiveLoop:
    """Tests for _resolve_interactive_loop — the interactive picker invoked by
    `mm resolve`. Uses monkeypatch on typer.prompt to simulate user input.
    """

    @staticmethod
    def _make_conflict_pair(tmp_path: Path) -> tuple[Path, Path]:
        """Create a canonical + conflict sibling with distinct content.
        Returns (canonical, conflict)."""
        canonical = tmp_path / "user.md"
        canonical.write_bytes(b"canonical content")
        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict content")
        return canonical, conflict

    def test_keep_canonical_deletes_conflict(self, tmp_path: Path, monkeypatch) -> None:
        """User picks 'c' — conflict file is deleted, canonical preserved."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "c")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.exists()
        assert canonical.read_bytes() == b"canonical content"
        assert not conflict.exists(), "conflict should be deleted"

    def test_force_promotes_conflict_over_canonical(self, tmp_path: Path, monkeypatch) -> None:
        """User picks 'f' — conflict renamed to canonical (overwriting canonical)."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "f")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.exists()
        assert canonical.read_bytes() == b"conflict content", (
            "canonical should now have conflict's content"
        )
        assert not conflict.exists(), "conflict path should be gone (renamed)"

    def test_keep_both_is_noop(self, tmp_path: Path, monkeypatch) -> None:
        """User picks 'b' (default) — both files remain unchanged."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "b")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.read_bytes() == b"canonical content"
        assert conflict.read_bytes() == b"conflict content"

    def test_abort_raises_typer_abort(self, tmp_path: Path, monkeypatch) -> None:
        """User picks 'a' — typer.Abort is raised, subsequent conflicts not processed."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "a")

        with pytest.raises(typer.Abort):
            _resolve_interactive_loop([("s1", conflict, canonical)])

    def test_canonical_missing_promote(self, tmp_path: Path, monkeypatch) -> None:
        """Canonical is gone, user picks 'p' — conflict is renamed to recovered canonical path."""

        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict content")
        expected_canonical = tmp_path / "user.md"
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")

        # canonical is None in the hit tuple because it's missing
        _resolve_interactive_loop([("s1", conflict, None)])

        assert expected_canonical.exists()
        assert expected_canonical.read_bytes() == b"conflict content"
        assert not conflict.exists()

    def test_canonical_missing_delete(self, tmp_path: Path, monkeypatch) -> None:
        """Canonical is gone, user picks 'd' — conflict file is deleted."""

        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict content")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "d")

        _resolve_interactive_loop([("s1", conflict, None)])

        assert not conflict.exists()

    def test_walks_multiple_conflicts(self, tmp_path: Path, monkeypatch) -> None:
        """Given multiple hits, each is prompted sequentially."""

        c1 = tmp_path / "a.md"
        c1.write_bytes(b"a-canon")
        conflict1 = tmp_path / "a.sync-conflict-20260421-143055-devA1234.md"
        conflict1.write_bytes(b"a-conflict")

        c2 = tmp_path / "b.md"
        c2.write_bytes(b"b-canon")
        conflict2 = tmp_path / "b.sync-conflict-20260421-143055-devA1234.md"
        conflict2.write_bytes(b"b-conflict")

        # Return different choices per call: first 'c' (keep canonical), then 'f' (force)
        choices = iter(["c", "f"])
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: next(choices))

        _resolve_interactive_loop([("s1", conflict1, c1), ("s1", conflict2, c2)])

        # First pair: canonical kept, conflict deleted
        assert c1.read_bytes() == b"a-canon"
        assert not conflict1.exists()
        # Second pair: conflict promoted over canonical
        assert c2.read_bytes() == b"b-conflict"
        assert not conflict2.exists()


class TestResolveExitCode:
    """Tests for the (resolved, failed) return shape and resolve()'s exit code.

    Before this change, mid-walk OSError on rename/unlink/read was printed to
    stderr but the command exited 0, leaving CI scripts unable to detect that
    the user still had unresolved conflicts. Now: walk continues through every
    conflict (so the user can triage everything in one pass), and the command
    exits 1 if anything failed.
    """

    def test_loop_returns_resolved_failed_tuple(self, tmp_path: Path, monkeypatch) -> None:
        """Happy path: 1 resolved, 0 failed."""

        canonical = tmp_path / "f.md"
        canonical.write_bytes(b"canon")
        conflict = tmp_path / "f.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict")

        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "c")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (1, 0)

    def test_loop_counts_rename_failure(self, tmp_path: Path, monkeypatch) -> None:
        """Promote choice + rename raises OSError -> failed += 1, walk continues."""

        conflict = tmp_path / "f.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict")
        # canonical=None branch -> 'p' choice -> rename
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")

        def boom(*a, **kw):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(Path, "rename", boom)

        resolved, failed = _resolve_interactive_loop([("s1", conflict, None)])
        assert (resolved, failed) == (0, 1)

    def test_loop_counts_unlink_failure(self, tmp_path: Path, monkeypatch) -> None:
        """Keep-canonical choice + unlink raises OSError -> failed += 1."""

        canonical = tmp_path / "f.md"
        canonical.write_bytes(b"canon")
        conflict = tmp_path / "f.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "c")

        def boom(self):
            raise OSError("simulated unlink failure")

        monkeypatch.setattr(Path, "unlink", boom)

        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (0, 1)

    def test_loop_counts_read_failure(self, tmp_path: Path, monkeypatch) -> None:
        """Read failure during diff display leaves the conflict unresolved -> failed."""

        canonical = tmp_path / "f.md"
        canonical.write_bytes(b"canon")
        conflict = tmp_path / "f.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict")

        def boom(self, *a, **kw):
            raise OSError("simulated read failure")

        monkeypatch.setattr(Path, "read_text", boom)

        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (0, 1)

    def test_loop_mixed_pass_fail_continues_walk(self, tmp_path: Path, monkeypatch) -> None:
        """3 conflicts where the middle one fails. All three get prompted."""

        items = []
        for n in ("a", "b", "c"):
            canonical = tmp_path / f"{n}.md"
            canonical.write_bytes(b"canon")
            conflict = tmp_path / f"{n}.sync-conflict-20260421-143055-devA1234.md"
            conflict.write_bytes(b"conflict")
            items.append(("s1", conflict, canonical))

        # Three 'c' choices. Middle unlink fails.
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "c")
        real_unlink = Path.unlink
        call_idx = {"n": 0}

        def maybe_boom(self):
            call_idx["n"] += 1
            if call_idx["n"] == 2:
                raise OSError("simulated mid-walk failure")
            return real_unlink(self)

        monkeypatch.setattr(Path, "unlink", maybe_boom)

        resolved, failed = _resolve_interactive_loop(items)
        assert resolved == 2
        assert failed == 1

    def test_resolve_command_exits_1_on_any_failure(self, tmp_path: Path, monkeypatch) -> None:
        """End-to-end: resolve walks, encounters one rename failure, exits 1."""

        storage = tmp_path / "storage"
        storage.mkdir()
        claude = tmp_path / ".claude"
        memory = claude / "projects" / "-Users-kb-app" / "memory"
        memory.mkdir(parents=True)
        canonical = memory / "role.md"
        canonical.write_bytes(b"canon")
        conflict = memory / "role.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"conflict")

        cfg_path = tmp_path / "config.toml"
        save_config(
            {
                "device": {"id": "dev-x", "name": "Mac X"},
                "storage": {"path": str(storage)},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [{"name": "claude", "path": str(claude), "type": "claude"}],
                },
                "crypto": {"argon2_memory_kb": 1024},
            },
            cfg_path,
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("mind_meld.config.LOCK_PATH", tmp_path / "lock")
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", tmp_path / "lock")

        # Force 'f' (force conflict -> canonical) and make rename fail.
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "f")

        def boom(*a, **kw):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(Path, "rename", boom)

        result = CliRunner().invoke(app, ["resolve"])
        assert result.exit_code == 1, (result.stdout, result.stderr)


class TestPredictPullOutcome:
    def test_predicts_write_for_missing_local(self, tmp_path: Path) -> None:
        info = _remote_info("xxx", datetime.now(timezone.utc))
        assert _predict_pull_outcome("missing.md", info, tmp_path) == "write"

    def test_predicts_unchanged_for_matching_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "a.md"
        f.write_bytes(b"same")
        info = _remote_info(hash_file(f), datetime.now(timezone.utc))
        assert _predict_pull_outcome("a.md", info, tmp_path) == "unchanged"

    def test_predicts_merge_for_jsonl(self, tmp_path: Path) -> None:
        f = tmp_path / "x.jsonl"
        f.write_bytes(b"line\n")
        info = _remote_info("other", datetime.now(timezone.utc))
        assert _predict_pull_outcome("x.jsonl", info, tmp_path) == "merge"

    def test_predicts_skip_when_local_newer(self, tmp_path: Path) -> None:
        f = tmp_path / "a.md"
        f.write_bytes(b"local")
        _set_mtime(f, datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))
        info = _remote_info("other", datetime(2026, 4, 21, 11, 0, tzinfo=timezone.utc))
        assert _predict_pull_outcome("a.md", info, tmp_path) == "skip"

    def test_predicts_conflict_when_remote_newer(self, tmp_path: Path) -> None:
        f = tmp_path / "a.md"
        f.write_bytes(b"local")
        _set_mtime(f, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))
        info = _remote_info("other", datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))
        assert _predict_pull_outcome("a.md", info, tmp_path) == "conflict"
