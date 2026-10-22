"""Integration tests for Mind Meld — full push/pull round-trips."""

import ast
import errno
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import cli as cli_module
from mind_meld import config as config_module
from mind_meld import crypto as crypto_module
from mind_meld import events as _mm_events
from mind_meld import events_tail, fsutil, synclog
from mind_meld import host_usage as _mm_host_usage
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import (
    bootstrap_crypto_init,
    decrypt,
    encrypt,
)
from mind_meld.devices import register_device
from mind_meld.manifest import (
    deserialize_manifest,
    load_manifest,
    normalize_manifest,
    serialize_manifest,
)
from mind_meld.storage import keys as storage_keys
from mind_meld.storage.local import LocalBackend
from mind_meld.synclog import write_sync_log

PASSPHRASE = "integration-test-passphrase"
MEMORY_KB = 1024  # Low for fast tests
runner = CliRunner()

# Track 72B: one distinct synthetic file per generator glob, including overlap-
# free archive/binary paths so directory exclusion cannot mask a missing glob.
_GENERATOR_PATHS72B = (
    "projects/acme/tool/venv/pyvenv.cfg",
    "projects/acme/tool/.venv/bin/python",
    "projects/acme/tool/Spike.app/Contents/Info.plist",
    "projects/acme/qa/.qa-evidence/001/observation.json",
    "projects/acme/artifacts/x.zip",
    "projects/acme/artifacts/x.tar",
    "projects/acme/artifacts/x.tgz",
    "projects/acme/artifacts/x.tar.gz",
    "projects/acme/artifacts/x.tar.xz",
    "projects/acme/artifacts/x.tar.bz2",
    "projects/acme/artifacts/x.tar.zst",
    "projects/acme/artifacts/x.whl",
    "projects/acme/artifacts/x.so",
    "projects/acme/artifacts/x.dylib",
    "projects/acme/artifacts/x.pak",
    "projects/acme/artifacts/x.dmg",
)
_KEPT_PATHS72B = {
    "projects/acme/decisions.jsonl": b'{"id":"base"}\n',
    "projects/acme/ceo-plans/plan.md": b"Keep this plan.\n",
    "projects/acme/deno-source/cli/tools/desktop.rs": b"fn main() {}\n",
    "projects/acme/probe/tests/fixtures/cases.json": b"{}\n",
    "projects/acme/review-and-prep/run/compact-snapshots/run-1/receipt.json": b"{}\n",
    "projects/acme/review-and-prep/run/ranking-summaries/summary.json": b"{}\n",
}
_PRE18_GSTACK_GLOBS72B = [
    "config.yaml",
    "projects/*/repo-mode.json",
    "projects/*/land-deploy-confirmed",
    "analytics/.last-sync-*",
    "projects/*/decisions.active.json",
    "projects/*/brain-cache/*",
    "projects/*/pair-review/session.yaml",
]

# Track 51A mixed-ts fixtures. Expected merge is a literal, not derived
# from merge._extract_ts. Local keeps a unique string-ts line so retry
# hashes still differ and `_apply_merge`'s no-op branch runs.
_MIXED_REMOTE_JSONL = (
    b'{"ts":"2026-01-02T00:00:00Z","k":"remote-str"}\n'
    b'{"ts":2,"k":"remote-int"}\n'
    b'{"ts":"2026-01-01T00:00:00Z","k":"remote-early"}\n'
)
_LOCAL_ONLY_JSONL = (
    b'{"ts":"2026-01-03T00:00:00Z","k":"local-only"}\n'
    b'{"ts":"2026-01-02T00:00:00Z","k":"remote-str"}\n'
)
_MERGED_MIXED_JSONL = (
    b'{"ts":"2026-01-01T00:00:00Z","k":"remote-early"}\n'
    b'{"ts":"2026-01-02T00:00:00Z","k":"remote-str"}\n'
    b'{"ts":"2026-01-03T00:00:00Z","k":"local-only"}\n'
    b'{"ts":2,"k":"remote-int"}\n'
)
_ORDINARY_PEER_BYTES = b"ordinary-from-peer\n"


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(tmp_path / "storage")


@pytest.fixture
def claude_dir_a(tmp_path):
    """Simulate Machine A's ~/.claude."""
    d = tmp_path / "machine_a" / ".claude"
    memory = d / "projects" / "-Users-kb-myapp" / "memory"
    memory.mkdir(parents=True)
    (memory / "user_role.md").write_text("---\nname: role\n---\nData scientist")
    (memory / "feedback.md").write_text("---\nname: feedback\n---\nNo mocks")
    return d


@pytest.fixture
def claude_dir_b(tmp_path):
    """Simulate Machine B's ~/.claude (starts empty)."""
    d = tmp_path / "machine_b" / ".claude"
    d.mkdir(parents=True)
    return d


class TestPushPullRoundTrip:
    """CLI-driven push/pull round-trips via CliRunner.

    Exercises the full `_pull_core` → `_apply_incoming_file` dispatch tree
    instead of hand-rolling encrypt/put/decrypt. Each test simulates two
    machines with distinct `~/.claude` paths and swaps the active config
    between phases so `mm push` and `mm pull` see the correct device
    identity.
    """

    def _make_config(self, tmp_path, storage_dir, claude_dir, device_id, device_name):
        config_path = tmp_path / f"config_{device_id}.toml"
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path

    def _populate_claude(self, claude_dir, role="Data scientist", feedback="No mocks"):
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "user_role.md").write_text(f"---\nname: role\n---\n{role}")
        (memory / "feedback.md").write_text(f"---\nname: feedback\n---\n{feedback}")

    def _activate(self, monkeypatch, config_path):
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

    def _bootstrap(self, storage_dir):
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        return backend

    def test_push_then_pull(self, tmp_path, monkeypatch):
        """Push from A, pull to B — files match bit-for-bit."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        claude_b = tmp_path / "machine_b" / ".claude"
        self._populate_claude(claude_a)
        claude_b.mkdir(parents=True)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = self._make_config(tmp_path, storage_dir, claude_a, "dev-a", "A")
        config_b = self._make_config(tmp_path, storage_dir, claude_b, "dev-b", "B")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        self._activate(monkeypatch, config_a)
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        self._activate(monkeypatch, config_b)
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 0, result.output

        # Files arrive verbatim on B's claude_dir.
        for rel in (
            "projects/-Users-kb-myapp/memory/user_role.md",
            "projects/-Users-kb-myapp/memory/feedback.md",
        ):
            original = (claude_a / rel).read_bytes()
            pulled = (claude_b / rel).read_bytes()
            assert original == pulled, f"Mismatch: {rel}"

    def test_muse_memory_round_trip_conflict_and_local_only_files(self, tmp_path, monkeypatch):
        """Push A, pull B: memory/ arrives; sessions/ and the PID lock never leave."""
        storage_dir = tmp_path / "storage"
        muse_a = tmp_path / "machine_a" / "muse"
        muse_b = tmp_path / "machine_b" / "muse"
        personal_a = muse_a / "memory" / "personal"
        personal_a.mkdir(parents=True)
        (personal_a / "MEMORY.md").write_text("- [Role](role.md)\n")
        (personal_a / "role.md").write_text("Data scientist\n")
        (personal_a / ".muse-memory.lock").write_text("pid=111\n")
        project_a = muse_a / "memory" / "projects" / "Users-kb-app-0c84"
        project_a.mkdir(parents=True)
        (project_a / "notes.md").write_text("project note\n")
        session_a = muse_a / "sessions" / "2026" / "10" / "14" / "s1"
        session_a.mkdir(parents=True)
        (session_a / "session.jsonl").write_text('{"prompt": "never synced"}\n')
        personal_b = muse_b / "memory" / "personal"
        personal_b.mkdir(parents=True)
        (personal_b / ".muse-memory.lock").write_text("pid=222\n")

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")
        configs = {}
        for device, root in (("dev-a", muse_a), ("dev-b", muse_b)):
            configs[device] = tmp_path / f"config_{device}.toml"
            save_config(
                {
                    "device": {"id": device, "name": device},
                    "storage": {"path": str(storage_dir)},
                    "sync": {
                        "max_file_size": 52_428_800,
                        "sources": [{"name": "muse", "path": str(root), "type": "muse"}],
                    },
                    "crypto": {"argon2_memory_kb": MEMORY_KB},
                },
                configs[device],
            )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        self._activate(monkeypatch, configs["dev-a"])
        pushed = runner.invoke(app, ["push"])
        assert pushed.exit_code == 0, pushed.output
        remote = load_manifest(
            decrypt(
                backend.get("manifests/dev-a/manifest.json.enc"), PASSPHRASE, memory_kb=MEMORY_KB
            )
        )
        assert set(remote["sources"]["muse"]["files"]) == {
            "memory/personal/MEMORY.md",
            "memory/personal/role.md",
            "memory/projects/Users-kb-app-0c84/notes.md",
        }

        self._activate(monkeypatch, configs["dev-b"])
        pulled = runner.invoke(app, ["pull"])
        assert pulled.exit_code == 0, pulled.output
        for rel in (
            "memory/personal/MEMORY.md",
            "memory/personal/role.md",
            "memory/projects/Users-kb-app-0c84/notes.md",
        ):
            assert (muse_b / rel).read_bytes() == (muse_a / rel).read_bytes(), rel
        assert (personal_b / ".muse-memory.lock").read_text() == "pid=222\n"
        assert not (muse_b / "sessions").exists()

        # Divergent edits: an ordinary note keeps local bytes plus a remote
        # sidecar; MEMORY.md is an index, so it line-unions like Claude's.
        self._activate(monkeypatch, configs["dev-a"])
        (personal_a / "role.md").write_text("Principal engineer\n")
        (personal_a / "MEMORY.md").write_text("- [Role](role.md)\n- [Team](team.md)\n")
        assert runner.invoke(app, ["push"]).exit_code == 0
        self._activate(monkeypatch, configs["dev-b"])
        role_b = personal_b / "role.md"
        role_b.write_text("Staff engineer\n")
        (personal_b / "MEMORY.md").write_text("- [Role](role.md)\n- [Goals](goals.md)\n")
        old = time.time() - 3600
        for path in (role_b, personal_b / "MEMORY.md"):
            os.utime(path, (old, old))
        pulled = runner.invoke(app, ["pull", "--conflict-mode", "keep-both"])
        assert pulled.exit_code == 0, pulled.output
        assert role_b.read_text() == "Staff engineer\n"
        sidecars = list(personal_b.glob("role.sync-conflict-*.md"))
        assert [p.read_text() for p in sidecars] == ["Principal engineer\n"]
        assert (personal_b / "MEMORY.md").read_text().splitlines() == sorted(
            ["- [Goals](goals.md)", "- [Role](role.md)", "- [Team](team.md)"]
        )
        assert (personal_b / ".muse-memory.lock").read_text() == "pid=222\n"

    def test_enable_grok_preserves_legacy_claude_manifest_on_next_push(self, tmp_path, monkeypatch):
        """Materializing Grok must not turn legacy Claude content into tombstones."""
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / "machine_a" / ".claude"
        self._populate_claude(claude_dir)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        config_path = self._make_config(tmp_path, storage_dir, claude_dir, "dev-a", "A")
        config = config_module.load_config(config_path)
        config["sync"].pop("sources")
        config["sync"]["claude_dir"] = str(claude_dir)
        save_config(config, config_path)
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("HOME", str(home))

        assert runner.invoke(app, ["push"]).exit_code == 0

        (home / ".grok").mkdir(parents=True)
        enabled = runner.invoke(app, ["enable-source", "grok"])
        assert enabled.exit_code == 0, enabled.output
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md").write_text(
            "changed after enabling grok"
        )

        pushed = runner.invoke(app, ["push"])
        assert pushed.exit_code == 0, pushed.output
        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        assert (
            "projects/-Users-kb-myapp/memory/user_role.md" in remote["sources"]["claude"]["files"]
        )
        assert not [key for key in remote["tombstones"] if key.startswith("claude:")]

    def test_deletion_not_propagated_in_additive_model(self, tmp_path, monkeypatch):
        """Delete on A, push, pull to B — B preserves its local copy (additive model)."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        claude_b = tmp_path / "machine_b" / ".claude"
        self._populate_claude(claude_a)
        claude_b.mkdir(parents=True)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = self._make_config(tmp_path, storage_dir, claude_a, "dev-a", "A")
        config_b = self._make_config(tmp_path, storage_dir, claude_b, "dev-b", "B")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # A pushes full state; B pulls and now has both files.
        self._activate(monkeypatch, config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0
        self._activate(monkeypatch, config_b)
        assert runner.invoke(app, ["pull"]).exit_code == 0
        role_b = claude_b / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md"
        assert role_b.exists()

        # A deletes user_role.md and pushes again. In the additive model, the
        # re-push records a tombstone (not blocking), but pull doesn't act on
        # tombstones from other devices in the default flow.
        self._activate(monkeypatch, config_a)
        (claude_a / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md").unlink()
        assert runner.invoke(app, ["push"]).exit_code == 0

        # B pulls again. Its local user_role.md must still be there.
        self._activate(monkeypatch, config_b)
        assert runner.invoke(app, ["pull"]).exit_code == 0
        assert role_b.exists(), "Additive pull must preserve local-only files"

    def test_push_pull_conflict_tombstone_combined(self, tmp_path, monkeypatch):
        """End-to-end push → pull → conflict → tombstone in one run.

        Exercises the interaction surface that the isolated
        test_conflict_copy.py and test_additive_sync.py tests don't cover
        together: conflict-copy preservation, tombstone generation on
        re-push, and the additive pull model co-existing cleanly.
        """
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        claude_b = tmp_path / "machine_b" / ".claude"
        self._populate_claude(claude_a)
        claude_b.mkdir(parents=True)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = self._make_config(tmp_path, storage_dir, claude_a, "dev-a", "A")
        config_b = self._make_config(tmp_path, storage_dir, claude_b, "dev-b", "B")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # Phase 1: A pushes, B pulls → B has both files.
        self._activate(monkeypatch, config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0
        self._activate(monkeypatch, config_b)
        assert runner.invoke(app, ["pull"]).exit_code == 0
        role_b = claude_b / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md"
        assert role_b.exists()
        original_role = role_b.read_text()

        # Phase 2: divergent edit. A modifies user_role.md and pushes; B edits
        # it locally (different content). B pulls — conflict-copy expected.
        self._activate(monkeypatch, config_a)
        (claude_a / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md").write_text(
            "---\nname: role\n---\nPrincipal engineer"
        )
        assert runner.invoke(app, ["push"]).exit_code == 0

        self._activate(monkeypatch, config_b)
        role_b.write_text("---\nname: role\n---\nSomething else entirely")
        # Backdate B's mtime so the pull sees remote as newer and generates
        # a conflict-copy instead of skipping.
        old = time.time() - 3600
        os.utime(role_b, (old, old))

        assert runner.invoke(app, ["pull", "--conflict-mode", "keep-both"]).exit_code == 0

        # Track 5E inversion: canonical now stays at LOCAL bytes (B's
        # edit), and REMOTE bytes (A's push) land in the .sync-conflict-*
        # sidecar. Pre-v0.9.2 was the opposite.
        assert role_b.read_text() == "---\nname: role\n---\nSomething else entirely"
        memory_b = role_b.parent
        conflict_siblings = list(memory_b.glob("user_role.sync-conflict-*.md"))
        assert len(conflict_siblings) == 1, f"Expected one conflict copy, got {conflict_siblings}"
        assert conflict_siblings[0].read_text() == "---\nname: role\n---\nPrincipal engineer"

        # Phase 3: A deletes feedback.md and pushes. Tombstone is recorded in
        # A's manifest. B pulls; B's local feedback.md is preserved (additive),
        # but the tombstone lives in storage and is visible to any future
        # correctness sweep.
        self._activate(monkeypatch, config_a)
        (claude_a / "projects" / "-Users-kb-myapp" / "memory" / "feedback.md").unlink()
        assert runner.invoke(app, ["push"]).exit_code == 0

        self._activate(monkeypatch, config_b)
        assert runner.invoke(app, ["pull"]).exit_code == 0
        feedback_b = claude_b / "projects" / "-Users-kb-myapp" / "memory" / "feedback.md"
        assert feedback_b.exists(), "Additive pull must preserve file locally"

        # Verify A's pushed manifest contains the tombstone for feedback.md.
        manifest_key_a = "manifests/dev-a/manifest.json.enc"
        enc = backend.get(manifest_key_a)
        remote = deserialize_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        tombstones = remote.get("tombstones", {})
        assert any("feedback.md" in k for k in tombstones), (
            f"Expected feedback.md tombstone in A's manifest, got keys: {list(tombstones)}"
        )

        # Sanity: B's modified content is what's now at canonical (post-inversion).
        assert original_role != role_b.read_text()


class TestConflictClockSeparation:
    """B7 / F1 / F2: v1 mint, fresh-marker device, vanish self-heal."""

    def _make_config(self, tmp_path, storage_dir, claude_dir, device_id, device_name):
        config_path = tmp_path / f"config_{device_id}.toml"
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path

    def _populate(self, claude_dir, role="Data scientist"):
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "user_role.md").write_text(f"---\nname: role\n---\n{role}")

    def _conflict_on_b(self, tmp_path, monkeypatch, *, wipe_marker: bool):
        from mind_meld import sidecar as sidecar_mod
        from mind_meld.manifest import is_pre_inversion_conflict_filename, is_v1_conflict_filename

        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        claude_b = tmp_path / "machine_b" / ".claude"
        self._populate(claude_a, role="Principal engineer")
        claude_b.mkdir(parents=True)

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")
        config_a = self._make_config(tmp_path, storage_dir, claude_a, "dev-a", "A")
        config_b = self._make_config(tmp_path, storage_dir, claude_b, "dev-b", "B")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
        assert runner.invoke(app, ["pull"]).exit_code == 0

        role_b = claude_b / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md"
        role_b.write_text("---\nname: role\n---\nSomething else entirely")
        os.utime(role_b, (time.time() - 3600, time.time() - 3600))

        if wipe_marker:
            marker = sidecar_mod.SIDECAR_DIR / "inversion-installed-at"
            marker.unlink(missing_ok=True)

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
        (claude_a / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md").write_text(
            "---\nname: role\n---\nEven newer from A"
        )
        assert runner.invoke(app, ["push"]).exit_code == 0
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
        assert runner.invoke(app, ["pull", "--conflict-mode", "keep-both"]).exit_code == 0

        memory_b = role_b.parent
        sidecars = list(memory_b.glob("user_role.sync-conflict-*.md"))
        assert len(sidecars) == 1, sidecars
        sidecar = sidecars[0]
        assert is_v1_conflict_filename(sidecar.name)
        assert not is_pre_inversion_conflict_filename(sidecar.name)
        assert role_b.read_text() == "---\nname: role\n---\nSomething else entirely"
        return config_b, role_b, sidecar

    def test_conflicting_pull_mints_v1_sidecar(self, tmp_path, monkeypatch):
        """F1."""
        _cfg, role_b, sidecar = self._conflict_on_b(tmp_path, monkeypatch, wipe_marker=False)
        assert "v1" in sidecar.name
        assert sidecar.read_text() == "---\nname: role\n---\nEven newer from A"
        assert role_b.read_bytes() != sidecar.read_bytes()

    def test_fresh_marker_device_does_not_migrate_on_next_pull(self, tmp_path, monkeypatch):
        """B7: inversion-installed-at = now; sidecar survives a second pull."""
        from mind_meld.manifest import is_pre_inversion_conflict_filename, is_v1_conflict_filename

        config_b, _role_b, sidecar = self._conflict_on_b(tmp_path, monkeypatch, wipe_marker=True)
        name_before = sidecar.name
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
        assert runner.invoke(app, ["pull"]).exit_code == 0
        assert sidecar.exists()
        assert sidecar.name == name_before
        assert is_v1_conflict_filename(sidecar.name)
        assert not is_pre_inversion_conflict_filename(sidecar.name)
        v0 = list(sidecar.parent.glob("user_role.sync-conflict-v0-*.md"))
        assert v0 == []

    def test_unlinked_sidecar_returns_on_repull(self, tmp_path, monkeypatch):
        """F2 / W2 self-heal."""
        config_b, role_b, sidecar = self._conflict_on_b(tmp_path, monkeypatch, wipe_marker=False)
        sidecar.unlink()
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
        assert runner.invoke(app, ["pull", "--conflict-mode", "keep-both"]).exit_code == 0
        sidecars = list(role_b.parent.glob("user_role.sync-conflict-*.md"))
        assert len(sidecars) == 1
        assert sidecars[0].read_text() == "---\nname: role\n---\nEven newer from A"

    def test_status_mentions_unresolved_conflicts(self, tmp_path, monkeypatch):
        config_b, _role, _sidecar = self._conflict_on_b(tmp_path, monkeypatch, wipe_marker=False)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Unresolved conflicts: 1" in result.output


class TestExcludePatterns5C:
    """Track 5C IRON RULE regression pins.

    Reasoning is in `_filter_excluded_paths` and the consumer-boundary
    filter wiring (cli.py: `_pull_core`, `_push_core`). Failing these
    tests blocks ship.
    """

    def _make_gstack_config(
        self,
        tmp_path,
        storage_dir,
        gstack_dir,
        device_id,
        device_name,
        *,
        exclude_patterns=None,
        include_patterns=None,
    ):
        config_path = tmp_path / f"config_{device_id}.toml"
        gstack_src: dict = {
            "name": "gstack",
            "path": str(gstack_dir),
            "type": "generic",
            "include_dirs": ["projects"],
        }
        if exclude_patterns is not None:
            gstack_src["exclude_patterns"] = exclude_patterns
        if include_patterns is not None:
            gstack_src["include_patterns"] = include_patterns
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [gstack_src],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path

    def _populate_gstack(self, gstack_dir, project="myapp", repo_mode="solo"):
        proj = gstack_dir / "projects" / project
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "repo-mode.json").write_text(f'{{"mode": "{repo_mode}"}}')
        (proj / "land-deploy-confirmed").write_text("OK")
        (proj / "role.md").write_text("data scientist")

    def _activate(self, monkeypatch, config_path):
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

    def _bootstrap(self, storage_dir):
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        return backend

    def _backdate(self, *paths, seconds=3600):
        old = time.time() - seconds
        for p in paths:
            os.utime(p, (old, old))

    def _populate_generators72b(self, root):
        files = {rel: f"generated:{rel}\n".encode() for rel in _GENERATOR_PATHS72B}
        files.update(_KEPT_PATHS72B)
        for rel, data in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return files

    def _remote72b(self, backend, device):
        return load_manifest(
            decrypt(backend.get(storage_keys.manifest_key(device)), PASSPHRASE, memory_kb=MEMORY_KB)
        )

    def test_generator_explicit_config_upgrade72b(self, tmp_path, monkeypatch):
        """Migration trims A's manifest while a stale peer keeps and republishes bytes."""
        storage_dir = tmp_path / "storage"
        root_a, root_b = tmp_path / "a" / ".gstack", tmp_path / "b" / ".gstack"
        original = self._populate_generators72b(root_a)
        root_b.mkdir(parents=True)
        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")
        config_a = self._make_gstack_config(
            tmp_path,
            storage_dir,
            root_a,
            "dev-a",
            "A",
            exclude_patterns=[*_PRE18_GSTACK_GLOBS72B, "projects/acme/private/*"],
            # Pin to the generator-glob migration; allowlist has own coverage.
            include_patterns=[],
        )
        config_b = self._make_gstack_config(
            tmp_path,
            storage_dir,
            root_b,
            "dev-b",
            "B",
            exclude_patterns=list(_PRE18_GSTACK_GLOBS72B),
        )
        old_b_sources = config_module.load_config(config_b)["sync"]["sources"]
        TestMigrateConfigCommand()._redirect_lock(monkeypatch, tmp_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        def invoke(config, args):
            self._activate(monkeypatch, config)
            result = runner.invoke(app, args)
            assert result.exit_code == 0, result.output
            if config == config_b:
                # Initial crypto fingerprint backfill may update other sections.
                assert config_module.load_config(config_b)["sync"]["sources"] == old_b_sources
            return " ".join(result.output.split())

        invoke(config_a, ["push"])
        assert set(self._remote72b(backend, "dev-a")["sources"]["gstack"]["files"]) == set(original)
        invoke(config_b, ["pull"])
        invoke(config_b, ["push"])
        assert all((root_b / rel).read_bytes() == data for rel, data in original.items())

        before = config_a.read_bytes()
        preview = invoke(config_a, ["migrate-config", "--dry-run"])
        assert config_a.read_bytes() == before
        for glob in config_module._GSTACK_GENERATOR_GLOBS:
            assert f"+ {glob}" in preview
        written = invoke(config_a, ["migrate-config", "--yes"])
        assert "Run mm push to publish the exclusions" in written
        migrated = config_module.load_config(config_a)["sync"]["sources"][0]["exclude_patterns"]
        assert migrated == [
            *_PRE18_GSTACK_GLOBS72B,
            "projects/acme/private/*",
            *config_module._GSTACK_GENERATOR_GLOBS,
        ]
        rerun = invoke(config_a, ["migrate-config", "--yes"])
        assert "already up to date" in rerun
        assert "Run mm push to publish the exclusions" not in rerun
        invoke(config_a, ["push"])
        trimmed = self._remote72b(backend, "dev-a")
        assert set(trimmed["sources"]["gstack"]["files"]) == set(_KEPT_PATHS72B)
        assert trimmed["tombstones"] == {}
        assert all((root_a / rel).read_bytes() == data for rel, data in original.items())
        status = invoke(config_a, ["status"])
        assert "deleted" not in status
        assert "missing recommended" not in status

        invoke(config_b, ["pull"])
        invoke(config_b, ["push"])
        peer = self._remote72b(backend, "dev-b")
        assert set(peer["sources"]["gstack"]["files"]) == set(original)
        assert peer["tombstones"] == {}
        assert all((root_b / rel).read_bytes() == data for rel, data in original.items())
        assert not list(root_b.rglob("*sync-conflict-*"))

        divergent = "projects/acme/artifacts/x.zip"
        absent = "projects/acme/artifacts/x.tar"
        control = "projects/acme/decisions.jsonl"
        (root_b / divergent).write_bytes(b"different peer archive\n")
        (root_b / control).write_bytes(b'{"id":"base"}\n{"id":"peer"}\n')
        invoke(config_b, ["push"])
        peer = self._remote72b(backend, "dev-b")
        (root_a / absent).unlink()
        # Older divergent A bytes would conflict without the pull filter.
        self._backdate(root_a / divergent)
        excluded_keys = {
            storage_keys.blob_key("dev-b", peer["sources"]["gstack"]["files"][rel]["sha256"])
            for rel in _GENERATOR_PATHS72B
        }
        fetched = []
        real_get = LocalBackend.get

        def record_get(self, key):
            fetched.append(key)
            return real_get(self, key)

        monkeypatch.setattr(LocalBackend, "get", record_get)
        invoke(config_a, ["pull"])
        assert excluded_keys.isdisjoint(fetched)
        assert (root_a / divergent).read_bytes() == original[divergent]
        assert not (root_a / absent).exists()
        assert (root_a / control).read_bytes() == (root_b / control).read_bytes()
        assert not list(root_a.rglob("*sync-conflict-*"))
        assert self._remote72b(backend, "dev-a")["tombstones"] == {}
        assert peer["tombstones"] == {}
        assert config_module.load_config(config_b)["sync"]["sources"] == old_b_sources

    def test_generator_default_config_upgrade_autopush72b(self, tmp_path, monkeypatch):
        """An implicit source gains the new defaults without a config migration."""
        storage_dir = tmp_path / "storage"
        root_a, root_b = tmp_path / "a" / ".gstack", tmp_path / "b" / ".gstack"
        original = self._populate_generators72b(root_a)
        root_b.mkdir(parents=True)
        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")
        config_a = tmp_path / "default.toml"
        save_config(
            {
                "device": {"id": "dev-a", "name": "A"},
                "storage": {"path": str(storage_dir)},
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            config_a,
        )
        config_b = self._make_gstack_config(
            tmp_path,
            storage_dir,
            root_b,
            "dev-b",
            "B",
            exclude_patterns=list(_PRE18_GSTACK_GLOBS72B),
        )
        default = config_module.get_default_source("gstack")
        default["path"] = str(root_a)
        # Simulate the v1.8.0 default exactly: no include_patterns key yet.
        # The allowlist default has its own upgrade coverage.
        default = {k: v for k, v in default.items() if k != "include_patterns"}
        previous = {**default, "exclude_patterns": list(_PRE18_GSTACK_GLOBS72B)}
        TestMigrateConfigCommand()._redirect_lock(monkeypatch, tmp_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        with monkeypatch.context() as old_defaults:
            old_defaults.setattr(config_module, "DEFAULT_SOURCES", [previous])
            old_defaults.setattr(cli_module, "DEFAULT_SOURCES", [previous])
            self._activate(monkeypatch, config_a)
            result = runner.invoke(app, ["push"])
            assert result.exit_code == 0, result.output
            assert set(self._remote72b(backend, "dev-a")["sources"]["gstack"]["files"]) == set(
                original
            )
            self._activate(monkeypatch, config_b)
            result = runner.invoke(app, ["pull"])
            assert result.exit_code == 0, result.output

        monkeypatch.setattr(config_module, "DEFAULT_SOURCES", [default])
        monkeypatch.setattr(cli_module, "DEFAULT_SOURCES", [default])
        self._activate(monkeypatch, config_a)
        before = config_a.read_bytes()
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0, result.output
        trimmed = self._remote72b(backend, "dev-a")
        assert set(trimmed["sources"]["gstack"]["files"]) == set(_KEPT_PATHS72B)
        assert trimmed["tombstones"] == {}
        assert config_a.read_bytes() == before
        self._activate(monkeypatch, config_b)
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 0, result.output
        assert all((root_b / rel).read_bytes() == data for rel, data in original.items())
        assert not list(root_b.rglob("*sync-conflict-*"))
        self._activate(monkeypatch, config_a)
        result = runner.invoke(app, ["migrate-config"])
        assert result.exit_code == 0, result.output
        assert "nothing to migrate" in result.output
        assert "Run mm push to publish the exclusions" not in result.output

    def test_two_device_first_pull_no_conflicts_for_excluded_paths(self, tmp_path, monkeypatch):
        """2026-04-24 first-pull: A pushes per-machine artifacts WITHOUT the
        recommended excludes; B pulls WITH the excludes installed. Pull
        MUST emit zero `.sync-conflict-*` files for repo-mode.json or
        land-deploy-confirmed (the per-machine churn paths).

        Test deliberately backdates B's local files so the
        without-filter scenario WOULD have produced a conflict — the
        assertion proves the filter is what prevents it.
        """
        storage_dir = tmp_path / "storage"
        gstack_a = tmp_path / "machine_a" / ".gstack"
        gstack_b = tmp_path / "machine_b" / ".gstack"
        self._populate_gstack(gstack_a, repo_mode="solo-A")
        self._populate_gstack(gstack_b, repo_mode="collaborative-B")

        # Make B's local strictly older than A's about-to-be-pushed manifest.
        # Without this, mtime-skip might mask the conflict scenario and
        # the test would falsely pass without the exclude filter.
        proj_b = gstack_b / "projects" / "myapp"
        self._backdate(
            proj_b / "repo-mode.json",
            proj_b / "land-deploy-confirmed",
        )

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = self._make_gstack_config(
            tmp_path, storage_dir, gstack_a, "dev-a", "A", exclude_patterns=None
        )
        config_b = self._make_gstack_config(
            tmp_path,
            storage_dir,
            gstack_b,
            "dev-b",
            "B",
            exclude_patterns=[
                "projects/*/repo-mode.json",
                "projects/*/land-deploy-confirmed",
            ],
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        self._activate(monkeypatch, config_a)
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        self._activate(monkeypatch, config_b)
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 0, result.output

        repo_mode_conflicts = list(proj_b.glob("repo-mode.sync-conflict-*.json"))
        ldc_conflicts = list(proj_b.glob("land-deploy-confirmed.sync-conflict-*"))
        assert repo_mode_conflicts == [], (
            f"Excluded repo-mode.json must NOT produce a conflict copy; got {repo_mode_conflicts}"
        )
        assert ldc_conflicts == [], (
            f"Excluded land-deploy-confirmed must NOT produce a conflict copy; got {ldc_conflicts}"
        )
        # Untouched local content survives.
        assert (proj_b / "repo-mode.json").read_text() == '{"mode": "collaborative-B"}'
        assert (proj_b / "land-deploy-confirmed").read_text() == "OK"
        # role.md (not in exclude_patterns) does sync.
        assert (proj_b / "role.md").exists()

    def test_tombstone_not_emitted_on_exclude_transition(self, tmp_path, monkeypatch):
        """File previously synced; user adds the glob; next push must NOT
        emit a deletion tombstone for the now-excluded path."""
        storage_dir = tmp_path / "storage"
        gstack = tmp_path / ".gstack"
        self._populate_gstack(gstack)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")

        # Phase 1: push without excludes — repo-mode.json lands in remote manifest.
        config_path = self._make_gstack_config(
            tmp_path, storage_dir, gstack, "dev-a", "A", exclude_patterns=None
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        assert runner.invoke(app, ["push"]).exit_code == 0
        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        assert "projects/myapp/repo-mode.json" in remote["sources"]["gstack"]["files"]

        # Phase 2: install the exclude, push again. File still exists on
        # disk locally (would normally walk into the manifest if not excluded).
        self._make_gstack_config(
            tmp_path,
            storage_dir,
            gstack,
            "dev-a",
            "A",
            exclude_patterns=["projects/*/repo-mode.json"],
        )
        # Touch role.md so push has at least one diff to upload (otherwise
        # _push_core may early-exit on "nothing to push" before manifest write).
        (gstack / "projects" / "myapp" / "role.md").write_text("data scientist v2")
        assert runner.invoke(app, ["push"]).exit_code == 0

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        offending = [k for k in remote.get("tombstones", {}) if "repo-mode.json" in k]
        assert offending == [], f"Excluded path must NOT generate a tombstone; got {offending}"
        # And the file is dropped from sources.gstack.files (walker filtered it).
        assert "projects/myapp/repo-mode.json" not in remote["sources"]["gstack"]["files"]

    def test_marker_skip_does_not_emit_tombstone(self, tmp_path, monkeypatch):
        """A `.extend-root` skip must not generate deletion tombstones,
        exactly as adding a glob must not."""
        storage_dir = tmp_path / "storage"
        root = tmp_path / "codex"
        skill = root / "skills" / "new-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("generated v1")
        (root / "skills" / "keep").mkdir(parents=True)
        (root / "skills" / "keep" / "SKILL.md").write_text("hand-authored")

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        config_path = tmp_path / "config_dev-a.toml"
        save_config(
            {
                "device": {"id": "dev-a", "name": "A"},
                "storage": {"path": str(storage_dir)},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [
                        {
                            "name": "codex",
                            "path": str(root),
                            "type": "generic",
                            "include_dirs": ["skills"],
                        }
                    ],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            config_path,
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        assert runner.invoke(app, ["push"]).exit_code == 0
        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        assert "skills/new-skill/SKILL.md" in remote["sources"]["codex"]["files"]

        (skill / ".extend-root").write_text("gstack-extend")
        (root / "skills" / "keep" / "SKILL.md").write_text("hand-authored v2")
        assert runner.invoke(app, ["push"]).exit_code == 0
        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        offending = [k for k in remote.get("tombstones", {}) if "new-skill" in k]
        assert offending == [], f"marker skip must not tombstone; got {offending}"
        assert "skills/new-skill/SKILL.md" not in remote["sources"]["codex"]["files"]
        assert "skills/keep/SKILL.md" in remote["sources"]["codex"]["files"]

    def test_no_spurious_tombstone_on_unexclude_transition(self, tmp_path, monkeypatch):
        """Removing a glob brings the path back into sync as new — push
        must record it as a fresh entry, NOT as a tombstone."""
        storage_dir = tmp_path / "storage"
        gstack = tmp_path / ".gstack"
        self._populate_gstack(gstack)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")

        config_path = self._make_gstack_config(
            tmp_path,
            storage_dir,
            gstack,
            "dev-a",
            "A",
            exclude_patterns=["projects/*/repo-mode.json"],
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        assert runner.invoke(app, ["push"]).exit_code == 0

        # Phase 2: remove the exclude, push.
        self._make_gstack_config(tmp_path, storage_dir, gstack, "dev-a", "A", exclude_patterns=None)
        assert runner.invoke(app, ["push"]).exit_code == 0

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        offending = [k for k in remote.get("tombstones", {}) if "repo-mode.json" in k]
        assert offending == [], f"Removing an exclude must not surface a tombstone; got {offending}"
        assert "projects/myapp/repo-mode.json" in remote["sources"]["gstack"]["files"]

    def test_sidecar_bypass_guard(self, tmp_path, monkeypatch):
        """Corrupt own manifest → recovery via sidecar (which contains the
        pre-exclude path) → consumer-boundary filter applies before
        generate_tombstones → NO spurious tombstones for excluded paths.

        Pins codex-2 #2: without the consumer-boundary filter on the
        sidecar prior state, push silently re-tombstones every newly-
        excluded path on the first post-corruption recovery.
        """
        storage_dir = tmp_path / "storage"
        gstack = tmp_path / ".gstack"
        self._populate_gstack(gstack)

        # Redirect sidecar so the test is hermetic.
        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")

        # Phase 1: push without excludes; sidecar captures the file.
        config_path = self._make_gstack_config(
            tmp_path, storage_dir, gstack, "dev-a", "A", exclude_patterns=None
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        assert runner.invoke(app, ["push"]).exit_code == 0
        sidecar_path = sidecar_dir / "last-push.json"
        assert sidecar_path.exists()
        sidecar_content = json.loads(sidecar_path.read_text())
        assert "projects/myapp/repo-mode.json" in sidecar_content["sources"]["gstack"]["files"]

        # Corrupt the storage manifest so push recovery falls through to sidecar.
        manifest_storage_path = storage_dir / "manifests" / "dev-a" / "manifest.json.enc"
        manifest_storage_path.write_bytes(b"\x00\x01\x02 not a manifest")

        # Phase 2: install excludes, push (recovery via sidecar fires).
        self._make_gstack_config(
            tmp_path,
            storage_dir,
            gstack,
            "dev-a",
            "A",
            exclude_patterns=["projects/*/repo-mode.json"],
        )
        # Touch role.md so push has work to do beyond manifest rewrite.
        (gstack / "projects" / "myapp" / "role.md").write_text("v2")
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        offending = [k for k in remote.get("tombstones", {}) if "repo-mode.json" in k]
        assert offending == [], (
            f"Sidecar recovery bypassed the exclude filter: spurious tombstone(s) {offending}"
        )

    def test_mm_gc_does_not_orphan_excluded_path_blobs(self, tmp_path, monkeypatch):
        """Peers stay untouched; own raw references survive a newly added exclude."""
        storage_dir = tmp_path / "storage"
        gstack_a = tmp_path / "a" / ".gstack"
        gstack_b = tmp_path / "b" / ".gstack"
        self._populate_gstack(gstack_a, repo_mode="A")
        gstack_b.mkdir(parents=True)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = self._make_gstack_config(
            tmp_path, storage_dir, gstack_a, "dev-a", "A", exclude_patterns=None
        )
        config_b = self._make_gstack_config(
            tmp_path,
            storage_dir,
            gstack_b,
            "dev-b",
            "B",
            exclude_patterns=["projects/*/repo-mode.json"],
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        self._activate(monkeypatch, config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        repo_mode_sha = remote["sources"]["gstack"]["files"]["projects/myapp/repo-mode.json"][
            "sha256"
        ]
        blob_path = storage_dir / "data" / "dev-a" / f"{repo_mode_sha}.enc"
        assert blob_path.exists(), "Setup: blob must exist before gc"

        # B (with excludes) runs gc.
        self._activate(monkeypatch, config_b)
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output

        assert blob_path.exists(), "mm gc deleted a peer blob outside its namespace"
        own_config = tomllib.loads(config_a.read_text())
        own_config["sync"]["sources"][0]["exclude_patterns"] = ["projects/*/repo-mode.json"]
        save_config(own_config, config_a)
        self._activate(monkeypatch, config_a)
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output
        assert blob_path.exists(), "GC filtered its own manifest and orphaned an excluded reference"


class TestIncludePatternsAllowlist:
    """Allowlist narrowing must behave like exclusion, never like deletion.

    Reasoning mirrors TestExcludePatterns5C: the consumer-boundary inverse
    filter (`_filter_excluded_paths` + `_build_include_map`) strips
    de-scoped entries from prior/peer manifests before tombstone
    generation, so adding `include_patterns` mints no tombstones and no
    peer loses local files. A peer with no allowlist configured stands in
    for a pre-allowlist build: its filter inputs are empty either way.
    """

    PATTERNS = [
        "projects/*/checkpoints/**",
        "projects/*/learnings.jsonl",
        "projects/*/pair-review/*",
    ]
    KEPT = {
        "projects/demo/checkpoints/20261022-save.md": "ctx",
        "projects/demo/learnings.jsonl": "{}\n",
        "projects/demo/pair-review/report.md": "report",
    }
    DROPPED = {
        "projects/demo/review-and-prep/run-1/scratch.md": "junk",
        "projects/demo/pair-review/branches/b/report.md": "nested",
        "projects/demo/pair-review/session.yaml": "live",
        "projects/demo/loop-1.json": "{}",
    }

    def _config(self, tmp_path, storage_dir, root, device_id, name, *, include=None):
        src: dict = {
            "name": "gstack",
            "path": str(root),
            "type": "generic",
            "include_dirs": ["projects"],
            "exclude_patterns": ["projects/*/pair-review/session.yaml"],
        }
        if include is not None:
            src["include_patterns"] = include
        config = {
            "device": {"id": device_id, "name": name},
            "storage": {"path": str(storage_dir)},
            "sync": {"max_file_size": 52_428_800, "sources": [src]},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        path = tmp_path / f"config_{device_id}.toml"
        save_config(config, path)
        return path

    def _populate(self, root):
        for rel, data in {**self.KEPT, **self.DROPPED}.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data)

    def _setup_fleet(self, tmp_path, monkeypatch):
        storage = tmp_path / "storage"
        root_a, root_b = tmp_path / "a" / ".gstack", tmp_path / "b" / ".gstack"
        self._populate(root_a)
        root_b.mkdir(parents=True)
        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        return backend, root_a, root_b

    def _invoke(self, monkeypatch, config, args):
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config)
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        return result

    def _remote(self, backend, device):
        return load_manifest(
            decrypt(backend.get(storage_keys.manifest_key(device)), PASSPHRASE, memory_kb=MEMORY_KB)
        )

    def test_narrowing_mints_no_tombstones_and_peer_keeps_files(self, tmp_path, monkeypatch):
        backend, root_a, root_b = self._setup_fleet(tmp_path, monkeypatch)
        storage = tmp_path / "storage"
        config_a = self._config(tmp_path, storage, root_a, "dev-a", "A")
        config_b = self._config(tmp_path, storage, root_b, "dev-b", "B")

        self._invoke(monkeypatch, config_a, ["push"])
        fat = self._remote(backend, "dev-a")["sources"]["gstack"]["files"]
        assert set(self.KEPT) | set(self.DROPPED) - {
            "projects/demo/pair-review/session.yaml"
        } <= set(fat)
        self._invoke(monkeypatch, config_b, ["pull"])
        for rel in set(self.KEPT) | set(self.DROPPED):
            if rel.endswith("session.yaml"):
                continue
            assert (root_b / rel).exists(), rel

        # A adopts the allowlist and pushes: slim manifest, no tombstones
        # for de-scoped paths, and B (no allowlist — the old-build stand-in)
        # loses nothing on pull.
        config_a = self._config(tmp_path, storage, root_a, "dev-a", "A", include=self.PATTERNS)
        self._invoke(monkeypatch, config_a, ["push"])
        slim_manifest = self._remote(backend, "dev-a")
        assert set(slim_manifest["sources"]["gstack"]["files"]) == set(self.KEPT)
        tombstoned = {k.partition(":")[2] for k in slim_manifest.get("tombstones", {})}
        assert not (tombstoned & set(self.DROPPED)), tombstoned

        self._invoke(monkeypatch, config_b, ["pull"])
        for rel in self.DROPPED:
            if rel.endswith("session.yaml"):
                continue
            assert (root_b / rel).exists(), f"peer lost local file {rel}"

        # A's local files are untouched too; de-scoped files stay on disk.
        for rel in self.DROPPED:
            assert (root_a / rel).exists(), rel

    def test_fresh_allowlist_machine_pulls_only_kept_from_fat_peer(self, tmp_path, monkeypatch):
        backend, root_a, root_b = self._setup_fleet(tmp_path, monkeypatch)
        storage = tmp_path / "storage"
        config_a = self._config(tmp_path, storage, root_a, "dev-a", "A")
        config_b = self._config(tmp_path, storage, root_b, "dev-b", "B", include=self.PATTERNS)

        self._invoke(monkeypatch, config_a, ["push"])
        self._invoke(monkeypatch, config_b, ["pull"])
        for rel in self.KEPT:
            assert (root_b / rel).exists(), rel
        for rel in self.DROPPED:
            assert not (root_b / rel).exists(), rel

        # B pushes slim, then deletes a kept file's de-scoped sibling locally
        # and pulls again: the fat peer manifest must not re-apply it.
        self._invoke(monkeypatch, config_b, ["push"])
        self._invoke(monkeypatch, config_b, ["pull"])
        assert not (root_b / "projects/demo/review-and-prep/run-1/scratch.md").exists()

    def test_widening_allowlist_brings_files_back_as_new(self, tmp_path, monkeypatch):
        backend, root_a, _root_b = self._setup_fleet(tmp_path, monkeypatch)
        storage = tmp_path / "storage"
        config_a = self._config(tmp_path, storage, root_a, "dev-a", "A", include=self.PATTERNS)
        self._invoke(monkeypatch, config_a, ["push"])
        slim = self._remote(backend, "dev-a")
        assert "projects/demo/loop-1.json" not in slim["sources"]["gstack"]["files"]

        widened = [*self.PATTERNS, "projects/*/loop-*.json"]
        config_a = self._config(tmp_path, storage, root_a, "dev-a", "A", include=widened)
        self._invoke(monkeypatch, config_a, ["push"])
        wide = self._remote(backend, "dev-a")
        assert "projects/demo/loop-1.json" in wide["sources"]["gstack"]["files"]

    def test_mm_gc_does_not_orphan_narrowed_blobs_before_push(self, tmp_path, monkeypatch):
        """mm gc must read the raw published manifest, not _filter_excluded_paths,
        so narrowing include_patterns in config.toml before pushing never deletes
        blobs still referenced by the stored manifest."""
        backend, root_a, _root_b = self._setup_fleet(tmp_path, monkeypatch)
        storage = tmp_path / "storage"
        config_a = self._config(tmp_path, storage, root_a, "dev-a", "A")
        self._invoke(monkeypatch, config_a, ["push"])

        fat = self._remote(backend, "dev-a")
        dropped_rel = "projects/demo/review-and-prep/run-1/scratch.md"
        dropped_sha = fat["sources"]["gstack"]["files"][dropped_rel]["sha256"]
        blob_path = storage / "data" / "dev-a" / f"{dropped_sha}.enc"
        assert blob_path.exists()

        config_a = self._config(tmp_path, storage, root_a, "dev-a", "A", include=self.PATTERNS)
        self._invoke(monkeypatch, config_a, ["gc"])
        assert blob_path.exists(), "mm gc filtered own manifest through include_patterns"


class TestFilterExcludedPathsHelper:
    """Track 5C: direct unit coverage of _filter_excluded_paths so the
    consumer-boundary contract is provable without a full push/pull
    round-trip."""

    def test_no_op_when_exclude_map_empty(self):
        from mind_meld.cli import _filter_excluded_paths

        m = {
            "sources": {"gstack": {"files": {"projects/x/repo-mode.json": {"sha256": "a"}}}},
            "tombstones": {},
        }
        assert _filter_excluded_paths(m, {}) is m

    def test_strips_excluded_files_from_sources(self):
        from mind_meld.cli import _filter_excluded_paths

        m = {
            "sources": {
                "gstack": {
                    "base_path": "/x",
                    "files": {
                        "projects/myapp/repo-mode.json": {"sha256": "a"},
                        "projects/myapp/role.md": {"sha256": "b"},
                    },
                }
            },
            "tombstones": {},
        }
        out = _filter_excluded_paths(m, {"gstack": ["projects/*/repo-mode.json"]})
        kept = out["sources"]["gstack"]["files"]
        assert "projects/myapp/role.md" in kept
        assert "projects/myapp/repo-mode.json" not in kept
        # Original is untouched (returned a copy).
        assert "projects/myapp/repo-mode.json" in m["sources"]["gstack"]["files"]

    def test_strips_tombstones_for_excluded_paths(self):
        from mind_meld.cli import _filter_excluded_paths

        m = {
            "sources": {"gstack": {"files": {}}},
            "tombstones": {
                "gstack:projects/myapp/repo-mode.json": {"deleted_at": "2026-04-25T00:00:00+00:00"},
                "gstack:projects/myapp/role.md": {"deleted_at": "2026-04-25T00:00:00+00:00"},
            },
        }
        out = _filter_excluded_paths(m, {"gstack": ["projects/*/repo-mode.json"]})
        keys = list(out["tombstones"])
        assert "gstack:projects/myapp/role.md" in keys
        assert "gstack:projects/myapp/repo-mode.json" not in keys

    def test_legacy_bare_path_tombstones_default_to_claude_source(self):
        """Pre-v0.4 manifests had bare-path tombstone keys. Filter
        treats them as `claude` source per `normalize_manifest`'s rule."""
        from mind_meld.cli import _filter_excluded_paths

        m = {
            "sources": {"claude": {"files": {}}},
            "tombstones": {
                "projects/-foo/memory/role.md": {"deleted_at": "2026-04-25T00:00:00+00:00"},
            },
        }
        out = _filter_excluded_paths(m, {"claude": ["projects/*/memory/role.md"]})
        assert out["tombstones"] == {}

    def test_unaffected_sources_pass_through_unchanged(self):
        from mind_meld.cli import _filter_excluded_paths

        m = {
            "sources": {
                "gstack": {"files": {"projects/myapp/repo-mode.json": {"sha256": "a"}}},
                "claude": {"files": {"projects/-foo/memory/role.md": {"sha256": "b"}}},
            },
            "tombstones": {},
        }
        out = _filter_excluded_paths(m, {"gstack": ["projects/*/repo-mode.json"]})
        # claude source untouched.
        assert out["sources"]["claude"]["files"]["projects/-foo/memory/role.md"]["sha256"] == "b"
        # gstack source filtered.
        assert "projects/myapp/repo-mode.json" not in out["sources"]["gstack"]["files"]

    def test_strips_conflict_shaped_rel_paths_even_with_empty_map(self):
        """E1: a peer-chosen conflict-shaped name is rejected on pull."""
        from mind_meld.cli import _filter_excluded_paths

        m = {
            "sources": {
                "gstack": {
                    "files": {
                        "foo.sync-conflict-19700101-000000-deadbeef.md": {"sha256": "a"},
                        "foo.md": {"sha256": "b"},
                    }
                }
            },
            "tombstones": {},
        }
        out = _filter_excluded_paths(m, {})
        assert out is not m
        kept = out["sources"]["gstack"]["files"]
        assert "foo.md" in kept
        assert "foo.sync-conflict-19700101-000000-deadbeef.md" not in kept

    def test_strips_conflict_shaped_tombstones_even_with_empty_map(self):
        from mind_meld.cli import _filter_excluded_paths

        m = {
            "sources": {"gstack": {"files": {"foo.md": {"sha256": "b"}}}},
            "tombstones": {
                "gstack:foo.sync-conflict-19700101-000000-deadbeef.md": {
                    "deleted_at": "2026-01-01T00:00:00+00:00"
                },
                "gstack:foo.md": {"deleted_at": "2026-01-01T00:00:00+00:00"},
            },
        }
        out = _filter_excluded_paths(m, {})
        assert out is not m
        assert "gstack:foo.md" in out["tombstones"]
        assert "gstack:foo.sync-conflict-19700101-000000-deadbeef.md" not in out["tombstones"]

    def test_strips_extend_root_basename_even_with_empty_map(self):
        from mind_meld.cli import _filter_excluded_paths

        m = {
            "sources": {
                "codex": {
                    "files": {
                        "skills/.extend-root": {"sha256": "a"},
                        "skills/keep/SKILL.md": {"sha256": "b"},
                    }
                }
            },
            "tombstones": {},
        }
        out = _filter_excluded_paths(m, {})
        kept = out["sources"]["codex"]["files"]
        assert "skills/keep/SKILL.md" in kept
        assert "skills/.extend-root" not in kept


class TestDisabledSourcesTombstoneSuppression:
    """v0.10.0 IRON RULE regression pins for [sync].disabled_sources.

    Mirror of TestExcludePatterns5C — same shape, different filter target.
    The exclude_patterns invariant stripped per-path entries; this strips
    whole sources. Both apply at the consumer boundary (`_push_core`,
    `_pull_core`) and MUST NOT apply at `_fetch_remote_manifest` (mm gc
    reads raw manifests there).

    Failing these tests blocks ship: a regression here mints spurious
    tombstones that freeze restoration and propagation of a missing path
    across upgraded and stale peers for 30 days. Existing local bytes are
    never removed.
    """

    def _make_two_source_config(
        self,
        tmp_path,
        storage_dir,
        claude_dir,
        gstack_dir,
        device_id,
        device_name,
        *,
        disabled_sources=None,
    ):
        config_path = tmp_path / f"config_{device_id}.toml"
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_dir), "type": "claude"},
                    {
                        "name": "gstack",
                        "path": str(gstack_dir),
                        "type": "generic",
                        "include_dirs": ["projects"],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        if disabled_sources is not None:
            config["sync"]["disabled_sources"] = disabled_sources
        save_config(config, config_path)
        return config_path

    def _populate_claude(self, claude_dir):
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "user_role.md").write_text("---\nname: role\n---\nData scientist")

    def _populate_gstack(self, gstack_dir):
        proj = gstack_dir / "projects" / "myapp"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "role.md").write_text("data scientist")
        (proj / "feedback.md").write_text("no mocks")

    def _activate(self, monkeypatch, config_path):
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

    def _bootstrap(self, storage_dir):
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        return backend

    def test_disable_source_does_not_generate_tombstones_on_next_push(self, tmp_path, monkeypatch):
        """P0 propagation-freeze prevention: disable gstack on machine A
        and push. The new manifest must NOT contain deletion tombstones
        for any gstack file. Without the consumer-boundary filter on the
        prior_manifest, generate_tombstones would emit a tombstone per
        gstack file (because the local-walked manifest no longer has
        gstack via get_sources's filter). Spurious tombstones freeze
        restoration and propagation for 30 days; existing local bytes
        are never removed.
        """
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"
        self._populate_claude(claude_dir)
        self._populate_gstack(gstack_dir)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")

        # Phase 1: push without disable — both sources land in remote manifest.
        config_path = self._make_two_source_config(
            tmp_path, storage_dir, claude_dir, gstack_dir, "dev-a", "A"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        assert runner.invoke(app, ["push"]).exit_code == 0
        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        assert "projects/myapp/role.md" in remote["sources"]["gstack"]["files"]

        # Phase 2: disable gstack. Push.
        self._make_two_source_config(
            tmp_path,
            storage_dir,
            claude_dir,
            gstack_dir,
            "dev-a",
            "A",
            disabled_sources=["gstack"],
        )
        # Touch claude file so push has a diff to upload.
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md").write_text(
            "Data scientist v2"
        )
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        gstack_tombstones = [k for k in remote.get("tombstones", {}) if k.startswith("gstack:")]
        assert gstack_tombstones == [], (
            f"Disabling a source must NOT generate tombstones for that source's "
            f"files (would propagate fleet-wide deletion); got {gstack_tombstones}"
        )

    def test_enable_previously_disabled_source_brings_files_back_as_new(
        self, tmp_path, monkeypatch
    ):
        """Re-enable round-trip: disable gstack, push, enable gstack, push.
        Files reappear as fresh entries in the manifest, NOT as tombstones."""
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"
        self._populate_claude(claude_dir)
        self._populate_gstack(gstack_dir)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")

        # Phase 1: disable gstack, push.
        config_path = self._make_two_source_config(
            tmp_path,
            storage_dir,
            claude_dir,
            gstack_dir,
            "dev-a",
            "A",
            disabled_sources=["gstack"],
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        assert runner.invoke(app, ["push"]).exit_code == 0

        # Phase 2: re-enable gstack, push.
        self._make_two_source_config(tmp_path, storage_dir, claude_dir, gstack_dir, "dev-a", "A")
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md").write_text(
            "Data scientist v3"
        )
        assert runner.invoke(app, ["push"]).exit_code == 0

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        # gstack content reappears as fresh entries.
        assert "projects/myapp/role.md" in remote["sources"]["gstack"]["files"]
        # And no tombstones for the previously-disabled source.
        gstack_tombstones = [k for k in remote.get("tombstones", {}) if k.startswith("gstack:")]
        assert gstack_tombstones == []

    def test_pull_skips_disabled_source_peer_manifest_entries(self, tmp_path, monkeypatch):
        """A pushes both sources. B has gstack disabled. B's pull must
        NOT land any gstack files locally. Without the consumer-boundary
        filter on peer manifests in `_pull_core`, B would pull A's gstack
        content despite the local disable."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "a" / ".claude"
        gstack_a = tmp_path / "a" / ".gstack"
        claude_b = tmp_path / "b" / ".claude"
        gstack_b = tmp_path / "b" / ".gstack"
        self._populate_claude(claude_a)
        self._populate_gstack(gstack_a)
        claude_b.mkdir(parents=True)
        gstack_b.mkdir(parents=True)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = self._make_two_source_config(
            tmp_path, storage_dir, claude_a, gstack_a, "dev-a", "A"
        )
        config_b = self._make_two_source_config(
            tmp_path,
            storage_dir,
            claude_b,
            gstack_b,
            "dev-b",
            "B",
            disabled_sources=["gstack"],
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        self._activate(monkeypatch, config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0

        self._activate(monkeypatch, config_b)
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 0, result.output

        # B's gstack dir stays empty — A's content didn't land.
        landed = (
            list((gstack_b / "projects").glob("*/role.md"))
            if (gstack_b / "projects").exists()
            else []
        )
        assert landed == [], f"Disabled source must not pull peer content; got {landed}"
        # Claude content DID land (not disabled).
        claude_landed = list(claude_b.rglob("user_role.md"))
        assert claude_landed, "Non-disabled claude content should still pull"

    def test_sidecar_recovery_filters_disabled_sources(self, tmp_path, monkeypatch):
        """Corrupt own manifest → recovery via sidecar (which contains the
        pre-disable gstack entries) → consumer-boundary filter applies
        before generate_tombstones → NO spurious tombstones for disabled-
        source paths.

        Same shape as test_sidecar_bypass_guard for exclude_patterns.
        """
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"
        self._populate_claude(claude_dir)
        self._populate_gstack(gstack_dir)

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")

        # Phase 1: push with both sources; sidecar captures gstack.
        config_path = self._make_two_source_config(
            tmp_path, storage_dir, claude_dir, gstack_dir, "dev-a", "A"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        assert runner.invoke(app, ["push"]).exit_code == 0
        sidecar_path = sidecar_dir / "last-push.json"
        assert sidecar_path.exists()
        sidecar_content = json.loads(sidecar_path.read_text())
        assert "gstack" in sidecar_content["sources"]

        # Corrupt the storage manifest so push falls through to sidecar.
        manifest_storage_path = storage_dir / "manifests" / "dev-a" / "manifest.json.enc"
        manifest_storage_path.write_bytes(b"\x00\x01\x02 not a manifest")

        # Phase 2: disable gstack, push (recovery via sidecar fires).
        self._make_two_source_config(
            tmp_path,
            storage_dir,
            claude_dir,
            gstack_dir,
            "dev-a",
            "A",
            disabled_sources=["gstack"],
        )
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md").write_text(
            "Data scientist v2"
        )
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        gstack_tombstones = [k for k in remote.get("tombstones", {}) if k.startswith("gstack:")]
        assert gstack_tombstones == [], (
            f"Sidecar recovery bypassed the disabled-source filter: "
            f"spurious tombstone(s) {gstack_tombstones}"
        )

    def test_mm_gc_does_not_orphan_disabled_source_blobs(self, tmp_path, monkeypatch):
        """Peers stay untouched; disabling an own source cannot filter GC references."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "a" / ".claude"
        gstack_a = tmp_path / "a" / ".gstack"
        claude_b = tmp_path / "b" / ".claude"
        gstack_b = tmp_path / "b" / ".gstack"
        self._populate_claude(claude_a)
        self._populate_gstack(gstack_a)
        claude_b.mkdir(parents=True)
        gstack_b.mkdir(parents=True)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = self._make_two_source_config(
            tmp_path, storage_dir, claude_a, gstack_a, "dev-a", "A"
        )
        config_b = self._make_two_source_config(
            tmp_path,
            storage_dir,
            claude_b,
            gstack_b,
            "dev-b",
            "B",
            disabled_sources=["gstack"],
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        self._activate(monkeypatch, config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0

        # Locate a gstack blob A pushed.
        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        role_sha = remote["sources"]["gstack"]["files"]["projects/myapp/role.md"]["sha256"]
        blob_path = storage_dir / "data" / "dev-a" / f"{role_sha}.enc"
        assert blob_path.exists(), "Setup: gstack blob must exist before gc"

        # B (with gstack disabled) runs gc.
        self._activate(monkeypatch, config_b)
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output

        assert blob_path.exists(), "mm gc deleted a peer blob outside its namespace"
        own_config = tomllib.loads(config_a.read_text())
        own_config["sync"]["disabled_sources"] = ["gstack"]
        save_config(own_config, config_a)
        self._activate(monkeypatch, config_a)
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output
        assert blob_path.exists(), "GC filtered its own manifest and orphaned a disabled reference"

    def test_retired_source_emits_no_tombstones_across_the_transition(self, tmp_path, monkeypatch):
        """Track 37B: a prior manifest that still has sources.opencode must
        not mint new opencode:* tombstones on the first post-retirement
        push. The consumer-boundary filter keys on disabled_sources, so
        the post-migration config writes that name even after the
        [[sync.sources]] block is gone.

        Failing this blocks ship.
        """
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        opencode_dir = tmp_path / "opencode-root"
        self._populate_claude(claude_dir)
        skill = opencode_dir / "skills" / "my-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("hello from opencode")

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")

        config_path = tmp_path / "config_dev-a.toml"
        config = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_dir), "type": "claude"},
                    {
                        "name": "opencode",
                        "path": str(opencode_dir),
                        "type": "generic",
                        "include_dirs": ["skills"],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        # The test drives the post-retirement config itself; do not let
        # the interactive migration prompt rewrite it mid-setup.
        monkeypatch.setattr(cli_module, "_maybe_prompt_migration", lambda _config, **_kw: None)
        assert runner.invoke(app, ["push"]).exit_code == 0

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        prior = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        # Non-vacuity guard: the seeded prior must actually contain the file.
        assert "skills/my-skill/SKILL.md" in prior["sources"]["opencode"]["files"]

        prior.setdefault("tombstones", {})["opencode:skills/gone/SKILL.md"] = {
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "device_id": "dev-a",
        }
        backend.put(
            "manifests/dev-a/manifest.json.enc",
            encrypt(serialize_manifest(prior), PASSPHRASE, memory_kb=MEMORY_KB),
        )

        config["sync"]["sources"] = [
            {"name": "claude", "path": str(claude_dir), "type": "claude"},
        ]
        config["sync"]["disabled_sources"] = ["opencode"]
        save_config(config, config_path)
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "user_role.md").write_text(
            "Data scientist after retirement"
        )
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        enc = backend.get("manifests/dev-a/manifest.json.enc")
        remote = load_manifest(decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB))
        assert "opencode" not in remote.get("sources", {})
        new_tombstones = [
            k
            for k in remote.get("tombstones", {})
            if k.startswith("opencode:") and k != "opencode:skills/gone/SKILL.md"
        ]
        assert new_tombstones == [], (
            f"Retiring opencode must not mint new tombstones; got {new_tombstones}"
        )
        assert "opencode:skills/gone/SKILL.md" in remote.get("tombstones", {})

    def test_legacy_peer_opencode_section_is_not_pulled(self, tmp_path, monkeypatch):
        """Mixed fleet: a peer still publishing sources.opencode must not
        land files under the local opencode path, and pull must not error."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "a" / ".claude"
        opencode_a = tmp_path / "a" / "opencode-root"
        claude_b = tmp_path / "b" / ".claude"
        opencode_b = tmp_path / "b" / "opencode-root"
        self._populate_claude(claude_a)
        skill_b = opencode_b / "skills" / "peer-skill"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text("peer opencode file")
        claude_b.mkdir(parents=True)
        opencode_a.mkdir(parents=True)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_b = {
            "device": {"id": "dev-b", "name": "B"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_a), "type": "claude"},
                    {
                        "name": "opencode",
                        "path": str(opencode_b),
                        "type": "generic",
                        "include_dirs": ["skills"],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        config_path_b = tmp_path / "config_dev-b.toml"
        save_config(config_b, config_path_b)

        config_a = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_b), "type": "claude"},
                ],
                "disabled_sources": ["opencode"],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        config_path_a = tmp_path / "config_dev-a.toml"
        save_config(config_a, config_path_a)

        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        monkeypatch.setattr(cli_module, "_maybe_prompt_migration", lambda _config, **_kw: None)
        self._activate(monkeypatch, config_path_b)
        assert runner.invoke(app, ["push"]).exit_code == 0

        self._activate(monkeypatch, config_path_a)
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 0, result.output
        landed = list(opencode_a.rglob("SKILL.md")) if opencode_a.exists() else []
        assert landed == [], f"Retired source must not pull peer content; got {landed}"
        claude_landed = list(claude_b.rglob("user_role.md"))
        assert claude_landed, "Non-retired claude content should still pull"


class TestMigrateConfigCommand:
    """Track 5C: mm migrate-config adds missing recommended excludes,
    is idempotent, and preserves user-customized fields."""

    def _write_explicit_config(self, tmp_path, *, exclude_patterns=None, include_patterns=None):
        config_path = tmp_path / "config.toml"
        gstack_src: dict = {
            "name": "gstack",
            "path": str(tmp_path / ".gstack"),
            "type": "generic",
            "include_dirs": ["projects"],
        }
        if exclude_patterns is not None:
            gstack_src["exclude_patterns"] = exclude_patterns
        if include_patterns is not None:
            gstack_src["include_patterns"] = include_patterns
        config = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"max_file_size": 52_428_800, "sources": [gstack_src]},
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        return config_path

    def _redirect_lock(self, monkeypatch, tmp_path):
        lp = tmp_path / "test.lock"
        monkeypatch.setattr("mind_meld.config.LOCK_PATH", lp)
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", lp)

    def test_idempotent_on_already_migrated_config(self, tmp_path, monkeypatch):
        """Running migrate-config twice is a no-op the second time."""
        from mind_meld.config import DEFAULT_SOURCES

        gstack_default = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        config_path = self._write_explicit_config(
            tmp_path,
            exclude_patterns=list(gstack_default["exclude_patterns"]),
            include_patterns=list(gstack_default["include_patterns"]),
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output
        assert "already up to date" in result.output

    def test_adds_recommended_include_patterns_when_key_absent(self, tmp_path, monkeypatch):
        """A gstack source with excludes but no include_patterns gains the default."""
        from mind_meld.config import DEFAULT_SOURCES

        gstack_default = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        config_path = self._write_explicit_config(
            tmp_path, exclude_patterns=list(gstack_default["exclude_patterns"])
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        before = config_path.read_bytes()
        dry = runner.invoke(app, ["migrate-config", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "Recommended include_patterns updates" in dry.output
        assert config_path.read_bytes() == before

        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output
        output = " ".join(result.output.split())
        assert (
            "per-run output (review-prep trees, loop state, gate diffs) stays on this Mac" in output
        )
        assert "Run mm push to publish the exclusions" in output

        loaded = config_module.load_config(config_path)
        gstack = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack")
        assert gstack["include_patterns"] == list(gstack_default["include_patterns"])
        assert "projects/*/checkpoints/**" in gstack["include_patterns"]

    def test_user_edited_include_patterns_left_alone(self, tmp_path, monkeypatch):
        """A present (even empty) include_patterns is the user's choice."""
        from mind_meld.config import DEFAULT_SOURCES

        gstack_default = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        config_path = self._write_explicit_config(
            tmp_path,
            exclude_patterns=list(gstack_default["exclude_patterns"]),
            include_patterns=[],
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output
        assert "already up to date" in result.output
        loaded = config_module.load_config(config_path)
        gstack = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack")
        assert gstack["include_patterns"] == []

    def test_non_generic_gstack_source_skips_recommended_includes(self, tmp_path):
        """A source named gstack with a non-generic type must not receive
        recommended include_patterns (which _validate_sources rejects on
        non-generic sources)."""
        diffs = cli_module._compute_recommended_includes_diff(
            [{"name": "gstack", "path": str(tmp_path / ".gstack"), "type": "claude"}]
        )
        assert diffs == []

    def test_adds_missing_recommended_excludes(self, tmp_path, monkeypatch):
        """A source without exclude_patterns gets the recommended set."""
        config_path = self._write_explicit_config(tmp_path, exclude_patterns=None)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)
        # Auto-confirm the inner "Apply these updates?" prompt.
        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output

        output = " ".join(result.output.split())
        assert "per-machine caches and generated output" in output
        assert "for gstack: virtualenvs, app bundles, archives and binaries, QA evidence" in output
        assert "writes no tombstones, and every Mac keeps its copy" in output
        assert "Removing a recommended glob later brings this prompt back" in output
        assert output.index("writes no tombstones") < output.index("+ config.yaml")
        assert "Run mm push to publish the exclusions" in output
        assert "mm status counts them as pending deletions" in output
        assert "No Mac deletes its local copy" in output

        loaded = config_module.load_config(config_path)
        gstack = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack")
        assert "projects/*/repo-mode.json" in gstack["exclude_patterns"]
        assert "projects/*/land-deploy-confirmed" in gstack["exclude_patterns"]
        assert "config.yaml" in gstack["exclude_patterns"]  # v0.9.3

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        config_path = self._write_explicit_config(tmp_path, exclude_patterns=None)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        before = config_path.read_bytes()
        result = runner.invoke(app, ["migrate-config", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert config_path.read_bytes() == before
        output = " ".join(result.output.split())
        assert "per-machine caches and generated output" in output
        assert "writes no tombstones, and every Mac keeps its copy" in output
        assert "Run mm push to publish the exclusions" not in output
        loaded = config_module.load_config(config_path)
        gstack = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack")
        # exclude_patterns NOT added.
        assert "exclude_patterns" not in gstack or not gstack["exclude_patterns"]

    # Generated by /ship coverage audit for Track 72B.
    # Value: protects=incomplete migrations preserve config and omit the publish instruction;
    # fails_when=cancellation, lock refusal or write failure prints the success instruction;
    # why_new=existing migration tests cover success, previews and no-ops but no failures;
    # seam=none;
    # no_claim=live fleet migration; uses temporary config and lock plus an injected write failure.
    @pytest.mark.parametrize("outcome", ["declined", "locked", "write-failed"])
    def test_incomplete_migration_has_no_publish_instruction72b(
        self, tmp_path, monkeypatch, outcome
    ):
        config_path = self._write_explicit_config(
            tmp_path, exclude_patterns=list(_PRE18_GSTACK_GLOBS72B)
        )
        monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)
        before = config_path.read_bytes()

        if outcome == "write-failed":

            def fail_write(*args, **kwargs):
                raise OSError("simulated config write failure")

            monkeypatch.setattr(cli_module, "patch_config_on_disk", fail_write)
        if outcome == "locked":
            cli_module.acquire_lock()
        try:
            result = runner.invoke(
                app,
                ["migrate-config", *([] if outcome == "declined" else ["--yes"])],
                input="n\n" if outcome == "declined" else None,
            )
            assert result.exit_code == (0 if outcome == "declined" else 1), result.output
            output = " ".join(result.output.split())
            if outcome == "declined":
                assert "Cancelled." in output
            elif outcome == "locked":
                assert "already holds the lock" in output
            assert config_path.read_bytes() == before
            assert "Config written to" not in output
            assert "Run mm push to publish the exclusions" not in output
        finally:
            if outcome == "locked":
                cli_module.release_lock()

    def test_attended_push_migration_explains_and_publishes72b(self, tmp_path, monkeypatch):
        # include_patterns=[] pins this test to the generator-glob migration;
        # the allowlist migration has its own coverage.
        config_path = self._write_explicit_config(
            tmp_path, exclude_patterns=list(_PRE18_GSTACK_GLOBS72B), include_patterns=[]
        )
        root = tmp_path / ".gstack"
        fixture = TestExcludePatterns5C()
        fixture._populate_generators72b(root)
        backend = fixture._bootstrap(tmp_path / "storage")
        register_device(backend, "dev-a", "A")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)
        before = config_path.read_bytes()
        original = cli_module._maybe_prompt_migration

        def prompt(config, **kwargs):
            monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
            monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
            return original(config, **kwargs)

        monkeypatch.setattr(cli_module, "_maybe_prompt_migration", prompt)
        monkeypatch.setattr(typer, "confirm", lambda *a, **kw: True)
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output
        output = " ".join(result.output.split())
        assert "Config is missing recommended exclude_patterns for source(s): gstack" in output
        assert "for gstack: virtualenvs, app bundles, archives and binaries, QA evidence" in output
        assert "writes no tombstones, and every Mac keeps its copy" in output
        assert "produce churn on every pull" not in output
        assert "Run mm push to publish the exclusions" not in output
        assert config_path.read_bytes() != before
        remote = fixture._remote72b(backend, "dev-a")
        assert set(remote["sources"]["gstack"]["files"]) == set(_KEPT_PATHS72B)
        assert remote["tombstones"] == {}

    def test_codex_only_migration_has_generic_copy72b(self, tmp_path, monkeypatch):
        config_path = self._write_explicit_config(tmp_path)
        config = config_module.load_config(config_path)
        config["sync"]["sources"] = [
            {
                "name": "codex",
                "path": str(tmp_path / "codex"),
                "type": "generic",
                "include_dirs": ["skills"],
                "exclude_patterns": [],
            }
        ]
        save_config(config, config_path)
        monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)
        result = runner.invoke(app, ["migrate-config", "--dry-run"])
        assert result.exit_code == 0, result.output
        output = " ".join(result.output.split())
        assert "per-machine caches and generated output" in output
        assert "for gstack:" not in output
        assert "virtualenvs" not in output
        assert "Run mm push to publish the exclusions" not in output

    def test_preserves_user_added_excludes(self, tmp_path, monkeypatch):
        """Migration appends missing recommended globs to whatever the
        user already had — does not wipe their custom entries."""
        config_path = self._write_explicit_config(
            tmp_path, exclude_patterns=["my-custom-pattern.txt"]
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output
        loaded = config_module.load_config(config_path)
        gstack = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack")
        assert "my-custom-pattern.txt" in gstack["exclude_patterns"]
        assert "projects/*/repo-mode.json" in gstack["exclude_patterns"]
        assert "config.yaml" in gstack["exclude_patterns"]  # v0.9.3

    def _write_opencode_block_config(self, tmp_path):
        """gstack already has recommended excludes so the only pending
        migration is the leftover opencode [[sync.sources]] block."""
        from mind_meld.config import DEFAULT_SOURCES

        gstack_default = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        config_path = tmp_path / "config.toml"
        opencode_dir = tmp_path / "opencode-root"
        opencode_dir.mkdir()
        gstack_dir = tmp_path / ".gstack"
        gstack_dir.mkdir()
        config = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(gstack_dir),
                        "type": "generic",
                        "include_dirs": ["projects"],
                        "exclude_patterns": list(gstack_default["exclude_patterns"]),
                        "include_patterns": list(gstack_default["include_patterns"]),
                    },
                    {
                        "name": "opencode",
                        "path": str(opencode_dir),
                        "type": "generic",
                        "include_dirs": ["skills"],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        return config_path

    def test_migrate_config_removes_the_dead_opencode_source_block(self, tmp_path, monkeypatch):
        config_path = self._write_opencode_block_config(tmp_path)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Config written to" in " ".join(result.output.split())
        assert "Run mm push to publish the exclusions" not in " ".join(result.output.split())

        import tomllib

        with open(config_path, "rb") as f:
            on_disk = tomllib.load(f)
        names = [s["name"] for s in on_disk["sync"]["sources"]]
        assert "opencode" not in names
        assert "gstack" in names
        assert "opencode" in on_disk["sync"]["disabled_sources"]

    def test_maybe_prompt_migration_non_tty_warns_without_writing(self, tmp_path, monkeypatch):
        config_path = self._write_opencode_block_config(tmp_path)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        class _NonTTY:
            def isatty(self) -> bool:
                return False

        captured: list[str] = []
        monkeypatch.setattr(cli_module.sys, "stdin", _NonTTY())
        monkeypatch.setattr(cli_module.sys, "stdout", _NonTTY())
        monkeypatch.setattr(
            cli_module.stderr_console,
            "print",
            lambda message, *a, **k: captured.append(str(message)),
        )

        before = config_path.read_bytes()
        config = config_module.load_config(config_path)
        cli_module._maybe_prompt_migration(config, read_only=False)
        assert config_path.read_bytes() == before
        joined = " ".join(captured)
        assert "retired" in joined.lower() or "opencode" in joined
        assert "migrate-config" in joined

    def test_autopush_does_not_mutate_retired_opencode_block(self, tmp_path, monkeypatch):
        config_path = self._write_opencode_block_config(tmp_path)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)
        sidecar_dir = tmp_path / "sidecar"
        sidecar_dir.mkdir(exist_ok=True)
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)

        backend = LocalBackend(tmp_path / "storage")
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0, result.output
        import tomllib

        with open(config_path, "rb") as f:
            on_disk = tomllib.load(f)
        names = [s["name"] for s in on_disk["sync"]["sources"]]
        assert "opencode" in names
        assert "opencode" not in (on_disk["sync"].get("disabled_sources") or [])


class TestMmStatusMissingExcludesWarning:
    """5C-9: mm status surfaces the missing-excludes signal."""

    def test_status_warns_when_excludes_missing(self, tmp_path, monkeypatch):
        # Minimal config with an explicit gstack source missing excludes.
        config_path = tmp_path / "config.toml"
        gstack_dir = tmp_path / ".gstack"
        (gstack_dir / "projects" / "myapp").mkdir(parents=True)
        config = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(gstack_dir),
                        "type": "generic",
                        "include_dirs": ["projects"],
                    }
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        backend = LocalBackend(tmp_path / "storage")
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "missing recommended excludes" in result.output
        assert "gstack" in result.output


class TestMmSourcesShowsExcludeCounts:
    """5C-8: mm sources prints per-source exclude_patterns match counts."""

    def test_sources_table_includes_excluded_column(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.toml"
        gstack_dir = tmp_path / ".gstack"
        proj = gstack_dir / "projects" / "myapp"
        proj.mkdir(parents=True)
        (proj / "repo-mode.json").write_text("{}")
        (proj / "role.md").write_text("kept")
        config = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(gstack_dir),
                        "type": "generic",
                        "include_dirs": ["projects"],
                        "exclude_patterns": ["projects/*/repo-mode.json"],
                    }
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

        result = runner.invoke(app, ["sources"])
        assert result.exit_code == 0, result.output
        assert "Excluded" in result.output


class TestMmLogCommand:
    """5C-5: `mm log` queries pull-history.jsonl with filters and formats."""

    def test_log_empty_when_no_history(self, tmp_path, monkeypatch):
        # Isolate the history dir.
        monkeypatch.setattr("mind_meld.pullhistory.HISTORY_DIR", tmp_path / "mm_state")
        # Need a valid config to construct the typer context cleanly.
        config_path = tmp_path / "config.toml"
        save_config(
            {
                "device": {"id": "dev-a", "name": "A"},
                "storage": {"path": str(tmp_path / "storage")},
                "sync": {"max_file_size": 52_428_800, "claude_dir": str(tmp_path / ".claude")},
                "crypto": {"argon2_memory_kb": 1024},
            },
            config_path,
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

        result = runner.invoke(app, ["log"])
        assert result.exit_code == 0, result.output
        assert "No log entries" in result.output

    def test_log_filters_by_action(self, tmp_path, monkeypatch):
        from mind_meld import pullhistory

        monkeypatch.setattr("mind_meld.pullhistory.HISTORY_DIR", tmp_path / "mm_state")
        for i in range(3):
            pullhistory.append(
                verb="pull",
                device="dev-a",
                source="gstack",
                rel_path=f"f-{i}.md",
                action="written",
            )
        for i in range(2):
            pullhistory.append(
                verb="pull",
                device="dev-a",
                source="gstack",
                rel_path=f"e-{i}.json",
                action="excluded",
            )

        config_path = tmp_path / "config.toml"
        save_config(
            {
                "device": {"id": "dev-a", "name": "A"},
                "storage": {"path": str(tmp_path / "storage")},
                "sync": {"max_file_size": 52_428_800, "claude_dir": str(tmp_path / ".claude")},
                "crypto": {"argon2_memory_kb": 1024},
            },
            config_path,
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

        result = runner.invoke(app, ["log", "--action", "excluded", "--format", "jsonl"])
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.strip().split("\n") if line.startswith("{")]
        assert len(lines) == 2
        for line in lines:
            rec = json.loads(line)
            assert rec["action"] == "excluded"


class TestInversion5E:
    """Track 5E IRON RULE regression pins.

    Pins the conflict-direction inversion (canonical = local, sidecar =
    remote), the strict pull-start fleet-version refusal, the dual-mode
    resolve dispatch by filename prefix, and the pre-inversion file
    migration. Failing these tests blocks ship.
    """

    def _make_claude_config(self, tmp_path, storage_dir, claude_dir, device_id, device_name):
        config_path = tmp_path / f"config_{device_id}.toml"
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path

    def _bootstrap(self, storage_dir):
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        return backend

    def _activate(self, monkeypatch, config_path):
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

    def test_inversion_canonical_stays_local_remote_in_sidecar(self, tmp_path, monkeypatch):
        """Round-trip: A pushes, B locally diverges, B pulls. Post-inversion,
        canonical stays at B's LOCAL edit, A's REMOTE bytes land in the
        sidecar. Plus: rollback path is irrelevant (no rename happens),
        so canonical is preserved on sidecar-write failure too."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        claude_b = tmp_path / "machine_b" / ".claude"
        memory_a = claude_a / "projects" / "-Users-kb-myapp" / "memory"
        memory_a.mkdir(parents=True)
        (memory_a / "role.md").write_text("A initial")
        memory_b = claude_b / "projects" / "-Users-kb-myapp" / "memory"
        memory_b.mkdir(parents=True)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = self._make_claude_config(tmp_path, storage_dir, claude_a, "dev-a", "A")
        config_b = self._make_claude_config(tmp_path, storage_dir, claude_b, "dev-b", "B")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # Round-trip phase 1: A pushes, B pulls clean.
        self._activate(monkeypatch, config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0
        self._activate(monkeypatch, config_b)
        assert runner.invoke(app, ["pull"]).exit_code == 0
        role_b = memory_b / "role.md"
        assert role_b.exists()

        # Phase 2: divergent edits. A pushes new content; B edits locally.
        self._activate(monkeypatch, config_a)
        (memory_a / "role.md").write_text("A second push")
        assert runner.invoke(app, ["push"]).exit_code == 0

        self._activate(monkeypatch, config_b)
        role_b.write_text("B local edit")
        old = time.time() - 3600
        os.utime(role_b, (old, old))  # Backdate so pull sees remote-newer.

        assert runner.invoke(app, ["pull"]).exit_code == 0

        # Inversion: canonical = local; sidecar = remote.
        assert role_b.read_text() == "B local edit"
        siblings = list(memory_b.glob("role.sync-conflict-*.md"))
        assert len(siblings) == 1
        assert siblings[0].read_text() == "A second push"
        # No `v0-` prefix on a freshly-produced post-inversion file.
        assert "sync-conflict-v0-" not in siblings[0].name

    def test_pull_refuses_when_peer_pre_inversion(self, tmp_path, monkeypatch):
        """Mixed-version refusal: peer's last_seen_version < INVERSION_MIN_VERSION
        => mm pull exits non-zero before any I/O with peer name in message."""
        from mind_meld.cli import INVERSION_MIN_VERSION
        from mind_meld.devices import register_device as _register
        from mind_meld.storage.keys import device_key

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        backend = self._bootstrap(storage_dir)
        _register(backend, "dev-self", "Self")
        _register(backend, "dev-old", "OldPeer")
        # Manually write a stale device.json for the old peer with a
        # pre-inversion last_seen_version. The fleet check must refuse.
        old_data = {
            "device_id": "dev-old",
            "device_name": "OldPeer",
            "registered": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-04-01T00:00:00+00:00",
            "last_seen_version": "0.9.1",  # pre-INVERSION_MIN_VERSION
        }
        backend.put(device_key("dev-old"), json.dumps(old_data).encode())

        config_path = self._make_claude_config(
            tmp_path, storage_dir, claude_dir, "dev-self", "Self"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 1, result.output
        # Refusal cites the offending peer (via stderr — _error routes there).
        combined = (result.output or "") + (result.stderr or "")
        assert "OldPeer" in combined
        assert "Mixed-version" in combined or "0.9.1" in combined
        # Threshold appears in the message.
        assert INVERSION_MIN_VERSION in combined

    def test_pull_refuses_when_peer_last_seen_version_missing(self, tmp_path, monkeypatch):
        """Peer last_seen present (active), last_seen_version missing — REFUSE."""
        from mind_meld.devices import register_device as _register
        from mind_meld.storage.keys import device_key

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        backend = self._bootstrap(storage_dir)
        _register(backend, "dev-self", "Self")
        _register(backend, "dev-stale", "StalePeer")
        # Active peer (last_seen present) but no last_seen_version.
        stale_data = {
            "device_id": "dev-stale",
            "device_name": "StalePeer",
            "registered": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-04-01T00:00:00+00:00",
        }
        backend.put(device_key("dev-stale"), json.dumps(stale_data).encode())

        config_path = self._make_claude_config(
            tmp_path, storage_dir, claude_dir, "dev-self", "Self"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 1, result.output
        combined = (result.output or "") + (result.stderr or "")
        assert "StalePeer" in combined

    def test_pull_proceeds_when_peer_inactive(self, tmp_path, monkeypatch):
        """Inactive peer (registered, never pushed): no last_seen → ALLOW."""
        from mind_meld.devices import register_device as _register

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        backend = self._bootstrap(storage_dir)
        _register(backend, "dev-self", "Self")
        _register(backend, "dev-newpeer", "NewPeer")  # registered, no last_seen

        config_path = self._make_claude_config(
            tmp_path, storage_dir, claude_dir, "dev-self", "Self"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # Pull proceeds (no manifests to download, but doesn't refuse).
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 0, result.output

    def test_pull_proceeds_with_no_peers(self, tmp_path, monkeypatch):
        """Self-only fleet: pull does NOT self-refuse on its own version."""
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-self", "Self")

        config_path = self._make_claude_config(
            tmp_path, storage_dir, claude_dir, "dev-self", "Self"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 0, result.output

    def test_pull_refuses_when_peer_device_json_corrupt(self, tmp_path, monkeypatch):
        """Dropped peer (corrupt device.json): refuse and cite the storage key."""
        from mind_meld.devices import register_device as _register
        from mind_meld.storage.keys import DEVICES_PREFIX

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        backend = self._bootstrap(storage_dir)
        _register(backend, "dev-self", "Self")
        # Write a malformed device.json directly.
        bad_key = f"{DEVICES_PREFIX}dev-bad.json"
        backend.put(bad_key, b"{not valid json}")

        config_path = self._make_claude_config(
            tmp_path, storage_dir, claude_dir, "dev-self", "Self"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 1, result.output
        combined = (result.output or "") + (result.stderr or "")
        assert bad_key in combined or "dev-bad" in combined

    def test_pre_inversion_file_resolves_under_v0_dispatch(self, tmp_path, monkeypatch):
        """A `v0-`-prefixed file (sidecar = local, canonical = remote) routes
        through the pre-inversion arm of the dual dispatch: (l)ocal renames
        sidecar over canonical, recovering local edits."""
        from mind_meld.resolveflow import _resolve_interactive_loop

        canonical = tmp_path / "doc.md"
        canonical.write_bytes(b"remote content")  # pre-inversion: canonical = remote
        # `v0-`-prefixed sidecar — pre-inversion semantics.
        sidecar = tmp_path / "doc.sync-conflict-v0-20260420-120000-devA1234.md"
        sidecar.write_bytes(b"local content")

        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")
        _resolve_interactive_loop([("s1", sidecar, canonical)])

        # (l) under v0- prefix = sidecar.rename(canonical). Local recovered.
        assert canonical.read_bytes() == b"local content"
        assert not sidecar.exists()

    def test_pre_inversion_file_keep_remote_unlinks_local_sidecar(self, tmp_path, monkeypatch):
        """A `v0-`-prefixed file with (r)emote choice: canonical IS remote,
        so we drop the local sidecar."""
        from mind_meld.resolveflow import _resolve_interactive_loop

        canonical = tmp_path / "doc.md"
        canonical.write_bytes(b"remote content")
        sidecar = tmp_path / "doc.sync-conflict-v0-20260420-120000-devA1234.md"
        sidecar.write_bytes(b"local content")

        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "r")
        _resolve_interactive_loop([("s1", sidecar, canonical)])

        assert canonical.read_bytes() == b"remote content"
        assert not sidecar.exists()

    def test_mm_conflicts_is_read_only(self, tmp_path, monkeypatch):
        """mm conflicts must NOT migrate pre-inversion files — codex-2 #5
        race against autopull. The sidecar's filename stays unchanged."""
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        memory = claude_dir / "projects" / "-Users-kb-app" / "memory"
        memory.mkdir(parents=True)
        canonical = memory / "role.md"
        canonical.write_bytes(b"remote content")
        # Pre-inversion-style sidecar WITHOUT the v0- prefix.
        legacy = memory / "role.sync-conflict-20260420-120000-devA1234.md"
        legacy.write_bytes(b"local content")

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-self", "Self")
        config_path = self._make_claude_config(
            tmp_path, storage_dir, claude_dir, "dev-self", "Self"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["conflicts"])
        assert result.exit_code == 0, result.output
        # Filename UNCHANGED — no migration happened.
        assert legacy.exists()
        assert not (memory / "role.sync-conflict-v0-20260420-120000-devA1234.md").exists()

    def test_mm_resolve_migrates_pre_inversion_files(self, tmp_path, monkeypatch):
        """mm resolve runs under the lockfile and DOES migrate pre-inversion
        files to the v0- prefix on first discovery — but only when the
        file's mtime predates the inversion-install marker (5E ship-fix
        gate; see _ensure_inversion_marker)."""
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        memory = claude_dir / "projects" / "-Users-kb-app" / "memory"
        memory.mkdir(parents=True)
        canonical = memory / "role.md"
        canonical.write_bytes(b"remote content")
        legacy = memory / "role.sync-conflict-20260420-120000-devA1234.md"
        legacy.write_bytes(b"local content")
        # Backdate the legacy file to simulate a real pre-v0.9.2 conflict
        # produced before the install marker was created.
        old = time.time() - 86400  # 1 day ago
        os.utime(legacy, (old, old))

        # Redirect SIDECAR_DIR so the inversion marker lands in tmp_path.
        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)

        backend = self._bootstrap(storage_dir)
        register_device(backend, "dev-self", "Self")
        config_path = self._make_claude_config(
            tmp_path, storage_dir, claude_dir, "dev-self", "Self"
        )
        self._activate(monkeypatch, config_path)
        monkeypatch.setattr("mind_meld.config.LOCK_PATH", tmp_path / "lock")
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", tmp_path / "lock")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        # User picks 's' (skip; both files left on disk -- no mutation).
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")

        result = runner.invoke(app, ["resolve"])
        assert result.exit_code == 0, result.output
        # Migration happened — old name is gone, v0- name exists.
        assert not legacy.exists()
        migrated = memory / "role.sync-conflict-v0-20260420-120000-devA1234.md"
        assert migrated.exists()
        assert migrated.read_bytes() == b"local content"


class TestShipFixes5E:
    """Pre-landing review fixes (5E ship-fix). Pinned per IRON RULE.

    F1: post-inversion files must NOT be migrated to v0- on consecutive
        pulls (silent data loss caught by /ship adversarial review).
    F4: autopull (quiet=True) must NOT log "excluded" records — the
        1MB pullhistory cap rotates within hours under hourly hooks.
    """

    def _make_gstack_config(self, tmp_path, storage_dir, gstack_dir, device_id, device_name):
        config_path = tmp_path / f"config_{device_id}.toml"
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(gstack_dir),
                        "type": "generic",
                        "include_dirs": ["projects"],
                        "exclude_patterns": ["projects/*/repo-mode.json"],
                    }
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path

    def test_post_inversion_file_not_migrated_on_consecutive_runs(self, tmp_path, monkeypatch):
        """F1 ship-blocker: a fresh post-inversion conflict file (mtime
        AFTER the install marker) MUST NOT be renamed to v0- by the
        next pull's migration sweep. Without the mtime gate,
        `_resolve_interactive_loop`'s prefix-based dispatch would
        silently flip the (l)/(r) ops and destroy local edits."""
        from mind_meld.cli import (
            conflict_filename,
        )
        from mind_meld.manifest import is_pre_inversion_conflict_filename
        from mind_meld.resolveflow import _ensure_inversion_marker, _migrate_pre_inversion_conflict

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)

        # Stamp the install marker first (simulates a v0.9.2 install
        # that has run at least one pull/resolve before).
        marker_ts = _ensure_inversion_marker()
        assert marker_ts is not None

        # Now produce a fresh post-inversion sidecar via _apply_conflict's
        # path-builder. Mtime will be NOW (i.e. >= marker_ts).
        canonical = tmp_path / "doc.md"
        canonical.write_bytes(b"local content")
        sidecar_path = conflict_filename(canonical, "devAAAA1234")
        sidecar_path.write_bytes(b"remote bytes from peer")
        # Migration sweep must NOT rename this file.
        result = _migrate_pre_inversion_conflict(sidecar_path)
        assert result == sidecar_path
        assert sidecar_path.exists()
        assert not is_pre_inversion_conflict_filename(sidecar_path.name)
        # And the v0- variant must NOT have been created.
        v0_variant = sidecar_path.with_name(
            sidecar_path.name.replace(".sync-conflict-", ".sync-conflict-v0-")
        )
        assert not v0_variant.exists()
        # Sanity: sidecar_dir / "inversion-installed-at" exists with 0600.
        marker_path = sidecar_dir / "inversion-installed-at"
        assert marker_path.exists()
        import stat as _stat

        assert _stat.S_IMODE(marker_path.stat().st_mode) == 0o600

    def test_backdated_peer_mtime_does_not_migrate_v1_sidecar(self, tmp_path, monkeypatch):
        """B6: a fresh sidecar carrying a 90-day peer mtime is NOT migrated."""
        from mind_meld.cli import conflict_filename
        from mind_meld.manifest import is_pre_inversion_conflict_filename, is_v1_conflict_filename
        from mind_meld.resolveflow import _ensure_inversion_marker, _migrate_pre_inversion_conflict

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        marker_ts = _ensure_inversion_marker()
        assert marker_ts is not None
        canonical = tmp_path / "doc.md"
        canonical.write_bytes(b"local")
        sidecar_path = conflict_filename(canonical, "devAAAA1234")
        sidecar_path.write_bytes(b"remote")
        ancient = time.time() - 90 * 86400
        os.utime(sidecar_path, (ancient, ancient))
        assert ancient < marker_ts
        result = _migrate_pre_inversion_conflict(sidecar_path)
        assert result == sidecar_path
        assert sidecar_path.exists()
        assert is_v1_conflict_filename(sidecar_path.name)
        assert not is_pre_inversion_conflict_filename(sidecar_path.name)

    def test_unprefixed_sidecar_newer_than_marker_not_migrated_despite_old_mtime(
        self, tmp_path, monkeypatch
    ):
        """Filename-clock gate, not the v1 short-circuit: unprefixed post-
        inversion files with a filename timestamp after the marker must
        not be v0-tagged even when st_mtime is ancient."""
        from datetime import datetime, timezone

        from mind_meld.manifest import is_pre_inversion_conflict_filename
        from mind_meld.resolveflow import _ensure_inversion_marker, _migrate_pre_inversion_conflict

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        marker_ts = _ensure_inversion_marker()
        assert marker_ts is not None
        future = datetime.fromtimestamp(marker_ts + 3600, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        sidecar = tmp_path / f"doc.sync-conflict-{future}-devA1234.md"
        sidecar.write_bytes(b"remote")
        os.utime(sidecar, (marker_ts - 90 * 86400, marker_ts - 90 * 86400))
        result = _migrate_pre_inversion_conflict(sidecar)
        assert result == sidecar
        assert sidecar.exists()
        assert not is_pre_inversion_conflict_filename(sidecar.name)

    def test_unprefixed_post_inversion_not_migrated_when_marker_is_recreated(
        self, tmp_path, monkeypatch
    ):
        """A sidecar minted after v0.9.2 (unprefixed, filename 2026-08)
        must not be v0-tagged when inversion-installed-at is recreated
        as 'now'."""
        from mind_meld.manifest import is_pre_inversion_conflict_filename
        from mind_meld.resolveflow import _ensure_inversion_marker, _migrate_pre_inversion_conflict

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        _ensure_inversion_marker()
        sidecar = tmp_path / "doc.sync-conflict-20260815-120000-devA1234.md"
        sidecar.write_bytes(b"remote")
        result = _migrate_pre_inversion_conflict(sidecar)
        assert result == sidecar
        assert sidecar.exists()
        assert not is_pre_inversion_conflict_filename(sidecar.name)

    def test_unparseable_filename_refuses_to_migrate(self, tmp_path, monkeypatch):
        """B8: unparseable filename → refuse, not fall back to st_mtime."""
        from mind_meld.resolveflow import _ensure_inversion_marker, _migrate_pre_inversion_conflict

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        _ensure_inversion_marker()
        legacy = tmp_path / "doc.sync-conflict-20261345-999999-devA1234.md"
        legacy.write_bytes(b"x")
        old = time.time() - 86400
        os.utime(legacy, (old, old))
        result = _migrate_pre_inversion_conflict(legacy)
        assert result == legacy
        assert legacy.exists()

    def test_double_infix_does_not_accrete_v0(self, tmp_path, monkeypatch):
        """B9 / E2: rindex inserts v0- at the LAST infix, once."""
        from mind_meld.manifest import is_pre_inversion_conflict_filename
        from mind_meld.resolveflow import _ensure_inversion_marker, _migrate_pre_inversion_conflict

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        _ensure_inversion_marker()
        original = tmp_path / ("notes.sync-conflict-log.sync-conflict-20260101-000000-abcd1234.md")
        original.write_bytes(b"divergent")
        first = _migrate_pre_inversion_conflict(original)
        assert first != original
        assert first.name == (
            "notes.sync-conflict-log.sync-conflict-v0-20260101-000000-abcd1234.md"
        )
        assert is_pre_inversion_conflict_filename(first.name)
        second = _migrate_pre_inversion_conflict(first)
        third = _migrate_pre_inversion_conflict(second)
        assert second == first
        assert third == first
        assert first.exists()
        assert not original.exists()

    def test_pre_inversion_file_still_migrated_when_older_than_marker(self, tmp_path, monkeypatch):
        """F1 fix complement: pre-existing legacy files (mtime < marker)
        must still be migrated. The fix is a SAFETY gate, not a
        disable-migration switch."""
        from mind_meld.manifest import is_pre_inversion_conflict_filename
        from mind_meld.resolveflow import _ensure_inversion_marker, _migrate_pre_inversion_conflict

        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)

        # Create a legacy file BEFORE the marker exists, then backdate
        # it to simulate a real pre-v0.9.2 file from the past.
        legacy = tmp_path / "doc.sync-conflict-20260420-120000-devA1234.md"
        legacy.write_bytes(b"local bytes from pre-v0.9.2")
        old = time.time() - 86400
        os.utime(legacy, (old, old))

        # Now stamp the marker (mtime < marker).
        marker_ts = _ensure_inversion_marker()
        assert marker_ts is not None
        assert old < marker_ts

        # Migration sweep must rename it.
        result = _migrate_pre_inversion_conflict(legacy)
        assert result != legacy
        assert is_pre_inversion_conflict_filename(result.name)
        assert not legacy.exists()
        assert result.read_bytes() == b"local bytes from pre-v0.9.2"

    def test_marker_failure_degrades_to_no_migration(self, tmp_path, monkeypatch):
        """F1 fail-safe: if `_ensure_inversion_marker` returns None (perms,
        disk full, parse error), `_migrate_pre_inversion_conflict` must
        return the original path unchanged. Mass re-tagging on a broken
        marker would be the original CRITICAL bug all over again."""
        from mind_meld.resolveflow import _migrate_pre_inversion_conflict

        legacy = tmp_path / "doc.sync-conflict-20260420-120000-devA1234.md"
        legacy.write_bytes(b"local bytes")
        old = time.time() - 86400
        os.utime(legacy, (old, old))

        # Force the marker helper to return None.
        monkeypatch.setattr("mind_meld.resolveflow._ensure_inversion_marker", lambda: None)
        result = _migrate_pre_inversion_conflict(legacy)
        assert result == legacy
        assert legacy.exists()

    def test_autopull_does_not_log_excluded_paths(self, tmp_path, monkeypatch):
        """F4 ship-fix: autopull (quiet=True) skips the per-excluded-path
        pullhistory.append calls so the 1MB cap doesn't rotate within
        hours under repeated hook fires."""
        from mind_meld import pullhistory

        # Isolate pullhistory dir.
        history_dir = tmp_path / "mm_state"
        monkeypatch.setattr("mind_meld.pullhistory.HISTORY_DIR", history_dir)

        storage_dir = tmp_path / "storage"
        gstack_a = tmp_path / "machine_a" / ".gstack"
        gstack_b = tmp_path / "machine_b" / ".gstack"
        # Populate A with a file that B's exclude_patterns would filter out.
        proj_a = gstack_a / "projects" / "myapp"
        proj_a.mkdir(parents=True)
        (proj_a / "repo-mode.json").write_text("A's per-machine cache")
        (proj_a / "role.md").write_text("real content")
        gstack_b.mkdir(parents=True)

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = tmp_path / "config_dev-a.toml"
        save_config(
            {
                "device": {"id": "dev-a", "name": "A"},
                "storage": {"path": str(storage_dir)},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [
                        {
                            "name": "gstack",
                            "path": str(gstack_a),
                            "type": "generic",
                            "include_dirs": ["projects"],
                        }
                    ],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            config_a,
        )
        config_b = self._make_gstack_config(tmp_path, storage_dir, gstack_b, "dev-b", "B")

        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # A pushes (records its v0.9.2 last_seen_version so B's fleet check passes).
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0

        # Redirect lockfile + sidecar so B's autopull is hermetic.
        monkeypatch.setattr("mind_meld.config.LOCK_PATH", tmp_path / "lock")
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", tmp_path / "lock")
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", tmp_path / "sidecar_b")

        # B's autopull runs in quiet mode.
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0, result.output

        # Verify: NO "excluded" records were written by the quiet pull.
        records = list(pullhistory.read_records())
        excluded = [r for r in records if r.get("action") == "excluded"]
        assert excluded == [], (
            f"autopull (quiet=True) must NOT log excluded paths "
            f"(found {len(excluded)}). Interactive mm pull is the place "
            f"to surface them via the audit log."
        )

    def test_interactive_pull_still_logs_excluded(self, tmp_path, monkeypatch):
        """F4 fix complement: interactive `mm pull` (not quiet) DOES
        write the excluded records — the audit-log feature still works
        for users who explicitly ran the command."""
        from mind_meld import pullhistory

        history_dir = tmp_path / "mm_state"
        monkeypatch.setattr("mind_meld.pullhistory.HISTORY_DIR", history_dir)

        storage_dir = tmp_path / "storage"
        gstack_a = tmp_path / "machine_a" / ".gstack"
        gstack_b = tmp_path / "machine_b" / ".gstack"
        proj_a = gstack_a / "projects" / "myapp"
        proj_a.mkdir(parents=True)
        (proj_a / "repo-mode.json").write_text("A cache")
        (proj_a / "role.md").write_text("real")
        gstack_b.mkdir(parents=True)

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        register_device(backend, "dev-b", "B")

        config_a = tmp_path / "config_dev-a.toml"
        save_config(
            {
                "device": {"id": "dev-a", "name": "A"},
                "storage": {"path": str(storage_dir)},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [
                        {
                            "name": "gstack",
                            "path": str(gstack_a),
                            "type": "generic",
                            "include_dirs": ["projects"],
                        }
                    ],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            config_a,
        )
        config_b = self._make_gstack_config(tmp_path, storage_dir, gstack_b, "dev-b", "B")

        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
        assert runner.invoke(app, ["push"]).exit_code == 0

        monkeypatch.setattr("mind_meld.config.LOCK_PATH", tmp_path / "lock")
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", tmp_path / "lock")
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", tmp_path / "sidecar_b")
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 0, result.output

        records = list(pullhistory.read_records())
        excluded = [r for r in records if r.get("action") == "excluded"]
        # repo-mode.json should appear (excluded by B's pattern)
        assert any(r.get("rel_path") == "projects/myapp/repo-mode.json" for r in excluded)


class TestSyncLog:
    def test_writes_log_per_project(self, tmp_path):
        """Sync log should be written to each affected project dir."""

        claude_dir = tmp_path / ".claude"
        project_dir = claude_dir / "projects" / "-Users-kb-myapp"
        project_dir.mkdir(parents=True)

        logs = write_sync_log(
            claude_base=str(claude_dir),
            device_name="MacBook Pro",
            device_id="abc123",
            new_files=["projects/-Users-kb-myapp/memory/user_role.md"],
            modified_files=["projects/-Users-kb-myapp/memory/feedback.md"],
            deleted_files=[],
        )

        assert len(logs) == 1
        log_path = project_dir / ".mind-meld-log.md"
        assert log_path.exists()
        content = log_path.read_text()
        assert "MacBook Pro" in content
        assert "abc123" in content
        assert "memory/user_role.md" in content
        assert "memory/feedback.md" in content

    def test_no_log_without_changes(self, tmp_path):

        claude_dir = tmp_path / ".claude"
        (claude_dir / "projects" / "-foo").mkdir(parents=True)

        logs = write_sync_log(
            claude_base=str(claude_dir),
            device_name="Other",
            device_id="xyz",
            new_files=[],
            modified_files=[],
            deleted_files=[],
        )
        assert len(logs) == 0

    def test_sync_log_routes_through_fsutil_with_fsync_false(self, tmp_path, monkeypatch):
        """Sync log writes must go through fsutil with fsync=False —
        .mind-meld-log.md is cosmetic; per-file fsync would add pull latency."""

        claude_dir = tmp_path / ".claude"
        (claude_dir / "projects" / "-foo").mkdir(parents=True)

        calls: list[dict] = []
        real_write = synclog.fsutil.atomic_write_bytes

        def spy_write(path, data, *, fsync=False, mode=None):
            calls.append({"path": path, "fsync": fsync, "mode": mode})
            real_write(path, data, fsync=fsync, mode=mode)

        monkeypatch.setattr(synclog.fsutil, "atomic_write_bytes", spy_write)
        synclog.write_sync_log(
            claude_base=str(claude_dir),
            device_name="Other",
            device_id="xyz",
            new_files=["projects/-foo/memory/x.md"],
            modified_files=[],
            deleted_files=[],
        )
        assert len(calls) == 1
        assert calls[0]["fsync"] is False


class TestGCSafety:
    @staticmethod
    def _store_manifest(backend, device_id, hashes=()):
        fetch = crypto_module.fetch_crypto_init(backend)
        crypto_module.set_crypto_session(fetch.root_salt, fetch.argon2_memory_kb)
        manifest = {
            "device_id": device_id,
            "device_name": device_id,
            "sources": {
                "fixture": {
                    "base_path": str(backend.root),
                    "files": {
                        f"file-{i}.md": {"sha256": sha, "size": 1, "mtime": None}
                        for i, sha in enumerate(hashes)
                    },
                }
            },
            "tombstones": {},
        }
        backend.put(
            storage_keys.manifest_key(device_id),
            encrypt(serialize_manifest(manifest), PASSPHRASE, MEMORY_KB),
        )

    @pytest.mark.parametrize("entrypoint", ["gc", "push"])
    @pytest.mark.parametrize("manifest_first", [False, True])
    def test_gc_never_deletes_referenced_blobs(self, push_preview56, entrypoint, manifest_first):
        """Real GC and attended content push preserve peers in either delivery order."""
        env = push_preview56
        backend = env["backend"]
        initial = runner.invoke(app, ["push"])
        assert initial.exit_code == 0, initial.output
        own = cli_module._fetch_remote_manifest(backend, "dev-a", PASSPHRASE, MEMORY_KB).manifest
        live = {
            storage_keys.blob_key("dev-a", info["sha256"]): backend.get(
                storage_keys.blob_key("dev-a", info["sha256"])
            )
            for info in own["sources"]["claude"]["files"].values()
        }
        new_content = b"new content also held by the peer"
        new_sha = hashlib.sha256(new_content).hexdigest()
        orphan_content = b"own orphan still referenced by the peer"
        orphan_sha = hashlib.sha256(orphan_content).hexdigest()
        orphan = storage_keys.blob_key("dev-a", orphan_sha)
        backend.put(orphan, encrypt(orphan_content, PASSPHRASE, MEMORY_KB))
        peer_content = b"peer live before its manifest"
        peer_sha = hashlib.sha256(peer_content).hexdigest()
        peer_hashes = (peer_sha, new_sha, orphan_sha)
        register_device(backend, "dev-ab", "Prefix-sharing peer")
        if manifest_first:
            self._store_manifest(backend, "dev-ab", peer_hashes)
        peer_bytes = {}
        for sha, content in zip(peer_hashes, (peer_content, new_content, orphan_content)):
            key = storage_keys.blob_key("dev-ab", sha)
            peer_bytes[key] = encrypt(content, PASSPHRASE, MEMORY_KB)
            backend.put(key, peer_bytes[key])
        assert backend.exists(storage_keys.manifest_key("dev-ab")) == manifest_first

        if entrypoint == "push":
            (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_bytes(new_content)
        result = runner.invoke(app, [entrypoint])
        assert result.exit_code == 0, result.output
        assert not backend.exists(orphan)
        assert {key: backend.get(key) for key in live} == live
        assert {key: backend.get(key) for key in peer_bytes} == peer_bytes
        if entrypoint == "push":
            # A peer-owned identical hash cannot substitute for an own upload.
            uploaded = backend.get(storage_keys.blob_key("dev-a", new_sha))
            assert decrypt(uploaded, PASSPHRASE, MEMORY_KB) == new_content
            accepted = cli_module.sidecar.read("dev-a")
            assert (
                accepted["sources"]["claude"]["files"]["projects/-Users-kb-myapp/memory/new.md"][
                    "sha256"
                ]
                == new_sha
            )
        if not manifest_first:
            self._store_manifest(backend, "dev-ab", peer_hashes)
        again = runner.invoke(app, ["gc"])
        assert again.exit_code == 0, again.output
        assert {key: backend.get(key) for key in peer_bytes} == peer_bytes
        assert {key: backend.get(key) for key in live} == live

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_missing_own_manifest_skips_blobs_but_keeps_outer_retention(
        self, push_preview56, monkeypatch, dry_run
    ):
        env = push_preview56
        backend = env["backend"]
        assert not backend.exists(storage_keys.manifest_key("dev-a"))
        blob = storage_keys.blob_key("dev-a", hashlib.sha256(b"unproven orphan").hexdigest())
        backend.put(blob, b"keep without metadata")
        old_day = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
        old_event = env["events"] / "events" / f"dev-a-{old_day}.jsonl"
        old_event.parent.mkdir(parents=True)
        old_event.write_text("{}\n")
        own_tmp = backend.root / "data/dev-a/tmp-crashed.tmp"
        own_tmp.write_bytes(b"stale")
        peer_tmp = backend.root / "data/dev-ab/tmp-in-flight.tmp"
        peer_tmp.parent.mkdir(parents=True)
        peer_tmp.write_bytes(b"peer")
        listings, deletes = [], []
        original_list = LocalBackend.list_keys
        original_delete = LocalBackend.delete

        def list_keys(storage, prefix):
            listings.append(prefix)
            return original_list(storage, prefix)

        def delete(storage, key):
            deletes.append(key)
            return original_delete(storage, key)

        monkeypatch.setattr(LocalBackend, "list_keys", list_keys)
        monkeypatch.setattr(LocalBackend, "delete", delete)
        assert cli_module._do_gc(env["config"], PASSPHRASE, MEMORY_KB, dry_run, False) == 0
        before = _preview_tree(env["claude"].parent, pin=dry_run)
        result = runner.invoke(app, ["gc"] + (["--dry-run"] if dry_run else []))
        assert result.exit_code == 0, result.output
        warning = " ".join(result.stderr.split())
        assert "this device's manifest is missing: Mac A (dev-a)" in warning
        assert "Blob collection was skipped; no blobs were deleted" in warning
        assert "Wait for sync" in warning and "mm diag" in warning and "mm gc" in warning
        assert not listings and not deletes
        assert "GC complete" not in result.output and "orphaned blobs" not in result.output
        assert backend.get(blob) == b"keep without metadata"
        assert peer_tmp.read_bytes() == b"peer"
        assert old_event.exists() == dry_run
        assert own_tmp.exists() == dry_run
        if dry_run:
            assert cli_module.DRY_RUN_COMPLETE in " ".join(result.output.split())
            assert _preview_tree(env["claude"].parent) == before

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_valid_empty_own_manifest_collects_only_own_orphans(
        self, push_preview56, monkeypatch, dry_run
    ):
        env = push_preview56
        backend = env["backend"]
        self._store_manifest(backend, "dev-a")
        own = storage_keys.blob_key("dev-a", hashlib.sha256(b"own orphan").hexdigest())
        peer = storage_keys.blob_key("dev-ab", hashlib.sha256(b"peer").hexdigest())
        backend.put(own, b"own")
        backend.put(peer, b"peer")
        listings = []
        original = LocalBackend.list_keys

        def list_keys(storage, prefix):
            listings.append(prefix)
            return original(storage, prefix)

        monkeypatch.setattr(LocalBackend, "list_keys", list_keys)
        assert cli_module._do_gc(env["config"], PASSPHRASE, MEMORY_KB, dry_run, False) == 1
        assert storage_keys.data_prefix("dev-a") == "data/dev-a/"
        assert listings == ["data/dev-a/"]
        assert backend.exists(own) == dry_run
        assert backend.get(peer) == b"peer"

    @pytest.mark.parametrize("device_id", ["", ".", "..", "dev/a", "dev\\a", "dev\x00a"])
    def test_invalid_owner_cannot_reach_collection(self, push_preview56, monkeypatch, device_id):
        config = {**push_preview56["config"], "device": {"id": device_id, "name": "Invalid"}}
        calls = []

        def forbidden(*args, **kwargs):
            calls.append(args)
            raise AssertionError("invalid identity reached storage collection")

        monkeypatch.setattr(LocalBackend, "list_keys", forbidden)
        monkeypatch.setattr(LocalBackend, "delete", forbidden)
        monkeypatch.setattr(cli_module.retention, "_sweep_local_tmp_files", forbidden)
        with pytest.raises(ValueError, match="storage key: device_id"):
            cli_module._do_gc(config, PASSPHRASE, MEMORY_KB, False, False)
        assert calls == []

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_overbroad_listing_preserves_foreign_and_malformed_keys(
        self, push_preview56, monkeypatch, dry_run, capsys
    ):
        env = push_preview56
        backend = env["backend"]
        self._store_manifest(backend, "dev-a")
        own = storage_keys.blob_key("dev-a", hashlib.sha256(b"orphan").hexdigest())
        foreign = storage_keys.blob_key("elsewhere", hashlib.sha256(b"foreign").hexdigest())
        prefix_peer = storage_keys.blob_key("dev-ab", hashlib.sha256(b"peer").hexdigest())
        malformed = "data/dev-a/[/red]inject[red]\x1b]52;c;eA==\x07.enc"
        non_blob = "data/dev-a/notes.txt"
        keys = (own, foreign, prefix_peer, malformed, non_blob)
        for key in keys:
            backend.put(key, b"preserve unless own orphan")

        def list_keys(storage, prefix):
            assert prefix == "data/dev-a/"
            return list(keys)

        monkeypatch.setattr(LocalBackend, "list_keys", list_keys)
        assert cli_module._do_gc(env["config"], PASSPHRASE, MEMORY_KB, dry_run, True) == 1
        output = capsys.readouterr().out
        assert "malformed (skipped)" in output and "inject" in output
        assert "\x1b" not in output and "\x07" not in output
        assert foreign not in output and prefix_peer not in output
        assert backend.exists(own) == dry_run
        for key in keys[1:]:
            assert backend.get(key) == b"preserve unless own orphan"

    @pytest.mark.parametrize("peer_state", ["missing", "corrupt", "newer"])
    def test_peer_metadata_and_registry_are_not_gc_dependencies(
        self, push_preview56, monkeypatch, peer_state
    ):
        env = push_preview56
        backend = env["backend"]
        self._store_manifest(backend, "dev-a")
        own = storage_keys.blob_key("dev-a", hashlib.sha256(b"own orphan").hexdigest())
        peer = storage_keys.blob_key("peer", hashlib.sha256(b"peer").hexdigest())
        backend.put(own, b"own")
        backend.put(peer, b"peer")
        if peer_state != "missing":
            backend.put(
                storage_keys.manifest_key("peer"), b"\x03" if peer_state == "newer" else b"bad"
            )
        original = LocalBackend.get

        def get(storage, key):
            assert not key.startswith(("devices/", "manifests/peer/"))
            return original(storage, key)

        def forbidden(*args, **kwargs):
            raise AssertionError("GC read the peer registry")

        monkeypatch.setattr(LocalBackend, "get", get)
        monkeypatch.setattr(cli_module, "_list_devices_warn", forbidden)
        assert cli_module._do_gc(env["config"], PASSPHRASE, MEMORY_KB, False, False) == 1
        assert not backend.exists(own)
        assert backend.get(peer) == b"peer"

    def test_gc_keeps_raw_nested_checkout_references(self, push_preview56):
        env = push_preview56
        pushed = runner.invoke(app, ["push"])
        assert pushed.exit_code == 0, pushed.output
        own = cli_module.sidecar.read("dev-a")
        hashes = [info["sha256"] for info in own["sources"]["claude"]["files"].values()]
        (env["claude"] / "projects/-Users-kb-myapp/.git").mkdir()
        before = {
            storage_keys.blob_key("dev-a", sha): env["backend"].get(
                storage_keys.blob_key("dev-a", sha)
            )
            for sha in hashes
        }
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output
        assert {key: env["backend"].get(key) for key in before} == before

    @pytest.mark.parametrize("dry_run", [False, True])
    @pytest.mark.parametrize("copies", [1, 2])
    @pytest.mark.parametrize("canonical", ["unreadable", "absent"])
    def test_conflict_copies_alone_never_authorize_own_collection(
        self, push_preview56, monkeypatch, canonical, copies, dry_run
    ):
        env = push_preview56
        backend = env["backend"]
        older = hashlib.sha256(b"older reference").hexdigest()
        current = hashlib.sha256(b"current-only reference").hexdigest()
        self._store_manifest(backend, "dev-a", (older,))
        key = storage_keys.manifest_key("dev-a")
        # Older iCloud conflict copies still load; the current canonical does not.
        for n in range(2, 2 + copies):
            path = backend.root / Path(key).with_name(f"manifest.json {n}.enc")
            path.write_bytes(backend.get(key))
        if canonical == "absent":
            backend.delete(key)
        else:
            backend.put(key, b"\x02" + b"unreadable current manifest" * 4)
        blobs = {storage_keys.blob_key("dev-a", sha): sha.encode() for sha in (older, current)}
        for blob, data in blobs.items():
            backend.put(blob, data)
        listings = []
        original = LocalBackend.list_keys

        def list_keys(storage, prefix):
            listings.append(prefix)
            return original(storage, prefix)

        monkeypatch.setattr(LocalBackend, "list_keys", list_keys)
        result = runner.invoke(app, ["gc"] + (["--dry-run"] if dry_run else []))
        assert {blob: backend.get(blob) for blob in blobs} == blobs
        assert result.exit_code == 1, result.output
        flat = " ".join(result.output.split())
        assert "held only by iCloud conflict copies" in flat and "Mac A (dev-a)" in flat
        assert "corrupt" not in flat
        assert listings == []
        if dry_run:
            _assert_preview_refused(result)
        fetched = cli_module._fetch_remote_manifest(backend, "dev-a", PASSPHRASE, MEMORY_KB)
        assert fetched.status == "ok" and fetched.conflict_copies_only

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_canonical_with_own_conflict_copy_collects_with_union(self, push_preview56, dry_run):
        backend = push_preview56["backend"]
        canon, copy_only, orphan = (
            hashlib.sha256(name).hexdigest() for name in (b"canon", b"copy only", b"orphan")
        )
        # The older copy also lists copy_only; the readable canonical dropped it.
        self._store_manifest(backend, "dev-a", (canon, copy_only))
        key = storage_keys.manifest_key("dev-a")
        (backend.root / Path(key).with_name("manifest.json 2.enc")).write_bytes(backend.get(key))
        self._store_manifest(backend, "dev-a", (canon,))
        blobs = {storage_keys.blob_key("dev-a", sha): sha.encode() for sha in (canon, copy_only)}
        orphan_key = storage_keys.blob_key("dev-a", orphan)
        for blob, data in [*blobs.items(), (orphan_key, b"orphan")]:
            backend.put(blob, data)
        fetched = cli_module._fetch_remote_manifest(backend, "dev-a", PASSPHRASE, MEMORY_KB)
        assert fetched.status == "ok" and not fetched.conflict_copies_only
        result = runner.invoke(app, ["gc"] + (["--dry-run"] if dry_run else []))
        assert result.exit_code == 0, result.output
        assert "cannot GC safely" not in result.output
        # Peers pull the merged copies, so a copy-only reference stays live.
        assert {blob: backend.get(blob) for blob in blobs} == blobs
        assert backend.exists(orphan_key) == dry_run

    def test_push_and_gc_help_describe_own_device_gc(self):
        import re

        def plain(output):
            # CI renders Rich help in color; strip SGR codes before matching.
            text = re.sub(r"\x1b\[[0-9;]*m", "", output)
            return " ".join(text.replace("│", " ").split())

        result = runner.invoke(app, ["push", "--help"])
        assert result.exit_code == 0, result.output
        flat = plain(result.output)
        assert "run GC on this device's orphaned blobs" in flat
        assert "fleet-wide" not in flat
        # README Upgrading treats this marker as proof of an own-device build.
        gc_help = runner.invoke(app, ["gc", "--help"])
        assert gc_help.exit_code == 0, gc_help.output
        gc_flat = plain(gc_help.output)
        assert "Collect this device's orphaned blobs" in gc_flat
        assert "without deleting (this device's blobs only)" in gc_flat

    @pytest.mark.parametrize("canonical", ["unreadable", "absent"])
    def test_push_heals_conflict_copies_only_so_gc_can_resume(self, push_preview56, canonical):
        env = push_preview56
        backend = env["backend"]
        pushed = runner.invoke(app, ["push"])
        assert pushed.exit_code == 0, pushed.output
        key = storage_keys.manifest_key("dev-a")
        conflict = backend.root / Path(key).with_name("manifest.json 2.enc")
        conflict.write_bytes(backend.get(key))
        if canonical == "absent":
            backend.delete(key)
        else:
            backend.put(key, b"\x02" + b"unreadable current manifest" * 4)
        orphan = storage_keys.blob_key("dev-a", hashlib.sha256(b"own orphan").hexdigest())
        backend.put(orphan, b"own orphan")
        refused = runner.invoke(app, ["gc"])
        assert refused.exit_code == 1, refused.output
        assert "Run 'mm push' on this device" in " ".join(refused.output.split())
        # The remedy the refusal names must work without any content change.
        healed = runner.invoke(app, ["push"])
        assert healed.exit_code == 0, healed.output
        assert "Push: no content changes" not in healed.output
        healed_flat = " ".join(healed.output.split())
        assert "Push: manifest repaired." in healed_flat
        fetched = cli_module._fetch_remote_manifest(backend, "dev-a", PASSPHRASE, MEMORY_KB)
        assert fetched.status == "ok" and not fetched.conflict_copies_only
        assert not conflict.exists()
        live = [
            storage_keys.blob_key("dev-a", info["sha256"])
            for info in fetched.manifest["sources"]["claude"]["files"].values()
        ]
        collected = runner.invoke(app, ["gc"])
        assert collected.exit_code == 0, collected.output
        assert not backend.exists(orphan)
        assert all(backend.exists(blob) for blob in live)

    @pytest.mark.parametrize("canonical", ["unreadable", "absent"])
    def test_conflict_copies_only_heal_keeps_sidecar_deletions(self, push_preview56, canonical):
        env = push_preview56
        backend = env["backend"]
        assert runner.invoke(app, ["push"]).exit_code == 0
        key = storage_keys.manifest_key("dev-a")
        older = backend.get(key)
        # Created and deleted after the copy that will survive alone.
        gone = env["claude"] / "projects/-Users-kb-myapp/memory/gone.md"
        gone.write_text("created after the older copy")
        assert runner.invoke(app, ["push"]).exit_code == 0
        gone.unlink()
        assert runner.invoke(app, ["push"]).exit_code == 0
        tombstone = "claude:projects/-Users-kb-myapp/memory/gone.md"
        assert tombstone in cli_module.sidecar.read("dev-a")["tombstones"]
        (backend.root / Path(key).with_name("manifest.json 2.enc")).write_bytes(older)
        if canonical == "absent":
            backend.delete(key)
        else:
            backend.put(key, b"\x02" + b"unreadable current manifest" * 4)
        healed = runner.invoke(app, ["push"])
        assert healed.exit_code == 0, healed.output
        fetched = cli_module._fetch_remote_manifest(backend, "dev-a", PASSPHRASE, MEMORY_KB)
        assert fetched.status == "ok" and not fetched.conflict_copies_only
        # The rewrite and the refreshed sidecar both keep the deletion record.
        assert tombstone in fetched.manifest["tombstones"]
        assert tombstone in cli_module.sidecar.read("dev-a")["tombstones"]
        assert "deletion records from the local sidecar" in " ".join(healed.output.split())
        assert cli_module.sidecar.read("dev-a")["timestamp"] == fetched.manifest["timestamp"]

    def test_conflict_copies_only_heal_freezes_only_stored_blobs(self, push_preview56):
        env = push_preview56
        backend = env["backend"]
        assert runner.invoke(app, ["push"]).exit_code == 0
        key = storage_keys.manifest_key("dev-a")
        older = backend.get(key)
        late = env["claude"] / "projects/-Users-kb-myapp/memory/late.md"
        late.write_text("pushed after the surviving copy")
        assert runner.invoke(app, ["push"]).exit_code == 0
        late_sha = cli_module.sidecar.read("dev-a")["sources"]["claude"]["files"][
            "projects/-Users-kb-myapp/memory/late.md"
        ]["sha256"]
        # Lost with the canonical; the project then becomes a frozen checkout.
        backend.delete(storage_keys.blob_key("dev-a", late_sha))
        (env["claude"] / "projects/-Users-kb-myapp/.git").mkdir()
        (backend.root / Path(key).with_name("manifest.json 2.enc")).write_bytes(older)
        backend.put(key, b"\x02" + b"unreadable current manifest" * 4)
        healed = runner.invoke(app, ["push"])
        assert healed.exit_code == 0, healed.output
        fetched = cli_module._fetch_remote_manifest(backend, "dev-a", PASSPHRASE, MEMORY_KB)
        published = fetched.manifest["sources"]["claude"]["files"]
        assert "projects/-Users-kb-myapp/memory/late.md" not in published
        assert all(
            backend.exists(storage_keys.blob_key("dev-a", info["sha256"]))
            for info in published.values()
        )

    @pytest.mark.parametrize("late", ["missing", "corrupt", "unreadable", "more-refs"])
    def test_pre_delete_recheck_requires_a_clean_own_manifest(
        self, push_preview56, monkeypatch, late
    ):
        env = push_preview56
        backend = env["backend"]
        live, late_ref, orphan = (
            hashlib.sha256(name).hexdigest() for name in (b"live", b"late ref", b"orphan")
        )
        self._store_manifest(backend, "dev-a", (live,))
        blobs = {
            storage_keys.blob_key("dev-a", sha): sha.encode() for sha in (live, late_ref, orphan)
        }
        for blob, data in blobs.items():
            backend.put(blob, data)
        original = cli_module._fetch_remote_manifest
        calls = []

        def fetch(storage, device_id, passphrase, memory_kb):
            calls.append(device_id)
            if len(calls) == 1:
                return original(storage, device_id, passphrase, memory_kb)
            if late in ("missing", "corrupt"):
                return cli_module.ManifestFetch(status=late)
            files = {"late.md": {"sha256": late_ref, "size": 1, "mtime": None}}
            fields = {"conflict_copies_only": True} if late == "unreadable" else {}
            manifest = {"sources": {"late": {"files": files}}, "tombstones": {}}
            return cli_module.ManifestFetch(status="ok", manifest=manifest, **fields)

        monkeypatch.setattr(cli_module, "_fetch_remote_manifest", fetch)
        if late in ("corrupt", "unreadable"):
            with pytest.raises(typer.Exit):
                cli_module._do_gc(env["config"], PASSPHRASE, MEMORY_KB, False, False)
        else:
            expected = 1 if late == "more-refs" else 0
            assert cli_module._do_gc(env["config"], PASSPHRASE, MEMORY_KB, False, False) == expected
        assert calls == ["dev-a", "dev-a"]
        orphan_key = storage_keys.blob_key("dev-a", orphan)
        survivors = dict(blobs)
        if late == "more-refs":
            del survivors[orphan_key]
            assert not backend.exists(orphan_key)
        assert {blob: backend.get(blob) for blob in survivors} == survivors

    @pytest.mark.parametrize("state", ["corrupt", "missing"])
    def test_own_label_is_escaped_once(self, push_preview56, monkeypatch, state):
        from io import StringIO

        from rich.console import Console

        env = push_preview56
        if state == "corrupt":
            env["backend"].put(storage_keys.manifest_key("dev-a"), b"corrupt")
        config = {**env["config"], "device": {**env["config"]["device"], "name": "Mac [work]"}}
        buf = StringIO()
        monkeypatch.setattr(
            cli_module, "stderr_console", Console(file=buf, width=300, color_system=None)
        )
        if state == "corrupt":
            with pytest.raises(typer.Exit):
                cli_module._do_gc(config, PASSPHRASE, MEMORY_KB, False, False)
        else:
            assert cli_module._do_gc(config, PASSPHRASE, MEMORY_KB, False, False) == 0
        assert "Mac [work] (dev-a)" in buf.getvalue()
        assert "\\[work]" not in buf.getvalue()


class TestAutoCommands:
    def test_autopull_no_config_exits_silently(self, tmp_path, monkeypatch):
        """autopull should exit silently when mm is not initialized.

        Pins the silent-mode contract: a fresh Mac with no config must produce
        zero stderr noise on every Claude Code session start. Patches only the
        source module's CONFIG_PATH — the silent-mode preflight in
        `_auto_command_setup` MUST go through module-attribute access for this
        single-patch to take effect, otherwise the broken cli.py local-binding
        regresses and `load_config` raises ConfigError("init: config not
        found ...") which surfaces as `mm: pull failed - ...` on stderr.
        """
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "nonexistent.toml")
        # Breadcrumb lives under sidecar.SIDECAR_DIR — redirect for isolation.
        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0
        assert result.output == ""
        assert (result.stderr or "") == ""
        # Breadcrumb confirms the silent-exit went through the config-missing
        # branch, not the broader exception-fallback path.
        breadcrumb_path = sidecar_dir / "last-autorun.json"
        assert breadcrumb_path.exists()
        breadcrumb = json.loads(breadcrumb_path.read_text())
        assert breadcrumb["pull"]["outcome"] == "config-missing"

    def test_autopush_no_config_exits_silently(self, tmp_path, monkeypatch):
        """autopush silent-mode twin of test_autopull_no_config_exits_silently."""
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "nonexistent.toml")
        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert result.output == ""
        assert (result.stderr or "") == ""
        breadcrumb_path = sidecar_dir / "last-autorun.json"
        assert breadcrumb_path.exists()
        breadcrumb = json.loads(breadcrumb_path.read_text())
        assert breadcrumb["push"]["outcome"] == "config-missing"

    def test_autopull_bad_config_prints_stderr_and_exits_zero(self, tmp_path, monkeypatch):
        """Regression for eager validation: a config file that exists but has
        invalid sync.sources must NOT be silently swallowed — autopull should
        emit a one-line stderr and exit cleanly (Claude Code hook must see the
        failure instead of sync just stopping forever)."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "storage"}"\n'
            "[[sync.sources]]\n"
            'name = "claude"\n'
            'type = "claude"\n'
            # no path — eager validation catches it
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0
        assert "mm: pull failed" in result.stderr
        assert "missing required field" in result.stderr

    def test_autopush_bad_config_prints_stderr_and_exits_zero(self, tmp_path, monkeypatch):
        """Regression for eager validation: autopush must surface bad-config errors
        on stderr rather than silently swallowing them."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "storage"}"\n'
            "[[sync.sources]]\n"
            'name = "claude"\n'
            'type = "claude"\n'
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert "mm: push failed" in result.stderr

    def test_autopull_keyring_backend_error_breadcrumb(self, tmp_path, monkeypatch):
        """Regression: when crypto.get_passphrase propagates a non-KeyringError
        (OSError from locked keychain, RuntimeError from a broken DBus backend),
        the autopull hook must still honor the one-line-stderr + breadcrumb
        contract. Pre-v0.8.9's wide catch swallowed everything here; v0.8.9
        narrowed it, and this test pins the follow-through fix in
        _auto_command_setup that converts the propagating exception into a
        visible `keyring-error` breadcrumb outcome."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "storage"}"\n'
            "[[sync.sources]]\n"
            'name = "claude"\n'
            'type = "claude"\n'
            f'path = "{tmp_path / ".claude"}"\n'
        )
        (tmp_path / ".claude").mkdir()
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        # Breadcrumb lives under sidecar.SIDECAR_DIR — redirect for isolation.
        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        # Force the narrowed get_passphrase to propagate a non-KeyringError.
        import mind_meld.cli as cli_mod

        def boom(*_a, **_kw):
            raise RuntimeError("dbus session bus went away")

        monkeypatch.setattr(cli_mod, "get_passphrase", boom)
        monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)

        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0, result.output
        assert "mm: pull failed - keyring error" in result.stderr
        # Breadcrumb landed with the keyring-error outcome.
        breadcrumb_path = sidecar_dir / "last-autorun.json"
        assert breadcrumb_path.exists()
        breadcrumb = json.loads(breadcrumb_path.read_text())
        assert breadcrumb["pull"]["outcome"] == "keyring-error"
        assert breadcrumb["pull"]["detail"] == "RuntimeError"

    def test_interactive_command_surfaces_keyring_backend_failure(self, tmp_path, monkeypatch):
        """Regression: interactive commands (mm push / pull / diff / gc) must
        not traceback when crypto.get_passphrase leaks a non-KeyringError.
        _get_passphrase_or_exit now routes these through _error() so the
        user sees the one-line red banner and a clean exit(1)."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "storage"}"\n'
            "[[sync.sources]]\n"
            'name = "claude"\n'
            'type = "claude"\n'
            f'path = "{tmp_path / ".claude"}"\n'
        )
        (tmp_path / ".claude").mkdir()
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        import mind_meld.cli as cli_mod

        def boom(*_a, **_kw):
            raise OSError("keychain is locked")

        monkeypatch.setattr(cli_mod, "get_passphrase", boom)

        result = runner.invoke(app, ["push"])
        assert result.exit_code == 1
        # Error banner present, no raw traceback.
        assert "keyring backend failure" in result.output + (result.stderr or "")
        assert "Traceback" not in result.output + (result.stderr or "")
        # Value: protects=attended sync names the keyring exception class, never its message;
        # fails_when=the sync-scope branch echoes str(e) again; why_new=only presence was pinned;
        # seam=none; no_claim=real keychain behavior (get_passphrase faked)
        last = result.stderr.splitlines()[-1]
        assert last.startswith(
            "Push failed [authentication-error]: keyring backend failure: OSError"
        )
        assert last.endswith("Run: mm diag")
        assert "keychain is locked" not in result.output

    def test_init_tolerates_keyring_write_exception(self, tmp_path, monkeypatch):
        """Regression: _register_and_save must not abort on a non-KeyringError
        from store_passphrase_in_keyring after register + config are already
        committed. Old wide `except Exception` inside the helper swallowed
        everything; v0.8.9's narrowing made non-KeyringError propagate. The
        follow-through fix wraps the call at the _register_and_save call site
        so init degrades to the env-var-fallback path cleanly instead of
        leaving the user half-initialized with an uncaught traceback.

        (Function renamed from `_save_and_register` to `_register_and_save`
        in Track 5D / v0.9.4 when the order swapped to register-first.)"""
        config_path = tmp_path / "config.toml"
        storage = tmp_path / "icloud"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        # Force store_passphrase_in_keyring to leak a non-KeyringError.
        import mind_meld.cli as cli_mod

        def boom(*_a, **_kw):
            raise RuntimeError("keyring backend exploded")

        monkeypatch.setattr(cli_mod, "store_passphrase_in_keyring", boom)

        # Inputs match TestInitFlow pattern: storage, device name, passphrase,
        # confirm passphrase, then source prompts (Y claude, all others n).
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        # Init should complete despite the keyring write failure.
        assert result.exit_code == 0, result.output
        assert "Keyring backend error" in result.output
        assert "RuntimeError" in result.output
        # Env-var fallback path was surfaced, not a traceback.
        assert "MINDMELD_PASSPHRASE" in result.output
        assert "Traceback" not in result.output
        # Config was saved (device id committed).
        assert config_path.exists()
        cfg_text = config_path.read_text()
        assert 'name = "Mac A"' in cfg_text

    def test_autopush_silent_when_lock_held(self, tmp_path, monkeypatch):
        """autopush must exit silently if another mm process holds the lock.

        Simulates the Claude Code hot-path where `mm autopush` and
        `mm autopull` can fire simultaneously — exactly one acquires
        the flock; the loser must not crash, bubble a traceback, or
        write junk to stdout."""

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory").mkdir(parents=True)
        (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md").write_text("x")

        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "dev-a", "name": "Mac A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "claude_dir": str(claude_dir),
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_dir), "type": "claude"},
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)

        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")

        # Redirect the lockfile to a per-test path so this doesn't
        # contaminate the user's real ~/.config/mind-meld/mind-meld.lock.
        test_lock = tmp_path / "test.lock"
        monkeypatch.setattr("mind_meld.config.LOCK_PATH", test_lock)
        monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", test_lock)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # Spawn a child that grabs the lock and holds it until stdin closes.
        repo_src = str(Path(__file__).parent.parent / "src")
        ready_marker = tmp_path / "child-ready"
        child_script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {repo_src!r})
            from pathlib import Path
            from mind_meld.lockfile import acquire_lock, release_lock

            lp = Path({str(test_lock)!r})
            acquire_lock(lp)
            Path({str(ready_marker)!r}).write_text("ready")
            sys.stdin.read()
            release_lock(lp)
        """).strip()

        child = subprocess.Popen(
            [sys.executable, "-c", child_script],
            stdin=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if ready_marker.exists():
                    break
                time.sleep(0.02)
            else:
                child.kill()
                pytest.fail("child did not become ready")

            # Now try autopush — must exit cleanly, not crash.
            result = runner.invoke(app, ["autopush"])
            assert result.exit_code == 0, (
                f"autopush must handle lock contention gracefully, "
                f"got exit={result.exit_code} output={result.output!r}"
            )
            # autopush is a silent command; a LockError must surface only
            # as a short stderr line, never a traceback.
            assert "Traceback" not in result.output
            assert "Traceback" not in (result.stderr or "")
        finally:
            if child.stdin:
                child.stdin.close()
            child.wait(timeout=5)

    def test_autopush_round_trip(self, tmp_path, monkeypatch):
        """autopush publishes changed bytes without a success announcement."""
        storage_dir = tmp_path / "storage"
        claude_dir_a = tmp_path / "machine_a" / ".claude"

        # Create memory files on machine A
        memory = claude_dir_a / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        # Write config for device A
        config_a_path = tmp_path / "config_a.toml"
        config_a = {
            "device": {"id": "dev-a", "name": "Mac A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "claude_dir": str(claude_dir_a),
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude_dir_a), "type": "claude"},
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config_a, config_a_path)

        # Register device A
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")

        # Monkeypatch config path and passphrase
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        # Run autopush
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert result.stdout == result.stderr == ""


class TestMultiSourceSync:
    """Integration tests for multi-source (v2 manifest) sync."""

    def _make_claude_dir(self, base: "Path") -> "Path":
        d = base / ".claude"
        memory = d / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")
        return d

    def _make_gstack_dir(self, base: "Path") -> "Path":
        d = base / ".gstack"
        projects = d / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (d / "config.yaml").write_text("version: 1")
        return d

    def _make_config(
        self, tmp_path, storage_dir, claude_dir, device_id, device_name, gstack_dir=None
    ):
        config_path = tmp_path / f"config_{device_id}.toml"
        sources = [
            {"name": "claude", "path": str(claude_dir), "type": "claude"},
        ]
        if gstack_dir is not None:
            sources.append(
                {
                    "name": "gstack",
                    "path": str(gstack_dir),
                    "type": "generic",
                    "include_dirs": ["projects"],
                    "include_files": ["config.yaml"],
                }
            )
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": sources,
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path, config

    def test_push_pull_multi_source(self, tmp_path, monkeypatch):
        """Push with both claude and gstack from A, pull to B. Both arrive.

        Pull writes to the base_path from the remote manifest, so both
        machines must share the same logical paths for claude/gstack.
        We simulate this by: A populates the shared dirs, pushes, then we
        clear the dirs and pull (acting as B).
        """
        storage_dir = tmp_path / "storage"

        # Shared paths (simulating both machines having ~/.claude, ~/.gstack)
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Phase 1: Machine A populates and pushes
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        projects = gstack_dir / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (gstack_dir / "config.yaml").write_text("version: 1")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert result.stdout == ""

        # Phase 2: Clear local dirs (simulating Machine B that starts empty)
        shutil.rmtree(str(claude_dir))
        claude_dir.mkdir(parents=True)
        shutil.rmtree(str(gstack_dir))
        gstack_dir.mkdir(parents=True)
        (gstack_dir / "projects").mkdir()

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)

        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0

        # Verify claude files arrived
        pulled_role = claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md"
        assert pulled_role.exists()
        assert pulled_role.read_text() == "Data scientist"

        # Verify gstack files arrived
        pulled_state = gstack_dir / "projects" / "state.yaml"
        assert pulled_state.exists()
        assert pulled_state.read_text() == "active: true"

        pulled_config = gstack_dir / "config.yaml"
        assert pulled_config.exists()
        assert pulled_config.read_text() == "version: 1"

    def test_pull_calls_fsync_dir_per_unique_parent(self, tmp_path, monkeypatch):
        """End-of-pull deferred durability: fsync_dir called once per
        unique parent directory that received a write. Verifies the
        durability is actually deferred (not per-file) AND actually
        happens (not dropped entirely)."""

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Seed 3 distinct parent dirs on the "sending" side.
        (claude_dir / "projects" / "-app-a" / "memory").mkdir(parents=True)
        (claude_dir / "projects" / "-app-a" / "memory" / "a.md").write_text("A")
        (claude_dir / "projects" / "-app-b" / "memory").mkdir(parents=True)
        (claude_dir / "projects" / "-app-b" / "memory" / "b.md").write_text("B")
        (gstack_dir / "projects").mkdir(parents=True)
        (gstack_dir / "projects" / "state.yaml").write_text("x")
        (gstack_dir / "config.yaml").write_text("v: 1")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )
        backend = LocalBackend(storage_dir)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        runner.invoke(app, ["autopush"])

        # Wipe local dirs to simulate Machine B.
        shutil.rmtree(str(claude_dir))
        claude_dir.mkdir(parents=True)
        shutil.rmtree(str(gstack_dir))
        gstack_dir.mkdir(parents=True)
        (gstack_dir / "projects").mkdir()

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)

        # Spy: capture every fsync_dir call Pull triggers.
        calls: list[Path] = []
        real_fsync_dir = fsutil.fsync_dir

        def spy_fsync_dir(path):
            calls.append(Path(path))
            real_fsync_dir(path)

        # cli.py does `from mind_meld import fsutil` at import time and
        # calls `fsutil.fsync_dir(...)` through that alias, so patch the
        # attribute on cli.fsutil (which IS the same module object).
        monkeypatch.setattr(cli_module.fsutil, "fsync_dir", spy_fsync_dir)

        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0

        # At least 4 unique parent dirs should have been written to during pull:
        #   <claude>/projects/-app-a/memory  (a.md)
        #   <claude>/projects/-app-b/memory  (b.md)
        #   <gstack>/projects                (state.yaml)
        #   <gstack>                         (config.yaml)
        # Synclog may add ".mind-meld-log.md" writes but those are internal.
        unique_parents = set(calls)
        expected = {
            claude_dir / "projects" / "-app-a" / "memory",
            claude_dir / "projects" / "-app-b" / "memory",
            gstack_dir / "projects",
            gstack_dir,
        }
        missing = expected - unique_parents
        assert not missing, (
            f"expected fsync_dir calls for {expected}, missing {missing}. actual: {unique_parents}"
        )
        # Deferred-durability invariant: fsync_dir called exactly once per
        # unique parent (not per file).
        assert len(calls) == len(unique_parents)

    def test_jsonl_merge_on_pull(self, tmp_path, monkeypatch):
        """JSONL files are merged (not overwritten) on pull."""
        storage_dir = tmp_path / "storage"

        # Shared path (both machines see same ~/.claude)
        claude_dir = tmp_path / ".claude"
        memory = claude_dir / "projects" / "-app" / "memory"
        memory.mkdir(parents=True)

        # Phase 1: Machine A has lines 1-3, pushes
        lines_a = [
            '{"ts":"2026-01-01T00:00:00Z","key":"line1"}',
            '{"ts":"2026-01-02T00:00:00Z","key":"line2"}',
            '{"ts":"2026-01-03T00:00:00Z","key":"line3"}',
        ]
        (memory / "learnings.jsonl").write_text("\n".join(lines_a) + "\n")

        config_a_path, _ = self._make_config(tmp_path, storage_dir, claude_dir, "dev-a", "Mac A")

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Phase 2: Machine B has lines 1-2 + line4 locally, pulls A's data
        lines_b = [
            '{"ts":"2026-01-01T00:00:00Z","key":"line1"}',
            '{"ts":"2026-01-02T00:00:00Z","key":"line2"}',
            '{"ts":"2026-01-04T00:00:00Z","key":"line4"}',
        ]
        (memory / "learnings.jsonl").write_text("\n".join(lines_b) + "\n")

        config_b_path, _ = self._make_config(tmp_path, storage_dir, claude_dir, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0

        # B should have all 4 lines merged
        merged_text = (memory / "learnings.jsonl").read_text()
        merged_lines = [line for line in merged_text.strip().splitlines() if line.strip()]
        keys = set()
        for line in merged_lines:
            obj = json.loads(line)
            keys.add(obj["key"])
        assert keys == {"line1", "line2", "line3", "line4"}
        assert len(merged_lines) == 4

    def _prepare_mixed_timestamp_fleet(self, tmp_path, monkeypatch):
        """Push mixed-ts JSONL + ordinary file from A; B has a local-only line."""
        from mind_meld import sidecar as sidecar_mod

        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        jsonl_dir = claude_dir / "projects" / "-app" / "memory"
        notes_dir = claude_dir / "projects" / "-other" / "memory"
        jsonl_dir.mkdir(parents=True)
        notes_dir.mkdir(parents=True)
        jsonl_path = jsonl_dir / "learnings.jsonl"
        notes_path = notes_dir / "notes.md"
        jsonl_path.write_bytes(_MIXED_REMOTE_JSONL)
        notes_path.write_bytes(_ORDINARY_PEER_BYTES)

        config_a_path, _ = self._make_config(tmp_path, storage_dir, claude_dir, "dev-a", "Mac A")
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        pushed = runner.invoke(app, ["autopush"])
        assert pushed.exit_code == 0, pushed.output

        shutil.rmtree(str(claude_dir))
        jsonl_dir.mkdir(parents=True)
        jsonl_path.write_bytes(_LOCAL_ONLY_JSONL)

        config_b_path, _ = self._make_config(tmp_path, storage_dir, claude_dir, "dev-b", "Mac B")
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        return {
            "jsonl_path": jsonl_path,
            "notes_path": notes_path,
            "jsonl_parent": jsonl_dir,
            "notes_parent": notes_dir,
            "sidecar_dir": sidecar_mod.SIDECAR_DIR,
        }

    @pytest.mark.parametrize("command", ["pull", "autopull"])
    def test_mixed_timestamp_pull_bookkeeping_and_retry(self, tmp_path, monkeypatch, command):
        """Full CLI: mixed JSONL merges, ordinary file arrives, retry is quiet."""
        from mind_meld import pullhistory

        env = self._prepare_mixed_timestamp_fleet(tmp_path, monkeypatch)
        jsonl_path = env["jsonl_path"]
        notes_path = env["notes_path"]

        calls: list[Path] = []
        real_fsync_dir = fsutil.fsync_dir

        def spy_fsync_dir(path):
            calls.append(Path(path))
            real_fsync_dir(path)

        monkeypatch.setattr(cli_module.fsutil, "fsync_dir", spy_fsync_dir)

        first = runner.invoke(app, [command])
        assert first.exit_code == 0, first.output
        combined = (first.output or "") + (first.stderr or "")
        assert jsonl_path.read_bytes() == _MERGED_MIXED_JSONL
        assert notes_path.read_bytes() == _ORDINARY_PEER_BYTES
        assert "failed" not in combined.lower()
        assert "unexpected error" not in combined

        unique_parents = set(calls)
        expected_parents = {env["jsonl_parent"], env["notes_parent"]}
        missing = expected_parents - unique_parents
        assert not missing, f"missing fsync parents {missing}; actual {unique_parents}"

        records = list(pullhistory.read_records())
        pull_records = [r for r in records if r.get("verb") == "pull"]
        actions = {(r["rel_path"], r["action"]) for r in pull_records}
        assert ("projects/-app/memory/learnings.jsonl", "merged") in actions
        assert ("projects/-other/memory/notes.md", "written") in actions
        assert not any(r.get("action") == "failed" for r in pull_records)

        # Sync log: the merged JSONL is listed as updated in its project and
        # the ordinary file as new in its own project.
        app_log = (env["jsonl_parent"].parent / ".mind-meld-log.md").read_text()
        assert "## Updated from other machine" in app_log
        assert "- memory/learnings.jsonl" in app_log
        other_log = (env["notes_parent"].parent / ".mind-meld-log.md").read_text()
        assert "## New from other machine" in other_log
        assert "- memory/notes.md" in other_log

        if command == "autopull":
            breadcrumb = json.loads((env["sidecar_dir"] / "last-autorun.json").read_text())
            assert breadcrumb["pull"]["outcome"] == "success"

        merged_mtime = jsonl_path.stat().st_mtime_ns
        first_merged_count = sum(
            1
            for r in pull_records
            if r.get("rel_path") == "projects/-app/memory/learnings.jsonl"
            and r.get("action") == "merged"
        )

        second = runner.invoke(app, [command])
        assert second.exit_code == 0, second.output
        second_out = (second.output or "") + (second.stderr or "")
        assert jsonl_path.read_bytes() == _MERGED_MIXED_JSONL
        assert jsonl_path.stat().st_mtime_ns == merged_mtime
        assert notes_path.read_bytes() == _ORDINARY_PEER_BYTES
        assert "merged" not in second_out.lower()
        if command == "autopull":
            breadcrumb = json.loads((env["sidecar_dir"] / "last-autorun.json").read_text())
            assert breadcrumb["pull"]["outcome"] == "success"

        retry_records = [
            r
            for r in pullhistory.read_records()
            if r.get("verb") == "pull"
            and r.get("rel_path") == "projects/-app/memory/learnings.jsonl"
            and r.get("action") == "merged"
        ]
        assert len(retry_records) == first_merged_count

    def test_source_filter_on_pull(self, tmp_path, monkeypatch):
        """Pull with --source gstack only downloads gstack files."""
        storage_dir = tmp_path / "storage"

        # Shared paths
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Phase 1: Machine A populates both sources, pushes
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        projects = gstack_dir / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (gstack_dir / "config.yaml").write_text("version: 1")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Phase 2: Clear local dirs (Machine B starts empty)
        shutil.rmtree(str(claude_dir))
        claude_dir.mkdir(parents=True)
        shutil.rmtree(str(gstack_dir))
        gstack_dir.mkdir(parents=True)
        (gstack_dir / "projects").mkdir()

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        result = runner.invoke(app, ["pull", "--source", "gstack"])
        assert result.exit_code == 0

        # gstack files should be present
        assert (gstack_dir / "projects" / "state.yaml").exists()
        assert (gstack_dir / "config.yaml").exists()

        # claude files should NOT be present (source filter excluded them)
        assert not (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md").exists()

    def test_source_filter_deletion_scoping(self, tmp_path, monkeypatch):
        """Pulling --source gstack must not delete B's claude files.

        A has only gstack files (no claude). B has both. Pulling from A
        with --source gstack should update gstack but leave claude alone.
        """
        storage_dir = tmp_path / "storage"

        # Shared paths
        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Phase 1: Machine A has only gstack (no claude files), pushes
        claude_dir.mkdir(parents=True)  # exists but empty
        projects = gstack_dir / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (gstack_dir / "config.yaml").write_text("version: 1")

        config_a_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Phase 2: Machine B has both claude and gstack files locally
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        config_b_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-b", "Mac B", gstack_dir
        )

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b_path)
        result = runner.invoke(app, ["pull", "--source", "gstack"])
        assert result.exit_code == 0

        # B's claude files must still exist (not deleted by gstack-only pull)
        assert (claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md").exists()
        assert (
            claude_dir / "projects" / "-Users-kb-myapp" / "memory" / "role.md"
        ).read_text() == "Data scientist"

    def test_gc_with_v2_manifest(self, tmp_path, monkeypatch):
        """GC collects hashes from all sources in v2 manifests."""
        storage_dir = tmp_path / "storage"

        claude_dir = tmp_path / ".claude"
        gstack_dir = tmp_path / ".gstack"

        # Populate both sources
        memory = claude_dir / "projects" / "-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        projects = gstack_dir / "projects"
        projects.mkdir(parents=True)
        (projects / "state.yaml").write_text("active: true")
        (gstack_dir / "config.yaml").write_text("version: 1")

        config_path, _ = self._make_config(
            tmp_path, storage_dir, claude_dir, "dev-a", "Mac A", gstack_dir
        )

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")

        # Push (creates v2 manifest with both sources)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0

        # Plant an orphan blob. Post-1C: path must match the 64-hex sha
        # shape or _do_gc routes it through the malformed-count path.
        orphan_sha = "de" * 32  # 64-hex
        orphan_data = encrypt(b"orphan content", PASSPHRASE, memory_kb=MEMORY_KB)
        backend.put(f"data/dev-a/{orphan_sha}.enc", orphan_data)

        # Run GC
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0
        assert "1" in result.output  # 1 orphan deleted

        # Verify the orphan is gone
        assert not backend.exists(f"data/dev-a/{orphan_sha}.enc")

        # Verify referenced blobs still exist
        all_blobs = backend.list_keys("data/")
        assert len(all_blobs) >= 3  # at least role.md, state.yaml, config.yaml

    def test_backward_compat_v1_manifest(self, tmp_path):
        """V1 manifests (no "sources") should work via normalize_manifest."""
        v1_manifest = {
            "device_id": "old-device",
            "device_name": "Old Mac",
            "timestamp": "2026-01-01T00:00:00Z",
            "base_path": "/Users/alice/.claude",
            "files": {
                "projects/-myapp/memory/role.md": {
                    "sha256": "abc123",
                    "size": 100,
                    "mtime": "2026-01-01T00:00:00Z",
                },
                "projects/-myapp/todos/tasks.json": {
                    "sha256": "def456",
                    "size": 200,
                    "mtime": "2026-01-01T00:00:00Z",
                },
            },
        }

        # Serialize, then deserialize (simulating storage round-trip)
        data = serialize_manifest(v1_manifest)
        loaded = deserialize_manifest(data)

        # normalize should add sources
        normalize_manifest(loaded)

        assert "sources" in loaded
        assert "claude" in loaded["sources"]
        claude_src = loaded["sources"]["claude"]
        assert claude_src["base_path"] == "/Users/alice/.claude"
        assert len(claude_src["files"]) == 2
        assert claude_src["files"]["projects/-myapp/memory/role.md"]["sha256"] == "abc123"

        # Track 1B: normalize_manifest strips the top-level "files" key on
        # all paths (v1 promotion and v2 passthrough). The payload moves
        # into sources.claude.files — nothing is lost.
        assert "files" not in loaded


class TestInitFlow:
    """Integration tests for mm init paths: first-device, second-device,
    wrong-passphrase, bootstrap race, and convergence.

    Uses CliRunner with monkeypatched CONFIG_PATH. Passphrases are provided
    via stdin input rather than MINDMELD_PASSPHRASE env var, because init
    needs to actually prompt to exercise the double/single-prompt branching.
    """

    def _setup_monkeypatch(self, tmp_path, monkeypatch):
        """Isolate CONFIG_PATH and disable keyring."""

        cfg_path = tmp_path / "config_test.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
        # Make keyring a no-op so tests don't pollute the real Keychain.
        monkeypatch.setattr("mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False)
        # get_passphrase falls back to env; tests set MINDMELD_PASSPHRASE as needed.
        return cfg_path

    def test_first_device_init_bootstraps(self, tmp_path, monkeypatch):
        """Fresh storage: init prompts twice, bootstraps mm-crypto-init."""

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        # Inputs: storage path, device name, passphrase, confirm passphrase,
        # then per-source prompts (claude Y, all others n) — we
        # need at least one source enabled for init to succeed.
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        assert "bootstrapped" in result.output

        # mm-crypto-init exists at storage root.
        backend = LocalBackend(storage)
        assert backend.exists("mm-crypto-init")

        # Config has crypto.root_salt_fp populated.
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        assert "root_salt_fp" in cfg["crypto"]
        assert cfg["crypto"]["argon2_memory_kb"] == 65_536

    @pytest.mark.parametrize(
        ("customization_dir", "grok_enabled"),
        [(None, False), ("skills", True), ("commands", True), ("rules", True)],
    )
    def test_init_grok_default_requires_customization_dir(
        self, tmp_path, monkeypatch, customization_dir, grok_enabled
    ):
        """A stock Grok root is not consent; any allowlisted tree is default-Y."""
        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        grok_root = home / ".grok"
        grok_root.mkdir()
        if customization_dir is not None:
            (grok_root / customization_dir).mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(
            "mind_meld.skill_link._ensure_retro_skill_links",
            lambda *, dry_run=False, allow_mutate=True, explicit=False, may_create: (),
        )
        monkeypatch.setattr("mind_meld.events_tail._run_events_backfill", lambda *_args: None)

        grok_defaults: list[bool] = []

        def confirm(prompt: str, *, default: bool) -> bool:
            if "'grok'" in prompt:
                grok_defaults.append(default)
            return "'claude'" in prompt or default

        monkeypatch.setattr("mind_meld.cli.typer.confirm", confirm)
        storage = tmp_path / "icloud"
        result = runner.invoke(app, ["init"], input=f"{storage}\nMac A\npw123\npw123\n")
        assert result.exit_code == 0, result.output
        assert grok_defaults == [grok_enabled]

        with open(cfg_path, "rb") as f:
            config = tomllib.load(f)
        source_names = [source["name"] for source in config["sync"]["sources"]]
        assert ("grok" in source_names) is grok_enabled
        assert config.get("retro", {}).get("grok_host_usage") is not True

    def test_refuses_if_no_sources_enabled(self, tmp_path, monkeypatch):
        """User declines every source prompt → init refuses to finish.

        Behavior on first-device refuse-all (pinned here):
          * exit code != 0, clear error message
          * local config NOT written
          * mm-crypto-init IS written to storage (bootstrap ran before
            source prompt). This is benign — re-running init on the same
            storage hits the second-device verify path; the user's same
            passphrase verifies against their own earlier bootstrap. See
            test_first_device_refuse_all_is_recoverable below.

        Ordering rationale (see init() docstring): source prompt runs
        AFTER crypto bootstrap because the second-device wrong-passphrase
        path must fail fast, before we ask about sources. The cost is
        a benign orphan mm-crypto-init on first-device refuse-all.
        """

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        # Decline every user-facing source.
        stdin = f"{storage}\nMac A\npw123\npw123\nn\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0, result.output
        assert "no sync sources enabled" in result.output

        # Local config NOT written.
        assert not cfg_path.exists()
        # Storage-side bootstrap IS committed (documented behavior).
        backend = LocalBackend(storage)
        assert backend.exists("mm-crypto-init")

    def test_first_device_refuse_all_is_recoverable(self, tmp_path, monkeypatch):
        """First-device refuse-all leaves mm-crypto-init orphaned. Re-running
        init with the same passphrase should succeed via the second-device
        verify path (against the orphaned bootstrap).
        """

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        # First attempt: refuse all, leaves mm-crypto-init orphan.
        stdin1 = f"{storage}\nMac A\npw-shared\npw-shared\nn\nn\nn\nn\nn\nn\n"
        result1 = runner.invoke(app, ["init"], input=stdin1)
        assert result1.exit_code != 0

        # Second attempt: same passphrase, accept claude. Takes the
        # second-device path against the orphaned bootstrap.
        stdin2 = f"{storage}\nMac A\npw-shared\nY\nn\nn\nn\nn\nn\n"
        result2 = runner.invoke(app, ["init"], input=stdin2)
        assert result2.exit_code == 0, result2.output
        assert "Verified passphrase against existing mm-crypto-init" in result2.output

        # Config now written, with claude enabled. mm-events is mm-internal
        # infrastructure and auto-includes alongside any user-facing source.
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        assert [s["name"] for s in cfg["sync"]["sources"]] == ["claude", "mm-events"]

    def test_first_device_gstack_only_init(self, tmp_path, monkeypatch):
        """Decline claude, accept gstack → user-facing sources list has
        gstack only. mm-events auto-includes (mm-internal infrastructure)."""

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        # Decline claude, accept gstack, decline the other sources.
        stdin = f"{storage}\nMac A\npw123\npw123\nn\nY\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output

        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        names = [s["name"] for s in cfg["sync"]["sources"]]
        assert names == ["mm-events", "gstack"]
        # DEFAULT_SOURCES fields survive the indirection (Issue 1C regression pin).
        gstack = next(s for s in cfg["sync"]["sources"] if s["name"] == "gstack")
        assert "projects" in gstack["include_dirs"]
        assert "retro-context.md" in gstack["include_files"]
        # v0.9.3: exclude_patterns is also load-bearing now — pin that it
        # survives indirection AND contains the new config.yaml exclude.
        assert "config.yaml" in gstack["exclude_patterns"]

    def test_first_device_both_sources_init(self, tmp_path, monkeypatch):
        """Accept claude + gstack (decline gstack-extend) → final list has claude,
        mm-events, gstack."""

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        stdin = f"{storage}\nMac A\npw123\npw123\nY\nY\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output

        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        names = [s["name"] for s in cfg["sync"]["sources"]]
        assert names == ["claude", "mm-events", "gstack"]
        # Paths round-trip in tilde-form (Issue 1C regression pin: the
        # config must not silently rewrite ~/.claude to an absolute path).
        claude = next(s for s in cfg["sync"]["sources"] if s["name"] == "claude")
        assert claude["path"] == "~/.claude"

    def test_first_device_passphrase_mismatch_aborts(self, tmp_path, monkeypatch):
        """Passphrases don't match → abort, no state written."""

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        stdin = f"{storage}\nMac A\npw123\npw456\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0
        assert "don't match" in result.output or "don" in result.output

        # Storage was created (mkdir), but mm-crypto-init was NOT written.
        backend = LocalBackend(storage)
        assert not backend.exists("mm-crypto-init")
        # Config was NOT written.
        assert not cfg_path.exists()

    def test_second_device_init_verifies(self, tmp_path, monkeypatch):
        """Existing mm-crypto-init: init prompts once, verifies keycheck."""

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()

        # Pre-seed storage with mm-crypto-init bootstrapped at MEMORY_KB.
        backend = LocalBackend(storage)
        # Use reduced memory_kb for test speed; bootstrap writes it into the blob.
        bootstrap_crypto_init(backend, "pw-shared", argon2_memory_kb=MEMORY_KB)

        # Second-device init: only 1 passphrase prompt (single, no confirm),
        # then per-source prompts (claude Y, all others n).
        stdin = f"{storage}\nMac B\npw-shared\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        assert "Verified passphrase against existing mm-crypto-init" in result.output

        # Config's memory_kb comes from storage, not from 65_536 default.
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        assert cfg["crypto"]["argon2_memory_kb"] == MEMORY_KB

    def test_second_device_wrong_passphrase_aborts_cleanly(self, tmp_path, monkeypatch):
        """Wrong passphrase on second-device: abort, NO config or device registered."""

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()

        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, "correct-pw", argon2_memory_kb=MEMORY_KB)

        # Second device uses WRONG passphrase.
        stdin = f"{storage}\nMac B\nwrong-pw\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0
        assert "does not match" in result.output

        # No local state should have been written.
        assert not cfg_path.exists()
        # Storage devices/ should NOT have Mac B registered.
        devices_dir = storage / "devices"
        if devices_dir.exists():
            # Only the one device from bootstrap_crypto_init helper (which doesn't
            # register a device) — so there should be zero entries actually.
            assert list(devices_dir.iterdir()) == []

    def test_convergence_cross_device_via_conflict_copy(self, tmp_path, monkeypatch):
        """Simulate post-iCloud-reconciliation state: canonical + 'mm-crypto-init 2'.

        fetch_crypto_init picks the deterministic winner and canonicalizes.
        Subsequent commands interoperate as if only one init had happened.
        """

        self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)

        # Device A bootstraps with one passphrase.
        bootstrap_crypto_init(backend, "shared-pw", argon2_memory_kb=MEMORY_KB)
        # Simulate device B losing the race: its mm-crypto-init landed as
        # "mm-crypto-init 2". Build that blob manually.

        crypto_module.clear_crypto_session()
        other_salt = bytes([0xFF] * 16)
        crypto_module.set_crypto_session(other_salt, MEMORY_KB)
        other_mk = crypto_module.load_master_key("shared-pw", other_salt, MEMORY_KB)
        other_keycheck = crypto_module._encrypt_with_master_key(
            crypto_module._KEYCHECK_PLAINTEXT, other_mk
        )
        other_blob = (
            bytes([crypto_module.FORMAT_VERSION])
            + MEMORY_KB.to_bytes(4, "big")
            + other_salt
            + other_keycheck
        )
        (storage / "mm-crypto-init 2").write_bytes(other_blob)

        # Now fetch_crypto_init is called (as if we just started a command).
        # Lex-smallest salt wins. Our canonical salt is random; other_salt is 0xFF*16.
        fetched = crypto_module.fetch_crypto_init(backend)
        assert fetched.status == "ok"
        # Reading selects a winner without changing either lineage.
        assert (storage / "mm-crypto-init 2").read_bytes() == other_blob
        # Canonical now holds the winner (whichever of the two had the smaller salt).
        canonical_bytes = (storage / "mm-crypto-init").read_bytes()
        winner_salt_in_canonical = canonical_bytes[5:21]
        assert winner_salt_in_canonical == fetched.root_salt


class TestInitTwoTierGuard:
    """Group 2 Pre-flight 3: storage-occupancy-based re-init guard.

    Two tiers:
      * ORPHAN — mm-crypto-init ok + any occupancy: typer.confirm warn.
      * BRICK — mm-crypto-init missing + blobs/manifests: require typed BRICK.
    """

    def _setup(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config_test.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg)
        monkeypatch.setattr("mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False)
        return cfg

    def test_orphan_case_warns_and_confirms(self, tmp_path, monkeypatch):
        """Existing mm-crypto-init + existing blob + we answer 'y' to orphan
        prompt → init proceeds on the second-device path."""

        self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, "pw-shared", argon2_memory_kb=MEMORY_KB)
        # Seed a blob so occupancy.has_any_blobs is True.
        backend.put("data/oldpeer/decafbad.enc", b"stub-blob")

        # Inputs: storage path, orphan-confirm y, device name, passphrase,
        # per-source (claude Y, all others n).
        stdin = f"{storage}\ny\nMac B\npw-shared\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        # existing_device_id is None (no prior config in this test), so the
        # orphan prompt takes the "alongside existing devices" form.
        assert "alongside the existing devices" in result.output
        # Second-device verify completed.
        assert "Verified passphrase against existing mm-crypto-init" in result.output

    def test_orphan_case_abort_on_n_leaves_state_clean(self, tmp_path, monkeypatch):

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, "pw-shared", argon2_memory_kb=MEMORY_KB)
        backend.put("data/oldpeer/decafbad.enc", b"stub-blob")

        # Answer 'n' to the orphan prompt.
        stdin = f"{storage}\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0  # typer.Exit() without code is 0
        # Config not written.
        assert not cfg.exists()

    def test_brick_case_refuses_without_exact_typed_token(self, tmp_path, monkeypatch):
        """mm-crypto-init missing + blobs exist + user types wrong token."""

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        # Seed blobs but NO mm-crypto-init (simulating deletion / iCloud loss).
        backend.put("data/peer/cafebabe.enc", b"stub-blob")

        # Inputs: storage path, then WRONG token for BRICK.
        stdin = f"{storage}\nwhatever\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0, result.output
        assert "DANGER" in (result.stderr or "") + result.output
        # Config not written.
        assert not cfg.exists()
        # mm-crypto-init still not created.
        assert not backend.exists("mm-crypto-init")

    def test_brick_case_rejects_lowercase_brick(self, tmp_path, monkeypatch):
        """Case-sensitive match: 'brick' is NOT accepted."""

        cfg = self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        backend.put("manifests/peer/manifest.json.enc", b"stub-manifest")

        stdin = f"{storage}\nbrick\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code != 0
        assert not cfg.exists()
        assert not backend.exists("mm-crypto-init")

    def test_brick_case_accepts_exact_BRICK(self, tmp_path, monkeypatch):
        """Exact typed 'BRICK' proceeds to first-device bootstrap path."""

        self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        # Seed a manifest (no mm-crypto-init — our target scenario).
        backend.put("manifests/peer/manifest.json.enc", b"stub-manifest")

        # After BRICK, init continues on the first-device path:
        # device name, passphrase, confirm passphrase, per-source
        # (claude Y, all others n).
        stdin = f"{storage}\nBRICK\nMac A\npw-new\npw-new\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        # New mm-crypto-init bootstrapped.
        assert backend.exists("mm-crypto-init")

    def test_first_device_path_not_gated_on_empty_storage(self, tmp_path, monkeypatch):
        """Empty storage: no guard triggers, first-device path works normally.

        Regression guard: the two-tier logic must not fire on fresh init.
        """

        self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output
        # No orphan or BRICK output polluted the happy path.
        assert "orphaning" not in result.output
        assert "DANGER" not in (result.stderr or "") + result.output

    def test_devices_only_occupancy_triggers_orphan_not_brick(self, tmp_path, monkeypatch):
        """If only devices/ is populated (no blobs, no manifests, no
        mm-crypto-init), BRICK must NOT fire — no encrypted state is at risk.

        The guard should reach the orphan-case check, and since
        has_crypto_init is False, fall through to first-device path.
        """

        self._setup(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"
        storage.mkdir()
        backend = LocalBackend(storage)
        # Seed ONLY a devices/ entry (no data/, no manifests/).
        backend.put(
            "devices/stale.json",
            json.dumps({"device_id": "stale", "device_name": "stale-dev"}).encode(),
        )

        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        # BRICK did NOT fire (no typed token consumed from stdin).
        assert result.exit_code == 0, result.output
        assert "DANGER" not in (result.stderr or "") + result.output


class TestBackfillPreservesRawPaths:
    """Headline regression for Track 2B: first-run-after-upgrade backfill
    (crypto.root_salt_fp / argon2_memory_kb) must NOT silently rewrite the
    user's hand-written TOML paths. `~/.claude` stays `~/.claude`;
    symlinked storage roots stay symlinks. See ROADMAP.md Track 2B."""

    def _setup_config_and_storage(self, tmp_path, storage_path_value, storage_real_path):
        """Write a config pointing at a real storage dir, using storage_path_value
        as the raw storage.path field (which may be a symlink or tilde)."""

        claude_dir = tmp_path / "claude"
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "dev-a"\n'
            'name = "Mac A"\n'
            "[storage]\n"
            f'path = "{storage_path_value}"\n'
            "[sync]\n"
            f'claude_dir = "{claude_dir}"\n'
            "max_file_size = 52428800\n"
            "[[sync.sources]]\n"
            'name = "claude"\n'
            f'path = "{claude_dir}"\n'
            'type = "claude"\n'
            "[crypto]\n"
            f"argon2_memory_kb = {MEMORY_KB}\n"
            # NOTE: no root_salt_fp — backfill must fire
        )

        backend = LocalBackend(Path(storage_real_path))
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        return config_path

    def _run_autopush_with_config(self, config_path, monkeypatch):
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0, (
            f"autopush must succeed; got exit={result.exit_code} output={result.output!r}"
        )
        return result

    def test_symlinked_storage_path_preserved_through_backfill(self, tmp_path, monkeypatch):
        """CRITICAL REGRESSION: symlinked storage.path is NOT dereferenced
        by the crypto-init backfill save."""

        real_storage = tmp_path / "real_storage"
        real_storage.mkdir()
        symlink_storage = tmp_path / "link_storage"
        os.symlink(real_storage, symlink_storage)

        config_path = self._setup_config_and_storage(
            tmp_path,
            storage_path_value=str(symlink_storage),
            storage_real_path=str(real_storage),
        )
        self._run_autopush_with_config(config_path, monkeypatch)

        # Re-read raw TOML; the storage.path must still be the symlink
        # form the user wrote, NOT the resolved target.
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["storage"]["path"] == str(symlink_storage)
        assert raw["storage"]["path"] != str(real_storage.resolve())
        # Backfill DID fire — crypto fields added.
        assert "root_salt_fp" in raw["crypto"]
        assert raw["crypto"]["argon2_memory_kb"] == MEMORY_KB

    def test_sync_sources_array_preserved_through_backfill(self, tmp_path, monkeypatch):
        """Modern multi-source config: every sources[*].path string survives
        backfill byte-identical."""

        real_storage = tmp_path / "real_storage"
        real_storage.mkdir()
        symlink_storage = tmp_path / "link_storage"
        os.symlink(real_storage, symlink_storage)

        config_path = self._setup_config_and_storage(
            tmp_path,
            storage_path_value=str(symlink_storage),
            storage_real_path=str(real_storage),
        )
        self._run_autopush_with_config(config_path, monkeypatch)

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        # The claude source path was written verbatim; still exactly that.
        sources = raw["sync"]["sources"]
        assert len(sources) == 1
        assert sources[0]["name"] == "claude"
        assert sources[0]["type"] == "claude"
        # Path is a real absolute path in this test (can't use ~ safely in
        # integration tests because it points at the real home), but the
        # invariant we want is that it's unchanged from what the user wrote.
        original_claude_path = str(tmp_path / "claude")
        assert sources[0]["path"] == original_claude_path

    def test_tilde_storage_path_preserved_through_backfill(self, tmp_path, monkeypatch):
        """CRITICAL REGRESSION (end-to-end): a config with tilde-form paths
        survives the full `mm autopush` backfill flow. Monkeypatches HOME so
        `~/...` resolves inside the test sandbox."""

        monkeypatch.setenv("HOME", str(tmp_path))
        # Seed tilde-addressable locations: ~/real_storage and ~/claude
        real_storage = tmp_path / "real_storage"
        real_storage.mkdir()
        claude_dir = tmp_path / "claude"
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "role.md").write_text("Data scientist")

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "dev-a"\n'
            'name = "Mac A"\n'
            "[storage]\n"
            'path = "~/real_storage"\n'
            "[sync]\n"
            'claude_dir = "~/claude"\n'
            "max_file_size = 52428800\n"
            "[[sync.sources]]\n"
            'name = "claude"\n'
            'path = "~/claude"\n'
            'type = "claude"\n'
            "[crypto]\n"
            f"argon2_memory_kb = {MEMORY_KB}\n"
            # NOTE: no root_salt_fp — backfill must fire
        )

        backend = LocalBackend(Path(real_storage))
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")

        self._run_autopush_with_config(config_path, monkeypatch)

        # Re-read raw TOML: the tilde form must survive verbatim.
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["storage"]["path"] == "~/real_storage"
        assert raw["sync"]["claude_dir"] == "~/claude"
        assert raw["sync"]["sources"][0]["path"] == "~/claude"
        # Backfill DID fire — crypto fields added.
        assert "root_salt_fp" in raw["crypto"]

    def test_second_push_does_not_rewrite_config(self, tmp_path, monkeypatch):
        """Idempotency: after backfill, subsequent pushes must NOT touch the
        config file (no root_salt_fp drift, no mtime churn)."""

        real_storage = tmp_path / "real_storage"
        real_storage.mkdir()
        symlink_storage = tmp_path / "link_storage"
        os.symlink(real_storage, symlink_storage)

        config_path = self._setup_config_and_storage(
            tmp_path,
            storage_path_value=str(symlink_storage),
            storage_real_path=str(real_storage),
        )
        # First push: backfill fires and writes crypto fields.
        self._run_autopush_with_config(config_path, monkeypatch)
        with open(config_path, "rb") as f:
            raw_after_first = tomllib.load(f)

        # Second push: backfill MUST NOT fire (root_salt_fp already set).
        first_push_mtime = config_path.stat().st_mtime_ns
        self._run_autopush_with_config(config_path, monkeypatch)
        with open(config_path, "rb") as f:
            raw_after_second = tomllib.load(f)

        # Config content must be identical across second push.
        assert raw_after_first == raw_after_second
        # mtime stability is a strong signal — no write happened.
        assert config_path.stat().st_mtime_ns == first_push_mtime

    def test_backfill_survives_if_config_file_moved(self, tmp_path, monkeypatch):
        """Non-fatal: if the config file is missing when the backfill helper
        runs (user deleted it, or some concurrent process clobbered it after
        load_config read it into memory), the ConfigError swallow keeps
        autopush functional. In-memory root_salt_fp still serves this process;
        drift check won't fire on the next run."""
        real_storage = tmp_path / "real_storage"
        real_storage.mkdir()

        config_path = self._setup_config_and_storage(
            tmp_path,
            storage_path_value=str(real_storage),
            storage_real_path=str(real_storage),
        )
        # Simulate the config file being missing at helper-entry time by
        # wrapping patch_config_on_disk with a delete-then-call shim. The
        # real helper then hits the FileNotFoundError branch and raises
        # ConfigError, which autopush swallows.
        real_helper = config_module.patch_config_on_disk

        def delete_then_call(updates, path=None):
            (path or config_path).unlink()
            real_helper(updates, path=path)

        monkeypatch.setattr("mind_meld.cli.patch_config_on_disk", delete_then_call)

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0, (
            f"autopush must swallow backfill failure; got exit={result.exit_code} "
            f"output={result.output!r}"
        )
        # No traceback: the visible-failure contract covers known error modes;
        # backfill failure specifically is known-swallowable.
        assert "Traceback" not in result.output
        assert "Traceback" not in (result.stderr or "")


@pytest.fixture
def capture61(tmp_path, monkeypatch):
    helpers = TestTrack7BEventsTail()
    storage = tmp_path / "storage"
    claude = tmp_path / "claude"
    helpers._seed_claude(claude)
    backend = helpers._bootstrap(storage)
    register_device(backend, "dev-a", "A")
    path = helpers._make_config_with_events(tmp_path, storage, claude, "dev-a", "A")
    cfg = config_module.load_config(path)
    codex = tmp_path / "codex"
    codex.mkdir()
    cfg["sync"]["sources"].append({"name": "codex", "path": str(codex), "type": "generic"})
    save_config(cfg, path)
    helpers._activate(monkeypatch, path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    # Real empty reader: preserve the distinction between [] and ["codex"].
    _mm_host_usage.CODEX_SESSIONS_PATH.mkdir(parents=True, exist_ok=True)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    dayfile = next(helpers._events_dir(tmp_path).glob("*.jsonl"))
    return {"cfg": cfg, "path": path, "dayfile": dayfile, "backend": backend, "claude": claude}


def test_later_autopush_supersedes_attended_reader_coverage(capture61, monkeypatch):
    cfg = capture61["cfg"]
    cfg["retro"] = {"grok_host_usage": True}
    save_config(cfg, capture61["path"])
    monkeypatch.setattr(
        _mm_host_usage,
        "read_grok_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({}, complete=True),
    )
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    first = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    assert first["readers"] == {"codex": "contributed", "grok": "contributed"}
    monkeypatch.setattr(
        _mm_host_usage,
        "read_codex_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({}, complete=False, reason="deadline"),
    )
    monkeypatch.setattr(
        events_tail, "_warm_host_cache_with_notice", lambda *a: pytest.fail("autopush warm")
    )
    (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("changed")
    result = runner.invoke(app, ["autopush"])
    assert result.exit_code == 0, result.output
    latest = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    assert latest["ts"] > first["ts"]
    assert latest["readers"] == {"codex": "degraded", "grok": "contributed"}
    assert "mm push" in " ".join(runner.invoke(app, ["status"]).output.split())


def test_standalone_cursor_completion_reaches_encrypted_retro_snapshot(capture61):
    from mind_meld.skills.retro_fleet import aggregator

    enabled = runner.invoke(app, ["enable-source", "cursor"])
    assert enabled.exit_code == 0, enabled.output
    payload = {
        "hook_event_name": "stop",
        "status": "completed",
        "generation_id": "standalone-generation",
        "model": "grok-4.7-low",
        "input_tokens": 17_534,
        "cache_read_tokens": 1_152,
        "cache_write_tokens": 0,
        "output_tokens": 147,
        "text": "PRIVATE RESPONSE",
    }
    captured = runner.invoke(app, ["capture-cursor-usage"], input=json.dumps(payload))
    assert captured.exit_code == 0, captured.output
    pushed = runner.invoke(app, ["push"])
    assert pushed.exit_code == 0, pushed.output
    publication = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    assert publication["readers"]["cursor"] == "contributed"
    dayfile = capture61["dayfile"]
    manifest = load_manifest(
        decrypt(capture61["backend"].get(storage_keys.manifest_key("dev-a")), PASSPHRASE, MEMORY_KB)
    )
    entry = manifest["sources"]["mm-events"]["files"]["events/" + dayfile.name]
    encrypted = capture61["backend"].get(storage_keys.blob_key("dev-a", entry["sha256"]))
    assert b"PRIVATE RESPONSE" not in encrypted
    clear = decrypt(encrypted, PASSPHRASE, MEMORY_KB)
    assert b"PRIVATE RESPONSE" not in clear
    rows = [json.loads(line) for line in clear.splitlines()]
    row = [row for row in rows if row["type"] == "host-usage-snapshot"][-1]
    accepted = aggregator._accept_host_usage_snapshot(row)
    assert not isinstance(accepted, aggregator.HostReject)
    day = datetime.now(timezone.utc).date().isoformat()
    assert "cursor" in accepted.consulted
    assert accepted.lifetime_by_family["grok"][day]["input"] == 16_382
    assert accepted.tokens_by_day[day]["by_model"]["grok-4.7"]["cache_read"] == 1_152


def test_conductor_sqlite_run_reaches_encrypted_retro_snapshot(capture61):
    """A Conductor 0.90.1 run read from index.db publishes as aggregate counters only."""
    from mind_meld.skills.retro_fleet import aggregator
    from tests.test_cursor_sqlite import fin, make_store, usage

    enabled = runner.invoke(app, ["enable-source", "cursor"])
    assert enabled.exit_code == 0, enabled.output
    finished = datetime.now(timezone.utc) - timedelta(minutes=2)
    stamp = finished.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    row = fin(
        "run-PRIVATE-RUN-ID",
        at=stamp,
        request_id="PRIVATE-REQUEST-ID",
        usage_json=usage(inp=40_000, out=1_000, read=9_000, reasoning=500),
        result="PRIVATE RESULT",
    )
    make_store(_mm_host_usage.CURSOR_STORE_PATH, "0123456789abcdef", [row])
    pushed = runner.invoke(app, ["push"])
    assert pushed.exit_code == 0, pushed.output
    state = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_usage"]["cursor"]
    assert state["unread_sqlite_stores"] == 0
    assert state["unsupported_sqlite_stores"] == {"count": 0, "causes": []}
    assert state["last_reason"] is None and state["last_reason_detail"] is None
    publication = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    assert publication["readers"]["cursor"] == "contributed"
    manifest = load_manifest(
        decrypt(capture61["backend"].get(storage_keys.manifest_key("dev-a")), PASSPHRASE, MEMORY_KB)
    )
    entry = manifest["sources"]["mm-events"]["files"]["events/" + capture61["dayfile"].name]
    encrypted = capture61["backend"].get(storage_keys.blob_key("dev-a", entry["sha256"]))
    clear = decrypt(encrypted, PASSPHRASE, MEMORY_KB)
    for canary in (b"PRIVATE", b"run-PRIVATE-RUN-ID"):
        assert canary not in encrypted and canary not in clear
    rows = [json.loads(line) for line in clear.splitlines()]
    snapshot = [r for r in rows if r["type"] == "host-usage-snapshot"][-1]
    accepted = aggregator._accept_host_usage_snapshot(snapshot)
    assert not isinstance(accepted, aggregator.HostReject)
    day = finished.date().isoformat()
    assert "cursor" in accepted.consulted
    assert accepted.lifetime_by_family["grok"][day]["input"] == 40_000
    assert accepted.tokens_by_day[day]["by_model"]["grok-4.7"]["output"] == 1_000
    # A second push leaves the totals unchanged: each run is counted exactly once.
    assert runner.invoke(app, ["push"]).exit_code == 0
    again = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_usage"]["cursor"]
    assert again["runs_cached"] == state["runs_cached"] == 1


@pytest.mark.parametrize("absent", [False, True])
def test_partial_and_absent_capture_reports_own_outcome(capture61, monkeypatch, absent):
    from mind_meld.skills.retro_fleet import aggregator

    day = datetime.now(timezone.utc).date().isoformat()
    usage = {"input": 9, "output": 3, "cache_read": 0, "cache_create": 0}
    result = (
        _mm_host_usage.HostUsageResult({}, complete=False, reason="no_metadata_ledger")
        if absent
        else _mm_host_usage.HostUsageResult(
            {"codex": {day: usage}}, complete=True, partial_days=frozenset({day})
        )
    )
    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", lambda **kw: result)
    invocation = runner.invoke(app, ["push", "--verbose"])
    assert invocation.exit_code == 0, invocation.output
    assert ("absent (no metadata ledger)" if absent else "partial") in invocation.output
    assert "completed, no usage" not in invocation.output
    rows = [json.loads(line) for line in capture61["dayfile"].read_text().splitlines()]
    row = [r for r in rows if r["type"] == "host-usage-snapshot"][-1]
    assert not isinstance(aggregator._accept_host_usage_snapshot(row), aggregator.HostReject)


def test_capture_outcome_labels_each_reader_independently(capture61, monkeypatch):
    """A reader that itself completes with zero usage must not inherit
    'contributed' just because a SIBLING reader in the same sweep had real
    data. The outcome computation used to check whole-capture
    `capture.hosts` truthiness instead of this reader's own membership,
    mislabeling the empty reader as 'contributed'."""
    cfg = capture61["cfg"]
    cfg["retro"] = {"grok_host_usage": True}
    save_config(cfg, capture61["path"])
    day = datetime.now(timezone.utc).date().isoformat()
    usage = {"input": 9, "output": 3, "cache_read": 0, "cache_create": 0}
    monkeypatch.setattr(
        _mm_host_usage,
        "read_codex_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({"other": {day: usage}}, complete=True),
    )
    monkeypatch.setattr(
        _mm_host_usage,
        "read_grok_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({}, complete=True),
    )
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "Usage capture: codex — contributed" in result.output
    assert "Usage capture: grok — completed, no usage in snapshot" in result.output
    rows = [json.loads(line) for line in capture61["dayfile"].read_text().splitlines()]
    row = [row for row in rows if row["type"] == "host-usage-snapshot"][-1]
    assert row["empty_sources"] == ["grok"]
    for command in (["status"], ["diag"]):
        inspected = runner.invoke(app, command)
        assert inspected.exit_code == 0, inspected.output
        assert "grok: completed, no usage in snapshot" in inspected.stdout
        assert "codex: contributed" in inspected.stdout
    publication = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    assert publication["empty_readers"] == ["grok"]
    assert publication["publication"] == "published"


def test_requested_capture_holds_lock_against_push_and_autopush(capture61, monkeypatch):
    real = _mm_host_usage.read_codex_usage
    blocked = []

    def read(**kwargs):
        blocked.append(runner.invoke(app, ["push"]))
        blocked.append(runner.invoke(app, ["autopush"]))
        return real(**kwargs)

    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", read)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert len(blocked) == 2
    assert blocked[0].exit_code == 1 and "holds the lock" in blocked[0].output
    assert blocked[1].exit_code == 0 and blocked[1].output == ""
    # Existing hook behavior records contention, never a failed host capture.
    assert cli_module._read_autorun_breadcrumbs()["push"]["outcome"] == "lock-held"


def test_no_content_push_capture_usage_refreshes_once(capture61, monkeypatch):
    """R1/R2/R4: attended refresh bypasses zero-work without inventing a push."""
    from mind_meld.skills.retro_fleet import aggregator

    env = capture61
    before = env["dayfile"].read_text().splitlines()
    cursor = _mm_events.resolve_push_cursor(env["dayfile"].parent, "dev-a")
    calls = []
    real = _mm_host_usage.read_codex_usage

    def read(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", read)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "Usage capture:" not in result.stdout
    assert "Host usage published" not in result.stdout
    assert "usage published" in result.stdout
    added = [json.loads(line) for line in env["dayfile"].read_text().splitlines()[len(before) :]]
    rows = [r for r in added if r["type"] == "host-usage-snapshot"]
    assert len(rows) == len(calls) == 1
    assert not any(r["type"] == "mm-push" for r in added)
    assert _mm_events.resolve_push_cursor(env["dayfile"].parent, "dev-a") == cursor
    assert rows[0]["token_sources"] == ["codex"]
    assert rows[0]["hosts"] == {}
    assert not isinstance(aggregator._accept_host_usage_snapshot(rows[0]), aggregator.HostReject)
    diag = runner.invoke(app, ["diag", "--json"])
    pub = json.loads(diag.stdout)["host_publication"]
    assert pub["publication"] == "published"
    assert pub["readers"] == {"codex": "contributed"}
    assert pub["latest_attempt"] == "published"
    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert "Host usage last recorded capture" in status.output
    assert "Publication: published" in status.output


def test_usage_only_refresh_twice_preserves_retro_and_skips_all_activity(capture61, monkeypatch):
    from mind_meld.skills.retro_fleet import aggregator

    path = capture61["dayfile"]
    now = datetime.now(timezone.utc)

    def rendered_counts():
        data = aggregator.aggregate(
            events_dir=path.parent, window_days=7, author_emails=None, now=now
        )
        notes = [line for line in aggregator.format_retro(data).splitlines() if "pushes" in line]
        return data.git.zero_repo_captures, data.pushes.push_events, notes

    before = rendered_counts()
    original = path.read_bytes()
    cursor = _mm_events.resolve_push_cursor(path.parent, "dev-a")
    monkeypatch.setattr(
        events_tail, "_capture_event_snapshots", lambda *a, **kw: pytest.fail("activity walk")
    )
    real_core = cli_module._push_core
    results = []

    def core(*a, **kw):
        result = real_core(*a, **kw)
        results.append(result)
        return result

    monkeypatch.setattr(cli_module, "_push_core", core)
    for verbose in (False, True):
        result = runner.invoke(app, ["push", *(["--verbose"] if verbose else [])])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.stdout.split())  # the mode line wraps at 80 columns
        assert flat.endswith("Push: no content changes; usage published.")
        # The usage-only mode explanation moved to --verbose; it is not removed.
        assert ("Git cursor unchanged" in flat) is verbose
        assert ("mm recapture 30d" in flat) is verbose
        assert rendered_counts() == before
    added = [json.loads(line) for line in path.read_bytes()[len(original) :].splitlines()]
    assert [r["type"] for r in added] == ["host-usage-snapshot"] * 2
    assert _mm_events.resolve_push_cursor(path.parent, "dev-a") == cursor
    assert all(
        r is not None and not r.content_changed and not r.events_degradations for r in results
    )


@pytest.mark.parametrize(
    "change", ["bytes", "mtime", "add-source", "remove-source", "first", "corrupt"]
)
def test_content_refresh_counts_once_and_preserves_next_bare_push(
    capture61, monkeypatch, tmp_path, change
):
    from mind_meld.skills.retro_fleet import aggregator

    env = capture61
    path = env["dayfile"]
    cfg = env["cfg"]
    source_file = env["claude"] / "projects/-Users-kb-myapp/memory/user_role.md"
    if change == "bytes":
        source_file.write_text("new content")
    elif change == "mtime":
        stamp = source_file.stat().st_mtime + 1
        os.utime(source_file, (stamp, stamp))
    elif change == "add-source":
        extra = tmp_path / "extra"
        extra.mkdir()
        cfg["sync"]["sources"].append({"name": "extra", "path": str(extra), "type": "generic"})
    elif change == "remove-source":
        cfg["sync"]["disabled_sources"] = ["claude"]
    elif change == "first":
        env["backend"].delete(cli_module.manifest_key("dev-a"))
    else:
        env["backend"].put(cli_module.manifest_key("dev-a"), b"corrupt")
    save_config(cfg, env["path"])
    # Make discovery complete so this test can observe cursor advancement.
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **kw: _mm_events.GitRootDiscovery([], [], False),
    )
    before = path.read_text().splitlines()
    cursor = _mm_events.resolve_push_cursor(path.parent, "dev-a").since
    invocation = runner.invoke(app, ["push"])
    assert invocation.exit_code == 0, invocation.output
    assert "Refresh mode:" not in invocation.stdout
    added = [json.loads(line) for line in path.read_text().splitlines()[len(before) :]]
    assert sum(r["type"] == "host-usage-snapshot" for r in added) == 1
    assert sum(r["type"] == "git-snapshot" for r in added) == 1
    assert sum(r["type"] == "mm-push" for r in added) == 1
    assert sum(r["type"] == "sessions-snapshot" for r in added) == (change != "remove-source")
    assert _mm_events.resolve_push_cursor(path.parent, "dev-a").since > cursor
    now = datetime.now(timezone.utc)
    counted = aggregator.aggregate_git(
        added, since=now - timedelta(days=7), until=now, author_emails=None
    )
    assert counted.zero_repo_captures == {"dev-a": (1, 1)}
    committed = path.read_bytes()
    bare = runner.invoke(app, ["push"])
    assert bare.exit_code == 0 and "no content changes; usage published" in bare.stdout, bare.output
    more = [json.loads(line) for line in path.read_bytes()[len(committed) :].splitlines()]
    assert [row["type"] for row in more] == ["host-usage-snapshot"]


@pytest.mark.parametrize(
    "change",
    ["none", "mtime", "internal-mtime", "internal-bytes", "add", "remove", "first-internal"],
)
def test_usage_mode_without_appended_host_row(capture61, monkeypatch, tmp_path, change):
    """Exercise the gate legs whose byte-diff list is empty by construction."""
    env = capture61
    cfg = env["cfg"]
    if change in ("mtime", "internal-mtime"):
        path = (
            env["dayfile"]
            if change == "internal-mtime"
            else env["claude"] / "projects/-Users-kb-myapp/memory/user_role.md"
        )
        stamp = path.stat().st_mtime + 1
        os.utime(path, (stamp, stamp))
    elif change == "add":
        root = tmp_path / "empty-source"
        root.mkdir()
        cfg["sync"]["sources"].append({"name": "empty", "type": "generic", "path": str(root)})
    elif change == "remove":
        cfg["sync"]["disabled_sources"] = ["claude"]
    elif change == "internal-bytes":
        with env["dayfile"].open("ab") as stream:
            stream.write(b'{"type":"test-pending"}\n')
    elif change == "first-internal":
        remote = deserialize_manifest(
            decrypt(
                env["backend"].get(cli_module.manifest_key("dev-a")),
                PASSPHRASE,
                memory_kb=MEMORY_KB,
            )
        )
        del remote["sources"]["mm-events"]
        env["backend"].put(
            cli_module.manifest_key("dev-a"),
            encrypt(serialize_manifest(remote), PASSPHRASE, memory_kb=MEMORY_KB),
        )
    monkeypatch.setattr(events_tail, "_default_host_readers", lambda *a, **kw: ())
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **kw: _mm_events.GitRootDiscovery([], [], False),
    )
    before = env["dayfile"].read_text().splitlines()
    result = cli_module._push_core(cfg, PASSPHRASE, MEMORY_KB, attended=True)
    if change == "none":
        assert result is None
        assert env["dayfile"].read_text().splitlines() == before
        return
    assert result is not None
    assert result.content_changed == (change in {"mtime", "add", "remove"})
    assert result.content_files == 0
    added = [json.loads(line) for line in env["dayfile"].read_text().splitlines()[len(before) :]]
    assert sum(r["type"] == "mm-push" for r in added) == 1
    assert not any(r["type"] == "host-usage-snapshot" for r in added)


@pytest.mark.parametrize("emitter", ["push", "capture", "init", "recapture"])
def test_all_emitters_obey_git_batch_origin_contract(capture61, monkeypatch, emitter):
    from mind_meld.skills.retro_fleet import aggregator

    env = capture61
    batches = []
    real_write = _mm_events.write_push_event

    def write(events_dir, device, rows, **kwargs):
        batches.append(rows)
        return real_write(events_dir, device, rows, **kwargs)

    monkeypatch.setattr(_mm_events, "write_push_event", write)
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **kw: _mm_events.GitRootDiscovery([], [], False),
    )
    if emitter == "init":
        sources = config_module.resolve_sources(env["cfg"], bootstrap=False).available
        cursor = _mm_events.resolve_push_cursor(env["dayfile"].parent, "dev-a")
        events_tail._run_events_backfill(env["cfg"], sources, "dev-a")
        assert _mm_events.resolve_push_cursor(env["dayfile"].parent, "dev-a") == cursor
    elif emitter == "recapture":
        # Supply one discovered root so the shell writes the real prepared batch.
        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda *a, **kw: _mm_events.GitRootDiscovery([env["claude"]], [], False),
        )
        result = runner.invoke(app, ["recapture", "1d"])
        assert result.exit_code in (0, 4), result.output
    else:
        if emitter == "push":
            (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("changed")
        # "capture" is the converged tree: nothing pending, so only the host row lands.
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output
    assert batches
    for batch in batches:
        if not any(row["type"] == "mm-push" for row in batch):
            assert all(row.get("origin") for row in batch if row["type"] == "git-snapshot")
    rows = [row for batch in batches for row in batch]
    git_rows = [row for row in rows if row["type"] == "git-snapshot"]
    if emitter == "capture":
        assert [row["type"] for row in rows] == ["host-usage-snapshot"]
    elif emitter == "init":
        assert git_rows and all(row["origin"] == "init" for row in git_rows)
        now = datetime.now(timezone.utc)
        counted = aggregator.aggregate_git(
            rows, since=now - timedelta(days=31), until=now, author_emails=None
        )
        assert counted.zero_repo_captures == {}
    elif emitter == "recapture":
        assert any(row.get("origin") == "recapture" for row in git_rows)
    else:
        assert git_rows and all("origin" not in row for row in git_rows)


def _recapture_with_unstable(capture61, monkeypatch, *, held_name):
    """Run ``mm recapture 1d`` while scan reads of ``held_name`` never settle."""
    env = capture61
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **kw: _mm_events.GitRootDiscovery([env["claude"]], [], False),
    )
    real = cli_module.manifest.read_file_revision

    def flaky(path, **kw):
        if Path(path).name == held_name or (held_name == "*.jsonl" and path.suffix == ".jsonl"):
            raise cli_module.SnapshotChanged("changed while being read", "changed")
        return real(path, **kw)

    monkeypatch.setattr(cli_module.manifest, "read_file_revision", flaky)
    return runner.invoke(app, ["recapture", "1d"])


def test_recapture_with_a_held_day_file_takes_the_not_synced_exit_75a(capture61, monkeypatch):
    result = _recapture_with_unstable(capture61, monkeypatch, held_name="*.jsonl")
    flat = " ".join((result.output + (result.stderr or "")).split())
    assert result.exit_code == 1, result.output
    assert "Recapture was written locally but not synced." in flat
    assert "still changing; run 'mm push' to publish it." in flat
    assert "Recapture complete" not in flat and "Recapture found no commits" not in flat


def test_attended_capture_exit_with_a_held_day_file_carries_held_and_not_published_75a(
    capture61, monkeypatch
):
    """The captured-host-row no-op exit returns a PushResult carrying `held`, and the host
    row that sits in the held day file is not reported as published."""
    real = cli_module.manifest.read_file_revision

    def flaky(path, **kw):
        if path.suffix == ".jsonl" and path.parent.name == "events":
            raise cli_module.SnapshotChanged("changed while being read", "changed")
        return real(path, **kw)

    monkeypatch.setattr(cli_module.manifest, "read_file_revision", flaky)
    seen = []
    real_core = cli_module._push_core

    def spy(*args, **kwargs):
        result = real_core(*args, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(cli_module, "_push_core", spy)
    result = runner.invoke(app, ["push", "--verbose"])
    flat = " ".join((result.output + (result.stderr or "")).split())
    assert result.exit_code == 0, result.output
    assert "no content changes; 1 held." in flat
    assert "Push: no content changes." not in flat  # held count qualifies this invocation
    assert "not yet synced" in flat  # --verbose names the held file even on a held-only push
    assert "Host usage published" not in flat
    (core,) = seen
    assert core is not None and len(core.held) == 1
    assert core.held[0][0] == "mm-events" and core.held[0][2] == "changed"
    assert core.host_usage_published is False
    assert core.content_accepted is False


def test_recapture_is_unaffected_by_a_held_user_file_75a(capture61, monkeypatch):
    env = capture61
    (env["claude"] / "projects/-Users-kb-myapp/memory/hot.md").write_text("still being written")
    result = _recapture_with_unstable(capture61, monkeypatch, held_name="hot.md")
    flat = " ".join((result.output + (result.stderr or "")).split())
    assert result.exit_code in (0, 4), result.output
    assert "written locally but not synced" not in flat


@pytest.mark.parametrize("recovery", ["push", "autopush"])
def test_failed_usage_publication_recovery_respects_activity_gate(capture61, monkeypatch, recovery):
    from mind_meld.skills.retro_fleet import aggregator

    path = capture61["dayfile"]
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **kw: _mm_events.GitRootDiscovery([], [], False),
    )
    before = path.read_text().splitlines()
    real_put = LocalBackend.put

    def fail(self, key, data):
        if key.startswith("manifests/"):
            raise OSError("upload interrupted")
        return real_put(self, key, data)

    monkeypatch.setattr(LocalBackend, "put", fail)
    failed = runner.invoke(app, ["push"])
    assert failed.exit_code == 1, failed.output
    assert "(push-failed)" in failed.stderr
    assert "(push-failed)" not in failed.stdout
    assert "effects unconfirmed" in failed.stderr
    assert failed.stderr.splitlines()[-1].endswith("Run: mm diag")
    monkeypatch.setattr(LocalBackend, "put", real_put)
    result = runner.invoke(app, [recovery])
    assert result.exit_code == 0, result.output
    added = [json.loads(line) for line in path.read_text().splitlines()[len(before) :]]
    assert sum(r["type"] == "mm-push" for r in added) == (recovery == "autopush")
    now = datetime.now(timezone.utc)
    counted = aggregator.aggregate_git(
        added, since=now - timedelta(days=7), until=now, author_emails=None
    )
    assert counted.zero_repo_captures == ({"dev-a": (1, 1)} if recovery == "autopush" else {})


@pytest.mark.parametrize("recovery", ["autopush", "capture"])
def test_torn_append_failed_rollback_continues_and_separates_next_append(
    capture61, monkeypatch, recovery
):
    path = capture61["dayfile"]
    original = path.read_bytes()
    real_write, real_truncate = os.write, os.ftruncate
    accepted = cli_module.sidecar.read("dev-a")

    def torn(fd, payload):
        if b'"host-usage-snapshot"' in payload:
            return real_write(fd, payload[:10])
        return real_write(fd, payload)

    def cannot_rollback(fd, size):
        if os.fstat(fd).st_ino == path.stat().st_ino:
            raise OSError("truncate failed")
        return real_truncate(fd, size)

    monkeypatch.setattr(os, "write", torn)
    monkeypatch.setattr(os, "ftruncate", cannot_rollback)
    failed = runner.invoke(app, ["push", "--verbose"])
    assert failed.exit_code == 0, failed.output
    assert "(append-failed)" in failed.stderr
    assert "(append-failed)" not in failed.stdout
    assert cli_module.sidecar.read("dev-a") != accepted
    assert path.read_bytes().startswith(original)
    # The activity batch separates the damaged suffix before publication.
    assert path.read_bytes().endswith(b"\n")
    monkeypatch.setattr(os, "write", real_write)
    monkeypatch.setattr(os, "ftruncate", real_truncate)
    result = runner.invoke(app, ["autopush"] if recovery == "autopush" else ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    added = path.read_bytes()[len(original) :].splitlines()
    with pytest.raises(ValueError):
        json.loads(added[0])  # the torn row cannot be reconstructed
    rows = [json.loads(line) for line in added[1:]]
    assert any(row["type"] == "host-usage-snapshot" for row in rows) == (recovery == "capture")
    assert path.read_bytes().startswith(original)
    assert cli_module.sidecar.read("dev-a") != accepted


def test_capture_failure_nudges_upgrade_after_unlock(capture61, monkeypatch):
    monkeypatch.setattr(
        _mm_host_usage,
        "read_codex_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({}, complete=False, reason="unsupported"),
    )
    calls = []

    def nudge(config, *, attended, diagnostics=True):
        cli_module.acquire_lock()
        cli_module.release_lock()
        assert attended
        calls.append("nudge")

    monkeypatch.setattr(cli_module.upgrade, "update_or_nudge", nudge)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert calls == ["nudge"]
    assert "(no-row)" in result.stderr and "(no-row)" not in result.stdout


@pytest.mark.parametrize("state", ["disabled", "unavailable", "no-reader", "unknown"])
def test_capture_prerequisites_skip_and_push_content(capture61, monkeypatch, state):
    env = capture61
    cfg = env["cfg"]
    if state == "disabled":
        cfg["sync"]["disabled_sources"] = ["mm-events"]
    elif state == "unavailable":
        cfg["sync"]["sources"][1]["path"] += "-missing"
    elif state == "no-reader":
        cfg["sync"]["sources"] = [s for s in cfg["sync"]["sources"] if s["name"] != "codex"]
    else:
        # Unreadable config itself still refuses; inject the pure unknown
        # verdict to pin optional capture's behavior independently of loading.
        monkeypatch.setattr(config_module, "usage_capture_readiness", lambda *a, **kw: "unknown")
    save_config(cfg, env["path"])
    (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    before = env["dayfile"].read_text().splitlines()
    keychain = []
    monkeypatch.setattr(
        cli_module, "_get_passphrase_or_exit", lambda: keychain.append(True) or PASSPHRASE
    )
    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", lambda **kw: pytest.fail("reader"))
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert keychain == [True]
    assert result.stderr.count(f"(prerequisites: {state})") == 1
    remote = cli_module.sidecar.read("dev-a")
    assert "projects/-Users-kb-myapp/memory/new.md" in remote["sources"]["claude"]["files"]
    added = [json.loads(line) for line in env["dayfile"].read_text().splitlines()[len(before) :]]
    assert not any(row["type"] == "host-usage-snapshot" for row in added)


@pytest.mark.parametrize("reason", ["deadline", "malformed", "io_error", "locked"])
@pytest.mark.parametrize("pending", [False, True])
def test_capture_failure_still_calls_push(capture61, monkeypatch, reason, pending):
    if pending:
        (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    prior = cli_module.sidecar.read("dev-a")
    monkeypatch.setattr(
        _mm_host_usage,
        "read_codex_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({}, complete=False, reason=reason),
    )
    monkeypatch.setattr(
        _mm_host_usage,
        "warm_host_cache_inline",
        lambda **kw: _mm_host_usage.HostUsageResult({}, complete=False, reason=reason),
    )
    calls = []
    real_core = cli_module._push_core

    def core(*a, **kw):
        calls.append(True)
        return real_core(*a, **kw)

    monkeypatch.setattr(cli_module, "_push_core", core)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert reason in result.output
    assert calls == [True]
    assert ("Push: no content changes" in result.output) == (not pending)
    assert "(no-row)" in result.stderr and "(no-row)" not in result.stdout
    assert "Content sync continues" in " ".join(result.stderr.split())
    assert "#host-usage-capture" in result.stderr
    accepted = cli_module.sidecar.read("dev-a")
    assert (accepted != prior) == pending
    assert (
        "projects/-Users-kb-myapp/memory/new.md" in accepted["sources"]["claude"]["files"]
    ) == pending


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_requested_capture_observes_append_failure(capture61, monkeypatch, short, pending):
    if pending:
        (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    real = os.write

    def write(fd, payload):
        if b'"host-usage-snapshot"' in payload:
            if short:
                return real(fd, payload[:10])
            raise OSError("disk full at [events/dev-a.jsonl]")
        return real(fd, payload)

    monkeypatch.setattr(os, "write", write)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "OSError" in result.output
    assert "Host usage published" not in result.output
    assert "(append-failed)" in result.stderr and "(append-failed)" not in result.stdout
    assert "Content sync continues" in " ".join(result.stderr.split())
    assert "#host-usage-capture" in result.stderr
    accepted = cli_module.sidecar.read("dev-a")
    # A rolled-back append still bumps the internal file's mtime. The ordinary
    # activity path may republish that metadata even with no user-byte change.
    assert (
        "projects/-Users-kb-myapp/memory/new.md" in accepted["sources"]["claude"]["files"]
    ) == pending
    if not short:
        assert "[events/dev-a.jsonl]" not in result.stderr
        assert r"\[events" not in result.stderr
    if short:
        before = [
            json.loads(line)
            for line in capture61["dayfile"].read_text().splitlines()
            if line.strip()
        ]
        host_before = sum(row["type"] == "host-usage-snapshot" for row in before)
        monkeypatch.setattr(os, "write", real)
        retry = runner.invoke(app, ["push", "--verbose"])
        assert retry.exit_code == 0, retry.output
        parsed = [
            json.loads(line)
            for line in capture61["dayfile"].read_text().splitlines()
            if line.strip()
        ]
        assert sum(row["type"] == "host-usage-snapshot" for row in parsed) == host_before + 1


@pytest.mark.parametrize("mode", ["exclude", "include", "oversized"])
@pytest.mark.parametrize("unrelated", [False, True])
def test_capture_must_be_in_accepted_manifest(capture61, monkeypatch, mode, unrelated):
    env = capture61
    cfg = env["cfg"]
    # New daily file: an already advertised oversized/omitted file correctly
    # refuses the entire snapshot under Track 50A (exit 1), a different case.
    from datetime import timedelta

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)

    class NextDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return tomorrow

    env["dayfile"].unlink()
    monkeypatch.setattr(_mm_events, "datetime", NextDay)
    src = cfg["sync"]["sources"][1]
    if mode == "exclude":
        src["exclude_patterns"] = ["events/*"]
    elif mode == "include":
        src["include_dirs"] = []
    else:
        cfg["sync"]["max_file_size"] = 100
    if unrelated:
        (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("unrelated")
    save_config(cfg, env["path"])
    saved = config_module.load_config(env["path"])["sync"]
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    if unrelated:
        remote = cli_module.sidecar.read("dev-a")
        assert "projects/-Users-kb-myapp/memory/new.md" in remote["sources"]["claude"]["files"]
    if mode == "oversized":
        assert "Event append (max-file-size)" in result.stderr
        assert "Archive older rows outside mm-events" in " ".join(result.stderr.split())
        assert config_module.load_config(env["path"])["sync"] == saved
        return
    assert "Usage capture (not-published:" in result.stderr
    assert "Usage capture (not-published:" not in result.stdout
    assert (
        "Content changes were pushed" if unrelated else "User content was already up to date"
    ) in " ".join(result.stderr.split())
    assert "mm push" in " ".join(result.stderr.split())
    assert "#host-usage-capture" in result.stderr
    assert {"exclude": "exclude_patterns", "include": "include_dirs"}[mode] in result.output
    assert config_module.load_config(env["path"])["sync"] == saved


def test_converged_publication_evidence_failure_keeps_exit_zero(capture61, monkeypatch):
    """The nothing-to-push path reports publication too, and is contained the same way."""
    from datetime import timedelta

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)

    class NextDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return tomorrow

    env = capture61
    env["dayfile"].unlink()
    monkeypatch.setattr(_mm_events, "datetime", NextDay)
    env["cfg"]["sync"]["sources"][1]["exclude_patterns"] = ["events/*"]
    save_config(env["cfg"], env["path"])
    # The first push settles the exclusion into the advertised manifest, so the
    # next host row lands in a day file the manifest never carries.
    settled = runner.invoke(app, ["push", "--verbose"])
    assert settled.exit_code == 0, settled.output

    def broken(*args, **kwargs):
        raise RecursionError("evidence broke")

    monkeypatch.setattr(cli_module, "_report_usage_publication", broken)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "Push: no content changes" in result.stdout
    flat = " ".join(result.stderr.split())
    assert "(unverified: evidence-error)" in flat
    assert "RecursionError" in flat and "evidence broke" not in flat
    assert "User content was already up to date" in flat
    assert "push-failed" not in result.output


@pytest.mark.parametrize("damage", ["missing", "corrupt", "changed"])
def test_capture_publication_reports_local_revision_evidence_loss(capture61, monkeypatch, damage):
    path = capture61["dayfile"]
    real_put = LocalBackend.put

    def put(self, key, payload):
        result = real_put(self, key, payload)
        if key.startswith("manifests/"):
            # A concurrent local writer after storage acceptance invalidates the
            # revision proof; a file can also vanish or become unparseable.
            if damage == "missing":
                path.unlink()
            elif damage == "corrupt":
                path.write_bytes(b'{"torn":')
            else:
                with path.open("ab") as stream:
                    stream.write(b'{"concurrent":true}\n')
        return result

    monkeypatch.setattr(LocalBackend, "put", put)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    token = "missing" if damage == "missing" else "revision-mismatch"
    assert f"(unverified: {token})" in result.stderr
    assert "not-published:" not in result.output
    flat = " ".join(result.stderr.split())
    assert "User content was already up to date" in flat
    assert "mm status" in flat
    assert "#host-usage-capture" in flat


def test_mode_render_failure_cannot_undo_manifest_acceptance(capture61, monkeypatch):
    def fail(*args):
        raise OSError("mode output failed")

    prior = cli_module.sidecar.read("dev-a")
    monkeypatch.setattr(cli_module, "_print_usage_push_mode", fail)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "post-publication maintenance failed" in " ".join(result.stderr.split())
    # The usage-only push reached manifest acceptance: its new host row is in the sidecar.
    assert cli_module.sidecar.read("dev-a") != prior


@pytest.mark.parametrize("after", [False, True])
def test_capture_exit_uses_acceptance_boundary(capture61, monkeypatch, after):
    (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    if after:

        def fail_gc(*a, **kw):
            raise typer.Exit(1)

        monkeypatch.setattr(cli_module, "_do_gc", fail_gc)
    else:
        real = LocalBackend.put

        def fail_manifest(self, key, data):
            if key.startswith("manifests/"):
                raise OSError("manifest refused")
            return real(self, key, data)

        monkeypatch.setattr(LocalBackend, "put", fail_manifest)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == (0 if after else 1), result.output
    assert ("Host usage was published" if after else "effects unconfirmed") in result.output
    record, reason = cli_module.attemptlog.read()
    assert reason is None
    assert record["class"] == ("published" if after else "push-failed")
    assert record["row_ts"] is not None
    if after:
        assert "post-publication GC stopped" in result.output


@pytest.mark.parametrize("name", ["mm-events", "codex"])
def test_enable_default_from_explicit_empty_sources(capture61, monkeypatch, tmp_path, name):
    env = capture61
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".codex").mkdir(exist_ok=True)
    env["cfg"]["sync"]["sources"] = []
    save_config(env["cfg"], env["path"])
    result = runner.invoke(app, ["enable-source", name])
    assert result.exit_code == 0, result.output
    saved = config_module.load_config(env["path"])
    assert [s["name"] for s in saved["sync"]["sources"]] == [name]
    assert any(s["name"] == name for s in config_module.resolve_sources(saved).selected)


class TestTrack7BEventsTail:
    """Track 7B (v0.10.3): per-push events tail at HEAD of ``_push_core``.

    See docs/invariants/events-retro.md "Events tail in `_push_core`" for
    the four invariants. These tests pin the wiring shape — the tail must
    fire on every push attempt past the no-sources guard, never on
    ``--dry-run``, never on un-migrated configs lacking ``mm-events``,
    and must aggregate multi-claude scans into a single sessions-snapshot
    row. Failures inside the tail are forensic-only and cannot fail the
    push.
    """

    def _events_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "events_root" / "events"

    def _make_config_with_events(
        self,
        tmp_path: Path,
        storage_dir: Path,
        claude_dir: Path,
        device_id: str,
        device_name: str,
        *,
        include_mm_events: bool = True,
        extra_claude_dirs: list[Path] | None = None,
    ) -> Path:
        config_path = tmp_path / f"config_{device_id}.toml"
        sources: list[dict] = [{"name": "claude", "path": str(claude_dir), "type": "claude"}]
        for i, extra in enumerate(extra_claude_dirs or []):
            sources.append({"name": f"claude-{i + 2}", "path": str(extra), "type": "claude"})
        if include_mm_events:
            (tmp_path / "events_root").mkdir(exist_ok=True)
            sources.append(
                {
                    "name": "mm-events",
                    "path": str(tmp_path / "events_root"),
                    "type": "generic",
                    "include_dirs": ["events"],
                    "exclude_patterns": [],
                }
            )
        config = {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {"max_file_size": 52_428_800, "sources": sources},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        save_config(config, config_path)
        return config_path

    def _seed_claude(self, claude_dir: Path) -> None:
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "user_role.md").write_text("---\nname: role\n---\nEng")
        # session jsonl so walk_session_metadata has something to scan
        sessions = claude_dir / "projects" / "-Users-kb-myapp"
        (sessions / "session.jsonl").write_text(
            json.dumps({"cwd": str(claude_dir.parent), "type": "user"}) + "\n"
        )

    def _bootstrap(self, storage_dir: Path) -> LocalBackend:
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        return backend

    def _activate(self, monkeypatch, config_path: Path) -> None:
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

    def _events_files(self, events_dir: Path) -> list[Path]:
        if not events_dir.is_dir():
            return []
        return sorted(events_dir.glob("*.jsonl"))

    def _read_event_rows(self, events_file: Path) -> list[dict]:
        return [json.loads(ln) for ln in events_file.read_text().splitlines() if ln.strip()]

    def test_events_tail_fires_on_successful_push(self, tmp_path, monkeypatch):
        """Happy path: mm-events resolved, push succeeds, events file
        appears with a final mm-push row (CT-4 invariant)."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        events_files = self._events_files(self._events_dir(tmp_path))
        assert len(events_files) == 1, "exactly one events file expected for one device"
        rows = self._read_event_rows(events_files[0])
        assert rows, "events file must not be empty"
        # CT-4: mm-push is the LAST row.
        assert rows[-1]["type"] == "mm-push"
        assert rows[-1]["device"] == "dev-a"

    def test_events_tail_skipped_on_dry_run(self, tmp_path, monkeypatch):
        """Preview contract: ``mm push --dry-run`` must not write any
        events file. The tail returns immediately on dry_run=True."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        result = runner.invoke(app, ["push", "--dry-run"])
        assert result.exit_code == 0, result.output

        # No events file ever materialized.
        assert self._events_files(self._events_dir(tmp_path)) == []

    def test_events_tail_skipped_on_un_migrated_config(self, tmp_path, monkeypatch):
        """Codex C1: a config that pre-dates v0.10.1 has no mm-events
        source. The tail must no-op; pre-migration users see no
        ``~/.local/share/mind-meld/events/`` cruft."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(
            tmp_path, storage_dir, claude_a, "dev-a", "A", include_mm_events=False
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output

        # Tail no-opped — no events_root materialized.
        assert not self._events_dir(tmp_path).exists()

    def test_events_tail_skipped_when_mm_events_disabled(self, tmp_path, monkeypatch):
        """v0.10.0 disabled_sources path: when mm-events is in the
        per-device disabled list, get_sources() drops it and the tail
        no-ops (gate is "mm-events resolved", not just "not disabled")."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config_path = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        # Patch the config to disable mm-events on this device.
        with config_path.open("rb") as f:
            cfg = tomllib.loads(f.read().decode())
        cfg["sync"]["disabled_sources"] = ["mm-events"]
        save_config(cfg, config_path)

        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config_path)

        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output
        assert self._events_files(self._events_dir(tmp_path)) == []

    def test_events_tail_failure_does_not_fail_push(self, tmp_path, monkeypatch):
        """Forensic-only invariant: any exception inside the tail is
        swallowed + breadcrumbed via ``mm: notice:``. The push proceeds."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        # Break events.discover_git_roots so the tail blows up.
        def _boom(_config, **_kwargs):
            raise RuntimeError("synthetic walk failure")

        monkeypatch.setattr(_mm_events, "discover_git_roots", _boom)

        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, (
            f"push must succeed even when events tail crashes; output={result.output!r}"
        )

    def test_events_tail_skips_on_no_content_push(self, tmp_path, monkeypatch):
        """v0.12.2 substantive-change gate: events tail does NOT fire on a
        truly empty push (no user-source diffs, no corrupt-manifest recovery).
        Pre-v0.12.2 the tail fired at HEAD of _push_core unconditionally —
        empty pushes wrote a phantom mm-push row, mutated the mm-events
        file, and reported "1 file uploaded" forever. The cursor stays
        accurate because no-op pushes never advanced it anyway."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        # First push: real content, real events.
        assert runner.invoke(app, ["push"]).exit_code == 0
        first = self._events_files(self._events_dir(tmp_path))
        assert len(first) == 1
        first_rows = self._read_event_rows(first[0])
        first_mtime = first[0].stat().st_mtime_ns

        # Second push: nothing changed in claude_a → empty push. Events
        # file must NOT gain a new row; mtime must NOT advance; the file
        # must remain the only events file.
        assert runner.invoke(app, ["push"]).exit_code == 0
        second_rows = self._read_event_rows(first[0])
        assert len(second_rows) == len(first_rows), (
            f"empty push wrote a phantom event row (was {len(first_rows)}, now {len(second_rows)})"
        )
        assert first[0].stat().st_mtime_ns == first_mtime, (
            "empty push touched the events file mtime"
        )
        assert self._events_files(self._events_dir(tmp_path)) == first

        # Third push WITH a real change: events tail must fire again.
        (claude_a / "projects" / "-Users-kb-myapp" / "memory" / "new.md").write_text("new content")
        assert runner.invoke(app, ["push"]).exit_code == 0
        third_rows = self._read_event_rows(first[0])
        assert len(third_rows) > len(second_rows), "events tail did not fire on a substantive push"
        assert third_rows[-1]["type"] == "mm-push"

    def test_host_usage_row_ships_on_a_real_push(self, tmp_path, monkeypatch):
        """Track 19A end-to-end: the host snapshot rides the same push as the
        rows around it, ordered before the terminal ``mm-push``."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config_path = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        # The codex reader is CONSENT-GATED on the `codex` source being enabled,
        # so a host row only ships for a user who opted that host in.
        config = config_module.load_config(config_path)
        codex_root = tmp_path / "codex-home"
        codex_root.mkdir()
        config["sync"]["sources"].append(
            {"name": "codex", "path": str(codex_root), "type": "generic"}
        )
        save_config(config, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config_path)
        monkeypatch.setattr(
            _mm_host_usage,
            "read_codex_usage",
            lambda **_kw: _mm_host_usage.HostUsageResult(
                {
                    "codex": {
                        "2026-08-15": {"input": 12, "cache_create": 0, "cache_read": 0, "output": 3}
                    }
                },
                complete=True,
            ),
        )

        assert runner.invoke(app, ["push"]).exit_code == 0
        rows = self._read_event_rows(self._events_files(self._events_dir(tmp_path))[0])
        types = [r["type"] for r in rows]
        assert types.index("host-usage-snapshot") < types.index("mm-push")
        assert types[-1] == "mm-push"
        host_row = rows[types.index("host-usage-snapshot")]
        assert host_row["hosts"]["codex"]["2026-08-15"]["input"] == 12
        assert host_row["active_days"] == ["2026-08-15"]

    @pytest.mark.parametrize("consented", [False, True])
    def test_no_content_push_reads_only_consented_hosts(self, tmp_path, monkeypatch, consented):
        """An attended converged push reads consented hosts; no-reader stays empty."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        if consented:
            cfg = config_module.load_config(config)
            root = tmp_path / "codex"
            root.mkdir()
            cfg["sync"]["sources"].append({"name": "codex", "path": str(root), "type": "generic"})
            save_config(cfg, config)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        assert runner.invoke(app, ["push"]).exit_code == 0

        calls: list[str] = []

        def record(name):
            def read(**_kw):
                calls.append(name)
                return _mm_host_usage.HostUsageResult({}, complete=True)

            return read

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", record("codex"))
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", record("grok"))

        # Nothing changed since the first push → no-op.
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0
        assert ("usage published" in result.output) is consented
        assert calls == (["codex"] if consented else [])

    def test_noop_across_utc_keeps_cursor_for_later_git_catchup(self, tmp_path, monkeypatch):
        """A date rollover does not manufacture an event or lose idle commits.

        The local source stays unchanged while an external configured repo
        receives a commit. The empty day-two push must leave the day-one
        cursor intact; a later unrelated source change then captures that
        commit exactly once.
        """
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        repo = tmp_path / "retro-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
        )
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "seed.txt").write_text("seed")
        subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)

        config_path = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        config = config_module.load_config(config_path)
        config["retro"] = {"repo_roots": [str(repo)]}
        save_config(config, config_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config_path)

        class FrozenDatetime(datetime):
            current = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current if tz is not None else cls.current.replace(tzinfo=None)

        monkeypatch.setattr(_mm_events, "datetime", FrozenDatetime)
        monkeypatch.setattr(events_tail, "datetime", FrozenDatetime)

        assert runner.invoke(app, ["push"]).exit_code == 0
        first_files = self._events_files(self._events_dir(tmp_path))
        assert len(first_files) == 1
        first_rows = self._read_event_rows(first_files[0])
        first_cursor = [row for row in first_rows if row["type"] == "mm-push"][-1]["ts"]

        FrozenDatetime.current = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        (repo / "idle.txt").write_text("idle")
        subprocess.run(["git", "-C", str(repo), "add", "idle.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "idle"], check=True)
        idle_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # No synced source changed: no day-two event file and no new cursor.
        assert runner.invoke(app, ["push"]).exit_code == 0
        assert self._events_files(self._events_dir(tmp_path)) == first_files
        assert [row for row in self._read_event_rows(first_files[0]) if row["type"] == "mm-push"][
            -1
        ]["ts"] == first_cursor

        (claude_a / "projects" / "-Users-kb-myapp" / "memory" / "trigger.md").write_text(
            "substantive"
        )
        assert runner.invoke(app, ["push"]).exit_code == 0
        rows = [
            row
            for event_file in self._events_files(self._events_dir(tmp_path))
            for row in self._read_event_rows(event_file)
        ]
        captured_shas = [
            commit["sha"]
            for row in rows
            if row.get("type") == "git-snapshot"
            for project in row.get("projects", [])
            for commit in project.get("commits", [])
        ]
        assert captured_shas.count(idle_sha) == 1

    def test_events_tail_filters_mm_events_from_sources_field(self, tmp_path, monkeypatch):
        """Codex C7: mm-events source name MUST NOT appear in the
        ``sources`` field of the mm-push event (mm-owned infrastructure,
        not user fleet activity)."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        assert runner.invoke(app, ["push"]).exit_code == 0
        rows = self._read_event_rows(self._events_files(self._events_dir(tmp_path))[0])
        push_row = next(r for r in rows if r.get("type") == "mm-push")
        assert "mm-events" not in push_row["sources"]
        assert "claude" in push_row["sources"]

    def test_events_tail_aggregates_multi_claude_dirs(self, tmp_path, monkeypatch):
        """Multi-claude aggregation: two ``type=claude`` sources → ONE
        sessions-snapshot row with combined projects (mirrors
        walk_git_projects' aggregate-into-one-row shape)."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        claude_a2 = tmp_path / "machine_a" / ".claude2"
        self._seed_claude(claude_a)
        self._seed_claude(claude_a2)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(
            tmp_path,
            storage_dir,
            claude_a,
            "dev-a",
            "A",
            extra_claude_dirs=[claude_a2],
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        assert runner.invoke(app, ["push"]).exit_code == 0
        rows = self._read_event_rows(self._events_files(self._events_dir(tmp_path))[0])
        sessions_rows = [r for r in rows if r.get("type") == "sessions-snapshot"]
        assert len(sessions_rows) == 1, (
            f"expected exactly one sessions-snapshot row across multi-claude, "
            f"got {len(sessions_rows)}"
        )

    def test_events_tail_records_device_id_on_snapshots(self, tmp_path, monkeypatch):
        """All non-empty snapshot rows must carry the pushing device's
        id. ``walk_git_projects`` and ``walk_session_metadata`` set
        ``device: ""`` and the wiring fills it in."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        self._activate(monkeypatch, config)

        assert runner.invoke(app, ["push"]).exit_code == 0
        rows = self._read_event_rows(self._events_files(self._events_dir(tmp_path))[0])
        # Every row stamped with dev-a (no orphaned "" devices).
        for row in rows:
            assert row.get("device") == "dev-a", row


class TestPushMtimeOnlyPropagation:
    """v0.12.6: ``_push_core`` must republish the manifest when only mtimes
    differ from the remote, NOT bail at the substantive-change gate.

    Drives the resolve(local) cross-fleet propagation story: the resolve
    helper bumps canonical's mtime without changing bytes, and the next
    push must broadcast that mtime so peers see local-as-authoritative
    on their next pull. Pre-fix, the v0.12.2 gate compared by sha256 only
    via ``diff_files`` and the bumped-mtime decision stayed machine-local.
    Caught by Codex's adversarial pass on the v0.12.6 PR.
    """

    def test_has_mtime_only_changes_detects_drift(self) -> None:
        """Pure helper unit test: same sha256, different mtime -> True."""
        from mind_meld.cli import _has_mtime_only_changes_vs_remote

        local = {
            "sources": {
                "claude": {
                    "files": {"f.md": {"sha256": "abc", "mtime": "2026-05-12T12:00:00+00:00"}}
                }
            }
        }
        remote = {
            "claude": {"files": {"f.md": {"sha256": "abc", "mtime": "2026-05-09T08:48:00+00:00"}}}
        }
        assert _has_mtime_only_changes_vs_remote(local, remote) is True

    def test_has_mtime_only_changes_false_when_sha_differs(self) -> None:
        """sha256 mismatch is caught by the existing diff_files gate, NOT
        this helper -- avoid double-firing on real content changes."""
        from mind_meld.cli import _has_mtime_only_changes_vs_remote

        local = {
            "sources": {
                "claude": {
                    "files": {"f.md": {"sha256": "new", "mtime": "2026-05-12T12:00:00+00:00"}}
                }
            }
        }
        remote = {
            "claude": {"files": {"f.md": {"sha256": "old", "mtime": "2026-05-09T08:48:00+00:00"}}}
        }
        assert _has_mtime_only_changes_vs_remote(local, remote) is False

    def test_has_mtime_only_changes_false_when_file_only_local(self) -> None:
        """New file (absent from remote) is caught by diff_files as `new` --
        skip here to avoid double-counting."""
        from mind_meld.cli import _has_mtime_only_changes_vs_remote

        local = {
            "sources": {
                "claude": {
                    "files": {"new.md": {"sha256": "abc", "mtime": "2026-05-12T12:00:00+00:00"}}
                }
            }
        }
        assert _has_mtime_only_changes_vs_remote(local, {}) is False

    def test_has_mtime_only_changes_false_when_identical(self) -> None:
        """Same sha + same mtime -> False (no work to do)."""
        from mind_meld.cli import _has_mtime_only_changes_vs_remote

        info = {"sha256": "abc", "mtime": "2026-05-12T12:00:00+00:00"}
        local = {"sources": {"claude": {"files": {"f.md": info}}}}
        remote = {"claude": {"files": {"f.md": info}}}
        assert _has_mtime_only_changes_vs_remote(local, remote) is False

    def test_has_mtime_only_changes_false_when_local_older(self) -> None:
        """Codex P2 regression pin: forward-only invariant. Local mtime
        OLDER than remote (e.g. after `git checkout` / file-restore / `touch
        -t` to a past date) must NOT trigger a manifest republish.
        Downgrading the manifest's recorded mtime is a silent-skip hazard:
        a peer with different bytes and mtime between the old-remote and
        the downgraded value would now hit `local_mtime > remote_mtime` on
        pull and SKIP -- silently losing the conflict surface.
        """
        from mind_meld.cli import _has_mtime_only_changes_vs_remote

        local = {
            "sources": {
                "claude": {
                    "files": {"f.md": {"sha256": "abc", "mtime": "2026-05-09T08:48:00+00:00"}}
                }
            }
        }
        remote = {
            "claude": {"files": {"f.md": {"sha256": "abc", "mtime": "2026-05-12T12:00:00+00:00"}}}
        }
        assert _has_mtime_only_changes_vs_remote(local, remote) is False

    def test_has_mtime_only_changes_tolerates_malformed_mtime(self) -> None:
        """Codex P2 (5th pass) regression pin: peer with `mtime: 1234` (int,
        not string) or unparseable ISO must NOT crash the helper. Defensive
        parse path returns False (treats as non-drifting) — better to under-
        publish than crash `mm push`/`mm status` on a peer-controlled value."""
        from mind_meld.cli import _has_mtime_only_changes_vs_remote

        # Peer wrote an int instead of an ISO string. load_manifest doesn't
        # type-check files[*].mtime, so this is reachable on the wire.
        local = {
            "sources": {
                "claude": {
                    "files": {"f.md": {"sha256": "abc", "mtime": "2026-05-12T12:00:00+00:00"}}
                }
            }
        }
        remote_int = {"claude": {"files": {"f.md": {"sha256": "abc", "mtime": 1234}}}}
        assert _has_mtime_only_changes_vs_remote(local, remote_int) is False

        remote_bad = {"claude": {"files": {"f.md": {"sha256": "abc", "mtime": "not-a-date"}}}}
        assert _has_mtime_only_changes_vs_remote(local, remote_bad) is False

    def test_has_mtime_only_changes_normalizes_z_vs_offset(self) -> None:
        """Codex P2 (5th pass): `2026-05-12T12:00:00Z` and
        `2026-05-12T12:00:00+00:00` represent the same instant but lex-sort
        differently. Parsed comparison must report no drift."""
        from mind_meld.cli import _has_mtime_only_changes_vs_remote

        local = {
            "sources": {
                "claude": {"files": {"f.md": {"sha256": "abc", "mtime": "2026-05-12T12:00:00Z"}}}
            }
        }
        remote = {
            "claude": {"files": {"f.md": {"sha256": "abc", "mtime": "2026-05-12T12:00:00+00:00"}}}
        }
        assert _has_mtime_only_changes_vs_remote(local, remote) is False

    def test_has_mtime_only_changes_respects_source_filter(self) -> None:
        """Codex P3: source_filter scopes the walk. Drift in source `gstack`
        must NOT trip when caller asks about `claude` only. Mirrors
        ``iter_source_diffs``'s same-named arg so `mm status --source X`
        doesn't surface metadata-pending hints from sources Y, Z.
        """
        from mind_meld.cli import _has_mtime_only_changes_vs_remote

        local = {
            "sources": {
                "claude": {
                    "files": {"f.md": {"sha256": "abc", "mtime": "2026-05-09T08:48:00+00:00"}}
                },
                "gstack": {
                    "files": {"g.md": {"sha256": "xyz", "mtime": "2026-05-12T12:00:00+00:00"}}
                },
            }
        }
        remote = {
            "claude": {"files": {"f.md": {"sha256": "abc", "mtime": "2026-05-09T08:48:00+00:00"}}},
            "gstack": {"files": {"g.md": {"sha256": "xyz", "mtime": "2026-05-09T08:48:00+00:00"}}},
        }
        # gstack has forward drift; claude is in sync.
        assert _has_mtime_only_changes_vs_remote(local, remote) is True
        assert _has_mtime_only_changes_vs_remote(local, remote, source_filter="gstack") is True
        assert _has_mtime_only_changes_vs_remote(local, remote, source_filter="claude") is False

    def test_push_uploads_manifest_when_only_mtime_changed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """End-to-end regression pin for the Codex P1: after resolve(local)
        bumps a file's mtime, the next push must upload the manifest even
        though no blob bytes changed. Pre-fix, the substantive-change gate
        bailed at 'Nothing to push' and the bumped mtime never reached
        peer-visible storage.

        Repro: push once (publish baseline manifest), bump a file's mtime
        in place (no content change), push again, fetch the remote manifest,
        assert the file's mtime is the bumped value not the original.
        """
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / "machine_a" / ".claude"
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        target = memory / "user_role.md"
        target.write_text("---\nname: role\n---\nEng")

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
                    "sources": [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            config_path,
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

        # Baseline push.
        assert runner.invoke(app, ["push"]).exit_code == 0

        # Capture baseline manifest's mtime for the file.
        from mind_meld.storage.keys import manifest_key

        mkey = manifest_key("dev-a")
        baseline_enc = backend.get(mkey)
        baseline_manifest = load_manifest(decrypt(baseline_enc, PASSPHRASE, MEMORY_KB))
        baseline_mtime = baseline_manifest["sources"]["claude"]["files"][
            "projects/-Users-kb-myapp/memory/user_role.md"
        ]["mtime"]

        # Bump the file's mtime in place (simulates _bump_canonical_mtime_post_resolve).
        # +120s is safely past the manifest's resolution and within future-clamp.
        bumped_ts = target.stat().st_mtime + 120.0
        os.utime(target, (bumped_ts, bumped_ts))

        # Second push: no bytes changed, only mtime. Pre-fix: "Nothing to push".
        # Post-fix: manifest uploads with bumped mtime.
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output
        assert "Push: no content changes" not in result.output, (
            f"push bailed on mtime-only change instead of refreshing the manifest:\n{result.output}"
        )

        # Verify remote manifest now carries the bumped mtime.
        updated_enc = backend.get(mkey)
        updated_manifest = load_manifest(decrypt(updated_enc, PASSPHRASE, MEMORY_KB))
        updated_mtime = updated_manifest["sources"]["claude"]["files"][
            "projects/-Users-kb-myapp/memory/user_role.md"
        ]["mtime"]
        assert updated_mtime != baseline_mtime, (
            f"manifest mtime not refreshed: baseline={baseline_mtime} updated={updated_mtime}"
        )

    def test_push_still_bails_when_truly_nothing_changed(self, tmp_path: Path, monkeypatch) -> None:
        """The mtime-only gate must NOT regress the v0.12.2 phantom-push fix:
        a push with no content changes AND no mtime drift still bails at
        the substantive-change gate. Without this assertion the new gate
        could mask the original event-row-spam bug.
        """
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / "machine_a" / ".claude"
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        (memory / "user_role.md").write_text("---\nname: role\n---\nEng")

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
                    "sources": [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            config_path,
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

        assert runner.invoke(app, ["push"]).exit_code == 0
        # Second push with truly nothing changed.
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output
        assert "Push: no content changes" in result.output, (
            f"empty push must still bail at the substantive-change gate:\n{result.output}"
        )

    def _setup_one_machine(self, tmp_path: Path, monkeypatch) -> tuple[Path, LocalBackend]:
        """Setup helper for the status/dry-run mtime-only tests below."""
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / "machine_a" / ".claude"
        memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
        memory.mkdir(parents=True)
        target = memory / "user_role.md"
        target.write_text("---\nname: role\n---\nEng")

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
                    "sources": [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            config_path,
        )
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        # Baseline push so subsequent mtime bumps register against a real remote.
        assert runner.invoke(app, ["push"]).exit_code == 0
        return target, backend

    def test_status_flags_pending_mtime_only_changes(self, tmp_path: Path, monkeypatch) -> None:
        """Codex P2 regression pin: ``mm status`` must NOT print 'All sources
        in sync' when the only pending work is a metadata-only manifest
        republish. Pre-fix, the user would resolve(local), check status,
        see 'in sync', and walk away — leaving the fleet divergent."""
        target, _ = self._setup_one_machine(tmp_path, monkeypatch)

        # Bump mtime in place (simulates _bump_canonical_mtime_post_resolve).
        bumped_ts = target.stat().st_mtime + 120.0
        os.utime(target, (bumped_ts, bumped_ts))

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "In sync with this device's last push." not in result.output, (
            f"status reported 'in sync' on a mtime-only divergence:\n{result.output}"
        )
        assert "Metadata-only changes pending" in result.output, (
            f"status did not surface the mtime-only pending hint:\n{result.output}"
        )

    def test_dry_run_push_flags_pending_mtime_only_changes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex P2: ``mm push --dry-run`` must announce the metadata-only
        republish so users previewing a push see what will happen."""
        target, _ = self._setup_one_machine(tmp_path, monkeypatch)

        bumped_ts = target.stat().st_mtime + 120.0
        os.utime(target, (bumped_ts, bumped_ts))

        result = runner.invoke(app, ["push", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Would refresh manifest" in result.output, (
            f"dry-run did not surface the metadata-only republish hint:\n{result.output}"
        )


class TestConflictOwnershipEncryptedPull:
    """Track 48A: failed replacement on one file must not stop the next."""

    def test_encrypted_replacement_failure_continues_to_next_file(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        import hashlib

        from mind_meld.cli import _download_and_apply
        from mind_meld.storage.keys import blob_key

        storage_dir = tmp_path / "storage"
        base = tmp_path / "src"
        base.mkdir()
        backend = LocalBackend(storage_dir)
        peer = "devA1234"
        first = base / "notes.md"
        second = base / "other.md"
        first.write_bytes(b"local-a")
        second.write_bytes(b"local-b")
        old = datetime.now(timezone.utc).timestamp() - 3600
        os.utime(first, (old, old))
        os.utime(second, (old, old))
        old_sidecar = base / "notes.sync-conflict-20260421-120000-v1-devA1234.md"
        old_sidecar.write_bytes(b"peer R1")
        old_mtime = old_sidecar.stat().st_mtime_ns
        first_mtime = first.stat().st_mtime_ns

        remote_a = b"peer R2 for notes"
        remote_b = b"peer R2 for other"
        sha_a = hashlib.sha256(remote_a).hexdigest()
        sha_b = hashlib.sha256(remote_b).hexdigest()
        backend.put(blob_key(peer, sha_a), encrypt(remote_a, PASSPHRASE, memory_kb=MEMORY_KB))
        backend.put(blob_key(peer, sha_b), encrypt(remote_b, PASSPHRASE, memory_kb=MEMORY_KB))
        newer = datetime.now(timezone.utc).isoformat()
        to_download = {
            "notes.md": {"sha256": sha_a, "size": len(remote_a), "mtime": newer},
            "other.md": {"sha256": sha_b, "size": len(remote_b), "mtime": newer},
        }

        real_write = fsutil.atomic_write_bytes

        def selective_write(path: Path, data: bytes, **kw):
            if path.name.startswith("notes.sync-conflict-") and path != old_sidecar:
                raise OSError("disk full")
            return real_write(path, data, **kw)

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", selective_write)
        pending = {first.resolve(): 1.0}
        _bt, outcomes = _download_and_apply(
            backend,
            base,
            to_download,
            peer,
            PASSPHRASE,
            MEMORY_KB,
            quiet=True,
            pending_inline_bumps=pending,
        )
        captured = capsys.readouterr()
        assert outcomes["failed"] == ["notes.md"]
        assert outcomes["conflicted"] == ["other.md"]
        assert first.read_bytes() == b"local-a"
        assert first.stat().st_mtime_ns == first_mtime
        assert old_sidecar.exists()
        assert old_sidecar.read_bytes() == b"peer R1"
        assert old_sidecar.stat().st_mtime_ns == old_mtime
        assert second.read_bytes() == b"local-b"
        other_sidecars = list(base.glob("other.sync-conflict-*"))
        assert len(other_sidecars) == 1
        assert other_sidecars[0].read_bytes() == remote_b
        assert pending == {first.resolve(): 1.0}
        assert captured.out == ""
        assert "sidecar write failed" in captured.err
        assert "mm:" in captured.err


class TestMixedTimestampEncryptedPull:
    """Track 51A: mixed JSONL merge must complete and continue to the next file."""

    @pytest.mark.parametrize("quiet", [True, False])
    def test_jsonl_then_ordinary_file_both_succeed(self, tmp_path: Path, quiet: bool):
        import hashlib

        from mind_meld.cli import _download_and_apply
        from mind_meld.storage.keys import blob_key

        storage_dir = tmp_path / "storage"
        base = tmp_path / "src"
        base.mkdir()
        backend = LocalBackend(storage_dir)
        peer = "devA1234"
        jsonl_path = base / "events.jsonl"
        ordinary_path = base / "later.txt"
        jsonl_path.write_bytes(_LOCAL_ONLY_JSONL)
        old = datetime.now(timezone.utc).timestamp() - 3600
        os.utime(jsonl_path, (old, old))

        sha_j = hashlib.sha256(_MIXED_REMOTE_JSONL).hexdigest()
        sha_t = hashlib.sha256(_ORDINARY_PEER_BYTES).hexdigest()
        enc_j = encrypt(_MIXED_REMOTE_JSONL, PASSPHRASE, memory_kb=MEMORY_KB)
        enc_t = encrypt(_ORDINARY_PEER_BYTES, PASSPHRASE, memory_kb=MEMORY_KB)
        backend.put(blob_key(peer, sha_j), enc_j)
        backend.put(blob_key(peer, sha_t), enc_t)
        newer = datetime.now(timezone.utc).isoformat()
        to_download = {}
        to_download["events.jsonl"] = {
            "sha256": sha_j,
            "size": len(_MIXED_REMOTE_JSONL),
            "mtime": newer,
        }
        to_download["later.txt"] = {
            "sha256": sha_t,
            "size": len(_ORDINARY_PEER_BYTES),
            "mtime": newer,
        }
        assert list(to_download) == ["events.jsonl", "later.txt"]
        assert not ordinary_path.exists()

        bt, outcomes = _download_and_apply(
            backend,
            base,
            to_download,
            peer,
            PASSPHRASE,
            MEMORY_KB,
            quiet=quiet,
        )
        assert jsonl_path.read_bytes() == _MERGED_MIXED_JSONL
        assert ordinary_path.read_bytes() == _ORDINARY_PEER_BYTES
        assert outcomes["merged"] == ["events.jsonl"]
        assert outcomes["written"] == ["later.txt"]
        assert outcomes["failed"] == []
        assert outcomes["conflicted"] == []
        assert bt == len(enc_j) + len(enc_t)

    def test_merge_noop_retry_keeps_bytes_and_mtime(self, tmp_path: Path, monkeypatch):
        import hashlib

        from mind_meld.cli import _download_and_apply
        from mind_meld.storage.keys import blob_key

        storage_dir = tmp_path / "storage"
        base = tmp_path / "src"
        base.mkdir()
        backend = LocalBackend(storage_dir)
        peer = "devA1234"
        jsonl_path = base / "events.jsonl"
        jsonl_path.write_bytes(_MERGED_MIXED_JSONL)
        sha_j = hashlib.sha256(_MIXED_REMOTE_JSONL).hexdigest()
        local_sha = hashlib.sha256(_MERGED_MIXED_JSONL).hexdigest()
        assert local_sha != sha_j
        enc_j = encrypt(_MIXED_REMOTE_JSONL, PASSPHRASE, memory_kb=MEMORY_KB)
        backend.put(blob_key(peer, sha_j), enc_j)
        newer = datetime.now(timezone.utc).isoformat()
        to_download = {
            "events.jsonl": {
                "sha256": sha_j,
                "size": len(_MIXED_REMOTE_JSONL),
                "mtime": newer,
            }
        }
        writes: list[Path] = []
        real_write = fsutil.atomic_write_bytes

        def spy_write(path: Path, data: bytes, **kw):
            writes.append(Path(path))
            return real_write(path, data, **kw)

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", spy_write)
        before = jsonl_path.stat().st_mtime_ns
        _bt, outcomes = _download_and_apply(
            backend,
            base,
            to_download,
            peer,
            PASSPHRASE,
            MEMORY_KB,
            quiet=True,
        )
        assert outcomes["unchanged"] == ["events.jsonl"]
        assert outcomes["merged"] == []
        assert outcomes["written"] == []
        assert outcomes["failed"] == []
        assert jsonl_path.read_bytes() == _MERGED_MIXED_JSONL
        assert jsonl_path.stat().st_mtime_ns == before
        assert jsonl_path.resolve() not in {p.resolve() for p in writes}

    def test_numeric_only_file_reorders_once_then_stays_quiet(self, tmp_path: Path, monkeypatch):
        """A pre-51A numeric-only file in numeric order is rewritten lexically once.

        Pins the README troubleshooting claim: after upgrade, the first pull
        that merges such a file reports ``merged`` and reorders it as
        whole-line text (``10`` before ``2``); a retry against the same peer
        snapshot is ``unchanged`` and touches neither bytes nor mtime.
        """
        import hashlib

        from mind_meld.cli import _download_and_apply
        from mind_meld.storage.keys import blob_key

        two = b'{"ts":2,"k":"same"}\n'
        ten = b'{"ts":10,"k":"same"}\n'
        three = b'{"ts":3,"k":"same"}\n'
        old_numeric_order = two + ten
        peer_bytes = two + ten + three
        lexical = ten + two + three

        storage_dir = tmp_path / "storage"
        base = tmp_path / "src"
        base.mkdir()
        backend = LocalBackend(storage_dir)
        peer = "devA1234"
        jsonl_path = base / "events.jsonl"
        jsonl_path.write_bytes(old_numeric_order)
        old = datetime.now(timezone.utc).timestamp() - 3600
        os.utime(jsonl_path, (old, old))

        sha = hashlib.sha256(peer_bytes).hexdigest()
        backend.put(
            blob_key(peer, sha),
            encrypt(peer_bytes, PASSPHRASE, memory_kb=MEMORY_KB),
        )
        to_download = {
            "events.jsonl": {
                "sha256": sha,
                "size": len(peer_bytes),
                "mtime": datetime.now(timezone.utc).isoformat(),
            }
        }

        _bt, first = _download_and_apply(
            backend,
            base,
            to_download,
            peer,
            PASSPHRASE,
            MEMORY_KB,
            quiet=True,
        )
        assert first["merged"] == ["events.jsonl"]
        assert first["failed"] == []
        assert jsonl_path.read_bytes() == lexical

        writes: list[Path] = []
        real_write = fsutil.atomic_write_bytes

        def spy_write(path: Path, data: bytes, **kw):
            writes.append(Path(path))
            return real_write(path, data, **kw)

        monkeypatch.setattr(cli_module.fsutil, "atomic_write_bytes", spy_write)
        before = jsonl_path.stat().st_mtime_ns
        _bt, second = _download_and_apply(
            backend,
            base,
            to_download,
            peer,
            PASSPHRASE,
            MEMORY_KB,
            quiet=True,
        )
        assert second["unchanged"] == ["events.jsonl"]
        assert second["merged"] == []
        assert second["failed"] == []
        assert jsonl_path.read_bytes() == lexical
        assert jsonl_path.stat().st_mtime_ns == before
        assert jsonl_path.resolve() not in {p.resolve() for p in writes}


class TestCompleteSnapshots:
    """Track 49A: complete-or-refused publication and verified uploads."""

    def _prepare(self, tmp_path, monkeypatch, *, extra_sources=None):
        from mind_meld.storage.keys import device_key, manifest_key
        from tests.conftest import MEMORY_KB, PASSPHRASE, _redirect_lock

        storage_dir = tmp_path / "storage"
        claude = tmp_path / ".claude"
        memory = claude / "projects" / "-app" / "memory"
        memory.mkdir(parents=True)
        (memory / "a.md").write_text("old bytes")
        (memory / "b.md").write_text("old bytes")
        config = {
            "device": {"id": "dev-a", "name": "A"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude), "type": "claude"},
                    *(extra_sources or []),
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        }
        config_path = tmp_path / "config.toml"
        save_config(config, config_path)
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "A")
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        _redirect_lock(monkeypatch, tmp_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        return {
            "config": config,
            "config_path": config_path,
            "backend": backend,
            "claude": claude,
            "memory": memory,
            "storage_dir": storage_dir,
            "manifest_key": manifest_key("dev-a"),
            "device_key": device_key("dev-a"),
        }

    def _push(self, extra_args=None):
        result = runner.invoke(app, ["push", "--verbose", *(extra_args or [])])
        return result

    def _prior(self, env):
        from mind_meld import sidecar as sidecar_mod

        return (
            env["backend"].get(env["manifest_key"]),
            sidecar_mod.sidecar_path().read_bytes(),
            env["backend"].get(env["device_key"]),
        )

    def _assert_prior_kept(self, env, prior, result):
        assert result.exit_code != 0 or "Error" in (result.output + (result.stderr or ""))
        assert env["backend"].get(env["manifest_key"]) == prior[0]
        from mind_meld import sidecar as sidecar_mod

        assert sidecar_mod.sidecar_path().read_bytes() == prior[1]
        assert env["backend"].get(env["device_key"]) == prior[2]
        assert "Push: " not in result.output

    def test_upload_change_never_poisons_a_shared_hash_blob(self, tmp_path, monkeypatch):
        """Track 75A flips the 49A refusal: the changed file's deferred re-read settles on
        its own revision, so each blob holds exactly the bytes its digest names."""
        from mind_meld.crypto import decrypt
        from mind_meld.storage.keys import blob_key

        env = self._prepare(tmp_path, monkeypatch)
        first = self._push()
        assert first.exit_code == 0, first.output
        old_digest = hashlib.sha256(b"old bytes").hexdigest()
        old_blob = env["backend"].get(blob_key("dev-a", old_digest))
        real_upload = cli_module._upload_changed_blobs

        def mutate_after_scan(*a, **kw):
            (env["memory"] / "a.md").write_text("changed bytes")
            return real_upload(*a, **kw)

        monkeypatch.setattr(cli_module, "_upload_changed_blobs", mutate_after_scan)
        (env["memory"] / "a.md").write_text("shared v2")
        (env["memory"] / "b.md").write_text("shared v2")
        result = self._push()
        assert result.exit_code == 0, result.output
        passphrase = "shared-cli-test-passphrase"
        assert decrypt(old_blob, passphrase, 1024) == b"old bytes"
        for text in ("changed bytes", "shared v2"):
            digest = hashlib.sha256(text.encode()).hexdigest()
            assert decrypt(env["backend"].get(blob_key("dev-a", digest)), passphrase, 1024) == (
                text.encode()
            )
        from mind_meld.manifest import load_manifest

        files = load_manifest(decrypt(env["backend"].get(env["manifest_key"]), passphrase, 1024))[
            "sources"
        ]["claude"]["files"]
        assert (
            files["projects/-app/memory/a.md"]["sha256"]
            == hashlib.sha256(b"changed bytes").hexdigest()
        )
        assert (
            files["projects/-app/memory/b.md"]["sha256"] == hashlib.sha256(b"shared v2").hexdigest()
        )

    def test_missing_upload_input_is_held_not_refused(self, tmp_path, monkeypatch):
        """Track 75A flips the 49A refusal: a new file that vanishes before upload is
        held (left out, no tombstone) and the rest of the push publishes."""
        from mind_meld.crypto import decrypt
        from mind_meld.manifest import load_manifest

        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        (env["memory"] / "c.md").write_text("new")
        (env["memory"] / "b.md").write_text("b changed")

        real_upload = cli_module._upload_changed_blobs

        def drop_then_upload(*a, **kw):
            (env["memory"] / "c.md").unlink()
            return real_upload(*a, **kw)

        monkeypatch.setattr(cli_module, "_upload_changed_blobs", drop_then_upload)
        result = self._push()
        assert result.exit_code == 0, result.output
        assert "held" in " ".join(result.output.split())
        remote = load_manifest(
            decrypt(env["backend"].get(env["manifest_key"]), "shared-cli-test-passphrase", 1024)
        )
        files = remote["sources"]["claude"]["files"]
        assert "projects/-app/memory/c.md" not in files
        assert (
            files["projects/-app/memory/b.md"]["sha256"] == hashlib.sha256(b"b changed").hexdigest()
        )
        assert not any(k.endswith("c.md") for k in remote.get("tombstones", {}))

    def test_unreadable_existing_file_does_not_tombstone(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        prior = self._prior(env)
        target = env["memory"] / "a.md"
        real_open = cli_module.manifest._open_nofollow_nonblock

        def boom(path):
            if Path(path) == target:
                raise PermissionError("denied")
            return real_open(path)

        monkeypatch.setattr(cli_module.manifest, "_open_nofollow_nonblock", boom)
        result = self._push()
        self._assert_prior_kept(env, prior, result)
        from mind_meld.crypto import decrypt
        from mind_meld.manifest import load_manifest

        remote = load_manifest(decrypt(prior[0], "shared-cli-test-passphrase", 1024))
        assert "claude:projects/-app/memory/a.md" not in remote.get("tombstones", {})

    def test_source_selection_removal_publishes_without_removal_tombstones(
        self, tmp_path, monkeypatch
    ):
        from mind_meld.crypto import decrypt
        from mind_meld.manifest import load_manifest
        from tests.conftest import MEMORY_KB

        extra = tmp_path / "notes"
        extra.mkdir()
        (extra / "keep.md").write_text("notes")
        extra_src = {
            "name": "notes",
            "path": str(extra),
            "type": "generic",
            "include_files": ["keep.md"],
        }
        env = self._prepare(tmp_path, monkeypatch, extra_sources=[extra_src])
        (env["memory"] / "gone.md").write_text("will delete")
        assert self._push().exit_code == 0
        (env["memory"] / "gone.md").unlink()
        assert self._push().exit_code == 0
        remote = load_manifest(
            decrypt(env["backend"].get(env["manifest_key"]), "shared-cli-test-passphrase", 1024)
        )
        assert any(k.startswith("claude:") and k.endswith("gone.md") for k in remote["tombstones"])

        env["config"]["sync"]["sources"] = [
            {"name": "claude", "path": str(env["claude"]), "type": "claude"},
        ]
        save_config(env["config"], env["config_path"])
        result = self._push()
        assert result.exit_code == 0, result.output
        published = load_manifest(
            decrypt(env["backend"].get(env["manifest_key"]), "shared-cli-test-passphrase", 1024)
        )
        assert "notes" not in published["sources"]
        assert any(k.endswith("gone.md") for k in published["tombstones"])
        assert not any(k.startswith("notes:") for k in published["tombstones"] if "keep.md" in k)
        second = self._push()
        assert second.exit_code == 0
        assert "Push: no content changes" in second.output or "up to date" in second.output
        _ = MEMORY_KB

    def test_dry_run_scan_failure_does_not_publish(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        prior = self._prior(env)
        target = env["memory"] / "a.md"
        real_open = cli_module.manifest._open_nofollow_nonblock

        def boom(path):
            if Path(path) == target:
                raise PermissionError("denied")
            return real_open(path)

        monkeypatch.setattr(cli_module.manifest, "_open_nofollow_nonblock", boom)
        result = self._push(["--dry-run"])
        self._assert_prior_kept(env, prior, result)

    def test_retry_after_writer_settles(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        (env["memory"] / "a.md").write_text("new bytes")
        result = self._push()
        assert result.exit_code == 0, result.output
        from mind_meld.crypto import decrypt
        from mind_meld.storage.keys import blob_key

        digest = hashlib.sha256(b"new bytes").hexdigest()
        plain = decrypt(
            env["backend"].get(blob_key("dev-a", digest)),
            "shared-cli-test-passphrase",
            1024,
        )
        assert plain == b"new bytes"

    def test_second_events_scan_failure_keeps_prior(self, tmp_path, monkeypatch):
        events_root = tmp_path / "mm-events"
        events_root.mkdir()
        extra = {
            "name": "mm-events",
            "path": str(events_root),
            "type": "generic",
            "include_dirs": ["events"],
        }
        env = self._prepare(tmp_path, monkeypatch, extra_sources=[extra])
        assert self._push().exit_code == 0
        prior = self._prior(env)
        (env["memory"] / "a.md").write_text("changed")
        calls = {"n": 0}
        real_build = cli_module.build_manifest_v2

        def flaky(*a, **kw):
            calls["n"] += 1
            cfgs = a[2] if len(a) > 2 else kw.get("sources_configs")
            names = [s.get("name") for s in (cfgs or [])]
            if "mm-events" in names and "claude" not in names:
                raise cli_module.SnapshotError("second scan failed")
            return real_build(*a, **kw)

        monkeypatch.setattr(cli_module, "build_manifest_v2", flaky)
        result = self._push()
        self._assert_prior_kept(env, prior, result)

    def test_second_absence_proof_after_events_tail_keeps_prior(self, tmp_path, monkeypatch):
        events_root = tmp_path / "mm-events"
        events_root.mkdir()
        extra = {
            "name": "mm-events",
            "path": str(events_root),
            "type": "generic",
            "include_dirs": ["events"],
        }
        env = self._prepare(tmp_path, monkeypatch, extra_sources=[extra])
        assert self._push().exit_code == 0
        prior = self._prior(env)
        (env["memory"] / "a.md").write_text("changed")
        calls = {"n": 0}
        real_prove = cli_module._prove_omitted_paths_absent

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise cli_module.SnapshotError("second proof failed")
            return real_prove(*a, **kw)

        monkeypatch.setattr(cli_module, "_prove_omitted_paths_absent", flaky)
        result = self._push()
        self._assert_prior_kept(env, prior, result)
        assert calls["n"] >= 2

    def test_missing_previously_populated_source_refuses_sibling(self, tmp_path, monkeypatch):
        extra = tmp_path / "notes"
        extra.mkdir()
        (extra / "keep.md").write_text("notes")
        extra_src = {
            "name": "notes",
            "path": str(extra),
            "type": "generic",
            "include_files": ["keep.md"],
        }
        env = self._prepare(tmp_path, monkeypatch, extra_sources=[extra_src])
        assert self._push().exit_code == 0
        prior = self._prior(env)
        shutil.rmtree(extra)
        (env["memory"] / "a.md").write_text("sibling change")
        result = self._push()
        self._assert_prior_kept(env, prior, result)
        assert "notes" in (result.output + (result.stderr or ""))

    def test_oversized_prior_file_refuses_instead_of_tombstone(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        prior = self._prior(env)
        (env["memory"] / "a.md").write_bytes(b"x" * 200)
        env["config"]["sync"]["max_file_size"] = 50
        save_config(env["config"], env["config_path"])
        result = self._push()
        self._assert_prior_kept(env, prior, result)
        assert "max_file_size" in (result.output + (result.stderr or ""))

    def test_all_previously_populated_roots_missing_refuses_not_no_sources(
        self, tmp_path, monkeypatch
    ):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        prior = self._prior(env)
        shutil.rmtree(env["claude"])
        result = self._push()
        self._assert_prior_kept(env, prior, result)
        combined = (result.output + (result.stderr or "")).replace("\n", " ")
        assert "missing after" in combined
        assert "no sync sources found" not in combined


class TestHeldFiles75A:
    """Track 75A: a file that changes while mm reads it is re-read once, then held.

    Scan-time changes ride ``manifest.read_file_revision`` and upload-time changes
    ``cli.read_file_revision`` (separate bindings), so each seam is independent.
    Holds keep the accepted prior entry; the rest of the push publishes.
    """

    REL = "projects/-app/memory/a.md"

    def _prepare(self, tmp_path, monkeypatch, *, extra_sources=None):
        return TestCompleteSnapshots()._prepare(tmp_path, monkeypatch, extra_sources=extra_sources)

    def _events_source(self, tmp_path):
        root = tmp_path / "mm-events"
        root.mkdir(exist_ok=True)
        return {
            "name": "mm-events",
            "path": str(root),
            "type": "generic",
            "include_dirs": ["events"],
        }

    def _push(self, *args):
        return runner.invoke(app, ["push", *args])

    def _spy_core(self, monkeypatch):
        """Record every ``_push_core`` result (the PushResult the commands consume)."""
        seen = []
        real_core = cli_module._push_core

        def spy(*args, **kwargs):
            result = real_core(*args, **kwargs)
            seen.append(result)
            return result

        monkeypatch.setattr(cli_module, "_push_core", spy)
        return seen

    @staticmethod
    def _text(result):
        return " ".join((result.output + (result.stderr or "")).split())

    @staticmethod
    def _published(env):
        from mind_meld.manifest import load_manifest
        from tests.conftest import PASSPHRASE as SHARED_PASSPHRASE  # what _prepare uses

        return load_manifest(
            crypto_module.decrypt(
                env["backend"].get(env["manifest_key"]), SHARED_PASSPHRASE, memory_kb=1024
            )
        )

    @staticmethod
    def _entry(env, rel, source="claude"):
        return TestHeldFiles75A._published(env)["sources"][source]["files"].get(rel)

    @staticmethod
    def _digest(text):
        return hashlib.sha256(text.encode()).hexdigest()

    @staticmethod
    def _blob_exists(env, digest):
        return env["backend"].exists(storage_keys.blob_key("dev-a", digest))

    def _flaky_reader(self, monkeypatch, owner, real, names, *, times, reason, when, before_first):
        """Wrap a ``read_file_revision`` binding so reads of ``names`` are unstable.

        ``changed`` is genuine: the descriptor signature differs between the two
        stats inside the real function, so the real refusal text (location, fix)
        is raised. ``vanished`` and ``grew`` raise a transient directly. Returns
        the per-name read counts (every read counts, settled or not).
        """
        from mind_meld import manifest as manifest_module

        calls: dict[str, int] = {}
        real_signature = manifest_module._revision_signature

        def fake(path, **kw):
            name = Path(path).name
            if name not in names or (when is not None and not when()):
                return real(path, **kw)
            calls[name] = calls.get(name, 0) + 1
            if before_first is not None:
                if calls[name] == 1:
                    before_first(Path(path))
                return real(path, **kw)
            if times is not None and calls[name] > times:
                return real(path, **kw)
            if reason != "changed":
                raise cli_module.SnapshotChanged(f"{reason} while being read", reason)
            seen = {"n": 0}

            def flipping(st):
                seen["n"] += 1
                return (*real_signature(st), seen["n"])

            monkeypatch.setattr(manifest_module, "_revision_signature", flipping)
            try:
                return real(path, **kw)
            finally:
                monkeypatch.setattr(manifest_module, "_revision_signature", real_signature)

        monkeypatch.setattr(owner, "read_file_revision", fake)
        return calls

    def _scan_flaky(self, monkeypatch, names, *, times=None, reason="changed", when=None):
        """Scan reads of files named in ``names`` are unstable (``times`` reads, else always)."""
        calls = self._flaky_reader(
            monkeypatch,
            cli_module.manifest,
            cli_module.manifest.read_file_revision,
            names,
            times=times,
            reason=reason,
            when=when,
            before_first=None,
        )
        return calls

    def _upload_flaky(self, monkeypatch, names, *, before_first=None):
        """Upload reads of files named in ``names`` are unstable (or mutate the file first)."""
        return self._flaky_reader(
            monkeypatch,
            cli_module,
            cli_module.read_file_revision,
            names,
            times=None,
            reason="changed",
            when=None,
            before_first=before_first,
        )

    def _peer_pull(self, env, tmp_path, monkeypatch, *, extra_sources=()):
        claude_b = tmp_path / "machine_b" / ".claude"
        claude_b.mkdir(parents=True)
        register_device(env["backend"], "dev-b", "B")
        path_b = tmp_path / "config_b.toml"
        save_config(
            {
                "device": {"id": "dev-b", "name": "B"},
                "storage": {"path": str(env["storage_dir"])},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [
                        {"name": "claude", "path": str(claude_b), "type": "claude"},
                        *extra_sources,
                    ],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            path_b,
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", path_b)
        try:
            result = runner.invoke(app, ["pull"])
        finally:
            monkeypatch.setattr("mind_meld.config.CONFIG_PATH", env["config_path"])
        assert result.exit_code == 0, result.output
        return claude_b

    # ── scan-time ────────────────────────────────────────────────────

    def test_scan_change_settling_on_retry_publishes_read_two(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        (env["memory"] / "a.md").write_text("new bytes")
        calls = self._scan_flaky(monkeypatch, {"a.md"}, times=1)
        result = self._push()
        assert result.exit_code == 0, result.output
        assert calls["a.md"] == 2  # the unstable read plus exactly one retry, no third read
        assert " held." not in self._text(result)
        assert self._entry(env, self.REL)["sha256"] == self._digest("new bytes")

    def test_scan_still_changing_is_held_at_prior_and_peer_pulls_prior_bytes(
        self, tmp_path, monkeypatch
    ):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        old = self._digest("old bytes")
        (env["memory"] / "a.md").write_text("new bytes")
        (env["memory"] / "b.md").write_text("b changed")
        calls = self._scan_flaky(monkeypatch, {"a.md"})
        result = self._push()
        assert result.exit_code == 0, result.output
        assert calls["a.md"] == 2  # scan read plus exactly one retry
        text = self._text(result)
        assert "1 held." in text
        assert f"claude:{self.REL}" not in text
        assert "Their previous versions (if any) stay published" not in text
        published = self._published(env)
        assert self._entry(env, self.REL)["sha256"] == old
        assert self._entry(env, "projects/-app/memory/b.md")["sha256"] == self._digest("b changed")
        assert not any(k.endswith("a.md") for k in published["tombstones"])
        assert self._blob_exists(env, old)
        assert not self._blob_exists(env, self._digest("new bytes"))
        claude_b = self._peer_pull(env, tmp_path, monkeypatch)
        assert (claude_b / self.REL).read_bytes() == b"old bytes"
        assert (claude_b / "projects/-app/memory/b.md").read_bytes() == b"b changed"

    def test_held_file_goes_out_with_a_later_push(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        (env["memory"] / "a.md").write_text("new bytes")
        with monkeypatch.context() as m:
            self._scan_flaky(m, {"a.md"})
            assert self._push().exit_code == 0
        assert self._entry(env, self.REL)["sha256"] == self._digest("old bytes")
        later = self._push()
        assert later.exit_code == 0, later.output
        assert " held." not in self._text(later)
        assert self._entry(env, self.REL)["sha256"] == self._digest("new bytes")

    @pytest.mark.parametrize("has_prior", [False, True])
    @pytest.mark.parametrize("phase", ["scan", "upload"])
    def test_never_published_held_file_is_left_out_with_no_tombstone(
        self, tmp_path, monkeypatch, has_prior, phase
    ):
        # Value: protects=first and later pushes hold while publishing stable files;
        # fails_when=hold eligibility incorrectly requires an accepted manifest;
        # why_new=the old case always published a baseline and only held during scan;
        # seam=none; no_claim=real timing or provider delivery.
        env = self._prepare(tmp_path, monkeypatch)
        if has_prior:
            assert self._push().exit_code == 0
        else:
            assert not env["backend"].exists(env["manifest_key"])
        rel = "projects/-app/memory/c.md"
        (env["memory"] / "c.md").write_text("brand new")
        (env["memory"] / "b.md").write_text("b changed")
        with monkeypatch.context() as m:
            if phase == "scan":
                self._scan_flaky(m, {"c.md"})
            else:
                self._upload_flaky(m, {"c.md"})
            seen = self._spy_core(m)
            result = self._push()
        assert result.exit_code == 0, result.output
        assert "1 held" in self._text(result)
        published = self._published(env)
        files = published["sources"]["claude"]["files"]
        assert rel not in files
        assert files["projects/-app/memory/b.md"]["sha256"] == self._digest("b changed")
        assert not any(k.endswith("c.md") for k in published["tombstones"])
        assert seen[0].held == [("claude", rel, "changed")]
        later = self._push()
        assert later.exit_code == 0, later.output
        assert self._entry(env, rel)["sha256"] == self._digest("brand new")

    @pytest.mark.parametrize("phase", ["scan", "upload"])
    def test_tombstoned_path_stays_deleted_when_the_recreated_file_is_held(
        self, tmp_path, monkeypatch, phase
    ):
        # Value: protects=accepted deletions survive a held recreation;
        # fails_when=upload holding removes the file without restoring its tombstone;
        # why_new=the scan-only case never reaches post-diff holding; seam=none;
        # no_claim=real writer timing (deterministic read mutation).
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        (env["memory"] / "a.md").unlink()
        assert self._push().exit_code == 0
        prior_tombstones = self._published(env)["tombstones"]
        assert any(k.endswith("a.md") for k in prior_tombstones)
        (env["memory"] / "a.md").write_text("recreated")
        (env["memory"] / "b.md").write_text("b changed")
        if phase == "scan":
            self._scan_flaky(monkeypatch, {"a.md"})
        else:
            self._upload_flaky(monkeypatch, {"a.md"})
        result = self._push()
        assert result.exit_code == 0, result.output
        published = self._published(env)
        assert self.REL not in published["sources"]["claude"]["files"]
        assert published["tombstones"] == prior_tombstones
        assert " held." not in self._text(result)  # deleted, not waiting

    def test_held_only_scan_exit_makes_no_put_and_never_claims_up_to_date(
        self, tmp_path, monkeypatch
    ):
        # Value: protects=held-only pushes preserve event and publication side effects;
        # fails_when=the no-op runs the events tail or writes storage or the sidecar;
        # why_new=the original test checked only manifest bytes without mm-events;
        # seam=none; no_claim=live host capture or provider delivery.
        env = self._events_env(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        before = env["backend"].get(env["manifest_key"])
        device_before = env["backend"].get(env["device_key"])
        sidecar_path = cli_module.sidecar.sidecar_path()
        tracked_paths = [sidecar_path, *env["events_dir"].glob("*.jsonl")]
        local_before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked_paths
        }
        effects = []

        def track(name, real):
            def tracked(*args, **kwargs):
                effects.append(name)
                return real(*args, **kwargs)

            return tracked

        monkeypatch.setattr(
            events_tail, "_run_events_tail", track("events-tail", events_tail._run_events_tail)
        )
        monkeypatch.setattr(cli_module.sidecar, "write", track("sidecar", cli_module.sidecar.write))
        monkeypatch.setattr(
            cli_module, "update_last_seen", track("last-seen", cli_module.update_last_seen)
        )
        monkeypatch.setattr(LocalBackend, "put", track("storage-put", LocalBackend.put))
        (env["memory"] / "a.md").write_text("new bytes")
        self._scan_flaky(monkeypatch, {"a.md"})
        seen = self._spy_core(monkeypatch)
        result = self._push()
        assert result.exit_code == 0, result.output
        text = self._text(result)
        assert "no content changes; 1 held." in text
        assert "up to date" not in text
        assert env["backend"].get(env["manifest_key"]) == before
        assert env["backend"].get(env["device_key"]) == device_before
        assert {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked_paths
        } == local_before
        assert sorted(env["events_dir"].glob("*.jsonl")) == sorted(tracked_paths[1:])
        assert effects == []
        (core,) = seen
        assert core.held == [("claude", self.REL, "changed")]
        assert core.content_changed is False and core.content_accepted is False

    def test_scan_hold_does_not_suppress_an_unrelated_mtime_only_publication(
        self, tmp_path, monkeypatch
    ):
        # Value: protects=held files do not suppress unrelated mtime-only publication;
        # fails_when=held-only handling returns before the metadata-change gate;
        # why_new=mtime tests had no holds and held tests changed neighbor bytes;
        # seam=none; no_claim=real transport or physical two-Mac qualification.
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        prior = self._published(env)
        prior_files = prior["sources"]["claude"]["files"]
        neighbor_rel = "projects/-app/memory/b.md"
        neighbor = env["memory"] / "b.md"
        bumped = neighbor.stat().st_mtime + 120
        os.utime(neighbor, (bumped, bumped))
        (env["memory"] / "a.md").write_text("held new bytes")
        self._scan_flaky(monkeypatch, {"a.md"})
        seen = self._spy_core(monkeypatch)

        result = self._push()

        assert result.exit_code == 0, result.output
        assert "1 held." in self._text(result)
        published = self._published(env)
        files = published["sources"]["claude"]["files"]
        assert files[self.REL] == prior_files[self.REL]
        assert files[neighbor_rel]["sha256"] == prior_files[neighbor_rel]["sha256"]
        assert (
            files[neighbor_rel]["mtime"]
            == datetime.fromtimestamp(neighbor.stat().st_mtime, tz=timezone.utc).isoformat()
        )
        assert files[neighbor_rel]["mtime"] != prior_files[neighbor_rel]["mtime"]
        assert published["tombstones"] == prior["tombstones"]
        assert not self._blob_exists(env, self._digest("held new bytes"))
        (core,) = seen
        assert core.held == [("claude", self.REL, "changed")]
        assert core.content_changed is True and core.content_accepted is True
        assert (core.total_new, core.total_modified, core.content_files) == (0, 0, 0)

    def test_vanished_scan_file_is_held_and_worded_as_vanished(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push("--verbose").exit_code == 0
        (env["memory"] / "a.md").write_text("new bytes")
        (env["memory"] / "b.md").write_text("b changed")
        self._scan_flaky(monkeypatch, {"a.md"}, reason="vanished")
        result = self._push("--verbose")
        assert result.exit_code == 0, result.output
        text = self._text(result)
        assert "gone while being read" in text
        assert "recorded as deleted on a later push" in text
        assert self._entry(env, self.REL)["sha256"] == self._digest("old bytes")

    def test_default_output_counts_held_and_verbose_lists_all(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        names = {f"n{i}.md" for i in range(5)}
        for name in names:
            (env["memory"] / name).write_text(name)
        (env["memory"] / "b.md").write_text("b changed")
        with monkeypatch.context() as m:
            self._scan_flaky(m, names)
            default = self._push()
        assert default.exit_code == 0, default.output
        text = self._text(default)
        assert "5 held" in text
        assert "(+2 more)" not in text
        assert sum(f"memory/n{i}.md" in text for i in range(5)) == 0
        (env["memory"] / "b.md").write_text("b changed again")
        with monkeypatch.context() as m:
            self._scan_flaky(m, names)
            verbose = self._push("--verbose")
        text = self._text(verbose)
        assert "+2 more" not in text
        assert all(f"memory/n{i}.md" in text for i in range(5))

    def test_dry_run_applies_the_scan_hold_and_shows_the_count(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        before = env["backend"].get(env["manifest_key"])
        (env["memory"] / "a.md").write_text("new bytes")
        (env["memory"] / "b.md").write_text("b changed")
        self._scan_flaky(monkeypatch, {"a.md"})
        result = self._push("--dry-run")
        assert result.exit_code == 0, result.output
        text = self._text(result)
        assert "1 file would be held, not yet synced (still changing)" in text
        assert f"claude:{self.REL}" in text
        assert text.index("would be held") < text.rindex("Completed in")
        assert text.index("Source 'claude'") < text.index("would be held")
        assert env["backend"].get(env["manifest_key"]) == before

    def test_dry_run_held_only_says_nothing_would_be_pushed(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        (env["memory"] / "a.md").write_text("new bytes")
        self._scan_flaky(monkeypatch, {"a.md"})
        result = self._push("--dry-run")
        assert result.exit_code == 0, result.output
        text = self._text(result)
        assert "Nothing would be pushed; 1 file not yet synced." in text
        assert "up to date" not in text

    # ── upload-time ──────────────────────────────────────────────────

    @pytest.mark.parametrize("settled_text", ["v3 written", "old bytes"])
    def test_upload_change_settles_and_patches_the_entry_to_read_two(
        self, tmp_path, monkeypatch, settled_text
    ):
        from tests.conftest import PASSPHRASE as SHARED_PASSPHRASE

        # Value: protects=upload retry may return to prior bytes without losing counters;
        # fails_when=prior-digest equality suppresses the patch or fresh-diff counts;
        # why_new=existing equality case uses scanned bytes and no accepted prior;
        # seam=none; no_claim=real concurrent writer timing or two-Mac transport.
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        prior = dict(self._entry(env, self.REL))
        path = env["memory"] / "a.md"
        prior_mtime_ns = path.stat().st_mtime_ns
        path.write_text("v2")
        expected = {}

        def settle(path):
            path.write_text(settled_text)
            stamp = prior_mtime_ns + 2_000_000_000
            os.utime(path, ns=(stamp, stamp))
            expected["mtime"] = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat()

        calls = self._upload_flaky(monkeypatch, {"a.md"}, before_first=settle)
        seen = self._spy_core(monkeypatch)
        result = self._push()
        assert result.exit_code == 0, result.output
        assert calls["a.md"] == 2
        assert " held." not in self._text(result)
        entry = self._entry(env, self.REL)
        assert entry["sha256"] == self._digest(settled_text)
        assert entry["mtime"] == expected["mtime"]
        assert entry["mtime"] != prior["mtime"]
        (core,) = seen
        assert core.held == []
        assert (core.total_new, core.total_modified, core.content_files) == (0, 1, 1)
        if settled_text == "old bytes":
            assert entry["sha256"] == prior["sha256"]
        blob = env["backend"].get(storage_keys.blob_key("dev-a", entry["sha256"]))
        assert (
            crypto_module.decrypt(blob, SHARED_PASSPHRASE, memory_kb=1024) == settled_text.encode()
        )
        assert not self._blob_exists(env, self._digest("v2"))

    def test_upload_still_changing_holds_one_file_and_publishes_the_rest(
        self, tmp_path, monkeypatch
    ):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        (env["memory"] / "a.md").write_text("a2")
        (env["memory"] / "b.md").write_text("b2")
        (env["memory"] / "c.md").write_text("c2 never published")
        calls = self._upload_flaky(monkeypatch, {"a.md", "c.md"})
        seen = self._spy_core(monkeypatch)
        result = self._push()
        assert result.exit_code == 0, result.output
        assert calls == {"a.md": 2, "c.md": 2}
        (core,) = seen
        assert [(src, rel) for src, rel, _reason in core.held] == [
            ("claude", "projects/-app/memory/a.md"),
            ("claude", "projects/-app/memory/c.md"),
        ]
        # a.md (modified) and c.md (new) left the counters; b.md alone remains.
        assert (core.total_new, core.total_modified, core.content_files) == (0, 1, 1)
        assert core.content_changed is True  # the pre-upload gate fact is not recomputed
        assert core.content_accepted is True
        text = self._text(result)
        assert "2 held." in text
        assert "1 content change published" in text  # b only: held a.md left the counts
        assert text.splitlines()[-1] == "Push: 1 content change published; 2 held."
        assert self._entry(env, self.REL)["sha256"] == self._digest("old bytes")
        assert self._entry(env, "projects/-app/memory/b.md")["sha256"] == self._digest("b2")
        assert self._entry(env, "projects/-app/memory/c.md") is None
        assert not any(k.endswith("c.md") for k in self._published(env)["tombstones"])
        for entry in self._published(env)["sources"]["claude"]["files"].values():
            assert self._blob_exists(env, entry["sha256"])  # no dangling entry
        assert not self._blob_exists(env, self._digest("a2"))

    def test_incident_replay_upload_change_completes_and_next_push_publishes(
        self, tmp_path, monkeypatch
    ):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        for name in ("a.md", "b.md", "c.md"):
            (env["memory"] / name).write_text(f"{name} v2")
        with monkeypatch.context() as m:
            self._upload_flaky(m, {"b.md"})
            first = self._push()
        assert first.exit_code == 0, first.output
        assert "1 held" in self._text(first)
        assert self._entry(env, "projects/-app/memory/c.md")["sha256"] == self._digest("c.md v2")
        second = self._push()
        assert second.exit_code == 0, second.output
        assert " held." not in self._text(second)
        assert self._entry(env, "projects/-app/memory/b.md")["sha256"] == self._digest("b.md v2")

    def test_upload_held_only_push_still_puts_the_manifest(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        before = env["backend"].get(env["manifest_key"])
        (env["memory"] / "a.md").write_text("a2")
        self._upload_flaky(monkeypatch, {"a.md"})
        seen = self._spy_core(monkeypatch)
        result = self._push()
        assert result.exit_code == 0, result.output
        assert "1 held." in self._text(result)
        assert env["backend"].get(env["manifest_key"]) != before  # the manifest was put
        assert self._entry(env, self.REL)["sha256"] == self._digest("old bytes")
        (core,) = seen
        assert core.content_accepted is True and len(core.held) == 1
        assert (core.total_new, core.total_modified) == (0, 0)

    def test_held_mm_internal_upload_leaves_content_files_alone(self, tmp_path, monkeypatch):
        env = self._events_env(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        day = self._day_file(env)
        (env["memory"] / "a.md").write_text("content push")
        self._upload_flaky(monkeypatch, {day.name})
        seen = self._spy_core(monkeypatch)
        result = self._push()
        assert result.exit_code == 0, result.output
        (core,) = seen
        assert [(src, reason) for src, _rel, reason in core.held] == [("mm-events", "changed")]
        # content_files counted only the user file; the held internal file never was.
        assert core.content_files == 1
        assert (core.total_new, core.total_modified) == (0, 1)
        rel = f"events/{day.name}"
        assert self._blob_exists(env, self._entry(env, rel, "mm-events")["sha256"])

    # ── recovered prior ──────────────────────────────────────────────

    def _damage_prior(self, env, recovery):
        from tests.conftest import PASSPHRASE as SHARED_PASSPHRASE

        if recovery == "conflict-copies-only":
            conflict = env["backend"].root / Path(env["manifest_key"]).with_name(
                "manifest.json 2.enc"
            )
            conflict.write_bytes(env["backend"].get(env["manifest_key"]))
        env["backend"].put(env["manifest_key"], b"not a manifest")
        fetch = cli_module._fetch_remote_manifest(
            env["backend"], "dev-a", SHARED_PASSPHRASE, MEMORY_KB
        )
        assert fetch.status == ("ok" if recovery == "conflict-copies-only" else "corrupt")
        assert fetch.conflict_copies_only == (recovery == "conflict-copies-only")

    @pytest.mark.parametrize("recovery", ["corrupt", "conflict-copies-only"])
    def test_recovered_prior_refuses_a_still_changing_file_naming_it_and_the_fix(
        self, tmp_path, monkeypatch, recovery
    ):
        # Value: protects=recovered priors never hold an unstable scan;
        # fails_when=conflict-copy recovery is treated as accepted canonical state;
        # why_new=only corrupt-canonical recovery was exercised; seam=none;
        # no_claim=provider conflict-copy arrival timing (local backend).
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        self._damage_prior(env, recovery)
        (env["memory"] / "a.md").write_text("new bytes")
        self._scan_flaky(monkeypatch, {"a.md"})
        result = self._push()
        assert result.exit_code != 0
        text = self._text(result)
        assert self.REL in text
        assert "changed while being read" in text
        assert "Wait for the writer to finish" in text
        assert text.endswith("Run: mm push")
        assert env["backend"].get(env["manifest_key"]) == b"not a manifest"

    @pytest.mark.parametrize("recovery", ["corrupt", "conflict-copies-only"])
    def test_recovered_prior_still_publishes_when_the_retry_settles(
        self, tmp_path, monkeypatch, recovery
    ):
        # Value: protects=recovery can publish a settled retry;
        # fails_when=hold ineligibility disables the successful retry;
        # why_new=the conflict-copy recovery branch lacked coverage; seam=none;
        # no_claim=real concurrent writer timing (deterministic read mutation).
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        self._damage_prior(env, recovery)
        (env["memory"] / "a.md").write_text("new bytes")
        calls = self._scan_flaky(monkeypatch, {"a.md"}, times=1)
        result = self._push()
        assert result.exit_code == 0, result.output
        assert calls["a.md"] == 2  # the retry ran even though a hold is unavailable
        assert self._entry(env, self.REL)["sha256"] == self._digest("new bytes")

    @pytest.mark.parametrize("recovery", ["corrupt", "conflict-copies-only"])
    def test_recovered_prior_refuses_an_upload_that_keeps_changing(
        self, tmp_path, monkeypatch, recovery
    ):
        # Value: protects=conflict-copy recovery refuses an unstable upload;
        # fails_when=upload holding is enabled for an unaccepted recovery prior;
        # why_new=the upload test covered corrupt canonical state only; seam=none;
        # no_claim=real concurrent writer timing (deterministic read mutation).
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        self._damage_prior(env, recovery)
        (env["memory"] / "a.md").write_text("new bytes")
        self._upload_flaky(monkeypatch, {"a.md"})
        result = self._push()
        assert result.exit_code != 0
        assert env["backend"].get(env["manifest_key"]) == b"not a manifest"

    # ── mm-events rescan ─────────────────────────────────────────────

    def _events_env(self, tmp_path, monkeypatch, *, subtree=False):
        events_src = self._events_source(tmp_path)
        if subtree:
            sub = Path(events_src["path"]) / "events" / "sub"
            sub.mkdir(parents=True)
            (sub / "x.jsonl").write_text('{"type":"note","n":1}\n')
        monkeypatch.setattr(events_tail, "_default_host_readers", lambda *a, **kw: ())
        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda *a, **kw: _mm_events.GitRootDiscovery([], [], False),
        )
        env = self._prepare(tmp_path, monkeypatch, extra_sources=[events_src])
        env["events_dir"] = Path(events_src["path"]) / "events"
        return env

    def _phase_spy(self, monkeypatch, after_scan=None):
        """Track which build_manifest_v2 call is the first scan versus the mm-events rescan."""
        real_build = cli_module.build_manifest_v2
        state = {"phase": "idle"}

        def spy(*a, **kw):
            cfgs = a[2] if len(a) > 2 else kw.get("sources_configs")
            names = [c.get("name") for c in cfgs]
            rescan = names == ["mm-events"]
            state["phase"] = "rescan" if rescan else "scan"
            try:
                manifest = real_build(*a, **kw)
            finally:
                state["phase"] = "idle"
            if not rescan and after_scan is not None:
                after_scan()
            return manifest

        monkeypatch.setattr(cli_module, "build_manifest_v2", spy)
        return state

    def _day_file(self, env):
        (day,) = sorted(env["events_dir"].glob("*.jsonl"))
        return day

    def test_rescan_hold_carries_the_accepted_prior_never_a_first_scan_revision(
        self, tmp_path, monkeypatch
    ):
        # Value: protects=a peer can pull the accepted bytes of a rescan-held event file;
        # fails_when=the manifest references the unuploaded first-scan revision;
        # why_new=the prior peer assertion selected only Claude content; seam=none;
        # no_claim=physical two-Mac transport (two configs, one local backend).
        env = self._events_env(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        day = self._day_file(env)
        rel = f"events/{day.name}"
        prior = self._entry(env, rel, "mm-events")["sha256"]
        prior_bytes = day.read_bytes()
        lines = day.read_text().splitlines()
        day.write_text("\n".join([*lines, lines[-1]]) + "\n")  # the first scan will see this
        first_scan_digest = hashlib.sha256(day.read_bytes()).hexdigest()
        assert first_scan_digest != prior
        (env["memory"] / "a.md").write_text("content push")
        state = self._phase_spy(monkeypatch)
        self._scan_flaky(monkeypatch, {day.name}, when=lambda: state["phase"] == "rescan")
        result = self._push()
        assert result.exit_code == 0, result.output
        assert "held" in self._text(result)
        assert self._entry(env, rel, "mm-events")["sha256"] == prior
        assert not self._blob_exists(env, first_scan_digest)
        for source in self._published(env)["sources"].values():
            for entry in source["files"].values():
                assert self._blob_exists(env, entry["sha256"])  # nothing dangles
        events_b = tmp_path / "machine_b" / "mm-events"
        events_b.mkdir(parents=True)
        claude_b = self._peer_pull(
            env,
            tmp_path,
            monkeypatch,
            extra_sources=[
                {
                    "name": "mm-events",
                    "path": str(events_b),
                    "type": "generic",
                    "include_dirs": ["events"],
                }
            ],
        )
        assert (claude_b / "projects/-app/memory/a.md").read_bytes() == b"content push"
        assert (events_b / rel).read_bytes() == prior_bytes

    def test_rescan_reading_a_scan_held_path_cleanly_unholds_it(self, tmp_path, monkeypatch):
        env = self._events_env(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        day = self._day_file(env)
        rel = f"events/{day.name}"
        (env["memory"] / "a.md").write_text("content push")
        state = self._phase_spy(monkeypatch)
        self._scan_flaky(monkeypatch, {day.name}, when=lambda: state["phase"] == "scan")
        result = self._push()
        assert result.exit_code == 0, result.output
        assert " held." not in self._text(result)
        published = self._entry(env, rel, "mm-events")["sha256"]
        assert published == hashlib.sha256(day.read_bytes()).hexdigest()
        assert self._blob_exists(env, published)

    def test_scan_held_path_under_a_checkout_the_rescan_reports_moves_to_frozen(
        self, tmp_path, monkeypatch
    ):
        env = self._events_env(tmp_path, monkeypatch, subtree=True)
        assert self._push().exit_code == 0
        sub = env["events_dir"] / "sub"
        rel = "events/sub/x.jsonl"
        prior = self._entry(env, rel, "mm-events")["sha256"]
        (sub / "x.jsonl").write_text('{"type":"note","n":2}\n')
        (env["memory"] / "a.md").write_text("content push")
        state = self._phase_spy(monkeypatch, after_scan=lambda: (sub / ".git").mkdir())
        self._scan_flaky(monkeypatch, {"x.jsonl"}, when=lambda: state["phase"] == "scan")
        result = self._push()
        assert result.exit_code == 0, result.output
        text = self._text(result)
        assert " held." not in text  # frozen, out of the held count
        assert self._entry(env, rel, "mm-events")["sha256"] == prior
        assert self._blob_exists(env, prior)
        assert not any(k.endswith("x.jsonl") for k in self._published(env)["tombstones"])


@pytest.fixture
def apply_failure_fleet(tmp_path, monkeypatch):
    """Real encrypted peers and CLI, all state isolated by conftest into tmp_path."""
    from types import SimpleNamespace

    from mind_meld import pullhistory
    from mind_meld.storage.keys import blob_key, manifest_key

    storage_dir = tmp_path / "storage53"
    base = tmp_path / "claude53"
    memory = base / "projects/-app/memory"
    memory.mkdir(parents=True)
    backend = LocalBackend(storage_dir)
    init = bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    crypto_module.set_crypto_session(init.root_salt, MEMORY_KB)
    register_device(backend, "dev-b", "Mac B")
    config_path = TestPushPullRoundTrip()._make_config(
        tmp_path, storage_dir, base, "dev-b", "Mac B"
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    prefix = "projects/-app/memory/"

    def publish(files, *, peer="dev-a", name="Mac A", metadata=None):
        register_device(backend, peer, name)
        entries = {}
        for rel, data in files.items():
            digest = hashlib.sha256(data).hexdigest()
            backend.put(blob_key(peer, digest), encrypt(data, PASSPHRASE, MEMORY_KB))
            entries[prefix + rel] = {
                "sha256": digest,
                "size": len(data),
                "mtime": "2020-01-01T00:00:00+00:00",
            }
        if metadata:
            for rel, value in metadata.items():
                entries[prefix + rel] = value
        document = {
            "version": 2,
            "device_id": peer,
            "sources": {"claude": {"files": entries}},
            "tombstones": {},
        }
        backend.put(
            manifest_key(peer), encrypt(serialize_manifest(document), PASSPHRASE, MEMORY_KB)
        )

    calls = []
    original_fsync = fsutil.fsync_dir

    def fsync(path):
        calls.append(Path(path))
        original_fsync(path)

    monkeypatch.setattr(fsutil, "fsync_dir", fsync)

    def records():
        return [r for r in pullhistory.read_records() if r.get("verb") == "pull"]

    def ordered(names):
        # Pin the apply sequence explicitly for interruption assertions. The
        # real diff/filter/download stack runs; this only orders its batch.
        original = cli_module._download_and_apply

        def apply(backend, base_path, to_download, *args, **kwargs):
            order = [prefix + name for name in names]
            assert set(to_download) == set(order)
            return original(backend, base_path, {p: to_download[p] for p in order}, *args, **kwargs)

        monkeypatch.setattr(cli_module, "_download_and_apply", apply)

    return SimpleNamespace(
        base=base,
        memory=memory,
        backend=backend,
        publish=publish,
        records=records,
        fsync_calls=calls,
        prefix=prefix,
        ordered=ordered,
        log=memory.parent / ".mind-meld-log.md",
    )


class TestApplyRecovery53A:
    @pytest.mark.parametrize("command", ["pull", "autopull"])
    @pytest.mark.parametrize("blocked", ["bbb/inside.txt", "bbb/deeper/inside.txt"])
    def test_collision_records_all_files_and_retries(self, apply_failure_fleet, command, blocked):
        fleet = apply_failure_fleet
        fleet.publish({"aaa.txt": b"a", blocked: b"b", "ccc.txt": b"c"})
        (fleet.memory / "bbb").write_bytes(b"mine")
        result = runner.invoke(app, [command])
        assert result.exit_code == (1 if command == "pull" else 0), result.output
        assert (fleet.memory / "aaa.txt").read_bytes() == b"a"
        assert (fleet.memory / "ccc.txt").read_bytes() == b"c"
        assert (fleet.memory / "bbb").read_bytes() == b"mine"
        assert "Mac A published a folder named 'bbb' where this Mac has a file" in result.stderr
        assert result.stderr.count("cannot create folder") == 1
        assert {(r["rel_path"], r["action"]) for r in fleet.records()} == {
            (fleet.prefix + "aaa.txt", "written"),
            (fleet.prefix + blocked, "failed"),
            (fleet.prefix + "ccc.txt", "written"),
        }
        assert len(fleet.records()) == 3
        assert "aaa.txt" in fleet.log.read_text()
        assert "ccc.txt" in fleet.log.read_text()
        assert fleet.memory in fleet.fsync_calls
        if command == "autopull":
            assert result.stdout == ""
            crumb = cli_module._read_autorun_breadcrumbs()["pull"]
            assert (crumb["outcome"], crumb["detail"]) == ("degraded", "1 file(s) failed")
            assert (
                "mm: 1 file(s) failed to apply - see the warnings above; "
                "they retry on the next pull" in result.stderr
            )
            status_result = runner.invoke(app, ["status"])
            assert status_result.exit_code == 0, status_result.output
            assert "Files failed to apply on the last auto-pull" in status_result.stdout
        else:
            assert "Pull incomplete [result-degraded]: 2 files applied, 1 failed" in result.stderr
            assert "Pull complete." not in result.stdout
        (fleet.memory / "bbb").rename(fleet.memory / "bbb.local")
        retry = runner.invoke(app, ["autopull"])
        assert retry.exit_code == 0, retry.output
        assert (fleet.memory / blocked).read_bytes() == b"b"
        assert (fleet.memory / "bbb.local").read_bytes() == b"mine"
        assert fleet.memory / "bbb" in fleet.fsync_calls
        if "/deeper/" in blocked:
            assert fleet.memory / "bbb/deeper" in fleet.fsync_calls
        assert not [r for r in fleet.records()[3:] if r["action"] == "failed"]
        quiet = runner.invoke(app, ["autopull"])
        assert quiet.exit_code == 0
        assert quiet.stdout == quiet.stderr == ""

    def test_failed_apply_skips_manifest_conflict_cleanup(self, apply_failure_fleet, monkeypatch):
        fleet = apply_failure_fleet
        calls = []
        monkeypatch.setattr(
            cli_module,
            "_cleanup_conflict_copies",
            lambda *a, **k: calls.append(1) or 0,
        )
        fleet.publish({"aaa.txt": b"a", "bbb/inside.txt": b"b"})
        (fleet.memory / "bbb").write_bytes(b"mine")
        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0, result.output
        assert not calls
        (fleet.memory / "bbb").rename(fleet.memory / "bbb.local")
        retry = runner.invoke(app, ["autopull"])
        assert retry.exit_code == 0, retry.output
        assert calls == [1]

    @pytest.mark.parametrize("command", ["pull", "autopull"])
    @pytest.mark.parametrize("entry", [None, "scalar", [], {}, {"sha256": 4}])
    def test_bad_peer_metadata_does_not_stop_another_peer(
        self, apply_failure_fleet, command, entry
    ):
        fleet = apply_failure_fleet
        fleet.publish({"bad.txt": b"bad"}, metadata={"bad.txt": entry})
        fleet.publish({"good.txt": b"good"}, peer="dev-c", name="Mac C")
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        assert "is corrupt - skipping pull from this device" in " ".join(result.output.split())
        assert (fleet.memory / "good.txt").read_bytes() == b"good"
        assert not (fleet.memory / "bad.txt").exists()
        assert [r["action"] for r in fleet.records()] == ["written"]

    @pytest.mark.parametrize("command", ["pull", "autopull"])
    def test_loaded_int_mtime_conflicts_and_later_file_arrives(self, apply_failure_fleet, command):
        fleet = apply_failure_fleet
        fleet.publish(
            {"notes.md": b"remote", "later.txt": b"later"},
            metadata={
                "notes.md": {"sha256": hashlib.sha256(b"remote").hexdigest(), "mtime": 1234},
            },
        )
        (fleet.memory / "notes.md").write_bytes(b"local")
        fleet.ordered(["notes.md", "later.txt"])
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        assert (fleet.memory / "notes.md").read_bytes() == b"local"
        assert next(fleet.memory.glob("notes.sync-conflict-*")).read_bytes() == b"remote"
        assert (fleet.memory / "later.txt").read_bytes() == b"later"
        assert {r["action"] for r in fleet.records()} == {"conflicted", "written"}

    @pytest.mark.parametrize("command", ["pull", "autopull"])
    @pytest.mark.parametrize(
        "failure", ["runtime", "keyboard", "post-write", "post-sidecar", "post-lcs"]
    )
    def test_interruption_keeps_completed_bookkeeping(
        self, apply_failure_fleet, monkeypatch, command, failure
    ):
        fleet = apply_failure_fleet
        fleet.publish({"aaa.txt": b"a", "bbb.txt": b"b", "ccc.txt": b"c"})
        fleet.ordered(["aaa.txt", "bbb.txt", "ccc.txt"])
        exc = (
            KeyboardInterrupt("original interrupt")
            if failure == "keyboard"
            else RuntimeError("original apply error")
        )
        if failure == "keyboard":
            original_get = LocalBackend.get
            digest = hashlib.sha256(b"b").hexdigest()

            def get(backend, key):
                if digest in key:
                    raise exc
                return original_get(backend, key)

            monkeypatch.setattr(LocalBackend, "get", get)
        else:
            original_apply = cli_module._apply_incoming_file
            if failure in ("post-sidecar", "post-lcs"):
                (fleet.memory / "bbb.txt").write_bytes(b"local")
                os.utime(fleet.memory / "bbb.txt", (1, 1))
            if failure == "post-lcs":
                monkeypatch.setattr(
                    cli_module, "_prompt_conflict_choice", lambda *a, **kw: ("merge", b"local\nb\n")
                )

            def apply(**kwargs):
                if kwargs["rel_path"] == fleet.prefix + "bbb.txt":
                    if failure.startswith("post-"):
                        if failure == "post-lcs":
                            kwargs["interactive_resolve"] = True
                        original_apply(**kwargs)
                    raise exc
                return original_apply(**kwargs)

            monkeypatch.setattr(cli_module, "_apply_incoming_file", apply)
        original_core = cli_module._pull_core
        propagated = []

        def core(*args, **kwargs):
            try:
                return original_core(*args, **kwargs)
            except BaseException as error:
                propagated.append(error)
                raise

        monkeypatch.setattr(cli_module, "_pull_core", core)
        result = runner.invoke(app, [command])
        assert propagated == [exc]
        if command == "autopull" and failure != "keyboard":
            assert result.exit_code == 0
            crumb = cli_module._read_autorun_breadcrumbs()["pull"]
            assert (crumb["outcome"], crumb["detail"]) == ("failed", "RuntimeError")
        else:
            assert result.exit_code != 0
            # Click converts Ctrl-C to Abort at its outer CLI boundary.
            if failure != "keyboard":
                assert result.exit_code == 1
        assert (fleet.memory / "aaa.txt").read_bytes() == b"a"
        assert not (fleet.memory / "ccc.txt").exists()
        records = fleet.records()
        assert (fleet.prefix + "aaa.txt", "written") in {
            (r["rel_path"], r["action"]) for r in records
        }
        expected = 2 if failure.startswith("post-") else 1
        assert len(records) == expected
        if expected == 2:
            word = {
                "post-write": "written",
                "post-sidecar": "conflicted",
                "post-lcs": "merged-via-lcs",
            }[failure]
            assert (fleet.prefix + "bbb.txt", word) in {
                (r["rel_path"], r["action"]) for r in records
            }
        assert "aaa.txt" in fleet.log.read_text()
        assert fleet.memory in fleet.fsync_calls
        assert "Pull: " not in result.stdout
        if command == "pull":
            assert "completed changes remain" in result.stderr
            run = cli_module.attemptlog.read_runs()["runs"]["pull"]
            assert run["outcome"] == "failed"
            assert run["cause"] == ("interrupted" if failure == "keyboard" else "unexpected-error")
            assert (
                sum(run["counts"][k] for k in ("total_written", "total_merged", "total_conflicted"))
                == expected
            )
            assert run["content_accepted"] is None

    def test_abort_after_keep_local_records_without_draining(
        self, apply_failure_fleet, monkeypatch
    ):
        fleet = apply_failure_fleet
        fleet.publish({"new.txt": b"new", "aaa.txt": b"peer a", "bbb.txt": b"peer b"})
        for name in ("aaa.txt", "bbb.txt"):
            (fleet.memory / name).write_bytes(b"local")
            os.utime(fleet.memory / name, (1, 1))
        fleet.ordered(["new.txt", "aaa.txt", "bbb.txt"])
        drain = []
        monkeypatch.setattr(
            cli_module, "_drain_inline_bumps", lambda pending: drain.append(dict(pending))
        )
        result = runner.invoke(app, ["pull", "--conflict-mode", "prompt"], input="l\na\n")
        assert result.exit_code != 0
        assert [(r["rel_path"], r["action"]) for r in fleet.records()] == [
            (fleet.prefix + "new.txt", "written"),
            (fleet.prefix + "aaa.txt", "skipped"),
        ]
        assert "aaa.txt" in fleet.log.read_text()
        assert (fleet.memory / "aaa.txt").stat().st_mtime == 1
        assert fleet.memory in fleet.fsync_calls
        assert not drain
        assert "Pull interrupted [interrupted]" in result.stderr

    @pytest.mark.parametrize("command", ["pull", "autopull"])
    @pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
    def test_interrupted_history_resumes_remaining_rows_once(
        self, apply_failure_fleet, monkeypatch, command, exception_type
    ):
        fleet = apply_failure_fleet
        fleet.publish({"aaa.txt": b"a", "bbb.txt": b"b", "ccc.txt": b"c"})
        fleet.ordered(["aaa.txt", "bbb.txt", "ccc.txt"])
        exc = exception_type(47)
        original_append = cli_module.pullhistory.append
        attempted = []

        def append(**kwargs):
            if kwargs.get("verb") == "pull":
                name = kwargs["rel_path"]
                attempted.append(name)
                if name == fleet.prefix + "ccc.txt":
                    assert fleet.memory in fleet.fsync_calls
                original_append(**kwargs)
                if name == fleet.prefix + "bbb.txt":
                    assert fleet.memory not in fleet.fsync_calls
                    raise exc
            else:
                original_append(**kwargs)

        monkeypatch.setattr(cli_module.pullhistory, "append", append)
        original_core = cli_module._pull_core
        propagated = []

        def core(*args, **kwargs):
            try:
                return original_core(*args, **kwargs)
            except BaseException as error:
                propagated.append(error)
                raise

        monkeypatch.setattr(cli_module, "_pull_core", core)
        original_summary = cli_module._print_pull_summary
        summaries = []

        def summary(result, **kwargs):
            summaries.append((result.total_written, kwargs["interruption"]))
            original_summary(result, **kwargs)

        monkeypatch.setattr(cli_module, "_print_pull_summary", summary)
        drains = []
        monkeypatch.setattr(cli_module, "_drain_inline_bumps", lambda pending: drains.append(1))
        result = runner.invoke(app, [command])
        assert result.exit_code != 0
        assert propagated == [exc]
        expected_paths = [fleet.prefix + name for name in ("aaa.txt", "bbb.txt", "ccc.txt")]
        assert attempted == expected_paths
        assert [(r["rel_path"], r["action"]) for r in fleet.records()] == [
            (path, "written") for path in expected_paths
        ]
        log = fleet.log.read_text()
        for name, data in (("aaa.txt", b"a"), ("bbb.txt", b"b"), ("ccc.txt", b"c")):
            assert name in log
            assert (fleet.memory / name).read_bytes() == data
        assert summaries == [(3, "interrupted")]
        assert not drains
        assert "Pull: " not in result.stdout

    @pytest.mark.parametrize("command", ["pull", "autopull"])
    @pytest.mark.parametrize(
        "recovery_failure",
        ["sync-log", "fsync", "fsync-close", "summary", "cleanup", "bookkeeping", "before-source"],
    )
    def test_recovery_preserves_original_and_exactly_once(
        self, apply_failure_fleet, monkeypatch, command, recovery_failure
    ):
        from mind_meld.errors import StorageError

        fleet = apply_failure_fleet
        fleet.publish({"aaa.txt": b"a", "bbb.txt": b"b"})
        fleet.ordered(["aaa.txt", "bbb.txt"])
        exc = RuntimeError("original recovery probe")
        original_apply = cli_module._apply_incoming_file

        def apply(**kwargs):
            if kwargs["rel_path"] == fleet.prefix + "bbb.txt":
                raise exc
            return original_apply(**kwargs)

        monkeypatch.setattr(cli_module, "_apply_incoming_file", apply)

        def boom(*args, **kwargs):
            raise exc

        if recovery_failure == "sync-log":

            def log(**kwargs):
                assert fleet.memory in fleet.fsync_calls
                raise SystemExit(44)

            monkeypatch.setattr(cli_module, "write_sync_log", log)
        elif recovery_failure == "fsync":
            original_fsync = fsutil.fsync_dir

            def fsync(path):
                if Path(path) == fleet.memory:
                    raise StorageError("durability probe")
                return original_fsync(path)

            monkeypatch.setattr(fsutil, "fsync_dir", fsync)
        elif recovery_failure == "fsync-close":
            import errno

            original_fsync = fsutil.fsync_dir
            original_close = os.close

            def close(fd):
                original_close(fd)
                raise OSError(errno.EIO, "directory close probe")

            def fsync(path):
                if Path(path) == fleet.memory:
                    # Exercise the real fsync_dir finally block, whose close
                    # can raise a raw OSError instead of a StorageError.
                    with monkeypatch.context() as patch:
                        patch.setattr(fsutil.os, "close", close)
                        return original_fsync(path)
                return original_fsync(path)

            monkeypatch.setattr(fsutil, "fsync_dir", fsync)
        elif recovery_failure == "summary":

            def summary(*args, **kwargs):
                raise SystemExit(45)

            monkeypatch.setattr(cli_module, "_print_pull_summary", summary)
        elif recovery_failure == "cleanup":
            monkeypatch.setattr(cli_module, "_apply_incoming_file", original_apply)
            monkeypatch.setattr(cli_module, "_cleanup_conflict_copies", boom)
        elif recovery_failure == "bookkeeping":
            monkeypatch.setattr(cli_module, "_apply_incoming_file", original_apply)
            original_log = cli_module.write_sync_log

            def log(**kwargs):
                original_log(**kwargs)
                raise exc

            monkeypatch.setattr(cli_module, "write_sync_log", log)
        elif recovery_failure == "before-source":
            original_print = cli_module.console.print

            def output(*args, **kwargs):
                if args and "Pulling from" in str(args[0]):
                    raise exc
                return original_print(*args, **kwargs)

            # For quiet mode, raise from the first device access inside try.
            if command == "autopull":

                class BadDevice(dict):
                    def __getitem__(self, key):
                        raise exc

                original_select = cli_module._select_devices

                def select(*args):
                    all_devices, _ = original_select(*args)
                    return all_devices, [BadDevice()]

                monkeypatch.setattr(cli_module, "_select_devices", select)
            else:
                monkeypatch.setattr(cli_module.console, "print", output)
        original_core = cli_module._pull_core
        propagated = []

        def core(*args, **kwargs):
            try:
                return original_core(*args, **kwargs)
            except BaseException as error:
                propagated.append(error)
                raise

        monkeypatch.setattr(cli_module, "_pull_core", core)
        result = runner.invoke(app, [command, *(["--verbose"] if command == "pull" else [])])
        assert propagated == [exc]
        if command == "pull":
            assert result.exit_code == 1, result.output
        else:
            assert result.exit_code == 0, result.output
            crumb = cli_module._read_autorun_breadcrumbs()["pull"]
            assert (crumb["outcome"], crumb["detail"]) == ("failed", "RuntimeError")
        records = fleet.records()
        expected = (
            0
            if recovery_failure == "before-source"
            else 2
            if recovery_failure in ("cleanup", "bookkeeping")
            else 1
        )
        assert len(records) == expected
        assert len({(r["rel_path"], r["action"]) for r in records}) == expected
        if recovery_failure not in ("before-source", "sync-log"):
            assert "aaa.txt" in fleet.log.read_text()
        if recovery_failure not in ("before-source", "fsync"):
            assert fleet.memory in fleet.fsync_calls
        if recovery_failure in ("fsync", "fsync-close"):
            assert "durability fsync failed" in result.output
        assert "Pull: " not in result.stdout

    @pytest.mark.parametrize("command", [["pull", "--dry-run"], ["diff"]])
    def test_preview_with_loaded_int_mtime_never_applies(
        self, apply_failure_fleet, monkeypatch, command
    ):
        fleet = apply_failure_fleet
        fleet.publish(
            {"notes.md": b"peer"},
            metadata={"notes.md": {"sha256": hashlib.sha256(b"peer").hexdigest(), "mtime": 1234}},
        )
        (fleet.memory / "notes.md").write_bytes(b"local")

        def forbidden(*args, **kwargs):
            pytest.fail("preview reached the apply boundary")

        monkeypatch.setattr(cli_module, "_download_and_apply", forbidden)
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        assert (fleet.memory / "notes.md").read_bytes() == b"local"
        assert fleet.records() == []


@pytest.mark.parametrize("command", ["pull", "autopull"])
def test_53a_normal_summary_systemexit_propagates(apply_failure_fleet, monkeypatch, command):
    fleet = apply_failure_fleet
    fleet.publish({"aaa.txt": b"a"})

    def summary(*args, **kwargs):
        raise SystemExit(45)

    monkeypatch.setattr(cli_module, "_print_pull_summary", summary)
    result = runner.invoke(app, [command])
    assert result.exit_code == (1 if command == "pull" else 45)
    assert isinstance(result.exception, SystemExit)
    assert [r["action"] for r in fleet.records()] == ["written"]
    assert fleet.memory in fleet.fsync_calls


# One permanent hook, armed only around a preview invocation. Record instead
# of raising: setup catches OSError/Exception, so a raising guard can lie.
class _PreviewAuditWindow:
    """Only the invoking thread and threads started in this window belong to it."""

    def __init__(self):
        self.guard = threading.RLock()
        self.active = None
        self.threads = set()

    def set(self, value):
        with self.guard:
            assert self.active is None
            previous = sys.dont_write_bytecode, threading.Thread.start
            sys.dont_write_bytecode = True
            self.active = value
            self.threads = {threading.current_thread()}

            def start(thread, *args, **kwargs):
                with self.guard:
                    self.threads.add(thread)
                return previous[1](thread, *args, **kwargs)

            threading.Thread.start = start
            return previous

    def reset(self, token):
        with self.guard:
            self.active = None
            self.threads.clear()
            sys.dont_write_bytecode, threading.Thread.start = token

    def get(self):
        with self.guard:
            return self.active if threading.current_thread() in self.threads else None


_PREVIEW_AUDIT = _PreviewAuditWindow()


def _record_preview_mutation(event, args):
    active = _PREVIEW_AUDIT.get()
    if active is None:
        return
    records, lock_path, *declared = active
    exemptions = ([lock_path] if lock_path is not None else []) + list(declared)
    if event == "sqlite3.connect":
        if "mode=ro" not in str(args[0]):
            records.append((event, repr(args)))
        return
    if event in {"socket.connect", "urllib.Request", "subprocess.Popen", "os.posix_spawn"}:
        records.append((event, repr(args)))
        return
    paths = []
    if event == "open":
        path, mode, flags = args
        writing = bool(mode and any(c in mode for c in "wax+")) or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        if not writing:
            return
        paths = [path]
    elif event == "os.mkdir":
        if Path(args[0]).exists():
            return
        paths = [args[0]]
    elif event in {"os.rename", "os.replace", "os.link", "os.symlink"}:
        paths = list(args[:2])
    elif event in {"os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
        paths = [args[0]]
    else:
        return
    for value in paths:
        if isinstance(value, int):
            # Descriptor numbers are reusable; only the declared inode is exempt.
            fd_stat = os.fstat(value)
            if any(
                path.exists()
                and (fd_stat.st_dev, fd_stat.st_ino) == (path.stat().st_dev, path.stat().st_ino)
                for path in exemptions
            ):
                continue
        if isinstance(value, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(value)).absolute()
            if path in exemptions or (
                event == "os.mkdir" and any(path == p.parent for p in exemptions)
            ):
                continue
        records.append((event, repr(args)))
        break


sys.addaudithook(_record_preview_mutation)


def _preview_tree(root, *, pin=False, exemptions=()):
    """Bytes + mode + mtime for the whole fixture tree, including directories."""
    paths = [root, *root.rglob("*")]
    if pin:
        for path in paths:
            if not path.is_symlink():
                os.utime(path, ns=(1_600_000_000_000_000_000,) * 2)
    snapshot = {}
    for path in paths:
        rel = path.relative_to(root)
        if rel == Path("test.lock") or path in exemptions:
            continue
        stat = path.lstat()
        try:
            content = (
                ("link", os.readlink(path))
                if path.is_symlink()
                else ("file", path.read_bytes())
                if path.is_file()
                else ("dir",)
            )
        except PermissionError:
            content = ("unreadable",)
        # Creating the allowed lock can change its parent's mtime.
        allowed_parent = path == root or any(path == p.parent for p in exemptions)
        snapshot[str(rel)] = (content, stat.st_mode, None if allowed_parent else stat.st_mtime_ns)
    return snapshot


@pytest.fixture
def push_preview56(tmp_path, monkeypatch):
    from mind_meld import __version__, pullhistory, upgrade
    from tests.conftest import _make_config, _populate_claude

    assert __version__ != upgrade.DEV_BUILD_SENTINEL
    claude = tmp_path / "claude"
    _populate_claude(claude)
    backend = LocalBackend(tmp_path / "storage")
    fetch = bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
    register_device(backend, "dev-a", "Mac A")
    config_path, cfg = _make_config(tmp_path, backend.root, claude)
    monkeypatch.setenv("HOME", str(tmp_path))
    events_root = tmp_path / ".local" / "share" / "mind-meld"
    events_root.mkdir(parents=True)
    cfg["sync"]["sources"].append(
        {
            "name": "mm-events",
            "path": str(events_root),
            "type": "generic",
            "include_dirs": ["events"],
        }
    )
    cfg["crypto"]["root_salt_fp"] = crypto_module.root_salt_fingerprint(fetch.root_salt)
    save_config(cfg, config_path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    monkeypatch.setattr(config_module, "_BOOTSTRAP_WARNED_PATHS", set())
    from mind_meld import seen_sources

    monkeypatch.setattr(seen_sources, "SEEN_DIR", tmp_path / "seen-state")
    keyring_writes = []
    monkeypatch.setattr("keyring.set_password", lambda *a, **kw: keyring_writes.append(a))
    monkeypatch.setattr("keyring.delete_password", lambda *a, **kw: keyring_writes.append(a))
    fetches = []

    def fetch_tags():
        fetches.append(True)
        return ["v99.0.0"]

    monkeypatch.setattr(upgrade, "_fetch_tags", fetch_tags)
    # Keep the real upgrade hooks active, including their non-dev branches.
    cache = upgrade._empty_cache()
    cache.update(
        last_seen_self_version=__version__,
        latest_version=__version__,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    upgrade.CACHE_PATH.write_text(json.dumps(cache))

    def invoke():
        upgrade._reset_for_tests()
        config_module._BOOTSTRAP_WARNED_PATHS.clear()
        before = _preview_tree(tmp_path, pin=True)
        records = []
        token = _PREVIEW_AUDIT.set((records, (tmp_path / "test.lock").absolute()))
        try:
            result = runner.invoke(app, ["push", "--dry-run"])
        finally:
            _PREVIEW_AUDIT.reset(token)
        assert records == [], records
        assert keyring_writes == []
        assert fetches == []
        assert _preview_tree(tmp_path) == before
        return result

    return {
        "invoke": invoke,
        "config": cfg,
        "config_path": config_path,
        "backend": backend,
        "events": events_root,
        "claude": claude,
        "upgrade": upgrade,
        "cache": cache,
        "history": pullhistory.read_records,
        "history_baseline": [],
        "fetches": fetches,
        "keyring_writes": keyring_writes,
    }


def _preview_text(result):
    return " ".join(result.output.split())


def _assert_preview_complete(result):
    assert result.exit_code == 0, result.output
    text = _preview_text(result)
    assert "Dry run complete. Nothing was changed except the local lock file." in text
    assert text.endswith(
        "Not previewed: host-usage capture, the mm-events activity row a real push "
        "appends, post-push GC of orphaned blobs, and upload re-reads."
    )


def _assert_preview_refused(result, exit_code=1):
    assert result.exit_code == exit_code, result.output
    assert "Dry run complete." not in result.output
    assert _preview_text(result).endswith(
        "Nothing was changed except the local lock file (dry run)."
    )


def _seed_preview_prior(env, *, event_files=()):
    cfg = config_module.load_config(env["config_path"])
    crypto_module.set_crypto_session(
        crypto_module.fetch_crypto_init(env["backend"]).root_salt, MEMORY_KB
    )
    sources = config_module.get_sources(cfg, bootstrap=False)
    prior = cli_module.build_manifest_v2("dev-a", "Mac A", sources)
    for rel in event_files:
        prior["sources"]["mm-events"]["files"][rel] = {
            "sha256": hashlib.sha256(b"prior event").hexdigest(),
            "size": 11,
            "mtime": "2020-09-13T12:26:40+00:00",
        }
    from mind_meld.storage.keys import manifest_key

    env["backend"].put(
        manifest_key("dev-a"), encrypt(serialize_manifest(prior), PASSPHRASE, MEMORY_KB)
    )
    return prior


# Every new command must make an explicit intent decision here. Exemptions
# name owned paths and conditions, never broad directories or fd numbers.
COMMAND_INTENTS62 = {
    name: (
        "mutating",
        "lock"
        if name in {"push", "pull", "gc", "recapture"}
        else "none"
        if name == "migrate-config"
        else None,
    )
    for name in (
        "init",
        "push",
        "pull",
        "gc",
        "disable-source",
        "enable-source",
        "reconfigure-sources",
        "migrate-config",
        "install-skills",
        "recapture",
        "refresh-identity",
        "recover",
        "resolve",
        "autopull",
        "autopush",
        "update",
        "capture-cursor-usage",
        "cursor-agent",
    )
} | {
    name: ("inspection", None)
    for name in ("status", "diag", "devices", "diff", "sources", "log", "conflicts", "retro-fleet")
}


def _check_command_intents62(application, prefix=""):
    found = set()
    for command in application.registered_commands:
        name = prefix + (command.name or command.callback.__name__.replace("_", "-"))
        message = (
            f"{name}: update COMMAND_INTENTS62 with an intent and preview lock allowance "
            "(lock/none), or a reasoned exemption; see docs/invariants/sync.md"
        )
        assert name in COMMAND_INTENTS62, message
        params = inspect.signature(command.callback).parameters
        preview = "dry_run" in params or any(
            "--dry-run" in (getattr(p.default, "param_decls", ()) or ()) for p in params.values()
        )
        assert preview == (COMMAND_INTENTS62[name][1] is not None), message
        found.add(name)
    for group in application.registered_groups:
        found |= _check_command_intents62(group.typer_instance, prefix + group.name + " ")
    return found


def test_command_intents62():
    assert _check_command_intents62(app) == set(COMMAND_INTENTS62)
    synthetic = typer.Typer()
    synthetic.command()(lambda dry_run=False: None)
    with pytest.raises(AssertionError, match="COMMAND_INTENTS62.*docs/invariants/sync.md"):
        _check_command_intents62(synthetic)


def _check_read_only_policy62(source):
    class Policy(ast.NodeVisitor):
        owner = None

        def visit_FunctionDef(self, node):
            prior, self.owner = self.owner, node.name
            self.generic_visit(node)
            self.owner = prior

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id in {
                "_get_config",
                "_maybe_prompt_migration",
            }:
                assert any(k.arg == "read_only" for k in node.keywords), (
                    f"{self.owner}:{node.lineno}: {node.func.id} requires read_only"
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_transition_hook"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "upgrade"
            ):
                assert self.owner in {"_get_config", "_auto_command_setup", "init"}, self.owner
            self.generic_visit(node)

    Policy().visit(ast.parse(source))


def test_read_only_policy62():
    _check_read_only_policy62(Path(cli_module.__file__).read_text())
    for function in (cli_module._get_config, cli_module._maybe_prompt_migration):
        parameter = inspect.signature(function).parameters["read_only"]
        assert parameter.kind == inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
    for source in (
        "def wrong(): _get_config()",
        "def wrong(): _maybe_prompt_migration({})",
        "def wrong(): upgrade.run_transition_hook({})",
    ):
        with pytest.raises(AssertionError):
            _check_read_only_policy62(source)


@pytest.mark.parametrize("mode", ["worker", "import", "swallowed", "subprocess", "fd"])
def test_preview_audit62(tmp_path, mode):
    from concurrent.futures import ThreadPoolExecutor
    from importlib.util import module_from_spec, spec_from_file_location

    source = tmp_path / "fresh_preview_module.py"
    source.write_text("answer = 42\n")
    records = []
    token = _PREVIEW_AUDIT.set((records, None))
    try:
        if mode == "worker":
            with ThreadPoolExecutor(1) as pool:
                pool.submit((tmp_path / "worker").write_text, "write").result()
        elif mode == "import":
            spec = spec_from_file_location("fresh_preview_module", source)
            spec.loader.exec_module(module_from_spec(spec))
        elif mode == "subprocess":
            subprocess.run([sys.executable, "-c", "pass"], check=True)
        elif mode == "fd":
            with source.open("r+") as file:
                records.clear()
                os.ftruncate(file.fileno(), 0)
        else:
            try:
                (tmp_path / "test.lock").write_text("not exempt in lock-free mode")
                raise OSError("swallowed")
            except OSError:
                pass
    finally:
        _PREVIEW_AUDIT.reset(token)
    assert bool(records) == (mode != "import"), records
    assert not (tmp_path / "__pycache__").exists()


def test_preview_audit62_ignores_prior_worker(tmp_path):
    release = threading.Event()
    records = []
    token = _PREVIEW_AUDIT.set(([], None))
    thread = threading.Thread(target=lambda: (release.wait(), (tmp_path / "late").touch()))
    thread.start()
    _PREVIEW_AUDIT.reset(token)
    token = _PREVIEW_AUDIT.set((records, None))
    try:
        release.set()
        thread.join()
    finally:
        _PREVIEW_AUDIT.reset(token)
    assert records == []


def _publish_peer62(env, device, files, *, tombstones=None):
    from mind_meld.devices import update_last_seen
    from mind_meld.storage.keys import blob_key, manifest_key

    register_device(env["backend"], device, device)
    update_last_seen(env["backend"], device)
    manifest_sources = {}
    for (source, path), (data, mtime) in files.items():
        digest = hashlib.sha256(data).hexdigest()
        env["backend"].put(blob_key(device, digest), encrypt(data, PASSPHRASE, MEMORY_KB))
        manifest_sources.setdefault(source, {"files": {}})["files"][path] = {
            "sha256": digest,
            "size": len(data),
            "mtime": mtime,
        }
    peer = {
        "version": 2,
        "device_id": device,
        "device_name": device,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": manifest_sources,
        "tombstones": tombstones or {},
    }
    env["backend"].put(
        manifest_key(device), encrypt(serialize_manifest(peer), PASSPHRASE, MEMORY_KB)
    )
    return peer


@pytest.fixture
def preview62(push_preview56, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mind_meld import identity, pullhistory, seen_sources, token_usage
    from mind_meld.skills.retro_fleet import aggregator
    from mind_meld.storage.keys import blob_key
    from tests.test_host_skill_discovery import _ok_payload

    env = push_preview56
    pullhistory.append(
        verb="pull",
        device="history-seed",
        source="gstack",
        rel_path="history-fixture.md",
        action="written",
    )
    env["history_baseline"] = env["history"]()
    memory = env["claude"] / "projects/-Users-kb-myapp/memory"
    env["legacy"] = memory / "role.sync-conflict-20260101-120000-abcd1234.md"
    env["legacy"].write_text("old peer bytes")
    (memory / "role.sync-conflict-20200101-120000-v1-deadbeef.md").write_bytes(
        (memory / "role.md").read_bytes()
    )
    env["config"]["sync"]["sources"][0]["exclude_patterns"] = ["*/secret.md"]
    groot = tmp_path / "gstack"
    groot.mkdir()
    (groot / "config.yaml").write_text("local config")
    env["config"]["sync"]["sources"].append(
        {
            "name": "gstack",
            "path": str(groot),
            "type": "generic",
            "include_dirs": ["."],
            "exclude_patterns": [],
        }
    )
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "seed",
        ],
        check=True,
    )
    env["config"]["retro"] = {"repo_roots": [str(repo)]}
    save_config(env["config"], env["config_path"])
    _seed_preview_prior(env)
    stamp = datetime.now(timezone.utc).isoformat()
    prefix = "projects/-Users-kb-myapp/memory/"
    _publish_peer62(
        env,
        "dev-b",
        {
            ("claude", prefix + name): (data, stamp)
            for name, data in (
                ("new.md", b"new"),
                ("role.md", b"remote role"),
                ("secret.md", b"excluded"),
                ("gone.md", b"tombstoned"),
            )
        },
        tombstones={"claude:" + prefix + "gone.md": {"deleted_at": stamp}},
    )
    env["cache"].update(last_seen_self_version="0.0.1", checked_at="2000-01-01T00:00:00+00:00")
    env["upgrade"].CACHE_PATH.write_text(json.dumps(env["cache"]))
    breadcrumb = cli_module._migration_state_path()
    breadcrumb.parent.mkdir(parents=True, exist_ok=True)
    breadcrumb.write_text('{"missing": ["gstack"]}')
    (env["events"] / "events").mkdir()
    (env["events"] / "events/dev-a-2020-01-01.jsonl").write_text("{}\n")
    env["backend"].put(blob_key("dev-a", hashlib.sha256(b"orphan").hexdigest()), b"orphan")
    partial = env["backend"].root / "data/dev-a/tmpold.tmp"
    partial.write_text("partial")
    token_usage.CACHE_PATH.write_text(
        json.dumps(
            {
                "version": token_usage.CACHE_VERSION,
                "files": {
                    str(tmp_path / "gone.jsonl"): {
                        "size": 1,
                        "mtime": 1,
                        "by_day": {"2020-01-01": {}},
                    }
                },
            }
        )
    )
    seen_sources.read([s["name"] for s in env["config"]["sync"]["sources"]])
    monkeypatch.setattr(aggregator, "get_known_devices", lambda: (2, []))
    # Record real read-only git results before arming, replay at the subprocess
    # boundary. Discovery, walk threads and command wrappers still execute.
    original_run = subprocess.run
    replay = {}

    def record(argv, **kwargs):
        result = original_run(argv, **kwargs)
        replay[tuple(argv)] = result
        return result

    monkeypatch.setattr(subprocess, "run", record)
    sources = config_module.get_sources(env["config"])
    events_tail._prepare_recapture(
        env["config"], sources, "dev-a", since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    # The walk returns when its budget expires and does not wait out a slow
    # git log. The recording pass then has no log entry, and the command the
    # test issues next fails the allowlist. Seed the two read-only calls the
    # walk is allowed to make unless it already recorded them.
    from mind_meld import gitenv

    resolved = str(repo.resolve())

    def already_recorded(argv: list[str]) -> bool:
        key = tuple(arg for arg in argv if not arg.startswith("--since="))
        return any(
            key == tuple(arg for arg in seen if not arg.startswith("--since=")) for seen in replay
        )

    log_argv = [
        "git",
        "-C",
        resolved,
        "log",
        "--since=1970-01-01T00:00:00+00:00",
        "--numstat",
        "-M",
        "-C",
        f"--format={_mm_events._GIT_LOG_FORMAT}",
    ]
    remote_argv = ["git", "-C", resolved, "config", "--get", "remote.origin.url"]
    git_kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "env": gitenv.scrubbed_git_env(),
        "timeout": 30,
    }
    if not already_recorded(log_argv):
        record(log_argv, errors="replace", **git_kwargs)
    if not already_recorded(remote_argv):
        record(remote_argv, errors="strict", **git_kwargs)

    def run(argv, **kwargs):
        key = tuple(arg for arg in argv if not arg.startswith("--since="))
        for recorded, result in replay.items():
            if key == tuple(arg for arg in recorded if not arg.startswith("--since=")):
                return result
        if argv == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(argv, 0, '{"id": 1, "login": "test"}', "")
        if argv == ["git", "config", "--global", "user.email"] or (
            argv[:2] == ["git", "-C"] and argv[3:] == ["config", "user.email"]
        ):
            return subprocess.CompletedProcess(argv, 0, "test@example.com\n", "")
        pytest.fail(f"Unexpected subprocess: {argv}")

    monkeypatch.setattr(subprocess, "run", run)

    grok_output = tmp_path / "grok-inspect.json"
    grok_output.write_text(json.dumps(_ok_payload()))
    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: "grok" if name == "grok" else original_which(name)
    )

    def popen(argv, **kwargs):
        assert argv[1:] == ["inspect", "--json"]
        return SimpleNamespace(stdout=grok_output.open("rb"), returncode=0, wait=lambda **kw: 0)

    monkeypatch.setattr(subprocess, "Popen", popen)

    def invoke(argv, *, exemptions=(), check=True):
        env["upgrade"]._reset_for_tests()
        lock = (
            tmp_path / "test.lock"
            if "--dry-run" in argv and COMMAND_INTENTS62[argv[0]][1] == "lock"
            else None
        )
        tree_exemptions = (*exemptions, *(p.parent for p in exemptions if not p.parent.exists()))
        before = _preview_tree(tmp_path, pin=True, exemptions=tree_exemptions)
        records = []
        token = _PREVIEW_AUDIT.set((records, lock, *exemptions))
        try:
            result = runner.invoke(app, argv)
        finally:
            _PREVIEW_AUDIT.reset(token)
        if check:
            assert records == [], (result.output, records)
            assert _preview_tree(tmp_path, exemptions=tree_exemptions) == before
            assert env["fetches"] == []
            assert env["keyring_writes"] == []
            if lock is None:
                assert not (tmp_path / "test.lock").exists()
        return result

    env["audit"] = invoke
    env["identity"] = identity.CACHE_PATH
    return env


INSPECTION_CASES62 = [
    (name,) for name, (intent, _) in COMMAND_INTENTS62.items() if intent == "inspection"
]
PREVIEW_CASES62 = [
    (name, "--dry-run", *(["1d"] if name == "recapture" else []))
    for name, (_, lock) in COMMAND_INTENTS62.items()
    if lock is not None
]


@pytest.mark.parametrize("argv", INSPECTION_CASES62 + PREVIEW_CASES62)
def test_write_free_contract62(preview62, argv, tmp_path, monkeypatch):
    if argv[0] in {"push", "status", "diag"}:
        root = tmp_path / "codex-consented"
        root.mkdir()
        preview62["config"]["sync"]["sources"].append(
            {"name": "codex", "path": str(root), "type": "generic"}
        )
        save_config(preview62["config"], preview62["config_path"])
        preview62["events"].rename(tmp_path / "prior-events")
        monkeypatch.setattr(
            events_tail, "_capture_host_snapshot", lambda *a, **kw: pytest.fail("preview captured")
        )
        monkeypatch.setattr(
            _mm_host_usage, "warm_host_cache_inline", lambda *a, **kw: pytest.fail("preview warmed")
        )
        monkeypatch.setattr(
            cli_module.upgrade, "record_nudge", lambda *a, **kw: pytest.fail("preview nudged")
        )
    exemptions = (preview62["identity"],) if argv[0] == "retro-fleet" else ()
    result = preview62["audit"](list(argv), exemptions=exemptions)
    assert result.exit_code == 0, (result.output, result.exception)


@pytest.mark.parametrize("argv", INSPECTION_CASES62 + PREVIEW_CASES62)
def test_transition_deferred62(preview62, argv):
    env = preview62
    exemptions = (env["identity"],) if argv[0] == "retro-fleet" else ()
    assert env["audit"](list(argv), exemptions=exemptions).exit_code == 0
    assert env["history"]() == env["history_baseline"]
    for _ in range(2):
        env["upgrade"]._reset_for_tests()
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, (result.output, result.exception)
    transitions = [row for row in env["history"]() if row["verb"] == "self-upgrade"]
    assert len(transitions) == 1
    assert transitions[0]["old_version"] == "0.0.1"


@pytest.mark.parametrize("state", ["missing", "corrupt", "empty", "malformed"])
def test_status_seed_then_read_only62(preview62, state):
    from mind_meld import seen_sources

    path = seen_sources.seen_path()
    if state == "missing":
        path.unlink()
        path.parent.rmdir()
    else:
        path.write_bytes({"corrupt": b"\xff", "empty": b"", "malformed": b"{}"}[state])
    result = preview62["audit"](["status"], exemptions=(path,))
    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text())
    result = preview62["audit"](["status"])
    assert result.exit_code == 0, result.output


def test_seen_recovery_preserves_interleaved_ack62(tmp_path, monkeypatch):
    from mind_meld import seen_sources

    monkeypatch.setattr(seen_sources, "SEEN_DIR", tmp_path)
    seen_sources.seen_path().write_text("corrupt")
    original = os.open
    interleaved = []

    def opening(path, flags, *args, **kwargs):
        if flags & os.O_RDWR and not interleaved:
            interleaved.append(True)
            seen_sources.acknowledge(["codex"], initial=["claude"])
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    assert seen_sources.read(["gstack"]) == {"claude", "codex"}


@pytest.mark.parametrize("state", ["missing", "fresh", "stale", "no-filter"])
def test_retro_exemptions62(preview62, state):
    from mind_meld import identity

    path = preview62["identity"]
    if state != "missing":
        path.write_text(
            json.dumps(
                {
                    "version": identity.CACHE_VERSION,
                    "emails": ["test@example.com"],
                    "refreshed_at": datetime.now(timezone.utc).isoformat()
                    if state == "fresh"
                    else "2000-01-01T00:00:00+00:00",
                }
            )
        )
    argv = ["retro-fleet"] + (["--no-author-filter"] if state == "no-filter" else [])
    result = preview62["audit"](argv, exemptions=() if state == "no-filter" else (path,))
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "argv",
    [("diag", "--json"), ("devices", "--format", "json"), ("gc", "--dry-run", "--conflicts")],
)
def test_inspection_variants62(preview62, argv):
    result = preview62["audit"](list(argv))
    assert result.exit_code == 0, result.output
    if argv == ("diag", "--json"):
        assert json.loads(result.stdout)["host_skill_discovery"]["status"] == "ok"


def test_real_pull_keeps_migration_and_history62(preview62):
    from mind_meld import resolveflow

    result = runner.invoke(app, ["pull"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert not preview62["legacy"].exists()
    assert (
        preview62["legacy"]
        .with_name(preview62["legacy"].name.replace("sync-conflict-", "sync-conflict-v0-"))
        .exists()
    )
    assert resolveflow._inversion_marker_path().exists()
    rows = preview62["history"]()
    assert any(row.get("action") == "excluded" for row in rows)


@pytest.mark.parametrize("command", ["pull", "gc", "recapture"])
def test_preview_storage_refusal62(preview62, monkeypatch, command):
    from mind_meld.errors import StorageError

    def fail(*args, **kwargs):
        raise StorageError("injected storage refusal")

    if command == "pull":
        monkeypatch.setattr(cli_module, "list_devices_with_drops", fail)
    elif command == "recapture":
        monkeypatch.setattr(events_tail, "_prepare_recapture", fail)
    else:
        original = LocalBackend.list_keys

        def list_keys(backend, prefix=""):
            if prefix == storage_keys.data_prefix(preview62["config"]["device"]["id"]):
                return fail()
            return original(backend, prefix)

        monkeypatch.setattr(LocalBackend, "list_keys", list_keys)
    result = preview62["audit"]([command, "--dry-run"])
    _assert_preview_refused(result)


@pytest.mark.parametrize("directory_first", [False, True])
def test_pull_two_peer_parity62(preview62, tmp_path, monkeypatch, directory_first):
    """(a)-(j): encrypted peer bytes, twin local trees, outcomes keyed by peer."""
    from mind_meld import pullplan

    env = preview62
    preview = tmp_path / "forecast"
    preview.mkdir()
    for name, data in {
        "merge.jsonl": b'{"local":1}\n',
        "older.md": b"local newer",
        "unreadable.md": b"private",
        "ancestor": b"file",
        "canonical-match.md": b"canonical",
        "remote-match.md": b"canonical",
    }.items():
        (preview / name).write_bytes(data)
    (preview / "link").symlink_to(env["claude"], target_is_directory=True)
    applied = tmp_path / "applied"
    shutil.copytree(preview, applied, symlinks=True)
    for base in (preview, applied):
        (base / "unreadable.md").chmod(0)
    cfg = env["config"]
    cfg["sync"]["sources"] = [
        {
            "name": "payload",
            "type": "generic",
            "path": str(preview),
            "include_dirs": ["."],
            "exclude_patterns": ["excluded.md"],
        }
    ]
    save_config(cfg, env["config_path"])
    stamp, older = "2021-01-01T00:00:00+00:00", "2019-01-01T00:00:00+00:00"
    a = {
        name: (data, stamp)
        for name, data in {
            "identical.md": b"same",
            "divergent.md": b"first",
            "merge.jsonl": b'{"a":1}\n',
            "older.md": b"first newer peer",
            "tombstoned.md": b"gone",
            "excluded.md": b"excluded",
            "link/incoming.md": b"link bytes",
            "unreadable.md": b"peer private",
            "ancestor/child.md": b"impossible",
            "canonical-match.md": b"remote",
            "remote-match.md": b"remote",
            "foo/bar" if directory_first else "foo": b"first shape",
        }.items()
    }
    b = {
        name: (data, stamp)
        for name, data in {
            "identical.md": b"same",
            "divergent.md": b"second",
            "merge.jsonl": b'{"b":1}\n',
            "canonical-match.md": b"canonical",
            "remote-match.md": b"remote",
            "foo" if directory_first else "foo/bar": b"second shape",
        }.items()
    }
    b["older.md"] = (b"older peer", older)
    for device, files in (("dev-b", a), ("dev-c", b)):
        _publish_peer62(
            env,
            device,
            {("payload", name): info for name, info in files.items()},
            tombstones={
                "payload:tombstoned.md": {
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                    "device_id": device,
                }
            },
        )
    predictions, actual = [], {}
    original = cli_module._pull_one_source

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        predictions.extend(result.predictions)
        if not kwargs["dry_run"]:
            for outcome, paths in result.outcomes.items():
                actual.setdefault(outcome, set()).update(
                    (result.device_id, result.src_name, path) for path in paths
                )
        return result

    monkeypatch.setattr(cli_module, "_pull_one_source", capture)
    hashes = []
    original_hash = pullplan.hash_file

    def hash_once(path):
        hashes.append(path)
        return original_hash(path)

    monkeypatch.setattr(pullplan, "hash_file", hash_once)
    try:
        result = env["audit"](["pull", "--dry-run"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert len(hashes) == len(set(hashes))
        assert pullplan.totals(predictions) in _preview_text(result)
        assert "Pull complete." not in result.output
        expected = {}
        for prediction in predictions:
            expected.setdefault(prediction.outcome, set()).add(
                (prediction.device_id, prediction.src_name, prediction.rel_path)
            )
        cfg["sync"]["sources"][0]["path"] = str(applied)
        save_config(cfg, env["config_path"])
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 1, (result.output, result.exception)
        for predicted, real in (
            ("write", "written"),
            ("conflict", "conflicted"),
            ("skip", "skipped"),
            ("may fail", "failed"),
        ):
            assert expected.get(predicted, set()) == actual.get(real, set()), (
                predicted,
                expected,
                actual,
            )
        assert actual.get("merged", set()) <= expected["merge"]
        assert len(expected["merge"]) == 2
        assert len(expected["may fail"]) == 3
        assert len(expected["skip"]) == 2
        assert ("dev-c", "payload", "canonical-match.md") not in actual["conflicted"]
        assert ("dev-c", "payload", "remote-match.md") in actual["conflicted"]
        assert not any(
            key[2] in {"tombstoned.md", "excluded.md"}
            for paths in [*expected.values(), *actual.values()]
            for key in paths
        )
    finally:
        for base in (preview, applied):
            (base / "unreadable.md").chmod(0o600)


@pytest.mark.parametrize("dry_run", [False, True])
def test_fail_mode_local_failure_only62(preview62, dry_run):
    env = preview62
    prefix = "projects/-Users-kb-myapp/memory/"
    blocked = env["claude"] / prefix / "blocked.md"
    blocked.write_bytes(b"local")
    blocked.chmod(0)
    try:
        _publish_peer62(env, "dev-b", {("claude", prefix + "blocked.md"): (b"peer bytes", None)})
        argv = ["pull", "--conflict-mode", "fail"] + (["--dry-run"] if dry_run else [])
        result = env["audit"](argv) if dry_run else runner.invoke(app, argv)
        assert result.exit_code == 3, result.output
        text = _preview_text(result)
        assert "may fail (local file unreadable)" in text
        assert "! conflict" not in text
        blocked.chmod(0o600)
        assert blocked.read_bytes() == b"local"
        if dry_run:
            _assert_preview_refused(result, 3)
    finally:
        blocked.chmod(0o600)


@pytest.mark.parametrize("dry_run", [False, True])
def test_fail_mode_mergeable_two_peers62(preview62, dry_run):
    env = preview62
    path = "projects/-Users-kb-myapp/memory/merge.jsonl"
    for device in ("dev-b", "dev-c"):
        _publish_peer62(
            env, device, {("claude", path): (json.dumps({device: 1}).encode() + b"\n", None)}
        )
    argv = ["pull", "--conflict-mode", "fail"] + (["--dry-run"] if dry_run else [])
    result = env["audit"](argv) if dry_run else runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert "Pull refused" not in result.output


def test_fail_preview_preserves_warnings62(preview62):
    from mind_meld.storage.keys import manifest_key

    env = preview62
    _publish_peer62(env, "dev-c", {("unknown", "file"): (b"data", None)})
    register_device(env["backend"], "dev-d", "Corrupt peer")
    env["backend"].put(manifest_key("dev-d"), b"corrupt")
    result = env["audit"](["pull", "--dry-run", "--conflict-mode", "fail"])
    _assert_preview_refused(result, 3)
    assert "corrupt" in result.output
    assert "skipping unknown source" in result.output
    assert result.output.index("corrupt") < result.output.index("Pull refused")
    assert "Nothing was changed except the local lock file (dry run)." in _preview_text(result)
    assert "Nothing was changed" in result.stderr


def test_pull_planner_future_clamp62(tmp_path):
    from mind_meld import pullplan

    planner = pullplan.PullPlanner()
    first = planner.predict(
        "a", "A", "s", "x", {"sha256": "a", "mtime": "2099-01-01T00:00:00+00:00"}, tmp_path
    )
    second = planner.predict(
        "b",
        "B",
        "s",
        "x",
        {"sha256": "b", "mtime": (planner.now + timedelta(seconds=61)).isoformat()},
        tmp_path,
    )
    assert (first.outcome, second.outcome) == ("write", "conflict")


@pytest.mark.parametrize("command", ["push", "pull", "recapture"])
def test_preview_tty_notice62(preview62, monkeypatch, command):
    original = cli_module._maybe_prompt_migration
    confirms = []

    def prompt(config, **kwargs):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        return original(config, **kwargs)

    monkeypatch.setattr(cli_module, "_maybe_prompt_migration", prompt)
    monkeypatch.setattr(typer, "confirm", lambda *a, **kw: confirms.append(True) or True)
    result = preview62["audit"]([command, "--dry-run", *(["1d"] if command == "recapture" else [])])
    assert result.exit_code == 0, result.output
    assert confirms == []
    assert (
        "Without --dry-run, this command first offers to run mm migrate-config (default: yes)."
        in _preview_text(result)
    )
    assert preview62["legacy"].exists()
    assert not cli_module.resolveflow._inversion_marker_path().exists()
    assert preview62["history"]() == preview62["history_baseline"]


@pytest.mark.parametrize("command", ["pull", "gc", "recapture"])
def test_crypto_refusal_suffix62(preview62, monkeypatch, command):
    from mind_meld.errors import CryptoError

    def refuse(*a, **kw):
        raise CryptoError("crypto refused")

    monkeypatch.setattr(cli_module, "_init_crypto_session", refuse)
    _assert_preview_refused(preview62["audit"]([command, "--dry-run"]))


@pytest.mark.parametrize(
    "case", ["fleet", "device", "gc-corrupt", "custom-root", "zero-repos", "held-lock"]
)
def test_refusal_paths62(preview62, monkeypatch, case):
    from mind_meld.errors import LockError
    from mind_meld.storage.keys import device_key, manifest_key

    env = preview62
    argv = ["pull", "--dry-run"]
    if case == "fleet":
        key = device_key("dev-b")
        data = json.loads(env["backend"].get(key))
        data["last_seen_version"] = "0.8.0"
        env["backend"].put(key, json.dumps(data).encode())
    elif case == "device":
        argv += ["--from", "missing"]
    elif case == "gc-corrupt":
        argv = ["gc", "--dry-run"]
        env["backend"].put(manifest_key(env["config"]["device"]["id"]), b"corrupt")
    elif case == "custom-root":
        argv = ["recapture", "--dry-run"]
        env["config"]["sync"]["sources"][1]["path"] = str(env["events"].parent / "missing-custom")
        save_config(env["config"], env["config_path"])
    elif case == "zero-repos":
        argv = ["recapture", "--dry-run", "1d"]
        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda *a, **kw: _mm_events.GitRootDiscovery((), (), False),
        )
    else:

        def held():
            raise LockError("held lock")

        monkeypatch.setattr(cli_module, "acquire_lock", held)
    result = env["audit"](argv)
    if case == "held-lock":
        assert result.exit_code == 1
        assert "Nothing was changed" not in result.output
    else:
        _assert_preview_refused(result)
    if case == "zero-repos":
        assert "then preview again: mm recapture --dry-run 1d" in _preview_text(result)


@pytest.mark.parametrize("disabled,configured", [(True, True), (False, False), (True, False)])
@pytest.mark.parametrize("dry_run", [False, True])
def test_recapture_source_advice62(preview62, disabled, configured, dry_run):
    env = preview62
    if disabled:
        env["config"]["sync"]["disabled_sources"] = ["mm-events"]
    if not configured:
        env["config"]["sync"]["sources"] = [
            s for s in env["config"]["sync"]["sources"] if s["name"] != "mm-events"
        ]
    save_config(env["config"], env["config_path"])
    argv = ["recapture", "1d"] + (["--dry-run"] if dry_run else [])
    result = env["audit"](argv) if dry_run else runner.invoke(app, argv)
    assert result.exit_code == 1, result.output
    assert (
        f"but it is {'disabled' if disabled else 'not configured'} on this Mac."
        in _preview_text(result)
    )
    if dry_run:
        _assert_preview_refused(result)
        assert "Fix (changes config): mm enable-source mm-events" in _preview_text(result)
        assert "Then preview again: mm recapture --dry-run 1d" in _preview_text(result)
    else:
        assert "Then retry: mm recapture 1d" in result.output
    if not disabled and dry_run:
        enabled = runner.invoke(app, ["enable-source", "mm-events"])
        assert enabled.exit_code == 0, enabled.output
        retried = runner.invoke(app, ["recapture", "--dry-run", "1d"])
        assert retried.exit_code == 0, retried.output


@pytest.mark.parametrize(
    "discovery,reason",
    [("complete", "git_error"), ("budget", None), ("errors", None), ("complete", "no_commits")],
)
def test_recapture_incomplete_preview62(preview62, monkeypatch, discovery, reason):
    original = events_tail._prepare_recapture

    def prepare(*a, **kw):
        from dataclasses import replace

        prepared = original(*a, **kw)
        if reason:
            prepared.git_rows[0]["skipped"] = [{"path": "[red]repo\x1b[2J", "reason": reason}]
            prepared.walk_errors = int(reason != "no_commits")
        prepared.root_discovery = replace(
            prepared.root_discovery,
            exceeded=discovery == "budget",
            errors=("probe [red]failed\x1b[2J",) if discovery == "errors" else (),
        )
        return prepared

    monkeypatch.setattr(events_tail, "_prepare_recapture", prepare)
    result = preview62["audit"](["recapture", "--dry-run", "1d"])
    assert result.exit_code == 0, result.output
    text = _preview_text(result)
    if reason == "git_error":
        assert "Preview incomplete: 1 repositories could not be walked" in text
    elif discovery == "budget":
        assert "repository discovery exceeded its budget; more repositories may exist." in text
    elif discovery == "errors":
        assert "repository discovery reported errors: probe [red]failed" in text
    else:
        assert "Preview incomplete" not in text
    assert "\x1b" not in result.output
    assert text.endswith(cli_module.RECAPTURE_NOT_PREVIEWED)


def test_pending_events_root62(preview62):
    env = preview62
    shutil.rmtree(env["events"])
    _publish_peer62(env, "dev-b", {("mm-events", "events/incoming.jsonl"): (b"{}\n", None)})
    result = env["audit"](["pull", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "mm pull will create" in result.output
    assert "(0700) for incoming mm-events files." in _preview_text(result)
    assert not env["events"].exists()


@pytest.mark.parametrize("target", ["dev-a", "dev-b"])
def test_diff_excludes62(preview62, target):
    env = preview62
    prefix = "projects/-Users-kb-myapp/memory/"
    _publish_peer62(
        env,
        target,
        {("claude", prefix + name): (b"peer", None) for name in ("secret.md", "visible.md")},
    )
    argv = ["diff"] + (["--from", target] if target == "dev-b" else [])
    result = env["audit"](argv)
    assert result.exit_code == 0, result.output
    assert "secret.md" not in result.output
    assert "visible.md" in result.output


@pytest.mark.parametrize("state", ["empty", "current", "migration"])
def test_migrate_completion62(preview62, state):
    env = preview62
    if state != "migration":
        env["config"]["sync"]["sources"] = (
            [] if state == "empty" else [env["config"]["sync"]["sources"][1]]
        )
        save_config(env["config"], env["config_path"])
    result = env["audit"](["migrate-config", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert _preview_text(result).endswith("Dry run complete. Nothing was changed.")


@pytest.mark.parametrize(
    "state", ["empty", "converged", "corrupt", "unknown", "interrupt", "prompt"]
)
def test_pull_preview_output62(preview62, monkeypatch, state):
    from mind_meld.storage.keys import manifest_key

    env = preview62
    if state == "empty":
        _publish_peer62(env, "dev-b", {})
    if state == "converged":
        path = "projects/-Users-kb-myapp/memory/role.md"
        _publish_peer62(
            env, "dev-b", {("claude", path): ((env["claude"] / path).read_bytes(), None)}
        )
    if state == "corrupt":
        env["backend"].put(manifest_key("dev-b"), b"corrupt")
    if state == "unknown":
        _publish_peer62(env, "dev-b", {("unknown", "file"): (b"peer", None)})
    if state == "interrupt":

        def interrupt(*a, **kw):
            raise KeyboardInterrupt()

        monkeypatch.setattr(cli_module, "_pull_one_source", interrupt)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: pytest.fail("preview prompted"))
    argv = ["pull", "--dry-run"] + (["--conflict-mode", "prompt"] if state == "prompt" else [])
    result = env["audit"](argv)
    text = _preview_text(result)
    assert not any(
        word in text
        for word in (
            "Pull complete.",
            "Completed in",
            "nothing to apply",
            "completed changes were kept",
        )
    )
    if state == "interrupt":
        assert result.exit_code != 0
        assert "Dry run complete." not in text
    else:
        assert result.exit_code == 0, result.output
        assert text.endswith(cli_module.PULL_NOT_PREVIEWED)
        assert ("Preview incomplete:" in text) == (state == "corrupt")
    if state in {"empty", "unknown", "converged"}:
        assert "No changes predicted." in text
        assert "Dry run for" not in text
        if state != "unknown":
            assert "source '" not in text


@pytest.mark.parametrize(
    "command,phrases",
    [
        (
            "pull",
            [
                "no file writes or renames",
                "Exits 1 if the preview stops",
                "3 if --conflict-mode fail predicts conflicts",
                "combine with --dry-run for a write-free check",
            ],
        ),
        ("gc", ["Changes nothing except the local lock file.", "Exits 1 if the preview stops."]),
        (
            "recapture",
            [
                "no rows, uploads, config writes or upgrade records.",
                "a partial scan exits 0 and says so.",
            ],
        ),
        ("migrate-config", ["no config, lock or history writes.", "Exits 0."]),
        (
            "diff",
            [
                "Compare this Mac's files with its last push",
                "Changes nothing, not even the lock file.",
                "use mm pull --dry-run.",
            ],
        ),
    ],
)
def test_preview_help62(command, phrases):
    result = runner.invoke(app, [command, "--help"], env={"COLUMNS": "240"})
    text = _preview_text(result)
    assert result.exit_code == 0
    # Rich's option-column borders can split prose; the callback's help data
    # pins wording independently of terminal width.
    callback = next(
        c.callback
        for c in app.registered_commands
        if (c.name or c.callback.__name__.replace("_", "-")) == command
    )
    help_text = (
        " ".join((inspect.getdoc(callback) or "").split())
        + " "
        + " ".join(
            getattr(param.default, "help", "") or ""
            for param in inspect.signature(callback).parameters.values()
        )
    )
    for phrase in phrases:
        assert phrase in help_text, (phrase, text)
    if command == "pull":
        assert "_predict_pull_outcome" not in text
        assert "(no writes)" not in text


@pytest.mark.parametrize("conflicts", [False, True])
def test_gc_conflict_label62(preview62, conflicts):
    result = preview62["audit"](["gc", "--dry-run"] + (["--conflicts"] if conflicts else []))
    assert result.exit_code == 0, result.output
    assert ("deletion requires --conflicts" in _preview_text(result)) == (not conflicts)
    assert _preview_text(result).endswith(cli_module.DRY_RUN_COMPLETE)


class TestPushPreviewNoMutation56A:
    def test_guard_records_swallowed_write(self, tmp_path):
        records = []
        token = _PREVIEW_AUDIT.set((records, tmp_path / "test.lock"))
        try:
            try:
                (tmp_path / "swallowed").write_text("mutation")
                raise OSError("swallowed setup failure")
            except OSError:
                pass
        finally:
            _PREVIEW_AUDIT.reset(token)
        assert records and records[0][0] == "open"
        with pytest.raises(AssertionError):
            assert records == []

    def test_guard_records_os_replace(self, tmp_path):
        records = []
        token = _PREVIEW_AUDIT.set((records, tmp_path / "test.lock"))
        try:
            src = tmp_path / "a"
            dst = tmp_path / "b"
            src.write_text("x")
            records.clear()
            os.replace(src, dst)
        finally:
            _PREVIEW_AUDIT.reset(token)
        # POSIX os.replace is rename(2); the audit event is os.rename there.
        assert records and records[0][0] in {"os.replace", "os.rename"}

    def test_steady_state(self, push_preview56):
        _assert_preview_complete(push_preview56["invoke"]())

    @pytest.mark.parametrize("cache_state", ["absent", "stale", "due"])
    def test_upgrade_cache_and_network_untouched(self, push_preview56, cache_state):
        env = push_preview56
        cache = env["cache"]
        if cache_state == "absent":
            env["upgrade"].CACHE_PATH.unlink()
        else:
            cache["latest_version"] = "99.0.0"
            if cache_state == "stale":
                cache["checked_at"] = "2000-01-01T00:00:00+00:00"
            env["upgrade"].CACHE_PATH.write_text(json.dumps(cache))
        result = env["invoke"]()
        _assert_preview_complete(result)
        assert "mm: notice:" not in result.stderr

    def test_s4b_transition_survives_preview_and_status_then_push_records_once(
        self, push_preview56
    ):
        env = push_preview56
        env["cache"]["last_seen_self_version"] = "0.0.1"
        env["upgrade"].CACHE_PATH.write_text(json.dumps(env["cache"]))
        _assert_preview_complete(env["invoke"]())
        assert env["history"]() == env["history_baseline"]
        env["upgrade"]._reset_for_tests()
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert env["history"]() == env["history_baseline"]
        for _ in range(2):
            env["upgrade"]._reset_for_tests()
            result = runner.invoke(app, ["push"])
            assert result.exit_code == 0, result.output
        rows = env["history"]()
        transitions = [row for row in rows if row["verb"] == "self-upgrade"]
        assert len(transitions) == 1
        assert transitions[0]["old_version"] == "0.0.1"

    @pytest.mark.parametrize("no_op", [False, True])
    def test_s7_crypto_copy_is_retained_and_reported_even_on_noop(self, push_preview56, no_op):
        env = push_preview56
        if no_op:
            _preview_tree(env["events"].parent, pin=True)
            _seed_preview_prior(env)
        copy = env["backend"].root / "mm-crypto-init 2"
        copy.write_bytes(env["backend"].get("mm-crypto-init"))
        result = env["invoke"]()
        _assert_preview_complete(result)
        assert "delete 1 identical conflict copies" in _preview_text(result)
        assert "autopull or autopush that verifies the passphrase" in _preview_text(result)
        if no_op:
            assert "Nothing to push" in result.output

    def test_no_sources_still_reports_pending_and_trailer(self, push_preview56):
        env = push_preview56
        env["config"]["sync"]["sources"] = []
        save_config(env["config"], env["config_path"])
        (env["backend"].root / "mm-crypto-init 2").write_bytes(env["backend"].get("mm-crypto-init"))
        result = env["invoke"]()
        _assert_preview_complete(result)
        assert "no sync sources found" in result.output
        assert "delete 1 identical conflict copies" in _preview_text(result)

    @pytest.mark.parametrize("tty", [False, True])
    def test_migration_notice_without_prompt(self, push_preview56, monkeypatch, tty):
        env = push_preview56
        root = env["events"].parent / "gstack"
        root.mkdir()
        (root / "config.yaml").write_text("local config")
        env["config"]["sync"]["sources"].append(
            {
                "name": "gstack",
                "path": str(root),
                "type": "generic",
                "include_dirs": ["."],
                "exclude_patterns": [],
            }
        )
        save_config(env["config"], env["config_path"])
        original = cli_module._maybe_prompt_migration

        def migration(config, **kwargs):
            monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
            monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
            return original(config, **kwargs)

        monkeypatch.setattr(cli_module, "_maybe_prompt_migration", migration)
        monkeypatch.setattr(typer, "confirm", lambda *a, **kw: pytest.fail("preview prompted"))
        result = env["invoke"]()
        _assert_preview_complete(result)
        assert "config missing recommended excludes" in result.stderr
        assert ("This preview uses your current config." in result.stderr) == tty
        assert ("default: yes" in result.stderr) == tty

    def test_missing_root_preview_matches_empty_root(self, push_preview56):
        env = push_preview56
        # A prior manifest with an empty source exercises proof-list omission.
        _seed_preview_prior(env)
        env["events"].rmdir()
        missing = env["invoke"]()
        _assert_preview_complete(missing)
        assert "mm push will create" in missing.output
        assert not env["events"].exists()
        env["events"].mkdir()
        present = env["invoke"]()
        _assert_preview_complete(present)
        import re

        missing_text = re.sub(
            r"mm push will create .*? for the mm-events source\. ", "", _preview_text(missing)
        )
        assert missing_text == _preview_text(present)

    @pytest.mark.parametrize("excluded", [0, 1, 2])
    def test_missing_default_root_previews_filtered_deletions(self, push_preview56, excluded):
        env = push_preview56
        _seed_preview_prior(
            env, event_files=("events/dev-a-2026-09-10.jsonl", "events/dev-b-2026-09-12.jsonl")
        )
        env["config"]["sync"]["sources"][1]["exclude_patterns"] = [
            f"events/{name}.jsonl" for name in ("dev-a-2026-09-10", "dev-b-2026-09-12")[:excluded]
        ]
        save_config(env["config"], env["config_path"])
        env["events"].rmdir()
        result = env["invoke"]()
        _assert_preview_complete(result)
        if excluded < 2:
            # Display compares against the advertised snapshot, including exclusions.
            assert "- 2 deleted" in result.output

    def test_missing_root_deletions_use_sidecar_recovered_prior(self, push_preview56):
        from mind_meld import sidecar as sidecar_mod
        from mind_meld.storage.keys import manifest_key

        env = push_preview56
        prior = _seed_preview_prior(env, event_files=("events/a.jsonl",))
        prior["tombstones"] = {}
        sidecar_mod.write(prior)
        env["backend"].put(manifest_key("dev-a"), b"corrupt")
        env["events"].rmdir()
        result = env["invoke"]()
        _assert_preview_complete(result)
        assert "recovered prior state from local sidecar" in result.output

    def test_unrecoverable_corrupt_manifest_is_lock_only(self, push_preview56):
        from mind_meld.storage.keys import manifest_key

        env = push_preview56
        env["backend"].put(manifest_key("dev-a"), b"corrupt")
        result = env["invoke"]()
        _assert_preview_refused(result)
        assert "remote manifest corrupt" in result.output

    def test_events_subdir_missing_remains_truthful_deletion(self, push_preview56):
        env = push_preview56
        _seed_preview_prior(env, event_files=("events/a.jsonl",))
        result = env["invoke"]()
        _assert_preview_complete(result)
        assert "- 1 deleted" in result.output
        assert "mm push will create" not in result.output

    def test_unwritable_ancestor_refuses(self, push_preview56):
        env = push_preview56
        env["events"].rmdir()
        parent = env["events"].parent
        parent.chmod(0o500)
        try:
            result = env["invoke"]()
            _assert_preview_refused(result)
            assert "could not be created" in result.output
            assert "EACCES" in result.output
        finally:
            parent.chmod(0o700)

    @pytest.mark.parametrize("pending_repair", [False, True])
    def test_drift_reports_safe_remedy_and_pending_facts_without_repair(
        self, push_preview56, pending_repair
    ):
        env = push_preview56
        # Lexicographically smaller lineage wins; local fingerprint still pins canonical.
        salt = b"\x00" * 16
        key = crypto_module.derive_key(PASSPHRASE, salt, memory_kb=MEMORY_KB)
        blob = crypto_module._serialize_crypto_init(
            MEMORY_KB,
            salt,
            crypto_module._encrypt_with_master_key(crypto_module._KEYCHECK_PLAINTEXT, key),
        )
        if pending_repair:
            (env["backend"].root / "mm-crypto-init 2").write_bytes(blob)
        else:
            env["backend"].put("mm-crypto-init", blob)
        result = env["invoke"]()
        _assert_preview_refused(result)
        text = _preview_text(result)
        assert "root_salt changed" in text
        assert f"local fp={env['config']['crypto']['root_salt_fp']}" in text
        assert f"storage fp={crypto_module.root_salt_fingerprint(salt)}" in text
        assert "Allow the fleet's mm-crypto-init copies to finish syncing" in text
        assert "inspect with 'mm diag'" in text
        assert "If the fingerprints still differ after syncing, stop" in text
        assert "do not reinitialize or re-key" in text
        assert "mm init" not in text
        assert "minutes" not in text and "seconds" not in text
        qualification = (
            "Pending conflict cleanup does not resolve this device's fingerprint mismatch"
        )
        assert (qualification in text) == pending_repair
        if pending_repair:
            assert "replace the canonical copy; delete 1 identical conflict copies" in text
            assert "preserve 1 differing or unreadable copies" in text
            assert text.index("after syncing, stop") < text.index(qualification)
            assert "verifies the passphrase will reconcile mm-crypto-init" in text

    def test_scan_failure_is_lock_only(self, push_preview56, monkeypatch):
        env = push_preview56
        original = cli_module.manifest._open_nofollow_nonblock

        def fail(path):
            if Path(path).name == "role.md":
                raise PermissionError("scan refused")
            return original(path)

        monkeypatch.setattr(cli_module.manifest, "_open_nofollow_nonblock", fail)
        result = env["invoke"]()
        _assert_preview_refused(result)
        assert "could not be read" in result.output

    def test_real_push_performs_deferred_setup_once(self, push_preview56):
        env = push_preview56
        del env["config"]["crypto"]["root_salt_fp"]
        save_config(env["config"], env["config_path"])
        env["events"].rmdir()
        env["cache"].update(last_seen_self_version="0.0.1", latest_version="99.0.0")
        env["upgrade"].CACHE_PATH.write_text(json.dumps(env["cache"]))
        copy = env["backend"].root / "mm-crypto-init 2"
        copy.write_bytes(env["backend"].get("mm-crypto-init"))
        _assert_preview_complete(env["invoke"]())
        assert "root_salt_fp" not in tomllib.loads(env["config_path"].read_text())["crypto"]
        for _ in range(2):
            env["upgrade"]._reset_for_tests()
            result = runner.invoke(app, ["push"])
            assert result.exit_code == 0, result.output
        assert env["events"].is_dir()
        assert not copy.exists()
        assert "root_salt_fp" in tomllib.loads(env["config_path"].read_text())["crypto"]
        rows = env["history"]()
        assert len([row for row in rows if row["verb"] == "self-upgrade"]) == 1
        cache = json.loads(env["upgrade"].CACHE_PATH.read_text())
        assert cache["last_nudged_version"] == "99.0.0"


class TestMmEventsOwnership59A:
    @pytest.mark.no_mm_events_isolation
    def test_init_creates_private_root_before_backfill(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        root = tmp_path / ".local" / "share" / "mind-meld"
        TestInitFlow()._setup_monkeypatch(tmp_path, monkeypatch)
        monkeypatch.setattr(cli_module.skill_link, "_ensure_retro_skill_links", lambda **kw: ())
        calls = []

        def backfill(*args):
            calls.append(True)
            assert root.stat().st_mode & 0o777 == 0o700

        monkeypatch.setattr(cli_module.events_tail, "_run_events_backfill", backfill)
        result = runner.invoke(
            app, ["init"], input=f"{tmp_path / 'storage'}\nMac A\npw\npw\nY\nn\nn\nn\nn\nn\n"
        )
        assert result.exit_code == 0, result.output
        assert calls == [True]

    @pytest.mark.skipif(sys.platform != "darwin", reason="APFS-default needed")
    def test_pull_case_collision_into_missing_default_root(self, push_preview56):
        from mind_meld.storage.keys import blob_key, manifest_key

        env = push_preview56
        prior = _seed_preview_prior(env)
        files = {}
        for rel, data in [("events/A.jsonl", b"upper\n"), ("events/a.jsonl", b"lower\n")]:
            digest = hashlib.sha256(data).hexdigest()
            files[rel] = {"sha256": digest, "size": len(data), "mtime": None}
            env["backend"].put(blob_key("dev-a", digest), encrypt(data, PASSPHRASE, MEMORY_KB))
        prior["sources"]["mm-events"]["files"] = files
        env["backend"].put(
            manifest_key("dev-a"), encrypt(serialize_manifest(prior), PASSPHRASE, MEMORY_KB)
        )
        env["events"].rmdir()
        result = runner.invoke(app, ["pull", "--from", "dev-a", "--source", "mm-events"])
        assert result.exit_code == 0, result.output
        assert "mm: warning:" in result.stderr
        assert (env["events"] / "events" / "A.jsonl").read_bytes() == b"upper\n"
        assert len(list((env["events"] / "events").iterdir())) == 1
        assert env["events"].stat().st_mode & 0o777 == 0o700

    @pytest.mark.parametrize(
        "command",
        [
            ["status"],
            ["diag"],
            ["sources"],
            ["diff"],
            ["conflicts"],
            ["resolve"],
            ["gc"],
            ["disable-source", "mm-events"],
            ["enable-source", "mm-events"],
            ["reconfigure-sources"],
            ["autopush"],
            ["pull"],
            ["pull", "--source", "claude"],
        ],
    )
    def test_inspection_does_not_create_root(self, push_preview56, monkeypatch, command):
        env = push_preview56
        monkeypatch.setattr(cli_module, "__version__", "0.14.12")
        monkeypatch.setattr(env["upgrade"], "__version__", "0.14.12")
        monkeypatch.delenv("MM_SKILLS_DIR", raising=False)
        if command == ["autopush"]:
            # Isolate the post-core no-sources inspection from the real writer.
            monkeypatch.setattr(cli_module, "_push_core", lambda *a, **kw: None)
        env["events"].rmdir()
        records = []
        token = _PREVIEW_AUDIT.set((records, env["events"].parents[2] / "test.lock"))
        try:
            result = runner.invoke(app, command, input="\n" * 20)
        finally:
            _PREVIEW_AUDIT.reset(token)
        assert result.exit_code == 0, result.output
        assert not env["events"].exists()
        assert not [r for r in records if str(env["events"]) in r[1]], records

    @pytest.mark.parametrize("kind", ["default", "custom", "symlink", "chmod-failure"])
    def test_push_tightens_only_owned_default_root(self, push_preview56, monkeypatch, kind):
        from mind_meld.storage.keys import manifest_key

        env = push_preview56
        root = env["events"]
        target = root
        if kind in {"custom", "symlink"}:
            target = root.parent / "custom"
            target.mkdir()
            if kind == "symlink":
                root.rmdir()
                root.symlink_to(target, target_is_directory=True)
            else:
                env["config"]["sync"]["sources"][1]["path"] = str(target)
                save_config(env["config"], env["config_path"])
        target.chmod(0o755)
        if kind == "chmod-failure":
            original = os.fchmod
            info = root.stat()

            def fchmod(fd, mode):
                actual = os.fstat(fd)
                if (actual.st_dev, actual.st_ino) == (info.st_dev, info.st_ino):
                    raise PermissionError("tightening denied")
                return original(fd, mode)

            monkeypatch.setattr(os, "fchmod", fchmod)
        errors = []
        for _ in range(2):
            result = runner.invoke(app, ["push"])
            assert result.exit_code == 0, result.output
            errors.append(result.stderr)
        expected_mode = 0o700 if kind == "default" else 0o755
        assert target.stat().st_mode & 0o777 == expected_mode
        assert sum(text.count("could not tighten mm-events") for text in errors) == (
            1 if kind == "chmod-failure" else 0
        )
        assert env["backend"].exists(manifest_key("dev-a"))

    @pytest.mark.parametrize("own_today", [False, True])
    @pytest.mark.parametrize("wipe_root", [False, True])
    def test_preview_matches_push_except_activity_row(self, push_preview56, own_today, wipe_root):
        env = push_preview56
        today = datetime.now(timezone.utc).date().isoformat()
        own = f"events/dev-a-{today}.jsonl"
        prior_paths = ["events/dev-a-2026-09-10.jsonl", "events/dev-b-2026-09-12.jsonl"]
        if own_today:
            prior_paths.append(own)
        _seed_preview_prior(env, event_files=prior_paths)
        if wipe_root:
            env["events"].rmdir()
        preview = env["invoke"]()
        _assert_preview_complete(preview)
        assert f"- {len(prior_paths)} deleted" in preview.output
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output
        manifest = cli_module._fetch_remote_manifest(
            env["backend"], "dev-a", PASSPHRASE, MEMORY_KB
        ).manifest
        assert set(manifest["sources"]["mm-events"]["files"]) == {own}
        assert set(manifest["tombstones"]) == {f"mm-events:{p}" for p in prior_paths if p != own}
        assert env["events"].stat().st_mode & 0o777 == 0o700

    @pytest.mark.parametrize("command", ["push", "autopush"])
    def test_missing_custom_root_skips_without_tombstones_and_resumes(
        self, push_preview56, monkeypatch, command
    ):
        env = push_preview56
        prior = _seed_preview_prior(env, event_files=["events/dev-a-2026-09-10.jsonl"])
        existing_key = "mm-events:events/old.jsonl"
        prior["tombstones"] = {
            existing_key: {
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "deleted_by": "dev-a",
            }
        }
        from mind_meld.storage.keys import manifest_key

        env["backend"].put(
            manifest_key("dev-a"), encrypt(serialize_manifest(prior), PASSPHRASE, MEMORY_KB)
        )
        custom = env["events"].parent / "unplugged"
        env["config"]["sync"]["sources"][1]["path"] = str(custom)
        save_config(env["config"], env["config_path"])

        def no_tail(*a, **kw):
            pytest.fail("missing custom source ran events tail")

        monkeypatch.setattr(cli_module.events_tail, "_run_events_tail", no_tail)
        breadcrumbs = []
        monkeypatch.setattr(
            cli_module, "_write_autorun_breadcrumb", lambda *args: breadcrumbs.append(args)
        )
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        assert result.stderr.count("mm: warning:") == 1
        assert str(custom) in result.stderr
        assert "plug it in, create it, or run mm disable-source mm-events" in result.stderr
        assert not custom.exists()
        manifest = cli_module._fetch_remote_manifest(
            env["backend"], "dev-a", PASSPHRASE, MEMORY_KB
        ).manifest
        assert "mm-events" not in manifest["sources"]
        assert "claude" in manifest["sources"]
        assert set(manifest["tombstones"]) == {existing_key}
        if command == "autopush":
            assert breadcrumbs[-1][1] == "degraded"
            assert "pushed 0 files" not in result.output
            assert str(custom) in breadcrumbs[-1][2]
        (custom / "events").mkdir(parents=True)
        (custom / "events" / "returned.jsonl").write_text("{}\n")
        monkeypatch.setattr(cli_module.events_tail, "_run_events_tail", lambda *a, **kw: [])
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output
        manifest = cli_module._fetch_remote_manifest(
            env["backend"], "dev-a", PASSPHRASE, MEMORY_KB
        ).manifest
        assert set(manifest["sources"]["mm-events"]["files"]) == {"events/returned.jsonl"}

    @pytest.mark.parametrize("failure", ["parent", "unrelated", "custom"])
    def test_recapture_setup_failure_writes_no_rows(self, push_preview56, monkeypatch, failure):
        env = push_preview56
        env["events"].rmdir()
        if failure == "parent":
            original = Path.mkdir

            def mkdir(path, *a, **kw):
                if path == env["events"]:
                    raise PermissionError(13, "denied", str(path))
                return original(path, *a, **kw)

            monkeypatch.setattr(Path, "mkdir", mkdir)
        elif failure == "unrelated":
            original = Path.stat

            def stat(path, *a, **kw):
                if path == env["claude"]:
                    raise PermissionError(13, "denied", str(path))
                return original(path, *a, **kw)

            monkeypatch.setattr(Path, "stat", stat)
        else:
            env["config"]["sync"]["sources"][1]["path"] = str(env["events"].parent / "custom")
            save_config(env["config"], env["config_path"])
        result = runner.invoke(app, ["recapture", "1d"])
        assert result.exit_code == 1, result.output
        assert "No recapture rows were written." in _preview_text(result)
        assert "disabled on this Mac" not in result.output
        assert not env["events"].exists()


def test_bootstrap_calls_stay_in_explicit_writers59a():
    import ast

    root = Path(__file__).parents[1] / "src" / "mind_meld"
    found = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            bootstrap = next((kw.value for kw in node.keywords if kw.arg == "bootstrap"), None)
            writes = name == "_bootstrap_mm_events_path" or (
                bootstrap is not None
                and not (isinstance(bootstrap, ast.Constant) and bootstrap.value is False)
            )
            if not writes or path.name == "config.py":
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            found.add((path.name, owner.name if owner else None))
    assert found == {("cli.py", n) for n in ("init", "_push_core", "recapture", "_pull_one_source")}


def _seed_crypto_lineages60(env):
    def blob(salt):
        master = crypto_module.load_master_key(PASSPHRASE, salt, MEMORY_KB)
        return crypto_module._serialize_crypto_init(
            MEMORY_KB,
            salt,
            crypto_module._encrypt_with_master_key(crypto_module._KEYCHECK_PLAINTEXT, master),
        )

    backend = env["backend"]
    canonical = backend.root / "mm-crypto-init"
    canonical.write_bytes(blob(b"\x00" * 16))
    identical = backend.root / "mm-crypto-init 2"
    identical.write_bytes(canonical.read_bytes())
    distinct = backend.root / "mm-crypto-init 3"
    distinct.write_bytes(blob(b"\xff" * 16))
    env["config"]["crypto"]["root_salt_fp"] = crypto_module.root_salt_fingerprint(b"\x00" * 16)
    save_config(env["config"], env["config_path"])
    return canonical, identical, distinct


class TestNewerStorage66A:
    @pytest.mark.parametrize("copy_name", ["mm-crypto-init", "mm-crypto-init 2"])
    @pytest.mark.parametrize(
        "command", ["push", "pull", "gc", "autopush", "autopull", "status", "diff", "recapture"]
    )
    def test_refuses_before_manifest_read(self, push_preview56, monkeypatch, copy_name, command):
        from mind_meld import sidecar, upgrade

        env = push_preview56
        backend = env["backend"]
        older = backend.get("mm-crypto-init")
        (backend.root / copy_name).write_bytes(b"\x03")
        if copy_name == "mm-crypto-init":
            (backend.root / "mm-crypto-init 2").write_bytes(older)
        before = _preview_tree(backend.root)
        monkeypatch.setattr(
            cli_module, "_fetch_remote_manifest", lambda *a, **kw: pytest.fail("read manifest")
        )
        result = runner.invoke(app, [command])
        assert result.exit_code == (0 if command.startswith("auto") else 1), result.output
        flat = " ".join(result.output.split())
        assert upgrade.INSTALL_CMD in flat
        assert "newer mm (format 0x03)" in flat
        assert "delete" not in flat.lower()
        assert "re-run 'mm init'" not in flat
        assert _preview_tree(backend.root) == before
        if command.startswith("auto"):
            row = json.loads((sidecar.SIDECAR_DIR / "last-autorun.json").read_text())
            assert row[command[4:]]["outcome"] == "crypto-error"

    @pytest.mark.parametrize("race", [False, True])
    def test_init_refuses_newer_probe_or_bootstrap_race(self, tmp_path, monkeypatch, race):
        from mind_meld import upgrade
        from mind_meld.errors import StorageError

        cfg = TestInitFlow()._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "storage"
        storage.mkdir()

        def arrive(*a, **kw):
            (storage / "mm-crypto-init").write_bytes(b"\x03")
            raise StorageError("already exists")

        if race:
            monkeypatch.setattr(cli_module, "bootstrap_crypto_init", arrive)
        else:
            (storage / "mm-crypto-init").write_bytes(b"\x03")
        result = runner.invoke(app, ["init"], input=f"{storage}\nMac A\npw\npw\n")
        assert result.exit_code == 1, result.output
        assert upgrade.INSTALL_CMD in " ".join(result.output.split())
        assert "delete" not in result.output.lower()
        assert not cfg.exists()
        assert {p.name: p.read_bytes() for p in storage.iterdir()} == {"mm-crypto-init": b"\x03"}

    def test_init_refuses_newer_arriving_after_successful_bootstrap(self, tmp_path, monkeypatch):
        from mind_meld import upgrade

        cfg = TestInitFlow()._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "storage"
        storage.mkdir()
        original = cli_module.bootstrap_crypto_init

        def bootstrap(backend, passphrase, **kwargs):
            result = original(backend, passphrase, **kwargs)
            (backend.root / "mm-crypto-init").write_bytes(b"\x03")
            return result

        monkeypatch.setattr(cli_module, "bootstrap_crypto_init", bootstrap)
        result = runner.invoke(app, ["init"], input=f"{storage}\nMac A\npw\npw\n")
        assert result.exit_code == 1, result.output
        assert upgrade.INSTALL_CMD in " ".join(result.output.split())
        assert not cfg.exists()
        assert (storage / "mm-crypto-init").read_bytes() == b"\x03"

    def test_repair_arrival_keeps_upgrade_remedy(self, push_preview56, monkeypatch):
        from mind_meld import upgrade

        backend = push_preview56["backend"]
        original = crypto_module.apply_crypto_init_repair

        def arrive(backend, fetch):
            (backend.root / "mm-crypto-init 2").write_bytes(b"\x03")
            return original(backend, fetch)

        monkeypatch.setattr(crypto_module, "apply_crypto_init_repair", arrive)
        before = backend.get("mm-crypto-init")
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 1, result.output
        assert upgrade.INSTALL_CMD in " ".join(result.output.split())
        assert backend.get("mm-crypto-init") == before
        assert not backend.list_keys("manifests/")

    @pytest.mark.parametrize("command", ["pull", "gc", "push"])
    def test_newer_manifest_arrives_before_crypto_init(self, push_preview56, command):
        env = push_preview56
        backend = env["backend"]
        register_device(backend, "dev-peer", "Peer")
        peer_key = storage_keys.manifest_key("dev-peer")
        backend.put(peer_key, b"\x03")
        before = backend.get(peer_key)
        local_before = _preview_tree(env["claude"])
        result = runner.invoke(app, [command])
        # Pull skips unreadable peers; neither explicit nor post-push GC reads them.
        assert result.exit_code == 0, result.output
        assert backend.get(peer_key) == before
        assert _preview_tree(env["claude"]) == local_before
        if command == "push":
            assert sorted(backend.list_keys("manifests/")) == [
                storage_keys.manifest_key("dev-a"),
                peer_key,
            ]
        elif command == "pull":
            assert "corrupt" in result.output.lower()
        else:
            assert "Blob collection was skipped" in " ".join(result.stderr.split())

    @pytest.mark.parametrize("command", ["status", "diff"])
    def test_newer_manifest_inspection_never_claims_recovery_or_empty_diff(
        self, push_preview56, command
    ):
        from mind_meld import upgrade

        env = push_preview56
        backend = env["backend"]
        backend.put(storage_keys.manifest_key(env["config"]["device"]["id"]), b"\x03")

        result = runner.invoke(app, [command])
        text = " ".join((result.output + (result.stderr or "")).split())
        assert upgrade.INSTALL_CMD in text
        assert "next 'mm push' will attempt recovery" not in text
        assert "showing diff against empty remote" not in text
        assert "In sync." not in text
        assert "Total remote:" not in text
        assert result.exit_code == (0 if command == "status" else 1), result.output

    @pytest.mark.parametrize("shape", ["older-sibling", "conflict-only"])
    @pytest.mark.parametrize("owner", ["dev-a", "dev-peer"])
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_newer_manifest_sibling_blocks_only_own_gc(self, push_preview56, shape, owner, dry_run):
        env = push_preview56
        backend = env["backend"]
        register_device(backend, "dev-peer", "Peer")
        TestGCSafety._store_manifest(backend, "dev-a")
        key = storage_keys.manifest_key(owner)
        parent = backend.root / Path(key).parent
        parent.mkdir(parents=True, exist_ok=True)
        older = encrypt(
            serialize_manifest(
                {
                    "device_id": owner,
                    "device_name": owner,
                    "sources": {},
                    "tombstones": {},
                }
            ),
            PASSPHRASE,
            MEMORY_KB,
        )
        if shape == "older-sibling":
            backend.put(key, b"\x03")
            (parent / "manifest.json 2.enc").write_bytes(older)
        else:
            (parent / "manifest.json 2.enc").write_bytes(b"\x03")
        blob = storage_keys.blob_key(owner, "b" * 64)
        backend.put(blob, b"\x03newer-only")
        own_orphan = storage_keys.blob_key("dev-a", "e" * 64)
        backend.put(own_orphan, b"own orphan")
        local_before = _preview_tree(env["claude"])
        result = runner.invoke(app, ["gc"] + (["--dry-run"] if dry_run else []))
        assert result.exit_code == (1 if owner == "dev-a" else 0), result.output
        assert ("cannot GC safely" in result.output) == (owner == "dev-a")
        assert backend.exists(own_orphan) == (owner == "dev-a" or dry_run)
        assert backend.get(blob) == b"\x03newer-only"
        if owner == "dev-a":
            if dry_run:
                _assert_preview_refused(result)
            assert "Run 'mm push'" not in result.output
            return
        pull = runner.invoke(app, ["pull"])
        assert pull.exit_code == 0, pull.output
        assert backend.get(blob) == b"\x03newer-only"
        assert _preview_tree(env["claude"]) == local_before

    def test_newer_manifest_replacing_canonical_after_scan_blocks_gc(
        self, push_preview56, monkeypatch
    ):
        env = push_preview56
        backend = env["backend"]
        key = storage_keys.manifest_key(env["config"]["device"]["id"])
        parent = backend.root / Path(key).parent
        parent.mkdir(parents=True, exist_ok=True)
        older = encrypt(
            serialize_manifest(
                {
                    "device_id": "dev-a",
                    "device_name": "Mac A",
                    "sources": {},
                    "tombstones": {},
                }
            ),
            PASSPHRASE,
            MEMORY_KB,
        )
        backend.put(key, older)
        (parent / "manifest.json 2.enc").write_bytes(older)
        blob = storage_keys.blob_key("dev-a", "c" * 64)
        backend.put(blob, b"newer-only")
        reads = {"n": 0}
        original = type(backend).get

        def get(self, storage_key):
            data = original(self, storage_key)
            if storage_key == key:
                reads["n"] += 1
                if reads["n"] > 1:
                    return b"\x03"
            return data

        monkeypatch.setattr(type(backend), "get", get)
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 1, result.output
        assert "cannot GC safely" in result.output
        assert backend.get(blob) == b"newer-only"

    def test_newer_manifest_arriving_during_conflict_validation_is_not_ignored(
        self, push_preview56, monkeypatch
    ):
        env = push_preview56
        backend = env["backend"]
        register_device(backend, "dev-peer", "Peer")
        key = storage_keys.manifest_key("dev-peer")
        parent = backend.root / Path(key).parent
        parent.mkdir(parents=True, exist_ok=True)
        current = encrypt(
            serialize_manifest(
                {
                    "device_id": "dev-peer",
                    "device_name": "Peer",
                    "sources": {},
                    "tombstones": {},
                }
            ),
            PASSPHRASE,
            MEMORY_KB,
        )
        conflict = parent / "manifest.json 2.enc"
        conflict.write_bytes(current)
        reads = 0
        original = Path.read_bytes

        def read_bytes(path):
            nonlocal reads
            if path == conflict:
                reads += 1
                if reads == 2:
                    return b"\x03"
            return original(path)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        fetched = cli_module._fetch_remote_manifest(backend, "dev-peer", PASSPHRASE, MEMORY_KB)
        assert fetched.status == "corrupt"
        assert fetched.newer_version == 3

    def test_late_newer_manifest_blocks_publish_and_gc(self, push_preview56, monkeypatch):
        env = push_preview56
        backend = env["backend"]
        mine = env["config"]["device"]["id"]
        key = storage_keys.manifest_key(mine)
        TestGCSafety._store_manifest(backend, mine)
        original = cli_module._fetch_remote_manifest
        phase = {"name": "push", "n": 0}

        def fetch(storage, device_id, passphrase, memory_kb):
            result = original(storage, device_id, passphrase, memory_kb)
            if device_id != mine:
                return result
            phase["n"] += 1
            if phase["n"] > 1:
                return cli_module.ManifestFetch(status="corrupt", newer_version=3)
            return result

        monkeypatch.setattr(cli_module, "_fetch_remote_manifest", fetch)
        published = backend.get(key) if backend.exists(key) else None
        pushed = runner.invoke(app, ["push"])
        assert pushed.exit_code == 1, pushed.output
        assert "newer mm (format 0x03)" in pushed.output
        assert (backend.get(key) if backend.exists(key) else None) == published
        phase["name"] = "gc"
        phase["n"] = 0
        blob = storage_keys.blob_key(mine, "d" * 64)
        backend.put(blob, b"only-late-manifest")
        collected = runner.invoke(app, ["gc"])
        assert collected.exit_code == 1, collected.output
        assert "cannot GC safely" in collected.output
        assert backend.get(blob) == b"only-late-manifest"

    def test_push_leaves_own_newer_manifest_in_place(self, push_preview56):
        env = push_preview56
        backend = env["backend"]
        key = storage_keys.manifest_key(env["config"]["device"]["id"])
        backend.put(key, b"\x03")
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 1, result.output
        assert "newer mm (format 0x03)" in result.output
        assert backend.get(key) == b"\x03"

    def test_autopull_newer_blob_names_upgrade(self, push_preview56):
        from mind_meld import sidecar, upgrade

        env = push_preview56
        backend = env["backend"]
        register_device(backend, "dev-peer", "Peer")
        sha = "b" * 64
        backend.put(storage_keys.blob_key("dev-peer", sha), b"\x03")
        fetched = crypto_module.fetch_crypto_init(backend)
        crypto_module.set_crypto_session(fetched.root_salt, MEMORY_KB)
        manifest = {
            "device_id": "dev-peer",
            "device_name": "Peer",
            "sources": {
                "claude": {"files": {"projects/-Users-kb-myapp/memory/new.md": {"sha256": sha}}}
            },
            "tombstones": {},
        }
        backend.put(
            storage_keys.manifest_key("dev-peer"),
            encrypt(serialize_manifest(manifest), PASSPHRASE, MEMORY_KB),
        )
        before = _preview_tree(env["claude"])
        result = runner.invoke(app, ["autopull"])
        assert result.exit_code == 0, result.output
        flat = result.stderr
        assert upgrade.INSTALL_CMD in flat
        assert "unsupported format version 0x03" in flat
        assert _preview_tree(env["claude"]) == before
        row = json.loads((sidecar.SIDECAR_DIR / "last-autorun.json").read_text())
        assert row["pull"]["outcome"] == "degraded"

    def test_damaged_crypto_init_has_no_delete_advice(self, push_preview56):
        from mind_meld.errors import NEWER_STORAGE_URL

        backend = push_preview56["backend"]
        (backend.root / "mm-crypto-init").write_bytes(b"\x10garbage")
        result = runner.invoke(app, ["status"])
        flat = (result.output + result.stderr).lower()
        assert result.exit_code == 1, result.output
        assert "delete" not in flat
        assert "re-run 'mm init'" not in flat
        assert NEWER_STORAGE_URL in result.output + result.stderr

    def test_newer_blob_warning_names_upgrade(self, push_preview56):
        from mind_meld import upgrade

        env = push_preview56
        backend = env["backend"]
        register_device(backend, "dev-peer", "Peer")
        sha = "a" * 64
        backend.put(storage_keys.blob_key("dev-peer", sha), b"\x03")
        fetched = crypto_module.fetch_crypto_init(backend)
        crypto_module.set_crypto_session(fetched.root_salt, MEMORY_KB)
        manifest = {
            "device_id": "dev-peer",
            "device_name": "Peer",
            "sources": {
                "claude": {"files": {"projects/-Users-kb-myapp/memory/new.md": {"sha256": sha}}}
            },
            "tombstones": {},
        }
        backend.put(
            storage_keys.manifest_key("dev-peer"),
            encrypt(serialize_manifest(manifest), PASSPHRASE, MEMORY_KB),
        )
        before = _preview_tree(env["claude"])
        result = runner.invoke(app, ["pull"])
        assert result.exit_code == 1, result.output
        assert upgrade.INSTALL_CMD in " ".join(result.stderr.split()), result.output
        assert "unsupported format version 0x03" in result.stderr
        assert _preview_tree(env["claude"]) == before


class TestCryptoInspection60A:
    @pytest.mark.parametrize("two_distinct", [False, True])
    @pytest.mark.parametrize(
        "command",
        [
            ["status"],
            ["diag", "--json"],
            ["diff"],
            ["pull", "--dry-run"],
            ["gc", "--dry-run"],
            ["recapture", "--dry-run"],
        ],
    )
    def test_inspection_never_repairs(self, push_preview56, command, two_distinct):
        env = push_preview56
        paths = _seed_crypto_lineages60(env)
        if two_distinct:
            salt = b"\xee" * 16
            master = crypto_module.load_master_key(PASSPHRASE, salt, MEMORY_KB)
            paths[1].write_bytes(
                crypto_module._serialize_crypto_init(
                    MEMORY_KB,
                    salt,
                    crypto_module._encrypt_with_master_key(
                        crypto_module._KEYCHECK_PLAINTEXT, master
                    ),
                )
            )
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        result = runner.invoke(app, command)
        assert result.exit_code in ([0, 1] if command[0] == "recapture" else [0]), result.output
        assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths} == before
        assert not list(env["backend"].root.glob("mm-crypto-init.preserved-*"))
        if command[0] == "diag":
            counts = json.loads(result.stdout)["crypto_init"]["pending_repair"]
            assert counts == {
                "replace_canonical": False,
                "delete": 0 if two_distinct else 1,
                "preserve": 2 if two_distinct else 1,
            }
            assert paths[1].name not in result.stdout
        else:
            assert (
                f"delete {0 if two_distinct else 1} identical conflict copies; "
                f"preserve {2 if two_distinct else 1}" in _preview_text(result)
            )

    @pytest.mark.parametrize("correct", [False, True])
    def test_push_repairs_only_after_verification(self, push_preview56, monkeypatch, correct):
        env = push_preview56
        paths = _seed_crypto_lineages60(env)
        before = {p: p.read_bytes() for p in paths}
        if not correct:
            monkeypatch.setenv("MINDMELD_PASSPHRASE", "wrong")
        result = runner.invoke(app, ["push"])
        if correct:
            assert result.exit_code == 0, result.output
            assert not paths[1].exists() and not paths[2].exists()
            assert paths[0].read_bytes() == before[paths[0]]
            assert [
                p.read_bytes() for p in env["backend"].root.glob("mm-crypto-init.preserved-*")
            ] == [before[paths[2]]]
        else:
            assert result.exit_code == 1, result.output
            assert {p: p.read_bytes() for p in paths} == before

    def test_status_never_persists_missing_fingerprint(self, push_preview56):
        env = push_preview56
        env["config"]["crypto"].pop("root_salt_fp")
        save_config(env["config"], env["config_path"])
        before = env["config_path"].read_bytes()
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert env["config_path"].read_bytes() == before

    def test_stale_winner_aborts_push_before_publication(self, push_preview56, monkeypatch):
        env = push_preview56
        canonical, _, _ = _seed_crypto_lineages60(env)
        original = crypto_module.apply_crypto_init_repair

        def replace_winner(backend, fetch):
            raw = canonical.read_bytes()
            canonical.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            return original(backend, fetch)

        monkeypatch.setattr(crypto_module, "apply_crypto_init_repair", replace_winner)
        monkeypatch.setattr(
            cli_module, "_push_core", lambda *a, **kw: pytest.fail("published after winner change")
        )
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 1, result.output
        assert "nothing was published" in _preview_text(result)

    def test_stale_winner_aborts_init_before_config_or_registration(self, tmp_path, monkeypatch):
        cfg = TestInitFlow()._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "storage"
        storage.mkdir()
        backend = LocalBackend(storage)
        bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
        original = crypto_module.apply_crypto_init_repair

        def replace_winner(backend, fetch):
            canonical = backend.root / "mm-crypto-init"
            raw = canonical.read_bytes()
            canonical.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            return original(backend, fetch)

        monkeypatch.setattr(crypto_module, "apply_crypto_init_repair", replace_winner)
        monkeypatch.setattr(
            cli_module, "_register_and_save", lambda *a, **kw: pytest.fail("stale init registered")
        )
        result = runner.invoke(app, ["init"], input=f"{storage}\nMac A\n{PASSPHRASE}\n")
        assert result.exit_code == 1, result.output
        assert "nothing was published" in _preview_text(result)
        assert not cfg.exists()
        assert backend.list_keys("devices/") == []


@pytest.mark.parametrize("state", ["absent", "stale", "due", "malformed"])
def test_status_keeps_upgrade_cache_and_network_untouched60a(push_preview56, state):
    env = push_preview56
    path = env["upgrade"].CACHE_PATH
    if state == "absent":
        path.unlink()
    elif state == "malformed":
        path.write_text("bad json")
    else:
        env["cache"]["latest_version"] = "99.0.0"
        if state == "stale":
            env["cache"]["checked_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(env["cache"]))
    before = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
    env["upgrade"]._reset_for_tests()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert env["fetches"] == []
    assert ((path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None) == before
    if state in ("absent", "malformed"):
        assert "Upgrade check: unknown" in result.output
    if state == "stale":
        assert "Upgrade cache: checked" in result.output
        assert "(stale)" in result.output
    if state == "due":
        assert "Upgrade available" in result.output
        assert "99.0.0" in result.output
        assert env["upgrade"].INSTALL_CMD in _preview_text(result)


def test_status_shows_contended_upgrade_cache(push_preview56):
    """cache_state == 'lock_failed' remaps to 'contended' in status's display
    text -- untested by the parametrized test above, which only covers
    missing/stale/due/malformed cache shapes, never lock contention."""
    from mind_meld.lockedjson import locked_json_rmw

    env = push_preview56
    env["upgrade"]._reset_for_tests()
    with locked_json_rmw(env["upgrade"].CACHE_PATH):
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Upgrade check: unknown (contended cache)." in result.output


@pytest.mark.parametrize("empty", [False, True])
def test_requested_capture_publishes_warm_read_with_two_invocations(capture61, monkeypatch, empty):
    day = datetime.now(timezone.utc).date().isoformat()
    usage = {"input": 17, "output": 3, "cache_read": 0, "cache_create": 0}
    calls = []

    def bounded(**kwargs):
        calls.append("bounded")
        return _mm_host_usage.HostUsageResult({}, complete=False, reason="deadline")

    def warm(**kwargs):
        calls.append("warm")
        return _mm_host_usage.HostUsageResult(
            {} if empty else {"codex": {day: usage}},
            complete=True,
            tokens_by_day={} if empty else {day: {**usage, "by_model": {"gpt-5": usage}}},
        )

    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", bounded)
    monkeypatch.setattr(_mm_host_usage, "warm_host_cache_inline", warm)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert calls == ["bounded", "warm"]
    assert (
        "Usage capture: codex — " + ("completed, no usage" if empty else "contributed")
        in result.output
    )
    rows = [json.loads(line) for line in capture61["dayfile"].read_text().splitlines()]
    row = [r for r in rows if r["type"] == "host-usage-snapshot"][-1]
    assert row["hosts"] == ({} if empty else {"codex": {day: usage}})
    publication = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    assert publication["publication"] == "published"


def test_all_failed_autopush_keeps_previous_snapshot_to_age(capture61, monkeypatch):
    initial = runner.invoke(app, ["push"])
    assert initial.exit_code == 0, initial.output
    before = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    prior_rows = [json.loads(line) for line in capture61["dayfile"].read_text().splitlines()]
    prior_hosts = [row for row in prior_rows if row["type"] == "host-usage-snapshot"]
    monkeypatch.setattr(
        _mm_host_usage,
        "read_codex_usage",
        lambda **kwargs: _mm_host_usage.HostUsageResult({}, complete=False, reason="deadline"),
    )
    (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("changed")
    result = runner.invoke(app, ["autopush"])
    assert result.exit_code == 0, result.output
    after = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    rows = [json.loads(line) for line in capture61["dayfile"].read_text().splitlines()]
    assert [row for row in rows if row["type"] == "host-usage-snapshot"] == prior_hosts
    assert after["ts"] == before["ts"]
    assert after["readers"] == before["readers"] == {"codex": "contributed"}


@pytest.mark.parametrize("phase", ["capture", "warm-notice", "serialization"])
def test_attended_capture_exception_never_retries_or_blocks_content(capture61, monkeypatch, phase):
    env = capture61
    (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    calls = []
    real = events_tail._capture_host_snapshot

    def capture(*a, **kw):
        calls.append(True)
        if phase == "capture":
            raise RuntimeError("snapshot construction broke")
        return real(*a, **kw)

    monkeypatch.setattr(events_tail, "_capture_host_snapshot", capture)
    if phase == "warm-notice":
        monkeypatch.setattr(
            _mm_host_usage,
            "read_codex_usage",
            lambda **kw: _mm_host_usage.HostUsageResult({}, False, "deadline"),
        )

        def fail_notice(*a):
            raise RuntimeError("notice broke")

        monkeypatch.setattr(events_tail, "_warm_host_cache_with_notice", fail_notice)
    elif phase == "serialization":
        real_write = _mm_events.write_push_event

        def write(path, device, rows, **kw):
            if rows[0]["type"] == "host-usage-snapshot":
                raise ValueError("serialization broke")
            return real_write(path, device, rows, **kw)

        monkeypatch.setattr(_mm_events, "write_push_event", write)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert calls == [True]
    assert "mm: warning: Usage capture" in result.stderr
    assert (
        "projects/-Users-kb-myapp/memory/new.md"
        in cli_module.sidecar.read("dev-a")["sources"]["claude"]["files"]
    )


@pytest.mark.parametrize("capture_state", ["published", "failed", "no-reader"])
@pytest.mark.parametrize("maintenance", ["last-seen", "cleanup"])
def test_content_acceptance_survives_maintenance_independently(
    capture61, monkeypatch, capture_state, maintenance
):
    env = capture61
    (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    if capture_state == "failed":
        monkeypatch.setattr(
            _mm_host_usage,
            "read_codex_usage",
            lambda **kw: _mm_host_usage.HostUsageResult({}, False, "unsupported"),
        )
    elif capture_state == "no-reader":
        monkeypatch.setattr(events_tail, "_default_host_readers", lambda *a, **kw: ())

    def fail(*a, **kw):
        raise OSError("maintenance unavailable")

    monkeypatch.setattr(
        cli_module,
        "update_last_seen" if maintenance == "last-seen" else "_cleanup_conflict_copies",
        fail,
    )
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "post-publication maintenance failed" in " ".join(result.stderr.split())
    assert (
        "projects/-Users-kb-myapp/memory/new.md"
        in cli_module.sidecar.read("dev-a")["sources"]["claude"]["files"]
    )


@pytest.mark.parametrize("content", [False, True])
def test_usage_only_skips_auto_gc_content_runs_it(capture61, monkeypatch, content):
    if content:
        (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    calls = []
    monkeypatch.setattr(cli_module, "_do_gc", lambda *a, **kw: calls.append(True) or 0)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert calls == ([True] if content else [])


@pytest.mark.parametrize("capture_state", ["published", "failed", "no-reader"])
@pytest.mark.parametrize("refusal", [b"corrupt", b"\x03"])
def test_own_gc_refusal_preserves_post_publication_capture_state(
    capture61, monkeypatch, capture_state, refusal
):
    env = capture61
    # Inject unreadable own metadata only after the real publication succeeds.
    original_put = LocalBackend.put

    def put(storage, key, data):
        original_put(storage, key, data)
        if key == storage_keys.manifest_key("dev-a"):
            original_put(storage, key, refusal)

    monkeypatch.setattr(LocalBackend, "put", put)
    (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    if capture_state == "failed":
        monkeypatch.setattr(
            _mm_host_usage,
            "read_codex_usage",
            lambda **kw: _mm_host_usage.HostUsageResult({}, False, "unsupported"),
        )
    elif capture_state == "no-reader":
        monkeypatch.setattr(events_tail, "_default_host_readers", lambda *a, **kw: ())
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == (0 if capture_state == "published" else 1), result.output
    assert "corrupt" in result.stderr.lower()
    assert ("Host usage was published" in result.stderr) == (capture_state == "published")
    record, reason = cli_module.attemptlog.read()
    assert reason is None
    assert (
        record["class"]
        == {"published": "published", "failed": "no-row", "no-reader": "prerequisites"}[
            capture_state
        ]
    )
    assert (
        "projects/-Users-kb-myapp/memory/new.md"
        in cli_module.sidecar.read("dev-a")["sources"]["claude"]["files"]
    )
    accepted = cli_module.sidecar.read("dev-a")
    sha = accepted["sources"]["claude"]["files"]["projects/-Users-kb-myapp/memory/new.md"]["sha256"]
    assert decrypt(
        env["backend"].get(storage_keys.blob_key("dev-a", sha)), PASSPHRASE, MEMORY_KB
    ) == (b"pending")
    assert env["backend"].get(storage_keys.manifest_key("dev-a")) == refusal


@pytest.mark.parametrize("peer_manifest", [b"corrupt", b"\x03"])
def test_unreadable_peer_allows_post_push_own_collection(capture61, peer_manifest):
    env = capture61
    register_device(env["backend"], "peer", "Peer")
    env["backend"].put(storage_keys.manifest_key("peer"), peer_manifest)
    peer_blob = storage_keys.blob_key("peer", hashlib.sha256(b"peer").hexdigest())
    own_orphan = storage_keys.blob_key("dev-a", hashlib.sha256(b"own orphan").hexdigest())
    env["backend"].put(peer_blob, b"peer")
    env["backend"].put(own_orphan, b"own orphan")
    (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "cannot GC safely" not in result.output
    assert not env["backend"].exists(own_orphan)
    assert env["backend"].get(peer_blob) == b"peer"
    assert env["backend"].get(storage_keys.manifest_key("peer")) == peer_manifest


def test_attended_resolves_once_and_bootstraps_inside_lock(capture61, monkeypatch):
    calls = []
    real = cli_module.resolve_sources

    def resolve(*a, **kw):
        calls.append(kw)
        with pytest.raises(cli_module.LockError):
            cli_module.acquire_lock()
        return real(*a, **kw)

    monkeypatch.setattr(cli_module, "resolve_sources", resolve)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert calls == [{"strict": True, "bootstrap": True}]


def test_failed_refresh_records_attempt_without_replacing_capture(capture61, monkeypatch):
    before = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    monkeypatch.setattr(
        _mm_host_usage,
        "read_codex_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({}, False, "unsupported"),
    )
    failed = runner.invoke(app, ["push", "--verbose"])
    assert failed.exit_code == 0 and "(no-row)" in failed.stderr
    after = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
    assert after["ts"] == before["ts"] and after["age_days"] == 0
    assert after["latest_attempt"] == "no-row"
    assert after["latest_attempt_readers"] == {"codex": "dropped:unsupported"}
    status = runner.invoke(app, ["status"])
    assert "Last recorded attended attempt:" in status.stdout
    assert "codex dropped (unsupported)" in status.stdout


@pytest.mark.parametrize("stage", ["host", "activity"])
def test_event_size_guard_keeps_content_and_next_push_usable(capture61, monkeypatch, stage):
    env = capture61
    path = env["dayfile"]
    original = path.read_bytes()
    base = len(original)
    host = {
        "type": "host-usage-snapshot",
        "device": "dev-a",
        "ts": datetime.now(timezone.utc).isoformat(),
        "v": 1,
        "hosts": {},
        "token_sources": ["codex"],
        "active_days": [],
        "padding": "x" * 800,
    }
    capture = events_tail.HostUsageCapture({}, token_sources=("codex",))
    monkeypatch.setattr(events_tail, "_capture_host_snapshot", lambda *a, **kw: (capture, [host]))
    host_size = len(json.dumps(host, sort_keys=True).encode()) + 1
    row = _mm_events.make_mm_push_event(device="dev-a", mm_version="test")
    row["padding"] = "y" * 400
    activity_size = len(json.dumps(row, sort_keys=True).encode()) + 1
    if stage == "activity":
        # A deterministic real activity batch: each batch fits by itself, their
        # sum does not. Exercise the shared writer at BOTH call sites.
        limit = base + max(host_size, activity_size)
        assert base + host_size <= limit and base + activity_size <= limit
    else:
        # The host append cannot consume the space the smaller activity batch
        # still fits in. Capture failure must leave activity enabled.
        limit = base + activity_size
        assert base + host_size > limit

    def tail(config, sources, device, **kw):
        assert kw["capture_activity"]
        result = _mm_events.write_push_event(
            path.parent, device, [row], max_file_size=config["sync"]["max_file_size"]
        )
        if stage == "activity":
            assert isinstance(result, _mm_events.EventAppendSkipped)
            return ["event append skipped (max-file-size)"]
        assert result is None  # forensic append success
        return []

    monkeypatch.setattr(events_tail, "_run_events_tail", tail)
    env["cfg"]["sync"]["max_file_size"] = limit
    save_config(env["cfg"], env["path"])
    (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "(max-file-size)" in result.stderr
    assert path.stat().st_size <= limit
    assert (
        "projects/-Users-kb-myapp/memory/new.md"
        in cli_module.sidecar.read("dev-a")["sources"]["claude"]["files"]
    )
    monkeypatch.setattr(events_tail, "_run_events_tail", lambda *a, **kw: [])
    again = runner.invoke(app, ["push"])
    assert again.exit_code == 0, again.output
    assert path.stat().st_size <= limit


def test_already_oversized_advertised_day_still_refuses(capture61):
    env = capture61
    env["cfg"]["sync"]["max_file_size"] = env["dayfile"].stat().st_size - 1
    save_config(env["cfg"], env["path"])
    before = cli_module.sidecar.read("dev-a")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 1, result.output
    assert "sync.max_file_size" in result.stderr
    assert "snapshot" in result.stderr.lower()
    assert cli_module.sidecar.read("dev-a") == before


@pytest.mark.parametrize("emitter", ["push", "init", "recapture"])
def test_event_size_limit_reaches_every_emitter(capture61, monkeypatch, emitter):
    env = capture61
    original = env["dayfile"].read_bytes()
    env["cfg"]["sync"]["max_file_size"] = len(original)
    save_config(env["cfg"], env["path"])
    observed = []
    real = _mm_events.write_push_event

    def write(*a, **kw):
        assert kw["max_file_size"] == len(original)
        outcome = real(*a, **kw)
        observed.append(outcome)
        return outcome

    monkeypatch.setattr(_mm_events, "write_push_event", write)
    if emitter == "init":
        sources = config_module.resolve_sources(env["cfg"]).available
        events_tail._run_events_backfill(env["cfg"], sources, "dev-a")
    elif emitter == "recapture":
        monkeypatch.setattr(
            _mm_events,
            "discover_git_roots",
            lambda *a, **kw: _mm_events.GitRootDiscovery([env["claude"]], [], False),
        )
        result = runner.invoke(app, ["recapture", "1d"])
        assert result.exit_code == 1 and "not written" in result.stderr
    else:
        (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.output
        assert len(observed) == 2  # attended host append and ordinary activity tail
    assert observed and all(isinstance(r, _mm_events.EventAppendSkipped) for r in observed)
    assert env["dayfile"].read_bytes() == original


def test_legacy_grok_recovery_finalizes_readers_before_capture(capture61, monkeypatch, tmp_path):
    env = capture61
    root = tmp_path / "legacy-grok"
    root.mkdir()
    grok = {"name": "grok", "path": str(root), "type": "grok"}
    prior = cli_module.sidecar.read("dev-a")
    prior["sources"]["grok"] = {"base_path": str(root), "files": {}}
    env["backend"].put(
        cli_module.manifest_key("dev-a"), encrypt(serialize_manifest(prior), PASSPHRASE, MEMORY_KB)
    )
    real_resolution = cli_module.resolve_sources(env["cfg"])
    monkeypatch.setattr(
        cli_module,
        "resolve_sources",
        lambda *a, **kw: cli_module.replace(real_resolution, explicit=False),
    )
    get_default = cli_module.get_default_source
    monkeypatch.setattr(
        cli_module, "get_default_source", lambda name: grok if name == "grok" else get_default(name)
    )
    readers = []

    def read(**kw):
        readers.append("grok")
        return _mm_host_usage.HostUsageResult({}, complete=True)

    monkeypatch.setattr(_mm_host_usage, "read_grok_usage", read)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert readers == ["grok"]
    rows = [json.loads(line) for line in env["dayfile"].read_text().splitlines()]
    latest = [r for r in rows if r["type"] == "host-usage-snapshot"][-1]
    assert latest["token_sources"] == ["codex", "grok"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("failure", ["oserror", "mindmeld"])
def test_push_stop_reports_failure_and_nudges_only_after_a_real_attempt(
    capture61, monkeypatch, dry_run, failure
):
    """OSError and non-snapshot MindMeldError both stop push; only a real attempt nudges."""
    from mind_meld.errors import StorageError

    error = OSError("disk unplugged") if failure == "oserror" else StorageError("storage refused")

    def stop(*args, **kwargs):
        raise error

    nudges = []

    def nudge(config, *, attended, diagnostics=True):
        cli_module.acquire_lock()  # proves the lock was released first
        cli_module.release_lock()
        assert attended
        nudges.append(config["device"]["id"])

    monkeypatch.setattr(cli_module, "_push_core", stop)
    monkeypatch.setattr(cli_module.upgrade, "update_or_nudge", nudge)
    result = runner.invoke(app, ["push", *(["--dry-run"] if dry_run else [])])
    assert result.exit_code == 1, result.output
    flat = " ".join(result.stderr.split())
    assert str(error) in flat
    if dry_run:
        assert "(push-failed)" not in flat
        assert flat.endswith("Nothing was changed except the local lock file (dry run).")
        assert nudges == []
    else:
        assert "Push failed [storage-error]" in flat
        assert "effects unconfirmed" in flat
        assert flat.endswith("Run: mm diag")
        assert nudges == ["dev-a"]


@pytest.mark.parametrize("outcome", ["deleted", "oserror", "mindmeld"])
def test_post_push_gc_outcomes_never_undo_accepted_content(capture61, monkeypatch, outcome):
    from mind_meld.errors import MindMeldError

    (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")

    def gc(*args, **kwargs):
        if outcome == "oserror":
            raise OSError("gc scan failed")
        if outcome == "mindmeld":
            raise MindMeldError("gc refused")
        return 3

    monkeypatch.setattr(cli_module, "_do_gc", gc)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    if outcome == "deleted":
        assert "GC: deleted 3 orphaned blobs." in result.stdout
        assert "GC skipped" not in result.output
    else:
        cause = "gc scan failed" if outcome == "oserror" else "gc refused"
        flat = " ".join(result.stderr.split())
        assert f"mm: warning: GC skipped ({cause}). Run mm gc for details." in flat
        assert "GC skipped" not in result.stdout
        assert "GC: deleted" not in result.output
    sync = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert sync["counts"]["maintenance_failures"] == (0 if outcome == "deleted" else 1)
    assert sync["content_accepted"] is True
    accepted = cli_module.sidecar.read("dev-a")
    assert "projects/-Users-kb-myapp/memory/new.md" in accepted["sources"]["claude"]["files"]


def test_publication_evidence_failure_cannot_undo_content_acceptance(capture61, monkeypatch):
    (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")

    def broken(*args, **kwargs):
        raise RecursionError("evidence broke")

    monkeypatch.setattr(_mm_events, "recorded_row_revision", broken)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.stderr.split())
    assert "(unverified: evidence-error)" in flat
    assert "publication evidence could not be read" in flat
    assert "RecursionError" in flat and "evidence broke" not in flat
    assert "Content sync succeeded" in flat
    assert "not-published:" not in result.stdout
    accepted = cli_module.sidecar.read("dev-a")
    assert "projects/-Users-kb-myapp/memory/new.md" in accepted["sources"]["claude"]["files"]
    attempt, error = cli_module.attemptlog.read()
    assert error is None
    assert (attempt["class"], attempt["cause"]) == ("unverified", "evidence-error")


@pytest.mark.parametrize(
    "mode",
    ["published", "capture-failed", "append-failed", "max-file-size", "prerequisites", "no-row"],
)
@pytest.mark.parametrize("content_change", [False, True])
def test_attended_attempt_records_each_capture_outcome(
    capture61, monkeypatch, mode, content_change
):
    monkeypatch.setattr(
        _mm_events, "discover_git_roots", lambda *a, **k: _mm_events.GitRootDiscovery((), ())
    )
    monkeypatch.setattr(
        _mm_events,
        "resolve_push_cursor",
        lambda *a, **k: _mm_events.CursorResolution(
            since=datetime(2026, 1, 1, tzinfo=timezone.utc), held=True
        ),
    )
    if content_change:
        (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")

    def fail(*a, **kw):
        raise OSError("capture test failure")

    if mode == "capture-failed":
        monkeypatch.setattr(events_tail, "_capture_host_snapshot", fail)
    elif mode == "append-failed":
        monkeypatch.setattr(_mm_events, "write_push_event", fail)
    elif mode == "max-file-size":
        real = _mm_events.write_push_event

        def skipped(root, device, rows, **kw):
            if kw.get("strict"):
                return _mm_events.EventAppendSkipped(capture61["dayfile"])
            return real(root, device, rows, **kw)

        monkeypatch.setattr(_mm_events, "write_push_event", skipped)
    elif mode == "prerequisites":
        monkeypatch.setattr(events_tail, "_default_host_readers", lambda *a, **kw: ())
    elif mode == "no-row":
        monkeypatch.setattr(
            _mm_host_usage,
            "read_codex_usage",
            lambda **kw: _mm_host_usage.HostUsageResult({}, False, "unsupported"),
        )
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    record, reason = cli_module.attemptlog.read()
    assert reason is None and record["class"] == mode
    assert record["cause"] == ("no-reader" if mode == "prerequisites" else None)
    assert (record["row_ts"] is not None) == (mode == "published")
    sync = cli_module.attemptlog.read_runs()["runs"]["push"]
    capture_degraded = mode not in ("published", "prerequisites")
    # Explicitly hold the content tail's retro cursor independently of capture.
    # That degradation must survive successful host capture, without depending
    # on discovery timing during this fixture's initial push.
    degraded = capture_degraded or content_change
    assert sync["outcome"] == ("degraded" if degraded else "success")
    expected_cause = "no-changes" if mode == "prerequisites" and not content_change else "ok"
    assert sync["cause"] == ("result-degraded" if degraded else expected_cause)
    assert sync["content_accepted"] is (content_change or mode == "published")
    if capture_degraded:
        assert any("usage capture" in detail for detail in sync["degradations"])
    elif content_change:
        assert any(detail.startswith("retro cursor held") for detail in sync["degradations"])
    assert str(capture61["path"].parent) not in json.dumps(sync)


@pytest.mark.parametrize("stage", ["pre-rename", "post-rename", "mkdir"])
def test_attempt_write_failure_keeps_push_exit_and_releases_lock(capture61, monkeypatch, stage):
    log = cli_module.attemptlog
    before = log.record_path().read_bytes()
    real_write = log.write
    real_replace = os.replace
    real_fsync = cli_module.fsutil.fsync_dir
    real_mkdir = Path.mkdir

    def fail_replace(src, dst):
        if Path(dst) == log.record_path():
            raise OSError("ENOSPC before rename")
        return real_replace(src, dst)

    def fail_fsync(path):
        if Path(path) == log.record_path().parent:
            raise OSError("EIO after rename")
        return real_fsync(path)

    def fail_mkdir(path, *a, **kw):
        if path == log.record_path().parent:
            raise OSError("mkdir denied")
        return real_mkdir(path, *a, **kw)

    def write(outcome):
        # Fail only inside this record write, never the publishing sidecar.
        with monkeypatch.context() as patch:
            if stage == "pre-rename":
                patch.setattr(os, "replace", fail_replace)
            elif stage == "post-rename":
                patch.setattr(cli_module.fsutil, "fsync_dir", fail_fsync)
            else:
                patch.setattr(Path, "mkdir", fail_mkdir)
            real_write(outcome)

    monkeypatch.setattr(log, "write", write)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "could not durably record this attended capture attempt" in " ".join(
        result.stderr.split()
    )
    assert "check mm status" in " ".join(result.stderr.split())
    assert (log.record_path().read_bytes() == before) == (stage != "post-rename")
    cli_module.acquire_lock()
    cli_module.release_lock()


def test_dry_run_and_autopush_do_not_write_attended_record(capture61, monkeypatch):
    path = cli_module.attemptlog.record_path()
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(cli_module.attemptlog, "write", lambda *a: pytest.fail("wrote attempt"))
    for command in (["push", "--dry-run"], ["autopush"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("read_state", ["missing", "corrupt", "unreadable", "future"])
def test_attempt_status_diag_read_states_are_write_free(capture61, monkeypatch, read_state):
    from datetime import timedelta

    log = cli_module.attemptlog
    path = log.record_path()
    if read_state == "missing":
        path.unlink()
    elif read_state == "corrupt":
        path.write_text("{torn")
    elif read_state == "future":
        record = json.loads(path.read_text())
        record["attempted_at"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        path.write_text(json.dumps(record))
    original = Path.open
    if read_state == "unreadable":

        def denied(target, *args, **kwargs):
            if target == path:
                raise PermissionError("denied")
            return original(target, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)

    def identity():
        if not path.exists():
            return None
        stat = path.stat()
        # Ordinary reads can advance atime; only write-relevant identity counts.
        return _mm_events._event_file_identity(stat), stat.st_mode

    before = identity()
    monkeypatch.setattr(log, "write", lambda *a: pytest.fail("inspection wrote attempt"))
    for command in (["status"], ["diag"], ["diag", "--json"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        if "--json" in command:
            state = json.loads(result.stdout)["host_publication"]
            assert state["latest_attempt"] == ("published" if read_state == "future" else "unknown")
            assert state["latest_attempt_reason"] == (
                None if read_state == "future" else read_state
            )
        else:
            assert {
                "missing": "no attended attempt recorded yet",
                "corrupt": "record corrupt",
                "unreadable": "record unreadable:",
                "future": "in the future",
            }[read_state] in " ".join(result.stdout.split())
    assert identity() == before


def test_verbose_capture_uses_the_trimmed_row_for_partial_and_empty_labels(capture61, monkeypatch):
    cfg = capture61["cfg"]
    cfg["retro"] = {"grok_host_usage": True}
    save_config(cfg, capture61["path"])
    today = datetime.now(timezone.utc).date().isoformat()
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    monkeypatch.setattr(
        _mm_host_usage,
        "read_codex_usage",
        lambda **kw: _mm_host_usage.HostUsageResult(
            {"other": {"2020-01-01": usage}}, True, partial_days=frozenset({"2020-01-01"})
        ),
    )
    monkeypatch.setattr(
        _mm_host_usage,
        "read_grok_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({"other": {today: usage}}, True),
    )
    real = _mm_events.make_host_usage_snapshot
    monkeypatch.setattr(_mm_events, "make_host_usage_snapshot", lambda **kw: real(**kw, max_days=1))
    result = runner.invoke(app, ["push", "-v"])
    assert result.exit_code == 0, result.output
    assert "Usage capture: codex — completed, no usage in snapshot" in result.stdout
    assert "Usage capture: grok — contributed" in result.stdout
    assert "partial" not in result.stderr
    record, reason = cli_module.attemptlog.read()
    assert reason is None and record["readers"] == {"codex": "empty", "grok": "contributed"}


@pytest.mark.parametrize("trigger", ["recovery", "selection"])
def test_no_row_prerequisites_keep_activity_walk_and_cursor(capture61, monkeypatch, trigger):
    env = capture61
    old_cursor = _mm_events.resolve_push_cursor(env["dayfile"].parent, "dev-a")
    # No user-content diff. Only internal recovery/selection triggers the tail.
    monkeypatch.setattr(events_tail, "_default_host_readers", lambda *a, **kw: ())
    if trigger == "recovery":
        env["backend"].put(cli_module.manifest_key("dev-a"), b"corrupt")
    else:
        prior = cli_module.sidecar.read("dev-a")
        prior["sources"].pop("mm-events")
        env["backend"].put(
            cli_module.manifest_key("dev-a"),
            encrypt(serialize_manifest(prior), PASSPHRASE, MEMORY_KB),
        )
    calls = []
    real_git = _mm_events.walk_git_projects
    real_sessions = _mm_events.walk_session_metadata

    def git(*args, **kwargs):
        calls.append("git")
        return real_git(*args, **kwargs)

    def sessions(*args, **kwargs):
        calls.append("sessions")
        return real_sessions(*args, **kwargs)

    monkeypatch.setattr(_mm_events, "walk_git_projects", git)
    monkeypatch.setattr(_mm_events, "walk_session_metadata", sessions)
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **kw: _mm_events.GitRootDiscovery([env["claude"]], [], False),
    )
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert set(calls) == {"git", "sessions"}
    assert _mm_events.resolve_push_cursor(env["dayfile"].parent, "dev-a").since > old_cursor.since
    record, reason = cli_module.attemptlog.read()
    assert reason is None
    assert (record["class"], record["cause"]) == ("prerequisites", "no-reader")


def test_attended_push_deadline_after_warm_gets_an_honest_diagnostic(capture61, monkeypatch):
    """The warm already spent the attended allowance; do not promise it again."""
    exhausted = _mm_host_usage.HostUsageResult({}, complete=False, reason="deadline")
    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", lambda **kw: exhausted)
    monkeypatch.setattr(_mm_host_usage, "warm_host_cache_inline", lambda **kw: exhausted)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.stderr.split())
    assert "(no-row)" in flat and "(codex deadline)" in flat
    assert "Attended warming also exhausted its allowance" in flat
    assert "the next attended mm push retries automatically" in flat
    assert "can warm cold readers" not in flat
    assert "without that budget" not in flat


def test_push_receipt_counts_only_user_content_and_reports_verbose_publication(
    capture61, monkeypatch
):
    """content_files counts user byte changes; mm-events rewrites are internal."""
    env = capture61
    memory = env["claude"] / "projects/-Users-kb-myapp/memory"
    (memory / "old.md").write_text("old")
    assert runner.invoke(app, ["push"]).exit_code == 0
    (memory / "old.md").unlink()
    (memory / "new.md").write_text("new")
    (memory / "user_role.md").write_text("edited role")
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **kw: _mm_events.GitRootDiscovery([], [], False),
    )
    receipts = []
    real_core = cli_module._push_core

    def core(*a, **kw):
        receipts.append(real_core(*a, **kw))
        return receipts[-1]

    monkeypatch.setattr(cli_module, "_push_core", core)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    [receipt] = receipts
    assert receipt.content_changed and receipt.content_accepted and receipt.host_usage_published
    # One new, one modified and one deleted user file. The day file also changed
    # (host row plus activity rows) but is mm-owned, so it never counts here.
    assert receipt.content_files == 3
    assert receipt.total_new + receipt.total_modified + receipt.total_deleted > 3
    assert "Host usage published (manifest accepted)." in result.stdout
    receipts.clear()
    refresh = runner.invoke(app, ["push"])
    assert refresh.exit_code == 0, refresh.output
    [usage_only] = receipts
    assert (usage_only.content_changed, usage_only.content_files) == (False, 0)
    assert usage_only.content_accepted and usage_only.host_usage_published
    assert "Host usage published" not in refresh.stdout


@pytest.mark.parametrize("case", ["capture-failed", "readers"])
def test_capture_warning_token_names_the_phase_or_the_partial_row(capture61, monkeypatch, case):
    """`no-row` means nothing was written; these two mean something else happened."""
    env = capture61
    before = env["dayfile"].read_text().splitlines()
    if case == "capture-failed":

        def broken(*args, **kwargs):
            raise RuntimeError("snapshot construction broke")

        monkeypatch.setattr(events_tail, "_capture_host_snapshot", broken)
    else:
        env["cfg"]["retro"] = {"grok_host_usage": True}
        save_config(env["cfg"], env["path"])
        exhausted = _mm_host_usage.HostUsageResult({}, complete=False, reason="deadline")
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", lambda **kw: exhausted)
        monkeypatch.setattr(_mm_host_usage, "warm_host_cache_inline", lambda **kw: exhausted)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    sync = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert (sync["outcome"], sync["cause"]) == ("degraded", "result-degraded")
    assert sync["content_accepted"] is (case == "readers")
    if case == "readers":
        assert "usage capture published (incomplete reader coverage)" in sync["degradations"]
    flat = " ".join(result.stderr.split())
    assert "Content sync continues" in flat and "#host-usage-capture" in flat
    assert "(no-row)" not in flat and "(append-failed)" not in flat
    added = [json.loads(line) for line in env["dayfile"].read_text().splitlines()[len(before) :]]
    hosts = [row for row in added if row["type"] == "host-usage-snapshot"]
    if case == "capture-failed":
        assert "Usage capture (capture-failed): RuntimeError." in flat
        assert "snapshot construction broke" not in flat
        assert "Repair the local events folder if needed and run mm diag" in flat
        assert "the next attended mm push retries automatically" in flat
        assert hosts == []
    else:
        # Codex still contributes, so a row exists and is published; only grok is declared.
        assert "Usage capture (readers): host-usage snapshot skipped (grok deadline)" in flat
        assert "Attended warming also exhausted its allowance" in flat
        assert len(hosts) == 1
        assert hosts[0]["token_sources"] == ["codex"]
        assert hosts[0]["degraded_sources"] == ["grok"]
        published = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_publication"]
        assert published["publication"] == "published"
        assert published["readers"] == {"codex": "contributed", "grok": "degraded"}


def test_capture_warning_names_the_exception_when_its_message_is_empty(capture61, monkeypatch):
    """AssertionError() and StopIteration() stringify to nothing; the warning must not."""

    def broken(*args, **kwargs):
        raise AssertionError()

    monkeypatch.setattr(events_tail, "_capture_host_snapshot", broken)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "Usage capture (capture-failed): AssertionError." in " ".join(result.stderr.split())
    # The capture receipt owns this loss; it is not also an events-tail degradation.
    run = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert run["counts"].get("events_degradations", 0) == 0
    assert run["omitted_details"] == 0


def test_interrupt_during_capture_releases_the_lock_and_pushes_nothing(capture61, monkeypatch):
    """Capture now precedes content sync and can scan for seconds; Ctrl-C must be clean."""
    env = capture61
    real = _mm_host_usage.read_codex_usage
    before = env["dayfile"].read_bytes()
    (env["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")

    def interrupt(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", interrupt)
    aborted = runner.invoke(app, ["push"])
    assert aborted.exit_code != 0
    record, reason = cli_module.attemptlog.read()
    assert reason is None and record["class"] == "push-failed"
    assert record["cause"] == "interrupted"
    assert record["row_ts"] is None
    assert env["dayfile"].read_bytes() == before
    rel = "projects/-Users-kb-myapp/memory/new.md"
    assert rel not in cli_module.sidecar.read("dev-a")["sources"]["claude"]["files"]
    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", real)
    retry = runner.invoke(app, ["push"])
    assert retry.exit_code == 0, retry.output
    assert "holds the lock" not in retry.output
    assert rel in cli_module.sidecar.read("dev-a")["sources"]["claude"]["files"]


@pytest.mark.parametrize(
    "failure,cause,number",
    [
        ("interrupt", "interrupted", None),
        ("abort", "interrupted", None),
        ("os", "storage-error", "ENOSPC"),
        ("wrapped-os", "storage-error", "EIO"),
        ("storage", "storage-error", None),
        ("snapshot", "snapshot-refused", None),
        ("mindmeld", "error", None),
    ],
)
def test_upload_stop_cause_is_private_and_visible_in_status_diag(
    capture61, monkeypatch, failure, cause, number
):
    from mind_meld.errors import MindMeldError, SnapshotError, StorageError

    original = LocalBackend.put
    marker = "/private/sensitive-source-path"
    before = capture61["backend"].get(storage_keys.manifest_key("dev-a"))
    (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")

    def stop(backend, key, data):
        if key.startswith("data/"):
            if failure == "interrupt":
                raise KeyboardInterrupt()
            if failure == "abort":
                raise typer.Abort()
            if failure == "os":
                raise OSError(errno.ENOSPC, "sensitive storage message", marker)
            if failure == "wrapped-os":
                try:
                    raise OSError(errno.EIO, "sensitive storage message", marker)
                except OSError as exc:
                    raise StorageError(marker) from exc
            if failure == "storage":
                raise StorageError(marker)
            if failure == "snapshot":
                raise SnapshotError(marker)
            raise MindMeldError(marker)
        return original(backend, key, data)

    monkeypatch.setattr(LocalBackend, "put", stop)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 1, result.output
    record, reason = cli_module.attemptlog.read()
    assert reason is None
    assert (record["class"], record["cause"], record["errno"]) == ("push-failed", cause, number)
    assert record["row_ts"] is not None
    raw = cli_module.attemptlog.record_path().read_text()
    assert marker not in raw and "sensitive storage message" not in raw
    assert capture61["backend"].get(storage_keys.manifest_key("dev-a")) == before
    cli_module.acquire_lock()
    cli_module.release_lock()
    for command in (["status"], ["diag"]):
        shown = runner.invoke(app, command)
        assert shown.exit_code == 0, shown.output
        expected = f"push-failed ({cause}{': ' + number if number else ''})"
        assert expected in " ".join(shown.stdout.split())
    shown = runner.invoke(app, ["diag", "--json"])
    assert shown.exit_code == 0, shown.output
    publication = json.loads(shown.stdout)["host_publication"]
    assert publication["latest_attempt_cause"] == cause
    assert publication["latest_attempt_errno"] == number


def test_nested_checkout_freezes_published_entries_and_converges_on_delete(capture61, monkeypatch):
    root = capture61["path"].parent / "gstack"
    checkout = root / "projects" / "literal*" / "checkout"
    checkout.mkdir(parents=True)
    published_file = checkout / "note.md"
    published_file.write_text("published before this folder became a checkout")
    (checkout / "linked.md").write_text("becomes a symlink inside the checkout")
    sibling = root / "projects" / "other" / "note.md"
    sibling.parent.mkdir()
    sibling.write_text("ordinary content")
    src = {
        "name": "gstack",
        "type": "generic",
        "path": str(root),
        "include_dirs": ["projects", "projects/literal*/checkout"],
        "include_files": ["projects/literal*/checkout/note.md"],
    }
    capture61["cfg"]["sync"]["sources"].append(src)
    save_config(capture61["cfg"], capture61["path"])
    first = runner.invoke(app, ["push"])
    assert first.exit_code == 0, first.output
    prior = cli_module.sidecar.read("dev-a")
    rel = "projects/literal*/checkout/note.md"
    published = prior["sources"]["gstack"]["files"][rel]

    def accepted() -> dict:
        key = storage_keys.manifest_key("dev-a")
        return load_manifest(decrypt(capture61["backend"].get(key), PASSPHRASE, MEMORY_KB))

    linked = "projects/literal*/checkout/linked.md"
    (checkout / ".git").write_text("gitdir: elsewhere")
    published_file.write_text("edited inside the checkout")
    (checkout / "new.md").write_text("never published")
    (checkout / "linked.md").unlink()
    (checkout / "linked.md").symlink_to(published_file)
    expected = "skipped: nested git repository gstack:projects/literal*/checkout"
    # Autopush stays silent; attended push and its preview report the root once.
    for command, count in ((["push", "--dry-run"], 1), (["push", "-v"], 1), (["autopush"], 0)):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        assert " ".join(result.stderr.split()).count(expected) == count
        assert "still present but was omitted" not in result.output
    # An ordinary change forces a manifest upload; frozen entries ride along unchanged.
    (root / "projects" / "other" / "fresh.md").write_text("ordinary change")
    assert runner.invoke(app, ["push"]).exit_code == 0
    files = accepted()["sources"]["gstack"]["files"]
    assert set(files) == {"projects/other/note.md", "projects/other/fresh.md", rel, linked}
    assert files[rel]["sha256"] == published["sha256"]
    assert files[linked]["sha256"] == prior["sources"]["gstack"]["files"][linked]["sha256"]
    assert f"gstack:{rel}" not in accepted()["tombstones"]
    shown = runner.invoke(app, ["diff"])
    assert shown.exit_code == 0, shown.output
    assert "checkout" not in shown.stdout
    assert published_file.read_text() == "edited inside the checkout"
    for command in (["status"], ["diag"]):
        shown = runner.invoke(app, command)
        assert shown.exit_code == 0, shown.output
        if command == ["status"]:
            assert "Skipped nested repositories: 1. Details: mm diag" in shown.stdout
            assert "projects/literal*/checkout" not in shown.stdout
            assert "deleted" not in shown.stdout
        else:
            assert " ".join(shown.stdout.split()).count(expected) == 1
    shown = runner.invoke(app, ["diag", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["sync_scope"]["skipped_nested_repositories"]["gstack"] == [
        "projects/literal*/checkout"
    ]
    # Deleting the checkout tombstones the frozen entry, like any deletion.
    shutil.rmtree(checkout)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert rel not in accepted()["sources"]["gstack"]["files"]
    assert {f"gstack:{rel}", f"gstack:{linked}"} <= set(accepted()["tombstones"])


def test_corrupt_manifest_recovery_keeps_frozen_entries_that_have_blobs(capture61):
    root = capture61["path"].parent / "gstack"
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "note.md").write_text("published")
    src = {"name": "gstack", "type": "generic", "path": str(root), "include_dirs": ["proj"]}
    capture61["cfg"]["sync"]["sources"].append(src)
    save_config(capture61["cfg"], capture61["path"])
    assert runner.invoke(app, ["push"]).exit_code == 0
    key = storage_keys.manifest_key("dev-a")
    published = load_manifest(decrypt(capture61["backend"].get(key), PASSPHRASE, MEMORY_KB))
    (root / "proj" / ".git").mkdir()
    (root / "proj" / "note.md").write_text("edited inside the checkout")
    capture61["backend"].put(key, b"not a manifest")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "still present but was omitted" not in " ".join(result.output.split())
    healed = load_manifest(decrypt(capture61["backend"].get(key), PASSPHRASE, MEMORY_KB))
    files = healed["sources"]["gstack"]["files"]
    # Recovery freezes the stored entry and never re-reads the edited file.
    assert files["proj/note.md"] == published["sources"]["gstack"]["files"]["proj/note.md"]
    assert "gstack:proj/note.md" not in healed["tombstones"]
    shutil.rmtree(root / "proj")
    assert runner.invoke(app, ["push"]).exit_code == 0
    healed = load_manifest(decrypt(capture61["backend"].get(key), PASSPHRASE, MEMORY_KB))
    assert "gstack:proj/note.md" in healed["tombstones"]


def test_corrupt_manifest_recovery_excludes_checkout_entries_without_blobs(capture61):
    root = capture61["path"].parent / "gstack"
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "note.md").write_text("published")
    src = {"name": "gstack", "type": "generic", "path": str(root), "include_dirs": ["proj"]}
    capture61["cfg"]["sync"]["sources"].append(src)
    save_config(capture61["cfg"], capture61["path"])
    assert runner.invoke(app, ["push"]).exit_code == 0
    key = storage_keys.manifest_key("dev-a")
    published = load_manifest(decrypt(capture61["backend"].get(key), PASSPHRASE, MEMORY_KB))
    sha = published["sources"]["gstack"]["files"]["proj/note.md"]["sha256"]
    capture61["backend"].delete(storage_keys.blob_key("dev-a", sha))
    (root / "proj" / ".git").mkdir()
    capture61["backend"].put(key, b"not a manifest")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "still present but was omitted" not in " ".join(result.output.split())
    healed = load_manifest(decrypt(capture61["backend"].get(key), PASSPHRASE, MEMORY_KB))
    assert "proj/note.md" not in healed["sources"]["gstack"]["files"]
    assert "gstack:proj/note.md" not in healed["tombstones"]


def test_recovery_keeps_frozen_mm_events_entries_across_the_rescan(capture61):
    events = capture61["path"].parent / "events_root" / "events" / "nested"
    events.mkdir(parents=True)
    (events / "old.md").write_text("published before the checkout")
    assert runner.invoke(app, ["push"]).exit_code == 0
    (events / ".git").mkdir()
    key = storage_keys.manifest_key("dev-a")
    capture61["backend"].put(key, b"not a manifest")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    healed = load_manifest(decrypt(capture61["backend"].get(key), PASSPHRASE, MEMORY_KB))
    assert "events/nested/old.md" in healed["sources"]["mm-events"]["files"]
    assert "mm-events:events/nested/old.md" not in healed["tombstones"]


def test_tombstoned_checkout_entry_still_on_disk_does_not_wedge_push(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "a.md").write_text("recreated inside the checkout")
    (tmp_path / "repo" / "b.md").write_text("published")
    info = {"sha256": "0" * 64, "size": 1, "mtime": "2026-10-07T00:00:00+00:00"}
    src = {"name": "g", "type": "generic", "path": str(tmp_path), "include_dirs": ["repo"]}
    tombstone = {"deleted_at": datetime.now(timezone.utc).isoformat(), "device_id": "dev-a"}
    prior = {
        "sources": {"g": {"files": {"repo/a.md": info, "repo/b.md": info}}},
        "tombstones": {"g:repo/a.md": tombstone},
    }
    roots: dict[str, set[str]] = {}
    local = cli_module.build_manifest_v2("dev-a", "A", [src], strict=True, nested_roots=roots)
    frozen = cli_module._freeze_nested_checkout_entries(local, prior, roots)
    prior = cli_module._drop_unfrozen_checkout_files(prior, roots, frozen)
    cli_module._prove_omitted_paths_absent(local, prior, [src], max_file_size=52_428_800)
    tombstones = cli_module.generate_tombstones(local, prior, "dev-a")
    assert set(local["sources"]["g"]["files"]) == {"repo/b.md"}
    assert set(tombstones) == {"g:repo/a.md"}


def _gstack_source(capture61, **extra) -> tuple[Path, dict]:
    root = capture61["path"].parent / "gstack"
    root.mkdir(exist_ok=True)
    src = {"name": "gstack", "type": "generic", "path": str(root), **extra}
    capture61["cfg"]["sync"]["sources"].append(src)
    save_config(capture61["cfg"], capture61["path"])
    return root, src


def _accepted(capture61) -> dict:
    key = storage_keys.manifest_key("dev-a")
    return load_manifest(decrypt(capture61["backend"].get(key), PASSPHRASE, MEMORY_KB))


def test_linked_explicit_include_inside_a_checkout_still_freezes(capture61):
    root, _src = _gstack_source(capture61, include_files=["repo/note.md"])
    (root / "repo").mkdir()
    (root / "repo" / "note.md").write_text("published")
    (root / "target.md").write_text("elsewhere")
    assert runner.invoke(app, ["push"]).exit_code == 0
    (root / "repo" / ".git").mkdir()
    (root / "repo" / "note.md").unlink()
    (root / "repo" / "note.md").symlink_to(root / "target.md")
    assert runner.invoke(app, ["push"]).exit_code == 0
    assert "repo/note.md" in _accepted(capture61)["sources"]["gstack"]["files"]
    shutil.rmtree(root / "repo")
    assert runner.invoke(app, ["push"]).exit_code == 0
    assert "gstack:repo/note.md" in _accepted(capture61)["tombstones"]


def test_frozen_checkout_contents_are_never_probed(capture61):
    root, _src = _gstack_source(capture61, include_dirs=["repo"])
    (root / "repo" / "locked").mkdir(parents=True)
    (root / "repo" / "locked" / "f.md").write_text("published")
    (root / "repo" / "gone.md").write_text("published then deleted")
    assert runner.invoke(app, ["push"]).exit_code == 0
    (root / "repo" / "gone.md").unlink()
    assert runner.invoke(app, ["push"]).exit_code == 0
    assert "gstack:repo/gone.md" in _accepted(capture61)["tombstones"]
    (root / "repo" / ".git").mkdir()
    (root / "repo" / "gone.md").symlink_to(root / "repo" / "locked")
    (root / "repo" / "locked").chmod(0)
    try:
        # An unrelated change forces a manifest upload.
        (capture61["claude"] / "projects/-Users-kb-myapp/memory/x.md").write_text("change")
        result = runner.invoke(app, ["push"])
    finally:
        (root / "repo" / "locked").chmod(0o700)
    assert result.exit_code == 0, result.output
    accepted = _accepted(capture61)
    assert "repo/locked/f.md" in accepted["sources"]["gstack"]["files"]
    assert "gstack:repo/gone.md" in accepted["tombstones"]


def test_exclude_glob_retires_frozen_entries_without_tombstones(capture61):
    root, src = _gstack_source(capture61, include_dirs=["proj"])
    (root / "proj").mkdir()
    (root / "proj" / "note.md").write_text("published")
    assert runner.invoke(app, ["push"]).exit_code == 0
    (root / "proj" / ".git").mkdir()
    assert runner.invoke(app, ["push"]).exit_code == 0
    assert "proj/note.md" in _accepted(capture61)["sources"]["gstack"]["files"]
    src["exclude_patterns"] = ["proj/*"]
    save_config(capture61["cfg"], capture61["path"])
    status = runner.invoke(app, ["status", "--source", "gstack"])
    assert status.exit_code == 0, status.output
    block = status.stdout.split("Source 'gstack':", 1)[1].split("\n\n", 1)[0]
    assert "- 1 deleted" in block
    assert runner.invoke(app, ["push"]).exit_code == 0
    accepted = _accepted(capture61)
    assert "proj/note.md" not in accepted["sources"]["gstack"]["files"]
    assert "gstack:proj/note.md" not in accepted["tombstones"]


def test_status_agrees_with_push_for_a_checkout_below_a_marker_dir(capture61):
    root, _src = _gstack_source(capture61, include_dirs=["proj", "proj/r/c"])
    (root / "proj" / "r" / "c").mkdir(parents=True)
    (root / "proj" / "r" / "c" / "note.md").write_text("published")
    assert runner.invoke(app, ["push"]).exit_code == 0
    (root / "proj" / "r" / "c" / ".git").mkdir()
    assert runner.invoke(app, ["push"]).exit_code == 0
    assert "proj/r/c/note.md" in _accepted(capture61)["sources"]["gstack"]["files"]
    (root / "proj" / "r" / ".extend-root").write_text("generated")
    status = runner.invoke(app, ["status", "--source", "gstack"])
    assert status.exit_code == 0, status.output
    block = status.stdout.split("Source 'gstack':", 1)[1].split("\n\n", 1)[0]
    assert "- 1 deleted" in block
    assert runner.invoke(app, ["push"]).exit_code == 0
    accepted = _accepted(capture61)
    assert "proj/r/c/note.md" not in accepted["sources"]["gstack"]["files"]
    assert "gstack:proj/r/c/note.md" not in accepted["tombstones"]


def test_merged_conflict_copy_tombstone_inside_a_checkout_does_not_wedge_push(capture61):
    root, _src = _gstack_source(capture61, include_dirs=["proj"])
    (root / "proj").mkdir()
    (root / "proj" / "x.md").write_text("deleted later")
    (root / "proj" / "y.md").write_text("kept")
    assert runner.invoke(app, ["push"]).exit_code == 0
    key = storage_keys.manifest_key("dev-a")
    stale = capture61["backend"].get(key)
    (root / "proj" / "x.md").unlink()
    assert runner.invoke(app, ["push"]).exit_code == 0
    # An iCloud conflict copy carries the stale entry next to the tombstone.
    copy = capture61["backend"].root / key.replace("manifest.json.enc", "manifest.json 2.enc")
    copy.write_bytes(stale)
    (root / "proj" / ".git").mkdir()
    (root / "proj" / "x.md").write_text("recreated inside the checkout")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "still present but was omitted" not in " ".join(result.output.split())
    accepted = _accepted(capture61)
    assert "gstack:proj/x.md" in accepted["tombstones"]
    assert "proj/x.md" not in accepted["sources"]["gstack"]["files"]
    assert "proj/y.md" in accepted["sources"]["gstack"]["files"]


def test_mm_events_rescan_uploads_a_path_whose_checkout_vanished(capture61, monkeypatch):
    nested = capture61["path"].parent / "events_root" / "events" / "nested"
    nested.mkdir(parents=True)
    (nested / "old.md").write_text("published")
    assert runner.invoke(app, ["push"]).exit_code == 0
    (nested / ".git").mkdir()
    (capture61["claude"] / "projects/-Users-kb-myapp/memory/x.md").write_text("change")
    original = cli_module.build_manifest_v2

    def rescan(device_id, device_name, sources, *args, **kwargs):
        if [src["name"] for src in sources] == ["mm-events"]:
            # The checkout disappears between the first scan and the rescan.
            shutil.rmtree(nested / ".git")
            (nested / "old.md").write_text("changed after the first scan")
        return original(device_id, device_name, sources, *args, **kwargs)

    monkeypatch.setattr(cli_module, "build_manifest_v2", rescan)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    info = _accepted(capture61)["sources"]["mm-events"]["files"]["events/nested/old.md"]
    assert info["sha256"] == hashlib.sha256(b"changed after the first scan").hexdigest()
    assert capture61["backend"].exists(storage_keys.blob_key("dev-a", info["sha256"]))


def test_freeze_never_revives_a_tombstoned_path():
    info = {"sha256": "0" * 64, "size": 1, "mtime": "2026-10-07T00:00:00+00:00"}
    prior = {
        "sources": {"g": {"files": {"repo/kept.md": info, "repo/deleted.md": info}}},
        # A merged conflict copy can carry a file entry and its tombstone.
        "tombstones": {"g:repo/deleted.md": {"deleted_at": info["mtime"]}},
    }
    local = {"sources": {"g": {"files": {}}}}
    frozen = cli_module._freeze_nested_checkout_entries(local, prior, {"g": {"repo"}})
    assert frozen == {"g": {"repo/kept.md"}}
    assert list(local["sources"]["g"]["files"]) == ["repo/kept.md"]


def test_deselecting_an_include_inside_a_checkout_still_refuses_like_before(capture61):
    root = capture61["path"].parent / "gstack"
    notes = root / "checkout" / "notes"
    notes.mkdir(parents=True)
    (notes / "keep.md").write_text("still on disk")
    (root / "other").mkdir()
    (root / "other" / "a.md").write_text("ordinary")
    src = {"name": "gstack", "type": "generic", "path": str(root)}
    src["include_dirs"] = ["other", "checkout/notes"]
    capture61["cfg"]["sync"]["sources"].append(src)
    save_config(capture61["cfg"], capture61["path"])
    assert runner.invoke(app, ["push"]).exit_code == 0
    key = storage_keys.manifest_key("dev-a")
    before = capture61["backend"].get(key)
    (root / "checkout" / ".git").mkdir()
    src["include_dirs"] = ["other"]
    save_config(capture61["cfg"], capture61["path"])
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 1
    assert "still present but was omitted" in " ".join(result.output.split())
    assert capture61["backend"].get(key) == before


def test_interrupt_inside_storage_failure_handler_keeps_a_valid_record(capture61, monkeypatch):
    original_put = LocalBackend.put
    original_error = cli_module._error

    def put(backend, key, data):
        if key.startswith("data/"):
            raise OSError(errno.ENOSPC, "no space")
        return original_put(backend, key, data)

    def interrupted_error(message, *args, **kwargs):
        if "push-failed" in message:
            raise KeyboardInterrupt()
        return original_error(message, *args, **kwargs)

    monkeypatch.setattr(LocalBackend, "put", put)
    monkeypatch.setattr(cli_module, "_error", interrupted_error)
    (capture61["claude"] / "projects/-Users-kb-myapp/memory/new.md").write_text("pending")
    result = runner.invoke(app, ["push"])
    assert result.exit_code != 0
    record, reason = cli_module.attemptlog.read()
    assert reason is None
    assert (record["class"], record["cause"], record["errno"]) == (
        "push-failed",
        "interrupted",
        None,
    )


def test_peer_git_segments_are_stripped_before_they_can_mark_a_checkout():
    info = {"sha256": "0" * 64, "size": 1, "mtime": "2026-10-07T00:00:00+00:00"}
    peer = {
        "sources": {
            "claude": {
                "files": {
                    "projects/p/memory/sub/.git": info,
                    "projects/p/memory/sub/.git/config": info,
                    "projects/p/memory/ok.md": info,
                }
            }
        },
        "tombstones": {"claude:projects/p/memory/x/.git": {"deleted_at": info["mtime"]}},
    }
    out = cli_module._filter_excluded_paths(peer, {}, {})
    assert list(out["sources"]["claude"]["files"]) == ["projects/p/memory/ok.md"]
    assert out["tombstones"] == {}


@pytest.mark.parametrize(
    "rel",
    [
        "projects/p/memory/.Git",
        "projects/p/memory/.GIT/HEAD",
        "./repo/new.md",
        "repo/./new.md",
        "repo//new.md",
        "repo/new.md/",
    ],
)
def test_peer_case_variant_or_noncanonical_paths_are_stripped(rel):
    info = {"sha256": "0" * 64, "size": 1, "mtime": "2026-10-07T00:00:00+00:00"}
    peer = {"sources": {"claude": {"files": {rel: info}}}, "tombstones": {f"claude:{rel}": {}}}
    out = cli_module._filter_excluded_paths(peer, {}, {})
    assert out["sources"]["claude"]["files"] == {}
    assert out["tombstones"] == {}


def test_growth_warning_threshold_grouping_and_top_three(monkeypatch, capsys):
    from types import SimpleNamespace

    monkeypatch.setattr(cli_module, "_PUSH_GROWTH_WARNING_FILES", 2)

    def diff(*rels):
        return SimpleNamespace(new=list(rels))

    cli_module._warn_push_growth([("a", None, None, diff("x/y/1", "x/y/2"))])
    assert capsys.readouterr().err == ""
    cli_module._warn_push_growth(
        [
            ("a", None, None, diff("r1", "r2", "r3")),
            ("b", None, None, diff(*[f"p/q/{i}" for i in range(4)])),
            ("c", None, None, diff(*[f"p/q/{i}" for i in range(3)])),
            ("d", None, None, diff(*[f"p/q/{i}" for i in range(3)])),
        ]
    )
    err = " ".join(capsys.readouterr().err.split())
    assert "b:p/q (4 new files); a:. (3 new files); c:p/q (3 new files)" in err
    assert "d:p/q" not in err


def test_growth_warning_is_nonblocking_and_only_counts_new_files(capture61, monkeypatch):
    monkeypatch.setattr(cli_module, "_PUSH_GROWTH_WARNING_FILES", 2)
    memory = capture61["claude"] / "projects/-Users-kb-myapp/memory"
    for name in ("a.md", "b.md", "c.md"):
        (memory / name).write_text("new file")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "large sync growth: claude:projects/-Users-kb-myapp (3 new files)" in " ".join(
        result.stderr.split()
    )
    for name in ("a.md", "b.md", "c.md"):
        (memory / name).write_text("modified file")
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.output
    assert "large sync growth" not in result.stderr


def _two_macs(tmp_path, monkeypatch):
    helpers = TestPushPullRoundTrip()
    storage = tmp_path / "storage"
    claude_a = tmp_path / "machine_a" / ".claude"
    claude_b = tmp_path / "machine_b" / ".claude"
    helpers._populate_claude(claude_a)
    claude_b.mkdir(parents=True)
    backend = helpers._bootstrap(storage)
    register_device(backend, "dev-a", "A")
    register_device(backend, "dev-b", "B")
    configs = {
        "a": helpers._make_config(tmp_path, storage, claude_a, "dev-a", "A"),
        "b": helpers._make_config(tmp_path, storage, claude_b, "dev-b", "B"),
    }
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    def run(mac, *argv):
        helpers._activate(monkeypatch, configs[mac])
        result = runner.invoke(app, list(argv))
        assert result.exit_code == 0, result.output
        return result

    memory = Path("projects") / "-Users-kb-myapp" / "memory"
    return run, claude_a / memory, claude_b / memory


def test_deleting_formerly_published_checkout_does_not_resurrect_from_peers(tmp_path, monkeypatch):
    run, memory_a, memory_b = _two_macs(tmp_path, monkeypatch)
    fixture = memory_a / "fixture"
    fixture.mkdir()
    (fixture / "README.md").write_text("scratch")
    run("a", "push")
    run("b", "pull")
    run("b", "push")
    assert (memory_b / "fixture" / "README.md").read_text() == "scratch"
    (fixture / ".git").mkdir()
    run("a", "push")
    shutil.rmtree(fixture)
    run("a", "push")
    run("b", "pull")
    run("b", "push")
    run("a", "pull")
    assert not fixture.exists()


def test_pull_never_writes_or_deletes_inside_a_local_checkout(tmp_path, monkeypatch):
    run, memory_a, memory_b = _two_macs(tmp_path, monkeypatch)
    fixture = memory_a / "fixture"
    fixture.mkdir()
    (fixture / "tracked.md").write_text("published")
    run("a", "push")
    run("b", "pull")
    (memory_b / "fixture" / "new.md").write_text("peer addition")
    (memory_b / "fixture" / "tracked.md").write_text("peer edit")
    run("b", "push")
    (fixture / ".git").mkdir()
    (fixture / "tracked.md").write_text("uncommitted local work")
    run("a", "pull")
    assert (fixture / "tracked.md").read_text() == "uncommitted local work"
    assert not (fixture / "new.md").exists()
    assert not [path for path in fixture.iterdir() if "sync-conflict" in path.name]
    logged = run("a", "log", "--action", "excluded", "--format", "jsonl")
    records = [json.loads(line) for line in logged.output.splitlines() if line.startswith("{")]
    root = "projects/-Users-kb-myapp/memory/fixture"
    # One aggregated record per root and advertising manifest, never one per file.
    assert sorted((r["device"], r["source"], r["rel_path"]) for r in records) == [
        ("dev-a", "claude", root),
        ("dev-b", "claude", root),
    ]


def test_pull_logs_a_checkout_root_for_skipped_deletions(tmp_path, monkeypatch):
    run, memory_a, memory_b = _two_macs(tmp_path, monkeypatch)
    fixture = memory_a / "fixture"
    fixture.mkdir()
    (fixture / "tracked.md").write_text("published")
    run("a", "push")
    run("b", "pull")
    run("b", "push")
    (memory_b / "fixture" / "tracked.md").unlink()
    run("b", "push")
    (fixture / ".git").mkdir()
    run("a", "pull")
    assert (fixture / "tracked.md").read_text() == "published"
    logged = run("a", "log", "--action", "excluded", "--format", "jsonl")
    records = [json.loads(line) for line in logged.output.splitlines() if line.startswith("{")]
    root = "projects/-Users-kb-myapp/memory/fixture"
    # dev-b advertises only a tombstone under the checkout; it is still logged once.
    assert ("dev-b", "claude", root) in {(r["device"], r["source"], r["rel_path"]) for r in records}


def test_pull_logs_each_checkout_root_once_per_peer(tmp_path, monkeypatch):
    run, memory_a, memory_b = _two_macs(tmp_path, monkeypatch)
    fixture = memory_a / "fixture"
    fixture.mkdir()
    (fixture / "edited.md").write_text("published")
    (fixture / "deleted.md").write_text("published")
    run("a", "push")
    run("b", "pull")
    run("b", "push")
    (memory_b / "fixture" / "edited.md").write_text("peer edit")
    (memory_b / "fixture" / "deleted.md").unlink()
    run("b", "push")
    (fixture / ".git").mkdir()
    run("a", "pull")
    logged = run("a", "log", "--action", "excluded", "--format", "jsonl")
    records = [json.loads(line) for line in logged.output.splitlines() if line.startswith("{")]
    root = "projects/-Users-kb-myapp/memory/fixture"
    from_b = [(r["source"], r["rel_path"]) for r in records if r["device"] == "dev-b"]
    # dev-b advertises a file and a tombstone under the root: still one record.
    assert from_b == [("claude", root)]


def test_growth_warning_skips_a_macs_first_push_of_fleet_content(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_PUSH_GROWTH_WARNING_FILES", 1)
    run, _memory_a, _memory_b = _two_macs(tmp_path, monkeypatch)
    run("a", "push")
    run("b", "pull")
    assert "large sync growth" not in run("b", "push").stderr


def test_interrupt_after_acceptance_before_verdict_records_unverified(capture61, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_module, "_report_usage_publication", interrupt)
    result = runner.invoke(app, ["push"])
    assert result.exit_code != 0
    record, reason = cli_module.attemptlog.read()
    assert reason is None and record["row_ts"] is not None
    assert (record["class"], record["cause"]) == ("unverified", "evidence-error")
    sync_run = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert sync_run["outcome"] == "failed" and sync_run["cause"] == "interrupted"
    assert sync_run["content_accepted"] is True
    cli_module.acquire_lock()
    cli_module.release_lock()


@pytest.mark.parametrize("maintenance", ["last-seen", "cleanup"])
def test_unattended_push_still_fails_loudly_when_post_acceptance_maintenance_fails(
    capture61, monkeypatch, maintenance
):
    """Only attended push downgrades post-acceptance maintenance to a warning."""
    rel = "projects/-Users-kb-myapp/memory/new.md"
    (capture61["claude"] / rel).write_text("pending")

    def fail(*args, **kwargs):
        raise OSError("maintenance unavailable")

    monkeypatch.setattr(
        cli_module,
        "update_last_seen" if maintenance == "last-seen" else "_cleanup_conflict_copies",
        fail,
    )
    result = runner.invoke(app, ["autopush"])
    assert result.exit_code == 0, result.output
    assert "mm: push failed - unexpected error" in result.stderr
    crumb = cli_module._read_autorun_breadcrumbs()["push"]
    assert (crumb["outcome"], crumb["detail"]) == ("failed", "OSError")
    # Acceptance already happened, so the failure cannot roll the content back.
    assert rel in cli_module.sidecar.read("dev-a")["sources"]["claude"]["files"]


@pytest.fixture(params=["legacy", "table"])
def history73(request):
    from mind_meld import pullhistory

    rows = [
        {
            "ts": "2026-01-02T00:00:00+00:00",
            "verb": "pull",
            "action": "conflicted",
            "source": "claude",
            "rel_path": "one",
            "sidecar": "legacy-name",
        },
        {
            "ts": "2026-01-02T00:00:00+00:00",
            "verb": "pull",
            "action": "excluded",
            "source": "gstack",
            "rel_path": "two",
            "unknown": [1],
        },
        {
            "ts": "2026-01-01T00:00:00",
            "verb": "self-upgrade",
            "old_version": "1",
            "new_version": "2",
        },
        {"ts": "invalid", "verb": "push", "action": "uploaded", "source": 4, "rel_path": "\udcff"},
        {"verb": "pull", "action": None},
    ]
    path = pullhistory.history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"torn\n"
        + b"\xff\n"
        + b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in rows)
    )
    if request.param == "table":
        pullhistory.maintain_best_effort()
    return rows, request.param


@pytest.mark.parametrize(
    "argv,indices",
    [
        ([], [3, 0, 1, 2, 4]),
        (["--action", "conflicted"], [0]),
        (["--action", "excluded", "--source", "gstack"], [1]),
        (["--verb", "self-upgrade"], [2]),
        (["--since", "2026-01-02"], [0, 1]),
        # Generated by /ship test coverage audit (Track 73B).
        # Value: protects=--since treats a naive legacy ts as UTC on both read paths;
        # fails_when=_timestamp_us drops naive rows or reads them as local time;
        # why_new=the 2026-01-02 row cannot tell UTC, local or dropped apart;
        # seam=none; no_claim=local-time misreads on a UTC host (identical there)
        (["--since", "2026-01-01"], [0, 1, 2]),
        (["--since", "2026-01-01T00:00:00.000001"], [0, 1]),
        (["--limit", "2"], [3, 0]),
    ],
)
def test_log_legacy_and_table_contract73(history73, argv, indices):
    rows, _ = history73
    result = runner.invoke(app, ["log", "--format", "jsonl", *argv])
    assert result.exit_code == 0, result.exception
    assert result.stdout == "".join(json.dumps(rows[i], sort_keys=True) + "\n" for i in indices)
    assert result.stderr == ""


def test_log_title_and_safe_cells73(history73):
    from mind_meld import pullhistory

    rows, origin = history73
    result = runner.invoke(app, ["log", "--verb", "self-upgrade"])
    assert result.exit_code == 0
    assert ("state.db" if origin == "table" else "legacy pull-history.jsonl") in result.stdout
    pullhistory.append(
        verb="pull", device="d", source="s", rel_path="\x1b[31m[bold]x[/bold]", action="written"
    )
    result = runner.invoke(app, ["log", "--action", "written"])
    assert result.exit_code == 0
    assert "[bold]x[/bold]" in result.stdout
    assert "\x1b" not in result.stdout


@pytest.mark.parametrize("kind", ["hot", "busy", "too-new", "unavailable", "corrupt", "other"])
def test_log_errors_plain_empty_stdout73(kind, tmp_path, monkeypatch):
    import sqlite3

    from mind_meld import pullhistory, statedb
    from tests.test_statedb import make_hot_journal

    pullhistory.maintain_best_effort()
    monkeypatch.setattr(statedb, "BUSY_TIMEOUT_MS", 100)
    locker = None
    if kind == "hot":
        make_hot_journal(statedb.db_path(), tmp_path)
        expected = statedb.StateDBHotJournal("SQLITE_READONLY_ROLLBACK")
    elif kind == "busy":
        locker = sqlite3.connect(statedb.db_path())
        locker.execute("BEGIN EXCLUSIVE")
        expected = statedb.StateDBBusy("SQLITE_BUSY")
    elif kind == "too-new":
        with statedb.connect(read_only=False) as conn:
            conn.execute("UPDATE meta SET value='2' WHERE key='compat_version'")
        expected = statedb.StateDBTooNew(2)
    elif kind == "unavailable":
        monkeypatch.setitem(sys.modules, "sqlite3", None)
        expected = statedb.StateDBUnavailable("sqlite_unavailable")
    elif kind == "corrupt":
        statedb.db_path().write_bytes(b"not sqlite")
        expected = statedb.StateDBCorrupt("SQLITE_NOTADB")
    else:
        original = pullhistory._meta

        def broken(conn):
            original(conn)
            conn.execute("SELECT * FROM missing_table")

        monkeypatch.setattr(pullhistory, "_meta", broken)
        expected = statedb.StateDBError("SQLITE_ERROR")
    try:
        result = runner.invoke(app, ["log", "--format", "jsonl"])
    finally:
        if locker:
            locker.close()
    assert result.exit_code == 1, result.exception
    assert result.stdout == ""
    assert result.stderr == pullhistory.explain(expected) + "\n"
    assert len(result.stderr.splitlines()) == 1
    if kind != "corrupt":
        assert "move state.db" not in result.stderr


def test_log_no_sqlite_no_database_and_version73(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite3", None)
    assert runner.invoke(app, ["log", "--format", "jsonl"]).exit_code == 0
    assert runner.invoke(app, ["--version"]).exit_code == 0


def test_log_zero_byte_fallback_write_free_then_migrate73(history73):
    from mind_meld import pullhistory, statedb

    rows, _ = history73
    statedb.db_path().write_bytes(b"")
    before = _preview_tree(statedb.DB_DIR)
    result = runner.invoke(app, ["log", "--format", "jsonl"])
    assert result.exit_code == 0
    assert result.stdout == "".join(
        json.dumps(rows[i], sort_keys=True) + "\n" for i in (3, 0, 1, 2, 4)
    )
    assert _preview_tree(statedb.DB_DIR) == before
    pullhistory.append_self_upgrade(device="d", old_version="1", new_version="2")
    assert pullhistory.query().origin == "state.db"


def test_log_stale_notice_write_free_and_silenced73(history73):
    from mind_meld import pullhistory

    pullhistory.maintain_best_effort()
    path = pullhistory.history_path()
    with path.open("ab") as file:
        file.write(b'{"stale":true}\n')
    before = _preview_tree(path.parent)
    result = runner.invoke(app, ["log", "--format", "jsonl"])
    assert result.exit_code == 0
    assert "by an older mm" in result.stderr
    assert _preview_tree(path.parent) == before
    path.rename(path.with_suffix(".aside"))
    assert runner.invoke(app, ["log", "--format", "jsonl"]).stderr == ""


def test_log_unbounded_complete_and_no_read_lock73(monkeypatch):
    from mind_meld import pullhistory, statedb

    rows = [
        {"ts": "2026-01-01", "rel_path": str(i)} if i % 3 else {"rel_path": str(i)}
        for i in range(12000)
    ]
    with statedb.connect(read_only=False) as conn, statedb.transaction(conn):
        pullhistory.import_if_needed(conn)
        pullhistory.write_rows(conn, rows)
    # Output-time exclusive writer must succeed: the reader has closed already.
    original = cli_module.typer.echo
    checked = False

    def echo(*args, **kwargs):
        nonlocal checked
        if not checked and not kwargs.get("err"):
            with statedb.connect(read_only=False) as conn, statedb.transaction(conn):
                conn.execute("INSERT INTO meta (key,value) VALUES ('output-check','ok')")
            checked = True
        return original(*args, **kwargs)

    monkeypatch.setattr(cli_module.typer, "echo", echo)
    result = runner.invoke(app, ["log", "--limit", "0", "--format", "jsonl"])
    assert result.exit_code == 0, result.exception
    assert [json.loads(line) for line in result.stdout.splitlines()] == sorted(
        rows, key=lambda r: r.get("ts", ""), reverse=True
    )
    assert checked


# "autopush" row generated by /ship test coverage audit (Track 73B).
# Value: protects=autopush stays silent when its history flush fails (d3a);
# fails_when=autopush enters command_scope(attended=True); why_new=only pull and
# autopull were parametrized; seam=none; no_claim=a real busy database (write_rows
# is patched to raise)
@pytest.mark.parametrize("command", ["pull", "autopull", "autopush"])
def test_history_failure_attended_only73(push_preview56, monkeypatch, command):
    from mind_meld import pullhistory, statedb

    env = push_preview56
    assert runner.invoke(app, ["push"]).exit_code == 0
    if command == "autopush":
        memory = env["claude"] / "projects/-Users-kb-myapp/memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "local-73.md").write_text("local")
    _publish_peer62(
        env,
        "peer-73",
        {
            ("claude", "projects/-Users-kb-myapp/memory/new-73.md"): (
                b"peer",
                "2026-10-16T00:00:00+00:00",
            )
        },
    )
    attempts = []

    def fail(*args):
        attempts.append(1)
        raise statedb.StateDBBusy("SQLITE_BUSY")

    monkeypatch.setattr(pullhistory, "write_rows", fail)
    result = runner.invoke(app, [command])
    assert attempts == [1], result.output  # the flush really failed, once
    assert result.exit_code == 0, result.exception
    assert result.stderr.count("mm: notice: history not recorded") == (
        1 if command == "pull" else 0
    )


def test_unchanged_push_recovers_hot_history73(push_preview56, tmp_path):
    from mind_meld import statedb
    from tests.test_statedb import make_hot_journal

    assert runner.invoke(app, ["push"]).exit_code == 0
    make_hot_journal(statedb.db_path(), tmp_path)
    assert runner.invoke(app, ["log"]).exit_code == 1
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0, result.exception
    assert not statedb.db_path().with_name("state.db-journal").exists()
    assert runner.invoke(app, ["log"]).exit_code == 0


def test_pull_preview_legacy_never_creates_database73(push_preview56):
    from mind_meld import pullhistory, statedb

    path = pullhistory.history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"legacy":true}\n')
    result = runner.invoke(app, ["pull", "--dry-run"])
    assert result.exit_code == 0, result.exception
    assert not statedb.db_path().exists()


def test_log_populated_database_audit73(preview62):
    from mind_meld import pullhistory

    pullhistory.append_self_upgrade(device="d", old_version="1", new_version="2")
    result = preview62["audit"](["log"])
    assert result.exit_code == 0, result.exception
    assert "state.db" in result.stdout


def test_preview_audit_catches_writable_sqlite73(tmp_path):
    import sqlite3

    path = tmp_path / "audit.db"
    records = []
    token = _PREVIEW_AUDIT.set((records, None))
    try:
        sqlite3.connect(path).close()
    finally:
        _PREVIEW_AUDIT.reset(token)
    assert any(event == "sqlite3.connect" for event, _ in records)


def test_transition_and_busy_database_one_stall73(push_preview56, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    from mind_meld import pullhistory, statedb

    env = push_preview56
    assert runner.invoke(app, ["push"]).exit_code == 0
    cache = env["upgrade"]._empty_cache()
    cache.update(
        last_seen_self_version="0.0.1",
        latest_version="1.9.1",
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    env["upgrade"].CACHE_PATH.write_text(json.dumps(cache))
    env["upgrade"]._reset_for_tests()
    monkeypatch.setattr(statedb, "BUSY_TIMEOUT_MS", 100)
    original = statedb.connect
    opens = []

    @contextmanager
    def counted(*, read_only):
        if not read_only:
            opens.append(1)
        with original(read_only=read_only) as conn:
            yield conn

    monkeypatch.setattr(statedb, "connect", counted)
    with sqlite3.connect(statedb.db_path()) as locker:
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("INSERT INTO meta (key,value) VALUES ('locked','yes')")
        result = runner.invoke(app, ["push"])
        assert result.exit_code == 0, result.exception
        assert opens == [1]
        assert result.stderr.count("mm: notice: history not recorded") == 1
    assert not any(r.get("old_version") == "0.0.1" for r in pullhistory.read_records())


def test_attended_pull_2500_excluded_three_flushes73(push_preview56, monkeypatch):
    from mind_meld import pullhistory
    from mind_meld.storage.keys import manifest_key

    env = push_preview56
    cfg = env["config"]
    cfg["sync"]["sources"][0]["exclude_patterns"] = ["projects/*/memory/excluded-*"]
    save_config(cfg, env["config_path"])
    cli_module._init_crypto_session(env["backend"], PASSPHRASE, cfg, read_only=False)
    peer = _publish_peer62(env, "peer-73", {})
    peer["sources"] = {
        "claude": {
            "files": {
                f"projects/-Users-kb-myapp/memory/excluded-{i}.md": {
                    "sha256": "a" * 64,
                    "size": 1,
                    "mtime": "2026-10-16T00:00:00+00:00",
                }
                for i in range(2500)
            }
        }
    }
    env["backend"].put(
        manifest_key("peer-73"), encrypt(serialize_manifest(peer), PASSPHRASE, MEMORY_KB)
    )
    original = pullhistory.write_rows
    counts = []

    def counted(conn, rows):
        counts.append(len(rows))
        return original(conn, rows)

    monkeypatch.setattr(pullhistory, "write_rows", counted)
    result = runner.invoke(app, ["pull"])
    assert result.exit_code == 0, result.exception
    assert counts == [1000, 1000, 500], result.output
    assert len(pullhistory.query(action="excluded", limit=0)) == 2500


@pytest.mark.parametrize(
    "code,name,kind", [(5, "SQLITE_BUSY", "busy"), (779, "SQLITE_CORRUPT_INDEX", "corrupt")]
)
def test_query_time_error_no_partial_stdout73(monkeypatch, code, name, kind):
    import sqlite3

    from mind_meld import pullhistory, statedb

    pullhistory.append_self_upgrade(device="d", old_version="1", new_version="2")

    def broken(conn):
        error = sqlite3.OperationalError("injected during query")
        error.sqlite_errorcode = code
        error.sqlite_errorname = name
        raise error

    monkeypatch.setattr(pullhistory, "_meta", broken)
    result = runner.invoke(app, ["log", "--format", "jsonl"])
    assert result.exit_code == 1
    assert result.stdout == ""
    cls = statedb.StateDBBusy if kind == "busy" else statedb.StateDBCorrupt
    assert result.stderr == pullhistory.explain(cls(name)) + "\n"


@pytest.mark.parametrize("command", ["push", "autopush"])
def test_push_uploads_share_one_history_flush73(push_preview56, monkeypatch, command):
    from mind_meld import pullhistory

    env = push_preview56
    assert runner.invoke(app, ["push"]).exit_code == 0
    memory = env["claude"] / "projects/-Users-kb-myapp/memory"
    memory.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        (memory / f"batch-{i}.md").write_text(str(i))
    events = env["events"] / "events"
    events.mkdir(parents=True, exist_ok=True)
    (events / "batch-73.jsonl").write_text('{"batch":73}\n')
    original, flushes = pullhistory.write_rows, []

    def counted(conn, rows):
        flushes.append(rows)
        return original(conn, rows)

    monkeypatch.setattr(pullhistory, "write_rows", counted)
    result = runner.invoke(app, [command])
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(flushes) == 1, [len(rows) for rows in flushes]
    uploaded = [r for r in flushes[0] if r.get("action") == "uploaded"]
    assert len(uploaded) >= 6
    assert {r["source"] for r in uploaded} >= {"claude", "mm-events"}


def test_log_huge_limit_and_unencodable_source73():
    from mind_meld import pullhistory

    pullhistory.append_self_upgrade(device="d", old_version="1", new_version="2")
    result = runner.invoke(app, ["log", "--format", "jsonl", "--limit", "99999999999999999999"])
    assert result.exit_code == 0, result.exception
    assert len(result.stdout.splitlines()) == 1
    result = runner.invoke(app, ["log", "--format", "jsonl", "--source", "\udcff"])
    assert result.exit_code == 0, result.exception
    assert result.stdout == ""


def test_log_malformed_row_is_one_line_error73():
    from mind_meld import pullhistory, statedb

    pullhistory.append_self_upgrade(device="d", old_version="1", new_version="2")
    with statedb.connect(read_only=False) as conn:
        conn.execute("UPDATE history SET extra='not json'")
    result = runner.invoke(app, ["log", "--format", "jsonl"])
    assert result.exit_code == 1, result.exception
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert result.stderr.startswith("mm: error: ") and "is damaged" in result.stderr


def test_recapture_enters_history_scope_and_recovery73(preview62, monkeypatch):
    from mind_meld import statedb

    calls = []

    def recover():
        calls.append(1)
        return statedb.StateDBBusy("SQLITE_BUSY")

    monkeypatch.setattr(statedb, "recover_if_needed", recover)
    result = runner.invoke(app, ["recapture", "1d"])
    assert calls == [1], (result.output, result.exception)
    assert result.stderr.count("mm: notice: history not recorded") == 1
    preview = runner.invoke(app, ["recapture", "--dry-run", "1d"])
    assert calls == [1], preview.output


@pytest.mark.parametrize(
    "argv,repaired",
    [
        (["push"], True),
        (["pull"], True),
        (["autopush"], True),
        (["autopull"], True),
        (["push", "--dry-run"], False),
        (["pull", "--dry-run"], False),
    ],
)
def test_hot_history_recovery_per_command73(push_preview56, tmp_path, argv, repaired):
    from mind_meld import statedb
    from tests.test_statedb import make_hot_journal

    assert runner.invoke(app, ["push"]).exit_code == 0
    make_hot_journal(statedb.db_path(), tmp_path)
    journal = statedb.db_path().with_name("state.db-journal")
    before = journal.read_bytes()
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, (result.output, result.exception)
    assert (not journal.exists()) if repaired else journal.read_bytes() == before


def test_upload_missing_published_root_refuses_sibling(tmp_path, monkeypatch):
    """A root lost after omission proof must not become per-file holds."""
    # Value: protects=missing published roots still refuse publication;
    # fails_when=upload holds mask a whole missing root;
    # why_new=existing missing-root coverage removes it before scanning;
    # seam=none; the test renames an isolated source at the existing upload boundary;
    # no_claim=real unmount timing or native fleet delivery.
    harness = TestCompleteSnapshots()
    root = tmp_path / "notes"
    root.mkdir()
    (root / "keep.md").write_text("notes")
    env = harness._prepare(
        tmp_path,
        monkeypatch,
        extra_sources=[
            {
                "name": "notes",
                "path": str(root),
                "type": "generic",
                "include_files": ["keep.md"],
            }
        ],
    )
    first = harness._push()
    assert first.exit_code == 0, first.output
    prior = harness._prior(env)
    (root / "keep.md").write_text("notes changed")
    (env["memory"] / "a.md").write_text("sibling changed")
    real_upload = cli_module._upload_changed_blobs
    renamed = []

    def rename_then_upload(*args, **kwargs):
        if kwargs.get("src_name") == "notes":
            root.rename(tmp_path / "notes-away")
            renamed.append(True)
        return real_upload(*args, **kwargs)

    monkeypatch.setattr(cli_module, "_upload_changed_blobs", rename_then_upload)
    result = harness._push()
    assert renamed == [True]
    assert result.exit_code == 1, result.output
    harness._assert_prior_kept(env, prior, result)
    output = " ".join((result.output + (result.stderr or "")).split())
    assert "missing after it was previously published" in output
    assert "Restore the source folder" in output


@pytest.fixture
def status77(tmp_path, monkeypatch):
    """Real stable sync; isolate optional discovery from machine-dependent budgets."""
    helpers = TestTrack7BEventsTail()
    storage, claude = tmp_path / "storage", tmp_path / "claude"
    helpers._seed_claude(claude)
    backend = helpers._bootstrap(storage)
    register_device(backend, "dev-a", "A")
    path = helpers._make_config_with_events(tmp_path, storage, claude, "dev-a", "A")
    helpers._activate(monkeypatch, path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    monkeypatch.setattr(
        _mm_events, "discover_git_roots", lambda *a, **k: _mm_events.GitRootDiscovery((), ())
    )
    pushed = runner.invoke(app, ["push"])
    assert pushed.exit_code == 0, pushed.output
    return {
        "cfg": config_module.load_config(path),
        "path": path,
        "claude": claude,
        "backend": backend,
    }


def test_status77_content_and_metadata_round_trips(status77):
    from mind_meld import attemptlog

    root = status77["claude"] / "projects/-Users-kb-myapp/memory"
    baseline = runner.invoke(app, ["status"])
    assert baseline.exit_code == 0, baseline.output
    # Missing new history on upgrade is normal, not a mandatory seeding run.
    attemptlog.runs_path().unlink()
    assert (
        runner.invoke(app, ["status"]).output.splitlines()[0]
        == "In sync with this device's last push."
    )
    old = root / "user_role.md"
    (root / "delete77.md").write_text("will delete")
    assert runner.invoke(app, ["push"]).exit_code == 0
    changed = old.read_bytes() + b"\nlocal delta\n"
    old.write_bytes(changed)
    (root / "new77.md").write_text("new")
    deleted = root / "delete77.md"
    deleted.unlink()
    first = runner.invoke(app, ["status"])
    assert first.exit_code == 0, first.output
    assert (
        first.output.splitlines()[0]
        == "1 new, 1 modified and 1 deleted waiting to push. Run: mm push"
    )
    assert runner.invoke(app, ["push"]).exit_code == 0
    assert (
        runner.invoke(app, ["status"]).output.splitlines()[0]
        == "In sync with this device's last push."
    )
    previous = old.stat()
    os.utime(old, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))
    assert (
        runner.invoke(app, ["status"]).output.splitlines()[0]
        == "Metadata-only changes waiting to push. Run: mm push"
    )
    assert runner.invoke(app, ["push"]).exit_code == 0
    assert (
        runner.invoke(app, ["status"]).output.splitlines()[0]
        == "In sync with this device's last push."
    )


@pytest.mark.parametrize(
    "mode", ["unknown", "disabled", "unavailable", "published-missing", "zero", "unreadable"]
)
def test_status77_source_completeness(status77, monkeypatch, mode):
    cfg = status77["cfg"]
    args = ["status"]
    if mode == "unknown":
        args += ["--source", "missing"]
    elif mode == "disabled":
        cfg["sync"]["disabled_sources"] = ["claude"]
        args += ["--source", "claude"]
    elif mode in ("unavailable", "published-missing"):
        cfg["sync"]["sources"][0]["path"] = str(status77["path"].parent / "missing")
        if mode == "unavailable":
            args += ["--source", "claude"]
    elif mode == "zero":
        cfg["sync"]["sources"] = []
    else:
        original = cli_module.manifest.read_file_revision

        def denied(path, *a, **k):
            if path.name == "user_role.md":
                from mind_meld.errors import SnapshotError

                raise SnapshotError("could not be read")
            return original(path, *a, **k)

        monkeypatch.setattr(cli_module.manifest, "read_file_revision", denied)
    save_config(cfg, status77["path"])
    shown = runner.invoke(app, args)
    assert "In sync with" not in shown.output.splitlines()[0]
    assert "deleted waiting" not in shown.output.splitlines()[0]
    assert "run:" in shown.output.splitlines()[0].lower()
    if "--source" in args:
        assert args[-1] in shown.output.splitlines()[0]
    if mode == "unreadable":
        assert "Metadata-only changes pending" not in shown.output
        assert "Comparison incomplete" in shown.output


@pytest.mark.parametrize("bad", [{}, [], None])
def test_status77_malformed_autorun_sibling_stays_unknown(status77, bad):
    from datetime import timedelta

    stamp = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    cli_module._autorun_breadcrumb_path().write_text(
        json.dumps({"push": {"timestamp": stamp, "outcome": "success"}, "pull": bad})
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "run-evidence-corrupt" in result.output.splitlines()[0]


@pytest.mark.parametrize("conflict_source,count", [("other", 0), ("claude", 1)])
def test_status77_conflict_count_respects_source(status77, monkeypatch, conflict_source, count):
    monkeypatch.setattr(
        cli_module.resolveflow,
        "_find_conflict_files",
        lambda cfg: [(conflict_source, status77["path"].parent / "conflict", None)],
    )
    shown = runner.invoke(app, ["status", "--source", "claude"])
    assert shown.exit_code == 0, shown.output
    headline = shown.output.splitlines()[0]
    assert ("1 unresolved conflicts" in headline) is bool(count)
    if not count:
        assert "In sync with this device's last push." in headline
    assert (
        runner.invoke(app, ["status"]).output.splitlines()[0].startswith("1 unresolved conflicts")
    )


def test_status77_real_scoped_pull_records_selection(status77):
    from mind_meld import attemptlog

    register_device(status77["backend"], "dev-b", "B")
    shown = runner.invoke(app, ["pull", "--source", "claude", "--from", "dev-b"])
    assert shown.exit_code == 0, shown.output
    attended = attemptlog.read_runs()
    assert attended["runs"]["pull"]["scope"] == {"source": "claude", "from_device": "dev-b"}
    projected = attemptlog.project_runs(cli_module._read_autorun_evidence(), attended)
    assert projected["pull"]["limited_coverage"] is True


def test_status77_unused_optional_and_disabled_sources_are_normal(status77):
    cfg = status77["cfg"]
    cfg["sync"]["sources"].append(
        {"name": "optional", "type": "generic", "path": str(status77["path"].parent / "absent")}
    )
    save_config(cfg, status77["path"])
    assert (
        runner.invoke(app, ["status"]).output.splitlines()[0]
        == "In sync with this device's last push."
    )
    cfg["sync"]["disabled_sources"] = ["optional"]
    save_config(cfg, status77["path"])
    assert (
        runner.invoke(app, ["status"]).output.splitlines()[0]
        == "In sync with this device's last push."
    )


@pytest.mark.parametrize("state", ["missing", "corrupt", "newer"])
def test_status77_own_manifest_state_prevents_healthy(status77, state):
    key = storage_keys.manifest_key("dev-a")
    backend = status77["backend"]
    if state == "missing":
        backend.delete(key)
    else:
        backend.put(key, b"bad" if state == "corrupt" else b"\x03")
    shown = runner.invoke(app, ["status"])
    assert shown.exit_code == 0, shown.output
    first = shown.output.splitlines()[0]
    assert "In sync" not in first
    assert (
        "has not pushed yet" if state == "missing" else "corrupt" if state == "corrupt" else "newer"
    ) in first


@pytest.mark.parametrize("width", [24, 80, 240])
def test_status77_verdict_first_both_streams_narrow_and_notty(status77, monkeypatch, width):
    import sys

    monkeypatch.setattr(cli_module.console, "_width", width)
    monkeypatch.setattr(cli_module.stderr_console, "_width", width)
    original = cli_module._read_capture_rows

    def notices(*a, **k):
        print("preparatory stdout notice")
        print("preparatory stderr notice", file=sys.stderr)
        return original(*a, **k)

    monkeypatch.setattr(cli_module, "_read_capture_rows", notices)
    shown = runner.invoke(app, ["status"])
    assert shown.exit_code == 0, shown.output
    assert shown.output.splitlines()[0] == "In sync with this device's last push."
    assert "preparatory stdout notice" in shown.stdout
    assert "preparatory stderr notice" in shown.stderr
    assert shown.stdout.startswith("In sync with this device's last push.\n")


@pytest.mark.parametrize("late", [False, True])
def test_status77_fatal_replays_notices_preserves_exception(status77, monkeypatch, late):
    import sys

    def fail(*a, **k):
        print("ancillary stdout")
        print("ancillary stderr", file=sys.stderr)
        raise RuntimeError("original")

    monkeypatch.setattr(cli_module, "_host_publication" if late else "get_backend", fail)
    shown = runner.invoke(app, ["status"])
    assert isinstance(shown.exception, RuntimeError)
    assert str(shown.exception) == "original"
    assert shown.output.splitlines()[0] == "Status inspection failed (RuntimeError)."
    assert "ancillary stdout" in shown.stdout and "ancillary stderr" in shown.stderr
    assert shown.output.splitlines()[-1] == "Inspection did not complete. Run: mm diag"


@pytest.mark.parametrize("cancel", [False, True, "eof"])
def test_status77_auth_boundary_preserves_prompts(status77, monkeypatch, cancel):
    import getpass

    monkeypatch.delenv("MINDMELD_PASSPHRASE", raising=False)
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)

    def prompt(*args, **kwargs):
        print("Mind Meld passphrase: ", end="")
        if cancel == "eof":
            raise EOFError()
        if cancel:
            raise KeyboardInterrupt()
        return PASSPHRASE

    monkeypatch.setattr(getpass, "getpass", prompt)
    shown = runner.invoke(app, ["status"])
    if cancel:
        assert shown.exit_code != 0
        assert "In sync with" not in shown.output
    else:
        assert shown.exit_code == 0, shown.output
        assert shown.output.startswith(
            "Mind Meld passphrase: In sync with this device's last push.\n"
        )


def test_status77_preview_and_inspection_leave_run_bytes_unchanged(status77):
    from mind_meld import attemptlog

    path = attemptlog.runs_path()
    initial = (path.read_bytes(), path.stat().st_mtime_ns)
    for args in (["push", "--dry-run"], ["pull", "--dry-run"], ["status"], ["diag", "--json"]):
        shown = runner.invoke(app, args)
        assert shown.exit_code == 0, shown.output
        assert (path.read_bytes(), path.stat().st_mtime_ns) == initial
    path.unlink()
    for args in (["push", "--dry-run"], ["pull", "--dry-run"], ["status"], ["diag", "--json"]):
        assert runner.invoke(app, args).exit_code == 0
        assert not path.exists()
        assert not path.with_suffix(".json.lock").exists()


def test_status77_source_count_isolation_and_nested_diag_paths(status77):
    cfg = status77["cfg"]
    for name, count in (("alpha", 1), ("beta", 2)):
        root = status77["path"].parent / name
        (root / "items").mkdir(parents=True)
        for i in range(count):
            (root / "items" / f"new{i}.md").write_text("pending")
            (root / "items" / f"checkout{i}" / ".git").mkdir(parents=True)
        cfg["sync"]["sources"].append(
            {"name": name, "type": "generic", "path": str(root), "include_dirs": ["items"]}
        )
    save_config(cfg, status77["path"])
    for name, count in (("alpha", 1), ("beta", 2)):
        shown = runner.invoke(app, ["status", "--source", name])
        assert shown.exit_code == 0, shown.output
        assert shown.output.splitlines()[0] == (
            f"Source '{name}' — {count} new, 0 modified and 0 deleted waiting to push. Run: mm push"
        )
        assert f"Skipped nested repositories: {count}. Details: mm diag" in shown.output
        assert "items/checkout" not in shown.output
    diag = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
    assert diag["sync_scope"]["skipped_nested_repositories"]["alpha"] == ["items/checkout0"]
    assert diag["sync_scope"]["skipped_nested_repositories"]["beta"] == [
        "items/checkout0",
        "items/checkout1",
    ]


def test_status77_conflicts_and_failed_conflict_inspection(status77, monkeypatch):
    monkeypatch.setattr(cli_module.resolveflow, "_find_conflict_files", lambda cfg: ["one", "two"])
    shown = runner.invoke(app, ["status"])
    assert shown.output.splitlines()[0] == "2 unresolved conflicts. Run: mm resolve"

    def denied(cfg):
        raise PermissionError("private")

    monkeypatch.setattr(cli_module.resolveflow, "_find_conflict_files", denied)
    shown = runner.invoke(app, ["status"])
    assert "Conflict inspection incomplete" in shown.output.splitlines()[0]
    assert "private" not in shown.output


@pytest.mark.parametrize("mtime", [None, "bad", "2026-01-01", "2026-01-01T00:00:00+00:00"])
def test_status77_mtime_helper_does_not_invent_forward_metadata(mtime):
    local = {"sources": {"claude": {"files": {"a": {"sha256": "same", "mtime": mtime}}}}}
    remote = {"claude": {"files": {"a": {"sha256": "same", "mtime": "2026-02-01T00:00:00+00:00"}}}}
    assert not cli_module._has_mtime_only_changes_vs_remote(local, remote)


@pytest.mark.parametrize("width", [24, 80])
@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize("ancillary", [False, True])
def test_status77_typed_fatal_keeps_message_and_remedy(
    status77, monkeypatch, width, color, ancillary
):
    import sys

    from rich.console import Console

    from mind_meld import attemptlog
    from mind_meld.errors import CryptoError

    message = "crypto: missing initialization. Run 'mm init' to initialize this storage."
    monkeypatch.setattr(
        cli_module,
        "stderr_console",
        Console(
            stderr=True,
            force_terminal=color,
            color_system="standard" if color else None,
            no_color=False,
            width=width,
        ),
    )

    def refuse(*args, **kwargs):
        if ancillary:
            print("ancillary stdout")
            print("ancillary stderr", file=sys.stderr)
        raise CryptoError(message)

    monkeypatch.setattr(cli_module, "_init_crypto_session", refuse)
    path = attemptlog.runs_path()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    shown = runner.invoke(app, ["status"], color=True)
    assert shown.exit_code == 1, shown.output
    lines = shown.output.splitlines()
    expected = "Error: " + message
    assert lines[0] == expected
    assert lines[-1] == expected
    assert lines.count(expected) == (2 if ancillary else 1)
    assert "Inspection did not complete. Run: mm diag" not in shown.output
    assert ("ancillary stdout" in shown.stdout) == ancillary
    assert ("ancillary stderr" in shown.stderr) == ancillary
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_status77_unrelated_scan_failure_does_not_block_selected_source(status77, monkeypatch):
    cfg = status77["cfg"]
    other = status77["path"].parent / "other"
    other.mkdir()
    (other / "other.md").write_text("other context")
    cfg["sync"]["sources"].append(
        {
            "name": "other",
            "type": "generic",
            "path": str(other),
            "include_files": ["other.md"],
        }
    )
    save_config(cfg, status77["path"])
    original = cli_module.manifest.read_file_revision

    def denied(path, *args, **kwargs):
        if path == other / "other.md":
            from mind_meld.errors import SnapshotError

            raise SnapshotError("unrelated source could not be read")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(cli_module.manifest, "read_file_revision", denied)
    shown = runner.invoke(app, ["status", "--source", "claude"])
    assert shown.exit_code == 0, shown.output
    assert shown.output.splitlines()[0] == (
        "Source 'claude' — In sync with this device's last push."
    )
    whole = runner.invoke(app, ["status"])
    assert whole.exit_code == 0, whole.output
    assert "Source inspection incomplete" in whole.output.splitlines()[0]


@pytest.mark.parametrize("mode", ["published-oversized", "new-oversized", "deleted", "excluded"])
def test_status77_omitted_file_comparison_is_truthful(status77, mode):
    cfg = status77["cfg"]
    root = status77["claude"] / "projects/-Users-kb-myapp/memory"
    path = root / "user_role.md"
    if mode == "deleted":
        path.unlink()
    else:
        if mode == "new-oversized":
            path = root / "new-oversized.md"
        path.write_text("x" * 4096)
        cfg["sync"]["max_file_size"] = 2048
        if mode == "excluded":
            cfg["sync"]["sources"][0]["exclude_patterns"] = ["**/user_role.md"]
        save_config(cfg, status77["path"])
    shown = runner.invoke(app, ["status", "--source", "claude"])
    assert shown.exit_code == 0, shown.output
    headline = shown.output.splitlines()[0]
    if mode == "published-oversized":
        assert "Source inspection incomplete" in headline
        assert "deleted waiting" not in headline
        assert "Comparison incomplete" in shown.output
    elif mode == "new-oversized":
        assert "In sync with this device's last push." in headline
    else:
        assert "0 new, 0 modified and 1 deleted waiting to push" in headline


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("width", [24, 80, 240])
def test_sync78_default_and_verbose_share_one_logical_final_line(
    status77, monkeypatch, verbose, width
):
    monkeypatch.setattr(cli_module.console, "_width", width)
    monkeypatch.setattr(cli_module.stderr_console, "_width", width)
    result = runner.invoke(app, ["push", *(["--verbose"] if verbose else [])])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "Push: no content changes."
    assert sum(line.startswith("Push:") for line in result.output.splitlines()) == 1
    if not verbose:
        assert result.stdout == "Push: no content changes.\n"
        assert result.stderr == ""
    assert ("Building manifest" in result.output) is verbose


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("change", ["files", "metadata", "selection", "repair"])
def test_sync78_real_push_accepted_grammar(status77, monkeypatch, verbose, change):
    env = status77
    memory = env["claude"] / "projects/-Users-kb-myapp/memory"
    file = memory / "user_role.md"
    if change == "files":
        file.write_text("changed")
        (memory / "new78.md").write_text("new")
        expected = "Push: 2 content changes published."
    elif change == "metadata":
        stamp = file.stat().st_mtime + 2
        os.utime(file, (stamp, stamp))
        expected = "Push: metadata updated."
    elif change == "selection":
        cfg = env["cfg"]
        extra = env["path"].parent / "empty78"
        extra.mkdir()
        cfg["sync"]["sources"].append({"name": "empty78", "type": "generic", "path": str(extra)})
        save_config(cfg, env["path"])
        expected = "Push: source selection updated."
    else:
        backend = env["backend"]
        key = storage_keys.manifest_key("dev-a")
        (backend.root / Path(key).with_name("manifest.json 2.enc")).write_bytes(backend.get(key))
        backend.delete(key)
        expected = "Push: manifest repaired."
    result = runner.invoke(app, ["push", *(["--verbose"] if verbose else [])])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == expected
    record = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert record["content_accepted"] is True
    assert "published_content_files" not in record["counts"]


@pytest.mark.parametrize("verbose", [False, True])
def test_sync78_degraded_capture_default_is_quiet_and_status_actionable(
    capture61, monkeypatch, verbose
):
    monkeypatch.setattr(
        _mm_host_usage,
        "read_codex_usage",
        lambda **kw: _mm_host_usage.HostUsageResult({}, False, "unsupported"),
    )
    result = runner.invoke(app, ["push", *(["--verbose"] if verbose else [])])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "Push: no content changes."
    assert ("Usage capture (no-row)" in result.stderr) is verbose
    run = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert run["outcome"] == "degraded"
    assert any("no-row" in detail for detail in run["degradations"])
    status = runner.invoke(app, ["status"])
    assert "In sync" not in status.output.splitlines()[0]
    assert "mm update" in status.output or "mm diag" in status.output
    diag = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
    assert diag["last_attended_runs"]["runs"]["push"]["outcome"] == "degraded"


@pytest.mark.parametrize("fault", ["update", "unlock", "history", "record", "capture-record"])
def test_sync78_finalizer_notice_precedes_outcome_and_primary_wins(capture61, monkeypatch, fault):
    from mind_meld import pullhistory, statedb

    def stop(*args, **kwargs):
        raise RuntimeError("PRIVATE78 exception")

    monkeypatch.setattr(cli_module, "_push_core", stop)
    if fault == "update":
        monkeypatch.setattr(cli_module.upgrade, "update_or_nudge", stop)
    elif fault == "unlock":
        original = cli_module.release_lock

        def unlock():
            original()
            raise OSError("PRIVATE78 unlock")

        monkeypatch.setattr(cli_module, "release_lock", unlock)
    elif fault == "history":
        original = cli_module._push_core

        def history(*args, **kwargs):
            pullhistory.record_failure(statedb.StateDBBusy("SQLITE_BUSY"))
            return original(*args, **kwargs)

        monkeypatch.setattr(cli_module, "_push_core", history)
    elif fault == "record":
        monkeypatch.setattr(
            cli_module.attemptlog,
            "write_run",
            lambda *a: (_ for _ in ()).throw(OSError(5, "PRIVATE78")),
        )
    else:

        def capture_record(*args, **kwargs):
            kwargs["attempt"].outcome = cli_module.attemptlog.CaptureOutcome().finish("no-row")
            raise RuntimeError("PRIVATE78")

        monkeypatch.setattr(cli_module, "_push_core", capture_record)
        monkeypatch.setattr(cli_module.attemptlog, "write", stop)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 1, result.output
    assert result.output.splitlines()[-1].startswith(
        "Push failed [unexpected-error]: RuntimeError;"
    )
    assert result.output.splitlines()[-1].endswith("Run: mm diag")
    assert result.output.count("Push failed") == 1
    assert "PRIVATE78" not in result.output
    notice = {
        "update": "automatic update did not complete",
        "unlock": "lock release could not be confirmed",
        "record": "run-evidence-write-unavailable",
        "capture-record": "could not durably record",
    }.get(fault)
    if notice:
        assert result.output.index(notice) < result.output.rindex("Push failed")
    if fault != "record":
        assert cli_module.attemptlog.read_runs()["runs"]["push"]["cause"] == "unexpected-error"


@pytest.mark.parametrize("when", ["before", "accepted"])
def test_sync78_push_unexpected_receipt_survives_cleanup(status77, monkeypatch, when):
    file = status77["claude"] / "projects/-Users-kb-myapp/memory/user_role.md"
    file.write_text("new78 bytes")
    original = cli_module._push_core
    propagated = []
    exc = RuntimeError("PRIVATE78")
    if when == "accepted":
        monkeypatch.setattr(cli_module, "update_last_seen", lambda *a: (_ for _ in ()).throw(exc))
    else:
        monkeypatch.setattr(
            cli_module, "_upload_changed_blobs", lambda *a, **k: (_ for _ in ()).throw(exc)
        )

    def core(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except BaseException as error:
            propagated.append(error)
            raise

    monkeypatch.setattr(cli_module, "_push_core", core)
    result = runner.invoke(app, ["push", "--verbose"])
    assert result.exit_code == 1
    assert propagated == [exc]
    assert "PRIVATE78" not in result.output
    run = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert run["content_accepted"] is (when == "accepted")
    if when == "accepted":
        assert (
            "1 content change published; accepted changes remain" in result.stderr.splitlines()[-1]
        )
        assert run["counts"]["content_files"] == 1
    else:
        assert "effects unconfirmed" in result.stderr.splitlines()[-1]
    assert "cli.py:_push_core:" in result.stderr


@pytest.mark.parametrize("mode", ["success", "warning", "failure", "auth", "degraded"])
@pytest.mark.parametrize("verbose", [False, True])
def test_sync78_real_entrypoint_merged_buffered_pipes(tmp_path, mode, verbose):
    """Actual Typer entrypoint with isolated HOME/store and fake optional boundaries."""
    script = r"""
import os, sys
from pathlib import Path
import keyring
keyring.get_password = lambda *a: None
from mind_meld import config, crypto, events_tail, host_usage, upgrade
mode = sys.argv[1]
upgrade.run_transition_hook = lambda *a, **k: None
upgrade.update_or_nudge = lambda *a, **k: None
events_tail._default_host_readers = lambda *a, **k: ()
root = Path.home()
notes = root / "notes"
notes.mkdir(parents=True)
(notes / "note.md").write_text("synthetic content")
from mind_meld.storage.local import LocalBackend
backend = LocalBackend(str(root / "store"))
crypto.bootstrap_crypto_init(backend, "synthetic78", 1024)
sources = [{"name": "notes", "path": str(notes), "type": "generic", "include_files": ["note.md"]}]
if mode == "degraded":
    events_root = root / "events-root"
    events_root.mkdir()
    sources.append({"name": "mm-events", "path": str(events_root),
                    "type": "generic", "include_dirs": ["events"]})
    events_tail._default_host_readers = lambda *a, **k: ((
        "codex", lambda **kw: host_usage.HostUsageResult({}, False, "unsupported")
    ),)
config.save_config({
    "device": {"id": "dev78", "name": "fixture"},
    "storage": {"path": str(root / "store")},
    "sync": {"sources": sources},
    "crypto": {"argon2_memory_kb": 1024},
})
from mind_meld import cli
mode = sys.argv[1]
if mode == "warning":
    def warn(*a):
        print("mm: warning: large sync growth: synthetic; inspect the source", file=sys.stderr)
    cli._warn_push_growth = warn
    # first-push growth normally needs an accepted baseline; inject a required
    # warning through the real apply-independent scan boundary instead.
    original_build = cli.build_manifest_v2
    def build(*a, **k):
        warn()
        return original_build(*a, **k)
    cli.build_manifest_v2 = build
elif mode == "failure":
    def fail(*a):
        raise RuntimeError("PRIVATE78 /user/path secret") from ValueError("PRIVATE78 cause")
    cli.update_last_seen = fail
cli.app(args=["push", *(["--verbose"] if sys.argv[2] == "yes" else [])])
"""
    env = dict(
        os.environ, HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "home/.config")
    )
    env.pop("MINDMELD_PASSPHRASE", None)
    if mode != "auth":
        env["MINDMELD_PASSPHRASE"] = "synthetic78"
    child = subprocess.run(
        [sys.executable, "-c", script, mode, "yes" if verbose else "no"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )
    assert child.returncode == (1 if mode in ("failure", "auth") else 0), child.stdout
    assert "PRIVATE78" not in child.stdout
    assert "Traceback" not in child.stdout and "Aborted!" not in child.stdout
    last = child.stdout.splitlines()[-1]
    if mode == "failure":
        assert last.startswith("Push failed [unexpected-error]: RuntimeError;")
        assert "1 content change published" in last and last.endswith("Run: mm diag")
        assert ("cli.py:_push_core:" in child.stdout) is verbose
    elif mode == "auth":
        assert last.startswith("Push failed [authentication-error]:")
        assert "MINDMELD_PASSPHRASE" in last and "no content published" in last
    else:
        assert last == "Push: 1 content change published."
        if mode == "success" and not verbose:
            assert child.stdout == last + "\n"
        if mode == "degraded":
            assert ("Usage capture (no-row)" in child.stdout) is verbose
        if mode == "warning":
            assert child.stdout.index("mm: warning: large sync growth") < child.stdout.index(last)


def test_sync78_diagnostics_survive_refusal_are_bounded_and_reset(status77, monkeypatch):
    from mind_meld.errors import SnapshotError

    original = cli_module._push_core

    def core(*args, **kwargs):
        for _ in range(10000):
            cli_module._sync_diagnostic("unsafe /private/path\n", True)
        raise SnapshotError("Cannot publish: unreadable source. Previous snapshot kept.")

    monkeypatch.setattr(cli_module, "_push_core", core)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 1
    assert "unsafe" not in result.output
    run = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert run["counts"]["events_degradations"] == 10000
    assert run["degradations"] == []
    assert run["omitted_details"] == 10000
    monkeypatch.setattr(cli_module, "_push_core", original)
    next_run = runner.invoke(app, ["push"])
    assert next_run.exit_code == 0
    assert (
        cli_module.attemptlog.read_runs()["runs"]["push"]["counts"].get("events_degradations", 0)
        == 0
    )
    assert cli_module._SYNC_OUTPUT.get() is None


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_sync78_broken_stream_preserves_effects_and_never_retries(status77, monkeypatch, stream):
    calls = []
    original = cli_module._push_core

    class Broken:
        def write(self, value):
            raise OSError("PRIVATE78 pipe")

        def flush(self):
            raise OSError("PRIVATE78 pipe")

    def emit(message, **kwargs):
        # Break just the actual terminal emission, after all durable evidence.
        with monkeypatch.context() as patch:
            patch.setattr(sys, stream, Broken())
            cli_module._emit_sync_outcome_original78(message, **kwargs)

    monkeypatch.setattr(
        cli_module, "_emit_sync_outcome_original78", cli_module._emit_sync_outcome, raising=False
    )
    monkeypatch.setattr(cli_module, "_emit_sync_outcome", emit)

    def core(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(cli_module, "_push_core", core)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 0
    assert calls == [True]
    assert cli_module.attemptlog.read_runs()["runs"]["push"]["outcome"] == "success"


def test_sync78_no_peers_and_no_sources_are_explicit(status77):
    no_peers = runner.invoke(app, ["pull"])
    assert no_peers.exit_code == 0, no_peers.output
    assert no_peers.stdout == "Pull: no peer devices.\n"
    cfg = status77["cfg"]
    cfg["sync"]["sources"] = []
    save_config(cfg, status77["path"])
    no_sources = runner.invoke(app, ["push"])
    assert no_sources.exit_code == 0, no_sources.output
    assert no_sources.output.splitlines()[-1] == "Push: no enabled sources."


@pytest.mark.parametrize("interrupt", [False, True])
def test_sync78_pull_update_eligibility_and_partial_exit(
    apply_failure_fleet, monkeypatch, interrupt
):
    fleet = apply_failure_fleet
    fleet.publish({"aaa.txt": b"a", "bbb/inside.txt": b"b"})
    (fleet.memory / "bbb").write_bytes(b"mine")
    updates = []

    def update(*args, **kwargs):
        cli_module.acquire_lock()
        cli_module.release_lock()
        print("actionable update tail", file=sys.stderr)
        updates.append(True)

    monkeypatch.setattr(cli_module.upgrade, "update_or_nudge", update)
    if interrupt:
        monkeypatch.setattr(
            cli_module,
            "_pull_one_source",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    result = runner.invoke(app, ["pull"])
    assert result.exit_code == 1
    assert updates == ([] if interrupt else [True])
    assert result.output.splitlines()[-1].startswith(
        "Pull interrupted" if interrupt else "Pull incomplete [result-degraded]"
    )
    run = cli_module.attemptlog.read_runs()["runs"]["pull"]
    assert run["outcome"] == ("failed" if interrupt else "degraded")
    if not interrupt:
        assert run["counts"]["total_written"] == run["counts"]["total_failed"] == 1
        assert (fleet.memory / "aaa.txt").read_bytes() == b"a"
        assert "actionable update tail" in result.stderr


def test_sync78_activity_degradation_survives_later_upload_refusal(capture61, monkeypatch):
    from mind_meld.errors import SnapshotError

    (capture61["claude"] / "projects/-Users-kb-myapp/memory/refused78.md").write_text("pending")
    monkeypatch.setattr(events_tail, "_default_host_readers", lambda *a, **k: ())
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **k: _mm_events.GitRootDiscovery((), ("budget exhausted",), True),
    )
    monkeypatch.setattr(cli_module.identity, "gather_local_identities", lambda **k: [])

    def refuse(*args, **kwargs):
        raise SnapshotError("Cannot publish: fixture upload refused. Previous snapshot kept.")

    monkeypatch.setattr(cli_module, "_upload_changed_blobs", refuse)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 1
    assert "discovery hit its time budget" not in result.output
    record = cli_module.attemptlog.read_runs()["runs"]["push"]
    assert record["cause"] == "snapshot-refused"
    assert record["counts"]["events_degradations"] > 0
    assert events_tail._ROOT_DISCOVERY_DEGRADATION in record["degradations"]


_NEWER78 = cli_module._newer_format_message(3, "manifest")
_MISSING_ROOT78 = (
    "Cannot publish: source notes is missing after it was previously published. "
    "Previous snapshot kept. Restore the source folder, or run 'mm disable-source notes' "
    "if it should stop syncing. See https://example.test/#snapshot."
)


@pytest.mark.parametrize(
    ("state", "error", "expected"),
    [
        (
            {"message": _MISSING_ROOT78, "run": {"cause": "snapshot-refused"}},
            typer.Exit(1),
            "Push refused [snapshot-refused]: Cannot publish: source notes is missing after it "
            "was previously published. Previous snapshot kept. Restore the source folder, or run "
            "'mm disable-source notes' if it should stop syncing; no content published. "
            "Run: mm push",
        ),
        (
            {
                "message": "Cannot publish: a.md changed while being read. Previous snapshot "
                "kept. Wait for the writer to finish, then run mm push. See https://x.test/.",
                "run": {"cause": "snapshot-refused"},
            },
            typer.Exit(1),
            "Push refused [snapshot-refused]: Cannot publish: a.md changed while being read. "
            "Previous snapshot kept. Wait for the writer to finish; no content published. "
            "Run: mm push",
        ),
        (
            {"message": "wrong passphrase", "run": {"cause": "crypto-error"}},
            typer.Exit(1),
            "Push failed [crypto-error]: wrong passphrase; no content published. Run: mm diag",
        ),
        (
            {"run": {"cause": "storage-error", "stage": "command-refused"}},
            cli_module.StorageError("storage: failed to read devices.json"),
            "Push failed [storage-error]: storage: failed to read devices.json; "
            "effects unconfirmed; reconcile before retrying. Run: mm diag",
        ),
        (
            {"run": {"cause": "lock-held"}},
            typer.Exit(1),
            "Push refused [lock-held]: another mm operation holds the lock; wait for it to "
            "finish; no content published. Run: mm diag",
        ),
        # Value: protects=exit-3 and curated refusals end with their own remedy command;
        # fails_when=preflight_failures selection inverts or a curated remedy maps to mm diag;
        # why_new=exit-3 and mm update/mm init rows had prefix-only or no coverage;
        # seam=none; no_claim=which producer emits each message (pure renderer)
        (
            {"preflight": "conflict; conflict", "run": {"cause": "command-refused"}},
            typer.Exit(3),
            "Pull refused [command-refused]: preflight predicted conflict; conflict; "
            "no files applied. Run: mm pull --conflict-mode keep-both",
        ),
        (
            {
                "preflight": "may fail (local file unreadable)",
                "preflight_failures": True,
                "run": {"cause": "command-refused"},
            },
            typer.Exit(3),
            "Pull refused [command-refused]: preflight predicted may fail (local file "
            "unreadable); no files applied. Run: mm diag",
        ),
        (
            {"message": _NEWER78, "run": {"cause": "crypto-error"}},
            typer.Exit(1),
            f"Pull failed [crypto-error]: {_NEWER78.rstrip('. ')}; "
            "no files applied. Run: mm update",
        ),
        (
            {"message": "Not initialized. Run mm init first.", "run": {"stage": "setup-error"}},
            typer.Exit(1),
            "Pull failed [setup-error]: Not initialized. Run mm init first; no files applied. "
            "Run: mm init",
        ),
    ],
)
def test_sync78_failure_line_keeps_curated_remedy_and_known_effects(state, error, expected):
    verb = expected.split()[0].lower()
    assert cli_module._sync_failure(verb, state, error) == expected


def test_sync78_outcome_counts_are_singular_at_one():
    pull = cli_module.PullResult(total_written=1)
    assert cli_module._sync_success("pull", pull) == "Pull: 1 file applied."
    push = cli_module.PushResult(published_content_files=1)
    assert cli_module._sync_success("push", push) == "Push: 1 content change published."
    partial = cli_module.PullResult(total_written=1, total_failed=1)
    line = cli_module._sync_failure("pull", {"run": {"result": partial}}, None)
    assert line.startswith("Pull incomplete [result-degraded]: 1 file applied, 1 failed;")
    # Value: protects=conflict copies, skips and no-source state stay on the one pull line;
    # fails_when=the conflict/skip suffix or no_sources precedence is dropped;
    # why_new=no test reached these _sync_success branches; seam=none;
    # no_claim=which pull paths set these counts (pure renderer)
    one = cli_module.PullResult(total_written=1, total_conflicted=1)
    assert cli_module._sync_success("pull", one) == "Pull: 2 files applied (1 conflict copy)."
    many = cli_module.PullResult(total_conflicted=2, total_skipped=1)
    assert (
        cli_module._sync_success("pull", many)
        == "Pull: 2 files applied (2 conflict copies); 1 skipped."
    )
    empty = cli_module.PullResult(no_peers=True, no_sources=True)
    assert cli_module._sync_success("pull", empty) == "Pull: no enabled sources."


@pytest.mark.parametrize("verb", ["push", "pull"])
def test_sync78_lock_held_is_one_refusal_without_update(status77, monkeypatch, verb):
    def held():
        raise cli_module.LockError("another mm process holds the lock")

    updates = []
    monkeypatch.setattr(cli_module, "acquire_lock", held)
    monkeypatch.setattr(cli_module.upgrade, "update_or_nudge", lambda *a, **k: updates.append(1))
    result = runner.invoke(app, [verb])
    assert result.exit_code == 1
    last = result.stderr.splitlines()[-1]
    assert last.startswith(f"{verb.title()} refused [lock-held]:")
    assert last.endswith("Run: mm diag")
    assert result.output.count(f"{verb.title()} refused") == 1
    assert updates == []


@pytest.mark.parametrize("terminal", [False, True])
def test_sync78_passphrase_env_guidance_only_without_terminal(status77, monkeypatch, terminal):
    def cancelled(*args, **kwargs):
        raise cli_module.CryptoError("init: passphrase input cancelled.")

    monkeypatch.setattr(cli_module, "get_passphrase", cancelled)
    monkeypatch.setattr(cli_module, "_stdin_is_terminal", lambda: terminal)
    result = runner.invoke(app, ["push"])
    assert result.exit_code == 1
    last = result.stderr.splitlines()[-1]
    assert last.startswith("Push failed [authentication-error]:")
    assert ("MINDMELD_PASSPHRASE" in last) is (not terminal)
    assert ("input cancelled" in last) is terminal


def test_sync78_no_source_push_records_no_changes(status77):
    cfg = status77["cfg"]
    cfg["sync"]["sources"] = []
    save_config(cfg, status77["path"])
    result = runner.invoke(app, ["push"])
    assert result.output.splitlines()[-1] == "Push: no enabled sources."
    assert cli_module.attemptlog.read_runs()["runs"]["push"]["cause"] == "no-changes"


def test_sync78_pull_plain_warnings_strip_control_characters(capsys):
    peer = cli_module._CorruptPeer(device_id="id\r\x1b[2Jx", device_name="Mac\rFAKE OK\n")
    cli_module._print_pull_summary(
        cli_module.PullResult(), [peer], [], [], [], quiet=True, verbose=False
    )
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "\r" not in err and "\x1b" not in err
    assert "is corrupt - skipping pull from this device." in err


@pytest.mark.parametrize("command", ["push", "autopush"])
@pytest.mark.parametrize("fault", ["whole-walk", "host-lock", "reader-raise", "leaf-notice"])
def test_sync78_each_activity_loss_is_recorded_once(capture61, monkeypatch, fault, command):
    # Value: protects=one activity loss is counted and recorded once, with no notice text
    # persisted; fails_when=a scoped notice whose result already records the loss is also
    # counted (degraded=True), or an unknown leaf notice is dropped or persisted verbatim;
    # why_new=leaf tests pin the degraded flag only, the bounded test injects the sink
    # directly; seam=none; no_claim=real Git/host stores (pump, reader and lock faked
    # in-process; discovery fixed to tmp roots)
    from contextlib import nullcontext

    from mind_meld import lockedjson

    roots: list[Path] = []
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda *a, **k: _mm_events.GitRootDiscovery(tuple(roots), ()),
    )
    memory = capture61["claude"] / "projects/-Users-kb-myapp/memory"
    # Prime once with deterministic discovery so the fixture's machine-dependent
    # first capture cannot leave a held cursor behind as unrelated noise.
    (memory / "prime78.md").write_text("prime")
    assert runner.invoke(app, ["push"]).exit_code == 0
    if fault == "whole-walk":
        roots.append(capture61["path"].parent / "repo78")
        roots[0].mkdir()

        def pump(*args, **kwargs):
            raise RuntimeError("PRIVATE78 pump")
            yield

        monkeypatch.setattr(_mm_events, "as_completed", pump)
    elif fault == "reader-raise":

        def raising(**kwargs):
            raise RuntimeError("PRIVATE78 reader")

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", raising)
    elif fault == "leaf-notice":

        def noisy(**kwargs):
            _mm_host_usage._notice_cache_write_failure(OSError(errno.EIO, "PRIVATE78"))
            return _mm_host_usage.HostUsageResult({}, complete=True)

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", noisy)
    (memory / "loss78.md").write_text("changed")
    held = (
        lockedjson.locked_json_rmw(_mm_host_usage.CACHE_PATH)
        if fault == "host-lock"
        else nullcontext()
    )
    with held:
        result = runner.invoke(app, [command])
    assert result.exit_code == 0, result.output
    assert "PRIVATE78" not in result.output
    for notice in ("whole-walk failure", "was locked; skipping", "raised", "write failed"):
        assert notice not in result.output
    if command == "push":
        (line,) = result.stdout.splitlines()
        assert line.startswith("Push: 1 content change published")
        run = cli_module.attemptlog.read_runs()["runs"]["push"]
        assert run["outcome"] == "degraded"
        details = run["degradations"]
        count = run["counts"].get("events_degradations", 0)
        if fault == "whole-walk":
            assert count == 1
            assert len(details) == 1 and details[0].startswith("git walk dropped 1 repositories")
        elif fault == "leaf-notice":
            assert count == 1 and details == [] and run["omitted_details"] == 1
        else:
            # The attended capture owns this loss; the tail does not read hosts again.
            assert count == 0 and details == ["usage capture no-row (none)"]
    else:
        assert result.stdout == result.stderr == ""
        crumb = cli_module._read_autorun_breadcrumbs()["push"]
        assert crumb["outcome"] == "degraded"
        detail = crumb["detail"]
        assert "PRIVATE78" not in detail and "EIO" not in detail
        expected = {
            "whole-walk": "git walk dropped 1 repositories",
            "host-lock": "host-usage snapshot skipped (codex locked)",
            "reader-raise": "host-usage snapshot skipped (codex unavailable)",
            "leaf-notice": "1 diagnostic details omitted (run mm diag)",
        }[fault]
        assert detail.startswith(expected) and detail.count(expected) == 1
        assert detail.count("git walk dropped") + detail.count("snapshot skipped") <= 1
        if fault != "leaf-notice":
            assert "diagnostic details omitted" not in detail


def test_sync78_remaining_remedy_commands_select_their_run_command():
    # Value: protects=unsupported-format and migrate-config refusals end on their own command;
    # fails_when=those substrings fall through to mm diag or repeat the command in the reason;
    # why_new=the failure table covers newer-mm, init, and devices only;
    # seam=none; no_claim=which producer raises each message
    unsupported = cli_module._sync_failure(
        "pull",
        {
            "message": ("decrypt: unsupported format version 0x03. Expected 0x02. Update mm?"),
            "run": {"cause": "crypto-error"},
        },
        typer.Exit(1),
    )
    assert unsupported == (
        "Pull failed [crypto-error]: decrypt: unsupported format version 0x03. "
        "Expected 0x02. Update mm?; no files applied. Run: mm update"
    )
    assert "newer mm" not in unsupported
    migrated = cli_module._sync_failure(
        "push",
        {
            "message": "Recommended excludes are missing, then run mm migrate-config.",
            "run": {"stage": "setup-error"},
        },
        typer.Exit(1),
    )
    assert migrated == (
        "Push failed [setup-error]: Recommended excludes are missing; "
        "no content published. Run: mm migrate-config"
    )
    assert migrated.count("mm migrate-config") == 1


def test_sync78_pull_with_a_peer_and_no_sources_names_sources(status77):
    # Value: protects=an attended pull with a peer and no enabled sources says so;
    # fails_when=the peer-path result omits no_sources and reports no changes;
    # why_new=the live no-peer pull and the renderer test never take this path;
    # seam=none; no_claim=apply behavior when sources are enabled
    register_device(status77["backend"], "dev-peer78", "Peer78")
    cfg = status77["cfg"]
    cfg["sync"]["sources"] = []
    save_config(cfg, status77["path"])
    result = runner.invoke(app, ["pull", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "Pulling from Peer78" in result.output
    assert result.stdout.splitlines()[-1] == "Pull: no enabled sources."


def test_sync78_pre_write_refusals_and_verbose_trace_cause():
    line = cli_module._sync_failure(
        "pull",
        {"message": "Device not found: Mac Z", "run": {"cause": "command-refused"}},
        typer.Exit(1),
    )
    assert line == (
        "Pull failed [command-refused]: Device not found: Mac Z; no files applied. Run: mm devices"
    )
    try:
        try:
            raise RuntimeError("PRIVATE78 keyring detail")
        except RuntimeError:
            raise typer.Exit(1)
    except typer.Exit as exit_error:
        trace = cli_module._compact_sync_trace(exit_error)
    assert trace.startswith("RuntimeError (no errno):")
    assert "PRIVATE78" not in trace


def test_sync78_lock_failure_keeps_its_own_text_and_conflicts_name_review(capsys):
    line = cli_module._sync_failure(
        "push",
        {"message": "Could not open lockfile L: permission denied", "run": {"cause": "lock-held"}},
        typer.Exit(1),
    )
    assert line.startswith(
        "Push refused [lock-held]: Could not open lockfile L: permission denied;"
    )
    token = cli_module._SYNC_OUTPUT.set(
        {"verbose": False, "diagnostics": events_tail.Degradations()}
    )
    try:
        cli_module._print_pull_summary(
            cli_module.PullResult(total_written=1, total_conflicted=1),
            [],
            [],
            [],
            [],
            quiet=True,
            verbose=False,
        )
    finally:
        cli_module._SYNC_OUTPUT.reset(token)
    err = capsys.readouterr().err
    assert "1 conflict copy kept; run mm conflicts to review, mm resolve to pick a winner" in err
