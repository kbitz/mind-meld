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

ATTENDED_REQ_TIMEOUT_S = 2.0
ATTENDED_REQUEST_TIMEOUT_S = ATTENDED_REQ_TIMEOUT_S
ATTENDED_PASS_BUDGET_S = 10.0
HOOK_REQ_TIMEOUT_S = 0.5
HOOK_REQUEST_TIMEOUT_S = HOOK_REQ_TIMEOUT_S
HOOK_PASS_BUDGET_S = 2.0
BREADCRUMB_BUDGET_CHARS = 300


_PATH_CHAR_TRANS = str.maketrans({"/": "_", "~": "_"})


def _safe_detail_token(raw: object) -> str:
    """Sanitize a token for single-line stderr and path-free run details."""
    text = safety.safe_terminal_str(str(raw)).translate(_PATH_CHAR_TRANS).strip()
    return text or "unknown"


def _safe_peer_name(raw_name: object, fallback_id: str) -> str:
    """Sanitize a registry device name for path-free outcome and degradation lines."""
    text = safety.safe_terminal_str(str(raw_name or "")).translate(_PATH_CHAR_TRANS).strip()
    return text or _safe_detail_token(fallback_id)


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
    return lead[:budget]


def compose_hook_breadcrumb(
    existing_degradations: Sequence[str],
    mesh_result: MeshResult,
    *,
    extra_success: Sequence[str] = (),
    budget: int = BREADCRUMB_BUDGET_CHARS,
) -> tuple[str, str]:
    """Compose a hook breadcrumb ``(outcome, detail)`` within ``budget`` characters.

    Existing non-mesh sync/capture degradations are preserved intact; mesh
    degradations come before the mesh share summary within the 300-character
    budget with ``+N more``; any degradation makes the outcome ``degraded``.
    """
    existing_clean = [safety.safe_terminal_str(d) for d in existing_degradations if d]
    mesh_degs = [safety.safe_terminal_str(d) for d in mesh_result.degradations if d]
    if not mesh_degs and not mesh_result.shared:
        if existing_clean:
            return ("degraded", "; ".join(existing_clean))
        success_clean = [safety.safe_terminal_str(p) for p in extra_success if p]
        return ("success", "; ".join(success_clean))

    has_degradation = bool(existing_clean or mesh_degs)
    if has_degradation:
        existing_prefix = "; ".join(existing_clean)
        used = (len(existing_prefix) + 2) if existing_prefix else 0
        mesh_budget = max(0, budget - used) if existing_prefix else budget
        share_text = format_share_summary(mesh_result.shared)
        if not share_text:
            mesh_part = _join_within_budget(mesh_degs, mesh_budget or budget)
            if existing_prefix and mesh_part:
                return ("degraded", f"{existing_prefix}; {mesh_part}")
            return ("degraded", existing_prefix or mesh_part)

        if not mesh_degs:
            min_lead = len("Syncthing now shares the fleet folder with 1 fleet machine")
            bounded_share = format_share_summary(
                mesh_result.shared,
                budget=mesh_budget or min_lead,
            )
            if existing_prefix and bounded_share:
                return ("degraded", f"{existing_prefix}; {bounded_share}")
            return ("degraded", existing_prefix or bounded_share)

        eff_budget = mesh_budget or budget
        deg_text = _join_within_budget(mesh_degs, eff_budget)
        remaining = eff_budget - len(deg_text) - 2
        if remaining >= len("Syncthing now shares the fleet folder with 1 fleet machine"):
            bounded_share = format_share_summary(mesh_result.shared, budget=remaining)
            if bounded_share and len(deg_text) + 2 + len(bounded_share) <= eff_budget:
                combined = f"{deg_text}; {bounded_share}"
                return (
                    "degraded",
                    f"{existing_prefix}; {combined}" if existing_prefix else combined,
                )
        min_share = format_share_summary(mesh_result.shared, budget=min(120, eff_budget // 2))
        deg_budget = max(0, eff_budget - len(min_share) - 2)
        deg_trimmed = _join_within_budget(mesh_degs, deg_budget)
        if deg_trimmed and min_share and len(deg_trimmed) + 2 + len(min_share) <= eff_budget:
            combined = f"{deg_trimmed}; {min_share}"
            return (
                "degraded",
                f"{existing_prefix}; {combined}" if existing_prefix else combined,
            )
        return ("degraded", f"{existing_prefix}; {deg_text}" if existing_prefix else deg_text)

    success_parts = [safety.safe_terminal_str(p) for p in extra_success if p]
    if mesh_result.shared:
        used = len("; ".join(success_parts)) + (2 if success_parts else 0)
        rem = max(0, budget - used)
        share_budget = rem if success_parts else budget
        share_part = format_share_summary(mesh_result.shared, budget=share_budget)
        if share_part:
            success_parts.append(share_part)
    return ("success", _join_within_budget(success_parts, budget))


def _join_within_budget(items: Sequence[str], budget: int) -> str:
    """Join ``items`` with ``'; '`` within ``budget`` characters, using ``+N more``."""
    cleaned = [safety.safe_terminal_str(x) for x in items if x]
    if not cleaned:
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
    more = len(cleaned) - 1
    tail = f" (+{more} more)" if more else ""
    avail = max(1, budget - len(tail))
    return cleaned[0][:avail] + tail


@dataclass
class MeshResult:
    """Outcome of one publish check and Syncthing mesh pass."""

    shared: list[tuple[str, str]] = field(default_factory=list)
    partial: list[tuple[str, str, str]] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    unvouched_pending: int | None = None
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
    """Query ``classify_config`` for live REST ``myID`` and publish if missing or changed."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return own_syncthing_id
    check = syncthing.classify_config(
        sanitize=safety.safe_terminal_str,
        timeout=min(req_timeout, remaining),
        budget=remaining,
    )
    if check.reach == "ok" and check.source == "rest" and check.local_device_id:
        live_id = check.local_device_id
        if live_id != own_syncthing_id:
            try:
                if devices.publish_record(
                    backend,
                    own_mm_id,
                    live_id,
                    passphrase,
                    memory_kb,
                ):
                    result.published = True
                    return live_id
            except (OSError, StorageError) as exc:
                cause = _safe_detail_token(attemptlog.storage_errno(exc) or os_error_cause(exc))
                _record_degradation(
                    result,
                    f"could not publish this machine's Syncthing ID ({cause}); "
                    "the next push retries",
                    attended=attended,
                    notice=True,
                )
        return own_syncthing_id
    if own_syncthing_id is None and (
        check.reach in ("unauthorized", "config-unreadable")
        or (check.reach == "error" and check.cause == "bad-response")
    ):
        token = (
            check.reach if check.reach in ("unauthorized", "config-unreadable") else "bad-response"
        )
        _record_degradation(
            result,
            f"Syncthing ID not published ({token}). Run: mm diag",
            attended=attended,
            notice=True,
        )
    return own_syncthing_id


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
    req_timeout = ATTENDED_REQ_TIMEOUT_S if attended else HOOK_REQ_TIMEOUT_S
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

    need_classify = False
    if verb == "push":
        local_expected_id = (
            recorded_id
            if isinstance(recorded_id, str) and recorded_id
            else syncthing.local_device_id_offline()
        )
        need_classify = (
            records.own_syncthing_id is None
            or local_expected_id is None
            or records.own_syncthing_id != local_expected_id
        )
        if need_classify:
            records.own_syncthing_id = _publish_via_classify(
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
    if remaining <= 0:
        result.stop_cause = "timeout"
        result.stops.append("timeout")
        _record_degradation(
            result,
            f"Syncthing mesh step stopped (timeout) after 0 of {len(other_vouched)} steps; "
            "nothing else changed; retries on the next push or pull",
            attended=attended,
            notice=False,
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
                records.own_syncthing_id = _publish_via_classify(
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
            token = "timeout" if snap.cause == "timeout" else "unreachable"
            result.stop_cause = token
            result.stops.append(token)
            _record_degradation(
                result,
                f"Syncthing mesh step stopped ({token}) after 0 of {len(other_vouched)} steps; "
                "nothing else changed; retries on the next push or pull",
                attended=attended,
                notice=False,
            )
            return result
        token = (
            snap.outcome
            if snap.outcome in ("unauthorized", "config-unreadable", "address-unsupported")
            else _safe_detail_token(snap.cause or "bad-response")
        )
        result.stop_cause = token
        result.stops.append(token)
        _record_degradation(
            result,
            f"Syncthing mesh step stopped ({token}). Run: mm diag",
            attended=attended,
            notice=True,
        )
        return result

    result.snapshot_taken = True
    result.unvouched_pending = sum(
        1 for pid in snap.pending_ids if pid != snap.my_id and pid not in other_vouched
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
    pending_set = set(snap.pending_ids)
    for dev_id in snap.pending_ids:
        if dev_id in other_vouched and dev_id != snap.my_id:
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
        if dev_id in pending_set or dev_id == snap.my_id or dev_id in snap.folder_device_ids:
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
                        f"Syncthing did not confirm {step_label} for {peer_label}; "
                        "it may have applied; the next push or pull reconciles and finishes it",
                        attended=attended,
                        notice=False,
                    )
                result.stop_cause = "timeout"
                result.stops.append("timeout")
                _record_degradation(
                    result,
                    f"Syncthing mesh step stopped (timeout) after {completed_steps} of "
                    f"{total_steps} steps; nothing else changed; retries on the next push or pull",
                    attended=attended,
                    notice=False,
                )
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
                w_res = step_writer.mark_introducer(dev_id)
            else:
                w_res = step_writer.share_folder(_config_module.SYNCTHING_FOLDER_ID, dev_id)

            if w_res.outcome in ("ok", "unchanged"):
                completed_steps += 1
                device_started = True
                if op_name == "share_folder":
                    result.shared.append((safe_name, dev_id))
                continue

            if w_res.outcome in ("unreachable", "not-found"):
                token = "timeout" if w_res.cause == "timeout" else "unreachable"
                if device_started:
                    result.partial.append((safe_name, dev_id, step_label))
                    _record_degradation(
                        result,
                        f"Syncthing did not confirm {step_label} for {peer_label}; "
                        "it may have applied; the next push or pull reconciles and finishes it",
                        attended=attended,
                        notice=False,
                    )
                result.stop_cause = token
                result.stops.append(token)
                _record_degradation(
                    result,
                    f"Syncthing mesh step stopped ({token}) after {completed_steps} of "
                    f"{total_steps} steps; nothing else changed; retries on the next push or pull",
                    attended=attended,
                    notice=False,
                )
                return result

            if w_res.outcome in ("unauthorized", "config-unreadable", "address-unsupported"):
                token = w_res.outcome
                if device_started:
                    result.partial.append((safe_name, dev_id, step_label))
                    _record_degradation(
                        result,
                        f"Syncthing did not confirm {step_label} for {peer_label}; "
                        "it may have applied; the next push or pull reconciles and finishes it",
                        attended=attended,
                        notice=False,
                    )
                result.stop_cause = token
                result.stops.append(token)
                _record_degradation(
                    result,
                    f"Syncthing mesh step stopped ({token}). Run: mm diag",
                    attended=attended,
                    notice=True,
                )
                return result

            result.partial.append((safe_name, dev_id, step_label))
            result.stop_cause = _safe_detail_token(w_res.cause or w_res.outcome)
            result.stops.append(result.stop_cause)
            _record_degradation(
                result,
                f"Syncthing did not confirm {step_label} for {peer_label}; "
                "it may have applied; the next push or pull reconciles and finishes it",
                attended=attended,
                notice=False,
            )
            return result

    return result
