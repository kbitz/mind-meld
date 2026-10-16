"""Table-backed history and lossless legacy import contracts."""

from __future__ import annotations

import json
import os
import stat

import pytest

from mind_meld import pullhistory, statedb


@pytest.fixture(autouse=True)
def _isolate_history_dir(tmp_path, monkeypatch):
    """Redirect HISTORY_DIR so tests never touch the user's real history."""
    iso = tmp_path / "mm_state"
    monkeypatch.setattr("mind_meld.pullhistory.HISTORY_DIR", iso)


def _live_path() -> "os.PathLike":
    return pullhistory.history_path()


def _rotated_path() -> "os.PathLike":
    p = pullhistory.history_path()
    return p.with_name(p.name + pullhistory.ROTATED_SUFFIX)


class TestAppend:
    def test_append_writes_state_db_not_patched_legacy_dir(self, monkeypatch, tmp_path):
        redirected = tmp_path / "late-patch"
        monkeypatch.setattr(pullhistory, "HISTORY_DIR", redirected)

        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="memory/role.md",
            action="written",
        )

        assert pullhistory.history_path() == redirected / "pull-history.jsonl"
        assert statedb.db_path().exists()
        assert not pullhistory.history_path().exists()

    def test_creates_directory_on_first_append(self):
        assert not statedb.DB_DIR.exists()
        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="memory/role.md",
            action="written",
        )
        assert statedb.DB_DIR.is_dir()
        assert statedb.db_path().exists()

    def test_append_writes_one_row_per_record(self):
        for i in range(3):
            pullhistory.append(
                verb="pull",
                device="dev-a",
                source="claude",
                rel_path=f"memory/file-{i}.md",
                action="written",
            )
        assert len(pullhistory.read_records()) == 3
        assert not _live_path().exists()

    def test_appended_record_has_required_schema_keys(self):
        pullhistory.append(
            verb="push",
            device="dev-b",
            source="gstack",
            rel_path="config.yaml",
            action="uploaded",
            local_sha="abc123",
        )
        rec = pullhistory.read_records()[0]
        assert rec["verb"] == "push"
        assert rec["device"] == "dev-b"
        assert rec["source"] == "gstack"
        assert rec["rel_path"] == "config.yaml"
        assert rec["action"] == "uploaded"
        assert rec["local_sha"] == "abc123"
        assert "ts" in rec

    def test_optional_fields_omitted_when_unset(self):
        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="memory/role.md",
            action="skipped",
        )
        rec = pullhistory.read_records()[0]
        assert "local_sha" not in rec
        assert "remote_sha" not in rec

    def test_new_rows_reject_sidecar_key(self):
        payload = pullhistory.row(
            verb="pull", device="d", source="s", rel_path="p", action="conflicted"
        ) | {"sidecar": "x"}
        with statedb.connect(read_only=False) as conn:
            with pytest.raises(ValueError), statedb.transaction(conn):
                pullhistory.write_rows(conn, [payload])
            assert conn.execute("SELECT count(*) FROM history").fetchone() == (0,)

    def test_file_perms_are_0600(self):
        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="memory/role.md",
            action="written",
        )
        mode = stat.S_IMODE(statedb.db_path().stat().st_mode)
        assert mode == 0o600

    def test_failure_to_create_dir_does_not_raise(self, monkeypatch):
        """append() must never raise — history is forensic only."""

        def boom(*_a, **_kw):
            raise OSError("simulated")

        monkeypatch.setattr("pathlib.Path.mkdir", boom)
        # Must not raise
        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="x",
            action="written",
        )


