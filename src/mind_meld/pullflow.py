"""Pull planning, application and summary, independent of command registration.

Raise surface and recovery for direct callers of _pull_core:
- typer.Exit(1): unsafe peers or setup refusal; follow the printed remedy and retry.
- typer.Exit(3): fail-mode preflight refused incoming apply; review conflicts or
  retry with keep-both. Only dry_run suppresses migration/history writes too.
- NewerFormatError: update mm before retrying the unreadable storage format.
- CryptoError: verify the passphrase and mm-crypto-init integrity before retrying.
- SnapshotError: restore the named source/access and retry the pull.
- StorageError/OSError: restore storage access or disk space, then retry.
- KeyboardInterrupt/SystemExit/typer.Abort: completed effects/bookkeeping survive;
  retry when ready. Unexpected exceptions reach the shell's mm diag recovery.
Per-file failures are contained in PullResult and retry on the next pull.

Helper-only ValueError (invalid device/conflict name) requires corrected caller
input; _ApplyReporter's RuntimeError requires correcting inconsistent outcomes.
"""

from __future__ import annotations

import copy
import difflib
import errno
import hashlib
import secrets
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import typer
from packaging.version import InvalidVersion, Version
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from mind_meld import config as _config_module
from mind_meld import (
    core,
    fsutil,
    manifest,
    projectid,
    pullhistory,
    pullplan,
    resolveflow,
    safety,
)
from mind_meld.config import MM_INTERNAL_SOURCE_NAMES, resolve_sources
from mind_meld.conflictdiff import (
    count_divergent_lines,
    render_banner,
    render_prompt,
    render_time_line,
    render_verdict,
)
from mind_meld.conflictmtime import (
    _bump_canonical_mtime_post_resolve,
    _restore_mtime_best_effort,
    _stat_mtime_btime,
)
from mind_meld.consoles import console, stderr_console
from mind_meld.crypto import decrypt
from mind_meld.devices import (
    DeviceInventory,
    inventory_devices,
    lookup_device_by_short_id,
)
from mind_meld.errors import (
    PULL_FAILURES_URL,
    SNAPSHOT_FAILURES_URL,
    CryptoError,
    MindMeldError,
    NewerFormatError,
    SnapshotError,
    StorageError,
    StorageNotFound,
)
from mind_meld.manifest import (
    CONFLICT_INFIX,
    CONFLICT_V1_MARKER,
    _under_skip_prefix,
    collect_tombstones,
    diff_files,
    hash_file,
    is_conflict_filename,
    is_pre_inversion_conflict_filename,
    is_tombstoned,
    mtime_from_manifest,
    mtime_from_path,
    parse_conflict_device_short,
)
from mind_meld.merge import lcs_merge, merge_file, should_merge
from mind_meld.safety import safe_str
from mind_meld.storage import get_backend
from mind_meld.storage.keys import blob_key
from mind_meld.storage.local import LocalBackend
from mind_meld.synclog import write_sync_log

ApplyOutcome = Literal[
    "written",
    "merged",
    "merged-via-lcs",
    "skipped",
    "conflicted",
    "unchanged",
    "failed",
    "waiting",
]


# Track 12A: the eligibility invariant for the deferred keep-canonical bump,
# centralized at the _download_and_apply seam. A path is drain-eligible iff its
# last outcome was the keep-canonical "skipped" (the RECORD case). Any outcome
# in this set means a canonical-mutating decision (write, merge, sidecar) hit
# disk successfully, so an earlier pending bump for that path is void — pop it.
# Success-only by construction: _apply_incoming_file returns these ONLY on
# successful canonical mutation (write/merge/sidecar); on failure it returns
# "failed", which is intentionally absent here so the prior decision stands.
# "skipped" / "unchanged" leave canonical untouched, so the prior decision
# also stands.
_CANONICAL_WRITE_OUTCOMES: frozenset[ApplyOutcome] = frozenset(
    {"written", "merged", "merged-via-lcs", "conflicted"}
)


# Track 5E (v0.9.2 BREAKING): minimum peer version required for safe pull.
# v0.9.2 inverted the conflict-direction semantics — a peer running an
# older mm would produce conflict files under the OLD direction (canonical
# = remote, sidecar = local), but a v0.9.2 puller dispatches by filename
# prefix (`v0-` = pre-inversion, no prefix = post-inversion) and would
# silently mis-resolve the peer's just-produced files. Refuse the pull
# until every peer reports last_seen_version >= this constant.
INVERSION_MIN_VERSION = "0.9.2"


@dataclass
class WaitingPeer:
    """What one pull is waiting for from one peer (Track 87A).

    ``files`` counts blobs not delivered yet. ``manifest`` marks a peer
    manifest that vanished mid-read; ``entry`` marks a device entry that
    vanished mid-read (``device_name`` is then the device id).
    """

    device_id: str
    device_name: str
    files: int = 0
    manifest: bool = False
    entry: bool = False


@dataclass
class OldFormPeer:
    """A peer still publishing path names: files withheld until it updates (Track 90A).

    Kept apart from `WaitingPeer`: this waits on a person updating a machine,
    not on the sync tool's delivery, so it never reads as "waiting for".
    """

    device_id: str
    device_name: str
    files: int = 0


@dataclass
class PullResult:
    """Result of a pull operation.

    Outcomes are split per the _apply_incoming_file decision tree:
      written    — local had no copy; remote written to canonical path
      merged     — .jsonl or MEMORY.md union-merged with local
      skipped    — local is newer than remote (no write)
      conflicted — hashes differed and remote newer/equal: local canonical
                   preserved, remote written to .sync-conflict-*
      unchanged  — apply-time re-read showed local already matches remote
      failed     — rename/write error; local preserved, no change applied
      waiting    — the blob has not been delivered by the sync tool yet
                   (Track 87A); nothing was applied and nothing failed. It
                   is retried on the next pull and is never a failure or a
                   degradation. A peer's manifest conflict copies are kept
                   while any of its files are waiting.

    `total_skipped_unknown_source` counts (device, source_name) pairs where
    a peer advertised a source name the local config doesn't know about
    (partition risk: if a user renames a source, peers' data stops syncing
    silently). One increment per (device, source) pair per pull, not per file.
    """

    no_peers: bool = False
    no_sources: bool = False
    total_written: int = 0
    total_merged: int = 0
    total_skipped: int = 0
    total_conflicted: int = 0
    total_failed: int = 0
    total_skipped_unknown_source: int = 0
    bytes_transferred: int = 0
    device_names: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    # Degradation signals: set to non-zero when the pull succeeded overall
    # but some part of it is at-risk. autopull() aggregates these into the
    # `degraded` breadcrumb outcome. Counts only — detailed failure records
    # live in fsync_warnings / corrupt_peers inside _pull_core and are
    # surfaced to stderr by _print_pull_summary.
    durability_fsync_failures: int = 0
    corrupt_peer_count: int = 0
    # Content the sync tool has not delivered yet (Track 87A): blobs per peer,
    # a peer manifest or a device entry that vanished mid-read. Never a failure
    # and never a degradation; the outcome line names it and the next pull
    # picks it up. Attended runs do not record it.
    total_waiting: int = 0
    waiting: list[WaitingPeer] = field(default_factory=list)
    # Track 90A: another home's files from peers still on path names, and the
    # projects whose checkout is not on this machine. Never a failure.
    old_form_peers: list[OldFormPeer] = field(default_factory=list)
    not_placed_files: int = 0
    not_placed_projects: int = 0

    @property
    def total_applied(self) -> int:
        """Files actually changed on disk (written + merged + conflicted)."""
        return self.total_written + self.total_merged + self.total_conflicted


PULL_NOT_PREVIEWED = (
    "Not previewed: renaming pre-v0.9.2 conflict files to the v0- prefix, "
    "pull-history rows, per-project .mind-meld-log.md sync logs, cleanup of "
    "merged manifest conflict copies in storage, blob download and decrypt failures, "
    "and blobs the sync tool has not delivered yet (reported as waiting)."
)


def _detect_case_insensitive_fs(path: Path) -> bool:
    """Probe whether `path`'s volume is case-insensitive (APFS default, NTFS).

    Non-invasive: no writes. Constructs a swapcase variant of the path's
    own basename and checks via `samefile()` whether both names resolve
    to the same inode. Missing roots use the nearest existing ancestor.
    Inconclusive probes conservatively assume case insensitivity.
    """
    try:
        while True:
            try:
                path.stat()
                break
            except FileNotFoundError:
                if path.parent == path:
                    return True
                path = path.parent
        if path.name.swapcase() == path.name:
            return True
        alt = path.with_name(path.name.swapcase())
        try:
            return alt.samefile(path)
        except FileNotFoundError:
            return False
    except OSError:
        return True


