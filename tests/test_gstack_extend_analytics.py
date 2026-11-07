"""Tests for Track 113A: syncing gstack-extend's per-host run history."""

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import config as config_module
from mind_meld import core, manifest
from mind_meld.cli import app
from mind_meld.config import get_default_source, load_config, save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "gstack-extend-analytics-test-passphrase"
MEMORY_KB = 1024
runner = CliRunner()

INCLUDED_REL_PATHS = (
    "analytics/stage-runs/mac-a.jsonl",
    "analytics/stage-runs/linux-a.jsonl",
    "analytics/stage-runs/mac-a.legacy.jsonl",
    "projects/demo/notes.md",
)

EXCLUDED_SHAPE_PATHS = (
    "analytics/stage-runs.jsonl",
    "analytics/skill-usage.jsonl",
    "analytics/workflow-metrics/run-1.json",
    "analytics/other.jsonl",
    "analytics/stage-runs/nested/linux-b.jsonl",
    "analytics/stage-runs/notes.txt",
    "config",
    "update-snoozed",
    "just-upgraded-from",
    "projects.json",
    "telemetry/events.jsonl",
)


def _build_gstack_extend_fixture(tmp_path: Path) -> Path:
    root = tmp_path / ".gstack-extend"
    for rel in INCLUDED_REL_PATHS:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'{{"path": "{rel}"}}\n', encoding="utf-8")

    for rel in EXCLUDED_SHAPE_PATHS:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'{{"excluded": "{rel}"}}\n', encoding="utf-8")

    outside = tmp_path / "outside-target.jsonl"
    outside.write_text('{"outside": true}\n', encoding="utf-8")
    (root / "analytics/stage-runs/linked.jsonl").symlink_to(outside)
    return root


