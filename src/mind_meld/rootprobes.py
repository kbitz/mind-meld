"""Host-record readers for automatic git-root discovery (Track 102A).

Each :class:`RootProber` knows one host's records that name a working
directory: how to list its *units* (the cheapest thing that can be re-checked,
such as a day directory, one small JSON file or one SQLite file), a *stamp*
that says whether a unit changed, and how to read the directories one unit
names. ``events._probe_host_records`` owns the generic index, deadline and
resume protocol; this module owns only host record shapes.

A leaf: it imports the standard library and ``token_usage`` and nothing else,
so ``events`` can use it without reaching a usage reader (``host_usage``,
``host_registry``, ``hosts.*``). Nothing here is a usage reader; a record that
names a directory is read for that directory alone. No session is attributed
to a project from its working directory, and no working directory leaves this
machine.

Credential-bearing neighbours are never opened: Cursor ``projects/*`` and
``chats/*``, ``~/.gemini/antigravity-cli/antigravity-oauth-token`` and
``~/.paseo/config.json``. Every reader names its record files exactly.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import unquote

from mind_meld import token_usage

Stamp = Any
"""``None`` (immutable once read) or a JSON list compared for equality."""

ListUnits = Callable[[Path, dict, "float | None"], "list[tuple[str, Stamp]] | None"]
ReadUnit = Callable[[Path, str, "float | None"], "tuple[list[str] | None, bool]"]
ResumeUnit = Callable[[Path, str, "float | None", dict], "tuple[list[str] | None, bool]"]


@dataclass(frozen=True)
class RootProber:
    """One host's directory-naming records.

    ``kind`` is ``"provider"`` (runs when its provider is enabled) or
    ``"tool"`` (runs when installed, see :func:`installed`).

    ``list_units(home, state, deadline)`` returns ``(key, stamp)`` pairs, newest
    first where that is cheap, or ``None`` when the deadline cut the listing.
    ``state`` is a prober-private dict persisted beside the index.

    ``read_unit(home, key, deadline)`` returns ``(directories, completed)``:
    ``completed`` is False only when the deadline cut the read; any read error
    is ``(None, True)``, and a unit that names no directory is ``([], True)``.

    Optional ``resume_unit`` also receives a unit-local reader state. It returns
    newly read directories while preserving its cursor across deadlines. Its
    ``retry`` flag retains state after a complete pass for unfinished metadata;
    the generic index retries that state next push even if the stamp is stable.
    """

    name: str
    kind: str
    list_units: ListUnits
    read_unit: ReadUnit
    resume_unit: ResumeUnit | None = None


def _discovery_home() -> Path:
    """The home whose host records discovery reads. The one test patch point."""
    return Path.home()


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _check_home(home: Path) -> None:
    """Refuse the real account home under pytest (criterion 12, belt and braces).

    ``tests/conftest.py`` redirects ``_discovery_home``; this catches a test
    that hands a prober the real home some other way.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        real = Path(pwd.getpwuid(os.getuid()).pw_dir)
        same = os.path.realpath(home) == os.path.realpath(real)
    except (KeyError, OSError):
        return
    if same:
        raise RuntimeError("refusing to read the real account's host stores from a test")


# ---------------------------------------------------------------------------
# Bounded, record-only file access.
# ---------------------------------------------------------------------------


