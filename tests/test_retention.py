"""Focused plan/apply tests for the non-event ``mm gc`` retention reapers."""

from __future__ import annotations

import io
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mind_meld import statedb
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.gcflow import _do_gc
from mind_meld.retention import (
    _SNAPSHOT_FILENAME_RE,
    _gc_deleted_copies,
    _gc_old_event_files,
    _gc_orphan_retros_dir,
    _gc_token_cache,
    _sweep_local_tmp_files,
)
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
        captured = capsys.readouterr()
        assert "Temporary files: candidates=2 deleted=1 failed=1" in captured.out
        assert (
            "mm: warning: Retention cleanup was incomplete. Fix permissions or locks; "
            "use -v for paths and details. Run: mm gc -v"
        ) in captured.err


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
        captured = capsys.readouterr()
        assert "Events: candidates=1 deleted=0 failed=1" in captured.out
        assert (
            "mm: warning: Retention cleanup was incomplete. Fix permissions or locks; "
            "use -v for paths and details. Run: mm gc -v"
        ) in captured.err


class TestOrphanRetrosDir:
    def test_reaps_matching_files_and_rmdir_if_empty(self, tmp_path, capsys):
        retros = tmp_path / "retros"
        retros.mkdir()
        snap = retros / "2026-05-10-001.json"
        snap.write_text("{}")
        outcome = _gc_orphan_retros_dir(dry_run=False, verbose=False, retros_dir=retros)
        assert outcome.candidates == 1
        assert outcome.deleted == 1
        assert outcome.repairs == 1
        assert not snap.exists()
        assert not retros.exists()
        assert "Orphan retros:" in capsys.readouterr().out

    def test_leaves_non_matching_files_and_keeps_dir(self, tmp_path):
        retros = tmp_path / "retros"
        retros.mkdir()
        keep = retros / "notes.md"
        keep.write_text("mine")
        snap = retros / "2026-05-10-001.json"
        snap.write_text("{}")
        outcome = _gc_orphan_retros_dir(dry_run=False, verbose=False, retros_dir=retros)
        assert outcome.candidates == 1
        assert outcome.deleted == 1
        assert outcome.repairs == 0
        assert keep.exists()
        assert retros.exists()
        assert _SNAPSHOT_FILENAME_RE.match("2026-05-10-001.json")

    def test_dry_run_does_not_unlink(self, tmp_path, capsys):
        retros = tmp_path / "retros"
        retros.mkdir()
        snap = retros / "2026-08-15-001.json"
        snap.write_text("{}")
        outcome = _gc_orphan_retros_dir(dry_run=True, verbose=False, retros_dir=retros)
        assert outcome.candidates == 1
        assert outcome.deleted == 0
        assert snap.exists()
        assert retros.exists()
        assert "dry-run" in capsys.readouterr().out

    def test_missing_dir_is_zero(self, tmp_path, capsys):
        outcome = _gc_orphan_retros_dir(dry_run=False, verbose=False, retros_dir=tmp_path / "nope")
        assert outcome.candidates == 0
        assert "Orphan retros:" in capsys.readouterr().out


class TestTokenCacheGcFailureNotice:
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_hostile_exception_is_printable_and_skips(self, monkeypatch, dry_run):
        from mind_meld import retention
        from tests.test_safe_str import (
            _NESTED_OSC_ST,
            _assert_no_esc_or_c1,
            _assert_plain_notice_field,
        )

        def boom(*_args, **_kwargs):
            raise OSError(f"denied[red]{_NESTED_OSC_ST}")

        target = "plan_cache_entries" if dry_run else "reap_cache_entries"
        monkeypatch.setattr(retention.token_usage, target, boom)
        buf = io.StringIO()
        err_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err_buf)
        monkeypatch.setattr(
            retention,
            "console",
            Console(
                file=buf,
                force_terminal=True,
                color_system=None,
                highlight=False,
                width=200,
            ),
        )

        outcome = _gc_token_cache(dry_run=dry_run, verbose=False)

        assert outcome.skipped == 1
        err = err_buf.getvalue()
        err_lines = err.splitlines()
        if not err_lines or not err_lines[0].startswith(
            "mm: notice: token cache gc failed: OSError: "
        ):
            raise AssertionError(f"bad GC prefix in {ascii(err)}")
        if dry_run:
            assert len(err_lines) == 1, ascii(err)
        else:
            assert len(err_lines) == 2, ascii(err)
            assert (
                err_lines[1]
                == "mm: warning: Retention cleanup was incomplete. Fix permissions or locks; "
                "use -v for paths and details. Run: mm gc -v"
            )
        _assert_no_esc_or_c1(err)
        field = err_lines[0].split("OSError: ", 1)[1]
        _assert_plain_notice_field(field, starts_with="denied[red]")
        summary = buf.getvalue()
        assert "Token cache" in summary
        assert "skipped=1" in summary
        if dry_run:
            assert "dry-run" in summary


