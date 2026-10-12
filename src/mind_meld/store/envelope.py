"""Explicit v3 fleet verification and authenticated, bounded byte envelopes.

No legacy session/cache, keyring or storage calls. See docs/invariants/store.md.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import io
import json
import math
import os
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mind_meld import crypto
from mind_meld.errors import CryptoError

FORMAT_VERSION = 0x03
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_SEALED_PART_BYTES = MAX_PART_BYTES + 64 * 1024
MAX_PARTS = 512
MAX_SEQ = 999_999_999_999
MAX_FLEET_BYTES = 16 * 1024
MAX_PRESENCE_BYTES = 64 * 1024
PART_WRAPPER_BYTES = 40
NONCE_BYTES = 12
TAG_BYTES = 16
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)

Kind = Literal["commit", "snapshot", "presence"]
ErrorCategory = Literal[
    "unsupported_format",
    "invalid_fleet",
    "authentication",
    "invalid_payload",
    "invalid_parts",
    "resource_limit",
]
_GUIDANCE = {
    "unsupported_format": "Unsupported store format or algorithm; use a compatible reader.",
    "invalid_fleet": (
        "Invalid fleet metadata or passphrase input; the header may be damaged or the "
        "passphrase empty. Check the passphrase, then preserve the header and inspect the fleet."
    ),
    "authentication": (
        "Authentication failed; the passphrase, expected context or bytes may differ. "
        "Check the passphrase and expected identity, and preserve the inputs."
    ),
    "invalid_payload": (
        "Malformed store payload; the record or producer may be damaged. "
        "Preserve the inputs and inspect the producer."
    ),
    "invalid_parts": (
        "Inconsistent store parts or context; inputs may be mixed or misnamed. "
        "Preserve the inputs and inspect their identities."
    ),
    "resource_limit": (
        "Store resource limit exceeded or key derivation unavailable; "
        "inspect the record, producer and machine resources. Do not truncate or reinitialize."
    ),
}


class StoreError(Exception):
    """Safe failure with a stable category; never includes untrusted text."""

    def __init__(self, category: ErrorCategory):
        self.category = category
        super().__init__(_GUIDANCE[category] + " See docs/invariants/store.md#errors.")


def _json_bytes(value: Any, *, category: ErrorCategory, limit: int) -> bytes:
    try:
        data = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError(category) from None
    if len(data) > limit:
        raise StoreError("resource_limit")
    return data


def _read_json(data: bytes, *, category: ErrorCategory, limit: int) -> Any:
    if not isinstance(data, bytes):
        raise StoreError(category)
    if len(data) > limit:
        raise StoreError("resource_limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise ValueError("nonfinite value")

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        # json accepts escaped lone surrogates and 1e999; neither is valid wire data.
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("nonfinite value")
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise StoreError(category) from None


def _fields(value: Any, required: set[str], optional: set[str], category: ErrorCategory) -> None:
    if not isinstance(value, dict) or not required <= value.keys() <= required | optional:
        raise StoreError(category)


def _integer(value: Any, low: int, high: int, category: ErrorCategory) -> None:
    if type(value) is not int or value < low:
        raise StoreError(category)
    if value > high:
        raise StoreError("resource_limit")


def _hex(value: Any, length: int, category: ErrorCategory) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None:
        raise StoreError(category)


def _text(value: Any, category: ErrorCategory) -> None:
    if not isinstance(value, str) or not value:
        raise StoreError(category)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise StoreError(category) from None


def _timestamp(value: Any, category: ErrorCategory) -> None:
    _text(value, category)
    # One RFC 3339 profile on every Python: fromisoformat's accepted set varies by
    # version (3.14 takes T24:00) and admits any separator, including control text.
    if _TIMESTAMP.fullmatch(value) is None:
        raise StoreError(category)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise StoreError(category) from None


@dataclass(frozen=True)
class RecordContext:
    """Expected identity from verified fleet + layout/caller, never ciphertext."""

    fleet_id: str
    device_id: str
    kind: Kind
    seq: int
    part: int = 1
    nparts: int = 1

    def __post_init__(self) -> None:
        _hex(self.fleet_id, 32, "invalid_parts")
        _hex(self.device_id, 8, "invalid_parts")
        if self.kind not in ("commit", "snapshot", "presence"):
            raise StoreError("invalid_parts")
        _integer(self.seq, 1 if self.kind == "commit" else 0, MAX_SEQ, "invalid_parts")
        _integer(self.nparts, 1, MAX_PARTS, "invalid_parts")
        _integer(self.part, 1, MAX_PARTS, "invalid_parts")
        if self.part > self.nparts:
            raise StoreError("invalid_parts")
        if self.kind == "presence" and (self.seq, self.part, self.nparts) != (0, 1, 1):
            raise StoreError("invalid_parts")

    def associated_data(self) -> bytes:
        """The exact domain-separated tuple AAD (canonical UTF-8 JSON)."""
        return b"mm-record-v3\0" + _json_bytes(
            [self.fleet_id, self.device_id, self.kind, self.seq, self.part, self.nparts],
            category="invalid_parts",
            limit=1024,
        )


@dataclass(frozen=True)
class FleetKeys:
    """Explicit verified fleet keys; create/unlock owns construction, no global cache."""

    fleet_id: str
    created_at: str
    memory_kb: int
    time_cost: int
    parallelism: int
    salt: bytes = field(repr=False)
    master_key: bytes = field(repr=False)
    commit_key: bytes = field(repr=False)
    snapshot_key: bytes = field(repr=False)
    presence_key: bytes = field(repr=False)
    keycheck_key: bytes = field(repr=False)


def _fleet_keys(header: dict[str, Any], master: bytes, salt: bytes) -> FleetKeys:
    subkeys = [
        HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=bytes.fromhex(header["fleet_id"]),
            info=b"mm-store-v3/" + kind.encode("ascii"),
        ).derive(master)
        for kind in ("commit", "snapshot", "presence", "keycheck")
    ]
    kdf = header["kdf"]
    return FleetKeys(
        header["fleet_id"],
        header["created_at"],
        kdf["memory_kb"],
        kdf["time"],
        kdf["parallelism"],
        salt,
        master,
        *subkeys,
    )


def _kdf(kdf: Any) -> None:
    if not isinstance(kdf, dict) or "name" not in kdf:
        raise StoreError("invalid_fleet")
    _text(kdf["name"], "invalid_fleet")
    if kdf["name"] != "argon2id":
        raise StoreError("unsupported_format")
    _fields(kdf, {"name", "memory_kb", "time", "parallelism"}, set(), "invalid_fleet")
    # KDF policy violations are metadata errors, not a native allocation attempt.
    for name, low, high in (("memory_kb", 1024, 262144), ("time", 1, 10), ("parallelism", 1, 16)):
        value = kdf[name]
        if type(value) is not int or not low <= value <= high:
            raise StoreError("invalid_fleet")
    # Implied by today's floors (1024 >= 8 * 16); kept so a lowered floor cannot skip it.
    if kdf["memory_kb"] < 8 * kdf["parallelism"]:
        raise StoreError("invalid_fleet")


def _base64(value: Any, *, length: int | None = None) -> bytes:
    _text(value, "invalid_fleet")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise StoreError("invalid_fleet") from None
    if base64.b64encode(raw).decode("ascii") != value or (
        length is not None and len(raw) != length
    ):
        raise StoreError("invalid_fleet")
    return raw


def _header(data: bytes) -> tuple[dict[str, Any], bytes, bytes]:
    header = _read_json(data, category="invalid_fleet", limit=MAX_FLEET_BYTES)
    # A future discriminator refuses without assuming that version's field layout.
    if not isinstance(header, dict) or "format" not in header:
        raise StoreError("invalid_fleet")
    if type(header["format"]) is not int or header["format"] < 3:
        raise StoreError("invalid_fleet")
    if header["format"] > 3:
        raise StoreError("unsupported_format")
    _fields(
        header,
        {"format", "fleet_id", "created_at", "kdf", "salt", "keycheck"},
        set(),
        "invalid_fleet",
    )
    _hex(header["fleet_id"], 32, "invalid_fleet")
    _timestamp(header["created_at"], "invalid_fleet")
    _kdf(header["kdf"])
    salt = _base64(header["salt"], length=16)
    keycheck = _base64(header["keycheck"])
    _wire_precheck(keycheck, MAX_FLEET_BYTES, "invalid_fleet")
    return header, salt, keycheck


def _derive(passphrase: str, salt: bytes, kdf: dict[str, Any]) -> bytes:
    _text(passphrase, "invalid_fleet")
    try:
        return crypto.derive_key(
            passphrase,
            salt,
            memory_kb=kdf["memory_kb"],
            time_cost=kdf["time"],
            parallelism=kdf["parallelism"],
        )
    except CryptoError:
        # The legacy exception's advice concerns v2 config; it is wrong for an existing fleet.
        raise StoreError("resource_limit") from None


def _fleet_aad(header: dict[str, Any]) -> bytes:
    return b"mm-fleet-v3\0" + _json_bytes(
        {key: value for key, value in header.items() if key != "keycheck"},
        category="invalid_fleet",
        limit=MAX_FLEET_BYTES,
    )


def create_fleet(
    passphrase: str,
    *,
    created_at: str | None = None,
    memory_kb: int = 65536,
    time_cost: int = 3,
    parallelism: int = 1,
) -> tuple[bytes, FleetKeys]:
    """Return canonical fleet.json bytes and keys, deriving once; does not publish."""
    kdf = {
        "name": "argon2id",
        "memory_kb": memory_kb,
        "time": time_cost,
        "parallelism": parallelism,
    }
    _kdf(kdf)
    _text(passphrase, "invalid_fleet")
    stamp = datetime.now(timezone.utc).isoformat() if created_at is None else created_at
    _timestamp(stamp, "invalid_fleet")
    header = {"format": 3, "fleet_id": os.urandom(16).hex(), "created_at": stamp, "kdf": kdf}
    salt = os.urandom(16)
    header["salt"] = base64.b64encode(salt).decode("ascii")
    keys = _fleet_keys(header, _derive(passphrase, salt, kdf), salt)
    keycheck = _seal(b"mm-keycheck-v3", keys.keycheck_key, _fleet_aad(header))
    header["keycheck"] = base64.b64encode(keycheck).decode("ascii")
    return _json_bytes(header, category="invalid_fleet", limit=MAX_FLEET_BYTES), keys


def unlock_fleet(data: bytes, passphrase: str) -> FleetKeys:
    """Prevalidate untrusted fleet bytes, derive once and authenticate the entire header."""
    header, salt, keycheck = _header(data)
    keys = _fleet_keys(header, _derive(passphrase, salt, header["kdf"]), salt)
    check = _open(
        keycheck,
        keys.keycheck_key,
        _fleet_aad(header),
        allowance=64,
        sealed_limit=MAX_FLEET_BYTES,
        category="invalid_fleet",
    )
    if check != b"mm-keycheck-v3":
        raise StoreError("authentication")
    return keys


def _seal(plaintext: bytes, key: bytes, aad: bytes) -> bytes:
    # GzipFile fixes mtime=0 and OS=255 across supported Python versions.
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as writer:
        writer.write(plaintext)
    nonce = os.urandom(NONCE_BYTES)
    return bytes([FORMAT_VERSION]) + nonce + AESGCM(key).encrypt(nonce, output.getvalue(), aad)


def _wire_precheck(data: bytes, limit: int, category: ErrorCategory) -> None:
    if not isinstance(data, bytes) or not data:
        raise StoreError(category)
    if 0x04 <= data[0] <= 0x0F:
        raise StoreError("unsupported_format")
    if data[0] != FORMAT_VERSION or len(data) < 1 + NONCE_BYTES + TAG_BYTES:
        raise StoreError(category)
    if len(data) > limit:
        raise StoreError("resource_limit")


def _open(
    data: bytes,
    key: bytes,
    aad: bytes,
    *,
    allowance: int,
    sealed_limit: int,
    category: ErrorCategory,
) -> bytes:
    _wire_precheck(data, sealed_limit, category)
    try:
        compressed = AESGCM(key).decrypt(data[1:13], data[13:], aad)
    except InvalidTag:
        raise StoreError("authentication") from None
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        plaintext = decoder.decompress(compressed, allowance + 1)
    except zlib.error:
        raise StoreError(category) from None
    if len(plaintext) > allowance or decoder.unconsumed_tail:
        raise StoreError("resource_limit")
    if not decoder.eof or decoder.unused_data:
        raise StoreError(category)
    return plaintext


def _record_subkey(keys: FleetKeys, expected: RecordContext) -> bytes:
    if not isinstance(keys, FleetKeys) or not isinstance(expected, RecordContext):
        raise StoreError("invalid_parts")
    if keys.fleet_id != expected.fleet_id:
        raise StoreError("authentication")
    key = getattr(keys, expected.kind + "_key")
    if not isinstance(key, bytes) or len(key) != 32:
        raise StoreError("invalid_fleet")
    return key


def _allowance(expected: RecordContext) -> int:
    return (
        MAX_PRESENCE_BYTES if expected.kind == "presence" else MAX_PART_BYTES
    ) + PART_WRAPPER_BYTES


def seal_part(plaintext: bytes, keys: FleetKeys, *, expected: RecordContext) -> bytes:
    """Seal bounded part plaintext; format.seal_record supplies its authenticated wrapper."""
    key = _record_subkey(keys, expected)
    if not isinstance(plaintext, bytes):
        raise StoreError("invalid_payload")
    if len(plaintext) > _allowance(expected):
        raise StoreError("resource_limit")
    data = _seal(plaintext, key, expected.associated_data())
    if len(data) > MAX_SEALED_PART_BYTES:
        raise StoreError("resource_limit")
    return data


def open_part(data: bytes, keys: FleetKeys, *, expected: RecordContext) -> bytes:
    """Authenticate independently supplied identity before bounded single-member gzip decoding."""
    # Inspect the version/length before key selection or assuming any layout.
    _wire_precheck(data, MAX_SEALED_PART_BYTES, "invalid_payload")
    key = _record_subkey(keys, expected)
    return _open(
        data,
        key,
        expected.associated_data(),
        allowance=_allowance(expected),
        sealed_limit=MAX_SEALED_PART_BYTES,
        category="invalid_payload",
    )
