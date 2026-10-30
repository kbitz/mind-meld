"""Mind Meld CLI — built with Typer.

Commands: init, push, pull, status, devices, diff, gc, autopull, autopush,
          sources, conflicts, resolve, retro-fleet, recapture, update.
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import typer
from packaging.version import Version
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.table import Table
from typer.core import TyperCommand

from mind_meld import (
    __version__,
    attemptlog,
    core,
    crypto,
    events,
    events_tail,
    fsutil,
    identity,
    initflow,
    manifest,
    pullflow,
    pullhistory,
    pullplan,
    resolveflow,
    retention,
    safety,
    seen_sources,
    sidecar,
    skill_link,
    statedb,
    statusflow,
    updateprogress,
    upgrade,
)
from mind_meld import config as _config_module
from mind_meld.config import (
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_SOURCES,
    MM_INTERNAL_SOURCE_NAMES,
    SourceResolution,
    get_default_source,
    get_sources,
    load_config,
    patch_config_on_disk,
    resolve_sources,
)
from mind_meld.conflictdiff import (
    format_age_delta,
)
from mind_meld.consoles import console, stderr_console

# Compatibility imports; runtime callers resolve through the owner module.
from mind_meld.core import (  # noqa: F401
    _AUTORUN_VERBS,
    _SYNC_OUTPUT,
    FetchStatus,
    ManifestFetch,
    _apply_verified_crypto_repair,
    _autorun_breadcrumb_path,
    _build_exclude_map,
    _build_include_map,
    _compute_recommended_excludes_diff,
    _compute_recommended_includes_diff,
    _config_missing_recommended_excludes,
    _count,
    _error,
    _fetch_remote_manifest,
    _filter_disabled_sources,
    _filter_excluded_paths,
    _filter_symlinked_paths,
    _freeze_nested_checkout_entries,
    _get_config,
    _get_passphrase_or_exit,
    _has_mtime_only_changes_vs_remote,
    _home_relative_path,
    _init_crypto_session,
    _list_devices_warn,
    _manifest_content_hash,
    _merge_manifests,
    _newer_format_message,
    _normalize_autorun_breadcrumbs,
    _pause_sync_progress,
    _prove_omitted_paths_absent,
    _require_published_source_root,
    _resolve_all_configured_sources,
    _retain_prior_default_sources,
    _stdin_is_terminal,
    iter_source_diffs,
)
from mind_meld.crypto import (
    encrypt,
    get_passphrase,
)
from mind_meld.devices import (
    list_devices,
    register_device,
    update_last_seen,
)
from mind_meld.errors import (
    GIT_WALK_FAILURES_URL,
    HOST_USAGE_CAPTURE_URL,
    ConfigError,
    CryptoError,
    KeyringUnavailableError,
    LockError,
    MindMeldError,
    SnapshotChanged,
    SnapshotError,
    StorageError,
    os_error_cause,
    snapshot_refusal,
)
from mind_meld.lockedjson import LockContended, locked_json_rmw
from mind_meld.lockfile import acquire_lock, release_lock
from mind_meld.manifest import (
    DiffResult,
    _under_skip_prefix,
    build_manifest_v2,
    generate_tombstones,
    is_pre_inversion_conflict_filename,
    is_tombstoned,
    mtime_from_manifest,
    parse_conflict_created_at,
    read_file_revision,
    serialize_manifest,
    walk_source,
)
from mind_meld.manifest import (
    _FileTooLarge as _ManifestFileTooLarge,
)
from mind_meld.manifest import (
    _is_excluded as _manifest_is_excluded,
)
from mind_meld.retention import CONFLICT_AGE_DAYS
from mind_meld.safety import (  # noqa: F401 — re-exported for backwards-compat
    safe_str,
    safe_text,
    strip_terminal_escapes,
)
from mind_meld.statusflow import (  # noqa: F401
    _BREADCRUMB_STALE_AFTER_HOURS,
    _BROKEN_DIAG_STATUS_TO_INSTALL,
    _CURSOR_DETAIL_NO_CLAUSE,
    _CURSOR_DISABLE,
    _CURSOR_ROW_CAUSES,
    _DIAG_CAPTURE_CLAMP,
    _DIAG_MODELS_SHOWN,
    _DISCOVERY_REJECT_REASONS,
    _breadcrumb_staleness_suffix,
    _clamp_peer_text,
    _collect_diag_state,
    _collect_discovery_diag,
    _collect_git_capture_diag,
    _cursor_store_detail_clause,
    _cursor_unsupported_clause,
    _diag_models_line,
    _fresh_discovery_label,
    _host_complete_read_line,
    _host_publication,
    _host_read_budgets,
    _host_read_sweep_line,
    _host_usage_blocker,
    _host_usage_module,
    _print_host_publication,
    _print_retro_capture_status,
    _read_autorun_breadcrumbs,
    _read_autorun_evidence,
    _read_capture_rows,
    _reader_capture_readiness,
    _render_broken_skill_status,
    _sanitize_recorded_capture,
    _sqlite_version,
    _status_notices,
    _status_verdict,
    _usage_capture_needs_upgrade,
    _usage_capture_remedy,
)
from mind_meld.storage import get_backend
from mind_meld.storage import keys as storage_keys
from mind_meld.storage.keys import (
    blob_key,
    device_key,
    manifest_key,
    parse_blob_key,
)
from mind_meld.storage.local import LocalBackend


@dataclass
class PushResult:
    """Result of a push operation."""

    # Invocation-local display facts for the attended outcome line (Track 78A);
    # never persisted.
    no_sources: bool = False
    published_content_files: int | None = None
    metadata_updated: bool = False
    selection_updated: bool = False
    manifest_repaired: bool = False
    # User-source bytes, metadata or selection changed (mm-owned sources excluded).
    content_changed: bool = False
    content_files: int = 0
    content_accepted: bool = False
    host_usage_published: bool = False
    maintenance_failures: int = 0
    total_new: int = 0
    total_modified: int = 0
    total_deleted: int = 0
    bytes_transferred: int = 0
    elapsed: float = 0.0
    # Bounded events-tail candidates; Degradations.total preserves occurrences
    # (v0.12.16). Empty on a healthy push. `autopush` turns a non-empty list
    # into a `degraded` autorun breadcrumb so `mm status` stops reporting
    # `success` while the retro pipeline is dead — the events tail is
    # forensic-only and swallows its own failures, so before this the ONLY
    # signal was a stderr notice from a command that runs unattended from a
    # Claude Code hook, i.e. nobody's terminal. Mirrors the `degradations`
    # list `autopull` has carried since v0.8.1.
    events_degradations: list[str] = field(default_factory=lambda: events_tail.Degradations())
    # Files this push left at their previously published version because they
    # were still changing after one re-read (Track 75A): (source, rel_path,
    # reason) sorted by source then path, reason one of changed / vanished /
    # grew. A never-published file is left out. Every displayed count is
    # `len(held)`; autopush records it in its `degraded` breadcrumb.
    held: list[tuple[str, str, str]] = field(default_factory=list)


app = typer.Typer(
    name="mm",
    help="Mind Meld — sync Claude Code sessions and other sources across machines.",
    add_completion=True,
)
# `console` / `stderr_console` now live in `mind_meld.consoles` (Track 16A) so
# `resolveflow` and `retention` can render through the SAME objects without
# importing `cli` — see that module's docstring for why sharing the instances is
# load-bearing. Imported into this namespace so the ~280 existing bare
# `console.print(...)` call sites stay unchanged.
#
# stderr_console is the dedicated stderr sink for _error and other failure-path
# output. Rich formatting is preserved in interactive terminals; pipes
# (autopush/autopull quiet mode, CI) get a clean stdout and one-line stderr per
# the contract documented in README.md "Claude Code Integration." A single
# module-level instance rather than ad-hoc construction keeps color-capability
# detection and terminal-width behavior consistent across call sites.


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mm {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    no_check_version: bool = typer.Option(
        False,
        "--no-check-version",
        help=(
            "Skip the update check for this invocation: no nudge and no "
            "automatic update. Force-skips regardless of \\[upgrade] auto_check in config."
        ),
    ),
) -> None:
    # Wire the per-invocation override into the upgrade module before any
    # command body runs. Force-skips both `check_for_upgrade` and
    # `detect_self_version_transition` for this process.
    if no_check_version:
        upgrade.set_invocation_skip(True)


RECAPTURE_NOT_PREVIEWED = (
    "Not previewed: writing the git-snapshot rows and the full push that publishes "
    "them together with any other pending local changes."
)


def _filter_unselected_sources(manifest: dict, selected: set[str] | list[str]) -> dict:
    """Drop unselected source file entries; preserve existing tombstones.

    Asymmetric, matching ``_filter_disabled_sources``: retired or unselected
    source files must not generate deletion tombstones, and established
    unexpired tombstones stay.
    """
    selected_set = set(selected) - _config_module.RETIRED_SOURCE_NAMES
    current = manifest.get("sources", {})
    if not current or all(name in selected_set for name in current):
        return manifest
    out = dict(manifest)
    out["sources"] = {name: data for name, data in current.items() if name in selected_set}
    return out


def _drop_unfrozen_checkout_files(
    prior_manifest: dict,
    nested_roots: Mapping[str, Iterable[str]],
    frozen: Mapping[str, set[str]],
) -> dict:
    """Drop prior file entries under checkout roots that were not frozen; keep tombstones.

    A tombstoned path stays deleted, and a recovered entry without a stored blob
    is excluded. Neither is this push's to prove: the omission guard would
    refuse a file still on disk inside the checkout.
    """
    sources = dict(prior_manifest.get("sources", {}))
    changed = False
    for name, roots in nested_roots.items():
        data = sources.get(name)
        if not data:
            continue
        prefixes = sorted(roots)
        keep = frozen.get(name, set())
        files = data.get("files", {})
        kept = {
            rel: info
            for rel, info in files.items()
            if rel in keep or not _under_skip_prefix(rel, prefixes)
        }
        if len(kept) != len(files):
            sources[name] = {**data, "files": kept}
            changed = True
    return {**prior_manifest, "sources": sources} if changed else prior_manifest


def _include_prior_host_if_needed(
    config: dict,
    resolution: SourceResolution,
    remote_manifest: dict | None,
    sources: list[dict[str, Any]],
    name: str,
) -> list[dict[str, Any]]:
    """Retain a published host source whose allowlisted tree is gone.

    Muse and Antigravity sources activate by explicit enrollment, and their
    allowlisted trees can still be deleted later. Once a source has been published, losing
    the tree must not silently drop the source from the scan: retain it so
    the deletions truthfully propagate, and refuse loudly when the root
    itself is unreadable.
    """
    if remote_manifest is None or not (
        remote_manifest.get("sources", {}).get(name, {}).get("files")
    ):
        # An empty usage-only source does not establish a populated sync root.
        return sources
    disabled = set(config.get("sync", {}).get("disabled_sources") or [])
    if name in disabled or any(src["name"] == name for src in sources):
        return sources
    host_cfg = next((src for src in resolution.selected if src["name"] == name), None)
    if host_cfg is None:
        if resolution.explicit:
            return sources
        host_cfg = get_default_source(name)
        if host_cfg is None:
            return sources
        host_cfg = {**host_cfg, "path": str(Path(host_cfg["path"]).expanduser())}
    root = Path(host_cfg["path"]).expanduser()
    try:
        root.stat()
    except FileNotFoundError as e:
        raise SnapshotError(
            snapshot_refusal(
                source=name,
                problem="is missing after it was previously published",
                cause=os_error_cause(e),
                next_action=(
                    "Restore the source folder, or run "
                    f"'mm disable-source {name}' if it should stop syncing."
                ),
            )
        ) from e
    except OSError as e:
        raise SnapshotError(
            snapshot_refusal(
                source=name,
                problem="could not be read",
                cause=os_error_cause(e),
                next_action=(
                    "Restore read access, or run "
                    f"'mm disable-source {name}' if it should stop syncing."
                ),
            )
        ) from e
    return [*sources, host_cfg]


def _refuse_unavailable_selected_sources(
    resolution: SourceResolution,
    remote_manifest: dict | None,
    available: list[dict[str, Any]],
) -> None:
    """Refuse a still-selected previously populated root that is unavailable."""
    available_names = {src["name"] for src in available}
    prior_sources = (remote_manifest or {}).get("sources", {})
    for src in resolution.selected:
        name = src["name"]
        if name in available_names:
            continue
        prior_files = prior_sources.get(name, {}).get("files") or {}
        if not prior_files:
            continue
        raise SnapshotError(
            snapshot_refusal(
                source=name,
                problem="is missing after it was previously published",
                next_action=(
                    "Restore the source folder, or run "
                    f"'mm disable-source {name}' if it should stop syncing."
                ),
            )
        )


def _print_diff_summary(diff: DiffResult, elapsed: float) -> None:
    if not diff.has_changes:
        console.print("[green]Nothing to sync — everything is up to date.[/green]")
        return

    if diff.new:
        console.print(f"  [green]+ {len(diff.new)} new[/green]")
    if diff.modified:
        console.print(f"  [yellow]~ {len(diff.modified)} modified[/yellow]")
    if diff.deleted:
        console.print(f"  [red]- {len(diff.deleted)} deleted[/red]")
    console.print(f"  {len(diff.unchanged)} unchanged")
    console.print(f"  Completed in {elapsed:.1f}s")


# ── shared helpers ────────────────────────────────────────────────────


@dataclass
class UploadOutcome:
    """What one source's upload loop did (Track 75A).

    ``patches`` holds the replacement manifest entry (sha256, size, mtime) for
    each file whose deferred re-read settled on a revision other than the one
    scanned; ``held`` maps each file still changing after that re-read to its
    reason. The caller applies both to its manifest before serializing; the
    loop never mutates the caller's manifest dicts.
    """

    bytes_transferred: int = 0
    patches: dict[str, dict[str, Any]] = field(default_factory=dict)
    held: dict[str, str] = field(default_factory=dict)


def _upload_changed_blobs(
    backend: LocalBackend,
    base_path: Path,
    to_upload: dict[str, dict],
    device_id: str,
    passphrase: str,
    memory_kb: int,
    verbose: bool = False,
    src_name: str | None = None,
    *,
    source_type: str = "claude",
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    allow_hold: bool = False,
) -> UploadOutcome:
    """Upload changed blobs to storage.

    Verifies the upload revision against the scanned digest, size, and
    mtime before any encryption or ``backend.put``. A file that no longer
    matches, vanished, or grew past the cap is queued and re-read exactly once
    after the rest of this source has uploaded (the other uploads are the
    settle delay; no sleep). A settled re-read is a verified revision of its
    own: its blob is keyed by its own digest and the returned ``patches`` entry
    replaces the scanned one. A file still changing after that re-read is
    ``held`` when ``allow_hold`` (no blob is put for it), else the transient
    ``SnapshotChanged`` aborts the push. Non-transient failures (could not be
    read, confinement) abort at once.

    `src_name` (when provided) drives `pullhistory.append` so the history
    log records one "uploaded" entry per file. Optional so legacy/test
    callers without source context still work; production push paths
    always pass it. Source type and cap come from trusted local config,
    never from a source name string.
    """

    def read_once(rel_path: str) -> Any:
        # Every transient is a SnapshotChanged here; at upload an oversized
        # file is one that grew after the scan accepted it at or under the cap.
        try:
            return read_file_revision(
                base_path / rel_path,
                max_file_size=max_file_size,
                retain_bytes=True,
                source_name=src_name,
                source_type=source_type,
                base=base_path,
            )
        except _ManifestFileTooLarge as e:
            raise SnapshotChanged(
                snapshot_refusal(
                    source=src_name,
                    rel_path=rel_path,
                    problem="grew beyond max_file_size while being read",
                    next_action=(
                        "Wait for the writer to finish, or raise sync.max_file_size, "
                        "then run mm push."
                    ),
                ),
                "grew",
            ) from e

    def matches_scan(revision: Any, info: dict) -> bool:
        # Only freshly scanned local revisions enter this upload path;
        # loaded manifests (whose mtime may normalize to None) must never.
        advertised_sha = info.get("sha256")
        advertised_size = info.get("size")
        advertised_mtime = info.get("mtime")
        mtime_ok = isinstance(advertised_mtime, str) and (
            revision.mtime_iso == advertised_mtime
            or _upload_mtime_matches(revision.mtime_iso, advertised_mtime)
        )
        return not (
            revision.digest != advertised_sha
            or revision.size != advertised_size
            or len(revision.data) != advertised_size
            or hashlib.sha256(revision.data).hexdigest() != advertised_sha
            or not mtime_ok
        )

    if to_upload:
        core._sync_progress_ensure_batch("Uploading files", len(to_upload), "files")

    with pullhistory.batch():
        outcome = UploadOutcome()

        def put_blob(rel_path: str, revision: Any) -> None:
            assert revision.data is not None
            enc_data = encrypt(revision.data, passphrase, memory_kb)
            backend.put(blob_key(device_id, revision.digest), enc_data)
            outcome.bytes_transferred += len(enc_data)

            if src_name is not None:
                pullhistory.append(
                    verb="push",
                    device=device_id,
                    source=src_name,
                    rel_path=rel_path,
                    action="uploaded",
                    local_sha=revision.digest,
                )

            if verbose:
                console.print(f"  [green]\u2191[/green] {safe_str(rel_path)}")
            core._sync_progress_advance()

        deferred: list[tuple[str, dict]] = []
        for rel_path, info in to_upload.items():
            try:
                revision = read_once(rel_path)
            except SnapshotChanged:
                deferred.append((rel_path, info))
                continue
            assert revision.data is not None
            if not matches_scan(revision, info):
                deferred.append((rel_path, info))
                continue
            put_blob(rel_path, revision)

        for rel_path, info in deferred:
            try:
                revision = read_once(rel_path)
            except SnapshotChanged as e:
                if not allow_hold:
                    raise
                outcome.held[rel_path] = e.reason
                core._sync_progress_advance()
                continue
            assert revision.data is not None
            # Read 2 publishes its own verified revision. Record the entry
            # before the blob put; a digest equal to the scanned one is an
            # idempotent put plus an mtime-only republish, no special case.
            outcome.patches[rel_path] = {
                **info,
                "sha256": revision.digest,
                "size": revision.size,
                "mtime": revision.mtime_iso,
            }
            put_blob(rel_path, revision)

        return outcome


def _upload_mtime_matches(accepted_iso: str, advertised: str) -> bool:
    try:
        return mtime_from_manifest(accepted_iso) == mtime_from_manifest(advertised)
    except (TypeError, ValueError):
        return False


# ── init ──────────────────────────────────────────────────────────────


@app.command()
def init() -> None:
    """Initialize Mind Meld: generate device ID, configure iCloud storage, set passphrase.

    Two-path flow. Storage is probed (mm-crypto-init) BEFORE any local state is
    written or any passphrase is committed to the keyring.

    First-device path (mm-crypto-init missing):
        Double-prompt passphrase, generate root_salt + keycheck, atomic write,
        then register device + save config + store passphrase (Track 5D order).

    Second-device path (mm-crypto-init ok):
        Single-prompt passphrase, derive master_key from the stored root_salt,
        verify keycheck. On success, register device + save config + store
        passphrase. On failure, nothing local is written.

    Two-tier re-init guard (Group 2 pre-flight 3):
        * Orphan case — mm-crypto-init ok + other occupancy: warn that
          a new device entry gets created alongside existing devices;
          require typer.confirm.
        * BRICK case — mm-crypto-init missing + blobs/manifests exist:
          re-bootstrap would generate a new root_salt and brick every
          existing blob. Refuse by default; require exact typed "BRICK".
    """
    initflow.init()


def _capture_notice(message: str) -> None:
    if core._SYNC_OUTPUT.get() is None:
        stderr_console.print(message)
    else:
        core._sync_diagnostic(message)


def _capture_attended_usage(
    config: dict,
    sources: list[dict],
    selected: list[dict],
    *,
    verbose: bool,
    outcome: attemptlog.CaptureOutcome | None = None,
) -> attemptlog.CaptureOutcome:
    """Optional capture inside the push lock, after the final source selection.

    Contain the whole phase: notice, warming, construction and serialization can
    fail as well as readers and append. The caller never retries it in the tail.
    """
    outcome = outcome or attemptlog.CaptureOutcome()
    phase = "capture-failed"
    try:
        readers = events_tail._default_host_readers(
            sources,
            cursor_consented=_config_module.cursor_host_usage_enabled(config),
        )
        readiness = _config_module.usage_capture_readiness(
            selected, sources, reader_consented=bool(readers)
        )
        if readiness != "ready":
            outcome.finish("prerequisites", readiness)
            _capture_notice(
                f"mm: warning: Usage capture (prerequisites: {readiness}): skipped — "
                f"{safe_str(_config_module.usage_capture_remedy(readiness))} "
                "Content sync continues."
            )
            return outcome.finish("prerequisites", readiness)
        core._sync_progress_phase("Capturing host usage")
        capture, rows = events_tail._capture_host_snapshot(
            config["device"]["id"],
            readers,
            host_budget_ms=events_tail.resolve_host_read_budget(config, interactive=True)[0],
            warm_host_cache=events_tail._warm_host_cache_with_notice,
        )
        dropped = dict(capture.dropped)
        outcomes = events.host_reader_outcomes(
            rows[0] if rows else None,
            [name for name, _read in readers],
            dropped=capture.dropped,
        )
        outcome.readers = outcomes
        notices = []
        for name, _read in readers:
            if name in dropped:
                notices.append(
                    events_tail._host_skip_phrase(
                        name, dropped[name], readiness="ready", attended=True
                    )
                )
            elif outcomes.get(name) == "partial":
                notices.append(f"{name} coverage is partial; mm diag shows the reader's state.")
            elif outcomes.get(name) == "absent":
                notices.append(f"{name} absent (no metadata ledger).")
            elif verbose:
                label = events.host_reader_label(outcomes[name])
                console.print(f"Usage capture: {safe_str(name)} — {label}")
        if not rows:
            notices.append("No usage snapshot written; run mm diag to diagnose the reader.")
        if not rows:
            outcome.finish("no-row")
        if notices:
            token = "no-row" if not rows else "readers"
            _capture_notice(
                f"mm: warning: Usage capture ({token}): {safe_str(' '.join(notices))} "
                f"Content sync continues. See {HOST_USAGE_CAPTURE_URL}"
            )
        if not rows:
            return outcome.finish("no-row")
        src = next(s for s in sources if s["name"] == "mm-events")
        phase = "append-failed"
        path = events.write_push_event(
            Path(src["path"]) / "events",
            config["device"]["id"],
            rows,
            strict=True,
            max_file_size=config["sync"]["max_file_size"],
        )
        if isinstance(path, events.EventAppendSkipped):
            return outcome.finish("max-file-size")  # Append already reported the remedy.
        assert path is not None
        outcome.appended = (path, rows[0])
        return outcome
    except Exception as e:
        outcome.finish(phase)
        core._sync_diagnostic(f"usage capture failed ({type(e).__name__})")
        if core._SYNC_OUTPUT.get() is not None:
            core._sync_diagnostic(_compact_sync_trace(e))
        _capture_notice(
            f"mm: warning: Usage capture ({phase}): {type(e).__name__}. "
            "Content sync continues. Repair the local events folder if needed and run mm diag; "
            f"the next attended mm push retries automatically. See {HOST_USAGE_CAPTURE_URL}"
        )
        return outcome.finish(phase)


def _warn_publication_evidence_unavailable(
    e: Exception, content_outcome: str, *, attempt: attemptlog.Attempt | None = None
) -> None:
    """Publication evidence failing must never change what the push already did."""
    if attempt is not None and attempt.outcome is not None:
        attempt.outcome.finish("unverified", "evidence-error")
    _capture_notice(
        "mm: warning: Usage capture (unverified: evidence-error): "
        f"publication evidence could not be read ({type(e).__name__}); check mm status. "
        f"{content_outcome} See {HOST_USAGE_CAPTURE_URL}"
    )


def _mm_events_files(manifest: dict | None) -> dict:
    if not isinstance(manifest, dict):
        return {}
    files = manifest.get("sources", {}).get("mm-events", {}).get("files", {})
    return files if isinstance(files, dict) else {}


def _stored_event_key(files: dict, rel: str) -> str | None:
    """Manifest key for this day file, including one APFS case variant.

    The writer always uses ``events/<file>``. A walker that opened
    ``include_dirs = ["Events"]`` can store that same inode under the
    config's spelling. Two case variants is ambiguous, so it does not match.
    """
    if rel in files:
        return rel
    folded = rel.casefold()
    matches = [key for key in files if isinstance(key, str) and key.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    return None


def _casefold_selected(rel: str, src: dict) -> bool:
    """Same selection as the walker, ignoring case in configured spellings.

    ``include_dirs = []`` still excludes the day file. ``["Events"]`` selects
    the writer's ``events/<file>`` on a case-insensitive volume without
    treating a stale manifest key as permission to ignore the config.
    """
    child = PurePosixPath(rel)
    child_parts = [part.casefold() for part in child.parts]
    for directory in src.get("include_dirs", []):
        parent = PurePosixPath(directory)
        if parent == PurePosixPath("."):
            return True
        parent_parts = [part.casefold() for part in parent.parts]
        if child_parts[: len(parent_parts)] == parent_parts and len(child_parts) > len(
            parent_parts
        ):
            return True
    return any(
        rel.casefold() == str(PurePosixPath(file)).casefold()
        for file in src.get("include_files", [])
    )


def _usage_publication_verdict(
    src: dict, path: Path, row: dict, manifest: dict | None
) -> tuple[str, str | None]:
    """Claim non-publication only from config or the accepted bytes themselves."""
    rel = f"events/{path.name}"
    if _manifest_is_excluded(rel, src.get("exclude_patterns", [])):
        return "not-published", "exclude-patterns"
    if not _casefold_selected(rel, src):
        return "not-published", "include-dirs"
    if manifest is None:
        raise ValueError("accepted manifest unavailable")
    files = _mm_events_files(manifest)
    stored = _stored_event_key(files, rel)
    if stored is None:
        return "not-published", "file-absent"
    revision = events.recorded_row_revision(path, row)
    if revision.reason:
        return "unverified", revision.reason
    if not events.capture_revision_in_manifest(manifest, stored, revision.digest):
        return "unverified", "revision-mismatch"
    return ("published", None) if revision.found else ("not-published", "row-missing")


def _publication_remedy(outcome: str, cause: str, path: Path) -> str:
    local_path = core._home_relative_path(path)
    if outcome == "unverified":
        return {
            "changed": "the day file changed while mm verified it; "
            "check publication read-only with mm status",
            "missing": "the day file was moved or deleted before verification; "
            "check mm status, and the next attended mm push writes a new row",
            "unreadable": f"mm could not read {local_path}; restore read access, "
            "then check mm status",
            "oversized-line": f"{local_path} holds a line over 16 MiB that mm did not write, "
            "so publication cannot be verified from this file; content sync is unaffected "
            "and mm status shows publication unverified for it",
            "revision-mismatch": "the day file changed after the push accepted it; "
            "check publication read-only with mm status",
        }[cause]
    return {
        "exclude-patterns": "mm-events exclude_patterns excludes this day file. "
        "Adjust mm-events exclude_patterns, then run mm push",
        "include-dirs": "mm-events include_dirs does not select events/. "
        "Add events/ to mm-events include_dirs, then run mm push",
        "file-absent": "the day file is absent from the accepted manifest; "
        "check mm-events source selection with mm diag before the next mm push",
        "row-missing": "the accepted day file does not contain the captured row. "
        "Repair the local day file (preserve a copy outside mm-events before repairing "
        "damaged JSONL), then run mm push",
    }[cause]


def _report_usage_publication(
    sources: list[dict],
    captured: tuple[Path, dict],
    manifest: dict | None,
    result: PushResult,
    *,
    verbose: bool,
    attempt: attemptlog.Attempt | None = None,
) -> bool:
    """Publication evidence is independent of content acceptance and command exit."""
    path, row = captured
    src = next(s for s in sources if s["name"] == "mm-events")
    outcome, cause = _usage_publication_verdict(src, path, row, manifest)
    if attempt is not None and attempt.outcome is not None:
        attempt.outcome.finish(outcome, cause)
    published = outcome == "published"
    try:
        if published:
            if verbose:
                console.print("Host usage published (manifest accepted).")
        else:
            assert cause is not None
            remedy = _publication_remedy(outcome, cause, path)
            content_outcome = (
                "Content changes were pushed."
                if result.content_changed
                else "User content was already up to date."
            )
            _capture_notice(
                f"mm: warning: Usage capture ({outcome}: {cause}): "
                f"{safe_str(remedy)}. {content_outcome} See {HOST_USAGE_CAPTURE_URL}"
            )
    except Exception:
        core._sync_diagnostic("mm: notice: could not print publication evidence; check mm status")
    return published


def _sync_update(config: dict, *, verbose: bool) -> None:
    try:
        upgrade.update_or_nudge(config, attended=True, diagnostics=verbose)
    except (Exception, KeyboardInterrupt):
        core._sync_warning("mm: notice: automatic update did not complete; run mm update")


def _sync_success(verb: str, result: PushResult | pullflow.PullResult | None) -> str:
    if verb == "push":
        result = result if isinstance(result, PushResult) else PushResult()
        published_files = result.published_content_files
        if published_files is None:
            published_files = result.content_files
        lead = (
            "no enabled sources"
            if result.no_sources
            else f"{core._count(published_files, 'content change', 'content changes')} published"
            if published_files
            else "metadata updated"
            if result.metadata_updated
            else "source selection updated"
            if result.selection_updated
            else "manifest repaired"
            if result.manifest_repaired
            else "no content changes"
        )
        parts = [lead]
        if result.host_usage_published:
            parts.append("usage published")
        if result.held:
            parts.append(f"{len(result.held)} held")
        return "Push: " + "; ".join(parts) + "."
    assert isinstance(result, pullflow.PullResult)
    lead = (
        "no enabled sources"
        if result.no_sources
        else "no peer devices"
        if result.no_peers
        else f"{core._count(result.total_applied, 'file', 'files')} applied"
        if result.total_applied
        else "no changes"
    )
    if result.total_conflicted:
        word = "copy" if result.total_conflicted == 1 else "copies"
        lead += f" ({result.total_conflicted} conflict {word})"
    if result.total_skipped:
        lead += f"; {result.total_skipped} skipped"
    if clause := pullflow.waiting_clause(result):
        lead += f"; {clause}"
    return "Pull: " + lead + "."


def _compact_sync_trace(exc: BaseException) -> str:
    """Trusted frame identifiers only: no messages, source text, locals or paths."""
    seen: set[int] = set()
    while (
        isinstance(exc, (typer.Exit, typer.Abort))
        and (exc.__cause__ or exc.__context__) is not None
        and id(exc) not in seen
    ):
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    frames = deque(maxlen=8)
    tb = exc.__traceback__
    package = Path(__file__).parent
    while tb is not None:
        code = tb.tb_frame.f_code
        path = Path(code.co_filename)
        if path.parent == package and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", code.co_name):
            frames.append(f"{path.name}:{code.co_name}:{tb.tb_lineno}")
        tb = tb.tb_next
    return f"{type(exc).__name__} ({attemptlog.storage_errno(exc) or 'no errno'}): " + " > ".join(
        frames
    )


def _sync_failure(verb: str, state: dict, error: BaseException | None) -> str:
    run = state.get("run", {})
    result = run.get("result")
    record = state.get("record", {})
    partial = isinstance(result, pullflow.PullResult) and result.total_failed and error is None
    cause = (
        "result-degraded"
        if partial
        else record.get("cause", run.get("cause", run.get("stage", "unexpected-error")))
    )
    interrupted = cause == "interrupted" or isinstance(error, (KeyboardInterrupt, typer.Abort))
    refused = (
        cause in ("lock-held", "snapshot-refused")
        or isinstance(error, typer.Exit)
        and error.exit_code == 3
    )
    kind = (
        "incomplete"
        if partial
        else "interrupted"
        if interrupted
        else "refused"
        if refused
        else "failed"
    )
    command = "mm diag"
    message = state.get("message", "")
    if partial:
        reason = (
            f"{core._count(result.total_applied, 'file', 'files')} applied, "
            f"{result.total_failed} failed"
        )
    elif isinstance(error, typer.Exit) and error.exit_code == 3:
        reason = "preflight predicted " + state.get("preflight", "conflicts or failures")
        command = (
            "mm diag" if state.get("preflight_failures") else "mm pull --conflict-mode keep-both"
        )
    elif cause == "lock-held":
        # LockError also covers permission/write failures: keep its own text.
        reason = message or "another mm operation holds the lock; wait for it to finish"
    elif cause == "authentication-error":
        reason = message or "authentication could not complete"
    elif message:
        # Known CLI refusals already have curated location/problem text. Keep the
        # problem, select its existing remedy, and avoid repeating command prose.
        if "newer mm" in message or "unsupported format" in message:
            command = "mm update"
        elif "mm migrate-config" in message:
            command = "mm migrate-config"
        elif "mm init" in message:
            command = "mm init"
        elif message.startswith("Device not found"):
            command = "mm devices"
        elif cause == "snapshot-refused":
            command = "mm push"
        # Keep the curated text intact (it may name an alternative remedy such
        # as mm disable-source); only drop a trailing step that repeats Run:.
        reason = message.split(" See https:")[0].strip()
        reason = re.sub(rf",? then (?:run |retry )?[`']?{re.escape(command)}[`']?\.?$", ".", reason)
    elif isinstance(error, MindMeldError) and str(error):
        # Curated library text; sanitized and bounded below.
        reason = str(error).split(" See https:")[0]
    else:
        reason = (
            "interrupted"
            if interrupted
            else type(error).__name__
            if error is not None
            else "command stopped"
        )
    if isinstance(result, pullflow.PullResult):
        effects = (
            f"{core._count(result.total_applied, 'file', 'files')} applied; "
            "completed changes remain"
        )
        if partial:
            effects = "completed changes remain; repair the reported failures before one retry"
        if clause := pullflow.waiting_clause(result):
            effects += f"; {clause}"
    elif isinstance(result, PushResult) and result.content_accepted:
        accepted = result.published_content_files
        if accepted is None:
            accepted = result.content_files
        effects = (
            f"{core._count(accepted, 'content change', 'content changes')} published; "
            "accepted changes remain"
        )
    elif (
        run.get("stage") in ("setup-error", "authentication-error")
        # Crypto-session setup fails before any upload or apply; a core error
        # keeps its partial result above, so a bare cause here means no effect.
        or (result is None and run.get("cause") in ("setup-error", "crypto-error"))
        # Pre-write refusals: unknown device, newer storage format.
        or (result is None and command in ("mm devices", "mm update"))
        or cause == "lock-held"
        or refused
    ):
        effects = "no content published" if verb == "push" else "no files applied"
    else:
        effects = "effects unconfirmed; reconcile before retrying"
    # One logical line; bound peer/config text without treating display as identity.
    reason = safety.safe_terminal_str(reason)[:384].rstrip(". ")
    return f"{verb.title()} {kind} [{cause}]: {reason}; {effects}. Run: {command}"


def _emit_sync_outcome(message: str, *, failed: bool) -> None:
    # Rich wrapping is deliberately bypassed. Flush both pipes before the last
    # write so stderr=STDOUT at process spawn preserves the observed order.
    for stream in (sys.stdout, sys.stderr):
        with suppress(Exception, SystemExit):
            stream.flush()
    stream = sys.stderr if failed else sys.stdout
    with suppress(Exception, SystemExit):
        stream.write(safety.safe_terminal_str(message) + "\n")
        stream.flush()


@contextmanager
def _sync_outcome(verb: str, *, verbose: bool, dry_run: bool):
    if dry_run:
        yield
        return
    progress = core._SyncProgress(enabled=not verbose)
    state = {"verbose": verbose, "diagnostics": events_tail.Degradations(), "progress": progress}
    token = core._SYNC_OUTPUT.set(state)
    error = None
    try:
        with events_tail.diagnostic_scope(core._sync_diagnostic):
            yield
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        error = exc
    finally:
        progress.stop()
        core._SYNC_OUTPUT.reset(token)
    result = state.get("run", {}).get("result")
    failed = (
        error is not None or isinstance(result, pullflow.PullResult) and result.total_failed > 0
    )
    if error is not None and verbose:
        with suppress(Exception, SystemExit):
            print(_compact_sync_trace(error), file=sys.stderr)
    _emit_sync_outcome(
        _sync_failure(verb, state, error) if failed else _sync_success(verb, result), failed=failed
    )
    if isinstance(error, typer.Exit) and error.exit_code == 3:
        raise typer.Exit(3)
    if failed:
        raise typer.Exit(1)


@contextmanager
def _attended_sync_run(verb: str, *, dry_run: bool, scope: dict | None = None):
    """Callback-local facts; finalization never changes the command's result."""
    run: dict = {"stage": "setup-error", "result": None, "attempt": None}
    state = core._SYNC_OUTPUT.get()
    if state is not None:
        state["run"] = run
    error: BaseException | None = None
    try:
        yield run
    except BaseException as exc:
        error = exc
        raise
    finally:
        if not dry_run:
            result = run["result"]
            attempt = run["attempt"]
            accepted = (
                result.content_accepted
                if isinstance(result, PushResult)
                else (bool(attempt.content_accepted) if attempt is not None else None)
            )
            counts = {}
            details = events_tail.Degradations()
            if result is None and state is not None:
                pending = state.get("events_degradations", ())
                if getattr(pending, "total", len(pending)):
                    counts["events_degradations"] = getattr(pending, "total", len(pending))
                    details.extend(pending)
            if result is not None:
                counts = {
                    name: getattr(result, name)
                    for name in attemptlog.RUN_COUNTS
                    if hasattr(result, name) and type(getattr(result, name)) is int
                }
                if isinstance(result, PushResult):
                    counts.update(
                        held=len(result.held),
                        events_degradations=getattr(
                            result.events_degradations, "total", len(result.events_degradations)
                        ),
                    )
                    details.extend(result.events_degradations)
            capture = attempt.outcome if attempt is not None else None
            capture_degraded = capture is not None and (
                capture.kind not in ("published", "no-row")
                and not (capture.kind == "prerequisites" and capture.cause == "no-reader")
                or any(
                    v not in ("contributed", "empty", "absent") for v in capture.readers.values()
                )
            )
            if capture_degraded:
                capture_detail = (
                    "usage capture published (incomplete reader coverage)"
                    if capture.kind == "published"
                    else f"usage capture {capture.kind} ({capture.cause or 'none'})"
                )
                details.append(capture_detail)
            if state is not None:
                extra = state["diagnostics"]
                details.extend(extra)
                if extra.total:
                    counts["events_degradations"] = min(
                        2**53, counts.get("events_degradations", 0) + extra.total
                    )
            degraded = capture_degraded or any(
                counts.get(k, 0)
                for k in (
                    "held",
                    "events_degradations",
                    "total_failed",
                    "total_skipped_unknown_source",
                    "durability_fsync_failures",
                    "corrupt_peer_count",
                    "maintenance_failures",
                )
            )
            cause = (
                "result-degraded"
                if degraded
                else "ok"
                if result is not None and not getattr(result, "no_sources", False)
                else "no-changes"
            )
            outcome = "degraded" if degraded else "success"
            number = None
            if error is not None:
                # Existing CLI errors may wrap a typed exception in typer.Exit.
                typed_errors = []
                typed = error
                seen_errors: set[int] = set()
                while id(typed) not in seen_errors:
                    seen_errors.add(id(typed))
                    typed_errors.append(typed)
                    if typed.__context__ is None:
                        break
                    typed = typed.__context__
                cause = run.get("cause") or run["stage"]
                if any(isinstance(exc, (KeyboardInterrupt, typer.Abort)) for exc in typed_errors):
                    cause = "interrupted"
                elif not run.get("cause") and run["stage"] == "command-refused":
                    if any(isinstance(exc, SnapshotError) for exc in typed_errors):
                        cause = "snapshot-refused"
                    elif any(isinstance(exc, CryptoError) for exc in typed_errors):
                        cause = "crypto-error"
                    elif any(isinstance(exc, (OSError, StorageError)) for exc in typed_errors):
                        cause = "storage-error"
                    elif not isinstance(error, (typer.Exit, MindMeldError)):
                        cause = "unexpected-error"
                outcome = "skipped" if cause == "lock-held" else "failed"
                number = run.get("errno") or attemptlog.storage_errno(error)
            try:
                record = attemptlog.make_run(
                    outcome=outcome,
                    cause=cause,
                    counts=counts,
                    content_accepted=accepted,
                    scope=scope,
                    degradations=details,
                    errno_name=number,
                )
                record["omitted_details"] = min(2**53, record["omitted_details"] + details.omitted)
                if state is not None:
                    state["record"] = record
                attemptlog.write_run(verb, record)
            except (Exception, SystemExit, KeyboardInterrupt) as exc:
                effect = (
                    "accepted"
                    if accepted
                    else "not accepted"
                    if accepted is False
                    else (
                        "partial"
                        if isinstance(result, pullflow.PullResult) and result.total_applied
                        else "unknown"
                    )
                )
                code = attemptlog.storage_errno(exc) or (
                    "contended" if isinstance(exc, LockContended) else "unavailable"
                )
                with suppress(Exception, SystemExit):
                    print(
                        safety.safe_terminal_str(
                            "mm: notice [run-evidence-write-unavailable]: "
                            f"{verb} content {effect}; "
                            f"run evidence unavailable ({code}). "
                            "Do not repeat sync solely to record it; "
                            "restore local access/storage if needed, then run: mm diag"
                        ),
                        file=sys.stderr,
                    )


