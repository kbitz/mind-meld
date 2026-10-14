"""Narrow persisted seam. A single replacement supplies atomic fixture transactions.

75A must supply the SQLite backend; this is not a database schema promise.
"""

from __future__ import annotations

import base64
import copy
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ContextManager, Protocol, runtime_checkable

from mind_meld.store.format import Entry


def path_key(source: str, path: str) -> str:
    return json.dumps([source, path], separators=(",", ":"))


@dataclass
class State:
    meta: dict = field(default_factory=dict)
    ledger: dict[str, dict] = field(default_factory=dict)
    cursors: dict[str, dict] = field(default_factory=dict)
    own_log: dict[str, dict] = field(default_factory=dict)
    displaced: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)


def row(entry: Entry, *, pending=False) -> dict:
    return {**entry.metadata, "content": entry.content, "pending": pending}


def from_row(value: dict | None) -> Entry | None:
    if value is None:
        return None
    return Entry(
        {k: value.get(k, 0) for k in ("source", "path", "vv", "sha", "size", "mtime", "deleted")},
        value.get("content"),
    )


@runtime_checkable
class StateSeam(Protocol):
    def read(self) -> State: ...
    def transaction(self) -> ContextManager[State]: ...


def _encode(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: _encode(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            return base64.b64decode(value["$bytes"])
        return {k: _decode(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


class FileState:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "model-state.json"

    def read(self) -> State:
        try:
            return State(**_decode(json.loads(self.path.read_bytes())))
        except FileNotFoundError:
            return State()

    @contextmanager
    def transaction(self) -> Iterator[State]:
        state = copy.deepcopy(self.read())
        yield state
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / ".model-state.tmp"
        temp.write_text(json.dumps(_encode(vars(state)), sort_keys=True, separators=(",", ":")))
        os.replace(temp, self.path)
