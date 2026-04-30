"""Tests for merge logic — JSONL, MEMORY.md, and LCS-based 3-way merge."""

from mind_meld.merge import lcs_merge, merge_file, merge_jsonl, merge_lines, should_merge


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