@app.command()
def push(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Preview what mm push would publish or delete. Changes nothing except "
            "the local lock file (no uploads, config writes, pull-history rows, "
            "upgrade checks or new directories); setup the real push would perform "
            "is reported, not done. Never scans or warms host usage. Exits 1 if the preview stops."
        ),
    ),
) -> None:
    """Push selected agent context to storage.

    Publishes only after a complete source scan. Unreadable selected files
    refuse the push and preserve the previous snapshot.

    Attended pushes also refresh and publish host usage, even when user content
    is up to date. Requires mm-events and a consented host reader.
    Capture runs before content sync. Host sweep and inline warm share at
    most half of the capture budget (default five seconds), and both readers
    can be cold. Budgets are not hard timeouts.
    Content-changing pushes also run GC on this device's orphaned blobs;
    usage-only pushes leave obsolete blobs for the next content push or mm gc.

    Exit 0 means content sync succeeded, regardless of usage coverage; check
    mm status for recorded capture/publication evidence. Exit 1 means sync or
    required maintenance stopped; exit 2 means invalid arguments.
    """
    with _sync_outcome("push", verbose=verbose, dry_run=dry_run):
        with (
            _attended_sync_run("push", dry_run=dry_run) as run,
            pullhistory.command_scope(attended=True),
        ):
            config = core._get_config(read_only=dry_run)
            core._maybe_prompt_migration(config, read_only=dry_run)
            # Re-load in case the migration prompt mutated config on disk so the
            # current command sees the new exclude_patterns.
            if not dry_run:
                config = core._get_config(read_only=False)
            run["stage"] = "authentication-error"
            passphrase = core._get_passphrase_or_exit()
            run["stage"] = "command-refused"
            pending: list[str] = []
            refusal_suffix = core.DRY_RUN_REFUSAL if dry_run else ""
            attempt = attemptlog.Attempt()
            run["attempt"] = attempt
            core_finished = False
            stop_cause = "error"
            stop_errno = None

            try:
                acquire_lock()
            except LockError as e:
                run["cause"] = "lock-held"
                core._error(str(e))

            try:
                core._start_sync_progress("Unlocking storage")
                if not dry_run and not pullhistory.failed():
                    pullhistory.record_failure(statedb.recover_if_needed())
                backend = get_backend(config)
                try:
                    memory_kb = core._init_crypto_session(
                        backend, passphrase, config, read_only=dry_run, pending=pending
                    )
                except MindMeldError as e:
                    run["cause"] = "crypto-error" if isinstance(e, CryptoError) else "setup-error"
                    core._error(str(e) + refusal_suffix)
                try:
                    # One history transaction per push; per-source batches join it.
                    with pullhistory.batch():
                        result = _push_core(
                            config,
                            passphrase,
                            memory_kb,
                            verbose,
                            dry_run,
                            preview_notes=pending,
                            attended=True,
                            attempt=attempt,
                            diagnostics=verbose or dry_run,
                            result_observer=lambda result: run.__setitem__("result", result),
                        )
                    run["result"] = result
                    core_finished = True
                except SnapshotError as e:
                    stop_cause = "snapshot-refused"
                    run["cause"] = stop_cause
                    core._error(str(e) + refusal_suffix)
                except (OSError, MindMeldError) as e:
                    if isinstance(e, (OSError, StorageError)):
                        stop_cause = "storage-error"
                        stop_errno = attemptlog.storage_errno(e)
                    run["cause"] = (
                        stop_cause
                        if stop_cause != "error"
                        else "crypto-error"
                        if isinstance(e, CryptoError)
                        else "command-refused"
                    )
                    run["errno"] = stop_errno
                    if dry_run:
                        core._error(str(e) + refusal_suffix)
                    core._error(
                        f"Push (push-failed): {e}. "
                        f"Repair the sync failure. See {HOST_USAGE_CAPTURE_URL}"
                    )

                if dry_run:
                    for note in pending:
                        console.print(safe_str(note))
                    console.print(f"\n[bold]{core.DRY_RUN_COMPLETE}[/bold]")
                    console.print(
                        "Not previewed: host-usage capture, the mm-events activity row a real push "
                        "appends, post-push GC of orphaned blobs, and upload re-reads."
                    )

                # Usage-only publication leaves obsolete day-file blobs for the next
                # content-changing push or explicit mm gc, avoiding an own-blob sweep per refresh.
                if not dry_run and result and result.content_changed:
                    try:
                        core._sync_progress_phase("Cleaning up orphaned blobs")
                        gc_count = _do_gc(
                            config,
                            passphrase,
                            memory_kb,
                            dry_run=False,
                            verbose=False,
                            emit_retention_summary=False,
                        )
                        if gc_count and verbose:
                            console.print(f"  GC: deleted {gc_count} orphaned blobs.")
                    except typer.Exit:
                        result.maintenance_failures += 1
                        if not result.host_usage_published:
                            raise
                        problem = (core._SYNC_OUTPUT.get() or {}).get("message", "GC refused")
                        with core._pause_sync_progress():
                            stderr_console.print(
                                "mm: warning: Host usage was published; "
                                f"post-publication GC stopped. {safe_str(problem)} "
                                "Run mm gc for details."
                            )
                    except (OSError, MindMeldError) as e:
                        result.maintenance_failures += 1
                        with core._pause_sync_progress():
                            stderr_console.print(
                                f"mm: warning: GC skipped ({safe_str(e)}). Run mm gc for details."
                            )
            except (KeyboardInterrupt, typer.Abort):
                # Only a storage-error record may carry errno.
                stop_cause, stop_errno = "interrupted", None
                raise
            finally:
                core._stop_sync_progress()
                try:
                    if not dry_run and attempt.outcome is not None:
                        if not core_finished:
                            attempt.stopped(stop_cause, errno_name=stop_errno)
                        try:
                            attemptlog.write(attempt.outcome)
                        except (Exception, SystemExit) as e:
                            core._sync_warning(
                                "mm: notice: could not durably record this attended "
                                "capture attempt at "
                                f"{safe_str(core._home_relative_path(attemptlog.record_path()))} "
                                f"({type(e).__name__}); check mm status"
                            )
                finally:
                    core._release_sync_lock()
                # Failed attended attempts need the update or nudge too; previews never
                # fetch, install or write it.
                if not dry_run and not isinstance(
                    sys.exception(), (KeyboardInterrupt, typer.Abort)
                ):
                    _sync_update(config, verbose=verbose)


