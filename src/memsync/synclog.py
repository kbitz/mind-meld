"""Sync log generation for MemSync.

After pull, writes a .memsync-log.md in each affected project directory
so Claude Code can discover what changed from other machines.

This file is excluded from sync (listed in EXCLUDED patterns).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def write_sync_log(
    claude_dir: str | Path,
    device_name: str,
    device_id: str,
    new_files: list[str],
    modified_files: list[str],
    deleted_files: list[str],
    conflicted_files: list[str] | None = None,
    skipped_files: list[str] | None = None,
) -> list[Path]:
    """Write .memsync-log.md to each affected project directory.

    Groups changes by project and writes one log file per project. Conflicts
    and skips are surfaced so Claude Code can see them alongside plain
    additions and merges — conflicts in particular are things the user
    should resolve (msync conflicts / msync resolve).

    Returns list of log files written.
    """
    claude_path = Path(claude_dir).expanduser().resolve()
    projects_dir = claude_path / "projects"
    if not projects_dir.exists():
        return []

    conflicted_files = conflicted_files or []
    skipped_files = skipped_files or []

    # Group all changes by project directory.
    # rel_path format: "projects/{project-name}/memory/file.md"
    def _new_bucket() -> dict[str, list[str]]:
        return {"new": [], "modified": [], "deleted": [], "conflicted": [], "skipped": []}

    project_changes: dict[str, dict[str, list[str]]] = {}

    def _add(bucket_name: str, rel_paths: list[str]) -> None:
        for rel_path in rel_paths:
            project = _extract_project(rel_path)
            if project:
                project_changes.setdefault(project, _new_bucket())
                project_changes[project][bucket_name].append(_friendly_name(rel_path))

    _add("new", new_files)
    _add("modified", modified_files)
    _add("deleted", deleted_files)
    _add("conflicted", conflicted_files)
    _add("skipped", skipped_files)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    written: list[Path] = []

    for project_name, changes in project_changes.items():
        project_dir = projects_dir / project_name
        if not project_dir.exists():
            continue

        log_path = project_dir / ".memsync-log.md"
        lines = [
            "# MemSync Activity",
            "",
            f"Last pull: {now} from **{device_name}** (`{device_id}`)",
            "",
        ]

        if changes["new"]:
            lines.append("## New from other machine")
            for name in sorted(changes["new"]):
                lines.append(f"- {name}")
            lines.append("")

        if changes["modified"]:
            lines.append("## Updated from other machine")
            for name in sorted(changes["modified"]):
                lines.append(f"- {name}")
            lines.append("")

        if changes["conflicted"]:
            lines.append("## Conflicts (remote differed, local preserved as .sync-conflict-*)")
            for name in sorted(changes["conflicted"]):
                lines.append(f"- {name}")
            lines.append("")
            lines.append("Run `msync conflicts` to review, `msync resolve` to pick a winner.")
            lines.append("")

        if changes["skipped"]:
            lines.append("## Skipped (local was newer)")
            for name in sorted(changes["skipped"]):
                lines.append(f"- {name}")
            lines.append("")

        if changes["deleted"]:
            lines.append("## Removed on other machine")
            for name in sorted(changes["deleted"]):
                lines.append(f"- {name}")
            lines.append("")

        log_path.write_text("\n".join(lines))
        written.append(log_path)

    return written


def _extract_project(rel_path: str) -> str | None:
    """Extract the project directory name from a relative path.

    "projects/-Users-kb-myapp/memory/file.md" → "-Users-kb-myapp"
    """
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[0] == "projects":
        return parts[1]
    return None


def _friendly_name(rel_path: str) -> str:
    """Convert a full relative path to a human-readable name.

    "projects/-Users-kb-myapp/memory/user_role.md" → "memory/user_role.md"
    """
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[0] == "projects":
        return "/".join(parts[2:])
    return rel_path
