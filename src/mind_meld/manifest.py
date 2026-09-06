"""Manifest building and diffing for Mind Meld.

Walks ~/.claude/projects/*/memory/ and ~/.claude/projects/*/todos/ only (claude source).
Also supports generic sources with configurable include_dirs/include_files.

Builds truth-based manifest snapshots and diffs them to find changes.

Manifest formats:
  v1 — flat "files" dict, single claude source (pre-v0.4 on-disk only)
  v2 — "sources" dict keyed by source name, each with base_path + files

Read-path invariant: every manifest loaded from bytes/disk MUST go through
`load_manifest`, which composes `deserialize_manifest` + `normalize_manifest`.
This guarantees downstream code sees a v2-shaped manifest with `sources` and
`tombstones` dicts and `<source>:<path>`-shaped tombstone keys (where keys
were normalizable). Do NOT add a new manifest-load path that bypasses
`load_manifest` — that's how silent deletion-resurrection bugs creep in.

Top-level "files" key: pre-Track-1B v2 writers also emitted a redundant
top-level "files" mirror of the claude source. `normalize_manifest` now
strips it unconditionally (both v1 promotion and v2 passthrough) so
shallow dict-copies (e.g. `_merge_manifests` at cli.py:553) can't carry
it forward from an old on-disk manifest. The payload itself is never
lost: v1 promotion copies it into `sources.claude.files` first.
"""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

from mind_meld.errors import ManifestError, SnapshotError, os_error_cause, snapshot_refusal

# Syncthing-style local conflict copies: <stem>.sync-conflict-<YYYYmmdd-HHMMSS>-<device>[.<ext>]
# Pattern pinned to the exact timestamp shape `conflict_filename()` emits
# (8 digits + dash + 6 digits + dash + suffix), so user files like
# `notes.sync-conflict-log.md` or `notes.sync-conflict-2024-summary.md`
# are NEVER false-positive-excluded. fnmatch char classes match exactly
# one character, so the digit count is enforced precisely.
#
# Track 5E (v0.9.2 BREAKING) introduces an optional `v0-` prefix in the
# metadata position to mark pre-inversion conflict files. Files without the
# prefix were created by post-inversion code; files WITH the prefix were
# either created by pre-inversion code AND migrated by `_find_conflict_files`,
# or pre-inversion code that mm rewrote during migration. The dual-pattern
# match keeps `mm gc --conflicts` and walker exclusion working uniformly
# across both eras.
#
# Track 45A (v0.14.0): post-inversion files mint a `v1` era marker AFTER
# the timestamp: <stem>.sync-conflict-<8d>-<6d>-v1-<dev8>[-<rand4>]<ext>.
# NEVER as a v0-style prefix — `*.sync-conflict-v1-<8d>-...` fails
# CONFLICT_PATTERN, so is_conflict_filename is False, so _is_excluded is
# False, so the conflict copy would upload to the fleet.
CONFLICT_INFIX = ".sync-conflict-"
CONFLICT_V0_PREFIX = "v0-"
CONFLICT_V1_MARKER = "v1"
_DIGITS_8 = "[0-9]" * 8
_DIGITS_6 = "[0-9]" * 6
CONFLICT_PATTERN = f"*{CONFLICT_INFIX}{_DIGITS_8}-{_DIGITS_6}-*"
CONFLICT_PATTERN_V0 = f"*{CONFLICT_INFIX}{CONFLICT_V0_PREFIX}{_DIGITS_8}-{_DIGITS_6}-*"
# gstack-extend drops this file in every directory it renders. The walker
# skips the containing directory; see marker_skip_globs.
MARKER_SKIP_NAME = ".extend-root"


def is_conflict_filename(name: str) -> bool:
    """Return True iff `name` is an mm/Syncthing-style conflict copy.

    Strict matcher: the suffix after `.sync-conflict-` must start with a
    timestamp (or `v0-<timestamp>` for pre-inversion files migrated by
    Track 5E). Used by the walker to keep conflict files local-only and by
    `mm conflicts`/`mm gc --conflicts` to avoid false-positives on user
    files that happen to contain `.sync-conflict-` in their name.
    """
    if not name:
        return False
    return fnmatch.fnmatch(name, CONFLICT_PATTERN) or fnmatch.fnmatch(name, CONFLICT_PATTERN_V0)


def _conflict_metadata_parts(name: str) -> list[str] | None:
    """Split the conflict metadata block into hyphen segments, or None.

    Uses ``rindex`` so a documented non-conflict name that itself contains
    the infix (``notes.sync-conflict-log.md``) plus mm's own appended infix
    parses the LAST infix. Strips an optional ``v0-`` prefix and the file
    extension. Does not raise.
    """
    if not is_conflict_filename(name):
        return None
    try:
        infix_at = name.rindex(CONFLICT_INFIX)
    except ValueError:
        return None
    after = name[infix_at + len(CONFLICT_INFIX) :]
    if after.startswith(CONFLICT_V0_PREFIX):
        after = after[len(CONFLICT_V0_PREFIX) :]
    last_dot = after.rfind(".")
    metadata = after[:last_dot] if last_dot != -1 else after
    return metadata.split("-")


def parse_conflict_device_short(name: str) -> str | None:
    """Extract the device-short id from a conflict filename, if present.

    Returns the 8-char device prefix `conflict_filename()` stamped into the
    name, or ``None`` if `name` doesn't match the conflict pattern. Handles
    pre-inversion (``v0-``), unprefixed post-inversion, and v1-era
    (``-v1-`` after the timestamp) shapes, plus the optional 4-char
    same-second random suffix.

    Filename grammar produced by ``conflict_filename`` (v0.14.0+):
      ``<stem>.sync-conflict-[v0-]<8d>-<6d>[-v1]-<device8>[-<rand4>]<ext>``

    After stripping ``v0-`` and the extension, parts are one of::

        [<8d>, <6d>, <device8>]
        [<8d>, <6d>, <device8>, <rand4>]
        [<8d>, <6d>, v1, <device8>]
        [<8d>, <6d>, v1, <device8>, <rand4>]

    The ``v1`` token is skipped when present so the device is still the
    segment after the timestamp (or after ``v1``). If the conflict portion
    ends with the optional ``-<rand4>`` collision suffix, the device is the
    last-but-one remaining segment; otherwise it's the last one.

    Used by ``mm resolve`` to attribute the REMOTE side of a conflict to a
    peer device-name rather than the bare hex id, and by
    ``_existing_post_inversion_sidecars_from_peer`` to dedup.
    """
    parts = _conflict_metadata_parts(name)
    if parts is None:
        return None
    # Skip the v1 era marker (after timestamp, before device). The v1
    # token is 2 chars, not 4, so the rand4 heuristic below would NOT
    # skip it — we have to drop it explicitly or a future "return the
    # last 8-char hex segment" rewrite silently breaks attribution.
    if len(parts) >= 4 and parts[2] == CONFLICT_V1_MARKER:
        parts = parts[:2] + parts[3:]
    # parts shape: [<8d>, <6d>, <device8>] OR [<8d>, <6d>, <device8>, <rand4>]
    # `is_conflict_filename` already enforced the leading shape, so any
    # length < 3 here is a malformed file we can't attribute.
    if len(parts) < 3:
        return None
    # If the trailing segment looks like the 4-char random suffix (hex,
    # length 4), drop it. Otherwise the trailing segment IS the device.
    if len(parts) >= 4 and len(parts[-1]) == 4 and all(c in "0123456789abcdef" for c in parts[-1]):
        return parts[-2]
    return parts[-1]


