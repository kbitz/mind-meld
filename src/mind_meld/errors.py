"""Mind Meld error hierarchy.

All user-facing errors follow the format:
    [operation]: [what failed] — [why]. [what to do]
"""

from __future__ import annotations

import errno

from mind_meld.safety import safe_str

SNAPSHOT_FAILURES_URL = "https://github.com/kbitz/mind-meld#snapshot-failures"
PULL_FAILURES_URL = "https://github.com/kbitz/mind-meld#pull-incomplete--could-not-pull-a-file"
GIT_WALK_FAILURES_URL = (
    "https://github.com/kbitz/mind-meld#dropped-repositories-and-ignored-git-environment-variables"
)
HOST_USAGE_CAPTURE_URL = "https://github.com/kbitz/mind-meld#host-usage-capture"
CURSOR_SQLITE_URL = "https://github.com/kbitz/mind-meld#cursor-sqlite-stores"
NEWER_STORAGE_URL = "https://github.com/kbitz/mind-meld#newer-or-damaged-storage-format"
KEY_HEADER_COPIES_URL = "https://github.com/kbitz/mind-meld#key-header-copies-differ"
LOCK_HELD_URL = "https://github.com/kbitz/mind-meld#another-mm-operation-is-running"
KEYRING_UNAVAILABLE_URL = (
    "https://github.com/kbitz/mind-meld#auto-commands-skip-keyring-locked-or-unavailable"
)
STATEDB_HELP_URL = "https://github.com/kbitz/mind-meld#history-database-statedb"
SQLITE_UNAVAILABLE_REMEDY = (
    "mm's Python has no usable sqlite3 module; "
    "reinstall mm as described in the README Install section"
)


class MindMeldError(Exception):
    """Base exception for all Mind Meld errors."""


class CryptoError(MindMeldError):
    """Encryption or decryption failure."""


class KeyringUnavailableError(CryptoError):
    """The keyring cannot answer without a desktop prompt (Track 73A).

    Raised only on the non-interactive read path. `reason` is one of
    "locked", "denied", "no-default-collection"; `backend_kind` is one of
    "secret-service", "keychain", "other". `render(verb)` builds the
    remedy line for an auto command; `str(exc)` is the same text without
    the verb-specific rerun hint.
    """

    def __init__(self, reason: str, backend_kind: str) -> None:
        self.reason = reason
        self.backend_kind = backend_kind
        super().__init__(self.render())

    def render(self, verb: str | None = None) -> str:
        cell = (self.reason, self.backend_kind)
        if cell == ("locked", "secret-service"):
            lead = "keyring locked (Secret Service collection is locked)"
            action = "unlock your login keyring in the desktop session"
        elif cell == ("no-default-collection", "secret-service"):
            lead = "keyring has no default collection"
            action = "create one and store the passphrase with secret-tool"
        elif cell == ("denied", "keychain"):
            lead = "keyring locked (Keychain access was denied)"
            attended = f"mm {verb}" if verb else "mm"
            action = f"run {attended} in a terminal in your desktop session and choose Always Allow"
        elif cell == ("denied", "other"):
            lead = "keyring locked (desktop keyring refused access)"
            action = "unlock or allow access in your desktop keyring"
        else:
            lead = f"keyring unavailable ({self.reason})"
            action = "unlock or allow access in your desktop keyring"
        rerun = f", then rerun mm auto{verb}" if verb else ""
        return f"{lead}; {action}{rerun}. See {KEYRING_UNAVAILABLE_URL}"


class NewerFormatError(CryptoError):
    """An envelope uses a reserved future version; never repair it as garbage."""

    def __init__(self, version: int, subject: str = "blob") -> None:
        self.version = version
        self.subject = subject
        super().__init__(
            f"crypto: {subject}: unsupported format version 0x{version:02x}; "
            "written by a newer mm or damaged."
        )


class StorageError(MindMeldError):
    """Storage backend I/O failure."""


class StorageNotFound(StorageError):
    """A storage key is not there (Track 87A).

    Only ``FileNotFoundError`` maps here; every other ``OSError`` stays a plain
    ``StorageError`` (a failure, not an absence). ``vanished`` is True when the
    caller had observed the key (listed, ``exists``-checked or conflict-scanned)
    and it was still gone after the bounded retry, which on a Syncthing folder
    means a replacement was in flight. A key never seen reads missing at once
    with ``vanished`` False. Subclassing keeps ``except StorageError`` unchanged.
    """

    def __init__(self, key: str, *, vanished: bool = False) -> None:
        self.key = key
        self.vanished = vanished
        super().__init__(f"storage: file not found — {key}")


class ConfigError(MindMeldError):
    """Configuration parsing or validation failure."""


class ManifestError(MindMeldError):
    """Manifest corruption or incompatibility."""


class LockError(MindMeldError):
    """Concurrent operation conflict."""


class SnapshotError(MindMeldError):
    """A selected source could not be published as a complete snapshot.

    Per-file digest, size, and mtime describe one accepted file revision,
    and an incomplete scan is never treated as absence. This is not a
    filesystem-wide atomic snapshot.
    """


class SnapshotChanged(SnapshotError):
    """One file changed, disappeared or grew while mm read it (Track 75A).

    The only transient snapshot failure. Publishing scans and uploads re-read
    such a file once; a push that may hold it (``reason`` is one of
    ``changed``, ``vanished``, ``grew``) keeps its previously published entry
    and goes on. Every other ``SnapshotError`` (could not be read, enumeration,
    confinement, missing roots) is not transient and still refuses the whole
    push. Subclassing keeps ``except SnapshotError`` handlers unchanged.
    """

    def __init__(self, message: str, reason: str = "changed") -> None:
        super().__init__(message)
        self.reason = reason


def os_error_cause(exc: BaseException) -> str:
    """Bounded OS cause/errno for snapshot messages and hook logs."""
    if isinstance(exc, OSError):
        bits: list[str] = []
        if exc.errno is not None:
            name = errno.errorcode.get(exc.errno)
            if name:
                bits.append(name)
            bits.append(str(exc.errno))
        detail = exc.strerror or str(exc) or type(exc).__name__
        bits.append(safe_str(detail))
        return " ".join(bits)
    text = str(exc).strip() or type(exc).__name__
    return safe_str(text)


def snapshot_refusal(
    *,
    problem: str,
    next_action: str,
    source: str | None = None,
    rel_path: str | None = None,
    cause: str | None = None,
) -> str:
    """Four-part snapshot refusal: location, problem, preservation, next action."""
    location_bits: list[str] = []
    if source:
        location_bits.append(f"source {safe_str(source)}")
    if rel_path:
        location_bits.append(safe_str(rel_path))
    location = ", ".join(location_bits)
    if location:
        lead = f"Cannot publish: {location} {problem}"
    else:
        lead = f"Cannot publish: {problem}"
    if cause:
        lead = f"{lead} ({cause})"
    return f"{lead}. Previous snapshot kept. {next_action} See {SNAPSHOT_FAILURES_URL}."
