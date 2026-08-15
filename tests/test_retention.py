"""Focused plan/apply tests for the non-event ``mm gc`` retention reapers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from mind_meld.cli import _do_gc
from mind_meld.retention import _gc_old_event_files, _sweep_local_tmp_files
from mind_meld.storage.local import LocalBackend


def _event_config(tmp_path: Path, events_root: Path) -> dict:
    return {
        "device": {"id": "dev-a", "name": "A"},
        "storage": {"path": str(tmp_path / "storage")},
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {
                    "name": "mm-events",
                    "path": str(events_root),
                    "type": "generic",
                    "include_dirs": ["events"],
                    "exclude_patterns": [],
                }
            ],
        },
    }


class TestTmpRetention:
    def test_push_auto_gc_can_hide_the_retention_summary(self, tmp_path: Path, capsys) -> None:
        storage_root = tmp_path / "storage"
        tmp_file = storage_root / "data" / "dev-a" / "tmp-upload.tmp"
        tmp_file.parent.mkdir(parents=True)
        tmp_file.write_bytes(b"tmp")
        config = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(storage_root)},
            "sync": {"max_file_size": 52_428_800},
        }

        _do_gc(
            config,
            "unused-without-device-manifests",
            1024,
            dry_run=False,
            verbose=False,
            emit_retention_summary=False,
        )

        assert not tmp_file.exists()
        assert "Temporary files:" not in capsys.readouterr().out

    def test_dry_run_selects_only_this_devices_tmp_files_without_mutating(
        self, tmp_path: Path, capsys
    ) -> None:
        backend = LocalBackend(tmp_path / "storage")
        data_tmp = backend.root / "data" / "dev-a" / "tmp-upload.tmp"
        manifest_tmp = backend.root / "manifests" / "dev-a" / "nested" / "tmp-manifest.tmp"
        peer_tmp = backend.root / "data" / "dev-b" / "tmp-peer.tmp"
        shared_tmp = backend.root / "devices" / "tmp-shared.tmp"
        for path in (data_tmp, manifest_tmp, peer_tmp, shared_tmp):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"retain exact bytes")
            path.chmod(0o640)
        before = {path: (path.read_bytes(), path.stat()) for path in (data_tmp, manifest_tmp)}

        outcome = _sweep_local_tmp_files(backend, "dev-a", dry_run=True, verbose=False)

        assert outcome.candidates == 2
        assert outcome.deleted == 0
        assert outcome.failed == 0
        for path, (contents, stat) in before.items():
            after = path.stat()
            assert path.read_bytes() == contents
            assert after.st_mode & 0o777 == stat.st_mode & 0o777
            assert after.st_mtime_ns == stat.st_mtime_ns
        assert peer_tmp.exists()
        assert shared_tmp.exists()
        assert (
            "Temporary files dry-run: candidates=2 repairs=0 skipped=0" in capsys.readouterr().out
        )

    def test_apply_counts_partial_unlink_failure(self, tmp_path: Path, monkeypatch, capsys) -> None:
        backend = LocalBackend(tmp_path / "storage")
        failed_path = backend.root / "data" / "dev-a" / "tmp-failed.tmp"
        deleted_path = backend.root / "manifests" / "dev-a" / "tmp-deleted.tmp"
        for path in (failed_path, deleted_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        original_unlink = Path.unlink

        def fail_one(self: Path, missing_ok: bool = False) -> None:
            if self == failed_path:
                raise OSError("permission denied")
            original_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_one)

        outcome = _sweep_local_tmp_files(backend, "dev-a", dry_run=False, verbose=False)

        assert outcome.candidates == 2
        assert outcome.deleted == 1
        assert outcome.failed == 1
        assert failed_path.exists()
        assert not deleted_path.exists()
        output = capsys.readouterr().out
        assert "Temporary files: candidates=2 deleted=1 failed=1" in output
        assert "Fix permissions or locks" in output


class TestEventsRetention:
    def test_apply_reports_unlink_failure_and_preserves_event(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        events_root = tmp_path / "events"
        old_day = (datetime(2026, 6, 1, tzinfo=timezone.utc) - timedelta(days=120)).date()
        event = events_root / "events" / f"dev-a-{old_day.isoformat()}.jsonl"
        event.parent.mkdir(parents=True)
        event.write_text("{}\n")
        original_unlink = Path.unlink

        def fail_event(self: Path, missing_ok: bool = False) -> None:
            if self == event:
                raise OSError("read-only filesystem")
            original_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_event)

        outcome = _gc_old_event_files(
            _event_config(tmp_path, events_root),
            dry_run=False,
            verbose=False,
            now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

        assert outcome.candidates == 1
        assert outcome.deleted == 0
        assert outcome.failed == 1
        assert event.exists()
        output = capsys.readouterr().out
        assert "Events: candidates=1 deleted=0 failed=1" in output
        assert "use `-v` for paths and details" in output
