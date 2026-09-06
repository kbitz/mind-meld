"""Unit tests for the helpers underneath `_pull_core` / `_apply_incoming_file`.

Pins each extracted helper at the unit boundary so refactors don't have to
rely on the CLI-driven integration tests to surface a regression. Originally
landed as "Track 2A" (v0.8.7) when cli.py was decomposed.
"""

from __future__ import annotations

import errno
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mind_meld import resolveflow
from mind_meld.cli import (
    _apply_conflict,
    _apply_merge,
    _apply_write,
    _bootstrap_or_verify_crypto,
    _CorruptPeer,
    _fsync_touched_parents,
    _FsyncWarning,
    _include_prior_grok_if_needed,
    _load_prior_device_metadata,
    _PerSourceResult,
    _prefetch_manifests,
    _preflight_conflicts,
    _prompt_passphrase,
    _prompt_sources,
    _prove_omitted_paths_absent,
    _pull_one_source,
    _register_and_save,
    _restore_mtime_best_effort,
    _retain_prior_default_sources,
    _select_devices,
    _UnknownSourceWarning,
    _upload_changed_blobs,
)
from mind_meld.config import DEFAULT_SOURCES, SourceResolution
from mind_meld.errors import SnapshotError, StorageError
from mind_meld.manifest import read_file_revision

# ── fixtures ─────────────────────────────────────────────────────────


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _info(sha: str, mtime: datetime | None = None) -> dict:
    return {
        "sha256": sha,
        "size": 0,
        "mtime": (mtime or datetime.now(timezone.utc)).isoformat(),
    }


# ── _apply_write ─────────────────────────────────────────────────────


class TestApplyWrite:
    def test_happy_path(self, tmp_path: Path) -> None:
        local = tmp_path / "sub" / "file.md"
        local.parent.mkdir(parents=True, exist_ok=True)
        outcome = _apply_write(local, "sub/file.md", b"data")
        assert outcome == "written"
        assert local.read_bytes() == b"data"

    def test_oserror_returns_failed(self, tmp_path: Path, monkeypatch) -> None:
        # Force fsutil.atomic_write_bytes to raise OSError.
        from mind_meld import cli as cli_module

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", boom)
        local = tmp_path / "x.md"
        outcome = _apply_write(local, "x.md", b"data")
        assert outcome == "failed"

    def test_restores_remote_mtime(self, tmp_path: Path) -> None:
        """Pulled file's mtime matches the manifest, not the time of pull.

        Pre-fix, atomic_write_bytes stamped st_mtime = now-of-pull,
        which broke any consumer that uses mtime for recency ordering
        (gstack skill preambles' `ls -t` over checkpoints/, ceo-plans/).
        """
        local = tmp_path / "doc.md"
        # An mtime well in the past so we can tell it apart from "now".
        remote_mtime = datetime(2026, 1, 15, 12, 30, 45, tzinfo=timezone.utc)

        outcome = _apply_write(
            local,
            "doc.md",
            b"data",
            remote_mtime_iso=remote_mtime.isoformat(),
        )

        assert outcome == "written"
        actual = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
        # Allow sub-second drift from filesystem timestamp resolution
        # (HFS+ rounds to 1s; APFS keeps nanoseconds but utime/stat may lose them).
        assert abs((actual - remote_mtime).total_seconds()) < 1.0

    def test_no_mtime_in_manifest_leaves_now(self, tmp_path: Path) -> None:
        """Manifests without mtime (defensive — older blobs, future schema)
        fall through cleanly: file is written, mtime stays at NOW."""
        local = tmp_path / "doc.md"
        before = datetime.now(timezone.utc)

        outcome = _apply_write(local, "doc.md", b"data", remote_mtime_iso=None)

        assert outcome == "written"
        actual = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
        # Should be ~now, not 1970-01-01 or any other parse-fail sentinel.
        assert (actual - before).total_seconds() < 5.0

    def test_unparseable_mtime_does_not_fail_write(self, tmp_path: Path) -> None:
        """Best-effort contract: a malformed peer-supplied mtime must not
        abort the file write or raise. The file is on disk; mtime is just
        metadata and falls back to NOW."""
        local = tmp_path / "doc.md"

        outcome = _apply_write(local, "doc.md", b"data", remote_mtime_iso="not-a-date")

        assert outcome == "written"
        assert local.read_bytes() == b"data"


# ── _apply_merge ─────────────────────────────────────────────────────


