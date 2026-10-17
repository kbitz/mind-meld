"""Deterministic programs kill each seeded defect at its named invariant."""

import pytest

from mind_meld.store import layout

from .invariants import InvariantError, inv1_convergence, settle
from .model import core, store
from .model.registry import Piece
from .model.state import FileState, path_key, row
from .model.store import Outcome
from .store_fleet import CONTENTS


@pytest.mark.parametrize(
    "mutant,expected",
    [
        ("drop-later", 1),
        ("overwrite-unpublished", 2),
        ("peer-write", 3),
        ("missing-error", 4),
        ("local-wins", 5),
        ("skip-host-fork", 6),
        ("refuse-everything", 4),
        ("path-loss", 2),
        ("dominance-concurrent", 7),
        ("resurrect", 7),
        ("plaintext", 3),
    ],
)
def test_mutant_witness(store_fleet, monkeypatch, mutant, expected):
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    key = path_key("claude", "notes.txt")

    def replace_piece(name, implementation):
        previous = f.registry[name]
        f.registry[name] = Piece(implementation, "model", previous.owner, "seeded mutant")

    def drop_later(ctx, *, incoming, peer, seq, **kwargs):
        return "ignore" if seq >= 2 else store.classify(ctx, incoming=incoming, peer=peer, seq=seq)

    def overwrite(ctx, *, incoming, peer, seq, **kwargs):
        (ctx.source_roots["claude"] / incoming.metadata["path"]).unlink(missing_ok=True)
        with ctx.state.transaction() as state:
            state.ledger.pop(path_key(incoming.metadata["source"], incoming.metadata["path"]), None)
        return store.classify(ctx, incoming=incoming, peer=peer, seq=seq)

    def peer_write(ctx, **kwargs):
        target = ctx.replica_root / layout.record_key(b.mm_id, "commit", 1, family="deadbeef")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"bad peer write")
        return Outcome("published", seq=1)

    def path_loss(ctx, **kwargs):
        result = store.read(ctx, **kwargs)
        (ctx.source_roots["claude"] / "notes.txt").unlink(missing_ok=True)
        return result

    def resurrect(ctx, *, incoming, peer, seq, **kwargs):
        action = store.classify(ctx, incoming=incoming, peer=peer, seq=seq)
        if incoming.content is None:
            revived = core.entry("claude", "notes.txt", CONTENTS[1], incoming.metadata["vv"])
            (ctx.source_roots["claude"] / "notes.txt").write_bytes(revived.content)
            with ctx.state.transaction() as state:
                state.ledger[key] = row(revived)
                state.history[-1]["result"] = row(revived)
        return action

    def plaintext(ctx, **kwargs):
        target = ctx.replica_root / layout.record_key(ctx.device_id, "commit", 1, family="deadbeef")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CONTENTS[1])
        return Outcome("published", seq=1)

    with pytest.raises(InvariantError) as raised:
        if mutant == "drop-later":
            replace_piece("classify", drop_later)
            for data in CONTENTS[1:3]:
                a.edit("notes.txt", data)
                f.action("a", "push")
                f.drain()
                f.action("b", "pull")
            settle(f)
        elif mutant == "overwrite-unpublished":
            a.edit("notes.txt", CONTENTS[1])
            settle(f)
            b.edit("notes.txt", CONTENTS[2])
            a.edit("notes.txt", CONTENTS[3])
            f.action("a", "push")
            f.drain()
            replace_piece("classify", overwrite)
            f.action("b", "pull")
        elif mutant == "peer-write":
            replace_piece("publish", peer_write)
            f.action("a", "push")
        elif mutant == "missing-error":
            replace_piece("read", lambda ctx, **kw: Outcome("error", reason="missing commit"))
            f.action("b", "pull")
        elif mutant == "local-wins":
            replace_piece(
                "resolve",
                lambda ctx, *, local, incoming, **kw: core.entry(
                    local.metadata["source"],
                    local.metadata["path"],
                    local.content,
                    core.vector_max(local.metadata["vv"], incoming.metadata["vv"]),
                    local.metadata["mtime"],
                ),
            )
            for name, data in [("a", CONTENTS[1]), ("b", CONTENTS[2])]:
                f.devices[name].edit("notes.txt", data)
                f.action(name, "push")
            f.drain()
            settle(f)
        elif mutant == "skip-host-fork":
            # Live hardware differs from persisted own presence, just as a copied device id does.
            a.hardware_id = "clone-hardware"
            a.edit("notes.txt", CONTENTS[1])
            replace_piece("reconcile", lambda ctx, **kw: Outcome("applied"))
            f.action("a", "push")
        elif mutant == "refuse-everything":
            replace_piece("read", lambda ctx, **kw: Outcome("refused", reason="fork-clone"))
            f.action("a", "pull")
        elif mutant == "path-loss":
            for path in ("notes.txt", "other.txt"):
                a.edit(path, CONTENTS[1])
            f.action("a", "push")
            f.drain()
            replace_piece("read", path_loss)
            settle(f)
        elif mutant == "dominance-concurrent":
            a.edit("notes.txt", CONTENTS[1])
            settle(f)
            a.edit("notes.txt", CONTENTS[2])
            f.action("a", "push")
            f.drain()
            monkeypatch.setattr(core, "classify", lambda local, incoming, **kw: "resolve")
            f.action("b", "pull")
        elif mutant == "resurrect":
            a.edit("notes.txt", CONTENTS[1])
            settle(f)
            a.delete("notes.txt")
            f.action("a", "push")
            f.drain()
            replace_piece("classify", resurrect)
            f.action("b", "pull")
        else:
            replace_piece("publish", plaintext)
            f.action("a", "push")
    failures = {raised.value.invariant}
    # INV7 now detects these defects before settle. Independently retain the
    # original convergence/path-preservation witnesses at the same broken state.
    if mutant in ("drop-later", "path-loss", "overwrite-unpublished"):
        with pytest.raises(InvariantError) as original_clause:
            if mutant == "drop-later":
                inv1_convergence(f)
            else:
                f.oracle.check(f, quiescent=True)
        failures.add(original_clause.value.invariant)
    assert expected in failures, f"{mutant}: INV{expected} lost its teeth; observed {raised.value}"


