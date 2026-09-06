"""Integration tests for Mind Meld — full push/pull round-trips."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
from datetime import datetime, timezone
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
from mind_meld.devices import list_devices, register_device
from mind_meld.manifest import (
    deserialize_manifest,
    load_manifest,
    normalize_manifest,
    serialize_manifest,
)
from mind_meld.storage.local import LocalBackend
from mind_meld.synclog import write_sync_log

PASSPHRASE = "integration-test-passphrase"
MEMORY_KB = 1024  # Low for fast tests
runner = CliRunner()


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
        """Peer manifest contains an excluded-path blob. mm gc reads RAW
        manifests via `_fetch_remote_manifest` (no filter applied at fetch
        boundary), so the blob remains referenced and is NOT deleted.

        Pins codex-2 #1: without the no-filter-at-fetch invariant, gc
        treats an excluded-path blob as orphan and silently deletes it,
        breaking peers that haven't yet adopted the exclude.
        """
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

        assert blob_path.exists(), (
            "mm gc deleted a blob that's still referenced by a peer "
            "manifest. The exclude filter must NOT apply at the fetch "
            "boundary used by gc — the consumer-boundary contract."
        )


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
        """A peer manifest references gstack blobs. Local has gstack disabled.
        `mm gc` reads RAW peer manifests via _fetch_remote_manifest (no filter
        applied at fetch boundary), so the gstack blobs remain referenced and
        are NOT deleted. Without this invariant, gc would orphan and silently
        delete blobs that peers still reference, breaking sync for peers who
        still have gstack enabled.
        """
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

        assert blob_path.exists(), (
            "mm gc deleted a gstack blob that's still referenced by peer A's "
            "manifest. The disabled-source filter must NOT apply at the fetch "
            "boundary used by gc — the consumer-boundary contract."
        )

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
        monkeypatch.setattr(cli_module, "_maybe_prompt_migration", lambda _config: None)
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
        monkeypatch.setattr(cli_module, "_maybe_prompt_migration", lambda _config: None)
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

    def _write_explicit_config(self, tmp_path, *, exclude_patterns=None):
        config_path = tmp_path / "config.toml"
        gstack_src: dict = {
            "name": "gstack",
            "path": str(tmp_path / ".gstack"),
            "type": "generic",
            "include_dirs": ["projects"],
        }
        if exclude_patterns is not None:
            gstack_src["exclude_patterns"] = exclude_patterns
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
            tmp_path, exclude_patterns=list(gstack_default["exclude_patterns"])
        )
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output
        assert "already up to date" in result.output

    def test_adds_missing_recommended_excludes(self, tmp_path, monkeypatch):
        """A source without exclude_patterns gets the recommended set."""
        config_path = self._write_explicit_config(tmp_path, exclude_patterns=None)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)
        # Auto-confirm the inner "Apply these updates?" prompt.
        result = runner.invoke(app, ["migrate-config", "--yes"])
        assert result.exit_code == 0, result.output

        loaded = config_module.load_config(config_path)
        gstack = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack")
        assert "projects/*/repo-mode.json" in gstack["exclude_patterns"]
        assert "projects/*/land-deploy-confirmed" in gstack["exclude_patterns"]
        assert "config.yaml" in gstack["exclude_patterns"]  # v0.9.3

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        config_path = self._write_explicit_config(tmp_path, exclude_patterns=None)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        self._redirect_lock(monkeypatch, tmp_path)

        result = runner.invoke(app, ["migrate-config", "--dry-run"])
        assert result.exit_code == 0, result.output
        loaded = config_module.load_config(config_path)
        gstack = next(s for s in loaded["sync"]["sources"] if s["name"] == "gstack")
        # exclude_patterns NOT added.
        assert "exclude_patterns" not in gstack or not gstack["exclude_patterns"]

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
        cli_module._maybe_prompt_migration(config)
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
    def test_gc_never_deletes_referenced_blobs(self, storage):
        """GC must check ALL manifests before deleting.

        Manifests use the v2 shape (sources dict) that production emits, and
        the ref-counting loop reads via `load_manifest` so it exercises the
        same normalization path `_do_gc` hits (see cli.py:_do_gc).
        """

        # Device A has hash1 and hash2
        manifest_a = {
            "device_id": "a",
            "device_name": "A",
            "timestamp": "2026-01-01T00:00:00Z",
            "sources": {
                "claude": {
                    "base_path": "/tmp",
                    "files": {
                        "file1.json": {
                            "sha256": "hash1",
                            "size": 100,
                            "mtime": "2026-01-01T00:00:00Z",
                        },
                        "file2.json": {
                            "sha256": "hash2",
                            "size": 200,
                            "mtime": "2026-01-01T00:00:00Z",
                        },
                    },
                },
            },
            "tombstones": {},
        }

        # Device B has only hash1
        manifest_b = {
            "device_id": "b",
            "device_name": "B",
            "timestamp": "2026-01-01T00:00:00Z",
            "sources": {
                "claude": {
                    "base_path": "/tmp",
                    "files": {
                        "file1.json": {
                            "sha256": "hash1",
                            "size": 100,
                            "mtime": "2026-01-01T00:00:00Z",
                        },
                    },
                },
            },
            "tombstones": {},
        }

        register_device(storage, "a", "A")
        register_device(storage, "b", "B")

        # Store manifests
        for did, manifest in [("a", manifest_a), ("b", manifest_b)]:
            enc = encrypt(serialize_manifest(manifest), PASSPHRASE, memory_kb=MEMORY_KB)
            storage.put(f"manifests/{did}/manifest.json.enc", enc)

        # Store blobs
        storage.put("data/a/hash1.enc", encrypt(b"blob1", PASSPHRASE, memory_kb=MEMORY_KB))
        storage.put("data/a/hash2.enc", encrypt(b"blob2", PASSPHRASE, memory_kb=MEMORY_KB))
        storage.put("data/a/hash3.enc", encrypt(b"orphan", PASSPHRASE, memory_kb=MEMORY_KB))

        # Collect referenced hashes from ALL manifests — same shape _do_gc uses.
        all_devices = list_devices(storage)
        referenced = set()
        for d in all_devices:
            did = d["device_id"]
            key = f"manifests/{did}/manifest.json.enc"
            if storage.exists(key):
                enc = storage.get(key)
                plain = decrypt(enc, PASSPHRASE, memory_kb=MEMORY_KB)
                m = load_manifest(plain)
                for src_data in m["sources"].values():
                    for info in src_data["files"].values():
                        referenced.add(info["sha256"])

        # hash1 and hash2 are referenced, hash3 is not
        assert "hash1" in referenced
        assert "hash2" in referenced
        assert "hash3" not in referenced

        # GC should only delete hash3
        all_blobs = storage.list_keys("data/")
        orphans = []
        for blob_key in all_blobs:
            if not blob_key.endswith(".enc"):
                continue
            parts = blob_key.split("/")
            if len(parts) == 3:
                sha = parts[2].removesuffix(".enc")
                if sha not in referenced:
                    orphans.append(blob_key)

        assert len(orphans) == 1
        assert "hash3" in orphans[0]


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
        """autopush should push changes and print a one-line summary."""
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
        assert "mm: pushed" in result.output
        assert "1 new" in result.output


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
        assert "mm: pushed" in result.output

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
        # Conflict copy is gone after canonicalization.
        assert not (storage / "mm-crypto-init 2").exists()
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


class TestTrack7BEventsTail:
    """Track 7B (v0.10.3): per-push events tail at HEAD of ``_push_core``.

    See CLAUDE.md "Events tail in _push_core (load-bearing, v0.10.3)" for
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

    def _read_events(self, events_file: Path) -> list[dict]:
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
        rows = self._read_events(events_files[0])
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
        first_rows = self._read_events(first[0])
        first_mtime = first[0].stat().st_mtime_ns

        # Second push: nothing changed in claude_a → empty push. Events
        # file must NOT gain a new row; mtime must NOT advance; the file
        # must remain the only events file.
        assert runner.invoke(app, ["push"]).exit_code == 0
        second_rows = self._read_events(first[0])
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
        third_rows = self._read_events(first[0])
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
        rows = self._read_events(self._events_files(self._events_dir(tmp_path))[0])
        types = [r["type"] for r in rows]
        assert types.index("host-usage-snapshot") < types.index("mm-push")
        assert types[-1] == "mm-push"
        host_row = rows[types.index("host-usage-snapshot")]
        assert host_row["hosts"]["codex"]["2026-08-15"]["input"] == 12
        assert host_row["active_days"] == ["2026-08-15"]

    def test_no_content_push_touches_no_host_reader(self, tmp_path, monkeypatch):
        """Zero-work gate: the substantive-change gate short-circuits before
        the tail, so an empty push must not open a host store or its cache
        either. Host reads are optional analytics — they never pay for a
        push that has nothing to say."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        self._seed_claude(claude_a)
        self._bootstrap(storage_dir)
        register_device(LocalBackend(storage_dir), "dev-a", "A")

        config = self._make_config_with_events(tmp_path, storage_dir, claude_a, "dev-a", "A")
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
        assert "Nothing to push" in result.output
        assert calls == []

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
        first_rows = self._read_events(first_files[0])
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
        assert [row for row in self._read_events(first_files[0]) if row["type"] == "mm-push"][-1][
            "ts"
        ] == first_cursor

        (claude_a / "projects" / "-Users-kb-myapp" / "memory" / "trigger.md").write_text(
            "substantive"
        )
        assert runner.invoke(app, ["push"]).exit_code == 0
        rows = [
            row
            for event_file in self._events_files(self._events_dir(tmp_path))
            for row in self._read_events(event_file)
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
        rows = self._read_events(self._events_files(self._events_dir(tmp_path))[0])
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
        rows = self._read_events(self._events_files(self._events_dir(tmp_path))[0])
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
        rows = self._read_events(self._events_files(self._events_dir(tmp_path))[0])
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
        assert "Nothing to push" not in result.output, (
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
        assert "Nothing to push" in result.output, (
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
        assert "All sources in sync" not in result.output, (
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
        result = runner.invoke(app, ["push", *(extra_args or [])])
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
        assert "Push complete" not in result.output

    def test_shared_hash_poison_is_refused_and_old_blob_kept(self, tmp_path, monkeypatch):
        from mind_meld.crypto import decrypt
        from mind_meld.storage.keys import blob_key

        env = self._prepare(tmp_path, monkeypatch)
        first = self._push()
        assert first.exit_code == 0, first.output
        prior = self._prior(env)
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
        self._assert_prior_kept(env, prior, result)
        plain = decrypt(old_blob, "shared-cli-test-passphrase", 1024)
        assert plain == b"old bytes"

    def test_missing_upload_input_refuses(self, tmp_path, monkeypatch):
        env = self._prepare(tmp_path, monkeypatch)
        assert self._push().exit_code == 0
        prior = self._prior(env)
        (env["memory"] / "c.md").write_text("new")

        real_upload = cli_module._upload_changed_blobs

        def drop_then_upload(*a, **kw):
            (env["memory"] / "c.md").unlink()
            return real_upload(*a, **kw)

        monkeypatch.setattr(cli_module, "_upload_changed_blobs", drop_then_upload)
        result = self._push()
        self._assert_prior_kept(env, prior, result)

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
        assert "Nothing to push" in second.output or "up to date" in second.output
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
