"""Read-only pull predictions over virtual canonical state, in peer order.

Only write and merge advance canonical state. Conflict/skip leave it alone.
Merges have an unknown digest: predicting their bytes would require fetching
blobs. Thus real merges are a subset of predicted merges. This plan never
selects downloads for a real pull and cannot predict blob/decrypt failures.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mind_meld import conflictmtime
from mind_meld.manifest import (
    hash_file,
    mtime_from_manifest,
    mtime_from_path,
    path_has_descendant_symlink,
)
from mind_meld.merge import should_merge


def _has_symlinked_component(
    path: Path, base_path: Path, *, strict: bool = False, source_name: str | None = None
) -> bool:
    """Links below a source root are local routing; a linked root is allowed."""
    return path_has_descendant_symlink(path, base_path, strict=strict, source_name=source_name)


@dataclass
class _LocalState:
    exists: bool = False
    kind: str = "file"
    sha256: str | None = None  # None after a merge means UNKNOWN, never equal.
    mtime: datetime | None = None
    hashed: bool = False
    unreadable: bool = False


@dataclass(frozen=True)
class PullPrediction:
    device_id: str
    device_name: str
    src_name: str
    rel_path: str
    outcome: str
    reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.outcome} ({self.reason})" if self.reason else self.outcome


class PullPlanner:
    def __init__(self):
        self.states: dict[tuple[str, Path], _LocalState] = {}
        self.predictions: list[PullPrediction] = []
        self.now = datetime.now(timezone.utc)

    def _state(self, source: str, path: Path, *, hash_contents: bool = False) -> _LocalState:
        key = source, path
        if key not in self.states:
            try:
                path.stat()
                state = _LocalState(exists=True, kind="directory" if path.is_dir() else "file")
                try:
                    state.mtime = mtime_from_path(path)
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
            except (FileNotFoundError, NotADirectoryError):
                state = _LocalState()
            except OSError:
                state = _LocalState(exists=True, unreadable=True)
            self.states[key] = state
        state = self.states[key]
        if hash_contents and state.exists and state.kind == "file" and not state.hashed:
            state.hashed = True
            try:
                state.sha256 = hash_file(path)
            except OSError:
                state.unreadable = True
        return state

    def predict(
        self,
        device_id: str,
        device_name: str,
        src_name: str,
        rel_path: str,
        remote_info: dict,
        base_path: Path,
    ) -> PullPrediction:
        path = base_path / rel_path
        reason = ""
        try:
            remote_mtime = mtime_from_manifest(remote_info.get("mtime"))
        except (TypeError, ValueError, OverflowError, OSError):
            remote_mtime = None
        if _has_symlinked_component(path, base_path):
            outcome, reason = "skip", "local symlink preserved"
        elif (
            any(
                (state := self._state(src_name, parent)).exists and state.kind != "directory"
                for parent in path.parents
            )
            or self._state(src_name, path).kind == "directory"
        ):
            outcome, reason = "may fail", "file/directory collision"
        else:
            state = self._state(src_name, path, hash_contents=True)
            if not state.exists:
                outcome = "write"
                state.exists, state.hashed = True, True
                state.sha256 = remote_info["sha256"]
                state.mtime = min(
                    remote_mtime or self.now,
                    self.now + timedelta(seconds=conflictmtime._MTIME_RESTORE_MAX_SKEW_SECONDS),
                )
                for parent in path.parents:
                    ancestor = self._state(src_name, parent)
                    if not ancestor.exists:
                        ancestor.exists, ancestor.kind = True, "directory"
            elif state.unreadable:
                outcome, reason = "may fail", "local file unreadable"
            elif state.sha256 is not None and state.sha256 == remote_info.get("sha256"):
                outcome = "unchanged"
            elif should_merge(rel_path):
                outcome = "merge"
                state.sha256, state.mtime = None, self.now
            elif (
                remote_mtime is not None and state.mtime is not None and state.mtime > remote_mtime
            ):
                outcome, reason = "skip", "local newer"
            else:
                outcome = "conflict"
        prediction = PullPrediction(device_id, device_name, src_name, rel_path, outcome, reason)
        self.predictions.append(prediction)
        return prediction


def _predict_pull_outcome(rel_path: str, remote_info: dict, base_path: Path) -> str:
    """Single-file compatibility surface; multi-peer consumers share a planner."""
    return PullPlanner().predict("", "", "", rel_path, remote_info, base_path).outcome


def totals(predictions: list[PullPrediction]) -> str:
    counts = Counter(p.outcome for p in predictions if p.outcome != "unchanged")
    if not counts:
        return "No changes predicted."
    line = (
        f"Would write {counts['write']}, merge up to {counts['merge']}, "
        f"conflict {counts['conflict']}; {counts['skip']} skipped"
    )
    if counts["may fail"]:
        line += f"; {counts['may fail']} may fail"
    return line
