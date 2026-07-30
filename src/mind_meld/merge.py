"""Merge logic for Mind Meld.

On pull, certain files are merged instead of overwritten to preserve
entries appended on different machines:

  .jsonl files — set-union of lines, sorted by 'ts' field if present.
  MEMORY.md    — line-union of the index file (preserves entries from all machines).

For the residual conflict path (files that should_merge() rejects -- prose
memory entry files, etc.) ``lcs_merge`` offers a 3-way merge using
LCS(local, remote) as a synthetic ancestor so additive edits on either
side land cleanly. Driven by the (m)erge prompt option in
``_resolve_interactive_loop`` and ``_prompt_conflict_choice`` -- never
applied silently. See CLAUDE.md "Conflict-prompt UX" for the user-facing
contract.
"""

from __future__ import annotations

import difflib
import json
import os
from collections.abc import Callable


# Merge dispatch (single source of truth for "is this file mergeable?"):
#
#   rel_path ──► endswith(".jsonl")?        ─── yes ──► merge_jsonl
#                    │
#                    no
#                    ▼
#                basename == "MEMORY.md"?   ─── yes ──► merge_lines
#                    │
#                    no
#                    ▼
#                  None  (caller overwrites with remote_bytes)
#
# `.jsonl` is checked first: a file that happened to be literally named
# `MEMORY.md.jsonl` is JSONL-merged, not line-union merged. The check
# order IS the precedence contract.
def _merge_strategy(rel_path: str) -> Callable[[bytes, bytes], bytes] | None:
    """Return the merge function for `rel_path`, or None if the file should be overwritten."""
    if rel_path.endswith(".jsonl"):
        return merge_jsonl
    if os.path.basename(rel_path) == "MEMORY.md":
        return merge_lines
    return None


def should_merge(rel_path: str) -> bool:
    """Check if a file should use merge-on-pull instead of overwrite.

    Returns True for .jsonl files and MEMORY.md index files.
    """
    return _merge_strategy(rel_path) is not None


