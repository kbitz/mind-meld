"""Independent v2 apparatus. Never imports the 1.x CLI harness or conftest."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mind_meld.store import envelope

from .transport import EPOCH, FakeTransport, Index, device_name, owned_path, read_tree

PASSPHRASE = "simulated-v2-passphrase"
# Record families come from sha256(run salt || fleet counter): deterministic for a
# replayed trace and independent of PYTHONHASHSEED and os.urandom, but not
# monotonic, so nothing may assume that a later family sorts later.
FAMILY_SALT = b"mm-sim-record-family-v1"
STIGNORE = b"(?d).mm-tmp-*\n(?d).DS_Store\n"
DAY = 86400
PATHS = ("notes.txt", "other.txt", "MEMORY.md", "events.jsonl")
# Markers are independent of ciphertext/nonce. Empty content has no marker.
CONTENTS = (
    b"",
    b"sim-marker-alpha-0001\n",
    b"sim-marker-bravo-0002\n",
    b"sim-marker-alpha-0001\nsim-marker-bravo-0002\n",
    b"sim-marker-bravo-0002\nsim-marker-alpha-0001\n",
    b"sim-marker-crlf--0003\r\n",
    b"sim-marker-space-0004   \n",
    b"sim-marker-binary0005\xff\n",
)
MARKERS = tuple(data if i in (3, 4) else data[:20] for i, data in enumerate(CONTENTS) if data)
# Four devices plus up to four rejoins. The old cap of six left room for two rejoins
# once the fleet was full, and restores add fork-and-rejoin paths; PART_BYTES stays
# at its 1024 floor (the snapshot cap moves 7724 -> 7980 bytes), so sealing is
# unchanged.
MAX_DEVICES = 4
MAX_REJOINS = 4
MAX_IDS = MAX_DEVICES + MAX_REJOINS
# Snapshot cap: 4 paths, each <= all pool lines + MAX_IDS counters; <= 100 steps
# per example => counters <= 10000. 2048-byte header/cursor allowance and
# 1024 bytes metadata per path. Divide by 512, round up; floor 1024 permits
# ordinary commits into multiple parts while bounding the largest snapshot.
MAX_SNAPSHOT_BYTES = 2048 + len(PATHS) * (sum(map(len, CONTENTS)) + 1024 + MAX_IDS * 32)
PART_BYTES = max(1024, (MAX_SNAPSHOT_BYTES + 511) // 512)


@dataclass
class Clock:
    now: int = int(EPOCH) + 1_000_000

    def advance(self, seconds: int) -> None:
        if type(seconds) is not int or seconds < 0:
            raise ValueError("clock delta must be nonnegative seconds")
        self.now += seconds

    @property
    def iso(self) -> str:
        return datetime.fromtimestamp(self.now, timezone.utc).isoformat()


@dataclass
class StoreDevice:
    fleet: StoreFleet
    name: str
    root: Path
    mm_id: str
    hardware_id: str
    extra_source_roots: dict[str, Path] = field(default_factory=dict)

    @property
    def source_roots(self) -> dict[str, Path]:
        return {"claude": self.source_root, **self.extra_source_roots}

    @property
    def source_root(self) -> Path:
        return self.root / "sources" / "claude"

    @property
    def state_root(self) -> Path:
        return self.root / "state"

    @property
    def replica_root(self) -> Path:
        return self.root / "replica"

    def edit(self, path: str, data: bytes, mtime: int | None = None) -> None:
        self.fleet.edit(self.name, path, data, mtime=mtime)

    def delete(self, path: str) -> None:
        self.fleet.delete(self.name, path)


class StoreFleet:
    def __init__(self, root: Path, *, write_guard, registry=None, state_factory=None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.write_guard = write_guard
        self.write_guard()
        self.transport = FakeTransport(root)
        self.clock = Clock()
        self.devices: dict[str, StoreDevice] = {}
        self.ids: set[str] = set()
        self.keys_cache: dict[bytes, envelope.FleetKeys] = {}
        from .model.state import FileState

        self.state_factory = FileState if state_factory is None else state_factory
        self.registry = registry
        self.oracle = None
        self.steps: list[tuple] = []
        self.coverage: set[str] = set()
        self.last_piece = "harness"
        self.last_observation = None
        self.action_seconds = []
        self.allow_corruption = False
        # Held by the fleet, never on a device disk. ``forced_families`` is the
        # collision hook: its values are returned first, in order.
        self.family_counter = 0
        self.forced_families: list[str] = []
        # Called with every drawn family, so a test can make a name appear between the
        # publisher's listing and its exclusive create (a real EEXIST race).
        self.family_hook = None

    def ordered(self) -> list[StoreDevice]:
        return [self.devices[n] for n in sorted(self.devices)]

    def add_device(self, name: str) -> StoreDevice:
        device_name(name)
        if not name.isalnum() or name in self.devices or len(self.devices) >= 4:
            raise ValueError("v2 requires a fresh alphanumeric name and at most 4 devices")
        mm_id = hashlib.sha256(name.encode()).hexdigest()[:8]
        if len(self.ids | {mm_id}) > MAX_IDS:
            raise ValueError(f"v2 run permits at most {MAX_IDS} device ids")
        root = owned_path(self.root, name)
        device = StoreDevice(self, name, root, mm_id, "hardware-" + name)
        for directory in (device.source_root, device.state_root, device.replica_root):
            directory.mkdir(parents=True)
        self.transport.add(name, device.replica_root)
        self.devices[name] = device
        self.ids.add(mm_id)
        return device

    def next_family(self) -> str:
        """The simulator's ``family_source``: 8 lowercase hex, fresh per publication."""
        if self.forced_families:
            family = self.forced_families.pop(0)
        else:
            self.family_counter += 1
            digest = hashlib.sha256(FAMILY_SALT + self.family_counter.to_bytes(8, "big"))
            family = digest.hexdigest()[:8]
        if self.family_hook is not None:
            self.family_hook(family)
        return family

    def force_families(self, *families: str) -> None:
        """Test hook: the next draws return these, so a name can collide on purpose."""
        self.forced_families.extend(families)

    def folder_synced(self, name: str) -> bool:
        """Model of Syncthing reporting an idle folder with nothing needed here."""
        return not any(choice.dst == name for choice in self.transport.pending())

    def keys(self, device: StoreDevice) -> envelope.FleetKeys | None:
        try:
            raw = (device.replica_root / "fleet.json").read_bytes()
        except FileNotFoundError:
            return None
        if raw not in self.keys_cache:
            self.keys_cache[raw] = envelope.unlock_fleet(raw, PASSPHRASE)
        return self.keys_cache[raw]

    def bootstrap(self, names=("a", "b")) -> StoreFleet:
        from .invariants import Oracle
        from .model.registry import default_registry

        self.registry = default_registry() if self.registry is None else self.registry
        self.oracle = Oracle()
        for name in names:
            self.add_device(name)
        self.action(names[0], "create_fleet")
        self.drain()
        for name in names[1:]:
            self.action(name, "join")
        self.drain()
        return self

    def action(self, name: str, operation: str, **kwargs):
        from .invariants import store_action

        return store_action(self, name, operation, **kwargs)

    def edit(self, name: str, path: str, data: bytes, *, mtime=None) -> None:
        stamp = self.clock.now if mtime is None else mtime
        if stamp > self.clock.now or stamp < 0:
            raise ValueError("fixture mtime must be within the logical clock")
        device = self.devices[name]
        target = owned_path(device.source_root, path)
        self.oracle.user_action(self, device, "claude", path, data)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.utime(target, (stamp, stamp))
        self.steps.append(("edit", name, path, data.hex(), stamp))

    def delete(self, name: str, path: str) -> None:
        self.oracle.user_action(self, self.devices[name], "claude", path, None)
        owned_path(self.devices[name].source_root, path).unlink(missing_ok=True)
        self.steps.append(("delete", name, path))

    def drain(self) -> None:
        result = self.transport.drain(max_steps=100_000)
        if not result.quiescent:
            raise AssertionError(f"transport not quiescent: {result.remaining}")

    def advance(self, seconds: int) -> None:
        self.clock.advance(seconds)
        self.steps.append(("advance", seconds))

    def participants(self) -> list[StoreDevice]:
        return [
            d
            for d in self.ordered()
            if (m := self.state_factory(d.state_root).read().meta).get("joined")
            and not m.get("retired")
            and not m.get("refused")
        ]

    def idle(self, device: StoreDevice, operation: str) -> None:
        if self.devices.get(device.name) is not device:
            raise ValueError(f"unowned device for {operation}; see README#lifecycle")
        if self.transport.unfinished(device.name):
            raise RuntimeError(f"finish_replace before {operation}; see README#lifecycle")

    def snapshot(self, device: StoreDevice):
        import stat

        from .disk import Snapshot

        self.idle(device, "backup")
        directories = tuple(
            (p.relative_to(device.root).as_posix(), stat.S_IMODE(p.lstat().st_mode))
            for p in sorted(device.root.rglob("*"))
            if p.is_dir()
        )
        return Snapshot(
            device.name,
            device.root,
            tuple(sorted(read_tree(device.root).items())),
            directories,
            stat.S_IMODE(device.root.stat().st_mode),
            self.transport.index(device.name),
            device.hardware_id,
            device.mm_id,
        )

    def scoped_snapshot(self, device: StoreDevice, snapshot, scope: str):
        """A backup narrowed to what ``scope`` rolls back; the rest of the disk stays as is.

        ``state``: sources and state come from the backup, the replica stays current.
        ``fresh-replica``: sources and state come from the backup, the replica is empty.
        """
        import stat
        from dataclasses import replace

        def kept(path):
            return path.split("/", 1)[0] in ("sources", "state")

        files = [(p, f) for p, f in snapshot.files if kept(p)]
        directories = [(p, mode) for p, mode in snapshot.directories if kept(p)]
        if scope == "state":
            files += sorted(
                (p, f) for p, f in read_tree(device.root).items() if p.startswith("replica/")
            )
            directories += [
                (p.relative_to(device.root).as_posix(), stat.S_IMODE(p.lstat().st_mode))
                for p in sorted(device.replica_root.rglob("*"))
                if p.is_dir()
            ]
        directories.append(("replica", stat.S_IMODE(device.replica_root.stat().st_mode)))
        return replace(
            snapshot, files=tuple(sorted(files)), directories=tuple(sorted(set(directories)))
        )

    def restore(
        self, device: StoreDevice, snapshot, *, index_policy: str | None = None, scope="disk"
    ) -> None:
        """Roll a device back to a backup.

        ``scope="disk"`` (default) restores the whole disk and needs ``index_policy``
        ``saved`` or ``current``. ``scope="state"`` rolls back sources and state only and
        ``scope="fresh-replica"`` also empties the replica and resets the transport index
        so peers refill it; neither takes an ``index_policy``.
        """
        from .disk import copy_snapshot, discard_backup
        from .invariants import contains, record_versions, source_inventory

        self.idle(device, "restore")
        if scope not in ("disk", "state", "fresh-replica"):
            raise ValueError(
                "restore scope must be disk, state or fresh-replica; see README#lifecycle"
            )
        if (scope == "disk") != (index_policy is not None) or (
            scope == "disk" and index_policy not in ("saved", "current")
        ):
            raise ValueError(
                "restore with scope disk needs index_policy saved or current; "
                "scopes state and fresh-replica take none; see README#lifecycle"
            )
        if (
            snapshot.owner != device.name
            or snapshot.root != device.root
            or snapshot.hardware_id != device.hardware_id
        ):
            raise ValueError("restore requires its own backup; see README#lifecycle")
        external = [
            (source, path, file.data)
            for d in self.ordered()
            if d is not device
            for (source, path), file in source_inventory(d).items()
        ]
        records = record_versions(self)
        external += [
            (source, path, data) for source, path, data, name in records if name != device.name
        ]
        external += [
            (v["source"], v["path"], v["content"])
            for d in self.ordered()
            if d is not device
            for v in self.state_factory(d.state_root).read().displaced
            if not v.get("expired")
        ]
        prior = [
            (source, path, file.data) for (source, path), file in source_inventory(device).items()
        ]
        prior += [
            (source, path, data) for source, path, data, name in records if name == device.name
        ]
        prior += [
            (v["source"], v["path"], v["content"])
            for v in self.state_factory(device.state_root).read().displaced
            if not v.get("expired")
        ]
        candidates = [
            v.event
            for _, v in sorted(self.oracle.versions.items())
            if any(
                (source, path) == (v.source, v.path) and contains(path, v.data, data)
                for source, path, data in prior
            )
            and not any(
                (source, path) == (v.source, v.path) and contains(path, v.data, data)
                for source, path, data in external
            )
        ]
        installed = snapshot if scope == "disk" else self.scoped_snapshot(device, snapshot, scope)
        backup = copy_snapshot(self.root, installed, device.root)
        if scope == "fresh-replica":
            self.transport.restore_index(device.name, Index((), ()))
        elif scope == "disk" and index_policy == "saved":
            self.transport.restore_index(device.name, snapshot.index)
        device.mm_id = snapshot.mm_id
        restored = [
            (source, path, file.data) for (source, path), file in source_inventory(device).items()
        ]
        restored += [
            (source, path, data)
            for source, path, data, name in record_versions(self)
            if name == device.name
        ]
        restored += [
            (v["source"], v["path"], v["content"])
            for v in self.state_factory(device.state_root).read().displaced
            if not v.get("expired")
        ]
        for event in candidates:
            version = self.oracle.versions[event]
            if not any(
                (source, path) == (version.source, version.path)
                and contains(path, version.data, data)
                for source, path, data in restored
            ):
                self.oracle.excluded.add(event)
        # Only versions this rollback actually undid: authored after the backup point,
        # causally. A user version's own counter is linear per path, so it postdates
        # the backup exactly when it exceeds the restored ledger's counter for that
        # path. (Byte comparison cannot say this: deleting a file created after the
        # backup restores to the same absence the deletion produced.)
        from .model.state import path_key as _path_key

        restored_ledger = self.state_factory(device.state_root).read().ledger

        def _undone(version):
            identity = version.identity or snapshot.mm_id
            row = restored_ledger.get(_path_key(version.source, version.path))
            restored_counter = row["vv"].get(identity, 0) if row else 0
            return version.vv.get(identity, 0) > restored_counter

        self.oracle.rolled_back |= {
            event
            for event, version in self.oracle.versions.items()
            if version.user and version.device == device.name and _undone(version)
        }
        discard_backup(backup)
        self.steps.append(("restore", device.name, index_policy, scope))
        self.oracle_checked = False
        self.transport.scan(device.name)

    def clone(self, device: StoreDevice, name: str) -> StoreDevice:
        from .disk import copy_snapshot

        self.idle(device, "clone")
        device_name(name)
        if not name.isalnum() or name in self.devices or len(self.devices) >= 4:
            raise ValueError("clone requires a fresh alphanumeric name and a free device slot")
        snapshot = self.snapshot(device)
        root = owned_path(self.root, name)
        self.transport.validate_root(name, root / "replica")
        backup = copy_snapshot(self.root, snapshot, root)
        assert backup is None
        clone = StoreDevice(self, name, root, device.mm_id, "hardware-" + name)
        clone.extra_source_roots = {
            source: root / path.relative_to(device.root)
            for source, path in sorted(device.extra_source_roots.items())
        }
        self.transport.add(name, clone.replica_root, inherit=snapshot.index)
        self.devices[name] = clone
        self.steps.append(("clone", device.name, name))
        self.oracle_checked = False
        return clone

    def rejoin(self, device: StoreDevice):
        self.idle(device, "rejoin")
        if len(self.ids) >= MAX_IDS:
            raise ValueError(f"v2 run permits at most {MAX_IDS} device ids")
        new_id = hashlib.sha256(
            (device.name + ":rejoin:" + str(len(self.ids))).encode()
        ).hexdigest()[:8]
        result = self.action(device.name, "rejoin", new_id=new_id)
        if device.mm_id == new_id:
            self.ids.add(new_id)
        return result

    def diagnostics(self) -> str:
        from .invariants import KNOWN_DESIGN_GAPS

        pieces = {k: v.kind for k, v in sorted(self.registry.items())}
        return (
            f"pieces={pieces}; gaps={KNOWN_DESIGN_GAPS}; participants="
            f"{len(self.participants())}; steps={self.steps}; "
            f"transport={list(self.transport.trace)}"
        )
