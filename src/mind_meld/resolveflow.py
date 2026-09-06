"""Conflict discovery, promotion, and the interactive `mm resolve` walk.

Extracted from ``cli.py`` in Track 16A.

Two responsibilities that share the sidecar-filename vocabulary:

* **discovery** — ``_find_conflict_files`` walks each source's synced dirs for
  ``.sync-conflict-*`` sidecars, migrating pre-v0.9.2 files to the ``v0-``
  prefix under the mm lock as it goes.
* **resolution** — ``_resolve_interactive_loop`` prompts per conflict and
  applies the chosen action.

The ``@app.command()`` shells (``conflicts``, ``resolve``, ``recover``) stay in
``cli.py``; this module owns no typer registration.

Imports nothing from ``cli`` — pinned by ``tests/test_module_boundaries.py``.
The two mtime primitives it needs (``_stat_mtime_btime`` and
``_bump_canonical_mtime_post_resolve``) live in the ``conflictmtime`` leaf
precisely because ``cli``'s pull/apply path calls them too; the Rich consoles
come from the ``consoles`` leaf for the same reason.

**Read ``docs/invariants/conflicts.md`` BEFORE editing.** The dual-mode dispatch
by filename prefix (``v0-`` = pre-inversion semantics, no prefix =
post-inversion) is load-bearing and silently loses user data if inverted, and
the post-(l)ocal canonical mtime bump is what stops a resolve -> pull -> resolve
loop across the fleet.
"""

from __future__ import annotations

import difflib
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import typer

from mind_meld import fsutil, manifest, sidecar
from mind_meld.config import get_sources
from mind_meld.conflictdiff import (
    count_divergent_lines,
    format_age_delta,
    merge_has_line_structure,
    newer_side,
    render_banner,
    render_capped_diff,
    render_prompt,
    render_time_line,
    render_verdict,
)
from mind_meld.conflictmtime import _bump_canonical_mtime_post_resolve, _stat_mtime_btime
from mind_meld.consoles import console, stderr_console
from mind_meld.devices import lookup_device_by_short_id
from mind_meld.errors import StorageError
from mind_meld.manifest import (
    CONFLICT_INFIX,
    CONFLICT_V0_PREFIX,
    GROK_SYNCED_SUBDIRS,
    SYNCED_SUBDIRS,
    is_conflict_filename,
    is_pre_inversion_conflict_filename,
    is_v1_conflict_filename,
    parse_conflict_created_at,
    parse_conflict_device_short,
)
from mind_meld.merge import lcs_merge
from mind_meld.safety import safe_str

# v0.9.2 inversion ship date (CHANGELOG). Unprefixed sidecars whose
# filename timestamp is on or after this are post-inversion mints.
_INVERSION_SHIPPED_AT = datetime(2026, 4, 25, tzinfo=timezone.utc)

_LEGACY_SKIP_ALIAS_NOTICE = (
    "mm: notice: 'b' / 'both' now means 'skip'; use 's' going forward (alias removed at 1.0)."
)


def _normalize_legacy_skip_choice_and_warn(choice: str) -> str:
    """Map only the retired ``b`` / ``both`` aliases to skip and warn.

    This is deliberately a side-effecting compatibility helper: both prompt
    sites must preserve the exact stderr notice while one authority owns the
    exact-match rule. Callers normalize casing and whitespace before calling.
    All other input, including ``back`` / ``browse`` / ``between`` and the
    resolver-only legacy ``c`` / ``f`` policy, stays untouched.
    """
    if choice in ("b", "both"):
        print(_LEGACY_SKIP_ALIAS_NOTICE, file=sys.stderr)
        return "s"
    return choice


def _synced_scan_dirs(src_cfg: dict, base_path: Path) -> list[Path]:
    """Return the directories `mm push` would walk for this source.

    Limits conflict discovery to paths mm actually syncs so we don't
    list .sync-conflict-* files from unsynced areas (e.g., ~/.claude/sessions
    when the claude source only syncs memory/ and todos/).

    - claude type: projects/<any>/memory, projects/<any>/todos
    - grok type: skills, commands, rules at the source root
    - generic type: include_dirs (relative to source root)
    """
    src_type = src_cfg.get("type", "claude")
    if src_type == "claude":
        projects = base_path / "projects"
        if not projects.exists():
            return []
        dirs: list[Path] = []
        for project_dir in projects.iterdir():
            if not project_dir.is_dir():
                continue
            for sub in SYNCED_SUBDIRS:
                candidate = project_dir / sub
                if candidate.exists():
                    dirs.append(candidate)
        return dirs
    if src_type == "grok":
        dirs = []
        for sub in GROK_SYNCED_SUBDIRS:
            candidate = base_path / sub
            if candidate.is_dir() and not candidate.is_symlink():
                dirs.append(candidate)
        return dirs
    # generic: include_dirs (resolved) + base for single-file includes
    dirs = []
    for d in src_cfg.get("include_dirs", []):
        candidate = base_path / d
        if candidate.exists():
            dirs.append(candidate)
    return dirs


def _inversion_marker_path() -> Path:
    """Canonical path for the one-shot inversion-install timestamp file."""
    return sidecar.SIDECAR_DIR / "inversion-installed-at"


