"""Deterministic witnesses, separate from Hypothesis search."""

from contextlib import contextmanager

import pytest

from .invariants import InvariantError, settle
from .model.state import FileState
from .store_fleet import CONTENTS


class ExpectedDesignGap(AssertionError):
    """Only a checked invariant at the designated scenario boundary qualifies."""


@contextmanager
def expected_gap(invariant):
    try:
        yield
    except InvariantError as error:
        assert error.invariant == invariant, str(error)
        raise ExpectedDesignGap(str(error)) from error


def test_v2_smoke(store_fleet):
    a, b = store_fleet.devices["a"], store_fleet.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    store_fleet.action("a", "push")
    store_fleet.drain()
    assert store_fleet.action("b", "pull").status == "applied"
    settle(store_fleet, min_participants=2)
    assert b.source_root.joinpath("notes.txt").read_bytes() == CONTENTS[1]
    assert FileState(b.state_root).read().ledger


@pytest.mark.xfail(strict=True, raises=ExpectedDesignGap, reason="non-utf8-union: §4 loses bytes")
@pytest.mark.parametrize("path", ["MEMORY.md", "events.jsonl"])
def test_gap_non_utf8_union(store_fleet, path):
    for name, data in (("a", CONTENTS[7]), ("b", CONTENTS[1])):
        store_fleet.devices[name].edit(path, data)
        store_fleet.action(name, "push")
    store_fleet.drain()
    with expected_gap(2):
        settle(store_fleet, min_participants=2)


@pytest.mark.parametrize("path", ["notes.txt", "MEMORY.md", "events.jsonl"])
def test_concurrent_resolvers_and_same_mtime(store_fleet, path):
    import hashlib

    for name, data in [("a", CONTENTS[1]), ("b", CONTENTS[2])]:
        store_fleet.devices[name].edit(path, data)
        store_fleet.action(name, "push")
    store_fleet.drain()
    settle(store_fleet, min_participants=2)
    expected = (
        max(CONTENTS[1:3], key=lambda d: hashlib.sha256(d).hexdigest())
        if path == "notes.txt"
        else CONTENTS[3]
    )
    assert (store_fleet.devices["a"].source_root / path).read_bytes() == expected


def test_edit_delete_then_redelete(store_fleet):
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    settle(f)
    a.delete("notes.txt")
    b.edit("notes.txt", CONTENTS[2])
    for name in ("a", "b"):
        f.action(name, "push")
    f.drain()
    settle(f, min_participants=2)
    assert (a.source_root / "notes.txt").read_bytes() == CONTENTS[2]
    a.delete("notes.txt")
    settle(f, min_participants=2)
    assert not (b.source_root / "notes.txt").exists()
    assert FileState(b.state_root).read().displaced


def publish_gap(f):
    from mind_meld.store import layout

    a = f.devices["a"]
    for data in CONTENTS[1:3]:
        a.edit("notes.txt", data)
        f.action("a", "push")
    for choice in f.transport.pending():
        parsed = layout.parse_record_key(choice.path)
        if choice.dst == "b" and parsed and parsed.kind == "commit" and parsed.seq == 2:
            f.transport.deliver(choice)
    return f.action("b", "pull")


def test_gap_and_snapshot_remedy(store_fleet):
    f = store_fleet
    result = publish_gap(f)
    assert (result.status, result.gap_seq) == ("pending", 1)
    f.action("a", "publish_snapshot")
    # Deliver the snapshot while the missing commit remains in flight.
    for choice in f.transport.pending():
        if choice.dst == "b" and "/snap/" in choice.path and not choice.reasons:
            f.transport.deliver(choice)
    result = f.action("b", "pull")
    assert "snapshot-fallback" in result.events
    settle(f, min_participants=2)


@pytest.mark.parametrize("seconds,warning", [(86399, False), (86400, False), (86401, True)])
def test_gap_warning_exact_boundary(store_fleet, seconds, warning):
    f = store_fleet
    publish_gap(f)
    f.advance(seconds)
    assert bool(f.action("b", "pull").warnings) is warning


@pytest.mark.parametrize("seconds,refresh", [(3599, False), (3600, True), (3601, True)])
def test_presence_exact_boundary(store_fleet, seconds, refresh):
    from mind_meld.store import layout

    f = store_fleet
    d = f.devices["a"]
    target = d.replica_root / layout.record_key(d.mm_id, "presence", 0)
    before = target.read_bytes()
    f.advance(seconds)
    f.action("a", "push")
    assert (target.read_bytes() != before) is refresh
    settle(f, min_participants=2)


@pytest.mark.parametrize("days,seconds,removed", [(14, -1, False), (14, 0, True), (14, 1, True)])
def test_compaction_exact_boundary(store_fleet, days, seconds, removed):
    from mind_meld.store import layout

    f = store_fleet
    for data in CONTENTS[1:3]:
        f.devices["a"].edit("notes.txt", data)
        f.action("a", "publish_snapshot")
    f.advance(days * 86400 + seconds)
    f.action("a", "compact")
    keys = [
        layout.parse_record_key(p.relative_to(f.devices["a"].replica_root).as_posix())
        for p in f.devices["a"].replica_root.rglob("*.enc")
    ]
    assert (not any(n and n.kind == "commit" and n.seq == 1 for n in keys)) is removed
    assert (
        len(
            {
                n.seq
                for n in keys
                if n and n.kind == "snapshot" and n.device_id == f.devices["a"].mm_id
            }
        )
        == 2
    )
    f.drain()
    result = f.action("b", "pull")
    if removed:
        assert "snapshot-fallback" in result.events
    settle(f, min_participants=2)


