"""Independent observations, preservation oracle and invariants 1-7.

Invariant 7 extends store-v2 §12. Checkers never import the model core or merge
normalization helpers. Every store operation goes through store_action.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache

from mind_meld.store import envelope, layout
from mind_meld.store import format as codec

from .model.registry import Adapter
from .model.state import FileState, State, path_key
from .model.store import Context, Outcome
from .store_fleet import DAY, MARKERS, STIGNORE
from .transport import read_tree

# Owner approved UC2's checkable invariant, and explicitly kept both restore
# counterexamples pinned. Rows are removed only when their design fix lands.
KNOWN_DESIGN_GAPS = {
    "restore-saved": {
        "section": "§4, §9 rows 1/4",
        "outcome": "O1/O5",
        "tracks": "75A/76A/78A/80A",
        "decision": "UC2 accepted; fix pending",
        "node": "test_scenarios.py::test_gap_restore_saved",
        "gates": "restore saved",
    },
    "restore-current": {
        "section": "§4, §9 rows 1/4",
        "outcome": "O1/O5",
        "tracks": "75A/76A/78A/80A",
        "decision": "UC2 accepted; fix pending",
        "node": "test_scenarios.py::test_gap_restore_current",
        "gates": "restore current",
    },
}
KNOWN_DESIGN_GAPS["non-utf8-union"] = {
    "section": "§4 symmetric .jsonl/MEMORY.md resolution",
    "outcome": "O1.1",
    "tracks": "78A",
    "decision": "confirmed counterexample; owner fix pending",
    "node": "test_scenarios.py::test_gap_non_utf8_union",
    "gates": "non-UTF-8 union input in random edit",
}
KNOWN_DESIGN_GAPS["partial-own-commit"] = {
    "section": "§1 write-once, §5 steps 4/5, §9 row 1",
    "outcome": "O1/O3/O5",
    "tracks": "75A/76A",
    "decision": "confirmed underspecified crash recovery; owner decision pending",
    "node": "test_scenarios.py::test_gap_partial_own_commit",
    "gates": "push crash after k of n parts with 0 < k < n",
}
KNOWN_DESIGN_GAPS["retire-delayed-presence"] = {
    "section": "§4 authored-only logs, §6 reader table, §8 active-device coverage",
    "outcome": "O1.1",
    "tracks": "76A/77A/81A",
    "decision": "confirmed E23 counterexample; owner design decision pending",
    "node": "test_scenarios.py::test_gap_retire_delayed_active_presence",
    "gates": "random retirement requires fully delivered active presence and covering snapshots",
}

NAMES = {
    1: "convergence",
    2: "no-silent-loss",
    3: "single-writer",
    4: "ordering-never-fails",
    5: "termination",
    6: "fork-safety",
    7: "causal-fidelity",
}
SECTIONS = {
    1: "§4/6/12 O1",
    2: "§4/9/12 O1/O5",
    3: "§1/8/12 O4",
    4: "§6/9/12 O3/O5",
    5: "§4/12 O1",
    6: "§9/12 O5",
    7: "§4 O1.1",
}


class InvariantError(AssertionError):
    def __init__(
        self,
        invariant: int,
        *,
        subject,
        evidence,
        step=0,
        piece="model",
        next_step="fix the model; replay tests/sim/README.md#failure-replay",
    ):
        self.invariant = invariant
        self.subject = subject
        self.evidence = evidence
        super().__init__(
            f"INV{invariant} {NAMES[invariant]} store-v2 {SECTIONS[invariant]}; "
            f"subject={subject}; step={step}; expected/observed={evidence}; "
            f"last-moving piece={piece}; next={next_step}; "
            f"KNOWN_DESIGN_GAPS={KNOWN_DESIGN_GAPS}; "
            "tests/sim/README.md#known-design-gaps"
        )


def fail(fleet, invariant, subject, evidence):
    raise InvariantError(
        invariant, subject=subject, evidence=evidence, step=len(fleet.steps), piece=fleet.last_piece
    )


@lru_cache(maxsize=128)
def normalized_lines(data: bytes) -> frozenset[bytes]:
    """Lossless, byte-exact newline split; drop trailing whitespace and empty lines."""
    text = data.decode("utf-8", errors="surrogateescape")
    return frozenset(
        line.rstrip().encode("utf-8", errors="surrogateescape")
        for line in text.split("\n")
        if line.strip()
    )


def contains(path: str, version: bytes | None, data: bytes | None) -> bool:
    if version is None or data is None:
        return version is data
    if path.endswith(".jsonl") or path.rsplit("/", 1)[-1] == "MEMORY.md":
        return normalized_lines(version) <= normalized_lines(data)
    return version == data


@dataclass
class Version:
    event: int
    source: str
    path: str
    data: bytes | None
    device: str
    user: bool = True
    authored: bool = True
    identity: str | None = None
    vv: dict[str, int] = field(default_factory=dict)
    piece: str | None = None


class Oracle:
    def __init__(self):
        self.versions: dict[int, Version] = {}
        self.edges: dict[int, set[int]] = {}
        self.excluded: set[int] = set()
        self.last_holders: dict[int, list[str]] = {}

    def add(
        self,
        source,
        path,
        data,
        device,
        *,
        user=True,
        vv=None,
        authored=None,
        identity=None,
        piece=None,
    ) -> Version:
        version = Version(
            len(self.versions) + 1,
            source,
            path,
            data,
            device,
            user,
            user if authored is None else authored,
            identity,
            vv or {},
            piece,
        )
        self.versions[version.event] = version
        return version

    def user_action(self, fleet, device, source, path, data):
        target = source_roots(device)[source] / path
        previous = target.read_bytes() if target.exists() else None
        if data == previous:
            return  # No changed bytes and no new causal author version.
        state = state_for(fleet, device).read()
        old = state.ledger.get(path_key(source, path))
        vv = dict(old["vv"]) if old else {}
        vv[device.mm_id] = vv.get(device.mm_id, 0) + 1
        held = [
            v.event
            for _, v in sorted(self.versions.items())
            if (v.source, v.path) == (source, path)
            and previous is not None
            and contains(path, v.data, previous)
        ]
        new = self.add(source, path, data, device.name, vv=vv, identity=device.mm_id)
        for event in held:
            self.edges.setdefault(event, set()).add(new.event)

    def store_version(self, source, path, data, device, vv, *, piece):
        new = self.add(source, path, data, device, user=False, vv=vv, piece=piece)
        for event, old in sorted(self.versions.items()):
            if (
                event != new.event
                and (old.source, old.path) == (source, path)
                and (old.data is not None and data is not None and contains(path, old.data, data))
            ):
                self.edges.setdefault(event, set()).add(new.event)

    def check(self, fleet, *, quiescent=False):
        current, displaced = [], []
        for device in fleet.ordered():
            current.extend(
                (source, path, file.data, device.name)
                for (source, path), file in source_inventory(device).items()
            )
            displaced.extend(
                (value, device.name) for value in state_for(fleet, device).read().displaced
            )
        records = []
        preserved = set(self.excluded)
        for event, version in sorted(self.versions.items()):
            holders = [
                name
                for source, path, data, name in current
                if (source, path) == (version.source, version.path)
                and contains(path, version.data, data)
            ]
            holders += [
                name + ":displaced"
                for value, name in displaced
                if (value["source"], value["path"]) == (version.source, version.path)
                and (
                    (
                        value.get("expired")
                        and value["sha"] == sha(version.data)
                        and fleet.clock.now - value["time"] >= 30 * DAY
                    )
                    or contains(version.path, version.data, value["content"])
                )
            ]
            holders += [
                name + ":record"
                for source, path, data, name in records
                if (source, path) == (version.source, version.path)
                and contains(path, version.data, data)
            ]
            # A harness delete is an explicit user decision, not a store resolution.
            if holders or (version.user and version.data is None):
                preserved.add(event)
            self.last_holders[event] = holders
        preserved = self.closure(preserved)
        missing = [
            v
            for event, v in sorted(self.versions.items())
            if v.user and v.data is not None and event not in preserved
        ]
        if missing and not quiescent:
            records = record_versions(fleet)
            for event, version in sorted(self.versions.items()):
                holders = [
                    name + ":record"
                    for source, path, data, name in records
                    if (source, path) == (version.source, version.path)
                    and contains(path, version.data, data)
                ]
                if holders:
                    preserved.add(event)
                    self.last_holders[event].extend(holders)
            preserved = self.closure(preserved)
        for event, version in sorted(self.versions.items()):
            if version.user and version.data is not None and event not in preserved:
                history = [
                    vars(v)
                    for _, v in sorted(self.versions.items())
                    if (v.source, v.path) == (version.source, version.path)
                ]
                fail(
                    fleet,
                    2,
                    (version.source, version.path, event, version.device),
                    {
                        "expected": "current/displaced/superseded/recoverable",
                        "last_holders": self.last_holders[event],
                        "path_history": history,
                        "quiescent": quiescent,
                    },
                )

    def closure(self, preserved):
        changed = True
        while changed:
            changed = False
            for event, successors in sorted(self.edges.items()):
                if event not in preserved and any(e in preserved for e in sorted(successors)):
                    preserved.add(event)
                    changed = True
        return preserved


def sha(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def source_roots(device):
    return getattr(device, "source_roots", {"claude": device.source_root})


def source_inventory(device):
    return {
        (source, path): file
        for source, root in sorted(source_roots(device).items())
        for path, file in read_tree(root).items()
    }


def state_for(fleet, device):
    return getattr(fleet, "state_factory", FileState)(device.state_root)


def context_for(fleet, device):
    return Context(
        device.name,
        device.mm_id,
        state_for(fleet, device),
        device.replica_root,
        source_roots(device),
        fleet.clock,
        lambda: device.hardware_id,
        fleet.keys(device),
        fleet.registry,
    )


def record_versions(fleet):
    result = []
    for device in fleet.ordered():
        ctx = context_for(fleet, device)
        if ctx.keys is None:
            continue
        for peer in sorted({n.device_id for n in ctx.names().values()}):
            for kind in ("commit", "snapshot"):
                for _, _, record in ctx.records(peer, kind):
                    result.extend(
                        (e.metadata["source"], e.metadata["path"], e.content, device.name)
                        for e in record.entries
                    )
        # Conflict copies retain their original authenticated identity. Only a
        # complete, readable family counts; never mix parts from competing copies.
        families = {}
        for path in ctx.file_keys():
            match = re.fullmatch(r"(.+?)((?:\.sync-conflict-\d{8}-\d{6}-[A-Za-z0-9]+)+)\.enc", path)
            name = layout.parse_record_key(match[1] + ".enc") if match else None
            if name is None or name.kind not in ("commit", "snapshot"):
                continue
            family_identity = (name.device_id, name.kind, name.seq, name.nparts, match[2])
            expected = envelope.RecordContext(
                ctx.keys.fleet_id, name.device_id, name.kind, name.seq, name.part, name.nparts
            )
            families.setdefault(family_identity, []).append(
                (expected, (ctx.replica_root / path).read_bytes())
            )
        for (peer, kind, seq, _, _), parts in sorted(families.items()):
            expected = ctx.context(peer, kind, seq)
            try:
                payload = codec.open_record(parts, ctx.keys, expected=expected)
                if not isinstance(payload, bytes):
                    continue
                decode = codec.decode_commit if kind == "commit" else codec.decode_snapshot
                record = decode(payload, expected=expected)
            except envelope.StoreError:
                continue
            result.extend(
                (e.metadata["source"], e.metadata["path"], e.content, device.name)
                for e in record.entries
            )
    return result


@dataclass
class Observation:
    device: str
    operation: str
    outcome: Outcome
    before_state: State
    state: State
    replica_before: dict
    replica_after: dict
    sources_before: dict
    sources_after: dict
    history: list[dict]
    ground_truth: set[str]
    piece: str
    before_identity: str | None = None
    identity: str | None = None


def ground_truth(ctx: Context, state: State, tree: dict) -> set[str]:
    """Derived from disk and live hardware, never a model refusal flag."""
    result = set()
    for fork in layout.classify_keys(sorted(tree)).forks:
        if fork.startswith("fleet.") or fork.startswith("dev/" + ctx.device_id + "/"):
            result.add("fork")
    names = {
        key: name for key in sorted(tree) if (name := layout.parse_record_key(key)) is not None
    }
    if state.own_log and not any(n.device_id == ctx.device_id for n in names.values()):
        result.add("missing-own-directory")
    if ctx.keys is None:
        return result
    hashes = {int(seq): value["hash"] for seq, value in state.own_log.items()}
    sequences = sorted(
        {n.seq for n in names.values() if (n.device_id, n.kind) == (ctx.device_id, "commit")}
    )
    for seq in sequences:
        expected = envelope.RecordContext(ctx.keys.fleet_id, ctx.device_id, "commit", seq)
        parts = [
            (
                envelope.RecordContext(
                    ctx.keys.fleet_id, ctx.device_id, "commit", seq, name.part, name.nparts
                ),
                tree[key].data,
            )
            for key, name in names.items()
            if (name.device_id, name.kind, name.seq) == (ctx.device_id, "commit", seq)
        ]
        try:
            payload = codec.open_record(parts, ctx.keys, expected=expected)
            if not isinstance(payload, bytes):
                continue
            record = codec.decode_commit(payload, expected=expected)
        except envelope.StoreError:
            continue
        if record.header["host"] != ctx.host:
            result.add("foreign-host")
        digest = hashlib.sha256(payload).hexdigest()
        if (seq > 1 and seq - 1 in hashes and record.header["prev"] != hashes[seq - 1]) or (
            seq in hashes and hashes[seq] != digest
        ):
            result.add("fork")
        else:
            # Reconciliation can replay an unseen prefix before discovering a fork.
            hashes[seq] = digest
    try:
        presence = ctx.load(ctx.device_id, "presence", 0)
    except envelope.StoreError:
        presence = None
    if (
        isinstance(presence, bytes)
        and codec.decode_presence(presence, expected=ctx.context(ctx.device_id, "presence", 0))[
            "host"
        ]
        != ctx.host
    ):
        result.add("foreign-host")
    return result


def inv3_single_writer(fleet, observation, *, target=None):
    device = fleet.devices[observation.device]
    before, after = observation.replica_before, observation.replica_after
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) == after.get(path):
            continue
        allowed = path.startswith("dev/" + device.mm_id + "/")
        allowed |= observation.operation == "create_fleet" and path == "fleet.json"
        allowed |= (
            observation.operation in ("create_fleet", "join", "rejoin") and path == ".stignore"
        )
        allowed |= (
            observation.operation == "retire"
            and target is not None
            and path.startswith("dev/" + target + "/")
            and path not in after
        )
        if not allowed:
            fail(fleet, 3, path, "write outside the owner's namespace")
    for path in sorted(after):
        if any(part.startswith(".mm-tmp-") for part in path.split("/")):
            fail(fleet, 3, path, "publication left a temporary file")


def replica_integrity(fleet, device, tree):
    keys = fleet.keys(device)
    for path, file in sorted(tree.items()):
        if path in ("fleet.json", ".stignore"):
            if path == ".stignore" and file.data != STIGNORE:
                fail(fleet, 3, path, "wrong .stignore contents")
            continue
        if any(marker in file.data for marker in MARKERS):
            fail(fleet, 3, path, "content-pool plaintext marker in replica")
        name = layout.parse_record_key(path)
        if name is None:
            if path not in layout.classify_keys([path]).forks:
                # Transport may create presence/snapshot copies; they are foreign,
                # but still must authenticate under their original record identity.
                copy = re.fullmatch(
                    r"(.+?)(?:\.sync-conflict-\d{8}-\d{6}-[A-Za-z0-9]+)+\.enc", path
                )
                name = None if copy is None else layout.parse_record_key(copy[1] + ".enc")
                if name is None:
                    fail(fleet, 3, path, "foreign file is not a sealed record/fork signal")
            else:
                stem = path.split(".sync-conflict-", 1)[0]
                if stem == "fleet":
                    envelope.unlock_fleet(file.data, "simulated-v2-passphrase")
                    continue
                name = layout.parse_record_key(stem + ".enc")
        if file.data[:1] != b"\x03" or name is None or keys is None:
            fail(fleet, 3, path, "expected envelope 0x03 record")
        expected = envelope.RecordContext(
            keys.fleet_id, name.device_id, name.kind, name.seq, name.part, name.nparts
        )
        try:
            envelope.open_part(file.data, keys, expected=expected)
        except envelope.StoreError:
            if not fleet.allow_corruption:
                fail(fleet, 3, path, "envelope does not authenticate")


def inv4_ordering_never_fails(fleet, obs):
    outcome = obs.outcome
    if outcome.status == "stalled-unreadable" and not fleet.allow_corruption:
        fail(fleet, 4, obs.device, "unreadable outcome outside corruption scenario")
    if obs.operation == "pull" and outcome.status not in ("applied", "pending", "refused"):
        if not (fleet.allow_corruption and outcome.status == "stalled-unreadable"):
            fail(fleet, 4, obs.device, vars(outcome))
    if outcome.status == "refused":
        valid = outcome.reason in obs.ground_truth
        if outcome.peer is not None:
            ctx = context_for(fleet, fleet.devices[obs.device])
            peer_fork = any(
                k.startswith("dev/" + outcome.peer + "/")
                for k in layout.classify_keys(sorted(obs.replica_before)).forks
            )
            cursor = obs.state.cursors.get(outcome.peer, {"applied_seq": 0, "hash": None})
            try:
                payload = ctx.load(outcome.peer, "commit", cursor["applied_seq"] + 1)
            except envelope.StoreError:
                payload = None
            chain_fork = (
                (cursor["applied_seq"] == 0 or cursor["hash"] is not None)
                and isinstance(payload, bytes)
                and codec.decode_commit(
                    payload, expected=ctx.context(outcome.peer, "commit", cursor["applied_seq"] + 1)
                ).header["prev"]
                != cursor["hash"]
            )
            valid |= outcome.reason == "fork" and (peer_fork or chain_fork)
        if not valid:
            fail(
                fleet, 4, obs.device, {"refusal": outcome.reason, "ground_truth": obs.ground_truth}
            )
    for peer, cursor in sorted(obs.state.cursors.items()):
        if (
            obs.operation == "pull"
            and cursor.get("gap_since") is not None
            and fleet.clock.now - cursor["gap_since"] > DAY
        ):
            if not any(
                warning in ("gap older than 24 hours", "chain anchor gap older than 24 hours")
                for warning in outcome.warnings
            ):
                fail(
                    fleet,
                    4,
                    (obs.device, peer),
                    {
                        "expected": "gap older than 24h has visible warning",
                        "cursor": cursor,
                        "outcome": vars(outcome),
                    },
                )
    if outcome.status == "stalled-unreadable" and not outcome.warnings:
        fail(fleet, 4, obs.device, "unreadable commit has no always-visible warning")


def inv6_fork_safety(fleet, obs):
    if obs.ground_truth & {"foreign-host", "fork"}:
        changed = [
            p
            for p in sorted(obs.replica_after)
            if p.startswith("dev/") and obs.replica_before.get(p) != obs.replica_after[p]
        ]
        if obs.operation == "rejoin" and obs.identity != obs.before_identity:
            # Rejoin may publish only under its freshly allocated identity.
            changed = [p for p in changed if not p.startswith("dev/" + obs.identity + "/")]
        if changed or (obs.operation in ("push", "pull") and obs.outcome.status != "refused"):
            fail(
                fleet,
                6,
                obs.device,
                {
                    "published": changed,
                    "outcome": obs.outcome.status,
                    "ground_truth": obs.ground_truth,
                },
            )


def vv_order(left, right):
    keys = sorted(left.keys() | right.keys())
    before = all(left.get(k, 0) <= right.get(k, 0) for k in keys)
    after = all(left.get(k, 0) >= right.get(k, 0) for k in keys)
    return (
        "equal" if before and after else "before" if before else "after" if after else "concurrent"
    )


def inv7_causal_fidelity(fleet, obs):
    for value in obs.history:
        if value.get("operation") != "pull-entry":
            continue
        local, incoming, action = value["local"], value["incoming"], value["action"]
        if value["bootstrap"] and incoming["deleted"]:
            expected = "bootstrap-delete"
        elif local is None:
            expected = "fast-forward"
        elif local["sha"] == incoming["sha"]:
            expected = "same-sha"
        else:
            expected = {
                "before": "fast-forward",
                "after": "ignore",
                "equal": "resolve",
                "concurrent": "resolve",
            }[vv_order(local["vv"], incoming["vv"])]
        if action != expected or (expected in ("ignore", "same-sha") and value["wrote"]):
            fail(
                fleet,
                7,
                (incoming["source"], incoming["path"], obs.device),
                {
                    "expected": expected,
                    "action": action,
                    "local": local,
                    "incoming": incoming,
                    "wrote": value["wrote"],
                },
            )
        if expected == "fast-forward" and value["result"]["sha"] != incoming["sha"]:
            fail(fleet, 7, incoming["path"], "dominant version did not fast-forward")
        if expected == "same-sha":
            keys = sorted(local["vv"].keys() | incoming["vv"].keys())
            union = {key: max(local["vv"].get(key, 0), incoming["vv"].get(key, 0)) for key in keys}
            if value["result"]["vv"] != union or value["result"]["sha"] != local["sha"]:
                fail(
                    fleet,
                    7,
                    incoming["path"],
                    {"expected_vector_union": union, "result": value["result"]},
                )
        if expected == "ignore" and value["result"]["sha"] != local["sha"]:
            fail(fleet, 7, incoming["path"], "dominated entry changed the local version")


def inv7_quiescent(fleet):
    participants = {d.name for d in fleet.participants()}
    paths = sorted({(v.source, v.path) for v in fleet.oracle.versions.values() if v.authored})
    for source, path in paths:
        authored = [
            v
            for _, v in sorted(fleet.oracle.versions.items())
            if v.authored
            and (
                not v.user
                or (
                    v.device in participants
                    and (v.identity is None or v.identity == fleet.devices[v.device].mm_id)
                )
            )
            and (v.source, v.path) == (source, path)
            and v.event not in fleet.oracle.excluded
        ]
        maximal = [
            v
            for v in authored
            if not any(
                vv_order(v.vv, other.vv) == "before"
                or (v.device == other.device and v.vv == other.vv and v.event < other.event)
                for other in authored
            )
        ]
        unique = {v.data for v in maximal}
        if len(unique) != 1:
            continue
        version = maximal[-1]
        if version.data is None and any(
            v.data is not None and vv_order(v.vv, version.vv) == "concurrent" for v in authored
        ):
            continue
        for device in fleet.participants():
            target = source_roots(device)[source] / path
            observed = target.read_bytes() if target.exists() else None
            if observed != version.data:
                fail(
                    fleet,
                    7,
                    (source, path, version.event, device.name),
                    {"expected": version.data, "observed": observed, "vv": version.vv},
                )


def observed_record(fleet, obs, peer, kind, seq, *, published=False, required=True):
    """Authenticate the observation's bytes, independently of model read/history."""
    keys = fleet.keys(fleet.devices[obs.device])
    trees = (
        (obs.replica_after, obs.replica_before)
        if published
        else (obs.replica_before, obs.replica_after)
    )
    for tree in trees:
        parts = []
        for path, file in sorted(tree.items()):
            name = layout.parse_record_key(path)
            if name and (name.device_id, name.kind, name.seq) == (peer, kind, seq):
                parts.append(
                    (
                        envelope.RecordContext(
                            keys.fleet_id, peer, kind, seq, name.part, name.nparts
                        ),
                        file.data,
                    )
                )
        expected = envelope.RecordContext(keys.fleet_id, peer, kind, seq)
        try:
            payload = codec.open_record(parts, keys, expected=expected)
        except envelope.StoreError:
            continue
        if isinstance(payload, bytes):
            decode = codec.decode_commit if kind == "commit" else codec.decode_snapshot
            return payload, decode(payload, expected=expected)
    if required:
        fail(fleet, 7, (peer, kind, seq), "applied history has no authenticated record")
    return None