class TestReadRecords:
    def test_invalid_utf8_line_is_skipped_not_raised(self):
        """v0.12.16 T3: `_yield_lines` read text-mode guarded only by
        OSError, so one invalid UTF-8 byte raised UnicodeDecodeError out of
        the generator into the caller's frame — inconsistent with the
        forensic-reader stance every other branch of this function takes.

        Bad byte in the MIDDLE: the rows on both sides must survive, which
        also proves the tolerance is per-line rather than a bail-out.
        """
        live = _live_path()
        live.parent.mkdir(parents=True, exist_ok=True)

        def _rec(rel_path: str) -> bytes:
            return (
                json.dumps(
                    {
                        "ts": "2026-08-14T00:00:00+00:00",
                        "verb": "pull",
                        "device": "dev-a",
                        "source": "claude",
                        "rel_path": rel_path,
                        "action": "written",
                    }
                ).encode()
                + b"\n"
            )

        live.write_bytes(_rec("before.md") + b'{"rel_path":"\xe9bad"}\n' + _rec("after.md"))
        recs = list(pullhistory.read_records())
        assert [r["rel_path"] for r in recs] == ["before.md", "after.md"]

    def test_read_returns_records_in_file_order(self):
        for i in range(3):
            pullhistory.append(
                verb="pull",
                device="dev-a",
                source="claude",
                rel_path=f"f-{i}.md",
                action="written",
            )
        recs = list(pullhistory.read_records())
        assert [r["rel_path"] for r in recs] == ["f-0.md", "f-1.md", "f-2.md"]

    def test_read_yields_rotated_then_live(self):
        """Reader yields rotated `.1` records before live records."""
        # Hand-built legacy inputs for the pre-import fallback reader,
        # which reads `.1` before the live file.
        rotated = _rotated_path()
        live = _live_path()
        rotated.parent.mkdir(parents=True, exist_ok=True)

        def _rec(rel_path: str) -> str:
            return json.dumps(
                {
                    "ts": "2026-04-25T00:00:00+00:00",
                    "verb": "pull",
                    "device": "dev-a",
                    "source": "claude",
                    "rel_path": rel_path,
                    "action": "written",
                },
                sort_keys=True,
            )

        rotated.write_text("\n".join(_rec(f"old-{i}.md") for i in range(3)) + "\n")
        live.write_text("\n".join(_rec(f"new-{i}.md") for i in range(2)) + "\n")

        recs = list(pullhistory.read_records())
        rels = [r["rel_path"] for r in recs]
        # Rotated content (older) appears first.
        assert rels == ["old-0.md", "old-1.md", "old-2.md", "new-0.md", "new-1.md"]

    def test_read_tolerates_corrupt_first_line_in_rotated(self):
        """Crash-mid-rotate may leave a torn first line in .1.
        Reader skips it and continues."""
        rotated = _rotated_path()
        rotated.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(
            {
                "ts": "2026-04-25T00:00:00+00:00",
                "verb": "pull",
                "device": "dev-a",
                "source": "claude",
                "rel_path": "good.md",
                "action": "written",
            },
            sort_keys=True,
        )
        # First line is corrupt JSON; remaining lines are valid.
        rotated.write_text(f"{{not-valid-json\n{good}\n{good}\n")
        recs = list(pullhistory.read_records())
        assert len(recs) == 2
        assert all(r["rel_path"] == "good.md" for r in recs)

    def test_read_skips_corrupt_lines_in_live(self):
        live = _live_path()
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text("not-json\n" + json.dumps({"verb": "pull", "rel_path": "x"}) + "\n")
        recs = list(pullhistory.read_records())
        assert len(recs) == 1

    def test_read_returns_empty_when_no_files(self):
        assert list(pullhistory.read_records()) == []


def add(path="file", **kwargs):
    pullhistory.append(
        verb="pull", device="dev", source="s", rel_path=path, action="written", **kwargs
    )


def legacy(rows, *, rotated=False):
    path = _rotated_path() if rotated else _live_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def test_standalone_import_once_lossless():
    rows = [
        {
            "ts": "2026-01-01",
            "verb": "pull",
            "action": None,
            "source": 1,
            "rel_path": "\udcff",
            "sidecar": ["old"],
            "unknown": {"a": True},
        },
        {"ts": "2026-01-02", "verb": "self-upgrade", "old_version": "1", "new_version": "2"},
    ]
    legacy(rows[:1], rotated=True)
    _rotated_path().write_bytes(b"torn first line\n\xff\n" + _rotated_path().read_bytes())
    legacy(rows[1:])
    add("\udcff")
    add("second")
    assert pullhistory.read_records()[:2] == rows
    assert pullhistory.read_records()[2]["rel_path"] == "\udcff"
    with statedb.connect(read_only=True) as conn:
        assert dict(conn.execute("SELECT key,value FROM meta"))["history_imported"]
        stats = json.loads(dict(conn.execute("SELECT key,value FROM meta"))["legacy_jsonl_stat"])
        assert stats["pull-history.jsonl"] == [
            _live_path().stat().st_size,
            _live_path().stat().st_mtime_ns,
        ]
        assert set(stats) == {"pull-history.jsonl", "pull-history.jsonl.1"}
        assert conn.execute("SELECT rel_path FROM history WHERE id=1").fetchone() == (None,)
    assert len(pullhistory.read_records()) == 4


