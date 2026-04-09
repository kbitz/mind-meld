"""Local folder storage backend for MemSync.

Writes encrypted blobs to a local directory synced by iCloud Drive.
Includes conflict detection for iCloud and Dropbox-style conflicted copies.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from memsync.errors import StorageError

# iCloud conflict pattern: "filename 2.ext", "filename 3.ext"
_ICLOUD_CONFLICT_RE = re.compile(
    r"^(.+?)\s+(\d+)(\.[^.]+)$"
)

# Dropbox conflict pattern: "filename (conflicted copy YYYY-MM-DD).ext"
_DROPBOX_CONFLICT_RE = re.compile(
    r"^(.+?)\s+\((?:.*?conflicted copy.*?)\)(\.[^.]+)$", re.IGNORECASE
)


class LocalBackend:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to temp file, then rename
        try:
            fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.rename(tmp_path, path)
        except OSError as e:
            raise StorageError(f"storage: failed to write {key} — {e}") from e

    def get(self, key: str) -> bytes:
        path = self.root / key
        if not path.exists():
            raise StorageError(f"storage: file not found — {key}")
        try:
            return path.read_bytes()
        except OSError as e:
            raise StorageError(f"storage: failed to read {key} — {e}") from e

    def list_keys(self, prefix: str) -> list[str]:
        prefix_path = self.root / prefix
        if not prefix_path.exists():
            return []
        result = []
        # Walk the prefix directory
        base = prefix_path if prefix_path.is_dir() else prefix_path.parent
        if not base.exists():
            return []
        for path in base.rglob("*"):
            if path.is_file():
                rel = str(path.relative_to(self.root))
                if rel.startswith(prefix):
                    result.append(rel)
        return sorted(result)

    def delete(self, key: str) -> None:
        path = self.root / key
        if path.exists():
            try:
                path.unlink()
            except OSError as e:
                raise StorageError(f"storage: failed to delete {key} — {e}") from e

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    def find_conflict_copies(self, key: str) -> list[Path]:
        """Find iCloud or Dropbox-style conflicted copies of a file.

        iCloud creates: "manifest.json 2.enc", "manifest.json 3.enc"
        Dropbox creates: "manifest.json (conflicted copy 2026-03-18).enc"
        """
        path = self.root / key
        parent = path.parent
        if not parent.exists():
            return []

        stem_base = path.stem  # e.g., "manifest.json" (without .enc)
        ext = path.suffix  # e.g., ".enc"

        conflicts = []
        for f in parent.iterdir():
            if f == path or not f.is_file():
                continue
            # Check iCloud pattern
            m = _ICLOUD_CONFLICT_RE.match(f.name)
            if m and m.group(1) == stem_base and m.group(3) == ext:
                conflicts.append(f)
                continue
            # Check Dropbox pattern
            m = _DROPBOX_CONFLICT_RE.match(f.name)
            if m and m.group(1) == stem_base and m.group(2) == ext:
                conflicts.append(f)
        return sorted(conflicts)

    def delete_conflict_copies(self, key: str) -> int:
        """Delete all conflicted copies. Returns count deleted."""
        copies = self.find_conflict_copies(key)
        for c in copies:
            c.unlink(missing_ok=True)
        return len(copies)
