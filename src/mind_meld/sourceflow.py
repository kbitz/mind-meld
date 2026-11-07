"""The source commands' bodies and their source-only helpers, independent of command registration.

`mm sources` lists the configured sources and how many files each keeps.
`mm enable-source` and `mm disable-source` toggle one source on this device
(`[sync].disabled_sources`) and validate the name against the known set.
`mm reconfigure-sources` re-runs the picker over every known source and commits
the answers in one config patch.

Raise surface for direct callers:
- returns None on every success path, including the already-enabled and
  already-disabled no-ops.
- typer.Exit(1): a core._error refusal (config problem, unknown or retired
  source name), or a reconfigure abort (Ctrl-C or typer.Abort mid-prompt) with
  nothing written.
- anything else propagates uncaught. A failed config write prints no success
  line and records nothing as seen.
A nonzero exit does not prove nothing changed: the steps around the config write
can fail too (`_record_seen` after it).
The `@app.command()` shells and the `--help` text stay in cli.py; this module
never imports cli.
"""

from __future__ import annotations

import difflib
import sys
from typing import Any

import typer
from rich.table import Table

from mind_meld import config as _config_module
from mind_meld import core, seen_sources
from mind_meld.config import (
    DEFAULT_SOURCES,
    get_default_source,
    get_sources,
    patch_config_on_disk,
)
from mind_meld.consoles import console
from mind_meld.errors import ConfigError
from mind_meld.manifest import _is_excluded as _manifest_is_excluded
from mind_meld.manifest import walk_source


def sources() -> None:
    """List configured sync sources, active and disabled, with file counts."""
    config = core._get_config(read_only=True)

    src_list = core._resolve_all_configured_sources(config)
    disabled_set = set(config.get("sync", {}).get("disabled_sources", []) or [])
    max_file_size = config["sync"]["max_file_size"]

    table = Table(title="Configured Sources")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Type")
    table.add_column("Enabled")
    table.add_column("Files")
    table.add_column("Excluded")

    for src in src_list:
        # Walk WITHOUT exclude_patterns so we count what the per-source
        # globs would have stripped. `all_files` is already post-EXCLUDED
        # (the global junk list runs inside `_record_file`), so any match
        # against `patterns` here is attributable to per-source globs only.
        # Two passes is the cheapest accurate diagnostic — instrumenting
        # the walker with a counter would leak diagnostic state into the
        # hot path.
        src_no_excludes = {
            k: v for k, v in src.items() if k not in ("exclude_patterns", "include_patterns")
        }
        _, all_files = walk_source(src_no_excludes, max_file_size)
        patterns = src.get("exclude_patterns") or []
        includes = src.get("include_patterns") or []
        if patterns or includes:
            excluded_count = sum(
                1
                for rel in all_files
                if _manifest_is_excluded(rel, patterns, None, includes or None)
            )
            kept = len(all_files) - excluded_count
            excluded_display = str(excluded_count)
        else:
            kept = len(all_files)
            excluded_display = "—"
        is_disabled = src["name"] in disabled_set
        enabled_display = "[red]N[/red]" if is_disabled else "[green]Y[/green]"
        # Dim the row's data columns when disabled to make the state
        # obvious at a glance.
        wrap = (lambda s: f"[dim]{s}[/dim]") if is_disabled else (lambda s: s)
        table.add_row(
            wrap(src["name"]),
            wrap(src["path"]),
            wrap(src["type"]),
            enabled_display,
            wrap(str(kept)),
            wrap(excluded_display),
        )

    console.print(table)


def _known_source_names(config: dict) -> list[str]:
    """Sorted union of configured and default sync source names.

    The validation surface for `mm enable-source` / `mm disable-source`.
    A name is "known" if the user configures its file source or mm ships a
    default for it. --force accepts unknown names.
    """
    explicit = [
        s["name"]
        for s in config.get("sync", {}).get("sources", []) or []
        if s.get("name") not in _config_module.RETIRED_SOURCE_NAMES
    ]
    defaults = [s["name"] for s in DEFAULT_SOURCES]
    return sorted(set(explicit) | set(defaults))


def _validate_source_name(name: str, config: dict, *, force: bool) -> None:
    """Raise ConfigError on unknown name unless --force.

    Closest-match hint via difflib so a typo like `gstck` suggests `gstack`.
    --force surfaces a stderr breadcrumb but accepts (forward-compat:
    pre-disabling a not-yet-shipped source like codex).
    """
    if name in _config_module.RETIRED_SOURCE_NAMES:
        raise ConfigError(
            f"source '{name}' was retired; "
            "run mm migrate-config to remove it from your configuration."
        )
    valid = _known_source_names(config)
    if name in valid:
        return
    if force:
        print(
            f"mm: warning: '{name}' is not a known source "
            f"(valid: {', '.join(valid)}); accepting via --force.",
            file=sys.stderr,
        )
        return
    matches = difflib.get_close_matches(name, valid, n=1, cutoff=0.6)
    suggestion = f" Did you mean '{matches[0]}'?" if matches else ""
    raise ConfigError(
        f"unknown source '{name}' — valid: {', '.join(valid)}.{suggestion} "
        "Use --force to accept a name not yet known to mm "
        "(forward-compat for not-yet-shipped sources)."
    )


