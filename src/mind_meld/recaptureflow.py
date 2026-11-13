"""Implementation of `mm recapture` and its window/commit helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer

from mind_meld import config as _config_module
from mind_meld import core, events, events_tail, pullhistory, statedb, upgrade
from mind_meld.config import MM_INTERNAL_SOURCE_NAMES, resolve_sources
from mind_meld.consoles import console, stderr_console
from mind_meld.errors import GIT_WALK_FAILURES_URL, LockError, MindMeldError, SnapshotError
from mind_meld.lockfile import acquire_lock, release_lock
from mind_meld.safety import safe_str
from mind_meld.storage import get_backend

RECAPTURE_NOT_PREVIEWED = (
    "Not previewed: writing the git-snapshot rows and the full push that publishes "
    "them together with any other pending local changes."
)

RECAPTURE_EXIT_PARTIAL = 4
"""Partial recapture exit. Distinct from pull ``--conflict-mode fail`` (3)."""


def _held_blocks_recapture(held: list[tuple[str, str, str]]) -> bool:
    """True when a recapture's own mm-events day file did not go out this push."""
    return any(name in MM_INTERNAL_SOURCE_NAMES for name, _rel, _reason in held)


def _notice_recapture_host_usage(config: dict, sources: list[dict]) -> None:
    """Cache-only bridge for operators who previously used git recapture."""
    from mind_meld import host_registry

    for name, _ in events_tail._default_host_readers(
        sources,
        grok_consented=_config_module.grok_usage_consented(config),
    ):
        state = host_registry.reader_usage_diag(name)
        cold = host_registry.reader_cache_cold(name, state)
        if cold or state.get("last_reason"):
            core._notice(
                "Recapture covers Git history only. Refresh host usage on this Mac.",
                fix="mm push",
            )
            return


def _parse_recapture_window(window: str) -> int:
    days = events.parse_nd_window(window)
    if days is None:
        core._error(events.window_syntax_error(window))
    if days < events.RECAPTURE_WINDOW_MIN_DAYS or days > events.RECAPTURE_WINDOW_MAX_DAYS:
        core._error(
            f"WINDOW must be between {events.RECAPTURE_WINDOW_MIN_DAYS}d and "
            f"{events.RECAPTURE_WINDOW_MAX_DAYS}d; got {days}d. "
            "The limit keeps the synced event log bounded."
        )
    return days


def _recapture_commit_stats(
    git_rows: list[dict], events_dir: Path
) -> tuple[list[dict], list[dict]]:
    already = events.recorded_commit_keys(events_dir)
    records = []
    for row in git_rows:
        records.extend(events.git_row_commit_records(row))
    new = [c for c in records if (c.get("remote", ""), c.get("sha", "")) not in already]
    return records, new


def _oldest_commit_date(commits: list[dict]) -> datetime | None:
    oldest: datetime | None = None
    for commit in commits:
        raw = commit.get("date")
        if not isinstance(raw, str):
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if oldest is None or ts < oldest:
            oldest = ts
    return oldest