def test_missing_unpublished_increment_is_killed_by_causal_checker(store_fleet, monkeypatch):
    from dataclasses import replace

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("MEMORY.md", CONTENTS[1])
    settle(f)
    b.edit("MEMORY.md", CONTENTS[2])
    a.edit("MEMORY.md", CONTENTS[3])
    f.action("a", "push", attended=False)
    f.drain()
    original = core.local_version

    def missing_increment(ledger, data, mtime, device, **kwargs):
        local = original(ledger, data, mtime, device, **kwargs)
        if ledger is not None and local is not None:
            return replace(local, metadata={**local.metadata, "vv": ledger.metadata["vv"]})
        return local

    monkeypatch.setattr(core, "local_version", missing_increment)
    with pytest.raises(InvariantError) as raised:
        f.action("b", "pull")
    assert raised.value.invariant == 7


@pytest.mark.parametrize(
    "fault", ["missing-history", "incoming-vector", "result-content", "source-effect"]
)
def test_causal_checker_binds_history_to_records_and_observed_effects(store_fleet, fault):
    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()

    def tamper(ctx, **kwargs):
        result = store.classify(ctx, **kwargs)
        with ctx.state.transaction() as state:
            if fault == "missing-history":
                state.history.pop()
            elif fault == "incoming-vector":
                state.history[-1]["incoming"]["vv"] = {a.mm_id: 99}
            elif fault == "result-content":
                state.history[-1]["result"]["content"] = b"forged"
        if fault == "source-effect":
            (b.source_root / "notes.txt").write_bytes(b"forged")
        return result

    old = f.registry["classify"]
    f.registry["classify"] = Piece(tamper, "model", old.owner, "causal binding mutant")
    with pytest.raises(InvariantError) as raised:
        f.action("b", "pull")
    assert raised.value.invariant == 7


def test_same_sha_cannot_clear_pending_publication(store_fleet):
    # Value: protects=E12 pending publication across same-SHA receipt;
    # fails_when=classifier clears persisted and reported pending together;
    # why_new=vector and bytes agree despite lost publication; seam=registry;
    # no_claim=real publisher or SQLite acceptance (model and file seam).
    from .test_scenarios import join_extra

    f = store_fleet
    c = join_extra(f)
    key = path_key("claude", "MEMORY.md")
    for name, data in (("a", CONTENTS[1]), ("b", CONTENTS[2])):
        f.devices[name].edit("MEMORY.md", data)
        f.action(name, "push", attended=False)
    f.drain()
    f.action("c", "pull")
    assert FileState(c.state_root).read().ledger[key]["pending"]
    f.action("a", "pull")
    assert f.action("a", "push", attended=False).status == "published"
    f.drain()
    seen = []

    def clear_pending(ctx, **kwargs):
        action = store.classify(ctx, **kwargs)
        if action == "same-sha":
            with ctx.state.transaction() as state:
                assert state.ledger[key]["pending"]
                seen.append(action)
                state.ledger[key]["pending"] = False
                state.history[-1]["result"]["pending"] = False
        return action

    previous = f.registry["classify"]
    f.registry["classify"] = Piece(clear_pending, "model", previous.owner, "pending loss mutant")
    with pytest.raises(InvariantError) as raised:
        f.action("c", "pull")
    assert seen == ["same-sha"]
    assert raised.value.invariant == 7


