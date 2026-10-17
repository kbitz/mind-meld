"""Deterministic witnesses, separate from Hypothesis search."""

import re
from contextlib import contextmanager
from pathlib import Path

import pytest

from .invariants import InvariantError, settle
from .model import store as model_store
from .model.state import FileState
from .model.store import owner_text
from .store_fleet import CONTENTS, DAY


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
    # Complete the family: completeness is judged from names, so only a name-complete
    # family is opened and only then can a part vanish between listing and reading.
    for choice in choices[1:]:
        f.transport.deliver(choice)
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
        refused = f.action("b", "push")
        assert refused.reason == "missing-own-directory"
        assert (
            "its newest publish was " in refused.warnings[0]
            and "`mm rejoin`" in refused.warnings[0]
        )
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


def alpha_bravo_restore(f, *, deliver_to="b"):
    """Saved restore with a reused seq: alpha (pre-restore) reached ``deliver_to``,
    bravo (post-restore) is a different record under the same seq."""
    join_extra(f, "c")
    join_extra(f, "d")
    a = f.devices["a"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])  # alpha
    f.action("a", "push", attended=False)
    for choice in f.transport.pending():
        if choice.dst == deliver_to and "/log/" in choice.path:
            f.transport.deliver(choice)
    f.restore(a, backup, index_policy="saved")
    a.edit("notes.txt", CONTENTS[2])  # bravo
    f.action("a", "push", attended=False)
    f.drain()
    return a, backup


def test_gap_restore_saved(store_fleet):
    # Value: protects=a saved restore that reuses a seq strands neither version and the
    # fleet converges; fails_when=the second family is refused, dropped or left as an
    # unapplied record; why_new=was a strict design gap; seam=none;
    # no_claim=native conflict behaviour.
    f = store_fleet
    a, _ = alpha_bravo_restore(f)
    a_id = a.mm_id
    # The forked device alone refuses (same host, own chain diverged); it keeps bravo.
    assert f.action("a", "push").status == "refused"
    assert f.action("b", "push").status == "unchanged"
    # Readers never refuse: b applies the second family and warns, naming the remedy.
    result = f.action("b", "pull")
    assert result.status == "applied"
    assert {"fork-evidence", "late-family"} <= set(result.events)
    (warning,) = [w for w in result.warnings if a_id in w]
    assert "two different records for push #1" in warning
    assert f"run `mm rejoin` on {a_id}" in warning
    fork = FileState(f.devices["b"].state_root).read().cursors[a_id]["fork"]
    assert (fork["kind"], fork["seq"], fork["clone"]) == ("family", 1, False)
    f.oracle.check(f)
    assert len(f.participants()) >= 2
    settle(f, min_participants=2)  # Alpha and bravo are each current or displaced (INV2).
    # Rejoining the forked device lets all four devices converge.
    f.rejoin(a)
    assert a.mm_id != a_id
    settle(f, min_participants=4)


def test_gap_restore_current(store_fleet):
    # Value: protects=late families reconcile reused seqs after a current-policy restore;
    # fails_when=b/d keep alpha while a/c hold the merge; why_new=was a strict design gap;
    # seam=none; no_claim=current-policy restores remain an unsupported setup (the
    # residual pins live in test_gap_restore_current_residual).
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
    # Nothing has arrived yet that claims push #1, so the restored device publishes at
    # the reused seq (a publish before any own record or receipt arrives is residual 4).
    a.edit("notes.txt", CONTENTS[2])
    assert f.action("a", "push", attended=False).status == "published"
    f.drain()
    a.edit("notes.txt", CONTENTS[3])
    f.action("a", "push", attended=False)
    f.drain()
    for name in ("b", "d"):
        result = f.action(name, "pull")
        # The applied family vanished from storage and a new digest holds its slot.
        assert (result.status, result.reason) == ("applied", None)
        assert any("republished an earlier sequence" in w for w in result.warnings)
        cursor = FileState(f.devices[name].state_root).read().cursors[a.mm_id]
        assert (cursor["fork"]["kind"], cursor["applied_seq"]) == ("republished", 2)
    f.oracle.check(f)
    assert len(f.participants()) >= 2
    settle(f, min_participants=2)
    assert {(d.source_root / "notes.txt").read_bytes() for d in f.participants()} == {CONTENTS[3]}


def receipts(device, peer_id):
    """(seq, family) of every commit this device applied from ``peer_id``, in order."""
    return [
        (r["seq"], r["family"])
        for r in FileState(device.state_root).read().history
        if r.get("operation") == "record-applied" and r["peer"] == peer_id and r["kind"] == "commit"
    ]


def test_same_slot_families_apply_in_family_order_not_publication_order(store_fleet):
    # Value: protects=apply order is a pure function of names; fails_when=a reader
    # assumes the lexically later family is the later publication; why_new=a monotonic
    # family counter hid this; seam=family_source; no_claim=none beyond protects.
    f = store_fleet
    join_extra(f, "c")
    a = f.devices["a"]
    backup = f.snapshot(a)
    f.force_families("ffffffff")  # Alpha, published first, sorts LAST.
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    for item in f.transport.pending():
        if item.dst == "c" and "/log/" in item.path:
            f.transport.deliver(item)  # c holds alpha, so the restore cannot erase it.
    f.restore(a, backup, index_policy="saved")
    f.force_families("00000000")  # Bravo, published second, sorts FIRST.
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)
    f.drain()
    c = f.devices["c"]
    assert f.action("c", "pull").status == "applied"
    assert receipts(c, a.mm_id) == [(1, "00000000"), (1, "ffffffff")]
    state = FileState(c.state_root).read()
    first = state.applied[f"{a.mm_id}:1:00000000"]["digest"]
    assert state.cursors[a.mm_id]["hash"] == first  # The first applied family anchors.
    settle(f, min_participants=2)


def test_a_renamed_copy_of_one_record_is_one_record(store_fleet):
    # Value: protects=equal digests are one record, not a fork; fails_when=a second
    # family name for the same payload applies twice or raises fork evidence;
    # why_new=families are new; seam=none; no_claim=none beyond protects.
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    original = next(b.replica_root.glob(f"dev/{a.mm_id}/log/000000000001.*.enc"))
    copy = original.with_name("000000000001.0badf00d.enc")
    copy.write_bytes(original.read_bytes())  # The family is not in the AAD.
    result = f.action("b", "pull")
    assert result.status == "applied" and not result.warnings
    state = FileState(b.state_root).read()
    assert len([r for r in state.applied.values() if r["peer"] == a.mm_id]) == 1
    assert "fork" not in state.cursors[a.mm_id]
    settle(f, min_participants=2)


def test_torn_family_beside_a_complete_family_of_another_part_count(store_fleet):
    # Value: protects=only complete families count and mixed part counts at one slot are
    # no fork signal; fails_when=a reader opens the torn parts, stalls or records fork
    # evidence; why_new=today mixed counts raise invalid_parts; seam=none;
    # no_claim=native transport ordering.
    from mind_meld.store import layout

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    multipart_edit(a)  # A 3-part commit; the crash leaves part 1.
    assert f.action("a", "push", crash_parts=1, attended=False).status == "crashed"
    for path in MULTIPART_PATHS:
        a.edit(path, path[:1].encode() * 40)  # The re-author is smaller: 2 parts.
    result = f.action("a", "push", attended=False)
    assert (result.status, result.events) == ("published", ("torn-supersede",))
    counts = {}
    for key in own_log_parts(a):
        name = layout.parse_record_key(key)
        counts.setdefault(name.family, set()).add(name.nparts)
    assert sorted(len(v) for v in counts.values()) == [1, 1]
    assert sorted(n for v in counts.values() for n in v) == [2, 3]
    f.drain()
    result = f.action("b", "pull")
    assert result.status == "applied" and not result.warnings
    assert "fork" not in FileState(b.state_root).read().cursors[a.mm_id]
    assert (b.source_root / "notes.txt").read_bytes() == b"n" * 40
    settle(f, min_participants=2)


def test_unreadable_family_beside_a_complete_one_is_ignored_with_a_warning(store_fleet):
    # Value: protects=a spliced or corrupt family cannot stall a slot; fails_when=a
    # reader or the owner stalls or refuses; why_new=slot rule is new; seam=none;
    # no_claim=cryptographic writer authentication.
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    f.allow_corruption = True
    for device in (a, b):
        good = next(device.replica_root.glob(f"dev/{a.mm_id}/log/000000000001.*.enc"))
        data = good.read_bytes()
        good.with_name("000000000001.baadf00d.enc").write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    result = f.action("b", "pull")
    assert result.status == "applied"
    assert result.warnings == (
        f"{a.mm_id} record 1 has an unreadable copy beside a complete one; ignored; run `mm diag`",
    )
    assert (b.source_root / "notes.txt").read_bytes() == CONTENTS[1]
    # The owner ignores its own spliced copy the same way and keeps publishing.
    a.edit("notes.txt", CONTENTS[2])
    result = f.action("a", "push", attended=False)
    assert result.status == "published" and result.seq == 2
    assert any("unreadable copy beside a complete one" in w for w in result.warnings)


def test_family_with_differing_part_counts_is_unreadable_beside_a_complete_one(store_fleet):
    # Value: protects=one family name has exactly one part count, so a spliced family
    # is unreadable, not pending; fails_when=grouping treats differing counts as an
    # incomplete family (no warning beside a complete one, a pending stall alone) or
    # opens the mixed parts as one record; why_new=the unreadable witness above
    # corrupts bytes, exercising authentication failure, not the name-only count
    # check in the model slot view and the checker's family grouping; seam=none;
    # no_claim=cryptographic writer authentication.
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    f.allow_corruption = True
    good = next(b.replica_root.glob(f"dev/{a.mm_id}/log/000000000001.*.enc"))
    data = good.read_bytes()
    # One family name, two part counts: a splice, judged from the names alone.
    good.with_name("000000000001.baadf00d.1of2.enc").write_bytes(data)
    good.with_name("000000000001.baadf00d.2of3.enc").write_bytes(data)
    result = f.action("b", "pull")
    assert result.status == "applied"
    assert result.warnings == (
        f"{a.mm_id} record 1 has an unreadable copy beside a complete one; ignored; run `mm diag`",
    )
    assert (b.source_root / "notes.txt").read_bytes() == CONTENTS[1]
    assert "fork" not in FileState(b.state_root).read().cursors[a.mm_id]
    settle(f, min_participants=2)


