"""Encrypted-record Syncthing mesh pass for mm-managed machines (Track 152A).

Imported only by ``cli``; never imports ``cli``, ``devicesflow`` or ``initflow``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mind_meld import attemptlog, core, devices, safety, syncthing
from mind_meld import config as _config_module
from mind_meld.errors import ConfigError, StorageError, os_error_cause
from mind_meld.storage.local import LocalBackend

ATTENDED_REQUEST_TIMEOUT_S = 2.0
ATTENDED_PASS_BUDGET_S = 10.0
HOOK_REQUEST_TIMEOUT_S = 0.5
HOOK_PASS_BUDGET_S = 2.0
BREADCRUMB_BUDGET_CHARS = 300
_MAX_PEER_NAME_CHARS = 128


_PATH_CHAR_TRANS = str.maketrans({"/": "_", "~": "_"})


def _safe_detail_token(raw: object) -> str:
    """Sanitize a diagnostic cause for single-line stderr and path-free run details."""
    text = safety.safe_terminal_str(str(raw)).translate(_PATH_CHAR_TRANS).strip()
    return text or "unknown"


def _safe_peer_name(raw_name: object, fallback_id: str) -> str:
    """Sanitize a registry device name for path-free outcome and degradation lines."""
    text = safety.safe_terminal_str(str(raw_name or "")).translate(_PATH_CHAR_TRANS).strip()
    return (text or _safe_detail_token(fallback_id))[:_MAX_PEER_NAME_CHARS]


def format_share_summary(
    shared: Sequence[tuple[str, str]],
    *,
    budget: int | None = None,
) -> str:
    """Format ``Syncthing now shares the fleet folder with N fleet machine(s): ...``."""
    if not shared:
        return ""
    noun = "fleet machine" if len(shared) == 1 else "fleet machines"
    lead = f"Syncthing now shares the fleet folder with {len(shared)} {noun}"
    entries = [f"{name} ({st_id[:7]})" for name, st_id in shared]
    full = f"{lead}: {', '.join(entries)}"
    if budget is None or len(full) <= budget:
        return full
    for keep in range(len(entries), 0, -1):
        shown = ", ".join(entries[:keep])
        more = len(entries) - keep
        candidate = f"{lead}: {shown}" + (f", +{more} more" if more else "")
        if len(candidate) <= budget:
            return candidate
    short = f"{lead} (+{len(entries)} more)"
    if len(short) <= budget:
        return short
    counts = f"{len(entries)} Syncthing share" + ("s" if len(entries) != 1 else "")
    return counts if len(counts) <= budget else ""


def compose_hook_breadcrumb(
    existing_degradations: Sequence[str],
    mesh_result: MeshResult,
    *,
    extra_success: Sequence[str] = (),
    budget: int = BREADCRUMB_BUDGET_CHARS,
) -> tuple[str, str]:
    """Compose a hook breadcrumb ``(outcome, detail)`` within ``budget`` characters.

    Existing non-mesh sync/capture degradations are preserved intact; mesh
    degradations come before the mesh share summary within the remaining
    budget with ``+N more``; any degradation makes the outcome ``degraded``.
    """
    degraded = any(existing_degradations) or bool(mesh_result.degradations)
    parts = [
        safety.safe_terminal_str(d)
        for d in (existing_degradations if degraded else extra_success)
        if d
    ]
    remaining = budget - sum(len(part) + 2 for part in parts)
    mesh_detail = _join_within_budget(mesh_result.degradations, remaining)
    if mesh_detail:
        parts.append(mesh_detail)
        remaining -= len(mesh_detail) + 2
    if share_summary := format_share_summary(mesh_result.shared, budget=remaining):
        parts.append(share_summary)
    return ("degraded" if degraded else "success", "; ".join(parts))


def _join_within_budget(items: Sequence[str], budget: int) -> str:
    """Join ``items`` with ``'; '`` within ``budget`` characters, using ``+N more``."""
    cleaned = [safety.safe_terminal_str(x) for x in items if x]
    if not cleaned or budget <= 0:
        return ""
    full = "; ".join(cleaned)
    if len(full) <= budget:
        return full
    for keep in range(len(cleaned), 0, -1):
        more = len(cleaned) - keep
        tail = f"; +{more} more" if more else ""
        head = "; ".join(cleaned[:keep])
        candidate = f"{head}{tail}"
        if len(candidate) <= budget:
            return candidate
    omitted = f"+{len(cleaned)} more"
    if len(cleaned[0]) <= budget:
        return cleaned[0]
    return omitted if len(omitted) <= budget else ""


@dataclass
class MeshResult:
    """Outcome of one publish check and Syncthing mesh pass."""

    shared: list[tuple[str, str]] = field(default_factory=list)
    partial: list[tuple[str, str, str]] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    unvouched_pending: int | None = None
    unpublished_names: tuple[str, ...] = ()
    vouched_ids: frozenset[str] | None = None
    ran: bool = False
    snapshot_taken: bool = False
    published: bool = False
    stop_cause: str | None = None

    @property
    def shared_clause(self) -> str:
        return format_share_summary(self.shared)

    def compose_breadcrumb(
        self,
        existing_degradations: Sequence[str],
        *,
        extra_success: Sequence[str] = (),
        budget: int = BREADCRUMB_BUDGET_CHARS,
    ) -> tuple[str, str]:
        return compose_hook_breadcrumb(
            existing_degradations,
            self,
            extra_success=extra_success,
            budget=budget,
        )


def _record_degradation(
    result: MeshResult,
    detail: str,
    *,
    attended: bool,
    notice: bool,
) -> None:
    safe_detail = safety.safe_terminal_str(detail)
    result.degradations.append(safe_detail)
    if attended:
        state = core._SYNC_OUTPUT.get()
        if state is not None and "diagnostics" in state:
            state["diagnostics"].append(safe_detail)
        if notice:
            core._notice(safe_detail)


def _publish_via_classify(
    result: MeshResult,
    *,
    backend: LocalBackend,
    own_mm_id: str,
    own_syncthing_id: str | None,
    passphrase: str,
    memory_kb: int,
    req_timeout: float,
    deadline: float,
    attended: bool,
) -> str | None:
    """Publish live REST ``myID`` if needed, returning a transport stop cause."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "timeout"
    check = syncthing.classify_config(
        sanitize=safety.safe_terminal_str,
        timeout=min(req_timeout, remaining),
        budget=remaining,
        deadline=deadline,
    )
    live_id = check.local_device_id
    live_response = check.reach == "ok" and check.source == "rest"
    if (
        live_response
        and live_id is not None
        and syncthing.is_device_id(live_id)
        and syncthing.has_valid_device_id_check_digits(live_id)
    ):
        if live_id != own_syncthing_id:
            try:
                result.published = devices.publish_record(
                    backend,
                    own_mm_id,
                    live_id,
                    passphrase,
                    memory_kb,
                )
            except (OSError, StorageError) as exc:
                cause = _safe_detail_token(attemptlog.storage_errno(exc) or os_error_cause(exc))
                _record_degradation(
                    result,
                    f"could not publish this machine's Syncthing ID ({cause}); "
                    "the next push retries",
                    attended=attended,
                    notice=True,
                )
        return None
    if check.reach == "unreachable":
        return "timeout" if check.cause == "timeout" else "unreachable"
    diagnostic_cause = None
    if check.reach in ("unauthorized", "config-unreadable"):
        diagnostic_cause = check.reach
    elif live_response or (check.reach == "error" and check.cause == "bad-response"):
        diagnostic_cause = "bad-response"
    if own_syncthing_id is None and diagnostic_cause is not None:
        _record_degradation(
            result,
            f"Syncthing ID not published ({diagnostic_cause}). Run: mm diag",
            attended=attended,
            notice=True,
        )
    return None