@pytest.mark.parametrize(
    "target,noop",
    [
        ("own-log", False),
        ("own-cursor", False),
        ("both", False),
        ("peer-cursor", False),
        ("own-log", True),
        ("own-cursor", True),
    ],
)
def test_saved_hash_mutant_is_detected(store_fleet, target, noop):
    # Value: protects=authenticated chain state; fails_when=hash-only state
    # corruption escapes INV7; why_new=existing checks compare only own-log
    # keys and cursor sequences; seam=registry replacement plus FileState.
    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    peer = target == "peer-cursor"
    if peer or noop:
        f.action("a", "push", attended=False)
        f.drain()
    piece = "read" if peer else "publish"
    previous = f.registry[piece]

    def corrupt_saved_hash(ctx, **kwargs):
        result = previous.implementation(ctx, **kwargs)
        with ctx.state.transaction() as state:
            if target in ("own-log", "both"):
                state.own_log["1"]["hash"] = "0" * 64
            if target in ("own-cursor", "both", "peer-cursor"):
                state.cursors[a.mm_id]["hash"] = "0" * 64
        return result

    f.registry[piece] = Piece(
        corrupt_saved_hash, "model", previous.owner, "saved chain hash mutant"
    )
    with pytest.raises(InvariantError) as raised:
        f.action("b" if peer else "a", "pull" if peer else "push", attended=False)
    assert raised.value.invariant == 7


# --- Restore and torn-publish mutants (store-v2 sections 5, 6 and 9) ----------------
# Each program breaks exactly one new rule. NEW_MUTANTS records the invariant that fires
# FIRST, confirmed by running; a rule's own invariant must also hold on the broken state.
NEW_MUTANTS = {
    "count-incomplete": 7,
    "mix-families": 4,
    "skip-late-family": 1,
    "reader-refuses-peer-fork": 4,
    "delete-unproven-incomplete": 2,
    "overwrite-part": 4,
    "disable-heal": 1,
    "reapply-applied-family": 7,
}


def replace_piece(f, name, implementation):
    previous = f.registry[name]
    f.registry[name] = Piece(implementation, "model", previous.owner, "seeded mutant")


