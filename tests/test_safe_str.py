"""safe_str() regression pins (Group 7 preflight #1 + D2 + D7).

Peer-controlled strings (filenames, file contents) flow through Rich
console.print at many sites in cli.py. Without sanitization, a peer can
plant Rich markup or ANSI escape sequences in synced filenames or file
bodies and have them rendered as control output during pull/conflict/
merge feedback. safe_str strips ANSI escapes AND escapes Rich markup.

Diff content lines additionally use console.print(Text(line)) so Rich
never interprets markup in remote-byte file contents.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text

from mind_meld.cli import safe_str, safe_text, strip_terminal_escapes


class TestStripsAnsi:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("\x1b[31mevil\x1b[0m", "evil"),
            ("\x1b[2Jclear", "clear"),
            ("plain", "plain"),
            ("", ""),
            ("\x1b[1;33;40mcomplex", "complex"),
        ],
    )
    def test_ansi_escape_sequences_removed(self, raw, expected):
        assert safe_str(raw) == expected


class TestEscapesRichMarkup:
    def test_brackets_escaped(self):
        assert safe_str("[red]inject[/red]") == r"\[red]inject\[/red]"

    def test_close_only_tag_escaped(self):
        assert safe_str("evil[/dim]name.md") == r"evil\[/dim]name.md"


class TestComposed:
    def test_ansi_inside_markup(self):
        # ANSI stripped first, then markup escaped — both threats handled.
        result = safe_str("\x1b[31m[/red]evil[red]\x1b[0m")
        assert "\x1b" not in result
        # Markup brackets escaped: every `[` in the input is preceded by a
        # backslash in the output (rich.markup.escape pattern).
        assert "\\[/red]" in result
        assert "\\[red]" in result
        assert "evil" in result

    def test_handles_non_str(self):
        # safe_str(Path) and safe_str(Exception) are common at call sites.
        from pathlib import Path

        result = safe_str(Path("/tmp/[red]name"))
        assert "\\[red]" in result


class TestRendersAsLiteralViaRich:
    """The headline contract: a sanitized peer string renders as the
    literal characters the peer chose, not as styled output."""

    def test_markup_renders_as_literal(self):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=200)
        peer_filename = "[/red]hidden[red]secret.md"
        console.print(f"  [yellow]conflict:[/yellow] {safe_str(peer_filename)}")
        out = buf.getvalue()
        # The literal brackets survive — Rich doesn't strip them as markup.
        assert "[/red]" in out
        assert "[red]" in out
        assert "secret.md" in out

    def test_ansi_does_not_alter_terminal_state(self):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=200)
        peer_filename = "innocent\x1b[2J\x1b[Hname.md"
        console.print(f"  [dim]skipped (missing): {safe_str(peer_filename)}[/dim]")
        out = buf.getvalue()
        # No raw ANSI escape bytes survived.
        assert "\x1b" not in out
        assert "innocentname.md" in out


class TestDiffContentViaText:
    """Diff lines (file body bytes from remote peer) render via safe_text()
    so the body cannot inject Rich markup OR terminal escapes."""

    def test_text_disables_markup_interpretation(self):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=200)
        peer_diff_line = "+ [/red]EVIL[red] body content"
        console.print(Text(peer_diff_line))
        out = buf.getvalue()
        assert "[/red]" in out
        assert "[red]" in out
        assert "EVIL" in out

    def test_safe_text_strips_ansi_in_body(self):
        """Adversarial #1: Text() alone passes raw ANSI through to terminal.
        safe_text() strips escapes BEFORE wrapping so the body can't
        clear-screen / move cursor / spoof prompts via peer-controlled bytes.
        """
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=200)
        peer_body = "+ innocent\x1b[2J\x1b[Hcontent"
        console.print(safe_text(peer_body))
        out = buf.getvalue()
        # No raw ANSI escape bytes survived.
        assert "\x1b" not in out
        assert "innocentcontent" in out

    def test_safe_text_strips_osc_clipboard_in_body(self):
        """Adversarial #2: OSC 52 (\\x1b]52;c;<base64>\\x07) is the worst
        terminal escape vector — many terminals honor it as a clipboard
        write from remote-controlled input. safe_text must strip it from
        diff body bytes."""
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=200)
        peer_body = "+ benign-looking\x1b]52;c;ZXZpbA==\x07line"
        console.print(safe_text(peer_body))
        out = buf.getvalue()
        assert "\x1b" not in out
        assert "ZXZpbA==" not in out  # base64 payload stripped too
        assert "benign-looking" in out
        assert "line" in out


class TestStripTerminalEscapesBroadCoverage:
    """The escape grammar is more than just CSI. Pin the broad coverage."""

    def test_csi_sequence(self):
        assert strip_terminal_escapes("\x1b[31mred\x1b[0m") == "red"

    def test_osc_sequence_bel_terminator(self):
        # OSC 52 clipboard write — the worst-case adversarial vector.
        assert strip_terminal_escapes("a\x1b]52;c;ZXZpbA==\x07b") == "ab"

    def test_osc_sequence_st_terminator(self):
        # OSC terminated by ESC \\ (ST) instead of BEL.
        assert strip_terminal_escapes("a\x1b]0;title\x1b\\b") == "ab"

    def test_dcs_sequence(self):
        assert strip_terminal_escapes("a\x1bP1$rm\x1b\\b") == "ab"

    def test_c1_8bit_csi(self):
        # 0x9b is the 8-bit C1 form of CSI.
        assert strip_terminal_escapes("a\x9b31mred") == "ared"

    def test_single_byte_escape(self):
        # SS3 is ESC + 'O' — used by some keyboards for function keys.
        assert strip_terminal_escapes("a\x1bOPb") == "aPb"

    def test_does_not_strip_normal_brackets(self):
        # Square brackets that are NOT terminal escapes survive.
        assert strip_terminal_escapes("plain [bracketed] text") == "plain [bracketed] text"
