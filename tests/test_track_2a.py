"""Track 2A unit tests — extracted helpers from _pull_core and _apply_incoming_file.

Pins the behavior of each extracted helper directly so regressions during
this or future refactors surface at the unit boundary, not only via the
CLI-driven integration tests.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from mind_meld.cli import (
    _CorruptPeer,
    _FsyncWarning,
    _PerSourceResult,
    _PredictedConflict,
    _UnknownSourceWarning,
    _apply_conflict,
    _apply_merge,
    _apply_write,
    _bootstrap_or_verify_crypto,
    _empty_outcomes,
    _fsync_touched_parents,
    _load_prior_device_metadata,
    _preflight_conflicts,
    _prefetch_manifests,
    _prompt_passphrase,
    _prompt_sources,
    _pull_one_source,
    _save_and_register,
    _select_devices,
)
from mind_meld.config import DEFAULT_SOURCES, get_default_source
from mind_meld.errors import StorageError


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


# ── _apply_conflict ──────────────────────────────────────────────────


class TestApplyConflict:
    def test_happy_path_renames_and_writes(self, tmp_path: Path) -> None:
        local = tmp_path / "doc.md"
        local.write_bytes(b"local content")
        outcome = _apply_conflict(
            local, "doc.md", b"remote content", "devAAAA1234"
        )
        assert outcome == "conflicted"
        assert local.read_bytes() == b"remote content"
        # Exactly one conflict sibling, holding local's original bytes.
        siblings = [
            p for p in tmp_path.iterdir() if p.name.startswith("doc.sync-conflict-")
        ]
        assert len(siblings) == 1
        assert siblings[0].read_bytes() == b"local content"

    def test_empty_device_id_returns_failed(self, tmp_path: Path) -> None:
        local = tmp_path / "doc.md"
        local.write_bytes(b"local")
        outcome = _apply_conflict(local, "doc.md", b"remote", "")
        assert outcome == "failed"
        # Local preserved at canonical (not renamed away).
        assert local.read_bytes() == b"local"

    def test_write_failure_rolls_back(self, tmp_path: Path, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        local = tmp_path / "doc.md"
        local.write_bytes(b"local")

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", boom)
        outcome = _apply_conflict(local, "doc.md", b"remote", "devAAAA1234")
        assert outcome == "failed"
        # Rollback: canonical exists (rolled back from conflict_path).
        assert local.exists()
        assert local.read_bytes() == b"local"


# ── _empty_outcomes ──────────────────────────────────────────────────


class TestEmptyOutcomes:
    def test_has_all_six_keys(self) -> None:
        outcomes = _empty_outcomes()
        assert set(outcomes.keys()) == {
            "written", "merged", "skipped", "conflicted", "unchanged", "failed",
        }
        assert all(outcomes[k] == [] for k in outcomes)


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
            cli_module, "_list_devices_warn",
            lambda b: [
                {"device_id": "self1", "device_name": "me"},
                {"device_id": "peerA", "device_name": "A"},
                {"device_id": "peerB", "device_name": "B"},
            ],
        )
        all_devs, targets = _select_devices(
            backend=None, my_device_id="self1", from_device="peerA"
        )
        assert len(all_devs) == 3
        assert [d["device_id"] for d in targets] == ["peerA"]

    def test_from_device_unmatched_returns_empty_targets(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module

        monkeypatch.setattr(
            cli_module, "_list_devices_warn",
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
            cli_module, "_list_devices_warn",
            lambda b: [
                {"device_id": "self1", "device_name": "me"},
                {"device_id": "peerA", "device_name": "A"},
                {"device_id": "peerB", "device_name": "B"},
            ],
        )
        all_devs, targets = _select_devices(
            backend=None, my_device_id="self1", from_device=None
        )
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
            pull_targets, manifest_cache, local_sources,
            source_filter=None, all_tombstones={},
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
            pull_targets, manifest_cache, local_sources,
            source_filter=None, all_tombstones={},
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
            pull_targets, manifest_cache, local_sources,
            source_filter=None, all_tombstones={},
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
            pull_targets, manifest_cache, local_sources,
            source_filter=None, all_tombstones={},
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
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            }

        monkeypatch.setattr(cli_module, "_download_and_apply", fake_dl)
        result = _pull_one_source(
            backend=None,
            src_name="my-claude",   # user renamed
            src_type="claude",      # but type is still claude
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
            return 0, {k: [] for k in [
                "written", "merged", "skipped", "conflicted", "unchanged", "failed",
            ]}

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
                "skipped": [],
                "conflicted": [],
                "unchanged": [],
                "failed": [],
            }

        monkeypatch.setattr(cli_module, "_download_and_apply", fake_dl)
        result = _pull_one_source(
            backend=None,
            src_name="claude",      # name-only
            src_type="generic",     # but NOT a claude-typed source
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
            return 0, {k: [] for k in [
                "written", "merged", "skipped", "conflicted", "unchanged", "failed",
            ]}

        monkeypatch.setattr(cli_module, "_download_and_apply", fake_dl)
        # is_tombstoned uses f"{source}:{rel_path}" as the flat key.
        tombstones = {"claude:old.md": {"deleted_at": "2026-01-01T00:00:00Z"}}
        _pull_one_source(
            backend=None,
            src_name="claude",
            src_type="claude",
            src_data={"files": {
                "old.md": _info("abc"),
                "keep.md": _info("def"),
            }},
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
        monkeypatch.setattr(
            cli_module,
            "collect_tombstones",
            lambda *a, **kw: {},
        )

        # Run in quiet mode to assert stderr routing survives.
        result = _pull_core(
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
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", cfg)
        assert _load_prior_device_metadata() == (None, None)

    def test_readable_config_returns_id_and_name(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[device]\nid = "abc123"\nname = "OldMac"\n'
            '[storage]\npath = "/tmp/x"\n'
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", cfg)
        assert _load_prior_device_metadata() == ("abc123", "OldMac")

    def test_malformed_config_returns_none_tuple(self, tmp_path: Path, monkeypatch) -> None:
        """Broken config doesn't crash init; best-effort returns Nones so
        the orphan-case warning just loses the descriptive name."""
        cfg = tmp_path / "config.toml"
        cfg.write_text("this is not: valid [toml at all\n")
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", cfg)
        assert _load_prior_device_metadata() == (None, None)


class TestPromptPassphrase:
    """_prompt_passphrase — double-prompt on first-device, single otherwise."""

    def test_first_device_match(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module
        responses = iter(["pw123", "pw123"])
        monkeypatch.setattr(cli_module.typer, "prompt", lambda *a, **kw: next(responses))
        assert _prompt_passphrase(is_first_device=True) == "pw123"

    def test_first_device_mismatch_exits(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module
        import typer as _typer
        responses = iter(["pw123", "pw456"])
        monkeypatch.setattr(cli_module.typer, "prompt", lambda *a, **kw: next(responses))
        with pytest.raises(_typer.Exit):
            _prompt_passphrase(is_first_device=True)

    def test_first_device_empty_exits(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module
        import typer as _typer
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
        from mind_meld import cli as cli_module
        import typer as _typer
        monkeypatch.setattr(cli_module.typer, "prompt", lambda *a, **kw: "")
        with pytest.raises(_typer.Exit):
            _prompt_passphrase(is_first_device=False)


class TestPromptSources:
    """_prompt_sources — per-source Y/n prompt; returns enabled entries."""

    def test_all_declined_returns_empty(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module
        monkeypatch.setattr(cli_module.typer, "confirm", lambda *a, **kw: False)
        assert _prompt_sources() == []

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

    def test_claude_only(self, monkeypatch) -> None:
        from mind_meld import cli as cli_module
        responses = iter([True, False])  # Y claude, n gstack
        monkeypatch.setattr(cli_module.typer, "confirm", lambda *a, **kw: next(responses))
        result = _prompt_sources()
        assert [s["name"] for s in result] == ["claude"]

    def test_gstack_only_preserves_include_fields(self, monkeypatch) -> None:
        """The gstack default carries include_dirs / include_files — they
        must survive the indirection through get_default_source."""
        from mind_meld import cli as cli_module
        responses = iter([False, True])  # n claude, Y gstack
        monkeypatch.setattr(cli_module.typer, "confirm", lambda *a, **kw: next(responses))
        result = _prompt_sources()
        assert [s["name"] for s in result] == ["gstack"]
        gstack = result[0]
        assert "projects" in gstack["include_dirs"]
        assert "config.yaml" in gstack["include_files"]


class TestSaveAndRegister:
    """_save_and_register — config write → device register → keyring store."""

    def test_ordering(self, tmp_path: Path, monkeypatch) -> None:
        """Order matters: if config write fails we must NOT have registered
        the device or stored the passphrase. Pin the sequence."""
        from mind_meld import cli as cli_module

        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", cfg)

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
        _save_and_register(config, backend=None, device_id="d1",
                           device_name="Mac", passphrase="pw")
        assert call_order == ["save", "register", "keyring"]

    def test_no_keyring_still_succeeds(self, tmp_path: Path, monkeypatch) -> None:
        """Keyring unavailable (lambda _pw: False) → function completes
        without raising; caller sees a yellow warning on stdout."""
        from mind_meld import cli as cli_module

        cfg = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        monkeypatch.setattr("mind_meld.cli.CONFIG_PATH", cfg)

        monkeypatch.setattr(cli_module, "register_device", lambda *a, **kw: None)
        monkeypatch.setattr(cli_module, "store_passphrase_in_keyring", lambda _pw: False)

        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path)},
        }
        # Must not raise.
        _save_and_register(config, backend=None, device_id="d1",
                           device_name="Mac", passphrase="pw")


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