@pytest.mark.parametrize("complete_at_cover", [False, True])
def test_a_snapshot_cursor_covers_no_family_so_a_late_family_applies(
    store_fleet, complete_at_cover
):
    # Value: protects=covered means incorporated; fails_when=a reader skips a family it
    # observed (incomplete or complete) at cover time but never applied; why_new=cover
    # rule is new; seam=none; no_claim=none beyond protects.
    from mind_meld.store import layout

    f = store_fleet
    c = join_extra(f)
    a, b = f.devices["a"], f.devices["b"]
    multipart_edit(a)
    assert f.action("a", "push", attended=False).status == "published"
    a_to_b = [i for i in f.transport.pending() if i.src == "a" and i.dst == "b" and not i.reasons]
    for item in a_to_b:
        f.transport.deliver(item)
    assert f.action("b", "pull").status == "applied"
    b.edit("other.txt", CONTENTS[2])
    f.action("b", "publish_snapshot")  # Its cursors[a] = 1 covers none of a's families.
    for item in f.transport.pending():
        if item.dst == "c" and item.src == "b" and "/snap/" in item.path and not item.reasons:
            f.transport.deliver(item)
    parts = [
        i
        for i in f.transport.pending()
        if i.src == "a" and i.dst == "c" and "/log/" in i.path and not i.reasons
    ]
    assert len(parts) == 3
    for item in parts if complete_at_cover else parts[:1]:
        f.transport.deliver(item)
    result = f.action("c", "pull")
    assert "snapshot-fallback" in result.events
    state = FileState(c.state_root).read()
    assert state.cursors[a.mm_id]["applied_seq"] == 1
    applied = [r for r in state.applied.values() if r["peer"] == a.mm_id]
    assert len(applied) == (1 if complete_at_cover else 0)
    for item in [] if complete_at_cover else parts[1:]:
        f.transport.deliver(item)
    result = f.action("c", "pull")
    applied = [r for r in FileState(c.state_root).read().applied.values() if r["peer"] == a.mm_id]
    assert len(applied) == 1 and ("late-family" in result.events) is not complete_at_cover
    assert layout.parse_record_key(next(iter(own_log_parts(a)))).family == applied[0]["family"]
    assert "fork" not in FileState(c.state_root).read().cursors[a.mm_id]
    settle(f, min_participants=2)


def derived_edit_then_restore(f, *, dominating_first):
    """Alpha reaches b and c; c derives a newer edit; a is restored (saved) and publishes
    bravo under the reused counter. Returns (a, b, c, bravo)."""
    c = join_extra(f, "c")
    a, b = f.devices["a"], f.devices["b"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])  # Alpha.
    f.action("a", "push", attended=False)
    f.drain()
    for name in ("b", "c"):
        f.action(name, "pull")
    c.edit("notes.txt", CONTENTS[5])  # Derived from alpha: vv {a:1, c:1}.
    f.action("c", "push", attended=False)
    f.drain()
    if dominating_first:
        f.action("b", "pull")  # b already holds the dominating version.
    f.restore(a, backup, index_policy="saved")
    a.edit("notes.txt", CONTENTS[2])  # Bravo: {a:1} again.
    f.action("a", "push", attended=False)
    f.drain()
    return a, b, c, CONTENTS[2]


@pytest.mark.parametrize("stop", ["expiry", "restored"])
def test_fork_displaced_rows_repeat_the_warning_until_expiry_or_restore(store_fleet, stop):
    # Value: protects=a post-restore edit a reader would drop stays recoverable and the
    # warning repeats while it is at risk; fails_when=the dominated bravo is ignored
    # without a trace or the warning is shown once then silent; why_new=ignore never
    # preserved bytes; seam=none; no_claim=mm status rendering (81A) and the restore
    # operation itself (78A, stood in for by the row's restored flag).
    from datetime import datetime, timezone

    f = store_fleet
    a, b, c, bravo = derived_edit_then_restore(f, dominating_first=True)
    result = f.action("b", "pull")
    assert result.status == "applied" and "fork-evidence" in result.events
    state = FileState(b.state_root).read()
    (row,) = [v for v in state.displaced if v.get("cause") == "fork"]
    assert (row["peer"], row["content"], row["reason"]) == (a.mm_id, bravo, "ignored-fork")
    expiry = datetime.fromtimestamp(row["time"] + 30 * DAY, timezone.utc).date().isoformat()
    text = (
        f"{a.mm_id} has two different records for push #1 (restored from a backup?); "
        f"both versions were kept; run `mm rejoin` on {a.mm_id}; 1 displaced version kept "
        f"until {expiry}; see `mm displaced` before then"
    )
    assert result.warnings == (text,)
    assert state.meta["fork_breadcrumb"] == {
        a.mm_id: {"displaced": 1, "earliest": row["time"] + 30 * DAY}
    }
    # Every later push and pull warns again while the version is unexpired and unrestored.
    for operation in ("push", "pull", "push", "pull"):
        assert text in f.action("b", operation).warnings
    f.oracle.check(f)
    if stop == "restored":
        with FileState(b.state_root).transaction() as updated:
            updated.displaced[updated.displaced.index(row)]["restored"] = True
    else:
        f.advance(30 * DAY)
        f.action("b", "compact")
        assert FileState(b.state_root).read().displaced[0]["expired"]
    for operation in ("push", "pull"):
        result = f.action("b", operation)
        assert not [w for w in result.warnings if a.mm_id in w]
    assert "fork_breadcrumb" not in FileState(b.state_root).read().meta
    # The warning stops, but the peer stays listed in cursors.fork until retired.
    assert FileState(b.state_root).read().cursors[a.mm_id]["fork"]["kind"] == "family"
    # The restored device keeps bravo current until it rejoins; rejoin lists what it
    # displaces and tags those rows like the peer fork's.
    assert (a.source_root / "notes.txt").read_bytes() == bravo
    before = len(FileState(a.state_root).read().displaced)
    old_id = a.mm_id
    f.rejoin(a)
    rejoined = FileState(a.state_root).read().displaced[before:]
    assert rejoined and all(v["cause"] == "fork" and v["peer"] == old_id for v in rejoined)
    settle(f, min_participants=3)


def test_fork_evidence_arriving_after_classification_still_keeps_the_ignored_edit(store_fleet):
    # Value: protects=bravo is kept even when the reader ignored it as dominated BEFORE
    # any fork evidence existed; fails_when=only entries classified after the evidence
    # are retained; why_new=ignore never preserved bytes; seam=selective delivery;
    # no_claim=none beyond protects.
    f = store_fleet
    c = join_extra(f, "c")
    a, b = f.devices["a"], f.devices["b"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])  # Alpha, family F1.
    f.action("a", "push", attended=False)
    deliver_where(f, c, lambda path: "/log/" in path and a.mm_id in path)
    f.action("c", "pull")
    c.edit("notes.txt", CONTENTS[5])  # Derived from alpha: {a:1, c:1}.
    f.action("c", "push", attended=False)
    deliver_where(f, b, lambda path: "/log/" in path and c.mm_id in path)
    assert f.action("b", "pull").status == "applied"  # b holds the dominating version.
    f.restore(a, backup, index_policy="saved")
    a.edit("notes.txt", CONTENTS[2])  # Bravo reuses counter {a:1}.
    f.action("a", "push", attended=False)
    bravo = [
        ch.path
        for ch in f.transport.pending()
        if ch.dst == "b" and ch.src == "a" and "/log/" in ch.path and not ch.reasons
    ]
    assert len(bravo) == 1  # Only bravo's file is a's to deliver: alpha reaches b via c.
    f.transport.deliver(
        next(ch for ch in f.transport.pending() if ch.path == bravo[0] and ch.dst == "b")
    )
    result = f.action("b", "pull")  # Bravo is dominated by the version b already holds.
    assert not [w for w in result.warnings if a.mm_id in w]
    assert not [v for v in FileState(b.state_root).read().displaced if v.get("cause") == "fork"]
    assert "fork" not in FileState(b.state_root).read().cursors.get(a.mm_id, {})
    f.drain()  # Alpha finally arrives: a second complete family, so now there is evidence.
    result = f.action("b", "pull")
    assert "fork-evidence" in result.events
    state = FileState(b.state_root).read()
    kept = [v for v in state.displaced if v.get("cause") == "fork"]
    # Bravo is kept, and so is alpha: the honest ancestor the dominating version was
    # derived from was also ignored at or after the fork seq, which the rule retains.
    assert sorted((v["peer"], v["content"], v["reason"]) for v in kept) == sorted(
        [(a.mm_id, CONTENTS[1], "ignored-fork"), (a.mm_id, CONTENTS[2], "ignored-fork")]
    )
    assert any("2 displaced versions kept until" in w for w in result.warnings)
    # Retention happens once however many pulls follow.
    f.action("b", "pull")
    assert (
        len([v for v in FileState(b.state_root).read().displaced if v.get("cause") == "fork"]) == 2
    )
    f.oracle.check(f)
    settle(f, min_participants=2)


def test_post_restore_edit_survives_when_the_dominating_version_arrives_second(store_fleet):
    # Value: protects=the other arrival order loses no version either; fails_when=bravo
    # is applied then silently overwritten; why_new=ignore never preserved bytes;
    # seam=none; no_claim=none beyond protects.
    f = store_fleet
    a, b, c, bravo = derived_edit_then_restore(f, dominating_first=False)
    for name in ("b", "c"):
        f.action(name, "pull")
    f.oracle.check(f)
    settle(f, min_participants=2)
    # Bravo is current, displaced or superseded on a participant or the restored device.
    held = [(d.source_root / "notes.txt").read_bytes() for d in f.ordered()]
    held += [v["content"] for d in f.ordered() for v in FileState(d.state_root).read().displaced]
    assert any(bravo == data or (data and bravo in data) for data in held)


def deliver_where(f, device, predicate):
    """Deliver every ready transport choice for ``device`` that ``predicate`` accepts."""
    delivered = 0
    for choice in f.transport.pending():
        if choice.dst == device.name and not choice.reasons and predicate(choice.path):
            f.transport.deliver(choice)
            delivered += 1
    return delivered