@pytest.mark.parametrize(
    "seconds,expired", [(30 * 86400 - 1, False), (30 * 86400, True), (30 * 86400 + 1, True)]
)
def test_displaced_expiry_exact_boundary(store_fleet, seconds, expired):
    f = store_fleet
    f.devices["a"].edit("notes.txt", CONTENTS[1])
    settle(f)
    f.devices["a"].delete("notes.txt")
    settle(f)
    f.advance(seconds)
    f.action("b", "compact")
    entries = FileState(f.devices["b"].state_root).read().displaced
    assert bool(entries[0].get("expired")) is expired
    settle(f, min_participants=2)


def test_partial_multipart_and_enoent_replace(store_fleet, monkeypatch):
    from mind_meld.store import layout

    f = store_fleet
    for path in ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl"):
        f.devices["a"].edit(path, CONTENTS[3])
    f.action("a", "push")
    choices = [c for c in f.transport.pending() if c.dst == "b" and "/log/" in c.path]
    assert len(choices) >= 2
    f.transport.deliver(choices[0])
    assert f.action("b", "pull").status == "pending"
    # Mutable presence can enter a genuine replacement window.
    f.advance(3600)
    f.action("a", "push")
    presence = next(
        c
        for c in f.transport.pending()
        if c.dst == "b" and c.path.endswith("presence.enc") and c.src == "a"
    )
    token = f.transport.begin_replace(presence)
    assert not (f.devices["b"].replica_root / presence.path).exists()
    assert f.action("b", "pull").status == "pending"
    f.transport.finish_replace(token)
    # Force the listing/read race on an immutable part, at the actual read site.
    import traceback
    from pathlib import Path

    original = Path.read_bytes
    raced = []

    def race(path):
        if path == f.devices["b"].replica_root / choices[0].path and any(
            frame.name == "load" for frame in traceback.extract_stack()
        ):
            raced.append(path)
            raise FileNotFoundError(path)
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", race)
        assert f.action("b", "pull").status == "pending"
    assert raced
    assert layout.parse_record_key(choices[1].path).nparts >= 2
    settle(f, min_participants=2)


def test_regression_unpublished_live_concurrent_with_delete(store_fleet):
    f = store_fleet
    f.devices["a"].edit("events.jsonl", CONTENTS[3])
    settle(f)
    f.devices["a"].edit("events.jsonl", CONTENTS[2])
    f.devices["b"].delete("events.jsonl")
    f.action("b", "push")
    f.drain()
    f.action("a", "pull")
    settle(f, min_participants=2)
    assert (f.devices["b"].source_root / "events.jsonl").read_bytes() == CONTENTS[2]


def test_pull_presence_receipt_updates_without_hourly_tick(store_fleet):
    from mind_meld.store import format as codec

    from .invariants import context_for

    f = store_fleet
    f.devices["a"].edit("notes.txt", CONTENTS[1])
    result = f.action("a", "push")
    f.drain()
    f.action("b", "pull")
    ctx = context_for(f, f.devices["b"])
    payload = ctx.load(ctx.device_id, "presence", 0)
    presence = codec.decode_presence(payload, expected=ctx.context(ctx.device_id, "presence", 0))
    assert presence["applied"][f.devices["a"].mm_id] == result.seq
    settle(f, min_participants=2)


def test_presence_only_advertisement_is_pending(store_fleet):
    f = store_fleet
    f.devices["a"].edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    choice = next(
        c
        for c in f.transport.pending()
        if c.dst == "b" and c.path.endswith("presence.enc") and c.src == "a"
    )
    f.transport.deliver(choice)
    result = f.action("b", "pull")
    assert (result.status, result.peer, result.gap_seq) == ("pending", f.devices["a"].mm_id, 1)
    settle(f, min_participants=2)


def join_extra(f, name="c"):
    device = f.add_device(name)
    f.drain()
    assert f.action(name, "join").status in ("applied", "refused")
    f.drain()
    return device


def test_join_waits_and_bootstrap_tombstone_beats_unledgered_file(store_fleet):
    f = store_fleet
    c = f.add_device("c")
    c.edit("notes.txt", CONTENTS[2])
    assert f.action("c", "join").status == "waiting"
    f.devices["a"].edit("notes.txt", CONTENTS[1])
    settle(f)
    f.devices["a"].delete("notes.txt")
    settle(f)
    f.advance(1)
    f.action("a", "publish_snapshot")
    f.drain()
    assert f.action("c", "join").status == "applied"
    assert "bootstrap-inversion" in f.coverage
    assert not (c.source_root / "notes.txt").exists()
    assert FileState(c.state_root).read().displaced
    settle(f, min_participants=2)


def test_clone_refuses_then_rejoins_with_new_identity(store_fleet):
    f = store_fleet
    f.devices["a"].edit("notes.txt", CONTENTS[1])
    settle(f)
    c = f.clone(f.devices["a"], "c")
    assert f.action("c", "push").status == "refused"
    assert f.action("c", "pull").status == "refused"
    original_id = c.mm_id
    f.rejoin(c)
    assert c.mm_id != original_id
    settle(f, min_participants=2)
    assert (c.source_root / "notes.txt").read_bytes() == CONTENTS[1]


