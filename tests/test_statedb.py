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
    # Track 91A migration 2: rebuildable pull-ledger tables.
    "peer_base": [
        ("peer", "TEXT", 1, None, 1),
        ("source", "TEXT", 1, None, 2),
        ("path", "TEXT", 1, None, 3),
        ("sha", "TEXT", 1, None, 0),
    ],
    "seen_version": [
        ("source", "TEXT", 1, None, 1),
        ("path", "TEXT", 1, None, 2),
        ("sha", "TEXT", 1, None, 3),
        ("time", "INTEGER", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("deleted_by", "TEXT", 0, None, 0),
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
    "peer_base": [("sqlite_autoindex_peer_base_1", 1, "pk", 0), ("peer_base_path", 0, "c", 0)],
    "seen_version": [
        ("sqlite_autoindex_seen_version_1", 1, "pk", 0),
        ("seen_version_time", 0, "c", 0),
    ],
}


def test_schema_settings_and_reopen():
    for _ in range(2):
        with statedb.connect(read_only=False) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (2,)
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
            for table in (
                "ledger",
                "cursors",
                "own_log",
                "displaced",
                "peer_base",
                "seen_version",
            ):
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
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
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
        conn.execute("PRAGMA user_version=3")  # newer than this build (2) but compatible
        conn.execute("DROP TABLE ledger")
        conn.execute("CREATE TABLE ledger (future TEXT)")
    for read_only in (True, False):
        with statedb.connect(read_only=read_only) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (3,)
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
    assert conn.execute('PRAGMA user_version').fetchone() == (2,)
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
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
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
                newer.execute("PRAGMA user_version = 3")
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


# ── Track 91A: the pull ledger tables ─────────────────────────────────

SHA_A = "a" * 16
SHA_B = "b" * 16
FULL = "c" * 64


def _commit(conn, *, bases=None, drop_bases=(), seen=(), unstamp=(), now_us=10_000):
    with statedb.transaction(conn):
        statedb.commit_ledger(
            conn,
            bases=bases or {},
            drop_bases=set(drop_bases),
            seen=list(seen),
            unstamp=set(unstamp),
            now_us=now_us,
        )


def _seen_rows(conn):
    return conn.execute(
        "SELECT source, path, sha, time, kind, deleted_by FROM seen_version ORDER BY path, sha"
    ).fetchall()


def test_migration_two_keeps_compat_and_a_v1_database_reads_silently():
    # A v1 database (migration 1 only), as written by every earlier release.
    statedb.DB_DIR.mkdir(parents=True)
    raw = sqlite3.connect(statedb.db_path())
    for statement in statedb.MIGRATIONS[0][0]:
        raw.execute(statement)
    raw.execute("INSERT INTO meta (key, value) VALUES ('compat_version', '1')")
    raw.execute("INSERT INTO history (verb, action) VALUES ('pull', 'written')")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    before = statedb.db_path().read_bytes()

    # Inspection never migrates and reads "no base, nothing seen" without a notice.
    with statedb.connect(read_only=True) as conn:
        assert statedb.read_peer_bases(conn, "peerpeer") == {}
        assert statedb.read_seen(conn) == {}
        assert statedb.ledger_counts(conn) == ({}, 0)
    assert statedb.db_path().read_bytes() == before

    with statedb.connect(read_only=False) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
        # No compat bump: a release that only knows version 1 keeps working.
        assert conn.execute("SELECT value FROM meta WHERE key='compat_version'").fetchone() == (
            "1",
        )
        assert conn.execute("SELECT count(*) FROM history").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM peer_base").fetchone() == (0,)


def test_absent_database_reads_as_no_ledger_and_creates_nothing():
    with statedb.connect(read_only=True) as conn:
        assert conn is None
        assert statedb.read_peer_bases(conn, "peerpeer") == {}
        assert statedb.read_seen(conn) == {}
        assert statedb.ledger_counts(conn) == ({}, 0)
    assert not statedb.DB_DIR.exists()


def test_deleted_copies_dir_sits_beside_the_database():
    assert statedb.deleted_copies_dir() == statedb.DB_DIR / "deleted"


def test_commit_requires_the_callers_transaction():
    with statedb.connect(read_only=False) as conn:
        with pytest.raises(statedb.StateDBError, match="requires a transaction"):
            statedb.commit_ledger(
                conn, bases={}, drop_bases=set(), seen=[], unstamp=set(), now_us=1
            )


def test_bases_round_trip_per_peer_and_write_only_changed_rows():
    with statedb.connect(read_only=False) as conn:
        _commit(
            conn,
            bases={("peer1", "notes", "a.md"): FULL, ("peer2", "notes", "a.md"): "d" * 64},
        )
        assert statedb.read_peer_bases(conn, "peer1") == {("notes", "a.md"): FULL}
        assert statedb.read_peer_bases(conn, "peer2") == {("notes", "a.md"): "d" * 64}
        assert statedb.read_peer_bases(conn, "nobody") == {}

        # Re-committing identical rows changes no row (a no-change pull writes nothing).
        before = conn.total_changes
        _commit(conn, bases={("peer1", "notes", "a.md"): FULL})
        assert conn.total_changes == before

        _commit(conn, bases={("peer1", "notes", "a.md"): "e" * 64})
        assert conn.total_changes == before + 1
        assert statedb.read_peer_bases(conn, "peer1") == {("notes", "a.md"): "e" * 64}


def test_drop_bases_clears_a_path_across_every_peer_only():
    with statedb.connect(read_only=False) as conn:
        _commit(
            conn,
            bases={
                ("peer1", "notes", "a.md"): FULL,
                ("peer2", "notes", "a.md"): FULL,
                ("peer1", "notes", "b.md"): FULL,
                ("peer1", "other", "a.md"): FULL,
            },
        )
        _commit(conn, drop_bases={("notes", "a.md")})
        assert statedb.read_peer_bases(conn, "peer1") == {
            ("notes", "b.md"): FULL,
            ("other", "a.md"): FULL,
        }
        assert statedb.read_peer_bases(conn, "peer2") == {}


def test_drop_and_new_base_in_one_commit_keeps_the_new_one():
    with statedb.connect(read_only=False) as conn:
        _commit(conn, bases={("peer1", "notes", "a.md"): FULL})
        _commit(conn, drop_bases={("notes", "a.md")}, bases={("peer1", "notes", "a.md"): "e" * 64})
        assert statedb.read_peer_bases(conn, "peer1") == {("notes", "a.md"): "e" * 64}


def test_held_outranks_seen_and_never_goes_back():
    with statedb.connect(read_only=False) as conn:
        _commit(conn, seen=[("notes", "a.md", SHA_A, "seen", 100, None)], now_us=200)
        assert _seen_rows(conn) == [("notes", "a.md", SHA_A, 100, "seen", None)]
        # held outranks seen and moves the row's time.
        _commit(conn, seen=[("notes", "a.md", SHA_A, "held", 150, None)], now_us=200)
        assert _seen_rows(conn) == [("notes", "a.md", SHA_A, 150, "held", None)]
        # A later "seen" for a held version changes nothing.
        before = conn.total_changes
        _commit(conn, seen=[("notes", "a.md", SHA_A, "seen", 190, None)], now_us=200)
        assert conn.total_changes == before
        assert _seen_rows(conn) == [("notes", "a.md", SHA_A, 150, "held", None)]
        # A repeated "seen" for a seen version keeps its first time.
        _commit(conn, seen=[("notes", "a.md", SHA_B, "seen", 120, None)], now_us=200)
        _commit(conn, seen=[("notes", "a.md", SHA_B, "seen", 180, None)], now_us=200)
        assert _seen_rows(conn)[1] == ("notes", "a.md", SHA_B, 120, "seen", None)


def test_a_stamp_is_never_overwritten_by_none_and_unstamp_clears_it():
    with statedb.connect(read_only=False) as conn:
        _commit(conn, seen=[("notes", "a.md", SHA_A, "held", 100, "origin01")], now_us=200)
        assert _seen_rows(conn)[0][5] == "origin01"
        # Re-holding the same version without a stamp keeps the stamp.
        _commit(conn, seen=[("notes", "a.md", SHA_A, "held", 150, None)], now_us=200)
        assert _seen_rows(conn)[0] == ("notes", "a.md", SHA_A, 150, "held", "origin01")
        # Another origin stamps it: the stamp changes even for a "seen" row.
        _commit(conn, seen=[("notes", "a.md", SHA_A, "seen", 160, "origin02")], now_us=200)
        assert _seen_rows(conn)[0][5] == "origin02"
        _commit(conn, unstamp={("notes", "a.md")}, now_us=200)
        assert _seen_rows(conn)[0][5] is None
        # Unstamp touches only that path.
        _commit(
            conn,
            seen=[("notes", "b.md", SHA_A, "held", 100, "origin01")],
            now_us=200,
        )
        _commit(conn, unstamp={("notes", "a.md")}, now_us=200)
        assert [r[5] for r in _seen_rows(conn)] == [None, "origin01"]


def test_rows_older_than_a_year_are_pruned_at_commit():
    year = statedb.SEEN_MAX_AGE_US
    now = year * 3
    with statedb.connect(read_only=False) as conn:
        _commit(
            conn,
            seen=[
                ("notes", "old.md", SHA_A, "held", now - year - 1, None),
                ("notes", "new.md", SHA_A, "held", now - year + 1, None),
            ],
            now_us=now,
        )
        assert [r[1] for r in _seen_rows(conn)] == ["new.md"]


def test_read_seen_and_counts_group_by_path_oldest_first():
    with statedb.connect(read_only=False) as conn:
        _commit(
            conn,
            bases={("peer1", "notes", "a.md"): FULL, ("peer1", "notes", "b.md"): FULL},
            seen=[
                ("notes", "a.md", SHA_B, "held", 300, None),
                ("notes", "a.md", SHA_A, "seen", 200, "origin01"),
            ],
            now_us=400,
        )
        assert statedb.read_seen(conn) == {
            ("notes", "a.md"): [(SHA_A, "seen", 200, "origin01"), (SHA_B, "held", 300, None)]
        }
        assert statedb.ledger_counts(conn) == ({"peer1": 2}, 2)


def test_a_failed_commit_rolls_every_part_back():
    with statedb.connect(read_only=False) as conn:
        _commit(conn, bases={("peer1", "notes", "a.md"): FULL})
        with pytest.raises(statedb.StateDBError):
            # A NULL sha violates NOT NULL after the earlier statements ran.
            _commit(
                conn,
                drop_bases={("notes", "a.md")},
                bases={("peer1", "notes", "b.md"): None},
            )
        assert statedb.read_peer_bases(conn, "peer1") == {("notes", "a.md"): FULL}


# `core` wraps the table API: these pin the failure classes a pull reports.


def test_core_load_and_commit_report_each_database_failure_class(monkeypatch):
    from contextlib import contextmanager

    from mind_meld import core, pullplan

    ledger, exc = core.load_ledger(["peerpeer"])
    assert exc is None and ledger.readable  # absent database: a fresh machine
    assert core.ledger_cause(None) is None

    for read_only_exc, word in (
        (statedb.StateDBBusy("SQLITE_BUSY"), "busy"),
        (statedb.StateDBTooNew(9), "too-new"),
        (statedb.StateDBCorrupt("SQLITE_CORRUPT"), "corrupt"),
        (statedb.StateDBHotJournal("SQLITE_READONLY_ROLLBACK"), "interrupted"),
        (statedb.StateDBUnavailable("sqlite_unavailable"), "unavailable"),
        (statedb.StateDBError("SQLITE_IOERR"), "unreadable"),
        (OSError("disk"), "unreadable"),
    ):

        @contextmanager
        def broken(*, read_only, _exc=read_only_exc):
            raise _exc
            yield

        with monkeypatch.context() as patch:
            patch.setattr(statedb, "connect", broken)
            ledger, exc = core.load_ledger(["peerpeer"])
            assert not ledger.readable and exc is read_only_exc
            assert core.ledger_cause(exc) == word
            # An unreadable ledger commits nothing and is not an error to report twice.
            assert core.commit_ledger(ledger) is None
            fresh = pullplan.Ledger()
            fresh.settle("peerpeer", "notes", "a.md", FULL)
            failure = core.commit_ledger(fresh)
            assert core.ledger_cause(failure) == word
            assert fresh.dirty  # nothing was recorded, so the next pull re-derives it


def test_core_commit_persists_dirty_rows_once_and_marks_them_committed():
    from mind_meld import core, pullplan

    ledger, _ = core.load_ledger(["peerpeer"])
    ledger.settle("peerpeer", "notes", "a.md", FULL)
    assert ledger.dirty
    assert core.commit_ledger(ledger) is None
    assert not ledger.dirty
    again, exc = core.load_ledger(["peerpeer"])
    assert exc is None
    assert again.base("peerpeer", "notes", "a.md") == FULL
    assert again.newest_held("notes", "a.md") == FULL[:16]
    assert isinstance(again, pullplan.Ledger)


def test_core_load_ledger_is_unreadable_for_a_damaged_database():
    from mind_meld import core

    statedb.DB_DIR.mkdir(parents=True)
    statedb.db_path().write_bytes(b"not a database")
    ledger, exc = core.load_ledger(["peerpeer"])
    assert not ledger.readable
    assert core.ledger_cause(exc) == "corrupt"
    # An unreadable ledger answers "no base, nothing seen": today's rule runs.
    assert ledger.base("peerpeer", "notes", "a.md") is None
    assert ledger.entries("notes", "a.md") == {}