def _ensure_inversion_marker() -> float | None:
    """Get-or-create the inversion-install timestamp (epoch seconds).

    Returns the timestamp as a float, or None on any read/parse/write
    failure (fail-safe: the caller treats None as "skip migration").

    Critical safety property: distinguishes pre-inversion conflict files
    from post-inversion ones. The gate (in `_migrate_pre_inversion_conflict`)
    reads the filename era marker / filename timestamp, NOT ``st_mtime``.
    ``_apply_conflict`` restores the sidecar's mtime to the *peer file's*
    clock, so a fresh sidecar is essentially always in the past; using
    that clock as "sidecar age" mis-tags post-inversion files as `v0-`
    and resolve's `(l)ocal` then overwrites local with peer bytes.

    First-call semantics: writes the marker at "now". Combined with the
    filename-clock gate: this sweep runs at the TOP of the pull, before
    any `_apply_conflict`, so every sidecar this device mints has a
    filename timestamp ``>= marker_ts`` by construction. The `v1` era
    token is belt-and-braces for a deleted or restored marker file.

    Best-effort: directory creation and file write may fail (perms,
    disk full). Failure returns None — the caller MUST treat None as
    "do not migrate" rather than "migrate everything", so a broken
    marker degrades to safe-default-no-migration instead of mass
    re-tagging.
    """
    path = _inversion_marker_path()
    try:
        if path.exists():
            return float(path.read_text().strip())
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        marker_ts = time.time()
        # Atomic write so a crash mid-create doesn't leave a partial
        # number that fails to float-parse on the next read.
        fsutil.atomic_write_bytes(
            path,
            f"{marker_ts}\n".encode(),
            fsync=True,
            mode=0o600,
        )
        return marker_ts
    except (OSError, ValueError, StorageError):
        return None


def _migrate_pre_inversion_conflict(path: Path) -> Path:
    """Rename a pre-inversion conflict file to carry the `v0-` prefix.

    Idempotent: a path already prefixed with `v0-` returns unchanged.
    `v1` era files (minted by v0.14.0+) are definitively post-inversion
    and return immediately. Anything else is gated on
    ``parse_conflict_created_at`` against the inversion-install marker:
    unparseable → refuse to migrate (mirrors ``marker_ts is None``).
    Do NOT fall back to ``st_mtime`` — that IS the clock-conflation bug
    (sidecar mtime is the peer's clock, restored by `_apply_conflict`).

    Uses ``rindex`` (not ``find``) so a double-infix name — mm mints
    this itself when a documented non-conflict canonical like
    ``notes.sync-conflict-log.md`` conflicts — inserts ``v0-`` before
    the LAST infix. ``find`` accretes ``v0-`` forever, one rename per
    pull, because the prefix lands before the inner segment and
    ``is_pre_inversion_conflict_filename`` never latches.

    MUST only be called from a lock-protected context (mm pull, mm
    resolve). `mm conflicts` is intentionally read-only and lockless;
    renaming there would race with autopull's own discovery walk
    (codex-2 #5).
    """
    name = path.name
    if is_pre_inversion_conflict_filename(name):
        return path
    if not is_conflict_filename(name):
        return path
    # v1 era token is definitive post-inversion, independent of the
    # marker file (which can be deleted or restored).
    if is_v1_conflict_filename(name):
        return path

    marker_ts = _ensure_inversion_marker()
    if marker_ts is None:
        # Fail-safe: marker unreadable / unwriteable. Refuse to migrate
        # rather than risk mis-tagging.
        return path
    created = parse_conflict_created_at(name)
    if created is None:
        # Unparseable filename. Refuse — falling back to st_mtime is
        # the clock-conflation bug this helper exists to close.
        return path
    # v0.9.2 (2026-04-25) inverted conflict direction. Unprefixed
    # sidecars minted on or after that date are post-inversion even
    # if a deleted marker is recreated as "now". Without this floor,
    # restoring ~/.claude onto a fresh mm init v0-tags the whole
    # 0.9.2–0.13 fleet and resolve `(l)ocal` overwrites local with peer.
    if created >= _INVERSION_SHIPPED_AT:
        return path
    if created.timestamp() >= marker_ts:
        # Filename birth is at-or-after this install's first
        # post-inversion pull. Leave unprefixed.
        return path

    try:
        idx = name.rindex(CONFLICT_INFIX)
    except ValueError:
        return path  # defensive — is_conflict_filename guarantees presence
    before = name[: idx + len(CONFLICT_INFIX)]
    after = name[idx + len(CONFLICT_INFIX) :]
    new_name = f"{before}{CONFLICT_V0_PREFIX}{after}"
    new_path = path.with_name(new_name)
    if new_path.exists():
        return path  # collision — leave both copies in place for resolve
    try:
        path.rename(new_path)
    except OSError as e:
        # safe_str both: the sidecar filename derives from a peer-supplied
        # manifest rel_path stem, and manifest._validate_rel_path rejects only
        # NUL / absolute / ".." / drive-letter — ESC passes through. Without
        # this, a peer holding the storage passphrase can land an OSC-52 payload
        # in a filename that reaches the terminal unstripped. Every other print
        # in this module was already sanitized; this was the one hole, carried
        # over from cli.py.
        stderr_console.print(
            f"[yellow]warning:[/yellow] failed to migrate pre-inversion conflict "
            f"file {safe_str(path)} — {safe_str(e)}"
        )
        return path
    return new_path


