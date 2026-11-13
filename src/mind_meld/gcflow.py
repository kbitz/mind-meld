"""Implementation of `mm gc` and own-device orphan blob collection."""

from __future__ import annotations

from dataclasses import dataclass

from mind_meld import core, retention, safety, upgrade
from mind_meld.consoles import console, stderr_console
from mind_meld.errors import LockError, MindMeldError
from mind_meld.lockfile import acquire_lock, release_lock
from mind_meld.safety import safe_str
from mind_meld.storage import get_backend
from mind_meld.storage import keys as storage_keys
from mind_meld.storage.keys import parse_blob_key


@dataclass(frozen=True)
class GcBlobResult:
    """Outcome of `_do_gc` for `gcflow.gc` and `cli.push`."""

    deleted: int = 0
    collected: bool = True
    tmp_outcome: retention.ReapOutcome = retention.ReapOutcome()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int) and not isinstance(other, bool):
            return self.deleted == other
        if isinstance(other, GcBlobResult):
            return (self.deleted, self.collected, self.tmp_outcome) == (
                other.deleted,
                other.collected,
                other.tmp_outcome,
            )
        return NotImplemented


def gc(
    dry_run: bool = False,
    verbose: bool = False,
    prune_conflicts: bool = False,
) -> None:
    """Collect this device's orphaned blobs and local retention data."""
    config = core._get_config(read_only=dry_run)
    refusal_suffix = core.DRY_RUN_REFUSAL if dry_run else ""
    passphrase = core._get_passphrase_or_exit()

    try:
        acquire_lock()
    except LockError as e:
        core._error(str(e))

    try:
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
        blob_res = _do_gc(
            config,
            passphrase,
            memory_kb,
            dry_run,
            verbose,
            emit_retention_summary=verbose,
            emit_remediation=False,
        )
        outcomes: list[retention.ReapOutcome] = [blob_res.tmp_outcome]
        # Track 7B: events retention is always-on (fleet policy, not opt-in).
        # See `_gc_old_event_files` for the tombstone-propagation framing.
        outcomes.append(
            retention._gc_old_event_files(
                config, dry_run, verbose, emit_summary=verbose, emit_remediation=False
            )
        )
        # v0.11.14+: token cache reaper. Stale entries (no living jsonl OR
        # by_day older than 90d) are dropped. Dry-run reports without
        # mutating the cache file.
        outcomes.append(
            retention._gc_token_cache(
                dry_run, verbose, emit_summary=verbose, emit_remediation=False
            )
        )
        # v0.12.39: leftover v0.12.0 trend-snapshot files. Best-effort,
        # filename-matched only, then rmdir if empty. Never rm -rf.
        outcomes.append(
            retention._gc_orphan_retros_dir(
                dry_run, verbose, emit_summary=verbose, emit_remediation=False
            )
        )
        # Bare `--dry-run` previews the conflict reaper (the one that
        # touches user content). Apply still requires `--conflicts`.
        if prune_conflicts or dry_run:
            outcomes.append(
                retention._gc_old_conflict_files(
                    config,
                    dry_run,
                    verbose,
                    deletion_requires_flag=dry_run and not prune_conflicts,
                    emit_summary=verbose,
                    emit_remediation=False,
                )
            )
        outcomes.append(
            retention._gc_deleted_copies(
                dry_run, verbose, emit_summary=verbose, emit_remediation=False
            )
        )
        if dry_run:
            console.print(core.DRY_RUN_COMPLETE)
        elif any(o.needs_remediation for o in outcomes):
            core._warning(
                "Retention cleanup was incomplete. Fix permissions or locks; "
                "use -v for paths and details.",
                fix="mm gc -v",
            )
        elif not blob_res.collected:
            console.print(
                core._outcome_line(
                    "gc", "blob collection skipped (own manifest missing); local retention complete"
                ),
                soft_wrap=True,
            )
        else:
            console.print(
                core._outcome_line(
                    "gc",
                    f"{core._count(blob_res.deleted, 'orphaned blob', 'orphaned blobs')} deleted",
                ),
                soft_wrap=True,
            )
    except MindMeldError as e:
        if dry_run:
            core._error(str(e) + refusal_suffix)
        raise
    finally:
        release_lock()


