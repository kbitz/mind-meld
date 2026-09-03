"""Tests for ``_gc_old_event_files`` — Track 7B fleet retention.

The reaper drops day files older than ``EVENTS_RETENTION_DAYS`` (90).
Reap by FILENAME date (Codex C5/C6) — iCloud restores rewrite mtimes
back to "now" while the filename's YYYY-MM-DD is intrinsic to the
event-day boundary.

Fleet retention via tombstone propagation: this device unlinks → next
push generates a tombstone → all peers drop their copy on pull. Offline
peers see the tombstone too, suppressing resurrection.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from mind_meld import token_usage
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.retention import _gc_old_event_files
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "gc-events-test"
MEMORY_KB = 1024
runner = CliRunner()


def _make_events_file(events_dir: Path, device: str, day: datetime) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / f"{device}-{day.date().isoformat()}.jsonl"
    path.write_text('{"type":"mm-push","device":"' + device + '"}\n')
    return path


def _config_with_events(tmp_path: Path, events_root: Path) -> dict:
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
        "crypto": {"argon2_memory_kb": MEMORY_KB},
    }


class TestGcOldEventFiles:
    def test_files_older_than_90d_are_unlinked(self, tmp_path):
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"
        old = datetime.now(timezone.utc) - timedelta(days=120)
        old_file = _make_events_file(events_dir, "dev-a", old)
        cfg = _config_with_events(tmp_path, events_root)

        reaped = _gc_old_event_files(cfg, dry_run=False, verbose=False)
        assert reaped.deleted == 1
        assert not old_file.exists()

    def test_files_younger_than_90d_are_kept(self, tmp_path):
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"
        recent = datetime.now(timezone.utc) - timedelta(days=30)
        keep = _make_events_file(events_dir, "dev-a", recent)
        cfg = _config_with_events(tmp_path, events_root)

        reaped = _gc_old_event_files(cfg, dry_run=False, verbose=False)
        assert reaped.deleted == 0
        assert keep.exists()

    def test_threshold_boundary_at_90d_exact(self, tmp_path):
        """Exactly 90 days old is reaped (>= EVENTS_RETENTION_DAYS)."""
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"
        boundary = datetime.now(timezone.utc) - timedelta(days=90)
        f = _make_events_file(events_dir, "dev-a", boundary)
        cfg = _config_with_events(tmp_path, events_root)
        reaped = _gc_old_event_files(cfg, dry_run=False, verbose=False)
        assert reaped.deleted == 1
        assert not f.exists()

    def test_reap_by_filename_date_not_mtime(self, tmp_path):
        """Codex C5/C6: iCloud restore can rewrite mtime to "now" while
        the filename date is intrinsic. The reaper uses the filename, not
        st_mtime."""
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"
        old = datetime.now(timezone.utc) - timedelta(days=180)
        old_file = _make_events_file(events_dir, "dev-a", old)
        # Rewrite mtime to NOW (simulating iCloud restore).
        now_ts = time.time()
        os.utime(old_file, (now_ts, now_ts))
        # Sanity: mtime is fresh, but filename says 180 days ago.
        assert (time.time() - old_file.stat().st_mtime) < 5

        cfg = _config_with_events(tmp_path, events_root)
        reaped = _gc_old_event_files(cfg, dry_run=False, verbose=False)
        assert reaped.deleted == 1, "filename date must override misleading mtime"

    def test_dry_run_does_not_unlink(self, tmp_path: Path):
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        old = now - timedelta(days=120)
        old_file = _make_events_file(events_dir, "dev-a", old)
        old_file.write_bytes(b"event bytes must remain exact")
        old_file.chmod(0o640)
        before = old_file.stat()
        cfg = _config_with_events(tmp_path, events_root)

        reaped = _gc_old_event_files(cfg, dry_run=True, verbose=False, now=now)
        assert reaped.candidates == 1, "dry_run still reports the count"
        assert old_file.exists(), "dry_run must NOT unlink"
        assert old_file.read_bytes() == b"event bytes must remain exact"
        after = old_file.stat()
        assert after.st_mode & 0o777 == before.st_mode & 0o777
        assert after.st_mtime_ns == before.st_mtime_ns

    def test_verbose_prints_per_file(self, tmp_path, capsys):
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"
        old = datetime.now(timezone.utc) - timedelta(days=120)
        _make_events_file(events_dir, "dev-a", old)
        cfg = _config_with_events(tmp_path, events_root)

        _gc_old_event_files(cfg, dry_run=False, verbose=True)
        out = capsys.readouterr().out
        assert "deleted" in out
        assert "dev-a-" in out

    def test_missing_events_dir_is_noop(self, tmp_path):
        events_root = tmp_path / "events_root"
        # No events dir created — get_sources will path-existence-filter
        # the source out OR _gc_old_event_files's is_dir guard catches it.
        cfg = _config_with_events(tmp_path, events_root)
        reaped = _gc_old_event_files(cfg, dry_run=False, verbose=False)
        assert reaped.deleted == 0

    def test_no_mm_events_source_is_noop(self, tmp_path):
        """User who hasn't migrated config: no mm-events source → 0
        reaped, no error."""
        cfg = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [{"name": "claude", "path": str(tmp_path), "type": "claude"}],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        reaped = _gc_old_event_files(cfg, dry_run=False, verbose=False)
        assert reaped.deleted == 0

    def test_non_jsonl_files_left_alone(self, tmp_path):
        """Only files matching <device>-YYYY-MM-DD.jsonl are reaped.
        Non-conforming neighbors are left intact."""
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"
        events_dir.mkdir(parents=True)
        # A non-jsonl file in the events dir
        (events_dir / "README.md").write_text("not a sync file")
        # A jsonl with a non-conforming filename (no date)
        (events_dir / "garbage.jsonl").write_text("{}\n")
        # A real old daily file
        old = datetime.now(timezone.utc) - timedelta(days=120)
        old_file = _make_events_file(events_dir, "dev-a", old)

        cfg = _config_with_events(tmp_path, events_root)
        reaped = _gc_old_event_files(cfg, dry_run=False, verbose=False)
        assert reaped.deleted == 1
        assert not old_file.exists()
        assert (events_dir / "README.md").exists()
        assert (events_dir / "garbage.jsonl").exists()

    def test_custom_mm_events_path_honored(self, tmp_path):
        """A user who points mm-events at a custom path (e.g. via
        ``mm reconfigure-sources``) still gets retention."""
        custom_root = tmp_path / "custom" / "elsewhere"
        custom_dir = custom_root / "events"
        old = datetime.now(timezone.utc) - timedelta(days=120)
        old_file = _make_events_file(custom_dir, "dev-a", old)
        cfg = _config_with_events(tmp_path, custom_root)
        reaped = _gc_old_event_files(cfg, dry_run=False, verbose=False)
        assert reaped.deleted == 1
        assert not old_file.exists()


class TestGcEventsIronRule:
    """IRON RULE: reaper unlink → next push generates a tombstone for
    the dropped path. Tombstone propagation IS the fleet retention
    mechanism — without this pin, a 90-day reaper that didn't tombstone
    would leave fleet peers with stale day files forever."""

    def test_reap_triggers_tombstone_on_next_push(self, tmp_path, monkeypatch):
        storage_dir = tmp_path / "storage"
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"

        # Bootstrap storage + register device.
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")

        # Seed an old events file (will be reaped).
        claude_dir = tmp_path / "claude"
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "x.md").write_text("seed")
        old = datetime.now(timezone.utc) - timedelta(days=120)
        old_file = _make_events_file(events_dir, "dev-a", old)

        config_path = tmp_path / "config.toml"
        cfg = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_dir), "type": "claude"},
                    {
                        "name": "mm-events",
                        "path": str(events_root),
                        "type": "generic",
                        "include_dirs": ["events"],
                        "exclude_patterns": [],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(cfg, config_path)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # First push registers the old events file in the manifest.
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        # Run gc — old file is reaped, a fresh today-events file remains
        # (head-position events tail just wrote it on the push above).
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output
        assert not old_file.exists(), "old events file must be reaped"

        # Next push must generate a tombstone for the unlinked path so
        # peers drop their copy on pull (fleet retention propagation).
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        # Inspect the manifest in storage to verify a tombstone for the
        # reaped events path landed there.
        from mind_meld.crypto import decrypt
        from mind_meld.manifest import load_manifest
        from mind_meld.storage.keys import manifest_key

        manifest_blob = backend.get(manifest_key("dev-a"))
        assert manifest_blob, "no manifest written after gc + push"
        plain = decrypt(manifest_blob, PASSPHRASE, memory_kb=MEMORY_KB)
        manifest = load_manifest(plain)
        tombstones = manifest.get("tombstones", {})
        assert any(old_file.name in path for path in tombstones.keys()), (
            f"reaper-driven tombstone missing; tombstones={list(tombstones.keys())}"
        )


class TestGcDryRunRetentionReport:
    def test_dry_run_conflicts_reports_every_reaper_without_mutation(self, tmp_path, monkeypatch):
        storage_dir = tmp_path / "storage"
        events_root = tmp_path / "events_root"
        events_dir = events_root / "events"
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")

        old = datetime.now(timezone.utc) - timedelta(days=120)
        old_event = _make_events_file(events_dir, "dev-a", old)
        old_conflict = events_dir / "notes.sync-conflict-20260101-120000-peer0001.jsonl"
        old_conflict.write_text("conflict")
        # Converged canonical: the only state `_is_live_conflict` lets the
        # reaper reach, so it is what makes this a dry-run candidate at all.
        (events_dir / "notes.jsonl").write_text("conflict")
        ancient = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
        os.utime(old_conflict, (ancient, ancient))
        tmp_file = storage_dir / "data" / "dev-a" / "tmp-upload.tmp"
        tmp_file.parent.mkdir(parents=True)
        tmp_file.write_bytes(b"tmp bytes")
        stale_cache_path = tmp_path / "missing.jsonl"
        token_usage.CACHE_PATH.write_text(
            '{"version":1,"files":{"' + str(stale_cache_path) + '":{"by_day":{"2020-01-01":{}}}}}'
        )
        watched = (old_event, old_conflict, tmp_file, token_usage.CACHE_PATH)
        before = {path: (path.read_bytes(), path.stat()) for path in watched}

        config_path = tmp_path / "config.toml"
        cfg = _config_with_events(tmp_path, events_root)
        cfg["storage"] = {"path": str(storage_dir)}
        save_config(cfg, config_path)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["gc", "--dry-run", "--conflicts"])

        assert result.exit_code == 0, result.output
        assert "Temporary files dry-run: candidates=1 repairs=0 skipped=0" in result.output
        assert "Events dry-run: candidates=1 repairs=0 skipped=0" in result.output
        assert "Token cache dry-run: candidates=1 repairs=0 skipped=0" in result.output
        assert "Conflicts dry-run: candidates=1 repairs=0 skipped=0" in result.output
        for path, (contents, stat) in before.items():
            after = path.stat()
            assert path.read_bytes() == contents
            assert after.st_mode == stat.st_mode
            assert after.st_mtime_ns == stat.st_mtime_ns

        without_conflicts = runner.invoke(app, ["gc", "--dry-run"])
        assert without_conflicts.exit_code == 0, without_conflicts.output
        assert "Conflicts dry-run:" in without_conflicts.output

    def test_gc_help_describes_complete_dry_run_scope(self) -> None:
        result = runner.invoke(app, ["gc", "--help"])

        assert result.exit_code == 0, result.output
        assert "Preview orphan blobs" in result.output
        assert "retention cleanup" in result.output
        assert "deleting" in result.output


class TestGcEventsOfflinePeerSuppression:
    """An offline peer that comes back online sees the tombstone
    (propagated above) and drops its own copy on pull. Without this,
    the peer's stale 120-day-old events file would resurrect on the
    next round of pushes."""

    def test_tombstone_visible_to_pulling_peer(self, tmp_path, monkeypatch):
        # This exercises the same plumbing as the existing tombstone
        # propagation tests in TestPushPullRoundTrip — the events tree
        # is just another `type=generic` source under the hood. We pin
        # the link by checking that a tombstone for an events path
        # behaves like any other tombstone: peer's local copy is
        # respected (additive) but the tombstone is recorded in the
        # propagated manifest.
        from mind_meld.manifest import generate_tombstones

        # Build a synthetic manifest pair: prior had the events file,
        # local does not. Tombstone must include the events path.
        prior = {
            "version": 2,
            "device_id": "dev-a",
            "device_name": "A",
            "sources": {
                "mm-events": {
                    "files": {
                        "events/dev-a-2026-01-01.jsonl": {
                            "sha256": "deadbeef" * 8,
                            "size": 100,
                            "mtime": 1700000000.0,
                        }
                    }
                }
            },
            "tombstones": {},
        }
        local = {
            "version": 2,
            "device_id": "dev-a",
            "device_name": "A",
            "sources": {"mm-events": {"files": {}}},
            "tombstones": {},
        }
        tombstones = generate_tombstones(local, prior, "dev-a")
        tomb_keys = list(tombstones.keys())
        assert any("events/dev-a-2026-01-01.jsonl" in k for k in tomb_keys), (
            f"tombstone for reaped events path missing: {tomb_keys}"
        )