@pytest.mark.parametrize("mutant", sorted(NEW_MUTANTS))
def test_restore_and_torn_publish_mutant_is_killed_at_its_invariant(
    store_fleet, monkeypatch, mutant
):
    import os

    from mind_meld.store import envelope, layout

    from .model import store as model
    from .test_scenarios import (
        MULTIPART_PATHS,
        alpha_bravo_restore,
        deliver_where,
        join_extra,
        multipart_edit,
    )

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    with pytest.raises(InvariantError) as raised:
        if mutant == "count-incomplete":
            # The reader counts a torn family as applied: the cursor moves with no receipt.
            multipart_edit(a)
            f.action("a", "push", crash_parts=1, attended=False)
            f.drain()

            def count(ctx, **kwargs):
                result = store.read(ctx, **kwargs)
                if result.status == "pending" and result.peer != ctx.device_id:
                    with ctx.state.transaction() as state:
                        state.cursors[result.peer]["applied_seq"] = result.gap_seq
                return result

            replace_piece(f, "read", count)
            f.action("b", "pull")
        elif mutant == "mix-families":
            # One open over every part at a slot, whatever family names them.
            multipart_edit(a)
            f.action("a", "push", crash_parts=1, attended=False)
            for path in MULTIPART_PATHS:
                a.edit(path, path[:1].encode() * 40)
            # Autopush: an attended push would also snapshot and give b a way around.
            assert f.action("a", "push", attended=False).events == ("torn-supersede",)
            f.drain()
            real_slot = model.View.slot

            def mixed(self, device, kind, seq):
                found = real_slot(self, device, kind, seq)
                if len(found.complete) + len(found.incomplete) < 2:
                    return found
                parts = [
                    (
                        envelope.RecordContext(
                            self.ctx.keys.fleet_id, device, kind, seq, n.part, n.nparts
                        ),
                        (self.ctx.replica_root / key).read_bytes(),
                    )
                    for key, n in self.names.items()
                    if (n.device_id, n.kind, n.seq) == (device, kind, seq)
                ]
                try:
                    envelope_open = model.codec.open_record(
                        parts, self.ctx.keys, expected=self.ctx.context(device, kind, seq)
                    )
                except envelope.StoreError:
                    return model.Slot(unreadable=("mixed",))
                raise AssertionError(f"unexpected mixed open: {envelope_open!r}")

            monkeypatch.setattr(model.View, "slot", mixed)
            f.action("b", "pull")
        elif mutant == "skip-late-family":
            # Bravo is the only carrier of other.txt; b already holds alpha at that seq.
            join_extra(f, "c")
            backup = f.snapshot(a)
            a.edit("notes.txt", CONTENTS[1])
            f.action("a", "push", attended=False)
            deliver_where(f, b, lambda path: "/log/" in path)
            f.action("b", "pull")
            f.restore(a, backup, index_policy="saved")
            a.edit("notes.txt", CONTENTS[2])
            a.edit("other.txt", CONTENTS[3])
            f.action("a", "push", attended=False)
            f.drain()

            class Anything:
                def __ne__(self, other):
                    return False

            class CoversBySeq(dict):
                """A snapshot cursor that covers every family at or below it."""

                def get(self, key, default=None):
                    return Anything()

            monkeypatch.setattr(model, "covered_chain", lambda *args: CoversBySeq())
            settle(f)
        elif mutant == "reader-refuses-peer-fork":
            alpha_bravo_restore(f)

            def refuse_peer(ctx, **kwargs):
                result = store.read(ctx, **kwargs)
                forked = [p for p, c in ctx.state.read().cursors.items() if "fork" in c]
                if forked:
                    return Outcome("refused", peer=forked[0], reason="fork")
                return result

            replace_piece(f, "read", refuse_peer)
            f.action("b", "pull")
        elif mutant == "delete-unproven-incomplete":
            # Publish clears the incomplete family it cannot prove it wrote, then writes in
            # its slot; every holder of the complete original then loses a part.
            join_extra(f, "c")
            c = f.devices["c"]
            backup = f.snapshot(a)
            multipart_edit(a)
            f.action("a", "push", attended=False)
            f.drain()
            f.restore(a, backup, index_policy="saved")
            deliver_where(f, a, lambda path: "/log/" in path and path.endswith("1of3.enc"))
            for device in (b, c):
                f.transport.set_online(device.name, False)
            real_slot = model.own_slot

            def reckless(ctx, state, view, kind, seq):
                slot = view.slot(ctx.device_id, kind, seq)
                for key, name in view.names.items():
                    if (name.device_id, name.kind, name.seq) == (ctx.device_id, kind, seq) and (
                        name.family in slot.incomplete
                    ):
                        (ctx.replica_root / key).unlink()
                return slot, (
                    "torn" if slot.incomplete else real_slot(ctx, state, view, kind, seq)[1]
                )

            monkeypatch.setattr(model, "own_slot", reckless)
            a.edit("notes.txt", CONTENTS[2])
            f.action("a", "push", attended=False)
            settle(f)
        elif mutant == "overwrite-part":
            # Complete a torn family by re-sealing into the SAME names: hybrid parts.
            multipart_edit(a)
            f.action("a", "push", crash_parts=1, attended=False)
            f.drain()  # b holds the old part 1.
            f.advance(1)  # The re-authored commit differs, so its sealed parts do too.
            real_write = model.write_record

            def overwrite(ctx, kind, seq, payload, *, crash_parts=None):
                torn = [v for v in ctx.state.read().intents.values() if v["seq"] == seq]
                if not torn:
                    return real_write(ctx, kind, seq, payload, crash_parts=crash_parts)
                family = torn[0]["family"]
                parts = tuple(
                    model.codec.seal_record(
                        payload, ctx.keys, expected=ctx.context(ctx.device_id, kind, seq)
                    )
                )
                for part, data in parts:
                    key = layout.record_key(
                        ctx.device_id, kind, seq, part=part.part, nparts=part.nparts, family=family
                    )
                    target = ctx.replica_root / key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp = target.parent / ".mm-tmp-publish"
                    temp.write_bytes(data)
                    os.replace(temp, target)
                return model.Written(family, len(parts), len(parts))

            monkeypatch.setattr(model, "write_record", overwrite)
            f.action("a", "push", attended=False)
            # b receives the new parts 2 and 3 but not the part-1 replacement: hybrid parts.
            deliver_where(f, b, lambda path: path.endswith(("2of3.enc", "3of3.enc")))
            f.action("b", "pull")
        elif mutant == "reapply-applied-family":
            # A family already applied is applied again on a later pull.
            a.edit("notes.txt", CONTENTS[1])
            f.action("a", "push", attended=False)
            f.drain()
            f.action("b", "pull")
            monkeypatch.setattr(model, "applied_digests", lambda *args, **kwargs: set())
            f.action("b", "pull")
        elif mutant == "disable-heal":
            a.edit("notes.txt", CONTENTS[1])
            f.action("a", "push", attended=False)
            f.drain()
            f.action("b", "pull")
            a.edit("notes.txt", CONTENTS[2])
            f.action("a", "push", attended=False)
            for path in a.replica_root.glob(f"dev/{a.mm_id}/log/000000000002.*.enc"):
                path.unlink()
            f.transport.scan("a")
            f.drain()
            monkeypatch.setattr(model, "own_records_lost", lambda *args, **kwargs: False)
            f.action("a", "push")
            settle(f)
    assert raised.value.invariant == NEW_MUTANTS[mutant], (
        f"{mutant}: expected INV{NEW_MUTANTS[mutant]} first, observed {raised.value}"
    )