# ── Track 91A: recovery copies of files a pull deleted ────────────────

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _folder(age: timedelta, suffix: str = "ab12") -> Path:
    """A recovery folder named for the moment `age` before NOW, holding one copy."""
    name = f"{(NOW - age):%Y%m%d-%H%M%S}-{suffix}"
    folder = statedb.deleted_copies_dir() / name
    (folder / "notes").mkdir(parents=True)
    (folder / "notes" / "gone.md").write_bytes(b"recoverable\n")
    return folder


class TestDeletedCopiesReaper:
    def test_reaps_a_31_day_old_folder_by_its_name_and_keeps_a_29_day_old_one(self, capsys):
        old, fresh = _folder(timedelta(days=31)), _folder(timedelta(days=29), "cd34")

        outcome = _gc_deleted_copies(False, False, now=NOW)

        assert not old.exists() and fresh.exists()
        assert (outcome.candidates, outcome.deleted, outcome.failed) == (1, 1, 0)
        assert "Deleted-file copies: candidates=1 deleted=1 failed=0" in capsys.readouterr().out

    def test_the_boundary_is_thirty_days_exactly(self):
        edge = _folder(timedelta(days=30))
        just_over = _folder(timedelta(days=30, seconds=1), "cd34")
        _gc_deleted_copies(False, False, now=NOW, quiet=True)
        assert edge.exists() and not just_over.exists()

    def test_age_comes_from_the_folder_name_never_from_the_modified_time(self):
        # A restore from backup rewrites mtimes: a recent mtime must not save an old folder,
        # and an ancient mtime must not doom a recent one.
        old, fresh = _folder(timedelta(days=45)), _folder(timedelta(days=2), "cd34")
        now = NOW.timestamp()
        os.utime(old, (now, now))
        os.utime(fresh, (1_000_000_000, 1_000_000_000))
        _gc_deleted_copies(False, False, now=NOW, quiet=True)
        assert not old.exists() and fresh.exists()

    @pytest.mark.parametrize(
        "name",
        [
            "backup",
            "20200101-000000",  # no random suffix
            "20200101-000000-ABCD",  # not the lowercase hex mm writes
            "20200101-000000-abcde",
            "20200230-000000-abcd",  # not a date
            "2020010-000000-abcd",
            "x20200101-000000-abcd",
            "20200101-000000-abcd.bak",
        ],
    )
    def test_an_unparseable_name_is_never_reaped_however_old_its_files(self, name, capsys):
        folder = statedb.deleted_copies_dir() / name
        folder.mkdir(parents=True)
        (folder / "keep.md").write_bytes(b"x")
        os.utime(folder, (1_000_000_000, 1_000_000_000))
        outcome = _gc_deleted_copies(False, False, now=NOW)
        assert (folder / "keep.md").exists()
        assert (outcome.candidates, outcome.deleted, outcome.skipped) == (0, 0, 1)

    def test_a_dry_run_deletes_nothing_and_names_what_it_would(self, capsys):
        old = _folder(timedelta(days=40))
        outcome = _gc_deleted_copies(True, False, now=NOW)
        assert (old / "notes" / "gone.md").read_bytes() == b"recoverable\n"
        assert outcome.candidates == 1 and outcome.deleted == 0
        out = capsys.readouterr().out
        assert "would delete recovery folder" in out and "Deleted-file copies dry-run" in out

    def test_a_missing_recovery_root_is_just_empty(self, capsys):
        assert not statedb.deleted_copies_dir().exists()
        outcome = _gc_deleted_copies(False, False, now=NOW)
        assert (outcome.candidates, outcome.deleted, outcome.failed) == (0, 0, 0)
        assert not statedb.deleted_copies_dir().exists()  # the reaper creates nothing

    def test_symlinks_and_plain_files_are_not_recovery_folders(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "precious.md").write_bytes(b"x")
        root = statedb.deleted_copies_dir()
        root.mkdir(parents=True)
        name = f"{(NOW - timedelta(days=90)):%Y%m%d-%H%M%S}-ab12"
        (root / name).symlink_to(elsewhere)
        (root / f"{(NOW - timedelta(days=91)):%Y%m%d-%H%M%S}-cd34").write_bytes(b"a file")
        outcome = _gc_deleted_copies(False, False, now=NOW, quiet=True)
        assert (elsewhere / "precious.md").exists() and (root / name).is_symlink()
        assert outcome.candidates == 0

    def test_a_removal_failure_prints_one_notice_and_leaves_the_folder(self, monkeypatch, capsys):
        old = _folder(timedelta(days=40))
        other = _folder(timedelta(days=50), "cd34")

        def refuse(path, *args, **kwargs):
            raise OSError("Permission denied")

        monkeypatch.setattr("mind_meld.retention.shutil.rmtree", refuse)
        outcome = _gc_deleted_copies(False, False, now=NOW)

        assert (outcome.candidates, outcome.deleted, outcome.failed) == (2, 0, 2)
        assert (old / "notes" / "gone.md").exists() and (other / "notes" / "gone.md").exists()
        captured = capsys.readouterr()
        assert captured.out.count("could not delete") == 2  # one notice per folder, nothing raised
        assert (
            "mm: warning: Retention cleanup was incomplete. Fix permissions or locks; "
            "use -v for paths and details. Run: mm gc -v"
        ) in captured.err

    def test_a_listing_failure_prints_one_notice_and_changes_nothing(self, monkeypatch, capsys):
        old = _folder(timedelta(days=40))
        real_iterdir = Path.iterdir

        def refuse(self):
            if self == statedb.deleted_copies_dir():
                raise PermissionError("Permission denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", refuse)
        outcome = _gc_deleted_copies(False, False, now=NOW)
        assert outcome.failed == 1 and outcome.deleted == 0
        assert (old / "notes" / "gone.md").exists()
        assert capsys.readouterr().out.count("could not be listed") == 1

    def test_quiet_prints_nothing_even_when_it_fails(self, monkeypatch, capsys):
        _folder(timedelta(days=40))
        monkeypatch.setattr(
            "mind_meld.retention.shutil.rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
        )
        _gc_deleted_copies(False, False, now=NOW, quiet=True)
        assert capsys.readouterr().out == ""

    def test_the_reaper_refuses_to_touch_real_account_state_under_pytest(
        self, synthetic_account_home, monkeypatch
    ):
        from mind_meld import fsutil

        real = synthetic_account_home / ".config" / "mind-meld" / "deleted"
        stale = real / f"{(NOW - timedelta(days=90)):%Y%m%d-%H%M%S}-ab12"
        stale.mkdir(parents=True)
        monkeypatch.setattr(statedb, "DB_DIR", real.parent)
        with pytest.raises(fsutil.PytestRealStateWriteError):
            _gc_deleted_copies(False, False, now=NOW, quiet=True)
        assert stale.exists()