def _find_conflict_files(
    config: dict,
    *,
    migrate_pre_inversion: bool = False,
) -> list[tuple[str, Path, Path | None]]:
    """Walk all sync sources looking for .sync-conflict-* files.

    Scoped to the same paths mm push walks — won't surface conflict files
    from unsynced areas of the source tree. Returns (source_name,
    conflict_path, canonical_path_if_exists). Canonical is None if the user
    has already deleted it.

    Two scan strategies, since `mm push` walks two surfaces per source:
      1. Recursive scan inside include_dirs (and claude SYNCED_SUBDIRS).
      2. Depth-0 sibling-glob for generic include_files entries — top-level
         single-file syncs whose conflict siblings live next to them, not
         inside `_synced_scan_dirs`' recursive surface. Without (2), conflict
         files for top-level entries like ~/.gstack/retro-context.md are invisible
         to `mm conflicts` / `mm resolve` / `mm gc --conflicts` (the
         2026-04-24 first-pull bug — listed 5 of 6 conflicts).

    `migrate_pre_inversion` (default False): if True, rename any
    pre-inversion conflict files to carry the `v0-` prefix before
    returning. Lock-protected callers ONLY (mm pull, mm resolve).
    Pass False from `mm conflicts` (read-only; lockless — would race
    autopull) and from `_gc_old_conflict_files` (filename-clock reaping
    doesn't need the prefix discrimination, codex-2 #5).

    Surface asymmetry (load-bearing, currently surprising): this walk
    does NOT consult `exclude_patterns`. Discovery, gc, resolve, and the
    pull-top migration sweep all reach trees that sync does not. A
    sidecar stranded in an excluded tree can never converge — `(l)`/`(r)`
    operate on a file that will never sync again.

    Dedup: scan strategies (1) and (2) overlap when an `include_files`
    entry sits inside an `include_dirs` directory (e.g. user customizes
    `include_files: ["projects/notes.md"]` AND `include_dirs:
    ["projects"]`). Without dedup, `mm conflicts` shows duplicate rows
    and `mm gc --conflicts` double-counts reaped files. Key is
    `(src_name, conflict_path)` not bare `Path`: two configured sources
    could legitimately reference overlapping subtrees, and dedup must
    preserve source attribution.
    """
    hits: list[tuple[str, Path, Path | None]] = []
    # Group 7 preflight #3 + D6: dedup key uses filesystem identity
    # (src_name, st_dev, st_ino) when stat succeeds — handles APFS
    # case-mismatched config (e.g. include_dirs ["projects"] +
    # include_files ["Projects/notes.md"]) correctly. Falls back to
    # (src_name, str(path)) when stat fails (race window between glob
    # and dedup) so we never silently drop a conflict file just because
    # of a transient stat error. The src_name component preserves source
    # attribution when two configured sources legitimately reference
    # overlapping subtrees.
    seen: set[tuple[str, int, int] | tuple[str, str]] = set()

    def _maybe_migrate(p: Path) -> Path:
        if migrate_pre_inversion:
            return _migrate_pre_inversion_conflict(p)
        return p

    def _identity_key(src_name: str, conflict_path: Path) -> tuple[str, int, int] | tuple[str, str]:
        try:
            st = conflict_path.stat()
        except OSError:
            return (src_name, str(conflict_path))
        return (src_name, st.st_dev, st.st_ino)

    def _try_add(src_name: str, conflict_path: Path, canonical: Path | None) -> None:
        key = _identity_key(src_name, conflict_path)
        if key in seen:
            return
        seen.add(key)
        hits.append((src_name, conflict_path, canonical))

    for src_cfg in get_sources(config):
        base_path = Path(src_cfg["path"]).expanduser().resolve()
        if not base_path.exists():
            continue

        # (1) Recursive scan in include_dirs / SYNCED_SUBDIRS.
        for scan_dir in _synced_scan_dirs(src_cfg, base_path):
            # rglob is loose (substring); filter strictly via is_conflict_filename
            # so user files like notes.sync-conflict-log.md are not listed/reaped.
            for conflict_path in scan_dir.rglob(f"*{CONFLICT_INFIX}*"):
                if not is_conflict_filename(conflict_path.name):
                    continue
                try:
                    is_regular = conflict_path.is_file()
                except OSError as e:
                    print(
                        "mm: warning: conflict sidecar unreadable (left in place): "
                        f"{safe_str(conflict_path)} \u2014 {safe_str(e)}",
                        file=sys.stderr,
                    )
                    continue
                if not is_regular:
                    continue
                conflict_path = _maybe_migrate(conflict_path)
                canonical = manifest._canonical_for_conflict(conflict_path)
                _try_add(
                    src_cfg["name"],
                    conflict_path,
                    canonical if canonical is not None and canonical.exists() else None,
                )

        # (2) Depth-0 sibling-glob for include_files entries. Gate on data
        # presence (not source type) so a future schema that adds
        # include_files to other source types doesn't silently lose
        # conflict visibility — the same scope-mismatch class of bug as
        # the original Track 5A Task 2.
        if src_cfg.get("include_files"):
            for filename in src_cfg.get("include_files", []):
                canonical = base_path / filename
                # parent_dir handles both top-level entries (parent == base_path)
                # and nested entries like "subdir/file.txt" (parent == base/subdir).
                # .glob() is depth-0 — never recurses into unsynced subtrees.
                parent_dir = canonical.parent
                if not parent_dir.exists():
                    continue
                # Fixed glob: stem-prefix patterns treat literal metacharacters
                # in the canonical name as glob syntax and also match a
                # different file that shares a prefix (notes.md vs
                # notes.sync-conflict-log.md). Ownership is exact Path equality
                # via the shared parser, checked before stat/migration.
                for conflict_path in parent_dir.glob(f"*{CONFLICT_INFIX}*"):
                    if not is_conflict_filename(conflict_path.name):
                        continue
                    if manifest._canonical_for_conflict(conflict_path) != canonical:
                        continue
                    try:
                        is_regular = conflict_path.is_file()
                    except OSError as e:
                        print(
                            "mm: warning: conflict sidecar unreadable (left in place): "
                            f"{safe_str(conflict_path)} \u2014 {safe_str(e)}",
                            file=sys.stderr,
                        )
                        continue
                    if not is_regular:
                        continue
                    conflict_path = _maybe_migrate(conflict_path)
                    _try_add(
                        src_cfg["name"],
                        conflict_path,
                        canonical if canonical.exists() else None,
                    )
    return hits


