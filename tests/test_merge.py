"""Tests for JSONL merge logic."""

from memsync.merge import merge_jsonl, should_merge


class TestShouldMerge:
    def test_jsonl_extension(self):
        assert should_merge("projects/foo/learnings.jsonl") is True

    def test_json_extension(self):
        assert should_merge("projects/foo/data.json") is False

    def test_md_extension(self):
        assert should_merge("projects/foo/plan.md") is False

    def test_yaml_extension(self):
        assert should_merge("config.yaml") is False


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
        # Corrupt line should be last (after timestamped lines)
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
