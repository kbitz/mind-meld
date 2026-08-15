"""Retention: every `mm gc` reaper, and the crashed-push tmp sweep.

Extracted from ``cli.py`` in Track 16A. Four reapers behind ``mm gc``:

* ``_gc_token_cache`` — session-token cache entries whose jsonl is gone, or
  whose newest day is over 90 days old
* ``_sweep_local_tmp_files`` — ``.tmp`` blobs left by a crashed push
* ``_gc_old_event_files`` — mm-events JSONLs past ``EVENTS_RETENTION_DAYS``
* ``_gc_old_conflict_files`` — ``.sync-conflict-*`` sidecars past
  ``CONFLICT_AGE_DAYS`` (only with ``--conflicts``)

Depends on ``resolveflow._find_conflict_files``, which is why this module lands
AFTER resolveflow in the Track 16A series and why ``retention -> resolveflow``
is the one intra-Track edge. Imports nothing from ``cli`` — pinned by
``tests/test_module_boundaries.py``.

``CONFLICT_AGE_DAYS`` is owned here but read back by ``cli``: the ``gc`` command
interpolates it into its ``--conflicts`` help string, which typer evaluates at
DECORATOR time. That is why a deferred/function-local import would not have
worked as a cycle workaround.

Track 17B owns the honesty fixes for these reapers (the ``--dry-run`` count that
always reports 0, ``_gc_token_cache`` ignoring ``--dry-run``); do NOT fix them
here — this Track is movement.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mind_meld import resolveflow, token_usage
from mind_meld.config import get_sources
from mind_meld.consoles import console
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


def _gc_token_cache(dry_run: bool, verbose: bool) -> None:
    """Reap session-tokens.json entries with no living jsonl AND entries
    whose most recent by_day key is older than 90 days. Best-effort —
    cache reconstruction on the next push backstops a GC failure."""
    if dry_run:
        # Dry-run: count without mutating. Re-implement the predicate
        # cheaply via is_cache_cold + a peek.
        if not token_usage.CACHE_PATH.exists():
            if verbose:
                console.print("[dim]No token cache to gc.[/dim]")
            return
        if verbose:
            console.print("[dim]Token cache reaper: dry-run; skipping.[/dim]")
        return
    try:
        n = token_usage.gc_cache_entries()
    except Exception as e:
        sys.stderr.write(f"mm: notice: token cache gc failed: {type(e).__name__}: {safe_str(e)}\n")
        return
    if verbose and n:
        console.print(f"[dim]Reaped {n} stale token cache entr{'y' if n == 1 else 'ies'}.[/dim]")


def _sweep_local_tmp_files(
    backend: LocalBackend,
    my_device_id: str,
    dry_run: bool,
    verbose: bool,
) -> int:
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

    Returns the count swept (or would-be-swept if dry_run).
    """
    count = 0
    scoped_dirs = [
        backend.root / "data" / my_device_id,
        backend.root / "manifests" / my_device_id,
    ]
    victims: list[Path] = []
    for base in scoped_dirs:
        if not base.exists():
            continue
        for p in base.rglob("tmp*.tmp"):
            if p.is_file():
                victims.append(p)

    # devices/ deliberately excluded — see docstring.

    for v in victims:
        if dry_run:
            if verbose:
                console.print(f"  [dim]would sweep: {v}[/dim]")
        else:
            try:
                v.unlink()
            except OSError as e:
                if verbose:
                    console.print(f"  [yellow]sweep failed: {v} — {e}[/yellow]")
                continue
        count += 1

    if count > 0 and not dry_run:
        console.print(f"  [dim]swept {count} stale tmp files[/dim]")
    elif count > 0 and dry_run:
        console.print(f"  [dim]would sweep {count} stale tmp files[/dim]")
    return count


def _gc_old_event_files(config: dict, dry_run: bool, verbose: bool) -> int:
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
    mm-events paths are honored. Returns 0 when no mm-events source is
    enabled / resolved.
    """
    sources = get_sources(config)
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return 0
    events_dir = Path(mm_events_src["path"]).expanduser() / "events"
    if not events_dir.is_dir():
        return 0

    today = datetime.now(timezone.utc).date()
    reaped = 0
    for path in events_dir.rglob("*-*.jsonl"):
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
        if verbose or dry_run:
            prefix = "would delete" if dry_run else "deleted"
            console.print(f"  [dim]{prefix} (age {age_days}d):[/dim] {safe_str(path)}")
        if not dry_run:
            try:
                path.unlink()
                reaped += 1
            except OSError:
                pass
        else:
            reaped += 1
    label = "would reap" if dry_run else "reaped"
    console.print(
        f"[bold]{label}[/bold] {reaped} stale events files "
        f"(older than {EVENTS_RETENTION_DAYS} days)"
    )
    return reaped


def _gc_old_conflict_files(config: dict, dry_run: bool, verbose: bool) -> int:
    """Delete .sync-conflict-* files older than CONFLICT_AGE_DAYS. Returns count."""
    hits = resolveflow._find_conflict_files(config)
    cutoff = datetime.now(timezone.utc) - timedelta(days=CONFLICT_AGE_DAYS)
    reaped = 0
    for src_name, cpath, _canonical in hits:
        try:
            mtime = datetime.fromtimestamp(cpath.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            if verbose or dry_run:
                age_days = (datetime.now(timezone.utc) - mtime).days
                prefix = "would delete" if dry_run else "deleted"
                console.print(f"  [dim]{prefix} (age {age_days}d):[/dim] {safe_str(cpath)}")
            if not dry_run:
                try:
                    cpath.unlink()
                    reaped += 1
                except OSError:
                    pass
    label = "would reap" if dry_run else "reaped"
    console.print(
        f"[bold]{label}[/bold] {reaped} stale conflict files (older than {CONFLICT_AGE_DAYS} days)"
    )
    return reaped


__all__ = [
    "CONFLICT_AGE_DAYS",
    "EVENTS_RETENTION_DAYS",
    "_gc_old_conflict_files",
    "_gc_old_event_files",
    "_gc_token_cache",
    "_sweep_local_tmp_files",
]