@pytest.mark.parametrize("policy", ["saved", "current"])
def test_restore_rolls_back_persisted_state_and_last_local_copy(store_fleet, policy):
    f = store_fleet
    a = f.devices["a"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    assert FileState(a.state_root).read().own_log
    f.restore(a, backup, index_policy=policy)
    assert not FileState(a.state_root).read().own_log
    assert f.oracle.excluded
    f.drain()
    settle(f, min_participants=2)


@pytest.mark.parametrize(
    "seconds,eligible", [(30 * 86400 - 1, False), (30 * 86400, True), (30 * 86400 + 1, True)]
)
def test_retire_age_and_snapshot_coverage(store_fleet, seconds, eligible):
    f = store_fleet
    b = f.devices["b"]
    b.edit("notes.txt", CONTENTS[1])
    settle(f)
    join_extra(f)
    settle(f)
    f.advance(seconds)
    for name in ("a", "c"):
        f.devices[name].edit("other.txt", CONTENTS[2])
        f.action(name, "publish_snapshot")
    f.drain()
    result = f.action("a", "retire", target=b.mm_id)
    assert (result.status == "retired") is eligible
    if eligible:
        f.drain()
        assert f.action("b", "push").reason == "missing-own-directory"
        f.rejoin(b)
    settle(f, min_participants=2)


@pytest.mark.parametrize("operation", ["push", "publish_snapshot"])
@pytest.mark.parametrize("crash_state", [False, True])
def test_complete_publication_crash_recovery(store_fleet, operation, crash_state):
    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    # A large bound writes all parts and crashes before state in the other variant.
    result = f.action(
        "a", operation, crash_parts=100 if not crash_state else None, crash_state=crash_state
    )
    assert result.status == ("crashed" if crash_state else "published")
    if operation == "publish_snapshot" and crash_state:
        assert FileState(a.state_root).read().meta["snapshot_seq"] < result.seq
    f.action("a", "push")
    if operation == "publish_snapshot" and crash_state:
        assert FileState(a.state_root).read().meta["snapshot_seq"] == result.seq, (
            "snapshot recovery"
        )
        assert {
            "operation": "snapshot-recovery",
            "seq": result.seq,
        } in f.last_observation.history, "snapshot recovery history"
    if operation == "push" and crash_state:
        assert "crash-recovery" in f.coverage
    settle(f, min_participants=2)


def test_snapshot_crash_scenario_kills_missing_recovery(store_fleet):
    from .model import store
    from .model.registry import Piece

    f = store_fleet

    def without_snapshot_recovery(ctx, **kwargs):
        before = ctx.state.read()
        result = store.reconcile(ctx, **kwargs)
        with ctx.state.transaction() as state:
            state.meta["snapshot_seq"] = before.meta.get("snapshot_seq", 0)
            state.history = before.history + [
                event
                for event in state.history[len(before.history) :]
                if event.get("operation") != "snapshot-recovery"
            ]
        return result

    original = f.registry["reconcile"]
    f.registry["reconcile"] = Piece(
        without_snapshot_recovery, "model", original.owner, "snapshot recovery mutant"
    )
    with pytest.raises(AssertionError, match="snapshot recovery"):
        test_complete_publication_crash_recovery(f, "publish_snapshot", True)


def test_equal_vector_counter_reuse_after_publication_crash(store_fleet):
    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    assert f.action("a", "push", crash_state=True, attended=False).status == "crashed"
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)
    assert "counter-reuse" in f.coverage
    settle(f, min_participants=2)


def test_unreadable_commit_propagates_and_owner_snapshot_repairs(store_fleet):

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    target = next(b.replica_root.glob("dev/*/log/*.enc"))
    data = target.read_bytes()
    target.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    f.allow_corruption = True
    f.transport.scan("b")
    f.drain()
    assert f.action("b", "pull").status == "stalled-unreadable"
    assert f.action("a", "pull").status == "stalled-unreadable"
    result = f.action("a", "publish_snapshot", force=True)
    assert result.status == "published"
    f.drain()
    result = f.action("b", "pull")
    assert "snapshot-fallback" in result.events
    settle(f, min_participants=2)


@pytest.mark.xfail(
    strict=True, raises=ExpectedDesignGap, reason="restore-saved: §4/9 fork recovery missing"
)
def test_gap_restore_saved(store_fleet):
    f = store_fleet
    join_extra(f, "c")
    join_extra(f, "d")
    a = f.devices["a"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    for choice in f.transport.pending():
        if choice.dst == "b" and "/log/" in choice.path:
            f.transport.deliver(choice)
    f.restore(a, backup, index_policy="saved")
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)
    f.drain()
    assert f.action("a", "push").status == "refused"
    assert f.action("b", "push").status == "unchanged"
    assert f.action("b", "pull").status == "refused"
    f.oracle.check(f)
    assert len(f.participants()) >= 2
    with expected_gap(2):
        settle(f, min_participants=2)


@pytest.mark.xfail(
    strict=True, raises=ExpectedDesignGap, reason="restore-current: §4/9 seq reuse divergence"
)
def test_gap_restore_current(store_fleet):
    f = store_fleet
    join_extra(f, "c")
    join_extra(f, "d")
    a = f.devices["a"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    for name in ("b", "d"):
        f.action(name, "pull")
    f.restore(a, backup, index_policy="current")
    f.drain()
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)
    f.drain()
    a.edit("notes.txt", CONTENTS[3])
    f.action("a", "push", attended=False)
    f.drain()
    for name in ("b", "d"):
        result = f.action(name, "pull")
        assert (result.status, result.reason, result.peer) == ("refused", "fork", a.mm_id)
    f.oracle.check(f)
    assert len(f.participants()) >= 2
    with expected_gap(1):
        settle(f, min_participants=2)


def test_regression_retired_edit_remains_local(tmp_path, sim_write_guard, monkeypatch):
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        state.lifecycle_witness(case=0)
        state.core_witness(case=4)
        state.crash_rule(device=1, snapshot=False)
        state.backup_rule(device=0)
        state.backup_rule(device=0)
        state.retire_rule(device=0)
        state.replace(choice=0, finish=False)
        state.settle_rule()
        state.settle_rule()
        state.settle_rule()
        state.edit(content=0, device=0, old_mtime=False, path=2)
        state.fleet.oracle.check(state.fleet)
        state.settle_rule()
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


