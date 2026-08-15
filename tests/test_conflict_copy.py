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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import resolveflow
from mind_meld.cli import (
    CONFLICT_INFIX,
    _apply_incoming_file,
    _predict_pull_outcome,
    _prompt_conflict_choice,
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
from mind_meld.resolveflow import (
    _canonical_for_conflict,
    _find_conflict_files,
    _promote_conflict_file,
    _promote_target_path,
    _resolve_interactive_loop,
)
from mind_meld.retention import _gc_old_conflict_files

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
        """[M] precedes [S]: a newer local .jsonl still merges, not skips.

        Also pins exact line-union set membership: every local line AND every
        remote line is present in the merged output. A weaker substring check
        would let a regression silently drop one side's overlap rows.
        """
        rel = "projects/p1/learnings.jsonl"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b'{"ts":"2026-01-01","key":"a"}\n{"ts":"2026-01-02","key":"shared"}\n')

        # Local is "newer" than remote, but merge is the correct operation.
        _set_mtime(local, datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))

        remote_data = b'{"ts":"2026-01-02","key":"shared"}\n{"ts":"2026-02-01","key":"b"}\n'
        info = _remote_info("bbb", datetime(2026, 1, 1, tzinfo=timezone.utc))

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=remote_data,
            remote_info=info,
            remote_device_id="devA1234",
        )

        assert outcome == "merged"
        merged_lines = set(local.read_bytes().splitlines())
        assert b'{"ts":"2026-01-01","key":"a"}' in merged_lines
        assert b'{"ts":"2026-01-02","key":"shared"}' in merged_lines
        assert b'{"ts":"2026-02-01","key":"b"}' in merged_lines

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

    def test_conflict_writes_remote_to_sidecar_keeps_local_at_canonical(
        self, tmp_path: Path
    ) -> None:
        """[C] Track 5E inversion: local older than remote -> remote bytes
        land in .sync-conflict-* sidecar; local stays at canonical."""
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
        # Canonical UNTOUCHED — still local bytes (post-inversion).
        assert local.read_bytes() == b"local content"
        # Sidecar holds the REMOTE bytes (post-inversion).
        conflicts = list(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert len(conflicts) == 1
        assert conflicts[0].read_bytes() == b"remote content"
        assert conflicts[0].name.endswith(".md")
        # Sidecar filename has NO `v0-` prefix — that prefix is reserved
        # for pre-inversion files migrated by Track 5E's helper.
        assert "sync-conflict-v0-" not in conflicts[0].name

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
        """REGRESSION (post-v0.11.4): re-pulling the same remote bytes for
        an already-conflicted file does NOT accumulate timestamped sidecars.

        Pre-fix behavior: every re-apply stamped `datetime.now()` into a
        fresh filename (`conflict_filename` collision-detected only same-
        SECOND duplicates) so users running `mm pull` against a peer
        that hadn't rebased yet would walk away from a single conflict
        with N timestamped sidecars after N pulls. The screenshot bug
        had three sidecars from one peer at 11:59 / 12:33 / 14:15.

        Post-fix: `_apply_conflict` scans existing sidecars from this
        peer for this canonical and skips the write when bytes match.
        Outcome is still "conflicted" (the conflict still exists on
        disk) -- we just don't add a duplicate.
        """
        rel = "memory/user_role.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))

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

        second = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
        )
        # Outcome is still "conflicted" -- the conflict state persists on
        # disk, we just didn't write a duplicate sidecar.
        assert second == "conflicted"
        conflicts = list(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert len(conflicts) == 1
        assert conflicts[0].read_bytes() == b"remote content"

    def test_pull_replaces_stale_sidecar_when_peer_pushes_new_bytes(self, tmp_path: Path) -> None:
        """Different bytes from the same peer across pulls -> single sidecar
        with the latest content (stale snapshot reaped).

        Without this, peer X pushing R1 then R2 between two pulls would
        leave the user with sidecar(R1) AND sidecar(R2) -- merging the
        stale R1 could resurrect peer-deleted content. The reap keeps
        at most one current-state sidecar per peer.
        """
        rel = "memory/user_role.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))

        info_old = _remote_info(
            hashlib.sha256(b"peer R1").hexdigest(),
            datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        )
        info_new = _remote_info(
            hashlib.sha256(b"peer R2").hexdigest(),
            datetime(2026, 4, 21, 13, 0, tzinfo=timezone.utc),
        )

        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"peer R1",
            remote_info=info_old,
            remote_device_id="devA1234",
        )
        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"peer R2",
            remote_info=info_new,
            remote_device_id="devA1234",
        )

        conflicts = list(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert len(conflicts) == 1
        assert conflicts[0].read_bytes() == b"peer R2"

    def test_dedup_does_not_collapse_sidecars_from_different_peers(self, tmp_path: Path) -> None:
        """Per-peer dedup must NOT cross peers. Two peers with the same
        canonical produce two distinct sidecars (one per device_short).
        """
        rel = "memory/user_role.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))

        info = _remote_info("remotehash", datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))

        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"peer A bytes",
            remote_info=info,
            remote_device_id="devA1234",
        )
        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"peer B bytes",
            remote_info=info,
            remote_device_id="devB5678",
        )

        conflicts = sorted(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert len(conflicts) == 2
        # Each sidecar carries its peer's bytes.
        contents = {c.read_bytes() for c in conflicts}
        assert contents == {b"peer A bytes", b"peer B bytes"}

    def test_dedup_does_not_reap_pre_inversion_sidecar_from_same_peer(self, tmp_path: Path) -> None:
        """Pre-inversion (v0-) sidecars hold LOCAL bytes from a pre-v0.9.2
        conflict and must NEVER be reaped by the apply path -- they
        encode user data the inverted semantics rotated out of canonical.
        Dedup is post-inversion-only.
        """
        rel = "memory/user_role.md"
        local = tmp_path / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))

        # Plant a pre-inversion (v0-) sidecar from devA1234.
        v0_sidecar = local.parent / "user_role.sync-conflict-v0-20260101-100000-devA1234.md"
        v0_sidecar.write_bytes(b"local-bytes-from-pre-inversion-era")

        info = _remote_info("remotehash", datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))
        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"current peer bytes",
            remote_info=info,
            remote_device_id="devA1234",
        )

        # Both sidecars co-exist: v0- (pre-inversion, local bytes) AND a
        # fresh post-inversion sidecar (peer bytes).
        all_sidecars = sorted(local.parent.glob(f"*{CONFLICT_INFIX}*"))
        assert len(all_sidecars) == 2
        v0_files = [s for s in all_sidecars if "sync-conflict-v0-" in s.name]
        post_files = [s for s in all_sidecars if "sync-conflict-v0-" not in s.name]
        assert len(v0_files) == 1
        assert v0_files[0].read_bytes() == b"local-bytes-from-pre-inversion-era"
        assert len(post_files) == 1
        assert post_files[0].read_bytes() == b"current peer bytes"


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
    """Regression for the 2026-04-24 first-pull: a 286-file pull produced 6
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


class TestFindConflictFilesNestedDedup:
    """Track 5D Task 1: dedup pass guards against double-counting when an
    `include_files` entry sits inside an `include_dirs` directory.

    The default config doesn't trigger this (all `include_files` entries
    are bare top-level dotfiles or `retro-context.md`/`greptile-history.md`).
    But a user customizing their gstack source with `include_files:
    ["projects/notes.md"]` AND `include_dirs: ["projects"]` would have
    `_find_conflict_files` visit a `projects/notes.sync-conflict-...md`
    twice — once via the rglob and once via the depth-0 sibling-glob —
    producing duplicate rows in `mm conflicts` and double-counted reaps
    in `mm gc --conflicts`.

    Dedup key is `(src_name, conflict_path)` not bare `Path`: two
    legitimately-overlapping sources must keep distinct rows so each
    source's attribution is preserved.
    """

    def _nested_config(self, src: Path) -> dict:
        return {
            "sync": {
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(src),
                        "type": "generic",
                        "include_dirs": ["projects"],
                        "include_files": ["projects/notes.md"],
                    }
                ]
            }
        }

    def test_nested_include_file_inside_include_dir_dedups(self, tmp_path: Path) -> None:
        """The headline regression pin: a single conflict file at a path
        the rglob AND the sibling-glob both reach is listed exactly once."""
        src = tmp_path / "gstack"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "notes.md").write_bytes(b"canonical")
        conflict = src / "projects" / "notes.sync-conflict-20260425-150000-devA1234.md"
        conflict.write_bytes(b"divergent")

        hits = _find_conflict_files(self._nested_config(src))
        hit_paths = [h[1] for h in hits]
        assert hit_paths.count(conflict) == 1
        assert len(hits) == 1

    def test_dedup_preserves_canonical_resolution(self, tmp_path: Path) -> None:
        """The single deduped row must still report the right canonical
        path. mm resolve relies on this."""
        src = tmp_path / "gstack"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "notes.md").write_bytes(b"canonical")
        conflict = src / "projects" / "notes.sync-conflict-20260425-150000-devA1234.md"
        conflict.write_bytes(b"divergent")

        hits = _find_conflict_files(self._nested_config(src))
        assert len(hits) == 1
        _src_name, _cpath, canonical = hits[0]
        assert canonical == src / "projects" / "notes.md"

    def test_dedup_does_not_collapse_distinct_conflicts(self, tmp_path: Path) -> None:
        """Two distinct conflict files in the overlap zone must remain as
        two distinct rows. Dedup is by `(src_name, path)`, not by canonical."""
        src = tmp_path / "gstack"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "notes.md").write_bytes(b"canonical")
        c1 = src / "projects" / "notes.sync-conflict-20260425-150000-devA1234.md"
        c2 = src / "projects" / "notes.sync-conflict-20260425-160000-devB5678.md"
        c1.write_bytes(b"divergent-1")
        c2.write_bytes(b"divergent-2")

        hits = _find_conflict_files(self._nested_config(src))
        hit_paths = {h[1] for h in hits}
        assert hit_paths == {c1, c2}
        assert len(hits) == 2

    def test_gc_reaps_nested_include_file_conflict_once(self, tmp_path: Path) -> None:
        """End-to-end via _gc_old_conflict_files: pre-fix this returned 2
        (path visited twice, second unlink no-ops via missing_ok=True but
        reaped count was inflated). After dedup it returns 1."""
        src = tmp_path / "gstack"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "notes.md").write_bytes(b"canonical")
        old = src / "projects" / "notes.sync-conflict-20000101-000000-devA1234.md"
        old.write_bytes(b"old")
        ancient = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(old, (ancient, ancient))

        reaped = _gc_old_conflict_files(self._nested_config(src), dry_run=False, verbose=False)
        assert reaped == 1
        assert not old.exists()

    def test_case_mismatched_config_dedups_on_apfs(self, tmp_path: Path) -> None:
        """Group 7 preflight #3 + D6: filesystem-identity dedup correctly
        collapses overlap when user config has case-mismatched paths on a
        case-insensitive volume (APFS default).

        The codex outside-voice review flagged that os.path.normcase is a
        no-op on POSIX — the original D4 fix would have done nothing here.
        (st_dev, st_ino) keying handles the case correctly because both
        paths resolve to the same inode.

        Skips on case-sensitive volumes (Linux, APFS-CS): the test setup
        relies on creating one directory and accessing it through a
        differently-cased path. On case-sensitive FS the alt-cased path
        simply doesn't exist, the test setup degenerates, and the dedup
        invariant isn't being exercised.
        """
        if sys.platform != "darwin":
            pytest.skip("case-insensitive FS test requires APFS default")

        src = tmp_path / "gstack"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "notes.md").write_bytes(b"canonical")
        conflict = src / "projects" / "notes.sync-conflict-20260425-150000-devA1234.md"
        conflict.write_bytes(b"divergent")

        # Verify the test environment is case-insensitive (APFS default).
        # Skip if the user's tmp_path happens to be on a case-sensitive volume.
        alt_case = src / "Projects" / "notes.sync-conflict-20260425-150000-devA1234.md"
        if not alt_case.exists():
            pytest.skip("tmp_path is on a case-sensitive volume")

        config = {
            "sync": {
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(src),
                        "type": "generic",
                        # Case-mismatch between dir and file:
                        "include_dirs": ["Projects"],
                        "include_files": ["projects/notes.md"],
                    }
                ]
            }
        }

        hits = _find_conflict_files(config)
        # On APFS, "Projects" and "projects" are the same directory; the
        # rglob walk and the include_files sibling-glob both reach the
        # same inode; (st_dev, st_ino) dedup collapses to ONE hit.
        assert len(hits) == 1

    def test_dedup_in_lock_protected_migration_path(self, tmp_path: Path, monkeypatch) -> None:
        """`mm pull` and `mm resolve` call the function with
        migrate_pre_inversion=True, which renames pre-inversion files
        mid-scan. A nested-overlap pre-inversion conflict must surface
        exactly once after migration — once renamed by the rglob pass,
        the sibling-glob does not re-discover it under the old name
        (file no longer exists at that path) and the dedup guards
        against any future re-introduction."""
        from mind_meld.resolveflow import _ensure_inversion_marker

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)

        src = tmp_path / "gstack"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "notes.md").write_bytes(b"canonical")
        # Pre-inversion conflict file: backdate mtime so the migration
        # mtime gate (5E ship-fix) treats it as legacy and renames it.
        pre_inversion = src / "projects" / "notes.sync-conflict-20260301-100000-devA1234.md"
        pre_inversion.write_bytes(b"divergent")
        old_mtime = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() - 86400
        os.utime(pre_inversion, (old_mtime, old_mtime))

        # Stamp the inversion marker AFTER backdating so the gate fires.
        marker_ts = _ensure_inversion_marker()
        assert marker_ts is not None
        assert old_mtime < marker_ts

        hits = _find_conflict_files(self._nested_config(src), migrate_pre_inversion=True)
        # Migration renames the file to embed the v0- prefix between the
        # CONFLICT_INFIX and the timestamp segment.
        migrated = src / "projects" / "notes.sync-conflict-v0-20260301-100000-devA1234.md"
        assert migrated.exists()
        assert not pre_inversion.exists()
        # Exactly one row, pointing at the migrated path.
        hit_paths = [h[1] for h in hits]
        assert hit_paths == [migrated]


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
    """Tests for _resolve_interactive_loop — the interactive picker invoked
    by `mm resolve`. Uses monkeypatch on typer.prompt to simulate user input.

    Track 5E (v0.9.2 BREAKING) inverted _apply_conflict: canonical now
    holds LOCAL bytes; .sync-conflict-* holds REMOTE bytes. The dispatch
    in _resolve_interactive_loop is dual-mode by FILENAME PREFIX — files
    with no `v0-` prefix are post-inversion (and (l) unlinks, (r) renames);
    files with `v0-` prefix are pre-inversion-migrated (and (l) renames,
    (r) unlinks). Tests below use no-prefix filenames so they cover the
    post-inversion path; pre-inversion behavior is covered by
    TestResolveInteractiveLoopPreInversion below.
    """

    @staticmethod
    def _make_conflict_pair(tmp_path: Path) -> tuple[Path, Path]:
        """Create a canonical + conflict sidecar pair under post-inversion
        semantics: canonical = local bytes, sidecar = remote bytes.

        Returns (canonical, conflict).
        """
        canonical = tmp_path / "user.md"
        canonical.write_bytes(b"local content")  # post-inversion: canonical = local
        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"remote content")  # post-inversion: sidecar = remote
        return canonical, conflict

    def test_keep_remote_promotes_conflict_to_canonical(self, tmp_path: Path, monkeypatch) -> None:
        """User picks 'r' under post-inversion: sidecar (REMOTE bytes) is
        renamed over canonical, overwriting local."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "r")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.exists()
        assert canonical.read_bytes() == b"remote content", (
            "canonical now holds remote bytes (sidecar promoted over)"
        )
        assert not conflict.exists(), "sidecar should be gone (renamed)"

    def test_keep_local_unlinks_remote_sidecar(self, tmp_path: Path, monkeypatch) -> None:
        """User picks 'l' under post-inversion: canonical IS local
        already, so we just drop the remote sidecar."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.exists()
        assert canonical.read_bytes() == b"local content", "canonical untouched — already local"
        assert not conflict.exists(), "remote sidecar should be unlinked"

    def test_skip_is_noop(self, tmp_path: Path, monkeypatch) -> None:
        """User picks 's' (default) — both files remain unchanged."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

    def test_skip_default_on_enter(self, tmp_path: Path, monkeypatch) -> None:
        """REGRESSION: default key flipped from 'b' to 's' in v0.11.x.
        Empty input (Enter) maps to the default and leaves both files."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        # Simulate an empty submission by returning "" -- typer.prompt
        # would normally substitute the default; we approximate that
        # by returning the default key directly.
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: kw.get("default", ""))

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

    def test_b_alias_warns_then_skips(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Pre-1.0 deprecation alias: 'b' / 'both' map to (s)kip with a
        one-time stderr notice. On-disk effect identical to skip --
        no risk of silent data loss in mapping it through."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "b")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        # Skip semantics: nothing changes on disk.
        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err
        assert "now means 'skip'" in captured.err

    def test_full_word_both_alias_warns_then_skips(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The full word 'both' is also accepted as the deprecation alias."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "both")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err

    def test_back_does_not_trigger_b_alias(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """REGRESSION: alias dispatch is exact-match, not startswith.
        'back', 'browse', 'between' must NOT silently trigger the alias."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "back")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        # Falls through to the unrecognized-input branch -- skip semantics.
        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"
        # No alias notice should fire for 'back'.
        captured = capsys.readouterr()
        assert "mm: notice:" not in captured.err

    def test_abort_raises_typer_abort(self, tmp_path: Path, monkeypatch) -> None:
        """User picks 'a' — typer.Abort is raised, subsequent conflicts not processed."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "a")

        with pytest.raises(typer.Abort):
            _resolve_interactive_loop([("s1", conflict, canonical)])

    def test_old_letter_c_rejected_loudly(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """User pipes legacy 'c' — error to stderr, typer.Exit(1), no mutation."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "c")

        with pytest.raises(typer.Exit) as exc:
            _resolve_interactive_loop([("s1", conflict, canonical)])
        assert exc.value.exit_code == 1

        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

        captured = capsys.readouterr()
        assert "no longer accepted" in captured.err
        assert "(l)ocal" in captured.err
        assert "(r)emote" in captured.err

    def test_old_letter_f_rejected_loudly(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """User pipes legacy 'f' — error to stderr, typer.Exit(1), no mutation."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "f")

        with pytest.raises(typer.Exit) as exc:
            _resolve_interactive_loop([("s1", conflict, canonical)])
        assert exc.value.exit_code == 1

        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

        captured = capsys.readouterr()
        assert "no longer accepted" in captured.err

    def test_full_word_local_does_not_match_lookup(self, tmp_path: Path, monkeypatch) -> None:
        """REGRESSION: dispatch must use exact match, not startswith.
        Pre-fix, typing 'lookup' would silently match (l)ocal."""
        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "lookup")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

    def test_full_word_remote_does_not_match_retry(self, tmp_path: Path, monkeypatch) -> None:
        """REGRESSION: dispatch must use exact match, not startswith."""
        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "retry")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

    def test_cancel_does_not_trigger_legacy_rejection(self, tmp_path: Path, monkeypatch) -> None:
        """REGRESSION: legacy 'c'/'f' rejection uses exact match —
        'cancel' must not be misclassified as legacy 'c'."""
        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "cancel")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"

    def test_full_word_local_alias_works(self, tmp_path: Path, monkeypatch) -> None:
        """User typing 'local' (the full word) keeps local — same as 'l'."""
        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "local")

        _resolve_interactive_loop([("s1", conflict, canonical)])

        # Local kept; remote sidecar dropped.
        assert canonical.read_bytes() == b"local content"
        assert not conflict.exists()

    def test_canonical_missing_promote(self, tmp_path: Path, monkeypatch) -> None:
        """Canonical is gone, user picks 'p' — conflict is renamed to recovered canonical path.

        D7: parallel (p)/(d)/(s) prompt unchanged from prior versions; only
        the surrounding preface was rewritten.
        """

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
        """Given multiple hits, each is prompted sequentially.

        Post-inversion: canonical = local bytes, sidecar = remote bytes.
        """

        c1 = tmp_path / "a.md"
        c1.write_bytes(b"a-local")  # canonical = local (post-inversion)
        conflict1 = tmp_path / "a.sync-conflict-20260421-143055-devA1234.md"
        conflict1.write_bytes(b"a-remote")  # sidecar = remote

        c2 = tmp_path / "b.md"
        c2.write_bytes(b"b-local")
        conflict2 = tmp_path / "b.sync-conflict-20260421-143055-devA1234.md"
        conflict2.write_bytes(b"b-remote")

        # First 'r' (keep remote — promotes sidecar over canonical),
        # then 'l' (keep local — drops sidecar).
        choices = iter(["r", "l"])
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: next(choices))

        _resolve_interactive_loop([("s1", conflict1, c1), ("s1", conflict2, c2)])

        # First pair: 'r' under post-inversion = sidecar promoted over canonical.
        assert c1.read_bytes() == b"a-remote"
        assert not conflict1.exists()
        # Second pair: 'l' under post-inversion = canonical IS local; drop sidecar.
        assert c2.read_bytes() == b"b-local"
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

        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "r")
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

    def test_loop_counts_keep_local_unlink_failure(self, tmp_path: Path, monkeypatch) -> None:
        """Post-inversion: 'l' (keep-local) unlinks the remote sidecar.
        Failure -> failed += 1, walk continues.
        """

        canonical = tmp_path / "f.md"
        canonical.write_bytes(b"local content")
        conflict = tmp_path / "f.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"remote content")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")

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

        # _resolve_interactive_loop reads bytes (for lcs_merge) before
        # decoding for the diff display, so the read trip-point is
        # read_bytes now.
        monkeypatch.setattr(Path, "read_bytes", boom)

        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (0, 1)

    def test_loop_mixed_pass_fail_continues_walk(self, tmp_path: Path, monkeypatch) -> None:
        """3 conflicts where the middle one fails. All three get prompted."""

        items = []
        for n in ("a", "b", "c"):
            canonical = tmp_path / f"{n}.md"
            canonical.write_bytes(b"local content")
            conflict = tmp_path / f"{n}.sync-conflict-20260421-143055-devA1234.md"
            conflict.write_bytes(b"remote content")
            items.append(("s1", conflict, canonical))

        # Three 'l' choices (keep local — drops sidecar under post-inversion).
        # Middle unlink fails.
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")
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
        monkeypatch.setattr("mind_meld.config.LOCK_PATH", tmp_path / "lock")
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", tmp_path / "lock")

        # Post-inversion: 'r' (keep remote) renames sidecar over canonical.
        # Force the rename to fail so resolve exits 1.
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "r")

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


class TestResolveInteractiveLoopNewBehavior:
    """v0.12.0 conflict-prompt-ux additions:
    * color LOCAL/REMOTE banners above the diff
    * device-name attribution on the REMOTE banner
    * three-number divergence summary
    * (b)oth -> (s)kip alias with one-time notice
    * (a)bort leaves all on-disk state unchanged
    """

    @staticmethod
    def _make_conflict_pair(tmp_path: Path) -> tuple[Path, Path]:
        canonical = tmp_path / "user.md"
        canonical.write_bytes(b"local content\n")
        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"remote content\n")
        return canonical, conflict

    def test_device_name_surfaced_on_remote_banner(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Conflict filename's device prefix resolves against the cached
        devices list; banner shows '(from <peer_name>)'."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")
        # Capture Console output by redirecting Rich's console.
        from mind_meld import cli as _cli

        out_lines: list[str] = []
        monkeypatch.setattr(
            _cli.console,
            "print",
            lambda *args, **kw: out_lines.append(" ".join(str(a) for a in args)),
        )

        devices = [{"device_id": "devA1234", "device_name": "kb-mbp"}]
        _resolve_interactive_loop([("s1", conflict, canonical)], devices)

        joined = "\n".join(out_lines)
        assert "from " in joined and "kb-mbp" in joined

    def test_unknown_peer_fallback_when_no_match(self, tmp_path: Path, monkeypatch) -> None:
        """Conflict filename's device prefix doesn't match any registered
        peer -- banner falls back to '(unknown peer)'."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")
        from mind_meld import cli as _cli

        out_lines: list[str] = []
        monkeypatch.setattr(
            _cli.console,
            "print",
            lambda *args, **kw: out_lines.append(" ".join(str(a) for a in args)),
        )

        devices = [{"device_id": "deadbeef", "device_name": "Other Mac"}]
        _resolve_interactive_loop([("s1", conflict, canonical)], devices)

        joined = "\n".join(out_lines)
        assert "unknown peer" in joined
        assert "Other Mac" not in joined

    def test_ambiguous_prefix_renders_in_banner(self, tmp_path: Path, monkeypatch) -> None:
        """Two registered peers share the conflict's 8-char prefix;
        banner annotates 'ambiguous -- N peers match this prefix'
        (T4 cross-model finding) and refuses to attribute either name."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")
        # Reset the per-process notice cache so capsys sees the stderr.
        from mind_meld import cli as _cli
        from mind_meld import devices as _devices

        monkeypatch.setattr(_devices, "_AMBIGUOUS_PREFIX_NOTICED", set())

        out_lines: list[str] = []
        monkeypatch.setattr(
            _cli.console,
            "print",
            lambda *args, **kw: out_lines.append(" ".join(str(a) for a in args)),
        )

        # Both these IDs share the "devA1234" prefix the test conflict
        # filename uses -- collision against the lookup helper.
        devices = [
            {"device_id": "devA1234", "device_name": "Mac A"},
            {"device_id": "devA1234alt", "device_name": "Mac B"},
        ]
        _resolve_interactive_loop([("s1", conflict, canonical)], devices)

        joined = "\n".join(out_lines)
        assert "ambiguous" in joined
        assert "2 peers match" in joined
        # Neither peer name should leak (we refused to attribute).
        assert "Mac A" not in joined
        assert "Mac B" not in joined

    def test_divergence_summary_shows_three_numbers(self, tmp_path: Path, monkeypatch) -> None:
        """Pre-diff summary gives M / N / K so the user sees scale."""

        canonical = tmp_path / "user.md"
        canonical.write_bytes(b"line1\nline2\n")
        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        # 1 line replaced + 1 new line added on the remote side.
        conflict.write_bytes(b"line1\nLINE2-CHANGED\nline3\n")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")
        from mind_meld import cli as _cli

        out_lines: list[str] = []
        monkeypatch.setattr(
            _cli.console,
            "print",
            lambda *args, **kw: out_lines.append(" ".join(str(a) for a in args)),
        )

        _resolve_interactive_loop([("s1", conflict, canonical)])

        joined = "\n".join(out_lines)
        # Summary names the user's lines and the peer's lines explicitly
        # so the count is mode-correct in pre_inversion too (post-clarity
        # rename from the older "removed-or-replaced on local side" copy
        # which was wrong in pre_inversion).
        assert "of yours" in joined
        assert "from peer" in joined
        assert "total diff lines" in joined

    def test_remote_overwrites_only_after_typed_confirmation(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """ROLLBACK regression (T7 / codex): pressing Enter must NOT
        promote remote bytes. Default is (s)kip; only an explicit 'r'
        triggers the destructive overwrite."""

        canonical, conflict = self._make_conflict_pair(tmp_path)
        # Simulate Enter by returning the default key the prompt was
        # configured with.
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: kw.get("default", ""))

        _resolve_interactive_loop([("s1", conflict, canonical)])

        # Default is 's' -- no overwrite.
        assert canonical.read_bytes() == b"local content\n"
        assert conflict.read_bytes() == b"remote content\n"

    def test_abort_leaves_all_files_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        """ROLLBACK regression: 'a' must leave every conflict in the
        walk untouched, including subsequent unprocessed conflicts."""

        canonical1, conflict1 = self._make_conflict_pair(tmp_path)
        canonical2 = tmp_path / "second.md"
        canonical2.write_bytes(b"local2 content\n")
        conflict2 = tmp_path / "second.sync-conflict-20260421-143056-devA1234.md"
        conflict2.write_bytes(b"remote2 content\n")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "a")

        with pytest.raises(typer.Abort):
            _resolve_interactive_loop(
                [
                    ("s1", conflict1, canonical1),
                    ("s1", conflict2, canonical2),
                ]
            )

        assert canonical1.read_bytes() == b"local content\n"
        assert conflict1.read_bytes() == b"remote content\n"
        assert canonical2.read_bytes() == b"local2 content\n"
        assert conflict2.read_bytes() == b"remote2 content\n"


class TestResolveLocalMtimeBump:
    """``mm resolve`` picking (l)ocal MUST bump canonical's mtime past peer's.

    Without the bump, the user's "I picked local" decision is silent: canonical
    keeps its old (<= peer's) mtime, the next pull from the same peer re-hits
    the conflict path (sidecar dedup signal was just deleted), and the user is
    stuck in a resolve -> pull -> resolve -> pull loop. With the bump, the next
    pull's mtime gate sees local_mtime > remote_mtime and skips, and the next
    push broadcasts local-as-authoritative to other peers in the fleet so they
    converge.

    Pinned at the resolve seam (not the apply seam) because the bump is the
    resolve decision propagating; the apply path is unchanged.
    """

    def test_post_inversion_local_bumps_canonical_mtime_past_sidecar(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Post-inversion (l)ocal: canonical stays, sidecar dropped. mtime
        on canonical must end up strictly greater than the sidecar's mtime
        so the next pull from this peer hits the mtime-skip branch."""
        canonical = tmp_path / "f.md"
        canonical.write_bytes(b"local content")
        _set_mtime(canonical, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))
        peer_mtime_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)
        conflict = tmp_path / "f.sync-conflict-20260512-122031-3a6c7dc9.md"
        conflict.write_bytes(b"remote content")
        _set_mtime(conflict, peer_mtime_dt)

        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])

        assert (resolved, failed) == (1, 0)
        assert canonical.exists()
        assert not conflict.exists()
        assert canonical.stat().st_mtime > peer_mtime_dt.timestamp()

    def test_pre_inversion_local_bumps_canonical_mtime_past_pre_inversion_remote(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Pre-inversion (l)ocal: sidecar (holds local bytes) renames onto
        canonical (held remote bytes). Bump must read peer's mtime from the
        ORIGINAL canonical before rename, then stamp the new canonical past
        it. Without the bump, the renamed file inherits the pre-v0.9.2
        sidecar's mtime (years-old) -- guaranteed older than any peer's,
        so next pull always re-conflicts."""
        canonical = tmp_path / "f.md"
        canonical.write_bytes(b"old remote content")
        peer_mtime_dt = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
        _set_mtime(canonical, peer_mtime_dt)

        # v0- prefix marks pre-inversion: sidecar holds local bytes.
        conflict = tmp_path / "f.sync-conflict-v0-20250101-120000-devA1234.md"
        conflict.write_bytes(b"local content")
        ancient = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        _set_mtime(conflict, ancient)

        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])

        assert (resolved, failed) == (1, 0)
        assert canonical.read_bytes() == b"local content"
        assert not conflict.exists()
        # Bumped past peer's mtime AND past the pre-inversion sidecar's mtime.
        assert canonical.stat().st_mtime > peer_mtime_dt.timestamp()
        assert canonical.stat().st_mtime > ancient.timestamp()

    def test_resolve_local_then_pull_same_peer_skips_instead_of_reconflicting(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """End-to-end loop closure: resolve(local) -> simulated next pull from
        the same peer with unchanged bytes -> _apply_incoming_file returns
        'skipped' (mtime gate), NOT 'conflicted'.

        This is the regression pin for the resolve -> pull -> resolve -> pull
        loop that the bump fixes. Pre-fix this assertion would fail with
        outcome == 'conflicted'.
        """
        rel = "f.md"
        canonical = tmp_path / rel
        canonical.write_bytes(b"local content")
        _set_mtime(canonical, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))
        peer_mtime_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)
        conflict = tmp_path / "f.sync-conflict-20260512-122031-devA1234.md"
        conflict.write_bytes(b"remote content")
        _set_mtime(conflict, peer_mtime_dt)

        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (1, 0)

        # Peer pushes again with same bytes + same manifest mtime.
        remote_sha = hashlib.sha256(b"remote content").hexdigest()
        info = _remote_info(remote_sha, peer_mtime_dt)
        outcome = _apply_incoming_file(
            local_path=canonical,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
        )
        assert outcome == "skipped"
        # No new sidecar was created.
        assert list(tmp_path.glob(f"*{CONFLICT_INFIX}*")) == []

    def test_inline_keep_local_no_dict_records_nothing_and_no_mutation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Back-compat (Track 12A): `_apply_incoming_file` with
        `pending_inline_bumps=None` (the non-interactive / direct-call-test
        default) MUST neither record a bump nor mutate canonical's mtime.
        The mid-walk no-mutation guarantee from the v0.12.6 revert still holds
        at the apply seam — the bump moved entirely to `_pull_core`'s
        end-of-batch drain."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        original_mtime_dt = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
        _set_mtime(local, original_mtime_dt)
        peer_mtime_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)
        info = _remote_info(hashlib.sha256(b"remote content").hexdigest(), peer_mtime_dt)

        from mind_meld import cli as cli_module

        monkeypatch.setattr(
            cli_module,
            "_prompt_conflict_choice",
            lambda *a, **kw: ("keep-canonical", None),
        )

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
            interactive_resolve=True,
        )
        assert outcome == "skipped"
        # Canonical's mtime must be unchanged from the original so later
        # peers in the same pull walk see the same mtime gate result.
        assert local.stat().st_mtime == pytest.approx(original_mtime_dt.timestamp())

    def test_inline_keep_local_records_bump_but_does_not_mutate_mid_walk(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Core of Track 12A: keep-canonical with a `pending_inline_bumps`
        dict passed RECORDS the peer's mtime (keyed on the resolved path) but
        leaves canonical's mtime untouched. The bump is deferred to the
        end-of-batch drain so every later peer in the walk is judged against
        the same original-local baseline."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        original_mtime_dt = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
        _set_mtime(local, original_mtime_dt)
        peer_mtime_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)
        info = _remote_info(hashlib.sha256(b"remote content").hexdigest(), peer_mtime_dt)

        from mind_meld import cli as cli_module

        monkeypatch.setattr(
            cli_module,
            "_prompt_conflict_choice",
            lambda *a, **kw: ("keep-canonical", None),
        )

        bumps: dict[Path, float] = {}
        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert outcome == "skipped"
        # Recorded under the resolved path, value == peer's mtime.
        assert bumps == {local.resolve(): pytest.approx(peer_mtime_dt.timestamp())}
        # Canonical's mtime is NOT mutated mid-walk.
        assert local.stat().st_mtime == pytest.approx(original_mtime_dt.timestamp())

    def test_inline_keep_local_records_zero_when_remote_mtime_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex #7 / Issue 2: when the peer's manifest mtime is missing (or
        malformed), the conflict path still fires but `remote_mtime` is None.
        keep-canonical records `0.0` — mirroring the resolve-side
        stat-failure degradation — so the drain still bumps to ~now. (Honest
        limit: a peer with no parseable mtime never reaches the mtime gate, so
        the bump can't close the loop for THAT peer — but recording 0.0 is
        still the correct, non-crashing mechanical choice.)"""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        original_mtime_dt = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
        _set_mtime(local, original_mtime_dt)
        # Manifest entry with NO mtime key.
        info = {"sha256": hashlib.sha256(b"remote content").hexdigest(), "size": 0}

        from mind_meld import cli as cli_module

        monkeypatch.setattr(
            cli_module,
            "_prompt_conflict_choice",
            lambda *a, **kw: ("keep-canonical", None),
        )

        bumps: dict[Path, float] = {}
        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote content",
            remote_info=info,
            remote_device_id="devA1234",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert outcome == "skipped"
        assert bumps == {local.resolve(): 0.0}

    def test_inline_keep_local_two_peers_same_path_records_max(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Cross-peer max(): when two peers conflict on the same file in one
        pull walk and the user picks keep-canonical for both, the dict holds
        `max(peerA_mtime, peerB_mtime)` so the end-of-batch bump beats every
        peer walked — regardless of walk order."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))
        older = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
        newer = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)

        from mind_meld import cli as cli_module

        monkeypatch.setattr(
            cli_module,
            "_prompt_conflict_choice",
            lambda *a, **kw: ("keep-canonical", None),
        )

        bumps: dict[Path, float] = {}
        # Walk order: NEWER peer first, then OLDER — max() must still win.
        for peer_dt, dev in ((newer, "devA1234"), (older, "devB5678")):
            info = _remote_info(hashlib.sha256(b"remote content").hexdigest(), peer_dt)
            _apply_incoming_file(
                local_path=local,
                rel_path=rel,
                plain_data=b"remote content",
                remote_info=info,
                remote_device_id=dev,
                interactive_resolve=True,
                pending_inline_bumps=bumps,
                resolved_local=local.resolve(),
            )
        assert bumps == {local.resolve(): pytest.approx(newer.timestamp())}

    def test_drain_inline_bumps_bumps_canonical_past_peer_mtime(self, tmp_path: Path) -> None:
        """`_drain_inline_bumps` stamps each recorded canonical with an mtime
        strictly greater than the recorded peer mtime, and future-clamps at
        now+60s (inherited from `_bump_canonical_mtime_post_resolve`)."""
        from mind_meld.cli import _drain_inline_bumps

        canonical = tmp_path / "f.md"
        canonical.write_bytes(b"local content")
        _set_mtime(canonical, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))
        peer_mtime = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc).timestamp()

        # Far-future peer mtime on a second file proves the clamp still applies.
        future_file = tmp_path / "g.md"
        future_file.write_bytes(b"local content")
        _set_mtime(future_file, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))
        far_future = time.time() + 3600.0

        before = time.time()
        _drain_inline_bumps({canonical: peer_mtime, future_file: far_future})
        after = time.time()

        assert canonical.stat().st_mtime > peer_mtime
        # future_file clamped to [before, after + 60s].
        assert before <= future_file.stat().st_mtime <= after + 60.0

        # None / empty are no-ops (non-interactive pull).
        _drain_inline_bumps(None)
        _drain_inline_bumps({})

    def test_inline_keep_remote_after_keep_local_pops_pending_bump(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression pin (Issue 1 / Codex #1): a later peer's keep-remote on
        the same file invalidates an earlier peer's pending keep-canonical
        bump. Without the pop, the end-of-batch drain would bump a file that
        now holds the later peer's REMOTE bytes — broadcasting remote bytes as
        locally-authored and silently mtime-skipping the first peer's future
        divergence.

        Post-restructure: _apply_incoming_file returns the write outcome and
        _download_and_apply gates invalidation on it via _CANONICAL_WRITE_OUTCOMES.
        This test exercises the real _apply_incoming_file plus the real gate
        primitive — the integration is pinned separately by
        test_download_and_apply_outcome_gate_invalidates_on_canonical_write."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))
        peer_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)

        from mind_meld import cli as cli_module

        bumps: dict[Path, float] = {}

        # Peer A: keep-canonical -> records.
        monkeypatch.setattr(
            cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("keep-canonical", None)
        )
        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote A",
            remote_info=_remote_info(hashlib.sha256(b"remote A").hexdigest(), peer_dt),
            remote_device_id="devA1234",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert local.resolve() in bumps

        # Peer B: keep-remote -> writes canonical, pops the pending bump.
        monkeypatch.setattr(
            cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("keep-remote", None)
        )
        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote B",
            remote_info=_remote_info(hashlib.sha256(b"remote B").hexdigest(), peer_dt),
            remote_device_id="devB5678",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert outcome == "written"
        assert local.read_bytes() == b"remote B"
        # The contract: "written" is a canonical-mutating outcome; the gate
        # in _download_and_apply pops on it.
        assert outcome in cli_module._CANONICAL_WRITE_OUTCOMES
        cli_module._invalidate_inline_bump(bumps, local.resolve())
        assert bumps == {}

    def test_inline_merge_after_keep_local_pops_pending_bump(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression pin (Issue 1 / Codex #1): a later peer's inline merge on
        the same file invalidates an earlier pending keep-canonical bump —
        canonical now holds merged bytes, so the bump is void."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))
        peer_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)

        from mind_meld import cli as cli_module

        bumps: dict[Path, float] = {}

        monkeypatch.setattr(
            cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("keep-canonical", None)
        )
        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote A",
            remote_info=_remote_info(hashlib.sha256(b"remote A").hexdigest(), peer_dt),
            remote_device_id="devA1234",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert local.resolve() in bumps

        # Peer B: merge -> writes merged bytes to canonical, pops the bump.
        monkeypatch.setattr(
            cli_module,
            "_prompt_conflict_choice",
            lambda *a, **kw: ("merge", b"merged content"),
        )
        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote B",
            remote_info=_remote_info(hashlib.sha256(b"remote B").hexdigest(), peer_dt),
            remote_device_id="devB5678",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert outcome == "merged-via-lcs"
        assert local.read_bytes() == b"merged content"
        assert outcome in cli_module._CANONICAL_WRITE_OUTCOMES
        cli_module._invalidate_inline_bump(bumps, local.resolve())
        assert bumps == {}

    def test_inline_keep_both_after_keep_local_pops_pending_bump(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression pin (T1-A / Codex #1): a later peer's keep-both on the
        same file invalidates an earlier pending keep-canonical bump. keep-both
        leaves the file UNRESOLVED (sidecar on disk); bumping canonical past
        that peer would silently mtime-resolve a conflict the user explicitly
        left open."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))
        peer_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)

        from mind_meld import cli as cli_module

        bumps: dict[Path, float] = {}

        monkeypatch.setattr(
            cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("keep-canonical", None)
        )
        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote A",
            remote_info=_remote_info(hashlib.sha256(b"remote A").hexdigest(), peer_dt),
            remote_device_id="devA1234",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert local.resolve() in bumps

        # Peer B: keep-both -> falls through to _apply_conflict, pops the bump.
        monkeypatch.setattr(
            cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("keep-both", None)
        )
        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote B",
            remote_info=_remote_info(hashlib.sha256(b"remote B").hexdigest(), peer_dt),
            remote_device_id="devB5678",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert outcome == "conflicted"
        assert outcome in cli_module._CANONICAL_WRITE_OUTCOMES
        cli_module._invalidate_inline_bump(bumps, local.resolve())
        assert bumps == {}

    def test_inline_failed_keep_remote_write_does_not_pop_pending_bump(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression pin (T1-A success-only / Codex #3): if keep-remote's
        write FAILS, canonical is still local — the prior keep-canonical bump
        must NOT be popped. The propagation decision stands; popping on a
        failed write would silently drop it."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))
        peer_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)

        from mind_meld import cli as cli_module

        bumps: dict[Path, float] = {}

        monkeypatch.setattr(
            cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("keep-canonical", None)
        )
        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote A",
            remote_info=_remote_info(hashlib.sha256(b"remote A").hexdigest(), peer_dt),
            remote_device_id="devA1234",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert local.resolve() in bumps

        # Peer B: keep-remote, but the write blows up.
        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", boom)
        monkeypatch.setattr(
            cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("keep-remote", None)
        )
        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote B",
            remote_info=_remote_info(hashlib.sha256(b"remote B").hexdigest(), peer_dt),
            remote_device_id="devB5678",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert outcome == "failed"
        assert local.read_bytes() == b"local content"  # write failed, local kept
        # "failed" is intentionally absent from the gate set; the gate would
        # not pop on this outcome, so the prior keep-canonical decision stands.
        assert outcome not in cli_module._CANONICAL_WRITE_OUTCOMES
        assert bumps == {local.resolve(): pytest.approx(peer_dt.timestamp())}  # NOT popped

    def test_inline_failed_merge_write_does_not_pop_pending_bump(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression pin (testing specialist finding): the merge branch has
        the same success-only invalidation contract as keep-remote. If the
        merge write fails, canonical is still pure local, the prior keep-
        canonical decision stands, and the dict entry must be retained.

        The gate test ("failed" not in _CANONICAL_WRITE_OUTCOMES) makes the
        invariant explicit so a future refactor of the gate set can't silently
        regress it."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))
        peer_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)

        from mind_meld import cli as cli_module

        bumps: dict[Path, float] = {}

        monkeypatch.setattr(
            cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("keep-canonical", None)
        )
        _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote A",
            remote_info=_remote_info(hashlib.sha256(b"remote A").hexdigest(), peer_dt),
            remote_device_id="devA1234",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert local.resolve() in bumps

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", boom)
        monkeypatch.setattr(
            cli_module,
            "_prompt_conflict_choice",
            lambda *a, **kw: ("merge", b"merged content"),
        )
        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote B",
            remote_info=_remote_info(hashlib.sha256(b"remote B").hexdigest(), peer_dt),
            remote_device_id="devB5678",
            interactive_resolve=True,
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        assert outcome == "failed"
        assert local.read_bytes() == b"local content"
        assert outcome not in cli_module._CANONICAL_WRITE_OUTCOMES
        assert bumps == {local.resolve(): pytest.approx(peer_dt.timestamp())}

    def test_apply_write_after_keep_local_pops_pending_bump(self, tmp_path: Path) -> None:
        """Regression pin (Codex adversarial HIGH): peer A picks keep-canonical
        on path P (records a bump). The canonical file is deleted before peer B
        is walked (e.g., user `rm`'d it while the blocking prompt waited, or an
        autopull race). Peer B's _apply_incoming_file hits the [W] branch
        (`not local_path.exists()`) and _apply_write writes peer B's REMOTE
        bytes to canonical, returning "written". Without invalidation on that
        outcome, the end-of-batch drain would bump peer B's remote bytes as if
        locally-authored — silent cross-fleet corruption. The outcome-based
        gate at the _download_and_apply seam invalidates on "written" uniformly,
        whether it came from keep-remote OR the file-vanished _apply_write path."""
        rel = "f.md"
        local = tmp_path / rel
        local.write_bytes(b"local content")
        _set_mtime(local, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))
        peer_dt = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc)
        info = _remote_info(hashlib.sha256(b"remote B").hexdigest(), peer_dt)

        from mind_meld import cli as cli_module

        bumps: dict[Path, float] = {local.resolve(): peer_dt.timestamp()}
        # File vanishes between peer A's prompt and peer B's apply.
        local.unlink()

        outcome = _apply_incoming_file(
            local_path=local,
            rel_path=rel,
            plain_data=b"remote B",
            remote_info=info,
            remote_device_id="devB5678",
            pending_inline_bumps=bumps,
            resolved_local=local.resolve(),
        )
        # _apply_write fired (local was missing).
        assert outcome == "written"
        assert local.read_bytes() == b"remote B"
        # "written" is in the gate set regardless of which branch produced it.
        assert outcome in cli_module._CANONICAL_WRITE_OUTCOMES
        cli_module._invalidate_inline_bump(bumps, local.resolve())
        assert bumps == {}

    def test_pull_core_abort_skips_end_of_batch_drain(self, tmp_path: Path, monkeypatch) -> None:
        """Regression pin (T2-A / Codex #6): a `typer.Abort()` raised during
        the pull walk propagates past the end-of-batch drain (which lives
        inside `_pull_core`'s try block). Recorded keep-canonical decisions
        are NOT applied — abort means the user does not trust this pull, so
        half-made decisions are not broadcast to the fleet."""
        from mind_meld import cli as cli_module
        from mind_meld.cli import ManifestFetch, _pull_core

        (tmp_path / "storage").mkdir()
        (tmp_path / "claude").mkdir()
        canonical = tmp_path / "claude" / "test.md"
        canonical.write_bytes(b"local content")
        original_dt = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
        _set_mtime(canonical, original_dt)

        config = {
            "device": {"id": "selfdev"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "sources": [
                    {
                        "name": "claude",
                        "type": "claude",
                        "path": str(tmp_path / "claude"),
                        "max_file_size": 1024,
                    }
                ]
            },
            "crypto": {"argon2_memory_kb": 1024},
        }

        def fake_list_devices_warn(b):
            return [
                {"device_id": "selfdev", "device_name": "me"},
                {"device_id": "peer", "device_name": "Peer"},
            ]

        def fake_fetch_remote_manifest(b, did, pp, mk):
            if did == "peer":
                return ManifestFetch(
                    status="ok",
                    manifest={
                        "sources": {
                            "claude": {
                                "files": {
                                    "test.md": {
                                        "sha256": "differs",
                                        "size": 5,
                                        "mtime": "2026-05-09T08:48:00Z",
                                    }
                                }
                            }
                        },
                        "tombstones": {},
                    },
                )
            return ManifestFetch(status="missing")

        def aborting_download_and_apply(b, bp, td, did, pp, mk, **kw):
            # Simulate: user picked keep-canonical (records a bump), then
            # aborted on the next file.
            bumps = kw.get("pending_inline_bumps")
            if bumps is not None:
                bumps[canonical.resolve()] = datetime(
                    2026, 5, 9, 8, 48, tzinfo=timezone.utc
                ).timestamp()
            raise typer.Abort()

        monkeypatch.setattr(cli_module, "_list_devices_warn", fake_list_devices_warn)
        monkeypatch.setattr(cli_module, "_fetch_remote_manifest", fake_fetch_remote_manifest)
        monkeypatch.setattr(cli_module, "_download_and_apply", aborting_download_and_apply)
        monkeypatch.setattr(cli_module, "get_backend", lambda c: None)
        monkeypatch.setattr(cli_module, "_check_fleet_version_or_refuse", lambda *a, **kw: None)
        monkeypatch.setattr(resolveflow, "_find_conflict_files", lambda *a, **kw: [])
        monkeypatch.setattr(cli_module, "collect_tombstones", lambda *a, **kw: {})

        with pytest.raises(typer.Abort):
            _pull_core(
                config=config,
                passphrase="pp",
                memory_kb=1024,
                conflict_mode="prompt",
            )
        # Drain was skipped — canonical's mtime is untouched.
        assert canonical.stat().st_mtime == pytest.approx(original_dt.timestamp())

    def test_pull_core_drains_pending_bumps_after_device_loop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """End-to-end (Test 5 integration): on a clean interactive pull,
        `_pull_core` drains the shared `pending_inline_bumps` dict after the
        device loop — canonical's mtime ends up bumped past the recorded peer
        mtime."""
        from mind_meld import cli as cli_module
        from mind_meld.cli import ManifestFetch, _pull_core

        (tmp_path / "storage").mkdir()
        (tmp_path / "claude").mkdir()
        canonical = tmp_path / "claude" / "test.md"
        canonical.write_bytes(b"local content")
        _set_mtime(canonical, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))
        peer_mtime = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc).timestamp()

        config = {
            "device": {"id": "selfdev"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "sources": [
                    {
                        "name": "claude",
                        "type": "claude",
                        "path": str(tmp_path / "claude"),
                        "max_file_size": 1024,
                    }
                ]
            },
            "crypto": {"argon2_memory_kb": 1024},
        }

        def fake_list_devices_warn(b):
            return [
                {"device_id": "selfdev", "device_name": "me"},
                {"device_id": "peer", "device_name": "Peer"},
            ]

        def fake_fetch_remote_manifest(b, did, pp, mk):
            if did == "peer":
                return ManifestFetch(
                    status="ok",
                    manifest={
                        "sources": {
                            "claude": {
                                "files": {
                                    "test.md": {
                                        "sha256": "differs",
                                        "size": 5,
                                        "mtime": "2026-05-09T08:48:00Z",
                                    }
                                }
                            }
                        },
                        "tombstones": {},
                    },
                )
            return ManifestFetch(status="missing")

        def keep_local_download_and_apply(b, bp, td, did, pp, mk, **kw):
            # Simulate keep-canonical: record the bump, don't touch canonical.
            bumps = kw.get("pending_inline_bumps")
            assert bumps is not None  # interactive pull -> dict is allocated
            bumps[canonical.resolve()] = peer_mtime
            return 0, {
                "written": [],
                "merged": [],
                "merged-via-lcs": [],
                "skipped": ["test.md"],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            }

        monkeypatch.setattr(cli_module, "_list_devices_warn", fake_list_devices_warn)
        monkeypatch.setattr(cli_module, "_fetch_remote_manifest", fake_fetch_remote_manifest)
        monkeypatch.setattr(cli_module, "_download_and_apply", keep_local_download_and_apply)
        monkeypatch.setattr(cli_module, "get_backend", lambda c: None)
        monkeypatch.setattr(cli_module, "_check_fleet_version_or_refuse", lambda *a, **kw: None)
        monkeypatch.setattr(resolveflow, "_find_conflict_files", lambda *a, **kw: [])
        monkeypatch.setattr(cli_module, "collect_tombstones", lambda *a, **kw: {})
        monkeypatch.setattr(cli_module, "_cleanup_conflict_copies", lambda *a, **kw: None)

        _pull_core(
            config=config,
            passphrase="pp",
            memory_kb=1024,
            conflict_mode="prompt",
            quiet=True,
        )
        # Drain ran after the device loop — canonical bumped past the peer.
        assert canonical.stat().st_mtime > peer_mtime

    def test_download_and_apply_outcome_gate_invalidates_on_canonical_write(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Integration pin for the outcome-gated invalidation in
        _download_and_apply (the seam that owns the eligibility invariant
        post-restructure): when _apply_incoming_file returns an outcome in
        _CANONICAL_WRITE_OUTCOMES, the corresponding entry in
        pending_inline_bumps is popped. When it returns 'failed' or 'skipped',
        the entry is retained. Drives the real _download_and_apply with a
        MagicMock backend + monkeypatched decrypt + monkeypatched
        _apply_incoming_file so the gate is exercised, not mocked."""
        from unittest.mock import MagicMock

        from mind_meld import cli as cli_module
        from mind_meld.cli import _download_and_apply

        rel = "f.md"
        canonical = tmp_path / rel
        canonical.write_bytes(b"local content")
        resolved = canonical.resolve()
        peer_mtime = datetime(2026, 5, 9, 8, 48, tzinfo=timezone.utc).timestamp()
        info = {
            "sha256": hashlib.sha256(b"remote").hexdigest(),
            "size": 6,
            "mtime": "2026-05-09T08:48:00Z",
        }

        backend = MagicMock()
        backend.get = MagicMock(return_value=b"encrypted-blob-bytes")
        monkeypatch.setattr(cli_module, "decrypt", lambda enc, pp, mk: b"remote")

        # Outcome "written" -> in the gate set -> pops.
        bumps = {resolved: peer_mtime}
        monkeypatch.setattr(cli_module, "_apply_incoming_file", lambda **kw: "written")
        _download_and_apply(
            backend,
            tmp_path,
            {rel: info},
            "peerB",
            "pp",
            1024,
            quiet=True,
            pending_inline_bumps=bumps,
        )
        assert bumps == {}

        # Outcome "failed" -> NOT in the gate set -> entry retained.
        bumps = {resolved: peer_mtime}
        monkeypatch.setattr(cli_module, "_apply_incoming_file", lambda **kw: "failed")
        _download_and_apply(
            backend,
            tmp_path,
            {rel: info},
            "peerB",
            "pp",
            1024,
            quiet=True,
            pending_inline_bumps=bumps,
        )
        assert bumps == {resolved: peer_mtime}

        # Outcome "skipped" -> NOT in the gate set -> entry retained
        # (also the keep-canonical RECORD outcome — must not self-invalidate).
        bumps = {resolved: peer_mtime}
        monkeypatch.setattr(cli_module, "_apply_incoming_file", lambda **kw: "skipped")
        _download_and_apply(
            backend,
            tmp_path,
            {rel: info},
            "peerB",
            "pp",
            1024,
            quiet=True,
            pending_inline_bumps=bumps,
        )
        assert bumps == {resolved: peer_mtime}

    def test_post_inversion_local_mtime_capped_at_now_plus_60s(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Future-clamp symmetry: if peer's sidecar mtime is way in the future
        (e.g. set by a buggy/malicious peer that bypassed _restore_mtime_best_effort),
        the bump must cap at now + 60s so downstream peers don't have to
        clamp our pushed mtime. Mirrors the _restore_mtime_best_effort cap."""
        canonical = tmp_path / "f.md"
        canonical.write_bytes(b"local content")
        _set_mtime(canonical, datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))
        conflict = tmp_path / "f.sync-conflict-20260512-122031-devA1234.md"
        conflict.write_bytes(b"remote content")
        # Sidecar mtime 1 hour in the future (far past the 60s clamp).
        far_future = time.time() + 3600.0
        os.utime(conflict, (far_future, far_future))

        before = time.time()
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        after = time.time()

        assert (resolved, failed) == (1, 0)
        # Canonical's bumped mtime must be within [before, after + 60s].
        canonical_mtime = canonical.stat().st_mtime
        assert canonical_mtime <= after + 60.0
        assert canonical_mtime >= before


