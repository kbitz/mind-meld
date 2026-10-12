"""CP3-6 / EC1-3: opaque bytes and explicit schedules, no product calls."""

import errno
import os
import random
from dataclasses import replace
from pathlib import Path

import pytest

from .transport import EPOCH, Choice, FakeTransport, owned_path, read_tree


@pytest.fixture
def transport(tmp_path):
    transport = FakeTransport(tmp_path, seed=17)
    for name in ("a", "b", "c"):
        transport.add(name, tmp_path / name)
    return transport


def write(transport, name, data, *, path="note.md", mtime=None):
    target = owned_path(transport.roots[name], path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    stamp = transport.next_mtime() if mtime is None else mtime
    os.utime(target, (stamp, stamp))
    return transport.scan(name)[0]


def choice(transport, event, dst):
    return next(c for c in transport.pending() if c.event == event and c.dst == dst)


def contents(transport, name):
    return {path: file.data for path, file in read_tree(transport.roots[name]).items()}


@pytest.mark.parametrize("data", [b"", b"\x00\xff\x01whole\nfile"])
def test_capture_is_immutable_and_files_are_whole(transport, data):
    event = write(transport, "a", data)
    captured = choice(transport, event, "b")
    (transport.roots["a"] / "note.md").write_bytes(b"later unscheduled bytes")
    transport.deliver(captured)
    assert contents(transport, "b") == {"note.md": data}
    assert transport.events[event].file.data == data


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 0, 1), (1, 0, 2)])
def test_stale_duplicate_edit_and_delete_never_resurrect(transport, order):
    first = write(transport, "a", b"first")
    second = write(transport, "a", b"second")
    (transport.roots["a"] / "note.md").unlink()
    deleted = transport.scan("a")[0]
    scheduled = [choice(transport, e, "b") for e in (first, second, deleted)]
    for i in order:
        transport.deliver(scheduled[i])
        transport.deliver(scheduled[i])
    assert contents(transport, "b") == {}
    assert not list(transport.roots["b"].glob("*.sync-conflict-*"))
    assert transport.scan("b") == ()
    assert transport.events[deleted].file is None


def test_stale_after_overwrite_never_regresses(transport):
    first = write(transport, "a", b"first")
    second = write(transport, "a", b"second")
    steps = choice(transport, second, "b"), choice(transport, first, "b")
    for step in steps + steps:
        transport.deliver(step)
    assert contents(transport, "b") == {"note.md": b"second"}
    assert transport.scan("b") == ()


@pytest.mark.parametrize("edited", [False, True])
def test_known_conflict_copy_keeps_its_history_after_later_concurrency(transport, edited):
    write(transport, "a", b"alpha", mtime=EPOCH + 10)
    bravo = write(transport, "b", b"bravo", mtime=EPOCH + 20)
    charlie = write(transport, "c", b"charlie", mtime=EPOCH + 30)
    transport.deliver(choice(transport, bravo, "a"))
    (copy,) = transport.roots["a"].glob("*.sync-conflict-*-a.md")
    assert copy.read_bytes() == b"alpha"
    if edited:
        copy.write_bytes(b"edited copy")
    else:
        copy.unlink()
    transport.scan("a")
    transport.deliver(choice(transport, charlie, "a"))  # Previously raised or resurrected.
    assert transport.drain().quiescent
    for name in ("a", "b", "c"):
        tree = contents(transport, name)
        assert tree["note.md"] == b"charlie"
        assert tree.get(copy.name) == (b"edited copy" if edited else None)
        assert b"alpha" not in tree.values() and b"bravo" in tree.values()
        assert transport.scan(name) == ()


def test_unindexed_file_at_conflict_name_meets_the_copy(transport):
    alpha = write(transport, "a", b"alpha", mtime=EPOCH + 10)
    bravo = write(transport, "b", b"bravo", mtime=EPOCH + 20)
    before = transport.index("a")
    transport.deliver(choice(transport, bravo, "a"))
    (copy,) = transport.roots["a"].glob("*.sync-conflict-*-a.md")
    copy.write_bytes(b"edited copy")
    transport.restore_index("a", before)  # Index behind disk, as after a current restore.
    rescanned = next(e for e in transport.scan("a") if transport.events[e].path == copy.name)
    transport.deliver(choice(transport, rescanned, "b"))
    transport.deliver(choice(transport, alpha, "b"))  # Previously raised "occupied".
    assert transport.drain().quiescent
    for name in ("a", "b", "c"):
        tree = contents(transport, name)
        assert tree["note.md"] == b"bravo" and tree[copy.name] == b"edited copy"
        assert b"alpha" in tree.values()
        assert transport.scan(name) == ()