def merge_file(rel_path: str, local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Dispatch to the appropriate merge strategy based on file type."""
    strategy = _merge_strategy(rel_path)
    if strategy is None:
        return remote_bytes  # fallback: overwrite
    return strategy(local_bytes, remote_bytes)


def merge_jsonl(local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Merge two JSONL files by taking the union of their lines.

    Dedup: byte-exact after whitespace strip.
    Sort: by 'ts' field if present, then lexicographic for non-JSON lines.

    INVARIANT (load-bearing): the sort key MUST break ties on full line
    content, not on `ts` alone. The line collection is built by iterating
    a `set`, whose iteration order is hash-randomized across Python
    processes. A `ts`-only sort key leaves tied lines in set-iteration
    order, so two consecutive `mm pull` invocations on identical inputs
    produce different bytes — the no-op suppression in `_apply_merge`
    fails (`merged != local_bytes`), the file is rewritten, and the
    "merged" outcome fires on every pull forever. Pinned by
    `test_jsonl_tied_ts_deterministic_across_hash_seeds` in test_merge.py.
    """
    local_lines = _split_lines(local_bytes)
    remote_lines = _split_lines(remote_bytes)

    # Union by content (set dedup)
    merged = set(local_lines) | set(remote_lines)

    # Sort: timestamped lines first (by ts, then full line content),
    # then non-timestamped lexicographically.
    timestamped = []
    non_timestamped = []
    for line in merged:
        ts = _extract_ts(line)
        if ts is not None:
            timestamped.append((ts, line))
        else:
            non_timestamped.append(line)

    timestamped.sort(key=lambda x: (x[0], x[1]))
    non_timestamped.sort()

    result_lines = [line for _, line in timestamped] + non_timestamped
    return _join_lines(result_lines)


def merge_lines(local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Merge two line-oriented files by taking the union of their lines.

    Used for MEMORY.md and similar index files where each line is an
    independent entry. Dedup is byte-exact after whitespace strip.
    Lines are sorted lexicographically to produce deterministic output.
    """
    local_lines = _split_lines(local_bytes)
    remote_lines = _split_lines(remote_bytes)

    merged = set(local_lines) | set(remote_lines)
    result_lines = sorted(merged)

    return _join_lines(result_lines)


def _split_lines(data: bytes) -> list[str]:
    """Split bytes into non-empty stripped lines."""
    if not data:
        return []
    text = data.decode("utf-8", errors="replace")
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _join_lines(lines: list[str]) -> bytes:
    """Serialize a list of lines to UTF-8 bytes with a trailing newline.

    Returns b"" (NOT b"\\n") for an empty list so that an empty merge result
    does not round-trip into a single blank line on the next merge.
    """
    if not lines:
        return b""
    return "\n".join(lines).encode("utf-8") + b"\n"


def _extract_ts(line: str) -> str | None:
    """Extract 'ts' field from a JSON line. Returns None if not parseable."""
    try:
        obj = json.loads(line)
        if isinstance(obj, dict) and "ts" in obj:
            return obj["ts"]
    except (json.JSONDecodeError, ValueError):
        pass
    return None


# LCS-as-synthetic-base 3-way merge for the residual conflict path.
#
# Without a stored last-synced hash (the deferred Future TODO), we can't
# do a true 3-way merge. The trick: SequenceMatcher.get_opcodes() on
# (local, remote) implicitly treats the longest common subsequence as
# the shared ancestor. "equal" runs are kept; one-sided "insert" /
# "delete" are kept (lossless additive); "replace" runs become git-style
# conflict markers that fall through to user resolution.
#
# This is conservative enough that the (m)erge prompt option can offer
# the result for user confirmation -- the only silent-data-loss vector
# would be a wrong "equal" alignment on pathological inputs (lots of
# repeated short lines). Memory/JSONL/markdown content sees mostly-
# unique lines, so misalignment is rare and visible (markers).
_CONFLICT_OPEN = "<<<<<<< local\n"
_CONFLICT_SEP = "=======\n"
_CONFLICT_CLOSE = ">>>>>>> remote\n"


def lcs_merge(local_bytes: bytes, remote_bytes: bytes) -> tuple[bytes, int]:
    """Three-way merge with LCS(local, remote) as synthetic ancestor.

    Returns ``(merged_bytes, conflict_count)``. ``conflict_count == 0``
    means the merge produced no ``<<<<<<<`` markers and is safe to
    accept; ``> 0`` means at least one region was edited differently on
    each side and the merged result contains git-style conflict markers
    the user must resolve manually.

    Binary inputs (NUL byte present) are detected and skipped:
    ``conflict_count`` is set to ``-1`` to signal "merge not attempted"
    so callers can suppress the (m) option entirely. Returned bytes in
    the binary case are ``b""``.

    Lines are split via ``splitlines()`` (no keepends) so a trailing-
    newline variation between local and remote does not trip the LCS
    into a spurious ``replace`` on the only line of a file. The merged
    output joins lines with ``\\n`` and re-attaches a final ``\\n`` if
    either input had one.
    """
    # Two-stage binary detection. NUL byte is the cheap fast-path (catches
    # most binaries, UTF-16 with high-byte zeros, etc). Strict UTF-8 decode
    # is the load-bearing check: a 7-bit-clean or NUL-free non-text payload
    # (some packed binary, mojibake, UTF-16-LE without BOM where high bytes
    # happen non-zero) would otherwise pass the NUL check, get decoded with
    # errors="replace", and round-trip through utf-8 as a lossy "merge"
    # that silently overwrites canonical with corrupted bytes on (m)
    # accept. Strict decode rejects those paths up front.
    if b"\x00" in local_bytes or b"\x00" in remote_bytes:
        return b"", -1
    try:
        local_text = local_bytes.decode("utf-8")
        remote_text = remote_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return b"", -1

    # Split WITHOUT keepends so trailing-newline variations don't trip
    # the LCS into a spurious "replace" on the only line of a file.
    # Re-attach a uniform "\n" terminator on output; the file's final
    # newline tracks whichever input had one.
    local_lines = local_text.splitlines()
    remote_lines = remote_text.splitlines()

    matcher = difflib.SequenceMatcher(a=local_lines, b=remote_lines, autojunk=False)
    out: list[str] = []
    conflicts = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(local_lines[i1:i2])
        elif tag == "delete":
            # Lines in local but not remote. Without a real base we can't
            # tell apart "we added these" from "remote removed these";
            # keep them either way (lossless additive interpretation).
            out.extend(local_lines[i1:i2])
        elif tag == "insert":
            # Symmetric: lines in remote but not local -> kept as
            # "peer added" under the same lossless rule.
            out.extend(remote_lines[j1:j2])
        elif tag == "replace":
            # Both sides differ on this region. Conservative: emit
            # git-style conflict markers and let the user resolve.
            conflicts += 1
            out.append(_CONFLICT_OPEN.rstrip("\n"))
            out.extend(local_lines[i1:i2])
            out.append(_CONFLICT_SEP.rstrip("\n"))
            out.extend(remote_lines[j1:j2])
            out.append(_CONFLICT_CLOSE.rstrip("\n"))

    if not out:
        return b"", conflicts

    body = "\n".join(out)
    # Preserve trailing-newline behavior: if either input ended with
    # "\n" the merged file does too.
    if local_text.endswith("\n") or remote_text.endswith("\n"):
        body = body + "\n"
    return body.encode("utf-8"), conflicts


def similarity_ratio(local_bytes: bytes, remote_bytes: bytes) -> float | None:
    """LCS similarity ratio over the SAME line representation ``lcs_merge`` uses.

    CONFLICT-TELEMETRY (temporary): feeds the conflict-decision collector so the
    ``similarity`` feature it records is byte-for-byte the metric a future
    similarity-gated auto-resolver (the deferred Phase 2 classifier) would
    compute. If this drifts from ``lcs_merge``'s representation the collected
    dataset can't validate the classifier's thresholds -- so the binary guard,
    ``splitlines()`` (no keepends), and ``autojunk=False`` MUST match
    ``lcs_merge`` exactly. Not called by any sync path.

    Returns ``SequenceMatcher.ratio()`` in ``[0.0, 1.0]``, or ``None`` for
    binary / non-UTF-8 input (parity with ``lcs_merge``'s ``-1`` sentinel).
    """
    if b"\x00" in local_bytes or b"\x00" in remote_bytes:
        return None
    try:
        local_text = local_bytes.decode("utf-8")
        remote_text = remote_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    local_lines = local_text.splitlines()
    remote_lines = remote_text.splitlines()
    return difflib.SequenceMatcher(a=local_lines, b=remote_lines, autojunk=False).ratio()
