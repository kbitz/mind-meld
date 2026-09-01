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

from datetime import datetime
from typing import Literal, Sequence

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
    promote_available: bool = False,
    newer_available: bool = False,
    newer_desc: str = "",
    local_only_lines: int | None = None,
    remote_only_lines: int | None = None,
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

    ``promote_available`` enables the ``(p)romote`` option line: keep
    BOTH files by renaming the conflict sidecar to its own first-class
    filename. Only the ``mm resolve`` walk passes ``promote_available=True``
    -- the inline pull-time prompt does not (the sidecar is not on disk
    yet at that site).

    ``newer_available`` enables the ``(n)ewer`` option line: keep whichever
    side has the greater mtime. Only ``mm resolve`` passes
    ``newer_available=True`` -- the inline pull-time prompt does NOT, because
    ``_apply_incoming_file`` already skips before prompting when the local
    file is newer, so "newer" there is always the remote side (= ``(r)``).
    ``newer_desc`` is the caller-composed annotation (e.g. ``REMOTE, 2d
    newer``) shown in parentheses so the user sees what the key will do.
    The caller suppresses ``(n)ewer`` when either mtime is unreadable.

    ``local_only_lines`` / ``remote_only_lines`` are the count of unified-
    diff lines unique to each side (semantic, mode-corrected -- the caller
    maps the raw diff ``m``/``n`` from ``count_divergent_lines`` to these
    based on ``mode``). When BOTH are non-None, each destructive choice
    line gets a ``(drops N ...)`` suffix so the user sees the consequence
    inline: ``(l)ocal`` drops the peer-only lines (end state = local
    bytes); ``(r)emote`` drops the local-only lines. Pass ``None`` (the
    default) for binary-content prompts where the diff was empty.
    """
    show_counts = local_only_lines is not None and remote_only_lines is not None
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
    if show_counts:
        # (l)ocal end-state = local bytes; the peer-unique lines are dropped.
        # (r)emote end-state = remote bytes; the local-unique lines are dropped.
        local_line += "  " + _drop_suffix(remote_only_lines or 0, side="peer")
        remote_line += "  " + _drop_suffix(local_only_lines or 0, side="your")
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
    if newer_available:
        suffix = f" ({newer_desc})" if newer_desc else ""
        lines.append(f"  (n)ewer   -> keep the more recently modified file{suffix}")
    if promote_available:
        lines.append(
            f"  (p)romote -> keep BOTH: give {conflict_name} its own filename, "
            f"keep {canonical_name}"
        )
    lines.append(
        "  (s)kip    -> leave both files on disk; run `mm resolve` later or delete manually"
    )
    lines.append("  (a)bort   -> stop reviewing; exit")
    return "\n".join(lines)


def _drop_suffix(count: int, *, side: Literal["peer", "your"]) -> str:
    """Render the ``(drops N ...)`` annotation appended to (l)ocal / (r)emote."""
    plural = "" if count == 1 else "s"
    if side == "peer":
        return f"[dim](drops {count} peer line{plural})[/dim]"
    return f"[dim](drops {count} of your line{plural})[/dim]"


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


def merge_has_line_structure(local_lines: Sequence[str], remote_lines: Sequence[str]) -> bool:
    """True when both sides have enough line structure for ``lcs_merge`` to help.

    ``lcs_merge`` is line-based: it builds a synthetic ancestor from
    ``LCS(local, remote)`` and can only produce a useful result when the two
    sides share lines to align on. When EITHER side is a single line, there is
    nothing to align — the only possible output is one ``<<<<<<<`` region
    wrapping both versions whole, which is never a valid file of that type.
    Offering ``(m)erge`` there is strictly worse than ``(l)ocal`` / ``(r)emote``:
    it costs the user a manual editor round-trip to reach a state one keystroke
    would have given them.

    This is the shape of the fleet's most common conflict: gstack writes
    ``decisions.active.json`` as ONE minified ``JSON.stringify`` line, so every
    collision reported ``merge_available=True, merge_conflicts=1`` on
    ``local_lines=1, remote_lines=1``. All nine decisions in the 2026-08
    ``conflict-decisions.jsonl`` sample had that shape.

    A zero-line (empty) side is suppressed too: ``lcs_merge(b"", remote)``
    returns remote as a "clean" merge, which is just ``(r)emote`` wearing a
    disguise — see the ``local_read_failed`` guard in ``_prompt_conflict_choice``
    for the same reasoning applied to an unreadable local.

    Callers AND this predicate together decide ``merge_available``; suppression
    routes through the existing binary-content path, so the "typed ``m`` while
    unavailable degrades to keep-both" contract is reused verbatim rather than
    given a new branch.
    """
    return len(local_lines) > 1 and len(remote_lines) > 1


def render_capped_diff(diff: Sequence[str], *, cap: int) -> list[Text]:
    """Render a bounded unified diff through the terminal-safety boundary.

    ``diff`` is the raw sequence produced by a caller's ``difflib.unified_diff``
    invocation. The cap counts those raw entries, exactly as the two prompt
    sites did before this helper existed: inline pull passes 60 and ``mm
    resolve`` passes 80. This helper does not construct a diff, map its sides,
    or make a choice; those are site-specific consent semantics.

    Every diff entry is peer-controlled content and therefore passes through
    :func:`safe_text` before Rich sees it. Added and removed content lines get
    color, but the ``+++`` and ``---`` headers intentionally remain neutral.
    Empty diffs receive the existing binary-content hint; a capped diff gets
    the exact overflow count from the raw sequence length.
    """
    if not diff:
        return [Text("  (files differ but text diff is empty — likely binary)", style="dim")]

    rendered: list[Text] = []
    for line in diff[:cap]:
        if line.startswith("+") and not line.startswith("+++"):
            rendered.append(safe_text(line, style="green"))
        elif line.startswith("-") and not line.startswith("---"):
            rendered.append(safe_text(line, style="red"))
        else:
            rendered.append(safe_text(line))
    if len(diff) > cap:
        rendered.append(Text(f"  ...({len(diff) - cap} more diff lines)", style="dim"))
    return rendered


def format_ts(ts: float | None) -> str:
    """Format an epoch timestamp as local ``YYYY-MM-DD HH:MM``.

    Returns ``"unknown"`` for None or any unconvertible value (a corrupt
    mtime, an overflowing year). Local time is intentional -- it matches
    what the user sees in Finder and in their shell ``ls -l``.
    """
    if ts is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def format_age_delta(seconds: float) -> str:
    """Render an absolute duration as ``Nd`` / ``Nh`` / ``Nm`` / ``<1m``.

    Extends the ``mm conflicts`` Age convention (``cli.py`` ``{days}d`` /
    ``{hours}h``) with a minutes bucket so a sub-hour gap doesn't collapse
    to a confusing ``0h``. Sign is ignored -- the caller already knows which
    side is newer; this is just the magnitude.
    """
    seconds = abs(seconds)
    days = int(seconds // 86400)
    if days:
        return f"{days}d"
    hours = int(seconds // 3600)
    if hours:
        return f"{hours}h"
    minutes = int(seconds // 60)
    if minutes:
        return f"{minutes}m"
    return "<1m"


def newer_side(
    local_mtime: float | None, remote_mtime: float | None
) -> Literal["local", "remote", "tie", "unknown"]:
    """Classify which side is more recently modified.

    ``"unknown"`` when either mtime is None (unreadable stat / missing
    manifest mtime) -- the caller suppresses the ``(n)ewer`` shortcut in
    that case. ``"tie"`` on an exact match -- the caller re-prompts rather
    than guessing a side.
    """
    if local_mtime is None or remote_mtime is None:
        return "unknown"
    if local_mtime > remote_mtime:
        return "local"
    if remote_mtime > local_mtime:
        return "remote"
    return "tie"


def render_time_line(fields: list[tuple[str, float | None]]) -> Text:
    """Render one dim, indented line of labeled timestamps for a side.

    ``fields`` is a list of ``(label, ts)`` pairs composed by the caller so
    each call site controls which times are semantically real:
      * local side:           ``[("modified", m), ("created", b)]``
      * remote sidecar (resolve): ``[("modified", m), ("pulled", b)]``
        -- the sidecar's birthtime is the local iCloud-drop time, NOT the
        peer's real creation, so it is labeled ``pulled``, never ``created``.
      * remote (inline pull):  ``[("modified", m)]`` -- not on disk, so no
        birthtime exists.

    All values are locally-formatted ASCII date strings via :func:`format_ts`;
    no peer-controlled bytes flow through here (those stay in the banners).
    """
    text = Text("    ")
    text.append(
        "  ·  ".join(f"{label} {format_ts(ts)}" for label, ts in fields),
        style="dim",
    )
    return text


def render_verdict(local_mtime: float | None, remote_mtime: float | None) -> Text | None:
    """Render the ``-> SIDE is newer by N`` recency verdict line.

    Returns None when the verdict can't be computed (either mtime
    unreadable) so the caller simply omits the line. The delta is computed
    BETWEEN THE TWO FILES (not vs wall-clock) so it is deterministic in
    tests and stable across runs. Recency is a heuristic, not correctness --
    the copy says "newer", never "correct".
    """
    side = newer_side(local_mtime, remote_mtime)
    if side == "unknown":
        return None
    text = Text("  ")
    if side == "tie":
        text.append("-> same modified time on both sides", style="yellow")
        return text
    delta = format_age_delta((local_mtime or 0.0) - (remote_mtime or 0.0))
    winner = "LOCAL" if side == "local" else "REMOTE"
    color = "red" if side == "local" else "green"
    text.append(f"-> {winner} is newer by {delta}", style=f"bold {color}")
    return text