def _open_regular(path: Path) -> BinaryIO | None:
    """Open one named record file read-only: no symlink, regular files only."""
    flags = os.O_RDONLY
    for extra in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
        flags |= getattr(os, extra, 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return os.fdopen(fd, "rb")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def _read_capped(path: Path, cap: int) -> bytes | None:
    fh = _open_regular(path)
    if fh is None:
        return None
    try:
        with fh:
            data = fh.read(cap + 1)
    except OSError:
        return None
    return data if len(data) <= cap else None


def _loads(raw: bytes | str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _child_dirs(path: Path, pattern: re.Pattern[str], deadline: float | None):
    """Real child directories whose name matches ``pattern``, sorted by name.

    ``None`` when the deadline cut the listing; ``[]`` when ``path`` is absent.
    Other errors (permission) propagate so discovery records the prober error
    rather than treating an unreadable tree as an empty one.
    """
    if _expired(deadline):
        return None
    try:
        with os.scandir(path) as listing:
            found = []
            for entry in listing:
                if not pattern.fullmatch(entry.name):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        found.append(entry)
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        return []
    found.sort(key=lambda entry: entry.name)
    return found


def _strings(values: Any) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if isinstance(value, str) and value and "\x00" not in value:
            seen.setdefault(value)
    return list(seen)


# ---------------------------------------------------------------------------
# SQLite (read-only), after the Antigravity reader's open pattern.
# ---------------------------------------------------------------------------


class _Cut(Exception):
    """The deadline interrupted a read."""


class _ReadLimit(Exception):
    """A record exceeded its byte bound; do not drain it or print a notice."""


class _Pending(Exception):
    """The first metadata record has not finished arriving yet."""


class _RecordStream:
    """Bound reads before the shared JSONL reader can drain an oversized line."""

    def __init__(self, stream: BinaryIO, cap: int, deadline: float | None):
        self.stream = stream
        self.remaining = cap
        self.deadline = deadline

    def readline(self, size: int) -> bytes:
        if _expired(self.deadline):
            raise _Cut
        if self.remaining <= 0:
            raise _ReadLimit
        limit = min(size, self.remaining)
        raw = self.stream.readline(limit)
        self.remaining -= len(raw)
        if len(raw) == limit and not raw.endswith(b"\n"):
            raise _ReadLimit
        return raw


def _sqlite_stamp(db: Path) -> list[int] | None:
    """lstat of the database and its ``-wal``, plus a hot-journal flag."""
    main = _lstat(db)
    if main is None or not stat.S_ISREG(main.st_mode):
        return None
    wal = _lstat(db.with_name(db.name + "-wal"))
    journal = _lstat(db.with_name(db.name + "-journal"))
    return [
        main.st_mtime_ns,
        main.st_size,
        wal.st_mtime_ns if wal else 0,
        wal.st_size if wal else 0,
        1 if journal else 0,
    ]


def _sqlite_first_column(db: Path, sql: str, deadline: float | None) -> list[Any] | None:
    """First column of ``sql`` from ``db``; ``None`` on any error, ``_Cut`` on the deadline.

    ``mode=ro``; ``immutable=1`` only when no ``-wal`` exists, because an
    immutable open of a live WAL database would miss committed rows.
    """
    try:
        import sqlite3
    except ImportError:
        return None
    remaining = 1.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise _Cut
    wal_present = _lstat(db.with_name(db.name + "-wal")) is not None
    uri = db.absolute().as_uri() + ("?mode=ro" if wal_present else "?mode=ro&immutable=1")
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=min(remaining, 1.0))
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA query_only=ON").fetchall()
        conn.execute("PRAGMA trusted_schema=OFF").fetchall()
        conn.set_progress_handler(lambda: 1 if _expired(deadline) else 0, 1000)
        return [row[0] for row in conn.execute(sql)]
    except sqlite3.Error:
        if _expired(deadline):
            raise _Cut from None
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Paseo (tool): ~/.paseo/projects/projects.json
# ---------------------------------------------------------------------------

_PASEO_PROJECTS = (".paseo", "projects", "projects.json")
_PASEO_CAP = 4 * 1024 * 1024


def _paseo_list(home: Path, state: dict, deadline: float | None):
    _check_home(home)
    info = _lstat(home.joinpath(*_PASEO_PROJECTS))
    if info is None or not stat.S_ISREG(info.st_mode):
        return []
    return [("projects.json", [info.st_mtime_ns, info.st_size])]


def _paseo_read(home: Path, key: str, deadline: float | None):
    _check_home(home)
    raw = _read_capped(home.joinpath(*_PASEO_PROJECTS), _PASEO_CAP)
    data = _loads(raw)
    if isinstance(data, dict) and isinstance(data.get("projects"), list):
        data = data["projects"]
    if not isinstance(data, list):
        return (None, True) if raw is None else ([], True)
    return _strings(item.get("rootPath") for item in data if isinstance(item, dict)), True


# ---------------------------------------------------------------------------
# mmm factory (tool): ~/.mmm/items/<id>.json, top-level "repo"
# ---------------------------------------------------------------------------

_MMM_ITEM_CAP = 8 * 1024 * 1024
_FILE_NAME = re.compile(r"[^/\x00]+")


def _mmm_list(home: Path, state: dict, deadline: float | None):
    _check_home(home)
    try:
        with os.scandir(home / ".mmm" / "items") as listing:
            names = []
            for n, entry in enumerate(listing):
                if n % 256 == 0 and _expired(deadline):
                    return None
                if entry.name.endswith(".json"):
                    try:
                        if entry.is_file(follow_symlinks=False):
                            names.append(entry.name)
                    except OSError:
                        continue
    except (FileNotFoundError, NotADirectoryError):
        return []
    names.sort()
    return [(f"items/{name}", None) for name in names]


def _mmm_read(home: Path, key: str, deadline: float | None):
    _check_home(home)
    name = key.partition("/")[2]
    if not _FILE_NAME.fullmatch(name) or name in (".", ".."):
        return None, True
    data = _loads(_read_capped(home / ".mmm" / "items" / name, _MMM_ITEM_CAP))
    if not isinstance(data, dict):
        return None, True
    return _strings([data.get("repo")]), True


# ---------------------------------------------------------------------------
# Antigravity (provider): ~/.gemini/antigravity-cli/conversation_summaries.db
# ---------------------------------------------------------------------------

_AGY_SUMMARIES = (".gemini", "antigravity-cli", "conversation_summaries.db")


def _file_uri_path(uri: str) -> str | None:
    """Absolute path of a ``file://`` URI (percent-decoded), else ``None``."""
    if not uri.startswith("file://"):
        return None
    rest = uri[len("file://") :]
    slash = rest.find("/")
    if slash < 0 or rest[:slash] not in ("", "localhost"):
        return None
    return unquote(rest[slash:])


def _agy_list(home: Path, state: dict, deadline: float | None):
    _check_home(home)
    stamp = _sqlite_stamp(home.joinpath(*_AGY_SUMMARIES))
    return [] if stamp is None else [("conversation_summaries.db", stamp)]


def _agy_read(home: Path, key: str, deadline: float | None):
    _check_home(home)
    try:
        values = _sqlite_first_column(
            home.joinpath(*_AGY_SUMMARIES),
            "SELECT workspace_uris FROM conversation_summaries",
            deadline,
        )
    except _Cut:
        return None, False
    if values is None:
        return None, True
    paths: list[Any] = []
    for raw in values:
        if isinstance(raw, (bytes, memoryview)):
            raw = bytes(raw)
        uris = _loads(raw)
        for uri in uris if isinstance(uris, list) else []:
            if isinstance(uri, str):
                paths.append(_file_uri_path(uri))
    return _strings(paths), True


# ---------------------------------------------------------------------------
# Muse (provider): ~/.local/share/muse/sessions/YYYY/MM/DD/<session>/session.jsonl
# ---------------------------------------------------------------------------

_YEAR = re.compile(r"\d{4}")
_MONTH_OR_DAY = re.compile(r"\d{2}")
_MUSE_KEY = re.compile(r"\d{4}/\d{2}/\d{2}/[^/\x00]+")
_MUSE_RECORDS = 16
_MUSE_CAP = 8 * 1024 * 1024


def _muse_root(home: Path) -> Path:
    return home / ".local" / "share" / "muse" / "sessions"


def _muse_list(home: Path, state: dict, deadline: float | None):
    _check_home(home)
    units: list[tuple[str, Stamp]] = []
    years = _child_dirs(_muse_root(home), _YEAR, deadline)
    if years is None:
        return None
    for year in reversed(years):
        months = _child_dirs(Path(year.path), _MONTH_OR_DAY, deadline)
        if months is None:
            return None
        for month in reversed(months):
            days = _child_dirs(Path(month.path), _MONTH_OR_DAY, deadline)
            if days is None:
                return None
            for day in reversed(days):
                sessions = _child_dirs(Path(day.path), _FILE_NAME, deadline)
                if sessions is None:
                    return None
                prefix = f"{year.name}/{month.name}/{day.name}/"
                units.extend((prefix + s.name, None) for s in reversed(sessions))
    return units


def _muse_workspace(record: Any) -> str | None:
    payload = record.get("payload") if isinstance(record, dict) else None
    inner = payload.get("record") if isinstance(payload, dict) else None
    if not isinstance(inner, dict):
        return None
    direct = inner.get("workspace_root")
    if isinstance(direct, str) and direct:
        return direct
    command = inner.get("command")
    meta = command.get("session_metadata") if isinstance(command, dict) else None
    nested = meta.get("workspace_root") if isinstance(meta, dict) else None
    return nested if isinstance(nested, str) and nested else None


def _muse_read(home: Path, key: str, deadline: float | None):
    _check_home(home)
    if not _MUSE_KEY.fullmatch(key) or ".." in key.split("/"):
        return None, True
    path = _muse_root(home) / key / "session.jsonl"
    fh = _open_regular(path)
    if fh is None:
        return None, True
    try:
        with fh:
            lines = token_usage.iter_bounded_lines(
                _RecordStream(fh, _MUSE_CAP, deadline),
                str(path),
                0,
                label="root probe",
                yield_final_partial=True,
            )
            for _ in range(_MUSE_RECORDS):
                line = next(lines, None)
                if line is None:
                    return None, True  # metadata may arrive in a later append
                raw, _end = line
                found = _muse_workspace(_loads(raw.strip()))
                if found:
                    return [found], True
    except _Cut:
        return None, False
    except (OSError, _ReadLimit):
        return None, True
    return [], True


# ---------------------------------------------------------------------------
# Cursor (provider, usage consent): ACP sessions and Conductor's SDK store.
# Never opens ~/.cursor/projects/* or ~/.cursor/chats/*.
# ---------------------------------------------------------------------------

_CURSOR_ACP_CAP = 64 * 1024
_CURSOR_NDJSON_CAP = 8 * 1024 * 1024
_CONDUCTOR_STORE = ("Library", "Application Support", "com.conductor.app", "cursor-sdk-store")


def _cursor_acp_root(home: Path) -> Path:
    return home / ".cursor" / "acp-sessions"


def _cursor_list(home: Path, state: dict, deadline: float | None):
    _check_home(home)
    units: list[tuple[str, Stamp]] = []
    root = _cursor_acp_root(home)
    if _expired(deadline):
        return None
    try:
        with os.scandir(root) as listing:
            sessions = []
            for n, entry in enumerate(listing):
                if n % 256 == 0 and _expired(deadline):
                    return None
                try:
                    if entry.is_dir(follow_symlinks=False):
                        sessions.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name))
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        sessions = []
    sessions.sort(reverse=True)
    units.extend((f"acp/{name}", None) for _mtime, name in sessions)
    stores = _child_dirs(home.joinpath(*_CONDUCTOR_STORE), _FILE_NAME, deadline)
    if stores is None:
        return None
    for store in stores:
        ndjson = _lstat(Path(store.path) / "agents.ndjson")
        if ndjson is not None and stat.S_ISREG(ndjson.st_mode):
            units.append(
                (f"conductor/{store.name}/agents.ndjson", [ndjson.st_mtime_ns, ndjson.st_size])
            )
        stamp = _sqlite_stamp(Path(store.path) / "index.db")
        if stamp is not None:
            units.append((f"conductor/{store.name}/index.db", stamp))
    return units