def _ensure_device_registered(
    backend: LocalBackend,
    device_id: str,
    device_name: str,
    *,
    dry_run: bool = False,
) -> None:
    """Self-heal a missing device entry before push runs (Track 5D Task 2b).

    If the device's storage entry is absent, recreate it. Two scenarios
    converge here:
      * SIGKILL/OOM crash between `register_device` and `save_config` in
        a v0.9.4+ init — the order swap (Task 2) accepts this as a rare
        cosmetic event for the *next* init, but a user who got past init
        with a half-state lands here.
      * Pre-v0.9.4 init crash in the v0.8.15..v0.9.3 inverted window:
        local config has a `device_id` that storage's `devices/` doesn't
        contain. Without this hook those users push manifests under an
        ID no peer recognizes, silently. Self-heal makes the fix retro-
        active: first push after upgrade re-registers the device.

    `dry_run=True` is a no-op (codex review 2026-04-25): self-heal does
    a backend.put via register_device, which would violate `mm push
    --dry-run`'s "preview only" contract. The real `mm push` afterwards
    does the heal.

    register_device is idempotent on the storage write and the entry
    carries no `last_seen` until the first successful push completes,
    so a brief race against a concurrent init is benign.

    On register failure (transient iCloud StorageError), emit a stderr
    breadcrumb before re-raising. The breadcrumb is load-bearing for
    autopush, whose generic `except Exception` would otherwise swallow
    the failure and silently no-op every push — `mm: warning:` matches
    the visible-failure contract for degraded-state signals.
    """
    if dry_run:
        return
    if backend.exists(device_key(device_id)):
        return
    try:
        register_device(backend, device_id, device_name)
    except Exception as e:
        with core._pause_sync_progress():
            stderr_console.print(
                f"mm: warning: device entry self-heal failed ({type(e).__name__}): {e}"
            )
        raise


def _print_usage_push_mode(result: PushResult) -> None:
    if not result.content_changed:
        console.print(
            "Refresh mode: usage-only; user content already up to date, Git cursor unchanged. "
            "Run mm recapture 30d for Git history."
        )


def _push_result_or_none(
    events_degradations: list[str],
    dry_run: bool,
    held: list[tuple[str, str, str]] | None = None,
) -> PushResult | None:
    """Preserve forensic degradations and held files on no-op exits without inventing a capture."""
    if (getattr(events_degradations, "total", len(events_degradations)) or held) and not dry_run:
        return PushResult(events_degradations=events_degradations, held=list(held or []))
    return None


# ── held files (Track 75A) ────────────────────────────────────────────

_HELD_REASON_WORDS = {
    "changed": "still changing",
    "grew": "still growing",
    "vanished": "gone while being read",
}
_HELD_PATHS_SHOWN = 3
_HELD_BREADCRUMB_BUDGET = 300


def _held_list(held_files: Mapping[str, Mapping[str, str]]) -> list[tuple[str, str, str]]:
    """Flatten source -> rel -> reason into the sorted ``PushResult.held`` form."""
    return sorted(
        (name, rel, reason) for name, rels in held_files.items() for rel, reason in rels.items()
    )


def _held_count_phrase(held: list[tuple[str, str, str]], state: str = "not yet synced") -> str:
    """Count phrase: ``2 files not yet synced (still changing)``, reasons split when mixed."""
    noun = "file" if len(held) == 1 else "files"
    counts = Counter(reason for _name, _rel, reason in held)
    if len(counts) == 1:
        detail = _HELD_REASON_WORDS.get(next(iter(counts)), "still changing")
    else:
        detail = ", ".join(
            f"{count} {_HELD_REASON_WORDS.get(reason, reason)}"
            for reason, count in sorted(counts.items())
        )
    return f"{len(held)} {noun} {state} ({detail})"


def _print_held_outcome(
    held: list[tuple[str, str, str]], *, verbose: bool, preview: bool = False
) -> None:
    """Attended outcome for held files: count, paths, and what happens next.

    Lists up to three paths with ``+N more``; ``--verbose`` lists all. Rich
    output, so every path goes through ``safe_str``.
    """
    if not held:
        return
    phrase = _held_count_phrase(
        held, "would be held, not yet synced" if preview else "not yet synced"
    )
    shown = held if verbose else held[:_HELD_PATHS_SHOWN]
    paths = [f"{safe_str(name)}:{safe_str(rel)}" for name, rel, _reason in shown]
    more = len(held) - len(shown)
    if verbose:
        console.print(f"  [yellow]{phrase}:[/yellow]")
        for path in paths:
            console.print(f"    {path}")
    else:
        tail = f" (+{more} more)" if more else ""
        console.print(f"  [yellow]{phrase}:[/yellow] {', '.join(paths)}{tail}")
    console.print(
        "  Their previous versions (if any) stay published; they go with a later push."
        if not preview
        else "  A real push would leave their previous versions (if any) published."
    )
    if any(reason == "vanished" for _name, _rel, reason in held):
        console.print(
            "  A file that vanished is recorded as deleted on a later push if it is still gone."
        )


def _held_breadcrumb_detail(held: list[tuple[str, str, str]], budget: int) -> str:
    """One-line autopush breadcrumb fragment: count, up to 3 paths, ``+N more``.

    Prefer complete paths; truncate the first if none fits. The count survives
    even when existing degradations exhaust the budget. Sanitized for plain
    stderr/status rendering (single line).
    """
    lead = _held_count_phrase(held, "held, not yet synced")
    for keep in range(min(_HELD_PATHS_SHOWN, len(held)), 0, -1):
        paths = ", ".join(
            safety.safe_terminal_str(f"{name}:{rel}") for name, rel, _reason in held[:keep]
        )
        more = len(held) - keep
        text = f"{lead}: {paths}" + (f", +{more} more" if more else "")
        if len(text) <= budget:
            return text
    more = len(held) - 1
    tail = f", +{more} more" if more else ""
    path_budget = budget - len(lead) - len(": ") - len(tail)
    if held and path_budget > 1:
        name, rel, _reason = held[0]
        path = safety.safe_terminal_str(f"{name}:{rel}")
        return f"{lead}: {path[: path_budget - 1]}…{tail}"
    return lead


def _held_blocks_recapture(held: list[tuple[str, str, str]]) -> bool:
    """True when a recapture's own mm-events day file did not go out this push."""
    return any(name in MM_INTERNAL_SOURCE_NAMES for name, _rel, _reason in held)


def _inject_held_entries(
    local_manifest: dict,
    prior_manifest: dict | None,
    held: Mapping[str, Mapping[str, str]],
    candidates: Mapping[str, Iterable[str]],
    *,
    exclude_map: Mapping[str, list[str]],
    skip_prefixes: Mapping[str, list[str]],
    frozen: Mapping[str, set[str]],
    include_map: Mapping[str, list[str]] | None = None,
) -> dict[str, dict[str, str]]:
    """Resolve a strict scan's held files into the manifest about to be published.

    Each held path keeps its accepted prior entry in ``local_manifest`` (parity
    with the frozen-entry path: no blob-existence probe, the entry was accepted
    as-is), so it reads as unchanged, uploads nothing and mints no tombstone. A
    never-published path is simply left out. A path that is excluded, frozen or
    tombstoned is not waiting and is pruned. ``candidates`` are enumeration
    entries that vanished before their stat: re-lstat each, and refuse unless it
    is still gone and was not a published directory. Returns the surviving holds.

    Run after the unselected/exclude filters, symlink prior-drop and freeze, and
    before the omission proof and tombstones. The carried entry is the *accepted
    prior* only: a first-scan revision's blob is not in storage until the upload
    loop, so carrying it would publish a dangling entry.
    """
    prior_sources = (prior_manifest or {}).get("sources", {})
    tombstones = (prior_manifest or {}).get("tombstones", {})
    local_sources = local_manifest.get("sources", {})
    surviving: dict[str, dict[str, str]] = {}
    for name in sorted(set(held) | set(candidates)):
        local_src = local_sources.get(name)
        if local_src is None:
            continue
        files = local_src["files"]
        prior_files = prior_sources.get(name, {}).get("files", {})
        patterns = exclude_map.get(name)
        prefixes = skip_prefixes.get(name)
        includes = (include_map or {}).get(name)
        reasons = dict(held.get(name, {}))
        directories: set[str] | None = None
        for rel in sorted(candidates.get(name, ())):
            if rel in reasons or _manifest_is_excluded(rel, patterns, prefixes, includes):
                continue
            if directories is None:
                directories = {
                    "/".join(parts[:i])
                    for path in prior_files
                    for parts in (path.split("/"),)
                    for i in range(1, len(parts))
                }
            _validate_vanished_candidate(name, rel, Path(local_src["base_path"]), directories)
            reasons[rel] = "vanished"
        for rel, reason in sorted(reasons.items()):
            if (
                rel in files
                or rel in frozen.get(name, ())
                or _manifest_is_excluded(rel, patterns, prefixes, includes)
                or is_tombstoned(name, rel, tombstones)
            ):
                continue
            if (prior := prior_files.get(rel)) is not None:
                files[rel] = dict(prior)
            surviving.setdefault(name, {})[rel] = reason
    return surviving


def _validate_vanished_candidate(
    source: str, rel: str, base_path: Path, directories: set[str]
) -> None:
    """Keep a vanished enumeration entry a hold only when its shape is provable.

    The entry's type is unknown (its stat failed). Published descendants prove
    a prior directory, even if a merged conflict copy also has an exact file
    entry: a vanished subtree must refuse. Otherwise, a still-missing exact
    prior file can hold at the prior; a never-published file is omitted.
    Present again in any form, or unreadable: refuse.
    """
    try:
        os.lstat(base_path / rel)
    except (FileNotFoundError, NotADirectoryError):
        if rel not in directories:
            return
        problem, cause = "disappeared while being read", None
    except OSError as e:
        problem, cause = "could not be read", os_error_cause(e)
    else:
        problem, cause = "disappeared while being read", None
    raise SnapshotError(
        snapshot_refusal(
            source=source,
            rel_path=rel,
            problem=problem,
            cause=cause,
            next_action=(
                "Wait for the writer to finish, then run mm push."
                if cause is None
                else "Restore read access, then run mm push."
            ),
        )
    )


_PUSH_GROWTH_WARNING_FILES = 1000


def _warn_push_growth(source_diffs: list) -> None:
    """Warn once about large additions, using observed counts before uploads."""
    counts: Counter[str] = Counter()
    for name, _local, _remote, diff in source_diffs:
        for rel in diff.new:
            subtree = "/".join(PurePosixPath(rel).parent.parts[:2]) or "."
            counts[f"{name}:{subtree}"] += 1
    large = sorted(
        ((path, count) for path, count in counts.items() if count > _PUSH_GROWTH_WARNING_FILES),
        key=lambda item: (-item[1], item[0]),
    )
    if large:
        detail = "; ".join(f"{path} ({count} new files)" for path, count in large[:3])
        with core._pause_sync_progress():
            print(
                "mm: warning: large sync growth: "
                + safety.safe_terminal_str(detail)
                + ". Check these subtrees before adding more files to sync.",
                file=sys.stderr,
            )


