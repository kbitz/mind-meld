"""Local attended-capture evidence. Never synced; inspection only reads.

The caller holds the mm lock across capture, acceptance and this atomic write.
A rename can succeed before directory fsync fails, so write errors make no
promise about whether the previous or the new record is visible.
"""

from __future__ import annotations

import errno
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

from mind_meld import config, events, fsutil, host_usage, lockedjson, safety, sidecar

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
    # None remains readable for records written before stop causes existed.
    "push-failed": {None, "interrupted", "storage-error", "snapshot-refused", "error"},
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
    errno_name: str | None = None

    def finish(
        self, kind: str, cause: str | None = None, *, errno_name: str | None = None
    ) -> CaptureOutcome:
        self.kind, self.cause = kind, cause
        self.errno_name = errno_name
        return self


@dataclass
class Attempt:
    outcome: CaptureOutcome | None = None
    content_accepted: bool = False

    def stopped(self, cause: str = "error", *, errno_name: str | None = None) -> None:
        if self.outcome is None or self.outcome.kind in PUBLICATION_CLASSES:
            return
        if not self.content_accepted:
            self.outcome.finish("push-failed", cause, errno_name=errno_name)
        elif self.outcome.appended is not None:
            # An interrupt after acceptance but before the verdict cannot
            # retroactively make either capture or content publication fail.
            self.outcome.finish("unverified", "evidence-error")