def _stop_pass(
    result: MeshResult,
    cause: str,
    completed: int,
    total: int,
    *,
    attended: bool,
    write_uncertain: bool = False,
) -> None:
    result.stop_cause = cause
    result.stops.append(cause)
    effect = "a write may have applied" if write_uncertain else "nothing else changed"
    _record_degradation(
        result,
        f"Syncthing mesh step stopped ({cause}) after {completed} of {total} steps; "
        f"{effect}; retries on the next push or pull",
        attended=attended,
        notice=False,
    )


def run(
    config: dict[str, Any],
    passphrase: str,
    memory_kb: int,
    *,
    verb: Literal["push", "pull"],
    attended: bool,
    verbose: bool = False,
    backend: LocalBackend | None = None,
    log_unexpected: Callable[[str, BaseException], None] | None = None,
    trace_formatter: Callable[[BaseException], str] | None = None,
) -> MeshResult:
    """Run the publish check (on push) and the managed Syncthing mesh pass."""
    result = MeshResult()
    try:
        _run_inner(
            result,
            config=config,
            passphrase=passphrase,
            memory_kb=memory_kb,
            verb=verb,
            attended=attended,
            backend=backend,
        )
    except Exception as exc:
        cls_name = type(exc).__name__
        result.stop_cause = "unexpected-error"
        result.stops.append(cls_name)
        if attended:
            detail = f"Syncthing mesh step failed ({cls_name}). Run: mm diag"
            _record_degradation(result, detail, attended=True, notice=True)
            if verbose and trace_formatter is not None:
                core._stderr_lines((trace_formatter(exc),))
        else:
            if log_unexpected is not None:
                log_unexpected(verb, exc)
            detail = f"Syncthing mesh step failed ({cls_name}, see auto{verb}.log)"
            _record_degradation(result, detail, attended=False, notice=False)
    if attended and result.shared:
        state = core._SYNC_OUTPUT.get()
        if state is not None:
            state["mesh_outcome"] = result.shared_clause
    return result


