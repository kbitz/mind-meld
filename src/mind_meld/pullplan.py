"""Pull rule and read-only predictions over virtual canonical state, in peer order.

The rule (``decide``) is one pure function used by the real apply path and by
the planner. Only write, fast-forward and merge advance canonical state;
conflict, skip and hold leave it alone. Merges have an unknown digest:
predicting their bytes would require fetching blobs. Thus real merges are a
subset of predicted merges. This plan never selects downloads for a real pull
and cannot predict blob/decrypt failures.

``Ledger`` is the in-memory overlay of ``state.db``'s ``peer_base`` and
``seen_version`` tables. It is pure: loading and committing live in ``core``.
"""

from __future__ import annotations

import re
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterable
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

HELD = "held"
SEEN = "seen"
PREFIX_LEN = 16
FROM_MAX = 16
_PREFIX_RE = re.compile(r"[0-9a-f]{16}\Z")


def prefix(sha: str | None) -> str | None:
    """The 16-hex ancestry spelling of a sha256, or None for anything else."""
    if isinstance(sha, str) and len(sha) >= PREFIX_LEN:
        head = sha[:PREFIX_LEN]
        if _PREFIX_RE.match(head):
            return head
    return None


def valid_hash(value: object) -> bool:
    """A tombstone/content hash is a 64-char lowercase hex string; all else is no hash."""
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def parse_from(info: dict | None) -> list[str]:
    """A peer-controlled ``from`` list: valid prefixes only; any bad shape reads as absent."""
    raw = (info or {}).get("from")
    if not isinstance(raw, list) or len(raw) > FROM_MAX:
        return []
    out = []
    for item in raw:
        if not isinstance(item, str) or not _PREFIX_RE.match(item):
            return []
        out.append(item)
    return out


@dataclass
class SeenEntry:
    kind: str
    time: int
    deleted_by: str | None = None