class TestParseConflictDeviceShort:
    """Parser pin for ``manifest.parse_conflict_device_short``.

    The conflict-prompt-ux REMOTE banner attribution depends on extracting
    the 8-char device prefix `conflict_filename()` stamps into a sidecar
    name. Two grammar shapes (post-inversion / pre-inversion `v0-`), plus
    the optional 4-char same-second random suffix, plus extension stripping.
    """

    def test_post_inversion_extracts_device(self) -> None:
        from mind_meld.manifest import parse_conflict_device_short

        assert (
            parse_conflict_device_short("user.sync-conflict-20260421-143055-devA1234.md")
            == "devA1234"
        )

    def test_pre_inversion_v0_prefix_extracts_device(self) -> None:
        from mind_meld.manifest import parse_conflict_device_short

        assert (
            parse_conflict_device_short("role.sync-conflict-v0-20260420-120000-devA1234.md")
            == "devA1234"
        )

    def test_random_suffix_dropped_in_favor_of_device(self) -> None:
        from mind_meld.manifest import parse_conflict_device_short

        # Same-second collision suffix `-abcd` (4 hex chars). Device stays
        # as the segment before the suffix.
        assert (
            parse_conflict_device_short("user.sync-conflict-20260421-143055-devA1234-abcd.md")
            == "devA1234"
        )

    def test_non_conflict_filename_returns_none(self) -> None:
        from mind_meld.manifest import parse_conflict_device_short

        assert parse_conflict_device_short("plain.md") is None
        assert parse_conflict_device_short("user.sync-conflict-log.md") is None

    def test_multidot_stem_preserves_device(self) -> None:
        from mind_meld.manifest import parse_conflict_device_short

        assert (
            parse_conflict_device_short("notes.draft.sync-conflict-20260421-143055-devA1234.md")
            == "devA1234"
        )


