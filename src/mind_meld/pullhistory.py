"""Best-effort forensic history in state.db, with a tolerant legacy JSONL reader.

Rows retain the legacy public JSON shape. History never participates in sync
correctness; buffers lost to a failed flush are dropped, never replayed.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, get_args

from mind_meld import errors, safety, statedb

HISTORY_DIR = Path.home() / ".config" / "mind-meld"
ROTATED_SUFFIX = ".1"
HISTORY_CLASS_LIMIT = 20_000
BATCH_SIZE = 1000
Action = Literal[
    "written", "merged", "merged-via-lcs", "skipped", "conflicted", "excluded", "uploaded", "failed"
]
Verb = Literal["pull", "push", "self-upgrade"]
COLUMNS = ("ts", "verb", "device", "source", "rel_path", "action", "local_sha", "remote_sha")
KNOWN_KEYS = frozenset((*COLUMNS, "old_version", "new_version"))
KNOWN_CLASSES = tuple((v, a) for v in ("pull", "push") for a in (*get_args(Action), None)) + (
    ("self-upgrade", None),
)
_SELECT = ", ".join((*COLUMNS, "extra"))
_ROW_ERRORS = (18, 19, 20, 25)  # SQLITE_TOOBIG, CONSTRAINT, MISMATCH, RANGE
_MAX_LIMIT = 2**63 - 1


@dataclass
class _Command:
    failure: Exception | None = None


@dataclass
class _Batch:
    path: Path
    legacy_path: Path
    rows: list[dict] = field(default_factory=list)
    failed: bool = False


_command: ContextVar[_Command | None] = ContextVar("history_command", default=None)
_batch: ContextVar[_Batch | None] = ContextVar("history_batch", default=None)


@dataclass
class HistoryNotRecorded:
    cause: Exception


@dataclass
class LegacyHistoryPending:
    path: Path
    cause: str


@dataclass
class LegacyHistoryDiverged:
    imported: str


def explain(exc) -> str:
    """Single message table for plain errors and all history notices."""
    prefix = "mm: error: "
    if isinstance(exc, HistoryNotRecorded):
        cause = explain(exc.cause).removeprefix("mm: error: ")
        return (
            "mm: notice: history not recorded (sync unaffected; these rows are dropped and "
            "not replayed): " + cause
        )
    if isinstance(exc, LegacyHistoryPending):
        prefix = "mm: notice: "
        message = (
            f"legacy history in {exc.path} is not imported yet ({exc.cause}); "
            "it is still in that file"
        )
    elif isinstance(exc, LegacyHistoryDiverged):
        prefix = "mm: notice: "
        message = (
            f"rows written after {exc.imported} by an older mm are in "
            "~/.config/mind-meld/pull-history.jsonl or pull-history.jsonl.1 and not shown "
            "here; fix PATH (which -a mm, README#upgrading), then move both files aside to "
            "silence this"
        )
    elif isinstance(exc, statedb.StateDBHotJournal):
        message = (
            f"an interrupted mm left an unfinished history write in {exc.path}; "
            "the next `mm pull` or `mm push` repairs it"
        )
    elif isinstance(exc, statedb.StateDBBusy):
        message = f"another mm command is writing history to {exc.path}; retry when it finishes"
    elif isinstance(exc, statedb.StateDBTooNew):
        message = (
            f"{exc.path} uses history schema v{exc.version}; "
            f"this mm reads up to v{exc.known_version}; "
            "run `mm update`, and if `mm --version` is already newer, an older mm is first "
            "on PATH (`which -a mm`)"
        )
    elif isinstance(exc, statedb.StateDBUnavailable):
        message = errors.SQLITE_UNAVAILABLE_REMEDY
    elif isinstance(exc, statedb.StateDBCorrupt):
        message = (
            f"{exc.path} is damaged ({exc.name}); with no mm running, "
            "move state.db and every state.db-* file aside together; history recorded since "
            "the import is lost and the next write re-imports the legacy JSONL"
        )
    else:
        path = getattr(exc, "path", statedb.db_path())
        if isinstance(exc, statedb.StateDBError):
            name = exc.name
        else:
            # Built-in errors such as AttributeError carry an unrelated `.name`.
            name = f"{type(exc).__name__}: {exc}"
        message = (
            f"{path}: {name}; mm could not read or write its "
            "history database; see the README section"
        )
    return f"{prefix}{message}. See {errors.STATEDB_HELP_URL}"


def record_failure(exc):
    state = _command.get()
    if state is not None and exc is not None and state.failure is None:
        state.failure = exc


def failed() -> bool:
    state = _command.get()
    return state is not None and state.failure is not None


@contextmanager
def command_scope(*, attended=False):
    """One failure budget, entered before configuration/transition handling."""
    state = _Command()
    token = _command.set(state)
    try:
        yield
    finally:
        _command.reset(token)
        if attended and state.failure is not None:
            # Import only at output time; plain stderr must not be Rich-wrapped.
            import typer

            try:
                typer.echo(
                    safety.safe_terminal_str(explain(HistoryNotRecorded(state.failure))), err=True
                )
            except OSError:
                pass


def history_path() -> Path:
    return HISTORY_DIR / "pull-history.jsonl"


def row(
    *,
    verb: Verb,
    device: str,
    source: str,
    rel_path: str,
    action: Action,
    local_sha: str | None = None,
    remote_sha: str | None = None,
    ts: datetime | None = None,
) -> dict[str, Any]:
    payload = dict(
        ts=(ts or datetime.now(timezone.utc)).isoformat(),
        verb=verb,
        device=device,
        source=source,
        rel_path=rel_path,
        action=action,
    )
    if local_sha is not None:
        payload["local_sha"] = local_sha
    if remote_sha is not None:
        payload["remote_sha"] = remote_sha
    return payload


def append(
    *,
    verb: Verb,
    device: str,
    source: str,
    rel_path: str,
    action: Action,
    local_sha: str | None = None,
    remote_sha: str | None = None,
    ts: datetime | None = None,
) -> None:
    _append_payload(
        row(
            verb=verb,
            device=device,
            source=source,
            rel_path=rel_path,
            action=action,
            local_sha=local_sha,
            remote_sha=remote_sha,
            ts=ts,
        )
    )


def append_self_upgrade(
    *, device: str, old_version: str, new_version: str, ts: datetime | None = None
) -> None:
    if not old_version or not new_version:
        return
    _append_payload(
        dict(
            ts=(ts or datetime.now(timezone.utc)).isoformat(),
            verb="self-upgrade",
            device=device,
            old_version=old_version,
            new_version=new_version,
        )
    )


def _timestamp_us(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(microseconds=1)
    except (TypeError, ValueError, OverflowError):
        return None


def _encodable(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _values(payload):
    columns = {}
    extra = {}
    for key, value in payload.items():
        if key in COLUMNS and isinstance(value, str) and _encodable(value):
            columns[key] = value
        else:
            extra[key] = value
    return (
        *[columns.get(k) for k in COLUMNS],
        json.dumps(extra, sort_keys=True),
        _timestamp_us(payload.get("ts")),
    )


def _insert(conn, payload, *, ident=None):
    names = (*COLUMNS, "extra", "ts_us")
    values = _values(payload)
    if ident is not None:
        names = ("id", *names)
        values = (ident, *values)
    with statedb.translate():
        conn.execute(
            f"INSERT INTO history ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            values,
        )


def write_rows(conn, rows):
    """Only insert into the caller's transaction; never import, prune or commit."""
    if not conn.in_transaction:
        raise statedb.StateDBError("history writes require a transaction")
    for payload in rows:
        if payload.keys() - KNOWN_KEYS:
            raise ValueError("unknown history keys")
        _insert(conn, payload)