class TestGstackExtendMigration113A:
    def _old_default_entry(self, root: Path) -> dict:
        return {
            "name": "gstack-extend",
            "path": str(root),
            "type": "generic",
            "include_dirs": ["projects"],
            "include_files": [],
            "exclude_patterns": [],
        }

    def _write_explicit_config(self, tmp_path: Path, source_entry: dict) -> Path:
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"max_file_size": 52_428_800, "sources": [source_entry]},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path

    def _redirect_lock(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        lp = tmp_path / "test.lock"
        monkeypatch.setattr("mind_meld.config.LOCK_PATH", lp)
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", lp)

    def test_diff_and_missing_excludes_detect_old_default_entry(self, tmp_path: Path):
        old_default_entry = self._old_default_entry(tmp_path / ".gstack-extend")
        assert core._compute_recommended_includes_diff([old_default_entry]) == [
            ("gstack-extend", ["projects/**", "analytics/stage-runs/*.jsonl"], None)
        ]
        assert core._config_missing_recommended_excludes(
            {"sync": {"sources": [old_default_entry]}}
        ) == ["gstack-extend"]

    def test_migrate_config_dry_run_and_apply_on_old_default_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        old_default_entry = self._old_default_entry(tmp_path / ".gstack-extend")
        config_path = self._write_explicit_config(tmp_path, old_default_entry)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        before = config_path.read_bytes()
        dry = runner.invoke(app, ["migrate-config", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        dry_flat = " ".join(dry.output.split())
        assert "+ analytics/stage-runs/*.jsonl" in dry_flat
        assert "+ include_dirs: analytics/stage-runs" in dry_flat
        assert "For gstack-extend: each machine's run history" in dry_flat
        assert "per-run output (review-prep trees, loop state, gate diffs)" not in dry_flat
        assert config_path.read_bytes() == before

        applied = runner.invoke(app, ["migrate-config", "--yes"])
        assert applied.exit_code == 0, applied.output
        default_entry = get_default_source("gstack-extend")
        assert default_entry is not None
        loaded = load_config(config_path)
        migrated = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack-extend")
        assert migrated["include_dirs"] == ["projects", "analytics/stage-runs"]
        assert migrated["include_patterns"] == default_entry["include_patterns"]
        assert migrated["include_files"] == []
        assert migrated["exclude_patterns"] == []

        second = runner.invoke(app, ["migrate-config", "--yes"])
        assert second.exit_code == 0, second.output
        assert "already up to date" in second.output

    def test_edited_include_dirs_gets_allowlist_but_keeps_custom_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        entry = self._old_default_entry(tmp_path / ".gstack-extend")
        entry["include_dirs"] = ["projects", "mine"]
        config_path = self._write_explicit_config(tmp_path, entry)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        dry = runner.invoke(app, ["migrate-config", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "+ include_dirs:" not in dry.output
        dry_flat = " ".join(dry.output.split())
        assert "starts syncing" not in dry_flat
        assert "Other configured paths outside this allowlist stop syncing" in dry_flat
        assert "Publishing still requires matching include_dirs or include_files" in dry_flat

        applied = runner.invoke(app, ["migrate-config", "--yes"])
        assert applied.exit_code == 0, applied.output
        default_entry = get_default_source("gstack-extend")
        assert default_entry is not None
        loaded = load_config(config_path)
        migrated = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack-extend")
        assert migrated["include_dirs"] == ["projects", "mine"]
        assert migrated["include_patterns"] == default_entry["include_patterns"]

    def test_present_include_patterns_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        entry = self._old_default_entry(tmp_path / ".gstack-extend")
        entry["include_patterns"] = []
        config_path = self._write_explicit_config(tmp_path, entry)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output
        assert "already up to date" in result.output
        loaded = load_config(config_path)
        migrated = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack-extend")
        assert migrated["include_dirs"] == ["projects"]
        assert migrated["include_patterns"] == []


class TestGstackExtendAnalyticsAllowlist113A:
    def test_walk_generic_source_selects_only_allowlisted_files(self, tmp_path: Path):
        root = _build_gstack_extend_fixture(tmp_path)
        default_entry = get_default_source("gstack-extend")
        assert default_entry is not None
        walked = manifest.walk_generic_source({**default_entry, "path": str(root)})
        assert set(walked) == set(INCLUDED_REL_PATHS)

    @pytest.mark.parametrize("link_mode", ["stage-runs-link", "analytics-link"])
    def test_walk_never_follows_symlinked_analytics_or_stage_runs(
        self, tmp_path: Path, link_mode: str
    ):
        root = tmp_path / ".gstack-extend"
        (root / "projects/demo").mkdir(parents=True)
        (root / "projects/demo/notes.md").write_text("notes\n", encoding="utf-8")
        outside = tmp_path / "outside-analytics"

        if link_mode == "stage-runs-link":
            outside.mkdir(parents=True)
            (outside / "mac-a.jsonl").write_text('{"host": "mac-a"}\n', encoding="utf-8")
            (root / "analytics").mkdir(parents=True)
            (root / "analytics/stage-runs").symlink_to(outside)
        else:
            (outside / "stage-runs").mkdir(parents=True)
            (outside / "stage-runs/mac-a.jsonl").write_text('{"host": "mac-a"}\n', encoding="utf-8")
            (root / "analytics").symlink_to(outside)

        default_entry = get_default_source("gstack-extend")
        assert default_entry is not None
        walked = manifest.walk_generic_source({**default_entry, "path": str(root)})
        assert set(walked) == {"projects/demo/notes.md"}
        assert not any(k.startswith("analytics/") for k in walked)

    def test_consumer_filter_strips_excluded_analytics_shapes(self):
        default_entry = get_default_source("gstack-extend")
        assert default_entry is not None
        all_paths = [*INCLUDED_REL_PATHS, *EXCLUDED_SHAPE_PATHS]
        peer_manifest = {
            "sources": {
                "gstack-extend": {
                    "files": {
                        rel: {"sha256": "a" * 64, "size": 10, "mtime": 1700000000.0}
                        for rel in all_paths
                    }
                }
            },
            "tombstones": {
                f"gstack-extend:{rel}": {"deleted_at": "2026-11-07T00:00:00Z"} for rel in all_paths
            },
        }
        include_map = core._build_include_map({}, sources=[default_entry])
        filtered = core._filter_excluded_paths(peer_manifest, {}, None, include_map)
        assert set(filtered["sources"]["gstack-extend"]["files"]) == set(INCLUDED_REL_PATHS)
        assert set(filtered["tombstones"]) == {f"gstack-extend:{rel}" for rel in INCLUDED_REL_PATHS}

    def test_two_machines_converge_without_conflicts_or_historical_file_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        storage = tmp_path / "storage"
        root_a = tmp_path / "a" / ".gstack-extend"
        root_b = tmp_path / "b" / ".gstack-extend"
        root_a.mkdir(parents=True)
        root_b.mkdir(parents=True)

        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        def write_config(device_id: str, name: str, root: Path) -> Path:
            default_entry = config_module.get_default_source("gstack-extend")
            assert default_entry is not None
            src = {**default_entry, "path": str(root)}
            cfg = {
                "device": {"id": device_id, "name": name},
                "storage": {"path": str(storage)},
                "sync": {"max_file_size": 52_428_800, "sources": [src]},
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            }
            cfg_path = tmp_path / f"config_{device_id}.toml"
            save_config(cfg, cfg_path)
            return cfg_path

        config_a = write_config("dev-a", "A", root_a)
        config_b = write_config("dev-b", "B", root_b)

        clock = [1_700_000_000.0]

        def write_timed(path: Path, text: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            clock[0] += 10.0
            os.utime(path, (clock[0], clock[0]))

        def invoke_for(cfg_path: Path, args: list[str]) -> None:
            lp = tmp_path / f"{cfg_path.stem}.lock"
            monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
            monkeypatch.setattr("mind_meld.config.LOCK_PATH", lp)
            monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", lp)
            res = runner.invoke(app, args)
            assert res.exit_code == 0, res.output

        def assert_no_conflicts() -> None:
            for r in (root_a, root_b):
                conflicts = [
                    p.relative_to(r).as_posix()
                    for p in r.rglob("*")
                    if manifest.is_conflict_filename(p.name)
                ]
                assert conflicts == []

        mac_row_1 = '{"ts":"2026-11-07T01:00:00Z","host":"mac-a","seq":1}\n'
        linux_row_1 = '{"ts":"2026-11-07T01:05:00Z","host":"linux-a","seq":1}\n'
        hist_a = '{"historical":"mac-a-only"}\n'
        hist_b = '{"historical":"linux-a-only"}\n'
        metrics_a = '{"metric":1}\n'

        write_timed(root_a / "analytics/stage-runs/mac-a.jsonl", mac_row_1)
        write_timed(root_a / "analytics/stage-runs.jsonl", hist_a)
        write_timed(root_a / "analytics/workflow-metrics/run-1.json", metrics_a)

        write_timed(root_b / "analytics/stage-runs/linux-a.jsonl", linux_row_1)
        write_timed(root_b / "analytics/stage-runs.jsonl", hist_b)

        invoke_for(config_a, ["push"])
        invoke_for(config_b, ["push"])
        invoke_for(config_a, ["pull"])
        invoke_for(config_b, ["pull"])
        assert_no_conflicts()

        for r in (root_a, root_b):
            assert (r / "analytics/stage-runs/mac-a.jsonl").read_text(encoding="utf-8") == mac_row_1
            assert (r / "analytics/stage-runs/linux-a.jsonl").read_text(
                encoding="utf-8"
            ) == linux_row_1

        assert (root_a / "analytics/stage-runs.jsonl").read_text(encoding="utf-8") == hist_a
        assert (root_a / "analytics/workflow-metrics/run-1.json").read_text(
            encoding="utf-8"
        ) == metrics_a
        assert (root_b / "analytics/stage-runs.jsonl").read_text(encoding="utf-8") == hist_b
        assert not (root_b / "analytics/workflow-metrics/run-1.json").exists()

        # Both machines append to their own per-host files, push, pull, then A
        # pushes and B pulls once more (A republishes B's file).
        mac_row_2 = mac_row_1 + '{"ts":"2026-11-07T02:00:00Z","host":"mac-a","seq":2}\n'
        linux_row_2 = linux_row_1 + '{"ts":"2026-11-07T02:05:00Z","host":"linux-a","seq":2}\n'
        write_timed(root_a / "analytics/stage-runs/mac-a.jsonl", mac_row_2)
        write_timed(root_b / "analytics/stage-runs/linux-a.jsonl", linux_row_2)

        invoke_for(config_a, ["push"])
        invoke_for(config_b, ["push"])
        invoke_for(config_a, ["pull"])
        invoke_for(config_b, ["pull"])
        invoke_for(config_a, ["push"])
        invoke_for(config_b, ["pull"])
        assert_no_conflicts()

        for r in (root_a, root_b):
            assert (r / "analytics/stage-runs/mac-a.jsonl").read_text(encoding="utf-8") == mac_row_2
            assert (r / "analytics/stage-runs/linux-a.jsonl").read_text(
                encoding="utf-8"
            ) == linux_row_2
        assert (root_a / "analytics/stage-runs.jsonl").read_text(encoding="utf-8") == hist_a
        assert (root_b / "analytics/stage-runs.jsonl").read_text(encoding="utf-8") == hist_b