def _cursor_read(home: Path, key: str, deadline: float | None):
    return _cursor_resume(home, key, deadline, {})


def _cursor_resume(home: Path, key: str, deadline: float | None, state: dict):
    _check_home(home)
    kind, _, rest = key.partition("/")
    if kind == "acp":
        if not _FILE_NAME.fullmatch(rest) or rest in (".", ".."):
            return None, True
        data = _loads(_read_capped(_cursor_acp_root(home) / rest / "meta.json", _CURSOR_ACP_CAP))
        if not isinstance(data, dict):
            return None, True
        paths = _strings([data.get("cwd")])
        return paths or None, True
    store, _, name = rest.partition("/")
    if kind != "conductor" or not _FILE_NAME.fullmatch(store) or store in (".", ".."):
        return None, True
    directory = home.joinpath(*_CONDUCTOR_STORE) / store
    if name == "agents.ndjson":
        return _cursor_ndjson(directory / name, deadline, state)
    if name == "index.db":
        if _lstat(directory / "index.db-journal") is not None:
            return None, True  # mid-transaction; the stamp changes when it settles
        try:
            values = _sqlite_first_column(
                directory / name, "SELECT workspace_ref FROM agents", deadline
            )
        except _Cut:
            return None, False
        if values is None:
            return None, True
        return _strings(v for v in values if isinstance(v, str) and v.startswith("/")), True
    return None, True


