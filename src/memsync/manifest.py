"""Manifest building and diffing for MemSync.

Walks ~/.claude/projects/*/memory/ and ~/.claude/projects/*/todos/ only.
Other subdirectories (sessions, settings, etc.) are intentionally excluded —
they're either tracked via git or are ephemeral conversation transcripts.

Builds truth-based manifest snapshots and diffs them to find changes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memsync.errors import ManifestError

EXCLUDED = [
    "node_modules/",
    ".git/",
    ".DS_Store",
    ".env",
    ".env.*",
    "*.log",
    ".claude-sync/",
    "dist/",
    "build/",
    ".next/",
    ".turbo/",
    "__pycache__/",
    "*.pyc",
    ".memsync-log.md",
]

# Only sync these subdirectories within each project.
# Everything else (sessions, settings, etc.) is either git-tracked or ephemeral.
SYNCED_SUBDIRS = ["memory", "todos"]


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


def walk_directory(
    base_dir: str | Path,
    max_file_size: int = 52_428_800,
    on_skip: Any = None,
) -> dict[str, dict[str, Any]]:
    """Walk a directory and build the files dict for a manifest.

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
