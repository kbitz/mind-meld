"""Direct unit tests for synclog.write_sync_log.

Pins the behavior of the per-project sync log that Claude Code picks up
after a pull. Previously coverage was indirect via test_integration.py's
TestSyncLog (3 happy-path cases); this file adds edge cases and a
regression pin for the v0.8.7 `claude_dir` -> `claude_base` param rename.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mind_meld.synclog import write_sync_log


def _claude_tree(tmp_path: Path, *project_names: str) -> Path:
    """Create a ~/.claude-shaped tree with the given project subdirectories."""
    claude = tmp_path / ".claude"
    for name in project_names:
        (claude / "projects" / name).mkdir(parents=True, exist_ok=True)
    return claude


# ── path handling ────────────────────────────────────────────────────


class TestPathHandling:
    def test_projects_dir_missing_returns_empty(self, tmp_path: Path) -> None:
        """No projects/ subdirectory under claude_base → no log files written."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()  # exists but no projects/

        logs = write_sync_log(
            claude_base=str(claude_dir),
            device_name="A",
            device_id="abc123",
            new_files=["projects/foo/memory/x.md"],
            modified_files=[],
            deleted_files=[],
        )
        assert logs == []

    def test_tilde_path_expands(self, tmp_path: Path, monkeypatch) -> None:
        """claude_base=`~/.claude-test` expands via expanduser()."""
        monkeypatch.setenv("HOME", str(tmp_path))
        claude = tmp_path / ".claude-test"
        (claude / "projects" / "-foo").mkdir(parents=True)

        logs = write_sync_log(
            claude_base="~/.claude-test",
            device_name="A",
            device_id="abc123",
            new_files=["projects/-foo/memory/x.md"],
            modified_files=[],
            deleted_files=[],
        )
        assert len(logs) == 1
        assert logs[0].parent.name == "-foo"

    def test_path_object_accepted(self, tmp_path: Path) -> None:
        """claude_base accepts Path objects, not just strings."""
        claude = _claude_tree(tmp_path, "-foo")

        logs = write_sync_log(
            claude_base=claude,
            device_name="A",
            device_id="abc",
            new_files=["projects/-foo/memory/x.md"],
            modified_files=[],
            deleted_files=[],
        )
        assert len(logs) == 1


# ── per-project grouping ─────────────────────────────────────────────


class TestPerProjectGrouping:
    def test_writes_one_log_per_affected_project(self, tmp_path: Path) -> None:
        claude = _claude_tree(tmp_path, "-a", "-b", "-c")
        logs = write_sync_log(
            claude_base=str(claude),
            device_name="Mac",
            device_id="abc123",
            new_files=[
                "projects/-a/memory/alpha.md",
                "projects/-b/memory/beta.md",
            ],
            modified_files=[],
            deleted_files=[],
        )
        assert len(logs) == 2
        # -c had no changes, so no log there
        assert not (claude / "projects" / "-c" / ".mind-meld-log.md").exists()

    def test_skips_projects_without_local_dir(self, tmp_path: Path) -> None:
        """Changes for a project whose local dir doesn't exist → no crash, skipped."""
        claude = _claude_tree(tmp_path, "-a")  # only -a exists locally

        logs = write_sync_log(
            claude_base=str(claude),
            device_name="Mac",
            device_id="abc",
            new_files=[
                "projects/-a/memory/here.md",
                "projects/-b/memory/not-here.md",  # -b doesn't exist locally
            ],
            modified_files=[],
            deleted_files=[],
        )
        assert len(logs) == 1
        assert logs[0].parent.name == "-a"


# ── all five change categories ───────────────────────────────────────


