"""`mm devices` and `mm devices approve`, independent of command registration.

`devices` lists the fleet's registered devices as a Rich table or as the stable
`--format=json` array consumed by `retro-fleet`. `approve` pairs a joining
machine on an mm-managed Syncthing instance by its exact pending device ID (or
finishes an earlier half-finished approval), marks it as introducer and shares
the fleet folder with it.

Raise surface for direct callers:
- `typer.Exit(1)`: a `core._error` refusal (`--format` invalid, malformed or
  own device ID, malformed `syncthing.toml`, Syncthing not managed by mm,
  Syncthing down or unreadable, missing/non-mm fleet folder, non-pending device
  that is not a half-finished approval, or a failed Syncthing write).
The `@devices_app` group callback and `@devices_app.command("approve")` shells
and their `--help` text stay in `cli.py`; this module never imports `cli`.
"""

from __future__ import annotations

import json

from rich.table import Table

from mind_meld import config as _config_module
from mind_meld import core, syncthing
from mind_meld.config import ConfigError
from mind_meld.consoles import console
from mind_meld.safety import safe_str, safe_terminal_str
from mind_meld.storage import get_backend


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
    introducer_done: bool,
    outcome: str,
    cause: str | None,
) -> None:
    """Exit 1 after the device is configured in Syncthing but approval is incomplete."""
    id_s = safe_terminal_str(candidate)
    out_s = safe_terminal_str(outcome)
    cause_s = safe_terminal_str(cause or "unknown")
    if introducer_done:
        prefix = (
            f"devices approve: Syncthing added {id_s} and marked it as introducer, "
            f"but did not share the fleet folder with it ({out_s}/{cause_s})."
        )
    else:
        prefix = (
            f"devices approve: Syncthing added {id_s} but did not mark it as "
            f"introducer or share the fleet folder ({out_s}/{cause_s})."
        )
    if outcome in ("not-found", "unreachable"):
        core._error(
            f"{prefix} Start Syncthing, then run 'mm devices approve {id_s}' "
            f"again to finish. Run: {core._syncthing_start_command()}"
        )
    core._error(
        f"{prefix} Once Syncthing accepts the change, run "
        f"'mm devices approve {id_s}' again to finish. Run: mm diag"
    )


def approve(device_id: str) -> None:
    """Approve a pending joining device in an mm-managed Syncthing instance."""
    trimmed = device_id.strip()
    candidate = core._normalize_device_id(trimmed)
    why = core._device_id_problem(candidate, None)
    if why is not None:
        _stop_bad_device_id(trimmed, why)

    try:
        recorded = _config_module.load_syncthing_record()
    except ConfigError as e:
        core._error(
            f"devices approve: {safe_terminal_str(e)} Nothing was changed. "
            "Fix or remove that file, then run 'mm devices approve <device-id>' again."
        )
    if recorded is None:
        _stop_not_managed()

    why_own = core._device_id_problem(candidate, recorded)
    if why_own is not None:
        _stop_bad_device_id(trimmed, why_own)

    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(device_id=recorded))
    state = writer.approval_state(_config_module.SYNCTHING_FOLDER_ID, candidate)
    if state.outcome != "ok":
        _stop_unchanged(state.outcome, state.cause, candidate)

    if state.configured and state.shared:
        console.print(
            f"{safe_str(candidate)} is already approved: Syncthing on this machine "
            "already shares the fleet folder with it.",
            highlight=False,
            soft_wrap=True,
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
        if add_res.outcome not in ("ok", "unchanged"):
            _stop_unchanged(add_res.outcome, add_res.cause, candidate)
        intro_res = writer.mark_introducer(candidate)
        if intro_res.outcome not in ("ok", "unchanged"):
            _stop_partial(
                candidate,
                introducer_done=False,
                outcome=intro_res.outcome,
                cause=intro_res.cause,
            )
        share_res = writer.share_folder(_config_module.SYNCTHING_FOLDER_ID, candidate)
        if share_res.outcome not in ("ok", "unchanged"):
            _stop_partial(
                candidate,
                introducer_done=True,
                outcome=share_res.outcome,
                cause=share_res.cause,
            )
        console.print(
            f"Approved {safe_str(candidate)}: Syncthing here shares the fleet folder "
            "with it and marks it as introducer. On the new machine, run: mm init",
            highlight=False,
            soft_wrap=True,
        )
        return

    if state.configured and not state.shared and state.writer_shape:
        intro_res = writer.mark_introducer(candidate)
        if intro_res.outcome not in ("ok", "unchanged"):
            _stop_partial(
                candidate,
                introducer_done=False,
                outcome=intro_res.outcome,
                cause=intro_res.cause,
            )
        share_res = writer.share_folder(_config_module.SYNCTHING_FOLDER_ID, candidate)
        if share_res.outcome not in ("ok", "unchanged"):
            _stop_partial(
                candidate,
                introducer_done=True,
                outcome=share_res.outcome,
                cause=share_res.cause,
            )
        if intro_res.outcome == "unchanged":
            console.print(
                f"Finished approving {safe_str(candidate)}: shared the fleet folder "
                "with it (it was already added and marked as introducer). "
                "On the new machine, run: mm init",
                highlight=False,
                soft_wrap=True,
            )
        else:
            console.print(
                f"Finished approving {safe_str(candidate)}: marked it as introducer "
                "and shared the fleet folder with it (it was already added). "
                "On the new machine, run: mm init",
                highlight=False,
                soft_wrap=True,
            )
        return

    if state.configured:
        core._error(
            f"devices approve: {safe_terminal_str(candidate)} is paired with Syncthing "
            "here but was not added by an unfinished 'mm devices approve' and is not a "
            "pending device, so mm will not approve it. Nothing was changed. Share the "
            f'fleet folder with it in Syncthing\'s GUI as README "{core.SYNCTHING_GUIDE_HEADING}" '
            "describes."
        )

    core._error(
        f"devices approve: {safe_terminal_str(candidate)} is not among Syncthing's "
        "pending devices on this machine. Nothing was changed. Check the ID 'mm init' "
        "printed on the new machine and that Syncthing runs there and has tried to "
        "connect (run 'mm init' there again), then run "
        "'mm devices approve <device-id>' again."
    )