# ── Component 1: never default Enter to (m)erge ──────────────────────


class TestNeverDefaultToMerge:
    """Component 1: the prompt default key is always (s)kip, never (m)erge,
    even when the LCS merge would be clean. A clean merge of two genuinely
    different documents has zero markers, so Enter-defaulting to (m) is a
    one-keystroke silent-corruption footgun. (m)erge stays selectable when
    the user types it.
    """

    @staticmethod
    def _clean_merge_pair(tmp_path: Path) -> tuple[Path, Path]:
        """Canonical + sidecar whose lcs_merge is CLEAN (purely additive)."""
        canonical = tmp_path / "notes.md"
        canonical.write_bytes(b"line one\nline two\n")
        conflict = tmp_path / "notes.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"line one\nline two\nline three\n")
        return canonical, conflict

    def test_resolve_loop_default_is_skip_on_clean_merge(self, tmp_path: Path, monkeypatch) -> None:
        canonical, conflict = self._clean_merge_pair(tmp_path)
        captured: dict = {}

        def fake_prompt(*a, **kw):
            captured["default"] = kw.get("default")
            return "s"

        monkeypatch.setattr(typer, "prompt", fake_prompt)
        _resolve_interactive_loop([("s1", conflict, canonical)])
        assert captured["default"] == "s", (
            "default key must be (s)kip even when the LCS merge is clean"
        )

    def test_prompt_conflict_choice_default_is_skip_on_clean_merge(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        local = tmp_path / "notes.md"
        local.write_bytes(b"line one\nline two\n")
        captured: dict = {}

        def fake_prompt(*a, **kw):
            captured["default"] = kw.get("default")
            return "s"

        monkeypatch.setattr(typer, "prompt", fake_prompt)
        _prompt_conflict_choice("notes.md", local, b"line one\nline two\nline three\n")
        assert captured["default"] == "s", (
            "inline pull-time prompt default must be (s)kip even on a clean merge"
        )

    def test_merge_still_selectable_when_typed(self, tmp_path: Path, monkeypatch) -> None:
        """Only the DEFAULT changed -- typing (m) still applies the clean merge."""
        canonical, conflict = self._clean_merge_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "m")
        _resolve_interactive_loop([("s1", conflict, canonical)])
        assert canonical.read_bytes() == b"line one\nline two\nline three\n"
        assert not conflict.exists()


