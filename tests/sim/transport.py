"""Finite, explicit whole-file transport. See README for fidelity limits."""

from __future__ import annotations

import math
import os
import random
import re
import stat
import tempfile
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

EPOCH = 1_791_720_000.0  # Fixed fixture time; unrelated to delivery time.


def owned_path(root: Path, relative: str) -> Path:
    """Validate lexical containment and every existing component before I/O."""
    if (
        not isinstance(relative, str)
        or not relative
        or "\x00" in relative
        or relative.startswith("/")
        or any(part in ("", ".", "..") for part in relative.split("/"))
    ):
        raise ValueError(f"Bad path {relative!r}: use a contained relative path; see README#paths")
    target = root / relative
    for component in (target, *target.parents):
        if component.is_symlink():
            raise ValueError(f"Bad path {target}: symlink {component}; remove it; see README#paths")
    return target


def device_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError(f"Bad device {name!r}: use letters/digits/_/-; see README#paths")


@dataclass(frozen=True)
class File:
    data: bytes
    mtime_ns: int
    mode: int


def read_tree(root: Path, *, ignore_transport: bool = False, _cache=None) -> dict[str, File]:
    """Read a finite tree without following links. Optional scan cache uses full stat identity.

    Validate root ancestry once and lstat every traversed child. No directory
    or file symlink is followed, including ignored names. This remains a
    sequential fixture, not a proof against hostile filesystem rename races.
    """
    owned_path(root, "containment-check")
    files, signatures = {}, {}

    def walk(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered:
            path = Path(entry.path)
            rel = prefix + entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Bad path {path}: symlink; remove it; see README#paths")
            if ignore_transport and ignored(entry.name):
                continue
            if stat.S_ISDIR(info.st_mode):
                walk(path, rel + "/")
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Bad path {path}: require a regular file; see README#paths")
            else:
                signature = (
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    stat.S_IMODE(info.st_mode),
                )
                cached = None if _cache is None else _cache.get(rel)
                try:
                    file = (
                        cached[1]
                        if cached is not None and cached[0] == signature
                        else File(path.read_bytes(), info.st_mtime_ns, stat.S_IMODE(info.st_mode))
                    )
                except OSError as error:
                    error.add_note(
                        f"Reading {root.name}/{rel}; repair I/O and retry; see README#paths"
                    )
                    raise
                files[rel] = file
                signatures[rel] = signature, file

    walk(root, "")
    if _cache is not None:
        _cache.clear()
        _cache.update(signatures)
    return files


def ignored(name: str) -> bool:
    return name in (".DS_Store", ".stfolder", ".stignore", ".stversions") or name.startswith(
        (".mm-tmp-", ".syncthing.", "~syncthing~")
    )


@dataclass(frozen=True)
class Event:
    id: int
    author: str
    path: str
    file: File | None
    mtime: float
    ancestors: frozenset[int]


@dataclass(frozen=True)
class Choice:
    event: int
    src: str
    dst: str
    path: str
    ready_at: float
    stage: str = "delivery"
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Drain:
    delivered: int
    remaining: tuple[Choice, ...]

    @property
    def quiescent(self) -> bool:
        return not self.remaining


@dataclass(frozen=True)
class Index:
    knowledge: tuple[tuple[str, frozenset[int]], ...]
    disk: tuple[tuple[str, File], ...]


@dataclass(frozen=True)
class Replacement:
    id: int
    choice: Choice


class _ReplacementInProgress(RuntimeError):
    """A generated conflict copy discovered an open destination token."""


class FakeTransport:
    def __init__(self, root: Path, *, seed: int = 0):
        self.root = root.absolute()
        self.seed = seed
        self.random = random.Random(seed)
        self.now = 0.0
        self.trace: deque[dict] = deque(maxlen=200)
        self.roots: dict[str, Path] = {}
        self.online: dict[str, bool] = {}
        self.events: dict[int, Event] = {}
        self._knowledge: dict[str, dict[str, frozenset[int]]] = {}
        # Global events survive rollback, but a saved-index restore revokes
        # this device's holdings for events it no longer knows.
        self._holders: dict[int, set[str]] = {}
        self._disk: dict[str, dict[str, File]] = {}
        self._scan_cache: dict[str, dict] = {}
        self._due: dict[tuple[int, str], float] = {}
        self._replacements: dict[int, Replacement] = {}
        self._busy: dict[tuple[str, str], int] = {}
        self._copies: dict[tuple[str, int], int] = {}
        self._serial = self._edits = self._token = 0

    def record(self, stage: str, **fields) -> None:
        self.trace.append({"seed": self.seed, "stage": stage, **fields})

    @property
    def fixture_time(self) -> float:
        """Latest fixture stamp; independent of scheduler advance()."""
        return EPOCH + self._edits

    def next_mtime(self) -> float:
        self._edits += 1
        return self.fixture_time

    def validate_root(self, name: str, root: Path) -> Path:
        device_name(name)
        root = root.absolute()
        if name in self.roots or root == self.root or not root.is_relative_to(self.root):
            raise ValueError(
                f"Bad device/root {name}/{root}: allocate a fresh owned root; see README#paths"
            )
        owned_path(self.root, root.relative_to(self.root).as_posix())
        for other in self.roots.values():
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise ValueError(
                    f"Overlapping device root {name}/{root}: use disjoint roots; see README#paths"
                )
        return root

    def add(self, name: str, root: Path, *, inherit: Index | None = None) -> None:
        root = self.validate_root(name, root)
        root.mkdir(parents=True, exist_ok=True)
        self.roots[name], self.online[name] = root, True
        self._knowledge[name], self._disk[name] = {}, {}
        if inherit is not None:
            self.restore_index(name, inherit)

    def _name(self, device) -> str:
        name = device if isinstance(device, str) else device.name
        if name not in self.roots:
            raise ValueError(f"Unknown device {name!r}: add it first; see README#paths")
        owned_path(self.roots[name], "containment-check")
        return name

    def _event(self, author, path, file, ancestors) -> Event:
        self._serial += 1  # Fleet lifetime: never part of a saved Index.
        event = Event(
            self._serial,
            author,
            path,
            file,
            file.mtime_ns / 1e9 if file is not None else self.next_mtime(),
            frozenset(ancestors),
        )
        self.events[event.id] = event
        self._holders[event.id] = set()
        return event

    def baseline(self) -> None:
        """Identical seeded replicas have common ancestry and no initial traffic."""
        if self.events or not self.roots:
            raise RuntimeError(
                "Baseline requires fresh transport and devices; see README#scheduling"
            )
        trees = {name: read_tree(root, ignore_transport=True) for name, root in self.roots.items()}
        first = next(iter(trees.values()))
        if any(tree != first for tree in trees.values()):
            raise ValueError(
                "Replica seeds differ: copy one bootstrap including mtimes; see README#scheduling"
            )
        for path, file in first.items():
            event = self._event(next(iter(self.roots)), path, file, ())
            for name in self.roots:
                self._knowledge[name][path] = frozenset((event.id,))
                self._disk[name][path] = file
                self._holders[event.id].add(name)
        self.record("baseline")

    def scan(self, device) -> tuple[int, ...]:
        name = self._name(device)
        tree = read_tree(
            self.roots[name], ignore_transport=True, _cache=self._scan_cache.setdefault(name, {})
        )
        new = []
        for path in sorted(tree.keys() | self._disk[name].keys()):
            file = tree.get(path)
            if file == self._disk[name].get(path):
                continue
            if file is None and (name, path) in self._busy:
                continue  # Scheduled absence is never a user deletion.
            event = self._event(name, path, file, self._knowledge[name].get(path, ()))
            self._knowledge[name][path] = event.ancestors | {event.id}
            self._holders[event.id].add(name)
            if file is None:
                self._disk[name].pop(path, None)
            else:
                self._disk[name][path] = file
            new.append(event.id)
            self.record("scan", event=event.id, src=name, path=path)
        return tuple(new)

    def index(self, device) -> Index:
        name = self._name(device)
        return Index(
            tuple(sorted(self._knowledge[name].items())), tuple(sorted(self._disk[name].items()))
        )

    def restore_index(self, device, index: Index) -> None:
        name = self._name(device)
        self._knowledge[name], self._disk[name] = dict(index.knowledge), dict(index.disk)
        self._scan_cache.pop(name, None)
        observed = {event for _, events in index.knowledge for event in events}
        for event, holders in self._holders.items():
            if event in observed:
                holders.add(name)
            else:
                holders.discard(name)
        self.record("restore-index", dst=name)

    def set_online(self, device, online: bool) -> None:
        name = self._name(device)
        if not isinstance(online, bool):
            raise ValueError(f"Bad online state {online!r} for {name}: use bool")
        self.online[name] = online
        self.record("online", dst=name, online=online)

    @staticmethod
    def _delta(delta: float) -> None:
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or not math.isfinite(delta)
            or delta < 0
        ):
            raise ValueError(
                f"Bad delta {delta!r}: use finite nonnegative seconds; see README#scheduling"
            )

    def advance(self, delta: float) -> None:
        self._delta(delta)
        self.now += delta
        self.record("advance", delta=delta)

    def delay(self, choice: Choice, delta: float) -> None:
        self._check(choice, eligible=False)
        self._delta(delta)
        self._due[choice.event, choice.dst] = self.now + delta
        self.record("delay", event=choice.event, dst=choice.dst, delta=delta)

    def _reasons(self, event: int, src: str, dst: str, path: str) -> tuple[str, ...]:
        reasons = []
        if not self.online[src] or not self.online[dst]:
            reasons.append("offline")
        if self._due.get((event, dst), 0) > self.now:
            reasons.append("delayed")
        if (dst, path) in self._busy or self._replacement_blocked(dst, self.events[event]):
            reasons.append("replacement-in-progress")
        return tuple(reasons)

    def pending(self) -> tuple[Choice, ...]:
        choices = []
        for event in self.events.values():
            for dst in sorted(self.roots):
                holders = self._holders[event.id] - {dst}
                if holders and event.id not in self._knowledge[dst].get(event.path, ()):
                    src = min(holders, key=lambda name: (not self.online[name], name))
                    choices.append(
                        Choice(
                            event.id,
                            src,
                            dst,
                            event.path,
                            self._due.get((event.id, dst), 0),
                            reasons=self._reasons(event.id, src, dst, event.path),
                        )
                    )
        for token in self._replacements.values():
            if not any(
                c.event == token.choice.event and c.dst == token.choice.dst for c in choices
            ):
                choices.append(
                    replace(token.choice, stage="replacement", reasons=("replacement-in-progress",))
                )
        return tuple(sorted(choices, key=lambda c: (c.event, c.dst, c.src, c.stage)))

    def choose(self) -> Choice | None:
        ready = [choice for choice in self.pending() if not choice.reasons]
        return self.random.choice(ready) if ready else None

    def _check(self, choice: Choice, *, eligible: bool = True) -> Event:
        if not isinstance(choice, Choice) or choice.event not in self.events:
            raise ValueError(
                f"Unknown delivery choice {choice!r}; use pending(); see README#scheduling"
            )
        event = self.events[choice.event]
        if (
            choice.src not in self.roots
            or choice.dst not in self.roots
            or choice.src == choice.dst
            or choice.path != event.path
            or choice.stage != "delivery"
            or choice.src not in self._holders[event.id]
        ):
            raise ValueError(f"Invalid delivery {choice!r}; use pending(); see README#scheduling")
        owned_path(self.roots[choice.dst], event.path)
        if eligible:
            reasons = self._reasons(event.id, choice.src, choice.dst, event.path)
            if reasons:
                raise RuntimeError(
                    f"Cannot deliver e{event.id} to {choice.dst}/{event.path}: {reasons}; "
                    "fix pending reason; see README#scheduling"
                )
        return event

    def _publish(self, name: str, path: str, file: File | None) -> None:
        target = owned_path(self.roots[name], path)
        temporary = None
        try:
            if file is None:
                target.unlink(missing_ok=True)
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".mm-tmp-sim-", dir=target.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(file.data)
            os.chmod(temporary, file.mode)
            os.utime(temporary, ns=(file.mtime_ns, file.mtime_ns))
            os.replace(temporary, target)
            temporary = None
        except OSError as error:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError as cleanup:
                    error.add_note(f"Staging cleanup also failed at {temporary}: {cleanup}")
            error.add_note(
                f"Publishing {name}/{path}; work remains pending; "
                "repair I/O and retry; see README#scheduling"
            )
            raise

    def _conflict(self, loser: Event) -> Event:
        loser_ref = (loser.path, loser.id)
        if loser_ref not in self._copies:
            original = Path(loser.path)
            stamp = datetime.fromtimestamp(EPOCH + loser.id, timezone.utc).strftime("%Y%m%d-%H%M%S")
            path = original.with_name(
                f"{original.stem}.sync-conflict-{stamp}-{loser.author}{original.suffix}"
            ).as_posix()
            event = self._event(loser.author, path, loser.file, ())
            self._copies[loser_ref] = event.id
        return self.events[self._copies[loser_ref]]

    def _resolve(self, name: str, event: Event) -> tuple[frozenset[int], list[Event], Event]:
        known = self._knowledge[name].get(event.path, frozenset())
        merged = known | event.ancestors | {event.id}
        heads = [
            self.events[e]
            for e in merged
            if not any(e in self.events[other].ancestors for other in merged)
        ]
        # Higher mtime wins, smaller fixture identity breaks ties; ID only
        # breaks a same-author rollback tie. Random ciphertext is never a key.
        return merged, heads, min(heads, key=lambda e: (-e.mtime, e.author, -e.id))

    def _replacement_blocked(self, name: str, event: Event) -> bool:
        if event.id in self._knowledge[name].get(event.path, ()):
            return False
        if (name, event.path) in self._busy:
            return True
        # Only inspect already minted copies: pending() never authors events.
        # Acceptance may discover another dependency; drain retries its query.
        _, heads, winner = self._resolve(name, event)
        return any(
            self._replacement_blocked(name, self.events[copy])
            for loser in heads
            if loser.file is not None
            and (winner.file is None or loser.file.data != winner.file.data)
            and (copy := self._copies.get((loser.path, loser.id))) is not None
        )

    def _accept(self, name: str, event: Event, *, replacement: int | None = None) -> bool:
        if event.id in self._knowledge[name].get(event.path, ()):
            return False
        owner = self._busy.get((name, event.path))
        if owner is not None and owner != replacement:
            raise _ReplacementInProgress(
                f"Cannot accept e{event.id} at {name}/{event.path}: replacement-in-progress; "
                "finish the owning token, then retry; see README#scheduling"
            )
        merged, heads, winner = self._resolve(name, event)
        # A loser stays a head forever; once its copy is known here, that
        # copy's own history (edits, deletion) governs, never regeneration.
        copies = [
            copy
            for copy in (
                self._conflict(e)
                for e in heads
                if e.file is not None and (winner.file is None or e.file.data != winner.file.data)
            )
            if copy.id not in self._knowledge[name].get(copy.path, ())
        ]
        for copy in copies:
            # A file the index never saw (restored behind its disk) can hold
            # this name; it meets the copy under the same rule, so neither
            # version is lost. Each copy is recorded before the next publish.
            self._accept(name, copy)
            self.record("conflict", event=copy.id, dst=name, path=copy.path)
        if self._disk[name].get(event.path) != winner.file or (name, event.path) in self._busy:
            self._publish(name, event.path, winner.file)
        # No receipt before publication succeeds. Failed work stays replayable.
        self._knowledge[name][event.path] = merged
        for observed in merged:
            self._holders[observed].add(name)
        if winner.file is None:
            self._disk[name].pop(event.path, None)
        else:
            self._disk[name][event.path] = winner.file
        return True

    def deliver(self, choice: Choice) -> bool:
        event = self._check(choice)
        self.scan(choice.dst)  # Protect local changes, including unscanned edits.
        applied = self._accept(choice.dst, event)
        self.record("deliver", event=event.id, src=choice.src, dst=choice.dst, path=event.path)
        return applied

    def begin_replace(self, choice: Choice) -> Replacement:
        event = self._check(choice)
        target = owned_path(self.roots[choice.dst], event.path)
        if (
            event.file is None
            or not target.is_file()
            or event.id in self._knowledge[choice.dst].get(event.path, ())
        ):
            raise ValueError(
                f"Cannot begin e{event.id} at {choice.dst}/{event.path}: "
                "require a new live event and existing destination; "
                "use deliver; see README#scheduling"
            )
        self.scan(choice.dst)
        if self._resolve(choice.dst, event)[2] is not event:
            # A losing version only ever lands as a conflict copy; the
            # canonical path has no rename gap.
            raise ValueError(
                f"Cannot begin e{event.id} at {choice.dst}/{event.path}: it would not win "
                "the canonical path; use deliver; see README#scheduling"
            )
        # Payload is already immutable and complete; only the rename gap is
        # exposed. Publication failure leaves this token open for explicit retry.
        try:
            target.unlink()
        except OSError as error:
            error.add_note(
                f"Beginning replace e{event.id} at {choice.dst}/{event.path}; "
                "retry after repairing I/O"
            )
            raise
        self._token += 1
        token = Replacement(self._token, choice)
        self._replacements[token.id] = token
        self._busy[choice.dst, event.path] = token.id
        self.record(
            "begin-replace", event=event.id, src=choice.src, dst=choice.dst, path=event.path
        )
        return token

    def finish_replace(self, token: Replacement) -> None:
        if not isinstance(token, Replacement) or self._replacements.get(token.id) != token:
            raise ValueError(
                f"Unknown/finished replacement {token!r}; use the open token; see README#scheduling"
            )
        choice = token.choice
        self.scan(choice.dst)  # Busy absence ignored; a user's new file is not.
        self._accept(choice.dst, self.events[choice.event], replacement=token.id)  # Local offline.
        del self._busy[choice.dst, choice.path], self._replacements[token.id]
        self.record(
            "finish-replace", event=choice.event, src=choice.src, dst=choice.dst, path=choice.path
        )

    def unfinished(self, device) -> tuple[Replacement, ...]:
        name = self._name(device)
        return tuple(t for t in self._replacements.values() if t.choice.dst == name)

    def drain(self, max_steps: int = 1000) -> Drain:
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0:
            raise ValueError(f"Bad max_steps {max_steps!r}: use a nonnegative integer")
        delivered = 0
        while delivered < max_steps:
            choice = next((c for c in self.pending() if not c.reasons), None)
            if choice is None:
                break
            try:
                self.deliver(choice)
            except _ReplacementInProgress:
                # The newly minted conflict dependency is now visible to pending().
                continue
            delivered += 1
        remaining = self.pending()
        if delivered == max_steps:
            # Only ready work was stopped by the limit; blocked work keeps its reasons.
            remaining = tuple(
                c if c.reasons else replace(c, reasons=("step-limit",)) for c in remaining
            )
        self.record("drain", delivered=delivered, remaining=len(remaining))
        return Drain(delivered, remaining)
