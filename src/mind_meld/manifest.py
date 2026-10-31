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
import functools
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NoReturn

from mind_meld import projectid
from mind_meld.errors import (
    ManifestError,
    SnapshotChanged,
    SnapshotError,
    os_error_cause,
    snapshot_refusal,
)

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
# Chromium and Electron write this file at the root of every user-data
# directory, and the directory's name varies (`chrome-profile`,
# `signed-profile`, `.dev/user-data`), so detection is by marker, never by
# folder name. The containing directory is skipped like an `.extend-root`
# one, minus the include-root exemption; see marker_skip_globs.
BROWSER_PROFILE_MARKER = "Local State"
NESTED_REPO_SKIP_REASON = "nested git repository"
# Track 90A: a project folder whose name starts with "@" is a wire name, never a
# folder Claude created; the walker skips it (verbose-only).
UNPLACEABLE_SKIP_REASON = projectid.UNPLACEABLE_SKIP_REASON
CLAUDE_SOURCE_NAME = "claude"


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

# Muse memory tree. Hardcoded the same way: the data root also holds
# sessions/, the session index, bundled skills and plugin caches, and
# ~/.config/muse holds auth.json — none of that is sync data. Only
# memory/ (personal/ + projects/<workspace>/) roams. Measured 2026-10-14
# against Muse 1.4.7's live ~/.local/share/muse.
MUSE_SYNCED_SUBDIRS = ["memory"]
# Muse keeps a machine-local PID lock beside the personal notes
# (``memory/personal/.muse-memory.lock``, ``pid=<n>``; observed on 1.4.7).
# Roaming it would hand every peer a foreign PID and a conflict sidecar
# each time another Muse process takes the lock. The pull-side filters
# (``cli._build_exclude_map``, ``cli._prove_omitted_paths_absent``) merge
# this list exactly as they merge Grok's.
MUSE_EXCLUDE_PATTERNS: list[str] = ["*.muse-memory.lock"]


def _under_skip_prefix(rel_path: str, prefixes: list[str] | None) -> bool:
    """True if ``rel_path`` is a skip-prefix or a file under one.

    Prefix match, not fnmatch: a directory literally named ``*`` with a
    marker must not exclude ``projects/myapp/foo``. ``.`` is the source
    root, so its marker excludes every relative path in that source.
    """
    if not prefixes:
        return False
    for prefix in prefixes:
        if prefix == "." or rel_path == prefix or rel_path.startswith(prefix + "/"):
            return True
    return False


def _directory_is_git_repo(
    directory: Path, base: Path, *, strict: bool, source_name: str | None
) -> bool:
    try:
        marker = (directory / ".git").lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as e:
        if strict:
            raise SnapshotError(
                snapshot_refusal(
                    source=source_name,
                    rel_path=_rel_or_name(directory, base),
                    problem="could not check for a nested git repository",
                    cause=os_error_cause(e),
                    next_action="Restore read access, then run mm push.",
                )
            ) from e
        return False
    return stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode)


def nested_repo_root(
    directory: Path, base: Path, *, strict: bool = False, source_name: str | None = None
) -> str | None:
    """Find the outermost checkout ancestor strictly below the source root.

    Probe components in order and stop at the first missing, non-directory or
    symlinked one: a checkout above a descendant link is still found, and
    nothing is probed through a link. Check ancestors too, since an include_dir
    or include_file can point directly into a checkout. The source root's own
    .git never changes its selection.
    """
    try:
        parts = directory.relative_to(base).parts
    except ValueError:
        return None
    current = base
    for part in parts:
        current /= part
        try:
            st = current.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=_rel_or_name(current, base),
                        problem="could not check for a nested git repository",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
        if _directory_is_git_repo(current, base, strict=strict, source_name=source_name):
            return current.relative_to(base).as_posix()
    return None


def nested_repo_skip_prefixes(
    source_config: dict[str, Any], *, strict: bool = False, skip_prefixes: list[str] | None = None
) -> list[str]:
    """Inventory selected directories for diag; prune checkouts without reading their files.

    Returns literal relative roots, never fnmatch patterns or persisted
    manifest metadata. Push and status take roots from their own walk; pull
    and diff probe incoming paths with ``nested_repo_roots_for_paths``.
    """
    source_name = source_config.get("name")
    base = Path(source_config["path"]).expanduser().resolve()
    source_type = source_config.get("type", "claude")
    roots: set[str] = set()
    scan_dirs: list[Path] = []
    if source_type == "generic" and skip_prefixes is None:
        skip_prefixes = marker_skip_globs(source_config, strict=strict)
    if _under_skip_prefix("", skip_prefixes):
        return []
    if source_type == "claude":
        projects = base / "projects"
        if path_has_descendant_symlink(projects, base, strict=strict, source_name=source_name):
            return []
        if root := nested_repo_root(projects, base, strict=strict, source_name=source_name):
            return [root]
        projects_st = _lstat_or_none(
            projects, strict=strict, source_name=source_name, rel_path="projects"
        )
        # Mirror walk_claude_source: a missing or non-directory projects is empty.
        if projects_st is None or not stat.S_ISDIR(projects_st.st_mode):
            return []
        try:
            with os.scandir(projects) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        project = Path(entry.path)
                        if _directory_is_git_repo(
                            project, base, strict=strict, source_name=source_name
                        ):
                            roots.add(project.relative_to(base).as_posix())
                        else:
                            scan_dirs.extend(project / subdir for subdir in SYNCED_SUBDIRS)
        except FileNotFoundError:
            return []
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path="projects",
                        problem="could not be enumerated",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
    else:
        dirs = (
            MUSE_SYNCED_SUBDIRS
            if source_type == "muse"
            else source_config.get("include_dirs") or []
        )
        scan_dirs = [base / name for name in dirs]
        include_files = source_config.get("include_files") or [] if source_type == "generic" else []
        for name in include_files:
            path = base / name
            if path.is_relative_to(base) and _under_skip_prefix(
                path.relative_to(base).as_posix(), skip_prefixes
            ):
                continue
            if root := nested_repo_root(path.parent, base, strict=strict, source_name=source_name):
                roots.add(root)
    for directory in scan_dirs:
        if directory.is_relative_to(base) and _under_skip_prefix(
            directory.relative_to(base).as_posix(), skip_prefixes
        ):
            continue
        # An absent or linked include inside a checkout still excludes that checkout.
        if root := nested_repo_root(directory, base, strict=strict, source_name=source_name):
            roots.add(root)
            continue
        if path_has_descendant_symlink(directory, base, strict=strict, source_name=source_name):
            continue
        st = _lstat_or_none(
            directory,
            strict=strict,
            source_name=source_name,
            rel_path=_rel_or_name(directory, base),
        )
        if st is None or not stat.S_ISDIR(st.st_mode):
            continue
        _collect_regular_files_scandir(
            directory,
            base,
            strict=strict,
            source_name=source_name,
            skip_prefixes=skip_prefixes,
            nested_repos=roots,
            collect_files=False,
        )
    return sorted(roots)


