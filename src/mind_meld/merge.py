"""Merge logic for Mind Meld.

On pull, certain files are merged instead of overwritten to preserve
entries appended on different machines:

  .jsonl files — set-union of unique normalized lines. Records with a
    string ``ts`` sort first by that string then the full original line;
    every other original line follows in lexical order. No date parsing
    or numeric-epoch interpretation.
  MEMORY.md    — line-union of the index file, keeping at most one line
    per relative ``.md`` link (preferring the local side's lexicographically
    first line) and exact-dedup unioning lines without one, sorted
    lexicographically. MEMORY.md does not consult ``ts``. After pull applies
    and deletions finish, ``drop_missing_links`` removes lines whose linked
    file is absent.

For the residual conflict path (files that should_merge() rejects -- prose
memory entry files, etc.) ``lcs_merge`` offers a 3-way merge using
LCS(local, remote) as a synthetic ancestor so additive edits on either
side land cleanly. Driven by the (m)erge prompt option in
``_resolve_interactive_loop`` and ``_prompt_conflict_choice`` -- never
applied silently. See docs/invariants/conflicts.md "Conflict-prompt UX" for the user-facing
contract.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections.abc import Callable, Iterator


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


def is_line_index(rel_path: str) -> bool:
    """Return True if `rel_path` uses the line-index merge (`merge_lines`)."""
    return _merge_strategy(rel_path) is merge_lines


def merge_file(rel_path: str, local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Dispatch to the appropriate merge strategy based on file type."""
    strategy = _merge_strategy(rel_path)
    if strategy is None:
        return remote_bytes  # fallback: overwrite
    return strategy(local_bytes, remote_bytes)