def test_state_restore_replays_own_records_and_converges(store_fleet):
    # Value: protects=a rolled-back state with an intact replica recovers through row 1
    # with no fork and no rejoin; fails_when=the restored device republishes seq 1 or is
    # told to rejoin; why_new=restore scopes are new; seam=none; no_claim=native Syncthing.
    f = store_fleet
    a = f.devices["a"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    old_id = a.mm_id
    f.restore(a, backup, scope="state")
    assert not FileState(a.state_root).read().own_log  # State is behind, replica is not.
    assert not (a.source_root / "notes.txt").exists()
    result = f.action("a", "push", attended=False)
    assert result.status == "unchanged"
    assert any(r.get("operation") == "own-replay" for r in f.last_observation.history)
    state = FileState(a.state_root).read()
    assert "1" in state.own_log and not state.meta.get("refused")
    assert (a.source_root / "notes.txt").read_bytes() == CONTENTS[1]
    settle(f, min_participants=2)
    assert a.mm_id == old_id
    assert not any(r.get("operation") == "rejoin" for r in FileState(a.state_root).read().history)


@pytest.mark.parametrize("backup_age_days", [0, 31])
def test_fresh_replica_restore_waits_for_syncthing_and_never_advises_rejoin(
    store_fleet, backup_age_days
):
    # Value: protects=row 2 waits for sync, not for age; fails_when=the device is told to
    # rejoin while peers refill it, or an old backup is mistaken for a retirement;
    # why_new=row 2 is keyed on sync state; seam=none; no_claim=Syncthing need-count
    # accuracy (modelled as pending deliveries to this device).
    from mind_meld.store import layout

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)  # own_log {1}
    backup = f.snapshot(a)
    a.edit("other.txt", CONTENTS[2])
    f.action("a", "push", attended=False)  # push #2 exists only after the backup
    f.drain()
    f.action("b", "pull")
    f.advance(backup_age_days * DAY)
    old_id = a.mm_id
    f.restore(a, backup, scope="fresh-replica")
    assert not list(a.replica_root.iterdir())
    for operation in ("push", "pull"):
        result = f.action("a", operation)
        assert (result.status, result.reason) == ("pending", "fleet-header")
    deliver_where(f, a, lambda path: path == "fleet.json")
    for operation in ("push", "pull"):
        result = f.action("a", operation)
        assert (result.status, result.reason) == ("pending", "own-records-in-flight")
        assert result.warnings == (owner_text("own-records-in-flight-folder"),)
    # a's own presence is back and says push #2 exists: wait for its records.
    deliver_where(f, a, lambda path: path == f"dev/{old_id}/presence.enc")
    a.edit("notes.txt", CONTENTS[3])  # An edit made meanwhile must not be published yet.
    result = f.action("a", "push", attended=False)
    assert (result.status, result.reason, result.gap_seq) == (
        "pending",
        "own-records-in-flight",
        2,
    )
    assert result.warnings == (owner_text("own-records-in-flight", claimed=2),)
    own_files = [
        p
        for p in a.replica_root.rglob("*.enc")
        if layout.parse_record_key(p.relative_to(a.replica_root).as_posix()).kind == "commit"
        and f"dev/{old_id}/" in p.as_posix()
    ]
    assert not own_files  # Nothing was published while records were in flight.
    f.drain()
    result = f.action("a", "push", attended=False)
    assert result.status == "published" and result.seq == 3
    assert a.mm_id == old_id
    assert not FileState(a.state_root).read().meta.get("refused")
    settle(f, min_participants=2)
    assert (b.source_root / "notes.txt").read_bytes() == CONTENTS[3]
    assert (a.source_root / "other.txt").read_bytes() == CONTENTS[2]


def test_attended_push_skips_ahead_when_own_records_never_return(store_fleet):
    # Value: protects=a restored device whose records are gone for good still converges
    # the fleet; fails_when=it waits forever or readers stay stuck on the missing seqs;
    # why_new=restored-device detection is new; seam=none; no_claim=none beyond protects.
    f = store_fleet
    c = join_extra(f)
    a = f.devices["a"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)  # Push #1; c never applies it.
    f.drain()
    f.action("b", "pull")
    # A current-policy restore deletes push #1's files fleet-wide; receipts survive.
    f.restore(a, backup, index_policy="current")
    f.drain()
    a.edit("notes.txt", CONTENTS[1])  # The user redoes the lost work ...
    a.edit("other.txt", CONTENTS[2])  # ... and adds more.
    pending = f.action("a", "push", attended=False)
    assert (pending.status, pending.reason, pending.gap_seq) == (
        "pending",
        "own-records-in-flight",
        1,
    )
    assert pending.warnings == (owner_text("own-records-in-flight", claimed=1),)
    pulled = f.action("a", "pull")  # Pull applies peers and own replay, never presence.
    assert (pulled.status, pulled.reason) == ("pending", "own-records-in-flight")
    f.drain()
    assert f.action("a", "push", attended=False).status == "pending"  # Autopush waits.
    result = f.action("a", "push")  # Synced and still incomplete: skip ahead.
    assert (result.status, result.seq, result.events) == ("published", 2, ("skip-ahead",))
    from .invariants import context_for

    ctx = context_for(f, a)
    assert [r.seq for r in ctx.view().records(a.mm_id, "snapshot")][-1] == 2  # Forced snapshot.
    f.drain()
    result = f.action("c", "pull")
    assert "snapshot-fallback" in result.events  # c waits on seq 1; the snapshot covers it.
    result = f.action("b", "pull")
    assert any("does not follow the record applied before it" in w for w in result.warnings)
    settle(f, min_participants=3)
    assert (c.source_root / "other.txt").read_bytes() == CONTENTS[2]


@pytest.mark.parametrize("scope", ["state", "fresh-replica"])
def test_restore_older_than_the_compaction_floor_recovers_from_the_own_snapshot(store_fleet, scope):
    # Value: protects=own-snapshot recovery; fails_when=a path that survives only in the
    # own snapshot is lost after a restore from a backup older than the floor;
    # why_new=reconcile only bumped snapshot_seq; seam=none; no_claim=none beyond protects.
    from mind_meld.store import layout

    f = store_fleet
    join_extra(f)
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    backup = f.snapshot(a)  # Taken at push #1.
    a.edit("other.txt", CONTENTS[2])
    f.action("a", "push", attended=False)  # Push #2.
    f.action("a", "publish_snapshot")
    a.edit("notes.txt", CONTENTS[3])
    f.action("a", "publish_snapshot")
    f.advance(15 * DAY)
    f.action("a", "compact")
    f.drain()
    own = a.replica_root / "dev" / a.mm_id
    commits = {
        layout.parse_record_key(p.relative_to(a.replica_root).as_posix()).seq
        for p in own.glob("log/*.enc")
    }
    assert 2 not in commits  # other.txt now survives only in the own snapshot.
    before = {p.name for p in b.replica_root.glob("dev/*/snap/*.enc")}
    f.restore(a, backup, scope=scope)
    f.drain()
    assert not (a.source_root / "other.txt").exists()
    result = f.action("a", "pull")
    assert result.status in ("applied", "pending")
    f.drain()
    f.action("a", "pull")
    ops = [r.get("operation") for r in FileState(a.state_root).read().history]
    assert "own-snapshot-recovery" in ops
    assert (a.source_root / "other.txt").read_bytes() == CONTENTS[2]
    assert (a.source_root / "notes.txt").read_bytes() == CONTENTS[3]
    anchored = FileState(a.state_root).read().own_log
    assert max(map(int, anchored)) >= 3
    assert {p.name for p in b.replica_root.glob("dev/*/snap/*.enc")} == before
    settle(f, min_participants=3)


def test_compaction_deletes_only_families_it_can_prove(store_fleet):
    # Value: protects=a family this device cannot prove it wrote survives compaction at
    # any seq; fails_when=compaction deletes by seq and takes the last copy of an unknown
    # family; why_new=the compactor used to delete every own name at or below the floor;
    # seam=authentic squatter parts; no_claim=none beyond protects.
    from mind_meld.store import layout

    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    for data in (CONTENTS[2], CONTENTS[3]):
        a.edit("notes.txt", data)
        f.action("a", "publish_snapshot")
    proven = next(a.replica_root.glob(f"dev/{a.mm_id}/log/000000000001.*.enc"))
    unknown_copy = proven.with_name("000000000001.0badf00d.enc")  # Same record, other name.
    unknown_copy.write_bytes(proven.read_bytes())
    (key,) = [
        k for k in (layout.record_key(a.mm_id, "commit", 1, part=1, nparts=3, family="feedface"),)
    ]
    unknown_part = a.replica_root / key
    unknown_part.write_bytes(squatter_parts(f, a, 1, 3)[0])
    f.advance(15 * DAY)
    assert f.action("a", "compact").status == "compacted"
    assert not proven.exists()  # The family own_log names went.
    assert unknown_copy.exists() and unknown_part.exists()  # Unproven families stay.
    settle(f, min_participants=2)


def test_saved_restore_partial_redelivery_with_offline_holders_loses_nothing(store_fleet):
    # Value: protects=the original edit survives a restore that has only a partial
    # redelivery, a compaction and holders of the complete family that are offline;
    # fails_when=this device deletes, supersedes or completes the incomplete family it
    # cannot prove it wrote; why_new=intent rows and proven-only compaction are new;
    # seam=selective delivery plus offline peers; no_claim=native redelivery order.
    from mind_meld.store import layout

    f = store_fleet
    c = join_extra(f)
    a, b = f.devices["a"], f.devices["b"]
    backup = f.snapshot(a)
    multipart_edit(a)  # Alpha: a 3-part commit, held complete by b and c.
    f.action("a", "push", attended=False)
    f.drain()
    f.restore(a, backup, index_policy="saved")
    deliver_where(f, a, lambda path: path.endswith("1of3.enc"))  # Only part 1 returns.
    for device in (b, c):
        f.transport.set_online(device.name, False)
    held = {k: v for k, v in own_log_parts(a).items()}
    assert len(held) == 1
    a.edit("notes.txt", CONTENTS[2])
    result = f.action("a", "push", attended=False)
    assert (result.status, result.reason) == ("pending", "own-records-in-flight")
    f.advance(15 * DAY)
    f.action("a", "compact")
    assert own_log_parts(a) == held  # The family it cannot prove it wrote is untouched.
    assert {layout.parse_record_key(k).family for k in held} == {
        layout.parse_record_key(k).family for k in own_log_parts(a)
    }
    settle(f, min_participants=3)  # Everyone returns; the records complete; a replays.
    assert (a.source_root / "other.txt").read_bytes() == b"o" * 450  # Alpha's edit survived.
    assert not FileState(a.state_root).read().meta.get("refused")