@pytest.mark.xfail(
    strict=True, raises=ExpectedDesignGap, reason="partial-own-commit: §5/9 no completion rule"
)
def test_gap_partial_own_commit(store_fleet):
    f = store_fleet
    for path in ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl"):
        f.devices["a"].edit(path, CONTENTS[3])
    assert f.action("a", "push", crash_parts=1, attended=False).status == "crashed"
    f.drain()
    assert f.action("b", "pull").status == "pending"
    assert f.action("a", "push").reason == "partial-own-commit"
    f.oracle.check(f)
    with expected_gap(1):
        settle(f, min_participants=2)


def test_partial_snapshot_preserves_commit_recovery(store_fleet):
    f = store_fleet
    for path in ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl"):
        f.devices["a"].edit(path, CONTENTS[3])
    assert f.action("a", "publish_snapshot", crash_parts=1).status == "crashed"
    assert FileState(f.devices["a"].state_root).read().own_log
    f.drain()
    settle(f, min_participants=2)
    # A partial immutable snapshot is not a complete bootstrap candidate.
    c = join_extra(f)
    settle(f, min_participants=2)
    assert (c.source_root / "notes.txt").read_bytes() == CONTENTS[3]


def test_regression_retired_plain_edit_remains_local(tmp_path, sim_write_guard, monkeypatch):
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        state.lifecycle_witness(case=2)
        state.core_witness(case=2)
        state.retire_rule(device=0)
        state.edit(content=2, device=3, old_mtime=True, path=2)
        state.retire_rule(device=3)
        state.edit(content=1, device=1, old_mtime=False, path=2)
        state.retire_rule(device=1)
        state.retire_rule(device=2)
        state.edit(content=7, device=2, old_mtime=True, path=0)
        state.edit(content=5, device=2, old_mtime=False, path=1)
        state.store(device=1, operation="publish_snapshot")
        state.retire_rule(device=0)
        state.retire_rule(device=0)
        state.settle_rule()
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


