"""`mm devices`, `mm devices approve` and `mm devices retire`, independent of command registration.

`devices` lists the fleet's registered devices as a Rich table or as the stable
`--format=json` array consumed by `retro-fleet`. `approve` pairs a joining
machine on an mm-managed Syncthing instance (with no argument, by device name,
by short-ID prefix, or by full Syncthing device ID; or finishes an earlier
half-finished approval by full ID), marks it as introducer and shares the fleet
folder with it. `retire` records an encrypted retirement record under
`retired/<mm-id>.enc` and removes the machine's `syncthing/<id>.enc` and
`devices/<id>.json` records under the mm lock.

Raise surface for direct callers:
- `typer.Exit(1)`: a `core._error` refusal.
The `@devices_app` group callback and `@devices_app.command` shells and their
`--help` text stay in `cli.py`; this module never imports `cli`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from packaging.version import InvalidVersion, Version
from rich.table import Table

from mind_meld import attemptlog, core, crypto, lockfile, outputstyle, syncthing
from mind_meld import config as _config_module
from mind_meld import devices as _devices_module
from mind_meld.consoles import console
from mind_meld.errors import ConfigError, CryptoError, LockError, StorageError, os_error_cause
from mind_meld.safety import safe_str, safe_terminal_str
from mind_meld.storage import get_backend
from mind_meld.storage.keys import (
    device_key,
    parse_device_key,
    retired_record_key,
    syncthing_record_key,
)


def devices(fmt: str) -> None:
    """List all registered devices (`table` or `json`)."""
    config = core._get_config(read_only=True)
    backend = get_backend(config)
    device_list = core._list_devices_warn(backend)
    my_id = config["device"]["id"]

    fmt_lower = fmt.lower()
    if fmt_lower not in ("table", "json"):
        core._error(f"--format must be 'table' or 'json', got {fmt!r}")
        return  # unreachable

    if fmt_lower == "json":
        # Stable contract for the retro-fleet skill: emit a JSON list with
        # canonical keys. Render missing fields as null (not em-dashes \u2014
        # em-dashes are a table-side rendering convention). Print to stdout
        # so subprocess consumers can capture it cleanly.
        #
        # Sort by device_id for stable output across platforms \u2014 list_devices
        # iterates the storage directory and macOS APFS typically yields
        # alphabetical order, but Linux ext4 and other filesystems do not.
        # Cross-platform peers must see the same order or any consumer's
        # diff against a prior snapshot would flap.
        sorted_devices = sorted(device_list, key=lambda d: d["device_id"])
        records = [
            {
                "device_id": d["device_id"],
                "device_name": d.get("device_name"),
                "last_seen": d.get("last_seen"),
                "last_seen_version": d.get("last_seen_version"),
                "is_self": d["device_id"] == my_id,
            }
            for d in sorted_devices
        ]
        # Use plain print, not console \u2014 Rich injects styling that breaks the
        # JSON contract. sort_keys=True for stable per-record key order.
        print(json.dumps(records, sort_keys=True))
        return

    if not device_list:
        console.print("[yellow]No devices registered.[/yellow]")
        return

    table = Table(title="Registered Devices")
    table.add_column("Name")
    table.add_column("ID")
    table.add_column("Last Push")
    table.add_column("Version")
    table.add_column("")

    for d in device_list:
        marker = "[green]\u2190 this device[/green]" if d["device_id"] == my_id else ""
        # `last_seen` is seeded only on push (not at register time), so a
        # registered-but-never-pushed device renders as an em-dash rather
        # than misleadingly showing its registration time.
        # `last_seen_version` (v0.9.2+) records the mm version on the
        # peer's last push. Missing value => peer hasn't pushed since
        # upgrading to v0.9.2 \u2014 surface as em-dash so users can spot
        # pre-v0.9.2 peers that are blocking pull via the fleet-version
        # refusal gate.
        # device_name, device_id, last_seen_version are peer-controlled
        # JSON values. Rich Table cells interpret markup AND pass raw
        # terminal escapes through (verified). Sanitize before render.
        # Group 7 preflight #1 sweep extension (adversarial #3).
        table.add_row(
            safe_str(d.get("device_name", "?")),
            safe_str(d["device_id"]),
            safe_str(d.get("last_seen", "\u2014")),
            safe_str(d.get("last_seen_version", "\u2014")),
            marker,
        )

    console.print(table)


def _stop_bad_device_id(pasted: str, why: str) -> None:
    """Exit 1 when the pasted string is not a valid peer Syncthing device ID."""
    core._error(
        f"devices approve: '{safe_terminal_str(pasted)}' is not a Syncthing device ID "
        f"({why}). Nothing was changed. Copy the ID that 'mm init' printed on the new "
        'machine ("This machine\'s Syncthing device ID: ..."), then run '
        "'mm devices approve <device-id>' again."
    )


def _stop_not_managed() -> None:
    """Exit 1 when mm does not manage Syncthing on this machine."""
    core._error(
        "devices approve: mm does not manage Syncthing on this machine, so it changes "
        "nothing there. Approve the new machine by hand in Syncthing's GUI as README "
        f'"{core.SYNCTHING_GUIDE_HEADING}" describes '
        '("Approving a new machine by hand on a fleet machine").'
    )


def _stop_unchanged(outcome: str, cause: str | None, candidate: str) -> None:
    """Exit 1 on a read or initial-add failure before any change is committed."""
    if outcome in ("not-found", "unreachable"):
        core._error(
            "devices approve: Syncthing is not running on this machine (or its API at the "
            f"GUI address is not answering), so mm cannot approve {safe_terminal_str(candidate)}. "
            "Nothing was changed. Start it, then run this command again. "
            f"Run: {core._syncthing_start_command()}"
        )
    if outcome == "refused" and cause == "not-managed":
        _stop_not_managed()
    out_s = safe_terminal_str(outcome)
    cause_s = safe_terminal_str(cause or "unknown")
    core._error(
        f"devices approve: could not read Syncthing's configuration ({out_s}/{cause_s}). "
        "Nothing was changed. Run: mm diag"
    )


def _stop_partial(
    candidate: str,
    *,
    written: str,
    outcome: str,
    cause: str | None,
) -> None:
    """Exit 1 after an approval write failed, naming what Syncthing confirmed.

    ``written`` is the last confirmed step: ``"nothing"``, ``"added"`` or
    ``"introducer"``. A write whose response was lost may still have applied, so
    the failed step is reported as unconfirmed, never as not done; a rerun reads
    Syncthing's state and finishes either way.
    """
    id_s = safe_terminal_str(candidate)
    out_s = safe_terminal_str(outcome)
    cause_s = safe_terminal_str(cause or "unknown")
    if written == "nothing":
        prefix = (
            f"devices approve: Syncthing did not confirm adding {id_s} "
            f"({out_s}/{cause_s}), so it may or may not have added it."
        )
    elif written == "added":
        prefix = (
            f"devices approve: Syncthing added {id_s} but did not confirm marking it as "
            f"introducer or sharing the fleet folder with it ({out_s}/{cause_s})."
        )
    elif written == "introducer":
        prefix = (
            f"devices approve: Syncthing added {id_s} and marked it as introducer, "
            f"but did not confirm sharing the fleet folder with it ({out_s}/{cause_s})."
        )
    else:
        raise ValueError(f"unknown approval step {written!r}")
    if outcome in ("not-found", "unreachable"):
        core._error(
            f"{prefix} Start Syncthing, then run 'mm devices approve {id_s}' "
            f"again to finish. Run: {core._syncthing_start_command()}"
        )
    core._error(
        f"{prefix} Once Syncthing accepts the change, run "
        f"'mm devices approve {id_s}' again to finish. Run: mm diag"
    )


def _looks_like_full_device_id_attempt(candidate: str) -> bool:
    """Return True when the input has full 56-char / 8-group Syncthing ID shape."""
    if syncthing.is_device_id(candidate):
        return True
    compact = candidate.replace("-", "")
    if len(compact) == 56:
        return True
    parts = candidate.split("-")
    return len(parts) == 8 and all(len(part) == 7 for part in parts)


_FULL_ID_IN_PENDING_NAME = re.compile(
    r"[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}|[A-Z2-7]{56}", re.ASCII | re.IGNORECASE
)


def _display_pending_name(raw_name: object, device_id: str, *, short_ids_only: bool = False) -> str:
    """Return a sanitized display name for a pending or configured Syncthing device."""
    cleaned = safe_terminal_str(str(raw_name or "")).strip()
    if short_ids_only:
        cleaned = _FULL_ID_IN_PENDING_NAME.sub(
            lambda match: syncthing.short_device_id(match[0]), cleaned
        )
    return cleaned or syncthing.short_device_id(device_id)


@dataclass(frozen=True)
class _PendingCandidate:
    device_id: str
    short_id: str
    raw_name: str = field(repr=False)
    display_name: str
    is_retired: bool = False
    is_unpublished_fleet: bool = False


def _format_pending_candidate(item: _PendingCandidate) -> str:
    label = f"{item.display_name} ({item.short_id})"
    if item.is_retired:
        return f"{label} (retired)"
    if item.is_unpublished_fleet:
        return (
            f"{label} (fleet machine without a published Syncthing record; "
            "on it run 'mm update', then 'mm push')"
        )
    return label


def _match_pending_candidates(
    query: str,
    candidates: list[_PendingCandidate],
) -> list[_PendingCandidate]:
    """Match ``query`` by exact casefolded sanitized name union >=4-char base32 ID prefix."""
    trimmed = query.strip()
    if not trimmed:
        return []
    norm_name = _devices_module.normalize_device_name(trimmed)
    compact = trimmed.replace("-", "").upper()
    is_prefix = len(compact) >= 4 and all(
        ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in compact
    )
    matched: list[_PendingCandidate] = []
    for item in candidates:
        item_norm = _devices_module.normalize_device_name(item.raw_name)
        name_hit = bool(norm_name and item_norm and item_norm == norm_name)
        prefix_hit = bool(is_prefix and item.device_id.replace("-", "").upper().startswith(compact))
        if name_hit or prefix_hit:
            matched.append(item)
    return matched


@dataclass
class _ApproveContext:
    storage_root: Path | None = None
    retirements: _devices_module.FleetRetirementRecords | None = None
    fleet_records: _devices_module.FleetSyncthingRecords | None = None
    split: _devices_module.UnpublishedSplit | None = None


def _try_read_approve_fleet_context(*, include_fleet: bool) -> _ApproveContext:
    """Read authenticated retirement data independently of optional list labels."""
    context = _ApproveContext()
    try:
        cfg = _config_module.load_config()
        backend = get_backend(cfg)
        context.storage_root = backend.root
    except (ConfigError, OSError, StorageError):
        return context
    try:
        passphrase = crypto.get_passphrase(non_interactive=True)
    except Exception:
        # The optional probe also falls back on backend-specific keyring failures.
        return context
    if not passphrase:
        return context
    try:
        fetch = crypto.fetch_crypto_init(backend)
        if (
            fetch.status != "ok"
            or fetch.root_salt is None
            or fetch.argon2_memory_kb is None
            or fetch.keycheck_blob is None
        ):
            return context
        memory_kb = fetch.argon2_memory_kb
        crypto.set_crypto_session(fetch.root_salt, memory_kb)
        master_key = crypto.load_master_key(passphrase, fetch.root_salt, memory_kb)
        crypto.verify_passphrase(master_key, fetch.keycheck_blob)
    except (ConfigError, CryptoError, OSError, StorageError):
        return context
    try:
        context.retirements = _devices_module.read_retirement_records(
            backend, passphrase, memory_kb
        )
    except (OSError, StorageError):
        pass
    if include_fleet:
        device = cfg.get("device")
        own_mm_id = device.get("id") if isinstance(device, dict) else None
        if not isinstance(own_mm_id, str) or not own_mm_id:
            return context
        try:
            records = _devices_module.read_fleet_records(
                backend, passphrase, memory_kb, own_device_id=own_mm_id, strict=True
            )
            inventory = records.inventory or _devices_module.inventory_devices(backend)
            published_ids = _devices_module.published_record_mm_ids(backend, records)
            context.split = _devices_module.split_unpublished(
                inventory, published_ids, own_device_id=own_mm_id
            )
            context.fleet_records = records
        except (OSError, StorageError):
            pass
    return context


def _load_recorded_syncthing_id_or_stop() -> str:
    try:
        recorded = _config_module.load_syncthing_record()
    except ConfigError as e:
        core._error(
            f"devices approve: {safe_terminal_str(e)} Nothing was changed. "
            "Fix or remove that file, then run 'mm devices approve <device-id>' again."
        )
    if recorded is None:
        _stop_not_managed()
    return recorded


def approve(device_id: str | None = None) -> None:
    """Approve a pending joining device in an mm-managed Syncthing instance."""
    if device_id is not None:
        trimmed = device_id.strip()
        if not trimmed:
            _stop_bad_device_id(device_id, "device name or ID cannot be empty")
        candidate = core._normalize_device_id(trimmed)
        if _looks_like_full_device_id_attempt(candidate):
            why = core._device_id_problem(candidate, None)
            if why is not None:
                _stop_bad_device_id(trimmed, why)
            recorded = _load_recorded_syncthing_id_or_stop()
            why_own = core._device_id_problem(candidate, recorded)
            if why_own is not None:
                _stop_bad_device_id(trimmed, why_own)

            writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(device_id=recorded))
            state = writer.approval_state(_config_module.SYNCTHING_FOLDER_ID, candidate)
            if state.outcome != "ok":
                _stop_unchanged(state.outcome, state.cause, candidate)

            if state.configured and state.shared:
                core._print_outcome(
                    outputstyle._outcome_line(
                        "approve",
                        f"{safe_terminal_str(candidate)} is already approved: Syncthing on this "
                        "machine already shares the fleet folder with it",
                    )
                )
                return

            if state.folder != "ok":
                core._error(
                    "devices approve: Syncthing on this machine has no fleet folder "
                    f"('{_config_module.SYNCTHING_FOLDER_ID}') with mm's folder options, so "
                    "there is nothing to share. Nothing was changed. Run: mm diag"
                )

            pending = writer.pending_devices()
            if pending.outcome != "ok":
                _stop_unchanged(pending.outcome, pending.cause, candidate)

            if any(d.device_id == candidate for d in pending.devices):
                add_res = writer.add_device(candidate)
                if add_res.outcome in ("not-found", "refused"):
                    _stop_unchanged(add_res.outcome, add_res.cause, candidate)
                if add_res.outcome not in ("ok", "unchanged"):
                    _stop_partial(
                        candidate,
                        written="nothing",
                        outcome=add_res.outcome,
                        cause=add_res.cause,
                    )
                intro_res = writer.mark_introducer(candidate)
                if intro_res.outcome not in ("ok", "unchanged"):
                    _stop_partial(
                        candidate,
                        written="added",
                        outcome=intro_res.outcome,
                        cause=intro_res.cause,
                    )
                share_res = writer.share_folder(_config_module.SYNCTHING_FOLDER_ID, candidate)
                if share_res.outcome not in ("ok", "unchanged"):
                    _stop_partial(
                        candidate,
                        written="introducer",
                        outcome=share_res.outcome,
                        cause=share_res.cause,
                    )
                core._print_outcome(
                    outputstyle._outcome_line(
                        "approve",
                        f"approved {safe_terminal_str(candidate)}: Syncthing here shares the "
                        "fleet folder with it and marks it as introducer",
                    )
                    + f" On the new machine, {outputstyle._fix_line('mm init')}"
                )
                return

            if state.configured and not state.shared and state.writer_shape:
                intro_res = writer.mark_introducer(candidate)
                if intro_res.outcome not in ("ok", "unchanged"):
                    _stop_partial(
                        candidate,
                        written="added",
                        outcome=intro_res.outcome,
                        cause=intro_res.cause,
                    )
                share_res = writer.share_folder(_config_module.SYNCTHING_FOLDER_ID, candidate)
                if share_res.outcome not in ("ok", "unchanged"):
                    _stop_partial(
                        candidate,
                        written="introducer",
                        outcome=share_res.outcome,
                        cause=share_res.cause,
                    )
                if intro_res.outcome == "unchanged":
                    core._print_outcome(
                        outputstyle._outcome_line(
                            "approve",
                            f"finished approving {safe_terminal_str(candidate)}: Syncthing here "
                            "now shares the fleet folder with it (it was already added and "
                            "marked as introducer)",
                        )
                        + f" On the new machine, {outputstyle._fix_line('mm init')}"
                    )
                else:
                    core._print_outcome(
                        outputstyle._outcome_line(
                            "approve",
                            f"finished approving {safe_terminal_str(candidate)}: Syncthing here "
                            "now marks it as introducer and shares the fleet folder with it "
                            "(it was already added)",
                        )
                        + f" On the new machine, {outputstyle._fix_line('mm init')}"
                    )
                return

            if state.configured:
                core._error(
                    f"devices approve: {safe_terminal_str(candidate)} is paired with Syncthing "
                    "here but was not added by an unfinished 'mm devices approve' and is not a "
                    "pending device, so mm will not approve it. Nothing was changed. Share the "
                    "fleet folder with it in Syncthing's GUI as README "
                    f'"{core.SYNCTHING_GUIDE_HEADING}" describes.'
                )

            core._error(
                f"devices approve: {safe_terminal_str(candidate)} is not among Syncthing's "
                "pending devices on this machine. Nothing was changed. Check the ID 'mm init' "
                "printed on the new machine and that Syncthing runs there and has tried to "
                "connect (run 'mm init' there again), then run "
                "'mm devices approve <device-id>' again."
            )
            return

    # Interactive / discovery / name / short-ID prefix flow
    recorded = _load_recorded_syncthing_id_or_stop()
    is_tty = core._both_streams_are_terminals()
    if device_id is not None and not is_tty:
        core._error(
            "devices approve: approving by name or short ID needs a terminal. Nothing was changed.",
            fix="mm devices approve <device-id>",
        )

    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(device_id=recorded))
    snap = writer.mesh_snapshot(_config_module.SYNCTHING_FOLDER_ID)
    if snap.outcome != "ok":
        _stop_unchanged(snap.outcome, snap.cause, device_id or "pending devices")
    if not snap.folder_present or snap.folder_problems:
        core._error(
            "devices approve: Syncthing on this machine has no fleet folder "
            f"('{_config_module.SYNCTHING_FOLDER_ID}') with mm's folder options, so "
            "there is nothing to share. Nothing was changed. Run: mm diag"
        )
    context = _try_read_approve_fleet_context(include_fleet=device_id is None)
    if context.storage_root is not None and context.storage_root != snap.folder_path:
        core._error(
            "devices approve: the Syncthing fleet folder does not match this machine's "
            "Mind Meld storage path. Nothing was changed.",
            fix="mm diag",
        )
    if device_id is None and context.fleet_records is None:
        console.print(
            "  Note: could not read fleet records, so the list below may include fleet machines."
        )

    vouched_map = context.fleet_records.vouched if context.fleet_records is not None else {}
    retired_by_st = context.retirements.by_syncthing_id if context.retirements is not None else {}
    unpub_by_norm = context.split.unpublished_by_norm_name if context.split is not None else {}

    unvouched_candidates: list[_PendingCandidate] = []
    vouched_knockers: list[_PendingCandidate] = []
    for pending_id, name in snap.pending_devices.items():
        short_id = syncthing.short_device_id(pending_id)
        if pending_id in vouched_map:
            _, reg_name = vouched_map[pending_id]
            vouched_knockers.append(
                _PendingCandidate(
                    pending_id,
                    short_id,
                    name,
                    _display_pending_name(name or reg_name, pending_id, short_ids_only=not is_tty),
                )
            )
            continue
        is_retired = pending_id in retired_by_st
        unvouched_candidates.append(
            _PendingCandidate(
                pending_id,
                short_id,
                name,
                _display_pending_name(name, pending_id, short_ids_only=not is_tty),
                is_retired=is_retired,
                is_unpublished_fleet=(
                    not is_retired and _devices_module.normalize_device_name(name) in unpub_by_norm
                ),
            )
        )

    target_candidate: _PendingCandidate | None = None

    if device_id is not None:
        trimmed = device_id.strip()
        matches = _match_pending_candidates(trimmed, unvouched_candidates)
        if not matches:
            # Check configured devices read-only (X4)
            configured_list = [
                _PendingCandidate(
                    device_id=cid,
                    short_id=syncthing.short_device_id(cid),
                    raw_name=cdev.name,
                    display_name=_display_pending_name(cdev.name, cid),
                )
                for cid, cdev in sorted(snap.devices.items())
            ]
            cfg_matches = _match_pending_candidates(trimmed, configured_list)
            if len(cfg_matches) == 1:
                cm = cfg_matches[0]
                cdev = snap.devices[cm.device_id]
                if cm.device_id in snap.folder_device_ids:
                    core._print_outcome(
                        outputstyle._outcome_line(
                            "approve",
                            f"{safe_terminal_str(cm.display_name)} ({cm.short_id}) is already "
                            "approved: Syncthing on this machine already shares the fleet folder "
                            "with it",
                        )
                    )
                    return
                if cdev.writer_shape and not cdev.paused:
                    core._error(
                        f"devices approve: {safe_terminal_str(cm.display_name)} ({cm.short_id}) "
                        "was already added by an unfinished approval; finishing it requires the "
                        "full device ID. Nothing was changed.",
                        fix=f"mm devices approve {cm.device_id}",
                    )
            if not unvouched_candidates:
                core._error(
                    f"devices approve: '{safe_terminal_str(trimmed)}' is not among Syncthing's "
                    "pending devices on this machine (no devices are currently asking to join). "
                    "Nothing was changed. On the new machine, run 'mm init' and leave it waiting.",
                    fix="mm devices approve",
                )
            console.print("Pending devices asking to join:")
            for item in unvouched_candidates:
                console.print(f"  {safe_str(_format_pending_candidate(item))}", soft_wrap=True)
            compact = trimmed.replace("-", "")
            if len(compact) < 4:
                core._error(
                    f"devices approve: '{safe_terminal_str(trimmed)}' did not match any pending "
                    "device name (short-ID prefixes need at least 4 characters). "
                    "Nothing was changed.",
                    fix="mm devices approve <longer short-ID prefix>",
                )
            core._error(
                f"devices approve: '{safe_terminal_str(trimmed)}' is not among Syncthing's "
                "pending devices on this machine. Nothing was changed.",
                fix="mm devices approve <longer short-ID prefix>",
            )
        if len(matches) > 1:
            console.print("Matching pending devices:")
            for item in matches:
                console.print(f"  {safe_str(_format_pending_candidate(item))}", soft_wrap=True)
            core._error(
                f"devices approve: '{safe_terminal_str(trimmed)}' matches {len(matches)} "
                "pending devices. Nothing was changed.",
                fix="mm devices approve <longer short-ID prefix>",
            )
        chosen = matches[0]
        if chosen.is_retired:
            core._error(
                f"devices approve: {safe_terminal_str(chosen.display_name)} ({chosen.short_id}) "
                "is a retired fleet device. Nothing was changed. To re-approve it intentionally, "
                "pass its full Syncthing device ID.",
                fix=f"mm devices approve {chosen.device_id}",
            )
        if not core.ask_yes_no(
            f"Approve {safe_terminal_str(chosen.display_name)} ({chosen.short_id})?",
            default=True,
        ):
            core._print_outcome(outputstyle._outcome_line("approve", "Nothing was changed"))
            return
        target_candidate = chosen
    else:
        for vk in vouched_knockers:
            console.print(
                f"  {safe_str(vk.display_name)} ({safe_str(vk.short_id)}) is asking to join "
                "and a fleet record vouches for it; the next push or pull pairs it. "
                f"{outputstyle._fix_line('mm pull')}",
                soft_wrap=True,
            )
        for cid, cdev in sorted(snap.devices.items()):
            if (
                is_tty
                and cdev.writer_shape
                and not cdev.paused
                and cid not in snap.folder_device_ids
                and cid not in snap.other_folder_device_ids
                and cid not in retired_by_st
            ):
                cname = _display_pending_name(cdev.name, cid)
                cshort = syncthing.short_device_id(cid)
                console.print(
                    f"  {safe_str(cname)} ({safe_str(cshort)}) may have an unfinished approval. "
                    f"{outputstyle._fix_line(f'mm devices approve {cid}')}",
                    soft_wrap=True,
                )
        if not unvouched_candidates:
            core._print_outcome(
                outputstyle._outcome_line(
                    "approve", "no pending devices are asking to join. Nothing was changed"
                )
            )
            return

        console.print("Pending devices asking to join:")
        for item in unvouched_candidates:
            console.print(f"  {safe_str(_format_pending_candidate(item))}", soft_wrap=True)

        if not is_tty:
            core._error(
                "devices approve: approving by name or short ID needs a terminal. "
                "Nothing was changed.",
                fix="mm devices approve <device-id>",
            )

        if len(unvouched_candidates) == 1:
            only = unvouched_candidates[0]
            if only.is_retired:
                core._error(
                    f"devices approve: {safe_terminal_str(only.display_name)} ({only.short_id}) "
                    "is a retired fleet device. Nothing was changed. To re-approve it "
                    "intentionally, pass its full Syncthing device ID.",
                    fix=f"mm devices approve {only.device_id}",
                )
            if not core.ask_yes_no(
                f"Approve {safe_terminal_str(only.display_name)} ({only.short_id})?",
                default=True,
            ):
                core._print_outcome(outputstyle._outcome_line("approve", "Nothing was changed"))
                return
            target_candidate = only
        else:
            prompt_text = "Which device do you want to approve? (name or short ID, Enter = none)"
            for attempt_idx in range(2):
                raw = typer.prompt(prompt_text, default="", show_default=False)
                choice = raw.strip()
                if not choice:
                    core._print_outcome(outputstyle._outcome_line("approve", "Nothing was changed"))
                    return
                matches = _match_pending_candidates(choice, unvouched_candidates)
                if len(matches) == 1:
                    chosen = matches[0]
                    if chosen.is_retired:
                        core._error(
                            f"devices approve: {safe_terminal_str(chosen.display_name)} "
                            f"({chosen.short_id}) is a retired fleet device. Nothing was changed. "
                            "To re-approve it intentionally, pass its full Syncthing device ID.",
                            fix=f"mm devices approve {chosen.device_id}",
                        )
                    target_candidate = chosen
                    break
                if attempt_idx == 0:
                    console.print(
                        "  Enter one listed device name or short ID (at least 4 characters), "
                        "or press Enter to cancel."
                    )
                else:
                    core._error(
                        f"devices approve: '{safe_terminal_str(choice)}' did not uniquely match "
                        "a pending device. Nothing was changed.",
                        fix="mm devices approve",
                    )

    assert target_candidate is not None
    full_id = target_candidate.device_id
    if why := core._device_id_problem(full_id, recorded):
        _stop_bad_device_id(full_id, why)
    # The terminal may have been waiting for the owner while the pending
    # request, retirement record or folder configuration changed.
    latest = writer.mesh_snapshot(_config_module.SYNCTHING_FOLDER_ID)
    if latest.outcome != "ok":
        _stop_unchanged(latest.outcome, latest.cause, full_id)
    approved_path = context.storage_root or snap.folder_path
    if latest.folder_problems or latest.folder_path is None or latest.folder_path != approved_path:
        core._error(
            "devices approve: the Syncthing fleet folder changed. Nothing was changed.",
            fix="mm diag",
        )
    if full_id in latest.devices:
        if full_id in latest.folder_device_ids:
            core._print_outcome(
                outputstyle._outcome_line(
                    "approve",
                    f"{safe_terminal_str(target_candidate.display_name)} "
                    f"({target_candidate.short_id}) is already approved",
                )
            )
            return
        core._error(
            "devices approve: the device is already configured. Finishing approval requires "
            "the full Syncthing device ID. Nothing was changed.",
            fix=f"mm devices approve {full_id}",
        )
    if full_id not in latest.pending_devices:
        core._error(
            "devices approve: that Syncthing device is no longer pending. Nothing was changed.",
            fix="mm devices approve",
        )
    current = _try_read_approve_fleet_context(include_fleet=False)
    if current.retirements is not None and full_id in current.retirements.by_syncthing_id:
        core._error(
            "devices approve: that Syncthing device has a retirement record. "
            "Nothing was changed. To re-approve it intentionally, pass its full ID.",
            fix=f"mm devices approve {full_id}",
        )
    add_res = writer.add_device(full_id)
    if add_res.outcome == "unchanged":
        core._error(
            "devices approve: the device was configured during approval. Nothing was changed.",
            fix=f"mm devices approve {full_id}",
        )
    if add_res.outcome in ("not-found", "refused"):
        _stop_unchanged(add_res.outcome, add_res.cause, full_id)
    if add_res.outcome not in ("ok", "unchanged"):
        _stop_partial(
            full_id,
            written="nothing",
            outcome=add_res.outcome,
            cause=add_res.cause,
        )
    intro_res = writer.mark_introducer(full_id, require_writer_shape=True)
    if intro_res.outcome not in ("ok", "unchanged"):
        _stop_partial(
            full_id,
            written="added",
            outcome=intro_res.outcome,
            cause=intro_res.cause,
        )
    share_res = writer.share_folder(
        _config_module.SYNCTHING_FOLDER_ID, full_id, expected_path=approved_path
    )
    if share_res.outcome not in ("ok", "unchanged"):
        _stop_partial(
            full_id,
            written="introducer",
            outcome=share_res.outcome,
            cause=share_res.cause,
        )
    core._print_outcome(
        outputstyle._outcome_line(
            "approve",
            f"approved {safe_terminal_str(target_candidate.display_name)} "
            f"({safe_terminal_str(full_id)}): Syncthing here shares the fleet folder with it "
            "and marks it as introducer",
        )
        + f" On the new machine, {outputstyle._fix_line('mm init')}"
    )


def _retire_consequence_clause(
    syncthing_id: str | None,
    retired_mm_id: str,
    fleet_records: _devices_module.FleetSyncthingRecords,
    valid_by_id: dict[str, dict[str, Any]],
) -> str:
    """Return the three-branch Syncthing consequence clause for ``mm devices retire``."""
    if syncthing_id is None:
        return (
            "mm has no Syncthing ID on record for it, so no machine will remove it from "
            "Syncthing automatically; remove it in Syncthing's web UI wherever Syncthing "
            "still lists it"
        )
    short_id = syncthing.short_device_id(syncthing_id)
    other_holders = [
        mm_id
        for mm_id, st_id in sorted(fleet_records.by_mm_id.items())
        if st_id == syncthing_id and mm_id != retired_mm_id and mm_id in valid_by_id
    ]
    if other_holders:
        other_id = other_holders[0]
        raw_other_name = valid_by_id[other_id].get("device_name") or other_id
        other_name = safe_terminal_str(str(raw_other_name)).strip() or other_id
        return (
            f"Syncthing device {safe_terminal_str(short_id)} stays because registration "
            f"{other_name} ({safe_terminal_str(other_id)}) still uses it"
        )
    return (
        f"each machine where mm manages Syncthing removes {safe_terminal_str(short_id)} "
        "from Syncthing at its first push or pull after the device stops connecting "
        "(stopping mm alone does not stop Syncthing there, and a machine that is still "
        "running re-registers on its next push); on owner-managed machines, remove it in "
        "Syncthing's web UI"
    )


def retire(target: str, *, yes: bool = False) -> None:
    """Retire a fleet device by name or mm device ID under the mm lock."""
    trimmed = target.strip()
    if not trimmed:
        core._error(
            "devices retire: device name or ID cannot be empty. Nothing was changed.",
            fix="mm devices",
        )

    config = core._get_config(read_only=True)
    own_id = config["device"]["id"]
    backend = get_backend(config)
    if trimmed == own_id:
        core._error(
            "devices retire: cannot retire this machine from itself. Nothing was changed. "
            "Retire it from another fleet machine.",
            fix="mm devices",
        )

    try:
        lockfile.acquire_lock()
    except LockError as e:
        core._error(str(e))

    try:
        is_tty = core._both_streams_are_terminals()
        try:
            passphrase = core._get_passphrase_or_exit(non_interactive=not is_tty)
            memory_kb = core._init_crypto_session(backend, passphrase, config, read_only=True)
            fleet_records = _devices_module.read_fleet_records(
                backend, passphrase, memory_kb, own_device_id=own_id, strict=True
            )
            retirements = _devices_module.read_retirement_records(
                backend, passphrase, memory_kb, strict=True
            )
        except (CryptoError, OSError, StorageError) as exc:
            core._error(
                f"devices retire: could not verify the fleet records ({safe_terminal_str(exc)}). "
                "Nothing was changed.",
                fix="mm diag",
            )
        inventory = fleet_records.inventory
        assert inventory is not None  # strict reads always include the registry

        valid_by_id: dict[str, dict[str, Any]] = {d["device_id"]: d for d in inventory.valid}
        dropped_ids: set[str] = {
            did for key, _ in inventory.dropped if (did := parse_device_key(key)) is not None
        }
        vanished_ids: set[str] = set(inventory.vanished)
        all_registry_ids: set[str] = set(valid_by_id) | dropped_ids | vanished_ids

        norm_target = _devices_module.normalize_device_name(trimmed)
        name_matches = [
            d
            for d in sorted(inventory.valid, key=lambda item: item["device_id"])
            if _devices_module.normalize_device_name(d.get("device_name")) == norm_target
        ]
        if (
            trimmed not in all_registry_ids
            and trimmed not in retirements.records
            and name_matches
            and all(d["device_id"] == own_id for d in name_matches)
        ):
            core._error(
                "devices retire: cannot retire this machine from itself. Nothing was changed. "
                "Retire it from another fleet machine.",
                fix="mm devices",
            )

        peer_name_matches = [d for d in name_matches if d["device_id"] != own_id]
        retired_name_matches = [
            rec
            for _, rec in sorted(retirements.records.items())
            if rec.mm_device_id != own_id
            and _devices_module.normalize_device_name(rec.device_name) == norm_target
        ]

        is_exact_id = (trimmed in all_registry_ids) or (trimmed in retirements.records)
        if (yes or not is_tty) and not is_exact_id:
            if peer_name_matches or retired_name_matches:
                console.print(f"Matching devices for '{safe_str(trimmed)}':")
                for entry in peer_name_matches:
                    did = entry["device_id"]
                    dname = safe_terminal_str(str(entry.get("device_name") or did)).strip() or did
                    console.print(f"  {safe_str(dname)} ({safe_str(did)})")
                for rec in retired_name_matches:
                    if rec.mm_device_id not in valid_by_id:
                        console.print(
                            f"  {safe_str(rec.device_name)} ({safe_str(rec.mm_device_id)}) "
                            "(retirement record)"
                        )
                core._error(
                    f"devices retire: '{safe_terminal_str(trimmed)}' is a device name; --yes "
                    "and non-terminal runs require an exact device ID from 'mm devices'. "
                    "Nothing was changed.",
                    fix="mm devices retire <id> --yes",
                )
            core._error(
                f"devices retire: unknown device '{safe_terminal_str(trimmed)}'. "
                "Nothing was changed.",
                fix="mm devices",
            )

        target_id = ""
        target_name = ""
        already_retired_only = False

        if is_exact_id:
            target_id = trimmed
            if target_id in all_registry_ids:
                reg_entry = valid_by_id.get(target_id)
                raw_name = (
                    (reg_entry.get("device_name") if reg_entry is not None else None)
                    or (
                        retirements.records[target_id].device_name
                        if target_id in retirements.records
                        else None
                    )
                    or target_id
                )
                target_name = safe_terminal_str(str(raw_name)).strip() or target_id
                already_retired_only = False
            else:
                rec = retirements.records[target_id]
                target_name = safe_terminal_str(rec.device_name).strip() or target_id
                already_retired_only = True
        else:
            if len(peer_name_matches) == 1:
                reg_entry = peer_name_matches[0]
                target_id = reg_entry["device_id"]
                target_name = (
                    safe_terminal_str(str(reg_entry.get("device_name") or target_id)).strip()
                    or target_id
                )
            elif len(peer_name_matches) > 1:
                published_ids = _devices_module.published_record_mm_ids(backend, fleet_records)
                console.print(f"Multiple registered devices are named '{safe_str(trimmed)}':")
                by_choice_id: dict[str, dict[str, Any]] = {}
                for entry in peer_name_matches:
                    did = entry["device_id"]
                    by_choice_id[did] = entry
                    dname = safe_terminal_str(str(entry.get("device_name") or did)).strip() or did
                    reg_s = safe_terminal_str(str(entry.get("registered") or "—"))
                    seen_s = safe_terminal_str(str(entry.get("last_seen") or "never pushed"))
                    ver_s = safe_terminal_str(str(entry.get("last_seen_version") or "—"))
                    pub_s = (
                        "Syncthing ID published"
                        if did in published_ids
                        else "no Syncthing ID published"
                    )
                    console.print(
                        f"  {safe_str(dname)} ({safe_str(did)}) — registered {safe_str(reg_s)}, "
                        f"last push {safe_str(seen_s)}, version {safe_str(ver_s)}, {pub_s}",
                        soft_wrap=True,
                    )
                prompt_text = "Which device ID do you want to retire? (Enter = none)"
                for attempt_idx in range(2):
                    raw = typer.prompt(prompt_text, default="", show_default=False)
                    choice = raw.strip()
                    if not choice:
                        core._print_outcome(
                            outputstyle._outcome_line("retire", "Nothing was changed")
                        )
                        return
                    if choice in by_choice_id:
                        chosen_entry = by_choice_id[choice]
                        target_id = chosen_entry["device_id"]
                        target_name = (
                            safe_terminal_str(
                                str(chosen_entry.get("device_name") or target_id)
                            ).strip()
                            or target_id
                        )
                        break
                    if attempt_idx == 0:
                        console.print(
                            "  Enter one of the listed device IDs, or press Enter to cancel."
                        )
                    else:
                        core._error(
                            f"devices retire: '{safe_terminal_str(choice)}' is not one of the "
                            "listed device IDs. Nothing was changed.",
                            fix="mm devices",
                        )
            elif retired_name_matches:
                if len(retired_name_matches) > 1:
                    for rec in retired_name_matches:
                        console.print(
                            f"  {safe_str(rec.device_name)} ({safe_str(rec.mm_device_id)})"
                        )
                    core._error(
                        f"devices retire: '{safe_terminal_str(trimmed)}' matches several "
                        "retirement records. Choose an exact device ID. Nothing was changed.",
                        fix="mm devices retire <id>",
                    )
                rec = retired_name_matches[0]
                target_id = rec.mm_device_id
                current_name = valid_by_id.get(target_id, {}).get("device_name")
                target_name = (
                    safe_terminal_str(current_name or rec.device_name).strip() or target_id
                )
                already_retired_only = target_id not in all_registry_ids
            else:
                core._error(
                    f"devices retire: unknown device '{safe_terminal_str(trimmed)}'. "
                    "Nothing was changed.",
                    fix="mm devices",
                )

        if not is_tty and not yes:
            core._error(
                "devices retire: confirming needs a terminal. Nothing was changed.",
                fix=_devices_module.retire_command(target_id, yes=True),
            )

        if already_retired_only:
            try:
                # A prior run can unlink the registration and fail its directory flush.
                backend.flush_namespace("devices/")
            except (OSError, StorageError):
                core._error(
                    "devices retire: could not confirm the device registry is durable.",
                    fix=_devices_module.retire_command(target_id, yes=yes),
                )
            rec = retirements.records[target_id]
            consequence = _retire_consequence_clause(
                rec.syncthing_device_id,
                target_id,
                fleet_records,
                valid_by_id,
            )
            core._print_outcome(
                outputstyle._outcome_line(
                    "retire",
                    f"{safe_terminal_str(target_name)} ({safe_terminal_str(target_id)}) "
                    f"is already retired; {consequence}",
                )
            )
            return

        if target_id in retirements.newer_format:
            core._error(
                "devices retire: this machine's retirement record needs a newer mm. "
                "Nothing was changed.",
                fix="mm update",
            )
        if target_id in retirements.unreadable or target_id in retirements.vanished:
            key = safe_terminal_str(retired_record_key(target_id))
            core._error(
                f"devices retire: could not verify {key}. "
                "Nothing was changed. Wait for sync or check the retirement file.",
                fix=_devices_module.retire_command(target_id, yes=yes),
            )

        recorded_id = _devices_module.read_single_syncthing_record(
            backend, target_id, passphrase, memory_kb
        )
        prior_retirement = retirements.records.get(target_id)
        syncthing_id = recorded_id or (prior_retirement and prior_retirement.syncthing_device_id)

        old_gc_peers = [
            safe_terminal_str(pid)
            for pid in sorted((dropped_ids | vanished_ids) - {own_id, target_id})
        ]
        old_release_peers: list[str] = []
        min_gc_ver = Version("1.7.1")
        curr_ver = Version(core.__version__)
        for peer in sorted(inventory.valid, key=lambda item: item["device_id"]):
            pid = peer["device_id"]
            if pid in (own_id, target_id):
                continue
            pname = safe_terminal_str(str(peer.get("device_name") or pid)).strip() or pid
            plabel = f"{pname} ({safe_terminal_str(pid)})"
            raw_ver = peer.get("last_seen_version")
            if not isinstance(raw_ver, str) or not raw_ver.strip():
                old_gc_peers.append(plabel)
                continue
            try:
                parsed_ver = Version(raw_ver.strip())
            except InvalidVersion:
                old_gc_peers.append(plabel)
                continue
            if parsed_ver <= min_gc_ver:
                old_gc_peers.append(plabel)
            elif parsed_ver < curr_ver:
                old_release_peers.append(plabel)

        if old_gc_peers and yes:
            core._error(
                f"devices retire: fleet machine(s) {', '.join(old_gc_peers)} last pushed from "
                "mm 1.7.1 or older (or have never pushed); retiring with --yes is refused until "
                "they are updated so 'mm gc' there knows this device was retired. "
                "Nothing was changed.",
                fix="mm update",
            )
        if old_gc_peers:
            core._warning(
                f"fleet machine(s) {', '.join(old_gc_peers)} last pushed from mm 1.7.1 or older "
                "(or have never pushed); update mm there before running 'mm gc' on them.",
                fix="mm update",
            )
        if old_release_peers:
            core._warning(
                f"fleet machine(s) {', '.join(old_release_peers)} last pushed from an older mm "
                "release and will not auto-remove retired Syncthing devices until updated.",
                fix="mm update",
            )

        consequence = _retire_consequence_clause(
            syncthing_id, target_id, fleet_records, valid_by_id
        )
        if syncthing_id is None:
            unpublished = _devices_module.split_unpublished(
                inventory, _devices_module.published_record_mm_ids(backend, fleet_records)
            )
            if any(entry.old_id == target_id for entry in unpublished.superseded):
                consequence += (
                    "; if the machine rejoined under the same Syncthing ID, keep that device"
                )
        console.print(
            f"  Before retiring {safe_str(target_name)} ({safe_str(target_id)}), pull on a "
            "machine with the same home if it had unpulled project memory. Shut it down "
            "or stop Syncthing there before retiring it.",
            soft_wrap=True,
        )
        if not yes:
            console.print(f"  After retirement, {safe_str(consequence)}.", soft_wrap=True)
            if not core.ask_yes_no(
                f"Retire {safe_terminal_str(target_name)} ({safe_terminal_str(target_id)})?",
                default=False,
            ):
                core._print_outcome(outputstyle._outcome_line("retire", "Nothing was changed"))
                return

            try:
                current = _devices_module.read_retirement_records(
                    backend, passphrase, memory_kb, strict=True
                )
                backend.check_namespace("syncthing/")
                current_id = _devices_module.read_single_syncthing_record(
                    backend, target_id, passphrase, memory_kb
                )
            except (OSError, StorageError):
                core._error(
                    "devices retire: could not verify fleet records after confirmation. "
                    "Nothing was changed.",
                    fix="mm diag",
                )
            if (
                current_id != recorded_id
                or current.records.get(target_id) != prior_retirement
                or target_id in current.unreadable
                or target_id in current.newer_format
                or target_id in current.vanished
            ):
                core._error(
                    "devices retire: the retirement target changed during confirmation. "
                    "Nothing was changed.",
                    fix=_devices_module.retire_command(target_id),
                )

        try:
            _devices_module.write_retirement_record(
                backend,
                target_id,
                target_name,
                syncthing_id,
                passphrase,
                memory_kb,
            )
        except (OSError, StorageError) as exc:
            cause = attemptlog.storage_errno(exc) or os_error_cause(exc)
            core._error(
                f"devices retire: could not write retirement record for "
                f"{safe_terminal_str(target_name)} ({safe_terminal_str(target_id)}) "
                f"({safe_terminal_str(cause)}); nothing was deleted.",
                fix=_devices_module.retire_command(target_id, yes=yes),
            )

        try:
            backend.delete(syncthing_record_key(target_id), durable_flat=True)
        except (OSError, StorageError) as exc:
            cause = attemptlog.storage_errno(exc) or os_error_cause(exc)
            core._error(
                f"devices retire: wrote the retirement record for "
                f"{safe_terminal_str(target_name)} ({safe_terminal_str(target_id)}), "
                f"but could not remove its Syncthing record ({safe_terminal_str(cause)}).",
                fix=_devices_module.retire_command(target_id, yes=yes),
            )

        try:
            backend.delete(device_key(target_id), durable_flat=True)
        except (OSError, StorageError) as exc:
            cause = attemptlog.storage_errno(exc) or os_error_cause(exc)
            core._error(
                f"devices retire: wrote the retirement record and removed the Syncthing "
                f"record for {safe_terminal_str(target_name)} ({safe_terminal_str(target_id)}), "
                f"but could not remove its device entry ({safe_terminal_str(cause)}).",
                fix=_devices_module.retire_command(target_id, yes=yes),
            )

        core._print_outcome(
            outputstyle._outcome_line(
                "retire",
                f"retired {safe_terminal_str(target_name)} ({safe_terminal_str(target_id)}); "
                f"{consequence}",
            )
        )
    finally:
        lockfile.release_lock()
