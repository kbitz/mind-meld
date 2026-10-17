"""Bounded/deep executable protocol search, with per-invocation reachability."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

import hypothesis
import pytest
from hypothesis import HealthCheck, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from mind_meld.store import format as codec

from .invariants import KNOWN_DESIGN_GAPS, settle
from .model.registry import default_registry
from .store_fleet import CONTENTS, DAY, MAX_IDS, PART_BYTES, PATHS, StoreFleet

settings.register_profile(
    "sim-bounded",
    parent=settings.get_profile("ci"),
    max_examples=50,
    stateful_step_count=40,
    derandomize=True,
    print_blob=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "sim-deep",
    parent=settings.get_profile("default"),
    max_examples=1000,
    stateful_step_count=100,
    derandomize=False,
    print_blob=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

CORE_HARD = {
    "concurrent-newest",
    "concurrent-jsonl",
    "concurrent-lines",
    "gap-pending",
    "partial-multipart",
    "snapshot-fallback",
    "displaced-expiry",
}


@dataclass
class Invocation:
    root: Path
    guard: object
    examples: int = 0
    steps: int = 0
    coverage: set[str] = field(default_factory=set)
    quiescent: int = 0
    remedies: int = 0  # Examples whose teardown rejoined at least one refused device.
    settle_seconds: list[float] = field(default_factory=list)
    action_seconds: list[float] = field(default_factory=list)


ACTIVE: ContextVar[Invocation] = ContextVar("sim_invocation")


def selected_settings(*, lifecycle=False):
    value = os.environ.get("MM_SIM_DEEP", "0")
    if value not in ("0", "1"):
        pytest.fail(
            "MM_SIM_DEEP must be unset, 0 or 1; use MM_SIM_DEEP=1 for deep search", pytrace=False
        )
    profile = "sim-deep" if value == "1" else "sim-bounded"
    selected = settings.get_profile(profile)
    if lifecycle and profile == "sim-bounded":
        selected = settings(parent=selected, max_examples=30, stateful_step_count=50)
    return profile, selected


class CoreMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.invocation = ACTIVE.get()
        self.invocation.examples += 1
        root = self.invocation.root / str(self.invocation.examples)
        self.fleet = StoreFleet(root, write_guard=self.invocation.guard).bootstrap()

    def device(self, index):
        return self.fleet.ordered()[index % len(self.fleet.devices)]

    def tick(self):
        self.invocation.steps += 1
        self.fleet.oracle_checked = False

    @initialize(case=st.integers(0, 4))
    def core_witness(self, case):
        # Hypothesis draws the witness, so replay is independent of invocation count.
        if case < 3:
            self.concurrent(PATHS[(0, 2, 3)[case]])
        elif case == 3:
            self.gap_witness()
        else:
            self.snapshot_witness()

    def concurrent(self, path):
        settle(self.fleet)
        for name, data in (("a", CONTENTS[1]), ("b", CONTENTS[2])):
            self.fleet.devices[name].edit(path, data)
            self.fleet.action(name, "push")
        self.fleet.drain()
        for name in ("a", "b"):
            self.fleet.action(name, "pull")
        settle(self.fleet)

    def gap_witness(self):
        a = self.fleet.devices["a"]
        for path in PATHS:
            a.edit(path, CONTENTS[3])
        self.fleet.action("a", "push")
        choices = [c for c in self.fleet.transport.pending() if c.dst == "b" and "/log/" in c.path]
        assert len(choices) >= 2
        self.fleet.transport.deliver(choices[0])
        assert self.fleet.action("b", "pull").status == "pending"
        self.fleet.coverage.add("partial-multipart")
        settle(self.fleet)

    def snapshot_witness(self):
        self.fleet.devices["a"].edit("notes.txt", CONTENTS[0])
        settle(self.fleet)
        for content in (CONTENTS[1], CONTENTS[2], CONTENTS[3]):
            self.fleet.devices["a"].edit("notes.txt", content)
            self.fleet.action("a", "publish_snapshot")
            self.fleet.advance(14 * DAY)
        self.fleet.action("a", "compact")
        self.fleet.drain()
        self.fleet.action("b", "pull")
        settle(self.fleet)
        self.fleet.advance(30 * DAY)
        self.fleet.action("b", "compact")

    @rule(
        device=st.integers(0, 3),
        path=st.integers(0, 3),
        content=st.integers(0, 7),
        old_mtime=st.booleans(),
    )
    def edit(self, device, path, content, old_mtime):
        self.tick()
        if path in (2, 3) and content == 7 and "non-utf8-union" in KNOWN_DESIGN_GAPS:
            return  # Named design gap; the strict regression retains the unmodified oracle.
        self.device(device).edit(
            PATHS[path], CONTENTS[content], mtime=self.fleet.clock.now - int(old_mtime)
        )

    @rule(device=st.integers(0, 3), path=st.integers(0, 3))
    def delete(self, device, path):
        self.tick()
        self.device(device).delete(PATHS[path])

    @rule(
        device=st.integers(0, 3),
        operation=st.sampled_from(["push", "pull", "publish_snapshot", "compact"]),
    )
    def store(self, device, operation):
        self.tick()
        selected = self.device(device)
        if selected in self.fleet.participants():
            self.fleet.action(selected.name, operation)

    @rule(choice=st.integers(0, 1000), delay=st.integers(0, 3))
    def deliver(self, choice, delay):
        self.tick()
        pending = self.fleet.transport.pending()
        if pending:
            item = pending[choice % len(pending)]
            if delay:
                self.fleet.transport.delay(item, delay)
            elif not item.reasons:
                self.fleet.transport.deliver(item)

    @rule(device=st.integers(0, 3), online=st.booleans(), seconds=st.integers(0, 4))
    def scheduler(self, device, online, seconds):
        self.tick()
        self.fleet.transport.set_online(self.device(device).name, online)
        self.fleet.transport.advance(seconds)

    @rule(choice=st.integers(0, 1000), finish=st.booleans())
    def replace(self, choice, finish):
        self.tick()
        tokens = [t for d in self.fleet.ordered() for t in self.fleet.transport.unfinished(d.name)]
        if tokens and finish:
            self.fleet.transport.finish_replace(tokens[choice % len(tokens)])
        elif not tokens:
            ready = [c for c in self.fleet.transport.pending() if not c.reasons]
            if ready:
                try:
                    self.fleet.transport.begin_replace(ready[choice % len(ready)])
                except ValueError:  # Choice loses canonical path; explicit transport contract.
                    pass

    @rule(seconds=st.sampled_from([1, 3599, 3600, DAY, 14 * DAY, 30 * DAY, 31 * DAY]))
    def time_advance(self, seconds):
        self.tick()
        self.fleet.advance(seconds)

    @rule()
    def settle_rule(self):
        self.tick()
        self.invocation.settle_seconds.append(settle(self.fleet, min_participants=2))

    @invariant()
    def observations_hold(self):
        if not getattr(self.fleet, "oracle_checked", False):
            self.fleet.oracle.check(self.fleet)
        if hypothesis.control.currently_in_test_context():
            note(f"step={self.fleet.steps[-1]}; participants={len(self.fleet.participants())}")

    def teardown(self):
        try:
            if sys.exc_info()[0] is None:
                self.invocation.settle_seconds.append(settle(self.fleet, min_participants=2))
                self.invocation.quiescent += 1
                self.invocation.coverage.update(self.fleet.coverage)
                self.invocation.action_seconds.extend(self.fleet.action_seconds)
        finally:
            if hypothesis.control.currently_in_test_context():
                note(self.fleet.diagnostics())
            shutil.rmtree(self.fleet.root)


def run_machine(
    machine, tmp_path_factory, sim_write_guard, monkeypatch, request, *, lifecycle=False
):
    profile, selected = selected_settings(lifecycle=lifecycle)
    invocation = Invocation(tmp_path_factory.mktemp("store-machine"), sim_write_guard)
    token = ACTIVE.set(invocation)
    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    started = time.perf_counter()
    try:
        run_state_machine_as_test(machine, settings=selected)
        required = CORE_HARD | (LIFECYCLE_HARD if lifecycle else set())
        missed = required - invocation.coverage
        if missed:
            pytest.fail(
                f"REACHABILITY missing {sorted(missed)}; add macro rules, initialize witnesses, "
                "day jumps or deterministic witnesses",
                pytrace=False,
            )
        if lifecycle and invocation.quiescent < invocation.examples / 2:
            pytest.fail(
                "REACHABILITY fewer than 50% of lifecycle examples quiesced with 2+ participants"
            )
    finally:
        ACTIVE.reset(token)
        duration = time.perf_counter() - started
        summary = {
            "hypothesis": hypothesis.__version__,
            "profile": profile,
            "configured_examples": selected.max_examples,
            "configured_steps": selected.stateful_step_count,
            "executed_examples": invocation.examples,
            "executed_steps": invocation.steps,
            "wall_seconds": duration,
            "local_ceiling_seconds": 60,
            "quiescent_examples": invocation.quiescent,
            "teardown_remedy_examples": invocation.remedies,
            "coverage": sorted(invocation.coverage),
            "step_seconds": sum(invocation.action_seconds) / max(1, len(invocation.action_seconds)),
            "settle_seconds": sum(invocation.settle_seconds)
            / max(1, len(invocation.settle_seconds)),
            "pieces": {k: v.kind for k, v in sorted(default_registry().items())},
            "gaps": KNOWN_DESIGN_GAPS,
        }
        request.node.user_properties.append(("sim_summary", json.dumps(summary, sort_keys=True)))
        request.node.add_report_section(
            "call", "store simulator", json.dumps(summary, sort_keys=True)
        )


def test_core_replication(tmp_path_factory, sim_write_guard, monkeypatch, request):
    run_machine(CoreMachine, tmp_path_factory, sim_write_guard, monkeypatch, request)


LIFECYCLE_HARD = {
    "bootstrap-inversion",
    "join-snapshot",
    "retire",
    "fork-refusal",
    "crash-recovery",
    "torn-supersede",
    "late-family",
    "heal-snapshot",
}


class LifecycleMachine(CoreMachine):
    @initialize(case=st.integers(0, 3))
    def lifecycle_witness(self, case):
        if case == 0:
            c = self.fleet.add_device("c")
            c.edit("other.txt", CONTENTS[2])
            self.fleet.devices["a"].edit("other.txt", CONTENTS[1])
            settle(self.fleet)
            self.fleet.devices["a"].delete("other.txt")
            settle(self.fleet)
            self.fleet.advance(1)
            self.fleet.action("a", "publish_snapshot")
            self.fleet.drain()
            self.fleet.action("c", "join")
        elif case == 1:
            c = self.fleet.clone(self.fleet.devices["a"], "c")
            assert self.fleet.action("c", "push").status == "refused"
            self.fleet.coverage.add("fork-refusal")
            self.fleet.rejoin(c)
        elif case == 2:
            c = self.fleet.add_device("c")
            self.fleet.drain()
            self.fleet.action("c", "join")
            c.edit("other.txt", CONTENTS[1])
            settle(self.fleet)
            self.fleet.advance(31 * DAY)
            for name in ("a", "b"):
                self.fleet.devices[name].edit("notes.txt", CONTENTS[2])
                self.fleet.action(name, "publish_snapshot")
            self.fleet.drain()
            assert self.fleet.action("a", "retire", target=c.mm_id).status == "retired"
        elif case == 3:
            self.fleet.devices["a"].edit("notes.txt", CONTENTS[1])
            self.fleet.action("a", "push", crash_state=True, attended=False)
            self.fleet.action("a", "push")
        settle(self.fleet, min_participants=2)
        self.backups = {}

    @rule(choice=st.integers(0, 3))
    def join_rule(self, choice):
        self.tick()
        if len(self.fleet.devices) < 4 and len(self.fleet.ids) < MAX_IDS:
            name = next(n for n in ("c", "d") if n not in self.fleet.devices)
            self.fleet.add_device(name)
        unjoined = [
            d
            for d in self.fleet.ordered()
            if not self.fleet.state_factory(d.state_root).read().meta.get("joined")
        ]
        if unjoined:
            d = unjoined[choice % len(unjoined)]
            if self.fleet.keys(d) is not None:
                self.fleet.action(d.name, "join")

    @rule(device=st.integers(0, 3))
    def clone_rule(self, device):
        self.tick()
        d = self.device(device)
        if (
            len(self.fleet.devices) < 4
            and d in self.fleet.participants()
            and not self.fleet.transport.unfinished(d.name)
        ):
            name = next(n for n in ("c", "d") if n not in self.fleet.devices)
            self.fleet.clone(d, name)
            assert self.fleet.action(name, "push").status == "refused"
            self.fleet.coverage.add("fork-refusal")

    @rule(device=st.integers(0, 3))
    def rejoin_rule(self, device):
        self.tick()
        d = self.device(device)
        meta = self.fleet.state_factory(d.state_root).read().meta
        if (
            len(self.fleet.ids) < MAX_IDS
            and (meta.get("refused") or meta.get("retired"))
            and not self.fleet.transport.unfinished(d.name)
        ):
            self.fleet.rejoin(d)

    @rule(device=st.integers(0, 3))
    def backup_rule(self, device):
        self.tick()
        d = self.device(device)
        if not self.fleet.transport.unfinished(d.name):
            self.backups[d.name] = self.fleet.snapshot(d)

    @rule(
        device=st.integers(0, 3),
        policy=st.sampled_from(["saved", "current", "state", "fresh-replica"]),
    )
    def restore_rule(self, device, policy):
        self.tick()
        d = self.device(device)
        if "restore-" + policy in KNOWN_DESIGN_GAPS:
            return  # Exact divergent histories remain in strict deterministic regressions.
        if policy == "fresh-replica" and not any(
            (other.replica_root / "fleet.json").exists()
            for other in self.fleet.ordered()
            if other is not d
        ):
            return  # Peers refill an emptied replica; with no surviving copy there is no store.
        if policy == "fresh-replica":
            # KNOWN_DESIGN_GAPS[fresh-replica-unreplicated]: emptying the replica
            # destroys own records no peer has synced yet.
            own = (
                {
                    p.relative_to(d.replica_root).as_posix()
                    for p in (d.replica_root / "dev" / d.mm_id).rglob("*.enc")
                }
                if (d.replica_root / "dev" / d.mm_id).exists()
                else set()
            )
            peer_held = set()
            for other in self.fleet.ordered():
                if other is not d:
                    peer_held |= {
                        p.relative_to(other.replica_root).as_posix()
                        for p in other.replica_root.rglob("*.enc")
                    }
            if own - peer_held:
                return
        if d.name in self.backups and not self.fleet.transport.unfinished(d.name):
            if policy in ("saved", "current"):
                self.fleet.restore(d, self.backups[d.name], index_policy=policy)
            else:
                self.fleet.restore(d, self.backups[d.name], scope=policy)

    @rule(device=st.integers(0, 3))
    def retire_rule(self, device):
        self.tick()
        target = self.device(device)
        participants = self.fleet.participants()
        if len(participants) <= 2 or target not in participants:
            return
        state = self.fleet.state_factory(target.state_root).read()
        if self.fleet.clock.now - state.meta.get("presence_time", self.fleet.clock.now) < 30 * DAY:
            return
        actor = next(d for d in participants if d is not target)
        # E4 eligibility also gates KNOWN_DESIGN_GAPS["retire-delayed-presence"]:
        # random retirement has full delivery and active-peer snapshot coverage.
        # The delayed-presence schedule remains a strict deterministic regression.
        settle(self.fleet)
        self.fleet.advance(30 * DAY)
        for d in self.fleet.participants():
            if d is not target:
                old = d.source_root / "notes.txt"
                data = old.read_bytes() if old.exists() else None
                d.edit("notes.txt", CONTENTS[1] if data != CONTENTS[1] else CONTENTS[2])
                self.fleet.action(d.name, "publish_snapshot")
        self.fleet.drain()
        self.fleet.action(actor.name, "retire", target=target.mm_id)

    def teardown(self):
        """An operator's remedy before the final settle: a device a restore or clone
        forked refuses until it runs ``mm rejoin`` (store-v2 section 9), within MAX_IDS."""
        if sys.exc_info()[0] is None:
            # Counted in the summary: the quiescent floor measures post-remedy state,
            # and the count keeps that visible instead of silent.
            remedied = False
            for d in self.fleet.ordered():
                meta = self.fleet.state_factory(d.state_root).read().meta
                if (
                    meta.get("refused")
                    and len(self.fleet.ids) < MAX_IDS
                    and not self.fleet.transport.unfinished(d.name)
                ):
                    self.fleet.rejoin(d)
                    remedied = True
            if remedied:
                self.invocation.remedies += 1
        super().teardown()

    @rule(device=st.integers(0, 3))
    def torn_rule(self, device):
        """Torn own slot: a crash after k of n parts, closed by the next push."""
        self.tick()
        d = self.device(device)
        if d in self.fleet.participants() and not self.fleet.transport.unfinished(d.name):
            # Fresh ASCII per path keeps the commit multipart and avoids the pinned
            # non-UTF-8 union gap that random edits also stay clear of.
            for path in PATHS:
                d.edit(path, path[:1].encode() * 450 + b"%02x" % (len(self.fleet.steps) % 256))
            self.fleet.action(d.name, "push", crash_parts=1, attended=False)
            self.fleet.action(d.name, "push")

    @rule(device=st.integers(0, 3))
    def heal_rule(self, device):
        """Another machine's rollback deleted this device's newest record above its
        snapshot floor: the next attended push publishes the snapshot readers fall back to."""
        self.tick()
        d = self.device(device)
        if d not in self.fleet.participants() or self.fleet.transport.unfinished(d.name):
            return
        state = self.fleet.state_factory(d.state_root).read()
        head = max(map(int, state.own_log), default=0)
        if head <= state.meta.get("snapshot_seq", 0):
            return
        families = list(d.replica_root.glob(f"dev/{d.mm_id}/log/{head:012d}.*.enc"))
        for path in families:
            path.unlink()
        if families:
            self.fleet.transport.scan(d.name)
            self.fleet.action(d.name, "push")

    @rule(device=st.integers(0, 3), snapshot=st.booleans(), parts=st.integers(0, 2))
    def crash_rule(self, device, snapshot, parts=0):
        self.tick()
        d = self.device(device)
        if d in self.fleet.participants() and not self.fleet.transport.unfinished(d.name):
            d.edit("other.txt", CONTENTS[2])
            self.fleet.action(
                d.name,
                "publish_snapshot" if snapshot else "push",
                crash_state=not parts,
                crash_parts=parts or None,
            )
            self.fleet.action(d.name, "push")


def test_lifecycle_replication(tmp_path_factory, sim_write_guard, monkeypatch, request):
    run_machine(
        LifecycleMachine, tmp_path_factory, sim_write_guard, monkeypatch, request, lifecycle=True
    )


def test_snapshot_witness_reaches_actual_displaced_expiry(tmp_path, sim_write_guard, monkeypatch):
    # Value: protects=honest per-invocation reachability; fails_when=witness
    # reports expiry without expiring anything; why_new=summary checks only labels;
    # seam=existing no-argument machine replay context.
    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    activation_context = ACTIVE.set(Invocation(tmp_path / "expiry-witness", sim_write_guard))
    machine = CoreMachine()
    try:
        machine.snapshot_witness()
        expired = [
            value
            for device in machine.fleet.ordered()
            for value in machine.fleet.state_factory(device.state_root).read().displaced
            if value.get("expired")
            and value["content"] is None
            and machine.fleet.clock.now - value["time"] >= 30 * DAY
        ]
        assert "displaced-expiry" in machine.fleet.coverage
        assert expired, "expiry reachability requires an actually expired displaced version"
    finally:
        try:
            machine.teardown()
        finally:
            ACTIVE.reset(activation_context)


def test_teardown_preserves_original_failure_and_cleans_root(
    tmp_path, sim_write_guard, monkeypatch
):
    import sys

    from .invariants import InvariantError

    monkeypatch.setattr(codec, "MAX_PART_BYTES", PART_BYTES)
    activation_context = ACTIVE.set(Invocation(tmp_path / "first-failure", sim_write_guard))
    machine = CoreMachine()
    calls = []

    def secondary_failure(*args, **kwargs):
        calls.append("settle")
        raise InvariantError(5, subject="secondary teardown", evidence={})

    monkeypatch.setattr(sys.modules[__name__], "settle", secondary_failure)
    try:
        with pytest.raises(InvariantError) as raised:
            try:
                raise InvariantError(3, subject="original rule failure", evidence={})
            finally:
                machine.teardown()
        assert raised.value.invariant == 3
        assert calls == []
        assert not machine.fleet.root.exists()
    finally:
        ACTIVE.reset(activation_context)
