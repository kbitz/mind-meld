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

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mind_meld.errors import ManifestError

# Syncthing-style local conflict copies: <stem>.sync-conflict-<YYYYmmdd-HHMMSS>-<device>[.<ext>]
# Pattern pinned to the exact timestamp shape `conflict_filename()` emits
# (8 digits + dash + 6 digits + dash + suffix), so user files like
# `notes.sync-conflict-log.md` or `notes.sync-conflict-2024-summary.md`
# are NEVER false-positive-excluded. fnmatch char classes match exactly
# one character, so the digit count is enforced precisely.
CONFLICT_INFIX = ".sync-conflict-"
_DIGITS_8 = "[0-9]" * 8
_DIGITS_6 = "[0-9]" * 6
CONFLICT_PATTERN = f"*{CONFLICT_INFIX}{_DIGITS_8}-{_DIGITS_6}-*"


def is_conflict_filename(name: str) -> bool:
    """Return True iff `name` is an mm/Syncthing-style conflict copy.

    Strict matcher: the suffix after `.sync-conflict-` must start with a
    digit (timestamp). Used by the walker to keep conflict files local-only
    and by `mm conflicts`/`mm gc --conflicts` to avoid false-positives on
    user files that happen to contain `.sync-conflict-` in their name.
    """
    if not name:
        return False
    return fnmatch.fnmatch(name, CONFLICT_PATTERN)


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
]

# Only sync these subdirectories within each project.
# Everything else (sessions, settings, etc.) is either git-tracked or ephemeral.
SYNCED_SUBDIRS = ["memory", "todos"]


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


def _is_excluded(rel_path: str) -> bool:
    """Check if a relative path matches any exclude pattern."""
    parts = rel_path.split("/")
    filename = parts[-1]
    # Conflict copies stay local-only — uploading them would defeat the
    # Syncthing-style preservation model (one local conflict turns into N
    # cross-device conflict files).
    if is_conflict_filename(filename):
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
    return False