def test_mixed_publish_transaction_then_third_device_model_pull(store_fleet):
    """Independent 71C-shaped publisher fixture: no production publish exists yet."""
    import hashlib
    import os

    from mind_meld.store import format as codec
    from mind_meld.store import layout

    from .model import core
    from .model.registry import Piece
    from .model.state import from_row, path_key, row
    from .model.store import Outcome

    f = store_fleet
    c = join_extra(f)
    for name, data in (("a", CONTENTS[1]), ("b", CONTENTS[2])):
        f.devices[name].edit("MEMORY.md", data)
        f.action(name, "push", attended=False)
    f.drain()
    f.action("a", "pull")
    key = path_key("claude", "MEMORY.md")
    resolved = FileState(f.devices["a"].state_root).read().ledger[key]
    assert resolved["pending"]
    assert resolved["vv"] == {f.devices[n].mm_id: 1 for n in ("a", "b")}
    calls = []

    def independent_publish(ctx, **kwargs):
        calls.append(ctx.name)
        result = ctx.call("reconcile")
        if result.status != "applied":
            return result
        state = ctx.state.read()
        entries = []
        for _, value in sorted(state.ledger.items()):
            data, mtime = ctx.source(value["source"], value["path"])
            incoming = core.author(
                value["source"],
                value["path"],
                from_row(value),
                data,
                mtime,
                ctx.device_id,
                pending=value["pending"],
            )
            if incoming is not None:
                entries.append(incoming)
        if not entries:
            return Outcome("unchanged")
        seq = max(map(int, state.own_log), default=0) + 1
        prev = state.own_log[str(seq - 1)]["hash"] if seq > 1 else None
        expected = ctx.context(ctx.device_id, "commit", seq)
        head = dict(
            fleet_id=ctx.keys.fleet_id,
            device_id=ctx.device_id,
            host=ctx.host,
            seq=seq,
            prev=prev,
            created_at=ctx.clock.iso,
            mm_version="2.0.0",
            entries=len(entries),
        )
        payload = codec.encode_commit(head, tuple(entries), expected=expected)
        for part, sealed in codec.seal_record(payload, ctx.keys, expected=expected):
            target = ctx.replica_root / layout.record_key(
                ctx.device_id, "commit", seq, part=part.part, nparts=part.nparts
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.parent / ".mm-tmp-fixture"
            temporary.write_bytes(sealed)
            try:
                os.link(temporary, target)
            finally:
                temporary.unlink()
        digest = hashlib.sha256(payload).hexdigest()
        with ctx.state.transaction() as updated:
            for incoming in entries:
                updated.ledger[path_key(incoming.metadata["source"], incoming.metadata["path"])] = (
                    row(incoming)
                )
            updated.own_log[str(seq)] = dict(
                hash=digest,
                prev=prev,
                time=ctx.clock.now,
                created_at=ctx.clock.iso,
                bytes=len(payload),
                parts=part.nparts,
            )
            updated.cursors[ctx.device_id] = dict(applied_seq=seq, hash=digest)
            updated.history.append(dict(operation="publish", seq=seq))
        ctx.call("presence", changed=True)
        return Outcome("published", seq=seq)

    f.registry["publish"] = Piece(independent_publish, "real", "75A", "fixture substitution only")
    f.action("a", "push")
    published = FileState(f.devices["a"].state_root).read().ledger[key]
    assert not published["pending"]
    assert published["sha"] == resolved["sha"]
    assert published["vv"] == {f.devices["a"].mm_id: 2, f.devices["b"].mm_id: 1}
    f.drain()
    f.action("c", "pull")
    assert (c.source_root / "MEMORY.md").read_bytes() == CONTENTS[3]
    # C saw both earlier concurrent commits too: its own resolution remains
    # pending until C publishes, even though A's same-sha receipt merges vectors.
    before_publish = FileState(c.state_root).read().ledger[key]
    assert before_publish["pending"]
    assert f.action("c", "push").status == "published"
    after_publish = FileState(c.state_root).read().ledger[key]
    assert not after_publish["pending"]
    assert after_publish["vv"] == {
        **before_publish["vv"],
        c.mm_id: before_publish["vv"].get(c.mm_id, 0) + 1,
    }
    assert "a" in calls
    settle(f, min_participants=2)


def test_clone_before_first_presence_commit_fork_scope_and_rejoin(
    tmp_path, sim_write_guard, monkeypatch
):
    from mind_meld.store import format as codec
    from mind_meld.store import layout

    from .model.registry import Piece, default_registry
    from .model.store import Outcome
    from .store_fleet import PART_BYTES, StoreFleet

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    registry = default_registry()
    snapshot_piece = registry["snapshot"]
    registry["snapshot"] = Piece(
        lambda ctx, **kwargs: Outcome("unchanged"), "model", "76A", "prepresence fixture"
    )
    f = StoreFleet(
        tmp_path / "before-presence", write_guard=sim_write_guard, registry=registry
    ).bootstrap()
    assert not (
        f.devices["a"].replica_root / layout.record_key(f.devices["a"].mm_id, "presence", 0)
    ).exists()
    c = f.clone(f.devices["a"], "c")
    for name, data in (("a", CONTENTS[1]), ("c", CONTENTS[2])):
        f.devices[name].edit("notes.txt", data)
        assert f.action(name, "push", attended=False).status == "published"
    f.drain()
    assert any(
        layout.classify_keys([p.relative_to(c.replica_root).as_posix()]).forks
        for p in c.replica_root.rglob("*.enc")
    )
    for name in ("a", "c"):
        assert f.action(name, "push").reason == "fork"
        assert f.action(name, "pull").reason == "fork"
    assert f.action("b", "pull").reason == "fork"
    # Reader still applies an unrelated log despite refusing the forked id.
    f.devices["b"].edit("other.txt", CONTENTS[3])
    f.action("b", "push", attended=False)
    f.rejoin(c)
    f.registry["snapshot"] = snapshot_piece
    f.drain()
    assert f.action("c", "pull").reason == "fork"
    assert (c.source_root / "other.txt").read_bytes() == CONTENTS[3]
    f.oracle.check(f)
    settle(f, min_participants=2)


def test_fleet_header_fork_refuses_every_device(store_fleet):
    f = store_fleet
    settle(f, min_participants=2)
    for d in f.ordered():
        copy = d.replica_root / "fleet.sync-conflict-20261013-000000-a.json"
        copy.write_bytes((d.replica_root / "fleet.json").read_bytes())
        assert f.action(d.name, "push").reason == "fork"
        assert f.action(d.name, "pull").reason == "fork"
    # Inspection is global refusal; return to the already demonstrated 2-device Q.
    for d in f.ordered():
        for copy in d.replica_root.glob("fleet.sync-conflict-*.json"):
            copy.unlink()
        with FileState(d.state_root).transaction() as state:
            state.meta.pop("refused", None)
        f.transport.scan(d.name)
    f.drain()
    settle(f, min_participants=2)


@pytest.mark.parametrize("anchor_kind", ["snapshot", "commit"])
def test_snapshot_fallback_raises_other_cursors_and_waits_for_hash_anchor(store_fleet, anchor_kind):
    from mind_meld.store import layout

    f = store_fleet
    c = join_extra(f)
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    for item in f.transport.pending():
        if item.src == "a" and item.dst == "b" and not item.reasons:
            f.transport.deliver(item)
    f.action("b", "pull")
    b.edit("other.txt", CONTENTS[2])
    f.action("b", "publish_snapshot")
    for item in f.transport.pending():
        if item.dst == "c" and item.src == "b" and "/snap/" in item.path and not item.reasons:
            f.transport.deliver(item)
    assert "snapshot-fallback" in f.action("c", "pull").events
    cursor = FileState(c.state_root).read().cursors[a.mm_id]
    assert cursor == {"applied_seq": 1, "hash": None}
    f.action("a", "publish_snapshot")
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)
    for item in f.transport.pending():
        parsed = layout.parse_record_key(item.path)
        if (
            item.dst == "c"
            and parsed
            and parsed.device_id == a.mm_id
            and parsed.kind == "commit"
            and parsed.seq == 2
            and not item.reasons
        ):
            f.transport.deliver(item)
    assert f.action("c", "pull").reason == "chain-anchor"
    for item in f.transport.pending():
        parsed = layout.parse_record_key(item.path)
        if (
            item.dst == "c"
            and parsed
            and parsed.device_id == a.mm_id
            and parsed.kind == anchor_kind
            and parsed.seq == 1
            and not item.reasons
        ):
            f.transport.deliver(item)
    f.action("c", "pull")
    assert FileState(c.state_root).read().cursors[a.mm_id]["applied_seq"] == 2
    settle(f, min_participants=2)


def test_replayed_commit_retains_original_age_and_multipart_provenance(store_fleet):
    from mind_meld.store import layout

    f = store_fleet
    a = f.devices["a"]
    for path in ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl"):
        a.edit(path, CONTENTS[3])
    created = f.clock.now
    assert f.action("a", "push", crash_state=True, attended=False).status == "crashed"
    f.advance(14 * 86400)
    f.action("a", "publish_snapshot")
    replayed = FileState(a.state_root).read().own_log["1"]
    assert replayed["time"] == created
    assert replayed["parts"] >= 2
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "publish_snapshot")
    # Previous snapshot covers seq 1; the crash/replay did not restart its age.
    assert not any(
        n and n.kind == "commit" and n.seq == 1
        for p in a.replica_root.rglob("*.enc")
        if (n := layout.parse_record_key(p.relative_to(a.replica_root).as_posix()))
    )
    settle(f, min_participants=2)


