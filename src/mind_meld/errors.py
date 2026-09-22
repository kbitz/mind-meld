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
HOST_USAGE_CAPTURE_URL = "https://github.com/kbitz/mind-meld#host-usage-capture-codex-and-grok"
NEWER_STORAGE_URL = "https://github.com/kbitz/mind-meld#newer-or-damaged-storage-format"


class MindMeldError(Exception):
    """Base exception for all Mind Meld errors."""


class CryptoError(MindMeldError):
    """Encryption or decryption failure."""


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