def _run_inner(
    result: MeshResult,
    *,
    config: dict[str, Any],
    passphrase: str,
    memory_kb: int,
    verb: Literal["push", "pull"],
    attended: bool,
    backend: LocalBackend | None,
) -> MeshResult:
    req_timeout = ATTENDED_REQUEST_TIMEOUT_S if attended else HOOK_REQUEST_TIMEOUT_S
    pass_budget = ATTENDED_PASS_BUDGET_S if attended else HOOK_PASS_BUDGET_S
    deadline = time.monotonic() + pass_budget

    if attended:
        core._sync_progress_phase("Checking Syncthing fleet devices")

    try:
        recorded_id = _config_module.load_syncthing_record()
    except ConfigError:
        result.stop_cause = "config-error"
        return result

    storage_cfg = config.get("storage") if isinstance(config, dict) else None
    raw_path = storage_cfg.get("path") if isinstance(storage_cfg, dict) else None
    if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
        return result
    storage_root = Path(raw_path).expanduser().resolve(strict=False)
    if backend is None:
        backend = LocalBackend(str(storage_root))

    dev_cfg = config.get("device") if isinstance(config, dict) else None
    own_mm_id = dev_cfg.get("id") if isinstance(dev_cfg, dict) else None
    if not isinstance(own_mm_id, str) or not own_mm_id:
        return result

    try:
        records = devices.read_fleet_records(
            backend,
            passphrase,
            memory_kb,
            own_device_id=own_mm_id,
        )
    except (OSError, StorageError) as exc:
        cause = _safe_detail_token(attemptlog.storage_errno(exc) or os_error_cause(exc))
        _record_degradation(
            result,
            f"could not list Syncthing records ({cause}); the next push or pull retries",
            attended=attended,
            notice=False,
        )
        result.stop_cause = "storage-error"
        return result

    for mm_id in records.newer_format:
        safe_id = _safe_detail_token(mm_id)
        result.unreadable.append(mm_id)
        _record_degradation(
            result,
            f"the Syncthing record of device {safe_id} is from a newer mm. Run: mm update",
            attended=attended,
            notice=True,
        )
    for mm_id in records.unreadable:
        safe_id = _safe_detail_token(mm_id)
        result.unreadable.append(mm_id)
        _record_degradation(
            result,
            f"the Syncthing record of device {safe_id} is unreadable. Run: mm diag",
            attended=attended,
            notice=True,
        )

    try:
        inventory = devices.inventory_devices(backend)
    except (OSError, StorageError):
        pass
    else:
        published_mm_ids = (
            set(records.by_mm_id)
            | set(records.unreadable)
            | set(records.newer_format)
            | set(records.vanished)
        )
        unpublished_entries = sorted(
            (
                entry
                for entry in inventory.valid
                if isinstance(entry.get("device_id"), str)
                and entry["device_id"] != own_mm_id
                and entry["device_id"] not in published_mm_ids
            ),
            key=lambda entry: entry["device_id"],
        )
        result.unpublished_names = tuple(
            _safe_peer_name(entry.get("device_name"), entry["device_id"])
            for entry in unpublished_entries
        )
        result.vouched_ids = frozenset(records.vouched)

    need_classify = False
    publish_stop = None
    if verb == "push":
        local_expected_id = syncthing.local_device_id_offline() or recorded_id
        need_classify = (
            records.own_syncthing_id is None
            or local_expected_id is None
            or records.own_syncthing_id != local_expected_id
        )
        if need_classify:
            publish_stop = _publish_via_classify(
                result,
                backend=backend,
                own_mm_id=own_mm_id,
                own_syncthing_id=records.own_syncthing_id,
                passphrase=passphrase,
                memory_kb=memory_kb,
                req_timeout=req_timeout,
                deadline=deadline,
                attended=attended,
            )

    if not isinstance(recorded_id, str) or not recorded_id:
        return result

    other_vouched: dict[str, tuple[str, str]] = {
        st_id: (mm_id, dev_name)
        for st_id, (mm_id, dev_name) in records.vouched.items()
        if st_id != recorded_id and mm_id != own_mm_id
    }
    if not other_vouched:
        return result

    result.ran = True
    remaining = deadline - time.monotonic()
    if publish_stop or remaining <= 0:
        _stop_pass(
            result,
            publish_stop or "timeout",
            0,
            len(other_vouched),
            attended=attended,
        )
        return result

    managed = syncthing.ManagedRecord(device_id=recorded_id)
    writer = syncthing.SyncthingWriter(
        managed,
        timeout=min(req_timeout, remaining),
        budget=remaining,
        deadline=deadline,
    )
    snap = writer.mesh_snapshot(_config_module.SYNCTHING_FOLDER_ID)
    if snap.outcome != "ok":
        if snap.outcome == "refused" and snap.cause == "not-managed":
            result.stop_cause = "not-managed"
            if verb == "push" and not need_classify:
                _publish_via_classify(
                    result,
                    backend=backend,
                    own_mm_id=own_mm_id,
                    own_syncthing_id=records.own_syncthing_id,
                    passphrase=passphrase,
                    memory_kb=memory_kb,
                    req_timeout=req_timeout,
                    deadline=deadline,
                    attended=attended,
                )
            return result
        if snap.outcome in ("unreachable", "not-found"):
            diagnostic_cause = "timeout" if snap.cause == "timeout" else "unreachable"
            _stop_pass(result, diagnostic_cause, 0, len(other_vouched), attended=attended)
            return result
        diagnostic_cause = (
            snap.outcome
            if snap.outcome in ("unauthorized", "config-unreadable", "address-unsupported")
            else _safe_detail_token(snap.cause or "bad-response")
        )
        result.stop_cause = diagnostic_cause
        result.stops.append(diagnostic_cause)
        _record_degradation(
            result,
            f"Syncthing mesh step stopped ({diagnostic_cause}). Run: mm diag",
            attended=attended,
            notice=True,
        )
        return result

    result.snapshot_taken = True
    result.unvouched_pending = len(
        syncthing.unvouched_pending_ids(snap.pending_ids, other_vouched, own_id=snap.my_id)
    )

    if not snap.folder_present:
        result.stop_cause = "no-folder"
        result.stops.append("no-folder")
        _record_degradation(
            result,
            "Syncthing mesh step stopped (no-folder). Run: mm diag",
            attended=attended,
            notice=True,
        )
        return result
    if snap.folder_path != storage_root or snap.folder_problems:
        result.stop_cause = "not-mm-folder"
        result.stops.append("not-mm-folder")
        _record_degradation(
            result,
            "Syncthing mesh step stopped (not-mm-folder). Run: mm diag",
            attended=attended,
            notice=True,
        )
        return result

    candidates: list[tuple[str, list[tuple[str, str]]]] = []
    for dev_id in snap.pending_ids:
        if dev_id in other_vouched and dev_id != snap.my_id and dev_id not in snap.devices:
            candidates.append(
                (
                    dev_id,
                    [
                        ("adding device", "add_device"),
                        ("marking as introducer", "mark_introducer"),
                        ("sharing the fleet folder", "share_folder"),
                    ],
                )
            )
    for dev_id in sorted(other_vouched):
        if dev_id == snap.my_id or dev_id in snap.folder_device_ids:
            continue
        cdev = snap.devices.get(dev_id)
        if cdev is None or not cdev.writer_shape or cdev.paused:
            continue
        steps: list[tuple[str, str]] = []
        if not cdev.introducer:
            steps.append(("marking as introducer", "mark_introducer"))
        steps.append(("sharing the fleet folder", "share_folder"))
        candidates.append((dev_id, steps))

    if not candidates:
        return result

    total_steps = sum(len(steps) for _, steps in candidates)
    completed_steps = 0

    for dev_id, steps in candidates:
        mm_id, raw_name = other_vouched[dev_id]
        safe_name = _safe_peer_name(raw_name, mm_id)
        peer_label = f"{safe_name} ({dev_id[:7]})"
        device_started = False
        for step_label, op_name in steps:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if device_started:
                    result.partial.append((safe_name, dev_id, step_label))
                    _record_degradation(
                        result,
                        f"Syncthing mesh step did not finish for {peer_label}; "
                        "the next push or pull reconciles it",
                        attended=attended,
                        notice=False,
                    )
                _stop_pass(result, "timeout", completed_steps, total_steps, attended=attended)
                return result

            step_writer = syncthing.SyncthingWriter(
                managed,
                timeout=min(req_timeout, remaining),
                budget=remaining,
                deadline=deadline,
            )
            if op_name == "add_device":
                w_res = step_writer.add_device(dev_id, name="")
            elif op_name == "mark_introducer":
                w_res = step_writer.mark_introducer(dev_id, require_writer_shape=True)
            else:
                w_res = step_writer.share_folder(
                    _config_module.SYNCTHING_FOLDER_ID, dev_id, expected_path=storage_root
                )

            if w_res.outcome in ("ok", "unchanged"):
                completed_steps += 1
                device_started = True
                if op_name == "share_folder" and w_res.outcome == "ok":
                    result.shared.append((safe_name, dev_id))
                continue
            if w_res.outcome == "refused" and w_res.cause == "device-changed":
                # The owner changed or paused this device after the snapshot.
                break

            if w_res.write_attempted or device_started:
                result.partial.append((safe_name, dev_id, step_label))
                if w_res.write_attempted:
                    detail = (
                        f"Syncthing did not confirm {step_label} for {peer_label}; "
                        "it may have applied; the next push or pull reconciles it"
                    )
                else:
                    detail = (
                        f"Syncthing mesh step did not finish for {peer_label}; "
                        "the next push or pull reconciles it"
                    )
                _record_degradation(result, detail, attended=attended, notice=False)

            if w_res.outcome in ("unreachable", "not-found"):
                diagnostic_cause = "timeout" if w_res.cause == "timeout" else "unreachable"
                _stop_pass(
                    result,
                    diagnostic_cause,
                    completed_steps,
                    total_steps,
                    attended=attended,
                    write_uncertain=w_res.write_attempted,
                )
                return result

            env_stop = w_res.outcome in ("unauthorized", "config-unreadable", "address-unsupported")
            diagnostic_cause = (
                w_res.outcome if env_stop else _safe_detail_token(w_res.cause or w_res.outcome)
            )
            result.stop_cause = diagnostic_cause
            result.stops.append(diagnostic_cause)
            if env_stop or not w_res.write_attempted:
                _record_degradation(
                    result,
                    f"Syncthing mesh step stopped ({diagnostic_cause}). Run: mm diag",
                    attended=attended,
                    notice=True,
                )
                return result

            return result

    return result
