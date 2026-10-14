"""Test-only I/O shell for store-v2 §2/4/6/8/9. Never promoted into product code."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from mind_meld.store import envelope, layout
from mind_meld.store import format as codec

from ..store_fleet import DAY, PASSPHRASE, STIGNORE
from ..transport import owned_path
from . import core
from .state import StateSeam, from_row, path_key, row


@dataclass(frozen=True)
class Outcome:
    status: str
    seq: int | None = None
    peer: str | None = None
    gap_seq: int | None = None
    reason: str | None = None
    warnings: tuple[str, ...] = ()
    events: tuple[str, ...] = ()


@dataclass
class Context:
    name: str
    device_id: str
    state: StateSeam
    replica_root: Path
    source_roots: dict[str, Path]
    clock: object
    machine_id_provider: Callable[[], str]
    keys: envelope.FleetKeys | None
    registry: dict
    executed: list[str] = field(default_factory=list)
    active: list[str] = field(default_factory=list)

    def call(self, piece: str, **kwargs):
        self.executed.append(piece)
        self.active.append(piece)
        try:
            return self.registry[piece].implementation(self, **kwargs)
        finally:
            self.active.pop()

    @property
    def current_piece(self) -> str:
        piece = self.active[-1]
        return piece + ":" + self.registry[piece].kind

    @property
    def host(self) -> str:
        return codec.host_fingerprint(self.keys.fleet_id, self.machine_id_provider())

    def context(self, device: str, kind: str, seq: int) -> envelope.RecordContext:
        return envelope.RecordContext(self.keys.fleet_id, device, kind, seq)

    def file_keys(self) -> tuple[str, ...]:
        result = []

        def walk(root, prefix):
            with os.scandir(root) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
            for item in ordered:
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("symlink in model replica; see README#paths")
                key = prefix + item.name
                if stat.S_ISDIR(info.st_mode):
                    walk(item.path, key + "/")
                elif stat.S_ISREG(info.st_mode):
                    result.append(key)

        walk(self.replica_root, "")
        return tuple(result)

    def names(self) -> dict[str, layout.RecordName]:
        return {
            key: parsed
            for key in self.file_keys()
            if (parsed := layout.parse_record_key(key)) is not None
        }

    def load(self, device: str, kind: str, seq: int, *, names=None):
        """ENOENT is rescued only at the read site; every part family is supplied."""
        parts = []
        for key, parsed in (self.names() if names is None else names).items():
            if (parsed.device_id, parsed.kind, parsed.seq) == (device, kind, seq):
                try:
                    data = (self.replica_root / key).read_bytes()
                except FileNotFoundError:
                    continue
                expected = envelope.RecordContext(
                    self.keys.fleet_id, device, kind, seq, parsed.part, parsed.nparts
                )
                parts.append((expected, data))
        return codec.open_record(parts, self.keys, expected=self.context(device, kind, seq))

    def records(self, device: str, kind: str, *, names=None, errors=None):
        names = self.names() if names is None else names
        result = []
        for seq in sorted(
            {n.seq for n in names.values() if (n.device_id, n.kind) == (device, kind)}
        ):
            try:
                payload = self.load(device, kind, seq, names=names)
            except envelope.StoreError:
                if errors is not None:
                    errors.append(seq)
                continue
            if isinstance(payload, bytes):
                decoder = codec.decode_commit if kind == "commit" else codec.decode_snapshot
                result.append(
                    (seq, payload, decoder(payload, expected=self.context(device, kind, seq)))
                )
        return result

    def source(self, source: str, path: str) -> tuple[bytes | None, float]:
        target = owned_path(self.source_roots[source], path)
        try:
            return target.read_bytes(), target.stat().st_mtime
        except FileNotFoundError:
            return None, 0


def header(ctx: Context, kind: str, seq: int, prev: str | None, entries) -> dict:
    value = {
        "fleet_id": ctx.keys.fleet_id,
        "device_id": ctx.device_id,
        "host": ctx.host,
        "seq": seq,
        "prev": prev,
        "created_at": ctx.clock.iso,
        "mm_version": "2.0.0",
        "entries": len(entries),
    }
    if kind == "snapshot":
        value["cursors"] = {
            p: r["applied_seq"] for p, r in sorted(ctx.state.read().cursors.items())
        }
    return value


def write_record(ctx: Context, kind: str, seq: int, payload: bytes, *, crash_parts=None) -> int:
    parts = tuple(
        codec.seal_record(payload, ctx.keys, expected=ctx.context(ctx.device_id, kind, seq))
    )
    for index, (part, data) in enumerate(parts):
        if crash_parts is not None and index >= crash_parts:
            return 0
        key = layout.record_key(ctx.device_id, kind, seq, part=part.part, nparts=part.nparts)
        target = owned_path(ctx.replica_root, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / ".mm-tmp-publish"
        temp.write_bytes(data)
        try:
            os.link(temp, target)
        finally:
            temp.unlink()
    return len(parts)


def refuse(ctx: Context, reason: str) -> Outcome:
    with ctx.state.transaction() as state:
        state.meta["refused"] = reason
        aged_gap = any(
            "gap_since" in cursor and ctx.clock.now - cursor["gap_since"] > DAY
            for cursor in state.cursors.values()
        )
    return Outcome(
        "refused", reason=reason, warnings=("gap older than 24 hours",) if aged_gap else ()
    )


def create_fleet(ctx: Context, **kwargs) -> Outcome:
    if (ctx.replica_root / "fleet.json").exists() or (ctx.replica_root / "dev").exists():
        return Outcome("refused", reason="occupied-fleet")
    raw, keys = envelope.create_fleet(PASSPHRASE, created_at=ctx.clock.iso, memory_kb=1024)
    with (ctx.replica_root / "fleet.json").open("xb") as stream:
        stream.write(raw)
    (ctx.replica_root / ".stignore").write_bytes(STIGNORE)
    ctx.keys = keys
    with ctx.state.transaction() as state:
        state.meta.update(joined=True, registered=ctx.clock.iso, snapshot_seq=0)
        state.history.append({"operation": "joined"})
    ctx.call("snapshot", force=True)
    return Outcome("published", seq=0)


def reconcile(ctx: Context, *, allow_known_unreadable=False, **kwargs) -> Outcome:
    paths = ctx.file_keys()
    names = ctx.names()
    forks = layout.classify_keys(paths).forks
    own_prefix = "dev/" + ctx.device_id + "/"
    fleet_fork = any(p.startswith("fleet.sync-conflict-") for p in forks)
    own_fork = any(p.startswith(own_prefix) for p in forks)
    if fleet_fork or own_fork:
        return refuse(ctx, "fork")
    if ctx.keys is None:
        return Outcome("waiting", reason="fleet-header")
    foreign = False
    try:
        raw_presence = ctx.load(ctx.device_id, "presence", 0, names=names)
        if isinstance(raw_presence, bytes):
            value = codec.decode_presence(
                raw_presence, expected=ctx.context(ctx.device_id, "presence", 0)
            )
            foreign = value["host"] != ctx.host
    except envelope.StoreError:
        return Outcome(
            "stalled-unreadable",
            peer=ctx.device_id,
            gap_seq=0,
            warnings=("unreadable own presence",),
        )
    unreadable = []
    commits = ctx.records(ctx.device_id, "commit", names=names, errors=unreadable)
    foreign |= any(record.header["host"] != ctx.host for _, _, record in commits)
    state = ctx.state.read()
    reason = core.reconcile_rule(
        fleet_fork=fleet_fork,
        own_fork=own_fork,
        foreign_host=foreign,
        own_missing=bool(state.own_log)
        and not any(n.device_id == ctx.device_id for n in names.values()),
    )
    if reason:
        return refuse(ctx, reason)
    warnings = []
    own_snapshots = ctx.records(ctx.device_id, "snapshot", names=names)
    latest_snapshot = own_snapshots[-1][0] if own_snapshots else -1
    if unreadable:
        warnings.append("unreadable own commit; owner: push --snapshot")
        trusted = all(str(seq) in state.own_log for seq in unreadable)
        if not trusted or (not allow_known_unreadable and max(unreadable) > latest_snapshot):
            return Outcome(
                "stalled-unreadable",
                peer=ctx.device_id,
                gap_seq=unreadable[0],
                warnings=tuple(warnings),
            )
    for seq, payload, record in commits:
        previous = state.own_log.get(str(seq - 1))
        if seq > 1 and previous and record.header["prev"] != previous["hash"]:
            return refuse(ctx, "fork")
        known = state.own_log.get(str(seq))
        if known and known["hash"] != hashlib.sha256(payload).hexdigest():
            return refuse(ctx, "fork")
        if known:
            continue
        for incoming in record.entries:
            ctx.call("classify", incoming=incoming, peer=ctx.device_id, seq=seq)
        with ctx.state.transaction() as updated:
            digest = hashlib.sha256(payload).hexdigest()
            updated.history.append(
                {"operation": "record-applied", "peer": ctx.device_id, "kind": "commit", "seq": seq}
            )
            updated.own_log[str(seq)] = {
                "hash": digest,
                "prev": record.header["prev"],
                "created_at": record.header["created_at"],
                "time": int(datetime.fromisoformat(record.header["created_at"]).timestamp()),
                "bytes": len(payload),
                "parts": max(
                    n.nparts
                    for n in names.values()
                    if n.device_id == ctx.device_id and n.kind == "commit" and n.seq == seq
                ),
            }
            updated.cursors[ctx.device_id] = {"applied_seq": seq, "hash": digest}
        state = ctx.state.read()
        with ctx.state.transaction() as updated:
            updated.history.append({"operation": "own-replay", "seq": seq})
    if latest_snapshot > ctx.state.read().meta.get("snapshot_seq", 0):
        with ctx.state.transaction() as updated:
            updated.meta["snapshot_seq"] = latest_snapshot
            updated.history.append({"operation": "snapshot-recovery", "seq": latest_snapshot})
    return Outcome("applied", warnings=tuple(warnings))


def presence(ctx: Context, *, changed=False, **kwargs) -> Outcome:
    state = ctx.state.read()
    if not changed and ctx.clock.now - state.meta.get("presence_time", 0) < 3600:
        return Outcome("unchanged")
    seq = max(map(int, state.own_log), default=0)
    value = {
        "device_id": ctx.device_id,
        "device_name": ctx.name,
        "os": "fixture",
        "mm_version": "2.0.0",
        "host": ctx.host,
        "syncthing_id": "fixture-" + ctx.name,
        "registered": state.meta.get("registered", ctx.clock.iso),
        "last_active": ctx.clock.iso,
        "published_seq": seq,
        "snapshot_seq": state.meta.get("snapshot_seq", 0),
        "applied": {p: r["applied_seq"] for p, r in sorted(state.cursors.items())},
    }
    expected = ctx.context(ctx.device_id, "presence", 0)
    payload = codec.encode_presence(value, expected=expected)
    _, sealed = next(codec.seal_record(payload, ctx.keys, expected=expected))
    target = owned_path(ctx.replica_root, layout.record_key(ctx.device_id, "presence", 0))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / ".mm-tmp-presence"
    temp.write_bytes(sealed)
    os.replace(temp, target)
    with ctx.state.transaction() as updated:
        updated.meta["presence_time"] = ctx.clock.now
    return Outcome("published", seq=seq)


def authored_entries(ctx: Context) -> tuple[codec.Entry, ...]:
    state = ctx.state.read()
    paths = {
        path_key(source, p.relative_to(root).as_posix()): (source, p.relative_to(root).as_posix())
        for source, root in sorted(ctx.source_roots.items())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    for key, value in sorted(state.ledger.items()):
        paths[key] = value["source"], value["path"]
    result = []
    for key, (source, path) in sorted(paths.items()):
        old = state.ledger.get(key)
        data, mtime = ctx.source(source, path)
        incoming = core.author(
            source,
            path,
            from_row(old),
            data,
            mtime,
            ctx.device_id,
            pending=bool(old and old["pending"]),
        )
        if incoming is not None:
            result.append(incoming)
    return tuple(result)


def publish(
    ctx: Context,
    *,
    crash_parts=None,
    crash_state=False,
    attended=True,
    allow_known_unreadable=False,
    checked=None,
    **kwargs,
) -> Outcome:
    reconciled = checked or ctx.call("reconcile", allow_known_unreadable=allow_known_unreadable)
    if reconciled.status != "applied":
        return reconciled
    entries = authored_entries(ctx)
    if not entries:
        ctx.call(
            "presence",
            changed=bool(reconciled.warnings)
            or any(
                r.get("operation") == "snapshot-recovery" for r in ctx.state.read().history[-1:]
            ),
        )
        return Outcome("unchanged", warnings=reconciled.warnings)
    state = ctx.state.read()
    seq = max(map(int, state.own_log), default=0) + 1
    # Conservative partial-own-commit reading: don't overwrite any published part.
    if any(
        n.device_id == ctx.device_id and n.kind == "commit" and n.seq == seq
        for n in ctx.names().values()
    ):
        return Outcome("pending", peer=ctx.device_id, gap_seq=seq, reason="partial-own-commit")
    prev = state.own_log[str(seq - 1)]["hash"] if seq > 1 else None
    expected = ctx.context(ctx.device_id, "commit", seq)
    payload = codec.encode_commit(
        header(ctx, "commit", seq, prev, entries), entries, expected=expected
    )
    parts = write_record(ctx, "commit", seq, payload, crash_parts=crash_parts)
    if not parts or crash_state:
        return Outcome("crashed", seq=seq)
    digest = hashlib.sha256(payload).hexdigest()
    with ctx.state.transaction() as updated:
        for incoming in entries:
            updated.ledger[path_key(incoming.metadata["source"], incoming.metadata["path"])] = row(
                incoming
            )
        updated.own_log[str(seq)] = {
            "hash": digest,
            "prev": prev,
            "time": ctx.clock.now,
            "created_at": ctx.clock.iso,
            "bytes": len(payload),
            "parts": parts,
        }
        updated.cursors[ctx.device_id] = {"applied_seq": seq, "hash": digest}
        updated.history.append({"operation": "publish", "seq": seq})
    ctx.call("presence", changed=True)
    if attended:
        complete = ctx.records(ctx.device_id, "snapshot")
        last_seq, last_bytes = (complete[-1][0], len(complete[-1][1])) if complete else (0, 0)
        new_rows = [r for n, r in sorted(ctx.state.read().own_log.items()) if int(n) > last_seq]
        if core.snapshot_due(sum(r["bytes"] for r in new_rows), len(new_rows), last_bytes):
            ctx.call("snapshot", force=True)
    return Outcome("published", seq=seq, warnings=reconciled.warnings)


def displace(state, version: codec.Entry | None, now: int, reason: str) -> None:
    if version is not None and version.content is not None:
        state.displaced.append({**row(version), "time": now, "reason": reason})


def resolve(ctx: Context, *, local, incoming, **kwargs):
    return core.resolve(local, incoming)


def classify(ctx: Context, *, incoming: codec.Entry, peer: str, seq: int, **kwargs) -> str:
    meta = incoming.metadata
    key = path_key(meta["source"], meta["path"])
    state = ctx.state.read()
    old = state.ledger.get(key)
    data, mtime = ctx.source(meta["source"], meta["path"])
    # A pending resolution's ledger vector is max(L,R); publish owns +me.
    local = core.local_version(from_row(old), data, mtime, ctx.device_id)
    if local is not None:
        local = core.entry(
            meta["source"],
            meta["path"],
            local.content,
            local.metadata["vv"],
            local.metadata["mtime"],
        )
    bootstrap = not state.meta.get("joined") and old is None and data is not None
    action = core.classify(local, incoming, bootstrap=bootstrap)
    chosen, pending = incoming, False
    if action == "ignore":
        chosen = local
    elif action == "same-sha":
        chosen = core.entry(
            meta["source"],
            meta["path"],
            data,
            core.vector_max(local.metadata["vv"], meta["vv"]),
            mtime,
        )
        # Preserve E12 publication obligation through a same-sha collapse.
        pending = bool(old and old["pending"])
    elif action == "resolve":
        chosen = ctx.call("resolve", local=local, incoming=incoming)
        pending = True
    target = owned_path(ctx.source_roots[meta["source"]], meta["path"])
    wrote = action not in ("ignore", "same-sha") and data != chosen.content
    with ctx.state.transaction() as updated:
        if action in ("fast-forward", "bootstrap-delete", "resolve"):
            if data != chosen.content and (
                action != "resolve"
                or core.resolver_name(meta["path"]) == "newest"
                or incoming.content is None
                or data is None
            ):
                displace(updated, local, ctx.clock.now, action)
            if incoming.content != chosen.content and (
                core.resolver_name(meta["path"]) == "newest" or data is None
            ):
                displace(updated, incoming, ctx.clock.now, "resolution-loser")
        if action != "ignore":
            updated.ledger[key] = row(chosen, pending=pending)
        updated.history.append(
            {
                "operation": "pull-entry",
                "peer": peer,
                "seq": seq,
                "action": action,
                "local": None if local is None else row(local),
                "incoming": row(incoming),
                "result": row(chosen, pending=pending),
                "wrote": wrote,
                "writer": ctx.current_piece if wrote else None,
                "bootstrap": bootstrap,
                "counter_reuse": local is not None
                and local.metadata["vv"] == meta["vv"]
                and local.metadata["sha"] != meta["sha"],
            }
        )
    if wrote:
        if chosen.content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(chosen.content)
            stamp = chosen.metadata["mtime"]
            os.utime(target, (stamp, stamp))
    return action


def snapshot(
    ctx: Context, *, force=False, crash_parts=None, crash_state=False, **kwargs
) -> Outcome:
    reconciled = ctx.call("reconcile", allow_known_unreadable=force)
    if reconciled.status != "applied":
        return reconciled
    result = ctx.call("publish", attended=False, allow_known_unreadable=force, checked=reconciled)
    if result.status not in ("published", "unchanged"):
        return result
    state = ctx.state.read()
    seq = max(map(int, state.own_log), default=0)
    if any(
        n.device_id == ctx.device_id and n.kind == "snapshot" and n.seq == seq
        for n in ctx.names().values()
    ):
        return Outcome("unchanged", seq=seq)
    entries = tuple(
        core.entry(
            value["source"],
            value["path"],
            ctx.source(value["source"], value["path"])[0],
            value["vv"],
            value["mtime"],
        )
        for _, value in sorted(state.ledger.items())
    )
    prev = state.own_log[str(seq)]["hash"] if seq else None
    expected = ctx.context(ctx.device_id, "snapshot", seq)
    payload = codec.encode_snapshot(
        header(ctx, "snapshot", seq, prev, entries), entries, expected=expected
    )
    if not write_record(ctx, "snapshot", seq, payload, crash_parts=crash_parts) or crash_state:
        return Outcome("crashed", seq=seq)
    with ctx.state.transaction() as updated:
        updated.meta["snapshot_seq"] = seq
    ctx.call("presence", changed=True)
    ctx.call("compact", checked=reconciled)
    return Outcome("published", seq=seq, warnings=reconciled.warnings)


def compact(ctx: Context, *, checked=None, **kwargs) -> Outcome:
    checked = checked or ctx.call("reconcile")
    if checked.status != "applied":
        return checked
    snaps = ctx.records(ctx.device_id, "snapshot")
    previous = snaps[-2][0] if len(snaps) > 1 else -1
    state = ctx.state.read()
    removed = False
    for key, name in ctx.names().items():
        if name.device_id != ctx.device_id:
            continue
        own = state.own_log.get(str(name.seq))
        if (name.kind == "snapshot" and name.seq < previous) or (
            name.kind == "commit"
            and name.seq <= previous
            and own is not None
            and ctx.clock.now - own["time"] >= 14 * DAY
        ):
            (ctx.replica_root / key).unlink()
            removed = True
    with ctx.state.transaction() as updated:
        for value in updated.displaced:
            if ctx.clock.now - value["time"] >= 30 * DAY:
                value["expired"] = True
                value["content"] = None
    return Outcome(
        "compacted" if removed else "unchanged", seq=max(map(int, state.own_log), default=0)
    )


def load_snapshot(ctx: Context, peer: str, snapshot_record) -> None:
    seq, _, record = snapshot_record
    for incoming in record.entries:
        ctx.call("classify", incoming=incoming, peer=peer, seq=seq)
    with ctx.state.transaction() as state:
        state.history.append(
            {"operation": "record-applied", "peer": peer, "kind": "snapshot", "seq": seq}
        )
        state.cursors[peer] = {"applied_seq": seq, "hash": record.header["prev"]}
        for other, cursor in sorted(record.header["cursors"].items()):
            existing = state.cursors.get(other, {"applied_seq": 0, "hash": None})
            if cursor > existing["applied_seq"]:
                # The wire snapshot carries seq cursors, not other-peer hashes.
                candidates = [r for r in ctx.records(other, "commit") if r[0] == cursor]
                anchors = [r for r in ctx.records(other, "snapshot") if r[0] == cursor]
                digest = (
                    hashlib.sha256(candidates[0][1]).hexdigest()
                    if candidates
                    else anchors[0][2].header["prev"]
                    if anchors
                    else None
                )
                state.cursors[other] = {"applied_seq": cursor, "hash": digest}


def read(ctx: Context, **kwargs) -> Outcome:
    checked = ctx.call("reconcile")
    if checked.status != "applied":
        return checked
    events, warnings, pending, refusals = [], [], [], []
    changed = False
    names = ctx.names()
    fork_keys = layout.classify_keys(
        sorted(
            p.relative_to(ctx.replica_root).as_posix()
            for p in ctx.replica_root.rglob("*")
            if p.is_file()
        )
    ).forks
    peers = sorted(
        (
            {n.device_id for n in names.values()}
            | {p.split("/")[1] for p in fork_keys if p.startswith("dev/")}
        )
        - {ctx.device_id}
    )
    for peer in peers:
        advertised = 0
        try:
            payload = ctx.load(peer, "presence", 0, names=names)
            if isinstance(payload, bytes):
                advertised = codec.decode_presence(
                    payload, expected=ctx.context(peer, "presence", 0)
                )["published_seq"]
        except envelope.StoreError:
            warnings.append("unreadable peer presence")
        snapshots = ctx.records(peer, "snapshot", names=names)
        latest = snapshots[-1] if snapshots else None
        peer_forks = [p for p in fork_keys if p.startswith("dev/" + peer + "/")]
        fork_seq = min((int(p.split("/")[-1].split(".")[0]) for p in peer_forks), default=None)
        for _ in range(1000):
            state = ctx.state.read()
            cursor = state.cursors.get(peer, {"applied_seq": 0, "hash": None})
            seq = cursor["applied_seq"] + 1
            if fork_seq is not None and seq >= fork_seq:
                refusals.append((peer, "fork"))
                break
            known_seq = max(
                [
                    n.seq
                    for n in names.values()
                    if n.device_id == peer and n.kind in ("commit", "snapshot")
                ]
                # Compaction may remove the slot before its covering snapshot
                # arrives. Keep the already observed gap until it is covered.
                + [cursor["applied_seq"], advertised, cursor.get("gap_seq", 0)]
            )
            if seq > known_seq:
                break
            unreadable = False
            try:
                payload = ctx.load(peer, "commit", seq, names=names)
            except envelope.StoreError:
                unreadable, payload = True, None
            if unreadable:
                warnings.append("unreadable commit; owner: push --snapshot")
            rule = core.reader_rule(
                seq,
                complete=isinstance(payload, bytes),
                unreadable=unreadable,
                snapshot_seq=None if latest is None else latest[0],
            )
            if rule == "snapshot" and fork_seq is not None and latest[0] >= fork_seq:
                refusals.append((peer, "fork"))
                break
            if rule == "snapshot":
                load_snapshot(ctx, peer, latest)
                changed = True
                events.append("snapshot-fallback")
                continue
            if rule == "commit":
                if cursor["hash"] is None and cursor["applied_seq"] > 0:
                    anchors = [r for r in snapshots if r[0] == cursor["applied_seq"]]
                    try:
                        anchor = ctx.load(peer, "commit", cursor["applied_seq"], names=names)
                    except envelope.StoreError:
                        anchor = None
                    if isinstance(anchor, bytes) or anchors:
                        digest = (
                            hashlib.sha256(anchor).hexdigest()
                            if isinstance(anchor, bytes)
                            else anchors[-1][2].header["prev"]
                        )
                        cursor = {**cursor, "hash": digest}
                        with ctx.state.transaction() as updated:
                            updated.cursors[peer] = cursor
                record = codec.decode_commit(payload, expected=ctx.context(peer, "commit", seq))
                if record.header["prev"] != cursor["hash"]:
                    # Indirect snapshot cursors have no chain anchor; wait for one.
                    if cursor["hash"] is None and cursor["applied_seq"] > 0:
                        if latest is not None and latest[0] >= seq:
                            load_snapshot(ctx, peer, latest)
                            events.append("snapshot-fallback")
                            continue
                        since = cursor.get("gap_since", ctx.clock.now)
                        with ctx.state.transaction() as updated:
                            updated.cursors[peer] = {**cursor, "gap_seq": seq, "gap_since": since}
                        if ctx.clock.now - since > DAY:
                            warnings.append("chain anchor gap older than 24 hours")
                        pending.append((peer, seq, "chain-anchor"))
                        break
                    refusals.append((peer, "fork"))
                    break
                changed = True
                for incoming in record.entries:
                    action = ctx.call("classify", incoming=incoming, peer=peer, seq=seq)
                    if action == "resolve":
                        events.append("concurrent-" + core.resolver_name(incoming.metadata["path"]))
                    if action == "bootstrap-delete":
                        events.append("bootstrap-inversion")
                with ctx.state.transaction() as updated:
                    updated.history.append(
                        {"operation": "record-applied", "peer": peer, "kind": "commit", "seq": seq}
                    )
                    updated.cursors[peer] = {
                        "applied_seq": seq,
                        "hash": hashlib.sha256(payload).hexdigest(),
                    }
                continue
            since = cursor.get("gap_since", ctx.clock.now)
            with ctx.state.transaction() as updated:
                updated.cursors[peer] = {**cursor, "gap_seq": seq, "gap_since": since}
            if (
                not unreadable and ctx.clock.now - since > DAY
            ):  # §6 says older than, strictly greater.
                warnings.append("gap older than 24 hours")
            pending.append((peer, seq, rule))
            events.append("gap-pending")
            break
    ctx.call("presence", changed=changed)
    if refusals:
        state = ctx.state.read()
        for refused_peer, _ in refusals:
            since = state.cursors.get(refused_peer, {}).get("gap_since")
            if since is not None and ctx.clock.now - since > DAY:
                warnings.append("gap older than 24 hours")
        peer, reason = refusals[0]
        return Outcome(
            "refused", peer=peer, reason=reason, warnings=tuple(warnings), events=tuple(events)
        )
    if pending:
        peer, seq, reason = pending[0]
        return Outcome(
            "stalled-unreadable" if reason == "stalled-unreadable" else "pending",
            peer=peer,
            gap_seq=seq,
            reason=reason,
            warnings=tuple(warnings),
            events=tuple(events),
        )
    return Outcome("applied", warnings=tuple(warnings), events=tuple(events))


def join(ctx: Context, **kwargs) -> Outcome:
    if ctx.keys is None:
        return Outcome("waiting", reason="fleet-header")
    (ctx.replica_root / ".stignore").write_bytes(STIGNORE)
    candidates = [
        r + (peer,)
        for peer in sorted({n.device_id for n in ctx.names().values()})
        if peer != ctx.device_id
        for r in ctx.records(peer, "snapshot")
    ]
    # Freshest = greatest logical created_at; tie: device id, then sequence.
    if candidates:
        newest = max(candidates, key=lambda r: (r[2].header["created_at"], r[3], r[0]))
        load_snapshot(ctx, newest[3], newest[:3])
    with ctx.state.transaction() as state:
        state.meta.update(joined=True, registered=ctx.clock.iso, snapshot_seq=0)
        state.history.append({"operation": "joined"})
    ctx.call("snapshot", force=True)
    pulled = ctx.call("read")
    return replace(pulled, events=pulled.events + (("join-snapshot",) if candidates else ()))


def rejoin(ctx: Context, *, new_id, **kwargs) -> Outcome:
    layout.record_key(new_id, "presence", 0)
    with ctx.state.transaction() as state:
        state.ledger.clear()
        state.cursors.clear()
        state.own_log.clear()
        state.meta = {"device_id": new_id}
        state.history.append({"operation": "rejoin", "old_id": ctx.device_id, "new_id": new_id})
    ctx.device_id = new_id
    return ctx.call("join")


def retire(ctx: Context, *, target, **kwargs) -> Outcome:
    checked = ctx.call("reconcile")
    if checked.status != "applied":
        return checked
    layout.record_key(target, "presence", 0)  # Validate the identity before path construction.
    if target == ctx.device_id:
        return Outcome("pending", peer=target, reason="cannot-retire-self")
    raw = ctx.load(target, "presence", 0)
    if not isinstance(raw, bytes):
        return Outcome("pending", peer=target, reason="waiting-for-presence")
    target_presence = codec.decode_presence(raw, expected=ctx.context(target, "presence", 0))
    last_active = datetime.fromisoformat(target_presence["last_active"]).timestamp()
    if ctx.clock.now - last_active < 30 * DAY:
        return Outcome("pending", peer=target, reason="retire-age")
    last_seq = max(
        [target_presence["published_seq"]]
        + [
            n.seq
            for n in ctx.names().values()
            if n.device_id == target and n.kind in ("commit", "snapshot")
        ]
    )
    peers = sorted({n.device_id for n in ctx.names().values()})
    for peer in peers:
        if peer == target:
            continue
        raw = ctx.load(peer, "presence", 0)
        if not isinstance(raw, bytes):
            return Outcome("pending", peer=target, reason="waiting-for-presence")
        value = codec.decode_presence(raw, expected=ctx.context(peer, "presence", 0))
        active = (
            peer == ctx.device_id
            or ctx.clock.now - datetime.fromisoformat(value["last_active"]).timestamp() < 30 * DAY
        )
        if active:
            snapshots = ctx.records(peer, "snapshot")
            if not snapshots or snapshots[-1][2].header["cursors"].get(target, 0) < last_seq:
                return Outcome("pending", peer=target, reason="snapshot-coverage")
    directory = owned_path(ctx.replica_root, "dev/" + target)
    if directory.exists():
        shutil.rmtree(directory)
    return Outcome("retired", seq=last_seq, peer=target, events=("retire",))
