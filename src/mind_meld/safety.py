"""Peer-controlled string sanitization (load-bearing, v0.10.1, security).

Every synced filename, file body, and config-derived peer name crosses an
untrusted trust boundary. Without sanitization, a peer can plant Rich markup
or terminal escape sequences (CSI / OSC / DCS / single-byte) that get
interpreted when rendered to a terminal. The OSC 52 vector is particularly
nasty: many terminals (xterm, iTerm2, kitty, alacritty) honor base64-encoded
clipboard writes from remote-controlled escape sequences, silently changing
the user's clipboard contents.

`strip_terminal_escapes` removes the full set of common escape grammars.
It is not a no-control guarantee: one regex pass can assemble a fresh OSC
by deleting an inner CSI. `safe_str` composes that with Rich markup
escaping so peer-controlled strings render as literal text in markup
contexts. `safe_terminal_str` is the stronger single-line plain-stderr
helper: strip, then visibly escape every remaining nonprintable character.

Diff CONTENT (where the line is bytes from a remote file) goes through
`safe_text` instead, which strips escapes BEFORE wrapping in Rich's
`Text()` -- `Text()` alone defangs markup but passes raw ANSI through.
`safe_text` still uses one strip pass; nested-escape hardening of Rich
and multiline sinks is a recorded follow-up.

Originally defined in cli.py through v0.11.0; extracted to this module
so conflictdiff.py can use the helpers without creating a circular
import via cli.py.
"""

from __future__ import annotations

import re

from rich.markup import escape as rich_markup_escape
from rich.text import Text

_ANSI_ESCAPE_RE = re.compile(
    # CSI: ESC [ params final-byte (40-126) -- matches \x1b[2J, \x1b[31m, etc.
    r"\x1b\[[\d;?]*[\x40-\x7e]"
    # OSC: ESC ] params terminator (BEL or ESC \) -- matches \x1b]52;c;...\x07
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    # DCS / SOS / PM / APC: ESC P/X/^/_ params terminator (ESC \)
    r"|\x1b[PX^_][^\x1b]*\x1b\\"
    # 8-bit C1 introducer (rarely-used 0x9b CSI variant)
    r"|\x9b[\d;?]*[\x40-\x7e]"
    # Single-byte escapes: SS2, SS3, RIS, etc. (ESC + single 0x40-0x5F char)
    r"|\x1b[\x40-\x5f]"
)


def strip_terminal_escapes(s: str) -> str:
    """Strip CSI / OSC / DCS / C1 / single-byte terminal escape sequences.

    The peer-controlled trust boundary spans more than just CSI color
    codes. Apply BEFORE rendering any peer-controlled string to a
    real terminal -- Rich's Text() does not strip these.

    One pass is not a no-control guarantee: deleting an inner CSI can
    assemble a fresh OSC that this function no longer sees. Plain-stderr
    fields should use `safe_terminal_str`. Other consumers of this
    helper remain a recorded follow-up.
    """
    return _ANSI_ESCAPE_RE.sub("", s)


def safe_terminal_str(value: object) -> str:
    """Return a single-line printable representation of `value` for plain stderr.

    Stringify, strip recognized terminal-escape grammars, then keep each
    remaining character only if `str.isprintable()` is true; otherwise
    render that character using `ascii(character)[1:-1]`. Preserves
    printable Unicode and literal brackets. Does not add Rich markup
    escaping -- compose with `safe_str` when interpolating into Rich
    markup.

    Display text is not a filesystem identity or a shell argument.
    Callers must keep the original Path / storage key for validation,
    recovery, and deletion.
    """
    stripped = strip_terminal_escapes(str(value))
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in stripped)


def safe_str(s: object) -> str:
    """Return a Rich-markup-escaped, escape-stripped representation of `s`.

    Use at print sites interpolating a peer-controlled string into Rich
    markup (filenames, paths, source names, device names, error message
    tails). Returns a plain str so f-string composition with Rich markup
    tags continues to work -- `f"[red]write failed:[/red] {safe_str(rel_path)}"`.

    This is not a no-control guarantee: one regex pass can assemble a
    fresh OSC by deleting an inner CSI. For single-line plain stderr
    (no Rich markup), use `safe_terminal_str` instead. Other consumers
    of this helper remain a recorded follow-up.
    """
    return rich_markup_escape(strip_terminal_escapes(str(s)))


def safe_text(s: str, **kwargs: object) -> Text:
    """Return a Rich Text wrapping a terminal-escape-stripped str.

    Use for diff CONTENT lines (peer-controlled file bytes printed via
    console.print). Text() alone defangs Rich markup but passes raw
    ANSI/OSC/DCS through to the terminal. Strip escapes first. One pass
    is not a no-control guarantee; nested-escape hardening of multiline
    sinks is a recorded follow-up.
    """
    return Text(strip_terminal_escapes(s), **kwargs)
