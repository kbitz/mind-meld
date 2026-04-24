"""Group 2 pre-flight — storage key helpers.

Pure unit tests for src/mind_meld/storage/keys.py. No filesystem, no
backend — just string construction and parsing.
"""

from __future__ import annotations

import pytest

from mind_meld.storage.keys import (
    CRYPTO_INIT_KEY,
    DATA_PREFIX,
    DEVICES_PREFIX,
    MANIFESTS_PREFIX,
    blob_key,
    device_key,
    manifest_key,
    parse_blob_key,
)


class TestPrefixes:
    def test_prefixes_are_trailing_slash_strings(self):
        assert MANIFESTS_PREFIX == "manifests/"
        assert DATA_PREFIX == "data/"
        assert DEVICES_PREFIX == "devices/"

    def test_crypto_init_key_is_root_scoped(self):
        # No prefix — lives at the storage root.
        assert CRYPTO_INIT_KEY == "mm-crypto-init"
        assert "/" not in CRYPTO_INIT_KEY


class TestManifestKey:
    def test_shape_matches_historical_literal(self):
        assert manifest_key("abc123") == "manifests/abc123/manifest.json.enc"

    def test_uses_prefix(self):
        assert manifest_key("x").startswith(MANIFESTS_PREFIX)

    @pytest.mark.parametrize(
        "bad_did",
        ["", ".", "..", "a/b", "a\\b", "../etc", "a\x00b"],
    )
    def test_rejects_path_traversal(self, bad_did):
        with pytest.raises(ValueError):
            manifest_key(bad_did)


class TestBlobKey:
    def test_shape_matches_historical_literal(self):
        sha = "a" * 64
        assert blob_key("dev1", sha) == f"data/dev1/{sha}.enc"

    def test_uses_prefix(self):
        assert blob_key("x", "y").startswith(DATA_PREFIX)

    @pytest.mark.parametrize(
        "bad_sha",
        ["", ".", "..", "a/b", "a\\b", "../../../etc/passwd", "a\x00b"],
    )
    def test_rejects_malicious_sha(self, bad_sha):
        """Corrupt/malicious manifest shipping a sha with path separators
        must be rejected at key construction, not smuggled into backend.get.
        """
        with pytest.raises(ValueError):
            blob_key("dev1", bad_sha)

    @pytest.mark.parametrize(
        "bad_did",
        ["", ".", "..", "a/b", "../etc", "a\x00b"],
    )
    def test_rejects_malicious_device_id(self, bad_did):
        with pytest.raises(ValueError):
            blob_key(bad_did, "a" * 64)


class TestDeviceKey:
    def test_shape_matches_historical_literal(self):
        assert device_key("dev1") == "devices/dev1.json"

    def test_uses_prefix(self):
        assert device_key("x").startswith(DEVICES_PREFIX)

    @pytest.mark.parametrize(
        "bad_did",
        ["", ".", "..", "a/b", "a\\b", "../etc", "a\x00b"],
    )
    def test_rejects_path_traversal(self, bad_did):
        with pytest.raises(ValueError):
            device_key(bad_did)


class TestParseBlobKey:
    def test_happy_path(self):
        sha = "f" * 64
        parsed = parse_blob_key(f"data/dev1/{sha}.enc")
        assert parsed == ("dev1", sha)

    def test_roundtrip(self):
        sha = "0123456789abcdef" * 4  # 64 chars
        did = "abcd1234"
        key = blob_key(did, sha)
        assert parse_blob_key(key) == (did, sha)

    @pytest.mark.parametrize(
        "bad_key",
        [
            "manifests/dev/manifest.json.enc",  # wrong prefix
            "data/dev/sha.txt",  # wrong suffix
            "data/devsha.enc",  # wrong depth (2 parts)
            "data/dev/foo/sha.enc",  # wrong depth (4 parts)
            "data//sha.enc",  # empty device_id
            "data/dev/.enc",  # empty sha
            "data/dev/sha.tmp",  # wrong suffix
            "",  # empty
            "data/",  # prefix only
        ],
    )
    def test_malformed_returns_none(self, bad_key):
        assert parse_blob_key(bad_key) is None

    def test_arbitrary_sha_bytes_accepted(self):
        # Depth-check only; does NOT validate hex shape of sha.
        # Shape validation is a separate TODO (GC: validate blob shape).
        parsed = parse_blob_key("data/dev1/not-a-sha.enc")
        assert parsed == ("dev1", "not-a-sha")