def recapture(
    *,
    window: str = events.RECAPTURE_WINDOW_DEFAULT,
    dry_run: bool = False,
    verbose: bool = False,
    push_core: Callable[..., Any],
) -> None:
    """Recover omitted git commits into the fleet retro."""
    final_outcome: str | None = None
    should_nudge = False
    with pullhistory.command_scope(attended=True):
        days = _parse_recapture_window(window)
        config = core._get_config(read_only=dry_run)
        core._maybe_prompt_migration(config, read_only=dry_run)
        if not dry_run:
            config = core._get_config(read_only=False)
        refusal_suffix = core.DRY_RUN_REFUSAL if dry_run else ""
        passphrase = core._get_passphrase_or_exit()

        try:
            acquire_lock()
        except LockError as e:
            core._error(str(e))

        try:
            if not dry_run and not pullhistory.failed():
                pullhistory.record_failure(statedb.recover_if_needed())
            backend = get_backend(config)
            try:
                pending: list[str] = []
                memory_kb = core._init_crypto_session(
                    backend, passphrase, config, read_only=dry_run, pending=pending
                )
                for note in pending:
                    console.print(safe_str(note))
            except MindMeldError as e:
                core._error(str(e) + refusal_suffix)
            try:
                resolution = resolve_sources(config, strict=True, bootstrap=not dry_run)
                sources = resolution.available
                for src in resolution.selected:
                    if src["name"] == "mm-events" and src not in sources:
                        raise SnapshotError(
                            _config_module._missing_custom_mm_events_message(src["path"])
                        )
            except SnapshotError as e:
                suffix = refusal_suffix
                core._error(f"{e} No recapture rows were written.{suffix}")
            disabled = list(config.get("sync", {}).get("disabled_sources", []) or [])
            if "mm-events" in disabled or not any(s.get("name") == "mm-events" for s in sources):
                state = "disabled" if "mm-events" in disabled else "not configured"
                if dry_run:
                    core._print_error(
                        f"recapture requires the 'mm-events' source, "
                        f"but it is {state} on this Mac. "
                        f"Enabling it changes config; then preview again: "
                        f"mm recapture --dry-run {window}.",
                        fix="mm enable-source mm-events",
                    )
                    stderr_console.print(core.DRY_RUN_REFUSAL.strip())
                else:
                    core._print_error(
                        f"recapture requires the 'mm-events' source, "
                        f"but it is {state} on this Mac. "
                        f"Enabling it changes config; then retry: mm recapture {window}.",
                        fix="mm enable-source mm-events",
                    )
                raise typer.Exit(1)

            since = datetime.now(timezone.utc) - timedelta(days=days)
            prepared = events_tail._prepare_recapture(
                config, sources, config["device"]["id"], since=since, persist=not dry_run
            )
            assert prepared is not None  # mm-events availability was checked above.

            n_roots = len(prepared.root_discovery.roots)
            skipped = prepared.walk_budget_aborts + prepared.walk_errors
            records, new_records = _recapture_commit_stats(prepared.git_rows, prepared.events_dir)
            until = prepared.until
            since_day = prepared.since.date().isoformat()
            until_day = until.date().isoformat()
            row_bytes = sum(
                len(json.dumps(row, sort_keys=True).encode("utf-8")) for row in prepared.git_rows
            )

            if dry_run:
                console.print("[bold]Recapture dry-run[/bold] — no commits written or uploaded.")
                console.print(f"  Window scanned:   {since_day} → {until_day} ({days}d)")
                console.print(f"  Repositories:     {n_roots} scanned, {skipped} skipped")
                non_benign_skip = False
                if prepared.git_rows:
                    for entry in prepared.git_rows[0].get("skipped", []):
                        reason = entry["reason"]
                        if reason == events.WALK_SKIP_NO_COMMITS:
                            reason = "no commits yet (benign)"
                        else:
                            non_benign_skip = True
                        console.print(
                            f"    skipped: {safe_str(entry['path'])} — {safe_str(reason)}"
                        )
                if non_benign_skip:
                    console.print(f"  See {GIT_WALK_FAILURES_URL}")
                console.print(f"  Commit records:   {len(records)} captured")
                console.print(f"  Estimated size:   {row_bytes} bytes")
                if n_roots == 0:
                    core._print_error(
                        "Recapture stopped: no Git repositories were discovered on this Mac. "
                        "Add their absolute paths under [retro].repo_roots, verify with 'mm diag', "
                        f"then preview again: mm recapture --dry-run {window}.",
                        fix="mm diag",
                    )
                    stderr_console.print(core.DRY_RUN_REFUSAL.strip())
                    raise typer.Exit(1)
                if skipped:
                    console.print(
                        f"Preview incomplete: {skipped} repositories could not be walked "
                        "(see the skipped lines above)."
                    )
                if prepared.root_discovery.exceeded:
                    console.print(
                        "Preview incomplete: repository discovery exceeded its budget; "
                        "more repositories may exist."
                    )
                if prepared.root_discovery.errors:
                    reasons = "; ".join(
                        safe_str(reason) for reason in prepared.root_discovery.errors
                    )
                    console.print(
                        f"Preview incomplete: repository discovery reported errors: {reasons}"
                    )
                console.print(core.DRY_RUN_COMPLETE)
                console.print(RECAPTURE_NOT_PREVIEWED)
                return

            if n_roots == 0:
                core._error(
                    "Recapture stopped: no Git repositories were discovered on this Mac. "
                    "Add their absolute paths under [retro].repo_roots, verify with 'mm diag', "
                    f"then retry: mm recapture {window}.",
                    fix="mm diag",
                )

            device_id = config["device"]["id"]
            appended = events.write_push_event(
                prepared.events_dir,
                device_id,
                prepared.git_rows,
                max_file_size=config["sync"]["max_file_size"],
            )
            if isinstance(appended, events.EventAppendSkipped):
                core._error(
                    "Recapture stopped: event batch was not written. Fix sync.max_file_size "
                    "or shrink the day file.",
                    fix=f"mm recapture {window}",
                )

            events_tail._commit_recorded_git_rows(
                prepared.events_dir,
                device_id,
                prepared.git_rows,
                since=prepared.since,
                complete=(
                    events.classify_discovery(prepared.root_discovery) not in {"partial", "empty"}
                    and not prepared.walk_budget_aborts
                    and not prepared.walk_errors
                ),
            )
            try:
                with pullhistory.batch():
                    result = push_core(
                        config,
                        passphrase,
                        memory_kb,
                        verbose=False,
                        dry_run=False,
                        report_nested_repositories=False,
                    )
            except SnapshotError as e:
                core._error(
                    f"Recapture was written locally but not synced. {e}",
                    fix="mm push",
                )
            except typer.Exit:
                core._print_error(
                    "Recapture was written locally but not synced. Fix the storage error above.",
                    fix="mm push",
                )
                raise

            synced = result is not None
            if not synced:
                core._error(
                    "Recapture was written locally but not synced. Fix the storage error above.",
                    fix="mm push",
                )
            if result is not None and _held_blocks_recapture(result.held):
                # The push held an mm-events file (a recapture day file was still
                # changing): the rows exist locally but have not gone out.
                core._error(
                    "Recapture was written locally but not synced. "
                    "A recapture file was still changing.",
                    fix="mm push",
                )

            _notice_recapture_host_usage(config, sources)
            partial = (
                prepared.root_discovery.exceeded
                or bool(prepared.root_discovery.errors)
                or skipped > 0
            )
            n_scanned_ok = n_roots - skipped
            if partial:
                narrower = "7d" if days > 7 else "1d"
                console.print(
                    f"Recapture incomplete: captured {len(records)} commit records "
                    f"from {n_scanned_ok} of {n_roots} repositories for "
                    f"{since_day} → {until_day}."
                )
                if prepared.walk_errors:
                    n = prepared.walk_errors
                    noun = "repository was" if n == 1 else "repositories were"
                    console.print(
                        f"{n} {noun} skipped because the Git walk failed for those repositories. "
                        "Check current repository failures: mm recapture --dry-run, "
                        f"then retry mm recapture {window}. See {GIT_WALK_FAILURES_URL}"
                    )
                if prepared.walk_budget_aborts:
                    n = prepared.walk_budget_aborts
                    noun = "repository was" if n == 1 else "repositories were"
                    console.print(
                        f"{n} {noun} skipped because the Git walk exceeded its budget. "
                        f"Retry a narrower window: mm recapture {narrower}"
                    )
                if prepared.root_discovery.exceeded:
                    console.print(
                        "Git repository discovery was incomplete. "
                        f"Retry a narrower window: mm recapture {narrower}"
                    )
                elif prepared.root_discovery.errors:
                    console.print(
                        "Git repository discovery reported errors. "
                        f"Check mm diag, then retry mm recapture {window}"
                    )
                if prepared.walk_errors:
                    fix_cmd = "mm recapture --dry-run"
                elif prepared.walk_budget_aborts or prepared.root_discovery.exceeded:
                    fix_cmd = f"mm recapture {narrower}"
                else:
                    fix_cmd = "mm diag"
                core._print_error(
                    f"Recapture incomplete: captured {len(records)} commit records "
                    f"from {n_scanned_ok} of {n_roots} repositories for "
                    f"{since_day} → {until_day}.",
                    fix=fix_cmd,
                )
                console.print(core._fix_line(fix_cmd))
                raise typer.Exit(RECAPTURE_EXIT_PARTIAL)

            if not records:
                if verbose:
                    console.print(f"  Window scanned:   {since_day} → {until_day} ({days}d)")
                    console.print(f"  Repositories:     {n_roots} scanned, {skipped} skipped")
                    console.print("  Commit records:   0 captured")
                repo_count = core._count(n_roots, "repository", "repositories")
                final_outcome = core._outcome_line(
                    "recapture",
                    f"0 commit records found across {repo_count} ({since_day} → {until_day})",
                )
            elif not new_records:
                if verbose:
                    console.print(f"  Window scanned:   {since_day} → {until_day} ({days}d)")
                    console.print(f"  Repositories:     {n_roots} scanned, {skipped} skipped")
                    console.print(f"  Commit records:   0 new ({len(records)} already recorded)")
                    console.print(
                        "  If the retro is still short, the gap is on another Mac "
                        "(mm devices) or in a repo discovery cannot see "
                        "(mm diag, then [retro] repo_roots)."
                    )
                rec_count = core._count(len(records), "commit record", "commit records")
                final_outcome = core._outcome_line(
                    "recapture",
                    f"0 new commit records ({rec_count} already recorded "
                    f"for {since_day} → {until_day})",
                )
            else:
                oldest = _oldest_commit_date(new_records)
                needed_days = days
                if oldest is not None:
                    needed_days = max(1, (until.date() - oldest.date()).days + 1)
                    needed_days = min(needed_days, events.RECAPTURE_WINDOW_MAX_DAYS)
                cover_days = min(max(days, needed_days), events.RECAPTURE_WINDOW_MAX_DAYS)
                if verbose:
                    console.print(f"  Window scanned:   {since_day} → {until_day} ({days}d)")
                    console.print(f"  Repositories:     {n_roots} scanned, {skipped} skipped")
                    console.print(f"  Commit records:   {len(records)} captured")
                    console.print(f"  Estimated size:   {row_bytes} bytes")
                    console.print(f"  Synced:           {'yes' if synced else 'no'}")
                    console.print("")
                    console.print(
                        "Retros window by the COMMIT's date, not by when mm captured it, so a short"
                    )
                    console.print("window will not show the older recoveries:")
                    console.print(
                        f"  mm retro-fleet {cover_days}d   # covers everything just recaptured"
                    )
                    console.print("  mm retro-fleet 7d    # covers the last 7 days only")
                if oldest is not None and needed_days > 7:
                    core._notice(
                        "Retros window by commit date, not capture time.",
                        fix=f"mm retro-fleet {cover_days}d",
                    )
                repo_count = core._count(n_roots, "repository", "repositories")
                if len(new_records) == len(records):
                    rec_count = core._count(len(new_records), "commit record", "commit records")
                    summary = (
                        f"{rec_count} captured and published across {repo_count} "
                        f"({since_day} → {until_day})"
                    )
                else:
                    new_count = core._count(
                        len(new_records), "new commit record", "new commit records"
                    )
                    summary = (
                        f"{new_count} captured and published "
                        f"({len(records)} total across {repo_count}, {since_day} → {until_day})"
                    )
                final_outcome = core._outcome_line("recapture", summary)
                should_nudge = True
        except MindMeldError as e:
            if dry_run:
                core._error(str(e) + refusal_suffix)
            raise
        finally:
            release_lock()

    if should_nudge:
        upgrade.emit_nudge_if_due(config)
    if final_outcome is not None:
        console.print(final_outcome, soft_wrap=True)
