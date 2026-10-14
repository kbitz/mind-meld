"""Checker clauses tested on independent histories, including legitimate edge cases."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from .invariants import (
    KNOWN_DESIGN_GAPS,
    InvariantError,
    Observation,
    Oracle,
    inv1_convergence,
    inv3_single_writer,
    inv4_ordering_never_fails,
    inv6_fork_safety,
    inv7_causal_fidelity,
    inv7_quiescent,
    normalized_lines,
    replica_integrity,
)
from .model import core
from .model.registry import (
    MODEL_PIECES,
    Adapter,
    Piece,
    StoreAdapter,
    default_registry,
    validate_registry,
)
from .model.state import FileState, State, StateSeam, row
from .model.store import Outcome
from .store_fleet import Clock
from .transport import File


@pytest.fixture
def observed_fleet(tmp_path):
    devices = []
    for name in ("a", "b", "c"):
        root = tmp_path / name
        (root / "sources").mkdir(parents=True)
        (root / "state").mkdir()
        devices.append(
            SimpleNamespace(
                name=name, mm_id=name * 8, source_root=root / "sources", state_root=root / "state"
            )
        )
    return SimpleNamespace(
        ordered=lambda: devices,
        participants=lambda: devices,
        devices={d.name: d for d in devices},
        clock=Clock(),
        steps=[],
        last_piece="classify:model",
        oracle=Oracle(),
        allow_corruption=False,
    )


def obs(**kwargs):
    value = Observation(
        "a", "pull", Outcome("applied"), State(), State(), {}, {}, {}, {}, [], set(), "read:model"
    )
    return replace(value, **kwargs)


def put(fleet, name, path, data):
    (fleet.devices[name].source_root / path).write_bytes(data)


@pytest.mark.parametrize("invariant", range(1, 8))
def test_invariant_error_contract(invariant):
    error = InvariantError(
        invariant,
        subject=("claude", "notes.txt", 1, "a"),
        evidence={"vv": {"aaaaaaaa": 1}},
        step=3,
        piece="read:real",
    )
    for expected in (
        f"INV{invariant}",
        "store-v2",
        "O",
        "subject=",
        "step=3",
        "vv",
        "expected/observed",
        "read:real",
        "next=",
        "KNOWN_DESIGN_GAPS",
    ):
        assert expected in str(error)


def test_normalization_is_lossless_and_independent():
    assert normalized_lines(b"\xff  \r\n\nalpha\n") == frozenset((b"\xff", b"alpha"))
    assert normalized_lines(b"a\nb\n") == normalized_lines(b"b\na\n")


def test_oracle_current_on_refused_retired_and_unjoined_devices(observed_fleet):
    f = observed_fleet
    f.oracle.add("claude", "notes.txt", b"x", "a")
    put(f, "c", "notes.txt", b"x")
    with FileState(f.devices["c"].state_root).transaction() as state:
        state.meta.update(joined=False, retired=True, refused="fork")
    f.oracle.check(f, quiescent=True)


def test_oracle_path_identity_and_missing_version(observed_fleet):
    f = observed_fleet
    f.oracle.add("claude", "notes.txt", b"x", "a")
    put(f, "a", "other.txt", b"x")
    with pytest.raises(InvariantError) as raised:
        f.oracle.check(f, quiescent=True)
    assert raised.value.invariant == 2
    assert "notes.txt" in str(raised.value)
    assert "last_holders" in str(raised.value)


def test_oracle_user_supersession_transitive_subsumption(observed_fleet):
    f = observed_fleet
    old = f.oracle.add("claude", "MEMORY.md", b"a\n", "a")
    middle = f.oracle.add("claude", "MEMORY.md", b"a\nb\n", "b", user=False)
    latest = f.oracle.add("claude", "MEMORY.md", b"c\n", "b")
    f.oracle.edges[old.event] = {middle.event}
    f.oracle.edges[middle.event] = {latest.event}
    put(f, "b", "MEMORY.md", b"c\n")
    f.oracle.check(f, quiescent=True)
    (f.devices["b"].source_root / "MEMORY.md").unlink()
    with pytest.raises(InvariantError):
        f.oracle.check(f, quiescent=True)


def test_oracle_resolution_is_not_user_supersession(observed_fleet):
    f = observed_fleet
    f.oracle.add("claude", "notes.txt", b"lost", "a")
    f.oracle.store_version(
        "claude", "notes.txt", b"winner", "a", {"aaaaaaaa": 2}, piece="classify:model"
    )
    put(f, "a", "notes.txt", b"winner")
    with pytest.raises(InvariantError):
        f.oracle.check(f, quiescent=True)


def test_oracle_displaced_expiry_is_path_bound(observed_fleet):
    f = observed_fleet
    version = core.entry("claude", "notes.txt", b"old", {"aaaaaaaa": 1})
    f.oracle.add("claude", "notes.txt", b"old", "a")
    with FileState(f.devices["b"].state_root).transaction() as state:
        state.displaced.append({**row(version), "time": 0, "expired": True, "content": None})
    f.oracle.check(f, quiescent=True)
    with FileState(f.devices["b"].state_root).transaction() as state:
        state.displaced[0]["path"] = "other.txt"
    with pytest.raises(InvariantError):
        f.oracle.check(f, quiescent=True)


def test_oracle_restore_last_copy_exclusion_is_narrow(observed_fleet):
    f = observed_fleet
    a = f.oracle.add("claude", "notes.txt", b"a", "a")
    b = f.oracle.add("claude", "notes.txt", b"b", "a")
    f.oracle.excluded.add(a.event)
    with pytest.raises(InvariantError) as raised:
        f.oracle.check(f, quiescent=True)
    assert raised.value.subject[2] == b.event


def test_oracle_user_delete_and_redelete_after_live_wins(observed_fleet):
    f = observed_fleet
    put(f, "a", "notes.txt", b"live")
    f.oracle.add("claude", "notes.txt", b"live", "a")
    f.oracle.user_action(f, f.devices["a"], "claude", "notes.txt", None)
    (f.devices["a"].source_root / "notes.txt").unlink()
    f.oracle.check(f, quiescent=True)
    put(f, "a", "notes.txt", b"live")
    f.oracle.user_action(f, f.devices["a"], "claude", "notes.txt", None)
    (f.devices["a"].source_root / "notes.txt").unlink()
    f.oracle.check(f, quiescent=True)


def test_single_writer_exceptions_and_temp_rejection(observed_fleet):
    f = observed_fleet
    file = File(b"x", 0, 0o600)
    for operation, path in (
        ("push", "dev/aaaaaaaa/log/000000000001.enc"),
        ("create_fleet", "fleet.json"),
        ("join", ".stignore"),
    ):
        inv3_single_writer(f, obs(operation=operation, replica_after={path: file}))
    inv3_single_writer(
        f, obs(operation="retire", replica_before={"dev/bbbbbbbb/x": file}), target="bbbbbbbb"
    )
    for path in ("dev/bbbbbbbb/x", "dev/aaaaaaaa/.mm-tmp-left"):
        with pytest.raises(InvariantError) as raised:
            inv3_single_writer(f, obs(replica_after={path: file}))
        assert raised.value.invariant == 3


def test_ordering_outcomes_ground_truth_and_warnings(observed_fleet):
    f = observed_fleet
    for outcome in (
        Outcome("applied"),
        Outcome("pending", peer="bbbbbbbb", gap_seq=2),
        Outcome("waiting", reason="fleet-header"),
    ):
        inv4_ordering_never_fails(
            f, obs(operation="join" if outcome.status == "waiting" else "pull", outcome=outcome)
        )
    inv4_ordering_never_fails(
        f, obs(outcome=Outcome("refused", reason="foreign-host"), ground_truth={"foreign-host"})
    )
    for outcome in (
        Outcome("error"),
        Outcome("refused", reason="foreign-host"),
        Outcome("stalled-unreadable"),
    ):
        with pytest.raises(InvariantError) as raised:
            inv4_ordering_never_fails(f, obs(outcome=outcome))
        assert raised.value.invariant == 4
    f.allow_corruption = True
    inv4_ordering_never_fails(f, obs(outcome=Outcome("stalled-unreadable", warnings=("visible",))))
    state = State(cursors={"bbbbbbbb": {"applied_seq": 0, "gap_since": f.clock.now - 86401}})
    with pytest.raises(InvariantError):
        inv4_ordering_never_fails(f, obs(state=state))
    inv4_ordering_never_fails(
        f, obs(state=state, outcome=Outcome("pending", warnings=("gap older than 24 hours",)))
    )


@pytest.mark.parametrize("warning", ["unreadable peer presence", "unreadable own commit"])
def test_aged_gap_rejects_unrelated_warning(observed_fleet, warning):
    f = observed_fleet
    state = State(cursors={"bbbbbbbb": {"applied_seq": 0, "gap_since": f.clock.now - 86401}})
    with pytest.raises(InvariantError) as raised:
        inv4_ordering_never_fails(
            f, obs(state=state, outcome=Outcome("pending", warnings=(warning,)))
        )
    assert raised.value.invariant == 4
    for gap_warning in ("gap older than 24 hours", "chain anchor gap older than 24 hours"):
        inv4_ordering_never_fails(
            f, obs(state=state, outcome=Outcome("pending", warnings=(warning, gap_warning)))
        )


@pytest.mark.parametrize("fork", ["previous", "known"])
@pytest.mark.parametrize("operation", ["push", "pull"])
def test_own_chain_refusal_ground_truth(store_fleet, fork, operation):
    from mind_meld.store import format as codec

    from .invariants import context_for
    from .model import store
    from .store_fleet import CONTENTS

    f = store_fleet
    a = f.devices["a"]
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    ctx = context_for(f, a)
    seq, prev = (2, "f" * 64) if fork == "previous" else (1, None)
    payload = codec.encode_commit(
        store.header(ctx, "commit", seq, prev, ()),
        (),
        expected=ctx.context(a.mm_id, "commit", seq),
    )
    if fork == "known":
        # Fault injection: independently valid alternate bytes for a known slot.
        for key, name in ctx.names().items():
            if (name.device_id, name.kind, name.seq) == (a.mm_id, "commit", seq):
                (a.replica_root / key).unlink()
    store.write_record(ctx, "commit", seq, payload)
    result = f.action("a", operation)
    assert (result.status, result.reason) == ("refused", "fork")
    observation = f.last_observation
    assert "fork" in observation.ground_truth
    with pytest.raises(InvariantError) as raised:
        inv6_fork_safety(f, replace(observation, outcome=Outcome("published")))
    assert raised.value.invariant == 6


@pytest.mark.parametrize("kind", ["model", "real"])
def test_source_writes_keep_piece_attribution(store_fleet, kind):
    from .invariants import settle
    from .model import store
    from .store_fleet import CONTENTS

    f = store_fleet
    a = f.devices["a"]
    original = f.registry["classify"]
    f.registry["classify"] = Piece(store.classify, kind, original.owner, "attribution fixture")
    for data in CONTENTS[1:3]:
        a.edit("notes.txt", data)
        f.action("a", "push", attended=False)
    f.drain()
    f.action("b", "pull")
    writes = [v for v in f.oracle.versions.values() if v.device == "b" and not v.authored]
    assert [v.data for v in writes] == list(CONTENTS[1:3])
    assert [v.piece for v in writes] == ["classify:" + kind] * 2
    assert f.last_piece == f.last_observation.piece == "classify:" + kind
    assert all(not v.user for v in writes)
    settle(f, min_participants=2)
    a.delete("notes.txt")
    f.action("a", "push", attended=False)
    f.drain()
    f.action("b", "pull")
    writes = [v for v in f.oracle.versions.values() if v.device == "b" and not v.authored]
    assert writes[-1].data is None
    assert writes[-1].piece == f.last_piece == "classify:" + kind
    settle(f, min_participants=2)


@pytest.mark.parametrize("writer", [None, "presence:model", "classify:real"])
def test_source_write_rejects_missing_or_false_piece(store_fleet, writer):
    from .model import store
    from .store_fleet import CONTENTS

    f = store_fleet

    def wrong_writer(ctx, **kwargs):
        result = store.classify(ctx, **kwargs)
        with ctx.state.transaction() as state:
            state.history[-1]["writer"] = writer
        return result

    f.devices["a"].edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    f.drain()
    original = f.registry["classify"]
    f.registry["classify"] = Piece(wrong_writer, "model", original.owner, "writer mutant")
    with pytest.raises(InvariantError, match="source writer") as raised:
        f.action("b", "pull")
    assert raised.value.invariant == 7


def test_fork_safety_does_not_vacuously_accept_any_refusal(observed_fleet):
    f = observed_fleet
    inv6_fork_safety(f, obs(ground_truth={"fork"}, outcome=Outcome("refused", reason="fork")))
    with pytest.raises(InvariantError) as raised:
        inv6_fork_safety(f, obs(ground_truth={"foreign-host"}))
    assert raised.value.invariant == 6


@pytest.mark.parametrize(
    "local_vv,remote_vv,same,expected",
    [
        ({"aaaaaaaa": 1}, {"aaaaaaaa": 2}, False, "fast-forward"),
        ({"aaaaaaaa": 2}, {"aaaaaaaa": 1}, False, "ignore"),
        ({"aaaaaaaa": 1}, {"bbbbbbbb": 1}, False, "resolve"),
        ({"aaaaaaaa": 1}, {"aaaaaaaa": 1}, False, "resolve"),
        ({"aaaaaaaa": 1}, {"bbbbbbbb": 1}, True, "same-sha"),
    ],
)
def test_causal_fidelity_and_equal_vector_clause(
    observed_fleet, local_vv, remote_vv, same, expected
):
    f = observed_fleet
    local = row(core.entry("claude", "notes.txt", b"a", local_vv))
    remote = row(core.entry("claude", "notes.txt", b"a" if same else b"b", remote_vv))
    record = {
        "operation": "pull-entry",
        "local": local,
        "incoming": remote,
        "result": (
            {
                **local,
                "vv": {
                    k: max(local_vv.get(k, 0), remote_vv.get(k, 0))
                    for k in sorted(local_vv.keys() | remote_vv.keys())
                },
            }
            if same
            else local
            if expected == "ignore"
            else remote
        ),
        "action": expected,
        "wrote": expected == "fast-forward",
        "bootstrap": False,
    }
    inv7_causal_fidelity(f, obs(history=[record]))
    invalid = [{**record, "action": "bad-action"}]
    if expected in ("same-sha", "ignore"):
        invalid.append({**record, "wrote": True})
    if expected == "fast-forward":
        invalid.append({**record, "result": {**record["result"], "sha": local["sha"]}})
    if expected == "ignore":
        invalid.append({**record, "result": {**record["result"], "sha": remote["sha"]}})
    if expected == "same-sha":
        invalid.extend(
            [
                {**record, "result": {**record["result"], "vv": local_vv}},
                {**record, "result": {**record["result"], "sha": "0" * 64}},
            ]
        )
    for bad in invalid:
        with pytest.raises(InvariantError) as raised:
            inv7_causal_fidelity(f, obs(history=[bad]))
        assert raised.value.invariant == 7


def test_bootstrap_tombstone_and_quiescent_causal_target(observed_fleet):
    f = observed_fleet
    tombstone = row(core.entry("claude", "notes.txt", None, {"bbbbbbbb": 1}))
    local = row(core.entry("claude", "notes.txt", b"old", {"aaaaaaaa": 1}))
    history = {
        "operation": "pull-entry",
        "local": local,
        "incoming": tombstone,
        "result": tombstone,
        "action": "bootstrap-delete",
        "wrote": True,
        "bootstrap": True,
    }
    inv7_causal_fidelity(f, obs(history=[history]))
    f.oracle.add("claude", "notes.txt", b"latest", "a", vv={"aaaaaaaa": 2})
    for device in f.ordered():
        put(f, device.name, "notes.txt", b"latest")
    inv7_quiescent(f)
    put(f, "c", "notes.txt", b"old")
    with pytest.raises(InvariantError) as raised:
        inv7_quiescent(f)
    assert raised.value.invariant == 7


@pytest.mark.parametrize("difference", ["file", "ledger-sha"])
def test_convergence_compares_files_and_ledger(observed_fleet, difference):
    # Value: protects=files and ledger SHA convergence; fails_when=checker omits
    # either view; why_new=existing test changes only files; seam=hand observation.
    from .model.state import path_key

    f = observed_fleet
    key = path_key("claude", "notes.txt")
    common = core.entry("claude", "notes.txt", b"same", {"aaaaaaaa": 1})
    for device in f.ordered():
        put(f, device.name, "notes.txt", b"same")
        with FileState(device.state_root).transaction() as state:
            state.ledger[key] = row(common)
    inv1_convergence(f)
    if difference == "file":
        put(f, "b", "notes.txt", b"different")
    else:
        with FileState(f.devices["b"].state_root).transaction() as state:
            state.ledger[key]["sha"] = core.entry(
                "claude", "notes.txt", b"different", {"aaaaaaaa": 1}
            ).metadata["sha"]
    with pytest.raises(InvariantError) as raised:
        inv1_convergence(f)
    assert raised.value.invariant == 1


def test_state_transaction_rolls_back_on_failure(tmp_path):
    state = FileState(tmp_path)
    assert isinstance(state, StateSeam)
    with pytest.raises(RuntimeError):
        with state.transaction() as value:
            value.ledger["test"] = {"sha": "not-committed"}
            value.own_log["1"] = {"hash": "not-committed"}
            value.history.append({"not": "committed"})
            raise RuntimeError("crash")
    assert state.read() == State()
    with state.transaction() as value:
        value.ledger["test"] = {"content": b"\xff", "pending": True}
        value.own_log["1"] = {"hash": "committed"}
        value.history.append({"committed": True})
    assert FileState(tmp_path).read().ledger["test"]["content"] == b"\xff"
    assert isinstance(Adapter(None), StoreAdapter)


# Collection-time contract failure is deliberate: no malformed registry can run.
validate_registry(default_registry())


def test_registry_rejects_miswired_piece_and_real_executes(store_fleet):
    registry = default_registry()
    registry["publish"] = Piece(lambda: None, "real", "75A", "contract test")
    with pytest.raises(TypeError, match="publish"):
        validate_registry(registry)
    called = []

    def independent_real(context, **kwargs):
        called.append(context.name)
        return Outcome("unchanged")

    store_fleet.registry["publish"] = Piece(independent_real, "real", "75A", "contract test")
    store_fleet.action("a", "push")
    assert called == ["a"]
    assert store_fleet.last_observation.piece == "publish:real"
    assert set(MODEL_PIECES) == set(store_fleet.registry)


def test_plaintext_and_invalid_envelope_mutants(store_fleet):
    d = store_fleet.devices["a"]
    for path, data in [
        ("foreign", b"plaintext"),
        ("dev/" + d.mm_id + "/log/000000000001.enc", b"\x03bad"),
    ]:
        with pytest.raises(InvariantError) as raised:
            replica_integrity(store_fleet, d, {path: File(data, 0, 0o600)})
        assert raised.value.invariant == 3
    marker = b"sim-marker-alpha-0001"
    with pytest.raises(InvariantError, match="plaintext marker"):
        replica_integrity(store_fleet, d, {"bad": File(marker, 0, 0o600)})


def test_known_gap_rows_have_replay_and_owner():
    for name, entry in sorted(KNOWN_DESIGN_GAPS.items()):
        assert set(entry) == {"section", "outcome", "tracks", "decision", "node", "gates"}
        assert name and all(entry.values())


@pytest.mark.parametrize("piece", MODEL_PIECES)
def test_each_real_registry_piece_dispatches_without_model_fallback(tmp_path, piece):
    from .model.store import Context
    from .store_fleet import Clock

    calls = []
    registry = default_registry()

    def real(context, **kwargs):
        calls.append((context.name, kwargs))
        return "sentinel"

    registry[piece] = Piece(real, "real", registry[piece].owner, "dispatch contract")
    validate_registry(registry)
    ctx = Context(
        "a",
        "aaaaaaaa",
        FileState(tmp_path / "state"),
        tmp_path / "replica",
        {"claude": tmp_path / "sources"},
        Clock(),
        lambda: "hardware-a",
        None,
        registry,
    )
    assert ctx.call(piece, fixture_argument=1) == "sentinel"
    assert calls == [("a", {"fixture_argument": 1})]
    assert ctx.executed == [piece]


def test_hash_seed_and_crypto_randomness_do_not_steer_trace(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
import json, os, random, sys
from pathlib import Path
rng = random.Random(int(os.environ['SIM_RANDOM']))
os.urandom = lambda n: bytes(rng.getrandbits(8) for _ in range(n))
from mind_meld import fsutil
from mind_meld.store import format as codec
from tests.sim.store_fleet import StoreFleet, PART_BYTES, CONTENTS
from tests.sim.invariants import settle
root = Path(os.environ['HOME']).resolve() / 'example'
codec.MAX_PART_BYTES = PART_BYTES
guard = lambda: fsutil.refuse_real_state_write_under_pytest(root / 'probe')
f = StoreFleet(root, write_guard=guard).bootstrap()
assert 'mind_meld.cli' not in sys.modules
for name, data in [('a', CONTENTS[1]), ('b', CONTENTS[2])]:
    f.devices[name].edit('MEMORY.md', data)
    f.action(name, 'push')
f.drain()
settle(f, min_participants=2)
from hypothesis import settings
from hypothesis.stateful import run_state_machine_as_test
from tests.sim.test_replication import CoreMachine, Invocation, ACTIVE
traces = []
class Traced(CoreMachine):
    def teardown(self):
        super().teardown()
        traces.append({'steps': self.fleet.steps, 'transport': list(self.fleet.transport.trace)})
token = ACTIVE.set(Invocation(root / 'hypothesis', guard))
try:
    run_state_machine_as_test(Traced, settings=settings(
        parent=settings.get_profile('sim-bounded'), max_examples=1, stateful_step_count=2))
finally:
    ACTIVE.reset(token)
print(json.dumps({'program': f.steps, 'machine': traces}, sort_keys=True))
"""
    traces = []
    for seed in (1, 917):
        home = tmp_path / str(seed)
        home.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "PYTHONHASHSEED": str(seed),
            "SIM_RANDOM": str(seed),
            "PYTEST_CURRENT_TEST": "v2-determinism-subprocess",
        }
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        traces.append(json.loads(result.stdout))
    assert traces[0] == traces[1]