class Ledger:
    """Per-peer last-synced bases and the versions this machine has held or seen.

    ``readable`` False (the tables could not be read) means nothing counts as
    held and nothing is committed: callers fall to today's rule and delete nothing.
    """

    def __init__(
        self,
        bases: dict[tuple[str, str, str], str] | None = None,
        seen: dict[tuple[str, str], list[tuple[str, str, int, str | None]]] | None = None,
        *,
        readable: bool = True,
        untracked: frozenset[str] = frozenset(),
        now_us: int | None = None,
    ):
        self.bases: dict[tuple[str, str, str], str] = dict(bases or {})
        self.seen: dict[tuple[str, str], dict[str, SeenEntry]] = {}
        for key, rows in (seen or {}).items():
            self.seen[key] = {sha: SeenEntry(kind, when, by) for sha, kind, when, by in rows}
        self.readable = readable
        self.untracked = untracked
        self.now_us = now_us if now_us is not None else time.time_ns() // 1000
        self.new_bases: dict[tuple[str, str, str], str] = {}
        self.drop_bases: set[tuple[str, str]] = set()
        self.drop_seen: set[tuple[str, str]] = set()
        self.new_seen: dict[tuple[str, str, str], tuple[str, int, str | None]] = {}
        self.unstamp: set[tuple[str, str]] = set()
        # Files written for settled rows (to flush before the commit) and inline
        # keep-local decisions that settle only when the pull is not aborted.
        self.flush_paths: dict[tuple[str, str], list[Path]] = {}
        self.deferred: list[tuple[str, str, str, str, tuple[str, ...]]] = []

    def copy(self) -> Ledger:
        """A planner's private overlay; real state is never touched."""
        other = Ledger(
            self.bases,
            readable=self.readable,
            untracked=self.untracked,
            now_us=self.now_us,
        )
        other.seen = {
            key: {sha: SeenEntry(e.kind, e.time, e.deleted_by) for sha, e in rows.items()}
            for key, rows in self.seen.items()
        }
        return other

    def flush_deferred(self) -> None:
        for peer, source, path, sha, ancestors in self.deferred:
            self.settle(peer, source, path, sha, hold=False, ancestors=ancestors)
        self.deferred.clear()

    def discard_deferred(self, source: str, path: str) -> None:
        """Cancel keep-local settlements only after the canonical file was removed."""
        self.deferred = [row for row in self.deferred if row[1:3] != (source, path)]

    def discard(self, source: str, path: str) -> None:
        """Drop every uncommitted change for a path whose bytes could not be flushed."""
        for key in [k for k in self.new_bases if k[1:] == (source, path)]:
            del self.new_bases[key]
            self.bases.pop(key, None)
        for key in [k for k in self.new_seen if k[:2] == (source, path)]:
            del self.new_seen[key]
            self.seen.get((source, path), {}).pop(key[2], None)

    def mark_committed(self) -> None:
        self.new_bases.clear()
        self.drop_bases.clear()
        self.drop_seen.clear()
        self.new_seen.clear()
        self.unstamp.clear()
        self.flush_paths.clear()

    @property
    def dirty(self) -> bool:
        return bool(
            self.new_bases or self.drop_bases or self.drop_seen or self.new_seen or self.unstamp
        )

    # reads
    def base(self, peer: str, source: str, path: str) -> str | None:
        return self.bases.get((peer, source, path)) if self.readable else None

    def entries(self, source: str, path: str) -> dict[str, SeenEntry]:
        return self.seen.get((source, path), {}) if self.readable else {}

    def newest_held(self, source: str, path: str) -> str | None:
        held = [(e.time, sha) for sha, e in self.entries(source, path).items() if e.kind == HELD]
        return max(held)[1] if held else None

    def held_newest_first(self, source: str, path: str) -> list[str]:
        held = [(e.time, sha) for sha, e in self.entries(source, path).items() if e.kind == HELD]
        return [sha for _, sha in sorted(held, reverse=True)]

    def newest_stamp(self, source: str, path: str) -> str | None:
        """The ``deleted_by`` of the path's newest row, if that row is stamped."""
        rows = self.entries(source, path)
        if not rows:
            return None
        return max(rows.values(), key=lambda e: e.time).deleted_by

    # writes (in memory; ``core.commit_ledger`` persists the dirty parts)
    def _next_time(self, source: str, path: str) -> int:
        rows = self.seen.get((source, path), {})
        latest = max((e.time for e in rows.values()), default=0)
        return max(self.now_us, latest + 1)

    def _tracked(self, source: str) -> bool:
        return self.readable and source not in self.untracked

    def hold(self, source: str, path: str, sha: str) -> None:
        head = prefix(sha)
        if head is None or not self._tracked(source):
            return
        if self.newest_held(source, path) == head:
            return
        existing = self.seen.get((source, path), {}).get(head)
        entry = SeenEntry(
            HELD, self._next_time(source, path), existing.deleted_by if existing else None
        )
        self.seen.setdefault((source, path), {})[head] = entry
        self.new_seen[(source, path, head)] = (HELD, entry.time, None)

    def see(self, source: str, path: str, sha: str) -> None:
        head = prefix(sha)
        if head is None or not self._tracked(source):
            return
        rows = self.seen.setdefault((source, path), {})
        if head in rows:
            return
        entry = SeenEntry(SEEN, self._next_time(source, path))
        rows[head] = entry
        self.new_seen[(source, path, head)] = (SEEN, entry.time, None)

    def settle(
        self,
        peer: str,
        source: str,
        path: str,
        remote_sha: str,
        *,
        hold: bool = True,
        also_hold: tuple[str, ...] = (),
        ancestors: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Record that this machine and ``peer`` now agree about ``remote_sha``.

        ``ancestors`` are the remote entry's ``from`` prefixes: versions the
        remote was built on. They are seen (never held), so a lagging peer that
        still advertises one of them reads as stale instead of as a rival edit.
        """
        if not self.readable:
            return
        key = (peer, source, path)
        if self.bases.get(key) != remote_sha:
            self.bases[key] = remote_sha
            self.new_bases[key] = remote_sha
        for extra in also_hold:
            self.hold(source, path, extra)
        if hold:
            self.hold(source, path, remote_sha)
        else:
            self.see(source, path, remote_sha)
        for ancestor in ancestors:
            self.see(source, path, ancestor)

    def stamp_deleted(self, source: str, path: str, sha: str, device_id: str) -> None:
        """Mark ``sha`` as the version a followed deletion removed (origin ``device_id``)."""
        head = prefix(sha)
        if head is None or not self._tracked(source):
            return
        self.hold(source, path, sha)
        entry = self.seen[(source, path)][head]
        # settle() may have learned ancestors after holding this version.
        # A followed deletion must remain the newest fact for the mint guard.
        entry.time = self._next_time(source, path)
        entry.deleted_by = device_id
        self.new_seen[(source, path, head)] = (HELD, entry.time, device_id)

    def clear_stamps(self, source: str, path: str) -> None:
        rows = self.entries(source, path)
        if any(e.deleted_by for e in rows.values()):
            for e in rows.values():
                e.deleted_by = None
            self.unstamp.add((source, path))
            for key in [k for k in self.new_seen if k[:2] == (source, path)]:
                kind, when, _ = self.new_seen[key]
                self.new_seen[key] = (kind, when, None)

    def clear_seen(self, source: str, path: str) -> None:
        """Forget absence history only after push accepted a path as left out."""
        if not self._tracked(source):
            return
        slot = (source, path)
        self.seen.pop(slot, None)
        self.drop_seen.add(slot)
        self.unstamp.discard(slot)
        for pending in [k for k in self.new_seen if k[:2] == slot]:
            del self.new_seen[pending]
        self.discard_deferred(source, path)

    def undelete(self, source: str, path: str, peer: str, sha: str, *, restore_base: bool) -> None:
        """Take back a followed deletion that did not happen (the file is still here).

        The stamp goes (otherwise a later hand deletion would read as merely
        followed and never propagate) and, when the bytes are still the peer's
        version, so does the dropped base come back.
        """
        if not self.readable:
            return
        self.drop_bases.discard((source, path))
        self.clear_stamps(source, path)
        if restore_base:
            key = (peer, source, path)
            self.bases[key] = sha
            self.new_bases[key] = sha

    def drop(self, source: str, path: str) -> None:
        """Forget every peer's base for a path (a deletion is about to be followed)."""
        self.drop_many([(source, path)])

    def drop_many(self, paths: Iterable[tuple[str, str]]) -> None:
        """Forget every peer's base for selected paths in one pass over each map."""
        if not self.readable:
            return
        removed = set(paths)
        for key in [k for k in self.bases if k[1:] in removed]:
            del self.bases[key]
        for key in [k for k in self.new_bases if k[1:] in removed]:
            del self.new_bases[key]
        self.drop_bases.update(removed)


@dataclass(frozen=True)
class LocalFile:
    exists: bool = False
    sha: str | None = None  # None with exists=True means unknown (never equal)
    mtime: datetime | None = None


def _remote_is_older(local: LocalFile, remote_mtime: datetime | None) -> bool:
    """Strictly older; an unparseable mtime on either side is never older."""
    try:
        return remote_mtime is not None and local.mtime is not None and remote_mtime < local.mtime
    except TypeError:
        return False


def _remote_is_newer(local: LocalFile, remote_mtime: datetime | None) -> bool:
    """A same-base revert needs positive freshness, not merely 'not older'."""
    try:
        return remote_mtime is not None and local.mtime is not None and remote_mtime > local.mtime
    except TypeError:
        return False


def _held_missing(
    ledger: Ledger,
    peer_id: str,
    source: str,
    path: str,
    remote_sha: str,
    remote_mtime: datetime | None,
    own_lists: bool,
    anchor_ok: bool,
) -> bool:
    """Row 1: the file is gone here, and this machine knowingly removed it."""
    head = prefix(remote_sha)
    entry = ledger.entries(source, path).get(head) if head else None
    if entry is None or entry.deleted_by == peer_id or not anchor_ok:
        return False
    try:
        if remote_mtime is None or remote_mtime.timestamp() * 1_000_000 > entry.time:
            return False
    except (OverflowError, OSError, ValueError):
        return False
    return own_lists or entry.deleted_by is not None


def decide(
    ledger: Ledger | None,
    peer_id: str,
    source: str,
    path: str,
    local: LocalFile,
    remote_sha: str,
    remote_mtime: datetime | None,
    remote_from: list[str],
    *,
    own_lists: bool = False,
    anchor_ok: bool = False,
) -> str:
    """The pull rule for one remote file; one of

    ``write`` (absent here), ``held`` (absent here on purpose), ``matched``,
    ``skip`` (remote equals this peer's base without a fresh successor), ``fast-forward``, ``older``
    (a fast-forward whose remote mtime is older than the local file),
    ``stale`` (a version this machine already moved past), ``fallback``
    (no base and no ancestry: today's rule) and ``both`` (both sides changed).
    """
    if not local.exists:
        if ledger is not None and ledger.readable:
            if _held_missing(
                ledger, peer_id, source, path, remote_sha, remote_mtime, own_lists, anchor_ok
            ):
                return "held"
        return "write"
    if local.sha is not None and local.sha == remote_sha:
        return "matched"
    if ledger is None or not ledger.readable:
        return "fallback"
    base = ledger.base(peer_id, source, path)
    held = ledger.newest_held(source, path)
    local_head = prefix(local.sha)
    if held is not None:
        clean = local_head is not None and local_head == held
    else:
        clean = base is not None and local.sha is not None and local.sha == base
    built_on_local = clean and local_head is not None and local_head in remote_from
    if base is not None and base == remote_sha:
        # The peer can return to exactly its old bytes after building on ours.
        # Keep acknowledged stale cycles silent unless this is a fresh successor.
        if not (built_on_local and _remote_is_newer(local, remote_mtime)):
            return "skip"

    def forward() -> str:
        return "older" if _remote_is_older(local, remote_mtime) else "fast-forward"

    if built_on_local:
        return forward()
    remote_head = prefix(remote_sha)
    if remote_head is not None and remote_head in ledger.entries(source, path):
        return "stale"
    if clean and base is not None and local.sha == base:
        return forward()
    if base is None and not remote_from:
        return "fallback"
    return "both"


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


OwnLookup = Callable[[str, str], "tuple[bool, bool]"]
SidecarHolds = Callable[[str, str, str, str], bool]


class PullPlanner:
    def __init__(
        self,
        ledger: Ledger | None = None,
        own: OwnLookup | None = None,
        sidecar_holds: SidecarHolds | None = None,
    ):
        self.states: dict[tuple[str, Path], _LocalState] = {}
        self.predictions: list[PullPrediction] = []
        self.now = datetime.now(timezone.utc)
        self.ledger = ledger.copy() if ledger is not None else None
        self.own = own
        self.sidecar_holds = sidecar_holds
        # (peer, source, path, reason) for files an active deletion record leaves alone.
        self.keeps: list[tuple[str, str, str, str]] = []

    def _state(self, source: str, path: Path, *, hash_contents: bool = False) -> _LocalState:
        key = source, path
        if key not in self.states:
            try:
                st = path.stat()
                is_dir = stat.S_ISDIR(st.st_mode)
                state = _LocalState(
                    exists=True,
                    kind="directory" if is_dir else "file",
                    unreadable=not is_dir and not stat.S_ISREG(st.st_mode),
                )
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
        if (
            hash_contents
            and state.exists
            and state.kind == "file"
            and not state.hashed
            and not state.unreadable
        ):
            state.hashed = True
            try:
                state.sha256 = hash_file(path)
            except OSError:
                state.unreadable = True
        return state

    def _advance(self, state: _LocalState, path: Path, src_name: str, remote_info, remote_mtime):
        """Virtual canonical state after a write or fast-forward."""
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

    def _decide(self, device_id, src_name, rel_path, state, remote_info, remote_mtime):
        own_lists, anchor_ok = self.own(src_name, rel_path) if self.own else (False, False)
        return decide(
            self.ledger,
            device_id,
            src_name,
            rel_path,
            LocalFile(state.exists, state.sha256, state.mtime),
            remote_info["sha256"],
            remote_mtime,
            parse_from(remote_info),
            own_lists=own_lists,
            anchor_ok=anchor_ok,
        )

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
            if state.exists and state.unreadable:
                outcome, reason = "may fail", "local file unreadable"
            else:
                outcome, reason = self._predict_file(
                    device_id, src_name, rel_path, path, state, remote_info, remote_mtime
                )
        prediction = PullPrediction(device_id, device_name, src_name, rel_path, outcome, reason)
        self.predictions.append(prediction)
        return prediction

    def _settle(self, device_id, src_name, rel_path, remote_info, *, hold=True, also=()):
        if self.ledger is not None:
            self.ledger.settle(
                device_id,
                src_name,
                rel_path,
                remote_info["sha256"],
                hold=hold,
                also_hold=also,
                ancestors=parse_from(remote_info),
            )

    def _predict_file(self, device_id, src_name, rel_path, path, state, remote_info, remote_mtime):
        verdict = self._decide(device_id, src_name, rel_path, state, remote_info, remote_mtime)
        if verdict == "write":
            self._advance(state, path, src_name, remote_info, remote_mtime)
            self._settle(device_id, src_name, rel_path, remote_info)
            return "write", ""
        if verdict == "held":
            return "skip", "removed here; not restored"
        if verdict == "matched":
            self._settle(device_id, src_name, rel_path, remote_info)
            return "unchanged", ""
        if verdict == "skip":
            return "unchanged", ""
        if verdict == "stale":
            return "unchanged", ""
        if verdict == "fast-forward":
            self._advance(state, path, src_name, remote_info, remote_mtime)
            self._settle(device_id, src_name, rel_path, remote_info)
            return "write", "fast-forward"
        if verdict == "older":
            return "older", "peer edit has an older timestamp"
        if verdict == "fallback":
            return self._legacy(device_id, src_name, rel_path, state, remote_info, remote_mtime)
        return self._both(device_id, src_name, rel_path, state, remote_info)

    def _both(self, device_id, src_name, rel_path, state, remote_info):
        local_before = state.sha256
        if should_merge(rel_path):
            state.sha256, state.mtime = None, self.now
            self._settle(
                device_id,
                src_name,
                rel_path,
                remote_info,
                also=(local_before,) if local_before else (),
            )
            return "merge", ""
        return self._conflict(device_id, src_name, rel_path, remote_info)

    def _conflict(self, device_id, src_name, rel_path, remote_info):
        self._settle(device_id, src_name, rel_path, remote_info, hold=False)
        if self.sidecar_holds and self.sidecar_holds(
            src_name, rel_path, device_id, remote_info["sha256"]
        ):
            return "kept", "conflict copy already saved"
        return "conflict", ""

    def _legacy(self, device_id, src_name, rel_path, state, remote_info, remote_mtime):
        """No base and no ancestry: today's rule exactly."""
        if state.sha256 is not None and state.sha256 == remote_info.get("sha256"):
            return "unchanged", ""
        if should_merge(rel_path):
            local_before = state.sha256
            state.sha256, state.mtime = None, self.now
            self._settle(
                device_id,
                src_name,
                rel_path,
                remote_info,
                also=(local_before,) if local_before else (),
            )
            return "merge", ""
        if remote_mtime is not None and state.mtime is not None and state.mtime > remote_mtime:
            return "skip", "local newer"
        return self._conflict(device_id, src_name, rel_path, remote_info)

    def predict_delete(
        self, device_id: str, device_name: str, src_name: str, rel_path: str, reason: str = ""
    ) -> PullPrediction:
        """A tombstone-driven deletion the real pull would carry out."""
        prediction = PullPrediction(device_id, device_name, src_name, rel_path, "delete", reason)
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
    if counts["delete"]:
        line += f"; {counts['delete']} would be deleted"
    if counts["older"]:
        line += f"; {counts['older']} older peer edits would not be applied"
    if counts["kept"]:
        line += f"; {counts['kept']} conflict copies already kept"
    return line