def _promote_target_path(
    canonical: Path,
    is_pre_inversion: bool,
    peer_short: str | None,
    now: datetime | None = None,
) -> Path:
    """Compute the collision-free target filename for promoting a conflict sidecar.

    Per-mode naming -- the sidecar's bytes mean different things by inversion era:
      * post-inversion sidecar HOLDS the peer's bytes -> ``<stem>.from-<peer>-<ts>.<ext>``
      * pre-inversion (``v0-``) sidecar HOLDS the user's own LOCAL bytes ->
        ``<stem>.local-<ts>.<ext>`` (naming it ``from-<peer>`` would lie about
        provenance).

    ``<ts>`` is not collision-proof at same-second granularity; if the computed
    path already exists, append a 4-hex random suffix (same pattern as
    ``conflict_filename``). This is a best-effort pre-check -- ``os.link`` in
    ``_promote_conflict_file`` is the actual atomic no-clobber guarantee.
    """
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    stem = canonical.stem
    suffix = canonical.suffix
    if is_pre_inversion:
        base_name = f"{stem}.local-{ts}"
    else:
        base_name = f"{stem}.from-{peer_short or 'unknown'}-{ts}"
    target = canonical.with_name(f"{base_name}{suffix}")
    if target.exists():
        target = canonical.with_name(f"{base_name}-{secrets.token_hex(2)}{suffix}")
    return target


def _promote_conflict_file(cpath: Path, target: Path) -> Path:
    """Rename a conflict sidecar to ``target`` with a no-clobber guarantee.

    ``os.link`` raises ``FileExistsError`` atomically if ``target`` exists --
    closing the TOCTOU window a plain ``Path.rename()`` (which silently
    replaces the target on POSIX) would leave open. The promoted file is a
    first-class user filename, so silent clobber is real data loss. On the
    rare race, retry once with a fresh 4-hex suffix. Returns the actual path
    written. Raises ``OSError`` on any other failure (caller counts it as
    ``failed``).
    """
    try:
        os.link(cpath, target)
    except FileExistsError:
        target = target.with_name(f"{target.stem}-{secrets.token_hex(2)}{target.suffix}")
        os.link(cpath, target)
    os.unlink(cpath)
    return target


