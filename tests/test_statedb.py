"""Local state schema, transaction, safety and compatibility contracts."""

import ast
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mind_meld import fsutil, statedb


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(statedb, "DB_DIR", tmp_path / "state ?#%")
    monkeypatch.setattr(statedb, "BUSY_TIMEOUT_MS", 100)


# Hardcoded independently of migration SQL: name, type, notnull, default, pk.
SCHEMA = {
    "meta": [("key", "TEXT", 1, None, 1), ("value", "TEXT", 1, None, 0)],
    "ledger": [
        ("source", "TEXT", 1, None, 1),
        ("path", "TEXT", 1, None, 2),
        ("sha", "TEXT", 0, None, 0),
        ("vv", "TEXT", 1, None, 0),
        ("size", "INTEGER", 0, None, 0),
        ("mtime", "REAL", 0, None, 0),
        ("deleted", "INTEGER", 1, "0", 0),
        ("pending", "INTEGER", 1, "0", 0),
    ],
    "cursors": [
        ("peer", "TEXT", 1, None, 1),
        ("applied_seq", "INTEGER", 1, "0", 0),
        ("hash", "TEXT", 0, None, 0),
        ("gap_seq", "INTEGER", 0, None, 0),
        ("gap_since", "INTEGER", 0, None, 0),
    ],
    "own_log": [
        ("seq", "INTEGER", 0, None, 1),
        ("prev", "TEXT", 0, None, 0),
        ("hash", "TEXT", 1, None, 0),
        ("parts", "INTEGER", 1, None, 0),
        ("bytes", "INTEGER", 1, None, 0),
        ("time", "INTEGER", 1, None, 0),
    ],
    "displaced": [
        ("id", "INTEGER", 0, None, 1),
        ("source", "TEXT", 1, None, 0),
        ("path", "TEXT", 1, None, 0),
        ("sha", "TEXT", 0, None, 0),
        ("vv", "TEXT", 1, None, 0),
        ("size", "INTEGER", 0, None, 0),
        ("mtime", "REAL", 0, None, 0),
        ("deleted", "INTEGER", 1, "0", 0),
        ("content", "BLOB", 0, None, 0),
        ("reason", "TEXT", 1, None, 0),
        ("time", "INTEGER", 1, None, 0),
        ("expired", "INTEGER", 1, "0", 0),
    ],
    "history": [("id", "INTEGER", 0, None, 1)]
    + [
        (name, "INTEGER" if name == "ts_us" else "TEXT", 0, None, 0)
        for name in (
            "ts",
            "ts_us",
            "verb",
            "device",
            "source",
            "rel_path",
            "action",
            "local_sha",
            "remote_sha",
            "extra",
        )
    ],
}
INDEXES = {
    "meta": [("sqlite_autoindex_meta_1", 1, "pk", 0)],
    "ledger": [("sqlite_autoindex_ledger_1", 1, "pk", 0)],
    "cursors": [("sqlite_autoindex_cursors_1", 1, "pk", 0)],
    "own_log": [],
    "displaced": [("displaced_time", 0, "c", 0)],
    "history": [
        ("history_class", 0, "c", 0),
        ("history_order", 0, "c", 0),
        ("history_since", 0, "c", 0),
    ],
}


