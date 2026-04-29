"""Unit tests for the conflictdiff leaf primitives.

These primitives are pure functions, so the tests are tmp_path-free and
do not exercise any CLI surface. Integration tests for the prompt sites
themselves live in test_conflict_copy.py.

Coverage targets, per the eng-review test plan:
* render_prompt: post-inversion + pre-inversion copy
* render_banner: local vs remote, peer_name set / None / ambiguous,
  adversarial OSC 52 in path AND device_name
* count_divergent_lines: empty, only +, only -, mixed, header exclusion
"""

from __future__ import annotations

from rich.console import Console

from mind_meld.conflictdiff import (
    count_divergent_lines,
    render_banner,
    render_prompt,
)


class TestRenderPrompt:
    def test_post_inversion_lines(self) -> None:
        out = render_prompt("notes.md", "notes.sync-conflict-X.md", "post_inversion")
        assert "(l)ocal" in out
        assert "discard notes.sync-conflict-X.md" in out
        assert "keep notes.md as-is" in out
        assert "(r)emote" in out
        assert "overwrite notes.md with bytes from notes.sync-conflict-X.md" in out
        assert "(s)kip" in out
        assert "(a)bort" in out

    def test_pre_inversion_flips_local_remote_actions(self) -> None:
        out = render_prompt("notes.md", "v0-notes.sync-conflict-X.md", "pre_inversion")
        # In pre-inversion the conflict file holds LOCAL bytes; (l)ocal
        # promotes the conflict file over canonical, (r)emote discards it.
        assert "promote v0-notes.sync-conflict-X.md (your local edits) over notes.md" in out
        assert "discard v0-notes.sync-conflict-X.md (your local edits); keep notes.md as-is" in out

    def test_skip_and_abort_copy_mode_independent(self) -> None:
        post = render_prompt("a", "b", "post_inversion")
        pre = render_prompt("a", "b", "pre_inversion")
        # The (s)kip and (a)bort lines are identical across modes.
        assert "leave both files on disk" in post
        assert "leave both files on disk" in pre
        assert "stop reviewing; exit" in post
        assert "stop reviewing; exit" in pre


class TestRenderBanner:
    def _render_to_str(self, text) -> str:
        # Render through a Console with terminal=True so ANSI codes show
        # up if they leak; capture as string for assertions.
        c = Console(record=True, width=120, force_terminal=True, color_system="truecolor")
        c.print(text)
        return c.export_text()

    def test_local_side_label_and_path(self) -> None:
        rendered = self._render_to_str(render_banner("local", "notes.md", None))
        assert "LOCAL" in rendered
        assert "notes.md" in rendered

    def test_remote_side_with_peer_name(self) -> None:
        rendered = self._render_to_str(
            render_banner("remote", "notes.sync-conflict-X.md", "kb-mbp")
        )
        assert "REMOTE" in rendered
        assert "from kb-mbp" in rendered

    def test_remote_side_unknown_peer(self) -> None:
        rendered = self._render_to_str(render_banner("remote", "notes.sync-conflict-X.md", None))
        assert "unknown peer" in rendered

    def test_remote_side_ambiguous_count(self) -> None:
        rendered = self._render_to_str(
            render_banner("remote", "notes.sync-conflict-X.md", "kb-mbp", ambiguous_count=2)
        )
        assert "ambiguous" in rendered
        assert "2 peers match" in rendered
        # Ambiguous beats peer_name -- don't show a name we can't trust.
        assert "from kb-mbp" not in rendered

    def test_local_side_strips_osc52_from_path(self) -> None:
        # OSC 52 clipboard write embedded in the filename. Banner must
        # strip the escape sequence before it reaches the terminal.
        evil = "notes\x1b]52;c;ZXZpbA==\x07.md"
        rendered = self._render_to_str(render_banner("local", evil, None))
        assert "\x1b]52" not in rendered
        assert "\x07" not in rendered
        # The literal characters of the path that survive stripping
        # should still render.
        assert "notes" in rendered

    def test_remote_side_strips_csi_from_peer_name(self) -> None:
        evil_name = "kb\x1b[2J-mbp"
        rendered = self._render_to_str(
            render_banner("remote", "notes.sync-conflict-X.md", evil_name)
        )
        assert "\x1b[2J" not in rendered
        assert "kb" in rendered
        assert "mbp" in rendered

    def test_local_side_does_not_render_peer_attribution(self) -> None:
        rendered = self._render_to_str(render_banner("local", "notes.md", "kb-mbp"))
        # peer_name is irrelevant on the LOCAL side; banner ignores it.
        assert "from kb-mbp" not in rendered
        assert "unknown peer" not in rendered


class TestCountDivergentLines:
    def test_empty_diff_returns_zeros(self) -> None:
        assert count_divergent_lines([]) == (0, 0, 0)

    def test_only_added_lines(self) -> None:
        diff = ["@@ -1,0 +1,2 @@", "+new line 1", "+new line 2"]
        assert count_divergent_lines(diff) == (0, 2, 2)

    def test_only_removed_lines(self) -> None:
        diff = ["@@ -1,2 +1,0 @@", "-old line 1", "-old line 2"]
        assert count_divergent_lines(diff) == (2, 0, 2)

    def test_mixed_diff_counts_both(self) -> None:
        diff = [
            "@@ -1,3 +1,3 @@",
            " context",
            "-removed",
            "+added",
            " more context",
        ]
        assert count_divergent_lines(diff) == (1, 1, 2)

    def test_excludes_unified_diff_headers(self) -> None:
        # The "---" / "+++" file-header lines must NOT count as removed/
        # added. Otherwise every diff would have +1 to both M and N.
        diff = [
            "--- local (notes.md)",
            "+++ remote (notes.sync-conflict-X.md)",
            "@@ -1,1 +1,1 @@",
            "-old",
            "+new",
        ]
        assert count_divergent_lines(diff) == (1, 1, 2)

    def test_replacement_counts_as_one_each_side(self) -> None:
        # A 1-line replacement shows as one "-" and one "+". The summary
        # copy in cli.py reads "removed-or-replaced" / "added-or-replaced"
        # precisely so this isn't misread as two independent edits.
        diff = ["@@ -1,1 +1,1 @@", "-foo", "+bar"]
        assert count_divergent_lines(diff) == (1, 1, 2)