def _meta(conn):
    return dict(conn.execute("SELECT key, value FROM meta"))


def _legacy_paths(live):
    return (live.with_name(live.name + ROTATED_SUFFIX), live)


def _legacy_stats(live):
    result = {}
    for path in _legacy_paths(live):
        try:
            info = path.stat()
        except FileNotFoundError:
            result[path.name] = None
        else:
            result[path.name] = [info.st_size, info.st_mtime_ns]
    return result


def _set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, value))


def import_if_needed(conn, *, path=None):
    """Atomic legacy import, retried only on a later flush after an OS failure."""
    live = path if path is not None else history_path()
    with statedb.translate():
        meta = _meta(conn)
        if "history_imported" in meta and "history_import_pending" not in meta:
            return
        try:
            # Stat first: a concurrent older-mm append then differs from the
            # recorded baseline and fires the stale-binary notice.
            stats = _legacy_stats(live)
            legacy = []
            for file in _legacy_paths(live):
                legacy.extend(_yield_lines(file, strict=True))
        except OSError as exc:
            _set_meta(
                conn,
                "history_imported",
                meta.get("history_imported", datetime.now(timezone.utc).isoformat()),
            )
            _set_meta(conn, "history_import_pending", safety.safe_terminal_str(exc))
            return
        first = conn.execute("SELECT min(id) FROM history").fetchone()[0]
        first = min(first if first is not None else 1, 1) - len(legacy)
        for offset, payload in enumerate(legacy):
            try:
                _insert(conn, payload, ident=first + offset)
            except Exception as exc:
                # Skip only a malformed row. A rolled-back transaction or an
                # environmental SQLite error aborts the whole import for retry.
                code = getattr(exc.__cause__, "sqlite_errorcode", None)
                if not conn.in_transaction or (code is not None and code & 0xFF not in _ROW_ERRORS):
                    raise
                continue
        _set_meta(conn, "history_imported", datetime.now(timezone.utc).isoformat())
        _set_meta(conn, "legacy_jsonl_stat", json.dumps(stats, sort_keys=True))
        conn.execute("DELETE FROM meta WHERE key='history_import_pending'")