def test_schema_settings_and_reopen():
    for _ in range(2):
        with statedb.connect(read_only=False) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (1,)
            assert conn.execute("PRAGMA auto_vacuum").fetchone() == (2,)
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            assert conn.execute("PRAGMA synchronous").fetchone() == (2,)
            assert conn.execute("PRAGMA fullfsync").fetchone() == (0,)
            assert conn.execute("PRAGMA busy_timeout").fetchone() == (100,)
            assert conn.execute("SELECT value FROM meta WHERE key='compat_version'").fetchone() == (
                "1",
            )
            for table, expected in SCHEMA.items():
                assert [r[1:] for r in conn.execute(f"PRAGMA table_info({table})")] == expected
                assert sorted(r[1:] for r in conn.execute(f"PRAGMA index_list({table})")) == sorted(
                    INDEXES[table]
                )
            for table in ("ledger", "cursors", "own_log", "displaced"):
                assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert stat.S_IMODE(statedb.db_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(statedb.DB_DIR.stat().st_mode) == 0o700


def test_read_only_absent_populated_and_closes():
    with statedb.connect(read_only=True) as conn:
        assert conn is None
    assert not statedb.DB_DIR.exists()
    with statedb.connect(read_only=False) as conn:
        pass
    before = {p.name: p.read_bytes() for p in statedb.DB_DIR.iterdir()}
    with statedb.connect(read_only=True) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
    assert {p.name: p.read_bytes() for p in statedb.DB_DIR.iterdir()} == before
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_permissions_journal_existing_no_open(monkeypatch):
    with statedb.connect(read_only=False):
        pass
    statedb.db_path().chmod(0o644)
    original = os.open

    def guarded(path, *args, **kwargs):
        assert Path(path) != statedb.db_path(), "existing DB must only be opened by SQLite"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    with statedb.connect(read_only=False) as conn, statedb.transaction(conn):
        conn.execute("INSERT INTO meta (key,value) VALUES ('test','value')")
        assert stat.S_IMODE(statedb.db_path().stat().st_mode) == 0o600
        assert stat.S_IMODE(statedb.db_path().with_name("state.db-journal").stat().st_mode) == 0o600


def test_racing_creator_and_symlink(monkeypatch, tmp_path):
    original = os.open

    def race(path, *args, **kwargs):
        fd = original(path, *args, **kwargs)
        os.close(fd)
        raise FileExistsError(path)

    monkeypatch.setattr(os, "open", race)
    with statedb.connect(read_only=False):
        pass
    statedb.db_path().unlink()
    target = tmp_path / "target"
    target.write_bytes(b"")
    statedb.db_path().symlink_to(target)
    with pytest.raises(statedb.StateDBError, match="not a regular file"):
        with statedb.connect(read_only=False):
            pass
    assert target.read_bytes() == b""


def test_atomic_failed_migration(monkeypatch):
    monkeypatch.setattr(statedb, "MIGRATIONS", (((statedb.MIGRATIONS[0][0][0], "INVALID SQL"), 1),))
    with pytest.raises(statedb.StateDBError):
        with statedb.connect(read_only=False):
            pass
    with sqlite3.connect(statedb.db_path()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_nested_and_outer_rollback():
    with statedb.connect(read_only=False) as conn:
        with pytest.raises(ValueError), statedb.transaction(conn):
            conn.execute("INSERT INTO meta (key,value) VALUES ('rolled','back')")
            with pytest.raises(statedb.StateDBError, match="^nested transaction$"):
                with statedb.transaction(conn):
                    pass
            raise ValueError()
        assert conn.execute("SELECT value FROM meta WHERE key='rolled'").fetchone() is None


def test_displaced_expiry_preserves_metadata():
    with statedb.connect(read_only=False) as conn, statedb.transaction(conn):
        conn.execute(
            (
                "INSERT INTO displaced "
                "(source,path,sha,vv,size,mtime,content,reason,time) VALUES "
                "('s','p','sha','{}',3,2,?,'conflict',100)"
            ),
            (b"abc",),
        )
        before = conn.execute("SELECT * FROM displaced").fetchone()
        statedb.expire_displaced(conn, 100 + statedb.DISPLACED_MAX_AGE - 1)
        assert conn.execute("SELECT * FROM displaced").fetchone() == before
        statedb.expire_displaced(conn, 100 + statedb.DISPLACED_MAX_AGE)
        after = conn.execute("SELECT * FROM displaced").fetchone()
        assert after[:8] == before[:8]
        assert after[8:] == (None, "conflict", 100, 1)


def test_compatible_future_and_wal_then_incompatible_untouched():
    with statedb.connect(read_only=False) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA user_version=2")
        conn.execute("DROP TABLE ledger")
        conn.execute("CREATE TABLE ledger (future TEXT)")
    for read_only in (True, False):
        with statedb.connect(read_only=read_only) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (2,)
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    with sqlite3.connect(statedb.db_path()) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("UPDATE meta SET value='2' WHERE key='compat_version'")
    statedb.db_path().chmod(0o644)
    before = statedb.db_path().read_bytes()
    for read_only in (True, False):
        with pytest.raises(statedb.StateDBTooNew) as exc:
            with statedb.connect(read_only=read_only):
                pass
        assert exc.value.version == 2
        assert statedb.db_path().read_bytes() == before
        assert stat.S_IMODE(statedb.db_path().stat().st_mode) == 0o644


def test_unavailable_corrupt_busy(monkeypatch):
    with statedb.connect(read_only=False):
        pass
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "sqlite3", None)
        with pytest.raises(statedb.StateDBUnavailable):
            with statedb.connect(read_only=True):
                pass
    with sqlite3.connect(statedb.db_path()) as locker:
        locker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(statedb.StateDBBusy):
            with statedb.connect(read_only=True):
                pass
    statedb.db_path().write_bytes(b"not a database")
    with pytest.raises(statedb.StateDBCorrupt):
        with statedb.connect(read_only=True):
            pass


@pytest.mark.parametrize(
    "code,kind",
    [
        (776, statedb.StateDBHotJournal),
        (517, statedb.StateDBBusy),
        (262, statedb.StateDBBusy),
        (779, statedb.StateDBCorrupt),
        (266, statedb.StateDBError),
        (13, statedb.StateDBError),
    ],
)
def test_extended_error_mapping(code, kind):
    exc = sqlite3.OperationalError("injected")
    exc.sqlite_errorcode = code
    exc.sqlite_errorname = "INJECTED"
    with pytest.raises(kind) as caught, statedb.translate():
        raise exc
    assert type(caught.value) is kind
    assert caught.value.name == "INJECTED"


def test_real_account_state_guard(monkeypatch):
    import pwd

    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/mind-meld"
    monkeypatch.setattr(statedb, "DB_DIR", real)
    with pytest.raises(fsutil.PytestRealStateWriteError):
        with statedb.connect(read_only=False):
            pass


def test_account_state_guard_refuses_before_mutation(
    synthetic_account_home, forbid_fs_mutations, monkeypatch
):
    monkeypatch.setattr(statedb, "DB_DIR", synthetic_account_home / ".config/mind-meld")
    calls = forbid_fs_mutations()
    with pytest.raises(fsutil.PytestRealStateWriteError):
        with statedb.connect(read_only=False):
            pass
    assert calls == []
    assert not (synthetic_account_home / ".config").exists()


def _child_env(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parent)
    return env


def test_two_new_database_openers(tmp_path):
    code = """from pathlib import Path
import sys
from mind_meld import statedb
statedb.DB_DIR = Path(sys.argv[1])
with statedb.connect(read_only=False) as conn:
    assert conn.execute('PRAGMA user_version').fetchone() == (1,)
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(statedb.DB_DIR)],
            env=_child_env(tmp_path),
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    for child in children:
        _, err = child.communicate(timeout=10)
        assert child.returncode == 0, err.decode()


def make_hot_journal(path, tmp_path):
    code = """import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA cache_size=5')
conn.execute('BEGIN IMMEDIATE')
for i in range(200):
    conn.execute('INSERT INTO meta (key,value) VALUES (?,?)', (str(i), 'x'*5000))
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(path)], env=_child_env(tmp_path), check=True)


