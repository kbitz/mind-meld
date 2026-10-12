"""CP1/2/7-9 and EC4-8. Product execution requires the delivered 71B gate."""

import errno
import json
import os
import shutil
import stat
import subprocess
import tomllib
import urllib.error
from pathlib import Path

import pytest
from typer.testing import CliRunner

import mind_meld
from mind_meld import (
    cli,
    config,
    crypto,
    fsutil,
    host_skill_discovery,
    host_usage,
    identity,
    lockfile,
    sidecar,
    token_usage,
    upgrade,
)

from . import conftest as sim_conftest
from .devices import SimFleet, inventory
from .transport import EPOCH, owned_path, read_tree

REL = "projects/-sim-project/memory/note.md"


def test_one_file_two_devices(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    a.edit("projects/-sim-project/memory/note.md", b"hello from a\n")
    pushed = a.push()
    assert pushed.exit_code == 0, pushed.output
    assert not (b.source_root / "projects/-sim-project/memory/note.md").exists()
    assert sim_fleet.transport.drain().quiescent
    pulled = b.pull()
    assert pulled.exit_code == 0, pulled.output
    assert (
        b.source_root / "projects/-sim-project/memory/note.md"
    ).read_bytes() == b"hello from a\n"
    assert a.replica_root != b.replica_root and a.state_root != b.state_root
    assert a.home != b.home and a.hardware_id != b.hardware_id


def test_pull_requires_transport_delivery_into_its_own_replica(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    for device in (a, b):
        raw = tomllib.loads(device.config_path.read_text())
        assert raw["storage"]["path"] == str(device.replica_root)
    a.edit(REL, b"only via transport")
    assert a.push().exit_code == 0
    assert b.pull().exit_code == 0
    assert not (b.source_root / REL).exists()
    assert sim_fleet.transport.drain().delivered > 0
    assert b.pull().exit_code == 0
    assert (b.source_root / REL).read_bytes() == b"only via transport"


def test_shared_bootstrap_baseline_preserves_outer_crypto(sim_fleet, test_root_salt):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    assert read_tree(a.replica_root) == read_tree(b.replica_root)
    assert (a.replica_root / crypto.CRYPTO_INIT_KEY).read_bytes() == (
        b.replica_root / crypto.CRYPTO_INIT_KEY
    ).read_bytes()
    assert sim_fleet.transport.pending() == ()
    outer_session, outer_cache = crypto._SESSION.copy(), crypto._MASTER_KEY_CACHE.copy()
    assert outer_session["root_salt"] == test_root_salt
    a.edit(REL, b"first real encrypted transfer")
    assert a.push().exit_code == 0
    assert sim_fleet.transport.drain().quiescent
    assert b.pull().exit_code == 0
    assert (b.source_root / REL).read_bytes() == b"first real encrypted transfer"
    assert crypto._SESSION == outer_session and crypto._MASTER_KEY_CACHE == outer_cache


def process_state():
    return (
        dict(os.environ),
        mind_meld.__version__,
        config.CONFIG_PATH,
        config.LOCK_PATH,
        lockfile.LOCK_PATH,
        sidecar.SIDECAR_DIR,
        upgrade.CACHE_DIR,
        upgrade.CACHE_PATH,
        host_usage.CACHE_PATH,
        token_usage.CACHE_PATH,
        crypto._SESSION,
        crypto._SESSION.copy(),
        crypto._MASTER_KEY_CACHE,
        crypto._MASTER_KEY_CACHE.copy(),
        token_usage._WARNED_UNKNOWN_MODELS,
        token_usage._WARNED_UNKNOWN_MODELS.copy(),
        tuple((id(s), s["path"]) for s in config.DEFAULT_SOURCES),
    )


def forbidden_process(*args, **kwargs):
    raise AssertionError(f"simulator launched a host process: {args!r}")


def test_a_b_a_real_commands_and_activation_owners(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    outer = process_state()
    version = mind_meld.__version__
    a.edit(REL, b"a-b-a")
    inactive = inventory(b.root)
    assert a.push().exit_code == 0
    assert inventory(b.root) == inactive
    assert sim_fleet.transport.drain().quiescent
    inactive = inventory(a.root)
    assert b.pull().exit_code == 0
    assert inventory(a.root) == inactive
    assert a.push().exit_code == 0
    for device in (a, b, a):
        with sim_fleet.activate(device):
            assert Path.home() == device.home
            assert config.CONFIG_PATH == device.config_path
            assert upgrade.CACHE_DIR == device.state_root
            assert upgrade.CACHE_PATH == device.state_root / "upgrade-state.json"
            assert token_usage.CACHE_PATH.is_relative_to(device.state_root)
            assert host_usage.CACHE_PATH.is_relative_to(device.state_root)
            mm_events = next(s for s in config.DEFAULT_SOURCES if s["name"] == "mm-events")
            assert config._is_default_mm_events_path(mm_events["path"])
            assert Path(mm_events["path"]).is_relative_to(device.home)
            assert crypto._SESSION["root_salt"] == sim_fleet.crypto_init.root_salt
            assert mind_meld.__version__ == version
            for default in (config.DEFAULT_STORAGE_PATH, cli.DEFAULT_STORAGE_PATH):
                assert Path(default).is_relative_to(device.home)
            assert cli._ICLOUD_DRIVE_ROOT.is_relative_to(device.home)
            assert host_skill_discovery.probe_grok_skill_discovery()["status"] == "binary-absent"
            with pytest.raises(urllib.error.URLError):
                upgrade._fetch_tags()
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(subprocess, "run", forbidden_process)
                assert identity.refresh_identity_cache(force=True) == []
                cli._auto_pin_storage_for_icloud(Path(cli.DEFAULT_STORAGE_PATH))
    assert process_state() == outer
    assert (b.source_root / REL).read_bytes() == b"a-b-a"


def test_failed_real_command_returns_original_result_and_unwinds(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    outer = process_state()
    valid_config = a.config_path.read_bytes()
    a.config_path.write_bytes(b"broken = [")
    result = a.push()
    assert result.exit_code != 0 and result.exception is not None
    assert "config" in result.output.lower()
    assert sim_fleet.latest_result is result
    assert process_state() == outer and SimFleet._active is None
    a.config_path.write_bytes(valid_config)
    assert b.push().exit_code == 0
    assert a.push().exit_code == 0
    assert process_state() == outer


def test_nested_activation_and_exception_leave_entry_state(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    outer = process_state()
    with sim_fleet.activate(a):
        before, active_state = inventory(sim_fleet.tmp_path), process_state()
        with pytest.raises(RuntimeError, match="b.*a.*sequentially"):
            with sim_fleet.activate(b):
                pytest.fail("Overlapping activation entered")
        assert inventory(sim_fleet.tmp_path) == before
        assert process_state() == active_state
    original = RuntimeError("injected activation failure")
    with pytest.raises(RuntimeError) as raised:
        with sim_fleet.activate(a):
            token_usage._WARNED_UNKNOWN_MODELS.add("device-a-only")
            raise original
    assert raised.value is original and process_state() == outer
    for device in (b, a):  # Each activation is a fresh process, including a again.
        with sim_fleet.activate(device):
            assert "device-a-only" not in token_usage._WARNED_UNKNOWN_MODELS
    assert process_state() == outer
    other = SimFleet(sim_fleet.tmp_path / "second", write_guard=sim_fleet.write_guard)
    stranger = other._allocate("x")
    with sim_fleet.activate(a):
        with pytest.raises(RuntimeError, match="x.*a.*sequentially"):
            with other.activate(stranger):
                pytest.fail("Cross-fleet activation entered")
    assert process_state() == outer


def test_broken_config_diag_never_reads_the_real_storage_default(sim_fleet, monkeypatch):
    a = sim_fleet.device("a")
    outer_default = config.DEFAULT_STORAGE_PATH
    opened = []
    real_backend = cli.LocalBackend

    def spy(root, *args, **kwargs):
        opened.append(Path(root))
        return real_backend(root, *args, **kwargs)

    monkeypatch.setattr(cli, "LocalBackend", spy)
    a.config_path.write_bytes(b"broken = [")
    result = a.invoke(["diag", "--json"])
    assert result.exit_code == 0, result.output
    assert opened and all(root.is_relative_to(a.home) for root in opened)
    assert config.DEFAULT_STORAGE_PATH == outer_default == cli.DEFAULT_STORAGE_PATH


def test_tree_isolation_reports_original_result_and_preserves_original_exception(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    outer = process_state()
    leak = sim_fleet.tmp_path / "injected-outer-write"
    with pytest.raises(RuntimeError, match="Isolation failure.*a") as raised:
        with sim_fleet.activate(a):
            result = CliRunner().invoke(cli.app, ["--invalid-sim-option"])
            sim_fleet.latest_result = result
            leak.write_bytes(b"leak")
    assert raised.value.cli_result is result and result.exit_code == 2
    assert result.exception is not None and process_state() == outer
    leak.unlink()
    original = OSError(errno.EIO, "original command failure")
    with pytest.raises(OSError) as raised:
        with sim_fleet.activate(a):
            leak.write_bytes(b"leak")
            raise original
    assert raised.value is original and "Isolation failure" in " ".join(original.__notes__)
    leak.unlink()
    assert b.invoke(["--version"]).exit_code == 0
    assert process_state() == outer
    link = a.home / "agents" / "new-link"
    with pytest.raises(RuntimeError, match="Isolation failure.*new links") as raised:
        with sim_fleet.activate(a):
            link.symlink_to(sim_fleet.tmp_path.parent, target_is_directory=True)
    assert raised.value.cli_result is None  # No command ran in that activation.
    link.unlink()


def test_activation_rejects_existing_descendant_symlink(sim_fleet, tmp_path_factory):
    a = sim_fleet.device("a")
    outside = tmp_path_factory.mktemp("activation-outside")
    agents = a.home / "agents"
    shutil.rmtree(agents)
    agents.symlink_to(outside, target_is_directory=True)
    outer = process_state()
    try:
        with pytest.raises(ValueError, match="symlink"):
            with sim_fleet.activate(a):
                pass
        assert inventory(outside) == {}
        assert SimFleet._active is None and process_state() == outer
    finally:
        agents.unlink()
    with sim_fleet.activate(a):
        pass


def test_inventory_records_special_files_without_reading(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    assert inventory(tmp_path)["pipe"][0] == "special"


def replica_write(fleet, device, data, *, path="record.enc"):
    target = owned_path(device.replica_root, path)
    target.write_bytes(data)
    stamp = fleet.transport.next_mtime()
    os.utime(target, (stamp, stamp))
    return fleet.transport.scan(device)[0]


def delivery(fleet, event, device):
    return next(c for c in fleet.transport.pending() if c.event == event and c.dst == device.name)


@pytest.mark.parametrize("peer_holds", [False, True])
@pytest.mark.parametrize("deleted", [False, True])
def test_saved_restore_revokes_only_the_restored_devices_event_holding(
    sim_fleet, peer_holds, deleted
):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    transport = sim_fleet.transport
    replica_write(sim_fleet, a, b"saved")
    assert transport.drain().quiescent
    c = sim_fleet.clone(b, "c")
    saved = sim_fleet.snapshot(a)
    transport.set_online(a, False)
    if deleted:
        (a.replica_root / "record.enc").unlink()
        event = transport.scan(a)[0]
    else:
        event = replica_write(sim_fleet, a, b"after snapshot")
    stale = delivery(sim_fleet, event, c)
    if peer_holds:
        transport.set_online(a, True)
        transport.deliver(delivery(sim_fleet, event, b))
        transport.set_online(a, False)
    peer = inventory(b.root)
    captured = transport.events[event]
    sim_fleet.restore(a, saved, index_policy="saved")
    transport.set_online(a, True)
    assert inventory(b.root) == peer and transport.events[event] is captured
    assert (a.replica_root / "record.enc").read_bytes() == b"saved"
    assert transport.scan(a) == ()
    with pytest.raises(ValueError, match="Invalid delivery"):
        transport.deliver(stale)  # A saved choice cannot use a revoked holder.
    remaining = [step for step in transport.pending() if step.event == event]
    if peer_holds:
        assert {(step.src, step.dst) for step in remaining} == {("b", "a"), ("b", "c")}
        transport.deliver(delivery(sim_fleet, event, c))
        target = c.replica_root / "record.enc"
        assert not target.exists() if deleted else target.read_bytes() == b"after snapshot"
    else:
        assert remaining == []
        assert transport.drain().quiescent
        assert (c.replica_root / "record.enc").read_bytes() == b"saved"


@pytest.mark.parametrize("policy", ["saved", "current"])
def test_snapshot_restore_policies_keep_peers_and_global_pending(sim_fleet, policy):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    a.edit(REL, b"saved source")
    assert a.push().exit_code == 0
    replica_write(sim_fleet, a, b"saved replica")
    sim_fleet.transport.drain()
    saved = sim_fleet.snapshot(a)
    saved_files = saved.files
    a.edit(REL, b"later source")
    a.edit("new-after-snapshot", b"new source")
    undone = replica_write(sim_fleet, a, b"undo this")
    later = replica_write(sim_fleet, a, b"new replica", path="new-after-snapshot.enc")
    pending, peer = sim_fleet.transport.pending(), inventory(b.root)
    current = sim_fleet.transport.index(a)
    sim_fleet.restore(a, saved, index_policy=policy)
    assert (a.source_root / REL).read_bytes() == b"saved source"
    assert (a.replica_root / "record.enc").read_bytes() == b"saved replica"
    assert not (a.source_root / "new-after-snapshot").exists()
    assert not (a.replica_root / "new-after-snapshot.enc").exists()
    assert saved.files == saved_files and inventory(b.root) == peer
    assert a.hardware_id == saved.hardware_id and a.mm_id == saved.mm_id
    if policy == "saved":
        assert sim_fleet.transport.index(a) == saved.index
        assert sim_fleet.transport.scan(a) == ()
        assert sim_fleet.transport.pending() == ()  # Neither undone event reached a peer.
    else:
        assert sim_fleet.transport.pending() == pending
        assert sim_fleet.transport.index(a) == current
        changed = sim_fleet.transport.scan(a)
        assert {sim_fleet.transport.events[e].path for e in changed} == {
            "record.enc",
            "new-after-snapshot.enc",
        }
        assert any(sim_fleet.transport.events[e].file is None for e in changed)
    assert undone in sim_fleet.transport.events and later in sim_fleet.transport.events


def test_saved_restore_new_write_replays_unique_concurrent_events(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    replica_write(sim_fleet, a, b"base")
    sim_fleet.transport.drain()
    saved = sim_fleet.snapshot(a)
    undone = replica_write(sim_fleet, a, b"undone")
    sim_fleet.transport.deliver(delivery(sim_fleet, undone, b))
    sim_fleet.restore(a, saved, index_policy="saved")
    new = replica_write(sim_fleet, a, b"new after rollback")
    assert new > undone and undone not in sim_fleet.transport.events[new].ancestors
    sim_fleet.transport.deliver(delivery(sim_fleet, new, b))
    assert {f.data for f in read_tree(b.replica_root).values()} >= {
        b"undone",
        b"new after rollback",
    }
    assert sim_fleet.transport.drain().quiescent


def test_clone_copies_identity_and_history_without_authoring(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    a.edit(REL, b"source")
    assert a.push().exit_code == 0
    replica_write(sim_fleet, a, b"old")
    sim_fleet.transport.drain()
    count = len(sim_fleet.transport.events)
    clone = sim_fleet.clone(a, "c")
    assert clone.mm_id == a.mm_id
    assert clone.hardware_id != a.hardware_id and clone.name != a.name
    assert clone.root != a.root and clone.home != a.home
    assert clone.replica_root != a.replica_root and clone.state_root != a.state_root
    assert sim_fleet.transport.index(clone) == sim_fleet.transport.index(a)
    assert sim_fleet.transport.scan(clone) == () and len(sim_fleet.transport.events) == count
    later = replica_write(sim_fleet, a, b"delayed newer")
    step = delivery(sim_fleet, later, clone)
    sim_fleet.transport.delay(step, 7)
    assert (clone.replica_root / "record.enc").read_bytes() == b"old"
    sim_fleet.transport.advance(7)
    sim_fleet.transport.deliver(step)
    assert (clone.replica_root / "record.enc").read_bytes() == b"delayed newer"
    replica_write(sim_fleet, a, b"a diverged")
    replica_write(sim_fleet, clone, b"clone diverged")
    assert sim_fleet.transport.drain().quiescent
    for device in (a, b, clone):
        assert {f.data for f in read_tree(device.replica_root).values()} >= {
            b"a diverged",
            b"clone diverged",
        }
    clone.edit(REL, b"clone source only")
    assert (a.source_root / REL).read_bytes() == b"source"
    original = inventory(a.root)
    for command in (clone.push, clone.pull):
        command()  # Activation raises if a rebased path still reached a's tree.
    assert inventory(a.root) == original


def test_failed_clone_registers_nothing_and_can_retry(sim_fleet):
    a = sim_fleet.device("a")
    valid_config = a.config_path.read_bytes()
    a.config_path.write_bytes(b"broken = [")
    with pytest.raises(tomllib.TOMLDecodeError) as raised:
        sim_fleet.clone(a, "c")
    assert "registered nothing" in " ".join(raised.value.__notes__)
    assert "c" not in sim_fleet.devices and "c" not in sim_fleet.transport.roots
    assert not (sim_fleet.root / "c").exists()
    a.config_path.write_bytes(valid_config)
    clone = sim_fleet.clone(a, "c")
    assert tomllib.loads(clone.config_path.read_text())["storage"]["path"] == str(
        clone.replica_root
    )


def test_restore_and_clone_preserve_directory_modes(sim_fleet):
    a = sim_fleet.device("a")
    private = a.source_root / "private"
    private.mkdir()
    (private / "kept.md").write_bytes(b"kept")
    private.chmod(0o500)
    a.root.chmod(0o750)
    saved = sim_fleet.snapshot(a)
    private.chmod(0o755)
    a.root.chmod(0o755)
    sim_fleet.restore(a, saved, index_policy="saved")
    clone = sim_fleet.clone(a, "c")
    # The backup now holds the read-only directory; cleanup must still succeed.
    sim_fleet.restore(a, saved, index_policy="saved")
    assert not list(sim_fleet.root.glob(".mm-tmp-*"))
    for device in (a, clone):
        assert stat.S_IMODE((device.source_root / "private").stat().st_mode) == 0o500
        assert (device.source_root / "private/kept.md").read_bytes() == b"kept"
        assert stat.S_IMODE(device.root.stat().st_mode) == 0o750


def test_leaked_activation_is_undone_and_cannot_clear_a_later_one(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    outer = process_state()
    leaked = sim_fleet.activate(a)
    leaked.__enter__()
    assert Path.home() == a.home
    assert SimFleet.release_leaked() is a
    assert process_state() == outer and SimFleet._active is None
    with sim_fleet.activate(b):
        leaked.__exit__(None, None, None)  # A late unwind of the leak.
        assert SimFleet._active is b and Path.home() == b.home
    assert process_state() == outer


def test_backup_cleanup_failure_still_commits_saved_index(sim_fleet, monkeypatch):
    a = sim_fleet.device("a")
    replica_write(sim_fleet, a, b"saved")
    saved = sim_fleet.snapshot(a)
    replica_write(sim_fleet, a, b"undo this")
    original_rmtree = shutil.rmtree

    def broken_rmtree(path, *args, **kwargs):
        if Path(path).name == ".mm-tmp-backup-a":
            raise OSError(errno.EIO, "injected backup cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", broken_rmtree)
        with pytest.raises(OSError, match="backup cleanup") as raised:
            sim_fleet.restore(a, saved, index_policy="saved")
    assert "Disk and index are committed" in " ".join(raised.value.__notes__)
    assert sim_fleet.transport.index(a) == saved.index
    assert (a.replica_root / "record.enc").read_bytes() == b"saved"
    assert sim_fleet.transport.scan(a) == ()
    shutil.rmtree(sim_fleet.root / ".mm-tmp-backup-a")
    sim_fleet.restore(a, saved, index_policy="saved")


def test_clone_rebases_only_known_fields_preserves_opaque_disk_and_mtimes(sim_fleet):
    a = sim_fleet.device("a")
    user_text = str(a.root).encode()
    a.edit(REL, user_text, mtime=EPOCH + 101)
    for filename, data in (
        ("state.db", b"database"),
        ("state.db-wal", b"wal"),
        ("state.db-shm", b"shm"),
    ):
        path = a.state_root / filename
        path.write_bytes(data)
        os.utime(path, (EPOCH + 102, EPOCH + 102))
    last_push = a.state_root / "last-push.json"
    last_push.write_text(
        json.dumps(
            {"sources": {"claude": {"base_path": str(a.source_root)}}, "unrelated": str(a.root)}
        )
    )
    replica_write(sim_fleet, a, user_text)
    source_before = read_tree(a.root)
    clone = sim_fleet.clone(a, "c")
    raw = tomllib.loads(clone.config_path.read_text())
    assert raw["storage"]["path"] == str(clone.replica_root)
    assert raw["sync"]["sources"][0]["path"] == str(clone.source_root)
    assert raw["device"]["id"] == a.mm_id
    sidecar_data = json.loads((clone.state_root / "last-push.json").read_text())
    assert sidecar_data["sources"]["claude"]["base_path"] == str(clone.source_root)
    assert sidecar_data["unrelated"] == str(a.root)
    assert (clone.source_root / REL).read_bytes() == user_text
    assert (clone.source_root / REL).stat().st_mtime_ns == (a.source_root / REL).stat().st_mtime_ns
    assert (clone.replica_root / "record.enc").read_bytes() == user_text
    for filename, data in (
        ("state.db", b"database"),
        ("state.db-wal", b"wal"),
        ("state.db-shm", b"shm"),
    ):
        assert (clone.state_root / filename).read_bytes() == data
        assert (clone.state_root / filename).stat().st_mtime_ns == (
            a.state_root / filename
        ).stat().st_mtime_ns
    assert read_tree(a.root) == source_before


@pytest.mark.parametrize("operation", ["snapshot", "restore", "clone"])
def test_lifecycle_requires_idle_even_when_offline(sim_fleet, operation):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    replica_write(sim_fleet, a, b"old")
    sim_fleet.transport.drain()
    saved = sim_fleet.snapshot(b)

    def act():
        if operation == "snapshot":
            return sim_fleet.snapshot(b)
        if operation == "restore":
            return sim_fleet.restore(b, saved, index_policy="saved")
        return sim_fleet.clone(b, "new-clone")

    with sim_fleet.activate(a):
        before = inventory(sim_fleet.tmp_path)
        with pytest.raises(RuntimeError, match="b.*activation.*exit activation"):
            act()
        assert inventory(sim_fleet.tmp_path) == before
    event = replica_write(sim_fleet, a, b"new")
    token = sim_fleet.transport.begin_replace(delivery(sim_fleet, event, b))
    sim_fleet.transport.set_online(b, False)
    before = inventory(sim_fleet.tmp_path)
    with pytest.raises(RuntimeError, match="b.*replacement.*finish_replace"):
        act()
    assert inventory(sim_fleet.tmp_path) == before
    sim_fleet.transport.finish_replace(token)
    act()  # An idle, offline device is permitted.


@pytest.mark.parametrize("relative", ["", "/escape", "../escape", "dir/../../escape"])
def test_source_helpers_reject_escaping_paths_before_mutation(sim_fleet, relative):
    a = sim_fleet.device("a")
    before = inventory(sim_fleet.tmp_path)
    for operation in (lambda: a.edit(relative, b"invalid"), lambda: a.delete(relative)):
        with pytest.raises(ValueError, match="path"):
            operation()
        assert inventory(sim_fleet.tmp_path) == before


def test_lifecycle_rejects_symlinks_and_duplicate_destinations(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    saved = sim_fleet.snapshot(a)
    (a.source_root / "escape").symlink_to(b.source_root, target_is_directory=True)
    (sim_fleet.root / "symlink-clone").symlink_to(b.root, target_is_directory=True)
    before = inventory(sim_fleet.tmp_path)
    for operation in (
        lambda: a.edit("escape/x", b"invalid"),
        lambda: a.delete("escape/x"),
        lambda: sim_fleet.snapshot(a),
        lambda: sim_fleet.restore(a, saved, index_policy="saved"),
        lambda: sim_fleet.clone(a, "fresh"),
        lambda: sim_fleet.clone(b, "a"),
        lambda: sim_fleet.clone(b, "../escape"),
        lambda: sim_fleet.clone(b, "symlink-clone"),
    ):
        with pytest.raises(ValueError):
            operation()
        assert inventory(sim_fleet.tmp_path) == before


@pytest.mark.parametrize("failure", ["write", "rename"])
def test_copy_failure_preserves_disk_index_and_original_error(sim_fleet, monkeypatch, failure):
    a = sim_fleet.device("a")
    a.edit(REL, b"saved")
    saved = sim_fleet.snapshot(a)
    a.edit(REL, b"keep until successful restore")
    before, index = inventory(sim_fleet.tmp_path), sim_fleet.transport.index(a)
    original = OSError(errno.EIO, "injected copy failure")
    original_write, original_replace = Path.write_bytes, os.replace

    def broken_write(path, data):
        if ".mm-tmp-copy-a" in path.parts:
            raise original
        return original_write(path, data)

    def broken_replace(src, dst):
        if Path(src).name == ".mm-tmp-copy-a":
            raise original
        return original_replace(src, dst)

    with monkeypatch.context() as patch:
        if failure == "write":
            patch.setattr(Path, "write_bytes", broken_write)
        else:
            patch.setattr(os, "replace", broken_replace)
        with pytest.raises(OSError) as raised:
            sim_fleet.restore(a, saved, index_policy="saved")
    assert raised.value is original and "Copying device a" in " ".join(original.__notes__)
    assert inventory(sim_fleet.tmp_path) == before and sim_fleet.transport.index(a) == index
    sim_fleet.restore(a, saved, index_policy="saved")
    assert (a.source_root / REL).read_bytes() == b"saved"


def test_failure_report_boundary_has_trace_and_real_result_only_for_fleet(
    sim_fleet, tmp_path, capsys
):
    a = sim_fleet.device("a")
    a.edit(REL, b"report evidence")
    assert a.push().exit_code == 0
    sim_fleet.transport.set_online("b", False)
    # Nested pytest exercises the reporting boundary, with this already-
    # guarded real fleet/result. It does not bootstrap another fleet or call
    # the product. The production hook is used, not copied or unit-asserted.
    case = tmp_path / "test_report_boundary.py"
    case.write_text(
        "def test_fleet(sim_fleet):\n    assert False, 'intentional fleet failure'\n\n"
        "def test_unrelated():\n    assert False, 'intentional unrelated failure'\n"
    )
    settings = tmp_path / "pytest.ini"
    settings.write_text("[pytest]\n")
    fleet = sim_fleet

    class ReportPlugin:
        @pytest.fixture
        def sim_fleet(self):
            return fleet

        pytest_runtest_makereport = staticmethod(sim_conftest.pytest_runtest_makereport)

    code = pytest.main(
        ["-c", str(settings), "--confcutdir", str(tmp_path), "-q", str(case)],
        plugins=[ReportPlugin()],
    )
    output = capsys.readouterr().out
    assert code == pytest.ExitCode.TESTS_FAILED
    assert output.count("sim_fleet reproduction") == 1
    assert "seed=0" in output and "choices/trace=" in output and "offline" in output
    assert "latest real CLI Result" in output and "exit=0" in output and "stdout=" in output
    assert "intentional unrelated failure" in output


def test_missing_guard_lookup_fails_before_product_execution(tmp_path, monkeypatch, request):
    calls = []
    monkeypatch.setattr(sim_conftest, "_lookup_write_guard", lambda request: None)
    monkeypatch.setattr(SimFleet, "_bootstrap_legacy", lambda self: calls.append("bootstrap"))
    monkeypatch.setattr(CliRunner, "invoke", lambda *a, **kw: calls.append("cli"))
    before = inventory(tmp_path)
    with pytest.raises(pytest.fail.Exception, match="Track 71B.*guard.*Fix"):
        request.getfixturevalue("sim_write_guard")
    with pytest.raises(RuntimeError, match="Track 71B"):
        SimFleet(tmp_path).bootstrap()
    assert calls == [] and inventory(tmp_path) == before


@pytest.mark.parametrize("unbind", ["delete", "none"])
def test_real_lookup_rejects_unbound_guard(monkeypatch, request, unbind):
    if unbind == "delete":
        monkeypatch.delattr(fsutil, "refuse_real_state_write_under_pytest")
    else:
        monkeypatch.setattr(fsutil, "refuse_real_state_write_under_pytest", None)
    assert sim_conftest._lookup_write_guard(request) is None


def test_bound_guard_requires_a_running_pytest_test(monkeypatch, request):
    verify = sim_conftest._lookup_write_guard(request)
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    with pytest.raises(RuntimeError, match="PYTEST_CURRENT_TEST"):
        verify()


def test_source_mtime_defaults_are_fleet_wide_and_not_delivery_time(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    a.edit(REL, b"a")
    first = (a.source_root / REL).stat().st_mtime
    sim_fleet.transport.advance(100_000)
    b.edit(REL, b"b")
    second = (b.source_root / REL).stat().st_mtime
    a.edit(REL, b"explicit", mtime=EPOCH + 200)
    assert second == first + 1
    assert (a.source_root / REL).stat().st_mtime == EPOCH + 200
    assert sim_fleet.product_time < EPOCH + 100_000