def _push_core(
    config: dict,
    passphrase: str,
    memory_kb: int,
    verbose: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    *,
    preview_notes: list[str] | None = None,
    attended: bool = False,
    attempt: attemptlog.Attempt | None = None,
    diagnostics: bool | None = None,
    result_observer: Callable[[PushResult], None] | None = None,
) -> PushResult | None:
    """Core push logic shared by push, autopush, and recapture.

    When quiet=True, suppresses all rich console output (for autopush).
    ``diagnostics`` controls presentation only; ``result_observer`` marks the
    attended outcome owner: it receives the result at manifest acceptance, a
    no-source run returns PushResult(no_sources=True), and the legacy summary
    lines are left to the caller's single outcome line.
    Returns PushResult on success, None if nothing to push or dry_run. A no-op
    push that captured a host row, or that carries events degradations, returns
    a PushResult with ``content_changed=False`` instead of None.
    Only the attended push command captures before the manifest build.
    Incomplete scans raise ``SnapshotError``; callers must catch it
    (interactive push and recapture via ``_error``, autopush via
    ``_auto_command_scope``). A single file that changes, vanishes or grows while
    mm reads it is re-read once and, if still changing, held at its previously
    published version (``PushResult.held``) instead of refusing the push; a push
    recovering a corrupt or conflict-copy manifest has no accepted prior to hold
    at and still refuses.
    """
    show = not quiet and (diagnostics if diagnostics is not None else True)
    start = time.time()
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)
    core._sync_progress_phase("Checking remote manifest")
    _ensure_device_registered(backend, device_id, device_name, dry_run=dry_run)

    # Resolve under the lock, then recover prior sources before capturing so
    # consent and readers are final. Only real push bootstraps mm-events here.
    resolution = resolve_sources(config, strict=True, bootstrap=not dry_run)
    events_degradations: list[str] = events_tail.Degradations()
    state = core._SYNC_OUTPUT.get()
    if state is not None:
        state["events_degradations"] = events_degradations
    available_names = {src["name"] for src in resolution.available}
    skipped_internal = [
        src
        for src in resolution.selected
        if src["name"] in MM_INTERNAL_SOURCE_NAMES
        and src["name"] not in available_names
        and not _config_module._is_default_mm_events_path(src["path"])
    ]
    for src in skipped_internal:
        message = _config_module._missing_custom_mm_events_message(src["path"])
        with core._pause_sync_progress():
            print(f"mm: warning: {safety.safe_terminal_str(message)}", file=sys.stderr)
        events_degradations.append(message)
    if skipped_internal:
        resolution = replace(
            resolution,
            selected=[src for src in resolution.selected if src not in skipped_internal],
        )
    if preview_notes is not None:
        for src in resolution.selected:
            if src["name"] in resolution.would_create:
                preview_notes.append(f"mm push will create {src['path']} for the mm-events source.")
    sources = list(resolution.available)
    may_create = skill_link.consented_agent_keys(config, sources)

    # Preserve skill maintenance before the no-sources return. A changed store
    # version/payload refreshes before the 24h link-drift gate. Preview skips
    # installation, matching _ensure_device_registered's write-free contract.
    # The gate and the installer MUST receive the same may_create: a
    # declined row never gets its success marker touched, so an unfiltered
    # gate stays open and runs the full installer prologue on every push.
    if not dry_run:
        try:
            if skill_link._skill_links_check_due(may_create=may_create):
                skill_link._ensure_retro_skill_links(
                    dry_run=False,
                    allow_mutate=not quiet,
                    may_create=may_create,
                )
        except Exception as e:
            with core._pause_sync_progress():
                stderr_console.print(
                    f"mm: notice: retro-fleet skill installation failed: "
                    f"{type(e).__name__}: {safe_str(e)}"
                )
    if not resolution.selected and not skipped_internal:
        msg = "no sync sources found. Run 'mm init' to configure."
        with core._pause_sync_progress():
            if quiet:
                print(f"mm: warning: {msg}", file=sys.stderr)
            else:
                console.print(f"[yellow]Warning:[/yellow] {msg}")
        return (
            PushResult(no_sources=True, events_degradations=events_degradations)
            if result_observer is not None
            else _push_result_or_none(events_degradations, dry_run)
        )

    # Fetch remote manifest (tri-state: ok / missing / corrupt).
    # `fetch.manifest` (when ok) is pre-normalized via load_manifest;
    # _recover_prior_manifest's sidecar/peer paths emit the same shape.
    fetch = core._fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    if fetch.newer_version is not None:
        # Heal would replace this Mac's newer manifest with a v2 snapshot.
        core._error(core._newer_format_message(fetch.newer_version, "manifest"))
    remote_manifest = core._recover_prior_manifest(
        fetch, backend, device_id, passphrase, memory_kb, quiet=quiet, dry_run=dry_run
    )
    advertised_prior_sources = set((remote_manifest or {}).get("sources", {}))
    # A manifest held only by conflict copies is rewritten like a corrupt one,
    # so GC's "run mm push" remedy works without a content change.
    recovering_from_corrupt = (
        fetch.status == "corrupt" or fetch.conflict_copies_only or fetch.vanished
    )
    resolution = core._retain_prior_default_sources(resolution, remote_manifest, config)
    sources = _include_prior_host_if_needed(
        config,
        resolution,
        remote_manifest,
        sources,
        "muse",
    )
    sources = _include_prior_host_if_needed(
        config,
        resolution,
        remote_manifest,
        sources,
        "agy",
    )
    intended_names = {src["name"] for src in resolution.selected}
    intended_names.update(src["name"] for src in sources)
    _refuse_unavailable_selected_sources(resolution, remote_manifest, sources)
    if not sources and not skipped_internal:
        msg = "no sync sources found. Run 'mm init' to configure."
        with core._pause_sync_progress():
            if quiet:
                print(f"mm: warning: {msg}", file=sys.stderr)
            else:
                console.print(f"[yellow]Warning:[/yellow] {msg}")
        return (
            PushResult(no_sources=True, events_degradations=events_degradations)
            if result_observer is not None
            else _push_result_or_none(events_degradations, dry_run)
        )

    # Evaluate prerequisites against the final set, including recovered Grok.
    # Keep skipped custom roots in this view so "unavailable" is not "disabled".
    captured = None
    capture_attempted = attended and not dry_run
    if capture_attempted:
        captured = attemptlog.CaptureOutcome()
        if attempt is not None:
            attempt.outcome = captured
        captured = _capture_attended_usage(
            config,
            sources,
            [*resolution.selected, *skipped_internal],
            verbose=verbose,
            outcome=captured,
        )
    host_row_appended = captured is not None and captured.appended is not None
    retro_catchup_pending = attended and not dry_run and _retro_catchup_pending(sources, device_id)
    skipped: list[tuple[str, str]] = []
    skipped_repos: set[str] = set()
    nested_roots: dict[str, set[str]] = {}
    # Track 75A: a file still changing after its one re-read is held at its
    # accepted prior entry (or left out if never published) instead of refusing
    # the push. Holding needs an accepted prior, so a push recovering a corrupt
    # or conflict-copy manifest passes no collector and a transient refuses.
    hold_eligible = not recovering_from_corrupt
    held: dict[str, dict[str, str]] | None = {} if hold_eligible else None
    held_candidates: dict[str, set[str]] | None = {} if hold_eligible else None
    held_files: dict[str, dict[str, str]] = {}

    def on_skip(path: str, reason: str) -> None:
        if reason == manifest.NESTED_REPO_SKIP_REASON:
            # Autopush stays silent; attended push, preview, status and diag report it.
            if show and path not in skipped_repos:
                skipped_repos.add(path)
                with core._pause_sync_progress():
                    print(
                        "mm: notice: skipped: nested git repository "
                        f"{safety.safe_terminal_str(path)}",
                        file=sys.stderr,
                    )
            return
        skipped.append((path, reason))
        if verbose and not quiet:
            console.print(f"  [dim]skipped: {safe_str(path)} ({safe_str(reason)})[/dim]")

    # Scan before the activity tail. A captured host row can itself require
    # an upload, but only user-content changes then warrant activity capture.
    # With no host row, the existing substantive-change gate stays in force.
    core._sync_progress_phase("Scanning sources")
    if show:
        console.print("[bold]Building manifest...[/bold]")
    local_manifest = build_manifest_v2(
        device_id,
        device_name,
        sources,
        max_file_size,
        on_skip,
        strict=True,
        nested_roots=nested_roots,
        held=held,
        held_candidates=held_candidates,
    )

    total_file_count = sum(
        len(src_data["files"]) for src_data in local_manifest["sources"].values()
    )
    if show:
        console.print(
            f"  {total_file_count} files scanned across {len(local_manifest['sources'])} source(s)"
        )
        if skipped:
            console.print(f"  [yellow]{len(skipped)} files skipped[/yellow]")

    # Consumer-boundary filters. Strip from prior_manifest BOTH (1) paths
    # the local config now excludes via per-source `exclude_patterns` and
    # (2) entire sources no longer selected (disabled or retired).
    # Both filters apply to the ok-fetch path AND the recovery branches
    # (sidecar prior state, peer-tombstone aggregation).
    #
    # Without (1): generate_tombstones emits a deletion tombstone for
    # every newly-excluded path on first post-migration push (the
    # 2026-04-24 first-pull regression).
    #
    # Without (2): generate_tombstones emits a deletion tombstone for
    # every file in a newly-disabled source on first post-disable push,
    # propagating fleet-wide data loss. v0.10.0 source-toggle invariant;
    # mirror of (1)'s pattern. See docs/designs/source-toggle.md.
    #
    # Order matters: unselected first, then exclude. Dropping the
    # whole source first avoids walking a soon-to-be-dropped source's
    # exclude_patterns for nothing. Tombstones stay (asymmetric filter).
    exclude_map, skip_prefixes = core._build_exclude_map(config, sources, strict=True)
    include_map = core._build_include_map(config, sources)
    frozen: dict[str, set[str]] = {}
    if remote_manifest is not None:
        remote_manifest = _filter_unselected_sources(remote_manifest, intended_names)
        remote_manifest = core._filter_excluded_paths(
            remote_manifest, exclude_map, skip_prefixes, include_map
        )
        # Freeze before the symlink filter: entries below a checkout root are
        # kept as published without inspecting what replaced them on disk.
        frozen = core._freeze_nested_checkout_entries(
            local_manifest,
            remote_manifest,
            nested_roots,
            # A recovered prior (sidecar, peers, or conflict copies merged with
            # the sidecar) was never accepted as-is: freeze only entries whose
            # blob is still stored, and exclude the rest.
            keep=None
            if fetch.is_ok and not fetch.conflict_copies_only
            else lambda info: backend.exists(blob_key(device_id, info["sha256"])),
        )
        remote_manifest = _drop_unfrozen_checkout_files(remote_manifest, nested_roots, frozen)
        # Frozen checkouts are kept as published: never probe beneath their roots.
        remote_manifest = core._filter_symlinked_paths(
            remote_manifest, sources, strict=True, exempt_roots=nested_roots
        )
        # Held files keep their accepted prior entry before the omission proof
        # sees them as omitted and before tombstones are generated.
        if held is not None and held_candidates is not None:
            held_files = _inject_held_entries(
                local_manifest,
                remote_manifest,
                held,
                held_candidates,
                exclude_map=exclude_map,
                skip_prefixes=skip_prefixes,
                frozen=frozen,
                include_map=include_map,
            )
        proof_sources = sources
        if dry_run and resolution.would_create:
            # Missing default roots truthfully preview deletions.
            proof_sources = [s for s in sources if s["name"] not in resolution.would_create]
        core._prove_omitted_paths_absent(
            local_manifest, remote_manifest, proof_sources, max_file_size=max_file_size
        )
    elif held is not None and held_candidates is not None:
        # First push: no prior entry to keep, so a held file is simply left out.
        held_files = _inject_held_entries(
            local_manifest,
            None,
            held,
            held_candidates,
            exclude_map=exclude_map,
            skip_prefixes=skip_prefixes,
            frozen=frozen,
            include_map=include_map,
        )

    # Generate tombstones for files that disappeared since last push
    tombstones = generate_tombstones(local_manifest, remote_manifest, device_id)
    local_manifest["tombstones"] = tombstones

    # Only the REAL remote manifest drives the diff (avoid re-uploading every
    # file just because we're recovering from corruption via the sidecar).
    real_remote = fetch.manifest if fetch.is_ok else None
    remote_sources = real_remote.get("sources", {}) if real_remote else {}

    # Substantive-change gate (v0.12.2). Run BEFORE the events tail so
    # truly empty pushes don't write an mm-push event row that becomes
    # the only "change" pushed. Counts ANY source diff (user OR mm-events
    # \u2014 the latter catches an un-flushed prior push that wrote an event
    # but failed mid-upload). Pre-v0.12.2: events tail fired at HEAD of
    # _push_core unconditionally, so every `mm push` reported at least
    # the events-file modification.
    source_diffs = list(core.iter_source_diffs(local_manifest, remote_sources, skip_unchanged=True))
    if fetch.is_ok:
        # Without an accepted manifest of our own, everything (including
        # content already fleet-wide) counts as new.
        _warn_push_growth(source_diffs)
    has_substantive = bool(source_diffs)
    # mtime-only republish leg (v0.12.6): sha256-equal files with drifted
    # mtime must still trigger a manifest upload so peers see authoritative-
    # local claims from `mm resolve` (l)ocal. See `_has_mtime_only_changes_vs_remote`.
    has_mtime_only = (not has_substantive) and core._has_mtime_only_changes_vs_remote(
        local_manifest, remote_sources
    )
    has_selection_change = set(local_manifest["sources"]) != advertised_prior_sources
    # Three independent facts: byte diffs, forward-only mtime drift, and
    # selection in BOTH directions. Host rows make has_substantive true even
    # during a user-file mtime republish, so do not gate this on has_mtime_only.
    content_diffs = [d for d in source_diffs if d[0] not in MM_INTERNAL_SOURCE_NAMES]
    content_changed = bool(content_diffs) or bool(
        (set(local_manifest["sources"]) ^ advertised_prior_sources) - MM_INTERNAL_SOURCE_NAMES
    )
    if not content_changed:
        content_changed = any(
            core._has_mtime_only_changes_vs_remote(
                local_manifest, remote_sources, source_filter=name
            )
            for name in local_manifest["sources"]
            if name not in MM_INTERNAL_SOURCE_NAMES
        )
    content_files = sum(len(d.new) + len(d.modified) + len(d.deleted) for *_, d in content_diffs)
    if (
        not has_substantive
        and not has_mtime_only
        and not recovering_from_corrupt
        and not has_selection_change
        and not retro_catchup_pending
    ):
        scan_held = _held_list(held_files)
        if show and (dry_run or result_observer is None):
            if scan_held:
                lead = "Nothing would be pushed" if dry_run else "Nothing pushed"
                console.print(
                    f"[yellow]{lead}; {len(scan_held)} "
                    f"{'file' if len(scan_held) == 1 else 'files'} not yet synced.[/yellow]"
                )
                _print_held_outcome(scan_held, verbose=verbose, preview=dry_run)
            else:
                console.print("[green]Nothing to push \u2014 everything is up to date.[/green]")
        elif show and scan_held:
            # Attended outcome owner: the final line carries the count; verbose names them.
            _print_held_outcome(scan_held, verbose=verbose)
        if captured is not None and captured.appended is not None:
            result = PushResult(events_degradations=events_degradations, held=scan_held)
            if result_observer is not None:
                result_observer(result)
            try:
                result.host_usage_published = _report_usage_publication(
                    sources,
                    captured.appended,
                    remote_manifest,
                    result,
                    verbose=verbose,
                    attempt=attempt,
                )
            except Exception as e:
                _warn_publication_evidence_unavailable(
                    e, "User content was already up to date.", attempt=attempt
                )
            try:
                if show:
                    _print_usage_push_mode(result)
            except Exception as e:
                print(
                    "mm: notice: could not print usage refresh mode "
                    f"({type(e).__name__}); check mm status",
                    file=sys.stderr,
                )
            return result
        return _push_result_or_none(events_degradations, dry_run, scan_held)

    # OK, this push will upload bytes. Run the events tail now to capture
    # the cursor + git/sessions snapshots, then re-walk mm-events to fold
    # the just-written event row into local_manifest. dry_run still gates
    # the tail's own writes; the re-walk reads existing on-disk state.
    # Skipped entirely when no mm-internal source is selected for this push.
    if any(src["name"] in MM_INTERNAL_SOURCE_NAMES for src in sources):
        core._sync_progress_phase("Recording activity")
        events_degradations.extend(
            events_tail._run_events_tail(
                config,
                sources,
                device_id,
                dry_run=dry_run,
                quiet=quiet,
                suppress_host_capture=capture_attempted,
                capture_activity=retro_catchup_pending or not host_row_appended or content_changed,
            )
        )
    if not dry_run:
        mm_internal_cfgs = [s for s in sources if s["name"] in MM_INTERNAL_SOURCE_NAMES]
        if mm_internal_cfgs:
            rescan_held: dict[str, dict[str, str]] | None = {} if hold_eligible else None
            rescan_candidates: dict[str, set[str]] | None = {} if hold_eligible else None
            events_manifest = build_manifest_v2(
                device_id,
                device_name,
                mm_internal_cfgs,
                max_file_size,
                on_skip,
                strict=True,
                nested_roots=nested_roots,
                held=rescan_held,
                held_candidates=rescan_candidates,
            )
            # The rescan replaces these sources' files; carry their frozen entries.
            # A path the rescan sees again (its .git vanished) uploads normally.
            for name, data in events_manifest["sources"].items():
                previous = local_manifest["sources"].get(name, {}).get("files", {})
                for rel in list(frozen.get(name, ())):
                    if rel in data["files"]:
                        frozen[name].discard(rel)
                    elif rel in previous:
                        data["files"][rel] = previous[rel]
                # A file the scan held that now sits under a checkout the rescan
                # reported is frozen instead: its carried entry is the accepted
                # prior the first scan injected, never a first-scan revision.
                roots = sorted(nested_roots.get(name, ()))
                for rel in held_files.get(name, ()):
                    if roots and rel not in data["files"] and _under_skip_prefix(rel, roots):
                        if rel in previous:
                            data["files"][rel] = previous[rel]
                            frozen.setdefault(name, set()).add(rel)
            if held is not None and rescan_held is not None and rescan_candidates is not None:
                # The rescan is the new truth for these sources: a path it reads
                # cleanly leaves the held set, and one it holds keeps the
                # accepted prior entry (or is left out), never its first-scan
                # revision, whose blob is not stored until the upload loop below.
                for name in events_manifest["sources"]:
                    held_files.pop(name, None)
                held_files.update(
                    _inject_held_entries(
                        events_manifest,
                        remote_manifest,
                        rescan_held,
                        rescan_candidates,
                        exclude_map=exclude_map,
                        skip_prefixes=skip_prefixes,
                        frozen=frozen,
                        include_map=include_map,
                    )
                )
            local_manifest["sources"].update(events_manifest["sources"])
            if remote_manifest is not None:
                core._prove_omitted_paths_absent(
                    local_manifest, remote_manifest, sources, max_file_size=max_file_size
                )
            # mm-events file count may have rolled (new daily file); regenerate.
            local_manifest["tombstones"] = generate_tombstones(
                local_manifest, remote_manifest, device_id
            )

    source_by_name = {src["name"]: src for src in sources}

    # Diff and upload per-source.
    total_bytes = 0
    total_new = 0
    total_modified = 0
    total_deleted = 0
    upload_held = False
    source_diffs_to_upload = list(
        core.iter_source_diffs(local_manifest, remote_sources, skip_unchanged=True)
    )
    if not dry_run:
        # diff.new and diff.modified are disjoint; count both without merging.
        total_to_upload = sum(
            1
            for src_name, _src_data, _remote_src, diff in source_diffs_to_upload
            for changes in (diff.new, diff.modified)
            for rel in changes
            if rel not in frozen.get(src_name, ()) and rel not in held_files.get(src_name, ())
        )
        if total_to_upload > 0:
            core._sync_progress_start_batch("Uploading files", total_to_upload, "files")

    for src_name, src_data, _remote_src, diff in source_diffs_to_upload:
        if dry_run:
            if show:
                console.print(f"\n[bold]Source '{src_name}':[/bold]")
                _print_diff_summary(diff, 0)
            continue

        # Frozen checkout entries are already stored; their local bytes may differ.
        # Held files keep their prior entry and are never read again here.
        to_upload = {
            rel: info
            for rel, info in {**diff.new, **diff.modified}.items()
            if rel not in frozen.get(src_name, ()) and rel not in held_files.get(src_name, ())
        }
        base_path = Path(src_data["base_path"])
        src_cfg = source_by_name.get(src_name, {})

        if verbose and not quiet:
            console.print(f"\n[bold]Uploading {len(to_upload)} files from '{src_name}'...[/bold]")

        uploaded = _upload_changed_blobs(
            backend,
            base_path,
            to_upload,
            device_id,
            passphrase,
            memory_kb,
            verbose=(verbose and not quiet),
            src_name=src_name,
            source_type=src_cfg.get("type", "claude"),
            max_file_size=max_file_size,
            allow_hold=hold_eligible,
        )
        total_bytes += uploaded.bytes_transferred
        if src_name in remote_sources and "vanished" in uploaded.held.values():
            # A vanished file may mean its entire published root disappeared.
            core._require_published_source_root(src_name, Path(src_cfg["path"]).expanduser())
        # Apply the upload's patches and holds to the manifest BEFORE it is
        # serialized: a settled re-read advertises its own revision, and a held
        # file goes back to its accepted prior entry (or out), so the manifest
        # never names a blob that was not put.
        local_files = local_manifest["sources"][src_name]["files"]
        local_files.update(uploaded.patches)
        upload_held |= bool(uploaded.held)
        for rel, reason in uploaded.held.items():
            prior = remote_sources.get(src_name, {}).get("files", {}).get(rel)
            tombstoned = is_tombstoned(src_name, rel, (real_remote or {}).get("tombstones", {}))
            if prior is not None and not tombstoned:
                local_files[rel] = dict(prior)
            else:
                local_files.pop(rel, None)
            if not tombstoned:
                held_files.setdefault(src_name, {})[rel] = reason
        held_new = sum(1 for rel in uploaded.held if rel in diff.new)
        held_modified = sum(1 for rel in uploaded.held if rel in diff.modified)
        total_new += len(diff.new) - held_new
        total_modified += len(diff.modified) - held_modified
        total_deleted += len(diff.deleted)
        if src_name not in MM_INTERNAL_SOURCE_NAMES:
            content_files -= len(uploaded.held)

    if dry_run:
        if show:
            elapsed = time.time() - start
            if has_selection_change and not (total_new or total_modified or total_deleted):
                console.print("\n[bold]Would refresh manifest[/bold] (source selection changed).")
            elif has_mtime_only and not (total_new or total_modified or total_deleted):
                # Symmetric with the status command's metadata-only branch (v0.12.6).
                # Without this, dry-run would print "Dry run complete" with zero
                # per-source output and the user would miss that `mm push` would
                # republish the manifest to propagate bumped mtimes. NOTE: dry-run
                # counters stay zero (the per-source loop `continue`s before
                # incrementing), so we can't honestly promise "no blobs" -- the
                # events tail may still emit an mm-events row on the actual push.
                console.print(
                    "\n[bold]Would refresh manifest[/bold] (metadata-only changes pending)."
                )
            _print_held_outcome(_held_list(held_files), verbose=verbose, preview=True)
            console.print(f"  Completed in {elapsed:.1f}s")
        return None

    if upload_held:
        # The scan may have cleared a tombstone for a recreated file that the
        # upload then held and removed. Reconcile against the final entries so
        # that accepted deletion survives until replacement bytes are published.
        local_manifest["tombstones"] = generate_tombstones(
            local_manifest, remote_manifest, device_id
        )

    # Upload manifest (includes tombstones)
    if show:
        if fetch.conflict_copies_only:
            # The mm-events activity row nearly always adds a modified file, so
            # name the rewrite even when files upload too.
            console.print(
                "\n[bold]Rewriting manifest held only by conflict copies "
                f"({total_new + total_modified} files to upload)...[/bold]"
            )
        elif recovering_from_corrupt and not (total_new or total_modified or total_deleted):
            console.print("\n[bold]Rewriting manifest to heal remote corruption...[/bold]")
        elif has_selection_change and not (total_new or total_modified or total_deleted):
            console.print("\n[bold]Refreshing manifest (source selection changed)...[/bold]")
        elif has_mtime_only and not (total_new or total_modified or total_deleted):
            console.print("\n[bold]Refreshing manifest (metadata-only changes)...[/bold]")
        else:
            console.print(f"\n[bold]Uploading {total_new + total_modified} files...[/bold]")
    core._sync_progress_phase("Saving manifest")
    manifest_data = serialize_manifest(local_manifest)
    enc_manifest = encrypt(manifest_data, passphrase, memory_kb)
    mkey = manifest_key(device_id)
    fresh_manifest = core._fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    if fresh_manifest.newer_version is not None:
        core._error(core._newer_format_message(fresh_manifest.newer_version, "manifest"))
    # Display-only facts for the attended outcome line; autopush/recapture skip
    # the extra manifest passes.
    published_content_files = (
        sum(
            len(diff.new) + len(diff.modified) + len(diff.deleted)
            for name, _local, _remote, diff in core.iter_source_diffs(
                local_manifest, remote_sources, skip_unchanged=True
            )
            if name not in MM_INTERNAL_SOURCE_NAMES
        )
        if result_observer is not None
        else None
    )
    # Independent content receipt at the actual acceptance boundary, even when
    # capture was skipped or failed. Maintenance cannot revoke accepted content.
    result = PushResult(
        total_new=total_new,
        total_modified=total_modified,
        total_deleted=total_deleted,
        bytes_transferred=total_bytes,
        events_degradations=events_degradations,
        content_changed=content_changed,
        content_files=content_files,
        content_accepted=False,
        published_content_files=published_content_files,
        metadata_updated=result_observer is not None
        and not published_content_files
        and any(
            core._has_mtime_only_changes_vs_remote(
                local_manifest, remote_sources, source_filter=name
            )
            for name in local_manifest["sources"]
            if name not in MM_INTERNAL_SOURCE_NAMES
        ),
        selection_updated=bool(
            (set(local_manifest["sources"]) ^ advertised_prior_sources) - MM_INTERNAL_SOURCE_NAMES
        ),
        manifest_repaired=recovering_from_corrupt,
        held=_held_list(held_files),
    )
    backend.put(mkey, enc_manifest)
    result.content_accepted = True
    if attempt is not None:
        attempt.content_accepted = True
    if result_observer is not None:
        result_observer(result)
    if captured is not None and captured.appended is not None:
        try:
            result.host_usage_published = _report_usage_publication(
                sources, captured.appended, local_manifest, result, verbose=verbose, attempt=attempt
            )
        except Exception as e:
            _warn_publication_evidence_unavailable(e, "Content sync succeeded.", attempt=attempt)
    if captured is not None and captured.appended is not None:
        try:
            if show and not retro_catchup_pending:
                _print_usage_push_mode(result)
        except OSError as e:
            with core._pause_sync_progress():
                stderr_console.print(
                    f"mm: warning: Content sync reached manifest acceptance; "
                    f"post-publication maintenance failed: {safe_str(e)}. Run mm status."
                )
    # Write sidecar (best-effort: failure warns but does not abort push;
    # the remote manifest succeeded, so peers still have a path to recovery).
    # Warn on both paths: without the sidecar, future corruption recovery
    # on THIS device can only rely on peers -- silently degrading that path
    # defeats the TODOS #1 guarantee.
    try:
        sidecar.write(local_manifest)
    except (OSError, StorageError) as e:
        result.maintenance_failures += 1
        with core._pause_sync_progress():
            if quiet:
                print(
                    f"mm: warning: failed to write recovery sidecar "
                    f"({sidecar.sidecar_path()}): {e}. Push succeeded; future "
                    f"corruption recovery will fall back to peer devices.",
                    file=sys.stderr,
                )
            else:
                console.print(
                    f"[yellow]Warning:[/yellow] failed to write recovery sidecar "
                    f"({sidecar.sidecar_path()}): {e}. Push succeeded; future "
                    f"corruption recovery will fall back to peer devices."
                )

    try:
        update_last_seen(backend, device_id)
        core._cleanup_conflict_copies(backend, device_id, passphrase, memory_kb)
    except (OSError, MindMeldError) as e:
        result.maintenance_failures += 1
        if not attended:
            raise
        with core._pause_sync_progress():
            stderr_console.print(
                f"mm: warning: Content sync reached manifest acceptance; "
                f"post-publication maintenance failed: {safe_str(e)}. Run mm diag."
            )

    elapsed = time.time() - start
    result.elapsed = elapsed

    if show and result_observer is not None:
        _print_held_outcome(result.held, verbose=verbose)
    if show and result_observer is None:
        if result.held:
            console.print(
                f"\n[bold green]Push complete;[/bold green] [yellow]{len(result.held)} "
                f"{'file' if len(result.held) == 1 else 'files'} not yet synced.[/yellow]"
            )
        else:
            console.print("\n[bold green]Push complete.[/bold green]")
        if total_new:
            console.print(f"  [green]+ {total_new} new[/green]")
        if total_modified:
            console.print(f"  [yellow]~ {total_modified} modified[/yellow]")
        if total_deleted:
            console.print(f"  [red]- {total_deleted} deleted[/red]")
        console.print(f"  Completed in {elapsed:.1f}s")
        if total_bytes:
            mb = total_bytes / (1024 * 1024)
            console.print(f"  {mb:.1f}MB transferred")
        _print_held_outcome(result.held, verbose=verbose)

    return result