def test_insert_api_outer_rollback_unknown_and_no_maintenance():
    legacy([{"verb": "legacy"}])
    with statedb.connect(read_only=False) as conn:
        with pytest.raises(ValueError), statedb.transaction(conn):
            pullhistory.write_rows(
                conn,
                [
                    pullhistory.row(
                        verb="pull", device="d", source="s", rel_path="p", action="written"
                    )
                ],
            )
            pullhistory.write_rows(conn, [{"unknown": "reject"}])
        assert conn.execute("SELECT count(*) FROM history").fetchone() == (0,)
        assert (
            conn.execute("SELECT value FROM meta WHERE key='history_imported'").fetchone() is None
        )


def test_bad_import_row_skipped_and_marked(monkeypatch):
    legacy([{"bad": True}, {"keep": True}])
    original = pullhistory._insert

    def insert(conn, payload, **kwargs):
        if payload.get("bad"):
            raise ValueError("bad row")
        return original(conn, payload, **kwargs)

    monkeypatch.setattr(pullhistory, "_insert", insert)
    add()
    assert pullhistory.read_records()[0] == {"keep": True}
    assert len(pullhistory.read_records()) == 2
    add()
    assert len(pullhistory.read_records()) == 3


def test_pending_import_all_or_nothing_then_once(monkeypatch):
    import builtins

    legacy([{"rel_path": "old-1"}], rotated=True)
    legacy([{"rel_path": "old-2"}])
    original = builtins.open

    def blocked(path, *args, **kwargs):
        if path == _live_path():
            raise PermissionError(13, "denied")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", blocked)
        add("new-1")
        assert [r["rel_path"] for r in pullhistory.query()] == ["new-1"]
        assert isinstance(pullhistory.query().notices[0], pullhistory.LegacyHistoryPending)
        add("new-2")
    add("new-3")
    assert [r["rel_path"] for r in pullhistory.read_records()] == [
        "old-1",
        "old-2",
        "new-1",
        "new-2",
        "new-3",
    ]
    pullhistory.maintain_best_effort()
    assert len(pullhistory.read_records()) == 5
    assert not pullhistory.query().notices


def test_batch_counts_stamp_nested_and_binding(monkeypatch, tmp_path):
    original = pullhistory.write_rows
    calls = []

    def write(conn, rows):
        calls.append(len(rows))
        return original(conn, rows)

    monkeypatch.setattr(pullhistory, "write_rows", write)
    initial = statedb.db_path()
    with pullhistory.batch():
        for i in range(2500):
            with pullhistory.batch():
                add(str(i))
        monkeypatch.setattr(statedb, "DB_DIR", tmp_path / "different")
    assert calls == [1000, 1000, 500]
    assert not statedb.db_path().exists()
    with statedb.path_scope(initial):
        assert len(pullhistory.read_records()) == 2500
    assert initial.exists()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_batch_flush_on_interrupt(interrupt):
    with pytest.raises(interrupt), pullhistory.batch():
        add()
        raise interrupt()
    assert len(pullhistory.read_records()) == 1


