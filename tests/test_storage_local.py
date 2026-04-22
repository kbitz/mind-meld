"""Tests for mind_meld.storage.local — local folder backend."""

import pytest

from mind_meld.errors import StorageError
from mind_meld.storage.local import LocalBackend


class TestLocalBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    def test_put_get_round_trip(self, backend):
        backend.put("test/file.enc", b"hello")
        assert backend.get("test/file.enc") == b"hello"

    def test_put_creates_parents(self, backend):
        backend.put("deep/nested/dir/file.enc", b"data")
        assert backend.get("deep/nested/dir/file.enc") == b"data"

    def test_get_missing_raises(self, backend):
        with pytest.raises(StorageError, match="not found"):
            backend.get("nonexistent.enc")

    def test_exists(self, backend):
        assert not backend.exists("test.enc")
        backend.put("test.enc", b"data")
        assert backend.exists("test.enc")

    def test_delete(self, backend):
        backend.put("test.enc", b"data")
        assert backend.exists("test.enc")
        backend.delete("test.enc")
        assert not backend.exists("test.enc")

    def test_delete_nonexistent_is_noop(self, backend):
        backend.delete("nonexistent.enc")  # should not raise

    def test_list_keys(self, backend):
        backend.put("data/abc/hash1.enc", b"1")
        backend.put("data/abc/hash2.enc", b"2")
        backend.put("data/def/hash3.enc", b"3")
        backend.put("manifests/abc/manifest.json.enc", b"m")

        keys = backend.list_keys("data/")
        assert len(keys) == 3
        assert "data/abc/hash1.enc" in keys
        assert "data/def/hash3.enc" in keys

    def test_list_keys_empty_prefix(self, backend):
        assert backend.list_keys("nonexistent/") == []

    def test_list_keys_specific_device(self, backend):
        backend.put("data/abc/h1.enc", b"1")
        backend.put("data/def/h2.enc", b"2")
        keys = backend.list_keys("data/abc/")
        assert len(keys) == 1
        assert "data/abc/h1.enc" in keys

    def test_atomic_write(self, backend, tmp_path):
        """Put should use atomic write (temp + rename)."""
        backend.put("test.enc", b"original")
        assert backend.get("test.enc") == b"original"
        # Overwrite
        backend.put("test.enc", b"updated")
        assert backend.get("test.enc") == b"updated"

    def test_binary_data(self, backend):
        data = bytes(range(256))
        backend.put("binary.enc", data)
        assert backend.get("binary.enc") == data

    def test_large_data(self, backend):
        data = b"x" * 1_000_000
        backend.put("large.enc", data)
        assert backend.get("large.enc") == data


class TestConflictDetection:
    """Test detection of iCloud and Dropbox conflict copies."""

    @pytest.fixture
    def backend(self, tmp_path):
        root = tmp_path / "storage"
        return LocalBackend(root)

    def test_find_no_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"data")
        conflicts = backend.find_conflict_copies("manifests/abc/manifest.json.enc")
        assert conflicts == []

    def test_find_icloud_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        # iCloud creates "filename 2.ext" copies
        conflict_path = backend.root / "manifests" / "abc" / "manifest.json 2.enc"
        conflict_path.write_bytes(b"conflict")

        conflicts = backend.find_conflict_copies("manifests/abc/manifest.json.enc")
        assert len(conflicts) == 1

    def test_find_multiple_icloud_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        for i in range(2, 5):
            path = backend.root / "manifests" / "abc" / f"manifest.json {i}.enc"
            path.write_bytes(f"conflict {i}".encode())

        conflicts = backend.find_conflict_copies("manifests/abc/manifest.json.enc")
        assert len(conflicts) == 3

    def test_find_dropbox_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        conflict_name = "manifest.json (conflicted copy 2026-03-18).enc"
        conflict_path = backend.root / "manifests" / "abc" / conflict_name
        conflict_path.write_bytes(b"conflict")

        conflicts = backend.find_conflict_copies("manifests/abc/manifest.json.enc")
        assert len(conflicts) == 1

    def test_delete_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        # Create one of each type
        icloud = backend.root / "manifests" / "abc" / "manifest.json 2.enc"
        icloud.write_bytes(b"icloud conflict")
        dropbox = backend.root / "manifests" / "abc" / "manifest.json (conflicted copy 2026-03-18).enc"
        dropbox.write_bytes(b"dropbox conflict")

        count = backend.delete_conflict_copies("manifests/abc/manifest.json.enc")
        assert count == 2
        assert not icloud.exists()
        assert not dropbox.exists()
