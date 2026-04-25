"""Integration tests for Mind Meld — full push/pull round-trips."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import cli as cli_module
from mind_meld import config as config_module
from mind_meld import crypto as crypto_module
from mind_meld import fsutil, synclog
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

    def test_kb_mbp_regression_no_conflicts_for_excluded_paths(self, tmp_path, monkeypatch):
        """kb-mbp 2026-04-24: A pushes per-machine artifacts WITHOUT the
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
        from mind_meld.cli import _resolve_interactive_loop

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
        from mind_meld.cli import _resolve_interactive_loop

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
        # User picks 'b' (kept both — no further mutation).
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "b")

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
            _ensure_inversion_marker,
            _migrate_pre_inversion_conflict,
            conflict_filename,
        )
        from mind_meld.manifest import is_pre_inversion_conflict_filename

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

    def test_pre_inversion_file_still_migrated_when_older_than_marker(self, tmp_path, monkeypatch):
        """F1 fix complement: pre-existing legacy files (mtime < marker)
        must still be migrated. The fix is a SAFETY gate, not a
        disable-migration switch."""
        from mind_meld.cli import (
            _ensure_inversion_marker,
            _migrate_pre_inversion_conflict,
        )
        from mind_meld.manifest import is_pre_inversion_conflict_filename

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
        from mind_meld.cli import _migrate_pre_inversion_conflict

        legacy = tmp_path / "doc.sync-conflict-20260420-120000-devA1234.md"
        legacy.write_bytes(b"local bytes")
        old = time.time() - 86400
        os.utime(legacy, (old, old))

        # Force the marker helper to return None.
        monkeypatch.setattr("mind_meld.cli._ensure_inversion_marker", lambda: None)
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
        # Breadcrumb confirms the silent-exit went through the config-missing
        # branch, not the broader exception-fallback path.
        breadcrumb_path = sidecar_dir / "last-autorun.json"
        assert breadcrumb_path.exists()
        breadcrumb = json.loads(breadcrumb_path.read_text())
        assert breadcrumb["outcome"] == "config-missing"

    def test_autopush_no_config_exits_silently(self, tmp_path, monkeypatch):
        """autopush silent-mode twin of test_autopull_no_config_exits_silently."""
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "nonexistent.toml")
        sidecar_dir = tmp_path / "sidecar"
        monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", sidecar_dir)
        result = runner.invoke(app, ["autopush"])
        assert result.exit_code == 0
        assert result.output == ""
        breadcrumb_path = sidecar_dir / "last-autorun.json"
        assert breadcrumb_path.exists()
        breadcrumb = json.loads(breadcrumb_path.read_text())
        assert breadcrumb["outcome"] == "config-missing"

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
        assert breadcrumb["outcome"] == "keyring-error"
        assert breadcrumb["detail"] == "RuntimeError"

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
        """Regression: _save_and_register must not abort on a non-KeyringError
        from store_passphrase_in_keyring after config + device are already
        committed. Old wide `except Exception` inside the helper swallowed
        everything; v0.8.9's narrowing made non-KeyringError propagate. The
        follow-through fix wraps the call at the _save_and_register call site
        so init degrades to the env-var-fallback path cleanly instead of
        leaving the user half-initialized with an uncaught traceback."""
        config_path = tmp_path / "config.toml"
        storage = tmp_path / "icloud"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        # Force store_passphrase_in_keyring to leak a non-KeyringError.
        import mind_meld.cli as cli_mod

        def boom(*_a, **_kw):
            raise RuntimeError("keyring backend exploded")

        monkeypatch.setattr(cli_mod, "store_passphrase_in_keyring", boom)

        # Inputs match TestInitFlow pattern: storage, device name, passphrase,
        # confirm passphrase, then source prompts (Y claude, n gstack).
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\n"
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
        # then per-source prompts (claude Y, gstack n) — we need at least
        # one source enabled for init to succeed.
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\n"
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

        # Decline both claude and gstack.
        stdin = f"{storage}\nMac A\npw123\npw123\nn\nn\n"
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
        stdin1 = f"{storage}\nMac A\npw-shared\npw-shared\nn\nn\n"
        result1 = runner.invoke(app, ["init"], input=stdin1)
        assert result1.exit_code != 0

        # Second attempt: same passphrase, accept claude. Takes the
        # second-device path against the orphaned bootstrap.
        stdin2 = f"{storage}\nMac A\npw-shared\nY\nn\n"
        result2 = runner.invoke(app, ["init"], input=stdin2)
        assert result2.exit_code == 0, result2.output
        assert "Verified passphrase against existing mm-crypto-init" in result2.output

        # Config now written, with claude enabled.
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        assert [s["name"] for s in cfg["sync"]["sources"]] == ["claude"]

    def test_first_device_gstack_only_init(self, tmp_path, monkeypatch):
        """Decline claude, accept gstack → sources list has gstack only."""

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        # Decline claude, accept gstack.
        stdin = f"{storage}\nMac A\npw123\npw123\nn\nY\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output

        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        names = [s["name"] for s in cfg["sync"]["sources"]]
        assert names == ["gstack"]
        # DEFAULT_SOURCES fields survive the indirection (Issue 1C regression pin).
        gstack = cfg["sync"]["sources"][0]
        assert "projects" in gstack["include_dirs"]
        assert "config.yaml" in gstack["include_files"]

    def test_first_device_both_sources_init(self, tmp_path, monkeypatch):
        """Accept both → sources list has claude AND gstack."""

        cfg_path = self._setup_monkeypatch(tmp_path, monkeypatch)
        storage = tmp_path / "icloud"

        stdin = f"{storage}\nMac A\npw123\npw123\nY\nY\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output

        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        names = [s["name"] for s in cfg["sync"]["sources"]]
        assert names == ["claude", "gstack"]
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
        # then per-source prompts (claude Y, gstack n).
        stdin = f"{storage}\nMac B\npw-shared\nY\nn\n"
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
        # per-source (claude Y, gstack n).
        stdin = f"{storage}\ny\nMac B\npw-shared\nY\nn\n"
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
        # device name, passphrase, confirm passphrase, per-source (claude Y, gstack n).
        stdin = f"{storage}\nBRICK\nMac A\npw-new\npw-new\nY\nn\n"
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
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\n"
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

        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\n"
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
