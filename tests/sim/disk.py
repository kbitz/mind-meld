"""Opaque disk copies shared by the independent 1.x and v2 harnesses."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .transport import File, Index, owned_path, read_tree


def _rmtree(path: Path) -> None:
    """Remove a fixture copy whose restored directory modes may be read-only."""
    os.chmod(path, 0o700)
    for directory, children, _ in os.walk(path):
        for child in children:
            target = os.path.join(directory, child)
            if not os.path.islink(target):  # chmod would follow it out of the copy.
                os.chmod(target, 0o700)
    shutil.rmtree(path)


@dataclass(frozen=True)
class Snapshot:
    owner: str
    root: Path
    files: tuple[tuple[str, File], ...]
    directories: tuple[tuple[str, int], ...]  # Relative name and mode.
    root_mode: int
    index: Index
    hardware_id: str
    mm_id: str


def copy_snapshot(root: Path, snapshot: Snapshot, destination: Path) -> Path | None:
    """Install the disk; returns a backup to discard once the index is committed."""
    # Validate all source/destination names before any destructive action.
    owned_path(root, destination.relative_to(root).as_posix())
    for relative, _ in snapshot.files:
        owned_path(destination, relative)
    for relative, _ in snapshot.directories:
        owned_path(destination, relative)
    if destination.exists():
        read_tree(destination)
    stage = root / f".mm-tmp-copy-{destination.name}"
    backup = root / f".mm-tmp-backup-{destination.name}"
    for path in (stage, backup):
        if path.exists() or path.is_symlink():
            raise ValueError(
                f"Copy staging occupied at {path}: "
                "remove fixture staging before retrying; see README#lifecycle"
            )
    try:
        stage.mkdir()
        for relative, _ in snapshot.directories:
            owned_path(stage, relative).mkdir(parents=True, exist_ok=True)
        for relative, file in snapshot.files:
            target = owned_path(stage, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.data)
            os.chmod(target, file.mode)
            os.utime(target, ns=(file.mtime_ns, file.mtime_ns))
        # Children before parents, so a read-only parent is applied last.
        for relative, mode in reversed(snapshot.directories):
            os.chmod(owned_path(stage, relative), mode)
        os.chmod(stage, snapshot.root_mode)
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except OSError as error:
            if backup.exists():
                try:
                    os.replace(backup, destination)
                except OSError as rollback:
                    error.add_note(
                        f"Rollback also failed: {rollback}; saved disk remains at {backup}"
                    )
            raise
    except OSError as error:
        error.add_note(
            f"Copying device {snapshot.owner} to {destination}; "
            "repair I/O and retry; see README#lifecycle"
        )
        if stage.exists():
            try:
                _rmtree(stage)
            except OSError as cleanup:
                error.add_note(f"Staging cleanup failed: {cleanup}; remove {stage} before retry")
        raise
    return backup if backup.exists() else None


def discard_backup(backup: Path | None) -> None:
    if backup is None:
        return
    try:
        _rmtree(backup)
    except OSError as error:
        error.add_note(
            f"Disk and index are committed, but backup cleanup failed; "
            f"remove {backup} before the next copy; see README#lifecycle"
        )
        raise