def test_detection_reads_a_peers_snapshot_cursors(store_fleet):
    # Value: protects=a peer snapshot's cursors[me] counts as a claim; fails_when=only
    # presence is consulted, so a device whose own and peers' presence are missing
    # publishes into records that are still arriving; why_new=detection is new;
    # seam=selective delivery after a fresh-replica restore; no_claim=none beyond protects.
    from .invariants import context_for
    from .model.store import claimed_seq

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    f.action("b", "pull")
    b.edit("other.txt", CONTENTS[2])
    f.action("b", "publish_snapshot")  # Its cursors[a] = 1.
    f.drain()
    f.restore(a, backup, scope="fresh-replica")
    deliver_where(f, a, lambda path: path == "fleet.json")
    deliver_where(f, a, lambda path: f"dev/{b.mm_id}/snap/" in path)  # No presence at all.
    ctx = context_for(f, a)
    view = ctx.view()
    assert not any(path.endswith("presence.enc") for path in view.names)
    assert claimed_seq(ctx, view, 0) == 1
    result = f.action("a", "push", attended=False)
    assert (result.status, result.reason, result.gap_seq) == ("pending", "own-records-in-flight", 1)
    f.drain()
    settle(f, min_participants=2)
    assert (a.source_root / "notes.txt").read_bytes() == CONTENTS[1]


def test_lost_own_records_are_healed_by_the_next_attended_push(store_fleet):
    # Value: protects=readers stuck on a gap recover; fails_when=an owner whose records
    # vanished from storage stays silent or never publishes the snapshot readers fall
    # back to; why_new=heal is new; seam=a deletion as a restore elsewhere would cause;
    # no_claim=native deletion semantics.
    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    f.action("b", "pull")
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)  # Push #2.
    for path in list(a.replica_root.glob(f"dev/{a.mm_id}/log/000000000002.*.enc")):
        path.unlink()  # Another machine's rollback deleted push #2 fleet-wide.
    f.transport.scan("a")
    f.drain()
    stuck = f.action("b", "pull")
    assert stuck.status == "pending" and stuck.gap_seq == 2
    lost = owner_text("own-records-lost")
    for operation in ("pull", "push"):
        result = f.action("a", operation, **({"attended": False} if operation == "push" else {}))
        assert lost in result.warnings  # Always visible until healed.
    result = f.action("a", "push")  # Attended.
    assert (result.status, result.events) == ("published", ("heal-snapshot",))
    assert lost not in result.warnings or result.warnings.count(lost) == 1
    healed = f.action("a", "pull")
    assert lost not in healed.warnings
    f.drain()
    assert "snapshot-fallback" in f.action("b", "pull").events
    settle(f, min_participants=2)


def test_lost_own_snapshot_is_healed_by_the_next_attended_push(store_fleet):
    # Value: protects=heal also fires when the own snapshot at meta.snapshot_seq
    # vanishes, not only when a commit above it does; fails_when=the heal trigger
    # drops its snapshot check and the fleet silently loses the bootstrap/fallback
    # snapshot; why_new=the heal witness above deletes a commit, which trips a
    # different clause of the same trigger; seam=deleting the snapshot files from
    # every replica, as a rollback elsewhere would; no_claim=native deletion
    # semantics.
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    assert f.action("a", "publish_snapshot").status == "published"  # snapshot_seq 1.
    f.drain()
    f.action("b", "pull")

    def snapshot_parts(device):
        return list(device.replica_root.glob(f"dev/{a.mm_id}/snap/000000000001.*.enc"))

    assert snapshot_parts(a) and snapshot_parts(b)
    for device in (a, b):
        for path in snapshot_parts(device):
            path.unlink()  # Another machine's rollback deleted the snapshot fleet-wide.
        f.transport.scan(device.name)
    f.drain()
    assert not snapshot_parts(a)
    lost = owner_text("own-records-lost")
    assert lost in f.action("a", "pull").warnings
    result = f.action("a", "push")  # Attended, with no new entries to publish.
    assert (result.status, result.events) == ("published", ("heal-snapshot",))
    assert snapshot_parts(a)
    assert lost not in f.action("a", "pull").warnings
    settle(f, min_participants=2)


def test_partial_redelivery_below_a_complete_slot_is_not_superseded(store_fleet):
    # Value: protects=no write at a lower slot while records above it are complete;
    # fails_when=the publisher writes at the torn slot and forks; why_new=intent rows are
    # new; seam=saved restore plus selective redelivery; no_claim=native delivery order.
    from mind_meld.store import layout

    f = store_fleet
    c = join_extra(f)
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)  # Push #1.
    f.drain()
    for name in ("b", "c"):
        f.action(name, "pull")
    backup = f.snapshot(a)  # own_log {1}.
    multipart_edit(a)
    f.action("a", "push", attended=False)  # Push #2: a 3-part commit.
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)  # Push #3.
    f.drain()
    f.action("b", "pull")
    f.restore(a, backup, index_policy="saved")
    two = [
        ch.path
        for ch in f.transport.pending()
        if ch.dst == "a" and "/log/000000000002." in ch.path and not ch.reasons
    ]
    assert len(two) == 3
    deliver_where(f, a, lambda path: path == two[0])  # Only part 1 of push #2 ...
    deliver_where(f, a, lambda path: "/log/000000000003." in path)  # ... but all of #3.
    before = {k for k in map(str, a.replica_root.rglob("*.enc"))}
    # Replay is contiguous: it stops at the hole, never jumps it to #3, and detection
    # owns the wait. No push writes at #2 and no heal snapshot is taken over a record
    # this device's ledger has not incorporated.
    result = f.action("a", "push", attended=False)
    assert (result.status, result.reason) == ("pending", "own-records-in-flight")
    state = FileState(a.state_root).read()
    assert sorted(state.own_log) == ["1"]  # Push #2 is a hole, never written at.
    own_two = {
        layout.parse_record_key(p.relative_to(a.replica_root).as_posix()).family
        for p in a.replica_root.glob(f"dev/{a.mm_id}/log/000000000002.*.enc")
    }
    assert len(own_two) == 1 and {str(p) for p in a.replica_root.rglob("*.enc")} >= before
    assert f.action("a", "push").status == "pending"  # Attended too, while parts fly.
    f.drain()  # The rest of #2 arrives; replay now runs through #3.
    f.action("a", "pull")
    assert sorted(FileState(a.state_root).read().own_log) == ["1", "2", "3"]
    settle(f, min_participants=3)
    assert (c.source_root / "other.txt").read_bytes() == b"o" * 450


def test_reader_with_a_partial_copy_is_not_covered_past_the_hole(store_fleet):
    # Value: protects=a reader holding only part of a record still applies it once it
    # completes, even after the owner's restore left a hole below a complete slot;
    # fails_when=a heal snapshot taken over the hole marks the record covered through
    # its prev chain and the reader skips it forever (INV1); why_new=heal and the
    # cover rule are new and interact; seam=saved restore plus selective redelivery;
    # no_claim=native delivery order.
    f = store_fleet
    c = join_extra(f)
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)  # Push #1.
    f.drain()
    for name in ("b", "c"):
        f.action(name, "pull")
    backup = f.snapshot(a)
    multipart_edit(a)
    f.action("a", "push", attended=False)  # Push #2: a 3-part commit.
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)  # Push #3.
    deliver_where(f, b, lambda path: True)
    f.action("b", "pull")
    two = [
        ch.path
        for ch in f.transport.pending()
        if ch.dst == "c" and "/log/000000000002." in ch.path and not ch.reasons
    ]
    assert len(two) == 3
    deliver_where(f, c, lambda path: path == two[0])  # c holds only part 1 of #2 ...
    deliver_where(f, c, lambda path: "/log/000000000003." in path)  # ... but all of #3.
    f.action("c", "pull")
    f.restore(a, backup, index_policy="saved")
    two_a = [
        ch.path
        for ch in f.transport.pending()
        if ch.dst == "a" and "/log/000000000002." in ch.path and not ch.reasons
    ]
    assert len(two_a) == 3
    deliver_where(f, a, lambda path: path == two_a[0])
    deliver_where(f, a, lambda path: "/log/000000000003." in path)
    result = f.action("a", "push")  # Attended: no heal snapshot over the hole.
    assert (result.status, result.reason, result.events) == (
        "pending",
        "own-records-in-flight",
        (),
    )
    f.drain()  # The rest of #2 reaches both a and c.
    f.action("c", "pull")
    settle(f, min_participants=3)
    assert (c.source_root / "other.txt").read_bytes() == b"o" * 450


def test_intent_row_restored_from_a_backup_does_not_prove_a_torn_slot(store_fleet):
    # Value: protects=a backup taken after a torn crash, restored after the slot was
    # later closed, does not license superseding it before the closing family is
    # redelivered; fails_when=the restored intent row alone proves torn and the push
    # writes a second family at the seq (a self-fork on redelivery); why_new=intent
    # rows live in the state backups copy; seam=saved restore; no_claim=none.
    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)  # Push #1.
    f.drain()
    f.action("b", "pull")
    multipart_edit(a)
    f.action("a", "push", crash_parts=1, attended=False)  # Torn #2 plus its intent row.
    backup = f.snapshot(a)  # The backup carries the row and the torn part.
    f.action("a", "push", attended=False)  # A fresh family closes #2.
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)  # Push #3.
    f.drain()
    f.action("b", "pull")
    f.restore(a, backup, index_policy="saved")
    result = f.action("a", "push", attended=False)
    assert (result.status, result.reason) == ("pending", "own-records-in-flight")
    f.drain()  # The closing family returns; replay runs through #3, no fork.
    assert f.action("a", "pull").status != "refused"
    settle(f, min_participants=2)


def test_snapshot_only_clone_refuses_as_a_clone(store_fleet):
    # Value: protects=a clone whose only publication under this id is a snapshot is
    # still a clone; fails_when=own-snapshot recovery adopts the foreign snapshot as
    # this device's anchor instead of refusing fork-clone; why_new=own-snapshot
    # recovery is new; seam=cloned disk on another host; no_claim=none.
    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push")  # Attended: a snapshot exists under a's host.
    f.drain()
    clone = f.clone(a, "c")
    # Strip the guards that would refuse first (presence and commits), leaving only
    # the foreign snapshot: recovery must still refuse, never adopt it as an anchor.
    for path in list(clone.replica_root.rglob("*.enc")):
        if "/snap/" not in path.as_posix():
            path.unlink()
    result = f.action("c", "pull")
    assert (result.status, result.reason) == ("refused", "fork-clone")


def test_fork_below_a_recovery_snapshot_still_refuses(store_fleet):
    # Value: protects=own-snapshot recovery never launders a same-host fork below the
    # snapshot; fails_when=recovery loads the snapshot and skips the forked seq's
    # digest check, changing files instead of refusing fork-restore; why_new=recovery
    # is new and runs before replay; seam=saved restore plus selective redelivery;
    # no_claim=native delivery order.
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])  # alpha at push #1
    f.action("a", "push", attended=False)
    a.edit("notes.txt", CONTENTS[3])
    f.action("a", "push", attended=False)  # Push #2 on the alpha branch.
    f.action("a", "publish_snapshot")  # Snapshot at seq 2, alpha branch.
    deliver_where(f, b, lambda path: True)  # b alone holds the alpha branch + snapshot.
    f.restore(a, backup, index_policy="saved")
    a.edit("notes.txt", CONTENTS[2])  # bravo reuses push #1
    f.action("a", "push", attended=False)
    f.drain()  # Alpha family and its snapshot redeliver to a.
    result = f.action("a", "pull")
    assert (result.status, result.reason) == ("refused", "fork-restore")


