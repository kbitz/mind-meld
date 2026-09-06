"""Tests for merge logic — JSONL, MEMORY.md, and LCS-based 3-way merge."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mind_meld.merge import lcs_merge, merge_file, merge_jsonl, merge_lines, should_merge


def _jsonl(*lines: str) -> bytes:
    """Join already-normalized JSONL lines into trailing-newline bytes."""
    return ("\n".join(lines) + "\n").encode("utf-8")


class TestShouldMerge:
    def test_jsonl_extension(self):
        assert should_merge("projects/foo/learnings.jsonl") is True

    def test_json_extension(self):
        assert should_merge("projects/foo/data.json") is False

    def test_regular_md_extension(self):
        """Regular .md files should NOT be merged (frontmatter would garble)."""
        assert should_merge("projects/foo/plan.md") is False

    def test_yaml_extension(self):
        assert should_merge("config.yaml") is False

    def test_memory_md_is_merged(self):
        """MEMORY.md index files should be merged (line-oriented)."""
        assert should_merge("projects/-Users-kb-myapp/memory/MEMORY.md") is True

    def test_memory_md_at_root(self):
        assert should_merge("MEMORY.md") is True

    def test_other_md_not_merged(self):
        """Individual memory .md files should NOT be merged."""
        assert should_merge("projects/foo/memory/user_role.md") is False


class TestMergeFile:
    def test_dispatches_jsonl(self):
        local = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        remote = b'{"ts":"2026-01-02T00:00:00Z","key":"b"}\n'
        result = merge_file("foo.jsonl", local, remote)
        assert b'"key":"a"' in result
        assert b'"key":"b"' in result

    def test_dispatches_memory_md(self):
        local = b"- [Foo](foo.md) -- hook\n"
        remote = b"- [Bar](bar.md) -- hook\n"
        result = merge_file("memory/MEMORY.md", local, remote)
        assert b"foo.md" in result
        assert b"bar.md" in result

    def test_dispatches_other_overwrite(self):
        """Non-mergeable files return remote bytes (overwrite)."""
        result = merge_file("notes.txt", b"local", b"remote")
        assert result == b"remote"


class TestMergeJsonl:
    def test_empty_both(self):
        assert merge_jsonl(b"", b"") == b""

    def test_empty_local_nonempty_remote(self):
        remote = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        result = merge_jsonl(b"", remote)
        assert b'"key":"a"' in result

    def test_nonempty_local_empty_remote(self):
        local = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        result = merge_jsonl(local, b"")
        assert b'"key":"a"' in result

    def test_disjoint_lines(self):
        local = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        remote = b'{"ts":"2026-01-02T00:00:00Z","key":"b"}\n'
        result = merge_jsonl(local, remote)
        lines = result.decode().strip().splitlines()
        assert len(lines) == 2

    def test_overlapping_lines_deduplicated(self):
        line = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        result = merge_jsonl(line, line)
        lines = result.decode().strip().splitlines()
        assert len(lines) == 1

    def test_sort_by_ts(self):
        local = b'{"ts":"2026-01-02T00:00:00Z","key":"b"}\n'
        remote = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        result = merge_jsonl(local, remote)
        lines = result.decode().strip().splitlines()
        assert '"key":"a"' in lines[0]
        assert '"key":"b"' in lines[1]

    def test_corrupt_line_kept_at_end(self):
        local = b'not json at all\n{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        remote = b'{"ts":"2026-01-02T00:00:00Z","key":"b"}\n'
        result = merge_jsonl(local, remote)
        lines = result.decode().strip().splitlines()
        assert len(lines) == 3
        assert lines[-1] == "not json at all"

    def test_idempotent(self):
        a = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        b_data = b'{"ts":"2026-01-02T00:00:00Z","key":"b"}\n'
        first_merge = merge_jsonl(a, b_data)
        second_merge = merge_jsonl(a, first_merge)
        assert first_merge == second_merge

    def test_trailing_whitespace_stripped(self):
        local = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}  \n'
        remote = b'{"ts":"2026-01-01T00:00:00Z","key":"a"}\n'
        result = merge_jsonl(local, remote)
        lines = result.decode().strip().splitlines()
        assert len(lines) == 1

    def test_tied_ts_ordered_by_full_line_content(self):
        """Lines sharing a `ts` value MUST sort by full line content as the
        tiebreaker, not retain set-iteration order. Otherwise the merge
        result varies across processes (set hash randomization), defeating
        the no-op suppression in `_apply_merge` and causing every `mm pull`
        to re-merge the file forever. See INVARIANT in merge.py.
        """
        ts = "2026-01-01T00:00:00Z"
        # Input order is gamma, beta, alpha. After tie-break on content,
        # output is alpha, beta, gamma.
        g = f'{{"ts":"{ts}","k":"gamma"}}\n'.encode()
        b = f'{{"ts":"{ts}","k":"beta"}}\n'.encode()
        a = f'{{"ts":"{ts}","k":"alpha"}}\n'.encode()
        result = merge_jsonl(g + b + a, b"")
        lines = result.decode().strip().splitlines()
        assert lines[0].endswith('"k":"alpha"}'), lines
        assert lines[1].endswith('"k":"beta"}'), lines
        assert lines[2].endswith('"k":"gamma"}'), lines

    def test_jsonl_tied_ts_deterministic_across_hash_seeds(self, tmp_path: Path):
        """Regression: pre-fix, three consecutive `mm pull`s reliably
        re-merged the same files because each pull's merge produced
        different bytes than the prior pull's local result.

        Root cause: `merge_jsonl` built its line set then sorted by `ts`
        only. Tied-`ts` lines retained set-iteration order, which is
        hash-randomized per Python process via `PYTHONHASHSEED`. Each
        `mm pull` invocation is a fresh process → fresh seed → different
        ordering → `merged != local_bytes` → no-op suppression in
        `_apply_merge` fails → "merged" fires forever.

        This test runs `merge_jsonl` in three subprocesses with three
        different hash seeds and asserts the bytes are identical. With
        the buggy `key=lambda x: x[0]`, this test fails at SOME seed
        triple (flaky on the bug, deterministic on the fix).
        """
        runner = tmp_path / "runner.py"
        runner.write_text(
            textwrap.dedent(
                """
                import sys
                from mind_meld.merge import merge_jsonl

                ts = "2026-01-01T00:00:00Z"
                # Eight tied-ts lines — enough that random set-iteration
                # order is overwhelmingly unlikely to match content order
                # by chance under any single hash seed.
                lines = b"".join(
                    f'{{"ts":"{ts}","k":"v{i}"}}\\n'.encode()
                    for i in range(8)
                )
                sys.stdout.buffer.write(merge_jsonl(lines, b""))
                """
            ).strip()
        )

        def run(seed: str) -> bytes:
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = seed
            return subprocess.run(
                [sys.executable, str(runner)],
                env=env,
                capture_output=True,
                check=True,
            ).stdout

        out1 = run("1")
        out2 = run("42")
        out3 = run("999999")
        assert out1 == out2 == out3, (
            f"merge_jsonl output differs across hash seeds:\n"
            f"  seed=1:      {out1!r}\n"
            f"  seed=42:     {out2!r}\n"
            f"  seed=999999: {out3!r}"
        )


# Unique normalized lines used by the Track 51A type-matrix / combined
# fixture. Expected order is a literal, not derived from _extract_ts.
_TS_EMPTY = '{"ts":"","k":"empty"}'
_TS_NUMLOOK = '{"ts":"10","k":"numeric-looking"}'
_TS_OFFSET = '{"ts":"2026-01-01T00:00:00+00:00","k":"offset"}'
_TS_ISO_A = '{"ts":"2026-01-01T00:00:00Z","k":"a"}'
_TS_ISO_Z = '{"ts":"2026-01-01T00:00:00Z","k":"z"}'
_TS_ISO_B = '{"ts":"2026-01-02T00:00:00Z","k":"b"}'
_TS_ESCAPED = '{"ts":"caf\\u00e9","k":"escaped"}'
_TS_UNICODE = '{"ts":"café","k":"unicode"}'
_TS_TEXT = '{"ts":"hello","k":"text"}'
_FB_TOP_STR = '"just a string"'
_FB_TOP_NUM = "42"
_FB_TOP_ARR = "[1,2]"
_FB_MALFORMED = "not json at all"
_FB_TOP_NULL = "null"
_FB_TOP_BOOL = "true"
_FB_MISSING = '{"k":"missing-ts"}'
_FB_NEG = '{"ts":-1,"k":"neg"}'
_FB_NINF = '{"ts":-Infinity,"k":"ninf"}'
_FB_ZERO = '{"ts":0,"k":"zero"}'
_FB_FLOAT = '{"ts":1.5,"k":"float"}'
_FB_TEN = '{"ts":10,"k":"ten"}'
_FB_OVERFLOW = '{"ts":1e999,"k":"overflow"}'
_FB_TWO = '{"ts":2,"k":"two"}'
_FB_INF = '{"ts":Infinity,"k":"inf"}'
_FB_NAN = '{"ts":NaN,"k":"nan"}'
_FB_ARR_NESTED = '{"ts":[1,"x",true],"k":"arr-nested"}'
_FB_ARR_EMPTY = '{"ts":[],"k":"arr-empty"}'
_FB_FALSE = '{"ts":false,"k":"false"}'
_FB_NULL = '{"ts":null,"k":"null"}'
_FB_TRUE = '{"ts":true,"k":"true"}'
_FB_OBJ_A = '{"ts":{"a":1},"k":"obj-pop"}'
_FB_OBJ_B = '{"ts":{"b":2},"k":"obj-pop2"}'
_FB_OBJ_EMPTY = '{"ts":{},"k":"obj-empty"}'

# String-ts bucket, then full-line fallback. Hand-written order:
# empty / numeric-looking / offset-before-Z / tied ISO by whole line /
# later ISO / escaped-Unicode original before UTF-8 café / arbitrary text;
# then top-level JSON, malformed, missing ts, signed/nonfinite/finite
# numbers (10 before 2), heterogeneous arrays, bools, null, objects.
_COMBINED_EXPECTED = _jsonl(
    _TS_EMPTY,
    _TS_NUMLOOK,
    _TS_OFFSET,
    _TS_ISO_A,
    _TS_ISO_Z,
    _TS_ISO_B,
    _TS_ESCAPED,
    _TS_UNICODE,
    _TS_TEXT,
    _FB_TOP_STR,
    _FB_TOP_NUM,
    _FB_TOP_ARR,
    _FB_MALFORMED,
    _FB_TOP_NULL,
    _FB_TOP_BOOL,
    _FB_MISSING,
    _FB_NEG,
    _FB_NINF,
    _FB_ZERO,
    _FB_FLOAT,
    _FB_TEN,
    _FB_OVERFLOW,
    _FB_TWO,
    _FB_INF,
    _FB_NAN,
    _FB_ARR_NESTED,
    _FB_ARR_EMPTY,
    _FB_FALSE,
    _FB_NULL,
    _FB_TRUE,
    _FB_OBJ_A,
    _FB_OBJ_B,
    _FB_OBJ_EMPTY,
)

_COMBINED_LOCAL = _jsonl(
    _TS_ISO_B,
    _FB_TWO,
    _FB_NULL,
    _FB_MALFORMED,
    _TS_EMPTY,
    _FB_OBJ_A,
    _FB_NAN,
    _FB_TOP_STR,
    _TS_UNICODE,
)
_COMBINED_REMOTE = _jsonl(
    _TS_ISO_A,
    _TS_ISO_Z,
    _TS_NUMLOOK,
    _TS_OFFSET,
    _TS_ESCAPED,
    _TS_TEXT,
    _FB_TOP_NUM,
    _FB_TOP_ARR,
    _FB_TOP_NULL,
    _FB_TOP_BOOL,
    _FB_MISSING,
    _FB_NEG,
    _FB_NINF,
    _FB_ZERO,
    _FB_FLOAT,
    _FB_TEN,
    _FB_OVERFLOW,
    _FB_INF,
    _FB_ARR_NESTED,
    _FB_ARR_EMPTY,
    _FB_FALSE,
    _FB_TRUE,
    _FB_OBJ_B,
    _FB_OBJ_EMPTY,
    _FB_TWO,
)


class TestMergeJsonlMixedTimestamps:
    """Track 51A: non-string ts values must not abort or reorder non-deterministically."""

    def test_combined_fixture_exact_bytes(self):
        result = merge_jsonl(_COMBINED_LOCAL, _COMBINED_REMOTE)
        assert result == _COMBINED_EXPECTED
        decoded = result.decode("utf-8")
        str_bucket_end = decoded.index(_FB_TOP_STR)
        assert decoded.index(_TS_TEXT) < str_bucket_end
        assert decoded.index(_TS_ISO_A) < decoded.index(_TS_ISO_Z)
        assert decoded.index(_TS_ISO_Z) < decoded.index(_TS_ISO_B)

    def test_numeric_only_two_and_ten_are_lexical(self):
        """Identical surrounding shape; 10 before 2 as whole-line text, not numeric."""
        ten = '{"ts":10,"k":"same"}'
        two = '{"ts":2,"k":"same"}'
        expected = _jsonl(ten, two)
        assert merge_jsonl(_jsonl(two), _jsonl(ten)) == expected
        assert merge_jsonl(_jsonl(ten), _jsonl(two)) == expected

    @pytest.mark.parametrize(
        "fallback_line",
        [
            '{"ts":0,"k":"zero"}',
            '{"ts":-1,"k":"neg"}',
            '{"ts":2,"k":"two"}',
            '{"ts":10,"k":"ten"}',
            '{"ts":1.5,"k":"float"}',
            '{"ts":true,"k":"true"}',
            '{"ts":false,"k":"false"}',
            '{"ts":null,"k":"null"}',
            '{"ts":[],"k":"arr-empty"}',
            '{"ts":[1,"x",true],"k":"arr-nested"}',
            '{"ts":{},"k":"obj-empty"}',
            '{"ts":{"a":1},"k":"obj-pop"}',
            '{"ts":{"b":2},"k":"obj-pop2"}',
            '{"k":"missing-ts"}',
            "[1,2]",
            '"just a string"',
            "42",
            "true",
            "null",
            "not json at all",
            '{"ts":NaN,"k":"nan"}',
            '{"ts":Infinity,"k":"inf"}',
            '{"ts":-Infinity,"k":"ninf"}',
            '{"ts":1e999,"k":"overflow"}',
        ],
    )
    def test_each_non_string_ts_is_preserved_after_string_bucket(self, fallback_line):
        string_line = '{"ts":"2026-01-01T00:00:00Z","k":"s"}'
        result = merge_jsonl(_jsonl(fallback_line), _jsonl(string_line))
        assert result == _jsonl(string_line, fallback_line)

    @pytest.mark.parametrize(
        "string_line",
        [
            '{"ts":"","k":"empty"}',
            '{"ts":"10","k":"numeric-looking"}',
            '{"ts":"hello","k":"text"}',
            '{"ts":"2026-01-01T00:00:00Z","k":"iso"}',
            '{"ts":"2026-01-01T00:00:00+00:00","k":"offset"}',
            '{"ts":"caf\\u00e9","k":"escaped"}',
        ],
    )
    def test_string_ts_variants_stay_in_string_bucket(self, string_line):
        fallback = '{"ts":2,"k":"two"}'
        result = merge_jsonl(_jsonl(fallback), _jsonl(string_line))
        assert result == _jsonl(string_line, fallback)

    def test_blank_lines_are_dropped_and_originals_kept(self):
        local = b'{"ts":"2026-01-01T00:00:00Z","k":"a"}\n\n  \n'
        remote = b'{"ts":2,"k":"two"}\n'
        assert merge_jsonl(local, remote) == _jsonl(
            '{"ts":"2026-01-01T00:00:00Z","k":"a"}',
            '{"ts":2,"k":"two"}',
        )

    def test_finite_plus_nan_lexical_fallback(self):
        """NaN is not a total order; original lines sort as whole-line text."""
        zero = '{"ts":0,"id":"zero"}'
        one = '{"ts":1,"id":"one"}'
        two = '{"ts":2,"id":"two"}'
        nan = '{"ts":NaN,"id":"nan"}'
        expected = _jsonl(zero, one, two, nan)
        shuffled = _jsonl(two, nan, zero, one)
        assert merge_jsonl(shuffled, b"") == expected
        assert merge_jsonl(_jsonl(zero, one), _jsonl(two, nan)) == expected

    def test_invalid_utf8_bytes_are_replaced_not_raised(self):
        """Invalid UTF-8 decodes to U+FFFD; the record still keys on its string ts."""
        local = b'{"ts":"2026-01-01T00:00:00Z","k":"\xff\xfe"}\n'
        remote = b'{"ts":2,"k":"two"}\n'
        result = merge_jsonl(local, remote)
        assert result == _jsonl(
            '{"ts":"2026-01-01T00:00:00Z","k":"��"}',
            '{"ts":2,"k":"two"}',
        )
        assert b"\xff" not in result
        assert b"\xfe" not in result

    def test_deeply_nested_ts_keeps_original_line(self):
        nested = '{"ts":' + ("[" * 1500) + ("]" * 1500) + "}"
        string_line = '{"ts":"2026-01-01T00:00:00Z","k":"a"}'
        result = merge_jsonl(_jsonl(nested), _jsonl(string_line))
        assert result == _jsonl(string_line, nested)

    def test_injected_recursionerror_falls_back(self, monkeypatch):
        """Portable pin: decoder depth need not match across Python versions."""
        real_loads = json.loads

        def boom(s, *a, **kw):
            if "AAA" in s:
                raise RecursionError("decoder depth")
            return real_loads(s, *a, **kw)

        monkeypatch.setattr("mind_meld.merge.json.loads", boom)
        aaa = '{"ts":"AAA","k":"aaa"}'
        zzz = '{"ts":"ZZZ","k":"zzz"}'
        # If both parsed as strings, AAA would precede ZZZ. RecursionError
        # on AAA puts it in fallback, so the string-ts ZZZ comes first.
        assert merge_jsonl(_jsonl(aaa), _jsonl(zzz)) == _jsonl(zzz, aaa)

    def test_injected_valueerror_falls_back(self, monkeypatch):
        real_loads = json.loads

        def boom(s, *a, **kw):
            if "BIGINT" in s:
                raise ValueError("int too large")
            return real_loads(s, *a, **kw)

        monkeypatch.setattr("mind_meld.merge.json.loads", boom)
        big = '{"ts":"BIGINT","k":"big"}'
        ok = '{"ts":"2026-01-01T00:00:00Z","k":"ok"}'
        assert merge_jsonl(_jsonl(big), _jsonl(ok)) == _jsonl(ok, big)

    def test_union_idempotent_commutative_associative(self):
        a = _jsonl(_TS_ISO_A, _FB_TWO)
        b = _jsonl(_TS_ISO_B, _FB_TRUE, _TS_ISO_A)
        c = _jsonl(_FB_MALFORMED, _FB_NULL)
        expected_ab = _jsonl(_TS_ISO_A, _TS_ISO_B, _FB_TWO, _FB_TRUE)
        expected_abc = _jsonl(
            _TS_ISO_A,
            _TS_ISO_B,
            _FB_MALFORMED,
            _FB_TWO,
            _FB_NULL,
            _FB_TRUE,
        )
        merged_ab = merge_jsonl(a, b)
        assert merged_ab == expected_ab
        assert merge_jsonl(merged_ab, b) == expected_ab
        assert merge_jsonl(b, a) == expected_ab
        assert merge_jsonl(merge_jsonl(a, b), c) == expected_abc
        assert merge_jsonl(a, merge_jsonl(b, c)) == expected_abc
        assert set(merged_ab.decode().splitlines()) == {
            _TS_ISO_A,
            _TS_ISO_B,
            _FB_TWO,
            _FB_TRUE,
        }

    def _run_merge_subprocess(self, tmp_path: Path, seed: str, source: str) -> bytes:
        runner = tmp_path / f"runner_{seed}.py"
        runner.write_text(source)
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        return subprocess.run(
            [sys.executable, str(runner)],
            env=env,
            capture_output=True,
            check=True,
        ).stdout

    def test_mixed_types_deterministic_across_hash_seeds(self, tmp_path: Path):
        source = textwrap.dedent(
            """
            import sys
            from mind_meld.merge import merge_jsonl
            local = (
                b'{"ts":"2026-01-02T00:00:00Z","k":"b"}\\n'
                b'{"ts":2,"k":"two"}\\n'
                b'{"ts":NaN,"k":"nan"}\\n'
                b'not json at all\\n'
            )
            remote = (
                b'{"ts":"2026-01-01T00:00:00Z","k":"a"}\\n'
                b'{"ts":10,"k":"ten"}\\n'
                b'{"ts":true,"k":"true"}\\n'
            )
            sys.stdout.buffer.write(merge_jsonl(local, remote))
            """
        ).strip()
        expected = _jsonl(
            '{"ts":"2026-01-01T00:00:00Z","k":"a"}',
            '{"ts":"2026-01-02T00:00:00Z","k":"b"}',
            "not json at all",
            '{"ts":10,"k":"ten"}',
            '{"ts":2,"k":"two"}',
            '{"ts":NaN,"k":"nan"}',
            '{"ts":true,"k":"true"}',
        )
        outputs = [
            self._run_merge_subprocess(tmp_path, seed, source)
            for seed in ("1", "2", "42", "999999")
        ]
        for out in outputs:
            assert out == expected
        assert len(set(outputs)) == 1

    def test_finite_plus_nan_deterministic_across_hash_seeds(self, tmp_path: Path):
        source = textwrap.dedent(
            """
            import sys
            from mind_meld.merge import merge_jsonl
            payload = (
                b'{"ts":2,"id":"two"}\\n'
                b'{"ts":NaN,"id":"nan"}\\n'
                b'{"ts":0,"id":"zero"}\\n'
                b'{"ts":1,"id":"one"}\\n'
            )
            sys.stdout.buffer.write(merge_jsonl(payload, b""))
            """
        ).strip()
        expected = _jsonl(
            '{"ts":0,"id":"zero"}',
            '{"ts":1,"id":"one"}',
            '{"ts":2,"id":"two"}',
            '{"ts":NaN,"id":"nan"}',
        )
        outputs = [
            self._run_merge_subprocess(tmp_path, seed, source)
            for seed in ("1", "2", "42", "999999")
        ]
        for out in outputs:
            assert out == expected
        assert len(set(outputs)) == 1


class TestMergeLines:
    def test_union_of_lines(self):
        local = b"- [Foo](foo.md) -- hook\n- [Bar](bar.md) -- hook\n"
        remote = b"- [Bar](bar.md) -- hook\n- [Baz](baz.md) -- hook\n"
        result = merge_lines(local, remote)
        lines = result.decode().strip().splitlines()
        assert len(lines) == 3

    def test_empty_local_nonempty_remote(self):
        remote = b"- [Foo](foo.md) -- hook\n"
        result = merge_lines(b"", remote)
        assert b"foo.md" in result

    def test_empty_both(self):
        assert merge_lines(b"", b"") == b""

    def test_non_utf8_graceful(self):
        """Non-UTF-8 bytes should be handled with replacement chars."""
        local = b"- [Good](good.md)\n"
        remote = b"- [Bad](\xff\xfe.md)\n"
        result = merge_lines(local, remote)
        assert b"good.md" in result
        # Should not raise, replacement chars used

    def test_memory_md_does_not_consult_ts(self):
        """MEMORY.md is a plain lexical line-union; a ts field is not a sort key."""
        later_ts = '{"k":"a","ts":"2026-01-02T00:00:00Z"}'
        earlier_ts = '{"k":"z","ts":"2026-01-01T00:00:00Z"}'
        local = (earlier_ts + "\n").encode()
        remote = (later_ts + "\n").encode()
        # Whole-line order puts "k":"a" first even though its ts is later.
        lexical = (later_ts + "\n" + earlier_ts + "\n").encode()
        assert merge_lines(local, remote) == lexical
        assert merge_file("memory/MEMORY.md", local, remote) == lexical
        # The same lines through the JSONL strategy sort by ts instead.
        by_ts = (earlier_ts + "\n" + later_ts + "\n").encode()
        assert merge_file("memory/log.jsonl", local, remote) == by_ts

    def test_idempotent(self):
        a = b"- [Foo](foo.md) -- hook\n"
        b_data = b"- [Bar](bar.md) -- hook\n"
        first_merge = merge_lines(a, b_data)
        second_merge = merge_lines(a, first_merge)
        assert first_merge == second_merge

    def test_dedup_exact(self):
        """Identical lines from both sides should appear once."""
        line = b"- [Same](same.md) -- hook\n"
        result = merge_lines(line, line)
        lines = result.decode().strip().splitlines()
        assert len(lines) == 1

    def test_sorted_output(self):
        """Output lines should be sorted for deterministic results."""
        local = b"- [Zebra](z.md)\n- [Apple](a.md)\n"
        result = merge_lines(local, b"")
        lines = result.decode().strip().splitlines()
        assert "Apple" in lines[0]


class TestLcsMerge:
    """LCS-as-synthetic-base 3-way merge for the (m)erge prompt option."""

    def test_strict_superset_remote_extends_local(self):
        """The on-disk example case: local is a prefix of remote.

        Remote = local + one appended bullet. Clean merge produces
        remote bytes; conflict_count == 0.
        """
        local = b"line1\nline2\nline3\n"
        remote = b"line1\nline2\nline3\nline4\n"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == 0
        assert merged == remote

    def test_strict_superset_local_extends_remote(self):
        """Symmetric: local has the extra line. Lossless additive keeps it."""
        local = b"line1\nline2\nline3\nline4\n"
        remote = b"line1\nline2\nline3\n"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == 0
        assert b"line4" in merged

    def test_interleaved_additions_no_overlap(self):
        """Peer added at top, local added at bottom -- both kept, no conflict."""
        local = b"shared1\nshared2\nlocal-extra\n"
        remote = b"remote-extra\nshared1\nshared2\n"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == 0
        assert b"local-extra" in merged
        assert b"remote-extra" in merged

    def test_both_edited_same_line_produces_markers(self):
        """Same region edited differently on each side -> conflict markers."""
        local = b"shared1\nLOCAL_VERSION\nshared2\n"
        remote = b"shared1\nREMOTE_VERSION\nshared2\n"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == 1
        assert b"<<<<<<< local" in merged
        assert b"=======" in merged
        assert b">>>>>>> remote" in merged
        assert b"LOCAL_VERSION" in merged
        assert b"REMOTE_VERSION" in merged

    def test_empty_local_takes_remote(self):
        merged, conflicts = lcs_merge(b"", b"line1\nline2\n")
        assert conflicts == 0
        assert merged == b"line1\nline2\n"

    def test_empty_remote_takes_local(self):
        merged, conflicts = lcs_merge(b"line1\nline2\n", b"")
        assert conflicts == 0
        assert merged == b"line1\nline2\n"

    def test_both_empty_returns_empty(self):
        merged, conflicts = lcs_merge(b"", b"")
        assert conflicts == 0
        assert merged == b""

    def test_identical_files_returns_unchanged(self):
        """Won't fire in practice (caller short-circuits on hash match) but
        the primitive should handle it cleanly anyway."""
        data = b"line1\nline2\nline3\n"
        merged, conflicts = lcs_merge(data, data)
        assert conflicts == 0
        assert merged == data

    def test_binary_input_returns_sentinel(self):
        """NUL byte in either side returns conflict_count=-1 to signal
        "merge not attempted" so callers suppress the (m) option."""
        local = b"some text\nmore text\n"
        remote = b"binary \x00 content\nmore\n"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == -1
        assert merged == b""

    def test_binary_input_local_side(self):
        local = b"\x00\x01\x02"
        remote = b"text content\n"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == -1

    def test_trailing_newline_preserved_when_present_local(self):
        local = b"line1\nline2\n"
        remote = b"line1\nline2\nline3\n"
        merged, _ = lcs_merge(local, remote)
        assert merged.endswith(b"\n")

    def test_no_trailing_newline_preserved_when_neither(self):
        """If neither input had a trailing newline, the merge shouldn't add one."""
        local = b"line1\nline2"
        remote = b"line1\nline2"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == 0
        assert not merged.endswith(b"\n")

    def test_trailing_newline_added_when_either_has_it(self):
        local = b"line1\nline2\n"
        remote = b"line1\nline2"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == 0
        assert merged.endswith(b"\n")

    def test_non_utf8_returns_binary_sentinel(self):
        """Invalid utf-8 (NUL-free, fails strict decode) returns conflict_count=-1
        so the (m) option is suppressed. Earlier the function decoded with
        errors='replace' and produced a lossy merge that could silently
        corrupt canonical on (m) accept (security: pre-ship reviewer flagged
        as silent-data-loss path)."""
        local = b"line1\n\xff\xfe\nline3\n"  # 0xff 0xfe is invalid utf-8
        remote = b"line1\nline2\nline3\n"
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == -1
        assert merged == b""

    def test_nul_free_non_text_returns_binary_sentinel(self):
        """A NUL-free byte sequence that fails strict utf-8 decode (e.g.,
        binary that lacks NUL) returns -1 instead of being lossy-decoded
        into a 'merge'. Closes the gap the NUL-only fast-path leaves."""
        local = b"plain text on one line\n"
        remote = b"plain text\n\xc3\x28 broken\n"  # 0xc3 0x28 is invalid utf-8
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == -1
        assert merged == b""

    def test_two_sidecars_for_same_canonical_regression(self):
        """The on-disk example: two sidecars (from different push timestamps)
        both contain the same additional bullet vs canonical. Walking
        them sequentially: first merge produces canonical = sidecar bytes;
        second merge sees canonical == sidecar2 bytes, returns no-op."""
        canonical = b"a\nb\nc\n"
        sidecar1 = b"a\nb\nc\nd\n"
        sidecar2 = b"a\nb\nc\nd\n"  # identical to sidecar1

        merged1, c1 = lcs_merge(canonical, sidecar1)
        assert c1 == 0
        assert merged1 == sidecar1

        # After first walk: canonical = merged1. Second walk:
        merged2, c2 = lcs_merge(merged1, sidecar2)
        assert c2 == 0
        assert merged2 == merged1  # idempotent on the second pass

    def test_real_on_disk_conflict_shape(self):
        """Pinned regression: the actual file shape from the kbitz fleet
        on 2026-04-30 -- a memory entry with one appended bullet."""
        local = (
            b"---\nname: foo\n---\n- existing bullet 1\n- existing bullet 2\n- existing bullet 3\n"
        )
        remote = (
            b"---\nname: foo\n---\n"
            b"- existing bullet 1\n"
            b"- existing bullet 2\n"
            b"- existing bullet 3\n"
            b"- new appended bullet from peer\n"
        )
        merged, conflicts = lcs_merge(local, remote)
        assert conflicts == 0
        assert merged == remote

    def test_marker_label_first_arg_is_local(self):
        """Pin the marker convention: FIRST arg's bytes appear under
        '<<<<<<< local', SECOND under '>>>>>>> remote'. Load-bearing for
        the inversion-aware arg swap at cli.py:5926 -- if a refactor
        flips the order, markers would say 'local' but bytes are remote.
        Pinned per pre-ship reviewer INFO #4."""
        a = b"shared\nA_VARIANT\nshared\n"
        b = b"shared\nB_VARIANT\nshared\n"
        merged_ab, c_ab = lcs_merge(a, b)
        assert c_ab == 1
        text = merged_ab.decode("utf-8")
        local_pos = text.index("<<<<<<< local")
        sep_pos = text.index("=======")
        remote_pos = text.index(">>>>>>> remote")
        a_pos = text.index("A_VARIANT")
        b_pos = text.index("B_VARIANT")
        assert local_pos < a_pos < sep_pos
        assert sep_pos < b_pos < remote_pos

        # Symmetric: swapping args swaps the label-to-bytes mapping.
        merged_ba, _ = lcs_merge(b, a)
        text2 = merged_ba.decode("utf-8")
        sep2 = text2.index("=======")
        assert text2.index("B_VARIANT") < sep2 < text2.index("A_VARIANT")