def test_hot_journal_read_only_and_recovery(tmp_path):
    # Generated by /ship test coverage audit (Track 73B).
    # Value: protects=recover_if_needed opens nothing and creates no state.db when no
    # state.db-journal exists; fails_when=the journal-existence gate is dropped so
    # every push/pull/auto hook opens writable; why_new=every recovery test seeds a
    # journal first; seam=none; no_claim=CLI wiring (unit only; tmp DB_DIR)
    assert statedb.recover_if_needed() is None
    assert not statedb.DB_DIR.exists()
    with statedb.connect(read_only=False):
        pass
    make_hot_journal(statedb.db_path(), tmp_path)
    before = {p.name: p.read_bytes() for p in statedb.DB_DIR.iterdir()}
    with pytest.raises(statedb.StateDBHotJournal):
        with statedb.connect(read_only=True):
            pass
    assert {p.name: p.read_bytes() for p in statedb.DB_DIR.iterdir()} == before
    assert statedb.recover_if_needed() is None
    assert not statedb.db_path().with_name("state.db-journal").exists()
    with statedb.connect(read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM meta").fetchone() == (1,)


def test_no_script_api():
    tree = ast.parse(Path(statedb.__file__).read_text())
    assert not any(
        isinstance(n, ast.Attribute) and n.attr == "executescript" for n in ast.walk(tree)
    )


def test_recovery_swallows_exception_but_not_guard(monkeypatch):
    statedb.DB_DIR.mkdir(parents=True)
    statedb.db_path().with_name("state.db-journal").touch()
    from contextlib import contextmanager

    @contextmanager
    def broken(*, read_only):
        raise statedb.StateDBCorrupt("SQLITE_CORRUPT")
        yield

    monkeypatch.setattr(statedb, "connect", broken)
    assert isinstance(statedb.recover_if_needed(), statedb.StateDBCorrupt)

    @contextmanager
    def guarded(*, read_only):
        raise fsutil.PytestRealStateWriteError("guard")
        yield

    monkeypatch.setattr(statedb, "connect", guarded)
    with pytest.raises(fsutil.PytestRealStateWriteError):
        statedb.recover_if_needed()


def test_racing_opener_migrates_before_our_begin(monkeypatch):
    from contextlib import contextmanager

    original = statedb.transaction
    raced = []

    @contextmanager
    def transaction(conn):
        if not raced:
            raced.append(1)
            with statedb.connect(read_only=False):  # the other process wins
                pass
        with original(conn):
            yield

    monkeypatch.setattr(statedb, "transaction", transaction)
    with statedb.connect(read_only=False) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM meta").fetchone() == (1,)
    assert raced == [1]


def test_writer_refuses_compat_raised_before_its_transaction(monkeypatch):
    # Generated by /ship test coverage audit (Track 73B).
    # Value: protects=a writable connect refuses, bytes unchanged, when a newer mm
    # raises compat_version between connect's gate and BEGIN IMMEDIATE;
    # fails_when=the in-transaction check_compat recheck is dropped;
    # why_new=test_racing_opener_migrates_before_our_begin races a same-version
    # opener only; seam=none; no_claim=a separate process (in-process raw writer
    # stands in for the newer mm)
    from contextlib import contextmanager

    with statedb.connect(read_only=False):
        pass
    original = statedb.transaction
    raced = []

    @contextmanager
    def transaction(conn):
        if not raced:
            newer = sqlite3.connect(statedb.db_path())
            with newer:  # the newer mm migrates after our gate, before our BEGIN
                newer.execute("UPDATE meta SET value='2' WHERE key='compat_version'")
                newer.execute("PRAGMA user_version = 2")
            newer.close()
            raced.append(statedb.db_path().read_bytes())
        with original(conn):
            yield

    monkeypatch.setattr(statedb, "transaction", transaction)
    with pytest.raises(statedb.StateDBTooNew) as exc:
        with statedb.connect(read_only=False):
            pass
    assert exc.value.version == 2
    assert len(raced) == 1
    assert statedb.db_path().read_bytes() == raced[0]


def test_cli_starts_without_sqlite3(tmp_path):
    code = (
        "import sys; sys.modules['sqlite3'] = None\n"
        "from typer.testing import CliRunner\n"
        "from mind_meld.cli import app\n"
        "result = CliRunner().invoke(app, ['--version'])\n"
        "print(result.output); sys.exit(result.exit_code)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env=_child_env(tmp_path), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