def test_causal_fidelity_excludes_unpublished_offfleet_edits_but_oracle_keeps_them(observed_fleet):
    f = observed_fleet
    f.participants = lambda: f.ordered()[:2]
    f.oracle.add(
        "claude", "notes.txt", b"fleet", "a", user=False, authored=True, vv={"aaaaaaaa": 1}
    )
    f.oracle.add("claude", "notes.txt", b"offfleet", "c", vv={"aaaaaaaa": 2})
    for name in ("a", "b"):
        put(f, name, "notes.txt", b"fleet")
    put(f, "c", "notes.txt", b"offfleet")
    inv7_quiescent(f)
    f.oracle.check(f, quiescent=True)


def test_saved_restore_record_witness_is_transient(store_fleet):
    from mind_meld.store import layout

    from .invariants import InvariantError
    from .store_fleet import CONTENTS

    f = store_fleet
    a = f.devices["a"]
    backup = f.snapshot(a)
    a.edit("notes.txt", CONTENTS[1])
    f.action("a", "push", attended=False)
    for choice in f.transport.pending():
        if choice.dst == "b" and "/log/" in choice.path:
            f.transport.deliver(choice)
    f.restore(a, backup, index_policy="saved")
    assert not f.oracle.excluded
    f.oracle.check(f)
    with pytest.raises(InvariantError):
        f.oracle.check(f, quiescent=True)
    for target in f.devices["b"].replica_root.rglob("*.enc"):
        parsed = layout.parse_record_key(target.relative_to(f.devices["b"].replica_root).as_posix())
        if parsed and parsed.kind == "commit":
            target.unlink()
    with pytest.raises(InvariantError):
        f.oracle.check(f)
    # Restore the saved transport witness through normal eventual delivery.
    f.transport.scan("b")
    # This counterexample is the saved restore gap; its complete strict pin is below.