def test_regression_compacted_gap_keeps_warning_until_snapshot_arrives(
    tmp_path, sim_write_guard, monkeypatch
):
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, CoreMachine, Invocation

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = CoreMachine()
    try:
        state.core_witness(case=0)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.edit(content=0, device=0, old_mtime=True, path=0)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.store(device=0, operation="push")
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.edit(content=0, device=0, old_mtime=False, path=0)
        state.observations_hold()
        state.edit(content=0, device=0, old_mtime=False, path=3)
        state.observations_hold()
        state.replace(choice=963, finish=True)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.replace(choice=261, finish=False)
        state.observations_hold()
        state.store(device=0, operation="publish_snapshot")
        state.observations_hold()
        state.replace(choice=392, finish=True)
        state.observations_hold()
        state.time_advance(seconds=1209600)
        state.observations_hold()
        state.edit(content=6, device=0, old_mtime=True, path=1)
        state.observations_hold()
        state.edit(content=1, device=3, old_mtime=True, path=0)
        state.observations_hold()
        state.edit(content=6, device=0, old_mtime=False, path=3)
        state.observations_hold()
        state.time_advance(seconds=2592000)
        state.observations_hold()
        state.time_advance(seconds=86400)
        state.observations_hold()
        state.store(device=0, operation="compact")
        state.observations_hold()
        state.replace(choice=611, finish=True)
        state.observations_hold()
        state.replace(choice=177, finish=False)
        state.observations_hold()
        state.store(device=3, operation="publish_snapshot")
        state.observations_hold()
        state.store(device=1, operation="push")
        state.observations_hold()
        state.edit(content=2, device=0, old_mtime=True, path=1)
        state.observations_hold()
        state.store(device=0, operation="compact")
        state.observations_hold()
        state.edit(content=4, device=3, old_mtime=True, path=1)
        state.observations_hold()
        state.replace(choice=104, finish=False)
        state.observations_hold()
        state.replace(choice=194, finish=True)
        state.observations_hold()
        state.time_advance(seconds=2592000)
        state.observations_hold()
        state.replace(choice=1000, finish=False)
        state.observations_hold()
        state.edit(content=5, device=2, old_mtime=True, path=1)
        state.observations_hold()
        state.replace(choice=225, finish=True)
        state.observations_hold()
        state.edit(content=6, device=0, old_mtime=False, path=1)
        state.observations_hold()
        state.edit(content=4, device=3, old_mtime=False, path=2)
        state.observations_hold()
        state.edit(content=1, device=3, old_mtime=False, path=3)
        state.observations_hold()
        state.time_advance(seconds=2592000)
        state.observations_hold()
        state.edit(content=4, device=1, old_mtime=False, path=3)
        state.observations_hold()
        state.replace(choice=4, finish=False)
        state.observations_hold()
        state.replace(choice=205, finish=False)
        state.observations_hold()
        state.store(device=3, operation="pull")
        state.observations_hold()
        state.time_advance(seconds=1209600)
        state.observations_hold()
        state.replace(choice=310, finish=True)
        state.observations_hold()
        state.replace(choice=148, finish=True)
        state.observations_hold()
        state.time_advance(seconds=1209600)
        state.observations_hold()
        state.store(device=0, operation="publish_snapshot")
        state.observations_hold()
        state.edit(content=2, device=3, old_mtime=True, path=3)
        state.observations_hold()
        state.store(device=2, operation="pull")
        state.observations_hold()
        state.replace(choice=169, finish=True)
        state.observations_hold()
        state.edit(content=1, device=3, old_mtime=True, path=0)
        state.observations_hold()
        state.replace(choice=193, finish=False)
        state.observations_hold()
        state.replace(choice=505, finish=True)
        state.observations_hold()
        state.store(device=1, operation="push")
        state.observations_hold()
        state.store(device=0, operation="push")
        state.observations_hold()
        state.time_advance(seconds=3600)
        state.observations_hold()
        state.store(device=1, operation="pull")
        state.observations_hold()
        state.replace(choice=882, finish=True)
        state.observations_hold()
        state.replace(choice=766, finish=True)
        state.observations_hold()
        state.store(device=3, operation="pull")
        state.observations_hold()
        state.replace(choice=454, finish=False)
        state.observations_hold()
        state.replace(choice=117, finish=False)
        state.observations_hold()
        state.replace(choice=226, finish=True)
        state.observations_hold()
        state.time_advance(seconds=2592000)
        state.observations_hold()
        state.time_advance(seconds=1209600)
        state.observations_hold()
        state.replace(choice=137, finish=False)
        state.observations_hold()
        state.replace(choice=96, finish=False)
        state.observations_hold()
        state.edit(content=1, device=3, old_mtime=True, path=2)
        state.observations_hold()
        state.time_advance(seconds=1)
        state.observations_hold()
        state.store(device=2, operation="publish_snapshot")
        state.observations_hold()
        state.replace(choice=173, finish=True)
        state.observations_hold()
        state.replace(choice=231, finish=True)
        state.observations_hold()
        state.store(device=0, operation="push")
        state.observations_hold()
        state.time_advance(seconds=86400)
        state.observations_hold()
        state.replace(choice=433, finish=False)
        state.observations_hold()
        state.time_advance(seconds=1209600)
        state.observations_hold()
        state.store(device=1, operation="publish_snapshot")
        state.observations_hold()
        state.store(device=3, operation="pull")
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


