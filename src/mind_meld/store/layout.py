"""Strict v3 keys and foreign/fork classification. Standard library only, no I/O."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

FLEET_KEY = "fleet.json"
STIGNORE_KEY = ".stignore"
# Mirrors envelope.MAX_SEQ / MAX_PARTS without a crypto import; T13 pins them equal.
MAX_SEQ = 999_999_999_999
MAX_PARTS = 512
_DEVICE = re.compile(r"[0-9a-f]{8}")
_LOG_SNAP = re.compile(
    r"dev/(?P<device>[0-9a-f]{8})/(?P<directory>log|snap)/(?P<seq>[0-9]{12})"
    r"(?:\.(?P<part>[1-9][0-9]{0,2})of(?P<nparts>[1-9][0-9]{0,2}))?\.enc"
)
_PRESENCE = re.compile(r"dev/(?P<device>[0-9a-f]{8})/presence\.enc")
_CONFLICT = re.compile(
    r"(?P<stem>.+)\.sync-conflict-(?P<date>[0-9]{8})-(?P<time>[0-9]{6})-"
    r"(?P<peer>[A-Za-z0-9]{1,64})(?P<extension>\.json|\.enc)"
)


@dataclass(frozen=True)
class RecordName:
    """Identity from a canonical key. Caller adds the independently verified fleet ID."""

    device_id: str
    kind: Literal["commit", "snapshot", "presence"]
    seq: int
    part: int = 1
    nparts: int = 1


@dataclass(frozen=True)
class KeyClassification:
    """Inventory facts; forks are already included in foreign_count, never additive."""

    records: tuple[tuple[str, RecordName], ...]
    controls: tuple[str, ...]
    foreign: tuple[str, ...]
    forks: tuple[str, ...]

    @property
    def foreign_count(self) -> int:
        return len(self.foreign)


def record_key(
    device_id: str,
    kind: Literal["commit", "snapshot", "presence"],
    seq: int,
    *,
    part: int = 1,
    nparts: int = 1,
) -> str:
    """Build a canonical key; invalid caller components raise a safe ValueError.

    Untrusted keys belong in parse_record_key/classify_keys, which return facts.
    No normalization, filesystem path construction or storage mutation occurs.
    """
    if (
        not isinstance(device_id, str)
        or _DEVICE.fullmatch(device_id) is None
        or kind not in ("commit", "snapshot", "presence")
        or type(seq) is not int
        or not (1 if kind == "commit" else 0) <= seq <= MAX_SEQ
        or type(nparts) is not int
        or not 1 <= nparts <= MAX_PARTS
        or type(part) is not int
        or not 1 <= part <= nparts
        or (kind == "presence" and (seq, part, nparts) != (0, 1, 1))
    ):
        raise ValueError(
            "invalid_parts: invalid canonical record identity; inspect the caller components. "
            "See docs/invariants/store.md#errors."
        )
    if kind == "presence":
        return f"dev/{device_id}/presence.enc"
    name = f"{seq:012d}" + (f".{part}of{nparts}" if nparts > 1 else "") + ".enc"
    return f"dev/{device_id}/{'log' if kind == 'commit' else 'snap'}/{name}"


def parse_record_key(key: str) -> RecordName | None:
    """Parse exact record grammar, or return None. Never repairs or normalizes a name."""
    if not isinstance(key, str):
        return None
    presence = _PRESENCE.fullmatch(key)
    if presence:
        return RecordName(presence["device"], "presence", 0)
    match = _LOG_SNAP.fullmatch(key)
    if match is None:
        return None
    seq = int(match["seq"])
    kind = "commit" if match["directory"] == "log" else "snapshot"
    part = int(match["part"]) if match["part"] is not None else 1
    nparts = int(match["nparts"]) if match["nparts"] is not None else 1
    if (
        (kind == "commit" and seq == 0)
        or (match["part"] is not None and not 2 <= nparts <= MAX_PARTS)
        or part > nparts
    ):
        return None
    return RecordName(match["device"], kind, seq, part, nparts)


def _fork(key: str) -> bool:
    match = _CONFLICT.fullmatch(key)
    if match is None:
        return False
    try:
        datetime.strptime(match["date"] + match["time"], "%Y%m%d%H%M%S")
    except ValueError:
        return False
    original = match["stem"] + match["extension"]
    if original == FLEET_KEY:
        return True
    record = parse_record_key(original)
    return record is not None and record.kind == "commit"


def classify_keys(keys: Iterable[str]) -> KeyClassification:
    """Classify supplied file keys; foreign includes fork signals. No filesystem scan."""
    records = []
    controls = []
    foreign = []
    forks = []
    for key in keys:
        record = parse_record_key(key)
        if record is not None:
            records.append((key, record))
        elif key in (FLEET_KEY, STIGNORE_KEY):
            controls.append(key)
        else:
            foreign.append(key)
            if isinstance(key, str) and _fork(key):
                forks.append(key)
    return KeyClassification(tuple(records), tuple(controls), tuple(foreign), tuple(forks))
