"""Peer-controlled string sanitization (load-bearing, v0.10.1, security).

Every synced filename, file body, and config-derived peer name crosses an
untrusted trust boundary. Without sanitization, a peer can plant Rich markup
or terminal escape sequences (CSI / OSC / DCS / single-byte) that get
interpreted when rendered to a terminal. The OSC 52 vector is particularly
nasty: many terminals (xterm, iTerm2, kitty, alacritty) honor base64-encoded
clipboard writes from remote-controlled escape sequences, silently changing
the user's clipboard contents.

`_strip_terminal_grammar` removes recognized CSI / OSC / DCS / C1-CSI /
single-byte grammars. That private intermediate is not safe to render: one
regex pass can assemble a fresh OSC by deleting an inner CSI. Public
`strip_terminal_escapes` then deletes every residual ESC (U+001B) and C1
(U+0080–U+009F). `safe_str` and `safe_text` compose that public helper with
Rich markup escaping and Text wrapping. The guarantee is ESC/C1-free
peer-controlled text (and Text.plain), not an all-control or general
terminal-spoofing guarantee; LF and HT remain for diff bodies.
Caller-supplied Rich styles/kwargs are trusted application formatting.

`safe_terminal_str` is the stronger single-line plain-stderr helper: the
same grammar pass, then visible ASCII notation for every remaining
nonprintable character. Residual ESC/C1 appear as notation rather than
being deleted, which already satisfies the forbidden-codepoint property.

Display text is not a filesystem identity or a shell argument. Callers
must keep the original Path / storage key / model id for validation,
lookup, recovery, and deletion.

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
    # Single-byte C1-index escapes: SS2, SS3, etc. (ESC + one 0x40-0x5F char).
    # RIS (ESC c) is NOT in this range. Public strip_terminal_escapes deletes
    # leftover ESC; safe_terminal_str renders it as ascii() notation instead.
    r"|\x1b[\x40-\x5f]"
)

# After grammar removal, delete leftover introducers so a nested CSI cannot
# assemble a fresh OSC. ESC is U+001B; C1 is U+0080–U+009F inclusive.
_RESIDUAL_ESC_C1_RE = re.compile(r"[\x1b\x80-\x9f]")


def _strip_terminal_grammar(s: str) -> str:
    """Remove recognized terminal-escape grammars. Not safe to render.

    One substitution pass can assemble a fresh OSC by deleting an inner
    CSI. Public helpers apply a second policy after this call.
    """
    return _ANSI_ESCAPE_RE.sub("", s)


def strip_terminal_escapes(s: str) -> str:
    """Strip CSI / OSC / DCS / C1 / single-byte terminal escape sequences.

    The peer-controlled trust boundary spans more than just CSI color
    codes. Apply BEFORE rendering any peer-controlled string to a
    real terminal -- Rich's Text() does not strip these.

    After grammar stripping, every residual ESC (U+001B) and C1
    (U+0080–U+009F) is deleted. Returned text is ESC/C1-free; it is
    not an all-control guarantee. LF and HT survive for diff bodies.
    For single-line plain stderr, use `safe_terminal_str`.
    """
    return _RESIDUAL_ESC_C1_RE.sub("", _strip_terminal_grammar(s))


def safe_terminal_str(value: object) -> str:
    """Return a single-line printable representation of `value` for plain stderr.

    Stringify, strip recognized terminal-escape grammars, then keep each
    remaining character only if `str.isprintable()` is true; otherwise
    render that character using `ascii(character)[1:-1]`. Preserves
    printable Unicode and literal brackets. Residual ESC/C1 become
    visible ASCII notation rather than disappearing. Does not add Rich
    markup escaping -- compose with `safe_str` when interpolating into
    Rich markup.

    Display text is not a filesystem identity or a shell argument.
    Callers must keep the original Path / storage key / model id for
    validation, lookup, recovery, and deletion.
    """
    stripped = _strip_terminal_grammar(str(value))
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in stripped)


def safe_str(s: object) -> str:
    """Return a Rich-markup-escaped, escape-stripped representation of `s`.

    Use at print sites interpolating a peer-controlled string into Rich
    markup (filenames, paths, source names, device names, error message
    tails). Returns a plain str so f-string composition with Rich markup
    tags continues to work -- `f"[red]write failed:[/red] {safe_str(rel_path)}"`.

    Returned text contains no ESC or C1; this is not an all-control
    guarantee. For single-line plain stderr (no Rich markup), use
    `safe_terminal_str` instead.
    """
    return rich_markup_escape(strip_terminal_escapes(str(s)))


def safe_text(s: str, **kwargs: object) -> Text:
    """Return a Rich Text wrapping a terminal-escape-stripped str.

    Use for diff CONTENT lines (peer-controlled file bytes printed via
    console.print). Text() alone defangs Rich markup but passes raw
    ANSI/OSC/DCS through to the terminal. Strip escapes first. The
    returned Text.plain contains no ESC or C1; LF and HT are preserved.
    Caller-supplied Rich styles/kwargs are trusted application formatting.
    """
    return Text(strip_terminal_escapes(s), **kwargs)
