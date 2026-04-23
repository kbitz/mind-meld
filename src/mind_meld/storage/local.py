"""Local folder storage backend for Mind Meld.

Writes encrypted blobs to a local directory synced by iCloud Drive.
Includes conflict detection for iCloud and Dropbox-style conflicted copies.
"""

from __future__ import annotations

import errno
import os
import re
import tempfile
from pathlib import Path

from mind_meld.errors import StorageError

# iCloud conflict pattern: "filename 2.ext", "filename 3.ext", or extensionless
# like "mm-crypto-init 2". Extension is optional to accommodate bootstrap blobs
# without a suffix.
_ICLOUD_CONFLICT_RE = re.compile(
    r"^(.+?)\s+(\d+)(\.[^.]+)?$"
)

# Dropbox conflict pattern: "filename (conflicted copy YYYY-MM-DD).ext", or
# extensionless variant.
_DROPBOX_CONFLICT_RE = re.compile(
    r"^(.+?)\s+\((?:.*?conflicted copy.*?)\)(\.[^.]+)?$", re.IGNORECASE
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

    def put_exclusive(self, key: str, data: bytes) -> None:
        """Atomic create-only write. Fails StorageError if target already exists.

        Used for bootstrap-once files like mm-crypto-init. Local-only coordination
        (O_CREAT|O_EXCL semantics via os.link) — NOT cross-device coordination.
        Two Macs writing simultaneously via iCloud will both succeed locally; iCloud
        reconciles later by renaming one to a conflict copy. Callers that need
        cross-device convergence must combine this with conflict-copy scanning and
        deterministic winner selection (see crypto.fetch_crypto_init).

        Implementation: write to a temp file, then os.link(tmp, target). os.link
        is atomic AND fails with EEXIST if the target exists. Both properties we
        need: atomicity (readers never see a partial file) and exclusivity.
        """
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp_path, path)
        except OSError as e:
            if e.errno == errno.EEXIST:
                # Loser of the race. Clean up our temp file and signal caller.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise StorageError(
                    f"storage: {key} already exists (put_exclusive)."
                ) from e
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise StorageError(f"storage: failed to link {key} — {e}") from e
        # Linked; remove the temp so we don't leak a sibling.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

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

        iCloud creates: "manifest.json 2.enc", "manifest.json 3.enc", or
                       extensionless "mm-crypto-init 2" (no suffix).
        Dropbox creates: "manifest.json (conflicted copy 2026-03-18).enc"
        """
        path = self.root / key
        parent = path.parent
        if not parent.exists():
            return []

        # Use path.name's stem/suffix split rather than Path.stem/suffix, which
        # behave awkwardly when there's no extension. For "mm-crypto-init" the
        # full name IS the stem and suffix is "".
        name = path.name
        if "." in name:
            # Last dot delimits stem/suffix as Path would do.
            stem_base = path.stem
            ext = path.suffix
        else:
            stem_base = name
            ext = ""

        conflicts = []
        for f in parent.iterdir():
            if f == path or not f.is_file():
                continue
            # Check iCloud pattern
            m = _ICLOUD_CONFLICT_RE.match(f.name)
            if m and m.group(1) == stem_base and (m.group(3) or "") == ext:
                conflicts.append(f)
                continue
            # Check Dropbox pattern
            m = _DROPBOX_CONFLICT_RE.match(f.name)
            if m and m.group(1) == stem_base and (m.group(2) or "") == ext:
                conflicts.append(f)
        return sorted(conflicts)

    def delete_conflict_copies(self, key: str) -> int:
        """Delete all conflicted copies. Returns count deleted."""
        copies = self.find_conflict_copies(key)
        for c in copies:
            c.unlink(missing_ok=True)
        return len(copies)
