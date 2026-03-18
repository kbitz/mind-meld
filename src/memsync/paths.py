"""Cross-machine path resolution for MemSync.

Claude Code encodes absolute paths into directory names under ~/.claude/projects/.
For example, /Users/kb/code/myapp → ~/.claude/projects/-Users-kb-code-myapp/

When pulling from a device with a different home dir, paths need rewriting.
"""

from __future__ import annotations


def rewrite_path(rel_path: str, path_map: dict[str, str]) -> str:
    """Rewrite a relative path using the path_map.

    Claude Code encodes paths by replacing / with - in the projects/ directory.
    E.g., "projects/-Users-kb-code-myapp/sessions/abc.json"

    path_map maps remote prefixes to local ones:
        {"/Users/kb": "/home/kb"}

    This rewrites the encoded prefix in the path.
    """
    for remote_prefix, local_prefix in path_map.items():
        # Convert path prefixes to Claude Code's encoding format:
        # /Users/kb → -Users-kb
        remote_encoded = remote_prefix.replace("/", "-")
        local_encoded = local_prefix.replace("/", "-")

        if remote_encoded in rel_path:
            rel_path = rel_path.replace(remote_encoded, local_encoded)
    return rel_path


def rewrite_manifest_paths(
    files: dict[str, dict],
    path_map: dict[str, str],
) -> dict[str, dict]:
    """Rewrite all file paths in a manifest's files dict."""
    if not path_map:
        return files
    return {rewrite_path(path, path_map): info for path, info in files.items()}
