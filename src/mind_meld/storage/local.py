"""Local folder storage backend for Mind Meld.

Writes encrypted blobs to a local directory synced by iCloud Drive or
Syncthing. Includes conflict detection for iCloud, Dropbox and Syncthing
conflicted copies, and tolerates a sync tool that delivers files late and
replaces them mid-read (Track 87A): foreign names are ignored, and a file
the caller has seen vanish is retried briefly. Writes route through
`fsutil.atomic_write_bytes` for crash safety; durability policy is set per
key prefix:

    manifests/  → fsync=True (source of truth; loss = silent un-deletion)
    devices/    → fsync=True (peer discovery; pull and recovery inputs)
    mm-crypto-init* → fsync=True (canonical and preserved crypto lineage)
    data/       → fsync=False (hash-addressed blobs, self-healing via re-push)

Conflict-copy detection accepts an optional validator predicate that
confirms a candidate really IS a semantically valid match for the caller
(e.g., decrypts as a Mind Meld manifest). Without a validator, a random
file whose name happens to match the iCloud/Dropbox/Syncthing rename patterns
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
import time
from collections.abc import Callable
from pathlib import Path

from mind_meld import fsutil, safety
from mind_meld.errors import StorageError, StorageNotFound

# iCloud conflict pattern: "filename 2.ext", "filename 3.ext", or
# extensionless like "mm-crypto-init 2" (no suffix).
_ICLOUD_CONFLICT_RE = re.compile(r"^(.+?)\s+(\d+)(\.[^.]+)?$")

# Dropbox conflict pattern: "filename (conflicted copy YYYY-MM-DD).ext",
# or extensionless variant.
_DROPBOX_CONFLICT_RE = re.compile(
    r"^(.+?)\s+\((?:.*?conflicted copy.*?)\)(\.[^.]+)?$", re.IGNORECASE
)

# Syncthing conflict marker, inserted before the extension:
# "manifest.json.sync-conflict-20260101-101010-ABCDEFG.enc" (the device id is
# the first 7 characters and older versions omit it). A conflict of a conflict
# stacks the marker, and an extensionless key ("mm-crypto-init") keeps no ext.
_SYNCTHING_MARKER = r"\.sync-conflict-\d{8}-\d{6}(?:-[A-Za-z0-9]{7})?"
_SYNCTHING_CONFLICT_RE = re.compile(rf"^(.+?)(?:{_SYNCTHING_MARKER})+(\.[^.]+)?$")

# Syncthing's in-flight temporary files are ".syncthing.<name>.tmp" (and
# "~syncthing~<name>.tmp" on Windows); ".stfolder" and ".stversions" are its own
# bookkeeping. Every dot-prefixed or tilde-syncthing component is foreign.
_FOREIGN_PREFIXES = (".", "~syncthing~")

# A file the caller has seen vanishes when Syncthing replaces it. Retry the open
# on this schedule (about 0.35 s per file) and stop sleeping once a backend
# instance (one per command) has spent the budget; a key never seen is missing
# at once.
_VANISH_RETRY_DELAYS_S: tuple[float, ...] = (0.05, 0.1, 0.2)
_VANISH_BUDGET_S = 2.0

# Key prefixes whose writes must be durably flushed (F_FULLFSYNC on Darwin).
# data/ blobs are hash-addressed and re-uploadable, so they skip fsync for
# latency. See docs/invariants/sync.md "Atomic write publication failures"
# for the durability model.
_DURABLE_PREFIXES = ("manifests/", "devices/", "mm-crypto-init")


def _needs_fsync(key: str) -> bool:
    return key.startswith(_DURABLE_PREFIXES)


def is_foreign_name(name: str) -> bool:
    """True for a file or directory name that is never Mind Meld data: a
    dot-prefixed or Syncthing temporary name. The one name classifier behind
    `list_keys`, the conflict scan, the device table and `mm diag`."""
    return name.startswith(_FOREIGN_PREFIXES)


def is_foreign_key(key: str) -> bool:
    """True when any component of `key` is a foreign name."""
    return any(is_foreign_name(part) for part in key.split("/") if part)


def is_conflict_name(name: str) -> bool:
    """True when `name` has the shape of an iCloud, Dropbox or Syncthing conflict
    copy of some file, whichever one. Used where the original is not known (a
    file in ``devices/`` that is not a device)."""
    return not is_foreign_name(name) and bool(
        _ICLOUD_CONFLICT_RE.match(name)
        or _DROPBOX_CONFLICT_RE.match(name)
        or _SYNCTHING_CONFLICT_RE.match(name)
    )


def is_conflict_of(name: str, stem_base: str, ext: str) -> bool:
    """True when `name` is an iCloud, Dropbox or Syncthing conflict copy of the
    key whose stem is `stem_base` and whose extension (with dot, or "") is `ext`."""
    if is_foreign_name(name):
        return False
    m = _ICLOUD_CONFLICT_RE.match(name)
    if m and m.group(1) == stem_base and (m.group(3) or "") == ext:
        return True
    m = _DROPBOX_CONFLICT_RE.match(name)
    if m and m.group(1) == stem_base and (m.group(2) or "") == ext:
        return True
    m = _SYNCTHING_CONFLICT_RE.match(name)
    return bool(m and m.group(1) == stem_base and (m.group(2) or "") == ext)


class LocalBackend:
    def __init__(self, root: str | Path, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.root = Path(root).expanduser().resolve()
        self._sleep = sleep
        self._vanish_budget_s = _VANISH_BUDGET_S

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        fsutil.refuse_real_state_write_under_pytest(path)
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
        fsutil.refuse_real_state_write_under_pytest(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # A failed write or fsync must not leave a temp file for the next
            # run to trip over; a failed cleanup is left to that run's refusal.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
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

    def get(self, key: str, *, expected: bool = False) -> bytes:
        """Read `key`. Raises `StorageNotFound` when it is absent and any other
        failure as `StorageError`.

        `expected=True` says the caller observed the key (listed it, checked
        `exists` or found it by conflict scan), so an ENOENT at open is a file
        being replaced: retry briefly before reporting it vanished. Blobs and
        other keys never seen pass the default and read missing at once.
        """
        path = self.root / key
        delays = iter(_VANISH_RETRY_DELAYS_S if expected else ())
        while True:
            try:
                return path.read_bytes()
            except FileNotFoundError:
                delay = next(delays, None)
                if delay is None or delay > self._vanish_budget_s:
                    raise StorageNotFound(key, vanished=expected) from None
                self._vanish_budget_s -= delay
                self._sleep(delay)
            except OSError as e:
                raise StorageError(f"storage: failed to read {key} — {e}") from e

    def list_keys(self, prefix: str) -> list[str]:
        """Keys under `prefix`, skipping foreign names (dot-prefixed and
        Syncthing temporary files, and everything under a foreign directory)."""
        return sorted(self._walk(prefix, foreign=False))

    def list_foreign_keys(self, prefix: str = "") -> list[str]:
        """The files `list_keys` ignores under `prefix`, for `mm diag`."""
        return sorted(self._walk(prefix, foreign=True))

    def _walk(self, prefix: str, *, foreign: bool) -> list[str]:
        prefix_path = self.root / prefix
        if not prefix_path.exists():
            return []
        base = prefix_path if prefix_path.is_dir() else prefix_path.parent
        if not base.exists():
            return []
        result: list[str] = []
        stack = [(base, False)]
        while stack:
            directory, inside_foreign = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue  # replaced or removed mid-walk
            for entry in entries:
                entry_foreign = inside_foreign or is_foreign_name(entry.name)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        # Walk a foreign directory only to count it; its files
                        # are never keys.
                        if foreign or not entry_foreign:
                            stack.append((Path(entry.path), entry_foreign))
                        continue
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if entry_foreign != foreign:
                    continue
                rel = str(Path(entry.path).relative_to(self.root))
                if rel.startswith(prefix):
                    result.append(rel)
        return result

    def delete(self, key: str) -> None:
        path = self.root / key
        fsutil.refuse_real_state_write_under_pytest(path)
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
        """Find iCloud/Dropbox/Syncthing-style conflict copies of `key`.

        iCloud creates: "manifest.json 2.enc", "manifest.json 3.enc", or
                       extensionless "mm-crypto-init 2" (no suffix).
        Dropbox creates: "manifest.json (conflicted copy 2026-03-18).enc"
        Syncthing creates: "manifest.json.sync-conflict-20260318-101010-ABCDEFG.enc"
                       (stacked for a conflict of a conflict; extensionless
                       "mm-crypto-init.sync-conflict-...").

        A candidate that disappears before validation (Syncthing replacing it)
        is skipped silently; it is gone, not suspicious.

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
            if is_conflict_of(f.name, stem_base, ext):
                candidates.append(f)

        if is_valid is None:
            return sorted(candidates)

        confirmed: list[Path] = []
        for c in candidates:
            try:
                ok = is_valid(c)
            except Exception as e:
                if not c.exists():
                    continue
                reason = safety.safe_terminal_str(f"{type(e).__name__}: {e}")
                sys.stderr.write(
                    f"mm: warning: ignoring suspicious file {safety.safe_terminal_str(c)} "
                    f"(validator raised: {reason}). "
                    f"File left in place; inspect it in your storage folder.\n"
                )
                continue
            if ok:
                confirmed.append(c)
            elif c.exists():
                sys.stderr.write(
                    f"mm: warning: ignoring suspicious file {safety.safe_terminal_str(c)} "
                    f"(failed validation). File left in place; inspect it in your storage folder.\n"
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
        fsutil.refuse_real_state_write_under_pytest(self.root / key)
        copies = self.find_conflict_copies(key, is_valid)
        for c in copies:
            c.unlink(missing_ok=True)
        return len(copies)