def _cursor_ndjson(path: Path, deadline: float | None, state: dict):
    fh = _open_regular(path)
    if fh is None:
        return None, True
    found: list[Any] = []
    offset = state.get("offset", 0)
    if not isinstance(offset, int) or not 0 <= offset < _CURSOR_NDJSON_CAP:
        offset = 0
    try:
        with fh:
            fh.seek(offset)
            lines = token_usage.iter_bounded_lines(
                _RecordStream(fh, _CURSOR_NDJSON_CAP - offset, deadline),
                str(path),
                offset,
                label="root probe",
                yield_final_partial=True,
            )
            for raw, end in lines:
                record = _loads(raw.strip())
                if isinstance(record, dict):
                    found.append(record.get("cwd"))
                state["offset"] = end
    except _Cut:
        return _strings(found), False
    except _ReadLimit:
        return _strings(found), True
    except OSError:
        return None, True
    return _strings(found), True


# ---------------------------------------------------------------------------
# Codex (provider): rollouts' first record, a session_meta carrying payload.cwd.
# Units are live day directories and archived date groups. Never opens
# state_5.sqlite or session_index.jsonl (neither is read: see Track 102A).
# ---------------------------------------------------------------------------

_CODEX_HEAD_BYTES = 4096
_CODEX_SESSION_META = b'"type":"session_meta"'
_CODEX_PAYLOAD = b'"payload":'
_CODEX_CWD = b'"cwd":'
_CODEX_LIVE_KEY = re.compile(r"live/(\d{4})/(\d{2})/(\d{2})")
_CODEX_ARCHIVED_KEY = re.compile(r"archived/(\d{4}-\d{2}-\d{2})")
_CODEX_ARCHIVED_NAME = re.compile(r"rollout-(\d{4}-\d{2}-\d{2})[^/\x00]*\.jsonl")
_CODEX_LIVE_NAME = re.compile(r"rollout-[^/\x00]*\.jsonl")