@pytest.mark.parametrize("deleted", [False, True])
def test_online_holder_relays_with_origin_offline(transport, deleted):
    transport.set_online("c", False)
    event = write(transport, "a", b"relay")
    transport.deliver(choice(transport, event, "b"))
    if deleted:
        (transport.roots["a"] / "note.md").unlink()
        event = transport.scan("a")[0]
        transport.deliver(choice(transport, event, "b"))
    count = len(transport.events)
    transport.set_online("a", False)
    transport.set_online("c", True)
    relayed = choice(transport, event, "c")
    assert relayed.src == "b" and transport.events[event].author == "a"
    transport.deliver(relayed)
    assert contents(transport, "c") == ({} if deleted else {"note.md": b"relay"})
    assert len(transport.events) == count


@pytest.mark.parametrize("same", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_concurrent_bytes_and_conflicts_propagate(transport, same, reverse):
    a = write(transport, "a", b"alpha", mtime=EPOCH + 10)
    b = write(transport, "b", b"alpha" if same else b"bravo", mtime=EPOCH + 20)
    steps = [choice(transport, a, "c"), choice(transport, b, "c")]
    for step in reversed(steps) if reverse else steps:
        transport.deliver(step)
    result = transport.drain()
    assert result.quiescent
    expected = {"note.md": b"alpha" if same else b"bravo"}
    if not same:
        # Fixed event ID 1 gives a deterministic timestamp; do not ask the
        # fake's winner/naming function to compute the expected oracle.
        expected["note.sync-conflict-20261011-120001-a.md"] = b"alpha"
    assert (
        contents(transport, "a") == contents(transport, "b") == contents(transport, "c") == expected
    )
    assert transport.scan("c") == ()


@pytest.mark.parametrize("edit_wins", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_edit_delete_concurrency_preserves_edited_bytes(transport, edit_wins, reverse):
    write(transport, "a", b"base")
    assert transport.drain().quiescent
    edit = write(transport, "a", b"edited", mtime=EPOCH + (100 if edit_wins else 0))
    (transport.roots["b"] / "note.md").unlink()
    deleted = transport.scan("b")[0]
    steps = [choice(transport, edit, "c"), choice(transport, deleted, "c")]
    for step in reversed(steps) if reverse else steps:
        transport.deliver(step)
    assert transport.drain().quiescent
    for name in ("a", "b", "c"):
        tree = contents(transport, name)
        assert b"edited" in tree.values()
        assert ("note.md" in tree) is edit_wins
        assert len(tree) == 1


def test_same_mtime_tie_uses_identity_not_payload(transport):
    write(transport, "a", b"z", mtime=EPOCH)
    write(transport, "b", b"a", mtime=EPOCH)
    assert transport.drain().quiescent
    assert contents(transport, "c")["note.md"] == b"z"


def replacement(transport):
    write(transport, "a", b"old")
    transport.drain()
    event = write(transport, "a", b"\x00complete-new")
    return choice(transport, event, "b")


def test_replace_window_busy_drain_scan_and_offline_finish(transport):
    incoming = replacement(transport)
    target = transport.roots["b"] / "note.md"
    assert target.read_bytes() == b"old"
    token = transport.begin_replace(incoming)
    with pytest.raises(FileNotFoundError):
        target.read_bytes()
    assert transport.scan("b") == ()
    newer = write(transport, "a", b"newer")
    competing = choice(transport, newer, "b")
    before = contents(transport, "b")
    for operation in (transport.deliver, transport.begin_replace):
        with pytest.raises(RuntimeError, match="replacement-in-progress"):
            operation(competing)
    assert contents(transport, "b") == before
    result = transport.drain()
    assert not result.quiescent
    assert all("replacement-in-progress" in c.reasons for c in result.remaining)
    assert transport.unfinished("b") == (token,)
    transport.set_online("b", False)
    transport.finish_replace(token)
    assert target.read_bytes() == b"\x00complete-new"
    with pytest.raises(ValueError, match="finished replacement"):
        transport.finish_replace(token)
    transport.set_online("b", True)
    assert transport.drain().quiescent
    assert target.read_bytes() == b"newer"


def test_losing_concurrent_event_cannot_open_a_rename_gap(transport):
    write(transport, "a", b"base")
    assert transport.drain().quiescent
    loser = write(transport, "a", b"older concurrent", mtime=EPOCH + 50)
    write(transport, "b", b"newer local", mtime=EPOCH + 60)
    target = transport.roots["b"] / "note.md"
    with pytest.raises(ValueError, match="would not win"):
        transport.begin_replace(choice(transport, loser, "b"))
    assert target.read_bytes() == b"newer local" and transport.unfinished("b") == ()
    transport.deliver(choice(transport, loser, "b"))
    assert set(contents(transport, "b").values()) == {b"newer local", b"older concurrent"}


@pytest.mark.parametrize("local_wins", [False, True])
def test_local_edit_during_replace_is_preserved(transport, local_wins):
    incoming = replacement(transport)
    token = transport.begin_replace(incoming)
    target = transport.roots["b"] / "note.md"
    target.write_bytes(b"local during gap")
    stamp = EPOCH + (100 if local_wins else 0)
    os.utime(target, (stamp, stamp))
    transport.finish_replace(token)
    tree = contents(transport, "b")
    assert set(tree.values()) == {b"local during gap", b"\x00complete-new"}
    assert target.read_bytes() == (b"local during gap" if local_wins else b"\x00complete-new")


def test_read_error_is_not_an_authored_deletion(transport, monkeypatch):
    target = transport.roots["a"] / "note.md"
    target.write_bytes(b"new")
    original = Path.read_bytes

    def broken(path):
        if path == target:
            raise OSError(errno.EIO, "injected read")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", broken)
        with pytest.raises(OSError, match="injected read") as error:
            transport.scan("a")
    assert "a/note.md" in " ".join(error.value.__notes__)
    assert not transport.events and not transport.pending()
    assert target.read_bytes() == b"new"


@pytest.mark.parametrize("paused", [False, True])
def test_publication_failure_retains_pending_and_token_for_retry(transport, monkeypatch, paused):
    incoming = replacement(transport)
    token = transport.begin_replace(incoming) if paused else None
    target = transport.roots["b"] / "note.md"
    original = os.replace

    def broken(src, dst):
        if dst == target:
            raise OSError(errno.EIO, "injected rename")
        return original(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", broken)
        with pytest.raises(OSError, match="injected rename") as error:
            transport.finish_replace(token) if paused else transport.deliver(incoming)
    assert "b/note.md" in " ".join(error.value.__notes__)
    assert any(c.event == incoming.event and c.dst == "b" for c in transport.pending())
    assert not list(target.parent.glob(".mm-tmp-*"))
    if paused:
        assert transport.unfinished("b") == (token,) and not target.exists()
        transport.finish_replace(token)
    else:
        assert target.read_bytes() == b"old"
        transport.deliver(incoming)
    assert target.read_bytes() == b"\x00complete-new"


@pytest.mark.parametrize("via_drain", [False, True])
def test_conflict_copy_respects_an_open_replacement(transport, via_drain):
    alpha = write(transport, "a", b"alpha", mtime=EPOCH + 10)
    bravo = write(transport, "c", b"bravo", mtime=EPOCH + 20)
    path = "note.sync-conflict-20261011-120001-a.md"
    write(transport, "b", b"old copy", path=path, mtime=EPOCH + 30)
    incoming = write(transport, "c", b"new copy", path=path, mtime=EPOCH + 40)
    token = transport.begin_replace(choice(transport, incoming, "b"))
    target = transport.roots["b"] / path
    transport.deliver(choice(transport, alpha, "b"))
    if not via_drain:
        with pytest.raises(RuntimeError, match="replacement"):
            transport.deliver(choice(transport, bravo, "b"))
    blocked = transport.drain()
    assert not target.exists()
    assert transport.unfinished("b") == (token,)
    assert any(
        c.event == bravo and c.dst == "b" and "replacement-in-progress" in c.reasons
        for c in blocked.remaining
    )
    transport.finish_replace(token)
    assert transport.drain().quiescent
    assert (transport.roots["b"] / "note.md").read_bytes() == b"bravo"
    assert target.read_bytes() == b"new copy"
    assert {b"alpha", b"old copy", b"new copy"} <= set(contents(transport, "b").values())


def test_virtual_delay_offline_and_bounded_drain(transport):
    assert transport.drain().quiescent
    event = write(transport, "a", b"delayed")
    transport.delay(choice(transport, event, "b"), 5)
    transport.set_online("c", False)
    assert {reason for c in transport.drain().remaining for reason in c.reasons} == {
        "delayed",
        "offline",
    }
    transport.advance(5)
    assert contents(transport, "b") == {}  # Advancing time never delivers.
    limited = transport.drain(max_steps=0)
    assert limited.delivered == 0
    assert {c.dst: c.reasons for c in limited.remaining} == {
        "b": ("step-limit",),
        "c": ("offline",),
    }
    assert transport.drain(max_steps=1).delivered == 1
    transport.set_online("c", True)
    assert transport.drain().quiescent
    assert contents(transport, "c") == {"note.md": b"delayed"}


def test_seed_scheduler_is_local_and_replayable(tmp_path):
    global_state = random.getstate()
    histories = []
    for run in ("first", "second"):
        root = tmp_path / run
        root.mkdir()
        t = FakeTransport(root, seed=44)
        for name in ("a", "b", "c"):
            t.add(name, root / name)
        write(t, "a", b"x", path="x", mtime=EPOCH)
        write(t, "b", b"y", path="y", mtime=EPOCH)
        while step := t.choose():
            t.deliver(step)
        assert t.drain().quiescent
        histories.append(list(t.trace))
    assert histories[0] == histories[1]
    assert random.getstate() == global_state


@pytest.mark.parametrize("delta", [-1, float("inf"), float("nan"), True, "one"])
def test_bad_delta_refused_without_clock_mutation(transport, delta):
    with pytest.raises(ValueError, match="delta"):
        transport.advance(delta)
    assert transport.now == 0


def test_bad_choices_and_tokens_are_rejected(transport):
    event = write(transport, "a", b"x")
    valid = choice(transport, event, "b")
    before = contents(transport, "b")
    for invalid in (
        replace(valid, event=999),
        replace(valid, src="b"),
        replace(valid, path="../x"),
        Choice(event, "a", "b", "wrong", 0),
    ):
        with pytest.raises(ValueError):
            transport.deliver(invalid)
    with pytest.raises(ValueError):
        transport.finish_replace(None)
    with pytest.raises(ValueError):
        transport.drain(max_steps=-1)
    assert contents(transport, "b") == before


@pytest.mark.parametrize(
    "relative", ["", "/outside", "../outside", "dir/../outside", "./x", "dir//x"]
)
def test_bad_paths_are_refused_before_mutation(transport, relative):
    before = read_tree(transport.root)
    with pytest.raises(ValueError, match="relative path"):
        owned_path(transport.roots["a"], relative)
    assert read_tree(transport.root) == before


def test_symlink_and_duplicate_overlapping_roots_refused(transport):
    before = read_tree(transport.root)
    for name, root in (
        ("a", transport.root / "unused"),
        ("d", transport.roots["a"]),
        ("d", transport.roots["a"] / "nested"),
        ("d", transport.root.parent / "escape"),
    ):
        with pytest.raises(ValueError):
            transport.add(name, root)
    assert read_tree(transport.root) == before
    (transport.roots["a"] / "link").symlink_to(transport.roots["b"], target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        owned_path(transport.roots["a"], "link/escape")
    with pytest.raises(ValueError, match="symlink"):
        transport.scan("a")
    assert contents(transport, "b") == {}


def test_exact_ignore_set_keeps_ordinary_tmp_and_conflict_payloads(transport):
    root = transport.roots["a"]
    for name in (".DS_Store", ".stignore", ".mm-tmp-x", ".syncthing.x.tmp", "~syncthing~x.tmp"):
        (root / name).write_bytes(b"ignored")
    (root / ".stfolder").mkdir()
    (root / ".stfolder" / "marker").write_bytes(b"ignored")
    (root / "ordinary.tmp").write_bytes(b"kept")
    (root / "ordinary.sync-conflict-name.md").write_bytes(b"also kept")
    assert len(transport.scan("a")) == 2
    assert transport.drain().quiescent
    assert contents(transport, "b") == {
        "ordinary.tmp": b"kept",
        "ordinary.sync-conflict-name.md": b"also kept",
    }


def test_saved_index_rollback_keeps_unique_events_without_false_ancestry(transport):
    write(transport, "a", b"base")
    transport.drain()
    saved = transport.index("a")
    undone = write(transport, "a", b"undone")
    transport.deliver(choice(transport, undone, "c"))
    original = dict(saved.disk)["note.md"]
    target = transport.roots["a"] / "note.md"
    target.write_bytes(original.data)
    os.utime(target, ns=(original.mtime_ns, original.mtime_ns))
    transport.restore_index("a", saved)
    new = write(transport, "a", b"after restore")
    assert new != undone and undone not in transport.events[new].ancestors
    queued = choice(transport, undone, "b")
    assert queued.src == "c"
    transport.deliver(queued)
    transport.deliver(choice(transport, new, "b"))
    assert set(contents(transport, "b").values()) == {b"undone", b"after restore"}