def prune(conn):
    """Keep newest ids in each known class; future receipt classes are untouched."""
    with statedb.translate():
        for verb, action in KNOWN_CLASSES:
            conn.execute(
                "DELETE FROM history WHERE verb IS ? AND action IS ? AND id < "
                "(SELECT id FROM history WHERE verb IS ? AND action IS ? "
                "ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (verb, action, verb, action, HISTORY_CLASS_LIMIT - 1),
            )


def _flush(buffer):
    if not buffer.rows:
        return
    rows, buffer.rows = buffer.rows, []
    if buffer.failed or failed():
        return
    try:
        with (
            statedb.path_scope(buffer.path),
            statedb.connect(read_only=False) as conn,
            statedb.transaction(conn),
        ):
            import_if_needed(conn, path=buffer.legacy_path)
            write_rows(conn, rows)
            prune(conn)
    except Exception as exc:
        buffer.failed = True
        record_failure(exc)


def _append_payload(payload):
    if failed():
        return
    buffer = _batch.get()
    if buffer is None:
        _flush(_Batch(statedb.db_path(), history_path(), [payload]))
    elif not buffer.failed:
        buffer.rows.append(payload)
        if len(buffer.rows) >= BATCH_SIZE:
            _flush(buffer)


@contextmanager
def batch():
    """Join an outer buffer; commit attempted rows even on an interruption."""
    if _batch.get() is not None:
        yield
        return
    buffer = _Batch(statedb.db_path(), history_path())
    token = _batch.set(buffer)
    try:
        yield
    finally:
        try:
            _flush(buffer)
        finally:
            _batch.reset(token)


def maintain_best_effort():
    if failed():
        return
    try:
        with statedb.connect(read_only=False) as conn, statedb.transaction(conn):
            import_if_needed(conn)
            prune(conn)
    except Exception as exc:
        record_failure(exc)


