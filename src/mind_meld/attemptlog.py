"""Local attended-capture evidence. Never synced; inspection only reads.

The caller holds the mm lock across capture, acceptance and this atomic write.
A rename can succeed before directory fsync fails, so write errors make no
promise about whether the previous or the new record is visible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

from mind_meld import config, events, fsutil, host_usage, sidecar

PUBLICATION_CLASSES = frozenset({"published", "not-published", "unverified"})
_MAX_RECORD_BYTES = 16_384
_MAX_TIMESTAMP_CHARS = 64
CAUSES = {
    "published": {None},
    "not-published": {"exclude-patterns", "include-dirs", "file-absent", "row-missing"},
    "unverified": {
        "missing",
        "unreadable",
        "oversized-line",
        "changed",
        "revision-mismatch",
        "evidence-error",
    },
    "no-row": {None},
    "capture-failed": {None},
    "append-failed": {None},
    "max-file-size": {None},
    "prerequisites": set(get_args(config.UsageCaptureReadiness)) - {"ready"},
    "push-failed": {None},
}
READER_OUTCOMES = frozenset({"contributed", "empty", "partial", "absent"}) | {
    f"dropped:{reason}"
    for reason in (*get_args(host_usage.Reason), "unavailable")
    if reason != "no_metadata_ledger"
}


@dataclass
class CaptureOutcome:
    attempted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    kind: str = "capture-failed"
    cause: str | None = None
    readers: dict[str, str] = field(default_factory=dict)
    appended: tuple[Path, dict] | None = None

    def finish(self, kind: str, cause: str | None = None) -> CaptureOutcome:
        self.kind, self.cause = kind, cause
        return self


@dataclass
class Attempt:
    outcome: CaptureOutcome | None = None
    content_accepted: bool = False

    def stopped(self) -> None:
        if self.outcome is None or self.outcome.kind in PUBLICATION_CLASSES:
            return
        if not self.content_accepted:
            self.outcome.finish("push-failed")
        elif self.outcome.appended is not None:
            # An interrupt after acceptance but before the verdict cannot
            # retroactively make either capture or content publication fail.
            self.outcome.finish("unverified", "evidence-error")


def record_path() -> Path:
    return sidecar.SIDECAR_DIR / "last-attended-capture.json"


def write(outcome: CaptureOutcome) -> None:
    """Called under the mm lock; errors propagate to push's guarded finally."""
    record = {
        "attempted_at": outcome.attempted_at,
        "class": outcome.kind,
        "cause": outcome.cause,
        "readers": outcome.readers,
        "row_ts": outcome.appended[1]["ts"] if outcome.appended is not None else None,
    }
    sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_bytes(
        record_path(), json.dumps(record, sort_keys=True).encode(), mode=0o600, fsync=True
    )


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or len(raw) > _MAX_TIMESTAMP_CHARS:
        return None
    try:
        value = datetime.fromisoformat(raw)
        return value.astimezone(timezone.utc) if value.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _valid(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    kind, readers = record.get("class"), record.get("readers")
    if "cause" not in record or not isinstance(kind, str) or kind not in CAUSES:
        return False
    cause = record["cause"]
    if cause is not None and not isinstance(cause, str):
        return False
    if cause not in CAUSES[kind] or _timestamp(record.get("attempted_at")) is None:
        return False
    if record.get("row_ts") is not None and _timestamp(record["row_ts"]) is None:
        return False
    return isinstance(readers, dict) and all(
        name in events.HOST_USAGE_TOKEN_SOURCES
        and isinstance(outcome, str)
        and outcome in READER_OUTCOMES
        for name, outcome in readers.items()
    )


def read() -> tuple[dict | None, str | None]:
    """Plain bounded read: atomic replacement needs no read lock or mutation."""
    try:
        with record_path().open("rb") as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if len(raw) > _MAX_RECORD_BYTES:
        return None, "corrupt"
    try:
        record = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "corrupt"
    return (record, None) if _valid(record) else (None, "corrupt")


def project(readers: list[str], recorded_ts: str | None) -> dict:
    record, reason = read()
    out = {
        "latest_attempt": "unknown",
        "latest_attempt_cause": None,
        "latest_attempt_at": None,
        "latest_attempt_readers": {},
        "latest_attempt_reason": reason,
        "latest_attempt_superseded": False,
    }
    if record is None:
        return out
    attempted = _timestamp(record["attempted_at"])
    newer = _timestamp(recorded_ts)
    row_ts = _timestamp(record.get("row_ts"))
    out.update(
        latest_attempt=record["class"],
        latest_attempt_cause=record.get("cause"),
        latest_attempt_at=attempted.isoformat(),
        latest_attempt_readers={
            name: record["readers"][name]
            for name in events.HOST_USAGE_TOKEN_SOURCES
            if name in readers and name in record["readers"]
        },
        latest_attempt_superseded=bool(
            record["class"] != "published"
            and newer
            and newer > attempted
            and (row_ts is None or newer > row_ts)
        ),
    )
    return out


def render(state: dict, *, age: str, path: str, refresh_ready: bool = True) -> list[str]:
    """Plain text; the console caller sanitizes every line for Rich."""
    kind = state.get("latest_attempt", "unknown")
    if kind == "unknown":
        reason = state.get("latest_attempt_reason") or "missing"
        refresh = " — run mm push" if refresh_ready else ""
        detail = {
            "missing": f"no attended attempt recorded yet{refresh}",
            "unreadable": f"record unreadable: {path}",
            "corrupt": "record corrupt — the next attended mm push rewrites it",
        }[reason]
        return [f"Last recorded attended attempt: unknown ({detail})"]
    cause = f" ({state['latest_attempt_cause']})" if state.get("latest_attempt_cause") else ""
    note = (
        "; a later capture has since been recorded"
        if state.get("latest_attempt_superseded")
        else ""
    )
    lines = [
        f"Last recorded attended attempt: {state['latest_attempt_at']} ({age}) "
        f"— {kind}{cause}{note}"
    ]
    outcomes = state.get("latest_attempt_readers", {})
    if any(value != "contributed" for value in outcomes.values()):
        lines.append(
            "  "
            + "; ".join(
                f"{name} {events.host_reader_label(outcomes[name])}"
                for name in events.HOST_USAGE_TOKEN_SOURCES
                if name in outcomes
            )
        )
    return lines
