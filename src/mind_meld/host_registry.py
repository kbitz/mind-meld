"""Host usage reader registry and dispatch (Track 84A).

Owns ``HOST_READER_DIAGS``, ``reader_usage_diag``, ``reader_cache_cold``, and
``warm_host_cache_inline`` so ``host_usage.py`` stays below ``hosts/*``
without importing any adapter module. Resolves reader and diag callables on
their owner modules at call time so test monkeypatches take effect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mind_meld import host_usage
from mind_meld.hosts import codex as codex_host


@dataclass(frozen=True)
class HostReaderDiag:
    function: str
    ready_key: str
    ready_value: Any
    label: str
    owner: Any = host_usage


HOST_READER_DIAGS = {
    "codex": HostReaderDiag("codex_usage_diag", "state", "ready", "codex", owner=codex_host),
    "cursor": HostReaderDiag(
        "cursor_usage_diag",
        "complete_once",
        True,
        "Cursor (Conductor + enrolled CLI)",
        owner=host_usage,
    ),
    "muse": HostReaderDiag("muse_usage_diag", "complete_once", True, "muse", owner=host_usage),
    "agy": HostReaderDiag("agy_usage_diag", "complete_once", True, "Antigravity", owner=host_usage),
}


def reader_usage_diag(reader: str, *, count_logs: bool = False) -> dict[str, Any]:
    # Resolve at call time so patching the owner still reaches every consumer.
    # Muse and Antigravity on-disk counts walk trees, so callers pass count_logs=True
    # when holding consent; status and recapture use their cache-only view.
    descriptor = HOST_READER_DIAGS[reader]
    fn = getattr(descriptor.owner, descriptor.function)
    if reader in {"muse", "agy"}:
        return fn(count_logs=count_logs)
    return fn()


def reader_cache_cold(reader: str, state: dict[str, Any]) -> bool:
    descriptor = HOST_READER_DIAGS[reader]
    return state.get(descriptor.ready_key) != descriptor.ready_value


def warm_host_cache_inline(
    root: Path | None = None,
    *,
    budget_s: float = host_usage.DEFAULT_READ_BUDGET_S,
    reader: str = "codex",
) -> host_usage.HostUsageResult:
    """Read host usage under the attended caller's generous one-off budget.

    The result is the retry: attended capture publishes it through the usual
    reader failure boundary, without a second short-budget read. Autopush
    never calls this helper. Codex/Grok/Muse can resume partial progress across
    pushes; Cursor reparses rewritten files and need not converge under the
    same short allowance.
    ``reader`` selects a name in ``events_tail.WARMABLE_HOST_READERS``.

    2026-09-17, device 3a6c7dc9, Python 3.14.7: an empty-cache parse of
    1,066 rollouts took 2,949.68 ms before serialization; warm reads took
    166.66–168.26 ms. The 5 s allowance is cooperative, not an end-to-end
    ceiling. Full interpreter and phase measurements: events-retro.md.
    """
    deadline = time.monotonic() + budget_s
    if reader == "codex":
        return codex_host.read_codex_usage(root, deadline=deadline)
    if reader == "cursor":
        return host_usage.read_cursor_usage(root, deadline=deadline, consented=True)
    if reader == "muse":
        return host_usage.read_muse_usage(root, deadline=deadline, consented=True)
    if reader == "agy":
        return host_usage.read_agy_usage(root, deadline=deadline, consented=True)
    raise ValueError(
        f"unknown host reader {reader!r}; register it in host_registry.py "
        "(docs/invariants/events-retro.md#host-reader-adapter-contract)"
    )