def nested_repo_roots_for_paths(source_config: dict[str, Any], rel_paths: Any) -> list[str]:
    """Find local checkout roots above incoming peer paths without walking local trees.

    Probes each local ancestor directory at most once. A missing, non-directory
    or symlinked component ends that path's probe: pull never writes through
    it, and descendant symlinks keep their own policy. Best-effort, like any
    permissive inspection; an unreadable probe is not a checkout.
    """
    source_name = source_config.get("name")
    try:
        base = Path(source_config["path"]).expanduser().resolve()
    except (KeyError, TypeError, OSError):
        return []
    roots: set[str] = set()
    probed: dict[str, str] = {}
    parents = {rel.rpartition("/")[0] for rel in rel_paths if isinstance(rel, str) and "/" in rel}
    for parent in parents:
        current = ""
        for part in parent.split("/"):
            if part in ("", ".", ".."):
                break
            current = f"{current}/{part}" if current else part
            state = probed.get(current)
            if state is None:
                directory = base / current
                try:
                    st = directory.lstat()
                except (OSError, ValueError):
                    # ValueError: a peer path the filesystem cannot encode.
                    state = "stop"
                else:
                    if not stat.S_ISDIR(st.st_mode):
                        state = "stop"
                    elif _directory_is_git_repo(
                        directory, base, strict=False, source_name=source_name
                    ):
                        state = "checkout"
                    else:
                        state = "dir"
                probed[current] = state
            if state == "checkout":
                roots.add(current)
            if state != "dir":
                break
    return sorted(roots)


def _contained_path(path: Path, base: Path) -> Path | None:
    """Collapse ``.`` and ``..`` and return ``path`` only when it stays under ``base``."""
    try:
        relative = path.relative_to(base)
    except ValueError:
        return None
    current = base
    for part in relative.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if current == base:
                return None
            current = current.parent
            continue
        current /= part
    return current


def _inside_frozen_checkout(
    current: Path, base: Path, *, strict: bool, source_name: str | None
) -> bool:
    """True when ``current`` is strictly inside a nested checkout below ``base``.

    ``nested_repo_root`` stops at the checkout, so an unreadable descendant is
    not stat-ed. The checkout root itself is still eligible for ``Local State``.
    """
    try:
        relative = current.relative_to(base).as_posix()
    except ValueError:
        return False
    checkout = nested_repo_root(current, base, strict=strict, source_name=source_name)
    return bool(checkout) and relative != checkout and relative.startswith(checkout + "/")


