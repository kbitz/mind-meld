"""Pure v3 logical record codecs and all-or-nothing authenticated assembly.

Logical payloads are byte APIs using O(payload) memory, not streaming apply.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

from mind_meld.store import envelope

MAX_PART_BYTES = envelope.MAX_PART_BYTES
MAX_RECORD_BYTES = 4 * 1024**3
MAX_HEADER_BYTES = 1024**2
MAX_METADATA_BYTES = 64 * 1024
MAX_ENTRIES = 1_000_000
MAX_VECTOR_COUNTER = 2**63 - 1
_HEADER_FIELDS = {
    "fleet_id",
    "device_id",
    "host",
    "seq",
    "prev",
    "created_at",
    "mm_version",
    "entries",
}
_ENTRY_FIELDS = {"source", "path", "vv", "sha", "size", "mtime", "deleted"}
_PRESENCE_FIELDS = {
    "device_id",
    "device_name",
    "os",
    "mm_version",
    "host",
    "syncthing_id",
    "registered",
    "last_active",
    "published_seq",
    "snapshot_seq",
    "applied",
}


@dataclass(frozen=True)
class Entry:
    """Metadata plus exact live bytes, or None for a tombstone; never an apply action."""

    metadata: dict[str, Any]
    content: bytes | None = None


@dataclass(frozen=True)
class Record:
    """Validated header and every retained entry, in supplied wire order."""

    header: dict[str, Any]
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class IncompleteParts:
    """Valid missing input. None means the part count is not yet known."""

    missing: tuple[int, ...] | None
    nparts: int | None

    @property
    def remedy(self) -> str:
        return "Wait for the remaining parts."


def host_fingerprint(fleet_id: str, machine_id: str) -> str:
    """Hash raw fleet ID + trimmed supplied machine ID UTF-8; performs no OS reads."""
    envelope._hex(fleet_id, 32, "invalid_payload")
    envelope._text(machine_id, "invalid_payload")
    machine_id = machine_id.strip()
    envelope._text(machine_id, "invalid_payload")
    return hashlib.sha256(bytes.fromhex(fleet_id) + machine_id.encode("utf-8")).digest()[:16].hex()


def _expected(expected: envelope.RecordContext, kind: envelope.Kind) -> None:
    if not isinstance(expected, envelope.RecordContext) or expected.kind != kind:
        raise envelope.StoreError("invalid_payload")


def _counter_map(value: Any, *, vector: bool = False) -> None:
    if not isinstance(value, dict) or (vector and not value):
        raise envelope.StoreError("invalid_payload")
    for device, counter in value.items():
        envelope._hex(device, 8, "invalid_payload")
        envelope._integer(
            counter,
            1 if vector else 0,
            MAX_VECTOR_COUNTER if vector else envelope.MAX_SEQ,
            "invalid_payload",
        )


def _header(header: Any, *, expected: envelope.RecordContext, kind: envelope.Kind) -> None:
    _expected(expected, kind)
    fields = _HEADER_FIELDS | ({"cursors"} if kind == "snapshot" else set())
    envelope._fields(header, fields, set(), "invalid_payload")
    envelope._hex(header["fleet_id"], 32, "invalid_payload")
    envelope._hex(header["device_id"], 8, "invalid_payload")
    envelope._hex(header["host"], 32, "invalid_payload")
    envelope._integer(
        header["seq"], 1 if kind == "commit" else 0, envelope.MAX_SEQ, "invalid_payload"
    )
    envelope._integer(header["entries"], 0, MAX_ENTRIES, "invalid_payload")
    if (header["fleet_id"], header["device_id"], header["seq"]) != (
        expected.fleet_id,
        expected.device_id,
        expected.seq,
    ):
        raise envelope.StoreError("invalid_payload")
    genesis = header["seq"] == (1 if kind == "commit" else 0)
    if genesis:
        if header["prev"] is not None:
            raise envelope.StoreError("invalid_payload")
    else:
        envelope._hex(header["prev"], 64, "invalid_payload")
    envelope._timestamp(header["created_at"], "invalid_payload")
    envelope._text(header["mm_version"], "invalid_payload")
    if kind == "snapshot":
        _counter_map(header["cursors"])


def _path(value: Any, *, component: bool = False) -> None:
    envelope._text(value, "invalid_payload")
    if (
        "\0" in value
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
        or any(segment in ("", ".", "..") for segment in value.split("/"))
        or (component and "/" in value)
    ):
        raise envelope.StoreError("invalid_payload")


def _metadata(meta: Any, kind: envelope.Kind) -> None:
    envelope._fields(
        meta, _ENTRY_FIELDS, {"held"} if kind == "snapshot" else set(), "invalid_payload"
    )
    _path(meta["source"], component=True)
    _path(meta["path"])
    _counter_map(meta["vv"], vector=True)
    if type(meta["deleted"]) is not bool or type(meta.get("held", False)) is not bool:
        raise envelope.StoreError("invalid_payload")
    envelope._integer(meta["size"], 0, MAX_RECORD_BYTES, "invalid_payload")
    if meta["deleted"]:
        if meta["sha"] is not None or meta["size"] != 0 or meta["mtime"] is not None:
            raise envelope.StoreError("invalid_payload")
    else:
        envelope._hex(meta["sha"], 64, "invalid_payload")
        value = meta["mtime"]
        try:
            valid_time = type(value) in (int, float) and value >= 0 and math.isfinite(value)
        except OverflowError:
            valid_time = False
        if not valid_time:
            raise envelope.StoreError("invalid_payload")


def _content(entry: Entry) -> None:
    meta, content = entry.metadata, entry.content
    if meta["deleted"]:
        if content is not None:
            raise envelope.StoreError("invalid_payload")
    elif (
        not isinstance(content, bytes)
        or len(content) != meta["size"]
        or hashlib.sha256(content).hexdigest() != meta["sha"]
    ):
        raise envelope.StoreError("invalid_payload")


def _unique(meta: dict[str, Any], kind: envelope.Kind, paths: set, versions: set) -> None:
    path = (meta["source"], meta["path"])
    if kind == "snapshot":
        version = (path, tuple(sorted(meta["vv"].items())))
        if version in versions:
            raise envelope.StoreError("invalid_payload")
        versions.add(version)
    if kind == "commit" or not meta.get("held", False):
        if path in paths:
            raise envelope.StoreError("invalid_payload")
        paths.add(path)


def _prefix(length: int) -> bytes:
    if length > 2**32 - 1:
        raise envelope.StoreError("resource_limit")
    return struct.pack(">I", length)


def _frame(data: bytes) -> bytes:
    return _prefix(len(data)) + data


def _read_frame(payload: bytes, offset: int, limit: int) -> tuple[bytes, int]:
    if len(payload) - offset < 4:
        raise envelope.StoreError("invalid_payload")
    length = struct.unpack_from(">I", payload, offset)[0]
    if length > limit:
        raise envelope.StoreError("resource_limit")
    start = offset + 4
    if length > len(payload) - start:
        raise envelope.StoreError("invalid_payload")
    return payload[start : start + length], start + length


def _payload(payload: bytes) -> None:
    if not isinstance(payload, bytes):
        raise envelope.StoreError("invalid_payload")
    if len(payload) > MAX_RECORD_BYTES:
        raise envelope.StoreError("resource_limit")


def _encode(
    header: dict[str, Any],
    entries: Sequence[Entry],
    *,
    expected: envelope.RecordContext,
    kind: envelope.Kind,
) -> bytes:
    _header(header, expected=expected, kind=kind)
    if len(entries) != header["entries"]:
        raise envelope.StoreError("invalid_payload")
    frames = [
        _frame(envelope._json_bytes(header, category="invalid_payload", limit=MAX_HEADER_BYTES))
    ]
    total = len(frames[0])
    paths: set = set()
    versions: set = set()
    for entry in entries:
        if not isinstance(entry, Entry):
            raise envelope.StoreError("invalid_payload")
        _metadata(entry.metadata, kind)
        _content(entry)
        _unique(entry.metadata, kind, paths, versions)
        meta = envelope._json_bytes(
            entry.metadata, category="invalid_payload", limit=MAX_METADATA_BYTES
        )
        # Check aggregate lengths before copying content into a framed buffer.
        addition = 4 + len(meta) + (0 if entry.content is None else 4 + len(entry.content))
        if total + addition > MAX_RECORD_BYTES:
            raise envelope.StoreError("resource_limit")
        total += addition
        frames.append(_frame(meta))
        if entry.content is not None:
            # Separate prefix and content so the final join is the only content copy.
            frames += (_prefix(len(entry.content)), entry.content)
    return b"".join(frames)


def _decode(payload: bytes, *, expected: envelope.RecordContext, kind: envelope.Kind) -> Record:
    _payload(payload)
    frame, offset = _read_frame(payload, 0, MAX_HEADER_BYTES)
    header = envelope._read_json(frame, category="invalid_payload", limit=MAX_HEADER_BYTES)
    _header(header, expected=expected, kind=kind)
    # Every entry needs at least one frame prefix; never loop an unbacked count.
    if header["entries"] > (len(payload) - offset) // 4:
        raise envelope.StoreError("invalid_payload")
    entries = []
    paths: set = set()
    versions: set = set()
    for _ in range(header["entries"]):
        frame, offset = _read_frame(payload, offset, MAX_METADATA_BYTES)
        meta = envelope._read_json(frame, category="invalid_payload", limit=MAX_METADATA_BYTES)
        _metadata(meta, kind)
        _unique(meta, kind, paths, versions)
        content = None
        if not meta["deleted"]:
            content, offset = _read_frame(payload, offset, MAX_RECORD_BYTES)
        entry = Entry(meta, content)
        _content(entry)
        entries.append(entry)
    if offset != len(payload):
        raise envelope.StoreError("invalid_payload")
    return Record(header, tuple(entries))


def encode_commit(
    header: dict[str, Any], entries: Sequence[Entry], *, expected: envelope.RecordContext
) -> bytes:
    """Encode exact commit frames; caller supplies prev hash, never lifecycle state."""
    return _encode(header, entries, expected=expected, kind="commit")


def decode_commit(payload: bytes, *, expected: envelope.RecordContext) -> Record:
    """Validate commit identity, framing, metadata and content hashes; no chain comparison."""
    return _decode(payload, expected=expected, kind="commit")


def encode_snapshot(
    header: dict[str, Any], entries: Sequence[Entry], *, expected: envelope.RecordContext
) -> bytes:
    """Encode full retained state, including tombstones and distinct held versions."""
    return _encode(header, entries, expected=expected, kind="snapshot")


def decode_snapshot(payload: bytes, *, expected: envelope.RecordContext) -> Record:
    """Validate snapshot frames; prev names the commit at seq, not a previous snapshot."""
    return _decode(payload, expected=expected, kind="snapshot")


def _presence(value: Any, expected: envelope.RecordContext) -> None:
    _expected(expected, "presence")
    envelope._fields(value, _PRESENCE_FIELDS, set(), "invalid_payload")
    envelope._hex(value["device_id"], 8, "invalid_payload")
    envelope._hex(value["host"], 32, "invalid_payload")
    if value["device_id"] != expected.device_id:
        raise envelope.StoreError("invalid_payload")
    for key in ("device_name", "os", "mm_version", "syncthing_id"):
        envelope._text(value[key], "invalid_payload")
    for key in ("registered", "last_active"):
        envelope._timestamp(value[key], "invalid_payload")
    for key in ("published_seq", "snapshot_seq"):
        envelope._integer(value[key], 0, envelope.MAX_SEQ, "invalid_payload")
    if value["snapshot_seq"] > value["published_seq"]:
        raise envelope.StoreError("invalid_payload")
    _counter_map(value["applied"])


def encode_presence(value: dict[str, Any], *, expected: envelope.RecordContext) -> bytes:
    """Encode one canonical JSON object; payload counters never bootstrap AAD."""
    _presence(value, expected)
    return envelope._json_bytes(
        value, category="invalid_payload", limit=envelope.MAX_PRESENCE_BYTES
    )


def decode_presence(payload: bytes, *, expected: envelope.RecordContext) -> dict[str, Any]:
    """Validate self-contained presence at fixed seq0/part1/nparts1; does not prove freshness."""
    value = envelope._read_json(
        payload, category="invalid_payload", limit=envelope.MAX_PRESENCE_BYTES
    )
    _presence(value, expected)
    return value


def _validate_record(payload: bytes, expected: envelope.RecordContext) -> None:
    if expected.kind == "commit":
        decode_commit(payload, expected=expected)
    elif expected.kind == "snapshot":
        decode_snapshot(payload, expected=expected)
    else:
        decode_presence(payload, expected=expected)


def _anchor(expected: envelope.RecordContext) -> None:
    if not isinstance(expected, envelope.RecordContext) or (expected.part, expected.nparts) != (
        1,
        1,
    ):
        raise envelope.StoreError("invalid_parts")


def seal_record(
    payload: bytes, keys: envelope.FleetKeys, *, expected: envelope.RecordContext
) -> Iterator[tuple[envelope.RecordContext, bytes]]:
    """Yield (full part context, sealed bytes) incrementally from a validated logical payload.

    expected is the independently known record identity with part=1,nparts=1;
    actual part counts are calculated from payload bytes, before compression.
    """
    _anchor(expected)
    # Check keys belong to the anchor fleet before any work; the subkey is unused here.
    envelope._record_subkey(keys, expected)
    _payload(payload)
    _validate_record(payload, expected)
    nparts = max(1, (len(payload) + MAX_PART_BYTES - 1) // MAX_PART_BYTES)
    digest = hashlib.sha256(payload).digest()
    wrapper = digest + struct.pack(">Q", len(payload))
    for part in range(1, nparts + 1):
        chunk = payload[(part - 1) * MAX_PART_BYTES : part * MAX_PART_BYTES]
        full_context = replace(expected, part=part, nparts=nparts)
        yield full_context, envelope.seal_part(wrapper + chunk, keys, expected=full_context)


def open_record(
    parts: Iterable[tuple[envelope.RecordContext, bytes]],
    keys: envelope.FleetKeys,
    *,
    expected: envelope.RecordContext,
    nparts: int | None = None,
) -> bytes | IncompleteParts:
    """Validate every supplied part, then return incomplete or an assembled, validated payload.

    expected anchors fleet/device/kind/seq (part=1,nparts=1). Each full part
    context must come from its independently parsed name; nparts can provide
    an independently known count even when the iterable is empty.
    """
    _anchor(expected)
    # Check keys belong to the anchor fleet before any work; the subkey is unused here.
    envelope._record_subkey(keys, expected)
    if nparts is not None:
        envelope._integer(nparts, 1, envelope.MAX_PARTS, "invalid_parts")
    if expected.kind == "presence":
        if nparts not in (None, 1):
            raise envelope.StoreError("invalid_parts")
        nparts = 1
    chunks: dict[int, bytes] = {}
    identity: tuple[bytes, int] | None = None
    for item in parts:
        if not isinstance(item, tuple) or len(item) != 2:
            raise envelope.StoreError("invalid_parts")
        full_context, sealed = item
        if not isinstance(full_context, envelope.RecordContext) or (
            full_context.fleet_id,
            full_context.device_id,
            full_context.kind,
            full_context.seq,
        ) != (expected.fleet_id, expected.device_id, expected.kind, expected.seq):
            raise envelope.StoreError("invalid_parts")
        if full_context.part in chunks or (nparts is not None and nparts != full_context.nparts):
            raise envelope.StoreError("invalid_parts")
        nparts = full_context.nparts
        plain = envelope.open_part(sealed, keys, expected=full_context)
        if len(plain) < envelope.PART_WRAPPER_BYTES:
            raise envelope.StoreError("invalid_parts")
        digest, total = plain[:32], struct.unpack_from(">Q", plain, 32)[0]
        if total > MAX_RECORD_BYTES:
            raise envelope.StoreError("resource_limit")
        if nparts != max(1, (total + MAX_PART_BYTES - 1) // MAX_PART_BYTES):
            raise envelope.StoreError("invalid_parts")
        current_identity = digest, total
        if identity is not None and current_identity != identity:
            raise envelope.StoreError("invalid_parts")
        identity = current_identity
        length = (
            MAX_PART_BYTES if full_context.part < nparts else total - MAX_PART_BYTES * (nparts - 1)
        )
        if len(plain) - envelope.PART_WRAPPER_BYTES != length:
            raise envelope.StoreError("invalid_parts")
        chunks[full_context.part] = plain[envelope.PART_WRAPPER_BYTES :]
    if nparts is None:
        return IncompleteParts(None, None)
    missing = tuple(part for part in range(1, nparts + 1) if part not in chunks)
    if missing:
        return IncompleteParts(missing, nparts)
    assert identity is not None  # A nonempty complete set established its digest/total.
    payload = b"".join(chunks[part] for part in range(1, nparts + 1))
    # Release the per-part copies before hashing and decoding (peak ~2P, not 3P).
    chunks.clear()
    if len(payload) != identity[1] or hashlib.sha256(payload).digest() != identity[0]:
        raise envelope.StoreError("invalid_parts")
    _validate_record(payload, expected)
    return payload
