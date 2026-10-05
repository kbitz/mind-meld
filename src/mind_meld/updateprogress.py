"""Translate installer output into measured progress and fixed phase labels.

No timing estimates or overall weights: a fraction belongs to the current
download, Git operation, or package batch. Unknown lines never advance it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mind_meld.safety import strip_terminal_escapes

_NUMBER = r"[0-9]{1,10}(?:\.[0-9]{1,3})?"
_DOWNLOAD = re.compile(rf"(?<![\d.])({_NUMBER})/({_NUMBER})\s*(bytes|[kMGT]?B|[KMGT]iB)\b")
_PACKAGES = re.compile(r"(?<![\d.])([0-9]{1,8})/([0-9]{1,8})\s+\[[a-zA-Z0-9_.-]{1,80}\]")
_GIT = re.compile(
    r"(Counting|Compressing|Receiving|Resolving) (objects|deltas):\s*"
    r"[0-9]{1,3}%\s*\(([0-9]{1,10})/([0-9]{1,10})\)"
)
_DOWNLOADING = "Downloading"
_INSTALLING = "Installing"
_PHASES = (
    ("upgrading shared libraries", "Preparing installer"),
    ("cloning ", "Fetching source"),
    ("fetching ", "Fetching source"),
    ("installing build dependencies", "Preparing build"),
    ("getting requirements to build wheel", "Checking build requirements"),
    ("preparing metadata", "Preparing package"),
    ("collecting ", "Checking dependencies"),
    ("requirement already satisfied:", "Checking dependencies"),
    ("using cached ", "Preparing dependencies"),
    ("downloading ", _DOWNLOADING),
    ("building wheels for", "Building wheel"),
    ("building wheel for", "Building wheel"),
    ("installing collected packages", _INSTALLING),
    ("attempting uninstall:", _INSTALLING),
    ("uninstalling ", _INSTALLING),
    ("successfully installed ", "Finishing installation"),
    ("upgraded package ", "Finishing installation"),
)


@dataclass(frozen=True)
class UpdateProgress:
    phase: str
    completed: float | None = None
    total: float | None = None
    detail: str = ""


class PipxProgressParser:
    """Accept arbitrary decoded chunks, including split ANSI terminal frames.

    Callers decode bytes incrementally; a multibyte character split across
    reads is the decoder's job, not this parser's.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._last: UpdateProgress | None = None

    def feed(self, chunk: str) -> list[UpdateProgress]:
        lines = re.split(r"[\r\n]", self._pending + chunk)
        # The parser needs one terminal line, never an unbounded log buffer.
        self._pending = lines.pop()[-8192:]
        updates: list[UpdateProgress] = []
        for raw in lines:
            self._accept(self._parse_line(_clean(raw)), updates)
        # pip's interactive spinner ends a step's line only when the step
        # finishes, so the unterminated line names the current phase. Its
        # counters wait for the line to end.
        self._accept(_parse_phase(_clean(self._pending)), updates)
        return updates

    def finish(self) -> list[UpdateProgress]:
        pending, self._pending = self._pending, ""
        updates: list[UpdateProgress] = []
        self._accept(self._parse_line(_clean(pending)), updates)
        return updates

    def _accept(self, state: UpdateProgress | None, updates: list[UpdateProgress]) -> None:
        if state is None:
            return
        if (
            state.completed is None
            and self._last is not None
            and state.phase == self._last.phase
            and state.phase != _DOWNLOADING
        ):
            # Uninstall/status lines within the same phase do not invalidate
            # its measured count. A new download does start a fresh transfer.
            return
        if state != self._last:
            self._last = state
            updates.append(state)

    @staticmethod
    def _parse_line(line: str) -> UpdateProgress | None:
        if match := _GIT.search(line):
            operation, unit, current, total = match.groups()
            completed, size = float(current), float(total)
            if size > 0 and 0 <= completed <= size:
                return UpdateProgress(
                    f"{operation} {unit}", completed, size, f"{current}/{total} {unit}"
                )
            return None
        if match := _DOWNLOAD.search(line):
            current, total, unit = match.groups()
            completed, size = float(current), float(total)
            if size > 0 and 0 <= completed <= size:
                return UpdateProgress(_DOWNLOADING, completed, size, f"{current}/{total} {unit}")
            return None
        if match := _PACKAGES.search(line):
            current, total = match.groups()
            completed, size = float(current), float(total)
            if size > 0 and 0 <= completed <= size:
                return UpdateProgress(_INSTALLING, completed, size, f"{current}/{total} packages")
            return None
        return _parse_phase(line)


def _clean(raw: str) -> str:
    return strip_terminal_escapes(raw).strip()


def _parse_phase(line: str) -> UpdateProgress | None:
    lower = line.lower()
    for marker, phase in _PHASES:
        if marker in lower:
            return UpdateProgress(phase)
    return None
