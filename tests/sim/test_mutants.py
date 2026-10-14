"""Deterministic programs kill each seeded defect at its named invariant."""

import pytest

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
        target = ctx.replica_root / "dev" / b.mm_id / "log" / "000000000001.enc"
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
        target = ctx.replica_root / "dev" / ctx.device_id / "log" / "000000000001.enc"
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
            replace_piece("read", lambda ctx, **kw: Outcome("refused", reason="foreign-host"))
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
