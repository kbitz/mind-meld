"""Device registration and listing for Mind Meld."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from mind_meld import __version__, crypto, fsutil, safety, syncthing
from mind_meld.config import DEFAULT_ARGON2_MEMORY_KB
from mind_meld.errors import CryptoError, NewerFormatError, StorageError, StorageNotFound
from mind_meld.storage.keys import (
    DEVICES_PREFIX,
    RETIRED_PREFIX,
    SYNCTHING_PREFIX,
    device_key,
    parse_device_key,
    parse_retired_record_key,
    parse_syncthing_record_key,
    retired_record_key,
    syncthing_record_key,
)
from mind_meld.storage.local import LocalBackend, is_conflict_name

# Local sentinel for serializing read-modify-write of devices/<id>.json
# entries across concurrent autopush + interactive push (Group 7 preflight
# D10 — codex outside-voice finding #6). Lives in the per-machine config
# dir, not on synced storage — fcntl.flock is a local-process primitive.
DEVICES_WRITE_LOCK = Path.home() / ".config" / "mind-meld" / "devices-write.lock"


# Brief retry budget on lock contention. The critical section is one
# storage GET + one storage PUT (typically <100ms on local FS, longer on
# iCloud cold-cache), so total acquire wait stays well under 1 second.
_LOCK_RETRY_INTERVALS_S: tuple[float, ...] = (0.05, 0.1, 0.2, 0.4)


@contextmanager
def _devices_write_lock() -> Iterator[None]:
    """Serialize read-modify-write of any devices/<id>.json mutator.

    All RMW callers (today: `update_last_seen`; future field-adders inherit
    safety by routing through this lock) must hold the flock for the read
    AND write so an interleaved read can't observe a partial state. Today's
    fields (`last_seen`, `last_seen_version`) are deterministic per-process,
    so concurrent writers without this lock would not lose data — the lock
    is forward-defense for the moment a non-deterministic field lands.

    Lock-file dir creation / open failures propagate to the caller. The
    lock file lives under ~/.config/mind-meld/ which mm controls; a
    permission failure here indicates a broken install and surfacing it
    via exception is more diagnostic than silently degrading the lock.

    Acquire is non-blocking (LOCK_EX | LOCK_NB) with a brief retry budget.
    On exhausted retries, degrade to executing without the lock and emit
    one `mm: warning:` line to stderr (visible-failure contract). Today's
    deterministic fields are safe under degraded operation; the warning
    lets the user catch a stuck-process scenario before a future non-
    deterministic field starts losing data.
    """
    fsutil.refuse_real_state_write_under_pytest(DEVICES_WRITE_LOCK)
    DEVICES_WRITE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(DEVICES_WRITE_LOCK), os.O_WRONLY | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            for delay in _LOCK_RETRY_INTERVALS_S:
                time.sleep(delay)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    continue
        if not locked:
            sys.stderr.write(
                "mm: warning: device write lock contended; "
                "skipping last_seen update for this push\n"
            )
        try:
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def register_device(
    backend: LocalBackend,
    device_id: str,
    device_name: str,
) -> None:
    """Register a device entry on storage. No-op if the entry already exists.

    Idempotent on re-call: ensures the entry exists, never bumps the
    `registered:` timestamp. Self-heal callers (`_ensure_device_registered`)
    can re-register safely knowing that an existing first-registration time
    is preserved.

    Uses `LocalBackend.put_exclusive` (atomic os.link with EEXIST detection)
    so the create-only invariant holds even when iCloud `.icloud` placeholder
    state hides the existing entry from `backend.exists()` (Group 7 preflight
    D8 + codex outside-voice finding #6).

    Does NOT seed `last_seen` at registration; `last_seen` means "time of
    last push" (not "time of last activity"). Callers display missing
    `last_seen` as em-dash so a registered-but-never-pushed device doesn't
    look like it pushed at registration time.
    """
    key = device_key(device_id)
    data = {
        "device_id": device_id,
        "device_name": device_name,
        "registered": datetime.now(timezone.utc).isoformat(),
    }
    try:
        backend.put_exclusive(key, json.dumps(data, indent=2).encode("utf-8"))
    except StorageError:
        # Entry exists. Preserve the existing `registered:` timestamp.
        return


def update_last_seen(
    backend: LocalBackend,
    device_id: str,
) -> None:
    """Update the `last_seen` timestamp + `last_seen_version` for a device.

    Semantic: `last_seen` records the time of this device's LAST PUSH.
    Pull does NOT update it -- a read-only device is correctly shown as
    "never pushed" rather than appearing active via pulls.

    `last_seen_version` (v0.9.2+) records the mm version that performed
    the last push. Used by Track 5E's strict pull-start fleet-version
    refusal — without it, a v0.9.2 puller can't tell whether a peer's
    `.sync-conflict-*` files were produced under pre-inversion or post-
    inversion semantics. Forward-compatible: older mm reads ignore
    unknown keys.

    Concurrency: read-modify-write is wrapped in `_devices_write_lock()`
    so concurrent autopush + interactive push can't lose interleaved
    field updates (Group 7 preflight D10).
    """
    key = device_key(device_id)
    with _devices_write_lock():
        try:
            data = json.loads(backend.get(key))
        except StorageError:
            return  # Device not registered yet -- skip silently
        data["last_seen"] = datetime.now(timezone.utc).isoformat()
        data["last_seen_version"] = __version__
        backend.put(key, json.dumps(data, indent=2).encode("utf-8"))


@dataclass
class DeviceInventory:
    """Everything one read of ``devices/`` found (Track 87A).

    A pull reads this once and feeds the fleet-version gate, peer selection,
    tombstone collection and the waiting notice from it, so a Syncthing
    replacement between two reads cannot make them disagree.

    * ``valid``: entries named ``devices/<id>.json`` whose body is a valid
      device record with that same ``device_id``.
    * ``dropped``: ``(key, reason)`` for key-named entries that are unreadable
      or shape-invalid. These still refuse the pull (Track 5E).
    * ``vanished``: ids of key-named entries that were listed and then
      disappeared mid-read. Their content has not arrived yet.
    * ``foreign``: keys ignored as not a device: conflict copies, misnamed
      files, a body whose ``device_id`` is not its file name.
    """

    valid: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)


def list_devices(backend: LocalBackend) -> list[dict[str, Any]]:
    """List all registered devices from storage.

    Drops entries that fail to load OR fail shape validation. Callers index
    `d["device_id"]` and `d["device_name"]` directly (e.g. cli.py's pull and
    `mm devices` table), so a JSON-valid but shape-invalid entry (non-dict
    top level, missing `device_id`, etc.) would crash the CLI without this
    guard.

    Dropped entries emit a per-entry warning via `stderr_console` at the
    call site — see `core._list_devices_warn`. Direct callers
    that import this function get silent drops (unchanged default) so
    library-mode consumers can read `devices` without side-effect output.
    """
    return inventory_devices(backend).valid


# Process-local set of device-short prefixes for which we've already emitted
# the "ambiguous prefix" mm: notice:. Avoids spamming stderr when
# lookup_device_by_short_id is called once per conflict in a multi-conflict
# walk against a fleet that has a real prefix collision.
_AMBIGUOUS_PREFIX_NOTICED: set[str] = set()

# Default retry budget for generate_unique_short_device_id. Each retry is a
# fresh uuid4 draw -- collisions are 1-in-4-billion per draw at 32 bits, so
# 5 retries makes a non-collision overwhelmingly likely even on a fleet with
# a deterministic-RNG bug. After exhaustion we emit a warning and fall back
# to the last-generated id; the runtime lookup_device_by_short_id helper
# still defends in depth via its multi-match path.
_GENERATE_DEVICE_ID_RETRY_BUDGET = 5


def generate_unique_short_device_id(
    devices: list[dict[str, Any]],
    *,
    max_retries: int = _GENERATE_DEVICE_ID_RETRY_BUDGET,
    backend: LocalBackend | None = None,
) -> str:
    """Generate an 8-char device id that doesn't collide with existing or retired peers.

    Today's ``device_id`` is ``uuid.uuid4().hex[:8]`` (32 bits). Collisions
    inside a fleet are extremely unlikely under healthy RNG (~1 in 4 billion
    per pair) but not impossible -- and a deterministic-RNG bug or a peer
    cloned from a snapshot could collide reproducibly. Init-time prevention
    is cheap; running ``list_devices`` once and retrying on collision gives
    forward defense plus a warning if the retry budget is exhausted.

    When ``backend`` is supplied, keys under ``retired/`` are also excluded so
    a newly initialized machine never draws an already-retired mm device ID.

    Returns the freshly-generated id. After ``max_retries`` consecutive
    collisions, emits a ``mm: warning:`` to stderr and returns the last id
    drawn -- the conflict-prompt-ux runtime still defends in depth via
    :func:`lookup_device_by_short_id`'s multi-match path, so a colliding
    install isn't catastrophic, just degraded for attribution.
    """
    existing = {d.get("device_id") for d in devices if isinstance(d.get("device_id"), str)}
    if backend is not None:
        try:
            for key in backend.list_keys(RETIRED_PREFIX):
                leaf = key.rsplit("/", 1)[-1]
                if is_conflict_name(leaf):
                    continue
                rid = parse_retired_record_key(key)
                if rid is not None:
                    existing.add(rid)
        except (OSError, StorageError):
            pass
    last_drawn = ""
    for _attempt in range(max_retries):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate
        last_drawn = candidate
    sys.stderr.write(
        f"mm: warning: could not generate a non-colliding device id in "
        f"{max_retries} attempts; proceeding with {last_drawn} -- attribution "
        "may be degraded if collisions persist\n"
    )
    return last_drawn


def lookup_device_by_short_id(
    devices: list[dict[str, Any]],
    short_id: str,
) -> tuple[dict[str, Any] | None, int]:
    """Resolve a conflict-filename's 8-char device prefix to a peer record.

    Returns ``(device_dict, match_count)``:
      * ``(None, 0)`` -- no peer matches the prefix (unknown peer).
      * ``(device, 1)`` -- exactly one match; safe to attribute.
      * ``(None, N)`` for N > 1 -- prefix collision; refuse to attribute,
        emit a one-shot ``mm: notice:`` to stderr (per-prefix, per-process)
        so the user has a forensic breadcrumb. Caller is expected to render
        an in-prompt "ambiguous -- N peers match" annotation using the
        match count returned here.

    Pure function over the existing devices list (callers should
    ``list_devices(backend)`` ONCE at the start of an interactive walk and
    pass the resulting list in for every conflict, avoiding N storage
    round-trips on a multi-conflict resolve).

    Today's ``device_short`` is the full 8-char ``uuid.uuid4().hex[:8]`` --
    so a "match" is exact-equality against ``device_id``. The function
    accepts shorter prefixes too (forward-defense if the convention ever
    grows or shrinks the prefix length); semantics are: any ``device_id``
    whose first ``len(short_id)`` characters equal ``short_id`` is a match.
    """
    matches = [d for d in devices if d.get("device_id", "")[: len(short_id)] == short_id]
    count = len(matches)
    if count == 0:
        return (None, 0)
    if count == 1:
        return (matches[0], 1)
    # Multiple matches: refuse to attribute. One-shot notice so a real
    # fleet-config issue surfaces forensically without spamming on every
    # conflict in a walk.
    if short_id not in _AMBIGUOUS_PREFIX_NOTICED:
        _AMBIGUOUS_PREFIX_NOTICED.add(short_id)
        names = ", ".join(sorted(d.get("device_id", "?") for d in matches))
        sys.stderr.write(
            f"mm: notice: device-id prefix {short_id!r} matches "
            f"{count} peers ({names}); attribution disabled for "
            "this prefix until you rename one of the devices\n"
        )
    return (None, count)


def inventory_devices(backend: LocalBackend) -> DeviceInventory:
    """Read ``devices/`` once (see `DeviceInventory`).

    A file counts as a device only when its name is that device's key
    (``devices/<id>.json`` holding ``device_id == <id>``). Everything else is
    foreign and never refuses a pull: a Syncthing conflict copy or an iCloud
    ``<id> 2.json`` would otherwise list the same device twice.
    """
    inventory = DeviceInventory()
    for key in backend.list_keys(DEVICES_PREFIX):
        key_id = parse_device_key(key)
        if key_id is None or is_conflict_name(key.rsplit("/", 1)[-1]):
            # Not `devices/<id>.json`, or a conflict copy of one: never read,
            # so a half-delivered copy cannot refuse the pull.
            inventory.foreign.append(key)
            continue
        try:
            raw = json.loads(backend.get(key, expected=True))
        except StorageNotFound:
            # Listed, then gone after the bounded retry: Syncthing is replacing
            # it. Not corrupt; the pull reports it as waiting.
            inventory.vanished.append(key_id)
            continue
        except (StorageError, ValueError) as e:
            inventory.dropped.append((key, f"unreadable ({type(e).__name__})"))
            continue
        # Shape validation: top-level must be a dict with a non-empty string
        # device_id and a string device_name. Anything else would crash
        # downstream `d["device_id"]` / `d["device_name"]` indexing.
        if not isinstance(raw, dict):
            inventory.dropped.append((key, "not a JSON object"))
            continue
        did = raw.get("device_id")
        dname = raw.get("device_name")
        if not isinstance(did, str) or not did:
            inventory.dropped.append((key, "missing or invalid device_id"))
            continue
        if did != key_id:
            inventory.foreign.append(key)
            continue
        if not isinstance(dname, str):
            inventory.dropped.append((key, "missing or invalid device_name"))
            continue
        inventory.valid.append(raw)
    return inventory


MAX_SYNCTHING_RECORD_BYTES = 4096


@dataclass
class FleetSyncthingRecords:
    """Syncthing records and the registry snapshot used to validate them."""

    vouched: dict[str, tuple[str, str]] = field(default_factory=dict)
    by_mm_id: dict[str, str] = field(default_factory=dict)
    own_syncthing_id: str | None = None
    newer_format: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    skipped_unregistered: list[str] = field(default_factory=list)
    inventory: DeviceInventory | None = field(default=None, repr=False, compare=False)


def publish_record(
    backend: LocalBackend,
    device_id: str,
    syncthing_device_id: str,
    passphrase: str,
    memory_kb: int = DEFAULT_ARGON2_MEMORY_KB,
) -> bool:
    """Write ``syncthing/<device_id>.enc`` when ``syncthing_device_id`` is valid.

    Never touches ``devices/<device_id>.json``. Returns True when written,
    False when ``syncthing_device_id`` is not a valid canonical device ID.
    """
    if not (
        isinstance(syncthing_device_id, str)
        and syncthing.is_device_id(syncthing_device_id)
        and syncthing.has_valid_device_id_check_digits(syncthing_device_id)
    ):
        return False
    key = syncthing_record_key(device_id)
    payload = {
        "mm_device_id": device_id,
        "syncthing_device_id": syncthing_device_id,
    }
    plaintext = json.dumps(payload, sort_keys=True).encode("utf-8")
    encrypted = crypto.encrypt(plaintext, passphrase, memory_kb)
    backend.put(key, encrypted)
    return True


def read_fleet_records(
    backend: LocalBackend,
    passphrase: str,
    memory_kb: int = DEFAULT_ARGON2_MEMORY_KB,
    *,
    own_device_id: str | None = None,
) -> FleetSyncthingRecords:
    """Read and authenticate ``syncthing/*.enc`` records under the fleet key.

    Never reads a Syncthing ID from ``devices/<id>.json``; the plaintext
    registry is used only to filter out records of unregistered (retired)
    devices without degradation and to associate each vouched ID with its
    registry device name.
    """
    records = FleetSyncthingRecords()
    keys = backend.list_keys(SYNCTHING_PREFIX)
    if not keys:
        return records
    inventory = inventory_devices(backend)
    records.inventory = inventory
    registered: dict[str, dict[str, Any]] = {d["device_id"]: d for d in inventory.valid}
    best_rank: dict[str, tuple[str, str, str]] = {}

    for key in keys:
        leaf = key.rsplit("/", 1)[-1]
        if is_conflict_name(leaf):
            continue
        key_id = parse_syncthing_record_key(key)
        if key_id is None:
            continue
        reg_entry = registered.get(key_id)
        if reg_entry is None:
            records.skipped_unregistered.append(key_id)
            continue
        path = backend.root / key
        try:
            st = path.stat()
        except FileNotFoundError:
            st = None
        except OSError:
            records.unreadable.append(key_id)
            continue
        if st is not None and st.st_size > MAX_SYNCTHING_RECORD_BYTES:
            records.unreadable.append(key_id)
            continue
        try:
            raw = backend.get(key, expected=True)
        except StorageNotFound:
            records.vanished.append(key_id)
            continue
        except (StorageError, OSError):
            records.unreadable.append(key_id)
            continue
        if len(raw) > MAX_SYNCTHING_RECORD_BYTES:
            records.unreadable.append(key_id)
            continue
        try:
            plaintext = crypto.decrypt(raw, passphrase, memory_kb)
        except NewerFormatError:
            records.newer_format.append(key_id)
            continue
        except CryptoError:
            records.unreadable.append(key_id)
            continue
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            records.unreadable.append(key_id)
            continue
        if not isinstance(payload, dict):
            records.unreadable.append(key_id)
            continue
        mm_id = payload.get("mm_device_id")
        st_id = payload.get("syncthing_device_id")
        if (
            not isinstance(mm_id, str)
            or mm_id != key_id
            or not isinstance(st_id, str)
            or not syncthing.is_device_id(st_id)
            or not syncthing.has_valid_device_id_check_digits(st_id)
        ):
            records.unreadable.append(key_id)
            continue
        if own_device_id is not None and key_id == own_device_id:
            records.own_syncthing_id = st_id
        records.by_mm_id[key_id] = st_id
        dev_name = reg_entry.get("device_name") or key_id
        rank = (
            str(reg_entry.get("last_seen") or ""),
            str(reg_entry.get("registered") or ""),
            key_id,
        )
        prior = best_rank.get(st_id)
        if prior is None or rank >= prior:
            best_rank[st_id] = rank
            records.vouched[st_id] = (key_id, dev_name)
    return records


def read_single_syncthing_record(
    backend: LocalBackend,
    device_id: str,
    passphrase: str,
    memory_kb: int = DEFAULT_ARGON2_MEMORY_KB,
) -> str | None:
    """Read and authenticate ``syncthing/<device_id>.enc`` directly, or return ``None``."""
    try:
        key = syncthing_record_key(device_id)
    except ValueError:
        return None
    path = backend.root / key
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > MAX_SYNCTHING_RECORD_BYTES:
        return None
    try:
        raw = backend.get(key, expected=True)
    except (StorageNotFound, StorageError, OSError):
        return None
    if len(raw) > MAX_SYNCTHING_RECORD_BYTES:
        return None
    try:
        plaintext = crypto.decrypt(raw, passphrase, memory_kb)
        payload = json.loads(plaintext.decode("utf-8"))
    except (NewerFormatError, CryptoError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("mm_device_id") != device_id:
        return None
    st_id = payload.get("syncthing_device_id")
    if (
        isinstance(st_id, str)
        and syncthing.is_device_id(st_id)
        and syncthing.has_valid_device_id_check_digits(st_id)
    ):
        return st_id
    return None


def normalize_device_name(raw_name: object) -> str:
    """Return the sanitized, trimmed, casefolded comparison key for a device name."""
    return safety.safe_terminal_str(str(raw_name or "")).strip().casefold()


def _parse_iso_ts(raw: object) -> datetime | None:
    """Parse an ISO-8601 timestamp string into an aware UTC datetime, or None."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 64:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        return None
    if dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class SupersededRegistration:
    """An unpublished registration superseded by a later published registration."""

    old_id: str
    name: str
    newer_id: str


@dataclass(frozen=True)
class UnpublishedSplit:
    """Result of splitting unpublished peer registrations into still-to-publish vs superseded."""

    unpublished_names: tuple[str, ...] = ()
    superseded: tuple[SupersededRegistration, ...] = ()
    unpublished_ids: tuple[str, ...] = ()
    unpublished_by_norm_name: dict[str, str] = field(default_factory=dict)


def published_record_mm_ids(
    backend: LocalBackend,
    records: FleetSyncthingRecords | None = None,
) -> set[str]:
    """Return every mm ``device_id`` that has a non-conflict ``syncthing/<id>.enc`` key.

    A device counts as having a published record whenever its non-conflict
    ``syncthing/<id>.enc`` key exists on disk, whether or not the payload
    decrypts on this machine.
    """
    if records is not None and records.inventory is not None:
        return set(records.by_mm_id).union(
            records.unreadable, records.newer_format, records.vanished
        )
    out: set[str] = set()
    try:
        for key in backend.list_keys(SYNCTHING_PREFIX):
            leaf = key.rsplit("/", 1)[-1]
            if is_conflict_name(leaf):
                continue
            mm_id = parse_syncthing_record_key(key)
            if mm_id is not None:
                out.add(mm_id)
    except (OSError, StorageError):
        if records is not None:
            return set(records.by_mm_id).union(
                records.unreadable, records.newer_format, records.vanished
            )
    return out


def split_unpublished(
    inventory: DeviceInventory,
    published_mm_ids: set[str] | frozenset[str],
    *,
    own_device_id: str | None = None,
) -> UnpublishedSplit:
    """Split unpublished peer registrations into still-to-publish vs superseded.

    An unpublished registration ``d`` is superseded when another valid
    registration in ``inventory.valid`` shares the same normalized sanitized
    device name, has a published ``syncthing/<id>.enc`` key in
    ``published_mm_ids``, has a strictly later valid ``registered`` timestamp,
    and ``d``'s ``last_seen`` is missing or not later than that newer
    registration's ``registered`` timestamp. Missing or unparseable timestamps
    never supersede or get superseded.
    """
    published_by_norm: dict[str, list[tuple[datetime, str]]] = {}
    for entry in inventory.valid:
        did = entry["device_id"]
        if did not in published_mm_ids:
            continue
        reg_dt = _parse_iso_ts(entry.get("registered"))
        if reg_dt is None:
            continue
        norm = normalize_device_name(entry.get("device_name") or did)
        if not norm:
            continue
        published_by_norm.setdefault(norm, []).append((reg_dt, did))

    unpublished_names: list[str] = []
    unpublished_ids: list[str] = []
    superseded: list[SupersededRegistration] = []
    by_norm: dict[str, str] = {}

    for entry in sorted(inventory.valid, key=lambda item: item["device_id"]):
        did = entry["device_id"]
        if did == own_device_id or did in published_mm_ids:
            continue
        safe_name = safety.safe_terminal_str(str(entry.get("device_name") or "")).strip() or did
        norm = safe_name.casefold()
        reg_dt = _parse_iso_ts(entry.get("registered"))
        raw_last_seen = entry.get("last_seen")
        if raw_last_seen is None or raw_last_seen == "":
            last_seen_dt: datetime | None = None
            last_seen_ok = True
        else:
            last_seen_dt = _parse_iso_ts(raw_last_seen)
            last_seen_ok = last_seen_dt is not None

        candidates: list[tuple[datetime, str]] = []
        if reg_dt is not None and last_seen_ok:
            for pub_reg_dt, pub_id in published_by_norm.get(norm, ()):
                if pub_id == did or pub_reg_dt <= reg_dt:
                    continue
                if last_seen_dt is None or last_seen_dt <= pub_reg_dt:
                    candidates.append((pub_reg_dt, pub_id))

        if candidates:
            _, newer_id = max(candidates, key=lambda item: (item[0], item[1]))
            superseded.append(SupersededRegistration(old_id=did, name=safe_name, newer_id=newer_id))
        else:
            unpublished_names.append(safe_name)
            unpublished_ids.append(did)
            by_norm.setdefault(norm, safe_name)

    return UnpublishedSplit(
        unpublished_names=tuple(unpublished_names),
        superseded=tuple(superseded),
        unpublished_ids=tuple(unpublished_ids),
        unpublished_by_norm_name=by_norm,
    )


def compose_pending_syncthing_join_message(
    base_message: str | None,
    unpublished_names: Sequence[str] = (),
) -> str | None:
    """Append an unpublished-machines remedy clause to ``base_message`` when needed."""
    if not base_message:
        return None
    names = [
        cleaned
        for raw in unpublished_names
        if (cleaned := safety.safe_terminal_str(str(raw)).strip())
    ]
    if not names:
        return base_message
    verb = "has" if len(names) == 1 else "have"
    target = "that machine" if len(names) == 1 else "those machines"
    return (
        f"{base_message} Registered fleet "
        f"{'machine' if len(names) == 1 else 'machines'} {', '.join(names)} "
        f"{verb} not published a Syncthing ID yet; run 'mm update' and 'mm push' on {target}."
    )


def compose_pending_syncthing_join_items(
    *,
    pending_count: int | None,
    pending_device_names: dict[str, str] | None = None,
    unpublished_names: Sequence[str] = (),
    superseded: Sequence[SupersededRegistration] = (),
) -> list[tuple[str, str]]:
    """Build ``(message, fix_command)`` pairs for knocking and superseded Syncthing devices."""
    count = (
        pending_count
        if not isinstance(pending_count, bool) and isinstance(pending_count, int)
        else 0
    )
    if count < 1:
        return []

    unpub_by_norm: dict[str, str] = {}
    cleaned_unpub: list[str] = []
    for raw in unpublished_names:
        cleaned = safety.safe_terminal_str(str(raw)).strip()
        if cleaned:
            cleaned_unpub.append(cleaned)
            unpub_by_norm.setdefault(cleaned.casefold(), cleaned)

    matched_names: list[str] = []
    unmatched_count = 0
    if pending_device_names:
        for _pid, raw_pname in sorted(pending_device_names.items()):
            norm_p = normalize_device_name(raw_pname)
            if norm_p and norm_p in unpub_by_norm:
                m_name = unpub_by_norm[norm_p]
                if m_name not in matched_names:
                    matched_names.append(m_name)
            else:
                unmatched_count += 1
    else:
        unmatched_count = count

    items: list[tuple[str, str]] = []
    if matched_names:
        noun = "machine" if len(matched_names) == 1 else "machines"
        verb = "is" if len(matched_names) == 1 else "are"
        has_verb = "has" if len(matched_names) == 1 else "have"
        target = "that machine" if len(matched_names) == 1 else "those machines"
        items.append(
            (
                f"Syncthing: registered fleet {noun} {', '.join(matched_names)} {verb} "
                f"asking to join and {has_verb} not published a Syncthing ID yet; "
                f"on {target}, run 'mm update', then 'mm push'.",
                "mm update",
            )
        )
    if unmatched_count > 0:
        remaining_unpub = [n for n in cleaned_unpub if n not in matched_names]
        msg = compose_pending_syncthing_join_message(
            syncthing.pending_join_message(unmatched_count),
            remaining_unpub,
        )
        if msg:
            items.append((msg, syncthing.PENDING_JOIN_FIX))
    for sup in superseded:
        s_name = safety.safe_terminal_str(sup.name).strip() or sup.old_id
        s_old = safety.safe_terminal_str(sup.old_id).strip()
        s_new = safety.safe_terminal_str(sup.newer_id).strip()
        items.append(
            (
                f"Syncthing: old {s_name} registration {s_old} superseded by {s_new}.",
                f"mm devices retire {s_old}",
            )
        )
    return items


@dataclass(frozen=True)
class RetirementRecord:
    """Authenticated retirement record from ``retired/<mm-id>.enc``."""

    mm_device_id: str
    device_name: str
    syncthing_device_id: str | None = None


@dataclass
class FleetRetirementRecords:
    """Decoded retirement records and per-key diagnostic buckets."""

    records: dict[str, RetirementRecord] = field(default_factory=dict)
    by_syncthing_id: dict[str, list[RetirementRecord]] = field(default_factory=dict)
    newer_format: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)


def write_retirement_record(
    backend: LocalBackend,
    mm_device_id: str,
    device_name: str,
    syncthing_device_id: str | None,
    passphrase: str,
    memory_kb: int = DEFAULT_ARGON2_MEMORY_KB,
) -> RetirementRecord:
    """Write ``retired/<mm_device_id>.enc`` durably (format 0x02, fsynced)."""
    clean_st_id: str | None = None
    if syncthing_device_id is not None:
        if not (
            isinstance(syncthing_device_id, str)
            and syncthing.is_device_id(syncthing_device_id)
            and syncthing.has_valid_device_id_check_digits(syncthing_device_id)
        ):
            raise ValueError(f"invalid syncthing_device_id: {syncthing_device_id!r}")
        clean_st_id = syncthing_device_id
    clean_name = (
        device_name.strip()
        if isinstance(device_name, str) and device_name.strip()
        else mm_device_id
    )
    key = retired_record_key(mm_device_id)
    payload = {
        "mm_device_id": mm_device_id,
        "device_name": clean_name,
        "syncthing_device_id": clean_st_id,
    }
    plaintext = json.dumps(payload, sort_keys=True).encode("utf-8")
    encrypted = crypto.encrypt(plaintext, passphrase, memory_kb)
    backend.put(key, encrypted)
    return RetirementRecord(
        mm_device_id=mm_device_id,
        device_name=clean_name,
        syncthing_device_id=clean_st_id,
    )


def read_retirement_records(
    backend: LocalBackend,
    passphrase: str,
    memory_kb: int = DEFAULT_ARGON2_MEMORY_KB,
) -> FleetRetirementRecords:
    """Read and authenticate ``retired/*.enc`` records under the fleet key."""
    out = FleetRetirementRecords()
    keys = backend.list_keys(RETIRED_PREFIX)
    if not keys:
        return out
    for key in sorted(keys):
        leaf = key.rsplit("/", 1)[-1]
        if is_conflict_name(leaf):
            continue
        key_id = parse_retired_record_key(key)
        if key_id is None:
            continue
        path = backend.root / key
        try:
            st = path.stat()
        except FileNotFoundError:
            st = None
        except OSError:
            out.unreadable.append(key_id)
            continue
        if st is not None and st.st_size > MAX_SYNCTHING_RECORD_BYTES:
            out.unreadable.append(key_id)
            continue
        try:
            raw = backend.get(key, expected=True)
        except StorageNotFound:
            out.vanished.append(key_id)
            continue
        except (StorageError, OSError):
            out.unreadable.append(key_id)
            continue
        if len(raw) > MAX_SYNCTHING_RECORD_BYTES:
            out.unreadable.append(key_id)
            continue
        try:
            plaintext = crypto.decrypt(raw, passphrase, memory_kb)
        except NewerFormatError:
            out.newer_format.append(key_id)
            continue
        except CryptoError:
            out.unreadable.append(key_id)
            continue
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            out.unreadable.append(key_id)
            continue
        if not isinstance(payload, dict):
            out.unreadable.append(key_id)
            continue
        mm_id = payload.get("mm_device_id")
        dev_name = payload.get("device_name")
        st_id = payload.get("syncthing_device_id")
        st_id_valid = st_id is None or (
            isinstance(st_id, str)
            and syncthing.is_device_id(st_id)
            and syncthing.has_valid_device_id_check_digits(st_id)
        )
        if (
            not isinstance(mm_id, str)
            or mm_id != key_id
            or not isinstance(dev_name, str)
            or not dev_name.strip()
            or not st_id_valid
        ):
            out.unreadable.append(key_id)
            continue
        record = RetirementRecord(
            mm_device_id=mm_id,
            device_name=dev_name.strip(),
            syncthing_device_id=st_id,
        )
        out.records[key_id] = record
        if st_id is not None:
            out.by_syncthing_id.setdefault(st_id, []).append(record)
    return out