def parse_conflict_created_at(name: str) -> datetime | None:
    """Parse the UTC timestamp mm stamped into a conflict filename.

    This is the sidecar's own birth time — distinct from ``st_mtime``,
    which ``_apply_conflict`` restores to the *peer file's* mtime. Age
    consumers (migration gate, gc bar, ``mm conflicts`` Conflict-age
    column) MUST use this, not ``st_mtime``.

    Index math off ``rindex(CONFLICT_INFIX)`` (not ``find``), skips an
    optional ``v0-``, takes ``<8d>-<6d>``, parses as UTC. Returns ``None``
    on anything unparseable — never raises. ``is_conflict_filename``
    validates digit SHAPE, not date validity, so
    ``a.sync-conflict-20261345-999999-abcd1234.md`` reaches ``strptime``
    and must be caught here.

    None-policy is per consumer, not uniform: migration refuses to
    migrate, gc refuses to reap, display renders ``?``. Falling back to
    ``st_mtime`` re-opens the clock-conflation bug.
    """
    parts = _conflict_metadata_parts(name)
    if parts is None or len(parts) < 2:
        return None
    stamp = f"{parts[0]}-{parts[1]}"
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_v1_conflict_filename(name: str) -> bool:
    """Return True iff `name` is a v1-era (post-v0.14.0) conflict copy.

    The ``v1`` token sits AFTER the timestamp, never as a ``v0-``-style
    prefix. A prefix form fails ``is_conflict_filename`` and would upload
    the conflict copy to the fleet.
    """
    parts = _conflict_metadata_parts(name)
    if parts is None:
        return False
    return len(parts) >= 3 and parts[2] == CONFLICT_V1_MARKER


def is_pre_inversion_conflict_filename(name: str) -> bool:
    """Return True iff `name` is a `v0-`-prefixed conflict copy.

    Used by `_resolve_interactive_loop`'s dual-mode dispatch: `v0-` files
    were produced under pre-inversion semantics (canonical = remote, sidecar
    = local), so `(l)ocal` means `rename sidecar -> canonical`. Files without
    the prefix are post-inversion and `(l)ocal` means `unlink sidecar`
    (canonical IS local already).
    """
    if not name:
        return False
    return fnmatch.fnmatch(name, CONFLICT_PATTERN_V0)


def _canonical_for_conflict(conflict_path: Path) -> Path | None:
    """Given a .sync-conflict-<ts>-<device>.<ext> path, return the canonical sibling.

    Strips the ".sync-conflict-<rest>" infix from the filename, re-assembling
    the original stem and extension. Uses rfind so that files which already
    had an infix before mm added its own (e.g., a Syncthing conflict file
    that mm then conflicted again) unwind the most recent layer only.

    Returns None when reconstruction is empty or a directory component
    ("." or ".."). Those shapes can pass the conflict-name check but have
    no file owner. Never alias the sidecar or a parent directory as its
    canonical, or pass an invalid name to Path.with_name. Non-conflict
    names (no infix) still return the input path unchanged.
    """
    name = conflict_path.name
    idx = name.rfind(CONFLICT_INFIX)
    if idx == -1:
        return conflict_path
    before = name[:idx]
    # Everything after the infix up to the final suffix is conflict metadata.
    after = name[idx + len(CONFLICT_INFIX) :]
    suffix = ""
    if "." in after:
        suffix = "." + after.rsplit(".", 1)[-1]
    reconstructed = before + suffix
    if reconstructed in ("", ".", ".."):
        return None
    return conflict_path.with_name(reconstructed)


EXCLUDED = [
    "node_modules/",
    ".git/",
    ".DS_Store",
    ".env",
    ".env.*",
    "*.log",
    "*.tmp",  # mm writes <target>.tmp during atomic writes; don't sync leftovers
    ".claude-sync/",
    "dist/",
    "build/",
    ".next/",
    ".turbo/",
    "__pycache__/",
    "*.pyc",
    ".mind-meld-log.md",
    MARKER_SKIP_NAME,  # gstack-extend render marker; never a sync file
]

# Only sync these subdirectories within each project.
# Everything else (sessions, settings, etc.) is either git-tracked or ephemeral.
SYNCED_SUBDIRS = ["memory", "todos"]

# Grok user-home customization trees. Hardcoded the same way Claude's
# walker hardcodes memory/todos — not user-editable include_dirs, so a
# config edit cannot widen the walk to sessions/ or auth.json.
# Inspected 2026-08-17 against Grok 1.0.4 user-guide + live ~/.grok.
GROK_SYNCED_SUBDIRS = ["skills", "commands", "rules"]
# Generated host links are per-machine routing, matching Codex/OpenCode.
#
# Deliberately does NOT mirror v0.12.51's additions to the Codex/OpenCode
# entries (`skills/.system/*` and `config._GENERATED_HOST_SKILL_GLOBS`).
# Verified 2026-09-01 against the live `~/.grok`: no `skills/.system`, no
# `.extend-root` marker anywhere in the tree, and none of the five
# gstack-extend-rendered skill names present. gstack-extend's
# `setup --host auto` renders into Claude, Codex and OpenCode only, and the
# Codex system payload is Codex's own. The asymmetry is a measured absence,
# not an oversight — if gstack-extend ever grows a Grok target, add the
# globs here at the same time.
GROK_EXCLUDE_PATTERNS = [
    "skills/gstack-*",
    "skills/log-work/*",
    "skills/retro-fleet/*",
]


def _under_skip_prefix(rel_path: str, prefixes: list[str] | None) -> bool:
    """True if ``rel_path`` is a skip-prefix or a file under one.

    Prefix match, not fnmatch: a directory literally named ``*`` with a
    marker must not exclude ``projects/myapp/foo``.
    """
    if not prefixes:
        return False
    for prefix in prefixes:
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return True
    return False


def marker_skip_globs(
    source_config: dict[str, Any],
    *,
    strict: bool = False,
) -> list[str]:
    """Return relative-dir prefixes for directories that contain ``.extend-root``.

    gstack-extend drops ``.extend-root`` in every directory it renders.
    ``exclude_patterns`` cannot express "skip the directory CONTAINING this
    file," so the walker and the consumer-boundary filter both call this
    helper. Results are PATH PREFIXES, not fnmatch globs — interpolating
    ``rel_dir`` into ``{rel_dir}/*`` would let a peer-chosen ``*`` in the
    path exclude unrelated files. A marker skip must not generate deletion
    tombstones, exactly as adding a glob must not.

    Permissive mode is best-effort: any OSError on a scan dir is skipped.
    Strict publishing raises SnapshotError instead. Symlinked scan dirs
    and symlink markers are ignored (same as the walker). Generic sources
    only — callers with no ``include_dirs`` get ``[]``.
    """
    include_dirs: list[str] = source_config.get("include_dirs") or []
    if not include_dirs:
        return []
    source_name = source_config.get("name")
    try:
        base = Path(source_config["path"]).expanduser().resolve()
    except (KeyError, TypeError):
        return []
    except OSError as e:
        if strict:
            raise SnapshotError(
                snapshot_refusal(
                    source=source_name,
                    problem="could not resolve the source path",
                    cause=os_error_cause(e),
                    next_action="Restore read access, then run mm push.",
                )
            ) from e
        return []
    try:
        base.stat()
    except FileNotFoundError:
        return []
    except OSError as e:
        if strict:
            raise SnapshotError(
                snapshot_refusal(
                    source=source_name,
                    problem="could not be read",
                    cause=os_error_cause(e),
                    next_action="Restore read access, then run mm push.",
                )
            ) from e
        return []
    globs: list[str] = []
    seen: set[str] = set()
    for dir_name in include_dirs:
        scan_dir = base / dir_name
        try:
            st = scan_dir.lstat()
        except FileNotFoundError:
            continue
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=dir_name,
                        problem="could not be read",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            continue
        try:
            markers = _collect_marker_files(scan_dir, base, source_name=source_name, strict=strict)
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=dir_name,
                        problem="could not be enumerated",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            continue
        for marker in markers:
            try:
                marker_st = marker.lstat()
                if stat.S_ISLNK(marker_st.st_mode) or not stat.S_ISREG(marker_st.st_mode):
                    continue
                rel_dir = marker.parent.relative_to(base).as_posix()
            except FileNotFoundError:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=dir_name,
                            problem="disappeared while being read",
                            next_action="Wait for the writer to finish, then run mm push.",
                        )
                    ) from None
                continue
            except (OSError, ValueError) as e:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=dir_name,
                            problem="could not be read",
                            cause=os_error_cause(e),
                            next_action="Restore read access, then run mm push.",
                        )
                    ) from e
                continue
            # A marker sitting ON the include-dir root would prefix the
            # entire source tree (`skills/.extend-root` → skip `skills/`).
            # gstack-extend drops the marker in each rendered child dir.
            #
            # Compare NORMALIZED forms. `rel_dir` came from `as_posix()`,
            # but `dir_name` is raw config and `include_dirs` entries are
            # not validated or normalized anywhere (`_validate_sources`
            # checks only name/path/type). A user writing
            # `include_dirs = ["skills/"]` or `["./skills"]` would miss
            # this exemption and silently drop the WHOLE tree — including
            # hand-authored files — out of sync.
            if rel_dir == Path(dir_name).as_posix():
                continue
            if rel_dir not in seen:
                seen.add(rel_dir)
                globs.append(rel_dir)
    return globs