class TestApplyMerge:
    def test_happy_path_jsonl_union(self, tmp_path: Path) -> None:
        local = tmp_path / "notes.jsonl"
        local.write_bytes(b'{"a":1}\n{"b":2}\n')
        remote = b'{"b":2}\n{"c":3}\n'
        outcome = _apply_merge(local, "notes.jsonl", remote)
        assert outcome == "merged"
        lines = set(local.read_bytes().splitlines())
        assert b'{"a":1}' in lines
        assert b'{"b":2}' in lines
        assert b'{"c":3}' in lines

    def test_oserror_returns_failed(self, tmp_path: Path, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        local = tmp_path / "notes.jsonl"
        local.write_bytes(b'{"a":1}\n')

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", boom)
        outcome = _apply_merge(local, "notes.jsonl", b'{"b":2}\n')
        assert outcome == "failed"

    def test_noop_merge_returns_unchanged_jsonl(self, tmp_path: Path, monkeypatch) -> None:
        """Local is a strict superset of remote → merged bytes equal local
        bytes → return "unchanged" without touching disk.

        Phantom-activity regression: pre-fix, every pull reported "merged"
        even when the merge produced no actual change, giving users the
        impression that new content had arrived.
        """
        from mind_meld import cli as cli_module

        local = tmp_path / "notes.jsonl"
        local.write_bytes(b'{"a":1}\n{"b":2}\n')
        original_mtime = local.stat().st_mtime
        original_bytes = local.read_bytes()

        # Sentinel: blow up if anything tries to write. The no-op path
        # must skip the write entirely.
        def must_not_write(*a, **kw):
            raise AssertionError("no-op merge must not call atomic_write_bytes")

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", must_not_write)

        # Remote is a subset of local — line-union merge produces local's bytes.
        outcome = _apply_merge(local, "notes.jsonl", b'{"a":1}\n')

        assert outcome == "unchanged"
        assert local.read_bytes() == original_bytes
        assert local.stat().st_mtime == original_mtime

    def test_noop_merge_returns_unchanged_memory_md(self, tmp_path: Path) -> None:
        """MEMORY.md is the other mergeable file class. Same no-op rule."""
        local = tmp_path / "MEMORY.md"
        local.write_bytes(b"- entry one\n- entry two\n")
        original_bytes = local.read_bytes()

        outcome = _apply_merge(local, "MEMORY.md", b"- entry one\n")

        assert outcome == "unchanged"
        # _join_lines sorts lexicographically; "- entry one" < "- entry two".
        # Both are present in local, so the merge result equals local bytes
        # by content union — the file on disk must be untouched either way.
        assert local.read_bytes() == original_bytes

    def test_real_merge_still_writes(self, tmp_path: Path) -> None:
        """Counter-test: when remote contains lines local doesn't have,
        the merge IS a real change → write happens, return "merged"."""
        local = tmp_path / "notes.jsonl"
        local.write_bytes(b'{"a":1}\n')

        outcome = _apply_merge(local, "notes.jsonl", b'{"a":1}\n{"b":2}\n')

        assert outcome == "merged"
        lines = set(local.read_bytes().splitlines())
        assert b'{"a":1}' in lines
        assert b'{"b":2}' in lines


# ── _apply_conflict ──────────────────────────────────────────────────


class TestApplyConflict:
    def test_sidecar_write_failure_preserves_local(self, tmp_path: Path, monkeypatch) -> None:
        """Post-inversion: sidecar write failure leaves local untouched
        at canonical because we never overwrite it. No rollback needed —
        the inversion eliminates the rename + rollback dance."""
        from mind_meld import cli as cli_module

        local = tmp_path / "doc.md"
        local.write_bytes(b"local")

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", boom)
        outcome = _apply_conflict(local, "doc.md", b"remote", "devAAAA1234")
        assert outcome == "failed"
        assert local.exists()
        assert local.read_bytes() == b"local"
        # No sidecar was written.
        assert not any(p.name.startswith("doc.sync-conflict-") for p in tmp_path.iterdir())

    def test_seeded_write_oserror_preserves_old_sidecar(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from mind_meld import cli as cli_module

        local = tmp_path / "doc.md"
        local.write_bytes(b"local")
        old = tmp_path / "doc.sync-conflict-20260421-120000-v1-devAAAA1.md"
        old.write_bytes(b"peer R1")
        local_mtime = local.stat().st_mtime_ns
        old_mtime = old.stat().st_mtime_ns

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", boom)
        outcome = _apply_conflict(local, "doc.md", b"peer R2", "devAAAA1234")
        assert outcome == "failed"
        assert local.read_bytes() == b"local"
        assert local.stat().st_mtime_ns == local_mtime
        assert old.exists()
        assert old.read_bytes() == b"peer R1"
        assert old.stat().st_mtime_ns == old_mtime
        err = capsys.readouterr().err
        assert "sidecar write failed" in err
        assert "prior conflict copies preserved" in err
        assert str(local) in err or "doc.md" in err

    def test_seeded_replace_storageerror_preserves_old_sidecar(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from mind_meld import fsutil as fsutil_mod

        local = tmp_path / "doc.md"
        local.write_bytes(b"local")
        old = tmp_path / "doc.sync-conflict-20260421-120000-v1-devAAAA1.md"
        old.write_bytes(b"peer R1")
        local_mtime = local.stat().st_mtime_ns
        old_mtime = old.stat().st_mtime_ns

        def bad_replace(src, dst):
            raise OSError("cross-device link")

        monkeypatch.setattr(fsutil_mod.os, "replace", bad_replace)
        outcome = _apply_conflict(local, "doc.md", b"peer R2", "devAAAA1234")
        assert outcome == "failed"
        assert local.read_bytes() == b"local"
        assert local.stat().st_mtime_ns == local_mtime
        assert old.exists()
        assert old.read_bytes() == b"peer R1"
        assert old.stat().st_mtime_ns == old_mtime
        assert not any(
            p.name.startswith("doc.sync-conflict-") and p != old and p.is_file()
            for p in tmp_path.iterdir()
        )
        assert list(tmp_path.glob("tmp*")) == []

    def test_path_build_valueerror_after_selection_preserves_old(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from mind_meld import cli as cli_module

        local = tmp_path / "doc.md"
        local.write_bytes(b"local")
        old = tmp_path / "doc.sync-conflict-20260421-120000-v1-devAAAA1.md"
        old.write_bytes(b"peer R1")
        old_mtime = old.stat().st_mtime_ns

        def boom(canonical, device_id, now=None):
            raise ValueError("injected after selection")

        monkeypatch.setattr(cli_module, "conflict_filename", boom)
        outcome = _apply_conflict(local, "doc.md", b"peer R2", "devAAAA1234")
        assert outcome == "failed"
        assert old.exists()
        assert old.read_bytes() == b"peer R1"
        assert old.stat().st_mtime_ns == old_mtime
        err = capsys.readouterr().err
        assert "conflict path build failed" in err
        assert "injected after selection" in err
        assert "\x1b" not in err

    def test_occupancy_probe_oserror_fails_one_file_and_continues(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        first = tmp_path / "first.md"
        second = tmp_path / "second.md"
        first.write_bytes(b"local-1")
        second.write_bytes(b"local-2")
        old = tmp_path / "first.sync-conflict-20260421-120000-v1-devA1234.md"
        old.write_bytes(b"peer R1")
        old_mtime = old.stat().st_mtime_ns
        orig_lstat = Path.lstat

        def selective_lstat(self: Path):
            if "first.sync-conflict-" in self.name:
                raise PermissionError("probe denied")
            return orig_lstat(self)

        monkeypatch.setattr(Path, "lstat", selective_lstat)
        first_outcome = _apply_conflict(first, "first.md", b"peer R2", "devA1234")
        second_outcome = _apply_conflict(second, "second.md", b"peer R2", "devA1234")
        assert first_outcome == "failed"
        assert second_outcome == "conflicted"
        assert old.exists()
        assert old.read_bytes() == b"peer R1"
        assert old.stat().st_mtime_ns == old_mtime
        assert first.read_bytes() == b"local-1"
        assert second.read_bytes() == b"local-2"
        err = capsys.readouterr().err
        assert "conflict path build failed" in err
        owned_second = [
            p
            for p in tmp_path.iterdir()
            if p.is_file() and p.name.startswith("second.sync-conflict-")
        ]
        assert len(owned_second) == 1
        assert owned_second[0].read_bytes() == b"peer R2"

    def test_sidecar_mtime_matches_remote(self, tmp_path: Path) -> None:
        """The sidecar holds the surprising remote bytes — and it should
        carry the remote's authorship time, not the time of pull. Lets
        users sort sidecars by when the conflicting content was authored."""
        local = tmp_path / "doc.md"
        local.write_bytes(b"local")
        remote_mtime = datetime(2026, 1, 15, 12, 30, 45, tzinfo=timezone.utc)

        outcome = _apply_conflict(
            local,
            "doc.md",
            b"remote",
            "devAAAA1234",
            remote_mtime_iso=remote_mtime.isoformat(),
        )

        assert outcome == "conflicted"
        sidecars = [p for p in tmp_path.iterdir() if "sync-conflict" in p.name]
        assert len(sidecars) == 1
        actual = datetime.fromtimestamp(sidecars[0].stat().st_mtime, tz=timezone.utc)
        assert abs((actual - remote_mtime).total_seconds()) < 1.0


class TestApplyMergeKeepsNowMtime:
    """Merge produces locally-authored content (line-union of local +
    remote). Its mtime must NOT be backdated to remote's mtime — peers
    pulling next would see local_mtime <= their remote_mtime and skip
    the merged result, losing the union content fleet-wide."""

    def test_merge_does_not_inherit_remote_mtime(self, tmp_path: Path) -> None:
        local = tmp_path / "notes.jsonl"
        local.write_bytes(b'{"a":1}\n')
        before = datetime.now(timezone.utc)

        outcome = _apply_merge(local, "notes.jsonl", b'{"a":1}\n{"b":2}\n')

        assert outcome == "merged"
        actual = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
        # Merged file's mtime is ~now, not some manifest-supplied past time.
        assert (actual - before).total_seconds() < 5.0


class TestRestoreMtimeBestEffort:
    def test_none_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "f.md"
        path.write_bytes(b"x")
        before = path.stat().st_mtime
        _restore_mtime_best_effort(path, None)
        assert path.stat().st_mtime == before

    def test_empty_string_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "f.md"
        path.write_bytes(b"x")
        before = path.stat().st_mtime
        _restore_mtime_best_effort(path, "")
        assert path.stat().st_mtime == before

    def test_unparseable_is_swallowed(self, tmp_path: Path) -> None:
        path = tmp_path / "f.md"
        path.write_bytes(b"x")
        # Must not raise.
        _restore_mtime_best_effort(path, "not-a-date")

    def test_missing_file_is_swallowed(self, tmp_path: Path) -> None:
        # Path doesn't exist — os.utime raises FileNotFoundError. Helper
        # contract is best-effort; failure must not propagate.
        ghost = tmp_path / "does-not-exist.md"
        _restore_mtime_best_effort(ghost, "2026-01-15T12:30:45+00:00")

    def test_future_dated_mtime_clamped_to_now(self, tmp_path: Path) -> None:
        """A peer with a bad clock (or a passphrase-holding attacker)
        could mint a manifest with mtime far in the future. Without the
        clamp, the victim's `local_mtime > remote_mtime` skip would
        silently block all subsequent legitimate updates to that path.

        Verify the applied mtime is capped at ~now, not the future date.
        """
        from datetime import datetime, timezone

        path = tmp_path / "f.md"
        path.write_bytes(b"x")
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)

        _restore_mtime_best_effort(path, far_future.isoformat())
        after = time.time()

        actual = path.stat().st_mtime
        # Must be within the clamp window: at most after + 60s skew tolerance.
        assert actual <= after + 61.0
        # Must NOT be the year-2099 timestamp.
        assert actual < far_future.timestamp() - 86400  # off by at least a day

    def test_non_string_mtime_is_swallowed(self, tmp_path: Path) -> None:
        """A peer manifest with `mtime: 1234` (int instead of str)
        raises TypeError from datetime.fromisoformat. The helper must
        catch it — pre-fix this would propagate and abort the pull
        with a partial write already on disk."""
        path = tmp_path / "f.md"
        path.write_bytes(b"x")
        # Must not raise. type-ignore because we're deliberately violating
        # the str | None contract to simulate a malformed manifest.
        _restore_mtime_best_effort(path, 1234)  # type: ignore[arg-type]


# ── _select_devices ──────────────────────────────────────────────────


class TestSelectDevices:
    def _mock_backend_with(self, devices_data: list[dict]):
        """Backend that returns the given device entries via list_devices_warn."""
        backend = MagicMock()
        # _list_devices_warn calls _list_devices_impl(backend, on_drop=...)
        # via mind_meld.devices.list_devices-ish. The simpler path: patch
        # _list_devices_warn at the cli module level in each test.
        return backend

    def test_from_device_matches(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        monkeypatch.setattr(
            cli_module,
            "_list_devices_warn",
            lambda b: [
                {"device_id": "self1", "device_name": "me"},
                {"device_id": "peerA", "device_name": "A"},
                {"device_id": "peerB", "device_name": "B"},
            ],
        )
        all_devs, targets = _select_devices(backend=None, my_device_id="self1", from_device="peerA")
        assert len(all_devs) == 3
        assert [d["device_id"] for d in targets] == ["peerA"]

    def test_from_device_unmatched_returns_empty_targets(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        monkeypatch.setattr(
            cli_module,
            "_list_devices_warn",
            lambda b: [
                {"device_id": "self1", "device_name": "me"},
                {"device_id": "peerA", "device_name": "A"},
            ],
        )
        all_devs, targets = _select_devices(
            backend=None, my_device_id="self1", from_device="nonexistent"
        )
        assert len(all_devs) == 2
        assert targets == []

    def test_from_device_none_excludes_self(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        monkeypatch.setattr(
            cli_module,
            "_list_devices_warn",
            lambda b: [
                {"device_id": "self1", "device_name": "me"},
                {"device_id": "peerA", "device_name": "A"},
                {"device_id": "peerB", "device_name": "B"},
            ],
        )
        all_devs, targets = _select_devices(backend=None, my_device_id="self1", from_device=None)
        assert {d["device_id"] for d in targets} == {"peerA", "peerB"}

    def test_dedup_single_call(self, monkeypatch) -> None:
        """Regression: pre-decomp _pull_core called _list_devices_warn twice."""
        from mind_meld import cli as cli_module

        calls = []

        def counting_list(b):
            calls.append(1)
            return [{"device_id": "peerA", "device_name": "A"}]

        monkeypatch.setattr(cli_module, "_list_devices_warn", counting_list)
        _select_devices(backend=None, my_device_id="self1", from_device=None)
        assert len(calls) == 1


# ── _prefetch_manifests ──────────────────────────────────────────────


class TestPrefetchManifests:
    def test_all_ok_no_corrupt(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module
        from mind_meld.cli import ManifestFetch

        monkeypatch.setattr(
            cli_module,
            "_fetch_remote_manifest",
            lambda backend, did, pp, mk: ManifestFetch(
                status="ok", manifest={"sources": {}, "tombstones": {}}
            ),
        )
        devices = [
            {"device_id": "A", "device_name": "A"},
            {"device_id": "B", "device_name": "B"},
        ]
        cache, corrupt = _prefetch_manifests(None, devices, "pp", 1024)
        assert set(cache.keys()) == {"A", "B"}
        assert corrupt == []

    def test_corrupt_peer_surfaces(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module
        from mind_meld.cli import ManifestFetch

        def fake_fetch(backend, did, pp, mk):
            if did == "bad":
                return ManifestFetch(status="corrupt")
            return ManifestFetch(status="ok", manifest={"sources": {}, "tombstones": {}})

        monkeypatch.setattr(cli_module, "_fetch_remote_manifest", fake_fetch)
        devices = [
            {"device_id": "good", "device_name": "GoodMac"},
            {"device_id": "bad", "device_name": "BadMac"},
        ]
        cache, corrupt = _prefetch_manifests(None, devices, "pp", 1024)
        assert cache["good"] is not None
        assert cache["bad"] is None  # corrupt mapped to None
        assert len(corrupt) == 1
        assert corrupt[0].device_id == "bad"
        assert corrupt[0].device_name == "BadMac"

    def test_missing_peer_no_warning(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module
        from mind_meld.cli import ManifestFetch

        monkeypatch.setattr(
            cli_module,
            "_fetch_remote_manifest",
            lambda *a, **kw: ManifestFetch(status="missing"),
        )
        devices = [{"device_id": "A", "device_name": "A"}]
        cache, corrupt = _prefetch_manifests(None, devices, "pp", 1024)
        assert cache["A"] is None
        assert corrupt == []  # missing is not corrupt


# ── _preflight_conflicts ─────────────────────────────────────────────


class TestPreflightConflicts:
    def test_no_conflicts_empty_list(self, tmp_path: Path) -> None:
        # No local files, remote wants to write fresh — no conflict.
        pull_targets = [{"device_id": "peerA", "device_name": "A"}]
        manifest_cache = {
            "peerA": {
                "sources": {
                    "claude": {
                        "files": {"a.md": _info("abc")},
                    }
                },
                "tombstones": {},
            },
        }
        local_sources = {"claude": {"path": tmp_path, "type": "claude"}}
        predicted = _preflight_conflicts(
            pull_targets,
            manifest_cache,
            local_sources,
            source_filter=None,
            all_tombstones={},
        )
        assert predicted == []

    def test_conflict_predicted(self, tmp_path: Path) -> None:
        # Local file exists with different content, no mtime override —
        # predict_pull_outcome returns "conflict".
        local = tmp_path / "a.md"
        local.write_bytes(b"local bytes")
        pull_targets = [{"device_id": "peerA", "device_name": "A"}]
        manifest_cache = {
            "peerA": {
                "sources": {
                    "claude": {
                        "files": {
                            "a.md": _info(
                                "different-sha",
                                mtime=datetime.now(timezone.utc) + timedelta(hours=1),
                            ),
                        },
                    }
                },
                "tombstones": {},
            },
        }
        local_sources = {"claude": {"path": tmp_path, "type": "claude"}}
        predicted = _preflight_conflicts(
            pull_targets,
            manifest_cache,
            local_sources,
            source_filter=None,
            all_tombstones={},
        )
        assert len(predicted) == 1
        assert predicted[0].rel_path == "a.md"
        assert predicted[0].device_name == "A"
        assert predicted[0].src_name == "claude"

    def test_cross_peer_overlay(self, tmp_path: Path) -> None:
        """Peer A writes Y; peer B writes Z → B conflicts with A's overlay."""
        pull_targets = [
            {"device_id": "peerA", "device_name": "A"},
            {"device_id": "peerB", "device_name": "B"},
        ]
        manifest_cache = {
            "peerA": {
                "sources": {
                    "claude": {"files": {"shared.md": _info("shaY")}},
                },
                "tombstones": {},
            },
            "peerB": {
                "sources": {
                    "claude": {"files": {"shared.md": _info("shaZ")}},
                },
                "tombstones": {},
            },
        }
        local_sources = {"claude": {"path": tmp_path, "type": "claude"}}
        predicted = _preflight_conflicts(
            pull_targets,
            manifest_cache,
            local_sources,
            source_filter=None,
            all_tombstones={},
        )
        # B conflicts with A's overlay.
        assert len(predicted) == 1
        assert predicted[0].device_name == "B"

    def test_unknown_source_not_counted_as_conflict(self, tmp_path: Path) -> None:
        pull_targets = [{"device_id": "peerA", "device_name": "A"}]
        manifest_cache = {
            "peerA": {
                "sources": {
                    "gstack": {"files": {"any.md": _info("shaX")}},
                },
                "tombstones": {},
            },
        }
        # no gstack mapping — only claude source configured locally
        local_sources = {"claude": {"path": tmp_path, "type": "claude"}}
        predicted = _preflight_conflicts(
            pull_targets,
            manifest_cache,
            local_sources,
            source_filter=None,
            all_tombstones={},
        )
        assert predicted == []


# ── _pull_one_source ─────────────────────────────────────────────────


class TestPullOneSource:
    def test_empty_remote_files(self, tmp_path: Path) -> None:
        result = _pull_one_source(
            backend=None,
            src_name="claude",
            src_type="claude",
            src_data={"files": {}},
            did="peerA",
            dname="A",
            base_path=tmp_path,
            all_tombstones={},
            passphrase="pp",
            memory_kb=1024,
            interactive_resolve=False,
            dry_run=False,
            verbose_console=False,
        )
        assert result.src_name == "claude"
        assert result.device_id == "peerA"
        assert not result.had_changes

    def test_dry_run_returns_diff(self, tmp_path: Path) -> None:
        result = _pull_one_source(
            backend=None,
            src_name="claude",
            src_type="claude",
            src_data={"files": {"new.md": _info("abc")}},
            did="peerA",
            dname="A",
            base_path=tmp_path,
            all_tombstones={},
            passphrase="pp",
            memory_kb=1024,
            interactive_resolve=False,
            dry_run=True,
            verbose_console=False,
        )
        assert result.dry_run_diff is not None
        assert "new.md" in result.dry_run_diff.new

    def test_claude_sync_base_set_for_claude(self, tmp_path: Path, monkeypatch) -> None:
        """Non-dry-run with changes — claude_sync_base is set for type=='claude'."""
        from mind_meld import cli as cli_module

        # Stub _download_and_apply to pretend one file was written.
        def fake_dl(backend, base_path, to_download, did, pp, mk, **kw):
            outcomes = {
                "written": list(to_download.keys()),
                "merged": [],
                "merged-via-lcs": [],
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            }
            return 42, outcomes

        monkeypatch.setattr(cli_module, "_download_and_apply", fake_dl)
        result = _pull_one_source(
            backend=None,
            src_name="claude",
            src_type="claude",
            src_data={"files": {"new.md": _info("abc")}},
            did="peerA",
            dname="A",
            base_path=tmp_path,
            all_tombstones={},
            passphrase="pp",
            memory_kb=1024,
            interactive_resolve=False,
            dry_run=False,
            verbose_console=False,
        )
        assert result.claude_sync_base == str(tmp_path)
        assert result.bytes_transferred == 42

    def test_renamed_claude_source_still_logs(self, tmp_path: Path, monkeypatch) -> None:
        """REGRESSION PIN (same-device scope): user renames their local
        claude source from 'claude' to 'my-claude' — claude_sync_base
        MUST still fire because the gate is type-keyed, not name-keyed.
        Pre-fix this silently broke the per-project sync log for anyone
        who customized source names.

        OUT OF SCOPE: cross-device rename drift. Manifests are keyed by
        src_name, so if device A renames locally but device B keeps the
        original name, B's pull skips A's remote source entirely. That's
        a bigger design change (cross-device source identity) tracked as
        a known limitation, not fixed here.
        """
        from mind_meld import cli as cli_module

        def fake_dl(backend, base_path, to_download, did, pp, mk, **kw):
            return 1, {
                "written": list(to_download.keys()),
                "merged": [],
                "merged-via-lcs": [],
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            }

        monkeypatch.setattr(cli_module, "_download_and_apply", fake_dl)
        result = _pull_one_source(
            backend=None,
            src_name="my-claude",  # user renamed
            src_type="claude",  # but type is still claude
            src_data={"files": {"a.md": _info("abc")}},
            did="peerA",
            dname="A",
            base_path=tmp_path,
            all_tombstones={},
            passphrase="pp",
            memory_kb=1024,
            interactive_resolve=False,
            dry_run=False,
            verbose_console=False,
        )
        assert result.claude_sync_base == str(tmp_path), (
            "Renamed claude source must still set claude_sync_base — "
            "gate is type-keyed, not name-keyed."
        )

    def test_non_claude_sync_base_none(self, tmp_path: Path, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        def fake_dl(backend, base_path, to_download, did, pp, mk, **kw):
            return 0, {
                k: []
                for k in [
                    "written",
                    "merged",
                    "merged-via-lcs",
                    "skipped",
                    "conflicted",
                    "unchanged",
                    "failed",
                ]
            }

        monkeypatch.setattr(cli_module, "_download_and_apply", fake_dl)
        result = _pull_one_source(
            backend=None,
            src_name="gstack",
            src_type="generic",
            src_data={"files": {"x.md": _info("abc")}},
            did="peerA",
            dname="A",
            base_path=tmp_path,
            all_tombstones={},
            passphrase="pp",
            memory_kb=1024,
            interactive_resolve=False,
            dry_run=False,
            verbose_console=False,
        )
        assert result.claude_sync_base is None

    def test_claude_named_generic_does_not_log(self, tmp_path: Path, monkeypatch) -> None:
        """Symmetric pin: a source named 'claude' but typed 'generic' must
        NOT write a sync log. Name is cosmetic; type drives behavior."""
        from mind_meld import cli as cli_module

        def fake_dl(backend, base_path, to_download, did, pp, mk, **kw):
            return 1, {
                "written": list(to_download.keys()),
                "merged": [],
                "merged-via-lcs": [],
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            }

        monkeypatch.setattr(cli_module, "_download_and_apply", fake_dl)
        result = _pull_one_source(
            backend=None,
            src_name="claude",  # name-only
            src_type="generic",  # but NOT a claude-typed source
            src_data={"files": {"x.md": _info("abc")}},
            did="peerA",
            dname="A",
            base_path=tmp_path,
            all_tombstones={},
            passphrase="pp",
            memory_kb=1024,
            interactive_resolve=False,
            dry_run=False,
            verbose_console=False,
        )
        assert result.claude_sync_base is None

    def test_tombstoned_files_filtered(self, tmp_path: Path, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        downloaded_keys: list[str] = []

        def fake_dl(backend, base_path, to_download, did, pp, mk, **kw):
            downloaded_keys.extend(to_download.keys())
            return 0, {
                k: []
                for k in [
                    "written",
                    "merged",
                    "merged-via-lcs",
                    "skipped",
                    "conflicted",
                    "unchanged",
                    "failed",
                ]
            }

        monkeypatch.setattr(cli_module, "_download_and_apply", fake_dl)
        # is_tombstoned uses f"{source}:{rel_path}" as the flat key.
        tombstones = {"claude:old.md": {"deleted_at": "2026-01-01T00:00:00Z"}}
        _pull_one_source(
            backend=None,
            src_name="claude",
            src_type="claude",
            src_data={
                "files": {
                    "old.md": _info("abc"),
                    "keep.md": _info("def"),
                }
            },
            did="peerA",
            dname="A",
            base_path=tmp_path,
            all_tombstones=tombstones,
            passphrase="pp",
            memory_kb=1024,
            interactive_resolve=False,
            dry_run=False,
            verbose_console=False,
        )
        assert "old.md" not in downloaded_keys
        assert "keep.md" in downloaded_keys


# ── _fsync_touched_parents ───────────────────────────────────────────


class TestFsyncTouchedParents:
    def test_empty_set_no_warnings(self) -> None:
        assert _fsync_touched_parents(set()) == []

    def test_success_no_warnings(self, tmp_path: Path) -> None:
        warnings = _fsync_touched_parents({tmp_path})
        assert warnings == []

    def test_failure_returns_warning(self, tmp_path: Path, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        def boom(p):
            raise StorageError("fsync failed")

        monkeypatch.setattr(cli_module.fsutil, "fsync_dir", boom)
        warnings = _fsync_touched_parents({tmp_path})
        assert len(warnings) == 1
        assert warnings[0].parent_dir == tmp_path
        assert "fsync failed" in warnings[0].error


# ── _print_pull_summary stderr routing ───────────────────────────────


class TestPrintPullSummaryStderrRouting:
    """Regression pins for the v0.8.1 visible-failure contract.

    Load-bearing warnings (corrupt peers, unknown sources, fsync failures)
    MUST reach stderr even with quiet=True, because autopull's hook caller
    is quiet-mode and silent suppression would mask data-at-risk
    conditions.
    """

    def test_corrupt_peer_stderr_in_quiet(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        _print_pull_summary(
            PullResult(),
            corrupt_peers=[_CorruptPeer(device_id="bad", device_name="BadMac")],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[],
            quiet=True,
            verbose=False,
        )
        captured = capsys.readouterr()
        assert "BadMac" in captured.err
        assert "corrupt" in captured.err

    def test_unknown_source_stderr_in_quiet(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        _print_pull_summary(
            PullResult(),
            corrupt_peers=[],
            unknown_sources=[_UnknownSourceWarning(src_name="gstack", device_name="A")],
            fsync_warnings=[],
            per_source_results=[],
            quiet=True,
            verbose=False,
        )
        captured = capsys.readouterr()
        assert "gstack" in captured.err
        assert "not configured" in captured.err

    def test_fsync_warning_stderr_in_quiet(self, tmp_path: Path, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        _print_pull_summary(
            PullResult(),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[_FsyncWarning(parent_dir=tmp_path, error="disk full")],
            per_source_results=[],
            quiet=True,
            verbose=False,
        )
        captured = capsys.readouterr()
        assert "fsync failed" in captured.err
        assert "disk full" in captured.err

    def test_quiet_suppresses_cosmetic_summary(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        _print_pull_summary(
            PullResult(total_written=5, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[],
            quiet=True,
            verbose=False,
        )
        captured = capsys.readouterr()
        # "Pull complete" is cosmetic — suppressed in quiet.
        assert "Pull complete" not in captured.out
        assert "Pull complete" not in captured.err

    def test_nonquiet_shows_cosmetic_summary(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        _print_pull_summary(
            PullResult(total_written=5, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[],
            quiet=False,
            verbose=False,
        )
        captured = capsys.readouterr()
        # Cosmetic summary goes to stdout.
        assert "Pull complete" in captured.out


# ── Track 5B Task 2 + D11 ────────────────────────────────────────────


def _make_per_source(
    src_name: str,
    device_name: str,
    *,
    conflicted: list[str] | None = None,
    failed: list[str] | None = None,
    written: list[str] | None = None,
) -> _PerSourceResult:
    """Build a _PerSourceResult fixture for summary tests."""
    return _PerSourceResult(
        src_name=src_name,
        device_name=device_name,
        device_id=device_name + "-id",
        outcomes={
            "written": written or [],
            "merged": [],
            "merged-via-lcs": [],
            "skipped": [],
            "conflicted": conflicted or [],
            "unchanged": [],
            "failed": failed or [],
        },
        bytes_transferred=0,
        touched_parents=set(),
    )


class TestPullSummaryInlinePaths:
    """Track 5B Task 2: per-source line lists conflicted/failed paths inline.

    Cap at 20 (D5: --verbose unlocks). 4-space indent under the per-source
    line preserves device→source→file hierarchy when multi-device runs
    share a source name (D10).
    """

    def test_conflicted_paths_listed_under_per_source_line(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        per_source = _make_per_source("claude", "machine-a", conflicted=["a.md", "b.md", "c.md"])
        _print_pull_summary(
            PullResult(total_conflicted=3, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=False,
            verbose=False,
        )
        out = capsys.readouterr().out
        assert "claude:" in out
        for path in ("a.md", "b.md", "c.md"):
            assert path in out

    def test_failed_paths_listed_separately(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        per_source = _make_per_source(
            "claude", "machine-a", failed=["bad-blob.md", "bad-decrypt.md"]
        )
        _print_pull_summary(
            PullResult(total_failed=2, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=False,
            verbose=False,
        )
        out = capsys.readouterr().out
        assert "bad-blob.md" in out
        assert "bad-decrypt.md" in out

    def test_cap_at_20_shows_overflow(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        paths = [f"f{i}.md" for i in range(25)]
        per_source = _make_per_source("claude", "machine-a", conflicted=paths)
        _print_pull_summary(
            PullResult(total_conflicted=25, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=False,
            verbose=False,
        )
        out = capsys.readouterr().out
        # First 20 paths listed
        for i in range(20):
            assert f"f{i}.md" in out
        # Overflow line surfaced; remainder NOT listed
        assert "and 5 more" in out
        assert "f24.md" not in out

    def test_verbose_unlocks_cap(self, capsys) -> None:
        """D5: --verbose lifts the 20-path cap on inline path display."""
        from mind_meld.cli import PullResult, _print_pull_summary

        paths = [f"f{i}.md" for i in range(25)]
        per_source = _make_per_source("claude", "machine-a", conflicted=paths)
        _print_pull_summary(
            PullResult(total_conflicted=25, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=False,
            verbose=True,
        )
        out = capsys.readouterr().out
        # All 25 listed; no overflow line
        for i in range(25):
            assert f"f{i}.md" in out
        assert "more" not in out

    def test_zero_conflicts_no_inline_list(self, capsys) -> None:
        """No conflicted/failed paths => no inline list (no false positives)."""
        from mind_meld.cli import PullResult, _print_pull_summary

        per_source = _make_per_source("claude", "machine-a", written=["new.md"])
        _print_pull_summary(
            PullResult(total_written=1, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=False,
            verbose=True,
        )
        out = capsys.readouterr().out
        # No "- " bullet lines indicating an inline path list
        assert "    - " not in out


class TestPullSummaryQuietContract:
    """Track 5B D11: quiet-mode per-source conflicts/failures must reach
    stderr. Pre-existing docstring/code mismatch — _print_pull_summary's
    docstring claimed these were load-bearing-to-stderr, but `if quiet:
    return` suppressed them. Track 5B closes the gap.

    REGRESSION: D11 quiet conflicts/failures contract.
    """

    def test_quiet_routes_per_source_conflicts_to_stderr(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        per_source = _make_per_source("claude", "machine-a", conflicted=["a.md", "b.md", "c.md"])
        _print_pull_summary(
            PullResult(total_conflicted=3, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=True,
            verbose=False,
        )
        captured = capsys.readouterr()
        # Stderr surfaces device/source prefix + file list
        assert "machine-a/claude" in captured.err
        assert "3 conflicts" in captured.err
        for path in ("a.md", "b.md", "c.md"):
            assert path in captured.err
        # Cosmetic stdout still suppressed
        assert "Pull complete" not in captured.out

    def test_quiet_routes_per_source_failed_to_stderr(self, capsys) -> None:
        from mind_meld.cli import PullResult, _print_pull_summary

        per_source = _make_per_source("claude", "machine-a", failed=["bad.md"])
        _print_pull_summary(
            PullResult(total_failed=1, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=True,
            verbose=False,
        )
        captured = capsys.readouterr()
        assert "machine-a/claude" in captured.err
        assert "1 failed" in captured.err
        assert "bad.md" in captured.err

    def test_quiet_multi_device_disambiguation(self, capsys) -> None:
        """Two devices with same-named source must be disambiguated by
        device prefix in quiet stderr (the per-device console header at
        cli.py:2422 is suppressed in quiet mode, so the stderr line is
        the only context the user gets).
        """
        from mind_meld.cli import PullResult, _print_pull_summary

        ps_a = _make_per_source("claude", "machine-a", conflicted=["x.md"])
        ps_b = _make_per_source("claude", "machine-b", conflicted=["y.md"])
        _print_pull_summary(
            PullResult(total_conflicted=2, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[ps_a, ps_b],
            quiet=True,
            verbose=False,
        )
        err = capsys.readouterr().err
        assert "machine-a/claude" in err
        assert "machine-b/claude" in err
        assert "x.md" in err
        assert "y.md" in err

    def test_quiet_zero_conflicts_no_stderr(self, capsys) -> None:
        """No conflicts/failures => no stderr noise (no false positives)."""
        from mind_meld.cli import PullResult, _print_pull_summary

        per_source = _make_per_source("claude", "machine-a", written=["new.md"])
        _print_pull_summary(
            PullResult(total_written=1, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=True,
            verbose=False,
        )
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_quiet_cap_with_overflow_marker(self, capsys) -> None:
        """quiet stderr also caps at 20 (with --verbose unlock per D5)."""
        from mind_meld.cli import PullResult, _print_pull_summary

        paths = [f"f{i}.md" for i in range(25)]
        per_source = _make_per_source("claude", "machine-a", conflicted=paths)
        _print_pull_summary(
            PullResult(total_conflicted=25, elapsed=1.0),
            corrupt_peers=[],
            unknown_sources=[],
            fsync_warnings=[],
            per_source_results=[per_source],
            quiet=True,
            verbose=False,
        )
        err = capsys.readouterr().err
        for i in range(20):
            assert f"f{i}.md" in err
        assert "and 5 more" in err
        assert "f24.md" not in err


# ── Track 5B Task 4: download progress + quiet plumbing ──────────────


class TestDownloadAndApplyQuietAndProgress:
    """Track 5B Task 4: Rich Progress widget for non-quiet TTY pulls;
    plain banner for non-quiet non-TTY; silent in quiet (autopull).

    REGRESSION: Task 4 quiet plumbing — without `quiet` threaded through
    _pull_one_source → _download_and_apply, autopull would leak progress
    output to stdout/stderr, violating its silent-mode contract.
    """

    @staticmethod
    def _force_get_failure(monkeypatch) -> None:
        """Make every backend.get raise MindMeldError so the loop body
        exits early without decrypting. We're testing the wrapper's
        gating logic (quiet, empty, non-TTY), not the per-file body.
        """
        from mind_meld.errors import MindMeldError

        def boom(*a, **kw):
            raise MindMeldError("simulated blob miss")

        # Patched on a fresh MagicMock backend so we don't hit real storage
        backend = MagicMock()
        backend.get = MagicMock(side_effect=boom)
        return backend

    def test_quiet_true_no_progress_output(self, tmp_path, monkeypatch, capsys) -> None:
        """REGRESSION: D11/Task 4 quiet contract — no widget, no banner,
        no per-file lines reach stdout/stderr in quiet mode.
        """
        from mind_meld.cli import _download_and_apply

        backend = self._force_get_failure(monkeypatch)
        bt, outcomes = _download_and_apply(
            backend,
            tmp_path,
            {"a.md": _info("abc")},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )
        captured = capsys.readouterr()
        assert "downloading" not in captured.out
        assert "downloading" not in captured.err
        # Failed outcome still recorded (per-file isolation contract)
        assert outcomes["failed"] == ["a.md"]

    def test_empty_to_download_no_widget(self, tmp_path, capsys) -> None:
        """No files to download => no Progress instantiation, no banner.

        Rich Progress with total=0 renders an empty bar; div-by-zero risk
        on percentage display in some Rich versions. Gate it out entirely.
        """
        from mind_meld.cli import _download_and_apply

        backend = MagicMock()
        bt, outcomes = _download_and_apply(
            backend,
            tmp_path,
            {},
            "peerA",
            "pp",
            1024,
            quiet=False,
        )
        captured = capsys.readouterr()
        assert "downloading" not in captured.out
        assert bt == 0
        assert outcomes == {
            "written": [],
            "merged": [],
            "merged-via-lcs": [],
            "skipped": [],
            "conflicted": [],
            "unchanged": [],
            "failed": [],
        }

    @staticmethod
    def _force_non_tty_console(monkeypatch) -> None:
        """Replace cli.console with a non-TTY Rich Console for the test."""
        import io as _io

        from rich.console import Console as _Console

        from mind_meld import cli as cli_module

        non_tty = _Console(file=_io.StringIO(), force_terminal=False)
        monkeypatch.setattr(cli_module, "console", non_tty)
        return non_tty

    def test_non_tty_emits_start_banner(self, tmp_path, monkeypatch) -> None:
        """Non-TTY non-quiet => single start banner; no rewriting widget
        (widget would garble piped output / log capture).
        """
        from mind_meld import cli as cli_module

        backend = self._force_get_failure(monkeypatch)
        non_tty = self._force_non_tty_console(monkeypatch)

        cli_module._download_and_apply(
            backend,
            tmp_path,
            {"a.md": _info("abc"), "b.md": _info("def")},
            "peerA",
            "pp",
            1024,
            quiet=False,
        )
        # Banner went to the substituted Console's StringIO, not stdout.
        out = non_tty.file.getvalue()
        assert "downloading 2 file(s)" in out

    def test_non_tty_quiet_silent(self, tmp_path, monkeypatch) -> None:
        """Non-TTY + quiet => still silent (quiet wins over banner)."""
        from mind_meld import cli as cli_module

        backend = self._force_get_failure(monkeypatch)
        non_tty = self._force_non_tty_console(monkeypatch)

        cli_module._download_and_apply(
            backend,
            tmp_path,
            {"a.md": _info("abc")},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )
        out = non_tty.file.getvalue()
        assert "downloading" not in out

    def test_quiet_suppresses_per_file_blob_key_error(self, tmp_path, monkeypatch) -> None:
        """REGRESSION: codex /review v0.9.0.

        Pre-fix, _download_and_apply printed `bad blob key` per-file via
        console.print regardless of quiet, leaking to stdout in autopull.
        D11 contract requires per-source totals reach stderr via
        _print_pull_summary; per-file decoration must be suppressed in
        quiet mode.
        """
        from mind_meld import cli as cli_module

        non_tty = self._force_non_tty_console(monkeypatch)

        # Mock backend never gets called because blob_key raises ValueError.
        backend = MagicMock()

        # info has empty sha256 → blob_key raises ValueError → bad blob key path
        cli_module._download_and_apply(
            backend,
            tmp_path,
            {"a.md": {"sha256": "", "size": 0}},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )
        out = non_tty.file.getvalue()
        # Per-file error must NOT leak in quiet mode
        assert "bad blob key" not in out
        assert "local preserved" not in out

    def test_quiet_suppresses_per_file_blob_missing(self, tmp_path, monkeypatch) -> None:
        """REGRESSION: codex /review v0.9.0 — same contract for blob-missing path.

        With verbose AND quiet, the verbose-gated blob-missing line must
        still suppress (quiet wins).
        """
        from mind_meld import cli as cli_module

        non_tty = self._force_non_tty_console(monkeypatch)
        backend = self._force_get_failure(monkeypatch)

        cli_module._download_and_apply(
            backend,
            tmp_path,
            {"a.md": _info("abc")},
            "peerA",
            "pp",
            1024,
            verbose=True,
            quiet=True,
        )
        out = non_tty.file.getvalue()
        assert "blob missing" not in out


# ── Codex regression pins ────────────────────────────────────────────


class TestHadChangesExcludesUnchanged:
    """Regression: old code excluded 'unchanged' from device_had_changes.

    If only 'unchanged' outcomes were present, _cleanup_conflict_copies was
    NOT called. This matters when a peer's canonical manifest is corrupt and
    we recovered via an iCloud conflict copy — cleanup would delete the
    valid conflict copy, leaving only the corrupt canonical (permanent
    corruption for future pulls). Codex caught this during adversarial
    review.
    """

    def test_unchanged_only_is_not_changes(self) -> None:
        result = _PerSourceResult(
            src_name="claude",
            device_name="A",
            device_id="peerA",
            outcomes={
                "written": [],
                "merged": [],
                "merged-via-lcs": [],
                "skipped": [],
                "conflicted": [],
                "unchanged": ["stale.md"],
                "failed": [],
            },
            bytes_transferred=0,
            touched_parents=set(),
        )
        assert result.had_changes is False

    def test_skipped_only_is_changes(self) -> None:
        """One-way-sync (always local-newer) must still trigger cleanup."""
        result = _PerSourceResult(
            src_name="claude",
            device_name="A",
            device_id="peerA",
            outcomes={
                "written": [],
                "merged": [],
                "merged-via-lcs": [],
                "skipped": ["a.md", "b.md"],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            },
            bytes_transferred=0,
            touched_parents=set(),
        )
        assert result.had_changes is True

    def test_failed_only_is_changes(self) -> None:
        result = _PerSourceResult(
            src_name="claude",
            device_name="A",
            device_id="peerA",
            outcomes={
                "written": [],
                "merged": [],
                "merged-via-lcs": [],
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": ["bad.md"],
            },
            bytes_transferred=0,
            touched_parents=set(),
        )
        assert result.had_changes is True

    def test_empty_outcomes_is_not_changes(self) -> None:
        result = _PerSourceResult(
            src_name="claude",
            device_name="A",
            device_id="peerA",
            outcomes={
                "written": [],
                "merged": [],
                "merged-via-lcs": [],
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            },
            bytes_transferred=0,
            touched_parents=set(),
        )
        assert result.had_changes is False


class TestWarningsSurvivePartialPull:
    """Regression: load-bearing warnings must reach stderr even if mid-pull
    operations (write_sync_log, _cleanup_conflict_copies) raise. Codex caught
    this during adversarial review.
    """

    def _build_config(self, tmp_path: Path) -> dict:
        return {
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

    def test_corrupt_peer_warning_survives_cleanup_exception(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """If _cleanup_conflict_copies raises, corrupt-peer warning from the
        prefetch phase must still reach stderr via _print_pull_summary.
        """
        from mind_meld import cli as cli_module
        from mind_meld.cli import ManifestFetch, _pull_core
        from mind_meld.errors import StorageError

        # Build a config
        (tmp_path / "storage").mkdir()
        (tmp_path / "claude").mkdir()
        config = self._build_config(tmp_path)

        # Set up scenario: 2 peers — one corrupt (warning accumulates),
        # one with changes (triggers cleanup that will raise).
        def fake_list_devices_warn(b):
            return [
                {"device_id": "selfdev", "device_name": "me"},
                {"device_id": "badpeer", "device_name": "BadMac"},
                {"device_id": "goodpeer", "device_name": "GoodMac"},
            ]

        def fake_fetch_remote_manifest(b, did, pp, mk):
            if did == "badpeer":
                return ManifestFetch(status="corrupt")
            if did == "goodpeer":
                return ManifestFetch(
                    status="ok",
                    manifest={
                        "sources": {
                            "claude": {
                                "files": {
                                    "test.md": {
                                        "sha256": "abc",
                                        "size": 5,
                                        "mtime": "2026-01-01T00:00:00Z",
                                    }
                                }
                            }
                        },
                        "tombstones": {},
                    },
                )
            return ManifestFetch(status="missing")

        def fake_download_and_apply(b, bp, td, did, pp, mk, **kw):
            return 10, {
                "written": list(td.keys()),
                "merged": [],
                "merged-via-lcs": [],
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            }

        def raising_cleanup(b, did, pp, mk):
            raise StorageError("simulated iCloud cleanup failure")

        # Stub out crypto-init bootstrap (get_backend should work; we
        # monkeypatch the deeper operations).
        monkeypatch.setattr(cli_module, "_list_devices_warn", fake_list_devices_warn)
        monkeypatch.setattr(cli_module, "_fetch_remote_manifest", fake_fetch_remote_manifest)
        monkeypatch.setattr(cli_module, "_download_and_apply", fake_download_and_apply)
        monkeypatch.setattr(cli_module, "_cleanup_conflict_copies", raising_cleanup)
        monkeypatch.setattr(cli_module, "get_backend", lambda c: None)
        # Track 5E: bypass the fleet-version check + migration sweep so
        # the test stays scoped to corrupt-peer warning routing.
        monkeypatch.setattr(cli_module, "_check_fleet_version_or_refuse", lambda *a, **kw: None)
        monkeypatch.setattr(resolveflow, "_find_conflict_files", lambda *a, **kw: [])
        monkeypatch.setattr(
            cli_module,
            "collect_tombstones",
            lambda *a, **kw: {},
        )

        # Run in quiet mode to assert stderr routing survives.
        _pull_core(
            config=config,
            passphrase="pp",
            memory_kb=1024,
            quiet=True,
        )

        captured = capsys.readouterr()
        # Corrupt-peer warning MUST survive even though cleanup raised.
        assert "BadMac" in captured.err
        assert "corrupt" in captured.err
        # Cleanup failure ALSO surfaces as stderr warning.
        assert "cleanup failed" in captured.err


class TestWriteSyncLogBestEffort:
    """Regression: write_sync_log failure must not abort the pull or lose
    accumulated warnings."""

    def test_sync_log_oserror_surfaces_as_warning(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from mind_meld import cli as cli_module
        from mind_meld.cli import ManifestFetch, _pull_core

        (tmp_path / "storage").mkdir()
        (tmp_path / "claude").mkdir()
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
                {"device_id": "peerA", "device_name": "A"},
            ]

        def fake_fetch_remote_manifest(b, did, pp, mk):
            if did == "peerA":
                return ManifestFetch(
                    status="ok",
                    manifest={
                        "sources": {
                            "claude": {
                                "files": {
                                    "x.md": {
                                        "sha256": "abc",
                                        "size": 5,
                                        "mtime": "2026-01-01T00:00:00Z",
                                    }
                                }
                            }
                        },
                        "tombstones": {},
                    },
                )
            return ManifestFetch(status="missing")

        def fake_download_and_apply(b, bp, td, did, pp, mk, **kw):
            return 10, {
                "written": list(td.keys()),
                "merged": [],
                "merged-via-lcs": [],
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            }

        def boom_write_sync_log(**kw):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module, "_list_devices_warn", fake_list_devices_warn)
        monkeypatch.setattr(cli_module, "_fetch_remote_manifest", fake_fetch_remote_manifest)
        monkeypatch.setattr(cli_module, "_download_and_apply", fake_download_and_apply)
        monkeypatch.setattr(cli_module, "_cleanup_conflict_copies", lambda *a, **kw: 0)
        monkeypatch.setattr(cli_module, "write_sync_log", boom_write_sync_log)
        monkeypatch.setattr(cli_module, "get_backend", lambda c: None)
        # Track 5E: _pull_core's fleet-version check fires before any
        # other I/O. This unit test stubs get_backend → None to isolate
        # sync-log behavior, so the fleet check has no real backend to
        # query — bypass it.
        monkeypatch.setattr(cli_module, "_check_fleet_version_or_refuse", lambda *a, **kw: None)
        monkeypatch.setattr(resolveflow, "_find_conflict_files", lambda *a, **kw: [])
        monkeypatch.setattr(
            cli_module,
            "collect_tombstones",
            lambda *a, **kw: {},
        )

        # Should NOT raise; returns a partial PullResult.
        result = _pull_core(
            config=config,
            passphrase="pp",
            memory_kb=1024,
            quiet=True,
        )
        assert result.total_written == 1
        captured = capsys.readouterr()
        assert "sync log write failed" in captured.err
        assert "disk full" in captured.err


# ── init helpers (Track 2A decomposition) ────────────────────────────


class TestLoadPriorDeviceMetadata:
    """_load_prior_device_metadata — best-effort read of prior (id, name)."""

    def test_no_config_returns_none_tuple(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        assert _load_prior_device_metadata() == (None, None)

    def test_readable_config_returns_id_and_name(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[device]\nid = "abc123"\nname = "OldMac"\n[storage]\npath = "/tmp/x"\n')
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        assert _load_prior_device_metadata() == ("abc123", "OldMac")

    def test_malformed_config_returns_none_tuple(self, tmp_path: Path, monkeypatch) -> None:
        """Broken config doesn't crash init; best-effort returns Nones so
        the orphan-case warning just loses the descriptive name."""
        cfg = tmp_path / "config.toml"
        cfg.write_text("this is not: valid [toml at all\n")
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        assert _load_prior_device_metadata() == (None, None)


class TestPromptPassphrase:
    """_prompt_passphrase — double-prompt on first-device, single otherwise."""

    def test_first_device_match(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        responses = iter(["pw123", "pw123"])
        monkeypatch.setattr(cli_module.typer, "prompt", lambda *a, **kw: next(responses))
        assert _prompt_passphrase(is_first_device=True) == "pw123"

    def test_first_device_mismatch_exits(self, monkeypatch) -> None:
        import typer as _typer

        from mind_meld import cli as cli_module

        responses = iter(["pw123", "pw456"])
        monkeypatch.setattr(cli_module.typer, "prompt", lambda *a, **kw: next(responses))
        with pytest.raises(_typer.Exit):
            _prompt_passphrase(is_first_device=True)

    def test_first_device_empty_exits(self, monkeypatch) -> None:
        import typer as _typer

        from mind_meld import cli as cli_module

        monkeypatch.setattr(cli_module.typer, "prompt", lambda *a, **kw: "")
        with pytest.raises(_typer.Exit):
            _prompt_passphrase(is_first_device=True)

    def test_second_device_single_prompt(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        calls: list[int] = []

        def counting_prompt(*a, **kw):
            calls.append(1)
            return "pw-shared"

        monkeypatch.setattr(cli_module.typer, "prompt", counting_prompt)
        assert _prompt_passphrase(is_first_device=False) == "pw-shared"
        assert len(calls) == 1  # single prompt, no confirm

    def test_second_device_empty_exits(self, monkeypatch) -> None:
        import typer as _typer

        from mind_meld import cli as cli_module

        monkeypatch.setattr(cli_module.typer, "prompt", lambda *a, **kw: "")
        with pytest.raises(_typer.Exit):
            _prompt_passphrase(is_first_device=False)


class TestPromptSources:
    """_prompt_sources — per-source Y/n prompt; returns enabled entries."""

    def test_all_declined_returns_only_internal_sources(self, monkeypatch) -> None:
        """User declines every prompt — mm-internal sources (mm-events)
        still auto-include. The init guard treats this as 'no user-facing
        sources' and refuses; here we just pin the prompt-level shape."""
        from mind_meld import cli as cli_module

        monkeypatch.setattr(cli_module.typer, "confirm", lambda *a, **kw: False)
        result = _prompt_sources()
        assert [s["name"] for s in result] == ["mm-events"]

    def test_all_accepted_returns_every_default(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        monkeypatch.setattr(cli_module.typer, "confirm", lambda *a, **kw: True)
        result = _prompt_sources()
        names = [s["name"] for s in result]
        assert names == [s["name"] for s in DEFAULT_SOURCES]

    def test_returns_deep_copies_not_aliases(self, monkeypatch) -> None:
        """Mutating the returned dict must not pollute DEFAULT_SOURCES —
        Issue 1C's aliasing guard (get_default_source deep-copies)."""
        from mind_meld import cli as cli_module

        monkeypatch.setattr(cli_module.typer, "confirm", lambda *a, **kw: True)
        result = _prompt_sources()
        for src in result:
            src["path"] = "/mutated/value"
        # DEFAULT_SOURCES still has its original paths
        assert DEFAULT_SOURCES[0]["path"] == "~/.claude"

    def test_mm_init_does_not_prompt_for_opencode(self, monkeypatch) -> None:
        """Track 37B: DEFAULT_SOURCES no longer ships opencode, so init asks
        for the five remaining user-facing names (claude, gstack,
        gstack-extend, codex, grok). mm-events auto-includes."""
        from mind_meld import cli as cli_module

        prompted: list[str] = []

        def capture(prompt, default):
            prompted.append(prompt)
            return False

        monkeypatch.setattr(cli_module.typer, "confirm", capture)
        _prompt_sources()
        user_facing = [s["name"] for s in DEFAULT_SOURCES if s["name"] != "mm-events"]
        assert len(user_facing) == 5
        assert "opencode" not in user_facing
        assert len(prompted) == 5
        assert all("opencode" not in p for p in prompted)

    def test_claude_only(self, monkeypatch) -> None:
        """User answers Y for claude and n for the other agent sources. mm-events is
        mm-internal infrastructure and auto-includes without prompting,
        so it appears in the result list alongside claude."""
        from mind_meld import cli as cli_module

        responses = iter([True, False, False, False, False, False])
        monkeypatch.setattr(cli_module.typer, "confirm", lambda *a, **kw: next(responses))
        result = _prompt_sources()
        assert [s["name"] for s in result] == ["claude", "mm-events"]

    def test_gstack_only_preserves_include_fields(self, monkeypatch) -> None:
        """The gstack default carries include_dirs / include_files — they
        must survive the indirection through get_default_source. mm-events
        auto-includes without prompting (mm-internal infrastructure)."""
        from mind_meld import cli as cli_module

        responses = iter([False, True, False, False, False, False])
        monkeypatch.setattr(cli_module.typer, "confirm", lambda *a, **kw: next(responses))
        result = _prompt_sources()
        assert [s["name"] for s in result] == ["mm-events", "gstack"]
        gstack = next(s for s in result if s["name"] == "gstack")
        assert "projects" in gstack["include_dirs"]
        assert "retro-context.md" in gstack["include_files"]
        # v0.9.3: exclude_patterns is also load-bearing now — pin that it
        # survives indirection AND contains the new config.yaml exclude.
        assert "config.yaml" in gstack["exclude_patterns"]

    def test_init_uses_one_detection_snapshot_per_user_source(self, monkeypatch) -> None:
        """Prompt copy and default must share one filesystem observation."""
        from mind_meld import cli as cli_module

        probes: list[str] = []

        class CountingPath:
            def __init__(self, value: str):
                self.value = value

            def expanduser(self):
                return self

            def __truediv__(self, child: str):
                return CountingPath(f"{self.value}/{child}")

            def exists(self) -> bool:
                probes.append(self.value)
                return True

            def is_dir(self) -> bool:
                probes.append(self.value)
                return True

            def is_symlink(self) -> bool:
                return False

            def lstat(self):
                probes.append(self.value)
                return os.stat_result((0o040755, 1, 0, 1, 0, 0, 0, 0, 0, 0))

        prompts: list[tuple[str, bool]] = []

        def confirm(prompt: str, *, default: bool) -> bool:
            prompts.append((prompt, default))
            return False

        monkeypatch.setattr(cli_module, "Path", CountingPath)
        monkeypatch.setattr(cli_module.typer, "confirm", confirm)

        _prompt_sources()

        user_sources = [s for s in DEFAULT_SOURCES if s["name"] != "mm-events"]
        assert len(probes) == len(user_sources)
        assert all(default is True for _prompt, default in prompts)
        assert all("detected" in prompt for prompt, _default in prompts)


class TestRegisterAndSave:
    """_register_and_save — device register → config write → keyring store.

    Track 5D (v0.9.4) inverted the original Track 5A ordering. The
    canonical 'remote first, local pointer last' pattern means a SIGKILL
    or normal save_config failure leaves at most an inert orphan device
    entry in storage (recoverable on retry init) instead of the inverse
    half-state where local config claims a device storage doesn't know.
    """

    def test_ordering(self, tmp_path: Path, monkeypatch) -> None:
        """Track 5D order: register MUST run before save_config so a
        save failure at most leaves an orphan storage breadcrumb, never
        an orphan local config that silently breaks pushes."""
        from mind_meld import cli as cli_module

        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)

        call_order: list[str] = []

        original_save = cli_module.save_config

        def tracking_save(c, path=None):
            call_order.append("save")
            return original_save(c, path)

        def tracking_register(backend, did, dname):
            call_order.append("register")

        def tracking_keyring(pw):
            call_order.append("keyring")
            return True

        monkeypatch.setattr(cli_module, "save_config", tracking_save)
        monkeypatch.setattr(cli_module, "register_device", tracking_register)
        monkeypatch.setattr(cli_module, "store_passphrase_in_keyring", tracking_keyring)

        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path)},
        }
        _register_and_save(
            config, backend=MagicMock(), device_id="d1", device_name="Mac", passphrase="pw"
        )
        assert call_order == ["register", "save", "keyring"]

    def test_no_keyring_still_succeeds(self, tmp_path: Path, monkeypatch) -> None:
        """Keyring unavailable (lambda _pw: False) → function completes
        without raising; caller sees a yellow warning on stdout."""
        from mind_meld import cli as cli_module

        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)

        monkeypatch.setattr(cli_module, "register_device", lambda *a, **kw: None)
        monkeypatch.setattr(cli_module, "store_passphrase_in_keyring", lambda _pw: False)

        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path)},
        }
        # Must not raise.
        _register_and_save(
            config, backend=MagicMock(), device_id="d1", device_name="Mac", passphrase="pw"
        )

    def test_register_failure_does_not_save_config(self, tmp_path: Path, monkeypatch) -> None:
        """Track 5D Task 2 (replaces test_register_failure_rolls_back_saved_config):
        register_device raises before save_config runs, so there is no
        local config to roll back — the saved-config file must simply
        not exist. Storage has nothing because register failed atomically."""
        from mind_meld import cli as cli_module

        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)

        save_called = []

        def tracking_save(c, path=None):
            save_called.append(True)

        monkeypatch.setattr(cli_module, "save_config", tracking_save)

        def boom(*_a, **_kw):
            raise StorageError("transient iCloud put failure")

        monkeypatch.setattr(cli_module, "register_device", boom)

        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path)},
        }
        with pytest.raises(StorageError, match="transient iCloud put failure"):
            _register_and_save(
                config, backend=MagicMock(), device_id="d1", device_name="Mac", passphrase="pw"
            )
        assert save_called == [], "save_config must not run when register fails"
        assert not cfg.exists()

    def test_save_config_failure_after_register_triggers_cleanup(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Track 5D CMT-2: when save_config raises after register
        succeeded, _register_and_save best-effort-deletes the just-
        registered devices/<id>.json so retry init doesn't trip
        _init_storage_guard's orphan-case warning. Original save_config
        error propagates regardless of cleanup outcome."""
        from mind_meld import cli as cli_module
        from mind_meld.storage.keys import device_key

        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)

        backend = MagicMock()
        monkeypatch.setattr(cli_module, "register_device", lambda *a, **kw: None)

        def boom_save(_c, path=None):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module, "save_config", boom_save)

        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path)},
        }
        with pytest.raises(OSError, match="disk full"):
            _register_and_save(
                config, backend=backend, device_id="d1", device_name="Mac", passphrase="pw"
            )
        # Cleanup hit the right storage key.
        backend.delete.assert_called_once_with(device_key("d1"))

    def test_save_config_failure_when_cleanup_also_fails_propagates_save_error(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Defense: if backend.delete itself raises during cleanup, the
        original save_config error must still be the one the user sees.
        Masking the real cause behind a confusing cleanup error would
        hide what actually went wrong (same defense the original
        rollback-unlink test pinned, now phrased for the new ordering)."""
        from mind_meld import cli as cli_module

        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)

        backend = MagicMock()
        backend.delete.side_effect = StorageError("storage cleanup also failed")
        monkeypatch.setattr(cli_module, "register_device", lambda *a, **kw: None)

        def boom_save(_c, path=None):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module, "save_config", boom_save)

        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path)},
        }
        # Original save error wins, not the cleanup error.
        with pytest.raises(OSError, match="disk full"):
            _register_and_save(
                config, backend=backend, device_id="d1", device_name="Mac", passphrase="pw"
            )
        # Cleanup failure was surfaced via the visible-failure contract.
        captured = capsys.readouterr()
        assert "cleanup of" in captured.err
        assert "storage cleanup also failed" in captured.err

    def test_register_failure_does_not_print_committed_messages(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """User-facing message ordering: 'Device registered' and 'Config
        written' are committed-state confirmations; on register failure
        neither should appear since no durable step succeeded."""
        from mind_meld import cli as cli_module

        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)

        def boom(*_a, **_kw):
            raise StorageError("nope")

        monkeypatch.setattr(cli_module, "register_device", boom)

        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path)},
        }
        with pytest.raises(StorageError):
            _register_and_save(
                config, backend=MagicMock(), device_id="d1", device_name="Mac", passphrase="pw"
            )
        captured = capsys.readouterr()
        # Neither commit-confirmation line should have been emitted.
        assert "Config written" not in captured.out
        assert "Device registered" not in captured.out


class TestEnsureDeviceRegistered:
    """Track 5D Task 2b — push-time self-heal for missing device entry.

    Two scenarios converge here:
      * Future v0.9.4+ SIGKILL crash between register_device and
        save_config in _register_and_save.
      * Pre-v0.9.4 victims of the v0.8.15..v0.9.3 inverted half-state
        (config has device_id, storage's devices/<id>.json missing).
    """

    def test_self_heals_missing_device_entry(self, tmp_path: Path, monkeypatch) -> None:
        """When devices/<id>.json is absent in storage, push entry calls
        register_device to recreate it. Pre-existing victims of the
        inverted half-state self-heal on first push after upgrade."""
        from mind_meld.cli import _ensure_device_registered
        from mind_meld.storage.keys import device_key

        backend = MagicMock()
        backend.exists.return_value = False
        registered = []

        def fake_register(b, did, dname):
            registered.append((b, did, dname))

        monkeypatch.setattr("mind_meld.cli.register_device", fake_register)

        _ensure_device_registered(backend, "d1", "Mac")

        backend.exists.assert_called_once_with(device_key("d1"))
        assert registered == [(backend, "d1", "Mac")]

    def test_no_op_when_device_already_registered(self, tmp_path: Path, monkeypatch) -> None:
        """When devices/<id>.json already exists, register_device is NOT
        called. Self-heal must not re-register on every push (would reset
        `registered` timestamp and look noisy in `mm devices`)."""
        from mind_meld.cli import _ensure_device_registered

        backend = MagicMock()
        backend.exists.return_value = True
        registered = []

        def fake_register(b, did, dname):
            registered.append((b, did, dname))

        monkeypatch.setattr("mind_meld.cli.register_device", fake_register)

        _ensure_device_registered(backend, "d1", "Mac")

        assert registered == [], "register_device must not run when entry exists"

    def test_self_heal_register_failure_propagates(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """If register_device raises during self-heal (transient iCloud,
        permissions), the push aborts with that error AND a stderr
        warning lands first. The warning is load-bearing: autopush's
        generic `except Exception` would otherwise swallow the failure
        and silently no-op every push, violating the visible-failure
        contract for degraded-state signals."""
        from mind_meld.cli import _ensure_device_registered

        backend = MagicMock()
        backend.exists.return_value = False

        def boom(*_a, **_kw):
            raise StorageError("transient iCloud put failure")

        monkeypatch.setattr("mind_meld.cli.register_device", boom)

        with pytest.raises(StorageError, match="transient iCloud put failure"):
            _ensure_device_registered(backend, "d1", "Mac")
        captured = capsys.readouterr()
        # Visible-failure contract: warning reached stderr before re-raise.
        assert "mm: warning: device entry self-heal failed" in captured.err
        assert "StorageError" in captured.err

    def test_dry_run_skips_self_heal(self, tmp_path: Path, monkeypatch) -> None:
        """Track 5D codex review 2026-04-25: `mm push --dry-run` must
        not mutate storage, even when the device entry is missing. The
        self-heal does a `backend.put` via register_device, so dry_run
        gating is required to honor --dry-run's preview-only contract."""
        from mind_meld.cli import _ensure_device_registered

        backend = MagicMock()
        backend.exists.return_value = False
        registered = []

        def fake_register(b, did, dname):
            registered.append((b, did, dname))

        monkeypatch.setattr("mind_meld.cli.register_device", fake_register)

        _ensure_device_registered(backend, "d1", "Mac", dry_run=True)

        assert backend.exists.call_count == 0, (
            "dry_run must not even probe storage — short-circuit before any I/O"
        )
        assert registered == [], "dry_run must not call register_device"


class TestBootstrapOrVerifyCrypto:
    """_bootstrap_or_verify_crypto — one spot check for the lost-race path.

    The happy-path branches are covered end-to-end by TestInitFlow; here
    we pin the lost-race path that's hard to exercise via CliRunner.
    """

    def test_first_device_lost_race_falls_through_to_verify(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """bootstrap raises StorageError → fall through to retry_fetch +
        verify_passphrase. Pins the rare lost-race branch."""
        from mind_meld import cli as cli_module
        from mind_meld.crypto import CryptoInitFetch

        # bootstrap raises (simulating race)
        def raising_bootstrap(backend, pp, argon2_memory_kb):
            raise StorageError("concurrent put")

        # retry_fetch returns a valid winner
        winner_salt = b"\x00" * 16
        winner_keycheck = b"\x00" * 32

        def fake_retry_fetch(backend):
            return CryptoInitFetch(
                status="ok",
                root_salt=winner_salt,
                argon2_memory_kb=1024,
                keycheck_blob=winner_keycheck,
            )

        monkeypatch.setattr(cli_module, "bootstrap_crypto_init", raising_bootstrap)
        monkeypatch.setattr(cli_module, "fetch_crypto_init", fake_retry_fetch)
        monkeypatch.setattr(cli_module, "load_master_key", lambda *a, **kw: b"\x00" * 32)
        monkeypatch.setattr(cli_module, "verify_passphrase", lambda *a, **kw: None)
        monkeypatch.setattr(cli_module, "set_crypto_session", lambda *a, **kw: None)

        # Seed fetch (not used on first-device path but required as param)
        seed_fetch = CryptoInitFetch(status="missing")
        rs, mk, kc = _bootstrap_or_verify_crypto(
            backend=None, passphrase="pw", is_first_device=True, fetch=seed_fetch
        )
        assert rs == winner_salt
        assert mk == 1024
        assert kc == winner_keycheck

    @pytest.mark.parametrize("status", ["missing", "corrupt"])
    def test_first_device_lost_race_refuses_non_ok_winner(self, monkeypatch, status: str) -> None:
        """A bootstrap loser must re-fetch and refuse a missing/corrupt winner."""
        import typer

        from mind_meld import cli as cli_module
        from mind_meld.crypto import CryptoInitFetch

        monkeypatch.setattr(
            cli_module,
            "bootstrap_crypto_init",
            lambda *args, **kwargs: (_ for _ in ()).throw(StorageError("concurrent put")),
        )
        monkeypatch.setattr(
            cli_module,
            "fetch_crypto_init",
            lambda _backend: CryptoInitFetch(status=status),
        )

        with pytest.raises(typer.Exit):
            _bootstrap_or_verify_crypto(
                backend=None,
                passphrase="pw",
                is_first_device=True,
                fetch=CryptoInitFetch(status="missing"),
            )


class TestDownloadAndApplyPathTraversalGuard:
    """Belt-and-braces defense in `_download_and_apply` (v0.11.21).

    `manifest.load_manifest` rejects malformed rel_paths at the front door,
    but a future load path that bypasses that boundary (legacy on-disk
    cache, hand-built test fixture) must STILL not let a peer-controlled
    `..` segment or absolute path escape the source root. This test
    constructs a `to_download` dict directly (skipping load_manifest) and
    confirms the cli.py-side `is_relative_to(base_path)` guard catches it.

    Threat: passphrase + storage-write attacker mints a manifest containing
    `'../../.ssh/authorized_keys'` -> `_download_and_apply` would otherwise
    `mkdir(parents=True)` and `atomic_write_bytes` outside `tmp_path`.
    """

    def _patch_decrypt_chain(self, monkeypatch, payload: bytes) -> MagicMock:
        """Patch backend.get/decrypt so any rel_path that survives the
        guard reaches `_apply_incoming_file` with `payload`. We assert
        on outcome + filesystem state to detect escapes."""
        from mind_meld import cli as cli_module

        backend = MagicMock()
        backend.get = MagicMock(return_value=b"opaque-ciphertext")
        monkeypatch.setattr(cli_module, "decrypt", lambda *a, **kw: payload)
        return backend

    def test_rejects_parent_dir_escape(self, tmp_path, monkeypatch) -> None:
        from mind_meld.cli import _download_and_apply

        backend = self._patch_decrypt_chain(monkeypatch, b"attacker-bytes")
        # Construct a base inside tmp_path with a known sibling we can
        # check stayed unwritten.
        base = tmp_path / "src" / "claude"
        base.mkdir(parents=True)
        sentinel_outside = tmp_path / "should_not_exist"
        # Two `..` segments climb from <tmp_path>/src/claude back to
        # <tmp_path>, then write `should_not_exist` — outside `base`.
        bad_rel = "../../should_not_exist"

        apply_spy = MagicMock()
        monkeypatch.setattr("mind_meld.cli._apply_incoming_file", apply_spy)
        bt, outcomes = _download_and_apply(
            backend,
            base,
            {bad_rel: _info(_sha(b"attacker-bytes"))},
            "peerA",
            "pp",
            1024,
            quiet=False,
        )
        # File must NOT have been written outside the source root.
        assert not sentinel_outside.exists()
        # Outcome must be `failed` (per-file isolation, not raise).
        assert outcomes["failed"] == [bad_rel]
        assert outcomes["written"] == []
        apply_spy.assert_not_called()

    def test_rejects_absolute_path_override(self, tmp_path, monkeypatch) -> None:
        from mind_meld.cli import _download_and_apply

        backend = self._patch_decrypt_chain(monkeypatch, b"attacker-bytes")
        base = tmp_path / "src" / "claude"
        base.mkdir(parents=True)
        # Absolute key — `Path(base) / '/abs/...'` returns the abs path,
        # overriding base entirely.
        abs_target = tmp_path / "absolute_escape_target"
        bad_rel = str(abs_target)

        apply_spy = MagicMock()
        monkeypatch.setattr("mind_meld.cli._apply_incoming_file", apply_spy)
        bt, outcomes = _download_and_apply(
            backend,
            base,
            {bad_rel: _info(_sha(b"attacker-bytes"))},
            "peerA",
            "pp",
            1024,
            quiet=False,
        )
        assert not abs_target.exists()
        assert outcomes["failed"] == [bad_rel]
        assert outcomes["written"] == []
        apply_spy.assert_not_called()

    def test_accepts_legitimate_nested_path(self, tmp_path, monkeypatch) -> None:
        """Sanity: the guard does NOT false-positive on legitimate
        nested paths inside the source root."""
        from mind_meld.cli import _download_and_apply

        backend = self._patch_decrypt_chain(monkeypatch, b"good-bytes")
        base = tmp_path / "src" / "claude"
        base.mkdir(parents=True)
        good_rel = "memory/deeply/nested/file.md"

        bt, outcomes = _download_and_apply(
            backend,
            base,
            {good_rel: _info(_sha(b"good-bytes"))},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )
        # File written, no failure
        assert (base / good_rel).exists()
        assert (base / good_rel).read_bytes() == b"good-bytes"
        assert outcomes["written"] == [good_rel]
        assert outcomes["failed"] == []

    @pytest.mark.parametrize("dangling", [False, True])
    def test_preserves_local_symlink_destination(
        self, tmp_path, monkeypatch, dangling: bool
    ) -> None:
        from mind_meld.cli import _download_and_apply

        backend = self._patch_decrypt_chain(monkeypatch, b"peer-bytes")
        base = tmp_path / "src"
        base.mkdir()
        target = tmp_path / "managed-agents.md"
        if not dangling:
            target.write_text("managed")
        local_link = base / "AGENTS.md"
        local_link.symlink_to(target)

        _, outcomes = _download_and_apply(
            backend,
            base,
            {"AGENTS.md": _info(_sha(b"peer-bytes"))},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )

        assert local_link.is_symlink()
        assert outcomes["skipped"] == ["AGENTS.md"]
        assert outcomes["failed"] == []
        assert not target.exists() if dangling else target.read_text() == "managed"

    def test_preserves_symlinked_parent_below_source_root(self, tmp_path, monkeypatch) -> None:
        from mind_meld.cli import _download_and_apply

        backend = self._patch_decrypt_chain(monkeypatch, b"peer-bytes")
        base = tmp_path / "src"
        base.mkdir()
        managed = tmp_path / "managed-skills"
        managed.mkdir()
        (base / "skills").symlink_to(managed, target_is_directory=True)

        _, outcomes = _download_and_apply(
            backend,
            base,
            {"skills/tool/SKILL.md": _info(_sha(b"peer-bytes"))},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )

        assert (base / "skills").is_symlink()
        assert not (managed / "tool" / "SKILL.md").exists()
        assert outcomes["skipped"] == ["skills/tool/SKILL.md"]

    def test_allows_a_symlinked_source_root(self, tmp_path, monkeypatch) -> None:
        from mind_meld.cli import _download_and_apply

        backend = self._patch_decrypt_chain(monkeypatch, b"peer-bytes")
        actual_base = tmp_path / "actual-src"
        actual_base.mkdir()
        base_link = tmp_path / "source-link"
        base_link.symlink_to(actual_base, target_is_directory=True)

        _, outcomes = _download_and_apply(
            backend,
            base_link,
            {"skills/tool/SKILL.md": _info(_sha(b"peer-bytes"))},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )

        assert (actual_base / "skills" / "tool" / "SKILL.md").read_bytes() == b"peer-bytes"
        assert outcomes["written"] == ["skills/tool/SKILL.md"]

    def test_direct_apply_preserves_leaf_symlink(self, tmp_path) -> None:
        from mind_meld.cli import _apply_incoming_file

        target = tmp_path / "managed-agents.md"
        target.write_text("managed")
        local_link = tmp_path / "AGENTS.md"
        local_link.symlink_to(target)

        outcome = _apply_incoming_file(
            local_link,
            "AGENTS.md",
            b"peer-bytes",
            _info(_sha(b"peer-bytes")),
            "peerA",
        )

        assert outcome == "skipped"
        assert local_link.is_symlink()
        assert target.read_text() == "managed"

    def test_non_quiet_symlink_skip_emits_one_breadcrumb(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        from mind_meld.cli import _download_and_apply

        backend = self._patch_decrypt_chain(monkeypatch, b"peer-bytes")
        base = tmp_path / "src"
        base.mkdir()
        target = tmp_path / "managed-agents.md"
        target.write_text("managed")
        (base / "AGENTS.md").symlink_to(target)

        _download_and_apply(
            backend,
            base,
            {"AGENTS.md": _info(_sha(b"peer-bytes"))},
            "peerA",
            "pp",
            1024,
        )

        output = capsys.readouterr().out
        assert output.count("skipped (local symlink preserved)") == 1


class TestIncomingDigestCheck:
    """Receiving plaintext must match the advertised digest before apply."""

    def test_mismatch_fails_before_apply_and_preserves_bytes(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        from mind_meld.cli import _download_and_apply

        backend = MagicMock()
        backend.get = MagicMock(return_value=b"opaque-ciphertext")
        monkeypatch.setattr("mind_meld.cli.decrypt", lambda *a, **kw: b"wrong-bytes")
        apply_spy = MagicMock()
        monkeypatch.setattr("mind_meld.cli._apply_incoming_file", apply_spy)

        base = tmp_path / "src"
        base.mkdir()
        canonical = base / "notes.md"
        canonical.write_text("local notes")
        sidecar = base / "notes.sync-conflict-20260101-000000-v1-abcd1234.md"
        sidecar.write_text("peer copy")

        _, outcomes = _download_and_apply(
            backend,
            base,
            {"notes.md": _info(_sha(b"expected-bytes"))},
            "peerA",
            "pp",
            1024,
            quiet=False,
        )
        assert outcomes["failed"] == ["notes.md"]
        assert outcomes["written"] == []
        assert canonical.read_text() == "local notes"
        assert sidecar.read_text() == "peer copy"
        apply_spy.assert_not_called()
        assert "content check failed" in capsys.readouterr().out

    def test_mismatch_then_later_valid_file_applies(self, tmp_path, monkeypatch) -> None:
        from mind_meld.cli import _download_and_apply

        payloads = {"bad.md": b"wrong", "good.md": b"good-bytes"}

        def decrypt_payload(data, *a, **kw):
            return payloads[data.decode()]

        backend = MagicMock()
        backend.get = MagicMock(side_effect=[b"bad.md", b"good.md"])
        monkeypatch.setattr("mind_meld.cli.decrypt", decrypt_payload)

        base = tmp_path / "src"
        base.mkdir()
        to_download = {
            "bad.md": _info(_sha(b"expected")),
            "good.md": _info(_sha(b"good-bytes")),
        }
        _, outcomes = _download_and_apply(
            backend, base, to_download, "peerA", "pp", 1024, quiet=True
        )
        assert outcomes["failed"] == ["bad.md"]
        assert outcomes["written"] == ["good.md"]
        assert (base / "good.md").read_bytes() == b"good-bytes"
        assert not (base / "bad.md").exists()

    def test_quiet_mismatch_has_no_per_file_stdout(self, tmp_path, monkeypatch, capsys) -> None:
        from mind_meld.cli import _download_and_apply

        backend = MagicMock()
        backend.get = MagicMock(return_value=b"opaque")
        monkeypatch.setattr("mind_meld.cli.decrypt", lambda *a, **kw: b"wrong")
        base = tmp_path / "src"
        base.mkdir()
        _, outcomes = _download_and_apply(
            backend,
            base,
            {"a.md": _info(_sha(b"expected"))},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert outcomes["failed"] == ["a.md"]

    def test_mismatch_does_not_enter_conflict_replacement(self, tmp_path, monkeypatch) -> None:
        from mind_meld.cli import _download_and_apply

        backend = MagicMock()
        backend.get = MagicMock(return_value=b"opaque")
        monkeypatch.setattr("mind_meld.cli.decrypt", lambda *a, **kw: b"remote-wrong")
        apply_spy = MagicMock()
        monkeypatch.setattr("mind_meld.cli._apply_incoming_file", apply_spy)
        base = tmp_path / "src"
        base.mkdir()
        (base / "notes.md").write_text("local divergent")
        _, outcomes = _download_and_apply(
            backend,
            base,
            {"notes.md": _info(_sha(b"remote-right"))},
            "peerA",
            "pp",
            1024,
            quiet=True,
        )
        assert outcomes["failed"] == ["notes.md"]
        assert outcomes["conflicted"] == []
        apply_spy.assert_not_called()
        assert (base / "notes.md").read_text() == "local divergent"


class TestSnapshotPublicationHelpers:
    def test_omission_proof_does_not_open_recovered_base_or_traversal(self, tmp_path, monkeypatch):
        trusted = tmp_path / "src"
        trusted.mkdir()
        recovered = tmp_path / "peer-base"
        recovered.mkdir()
        secret = tmp_path / "secret"
        secret.write_text("do-not-open")
        probed: list[Path] = []
        real_lstat = Path.lstat

        def tracking(self):
            probed.append(self)
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", tracking)
        prior = {
            "sources": {
                "claude": {
                    "base_path": str(recovered),
                    "files": {"../secret": {"sha256": "a" * 64}},
                }
            }
        }
        _prove_omitted_paths_absent(
            {"sources": {"claude": {"files": {}}}},
            prior,
            [{"name": "claude", "path": str(trusted)}],
            max_file_size=1024,
        )
        assert secret not in probed
        assert recovered not in probed
        assert not any(p == secret or p == recovered for p in probed)

    def test_still_present_omitted_alias_refuses(self, tmp_path):
        root = tmp_path / "src"
        root.mkdir()
        (root / "a.md").write_text("same")
        os.link(root / "a.md", root / "alias.md")
        local = {"sources": {"gstack": {"files": {"a.md": {"sha256": "x"}}}}}
        prior = {
            "sources": {
                "gstack": {
                    "files": {
                        "a.md": {"sha256": "x"},
                        "alias.md": {"sha256": "x"},
                    }
                }
            }
        }
        with pytest.raises(SnapshotError, match="still present"):
            _prove_omitted_paths_absent(
                local,
                prior,
                [{"name": "gstack", "path": str(root)}],
                max_file_size=1024,
            )

    def test_proof_unreadable_root_refuses(self, tmp_path, monkeypatch):
        root = tmp_path / "src"
        root.mkdir()
        real_stat = Path.stat

        def boom(self, *a, **kw):
            if self == root:
                raise PermissionError("denied")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", boom)
        prior = {"sources": {"gstack": {"files": {"a.md": {"sha256": "a" * 64}}}}}
        with pytest.raises(SnapshotError, match="could not be read"):
            _prove_omitted_paths_absent(
                {"sources": {"gstack": {"files": {}}}},
                prior,
                [{"name": "gstack", "path": str(root)}],
                max_file_size=1024,
            )

    def test_upload_rejects_matching_digest_wrong_size(self, tmp_path):
        base = tmp_path / "src"
        base.mkdir()
        (base / "a.md").write_text("ok")
        digest = hashlib.sha256(b"ok").hexdigest()
        info = {"sha256": digest, "size": 99, "mtime": "2026-01-01T00:00:00+00:00"}
        backend = MagicMock()
        with pytest.raises(SnapshotError, match="changed while being read"):
            _upload_changed_blobs(backend, base, {"a.md": info}, "dev", "pw", 1024)
        backend.put.assert_not_called()

    def test_upload_accepts_z_mtime_alias(self, tmp_path, monkeypatch):
        base = tmp_path / "src"
        base.mkdir()
        path = base / "a.md"
        path.write_text("ok")
        rev = read_file_revision(path, max_file_size=1024, retain_bytes=True, base=base)
        advertised = rev.mtime_iso.replace("+00:00", "Z")
        info = {"sha256": rev.digest, "size": rev.size, "mtime": advertised}
        backend = MagicMock()
        monkeypatch.setattr("mind_meld.cli.encrypt", lambda *a, **kw: b"enc")
        transferred = _upload_changed_blobs(backend, base, {"a.md": info}, "dev", "pw", 1024)
        backend.put.assert_called_once()
        assert transferred == len(b"enc")

    def test_upload_refuses_grok_hardlink_replacement(self, tmp_path):
        base = tmp_path / ".grok"
        skills = base / "skills"
        skills.mkdir(parents=True)
        path = skills / "SKILL.md"
        path.write_text("ok")
        rev = read_file_revision(
            path,
            max_file_size=1024,
            retain_bytes=True,
            source_type="grok",
            base=base,
        )
        os.link(path, skills / "alias.md")
        info = {"sha256": rev.digest, "size": rev.size, "mtime": rev.mtime_iso}
        backend = MagicMock()
        with pytest.raises(SnapshotError, match="no longer a confined regular file"):
            _upload_changed_blobs(
                backend,
                base,
                {"skills/SKILL.md": info},
                "dev",
                "pw",
                1024,
                source_type="grok",
            )
        backend.put.assert_not_called()

    def test_prior_grok_missing_root_refuses(self, tmp_path):
        missing = tmp_path / "gone-grok"
        grok_cfg = {"name": "grok", "path": str(missing), "type": "grok"}
        resolution = SourceResolution(selected=[grok_cfg], available=[])
        remote = {"sources": {"grok": {"files": {"skills/a.md": {"sha256": "a" * 64}}}}}
        with pytest.raises(SnapshotError, match="source grok"):
            _include_prior_grok_if_needed(
                {"sync": {}},
                resolution,
                remote,
                [],
                {"sources": {}},
                "dev",
                "A",
                1024,
                None,
            )

    def test_prior_grok_empty_customization_dirs_scans(self, tmp_path):
        root = tmp_path / ".grok"
        root.mkdir()
        grok_cfg = {"name": "grok", "path": str(root), "type": "grok"}
        resolution = SourceResolution(selected=[grok_cfg], available=[])
        remote = {"sources": {"grok": {"files": {"skills/a.md": {"sha256": "a" * 64}}}}}
        sources, local = _include_prior_grok_if_needed(
            {"sync": {}},
            resolution,
            remote,
            [],
            {"sources": {}},
            "dev",
            "A",
            1024,
            None,
        )
        assert any(src["name"] == "grok" for src in sources)
        assert "grok" in local["sources"]
        assert local["sources"]["grok"]["files"] == {}

    def test_explicit_config_does_not_reinject_retired_grok(self, tmp_path):
        root = tmp_path / ".grok"
        root.mkdir()
        (root / "skills").mkdir()
        (root / "skills" / "a.md").write_text("x")
        resolution = SourceResolution(selected=[], available=[], explicit=True)
        remote = {"sources": {"grok": {"files": {"skills/a.md": {"sha256": "a" * 64}}}}}
        sources, local = _include_prior_grok_if_needed(
            {"sync": {"sources": []}},
            resolution,
            remote,
            [],
            {"sources": {}},
            "dev",
            "A",
            1024,
            None,
        )
        assert sources == []
        assert "grok" not in local["sources"]

    def test_legacy_prior_default_source_stays_selected(self):
        resolution = SourceResolution(
            selected=[{"name": "claude", "path": "/tmp/claude", "type": "claude"}],
            available=[{"name": "claude", "path": "/tmp/claude", "type": "claude"}],
            explicit=False,
        )
        remote = {"sources": {"claude": {"files": {}}, "gstack": {"files": {"a.md": {}}}}}
        out = _retain_prior_default_sources(resolution, remote, {"sync": {}})
        assert any(src["name"] == "gstack" for src in out.selected)
        explicit = SourceResolution(
            selected=[{"name": "claude", "path": "/tmp/claude", "type": "claude"}],
            available=[{"name": "claude", "path": "/tmp/claude", "type": "claude"}],
            explicit=True,
        )
        kept = _retain_prior_default_sources(explicit, remote, {"sync": {}})
        assert not any(src["name"] == "gstack" for src in kept.selected)

    def test_omission_proof_skips_walker_excluded_grok_path(self, tmp_path):
        root = tmp_path / ".grok"
        generated = root / "skills" / "gstack-foo"
        generated.mkdir(parents=True)
        (generated / "SKILL.md").write_text("generated")
        prior = {
            "sources": {
                "grok": {
                    "files": {"skills/gstack-foo/SKILL.md": {"sha256": "a" * 64}},
                }
            }
        }
        _prove_omitted_paths_absent(
            {"sources": {"grok": {"files": {}}}},
            prior,
            [{"name": "grok", "path": str(root), "type": "grok"}],
            max_file_size=1024,
        )

    def test_omission_proof_eloop_refuses(self, tmp_path, monkeypatch):
        root = tmp_path / "src"
        root.mkdir()
        (root / "a.md").write_text("x")
        real_lstat = Path.lstat

        def boom(self):
            if self == root / "a.md":
                err = OSError("loop")
                err.errno = errno.ELOOP
                raise err
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", boom)
        prior = {"sources": {"gstack": {"files": {"a.md": {"sha256": "a" * 64}}}}}
        with pytest.raises(SnapshotError, match="could not be read"):
            _prove_omitted_paths_absent(
                {"sources": {"gstack": {"files": {}}}},
                prior,
                [{"name": "gstack", "path": str(root)}],
                max_file_size=1024,
            )
