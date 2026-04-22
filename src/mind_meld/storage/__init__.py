"""Storage backend for Mind Meld."""

from mind_meld.storage.local import LocalBackend

__all__ = ["LocalBackend", "get_backend"]


def get_backend(config: dict) -> LocalBackend:
    """Return a LocalBackend pointed at the configured storage path."""
    return LocalBackend(config["storage"]["path"])