# ── Component 2: (p)romote helpers ───────────────────────────────────


class TestPromoteHelpers:
    """Component 2: _promote_target_path (per-mode naming + collision) and
    _promote_conflict_file (os.link no-clobber)."""

    _NOW = datetime(2026, 5, 14, 21, 40, 20, tzinfo=timezone.utc)

    def test_post_inversion_name_is_from_peer(self, tmp_path: Path) -> None:
        canonical = tmp_path / "report.md"
        target = _promote_target_path(canonical, False, "devA1234", now=self._NOW)
        assert target.name == "report.from-devA1234-20260514-214020.md"

    def test_pre_inversion_name_is_local(self, tmp_path: Path) -> None:
        canonical = tmp_path / "report.md"
        target = _promote_target_path(canonical, True, "devA1234", now=self._NOW)
        assert target.name == "report.local-20260514-214020.md"
        assert "from-" not in target.name, "pre-inversion sidecar holds LOCAL bytes"

    def test_none_peer_short_falls_back_to_unknown(self, tmp_path: Path) -> None:
        canonical = tmp_path / "report.md"
        target = _promote_target_path(canonical, False, None, now=self._NOW)
        assert target.name == "report.from-unknown-20260514-214020.md"

    def test_collision_appends_hex(self, tmp_path: Path) -> None:
        canonical = tmp_path / "report.md"
        (tmp_path / "report.from-devA1234-20260514-214020.md").write_bytes(b"x")
        target = _promote_target_path(canonical, False, "devA1234", now=self._NOW)
        assert target.name != "report.from-devA1234-20260514-214020.md"
        assert target.name.startswith("report.from-devA1234-20260514-214020-")
        assert target.suffix == ".md"

    def test_conflict_file_happy_path(self, tmp_path: Path) -> None:
        src = tmp_path / "report.sync-conflict-X.md"
        src.write_bytes(b"sidecar bytes")
        target = tmp_path / "report.from-devA1234-ts.md"
        actual = _promote_conflict_file(src, target)
        assert actual == target
        assert target.read_bytes() == b"sidecar bytes"
        assert not src.exists(), "sidecar consumed by the promote"

    def test_conflict_file_no_clobber(self, tmp_path: Path) -> None:
        """os.link must NOT silently overwrite a pre-existing target -- a
        promoted file is a first-class user filename, so clobber = data loss."""
        src = tmp_path / "report.sync-conflict-X.md"
        src.write_bytes(b"sidecar bytes")
        target = tmp_path / "report.from-devA1234-ts.md"
        target.write_bytes(b"PRE-EXISTING -- must not be clobbered")
        actual = _promote_conflict_file(src, target)
        assert actual != target, "must retry with a fresh suffix, not overwrite"
        assert target.read_bytes() == b"PRE-EXISTING -- must not be clobbered"
        assert actual.read_bytes() == b"sidecar bytes"
        assert not src.exists()


