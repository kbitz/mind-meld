"""JSONL merge logic for MemSync.

On pull, .jsonl files are merged instead of overwritten to preserve
entries appended on different machines. Uses byte-exact line dedup
with timestamp-based sorting.
"""

from __future__ import annotations

import json


def should_merge(rel_path: str) -> bool:
    """Check if a file should use merge-on-pull instead of overwrite."""
    return rel_path.endswith(".jsonl")


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
