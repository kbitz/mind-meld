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

    def test_merge_option_absent_by_default(self) -> None:
        out = render_prompt("notes.md", "notes.sync-conflict-X.md", "post_inversion")
        assert "(m)erge" not in out

    def test_merge_option_clean_when_available_zero_conflicts(self) -> None:
        out = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            merge_available=True,
            merge_conflicts=0,
        )
        assert "(m)erge" in out
        assert "clean, no markers" in out
        assert "<<<<<<<" not in out

    def test_merge_option_marker_count_when_dirty(self) -> None:
        out = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            merge_available=True,
            merge_conflicts=2,
        )
        assert "(m)erge" in out
        assert "2 <<<<<<< regions" in out
        assert "resolve in editor after" in out

    def test_merge_option_singular_grammar_for_one_conflict(self) -> None:
        out = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            merge_available=True,
            merge_conflicts=1,
        )
        assert "1 <<<<<<< region" in out
        assert "1 <<<<<<< regions" not in out

    def test_merge_option_renders_in_pre_inversion_mode(self) -> None:
        out = render_prompt(
            "notes.md",
            "v0-notes.sync-conflict-X.md",
            "pre_inversion",
            merge_available=True,
            merge_conflicts=0,
        )
        assert "(m)erge" in out
        assert "promote v0-notes.sync-conflict-X.md" in out

    def test_promote_option_absent_by_default(self) -> None:
        out = render_prompt("notes.md", "notes.sync-conflict-X.md", "post_inversion")
        assert "(p)romote" not in out

    def test_promote_option_present_when_available(self) -> None:
        out = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            promote_available=True,
        )
        assert "(p)romote" in out
        # Both filenames appear in the keep-BOTH copy.
        assert "notes.sync-conflict-X.md" in out
        assert "keep notes.md" in out

    def test_promote_and_merge_both_render(self) -> None:
        out = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            merge_available=True,
            merge_conflicts=0,
            promote_available=True,
        )
        assert "(m)erge" in out
        assert "(p)romote" in out

    def test_drop_counts_omitted_when_unset(self) -> None:
        out = render_prompt("notes.md", "notes.sync-conflict-X.md", "post_inversion")
        # Default behavior (binary content / no diff) -- no drop annotation.
        assert "drops" not in out

    def test_drop_counts_post_inversion(self) -> None:
        # M=0 local-only, N=1 remote-only: this is the screenshot case.
        # (l)ocal end-state = local bytes, drops the 1 peer-only line.
        # (r)emote end-state = remote bytes, drops 0 of the user's lines.
        out = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            local_only_lines=0,
            remote_only_lines=1,
        )
        # Annotations attach to the right lines.
        local_line = next(line for line in out.splitlines() if "(l)ocal" in line)
        remote_line = next(line for line in out.splitlines() if "(r)emote" in line)
        assert "drops 1 peer line" in local_line
        assert "drops 0 of your lines" in remote_line

    def test_drop_counts_pre_inversion(self) -> None:
        # In pre_inversion the actions flip: (l)ocal promotes the conflict
        # file (your local edits), (r)emote keeps canonical (remote bytes).
        # The semantic drop counts are still local_only/remote_only --
        # the caller is responsible for mapping the raw diff m/n correctly.
        out = render_prompt(
            "notes.md",
            "v0-notes.sync-conflict-X.md",
            "pre_inversion",
            local_only_lines=2,
            remote_only_lines=3,
        )
        local_line = next(line for line in out.splitlines() if "(l)ocal" in line)
        remote_line = next(line for line in out.splitlines() if "(r)emote" in line)
        # (l)ocal end-state = local bytes, drops the 3 peer-only lines.
        assert "drops 3 peer lines" in local_line
        # (r)emote end-state = remote bytes, drops the 2 of-yours.
        assert "drops 2 of your lines" in remote_line

    def test_drop_counts_pluralization(self) -> None:
        out = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            local_only_lines=1,
            remote_only_lines=1,
        )
        # Singular form for count==1.
        assert "drops 1 peer line)" in out
        assert "drops 1 of your line)" in out
        assert "drops 1 peer lines" not in out
        assert "drops 1 of your lines" not in out

    def test_drop_counts_zero_zero_still_annotates(self) -> None:
        # Defensive: empty-diff prompts should pass None for both counts
        # (suppressing the annotation entirely). But if a caller passes
        # zeros, the annotation still renders -- "drops 0 lines" is honest
        # when the caller has actually compared and found no unique lines.
        out = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            local_only_lines=0,
            remote_only_lines=0,
        )
        assert "drops 0 peer lines" in out
        assert "drops 0 of your lines" in out

    def test_drop_counts_require_both(self) -> None:
        # Annotations only render when BOTH counts are provided. Asymmetric
        # callers pass through with no annotation, NOT a half-rendered prompt.
        local_only_set = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            local_only_lines=2,
        )
        remote_only_set = render_prompt(
            "notes.md",
            "notes.sync-conflict-X.md",
            "post_inversion",
            remote_only_lines=3,
        )
        assert "drops" not in local_only_set
        assert "drops" not in remote_only_set


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
