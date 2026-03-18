"""S3-compatible storage backend for MemSync.

Works with AWS S3, Cloudflare R2, MinIO, and any S3-compatible service.
Requires boto3 (install with: pip install memsync[s3]).
"""

from __future__ import annotations

from memsync.errors import StorageError
from memsync.storage.base import StorageBackend


class S3Backend(StorageBackend):
    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        try:
            import boto3
        except ImportError:
            raise ImportError(
                "S3 backend requires boto3. Install with: pip install memsync[s3]"
            )

        self.bucket = bucket
        kwargs: dict = {"region_name": region}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self._client = boto3.client("s3", **kwargs)

    def put(self, key: str, data: bytes) -> None:
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except self._client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "403" or "AccessDenied" in str(e):
                raise StorageError(
                    f"push: failed to upload {key} — S3 access denied. "
                    "Check your IAM credentials."
                ) from e
            raise StorageError(f"push: failed to upload {key} — {e}") from e
        except Exception as e:
            raise StorageError(f"push: failed to upload {key} — {e}") from e

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except self._client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "404" or "NoSuchKey" in str(e):
                raise StorageError(f"pull: file not found in S3 — {key}") from e
            if code == "403" or "AccessDenied" in str(e):
                raise StorageError(
                    f"pull: S3 access denied for {key}. Check your IAM credentials."
                ) from e
            raise StorageError(f"pull: failed to download {key} — {e}") from e
        except Exception as e:
            raise StorageError(f"pull: failed to download {key} — {e}") from e

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        except Exception as e:
            raise StorageError(f"status: failed to list keys — {e}") from e
        return sorted(keys)

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as e:
            raise StorageError(f"gc: failed to delete {key} — {e}") from e

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except self._client.exceptions.ClientError:
            return False
        except Exception as e:
            raise StorageError(f"status: failed to check {key} — {e}") from e
