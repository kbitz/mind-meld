"""Tests for Track 157A: codex-memory source, wire codec, confinement, retirement, and CLI."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import (
    cli,
    config,
    core,
    crypto,
    devices,
    devicesflow,
    events,
    events_tail,
    fsutil,
    initflow,
    manifest,
    pullflow,
    resolveflow,
    sidecar,
    skill_link,
    sourceflow,
    statedb,
    statusflow,
)
from mind_meld.errors import ConfigError, ManifestError, SnapshotError, StorageError
from mind_meld.storage import keys as storage_keys
from mind_meld.storage import local as local_storage

PASSPHRASE = "codex-memory-test-passphrase"
MEMORY_KB = 8192
DEV_A = "aaaaaaaa"
DEV_B = "bbbbbbbb"
DEV_C = "cccccccc"

runner = CliRunner()


def _snapshot_tree(root: Path) -> dict[str, tuple[bytes, int]]:
    """Return ``{rel_posix: (bytes, mtime_ns)}`` for every regular file under ``root``."""
    out: dict[str, tuple[bytes, int]] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        if stat.S_ISREG(st.st_mode):
            out[p.relative_to(root).as_posix()] = (p.read_bytes(), st.st_mtime_ns)
    return out


def _decrypt_raw_manifest(backend: local_storage.LocalBackend, device_id: str) -> dict:
    """Decrypt a stored manifest without running ``codex_memory_from_wire``."""
    raw = backend.get(storage_keys.manifest_key(device_id))
    plaintext = crypto.decrypt_manifest(raw, PASSPHRASE, MEMORY_KB)
    return manifest.deserialize_manifest(plaintext)


class _MultiDeviceCodexMemoryEnv:
    """Isolated multi-device harness sharing one LocalBackend, each device with its own HOME."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        device_specs: tuple[tuple[str, str], ...] = (
            (DEV_A, "Mac A"),
            (DEV_B, "Linux B"),
            (DEV_C, "Linux C"),
        ),
    ) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.storage_dir = tmp_path / "storage"
        self.backend = local_storage.LocalBackend(self.storage_dir)
        boot = crypto.bootstrap_crypto_init(self.backend, PASSPHRASE, MEMORY_KB)
        crypto.set_crypto_session(boot.root_salt, MEMORY_KB)

        self.devices: dict[str, SimpleNamespace] = {}
        for did, dname in device_specs:
            devices.register_device(self.backend, did, dname)
            devices.update_last_seen(self.backend, did)
            dev_root = tmp_path / f"dev-{did}"
            home = dev_root / "home"
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True)
            state_dir = dev_root / "state"
            state_dir.mkdir(parents=True)
            sidecar_dir = dev_root / "sidecar"
            sidecar_dir.mkdir(parents=True)
            cfg_path = dev_root / "config.toml"
            copies_dir = home / ".local" / "share" / "mind-meld" / "codex-memory"
            codex_src = config.get_default_source("codex")
            assert codex_src is not None
            codex_src["path"] = str(codex_dir)
            cfg = {
                "device": {"id": did, "name": dname},
                "storage": {"path": str(self.storage_dir)},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [codex_src],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            }
            config.save_config(cfg, cfg_path)
            self.devices[did] = SimpleNamespace(
                id=did,
                name=dname,
                root=dev_root,
                home=home,
                codex=codex_dir,
                copies_dir=copies_dir,
                state_dir=state_dir,
                state_db=state_dir / "state.db",
                deleted_copies=state_dir / "deleted",
                sidecar_dir=sidecar_dir,
                lock_path=dev_root / "mm.lock",
                cfg_path=cfg_path,
                cfg=cfg,
            )

    def activate(self, did: str) -> SimpleNamespace:
        dev = self.devices[did]
        self.monkeypatch.setenv("HOME", str(dev.home))
        self.monkeypatch.setattr(Path, "home", lambda _h=dev.home: _h)
        self.monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self.monkeypatch.setattr(config, "CONFIG_PATH", dev.cfg_path)
        self.monkeypatch.setattr(config, "CONFIG_DIR", dev.cfg_path.parent)
        self.monkeypatch.setattr(config, "LOCK_PATH", dev.lock_path)
        self.monkeypatch.setattr(statedb, "DB_DIR", dev.state_dir)
        self.monkeypatch.setattr(statedb, "db_path", lambda _p=dev.state_db: _p)
        self.monkeypatch.setattr(statedb, "deleted_copies_dir", lambda _d=dev.deleted_copies: _d)
        self.monkeypatch.setattr(sidecar, "SIDECAR_DIR", dev.sidecar_dir)
        return dev

    def push(self, did: str, *, dry_run: bool = False) -> cli.PushResult | None:
        dev = self.activate(did)
        return cli._push_core(
            dev.cfg,
            PASSPHRASE,
            MEMORY_KB,
            dry_run=dry_run,
            verbose=False,
            quiet=True,
        )

    def pull(
        self,
        did: str,
        *,
        from_device: str | None = None,
        dry_run: bool = False,
        quiet: bool = True,
    ) -> pullflow.PullResult:
        dev = self.activate(did)
        return pullflow._pull_core(
            dev.cfg,
            PASSPHRASE,
            MEMORY_KB,
            from_device=from_device,
            dry_run=dry_run,
            quiet=quiet,
        )


# ── T1: Config validation & opt-in rule ──────────────────────────────


