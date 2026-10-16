"""Private local SQLite state. Write transactions are always the innermost lock.

History alone lives here today; the four provisional store tables have no
production readers yet. No connection is cached and sqlite3 is loaded lazily.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from mind_meld import fsutil

DB_DIR = Path.home() / ".config" / "mind-meld"
BUSY_TIMEOUT_MS = 5000
SCHEMA_VERSION = 1
DISPLACED_MAX_AGE = 30 * 24 * 60 * 60
_bound_path: ContextVar[Path | None] = ContextVar("state_db_path", default=None)


class StateDBError(Exception):
    """Unclassified database failure, with its SQLite name and database path."""

    def __init__(self, name: str, path: Path | None = None):
        self.name = name
        self.path = path if path is not None else db_path()
        super().__init__(name)


class StateDBHotJournal(StateDBError):
    """Read-only access needs rollback of an interrupted write."""


class StateDBBusy(StateDBError):
    """Another connection held the lock past the timeout."""


class StateDBCorrupt(StateDBError):
    """The file is damaged or is not a database."""


class StateDBUnavailable(StateDBError):
    """This Python cannot load sqlite3."""


class StateDBTooNew(StateDBError):
    """The database requires a newer reader."""

    def __init__(self, version: int, path: Path | None = None):
        self.version = version
        self.known_version = SCHEMA_VERSION
        super().__init__("incompatible schema", path)


def db_path() -> Path:
    return _bound_path.get() or DB_DIR / "state.db"


@contextmanager
def path_scope(path: Path):
    """Bind a buffered batch to the database selected when it started."""
    token = _bound_path.set(path)
    try:
        yield
    finally:
        _bound_path.reset(token)


@contextmanager
def translate():
    """Translate errors at execute/fetch time as well as at connection time."""
    try:
        yield
    except Exception as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        if code is None:
            raise
        cls = StateDBError
        if code == 776:  # SQLITE_READONLY_ROLLBACK (extended code first)
            cls = StateDBHotJournal
        elif code & 0xFF in (5, 6):  # SQLITE_BUSY, SQLITE_LOCKED
            cls = StateDBBusy
        elif code & 0xFF in (11, 26):  # SQLITE_CORRUPT, SQLITE_NOTADB
            cls = StateDBCorrupt
        raise cls(getattr(exc, "sqlite_errorname", type(exc).__name__)) from exc


# Each migration is (statements, minimum reader version or None). Execute each
# statement individually: the script API would implicitly commit our transaction.
MIGRATIONS = (
    (
        (
            "CREATE TABLE meta (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
            (
                "CREATE TABLE ledger (source TEXT NOT NULL, path TEXT NOT NULL, sha "
                "TEXT, vv TEXT NOT NULL, size INTEGER, mtime REAL, deleted INTEGER NOT "
                "NULL DEFAULT 0, pending INTEGER NOT NULL DEFAULT 0, PRIMARY KEY "
                "(source, path)) WITHOUT ROWID"
            ),
            (
                "CREATE TABLE cursors (peer TEXT NOT NULL PRIMARY KEY, applied_seq "
                "INTEGER NOT NULL DEFAULT 0, hash TEXT, gap_seq INTEGER, gap_since "
                "INTEGER) WITHOUT ROWID"
            ),
            (
                "CREATE TABLE own_log (seq INTEGER PRIMARY KEY, prev TEXT, hash TEXT NOT "
                "NULL, parts INTEGER NOT NULL, bytes INTEGER NOT NULL, time INTEGER NOT "
                "NULL)"
            ),
            (
                "CREATE TABLE displaced (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
                "path TEXT NOT NULL, sha TEXT, vv TEXT NOT NULL, size INTEGER, mtime "
                "REAL, deleted INTEGER NOT NULL DEFAULT 0, content BLOB, reason TEXT NOT "
                "NULL, time INTEGER NOT NULL, expired INTEGER NOT NULL DEFAULT 0)"
            ),
            (
                "CREATE TABLE history (id INTEGER PRIMARY KEY, ts TEXT, ts_us INTEGER, "
                "verb TEXT, device TEXT, source TEXT, rel_path TEXT, action TEXT, "
                "local_sha TEXT, remote_sha TEXT, extra TEXT)"
            ),
            "CREATE INDEX history_class ON history (verb, action, id)",
            "CREATE INDEX history_order ON history (ts DESC, id)",
            "CREATE INDEX history_since ON history (ts_us)",
            "CREATE INDEX displaced_time ON displaced (time)",
        ),
        1,
    ),
)


@contextmanager
def transaction(conn):
    if conn.in_transaction:
        raise StateDBError("nested transaction")
    with translate():
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def check_compat(conn):
    """Refuse a database this build cannot read; readers call it inside their snapshot."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" in tables:
        value = conn.execute("SELECT value FROM meta WHERE key='compat_version'").fetchone()
        try:
            version = int(value[0]) if value else 0
        except (TypeError, ValueError):
            raise StateDBCorrupt("malformed compat_version") from None
        if version > SCHEMA_VERSION:
            raise StateDBTooNew(version)
    return tables


@contextmanager
def connect(*, read_only: bool):
    """Open, gate and close one connection; inspection never creates or migrates."""
    path = db_path()
    try:
        absent = read_only and not path.exists()
    except OSError as exc:
        raise StateDBError(str(exc), path) from exc
    if absent:
        yield None
        return
    try:
        import sqlite3
    except ImportError as exc:
        raise StateDBUnavailable("sqlite_unavailable", path) from exc
    conn = None
    with translate():
        try:
            if not read_only:
                fsutil.refuse_real_state_write_under_pytest(path)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not path.exists():
                    try:
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    except FileExistsError:
                        pass
                    else:
                        os.close(fd)
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise StateDBError("not a regular file", path)
            uri = path.absolute().as_uri() + ("?mode=ro" if read_only else "?mode=rw")
            conn = sqlite3.connect(
                uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000
            )
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            tables = check_compat(conn)
            if not read_only:
                if stat.S_IMODE(info.st_mode) & 0o077:
                    os.chmod(path, 0o600)
                conn.execute("PRAGMA synchronous = FULL")
                if not tables:
                    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                    conn.execute("PRAGMA journal_mode = DELETE")
                with transaction(conn):
                    # The transaction rechecks both gates after racing openers.
                    check_compat(conn)
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    for index in range(version, len(MIGRATIONS)):
                        statements, compat = MIGRATIONS[index]
                        for statement in statements:
                            conn.execute(statement)
                        if compat is not None:
                            conn.execute(
                                "INSERT OR REPLACE INTO meta (key, value) "
                                "VALUES ('compat_version', ?)",
                                (str(compat),),
                            )
                        conn.execute(f"PRAGMA user_version = {index + 1}")
            yield conn
        except OSError as exc:
            raise StateDBError(str(exc), path) from exc
        finally:
            if conn is not None:
                conn.close()


def recover_if_needed():
    """Repair a hot journal under the caller's mm lock. Sync never depends on it.

    Return a swallowed failure so a command can share its history failure budget.
    The pytest account-state guard (a BaseException) always propagates.
    """
    try:
        if db_path().with_name("state.db-journal").exists():
            with connect(read_only=False):
                pass
    except Exception as exc:
        return exc
    return None


def expire_displaced(conn, now, *, max_age=DISPLACED_MAX_AGE):
    """76A's caller owns the transaction; expiry retains all forensic metadata."""
    if not conn.in_transaction:
        raise StateDBError("expiry requires a transaction")
    with translate():
        conn.execute(
            "UPDATE displaced SET content=NULL, expired=1 WHERE time <= ?",
            (now - max_age,),
        )
