"""Pure leaf primitives for conflict-resolution prompt rendering.

Two prompt sites consume these helpers:
* ``_prompt_conflict_choice`` (cli.py) -- inline ``mm pull --conflict-mode prompt``
* ``_resolve_interactive_loop`` (cli.py) -- the post-pull ``mm resolve`` walk

Each site keeps its own dispatch over the canonical-exists / canonical-missing
and pre-inversion / post-inversion modes (CLAUDE.md flags filename-prefix
dispatch as load-bearing). These helpers handle only the rendering: prompt
copy, color banners above the diff, and a divergence-line counter.

All inputs that originate from peer-controlled state (filenames,
``device_name``, paths) MUST be sanitized via :func:`mind_meld.safety.safe_str`
or :func:`mind_meld.safety.safe_text` BEFORE composition. The banner helper
does its own ``safe_text`` wrap so a caller passing raw bytes still produces
a defanged renderable -- but the prompt copy is plain string interpolation,
so the caller is responsible for pre-sanitizing.
"""

from __future__ import annotations

from typing import Literal

from rich.text import Text

from mind_meld.safety import safe_text

InversionMode = Literal["pre_inversion", "post_inversion"]
Side = Literal["local", "remote"]


def render_prompt(
    canonical_name: str,
    conflict_name: str,
    mode: InversionMode,
    *,
    merge_available: bool = False,
    merge_conflicts: int = 0,
) -> str:
    """Return the four- or five-option choice copy for a conflict prompt.

    ``canonical_name`` and ``conflict_name`` are the bare filenames of the
    two on-disk files. The caller must ``safe_str`` these BEFORE passing
    -- this helper composes them into Rich-markup-bearing strings via
    f-string interpolation.

    Mode semantics (CLAUDE.md "Conflict-direction inversion"):
      * ``post_inversion`` -- canonical holds LOCAL bytes (the everyday
        case for files produced by mm v0.9.2+).
      * ``pre_inversion`` -- canonical holds REMOTE bytes (legacy
        ``v0-`` files migrated from pre-v0.9.2 produced sidecars).

    The ``(s)kip`` and ``(a)bort`` lines are mode-independent. Only
    ``(l)ocal`` / ``(r)emote`` flip per mode because the on-disk action
    flips: in post_inversion local-keep is "discard the conflict file",
    while in pre_inversion local-keep is "promote the conflict file
    (which holds local bytes) over canonical."

    ``merge_available`` enables the ``(m)erge`` option line. The caller
    runs ``mind_meld.merge.lcs_merge`` BEFORE calling this and passes
    ``merge_available=True`` only when binary content was not detected
    (``conflict_count >= 0``). ``merge_conflicts`` (0 or positive)
    annotates the (m) line so the user knows whether the merged
    candidate is clean or contains ``<<<<<<<`` markers they would have
    to resolve in a text editor afterward.
    """
    if mode == "post_inversion":
        local_line = f"  (l)ocal   -> discard {conflict_name}, keep {canonical_name} as-is"
        remote_line = f"  (r)emote  -> overwrite {canonical_name} with bytes from {conflict_name}"
    else:
        local_line = (
            f"  (l)ocal   -> promote {conflict_name} (your local edits) over {canonical_name}"
        )
        remote_line = (
            f"  (r)emote  -> discard {conflict_name} (your local edits); "
            f"keep {canonical_name} as-is"
        )
    lines = ["[bold]Keep which?[/bold]"]
    if merge_available:
        if merge_conflicts == 0:
            merge_line = (
                f"  (m)erge   -> accept LCS-merged result over {canonical_name} (clean, no markers)"
            )
        else:
            merge_line = (
                f"  (m)erge   -> accept LCS-merged result over {canonical_name} "
                f"(contains {merge_conflicts} <<<<<<< region"
                f"{'s' if merge_conflicts != 1 else ''}; resolve in editor after)"
            )
        lines.append(merge_line)
    lines.append(local_line)
    lines.append(remote_line)
    lines.append(
        "  (s)kip    -> leave both files on disk; run `mm resolve` later or delete manually"
    )
    lines.append("  (a)bort   -> stop reviewing; exit")
    return "\n".join(lines)


def render_banner(
    side: Side,
    path: str,
    peer_name: str | None,
    *,
    ambiguous_count: int = 0,
) -> Text:
    """Return a styled Rich Text banner for one side of the conflict.

    The banner is one line: a colored gutter glyph, a side label, and the
    bare filename, plus an attribution suffix on the remote side.

    ``path`` and ``peer_name`` are peer-controlled bytes. This helper wraps
    them through :func:`safe_text` to strip terminal escapes -- a caller
    passing untrusted bytes directly cannot leak OSC 52 / CSI / DCS to the
    real terminal.

    ``ambiguous_count`` is non-zero only when the device-id-prefix lookup
    found multiple peer matches; the banner annotates the prompt with
    ``(ambiguous -- N peers match this prefix)`` so the user sees the
    fleet-config issue at the choice they're about to make.
    """
    color = "red" if side == "local" else "green"
    label = side.upper()
    text = Text()
    # Gutter glyph + label + path. Path goes through safe_text BEFORE
    # appending so any escape sequences are stripped at the renderable
    # boundary (Text() alone would pass them through to the terminal).
    text.append(f"▌ {label} ", style=f"bold {color}")
    text.append_text(safe_text(path, style=color))
    if side == "remote":
        if ambiguous_count > 0:
            text.append(
                f" (ambiguous -- {ambiguous_count} peers match this prefix)",
                style="yellow",
            )
        elif peer_name is None:
            text.append(" (unknown peer)", style="dim")
        else:
            text.append(" (from ", style="dim")
            text.append_text(safe_text(peer_name, style="dim"))
            text.append(")", style="dim")
    return text


def count_divergent_lines(diff: list[str]) -> tuple[int, int, int]:
    """Count ``-`` / ``+`` lines in a unified diff, excluding the headers.

    Returns ``(M, N, K)`` where:
      * ``M`` = removed-or-replaced lines (``-`` lines, excluding the
        ``---`` file-header line)
      * ``N`` = added-or-replaced lines (``+`` lines, excluding the
        ``+++`` file-header line)
      * ``K`` = ``M + N``

    Note on semantics: a 1-line replacement (``-old`` then ``+new``) shows
    as ``M=1, N=1, K=2``. The summary copy in cli.py says
    "removed-or-replaced" / "added-or-replaced" precisely so this isn't
    misread as two independent edits.
    """
    m = 0
    n = 0
    for line in diff:
        if line.startswith("---"):
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("-"):
            m += 1
        elif line.startswith("+"):
            n += 1
    return (m, n, m + n)