@app.command()
def pull(
    from_device: str | None = typer.Option(None, "--from", help="Pull from a specific device ID"),
    source: str | None = typer.Option(
        None, "--source", help="Only pull a specific source (e.g., 'claude', 'gstack')"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Preview what mm pull would write, merge or conflict. Changes nothing except "
            "the local lock file: no file writes or renames, config writes, pull-history "
            "rows or upgrade checks. Exits 1 if the preview stops, 3 if --conflict-mode "
            "fail predicts conflicts."
        ),
    ),
    conflict_mode: pullflow.ConflictMode = typer.Option(
        "keep-both",
        "--conflict-mode",
        help=(
            "How to handle conflicts (local edited, remote differs). "
            "'keep-both' (default): local stays at canonical, remote saved as "
            ".sync-conflict-*. 'prompt': ask per-file (prediction-only with --dry-run). "
            "fail: preflight and exit 3 before applying any file; combine with "
            "--dry-run for a write-free check."
        ),
        case_sensitive=False,
    ),
) -> None:
    """Pull selected context from other Macs.

    keep-both preserves local files and saves divergent remote bytes in
    .sync-conflict-* copies. prompt asks per file; with --dry-run it only
    predicts. fail exits 3 before applying any file if conflicts or failures
    are predicted. Use mm pull --dry-run --conflict-mode fail as a write-free
    CI check. Files can change between preflight and apply.

    Exit codes: 0 completed (warnings may indicate an incomplete preview),
    1 stopped or any file failed to apply, 2 usage error,
    3 conflicts or failures predicted in fail mode.
    """
    with _sync_outcome("pull", verbose=verbose, dry_run=dry_run):
        with (
            _attended_sync_run(
                "pull",
                dry_run=dry_run,
                scope={
                    k: v
                    for k, v in {"source": source, "from_device": from_device}.items()
                    if v is not None
                },
            ) as run,
            pullhistory.command_scope(attended=True),
        ):
            config = core._get_config(read_only=dry_run)
            core._maybe_prompt_migration(config, read_only=dry_run)
            # Re-load in case the migration prompt mutated config on disk so this
            # pull sees the new exclude_patterns.
            if not dry_run:
                config = core._get_config(read_only=False)
            refusal_suffix = core.DRY_RUN_REFUSAL if dry_run else ""
            run["stage"] = "authentication-error"
            passphrase = core._get_passphrase_or_exit()
            run["stage"] = "command-refused"

            try:
                acquire_lock()
            except LockError as e:
                run["cause"] = "lock-held"
                core._error(str(e))

            try:
                core._start_sync_progress("Unlocking storage")
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
                    run["cause"] = "crypto-error" if isinstance(e, CryptoError) else "setup-error"
                    core._error(str(e) + refusal_suffix)
                run["result"] = pullflow._pull_core(
                    config,
                    passphrase,
                    memory_kb,
                    from_device,
                    source,
                    verbose=verbose,
                    dry_run=dry_run,
                    conflict_mode=conflict_mode,
                    diagnostics=verbose or dry_run,
                    result_observer=lambda result: run.__setitem__("result", result),
                )
                if dry_run:
                    console.print(core.DRY_RUN_COMPLETE)
                    console.print(pullflow.PULL_NOT_PREVIEWED)
            except MindMeldError as e:
                if dry_run:
                    core._error(str(e) + refusal_suffix)
                raise
            finally:
                core._stop_sync_progress()
                core._release_sync_lock()

            # Seam 2 — interactive pull tail: self-update, or the nudge. Runs AFTER
            # the lock is released so the cold-cache HTTP fetch never blocks pull
            # progress and the update can take the lock for itself.
            if not dry_run:
                _sync_update(config, verbose=verbose)


# ── status ────────────────────────────────────────────────────────────


@app.command()
def status(
    source: str | None = typer.Option(
        None, "--source", help="Show status for a specific source only"
    ),
) -> None:
    """Compare selected local context with this device's last push; verdict after authentication."""
    statusflow.status(source=source)


# ── diag ──────────────────────────────────────────────────────────────


def _notice_recapture_host_usage(config: dict, sources: list[dict]) -> None:
    """Cache-only bridge for operators who previously used git recapture."""
    from mind_meld import host_registry

    for name, _ in events_tail._default_host_readers(
        sources,
        cursor_consented=_config_module.cursor_host_usage_enabled(config),
    ):
        state = host_registry.reader_usage_diag(name)
        cold = host_registry.reader_cache_cold(name, state)
        if cold or state.get("last_reason"):
            console.print(
                "Recapture covers Git history only. Refresh host usage on this Mac: "
                "[bold]mm push[/bold]"
            )
            return


def _retro_catchup_pending(sources: list[dict], device_id: str) -> bool:
    """Read local progress without probing projects or mutating capture state."""
    source = next((s for s in sources if s.get("name") == "mm-events"), None)
    if source is None:
        return False
    events_dir = Path(source["path"]).expanduser() / "events"
    try:
        scan = events.latest_event_rows(events_dir, device_id, {"mm-push"})
        projected = events.project_recorded_capture(scan.rows.get("mm-push"))
        cache = events.read_git_capture_cache()
        if projected is None:
            return bool(cache.get("discovery_resume_after") or cache.get("pending_refresh_since"))
        since = (
            scan.cursor.since
            if scan.cursor is not None
            else events.resolve_push_cursor(events_dir, device_id).since
        )
        observed = events.parse_aware_ts(projected.get("ts"))
        if observed is not None:
            since = max(since, observed)
        return bool(
            projected.get("discovery") == "partial"
            or projected.get("walk_budget_aborts")
            or cache.get("discovery_resume_after")
            or cache.get("pending_refresh_since")
            or events.count_pending_repositories([], since)
        )
    except (OSError, ValueError):
        return False


@app.command()
def diag(
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of plain text."
    ),
) -> None:
    """Dump non-secret crypto + sync state for support triage.

    Never prints raw root_salt, master_key, keycheck, passphrase, or peer
    device_ids. See SPEC.md "Manifest corruption recovery" for how this
    maps to the tri-state recovery chain — each field here names one
    possible cause of a recovery failure.
    """
    statusflow.diag(as_json=as_json)


# ── devices ───────────────────────────────────────────────────────────


@app.command()
def devices(
    fmt: str = typer.Option(
        "table",
        "--format",
        help="Output format: 'table' (human) or 'json' (machine).",
        case_sensitive=False,
    ),
) -> None:
    """List all registered devices.

    ``--format=json`` emits a single JSON array on stdout. Each entry has the
    keys ``device_id``, ``device_name``, ``last_seen``, ``last_seen_version``;
    missing fields are emitted as ``null`` (not em-dashes \u2014 em-dashes are a
    table-rendering convention). Empty fleet renders as ``[]``. Stable contract
    for the Group 8 retro-fleet skill's ``mm devices --format=json`` consumer.
    """
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


# ── diff ──────────────────────────────────────────────────────────────


@app.command(name="diff")
def diff_cmd(
    from_device: str | None = typer.Option(None, "--from", help="Diff against a specific device"),
    source: str | None = typer.Option(None, "--source", help="Diff a specific source only"),
) -> None:
    """Compare this Mac's files with its last push (or with --from DEVICE's manifest).

    Changes nothing, not even the lock file. For incoming changes from other
    Macs, use mm pull --dry-run.
    """
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

    # Build local manifest (v2)
    sources_configs = get_sources(config)
    local_manifest = build_manifest_v2(device_id, device_name, sources_configs, max_file_size)

    diff_fetch = core._fetch_remote_manifest(backend, target_id, passphrase, memory_kb)
    if diff_fetch.status == "missing":
        console.print(
            f"[dim]No remote manifest for "
            f"{'device ' + target_id if from_device else 'this device'} yet.[/dim]"
        )
    elif diff_fetch.newer_version is not None:
        core._error(core._newer_format_message(diff_fetch.newer_version, "manifest"))
    elif diff_fetch.status == "corrupt":
        console.print(
            f"[yellow]Warning:[/yellow] remote manifest for "
            f"{'device ' + target_id if from_device else 'this device'} is "
            f"corrupt — showing diff against empty remote."
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
        f"\n[bold]Diff against {'device ' + target_id if from_device else 'remote'}:[/bold]"
    )

    # Map source names → local base paths for pull-outcome prediction
    src_base_paths: dict[str, Path] = {
        s["name"]: Path(s["path"]).expanduser().resolve() for s in sources_configs
    }

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
                outcome = pullplan._predict_pull_outcome(path, remote_info, base_path)
                console.print(
                    f"    [yellow]~ push  [/yellow] {safe_str(path)} (pull would: {outcome})"
                )
            else:
                console.print(f"    [yellow]~ push  [/yellow] {safe_str(path)}")
        for path in sorted(diff.deleted):
            console.print(f"    [red]- only-remote[/red] {safe_str(path)}")

    if not any_changes:
        console.print("[green]No differences.[/green]")


# ── gc ────────────────────────────────────────────────────────────────


@app.command()
def gc(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Preview orphan blobs and retention cleanup without deleting (this "
            "device's blobs only). Changes nothing except the local lock file. Exits "
            "1 if the preview stops."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    prune_conflicts: bool = typer.Option(
        False,
        "--conflicts",
        help=f"Also delete .sync-conflict-* files older than {CONFLICT_AGE_DAYS} days",
    ),
) -> None:
    """Collect this device's orphaned blobs and local retention data.

    Reads only this device's manifest and lists only its own blob directory.

    ``--dry-run`` reports blob and retention candidates without changing files,
    including a preview of the conflict-sidecar reaper. ``--conflicts`` is
    required to actually reap stale conflict copies.
    """
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
        _do_gc(config, passphrase, memory_kb, dry_run, verbose)
        # Track 7B: events retention is always-on (fleet policy, not opt-in).
        # See `_gc_old_event_files` for the tombstone-propagation framing.
        retention._gc_old_event_files(config, dry_run, verbose)
        # v0.11.14+: token cache reaper. Stale entries (no living jsonl OR
        # by_day older than 90d) are dropped. Dry-run reports without
        # mutating the cache file.
        retention._gc_token_cache(dry_run, verbose)
        # v0.12.39: leftover v0.12.0 trend-snapshot files. Best-effort,
        # filename-matched only, then rmdir if empty. Never rm -rf.
        retention._gc_orphan_retros_dir(dry_run, verbose)
        # Bare `--dry-run` previews the conflict reaper (the one that
        # touches user content). Apply still requires `--conflicts`.
        if prune_conflicts or dry_run:
            retention._gc_old_conflict_files(
                config, dry_run, verbose, deletion_requires_flag=dry_run and not prune_conflicts
            )
        if dry_run:
            console.print(core.DRY_RUN_COMPLETE)
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
) -> int:
    """Collect this device's orphaned blobs from its raw manifest only."""
    backend = get_backend(config)
    my_device_id = config["device"]["id"]
    own_prefix = storage_keys.data_prefix(my_device_id)

    # Sweep this device's stale tmp*.tmp files before ref-counting.
    # Runs UNDER the caller's lock (acquire_lock already held by gc()),
    # so no concurrent writer can race with the sweep.
    retention._sweep_local_tmp_files(
        backend,
        my_device_id,
        dry_run,
        verbose,
        emit_summary=emit_retention_summary,
    )

    # Only this device's raw manifest can authorize collecting its blobs.
    # Consumer exclusions/disabled sources must never filter these references.
    gc_fetch = core._fetch_remote_manifest(backend, my_device_id, passphrase, memory_kb)
    # _error escapes markup itself; only the direct stderr print adds safe_str.
    label = safety.safe_terminal_str(
        f"{config['device'].get('name', my_device_id)} ({my_device_id})"
    )
    referenced_hashes: set[str] = set()

    def _skip_missing() -> int:
        with core._pause_sync_progress():
            stderr_console.print(
                f"mm: warning: this device's manifest is missing: {safe_str(label)}. "
                "Blob collection was skipped; no blobs were deleted. "
                "Wait for sync, inspect with 'mm diag', then retry 'mm gc'."
            )
        return 0

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
                "cannot GC safely — this device's manifest is held only by iCloud "
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
    else:
        if core._SYNC_OUTPUT.get() is None or core._SYNC_OUTPUT.get()["verbose"]:
            console.print(
                f"\n[bold green]GC complete.[/bold green] Deleted {orphan_count} orphaned blobs."
            )

    return orphan_count


# ── sources ───────────────────────────────────────────────────────────


@app.command()
def sources() -> None:
    """List configured sync sources.

    Shows ALL configured sources (active + disabled). The "Enabled" column
    reflects [sync].disabled_sources — disable a source on this device with
    `mm disable-source <name>`. The "Excluded" column reports how many files
    the source's `exclude_patterns` matched or its `include_patterns`
    allowlist left out on this scan; diagnostic only, used to sanity-check
    an over-broad glob or an under-broad allowlist.
    """
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


# ── enable-source / disable-source / reconfigure-sources ──────────────


def _known_source_names(config: dict) -> list[str]:
    """Sorted union of file sources and usage-only toggle names.

    The validation surface for `mm enable-source` / `mm disable-source`.
    A name is "known" if the user configures its file source, mm ships a default
    for it, or it has a usage-only consent alias. --force accepts unknown names.
    """
    explicit = [
        s["name"]
        for s in config.get("sync", {}).get("sources", []) or []
        if s.get("name") not in _config_module.RETIRED_SOURCE_NAMES
    ]
    defaults = [s["name"] for s in DEFAULT_SOURCES]
    return sorted(set(explicit) | set(defaults) | set(_config_module.HOST_USAGE_ONLY_CONSENT))


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


def _toggle_cursor_usage(name: str, config: dict, *, enabled: bool) -> bool:
    """Handle the cursor usage alias without changing file-source selection.

    An explicit custom source takes precedence over the alias so its existing
    enable/disable behavior remains available and does not grant usage consent.
    Enable installs the hook before granting consent; disable revokes consent
    before touching hooks.json, so a hook file mm cannot edit never blocks
    revocation (the leftover hook records nothing without consent).
    """
    key = _config_module.HOST_USAGE_ONLY_CONSENT.get(name)
    if name != "cursor" or key is None:
        return False
    if any(s["name"] == name for s in config.get("sync", {}).get("sources", []) or []):
        return False
    from mind_meld import host_usage

    hook_errors = (OSError, ValueError, RuntimeError, StorageError)
    already = config.get("retro", {}).get(key) is enabled
    hook_changed = False
    if enabled:
        try:
            hook_changed = host_usage.configure_cursor_hook(enabled=True)
        except hook_errors as exc:
            core._error("Could not install Cursor's completion hook: " + safe_str(str(exc)))
    if not already:
        patch_config_on_disk({"retro": {key: enabled}})
    if not enabled:
        try:
            hook_changed = host_usage.configure_cursor_hook(enabled=False)
        except hook_errors as exc:
            stderr_console.print(
                "[yellow]mm: warning:[/yellow] Cursor usage capture is disabled, but mm's "
                f"completion hook could not be removed from {host_usage.CURSOR_HOOK_CONFIG_PATH}: "
                + safe_str(str(exc))
                + f". It records nothing while capture is disabled; remove its "
                f"'{host_usage.CURSOR_HOOK_COMMAND}' stop entry to clean up."
            )
    state = "enabled" if enabled else "disabled"
    if already:
        repaired = ""
        if hook_changed:
            repaired = "; reinstalled its hook" if enabled else "; removed its hook"
        console.print(f"[dim]Cursor usage capture is already {state}{repaired}.[/dim]")
        return True
    verb = "Enabled" if enabled else "Disabled"
    console.print(f"[green]{verb} Cursor usage capture on this device.[/green]")
    if enabled:
        console.print(
            "[dim]Standalone interactive sessions use the completion hook. "
            "For --print runs, use 'mm cursor-agent' to record stdout usage. "
            "Only usage aggregates are synced.[/dim]"
        )
        console.print("[dim]Run 'mm push' to refresh usage, then 'mm status' to verify.[/dim]")
    opposite = "disable" if enabled else "enable"
    console.print(f"[dim]Run 'mm {opposite}-source cursor' to change this setting.[/dim]")
    return True


_CURSOR_RECORD_WARNING = "mm: warning: Cursor usage could not be recorded; run mm diag."
_CURSOR_WRAPPED_ENV = "MM_CURSOR_AGENT_ACTIVE"


def _cursor_usage_consented() -> bool:
    """Consent check shared by the hook and the wrapper; silent when mm is not set up."""
    if not _config_module.CONFIG_PATH.exists():
        return False
    try:
        return _config_module.cursor_host_usage_enabled(load_config())
    except (MindMeldError, OSError, ValueError):
        print(_CURSOR_RECORD_WARNING, file=sys.stderr)
        return False


def _record_cursor_completion(payload: object, *, model: str | None = None) -> None:
    from mind_meld import host_usage

    if not _cursor_usage_consented():
        return
    try:
        host_usage.record_cursor_usage(payload, model=model)
    except (MindMeldError, OSError, ValueError, RuntimeError):
        print(_CURSOR_RECORD_WARNING, file=sys.stderr)


@app.command(name="capture-cursor-usage", hidden=True)
def capture_cursor_usage() -> None:
    """Completion hook: retain counters only, and never block Cursor's agent loop."""
    from mind_meld import host_usage

    limit = host_usage.CURSOR_HOOK_STDIN_MAX_BYTES
    try:
        data = sys.stdin.buffer.read(limit + 1)
        if len(data) > limit:
            raise ValueError("oversized Cursor completion")
        payload = json.loads(data)
    except (OSError, ValueError, RecursionError):
        print("mm: warning: Cursor completion metadata was unreadable.", file=sys.stderr)
    else:
        try:
            _record_cursor_completion(payload)
        except Exception:
            # The hook's contract outranks diagnostics: exit 0 with `{}`.
            print(_CURSOR_RECORD_WARNING, file=sys.stderr)
    typer.echo("{}")


class _RawArgsCommand(TyperCommand):
    """Keep argv verbatim: Click's parser drops a literal `--` from ctx.args."""

    def parse_args(self, ctx, args):
        ctx.meta["raw_args"] = list(args)
        return super().parse_args(ctx, args)


def _terminal_delivers_sigint() -> bool:
    """True when Ctrl-C on our foreground terminal already reaches the child too."""
    for stream in (sys.stdin, sys.stderr):
        try:
            fd = stream.fileno()
            if os.isatty(fd):
                return os.tcgetpgrp(fd) == os.getpgrp()
        except (AttributeError, OSError, ValueError):
            continue
    return False


@contextmanager
def _forwarding_signals(child: subprocess.Popen):
    """Relay termination to the child, so the wrapper never dies first.

    A foreground terminal already delivers Ctrl-C to the child's process group,
    so the wrapper then ignores SIGINT instead of delivering it twice; a SIGINT
    aimed only at the wrapper (a supervisor's cancel) is relayed.
    """
    previous = {}
    relayed = [signal.SIGTERM, signal.SIGHUP]
    if _terminal_delivers_sigint():
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)
    else:
        relayed.append(signal.SIGINT)
    for signum in relayed:
        previous[signum] = signal.signal(signum, lambda sig, _frame: child.send_signal(sig))
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _cursor_exit_code(code: int) -> int:
    return code if code >= 0 else 128 - code


@app.command(
    name="cursor-agent",
    cls=_RawArgsCommand,
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "help_option_names": [],
    },
)
def cursor_agent(ctx: typer.Context) -> None:
    """Run Cursor normally and retain print-mode usage for retro-fleet."""
    from mind_meld import host_usage, token_usage

    if os.environ.get(_CURSOR_WRAPPED_ENV):
        core._error(
            "mm cursor-agent is running itself: the cursor-agent on PATH points back to mm. "
            "Put Cursor's own cursor-agent first on PATH."
        )
    env = {**os.environ, _CURSOR_WRAPPED_ENV: "1"}
    args = list(ctx.meta["raw_args"])
    options = args[: args.index("--")] if "--" in args else args
    printing = "--print" in options or "-p" in options
    capture = (
        printing and not {"--help", "-h", "--version"} & set(options) and _cursor_usage_consented()
    )
    if not capture:
        try:
            with (
                subprocess.Popen(["cursor-agent", *args], env=env) as child,
                _forwarding_signals(child),
            ):
                code = child.wait()
        except OSError:
            core._error("cursor-agent could not be started; check that it is on PATH.")
        raise typer.Exit(_cursor_exit_code(code))

    model = host_usage.cursor_cli_model(options)
    # The last --output-format before `--` wins, as in Cursor's own parser.
    output, value_index = "text", None
    for index, arg in enumerate(options):
        if arg.startswith("--output-format="):
            output, value_index = arg.removeprefix("--output-format="), index
        elif arg == "--output-format" and index + 1 < len(options):
            output, value_index = options[index + 1], index + 1
    child_args = list(args)
    if value_index is None:
        child_args = ["--output-format=json", *child_args]
    elif output == "text":
        inline = child_args[value_index].startswith("--output-format=")
        child_args[value_index] = "--output-format=json" if inline else "json"
    stdout_open = True
    result = None

    def emit(data: bytes) -> None:
        nonlocal stdout_open
        if stdout_open:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except (OSError, AttributeError, ValueError):
                # A closed pipe, hung-up tty or full disk: keep draining so the
                # child is not blocked and usage is kept. Point fd 1 at
                # /dev/null: Python retries the failed flush at exit and would
                # otherwise replace Cursor's status with 120.
                stdout_open = False
                with suppress(OSError, ValueError):
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, sys.stdout.fileno())
                    os.close(devnull)

    try:
        with (
            subprocess.Popen(
                ["cursor-agent", *child_args], stdout=subprocess.PIPE, env=env
            ) as child,
            _forwarding_signals(child),
        ):
            assert child.stdout is not None
            while line := child.stdout.readline(token_usage.MAX_JSONL_LINE_BYTES + 1):
                try:
                    payload = json.loads(line)
                except (ValueError, RecursionError):
                    payload = None
                terminal = isinstance(payload, dict) and payload.get("type") == "result"
                if terminal:
                    result = payload
                if output == "text" and terminal and isinstance(payload.get("result"), str):
                    emit(payload["result"].encode(errors="replace") + b"\n")
                else:
                    emit(line)
            code = child.wait()
    except OSError:
        core._error("cursor-agent could not be started or its output could not be read.")
    # The result line is only final at EOF; queue it once Cursor has exited
    # and the default signal handlers are back.
    if result is not None:
        _record_cursor_completion(result, model=model)
    elif code == 0:
        print(
            "mm: warning: Cursor usage was not recorded: no result line was found.",
            file=sys.stderr,
        )
    raise typer.Exit(_cursor_exit_code(code))


def _record_seen(names: list[str]) -> None:
    """Mark `names` as acknowledged in the seen_sources tracker.

    Atomic under flock via `seen_sources.acknowledge` (codex 2026-04-25:
    the previous read+update+write split lost concurrent acknowledgments).
    Best-effort: a write failure surfaces a stderr breadcrumb but does not
    crash the calling command. The seen tracker drives the `mm status`
    new-source hint; losing it just means a hint repeats.
    """
    config = core._get_config(read_only=False)
    currently_resolved = [s["name"] for s in get_sources(config)]
    seen_sources.acknowledge(names, initial=currently_resolved)