def test_empty_batch_and_failure_command_budget(monkeypatch, capsys):
    from contextlib import contextmanager

    calls = []

    @contextmanager
    def broken(**kwargs):
        calls.append(1)
        raise statedb.StateDBBusy("SQLITE_BUSY")
        yield

    monkeypatch.setattr(statedb, "connect", broken)
    with pullhistory.batch():
        pass
    assert not calls
    with pullhistory.command_scope(attended=True):
        add()
        with pullhistory.batch():
            for _ in range(2500):
                add()
        pullhistory.maintain_best_effort()
    assert calls == [1]
    assert (
        capsys.readouterr().err
        == pullhistory.explain(pullhistory.HistoryNotRecorded(statedb.StateDBBusy("SQLITE_BUSY")))
        + "\n"
    )
    with pullhistory.command_scope():
        add()
    assert len(calls) == 2
    assert capsys.readouterr().err == ""


def test_failed_batch_stops_without_scope(monkeypatch):
    calls = []

    def broken(*args):
        calls.append(1)
        raise OSError("full")

    monkeypatch.setattr(pullhistory, "write_rows", broken)
    with pullhistory.batch():
        for _ in range(2500):
            add()
    assert calls == [1]


def test_class_retention_and_unknown_class():
    with statedb.connect(read_only=False) as conn, statedb.transaction(conn):
        for verb, action, count in [
            ("pull", "excluded", 20001),
            ("pull", None, 20001),
            ("future", "receipt", 20001),
            ("pull", "conflicted", 1),
        ]:
            conn.executemany(
                "INSERT INTO history (verb,action) VALUES (?,?)", [(verb, action)] * count
            )
    pullhistory.maintain_best_effort()
    with statedb.connect(read_only=True) as conn:
        counts = {
            (v, a): n
            for v, a, n in conn.execute(
                "SELECT verb,action,count(*) FROM history GROUP BY verb,action"
            )
        }
    assert counts == {
        ("pull", "excluded"): 20000,
        ("pull", None): 20000,
        ("future", "receipt"): 20001,
        ("pull", "conflicted"): 1,
    }


def test_stale_tripwire_rotation_and_moving_aside():
    legacy([{"ts": "2026-01-01"}])
    add()
    assert not pullhistory.query().notices
    _live_path().rename(_rotated_path())
    legacy([{"ts": "2026-02-01"}])
    assert isinstance(pullhistory.query().notices[0], pullhistory.LegacyHistoryDiverged)
    _live_path().unlink()
    assert pullhistory.query().notices  # .1 diverged too
    _rotated_path().unlink()
    assert not pullhistory.query().notices


def test_no_sqlite_writer_and_legacy_fallback(monkeypatch):
    import sys

    legacy([{"verb": "legacy"}])
    monkeypatch.setitem(sys.modules, "sqlite3", None)
    add()
    assert not statedb.db_path().exists()
    assert pullhistory.read_records() == [{"verb": "legacy"}]


def test_unbounded_snapshot_null_ts_ties():
    rows = [
        {"ts": "2026-01-01", "rel_path": str(i)} if i % 3 else {"rel_path": str(i)}
        for i in range(12000)
    ]
    legacy(rows)
    pullhistory.maintain_best_effort()
    expected = sorted(rows, key=lambda r: r.get("ts", ""), reverse=True)
    assert pullhistory.query(limit=0) == expected


