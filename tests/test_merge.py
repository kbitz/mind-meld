"""Tests for merge logic — JSONL and MEMORY.md."""

from mind_meld.merge import merge_file, merge_jsonl, merge_lines, should_merge


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
        assert "Zebra" in lines[1]