class TestResolvePromote:
    """Component 2: (p)romote in the _resolve_interactive_loop canonical-exists
    path -- keep BOTH files by renaming the sidecar to its own filename."""

    @staticmethod
    def _post_inversion_pair(tmp_path: Path) -> tuple[Path, Path]:
        canonical = tmp_path / "report.md"
        canonical.write_bytes(b"local report\n")
        conflict = tmp_path / "report.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"remote report\n")
        return canonical, conflict

    def test_promote_post_inversion(self, tmp_path: Path, monkeypatch) -> None:
        canonical, conflict = self._post_inversion_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (1, 0)
        assert canonical.read_bytes() == b"local report\n", "canonical untouched"
        assert not conflict.exists(), "sidecar renamed away"
        promoted = list(tmp_path.glob("report.from-*"))
        assert len(promoted) == 1
        assert promoted[0].name.startswith("report.from-devA1234-")
        assert promoted[0].suffix == ".md"
        assert promoted[0].read_bytes() == b"remote report\n"

    def test_promote_pre_inversion_names_local(self, tmp_path: Path, monkeypatch) -> None:
        # v0- sidecar: pre-inversion -- canonical = remote, sidecar = LOCAL.
        canonical = tmp_path / "report.md"
        canonical.write_bytes(b"remote report\n")
        conflict = tmp_path / "report.sync-conflict-v0-20260101-100000-devA1234.md"
        conflict.write_bytes(b"my local report\n")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (1, 0)
        promoted = list(tmp_path.glob("report.local-*"))
        assert len(promoted) == 1
        assert "from-" not in promoted[0].name
        assert promoted[0].read_bytes() == b"my local report\n"
        assert canonical.read_bytes() == b"remote report\n"
        assert not conflict.exists()

    def test_promote_link_oserror_counts_failed(self, tmp_path: Path, monkeypatch) -> None:
        canonical, conflict = self._post_inversion_pair(tmp_path)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(os, "link", boom)
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (0, 1)
        assert conflict.exists(), "sidecar left in place on failure"
        assert canonical.exists()

    def test_promote_include_files_source_warns(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A promoted file under an include_files-only source falls outside the
        sync surface -- promote succeeds but warns."""
        src_root = tmp_path / "src"
        src_root.mkdir()
        canonical = src_root / "config.yaml"
        canonical.write_bytes(b"local: 1\n")
        conflict = src_root / "config.sync-conflict-20260421-143055-devA1234.yaml"
        conflict.write_bytes(b"remote: 2\n")
        src_cfg = {
            "name": "s1",
            "path": str(src_root),
            "type": "generic",
            "include_files": ["config.yaml"],
            "include_dirs": [],
        }
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")
        _resolve_interactive_loop([("s1", conflict, canonical)], None, {"s1": src_cfg})
        err = capsys.readouterr().err
        assert "will not sync" in err
        assert "include_files" in err

    def test_promote_include_dirs_source_no_warning(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A promoted file inside an include_dir DOES sync -- no warning."""
        src_root = tmp_path / "src"
        sub = src_root / "notes"
        sub.mkdir(parents=True)
        canonical = sub / "report.md"
        canonical.write_bytes(b"local\n")
        conflict = sub / "report.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"remote\n")
        src_cfg = {
            "name": "s1",
            "path": str(src_root),
            "type": "generic",
            "include_dirs": ["notes"],
            "include_files": [],
        }
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")
        _resolve_interactive_loop([("s1", conflict, canonical)], None, {"s1": src_cfg})
        err = capsys.readouterr().err
        assert "will not sync" not in err

    def test_no_base_promote_still_renames_to_canonical(self, tmp_path: Path, monkeypatch) -> None:
        """The pre-existing no-base (p)romote path is unchanged: when canonical
        is gone, promote renames the sidecar to the canonical name."""
        conflict = tmp_path / "report.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"orphan bytes")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, None)])
        assert (resolved, failed) == (1, 0)
        canonical = tmp_path / "report.md"
        assert canonical.read_bytes() == b"orphan bytes"
        assert not conflict.exists()

    def test_promote_post_inversion_bumps_canonical_mtime(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Post-inversion (p)romote must bump canonical's mtime past peer's --
        otherwise the local half of "keep both" silently fails to propagate
        across the fleet (origin peer's next pull mtime-gates it out). Same
        load-bearing fleet-propagation contract as (l)ocal: see
        _bump_canonical_mtime_post_resolve.
        """
        canonical, conflict = self._post_inversion_pair(tmp_path)
        # Stamp canonical with an old mtime and peer-sidecar with a NEW one
        # so the bug case is set up: without the bump, canonical mtime stays
        # at the old value (the peer would mtime-skip it on next pull).
        old_mtime = time.time() - 3600  # 1h ago
        peer_mtime = time.time() - 60  # 1m ago
        os.utime(canonical, (old_mtime, old_mtime))
        os.utime(conflict, (peer_mtime, peer_mtime))
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, canonical)])
        assert (resolved, failed) == (1, 0)
        bumped = canonical.stat().st_mtime
        assert bumped > peer_mtime, (
            f"canonical mtime ({bumped}) must be > peer_mtime ({peer_mtime}) "
            f"so the local half of keep-both propagates across the fleet"
        )

    def test_promote_pre_inversion_no_mtime_bump(self, tmp_path: Path, monkeypatch) -> None:
        """Pre-inversion (p)romote leaves canonical's mtime alone: canonical
        holds peer's bytes (intentionally kept), the sidecar held local bytes
        (now promoted to its own filename). Bumping canonical would lie about
        when peer's bytes arrived.
        """
        canonical = tmp_path / "report.md"
        canonical.write_bytes(b"remote report\n")
        conflict = tmp_path / "report.sync-conflict-v0-20260101-100000-devA1234.md"
        conflict.write_bytes(b"my local report\n")
        peer_mtime = time.time() - 300  # 5m ago
        os.utime(canonical, (peer_mtime, peer_mtime))
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "p")
        _resolve_interactive_loop([("s1", conflict, canonical)])
        after = canonical.stat().st_mtime
        assert abs(after - peer_mtime) < 1.0, (
            f"pre-inversion canonical mtime must NOT be bumped (was {peer_mtime}, "
            f"now {after}) -- canonical holds peer's bytes intentionally"
        )


class TestResolveNewerShortcut:
    """`(n)ewer` at the mm resolve prompt keeps whichever side has the greater
    mtime by remapping to the existing (l)/(r) dispatch. Verified through the
    dispatch (resulting file state), not just the rendered option line --
    a happy-path-only test would miss an inversion data-loss bug (Codex eng
    review #5/#6/#8). All four mapping cases are covered.
    """

    @staticmethod
    def _post_inversion_pair(tmp_path: Path, local_mtime: float, remote_mtime: float):
        # post-inversion: canonical = local bytes, sidecar = remote bytes.
        canonical = tmp_path / "user.md"
        canonical.write_bytes(b"local content")
        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"remote content")
        os.utime(canonical, (local_mtime, local_mtime))
        os.utime(conflict, (remote_mtime, remote_mtime))
        return canonical, conflict

    @staticmethod
    def _pre_inversion_pair(tmp_path: Path, local_mtime: float, remote_mtime: float):
        # pre-inversion (v0-): canonical = remote bytes, sidecar = local bytes.
        canonical = tmp_path / "user.md"
        canonical.write_bytes(b"remote content")
        conflict = tmp_path / "user.sync-conflict-v0-20260101-100000-devA1234.md"
        conflict.write_bytes(b"local content")
        os.utime(conflict, (local_mtime, local_mtime))  # local side = sidecar
        os.utime(canonical, (remote_mtime, remote_mtime))  # remote side = canonical
        return canonical, conflict

    def test_post_inversion_remote_newer_keeps_remote(self, tmp_path: Path, monkeypatch) -> None:
        canonical, conflict = self._post_inversion_pair(tmp_path, 100.0, 200.0)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "n")
        _resolve_interactive_loop([("s1", conflict, canonical)])
        # newer == remote → same as (r): sidecar promoted over canonical.
        assert canonical.read_bytes() == b"remote content"
        assert not conflict.exists()

    def test_post_inversion_local_newer_keeps_local(self, tmp_path: Path, monkeypatch) -> None:
        canonical, conflict = self._post_inversion_pair(tmp_path, 300.0, 100.0)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "n")
        _resolve_interactive_loop([("s1", conflict, canonical)])
        # newer == local → same as (l): canonical kept, sidecar unlinked.
        assert canonical.read_bytes() == b"local content"
        assert not conflict.exists()

    def test_pre_inversion_local_newer_promotes_sidecar(self, tmp_path: Path, monkeypatch) -> None:
        canonical, conflict = self._pre_inversion_pair(tmp_path, 300.0, 100.0)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "n")
        _resolve_interactive_loop([("s1", conflict, canonical)])
        # local newer; pre-inversion (l) promotes the v0- sidecar (local bytes).
        assert canonical.read_bytes() == b"local content"
        assert not conflict.exists()

    def test_pre_inversion_remote_newer_keeps_canonical(self, tmp_path: Path, monkeypatch) -> None:
        canonical, conflict = self._pre_inversion_pair(tmp_path, 100.0, 300.0)
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "n")
        _resolve_interactive_loop([("s1", conflict, canonical)])
        # remote newer; pre-inversion (r) drops the local sidecar, canonical stays.
        assert canonical.read_bytes() == b"remote content"
        assert not conflict.exists()

    def test_tie_reprompts_then_skips(self, tmp_path: Path, monkeypatch, capsys) -> None:
        canonical, conflict = self._post_inversion_pair(tmp_path, 150.0, 150.0)
        choices = iter(["n", "s"])  # tie 'n' must re-prompt, then skip
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: next(choices))
        _resolve_interactive_loop([("s1", conflict, canonical)])
        # Nothing changed: tie 'n' did not advance/guess; the follow-up 's' skipped.
        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"
        assert "equal mtime" in capsys.readouterr().out

    def test_unreadable_mtime_suppresses_and_reprompts(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        canonical, conflict = self._post_inversion_pair(tmp_path, 100.0, 200.0)
        # Force both stats to fail so (n)ewer is unavailable / unknown.
        monkeypatch.setattr("mind_meld.resolveflow._stat_mtime_btime", lambda p: (None, None))
        choices = iter(["n", "s"])
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: next(choices))
        _resolve_interactive_loop([("s1", conflict, canonical)])
        # Typed 'n' while suppressed re-prompts (does not advance), then skip.
        assert canonical.read_bytes() == b"local content"
        assert conflict.read_bytes() == b"remote content"
        assert "unavailable" in capsys.readouterr().out

    def test_canonical_none_typed_n_stays_skip(self, tmp_path: Path, monkeypatch) -> None:
        # The canonical-is-None prompt is promote/delete/skip -- it offers no
        # (n)ewer, and typed 'n' must remain a skip/no-op (Codex eng #4).
        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"remote orphan")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "n")
        resolved, failed = _resolve_interactive_loop([("s1", conflict, None)])
        assert conflict.exists()  # untouched
        assert (resolved, failed) == (0, 0)


class TestInlinePromptTimestamps:
    """The inline `mm pull --conflict-mode prompt` site shows timestamps + a
    recency verdict but offers NO (n)ewer: _apply_incoming_file already skips
    before prompting when local is newer (cli.py mtime gate), so remote is
    always newer-or-equal there and (n)ewer would just alias (r).
    """

    def test_shows_remote_modified_from_manifest_and_no_newer_option(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        local = tmp_path / "user.md"
        local.write_bytes(b"local content")
        os.utime(local, (100.0, 100.0))
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")
        remote_mtime = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
        choice, merged = _prompt_conflict_choice(
            "user.md", local, b"remote content", remote_mtime=remote_mtime
        )
        out = capsys.readouterr().out
        assert "modified" in out  # both sides render a modified line
        assert "2026-06-" in out  # remote modified came from the manifest dt
        assert "(n)ewer" not in out  # no shortcut at the inline site
        assert choice == "keep-both"  # 's' → keep-both

    def test_typed_n_inline_falls_to_keep_both_not_keep_remote(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        local = tmp_path / "user.md"
        local.write_bytes(b"local content")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "n")
        choice, merged = _prompt_conflict_choice(
            "user.md",
            local,
            b"remote content",
            remote_mtime=datetime(2026, 6, 20, tzinfo=timezone.utc),
        )
        # No inline (n) branch → unrecognized → keep-both (skip), NOT keep-remote.
        assert choice == "keep-both"
        assert merged is None