def merge_jsonl(local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Merge two JSONL files by taking the union of their lines.

    Dedup: byte-exact after whitespace strip (unique normalized lines).
    Sort:
      1. Records whose decoded JSON is an object with a string ``ts``
         (including the empty string), ordered by ``(ts, original line)``.
      2. Every other original normalized line after them, ordered
         lexically by the whole line.

    Non-string ``ts`` values (numbers, bools, null, arrays, objects),
    missing ``ts``, non-object JSON, malformed text, and decoder
    ``ValueError`` / ``RecursionError`` all keep the original normalized
    line in the fallback bucket. Lines are never reserialized. There is
    no chronology or numeric-epoch promise.

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

    # String-ts records first (by ts, then full line content); every
    # other original normalized line follows lexicographically.
    string_timestamped = []
    fallback_lines = []
    for line in merged:
        ts = _extract_ts(line)
        if ts is not None:
            string_timestamped.append((ts, line))
        else:
            fallback_lines.append(line)

    string_timestamped.sort(key=lambda x: (x[0], x[1]))
    fallback_lines.sort()

    result_lines = [line for _, line in string_timestamped] + fallback_lines
    return _join_lines(result_lines)


_INDEX_LINK_RE = re.compile(r"\[[^\]]*\]\(")


def _index_link_bodies(line: str) -> Iterator[str]:
    """Read whole destinations, never treating their inner syntax as another link."""
    offset = 0
    while match := _INDEX_LINK_RE.search(line, offset):
        start = match.end()
        depth = 1
        angle = False
        quote = ""
        for pos in range(start, len(line)):
            char = line[pos]
            if quote:
                if char == quote:
                    quote = ""
            elif angle:
                if char == ">":
                    angle = False
            elif char == "<" and not line[start:pos].strip():
                angle = True
            elif char in ('"', "'") and pos > start and line[pos - 1].isspace():
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    yield line[start:pos]
                    offset = pos + 1
                    break
        else:
            # An unterminated destination is uncertain: retain the line rather
            # than interpreting an embedded link as a missing file to prune.
            return


def index_link(line: str) -> str | None:
    """Return the line's first qualifying relative ``.md`` link target, or None.

    A target qualifies when, after stripping surrounding ``<>``, an optional
    quoted title and any ``#fragment``, it ends in ``.md``, contains no ``:``,
    ``%``, ``?`` or ``\\``, does not start with ``/`` or ``~``, and has no
    empty or ``..`` segment. Returned as a normalized relative POSIX path.
    """
    for body in _index_link_bodies(line):
        raw = body.strip()
        if not raw:
            continue
        if raw.startswith("<"):
            if ">" not in raw:
                continue
            target, rest = raw[1:].split(">", 1)
            rest = rest.strip()
        else:
            parts = raw.split(None, 1)
            target = parts[0]
            rest = parts[1].strip() if len(parts) > 1 else ""
        if rest and not (len(rest) >= 2 and rest[0] in ('"', "'") and rest[-1] == rest[0]):
            continue
        target = target.split("#", 1)[0]
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        if (
            not target.endswith(".md")
            or target.startswith(("/", "~"))
            or any(ch in target for ch in (":", "%", "?", "\\"))
        ):
            continue
        segments = target.split("/")
        if any(seg in ("", "..") for seg in segments):
            continue
        kept_segments = [seg for seg in segments if seg != "."]
        if not kept_segments:
            continue
        normalized = "/".join(kept_segments)
        if normalized.endswith(".md"):
            return normalized
    return None


def merge_lines(local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Merge two line-oriented index files by taking the union of their lines.

    Used for MEMORY.md and similar index files where each line is an
    independent entry. Lines without a qualifying relative ``.md`` link
    are deduplicated byte-for-byte after whitespace strip. Linked lines
    are grouped by ``index_link``: at most one line is kept per linked
    file, preferring the lexicographically smallest of the local side's
    distinct lines for that file, or the smallest remote line when local
    has none. Output lines are sorted lexicographically.
    """
    local_set = set(_split_lines(local_bytes))
    remote_set = set(_split_lines(remote_bytes))

    local_by_target: dict[str, list[str]] = {}
    for line in local_set:
        target = index_link(line)
        if target is not None:
            local_by_target.setdefault(target, []).append(line)

    remote_by_target: dict[str, list[str]] = {}
    for line in remote_set:
        target = index_link(line)
        if target is not None:
            remote_by_target.setdefault(target, []).append(line)

    kept: set[str] = {line for line in (local_set | remote_set) if index_link(line) is None}
    for target in local_by_target.keys() | remote_by_target.keys():
        candidates = local_by_target.get(target) or remote_by_target[target]
        kept.add(min(candidates))

    return _join_lines(sorted(kept))


def drop_missing_links(data: bytes, present: Callable[[str], bool]) -> bytes:
    """Drop lines whose relative ``.md`` link target is not ``present``.

    Returns ``data`` unchanged when every linked line's target is present;
    otherwise serializes the remaining lines in their existing order via
    ``_join_lines`` (``b""`` when none remain).
    """
    lines = _split_lines(data)
    kept: list[str] = []
    dropped = False
    for line in lines:
        target = index_link(line)
        if target is not None and not present(target):
            dropped = True
            continue
        kept.append(line)
    if not dropped:
        return data
    return _join_lines(kept)


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
    """Return a decoded string ``ts`` from a JSON object line, else None.

    Only a JSON object whose ``ts`` value is a Python ``str`` (including
    empty) contributes a timestamp sort key. All other decoded values —
    missing key, null, number, bool, array, object, non-object JSON —
    return None so ``merge_jsonl`` keeps the original normalized line in
    the lexical fallback.

    Decoder failures return None. ``ValueError`` includes
    ``JSONDecodeError`` by inheritance. ``RecursionError`` is a named
    rescue for parser-depth limits on deeply nested payloads; it is not
    a catch-all. Recursion limits are not changed.
    """
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        # JSONDecodeError subclasses ValueError. RecursionError is the
        # CPython decoder-depth failure; keep the original line.
        return None
    if isinstance(obj, dict) and "ts" in obj:
        ts = obj["ts"]
        if isinstance(ts, str):
            return ts
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