@pytest.mark.parametrize("failure", ["pending", "unreadable"])
def test_join_preserves_peer_read_diagnostics(store_fleet, failure):
    # Value: protects=join reports actual peer gap/stall; fails_when=join drops
    # read diagnostics; why_new=join tests only inspect status; seam=existing fleet.
    from mind_meld.store import layout

    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    if failure == "pending":
        a.edit("notes.txt", CONTENTS[2])
        f.action("a", "push", attended=False)
    c = f.add_device("c")
    if failure == "pending":
        # Deliver header, complete initial snapshots and the later commit;
        # keep commit 1 in flight, so join encounters a real sequence gap.
        for choice in f.transport.pending():
            parsed = layout.parse_record_key(choice.path)
            wanted = choice.path == "fleet.json" or (
                parsed is not None and (parsed.kind != "commit" or parsed.seq == 2)
            )
            if choice.dst == "c" and not choice.reasons and wanted:
                f.transport.deliver(choice)
    else:
        f.drain()
        target = next(c.replica_root.glob(f"dev/{a.mm_id}/log/*.enc"))
        original = target.read_bytes()
        target.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        f.allow_corruption = True

    result = f.action("c", "join")
    status = "pending" if failure == "pending" else "stalled-unreadable"
    assert (result.status, result.peer, result.gap_seq) == (status, a.mm_id, 1)
    assert {"join-snapshot", "gap-pending"} <= set(result.events)
    if failure == "unreadable":
        assert result.warnings

    # Resolve the delivered defect through the existing fixture protocol.
    f.drain()
    if failure == "unreadable":
        f.action("a", "publish_snapshot", force=True)
        f.drain()
    settle(f, min_participants=2)


def test_retire_waits_for_active_peer_snapshot_coverage(store_fleet):
    # Value: protects=retirement preserves target records until active peers
    # snapshot them; fails_when=coverage guard is removed; why_new=age test always
    # supplies covering snapshots; seam=existing fleet and persisted replica tree.
    from .transport import read_tree

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    c = join_extra(f)
    # C's initial snapshot precedes B's first commit.
    b.edit("notes.txt", CONTENTS[1])
    settle(f, min_participants=2)

    f.advance(30 * 86400)
    f.action("c", "push")  # Refresh active presence without a new snapshot.
    a.edit("other.txt", CONTENTS[2])
    f.action("a", "publish_snapshot")
    f.drain()

    target = a.replica_root / "dev" / b.mm_id
    before = read_tree(target)
    assert before
    result = f.action("a", "retire", target=b.mm_id)
    assert (result.status, result.reason, result.peer) == ("pending", "snapshot-coverage", b.mm_id)
    assert read_tree(target) == before

    c.edit("other.txt", CONTENTS[3])
    f.action("c", "publish_snapshot")
    f.drain()
    assert f.action("a", "retire", target=b.mm_id).status == "retired"
    assert not target.exists()
    f.drain()
    settle(f, min_participants=2)


def test_established_device_keeps_concurrent_live_file_on_snapshot_fallback(store_fleet):
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    a.delete("notes.txt")
    f.action("a", "publish_snapshot")
    b.edit("notes.txt", CONTENTS[2])
    for item in f.transport.pending():
        if item.dst == "b" and "/snap/" in item.path and not item.reasons:
            f.transport.deliver(item)
    assert "snapshot-fallback" in f.action("b", "pull").events
    assert (b.source_root / "notes.txt").read_bytes() == CONTENTS[2]
    assert any(r.get("action") == "resolve" for r in f.last_observation.history)
    settle(f, min_participants=2)


def test_rejoin_dispatch_observes_and_owns_identity_reset(store_fleet):
    from .model.registry import Piece

    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    settle(f)
    before = FileState(a.state_root).read()
    old_id = a.mm_id
    original = f.registry["rejoin"]

    def replacement(ctx, **kwargs):
        from .model.store import Outcome

        assert ctx.device_id == old_id
        assert ctx.state.read() == before
        return Outcome("unchanged")

    f.registry["rejoin"] = Piece(replacement, "real", "80A", "observed identity transition")
    f.rejoin(a)
    assert a.mm_id == old_id
    assert FileState(a.state_root).read() == before
    f.registry["rejoin"] = original
    f.rejoin(a)
    assert a.mm_id != old_id
    assert f.last_observation.before_state == before
    assert f.last_observation.state.meta["device_id"] == a.mm_id
    settle(f, min_participants=2)