# Process-local listing memo for the archived directory (never persisted).
_archived_memo: tuple[tuple[str, int], dict[str, list[str]]] | None = None


def _codex_meta_cwd(record: Any) -> str | None:
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def _codex_head_cwd(head: bytes) -> str | None:
    """``payload.cwd`` of a ``session_meta`` first record from its leading bytes.

    Relies on Codex writing ``type`` before ``payload`` and ``cwd`` early in
    it. Any shape this does not recognise returns ``None`` and the caller
    falls back to parsing the whole first line.
    """
    at_type = head.find(_CODEX_SESSION_META)
    if at_type < 0:
        return None
    at_payload = head.find(_CODEX_PAYLOAD, at_type)
    if at_payload < 0:
        return None
    at_cwd = head.find(_CODEX_CWD, at_payload)
    if at_cwd < 0:
        return None
    text = head[at_cwd + len(_CODEX_CWD) :].decode("utf-8", "replace").lstrip()
    if not text.startswith('"'):
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    return value if isinstance(value, str) and value else None


def _codex_rollout_cwd(path: Path) -> str | None:
    fh = _open_regular(path)
    if fh is None:
        return None
    try:
        with fh:
            head = fh.read(_CODEX_HEAD_BYTES)
            newline = head.find(b"\n")
            if newline >= 0 or len(head) < _CODEX_HEAD_BYTES:
                record = _loads(head if newline < 0 else head[:newline])
                if newline < 0 and record is None:
                    raise _Pending
                return _codex_meta_cwd(record)
            cwd = _codex_head_cwd(head)
            if cwd is not None:
                return cwd
            fh.seek(0)
            for raw, _end in token_usage.iter_bounded_lines(
                _RecordStream(fh, token_usage.MAX_JSONL_LINE_BYTES, None),
                str(path),
                0,
                label="root probe",
                yield_final_partial=True,
            ):
                record = _loads(raw.strip())
                if not raw.endswith(b"\n") and record is None:
                    raise _Pending
                return _codex_meta_cwd(record)
    except (OSError, _ReadLimit):
        return None
    return None