def inv7_observed_inputs(fleet, obs):
    """Replay recorded effects from independent before-state and sealed records.

    Record receipts supply grouping, not truth: every entry is authenticated,
    every local input is reconstructed, and cursor/ledger/source effects must
    reconcile to the observation. Omitting an entire receipt cannot hide progress.
    """
    ledger = deepcopy(obs.before_state.ledger)
    files = {
        (s, p): (v.data, v.mtime_ns / 1_000_000_000)
        for (s, p), v in obs.sources_before[obs.device].items()
    }
    cursors = {p: (r["applied_seq"], r["hash"]) for p, r in obs.before_state.cursors.items()}
    own_records = {seq: r["hash"] for seq, r in obs.before_state.own_log.items()}
    identity = obs.before_identity
    bootstrap_pending = obs.operation == "join" and not obs.before_state.meta.get("joined")
    pending = []
    authored = []

    def require(condition, subject, evidence):
        if not condition:
            fail(fleet, 7, subject, evidence)

    def wire_row(entry):
        return {**entry.metadata, "content": entry.content, "pending": False}

    def anchor_hash(peer, seq):
        for kind in ("commit", "snapshot"):
            observed = observed_record(fleet, obs, peer, kind, seq, required=False)
            if observed is not None:
                payload, record = observed
                return (
                    hashlib.sha256(payload).hexdigest()
                    if kind == "commit"
                    else record.header["prev"]
                )
        return None

    for event in obs.history:
        operation = event.get("operation")
        if operation == "rejoin":
            require(
                obs.operation == "rejoin"
                and identity == event["old_id"]
                and event["new_id"] == obs.identity
                and obs.identity != identity,
                obs.device,
                "unobserved rejoin identity transition",
            )
            identity, bootstrap_pending = obs.identity, True
            ledger, cursors = {}, {}
            own_records = {}
        elif operation == "joined":
            require(
                obs.operation in ("create_fleet", "join", "rejoin"),
                obs.device,
                "unexpected bootstrap boundary",
            )
        elif operation == "pull-entry":
            pending.append(event)
        elif operation in ("record-applied", "publish"):
            published = operation == "publish"
            peer = identity if published else event["peer"]
            kind = "commit" if published else event["kind"]
            payload, record = observed_record(
                fleet, obs, peer, kind, event["seq"], published=published
            )
            digest = hashlib.sha256(payload).hexdigest()
            if kind == "commit":
                if peer == identity:
                    own_records[str(event["seq"])] = digest
                    authored.extend((identity, entry) for entry in record.entries)
                else:
                    require(
                        event["seq"] == cursors.get(peer, (0, None))[0] + 1,
                        (peer, event["seq"]),
                        "commit skipped a sequence without a covering snapshot",
                    )
            if published:
                require(not pending, obs.device, "unbound classification before publication")
                for entry in record.entries:
                    incoming = wire_row(entry)
                    key = (incoming["source"], incoming["path"])
                    require(
                        incoming["content"] == files.get(key, (None, 0))[0],
                        key,
                        "publication differs from observed source",
                    )
                    old = ledger.get(path_key(*key))
                    expected_vv = dict(old["vv"]) if old else {}
                    expected_vv[identity] = expected_vv.get(identity, 0) + 1
                    require(
                        incoming["vv"] == expected_vv,
                        key,
                        {"expected_publication_vv": expected_vv, "observed": incoming["vv"]},
                    )
                    ledger[path_key(*key)] = incoming
                bootstrap_pending = False
            else:
                require(
                    len(pending) == len(record.entries),
                    (peer, kind, event["seq"]),
                    {"entries": len(record.entries), "classifications": len(pending)},
                )
                for value, entry in zip(pending, record.entries):
                    incoming = wire_row(entry)
                    key = (incoming["source"], incoming["path"])
                    require(
                        (value["peer"], value["seq"]) == (peer, event["seq"])
                        and value["incoming"] == incoming,
                        key,
                        "history differs from authenticated entry",
                    )
                    old = ledger.get(path_key(*key))
                    data, mtime = files.get(key, (None, 0))
                    local = None
                    if old is not None or data is not None:
                        vv = dict(old["vv"]) if old else {}
                        if old is None or old["sha"] != sha(data):
                            vv[identity] = vv.get(identity, 0) + 1
                        else:
                            mtime = old["mtime"]
                        local = {
                            "source": key[0],
                            "path": key[1],
                            "content": data,
                            "sha": sha(data),
                            "vv": vv,
                            "size": len(data) if data is not None else 0,
                            "deleted": data is None,
                            "mtime": mtime if data is not None else None,
                            "pending": False,
                        }
                    # Only the initial snapshot of a fresh join/rejoin is bootstrap.
                    # The piece's reported flag or joined event cannot select it.
                    bootstrap = (
                        bootstrap_pending
                        and kind == "snapshot"
                        and old is None
                        and data is not None
                    )
                    require(
                        value["local"] == local and value["bootstrap"] == bootstrap,
                        key,
                        {
                            "expected_local": local,
                            "reported_local": value["local"],
                            "bootstrap": bootstrap,
                        },
                    )
                    result = value["result"]
                    require(
                        result["sha"] == sha(result["content"])
                        and result["size"]
                        == (len(result["content"]) if result["content"] is not None else 0)
                        and result["deleted"] == (result["content"] is None)
                        and (result["source"], result["path"]) == key,
                        key,
                        "invalid result bytes or identity",
                    )
                    action = value["action"]
                    wrote = action not in ("ignore", "same-sha") and data != result["content"]
                    require(value["wrote"] == wrote, key, "reported I/O differs from entry effects")
                    if action in ("fast-forward", "bootstrap-delete"):
                        require(result == incoming, key, "fast-forward differs from incoming entry")
                    if action == "same-sha":
                        require(
                            result["pending"] == bool(old and old["pending"]),
                            key,
                            "same-SHA receipt changed pending publication",
                        )
                    if action == "resolve":
                        union = {
                            k: max(local["vv"].get(k, 0), incoming["vv"].get(k, 0))
                            for k in local["vv"].keys() | incoming["vv"].keys()
                        }
                        require(
                            result["vv"] == union and result["pending"],
                            key,
                            "resolution lost causal join or pending publication",
                        )
                    if action == "ignore":
                        require(result == local, key, "ignored entry changed local result")
                    else:
                        ledger[path_key(*key)] = result
                    if wrote:
                        if result["content"] is None:
                            files.pop(key, None)
                        else:
                            files[key] = (result["content"], result["mtime"])
                pending = []
                bootstrap_pending = False
            cursors[peer] = (event["seq"], digest if kind == "commit" else record.header["prev"])
            if kind == "snapshot":
                for other, seq in sorted(record.header["cursors"].items()):
                    if seq > cursors.get(other, (0, None))[0]:
                        cursors[other] = (seq, anchor_hash(other, seq))
    require(not pending, obs.device, "classification without an applied record")
    require(
        own_records == {seq: r["hash"] for seq, r in obs.state.own_log.items()},
        obs.device,
        "own log changed without authenticated record history",
    )
    require(
        ledger == obs.state.ledger,
        obs.device,
        "ledger differs from authenticated/reconstructed history",
    )
    actual_files = {
        (s, p): (v.data, v.mtime_ns / 1_000_000_000)
        for (s, p), v in obs.sources_after[obs.device].items()
    }
    require(files == actual_files, obs.device, "source effects differ from reconstructed history")
    actual_cursors = {p: (r["applied_seq"], r["hash"]) for p, r in obs.state.cursors.items()}
    # An indirect snapshot cursor may acquire its missing anchor before a fork
    # refusal, without applying another record. Only authenticated bytes can
    # justify that hash-only transition; an established hash cannot change.
    for peer, (seq, digest) in sorted(cursors.items()):
        if seq and digest is None and actual_cursors.get(peer) != (seq, None):
            cursors[peer] = (seq, anchor_hash(peer, seq))
    require(
        {p: value for p, value in cursors.items() if value != (0, None)}
        == {p: value for p, value in actual_cursors.items() if value != (0, None)},
        obs.device,
        {"expected_cursors": cursors, "observed_cursors": actual_cursors},
    )
    return authored


