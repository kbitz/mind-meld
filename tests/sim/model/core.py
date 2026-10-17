"""Pure store-v2 tables. Candidates for promotion; the I/O shell is not.

Promotion must sanitize peer display names through safety. No device, clock,
filesystem, keyring or CLI access lives here.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from mind_meld import merge
from mind_meld.store.format import Entry


def vector_order(left: dict[str, int], right: dict[str, int]) -> str:
    keys = sorted(left.keys() | right.keys())
    less = any(left.get(k, 0) < right.get(k, 0) for k in keys)
    more = any(left.get(k, 0) > right.get(k, 0) for k in keys)
    return {
        (False, False): "equal",
        (True, False): "before",
        (False, True): "after",
        (True, True): "concurrent",
    }[less, more]


def vector_max(*vectors: dict[str, int]) -> dict[str, int]:
    return {k: max(v.get(k, 0) for v in vectors) for k in sorted({k for v in vectors for k in v})}


def increment(vector: dict[str, int], device: str) -> dict[str, int]:
    result = vector_max(vector, {device: vector.get(device, 0) + 1})
    return result


def entry(source: str, path: str, data: bytes | None, vv: dict[str, int], mtime=0) -> Entry:
    return Entry(
        {
            "source": source,
            "path": path,
            "vv": vv,
            "sha": None if data is None else hashlib.sha256(data).hexdigest(),
            "size": 0 if data is None else len(data),
            "mtime": None if data is None else mtime,
            "deleted": data is None,
        },
        data,
    )


def local_version(
    ledger: Entry | None, data: bytes | None, mtime: float, device: str, *, pending=False
) -> Entry | None:
    """§4 unpublished edit is ledger.vv + me; E12 pending is not yet authored."""
    if ledger is None:
        return None if data is None else entry("claude", "unused", data, {device: 1}, mtime)
    if (
        ledger.metadata["sha"] == (None if data is None else hashlib.sha256(data).hexdigest())
        and not pending
    ):
        return entry(
            ledger.metadata["source"],
            ledger.metadata["path"],
            data,
            ledger.metadata["vv"],
            ledger.metadata["mtime"],
        )
    return entry(
        ledger.metadata["source"],
        ledger.metadata["path"],
        data,
        increment(ledger.metadata["vv"], device),
        mtime,
    )


def author(
    source: str,
    path: str,
    ledger: Entry | None,
    data: bytes | None,
    mtime: float,
    device: str,
    *,
    pending=False,
) -> Entry | None:
    if (
        ledger is not None
        and not pending
        and ledger.metadata["sha"] == (None if data is None else hashlib.sha256(data).hexdigest())
    ):
        return None
    local = local_version(ledger, data, mtime, device, pending=pending)
    return (
        None
        if local is None
        else replace(local, metadata={**local.metadata, "source": source, "path": path})
    )


def classify(local: Entry | None, remote: Entry, *, bootstrap=False) -> str:
    """§4 table, including bootstrap inversion and E11 counter reuse."""
    if bootstrap and remote.metadata["deleted"]:
        return "bootstrap-delete"
    if local is None:
        return "fast-forward"
    if local.metadata["sha"] == remote.metadata["sha"]:
        return "same-sha"
    order = vector_order(local.metadata["vv"], remote.metadata["vv"])
    return {
        "before": "fast-forward",
        "after": "ignore",
        "equal": "resolve",
        "concurrent": "resolve",
    }[order]


def resolver_name(path: str) -> str:
    return (
        "jsonl"
        if path.endswith(".jsonl")
        else ("lines" if path.rsplit("/", 1)[-1] == "MEMORY.md" else "newest")
    )


def resolve(left: Entry, right: Entry) -> Entry:
    """Symmetric unordered-pair resolution; publishing owns the increment."""
    lm, rm = left.metadata, right.metadata
    vv = vector_max(lm["vv"], rm["vv"])
    live = [v for v in (left, right) if v.content is not None]
    if not live:
        return entry(lm["source"], lm["path"], None, vv)
    if len(live) == 1:
        winner = live[0]
        return replace(winner, metadata={**winner.metadata, "vv": vv})
    strategy = resolver_name(lm["path"])
    mtime = max(lm["mtime"], rm["mtime"])
    if strategy == "newest":
        winner = max((left, right), key=lambda v: (v.metadata["mtime"], v.metadata["sha"]))
        return replace(winner, metadata={**winner.metadata, "vv": vv})
    merger = {"jsonl": merge.merge_jsonl, "lines": merge.merge_lines}[strategy]
    return entry(lm["source"], lm["path"], merger(left.content, right.content), vv, mtime)


def reader_rule(
    next_seq: int, *, complete: bool, unreadable: bool, snapshot_seq: int | None
) -> str:
    if complete and not unreadable:
        return "commit"
    if snapshot_seq is not None and next_seq <= snapshot_seq:
        return "snapshot"
    return "stalled-unreadable" if unreadable else "pending"


def reconcile_rule(
    *,
    fleet_fork=False,
    own_fork=False,
    foreign_host=False,
    chain_fork=False,
    own_missing=False,
) -> str | None:
    """§9 refusal codes, strongest evidence first.

    ``fork``: a transport conflict copy of a commit or ``fleet.json``. ``fork-clone``:
    a record from another host under this device id. ``fork-restore``: this host's own
    chain diverged from its own log. ``missing-own-directory``: only once the folder is
    synced and ``dev/<me>/`` is still absent (the caller decides; before that it waits).
    """
    return next(
        (
            reason
            for flag, reason in (
                (fleet_fork or own_fork, "fork"),
                (foreign_host, "fork-clone"),
                (chain_fork, "fork-restore"),
                (own_missing, "missing-own-directory"),
            )
            if flag
        ),
        None,
    )


def snapshot_due(log_bytes: int, commit_count: int, snapshot_bytes: int) -> bool:
    """§6 attended trigger; never a time trigger."""
    return commit_count >= 200 or log_bytes > snapshot_bytes
