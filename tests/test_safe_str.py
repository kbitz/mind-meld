"""Sanitizer regression pins (Group 7 preflight #1 + D2 + D7 + Track 50A + 52A).

Peer-controlled strings (filenames, file contents) flow through Rich
console.print at many sites in cli.py. Without sanitization, a peer can
plant Rich markup or ANSI escape sequences in synced filenames or file
bodies and have them rendered as control output during pull/conflict/
merge feedback. safe_str strips ANSI escapes AND escapes Rich markup.

Public strip_terminal_escapes / safe_str / safe_text are ESC/C1-free after
grammar stripping. safe_terminal_str is the plain-stderr field helper:
strip known grammars, then render residual nonprintable characters as
ascii() notation so the output is a single printable line. The
malformed-blob GC composition test pins safe_str(safe_terminal_str(...))
on a Rich sink.

Diff content lines additionally use console.print(Text(line)) so Rich
never interprets markup in remote-byte file contents.
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from rich.console import Console
from rich.text import Text

from mind_meld.safety import safe_str, safe_terminal_str, safe_text, strip_terminal_escapes

# Nested CSI that one strip pass turns into OSC 52 (BEL-terminated).
_NESTED_OSC_BEL = "\x1b\x1b[31m]52;c;ZXZpbA==\x07"
# Nested CSI that one strip pass turns into OSC 52 (ST-terminated).
_NESTED_OSC_ST = "\x1b\x1b[31m]52;c;VEVTVA==\x1b\x1b[31m\\"

_ESC = "\x1b"
_C1_CODEPOINTS = tuple(chr(n) for n in range(0x80, 0xA0))
_FORBIDDEN_ESC_C1 = frozenset((_ESC, *_C1_CODEPOINTS))
# Grammar-inert sentinel: not an ANSI final byte, so ESC/C1 + this cannot
# be consumed as a complete CSI/single-byte sequence.
_SNOWMAN = "\u2603"

_MALFORMED_SEQUENCES = (
    _ESC,
    f"{_ESC}[",
    f"{_ESC}]",
    f"{_ESC}]52;c;partial",
    f"{_ESC}P",
    f"{_ESC}c",
    "\x9b",
    "\x9d",
    "\x9d52;c;x",
    f"{_ESC}[31",
    f"{_ESC}P1$rm",
)


def _assert_no_esc_or_c1(value: str) -> None:
    hits = [f"U+{ord(ch):04X}" for ch in value if ch in _FORBIDDEN_ESC_C1]
    assert not hits, f"forbidden codepoints survived: {hits} in {ascii(value)}"


def _assert_plain_notice_field(field: str, *, starts_with: str) -> None:
    """Pin a migrated stderr field as safe_terminal_str, with ascii failures."""
    _assert_no_esc_or_c1(field)
    _assert_printable_field(field)
    if not field.startswith(starts_with):
        raise AssertionError(f"expected prefix {ascii(starts_with)}, got {ascii(field)}")
    if r"\[" in field:
        raise AssertionError(f"Rich markup backslash in {ascii(field)}")
    notation = ascii("\x1b")[1:-1]
    if notation not in field:
        raise AssertionError(f"missing residual ESC notation in {ascii(field)}")


def _helper_outputs(raw: str) -> dict[str, str]:
    return {
        "strip_terminal_escapes": strip_terminal_escapes(raw),
        "safe_str": safe_str(raw),
        "safe_text.plain": safe_text(raw).plain,
        "safe_terminal_str": safe_terminal_str(raw),
    }


def _forced_console(buf: io.StringIO, *, markup: bool) -> Console:
    return Console(
        file=buf,
        force_terminal=True,
        color_system=None,
        highlight=False,
        markup=markup,
        width=200,
    )


def _capture_printed(value, *, markup: bool) -> str:
    buf = io.StringIO()
    _forced_console(buf, markup=markup).print(value)
    return buf.getvalue()


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

    @pytest.mark.parametrize("introducer", ["X", "^", "_"])
    def test_sos_pm_apc_sequences(self, introducer):
        # SOS / PM / APC share the DCS alternation (ESC X / ESC ^ / ESC _,
        # ST-terminated): the whole grammar is removed. Unterminated, the
        # introducer (0x58 / 0x5E / 0x5F) falls inside the single-byte
        # ESC+0x40-0x5F alternation, so ESC+introducer is consumed together
        # and the body is literal with no residual ESC for either helper.
        terminated = f"a\x1b{introducer}payload\x1b\\b"
        assert strip_terminal_escapes(terminated) == "ab"
        unterminated = f"a\x1b{introducer}payload"
        assert strip_terminal_escapes(unterminated) == "apayload"
        assert safe_terminal_str(unterminated) == "apayload"

    def test_c1_8bit_csi(self):
        # 0x9b is the 8-bit C1 form of CSI.
        assert strip_terminal_escapes("a\x9b31mred") == "ared"

    def test_single_byte_escape(self):
        # SS3 is ESC + 'O' — used by some keyboards for function keys.
        assert strip_terminal_escapes("a\x1bOPb") == "aPb"

    def test_does_not_strip_normal_brackets(self):
        # Square brackets that are NOT terminal escapes survive.
        assert strip_terminal_escapes("plain [bracketed] text") == "plain [bracketed] text"


class TestFinalOutputSinks:
    """Pins for final renderers that previously interpolated raw values."""

    @staticmethod
    def _console(buf: io.StringIO) -> Console:
        return Console(file=buf, force_terminal=False, width=200)

    def test_error_renders_exception_text_as_literal_rich_text(self, monkeypatch):
        import typer

        from mind_meld import cli

        buf = io.StringIO()
        monkeypatch.setattr(cli, "stderr_console", self._console(buf))
        with pytest.raises(typer.Exit):
            cli._error("bad \x1b]52;c;ZXZpbA==\x07[red]configuration[/red]")

        out = buf.getvalue()
        assert "\x1b" not in out
        assert "ZXZpbA==" not in out
        assert "[red]configuration[/red]" in out

    def test_dropped_device_warning_renders_peer_fields_as_literal(self, monkeypatch):
        from mind_meld import cli

        buf = io.StringIO()
        monkeypatch.setattr(cli, "stderr_console", self._console(buf))
        evil = "peer\x1b]52;c;ZXZpbA==\x07[red]name[/red]"

        def fake_list_devices_impl(_backend, *, on_drop):
            on_drop(evil, evil)
            return []

        monkeypatch.setattr(cli, "_list_devices_impl", fake_list_devices_impl)
        assert cli._list_devices_warn(object()) == []

        out = buf.getvalue()
        assert "\x1b" not in out
        assert "ZXZpbA==" not in out
        assert "[red]name[/red]" in out

    def test_status_renders_peer_device_fields_as_literal(self, monkeypatch, tmp_path: Path):
        from mind_meld import cli

        buf = io.StringIO()
        peer_name = "peer\x1b]52;c;ZXZpbA==\x07[red]name[/red]"
        peer_id = "peer\x1b[2Jid"
        config = {
            "device": {"id": "self", "name": "self"},
            "sync": {"max_file_size": 0, "disabled_sources": []},
        }
        monkeypatch.setattr(cli, "console", self._console(buf))
        monkeypatch.setattr(cli, "_get_config", lambda **kwargs: config)
        monkeypatch.setattr(cli, "_get_passphrase_or_exit", lambda: "passphrase")
        monkeypatch.setattr(cli, "get_backend", lambda _config: object())
        monkeypatch.setattr(cli, "_init_crypto_session", lambda *args, **kwargs: 1024)
        monkeypatch.setattr(cli, "get_sources", lambda _config: [])
        monkeypatch.setattr(cli, "build_manifest_v2", lambda *args, **kwargs: {"sources": {}})
        monkeypatch.setattr(
            cli,
            "_fetch_remote_manifest",
            lambda *args: SimpleNamespace(is_ok=False, manifest=None, status="missing"),
        )
        monkeypatch.setattr(
            cli,
            "_list_devices_warn",
            lambda _backend: [
                {"device_id": "self", "device_name": "self"},
                {"device_id": peer_id, "device_name": peer_name},
            ],
        )
        monkeypatch.setattr(cli, "_autorun_breadcrumb_path", lambda: tmp_path / "missing")
        monkeypatch.setattr(cli, "_config_missing_recommended_excludes", lambda _config: [])
        monkeypatch.setattr(
            cli.upgrade,
            "cached_upgrade_view",
            lambda _config: cli.upgrade.UpgradeCheckResult("current", "0.14.12", None, None),
        )
        monkeypatch.setattr(cli.seen_sources, "read", lambda *, initial: set())
        monkeypatch.setattr(cli.seen_sources, "compute_new_sources", lambda **kwargs: [])
        monkeypatch.setattr(cli, "iter_source_diffs", lambda *args, **kwargs: iter(()))
        monkeypatch.setattr(cli, "_has_mtime_only_changes_vs_remote", lambda *args, **kwargs: False)

        cli.status(source=None)

        out = buf.getvalue()
        assert "\x1b" not in out
        assert "ZXZpbA==" not in out
        assert "[red]name[/red]" in out
        assert "peerid" in out

    def test_error_nested_st_probe_has_no_esc_or_c1(self, monkeypatch):
        import typer

        from mind_meld import cli

        buf = io.StringIO()
        monkeypatch.setattr(
            cli,
            "stderr_console",
            Console(
                file=buf,
                force_terminal=True,
                color_system=None,
                highlight=False,
                width=200,
            ),
        )
        with pytest.raises(typer.Exit):
            cli._error(f"bad {_NESTED_OSC_ST}[red]configuration[/red]")

        out = buf.getvalue()
        _assert_no_esc_or_c1(out)
        assert "[red]configuration[/red]" in out
        assert "bad" in out

    def test_auto_typed_error_nested_st_probe_has_no_esc_or_c1(self, monkeypatch):
        # cli.py's autopull/autopush typed-error line is a plain-stderr sink
        # routed through strip_terminal_escapes (not a Rich console). The
        # nested probe leaves a bare ESC after one grammar pass; the
        # residual pass must delete it before the line reaches stderr.
        # StringIO, not capsys: a failing capsys test replays the captured
        # bytes raw into the developer terminal.
        from mind_meld import cli

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", buf)
        cli._print_auto_typed_error(
            "autopull", "skipped", RuntimeError(f"bad {_NESTED_OSC_ST}config")
        )

        err = buf.getvalue()
        if err.count("\n") != 1:
            raise AssertionError(f"expected one line, got {ascii(err)}")
        if not err.startswith("mm: autopull skipped - bad "):
            raise AssertionError(f"bad prefix in {ascii(err)}")
        _assert_no_esc_or_c1(err)
        assert "config" in err

    def test_events_whole_walk_notice_strips_terminal_escapes(self, monkeypatch, capsys):
        from mind_meld import events

        evil = "walk\x1b]52;c;ZXZpbA==\x07failure"

        class FailingExecutor:
            def __init__(self, **_kwargs):
                pass

            def submit(self, *_args):
                raise RuntimeError(evil)

            def shutdown(self, **_kwargs):
                pass

        monkeypatch.setattr(events, "ThreadPoolExecutor", FailingExecutor)
        events.walk_git_projects([Path("/tmp/peer")], datetime.now(timezone.utc), 250)

        out = capsys.readouterr().err
        assert "\x1b" not in out
        assert "ZXZpbA==" not in out
        assert "walkfailure" in out

    def test_config_bootstrap_warning_strips_terminal_escapes(self, monkeypatch, capsys, tmp_path):
        from mind_meld import config

        evil = "denied\x1b]52;c;ZXZpbA==\x07"
        root = tmp_path / "path\x1b[2J"
        monkeypatch.setattr(config, "_normalized_mm_events_path", lambda _path: root)
        monkeypatch.setattr(config, "_is_default_mm_events_path", lambda _path: True)
        monkeypatch.setattr(config, "_BOOTSTRAP_WARNED_PATHS", set())

        def mkdir(path, **kwargs):
            raise OSError(evil)

        monkeypatch.setattr(Path, "mkdir", mkdir)
        config._bootstrap_mm_events_path(str(root))
        out = capsys.readouterr().err
        assert "\x1b" not in out
        assert "ZXZpbA==" not in out
        assert "denied" in out


class TestConflictBannerSanitization:
    """Cross-model tension T5 + T6 (eng-review 2026-04-29): peer-controlled
    bytes flow into the conflict-prompt LOCAL/REMOTE banners through both
    the conflict-file path AND the peer-supplied device_name. Banner
    rendering MUST strip terminal escapes from BOTH inputs before the
    Text/Console layer renders them, otherwise OSC 52 / CSI / DCS leak
    to the terminal.
    """

    @staticmethod
    def _render(text):
        buf = io.StringIO()
        c = Console(
            file=buf,
            force_terminal=True,
            color_system=None,
            highlight=False,
            width=120,
        )
        c.print(text)
        return buf.getvalue()

    def test_banner_strips_osc52_from_filename(self):
        from mind_meld.conflictdiff import render_banner

        evil_path = "notes\x1b]52;c;ZXZpbA==\x07.md"
        out = self._render(render_banner("local", evil_path, None))
        assert "\x1b]52" not in out
        assert "\x07" not in out
        assert "notes" in out

    def test_banner_strips_csi_from_filename(self):
        from mind_meld.conflictdiff import render_banner

        evil_path = "notes\x1b[2Jcleared.md"
        out = self._render(render_banner("local", evil_path, None))
        assert "\x1b[2J" not in out
        assert "notes" in out
        assert "cleared.md" in out

    def test_banner_strips_osc52_from_device_name(self):
        from mind_meld.conflictdiff import render_banner

        # A peer's device_name is set via typer.prompt at init on each
        # peer machine, then plaintext-synced via devices/<id>.json. A
        # malicious or confused peer can plant escapes in their own
        # device_name and have them reach every other machine that pulls.
        evil_name = "kb-mbp\x1b]52;c;ZXZpbA==\x07"
        out = self._render(render_banner("remote", "notes.sync-conflict-X.md", evil_name))
        assert "\x1b]52" not in out
        assert "\x07" not in out
        assert "kb-mbp" in out

    def test_banner_strips_csi_from_device_name(self):
        from mind_meld.conflictdiff import render_banner

        evil_name = "kb-mbp\x1b[2J"
        out = self._render(render_banner("remote", "notes.sync-conflict-X.md", evil_name))
        assert "\x1b[2J" not in out
        assert "kb-mbp" in out

    def test_banner_strips_dcs_from_filename(self):
        from mind_meld.conflictdiff import render_banner

        evil_path = "notes\x1bP1$rm\x1b\\.md"
        out = self._render(render_banner("local", evil_path, None))
        assert "\x1bP" not in out
        assert "notes" in out

    def test_banner_nested_st_probe_has_no_esc_or_c1(self):
        from mind_meld.conflictdiff import render_banner

        evil_path = f"notes{_NESTED_OSC_ST}.md"
        evil_name = f"kb-mbp{_NESTED_OSC_BEL}"
        path_out = self._render(render_banner("local", evil_path, None))
        name_out = self._render(render_banner("remote", "notes.sync-conflict-X.md", evil_name))
        _assert_no_esc_or_c1(path_out)
        _assert_no_esc_or_c1(name_out)
        assert "notes" in path_out
        assert "kb-mbp" in name_out


def _assert_printable_field(value: str) -> None:
    assert all(ch.isprintable() for ch in value)
    assert "\n" not in value
    assert "\r" not in value
    assert "\x1b" not in value
    assert "\x07" not in value


class TestSafeTerminalStr:
    """Plain-stderr field helper: strip known grammars, then escape residuals."""

    def test_preserves_ordinary_text_brackets_and_unicode(self):
        assert safe_terminal_str("plain") == "plain"
        assert safe_terminal_str("[red]inject[/red]") == "[red]inject[/red]"
        assert "\\" not in safe_terminal_str("[red]inject[/red]")
        assert safe_terminal_str("café naïve") == "café naïve"
        assert safe_terminal_str("日本語") == "日本語"
        assert safe_terminal_str("") == ""

    def test_stringifies_path_and_exception(self):
        result = safe_terminal_str(Path("/tmp/[red]name"))
        assert "[red]name" in result
        assert "\\" not in result
        assert safe_terminal_str(RuntimeError("boom")) == "boom"

    @pytest.mark.parametrize(
        "raw,sentinel",
        [
            ("pre\x1b[31mred\x1b[0mpost", "preredpost"),
            ("pre\x1b]52;c;ZXZpbA==\x07post", "prepost"),
            ("pre\x1b]0;title\x1b\\post", "prepost"),
            ("pre\x1bP1$rm\x1b\\post", "prepost"),
            ("pre\x9b31mredpost", "preredpost"),
        ],
    )
    def test_known_sequences_stripped_sentinels_kept(self, raw, sentinel):
        out = safe_terminal_str(raw)
        _assert_printable_field(out)
        assert out == sentinel

    def test_nested_escape_construction_has_no_raw_controls(self):
        out = safe_terminal_str(f"head{_NESTED_OSC_BEL}tail")
        _assert_printable_field(out)
        assert "head" in out
        assert "tail" in out
        assert "\x1b]52" not in out
        assert "\x07" not in out

    @pytest.mark.parametrize(
        "raw",
        [
            "\r",
            "\n",
            "\t",
            "\x08",
            "\x07",
            "\x7f",
            "\x84",
            "\x1bc",
            "\u2028",
            "\u2029",
            "\u200d",
            "\u2066",
            "\ud800",
        ],
    )
    def test_residual_controls_become_visible_notation(self, raw):
        out = safe_terminal_str(f"L{raw}R")
        _assert_printable_field(out)
        assert out.startswith("L")
        assert out.endswith("R")
        assert raw not in out
        for ch in raw:
            if not ch.isprintable():
                assert ascii(ch)[1:-1] in out

    @given(st.text(max_size=64))
    @settings(max_examples=80, deadline=None)
    def test_generated_unicode_is_printable_single_line(self, value: str):
        out = safe_terminal_str(value)
        _assert_printable_field(out)


class TestEscC1Postcondition:
    """Track 52A: public raw/Rich helpers are ESC/C1-free; plain helper
    keeps visible notation and still satisfies the forbidden-set property.
    """

    def test_nested_st_and_bel_have_no_esc_or_c1(self):
        for probe, label in ((_NESTED_OSC_ST, "st"), (_NESTED_OSC_BEL, "bel")):
            raw = f"head{probe}tail"
            outputs = _helper_outputs(raw)
            for name, out in outputs.items():
                _assert_no_esc_or_c1(out)
                assert "head" in out, f"{label}/{name}: {ascii(out)}"
                assert "tail" in out, f"{label}/{name}: {ascii(out)}"
            captured_raw = _capture_printed(outputs["strip_terminal_escapes"], markup=False)
            captured_plain = _capture_printed(outputs["safe_terminal_str"], markup=False)
            captured_markup = _capture_printed(outputs["safe_str"], markup=True)
            captured_text = _capture_printed(safe_text(raw), markup=True)
            for captured in (captured_raw, captured_plain, captured_markup, captured_text):
                _assert_no_esc_or_c1(captured)
                assert "head" in captured
                assert "tail" in captured

    def test_ris_esc_c_has_no_esc_or_c1(self):
        raw = f"{_SNOWMAN}{_ESC}c{_SNOWMAN}"
        outputs = _helper_outputs(raw)
        for name, out in outputs.items():
            _assert_no_esc_or_c1(out)
            assert _SNOWMAN in out, f"{name}: {ascii(out)}"
        assert outputs["strip_terminal_escapes"] == f"{_SNOWMAN}c{_SNOWMAN}"
        assert outputs["safe_text.plain"] == f"{_SNOWMAN}c{_SNOWMAN}"
        assert ascii(_ESC)[1:-1] in outputs["safe_terminal_str"]
        assert outputs["safe_terminal_str"].endswith(f"c{_SNOWMAN}")

    @pytest.mark.parametrize("ch", _C1_CODEPOINTS)
    def test_each_c1_codepoint_has_no_esc_or_c1(self, ch: str):
        raw = f"{_SNOWMAN}{ch}{_SNOWMAN}"
        outputs = _helper_outputs(raw)
        for name, out in outputs.items():
            _assert_no_esc_or_c1(out)
            assert out.startswith(_SNOWMAN), f"{name}: {ascii(out)}"
            assert out.endswith(_SNOWMAN), f"{name}: {ascii(out)}"
        assert outputs["strip_terminal_escapes"] == f"{_SNOWMAN}{_SNOWMAN}"
        assert outputs["safe_text.plain"] == f"{_SNOWMAN}{_SNOWMAN}"
        assert ascii(ch)[1:-1] in outputs["safe_terminal_str"]

    def test_bare_esc_and_bare_c1_osc_have_no_esc_or_c1(self):
        for raw in (
            f"{_SNOWMAN}{_ESC}{_SNOWMAN}",
            f"{_SNOWMAN}\x9d{_SNOWMAN}",
            f"{_SNOWMAN}\x9d52;c;x{_SNOWMAN}",
        ):
            for name, out in _helper_outputs(raw).items():
                _assert_no_esc_or_c1(out)
                assert _SNOWMAN in out, f"{name}: {ascii(out)}"

    @pytest.mark.parametrize("raw", _MALFORMED_SEQUENCES)
    def test_malformed_sequences_have_no_esc_or_c1(self, raw: str):
        wrapped = f"{_SNOWMAN}{raw}{_SNOWMAN}"
        for name, out in _helper_outputs(wrapped).items():
            _assert_no_esc_or_c1(out)
            assert _SNOWMAN in out, f"{name}: {ascii(out)}"

    def test_esc_r_is_consumed_by_existing_grammar(self):
        # ESC+R and C1-CSI+R are complete grammars; finite-set tests must
        # not treat the following ASCII letter as a surviving sentinel.
        assert strip_terminal_escapes(f"{_SNOWMAN}{_ESC}R{_SNOWMAN}") == f"{_SNOWMAN}{_SNOWMAN}"
        assert strip_terminal_escapes(f"{_SNOWMAN}\x9bR{_SNOWMAN}") == f"{_SNOWMAN}{_SNOWMAN}"

    def test_lf_and_ht_remain_exact_in_raw_and_text_plain(self):
        raw = "a\nb\tc"
        assert strip_terminal_escapes(raw) == raw
        assert safe_text(raw).plain == raw

    def test_safe_text_forwards_kwargs(self):
        text = safe_text("hi", style="red")
        assert text.plain == "hi"
        assert str(text.style) == "red"

    def test_literal_tag_fragments_joined_by_deletion_stay_literal(self):
        raw = f"[re{_ESC}[31md]inject[/re{_ESC}[0md]"
        escaped = safe_str(raw)
        assert escaped == r"\[red]inject\[/red]"
        out = _capture_printed(escaped, markup=True)
        _assert_no_esc_or_c1(out)
        assert "[red]" in out
        assert "[/red]" in out
        assert "inject" in out

    def test_empty_and_ordinary_unicode_unchanged(self):
        assert strip_terminal_escapes("") == ""
        assert safe_str("") == ""
        assert safe_text("").plain == ""
        assert safe_terminal_str("") == ""
        assert strip_terminal_escapes("café naïve 日本語") == "café naïve 日本語"

    @pytest.mark.parametrize("survivor", ["\x7f", "\xa0", "Ā"])
    def test_residual_deletion_bounds_are_exactly_esc_and_c1(self, survivor):
        # The residual regex is [\x1b\x80-\x9f]. Pin both fences: U+009F is
        # the last deleted codepoint; DEL (U+007F), NBSP (U+00A0) and the
        # first Latin Extended codepoint survive the raw / Rich helpers. The
        # policy is ESC/C1-only, not all-control, so a widened range would
        # start eating bytes out of peer filenames.
        raw = f"{_SNOWMAN}{survivor}{_SNOWMAN}"
        assert strip_terminal_escapes(raw) == raw
        assert safe_text(raw).plain == raw
        assert safe_str(raw) == raw
        assert strip_terminal_escapes(f"{_SNOWMAN}\x9f{_SNOWMAN}") == f"{_SNOWMAN}{_SNOWMAN}"

    @given(
        st.one_of(
            st.text(max_size=64),
            st.lists(
                st.one_of(
                    st.text(max_size=8),
                    st.sampled_from(
                        (
                            _ESC,
                            f"{_ESC}c",
                            _NESTED_OSC_ST,
                            _NESTED_OSC_BEL,
                            *_C1_CODEPOINTS,
                        )
                    ),
                ),
                max_size=8,
            ).map("".join),
        )
    )
    @settings(max_examples=80, deadline=None)
    @example(_NESTED_OSC_ST)
    @example(_NESTED_OSC_BEL)
    @example(f"{_ESC}c")
    @example("\x9d52;c;x\x9c")
    def test_generated_unicode_has_no_esc_or_c1(self, value: str):
        for name, out in _helper_outputs(value).items():
            _assert_no_esc_or_c1(out)


class TestGcMalformedKeyWarning:
    """G1: malformed blob-key warning is a Rich sink; compose both helpers."""

    def test_dry_run_warning_inert_and_key_preserved(self, tmp_path, monkeypatch):
        from mind_meld import cli
        from mind_meld.cli import _do_gc
        from mind_meld.crypto import encrypt
        from mind_meld.devices import register_device
        from mind_meld.manifest import serialize_manifest
        from mind_meld.storage.local import LocalBackend

        storage = LocalBackend(tmp_path / "storage")
        config = {
            "device": {"id": "dev1", "name": "Test"},
            "storage": {"path": str(tmp_path / "storage")},
            "crypto": {"argon2_memory_kb": 1024},
            "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        }
        register_device(storage, "dev1", "Test")
        manifest = {
            "device_id": "dev1",
            "device_name": "Test",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "files": {},
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {},
        }
        enc = encrypt(serialize_manifest(manifest), "test-passphrase", memory_kb=1024)
        storage.put("manifests/dev1/manifest.json.enc", enc)

        hostile_leaf = f"[/red]inject[red]{_NESTED_OSC_ST}.enc"
        bkey = f"data/{hostile_leaf}"
        payload = b"malformed-bytes"
        storage.put(bkey, payload)

        buf = io.StringIO()
        monkeypatch.setattr(
            cli,
            "console",
            Console(file=buf, force_terminal=True, width=200, color_system=None),
        )
        count = _do_gc(config, "test-passphrase", 1024, dry_run=True, verbose=False)
        out = buf.getvalue()

        assert count == 0
        assert "\x1b]52" not in out
        assert "\x1b\\" not in out
        assert "\x07" not in out
        assert "[/red]" in out
        assert "[red]" in out
        assert "inject" in out
        assert "malformed" in out
        assert storage.get(bkey) == payload

    def test_verbose_orphan_key_warning_is_inert(self, tmp_path, monkeypatch):
        from mind_meld import cli
        from mind_meld.cli import _do_gc
        from mind_meld.crypto import encrypt
        from mind_meld.devices import register_device
        from mind_meld.manifest import serialize_manifest
        from mind_meld.storage.local import LocalBackend

        storage = LocalBackend(tmp_path / "storage")
        config = {
            "device": {"id": "dev1", "name": "Test"},
            "storage": {"path": str(tmp_path / "storage")},
            "crypto": {"argon2_memory_kb": 1024},
            "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        }
        register_device(storage, "dev1", "Test")
        manifest = {
            "device_id": "dev1",
            "device_name": "Test",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "files": {},
            "sources": {"claude": {"base_path": "", "files": {}}},
            "tombstones": {},
        }
        enc = encrypt(serialize_manifest(manifest), "test-passphrase", memory_kb=1024)
        storage.put("manifests/dev1/manifest.json.enc", enc)

        sha = "a" * 64
        # parse_blob_key splits on /, so the device_id must not contain a slash
        # (`[/red]` would become extra path depth and miss the orphan branch).
        hostile_dev = f"evil[red]{_NESTED_OSC_ST}"
        bkey = f"data/{hostile_dev}/{sha}.enc"
        payload = b"orphan-bytes"
        storage.put(bkey, payload)

        buf = io.StringIO()
        monkeypatch.setattr(
            cli,
            "console",
            Console(file=buf, force_terminal=True, width=200, color_system=None),
        )
        count = _do_gc(config, "test-passphrase", 1024, dry_run=True, verbose=True)
        out = buf.getvalue()

        assert count == 1
        assert "\x1b]52" not in out
        assert "\x1b\\" not in out
        assert "\x07" not in out
        assert "[red]" in out
        assert "evil" in out
        assert "orphan" in out
        assert storage.get(bkey) == payload