class TestChangeCategories:
    def _log(self, tmp_path: Path, **kw) -> str:
        claude = _claude_tree(tmp_path, "-foo")
        logs = write_sync_log(
            claude_base=str(claude),
            device_name="Mac",
            device_id="abc",
            new_files=kw.get("new_files", []),
            modified_files=kw.get("modified_files", []),
            deleted_files=kw.get("deleted_files", []),
            conflicted_files=kw.get("conflicted_files"),
            skipped_files=kw.get("skipped_files"),
        )
        if not logs:
            return ""
        return logs[0].read_text()

    def test_new_section(self, tmp_path: Path) -> None:
        body = self._log(tmp_path, new_files=["projects/-foo/memory/x.md"])
        assert "New from other machine" in body
        assert "memory/x.md" in body

    def test_modified_section(self, tmp_path: Path) -> None:
        body = self._log(tmp_path, modified_files=["projects/-foo/memory/y.md"])
        assert "Updated from other machine" in body
        assert "memory/y.md" in body

    def test_deleted_section(self, tmp_path: Path) -> None:
        body = self._log(tmp_path, deleted_files=["projects/-foo/memory/z.md"])
        assert "Removed on other machine" in body
        assert "memory/z.md" in body

    def test_conflicted_section(self, tmp_path: Path) -> None:
        body = self._log(tmp_path, conflicted_files=["projects/-foo/memory/c.md"])
        assert "Conflicts" in body
        assert "memory/c.md" in body
        # Surfaces the mm commands for resolution
        assert "mm conflicts" in body
        assert "mm resolve" in body

    def test_conflicted_header_states_the_post_inversion_direction(self, tmp_path: Path) -> None:
        """The header must say REMOTE went to the sidecar, not local.

        Regression pin for v0.12.51. From v0.9.2 (the inversion) until then the
        header read "local preserved as .sync-conflict-*", which describes the
        PRE-inversion direction and told the reader their own edits had been
        moved aside — at exactly the moment they were deciding what to do about
        it. It survived four months because `test_conflicted_section` above
        asserts only that the word "Conflicts" appears, never which side moved.
        """
        body = self._log(tmp_path, conflicted_files=["projects/-foo/memory/c.md"])
        assert "remote saved as .sync-conflict-*" in body
        assert "local preserved" not in body

    def test_skipped_section(self, tmp_path: Path) -> None:
        body = self._log(tmp_path, skipped_files=["projects/-foo/memory/s.md"])
        assert "Skipped" in body
        assert "local was newer" in body
        assert "memory/s.md" in body

    def test_all_five_categories_together(self, tmp_path: Path) -> None:
        body = self._log(
            tmp_path,
            new_files=["projects/-foo/memory/n.md"],
            modified_files=["projects/-foo/memory/m.md"],
            deleted_files=["projects/-foo/memory/d.md"],
            conflicted_files=["projects/-foo/memory/c.md"],
            skipped_files=["projects/-foo/memory/s.md"],
        )
        for fname in ("n.md", "m.md", "d.md", "c.md", "s.md"):
            assert fname in body

    def test_empty_buckets_dont_emit_headers(self, tmp_path: Path) -> None:
        """A project with only new_files gets the New header but not the
        others — empty sections don't pollute the log."""
        body = self._log(tmp_path, new_files=["projects/-foo/memory/x.md"])
        assert "New from other machine" in body
        assert "Updated from other machine" not in body
        assert "Removed on other machine" not in body
        assert "Conflicts" not in body
        assert "Skipped" not in body


# ── metadata ─────────────────────────────────────────────────────────


class TestLogMetadata:
    def test_log_includes_device_and_timestamp(self, tmp_path: Path) -> None:
        claude = _claude_tree(tmp_path, "-foo")
        logs = write_sync_log(
            claude_base=str(claude),
            device_name="MacBook Pro",
            device_id="abc123",
            new_files=["projects/-foo/memory/x.md"],
            modified_files=[],
            deleted_files=[],
        )
        body = logs[0].read_text()
        assert "MacBook Pro" in body
        assert "abc123" in body
        assert "Mind Meld Activity" in body
        # Timestamp in ISO-like form
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", body)


# ── param rename regression (v0.8.7) ─────────────────────────────────


class TestParamRenameRegression:
    """v0.8.7 renamed the first positional parameter `claude_dir` → `claude_base`
    to tell the truth: this function is claude-specific (hardcodes the
    `projects/` subdirectory layout) but works for a custom-path claude-type
    source, not only a config claude_dir field. Pin the new name so a
    subsequent rename-back doesn't silently break callers at runtime.
    """

    def test_keyword_arg_by_new_name(self, tmp_path: Path) -> None:
        claude = _claude_tree(tmp_path, "-foo")
        # Must accept `claude_base=` keyword without TypeError.
        logs = write_sync_log(
            claude_base=str(claude),
            device_name="A",
            device_id="x",
            new_files=["projects/-foo/memory/x.md"],
            modified_files=[],
            deleted_files=[],
        )
        assert len(logs) == 1

    def test_old_kwarg_name_rejected(self, tmp_path: Path) -> None:
        """Passing the old `claude_dir=` kwarg raises TypeError — this
        lets any stale caller fail loudly, not silently write to the
        wrong location.
        """
        claude = _claude_tree(tmp_path, "-foo")
        with pytest.raises(TypeError):
            write_sync_log(
                claude_dir=str(claude),  # type: ignore[call-arg]
                device_name="A",
                device_id="x",
                new_files=[],
                modified_files=[],
                deleted_files=[],
            )
