"""Implementation of `mm diff`."""

from __future__ import annotations

from pathlib import Path

from mind_meld import core, pullplan
from mind_meld.config import get_sources
from mind_meld.consoles import console
from mind_meld.errors import MindMeldError
from mind_meld.manifest import build_manifest_v2
from mind_meld.safety import safe_str
from mind_meld.storage import get_backend


def diff_cmd(
    from_device: str | None = None,
    source: str | None = None,
) -> None:
    """Compare this Mac's files with its last push (or with --from DEVICE's manifest)."""
    config = core._get_config(read_only=True)
    passphrase = core._get_passphrase_or_exit()
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)
    try:
        pending: list[str] = []
        memory_kb = core._init_crypto_session(
            backend, passphrase, config, read_only=True, pending=pending
        )
        for note in pending:
            console.print(safe_str(note))
    except MindMeldError as e:
        core._error(str(e))

    target_id = from_device or device_id
    target_display = safe_str(core.safety.safe_terminal_str(target_id))

    # Build local manifest (v2)
    sources_configs = get_sources(config)
    local_manifest = build_manifest_v2(device_id, device_name, sources_configs, max_file_size)

    diff_fetch = core._fetch_remote_manifest(backend, target_id, passphrase, memory_kb)
    if diff_fetch.status == "missing":
        console.print(
            f"[dim]No remote manifest for "
            f"{'device ' + target_display if from_device else 'this device'} yet.[/dim]"
        )
    elif diff_fetch.newer_version is not None:
        core._error(core._newer_format_message(diff_fetch.newer_version, "manifest"))
    elif diff_fetch.status == "corrupt":
        core._warning(
            f"remote manifest for "
            f"{'device ' + target_id if from_device else 'this device'} is "
            "corrupt — showing diff against empty remote.",
            fix="mm diag",
        )
    # diff_fetch.manifest is pre-normalized via load_manifest.
    remote_manifest = diff_fetch.manifest if diff_fetch.is_ok else None

    if remote_manifest is not None:
        remote_manifest = core._filter_disabled_sources(remote_manifest, [])
        # Same checkout probe as pull, so the diff predicts what pull applies.
        exclude_map, skip_prefixes = core._build_exclude_map(
            config,
            sources_configs,
            nested_roots=core._incoming_nested_roots(sources_configs, [remote_manifest]),
        )
        remote_manifest = core._filter_excluded_paths(
            remote_manifest,
            exclude_map,
            skip_prefixes,
            core._build_include_map(config, sources_configs),
        )

    remote_sources = remote_manifest.get("sources", {}) if remote_manifest else {}

    console.print(
        f"\n[bold]Diff against {'device ' + target_display if from_device else 'remote'}:[/bold]"
    )

    # Map source names → local base paths for pull-outcome prediction
    src_base_paths: dict[str, Path] = {
        s["name"]: Path(s["path"]).expanduser().resolve() for s in sources_configs
    }

    # `--from DEVICE` previews with that peer's ledger, as the pull itself would.
    diff_planner = pullplan.PullPlanner(core.load_ledger([target_id])[0]) if from_device else None
    any_changes = False
    for src_name, src_data, remote_src, diff in core.iter_source_diffs(
        local_manifest, remote_sources, source_filter=source, skip_unchanged=True
    ):
        remote_files = remote_src.get("files", {})

        any_changes = True
        console.print(f"\n  [bold]Source '{safe_str(src_name)}':[/bold]")
        console.print("  [dim](push direction: local → remote)[/dim]")
        for path in sorted(diff.new):
            console.print(f"    [green]+ push  [/green] {safe_str(path)}")
        for path in sorted(diff.modified):
            # Predict what pulling the REMOTE version would do to local. The
            # manifests were built from the same state, so local hash matches
            # src_data; remote hash is the divergent one.
            remote_info = remote_files.get(path, {})
            base_path = src_base_paths.get(src_name)
            if base_path is not None and remote_info:
                outcome = (
                    diff_planner.predict(
                        target_id, "", src_name, path, remote_info, base_path
                    ).outcome
                    if diff_planner is not None
                    else pullplan._predict_pull_outcome(path, remote_info, base_path)
                )
                console.print(
                    f"    [yellow]~ push  [/yellow] {safe_str(path)} (pull would: {outcome})"
                )
            else:
                console.print(f"    [yellow]~ push  [/yellow] {safe_str(path)}")
        for path in sorted(diff.deleted):
            console.print(f"    [red]- only-remote[/red] {safe_str(path)}")

    if not any_changes:
        console.print("[green]No differences.[/green]")
