"""Storage backend ABC for MemSync.

Both backends implement this interface. All CLI logic is backend-agnostic.
"""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """Write data to the given key."""
        ...

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Read data from the given key. Raises StorageError if not found."""
        ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """List all keys under the given prefix."""
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete the given key. No-op if key doesn't exist."""
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if the given key exists."""
        ...
