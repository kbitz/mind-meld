"""Sanitizer regression pins (Group 7 preflight #1 + D2 + D7 + Track 50A).

Peer-controlled strings (filenames, file contents) flow through Rich
console.print at many sites in cli.py. Without sanitization, a peer can
plant Rich markup or ANSI escape sequences in synced filenames or file
bodies and have them rendered as control output during pull/conflict/
merge feedback. safe_str strips ANSI escapes AND escapes Rich markup.

safe_terminal_str is the plain-stderr field helper: strip known grammars,
then render residual nonprintable characters as ascii() notation so the
output is a single printable line. The malformed-blob GC composition test
pins safe_str(safe_terminal_str(...)) on a Rich sink.

Diff content lines additionally use console.print(Text(line)) so Rich
never interprets markup in remote-byte file contents.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from rich.console import Console
from rich.text import Text

from mind_meld.safety import safe_str, safe_terminal_str, safe_text, strip_terminal_escapes

# Nested CSI that one strip pass turns into OSC 52 (BEL-terminated).
_NESTED_OSC_BEL = "\x1b\x1b[31m]52;c;ZXZpbA==\x07"
# Nested CSI that one strip pass turns into OSC 52 (ST-terminated).
_NESTED_OSC_ST = "\x1b\x1b[31m]52;c;VEVTVA==\x1b\x1b[31m\\"


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
        monkeypatch.setattr(cli, "_get_config", lambda: config)
        monkeypatch.setattr(cli, "_get_passphrase_or_exit", lambda: "passphrase")
        monkeypatch.setattr(cli, "get_backend", lambda _config: object())
        monkeypatch.setattr(cli, "_init_crypto_session", lambda *args: 1024)
        monkeypatch.setattr(cli, "get_sources", lambda _config: [])
        monkeypatch.setattr(cli, "build_manifest_v2", lambda *args: {"sources": {}})
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
            "check_for_upgrade",
            lambda _config: SimpleNamespace(state="up-to-date", latest=None),
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

    def test_config_bootstrap_warning_strips_terminal_escapes(self, monkeypatch, capsys):
        from mind_meld import config

        evil = "denied\x1b]52;c;ZXZpbA==\x07"

        class FailingPath:
            def expanduser(self):
                return self

            def exists(self):
                return False

            def stat(self):
                raise FileNotFoundError("missing")

            def mkdir(self, **_kwargs):
                raise OSError(evil)

            def __str__(self):
                return "path\x1b[2J"

        monkeypatch.setattr(config, "Path", lambda _path: FailingPath())
        monkeypatch.setattr(config, "_BOOTSTRAP_WARNED_PATHS", set())
        config._bootstrap_mm_events_path("ignored")

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
        c = Console(record=True, width=120, force_terminal=True, color_system="truecolor")
        c.print(text)
        return c.export_text()

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