def test_skip_ahead_advances_past_a_complete_unreplayed_slot(store_fleet):
    # Value: protects=skip-ahead is not wedged when the claim lagged storage and a
    # complete own record already occupies claimed + 1 behind a permanent hole;
    # fails_when=the push pends forever at the occupied slot because no receipt can
    # grow the claim past a record no peer applied; why_new=skip slot inspection is
    # new; seam=state restore plus fleet-wide record deletion; no_claim=none.
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)  # Push #1.
    f.drain()
    f.action("b", "pull")
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[2])
    f.action("a", "push", attended=False)  # Push #2.
    deliver_where(f, b, lambda path: "/log/000000000002." in path)
    f.action("b", "pull")  # b's receipt claims push #2.
    a.edit("notes.txt", CONTENTS[3])
    f.action("a", "push", crash_state=True, attended=False)  # Push #3 complete, unrecorded.
    for device in (a, b):  # Push #2 is then lost fleet-wide (a rollback elsewhere).
        for path in list(device.replica_root.glob(f"dev/{a.mm_id}/log/000000000002.*.enc")):
            path.unlink()
        f.transport.scan(device.name)
    f.restore(a, backup, scope="state")  # own_log back to push #1; replica keeps #3.
    f.drain()
    result = f.action("a", "push")  # Attended: skip past the occupied slot.
    assert result.status == "published" and result.seq == 4, result
    assert "skip-ahead" in result.events
    settle(f, min_participants=2)


def test_torn_snapshot_is_closed_by_the_next_attended_push(store_fleet):
    # Value: protects=pull's "the next push completes it" is true for a torn snapshot,
    # not only a torn commit; fails_when=an attended push with nothing to publish
    # returns unchanged and the torn-own-slot warning repeats forever; why_new=torn
    # snapshot detection postdates the commit-slot closure; seam=none; no_claim=none.
    f = store_fleet
    a = f.devices["a"]
    multipart_edit(a)
    f.action("a", "push", attended=False)
    f.action("a", "publish_snapshot", crash_parts=1)  # Torn multipart snapshot.
    f.drain()
    assert owner_text("torn-own-slot") in f.action("a", "pull").warnings
    result = f.action("a", "push")  # Attended, nothing new to publish.
    assert (result.status, result.events) == ("published", ("torn-supersede",))
    assert owner_text("torn-own-slot") not in f.action("a", "pull").warnings
    settle(f, min_participants=2)


def test_torn_own_slot_is_reported_until_the_next_push_completes_it(store_fleet):
    # Value: protects=the owner is told a push was interrupted; fails_when=a pull after
    # a crash is silent; why_new=reason codes are new; seam=none; no_claim=none beyond
    # protects.
    f = store_fleet
    a = f.devices["a"]
    multipart_edit(a)
    assert f.action("a", "push", crash_parts=1, attended=False).status == "crashed"
    result = f.action("a", "pull")
    assert (result.status, result.reason, result.peer, result.gap_seq) == (
        "pending",
        "torn-own-slot",
        a.mm_id,
        1,
    )
    assert owner_text("torn-own-slot") in result.warnings
    assert f.action("a", "push").status == "published"
    assert f.action("a", "pull").status == "applied"
    settle(f, min_participants=2)


DESIGN = Path(__file__).parents[2] / "docs" / "designs" / "store-v2.md"


def design_section(number):
    """The text of one store-v2 section, whitespace-collapsed, so wrapped lines compare."""
    match = re.search(rf"^## {number}\. .*?(?=^## \d+\. |\Z)", DESIGN.read_text(), re.S | re.M)
    return re.sub(r"\s+", " ", match[0].replace("**", ""))


@pytest.mark.parametrize("code", sorted(model_store.OWNER_TEXTS))
def test_owner_texts_are_verbatim_in_the_design_section(code):
    # Value: protects=the model and section 9 say the same words; fails_when=a text or a
    # reason code drifts from the design the real publisher implements; why_new=every
    # section 9 row carries verbatim text and a reason code; seam=the design doc itself.
    text = model_store.OWNER_TEXTS[code].replace("{claimed}", "<N>").replace("{date}", "<date>")
    section = design_section(9)
    assert text in section, f"{code}: {text!r} is not in store-v2 section 9"
    assert f"`{code.removesuffix('-folder')}`" in section
    headings = [
        re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
        for h in re.findall(r"^## (.+)$", DESIGN.read_text(), re.M)
    ]
    assert model_store.DOCS_ANCHOR.split("#", 1)[1] in headings
    shown = model_store.owner_text(code, claimed=3, date="2026-10-16")
    assert shown.endswith(f" See {model_store.DOCS_ANCHOR}.") and shown.startswith(text[:20])


@pytest.mark.parametrize(
    "fork",
    [
        {"kind": "conflict-copy", "seq": 7},
        {"kind": "republished", "seq": 7},
        {"kind": "prev", "seq": 7},
        {"kind": "family", "seq": 7, "clone": False},
        {"kind": "family", "seq": 7, "clone": True},
    ],
)
def test_reader_fork_texts_are_verbatim_in_the_design_section(fork):
    text = re.sub(r"\b7\b", "<seq>", model_store.fork_text("<peer>", fork, []))
    section = design_section(6)
    assert text in section, f"{text!r} is not in store-v2 section 6"
    live = [{"time": 0}]
    suffix = model_store.fork_text("<peer>", fork, live)[
        len(model_store.fork_text("<peer>", fork, [])) :
    ]
    assert "displaced version kept until 1970-01-31; see `mm displaced` before then" in suffix
    assert "displaced versions kept until <date>; see `mm displaced` before then" in section


def restore_current_residual(f, case):
    """Setup for the accepted current-policy residuals; nothing here may fail."""
    join_extra(f, "c")
    if case != "pre-header-backup":
        join_extra(f, "d")
    a = f.devices["a"]
    if case == "unapplied-deleted":
        backup = f.snapshot(a)
        a.edit("notes.txt", CONTENTS[1])
        f.action("a", "push", attended=False)
        f.drain()  # The record reaches every peer, but nobody has applied it.
        f.restore(a, backup, index_policy="current")
        f.drain()  # The restore's deletions of the record files propagate fleet-wide.
    elif case == "pre-header-backup":
        late = f.add_device("d")
        backup = f.snapshot(late)  # Taken before fleet.json arrived.
        f.drain()
        f.restore(late, backup, index_policy="current")
        f.drain()
    else:
        backup = f.snapshot(a)
        a.edit("notes.txt", CONTENTS[1])
        f.action("a", "push", attended=False)
        for choice in f.transport.pending():
            if choice.dst == "b" and "/log/" in choice.path:
                f.transport.deliver(choice)
        f.action("b", "pull")  # b applies the version, then the record is deleted.
        f.restore(a, backup, index_policy="current")
        f.drain()


@pytest.mark.xfail(
    strict=True,
    raises=ExpectedDesignGap,
    reason="restore-current: unsupported setup, residuals pinned (73C gate)",
)
@pytest.mark.parametrize(
    "case,invariant",
    [
        # (1) A record no device had applied is deleted fleet-wide by the restore.
        ("unapplied-deleted", 2),
        # (3) A backup from before fleet.json arrived deletes it fleet-wide; every
        # device waits, which INV4 refuses as an outcome of a pull.
        ("pre-header-backup", 4),
        # (5) Applied versions whose records the rollback deleted stay divergent
        # unless the restored device republishes over them.
        ("applied-not-republished", 1),
    ],
)
def test_gap_restore_current_residual(store_fleet, case, invariant):
    # Value: protects=the accepted residuals stay visible; fails_when=a residual
    # silently starts passing or a different invariant fires first; why_new=the
    # restore-current row is re-scoped to what 73C accepted as an unsupported setup;
    # seam=none; no_claim=native Syncthing restore behaviour.
    f = store_fleet
    restore_current_residual(f, case)
    with expected_gap(invariant):
        f.oracle.check(f)
        settle(f, min_participants=2)


def test_regression_restore_discards_an_unpublished_deletion(
    tmp_path, sim_write_guard, monkeypatch
):
    # Deep-search counterexample, minimized: a restore rolled back a local deletion that
    # no record carried, and INV7 still demanded the fleet converge on that deletion.
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        state.lifecycle_witness(case=0)
        state.backup_rule(device=3)
        state.torn_rule(device=3)
        state.settle_rule()
        state.delete(device=3, path=2)
        state.restore_rule(device=3, policy="saved")
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


@pytest.mark.xfail(
    strict=True,
    raises=ExpectedDesignGap,
    reason="fresh-replica-unreplicated: emptying destroys the only copy (73C review)",
)
def test_gap_fresh_replica_unreplicated_own_record(tmp_path, sim_write_guard, monkeypatch):
    # KNOWN_DESIGN_GAPS[fresh-replica-unreplicated]: deep-search counterexample,
    # minimized. A crash completed a record only on its author's replica; a
    # fresh-replica restore then emptied that replica before any peer synced it, so
    # the version exists nowhere and INV7 demands it forever. Random search gates
    # this restore (see restore_rule); the direct restore below keeps it pinned.
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        with expected_gap(7):
            state.lifecycle_witness(case=0)
            state.backup_rule(device=0)
            state.crash_rule(device=0, parts=1, snapshot=False)
            device = state.device(0)
            state.fleet.restore(device, state.backups[device.name], scope="fresh-replica")
            state.teardown()
    finally:
        ACTIVE.reset(token)


def test_regression_unproven_torn_slot_after_state_restore_still_settles(
    tmp_path, sim_write_guard, monkeypatch
):
    # Deep-search counterexample, minimized: a torn crash's closure waited for sync,
    # a state restore to a pre-crash backup then dropped the intent row, and the
    # unproven incomplete family wedged publishing forever (INV5). The attended
    # synced-and-unclaimed supersede is the escape.
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        state.lifecycle_witness(case=0)
        state.backup_rule(device=0)
        state.crash_rule(device=2, parts=0, snapshot=True)
        state.torn_rule(device=3)
        state.restore_rule(device=0, policy="state")
        state.delete(device=2, path=1)
        state.crash_rule(device=3, parts=1, snapshot=False)
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