def test_append_stamps_before_flush(monkeypatch):
    from datetime import datetime, timezone

    class Clock(datetime):
        current = datetime(2020, 1, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(pullhistory, "datetime", Clock)
    with pullhistory.batch():
        add()
        Clock.current = datetime(2021, 1, 1, tzinfo=timezone.utc)
        assert not statedb.db_path().exists()
    assert pullhistory.read_records()[0]["ts"] == "2020-01-01T00:00:00+00:00"


def test_execute_time_errors_translate():
    import sqlite3

    class Broken:
        in_transaction = True

        def execute(self, *args):
            exc = sqlite3.DatabaseError("broken")
            exc.sqlite_errorcode = 779
            exc.sqlite_errorname = "SQLITE_CORRUPT_INDEX"
            raise exc

    with pytest.raises(statedb.StateDBCorrupt):
        pullhistory.write_rows(
            Broken(),
            [pullhistory.row(verb="pull", device="d", source="s", rel_path="p", action="written")],
        )


def test_concurrent_first_writers_import_once(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    legacy([{"legacy": True}])
    code = """import sys
from pathlib import Path
from mind_meld import statedb, pullhistory
statedb.DB_DIR = pullhistory.HISTORY_DIR = Path(sys.argv[1])
pullhistory.append_self_upgrade(device='d', old_version='1', new_version='2')
"""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parent)
    children = [
        subprocess.Popen([sys.executable, "-c", code, str(statedb.DB_DIR)], env=env)
        for _ in range(2)
    ]
    for child in children:
        assert child.wait(timeout=10) == 0
    records = pullhistory.read_records()
    assert records[0] == {"legacy": True}
    assert len(records) == 3


@pytest.mark.parametrize(
    "kind,body",
    [
        (
            "hot",
            "an interrupted mm left an unfinished history write in /state/state.db; "
            "the next `mm pull` or `mm push` repairs it",
        ),
        (
            "busy",
            "another mm command is writing history to /state/state.db; retry when it finishes",
        ),
        (
            "new",
            "/state/state.db uses history schema v2; this mm reads up to v1; run `mm "
            "update`, and if `mm --version` is already newer, an older mm is first "
            "on PATH (`which -a mm`)",
        ),
        (
            "unavailable",
            "mm's Python has no usable sqlite3 module; reinstall mm as described in "
            "the README Install section",
        ),
        (
            "corrupt",
            "/state/state.db is damaged (SQLITE_CORRUPT); with no mm running, move "
            "state.db and every state.db-* file aside together; history recorded "
            "since the import is lost and the next write re-imports the legacy JSONL",
        ),
        (
            "other",
            "/state/state.db: SQLITE_FULL; mm could not read or write its history "
            "database; see the README section",
        ),
        (
            "pending",
            "legacy history in /state/pull-history.jsonl is not imported yet "
            "(EACCES); it is still in that file",
        ),
        (
            "stale",
            "rows written after 2026-01-01 by an older mm are in "
            "~/.config/mind-meld/pull-history.jsonl or pull-history.jsonl.1 and not "
            "shown here; fix PATH (which -a mm, README#upgrading), then move both files "
            "aside to silence this",
        ),
    ],
)
def test_exact_message_table(kind, body):
    from pathlib import Path

    path = Path("/state/state.db")
    exceptions = {
        "hot": statedb.StateDBHotJournal("SQLITE_READONLY_ROLLBACK", path),
        "busy": statedb.StateDBBusy("SQLITE_BUSY", path),
        "new": statedb.StateDBTooNew(2, path),
        "unavailable": statedb.StateDBUnavailable("sqlite_unavailable", path),
        "corrupt": statedb.StateDBCorrupt("SQLITE_CORRUPT", path),
        "other": statedb.StateDBError("SQLITE_FULL", path),
        "pending": pullhistory.LegacyHistoryPending(Path("/state/pull-history.jsonl"), "EACCES"),
        "stale": pullhistory.LegacyHistoryDiverged("2026-01-01"),
    }
    suffix = ". See https://github.com/kbitz/mind-meld#history-database-statedb"
    prefix = "mm: notice: " if kind in ("pending", "stale") else "mm: error: "
    assert pullhistory.explain(exceptions[kind]) == prefix + body + suffix
    if kind not in ("pending", "stale"):
        expected = (
            "mm: notice: history not recorded (sync unaffected; these rows are dropped "
            "and not replayed): " + body + suffix
        )
        assert pullhistory.explain(pullhistory.HistoryNotRecorded(exceptions[kind])) == expected


def test_documented_history_recipes_execute(monkeypatch):
    import re
    from pathlib import Path

    doc = Path(__file__).parents[1] / "docs/invariants/sync.md"
    section = doc.read_text().split("### History API recipes\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```python\n(.*?)```", section, re.S)
    assert len(blocks) == 3
    namespace = {}
    for block in blocks:
        exec(compile(block, str(doc), "exec"), namespace)
    assert len(pullhistory.read_records()) == 2

    # 75A's committed row survives a separate maintenance failure.
    def broken(*args, **kwargs):
        raise OSError("maintenance failed")

    monkeypatch.setattr(pullhistory, "import_if_needed", broken)
    exec(compile(blocks[-1], str(doc), "exec"), namespace)
    assert len(pullhistory.read_records()) == 3


def test_environmental_insert_failure_aborts_whole_import(monkeypatch):
    import sqlite3

    rows = [{"rel_path": f"old-{i}"} for i in range(400)]
    rows[10] = {"rel_path": "x" * 300_000}
    legacy(rows)
    with statedb.connect(read_only=False) as conn:
        base = conn.execute("PRAGMA page_count").fetchone()[0]
    original = sqlite3.connect

    def limited(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.execute(f"PRAGMA max_page_count = {base + 40}")
        return conn

    monkeypatch.setattr(sqlite3, "connect", limited)
    add("dropped")
    monkeypatch.setattr(sqlite3, "connect", original)
    with statedb.connect(read_only=True) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        assert conn.execute("SELECT count(*) FROM history").fetchone() == (0,)
    assert "history_imported" not in meta
    add("kept")
    assert [r["rel_path"] for r in pullhistory.read_records()] == [
        *(r["rel_path"] for r in rows),
        "kept",
    ]


def test_sqlite_row_error_skips_only_that_legacy_row(monkeypatch):
    # Generated by /ship test coverage audit (Track 73B).
    # Value: protects=a legacy row SQLite rejects with a row-level code (TOOBIG) is
    # skipped while the rest import and the marker is set; fails_when=_ROW_ERRORS
    # is narrowed so a row-level SQLite error aborts the import; why_new=
    # test_bad_import_row_skipped_and_marked injects a code-less ValueError only;
    # seam=none; no_claim=SQLite's default 1 GB limit (a lowered length limit
    # stands in for an oversized row)
    import sqlite3

    legacy([{"rel_path": "old-0"}, {"rel_path": "x" * 5000}, {"rel_path": "old-2"}])
    original = sqlite3.connect

    def limited(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
        return conn

    monkeypatch.setattr(sqlite3, "connect", limited)
    add("new")
    monkeypatch.setattr(sqlite3, "connect", original)
    rows = pullhistory.query(limit=0)
    assert rows.origin == "state.db"
    assert [r["rel_path"] for r in pullhistory.read_records()] == ["old-0", "old-2", "new"]


def test_import_tripwire_covers_rows_appended_during_import(monkeypatch):
    legacy([{"rel_path": "old-1"}])
    original = pullhistory._yield_lines

    def racing(path, **kwargs):
        yield from original(path, **kwargs)
        if path == _live_path():
            with open(path, "a") as file:
                file.write(json.dumps({"rel_path": "stale-binary"}) + "\n")

    monkeypatch.setattr(pullhistory, "_yield_lines", racing)
    add("new-1")
    monkeypatch.setattr(pullhistory, "_yield_lines", original)
    rows = pullhistory.query(limit=0)
    assert any(r.get("rel_path") == "stale-binary" for r in rows) or any(
        isinstance(n, pullhistory.LegacyHistoryDiverged) for n in rows.notices
    )


def test_deeply_nested_legacy_line_is_skipped():
    path = _live_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"[" * 200_000 + b"\n" + json.dumps({"rel_path": "good"}).encode() + b"\n")
    assert [r["rel_path"] for r in pullhistory.query()] == ["good"]
    add("new")
    assert [r["rel_path"] for r in pullhistory.read_records()] == ["good", "new"]


def test_explain_names_foreign_exception():
    message = pullhistory.explain(pullhistory.HistoryNotRecorded(AttributeError("boom")))
    assert "AttributeError: boom" in message
    assert ": None;" not in message


def test_compat_rechecked_inside_read_snapshot(monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    add()
    original = statedb.connect

    @contextmanager
    def racing(*, read_only):
        with original(read_only=read_only) as conn:
            if read_only and conn is not None:
                with sqlite3.connect(statedb.db_path()) as writer:
                    writer.execute("UPDATE meta SET value='2' WHERE key='compat_version'")
                writer.close()
            yield conn

    monkeypatch.setattr(statedb, "connect", racing)
    with pytest.raises(statedb.StateDBTooNew):
        pullhistory.query()


def test_huge_limit_and_unencodable_filters():
    add("row")
    assert [r["rel_path"] for r in pullhistory.query(limit=10**20)] == ["row"]
    for key in ("source", "action", "verb"):
        rows = pullhistory.query(**{key: "\udcff"})
        assert rows == [] and rows.origin == "state.db"


@pytest.mark.parametrize("target", ["extra", "stat", "deep-stat", "compat"])
def test_malformed_stored_values_are_corrupt(target):
    legacy([{"rel_path": "old"}])
    add()
    with statedb.connect(read_only=False) as conn:
        if target == "extra":
            conn.execute("UPDATE history SET extra='[1]'")
        elif target == "stat":
            conn.execute("UPDATE meta SET value='{' WHERE key='legacy_jsonl_stat'")
        elif target == "deep-stat":
            conn.execute("UPDATE meta SET value=? WHERE key='legacy_jsonl_stat'", ("[" * 200_000,))
        else:
            conn.execute("UPDATE meta SET value='one' WHERE key='compat_version'")
    with pytest.raises(statedb.StateDBCorrupt):
        pullhistory.query()


def test_flush_prunes_each_class_to_newest(monkeypatch):
    monkeypatch.setattr(pullhistory, "HISTORY_CLASS_LIMIT", 3)
    for i in range(5):
        add(str(i))
    assert [r["rel_path"] for r in pullhistory.read_records()] == ["2", "3", "4"]
    with pullhistory.batch():
        for i in range(5, 10):
            add(str(i))
    assert [r["rel_path"] for r in pullhistory.read_records()] == ["7", "8", "9"]


def test_migrated_but_unimported_database_reads_legacy():
    legacy([{"verb": "pull", "rel_path": "legacy"}])
    with statedb.connect(read_only=False):
        pass  # migration committed; the flush transaction then failed
    rows = pullhistory.query()
    assert rows.origin == "legacy pull-history.jsonl"
    assert [r["rel_path"] for r in rows] == ["legacy"]


def test_write_rows_committed_never_imports_or_prunes(monkeypatch):
    legacy([{"verb": "legacy"}])
    monkeypatch.setattr(pullhistory, "HISTORY_CLASS_LIMIT", 1)
    rows = [
        pullhistory.row(verb="pull", device="d", source="s", rel_path=str(i), action="written")
        for i in range(2)
    ]
    with statedb.connect(read_only=False) as conn:
        with statedb.transaction(conn):
            pullhistory.write_rows(conn, rows)
        assert conn.execute("SELECT count(*) FROM history").fetchone() == (2,)
        assert conn.execute("SELECT 1 FROM meta WHERE key='history_imported'").fetchone() is None


def test_racing_first_writer_imports_once(monkeypatch):
    from contextlib import contextmanager

    legacy([{"legacy": True}])
    original = statedb.transaction
    seen = []

    @contextmanager
    def transaction(conn):
        seen.append(1)
        if len(seen) == 2:  # our flush, after our migration: the rival wins first
            pullhistory.append_self_upgrade(device="rival", old_version="1", new_version="2")
        with original(conn):
            yield

    monkeypatch.setattr(statedb, "transaction", transaction)
    add("ours")
    records = pullhistory.read_records()
    assert records[0] == {"legacy": True}
    assert [r.get("device") for r in records[1:]] == ["rival", "dev"]


def test_additive_history_column_keeps_reading_and_writing():
    add("before")
    with statedb.connect(read_only=False) as conn:
        conn.execute("ALTER TABLE history ADD COLUMN receipt TEXT")
        conn.execute("PRAGMA user_version = 2")
    add("after")
    assert [r["rel_path"] for r in pullhistory.read_records()] == ["before", "after"]
    assert pullhistory.query().origin == "state.db"


def test_since_keeps_microsecond_precision():
    from datetime import datetime, timezone

    stamp = datetime(2026, 1, 2, 3, 4, 5, 123457, tzinfo=timezone.utc)
    add("at", ts=stamp)
    assert pullhistory._timestamp_us(stamp.isoformat()) == 1767323045123457
    assert [r["rel_path"] for r in pullhistory.query(since=stamp)] == ["at"]
    later = datetime(2026, 1, 2, 3, 4, 5, 123458, tzinfo=timezone.utc)
    assert pullhistory.query(since=later) == []
