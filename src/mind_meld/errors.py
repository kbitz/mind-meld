"""Mind Meld error hierarchy.

All user-facing errors follow the format:
    [operation]: [what failed] — [why]. [what to do]
"""


class MindMeldError(Exception):
    """Base exception for all Mind Meld errors."""


class CryptoError(MindMeldError):
    """Encryption or decryption failure."""


class StorageError(MindMeldError):
    """Storage backend I/O failure."""


class ConfigError(MindMeldError):
    """Configuration parsing or validation failure."""


class ManifestError(MindMeldError):
    """Manifest corruption or incompatibility."""


class LockError(MindMeldError):
    """Concurrent operation conflict."""