@app.command(name="disable-source")
def disable_source(
    name: str = typer.Argument(..., help="Source name to disable on this device"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Accept a source name not yet known to mm (forward-compat).",
    ),
) -> None:
    """Disable a sync source on THIS DEVICE only.

    config.toml is per-machine (never synced), so `disabled_sources` is a
    natural per-device preference. Disabling does NOT delete the source's
    [[sync.sources]] entry — re-enabling preserves user customizations
    (include_dirs, exclude_patterns).

    Strict by default: unknown name errors with a closest-match hint.
    `--force` accepts unknown names so you can pre-disable a source that
    hasn't shipped yet (e.g. `mm disable-source codex --force`).

    `cursor` is a usage-only alias (unless you configured a file source named
    cursor): it revokes Cursor usage consent first, then removes mm's stop
    entry from ~/.cursor/hooks.json, warning if that file cannot be edited.
    """
    config = core._get_config(read_only=False)
    if _toggle_cursor_usage(name, config, enabled=False):
        return
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


@app.command(name="enable-source")
def enable_source(
    name: str = typer.Argument(..., help="Source name to enable on this device"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Accept a source name not yet known to mm (forward-compat).",
    ),
) -> None:
    """Enable a sync source on this device.

    Removes the name from [sync].disabled_sources. If the name is in
    DEFAULT_SOURCES but absent from the user's [[sync.sources]] (e.g.
    a freshly-shipped codex source that didn't auto-enable), append the
    default config so the source actually starts syncing.

    Enabling `codex`, `muse`, or `agy` ALSO authorizes that host's local
    usage reader, whose activity shows up in the `retro-fleet` AGENT LOGS
    block, and (for `codex` / `claude` / `agy`) authorizes mm to
    maintain a `retro-fleet` skill link in that agent's skills directory.
    That consent coupling is the feature's default switch, so it is stated
    here at the point of decision (see HOST_READER_SOURCE_GATE). To keep a
    skill link maintained WITHOUT enabling sync or usage reading, use
    `mm install-skills --agent <key>` instead. This command does not
    install the link itself.

    `cursor` is a usage-only alias (unless you configured a file source named
    cursor): it installs one stop entry in ~/.cursor/hooks.json, preserving
    other hooks, then sets `[retro] cursor_host_usage = true`. No Cursor
    files sync; only usage aggregates are published.
    """
    config = core._get_config(read_only=False)
    if _toggle_cursor_usage(name, config, enabled=True):
        return
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

    # Muse and Antigravity must be materialized even when their allowlisted
    # trees are not present yet: explicit enable is consent.  In a legacy
    # config, first preserve the resolved source set before writing
    # sync.sources.  An explicit list takes priority over legacy
    # claude_dir/default fallback; writing only the new host here would make the
    # existing sources disappear and eventually produce their deletion
    # tombstones.
    if name in {"muse", "agy"} and not has_explicit_sources:
        explicit_sources = get_sources(config)
        explicit_names = [s["name"] for s in explicit_sources]
        updates["sources"] = explicit_sources

    # If the user has explicit sources and this name isn't among them,
    # but it IS in DEFAULT_SOURCES, append the default so enable actually
    # has effect (auto-detect doesn't fire when explicit sources are set).
    needs_explicit_append = name not in explicit_names and (
        name in {"muse", "agy"} or (has_explicit_sources and name in default_names)
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
            return

    if updates:
        config_updates: dict[str, dict[str, Any]] = {"sync": updates}
        patch_config_on_disk(config_updates)

    _record_seen([name])
    console.print(f"[green]Enabled source '{name}' on this device.[/green]")
    if name == "muse":
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


@app.command(name="reconfigure-sources")
def reconfigure_sources() -> None:
    """Re-run the source picker against the current config + new defaults.

    Use this after `mm` ships a new source (e.g. codex in v0.11+) to walk
    through enable/disable for every known source. Preserves user
    customizations on active [[sync.sources]] entries (include_dirs,
    exclude_patterns). Removes retired source blocks and records their names
    in disabled_sources; retirement has already stopped their sync.

    Atomicity: Ctrl-C mid-prompt aborts without writing. The whole
    reconfigured state is committed in a single patch_config_on_disk call.
    """
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
            if iname == "muse":
                # Muse never defaults on: its memory/ tree is host-created, so
                # even a detected root is not consent for sync or usage reads.
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


@app.command(name="migrate-config")
def migrate_config(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the diff. Changes nothing: no config, lock or history writes. Exits 0.",
    ),
) -> None:
    """Add recommended sync patterns to existing `[[sync.sources]]`.

    Compares each user-configured source against the matching DEFAULT_SOURCES
    entry and proposes adding any missing recommended exclude globs, plus a
    recommended `include_patterns` allowlist when the key is absent or still
    holds a previous default list. Also removes a
    leftover ``grok`` or ``opencode`` source blocks and records their
    names in ``disabled_sources``. These sources have already stopped syncing.
    Idempotent — re-running on a fully-migrated
    config exits with "already up to date".

    Acquires the mm lockfile so a concurrent push/pull can't read a half-
    written config.
    """
    if core._migrate_config_core(yes=yes, dry_run=dry_run):
        console.print(
            "Run [bold]mm push[/bold] to publish the exclusions; until then [bold]mm status[/bold] "
            "counts them as pending deletions. No Mac deletes its local copy."
        )


# ── install-skills ────────────────────────────────────────────────────


@app.command(name="install-skills")
def install_skills_cmd(
    agent: list[str] = typer.Option(
        [],
        "--agent",
        help=(
            "Grant and persist skill-link maintenance for this agent key, then "
            "install every authorized agent (not only this one). Does not enable "
            "source sync or usage reading. Repeatable. Requires an existing "
            "config (run mm init first). Bare invocation (no --agent): with a "
            "config, install what is already authorized; with no config, install "
            "for every available agent (fresh-machine setup)."
        ),
    ),
) -> None:
    """Install (or re-install) retro-fleet for every authorized agent.

    Prints one line per agent, including agents that aren't installed.

    Force-runs the same self-heal that ``mm init`` and ``mm push`` invoke
    automatically, bypassing the steady-state TTL gate. Intended for:

    * post-cleanup recovery (link removed by hand, e.g. after deleting an
      old pipx workspace whose path the link pointed at)
    * manual install on a machine where ``mm push`` hasn't run yet
    * verifying the link state on a fresh ``pipx install`` of mm
    * granting skill-link maintenance for one agent without enabling that
      agent's sync source or usage reader (``--agent KEY``)

    Each agent link points at the mm-owned store
    ``~/.local/share/mind-meld/agent-skills/retro-fleet/``. ``mm`` copies
    ``SKILL.md`` there from the running package and refreshes it on a
    version-then-hash compare. ``pipx upgrade`` no longer updates the
    agent-visible file in place.
    """
    config_path = _config_module.CONFIG_PATH
    known = {row.key for row in skill_link.AGENT_ROWS}
    known_list = ", ".join(row.key for row in skill_link.AGENT_ROWS)
    requested = [key.strip() for key in agent if key.strip()]

    if requested:
        unknown = [key for key in requested if key not in known]
        if unknown:
            typer.echo(
                f"mm: error: unknown agent '{unknown[0]}'; known agents: {known_list}",
                err=True,
            )
            raise typer.Exit(code=1)
        if not config_path.exists():
            typer.echo(
                "mm: error: --agent needs a config to record the grant. "
                "Run 'mm init' first, then retry this command. "
                "No agent links were changed.",
                err=True,
            )
            raise typer.Exit(code=1)
        try:
            cfg = load_config()
            sources = get_sources(cfg)
        except MindMeldError as e:
            typer.echo(
                f"mm: error: could not read config: {safe_str(e)}. "
                "No agent links were changed. Run mm diag "
                "(it still works with a broken config).",
                err=True,
            )
            raise typer.Exit(code=1) from e
        order = {row.key: i for i, row in enumerate(skill_link.AGENT_ROWS)}
        effective_before = skill_link.consented_agent_keys(cfg, sources)
        skills = cfg.get("skills")
        unknown_explicit_agents = (
            [key for key in skills.get("agents", []) if key not in known]
            if isinstance(skills, dict)
            else []
        )
        new_agents = sorted(effective_before | set(requested), key=lambda k: order[k])
        new_agents.extend(unknown_explicit_agents)
        try:
            patch_config_on_disk(
                {"skills": {"maintain_links": True, "agents": new_agents}},
                config_path,
            )
        except (ConfigError, OSError, StorageError) as e:
            typer.echo(
                f"mm: error: could not write {config_path}: {e}. "
                "No agent links were changed. Retry this command.",
                err=True,
            )
            raise typer.Exit(code=1) from e
        try:
            cfg = load_config()
            sources = get_sources(cfg)
        except MindMeldError as e:
            typer.echo(
                f"mm: error: could not read config: {safe_str(e)}. "
                "No agent links were changed. Run mm diag "
                "(it still works with a broken config).",
                err=True,
            )
            raise typer.Exit(code=1) from e
        may_create = skill_link.consented_agent_keys(cfg, sources)
        typer.echo(
            f"Recorded skill-link grant in {config_path}: "
            f"maintain_links = true, agents = [{', '.join(new_agents)}]. "
            "This command installs every authorized agent, not only the ones named here."
        )
    elif not config_path.exists():
        may_create = None
    else:
        try:
            cfg = load_config()
            sources = get_sources(cfg)
            may_create = skill_link.consented_agent_keys(cfg, sources)
        except MindMeldError as e:
            typer.echo(
                f"mm: error: could not read config: {safe_str(e)}. "
                "No agent links were changed. Run mm diag "
                "(it still works with a broken config).",
                err=True,
            )
            raise typer.Exit(code=1) from e

    try:
        results = skill_link._ensure_retro_skill_links(
            dry_run=False, explicit=True, may_create=may_create
        )
    except Exception as e:
        typer.echo(
            f"mm: error: retro-fleet skill installation failed: "
            f"{type(e).__name__}: {safe_str(e)}. Run mm diag.",
            err=True,
        )
        raise typer.Exit(code=1) from e

    available = False
    failed = False

    for result in results:
        descriptor = result.descriptor
        target = safe_str(str(result.target))
        agent_root = safe_str(str(descriptor.agent_root))
        dest = (
            safe_str(str(result.link_target))
            if result.link_target is not None
            else (safe_str(str(result.skill_src)) if result.skill_src is not None else "unknown")
        )
        if result.status == "installed":
            available = True
            typer.echo(f"Installed: {descriptor.display_name}: {target} -> {dest}")
        elif result.status == "unchanged":
            available = True
            typer.echo(
                f"Installed (already correct): {descriptor.display_name}: {target} -> {dest}"
            )
        elif result.status == "unavailable":
            typer.echo(f"Unavailable: {descriptor.display_name} ({agent_root} is absent)")
        elif result.status == "declined":
            available = True
            typer.echo(f"Skipped: {skill_link.render_skill_status(result)}")
        elif result.status == "removed-by-user":
            # Unreachable while this command passes explicit=True (the guard in
            # _install_available_skill_target is skipped). Handled anyway: the
            # bare `else` below reports "installation failed: None" and exits 1,
            # so any future non-explicit caller would turn a benign outcome into
            # a hard failure.
            available = True
            typer.echo(f"Left removed: {skill_link.render_skill_status(result)}")
        elif result.status in ("dangling-ours", "dangling-ours-legacy", "foreign"):
            available = True
            failed = True
            typer.echo(
                f"mm: error: {descriptor.display_name}: {skill_link.render_skill_status(result)}",
                err=True,
            )
        else:
            available = True
            failed = True
            typer.echo(
                f"mm: error: {descriptor.display_name}: {target} installation failed: "
                f"{result.reason}",
                err=True,
            )

    statuses = {result.status for result in results}
    if statuses <= {"declined", "unavailable"} and "declined" in statuses:
        example = skill_link.AGENT_ROWS[0].key
        typer.echo(
            "No agent is enabled for skill install. "
            f"Enable one: mm install-skills --agent {example} "
            "(repeat --agent for each other link target). Inspect with: mm diag"
        )
        raise typer.Exit(code=0)
    if not available:
        typer.echo(
            "mm: error: no supported agent skills directory exists; install an agent first",
            err=True,
        )
        raise typer.Exit(code=1)
    if failed:
        raise typer.Exit(code=1)


# ── retro-fleet ───────────────────────────────────────────────────────


@app.command(name="retro-fleet")
def retro_fleet_cmd(
    window: str = typer.Argument("7d", help="Retro window (Nd, e.g. '7d', '30d'). Days only."),
    no_author_filter: bool = typer.Option(
        False,
        "--no-author-filter",
        help="Disable author-email filter; render ALL fleet commits.",
    ),
    theme: list[str] = typer.Option(
        [],
        "--theme",
        help=(
            "TOP WORK theme line for the ASCII card (pass up to 3 times). "
            "Supplied by the retro-fleet skill on the second pass."
        ),
    ),
    noteworthy: str = typer.Option(
        "",
        "--noteworthy",
        help="NOTEWORTHY line for the ASCII card. Supplied by the skill.",
    ),
    name: str = typer.Option(
        "", "--name", help="Optional name in the ASCII card header (e.g. 'kb')."
    ),
    no_save: bool = typer.Option(
        False,
        "--no-save",
        hidden=True,
        help="Deprecated no-op. Snapshot persistence was removed in v0.12.39.",
    ),
    set_model_price: list[str] = typer.Option(
        [],
        "--set-model-price",
        help="Local USD/MTok card: MODEL=IN,CACHE_READ,CACHE_CREATE,OUT. Repeatable.",
    ),
    mark_pricing_verified: bool = typer.Option(
        False,
        "--mark-pricing-verified",
        help="Vendor pricing docs checked today (UTC).",
    ),
    dump_host_usage: bool = typer.Option(
        False,
        "--dump-host-usage",
        help="Forensic JSON of accepted host inventory. Skips the markdown retro.",
    ),
) -> None:
    """Render fleet retrospective markdown to stdout.

    Primarily invoked by the ``retro-fleet`` Claude Code skill — direct CLI
    use is fine for scripted exports (``mm retro-fleet 30d > /tmp/retro.md``)
    but loses the LLM judgment layer (natural-language window parsing,
    error-translation) the skill provides.

    Two-pass shape (v0.12.0+):
    * Pass 1 — ``mm retro-fleet 7d`` emits the full markdown body plus a
      ``MM_THEMES_PROMPT`` JSON sidecar at the bottom. Skill reads the
      sidecar, synthesizes themes + noteworthy, then re-invokes.
    * Pass 2 — ``mm retro-fleet 7d --theme A --theme B --theme C
      --noteworthy "..." --name kb`` re-renders with a pixel-aligned
      ASCII card up top. ``--no-save`` is accepted as a hidden no-op
      (removed as of v0.12.39; kept so a stale skill copy still exits 0).

    Thin wrapper around ``mind_meld.skills.retro_fleet.aggregator.main``.
    Routes through ``mm`` (guaranteed on PATH wherever mm is installed)
    instead of the prior ``python -m mind_meld.skills.retro_fleet.aggregator``
    invocation, which assumed a ``python`` executable on PATH with mm
    importable — false on macOS systems where only ``python3`` is on PATH,
    and structurally false for pipx installs whose ``mm`` lives in an
    isolated venv that nothing else sees.
    """
    from mind_meld.skills.retro_fleet.aggregator import main as _aggregator_main

    argv = [window]
    if no_author_filter:
        argv.append("--no-author-filter")
    for t in theme:
        argv.extend(["--theme", t])
    if noteworthy:
        argv.extend(["--noteworthy", noteworthy])
    if name:
        argv.extend(["--name", name])
    if no_save:
        argv.append("--no-save")
    if dump_host_usage:
        argv.append("--dump-host-usage")
    for spec in set_model_price:
        argv.append("--set-model-price=" + spec)
    if mark_pricing_verified:
        argv.append("--mark-pricing-verified")
    raise typer.Exit(code=_aggregator_main(argv))


# ── recapture ─────────────────────────────────────────────────────────


RECAPTURE_EXIT_PARTIAL = 4
"""Partial recapture exit. Distinct from pull ``--conflict-mode fail`` (3)."""


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


@app.command()
def recapture(
    window: str = typer.Argument(
        events.RECAPTURE_WINDOW_DEFAULT,
        help=(
            "Redo this Mac's git capture for WINDOW (default 30d, same as mm init). "
            "Safe to re-run — commits dedup fleet-wide on (remote, sha). "
            "Retros window by the COMMIT's date, not by when mm captured it, "
            "so mm recapture 90d then mm retro-fleet 7d will not show June commits."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Discover and walk, then report. Changes nothing except the local lock file: "
            "no rows, uploads, config writes or upgrade records. Exits 1 if the preview "
            "stops or finds no repositories; a partial scan exits 0 and says so."
        ),
    ),
) -> None:
    """Recover omitted git commits into the fleet retro.

    Writes git-snapshot rows first, then runs the ordinary push path so the
    substantive-change gate passes without a special case. Partial recovery
    exits 4. Zero discovered repositories writes nothing and exits 1.
    """
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
                stderr_console.print(
                    "[red]Error:[/red] recapture requires the 'mm-events' source, "
                    f"but it is {state} on this Mac."
                )
                if dry_run:
                    stderr_console.print("Fix (changes config): mm enable-source mm-events")
                    stderr_console.print(
                        f"Then preview again: mm recapture --dry-run {safe_str(window)}"
                    )
                    stderr_console.print(core.DRY_RUN_REFUSAL.strip())
                else:
                    stderr_console.print("Fix: mm enable-source mm-events")
                    stderr_console.print(f"Then retry: mm recapture {safe_str(window)}")
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

            if dry_run:
                row_bytes = sum(
                    len(json.dumps(row, sort_keys=True).encode("utf-8"))
                    for row in prepared.git_rows
                )
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
                    console.print(
                        "Recapture stopped: no Git repositories were discovered on this Mac. "
                        "Add their absolute paths under [retro].repo_roots, verify with "
                        f"'mm diag', then preview again: mm recapture --dry-run {safe_str(window)}"
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
                console.print(
                    "Recapture stopped: no Git repositories were discovered on this Mac. "
                    "Add their absolute paths under [retro].repo_roots, verify with "
                    "'mm diag', then retry."
                )
                raise typer.Exit(1)

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
                    "or shrink the day file, then retry mm recapture."
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
                    result = _push_core(config, passphrase, memory_kb, verbose=False, dry_run=False)
            except SnapshotError as e:
                console.print(
                    "Recapture was written locally but not synced. "
                    "Fix the snapshot refusal below, then run 'mm push'."
                )
                core._error(str(e))
            except typer.Exit:
                console.print(
                    "Recapture was written locally but not synced. "
                    "Fix the storage error above, then run 'mm push'."
                )
                raise

            synced = result is not None
            if not synced:
                console.print(
                    "Recapture was written locally but not synced. "
                    "Fix the storage error above, then run 'mm push'."
                )
                raise typer.Exit(1)
            if result is not None and _held_blocks_recapture(result.held):
                # The push held an mm-events file (a recapture day file was still
                # changing): the rows exist locally but have not gone out.
                console.print(
                    "Recapture was written locally but not synced. "
                    "A recapture file was still changing; run 'mm push' to publish it."
                )
                raise typer.Exit(1)

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
                raise typer.Exit(RECAPTURE_EXIT_PARTIAL)

            if not records:
                console.print(
                    f"Recapture complete: scanned {n_roots} repositories but found "
                    f"no commits dated {since_day} → {until_day}. "
                    "No retro output will change."
                )
                return

            if not new_records:
                console.print(
                    "Recapture found no commits that were not already recorded. "
                    f"This Mac's events log already covers {since_day} → {until_day} "
                    f"for all {n_roots} repositories it can see.\n"
                    "If the retro is still short, the gap is on another Mac "
                    "(mm devices) or in a repo discovery cannot see "
                    "(mm diag, then [retro] repo_roots)."
                )
                return

            oldest = _oldest_commit_date(new_records)
            cover_days = days
            if oldest is not None:
                cover_days = max(days, (until.date() - oldest.date()).days + 1)
                cover_days = min(cover_days, events.RECAPTURE_WINDOW_MAX_DAYS)
            console.print("[bold green]Recapture complete.[/bold green]")
            console.print(f"  Window scanned:   {since_day} → {until_day} ({days}d)")
            console.print(f"  Repositories:     {n_roots} scanned, {skipped} skipped")
            console.print(f"  Commit records:   {len(records)} captured")
            console.print(f"  Synced:           {'yes' if synced else 'no'}")
            console.print("")
            console.print(
                "Retros window by the COMMIT's date, not by when mm captured it, so a short"
            )
            console.print("window will not show the older recoveries:")
            console.print(f"  mm retro-fleet {cover_days}d   # covers everything just recaptured")
            console.print("  mm retro-fleet 7d    # covers the last 7 days only")
        except MindMeldError as e:
            if dry_run:
                core._error(str(e) + refusal_suffix)
            raise
        finally:
            release_lock()

        if not dry_run:
            upgrade.emit_nudge_if_due(config)


# ── update ────────────────────────────────────────────────────────────


def _update_refusal(install: upgrade.InstallInfo) -> str | None:
    """Why `mm update` leaves this install alone, or None when it can act."""
    if install.kind == "dev":
        return "This mm is a source-tree build, not an installed release. Update it with git."
    if install.kind == "not-pipx":
        return (
            f"This mm ({sys.prefix}) was not installed by pipx, so it cannot update itself. "
            f"Install the released build with: {upgrade.INSTALL_CMD}"
        )
    if install.kind == "pipx-pinned":
        return f"This mm is held by pipx pin. Run pipx unpin {install.venv_name}, then mm update."
    if install.kind == "foreign":
        source = f"from {install.spec}" if install.spec else "from a source mm cannot read"
        return (
            f"This mm was installed {source}, not from the release branch, so mm update "
            f"leaves it alone. To switch to released builds run: {upgrade.INSTALL_CMD}"
        )
    return None


def _print_update_current(install: upgrade.InstallInfo) -> None:
    console.print(f"mm {safe_str(install.version)} is up to date.")
    if install.kind == "pinned":
        console.print(
            f"  This install was made from {safe_str(install.spec)}, so mm will not "
            "update it automatically. To follow releases, run:"
        )
        console.print(
            "    " + upgrade.reinstall_cmd(install),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )


def _run_update_with_progress(
    install: upgrade.InstallInfo, pipx: str, *, latest: str | None
) -> upgrade.UpdateOutcome:
    """Render a spinner from actual installer counters and phase changes.

    No bar: most phases report no fraction, so a bar could only sit at 0%
    until completion jumped it to 100%. The spinner shows activity, the
    description names the current step, and the detail shows a counter's
    real percentage when the installer reports one. A timer thread redraws
    the current state so the spinner keeps animating through silent steps;
    measured text still moves only on installer output.

    Display exceptions never replace the outcome or an interruption from
    `update`. Rich reports a closed pipe as SystemExit, so that counts as a
    display failure too. Blocked terminal writes can still delay rendering
    and completion; the join timeout bounds only the ticker join.
    """
    # FORCE_COLOR can make a pipe look like a terminal; only a real one
    # draws, so a reader that exits early never reaches the installer.
    if not (console.is_terminal and console.file.isatty()):
        return upgrade.run_update(install, pipx, latest=latest)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", markup=False),
        TextColumn("{task.fields[detail]}", markup=False),
        console=console,
        # Rich's own refresh thread dies with a traceback on a failed draw;
        # the ticker below turns itself off instead, per the display contract.
        auto_refresh=False,
        redirect_stdout=False,
        redirect_stderr=False,
    )
    task = progress.add_task("Starting installer", total=1, detail="")

    def render(state: updateprogress.UpdateProgress, *, complete: bool = False) -> None:
        measured = state.total is not None and state.completed is not None
        detail = f"{100 * state.completed / state.total:.1f}% {state.detail}" if measured else ""
        progress.update(
            task,
            # Phase counters are detail only. Completing Rich's task would
            # hide the spinner during later phases, including verification.
            completed=1 if complete else 0,
            description=state.phase,
            detail=detail.rstrip(),
            refresh=True,
        )

    def settle(phase: str, *, complete: bool = False) -> None:
        with suppress(Exception, SystemExit):
            render(updateprogress.UpdateProgress(phase), complete=complete)

    stop_ticking = threading.Event()

    def tick() -> None:
        while not stop_ticking.wait(core._UPDATE_SPINNER_TICK_SECONDS):
            try:
                progress.refresh()
            except (Exception, SystemExit):  # First failed draw: timer off.
                return

    ticker = threading.Thread(target=tick, name="mm-update-spinner", daemon=True)
    try:
        progress.start()
    except (Exception, SystemExit):
        with suppress(Exception, SystemExit):
            progress.stop()
        with suppress(Exception, SystemExit):
            console.show_cursor(True)  # A failed start can skip Rich's own restore.
        return upgrade.run_update(install, pipx, latest=latest)
    try:
        with suppress(Exception):  # No ticker thread: state changes still redraw.
            ticker.start()
        try:
            outcome = upgrade.run_update(install, pipx, latest=latest, on_progress=render)
        except BaseException as error:
            settle("Cancelled" if isinstance(error, KeyboardInterrupt) else "Failed")
            raise
        complete = outcome.status in ("updated", "unchanged") and (
            latest is None or Version(outcome.new or "") >= Version(latest)
        )
        settle(
            "Done" if complete else "Not started" if outcome.status == "busy" else "Failed",
            complete=complete,
        )
        return outcome
    finally:
        stop_ticking.set()
        with suppress(Exception, SystemExit):
            # ident also covers an interruption after native startup but
            # before Thread.start returned; an unstarted thread cannot join.
            if ticker.ident is not None:
                ticker.join(timeout=core._UPDATE_SPINNER_JOIN_SECONDS)
        # Avoid another wait on Rich if the ticker is still writing. This
        # timeout bounds only join: settlement above and later result output
        # can still block on a paused terminal. The ticker remains a daemon.
        if not ticker.is_alive():
            with suppress(Exception, SystemExit):
                progress.stop()