def test_regression_presence_refresh_does_not_starve_torn_closure(
    tmp_path, sim_write_guard, monkeypatch
):
    # Deep-search counterexample, minimized: settle's large clock advances made a
    # peer rewrite its presence every round, so the folder never read synced and a
    # torn slot never closed (INV5). Presence arrivals cannot complete a record and
    # do not hold the sync gate.
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        state.lifecycle_witness(case=0)
        state.crash_rule(device=1, parts=0, snapshot=True)
        state.torn_rule(device=2)
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


def test_regression_fork_at_a_recovery_snapshot_anchor_refuses(
    tmp_path, sim_write_guard, monkeypatch
):
    # Deep-search counterexample, minimized: own-snapshot recovery anchored own_log at
    # the snapshot's prev without comparing it to the complete family actually at
    # that seq (from the republished branch), so the forked device published presence
    # instead of refusing fork-restore (INV6).
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        state.lifecycle_witness(case=0)
        state.crash_rule(device=1, parts=0, snapshot=True)
        state.backup_rule(device=0)
        state.torn_rule(device=2)
        state.settle_rule()
        state.join_rule(choice=0)
        state.time_advance(seconds=2592000)
        state.retire_rule(device=2)
        state.restore_rule(device=0, policy="saved")
        state.crash_rule(device=0, parts=2, snapshot=False)
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


def test_regression_restore_discards_an_unpublished_union_edit(
    tmp_path, sim_write_guard, monkeypatch
):
    # Deep-search counterexample, minimized: a state restore rolled back an unpublished
    # edit of a union path. INV2 counts it preserved (its lines are held elsewhere), yet
    # INV7 demanded the exact bytes of an edit that exists nowhere.
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        state.core_witness(case=3)
        state.lifecycle_witness(case=0)
        state.join_rule(choice=3)
        state.settle_rule()
        state.backup_rule(device=3)
        state.join_rule(choice=2)
        state.edit(content=4, device=3, old_mtime=False, path=2)
        state.restore_rule(device=3, policy="state")
        state.join_rule(choice=3)
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


@pytest.mark.parametrize("scope", ["state", "saved"])
def test_restore_discards_an_unpublished_edit_of_a_union_path(store_fleet, scope):
    # Value: protects=INV7 does not demand the exact bytes of an edit a restore undid
    # before anyone saw it, even when INV2 counts its lines held elsewhere; fails_when=
    # line order alone fails convergence; why_new=random restores of unpublished edits
    # are now reachable; seam=none; no_claim=INV2's restore exclusion, unchanged.
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("MEMORY.md", CONTENTS[3])  # alpha, bravo
    settle(f, min_participants=2)
    backup = f.snapshot(b)
    b.edit("MEMORY.md", CONTENTS[4])  # bravo, alpha: the same lines, another order
    if scope == "state":
        f.restore(b, backup, scope="state")
    else:
        f.restore(b, backup, index_policy="saved")
    settle(f, min_participants=2)
    assert {(d.source_root / "MEMORY.md").read_bytes() for d in f.participants()} == {CONTENTS[3]}


@pytest.mark.parametrize("published", [False, True])
def test_restore_keeps_only_published_deletions_as_convergence_targets(store_fleet, published):
    # Value: protects=the oracle drops a deletion a restore undid before anyone saw it, and
    # keeps one that was published; fails_when=INV7 demands a deletion that exists nowhere,
    # or forgets one the fleet applied; why_new=random saved restores are now reachable;
    # seam=none; no_claim=INV2's restore exclusion, which is unchanged.
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    settle(f, min_participants=2)
    backup = f.snapshot(a)
    a.delete("notes.txt")
    if published:
        f.action("a", "push", attended=False)
        f.drain()
        f.action("b", "pull")
        assert not (b.source_root / "notes.txt").exists()
    deleted = max(e for e, v in f.oracle.versions.items() if v.data is None)
    f.restore(a, backup, index_policy="saved")
    assert deleted not in f.oracle.excluded  # INV2's restore exclusion is unchanged.
    settle(f, min_participants=2)
    expected = None if published else CONTENTS[1]
    for d in f.participants():
        path = d.source_root / "notes.txt"
        assert (path.read_bytes() if path.exists() else None) == expected


@pytest.mark.parametrize(
    ("delivered", "policy"), [(False, "saved"), (True, "saved"), (False, "current")]
)
def test_restore_drops_a_union_line_deletion_only_when_no_peer_received_it(
    store_fleet, delivered, policy
):
    # Value: protects=INV7 stops demanding a union version whose only carrier a restore
    # destroyed, keeps demanding one a peer's replica already holds, and never excuses
    # a peer's unpublished edit; fails_when=the restore's loss record judges holders by
    # line-set containment (a superset elsewhere "holds" a version that deleted lines)
    # or drops the restored-device-carried condition; why_new=a deep-search
    # counterexample on events.jsonl; seam=none; no_claim=bytes-without-vector
    # identity (test_regression_union_deletion_lost_to_a_saved_restore pins that).
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    union = CONTENTS[3] + CONTENTS[6]  # alpha, bravo, space
    a.edit("events.jsonl", union)
    settle(f, min_participants=2)
    backup = f.snapshot(a)
    a.edit("events.jsonl", CONTENTS[3])  # Deletes the space line.
    f.action("a", "push", attended=False)
    b.edit("notes.txt", CONTENTS[2])  # A peer's live edit: unpublished, carried by no row.
    peer_edit = max(e for e, v in f.oracle.versions.items() if v.device == "b")
    if delivered:
        for choice in f.transport.pending():
            if choice.dst == "b" and "/log/" in choice.path:
                f.transport.deliver(choice)
    deletion = [
        e
        for e, v in f.oracle.versions.items()
        if v.path == "events.jsonl" and v.data == CONTENTS[3] and v.device == "a"
    ]
    # Under either index policy the restore's scan announces the lost record's deletion.
    f.restore(a, backup, index_policy=policy)
    assert not set(deletion) & f.oracle.excluded  # INV2's union holders still count.
    assert bool(set(deletion) & f.oracle.destroyed) is not delivered
    assert peer_edit not in f.oracle.destroyed  # Only what the restored device carried.
    settle(f, min_participants=2)
    expected = CONTENTS[3] if delivered else union
    for d in f.participants():
        assert (d.source_root / "events.jsonl").read_bytes() == expected


@pytest.mark.parametrize("path", ["events.jsonl", "notes.txt"])
def test_restore_counts_a_family_split_across_peers_as_held(store_fleet, path):
    # Value: protects=a multipart own commit whose parts only peers hold, split between
    # them, is neither excused nor lost after a saved restore: peers reassemble it and
    # converge on it; fails_when=the restore or INV2 judges carriers one replica at a
    # time (a false INV2 or INV7 failure); why_new=review of the restore loss record;
    # seam=none; no_claim=native Syncthing block exchange between peers.
    f = store_fleet
    c = join_extra(f)
    a, b = f.devices["a"], f.devices["b"]
    a.edit(path, CONTENTS[3] + CONTENTS[6] if path == "events.jsonl" else CONTENTS[1])
    settle(f, min_participants=3)
    backup = f.snapshot(a)
    target = CONTENTS[3] if path == "events.jsonl" else b"n" * 450
    for other in ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl"):
        if other != path:
            a.edit(other, other[:1].encode() * 450)  # Forces a multipart commit.
    a.edit(path, target)
    f.action("a", "push", attended=False)
    parts = sorted({ch.path for ch in f.transport.pending() if "/log/" in ch.path})
    assert len(parts) > 1
    deliver_where(f, b, lambda p: p == parts[0])
    deliver_where(f, c, lambda p: p in parts[1:])
    lost = [
        e
        for e, v in f.oracle.versions.items()
        if v.path == path and v.data == target and v.device == "a"
    ]
    f.restore(a, backup, index_policy="saved")
    assert lost and not set(lost) & (f.oracle.excluded | f.oracle.destroyed)
    f.transport.set_online(a.name, False)
    a.edit("other.txt", CONTENTS[2])
    assert f.action("a", "push", attended=False).status == "published"  # A new family.
    f.transport.drain()  # b and c exchange parts while a stays offline.
    for d in (b, c):
        f.action(d.name, "pull")
        assert (d.source_root / path).read_bytes() == target
    settle(f, min_participants=2)


def test_regression_union_deletion_lost_to_a_saved_restore(tmp_path, sim_write_guard, monkeypatch):
    # Deep-search counterexample, minimized: a union line deletion was published only on
    # the device a saved disk restore then rolled back; a superset (and an older version
    # with the same bytes) elsewhere made it look held, so INV7 demanded bytes nobody
    # carried under that vector.
    from mind_meld.store import format as codec

    from .store_fleet import PART_BYTES
    from .test_replication import ACTIVE, Invocation, LifecycleMachine

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / "replay", sim_write_guard))
    state = LifecycleMachine()
    try:
        state.core_witness(case=3)
        state.lifecycle_witness(case=3)
        state.torn_rule(device=1)
        state.backup_rule(device=3)
        state.edit(content=3, device=1, old_mtime=True, path=3)
        state.store(device=1, operation="push")
        state.restore_rule(device=1, policy="saved")
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)


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


def test_gap_partial_own_commit(store_fleet):
    # Value: protects=a crash after k of n parts heals on the next push; fails_when=
    # the owner waits on its own torn slot forever or overwrites a part; why_new=was a
    # strict design gap; seam=none; no_claim=real process crash timing.
    from mind_meld.store import layout

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    for path in ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl"):
        a.edit(path, CONTENTS[3])
    assert f.action("a", "push", crash_parts=1, attended=False).status == "crashed"

    def own_parts():
        return {
            p.relative_to(a.replica_root).as_posix(): p.read_bytes()
            for p in a.replica_root.glob(f"dev/{a.mm_id}/log/*.enc")
        }

    torn = own_parts()
    assert len(torn) == 1
    (torn_family,) = {layout.parse_record_key(key).family for key in torn}
    assert any(r["family"] == torn_family for r in FileState(a.state_root).read().intents.values())
    f.drain()
    assert f.action("b", "pull").status == "pending"
    result = f.action("a", "push")
    # The proven torn slot closes with a fresh complete family at the same seq.
    assert (result.status, result.seq, result.events) == ("published", 1, ("torn-supersede",))
    after = own_parts()
    assert torn.items() <= after.items(), "the torn family is never completed or overwritten"
    families = {layout.parse_record_key(key).family for key in after}
    assert len(families) == 2 and torn_family in families
    after_state = FileState(a.state_root).read()
    published_family = after_state.own_log["1"]["family"]
    assert published_family != torn_family
    # The published family's intent row is cleared; the torn family keeps its row, the
    # proof that lets compaction delete it later and nothing else.
    assert {r["family"] for r in after_state.intents.values()} == {torn_family}
    f.drain()
    assert f.action("b", "pull").status == "applied"
    f.oracle.check(f)
    settle(f, min_participants=2)
    assert (b.source_root / "notes.txt").read_bytes() == CONTENTS[3]


