"""One-entry piece swaps. Every piece uses injected roots, keys, clock and state."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Operation(Protocol):
    def __call__(self, context, **kwargs): ...


@runtime_checkable
class StoreAdapter(Protocol):
    def push(self, **kwargs): ...
    def pull(self, **kwargs): ...
    def publish_snapshot(self, **kwargs): ...
    def compact(self, **kwargs): ...
    def join(self, **kwargs): ...
    def retire(self, **kwargs): ...


@dataclass(frozen=True)
class Piece:
    implementation: Operation
    kind: str
    owner: str
    completion_check: str


OWNERS = {
    "create_fleet": "80A",
    "publish": "75A",
    "presence": "75A",
    "snapshot": "76A",
    "compact": "76A",
    "reconcile": "76A",
    "read": "77A",
    "classify": "78A",
    "resolve": "78A",
    "join": "80A",
    "rejoin": "80A",
    "retire": "81A",
}
MODEL_PIECES = tuple(sorted(OWNERS))
REQUIRED_ARGUMENTS = {
    "classify": ("incoming", "peer", "seq"),
    "resolve": ("local", "incoming"),
    "retire": ("target",),
    "rejoin": ("new_id",),
}


def validate_registry(registry: dict[str, Piece]) -> None:
    if set(registry) != set(OWNERS):
        raise TypeError(f"registry piece set mismatch: {sorted(set(OWNERS) ^ set(registry))}")
    for name, piece in sorted(registry.items()):
        if (
            piece.kind not in ("model", "real")
            or not isinstance(piece.implementation, Operation)
            or not piece.owner
            or not piece.completion_check
        ):
            raise TypeError(f"mis-wired store piece {name}: expected Operation and provenance")
        try:
            signature = inspect.signature(piece.implementation)
            if not any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
            ):
                raise TypeError("operation must accept arbitrary keyword arguments")
            signature.bind(object(), **{key: object() for key in REQUIRED_ARGUMENTS.get(name, ())})
        except TypeError as error:
            raise TypeError(f"mis-wired store piece {name}: expected context, **kwargs") from error


def default_registry() -> dict[str, Piece]:
    from . import store

    result = {
        name: Piece(
            getattr(store, name),
            "model",
            owner,
            "./bin/check tests/sim (bounded, mutants, scenarios); deep handoff",
        )
        for name, owner in sorted(OWNERS.items())
    }
    validate_registry(result)
    return result


class Adapter:
    """Stateless: recreated per action; state is always read from the seam."""

    def __init__(self, context):
        self.context = context

    def push(self, **kwargs):
        return self.context.call("publish", **kwargs)

    def pull(self, **kwargs):
        return self.context.call("read", **kwargs)

    def publish_snapshot(self, **kwargs):
        return self.context.call("snapshot", **kwargs)

    def compact(self, **kwargs):
        return self.context.call("compact", **kwargs)

    def join(self, **kwargs):
        return self.context.call("join", **kwargs)

    def retire(self, **kwargs):
        return self.context.call("retire", **kwargs)