def _record_seen(names: list[str]) -> None:
    """Mark `names` as acknowledged in the seen_sources tracker.

    Atomic under flock via `seen_sources.acknowledge` (codex 2026-04-25:
    the previous read+update+write split lost concurrent acknowledgments).
    A failed tracker write surfaces a stderr breadcrumb and the command goes
    on; failing to re-read the config or to open or lock the tracker raises.
    The seen tracker drives the `mm status` new-source hint; losing it just
    means a hint repeats.
    """
    config = core._get_config(read_only=False)
    currently_resolved = [s["name"] for s in get_sources(config)]
    seen_sources.acknowledge(names, initial=currently_resolved)


def disable_source(name: str, *, force: bool = False) -> None:
    """Disable a sync source on this device only."""
    config = core._get_config(read_only=False)
    try:
        _validate_source_name(name, config, force=force)
    except ConfigError as e:
        core._error(str(e))

    sync = dict(config.get("sync", {}) or {})
    disabled = list(sync.get("disabled_sources", []) or [])
    if name in disabled:
        console.print(f"[dim]Source '{name}' is already disabled.[/dim]")
        return

    disabled.append(name)
    updates: dict[str, dict[str, Any]] = {"sync": {"disabled_sources": sorted(disabled)}}
    patch_config_on_disk(updates)
    _record_seen([name])

    console.print(f"[green]Disabled source '{name}' on this device.[/green]")
    console.print(f"[dim]Run 'mm enable-source {name}' to re-enable.[/dim]")


def _print_grok_enable_notes() -> None:
    console.print(
        "[dim]Syncs ~/.grok skills/, commands/, rules/, and memory-v2/ "
        "topics. "
        "Observations, sessions, SQLite indexes, MEMORY.md, settings, credentials, "
        "plugins and hooks are not synced.[/dim]"
    )
    console.print(
        "[dim]Grok keeps memory off by default; turn it on on each machine with "
        "\\[memory] enabled = true in ~/.grok/config.toml (or GROK_MEMORY=1).[/dim]"
    )


def enable_source(name: str, *, force: bool = False) -> None:
    """Enable a sync source on this device."""
    config = core._get_config(read_only=False)
    try:
        _validate_source_name(name, config, force=force)
    except ConfigError as e:
        core._error(str(e))

    sync = dict(config.get("sync", {}) or {})
    disabled = list(sync.get("disabled_sources", []) or [])
    explicit_sources = list(sync.get("sources", []) or [])
    has_explicit_sources = "sources" in sync
    explicit_names = [s["name"] for s in explicit_sources]
    default_names = [s["name"] for s in DEFAULT_SOURCES]

    updates: dict[str, Any] = {}

    if name in disabled:
        disabled.remove(name)
        updates["disabled_sources"] = sorted(disabled)

    # Grok, Muse and Antigravity must be materialized even when their
    # allowlisted trees are not present yet: explicit enable is consent.  In a
    # legacy config, first preserve the resolved source set before writing
    # sync.sources.  An explicit list takes priority over legacy
    # claude_dir/default fallback; writing only the new host here would make the
    # existing sources disappear and eventually produce their deletion
    # tombstones.
    if name in {"grok", "muse", "agy"} and not has_explicit_sources:
        explicit_sources = get_sources(config)
        explicit_names = [s["name"] for s in explicit_sources]
        updates["sources"] = explicit_sources

    # If the user has explicit sources and this name isn't among them,
    # but it IS in DEFAULT_SOURCES, append the default so enable actually
    # has effect (auto-detect doesn't fire when explicit sources are set).
    needs_explicit_append = name not in explicit_names and (
        name in {"grok", "muse", "agy"} or (has_explicit_sources and name in default_names)
    )
    if needs_explicit_append:
        default = get_default_source(name)
        if default is not None:
            explicit_sources.append(default)
            updates["sources"] = explicit_sources

    if not updates:
        # Already enabled and configured — no-op message.
        if name in explicit_names or (not has_explicit_sources and name in default_names):
            console.print(f"[dim]Source '{name}' is already enabled.[/dim]")
            if name == "grok":
                _print_grok_enable_notes()
            return

    if updates:
        config_updates: dict[str, dict[str, Any]] = {"sync": updates}
        patch_config_on_disk(config_updates)

    _record_seen([name])
    console.print(f"[green]Enabled source '{name}' on this device.[/green]")
    if name == "grok":
        _print_grok_enable_notes()
        console.print("[dim]Run 'mm disable-source grok' to turn this off.[/dim]")
    elif name == "muse":
        console.print(
            "[dim]Syncs ~/.local/share/muse memory/ only. "
            "Session files are not synced. Prompts never leave the Mac.[/dim]"
        )
        console.print(
            "[dim]Reads completed model-step token totals from local Muse session logs. "
            "Session files are not synced. Prompts never leave the Mac.[/dim]"
        )
        console.print("[dim]Run 'mm disable-source muse' to turn this off.[/dim]")
    elif name == "agy":
        console.print(
            "[dim]Syncs ~/.gemini/config skills/, rules/, AGENTS.md, and GEMINI.md. "
            "Session databases and settings are not synced.[/dim]"
        )
        console.print(
            "[dim]Reads completed model-step token totals from local Antigravity "
            "session databases. Prompts never leave this device.[/dim]"
        )
        console.print("[dim]Run 'mm disable-source agy' to turn this off.[/dim]")