def test_known_gap_rows_match_strict_xfail_tests_one_to_one():
    from . import test_scenarios

    strict = {}
    for name in sorted(vars(test_scenarios)):
        value = getattr(test_scenarios, name)
        for mark in getattr(value, "pytestmark", []):
            if mark.name == "xfail":
                assert mark.kwargs.get("strict") is True
                assert mark.kwargs.get("raises") is test_scenarios.ExpectedDesignGap
                gap = mark.kwargs["reason"].split(":", 1)[0]
                assert gap not in strict
                strict[gap] = "test_scenarios.py::" + name
    assert strict == {gap: row["node"] for gap, row in KNOWN_DESIGN_GAPS.items()}


def test_deep_profile_keeps_default_database_in_ci_environment(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
from pathlib import Path
import subprocess
import sys
from hypothesis import settings
from tests.sim.test_replication import selected_settings
bounded = settings.get_profile('sim-bounded')
deep = settings.get_profile('sim-deep')
assert bounded.derandomize and bounded.database is None
assert not deep.derandomize and deep.database is not None
assert (deep.max_examples, deep.stateful_step_count) == (1000, 100)
assert settings.default is settings.get_profile('ci')
example = deep.database.path.resolve().relative_to(Path.cwd().resolve()) / 'retained-example'
ignored = subprocess.run(
    ['git', 'check-ignore', '--verbose', '--no-index', '--', str(example)],
    capture_output=True, text=True, check=True, timeout=10)
source_rule, ignored_path = ignored.stdout.rstrip('\n').split('\t', 1)
source, _line, _pattern = source_rule.rsplit(':', 2)
assert ignored_path == str(example)
assert Path(source).resolve() == Path(sys.argv[1]).resolve(), ignored.stdout
print('deep database retained; ambient ci unchanged')
"""
    env = {
        **os.environ,
        "CI": "1",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
    }
    env.pop("HYPOTHESIS_STORAGE_DIRECTORY", None)
    ignore_file = (Path(__file__).parent / os.path.join("..", "..", ".gitignore")).resolve()
    result = subprocess.run(
        [sys.executable, "-c", script, str(ignore_file)],
        cwd=Path(__file__).parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "deep database retained; ambient ci unchanged"


@pytest.mark.parametrize("age", [30 * 86400 - 1, 30 * 86400, 30 * 86400 + 1])
def test_oracle_requires_retention_age_for_expired_witness(observed_fleet, age):
    # Value: protects=30-day preservation; fails_when=expired flag bypasses age;
    # why_new=existing oracle test checks path only; seam=existing observed fleet.
    f = observed_fleet
    f.oracle.add("claude", "notes.txt", b"old", "a")
    version = core.entry("claude", "notes.txt", b"old", {"aaaaaaaa": 1})
    with FileState(f.devices["b"].state_root).transaction() as state:
        state.displaced.append(
            {
                **row(version),
                "time": f.clock.now - age,
                "expired": True,
                "content": None,
            }
        )
    if age < 30 * 86400:
        with pytest.raises(InvariantError) as raised:
            f.oracle.check(f, quiescent=True)
        assert raised.value.invariant == 2
    else:
        f.oracle.check(f, quiescent=True)


@pytest.mark.parametrize(
    "implementation",
    [
        pytest.param(lambda context, required, **kwargs: None, id="required-positional"),
        pytest.param(lambda context, *, required, **kwargs: None, id="required-keyword-only"),
        pytest.param(lambda context, probe: None, id="no-arbitrary-keywords"),
    ],
)
def test_registry_rejects_incomplete_operation_signature(implementation):
    # Value: protects=collection-time piece contract; fails_when=bind_partial
    # accepts an unusable signature; why_new=existing test covers no context only;
    # seam=existing registry validation.
    registry = default_registry()
    registry["publish"] = Piece(implementation, "real", "75A", "signature contract")
    with pytest.raises(TypeError, match="publish"):
        validate_registry(registry)


@pytest.mark.parametrize("operation", ["pull", "push", "publish_snapshot", "compact", "join"])
def test_unreadable_requires_corruption_scenario(observed_fleet, operation):
    f = observed_fleet
    observation = obs(
        operation=operation, outcome=Outcome("stalled-unreadable", warnings=("unreadable record",))
    )
    with pytest.raises(InvariantError) as raised:
        inv4_ordering_never_fails(f, observation)
    assert raised.value.invariant == 4
    f.allow_corruption = True
    inv4_ordering_never_fails(f, observation)


@pytest.mark.parametrize(
    "name,kwargs,unexpected",
    [
        ("test_gap_non_utf8_union", {"path": "MEMORY.md"}, 3),
        ("test_gap_restore_saved", {}, 4),
        ("test_gap_restore_current", {}, 6),
        ("test_gap_partial_own_commit", {}, 7),
        ("test_gap_retire_delayed_active_presence", {}, 4),
    ],
)
def test_gap_marker_rejects_earlier_unrelated_invariant(
    store_fleet, monkeypatch, name, kwargs, unexpected
):
    from . import test_scenarios

    scenario = getattr(test_scenarios, name)
    marker = next(mark for mark in scenario.pytestmark if mark.name == "xfail")

    def fail_early(*args, **kw):
        raise InvariantError(unexpected, subject="injected early failure", evidence={})

    monkeypatch.setattr(store_fleet, "action", fail_early)
    with pytest.raises(AssertionError) as raised:
        scenario(store_fleet, **kwargs)
    assert not isinstance(raised.value, marker.kwargs["raises"])


def test_absent_delete_does_not_suppress_quiescent_causal_check(observed_fleet):
    f = observed_fleet
    f.oracle.add("claude", "notes.txt", b"peer", "b", vv={"bbbbbbbb": 1})
    put(f, "b", "notes.txt", b"peer")
    f.oracle.user_action(f, f.devices["a"], "claude", "notes.txt", None)
    with pytest.raises(InvariantError) as raised:
        inv7_quiescent(f)
    assert raised.value.invariant == 7
    assert len(f.oracle.versions) == 1


@pytest.mark.parametrize("known_anchor", [True, False])
def test_fork_refusal_uses_refusal_cursor_and_known_anchor(store_fleet, known_anchor):
    from mind_meld.store import format as codec

    from .invariants import context_for
    from .model import store
    from .transport import read_tree

    f = store_fleet
    a, b = f.devices["a"], f.devices["b"]
    a.edit("notes.txt", b"one")
    f.action("a", "push", attended=False)
    digest = FileState(a.state_root).read().own_log["1"]["hash"]
    ctx = context_for(f, a)
    payload = codec.encode_commit(
        store.header(ctx, "commit", 2, "f" * 64, ()), (), expected=ctx.context(a.mm_id, "commit", 2)
    )
    store.write_record(ctx, "commit", 2, payload)
    f.transport.scan("a")
    f.drain()
    before = State(cursors={a.mm_id: {"applied_seq": 0 if known_anchor else 1, "hash": None}})
    after = State(cursors={a.mm_id: {"applied_seq": 1, "hash": digest if known_anchor else None}})
    observation = obs(
        device="b",
        before_state=before,
        state=after,
        replica_before=read_tree(b.replica_root),
        outcome=Outcome("refused", peer=a.mm_id, reason="fork"),
    )
    if known_anchor:
        inv4_ordering_never_fails(f, observation)
    else:
        with pytest.raises(InvariantError) as raised:
            inv4_ordering_never_fails(f, observation)
        assert raised.value.invariant == 4


@pytest.mark.parametrize("family", ["complete", "partial", "corrupt"])
def test_authenticated_conflict_copy_is_only_a_transient_record_witness(store_fleet, family):
    f = store_fleet
    a = f.devices["a"]
    for path in ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl"):
        a.edit(path, b"recoverable" * 32)
    f.action("a", "push", attended=False)
    parts = list(a.replica_root.glob(f"dev/{a.mm_id}/log/*.enc"))
    assert len(parts) > 1
    copies = []
    for part in parts:
        copy = part.with_name(part.stem + ".sync-conflict-20261014-120000-a.enc")
        part.rename(copy)
        copies.append(copy)
    for path in a.source_root.iterdir():
        path.unlink()
    if family == "partial":
        copies[0].unlink()
    elif family == "corrupt":
        data = copies[0].read_bytes()
        copies[0].write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    if family == "complete":
        f.oracle.check(f)
    else:
        with pytest.raises(InvariantError) as raised:
            f.oracle.check(f)
        assert raised.value.invariant == 2
    with pytest.raises(InvariantError) as raised:
        f.oracle.check(f, quiescent=True)
    assert raised.value.invariant == 2


def test_empty_compaction_cannot_claim_displaced_expiry(store_fleet):
    f = store_fleet
    f.advance(30 * 86400)
    f.action("a", "compact")
    assert "displaced-expiry" not in f.coverage


def test_rejoin_rejects_an_existing_identity_before_dispatch(store_fleet):
    f = store_fleet
    before = FileState(f.devices["a"].state_root).read()
    with pytest.raises(ValueError, match="fresh identity"):
        f.action("a", "rejoin", new_id=f.devices["b"].mm_id)
    assert FileState(f.devices["a"].state_root).read() == before


@pytest.mark.parametrize("publication", ["edit", "pending-resolution"])
def test_publisher_cannot_reuse_previous_vector(store_fleet, monkeypatch, publication):
    # Value: protects=E12 publish owns exactly one local causal increment;
    # fails_when=publisher retains the prior vector in an authenticated record;
    # why_new=pull input mutants do not corrupt authoring; seam=piece registry;
    # no_claim=real publisher, SQLite or writer authentication.
    from dataclasses import replace

    from .invariants import settle
    from .model import store
    from .model.state import path_key
    from .store_fleet import CONTENTS

    f = store_fleet
    a = f.devices["a"]
    if publication == "edit":
        a.edit("notes.txt", CONTENTS[1])
        settle(f, min_participants=2)
        f.advance(1)
        a.edit("notes.txt", CONTENTS[2])
    else:
        for name, data in (("a", CONTENTS[1]), ("b", CONTENTS[2])):
            f.devices[name].edit("notes.txt", data)
            f.action(name, "push", attended=False)
        f.drain()
        f.action("a", "pull")
        assert FileState(a.state_root).read().ledger[path_key("claude", "notes.txt")]["pending"]
    original_author = core.author

    def missing_increment(source, path, ledger, data, mtime, device, **kwargs):
        authored = original_author(source, path, ledger, data, mtime, device, **kwargs)
        if authored is not None and ledger is not None:
            return replace(
                authored, metadata={**authored.metadata, "vv": dict(ledger.metadata["vv"])}
            )
        return authored

    def bad_publish(ctx, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(core, "author", missing_increment)
            return store.publish(ctx, **kwargs)

    old = f.registry["publish"]
    f.registry["publish"] = Piece(bad_publish, "real", old.owner, "publication causal mutant")
    with pytest.raises(InvariantError) as raised:
        f.action("a", "push", attended=False)
    assert raised.value.invariant == 7


def test_identical_edit_cannot_hide_later_dominating_version(observed_fleet):
    # Value: protects=INV7 rejects stale files after a genuine dominating edit;
    # fails_when=an unchanged rewrite invents a concurrent causal version;
    # why_new=absent-delete regression does not cover identical live bytes;
    # seam=independent hand-built files, ledger and oracle observations.
    from .model.state import path_key

    f = observed_fleet
    a, b = f.devices["a"], f.devices["b"]
    first = core.entry("claude", "notes.txt", b"alpha", {a.mm_id: 1})
    f.oracle.add("claude", "notes.txt", b"alpha", "a", vv={a.mm_id: 1})
    for device in f.ordered():
        put(f, device.name, "notes.txt", b"alpha")
        with FileState(device.state_root).transaction() as state:
            state.ledger[path_key("claude", "notes.txt")] = row(first)
    f.oracle.user_action(f, b, "claude", "notes.txt", b"alpha")
    f.oracle.add("claude", "notes.txt", b"beta", "a", vv={a.mm_id: 2})
    with pytest.raises(InvariantError) as raised:
        inv7_quiescent(f)
    assert raised.value.invariant == 7
    # The same history passes once every participant actually holds beta.
    for device in f.ordered():
        put(f, device.name, "notes.txt", b"beta")
    inv7_quiescent(f)