def _detect_pull_case_collisions(
    manifest_cache: dict[str, dict | None],
    local_sources_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    """Detect case-collision clusters in peer manifest paths per source.

    Returns `{src_name: {casefold_key: [colliding_rel_paths_sorted]}}`
    only for sources whose local base_path is on a case-insensitive FS
    AND have ≥2 distinct rel_paths casefolding to the same key. Empty
    dict on case-sensitive volumes — no local collision possible.

    Aggregates across ALL peer manifests so a collision between peer A's
    "Projects/x.md" and peer B's "projects/x.md" is detected even when
    neither peer alone exposes both casings.
    """
    collisions: dict[str, dict[str, list[str]]] = {}
    for src_name, src_info in local_sources_map.items():
        base_path = src_info["path"]
        # Probe the filesystem the files actually land on, not the symlink's
        # own location — local_sources_map keeps mm-events' custom roots
        # unresolved for ownership checks, but a link can cross a volume
        # boundary with different case-sensitivity than its parent directory.
        if not _detect_case_insensitive_fs(base_path.resolve()):
            continue
        seen_paths_by_key: dict[str, set[str]] = {}
        for peer_manifest in manifest_cache.values():
            if peer_manifest is None:
                continue
            src_data = peer_manifest.get("sources", {}).get(src_name)
            if not src_data:
                continue
            for rel_path in src_data.get("files", {}):
                key = rel_path.casefold()
                seen_paths_by_key.setdefault(key, set()).add(rel_path)
        clusters = {
            key: sorted(paths) for key, paths in seen_paths_by_key.items() if len(paths) > 1
        }
        if clusters:
            collisions[src_name] = clusters
    return collisions


def _drop_case_collisions_from_manifests(
    manifest_cache: dict[str, dict | None],
    collisions: dict[str, dict[str, list[str]]],
) -> dict[str, dict | None]:
    """Drop all-but-lex-first colliding rel_paths from each peer manifest.

    Returns a new cache; input is not mutated. Caller emits one
    `mm: warning:` per cluster naming the kept and dropped paths
    (visible-failure contract). Tombstones are not touched — collision
    is about per-pull WRITES on case-insensitive consumer; tombstones
    encode prior consensus and stay intact (mirrors the asymmetric
    `_filter_disabled_sources` invariant).
    """
    if not collisions:
        return manifest_cache
    new_cache: dict[str, dict | None] = {}
    for did, peer_manifest in manifest_cache.items():
        if peer_manifest is None:
            new_cache[did] = None
            continue
        new_manifest = copy.deepcopy(peer_manifest)
        for src_name, clusters in collisions.items():
            src_data = new_manifest.get("sources", {}).get(src_name)
            if not src_data:
                continue
            files = src_data.get("files", {})
            for paths in clusters.values():
                for drop in paths[1:]:
                    files.pop(drop, None)
        new_cache[did] = new_manifest
    return new_cache


def _print_pull_prediction(
    predictions: list[pullplan.PullPrediction], base_path: Path, src_name: str
) -> None:
    console.print(f"  [dim]source '{safe_str(src_name)}' ({safe_str(base_path)}):[/dim]")
    for prediction in predictions:
        if prediction.outcome != "unchanged":
            console.print(f"    {safe_str(prediction.label)}  {safe_str(prediction.rel_path)}")


_CONFLICT_NAME_RANDOM_ATTEMPTS = 5


def _conflict_name_is_free(path: Path) -> bool:
    """True only when ``lstat`` raises FileNotFoundError.

    Files, directories, and dangling symlinks are occupied. Other OSError
    (permission, I/O) propagates so the caller fails this file rather than
    treating a denied probe as either free or occupied.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def conflict_filename(
    canonical: Path,
    device_id: str,
    now: datetime | None = None,
) -> Path:
    """Compute the sibling path used to preserve a remote divergent version.

    Syncthing convention, v1-era (v0.14.0+):
    <stem>.sync-conflict-<YYYYMMDD-HHMMSS>-v1-<device_short>.<ext>

    The ``v1`` token goes AFTER the timestamp, never as a ``v0-``-style
    prefix: a prefix form fails ``is_conflict_filename``, so
    ``_is_excluded`` is False, so the conflict copy would upload to the
    fleet. The timestamp is UTC (``datetime.now(timezone.utc)``) and is
    the sidecar's own birth time; ``st_mtime`` is restored to the peer
    file's clock separately.

    If the computed path is already occupied (same-second double-conflict
    on the same device, or a leftover directory/symlink at that name),
    append a 4-char random suffix and retry up to
    ``_CONFLICT_NAME_RANDOM_ATTEMPTS`` times. Occupancy is ``lstat``:
    only FileNotFoundError means free. This protects preexisting occupied
    names under the mm lock; it is not atomic no-clobber against an
    external writer creating the same name between probe and publish.

    Raises ValueError on empty/None `device_id`, or if every candidate
    name is occupied. The caller (peer manifest or local config) is
    responsible for supplying a non-empty id; the previous `"unknown"`
    fallback silently minted cross-device-colliding filenames when a
    corrupted peer manifest fed an empty id, which is a data-loss footgun.
    """
    if not device_id:
        raise ValueError("conflict_filename: device_id must be non-empty")
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    device_short = device_id[:8]

    stem = canonical.stem
    suffix = canonical.suffix
    base_name = f"{stem}{CONFLICT_INFIX}{ts}-{CONFLICT_V1_MARKER}-{device_short}"
    path = canonical.with_name(f"{base_name}{suffix}")

    if _conflict_name_is_free(path):
        return path
    for _ in range(_CONFLICT_NAME_RANDOM_ATTEMPTS):
        rand = secrets.token_hex(2)
        candidate = canonical.with_name(f"{base_name}-{rand}{suffix}")
        if _conflict_name_is_free(candidate):
            return candidate
    raise ValueError(
        "conflict_filename: no unused name for "
        f"{canonical.name} after {_CONFLICT_NAME_RANDOM_ATTEMPTS} random suffixes"
    )


def _prompt_conflict_choice(
    rel_path: str,
    local_path: Path,
    remote_data: bytes,
    peer_name: str | None = None,
    ambiguous_count: int = 0,
    *,
    remote_mtime: datetime | None = None,
) -> tuple[str, bytes | None]:
    """Prompt interactively for how to handle one conflict. Default skip.

    ``peer_name`` is the human-readable name of the device that pushed the
    remote bytes (resolved by the caller via ``lookup_device_by_short_id``);
    ``None`` if unknown or ambiguous. ``ambiguous_count`` is the number of
    matching peers when the device-id prefix collides (>=2); zero otherwise.
    Both flow into the REMOTE banner so the user sees attribution at the
    moment of the choice.

    ``remote_mtime`` is the peer's modified time from the manifest (the
    remote file is not on disk at this site, so this is the only remote
    timestamp available). Keyword-only and trailing so it never shifts the
    positional ``peer_name`` / ``ambiguous_count`` binding at the call site.
    Used for the display + recency verdict only -- there is no ``(n)ewer``
    shortcut here (see the stat block below for why).

    Returns ``(choice, merged_bytes)``. Choice is one of:
    ``keep-canonical`` (= keep local), ``keep-remote``, ``merge``,
    ``abort``, ``keep-both``. ``keep-both`` is what (s)kip emits today --
    the on-disk effect is "leave both files in place." ``merge`` is
    accompanied by ``merged_bytes`` -- the LCS-merged result for the
    caller to write to ``local_path``. For all other choices
    ``merged_bytes`` is ``None``.
    """
    from mind_meld import conflictdiff

    # Stat local for the timestamp display. Best-effort: a failed stat just
    # renders "unknown". The remote side has NO on-disk file at this inline
    # site, so its created/birthtime is genuinely unavailable -- only the
    # manifest's modified time (passed in as ``remote_mtime``) is shown.
    # NOTE: no ``(n)ewer`` shortcut here -- _apply_incoming_file already
    # skipped before prompting when local is newer (cli.py mtime gate), so
    # at this prompt remote is always newer-or-equal and "(n)ewer" would be
    # a redundant alias of (r). Display + verdict only.
    local_mtime_ts, local_btime_ts = _stat_mtime_btime(local_path)
    remote_mtime_ts = remote_mtime.timestamp() if remote_mtime is not None else None

    local_read_failed = False
    try:
        local_bytes = local_path.read_bytes()
    except OSError:
        # Track the failure separately so the (m) option is not offered
        # against an empty-substitute local. Without this, lcs_merge(b"",
        # remote) returns remote as a "clean merge" with default-key (m)
        # and the user would silently overwrite an unreadable-but-extant
        # local with peer bytes (EACCES race, transient FS failure,
        # iCloud placeholder hiccup).
        local_bytes = b""
        local_read_failed = True
    local_text = local_bytes.decode("utf-8", errors="replace").splitlines()
    remote_text = remote_data.decode("utf-8", errors="replace").splitlines()

    # Inline pull-time prompts post-inversion only -- _apply_conflict only
    # produces post-inversion sidecars. local_bytes IS the local side;
    # remote_data IS the peer side; lcs_merge returns conflict_count = -1
    # when either side is binary so we can suppress (m).
    if local_read_failed:
        merged_bytes, merge_conflicts = b"", -1
    else:
        merged_bytes, merge_conflicts = lcs_merge(local_bytes, remote_data)
    # A single-line side gives lcs_merge nothing to align on, so (m) could only
    # ever emit one marker region wrapping both versions whole. Suppress it
    # through the same gate as binary content.
    merge_available = merge_conflicts >= 0 and conflictdiff.merge_has_line_structure(
        local_text, remote_text
    )

    safe_rel = safe_str(rel_path)
    diff = list(
        difflib.unified_diff(
            local_text,
            remote_text,
            fromfile=f"local {safe_rel}",
            tofile=f"remote {safe_rel}",
            lineterm="",
            n=3,
        )
    )

    with core._pause_sync_progress():
        console.print(f"\n[bold yellow]Conflict:[/bold yellow] {safe_rel}")
        console.print(render_banner("local", local_path.name, None))
        console.print(render_time_line([("modified", local_mtime_ts), ("created", local_btime_ts)]))
        console.print(
            render_banner(
                "remote",
                local_path.name,
                peer_name,
                ambiguous_count=ambiguous_count,
            )
        )
        console.print(render_time_line([("modified", remote_mtime_ts)]))
        _verdict = render_verdict(local_mtime_ts, remote_mtime_ts)
        if _verdict is not None:
            console.print(_verdict)

        # Inline pull-time site is post_inversion only (see comment above):
        # diff is local -> remote, so m = local-only, n = remote-only directly.
        m, n, k = count_divergent_lines(diff)
        if k:
            console.print(
                f"  [dim]{m} unique line{'' if m == 1 else 's'} of yours; "
                f"{n} unique line{'' if n == 1 else 's'} from peer; "
                f"{k} total diff lines.[/dim]"
            )

        # Shared rendering owns terminal-safe peer content, but inline pull keeps
        # its historical 60-entry consent window (mm resolve uses 80).
        for renderable in conflictdiff.render_capped_diff(diff, cap=60):
            console.print(renderable)

        # The conflict file is not on disk yet at this site -- _apply_conflict
        # writes it AFTER this function returns "keep-both" -- so render_prompt's
        # "discard <conflict-name>" copy doesn't quite apply at first glance.
        # Use the bare basename of the local file as both labels: the canonical
        # filename in both positions so the user sees the action's target.
        # Suppress drop-count annotations on empty-diff (binary) -- annotating
        # "drops 0 lines" when we couldn't compare would be a false reassurance.
        prompt_m: int | None = m if diff else None
        prompt_n: int | None = n if diff else None
        console.print(
            render_prompt(
                safe_rel,
                safe_rel,
                "post_inversion",
                merge_available=merge_available,
                merge_conflicts=max(merge_conflicts, 0),
                local_only_lines=prompt_m,
                remote_only_lines=prompt_n,
            )
        )
        # Default key is always (s)kip -- never (m)erge. A clean LCS merge of two
        # genuinely-different documents has zero conflict markers, so defaulting
        # Enter to (m) means one keystroke silently accepts a Frankenstein file.
        # (m)erge stays a fully available choice; the user must type it.
        prompt_default = "s"
        choice = typer.prompt("Choice", default=prompt_default, show_default=False).strip().lower()

    if choice in ("l", "local", "keep-canonical"):
        return "keep-canonical", None
    if choice in ("r", "remote", "keep-remote"):
        return "keep-remote", None
    if choice in ("m", "merge"):
        if merge_available:
            return "merge", merged_bytes
        # (m) was not offered (binary content) -- treat the literal
        # letter as skip rather than writing potentially-empty bytes.
        return "keep-both", None
    if choice in ("a", "abort"):
        return "abort", None
    # Default-or-skip path: any unrecognized input AND (s)kip itself.
    return "keep-both", None


# \u2500\u2500 _apply_incoming_file decision tree \u2500\u2500\u2500
#
#   Re-read local state at apply time (user may have edited since _pull_core
#   snapshotted). Decide outcome:
#
#   local missing                           -> WRITE     (remote -> canonical)
#   local hash == remote hash               -> UNCHANGED
#   should_merge(rel_path)                  -> MERGED    (jsonl / MEMORY.md)
#   local mtime > remote mtime              -> SKIPPED   (local newer)
#   local mtime <= remote mtime + (m)erge   -> MERGED-VIA-LCS  (user confirmed)
#                                              write merged_bytes -> canonical
#   local mtime <= remote mtime             -> CONFLICTED  (v0.9.2 INVERTED)
#        keep canonical at LOCAL bytes (no rename, no rollback)
#        write REMOTE bytes -> .sync-conflict-<ts>-<device>.<ext>
#
#   Pre-v0.9.2 did the opposite (canonical = remote, sidecar = local). The
#   inversion makes the visible `.sync-conflict-*` file hold the surprising
#   bytes, not the working bytes. Pre-inversion files migrated by Track 5E
#   carry a `v0-` prefix in the metadata position so resolve's dual-mode
#   dispatch can pick the right semantics per file.
#
#   Publication -> _ApplyReporter before restore, cleanup, or output.
#   OS/mm exceptions -> failed (or keep a recorded publication); other
#   exceptions propagate with completed bookkeeping. Invalid mtimes fall
#   through to conflict at the parse site, never at the general boundary.


# ── deferred inline keep-canonical mtime bump (Track 12A) ─────────────
#
#   _apply_incoming_file (per peer × source × file)
#     keep-canonical → _record_inline_bump (canonical untouched)
#     all other branches → return their outcome
#                          │
#   _ApplyReporter: at publication, or non-publishing return at the boundary
#     outcome in _CANONICAL_WRITE_OUTCOMES → _invalidate_inline_bump
#       (success-only by construction: write/merge/sidecar outcomes are only
#       returned on successful canonical mutation; "failed" / "skipped" /
#       "unchanged" leave canonical untouched and don't invalidate)
#                          │
#   _pull_core: after the device loop, INSIDE the try block
#     _drain_inline_bumps → _bump_canonical_mtime_post_resolve(path, mtime)
#
# Why deferred, not inline: bumping canonical's mtime mid-pull-walk makes a
# LATER peer's _apply_incoming_file mis-classify on the `local_mtime >
# remote_mtime` gate and silently skip — hiding a peer whose mtime falls
# between original-local and the bump value (the v0.12.6 revert). Deferring
# to end-of-batch means every peer is judged against the SAME original
# baseline; the bump value (max over every keep-canonical peer for that
# path) then beats all of them at once.
#
# Why invalidation lives in _ApplyReporter.record, not in
# _apply_incoming_file's per-branch returns: ONE site keyed on the outcome
# enum covers every canonical-mutating path uniformly — keep-remote, inline
# merge, keep-both (the _apply_conflict sidecar), AND the _apply_write
# branch that fires when canonical vanished mid-walk (user `rm`'d the file
# while the blocking prompt waited). Publication-time recording also voids
# the bump before a fallible restore/cleanup/print can raise. The per-branch
# approach missed _apply_write entirely (would silently bump REMOTE bytes as
# locally-authored) and was not success-only for keep-both (would pop on a
# sidecar write failure even though canonical was still local). See
# docs/invariants/conflicts.md.


def _record_inline_bump(
    pending: dict[Path, float] | None,
    canonical: Path | None,
    peer_mtime: float,
) -> None:
    """Record an inline ``keep-canonical`` decision for the end-of-batch drain.

    No-op when ``pending`` is None (non-interactive pull) or ``canonical`` is
    None (library / direct-call test callers). Keyed on the RESOLVED path so a
    symlinked source root or path-spelling alias can't leave a stale entry that
    a later write keyed under the resolved spelling fails to invalidate.
    ``max`` so that when several peers conflict on the same file in one walk,
    the bump beats every peer walked.
    """
    if pending is None or canonical is None:
        return
    pending[canonical] = max(pending.get(canonical, 0.0), peer_mtime)


def _invalidate_inline_bump(
    pending: dict[Path, float] | None,
    canonical: Path | None,
) -> None:
    """Drop a pending inline bump because a later peer changed/left this file.

    Called from ``_ApplyReporter.record`` when the outcome is in
    ``_CANONICAL_WRITE_OUTCOMES`` (write / merge / merge-via-lcs / conflicted):
    at publication, or at a non-publishing return such as sidecar dedup.
    Success-only by construction: those outcomes are only recorded on
    successful canonical mutation; "failed" leaves canonical pure local, so
    the prior keep-canonical decision still stands and the bump is NOT popped.
    Same property for "skipped" (canonical untouched — mtime-skip branch) and
    "unchanged" (sha match).

    An earlier peer's keep-canonical bump is void once a later peer's decision
    for the same file either overwrites canonical (bumping would broadcast the
    later peer's bytes as locally-authored — the _apply_write file-vanished-
    mid-walk hazard) or leaves it unresolved as a sidecar (bumping would
    silently mtime-resolve a conflict the user explicitly left open).
    """
    if pending is None or canonical is None:
        return
    pending.pop(canonical, None)


def _drain_inline_bumps(pending: dict[Path, float] | None) -> None:
    """Apply every recorded inline ``keep-canonical`` bump at end-of-pull-batch.

    Runs once in ``_pull_core`` AFTER every peer/source has been walked. No-op
    when ``pending`` is None (non-interactive pull) or empty. Placed INSIDE
    ``_pull_core``'s try block: a ``typer.Abort()`` from the inline ``(a)bort``
    choice intentionally skips the drain — abort means "stop, I don't trust
    this pull", so half-made decisions are not broadcast to the fleet.
    """
    if not pending:
        return
    for canonical, peer_mtime in pending.items():
        _bump_canonical_mtime_post_resolve(canonical, peer_mtime)


def _first_existing_ancestor(path: Path) -> Path:
    """Find the actual collider, even beneath several missing components."""
    while True:
        try:
            path.stat()
            return path
        except (FileNotFoundError, NotADirectoryError):
            if path.parent == path:
                raise
            path = path.parent


def _warn_apply_failure(
    device_name: str | None,
    src_name: str | None,
    rel_path: str,
    local_path: Path,
    operation: str,
    exc: BaseException,
    *,
    preserved: str,
) -> None:
    """One plain-stderr line: attribution, cause, preservation, and remedy."""
    safe = safety.safe_terminal_str
    location = "/".join(safe(p) for p in (device_name, src_name, rel_path) if p is not None)
    error_number = getattr(exc, "errno", None) or getattr(exc.__cause__, "errno", None)
    remedy = ""
    if isinstance(exc, NewerFormatError):
        remedy = core._newer_format_message(exc.version, exc.subject) + " "
    elif error_number in (errno.EEXIST, errno.ENOTDIR, errno.EISDIR):
        try:
            collider = _first_existing_ancestor(local_path.parent)
            if not collider.is_dir():
                remedy = (
                    f"{safe(device_name or 'The peer')} published a folder named "
                    f"'{safe(collider.name)}' where this Mac has a file; "
                    "keep yours or move it aside, then run mm pull. "
                )
        except OSError:
            pass
    elif error_number in (errno.EACCES, errno.EPERM, errno.EROFS):
        remedy = f"check write permission on {safe(local_path.parent)}. "
    elif error_number == errno.ENOSPC:
        remedy = "free disk space. "
    core._print_apply_warning(
        f"mm: warning: {safe(operation)} ({safe(preserved)}): {location} — "
        f"{safe(exc)}. {remedy}See {safe(PULL_FAILURES_URL)}."
    )


@dataclass
class _ApplyReporter:
    """Per-source publication ledger; begin() binds each file before apply.

    With outcomes=None this is the direct-call diagnostic default: it prints
    failures but does not record publications or invalidate deferred bumps.
    No disk inference: the recorded word is what this attempt actually did.
    """

    device_name: str | None = None
    src_name: str | None = None
    outcomes: dict[ApplyOutcome, list[str]] | None = None
    pending_inline_bumps: dict[Path, float] | None = None
    touched_parents: set[Path] = field(default_factory=set)
    bytes_transferred: int = 0
    rel_path: str = ""
    local_path: Path = Path()
    resolved_local: Path | None = None
    recorded: ApplyOutcome | None = None
    follow_up: str = "follow-up step"

    def begin(self, rel_path: str, local_path: Path, resolved_local: Path | None) -> None:
        self.rel_path, self.local_path, self.resolved_local = rel_path, local_path, resolved_local
        self.recorded = None
        self.follow_up = "follow-up step"

    def record(self, outcome: ApplyOutcome) -> None:
        if self.outcomes is None:
            return
        if self.recorded is not None:
            if self.recorded != outcome:
                raise RuntimeError(f"apply outcome changed: {self.recorded} -> {outcome}")
            return
        self.outcomes[outcome].append(self.rel_path)
        if outcome in _CANONICAL_WRITE_OUTCOMES:
            _invalidate_inline_bump(self.pending_inline_bumps, self.resolved_local)
            self.touched_parents.add(self.local_path.parent)
        self.recorded = outcome

    def published(self, outcome: ApplyOutcome) -> None:
        self.record(outcome)

    def created_ancestor(self, path: Path) -> None:
        if self.outcomes is not None:
            self.touched_parents.add(path.parent)

    def failed(self, operation: str, exc: BaseException, *, preserved: str) -> None:
        self.record("failed")
        _warn_apply_failure(
            self.device_name,
            self.src_name,
            self.rel_path,
            self.local_path,
            operation,
            exc,
            preserved=preserved,
        )

    def follow_up_failed(self, exc: BaseException) -> None:
        wording = {
            "written": "file written",
            "merged": "file merged",
            "merged-via-lcs": "file merged",
            "conflicted": "conflict copy saved",
        }.get(self.recorded, self.recorded)
        safe = safety.safe_terminal_str
        core._print_apply_warning(
            f"mm: notice: {safe(self.local_path)}: {safe(wording)}; "
            f"{safe(self.follow_up)} failed ({safe(exc)}). Completed changes are on disk."
        )


def _apply_write(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    verbose: bool = False,
    remote_mtime_iso: str | None = None,
    *,
    reporter: _ApplyReporter | None = None,
) -> ApplyOutcome:
    """[W] local has no copy \u2014 atomic_write remote to canonical."""
    reporter = reporter or _ApplyReporter(rel_path=rel_path, local_path=local_path)
    try:
        # Deferred durability: per-file fsync=False; end of pull calls
        # fsutil.fsync_dir once per touched parent.
        fsutil.atomic_write_bytes(local_path, plain_data, fsync=False)
    except (OSError, StorageError) as e:
        reporter.failed("write failed", e, preserved="local preserved")
        return "failed"
    reporter.published("written")
    reporter.follow_up = "mtime restore"
    _restore_mtime_best_effort(local_path, remote_mtime_iso)
    reporter.follow_up = "output"
    if verbose:
        console.print(f"  [green]\u2193[/green] {safe_str(rel_path)}")
    return "written"


def _apply_merge(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    verbose: bool = False,
    *,
    reporter: _ApplyReporter | None = None,
) -> ApplyOutcome:
    """[M] mergeable: jsonl / MEMORY.md are line-union safe.

    No-op suppression: if the line-union merge produces bytes byte-identical
    to what's already on disk (the dominant case when local is a strict
    superset of remote, e.g. peer's MEMORY.md already covered by ours), we
    skip the write entirely and return "unchanged". Skipping the write also
    avoids touching mtime \u2014 which would itself fabricate a phantom
    modification on the next push. The "merged" count then reflects only
    files that actually changed, eliminating the "every pull says
    1+ merged" noise users were seeing.
    """
    reporter = reporter or _ApplyReporter(rel_path=rel_path, local_path=local_path)
    try:
        local_bytes = local_path.read_bytes()
        merged = merge_file(rel_path, local_bytes, plain_data)
        if merged == local_bytes:
            if verbose:
                console.print(f"  [dim]= {safe_str(rel_path)} (merge no-op)[/dim]")
            return "unchanged"
        fsutil.atomic_write_bytes(local_path, merged, fsync=False)
    except (OSError, StorageError) as e:
        reporter.failed("merge failed", e, preserved="local preserved")
        return "failed"
    reporter.published("merged")
    reporter.follow_up = "output"
    if verbose:
        console.print(f"  [cyan]merged[/cyan] {safe_str(rel_path)}")
    return "merged"


def _existing_post_inversion_sidecars_from_peer(canonical: Path, device_short: str) -> list[Path]:
    """List post-inversion ``.sync-conflict-*`` siblings of `canonical` from one peer.

    Used by ``_apply_conflict`` to dedup against prior pulls: every pull
    where peer bytes still don't match local would otherwise create
    another timestamped sidecar (``conflict_filename`` always stamps
    ``datetime.now()``), so users were accumulating N near-identical
    sidecars per peer over N pulls.

    Ownership is exact Path equality via ``manifest._canonical_for_conflict``,
    checked before ``is_file``/read. A stem-prefix glob is not ownership:
    ``notes.md`` must not select ``notes.sync-conflict-log.md``'s sidecar.

    Skips ``v0-``-prefixed pre-inversion sidecars. Those hold LOCAL
    bytes from a pre-v0.9.2 conflict and must NEVER be reaped by the
    apply path -- they encode user data that may not exist anywhere
    else (the local file was renamed out under the inverted semantics).
    The user resolves them through ``mm resolve``'s migration path.

    Returns sidecars whose parsed device_short matches ``device_short``.
    Listing failures (permission, transient FS errors) return empty so
    the caller falls through to the existing write path. An individual
    eligible candidate that cannot be statted is skipped (left untouched)
    with a sanitized stderr warning.
    """
    parent = canonical.parent
    out: list[Path] = []
    try:
        candidates = list(parent.glob(f"*{CONFLICT_INFIX}*"))
    except OSError:
        return []
    for sibling in candidates:
        if not is_conflict_filename(sibling.name):
            continue
        if manifest._canonical_for_conflict(sibling) != canonical:
            continue
        if is_pre_inversion_conflict_filename(sibling.name):
            continue
        if parse_conflict_device_short(sibling.name) != device_short:
            continue
        try:
            is_regular = sibling.is_file()
        except OSError as e:
            core._print_apply_warning(
                "mm: warning: conflict sidecar unreadable (left in place): "
                f"{safety.safe_terminal_str(sibling)} — {safety.safe_terminal_str(e)}"
            )
            continue
        if not is_regular:
            continue
        out.append(sibling)
    return out


def _apply_conflict(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    remote_device_id: str,
    verbose: bool = False,
    remote_mtime_iso: str | None = None,
    *,
    reporter: _ApplyReporter | None = None,
) -> ApplyOutcome:
    """[C] conflict path (v0.9.2 INVERTED): keep local at canonical,
    route remote bytes to .sync-conflict-*.

    Pre-v0.9.2 (Tracks 5A/5B and earlier) did the opposite: rename local
    out to the sidecar, write remote to canonical. Track 5E inverted the
    default for two reasons: (1) asymmetric blast radius \u2014 local is the
    known-working version on this machine, remote is the unknown-from-
    elsewhere version; (2) the visible `.sync-conflict-*` should hold the
    *surprising* version, not the working one. Mtime-skip already
    handles "local newer," so the conflict path only fires when remote
    is newer or mtimes are equal \u2014 but "remote newer" never meant "remote
    correct for this machine."

    Per-peer dedup (post-v0.11.4, publish-then-cleanup as of Track 48A):
    scan existing post-inversion sidecars from this peer for the same
    canonical. If one already holds the same bytes, skip the write --
    the prior sidecar already represents this conflict. If existing
    sidecars hold STALE bytes (peer pushed something newer), publish
    the replacement first, then best-effort unlink only the copies
    whose bytes were successfully read and differ. Unreadable copies
    stay; a failed publish leaves every old copy in place.

    Failure modes (per-file isolation; never destroys local without a
    recoverable trail):
      * sidecar path-build (empty/None remote_device_id, occupied-name
        exhaustion, occupancy-probe OSError): warn on stderr, fail this
        file, keep walking. Local and prior conflict copies remain.
      * sidecar write fail: warn on stderr, fail this file. Local is
        untouched at canonical because we never wrote it out \u2014 the
        inversion makes rollback unnecessary. Prior conflict copies
        remain because cleanup has not run.
      * stale unlink fail after a successful write: warn on stderr;
        outcome stays conflicted. The new copy is saved and the old
        copy remains recoverable via ``mm resolve``.
    """
    # Candidate and verified-different lists are initialized outside the
    # optional peer scan so a path-build/write failure never depends on
    # an unbound local, and so cleanup cannot run before publication.
    reporter = reporter or _ApplyReporter(rel_path=rel_path, local_path=local_path)
    existing: list[Path] = []
    stale: list[Path] = []
    # Per-peer dedup. Empty/None remote_device_id falls through to the
    # ValueError branch below where conflict_filename refuses to mint a
    # path -- skip the dedup scan for that case (we couldn't attribute
    # any existing sidecar to this peer anyway).
    if remote_device_id:
        device_short = remote_device_id[:8]
        existing = _existing_post_inversion_sidecars_from_peer(local_path, device_short)
        for sidecar in existing:
            try:
                sidecar_bytes = sidecar.read_bytes()
            except OSError as e:
                core._print_apply_warning(
                    "mm: warning: conflict sidecar unreadable (left in place): "
                    f"{safety.safe_terminal_str(sidecar)} — {safety.safe_terminal_str(e)}"
                )
                continue
            if sidecar_bytes == plain_data:
                # Idempotent: peer bytes unchanged from a prior pull;
                # the existing sidecar already represents this conflict.
                # Skip the write so we don't accumulate duplicates.
                # Unchanged-content retries do not clean extras.
                if verbose:
                    console.print(
                        f"  [yellow]conflict (unchanged):[/yellow] "
                        f"{safe_str(rel_path)} "
                        f"(existing sidecar {safe_str(sidecar.name)})"
                    )
                return "conflicted"
            stale.append(sidecar)

    try:
        conflict_path = conflict_filename(local_path, remote_device_id)
    except (ValueError, OSError) as e:
        # Empty/None remote_device_id (corrupted peer manifest), name
        # exhaustion, or occupancy-probe OSError. Preserve per-file
        # isolation: warn and fail this file only, keep walking.
        reporter.failed(
            "conflict path build failed", e, preserved="local and prior conflict copies preserved"
        )
        return "failed"

    # Publish while every old copy still exists. Cleanup runs only after
    # this write succeeds, and only for verified-different candidates.
    # Inverted semantics: write remote bytes to the .sync-conflict-* sidecar.
    # Canonical (local) is untouched \u2014 no rename + rollback dance needed
    # because we never overwrite local in the conflict path. The sidecar
    # filename carries a `v1` era marker AFTER the timestamp (v0.14.0+).
    # The `v0-` prefix is reserved for pre-inversion files migrated by
    # `_migrate_pre_inversion_conflict`, NOT new files produced post-inversion.
    try:
        fsutil.atomic_write_bytes(conflict_path, plain_data, fsync=False)
    except (OSError, StorageError) as e:
        reporter.failed(
            "sidecar write failed", e, preserved="local and prior conflict copies preserved"
        )
        return "failed"
    reporter.published("conflicted")
    reporter.follow_up = "conflict-copy mtime restore"
    _restore_mtime_best_effort(conflict_path, remote_mtime_iso)

    reporter.follow_up = "stale-copy cleanup"
    for sidecar in stale:
        if sidecar == conflict_path:
            continue
        try:
            sidecar.unlink()
        except OSError as e:
            core._print_apply_warning(
                "mm: warning: replacement saved as "
                f"{safety.safe_terminal_str(conflict_path)}; old copy remains at "
                f"{safety.safe_terminal_str(sidecar)} — {safety.safe_terminal_str(e)}. "
                "Inspect with mm resolve."
            )

    reporter.follow_up = "output"
    if verbose:
        console.print(
            f"  [yellow]conflict:[/yellow] {safe_str(rel_path)} "
            f"(remote saved as {safe_str(conflict_path.name)})"
        )
    return "conflicted"


def _apply_incoming_file(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    remote_info: dict,
    remote_device_id: str,
    interactive_resolve: bool = False,
    verbose: bool = False,
    devices: list[dict[str, Any]] | None = None,
    pending_inline_bumps: dict[Path, float] | None = None,
    resolved_local: Path | None = None,
    *,
    reporter: _ApplyReporter | None = None,
) -> ApplyOutcome:
    """Dispatch one decrypted remote file to the appropriate _apply_* helper.

    See the decision-tree comment above for branch semantics. The local file
    is never destroyed without a recoverable trail (either conflict copy
    or rollback).

    ``pending_inline_bumps`` / ``resolved_local`` carry the Track 12A deferred
    keep-canonical bump. In interactive pull, the keep-canonical branch RECORDS
    into the dict; INVALIDATION is owned by ``_ApplyReporter.record`` keyed on
    the outcome (see ``_CANONICAL_WRITE_OUTCOMES``), at publication or at a
    non-publishing canonical-write return. Both default None for
    non-interactive callers and direct-call tests — when either is None the
    bump machinery is a no-op (today's behavior).
    """
    reporter = reporter or _ApplyReporter(rel_path=rel_path, local_path=local_path)
    # Direct callers bypass _download_and_apply's component check, so protect
    # a leaf symlink here before mkdir or atomic_write can replace it.
    if local_path.is_symlink():
        if verbose:
            console.print(
                f"  [yellow]skipped (local symlink preserved):[/yellow] {safe_str(rel_path)}"
            )
        return "skipped"

    fsutil.refuse_real_state_write_under_pytest(local_path)
    try:
        if not local_path.parent.is_dir():
            ancestor = _first_existing_ancestor(local_path.parent)
            if not ancestor.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(ancestor))
            # Create one component at a time so a later failure still
            # registers every directory that landed (including the parent
            # of the topmost new ancestor) for fsync.
            missing: list[Path] = []
            current = local_path.parent
            while current != ancestor:
                missing.append(current)
                current = current.parent
            for directory in reversed(missing):
                directory.mkdir(exist_ok=True)
                reporter.created_ancestor(directory)
    except OSError as e:
        reporter.failed("cannot create folder", e, preserved="local preserved")
        return "failed"
    remote_mtime_iso = remote_info.get("mtime")

    if not local_path.exists():
        return _apply_write(
            local_path,
            rel_path,
            plain_data,
            verbose=verbose,
            remote_mtime_iso=remote_mtime_iso,
            reporter=reporter,
        )

    # Re-read local state. Precomputed snapshot can be stale if the user
    # edited the file after _pull_core built its diff.
    try:
        local_hash = hash_file(local_path)
    except (PermissionError, OSError) as e:
        reporter.failed("read failed", e, preserved="local preserved")
        return "failed"

    if local_hash == remote_info.get("sha256"):
        return "unchanged"

    if should_merge(rel_path):
        return _apply_merge(local_path, rel_path, plain_data, verbose=verbose, reporter=reporter)

    # [S] local is newer. Keep local at canonical path \u2014 next push propagates it.
    remote_mtime_str = remote_mtime_iso
    local_mtime: datetime | None = None
    remote_mtime: datetime | None = None
    local_newer = False
    try:
        local_mtime = mtime_from_path(local_path)
        if remote_mtime_str:
            remote_mtime = mtime_from_manifest(remote_mtime_str)
        local_newer = (
            local_mtime is not None and remote_mtime is not None and local_mtime > remote_mtime
        )
    except (TypeError, ValueError, OverflowError, OSError) as e:
        # Malformed mtime or filesystem error: fall through to conflict path.
        with core._pause_sync_progress():
            console.print(
                "  [yellow]timestamp could not be interpreted; treating as a "
                f"conflict:[/yellow] {safe_str(rel_path)} \u2014 {safe_str(e)}"
            )
        local_mtime = None
        remote_mtime = None
    if local_newer:
        if verbose:
            try:
                console.print(f"  [dim]= {safe_str(rel_path)} (local newer, kept)[/dim]")
            except (OSError, SystemExit):
                pass
        return "skipped"

    # [C] conflict path. Optionally prompt the user; default keep-both.
    if interactive_resolve:
        # Resolve the remote_device_id against the cached devices list
        # so the inline banner shows "(from <peer_name>)" rather than
        # "(unknown peer)" when attribution is available. Falls back
        # cleanly when devices is None (unit tests, library callers).
        peer_name: str | None = None
        ambiguous_count = 0
        if devices:
            short = remote_device_id[:8]
            match, count = lookup_device_by_short_id(devices, short)
            if match is not None:
                peer_name = match.get("device_name")
            elif count > 1:
                ambiguous_count = count
        choice, merged_bytes = _prompt_conflict_choice(
            rel_path,
            local_path,
            plain_data,
            peer_name,
            ambiguous_count,
            remote_mtime=remote_mtime,
        )
        if choice == "keep-canonical":
            # Post-inversion: canonical IS local, so "keep-canonical" =
            # "keep-local" — both work as user-facing labels. The internal
            # outcome stays "skipped" for back-compat with PullResult.
            #
            # Track 12A: record the decision for the end-of-pull-batch drain
            # instead of bumping canonical's mtime here. A mid-walk bump makes
            # a LATER peer's _apply_incoming_file mis-classify on the
            # `local_mtime > remote_mtime` gate and silently skip (the v0.12.6
            # revert). Deferring means every peer is judged against the same
            # original-local baseline; _drain_inline_bumps applies one bump
            # past max(peer_mtime) after the whole pull. peer_mtime is None
            # when the manifest mtime is missing/malformed (conflict path
            # still fires) -- record 0.0, mirroring the resolve-side
            # stat-failure degradation. NOTE: a peer with no parseable mtime
            # never reaches the mtime gate at all, so the bump can't close the
            # resolve->pull loop for THAT peer -- it still helps every
            # mtime-bearing peer.
            peer_ts = remote_mtime.timestamp() if remote_mtime else 0.0
            _record_inline_bump(pending_inline_bumps, resolved_local, peer_ts)
            if verbose:
                console.print(f"  [dim]= {safe_str(rel_path)} (kept local by user)[/dim]")
            return "skipped"
        if choice == "keep-remote":
            # User overrode default keep-both by picking remote \u2014 overwrite
            # canonical with remote bytes. Same I/O as _apply_write but with
            # a different verbose string ("remote kept by user").
            try:
                fsutil.atomic_write_bytes(local_path, plain_data, fsync=False)
            except (OSError, StorageError) as e:
                reporter.failed("write failed", e, preserved="local preserved")
                return "failed"
            reporter.published("written")
            reporter.follow_up = "mtime restore"
            _restore_mtime_best_effort(local_path, remote_mtime_iso)
            reporter.follow_up = "output"
            if verbose:
                console.print(
                    f"  [yellow]\u2193[/yellow] {safe_str(rel_path)} (remote kept by user)"
                )
            return "written"
        if choice == "merge":
            # User accepted the LCS-merged result inline. Write merged
            # bytes to canonical; the sidecar was never created (we
            # suppress _apply_conflict by returning "merged-via-lcs"
            # here). Refusal of (m) when merge wasn't offered already
            # mapped to "keep-both" inside _prompt_conflict_choice.
            assert merged_bytes is not None  # invariant: choice=="merge" -> bytes
            try:
                fsutil.atomic_write_bytes(local_path, merged_bytes, fsync=False)
            except (OSError, StorageError) as e:
                reporter.failed("merge write failed", e, preserved="local preserved")
                return "failed"
            reporter.published("merged-via-lcs")
            reporter.follow_up = "output"
            if verbose:
                console.print(f"  [cyan]merged[/cyan] {safe_str(rel_path)} (LCS)")
            return "merged-via-lcs"
        if choice == "abort":
            raise typer.Abort()
        # choice == "keep-both" -> fall through to _apply_conflict, returns
        # "conflicted" on success. _ApplyReporter.record invalidates any
        # prior keep-canonical bump for this path on that outcome.

    return _apply_conflict(
        local_path,
        rel_path,
        plain_data,
        remote_device_id,
        verbose=verbose,
        remote_mtime_iso=remote_mtime_iso,
        reporter=reporter,
    )


def _download_and_apply(
    backend: LocalBackend,
    base_path: Path,
    to_download: dict[str, dict],
    source_device_id: str,
    passphrase: str,
    memory_kb: int,
    interactive_resolve: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    devices: list[dict[str, Any]] | None = None,
    pending_inline_bumps: dict[Path, float] | None = None,
    *,
    reporter: _ApplyReporter | None = None,
) -> tuple[int, dict[ApplyOutcome, list[str]]]:
    """Download blobs and dispatch each to _apply_incoming_file.

    Returns (encrypted_bytes_transferred, outcomes_by_path).
    outcomes_by_path groups rel_paths by outcome so callers can report
    per-outcome totals and write accurate sync logs.

    ``pending_inline_bumps`` is the shared Track 12A accumulator.
    ``_ApplyReporter.record`` owns INVALIDATION: it pops on publication, or
    on a non-publishing return in ``_CANONICAL_WRITE_OUTCOMES`` such as
    sidecar dedup. RECORD stays inside ``_apply_incoming_file``'s
    keep-canonical branch. Both halves no-op when ``pending_inline_bumps``
    is None (non-interactive pulls).

    Progress display (Track 5B Task 4, plus the sync spinner):
      - quiet=True: no progress output; apply failures still warn on stderr.
      - Non-verbose attended pull inside `_sync_outcome`: `_SyncProgress`
        renders the transient spinner and Applying-files counter; the widget
        below stays off (`show_progress` requires verbose or no sync scope).
      - TTY + not quiet (verbose/direct callers): Rich Progress widget
        renders bar + count + elapsed.
        First-pull-on-new-Mac case (the 2026-04-24 first-pull session): per-file
        backend.get(bkey) blocks on iCloud placeholder materialization;
        before this, the entire 286-file / 263s pull was indistinguishable
        from a hung process. Progress only updates BETWEEN files (a single
        blocking .get() does not yield), so this surfaces stalls at
        per-file boundaries — not intra-file.
      - non-TTY + not quiet: skip the rewriting widget (line-rewriting
        garbles log capture); a one-time start banner keeps scripted
        callers informed without spamming.
      - to_download empty: skip the widget entirely (Rich Progress with
        total=0 risks empty-bar / div-by-zero rendering).
    """
    reporter = reporter or _ApplyReporter(
        outcomes=_empty_outcomes(), pending_inline_bumps=pending_inline_bumps
    )
    assert reporter.outcomes is not None
    outcomes = reporter.outcomes

    total = len(to_download)
    show_progress = (
        bool(to_download)
        and not quiet
        and (core._SYNC_OUTPUT.get() is None or core._SYNC_OUTPUT.get()["verbose"])
    )
    use_widget = show_progress and console.is_terminal

    progress: Progress | None = None
    task_id: int | None = None
    if use_widget:
        progress = Progress(
            TextColumn("  [bold]downloading[/bold]"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
            redirect_stderr=False,
        )
        progress.start()
        task_id = progress.add_task("download", total=total)
    elif show_progress:
        # Non-TTY: emit a single start banner. Per-file lines would spam
        # logs; the totals line at the end of pull surfaces the result.
        console.print(f"  downloading {total} file(s)...")
    if total > 0 and not quiet:
        core._sync_progress_start_batch("Applying files", total, "files")

    def _advance() -> None:
        if progress is not None and task_id is not None:
            progress.advance(task_id)
        if not quiet:
            core._sync_progress_advance()

    # Quiet-mode contract: per-file decorations skipped in quiet (autopull);
    # the summary routes per-source totals to stderr in _print_pull_summary
    # (D11). Without this gate, autopull leaked per-file lines to stdout on
    # bad-blob/decrypt errors. (codex /review v0.9.0 caught the gap.)
    try:
        for rel_path, info in to_download.items():
            try:
                bkey = blob_key(source_device_id, info.get("sha256", ""))
            except ValueError as e:
                # Malicious or corrupt manifest shipped a sha256 with path
                # separators / parent-dir refs / null bytes / empty. Per-file
                # isolation: fail this file, keep walking — matches the
                # v0.8.1 empty-device_id handling in _apply_conflict.
                if not quiet:
                    with core._pause_sync_progress():
                        console.print(
                            f"  [red]bad blob key (local preserved):[/red] "
                            f"{safe_str(rel_path)} \u2014 {safe_str(e)}"
                        )
                outcomes["failed"].append(rel_path)
                _advance()
                continue
            try:
                enc_data = backend.get(bkey)
            except StorageNotFound:
                # The sync tool has not delivered this blob yet (Syncthing
                # sends manifests and blobs independently). Waiting, not a
                # failure: nothing was touched and the next pull retries it.
                outcomes.setdefault("waiting", []).append(rel_path)
                _advance()
                continue
            except MindMeldError:
                if verbose and not quiet:
                    with core._pause_sync_progress():
                        console.print(f"  [yellow]blob missing: {safe_str(bkey)}[/yellow]")
                outcomes["failed"].append(rel_path)
                _advance()
                continue

            try:
                plain_data = decrypt(enc_data, passphrase, memory_kb)
            except CryptoError as e:
                if isinstance(e, NewerFormatError):
                    _warn_apply_failure(
                        reporter.device_name,
                        reporter.src_name,
                        rel_path,
                        base_path / rel_path,
                        "decrypt failed",
                        e,
                        preserved="local preserved",
                    )
                elif not quiet:
                    with core._pause_sync_progress():
                        console.print(
                            "  [red]decrypt failed:[/red] "
                            f"{safe_str(rel_path)} \u2014 {safe_str(e)}"
                        )
                outcomes["failed"].append(rel_path)
                _advance()
                continue

            reporter.bytes_transferred += len(enc_data)
            local_path = base_path / rel_path

            # Reject a local link at the destination or in a child component
            # before resolving containment. The root itself may be symlinked,
            # but following a link below it would either escape the source or
            # let atomic_write_bytes replace local routing with peer content.
            if pullplan._has_symlinked_component(local_path, base_path):
                if not quiet:
                    with core._pause_sync_progress():
                        console.print(
                            f"  [yellow]skipped (local symlink preserved):[/yellow] "
                            f"{safe_str(rel_path)}"
                        )
                outcomes["skipped"].append(rel_path)
                _advance()
                continue

            # Belt-and-braces: load_manifest already rejects rel_paths
            # that could escape via '..' / absolute / null-byte (see
            # manifest._validate_rel_path). Re-check the FULLY-RESOLVED
            # path here so any future load path that bypasses load_manifest
            # (test fixtures, legacy on-disk caches) still cannot direct
            # writes outside the source root. resolve(strict=False) handles
            # not-yet-created files; matching .resolve() on both sides
            # normalizes symlinks consistently so legit symlinked source
            # roots don't false-positive.
            try:
                resolved_local = local_path.resolve(strict=False)
                resolved_base = base_path.resolve(strict=False)
                _path_inside_base = resolved_local.is_relative_to(resolved_base)
            except (OSError, ValueError):
                _path_inside_base = False
            if not _path_inside_base:
                if not quiet:
                    with core._pause_sync_progress():
                        console.print(
                            "  [red]rejected (would escape source root):[/red] "
                            f"{safe_str(rel_path)}"
                        )
                outcomes["failed"].append(rel_path)
                _advance()
                continue

            advertised = info.get("sha256")
            actual = hashlib.sha256(plain_data).hexdigest()
            if not isinstance(advertised, str) or actual != advertised:
                if not quiet:
                    with core._pause_sync_progress():
                        console.print(
                            f"  [red]content check failed:[/red] {safe_str(rel_path)} "
                            "does not match the sending snapshot. Local file kept. "
                            f"see {SNAPSHOT_FAILURES_URL}."
                        )
                outcomes["failed"].append(rel_path)
                _advance()
                continue

            reporter.begin(rel_path, local_path, resolved_local)
            try:
                outcome = _apply_incoming_file(
                    local_path=local_path,
                    rel_path=rel_path,
                    plain_data=plain_data,
                    remote_info=info,
                    remote_device_id=source_device_id,
                    interactive_resolve=interactive_resolve,
                    verbose=verbose and not quiet,
                    devices=devices,
                    pending_inline_bumps=pending_inline_bumps,
                    resolved_local=resolved_local,
                    reporter=reporter,
                )
            except (OSError, MindMeldError) as e:
                if reporter.recorded is None:
                    reporter.failed("apply failed", e, preserved="local preserved")
                else:
                    reporter.follow_up_failed(e)
            else:
                # Also checks publication/return agreement. A mismatch is a
                # programming error, never a contained per-file failure.
                reporter.record(outcome)
            _advance()
    finally:
        # progress.stop() in its own try/except so a Rich render failure
        # at teardown does not mask the original exception from the loop.
        # (codex /review v0.9.0)
        if progress is not None:
            try:
                progress.stop()
            except (Exception, SystemExit):
                pass

    return reporter.bytes_transferred, outcomes


# ── pull ──────────────────────────────────────────────────────────────


ConflictMode = Literal["prompt", "keep-both", "fail"]


@dataclass
class _CorruptPeer:
    device_id: str
    device_name: str
    # This Mac's own manifest vanished mid-read: its deletion records were not
    # applied, so it is reported like a corrupt manifest, never as waiting.
    own_vanished: bool = False
    # Track 90A: the reserved byte of a manifest from a newer mm. This release
    # names the remedy for a format it cannot read instead of "corrupt".
    newer_version: int | None = None


@dataclass
class _FsyncWarning:
    parent_dir: Path
    error: str


@dataclass
class _UnknownSourceWarning:
    src_name: str
    device_name: str


@dataclass
class _PerSourceResult:
    """Result of pulling one source from one peer.

    Helpers return this; the main loop aggregates into PullResult and
    passes the full list to _print_pull_summary for per-source line
    rendering.
    """

    src_name: str
    device_name: str
    device_id: str
    outcomes: dict[ApplyOutcome, list[str]]
    bytes_transferred: int
    touched_parents: set[Path]
    predictions: list[pullplan.PullPrediction] = field(default_factory=list)
    # Set when src_cfg["type"] == "claude" to trigger write_sync_log in the
    # caller. Keyed off type (not name) so a user renaming their claude
    # source to "my-claude" still gets sync-log entries written — otherwise
    # the rename silently breaks project-level activity logging.
    #
    # Scope: this fix covers SAME-DEVICE rename only. Manifests are still
    # keyed by src_name (see manifest.py), so if device A renames "claude"
    # → "work-claude" but device B keeps "claude", B's pull skips A's
    # remote source entirely (unknown-source warning path). Cross-device
    # rename drift is a bigger design question tracked as a known
    # limitation; this flag just makes sure single-device rename doesn't
    # silently break the per-project sync log.
    claude_sync_base: str | None = None
    # Recovery may resume this helper after its output raises. Each forensic
    # write is attempted once; replaying the whole block would duplicate rows.
    history_recorded: set[tuple[str, str]] = field(default_factory=set, repr=False)
    sync_log_attempted: bool = field(default=False, repr=False)

    @property
    def had_changes(self) -> bool:
        """True if ANY file was PROCESSED (written/merged/skipped/conflicted/failed/waiting).

        Excludes "unchanged" deliberately. "unchanged" means the apply-time
        re-read matched remote — nothing happened. Treating unchanged-only as
        "had changes" would trigger _cleanup_conflict_copies, which deletes
        valid iCloud conflict copies of the remote manifest. If we recovered
        from a corrupt canonical manifest via a valid conflict copy, that
        deletion leaves only the corrupt canonical → permanent corruption.

        Includes skipped/failed intentionally: one-way-sync setups (always
        local-newer = all skipped) still need cleanup to run or iCloud dup
        files accumulate forever. Includes waiting so a device whose blobs
        have not arrived still reaches accounting; `_pull_core` then holds
        the cleanup back for it (a kept conflict copy may be the only holder
        of the manifest those blobs belong to).
        """
        return any(self.outcomes[k] for k in self.outcomes if k != "unchanged")


def _check_fleet_version_or_refuse(
    backend: LocalBackend,
    my_device_id: str,
    *,
    refusal_suffix: str = "",
    inventory: DeviceInventory | None = None,
) -> None:
    """Refuse pull if any peer's last_seen_version is pre-v0.9.2 OR the
    peer's device.json is corrupt/shape-invalid.

    Track 5E (v0.9.2 BREAKING): the conflict-direction inversion ships
    incompatibly. A pre-v0.9.2 peer pushing now would produce conflict
    files under the OLD direction (canonical = remote, sidecar = local),
    but we dispatch by filename prefix (`v0-` = pre-inversion, no prefix
    = post-inversion) and a peer's just-produced (un-prefixed) old-
    direction file would be silently mis-resolved as post-inversion.

    Per-peer classification:
      * safe — last_seen_version is parseable and >= INVERSION_MIN_VERSION.
      * inactive — last_seen ALSO missing (registered, never pushed). ALLOW
        because the peer has no conflict files for us to misinterpret.
      * pre-v0.9.2 — last_seen present but last_seen_version missing OR
        parses to a version < INVERSION_MIN_VERSION. REFUSE.
      * dropped — device.json corrupt/shape-invalid. REFUSE by storage
        key — we can't read its version, so we can't trust its conflict
        files (codex-2 #3).

    Refusal exits non-zero via `_error` BEFORE any pull I/O. Implementation
    reads the silent device inventory (one read per pull, shared with
    `_select_devices`; Track 87A). Only key-named entries count: a conflict
    copy or misnamed file is foreign, and a key-named entry that vanished
    mid-read is waiting, not corrupt. Neither refuses.
    """
    if inventory is None:
        inventory = inventory_devices(backend)
    valid, dropped = inventory.valid, inventory.dropped
    refusals: list[str] = []

    for key, reason in dropped:
        refusals.append(
            f"  storage key {key} — corrupt device.json ({reason}); can't read last_seen_version"
        )

    try:
        threshold = Version(INVERSION_MIN_VERSION)
    except InvalidVersion:
        # Defensive — INVERSION_MIN_VERSION is hardcoded.
        return

    for d in valid:
        did = d.get("device_id")
        if did == my_device_id:
            continue
        if not d.get("last_seen"):
            # Inactive peer (registered, never pushed). No conflict files
            # exist on storage from this peer — safe to ignore.
            continue
        version_str = d.get("last_seen_version")
        # device_name, did, version_str are peer-controlled — sanitize for
        # the refusal message that flows through Rich console.print.
        safe_dname = safe_str(d.get("device_name", "?"))
        safe_did = safe_str(did)
        if not isinstance(version_str, str) or not version_str:
            refusals.append(
                f"  device {safe_dname} ({safe_did}) — "
                f"last_seen_version missing (last push was on a pre-"
                f"v{INVERSION_MIN_VERSION} mm)"
            )
            continue
        try:
            peer_version = Version(version_str)
        except InvalidVersion:
            refusals.append(
                f"  device {safe_dname} ({safe_did}) — "
                f"last_seen_version={safe_str(version_str)!r} is malformed"
            )
            continue
        if peer_version < threshold:
            refusals.append(
                f"  device {safe_dname} ({safe_did}) — "
                f"last_seen_version={safe_str(version_str)} < {INVERSION_MIN_VERSION}"
            )

    if refusals:
        core._error(
            f"Mixed-version fleet detected. v{INVERSION_MIN_VERSION} "
            f"inverted the conflict-file direction (canonical = local, "
            f"sidecar = remote); a pre-v{INVERSION_MIN_VERSION} peer "
            f"pushing now would produce conflict files under the OLD "
            f"semantics that this puller can't safely dispatch.\n\n"
            f"Update the following peer(s) to v{INVERSION_MIN_VERSION} "
            f"and have them push at least once before pulling here:\n"
            + "\n".join(refusals)
            + "\n\nRun `mm devices` for the version table. "
            "Last-resort recovery: hand-edit device.json to add "
            f'"last_seen_version": "{INVERSION_MIN_VERSION}"' + " — only after "
            "verifying the peer is actually upgraded." + refusal_suffix
        )


def _select_devices(
    backend: LocalBackend,
    my_device_id: str,
    from_device: str | None,
    inventory: DeviceInventory | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return (all_devices, pull_targets).

    Single call to _list_devices_warn — dedups the double-call bug
    (cli.py:1692 + 1709 pre-decomp) that emitted each dropped-device
    warning twice per pull. One warning per dropped peer per pull is
    the correct semantic.

    `all_devices` is used for tombstone collection (every peer's
    manifest feeds tombstones; we don't pull data from ourselves but
    we DO read our own tombstones via collect_tombstones).

    `pull_targets` is filtered: only the peer `from_device` matches, or
    all peers excluding self.
    """
    if inventory is None:
        all_devices = core._list_devices_warn(backend)
    else:
        # The pull's single inventory: dropped entries already refused at the
        # gate, so nothing is left to warn about here.
        all_devices = inventory.valid
    if from_device:
        pull_targets = [d for d in all_devices if d["device_id"] == from_device]
    else:
        pull_targets = [d for d in all_devices if d["device_id"] != my_device_id]
    return all_devices, pull_targets


def _prefetch_manifests(
    backend: LocalBackend,
    devices: list[dict],
    passphrase: str,
    memory_kb: int,
    vanished_out: list[_CorruptPeer] | None = None,
) -> tuple[dict[str, dict | None], list[_CorruptPeer]]:
    """Fetch every device's manifest. Missing/corrupt both map to None.

    Returns (cache, corrupt_peers). Callers route corrupt_peers to stderr
    via _print_pull_summary (load-bearing signal: silent skip = partial
    pull that looks like a successful pull). A manifest that vanished
    mid-read (a sync tool replacing it; Track 87A) is missing, not corrupt:
    it is appended to `vanished_out` when given so the pull can report it as
    waiting.
    """
    cache: dict[str, dict | None] = {}
    corrupt: list[_CorruptPeer] = []
    if devices:
        core._sync_progress_start_batch("Reading peer manifests", len(devices), "manifests")
    for d in devices:
        did = d["device_id"]
        peer_fetch = core._fetch_remote_manifest(backend, did, passphrase, memory_kb)
        if peer_fetch.status == "corrupt":
            corrupt.append(
                _CorruptPeer(
                    device_id=did,
                    device_name=d.get("device_name", did),
                    newer_version=peer_fetch.newer_version,
                )
            )
        elif peer_fetch.status == "missing" and peer_fetch.vanished and vanished_out is not None:
            vanished_out.append(_CorruptPeer(device_id=did, device_name=d.get("device_name", did)))
        cache[did] = peer_fetch.manifest if peer_fetch.is_ok else None
        core._sync_progress_advance()
    return cache, corrupt


def _plan_pull(
    pull_targets: list[dict],
    manifest_cache: dict,
    local_sources_map: dict,
    source_filter: str | None,
    all_tombstones: dict,
) -> tuple[pullplan.PullPlanner, list[_UnknownSourceWarning]]:
    planner = pullplan.PullPlanner()
    unknown = []
    for device in pull_targets:
        did, dname = device["device_id"], device["device_name"]
        remote = manifest_cache.get(did)
        if remote is None:
            continue
        for src_name, src_data in remote.get("sources", {}).items():
            if source_filter and src_name != source_filter:
                continue
            if src_name not in local_sources_map:
                unknown.append(_UnknownSourceWarning(src_name, dname))
                continue
            for path, info in src_data.get("files", {}).items():
                if not is_tombstoned(src_name, path, all_tombstones):
                    planner.predict(
                        did, dname, src_name, path, info, local_sources_map[src_name]["path"]
                    )
    return planner, unknown


_FAIL_MODE_REFUSALS = frozenset({"conflict", "may fail"})


def _fail_mode_refusals(
    predictions: list[pullplan.PullPrediction],
) -> list[pullplan.PullPrediction]:
    return [p for p in predictions if p.outcome in _FAIL_MODE_REFUSALS]


def _preflight_conflicts(
    pull_targets: list[dict],
    manifest_cache: dict,
    local_sources_map: dict,
    source_filter: str | None,
    all_tombstones: dict,
) -> list[pullplan.PullPrediction]:
    planner, _ = _plan_pull(
        pull_targets, manifest_cache, local_sources_map, source_filter, all_tombstones
    )
    return _fail_mode_refusals(planner.predictions)


_UNAPPLIED = frozenset({"unchanged", "waiting"})


def _empty_outcomes() -> dict[ApplyOutcome, list[str]]:
    return {
        "written": [],
        "merged": [],
        "merged-via-lcs": [],
        "skipped": [],
        "conflicted": [],
        "unchanged": [],
        "failed": [],
        "waiting": [],
    }


def _pull_one_source(
    backend: LocalBackend,
    *,
    src_name: str,
    src_type: str,
    src_data: dict,
    did: str,
    dname: str,
    base_path: Path,
    all_tombstones: dict[str, dict[str, str]],
    passphrase: str,
    memory_kb: int,
    interactive_resolve: bool,
    dry_run: bool,
    verbose_console: bool,
    quiet: bool = False,
    devices: list[dict[str, Any]] | None = None,
    pending_inline_bumps: dict[Path, float] | None = None,
    reporter: _ApplyReporter | None = None,
    predictions: list[pullplan.PullPrediction] | None = None,
) -> _PerSourceResult:
    """Pull one source from one peer. Returns _PerSourceResult.

    `src_type` is the source's `type` field from the local config. Used
    to gate sync-log behavior: only claude-type sources get a per-project
    `.mind-meld-log.md`. Passing type explicitly (rather than re-deriving
    from name) lets users rename the claude source without losing sync-log.

    `verbose_console` is `(verbose and not quiet)` — controls per-file
    console output inside _download_and_apply.

    `quiet` is the autopull flag — gates the Track 5B Task 4 download
    progress widget (silent in autopull, visible otherwise).
    """
    reporter = reporter or _ApplyReporter(
        device_name=dname,
        src_name=src_name,
        outcomes=_empty_outcomes(),
        pending_inline_bumps=pending_inline_bumps,
    )
    assert reporter.outcomes is not None
    remote_files = src_data.get("files", {})
    base_result = _PerSourceResult(
        src_name=src_name,
        device_name=dname,
        device_id=did,
        outcomes=reporter.outcomes,
        bytes_transferred=0,
        touched_parents=reporter.touched_parents,
        claude_sync_base=str(base_path) if src_type == "claude" else None,
    )
    if dry_run:
        if predictions is None:
            planner = pullplan.PullPlanner()
            predictions = [
                planner.predict(did, dname, src_name, path, info, base_path)
                for path, info in remote_files.items()
                if not is_tombstoned(src_name, path, all_tombstones)
            ]
        base_result.predictions = predictions
        return base_result
    if not remote_files:
        return base_result

    # Build local state for diff. mtime is re-read at apply time so it
    # reflects what the file actually looks like when we act on it.
    local_files: dict[str, dict] = {}
    for rel_path in remote_files:
        local_path = base_path / rel_path
        try:
            if local_path.exists():
                sha = hash_file(local_path)
                local_files[rel_path] = {"sha256": sha}
        except (PermissionError, OSError):
            pass

    # Arg-swap: this is the additive pull path. See diff_files docstring
    # — `new`/`modified` are files to download; `deleted` is ignored.
    diff = diff_files(remote_files, local_files)

    to_download = {**diff.new, **diff.modified}
    to_download = {
        path: info
        for path, info in to_download.items()
        if not is_tombstoned(src_name, path, all_tombstones)
    }
    if not to_download:
        return base_result

    if src_name in MM_INTERNAL_SOURCE_NAMES:
        try:
            _config_module._bootstrap_mm_events_path(
                str(base_path), strict=True, on_created=reporter.created_ancestor
            )
        except SnapshotError as e:
            core._print_apply_warning(f"mm: warning: {safety.safe_terminal_str(e)}")
            reporter.outcomes["failed"].extend(to_download)
            return base_result

    bt, outcomes = _download_and_apply(
        backend,
        base_path,
        to_download,
        did,
        passphrase,
        memory_kb,
        interactive_resolve=interactive_resolve,
        verbose=verbose_console,
        quiet=quiet,
        devices=devices,
        pending_inline_bumps=pending_inline_bumps,
        reporter=reporter,
    )

    return _PerSourceResult(
        src_name=src_name,
        device_name=dname,
        device_id=did,
        outcomes=outcomes,
        bytes_transferred=bt,
        touched_parents=reporter.touched_parents,
        claude_sync_base=str(base_path) if src_type == "claude" else None,
    )


def _record_source_bookkeeping(
    per_source: _PerSourceResult, src_data: dict, *, quiet: bool, verbose: bool
) -> None:
    """Attempt history and sync-log writes once, including during recovery."""
    # Log per-file outcomes for `mm log` audit trail. "unchanged"
    # is intentionally omitted — it represents apply-time
    # convergence (no I/O), and logging it would dwarf the
    # forensic signal in the file. All other outcomes ARE
    # I/O events worth recording.
    for action_key in (
        "written",
        "merged",
        "merged-via-lcs",
        "skipped",
        "conflicted",
        "failed",
    ):
        for rel_path in per_source.outcomes.get(action_key, []):
            key = (action_key, rel_path)
            if key in per_source.history_recorded:
                continue
            per_source.history_recorded.add(key)
            remote_info = src_data.get("files", {}).get(rel_path, {})
            pullhistory.append(
                verb="pull",
                device=per_source.device_id,
                source=per_source.src_name,
                rel_path=rel_path,
                action=action_key,
                remote_sha=remote_info.get("sha256"),
            )

    # Claude sync log is best-effort: log file is cosmetic
    # signal for Claude Code, losing it on error is harmless.
    # Swallowing the exception here protects the accumulated
    # corrupt-peer / unknown-source warnings from being lost
    # if write_sync_log raises.
    if per_source.claude_sync_base is not None and not per_source.sync_log_attempted:
        per_source.sync_log_attempted = True
        try:
            logs = write_sync_log(
                claude_base=per_source.claude_sync_base,
                device_name=per_source.device_name,
                device_id=per_source.device_id,
                new_files=per_source.outcomes["written"],
                modified_files=(
                    per_source.outcomes["merged"] + per_source.outcomes["merged-via-lcs"]
                ),
                deleted_files=[],
                conflicted_files=per_source.outcomes["conflicted"],
                skipped_files=per_source.outcomes["skipped"],
            )
        except (OSError, StorageError) as e:
            msg = f"sync log write failed: {e}"
            with core._pause_sync_progress():
                if quiet:
                    print(f"mm: warning: {msg}", file=sys.stderr)
                else:
                    console.print(f"  [yellow]warning:[/yellow] {msg}")
        else:
            if verbose and not quiet and logs:
                for log in logs:
                    console.print(f"  [dim]wrote sync log: {log}[/dim]")


def _fsync_touched_parents(touched_parents: set[Path]) -> list[_FsyncWarning]:
    """Deferred-durability commit: fsync each unique parent directory.

    A failure means some of this pull's renames may be non-durable —
    caller surfaces to stderr but does not roll back (files are already
    in place; a subsequent pull will simply re-apply if needed).
    """
    warnings: list[_FsyncWarning] = []
    for parent_dir in sorted(touched_parents):
        try:
            fsutil.fsync_dir(parent_dir)
        except (OSError, StorageError) as e:
            # Directory close can raise outside fsync_dir's StorageError
            # wrapper. Recovery must still record completed publications.
            warnings.append(_FsyncWarning(parent_dir=parent_dir, error=str(e)))
    return warnings


def _waiting_peers(
    per_source_results: list[_PerSourceResult],
    vanished_manifests: list[_CorruptPeer],
    vanished_entries: list[str],
) -> list[WaitingPeer]:
    """Per-peer waiting facts for `PullResult.waiting`, in first-seen order."""
    by_device: dict[str, WaitingPeer] = {}
    for r in per_source_results:
        if n := len(r.outcomes.get("waiting", ())):
            peer = by_device.setdefault(r.device_id, WaitingPeer(r.device_id, r.device_name))
            peer.files += n
    waiting = list(by_device.values())
    waiting.extend(
        WaitingPeer(p.device_id, p.device_name, manifest=True) for p in vanished_manifests
    )
    waiting.extend(WaitingPeer(i, i, entry=True) for i in vanished_entries)
    return waiting


def _waiting_items(result: PullResult, *, detail: bool) -> list[str]:
    items = []
    for w in result.waiting:
        name = safety.safe_terminal_str(w.device_name)
        if w.entry:
            items.append(f"{name}'s device entry" + (" waiting" if detail else ""))
        elif w.manifest:
            items.append(f"{name}'s manifest" + (" waiting" if detail else ""))
        else:
            files = core._count(w.files, "file", "files")
            items.append(f"{files} waiting from {name}" if detail else f"{files} from {name}")
    return items


def waiting_clause(result: PullResult) -> str:
    """The outcome-line clause for undelivered content, or "" when none:
    ``waiting for 2 files from Mac B, Mac C's manifest``. Peer text goes
    through `safety`; nothing here is a failure."""
    items = _waiting_items(result, detail=False)
    return "waiting for " + ", ".join(items) if items else ""


def waiting_detail(result: PullResult) -> str:
    """The autopull breadcrumb detail for the same facts (``2 files waiting
    from Mac B``), or "" when none."""
    return "; ".join(_waiting_items(result, detail=True))


def _old_form_text(result: PullResult) -> str:
    peers = result.old_form_peers
    if not peers:
        return ""
    items = [
        f"{core._count(p.files, 'file', 'files')} from {safety.safe_terminal_str(p.device_name)}"
        for p in peers
    ]
    who = "it publishes" if len(peers) == 1 else "they publish"
    return f"{', '.join(items)} wait until {who} shared names"


def old_form_clause(result: PullResult) -> str:
    """The outcome-line clause for another home's files from a peer still on path
    names, or "" when none: ``312 files from Mac B wait until it publishes shared
    names (mm status)``. Built apart from `waiting_clause`: it has no ``waiting
    for`` prefix and no re-pull remedy, because it waits on a person."""
    text = _old_form_text(result)
    return f"{text} (mm status)" if text else ""


def old_form_detail(result: PullResult) -> str:
    """The autopull breadcrumb detail for the same facts, without the pointer."""
    return _old_form_text(result)


def not_placed_line(result: PullResult) -> str:
    """The ``Not placed here`` line for projects whose checkout is not on this
    machine, or "" when none (a join adds it once)."""
    if not result.not_placed_projects:
        return ""
    projects = core._count(result.not_placed_projects, "project", "projects")
    return (
        f"Not placed here: {core._count(result.not_placed_files, 'file', 'files')} in "
        f"{projects} with no known checkout (list: mm pull --dry-run --verbose)."
    )


def _print_preflight_conflicts(predicted: list[pullplan.PullPrediction], quiet: bool) -> None:
    """Print predicted fail-mode refusals before --conflict-mode=fail raises.

    Quiet (autopull): one-liner per conflict or local failure to stderr.
    Non-quiet: rich console with resolution hint.
    """
    # src_name, rel_path, device_name are all peer-controlled — sanitize.
    state = core._SYNC_OUTPUT.get()
    if state is not None:
        state["preflight"] = "; ".join(p.label for p in predicted[:3])
        state["preflight_failures"] = any(p.outcome == "may fail" for p in predicted)
        # Which files would conflict is the "what broke" part of the refusal.
        for p in predicted:
            core._sync_warning(
                f"mm: {safety.safe_terminal_str(p.label)} "
                f"{safety.safe_terminal_str(p.src_name)}/"
                f"{safety.safe_terminal_str(p.rel_path)} "
                f"(from {safety.safe_terminal_str(p.device_name)})"
            )
        return
    if quiet:
        for p in predicted:
            print(
                f"mm: {safety.safe_terminal_str(p.label)} "
                f"{safety.safe_terminal_str(p.src_name)}/"
                f"{safety.safe_terminal_str(p.rel_path)} "
                f"(from {safety.safe_terminal_str(p.device_name)})",
                file=sys.stderr,
            )
        return
    console.print(f"[red]Pull refused:[/red] {len(predicted)} file(s) would conflict or may fail.")
    for p in predicted:
        console.print(
            f"  [yellow]! {safe_str(p.label)}[/yellow] {safe_str(p.src_name)}/"
            f"{safe_str(p.rel_path)} (from {safe_str(p.device_name)})"
        )
    console.print(
        "\nResolve conflicts locally, or re-run with --conflict-mode keep-both to auto-rename."
    )


# Inline-path display cap for the non-verbose pull summary. 20 was picked
# from the 2026-04-24 first-pull session (286-file pull / 6 conflicts):
# enough to surface a typical conflict batch without spamming, and short
# enough that --verbose still feels distinct.
_INLINE_PATH_CAP = 20


def _format_inline_paths(paths: list[str], *, verbose: bool, sep: str) -> str:
    """Format an inline path list for the quiet-mode stderr summary.

    Caps to _INLINE_PATH_CAP unless verbose. Cap overflow renders as
    "(and N more)" suffix so users know they're seeing a slice.
    """
    safe_paths = [safety.safe_terminal_str(p) for p in paths]
    if verbose or len(safe_paths) <= _INLINE_PATH_CAP:
        return sep.join(safe_paths)
    shown = sep.join(safe_paths[:_INLINE_PATH_CAP])
    return f"{shown} (and {len(safe_paths) - _INLINE_PATH_CAP} more)"


def _print_inline_paths(paths: list[str], *, verbose: bool, color: str) -> None:
    """Render an inline path list under a non-quiet per-source summary line.

    Uses 4-space indent so the device→source→file hierarchy stays visible
    when multiple devices share a source name. Caps to _INLINE_PATH_CAP
    unless --verbose; overflow renders as a dim "... and N more" line.
    """
    cap = len(paths) if verbose else min(_INLINE_PATH_CAP, len(paths))
    for p in paths[:cap]:
        console.print(f"    [{color}]- {safe_str(p)}[/{color}]")
    if not verbose and len(paths) > cap:
        console.print(f"    [dim]... and {len(paths) - cap} more[/dim]")


def _conflict_review_notice(result: PullResult) -> None:
    """Attended pulls keep the review/resolve remedy before their one outcome line."""
    if result.total_conflicted:
        core._sync_warning(
            f"mm: {core._count(result.total_conflicted, 'conflict copy', 'conflict copies')} kept; "
            "run mm conflicts to review, mm resolve to pick a winner"
        )


def _print_pull_summary(
    result: PullResult,
    corrupt_peers: list[_CorruptPeer],
    unknown_sources: list[_UnknownSourceWarning],
    fsync_warnings: list[_FsyncWarning],
    per_source_results: list[_PerSourceResult],
    quiet: bool,
    verbose: bool,
    *,
    dry_run: bool = False,
    interruption: Literal["interrupted", "aborted"] | None = None,
) -> None:
    """Single I/O owner for pull output.

    Load-bearing warnings (corrupt peers, unknown sources, fsync
    failures, per-source conflicts/failures) ALWAYS to stderr — they
    survive quiet mode because silent suppression would mask
    data-at-risk conditions (see docs/invariants/sync.md "Load-bearing warnings").

    Cosmetic summary (totals, bytes transferred, elapsed, per-source
    verbose lines) goes to console only when !quiet.
    """
    # Load-bearing: corrupt peers (silent skip = partial pull masquerading
    # as successful pull). device_name and device_id are peer-controlled —
    # sanitize before render (Group 7 preflight #1 sweep extension).
    for peer in corrupt_peers:
        if peer.own_vanished:
            text = (
                "this device's manifest ({}) vanished during the read - its deletion "
                "records were not applied, so files deleted on this Mac may have come "
                "back from peers. Check them before the next mm push."
            )
            if quiet:
                print(
                    "mm: warning: " + text.format(safety.safe_terminal_str(peer.device_id)),
                    file=sys.stderr,
                )
            else:
                console.print("[yellow]Warning:[/yellow] " + text.format(safe_str(peer.device_id)))
            continue
        if peer.newer_version is not None:
            subject = f"manifest for {peer.device_name} ({peer.device_id})"
            text = core._newer_format_message(peer.newer_version, subject)
            if quiet:
                print(f"mm: warning: {safety.safe_terminal_str(text)}", file=sys.stderr)
            else:
                console.print(f"[yellow]Warning:[/yellow] {safe_str(text)}")
            continue
        msg = (
            f"manifest for device {safe_str(peer.device_name)} "
            f"({safe_str(peer.device_id)}) "
            f"is corrupt - skipping pull from this device."
        )
        if quiet:
            print(
                "mm: warning: manifest for device "
                f"{safety.safe_terminal_str(peer.device_name)} "
                f"({safety.safe_terminal_str(peer.device_id)}) "
                "is corrupt - skipping pull from this device.",
                file=sys.stderr,
            )
        else:
            console.print(f"[yellow]Warning:[/yellow] {msg}")

    # Load-bearing: unknown sources (partition risk — rename drift or
    # missed config migration). src_name and device_name are peer-controlled.
    for unk in unknown_sources:
        msg = (
            f"skipping unknown source '{safe_str(unk.src_name)}' from "
            f"{safe_str(unk.device_name)} - not configured locally"
        )
        if quiet:
            print(
                f"mm: warning: skipping unknown source '{safety.safe_terminal_str(unk.src_name)}' "
                f"from {safety.safe_terminal_str(unk.device_name)} - not configured locally",
                file=sys.stderr,
            )
        else:
            console.print(f"  [yellow]Warning:[/yellow] {msg}")

    # Load-bearing: fsync failures (pulls non-durable).
    for w in fsync_warnings:
        msg = f"durability fsync failed on {safe_str(w.parent_dir)} — {safe_str(w.error)}"
        if quiet:
            print(
                "mm: warning: durability fsync failed on "
                f"{safety.safe_terminal_str(w.parent_dir)} — {safety.safe_terminal_str(w.error)}",
                file=sys.stderr,
            )
        else:
            console.print(f"  [yellow]warning:[/yellow] {msg}")

    if dry_run:
        return

    # Load-bearing: per-source conflicts/failures (D11 contract fix).
    # The docstring's promise that these reach stderr in quiet mode was
    # never honored before Track 5B — the early `return` below ate them.
    # Quiet path now emits one stderr line per per-source category, with
    # `<device>/<source>` prefix because the per-device header (printed
    # at the call site, not here) is suppressed in quiet mode.
    if quiet:
        for r in per_source_results:
            src_conflicted = len(r.outcomes["conflicted"])
            src_failed = len(r.outcomes["failed"])
            if src_conflicted:
                paths = _format_inline_paths(r.outcomes["conflicted"], verbose=verbose, sep=", ")
                print(
                    f"mm: warning: {safety.safe_terminal_str(r.device_name)}/"
                    f"{safety.safe_terminal_str(r.src_name)} — "
                    f"{src_conflicted} conflicts: {paths}",
                    file=sys.stderr,
                )
            if src_failed:
                paths = _format_inline_paths(r.outcomes["failed"], verbose=verbose, sep=", ")
                print(
                    f"mm: warning: {safety.safe_terminal_str(r.device_name)}/"
                    f"{safety.safe_terminal_str(r.src_name)} — {src_failed} failed: {paths}",
                    file=sys.stderr,
                )
        if core._SYNC_OUTPUT.get() is not None:
            _conflict_review_notice(result)
        return

    # Per-source lines (conflicts/failures always; verbose otherwise).
    for r in per_source_results:
        src_written = len(r.outcomes["written"])
        src_merged = len(r.outcomes["merged"]) + len(r.outcomes["merged-via-lcs"])
        src_skipped = len(r.outcomes["skipped"])
        src_conflicted = len(r.outcomes["conflicted"])
        src_failed = len(r.outcomes["failed"])
        src_waiting = len(r.outcomes.get("waiting", ()))
        if src_conflicted or src_failed or verbose:
            line = f"  [bold]{r.src_name}:[/bold]"
            if src_written:
                line += f" [green]{src_written} written[/green]"
            if src_merged:
                line += f" [cyan]{src_merged} merged[/cyan]"
            if src_skipped:
                line += f" [dim]{src_skipped} skipped (local newer)[/dim]"
            if src_conflicted:
                line += f" [yellow]{src_conflicted} conflicts[/yellow]"
            if src_failed:
                line += f" [red]{src_failed} failed[/red]"
            if src_waiting:
                line += f" [yellow]{src_waiting} waiting[/yellow]"
            console.print(line)
            # Task 2: inline conflicted/failed path list under per-source
            # line. 4-space indent keeps the device→source→file hierarchy
            # readable when multiple devices share a source name (D10).
            # --verbose unlocks the cap (D5).
            if src_conflicted:
                _print_inline_paths(r.outcomes["conflicted"], verbose=verbose, color="yellow")
            if src_failed:
                _print_inline_paths(r.outcomes["failed"], verbose=verbose, color="red")
            if src_waiting and verbose:
                _print_inline_paths(r.outcomes.get("waiting", []), verbose=verbose, color="yellow")

    if core._SYNC_OUTPUT.get() is not None:
        _conflict_review_notice(result)
        return

    # Totals.
    if interruption == "aborted":
        console.print("\n[bold yellow]Pull aborted; completed changes were kept.[/bold yellow]")
    elif interruption == "interrupted":
        console.print(
            "\n[bold yellow]Pull interrupted; completed changes were kept. "
            "Run 'mm pull' to continue.[/bold yellow]"
        )
    elif result.total_failed:
        console.print(
            f"\n[bold yellow]Pull incomplete: {result.total_written} written, "
            f"{result.total_failed} failed.[/bold yellow] Fix the warning above, "
            "then run 'mm pull'; failed files are retried automatically on the next pull."
        )
    else:
        console.print("\n[bold green]Pull complete.[/bold green]")
    parts = []
    if result.total_written:
        parts.append(f"{result.total_written} written")
    if result.total_merged:
        parts.append(f"{result.total_merged} merged")
    if result.total_skipped:
        parts.append(f"{result.total_skipped} skipped (local newer)")
    if result.total_conflicted:
        parts.append(f"[yellow]{result.total_conflicted} conflicts[/yellow]")
    if result.total_failed:
        parts.append(f"[red]{result.total_failed} failed[/red]")
    if result.total_waiting:
        parts.append(f"[yellow]{result.total_waiting} waiting[/yellow]")
    if result.total_skipped_unknown_source:
        parts.append(
            f"[yellow]{result.total_skipped_unknown_source} unknown source(s) skipped[/yellow]"
        )
    if parts:
        console.print("  " + ", ".join(parts))
    else:
        console.print("  nothing to apply")
    if clause := waiting_clause(result):
        console.print(f"  [yellow]{safe_str(clause)}[/yellow]; run 'mm pull' again once it syncs.")
    if clause := old_form_clause(result):
        # Waits on a person updating a machine, not on delivery: no re-pull remedy.
        console.print(f"  [yellow]{safe_str(clause)}[/yellow]")
    if result.bytes_transferred:
        mb = result.bytes_transferred / (1024 * 1024)
        console.print(f"  {mb:.1f}MB transferred")
    console.print(f"  Completed in {result.elapsed:.1f}s")
    if result.total_conflicted:
        console.print(
            "  [yellow]Run [bold]mm conflicts[/bold] to review, "
            "[bold]mm resolve[/bold] to pick a winner.[/yellow]"
        )


# ── iter_source_diffs: shared push/status/diff iteration ──────────────
#
# Three call sites (_push_core, status, diff) shared a 3-line per-source
# boilerplate: fetch remote_src, run diff_files, optionally filter by
# --source / skip unchanged. Extracted here. The pull path cannot use
# this helper — it calls diff_files(remote, local) with arguments swapped
# (see diff_files docstring's "Arg-swap convention" note), so the
# semantics of .new/.modified/.deleted flip and a shared iterator would
# obscure that. Pull stays inline.
#


# ── _pull_core: decomposition pattern ──────────────────────────────────
#
# _pull_core aggregates source results; _print_pull_summary owns totals.
# _ApplyReporter records at publication and emits per-file diagnostics.
# On interruption, fsync runs first, then guarded forensic bookkeeping;
# summary/teardown cannot replace the original exception. Load-bearing warnings
# (corrupt peers, unknown sources, fsync failures, per-source
# conflicts/failures) are accumulated into lists and routed to stderr by
# _print_pull_summary — they survive quiet-mode suppression.
#
# The rest of cli.py (push, status, diag, recover) still uses the older
# side-effect-during-logic style. Migrate opportunistically when touching.
#
def _unplaceable_project(src_name: str, rel_path: str) -> bool:
    """True for a ``claude`` file or tombstone path under a folder this machine cannot place."""
    if src_name != "claude":
        return False
    parts = manifest._split_project_key(rel_path)
    return parts is not None and projectid.unplaceable_reason(parts[0]) is not None


def _files_from(by_device: dict[str, set[str]], labels: dict[str, str]) -> str:
    return ", ".join(
        f"{core._count(len(files), 'file', 'files')} from {safe_str(labels.get(did, did))}"
        for did, files in sorted(by_device.items(), key=lambda item: labels.get(item[0], item[0]))
    )


def _print_project_lists(report: core.ProjectNameReport, labels: dict[str, str]) -> None:
    """`mm pull --dry-run --verbose`'s one inspection list; it writes nothing."""
    idx = projectid.index()
    if report.not_placed:
        console.print("Not placed here:")
        for wire in sorted(report.not_placed):
            console.print(
                f"  {safe_str(projectid.display_name(wire))}: "
                f"{_files_from(report.not_placed[wire], labels)}. "
                "Have a checkout? Run mm pull inside it."
            )
    if idx is not None and idx.clones:
        console.print("Clones of one project (memory goes to the first):")
        for clone in idx.clones:
            console.print(
                f"  {safe_str(clone.identity)} -> {safe_str(clone.owner)} "
                f"(other clone: {safe_str(', '.join(clone.others))})"
            )
    if report.superseded:
        console.print("Not applied, this project has a git remote here:")
        for wire in sorted(report.superseded):
            console.print(
                f"  {safe_str(projectid.display_name(wire))}: "
                f"{_files_from(report.superseded[wire], labels)}"
            )


def _print_this_directory() -> None:
    """The first line of `mm pull --dry-run --verbose`: what this directory resolves to."""
    idx = projectid.index()
    if idx is None or idx.cwd is None:
        return
    cwd = idx.cwd
    if cwd.identity is not None and cwd.owner is not None:
        console.print(f"This directory: {safe_str(cwd.identity)} -> {safe_str(cwd.owner)}")
    else:
        console.print(f"This directory: no identity ({cwd.why or projectid.WHY_NO_CHECKOUT})")


def _pull_core(
    config: dict,
    passphrase: str,
    memory_kb: int,
    from_device: str | None = None,
    source_filter: str | None = None,
    verbose: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    conflict_mode: ConflictMode = "keep-both",
    *,
    diagnostics: bool | None = None,
    result_observer: Callable[[PullResult], None] | None = None,
) -> PullResult:
    """Core pull logic shared by pull and autopull.

    Additive-only: downloads new and modified files, never deletes local
    files. Tombstoned files are skipped. JSONL files are merged. MEMORY.md
    files are line-merged. Non-mergeable files with divergent local edits
    follow the _apply_incoming_file decision tree.

    `conflict_mode`:
      - "keep-both": default; auto keep-both on conflict.
      - "prompt":    ask per-file (interactive only).
      - "fail":      preflight every file; if any would conflict, raise
                     typer.Exit(3) before applying any file. Best-effort (TOCTOU).

    When quiet=True, load-bearing warnings still reach stderr; cosmetic
    progress chatter is suppressed.
    """
    show = not quiet and (diagnostics if diagnostics is not None else True)
    with pullhistory.batch():
        refusal_suffix = core.DRY_RUN_REFUSAL if dry_run else ""
        interactive_resolve_flag = conflict_mode == "prompt"
        start = time.time()
        my_device_id = config["device"]["id"]
        backend = get_backend(config)
        # Track 90A: only the pull paths (mm pull, its dry run, mm autopull and the
        # join's pull) let the current directory's main checkout own a project
        # nobody else owns. Called before the first decode.
        projectid.allow_cwd_candidate()

        if dry_run and verbose and not quiet:
            _print_this_directory()

        # Track 12A: shared accumulator for inline keep-canonical decisions, drained
        # once after the whole device loop. Only allocated in interactive (`prompt`)
        # mode — non-interactive pulls (default keep-both, autopull) never populate
        # it, so None keeps the bump machinery a no-op down the whole call chain.
        pending_inline_bumps: dict[Path, float] | None = {} if interactive_resolve_flag else None

        # Track 5E gate: refuse pull if any peer is pre-v0.9.2 OR has a
        # corrupt device.json (we can't read its version). Runs BEFORE the
        # pre-inversion migration sweep so we don't accidentally migrate
        # files in a refusal scenario where the user is about to upgrade
        # their peers and re-pull. Exits via _error → typer.Exit(1).
        core._sync_progress_phase("Reading peer manifests")
        # One device inventory per pull (Track 87A): the gate, peer selection,
        # tombstone collection and the waiting notice all read this, so a
        # Syncthing replacement between two reads cannot make them disagree.
        inventory = inventory_devices(backend)
        _check_fleet_version_or_refuse(
            backend, my_device_id, refusal_suffix=refusal_suffix, inventory=inventory
        )

        # Pre-inversion conflict-file migration. Runs once per pull under the
        # already-held mm lockfile — safe against autopull racing with another
        # discovery walk. mm pull is about to potentially produce NEW (post-
        # inversion) conflict files via `_apply_conflict`; without this sweep,
        # the user's tree would end up with a mix of pre-inversion and post-
        # inversion files indistinguishable except by mtime — and resolve's
        # dual-mode dispatch needs the prefix, not the timestamp.
        if not dry_run:
            # resolveflow is a leaf and cannot pause the cli-layer spinner;
            # pause around the sweep so its migration warnings print clean.
            with core._pause_sync_progress():
                resolveflow._find_conflict_files(config, migrate_pre_inversion=True)

        # Widened to carry path + type per source. Type is load-bearing for
        # the sync-log gate in _pull_one_source — keying on type (not name)
        # lets users rename the claude source without losing per-project logs.
        pull_resolution = resolve_sources(config)
        pull_sources = list(pull_resolution.available)
        pull_sources.extend(
            src
            for src in pull_resolution.selected
            if src["name"] in MM_INTERNAL_SOURCE_NAMES and src not in pull_sources
        )
        local_sources_map: dict[str, dict[str, Any]] = {
            src_cfg["name"]: {
                "path": Path(src_cfg["path"]).expanduser().absolute(),
                "type": src_cfg["type"],
            }
            for src_cfg in pull_sources
        }

        all_devices, pull_targets = _select_devices(backend, my_device_id, from_device, inventory)
        # A device entry that vanished mid-read is waiting, not corrupt and not
        # unknown. Its id stays in the manifest set (its tombstones still
        # apply); only its version check and its files are skipped.
        vanished_entries = [
            i
            for i in inventory.vanished
            if i != my_device_id and (not from_device or i == from_device)
        ]
        if from_device and not pull_targets and from_device not in inventory.vanished:
            if not quiet:
                core._error(f"Device not found: {from_device}" + refusal_suffix)
        if not pull_targets:
            if show:
                console.print("[yellow]No other devices found to pull from.[/yellow]")
            if dry_run and not quiet:
                console.print("No changes predicted.")
            return PullResult(
                no_peers=not (from_device and from_device in inventory.vanished),
                no_sources=not pull_resolution.selected,
                elapsed=time.time() - start,
                waiting=_waiting_peers([], [], vanished_entries),
            )

        vanished_manifests: list[_CorruptPeer] = []
        manifest_cache, corrupt_peers = _prefetch_manifests(
            backend,
            all_devices + [{"device_id": i, "device_name": i} for i in inventory.vanished],
            passphrase,
            memory_kb,
            vanished_out=vanished_manifests,
        )
        # This Mac's own manifest carries its deletion records. If it vanished
        # mid-read they are not applied this pull, so a file deleted here can
        # come back from a peer: data at risk, reported like a corrupt
        # manifest (warning plus a degraded autopull), never as waiting.
        corrupt_peers.extend(
            _CorruptPeer(p.device_id, p.device_name, own_vanished=True)
            for p in vanished_manifests
            if p.device_id == my_device_id
        )
        vanished_manifests = [
            p
            for p in vanished_manifests
            if p.device_id != my_device_id
            and p.device_id not in inventory.vanished
            and (not from_device or p.device_id == from_device)
        ]
        core._sync_progress_phase("Checking local files")

        # Consumer-boundary exclude_patterns filter for pull. Drops excluded
        # paths from each peer manifest before tombstone collection AND before
        # the per-source download loop. Without this, a peer who hasn't yet
        # adopted the local exclude_patterns ships an excluded file in their
        # manifest, and pull would either download it (defeating the exclude)
        # or surface it as a conflict. Filter HERE, not in
        # `_fetch_remote_manifest` — `mm gc` reads this device's raw manifest
        # via that path to compute referenced blobs, and a filtered manifest
        # there would mark live own blobs as orphans (codex-2 #1).
        # Same disable-then-exclude order as _push_core. The disabled-sources
        # filter strips entire `sources.<name>` entries so the per-peer
        # download loop never tries to land a disabled source's files locally.
        # P0 invariant — see _filter_disabled_sources docstring.
        disabled_sources = list(config.get("sync", {}).get("disabled_sources", []) or [])
        manifest_cache = {
            did: (None if m is None else core._filter_disabled_sources(m, disabled_sources))
            for did, m in manifest_cache.items()
        }
        # Track 90A: what the peers hold that this machine cannot place, read
        # before the consumer filter drops it.
        target_ids = {d["device_id"] for d in pull_targets}
        project_names = core.project_name_report(
            {did: m for did, m in manifest_cache.items() if did in target_ids}
        )
        device_labels = {d["device_id"]: d.get("device_name", d["device_id"]) for d in all_devices}
        old_form_peers = [
            OldFormPeer(did, device_labels.get(did, did), files)
            for did, files in project_names.old_form.items()
        ]
        # Pull never walks local trees for nested checkouts: it probes only the
        # local ancestors of incoming peer paths, so it never writes into one.
        nested_roots = core._incoming_nested_roots(pull_sources, manifest_cache.values())
        exclude_map, skip_prefixes = core._build_exclude_map(config, nested_roots=nested_roots)
        include_map = core._build_include_map(config)
        # Always run: conflict-shaped names and `.extend-root` are stripped even
        # when no source declares exclude_patterns (claude-only installs).
        filtered_cache: dict[str, dict | None] = {}
        for did, m in manifest_cache.items():
            if m is None:
                filtered_cache[did] = None
                continue
            filtered = core._filter_excluded_paths(m, exclude_map, skip_prefixes, include_map)
            # Log "excluded" per peer-path so `mm log --action excluded`
            # shows what the user is NOT pulling because of their config.
            #
            # 5E ship-fix: skip logging in quiet (autopull/autopush) mode.
            # Without this gate, autopull writes one record per peer ×
            # source × excluded-path tuple on every hook fire — at
            # ~100 projects × hourly pulls would needlessly fill the excluded
            # class budget. Interactive `mm pull` still
            # logs the full set so users can audit their excludes.
            # A nested checkout logs one record per root and peer, not per file,
            # whether files or deletions under it were skipped.
            if not quiet and not dry_run:
                logged_roots: set[tuple[str, str]] = set()

                def checkout_root(src_name: str, rel_path: str) -> str | None:
                    return next(
                        (
                            r
                            for r in nested_roots.get(src_name, [])
                            if _under_skip_prefix(rel_path, [r])
                        ),
                        None,
                    )

                def log_root(src_name: str, root: str) -> None:
                    if (src_name, root) not in logged_roots:
                        logged_roots.add((src_name, root))
                        pullhistory.append(
                            verb="pull",
                            device=did,
                            source=src_name,
                            rel_path=root,
                            action="excluded",
                        )

                for src_name, src_data in m.get("sources", {}).items():
                    kept = filtered.get("sources", {}).get(src_name, {}).get("files", {})
                    for rel_path, info in src_data.get("files", {}).items():
                        if rel_path in kept:
                            continue
                        if root := checkout_root(src_name, rel_path):
                            log_root(src_name, root)
                            continue
                        if _unplaceable_project(src_name, rel_path):
                            # One row per peer per run for every name this
                            # machine cannot place, not one per file.
                            log_root(src_name, "projects/@")
                            continue
                        pullhistory.append(
                            verb="pull",
                            device=did,
                            source=src_name,
                            rel_path=rel_path,
                            action="excluded",
                            remote_sha=info.get("sha256"),
                        )
                for key in m.get("tombstones", {}):
                    if not isinstance(key, str) or key in filtered.get("tombstones", {}):
                        continue
                    src_name, rel_path = key.split(":", 1) if ":" in key else ("claude", key)
                    if root := checkout_root(src_name, rel_path):
                        log_root(src_name, root)
                    elif _unplaceable_project(src_name, rel_path):
                        log_root(src_name, "projects/@")
            filtered_cache[did] = filtered
        manifest_cache = filtered_cache

        # Group 7 preflight #6 + D11: pull-time case-collision detection.
        # On case-insensitive local FS (APFS default, NTFS), two manifest
        # entries that differ only in casing (peer A "Projects/x.md" + peer B
        # "projects/x.md") would resolve to the same inode locally — the
        # second would silently overwrite or alias the first. Detect per
        # source, emit `mm: warning:` per cluster, drop all-but-lex-first
        # from each peer manifest. Codex outside-voice T5: a Linux peer can
        # legitimately have both names; we don't normalize manifest keys
        # globally — we only skip the duplicate WRITE on case-insensitive
        # consumers. The raw manifest stays intact for future cross-platform
        # peers.
        case_collisions = _detect_pull_case_collisions(manifest_cache, local_sources_map)
        if case_collisions:
            with core._pause_sync_progress():
                for src_name, clusters in case_collisions.items():
                    for paths in clusters.values():
                        # src_name and paths come from peer manifests — sanitize
                        # before render (Group 7 preflight #1 sweep extension).
                        safe_kept = safe_str(paths[0])
                        safe_dropped = ", ".join(safe_str(p) for p in paths[1:])
                        stderr_console.print(
                            f"mm: warning: case-collision in source "
                            f"'{safe_str(src_name)}' on case-insensitive FS — "
                            f"keeping '{safe_kept}', skipping [{safe_dropped}] "
                            f"(Linux peer can legitimately push both; this device "
                            f"can only represent one)"
                        )
            manifest_cache = _drop_case_collisions_from_manifests(manifest_cache, case_collisions)

        all_tombstones = collect_tombstones(
            list(manifest_cache.keys()),
            lambda did: manifest_cache.get(did),
        )

        if dry_run and not quiet and (not source_filter or source_filter == "mm-events"):
            for src in pull_resolution.selected:
                if src["name"] in pull_resolution.would_create and any(
                    manifest_cache.get(device["device_id"], {})
                    .get("sources", {})
                    .get("mm-events", {})
                    .get("files")
                    for device in pull_targets
                    if manifest_cache.get(device["device_id"]) is not None
                ):
                    console.print(
                        f"mm pull will create {safe_str(src['path'])} (0700) "
                        "for incoming mm-events files."
                    )

        planner = None
        planned_unknown = []
        if dry_run or conflict_mode == "fail":
            planner, planned_unknown = _plan_pull(
                pull_targets,
                manifest_cache,
                local_sources_map,
                source_filter,
                all_tombstones,
            )
        if conflict_mode == "fail":
            assert planner is not None
            predicted = _fail_mode_refusals(planner.predictions)
            if predicted:
                core._stop_sync_progress()
                _print_pull_summary(
                    PullResult(),
                    corrupt_peers,
                    planned_unknown,
                    [],
                    [],
                    quiet,
                    verbose,
                    dry_run=True,
                )
                _print_preflight_conflicts(predicted, quiet)
                if dry_run:
                    stderr_console.print(core.DRY_RUN_REFUSAL.strip())
                raise typer.Exit(3)

        # Aggregate accumulators. Load-bearing warnings go through lists,
        # routed to stderr by _print_pull_summary. The try/finally below
        # guarantees _print_pull_summary runs even on unexpected exceptions
        # so accumulated warnings (corrupt peers, unknown sources, fsync
        # failures) still reach stderr — preserving the v0.8.1 visible-
        # failure contract.
        per_source_results: list[_PerSourceResult] = []
        unknown_sources: list[_UnknownSourceWarning] = []
        touched_parents: set[Path] = set()
        device_names: list[str] = []
        fsync_warnings: list[_FsyncWarning] = []
        total_written = total_merged = total_skipped = total_conflicted = 0
        total_failed = total_skipped_unknown_source = bytes_transferred = 0
        total_waiting = 0

        in_flight: _ApplyReporter | None = None
        per_source: _PerSourceResult | None = None
        accounted = False
        interruption: Literal["interrupted", "aborted"] | None = None

        def _account_source(per_source: _PerSourceResult) -> None:
            nonlocal bytes_transferred, total_written, total_merged, total_skipped
            nonlocal total_conflicted, total_failed, touched_parents, total_waiting
            per_source_results.append(per_source)
            bytes_transferred += per_source.bytes_transferred
            touched_parents |= per_source.touched_parents
            total_written += len(per_source.outcomes["written"])
            total_merged += len(per_source.outcomes["merged"]) + len(
                per_source.outcomes["merged-via-lcs"]
            )
            total_skipped += len(per_source.outcomes["skipped"])
            total_conflicted += len(per_source.outcomes["conflicted"])
            total_failed += len(per_source.outcomes["failed"])
            total_waiting += len(per_source.outcomes.get("waiting", ()))

        try:
            for device in pull_targets:
                did = device["device_id"]
                dname = device["device_name"]
                if show and not dry_run:
                    console.print(
                        f"\n[bold]Pulling from {safe_str(dname)} ({safe_str(did)})...[/bold]"
                    )

                remote_manifest = manifest_cache.get(did)
                if remote_manifest is None:
                    if show:
                        console.print(f"  [yellow]No manifest for {safe_str(dname)}[/yellow]")
                    continue

                device_had_changes = False

                for src_name, src_data in remote_manifest.get("sources", {}).items():
                    if source_filter and src_name != source_filter:
                        continue

                    if src_name not in local_sources_map:
                        total_skipped_unknown_source += 1
                        unknown_sources.append(
                            _UnknownSourceWarning(src_name=src_name, device_name=dname)
                        )
                        continue

                    src_info = local_sources_map[src_name]
                    base_path = src_info["path"]
                    src_type = src_info["type"]
                    if verbose and not quiet and not dry_run:
                        console.print(
                            f"  [bold]Source '{safe_str(src_name)}' ({safe_str(base_path)}):[/bold]"
                        )

                    in_flight = _ApplyReporter(
                        device_name=dname,
                        src_name=src_name,
                        outcomes=_empty_outcomes(),
                        pending_inline_bumps=pending_inline_bumps,
                    )
                    per_source = None
                    accounted = False
                    per_source = _pull_one_source(
                        backend,
                        src_name=src_name,
                        src_type=src_type,
                        src_data=src_data,
                        did=did,
                        dname=dname,
                        base_path=base_path,
                        all_tombstones=all_tombstones,
                        passphrase=passphrase,
                        memory_kb=memory_kb,
                        interactive_resolve=interactive_resolve_flag,
                        dry_run=dry_run,
                        verbose_console=(verbose and not quiet),
                        quiet=quiet,
                        devices=all_devices,
                        pending_inline_bumps=pending_inline_bumps,
                        reporter=in_flight,
                        predictions=(
                            [
                                p
                                for p in planner.predictions
                                if p.device_id == did and p.src_name == src_name
                            ]
                            if planner is not None
                            else None
                        ),
                    )

                    if dry_run:
                        per_source_results.append(per_source)
                        if not quiet and any(
                            p.outcome != "unchanged" for p in per_source.predictions
                        ):
                            console.print(f"  Dry run for {safe_str(dname)}/{safe_str(src_name)}:")
                            _print_pull_prediction(per_source.predictions, base_path, src_name)
                        in_flight = None
                        continue

                    if not per_source.had_changes:
                        in_flight = None
                        if verbose and not quiet:
                            console.print(
                                f"  [green]Up to date with "
                                f"{safe_str(dname)}/{safe_str(src_name)}.[/green]"
                            )
                        continue

                    _account_source(per_source)
                    accounted = True
                    # Waiting alone applies nothing: no device-name entry and no
                    # manifest cleanup for a peer whose blobs are still arriving.
                    if any(v for k, v in per_source.outcomes.items() if k not in _UNAPPLIED):
                        device_had_changes = True

                    _record_source_bookkeeping(per_source, src_data, quiet=quiet, verbose=verbose)
                    in_flight = None

                if device_had_changes:
                    device_names.append(dname)
                    # Clean up iCloud/Dropbox manifest conflict copies is best-
                    # effort: a failure here just leaves dup conflict copies on
                    # disk for the next pull to retry. Swallowing protects
                    # accumulated warnings from the visible-failure contract.
                    # Skip after a contained apply failure so an alternate
                    # conflict-copy of this device's manifest remains for retry.
                    # Waiting counts like failed: a kept copy of this peer's
                    # manifest may be the only holder of the manifest whose
                    # blobs have not been delivered yet.
                    device_failed = any(
                        r.device_id == did and (r.outcomes["failed"] or r.outcomes.get("waiting"))
                        for r in per_source_results
                    )
                    if not device_failed:
                        try:
                            core._cleanup_conflict_copies(backend, did, passphrase, memory_kb)
                        except (OSError, StorageError) as e:
                            msg = f"manifest conflict-copy cleanup failed for {dname}: {e}"
                            with core._pause_sync_progress():
                                if quiet:
                                    print(f"mm: warning: {msg}", file=sys.stderr)
                                else:
                                    console.print(f"  [yellow]warning:[/yellow] {msg}")

            fsync_warnings = _fsync_touched_parents(touched_parents)

            # Track 12A: end-of-pull-batch drain. Every peer has been walked, so
            # the recorded bump value beats all of them at once. INSIDE the try:
            # a typer.Abort() from the inline (a)bort choice propagates past this
            # point straight to `finally`, intentionally skipping the drain —
            # abort means the user does not trust this pull, so half-made
            # keep-canonical decisions are not broadcast to the fleet.
            _drain_inline_bumps(pending_inline_bumps)
        except BaseException as exc:
            # A finally-with-access-to-the-error: preserve completed publications
            # for every exception class, then re-raise the ORIGINAL exception.
            # Ctrl-C waits for bounded directory fsync. A second Ctrl-C during
            # recovery can replace the first traceback (accepted residual window).
            if in_flight is not None:
                if per_source is None:
                    assert in_flight.outcomes is not None
                    per_source = _PerSourceResult(
                        src_name=src_name,
                        device_name=dname,
                        device_id=did,
                        outcomes=in_flight.outcomes,
                        bytes_transferred=in_flight.bytes_transferred,
                        touched_parents=in_flight.touched_parents,
                        claude_sync_base=str(base_path) if src_type == "claude" else None,
                    )
                if not accounted:
                    _account_source(per_source)
            fsync_warnings = _fsync_touched_parents(touched_parents)
            if in_flight is not None:
                try:
                    _record_source_bookkeeping(per_source, src_data, quiet=quiet, verbose=verbose)
                except (Exception, SystemExit):
                    pass
            interruption = "aborted" if isinstance(exc, typer.Abort) else "interrupted"
            raise
        finally:
            # Even if an unexpected exception propagates from the loop above,
            # emit accumulated load-bearing warnings to stderr. The v0.8.1
            # contract requires corrupt-peer / unknown-source / fsync-failure
            # warnings to survive quiet mode AND partial pulls.
            _partial_result = PullResult(
                old_form_peers=old_form_peers,
                not_placed_files=project_names.not_placed_files,
                not_placed_projects=project_names.not_placed_projects,
                no_sources=not pull_resolution.selected,
                total_written=total_written,
                total_merged=total_merged,
                total_skipped=total_skipped,
                total_conflicted=total_conflicted,
                total_failed=total_failed,
                total_skipped_unknown_source=total_skipped_unknown_source,
                bytes_transferred=bytes_transferred,
                device_names=device_names,
                elapsed=time.time() - start,
                durability_fsync_failures=len(fsync_warnings),
                corrupt_peer_count=len(corrupt_peers),
                total_waiting=total_waiting,
                waiting=_waiting_peers(per_source_results, vanished_manifests, vanished_entries),
            )
            if result_observer is not None:
                with suppress(Exception, SystemExit):
                    result_observer(_partial_result)
            exception_in_flight = sys.exc_info()[0] is not None
            core._stop_sync_progress()
            try:
                _print_pull_summary(
                    _partial_result,
                    corrupt_peers=corrupt_peers,
                    unknown_sources=unknown_sources,
                    fsync_warnings=fsync_warnings,
                    per_source_results=per_source_results,
                    quiet=quiet or not show,
                    verbose=verbose,
                    interruption=interruption,
                    dry_run=dry_run,
                )
            except (Exception, SystemExit):
                if not exception_in_flight:
                    raise

        if dry_run and not quiet:
            assert planner is not None
            console.print(pullplan.totals(planner.predictions))
            if verbose:
                _print_project_lists(project_names, device_labels)
            if corrupt_peers:
                console.print(
                    f"Preview incomplete: {len(corrupt_peers)} peer(s) could not be "
                    "inspected (see the warnings above)."
                )
        return _partial_result
