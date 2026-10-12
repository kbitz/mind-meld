"""Sequential real CLI adapter and immutable local-disk lifecycle fixtures.

No entry point may bootstrap or invoke product code without the delivered
71B gate. Legacy layout/activation belongs here, never in transport.py.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
import tomllib
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import keyring
import pytest
from typer.testing import CliRunner, Result

# Load version and all owners under the outer pytest environment, before
# activation changes HOME. Never import root conftest or spoof the version.
import mind_meld
from mind_meld import (
    attemptlog,
    cli,
    config,
    conflictmtime,
    crypto,
    events,
    events_tail,
    host_skill_discovery,
    host_usage,
    identity,
    lockfile,
    manifest,
    pullhistory,
    pullplan,
    resolveflow,
    retention,
    seen_sources,
    sidecar,
    skill_link,
    synclog,
    token_usage,
    upgrade,
)
from mind_meld import devices as registry
from mind_meld.storage.local import LocalBackend

from .transport import FakeTransport, File, Index, device_name, owned_path, read_tree

PASSPHRASE = "simulated-fleet-test-passphrase"
MEMORY_KB = 1024
MISSING_GUARD = (
    "Cannot bootstrap/run the real CLI simulator: Track 71B's shared write-site "
    "guard fsutil.refuse_real_state_write_under_pytest is missing or not callable. "
    "Cause: root isolation fixtures alone do not supply that write-site protection. "
    "Fix: include the delivered 71B guard and bind its actual API in "
    "tests/sim/conftest.py before retrying the smoke; "
    "see tests/sim/README.md#prerequisites"
)


def _offline_fetch(*args, **kwargs):
    raise urllib.error.URLError("network disabled in the simulator")


def _rmtree(path: Path) -> None:
    """Remove a fixture copy whose restored directory modes may be read-only."""
    os.chmod(path, 0o700)
    for directory, children, _ in os.walk(path):
        for child in children:
            target = os.path.join(directory, child)
            if not os.path.islink(target):  # chmod would follow it out of the copy.
                os.chmod(target, 0o700)
    shutil.rmtree(path)


def inventory(root: Path) -> dict:
    """Finite whole-tmp-tree proof, including directories and symlink entries.

    Directory mtimes are excluded: changing an allowed child's name also
    changes its parent's mtime. File bytes, mode and mtime are compared.
    """
    result = {}

    def walk(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                result[relative] = ("dir", stat.S_IMODE(info.st_mode))
                walk(path)
            elif not stat.S_ISREG(info.st_mode):
                result[relative] = ("special", info.st_mode)  # Reading a FIFO blocks.
            else:
                result[relative] = (
                    "file",
                    path.read_bytes(),
                    info.st_mtime_ns,
                    stat.S_IMODE(info.st_mode),
                )

    walk(root)
    return result


@dataclass(frozen=True)
class Snapshot:
    owner: str
    root: Path
    files: tuple[tuple[str, File], ...]
    directories: tuple[tuple[str, int], ...]  # Relative name and mode.
    root_mode: int
    index: Index
    hardware_id: str
    mm_id: str


@dataclass(frozen=True)
class SimDevice:
    fleet: SimFleet
    name: str
    root: Path
    mm_id: str
    hardware_id: str

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def source_root(self) -> Path:
        return self.home / ".claude"

    @property
    def state_root(self) -> Path:
        return self.home / ".config" / "mind-meld"

    @property
    def config_path(self) -> Path:
        return self.state_root / "config.toml"

    @property
    def replica_root(self) -> Path:
        return self.root / "replica"

    def edit(self, relative_path: str, data: bytes, mtime: float | None = None) -> None:
        target = owned_path(self.source_root, relative_path)
        if not isinstance(data, bytes):
            raise ValueError(
                f"Cannot edit {self.name}/{relative_path}: use bytes; see README#paths"
            )
        stamp = self.fleet.transport.next_mtime() if mtime is None else mtime
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
        ):
            raise ValueError(
                f"Bad mtime {stamp!r} at {self.name}/{relative_path}: use finite seconds"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.utime(target, (stamp, stamp))
        self.fleet.transport.record("edit", src=self.name, path=relative_path, mtime=stamp)

    def delete(self, relative_path: str) -> None:
        owned_path(self.source_root, relative_path).unlink(missing_ok=True)
        self.fleet.transport.record("delete", src=self.name, path=relative_path)

    def invoke(self, args: list[str]) -> Result:
        with self.fleet.activate(self):
            before = read_tree(self.replica_root, ignore_transport=True)
            result = CliRunner().invoke(cli.app, args)
            self.fleet.latest_result = result
            self.fleet.transport.record(
                "cli", src=self.name, args=tuple(args), exit_code=result.exit_code
            )
            # OS-created replica mtimes must not make encrypted payloads pick
            # a wall-clock winner. Only changed files get fixture metadata.
            for relative, file in read_tree(self.replica_root, ignore_transport=True).items():
                if file != before.get(relative):
                    stamp = self.fleet.transport.next_mtime()
                    os.utime(owned_path(self.replica_root, relative), (stamp, stamp))
            self.fleet.transport.scan(self.name)
            return result

    def push(self) -> Result:
        return self.invoke(["push"])

    def pull(self) -> Result:
        return self.invoke(["pull"])


class SimFleet:
    _active: SimDevice | None = None  # All fleets in a process activate sequentially.
    _active_patch: pytest.MonkeyPatch | None = None

    def __init__(
        self, tmp_path: Path, *, write_guard: Callable[[], None] | None = None, seed: int = 0
    ):
        self.tmp_path = tmp_path.absolute()
        self.root = self.tmp_path / "sim"
        self.transport = FakeTransport(self.root, seed=seed)
        self.write_guard = write_guard
        self.devices: dict[str, SimDevice] = {}
        self.crypto_init = None
        self.latest_result: Result | None = None

    def _require_guard(self) -> None:
        if self.write_guard is None:
            raise RuntimeError(MISSING_GUARD)
        self.write_guard()

    def device(self, name: str) -> SimDevice:
        try:
            return self.devices[name]
        except KeyError as error:
            raise ValueError(
                f"Unknown device {name}: use bootstrap names; see README#paths"
            ) from error

    def _allocate(self, name: str, *, mm_id: str | None = None) -> SimDevice:
        device_name(name)
        root = self.root / name
        if name in self.devices or root.exists() or root.is_symlink():
            raise ValueError(
                f"Duplicate device/root {name}/{root}: choose a fresh name; see README#paths"
            )
        # Validate the replica AND disk before creating either.
        self.transport.validate_root(name, root / "replica")
        owned_path(self.root, name)
        device = SimDevice(self, name, root, mm_id or f"dev-{name}", f"hardware-{name}")
        for directory in (device.source_root, device.state_root, device.replica_root):
            directory.mkdir(parents=True)
        self.devices[name] = device
        return device

    def bootstrap(self, names: tuple[str, ...] = ("a", "b")) -> SimFleet:
        self._require_guard()  # Before allocation, crypto, config or CLI.
        if self.devices or not names or len(set(names)) != len(names):
            raise ValueError(
                f"Bad bootstrap names {names}: use distinct devices once; see README#paths"
            )
        for name in names:
            device_name(name)
            owned_path(self.root, name)
            if (self.root / name).exists():
                raise ValueError(f"Device root already exists: {name}; choose a fresh name")
        self.root.mkdir()
        for name in names:
            self._allocate(name)
        self._bootstrap_legacy()
        return self

    def _bootstrap_legacy(self) -> None:
        """One real salt/registry seed, copied byte-for-byte; replaced by 80A."""
        first = next(iter(self.devices.values()))
        with self.activate(first):
            backend = LocalBackend(first.replica_root)
            self.crypto_init = crypto.bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
            for device in self.devices.values():
                registry.register_device(backend, device.mm_id, device.name)
            for path in read_tree(first.replica_root):
                stamp = self.transport.next_mtime()
                os.utime(owned_path(first.replica_root, path), (stamp, stamp))
        for device in self.devices.values():
            if device is not first:
                shutil.copytree(first.replica_root, device.replica_root, dirs_exist_ok=True)
            with self.activate(device):
                config.save_config(
                    {
                        "device": {"id": device.mm_id, "name": device.name},
                        "storage": {"path": str(device.replica_root)},
                        "sync": {
                            "max_file_size": config.DEFAULT_MAX_FILE_SIZE,
                            "sources": [
                                {
                                    "name": "claude",
                                    "path": str(device.source_root),
                                    "type": "claude",
                                }
                            ],
                        },
                        "crypto": {"argon2_memory_kb": MEMORY_KB},
                        "upgrade": {"auto_check": False, "auto_install": False},
                    },
                    device.config_path,
                )
            self.transport.add(device.name, device.replica_root)
        self.transport.baseline()

    def _patch_legacy(self, patch: pytest.MonkeyPatch, device: SimDevice) -> None:
        state = device.state_root
        # Reconciled manually against every root-conftest isolation owner.
        # Immutable registry definitions/algorithm constants remain shared.
        paths = (
            (config, "CONFIG_DIR", state),
            (config, "CONFIG_PATH", device.config_path),
            (config, "LOCK_PATH", state / "mind-meld.lock"),
            (lockfile, "LOCK_PATH", state / "mind-meld.lock"),
            (sidecar, "SIDECAR_DIR", state),
            (registry, "DEVICES_WRITE_LOCK", state / "devices-write.lock"),
            (identity, "CACHE_PATH", state / "identity-cache.json"),
            (upgrade, "CACHE_DIR", state),
            (upgrade, "CACHE_PATH", state / "upgrade-state.json"),
            (token_usage, "CACHE_PATH", state / "session-tokens.json"),
            (pullhistory, "HISTORY_DIR", state),
            (seen_sources, "SEEN_DIR", state),
            (retention, "DEFAULT_RETROS_DIR", device.home / ".local/share/mind-meld/retros"),
            (host_usage, "CODEX_SESSIONS_PATH", device.home / ".codex/sessions"),
            (host_usage, "CODEX_ARCHIVED_SESSIONS_PATH", device.home / ".codex/archived_sessions"),
            (host_usage, "GROK_SESSIONS_PATH", device.home / ".grok/sessions"),
            (host_usage, "CACHE_PATH", state / "host-tokens.json"),
            (host_usage, "GROK_CACHE_PATH", state / "grok-host-tokens.json"),
            (host_usage, "CURSOR_STORE_PATH", device.home / ".cursor-sdk-store"),
            (host_usage, "CURSOR_CACHE_PATH", state / "cursor-host-tokens.json"),
            (host_usage, "CURSOR_SPOOL_PATH", state / "cursor-spool.jsonl"),
            (host_usage, "CURSOR_HOOK_CONFIG_PATH", device.home / ".cursor/hooks.json"),
            (host_usage, "CURSOR_CLI_CONFIG_PATH", device.home / ".cursor/cli-config.json"),
        )
        # Import-time real-home defaults: diag falls back to the storage
        # default on broken config, and init offers it.
        icloud = device.home / "Library/Mobile Documents/com~apple~CloudDocs"
        paths += (
            (config, "DEFAULT_STORAGE_PATH", str(icloud / "mind-meld")),
            (cli, "DEFAULT_STORAGE_PATH", str(icloud / "mind-meld")),
            (cli, "_ICLOUD_DRIVE_ROOT", icloud),
        )
        for owner, attribute, value in paths:
            patch.setattr(owner, attribute, value)
        patch.setenv("HOME", str(device.home))
        patch.setenv("XDG_CONFIG_HOME", str(device.home / ".config"))
        patch.setenv("XDG_STATE_HOME", str(device.home / ".local/state"))
        patch.setenv("XDG_DATA_HOME", str(device.home / ".local/share"))
        patch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        patch.delenv("GROK_HOME", raising=False)
        overrides = {}
        for row in skill_link.AGENT_ROWS:
            agent = device.home / "agents" / row.key
            agent.mkdir(parents=True, exist_ok=True)
            overrides[row.key] = str(agent / "skills")
        patch.setattr(skill_link, "_TEST_SKILL_ROOT_OVERRIDES", overrides)
        patch.setattr(skill_link, "_marker_dir", lambda: state / "skill-markers")
        patch.setenv("MM_SKILLS_DIR", str(device.home / "agent-skills/retro-fleet"))
        target = next(s for s in config.DEFAULT_SOURCES if s["name"] == "mm-events")
        default_events = str(device.home / ".local/share/mind-meld")
        patch.setitem(target, "path", default_events)  # Preserve cli's imported dict binding.
        outer_matcher = config._is_default_mm_events_path
        patch.setattr(
            config,
            "_is_default_mm_events_path",
            lambda path: (
                outer_matcher(path)
                or config._normalized_mm_events_path(path)
                == config._normalized_mm_events_path(default_events)
            ),
        )
        for owner, attribute in (
            (token_usage, "_WARNED_UNKNOWN_MODELS"),
            (token_usage, "_WARNED_OVERSIZE_PATHS"),
            (config, "_BOOTSTRAP_WARNED_PATHS"),
            (registry, "_AMBIGUOUS_PREFIX_NOTICED"),
            (skill_link, "_ORPHAN_OVERRIDE_WARNED"),
        ):
            # Each activation is a fresh process; warn-once state never carries over.
            patch.setattr(owner, attribute, set())
        for attribute in ("_MM_SKILLS_DIR_REJECTION_EMITTED", "_EMPTY_AGENTS_NOTICE_EMITTED"):
            patch.setattr(skill_link, attribute, False)
        for attribute, value in (
            ("_INVOCATION_SKIP", False),
            ("_TRANSITION_DETECTED_THIS_INVOCATION", False),
            ("_LAST_TRANSITION_SEEN", None),
        ):
            patch.setattr(upgrade, attribute, value)
        patch.setattr(crypto, "_SESSION", {})
        patch.setattr(crypto, "_MASTER_KEY_CACHE", {})
        crypto.clear_crypto_session()
        if self.crypto_init is not None:
            crypto.set_crypto_session(self.crypto_init.root_salt, MEMORY_KB)
        # External effects only. Real scans, crypto, storage and apply stay intact.
        patch.setattr(keyring, "get_password", lambda *a, **kw: None)
        patch.setattr(keyring, "set_password", lambda *a, **kw: None)
        patch.setattr(identity, "gather_local_identities", lambda **kw: [])
        # init and refresh-identity otherwise run real `git config` and `gh api`.
        patch.setattr(identity, "_do_full_gather", lambda **kw: [])
        # init otherwise runs the host's real `brctl download` on the store.
        patch.setattr(cli, "_auto_pin_storage_for_icloud", lambda storage_path: None)
        patch.setattr(
            events, "discover_git_roots", lambda *a, **kw: events.GitRootDiscovery((), ())
        )
        patch.setattr(upgrade, "update_or_nudge", lambda *a, **kw: None)
        patch.setattr(upgrade, "run_transition_hook", lambda *a, **kw: None)
        patch.setattr(upgrade, "_fetch_tags", _offline_fetch)
        # diag otherwise runs the developer's real `grok inspect`.
        patch.setattr(
            host_skill_discovery,
            "probe_grok_skill_discovery",
            lambda: {"host": "grok", "status": host_skill_discovery.STATUS_BINARY_ABSENT},
        )
        # A real push otherwise creates agent skill symlinks. Installation
        # is an external effect, and lifecycle copies reject all symlinks.
        patch.setattr(skill_link, "_ensure_retro_skill_links", lambda *a, **kw: [])
        patch.setattr(skill_link, "_reap_retired_opencode_skill_link", lambda *a, **kw: None)
        # Clock audit: manifest tombstone expiry, pullplan future clamp,
        # retention/usage windows, conflict naming, pull/sync logs, capture
        # attempts and promote names. CLI time.time/monotonic are elapsed or
        # deadline timers; crypto/identity/upgrade stamps stay real.
        fleet = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(fleet.product_time, tz)

        for owner in (
            cli,
            registry,
            manifest,
            pullplan,
            retention,
            token_usage,
            host_usage,
            events,
            events_tail,
            pullhistory,
            synclog,
            attemptlog,
            resolveflow,
        ):
            patch.setattr(owner, "datetime", Clock)
        patch.setattr(conflictmtime, "time", SimpleNamespace(time=lambda: self.product_time))

    @property
    def product_time(self) -> float:
        return self.transport.fixture_time

    @contextmanager
    def activate(self, device: SimDevice) -> Iterator[SimDevice]:
        self._require_guard()
        if self.device(device.name) is not device:
            raise ValueError(f"Unowned device {device.name}; use fleet.device(); see README#paths")
        if SimFleet._active is not None:
            raise RuntimeError(
                f"Cannot activate {device.name}: {SimFleet._active.name} is active; "
                "run sequentially, exit activation first; see README#activation"
            )
        owned_path(self.root, device.name)
        before, version = inventory(self.tmp_path), mind_meld.__version__
        prefix = device.root.relative_to(self.tmp_path).as_posix()
        for path, entry in before.items():
            if entry[0] == "link" and (path == prefix or path.startswith(prefix + "/")):
                # Reject existing links before patches or mkdir can follow them.
                owned_path(self.tmp_path, path)
        entry_result = self.latest_result
        SimFleet._active = device
        failure = patch = None
        try:
            with pytest.MonkeyPatch.context() as patch:
                SimFleet._active_patch = patch
                try:
                    self._patch_legacy(patch, device)
                    yield device
                except BaseException as error:
                    failure = error
                    raise
                finally:
                    try:
                        if mind_meld.__version__ != version:
                            raise RuntimeError(
                                f"Activation of {device.name} changed installed version "
                                f"{version} -> {mind_meld.__version__}; see README#activation"
                            )
                        after = inventory(self.tmp_path)
                        changed = [
                            p
                            for p in sorted(before.keys() | after.keys())
                            if before.get(p) != after.get(p)
                            and (
                                not (p == prefix or p.startswith(prefix + "/"))
                                # A new link could later route writes outside tmp.
                                or (after.get(p) or ("",))[0] == "link"
                            )
                        ]
                        if changed:
                            raise RuntimeError(
                                f"Isolation failure during {device.name}: "
                                f"changes outside active root or new links {changed}; "
                                "redirect the owning path; see README#activation"
                            )
                    except BaseException as isolation:
                        if failure is not None:
                            failure.add_note(str(isolation))
                        else:
                            ran = self.latest_result is not entry_result
                            isolation.cli_result = self.latest_result if ran else None
                            raise
        finally:
            # A released leak no longer owns the slot; never clear a later one.
            if SimFleet._active_patch is patch:
                SimFleet._active = SimFleet._active_patch = None

    @classmethod
    def release_leaked(cls) -> SimDevice | None:
        """Undo an activation entered but never exited; return its device."""
        leaked, patch = cls._active, cls._active_patch
        cls._active = cls._active_patch = None
        if patch is not None:
            patch.undo()
        return leaked

    def _idle(self, device: SimDevice, operation: str) -> None:
        if self.devices.get(device.name) is not device:
            raise ValueError(f"Cannot {operation} unowned device {device.name}; see README#paths")
        owned_path(self.root, device.name)
        if SimFleet._active is not None:
            raise RuntimeError(
                f"Cannot {operation} {device.name}: "
                f"CLI activation {SimFleet._active.name} is active; "
                "exit activation; see README#lifecycle"
            )
        if tokens := self.transport.unfinished(device.name):
            event = tokens[0].choice.event
            raise RuntimeError(
                f"Cannot {operation} {device.name}: replacement e{event} is unfinished; "
                "finish_replace before retrying; see README#lifecycle"
            )

    def snapshot(self, device: SimDevice) -> Snapshot:
        self._idle(device, "snapshot")
        files = tuple(sorted(read_tree(device.root).items()))
        directories = tuple(
            (p.relative_to(device.root).as_posix(), stat.S_IMODE(p.lstat().st_mode))
            for p in sorted(device.root.rglob("*"))
            if p.is_dir()
        )
        self.transport.record("snapshot", src=device.name)
        return Snapshot(
            device.name,
            device.root,
            files,
            directories,
            stat.S_IMODE(device.root.lstat().st_mode),
            self.transport.index(device.name),
            device.hardware_id,
            device.mm_id,
        )

    def _copy_snapshot(self, snapshot: Snapshot, destination: Path) -> Path | None:
        """Install the disk; returns a backup to discard once the index is committed."""
        # Validate all source/destination names before any destructive action.
        owned_path(self.root, destination.relative_to(self.root).as_posix())
        for relative, _ in snapshot.files:
            owned_path(destination, relative)
        for relative, _ in snapshot.directories:
            owned_path(destination, relative)
        if destination.exists():
            read_tree(destination)
        stage = self.root / f".mm-tmp-copy-{destination.name}"
        backup = self.root / f".mm-tmp-backup-{destination.name}"
        for path in (stage, backup):
            if path.exists() or path.is_symlink():
                raise ValueError(
                    f"Copy staging occupied at {path}: "
                    "remove fixture staging before retrying; see README#lifecycle"
                )
        try:
            stage.mkdir()
            for relative, _ in snapshot.directories:
                owned_path(stage, relative).mkdir(parents=True, exist_ok=True)
            for relative, file in snapshot.files:
                target = owned_path(stage, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(file.data)
                os.chmod(target, file.mode)
                os.utime(target, ns=(file.mtime_ns, file.mtime_ns))
            # Children before parents, so a read-only parent is applied last.
            for relative, mode in reversed(snapshot.directories):
                os.chmod(owned_path(stage, relative), mode)
            os.chmod(stage, snapshot.root_mode)
            if destination.exists():
                os.replace(destination, backup)
            try:
                os.replace(stage, destination)
            except OSError as error:
                if backup.exists():
                    try:
                        os.replace(backup, destination)
                    except OSError as rollback:
                        error.add_note(
                            f"Rollback also failed: {rollback}; saved disk remains at {backup}"
                        )
                raise
        except OSError as error:
            error.add_note(
                f"Copying device {snapshot.owner} to {destination}; "
                "repair I/O and retry; see README#lifecycle"
            )
            if stage.exists():
                try:
                    _rmtree(stage)
                except OSError as cleanup:
                    error.add_note(
                        f"Staging cleanup failed: {cleanup}; remove {stage} before retry"
                    )
            raise
        return backup if backup.exists() else None

    @staticmethod
    def _discard_backup(backup: Path | None) -> None:
        if backup is None:
            return
        try:
            _rmtree(backup)
        except OSError as error:
            error.add_note(
                f"Disk and index are committed, but backup cleanup failed; "
                f"remove {backup} before the next copy; see README#lifecycle"
            )
            raise

    def restore(self, device: SimDevice, snapshot: Snapshot, *, index_policy: str) -> None:
        self._idle(device, "restore")
        if (
            index_policy not in ("saved", "current")
            or snapshot.owner != device.name
            or snapshot.root != device.root
            or snapshot.hardware_id != device.hardware_id
            or snapshot.mm_id != device.mm_id
        ):
            raise ValueError(
                f"Cannot restore {device.name}: "
                "require its own snapshot and saved/current index_policy; see README#lifecycle"
            )
        backup = self._copy_snapshot(snapshot, device.root)
        if index_policy == "saved":
            self.transport.restore_index(device.name, snapshot.index)
        self.transport.record("restore", dst=device.name, index_policy=index_policy)
        self._discard_backup(backup)

    def _rebased_legacy(self, clone: SimDevice, original: SimDevice) -> tuple[dict, dict | None]:
        """Parse and rebase known path fields in memory; nothing is written."""

        def rebase(value: str) -> str:
            path = Path(value)
            return (
                str(clone.root / path.relative_to(original.root))
                if path.is_relative_to(original.root)
                else value
            )

        raw = tomllib.loads(clone.config_path.read_text())
        raw["storage"]["path"] = rebase(raw["storage"]["path"])
        for source in raw["sync"]["sources"]:
            source["path"] = rebase(source["path"])
        # Only known path fields in the local last-push sidecar. Replica and
        # source bytes, and other user strings, are deliberately opaque.
        last_push = clone.state_root / "last-push.json"
        data = None
        if last_push.exists():
            data = json.loads(last_push.read_text())
            for source in data.get("sources", {}).values():
                if isinstance(source.get("base_path"), str):
                    source["base_path"] = rebase(source["base_path"])
        return raw, data

    def clone(self, device: SimDevice, name: str) -> SimDevice:
        self._idle(device, "clone")
        device_name(name)
        destination = self.root / name
        if name in self.devices or destination.exists() or destination.is_symlink():
            raise ValueError(
                f"Cannot clone {device.name} to {name}: destination occupied; "
                "use a fresh name; see README#paths"
            )
        self.transport.validate_root(name, destination / "replica")
        snapshot = self.snapshot(device)
        self._copy_snapshot(snapshot, destination)  # Fresh destination: no backup.
        clone = SimDevice(self, name, destination, device.mm_id, f"hardware-{name}")
        try:
            raw, last_push = self._rebased_legacy(clone, device)
            self.devices[name] = clone  # Activation requires an owned device.
            with self.activate(clone):
                config.save_config(raw, clone.config_path)
            if last_push is not None:
                (clone.state_root / "last-push.json").write_text(json.dumps(last_push))
        except BaseException as error:
            # Never leave a half-rebased clone that still points at the original.
            self.devices.pop(name, None)
            try:
                _rmtree(destination)
            except OSError as cleanup:
                error.add_note(f"Clone cleanup failed: {cleanup}; remove {destination}")
            error.add_note(
                f"Cloning {device.name} to {name} registered nothing; "
                "repair the source device and retry; see README#lifecycle"
            )
            raise
        self.transport.add(name, clone.replica_root, inherit=snapshot.index)
        self.transport.record("clone", src=device.name, dst=name)
        return clone

    def diagnostics(self) -> str:
        result = self.latest_result
        cli_result = (
            "none"
            if result is None
            else (
                f"exit={result.exit_code}\nstdout={result.stdout}\n"
                f"stderr={result.stderr}\nexception={result.exception!r}"
            )
        )
        return (
            f"seed={self.transport.seed}\nchoices/trace={list(self.transport.trace)!r}\n"
            f"pending={self.transport.pending()!r}\nlatest real CLI Result: {cli_result}"
        )