@app.command()
def update() -> None:
    """Update mm to the latest release.

    Runs pipx for you: an in-place pipx upgrade when this install tracks the
    release branch, or the @latest reinstall when it is pinned to an old tag.
    Needs no config and no passphrase, so it works on a Mac where sync is
    refusing. pull and push do this on their own unless the
    upgrade.auto_install setting is false.

    Exit 0 means mm is on the latest release. Exit 1 means the update did not
    complete, or this mm was not installed from the release branch by pipx.
    """
    install = upgrade.detect_install()
    refusal = _update_refusal(install)
    if refusal is not None:
        core._error(refusal)
    pipx = upgrade.find_pipx()
    if pipx is None:
        core._error(f"pipx was not found. Install pipx, then run: {upgrade.INSTALL_CMD}")
    assert pipx is not None

    # The user asked: the 24h throttle, auto_check and --no-check-version do
    # not apply.
    check = upgrade.check_for_upgrade(force=True)
    pinned = install.kind == "pinned"
    latest = check.latest if check.state in ("current", "upgrade-available") else None
    if latest is not None and Version(install.version or "") >= Version(latest):
        _print_update_current(install)
        return
    if pinned and latest is None:
        # An unreachable GitHub is no verdict. The in-place upgrade below can
        # still let pipx decide; the forced reinstall a pinned install needs
        # deletes the venv if it fails, so it never runs on a guess.
        core._error(
            "Could not reach GitHub to check for a newer release, and this install is pinned "
            f"to {install.spec}. Retry when online, or run: {upgrade.INSTALL_CMD}"
        )

    try:
        acquire_lock()
    except LockError as e:
        core._error(str(e))
    try:
        # Another updater may have completed while the forced HTTP check ran.
        # Reclassify under the mm lock before choosing a destructive reinstall.
        install = upgrade.detect_install()
        refusal = _update_refusal(install)
        if refusal is not None:
            core._error(refusal)
        if latest is not None and Version(install.version or "") >= Version(latest):
            _print_update_current(install)
            return
        if install.kind == "pinned" and latest is None:
            core._error("The install is now pinned; retry mm update when GitHub is reachable.")
        argv = upgrade.update_argv(pipx, install)
        shown = " ".join(["pipx", *argv[1:]])
        target = f" → {safe_str(latest)}" if latest is not None else ""
        console.print(f"Updating mm {safe_str(install.version)}{target} ({shown})…")
        outcome = _run_update_with_progress(install, pipx, latest=latest)
    except KeyboardInterrupt:
        print(
            "Update cancelled. If mm is now missing, reinstall with: "
            f"{upgrade.reinstall_cmd(install)}",
            file=sys.stderr,
        )
        raise
    finally:
        release_lock()

    if outcome.status == "busy":
        core._error(f"Update not started: {outcome.detail}. See {upgrade.update_log_path()}.")
    if outcome.status == "failed":
        if outcome.output:
            print(strip_terminal_escapes(outcome.output.rstrip()), file=sys.stderr)
        # pipx removes the venv when a forced reinstall fails; say so rather
        # than leave the user with `mm: command not found` and no command.
        lost = f" If mm is now missing, reinstall with: {upgrade.reinstall_cmd(install)}"
        core._error(f"Update did not complete: {outcome.detail}.{lost}")
    if outcome.status == "unchanged":
        if latest is not None and Version(outcome.new or "") < Version(latest):
            core._error(
                f"pipx found no build newer than {outcome.old}, but {latest} is tagged. "
                f"Retry in a minute, or run: {upgrade.INSTALL_CMD}"
            )
        console.print(f"mm {safe_str(outcome.old)} is already the latest release pipx can see.")
        return
    if outcome.new != outcome.old:
        moved = f"{safe_str(outcome.old)} → {safe_str(outcome.new)}"
        console.print(f"[bold green]Updated mm {moved}.[/bold green]")
    if outcome.now_tracking:
        console.print("  This install now tracks the release branch.")


# ── refresh-identity ──────────────────────────────────────────────────


@app.command(name="refresh-identity")
def refresh_identity_cmd(
    json_output: bool = typer.Option(
        False, "--json", help="Emit the resulting email list as JSON to stdout."
    ),
) -> None:
    """Force-refresh the local identity cache.

    The cache feeds the ``local_emails`` field on every ``mm push``
    event (v0.11.17+), which the retro-fleet aggregator unions across
    peers to build a fleet-wide author-email trust set. The cache
    refreshes itself on a 7-day TTL automatically; this subcommand is the
    explicit knob for picking up identity changes immediately:

    * after editing ``[retro].author_emails`` in mm ``config.toml``
    * after ``gh auth login`` / changing the GitHub CLI account
    * after editing ``git config --global user.email``
    * after adding a per-repo ``user.email`` override

    Sources unioned: global git config, per-repo git config (every
    discovered git root, bounded by a 5s wall-clock budget), mm's
    ``[retro].author_emails`` config knob, and the GitHub
    ``<id>+<login>@users.noreply.github.com`` form when ``gh`` is
    authenticated. A failed source contributes nothing — the cache still
    rebuilds with what was reachable.
    """
    previous = identity.read_cached_identities()
    try:
        config = load_config()
    except ConfigError:
        discovery = None
    else:
        discovery = events.discover_git_roots(config, persist=True)
    emails = identity.refresh_identity_cache(force=True, root_discovery=discovery)
    if json_output:
        typer.echo(json.dumps(sorted(emails)))
        return
    if emails:
        typer.echo(f"Refreshed identity cache: {len(emails)} email(s)")
        for e in emails:
            typer.echo(f"  {e}")
    if previous is not None and set(previous) != set(emails):
        added = [f"+{strip_terminal_escapes(e)}" for e in sorted(set(emails) - set(previous))]
        removed = [f"-{strip_terminal_escapes(e)}" for e in sorted(set(previous) - set(emails))]
        typer.echo(f"Changes vs previous cache: {' '.join(added + removed)}")
    if not emails:
        typer.echo(
            "mm: warning: no author emails resolved — check `git config "
            "--global user.email`, `gh auth status`, and "
            "`[retro].author_emails` in your mm config",
            err=True,
        )
        raise typer.Exit(code=1)


# ── log ───────────────────────────────────────────────────────────────


_LogAction = Literal[
    "written",
    "merged",
    "merged-via-lcs",
    "skipped",
    "conflicted",
    "excluded",
    "uploaded",
    "failed",
]
_LogVerb = Literal["pull", "push", "self-upgrade"]


@app.command(name="log")
def log_cmd(
    source: str | None = typer.Option(
        None, "--source", help="Filter by source name (e.g. 'claude', 'gstack')."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only entries at/after this ISO-8601 timestamp."
    ),
    action: _LogAction | None = typer.Option(
        None,
        "--action",
        help="Filter by per-file action.",
        case_sensitive=False,
    ),
    verb: _LogVerb | None = typer.Option(
        None,
        "--verb",
        help="Filter by pull, push or self-upgrade.",
        case_sensitive=False,
    ),
    limit: int = typer.Option(
        50, "--limit", "-n", help="Show at most N records (most recent first)."
    ),
    fmt: str = typer.Option(
        "table",
        "--format",
        help="Output format: 'table' (human) or 'jsonl' (machine).",
        case_sensitive=False,
    ),
) -> None:
    """Query the pull/push history log.

    Reads ~/.config/mind-meld/state.db after its one-time legacy
    pull-history.jsonl import; before that, reads the JSONL. Keeps the newest
    20,000 rows per (verb, action) class. --limit 0 materializes every matching
    row in one snapshot and closes the connection before printing; memory grows
    with retained history. Useful for conflict diagnosis and exclusion audits
    via `mm log --action excluded`. Inspection writes nothing.
    """
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            core._error(f"--since: not a valid ISO-8601 timestamp: {since!r}")
            return  # unreachable
    else:
        since_dt = None

    fmt_lower = fmt.lower()
    if fmt_lower not in ("table", "jsonl"):
        core._error(f"--format must be 'table' or 'jsonl', got {fmt!r}")
        return  # unreachable

    try:
        rows = pullhistory.query(
            source=source, action=action, verb=verb, since=since_dt, limit=limit
        )
    except statedb.StateDBError as exc:
        typer.echo(safety.safe_terminal_str(pullhistory.explain(exc)), err=True)
        raise typer.Exit(1) from None
    for notice in rows.notices:
        typer.echo(safety.safe_terminal_str(pullhistory.explain(notice)), err=True)

    if not rows:
        if fmt_lower == "jsonl":
            return
        console.print("[dim]No log entries.[/dim]")
        return

    if fmt_lower == "jsonl":
        import json as _json

        for r in rows:
            typer.echo(_json.dumps(r, sort_keys=True))
        return

    table = Table(title=f"mm log ({len(rows)} entries · {rows.origin})")
    table.add_column("ts", overflow="fold")
    table.add_column("verb")
    table.add_column("action")
    table.add_column("source")
    table.add_column("path", overflow="fold")
    # `extra` column carries self-upgrade row details ("0.9.3 → 0.9.4")
    # without overloading the source/path columns. Empty for pull/push
    # rows. D8 locked this shape per Codex outside voice.
    table.add_column("extra", overflow="fold")
    for r in rows:
        verb = r.get("verb", "?")
        if verb == "self-upgrade":
            old = r.get("old_version", "?")
            new = r.get("new_version", "?")
            table.add_row(
                safe_str(r.get("ts", "?")),
                safe_str(verb),
                "—",
                "—",
                "—",
                safe_str(f"{old} → {new}"),
            )
        else:
            table.add_row(
                safe_str(r.get("ts", "?")),
                safe_str(verb),
                safe_str(r.get("action", "?")),
                safe_str(r.get("source", "?")),
                safe_str(r.get("rel_path", "?")),
                "",
            )
    console.print(table)


# ── conflicts / resolve ───────────────────────────────────────────────


@app.command()
def conflicts() -> None:
    """List .sync-conflict-* files across all synced sources.

    Per-row column meaning depends on the filename prefix:
      * `v0-` (pre-inversion, pre-v0.9.2 conflict file) — sidecar holds
        LOCAL bytes; canonical holds REMOTE.
      * no prefix (post-inversion, v0.9.2+) — canonical holds LOCAL;
        sidecar holds REMOTE.

    Read-only: never mutates conflict file names. Migration to the `v0-`
    prefix happens lock-protected in `mm pull` and `mm resolve` only —
    `mm conflicts` is lockless and any rename here would race autopull.
    """
    config = core._get_config(read_only=True)
    hits = resolveflow._find_conflict_files(config)
    if not hits:
        console.print("[green]No conflict files.[/green]")
        return

    table = Table(title=f"Conflict files ({len(hits)})")
    table.add_column("Source")
    table.add_column("Mode")
    table.add_column("local", no_wrap=False, overflow="fold")
    table.add_column("remote", no_wrap=False, overflow="fold")
    table.add_column("Conflict age")
    table.add_column("Peer edit age")
    now = datetime.now(timezone.utc)
    pre_inversion_seen = False
    for src_name, cpath, canonical in sorted(hits, key=lambda h: str(h[1])):
        created = parse_conflict_created_at(cpath.name)
        if created is None:
            conflict_age = "?"
        else:
            conflict_age = format_age_delta((now - created).total_seconds())
        try:
            mtime = datetime.fromtimestamp(cpath.stat().st_mtime, tz=timezone.utc)
            peer_age = format_age_delta((now - mtime).total_seconds())
        except OSError:
            peer_age = "?"
        is_pre = is_pre_inversion_conflict_filename(cpath.name)
        if is_pre:
            pre_inversion_seen = True
            mode = "[yellow]pre-v0.9.2[/yellow]"
            # Pre-inversion: sidecar = local, canonical = remote.
            local_display = safe_str(cpath)
            remote_display = safe_str(canonical) if canonical else "[dim](gone)[/dim]"
        else:
            mode = "v0.9.2+"
            # Post-inversion: canonical = local, sidecar = remote.
            local_display = safe_str(canonical) if canonical else "[dim](gone)[/dim]"
            remote_display = safe_str(cpath)
        table.add_row(src_name, mode, local_display, remote_display, conflict_age, peer_age)
    console.print(table)
    console.print(
        "\nRun [bold]mm resolve[/bold] to keep local, remote, or both "
        "interactively, or delete files manually with [bold]rm[/bold]."
    )
    if pre_inversion_seen:
        console.print(
            "\n[dim]Pre-v0.9.2 conflict files are listed in the table — "
            "run [bold]mm resolve[/bold] to migrate them to the new "
            "filename convention before resolving.[/dim]"
        )


# ── recover ───────────────────────────────────────────────────────────


def _quarantine_corrupt_manifest(
    backend: LocalBackend,
    storage_root: Path,
    device_id: str,
) -> Path:
    """Crash-durable move of a corrupt manifest blob to a quarantine sibling.

    Uses read-then-atomic-write-then-unlink (matching the discipline at
    storage/local.py:45 and sidecar.py:54) rather than plain os.rename.
    A power loss between steps leaves the source intact or the destination
    fully written — never both gone.

    Collision handling: if `<key>.corrupt-<ts>` already exists (a second
    quarantine within the same second), append a 4-char random suffix.

    Returns the quarantine path.
    """
    mkey = manifest_key(device_id)
    src = storage_root / mkey
    if not src.exists():
        raise FileNotFoundError(str(src))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidates = [
        src.with_name(src.name + f".corrupt-{ts}"),
        src.with_name(src.name + f".corrupt-{ts}-{secrets.token_hex(2)}"),
    ]
    dst = next((c for c in candidates if not c.exists()), None)
    if dst is None:
        # Both collided — extremely improbable, but pick a guaranteed-unique name.
        dst = src.with_name(src.name + f".corrupt-{ts}-{secrets.token_hex(4)}")

    data = src.read_bytes()
    # atomic_write_bytes with fsync=True ensures dst is durably written
    # before we unlink src.
    fsutil.atomic_write_bytes(dst, data, fsync=True)
    os.unlink(src)
    # Also fsync the parent directory so the unlink is durable. Best-effort
    # — the file content is already durable at dst. Worst-case on a crash
    # mid-fsync: src may reappear on next boot, but dst still has the
    # quarantined copy.
    try:
        fsutil.fsync_dir(src.parent)
    except (OSError, StorageError):
        pass
    return dst


@app.command()
def recover(
    abandon_manifest: bool = typer.Option(
        False,
        "--abandon-manifest",
        help=(
            "Quarantine this device's corrupt manifest and allow the next "
            "push to start fresh. DESTRUCTIVE: files deleted locally since "
            "the last successful push will lose their deletion records."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the typed-RESET confirmation (for scripted recovery).",
    ),
) -> None:
    """Last-resort escape hatch for corrupt-manifest recovery.

    Use ONLY when `mm push` refuses with "remote manifest corrupt, no
    local sidecar, and no peer manifests." Running this when the recovery
    chain has a viable source (sidecar or peers) is wasted destructiveness
    — push will self-heal through the normal chain.

    See SPEC.md "Manifest corruption recovery" / "Last-resort escape
    hatch" for the full contract.
    """
    if not abandon_manifest:
        core._error(
            "mm recover requires a recovery mode flag. Today the only mode "
            "is --abandon-manifest (quarantine a corrupt manifest and allow "
            "the next push to start fresh)."
        )

    config = core._get_config(read_only=False)
    passphrase = core._get_passphrase_or_exit()
    device_id = config["device"]["id"]
    storage_path = config["storage"]["path"]
    storage_root = Path(storage_path).expanduser()

    backend = get_backend(config)
    try:
        memory_kb = core._init_crypto_session(backend, passphrase, config)
    except MindMeldError as e:
        core._error(str(e))

    # Refuse-when-healthy: if the normal recovery chain has any viable
    # source, this command has no business running.
    fetch = core._fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    if fetch.newer_version is not None:
        core._error(core._newer_format_message(fetch.newer_version, "manifest"))
    if fetch.is_ok:
        core._error(
            "remote manifest is readable — recovery is not required. "
            "If you meant to investigate a different device's corrupt "
            "manifest, re-run mm push from that device."
        )
    if fetch.status == "missing":
        core._error(
            "no manifest exists for this device at "
            f"{manifest_key(device_id)} — nothing to quarantine."
        )

    # Sidecar and peer tombstones are the non-destructive paths. If either
    # exists, refuse: running --abandon-manifest would lose fresh deletions
    # that the normal push recovery chain would preserve.
    sidecar_manifest = sidecar.read(device_id)
    if sidecar_manifest is not None:
        core._error(
            f"local sidecar is present at {sidecar.sidecar_path()}. "
            f"Run 'mm push' — the normal recovery chain will use the "
            f"sidecar to preserve fresh deletions. --abandon-manifest "
            f"would throw those deletion records away."
        )
    peer_tombstones = core._collect_peer_tombstones(backend, device_id, passphrase, memory_kb)
    if peer_tombstones:
        core._error(
            f"peer manifests carry {len(peer_tombstones)} tombstone(s) that "
            f"'mm push' would use as a recovery source. Run 'mm push' instead "
            f"— --abandon-manifest would discard these tombstone records."
        )

    # Loud warning before the typed prompt. stderr_console so the UX is
    # consistent with other destructive paths (init BRICK).
    stderr_console.print(
        "\n[red]DANGER:[/red] this will QUARANTINE this device's corrupt "
        "manifest and allow the next push to start fresh with [bold]no "
        "prior-state knowledge[/bold]. Files you deleted locally since the "
        "last successful push will no longer propagate as deletions — peers "
        "will see those files come back on their next pull.\n"
    )
    _mkey_display = manifest_key(device_id)
    stderr_console.print(
        f"  Source blob:      {_mkey_display}\n"
        f"  Quarantine name:  {_mkey_display}.corrupt-<timestamp>\n"
        f"  Storage root:     {storage_root}\n"
    )

    if not yes:
        typed = typer.prompt('Type "RESET" (case-sensitive) to confirm and proceed')
        if typed != "RESET":
            stderr_console.print("[yellow]Aborted.[/yellow] Nothing changed.")
            raise typer.Exit(1)

    try:
        quarantine_path = _quarantine_corrupt_manifest(backend, storage_root, device_id)
    except FileNotFoundError:
        core._error(f"{manifest_key(device_id)} not found on disk. Nothing to quarantine.")
    except OSError as e:
        core._error(f"quarantine failed: {e}")

    console.print(f"[green]Quarantined[/green] corrupt manifest to [dim]{quarantine_path}[/dim].")
    console.print(
        "Next 'mm push' will start fresh with no prior-state manifest. "
        "The quarantined copy is preserved for post-mortem and can be "
        "deleted manually once you've confirmed recovery."
    )


@app.command()
def resolve(
    path: str | None = typer.Argument(
        None,
        help="Specific conflict path to resolve. If omitted, walks all conflicts.",
    ),
) -> None:
    """Interactively resolve .sync-conflict-* files.

    For each conflict: prints color LOCAL/REMOTE banners (with peer-name
    attribution when known), a 3-number divergence summary, the unified
    diff, then prompts:
      (l)ocal / (r)emote / (m)erge / (p)romote / (s)kip [default] / (a)bort.

    (l)ocal keeps your edits on this machine and discards the bytes from
    the other machine.
    (r)emote keeps the bytes from the other machine and discards your
    local edits on this conflict.
    (n)ewer keeps the more recently modified side; equal or unreadable
    mtimes re-prompt for a manual choice.
    (m)erge accepts the LCS-merged result over canonical (offered only
    when the content is text; never the default key -- you must type it).
    (p)romote keeps BOTH: renames the .sync-conflict-* sidecar to its own
    first-class filename. Use this when the two files turned out to be
    different documents that merely collided on a name. The promoted
    file syncs only when the source's include rules cover that new
    name (include_files sources match exact configured names, so a
    from-/local- name will not sync until you add it).
    (s)kip leaves both files on disk for now; you can run `mm resolve`
    again later or delete the .sync-conflict-* file manually. The next
    `mm pull` does NOT re-prompt unless remote changes again. A later
    pull of different bytes from the same peer may replace that managed
    sidecar after the new copy is published; promote or copy it out of
    managed naming to keep a durable editable document. Sidecars stay
    local-only. Deleting a sidecar here does not delete anything on
    other machines.
    (a)bort exits the resolve walk; previously-resolved conflicts stay
    resolved.

    Canonical deletions and renames propagate on the next `mm push` via
    the existing tombstone / additive-sync machinery when the path is
    in source scope. Sidecar deletion is local.

    Acquires the mm lockfile so an autopull running in parallel can't
    race with our rename/unlink operations on the synced files.

    `c` / `f` are rejected with exit 1. Any other unrecognized input,
    including `b` / `both` (alias removed in 1.0.0), skips without a notice.
    """
    config = core._get_config(read_only=False)
    backend = get_backend(config)

    try:
        acquire_lock()
    except LockError as e:
        core._error(str(e))

    failed = 0
    try:
        # Lock-protected discovery: opt into pre-inversion migration so any
        # legacy `.sync-conflict-<ts>-<dev>.<ext>` files get renamed to the
        # `v0-` prefix before resolve dispatches on the prefix below.
        hits = resolveflow._find_conflict_files(config, migrate_pre_inversion=True)

        if path:
            target = Path(path).expanduser().resolve()
            hits = [h for h in hits if h[1] == target]
            if not hits:
                core._error(f"No conflict file matching: {path}")

        if not hits:
            console.print("[green]No conflict files.[/green]")
            return
        # Cache the device list ONCE -- N-conflict walks would otherwise hit
        # storage N times to attribute the REMOTE side, and iCloud cold-cache
        # reads can spike to multi-second per call.
        devices = list_devices(backend)
        sources_by_name = {s["name"]: s for s in get_sources(config)}
        _, failed = resolveflow._resolve_interactive_loop(hits, devices, sources_by_name)
    finally:
        release_lock()

    if failed:
        # Surface partial-failure as a non-zero exit so CI / scripts driving
        # `mm resolve` can detect that some conflicts were not actually
        # resolved (rename/unlink/read errors mid-walk). Walk continues
        # through every conflict; only the exit code reflects the failure.
        raise typer.Exit(1)


# ── auto commands (hook-safe: silent, never-prompt, typed errors) ─────


_AUTO_LOG_MAX_BYTES = 1_000_000
_AUTO_LOG_KEEP_BYTES = 512_000


def _write_migration_breadcrumb(missing: list[str]) -> None:
    """Best-effort write; never raises except the pytest guard BaseException.

    Skipped when no sources are missing (delete the file so stale
    breadcrumbs don't outlive their cause). Called from autopull/autopush
    on every run; cost is one stat + (rarely) one write.
    """
    path = core._migration_state_path()
    fsutil.refuse_real_state_write_under_pytest(path)
    if not missing:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return
        return
    try:
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        import json as _json

        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "missing_sources": sorted(set(missing)),
        }
        path.write_text(_json.dumps(payload, indent=2))
    except Exception:
        # Forensic aid only; never block the calling auto-command.
        pass


