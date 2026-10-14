"""Pure core algebra and independent table examples."""

import ast
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

from .model import core

DEVICES = ("aaaaaaaa", "bbbbbbbb", "cccccccc")
VECTORS = st.dictionaries(st.sampled_from(DEVICES), st.integers(1, 30))


@given(VECTORS, VECTORS, VECTORS)
def test_vector_order_laws(a, b, c):
    inverse = {"equal": "equal", "before": "after", "after": "before", "concurrent": "concurrent"}
    assert core.vector_order(a, a) == "equal"
    assert core.vector_order(b, a) == inverse[core.vector_order(a, b)]
    assert core.vector_max(a, b) == core.vector_max(b, a)
    assert core.vector_max(a, core.vector_max(b, c)) == core.vector_max(core.vector_max(a, b), c)
    if core.vector_order(a, b) in ("before", "equal") and core.vector_order(b, c) in (
        "before",
        "equal",
    ):
        assert core.vector_order(a, c) in ("before", "equal")


@pytest.mark.parametrize(
    "left,right,same,expected",
    [
        ({"aaaaaaaa": 1}, {"aaaaaaaa": 2}, False, "fast-forward"),
        ({"aaaaaaaa": 2}, {"aaaaaaaa": 1}, False, "ignore"),
        ({"aaaaaaaa": 1}, {"bbbbbbbb": 1}, False, "resolve"),
        ({"aaaaaaaa": 1}, {"aaaaaaaa": 1}, False, "resolve"),
        ({"aaaaaaaa": 1}, {"bbbbbbbb": 1}, True, "same-sha"),
    ],
)
def test_classification_table(left, right, same, expected):
    local = core.entry("claude", "notes.txt", b"a", left)
    incoming = core.entry("claude", "notes.txt", b"a" if same else b"b", right)
    assert core.classify(local, incoming) == expected


@given(VECTORS, VECTORS, st.booleans(), st.booleans())
def test_classification_exhaustive(a, b, deleted_a, deleted_b):
    a = a or {DEVICES[0]: 1}
    b = b or {DEVICES[1]: 1}
    local = core.entry("claude", "notes.txt", None if deleted_a else b"a", a)
    incoming = core.entry("claude", "notes.txt", None if deleted_b else b"b", b)
    assert core.classify(local, incoming) in {"fast-forward", "ignore", "resolve", "same-sha"}
    assert core.classify(None, incoming) == "fast-forward"
    if deleted_b:
        assert core.classify(local, incoming, bootstrap=True) == "bootstrap-delete"


@pytest.mark.parametrize("path", ["notes.txt", "MEMORY.md", "events.jsonl"])
@given(st.lists(st.sampled_from([b"alpha\n", b"bravo\n", b"charlie\n"]), min_size=3, max_size=3))
def test_resolver_algebra(path, contents):
    entries = [
        core.entry("claude", path, data, {device: 1}, 0)
        for data, device in zip(contents, DEVICES, strict=True)
    ]
    a, b, c = entries
    assert core.resolve(a, b) == core.resolve(b, a)
    assert core.resolve(a, core.resolve(b, c)) == core.resolve(core.resolve(a, b), c)
    assert core.resolve(a, a) == a


def test_authoring_and_reader_tables():
    local = core.entry("claude", "notes.txt", b"a", {DEVICES[0]: 1})
    assert core.author("claude", "notes.txt", local, b"a", 0, DEVICES[0]) is None
    pending = core.author("claude", "notes.txt", local, b"a", 0, DEVICES[0], pending=True)
    assert pending.metadata["vv"] == {DEVICES[0]: 2}
    assert core.author("claude", "notes.txt", local, None, 0, DEVICES[0]).metadata["deleted"]
    for complete in (False, True):
        for unreadable in (False, True):
            result = core.reader_rule(2, complete=complete, unreadable=unreadable, snapshot_seq=2)
            assert result == ("commit" if complete and not unreadable else "snapshot")
    assert core.reader_rule(3, complete=False, unreadable=False, snapshot_seq=2) == "pending"
    assert (
        core.reader_rule(3, complete=True, unreadable=True, snapshot_seq=2) == "stalled-unreadable"
    )


def test_core_import_gate():
    from pathlib import Path

    tree = ast.parse(Path(core.__file__).read_text())
    for node in ast.walk(tree):
        imports = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module]
            if isinstance(node, ast.ImportFrom)
            else []
        )
        for name in imports:
            assert name.split(".")[0] in sys.stdlib_module_names or name in (
                "mind_meld",
                "mind_meld.store.format",
            )
            if name == "mind_meld":
                assert [a.name for a in node.names] == ["merge"]


@given(VECTORS, VECTORS)
def test_classifier_matches_independent_order(a, b):
    a = a or {DEVICES[0]: 1}
    b = b or {DEVICES[1]: 1}
    keys = sorted(a.keys() | b.keys())
    before = all(a.get(k, 0) <= b.get(k, 0) for k in keys) and a != b
    after = all(a.get(k, 0) >= b.get(k, 0) for k in keys) and a != b
    expected = "fast-forward" if before else "ignore" if after else "resolve"
    assert (
        core.classify(
            core.entry("claude", "notes.txt", b"a", a), core.entry("claude", "notes.txt", b"b", b)
        )
        == expected
    )


def test_snapshot_trigger_boundaries():
    assert not core.snapshot_due(100, 199, 100)
    assert core.snapshot_due(101, 199, 100)
    assert core.snapshot_due(0, 200, 100)
    assert core.snapshot_due(0, 201, 100)


def test_ledger_sha_is_authority_without_cached_content():
    from dataclasses import replace

    ledger = core.entry("claude", "notes.txt", b"old", {DEVICES[0]: 1})
    metadata_only = replace(ledger, content=None)
    assert core.author("claude", "notes.txt", metadata_only, b"old", 0, DEVICES[0]) is None
    assert core.local_version(metadata_only, b"old", 0, DEVICES[0]).content == b"old"
    changed = core.author("claude", "notes.txt", metadata_only, b"new", 0, DEVICES[0])
    assert changed.metadata["vv"] == {DEVICES[0]: 2}


@pytest.mark.parametrize("path", ["notes.txt", "MEMORY.md", "events.jsonl"])
@given(
    st.lists(
        st.sampled_from([None, b"", b"alpha\n", b"alpha\nbravo\n", b"bravo\n"]),
        min_size=3,
        max_size=3,
    ),
    st.lists(st.integers(0, 5), min_size=3, max_size=3),
    VECTORS,
    VECTORS,
    VECTORS,
)
def test_resolver_algebra_with_tombstones_vectors_and_mtime(path, data, mtimes, av, bv, cv):
    # Union algebra's byte domain is canonical normalized input; same-sha
    # classification bypasses a resolver for noncanonical equal inputs.
    a, b, c = [
        core.entry("claude", path, content, vv or {device: 1}, mtime)
        for content, vv, device, mtime in zip(data, (av, bv, cv), DEVICES, mtimes, strict=True)
    ]
    assert core.resolve(a, b) == core.resolve(b, a)
    assert core.resolve(a, core.resolve(b, c)) == core.resolve(core.resolve(a, b), c)
    assert core.resolve(a, a) == a
