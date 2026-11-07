"""Track 111A: Grok Build memory-v2 allowlist, Dream inbox held rule, and pull boundary tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mind_meld import (
    cli,
    config,
    core,
    crypto,
    devices,
    errors,
    manifest,
    pullflow,
    resolveflow,
    sidecar,
    statedb,
)
from mind_meld.storage.keys import blob_key, manifest_key
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "grok-memory-111a-test-passphrase"
MEMORY_KB = 1024
DEV_A = "aaaaaaaa"
DEV_B = "bbbbbbbb"
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
NOW_US = int(NOW.timestamp() * 1_000_000)


def _hours(n: float) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=n)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("wrapped_lstat", [False, True], ids=["native", "wrapped"])
def test_grok_parent_replaced_between_siblings_stays_outside_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrapped_lstat: bool
) -> None:
    root = tmp_path / "grok"
    topics = root / "memory-v2/global/topics"
    topics.mkdir(parents=True)
    (topics / "a.md").write_text("first")
    (topics / "b.md").write_text("second")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.md").write_text("outside sentinel")
    if wrapped_lstat:
        original_lstat = Path.lstat
        monkeypatch.setattr(Path, "lstat", lambda self: original_lstat(self))
    original_record = manifest._record_file

    def replace_after_first(path, *args, **kwargs):
        result = original_record(path, *args, **kwargs)
        if path.name == "a.md":
            topics.rename(topics.with_name("moved-topics"))
            topics.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(manifest, "_record_file", replace_after_first)
    files = manifest.walk_grok_source(
        {"name": "grok", "type": "grok", "path": str(root)}, strict=True
    )
    assert set(files) == {"memory-v2/global/topics/a.md"}


@pytest.mark.parametrize("wrapped_lstat", [False, True], ids=["native", "wrapped"])
def test_revision_refuses_linked_parent_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrapped_lstat: bool
) -> None:
    root = tmp_path / "grok"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("outside sentinel")
    (root / "topics").symlink_to(outside, target_is_directory=True)
    if wrapped_lstat:
        original_lstat = Path.lstat
        monkeypatch.setattr(Path, "lstat", lambda self: original_lstat(self))
    reads = []
    original_read = os.read

    def observe_read(fd, size):
        reads.append(fd)
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", observe_read)
    with pytest.raises(errors.SnapshotError, match="confined regular file"):
        manifest.read_file_revision(
            root / "topics/note.md",
            max_file_size=1024,
            retain_bytes=True,
            source_name="grok",
            source_type="grok",
            base=root,
        )
    assert reads == []


# ── Group 1: grok_memory_path_allowed, is_grok_inbox_path, grok_path_allowed ──


class TestGrokMemoryPathAllowlist:
    @pytest.mark.parametrize(
        "rel_path",
        [
            "memory-v2/global/topics/conventions.md",
            "memory-v2/global/topics/nested/sub/note.md",
            "memory-v2/global/observations/_inbox/2026-04-20-obs.md",
            "memory-v2/global/observations/_inbox/nested/obs.md",
            "memory-v2/workspaces/github.com-org-repo-main/topics/arch.md",
            "memory-v2/workspaces/github.com-org-repo-main/topics/deep/design.md",
            "memory-v2/workspaces/github.com-org-repo-main/observations/_inbox/obs.md",
            "memory-v2/workspaces/local-deadbeef/observations/_inbox/sub/obs.md",
        ],
    )
    def test_accepts_allowlisted_memory_markdown_paths(self, rel_path: str) -> None:
        assert manifest.grok_memory_path_allowed(rel_path) is True
        assert manifest.grok_path_allowed(rel_path) is True

    @pytest.mark.parametrize(
        "rel_path",
        [
            "memory-v2/global/MEMORY.md",
            "memory-v2/workspaces/proj/MEMORY.md",
            "memory-v2/global/index.sqlite",
            "memory-v2/global/memory_state.sqlite",
            "memory-v2/global/memory_state.sqlite-shm",
            "memory-v2/global/memory_state.sqlite-wal",
            "memory-v2/workspaces/proj/memory_state.sqlite",
            "memory-v2/workspaces/proj/memory_state.sqlite-shm",
            "memory-v2/workspaces/proj/memory_state.sqlite-wal",
            "memory-v2/global/archive/old.md",
            "memory-v2/workspaces/proj/archive/2026-04-20.md",
            "memory-v2/global/observations/2026-04-20.md",
            "memory-v2/workspaces/proj/observations/2026-04-20.md",
            "memory-v2/workspaces/proj/observations/other/obs.md",
            "memory-v2/.retired-stores/old/topics/a.md",
            "memory-v2/workspaces/.retired-stores/topics/a.md",
            "memory-v2/note.md",
            "memory-v2/global/note.md",
            "memory-v2/workspaces/note.md",
            "memory-v2/workspaces/proj/note.md",
            "memory-v2/global/topics/note.sync-conflict-20260421-143055-devA1234.md",
            "memory-v2/workspaces/proj/observations/_inbox/obs.sync-conflict-20260421-143055-devA1234.md",
            "memory-v2/global/topics/note.txt",
            "memory-v2/global/topics/note.json",
            "memory-v2/global/topics/NOTE.MD",
            "memory-v2/workspaces/proj/observations/_inbox/OBS.MD",
            "memory-v2/global/topics/../MEMORY.md",
            "memory-v2/global/topics/./a.md",
            "memory-v2/workspaces/../global/topics/a.md",
            "memory-v2\\global\\topics\\a.md",
            "memory-v2/global/topics//a.md",
            "/memory-v2/global/topics/a.md",
            "memory-v2/global/topics/",
            "memory-v2/workspaces//topics/a.md",
            "skills/my-skill/SKILL.md",
            "auth.json",
        ],
    )
    def test_rejects_disallowed_memory_paths(self, rel_path: str) -> None:
        assert manifest.grok_memory_path_allowed(rel_path) is False

    def test_is_grok_inbox_path_matches_only_observations_inbox(self) -> None:
        assert (
            manifest.is_grok_inbox_path("memory-v2/global/observations/_inbox/2026-04-20-obs.md")
            is True
        )
        assert (
            manifest.is_grok_inbox_path("memory-v2/workspaces/proj/observations/_inbox/sub/obs.md")
            is True
        )
        assert manifest.is_grok_inbox_path("memory-v2/global/topics/conventions.md") is False
        assert manifest.is_grok_inbox_path("memory-v2/workspaces/proj/topics/arch.md") is False
        assert (
            manifest.is_grok_inbox_path("memory-v2/workspaces/proj/observations/2026-04-20.md")
            is False
        )
        assert manifest.is_grok_inbox_path("skills/my-skill/SKILL.md") is False

    def test_grok_path_allowed_covers_customizations_and_memory_allowlist(self) -> None:
        assert manifest.grok_path_allowed("skills/my-skill/SKILL.md") is True
        assert manifest.grok_path_allowed("commands/ship.md") is True
        assert manifest.grok_path_allowed("rules/house.md") is True
        assert manifest.grok_path_allowed("memory-v2/global/topics/a.md") is True
        assert manifest.grok_path_allowed("memory-v2/workspaces/p/observations/_inbox/o.md") is True
        # Disallowed grok paths
        assert manifest.grok_path_allowed("skills") is False
        assert (
            manifest.grok_path_allowed("skills/a.sync-conflict-20260421-143055-devA1234.md")
            is False
        )
        assert manifest.grok_path_allowed("auth.json") is False
        assert manifest.grok_path_allowed("config.toml") is False
        assert manifest.grok_path_allowed("hooks/h.sh") is False
        assert manifest.grok_path_allowed("sessions/s/updates.jsonl") is False
        assert manifest.grok_path_allowed("memory-v2/global/MEMORY.md") is False
        assert manifest.grok_path_allowed("memory-v2/global/memory_state.sqlite") is False
        assert manifest.grok_path_allowed("memory-v2/workspaces/p/archive/o.md") is False


# ── Group 2: Walker fixture store test ────────────────────────────────


class TestGrokMemoryWalkerFixture:
    def test_walker_collects_only_allowlisted_memory_markdown_and_customizations(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / ".grok"
        # Customizations
        (home / "skills" / "custom-skill").mkdir(parents=True)
        (home / "skills" / "custom-skill" / "SKILL.md").write_text("skill\n")
        (home / "commands").mkdir(parents=True)
        (home / "commands" / "deploy.md").write_text("cmd\n")
        (home / "rules").mkdir(parents=True)
        (home / "rules" / "style.md").write_text("rule\n")

        # Global memory-v2
        g_topics = home / "memory-v2" / "global" / "topics"
        g_inbox = home / "memory-v2" / "global" / "observations" / "_inbox"
        g_topics.mkdir(parents=True)
        g_inbox.mkdir(parents=True)
        (g_topics / "prefs.md").write_text("prefs\n")
        # Default GROK_EXCLUDE_PATTERNS must not exclude topics mentioning skills/gstack
        (g_topics / "skills-notes.md").write_text("skills notes\n")
        (g_inbox / "obs-global.md").write_text("global obs\n")
        (home / "memory-v2" / "global" / "MEMORY.md").write_text("index\n")
        (home / "memory-v2" / "global" / "index.sqlite").write_bytes(b"sqlite")
        (home / "memory-v2" / "global" / "memory_state.sqlite").write_bytes(b"sqlite")
        (home / "memory-v2" / "global" / "memory_state.sqlite-shm").write_bytes(b"shm")
        (home / "memory-v2" / "global" / "memory_state.sqlite-wal").write_bytes(b"wal")
        (home / "memory-v2" / "global" / "archive").mkdir(parents=True)
        (home / "memory-v2" / "global" / "archive" / "dreamed.md").write_text("dreamed\n")
        (home / "memory-v2" / "global" / "observations" / "2026-04-20.md").write_text(
            "outside inbox\n"
        )

        # Workspace memory-v2 ("proj" and "gstack" to test GROK_EXCLUDE_PATTERNS non-interference)
        w_proj = home / "memory-v2" / "workspaces" / "proj"
        (w_proj / "topics" / "nested").mkdir(parents=True)
        (w_proj / "observations" / "_inbox").mkdir(parents=True)
        (w_proj / "topics" / "arch.md").write_text("arch\n")
        (w_proj / "topics" / "nested" / "sub.md").write_text("sub\n")
        (w_proj / "observations" / "_inbox" / "obs1.md").write_text("obs1\n")
        (w_proj / "MEMORY.md").write_text("proj index\n")
        (w_proj / "memory_state.sqlite").write_bytes(b"sqlite")
        (w_proj / "memory_state.sqlite-shm").write_bytes(b"shm")
        (w_proj / "memory_state.sqlite-wal").write_bytes(b"wal")
        (w_proj / "archive").mkdir(parents=True)
        (w_proj / "archive" / "archived.md").write_text("archived\n")
        (w_proj / "observations" / "2026-04-20.md").write_text("outside inbox\n")

        w_gstack = home / "memory-v2" / "workspaces" / "gstack" / "topics"
        w_gstack.mkdir(parents=True)
        (w_gstack / "note.md").write_text("gstack workspace note\n")

        # .retired-stores/
        retired = home / "memory-v2" / ".retired-stores" / "v1" / "topics"
        retired.mkdir(parents=True)
        (retired / "old.md").write_text("retired\n")

        # Nested .git repo inside topics/
        nested_git = w_proj / "topics" / "subrepo"
        (nested_git / ".git").mkdir(parents=True)
        (nested_git / "inside-git.md").write_text("in git\n")

        # Symlinked workspace scope directory
        outside_scope = tmp_path / "outside-scope"
        (outside_scope / "topics").mkdir(parents=True)
        (outside_scope / "topics" / "secret.md").write_text("secret\n")
        (home / "memory-v2" / "workspaces" / "symlinked-scope").symlink_to(
            outside_scope, target_is_directory=True
        )

        # Hard-linked topic file (st_nlink > 1)
        hardlink_target = tmp_path / "external-secret.md"
        hardlink_target.write_text("hardlinked\n")
        (g_topics / "hardlinked.md").hardlink_to(hardlink_target)

        # Uppercase .MD, non-Markdown, and conflict sidecar inside allowlisted dirs
        (g_topics / "UPPER.MD").write_text("upper\n")
        (g_topics / "notes.txt").write_text("txt\n")
        (g_topics / "prefs.sync-conflict-20260421-143055-devA1234.md").write_text("conflict\n")

        src = config.get_default_source("grok")
        assert src is not None
        src["path"] = str(home)
        skipped: list[tuple[str, str]] = []
        files = manifest.walk_grok_source(
            src, strict=True, on_skip=lambda p, r: skipped.append((p, r))
        )

        assert set(files) == {
            "skills/custom-skill/SKILL.md",
            "commands/deploy.md",
            "rules/style.md",
            "memory-v2/global/topics/prefs.md",
            "memory-v2/global/topics/skills-notes.md",
            "memory-v2/global/observations/_inbox/obs-global.md",
            "memory-v2/workspaces/proj/topics/arch.md",
            "memory-v2/workspaces/proj/topics/nested/sub.md",
            "memory-v2/workspaces/proj/observations/_inbox/obs1.md",
            "memory-v2/workspaces/gstack/topics/note.md",
        }
        assert (
            "memory-v2/workspaces/proj/topics/subrepo",
            manifest.NESTED_REPO_SKIP_REASON,
        ) in skipped
        assert manifest.nested_repo_skip_prefixes(src, strict=True) == [
            "memory-v2/workspaces/proj/topics/subrepo"
        ]

    def test_vanished_workspace_scope_mid_walk_raises_snapshot_error_when_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / ".grok"
        ws_topics = home / "memory-v2" / "workspaces" / "vanishing" / "topics"
        ws_topics.mkdir(parents=True)
        (ws_topics / "a.md").write_text("a\n")

        real_collect = manifest._collect_regular_files_scandir

        def vanish_before_collect(scan_dir: Path, *args, **kwargs):
            if scan_dir == ws_topics.resolve():
                # Simulate workspace directory disappearing between grok_memory_scan_dirs
                # discovery and _collect_regular_files_scandir.
                (ws_topics / "a.md").unlink()
                ws_topics.rmdir()
            return real_collect(scan_dir, *args, **kwargs)

        monkeypatch.setattr(manifest, "_collect_regular_files_scandir", vanish_before_collect)
        with pytest.raises(errors.SnapshotError, match="disappeared while being read"):
            manifest.walk_grok_source(
                {"name": "grok", "path": str(home), "type": "grok"},
                strict=True,
            )


# ── Two-device E2E harness for Groups 3, 4, 5, 6, 7, 8 ────────────────


class _TwoDeviceGrokEnv:
    """Two isolated devices sharing a LocalBackend, each with its own HOME, config, and state.db."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.storage_dir = tmp_path / "storage"
        self.backend = LocalBackend(self.storage_dir)
        boot = crypto.bootstrap_crypto_init(self.backend, PASSPHRASE, MEMORY_KB)
        crypto.set_crypto_session(boot.root_salt, MEMORY_KB)
        for did, dname in ((DEV_A, "Mac A"), (DEV_B, "Linux B")):
            devices.register_device(self.backend, did, dname)
            devices.update_last_seen(self.backend, did)

        self.devices: dict[str, SimpleNamespace] = {}
        for did, dname in ((DEV_A, "Mac A"), (DEV_B, "Linux B")):
            dev_root = tmp_path / f"dev-{did}"
            home = dev_root / "home"
            grok_dir = home / ".grok"
            grok_dir.mkdir(parents=True)
            state_dir = dev_root / "state"
            state_dir.mkdir(parents=True)
            sidecar_dir = dev_root / "sidecar"
            sidecar_dir.mkdir(parents=True)
            cfg_path = dev_root / "config.toml"
            cfg = {
                "device": {"id": did, "name": dname},
                "storage": {"path": str(self.storage_dir)},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [
                        {
                            "name": "grok",
                            "path": str(grok_dir),
                            "type": "grok",
                            "exclude_patterns": list(manifest.GROK_EXCLUDE_PATTERNS),
                        }
                    ],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            }
            config.save_config(cfg, cfg_path)
            self.devices[did] = SimpleNamespace(
                id=did,
                name=dname,
                root=dev_root,
                home=home,
                grok=grok_dir,
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
        self.monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self.monkeypatch.setattr(config, "CONFIG_PATH", dev.cfg_path)
        self.monkeypatch.setattr(statedb, "DB_DIR", dev.state_dir)
        self.monkeypatch.setattr(statedb, "db_path", lambda: dev.state_db)
        self.monkeypatch.setattr(sidecar, "SIDECAR_DIR", dev.sidecar_dir)
        return dev

    def push(self, did: str) -> None:
        dev = self.activate(did)
        cli._push_core(
            dev.cfg,
            PASSPHRASE,
            MEMORY_KB,
            dry_run=False,
            verbose=False,
            quiet=True,
        )

    def pull(self, did: str, *, from_device: str | None = None) -> pullflow.PullResult:
        dev = self.activate(did)
        return pullflow._pull_core(
            dev.cfg,
            PASSPHRASE,
            MEMORY_KB,
            from_device=from_device,
            dry_run=False,
            verbose=False,
            quiet=True,
        )


# ── Group 3: Scan root and anchor tests ───────────────────────────────


class TestGrokMemoryAnchorsAndTombstones:
    def test_missing_memory_v2_creates_nothing_and_leaves_root_id_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        (dev_a.grok / "skills" / "s1").mkdir(parents=True)
        (dev_a.grok / "skills" / "s1" / "SKILL.md").write_text("s1\n")

        env.push(DEV_A)

        assert not (dev_a.grok / "memory-v2").exists()
        fetched = core._fetch_remote_manifest(env.backend, DEV_A, PASSPHRASE, MEMORY_KB)
        assert fetched.is_ok
        root_ids = fetched.manifest["sources"]["grok"].get("root_ids", {})
        assert "skills" in root_ids
        assert "memory-v2" not in root_ids

    def test_first_push_after_upgrade_uploads_memory_v2_without_minting_tombstones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        (dev_a.grok / "skills" / "s1").mkdir(parents=True)
        (dev_a.grok / "skills" / "s1" / "SKILL.md").write_text("s1\n")
        env.push(DEV_A)

        # Simulate pre-upgrade state where memory-v2 existed locally before upgrade
        # while the prior manifest only had root_ids for ["skills"].
        topic = dev_a.grok / "memory-v2" / "global" / "topics" / "conventions.md"
        topic.parent.mkdir(parents=True)
        topic.write_text("conventions\n")

        env.push(DEV_A)

        fetched = core._fetch_remote_manifest(env.backend, DEV_A, PASSPHRASE, MEMORY_KB)
        assert fetched.is_ok
        grok_src = fetched.manifest["sources"]["grok"]
        assert "memory-v2/global/topics/conventions.md" in grok_src["files"]
        assert "memory-v2" in grok_src.get("root_ids", {})
        assert (dev_a.grok / "memory-v2" / manifest.ANCHOR_NAME).is_file()
        assert fetched.manifest.get("tombstones", {}) == {}

    def test_dream_archive_propagates_hashed_tombstone_with_recovery_copy_and_new_obs_syncs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        obs1_rel = "memory-v2/workspaces/proj/observations/_inbox/obs1.md"
        obs1_a = dev_a.grok / obs1_rel
        obs1_a.parent.mkdir(parents=True)
        obs1_a.write_text("observation 1\n")

        # A pushes obs1; B pulls it and pushes so both are anchored.
        env.push(DEV_A)
        env.pull(DEV_B)
        obs1_b = dev_b.grok / obs1_rel
        assert obs1_b.read_text() == "observation 1\n"
        env.push(DEV_B)

        # Dream on A moves _inbox/obs1.md -> archive/obs1.md, and a new session writes obs2.md.
        archive_a = dev_a.grok / "memory-v2" / "workspaces" / "proj" / "archive" / "obs1.md"
        archive_a.parent.mkdir(parents=True)
        obs1_a.rename(archive_a)
        obs2_rel = "memory-v2/workspaces/proj/observations/_inbox/obs2.md"
        obs2_a = dev_a.grok / obs2_rel
        obs2_a.write_text("observation 2\n")

        env.push(DEV_A)
        fetched_a = core._fetch_remote_manifest(env.backend, DEV_A, PASSPHRASE, MEMORY_KB)
        assert fetched_a.is_ok
        tomb = fetched_a.manifest["tombstones"].get(f"grok:{obs1_rel}")
        assert tomb is not None
        assert tomb.get("sha256") == _sha(b"observation 1\n")

        # B pulls: obs1.md is deleted with a 30-day recovery copy; obs2.md lands in B's _inbox/.
        res_b = env.pull(DEV_B)
        assert res_b.total_deleted == 1
        assert not obs1_b.exists()
        assert (dev_b.grok / obs2_rel).read_text() == "observation 2\n"
        recovery_files = list(dev_b.deleted_copies.rglob("obs1.md"))
        assert len(recovery_files) == 1
        assert recovery_files[0].read_text() == "observation 1\n"


# ── Group 4: Cross-machine Dream / inbox held rule tests ──────────────


class TestGrokInboxDreamOrderings:
    def test_machine_harness_dream_before_publication_holds(self) -> None:
        from tests.test_pullplan import Machine

        inbox_rel = "memory-v2/workspaces/proj/observations/_inbox/obs.md"
        raw = b"observation payload\n"

        # Ordering (i): A pushes obs.md, B pulls (never pushes), Dream on B removes obs.md,
        # B pulls A again while A still lists obs.md -> held, not re-created.
        a = Machine("a", rel=inbox_rel, src="grok")
        b = Machine("b", rel=inbox_rel, src="grok")
        a.edit(raw, _hours(1))
        a.push()
        assert b.pull(a) == "write"
        assert b.lists is False  # B has not pushed since pulling
        b.remove()  # Dream on B moves obs.md to archive/
        assert b.pull(a) == "held"
        assert b.data is None

    @pytest.mark.parametrize("a_pulls_b", [True, False], ids=["ii-base", "iii-no-base"])
    def test_machine_harness_dream_publication_base(self, a_pulls_b: bool) -> None:
        from tests.test_pullplan import Machine

        rel = "memory-v2/workspaces/proj/observations/_inbox/obs.md"
        a, b = Machine("a", rel=rel, src="grok"), Machine("b", rel=rel, src="grok")
        a.edit(b"observation\n", _hours(1))
        a.push()
        assert b.pull(a) == "write"
        b.push()
        if a_pulls_b:
            assert a.pull(b) == "matched"
        assert a.ledger.base(b.id, "grok", rel) == (b.sha if a_pulls_b else None)
        b.remove()
        assert b.pull(a) == "held"
        assert a.data == b"observation\n"

    @pytest.mark.parametrize("a_pulls_b", [True, False], ids=["ii-delete", "iii-retain"])
    def test_e2e_dream_deletion_requires_origin_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, a_pulls_b: bool
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a, dev_b = env.devices[DEV_A], env.devices[DEV_B]
        rel = "memory-v2/workspaces/proj/observations/_inbox/obs.md"
        a, b = dev_a.grok / rel, dev_b.grok / rel
        a.parent.mkdir(parents=True)
        a.write_text("pending observation\n")
        env.push(DEV_A)
        env.pull(DEV_B)
        env.push(DEV_B)
        if a_pulls_b:
            env.pull(DEV_A)
        b.unlink()
        env.push(DEV_B)
        result = env.pull(DEV_A)
        assert result.total_deleted == int(a_pulls_b)
        assert a.exists() is not a_pulls_b
        copies = list(dev_a.deleted_copies.rglob("obs.md"))
        assert len(copies) == int(a_pulls_b)
        if copies:
            assert copies[0].read_text() == "pending observation\n"
        else:
            assert a.read_text() == "pending observation\n"
            a.unlink()  # The origin's own Dream consumes its retained observation.
            env.push(DEV_A)
        env.pull(DEV_B)
        assert not b.exists()

    def test_e2e_dream_orderings_and_mixed_version_peer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        obs_rel = "memory-v2/workspaces/proj/observations/_inbox/obs.md"
        obs_a = dev_a.grok / obs_rel
        obs_b = dev_b.grok / obs_rel
        obs_a.parent.mkdir(parents=True)
        obs_a.write_text("pending observation\n")

        # A legitimate earlier empty B publication establishes the root anchor,
        # without ever publishing the observation that B will consume below.
        (dev_b.grok / "memory-v2").mkdir()
        env.push(DEV_B)
        env.push(DEV_A)
        env.pull(DEV_B)
        assert obs_b.read_text() == "pending observation\n"
        b_prior = core._fetch_remote_manifest(env.backend, DEV_B, PASSPHRASE, MEMORY_KB)
        assert obs_rel not in b_prior.manifest["sources"]["grok"]["files"]

        # Ordering (i): Dream on B archives obs.md BEFORE B pushes the removal; B pulls A again.
        obs_b.unlink()
        env.pull(DEV_B)
        assert not obs_b.exists(), "Dream-archived inbox file must stay held on B"

        # Finish ordering (i): the origin's later Dream is silent on B.
        obs_a.unlink()
        env.push(DEV_A)
        res_ii = env.pull(DEV_B)
        assert not obs_b.exists()
        assert res_ii.total_failed == 0
        assert res_ii.total_deleted == 0

        # Both machines may also Dream a new observation independently.
        # obs2 on BOTH A and B and both push and pull.
        obs2_rel = "memory-v2/workspaces/proj/observations/_inbox/obs2.md"
        obs2_a = dev_a.grok / obs2_rel
        obs2_b = dev_b.grok / obs2_rel
        obs2_a.write_text("second observation\n")
        env.push(DEV_A)
        env.pull(DEV_B)
        assert obs2_b.exists()
        obs2_a.unlink()
        obs2_b.unlink()
        env.push(DEV_A)
        env.push(DEV_B)
        env.pull(DEV_A)
        env.pull(DEV_B)
        assert not obs2_a.exists()
        assert not obs2_b.exists()
        assert list(dev_a.grok.rglob("*.sync-conflict-*")) == []
        assert list(dev_b.grok.rglob("*.sync-conflict-*")) == []

        # Mixed-version peer: Peer A has a pre-111A manifest (root_ids only has "skills",
        # no "memory-v2" files), while B has local memory-v2 files. Pulling A must not delete
        # B's local memory-v2 files.
        topic_b = dev_b.grok / "memory-v2" / "global" / "topics" / "local-topic.md"
        topic_b.parent.mkdir(parents=True, exist_ok=True)
        topic_b.write_text("keep me\n")
        env.push(DEV_B)

        head_shaped_a = {
            "version": 2,
            "device_id": DEV_A,
            "device_name": "Mac A",
            "timestamp": NOW.isoformat(),
            "sources": {
                "grok": {
                    "base_path": str(dev_a.grok),
                    "files": {},
                    "root_ids": {"skills": "abcdef0123456789abcdef0123456789"},
                }
            },
            "tombstones": {},
        }
        env.backend.put(
            manifest_key(DEV_A),
            core.encode_manifest(
                head_shaped_a, wire_names=False, passphrase=PASSPHRASE, memory_kb=MEMORY_KB
            ),
        )
        env.pull(DEV_B)
        assert topic_b.read_text() == "keep me\n"


# ── Group 5: Pull-side boundary tests ─────────────────────────────────


class TestGrokPullBoundaryAllowlist:
    @pytest.mark.parametrize("user_excludes", [[], ["skills/custom-skip*"]])
    def test_peer_manifest_injecting_non_allowlisted_files_and_tombstones_is_filtered_everywhere(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        user_excludes: list[str],
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_b = env.devices[DEV_B]
        dev_b.cfg["sync"]["sources"][0]["exclude_patterns"] = user_excludes
        config.save_config(dev_b.cfg, dev_b.cfg_path)

        # Seed local files on B at forbidden paths AND record ledger bases for DEV_A
        # so an unfiltered active hashed tombstone from DEV_A would delete them!
        forbidden_payloads = {
            "memory-v2/global/memory_state.sqlite": b"local-sqlite",
            "memory-v2/global/MEMORY.md": b"local-index\n",
            "memory-v2/workspaces/p/archive/o.md": b"local-archive\n",
            "auth.json": b"local-auth-token",
            "config.toml": b"local-config\n",
            "hooks/h.sh": b"#!/bin/sh\necho safe\n",
            "sessions/x/updates.jsonl": b"{}\n",
        }
        env.activate(DEV_B)
        ledger, _ = core.load_ledger([DEV_A])
        for rel, data in forbidden_payloads.items():
            target = dev_b.grok / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            digest = _sha(data)
            ledger.settle(DEV_A, "grok", rel, digest)
        core.commit_ledger(ledger)

        # Peer A manifest injects both `files` entries (with different bytes) AND active
        # hashed tombstones for forbidden paths, alongside a legitimate skill and topic.
        legit_skill = "skills/good/SKILL.md"
        legit_topic = "memory-v2/global/topics/good.md"
        injected_files: dict[str, dict] = {}
        injected_tombstones: dict[str, dict] = {}

        for rel, payload in [
            (legit_skill, b"good skill\n"),
            (legit_topic, b"good topic\n"),
            *[(k, b"malicious-overwrite") for k in forbidden_payloads],
        ]:
            digest = _sha(payload)
            env.backend.put(
                blob_key(DEV_A, digest),
                crypto.encrypt(payload, PASSPHRASE, MEMORY_KB),
            )
            injected_files[rel] = {
                "sha256": digest,
                "size": len(payload),
                "mtime": NOW.isoformat(),
            }

        for rel, local_bytes in forbidden_payloads.items():
            injected_tombstones[f"grok:{rel}"] = {
                "deleted_at": NOW.isoformat(),
                "deleted_by": DEV_A,
                "sha256": _sha(local_bytes),
            }

        peer_manifest = {
            "version": 2,
            "device_id": DEV_A,
            "device_name": "Mac A",
            "timestamp": NOW.isoformat(),
            "sources": {
                "grok": {
                    "base_path": str(env.devices[DEV_A].grok),
                    "files": injected_files,
                }
            },
            "tombstones": injected_tombstones,
        }
        env.backend.put(
            manifest_key(DEV_A),
            core.encode_manifest(
                peer_manifest, wire_names=False, passphrase=PASSPHRASE, memory_kb=MEMORY_KB
            ),
        )

        # 1) Pull applies only legitimate entries and leaves forbidden local files untouched.
        res = env.pull(DEV_B)
        assert res.total_applied == 2
        assert (dev_b.grok / legit_skill).read_bytes() == b"good skill\n"
        assert (dev_b.grok / legit_topic).read_bytes() == b"good topic\n"
        for rel, expected_bytes in forbidden_payloads.items():
            assert (dev_b.grok / rel).read_bytes() == expected_bytes, f"corrupted or deleted {rel}"

        # 2) Push B so B's remote manifest matches B's allowlisted local state, then check
        # `mm diff --from DEV_A` and `mm status`: zero phantom pending changes for forbidden paths.
        # Next, test a peer manifest that has ONLY the forbidden files (plus already-synced legit).
        peer_manifest_only_forbidden_tombstones = {
            **peer_manifest,
            "sources": {
                "grok": {
                    "base_path": str(env.devices[DEV_A].grok),
                    "files": {
                        legit_skill: injected_files[legit_skill],
                        legit_topic: injected_files[legit_topic],
                        **{k: injected_files[k] for k in forbidden_payloads},
                    },
                }
            },
            "tombstones": {},
        }
        env.backend.put(
            manifest_key(DEV_A),
            core.encode_manifest(
                peer_manifest_only_forbidden_tombstones,
                wire_names=False,
                passphrase=PASSPHRASE,
                memory_kb=MEMORY_KB,
            ),
        )
        # Also test tombstone-only peer manifest by verifying _filter_excluded_paths drops both.
        filtered = core._filter_excluded_paths(
            peer_manifest,
            exclude_map={},
            skip_prefixes={},
            include_map={},
        )
        assert set(filtered["sources"]["grok"]["files"]) == {legit_skill, legit_topic}
        assert filtered["tombstones"] == {}

        runner = CliRunner()
        env.activate(DEV_B)
        diff_res = runner.invoke(cli.app, ["diff", "--from", DEV_A])
        assert diff_res.exit_code == 0, diff_res.output
        assert "No differences." in diff_res.output
        for rel in forbidden_payloads:
            assert rel not in diff_res.output

        status_res = runner.invoke(cli.app, ["status"])
        assert status_res.exit_code == 0, status_res.output
        for rel in forbidden_payloads:
            assert rel not in status_res.output

    def test_pull_skips_local_symlink_at_workspace_scope_or_topics_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        rel_ws = "memory-v2/workspaces/sym-ws/topics/a.md"
        rel_topics = "memory-v2/global/topics/b.md"
        (dev_a.grok / rel_ws).parent.mkdir(parents=True)
        (dev_a.grok / rel_ws).write_text("from A ws\n")
        (dev_a.grok / rel_topics).parent.mkdir(parents=True)
        (dev_a.grok / rel_topics).write_text("from A global\n")
        env.push(DEV_A)

        # On B, make workspaces/sym-ws and global/topics symlinks to outside directories.
        outside_ws = tmp_path / "outside-ws"
        outside_ws.mkdir()
        outside_topics = tmp_path / "outside-topics"
        outside_topics.mkdir()
        (dev_b.grok / "memory-v2" / "workspaces").mkdir(parents=True)
        (dev_b.grok / "memory-v2" / "workspaces" / "sym-ws").symlink_to(
            outside_ws, target_is_directory=True
        )
        (dev_b.grok / "memory-v2" / "global").mkdir(parents=True)
        (dev_b.grok / "memory-v2" / "global" / "topics").symlink_to(
            outside_topics, target_is_directory=True
        )

        env.pull(DEV_B)
        assert list(outside_ws.rglob("*")) == []
        assert list(outside_topics.rglob("*")) == []


# ── Group 6: User exclude_patterns opt-out test ───────────────────────


class TestGrokMemoryExcludePatternsOptOut:
    def test_exclude_patterns_memory_v2_wildcard_skips_push_and_pull_without_tombstones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        # First push on A with memory-v2 enabled so a topic is in A's prior manifest.
        topic_rel = "memory-v2/global/topics/conventions.md"
        skill_rel = "skills/my-skill/SKILL.md"
        (dev_a.grok / topic_rel).parent.mkdir(parents=True)
        (dev_a.grok / topic_rel).write_text("initial topic\n")
        (dev_a.grok / skill_rel).parent.mkdir(parents=True)
        (dev_a.grok / skill_rel).write_text("v1 skill\n")
        env.push(DEV_A)
        env.pull(DEV_B)
        assert (dev_b.grok / topic_rel).read_text() == "initial topic\n"

        # Now user adds exclude_patterns = ["memory-v2/*"] on A, edits skill and topic, and pushes.
        dev_a.cfg["sync"]["sources"][0]["exclude_patterns"] = [
            *manifest.GROK_EXCLUDE_PATTERNS,
            "memory-v2/*",
        ]
        config.save_config(dev_a.cfg, dev_a.cfg_path)
        (dev_a.grok / skill_rel).write_text("v2 skill\n")
        (dev_a.grok / topic_rel).write_text("local-only topic edit\n")

        env.push(DEV_A)
        fetched_a = core._fetch_remote_manifest(env.backend, DEV_A, PASSPHRASE, MEMORY_KB)
        assert fetched_a.is_ok
        assert topic_rel not in fetched_a.manifest["sources"]["grok"]["files"]
        assert skill_rel in fetched_a.manifest["sources"]["grok"]["files"]
        # Must NOT mint a tombstone for the still-present excluded local memory file.
        assert f"grok:{topic_rel}" not in fetched_a.manifest.get("tombstones", {})

        # A resumes publishing a changed topic; only B opts out on the receive side.
        dev_a.cfg["sync"]["sources"][0]["exclude_patterns"] = list(manifest.GROK_EXCLUDE_PATTERNS)
        config.save_config(dev_a.cfg, dev_a.cfg_path)
        env.push(DEV_A)
        fetched_a = core._fetch_remote_manifest(env.backend, DEV_A, PASSPHRASE, MEMORY_KB)
        assert topic_rel in fetched_a.manifest["sources"]["grok"]["files"]
        dev_b.cfg["sync"]["sources"][0]["exclude_patterns"] = [
            *manifest.GROK_EXCLUDE_PATTERNS,
            "memory-v2/*",
        ]
        config.save_config(dev_b.cfg, dev_b.cfg_path)
        env.pull(DEV_B)
        assert (dev_b.grok / skill_rel).read_text() == "v2 skill\n"
        assert (dev_b.grok / topic_rel).read_text() == "initial topic\n"


# ── Group 7: Branch-A workspace mapping test ──────────────────────────


class TestGrokBranchAWorkspaceMapping:
    def test_cross_machine_git_remote_scope_round_trips_without_path_rewriting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Create two checkouts at different local paths (/Users/alice/code/app vs
        # /home/alice/src/app) sharing the same git remote origin URL.
        checkout_a = tmp_path / "Users" / "alice" / "code" / "app"
        checkout_b = tmp_path / "home" / "alice" / "src" / "app"
        remote_url = "https://github.com/kbitz/mind-meld.git"
        for checkout in (checkout_a, checkout_b):
            checkout.mkdir(parents=True)
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", remote_url],
                cwd=checkout,
                check=True,
                capture_output=True,
            )

        # Naming is proved by the live Grok check in host-parity.md. The sync
        # contract preserves that observed opaque name; mm does not derive it.
        for checkout in (checkout_a, checkout_b):
            assert (
                subprocess.run(
                    ["git", "config", "--get", "remote.origin.url"],
                    cwd=checkout,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                == remote_url
            )
        scope_a = scope_b = "mind-meld-ac6e5bf3"

        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        rel_path = f"memory-v2/workspaces/{scope_a}/topics/arch.md"
        topic_a = dev_a.grok / rel_path
        topic_a.parent.mkdir(parents=True)
        topic_a.write_text("# Architecture\nShared across machines.\n")

        env.push(DEV_A)
        fetched_a = core._fetch_remote_manifest(env.backend, DEV_A, PASSPHRASE, MEMORY_KB)
        assert fetched_a.is_ok
        # Verify grok paths are never rewritten by Claude's project_form_state / manifest_to_wire
        # and never flagged by project_name_report.
        assert rel_path in fetched_a.manifest["sources"]["grok"]["files"]
        report = core.project_name_report({DEV_A: fetched_a.manifest})
        assert report.not_placed_files == 0
        assert report.old_form == {}

        env.pull(DEV_B)
        topic_b = dev_b.grok / f"memory-v2/workspaces/{scope_b}/topics/arch.md"
        assert topic_b.read_text() == "# Architecture\nShared across machines.\n"


# ── Group 8: First-sync of two populated stores ───────────────────────


class TestGrokMemoryFirstSyncCollision:
    def test_first_sync_of_two_populated_stores_preserves_local_and_lists_conflict_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _TwoDeviceGrokEnv(tmp_path, monkeypatch)
        dev_a = env.devices[DEV_A]
        dev_b = env.devices[DEV_B]

        rel = "memory-v2/global/topics/conventions.md"
        file_a = dev_a.grok / rel
        file_b = dev_b.grok / rel
        file_a.parent.mkdir(parents=True)
        file_b.parent.mkdir(parents=True)
        file_a.write_text("Machine A conventions\n")
        file_b.write_text("Machine B conventions\n")
        # Give A a newer mtime so B's pull of A triggers a conflict (no ledger base).
        os.utime(file_b, (1_700_000_000, 1_700_000_000))
        os.utime(file_a, (1_700_000_100, 1_700_000_100))

        env.push(DEV_A)
        res = env.pull(DEV_B)
        assert res.total_conflicted == 1
        # Local canonical bytes on B are preserved.
        assert file_b.read_text() == "Machine B conventions\n"

        # Remote bytes are saved as a .sync-conflict-* sidecar and surfaced by _find_conflict_files.
        hits = resolveflow._find_conflict_files(dev_b.cfg)
        assert len(hits) == 1
        src_name, conflict_path, canonical_path = hits[0]
        assert src_name == "grok"
        assert canonical_path == file_b.resolve()
        assert conflict_path.read_text() == "Machine A conventions\n"