def test_saved_restore_redelivery_replays_own_log_and_converges(store_fleet):
    f = store_fleet
    a = f.devices["a"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    f.action("b", "pull")
    f.restore(a, backup, index_policy="saved")
    f.drain()
    assert f.action("a", "push", attended=False).status == "unchanged"
    assert any(r.get("operation") == "own-replay" for r in f.last_observation.history)
    assert "1" in FileState(a.state_root).read().own_log
    settle(f, min_participants=2)
    assert (a.source_root / "notes.txt").read_bytes() == CONTENTS[1]


@pytest.mark.parametrize("invalid", ["negative", "future"])
def test_edit_rejects_invalid_mtime_without_mutation(store_fleet, invalid):
    from copy import deepcopy

    from .transport import read_tree

    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    before = read_tree(a.source_root)
    versions, edges = deepcopy(f.oracle.versions), deepcopy(f.oracle.edges)
    steps = list(f.steps)
    stamp = -1 if invalid == "negative" else f.clock.now + 1
    with pytest.raises(ValueError, match="logical clock"):
        a.edit("notes.txt", CONTENTS[2], mtime=stamp)
    assert read_tree(a.source_root) == before
    assert f.oracle.versions == versions
    assert f.oracle.edges == edges
    assert f.steps == steps


@pytest.mark.parametrize("fork_kind", ["peer-log", "fleet-header"])
def test_aged_gap_then_fork_keeps_warning_and_refusal(store_fleet, fork_kind):
    from .store_fleet import DAY

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    for data in CONTENTS[1:3]:
        a.edit("notes.txt", data)
        f.action("a", "push", attended=False)
    for choice in f.transport.pending():
        if choice.dst == "b" and "/log/000000000002" in choice.path and not choice.reasons:
            f.transport.deliver(choice)
    assert f.action("b", "pull").status == "pending"
    f.advance(DAY + 1)
    if fork_kind == "peer-log":
        part = next(a.replica_root.glob(f"dev/{a.mm_id}/log/000000000001*.enc"))
        destination = b.replica_root / part.relative_to(a.replica_root)
        conflict = destination.with_name(part.stem + ".sync-conflict-20261014-120000-a.enc")
        conflict.write_bytes(part.read_bytes())
        expected_peer = a.mm_id
    else:
        conflict = b.replica_root / "fleet.sync-conflict-20261014-120000-b.json"
        conflict.write_bytes((b.replica_root / "fleet.json").read_bytes())
        expected_peer = None
    result = f.action("b", "pull")
    assert (result.status, result.reason, result.peer) == ("refused", "fork", expected_peer)
    assert result.warnings
    assert FileState(b.state_root).read().cursors[a.mm_id]["applied_seq"] == 0
    f.oracle.check(f)


def test_rejoin_records_fresh_identity_publication(store_fleet):
    # Value: protects=causal attribution across own-log reset;
    # fails_when=publication detection compares only sequence-key growth;
    # why_new=ordinary rejoin witness had no concurrent local edit;
    # seam=normal clone/edit/rejoin actions and persisted publication history.
    f = store_fleet
    a = f.devices["a"]
    a.edit("MEMORY.md", CONTENTS[1])
    settle(f, min_participants=2)
    c = f.clone(a, "c")
    assert f.action("c", "push").status == "refused"
    c.edit("MEMORY.md", CONTENTS[2])
    before = FileState(c.state_root).read()
    old_identity = c.mm_id
    assert "1" in before.own_log
    f.rejoin(c)
    assert c.mm_id != old_identity
    assert "1" in FileState(c.state_root).read().own_log
    assert any(row["operation"] == "publish" for row in f.last_observation.history)
    settle(f, min_participants=2)
    assert all((d.source_root / "MEMORY.md").read_bytes() == CONTENTS[3] for d in f.participants())


def test_partial_snapshot_is_not_resealed_after_compaction(store_fleet):
    # Value: protects=immutable snapshot names retain their original bytes;
    # fails_when=compaction deletes a partial current sequence then republishes it;
    # why_new=partial-snapshot reader test never compacted the owner;
    # seam=real sealed records on the model replica, no native transport claim.
    from mind_meld.store import layout

    from .store_fleet import PATHS

    f = store_fleet
    a = f.devices["a"]
    for path in PATHS:
        a.edit(path, CONTENTS[3])
    crash = f.action("a", "publish_snapshot", crash_parts=1)
    assert crash.status == "crashed"

    def parts():
        result = {}
        for part in a.replica_root.rglob("*.enc"):
            key = part.relative_to(a.replica_root).as_posix()
            name = layout.parse_record_key(key)
            if (
                name
                and name.device_id == a.mm_id
                and name.kind == "snapshot"
                and name.seq == crash.seq
            ):
                result[key] = part.read_bytes()
        return result

    original = parts()
    assert len(original) == 1
    f.action("a", "compact")
    assert parts() == original
    f.advance(1)
    retry = f.action("a", "publish_snapshot")
    assert (retry.status, retry.seq) == ("unchanged", crash.seq)
    assert parts() == original
    # Keep the partial family until two newer complete snapshots cover it.
    for data, retained in ((CONTENTS[1], True), (CONTENTS[2], False)):
        a.edit("notes.txt", data)
        assert f.action("a", "publish_snapshot").status == "published"
        assert bool(parts()) is retained
    f.drain()
    settle(f, min_participants=2)


@pytest.mark.xfail(
    strict=True,
    raises=ExpectedDesignGap,
    reason="retire-delayed-presence: §6/8 can strand an active peer",
)
def test_gap_retire_delayed_active_presence(store_fleet):
    # Value: pins the E23 membership ambiguity without weakening convergence;
    # fails_when=an active peer is omitted using stale local presence;
    # seam=public model actions plus finite transport delivery choices.
    from mind_meld.store import layout

    from .store_fleet import DAY

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    c = f.add_device("c")
    f.drain()
    assert f.action("c", "join").status == "applied"
    f.drain()
    b.edit("notes.txt", CONTENTS[1])
    assert f.action("b", "push", attended=False).status == "published"
    for choice in f.transport.pending():
        parsed = layout.parse_record_key(choice.path)
        if choice.dst == "a" and parsed and parsed.device_id == b.mm_id and not choice.reasons:
            f.transport.deliver(choice)
    assert f.action("a", "pull").status == "applied"
    assert (a.source_root / "notes.txt").read_bytes() == CONTENTS[1]
    assert not (c.source_root / "notes.txt").exists()

    f.advance(30 * DAY)
    f.action("c", "push", attended=False)  # Keep this refreshed presence in flight.
    a.edit("other.txt", CONTENTS[2])
    snapshot = f.action("a", "publish_snapshot")
    assert snapshot.status == "published"
    for choice in f.transport.pending():
        parsed = layout.parse_record_key(choice.path)
        if (
            choice.dst == "c"
            and parsed
            and parsed.device_id == a.mm_id
            and parsed.kind == "commit"
            and not choice.reasons
        ):
            f.transport.deliver(choice)
    assert f.action("c", "pull").status == "applied"
    assert FileState(c.state_root).read().cursors[a.mm_id]["applied_seq"] == snapshot.seq
    assert not (c.source_root / "notes.txt").exists()
    assert f.action("a", "retire", target=b.mm_id).status == "retired"
    f.drain()
    with expected_gap(1):
        settle(f, min_participants=2)
