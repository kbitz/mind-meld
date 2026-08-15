"""Mtime primitives shared by the pull/apply path and the conflict resolver.

Extracted from ``cli.py`` in Track 16A. Leaf module: imports only ``manifest``
(itself a leaf), never ``cli`` or ``resolveflow``.

**Why a leaf and not part of ``resolveflow`` (load-bearing).** These three have
callers on BOTH sides of the Track 16A cut:

  * ``_restore_mtime_best_effort`` — ``cli._apply_write`` / ``_apply_merge`` /
    ``_apply_conflict`` (the pull/apply path, stays in ``cli``)
  * ``_stat_mtime_btime`` — ``cli._prompt_conflict_choice`` (inline pull prompt)
    AND ``resolveflow._resolve_interactive_loop`` (the ``mm resolve`` prompt)
  * ``_bump_canonical_mtime_post_resolve`` — ``cli._drain_inline_bumps``
    (Track 12A's deferred bump) AND ``resolveflow``'s (l)ocal / (p)romote
    branches

Putting them in ``resolveflow`` would be acyclic but inverted: the early
pull/apply infrastructure would import a ~900-line interactive-resolution
module just to stat a file. A leaf keeps the dependency arrow pointing the way
the call graph actually runs.

All three share ``_MTIME_RESTORE_MAX_SKEW_SECONDS``, which is why the constant
moves with them rather than staying behind.

See ``docs/invariants/conflicts.md`` (the (l)ocal-must-bump rule) and
``docs/invariants/sync.md`` (mtime restore + future-clamp) before editing.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from mind_meld.manifest import mtime_from_manifest

_MTIME_RESTORE_MAX_SKEW_SECONDS = 60.0


def _restore_mtime_best_effort(path: Path, mtime_iso: str | None) -> None:
    """Stamp `path` with mtime/atime from a manifest ISO-8601 timestamp.

    Best-effort: silently no-ops on None/empty input, an unparseable or
    wrong-typed value, or any filesystem error. Mtime is metadata; the
    load-bearing contract is that the file's bytes match the remote \u2014
    `os.utime` failing on iCloud-restored placeholders or networked
    filesystems must not abort the pull.

    Future-clamp (load-bearing). Cap the applied mtime at
    ``now + _MTIME_RESTORE_MAX_SKEW_SECONDS`` so a peer with a bad clock
    (or a passphrase-holding attacker minting a manifest dated in 2099)
    can't poison this device's local mtime into a permanent
    `local_mtime > remote_mtime` skip at `_apply_incoming_file`'s mtime
    gate. Without this clamp, one bad pull would silently lock the
    victim out of all future legitimate updates to that path.

    Why we restore at all: pre-fix, every pulled file landed with
    `st_mtime = now-of-pull`, breaking any downstream consumer that uses
    mtime to order content (e.g. gstack skill preambles' `ls -t` recency
    scan over `~/.gstack/projects/*/checkpoints/`). The mtime mm captures
    on push is the original source mtime; restoring it on pull preserves
    cross-machine ordering even when locally-authored and remotely-pulled
    files are interleaved on the same disk.
    """
    if not mtime_iso:
        return
    try:
        ts = mtime_from_manifest(mtime_iso).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return
    ts = min(ts, time.time() + _MTIME_RESTORE_MAX_SKEW_SECONDS)
    try:
        os.utime(path, (ts, ts))
    except (OSError, OverflowError):
        return


def _stat_mtime_btime(path: Path) -> tuple[float | None, float | None]:
    """Best-effort ``(mtime, birthtime)`` epoch floats for the conflict-prompt
    timestamp display. Shared by both prompt sites.

    Returns ``(None, None)`` on any stat failure (iCloud placeholder, race,
    permission) so the renderer shows "unknown" rather than crashing the
    prompt. ``st_birthtime`` is present on macOS/APFS; ``getattr`` guards
    against filesystems that lack it (returns None there). The caller decides
    what the birthtime MEANS per side -- genuine "created" for the local
    file, "pulled" (local iCloud-drop time) for a restored remote sidecar.
    """
    try:
        st = path.stat()
    except OSError:
        return None, None
    return st.st_mtime, getattr(st, "st_birthtime", None)


def _bump_canonical_mtime_post_resolve(canonical: Path, peer_mtime: float) -> None:
    """Stamp ``canonical`` with mtime strictly greater than ``peer_mtime``.

    Called from ``_resolve_interactive_loop`` after the user picks (l)ocal
    so the "I picked local" decision propagates across the fleet on the
    next push. Without this bump, canonical's mtime stays at whatever
    ``_apply_incoming_file``'s mtime gate just classified as <= peer's,
    and the next pull from the same peer re-conflicts because the dedup
    signal (existing sidecar) was just deleted by the resolve. The user
    ends up in a resolve -> pull -> resolve -> pull loop indefinitely.

    Future-clamp symmetry: cap at ``now + _MTIME_RESTORE_MAX_SKEW_SECONDS``
    so downstream peers don't have to clamp our pushed mtime. The cap edge
    case (peer's mtime already at the max clamp) leaves canonical's mtime
    exactly equal to peer's; the cycle persists for one more pull but is
    self-correcting on peer's next legitimate push.

    Best-effort. ``os.utime`` failure on iCloud-restored placeholders or
    networked filesystems must not fail the resolve action itself --
    bumping is a propagation hint, not a correctness gate.
    """
    now = time.time()
    target = max(now, peer_mtime + 1.0)
    target = min(target, now + _MTIME_RESTORE_MAX_SKEW_SECONDS)
    try:
        os.utime(canonical, (target, target))
    except (OSError, OverflowError):
        return


__all__ = [
    "_MTIME_RESTORE_MAX_SKEW_SECONDS",
    "_bump_canonical_mtime_post_resolve",
    "_restore_mtime_best_effort",
    "_stat_mtime_btime",
]
