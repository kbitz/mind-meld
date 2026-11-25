"""Per-phase push timing model and local persistence.

Leaf module: imports only the standard library plus leaf modules (`config`,
`lockedjson`, `safety`). Never imports `cli`, `core`, `events_tail`, or any
`*flow` module.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence

from mind_meld import config, lockedjson, safety

PUSH_TIMINGS_PATH = config.CONFIG_DIR / "push-timings.json"
PUSH_KINDS = ("attended", "autopush", "catchup")
VALID_PHASE_STATUSES = frozenset({"ok", "deadline", "skipped", "error"})


@dataclass(frozen=True)
class PhaseTiming:
    """Single push phase timing sample."""

    phase: str
    label: str
    elapsed_ms: int
    budget_ms: int | None = None
    completed: int | None = None
    total: int | None = None
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        status = self.status if self.status in VALID_PHASE_STATUSES else "ok"
        out: dict[str, Any] = {
            "phase": self.phase,
            "label": self.label,
            "elapsed_ms": max(0, int(self.elapsed_ms)),
            "budget_ms": int(self.budget_ms) if self.budget_ms is not None else None,
            "status": status,
        }
        if self.completed is not None:
            out["completed"] = int(self.completed)
        if self.total is not None:
            out["total"] = int(self.total)
        return out


class PhaseTimer:
    """Thread-safe monotonic collector for per-phase push timings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._entries: list[PhaseTiming] = []
        self._index_by_phase: dict[str, int] = {}

    def record(
        self,
        phase: str,
        label: str,
        elapsed_ms: int,
        *,
        budget_ms: int | None = None,
        completed: int | None = None,
        total: int | None = None,
        status: str = "ok",
        accumulate: bool = False,
        accumulate_budget: bool = False,
    ) -> PhaseTiming:
        clean_status = status if status in VALID_PHASE_STATUSES else "ok"
        clean_ms = max(0, int(elapsed_ms))
        with self._lock:
            existing_idx = self._index_by_phase.get(phase)
            if existing_idx is not None:
                prev = self._entries[existing_idx]
                merged_ms = prev.elapsed_ms + clean_ms if accumulate else clean_ms
                if (
                    accumulate
                    and accumulate_budget
                    and budget_ms is not None
                    and prev.budget_ms is not None
                ):
                    merged_budget: int | None = prev.budget_ms + budget_ms
                else:
                    merged_budget = budget_ms if budget_ms is not None else prev.budget_ms
                merged_completed = completed if completed is not None else prev.completed
                merged_total = total if total is not None else prev.total
                entry = PhaseTiming(
                    phase=phase,
                    label=label,
                    elapsed_ms=merged_ms,
                    budget_ms=merged_budget,
                    completed=merged_completed,
                    total=merged_total,
                    status=clean_status,
                )
                self._entries[existing_idx] = entry
                return entry
            entry = PhaseTiming(
                phase=phase,
                label=label,
                elapsed_ms=clean_ms,
                budget_ms=budget_ms,
                completed=completed,
                total=total,
                status=clean_status,
            )
            self._index_by_phase[phase] = len(self._entries)
            self._entries.append(entry)
            return entry

    @contextmanager
    def measure(
        self,
        phase: str,
        label: str,
        *,
        budget_ms: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Time a block and record it on exit; caller may mutate yielded metadata."""
        meta: dict[str, Any] = {
            "status": "ok",
            "budget_ms": budget_ms,
            "completed": None,
            "total": None,
        }
        started = time.monotonic()
        try:
            yield meta
        except BaseException:
            meta["status"] = "error"
            raise
        finally:
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            self.record(
                phase,
                label,
                elapsed_ms,
                budget_ms=meta.get("budget_ms"),
                completed=meta.get("completed"),
                total=meta.get("total"),
                status=str(meta.get("status", "ok")),
            )

    def phases(self) -> tuple[PhaseTiming, ...]:
        with self._lock:
            return tuple(self._entries)

    def total_ms(self) -> int:
        return max(0, round((time.monotonic() - self._started_at) * 1000))


def format_verbose_timings(phases: Sequence[PhaseTiming | Mapping[str, Any]]) -> list[str]:
    """Render one verbose timing line per phase."""
    lines: list[str] = []
    for item in phases:
        if isinstance(item, PhaseTiming):
            label = item.label
            elapsed_ms = item.elapsed_ms
            budget_ms = item.budget_ms
            status = item.status
        else:
            label = str(item.get("label") or item.get("phase") or "unknown")
            elapsed_ms = int(item.get("elapsed_ms", 0))
            budget_raw = item.get("budget_ms")
            budget_ms = (
                int(budget_raw)
                if isinstance(budget_raw, int) and not isinstance(budget_raw, bool)
                else None
            )
            status = str(item.get("status") or "ok")
        safe_label = safety.safe_str(label)
        if budget_ms is not None:
            line = f"  {safe_label}: {elapsed_ms} ms / {budget_ms} ms"
        else:
            line = f"  {safe_label}: {elapsed_ms} ms"
        if status and status != "ok":
            line += f" [{safety.safe_str(status)}]"
        lines.append(line)
    return lines


def _normalize_phase_dict(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    phase = raw.get("phase")
    label = raw.get("label")
    elapsed_ms = raw.get("elapsed_ms")
    if not isinstance(phase, str) or not phase:
        return None
    if not isinstance(label, str) or not label:
        label = phase
    if type(elapsed_ms) is not int or elapsed_ms < 0:
        return None
    budget_ms = raw.get("budget_ms")
    if budget_ms is not None and (type(budget_ms) is not int or budget_ms < 0):
        budget_ms = None
    status = raw.get("status")
    if not isinstance(status, str) or status not in VALID_PHASE_STATUSES:
        status = "ok"
    out: dict[str, Any] = {
        "phase": safety.safe_str(phase),
        "label": safety.safe_str(label),
        "elapsed_ms": elapsed_ms,
        "budget_ms": budget_ms,
        "status": status,
    }
    completed = raw.get("completed")
    total = raw.get("total")
    if type(completed) is int and completed >= 0:
        out["completed"] = completed
    if type(total) is int and total >= 0:
        out["total"] = total
    return out


def _normalize_timing_run(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in PUSH_KINDS:
        return None
    recorded_at = raw.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at:
        recorded_at = None
    total_ms = raw.get("total_ms")
    if type(total_ms) is not int or total_ms < 0:
        total_ms = None
    raw_phases = raw.get("phases")
    phases: list[dict[str, Any]] = []
    if isinstance(raw_phases, list):
        for item in raw_phases:
            norm = _normalize_phase_dict(item)
            if norm is not None:
                phases.append(norm)
    return {
        "kind": kind,
        "recorded_at": safety.safe_str(recorded_at) if recorded_at else None,
        "total_ms": total_ms,
        "phases": phases,
    }


def record_push_timings(
    kind: str,
    phases: Sequence[PhaseTiming],
    *,
    total_ms: int | None = None,
    now: datetime | None = None,
) -> None:
    """Persist per-phase timings for ``kind`` (attended, autopush, or catchup). Best-effort."""
    if kind not in PUSH_KINDS:
        return
    current = now or datetime.now(timezone.utc)
    run_entry: dict[str, Any] = {
        "kind": kind,
        "recorded_at": current.isoformat(),
        "total_ms": max(0, int(total_ms))
        if total_ms is not None
        else sum(p.elapsed_ms for p in phases),
        "phases": [p.to_dict() for p in phases],
    }
    try:
        with lockedjson.locked_json_rmw(
            PUSH_TIMINGS_PATH,
            mode=0o600,
            compact=True,
            retry_intervals=(),
            on_contention="raise",
        ) as ljson:
            existing = (
                ljson.data
                if ljson.read_state == "valid" and isinstance(ljson.data, Mapping)
                else {}
            )
            raw_by_kind = existing.get("by_kind")
            by_kind: dict[str, Any] = {}
            if isinstance(raw_by_kind, Mapping):
                for k in PUSH_KINDS:
                    norm = _normalize_timing_run(raw_by_kind.get(k))
                    if norm is not None:
                        by_kind[k] = norm
            by_kind[kind] = run_entry
            ljson.data = {
                "version": 1,
                "latest": run_entry,
                "by_kind": by_kind,
            }
    except Exception:
        pass


def read_push_timings() -> dict[str, Any]:
    """Read-only snapshot of recorded push timings. Never creates or writes files."""
    empty: dict[str, Any] = {"latest": None, "by_kind": {}}
    try:
        with lockedjson.locked_json_snapshot(PUSH_TIMINGS_PATH, blocking=False) as snap:
            if snap.state != "valid" or not isinstance(snap.data, Mapping):
                return empty
            data = snap.data
            if type(data.get("version")) is not int or data.get("version") != 1:
                return empty
            latest = _normalize_timing_run(data.get("latest"))
            by_kind: dict[str, Any] = {}
            raw_by_kind = data.get("by_kind")
            if isinstance(raw_by_kind, Mapping):
                for k in PUSH_KINDS:
                    norm = _normalize_timing_run(raw_by_kind.get(k))
                    if norm is not None:
                        by_kind[k] = norm
            return {"latest": latest, "by_kind": by_kind}
    except Exception:
        return empty


def diag_timings_state() -> dict[str, Any]:
    """Non-secret timing summary for ``mm diag``."""
    return read_push_timings()
