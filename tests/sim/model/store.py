"""Test-only I/O shell for store-v2 §2/4/6/8/9. Never promoted into product code."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from mind_meld.store import envelope, layout
from mind_meld.store import format as codec

from ..store_fleet import DAY, PASSPHRASE, STIGNORE
from ..transport import owned_path
from . import core
from .state import StateSeam, applied_key, from_row, intent_key, path_key, row


class PublishError(RuntimeError):
    """A publisher failure that must be loud (name allocation exhausted)."""


@dataclass(frozen=True)
class FamilyRecord:
    """One complete, readable family at a slot, decoded and digested."""

    device: str
    kind: str
    seq: int
    family: str
    payload: bytes
    record: codec.Record
    digest: str
    parts: int


@dataclass(frozen=True)
class Slot:
    """Every family a slot holds, judged from names; only ``complete`` was opened."""

    complete: tuple[FamilyRecord, ...] = ()
    incomplete: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()


MAX_NAME_ATTEMPTS = 3
# How long replaced bytes stay restorable in ``displaced`` (the retire activity
# window further down is a different 30 days).
DISPLACED_RETENTION = 30 * DAY


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
    # Fleet-held inputs, never on a device disk: a fresh record family per
    # publication, and whether Syncthing has nothing left to deliver here.
    family_source: Callable[[], str] | None = None
    folder_synced: Callable[[], bool] = lambda: True
    # Per-action facts reconcile derives for publish/read/presence: ``in_flight``
    # (claimed seq whose own records have not all arrived), ``skip_ahead`` (the folder is
    # synced, so an attended push may skip ahead), ``lost`` (own records vanished from
    # storage: heal) and ``torn`` (a proven torn own slot). Reset by every reconcile.
    own: dict = field(default_factory=dict)

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

    def names(self, keys=None) -> dict[str, layout.RecordName]:
        return {
            key: parsed
            for key in (self.file_keys() if keys is None else keys)
            if (parsed := layout.parse_record_key(key)) is not None
        }

    def load(self, device: str, kind: str, seq: int, family: str | None = None, *, names=None):
        """Open ONE family's parts. ENOENT is rescued only at this read site.

        Presence has no family. Parts of other families at the slot are never mixed in.
        """
        parts = []
        for key, parsed in (self.names() if names is None else names).items():
            if (parsed.device_id, parsed.kind, parsed.seq, parsed.family) == (
                device,
                kind,
                seq,
                family,
            ):
                try:
                    data = (self.replica_root / key).read_bytes()
                except FileNotFoundError:
                    continue
                expected = envelope.RecordContext(
                    self.keys.fleet_id, device, kind, seq, parsed.part, parsed.nparts
                )
                parts.append((expected, data))
        return codec.open_record(parts, self.keys, expected=self.context(device, kind, seq))

    def view(self, keys=None) -> "View":
        return View(self, keys)

    def source(self, source: str, path: str) -> tuple[bytes | None, float]:
        target = owned_path(self.source_roots[source], path)
        try:
            return target.read_bytes(), target.stat().st_mtime
        except FileNotFoundError:
            return None, 0


class View:
    """Read-only per-action view of one replica.

    Names are grouped by ``(device, kind, seq, family)`` through
    ``layout.group_record_names``; completeness is judged from the names and only
    complete families are opened (and remembered), so a torn family costs no
    decryption on the hot path. A family that fails to open, or whose name set
    carries differing part counts, is unreadable by name. Build one view after any
    write the action made: it never refreshes itself.
    """

    def __init__(self, ctx: Context, keys=None):
        self.ctx = ctx
        self.names = ctx.names(keys)
        self.groups = layout.group_record_names(self.names.items())
        self._families: dict[tuple[str, str, int], list[str]] = {}
        for device, kind, seq, family in self.groups:
            self._families.setdefault((device, kind, seq), []).append(family)
        self._slots: dict[tuple[str, str, int], Slot] = {}

    def devices(self) -> set[str]:
        return {device for device, _, _, _ in self.groups}

    def seqs(self, device: str, kind: str) -> list[int]:
        return sorted({seq for (d, k, seq) in self._families if (d, k) == (device, kind)})

    def slot(self, device: str, kind: str, seq: int) -> Slot:
        identity = (device, kind, seq)
        if identity not in self._slots:
            complete, incomplete, unreadable = [], [], []
            for family in sorted(self._families.get(identity, ())):
                items = self.groups[(device, kind, seq, family)]
                counts = {name.nparts for _, name in items}
                if len(counts) != 1:
                    unreadable.append(family)  # One family name has exactly one part count.
                elif {name.part for _, name in items} != set(range(1, next(iter(counts)) + 1)):
                    incomplete.append(family)
                else:
                    opened = self._open(device, kind, seq, family, items)
                    if isinstance(opened, FamilyRecord):
                        complete.append(opened)
                    else:
                        (incomplete if opened == "incomplete" else unreadable).append(family)
            self._slots[identity] = Slot(tuple(complete), tuple(incomplete), tuple(unreadable))
        return self._slots[identity]

    def _open(self, device, kind, seq, family, items):
        ctx = self.ctx
        try:
            payload = ctx.load(device, kind, seq, family, names=dict(items))
            if not isinstance(payload, bytes):
                return "incomplete"  # A part vanished in a replacement window.
            decoder = codec.decode_commit if kind == "commit" else codec.decode_snapshot
            record = decoder(payload, expected=ctx.context(device, kind, seq))
        except envelope.StoreError:
            return "unreadable"
        return FamilyRecord(
            device,
            kind,
            seq,
            family,
            payload,
            record,
            hashlib.sha256(payload).hexdigest(),
            max(name.nparts for _, name in items),
        )

    def latest_snapshot(self, device: str) -> FamilyRecord | None:
        """Newest complete snapshot by (seq, created_at, family)."""
        return max(
            self.records(device, "snapshot"),
            key=lambda r: (r.seq, r.record.header["created_at"], r.family),
            default=None,
        )

    def records(self, device: str, kind: str, errors: list | None = None) -> list[FamilyRecord]:
        """Complete families in (seq, family) order; unreadable ones go to ``errors``."""
        result = []
        for seq in self.seqs(device, kind):
            slot = self.slot(device, kind, seq)
            result.extend(slot.complete)
            if errors is not None and not slot.complete:
                # Alone at a slot an unreadable family stalls; beside a complete one it
                # is ignored (the diag warning belongs to the caller).
                errors.extend((seq, family) for family in slot.unreadable)
        return result


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


@dataclass(frozen=True)
class Written:
    family: str
    linked: int
    total: int

    @property
    def complete(self) -> bool:
        return self.linked == self.total


def write_record(ctx: Context, kind: str, seq: int, payload: bytes, *, crash_parts=None) -> Written:
    """Link every part of one fresh family, recording a write-ahead intent row first.

    One intent row per attempted family, written before its first part is linked
    and left in place until the publishing transaction clears it: a row that
    survives a crash proves this device started that family. ``crash_parts`` stops
    after that many parts. On ``EEXIST`` for any part the parts already linked stay
    (a torn family of this device's own), a fresh family is drawn and the same
    sealed parts are linked under it; after ``MAX_NAME_ATTEMPTS`` the failure is loud.
    """
    parts = tuple(
        codec.seal_record(payload, ctx.keys, expected=ctx.context(ctx.device_id, kind, seq))
    )
    for _ in range(MAX_NAME_ATTEMPTS):
        family = ctx.family_source()
        with ctx.state.transaction() as state:
            state.intents[intent_key(kind, seq, family)] = {
                "kind": kind,
                "seq": seq,
                "family": family,
                "nparts": len(parts),
            }
        linked = 0
        try:
            for index, (part, data) in enumerate(parts):
                if crash_parts is not None and index >= crash_parts:
                    break
                key = layout.record_key(
                    ctx.device_id, kind, seq, part=part.part, nparts=part.nparts, family=family
                )
                target = owned_path(ctx.replica_root, key)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.parent / f".mm-tmp-publish-{part.part}"
                temp.write_bytes(data)
                try:
                    os.link(temp, target)
                finally:
                    temp.unlink()
                linked += 1
        except FileExistsError:
            continue
        return Written(family, linked, len(parts))
    raise PublishError(
        f"could not allocate a fresh record name for seq {seq} after {MAX_NAME_ATTEMPTS} "
        "attempts; another writer may share this device id; run `mm diag`"
    )


DOCS_ANCHOR = "docs/designs/store-v2.md#9-restore-crash-and-clone-safety"
# Verbatim owner-side texts, one per §9 row; every text is anchored at DOCS_ANCHOR in
# the design doc. Peer names would go through ``safety`` in the product.
OWNER_TEXTS = {
    "fork": (
        "a conflicting copy of this device's log or of fleet.json exists; "
        "run `mm diag`, then `mm rejoin` on this Mac"
    ),
    "fork-restore": (
        "this Mac republished records another copy of it already published "
        "(restored from a backup?); run `mm rejoin` on this Mac"
    ),
    "fork-clone": (
        "another machine is using this device id; run `mm rejoin` on each machine using it"
    ),
    "own-records-lost": (
        "records this device published are missing from storage; the next `mm push` "
        "publishes a full snapshot (about the size of this device's synced data)"
    ),
    "own-records-in-flight": (
        "this device's records through push #{claimed} have not arrived yet "
        "(restored from a backup?); waiting"
    ),
    "own-records-in-flight-folder": (
        "this device's records have not arrived yet (restored from a backup?); "
        "waiting for Syncthing to restore them"
    ),
    "torn-own-slot": "the last push was interrupted; the next push completes it",
    "missing-own-directory": (
        "this device's folder is gone from storage and Syncthing has nothing left to "
        "deliver (retired?); its newest publish was {date}; run `mm rejoin`"
    ),
}


def owner_text(code: str, **fields) -> str:
    """The row's text plus the pointer every §9 message ends with."""
    return OWNER_TEXTS[code].format(**fields) + f" See {DOCS_ANCHOR}."


def refuse(ctx: Context, reason: str, **fields) -> Outcome:
    with ctx.state.transaction() as state:
        state.meta["refused"] = reason
        aged_gap = any(
            "gap_since" in cursor and ctx.clock.now - cursor["gap_since"] > DAY
            for cursor in state.cursors.values()
        )
    return Outcome(
        "refused",
        reason=reason,
        warnings=(owner_text(reason, **fields),)
        + (("gap older than 24 hours",) if aged_gap else ()),
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


def newest_publish_date(state) -> str:
    rows = [r for r in state.own_log.values() if r.get("created_at")]
    return max(rows, key=lambda r: r["time"])["created_at"][:10] if rows else "unknown"


def claimed_seq(ctx: Context, view: View, own_published: int) -> int:
    """The highest own seq anyone says exists: this device's own presence, every peer's
    ``applied[me]`` receipt and every complete peer snapshot's ``cursors[me]``."""
    claimed = own_published
    for peer in sorted(view.devices() - {ctx.device_id}):
        try:
            payload = ctx.load(peer, "presence", 0, names=view.names)
            if isinstance(payload, bytes):
                value = codec.decode_presence(payload, expected=ctx.context(peer, "presence", 0))
                claimed = max(claimed, value["applied"].get(ctx.device_id, 0))
        except envelope.StoreError:
            pass
        for snapshot in view.records(peer, "snapshot"):
            claimed = max(claimed, snapshot.record.header["cursors"].get(ctx.device_id, 0))
    return claimed


def own_records_lost(ctx: Context, view: View, state) -> bool:
    """Heal trigger: the own snapshot at ``meta.snapshot_seq`` is no longer complete in
    storage, or a family ``own_log`` recorded above it has vanished or gone incomplete.
    A seq with no ``own_log`` row is a hole replay has not reached: its records are in
    flight and restored-device detection owns the wait, so heal never snapshots over
    it (the snapshot's ledger never incorporated that record, yet its ``prev`` chain
    would mark it covered for readers). A proven torn slot is not a loss. Commits at
    or below the snapshot are compacted by design and never trigger heal."""
    floor = state.meta.get("snapshot_seq", 0)
    if not any(r.seq == floor for r in view.records(ctx.device_id, "snapshot")):
        return True
    head = max(map(int, state.own_log), default=0)
    for seq in range(floor + 1, head + 1):
        row = state.own_log.get(str(seq))
        if row is None:
            continue  # A hole: in flight, never a heal.
        slot = view.slot(ctx.device_id, "commit", seq)
        if slot.complete:
            if not any(f.digest == row["hash"] for f in slot.complete):
                return True  # The family this device recorded is gone.
            continue
        if own_slot(ctx, state, view, "commit", seq)[1] != "torn":
            return True
    return False


def reconcile(ctx: Context, *, allow_known_unreadable=False, **kwargs) -> Outcome:
    """§9: refusal first, then row 2 (own directory), own-snapshot and own-commit replay,
    then restored-device detection, then heal and torn-slot facts for the publisher."""
    ctx.own = {}
    paths = ctx.file_keys()
    view = ctx.view(paths)
    names = view.names
    forks = layout.classify_keys(paths).forks
    own_prefix = "dev/" + ctx.device_id + "/"
    fleet_fork = any(p.startswith("fleet.sync-conflict-") for p in forks)
    own_fork = any(p.startswith(own_prefix) for p in forks)
    if fleet_fork or own_fork:
        return refuse(ctx, core.reconcile_rule(fleet_fork=fleet_fork, own_fork=own_fork))
    if ctx.keys is None:
        if ctx.state.read().meta.get("joined") and not ctx.folder_synced():
            return Outcome("pending", reason="fleet-header")  # Refilling from peers.
        return Outcome("waiting", reason="fleet-header")
    foreign, own_published = False, 0
    try:
        raw_presence = ctx.load(ctx.device_id, "presence", 0, names=names)
        if isinstance(raw_presence, bytes):
            value = codec.decode_presence(
                raw_presence, expected=ctx.context(ctx.device_id, "presence", 0)
            )
            foreign = value["host"] != ctx.host
            own_published = 0 if foreign else value["published_seq"]
    except envelope.StoreError:
        return Outcome(
            "stalled-unreadable",
            peer=ctx.device_id,
            gap_seq=0,
            warnings=("unreadable own presence",),
        )
    unreadable = []
    commits = view.records(ctx.device_id, "commit", errors=unreadable)
    beside = [
        seq
        for seq in view.seqs(ctx.device_id, "commit")
        if view.slot(ctx.device_id, "commit", seq).complete
        and view.slot(ctx.device_id, "commit", seq).unreadable
    ]
    foreign |= any(c.record.header["host"] != ctx.host for c in commits)
    # A snapshot-only clone is a clone: an own snapshot from another host refuses too,
    # before own-snapshot recovery could adopt it as this device's anchor.
    foreign |= any(
        c.record.header["host"] != ctx.host for c in view.records(ctx.device_id, "snapshot")
    )
    state = ctx.state.read()
    reason = core.reconcile_rule(fleet_fork=fleet_fork, own_fork=own_fork, foreign_host=foreign)
    if reason:
        return refuse(ctx, reason)
    if state.own_log and not any(n.device_id == ctx.device_id for n in names.values()):
        # Row 2: while Syncthing still has items for this device the folder is refilling
        # (a restored backup); only a synced folder that still lacks dev/<me>/ is a
        # retirement.
        if not ctx.folder_synced():
            return Outcome(
                "pending",
                peer=ctx.device_id,
                reason="own-records-in-flight",
                warnings=(owner_text("own-records-in-flight-folder"),),
            )
        return refuse(ctx, core.reconcile_rule(own_missing=True), date=newest_publish_date(state))
    warnings = [unreadable_beside_text(ctx.device_id, seq) for seq in beside]
    own_snapshots = view.records(ctx.device_id, "snapshot")
    latest_snapshot = max((c.seq for c in own_snapshots), default=-1)
    if unreadable:
        # Trusted = this device recorded THAT family at the seq; keyed by family, so a
        # different unreadable family alone at a slot stalls instead of being waved through.
        trusted = all(
            state.own_log.get(str(seq), {}).get("family") == family for seq, family in unreadable
        )
        # Only a trusted unreadable family can be healed by a snapshot; an untrusted one
        # stalls even a forced snapshot, so its remedy must not name that command.
        warnings.append(
            "unreadable own commit; owner: push --snapshot"
            if trusted
            else "unreadable own commit this device did not record; run `mm diag`, "
            "then `mm rejoin` if it persists"
        )
        if not trusted or (
            not allow_known_unreadable and max(seq for seq, _ in unreadable) > latest_snapshot
        ):
            return Outcome(
                "stalled-unreadable",
                peer=ctx.device_id,
                gap_seq=unreadable[0][0],
                warnings=tuple(warnings),
            )
    # Own-snapshot recovery: a complete own snapshot beyond own_log loads like a peer's,
    # anchors own_log at its seq and prev, and only later own commits replay after it.
    covered = 0
    newest = view.latest_snapshot(ctx.device_id)
    if newest is not None and newest.seq > max(map(int, state.own_log), default=0):
        load_snapshot(ctx, view, ctx.device_id, newest)
        created = newest.record.header["created_at"]
        with ctx.state.transaction() as updated:
            updated.own_log[str(newest.seq)] = {
                "hash": newest.record.header["prev"],
                "family": None,
                "prev": None,
                "created_at": created,
                "time": int(datetime.fromisoformat(created).timestamp()),
                "bytes": 0,
                "parts": 0,
            }
            # Record every complete own snapshot's chosen family, not only the newest:
            # compaction may delete only families this device can prove it wrote, and an
            # older snapshot this device published must not become undeletable forever
            # just because recovery loaded a newer one.
            recorded = updated.meta.setdefault("snapshots", {})
            for other in own_snapshots:
                winner = recorded.get(str(other.seq))
                if winner is None or other is max(
                    (r for r in own_snapshots if r.seq == other.seq),
                    key=lambda r: (r.record.header["created_at"], r.family),
                ):
                    recorded[str(other.seq)] = other.family
            updated.intents.pop(intent_key("snapshot", newest.seq, newest.family), None)
            updated.history.append({"operation": "own-snapshot-recovery", "seq": newest.seq})
        covered, state = newest.seq, ctx.state.read()
    for found in commits:
        seq, record = found.seq, found.record
        if seq <= covered:
            continue  # Covered by the own snapshot just loaded.
        known = state.own_log.get(str(seq))
        if known and known["hash"] != found.digest:
            return refuse(ctx, core.reconcile_rule(chain_fork=True))
        if known:
            continue
        # Replay is contiguous: it stops at the first seq holding no complete own
        # family, so a partially redelivered log is never jumped. The hole's records
        # are in flight; restored-device detection below owns the wait (and a later
        # heal snapshot can never claim to incorporate a record replay skipped).
        if seq > max(map(int, state.own_log), default=covered) + 1:
            break
        previous = state.own_log.get(str(seq - 1))
        if seq > 1 and previous and record.header["prev"] != previous["hash"]:
            return refuse(ctx, core.reconcile_rule(chain_fork=True))
        for incoming in record.entries:
            ctx.call("classify", incoming=incoming, peer=ctx.device_id, seq=seq)
        with ctx.state.transaction() as updated:
            updated.history.append(
                {
                    "operation": "record-applied",
                    "peer": ctx.device_id,
                    "kind": "commit",
                    "seq": seq,
                    "family": found.family,
                }
            )
            updated.own_log[str(seq)] = {
                "hash": found.digest,
                "family": found.family,
                "prev": record.header["prev"],
                "created_at": record.header["created_at"],
                "time": int(datetime.fromisoformat(record.header["created_at"]).timestamp()),
                "bytes": len(found.payload),
                "parts": found.parts,
            }
            if seq > updated.cursors.get(ctx.device_id, {"applied_seq": 0})["applied_seq"]:
                updated.cursors[ctx.device_id] = {"applied_seq": seq, "hash": found.digest}
            updated.intents.pop(intent_key("commit", seq, found.family), None)
        state = ctx.state.read()
        with ctx.state.transaction() as updated:
            updated.history.append({"operation": "own-replay", "seq": seq})
    if latest_snapshot > ctx.state.read().meta.get("snapshot_seq", 0):
        with ctx.state.transaction() as updated:
            updated.meta["snapshot_seq"] = latest_snapshot
            updated.history.append({"operation": "snapshot-recovery", "seq": latest_snapshot})
    state = ctx.state.read()
    head = max(map(int, state.own_log), default=0)
    # Restored-device detection: someone says records through ``claimed`` exist but they
    # are not all here yet (after replay and snapshot recovery they would be in own_log).
    claimed = claimed_seq(ctx, view, own_published)
    if claimed > head:
        ctx.own["in_flight"] = claimed
        # Receipts can name seqs, they cannot invent them: never skip to a seq the
        # record grammar cannot name, whatever a peer's cursor claims.
        ctx.own["skip_ahead"] = ctx.folder_synced() and claimed < layout.MAX_SEQ
    if own_records_lost(ctx, view, state):
        ctx.own["lost"] = True
    torn = _torn(ctx, state, view, head)
    if torn is not None:
        ctx.own["torn"] = torn
    elif claimed <= head:
        # No claim covers the next slot, but an incomplete family this device cannot
        # prove it wrote sits there: publishing is wedged until it completes or the
        # operator acts. Pull must say so instead of reporting clean.
        slot, slot_state = own_slot(ctx, state, view, "commit", head + 1)
        if slot_state == "in-flight" and not slot.complete:
            ctx.own["blocked"] = head + 1
    return Outcome("applied", warnings=tuple(warnings))


def _torn(ctx: Context, state, view: View, head: int) -> int | None:
    """The seq of a proven torn own slot (next commit slot, or the current snapshot)."""
    if own_slot(ctx, state, view, "commit", head + 1)[1] == "torn":
        return head + 1
    slot, slot_state = own_slot(ctx, state, view, "snapshot", head)
    return head if not slot.complete and slot_state == "torn" else None


def presence(ctx: Context, *, changed=False, **kwargs) -> Outcome:
    if ctx.own.get("in_flight") is not None:
        return Outcome("unchanged")  # Never rewrite presence while own records are in flight.
    state = ctx.state.read()
    seq = max(map(int, state.own_log), default=0)
    if not changed and ctx.clock.now - state.meta.get("presence_time", 0) < 3600:
        # An own presence behind own state needs only a rewrite, never a heal.
        try:
            raw = ctx.load(ctx.device_id, "presence", 0)
            behind = isinstance(raw, bytes) and (
                codec.decode_presence(raw, expected=ctx.context(ctx.device_id, "presence", 0))[
                    "published_seq"
                ]
                < seq
            )
        except envelope.StoreError:
            behind = False
        if not behind:
            return Outcome("unchanged")
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


def own_slot(ctx: Context, state, view: View, kind: str, seq: int) -> tuple[Slot, str]:
    """Classify this device's next slot: ``free``, ``torn`` or ``in-flight``.

    ``torn``: no complete family and every incomplete one is named by an intent row,
    so this device provably started it and a fresh family may take the slot.
    ``in-flight``: some incomplete family is not provably this device's own (for
    example records still arriving after a restore): never write at that seq.
    """
    slot = view.slot(ctx.device_id, kind, seq)
    if not slot.incomplete:
        return slot, "free"
    proven = [intent_key(kind, seq, family) in state.intents for family in slot.incomplete]
    return slot, "torn" if all(proven) else "in-flight"


def in_flight(seq: int, ctx: Context) -> Outcome:
    return Outcome(
        "pending",
        peer=ctx.device_id,
        gap_seq=seq,
        reason="own-records-in-flight",
        warnings=(owner_text("own-records-in-flight", claimed=seq),),
    )


def newest_complete_own_digest(ctx: Context, view: View, state) -> str:
    """``prev`` of a skip-ahead commit: the newest own family still complete in storage,
    else the newest own_log row; a device with neither has nothing readers could check."""
    commits = view.records(ctx.device_id, "commit")
    if commits:
        return commits[-1].digest
    if state.own_log:
        return state.own_log[str(max(map(int, state.own_log)))]["hash"]
    return "0" * 64


def publish(
    ctx: Context,
    *,
    crash_parts=None,
    crash_state=False,
    attended=True,
    allow_known_unreadable=False,
    checked=None,
    skip_ok=None,
    **kwargs,
) -> Outcome:
    """§5. Order after reconcile: own-fork refusal (done), restored-device detection,
    torn-slot handling, then heal. An attended push may skip ahead of records that never
    return once the folder is synced; autopush stays pending."""
    reconciled = checked or ctx.call("reconcile", allow_known_unreadable=allow_known_unreadable)
    if reconciled.status != "applied":
        return reconciled
    skip_ok = attended if skip_ok is None else skip_ok
    state = ctx.state.read()
    head = max(map(int, state.own_log), default=0)
    claimed, lost = ctx.own.get("in_flight"), ctx.own.get("lost")
    skip = False
    if claimed is not None:
        if not (skip_ok and ctx.own.get("skip_ahead")):
            return in_flight(claimed, ctx)
        skip = True
    seq = claimed + 1 if skip else head + 1
    slot, slot_state = own_slot(ctx, state, ctx.view(), "commit", seq)
    if skip and (slot.complete or slot.incomplete):
        # The claim lagged storage: a family already occupies the skip target (still
        # arriving, or complete but unreplayed behind the hole). Never write over it;
        # the claim grows once it lands.
        return in_flight(seq, ctx)
    if slot_state == "in-flight":
        return in_flight(seq, ctx)
    torn = slot_state == "torn"
    if torn and not ctx.folder_synced():
        # An intent row proves some copy of this state started the family; a backup
        # can carry the row back after the slot was later closed. While redelivery
        # may still complete the slot, superseding would fork this device, so torn
        # closure waits for the folder to sync, exactly like skip-ahead.
        return in_flight(seq, ctx)
    entries = authored_entries(ctx)
    heal_text = (owner_text("own-records-lost"),) if lost else ()
    if not entries and not torn and not skip:
        ctx.call(
            "presence",
            changed=bool(reconciled.warnings)
            or any(
                r.get("operation") == "snapshot-recovery" for r in ctx.state.read().history[-1:]
            ),
        )
        healed = ctx.call("snapshot", force=True) if attended and lost else None
        return Outcome(
            "published" if healed and healed.status == "published" else "unchanged",
            seq=healed.seq if healed and healed.status == "published" else None,
            warnings=reconciled.warnings + heal_text + fork_warnings(ctx),
            events=("heal-snapshot",) if healed and healed.status == "published" else (),
        )
    # A proven torn slot closes with a fresh complete family even when the unchanged
    # ledger and files author nothing, and a skip-ahead commit exists to anchor a forced
    # snapshot: peers wait on those seqs otherwise.
    prev = (
        newest_complete_own_digest(ctx, ctx.view(), state)
        if skip
        else (state.own_log[str(seq - 1)]["hash"] if seq > 1 else None)
    )
    expected = ctx.context(ctx.device_id, "commit", seq)
    payload = codec.encode_commit(
        header(ctx, "commit", seq, prev, entries), entries, expected=expected
    )
    written = write_record(ctx, "commit", seq, payload, crash_parts=crash_parts)
    if not written.complete or crash_state:
        return Outcome("crashed", seq=seq)
    digest = hashlib.sha256(payload).hexdigest()
    with ctx.state.transaction() as updated:
        for incoming in entries:
            updated.ledger[path_key(incoming.metadata["source"], incoming.metadata["path"])] = row(
                incoming
            )
        updated.own_log[str(seq)] = {
            "hash": digest,
            "family": written.family,
            "prev": prev,
            "time": ctx.clock.now,
            "created_at": ctx.clock.iso,
            "bytes": len(payload),
            "parts": written.total,
        }
        updated.cursors[ctx.device_id] = {"applied_seq": seq, "hash": digest}
        updated.history.append({"operation": "publish", "seq": seq, "family": written.family})
        updated.intents.pop(intent_key("commit", seq, written.family), None)
    ctx.own["in_flight"] = None  # This device has now published past what was claimed.
    ctx.call("presence", changed=True)
    forced = skip or (attended and lost)
    if attended and not forced:
        complete = ctx.view().records(ctx.device_id, "snapshot")
        last_seq, last_bytes = (complete[-1].seq, len(complete[-1].payload)) if complete else (0, 0)
        new_rows = [r for n, r in sorted(ctx.state.read().own_log.items()) if int(n) > last_seq]
        forced = core.snapshot_due(sum(r["bytes"] for r in new_rows), len(new_rows), last_bytes)
    healed = None
    if forced:
        healed = ctx.call("snapshot", force=True)
    events = (
        (("torn-supersede",) if torn else ())
        + (("skip-ahead",) if skip else ())
        + (("heal-snapshot",) if lost and healed is not None else ())
    )
    return Outcome(
        "published",
        seq=seq,
        warnings=reconciled.warnings + (() if skip else heal_text) + fork_warnings(ctx),
        events=events,
    )


def displace(
    state, version: codec.Entry | None, now: int, reason: str, *, cause=None, peer=None
) -> None:
    """Keep replaced bytes 30 days. ``cause="fork"`` + ``peer`` tags a version a peer's
    fork displaced, so the repeating fork warning can name and count it."""
    if version is not None and version.content is not None:
        value = {**row(version), "time": now, "reason": reason}
        if cause is not None:
            value.update(cause=cause, peer=peer)
        state.displaced.append(value)


def resolve(ctx: Context, *, local, incoming, **kwargs):
    return core.resolve(local, incoming)


def classify(
    ctx: Context, *, incoming: codec.Entry, peer: str, seq: int, fork=False, **kwargs
) -> str:
    """§4 table. ``fork``: the entry comes from a family at or after a peer's first fork
    seq, so an incoming version this device dominates is kept in ``displaced`` instead
    of being dropped (counter reuse after a restore can make a post-restore edit look
    derived from the pre-restore one)."""
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
        if action == "ignore" and fork:
            displace(updated, incoming, ctx.clock.now, "ignored-fork", cause="fork", peer=peer)
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
    result = ctx.call(
        "publish", attended=False, skip_ok=True, allow_known_unreadable=force, checked=reconciled
    )
    if result.status not in ("published", "unchanged"):
        return result
    state = ctx.state.read()
    seq = max(map(int, state.own_log), default=0)
    slot, slot_state = own_slot(ctx, state, ctx.view(), "snapshot", seq)
    if slot.complete:
        return Outcome("unchanged", seq=seq)
    if slot_state == "in-flight":
        return in_flight(seq, ctx)
    if slot_state == "torn" and not ctx.folder_synced():
        return in_flight(seq, ctx)  # Torn closure waits for sync, as in publish.
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
    written = write_record(ctx, "snapshot", seq, payload, crash_parts=crash_parts)
    if not written.complete or crash_state:
        return Outcome("crashed", seq=seq)
    with ctx.state.transaction() as updated:
        updated.meta["snapshot_seq"] = seq
        updated.meta.setdefault("snapshots", {})[str(seq)] = written.family
        updated.intents.pop(intent_key("snapshot", seq, written.family), None)
    ctx.call("presence", changed=True)
    ctx.call("compact", checked=reconciled)
    return Outcome(
        "published",
        seq=seq,
        warnings=reconciled.warnings,
        events=("torn-supersede",) if slot_state == "torn" else (),
    )


def compact(ctx: Context, *, checked=None, **kwargs) -> Outcome:
    checked = checked or ctx.call("reconcile")
    if checked.status != "applied":
        return checked
    seqs = sorted({r.seq for r in ctx.view().records(ctx.device_id, "snapshot")})
    previous = seqs[-2] if len(seqs) > 1 else -1
    state = ctx.state.read()
    removed = False
    # Only an own family this device can prove it wrote may be deleted: the family
    # named in own_log (a commit) or meta.snapshots (a snapshot), or one an intent row
    # names (a torn or superseded attempt). Any other own family, at any seq, is kept.
    proven = {(v["kind"], v["seq"], v["family"]) for v in state.intents.values()}
    for seq, row_ in state.own_log.items():
        if row_.get("family"):
            proven.add(("commit", int(seq), row_["family"]))
    for seq, family in state.meta.get("snapshots", {}).items():
        proven.add(("snapshot", int(seq), family))
    # Commits an own snapshot covered (recovery anchored own_log past them without a
    # row each) are proven by that snapshot's prev chain, digest for digest; without
    # this they could never be deleted after a restore older than the floor.
    view = ctx.view()
    chain = covered_chain(view, ctx.device_id, state.cursors.get(ctx.device_id, {}).get("snapshot"))
    chain_times = {}
    for seq, digest in chain.items():
        for found in view.slot(ctx.device_id, "commit", seq).complete:
            if found.digest == digest:
                proven.add(("commit", seq, found.family))
                chain_times[seq] = int(
                    datetime.fromisoformat(found.record.header["created_at"]).timestamp()
                )
    for key, name in ctx.names().items():
        if name.device_id != ctx.device_id or (name.kind, name.seq, name.family) not in proven:
            continue
        own = state.own_log.get(str(name.seq))
        authored = own["time"] if own is not None else chain_times.get(name.seq)
        if (name.kind == "snapshot" and name.seq < previous) or (
            name.kind == "commit"
            and name.seq <= previous
            and authored is not None
            and ctx.clock.now - authored >= 14 * DAY
        ):
            (ctx.replica_root / key).unlink()
            removed = True
    remaining = {
        (n.kind, n.seq, n.family) for n in ctx.names().values() if n.device_id == ctx.device_id
    }
    with ctx.state.transaction() as updated:
        # An intent row whose family no longer exists in storage has nothing left to prove.
        for key, value in list(updated.intents.items()):
            if (value["kind"], value["seq"], value["family"]) not in remaining and (
                value["seq"] <= previous
            ):
                del updated.intents[key]
        for value in updated.displaced:
            if ctx.clock.now - value["time"] >= DISPLACED_RETENTION:
                value["expired"] = True
                value["content"] = None
    return Outcome(
        "compacted" if removed else "unchanged", seq=max(map(int, state.own_log), default=0)
    )


GAP_KEYS = ("gap_seq", "gap_since")


def moved_cursor(old: dict | None, **fields) -> dict:
    """A cursor update keeps ``fork`` and ``snapshot`` and drops a stale gap."""
    kept = {k: v for k, v in (old or {}).items() if k not in GAP_KEYS}
    return {**{"applied_seq": 0, "hash": None}, **kept, **fields}


def load_snapshot(ctx: Context, view: View, peer: str, found: FamilyRecord) -> None:
    """Fallback rule: classify every entry, set this peer's cursor, raise the others'.

    Cursors raised for other devices cover no families: a family of theirs that is not
    applied (or on their own snapshot's prev chain) is still a late family later.
    """
    seq, record = found.seq, found.record
    for incoming in record.entries:
        ctx.call("classify", incoming=incoming, peer=peer, seq=seq)
    with ctx.state.transaction() as state:
        state.history.append(
            {
                "operation": "record-applied",
                "peer": peer,
                "kind": "snapshot",
                "seq": seq,
                "family": found.family,
            }
        )
        state.cursors[peer] = moved_cursor(
            state.cursors.get(peer),
            applied_seq=seq,
            hash=record.header["prev"],
            snapshot={"seq": seq, "prev": record.header["prev"]},
        )
        for other, cursor in sorted(record.header["cursors"].items()):
            existing = state.cursors.get(other, {"applied_seq": 0, "hash": None})
            if cursor > existing["applied_seq"]:
                # The wire snapshot carries seq cursors, not other-peer hashes.
                candidates = view.slot(other, "commit", cursor).complete
                anchors = [r for r in view.records(other, "snapshot") if r.seq == cursor]
                digest = (
                    candidates[0].digest
                    if candidates
                    else anchors[0].record.header["prev"]
                    if anchors
                    else None
                )
                state.cursors[other] = moved_cursor(
                    state.cursors.get(other), applied_seq=cursor, hash=digest
                )


def covered_chain(view: View, peer: str, snapshot: dict | None) -> dict[int, str]:
    """seq -> digest along the prev chain of the peer's own applied snapshot.

    Walks digest-by-digest through retained families; a seq is recorded only when a
    retained family at that seq carries the named digest, and the walk stops at the
    first seq that does not (compacted, or skipped by a skip-ahead commit whose
    ``prev`` names an older seq). Only these families count as covered: a digest is
    never assigned to a seq by arithmetic.
    """
    chain: dict[int, str] = {}
    if not snapshot or snapshot["prev"] is None:
        return chain
    seq, digest = snapshot["seq"], snapshot["prev"]
    while True:
        family = next(
            (f for f in view.slot(peer, "commit", seq).complete if f.digest == digest), None
        )
        if family is None:
            break
        chain[seq] = digest
        if seq == 1 or family.record.header["prev"] is None:
            break
        seq, digest = seq - 1, family.record.header["prev"]
    return chain


def applied_digests(state, peer: str, seq: int | None = None) -> set[str]:
    return {
        r["digest"]
        for r in state.applied.values()
        if r["peer"] == peer and (seq is None or r["seq"] == seq)
    }


def prev_follows(state, peer: str, found: FamilyRecord, chain: dict[int, str]) -> bool | None:
    """§6: ``prev`` must match ANY applied family at seq-1, or the peer's own snapshot
    ``prev`` there; with neither it is not checked (None). The chain-anchor wait is gone."""
    prev = found.record.header["prev"]
    if found.seq == 1:
        return prev is None
    known = applied_digests(state, peer, found.seq - 1)
    if found.seq - 1 in chain:
        known.add(chain[found.seq - 1])
    return None if not known else prev in known


def note_fork(ctx: Context, peer: str, kind: str, seq: int, *, clone=False) -> bool:
    """Record the FIRST fork evidence for a peer (``cursors.fork``). True when new."""
    if "fork" in ctx.state.read().cursors.get(peer, {}):
        return False
    with ctx.state.transaction() as state:
        cursor = state.cursors.setdefault(peer, {"applied_seq": 0, "hash": None})
        cursor["fork"] = {"kind": kind, "seq": seq, "first_seen": ctx.clock.now, "clone": clone}
    return True


def fork_evidence(ctx: Context, view: View, state, peer: str, fork_keys) -> tuple | None:
    """Earliest (seq, kind, clone) evidence in storage, never a refusal.

    ``conflict-copy``: a transport copy of the peer's commit. ``family``: two complete
    families with different payloads (both in storage). ``republished``: an applied
    family is gone from storage and a different digest holds its slot.
    """
    found = []
    for key in fork_keys:
        if key.startswith("dev/" + peer + "/log/"):
            found.append((int(key.rsplit("/", 1)[1].split(".")[0]), "conflict-copy", False))
    chain = covered_chain(view, peer, state.cursors.get(peer, {}).get("snapshot"))
    for seq in view.seqs(peer, "commit"):
        families = view.slot(peer, "commit", seq).complete
        present = {f.digest for f in families}
        known = applied_digests(state, peer, seq) | ({chain[seq]} if seq in chain else set())
        if len(present | known) > 1:
            hosts = {f.record.header["host"] for f in families}
            kind = "republished" if known - present else "family"
            found.append((seq, kind, kind == "family" and len(hosts) > 1))
    return min(found, key=lambda item: (item[0], item[1])) if found else None


def unreadable_beside_text(device: str, seq: int) -> str:
    """An unreadable family beside a complete readable one is ignored, with a diag note."""
    return (
        f"{device} record {seq} has an unreadable copy beside a complete one; "
        "ignored; run `mm diag`"
    )


def retain_ignored_by_fork(ctx: Context, peer: str) -> int:
    """Fork evidence can arrive after an entry was already ignored as dominated.

    Every entry of this peer at or after its first fork seq that classified as
    ``ignore`` (kept in the persisted history receipts) is recorded in ``displaced``
    with ``cause = fork``, once, unless the same bytes are already current or kept.
    """
    state = ctx.state.read()
    fork = state.cursors.get(peer, {}).get("fork")
    if not fork:
        return 0
    # Once only: an expired fork row still names the version, so it is never re-retained.
    kept = {
        (v["source"], v["path"], v["sha"])
        for v in state.displaced
        if v["content"] is not None or (v.get("cause") == "fork" and v.get("peer") == peer)
    }
    pending = []
    for value in state.history:
        if value.get("operation") != "pull-entry" or value["peer"] != peer:
            continue
        incoming = value["incoming"]
        if value["action"] != "ignore" or value["seq"] < fork["seq"] or incoming["deleted"]:
            continue
        identity = (incoming["source"], incoming["path"], incoming["sha"])
        current, _ = ctx.source(incoming["source"], incoming["path"])
        if identity in kept or current == incoming["content"]:
            continue
        kept.add(identity)
        pending.append(from_row(incoming))
    if pending:
        with ctx.state.transaction() as updated:
            for version in pending:
                displace(updated, version, ctx.clock.now, "ignored-fork", cause="fork", peer=peer)
    return len(pending)


def fork_text(peer: str, fork: dict, live: list[dict]) -> str:
    """Reader-side remedy text. ``peer`` is a device id here; the product routes the
    display name through ``safety`` (``safe_terminal_str`` for single-line stderr)."""
    seq, kind = fork["seq"], fork["kind"]
    if kind == "conflict-copy":
        text = (
            f"{peer}'s log has a conflicting copy of record {seq}; run `mm diag`, "
            f"then `mm rejoin` on {peer}"
        )
    elif kind == "republished":
        text = (
            f"{peer} republished an earlier sequence (restored from a backup?); "
            "both versions were kept"
        )
    elif kind == "prev":
        text = (
            f"{peer}'s record {seq} does not follow the record applied before it; both "
            f"versions were kept; run `mm diag`, then `mm rejoin` on {peer}"
        )
    elif fork.get("clone"):
        text = (
            f"{peer} is used by more than one machine (record {seq} exists twice); "
            "run `mm rejoin` on each machine using this id"
        )
    else:
        text = (
            f"{peer} has two different records for push #{seq} (restored from a backup?); "
            f"both versions were kept; run `mm rejoin` on {peer}"
        )
    if live:
        earliest = min(value["time"] for value in live) + DISPLACED_RETENTION
        date = datetime.fromtimestamp(earliest, timezone.utc).date().isoformat()
        text += (
            f"; {len(live)} displaced version{'s' if len(live) != 1 else ''} kept until "
            f"{date}; see `mm displaced` before then"
        )
    return text


def fork_warnings(ctx: Context, *, new=()) -> tuple[str, ...]:
    """Warn on the pull that first sees evidence, then on EVERY push and pull while a
    version the peer's fork displaced is unexpired and unrestored; keep one breadcrumb
    for ``mm status``. Data-at-risk text is never quiet-gated."""
    state = ctx.state.read()
    texts, crumbs = [], {}
    for peer, cursor in sorted(state.cursors.items()):
        fork = cursor.get("fork")
        if not fork:
            continue
        live = [
            v
            for v in state.displaced
            if v.get("cause") == "fork"
            and v.get("peer") == peer
            and not v.get("expired")
            and not v.get("restored")
        ]
        if live or peer in new:
            texts.append(fork_text(peer, fork, live))
        if live:
            crumbs[peer] = {
                "displaced": len(live),
                "earliest": min(v["time"] for v in live) + DISPLACED_RETENTION,
            }
    if state.meta.get("fork_breadcrumb", {}) != crumbs:
        with ctx.state.transaction() as updated:
            if crumbs:
                updated.meta["fork_breadcrumb"] = crumbs
            else:
                updated.meta.pop("fork_breadcrumb", None)
    return tuple(texts)


def apply_commit(
    ctx: Context, peer: str, found: FamilyRecord, chain: dict[int, str], events: list[str]
) -> None:
    """Apply one complete family once. At cursor+1 it moves the cursor; at or below it
    (a second family at the slot, or a late one) the cursor stays and the receipt says so.
    ``prev`` is checked once, here; a failure is fork evidence and never a refusal."""
    state = ctx.state.read()
    forward = found.seq == state.cursors.get(peer, {"applied_seq": 0})["applied_seq"] + 1
    if not forward:
        events.append("late-family")  # At or below the cursor: a second or late family.
    if prev_follows(state, peer, found, chain) is False:
        if note_fork(ctx, peer, "prev", found.seq):
            events.append("fork-evidence")
    fork = ctx.state.read().cursors.get(peer, {}).get("fork")
    taint = fork is not None and found.seq >= fork["seq"]
    for incoming in found.record.entries:
        action = ctx.call(
            "classify",
            incoming=incoming,
            peer=peer,
            seq=found.seq,
            family=found.family,
            fork=taint,
        )
        if action == "resolve":
            events.append("concurrent-" + core.resolver_name(incoming.metadata["path"]))
        if action == "bootstrap-delete":
            events.append("bootstrap-inversion")
    with ctx.state.transaction() as updated:
        updated.history.append(
            {
                "operation": "record-applied",
                "peer": peer,
                "kind": "commit",
                "seq": found.seq,
                "family": found.family,
            }
        )
        updated.applied[applied_key(peer, found.seq, found.family)] = {
            "peer": peer,
            "seq": found.seq,
            "family": found.family,
            "digest": found.digest,
        }
        if forward:
            updated.cursors[peer] = moved_cursor(
                updated.cursors.get(peer), applied_seq=found.seq, hash=found.digest
            )


def read(ctx: Context, **kwargs) -> Outcome:
    """§6 reader. Every complete readable family is applied once, in (seq, family)
    order; a peer fork is evidence and a warning, never a refusal."""
    checked = ctx.call("reconcile")
    if checked.status != "applied":
        return checked
    events, warnings, pending = [], [], []
    changed, new_forks = False, set()
    paths = ctx.file_keys()
    view = ctx.view(paths)
    names = view.names
    fork_keys = layout.classify_keys(paths).forks
    peers = sorted(
        (view.devices() | {p.split("/")[1] for p in fork_keys if p.startswith("dev/")})
        - {ctx.device_id}
    )
    for peer in peers:
        advertised = 0
        had_fork = "fork" in ctx.state.read().cursors.get(peer, {})
        try:
            payload = ctx.load(peer, "presence", 0, names=names)
            if isinstance(payload, bytes):
                advertised = codec.decode_presence(
                    payload, expected=ctx.context(peer, "presence", 0)
                )["published_seq"]
        except envelope.StoreError:
            warnings.append("unreadable peer presence")
        latest = view.latest_snapshot(peer)
        evidence = fork_evidence(ctx, view, ctx.state.read(), peer, fork_keys)
        if evidence and note_fork(ctx, peer, evidence[1], evidence[0], clone=evidence[2]):
            new_forks.add(peer)
            events.append("fork-evidence")
        chain = covered_chain(view, peer, ctx.state.read().cursors.get(peer, {}).get("snapshot"))
        # Late families: complete, at or below the cursor, neither applied nor covered.
        state = ctx.state.read()
        cursor = state.cursors.get(peer, {"applied_seq": 0})
        seen = applied_digests(state, peer)
        for found in view.records(peer, "commit"):
            if (
                found.seq <= cursor["applied_seq"]
                and found.digest not in seen
                and chain.get(found.seq) != found.digest
            ):
                apply_commit(ctx, peer, found, chain, events)
                seen.add(found.digest)
                changed = True
        for _ in range(1000):
            state = ctx.state.read()
            cursor = state.cursors.get(peer, {"applied_seq": 0, "hash": None})
            seq = cursor["applied_seq"] + 1
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
            slot = view.slot(peer, "commit", seq)
            unreadable = not slot.complete and bool(slot.unreadable)
            if unreadable:
                warnings.append("unreadable commit; owner: push --snapshot")
            elif slot.complete and slot.unreadable:
                warnings.append(unreadable_beside_text(peer, seq))
            rule = core.reader_rule(
                seq,
                complete=bool(slot.complete),
                unreadable=unreadable,
                snapshot_seq=None if latest is None else latest.seq,
            )
            if rule == "snapshot":
                load_snapshot(ctx, view, peer, latest)
                chain = covered_chain(view, peer, ctx.state.read().cursors[peer]["snapshot"])
                changed = True
                events.append("snapshot-fallback")
                continue
            if rule == "commit":
                changed = True
                seen = applied_digests(ctx.state.read(), peer)
                for found in slot.complete:
                    if found.digest not in seen:  # Equal digests are one record.
                        apply_commit(ctx, peer, found, chain, events)
                        seen.add(found.digest)
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
        if not had_fork and "fork" in ctx.state.read().cursors.get(peer, {}):
            new_forks.add(peer)  # Evidence found while applying (for example a bad prev).
        retain_ignored_by_fork(ctx, peer)
    ctx.call("presence", changed=changed)
    warnings.extend(fork_warnings(ctx, new=new_forks))
    own = ctx.own
    if own.get("lost"):
        warnings.append(owner_text("own-records-lost"))  # Visible in pull until healed.
    if own.get("in_flight") is not None:
        # Peers and own replay were applied above; presence was not rewritten.
        warnings.append(owner_text("own-records-in-flight", claimed=own["in_flight"]))
        pending.append((ctx.device_id, own["in_flight"], "own-records-in-flight"))
    if own.get("torn") is not None:
        warnings.append(owner_text("torn-own-slot"))
        pending.append((ctx.device_id, own["torn"], "torn-own-slot"))
    if own.get("blocked") is not None:
        # An unproven incomplete family wedges the next slot; pull names it instead
        # of reporting clean while publishing can never proceed.
        warnings.append(owner_text("own-records-in-flight", claimed=own["blocked"]))
        pending.append((ctx.device_id, own["blocked"], "own-records-in-flight"))
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
    view = ctx.view()
    candidates = [
        found
        for peer in sorted(view.devices())
        if peer != ctx.device_id
        for found in view.records(peer, "snapshot")
    ]
    # Freshest = greatest logical created_at; tie: device id, then sequence, then family.
    if candidates:
        newest = max(
            candidates, key=lambda r: (r.record.header["created_at"], r.device, r.seq, r.family)
        )
        load_snapshot(ctx, view, newest.device, newest)
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
        state.applied.clear()
        state.own_log.clear()
        state.intents.clear()
        state.meta = {"device_id": new_id}
        state.history.append({"operation": "rejoin", "old_id": ctx.device_id, "new_id": new_id})
    old_id, ctx.device_id = ctx.device_id, new_id
    kept = len(ctx.state.read().displaced)
    result = ctx.call("join")
    with ctx.state.transaction() as state:
        # Rejoin lists what it displaces, tagged like a peer fork's displacement.
        for value in state.displaced[kept:]:
            value.update(cause="fork", peer=old_id)
    return result


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
    view = ctx.view()
    peers = sorted(view.devices())
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
            newest = view.latest_snapshot(peer)
            if newest is None or newest.record.header["cursors"].get(target, 0) < last_seq:
                return Outcome("pending", peer=target, reason="snapshot-coverage")
    directory = owned_path(ctx.replica_root, "dev/" + target)
    if directory.exists():
        shutil.rmtree(directory)
    return Outcome("retired", seq=last_seq, peer=target, events=("retire",))