def inv1_convergence(fleet):
    views = [
        (
            d.name,
            {p: f.data for p, f in source_inventory(d).items()},
            {k: v["sha"] for k, v in sorted(state_for(fleet, d).read().ledger.items())},
        )
        for d in fleet.participants()
    ]
    if views and any(v[1:] != views[0][1:] for v in views[1:]):
        fail(fleet, 1, "participants", views)


def record_source_writes(fleet, obs, executed):
    """Attribute checked history after the action; never accept it instead of disk evidence."""
    writes = [v for v in obs.history if v.get("operation") == "pull-entry" and v["wrote"]]
    expected = "classify:" + fleet.registry["classify"].kind
    for value in writes:
        result = value["result"]
        if "classify" not in executed or value.get("writer") != expected:
            fail(fleet, 7, (result["source"], result["path"]), "missing or false source writer")
    changed = {
        key
        for key in obs.sources_before[obs.device].keys() | obs.sources_after[obs.device].keys()
        if obs.sources_before[obs.device].get(key) != obs.sources_after[obs.device].get(key)
    }
    recorded = {(v["result"]["source"], v["result"]["path"]) for v in writes}
    if changed - recorded:
        fail(fleet, 7, sorted(changed - recorded), "source writer missing for observed change")
    for value in writes:
        result = value["result"]
        fleet.oracle.store_version(
            result["source"],
            result["path"],
            result["content"],
            obs.device,
            result["vv"],
            piece=value["writer"],
        )


