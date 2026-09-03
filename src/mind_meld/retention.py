"""Retention: every `mm gc` reaper, and the crashed-push tmp sweep.

Extracted from ``cli.py`` in Track 16A. Four reapers behind ``mm gc``:

* ``_gc_token_cache`` — session-token cache entries whose jsonl is gone, or
  whose newest day is over 90 days old
* ``_sweep_local_tmp_files`` — ``.tmp`` blobs left by a crashed push
* ``_gc_old_event_files`` — mm-events JSONLs past ``EVENTS_RETENTION_DAYS``
* ``_gc_old_conflict_files`` — ``.sync-conflict-*`` sidecars past
  ``CONFLICT_AGE_DAYS`` (apply with ``--conflicts``; previewed by
  bare ``mm gc --dry-run``). Age is the filename timestamp, not
  ``st_mtime`` (the sidecar's mtime is the peer file's clock).
* ``_gc_orphan_retros_dir`` — leftover v0.12.0 ``YYYY-MM-DD-NNN.json``
  snapshot files, then ``rmdir`` if empty (never ``rm -rf``)

Depends on ``resolveflow._find_conflict_files``, which is why this module lands
AFTER resolveflow in the Track 16A series and why ``retention -> resolveflow``
is the one intra-Track edge. Imports nothing from ``cli`` — pinned by
``tests/test_module_boundaries.py``.

``CONFLICT_AGE_DAYS`` is owned here but read back by ``cli``: the ``gc`` command
interpolates it into its ``--conflicts`` help string, which typer evaluates at
DECORATOR time. That is why a deferred/function-local import would not have
worked as a cycle workaround.

Track 17D keeps these reapers plan-first: dry-run selects the same candidates
as apply without writing a cache or unlinking a path, then reports a stable
summary for each reaper that actually runs.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mind_meld import resolveflow, token_usage
from mind_meld.config import get_sources
from mind_meld.consoles import console
from mind_meld.manifest import (
    hash_file,
    is_pre_inversion_conflict_filename,
    parse_conflict_created_at,
)
from mind_meld.safety import safe_str
from mind_meld.storage.local import LocalBackend

CONFLICT_AGE_DAYS = 30
# Track 7B (v0.10.3): per-device daily JSONL events files older than this
# are reaped at every `mm gc`. The retention is fleet policy, not per-
# device opt-in: a stale device's old events would otherwise pin storage
# forever via tombstone propagation. Reap by FILENAME date (Codex C5,
# C6) — iCloud restores produce misleading mtimes.
EVENTS_RETENTION_DAYS = 90
_EVENTS_FILENAME_DATE_RE = re.compile(r"^(?P<device>.+)-(?P<date>\d{4}-\d{2}-\d{2})\.jsonl$")
DEFAULT_RETROS_DIR = Path("~/.local/share/mind-meld/retros").expanduser()
"""Orphaned v0.12.0 trend-snapshot directory. Track 24B deleted the
snapshot subsystem; ``_gc_orphan_retros_dir`` reaps leftover files."""
_SNAPSHOT_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d+)\.json$")
"""v0.12.0 snapshot filename ``YYYY-MM-DD-NNN.json``. The reaper unlinks
only files matching this shape — never ``rm -rf`` the directory."""


@dataclass(frozen=True)
class ReapOutcome:
    """Result of one retention reaper, with selected work separate from I/O."""

    candidates: int = 0
    deleted: int = 0
    failed: int = 0
    skipped: int = 0
    repairs: int = 0
    repair_failed: int = 0

    @property
    def needs_remediation(self) -> bool:
        return bool(self.failed or self.skipped or self.repair_failed)


def _render_reap_outcome(label: str, outcome: ReapOutcome, *, dry_run: bool) -> None:
    """Print the one stable non-verbose result line for a reaper."""
    if dry_run:
        console.print(
            f"[bold]{label}[/bold] dry-run: candidates={outcome.candidates} "
            f"repairs={outcome.repairs} skipped={outcome.skipped}"
        )
        return

    console.print(
        f"[bold]{label}[/bold]: candidates={outcome.candidates} "
        f"deleted={outcome.deleted} failed={outcome.failed} "
        f"skipped={outcome.skipped} repairs={outcome.repairs} "
        f"repair-failed={outcome.repair_failed}"
    )
    if outcome.needs_remediation:
        console.print(
            "[yellow]Retention cleanup was incomplete. Fix permissions or locks, "
            "then rerun `mm gc`; use `-v` for paths and details.[/yellow]"
        )


def _gc_token_cache(dry_run: bool, verbose: bool) -> ReapOutcome:
    """Reap session-tokens.json entries with no living jsonl AND entries
    whose most recent by_day key is older than 90 days. Best-effort —
    cache reconstruction on the next push backstops a GC failure."""
    try:
        if dry_run:
            plan = token_usage.plan_cache_entries()
            outcome = ReapOutcome(
                candidates=len(plan.stale_keys),
                repairs=plan.repairs,
                skipped=int(plan.skipped_reason is not None),
            )
            if verbose:
                for cache_key in plan.stale_keys:
                    console.print(
                        f"  [dim]would reap token cache entry:[/dim] {safe_str(cache_key)}"
                    )
            if verbose and plan.skipped_reason is not None:
                console.print(
                    f"  [yellow]token cache skipped: {safe_str(plan.skipped_reason)}[/yellow]"
                )
        else:
            result = token_usage.reap_cache_entries()
            outcome = ReapOutcome(
                candidates=result.candidates,
                deleted=result.deleted,
                failed=result.failed,
                skipped=int(result.plan.skipped_reason is not None),
                repairs=result.repairs_applied,
                repair_failed=result.repairs_failed,
            )
            if verbose and result.write_error is None:
                for cache_key in result.plan.stale_keys:
                    console.print(f"  [dim]reaped token cache entry:[/dim] {safe_str(cache_key)}")
            elif verbose and result.write_error is not None:
                console.print(
                    f"  [yellow]token cache write failed: {safe_str(result.write_error)}[/yellow]"
                )
    except Exception as e:
        sys.stderr.write(f"mm: notice: token cache gc failed: {type(e).__name__}: {safe_str(e)}\n")
        outcome = ReapOutcome(skipped=1)
    _render_reap_outcome("Token cache", outcome, dry_run=dry_run)
    return outcome


def _sweep_local_tmp_files(
    backend: LocalBackend,
    my_device_id: str,
    dry_run: bool,
    verbose: bool,
    *,
    emit_summary: bool = True,
) -> ReapOutcome:
    """Reap stale tmp*.tmp left by crashed atomic_write_bytes calls.

    Scoped strictly to THIS device's subtrees:
        <root>/data/<my_device_id>/
        <root>/manifests/<my_device_id>/

    Peer subtrees are never touched — the iCloud storage tree is shared
    across machines but flock only serializes THIS Mac, so a file in
    another device's subtree might be in the middle of being uploaded
    by their iCloud daemon. Not our garbage to collect.

    NOTE: devices/ is intentionally EXCLUDED. It is a flat directory
    shared across machines (no per-device subdir), and tempfile.mkstemp
    names are random — there is no reliable way to tell this device's
    stranded tmp from a peer's in-flight write. The rare leak there is
    accepted; see Track 3A GC sweep for global orphan reaping.

    Returns selection and persistence counts. Dry-run makes no filesystem
    changes, including to the candidate files' metadata.
    """
    skipped = 0
    scoped_dirs = [
        backend.root / "data" / my_device_id,
        backend.root / "manifests" / my_device_id,
    ]
    victims: list[Path] = []
    for base in scoped_dirs:
        if not base.is_dir():
            continue
        try:
            for p in base.rglob("tmp*.tmp"):
                if p.is_file():
                    victims.append(p)
        except OSError as e:
            skipped += 1
            if verbose:
                console.print(
                    f"  [yellow]tmp scan skipped: {safe_str(base)} — {safe_str(e)}[/yellow]"
                )

    # devices/ deliberately excluded — see docstring.

    deleted = 0
    failed = 0
    for v in victims:
        if dry_run:
            if verbose:
                console.print(f"  [dim]would sweep:[/dim] {safe_str(v)}")
        else:
            try:
                v.unlink()
            except OSError as e:
                failed += 1
                if verbose:
                    console.print(f"  [yellow]sweep failed:[/yellow] {safe_str(v)} — {safe_str(e)}")
                continue
            deleted += 1

    outcome = ReapOutcome(candidates=len(victims), deleted=deleted, failed=failed, skipped=skipped)
    if emit_summary:
        _render_reap_outcome("Temporary files", outcome, dry_run=dry_run)
    return outcome


def _gc_old_event_files(
    config: dict,
    dry_run: bool,
    verbose: bool,
    *,
    now: datetime | None = None,
) -> ReapOutcome:
    """Reap mm-events JSONL files older than ``EVENTS_RETENTION_DAYS``.

    Track 7B fleet retention. The retro skill reads events by walking the
    synced manifest at retro time, so deletion via tombstone propagation
    is the fleet-wide retention mechanism: this device drops the file
    locally → next push generates a tombstone → all peers drop it on
    pull. An offline peer that comes back online sees the tombstone
    too, suppressing resurrection of the deleted day file.

    Reap by filename date (``<device>-YYYY-MM-DD.jsonl``), NOT mtime —
    iCloud restores can rewrite mtimes back to "now" while the filename
    date is intrinsic to the event-day boundary.

    Path resolution: from ``get_sources(config)`` so user-customized
    mm-events paths are honored. A missing source is a successful zero-count
    reaper; it still produces the standard result line.
    """
    sources = get_sources(config)
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        outcome = ReapOutcome()
        _render_reap_outcome("Events", outcome, dry_run=dry_run)
        return outcome
    events_dir = Path(mm_events_src["path"]).expanduser() / "events"
    if not events_dir.is_dir():
        outcome = ReapOutcome()
        _render_reap_outcome("Events", outcome, dry_run=dry_run)
        return outcome

    today = (now or datetime.now(timezone.utc)).date()
    candidates = 0
    deleted = 0
    failed = 0
    skipped = 0
    try:
        event_paths = events_dir.rglob("*-*.jsonl")
        for path in event_paths:
            m = _EVENTS_FILENAME_DATE_RE.match(path.name)
            if m is None:
                # Non-conforming filename in the events tree — leave alone.
                continue
            try:
                file_date = datetime.strptime(m.group("date"), "%Y-%m-%d").date()
            except ValueError:
                continue
            age_days = (today - file_date).days
            if age_days < EVENTS_RETENTION_DAYS:
                continue
            candidates += 1
            if dry_run:
                if verbose:
                    console.print(f"  [dim]would delete (age {age_days}d):[/dim] {safe_str(path)}")
                continue
            try:
                path.unlink()
            except OSError as e:
                failed += 1
                if verbose:
                    console.print(
                        f"  [yellow]delete failed:[/yellow] {safe_str(path)} — {safe_str(e)}"
                    )
                continue
            deleted += 1
            if verbose:
                console.print(f"  [dim]deleted (age {age_days}d):[/dim] {safe_str(path)}")
    except OSError as e:
        skipped += 1
        if verbose:
            console.print(
                f"  [yellow]events scan skipped: {safe_str(events_dir)} — {safe_str(e)}[/yellow]"
            )

    outcome = ReapOutcome(candidates=candidates, deleted=deleted, failed=failed, skipped=skipped)
    _render_reap_outcome("Events", outcome, dry_run=dry_run)
    return outcome


def _gc_old_conflict_files(
    config: dict,
    dry_run: bool,
    verbose: bool,
    *,
    now: datetime | None = None,
) -> ReapOutcome:
    """Delete .sync-conflict-* files older than CONFLICT_AGE_DAYS.

    Age is ``parse_conflict_created_at`` (the filename timestamp mm
    stamped at mint), NOT ``st_mtime``. The sidecar's mtime is the peer
    file's clock, restored by ``_apply_conflict``; reading it as the
    sidecar's own age reaps a day-0 copy of a 90-day-old peer file.

    Unparseable filename → do not reap (falling back to st_mtime is the
    bug). A live conflict (canonical exists and still differs) is never
    reaped, regardless of age. Paths print on delete / would-delete,
    not only under ``-v``.
    """
    try:
        hits = resolveflow._find_conflict_files(config)
    except OSError as e:
        outcome = ReapOutcome(skipped=1)
        if verbose:
            console.print(f"  [yellow]conflict scan skipped: {safe_str(e)}[/yellow]")
        _render_reap_outcome("Conflicts", outcome, dry_run=dry_run)
        return outcome
    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(days=CONFLICT_AGE_DAYS)
    candidates = 0
    deleted = 0
    failed = 0
    skipped = 0
    for _src_name, cpath, canonical in hits:
        created = parse_conflict_created_at(cpath.name)
        if created is None:
            skipped += 1
            if verbose:
                console.print(
                    f"  [yellow]conflict age unreadable, not reaping:[/yellow] {safe_str(cpath)}"
                )
            continue
        if is_pre_inversion_conflict_filename(cpath.name):
            # v0- sidecar holds the user's local bytes and may be the
            # only remaining copy. Canonical-missing is the promote-later
            # case, not an orphan.
            skipped += 1
            if verbose:
                console.print(f"  [dim]pre-inversion sidecar, not reaping:[/dim] {safe_str(cpath)}")
            continue
        if created >= cutoff:
            continue
        if _is_live_conflict(cpath, canonical):
            skipped += 1
            if verbose:
                console.print(f"  [dim]live conflict, not reaping:[/dim] {safe_str(cpath)}")
            continue
        candidates += 1
        age_days = (current_time - created).days
        if dry_run:
            console.print(f"  [dim]would delete (age {age_days}d):[/dim] {safe_str(cpath)}")
            continue
        try:
            cpath.unlink()
        except OSError as e:
            failed += 1
            console.print(f"  [yellow]delete failed:[/yellow] {safe_str(cpath)} — {safe_str(e)}")
            continue
        deleted += 1
        console.print(f"  [dim]deleted (age {age_days}d):[/dim] {safe_str(cpath)}")
    outcome = ReapOutcome(candidates=candidates, deleted=deleted, failed=failed, skipped=skipped)
    _render_reap_outcome("Conflicts", outcome, dry_run=dry_run)
    return outcome


def _is_live_conflict(cpath: Path, canonical: Path | None) -> bool:
    """True when canonical exists and still differs from the sidecar.

    Hash failure degrades to live (refuse to reap) — we cannot tell.
    Canonical-missing is not live: the sidecar is an orphan leftover.
    """
    if canonical is None:
        return False
    try:
        if not canonical.is_file():
            return False
        return hash_file(canonical) != hash_file(cpath)
    except OSError:
        return True


def _gc_orphan_retros_dir(
    dry_run: bool,
    verbose: bool,
    *,
    retros_dir: Path | None = None,
) -> ReapOutcome:
    """Reap leftover v0.12.0 trend-snapshot files.

    Track 24B deleted the snapshot subsystem. This reaper unlinks only
    files matching ``_SNAPSHOT_FILENAME_RE``, then ``rmdir`` the directory
    if empty. Never ``rm -rf``: a user file that does not match the
    snapshot regex is left alone, and a non-empty dir stays. Best-effort:
    every I/O failure is skipped, not raised. Dry-run selects the same
    candidates without unlinking or rmdir.
    """
    target = retros_dir if retros_dir is not None else DEFAULT_RETROS_DIR
    if not target.is_dir():
        outcome = ReapOutcome()
        _render_reap_outcome("Orphan retros", outcome, dry_run=dry_run)
        return outcome

    candidates = 0
    deleted = 0
    failed = 0
    skipped = 0
    repairs = 0
    repair_failed = 0
    try:
        files = list(target.iterdir())
    except OSError as e:
        outcome = ReapOutcome(skipped=1)
        if verbose:
            console.print(
                f"  [yellow]orphan retros scan skipped: {safe_str(target)} — {safe_str(e)}[/yellow]"
            )
        _render_reap_outcome("Orphan retros", outcome, dry_run=dry_run)
        return outcome

    matching: list[Path] = []
    leftovers = 0
    for path in files:
        try:
            if not path.is_file():
                leftovers += 1
                continue
        except OSError:
            skipped += 1
            continue
        if _SNAPSHOT_FILENAME_RE.match(path.name) is None:
            leftovers += 1
            continue
        matching.append(path)

    candidates = len(matching)
    for path in matching:
        if dry_run:
            if verbose:
                console.print(f"  [dim]would delete:[/dim] {safe_str(path)}")
            continue
        try:
            path.unlink()
        except OSError as e:
            failed += 1
            leftovers += 1
            if verbose:
                console.print(f"  [yellow]delete failed:[/yellow] {safe_str(path)} — {safe_str(e)}")
            continue
        deleted += 1
        if verbose:
            console.print(f"  [dim]deleted:[/dim] {safe_str(path)}")

    would_rmdir = leftovers == 0 and (dry_run or failed == 0)
    if would_rmdir and (matching or not files):
        if dry_run:
            repairs = 1
            if verbose:
                console.print(f"  [dim]would rmdir:[/dim] {safe_str(target)}")
        else:
            try:
                target.rmdir()
                repairs = 1
                if verbose:
                    console.print(f"  [dim]rmdir:[/dim] {safe_str(target)}")
            except OSError as e:
                repair_failed = 1
                if verbose:
                    console.print(
                        f"  [yellow]rmdir failed:[/yellow] {safe_str(target)} — {safe_str(e)}"
                    )

    outcome = ReapOutcome(
        candidates=candidates,
        deleted=deleted,
        failed=failed,
        skipped=skipped,
        repairs=repairs,
        repair_failed=repair_failed,
    )
    _render_reap_outcome("Orphan retros", outcome, dry_run=dry_run)
    return outcome


__all__ = [
    "CONFLICT_AGE_DAYS",
    "DEFAULT_RETROS_DIR",
    "EVENTS_RETENTION_DAYS",
    "ReapOutcome",
    "_SNAPSHOT_FILENAME_RE",
    "_gc_old_conflict_files",
    "_gc_old_event_files",
    "_gc_orphan_retros_dir",
    "_gc_token_cache",
    "_sweep_local_tmp_files",
]