def _collect_marker_files(
    scan_dir: Path,
    base: Path,
    *,
    source_name: str | None,
    strict: bool,
) -> list[Path]:
    """Find ``.extend-root`` markers under ``scan_dir`` without following links."""
    found: list[Path] = []

    def walk(directory: Path, *, discovered: bool) -> None:
        try:
            rel_dir = directory.relative_to(base).as_posix() if directory != base else ""
        except ValueError:
            rel_dir = directory.name
        if rel_dir and _dir_is_policy_skipped(rel_dir, None):
            return
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except FileNotFoundError:
            if discovered and strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=_rel_or_name(directory, base),
                        problem="disappeared while being read",
                        next_action="Wait for the writer to finish, then run mm push.",
                    )
                ) from None
            return
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=rel_dir or _rel_or_name(directory, base),
                        problem="could not be enumerated",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            return
        entries.sort(key=lambda entry: entry.name)
        child_dirs: list[Path] = []
        marked = False
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=_rel_or_name(Path(entry.path), base),
                            problem="disappeared while being read",
                            next_action="Wait for the writer to finish, then run mm push.",
                        )
                    ) from None
                continue
            except OSError as e:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=_rel_or_name(Path(entry.path), base),
                            problem="could not be read",
                            cause=os_error_cause(e),
                            next_action="Restore read access, then run mm push.",
                        )
                    ) from e
                continue
            child = Path(entry.path)
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_ISREG(st.st_mode) and entry.name == MARKER_SKIP_NAME:
                found.append(child)
                marked = True
            elif stat.S_ISDIR(st.st_mode):
                child_dirs.append(child)
        # Include-root exemption: a marker on scan_dir itself must not
        # skip walking children. Nested marked dirs are fully pruned.
        if marked and discovered:
            return
        for child_dir in child_dirs:
            walk(child_dir, discovered=True)

    walk(scan_dir, discovered=False)
    return found


def mtime_from_manifest(iso_str: str) -> datetime:
    """Parse a manifest mtime ISO-8601 string to a timezone-aware UTC datetime.

    Manifests always emit UTC with either `+00:00` or `Z` suffix. We accept
    both; `datetime.fromisoformat` on 3.11+ handles `Z` natively.
    """
    return datetime.fromisoformat(iso_str)


def mtime_from_path(path: Path) -> datetime:
    """Return the file's mtime as a timezone-aware UTC datetime.

    Matches the canonical form used by walk_claude_source/walk_generic_source
    so comparisons against manifest-recorded mtimes are apples-to-apples.
    """
    stat = path.stat()
    return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)


def _is_excluded(
    rel_path: str,
    exclude_patterns: list[str] | None = None,
    skip_prefixes: list[str] | None = None,
) -> bool:
    """Check if a relative path matches any exclude pattern.

    The hardcoded EXCLUDED list covers universal junk (.git, *.tmp, etc.).
    Per-source globs in `exclude_patterns` extend the filter for source-
    specific artifacts (e.g. gstack's per-machine repo-mode.json caches).
    Per-source globs are matched against the FULL relative path (so users
    can scope to subtrees like `projects/*/repo-mode.json`), unlike the
    EXCLUDED list which mixes basename (`*.pyc`) and dir-segment (`.git/`)
    semantics for backward compatibility.
    ``skip_prefixes`` are path prefixes (not globs) for marker-skip dirs.
    """
    parts = rel_path.split("/")
    filename = parts[-1]
    # Conflict copies stay local-only — uploading them would defeat the
    # Syncthing-style preservation model (one local conflict turns into N
    # cross-device conflict files).
    if is_conflict_filename(filename):
        return True
    if _under_skip_prefix(rel_path, skip_prefixes):
        return True
    for pattern in EXCLUDED:
        # Directory patterns (ending with /)
        if pattern.endswith("/"):
            dir_name = pattern.rstrip("/")
            if dir_name in parts:
                return True
        # File patterns
        else:
            if fnmatch.fnmatch(filename, pattern):
                return True
    if exclude_patterns:
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
    return False


_HASH_CHUNK = 65_536


class _FileTooLarge(Exception):
    """A newly encountered file is already over the configured size cap."""

    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


@dataclass(frozen=True)
class FileRevision:
    """One accepted open-file revision: digest, size, and mtime agree."""

    digest: str
    size: int
    mtime_iso: str
    st_dev: int
    st_ino: int
    st_mtime_ns: int
    st_ctime_ns: int
    data: bytes | None = None