def _write_autorun_breadcrumb(verb: str, outcome: str, detail: str = "") -> None:
    """Record the last autopull/autopush attempt for forensic observability.

    Written on EVERY invocation -- success, lock-skip, config-missing,
    typed error, unexpected error. `mm status` surfaces this so a user can
    see 'last auto-sync attempt: 3h ago, skipped (lock held)' instead of
    wondering why sync appears wedged.

    Keyed per verb so README's documented lifecycle (autopull at
    conversation start, autopush at end) cannot erase the other verb's
    crumb. The shared JSON lock serializes concurrent sibling hooks, so a
    lock-held pull cannot replace a degraded push read from a stale payload.
    Silent contract preserved: nothing is printed. Any failure here is
    swallowed -- a broken breadcrumb must never crash the hook.
    """
    entry: dict[str, str] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
    }
    if detail:
        entry["detail"] = detail
    try:
        with locked_json_rmw(core._autorun_breadcrumb_path()) as ljson:
            if not ljson.is_locked:
                return
            existing = core._normalize_autorun_breadcrumbs(ljson.data)
            existing[verb] = entry
            ljson.data.clear()
            ljson.data.update({k: existing[k] for k in core._AUTORUN_VERBS if k in existing})
    except Exception:
        pass


def _auto_log_path(verb: str) -> Path:
    """`auto{verb}.log` next to the recovery sidecar.

    Resolves `sidecar.SIDECAR_DIR` at call time (not import time) so a test
    monkeypatching `mind_meld.sidecar.SIDECAR_DIR` alone is enough to redirect
    the log location. Import-time capture would be a foot-gun — a test that
    forgets to patch both the source constant and this module's alias would
    silently leak log writes to the user's real `~/.config/mind-meld/`.
    """
    return sidecar.SIDECAR_DIR / f"auto{verb}.log"


def _log_unexpected(verb: str, exc: BaseException) -> None:
    """Append a traceback block to ~/.config/mind-meld/auto{verb}.log.

    Hand-rolled (not `logging.handlers.RotatingFileHandler`) because this runs
    from short-lived typer commands: a named logger would either stack handlers
    across in-process test calls, or need explicit teardown.

    When the file exceeds `_AUTO_LOG_MAX_BYTES`, keep only the last
    `_AUTO_LOG_KEEP_BYTES` so the freshest tracebacks survive. The whole
    truncate/append sequence is guarded by `fcntl.flock(LOCK_EX)` so two
    racing failed hooks can't corrupt the log -- the original concern was
    that process A's truncate followed by process B's stat-read-truncate
    could silently erase A's just-written traceback, killing the one forensic
    artifact at the exact moment we needed it.

    Cause-chain logging (see docstring in _should_log_cause): if `exc.__cause__`
    is set (typed-error raised via `raise X from e`), we include the full
    chain so a `ConfigError` wrapping a `PermissionError` doesn't lose the
    original OSError + errno. For typed errors with no cause, this function
    is not called -- the caller short-circuits to stderr-only.

    Any failure here is swallowed; the caller has already emitted the
    one-line stderr message, and a broken log file must never crash the hook.
    The pytest write guard BaseException deliberately escapes this boundary.
    """
    try:
        path = _auto_log_path(verb)
        fsutil.refuse_real_state_write_under_pytest(path)
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        block = (
            f"--- {datetime.now(timezone.utc).isoformat()} mm {__version__} "
            f"auto{verb}\n"
            f"{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}"
            f"\n"
        )
        # Open read-write, create if missing. One fd, one lock, full rotate+append
        # in a single critical section. "ab+" would truncate to end on open on
        # some platforms; "a+b" is portable and gives us seek freedom.
        with open(path, "a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0, 2)  # end
                size = f.tell()
                if size > _AUTO_LOG_MAX_BYTES:
                    f.seek(size - _AUTO_LOG_KEEP_BYTES)
                    tail = f.read()
                    f.seek(0)
                    f.truncate()
                    f.write(tail)
                f.write(block.encode("utf-8"))
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        # Logging a traceback must never mask the real error.
        pass


@dataclass
class _AutoSetup:
    config: dict
    passphrase: str
    memory_kb: int


def _should_log_cause(exc: BaseException) -> bool:
    """Log a typed error iff it wraps a non-typed cause.

    Pure validation errors like `ConfigError("missing [device] section")` are
    raised without `from`, so `__cause__` is None and the stderr one-liner
    already contains everything a user needs. Adversarial/environmental
    errors wrap an underlying exception via `raise X from e` -- e.g.
    `ConfigError("failed to parse config.toml - ...") from tomllib.TOMLDecodeError`
    or a future `ConfigError from PermissionError`. In those cases the
    stderr message loses the original traceback/errno, so the log is the
    only place the original cause survives.
    """
    return exc.__cause__ is not None


def _print_auto_typed_error(verb: str, verb_action: str, exc: BaseException) -> None:
    """Render a typed hook error safely on its plain stderr surface."""
    print(
        f"mm: {verb} {verb_action} - {strip_terminal_escapes(str(exc))}",
        file=sys.stderr,
    )


def _auto_command_setup(verb: str) -> _AutoSetup | None:
    """Load config + passphrase + crypto session for autopull/autopush.

    Returns None when the caller should exit (silent or after a typed error).
    Logging policy:
      - Typed error with no __cause__ (pure validation): stderr only, no log.
        Expected conditions (missing passphrase, missing field) don't need
        a traceback to diagnose.
      - Typed error WITH __cause__ (wrapped OSError / TOMLDecodeError / etc.):
        stderr + log. The wrapper message drops the underlying traceback +
        errno, so the log is the only forensic artifact.
      - Unexpected (non-MindMeldError) exception: always stderr + log.

    Semantics:
      - config missing          -> silent return (not initialized)
      - config typed no-cause   -> typed one-line stderr (no log)
      - config typed w/cause    -> typed one-line stderr + full chain to log
      - config unexpected error -> one-line stderr + traceback to log
      - keyring cannot answer   -> remedy one-line stderr + keyring-locked /
        without a prompt           keyring-no-collection breadcrumb (no log)
      - keyring + env empty     -> typed one-line stderr (no log)
      - crypto typed no-cause   -> typed one-line stderr (no log)
      - crypto typed w/cause    -> typed one-line stderr + full chain to log
      - crypto unexpected error -> one-line stderr + traceback to log

    Does NOT acquire the lockfile; caller owns that and decides lock-policy.

    Writes a breadcrumb (`last-autorun.json`) on every early-exit path so
    `mm status` can surface silent-skip history.
    """
    # All CONFIG_PATH access goes through `_config_module.CONFIG_PATH` so
    # `monkeypatch.setattr("mind_meld.config.CONFIG_PATH", ...)` propagates.
    # `load_config()` already resolves through the module attribute; a
    # local `from mind_meld.config import CONFIG_PATH` binding would let
    # the two diverge under test (the silent-mode contract regression
    # that surfaced in v0.8.15).
    if not _config_module.CONFIG_PATH.exists():
        _write_autorun_breadcrumb(verb, "config-missing")
        return None

    try:
        config = load_config()
    except MindMeldError as e:
        _print_auto_typed_error(verb, "failed", e)
        if _should_log_cause(e):
            _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "config-error", str(e))
        return None
    except Exception as e:
        print(
            f"mm: {verb} failed - unexpected config error (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "config-error", type(e).__name__)
        return None

    # Seam 1 — transition detection on the silent autopull/autopush path.
    # Same hook as `_get_config`, but routed here directly to preserve this
    # function's silent-on-error contract (we MUST NOT shout on Claude Code
    # session start when config is broken or missing).
    upgrade.run_transition_hook(config)

    try:
        passphrase = get_passphrase(non_interactive=True)
    except KeyringUnavailableError as e:
        # Track 73A: the keyring cannot answer without a desktop prompt.
        # Skip with the remedy on stderr and a breadcrumb; never prompt,
        # never log a traceback for this expected condition.
        print(
            f"mm: {verb} skipped - {strip_terminal_escapes(e.render(verb))}",
            file=sys.stderr,
        )
        if e.reason == "no-default-collection":
            _write_autorun_breadcrumb(verb, "keyring-no-collection")
        else:
            _write_autorun_breadcrumb(verb, "keyring-locked", e.reason)
        return None
    except CryptoError as e:
        _print_auto_typed_error(verb, "skipped", e)
        # Detail: the backend this hook process saw (Track 73A). Hooks run
        # in the agent's environment, which `mm diag` in a terminal may not
        # share, so `mm status` shows what the hook actually used.
        _write_autorun_breadcrumb(verb, "no-passphrase", crypto._keyring_backend_label())
        return None
    except Exception as e:
        # crypto.get_passphrase narrowed its keyring catch to
        # (KeyringError, ImportError) in v0.8.9. Other exception kinds
        # (an unexpected OSError from a backend, RuntimeError or a
        # secretstorage error from a broken Linux DBus session) now
        # propagate here; a locked or denied keyring takes the
        # KeyringUnavailableError arm above (Track 73A). Without this guard they
        # would escape the hook entirely — no breadcrumb, no stderr line,
        # silent sync stop. Mirrors the backend-error shape below.
        print(
            f"mm: {verb} failed - keyring error (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "keyring-error", type(e).__name__)
        return None

    # get_backend() can still raise on malformed config["storage"]["path"]
    # (load_config validates presence, not types). Guard it the same way
    # as the crypto-session call so the hook contract holds end-to-end.
    try:
        backend = get_backend(config)
    except Exception as e:
        print(
            f"mm: {verb} failed - backend init failed (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "backend-error", type(e).__name__)
        return None

    try:
        memory_kb = core._init_crypto_session(backend, passphrase, config)
    except MindMeldError as e:
        _print_auto_typed_error(verb, "failed", e)
        if _should_log_cause(e):
            _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "crypto-error", str(e))
        return None
    except Exception as e:
        print(
            f"mm: {verb} failed - unexpected crypto error (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "crypto-error", type(e).__name__)
        return None

    return _AutoSetup(config=config, passphrase=passphrase, memory_kb=memory_kb)


@contextmanager
def _auto_command_scope(
    verb: str,
    *,
    typer_exit_outcome: str,
) -> Iterator[_AutoSetup | None]:
    """Own the shared unattended-command control flow.

    Pull and push keep their own tail bodies and success/degradation outcome
    mappings. This scope owns only the shared setup, migration breadcrumb,
    lock lifecycle, and common exception-to-breadcrumb policy.
    """
    setup = _auto_command_setup(verb)
    if setup is None:
        yield None
        return

    _write_migration_breadcrumb(core._config_missing_recommended_excludes(setup.config))

    try:
        acquire_lock()
    except LockError:
        _write_autorun_breadcrumb(verb, "lock-held")
        yield None
        return

    try:
        yield setup
    except typer.Exit:
        _write_autorun_breadcrumb(verb, typer_exit_outcome)
    except MindMeldError as e:
        _print_auto_typed_error(verb, "failed", e)
        if _should_log_cause(e):
            _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "failed", str(e))
    except Exception as e:
        print(
            f"mm: {verb} failed - unexpected error (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "failed", type(e).__name__)
    finally:
        release_lock()


@app.command()
def autopull() -> None:
    """Pull changes silently. Designed for Claude Code -- no prompts, minimal output.

    Never prompts (`get_passphrase(non_interactive=True)`). Silent exit on:
    missing config, missing passphrase, lock contention. Loud exit (one-line
    stderr + traceback to `~/.config/mind-meld/autopull.log`) on: corrupt
    config, crypto init failure, unexpected bug inside `_pull_core`.
    """
    with (
        events_tail.diagnostic_scope(lambda message, degraded=False: None),
        pullhistory.command_scope(attended=False),
    ):
        with _auto_command_scope("pull", typer_exit_outcome="fleet-refused") as setup:
            if setup is None:
                return

            if not pullhistory.failed():
                pullhistory.record_failure(statedb.recover_if_needed())

            result = pullflow._pull_core(
                setup.config,
                setup.passphrase,
                setup.memory_kb,
                quiet=True,
                conflict_mode="keep-both",
            )

            if result.total_conflicted:
                print(
                    f"mm: {result.total_conflicted} conflicts - run 'mm conflicts' to review",
                    file=sys.stderr,
                )
            if result.total_skipped_unknown_source:
                print(
                    f"mm: skipped {result.total_skipped_unknown_source} unknown "
                    "source(s) - run 'mm sources' to reconcile config",
                    file=sys.stderr,
                )
            if result.total_failed:
                # Per-file failures (decrypt error, conflict rename failure, write
                # failure, ValueError on corrupted device_id) increment total_failed
                # in _apply_incoming_file. Without this stderr surface, autopull's
                # quiet contract silently swallows the summary too — exactly the
                # silent-failure pattern Track 1A's helper-level audit was meant
                # to close.
                print(
                    f"mm: {result.total_failed} file(s) failed to apply - "
                    "see the warnings above; they retry on the next pull",
                    file=sys.stderr,
                )

            # Degraded outcome: pull succeeded overall but some part of it is
            # at-risk. Mirrors the v0.8.1 `no-sources` autopush breadcrumb
            # pattern — without a distinct outcome, `mm status` shows the last
            # run as "success" indefinitely while data may not survive crash
            # (fsync) or may be partition-drifting (corrupt peers, unknown
            # sources, per-file failures). Stderr warnings for the same signals
            # already fire in _print_pull_summary (corrupt_peers/fsync_warnings)
            # and in the total_skipped_unknown_source / total_failed prints
            # earlier in this function. The breadcrumb makes the degradation
            # state persistent for monitoring on top of `mm status` / `mm diag`.
            degradations: list[str] = []
            if result.durability_fsync_failures:
                degradations.append(
                    f"fsync failed on {result.durability_fsync_failures} parent dir(s)"
                )
            if result.corrupt_peer_count:
                degradations.append(f"{result.corrupt_peer_count} corrupt peer manifest(s)")
            if result.total_skipped_unknown_source:
                degradations.append(f"{result.total_skipped_unknown_source} unknown source(s)")
            if result.total_failed:
                degradations.append(f"{result.total_failed} file(s) failed")

            if degradations:
                _write_autorun_breadcrumb("pull", "degraded", "; ".join(degradations))
            else:
                # Undelivered content is waiting, not degraded: the sync tool
                # is still delivering it and the next pull picks it up.
                _write_autorun_breadcrumb("pull", "success", pullflow.waiting_detail(result))

            # Seam 2 — self-update or nudge at the TAIL. Runs AFTER the main work
            # + breadcrumb so the cold-cache HTTP fetch latency (~500ms 1x/24h)
            # doesn't stack on sync latency. Unattended: a due update is spawned
            # detached and says nothing; the nudge prints only when no update can
            # run AND the 24h re-nudge gate permits.
            upgrade.update_or_nudge(setup.config, attended=False)


@app.command()
def autopush() -> None:
    """Push changes silently. Designed for Claude Code -- no prompts, minimal output.

    Never prompts (`get_passphrase(non_interactive=True)`). Silent exit on:
    missing config, missing passphrase, lock contention. Loud exit (one-line
    stderr + traceback to `~/.config/mind-meld/autopush.log`) on: corrupt
    config, crypto init failure, unexpected bug inside `_push_core`. No
    auto-GC on autopush (prevents blob-deletion hole).
    """
    diagnostics = events_tail.Degradations()

    def notice(message: str, degraded: bool = False) -> None:
        if degraded:
            diagnostics.append(message.removeprefix("mm: notice: ").strip())

    with (
        events_tail.diagnostic_scope(notice),
        pullhistory.command_scope(attended=False),
    ):
        with _auto_command_scope("push", typer_exit_outcome="refused") as setup:
            if setup is None:
                return

            if not pullhistory.failed():
                pullhistory.record_failure(statedb.recover_if_needed())

            with pullhistory.batch():
                result = _push_core(
                    setup.config,
                    setup.passphrase,
                    setup.memory_kb,
                    quiet=True,
                )

            if diagnostics.total:
                result = result or PushResult()
                safe_details, omitted = attemptlog.run_details(diagnostics)
                bounded = result.events_degradations
                bounded.extend(safe_details)
                # One omission phrase is written below from the bounded total.
                extra = omitted + diagnostics.omitted
                bounded.total = min(2**53, getattr(bounded, "total", 0) + extra)
                bounded.omitted = min(2**53, getattr(bounded, "omitted", 0) + extra)

            if result is None and not get_sources(setup.config):
                # Distinguish "broken config no-op" from "nothing to push" no-op
                # in the breadcrumb. _push_core already printed the stderr warning;
                # if `mm status` only sees "success" forever, monitoring never
                # catches the wedge.
                _write_autorun_breadcrumb("push", "no-sources")
                return

            # Persist events-tail degradation the same way autopull persists its
            # own (see the `degradations` list in `autopull` below). The tail is
            # forensic-only and swallows its failures behind a `mm: notice:` line,
            # but autopush runs unattended from a Claude Code hook — that stderr
            # reaches nobody, so pre-v0.12.16 the breadcrumb said `success` while
            # the retro pipeline was dead, and `mm status` repeated it. Same
            # argument docs/invariants/sync.md makes for the `no-sources` breadcrumb:
            # without this, `mm status` only ever sees `success` and monitoring
            # built on top of it never catches the wedge.
            events_degradations = result.events_degradations if result else []
            # Files held because they were still changing (Track 75A) are not a
            # failure but a push that is not yet complete: record the count and a
            # few paths so `mm status` can name what lags. Degradations are never
            # dropped when both exist; the push still exits 0 and prints nothing.
            held = result.held if result else []
            details = list(events_degradations)
            if getattr(events_degradations, "omitted", 0):
                details.append(
                    f"{events_degradations.omitted} diagnostic details omitted (run mm diag)"
                )
            if result and result.maintenance_failures:
                details.append(
                    f"{result.maintenance_failures} post-publication maintenance failure(s)"
                )
            if held:
                used = len("; ".join(details)) + (2 if details else 0)
                budget = max(_HELD_BREADCRUMB_BUDGET - used, 0)
                details.append(_held_breadcrumb_detail(held, budget))
            if details:
                _write_autorun_breadcrumb("push", "degraded", "; ".join(details))
            else:
                _write_autorun_breadcrumb("push", "success")

            # Seam 2 — self-update or nudge at the TAIL (mirrors autopull).
            upgrade.update_or_nudge(setup.config, attended=False)


if __name__ == "__main__":
    app()