def reconfigure_sources() -> None:
    """Re-run the source picker against the current config and new defaults."""
    config = core._get_config(read_only=False)
    sync = dict(config.get("sync", {}) or {})
    explicit_sources = list(sync.get("sources", []) or [])
    explicit_names = [s["name"] for s in explicit_sources]
    has_explicit_sources = "sources" in sync
    disabled = list(sync.get("disabled_sources", []) or [])
    default_names = [s["name"] for s in DEFAULT_SOURCES]

    # Unified ordered list: DEFAULT_SOURCES first (canonical surfacing
    # order), then any user-customized names not in defaults. Prefer the
    # user's existing entry for each name so customizations show in the
    # picker context.
    seen_names: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for d in DEFAULT_SOURCES:
        seen_names.add(d["name"])
        existing = next((s for s in explicit_sources if s["name"] == d["name"]), None)
        ordered.append(existing if existing is not None else d)
    for s in explicit_sources:
        if s["name"] in _config_module.RETIRED_SOURCE_NAMES:
            continue
        if s["name"] not in seen_names:
            seen_names.add(s["name"])
            ordered.append(s)

    if not ordered:
        console.print("[dim]No sources configured to reconfigure.[/dim]")
        return

    console.print("[bold]Reconfigure sources for this device.[/bold]")
    console.print(
        "[dim]Disabling marks the source in disabled_sources (per-machine, "
        "not synced). Disabling does NOT delete the [[sync.sources]] entry — "
        "re-enabling preserves customizations. Retired source blocks are removed.[/dim]\n"
    )

    new_explicit_sources: list[dict[str, Any]] = []
    new_disabled = list(
        set(core._explicit_retired_sources(explicit_sources))
        | (set(disabled) & _config_module.RETIRED_SOURCE_NAMES)
    )

    try:
        for item in ordered:
            iname = item["name"]
            detected = core._source_path_is_detected(item)
            default_active = iname in default_names
            if iname in ("grok", "muse"):
                # Grok and Muse never default on: their trees are host-created
                # or opt-in only, so even a detected root is not consent for
                # sync or usage reads.
                default_active = False
            elif iname == "agy":
                # Interactive enrollment follows init's detected-host default;
                # background resolution still requires explicit consent.
                default_active = detected
            currently_active = (
                iname in explicit_names or (not has_explicit_sources and default_active)
            ) and iname not in disabled
            answered = core._prompt_source_toggle(
                item, current_state=currently_active, detected=detected
            )
            existing = next((s for s in explicit_sources if s["name"] == iname), None)
            if answered:
                # Source on: ensure in [[sync.sources]] (use existing for
                # customizations, else default).
                if existing is not None:
                    new_explicit_sources.append(existing)
                else:
                    default = get_default_source(iname)
                    if default is not None:
                        new_explicit_sources.append(default)
            else:
                # Source off: keep [[sync.sources]] entry intact for re-enable
                # to preserve customizations, plus mark disabled.
                if existing is not None:
                    new_explicit_sources.append(existing)
                new_disabled.append(iname)
    except (KeyboardInterrupt, typer.Abort):
        console.print("\n[yellow]Reconfigure aborted; no changes written.[/yellow]")
        raise typer.Exit(1) from None

    updates: dict[str, Any] = {
        "disabled_sources": sorted(new_disabled),
        "sources": new_explicit_sources,
    }

    patch_config_on_disk({"sync": updates})

    # Mark every name as seen — reconfigure is the explicit acknowledgment
    # surface, so even sources the user re-confirmed should not surface as
    # "new" hints next run.
    _record_seen([item["name"] for item in ordered])

    console.print("\n[green]Source configuration updated.[/green]")