def _rel_or_name(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def _mtime_iso_from_stat(st: os.stat_result) -> str:
    return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()


def _revision_signature(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _open_nofollow_nonblock(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return os.open(path, flags)


def path_has_descendant_symlink(
    path: Path,
    base: Path,
    *,
    strict: bool = False,
    source_name: str | None = None,
) -> bool:
    """True if any component strictly below ``base`` is a symlink.

    A symlinked source root is allowed. An unavailable probe refuses in
    strict mode instead of becoming an intentional omission.
    """
    try:
        relative = path.relative_to(base)
    except ValueError:
        # Not a descendant of this source root. Confinement/escape checks
        # belong to the caller; this helper only names descendant links.
        return False
    component = base
    for part in relative.parts:
        component /= part
        try:
            st = component.lstat()
        except FileNotFoundError:
            return False
        except OSError as e:
            if e.errno == errno.ENOTDIR:
                return False
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=_rel_or_name(path, base),
                        problem="could not be read",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
    return False


def _assert_revision_privacy(
    path: Path,
    base: Path | None,
    st: os.stat_result,
    *,
    source_name: str | None,
    source_type: str | None,
) -> None:
    if base is not None and path_has_descendant_symlink(
        path, base, strict=True, source_name=source_name
    ):
        raise SnapshotError(
            snapshot_refusal(
                source=source_name,
                rel_path=_rel_or_name(path, base),
                problem="is no longer a confined regular file",
                next_action=("Remove the descendant symlink or hard link, then run mm push."),
            )
        )
    if source_type == "grok" and st.st_nlink > 1:
        raise SnapshotError(
            snapshot_refusal(
                source=source_name,
                rel_path=_rel_or_name(path, base) if base is not None else path.name,
                problem="is no longer a confined regular file",
                next_action="Remove the extra hard link, then run mm push.",
            )
        )


def read_file_revision(
    path: Path,
    *,
    max_file_size: int,
    retain_bytes: bool,
    source_name: str | None = None,
    source_type: str | None = None,
    base: Path | None = None,
) -> FileRevision:
    """Read one regular-file revision from a single no-follow descriptor.

    Open with ``O_NOFOLLOW`` / ``O_NONBLOCK`` where the platform supports
    them, require a regular file, and refuse if device/inode/size/mtime_ns/
    ctime_ns or the pathname identity change during the read. Bytes read
    are bounded by ``max_file_size``. An already-oversized file raises
    ``_FileTooLarge`` so callers can keep the established skip policy.
    """
    rel = _rel_or_name(path, base) if base is not None else path.name

    def _refuse(problem: str, next_action: str, exc: BaseException | None = None) -> NoReturn:
        cause = os_error_cause(exc) if exc is not None else None
        err = SnapshotError(
            snapshot_refusal(
                source=source_name,
                rel_path=rel,
                problem=problem,
                cause=cause,
                next_action=next_action,
            )
        )
        if exc is not None:
            raise err from exc
        raise err

    try:
        pre_path_st = path.lstat()
    except FileNotFoundError as e:
        _refuse(
            "disappeared while being read",
            "Wait for the writer to finish, then run mm push.",
            e,
        )
    except OSError as e:
        _refuse("could not be read", "Restore read access, then run mm push.", e)

    if stat.S_ISLNK(pre_path_st.st_mode):
        _refuse(
            "is no longer a confined regular file",
            "Remove the descendant symlink, then run mm push.",
        )
    if not stat.S_ISREG(pre_path_st.st_mode):
        _refuse(
            "is no longer a confined regular file",
            "Restore the regular file, then run mm push.",
        )

    try:
        fd = _open_nofollow_nonblock(path)
    except FileNotFoundError as e:
        _refuse(
            "disappeared while being read",
            "Wait for the writer to finish, then run mm push.",
            e,
        )
    except OSError as e:
        _refuse("could not be read", "Restore read access, then run mm push.", e)

    try:
        try:
            pre_fd_st = os.fstat(fd)
        except OSError as e:
            _refuse("could not be read", "Restore read access, then run mm push.", e)
        if not stat.S_ISREG(pre_fd_st.st_mode):
            _refuse(
                "is no longer a confined regular file",
                "Restore the regular file, then run mm push.",
            )
        if (pre_fd_st.st_dev, pre_fd_st.st_ino) != (pre_path_st.st_dev, pre_path_st.st_ino):
            _refuse(
                "changed while being read",
                "Wait for the writer to finish, then run mm push.",
            )
        if pre_fd_st.st_size > max_file_size:
            raise _FileTooLarge(pre_fd_st.st_size)
        _assert_revision_privacy(
            path, base, pre_fd_st, source_name=source_name, source_type=source_type
        )

        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        remaining = max_file_size + 1
        while remaining > 0:
            try:
                chunk = os.read(fd, min(_HASH_CHUNK, remaining))
            except OSError as e:
                _refuse("could not be read", "Restore read access, then run mm push.", e)
            if not chunk:
                break
            hasher.update(chunk)
            if retain_bytes:
                chunks.append(chunk)
            remaining -= len(chunk)
        if remaining <= 0:
            _refuse(
                "grew beyond max_file_size while being read",
                "Wait for the writer to finish, or raise sync.max_file_size, then run mm push.",
            )
        hashed = (max_file_size + 1) - remaining
        if hashed != pre_fd_st.st_size:
            _refuse(
                "changed while being read",
                "Wait for the writer to finish, then run mm push.",
            )

        try:
            post_fd_st = os.fstat(fd)
            post_path_st = path.lstat()
        except FileNotFoundError as e:
            _refuse(
                "disappeared while being read",
                "Wait for the writer to finish, then run mm push.",
                e,
            )
        except OSError as e:
            _refuse("could not be read", "Restore read access, then run mm push.", e)
        if _revision_signature(pre_fd_st) != _revision_signature(post_fd_st):
            _refuse(
                "changed while being read",
                "Wait for the writer to finish, then run mm push.",
            )
        if (post_path_st.st_dev, post_path_st.st_ino) != (pre_fd_st.st_dev, pre_fd_st.st_ino):
            _refuse(
                "changed while being read",
                "Wait for the writer to finish, then run mm push.",
            )
        if not stat.S_ISREG(post_path_st.st_mode):
            _refuse(
                "is no longer a confined regular file",
                "Restore the regular file, then run mm push.",
            )
        _assert_revision_privacy(
            path, base, post_fd_st, source_name=source_name, source_type=source_type
        )
        data = b"".join(chunks) if retain_bytes else None
        return FileRevision(
            digest=hasher.hexdigest(),
            size=pre_fd_st.st_size,
            mtime_iso=_mtime_iso_from_stat(pre_fd_st),
            st_dev=pre_fd_st.st_dev,
            st_ino=pre_fd_st.st_ino,
            st_mtime_ns=pre_fd_st.st_mtime_ns,
            st_ctime_ns=pre_fd_st.st_ctime_ns,
            data=data,
        )
    finally:
        os.close(fd)


def hash_file(path: Path) -> str:
    """SHA-256 hash a file. Reads in chunks to handle large files."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_and_hash(path: Path) -> tuple[bytes, str]:
    """Read entire file into memory and SHA-256 hash it in one shot.

    Returns (file_bytes, hex_digest). This avoids the race condition where
    hash_file() and a later read_bytes() could see different content if
    the file is modified between the two reads.
    """
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return data, digest


def _dir_is_policy_skipped(rel_dir: str, skip_prefixes: list[str] | None) -> bool:
    """True only for proven directory-level selection exclusions, never file globs."""
    if not rel_dir:
        return False
    if _under_skip_prefix(rel_dir, skip_prefixes):
        return True
    parts = rel_dir.split("/")
    for pattern in EXCLUDED:
        if pattern.endswith("/"):
            dir_name = pattern.rstrip("/")
            if dir_name in parts:
                return True
    return False


def _collect_regular_files_scandir(
    start: Path,
    base: Path,
    *,
    strict: bool,
    source_name: str | None,
    skip_prefixes: list[str] | None = None,
) -> list[Path]:
    """Explicit ``os.scandir`` walk that never follows descendant symlinks."""
    collected: list[Path] = []

    def walk(directory: Path, *, discovered: bool) -> None:
        try:
            rel_dir = directory.relative_to(base).as_posix() if directory != base else ""
        except ValueError:
            rel_dir = directory.name
        if rel_dir and _dir_is_policy_skipped(rel_dir, skip_prefixes):
            return
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except FileNotFoundError as e:
            if discovered and strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=rel_dir or _rel_or_name(directory, base),
                        problem="disappeared while being read",
                        next_action="Wait for the writer to finish, then run mm push.",
                    )
                ) from e
            return
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=rel_dir or _rel_or_name(directory, base),
                        problem="could not be enumerated",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            return
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError as e:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=_rel_or_name(Path(entry.path), base),
                            problem="disappeared while being read",
                            next_action="Wait for the writer to finish, then run mm push.",
                        )
                    ) from e
                continue
            except OSError as e:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=_rel_or_name(Path(entry.path), base),
                            problem="could not be read",
                            cause=os_error_cause(e),
                            next_action="Restore read access, then run mm push.",
                        )
                    ) from e
                continue
            child = Path(entry.path)
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_ISDIR(st.st_mode):
                walk(child, discovered=True)
            elif stat.S_ISREG(st.st_mode):
                collected.append(child)

    walk(start, discovered=False)
    collected.sort(key=lambda p: str(p.relative_to(base)) if p.is_relative_to(base) else str(p))
    return collected


def _lstat_or_none(
    path: Path,
    *,
    strict: bool,
    source_name: str | None,
    rel_path: str,
) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as e:
        if strict:
            raise SnapshotError(
                snapshot_refusal(
                    source=source_name,
                    rel_path=rel_path,
                    problem="could not be read",
                    cause=os_error_cause(e),
                    next_action="Restore read access, then run mm push.",
                )
            ) from e
        return None


# Per-file walker pipeline (single source of truth, called by
# walk_claude_source, walk_generic_source, and walk_grok_source):
#
#   path ──► relative_to(base) ──► rel
#                                   │
#                          _is_excluded(rel)? ──► None
#                                   │
#                 strict: one descriptor revision ─── OSError/change ──► SnapshotError
#                 diagnostic: stat()/hash_file() skip on permission/read/size
#                                   │
#                          (rel, {sha256, size, mtime})
#
# Diagnostic "None" returns are NOT causal evidence of deletion — the
# permissive walker is intentionally lossy. Publishing scans use strict
# mode so an incomplete observation cannot become a tombstone. See SPEC.md
# "Merge invariants."
def _record_file(
    path: Path,
    base: Path,
    max_file_size: int,
    on_skip: Any = None,
    exclude_patterns: list[str] | None = None,
    skip_prefixes: list[str] | None = None,
    *,
    strict: bool = False,
    source_name: str | None = None,
    source_type: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Apply the per-file walker pipeline to `path` under `base`.

    Returns (rel_path, {"sha256", "size", "mtime"}) on success, or None if
    the file should be skipped. Skip reasons are reported via `on_skip(rel,
    reason)` — the exact reason strings ("permission denied", "read error",
    f"exceeds max_file_size ({size_mb:.1f}MB)") are load-bearing because
    cli.py surfaces them in verbose walker output. Do not reshape them
    without updating callers and tests.

    `exclude_patterns` extends the global EXCLUDED list with per-source
    fnmatch globs evaluated against the relative path. Excluded paths
    return None silently (no on_skip) — exclusion is intentional, not
    a degradation signal. Strict mode raises SnapshotError on read,
    identity, and privacy failures; a newly oversized file still skips.
    """
    rel = str(path.relative_to(base))

    if _is_excluded(rel, exclude_patterns, skip_prefixes):
        return None

    if strict:
        try:
            revision = read_file_revision(
                path,
                max_file_size=max_file_size,
                retain_bytes=False,
                source_name=source_name,
                source_type=source_type,
                base=base,
            )
        except _FileTooLarge as e:
            if on_skip:
                size_mb = e.size / (1024 * 1024)
                on_skip(rel, f"exceeds max_file_size ({size_mb:.1f}MB)")
            return None
        return rel, {
            "sha256": revision.digest,
            "size": revision.size,
            "mtime": revision.mtime_iso,
        }

    try:
        file_stat = path.stat()
    except PermissionError:
        if on_skip:
            on_skip(rel, "permission denied")
        return None

    if file_stat.st_size > max_file_size:
        if on_skip:
            size_mb = file_stat.st_size / (1024 * 1024)
            on_skip(rel, f"exceeds max_file_size ({size_mb:.1f}MB)")
        return None

    try:
        sha = hash_file(path)
    except (PermissionError, OSError):
        if on_skip:
            on_skip(rel, "read error")
        return None

    return rel, {
        "sha256": sha,
        "size": file_stat.st_size,
        "mtime": datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def walk_claude_source(
    base_dir: str | Path,
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
    exclude_patterns: list[str] | None = None,
    *,
    strict: bool = False,
    source_name: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Walk a Claude ~/.claude directory and build the files dict.

    Scans only projects/*/memory/ and projects/*/todos/ subdirectories.

    Args:
        base_dir: Root directory to walk (e.g., ~/.claude)
        max_file_size: Skip files larger than this (bytes). Default 50MB.
        on_skip: Optional callback(path, reason) for skipped files.
        exclude_patterns: Optional per-source fnmatch globs that extend the
            hardcoded EXCLUDED list. Matched against the relative path.
        strict: Publishing scans refuse incomplete observations.
        source_name: Source name for strict SnapshotError location.

    Returns:
        Dict mapping relative paths to {sha256, size, mtime}.
    """
    base = Path(base_dir).expanduser().resolve()
    projects_dir = base / "projects"
    try:
        projects_st = projects_dir.lstat()
    except FileNotFoundError:
        return {}
    except OSError as e:
        if strict:
            raise SnapshotError(
                snapshot_refusal(
                    source=source_name,
                    rel_path="projects",
                    problem="could not be read",
                    cause=os_error_cause(e),
                    next_action="Restore read access, then run mm push.",
                )
            ) from e
        return {}
    if stat.S_ISLNK(projects_st.st_mode):
        return {}
    if not stat.S_ISDIR(projects_st.st_mode):
        return {}

    files: dict[str, dict[str, Any]] = {}
    scan_dirs: list[Path] = []
    if strict:
        try:
            with os.scandir(projects_dir) as iterator:
                project_entries = list(iterator)
        except OSError as e:
            raise SnapshotError(
                snapshot_refusal(
                    source=source_name,
                    rel_path="projects",
                    problem="could not be enumerated",
                    cause=os_error_cause(e),
                    next_action="Restore read access, then run mm push.",
                )
            ) from e
        project_entries.sort(key=lambda entry: entry.name)
        for entry in project_entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError as e:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=_rel_or_name(Path(entry.path), base),
                        problem="disappeared while being read",
                        next_action="Wait for the writer to finish, then run mm push.",
                    )
                ) from e
            except OSError as e:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=_rel_or_name(Path(entry.path), base),
                        problem="could not be read",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                continue
            project_dir = Path(entry.path)
            for subdir_name in SYNCED_SUBDIRS:
                subdir = project_dir / subdir_name
                sub_st = _lstat_or_none(
                    subdir,
                    strict=True,
                    source_name=source_name,
                    rel_path=_rel_or_name(subdir, base),
                )
                if (
                    sub_st is None
                    or stat.S_ISLNK(sub_st.st_mode)
                    or not stat.S_ISDIR(sub_st.st_mode)
                ):
                    continue
                scan_dirs.append(subdir)
        for scan_dir in scan_dirs:
            for path in _collect_regular_files_scandir(
                scan_dir, base, strict=True, source_name=source_name
            ):
                if result := _record_file(
                    path,
                    base,
                    max_file_size,
                    on_skip,
                    exclude_patterns,
                    strict=True,
                    source_name=source_name,
                    source_type="claude",
                ):
                    rel, info = result
                    files[rel] = info
        return files

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for subdir_name in SYNCED_SUBDIRS:
            subdir = project_dir / subdir_name
            if subdir.exists() and subdir.is_dir():
                scan_dirs.append(subdir)

    for scan_dir in scan_dirs:
        for path in scan_dir.rglob("*"):
            if not path.is_file():
                continue
            if result := _record_file(path, base, max_file_size, on_skip, exclude_patterns):
                rel, info = result
                files[rel] = info

    return files


def walk_grok_source(
    source_config: dict[str, Any],
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
    *,
    strict: bool = False,
) -> dict[str, dict[str, Any]]:
    """Walk a Grok home and build the files dict.

    Scans only the hardcoded customization dirs at the source root
    (``GROK_SYNCED_SUBDIRS``). Sessions, credentials, vendor trees, and
    ``config.toml`` are never entered. Missing dirs are a no-op.
    """
    source_name = source_config.get("name")
    base = Path(source_config["path"]).expanduser().resolve()
    try:
        base.stat()
    except FileNotFoundError:
        return {}
    except OSError as e:
        if strict:
            raise SnapshotError(
                snapshot_refusal(
                    source=source_name,
                    problem="could not be read",
                    cause=os_error_cause(e),
                    next_action="Restore read access, then run mm push.",
                )
            ) from e
        return {}

    extra = source_config.get("exclude_patterns") or []
    exclude_patterns = [*GROK_EXCLUDE_PATTERNS, *extra]

    files: dict[str, dict[str, Any]] = {}
    collected_paths: list[Path] = []

    for dir_name in GROK_SYNCED_SUBDIRS:
        scan_dir = base / dir_name
        st = _lstat_or_none(scan_dir, strict=strict, source_name=source_name, rel_path=dir_name)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode):
            if on_skip:
                on_skip(str(scan_dir), "symlink")
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        if strict:
            collected_paths.extend(
                _collect_regular_files_scandir(scan_dir, base, strict=True, source_name=source_name)
            )
        else:
            for path in scan_dir.rglob("*"):
                if path.is_file():
                    collected_paths.append(path)

    collected_paths.sort(
        key=lambda p: str(p.relative_to(base)) if p.is_relative_to(base) else str(p)
    )
    seen: set[tuple[int, int]] = set()
    for path in collected_paths:
        if path_has_descendant_symlink(path, base, strict=strict, source_name=source_name):
            if on_skip:
                on_skip(str(path), "symlink")
            continue
        try:
            st = path.lstat() if strict else path.stat()
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=_rel_or_name(path, base),
                        problem="could not be read",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            continue
        if stat.S_ISLNK(st.st_mode):
            if on_skip:
                on_skip(str(path), "symlink")
            continue
        # A hard link can make a credential or session file reachable from an
        # allowlisted directory without introducing a symlink component.  The
        # Grok boundary is privacy-critical, so accept only singly-linked
        # regular files rather than trying to infer every other inode path.
        if st.st_nlink > 1:
            if on_skip:
                on_skip(str(path), "hardlink")
            continue
        identity = (st.st_dev, st.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        if result := _record_file(
            path,
            base,
            max_file_size,
            on_skip,
            exclude_patterns,
            strict=strict,
            source_name=source_name,
            source_type="grok",
        ):
            rel, info = result
            files[rel] = info

    return files


def walk_generic_source(
    source_config: dict[str, Any],
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
    *,
    strict: bool = False,
) -> dict[str, dict[str, Any]]:
    """Walk a generic source directory with configurable include_dirs/include_files.

    Args:
        source_config: Dict with keys:
            path: Base directory path (supports ~)
            include_dirs: List of directory names to walk recursively
            include_files: List of filenames to check at root level
            exclude_patterns: Optional fnmatch globs evaluated against the
                relative path; matches are silently skipped. Directories
                containing `.extend-root` are also skipped (see
                `marker_skip_globs`); that skip is a path-prefix match so
                it does not generate deletion tombstones.
        max_file_size: Skip files larger than this (bytes). Default 50MB.
        on_skip: Optional callback(path, reason) for skipped files.
        strict: Publishing scans refuse incomplete observations.

    Returns:
        Dict mapping relative paths (from base) to {sha256, size, mtime}.
    """
    source_name = source_config.get("name")
    base = Path(source_config["path"]).expanduser().resolve()
    try:
        base.stat()
    except FileNotFoundError:
        return {}
    except OSError as e:
        if strict:
            raise SnapshotError(
                snapshot_refusal(
                    source=source_name,
                    problem="could not be read",
                    cause=os_error_cause(e),
                    next_action="Restore read access, then run mm push.",
                )
            ) from e
        return {}

    include_dirs: list[str] = source_config.get("include_dirs", [])
    include_files: list[str] = source_config.get("include_files", [])
    exclude_patterns: list[str] = list(source_config.get("exclude_patterns") or [])
    # Marker-aware skip: directories containing `.extend-root` drop out of
    # the local manifest the same way a glob would, but as PATH PREFIXES
    # not fnmatch globs. `_build_exclude_map` threads the same prefixes
    # for tombstone suppression. Do NOT skip this — a walker-only skip
    # would tombstone previously-synced files on the next push.
    skip_prefixes = marker_skip_globs(source_config, strict=strict)

    files: dict[str, dict[str, Any]] = {}
    collected_paths: list[Path] = []

    for dir_name in include_dirs:
        scan_dir = base / dir_name
        st = _lstat_or_none(scan_dir, strict=strict, source_name=source_name, rel_path=dir_name)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode):
            if on_skip:
                on_skip(str(scan_dir), "symlink")
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        if strict:
            collected_paths.extend(
                _collect_regular_files_scandir(
                    scan_dir,
                    base,
                    strict=True,
                    source_name=source_name,
                    skip_prefixes=skip_prefixes,
                )
            )
        else:
            for path in scan_dir.rglob("*"):
                if path.is_file():
                    collected_paths.append(path)

    for filename in include_files:
        path = base / filename
        st = _lstat_or_none(path, strict=strict, source_name=source_name, rel_path=filename)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode):
            if on_skip:
                on_skip(str(path), "symlink")
            continue
        if stat.S_ISREG(st.st_mode):
            collected_paths.append(path)

    # Dedup by filesystem identity (Group 7 preflight #2 + D6). When an
    # include_files entry sits inside an include_dirs directory (e.g. user
    # writes include_files: ["projects/notes.md"] AND include_dirs:
    # ["projects"]), the same on-disk file lands in collected_paths twice.
    # Without dedup, _record_file hashes it twice — wasted CPU AND, on
    # case-insensitive volumes (APFS default) with case-mismatched config,
    # produces two distinct rel keys for one inode. (st_dev, st_ino) is
    # true filesystem identity — works on macOS (case-insensitive), Linux
    # (case-sensitive), symlinks, hard links — and never normalizes the
    # manifest key shape, so cross-platform peer compatibility is
    # preserved (codex outside-voice finding #5).
    #
    # Sort by relative-to-base path before dedup so the rel-key kept on
    # hardlink/symlink overlap is deterministic across runs and across
    # machines (rglob iteration order is FS-dependent on macOS APFS).
    # Without this sort, two peers walking the same tree could pick
    # different rel keys for the same inode, generating phantom
    # add/delete churn in the manifest diff.
    collected_paths.sort(
        key=lambda p: str(p.relative_to(base)) if p.is_relative_to(base) else str(p)
    )
    seen: set[tuple[int, int]] = set()
    for path in collected_paths:
        # A symlink is local routing, not syncable content. Publishing its
        # target would produce a manifest entry the pull path cannot safely
        # apply without either traversing outside the source root or replacing
        # the link with a regular file.
        if path_has_descendant_symlink(path, base, strict=strict, source_name=source_name):
            if on_skip:
                on_skip(str(path), "symlink")
            continue
        try:
            st = path.lstat() if strict else path.stat()
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=_rel_or_name(path, base),
                        problem="could not be read",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            continue  # consistent with _record_file's tolerance for races
        if stat.S_ISLNK(st.st_mode):
            if on_skip:
                on_skip(str(path), "symlink")
            continue
        identity = (st.st_dev, st.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        if result := _record_file(
            path,
            base,
            max_file_size,
            on_skip,
            exclude_patterns,
            skip_prefixes,
            strict=strict,
            source_name=source_name,
            source_type="generic",
        ):
            rel, info = result
            files[rel] = info

    return files


def walk_source(
    source_config: dict[str, Any],
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
    *,
    strict: bool = False,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Dispatch to the appropriate walker based on source type.

    Args:
        source_config: Dict with at least "type" and "path" keys.
            type="claude" -> walk_claude_source
            type="grok" -> walk_grok_source
            type="generic" -> walk_generic_source
        max_file_size: Skip files larger than this (bytes).
        on_skip: Optional callback(path, reason) for skipped files.
        strict: Publishing scans refuse incomplete observations.

    Returns:
        Tuple of (resolved_base_path_str, files_dict).
    """
    source_type = source_config.get("type", "claude")
    base_path = str(Path(source_config["path"]).expanduser().resolve())

    if source_type == "claude":
        files = walk_claude_source(
            source_config["path"],
            max_file_size,
            on_skip,
            exclude_patterns=source_config.get("exclude_patterns"),
            strict=strict,
            source_name=source_config.get("name"),
        )
    elif source_type == "grok":
        files = walk_grok_source(source_config, max_file_size, on_skip, strict=strict)
    elif source_type == "generic":
        files = walk_generic_source(source_config, max_file_size, on_skip, strict=strict)
    else:
        raise ManifestError(f"manifest: unknown source type '{source_type}'")

    return base_path, files


def build_manifest_v2(
    device_id: str,
    device_name: str,
    sources_configs: list[dict[str, Any]],
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Build a v2 manifest with multiple sources.

    Args:
        device_id: Unique device identifier.
        device_name: Human-readable device name.
        sources_configs: List of source config dicts, each with at least
            "name", "type", and "path" keys.
        max_file_size: Skip files larger than this (bytes).
        on_skip: Optional callback(path, reason) for skipped files.
        strict: Publishing scans refuse incomplete observations.

    Returns:
        v2 manifest dict with a "sources" dict keyed by source name.
    """
    sources: dict[str, dict[str, Any]] = {}

    for src_cfg in sources_configs:
        name = src_cfg["name"]
        base_path, files = walk_source(src_cfg, max_file_size, on_skip, strict=strict)
        sources[name] = {
            "base_path": base_path,
            "files": files,
        }

    return {
        "device_id": device_id,
        "device_name": device_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
    }


def normalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Ensure a manifest has the v2 "sources" structure and tombstones.

    If the manifest already has "sources", return as-is (with the redundant
    top-level "files" key scrubbed — see below).
    If it only has "files" (v1 format), wrap the files into a
    claude source entry under "sources".

    Always ensures "tombstones" key exists (empty dict for old manifests).

    During the v1→v2 promotion, bare-path tombstone keys are migrated to
    `claude:<path>` form. This is defensive: no shipped mm version emits
    bare-path tombstones (they were introduced AFTER the v2 sources format),
    but hand-edited v1 manifests, test fixtures, or external tooling could.
    For manifests that already have `sources`, we do NOT speculate on the
    meaning of unknown key shapes — `is_tombstoned` returning False is the
    same safe default as today.

    Top-level "files" scrub: pre-Track-1B v2 writers emitted a redundant
    top-level "files" mirror of the claude source. Strip it unconditionally
    on both the v1 promotion and v2 passthrough paths so a shallow dict-copy
    (e.g. `_merge_manifests` at cli.py:553) can't carry a stale mirror
    through when merging an old on-disk manifest. The v1 payload is not
    lost: promotion has already copied it into `sources.claude.files`
    before the scrub runs. Unconditional scrub also makes `normalize_manifest`
    idempotent on v1 input — important for the fuzz suite.

    Returns:
        The manifest dict (mutated in place) with "sources" and "tombstones" guaranteed.
    """
    is_v1_promotion = "sources" not in manifest
    if is_v1_promotion:
        manifest["sources"] = {
            "claude": {
                "base_path": manifest.get("base_path", ""),
                "files": manifest.get("files", {}),
            }
        }
    # Single enforcement point: strip the redundant top-level "files" on
    # every normalize path. v1 promotion has already copied the payload
    # into `sources.claude.files` above; v2 passthrough drops the stale
    # mirror that pre-Track-1B writers emitted. Running unconditionally
    # makes normalize idempotent on v1 input and closes the dict-copy
    # carry-forward path in _merge_manifests at cli.py:553.
    manifest.pop("files", None)

    if "tombstones" not in manifest:
        manifest["tombstones"] = {}
    elif is_v1_promotion and isinstance(manifest["tombstones"], dict):
        # v1 → v2 was unambiguously claude-only; migrate bare-path keys.
        # (Non-dict tombstones are caught by load_manifest's shape check;
        # defensive guard here is for direct normalize_manifest callers.)
        migrated: dict[str, Any] = {}
        for key, info in manifest["tombstones"].items():
            if isinstance(key, str) and ":" not in key:
                migrated[f"claude:{key}"] = info
            else:
                migrated[key] = info
        manifest["tombstones"] = migrated

    return manifest


def serialize_manifest(manifest: dict[str, Any]) -> bytes:
    """Serialize manifest to JSON bytes."""
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


def deserialize_manifest(data: bytes) -> dict[str, Any]:
    """Pure JSON-bytes → dict decode. Use `load_manifest` for the
    decode + normalize pipeline that downstream consumers expect.
    """
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ManifestError(f"manifest: failed to parse — {e}") from e


def _validate_rel_path(rel: Any, *, where: str) -> None:
    """Reject a manifest rel_path that could escape its source's base dir.

    Threat model: a peer with the storage passphrase can mint an authenticated
    manifest whose ``sources.<name>.files`` keys are free-form UTF-8. Without
    this guard, `_download_and_apply` builds ``local_path = base_path / rel``
    and writes decrypted bytes there — Python's `Path /` follows ``..``
    segments and lets an absolute right-hand side override the base entirely
    (``Path('/base') / '/etc/passwd' == Path('/etc/passwd')``). A crafted key
    like ``'../../.ssh/authorized_keys'`` lands attacker-chosen bytes outside
    the source root and escalates passphrase + storage-write into local code
    execution on every fleet device that pulls.

    Mirrors `storage/keys.py:_validate_component`'s style for the sibling
    sha256 defense — sha is hex-bounded, rel_path is free-form, so the
    rel_path surface is strictly more reachable. Raise loudly at the load
    boundary; `_fetch_remote_manifest` already catches ManifestError and
    falls through to sidecar/peer recovery.
    """
    if not isinstance(rel, str):
        raise ManifestError(f"manifest: {where} must be a string")
    if not rel:
        raise ManifestError(f"manifest: {where} must be non-empty")
    if "\x00" in rel:
        raise ManifestError(f"manifest: {where} must not contain null bytes")
    # Reject Windows-style absolute paths and leading separators uniformly.
    # POSIX `Path('/base') / '/etc/x'` returns `/etc/x` — absolute RHS wins.
    if rel.startswith("/") or rel.startswith("\\"):
        raise ManifestError(f"manifest: {where} must not be absolute (got {rel!r})")
    # Drive-letter form ("C:foo", "C:\\foo"). macOS-only project today, but
    # this is cheap belt-and-braces — `Path` semantics on Windows would
    # also let drive-letter strings escape.
    if len(rel) >= 2 and rel[1] == ":" and rel[0].isalpha():
        raise ManifestError(f"manifest: {where} must not contain a drive letter (got {rel!r})")
    # Per-segment ".." check. Honest writers (manifest.walk_*) build
    # rel keys via `path.relative_to(base)`, which NEVER produces ".."
    # segments — only an attacker-crafted manifest would. Reject any
    # ".." segment under either separator (forward or back slash) so
    # mixed spellings like `a\\..\\b` or `a/..\\b` are caught uniformly.
    # We do NOT pre-`normpath`: posixpath.normpath collapses `a/..` to
    # `.` and would silently drop the suspicious segment before the
    # check, defeating the defense.
    for segment in rel.replace("\\", "/").split("/"):
        if segment == "..":
            raise ManifestError(f"manifest: {where} must not contain '..' segments (got {rel!r})")


def load_manifest(data: bytes) -> dict[str, Any]:
    """Decode JSON bytes into a v2-normalized manifest dict.

    Single load boundary for every manifest path (remote fetch, sidecar
    recovery, test fixtures). Guarantees the returned dict has dict-typed
    `sources` and `tombstones`, each source entry has a dict-typed `files`,
    and each tombstone value is a dict. Every key in `sources[*].files` is
    confined to a relative path inside its source — no '..' segments, no
    absolute paths, no null bytes (see `_validate_rel_path`). Callers may
    rely on these invariants.

    Raises ManifestError on bad bytes, non-dict top-level JSON, any inner-
    shape violation, or any rel_path that could escape its source root.
    Enforcing the full shape at the load boundary turns a downstream
    AttributeError (deep in collect_tombstones, _merge_manifests, or the
    diff loop) into a clean recoverable error at the front door —
    `_fetch_remote_manifest` already catches ManifestError and falls
    through to the sidecar/peer recovery chain.
    """
    parsed = deserialize_manifest(data)
    if not isinstance(parsed, dict):
        raise ManifestError("manifest: top-level JSON value is not an object")
    normalized = normalize_manifest(parsed)
    sources = normalized.get("sources")
    tombstones = normalized.get("tombstones")
    if not isinstance(sources, dict):
        raise ManifestError("manifest: 'sources' must be an object")
    if not isinstance(tombstones, dict):
        raise ManifestError("manifest: 'tombstones' must be an object")
    for src_name, src_data in sources.items():
        if not isinstance(src_data, dict):
            raise ManifestError(f"manifest: sources[{src_name!r}] must be an object")
        files = src_data.get("files", {})
        if not isinstance(files, dict):
            raise ManifestError(f"manifest: sources[{src_name!r}]['files'] must be an object")
        for rel in files.keys():
            _validate_rel_path(rel, where=f"sources[{src_name!r}]['files'] key")
    for key, info in tombstones.items():
        if not isinstance(info, dict):
            raise ManifestError(f"manifest: tombstones[{key!r}] must be an object")
        # Tombstone keys are `<src>:<rel_path>` post-normalize. Validate the
        # path part — a tombstone with `..` doesn't drive deletion (pull is
        # additive-only), but a malformed key shouldn't survive the load
        # boundary either, and a tombstone keyed on `..` could otherwise
        # mask legitimate files in `is_tombstoned` checks.
        if isinstance(key, str) and ":" in key:
            _, _, rel = key.partition(":")
            if rel:
                _validate_rel_path(rel, where=f"tombstones[{key!r}] path part")
    return normalized


@dataclass(eq=False, repr=False)
class DiffResult:
    """Result of diffing two file dicts.

    `eq=False` preserves identity-based equality (and default-id-based hashing)
    from the pre-Track-1B hand-written class. Nothing in the codebase relies
    on structural equality or hashability today, but locking in identity
    semantics keeps the shape change purely additive (count-based __repr__
    + type hints + default_factory) rather than smuggling in a behavioral
    drift. Flip to `eq=True` (or `frozen=True`) only with intent and tests
    covering the new contract.
    """

    new: dict[str, dict] = field(default_factory=dict)
    modified: dict[str, dict] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.modified or self.deleted)

    def __repr__(self) -> str:
        # Count-format preserved from the pre-dataclass version: the default
        # dataclass repr dumps every dict entry, which on a 500-file manifest
        # is a 50KB line of log noise. Counts are what every caller actually
        # wants to see.
        return (
            f"DiffResult(new={len(self.new)}, modified={len(self.modified)}, "
            f"deleted={len(self.deleted)}, unchanged={len(self.unchanged)})"
        )


def diff_files(
    local_files: dict[str, dict],
    remote_files: dict[str, dict] | None = None,
) -> DiffResult:
    """Diff a local files dict against a remote files dict. Returns what changed.

    Truth-based: `local_files` is the source of truth.
    Files in local but not remote → new.
    Files in both but different hash → modified.
    Files in remote but not local → deleted.

    Arg-swap convention: the pull path (cli.py, `_pull_core`) intentionally
    calls `diff_files(remote_files, local_files)` with arguments swapped.
    Under the additive-pull model, that call's `new`/`modified` are files
    the puller should DOWNLOAD (present on remote, missing or stale locally)
    and `deleted` is ignored (additive pull never deletes local files).
    See also `test_additive_sync.py::TestAdditivePull`.
    """
    if remote_files is None:
        remote_files = {}

    new: dict[str, dict] = {}
    modified: dict[str, dict] = {}
    deleted: list[str] = []
    unchanged: list[str] = []

    for path, info in local_files.items():
        if path not in remote_files:
            new[path] = info
        elif info["sha256"] != remote_files[path]["sha256"]:
            modified[path] = info
        else:
            unchanged.append(path)

    for path in remote_files:
        if path not in local_files:
            deleted.append(path)

    return DiffResult(new=new, modified=modified, deleted=deleted, unchanged=unchanged)


# ── tombstones ───────────────────────────────────────────────────────

TOMBSTONE_TTL_DAYS = 30


def _is_active_tombstone(info: dict[str, Any], cutoff: datetime) -> bool:
    """True iff `info`'s `deleted_at` parses to a tz-aware datetime past `cutoff`.

    Single source of truth for "is this tombstone still live?" — shared by
    `generate_tombstones` (carry-forward) and `collect_tombstones` (fleet
    aggregation). Both sites previously duplicated the fromisoformat +
    tzinfo-None guard + cutoff compare + (ValueError, TypeError) handling.

    The naive-datetime → UTC guard is load-bearing: an older client may have
    written a timezone-naive `deleted_at`, and comparing a naive datetime
    against a tz-aware `cutoff` raises TypeError. We always repair naive
    inputs to UTC; we never silently succeed-with-wrong-offset.

    Returns False on any parse failure (unparseable ISO string, missing key,
    non-string value). Returning False means the caller drops the tombstone
    — the conservative choice, since a corrupt `deleted_at` could live
    forever otherwise.
    """
    try:
        ts = datetime.fromisoformat(info.get("deleted_at", ""))
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts > cutoff


def generate_tombstones(
    local_manifest: dict[str, Any],
    remote_manifest: dict[str, Any] | None,
    device_id: str,
) -> dict[str, dict[str, str]]:
    """Generate tombstones for files that disappeared since last push.

    Compares current local manifest against the previous remote manifest.
    Files that were in remote but are no longer in local get a tombstone.
    Existing non-expired tombstones from the remote manifest carry forward.

    Caller contract (load-bearing, enforced at runtime): `remote_manifest`
    MUST be v2-shaped — i.e. have a top-level `"sources"` key, either
    because it came through `load_manifest` or because it was hand-built
    v2 (e.g. the peer-fallback synthetic dict at `cli.py:_resolve_prior_manifest`).
    A dict with a top-level `"files"` key and no `"sources"` key (the v1
    shape) raises `ManifestError` at entry — previously this was silently
    promoted in-line via a positionally-broken `normalize_manifest` call
    at line 607, which (cross-model adversarial review, 2026-04-24)
    ran AFTER the carry-forward loop had already consumed tombstone keys
    AND was the only thing allowing v1 `"files"` dicts to produce
    `claude:<path>` tombstones during new-tombstone detection. Dropping
    it without a runtime guard would turn the latter into silent delete-
    propagation loss. We enforce instead of repair because every internal
    caller already routes through `load_manifest` (via `_fetch_remote_manifest`
    / `sidecar.read`) or hand-builds v2 shape, so a v1-shaped input is a
    bug at the call site, not something to silently paper over.

    Returns:
        Dict mapping relative paths to {"deleted_at": ISO timestamp, "device_id": str}.

    Raises:
        ManifestError: if `remote_manifest` is non-None and lacks a
            `"sources"` key (e.g. a raw v1-shaped dict that bypassed
            `load_manifest`).
    """
    if remote_manifest is not None and "sources" not in remote_manifest:
        raise ManifestError(
            "generate_tombstones: remote_manifest must be v2-normalized "
            "(missing 'sources' key). Route through load_manifest() first."
        )

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=TOMBSTONE_TTL_DAYS)
    now_iso = now.isoformat()
    tombstones: dict[str, dict[str, str]] = {}

    # Carry forward non-expired tombstones from remote manifest
    if remote_manifest:
        for path, info in remote_manifest.get("tombstones", {}).items():
            if _is_active_tombstone(info, cutoff):
                tombstones[path] = info

    # Detect new tombstones: files in remote manifest but not in local
    # Keys are "source:path" to prevent cross-source suppression
    if remote_manifest:
        local_sources = local_manifest.get("sources", {})
        remote_sources = remote_manifest.get("sources", {})

        for src_name, remote_src in remote_sources.items():
            local_src = local_sources.get(src_name, {"files": {}})
            local_files = local_src.get("files", {})
            remote_files = remote_src.get("files", {})

            for path in remote_files:
                key = f"{src_name}:{path}"
                if path not in local_files and key not in tombstones:
                    tombstones[key] = {
                        "deleted_at": now_iso,
                        "device_id": device_id,
                    }

    # Remove tombstones for files that exist locally again (un-delete)
    all_local_keys: set[str] = set()
    for src_name, src_data in local_manifest.get("sources", {}).items():
        for path in src_data.get("files", {}).keys():
            all_local_keys.add(f"{src_name}:{path}")
    tombstones = {key: info for key, info in tombstones.items() if key not in all_local_keys}

    return tombstones


def collect_tombstones(
    device_ids: list[str],
    fetch_manifest: Any,
) -> dict[str, dict[str, str]]:
    """Pre-collect all active tombstones from all device manifests.

    Args:
        device_ids: List of all device IDs.
        fetch_manifest: Callable(device_id) -> manifest dict or None.

    Returns:
        Dict mapping relative paths to tombstone info. For duplicates, the
        most recent tombstone wins.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=TOMBSTONE_TTL_DAYS)
    all_tombstones: dict[str, dict[str, str]] = {}

    for did in device_ids:
        manifest = fetch_manifest(did)
        if manifest is None:
            continue
        for path, info in manifest.get("tombstones", {}).items():
            if not _is_active_tombstone(info, cutoff):
                continue
            deleted_at = info.get("deleted_at", "")
            existing = all_tombstones.get(path)
            if existing is None or deleted_at > existing.get("deleted_at", ""):
                all_tombstones[path] = info

    return all_tombstones


def is_tombstoned(source: str, rel_path: str, tombstones: dict[str, dict[str, str]]) -> bool:
    """Check if a source:path has an active tombstone."""
    return f"{source}:{rel_path}" in tombstones