MULTIPART_PATHS = ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl")


def multipart_edit(device, size=450):
    """Four 450-byte files seal into a 3-part commit at the simulator's part size."""
    for path in MULTIPART_PATHS:
        device.edit(path, path[:1].encode() * size)


def own_log_parts(device, kind="log"):
    return {
        p.relative_to(device.replica_root).as_posix(): p.read_bytes()
        for p in device.replica_root.glob(f"dev/{device.mm_id}/{kind}/*.enc")
    }


def test_torn_slot_closes_with_a_zero_entry_commit(store_fleet):
    # Value: protects=a torn slot always closes; fails_when=the publisher skips an empty
    # re-author and leaves a permanent gap for peers; why_new=no supersede rule existed;
    # seam=none; no_claim=real crash timing.
    from mind_meld.store import format as codec
    from mind_meld.store import layout

    from .invariants import context_for

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    multipart_edit(a)
    assert f.action("a", "push", crash_parts=1, attended=False).status == "crashed"
    # The user takes every edit back before the next push: nothing is left to author.
    for path in MULTIPART_PATHS:
        a.delete(path)
    f.drain()
    assert f.action("b", "pull").status == "pending"
    result = f.action("a", "push")
    assert (result.status, result.seq, result.events) == ("published", 1, ("torn-supersede",))
    ctx = context_for(f, a)
    families = ctx.view().slot(a.mm_id, "commit", 1)
    assert len(families.complete) == 1 and len(families.incomplete) == 1
    assert families.complete[0].record.entries == ()
    assert (
        codec.decode_commit(
            families.complete[0].payload, expected=ctx.context(a.mm_id, "commit", 1)
        ).header["entries"]
        == 0
    )
    f.drain()
    assert f.action("b", "pull").status == "applied"
    assert FileState(b.state_root).read().cursors[a.mm_id]["applied_seq"] == 1
    assert layout.parse_record_key(next(iter(own_log_parts(a)))).family
    settle(f, min_participants=2)


@pytest.mark.parametrize("with_edit", [False, True])
def test_restore_redelivery_of_a_multipart_own_commit_stays_pending(store_fleet, with_edit):
    # Value: protects=an incomplete own family that this device cannot prove it wrote
    # is in flight, never torn; fails_when=push supersedes records that are still
    # arriving and forks the restored device; why_new=intent rows are new;
    # seam=saved restore plus part-by-part delivery; no_claim=native redelivery order.
    from mind_meld.store import layout

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    backup = f.snapshot(a)
    multipart_edit(a)
    assert f.action("a", "push", attended=False).status == "published"
    f.drain()
    f.restore(a, backup, index_policy="saved")
    assert not FileState(a.state_root).read().own_log
    assert not FileState(a.state_root).read().intents
    parts = [
        c
        for c in f.transport.pending()
        if c.dst == "a" and layout.parse_record_key(c.path) and "/log/" in c.path
    ]
    assert len(parts) == 3
    for index, choice in enumerate(parts[:-1]):
        f.transport.deliver(choice)
        if with_edit:
            a.edit("notes.txt", b"n" * (index + 1))
        result = f.action("a", "push", attended=False)
        assert (result.status, result.reason) == ("pending", "own-records-in-flight")
        assert result.gap_seq == 1
        # Never writes at that seq: only the redelivered family exists under a's id.
        assert len({layout.parse_record_key(k).family for k in own_log_parts(a)}) == 1
        assert not FileState(a.state_root).read().own_log
    f.drain()
    result = f.action("a", "push", attended=False)
    assert result.status == ("published" if with_edit else "unchanged")
    assert any(r.get("operation") == "own-replay" for r in f.last_observation.history)
    assert "1" in FileState(a.state_root).read().own_log
    assert f.action("b", "pull").status == "applied"
    settle(f, min_participants=2)
    assert (b.source_root / "other.txt").read_bytes() == b"o" * 450


def squatter_parts(f, device, seq, nparts):
    """Authentic sealed parts of ANOTHER writer's record for one slot (same device id).

    The integrity checker rightly refuses unauthentic files in a replica, so a name
    collision is modelled with a genuine record from a second publisher.
    """
    from mind_meld.store import format as codec

    from .invariants import context_for
    from .model import core, store

    ctx = context_for(f, device)
    expected = ctx.context(device.mm_id, "commit", seq)
    for size in range(500, 6000, 100):
        entries = (core.entry("claude", "squat.txt", b"s" * size, {"cccccccc": 1}),)
        payload = codec.encode_commit(
            store.header(ctx, "commit", seq, None, entries), entries, expected=expected
        )
        parts = [data for _, data in codec.seal_record(payload, ctx.keys, expected=expected)]
        if len(parts) == nparts:
            return parts
    raise AssertionError(f"no squatter payload seals into {nparts} parts")


def squat_on_draw(f, device, seq, nparts, parts, clashes):
    """Create the squatter's part once the publisher draws a clashing family."""
    from mind_meld.store import layout

    sealed = squatter_parts(f, device, seq, nparts)
    keys = {}

    def hook(family):
        if family in clashes:
            for part in parts:
                key = layout.record_key(
                    device.mm_id, "commit", seq, part=part, nparts=nparts, family=family
                )
                target = device.replica_root / key
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sealed[part - 1])
                keys[key] = sealed[part - 1]

    f.family_hook = hook
    return keys


def test_exclusive_create_collision_relinks_under_a_fresh_family(store_fleet):
    # Value: protects=the publisher never overwrites and recovers by drawing a fresh
    # family; fails_when=a retry overwrites a part or loops; why_new=retry contract is
    # new; seam=family_source collision hook; no_claim=real filesystem races.
    from mind_meld.store import layout

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    multipart_edit(a)
    clash = "feedface"
    squatted = squat_on_draw(f, a, 1, 3, (2,), {clash})
    f.force_families(clash)
    result = f.action("a", "push", attended=False)
    assert result.status == "published"
    assert len(squatted) == 1
    state = FileState(a.state_root).read()
    published = state.own_log["1"]["family"]
    assert published != clash and state.own_log["1"]["parts"] == 3
    by_family = {}
    for key in own_log_parts(a):
        name = layout.parse_record_key(key)
        by_family.setdefault(name.family, set()).add(name.part)
    # Part 1 of the colliding family stays (a torn family of this device's own), the
    # squatter's part 2 is untouched, and the published family is complete.
    assert by_family == {clash: {1, 2}, published: {1, 2, 3}}
    for key, data in squatted.items():
        assert (a.replica_root / key).read_bytes() == data
    # Both attempts recorded an intent row; only the published one was cleared.
    assert {r["family"] for r in state.intents.values()} == {clash}
    f.family_hook = None
    f.drain()
    assert f.action("b", "pull").status == "applied"
    assert (b.source_root / "notes.txt").read_bytes() == b"n" * 450
    settle(f, min_participants=2)