class TestCodexMemoryConfig:
    def test_default_source_and_opt_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex" / "memories_v2").mkdir(parents=True)
        (tmp_path / ".codex" / "memories_v2" / "MEMORY.md").write_text("# mem\n")
        (tmp_path / ".claude").mkdir(parents=True)

        assert "codex-memory" not in config.OPT_IN_SOURCE_NAMES
        assert config.get_default_source("codex-memory") is None

        # Legacy non-explicit config derives codex-memory when codex is auto-detected.
        cfg = {
            "device": {"id": DEV_A, "name": "Mac A"},
            "storage": {"path": str(tmp_path / "store")},
            "sync": {},
        }
        resolved_names = {s["name"] for s in config.get_sources(cfg)}
        assert "codex" in resolved_names
        assert "codex-memory" in resolved_names

        # Disabling codex removes both codex and derived codex-memory.
        cfg_disabled = {**cfg, "sync": {"disabled_sources": ["codex"]}}
        disabled_names = {s["name"] for s in config.get_sources(cfg_disabled)}
        assert "codex" not in disabled_names
        assert "codex-memory" not in disabled_names

    def test_filter_disabled_sources_translates_codex_and_ignores_stale_codex_memory(self):
        sample = {
            "version": 2,
            "sources": {
                "claude": {"files": {"a.md": {"sha256": "1"}}},
                "codex": {"files": {"AGENTS.md": {"sha256": "2"}}},
                "codex-memory": {"files": {f"{DEV_A}/MEMORY.md": {"sha256": "3"}}},
            },
            "tombstones": {
                "codex:old.md": {"deleted_at": "2026-11-21T00:00:00Z"},
                f"codex-memory:{DEV_A}/memory_summary.md": {"deleted_at": "2026-11-21T00:00:00Z"},
            },
        }
        # Stale codex-memory in disabled_sources has no effect.
        kept = core._filter_disabled_sources(sample, ["codex-memory"])
        assert set(kept["sources"]) == {"claude", "codex", "codex-memory"}
        assert kept["tombstones"] == sample["tombstones"]

        # Disabling codex strips both codex and codex-memory files, preserving tombstones.
        stripped = core._filter_disabled_sources(sample, ["codex"])
        assert set(stripped["sources"]) == {"claude"}
        assert stripped["tombstones"] == sample["tombstones"]

    @pytest.mark.parametrize(
        "bad_source",
        [
            {"name": "other", "path": "~/.codex", "type": "codex-memory"},
            {"name": "codex-memory", "path": "~/.codex", "type": "generic"},
            {
                "name": "codex-memory",
                "path": "~/.codex",
                "type": "codex-memory",
                "include_dirs": ["memories_v2"],
            },
            {
                "name": "codex-memory",
                "path": "~/.codex",
                "type": "codex-memory",
                "include_files": ["MEMORY.md"],
            },
            {
                "name": "codex-memory",
                "path": "~/.codex",
                "type": "codex-memory",
                "include_patterns": ["**/*"],
            },
        ],
    )
    def test_validation_refusals(self, bad_source):
        with pytest.raises(ConfigError):
            config._validate_sources([bad_source])


# ── T2: Walker, file set, and scan_roots ─────────────────────────────


