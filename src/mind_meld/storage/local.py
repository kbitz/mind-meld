"""Local folder storage backend for Mind Meld.

Writes encrypted blobs to a local directory synced by iCloud Drive.
Includes conflict detection for iCloud and Dropbox-style conflicted
copies. Writes route through `fsutil.atomic_write_bytes` for crash
safety; durability policy is set per key prefix:

    manifests/  → fsync=True (source of truth; loss = silent un-deletion)
    devices/    → fsync=True (peer discovery / GC inputs)
    data/       → fsync=False (hash-addressed blobs, self-healing via re-push)

Conflict-copy detection accepts an optional validator predicate that
confirms a candidate really IS a semantically valid match for the caller
(e.g., decrypts as a Mind Meld manifest). Without a validator, a random
file whose name happens to match the iCloud/Dropbox rename patterns
would pollute the candidate set — that's fine for some callers (the
crypto-v2 bootstrap path validates each candidate after the fact via
`_parse_crypto_init`) and a real problem for others (manifest recovery,
where a bogus sibling can spuriously flip status=missing→status=corrupt).
"""

from __future__ import annotations

import errno
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from mind_meld import fsutil
from mind_meld.errors import StorageError

# iCloud conflict pattern: "filename 2.ext", "filename 3.ext", or
# extensionless like "mm-crypto-init 2" (no suffix).
_ICLOUD_CONFLICT_RE = re.compile(r"^(.+?)\s+(\d+)(\.[^.]+)?$")

# Dropbox conflict pattern: "filename (conflicted copy YYYY-MM-DD).ext",
# or extensionless variant.
_DROPBOX_CONFLICT_RE = re.compile(
    r"^(.+?)\s+\((?:.*?conflicted copy.*?)\)(\.[^.]+)?$", re.IGNORECASE
)

# Key prefixes whose writes must be durably flushed (F_FULLFSYNC on Darwin).
# data/ blobs are hash-addressed and re-uploadable, so they skip fsync for
# latency. See CLAUDE.md "truth-based manifests" for the durability model.
_DURABLE_PREFIXES = ("manifests/", "devices/")


def _needs_fsync(key: str) -> bool:
    return key.startswith(_DURABLE_PREFIXES)


class LocalBackend:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        # All storage keys hold encrypted secrets (blobs, manifests,
        # devices, crypto-init) — explicit 0600 so new files aren't
        # world-readable via umask.
        fsutil.atomic_write_bytes(path, data, fsync=_needs_fsync(key), mode=0o600)

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
                raise StorageError(f"storage: {key} already exists (put_exclusive).") from e
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

    def find_conflict_copies(
        self,
        key: str,
        is_valid: Callable[[Path], bool] | None = None,
    ) -> list[Path]:
        """Find iCloud/Dropbox-style conflict copies of `key`.

        iCloud creates: "manifest.json 2.enc", "manifest.json 3.enc", or
                       extensionless "mm-crypto-init 2" (no suffix).
        Dropbox creates: "manifest.json (conflicted copy 2026-03-18).enc"

        The regex patterns here are loose by design — any sibling whose
        name matches is a CANDIDATE. The optional `is_valid` predicate
        is how the caller confirms a candidate is semantically legitimate
        (e.g., decrypts as a Mind Meld manifest). Without the predicate,
        ANY regex-matching sibling is returned — which is fine when the
        caller validates each candidate itself (crypto-v2 bootstrap path)
        and a correctness concern when it doesn't (manifest recovery,
        where a bogus sibling can spuriously flip missing→corrupt).

        Args:
            key: storage key. Can be any path; the caller supplies whatever
                validation semantics they need via `is_valid`.
            is_valid: optional predicate. If provided, only candidates for
                which `is_valid(path)` returns True are included. Exceptions
                from the predicate are caught, logged to stderr, and the
                candidate is treated as False (never as "conflict"). If
                omitted, all regex-matching candidates are returned.

        Returns:
            List of conflict copy paths, sorted lexicographically.
        """
        path = self.root / key
        parent = path.parent
        if not parent.exists():
            return []

        # Use path.name's stem/suffix split rather than Path.stem/suffix,
        # which behave awkwardly when there's no extension. For
        # "mm-crypto-init" the full name IS the stem and suffix is "".
        name = path.name
        if "." in name:
            stem_base = path.stem
            ext = path.suffix
        else:
            stem_base = name
            ext = ""

        candidates: list[Path] = []
        for f in parent.iterdir():
            if f == path or not f.is_file():
                continue
            m = _ICLOUD_CONFLICT_RE.match(f.name)
            if m and m.group(1) == stem_base and (m.group(3) or "") == ext:
                candidates.append(f)
                continue
            m = _DROPBOX_CONFLICT_RE.match(f.name)
            if m and m.group(1) == stem_base and (m.group(2) or "") == ext:
                candidates.append(f)

        if is_valid is None:
            return sorted(candidates)

        confirmed: list[Path] = []
        for c in candidates:
            try:
                ok = is_valid(c)
            except Exception as e:
                sys.stderr.write(f"warning: ignoring suspicious file {c} (validator raised: {e})\n")
                continue
            if ok:
                confirmed.append(c)
            else:
                sys.stderr.write(
                    f"warning: ignoring suspicious file {c} "
                    f"(failed validation). If you re-ran `mm init` with "
                    f"a new passphrase, old conflict copies stay "
                    f"unreadable until you remove them manually or run "
                    f"`mm gc --conflicts`.\n"
                )
        return sorted(confirmed)

    def delete_conflict_copies(
        self,
        key: str,
        is_valid: Callable[[Path], bool] | None = None,
    ) -> int:
        """Delete conflict copies of `key` (filtered by optional predicate).

        When `is_valid` is provided, only candidates the predicate confirms
        are deleted — predicate-rejected siblings (unrelated files, bytes
        from an older passphrase) are left on disk with a stderr warning.
        When `is_valid` is omitted, all regex-matching siblings are deleted.

        Returns the count deleted.
        """
        copies = self.find_conflict_copies(key, is_valid)
        for c in copies:
            c.unlink(missing_ok=True)
        return len(copies)
