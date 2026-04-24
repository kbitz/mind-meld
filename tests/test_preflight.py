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
        assert blob_key("x", "a" * 64).startswith(DATA_PREFIX)

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
        "bad_sha",
        [
            "a" * 63,                    # one char short
            "a" * 65,                    # one char too long
            "A" * 64,                    # uppercase hex (we require lowercase)
            "z" * 64,                    # non-hex char at every position
            "not-a-sha",                 # nonsense
            "0123456789abcdef" * 3 + "0123456789abcdeX",  # one bad char
        ],
    )
    def test_rejects_non_hex_sha(self, bad_sha):
        """blob_key must reject a sha that isn't exactly 64 lowercase hex
        chars. Prevents corrupt peer manifests from smuggling sentinel
        blobs through GC (they would parse-as-valid and get reaped as
        orphans under the old lax rule).
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

    @pytest.mark.parametrize(
        "non_string_sha",
        [None, 42, 3.14, b"raw-bytes", ["a" * 64], {"sha": "a" * 64}],
    )
    def test_rejects_non_string_sha_without_typeerror(self, non_string_sha):
        """A corrupt peer manifest shipping a non-string sha value (null,
        number, array, dict) must raise ValueError, not TypeError. Callers
        like _download_and_apply catch ValueError around blob_key() for
        per-file skip semantics; a TypeError escape would crash the whole
        pull.
        """
        with pytest.raises(ValueError):
            blob_key("dev1", non_string_sha)


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

    @pytest.mark.parametrize(
        "bad_sha_key",
        [
            "data/dev1/not-a-sha.enc",   # non-hex leaf (was accepted pre-1C)
            "data/dev1/" + "a" * 63 + ".enc",  # 63 chars
            "data/dev1/" + "a" * 65 + ".enc",  # 65 chars
            "data/dev1/" + "A" * 64 + ".enc",  # uppercase hex
            "data/dev1/" + "z" * 64 + ".enc",  # non-hex chars
        ],
    )
    def test_rejects_non_hex_sha_leaf(self, bad_sha_key):
        """Post-Track-1C: parse_blob_key rejects non-hex shas so `mm gc`
        routes them through the malformed-count (skipped) path instead of
        reaping them as "orphans" (they could never be in referenced_hashes
        since those hold real SHA-256 hex strings).
        """
        assert parse_blob_key(bad_sha_key) is None