@pytest.mark.parametrize("with_edit", [False, True])
def test_in_flight_witness_kills_the_supersede_in_flight_mutant(
    store_fleet, monkeypatch, with_edit
):
    # A restored device that treats any incomplete own family as torn writes at the slot
    # its records are still arriving into. That forks it, which is a recoverable owner
    # refusal (INV6 requires it), so NO invariant is violated and the kill is the
    # deterministic witness: it asserts the push stays pending and never writes there.
    from .model import store as model
    from .test_scenarios import test_restore_redelivery_of_a_multipart_own_commit_stays_pending

    monkeypatch.setattr(
        model,
        "own_slot",
        lambda ctx, state, view, kind, seq: (
            lambda slot: (slot, "torn" if slot.incomplete else "free")
        )(view.slot(ctx.device_id, kind, seq)),
    )
    # Torn closure has two independent guards (intent proof and the folder-synced
    # gate); force the folder synced so this mutant isolates the intent proof.
    store_fleet.folder_synced = lambda name: True
    with pytest.raises(AssertionError) as raised:
        test_restore_redelivery_of_a_multipart_own_commit_stays_pending(store_fleet, with_edit)
    assert not isinstance(raised.value, InvariantError), (
        "the mutant must be caught by the witness, not by an invariant: " + str(raised.value)
    )


def test_detection_witness_kills_the_detection_disabled_mutant(store_fleet, monkeypatch):
    # Without restored-device detection the device publishes at a seq its own presence and
    # its peers' receipts already claim, and its records then arrive into that seq (a
    # fork). That is again a recoverable owner refusal, so the kill is the witness.
    from .model import store as model
    from .test_scenarios import (
        test_fresh_replica_restore_waits_for_syncthing_and_never_advises_rejoin,
    )

    monkeypatch.setattr(model, "claimed_seq", lambda *args, **kwargs: 0)
    with pytest.raises(AssertionError) as raised:
        test_fresh_replica_restore_waits_for_syncthing_and_never_advises_rejoin(store_fleet, 0)
    assert not isinstance(raised.value, InvariantError), (
        "the mutant must be caught by the witness, not by an invariant: " + str(raised.value)
    )


def test_inv7_verifies_the_persisted_applied_set(store_fleet, monkeypatch):
    # Mutant: apply a commit's entries but drop its persisted receipt row. INV7 must
    # fail against the final state instead of seeding its next baseline from the
    # model's own (now wrong) applied set.
    from .model import store as model

    real = model.apply_commit

    def drop_receipt(ctx, peer, found, chain, events):
        real(ctx, peer, found, chain, events)
        with ctx.state.transaction() as updated:
            for key, value in list(updated.applied.items()):
                if value["peer"] == peer and value["digest"] == found.digest:
                    del updated.applied[key]

    monkeypatch.setattr(model, "apply_commit", drop_receipt)
    a = store_fleet.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    store_fleet.action("a", "push", attended=False)
    store_fleet.drain()
    with pytest.raises(InvariantError) as raised:
        store_fleet.action("b", "pull")
    assert raised.value.invariant == 7