def _promote_target_will_sync(src_cfg: dict, target: Path) -> bool:
    """True if ``target`` falls within the source's recursively-synced surface.

    A promoted file is a NEW filename, so it can only sync if it lives inside
    one of the source's scanned directories (``_synced_scan_dirs``). An
    ``include_files`` source matches only exact configured filenames -- a
    promoted ``<stem>.from-...`` / ``<stem>.local-...`` name will never match
    one, so promote under an ``include_files``-only source produces a file
    that will not sync until the user adds it to config.
    """
    base_path = Path(src_cfg["path"]).expanduser().resolve()
    try:
        resolved_target = target.resolve()
    except OSError:
        return False
    for scan_dir in _synced_scan_dirs(src_cfg, base_path):
        try:
            resolved_target.relative_to(scan_dir.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _resolve_interactive_loop(
    hits: list[tuple[str, Path, Path | None]],
    devices: list[dict[str, Any]] | None = None,
    sources_by_name: dict[str, dict] | None = None,
) -> tuple[int, int]:
    """Walk each conflict and prompt for resolution. Extracted so `resolve`
    stays a thin wrapper around acquire/release lock boilerplate.

    ``devices`` is the cached device list from ``list_devices(backend)``,
    used by the REMOTE banner to attribute conflict bytes to a peer name.
    None disables attribution -- legacy callers and unit tests can pass
    ``None`` (or omit the arg) and get an "(unknown peer)" annotation.
    Cache hoisted at the loop entry so a multi-conflict walk doesn't N+1
    on iCloud cold-cache reads.

    ``sources_by_name`` maps source name -> source config, used by the
    ``(p)romote`` branch to warn when a promoted file would land outside the
    source's sync surface (an ``include_files`` source). None disables the
    warning -- unit tests that don't exercise promote can omit it.

    Returns (resolved, failed). `failed` covers per-conflict OSErrors
    (rename/unlink/read) that left the conflict file in place. `resolve`
    uses the failure count to decide its exit code; the walk itself does
    not abort on per-file errors (so the user gets to triage every conflict
    in one pass).
    """

    devices = devices or []
    sources_by_name = sources_by_name or {}
    resolved = 0
    failed = 0
    for src_name, cpath, canonical in hits:
        console.print(
            f"\n[bold yellow]Conflict in {safe_str(src_name)}:[/bold yellow] {safe_str(cpath)}"
        )

        if canonical is None:
            # Dual-mode preface by filename prefix. Pre-inversion (`v0-`)
            # files were produced when sidecar = local bytes; post-inversion
            # files have sidecar = remote bytes. The promote/delete ops are
            # the same; only the preface wording flips.
            if is_pre_inversion_conflict_filename(cpath.name):
                console.print(
                    "  [dim]No canonical file exists. This pre-v0.9.2 "
                    "conflict file holds your LOCAL edits from before "
                    "the conflict was created.[/dim]"
                )
            else:
                console.print(
                    "  [dim]No canonical file exists. This conflict file "
                    "holds REMOTE bytes from another machine.[/dim]"
                )
            console.print(
                "  [dim]Promote it to make it the canonical file, "
                "delete it to discard, or skip to leave it for later.[/dim]"
            )
            choice = (
                typer.prompt(
                    "  (p)romote / (d)elete / (s)kip",
                    default="s",
                    show_default=False,
                )
                .strip()
                .lower()
            )
            # Exact-match dispatch (not startswith): "post"/"plan"/"description"
            # must not silently promote/delete. (codex /review v0.9.0)
            if choice in ("p", "promote"):
                target_canonical = manifest._canonical_for_conflict(cpath)
                if target_canonical is None:
                    console.print(
                        "  [red]promote failed:[/red] cannot reconstruct a "
                        "canonical name for this sidecar"
                    )
                    failed += 1
                else:
                    try:
                        cpath.rename(target_canonical)
                        console.print(
                            f"  [green]promoted[/green] "
                            f"{safe_str(cpath.name)} -> {safe_str(target_canonical.name)}"
                        )
                        resolved += 1
                    except OSError as e:
                        console.print(f"  [red]promote failed:[/red] {safe_str(e)}")
                        failed += 1
            elif choice in ("d", "delete"):
                try:
                    cpath.unlink()
                    console.print(f"  [red]deleted[/red] {safe_str(cpath.name)}")
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]delete failed:[/red] {safe_str(e)}")
                    failed += 1
            # else: skip (default)
            continue

        # Dual-mode dispatch by filename prefix. `v0-` = pre-inversion
        # (sidecar HOLDS local bytes; canonical holds remote). No prefix =
        # post-inversion (canonical IS local; sidecar holds remote bytes).
        # Picking by prefix (not timestamp) is sound: post-inversion files
        # are produced by code that NEVER stamps the v0- prefix, and
        # pre-inversion files are migrated to the prefix at discovery time
        # by `_migrate_pre_inversion_conflict`. Mixed prefixes in one walk
        # are expected during migration.
        is_pre_inversion = is_pre_inversion_conflict_filename(cpath.name)
        mode: Literal["pre_inversion", "post_inversion"] = (
            "pre_inversion" if is_pre_inversion else "post_inversion"
        )

        try:
            canonical_bytes = canonical.read_bytes()
            cpath_bytes = cpath.read_bytes()
        except OSError as e:
            console.print(f"  [red]read failed:[/red] {safe_str(e)}")
            failed += 1
            continue

        canonical_text = canonical_bytes.decode("utf-8", errors="replace").splitlines()
        cpath_text = cpath_bytes.decode("utf-8", errors="replace").splitlines()

        # Try LCS-as-synthetic-base 3-way merge so the (m)erge prompt option
        # can offer a clean union of additive edits. lcs_merge respects the
        # inversion-mode argument order so the embedded `<<<<<<< local` /
        # `>>>>>>> remote` markers stay accurate even on v0- files. Binary
        # input (NUL byte) returns conflict_count = -1 -- suppress (m).
        if is_pre_inversion:
            merged_bytes, merge_conflicts = lcs_merge(cpath_bytes, canonical_bytes)
        else:
            merged_bytes, merge_conflicts = lcs_merge(canonical_bytes, cpath_bytes)
        # A single-line side gives lcs_merge nothing to align on, so (m) could
        # only ever emit one marker region wrapping both versions whole.
        # Suppress it through the same gate as binary content. Order-independent
        # predicate, so the inversion-mode argument swap above doesn't matter.
        merge_available = merge_conflicts >= 0 and merge_has_line_structure(
            canonical_text, cpath_text
        )

        # Banner attribution: pull the device-short out of the conflict
        # filename and look it up against the cached devices list.
        short = parse_conflict_device_short(cpath.name)
        peer_name: str | None = None
        ambiguous_count = 0
        if short is not None:
            match, count = lookup_device_by_short_id(devices, short)
            if match is not None:
                peer_name = match.get("device_name")
            elif count > 1:
                ambiguous_count = count

        # Diff label semantics:
        #   pre_inversion: canonical = remote, cpath = local.
        #   post_inversion: canonical = local, cpath = remote.
        if is_pre_inversion:
            from_text, to_text = canonical_text, cpath_text
            from_label = f"remote ({safe_str(canonical.name)})"
            to_label = f"local  ({safe_str(cpath.name)})"
            local_path_for_banner = cpath
            remote_path_for_banner = canonical
        else:
            from_text, to_text = canonical_text, cpath_text
            from_label = f"local  ({safe_str(canonical.name)})"
            to_label = f"remote ({safe_str(cpath.name)})"
            local_path_for_banner = canonical
            remote_path_for_banner = cpath

        # Color banners ABOVE the diff so the user can scan-identify which
        # side is which without parsing diff prefixes. Both peer-controlled
        # paths AND the peer-controlled device_name flow into render_banner,
        # which strips terminal escapes via safe_text before they reach the
        # terminal (closes the same trust boundary safe_str closes for
        # filenames).
        # Timestamp display. Both files are on disk here, so stat both. The
        # local side shows genuine created+modified; the remote sidecar shows
        # modified (the peer's restored source mtime) + "pulled" (the local
        # iCloud-drop birthtime -- NOT the peer's real creation, so it is
        # never labeled "created"). local_*/remote_* track the LOCAL vs REMOTE
        # sides consistently regardless of inversion mode, so the verdict and
        # the (n)ewer shortcut stay correct for v0- files too.
        local_mtime_ts, local_btime_ts = _stat_mtime_btime(local_path_for_banner)
        remote_mtime_ts, remote_btime_ts = _stat_mtime_btime(remote_path_for_banner)

        console.print(render_banner("local", local_path_for_banner.name, None))
        console.print(render_time_line([("modified", local_mtime_ts), ("created", local_btime_ts)]))
        console.print(
            render_banner(
                "remote",
                remote_path_for_banner.name,
                peer_name,
                ambiguous_count=ambiguous_count,
            )
        )
        console.print(
            render_time_line([("modified", remote_mtime_ts), ("pulled", remote_btime_ts)])
        )
        _verdict = render_verdict(local_mtime_ts, remote_mtime_ts)
        if _verdict is not None:
            console.print(_verdict)

        diff = list(
            difflib.unified_diff(
                from_text,
                to_text,
                fromfile=from_label,
                tofile=to_label,
                lineterm="",
                n=3,
            )
        )

        # Three-number divergence summary BEFORE the diff so the user
        # gets a glance at scale. count_divergent_lines returns counts
        # keyed to the diff's from/to sides, which differ across modes:
        # in pre-inversion the diff is remote->local, so m = remote-only
        # and n = local-only. Map to semantic local/remote counts before
        # rendering so the summary copy stays honest in both modes AND
        # the prompt's (drops N ...) annotations are mode-correct.
        # Replacements count as one of each (a 1-line change is "1 of
        # yours + 1 from peer", K=2) -- the wording is honest about that.
        m, n, k = count_divergent_lines(diff)
        if is_pre_inversion:
            local_only, remote_only = n, m
        else:
            local_only, remote_only = m, n
        if k:
            console.print(
                f"  [dim]{local_only} unique line"
                f"{'' if local_only == 1 else 's'} of yours; "
                f"{remote_only} unique line"
                f"{'' if remote_only == 1 else 's'} from peer; "
                f"{k} total diff lines.[/dim]"
            )

        # Rendering is shared so both consent surfaces keep the same terminal
        # safety and color contract. The cap remains resolve-specific: this
        # post-pull walk has historically shown 80 raw unified-diff entries.
        for renderable in render_capped_diff(diff, cap=80):
            console.print(renderable)

        # Concrete-action prompt copy. Filenames pre-sanitized via safe_str
        # since render_prompt does plain f-string interpolation. (m)erge
        # is offered when the LCS attempt succeeded (binary content sets
        # merge_available=False). (s)kip remains the default key even for a
        # clean merge, so the user must explicitly choose (m)erge.
        # Pass semantic local/remote line counts so render_prompt can
        # annotate (l)ocal / (r)emote with the consequential drop count.
        # Suppress the counts on empty-diff (binary) so the annotation
        # doesn't claim "drops 0 lines" when we couldn't actually compare.
        prompt_local_only: int | None = local_only if diff else None
        prompt_remote_only: int | None = remote_only if diff else None
        # (n)ewer is offered when BOTH mtimes are readable (incl. a tie --
        # pressing it on a tie re-prompts, see the input loop below). It maps
        # to the existing (l)/(r) dispatch, so the per-mode keep-local /
        # keep-remote semantics (and the mtime bump) come for free. nside is
        # in LOCAL/REMOTE terms (we stat'd the banner paths), correct for
        # both inversion modes.
        nside = newer_side(local_mtime_ts, remote_mtime_ts)
        newer_available = nside != "unknown"
        if nside in ("local", "remote"):
            _delta = format_age_delta((local_mtime_ts or 0.0) - (remote_mtime_ts or 0.0))
            newer_desc = f"{'LOCAL' if nside == 'local' else 'REMOTE'}, {_delta} newer"
        else:
            newer_desc = ""  # tie: shown without a winner annotation
        console.print(
            render_prompt(
                safe_str(canonical.name),
                safe_str(cpath.name),
                mode,
                merge_available=merge_available,
                merge_conflicts=max(merge_conflicts, 0),
                promote_available=True,
                newer_available=newer_available,
                newer_desc=newer_desc,
                local_only_lines=prompt_local_only,
                remote_only_lines=prompt_remote_only,
            )
        )
        # Default key is always (s)kip -- never (m)erge or (n)ewer. A clean
        # LCS merge of two genuinely-different documents has zero markers, and
        # "more recently modified" is a heuristic, not correctness -- Enter
        # must not silently accept either. The user types m / n to pick them.
        prompt_default = "s"
        # Loop ONLY the input read. (n)ewer remaps to (l)/(r) here; a tie or an
        # unreadable-mtime 'n' re-prompts rather than skipping (never advance
        # the conflict on an action keystroke). Every other choice breaks out
        # to the existing dispatch below UNCHANGED -- the dispatch owns the
        # apply side-effects, and its `continue` advances the OUTER for-loop,
        # so wrapping the dispatch here would re-prompt a partially-applied
        # conflict (Codex eng review #3). Loop the parse, not the dispatch.
        while True:
            choice = (
                typer.prompt("  Choice", default=prompt_default, show_default=False).strip().lower()
            )
            # Backward-compat (v0.9.0 BREAKING): old letters `c` / `f` are still
            # rejected loudly. They encoded directional ambiguity post-inversion
            # (real silent-data-loss risk -- "kept canonical" meant local OR
            # remote depending on inversion era). Exact-match (not startswith):
            # otherwise "cancel" / "continue" would trip the rejection.
            if choice in ("c", "f"):
                print(
                    "mm: error: input letters 'c' and 'f' are no longer accepted. "
                    "Use (l)ocal to keep your local edits or (r)emote to keep "
                    "the other machine's bytes. (Old labels removed in v0.9.0.)",
                    file=sys.stderr,
                )
                raise typer.Exit(1)

            # (n)ewer: keep the more recently modified side. Remaps to the
            # existing (l)/(r) letters so the per-mode apply + mtime bump are
            # reused verbatim. Never guesses -- on a tie or when a mtime was
            # unreadable (option suppressed) it re-prompts with a note rather
            # than advancing the conflict.
            if choice in ("n", "newer"):
                if nside == "local":
                    choice = "l"
                elif nside == "remote":
                    choice = "r"
                elif nside == "tie":
                    console.print("  [dim]equal mtime — choose manually[/dim]")
                    continue
                else:  # unknown -- (n)ewer was not offered
                    console.print(
                        "  [dim](n)ewer unavailable (timestamp unreadable); "
                        "choose (l)/(r)/(s)[/dim]"
                    )
                    continue

            # Map only the retired b/both compatibility aliases after the
            # resolver-specific c/f and newer policies above. The shared
            # helper emits the existing stderr notice on an exact match.
            choice = _normalize_legacy_skip_choice_and_warn(choice)
            break

        # Exact-match dispatch (not startswith): "leave" / "lookup" must
        # not silently keep local; "retry" / "remove" must not silently
        # delete the conflict file. (codex /review v0.9.0 — caught a real
        # silent-data-loss footgun the eng review missed.)
        if choice in ("l", "local"):
            # Capture peer's mtime BEFORE the rename/unlink so we can bump
            # canonical past it afterward -- see _bump_canonical_mtime_post_resolve
            # for the load-bearing fleet-propagation rationale.
            # Pre-inversion: canonical holds peer's bytes with peer's mtime.
            # Post-inversion: sidecar holds peer's bytes with peer's restored mtime.
            try:
                peer_mtime = (canonical if is_pre_inversion else cpath).stat().st_mtime
            except OSError:
                peer_mtime = 0.0
            if is_pre_inversion:
                # Pre-inversion: sidecar HOLDS local bytes — promote.
                try:
                    cpath.rename(canonical)
                    _bump_canonical_mtime_post_resolve(canonical, peer_mtime)
                    console.print(
                        f"  [green]kept local; promoted[/green] "
                        f"{safe_str(cpath.name)} -> {safe_str(canonical.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]rename failed:[/red] {safe_str(e)}")
                    failed += 1
            else:
                # Post-inversion: canonical IS local — drop the remote sidecar.
                try:
                    cpath.unlink()
                    _bump_canonical_mtime_post_resolve(canonical, peer_mtime)
                    console.print(
                        f"  [green]kept local; discarded remote[/green] {safe_str(cpath.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]delete failed:[/red] {safe_str(e)}")
                    failed += 1
        elif choice in ("r", "remote"):
            if is_pre_inversion:
                # Pre-inversion: canonical IS remote — drop the local sidecar.
                try:
                    cpath.unlink()
                    console.print(
                        f"  [green]kept remote; discarded local[/green] {safe_str(cpath.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]delete failed:[/red] {safe_str(e)}")
                    failed += 1
            else:
                # Post-inversion: sidecar HOLDS remote bytes — promote over local.
                try:
                    cpath.rename(canonical)
                    console.print(
                        f"  [green]kept remote; promoted[/green] "
                        f"{safe_str(cpath.name)} -> {safe_str(canonical.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]rename failed:[/red] {safe_str(e)}")
                    failed += 1
        elif choice in ("m", "merge"):
            # (m)erge accept: write merged_bytes to canonical, drop sidecar.
            # Refuse silently when merge_available is False -- (m) was not
            # offered, treat any "m" / "merge" string as skip rather than
            # writing potentially-empty bytes from the binary-skip branch.
            if not merge_available:
                console.print(
                    "  [dim]merge unavailable for this file; "
                    "skipped (both files left on disk)[/dim]"
                )
                continue
            try:
                fsutil.atomic_write_bytes(canonical, merged_bytes, fsync=False)
            except (OSError, StorageError) as e:
                console.print(
                    f"  [red]merge write failed:[/red] {safe_str(canonical.name)} — {safe_str(e)}"
                )
                failed += 1
                continue
            # Sidecar unlink is best-effort: canonical already holds the
            # merged bytes, so a unlink failure is cosmetic. Stale
            # sidecars get reaped by `mm gc --conflicts` (30d TTL).
            try:
                cpath.unlink()
            except OSError as e:
                print(
                    f"mm: warning: merged result written; sidecar unlink "
                    f"failed: {safe_str(cpath.name)} — {safe_str(e)}",
                    file=sys.stderr,
                )
            if merge_conflicts == 0:
                console.print(f"  [cyan]merged[/cyan] {safe_str(canonical.name)} (clean LCS merge)")
            else:
                console.print(
                    f"  [cyan]merged[/cyan] {safe_str(canonical.name)} "
                    f"(contains {merge_conflicts} <<<<<<< region"
                    f"{'s' if merge_conflicts != 1 else ''}; "
                    f"resolve in editor)"
                )
            resolved += 1
        elif choice in ("p", "promote"):
            # Keep BOTH: rename the sidecar to its own first-class filename.
            # Per-mode naming -- post-inversion sidecar holds the peer's
            # bytes (from-<peer>-<ts>); pre-inversion v0- sidecar holds the
            # user's own local bytes (local-<ts>). `short` and
            # `is_pre_inversion` are already computed above for this hit.
            #
            # Post-inversion only: capture peer's mtime BEFORE the rename so
            # we can bump canonical past it afterward. Without the bump, the
            # local half of "keep both" fails to propagate -- canonical's
            # mtime stays at its old value, the peer's manifest mtime is
            # newer, and the origin peer's next pull mtime-gates this
            # device's local bytes out. Same fleet-propagation rationale as
            # (l)ocal -- promote means keep-both ACROSS the fleet, not just
            # locally. Pre-inversion: canonical holds peer's bytes
            # intentionally (the sidecar HAD local bytes); no bump needed.
            if is_pre_inversion:
                peer_mtime = 0.0
            else:
                try:
                    peer_mtime = cpath.stat().st_mtime
                except OSError:
                    peer_mtime = 0.0
            target = _promote_target_path(canonical, is_pre_inversion, short)
            try:
                target = _promote_conflict_file(cpath, target)
            except OSError as e:
                console.print(f"  [red]promote failed:[/red] {safe_str(e)}")
                failed += 1
            else:
                if not is_pre_inversion and peer_mtime > 0.0:
                    _bump_canonical_mtime_post_resolve(canonical, peer_mtime)
                console.print(
                    f"  [green]promoted[/green] {safe_str(cpath.name)} -> {safe_str(target.name)}"
                )
                resolved += 1
                # Warn if the promoted file landed outside the source's sync
                # surface (an include_files source matches only exact configured
                # names -- a from-/local- name will never be one of them).
                src_cfg = sources_by_name.get(src_name)
                if src_cfg is not None and not _promote_target_will_sync(src_cfg, target):
                    print(
                        f"mm: warning: {safe_str(target.name)} is under an "
                        f"include_files source and will not sync until you add "
                        f"it to config.",
                        file=sys.stderr,
                    )
        elif choice in ("a", "abort"):
            raise typer.Abort()
        else:
            # Default-or-skip path -- includes (s)kip, plain Enter, and any
            # unrecognized input. Both files stay on disk; user can run
            # `mm resolve` later or delete the .sync-conflict-* manually.
            console.print("  [dim]skipped; both files left on disk[/dim]")

    if failed:
        console.print(f"\n[bold]Resolved {resolved} of {len(hits)}; {failed} failed.[/bold]")
    else:
        console.print(f"\n[bold]Resolved {resolved} of {len(hits)}.[/bold]")
    return resolved, failed


__all__ = [
    "_ensure_inversion_marker",
    "_find_conflict_files",
    "_inversion_marker_path",
    "_migrate_pre_inversion_conflict",
    "_promote_conflict_file",
    "_promote_target_path",
    "_promote_target_will_sync",
    "_resolve_interactive_loop",
    "_synced_scan_dirs",
]
