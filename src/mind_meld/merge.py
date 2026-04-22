"""Merge logic for Mind Meld.

On pull, certain files are merged instead of overwritten to preserve
entries appended on different machines:

  .jsonl files — set-union of lines, sorted by 'ts' field if present.
  MEMORY.md    — line-union of the index file (preserves entries from all machines).
"""

from __future__ import annotations

import json
import os


def should_merge(rel_path: str) -> bool:
    """Check if a file should use merge-on-pull instead of overwrite.

    Returns True for .jsonl files and MEMORY.md index files.
    """
    if rel_path.endswith(".jsonl"):
        return True
    # MEMORY.md is the Claude Code memory index — line-oriented, safe to merge
    if os.path.basename(rel_path) == "MEMORY.md":
        return True
    return False


def merge_file(rel_path: str, local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Dispatch to the appropriate merge strategy based on file type."""
    if rel_path.endswith(".jsonl"):
        return merge_jsonl(local_bytes, remote_bytes)
    if os.path.basename(rel_path) == "MEMORY.md":
        return merge_lines(local_bytes, remote_bytes)
    return remote_bytes  # fallback: overwrite


def merge_jsonl(local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Merge two JSONL files by taking the union of their lines.

    Dedup: byte-exact after whitespace strip.
    Sort: by 'ts' field if present, then lexicographic for non-JSON lines.
    """
    local_lines = _split_lines(local_bytes)
    remote_lines = _split_lines(remote_bytes)

    # Union by content (set dedup)
    merged = set(local_lines) | set(remote_lines)

    # Sort: timestamped lines first (by ts), then non-timestamped lexicographically
    timestamped = []
    non_timestamped = []
    for line in merged:
        ts = _extract_ts(line)
        if ts is not None:
            timestamped.append((ts, line))
        else:
            non_timestamped.append(line)

    timestamped.sort(key=lambda x: x[0])
    non_timestamped.sort()

    result_lines = [line for _, line in timestamped] + non_timestamped
    return "\n".join(result_lines).encode("utf-8") + b"\n" if result_lines else b""


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

    return "\n".join(result_lines).encode("utf-8") + b"\n" if result_lines else b""


def _split_lines(data: bytes) -> list[str]:
    """Split bytes into non-empty stripped lines."""
    if not data:
        return []
    text = data.decode("utf-8", errors="replace")
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _extract_ts(line: str) -> str | None:
    """Extract 'ts' field from a JSON line. Returns None if not parseable."""
    try:
        obj = json.loads(line)
        if isinstance(obj, dict) and "ts" in obj:
            return obj["ts"]
    except (json.JSONDecodeError, ValueError):
        pass
    return None