def storage_errno(exc: BaseException) -> str | None:
    """Extract an errno name through backend wrappers, never exception text."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError):
            return errno.errorcode.get(exc.errno)
        exc = exc.__cause__
    return None


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
        "errno": outcome.errno_name,
    }
    fsutil.refuse_real_state_write_under_pytest(record_path())
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
    errno_name = record.get("errno")
    if errno_name is not None and (
        kind != "push-failed"
        or cause != "storage-error"
        or not isinstance(errno_name, str)
        or errno_name not in errno.errorcode.values()
    ):
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
        "latest_attempt_errno": None,
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
        latest_attempt_errno=record.get("errno"),
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
    detail = state.get("latest_attempt_cause")
    if detail and state.get("latest_attempt_errno"):
        detail += f": {state['latest_attempt_errno']}"
    cause = f" ({detail})" if detail else ""
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


# Sync execution evidence is deliberately separate from capture/publication.
RUN_CAUSES = frozenset(
    {
        "ok",
        "no-changes",
        "setup-error",
        "authentication-error",
        "lock-held",
        "snapshot-refused",
        "storage-error",
        "crypto-error",
        "command-refused",
        "interrupted",
        "unexpected-error",
        "result-degraded",
    }
)
RUN_OUTCOMES = frozenset({"success", "degraded", "failed", "skipped"})
RUN_COUNTS = frozenset(
    {
        "bytes_transferred",
        "total_new",
        "total_modified",
        "total_deleted",
        "content_files",
        "held",
        "total_written",
        "total_merged",
        "total_skipped",
        "total_conflicted",
        "total_failed",
        "total_skipped_unknown_source",
        "durability_fsync_failures",
        "corrupt_peer_count",
        "maintenance_failures",
        "events_degradations",
    }
)
_MAX_COUNT = 2**53
_MAX_DETAILS = 8
_MAX_DETAIL_CHARS = 384


def runs_path() -> Path:
    return sidecar.SIDECAR_DIR / "last-attended-runs.json"


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_COUNT


def _scope_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and bool(re.fullmatch(r"[\w. -]+", value, flags=re.ASCII))
    )


def _valid_run(record: object) -> bool:
    if not isinstance(record, dict) or set(record) != {
        "completed_at",
        "outcome",
        "cause",
        "counts",
        "content_accepted",
        "ordering_unknown",
        "scope",
        "degradations",
        "omitted_details",
        "errno",
    }:
        return False
    counts, scope, details = record["counts"], record["scope"], record["degradations"]
    return (
        type(record["ordering_unknown"]) is bool
        and _timestamp(record["completed_at"]) is not None
        and isinstance(record["outcome"], str)
        and record["outcome"] in RUN_OUTCOMES
        and isinstance(record["cause"], str)
        and record["cause"] in RUN_CAUSES
        and (record["content_accepted"] is None or type(record["content_accepted"]) is bool)
        and isinstance(counts, dict)
        and set(counts) <= RUN_COUNTS
        and all(_count(n) for n in counts.values())
        and isinstance(scope, dict)
        and set(scope) <= {"source", "from_device"}
        and all(_scope_value(v) for v in scope.values())
        and isinstance(details, list)
        and len(details) <= _MAX_DETAILS
        and all(
            isinstance(s, str)
            and len(s) <= _MAX_DETAIL_CHARS
            and s == safety.safe_terminal_str(s)
            and "/" not in s
            and "~" not in s
            for s in details
        )
        and _count(record["omitted_details"])
        and (
            record["errno"] is None
            or isinstance(record["errno"], str)
            and record["errno"] in errno.errorcode.values()
        )
    )


def _valid_runs(data: object) -> bool:
    return (
        isinstance(data, dict)
        and type(data.get("version")) is int
        and data["version"] == 1
        and set(data) <= {"version", "push", "pull"}
        and all(_valid_run(v) for k, v in data.items() if k != "version")
    )


def run_details(details: list[str]) -> tuple[list[str], int]:
    """Retain only known path-free producer phrases, with honest omissions.

    Families: append ceiling, cursor/discovery, git/session budgets, token
    cache, host-reader skips, exception class, and missing custom mm-events.
    No exception message or unknown future producer is persisted.
    """
    if not details:
        return [], 0
    from mind_meld import events_tail

    literals = {
        events_tail._CURSOR_HOLD_DEGRADATION,
        events_tail._CURSOR_COVERAGE_DEGRADATION,
        events_tail._ROOT_DISCOVERY_DEGRADATION,
        events_tail._ROOT_DISCOVERY_ERROR_DEGRADATION,
        events_tail._ROOT_DISCOVERY_EMPTY_DEGRADATION,
        "event append skipped (max-file-size)",
        "token cache was locked, so tokens and skills are missing",
        "token walk skipped, so tokens and skills are missing",
        "usage capture published (incomplete reader coverage)",
    } | {
        f"usage capture {kind} ({cause or 'none'})"
        for kind, causes in CAUSES.items()
        for cause in causes
    }
    host_phrases = {
        events_tail._host_skip_phrase(reader, reason, readiness="ready", attended=attended)
        for reader in (*events.HOST_USAGE_TOKEN_SOURCES, None)
        for reason in (*get_args(host_usage.Reason), "unavailable", None)
        for attended in (False, True)
    }
    retained, omitted = [], 0
    for detail in details:
        if not isinstance(detail, str):
            omitted += 1
            continue
        # This producer embeds a configured path. Keep the label and remedy.
        if detail.startswith("mm-events folder ") and detail.endswith(
            " is missing; plug it in, create it, or run mm disable-source mm-events. "
            "Skipping mm-events for this sync."
        ):
            detail = (
                "mm-events folder is missing; restore it or run mm disable-source mm-events. "
                "Skipping mm-events for this sync."
            )
        known = (
            detail in literals
            or detail in host_phrases
            or bool(
                re.fullmatch(
                    r"events walk exceeded its [0-9]+ms budget|"
                    r"events tail failed \([A-Za-z][A-Za-z0-9_]*\)|"
                    r"git walk dropped [0-9]+ repositories this push\. Run mm recapture --dry-run "
                    r"to check current repository failures, then mm recapture 30d "
                    r"to recover the omitted commits",
                    detail,
                )
            )
            or detail.startswith("mm-events folder is missing;")
        )
        detail = detail.replace(events_tail.HOST_USAGE_CAPTURE_URL, "mm diag")
        detail = detail.replace("git/session", "git and session")
        safe = safety.safe_terminal_str(detail)
        if (
            not known
            or safe != detail
            or "/" in safe
            or "~" in safe
            or len(safe) > _MAX_DETAIL_CHARS
            or len(retained) >= _MAX_DETAILS
        ):
            omitted += 1
        else:
            retained.append(safe)
    return retained, min(omitted, _MAX_COUNT)


def make_run(
    *,
    outcome: str,
    cause: str,
    counts: dict | None = None,
    content_accepted: bool | None = None,
    scope: dict | None = None,
    degradations: list[str] | None = None,
    errno_name: str | None = None,
    completed_at: str | None = None,
) -> dict:
    details, omitted = run_details(degradations or [])
    raw_scope = scope or {}
    record = {
        "completed_at": completed_at or datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "cause": cause,
        "counts": counts or {},
        "content_accepted": content_accepted,
        "ordering_unknown": False,
        "scope": {k: v if _scope_value(v) else "unknown" for k, v in raw_scope.items()},
        "degradations": details,
        "omitted_details": omitted,
        "errno": errno_name,
    }
    if not _valid_run(record):
        raise ValueError("invalid sync run")
    return record


def write_run(verb: str, record: dict) -> None:
    """Nonblocking recoverable RMW. Completion time is captured by the caller.

    An older delayed writer never replaces a newer completion. A tie or
    rollback is one ordering-unknown state, with no additional history.
    Errors propagate to the callback's best-effort notice, never sync logic.
    """
    if verb not in ("push", "pull") or not _valid_run(record):
        raise ValueError("invalid sync run")
    with lockedjson.locked_json_rmw(
        runs_path(), retry_intervals=(), on_contention="raise", mode=0o600, compact=True
    ) as locked:
        if not locked.is_locked:
            raise OSError("run evidence lock unavailable")
        existing = locked.data if _valid_runs(locked.data) else {"version": 1}
        prior = existing.get(verb)
        if prior and _timestamp(prior["completed_at"]) >= _timestamp(record["completed_at"]):
            if prior != record:
                prior["ordering_unknown"] = True
                locked.data = existing
            else:
                locked.write_on_exit = False
        else:
            existing[verb] = record
            locked.data = existing
        if locked.write_on_exit:
            while len(json.dumps(existing, sort_keys=True).encode()) > _MAX_RECORD_BYTES:
                largest = max(
                    (entry for key, entry in existing.items() if key != "version"),
                    key=lambda entry: len(json.dumps(entry["degradations"])),
                )
                if not largest["degradations"]:
                    raise ValueError("oversized sync evidence")
                largest["degradations"].pop()
                largest["omitted_details"] = min(_MAX_COUNT, largest["omitted_details"] + 1)
    if locked.write_error is not None:
        raise locked.write_error


def read_runs() -> dict:
    """Write-free reads: missing history is normal; damaged history is unknown.

    The stat bound rejects ordinary oversized files before shared JSON parsing.
    It is not a race-proof allocation bound (the shared ceiling is 64 MiB).
    """
    return read_run_file(runs_path(), attended=True)


def read_run_file(path: Path, *, attended: bool = False) -> dict:
    try:
        if attended and path.stat().st_size > _MAX_RECORD_BYTES:
            return {"state": "oversized", "runs": {}}
        with lockedjson.locked_json_snapshot(path, blocking=False) as snapshot:
            state = {
                "lock_failed": "contended",
                "empty": "corrupt",
                "malformed": "corrupt",
                "non_dict": "corrupt",
            }.get(snapshot.state, snapshot.state)
            if state != "valid":
                return {"state": state, "runs": {}}
            data = snapshot.data
            if attended and not _valid_runs(data):
                return {"state": "corrupt", "runs": {}}
            return {
                "state": "valid",
                "runs": {k: v for k, v in data.items() if k in ("push", "pull")}
                if attended
                else data,
            }
    except FileNotFoundError:
        return {"state": "missing", "runs": {}}
    except OSError:
        return {"state": "unreadable", "runs": {}}
    except (ValueError, RecursionError):
        return {"state": "corrupt", "runs": {}}


def _run_needs_action(run: dict) -> bool:
    if run["outcome"] == "failed":
        return True
    if run["outcome"] != "degraded":
        return False
    from mind_meld import events_tail

    # Finding no repositories is a fact, not lost work. Keep it in detail.
    return not (
        run.get("degradations") == [events_tail._ROOT_DISCOVERY_EMPTY_DEGRADATION]
        and not run.get("omitted_details")
        and not any(
            run.get("counts", {}).get(k)
            for k in (
                "held",
                "total_failed",
                "total_skipped_unknown_source",
                "durability_fsync_failures",
                "corrupt_peer_count",
                "maintenance_failures",
            )
        )
    )


def project_runs(auto: dict, attended: dict, *, now: datetime | None = None) -> dict:
    """Select one run per verb, retaining unknown ordering and coverage facts.

    Strict later success resolves an available earlier failure symmetrically.
    A skip proves neither failure nor recovery; no unavailable history is inferred.
    """
    from mind_meld import events_tail

    now = now or datetime.now(timezone.utc)
    projection = {}
    for verb in ("push", "pull"):
        candidates = []
        unknown = next(
            (s["state"] for s in (auto, attended) if s["state"] not in ("valid", "missing")), None
        )
        for kind, evidence in (("auto", auto), ("attended", attended)):
            raw = evidence["runs"].get(verb)
            if raw is None:
                continue
            if kind == "attended":
                run = dict(raw)
                if run.get("ordering_unknown"):
                    unknown = "ambiguous"
            else:
                outcome = raw.get("outcome") if isinstance(raw, dict) else None
                if not isinstance(outcome, str) or outcome not in {
                    "success",
                    "degraded",
                    "lock-held",
                    "config-missing",
                    "config-error",
                    "keyring-no-collection",
                    "keyring-locked",
                    "no-passphrase",
                    "keyring-error",
                    "backend-error",
                    "crypto-error",
                    "failed",
                    "fleet-refused",
                    "refused",
                    "no-sources",
                }:
                    unknown = "corrupt"
                    outcome = "command-refused"
                good = outcome == "success"
                skip = outcome == "lock-held"
                run = {
                    "completed_at": raw.get("timestamp") if isinstance(raw, dict) else None,
                    "outcome": "success"
                    if good
                    else "skipped"
                    if skip
                    else "degraded"
                    if outcome == "degraded"
                    else "failed",
                    "cause": "ok"
                    if good
                    else "lock-held"
                    if skip
                    else "result-degraded"
                    if outcome == "degraded"
                    else outcome
                    if outcome in RUN_CAUSES
                    else "command-refused",
                    "scope": {},
                    "counts": {},
                    "content_accepted": None,
                    "degradations": [events_tail._ROOT_DISCOVERY_EMPTY_DEGRADATION]
                    if isinstance(raw, dict)
                    and raw.get("detail") == events_tail._ROOT_DISCOVERY_EMPTY_DEGRADATION
                    else [],
                    "omitted_details": 0,
                    "errno": None,
                }
            timestamp = _timestamp(run["completed_at"])
            if timestamp is None or timestamp > now:
                unknown = "ambiguous"
            candidates.append((timestamp, kind, run))
        if len(candidates) == 2 and candidates[0][0] == candidates[1][0]:
            unknown = "ambiguous"
        ordered = sorted(
            candidates, key=lambda c: c[0] or datetime.min.replace(tzinfo=timezone.utc)
        )
        selected = ordered[-1] if ordered else None
        unresolved = any(_run_needs_action(c[2]) for c in candidates)
        if selected and not unknown and selected[2]["outcome"] == "success":
            # A scope covers an earlier execution only when its constraints
            # are a subset of that execution's constraints; empty covers all.
            scope = selected[2]["scope"]
            unresolved = any(
                _run_needs_action(candidate[2])
                and not all(candidate[2]["scope"].get(k) == v for k, v in scope.items())
                for candidate in ordered[:-1]
            )
        if selected and selected[2]["scope"] and unresolved:
            unknown = unknown or "limited-coverage"
        projection[verb] = {
            "kind": selected[1] if selected else None,
            "selected": selected[2] if selected else None,
            "superseded": [c[1] for c in ordered[:-1]] if not unknown else [],
            "unknown_reason": unknown,
            "unresolved": unresolved,
            "limited_coverage": bool(selected and selected[2]["scope"]),
        }
    return projection
