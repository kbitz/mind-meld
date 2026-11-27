"""Encrypted-record Syncthing mesh pass for mm-managed machines (Tracks 152A, 154A).

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


def format_outcome_clauses(mesh_result: MeshResult) -> list[str]:
    """Return shared-style outcome clauses for shares, clears, removals and renames."""
    clauses: list[str] = []
    if mesh_result.shared:
        clauses.append(format_share_summary(mesh_result.shared))
    clauses.extend(mesh_result.clear_clauses)
    for safe_name, st_id in mesh_result.removed:
        clauses.append(f"Syncthing removed retired device {safe_name} ({st_id[:7]})")
    for safe_name, st_id, is_own in mesh_result.renamed:
        if is_own:
            clauses.append(f"Syncthing named this device {safe_name} ({st_id[:7]})")
        else:
            clauses.append(f"Syncthing renamed {safe_name} ({st_id[:7]})")
    return clauses


def compose_hook_breadcrumb(
    existing_degradations: Sequence[str],
    mesh_result: MeshResult,
    *,
    extra_success: Sequence[str] = (),
    budget: int = BREADCRUMB_BUDGET_CHARS,
) -> tuple[str, str]:
    """Compose a hook breadcrumb ``(outcome, detail)`` within ``budget`` characters.

    Existing non-mesh sync/capture degradations are preserved intact; mesh
    degradations come before the mesh share, clear, removal and rename summaries
    within the remaining budget with ``+N more``; any degradation makes the
    outcome ``degraded``.
    """
    degraded = any(existing_degradations) or bool(mesh_result.degradations)
    parts = [
        safety.safe_terminal_str(d)
        for d in (existing_degradations if degraded else extra_success)
        if d
    ]
    remaining = budget - sum(len(part) + 2 for part in parts)
    non_wait_degradations = [
        d for d in mesh_result.degradations if not d.startswith(attemptlog.MESH_WAIT_PREFIX)
    ]
    mesh_detail = _join_within_budget(non_wait_degradations, remaining)
    if mesh_detail:
        parts.append(mesh_detail)
        remaining -= len(mesh_detail) + 2
    if share_summary := format_share_summary(mesh_result.shared, budget=remaining):
        parts.append(share_summary)
        remaining -= len(share_summary) + 2
    tail_clauses = [
        *mesh_result.clear_clauses,
        *(
            f"Syncthing removed retired device {safe_name} ({st_id[:7]})"
            for safe_name, st_id in mesh_result.removed
        ),
        *(
            f"Syncthing named this device {safe_name} ({st_id[:7]})"
            if is_own
            else f"Syncthing renamed {safe_name} ({st_id[:7]})"
            for safe_name, st_id, is_own in mesh_result.renamed
        ),
    ]
    if tail_clauses and remaining > 0:
        if tail_detail := _join_within_budget(tail_clauses, remaining):
            parts.append(tail_detail)
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
    cleared: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    renamed: list[tuple[str, str, bool]] = field(default_factory=list)
    partial: list[tuple[str, str, str]] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    unvouched_pending: int | None = None
    unvouched_pending_devices: dict[str, str] = field(default_factory=dict, repr=False)
    unpublished_names: tuple[str, ...] = ()
    superseded_registrations: tuple[devices.SupersededRegistration, ...] = ()
    vouched_ids: frozenset[str] | None = None
    ran: bool = False
    snapshot_taken: bool = False
    removals_evaluated: bool = False
    removal_waits_recorded: bool = False
    published: bool = False
    stop_cause: str | None = None

    @property
    def shared_clause(self) -> str:
        return format_share_summary(self.shared)

    @property
    def clear_clauses(self) -> list[str]:
        return [
            f"Syncthing cleared the introducer flag on {safe_name} ({st_id[:7]})"
            for safe_name, st_id in self.cleared
        ]

    @property
    def outcome_clauses(self) -> list[str]:
        return format_outcome_clauses(self)

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

    def removal_wait_only(self, existing_degradations: Sequence[str]) -> bool:
        """Identify hook degradations fully explained by the separate mesh record."""
        return (
            self.removal_waits_recorded
            and bool(self.degradations)
            and not any(existing_degradations)
            and all(detail.startswith(attemptlog.MESH_WAIT_PREFIX) for detail in self.degradations)
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
        if not safe_detail.startswith(attemptlog.MESH_WAIT_PREFIX):
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
    joining: bool = False,
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
            joining=joining,
            backend=backend,
        )
    except Exception as exc:
        cls_name = type(exc).__name__
        result.removals_evaluated = False
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
    if result.removals_evaluated:
        try:
            attemptlog.write_mesh_waits(
                [d for d in result.degradations if d.startswith(attemptlog.MESH_WAIT_PREFIX)]
            )
        except (Exception, SystemExit):
            pass
        else:
            result.removal_waits_recorded = True
    if attended and (state := core._SYNC_OUTPUT.get()) is not None:
        state["mesh_result"] = result
    return result


def _registered_ids(inventory: devices.DeviceInventory) -> set[str]:
    return (
        {entry["device_id"] for entry in inventory.valid}
        | {did for key, _ in inventory.dropped if (did := devices.parse_device_key(key))}
        | set(inventory.vanished)
    )


def _note_introducer_hold(
    result: MeshResult,
    *,
    retired_name: str,
    device_id: str,
    introducer_name: str,
    attended: bool,
) -> None:
    _record_degradation(
        result,
        f"{retired_name} ({syncthing.short_device_id(device_id)}) stays until {introducer_name} "
        "drops it; its next push or pull does that where mm manages Syncthing on this "
        "release; otherwise remove it in Syncthing's web UI there",
        attended=attended,
        notice=True,
    )


def _retired_share_still_listed(
    result: MeshResult, *, name: str, device_id: str, attended: bool
) -> None:
    _record_degradation(
        result,
        f"Syncthing did not remove retired device {name} ({syncthing.short_device_id(device_id)}) "
        "from the fleet folder; remove it in Syncthing's web UI",
        attended=attended,
        notice=True,
    )


def _stop_folder_steps(result: MeshResult, cause: str, *, attended: bool) -> None:
    result.removals_evaluated = False
    result.stop_cause = cause
    result.stops.append(cause)
    _record_degradation(
        result,
        f"Syncthing mesh step stopped ({cause}). Run: mm diag",
        attended=attended,
        notice=True,
    )


def _run_inner(
    result: MeshResult,
    *,
    config: dict[str, Any],
    passphrase: str,
    memory_kb: int,
    verb: Literal["push", "pull"],
    attended: bool,
    joining: bool = False,
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

    retirements_listed = True
    try:
        retirements = devices.read_retirement_records(
            backend,
            passphrase,
            memory_kb,
            strict=joining,
        )
    except (OSError, StorageError) as exc:
        retirements_listed = False
        cause = _safe_detail_token(attemptlog.storage_errno(exc) or os_error_cause(exc))
        _record_degradation(
            result,
            f"could not list retirement records ({cause}); the next push or pull retries",
            attended=attended,
            notice=False,
        )
        retirements = devices.FleetRetirementRecords()

    for mm_id in retirements.newer_format:
        safe_id = _safe_detail_token(mm_id)
        result.unreadable.append(mm_id)
        _record_degradation(
            result,
            f"the retirement record of device {safe_id} is from a newer mm. Run: mm update",
            attended=attended,
            notice=True,
        )
    for mm_id in retirements.unreadable:
        safe_id = _safe_detail_token(mm_id)
        result.unreadable.append(mm_id)
        _record_degradation(
            result,
            f"the retirement record of device {safe_id} is unreadable. "
            "Remove its retirement file by hand if it stays unreadable",
            attended=attended,
            notice=False,
        )
        if attended:
            core._notice(
                f"Retirement file {devices.retired_record_key(mm_id)} is unreadable. "
                "Remove it by hand if it stays unreadable."
            )
    for mm_id in retirements.vanished:
        safe_id = _safe_detail_token(mm_id)
        _record_degradation(
            result,
            f"the retirement record of device {safe_id} is still syncing; "
            "the next push or pull retries",
            attended=attended,
            notice=False,
        )

    inventory = records.inventory
    if inventory is None and (
        bool(retirements.records) or (attended and verb == "pull" and bool(recorded_id))
    ):
        try:
            inventory = devices.inventory_devices(backend)
        except (OSError, StorageError):
            inventory = None

    published_mm_ids = set(records.by_mm_id).union(
        records.unreadable, records.newer_format, records.vanished
    )
    split: devices.UnpublishedSplit | None = None
    if inventory is not None:
        split = devices.split_unpublished(
            inventory,
            published_mm_ids,
            own_device_id=own_mm_id,
        )
        if attended and verb == "pull" and recorded_id:
            result.vouched_ids = frozenset(records.vouched)
            result.unpublished_names = split.unpublished_names
            result.superseded_registrations = split.superseded
    elif attended and verb == "pull" and recorded_id:
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

    result.ran = True
    pre_snapshot_steps = max(1, len(other_vouched) + len(retirements.records))
    remaining = deadline - time.monotonic()
    if publish_stop or remaining <= 0:
        _stop_pass(
            result,
            publish_stop or "timeout",
            0,
            pre_snapshot_steps,
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
            _stop_pass(result, diagnostic_cause, 0, pre_snapshot_steps, attended=attended)
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
    unvouched_ids = syncthing.unvouched_pending_ids(
        snap.pending_ids, other_vouched, own_id=snap.my_id
    )
    result.unvouched_pending = len(unvouched_ids)
    result.unvouched_pending_devices = {
        pid: snap.pending_devices.get(pid, "") for pid in unvouched_ids
    }

    folder_ok = True
    if not snap.folder_present:
        folder_ok = False
        _stop_folder_steps(result, "no-folder", attended=attended)
    elif snap.folder_path != storage_root or snap.folder_problems:
        folder_ok = False
        _stop_folder_steps(result, "not-mm-folder", attended=attended)

    # 1. Plan pairing steps (only when fleet folder is valid)
    candidates: list[tuple[str, list[tuple[str, str]]]] = []
    if folder_ok:
        pairable = set(other_vouched)
        if joining:
            if not retirements_listed or (
                retirements.unreadable or retirements.vanished or retirements.newer_format
            ):
                pairable.clear()
            else:
                pairable = {
                    st_id
                    for st_id, (mm_id, _) in other_vouched.items()
                    if st_id not in retirements.by_syncthing_id
                    and mm_id not in retirements.records
                    and st_id not in snap.other_folder_device_ids
                }
        potential_adds = sorted(pairable) if joining else snap.pending_ids
        for dev_id in potential_adds:
            if dev_id not in pairable or dev_id == snap.my_id or dev_id in snap.devices:
                continue
            candidates.append(
                (
                    dev_id,
                    [
                        ("adding device", "add_device"),
                        ("sharing the fleet folder", "share_folder"),
                    ],
                )
            )
        for dev_id in sorted(pairable):
            if dev_id == snap.my_id or dev_id in snap.folder_device_ids:
                continue
            cdev = snap.devices.get(dev_id)
            if cdev is None or not cdev.writer_shape or cdev.paused:
                continue
            candidates.append((dev_id, [("sharing the fleet folder", "share_folder")]))

    # 1b. Plan stale introducer-flag clears (only when fleet folder is valid)
    introducer_clears: list[str] = []
    if folder_ok:
        introducer_clears = [
            dev_id
            for dev_id in sorted(other_vouched)
            if dev_id != snap.my_id
            and (cdev := snap.devices.get(dev_id)) is not None
            and cdev.introducer
            and dev_id in snap.folder_device_ids
            and dev_id not in snap.other_folder_device_ids
        ]
    introducer_clear_set = frozenset(introducer_clears)

    # 2. Plan retirement removal steps (only when fleet folder is valid)
    executable_removals: list[tuple[str, str, str]] = []
    if folder_ok and retirements.records:
        if inventory is None:
            try:
                inventory = devices.inventory_devices(backend)
                split = devices.split_unpublished(
                    inventory,
                    published_mm_ids,
                    own_device_id=own_mm_id,
                )
            except (OSError, StorageError) as exc:
                cause = _safe_detail_token(attemptlog.storage_errno(exc) or os_error_cause(exc))
                _record_degradation(
                    result,
                    f"Syncthing device removals wait for device registry ({cause}); "
                    "the next push or pull retries",
                    attended=attended,
                    notice=False,
                )
                inventory = None

        if inventory is not None:
            all_registered_ids = _registered_ids(inventory)
            seen_retired_st_ids: set[str] = set()
            candidate_removals: list[tuple[devices.RetirementRecord, str]] = []
            for rec in sorted(retirements.records.values(), key=lambda r: r.mm_device_id):
                if rec.mm_device_id in all_registered_ids:
                    continue
                st_id = rec.syncthing_device_id
                if (
                    st_id is None
                    or st_id == snap.my_id
                    or st_id == recorded_id
                    or st_id in records.vouched
                    or st_id in seen_retired_st_ids
                ):
                    continue
                seen_retired_st_ids.add(st_id)
                if st_id in snap.devices:
                    candidate_removals.append((rec, st_id))
                elif st_id in snap.folder_device_ids:
                    _retired_share_still_listed(
                        result,
                        name=_safe_peer_name(rec.device_name, rec.mm_device_id),
                        device_id=st_id,
                        attended=attended,
                    )

            if candidate_removals:
                hold_removals = False
                for mm_id in records.newer_format:
                    if mm_id == own_mm_id:
                        continue
                    hold_removals = True
                    safe_id = _safe_detail_token(mm_id)
                    _record_degradation(
                        result,
                        f"Syncthing device removals wait for device {safe_id}'s newer-format "
                        "Syncthing record. Run: mm update",
                        attended=attended,
                        notice=True,
                    )
                for mm_id in records.unreadable:
                    if mm_id == own_mm_id:
                        continue
                    hold_removals = True
                    safe_id = _safe_detail_token(mm_id)
                    _record_degradation(
                        result,
                        f"Syncthing device removals wait for device {safe_id}'s unreadable "
                        "Syncthing record. Run: mm diag",
                        attended=attended,
                        notice=True,
                    )
                for key, _reason in inventory.dropped:
                    mm_id = devices.parse_device_key(key)
                    if mm_id == own_mm_id:
                        continue
                    hold_removals = True
                    dropped_id = _safe_detail_token(mm_id or key)
                    _record_degradation(
                        result,
                        f"Syncthing device removals wait for unreadable device entry "
                        f"{dropped_id}. Run: mm diag",
                        attended=attended,
                        notice=True,
                    )
                for mm_id in sorted(
                    (set(inventory.vanished) | set(records.vanished)) - {own_mm_id}
                ):
                    hold_removals = True
                    van_id = _safe_detail_token(mm_id)
                    _record_degradation(
                        result,
                        f"Syncthing device removals wait for device {van_id} to finish syncing; "
                        "the next push or pull retries",
                        attended=attended,
                        notice=True,
                    )
                if split is not None:
                    superseded_ids = {s.old_id for s in split.superseded}
                    for sup in split.superseded:
                        hold_removals = True
                        s_name = _safe_peer_name(sup.name, sup.old_id)
                        s_old = _safe_detail_token(sup.old_id)
                        _record_degradation(
                            result,
                            f"Syncthing device removals wait for superseded registration "
                            f"{s_name} ({s_old}). Run: {devices.retire_command(sup.old_id)}",
                            attended=attended,
                            notice=True,
                        )
                    for entry in sorted(inventory.valid, key=lambda item: item["device_id"]):
                        did = entry["device_id"]
                        if did == own_mm_id or did in published_mm_ids or did in superseded_ids:
                            continue
                        hold_removals = True
                        u_name = _safe_peer_name(entry.get("device_name"), did)
                        u_id = _safe_detail_token(did)
                        _record_degradation(
                            result,
                            f"Syncthing device removals wait for {u_name} ({u_id}) to publish "
                            f"its Syncthing ID; run 'mm update' and 'mm push' on {u_name}",
                            attended=attended,
                            notice=True,
                        )

                if not hold_removals:
                    for rec, st_id in candidate_removals:
                        cdev = snap.devices.get(st_id)
                        ret_name = _safe_peer_name(rec.device_name, rec.mm_device_id)
                        intro_dev = (
                            snap.devices.get(cdev.introduced_by)
                            if cdev is not None and cdev.introduced_by
                            else None
                        )
                        if (
                            cdev is not None
                            and intro_dev is not None
                            and intro_dev.introducer
                            and cdev.introduced_by in other_vouched
                            and cdev.introduced_by not in introducer_clear_set
                        ):
                            intro_mm_id, intro_raw_name = other_vouched[cdev.introduced_by]
                            intro_name = _safe_peer_name(intro_raw_name, intro_mm_id)
                            _note_introducer_hold(
                                result,
                                retired_name=ret_name,
                                device_id=st_id,
                                introducer_name=intro_name,
                                attended=attended,
                            )
                            continue
                        executable_removals.append((ret_name, st_id, rec.mm_device_id))
    if folder_ok and retirements_listed and not executable_removals:
        result.removals_evaluated = True

    # 3. Plan own-device rename
    raw_own_cfg_name = dev_cfg.get("name") if isinstance(dev_cfg, dict) else None
    own_target_name = str(raw_own_cfg_name or "").strip()
    need_own_rename = bool(own_target_name) and snap.own_name != own_target_name

    # 4. Plan fleet peer renames
    peer_renames: list[tuple[str, str, str]] = []
    for st_id in sorted(other_vouched):
        cdev = snap.devices.get(st_id)
        if cdev is None:
            continue
        mm_id, raw_reg_name = other_vouched[st_id]
        target_name = safety.safe_terminal_str(str(raw_reg_name or "")).strip()[
            :_MAX_PEER_NAME_CHARS
        ]
        if not target_name or cdev.name == target_name:
            continue
        safe_name = _safe_peer_name(target_name, mm_id)
        peer_renames.append((st_id, target_name, safe_name))

    total_steps = (
        sum(len(steps) for _, steps in candidates)
        + len(introducer_clears)
        + len(executable_removals)
        + (1 if need_own_rename else 0)
        + len(peer_renames)
    )
    if total_steps == 0:
        return result

    completed_steps = 0

    # Execute 1: Pairing
    for dev_id, steps in candidates:
        mm_id, raw_name = other_vouched[dev_id]
        safe_name = _safe_peer_name(raw_name, mm_id)
        peer_target_name = safety.safe_terminal_str(str(raw_name or "")).strip()[
            :_MAX_PEER_NAME_CHARS
        ]
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
                w_res = step_writer.add_device(
                    dev_id, name=peer_target_name, join_path=storage_root if joining else None
                )
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

            if w_res.outcome == "refused" and w_res.cause in ("no-folder", "not-mm-folder"):
                folder_ok = False
                _stop_folder_steps(result, w_res.cause, attended=attended)
                break

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

        if not folder_ok:
            break

    # Execute 1b: Clear stale introducer flags
    for dev_id in introducer_clears if folder_ok else ():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_pass(result, "timeout", completed_steps, total_steps, attended=attended)
            return result
        mm_id, raw_name = other_vouched[dev_id]
        safe_name = _safe_peer_name(raw_name, mm_id)
        peer_label = f"{safe_name} ({dev_id[:7]})"
        step_writer = syncthing.SyncthingWriter(
            managed,
            timeout=min(req_timeout, remaining),
            budget=remaining,
            deadline=deadline,
        )
        w_res = step_writer.clear_introducer(dev_id)
        if w_res.outcome == "ok":
            completed_steps += 1
            result.cleared.append((safe_name, dev_id))
            continue
        if w_res.outcome == "unchanged" or (
            w_res.outcome == "refused" and w_res.cause == "device-not-configured"
        ):
            completed_steps += 1
            continue
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
        if w_res.outcome in ("unauthorized", "config-unreadable", "address-unsupported"):
            result.stop_cause = w_res.outcome
            result.stops.append(w_res.outcome)
            _record_degradation(
                result,
                f"Syncthing mesh step stopped ({w_res.outcome}). Run: mm diag",
                attended=attended,
                notice=True,
            )
            return result
        if w_res.write_attempted:
            detail = (
                f"Syncthing did not confirm clearing the introducer flag for {peer_label}; "
                "it may have applied; the next push or pull reconciles it"
            )
        else:
            detail = (
                f"Syncthing mesh step did not finish for {peer_label}; "
                "the next push or pull reconciles it"
            )
        _record_degradation(result, detail, attended=attended, notice=False)

    # Execute 2: Retirement removals
    def still_retired(mm_id: str, st_id: str, ret_name: str, introduced_by: str) -> bool:
        try:
            fresh = devices.read_fleet_records(
                backend, passphrase, memory_kb, own_device_id=own_mm_id, strict=True
            )
            current_retirement = devices.read_retirement_records(
                backend, passphrase, memory_kb, strict=True
            ).records.get(mm_id)
        except (OSError, StorageError):
            _record_degradation(
                result,
                "Syncthing device removals wait for a complete fleet registry read. Run: mm diag",
                attended=attended,
                notice=True,
            )
            return False

        if current_retirement is None or current_retirement.syncthing_device_id != st_id:
            return False
        assert fresh.inventory is not None
        registered = _registered_ids(fresh.inventory)
        if mm_id in registered or st_id in fresh.vouched:
            return False
        missing = registered - fresh.by_mm_id.keys() - {own_mm_id}
        if missing:
            for did in sorted(missing):
                safe_id = _safe_detail_token(did)
                _record_degradation(
                    result,
                    f"Syncthing device removals wait for {safe_id}'s Syncthing record; "
                    "the next push or pull retries",
                    attended=attended,
                    notice=False,
                )
            return False
        if introduced_by in fresh.vouched:
            intro_id, intro_name = fresh.vouched[introduced_by]
            _note_introducer_hold(
                result,
                retired_name=ret_name,
                device_id=st_id,
                introducer_name=_safe_peer_name(intro_name, intro_id),
                attended=attended,
            )
            return False
        return True

    for ret_name, st_id, mm_id in executable_removals if folder_ok else ():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_pass(result, "timeout", completed_steps, total_steps, attended=attended)
            return result
        step_writer = syncthing.SyncthingWriter(
            managed,
            timeout=min(req_timeout, remaining),
            budget=remaining,
            deadline=deadline,
        )
        rem_res = step_writer.remove_device(
            st_id,
            fleet_folder_id=_config_module.SYNCTHING_FOLDER_ID,
            expected_path=storage_root,
            still_retired=lambda introducer: still_retired(mm_id, st_id, ret_name, introducer),
        )
        if rem_res.outcome == "refused" and rem_res.cause == "retirement-held":
            continue
        if rem_res.outcome == "refused" and rem_res.cause in ("no-folder", "not-mm-folder"):
            folder_ok = False
            _stop_folder_steps(result, rem_res.cause, attended=attended)
            break
        if rem_res.outcome == "ok":
            completed_steps += 1
            result.removed.append((ret_name, st_id))
            continue
        if rem_res.outcome == "unchanged":
            completed_steps += 1
            continue
        if rem_res.outcome == "refused" and rem_res.cause == "device-connected":
            _record_degradation(
                result,
                f"{ret_name} ({st_id[:7]}) is retired but still connected; shut it down or "
                "stop Syncthing there, or remove it in Syncthing's web UI",
                attended=attended,
                notice=True,
            )
            continue
        if rem_res.outcome == "refused" and rem_res.cause == "shared-in-other-folder":
            _record_degradation(
                result,
                f"{ret_name} ({st_id[:7]}) is retired from Mind Meld but also shares another "
                "Syncthing folder on this machine; remove it in Syncthing's web UI",
                attended=attended,
                notice=True,
            )
            continue
        if rem_res.outcome in ("unreachable", "not-found"):
            diagnostic_cause = "timeout" if rem_res.cause == "timeout" else "unreachable"
            _stop_pass(
                result,
                diagnostic_cause,
                completed_steps,
                total_steps,
                attended=attended,
                write_uncertain=rem_res.write_attempted,
            )
            return result
        if rem_res.outcome in ("unauthorized", "config-unreadable", "address-unsupported"):
            result.stop_cause = rem_res.outcome
            result.stops.append(rem_res.outcome)
            _record_degradation(
                result,
                f"Syncthing mesh step stopped ({rem_res.outcome}). Run: mm diag",
                attended=attended,
                notice=True,
            )
            return result
        cause_tok = _safe_detail_token(rem_res.cause or rem_res.outcome)
        _record_degradation(
            result,
            f"Syncthing did not remove retired device {ret_name} ({st_id[:7]}) ({cause_tok}); "
            "remove it in Syncthing's web UI",
            attended=attended,
            notice=True,
        )
    if folder_ok and retirements_listed:
        result.removals_evaluated = True

    if result.removed:
        remaining = deadline - time.monotonic()
        members, check_error = syncthing.SyncthingWriter(
            managed,
            timeout=min(req_timeout, remaining),
            budget=remaining,
            deadline=deadline,
        ).fleet_folder_members(_config_module.SYNCTHING_FOLDER_ID)
        for ret_name, st_id in result.removed:
            if check_error is not None and check_error.cause == "timeout":
                _record_degradation(
                    result,
                    "Syncthing deferred its retirement share check to the next push or pull",
                    attended=attended,
                    notice=False,
                )
                break
            if check_error is not None:
                _record_degradation(
                    result,
                    f"could not confirm Syncthing removed retired device {ret_name} "
                    f"({syncthing.short_device_id(st_id)}); check the fleet folder in "
                    "Syncthing's web UI",
                    attended=attended,
                    notice=True,
                )
            elif st_id in members:
                _retired_share_still_listed(
                    result, name=ret_name, device_id=st_id, attended=attended
                )

    # Execute 3: Own-device rename
    if need_own_rename and snap.my_id is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_pass(result, "timeout", completed_steps, total_steps, attended=attended)
            return result
        step_writer = syncthing.SyncthingWriter(
            managed,
            timeout=min(req_timeout, remaining),
            budget=remaining,
            deadline=deadline,
        )
        own_res = step_writer.set_own_device_name(own_target_name)
        if own_res.outcome == "ok":
            completed_steps += 1
            result.renamed.append((_safe_peer_name(own_target_name, own_mm_id), snap.my_id, True))
        elif own_res.outcome == "unchanged":
            completed_steps += 1
        elif own_res.outcome in ("unreachable", "not-found"):
            diagnostic_cause = "timeout" if own_res.cause == "timeout" else "unreachable"
            _stop_pass(
                result,
                diagnostic_cause,
                completed_steps,
                total_steps,
                attended=attended,
                write_uncertain=own_res.write_attempted,
            )
            return result
        elif own_res.outcome in ("unauthorized", "config-unreadable", "address-unsupported"):
            result.stop_cause = own_res.outcome
            result.stops.append(own_res.outcome)
            _record_degradation(
                result,
                f"Syncthing mesh step stopped ({own_res.outcome}). Run: mm diag",
                attended=attended,
                notice=True,
            )
            return result
        else:
            cause_tok = _safe_detail_token(own_res.cause or own_res.outcome)
            _record_degradation(
                result,
                f"could not rename this device in Syncthing ({cause_tok}); "
                "rename it in Syncthing's web UI",
                attended=attended,
                notice=True,
            )

    # Execute 4: Fleet peer renames
    for st_id, target_name, safe_name in peer_renames:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_pass(result, "timeout", completed_steps, total_steps, attended=attended)
            return result
        step_writer = syncthing.SyncthingWriter(
            managed,
            timeout=min(req_timeout, remaining),
            budget=remaining,
            deadline=deadline,
        )
        ren_res = step_writer.set_device_name(st_id, target_name)
        if ren_res.outcome == "ok":
            completed_steps += 1
            result.renamed.append((safe_name, st_id, False))
        elif ren_res.outcome == "unchanged":
            completed_steps += 1
        elif ren_res.outcome in ("unreachable", "not-found"):
            diagnostic_cause = "timeout" if ren_res.cause == "timeout" else "unreachable"
            _stop_pass(
                result,
                diagnostic_cause,
                completed_steps,
                total_steps,
                attended=attended,
                write_uncertain=ren_res.write_attempted,
            )
            return result
        elif ren_res.outcome in ("unauthorized", "config-unreadable", "address-unsupported"):
            result.stop_cause = ren_res.outcome
            result.stops.append(ren_res.outcome)
            _record_degradation(
                result,
                f"Syncthing mesh step stopped ({ren_res.outcome}). Run: mm diag",
                attended=attended,
                notice=True,
            )
            return result
        else:
            cause_tok = _safe_detail_token(ren_res.cause or ren_res.outcome)
            _record_degradation(
                result,
                f"could not rename {safe_name} ({st_id[:7]}) in Syncthing ({cause_tok}); "
                "rename it in Syncthing's web UI",
                attended=attended,
                notice=True,
            )

    return result