def _do_gc(
    config: dict,
    passphrase: str,
    memory_kb: int,
    dry_run: bool,
    verbose: bool,
    *,
    emit_retention_summary: bool = True,
    emit_remediation: bool = True,
) -> GcBlobResult:
    """Collect this device's orphaned blobs from its raw manifest only."""
    backend = get_backend(config)
    my_device_id = config["device"]["id"]
    own_prefix = storage_keys.data_prefix(my_device_id)

    # Sweep this device's stale tmp*.tmp files before ref-counting.
    # Runs UNDER the caller's lock (acquire_lock already held by gc()),
    # so no concurrent writer can race with the sweep.
    tmp_outcome = retention._sweep_local_tmp_files(
        backend,
        my_device_id,
        dry_run,
        verbose,
        emit_summary=emit_retention_summary,
        emit_remediation=emit_remediation,
    )

    # Only this device's raw manifest can authorize collecting its blobs.
    # Consumer exclusions/disabled sources must never filter these references.
    gc_fetch = core._fetch_remote_manifest(backend, my_device_id, passphrase, memory_kb)
    # _error escapes markup itself; only the direct stderr print adds safe_str.
    label = safety.safe_terminal_str(
        f"{config['device'].get('name', my_device_id)} ({my_device_id})"
    )
    referenced_hashes: set[str] = set()

    def _skip_missing() -> GcBlobResult:
        with core._pause_sync_progress():
            stderr_console.print(
                f"mm: warning: this device's manifest is missing: {safe_str(label)}. "
                "Blob collection was skipped; no blobs were deleted. "
                "Wait for sync, inspect with 'mm diag', then retry 'mm gc'."
            )
        return GcBlobResult(deleted=0, collected=False, tmp_outcome=tmp_outcome)

    def _refuse_unless_complete(fetch: core.ManifestFetch) -> None:
        if fetch.newer_version is not None:
            core._error(
                "cannot GC safely — newer-format manifest is corrupt on: "
                f"{label}. Leave it in place. "
                f"Upgrade with: {upgrade.INSTALL_CMD}." + (core.DRY_RUN_REFUSAL if dry_run else "")
            )
        # A conflict copy alone cannot stand in for an absent or unreadable
        # canonical. Refuse previews too: an incomplete reference set cannot
        # supply a trustworthy orphan list for a separate delete flow.
        if fetch.conflict_copies_only:
            core._error(
                "cannot GC safely — this device's manifest is held only by "
                f"conflict copies (its main copy is missing or unreadable): {label}. "
                "Run 'mm push' on this device to rewrite it, then retry GC."
                + (core.DRY_RUN_REFUSAL if dry_run else "")
            )
        if fetch.status == "corrupt":
            core._error(
                "cannot GC safely — this device's manifest is corrupt: "
                f"{label}. It may reference blobs that would be reaped as orphans. "
                "Run 'mm push' on this device to recover its manifest, then retry GC."
                + (core.DRY_RUN_REFUSAL if dry_run else "")
            )

    def _add_references(fetch: core.ManifestFetch) -> None:
        # The manifest is validated/normalized by load_manifest, unfiltered.
        for src_data in fetch.manifest.get("sources", {}).values():
            for info in src_data.get("files", {}).values():
                referenced_hashes.add(info["sha256"])

    if gc_fetch.status == "missing":
        return _skip_missing()
    _refuse_unless_complete(gc_fetch)
    _add_references(gc_fetch)

    # The own manifest can change after the reference scan (a reserved
    # version, a conflict copy, or a failed read). Revalidate this device only
    # before listing/deleting blob candidates and keep any added references.
    again = core._fetch_remote_manifest(backend, my_device_id, passphrase, memory_kb)
    if again.status == "missing":
        return _skip_missing()
    _refuse_unless_complete(again)
    _add_references(again)

    # The trailing slash excludes prefix-sharing peer identities.
    all_blobs = backend.list_keys(own_prefix)
    orphan_count = 0
    malformed_count = 0

    for bkey in all_blobs:
        if not bkey.endswith(".enc"):
            continue
        parsed = parse_blob_key(bkey)
        if parsed is None:
            # Wrong-depth .enc under data/ — not a known blob shape. Could be
            # a misplaced artifact from a future format, an external write, or
            # corruption. Surface it (verbose/dry-run) so the user can audit;
            # never auto-reap (we don't know what it is). `.tmp` artifacts from
            # crashed pushes are handled separately by _sweep_local_tmp_files
            # at the start of _do_gc, so they're not seen here.
            malformed_count += 1
            if verbose or dry_run:
                # Printable-escape first, then Rich markup: the field helper
                # keeps literal brackets, so markup escaping is still required.
                console.print(
                    f"  [yellow]malformed (skipped):[/yellow] "
                    f"{safe_str(safety.safe_terminal_str(bkey))}"
                )
            continue
        did, sha = parsed
        if did != my_device_id:
            continue
        if sha not in referenced_hashes:
            orphan_count += 1
            if verbose:
                console.print(f"  [red]orphan:[/red] {safe_str(safety.safe_terminal_str(bkey))}")
            if not dry_run:
                backend.delete(bkey)

    if malformed_count and not (verbose or dry_run):
        # Always summarize malformed-path count even in non-verbose mode, so
        # the user has a signal that something weird is sitting in storage.
        with core._pause_sync_progress():
            console.print(
                f"  [yellow]warning:[/yellow] {malformed_count} blob(s) at unexpected "
                f"path depth (skipped; run with --verbose to see)."
            )

    if dry_run:
        console.print(f"\n[bold]Dry run:[/bold] {orphan_count} orphaned blobs found.")

    return GcBlobResult(deleted=orphan_count, collected=True, tmp_outcome=tmp_outcome)
