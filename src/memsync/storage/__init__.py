"""Storage backends for MemSync."""

from memsync.storage.base import StorageBackend
from memsync.storage.local import LocalBackend

__all__ = ["StorageBackend", "LocalBackend", "get_backend"]


def get_backend(config: dict) -> StorageBackend:
    """Factory: return the right StorageBackend based on config."""
    backend_type = config["storage"]["backend"]
    if backend_type == "local":
        return LocalBackend(config["storage"]["path"])
    elif backend_type == "s3":
        try:
            from memsync.storage.s3 import S3Backend
        except ImportError:
            raise ImportError(
                "S3 backend requires boto3. Install with: pip install memsync[s3]"
            )
        return S3Backend(
            bucket=config["storage"]["bucket"],
            region=config["storage"].get("region", "us-east-1"),
            endpoint_url=config["storage"].get("endpoint_url") or None,
        )
    else:
        raise ValueError(f"Unknown storage backend: {backend_type!r}")
