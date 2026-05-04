"""Property-based fuzz tests for manifest read-path invariants.

Hypothesis exercises `normalize_manifest`, `load_manifest`, and
`is_conflict_filename` over wide input distributions to catch the
"weird-shape manifest crashes the loader" class of bug that unit tests
miss by design.

Invariants under test:
- normalize_manifest never raises on dict input
- normalize_manifest is idempotent
- After load_manifest(serialize_manifest(m)), `sources` and `tombstones`
  are present and are dicts
- After v1→v2 promotion, every tombstone key whose original form was
  bare contains exactly one ":" prefix and starts with "claude:"
- is_conflict_filename never raises on string input
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from mind_meld.manifest import (
    deserialize_manifest,
    is_conflict_filename,
    load_manifest,
    normalize_manifest,
    serialize_manifest,
)

# Strategies ----------------------------------------------------------------

# Valid tombstone values are always dicts. Strategy used for round-trip
# tests where load_manifest must succeed.
valid_tombstone_value_strategy = st.one_of(
    st.fixed_dictionaries(
        {
            "deleted_at": st.text(min_size=0, max_size=40),
            "device_id": st.text(min_size=0, max_size=40),
        }
    ),
    st.dictionaries(st.text(min_size=0, max_size=10), st.text(min_size=0, max_size=20), max_size=4),
)

# Wild tombstone values: dicts AND junk (text/none/int). Strategy used for
# normalize_manifest crash-resistance tests where the function must tolerate
# arbitrary input. load_manifest will REJECT these via ManifestError; that's
# the intended hardened behavior post-cross-model-review.
wild_tombstone_value_strategy = st.one_of(
    valid_tombstone_value_strategy,
    st.text(),
    st.none(),
    st.integers(),
)

# Valid rel-paths for round-trip tests. v0.11.21 added a path-traversal
# guard at the load boundary (manifest._validate_rel_path) that rejects
# `..` segments, absolute paths, drive letters, and null bytes. Round-trip
# fuzz strategies must therefore produce ONLY paths that pass that guard,
# or `test_load_manifest_round_trip_preserves_keys` and
# `test_v1_promotion_migrates_bare_tombstone_keys` would fuzz themselves
# into the rejection path. The wild-input invariant (normalize tolerates
# garbage) is still covered by `arbitrary_dict_strategy` below, and the
# load-side rejection is covered by
# `test_load_manifest_either_returns_normalized_or_raises`.
_PATH_SAFE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
valid_rel_path_strategy = st.lists(
    st.text(min_size=1, max_size=10, alphabet=_PATH_SAFE_ALPHABET).filter(
        lambda s: s != ".." and s != "."
    ),
    min_size=1,
    max_size=4,
).map("/".join)

# Tombstone keys span "clearly bare path" and "already-normalized claude:/gstack:".
# All forms must produce paths that survive `load_manifest`'s validator.
tombstone_key_strategy = st.one_of(
    valid_rel_path_strategy,  # bare paths -> v1→v2 migration prepends "claude:"
    st.tuples(
        st.sampled_from(["claude", "gstack", "other"]),
        valid_rel_path_strategy,
    ).map(lambda t: f"{t[0]}:{t[1]}"),  # already-normalized
)

source_files_strategy = st.dictionaries(
    valid_rel_path_strategy,
    st.fixed_dictionaries(
        {
            "sha256": st.text(min_size=64, max_size=64, alphabet="0123456789abcdef"),
            "size": st.integers(min_value=0, max_value=10**9),
            "mtime": st.text(min_size=0, max_size=40),
        }
    ),
    max_size=6,
)

v2_source_strategy = st.fixed_dictionaries(
    {
        "base_path": st.text(min_size=0, max_size=80),
        "files": source_files_strategy,
    }
)

v2_manifest_strategy = st.fixed_dictionaries(
    {
        "device_id": st.text(min_size=1, max_size=40),
        "device_name": st.text(min_size=0, max_size=40),
        "timestamp": st.text(min_size=0, max_size=40),
        # Track 1B: v2 writers no longer emit a redundant top-level "files".
        # `normalize_manifest` strips it on passthrough; don't generate it here
        # so fuzz shapes match what the current writers emit.
        "sources": st.dictionaries(
            st.sampled_from(["claude", "gstack"]), v2_source_strategy, max_size=2
        ),
        # Round-trip tests use VALID tombstone values; load_manifest rejects garbage.
        "tombstones": st.dictionaries(
            tombstone_key_strategy, valid_tombstone_value_strategy, max_size=6
        ),
    }
)

v1_manifest_strategy = st.fixed_dictionaries(
    {
        "device_id": st.text(min_size=1, max_size=40),
        "device_name": st.text(min_size=0, max_size=40),
        "timestamp": st.text(min_size=0, max_size=40),
        "base_path": st.text(min_size=0, max_size=80),
        "files": source_files_strategy,
        # Bare-path tombstones that the v1→v2 promotion is supposed to migrate.
        # Values must be dicts so load_manifest's hardened shape check accepts
        # them. Keys must pass the v0.11.21 rel-path validator (no `..`, no
        # absolute, no null bytes) — bare paths are a subset of valid rel-paths
        # so we reuse the same strategy.
        "tombstones": st.dictionaries(
            valid_rel_path_strategy,
            valid_tombstone_value_strategy,
            max_size=6,
        ),
    }
)

arbitrary_dict_strategy = st.dictionaries(
    st.text(min_size=0, max_size=20),
    st.recursive(
        st.one_of(st.none(), st.booleans(), st.integers(), st.text(), st.floats(allow_nan=False)),
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(min_size=0, max_size=10), children, max_size=4),
        ),
        max_leaves=10,
    ),
    max_size=6,
)


# Tests ---------------------------------------------------------------------


@given(arbitrary_dict_strategy)
@settings(max_examples=200)
def test_normalize_never_crashes_on_arbitrary_dict(d: dict) -> None:
    out = normalize_manifest(dict(d))
    # Post-condition: sources and tombstones are always present.
    assert "sources" in out
    assert "tombstones" in out


@given(arbitrary_dict_strategy)
@settings(max_examples=200)
def test_normalize_is_idempotent(d: dict) -> None:
    once = normalize_manifest(dict(d))
    once_copy = json.loads(json.dumps(once, default=str))
    twice = normalize_manifest(once_copy)
    # Compare via JSON round-trip to dodge in-place mutation aliasing.
    assert json.dumps(once, sort_keys=True, default=str) == json.dumps(
        twice, sort_keys=True, default=str
    )


@given(v2_manifest_strategy)
@settings(max_examples=100)
def test_load_manifest_round_trip_preserves_keys(m: dict) -> None:
    blob = serialize_manifest(m)
    loaded = load_manifest(blob)
    assert isinstance(loaded.get("sources"), dict)
    assert isinstance(loaded.get("tombstones"), dict)
    assert loaded["device_id"] == m["device_id"]


@given(v1_manifest_strategy)
@settings(max_examples=100)
def test_v1_promotion_migrates_bare_tombstone_keys(m: dict) -> None:
    blob = serialize_manifest(m)
    loaded = load_manifest(blob)
    # Every key originating as bare (no ":") in v1 should now be claude:-prefixed.
    for key in loaded["tombstones"].keys():
        assert isinstance(key, str)
        assert key.startswith("claude:")


@given(st.text(min_size=0, max_size=120))
@settings(max_examples=200)
def test_is_conflict_filename_never_crashes(name: str) -> None:
    # Property: returns a bool, doesn't raise.
    result = is_conflict_filename(name)
    assert isinstance(result, bool)


@given(st.text(min_size=0, max_size=120))
@settings(max_examples=200)
def test_is_conflict_filename_strict_pattern(name: str) -> None:
    # Property: a True result implies the strict format is present.
    if is_conflict_filename(name):
        assert ".sync-conflict-" in name
        idx = name.find(".sync-conflict-") + len(".sync-conflict-")
        # First char after the infix must be a digit (timestamp anchor).
        assert idx < len(name) and name[idx].isdigit()


@given(st.binary(min_size=0, max_size=200))
@settings(max_examples=100)
def test_load_manifest_either_returns_normalized_or_raises(data: bytes) -> None:
    from mind_meld.errors import ManifestError

    try:
        out = load_manifest(data)
    except ManifestError:
        return  # acceptable: bad bytes
    assert isinstance(out, dict)
    assert isinstance(out.get("sources"), dict)
    assert isinstance(out.get("tombstones"), dict)


def test_deserialize_manifest_stays_pure() -> None:
    # Sanity: deserialize_manifest does NOT normalize. Preserves the contract
    # that test fixtures and round-trip tests can inspect raw JSON shape.
    raw = b'{"foo": "bar"}'
    parsed = deserialize_manifest(raw)
    assert parsed == {"foo": "bar"}
    assert "sources" not in parsed
    assert "tombstones" not in parsed