def hash_file(path: Path) -> str:
    """SHA-256 hash a file. Reads in chunks to handle large files."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65_536)
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


# Per-file walker pipeline (single source of truth, called by both
# walk_claude_source and walk_generic_source):
#
#   path ──► relative_to(base) ──► rel
#                                   │
#                          _is_excluded(rel)? ──► None
#                                   │
#                               stat() ─── PermissionError ──► on_skip("permission denied") ──► None
#                                   │
#                              size > cap? ──► on_skip("exceeds max_file_size (...MB)") ──► None
#                                   │
#                             hash_file() ─── Permission/OSError ──► on_skip("read error") ──► None
#                                   │
#                          (rel, {sha256, size, mtime})
#
# "None" returns are NOT causal evidence of deletion — the walker is
# intentionally lossy. Only explicit tombstones are. See SPEC.md
# "Merge invariants."
def _record_file(
    path: Path,
    base: Path,
    max_file_size: int,
    on_skip: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    """Apply the per-file walker pipeline to `path` under `base`.

    Returns (rel_path, {"sha256", "size", "mtime"}) on success, or None if
    the file should be skipped. Skip reasons are reported via `on_skip(rel,
    reason)` — the exact reason strings ("permission denied", "read error",
    f"exceeds max_file_size ({size_mb:.1f}MB)") are load-bearing because
    cli.py surfaces them in verbose walker output. Do not reshape them
    without updating callers and tests.
    """
    rel = str(path.relative_to(base))

    if _is_excluded(rel):
        return None

    try:
        stat = path.stat()
    except PermissionError:
        if on_skip:
            on_skip(rel, "permission denied")
        return None

    if stat.st_size > max_file_size:
        if on_skip:
            size_mb = stat.st_size / (1024 * 1024)
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
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def walk_claude_source(
    base_dir: str | Path,
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
) -> dict[str, dict[str, Any]]:
    """Walk a Claude ~/.claude directory and build the files dict.

    Scans only projects/*/memory/ and projects/*/todos/ subdirectories.

    Args:
        base_dir: Root directory to walk (e.g., ~/.claude)
        max_file_size: Skip files larger than this (bytes). Default 50MB.
        on_skip: Optional callback(path, reason) for skipped files.

    Returns:
        Dict mapping relative paths to {sha256, size, mtime}.
    """
    base = Path(base_dir).expanduser().resolve()
    projects_dir = base / "projects"
    if not projects_dir.exists():
        return {}

    files: dict[str, dict[str, Any]] = {}

    # Only walk synced subdirs (memory/, todos/) within each project.
    # Structure: projects/{project-name}/{subdir}/...
    scan_dirs: list[Path] = []
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
            if result := _record_file(path, base, max_file_size, on_skip):
                rel, info = result
                files[rel] = info

    return files


def walk_generic_source(
    source_config: dict[str, Any],
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
) -> dict[str, dict[str, Any]]:
    """Walk a generic source directory with configurable include_dirs/include_files.

    Args:
        source_config: Dict with keys:
            path: Base directory path (supports ~)
            include_dirs: List of directory names to walk recursively
            include_files: List of filenames to check at root level
        max_file_size: Skip files larger than this (bytes). Default 50MB.
        on_skip: Optional callback(path, reason) for skipped files.

    Returns:
        Dict mapping relative paths (from base) to {sha256, size, mtime}.
    """
    base = Path(source_config["path"]).expanduser().resolve()
    if not base.exists():
        return {}

    include_dirs: list[str] = source_config.get("include_dirs", [])
    include_files: list[str] = source_config.get("include_files", [])

    files: dict[str, dict[str, Any]] = {}
    collected_paths: list[Path] = []

    # Walk each include_dir recursively
    for dir_name in include_dirs:
        scan_dir = base / dir_name
        if not scan_dir.exists() or not scan_dir.is_dir():
            continue
        for path in scan_dir.rglob("*"):
            if path.is_file():
                collected_paths.append(path)

    # Check each include_files entry at root level
    for filename in include_files:
        path = base / filename
        if path.exists() and path.is_file():
            collected_paths.append(path)

    for path in collected_paths:
        if result := _record_file(path, base, max_file_size, on_skip):
            rel, info = result
            files[rel] = info

    return files


def walk_source(
    source_config: dict[str, Any],
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Dispatch to the appropriate walker based on source type.

    Args:
        source_config: Dict with at least "type" and "path" keys.
            type="claude" -> walk_claude_source
            type="generic" -> walk_generic_source
        max_file_size: Skip files larger than this (bytes).
        on_skip: Optional callback(path, reason) for skipped files.

    Returns:
        Tuple of (resolved_base_path_str, files_dict).
    """
    source_type = source_config.get("type", "claude")
    base_path = str(Path(source_config["path"]).expanduser().resolve())

    if source_type == "claude":
        files = walk_claude_source(source_config["path"], max_file_size, on_skip)
    elif source_type == "generic":
        files = walk_generic_source(source_config, max_file_size, on_skip)
    else:
        raise ManifestError(f"manifest: unknown source type '{source_type}'")

    return base_path, files


def build_manifest_v2(
    device_id: str,
    device_name: str,
    sources_configs: list[dict[str, Any]],
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
) -> dict[str, Any]:
    """Build a v2 manifest with multiple sources.

    Args:
        device_id: Unique device identifier.
        device_name: Human-readable device name.
        sources_configs: List of source config dicts, each with at least
            "name", "type", and "path" keys.
        max_file_size: Skip files larger than this (bytes).
        on_skip: Optional callback(path, reason) for skipped files.

    Returns:
        v2 manifest dict with a "sources" dict keyed by source name.
    """
    sources: dict[str, dict[str, Any]] = {}

    for src_cfg in sources_configs:
        name = src_cfg["name"]
        base_path, files = walk_source(src_cfg, max_file_size, on_skip)
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


def load_manifest(data: bytes) -> dict[str, Any]:
    """Decode JSON bytes into a v2-normalized manifest dict.

    Single load boundary for every manifest path (remote fetch, sidecar
    recovery, test fixtures). Guarantees the returned dict has dict-typed
    `sources` and `tombstones`, each source entry has a dict-typed `files`,
    and each tombstone value is a dict. Callers may rely on these invariants.

    Raises ManifestError on bad bytes, non-dict top-level JSON, or any
    inner-shape violation. Enforcing the full shape at the load boundary
    turns a downstream AttributeError (deep in collect_tombstones,
    _merge_manifests, or the diff loop) into a clean recoverable error at
    the front door — `_fetch_remote_manifest` already catches ManifestError
    and falls through to the sidecar/peer recovery chain.
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
    for key, info in tombstones.items():
        if not isinstance(info, dict):
            raise ManifestError(f"manifest: tombstones[{key!r}] must be an object")
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
