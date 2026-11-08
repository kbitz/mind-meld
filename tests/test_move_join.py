"""Integration tests for moving a machine from iCloud Drive to Syncthing (Track 120A)."""

from __future__ import annotations

import hashlib
import os
import time
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import config as config_module
from mind_meld import core, initflow, pullplan, statedb
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "move-join-test-passphrase"
MEMORY_KB = 1024
PROJECT_REL = "projects/-Users-kb-myapp/memory"
SHARED_FILES = (
    "untouched.md",
    "peer_ahead.md",
    "local_edit.md",
    "both_changed.md",
    "local_newer.md",
    "MEMORY.md",
    "peer_deleted.md",
)

runner = CliRunner()


def _write_at(path: Path, data: bytes, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def _v0_bytes(name: str) -> bytes:
    if name == "MEMORY.md":
        return b"# Memory\n- base fact from phase 1\n"
    return f"v0 content for {name}\n".encode()


def _snapshot_tree(root: Path, *, ignore_names: set[str] = frozenset()) -> dict[Path, bytes]:
    return {
        p.relative_to(root): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name not in ignore_names
    }


def _configure_init_patches(monkeypatch: pytest.MonkeyPatch, claude_dir: Path) -> None:
    monkeypatch.setattr(
        initflow,
        "_prompt_sources",
        lambda: [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )
    monkeypatch.setattr(initflow, "DEFAULT_ARGON2_MEMORY_KB", MEMORY_KB)
    monkeypatch.setattr(initflow, "store_passphrase_in_keyring", lambda _pw: False)
    monkeypatch.setattr(initflow.events_tail, "_run_events_backfill", lambda *a, **kw: None)
    monkeypatch.setattr(initflow.skill_link, "_ensure_retro_skill_links", lambda **kw: ())


def _make_phase1_config(
    config_path: Path, storage_dir: Path, claude_dir: Path, device_id: str, device_name: str
) -> None:
    save_config(
        {
            "device": {"id": device_id, "name": device_name},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [{"name": "claude", "path": str(claude_dir), "type": "claude"}],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        config_path,
    )


@pytest.mark.parametrize("scenario", ["moved-first", "fresh"])
def test_machine_with_icloud_history_joins_fresh_fleet_without_losing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Track 120A: a machine with populated state.db and unpushed local edits joins
    a separate fresh store without losing or deleting any file."""
    now = int(time.time())
    day = 86_400
    t0 = now - 10 * day
    t_peer_older = now - 3 * day
    t_local = now - 2 * day
    t_peer_newer = now - 1 * day

    icloud_dir = tmp_path / "icloud"
    sync_dir = tmp_path / "sync"
    claude_a = tmp_path / "machine_a" / ".claude"
    claude_b = tmp_path / "machine_b" / ".claude"
    memory_a = claude_a / PROJECT_REL
    memory_b = claude_b / PROJECT_REL
    memory_b.mkdir(parents=True, exist_ok=True)

    config_a = tmp_path / "config_a.toml"
    config_b = tmp_path / "config_b.toml"

    # Phase 1: populate the iCloud-era store and sync Mac A <-> Mac B so Mac A's
    # state.db has peer_base and held seen_version rows for every shared file.
    icloud_backend = LocalBackend(icloud_dir)
    bootstrap_crypto_init(icloud_backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(icloud_backend, "dev-a", "Mac A")
    register_device(icloud_backend, "dev-b", "Mac B")
    _make_phase1_config(config_a, icloud_dir, claude_a, "dev-a", "Mac A")
    _make_phase1_config(config_b, icloud_dir, claude_b, "dev-b", "Mac B")

    for name in SHARED_FILES:
        _write_at(memory_a / name, _v0_bytes(name), t0)

    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    monkeypatch.setattr(config_module, "CONFIG_PATH", config_a)
    res = runner.invoke(app, ["push"])
    assert res.exit_code == 0, res.output

    monkeypatch.setattr(config_module, "CONFIG_PATH", config_b)
    res = runner.invoke(app, ["pull"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["push"])
    assert res.exit_code == 0, res.output

    monkeypatch.setattr(config_module, "CONFIG_PATH", config_a)
    res = runner.invoke(app, ["pull"])
    assert res.exit_code == 0, res.output

    ledger_a, exc = core.load_ledger(["dev-b"])
    assert exc is None
    for name in SHARED_FILES:
        rel = f"{PROJECT_REL}/{name}"
        v0_sha = hashlib.sha256(_v0_bytes(name)).hexdigest()
        assert ledger_a.base("dev-b", "claude", rel) == v0_sha
        assert ledger_a.newest_held("claude", rel) == pullplan.prefix(v0_sha)

    # Phase 2: start the new fleet on `sync_dir` and populate the peer's versions.
    peer_bytes = {
        "peer_ahead.md": b"peer edit built on v0\n",
        "both_changed.md": b"peer edit on both_changed\n",
        "local_newer.md": b"peer edit on local_newer\n",
        "MEMORY.md": _v0_bytes("MEMORY.md") + b"- peer fact from new fleet\n",
    }

    if scenario == "moved-first":
        monkeypatch.setattr(config_module, "CONFIG_PATH", config_b)
        _configure_init_patches(monkeypatch, claude_b)
        init_peer = runner.invoke(
            app,
            ["init", "--new-fleet"],
            input=f"y\n{sync_dir}\nMac B\n{PASSPHRASE}\n{PASSPHRASE}\n",
        )
        assert init_peer.exit_code == 0, init_peer.output
        peer_memory = memory_b
    else:
        claude_c = tmp_path / "machine_c" / ".claude"
        peer_memory = claude_c / PROJECT_REL
        config_c = tmp_path / "config_c.toml"
        monkeypatch.setattr(config_module, "CONFIG_PATH", config_c)
        _configure_init_patches(monkeypatch, claude_c)
        init_peer = runner.invoke(
            app,
            ["init", "--new-fleet"],
            input=f"{sync_dir}\nLinux C\n{PASSPHRASE}\n{PASSPHRASE}\n",
        )
        assert init_peer.exit_code == 0, init_peer.output
        for name in ("untouched.md", "local_edit.md", "peer_deleted.md"):
            _write_at(peer_memory / name, _v0_bytes(name), t0)

    _write_at(peer_memory / "peer_ahead.md", peer_bytes["peer_ahead.md"], t_peer_newer)
    _write_at(peer_memory / "both_changed.md", peer_bytes["both_changed.md"], t_peer_newer)
    _write_at(peer_memory / "local_newer.md", peer_bytes["local_newer.md"], t_peer_older)
    _write_at(peer_memory / "MEMORY.md", peer_bytes["MEMORY.md"], t_peer_newer)

    res = runner.invoke(app, ["push"])
    assert res.exit_code == 0, res.output

    # Delete peer_deleted.md after the first push so the new fleet manifest carries a
    # hashed tombstone whose sha256 equals Mac A's V0 bytes.
    (peer_memory / "peer_deleted.md").unlink()
    res = runner.invoke(app, ["push"])
    assert res.exit_code == 0, res.output

    # Mac A, still pointed at the iCloud folder, makes local edits that were never pushed.
    _write_at(memory_a / "local_edit.md", b"mac a edit on local_edit\n", t_local)
    _write_at(memory_a / "both_changed.md", b"mac a edit on both_changed\n", t_local)
    _write_at(memory_a / "local_newer.md", b"mac a edit on local_newer\n", t_local)
    _write_at(
        memory_a / "MEMORY.md",
        _v0_bytes("MEMORY.md") + b"- mac a fact before move\n",
        t_local,
    )
    _write_at(memory_a / "local_only.md", b"mac a local only file\n", t_local)

    # Phase 3: snapshot Mac A's Claude files and the iCloud store, then join the new fleet.
    claude_a_snapshot = _snapshot_tree(claude_a, ignore_names={".mind-meld-anchor"})
    icloud_snapshot = _snapshot_tree(icloud_dir)
    assert len(claude_a_snapshot) == 8

    monkeypatch.setattr(config_module, "CONFIG_PATH", config_a)
    _configure_init_patches(monkeypatch, claude_a)
    join_res = runner.invoke(
        app,
        ["init"],
        input=f"y\n{sync_dir}\ny\nMac A\n{PASSPHRASE}\n",
    )

    # 1. Join succeeded, orphan prompt fired, config points at the new store with a new id.
    assert join_res.exit_code == 0, join_res.output
    assert "orphaning existing device" in join_res.output
    saved_cfg = tomllib.loads(config_a.read_text())
    assert saved_cfg["storage"]["path"] == str(sync_dir)
    assert saved_cfg["device"]["id"] != "dev-a"

    # 2. Every snapshotted path still exists with expected bytes.
    fast_forward_set = {"peer_ahead.md"} if scenario == "moved-first" else set()
    memory_rel = Path(PROJECT_REL) / "MEMORY.md"
    for rel_path, before_bytes in claude_a_snapshot.items():
        full_path = claude_a / rel_path
        assert full_path.is_file(), f"missing after join: {rel_path}"
        if rel_path == memory_rel:
            before_lines = set(before_bytes.decode().splitlines())
            after_lines = set(full_path.read_text().splitlines())
            assert before_lines <= after_lines
            assert "- peer fact from new fleet" in after_lines
        elif rel_path.name in fast_forward_set:
            assert full_path.read_bytes() == peer_bytes[rel_path.name]
        else:
            assert full_path.read_bytes() == before_bytes, f"unexpected change in {rel_path}"

    # 3. Conflict siblings match the scenario's expected conflict set and peer bytes.
    expected_conflicts = (
        {"both_changed.md", "local_newer.md"}
        if scenario == "moved-first"
        else {"peer_ahead.md", "both_changed.md"}
    )
    all_conflict_files = [p for p in memory_a.iterdir() if ".sync-conflict-" in p.name]
    assert len(all_conflict_files) == len(expected_conflicts)
    for rel_path in claude_a_snapshot:
        stem = rel_path.stem
        suffix = rel_path.suffix
        siblings = sorted(memory_a.glob(f"{stem}.sync-conflict-*{suffix}"))
        if rel_path.name in expected_conflicts:
            assert len(siblings) == 1, f"expected 1 conflict sibling for {rel_path.name}"
            assert siblings[0].read_bytes() == peer_bytes[rel_path.name]
        else:
            assert siblings == [], f"unexpected conflict sibling for {rel_path.name}: {siblings}"

    # 4. No file was deleted.
    assert "deleted" not in join_res.output.lower()
    assert not statedb.deleted_copies_dir().exists()

    # 5. The iCloud-era store is byte-identical before and after the join.
    assert _snapshot_tree(icloud_dir) == icloud_snapshot