def store_action(fleet, name, operation, **kwargs):
    started = time.perf_counter()
    device = fleet.devices[name]
    before_identity = device.mm_id
    if operation == "rejoin" and kwargs.get("new_id") in fleet.ids:
        raise ValueError("rejoin requires a fresh identity; see README#lifecycle")
    ctx = context_for(fleet, device)
    before = state_for(fleet, device).read()
    replica_before = {d.name: read_tree(d.replica_root) for d in fleet.ordered()}
    tree = replica_before[name]  # No transport ignore set.
    sources = {d.name: source_inventory(d) for d in fleet.ordered()}
    truth = ground_truth(ctx, before, tree)
    fleet.steps.append((operation, name, tuple(sorted(kwargs.items()))))
    adapter = Adapter(ctx)
    result = (
        getattr(adapter, operation)(**kwargs)
        if hasattr(adapter, operation)
        else ctx.call(operation, **kwargs)
    )
    replica_after = {d.name: read_tree(d.replica_root) for d in fleet.ordered()}
    after_tree = replica_after[name]
    state = state_for(fleet, device).read()
    if operation == "rejoin" and state.meta.get("device_id") == kwargs.get("new_id"):
        device.mm_id = state.meta["device_id"]
    source_after = {d.name: source_inventory(d) for d in fleet.ordered()}
    fleet.last_piece = " -> ".join(
        p + ":" + fleet.registry[p].kind for p in ctx.executed if p in fleet.registry
    )
    history = state.history[len(before.history) :]
    writers = [
        value["writer"]
        for value in history
        if value.get("operation") == "pull-entry" and value.get("wrote") and value.get("writer")
    ]
    if writers:
        fleet.last_piece = writers[-1]
    obs = Observation(
        name,
        operation,
        result,
        before,
        state,
        tree,
        after_tree,
        sources,
        source_after,
        history,
        truth,
        fleet.last_piece,
        before_identity,
        device.mm_id,
    )
    fleet.last_observation = obs
    inv3_single_writer(fleet, obs, target=kwargs.get("target"))
    inv6_fork_safety(fleet, obs)
    for d in fleet.ordered():
        if d.name != name and replica_before[d.name] != replica_after[d.name]:
            fail(fleet, 3, d.name, "store wrote another device replica")
        if d.name != name and sources[d.name] != source_after[d.name]:
            fail(fleet, 3, d.name, "store wrote another device's source tree")
    for path, file in sorted(after_tree.items()):
        if tree.get(path) != file:
            os.utime(device.replica_root / path, (fleet.clock.now, fleet.clock.now))
    fleet.transport.scan(name)
    replica_integrity(fleet, device, read_tree(device.replica_root))
    inv4_ordering_never_fails(fleet, obs)
    inv7_causal_fidelity(fleet, obs)
    if operation == "retire" and result.status == "retired":
        for d in fleet.ordered():
            if d.mm_id == kwargs["target"]:
                with state_for(fleet, d).transaction() as retired_state:
                    retired_state.meta["retired"] = True
    fleet.coverage.update(result.events)
    if any(
        not old.get("expired")
        and old["content"] is not None
        and new.get("expired")
        and new["content"] is None
        and (old["source"], old["path"], old["sha"], old["time"])
        == (new["source"], new["path"], new["sha"], new["time"])
        and fleet.clock.now - old["time"] >= 30 * DAY
        for old, new in zip(before.displaced, state.displaced)
    ):
        fleet.coverage.add("displaced-expiry")
    if any(value.get("operation") == "own-replay" for value in obs.history):
        fleet.coverage.add("crash-recovery")
    if any(value.get("action") == "bootstrap-delete" for value in obs.history):
        fleet.coverage.add("bootstrap-inversion")
    if any(value.get("counter_reuse") for value in obs.history):
        fleet.coverage.add("counter-reuse")
    for identity, entry in inv7_observed_inputs(fleet, obs):
        fleet.oracle.add(
            entry.metadata["source"],
            entry.metadata["path"],
            entry.content,
            name,
            user=False,
            authored=True,
            identity=identity,
            vv=entry.metadata["vv"],
        )
    record_source_writes(fleet, obs, ctx.executed)
    fleet.oracle.check(fleet)
    fleet.oracle_checked = True
    fleet.action_seconds.append(time.perf_counter() - started)
    return result