def test_exclusive_create_exhaustion_fails_loudly_without_overwriting(store_fleet):
    # Value: protects=bounded retries end in a loud, actionable failure; fails_when=the
    # publisher retries forever, overwrites a squatter or hides the failure;
    # why_new=retry contract is new; seam=family_source collision hook.
    from .model.store import MAX_NAME_ATTEMPTS, PublishError

    f = store_fleet
    a = f.devices["a"]
    multipart_edit(a)
    clashes = ["feedface", "deadbeef", "cafef00d"]
    assert len(clashes) == MAX_NAME_ATTEMPTS == 3
    squatted = squat_on_draw(f, a, 1, 3, (1,), set(clashes))
    f.force_families(*clashes)
    with pytest.raises(PublishError) as raised:
        f.action("a", "push", attended=False)
    assert str(raised.value) == (
        "could not allocate a fresh record name for seq 1 after 3 attempts; "
        "another writer may share this device id; run `mm diag`"
    )
    assert len(squatted) == 3
    for key, data in squatted.items():
        assert (a.replica_root / key).read_bytes() == data
    state = FileState(a.state_root).read()
    assert not state.own_log and not state.ledger
    assert {r["family"] for r in state.intents.values()} == set(clashes)


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
        family = ctx.family_source()  # The fixture publisher draws one like the real one must.
        for part, sealed in codec.seal_record(payload, ctx.keys, expected=expected):
            target = ctx.replica_root / layout.record_key(
                ctx.device_id, "commit", seq, part=part.part, nparts=part.nparts, family=family
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
    a_id = f.devices["a"].mm_id
    c = f.clone(f.devices["a"], "c")
    for name, data in (("a", CONTENTS[1]), ("c", CONTENTS[2])):
        f.devices[name].edit("notes.txt", data)
        assert f.action(name, "push", attended=False).status == "published"
    f.drain()
    # Family names make the clones' commits distinct files, so the transport mints no
    # conflict copy: the evidence is two complete families with different payloads.
    slot_families = {
        layout.parse_record_key(p.relative_to(c.replica_root).as_posix()).family
        for p in c.replica_root.glob(f"dev/{a_id}/log/000000000001.*.enc")
    }
    assert len(slot_families) == 2
    for name in ("a", "c"):
        # Each copy now holds a family published from the other host under its own id.
        result = f.action(name, "push")
        assert result.reason == "fork-clone"
        assert result.warnings[0] == owner_text("fork-clone")
        assert f.action(name, "pull").reason == "fork-clone"
    # A reader never refuses: b applies both families and names the clone remedy.
    result = f.action("b", "pull")
    assert result.status == "applied"
    (warning,) = [w for w in result.warnings if a_id in w]
    assert "is used by more than one machine" in warning
    assert warning.endswith("run `mm rejoin` on each machine using this id")
    f.devices["b"].edit("other.txt", CONTENTS[3])
    f.action("b", "push", attended=False)
    f.rejoin(c)
    f.registry["snapshot"] = snapshot_piece
    f.drain()
    result = f.action("c", "pull")
    assert result.status == "applied"
    assert (c.source_root / "other.txt").read_bytes() == CONTENTS[3]
    # The original keeps refusing until it rejoins too: the clone's records stay readable
    # in the old namespace.
    assert f.action("a", "push").reason == "fork-clone"
    f.rejoin(f.devices["a"])
    f.oracle.check(f)
    settle(f, min_participants=3)


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
def test_snapshot_fallback_raises_other_cursors_and_applies_without_a_chain_anchor(
    store_fleet, anchor_kind
):
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
    # Another device's snapshot raised a's cursor: no hash, and it covers no family of a's.
    assert (cursor["applied_seq"], cursor["hash"]) == (1, None)
    assert not any(r["peer"] == a.mm_id for r in FileState(c.state_root).read().applied.values())
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
    # No chain-anchor wait: a's commit 2 applies at once, its prev unchecked because
    # slot 1 is neither applied nor covered by a's own snapshot.
    result = f.action("c", "pull")
    assert (result.status, result.reason) == ("applied", None)
    assert FileState(c.state_root).read().cursors[a.mm_id]["applied_seq"] == 2
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
    result = f.action("c", "pull")
    assert FileState(c.state_root).read().cursors[a.mm_id]["applied_seq"] == 2
    # The anchor arriving later is a late family when it is a commit; a snapshot at or
    # below the cursor changes nothing.
    assert ("late-family" in result.events) is (anchor_kind == "commit")
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
    if fork_kind == "fleet-header":
        # The header copy is global: every device refuses (INV6).
        assert (result.status, result.reason, result.peer) == ("refused", "fork", expected_peer)
        assert result.warnings
    else:
        # A peer's conflict copy is evidence and a warning, never a refusal: the gap
        # stays pending, its aged warning stays visible, and the copy is named.
        assert (result.status, result.reason, result.peer, result.gap_seq) == (
            "pending",
            "pending",
            a.mm_id,
            1,
        )
        assert "gap older than 24 hours" in result.warnings
        assert any(
            f"{a.mm_id}'s log has a conflicting copy of record 1; run `mm diag`, then "
            f"`mm rejoin` on {a.mm_id}" == w
            for w in result.warnings
        )
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
    # The intent row proves the partial family is this device's own torn publish, so
    # the retry writes a fresh complete family at the same seq. The torn family's bytes
    # are neither completed nor overwritten nor deleted.
    assert (retry.status, retry.seq, retry.events) == ("published", crash.seq, ("torn-supersede",))
    assert original.items() <= parts().items()
    assert len({layout.parse_record_key(key).family for key in parts()}) == 2
    # Keep both families until two newer complete snapshots cover that seq.
    for data, retained in ((CONTENTS[1], True), (CONTENTS[2], False)):
        a.edit("notes.txt", data)
        assert f.action("a", "publish_snapshot").status == "published"
        assert bool(parts()) is retained
        assert not retained or original.items() <= parts().items()
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


# --- Coverage-audit witnesses (store-v2 §5, §9): the unproven-family escapes, fork
# checks beneath a recovery snapshot, and family-keyed trust in an unreadable record.


def test_unproven_torn_slot_waits_on_autopush_and_closes_on_an_attended_push(store_fleet):
    # Value: protects=an unproven incomplete own family wedges only autopush: pull names
    # it, autopush waits, an attended synced push supersedes it; fails_when=the escape
    # drops its attended guard or goes, or pull stops reporting the blocked slot;
    # why_new=of eight torn and restore witnesses probed, none pinned autopush waiting
    # or the pull report; seam=none; no_claim=real crash timing and Syncthing's idle
    # signal (modelled as no pending deliveries).
    f = store_fleet
    a = f.devices["a"]
    backup = f.snapshot(a)  # Taken before the crash: no intent row.
    multipart_edit(a)
    assert f.action("a", "push", crash_parts=1, attended=False).status == "crashed"
    f.drain()
    f.restore(a, backup, scope="state")  # Drops the intent row; the torn part stays.
    torn = own_log_parts(a)
    assert len(torn) == 1 and not FileState(a.state_root).read().intents
    blocked = owner_text("own-records-in-flight", claimed=1)
    pulled = f.action("a", "pull")
    assert (pulled.status, pulled.peer, pulled.gap_seq, pulled.reason) == (
        "pending",
        a.mm_id,
        1,
        "own-records-in-flight",
    )
    assert blocked in pulled.warnings
    waited = f.action("a", "push", attended=False)
    assert (waited.status, waited.reason, waited.gap_seq) == ("pending", "own-records-in-flight", 1)
    assert own_log_parts(a) == torn  # Autopush never writes at the slot.
    closed = f.action("a", "push")  # Attended, synced, and no receipt claims the seq.
    assert (closed.status, closed.seq, closed.events) == ("published", 1, ("torn-supersede",))
    assert torn.items() <= own_log_parts(a).items()  # The unproven family is untouched.
    assert blocked not in f.action("a", "pull").warnings
    f.drain()
    assert f.action("b", "pull").status == "applied"
    settle(f, min_participants=2)


def two_branches_at_push_one(f, *, alpha_snapshot):
    """Alpha reaches b, a is restored (saved) and republishes push #1 as bravo."""
    a, b = f.devices["a"], f.devices["b"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])  # Alpha at push #1.
    f.action("a", "push", attended=False)
    if alpha_snapshot:
        f.action("a", "publish_snapshot")  # Its prev names alpha.
    deliver_where(f, b, lambda path: alpha_snapshot or "/log/" in path)
    f.restore(a, backup, index_policy="saved")
    a.edit("notes.txt", CONTENTS[2])  # Bravo reuses push #1.
    f.action("a", "push", attended=False)
    return a, b, backup


def test_own_snapshot_recovery_refuses_two_own_families_below_it(store_fleet):
    # Value: protects=two complete own families with different digests below a recovery
    # snapshot refuse fork-restore with no own_log row naming either; fails_when=the
    # distinct-digest pre-check goes and recovery loads the snapshot, skipping the
    # forked seq; why_new=that check's mutant survived all nine fork and recovery
    # witnesses; seam=none; no_claim=native redelivery order.
    f = store_fleet
    a, _, backup = two_branches_at_push_one(f, alpha_snapshot=False)
    a.edit("other.txt", CONTENTS[3])
    f.action("a", "push", attended=False)  # Push #2 on the bravo branch.
    f.action("a", "publish_snapshot")  # The recovery snapshot, above the forked seq.
    f.drain()  # Alpha's push #1 reaches a.
    f.restore(a, backup, scope="state")  # No own_log row names either branch.
    result = f.action("a", "pull")
    assert (result.status, result.reason) == ("refused", "fork-restore")
    assert result.warnings[0] == owner_text("fork-restore")
    assert "fork-restore" in f.last_observation.ground_truth


def test_own_snapshot_recovery_refuses_a_branch_its_anchor_contradicts(store_fleet):
    # Value: protects=a recovery snapshot whose prev names another family than the only
    # complete own family at its seq refuses fork-restore (§9 rows 1 and 4);
    # fails_when=the anchor-prev check goes and recovery adopts the other branch,
    # skipping this device's own commit; why_new=that check's mutant survived all nine
    # fork and recovery witnesses; seam=none; no_claim=that random search reaches this.
    # Red when written (coverage audit, 2026-10-17): the model refused as §9 says, but
    # INV4 rejected the refusal because ground truth never applied row 1's anchor; the
    # checker now does, and the ground-truth assertion below pins it.
    f = store_fleet
    a, b, backup = two_branches_at_push_one(f, alpha_snapshot=True)
    for path in list(b.replica_root.glob(f"dev/{a.mm_id}/log/000000000001.*.enc")):
        path.unlink()  # Alpha's commit is gone (a rollback elsewhere); its snapshot is not.
    f.transport.scan("b")
    f.drain()  # Alpha's snapshot reaches a.
    f.restore(a, backup, scope="state")  # No own_log row names either branch.
    result = f.action("a", "pull")
    assert (result.status, result.reason) == ("refused", "fork-restore")
    assert result.warnings[0] == owner_text("fork-restore")
    assert "fork-restore" in f.last_observation.ground_truth


@pytest.mark.parametrize("operation", ["pull", "publish_snapshot"])
def test_unreadable_own_family_this_device_did_not_record_stalls(store_fleet, operation):
    # Value: protects=trust in an unreadable own commit is keyed by the family own_log
    # names: another family alone at the slot stalls, even a forced snapshot, with the
    # diag and rejoin text; fails_when=trust falls back to the seq, so push --snapshot
    # seals over a record this device never wrote; why_new=no test asserted the
    # untrusted branch or its text; seam=none; no_claim=cryptographic writer
    # authentication.
    from mind_meld.store import layout

    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.allow_corruption = True
    (recorded,) = a.replica_root.glob(f"dev/{a.mm_id}/log/000000000001.*.enc")
    name = layout.parse_record_key(recorded.relative_to(a.replica_root).as_posix())
    assert name.family == FileState(a.state_root).read().own_log["1"]["family"]
    data = recorded.read_bytes()
    recorded.with_name("000000000001.baadf00d.enc").write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    recorded.unlink()  # Only an unreadable family this device did not record remains.
    f.transport.scan("a")
    result = f.action("a", operation, **({"force": True} if operation != "pull" else {}))
    assert (result.status, result.peer, result.gap_seq) == ("stalled-unreadable", a.mm_id, 1)
    assert result.warnings == (
        "unreadable own commit this device did not record; run `mm diag`, "
        "then `mm rejoin` if it persists",
    )


def test_operator_snapshot_supersedes_an_unproven_torn_snapshot_once_synced(store_fleet):
    # Value: protects=`mm push --snapshot` is not wedged by an incomplete own snapshot
    # family it cannot prove it wrote, once synced and unclaimed; fails_when=snapshot()
    # loses its supersede escape and pends at that seq; why_new=its mutant survived the
    # eight torn and restore witnesses probed; seam=none; no_claim=real crash timing and
    # the torn family's later compaction.
    from .invariants import context_for

    f = store_fleet
    a = f.devices["a"]
    multipart_edit(a)
    f.action("a", "push", attended=False)  # Push #1: the ledger now seals multipart.
    backup = f.snapshot(a)  # own_log {1}; no snapshot intent row yet.
    assert f.action("a", "publish_snapshot", crash_parts=1).status == "crashed"
    f.drain()
    f.restore(a, backup, scope="state")  # Drops the intent row; the torn part stays.
    torn = own_log_parts(a, "snap")
    slot = context_for(f, a).view().slot(a.mm_id, "snapshot", 1)
    assert (len(slot.complete), len(slot.incomplete)) == (0, 1)
    assert not FileState(a.state_root).read().intents
    result = f.action("a", "publish_snapshot")
    assert (result.status, result.seq) == ("published", 1)
    assert torn.items() <= own_log_parts(a, "snap").items()  # Never completed or overwritten.
    slot = context_for(f, a).view().slot(a.mm_id, "snapshot", 1)
    assert (len(slot.complete), len(slot.incomplete)) == (1, 1)
    f.drain()
    assert f.action("b", "pull").status == "applied"
    settle(f, min_participants=2)
