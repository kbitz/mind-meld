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
) -> list[Path]:
    """Write .memsync-log.md to each affected project directory.

    Groups changes by project and writes one log file per project.
    Returns list of log files written.
    """
    claude_path = Path(claude_dir).expanduser().resolve()
    projects_dir = claude_path / "projects"
    if not projects_dir.exists():
        return []

    # Group all changes by project directory
    # rel_path format: "projects/{project-name}/memory/file.md"
    project_changes: dict[str, dict[str, list[str]]] = {}

    for rel_path in new_files:
        project = _extract_project(rel_path)
        if project:
            project_changes.setdefault(project, {"new": [], "modified": [], "deleted": []})
            project_changes[project]["new"].append(_friendly_name(rel_path))

    for rel_path in modified_files:
        project = _extract_project(rel_path)
        if project:
            project_changes.setdefault(project, {"new": [], "modified": [], "deleted": []})
            project_changes[project]["modified"].append(_friendly_name(rel_path))

    for rel_path in deleted_files:
        project = _extract_project(rel_path)
        if project:
            project_changes.setdefault(project, {"new": [], "modified": [], "deleted": []})
            project_changes[project]["deleted"].append(_friendly_name(rel_path))

    # Write one log per project
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