def settle(fleet, *, min_participants=0):
    started = time.perf_counter()
    for device in fleet.ordered():
        for token in fleet.transport.unfinished(device.name):
            fleet.transport.finish_replace(token)
        fleet.transport.set_online(device.name, True)
    fleet.transport.advance(10**12)
    fleet.drain()
    participants = fleet.participants()
    for _ in range(3 * len(participants) + 2):
        before = {d.name: source_inventory(d) for d in participants}
        published = False
        fleet.drain()
        for d in participants:
            outcome = fleet.action(d.name, "push")
            published |= outcome.status == "published"
        fleet.drain()
        for d in participants:
            fleet.action(d.name, "pull")
        fleet.drain()
        after = {d.name: source_inventory(d) for d in participants}
        pending_publication = any(
            value.get("pending")
            for d in participants
            for value in state_for(fleet, d).read().ledger.values()
        )
        if not published and not pending_publication and before == after:
            if len(fleet.participants()) < min_participants:
                raise AssertionError(f"expected {min_participants}+ participants")
            inv1_convergence(fleet)
            fleet.oracle.check(fleet, quiescent=True)
            inv7_quiescent(fleet)
            return time.perf_counter() - started
    fail(fleet, 5, "settle", f"no quiescence within {3 * len(participants) + 2} rounds")
