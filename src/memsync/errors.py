"""MemSync error hierarchy.

All user-facing errors follow the format:
    [operation]: [what failed] — [why]. [what to do]
"""


class MemSyncError(Exception):
    """Base exception for all MemSync errors."""


class CryptoError(MemSyncError):
    """Encryption or decryption failure."""


class StorageError(MemSyncError):
    """Storage backend I/O failure."""


class ConfigError(MemSyncError):
    """Configuration parsing or validation failure."""


class ManifestError(MemSyncError):
    """Manifest corruption or incompatibility."""


class LockError(MemSyncError):
    """Concurrent operation conflict."""