def _yield_lines(path, *, strict=False) -> Iterator[dict]:
    """Tolerate invalid bytes/JSON per line; import distinguishes absence from I/O."""
    try:
        with open(path, "rb") as file:
            for raw in file:
                try:
                    obj = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except FileNotFoundError:
        return
    except OSError:
        if strict:
            raise


def _legacy_records(path):
    return [record for file in _legacy_paths(path) for record in _yield_lines(file)]


def _decode(rows):
    result = []
    for values in rows:
        payload = {k: v for k, v in zip(COLUMNS, values[:-1]) if v is not None}
        try:
            payload.update(json.loads(values[-1] or "{}"))
        except (TypeError, ValueError, RecursionError):
            raise statedb.StateDBCorrupt("malformed history row") from None
        result.append(payload)
    return result


class QueryRows(list):
    """Materialized rows plus the source and notices from the same snapshot."""

    def __init__(self, rows, *, origin, notices=()):
        super().__init__(rows)
        self.origin = origin
        self.notices = notices


def _read_table(*, where="", params=(), ordering="id ASC", limit=0):
    with statedb.connect(read_only=True) as conn, statedb.translate():
        if conn is None:
            return None
        conn.execute("BEGIN")
        # Recheck inside the snapshot: a newer mm may migrate after connect().
        statedb.check_compat(conn)
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            return None
        meta = _meta(conn)
        if "history_imported" not in meta:
            return None
        sql = f"SELECT {_SELECT} FROM history {where} ORDER BY {ordering}"
        if limit > 0:
            sql += " LIMIT ?"
            params = (*params, limit)
        tuples = conn.execute(sql, params).fetchall()
        conn.execute("COMMIT")
    # Close before decoding, notices and output, including an unbounded --limit 0.
    notices = []
    if "history_import_pending" in meta:
        notices.append(LegacyHistoryPending(history_path(), meta["history_import_pending"]))
    elif "legacy_jsonl_stat" in meta:
        try:
            old = json.loads(meta["legacy_jsonl_stat"])
        except (ValueError, RecursionError):
            raise statedb.StateDBCorrupt("malformed legacy_jsonl_stat") from None
        if not isinstance(old, dict):
            raise statedb.StateDBCorrupt("malformed legacy_jsonl_stat")
        try:
            current = _legacy_stats(history_path())
        except OSError:
            current = {}
        if any(value is not None and value != old.get(key) for key, value in current.items()):
            notices.append(LegacyHistoryDiverged(meta["history_imported"]))
    return QueryRows(_decode(tuples), origin="state.db", notices=notices)


def read_records(path: Path | None = None) -> list[dict]:
    if path is None:
        rows = _read_table()
        if rows is not None:
            return rows
    return _legacy_records(path if path is not None else history_path())


def query(*, source=None, action=None, verb=None, since=None, limit=50):
    clauses, params = [], []
    for key, value in (("source", source), ("action", action), ("verb", verb)):
        if value and _encodable(value):
            clauses.append(f"{key} IS ?")
            params.append(value)
        elif value:
            # Such values were stored in `extra`, never in a filter column.
            clauses.append("0")
    if since is not None:
        if isinstance(since, datetime):
            since = since.isoformat()
        clauses.append("ts_us >= ?")
        params.append(_timestamp_us(since))
    rows = _read_table(
        where="WHERE " + " AND ".join(clauses) if clauses else "",
        params=tuple(params),
        ordering="ts DESC, id ASC",
        limit=min(limit, _MAX_LIMIT),
    )
    if rows is not None:
        return rows
    rows = []
    for record in _legacy_records(history_path()):
        if any(
            value and record.get(key) != value
            for key, value in (("source", source), ("action", action), ("verb", verb))
        ):
            continue
        if since is not None:
            stamp = _timestamp_us(record.get("ts"))
            threshold = _timestamp_us(since)
            if stamp is None or threshold is None or stamp < threshold:
                continue
        rows.append(record)
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    if limit > 0:
        rows = rows[:limit]
    return QueryRows(rows, origin="legacy pull-history.jsonl")