def _crosses_symlink(path: Path, base: Path, *, strict: bool, source_name: str | None) -> bool:
    """True when reaching ``path`` from ``base`` would traverse a symlink."""
    probe = base
    for part in path.relative_to(base).parts:
        probe /= part
        try:
            component = probe.lstat()
        except FileNotFoundError:
            return False
        except OSError as e:
            if strict:
                raise SnapshotError(
                    snapshot_refusal(
                        source=source_name,
                        rel_path=_rel_or_name(probe, base),
                        problem="could not be read",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            return True
        if stat.S_ISLNK(component.st_mode):
            return True
        if not stat.S_ISDIR(component.st_mode):
            return False
    return False


def _local_state_parent_prefix(
    selected: Path, base: Path, *, strict: bool, source_name: str | None
) -> str | None:
    """Return the parent of ``selected``, up through ``base``, that holds Local State.

    Looks only at ``BROWSER_PROFILE_MARKER``. A symlink directory is not
    entered, a symlink marker marks nothing, and a missing path is not an
    error. A directory inside a nested checkout is not probed: that checkout
    is already frozen, and an unreadable path there must not refuse the push.
    Strict mode refuses on a read error; permissive mode skips it.
    Never walks above ``base``.
    """
    contained = _contained_path(selected, base)
    if contained is None:
        return None
    current = contained.parent
    while True:
        current = _contained_path(current, base)
        if current is None:
            return None
        if _inside_frozen_checkout(current, base, strict=strict, source_name=source_name):
            directory_st = None
        elif _crosses_symlink(current, base, strict=strict, source_name=source_name):
            directory_st = None
        else:
            try:
                directory_st = current.lstat()
            except FileNotFoundError:
                directory_st = None
            except OSError as e:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=_rel_or_name(current, base),
                            problem="could not be read",
                            cause=os_error_cause(e),
                            next_action="Restore read access, then run mm push.",
                        )
                    ) from e
                directory_st = None
        if (
            directory_st is not None
            and not stat.S_ISLNK(directory_st.st_mode)
            and stat.S_ISDIR(directory_st.st_mode)
        ):
            try:
                current.resolve().relative_to(base)
            except ValueError:
                return None
            except OSError as e:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=_rel_or_name(current, base),
                            problem="could not be read",
                            cause=os_error_cause(e),
                            next_action="Restore read access, then run mm push.",
                        )
                    ) from e
                directory_st = None
        if (
            directory_st is not None
            and not stat.S_ISLNK(directory_st.st_mode)
            and stat.S_ISDIR(directory_st.st_mode)
        ):
            marker = current / BROWSER_PROFILE_MARKER
            try:
                marker_st = marker.lstat()
            except FileNotFoundError:
                marker_st = None
            except OSError as e:
                if strict:
                    raise SnapshotError(
                        snapshot_refusal(
                            source=source_name,
                            rel_path=_rel_or_name(marker, base),
                            problem="could not be read",
                            cause=os_error_cause(e),
                            next_action="Restore read access, then run mm push.",
                        )
                    ) from e
                marker_st = None
            if (
                marker_st is not None
                and not stat.S_ISLNK(marker_st.st_mode)
                and stat.S_ISREG(marker_st.st_mode)
            ):
                rel = current.relative_to(base).as_posix()
                return rel or "."
        if current == base:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def marker_skip_globs(
    source_config: dict[str, Any],
    *,
    strict: bool = False,
) -> list[str]:
    """Return relative-dir prefixes for directories that contain a skip marker.

    Two markers: gstack-extend drops ``.extend-root`` in every directory it
    renders, and Chromium or Electron write ``Local State`` at the root of a
    browser user-data directory (``BROWSER_PROFILE_MARKER``).
    ``exclude_patterns`` cannot express "skip the directory CONTAINING this
    file," so the walker and the consumer-boundary filter both call this
    helper. Results are PATH PREFIXES, not fnmatch globs — interpolating
    ``rel_dir`` into ``{rel_dir}/*`` would let a peer-chosen ``*`` in the
    path exclude unrelated files. A marker skip must not generate deletion
    tombstones, exactly as adding a glob must not.

    Only ``.extend-root`` has an include-root exemption. A ``Local State``
    on an include root returns that include as its prefix: it is a live
    profile, not a misplaced render marker, and holds nothing worth keeping.

    Permissive mode is best-effort: any OSError on a scan dir is skipped.
    Strict publishing raises SnapshotError instead. Symlinked scan dirs
    and symlink markers are ignored (same as the walker). Generic sources
    only. Each selected include directory and include file also checks its
    parent directories, up to and including the source root, for a regular
    non-symlink ``Local State``. That lookup does not apply to
    ``.extend-root``. An empty include list returns ``[]``.
    """
    include_dirs: list[str] = source_config.get("include_dirs") or []
    include_files: list[str] = source_config.get("include_files") or []
    if not include_dirs and not include_files:
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
    selected = [*include_dirs, *include_files]
    selected.sort(key=lambda name: len((base / name).parts))
    for name in selected:
        chosen = base / name
        try:
            rel = chosen.relative_to(base).as_posix()
        except ValueError:
            continue
        if _under_skip_prefix(rel, globs):
            continue
        prefix = _local_state_parent_prefix(chosen, base, strict=strict, source_name=source_name)
        if prefix is None:
            continue
        if prefix == ".":
            return ["."]
        if prefix not in seen:
            seen.add(prefix)
            globs.append(prefix)
    # Discover ancestor markers before probing any overlapping child include.
    for dir_name in sorted(include_dirs, key=lambda name: len((base / name).parts)):
        scan_dir = base / dir_name
        if scan_dir.is_relative_to(base) and _under_skip_prefix(
            scan_dir.relative_to(base).as_posix(), globs
        ):
            continue
        # A checkout covering this include is reported and frozen by the
        # walker; never probe beneath it or through a link above the include.
        if nested_repo_root(scan_dir, base, strict=strict, source_name=source_name):
            continue
        if path_has_descendant_symlink(
            scan_dir.parent, base, strict=strict, source_name=source_name
        ):
            continue
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
            # Compare paths, not the raw include spelling: trailing slashes,
            # `./skills` and a resolved absolute include keep this exemption.
            #
            # `Local State` has no such exemption: an include that is itself
            # a browser profile is skipped whole.
            if marker.name == MARKER_SKIP_NAME and marker.parent == scan_dir:
                continue
            if rel_dir == ".":
                return ["."]
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
    """Find ``.extend-root`` and ``Local State`` markers under ``scan_dir``.

    Never follows links. A directory holding a regular ``Local State`` is
    marked and returned from at once, even at the include root, without
    stat-ing its siblings or descending: a live browser profile churns, and
    a strict scan refuses on an entry that vanishes mid-scan.
    """
    found: list[Path] = []
    if path_has_descendant_symlink(
        scan_dir, base, strict=strict, source_name=source_name
    ) or nested_repo_root(scan_dir, base, strict=strict, source_name=source_name):
        return found

    def stat_entry(entry: os.DirEntry[str]) -> os.stat_result | None:
        try:
            return entry.stat(follow_symlinks=False)
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
            return None
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
            return None

    def walk(directory: Path, *, discovered: bool) -> None:
        try:
            rel_dir = directory.relative_to(base).as_posix() if directory != base else ""
        except ValueError:
            rel_dir = directory.name
        if rel_dir and _dir_is_policy_skipped(rel_dir, None):
            return
        if directory != base and _directory_is_git_repo(
            directory, base, strict=strict, source_name=source_name
        ):
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
        # Look the profile marker up by name before stat-ing any sibling.
        for entry in entries:
            if entry.name != BROWSER_PROFILE_MARKER:
                continue
            marker_st = stat_entry(entry)
            if marker_st is not None and stat.S_ISREG(marker_st.st_mode):
                found.append(Path(entry.path))
                return
            break
        child_dirs: list[Path] = []
        marked = False
        for entry in entries:
            st = stat_entry(entry)
            if st is None:
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
    both; `datetime.fromisoformat` on 3.11+ handles `Z` natively. A timezone-
    less string parses but cannot be compared with local UTC mtimes — raise
    so apply/predict gates treat it as uninterpretable instead of aborting.
    """
    parsed = datetime.fromisoformat(iso_str)
    if parsed.tzinfo is None:
        raise ValueError("manifest mtime must be timezone-aware")
    return parsed


def mtime_from_path(path: Path) -> datetime:
    """Return the file's mtime as a timezone-aware UTC datetime.

    Matches the canonical form used by walk_claude_source/walk_generic_source
    so comparisons against manifest-recorded mtimes are apples-to-apples.
    """
    stat = path.stat()
    return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)


@functools.lru_cache(maxsize=512)
def _include_pattern_body(pattern: str) -> str:
    """Translate one segment-aware include pattern to an unanchored regex body.

    Dialect (deliberately unlike the legacy fnmatch exclude globs, whose
    ``*`` crosses ``/``): a pattern is ``/``-separated segments; ``*`` and
    ``?`` match within one segment only; a whole ``**`` segment matches
    zero or more segments. Everything else is literal. So
    ``projects/*/pair-review/*`` means top-level files only, and
    ``projects/*/checkpoints/**`` means any depth below checkpoints.
    """
    raw_segments = pattern.split("/")
    segments = [
        seg
        for i, seg in enumerate(raw_segments)
        if not (seg == "**" and i > 0 and raw_segments[i - 1] == "**")
    ]
    out: list[str] = []
    for i, seg in enumerate(segments):
        if seg == "**":
            if i == len(segments) - 1:
                out.append(r"(?:/.*)?" if out else r".*")
            else:
                out.append(r"(?:[^/]+/)*" if not out else r"/(?:[^/]+/)*")
                # The join slash before the next segment is part of this
                # token; mark it so the joiner below does not add another.
                out.append("")
            continue
        if out and out[-1] != "":
            out.append("/")
        elif out:
            out.pop()
        out.append(
            "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in seg)
        )
    return "".join(out)


@functools.lru_cache(maxsize=16)
def _compile_include_patterns(patterns: tuple[str, ...]) -> re.Pattern[str] | None:
    """Compile a whole include list to one anchored alternation, or None when empty.

    The gstack allowlist runs to 184 entries and the walker tests every file
    under the include dirs, so one combined regex keeps the per-path cost
    well below an ``any()`` over per-pattern matches. A path matches when it
    matches any one pattern's body in full.
    """
    if not patterns:
        return None
    bodies = "|".join(_include_pattern_body(pattern) for pattern in patterns)
    return re.compile("^(?:" + bodies + r")\Z", re.DOTALL)


def matches_include_patterns(rel_path: str, include_patterns: list[str] | tuple[str, ...]) -> bool:
    """True when ``rel_path`` matches any segment-aware include pattern."""
    combined = _compile_include_patterns(tuple(include_patterns))
    return combined is not None and combined.match(rel_path) is not None


def _is_excluded(
    rel_path: str,
    exclude_patterns: list[str] | None = None,
    skip_prefixes: list[str] | None = None,
    include_patterns: list[str] | None = None,
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
    When ``include_patterns`` is non-empty, a path matching none of them is
    excluded too (allowlist); excludes above still win over an include match.
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
    if include_patterns and not matches_include_patterns(rel_path, include_patterns):
        return True
    return False


_HASH_CHUNK = 65_536


class _FileTooLarge(Exception):
    """A newly encountered file is already over the configured size cap."""

    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


@dataclass
class SourceHolds:
    """Per-source out-parameter for per-file transients a strict scan could not settle.

    Track 75A. A strict walker given one records a file that is still changing
    after its one re-read (``reasons``: rel -> ``changed`` | ``vanished`` |
    ``grew``) instead of refusing the whole push. ``candidates`` collects
    enumeration entries that vanished before their stat; the caller validates
    them against the prior manifest, because a vanished entry may have been a
    directory. ``candidates`` is None when the caller cannot validate them, so
    such an entry still refuses. Without a SourceHolds a transient raises
    ``SnapshotChanged`` (a ``SnapshotError``) after its retry.
    """

    reasons: dict[str, str] = field(default_factory=dict)
    candidates: set[str] | None = None


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
    if source_type == "muse" and st.st_nlink > 1:
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

    A file that changes, disappears or grows past the cap *during* the read
    raises ``SnapshotChanged`` (the transient subclass, Track 75A); every
    other refusal stays a plain ``SnapshotError``. Callers decide per raise
    site by exception type, never by message text.
    """
    rel = _rel_or_name(path, base) if base is not None else path.name

    def _refuse(
        problem: str,
        next_action: str,
        exc: BaseException | None = None,
        *,
        transient: str | None = None,
    ) -> NoReturn:
        cause = os_error_cause(exc) if exc is not None else None
        message = snapshot_refusal(
            source=source_name,
            rel_path=rel,
            problem=problem,
            cause=cause,
            next_action=next_action,
        )
        err: SnapshotError = (
            SnapshotChanged(message, transient) if transient else SnapshotError(message)
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
            transient="vanished",
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
            transient="vanished",
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
        _assert_revision_privacy(
            path, base, pre_fd_st, source_name=source_name, source_type=source_type
        )
        if (pre_fd_st.st_dev, pre_fd_st.st_ino) != (pre_path_st.st_dev, pre_path_st.st_ino):
            _refuse(
                "changed while being read",
                "Wait for the writer to finish, then run mm push.",
                transient="changed",
            )
        if pre_fd_st.st_size > max_file_size:
            if pre_path_st.st_size <= max_file_size:
                # Under the cap at the pre-open lstat, over it at the descriptor:
                # the file grew while being opened. Over the cap at both is a
                # stable oversized file and keeps the skip policy.
                _refuse(
                    "grew beyond max_file_size while being read",
                    "Wait for the writer to finish, or raise sync.max_file_size, then run mm push.",
                    transient="grew",
                )
            raise _FileTooLarge(pre_fd_st.st_size)

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
        try:
            post_fd_st = os.fstat(fd)
            post_path_st = path.lstat()
        except FileNotFoundError as e:
            _refuse(
                "disappeared while being read",
                "Wait for the writer to finish, then run mm push.",
                e,
                transient="vanished",
            )
        except OSError as e:
            _refuse("could not be read", "Restore read access, then run mm push.", e)
        # Confinement takes precedence over retryable revision changes.
        if not stat.S_ISREG(post_path_st.st_mode):
            _refuse(
                "is no longer a confined regular file",
                "Restore the regular file, then run mm push.",
            )
        _assert_revision_privacy(
            path, base, post_fd_st, source_name=source_name, source_type=source_type
        )
        if (post_path_st.st_dev, post_path_st.st_ino) != (post_fd_st.st_dev, post_fd_st.st_ino):
            _assert_revision_privacy(
                path, base, post_path_st, source_name=source_name, source_type=source_type
            )
        if remaining <= 0:
            _refuse(
                "grew beyond max_file_size while being read",
                "Wait for the writer to finish, or raise sync.max_file_size, then run mm push.",
                transient="grew",
            )
        hashed = (max_file_size + 1) - remaining
        if hashed != pre_fd_st.st_size:
            _refuse(
                "changed while being read",
                "Wait for the writer to finish, then run mm push.",
                transient="changed",
            )

        if _revision_signature(pre_fd_st) != _revision_signature(post_fd_st):
            _refuse(
                "changed while being read",
                "Wait for the writer to finish, then run mm push.",
                transient="changed",
            )
        if (post_path_st.st_dev, post_path_st.st_ino) != (pre_fd_st.st_dev, pre_fd_st.st_ino):
            _refuse(
                "changed while being read",
                "Wait for the writer to finish, then run mm push.",
                transient="changed",
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
    on_skip: Any = None,
    nested_repos: set[str] | None = None,
    collect_files: bool = True,
    holds: SourceHolds | None = None,
) -> list[Path]:
    """Explicit ``os.scandir`` walk that never follows descendant symlinks.

    Prunes nested checkouts (a ``start`` inside one, or any directory below
    it with ``.git``) before listing their contents, reporting each root to
    ``on_skip`` with NESTED_REPO_SKIP_REASON and to ``nested_repos``.
    ``collect_files=False`` runs that inventory without collecting files.

    A strict walk given ``holds.candidates`` records an entry that vanished
    between its listing and its stat (a temp file renamed away) as a hold
    candidate instead of refusing; the caller validates it against the prior
    manifest. A directory that vanishes before its own listing, and every
    other enumeration failure, still refuses.
    """
    collected: list[Path] = []

    def skip_repo(root: str) -> None:
        if nested_repos is not None:
            nested_repos.add(root)
        if on_skip:
            on_skip(root, NESTED_REPO_SKIP_REASON)

    if root := nested_repo_root(start, base, strict=strict, source_name=source_name):
        skip_repo(root)
        return collected
    if path_has_descendant_symlink(start, base, strict=strict, source_name=source_name):
        if on_skip and collect_files:
            on_skip(str(start), "symlink")
        return collected

    def walk(directory: Path, *, discovered: bool) -> None:
        try:
            rel_dir = directory.relative_to(base).as_posix() if directory != base else ""
        except ValueError:
            rel_dir = directory.name
        if rel_dir and _dir_is_policy_skipped(rel_dir, skip_prefixes):
            return
        if directory != base and _directory_is_git_repo(
            directory, base, strict=strict, source_name=source_name
        ):
            skip_repo(rel_dir)
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
                if strict and holds is not None and holds.candidates is not None:
                    holds.candidates.add(_rel_or_name(Path(entry.path), base))
                    continue
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
            elif collect_files and stat.S_ISREG(st.st_mode):
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


def _stat_collected_file(
    path: Path,
    base: Path,
    *,
    strict: bool,
    source_name: str | None,
    holds: SourceHolds | None,
    exclude_patterns: list[str] | None,
    skip_prefixes: list[str] | None = None,
    include_patterns: list[str] | None = None,
) -> os.stat_result | None:
    """Stat one already-enumerated file before it is recorded (muse/generic loops).

    Returns None when the walker should skip it. A strict walk re-probes a
    vanished file once, even during recovery. An unresolved vanish is held
    only with ``holds`` (excluded paths are skipped silently: they never sync).
    Any other ``OSError``, or a repeated vanish without ``holds``, refuses.
    """
    try:
        return path.lstat() if strict else path.stat()
    except OSError as e:
        if not strict:
            return None
        if isinstance(e, FileNotFoundError):
            try:
                return path.lstat()
            except FileNotFoundError as again:
                rel = _rel_or_name(path, base)
                if _is_excluded(rel, exclude_patterns, skip_prefixes, include_patterns):
                    return None
                if holds is not None:
                    holds.reasons[rel] = "vanished"
                    return None
                e = again
            except OSError as again:
                e = again
        raise SnapshotError(
            snapshot_refusal(
                source=source_name,
                rel_path=_rel_or_name(path, base),
                problem="could not be read",
                cause=os_error_cause(e),
                next_action="Restore read access, then run mm push.",
            )
        ) from e


# Per-file walker pipeline (single source of truth, called by
# walk_claude_source, walk_generic_source, and walk_muse_source):
#
#   path ──► relative_to(base) ──► rel
#                                   │
#                          _is_excluded(rel)? ──► None
#                                   │
#                 strict: one descriptor revision ─── OSError ──► SnapshotError
#                                   └─ changed/vanished/grew ─► re-read once ─► still
#                                      changing ─► SnapshotChanged (a hold when the
#                                      caller passed SourceHolds, else a refusal)
#                 diagnostic: stat()/hash_file() skip on permission/read/size
#                                   │
#                          (rel, {sha256, size, mtime})
#
# Diagnostic "None" returns are NOT causal evidence of deletion — the
# permissive walker is intentionally lossy. Publishing scans use strict
# mode so an incomplete observation cannot become a tombstone. See SPEC.md
# "Merge invariants."
#
# Transient census (Track 75A). Only these strict sites may hold a file
# instead of refusing, and only when the caller passes SourceHolds:
#   1. read_file_revision's changed/vanished/grew raises (SnapshotChanged),
#      retried once in _record_file, which every walker reaches;
#   2. the per-file lstat in the muse and generic loops
#      (_stat_collected_file): FileNotFoundError after one re-probe;
#   3. _collect_regular_files_scandir's entry stat (all four walkers):
#      FileNotFoundError becomes a hold *candidate* the caller validates.
# The claude walker has no per-file probe before _record_file. Marker discovery
# (_collect_marker_files) has its own entry stat and keeps refusing on a
# vanished entry: it cannot carry a collector and is outside the approved
# census (docs/TODOS.md). Directory and root probes (_lstat_or_none, project
# entries, scandir of a directory, nested-checkout probes,
# path_has_descendant_symlink) and every other OSError keep refusing: an
# unenumerated subtree cannot be proven tombstone-safe.
def _record_file(
    path: Path,
    base: Path,
    max_file_size: int,
    on_skip: Any = None,
    exclude_patterns: list[str] | None = None,
    skip_prefixes: list[str] | None = None,
    include_patterns: list[str] | None = None,
    *,
    strict: bool = False,
    source_name: str | None = None,
    source_type: str | None = None,
    diagnostic_hash: Callable[[Path, os.stat_result], str] | None = None,
    holds: SourceHolds | None = None,
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

    A file that changes, vanishes or grows during the read is re-read once.
    If the second read is also transient, a caller-supplied ``holds`` records
    the file and the scan goes on (returns None); without one it raises.
    """
    rel = str(path.relative_to(base))

    if _is_excluded(rel, exclude_patterns, skip_prefixes, include_patterns):
        return None

    if strict:
        read_args: dict[str, Any] = {
            "max_file_size": max_file_size,
            "retain_bytes": False,
            "source_name": source_name,
            "source_type": source_type,
            "base": base,
        }
        try:
            try:
                revision = read_file_revision(path, **read_args)
            except SnapshotChanged:
                # Read 2: a verified revision of its own, not required to equal
                # read 1. Non-transient failures and a stable oversize here take
                # their ordinary paths; there is no third read.
                revision = read_file_revision(path, **read_args)
        except _FileTooLarge as e:
            if on_skip:
                size_mb = e.size / (1024 * 1024)
                on_skip(rel, f"exceeds max_file_size ({size_mb:.1f}MB)")
            return None
        except SnapshotChanged as e:
            if holds is None:
                raise
            holds.reasons[rel] = e.reason
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
        # Inspection can reuse an unchanged revision from its event scan.
        # The strict publication branch above NEVER uses this callback.
        sha = diagnostic_hash(path, file_stat) if diagnostic_hash else hash_file(path)
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
    holds: SourceHolds | None = None,
) -> dict[str, dict[str, Any]]:
    """Walk a Claude ~/.claude directory and build the files dict.

    Scans only projects/*/memory/ and projects/*/todos/ subdirectories.

    Args:
        base_dir: Root directory to walk (e.g., ~/.claude)
        max_file_size: Skip files larger than this (bytes). Default 50MB.
        on_skip: Optional callback(path, reason) for skipped files. Nested
            checkouts report their bare relative directory root with
            NESTED_REPO_SKIP_REASON, possibly once per include that reaches
            them; build_manifest_v2 deduplicates.
        exclude_patterns: Optional per-source fnmatch globs that extend the
            hardcoded EXCLUDED list. Matched against the relative path.
        strict: Publishing scans refuse incomplete observations.
        source_name: Source name for strict SnapshotError location.
        holds: Optional strict-scan out-parameter that collects files still
            changing after their retry instead of refusing (Track 75A).

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
    if root := nested_repo_root(projects_dir, base, strict=strict, source_name=source_name):
        if on_skip:
            on_skip(root, NESTED_REPO_SKIP_REASON)
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
            if source_name == CLAUDE_SOURCE_NAME and entry.name.startswith("@"):
                if on_skip:
                    on_skip(project_dir.relative_to(base).as_posix(), UNPLACEABLE_SKIP_REASON)
                continue
            if _directory_is_git_repo(project_dir, base, strict=True, source_name=source_name):
                if on_skip:
                    on_skip(project_dir.relative_to(base).as_posix(), NESTED_REPO_SKIP_REASON)
                continue
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
                scan_dir,
                base,
                strict=True,
                source_name=source_name,
                on_skip=on_skip,
                holds=holds,
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
                    holds=holds,
                ):
                    rel, info = result
                    files[rel] = info
        return files

    for project_dir in projects_dir.iterdir():
        if project_dir.is_symlink() or not project_dir.is_dir():
            continue
        if source_name == CLAUDE_SOURCE_NAME and project_dir.name.startswith("@"):
            if on_skip:
                on_skip(project_dir.relative_to(base).as_posix(), UNPLACEABLE_SKIP_REASON)
            continue
        if _directory_is_git_repo(project_dir, base, strict=False, source_name=source_name):
            if on_skip:
                on_skip(project_dir.relative_to(base).as_posix(), NESTED_REPO_SKIP_REASON)
            continue
        for subdir_name in SYNCED_SUBDIRS:
            subdir = project_dir / subdir_name
            if subdir.exists() and subdir.is_dir():
                scan_dirs.append(subdir)

    for scan_dir in scan_dirs:
        for path in _collect_regular_files_scandir(
            scan_dir, base, strict=False, source_name=source_name, on_skip=on_skip
        ):
            if result := _record_file(path, base, max_file_size, on_skip, exclude_patterns):
                rel, info = result
                files[rel] = info

    return files


def walk_muse_source(
    source_config: dict[str, Any],
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
    *,
    strict: bool = False,
    holds: SourceHolds | None = None,
) -> dict[str, dict[str, Any]]:
    """Walk a Muse data root and build the files dict.

    Scans only the hardcoded ``memory/`` tree (``MUSE_SYNCED_SUBDIRS``).
    Sessions, the session index, bundled skills, plugin caches and every
    other root entry are never entered. Missing dirs are a no-op.
    This preserves a privacy-critical root where only singly-linked regular
    files under an allowlisted tree may roam.
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
    exclude_patterns = [*MUSE_EXCLUDE_PATTERNS, *extra]

    files: dict[str, dict[str, Any]] = {}
    collected_paths: list[Path] = []

    for dir_name in MUSE_SYNCED_SUBDIRS:
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
        collected_paths.extend(
            _collect_regular_files_scandir(
                scan_dir,
                base,
                strict=strict,
                source_name=source_name,
                on_skip=on_skip,
                holds=holds,
            )
        )

    collected_paths.sort(
        key=lambda p: str(p.relative_to(base)) if p.is_relative_to(base) else str(p)
    )
    seen: set[tuple[int, int]] = set()
    for path in collected_paths:
        if path_has_descendant_symlink(path, base, strict=strict, source_name=source_name):
            if on_skip:
                on_skip(str(path), "symlink")
            continue
        st = _stat_collected_file(
            path,
            base,
            strict=strict,
            source_name=source_name,
            holds=holds,
            exclude_patterns=exclude_patterns,
        )
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode):
            if on_skip:
                on_skip(str(path), "symlink")
            continue
        # Same hard-link boundary as Grok's: a second link could make a
        # session log or credential reachable from memory/ without any
        # symlink component, so accept only singly-linked regular files.
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
            source_type="muse",
            holds=holds,
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
    diagnostic_hash: Callable[[Path, os.stat_result], str] | None = None,
    holds: SourceHolds | None = None,
) -> dict[str, dict[str, Any]]:
    """Walk a generic source directory with configurable include_dirs/include_files.

    Args:
        source_config: Dict with keys:
            path: Base directory path (supports ~)
            include_dirs: List of directory names to walk recursively
            include_files: List of filenames to check at root level
            exclude_patterns: Optional fnmatch globs evaluated against the
                relative path; matches are silently skipped. Directories
                containing `.extend-root` or a browser profile's
                `Local State` are also skipped (see `marker_skip_globs`);
                that skip is a path-prefix match so it does not generate
                deletion tombstones.
            include_patterns: Optional segment-aware allowlist (see
                `matches_include_patterns`); when non-empty, only matching
                relative paths are walked. Excludes above win over a match.
        max_file_size: Skip files larger than this (bytes). Default 50MB.
        on_skip: Optional callback(path, reason) for skipped files. Nested
            checkouts report their bare relative directory root with
            NESTED_REPO_SKIP_REASON, possibly once per include that reaches
            them; build_manifest_v2 deduplicates.
        strict: Publishing scans refuse incomplete observations.
        diagnostic_hash: Inspection-only hash reuse. Ignored when
            strict=True; the publisher never consults it.
        holds: Optional strict-scan out-parameter that collects files still
            changing after their retry instead of refusing (Track 75A).

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
    include_patterns: list[str] = list(source_config.get("include_patterns") or [])
    exclude_patterns: list[str] = list(source_config.get("exclude_patterns") or [])
    # Marker-aware skip: directories containing `.extend-root` or `Local State`
    # drop out of the local manifest the same way a glob would, but as PATH PREFIXES
    # not fnmatch globs. `_build_exclude_map` threads the same prefixes
    # for tombstone suppression. Do NOT skip this — a walker-only skip
    # would tombstone previously-synced files on the next push.
    skip_prefixes = marker_skip_globs(source_config, strict=strict)
    if _under_skip_prefix("", skip_prefixes):
        return {}

    files: dict[str, dict[str, Any]] = {}
    collected_paths: list[Path] = []

    for dir_name in include_dirs:
        scan_dir = base / dir_name
        if scan_dir.is_relative_to(base) and _under_skip_prefix(
            scan_dir.relative_to(base).as_posix(), skip_prefixes
        ):
            continue
        # Report a checkout above an include even when the include is absent or
        # linked, so push freezes its published entries instead of dropping them.
        if root := nested_repo_root(scan_dir, base, strict=strict, source_name=source_name):
            if on_skip:
                on_skip(root, NESTED_REPO_SKIP_REASON)
            continue
        st = _lstat_or_none(scan_dir, strict=strict, source_name=source_name, rel_path=dir_name)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode):
            if on_skip:
                on_skip(str(scan_dir), "symlink")
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        collected_paths.extend(
            _collect_regular_files_scandir(
                scan_dir,
                base,
                strict=strict,
                source_name=source_name,
                skip_prefixes=skip_prefixes,
                on_skip=on_skip,
                holds=holds,
            )
        )

    for filename in include_files:
        path = base / filename
        if path.is_relative_to(base) and _under_skip_prefix(
            path.relative_to(base).as_posix(), skip_prefixes
        ):
            continue
        if root := nested_repo_root(path.parent, base, strict=strict, source_name=source_name):
            if on_skip:
                on_skip(root, NESTED_REPO_SKIP_REASON)
            continue
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
    # machines (include_files entries follow include_dirs results in config
    # order, and filesystem enumeration order varies across machines).
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
        st = _stat_collected_file(
            path,
            base,
            strict=strict,
            source_name=source_name,
            holds=holds,
            exclude_patterns=exclude_patterns,
            skip_prefixes=skip_prefixes,
            include_patterns=include_patterns,
        )
        if st is None:
            continue  # consistent with _record_file's tolerance for races
        if stat.S_ISLNK(st.st_mode):
            if on_skip:
                on_skip(str(path), "symlink")
            continue
        if _is_excluded(
            _rel_or_name(path, base), exclude_patterns, skip_prefixes, include_patterns
        ):
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
            include_patterns,
            strict=strict,
            source_name=source_name,
            source_type="generic",
            diagnostic_hash=diagnostic_hash,
            holds=holds,
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
    diagnostic_hash: Callable[[Path, os.stat_result], str] | None = None,
    holds: SourceHolds | None = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Dispatch to the appropriate walker based on source type.

    Args:
        source_config: Dict with at least "type" and "path" keys.
            type="claude" -> walk_claude_source
            type="muse" -> walk_muse_source
            type="generic" -> walk_generic_source
        max_file_size: Skip files larger than this (bytes).
        on_skip: Optional callback(path, reason) for skipped files. Nested
            checkouts report their bare relative directory root with
            NESTED_REPO_SKIP_REASON, possibly once per include that reaches
            them; build_manifest_v2 deduplicates.
        strict: Publishing scans refuse incomplete observations.
        diagnostic_hash: Inspection-only hash reuse. Ignored when
            strict=True; the publisher never consults it.
        holds: Optional strict-scan out-parameter (see ``SourceHolds``).

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
            holds=holds,
        )
    elif source_type == "muse":
        files = walk_muse_source(source_config, max_file_size, on_skip, strict=strict, holds=holds)
    elif source_type == "generic":
        files = walk_generic_source(
            source_config,
            max_file_size,
            on_skip,
            strict=strict,
            diagnostic_hash=diagnostic_hash,
            holds=holds,
        )
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
    diagnostic_hash: Callable[[Path, os.stat_result], str] | None = None,
    nested_roots: dict[str, set[str]] | None = None,
    held: dict[str, dict[str, str]] | None = None,
    held_candidates: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Build a v2 manifest with multiple sources.

    Args:
        device_id: Unique device identifier.
        device_name: Human-readable device name.
        sources_configs: List of source config dicts, each with at least
            "name", "type", and "path" keys.
        max_file_size: Skip files larger than this (bytes).
        on_skip: Optional callback(path, reason) for skipped files. For
            NESTED_REPO_SKIP_REASON the path is a checkout directory root
            qualified as ``source:rel``, reported once per source.
        strict: Publishing scans refuse incomplete observations.
        diagnostic_hash: Inspection-only hash reuse. Ignored when
            strict=True; the publisher never consults it.
        nested_roots: Optional out-parameter receiving each source's
            skipped checkout roots (bare relative paths) from this same walk.
        held: Optional strict-scan out-parameter (Track 75A) receiving, per
            source, each file still changing after its one re-read as
            ``rel -> changed | vanished | grew``. Such a file is absent from
            the returned manifest. Without it a transient raises
            ``SnapshotChanged``.
        held_candidates: Optional companion receiving, per source, enumeration
            entries that vanished before their stat. Only collected alongside
            ``held``; the caller validates them against its prior manifest.

    Returns:
        v2 manifest dict with a "sources" dict keyed by source name.
    """
    sources: dict[str, dict[str, Any]] = {}

    for src_cfg in sources_configs:
        name = src_cfg["name"]
        reported_repos: set[str] = set()

        def source_skip(path: str, reason: str) -> None:
            if reason == NESTED_REPO_SKIP_REASON:
                if path in reported_repos:
                    return
                reported_repos.add(path)
                if nested_roots is not None:
                    nested_roots.setdefault(name, set()).add(path)
                path = f"{name}:{path}"
            if on_skip is not None:
                on_skip(path, reason)

        holds = None
        if held is not None:
            holds = SourceHolds(
                reasons=held.setdefault(name, {}),
                candidates=(
                    held_candidates.setdefault(name, set()) if held_candidates is not None else None
                ),
            )
        base_path, files = walk_source(
            src_cfg,
            max_file_size,
            source_skip,
            strict=strict,
            diagnostic_hash=diagnostic_hash,
            holds=holds,
        )
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


# ── wire names for Claude project folders (Track 90A) ───────────────────
#
# In memory a manifest always carries LOCAL folder names: walkers, tombstones,
# diffs, holds, freezes, uploads, apply, previews, status and history never see
# a wire name. The codec runs at the manifest byte boundary only: encode maps
# local -> wire on a copy just before encryption, and `load_manifest` maps
# wire -> local once per decoded manifest. See docs/invariants/sync.md.


def _split_project_key(rel: str) -> tuple[str, str] | None:
    """``projects/<folder>/<rest>`` -> ``(folder, rest)``; any other shape is ``None``."""
    parts = rel.split("/", 2)
    if len(parts) == 3 and parts[0] == "projects" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def _remap_keys(
    files: dict[str, Any], tombstones: dict[str, Any], mapping: Callable[[str], str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rewrite the folder of every ``projects/<folder>/...`` file and claude tombstone key."""
    new_files: dict[str, Any] = {}
    for rel, info in files.items():
        parts = _split_project_key(rel) if isinstance(rel, str) else None
        new_files[f"projects/{mapping(parts[0])}/{parts[1]}" if parts else rel] = info
    new_tombstones: dict[str, Any] = {}
    prefix = f"{CLAUDE_SOURCE_NAME}:"
    for key, info in tombstones.items():
        parts = (
            _split_project_key(key[len(prefix) :])
            if isinstance(key, str) and key.startswith(prefix)
            else None
        )
        if parts is None:
            new_tombstones[key] = info
            continue
        new_key = f"{prefix}projects/{mapping(parts[0])}/{parts[1]}"
        existing = new_tombstones.get(new_key)
        if not isinstance(existing, dict) or (
            isinstance(info, dict)
            and str(info.get("deleted_at", "")) > str(existing.get("deleted_at", ""))
        ):
            new_tombstones[new_key] = info
    return new_files, new_tombstones


def _claude_entry(manifest: dict[str, Any]) -> dict[str, Any] | None:
    sources = manifest.get("sources")
    entry = sources.get(CLAUDE_SOURCE_NAME) if isinstance(sources, dict) else None
    return entry if isinstance(entry, dict) else None


def manifest_to_wire(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``manifest`` with Claude project folders under wire names.

    Never mutates its argument. The codec is off (this returns ``manifest``
    itself) without a configured Claude folder and a resolved home, and a
    manifest with no ``claude`` source, or of a shape it does not recognise,
    passes through. Raises `ManifestError` naming both folders if two local
    folders would share a wire name, which one owner per identity makes
    unreachable.
    """
    entry = _claude_entry(manifest)
    idx = projectid.index()
    if idx is None or entry is None or not isinstance(entry.get("files"), dict):
        return manifest
    tombstones = manifest.get("tombstones")
    if not isinstance(tombstones, dict):
        tombstones = {}
    owners_by_wire: dict[str, str] = {}
    wire_folders: dict[str, str] = {}

    def to_wire(folder: str) -> str:
        if folder.startswith("@"):
            return folder  # a carried deletion record under a wire name stays as it is
        wire = idx.to_wire(folder)
        other = owners_by_wire.setdefault(wire, folder)
        if other != folder:
            raise ManifestError(
                f"manifest: project folders {other!r} and {folder!r} would share the "
                f"wire name {wire!r}"
            )
        if wire.startswith(projectid.GIT_PREFIX):
            wire_folders[wire] = folder
        return wire

    files, new_tombstones = _remap_keys(entry["files"], tombstones, to_wire)
    out = dict(manifest)
    out["sources"] = {
        **manifest["sources"],
        CLAUDE_SOURCE_NAME: {**entry, "files": files, "wire_folders": wire_folders},
    }
    out["tombstones"] = new_tombstones
    return out


def _marked_mapping(names: set[str], idx: projectid.Index, home: str) -> dict[str, str]:
    """``wire folder -> local folder`` under the rules of a marked ``claude`` entry."""
    mapping: dict[str, str] = {}
    owned = idx.folder_identity
    claimed: set[str] = set()

    def displaced(local: str) -> str:
        name = projectid.LIT_PREFIX + local
        while name in names or name in claimed:
            name += "-"
        claimed.add(name)
        return name

    for wire in sorted(names):  # identity names take their owner's local name
        if wire.startswith(projectid.GIT_PREFIX):
            identity = projectid.wire_identity(wire)
            owner = idx.owners.get(identity) if identity else None
            if owner is None:
                mapping[wire] = wire
            elif owner in claimed:  # two spellings of one identity
                mapping[wire] = displaced(owner)
            else:
                mapping[wire] = owner
                claimed.add(owner)
    for wire in sorted(names):
        if projectid.is_home_form(wire):
            local = home + wire[len(projectid.HOME_NAME) :]
            if local in owned:
                mapping[wire] = wire  # stale home-form copy of a project real here
            elif local in claimed:
                mapping[wire] = displaced(local)
            else:
                mapping[wire] = local
                claimed.add(local)
    for wire in sorted(names):
        if wire in mapping:
            continue
        if wire.startswith("@"):
            mapping[wire] = wire
        elif wire in owned or wire in claimed:
            mapping[wire] = displaced(wire)
        else:
            mapping[wire] = wire
            claimed.add(wire)
    return mapping


def _from_wire_mapping(
    names: set[str],
    entry: dict[str, Any],
    idx: projectid.Index | None,
    carried: set[str],
    file_names: set[str],
) -> tuple[dict[str, str], set[str]]:
    """``(wire folder -> local folder, moved folders)`` for one decoded ``claude`` entry.

    ``carried`` holds the ``@`` names only deletion records use: a machine with
    the source disabled or the codec off still publishes those, unmarked.
    ``file_names`` holds the names with files: a `projects/` outage moves only
    those, or a deletion record alone would republish on every push.
    """
    moved: set[str] = set()
    home = projectid.encoded_home() if idx is None else idx.home_enc
    base = entry.get("base_path")
    if entry.get("wire_names") is True:
        if idx is None or home is None:
            return {n: n for n in names}, moved
        mapping = _marked_mapping(names, idx, home)
        folders = entry.get("wire_folders")
        if isinstance(folders, dict):
            for wire, recorded in folders.items():
                identity = projectid.wire_identity(wire) if isinstance(wire, str) else None
                owner = idx.owners.get(identity) if identity else None
                if owner and isinstance(recorded, str) and recorded != owner:
                    moved.add(owner)
        # Published from another Claude folder (a peer, or this machine before
        # its home or `claude` path changed), or `projects/` cannot be listed: no
        # name says what this folder should hold, so an absence is never a deletion.
        if isinstance(base, str) and base and base != idx.claude_real:
            moved.update(local for local in mapping.values() if not local.startswith("@"))
        elif not idx.listed:
            moved.update(mapping[n] for n in file_names if not mapping[n].startswith("@"))
        return mapping, moved
    # Unmarked: path names, plus any `@` form carried on a deletion record, which
    # names its own provenance and maps like a marked one.
    mapping = {n: n for n in names}
    if carried and idx is not None and home is not None:
        mapping.update(_marked_mapping(carried, idx, home))
    # The same rule for an own prior that has not switched yet.
    real = projectid.claude_real()
    if real is not None and isinstance(base, str) and base and base != real:
        moved.update(name for name in names if not name.startswith("@"))
    elif real is not None and not projectid.projects_listed():
        moved.update(name for name in file_names if not name.startswith("@"))
    if (
        not projectid.configured()
        or home is None
        or not isinstance(base, str)
        or Path(base).name != ".claude"
    ):
        return mapping, moved
    publisher = projectid.encode_path(str(Path(base).parent))
    if publisher == home:
        return mapping, moved
    for name in names - carried:
        if name == publisher or name.startswith(publisher + "-"):
            mapping[name] = projectid.OLD_PREFIX + name
    return mapping, moved


def manifest_from_wire(manifest: dict[str, Any]) -> dict[str, Any]:
    """Map a decoded manifest's Claude project folders to this machine's local names.

    Mutates ``manifest`` in place and returns it. Runs once per decoded
    manifest (`load_manifest` calls it after its shape validation) and never
    raises on shape. Marked entries (``wire_names`` is ``True``): ``@git-``
    goes to the folder that owns the identity here, else stays literal and is
    *unplaced*; ``@home`` forms go to the encoded home unless that folder owns
    an identity (then they stay literal and are dropped as superseded); a
    literal that names an owned folder, or a name another entry already holds,
    becomes ``@lit-<folder>``, so nothing is dropped and ``mm gc`` keeps every
    sha. Unmarked entries from another home: a folder equal to or under the
    publisher's encoded home becomes ``@old-<folder>``. Tombstone keys follow
    the same rules; one whose source has no entry passes through unless its
    own ``@`` form names where it belongs. When ``wire_folders`` names a
    different folder than the identity's current owner, the entry was published
    from another Claude folder, or ``projects/`` cannot be listed, the affected
    folders join an in-memory ``moved_folders`` list (never serialized) so a
    move or an outage mints no deletion.
    """
    entry = _claude_entry(manifest)
    if entry is not None:
        entry.pop("moved_folders", None)  # in-memory only; a serialized one means nothing
    files = entry.get("files") if entry is not None else None
    tombstones = manifest.get("tombstones")
    if not isinstance(tombstones, dict):
        tombstones = {}
    if not isinstance(files, dict):
        files = {}
        entry = None
    prefix = f"{CLAUDE_SOURCE_NAME}:"
    file_names: set[str] = set()
    for rel in files:
        if isinstance(rel, str) and (parts := _split_project_key(rel)):
            file_names.add(parts[0])
    tomb_names: set[str] = set()
    for key in tombstones:
        if isinstance(key, str) and key.startswith(prefix):
            if parts := _split_project_key(key[len(prefix) :]):
                tomb_names.add(parts[0])
    carried = {name for name in tomb_names - file_names if name.startswith("@")}
    marked = entry is not None and entry.get("wire_names") is True
    idx = projectid.index() if marked or carried else None
    mapping, moved = _from_wire_mapping(
        file_names | tomb_names, entry if entry is not None else {}, idx, carried, file_names
    )
    if any(old != new for old, new in mapping.items()):
        new_files, manifest["tombstones"] = _remap_keys(
            files, tombstones, lambda folder: mapping.get(folder, folder)
        )
        if entry is not None:
            entry["files"] = new_files
    if moved and entry is not None:
        entry["moved_folders"] = sorted(moved)
    return manifest


def load_manifest(data: bytes) -> dict[str, Any]:
    """Decode JSON bytes into a v2-normalized manifest dict.

    Single load boundary for every manifest path (remote fetch, sidecar
    recovery, test fixtures). Guarantees the returned dict has dict-typed
    `sources` and `tombstones`, each source entry has a dict-typed `files`,
    each file value is a dict with a string `sha256`, and each tombstone
    value is a dict. Non-string file `mtime` values are coerced to `None`
    only after that shape check. Every key in `sources[*].files` is
    confined to a relative path inside its source — no '..' segments, no
    absolute paths, no null bytes (see `_validate_rel_path`). Callers may
    rely on these invariants. It ends with `manifest_from_wire`, so a Claude
    project folder arrives under this machine's local name (Track 90A).

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
        for rel, info in files.items():
            _validate_rel_path(rel, where=f"sources[{src_name!r}]['files'] key")
            if not isinstance(info, dict) or not isinstance(info.get("sha256"), str):
                raise ManifestError(
                    f"manifest: sources[{src_name!r}]['files'][{rel!r}] "
                    "must be an object with a string sha256"
                )
            if not isinstance(info.get("mtime"), str):
                info["mtime"] = None
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
    manifest_from_wire(normalized)
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
            # A folder whose checkout moved holds another folder's published
            # files under the identity's name; their absence here is a move,
            # never a deletion (Track 90A). In-memory only, never serialized.
            moved = (
                set(remote_src.get("moved_folders") or ())
                if src_name == CLAUDE_SOURCE_NAME and isinstance(remote_src, dict)
                else set()
            )

            for path in remote_files:
                key = f"{src_name}:{path}"
                if moved and (parts := _split_project_key(path)) and parts[0] in moved:
                    continue
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
