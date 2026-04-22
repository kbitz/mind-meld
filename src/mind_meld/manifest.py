"""Manifest building and diffing for Mind Meld.

Walks ~/.claude/projects/*/memory/ and ~/.claude/projects/*/todos/ only (claude source).
Also supports generic sources with configurable include_dirs/include_files.

Builds truth-based manifest snapshots and diffs them to find changes.

Manifest formats:
  v1 — flat "files" dict, single claude source (backward compat)
  v2 — "sources" dict keyed by source name, each with base_path + files
        Also carries "files" for v1 compat (claude source only)
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mind_meld.errors import ManifestError

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
    for pattern in EXCLUDED:
        # Directory patterns (ending with /)
        if pattern.endswith("/"):
            dir_name = pattern.rstrip("/")
            if dir_name in parts:
                return True
        # File patterns
        else:
            filename = parts[-1]
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

            rel = str(path.relative_to(base))

            if _is_excluded(rel):
                continue

            try:
                stat = path.stat()
            except PermissionError:
                if on_skip:
                    on_skip(rel, "permission denied")
                continue

            if stat.st_size > max_file_size:
                if on_skip:
                    size_mb = stat.st_size / (1024 * 1024)
                    on_skip(rel, f"exceeds max_file_size ({size_mb:.1f}MB)")
                continue

            try:
                sha = hash_file(path)
            except (PermissionError, OSError):
                if on_skip:
                    on_skip(rel, "read error")
                continue

            files[rel] = {
                "sha256": sha,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }

    return files


def walk_directory(
    base_dir: str | Path,
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
) -> dict[str, dict[str, Any]]:
    """Walk a directory and build the files dict for a manifest.

    Backward-compat alias for walk_claude_source().
    """
    return walk_claude_source(base_dir, max_file_size, on_skip)


def build_manifest(
    device_id: str,
    device_name: str,
    claude_dir: str,
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
) -> dict[str, Any]:
    """Build a complete manifest dict."""
    files = walk_directory(claude_dir, max_file_size, on_skip)
    return {
        "device_id": device_id,
        "device_name": device_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "base_path": str(Path(claude_dir).expanduser().resolve()),
        "files": files,
    }


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
        rel = str(path.relative_to(base))

        if _is_excluded(rel):
            continue

        try:
            stat = path.stat()
        except PermissionError:
            if on_skip:
                on_skip(rel, "permission denied")
            continue

        if stat.st_size > max_file_size:
            if on_skip:
                size_mb = stat.st_size / (1024 * 1024)
                on_skip(rel, f"exceeds max_file_size ({size_mb:.1f}MB)")
            continue

        try:
            sha = hash_file(path)
        except (PermissionError, OSError):
            if on_skip:
                on_skip(rel, "read error")
            continue

        files[rel] = {
            "sha256": sha,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }

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
        v2 manifest dict with both "files" (v1 compat) and "sources".
    """
    sources: dict[str, dict[str, Any]] = {}
    claude_files: dict[str, dict[str, Any]] = {}

    for src_cfg in sources_configs:
        name = src_cfg["name"]
        base_path, files = walk_source(src_cfg, max_file_size, on_skip)
        sources[name] = {
            "base_path": base_path,
            "files": files,
        }
        # v1 compat: "files" at top level is the claude source only
        if name == "claude":
            claude_files = files

    return {
        "device_id": device_id,
        "device_name": device_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": claude_files,
        "sources": sources,
    }


def normalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Ensure a manifest has the v2 "sources" structure and tombstones.

    If the manifest already has "sources", return as-is.
    If it only has "files" (v1 format), wrap the files into a
    claude source entry under "sources".

    Always ensures "tombstones" key exists (empty dict for old manifests).

    Returns:
        The manifest dict (mutated in place) with "sources" and "tombstones" guaranteed.
    """
    if "sources" not in manifest:
        manifest["sources"] = {
            "claude": {
                "base_path": manifest.get("base_path", ""),
                "files": manifest.get("files", {}),
            }
        }

    if "tombstones" not in manifest:
        manifest["tombstones"] = {}

    return manifest


def serialize_manifest(manifest: dict[str, Any]) -> bytes:
    """Serialize manifest to JSON bytes."""
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


def deserialize_manifest(data: bytes) -> dict[str, Any]:
    """Deserialize manifest from JSON bytes."""
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ManifestError(f"manifest: failed to parse — {e}") from e


class DiffResult:
    """Result of diffing two manifests."""

    def __init__(
        self,
        new: dict[str, dict],
        modified: dict[str, dict],
        deleted: list[str],
        unchanged: list[str],
    ):
        self.new = new
        self.modified = modified
        self.deleted = deleted
        self.unchanged = unchanged

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.modified or self.deleted)

    def __repr__(self) -> str:
        return (
            f"DiffResult(new={len(self.new)}, modified={len(self.modified)}, "
            f"deleted={len(self.deleted)}, unchanged={len(self.unchanged)})"
        )


def diff_manifests(
    local: dict[str, Any],
    remote: dict[str, Any] | None,
) -> DiffResult:
    """Diff local manifest against remote. Returns what changed.

    Truth-based: local manifest is the source of truth.
    Files in local but not remote → new.
    Files in both but different hash → modified.
    Files in remote but not local → deleted.
    """
    local_files = local.get("files", {})
    remote_files = remote.get("files", {}) if remote else {}

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


def generate_tombstones(
    local_manifest: dict[str, Any],
    remote_manifest: dict[str, Any] | None,
    device_id: str,
) -> dict[str, dict[str, str]]:
    """Generate tombstones for files that disappeared since last push.

    Compares current local manifest against the previous remote manifest.
    Files that were in remote but are no longer in local get a tombstone.
    Existing non-expired tombstones from the remote manifest carry forward.

    Returns:
        Dict mapping relative paths to {"deleted_at": ISO timestamp, "device_id": str}.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=TOMBSTONE_TTL_DAYS)
    now_iso = now.isoformat()
    tombstones: dict[str, dict[str, str]] = {}

    # Carry forward non-expired tombstones from remote manifest
    if remote_manifest:
        for path, info in remote_manifest.get("tombstones", {}).items():
            deleted_at = info.get("deleted_at", "")
            try:
                ts = datetime.fromisoformat(deleted_at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts > cutoff:
                    tombstones[path] = info
            except (ValueError, TypeError):
                pass  # drop unparseable tombstones

    # Detect new tombstones: files in remote manifest but not in local
    # Keys are "source:path" to prevent cross-source suppression
    if remote_manifest:
        normalize_manifest(remote_manifest)
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
    tombstones = {
        key: info for key, info in tombstones.items()
        if key not in all_local_keys
    }

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
            deleted_at = info.get("deleted_at", "")
            try:
                ts = datetime.fromisoformat(deleted_at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts <= cutoff:
                    continue  # expired
            except (ValueError, TypeError):
                continue  # unparseable

            existing = all_tombstones.get(path)
            if existing is None or deleted_at > existing.get("deleted_at", ""):
                all_tombstones[path] = info

    return all_tombstones


def is_tombstoned(source: str, rel_path: str, tombstones: dict[str, dict[str, str]]) -> bool:
    """Check if a source:path has an active tombstone."""
    return f"{source}:{rel_path}" in tombstones