def _codex_root(home: Path) -> Path:
    return home / ".codex"


def _archived_groups(
    archived: Path, mtime_ns: int, deadline: float | None
) -> dict[str, list[str]] | None:
    """Archived rollout names grouped by their name date; ``None`` if cut."""
    global _archived_memo
    key = (str(archived), mtime_ns)
    if _archived_memo is not None and _archived_memo[0] == key:
        return _archived_memo[1]
    groups: dict[str, list[str]] = {}
    try:
        with os.scandir(archived) as listing:
            for n, entry in enumerate(listing):
                if n % 256 == 0 and _expired(deadline):
                    return None
                match = _CODEX_ARCHIVED_NAME.fullmatch(entry.name)
                if match:
                    groups.setdefault(match.group(1), []).append(entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return {}
    for names in groups.values():
        names.sort()
    _archived_memo = (key, groups)
    return groups


def _group_stamp(names: list[str]) -> list[Any]:
    digest = hashlib.sha256("\n".join(names).encode("utf-8", "surrogateescape")).hexdigest()
    return [len(names), digest[:16]]


def _codex_list(home: Path, state: dict, deadline: float | None):
    _check_home(home)
    root = _codex_root(home)
    live: list[tuple[str, Stamp]] = []
    years = _child_dirs(root / "sessions", _YEAR, deadline)
    if years is None:
        return None
    for year in years:
        months = _child_dirs(Path(year.path), _MONTH_OR_DAY, deadline)
        if months is None:
            return None
        for month in months:
            days = _child_dirs(Path(month.path), _MONTH_OR_DAY, deadline)
            if days is None:
                return None
            for day in days:
                try:
                    mtime = day.stat(follow_symlinks=False).st_mtime_ns
                except OSError:
                    continue
                live.append((f"live/{year.name}/{month.name}/{day.name}", [mtime]))
    live.sort(reverse=True)

    archived = root / "archived_sessions"
    info = _lstat(archived)
    if info is None or not stat.S_ISDIR(info.st_mode):
        state.pop("archived_mtime_ns", None)
        state.pop("archived_groups", None)
        return live
    stored = state.get("archived_groups")
    if state.get("archived_mtime_ns") == info.st_mtime_ns and isinstance(stored, dict):
        groups = {
            date: stamp
            for date, stamp in stored.items()
            if isinstance(date, str) and isinstance(stamp, list)
        }
    else:
        names = _archived_groups(archived, info.st_mtime_ns, deadline)
        if names is None:
            return None
        groups = {date: _group_stamp(group) for date, group in names.items()}
        state["archived_mtime_ns"] = info.st_mtime_ns
        state["archived_groups"] = groups
    return live + [(f"archived/{date}", groups[date]) for date in sorted(groups, reverse=True)]


def _codex_unit_cwds(paths: list[Path], deadline: float | None, state: dict):
    found: list[Any] = []
    after = state.get("after")
    if not isinstance(after, str):
        after = ""
    waiting = state.get("waiting")
    waiting = (
        set(waiting)
        if isinstance(waiting, list) and all(isinstance(n, str) for n in waiting)
        else set()
    )
    waiting.intersection_update(path.name for path in paths)
    # New files precede retry files so one unfinished record cannot starve the
    # main cursor. Each filename is attempted at most once per invocation.
    todo = [path for path in paths if path.name > after]
    retry_after = state.get("retry_after", "")
    if not isinstance(retry_after, str):
        retry_after = ""
    retries = [
        path
        for path in paths
        if path.name <= after and path.name in waiting and path.name > retry_after
    ]
    scan_after = after
    todo.extend(retries)
    for path in todo:
        if _expired(deadline):
            state["waiting"] = sorted(waiting)
            state["retry"] = bool(waiting)
            return _strings(found), False
        try:
            found.append(_codex_rollout_cwd(path))
        except _Pending:
            waiting.add(path.name)
        else:
            waiting.discard(path.name)
        state["after"] = max(after, path.name)
        after = state["after"]
        if path.name <= scan_after:
            state["retry_after"] = path.name
    state["waiting"] = sorted(waiting)
    state["retry"] = bool(waiting)
    state.pop("retry_after", None)
    return _strings(found), True


def _codex_read(home: Path, key: str, deadline: float | None):
    return _codex_resume(home, key, deadline, {})


def _codex_resume(home: Path, key: str, deadline: float | None, state: dict):
    _check_home(home)
    root = _codex_root(home)
    live = _CODEX_LIVE_KEY.fullmatch(key)
    if live:
        day = root.joinpath("sessions", *live.groups())
        try:
            with os.scandir(day) as listing:
                names = sorted(
                    entry.name for entry in listing if _CODEX_LIVE_NAME.fullmatch(entry.name)
                )
        except OSError:
            return None, True
        return _codex_unit_cwds([day / name for name in names], deadline, state)
    archived_key = _CODEX_ARCHIVED_KEY.fullmatch(key)
    if archived_key:
        archived = root / "archived_sessions"
        info = _lstat(archived)
        if info is None:
            return None, True
        try:
            groups = _archived_groups(archived, info.st_mtime_ns, deadline)
        except OSError:
            return None, True
        if groups is None:
            return None, False
        return _codex_unit_cwds(
            [archived / name for name in groups.get(archived_key.group(1), [])], deadline, state
        )
    return None, True


# ---------------------------------------------------------------------------
# Registry. Order is load-bearing: discovery stops at its deadline, so cheap
# probers run first and a cold Codex or Cursor corpus cannot starve Paseo or
# mmm. Attribution goes to the first prober that finds a root.
# ---------------------------------------------------------------------------

PROBERS: tuple[RootProber, ...] = (
    RootProber("paseo", "tool", _paseo_list, _paseo_read),
    RootProber("mmm", "tool", _mmm_list, _mmm_read),
    RootProber("agy", "provider", _agy_list, _agy_read),
    RootProber("muse", "provider", _muse_list, _muse_read),
    RootProber("cursor", "provider", _cursor_list, _cursor_read, _cursor_resume),
    RootProber("codex", "provider", _codex_list, _codex_read, _codex_resume),
)

_TOOL_MARKERS: dict[str, tuple[tuple[str, ...], bool]] = {
    "paseo": ((".paseo", "projects", "projects.json"), False),
    "mmm": ((".mmm", "items"), True),
}


def installed(prober: RootProber, home: Path) -> bool:
    """Whether a tool's record exists under ``home``.

    Providers return True: their gate is the user's consent, which the caller
    decides. gstack has no prober: its project state names no directory.
    """
    if prober.kind != "tool":
        return True
    marker = _TOOL_MARKERS.get(prober.name)
    if marker is None:
        return False
    parts, is_dir = marker
    try:
        info = os.stat(home.joinpath(*parts))
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) if is_dir else stat.S_ISREG(info.st_mode)