class TestCodexMemoryWalker:
    def test_holds_a_rollout_removed_after_listing(self, tmp_path, monkeypatch):
        codex = tmp_path / ".codex"
        rollouts = codex / "memories_v2" / "rollout_summaries"
        rollouts.mkdir(parents=True)
        rollout = rollouts / "old.md"
        rollout.write_text("old summary\n")
        original_scandir = os.scandir

        @contextmanager
        def scan_then_remove(path):
            with original_scandir(path) as entries:
                if path == rollouts:
                    entries = list(entries)
                    rollout.unlink()
                yield entries

        monkeypatch.setattr(manifest.os, "scandir", scan_then_remove)
        holds = manifest.SourceHolds(reasons={}, candidates=set())
        source = {"name": "codex-memory", "type": "codex-memory", "path": str(codex)}
        assert manifest.walk_source(source, strict=True, holds=holds)[1] == {}
        assert holds.candidates == {"memories_v2/rollout_summaries/old.md"}

    def test_exact_file_set_and_untouched_tree(self, tmp_path):
        codex = tmp_path / ".codex"
        mem = codex / "memories_v2"
        (mem / "rollout_summaries" / "nested").mkdir(parents=True)
        (mem / ".git").mkdir(parents=True)
        (mem / ".codex-baseline" / ".git").mkdir(parents=True)
        (mem / "extensions" / "ad_hoc").mkdir(parents=True)

        # Allowed files
        (mem / "memory_summary.md").write_text("summary\n")
        (mem / "MEMORY.md").write_text("# memory\n")
        (mem / "rollout_summaries" / "r1.md").write_text("rollout 1\n")

        # Excluded / out-of-set files
        (mem / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (mem / ".codex-baseline" / ".git" / "codex-baseline.lock").write_text("lock\n")
        (mem / "extensions" / "ad_hoc" / "instructions.md").write_text("ext\n")
        (mem / "raw_memories.md").write_text("raw\n")
        (mem / "rollout_summaries" / "nested" / "x.md").write_text("nested\n")
        (mem / "rollout_summaries" / "x.txt").write_text("txt\n")
        (codex / "memories_1.sqlite").write_bytes(b"sqlite")
        (codex / "memories_v2.lock").write_text("lock")

        # Symlink and hardlink inside rollout_summaries
        secret_src = tmp_path / "outside.md"
        secret_src.write_text("outside\n")
        os.symlink(secret_src, mem / "rollout_summaries" / "linked.md")
        os.link(secret_src, mem / "rollout_summaries" / "hardlinked.md")

        before = _snapshot_tree(codex)
        skips: list[tuple[str, str]] = []
        src_cfg = {"name": "codex-memory", "path": str(codex), "type": "codex-memory"}
        files = manifest.walk_codex_memory_source(
            src_cfg, on_skip=lambda p, r: skips.append((p, r)), strict=True
        )
        after = _snapshot_tree(codex)

        assert sorted(files.keys()) == [
            "memories_v2/MEMORY.md",
            "memories_v2/memory_summary.md",
            "memories_v2/rollout_summaries/r1.md",
        ]
        assert before == after
        assert any(r == "symlink" for _, r in skips)
        assert any(r == "hardlink" for _, r in skips)
        assert manifest.scan_roots(src_cfg) == []

    def test_memories_v2_wins_over_memories_and_fallback(self, tmp_path):
        codex = tmp_path / ".codex"
        (codex / "memories").mkdir(parents=True)
        (codex / "memories" / "MEMORY.md").write_text("v1\n")
        src_cfg = {"name": "codex-memory", "path": str(codex), "type": "codex-memory"}

        assert manifest.codex_memory_dir_name(codex) == "memories"
        files_v1 = manifest.walk_codex_memory_source(src_cfg, strict=True)
        assert list(files_v1.keys()) == ["memories/MEMORY.md"]

        (codex / "memories_v2").mkdir(parents=True)
        (codex / "memories_v2" / "MEMORY.md").write_text("v2\n")
        assert manifest.codex_memory_dir_name(codex) == "memories_v2"
        files_v2 = manifest.walk_codex_memory_source(src_cfg, strict=True)
        assert list(files_v2.keys()) == ["memories_v2/MEMORY.md"]


# ── T3: Wire codec, sidecar, idempotent re-push, anchorless deletions ─


class TestCodexMemoryPublication:
    def test_invalid_local_form_refuses_before_blob_uploads(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(tmp_path, monkeypatch)
        dev = env.devices[DEV_A]
        (dev.codex / "memories_v2").mkdir()
        (dev.codex / "memories_v2" / "MEMORY.md").write_text("own copy\n")
        (dev.home / "notes.md").write_text("other source\n")
        dev.cfg["sync"]["sources"].insert(
            0,
            {
                "name": "notes",
                "type": "generic",
                "path": str(dev.home),
                "include_files": ["notes.md"],
            },
        )
        build_manifest = cli.build_manifest_v2

        def invalid_local_form(*args, **kwargs):
            snapshot = build_manifest(*args, **kwargs)
            files = snapshot["sources"]["codex-memory"]["files"]
            files[f"{DEV_A}/MEMORY.md"] = files.pop("memories_v2/MEMORY.md")
            return snapshot

        monkeypatch.setattr(cli, "build_manifest_v2", invalid_local_form)
        with pytest.raises(ManifestError, match="not in local form"):
            env.push(DEV_A)
        assert not env.backend.list_keys("data/")
        assert not env.backend.exists(storage_keys.manifest_key(DEV_A))
        assert not (dev.home / manifest.ANCHOR_NAME).exists()

    def test_missing_published_memory_folder_does_not_mint_deletions(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(tmp_path, monkeypatch)
        codex = env.devices[DEV_A].codex
        memories = codex / "memories_v2"
        memories.mkdir()
        (memories / "MEMORY.md").write_text("keep this copy\n")
        env.push(DEV_A)
        manifest_path = storage_keys.manifest_key(DEV_A)
        before = env.backend.get(manifest_path)

        memories.rename(codex / "saved-memories")
        with pytest.raises(SnapshotError, match="previously published"):
            env.push(DEV_A)
        assert env.backend.get(manifest_path) == before

    def test_recover_sidecar_after_codex_memory_folder_switches(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(tmp_path, monkeypatch)
        codex = env.devices[DEV_A].codex
        memories = codex / "memories"
        memories.mkdir()
        summary = memories / "MEMORY.md"
        summary.write_text("old\n")
        env.push(DEV_A)
        summary.unlink()
        env.push(DEV_A)
        old_sidecar = sidecar.read(DEV_A)
        assert "codex-memory:memories/MEMORY.md" in old_sidecar["tombstones"]

        migrated = codex / "memories_v2"
        memories.rename(migrated)
        (migrated / "MEMORY.md").write_text("back after migration\n")
        env.backend.put(storage_keys.manifest_key(DEV_A), b"unreadable")
        env.push(DEV_A)
        raw = _decrypt_raw_manifest(env.backend, DEV_A)
        assert f"{DEV_A}/MEMORY.md" in raw["sources"]["codex-memory"]["files"]
        assert f"codex-memory:{DEV_A}/MEMORY.md" not in raw["tombstones"]

    def test_push_wire_form_sidecar_noop_and_anchorless_deletion(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(tmp_path, monkeypatch, ((DEV_A, "Mac A"),))
        dev_a = env.devices[DEV_A]
        mem = dev_a.codex / "memories_v2"
        (mem / "rollout_summaries").mkdir(parents=True)
        (mem / "memory_summary.md").write_text("summary A\n")
        (mem / "MEMORY.md").write_text("memory A\n")
        (mem / "rollout_summaries" / "x.md").write_text("rollout X\n")

        before = _snapshot_tree(mem)
        env.push(DEV_A)
        after = _snapshot_tree(mem)
        assert before == after
        assert not list(mem.rglob(manifest.ANCHOR_NAME))

        raw_m = _decrypt_raw_manifest(env.backend, DEV_A)
        wire_keys = sorted(raw_m["sources"]["codex-memory"]["files"].keys())
        assert wire_keys == [
            f"{DEV_A}/MEMORY.md",
            f"{DEV_A}/memory_summary.md",
            f"{DEV_A}/rollout_summaries/x.md",
        ]
        assert not any(k.startswith("memories") for k in wire_keys)

        sc = sidecar.read(DEV_A)
        assert sc is not None
        assert sorted(sc["sources"]["codex-memory"]["files"].keys()) == [
            "memories_v2/MEMORY.md",
            "memories_v2/memory_summary.md",
            "memories_v2/rollout_summaries/x.md",
        ]

        # Rewriting identical bytes with a new mtime publishes nothing.
        manifest_path = env.backend.root / storage_keys.manifest_key(DEV_A)
        before_manifest_bytes = manifest_path.read_bytes()
        mem_file = mem / "MEMORY.md"
        mem_file.write_text("memory A\n")
        new_t = time.time() + 10
        os.utime(mem_file, (new_t, new_t))
        res = env.push(DEV_A)
        assert res is None
        assert manifest_path.read_bytes() == before_manifest_bytes

        # Removing a rollout summary mints a hashed wire-form deletion record.
        (mem / "rollout_summaries" / "x.md").unlink()
        env.push(DEV_A)
        raw_after = _decrypt_raw_manifest(env.backend, DEV_A)
        tkey = f"codex-memory:{DEV_A}/rollout_summaries/x.md"
        assert tkey in raw_after["tombstones"]
        assert storage_keys.is_content_hash(raw_after["tombstones"][tkey].get("sha256"))

    def test_encode_guard_refuses_wire_form_local_manifest(self):
        bad_local = {
            "version": 2,
            "device_id": DEV_A,
            "device_name": "Mac A",
            "timestamp": "2026-11-21T00:00:00Z",
            "sources": {
                "codex-memory": {
                    "base_path": "/Users/user/.codex",
                    "files": {f"{DEV_A}/MEMORY.md": {"sha256": "a" * 64, "size": 1}},
                }
            },
            "tombstones": {},
        }
        with pytest.raises(ManifestError):
            core.encode_manifest(
                bad_local,
                wire_names=False,
                passphrase=PASSPHRASE,
                memory_kb=MEMORY_KB,
            )

    def test_peer_recovered_other_prefix_tombstone_dropped(self):
        local = {
            "version": 2,
            "device_id": DEV_A,
            "device_name": "Mac A",
            "timestamp": "2026-11-21T00:00:00Z",
            "sources": {
                "codex-memory": {
                    "base_path": "/Users/user/.codex",
                    "files": {"memories_v2/MEMORY.md": {"sha256": "a" * 64, "size": 1}},
                }
            },
            "tombstones": {
                f"codex-memory:{DEV_B}/MEMORY.md": {
                    "deleted_at": "2026-11-21T00:00:00Z",
                    "device_id": DEV_B,
                    "sha256": "b" * 64,
                },
                f"codex-memory:{DEV_A}/rollout_summaries/old.md": {
                    "deleted_at": "2026-11-21T00:00:00Z",
                    "device_id": DEV_A,
                    "sha256": "c" * 64,
                },
            },
        }
        wire = manifest.codex_memory_to_wire(local)
        assert f"codex-memory:{DEV_B}/MEMORY.md" not in wire["tombstones"]
        assert f"codex-memory:{DEV_A}/rollout_summaries/old.md" in wire["tombstones"]

    def test_peer_recovery_publishes_only_usable_records(self, tmp_path, monkeypatch):
        # Value: protects=peer recovery publication; fails_when=foreign records
        # are republished or block a viable recovery; why_new=codec tests alone
        # miss the recovery path; seam=none; no_claim=live fleet transport.
        env = _MultiDeviceCodexMemoryEnv(tmp_path, monkeypatch)
        origin, peer = env.devices[DEV_A], env.devices[DEV_B]
        origin_mem = origin.codex / "memories_v2"
        origin_mem.mkdir()
        (origin_mem / "MEMORY.md").write_text("own memory\n")
        peer_mem = peer.codex / "memories_v2"
        peer_mem.mkdir()
        peer_summary = peer_mem / "memory_summary.md"
        peer_summary.write_text("peer memory\n")
        note = peer.home / "note.md"
        note.write_text("notes\n")
        peer.cfg["sync"]["sources"].append(
            {
                "name": "notes",
                "type": "generic",
                "path": str(peer.home),
                "include_files": ["note.md"],
            }
        )
        env.push(DEV_B)
        peer_summary.unlink()
        note.unlink()
        env.push(DEV_B)
        raw_peer = _decrypt_raw_manifest(env.backend, DEV_B)
        assert f"codex-memory:{DEV_B}/memory_summary.md" in raw_peer["tombstones"]
        assert "notes:note.md" in raw_peer["tombstones"]

        env.backend.put(storage_keys.manifest_key(DEV_A), b"unreadable")
        env.push(DEV_A)
        raw_origin = _decrypt_raw_manifest(env.backend, DEV_A)
        assert f"{DEV_A}/MEMORY.md" in raw_origin["sources"]["codex-memory"]["files"]
        assert set(raw_origin["tombstones"]) == {"notes:note.md"}


# ── T4: Pull, confinement, mode 0700, retirement, conflicts, mm-events ─


class TestCodexMemoryPullAndConfinement:
    def test_pull_copies_mode_0700_fast_forward_and_deletion(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(
            tmp_path, monkeypatch, ((DEV_A, "Mac A"), (DEV_B, "Linux B"))
        )
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        mem_a = dev_a.codex / "memories_v2"
        (mem_a / "rollout_summaries").mkdir(parents=True)
        (mem_a / ".git").mkdir()
        (mem_a / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (mem_a / "MEMORY.md").write_text("A v1\n")
        (mem_a / "rollout_summaries" / "r1.md").write_text("rollout 1\n")
        env.push(DEV_A)

        b_codex_before = _snapshot_tree(dev_b.codex)
        env.pull(DEV_B)
        assert _snapshot_tree(dev_b.codex) == b_codex_before
        assert not (dev_b.codex / "memories_v2").exists()
        assert not (dev_b.codex / "memories").exists()

        assert (dev_b.copies_dir / DEV_A / "MEMORY.md").read_text() == "A v1\n"
        assert (dev_b.copies_dir / DEV_A / "rollout_summaries" / "r1.md").read_text() == (
            "rollout 1\n"
        )
        assert stat.S_IMODE(dev_b.copies_dir.parent.lstat().st_mode) == 0o700
        assert stat.S_IMODE(dev_b.copies_dir.lstat().st_mode) == 0o700

        # A drops r1.md and rewrites MEMORY.md -> B moves r1.md to recovery and fast-forwards.
        (mem_a / "rollout_summaries" / "r1.md").unlink()
        mem_file = mem_a / "MEMORY.md"
        mem_file.write_text("A v2\n")
        older = time.time() - 60
        os.utime(mem_file, (older, older))
        env.push(DEV_A)

        pull_res = env.pull(DEV_B)
        assert (dev_b.copies_dir / DEV_A / "MEMORY.md").read_text() == "A v2\n"
        assert not (dev_b.copies_dir / DEV_A / "rollout_summaries" / "r1.md").exists()
        assert pull_res.total_deleted == 1
        assert pull_res.deleted_folder is not None
        assert (
            Path(pull_res.deleted_folder) / "codex-memory" / DEV_A / "rollout_summaries" / "r1.md"
        ).read_text() == "rollout 1\n"
        assert not list(dev_b.copies_dir.rglob("*.sync-conflict-*"))

    def test_pull_without_codex_dir_still_receives_copies(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(
            tmp_path, monkeypatch, ((DEV_A, "Mac A"), (DEV_B, "Linux B"))
        )
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]
        dev_b.codex.rmdir()
        assert not dev_b.codex.exists()

        (dev_a.codex / "memories_v2").mkdir(parents=True)
        (dev_a.codex / "memories_v2" / "memory_summary.md").write_text("summary A\n")
        env.push(DEV_A)

        env.pull(DEV_B)
        assert (dev_b.copies_dir / DEV_A / "memory_summary.md").read_text() == "summary A\n"
        assert not dev_b.codex.exists()
        monkeypatch.setattr(statusflow, "_syncthing_status_lines", lambda *a, **k: [])
        sources = runner.invoke(cli.app, ["sources"], env={"COLUMNS": "180"})
        assert sources.exit_code == 0, sources.output
        assert "codex-memory" not in sources.output
        status = runner.invoke(cli.app, ["status", "--source", "codex-memory"])
        assert status.exit_code == 0, status.output
        assert "Source 'codex-memory'" in status.output

    def test_wire_format_byte_identical_between_legacy_and_codex_only_configs(
        self, tmp_path, monkeypatch
    ):
        env_new = _MultiDeviceCodexMemoryEnv(
            tmp_path / "new", monkeypatch, ((DEV_A, "Mac A"), (DEV_B, "Linux B"))
        )
        env_legacy = _MultiDeviceCodexMemoryEnv(
            tmp_path / "legacy", monkeypatch, ((DEV_A, "Mac A"), (DEV_B, "Linux B"))
        )
        # Legacy-shaped config lists both codex and codex-memory pointing at the same root.
        legacy_a = env_legacy.devices[DEV_A]
        legacy_a.cfg["sync"]["sources"].append(
            {"name": "codex-memory", "path": str(legacy_a.codex), "type": "codex-memory"}
        )
        config.save_config(legacy_a.cfg, legacy_a.cfg_path)

        for env in (env_new, env_legacy):
            dev_a = env.devices[DEV_A]
            (dev_a.codex / "memories_v2" / "rollout_summaries").mkdir(parents=True)
            (dev_a.codex / "memories_v2" / "MEMORY.md").write_text("wire memory\n")
            (dev_a.codex / "memories_v2" / "rollout_summaries" / "r1.md").write_text("r1\n")
            env.push(DEV_A)

        raw_new = _decrypt_raw_manifest(env_new.backend, DEV_A)
        raw_legacy = _decrypt_raw_manifest(env_legacy.backend, DEV_A)
        expected_keys = {f"{DEV_A}/MEMORY.md", f"{DEV_A}/rollout_summaries/r1.md"}
        assert set(raw_new["sources"]["codex-memory"]["files"]) == expected_keys
        assert set(raw_legacy["sources"]["codex-memory"]["files"]) == expected_keys
        for rel in expected_keys:
            assert (
                raw_new["sources"]["codex-memory"]["files"][rel]["sha256"]
                == raw_legacy["sources"]["codex-memory"]["files"][rel]["sha256"]
            )
            assert (
                raw_new["sources"]["codex-memory"]["files"][rel]["size"]
                == raw_legacy["sources"]["codex-memory"]["files"][rel]["size"]
            )

        # Delete r1.md on both and verify identical tombstone keys.
        for env in (env_new, env_legacy):
            (env.devices[DEV_A].codex / "memories_v2" / "rollout_summaries" / "r1.md").unlink()
            env.push(DEV_A)

        raw_new_del = _decrypt_raw_manifest(env_new.backend, DEV_A)
        raw_legacy_del = _decrypt_raw_manifest(env_legacy.backend, DEV_A)
        tkey = f"codex-memory:{DEV_A}/rollout_summaries/r1.md"
        assert tkey in raw_new_del["tombstones"]
        assert tkey in raw_legacy_del["tombstones"]
        assert (
            raw_new_del["tombstones"][tkey]["sha256"]
            == raw_legacy_del["tombstones"][tkey]["sha256"]
        )

        # Pull under a codex-only config applies a manifest pushed under the legacy shape.
        env_legacy.pull(DEV_B)
        dev_b_legacy = env_legacy.devices[DEV_B]
        assert (dev_b_legacy.copies_dir / DEV_A / "MEMORY.md").read_text() == "wire memory\n"
        assert not (dev_b_legacy.copies_dir / DEV_A / "rollout_summaries" / "r1.md").exists()

    def test_missing_copies_root_does_not_block_other_source_deletions(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(tmp_path, monkeypatch)
        for dev in env.devices.values():
            dev.cfg["sync"]["sources"].append(
                {
                    "name": "notes",
                    "type": "generic",
                    "path": str(dev.home),
                    "include_files": ["notes.md"],
                }
            )
        origin = env.devices[DEV_A].home / "notes.md"
        receiver = env.devices[DEV_B]
        copy = receiver.home / "notes.md"
        origin.write_text("notes to sync\n")
        env.push(DEV_A)
        env.pull(DEV_B)
        assert copy.read_text() == "notes to sync\n"
        assert not receiver.copies_dir.exists()

        origin.unlink()
        env.push(DEV_A)
        assert env.pull(DEV_B).total_deleted == 1
        assert not copy.exists()
        assert not receiver.copies_dir.exists()

    def test_forged_peer_paths_and_tombstones_refused(self, tmp_path, monkeypatch, capsys):
        env = _MultiDeviceCodexMemoryEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]
        dev_c = env.devices[DEV_C]

        # C publishes its MEMORY.md; B pulls it (establishing B's base for C).
        (dev_c.codex / "memories_v2").mkdir(parents=True)
        (dev_c.codex / "memories_v2" / "MEMORY.md").write_text("C memory v1\n")
        env.push(DEV_C)
        env.pull(DEV_B)
        c_copy = dev_b.copies_dir / DEV_C / "MEMORY.md"
        assert c_copy.read_text() == "C memory v1\n"
        c_sha = manifest.hash_file(c_copy)

        # Craft a forged manifest for A containing out-of-prefix files and a forged C tombstone.
        payload = b"evil payload\n"
        payload_sha = hashlib.sha256(payload).hexdigest()
        env.backend.put(
            storage_keys.blob_key(DEV_A, payload_sha),
            crypto.encrypt(payload, PASSPHRASE, MEMORY_KB),
        )
        forged = {
            "version": 2,
            "device_id": DEV_A,
            "device_name": "Mac A",
            "timestamp": "2026-11-21T01:00:00Z",
            "sources": {
                "codex-memory": {
                    "base_path": str(dev_a.codex),
                    "files": {
                        f"{DEV_B}/x.md": {"sha256": payload_sha, "size": len(payload)},
                        f"{DEV_C}/MEMORY.md": {"sha256": payload_sha, "size": len(payload)},
                        f"{DEV_A}/raw_memories.md": {"sha256": payload_sha, "size": len(payload)},
                        f"{DEV_A}/.git/HEAD": {"sha256": payload_sha, "size": len(payload)},
                        f"{DEV_A}/rollout_summaries/n/x.md": {
                            "sha256": payload_sha,
                            "size": len(payload),
                        },
                    },
                }
            },
            "tombstones": {
                f"codex-memory:{DEV_C}/MEMORY.md": {
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                    "device_id": DEV_A,
                    "sha256": c_sha,
                }
            },
        }
        enc = crypto.encrypt_manifest(
            manifest.serialize_manifest(forged), PASSPHRASE, MEMORY_KB, wire_names=False
        )
        env.backend.put(storage_keys.manifest_key(DEV_A), enc)

        # Also update C to v2 to prove C's copy is still updated despite A's forged tombstone.
        c_mem = dev_c.codex / "memories_v2" / "MEMORY.md"
        c_mem.write_text("C memory v2\n")
        future = time.time() + 15
        os.utime(c_mem, (future, future))
        env.push(DEV_C)

        capsys.readouterr()
        res = env.pull(DEV_B)
        err = capsys.readouterr().err

        assert res.total_failed == 0
        assert c_copy.read_text() == "C memory v2\n"
        assert not (dev_b.copies_dir / DEV_B).exists()
        assert not (dev_b.copies_dir / DEV_A).exists()
        assert "upgrade mm on that machine" in err
        assert "refused 6 paths" in err
        assert err.count("refused 6 paths") == 1

    def test_retired_machine_copies_removed_on_pull_and_autopull(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]
        dev_c = env.devices[DEV_C]

        (dev_a.codex / "memories_v2").mkdir(parents=True)
        (dev_a.codex / "memories_v2" / "MEMORY.md").write_text("A memory\n")
        env.push(DEV_A)

        (dev_c.codex / "memories_v2").mkdir(parents=True)
        (dev_c.codex / "memories_v2" / "MEMORY.md").write_text("C memory\n")
        env.push(DEV_C)

        env.pull(DEV_B)
        assert (dev_b.copies_dir / DEV_A / "MEMORY.md").exists()
        assert (dev_b.copies_dir / DEV_C / "MEMORY.md").exists()

        # Retire C from A through devicesflow.retire.
        env.activate(DEV_A)
        devicesflow.retire(DEV_C, yes=True)

        # Dry run writes nothing.
        env.pull(DEV_B, dry_run=True)
        assert (dev_b.copies_dir / DEV_C / "MEMORY.md").exists()

        # An unavailable recovery directory must leave retired files in place.
        real_fsync = fsutil.fsync_dir

        def fail_recovery_fsync(path):
            if path.is_relative_to(dev_b.deleted_copies):
                raise StorageError("recovery directory could not be flushed")
            return real_fsync(path)

        with monkeypatch.context() as patch:
            patch.setattr(fsutil, "fsync_dir", fail_recovery_fsync)
            assert env.pull(DEV_B).retired_copies_removed == 0
            assert (dev_b.copies_dir / DEV_C / "MEMORY.md").exists()

        def fail_recovery_move(*args):
            raise OSError("synthetic recovery move failure")

        with monkeypatch.context() as patch:
            patch.setattr(pullflow, "_move_into_copy", fail_recovery_move)
            failed = env.pull(DEV_B)
            assert failed.retired_copies_removed == 0
            assert failed.maintenance_failures == 1
            assert (dev_b.copies_dir / DEV_C / "MEMORY.md").exists()

        (dev_a.codex / "memories_v2" / "MEMORY.md").write_text("A memory v2\n")
        env.push(DEV_A)

        # Real pull removes C's copies into recovery and keeps A's copies.
        res = env.pull(DEV_B)
        assert res.retired_copies_removed == 1
        assert res.retired_copies_names == ["Linux C"]
        assert res.retired_copies_folder is not None
        assert not (dev_b.copies_dir / DEV_C).exists()
        active_copy = dev_b.copies_dir / DEV_A / "MEMORY.md"
        assert active_copy.read_text() == "A memory v2\n"
        assert res.durability_fsync_failures == 0
        ledger, error = core.load_ledger([DEV_A])
        assert error is None
        assert ledger.base(DEV_A, "codex-memory", f"{DEV_A}/MEMORY.md") == manifest.hash_file(
            active_copy
        )
        assert (
            Path(res.retired_copies_folder) / "codex-memory" / DEV_C / "MEMORY.md"
        ).read_text() == "C memory\n"

        # Recreate C's copy to verify autopull also removes retired copies.
        (dev_b.copies_dir / DEV_C).mkdir(parents=True)
        (dev_b.copies_dir / DEV_C / "MEMORY.md").write_text("C memory again\n")
        env.activate(DEV_B)
        monkeypatch.setattr(cli.upgrade, "update_or_nudge", lambda *a, **k: None)
        monkeypatch.setattr(
            cli.meshflow,
            "run",
            lambda *a, **k: cli.meshflow.MeshResult(),
        )
        cli.autopull()
        assert not (dev_b.copies_dir / DEV_C).exists()
        assert (dev_b.copies_dir / DEV_A / "MEMORY.md").exists()

        # The documented ordinary push re-registers a retired ID. The older
        # authenticated retirement record must not remove its new copies.
        (dev_c.codex / "memories_v2" / "MEMORY.md").write_text("C after rejoining\n")
        env.push(DEV_C)
        assert env.pull(DEV_B).retired_copies_removed == 0
        assert (dev_b.copies_dir / DEV_C / "MEMORY.md").read_text() == "C after rejoining\n"

    def test_last_retired_peer_copies_expire_after_recovery(self, tmp_path, monkeypatch):
        env = _MultiDeviceCodexMemoryEnv(
            tmp_path, monkeypatch, ((DEV_A, "Mac A"), (DEV_B, "Linux B"))
        )
        a, b = env.devices[DEV_A], env.devices[DEV_B]
        active = a.codex / "memories_v2"
        active.mkdir()
        (active / "MEMORY.md").write_text("last peer memory\n")
        env.push(DEV_A)
        env.pull(DEV_B)
        devicesflow.retire(DEV_A, yes=True)

        retired = env.pull(DEV_B)
        assert retired.retired_copies_removed == 1
        assert not (b.copies_dir / DEV_A).exists()
        recovery = Path(retired.retired_copies_folder)
        assert (recovery / "codex-memory" / DEV_A / "MEMORY.md").read_text() == "last peer memory\n"
        expired = recovery.with_name("20000101-000000-abcd")
        recovery.rename(expired)
        env.pull(DEV_B, dry_run=True)
        assert expired.exists()
        env.pull(DEV_B)
        assert not expired.exists()

    def test_edited_copy_conflict_found_by_resolveflow_and_mm_events_ignores_copies(
        self, tmp_path, monkeypatch
    ):
        env = _MultiDeviceCodexMemoryEnv(
            tmp_path, monkeypatch, ((DEV_A, "Mac A"), (DEV_B, "Linux B"))
        )
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        (dev_a.codex / "memories_v2").mkdir(parents=True)
        (dev_a.codex / "memories_v2" / "MEMORY.md").write_text("v1\n")
        dev_b.codex.rmdir()
        env.push(DEV_A)
        env.pull(DEV_B)

        # Edit B's copy locally, then rewrite on A -> conflict sidecar in copies_dir.
        (dev_b.copies_dir / DEV_A / "MEMORY.md").write_text("local edit on B\n")
        a_file = dev_a.codex / "memories_v2" / "MEMORY.md"
        a_file.write_text("v2 from A\n")
        future = time.time() + 15
        os.utime(a_file, (future, future))
        env.push(DEV_A)
        res = env.pull(DEV_B)
        assert res.total_conflicted == 1

        env.activate(DEV_B)
        hits = resolveflow._find_conflict_files(dev_b.cfg, migrate_pre_inversion=False)
        assert any(src == "codex-memory" and DEV_A in str(cpath) for src, cpath, _ in hits)
        listed = runner.invoke(cli.app, ["conflicts"])
        assert listed.exit_code == 0, listed.output
        assert "Conflict files (1)" in listed.output
        monkeypatch.setattr(typer, "prompt", lambda *args, **kwargs: "l")
        resolved = runner.invoke(cli.app, ["resolve"])
        assert resolved.exit_code == 0, resolved.output
        assert env.pull(DEV_B).total_conflicted == 0
        ledger, error = core.load_ledger([DEV_A])
        assert error is None
        assert ledger.base(DEV_A, "codex-memory", f"{DEV_A}/MEMORY.md") == manifest.hash_file(
            a_file
        )

        # mm-events push on B never lists codex-memory/ paths.
        mm_events_src = config.get_default_source("mm-events")
        assert mm_events_src is not None
        mm_events_src["path"] = str(dev_b.copies_dir.parent)
        dev_b.cfg["sync"]["sources"].append(mm_events_src)
        events_dir = dev_b.copies_dir.parent / "events"
        events_dir.mkdir()
        event_path = events.write_push_event(
            events_dir,
            DEV_B,
            [{"type": "mm-push", "device": DEV_B, "ts": datetime.now(timezone.utc).isoformat()}],
            strict=True,
        )
        monkeypatch.setattr(events_tail, "_run_events_tail", lambda *args, **kwargs: [])
        env.push(DEV_B)
        published = _decrypt_raw_manifest(env.backend, DEV_B)
        assert set(published["sources"]["mm-events"]["files"]) == {f"events/{event_path.name}"}
        assert "codex-memory" not in published["sources"]


# ── T5: Init detection, enable/disable, sources, status ──────────────


class TestCodexMemoryInitAndSourceCommands:
    @pytest.mark.parametrize("detected", [False, True])
    def test_init_never_persists_separate_codex_memory_source(
        self, tmp_path, monkeypatch, detected
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setattr(config, "CONFIG_PATH", home / "config.toml")
        monkeypatch.setattr(initflow, "DEFAULT_ARGON2_MEMORY_KB", MEMORY_KB)
        monkeypatch.setattr(initflow, "_default_device_name", lambda: "Mac A")
        monkeypatch.setattr(events_tail, "_run_events_backfill", lambda *args, **kwargs: None)
        monkeypatch.setattr(skill_link, "_ensure_retro_skill_links", lambda **kwargs: ())
        if detected:
            active = home / ".codex" / "memories_v2"
            active.mkdir(parents=True)
            (active / "memory_summary.md").write_text("init summary\n")
        storage = tmp_path / "storage"
        storage.mkdir()
        inputs = f"{storage}\nMac A\npw\npw\ny\n" + "\n" * 6
        initialized = runner.invoke(cli.app, ["init", "--new-fleet"], input=inputs)
        assert initialized.exit_code == 0, initialized.output
        assert "codex-memory" not in initialized.output
        saved = config.load_config(config.CONFIG_PATH)
        assert not any(src["name"] == "codex-memory" for src in saved["sync"]["sources"])
        assert any(src["name"] == "codex" for src in saved["sync"]["sources"]) is detected

    def test_init_prompts_never_ask_about_codex_memory(self, tmp_path, monkeypatch):
        from mind_meld import initflow

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        codex = tmp_path / ".codex"
        (codex / "memories_v2").mkdir(parents=True)
        (codex / "memories_v2" / "memory_summary.md").write_text("hi\n")

        seen_prompts: list[tuple[str, bool]] = []

        def fake_ask(prompt: str, *, default: bool = True) -> bool:
            seen_prompts.append((prompt, default))
            return default

        monkeypatch.setattr(core, "ask_yes_no", fake_ask)
        selected = initflow._prompt_sources()
        assert not any("codex-memory" in p[0] for p in seen_prompts)
        assert not any(s["name"] == "codex-memory" for s in selected)
        assert any(s["name"] == "codex" for s in selected)

    def test_codex_enable_disable_preserves_copies_and_merges_sources_row(
        self, tmp_path, monkeypatch
    ):
        env = _MultiDeviceCodexMemoryEnv(
            tmp_path, monkeypatch, ((DEV_A, "Mac A"), (DEV_B, "Linux B"))
        )
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        # Keep claude alongside codex on A so disabling codex still publishes a new manifest.
        claude_dir = dev_a.home / ".claude"
        claude_dir.mkdir(parents=True)
        dev_a.cfg["sync"]["sources"].insert(
            0, {"name": "claude", "path": str(claude_dir), "type": "claude"}
        )
        config.save_config(dev_a.cfg, dev_a.cfg_path)

        # Populate A's Codex memory and a Codex skill file, push, pull on B.
        (dev_a.codex / "skills" / "demo").mkdir(parents=True)
        (dev_a.codex / "skills" / "demo" / "SKILL.md").write_text("# skill\n")
        (dev_a.codex / "memories_v2").mkdir(parents=True)
        (dev_a.codex / "memories_v2" / "MEMORY.md").write_text("keep me\n")
        env.push(DEV_A)
        env.pull(DEV_B)
        assert (dev_b.copies_dir / DEV_A / "MEMORY.md").read_text() == "keep me\n"

        # mm sources shows codex with merged file count and no codex-memory row.
        env.activate(DEV_A)
        res_sources = runner.invoke(cli.app, ["sources"], env={"COLUMNS": "180"})
        assert res_sources.exit_code == 0, res_sources.output
        assert "codex-memory" not in res_sources.stdout
        assert "codex" in res_sources.stdout

        # Disable codex on A and push: neither codex nor codex-memory files or new tombstones,
        # and B keeps its existing copies after pull.
        sourceflow.disable_source("codex")
        reloaded_after = config.load_config(dev_a.cfg_path)
        assert "codex" in reloaded_after["sync"]["disabled_sources"]
        dev_a.cfg = reloaded_after

        env.push(DEV_A)
        raw_a = _decrypt_raw_manifest(env.backend, DEV_A)
        assert "codex" not in raw_a.get("sources", {})
        assert "codex-memory" not in raw_a.get("sources", {})
        assert not any(k.startswith("codex:") for k in raw_a.get("tombstones", {}))
        assert not any(k.startswith("codex-memory:") for k in raw_a.get("tombstones", {}))

        env.pull(DEV_B)
        assert (dev_b.copies_dir / DEV_A / "MEMORY.md").read_text() == "keep me\n"