PASSPHRASE = "retention-copies-test"
MEMORY_KB = 1024
runner = CliRunner()


class TestMmGcAndRecoveryCopies:
    def _store(self, tmp_path, monkeypatch):
        storage_dir = tmp_path / "storage"
        notes = tmp_path / "notes"
        notes.mkdir()
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        config_path = tmp_path / "config.toml"
        save_config(
            {
                "device": {"id": "dev-a", "name": "A"},
                "storage": {"path": str(storage_dir)},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [
                        {
                            "name": "notes",
                            "path": str(notes),
                            "type": "generic",
                            "include_dirs": ["."],
                        }
                    ],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            config_path,
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        return storage_dir, notes

    def test_gc_collects_store_blobs_and_leaves_the_recovery_copy_intact(
        self, tmp_path, monkeypatch
    ):
        import hashlib

        storage_dir, notes = self._store(tmp_path, monkeypatch)
        (notes / "doc.md").write_bytes(b"first version\n")
        assert runner.invoke(app, ["push"]).exit_code == 0
        first = hashlib.sha256(b"first version\n").hexdigest()
        blob = storage_dir / "data" / "dev-a" / f"{first}.enc"
        assert blob.exists()
        # A recovery copy of that same version, as a pull that followed a deletion keeps it.
        copy_dir = statedb.deleted_copies_dir() / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-ab12"
        (copy_dir / "notes").mkdir(parents=True)
        (copy_dir / "notes" / "doc.md").write_bytes(b"first version\n")
        # The store stops referencing the blob: the file changes and is pushed again.
        (notes / "doc.md").write_bytes(b"second version\n")
        assert runner.invoke(app, ["push"]).exit_code == 0

        result = runner.invoke(app, ["gc", "-v"])

        assert result.exit_code == 0, result.output
        assert not blob.exists(), "setup: gc must have collected the store blob"
        # The copy depends on no store blob.
        assert (copy_dir / "notes" / "doc.md").read_bytes() == b"first version\n"
        assert "Deleted-file copies: candidates=0" in result.output
        assert result.output.strip().splitlines()[-1] == "Gc: 0 orphaned blobs deleted."

    def test_gc_reaps_an_old_folder_and_its_dry_run_only_previews_it(self, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch)
        old_name = f"{(datetime.now(timezone.utc) - timedelta(days=45)):%Y%m%d-%H%M%S}-ab12"
        old = statedb.deleted_copies_dir() / old_name
        (old / "notes").mkdir(parents=True)
        (old / "notes" / "doc.md").write_bytes(b"old")
        recent = _folder_now("cd34")

        preview = runner.invoke(app, ["gc", "--dry-run"])
        assert preview.exit_code == 0, preview.output
        assert "would delete recovery folder" in preview.output
        assert (old / "notes" / "doc.md").exists()

        applied = runner.invoke(app, ["gc"])
        assert applied.exit_code == 0, applied.output
        assert not old.exists() and (recent / "notes" / "doc.md").exists()
        assert "Deleted-file copies:" not in applied.output
        assert applied.stdout.strip() == (
            "Gc: blob collection skipped (own manifest missing); local retention complete."
        )

    def test_a_reaper_failure_does_not_fail_gc_or_remove_anything(self, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch)
        old_name = f"{(datetime.now(timezone.utc) - timedelta(days=45)):%Y%m%d-%H%M%S}-ab12"
        old = statedb.deleted_copies_dir() / old_name
        (old / "notes").mkdir(parents=True)
        (old / "notes" / "doc.md").write_bytes(b"old")
        monkeypatch.setattr(
            "mind_meld.retention.shutil.rmtree",
            lambda *a, **k: (_ for _ in ()).throw(OSError("Permission denied")),
        )
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output
        assert (old / "notes" / "doc.md").exists()
        assert "could not delete" in result.output
        assert (
            "mm: warning: Retention cleanup was incomplete. Fix permissions or locks; "
            "use -v for paths and details. Run: mm gc -v"
        ) in result.output
        assert "Gc:" not in result.output


def _folder_now(suffix: str) -> Path:
    name = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{suffix}"
    folder = statedb.deleted_copies_dir() / name
    (folder / "notes").mkdir(parents=True)
    (folder / "notes" / "doc.md").write_bytes(b"recent")
    return folder
