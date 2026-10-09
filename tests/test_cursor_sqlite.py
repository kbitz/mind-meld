"""Conductor SQLite store reader (Track 70A).

Temp databases are built from the 2026-10-09 census rows and the producer DDL; none
is committed. Nothing here opens the real Conductor store, ``~/.cursor`` or
``~/.config/mind-meld``: conftest redirects every host path, and the reader itself
raises under pytest if a test reaches the account's real store.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mind_meld import cli, errors
from mind_meld import host_usage as hu
from mind_meld import token_usage as tu

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "tests/fixtures/host_sessions/cursor/CONTRACT.md"
S1, S2, S3 = "1111111111111111", "2222222222222222", "3333333333333333"
PARAMS = '[{"id":"fast","value":"false"},{"id":"reasoning_effort","value":"xhigh"}]'
RUNS_DDL = """CREATE TABLE runs (
 run_id TEXT PRIMARY KEY, request_id TEXT, agent_id TEXT NOT NULL, turn_number INTEGER NOT NULL,
 status TEXT NOT NULL, model TEXT, model_params_json TEXT, start_checkpoint_ref_json TEXT,
 latest_checkpoint_ref_json TEXT, error_code TEXT, usage_ref TEXT, usage_json TEXT, result TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 cancelled_at TEXT, expired_at TEXT)"""
SIBLING_DDL = (
    "CREATE TABLE agents (agent_id TEXT PRIMARY KEY, blob_key TEXT)",
    "CREATE TABLE run_events (run_id TEXT, idempotency_key TEXT, payload TEXT)",
)
# (run_id, request_id, input, output, cacheRead, reasoning, finished_at): the four
# FINISHED rows of the 2026-10-09 census, 598,589 tokens in all.
CENSUS = (
    ("run-16dccfa8-3188-47e6-a329-b0bae16ef858", "b918e504-2fad-48fb-8745-022e99e99928",
     38431, 1389, 22912, 1019, "2026-10-09T12:05:35.088Z"),
    ("run-45353a09-b57a-4908-8f74-4aa52aacd727", "2ae366f2-1777-4b40-b5ec-eab4a1fb48f4",
     155198, 3727, 68608, 1342, "2026-10-09T12:06:52.427Z"),
    ("run-5bc7a28a-5395-4c47-b290-24aeea14dedc", "ffbf493d-e1fc-4b51-af77-c85532b04509",
     115692, 5094, 110848, 3400, "2026-10-09T12:16:24.500Z"),
    ("run-52d56731-c1c6-4b6e-8100-797dc2bdcf96", "727d14ee-98d2-4132-9811-aa2551bd2c5a",
     67571, 1183, 7936, 65, "2026-10-09T12:23:45.204Z"),
)  # fmt: skip
CANCELLED = "run-7a845278-9092-4f69-931e-b1f9388bc0d6"


class RawText(bytes):
    """Bytes stored as TEXT rather than BLOB, so they can be invalid UTF-8."""


@pytest.fixture(autouse=True)
def census_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 9, 18, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(hu, "datetime", Clock)


def usage(inp=100, out=10, read=5, write=0, reasoning=None) -> str:
    body = {
        "inputTokens": inp,
        "outputTokens": out,
        "cacheReadTokens": read,
        "cacheWriteTokens": write,
        "totalTokens": inp + out + read + write,
    }
    if reasoning is not None:
        body["reasoningTokens"] = reasoning
    return json.dumps(body, separators=(",", ":"))


def fin(run_id="run-a", *, at="2026-10-09T12:00:00.000Z", **fields) -> dict:
    base = {
        "run_id": run_id,
        "status": "FINISHED",
        "model": "grok-4.7",
        "model_params_json": PARAMS,
        "usage_json": usage(),
        "finished_at": at,
        "updated_at": at,
    }
    return {**base, **fields}


def insert(conn, **fields) -> None:
    values = {
        "agent_id": "agent-1",
        "turn_number": 1,
        "created_at": "2026-10-09T12:00:00.000Z",
        "updated_at": "2026-10-09T12:00:00.000Z",
        "status": "FINISHED",
        **fields,
    }
    have = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    use = [c for c in have if c in values]
    marks = ["CAST(? AS TEXT)" if isinstance(values[c], RawText) else "?" for c in use]
    args = [bytes(values[c]) if isinstance(values[c], RawText) else values[c] for c in use]
    conn.execute(f"INSERT INTO runs ({', '.join(use)}) VALUES ({', '.join(marks)})", args)


def make_store(root, store, rows=(), *, ddl=RUNS_DDL, keep_open=False, setup=None):
    """Build ``root/store/index.db`` in WAL mode, as Conductor does.

    Closed, it is a checkpointed database with no ``-wal``. With ``keep_open`` the
    returned connection holds an uncheckpointed WAL, so ``-wal`` and ``-shm`` exist."""
    directory = Path(root) / store
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    if ddl:
        conn.execute(ddl)
        for statement in SIBLING_DDL:
            conn.execute(statement)
    for row in rows:
        insert(conn, **row)
    if setup:
        setup(conn)
    if keep_open:
        return conn
    conn.close()
    return directory


def census_rows():
    return [
        fin(run, request_id=req, usage_json=usage(i, o, r, 0, k), at=at)
        for run, req, i, o, r, k, at in CENSUS
    ]


def key(run_id) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()


def read(**kw):
    return hu.read_cursor_usage(hu.CURSOR_STORE_PATH, consented=True, **kw)


def cache() -> dict:
    return json.loads(hu.CURSOR_CACHE_PATH.read_text())


def history() -> dict:
    return {k: (r["day"], r["model"], r["usage"], r["partial"]) for k, r in cache()["runs"].items()}


def total(result) -> int:
    return sum(tu.sum_bucket(day) for day in result.tokens_by_day.values())


def footprint(directory) -> dict:
    """What a read may not change: every file's name, and ``index.db`` and ``-wal``
    byte for byte. ``-shm`` is SQLite's own wal-index: a reader may advance its mtime
    (observed on a quit Conductor's residue) but never its inode or size."""
    out = {}
    for p in sorted(Path(directory).iterdir()):
        info = p.stat()
        if p.name.endswith("-shm"):
            out[p.name] = (info.st_ino, info.st_size)
        else:
            out[p.name] = (info.st_ino, info.st_size, info.st_mtime_ns, p.read_bytes())
    return out


def sqlite_error(code, name, cls=sqlite3.OperationalError):
    exc = cls(name)
    if code is not None:
        exc.sqlite_errorcode = code
        exc.sqlite_errorname = name
    return exc


def failing_query(monkeypatch, exc):
    def boom(*_args):
        raise exc

    monkeypatch.setattr(hu, "_query_cursor_sqlite", boom)


def refused(result, reason, cause=None, store=None, **extra):
    assert not result.complete and result.reason == reason
    detail = cache().get("last_reason_detail")
    if cause is None:
        assert detail is None
    else:
        assert detail == {"cause": cause, "store": store, **extra}


def drifted(result, store, cause):
    assert result.complete and cache()["sqlite_read"]["stores"][store] == cause
    assert cache().get("last_reason") is None


# ── layouts ───────────────────────────────────────────────────────────────


def test_census_stores_count_each_run_once_on_its_terminal_day():
    make_store(hu.CURSOR_STORE_PATH, "6a0374c100b4e8c8", census_rows())
    make_store(hu.CURSOR_STORE_PATH, "a12bae7f256784c5", [
        {"run_id": CANCELLED, "status": "CANCELLED", "model": "grok-4.7",
         "model_params_json": PARAMS, "cancelled_at": "2026-10-09T12:04:54.455Z"},
    ])  # fmt: skip
    result = read()
    assert result.complete and set(result.tokens_by_day) == {"2026-10-09"}
    assert total(result) == 598_589 and set(result.hosts) == {"grok"}
    expected = {
        key(run): (
            "2026-10-09",
            "grok-4.7",
            {"input": i, "cache_create": 0, "cache_read": r, "output": o},
            False,
        )
        for run, _req, i, o, r, _k, _at in CENSUS
    }
    assert history() == expected
    assert cache()["sqlite_read"] == {
        "stores": {"6a0374c100b4e8c8": "read", "a12bae7f256784c5": "read"}
    }
    assert read().hosts == result.hosts and history() == expected  # a second pass adds nothing
    assert cache().get("last_reason") is None


def test_both_formats_in_one_directory_count_a_run_once(cursor_store):
    # The same legacy run reappears from index.db on the same day: one entry, not two.
    legacy = json.loads((cursor_store / "session-1/runs.ndjson").read_text().splitlines()[0])
    prior = read()
    assert prior.complete
    before = history()
    ended = datetime.fromtimestamp(legacy["endedAt"] / 1000, timezone.utc)
    row = fin(
        legacy["runId"],
        at=ended.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ended.microsecond // 1000:03d}Z",
        usage_json=json.dumps(legacy["usage"]),
        model_params_json=json.dumps(legacy["model"]["params"]),
    )
    make_store(cursor_store, S1, [row])
    ledger = cursor_store / "session-1/runs.ndjson"
    (cursor_store / S1 / "runs.ndjson").write_bytes(ledger.read_bytes())
    again = read()
    assert again.complete and history() == before
    assert again.hosts == prior.hosts


@pytest.mark.parametrize("session", ["session-1", "session-3"])
def test_migrated_legacy_fixtures_give_identical_history(cursor_store, session):
    """Conductor's migration deletes runs.ndjson and rewrites each run into index.db."""
    legacy_rows = (cursor_store / session / "runs.ndjson").read_text().splitlines()
    read_legacy = read()
    assert read_legacy.complete
    expected = history()
    for path in cursor_store.glob("*/runs.ndjson"):
        path.unlink()
    rows = []
    for line in legacy_rows:
        legacy = json.loads(line)
        ended = datetime.fromtimestamp(legacy["endedAt"] / 1000, timezone.utc)
        stamp = ended.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ended.microsecond // 1000:03d}Z"
        rows.append(
            fin(
                legacy["runId"],
                at=stamp,
                usage_json=json.dumps(legacy["usage"]),
                model_params_json=json.dumps(legacy["model"]["params"]),
            )
        )
    make_store(cursor_store, S1, rows)
    migrated = read()
    assert migrated.complete and total(migrated) == total(read_legacy)
    assert history() == expected  # labeled regression, not producer evidence


def stamp_of(legacy) -> str:
    ended = datetime.fromtimestamp(legacy["endedAt"] / 1000, timezone.utc)
    return ended.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ended.microsecond // 1000:03d}Z"


def test_one_run_in_both_formats_sharing_a_request_id_is_one_alias(cursor_store):
    legacy = json.loads((cursor_store / "session-1/runs.ndjson").read_text().splitlines()[0])
    legacy["requestId"] = "shared-request"
    ledger = json.dumps(legacy) + "\n"
    (cursor_store / "session-1/runs.ndjson").write_text(ledger)
    assert read().complete
    before = history()
    row = fin(
        legacy["runId"],
        at=stamp_of(legacy),
        request_id="shared-request",
        usage_json=json.dumps(legacy["usage"]),
        model_params_json=json.dumps(legacy["model"]["params"]),
    )
    make_store(cursor_store, S1, [row])
    (cursor_store / S1 / "runs.ndjson").write_text(ledger)
    assert read().complete and history() == before
    assert cache()["requests"][key("shared-request")] == key(legacy["runId"])


def test_two_runs_sharing_a_request_id_across_formats_refuse(cursor_store):
    legacy = json.loads((cursor_store / "session-1/runs.ndjson").read_text().splitlines()[0])
    legacy["requestId"] = "shared-request"
    for path in cursor_store.glob("*/runs.ndjson"):
        path.unlink()
    directory = make_store(
        cursor_store,
        S1,
        [fin("run-new", at="2026-09-23T10:00:00.000Z", request_id="shared-request")],
    )
    (directory / "runs.ndjson").write_text(json.dumps(legacy) + "\n")
    refused(read(), "malformed", "duplicate_request", S1)


def test_counted_jsonl_beats_a_sqlite_placeholder_and_survives_pruning(cursor_store):
    legacy = json.loads((cursor_store / "session-1/runs.ndjson").read_text().splitlines()[0])
    for path in cursor_store.glob("*/runs.ndjson"):
        path.unlink()
    directory = cursor_store / S1
    directory.mkdir()
    (directory / "runs.ndjson").write_text(json.dumps(legacy) + "\n")
    ended = datetime.fromtimestamp(legacy["endedAt"] / 1000, timezone.utc)
    stamp = ended.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    make_store(
        cursor_store, S1, [fin(legacy["runId"], at=stamp, usage_json=None, usage_ref="ref-1")]
    )
    assert read().complete  # fresh cache: counted JSONL outranks the SQLite placeholder
    counted = history()[key(legacy["runId"])]
    assert counted[2] is not None
    for path in directory.iterdir():
        path.unlink()  # both sources pruned
    directory.rmdir()
    assert read().complete and history()[key(legacy["runId"])] == counted


def test_ndjson_vanishing_under_a_sqlite_store_is_dropped(monkeypatch):
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    ledger = directory / "runs.ndjson"
    ledger.write_text("{}\n")
    original = hu._read_cursor_file

    def vanish(path, deadline):
        path.unlink()
        return original(path, deadline)

    monkeypatch.setattr(hu, "_read_cursor_file", vanish)
    assert read().complete and key("run-a") in history()


def test_ndjson_vanishing_without_a_sqlite_store_still_fails(monkeypatch):
    directory = hu.CURSOR_STORE_PATH / S1
    directory.mkdir(parents=True)
    ledger = directory / "runs.ndjson"
    ledger.write_text("{}\n")
    original = hu._read_cursor_file

    def vanish(path, deadline):
        path.unlink()
        return original(path, deadline)

    monkeypatch.setattr(hu, "_read_cursor_file", vanish)
    assert read().reason == "io_error"


def test_ndjson_appearing_after_index_db_is_read_normally(cursor_store):
    legacy = (cursor_store / "session-1/runs.ndjson").read_text()
    for path in cursor_store.glob("*/runs.ndjson"):
        path.unlink()
    directory = make_store(cursor_store, S1, [fin("run-a", at="2026-09-23T10:00:00.000Z")])
    assert read().complete and len(history()) == 1
    (directory / "runs.ndjson").write_text(legacy)
    assert read().complete and len(history()) == 3


def test_cross_directory_conflict_commits_nothing():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete
    before = cache()
    make_store(hu.CURSOR_STORE_PATH, S2, [fin("run-b")])
    make_store(hu.CURSOR_STORE_PATH, S3, [fin("run-b", usage_json=usage(999))])
    refused(read(), "malformed", "conflict", None)
    after = cache()
    assert after["runs"] == before["runs"] and after["sqlite_read"] == before["sqlite_read"]
    assert after["sqlite_retained"] == before["sqlite_retained"]


def test_spaced_path_and_deleted_store_and_foreign_directory(tmp_path):
    root = tmp_path / "Application Support" / "cursor-sdk-store"
    make_store(root, S1, [fin("run-a")])
    foreign = root / "newer-conductor"
    foreign.mkdir()
    (foreign / "index.db").write_bytes(b"not a database")
    result = hu.read_cursor_usage(root, consented=True)
    assert result.complete and set(cache()["sqlite_read"]["stores"]) == {S1}
    assert hu.unread_sqlite_stores(root) == 1  # the foreign name is not Conductor's


def test_store_deleted_between_listing_and_connect_is_skipped(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete
    original = hu._lstat_sqlite_files

    def delete_first(directory, store):
        (directory / "index.db").unlink()
        return original(directory, store)

    monkeypatch.setattr(hu, "_lstat_sqlite_files", delete_first)
    assert read().complete and key("run-a") in history()  # retained history intact
    assert S1 not in cache().get("sqlite_read", {}).get("stores", {})


def test_cantopen_followed_by_enoent_is_a_vanished_store(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    real = sqlite3.connect

    def vanish_then_fail(*args, **kwargs):
        (hu.CURSOR_STORE_PATH / S1 / "index.db").unlink()
        raise sqlite_error(14, "SQLITE_CANTOPEN")

    monkeypatch.setattr(sqlite3, "connect", vanish_then_fail)
    assert read().complete
    monkeypatch.setattr(sqlite3, "connect", real)


def test_first_failing_directory_stops_the_pass_and_earlier_ones_commit():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    broken = hu.CURSOR_STORE_PATH / S2
    broken.mkdir()
    (broken / "index.db").write_bytes(b"this is not a database" * 200)
    make_store(hu.CURSOR_STORE_PATH, S3, [fin("run-c")])
    refused(read(), "malformed", "corrupt_database", S2)
    assert set(history()) == {key("run-a")}
    assert cache()["sqlite_read"] == {"stores": {S1: "read"}}
    assert cache()["sqlite_retained"] == [key("run-a")]


def test_a_drifted_store_does_not_block_good_stores_or_the_spool():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    no_ref = RUNS_DDL.replace(" usage_ref TEXT,", "")
    make_store(hu.CURSOR_STORE_PATH, S2, [fin("run-b")], ddl=no_ref)
    assert hu.record_cursor_usage(
        {
            "hook_event_name": "stop",
            "status": "completed",
            "generation_id": "standalone-1",
            "model": "grok-4.7-low",
            "input_tokens": 50,
            "cache_write_tokens": 0,
            "cache_read_tokens": 10,
            "output_tokens": 5,
        }
    )
    result = read()
    assert result.complete and total(result) == 115 + 55
    assert cache()["sqlite_read"]["stores"] == {S1: "read", S2: "missing_required_column"}
    assert cache().get("last_reason") is None


# ── rows ──────────────────────────────────────────────────────────────────

TERMINAL = {"FINISHED": "finished_at", "CANCELLED": "cancelled_at", "EXPIRED": "expired_at"}


@pytest.mark.parametrize("status", ["FINISHED", "CANCELLED", "EXPIRED", "ERROR"])
def test_every_terminal_status_with_counters_is_counted_on_its_own_timestamp(status):
    column = TERMINAL.get(status, "updated_at")
    fields = {"status": status, "usage_json": usage(), "updated_at": "2026-10-08T01:00:00.000Z"}
    fields[column] = "2026-10-08T01:00:00.000Z"
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", **fields)])
    result = read()
    assert result.complete and set(result.tokens_by_day) == {"2026-10-08"}


def test_error_is_dated_by_updated_at_not_a_terminal_stamp():
    row = fin("run-a", status="ERROR", finished_at="2026-10-01T00:00:00.000Z")
    row["updated_at"] = "2026-10-08T01:00:00.000Z"
    make_store(hu.CURSOR_STORE_PATH, S1, [row])
    assert set(read().tokens_by_day) == {"2026-10-08"}


@pytest.mark.parametrize(
    ("fields", "counts"),
    [
        ({"status": "FINISHED", "usage_json": None, "usage_ref": None}, False),
        ({"status": "CANCELLED", "usage_json": None, "usage_ref": None}, False),
        ({"status": "CANCELLED", "usage_json": None, "usage_ref": "ref"}, False),
        ({"status": "ERROR", "usage_json": None, "usage_ref": "ref"}, False),
        ({"status": "EXPIRED", "usage_json": None, "usage_ref": "ref"}, False),
        ({"status": "QUEUED", "usage_json": None, "usage_ref": None}, False),
        ({"status": "CREATING", "usage_json": None, "usage_ref": None}, False),
        ({"status": "RUNNING", "usage_json": None, "usage_ref": None}, False),
    ],
)
def test_rows_that_contribute_nothing_never_refuse_and_skip_content_checks(fields, counts):
    # No timestamp, no model: a row that contributes nothing is never judged on them.
    row = {"run_id": "run-x", "model": b"\xff\xfe", "created_at": "x", **fields}
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a"), row])
    result = read()
    assert result.complete and set(history()) == {key("run-a")}


def test_usage_ref_only_finished_row_is_a_partial_placeholder():
    row = fin("run-a", usage_json=None, usage_ref="ref-1")
    make_store(hu.CURSOR_STORE_PATH, S1, [row, fin("run-b")])
    result = read()
    assert result.complete and result.partial_days == frozenset({"2026-10-09"})
    assert history()[key("run-a")][2:] == (None, True)


@pytest.mark.parametrize("status", ["QUEUED", "CREATING", "RUNNING"])
@pytest.mark.parametrize("column", ["usage_json", "usage_ref"])
def test_nonterminal_row_with_usage_is_store_scoped_drift(status, column):
    value = usage() if column == "usage_json" else "ref"
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", status=status, **{column: value})])
    drifted(read(), S1, "unknown_status")
    assert history() == {}


def test_unknown_status_is_store_scoped_drift_and_other_stores_still_count():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", status="SUSPENDED")])
    make_store(hu.CURSOR_STORE_PATH, S2, [fin("run-b")])
    drifted(read(), S1, "unknown_status")
    assert set(history()) == {key("run-b")} and cache()["sqlite_read"]["stores"][S2] == "read"


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        "",
        "2026-10-09T12:00:00Z",
        "2026-10-09 12:00:00.000Z",
        "2026-10-09T12:00:00.000+00:00",
        "2026-13-09T12:00:00.000Z",
        "2019-12-31T23:59:59.999Z",
        "2026-10-11T00:00:00.000Z",
        b"\x01\x02",
        7,
    ],
)
def test_contributing_row_with_a_bad_timestamp_refuses(stamp):
    ddl = RUNS_DDL.replace("finished_at TEXT", "finished_at")  # no affinity: keep an int an int
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", finished_at=stamp)], ddl=ddl)
    refused(read(), "malformed", "bad_timestamp", S1)


def test_oversize_text_fields_refuse_by_cap_before_any_decode():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", at="2026-10-09T12:00:00.000Z" + "0" * 40)])
    refused(read(), "malformed", "oversize_or_type", S1)


@pytest.mark.parametrize(
    ("column", "value"),
    [("run_id", "r" * 257), ("run_id", ""), ("run_id", None), ("status", "F" * 257)],
)
def test_oversize_or_empty_identifiers_refuse(column, value):
    fields = fin("run-a")
    fields[column] = value
    make_store(
        hu.CURSOR_STORE_PATH,
        S1,
        [fields],
        ddl=RUNS_DDL.replace("run_id TEXT PRIMARY KEY", "run_id TEXT"),
    )
    refused(read(), "malformed", "oversize_or_type", S1)


def test_missing_reasoning_tokens_is_accepted_but_a_bad_one_is_not():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", usage_json=usage(reasoning=None))])
    assert read().complete and len(history()) == 1


@pytest.mark.parametrize(
    "payload",
    [
        usage(reasoning=999),  # reasoning above output
        json.dumps({"inputTokens": 1}),
        json.dumps({"inputTokens": 1, "outputTokens": 1, "cacheReadTokens": 1,
                    "cacheWriteTokens": 0, "totalTokens": 99}),
        json.dumps({"inputTokens": True, "outputTokens": 0, "cacheReadTokens": 0,
                    "cacheWriteTokens": 0, "totalTokens": 1}),
        json.dumps({"inputTokens": -1, "outputTokens": 1, "cacheReadTokens": 0,
                    "cacheWriteTokens": 0, "totalTokens": 0}),
        "[]",
        '"text"',
        "5",
    ],
)  # fmt: skip
def test_invalid_counters_refuse(payload):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", usage_json=payload)])
    refused(read(), "malformed", "bad_counters", S1)


def test_cache_write_tokens_label_the_day_partial_but_still_count():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", usage_json=usage(write=7))])
    result = read()
    assert result.complete and result.partial_days == frozenset({"2026-10-09"})
    assert history()[key("run-a")][3] is True and total(result) == 122


def test_duplicate_request_id_across_contributing_rows_refuses():
    rows = [fin("run-a", request_id="req"), fin("run-b", request_id="req")]
    make_store(hu.CURSOR_STORE_PATH, S1, rows)
    refused(read(), "malformed", "duplicate_request", S1)


def test_cancel_and_retry_sharing_a_request_id_is_not_a_refusal():
    cancelled = {"run_id": "run-a", "status": "CANCELLED", "request_id": "req", "model": "grok-4.7"}
    make_store(hu.CURSOR_STORE_PATH, S1, [cancelled, fin("run-b", request_id="req")])
    result = read()
    assert result.complete and set(history()) == {key("run-b")}
    assert cache()["requests"] == {key("req"): key("run-b")}


@pytest.mark.parametrize(
    ("model", "params", "expected"),
    [
        (None, None, hu.CURSOR_UNKNOWN_MODEL),
        ("grok-4.7", None, "grok-4.7-unspecified"),
        ("grok-4.7", "[]", "grok-4.7-unspecified"),
        ("grok-4.7", '[{"id":"reasoning_effort","value":"high"}]', "grok-4.7-unspecified"),
        ("grok-4.7", PARAMS, "grok-4.7"),
        ("grok-4.7", '[{"id":"fast","value":"true"}]', "grok-4.7-fast"),
        ("composer-2.5", '[{"id":"fast","value":"maybe"}]', "composer-2.5"),
        ("composer-2.5", "not json at all", "composer-2.5"),
        ("gpt-6-astra", None, "gpt-6-astra"),
    ],
)
def test_model_and_fast_param_mapping(model, params, expected):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", model=model, model_params_json=params)])
    assert read().complete and history()[key("run-a")][1] == expected


@pytest.mark.parametrize(
    "params",
    ['[{"id":"fast","value":"true"},{"id":"fast","value":"false"}]',
     '[{"id":"fast","value":"maybe"}]', '{"id":"fast"}', '"text"', "7"],
)  # fmt: skip
def test_malformed_fast_param_on_grok_is_store_scoped_drift(params):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", model_params_json=params)])
    drifted(read(), S1, "model_params")


@pytest.mark.parametrize("model", [b"\x00\x01", "", "m" * 257, 5])
def test_unusable_model_on_a_contributing_row_refuses(model):
    ddl = RUNS_DDL.replace("model TEXT", "model")
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", model=model)], ddl=ddl)
    refused(read(), "malformed", "bad_model", S1)


# ── retention: SQLite never retracts ──────────────────────────────────────


def test_counterless_revisions_and_failed_reads_keep_retained_counts():
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete
    kept = history()
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    conn.execute("UPDATE runs SET status='CANCELLED', usage_json=NULL, cancelled_at=finished_at")
    conn.close()
    assert read().complete and history() == kept  # a counterless row retracts nothing
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    conn.execute("DELETE FROM runs")
    conn.close()
    assert read().complete and history() == kept  # nor does a vanished row
    (directory / "index.db").write_bytes(b"x" * 8192)
    assert read().reason == "malformed" and history() == kept


def test_counted_then_placeholder_then_pruned_keeps_the_counters():
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", request_id="req")])
    assert read().complete
    counted = history()[key("run-a")]
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    conn.execute("UPDATE runs SET usage_json=NULL, usage_ref='ref'")
    conn.close()
    assert read().complete and history()[key("run-a")] == counted
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()
    assert read().complete and history()[key("run-a")] == counted


def test_a_changed_counted_row_replaces_by_key():
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    conn.execute("UPDATE runs SET usage_json=?", [usage(inp=900)])
    conn.close()
    result = read()
    assert result.complete and total(result) == 915 and len(history()) == 1


# ── schema ────────────────────────────────────────────────────────────────


def test_zero_object_database_contributes_nothing_and_is_recorded_read():
    directory = hu.CURSOR_STORE_PATH / S1
    directory.mkdir(parents=True)
    sqlite3.connect(directory / "index.db").close()
    result = read()
    assert result.complete and cache()["sqlite_read"]["stores"] == {S1: "read"}


@pytest.mark.parametrize(
    ("ddl", "cause"),
    [
        ("CREATE TABLE other (a)", "schema_object"),
        ("CREATE TABLE t (a); CREATE VIEW runs AS SELECT 'r' AS run_id", "schema_object"),
        (RUNS_DDL.replace(" usage_ref TEXT,", ""), "missing_required_column"),  # pre-ALTER
        (RUNS_DDL.replace(", expired_at TEXT", ""), "missing_required_column"),
    ],
)
def test_schema_drift_is_store_scoped(ddl, cause):
    directory = hu.CURSOR_STORE_PATH / S1
    directory.mkdir(parents=True)
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    for statement in ddl.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.close()
    make_store(hu.CURSOR_STORE_PATH, S2, [fin("run-b")])
    drifted(read(), S1, cause)
    assert set(history()) == {key("run-b")}


def test_drift_keeps_the_stores_retained_history():
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete
    kept = history()
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    conn.execute("ALTER TABLE runs DROP COLUMN usage_ref")
    conn.close()
    drifted(read(), S1, "missing_required_column")
    assert history() == kept


def test_blob_in_a_selected_column_is_column_type_drift():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", usage_json=b"\x00\x01blob")])
    drifted(read(), S1, "column_type")


def test_integer_in_a_selected_column_is_column_type_drift():
    ddl = RUNS_DDL.replace("status TEXT NOT NULL", "status NOT NULL")
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", status=5)], ddl=ddl)
    drifted(read(), S1, "column_type")


def test_corrupt_schema_on_first_read_is_schema_unreadable():
    def break_schema(conn):
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET sql='CREATE TABLE runs (' WHERE name='runs'")

    make_store(hu.CURSOR_STORE_PATH, S1, setup=break_schema)
    drifted(read(), S1, "schema_unreadable")


def test_optional_columns_may_be_absent_and_extra_columns_are_tolerated():
    ddl = RUNS_DDL.replace(" request_id TEXT,", "").replace(" model_params_json TEXT,", "") + ""
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")], ddl=ddl)
    assert read().complete and history()[key("run-a")][1] == "grok-4.7-unspecified"


# ── limits and types ──────────────────────────────────────────────────────


def test_one_mebibyte_text_is_rejected_without_materializing_it_in_python():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", usage_json="x" * (1 << 20))])
    tracemalloc.start()
    try:
        result = read()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    refused(result, "malformed", "oversize_or_type", S1)
    assert peak < 512 * 1024


@pytest.mark.parametrize(
    ("usage_json", "cause"),
    [
        ("null", "bad_json"),
        ("{not json", "bad_json"),
        ("[" * 20000, "bad_json"),
        (RawText(b'{"inputTokens": \xff}'), "bad_utf8"),
    ],
)
def test_json_and_utf8_failures(usage_json, cause):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a", usage_json=usage_json)])
    refused(read(), "malformed", cause, S1)


def test_recursion_error_is_bad_json_for_sqlite_and_malformed_for_jsonl(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])

    class DeepJson:
        loads = staticmethod(lambda _text: (_ for _ in ()).throw(RecursionError()))

        def __getattr__(self, name):
            return getattr(json, name)

    monkeypatch.setattr(hu, "json", DeepJson())
    refused(read(), "malformed", "bad_json", S1)
    directory = hu.CURSOR_STORE_PATH / "legacy"
    directory.mkdir()
    (directory / "runs.ndjson").write_text("{}\n")
    (hu.CURSOR_STORE_PATH / S1 / "index.db").unlink()
    result = read()
    assert result.reason == "malformed" and cache().get("last_reason_detail") is None


# ── paths ─────────────────────────────────────────────────────────────────

PATHS = ("index.db", "index.db-wal", "index.db-shm", "index.db-journal")


@pytest.mark.parametrize("name", PATHS)
def test_non_regular_store_files_refuse_before_any_open(tmp_path, name):
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    target = directory / name
    target.unlink(missing_ok=True)
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"")
    target.symlink_to(decoy)
    refused(read(), "io_error", "non_regular_path", S1, file=name)


@pytest.mark.parametrize("name", PATHS)
def test_fifo_store_files_never_hang_the_reader(tmp_path, name):
    directory = make_store(tmp_path / "stores", S1, [fin("run-a")])
    target = directory / name
    target.unlink(missing_ok=True)
    os.mkfifo(target)
    code = (
        "import sys, time\nfrom pathlib import Path\nfrom mind_meld import host_usage as hu\n"
        "try:\n    hu._read_cursor_sqlite(Path(sys.argv[1]), time.monotonic() + 5)\n"
        "except hu._ReadFailure as exc:\n    print(exc.reason, exc.cause, exc.file)\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code, str(directory)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.stdout.split() == ["io_error", "non_regular_path", name], done.stderr


# ── the five open-rule rows ───────────────────────────────────────────────

WRITER = r"""
import os, signal, sqlite3, sys
db, ddl = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db, isolation_level=None)
c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA wal_autocheckpoint=0")
c.execute(ddl)
COLUMNS = "run_id, agent_id, turn_number, status, model, usage_json, "
COLUMNS += "created_at, updated_at, finished_at"
BODY = '{"inputTokens":%d,"outputTokens":0,"cacheReadTokens":0,'
BODY += '"cacheWriteTokens":0,"totalTokens":%d}'
def add(run, tokens):
    c.execute(
        "INSERT INTO runs (" + COLUMNS + ") VALUES (?, 'a', 1, 'FINISHED', 'gpt-6', ?, 't', 't', "
        "'2026-10-09T13:00:00.000Z')",
        (run, BODY % (tokens, tokens)),
    )
add("seed", 100)
print("ready", flush=True)
for line in sys.stdin:
    cmd = line.split()
    if cmd[0] == "insert": add(cmd[1], int(cmd[2]))
    elif cmd[0] == "checkpoint": c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    elif cmd[0] == "crash": os.kill(os.getpid(), signal.SIGKILL)
    elif cmd[0] == "quit": break
    print("ok", flush=True)
"""


class Writer:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-c", WRITER, str(directory / "index.db"), RUNS_DDL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout.readline().strip() == "ready"

    def tell(self, *words):
        self.proc.stdin.write(" ".join(words) + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline().strip()

    def crash(self):
        self.proc.stdin.write("crash\n")
        self.proc.stdin.flush()
        self.proc.wait(timeout=30)

    def close(self):
        self.proc.stdin.write("quit\n")
        self.proc.stdin.flush()
        self.proc.wait(timeout=30)


def test_row1_no_wal_opens_immutable_and_leaves_no_footprint():
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert sorted(footprint(directory)) == ["index.db"]
    before = footprint(directory)
    assert read().complete and footprint(directory) == before  # no -wal, no -shm created


def test_row2_live_wal_opens_read_only_and_leaves_no_footprint():
    conn = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")], keep_open=True)
    try:
        directory = hu.CURSOR_STORE_PATH / S1
        assert (directory / "index.db-wal").stat().st_size > 0
        assert (directory / "index.db-shm").exists()
        before = footprint(directory)
        result = read()
        assert result.complete and key("run-a") in history()  # the row is in the WAL only
        assert footprint(directory) == before
    finally:
        conn.close()


def test_row2_separate_process_writer_leaves_db_and_wal_untouched():
    writer = Writer(hu.CURSOR_STORE_PATH / S1)
    try:
        directory = hu.CURSOR_STORE_PATH / S1
        assert (directory / "index.db-wal").stat().st_size > 0
        before = footprint(directory)
        result = read()
        assert result.complete and key("seed") in history()
        assert footprint(directory) == before
    finally:
        writer.close()


def test_row3_zero_byte_wal_with_a_live_shm_is_read_read_only():
    conn = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")], keep_open=True)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the writer truncated its WAL
        directory = hu.CURSOR_STORE_PATH / S1
        assert (directory / "index.db-wal").stat().st_size == 0
        assert (directory / "index.db-shm").exists()
        before = footprint(directory)
        assert read().complete and key("run-a") in history()
        assert footprint(directory) == before
    finally:
        conn.close()


def test_row3_residue_of_a_quit_writer_is_read_without_touching_db_or_wal():
    """Conductor quit after truncating its WAL: 0-byte -wal, -shm left behind, no holder."""
    writer = Writer(hu.CURSOR_STORE_PATH / S1)
    assert writer.tell("checkpoint") == "ok"
    writer.crash()
    directory = hu.CURSOR_STORE_PATH / S1
    assert (directory / "index.db-wal").stat().st_size == 0 and (
        directory / "index.db-shm"
    ).exists()
    before = footprint(directory)
    result = read()
    assert result.complete and key("seed") in history()
    assert footprint(directory) == before


def test_row4_wal_without_shm_succeeds_and_sqlites_own_shm_is_exempt(tmp_path):
    writer = Writer(hu.CURSOR_STORE_PATH / S1)
    writer.crash()
    directory = hu.CURSOR_STORE_PATH / S1
    (directory / "index.db-shm").unlink(missing_ok=True)
    assert (directory / "index.db-wal").stat().st_size > 0
    wal = footprint(directory)["index.db-wal"]
    result = read()
    assert result.complete and key("seed") in history()  # recovered from the WAL
    assert footprint(directory)["index.db-wal"] == wal
    assert cache().get("last_reason") is None  # the -shm SQLite created is not a change


@pytest.mark.parametrize("size", [1, 4096])
def test_row5_nonempty_journal_is_a_hot_journal(size):
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    (directory / "index.db-journal").write_bytes(b"j" * size)
    refused(read(), "stale", "hot_journal", S1, file="index.db-journal")
    assert history() == {}


def test_a_zero_byte_journal_is_not_hot():
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    (directory / "index.db-journal").write_bytes(b"")
    assert read().complete


def test_immutable_identity_change_during_the_read_is_stale(monkeypatch):
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    original = hu._query_cursor_sqlite

    def touch_after(conn, sqlite3_, store, deadline):
        out = original(conn, sqlite3_, store, deadline)
        os.utime(directory / "index.db", ns=(1, 1))
        return out

    monkeypatch.setattr(hu, "_query_cursor_sqlite", touch_after)
    refused(read(), "stale", "identity_changed", S1)
    assert history() == {}


def test_a_wal_appearing_during_an_immutable_read_is_stale(monkeypatch):
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    original = hu._query_cursor_sqlite

    def wal_after(conn, sqlite3_, store, deadline):
        out = original(conn, sqlite3_, store, deadline)
        (directory / "index.db-wal").write_bytes(b"")
        return out

    monkeypatch.setattr(hu, "_query_cursor_sqlite", wal_after)
    refused(read(), "stale", "identity_changed", S1)


def test_wal_replaced_during_a_read_only_read_is_stale(monkeypatch):
    conn = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")], keep_open=True)
    directory = hu.CURSOR_STORE_PATH / S1
    original = hu._query_cursor_sqlite

    def replace_shm(c, sqlite3_, store, deadline):
        out = original(c, sqlite3_, store, deadline)
        shm = directory / "index.db-shm"
        shm.rename(directory / "moved")
        shm.write_bytes(b"\0" * 32768)
        return out

    monkeypatch.setattr(hu, "_query_cursor_sqlite", replace_shm)
    try:
        refused(read(), "stale", "identity_changed", S1)
    finally:
        conn.close()


# ── concurrency (a real second process, synchronized over pipes) ──────────


def test_live_writer_commits_are_seen_on_the_next_pass_and_passes_converge():
    writer = Writer(hu.CURSOR_STORE_PATH / S1)
    try:
        first = read()
        assert first.complete and total(first) == 100
        for index in range(1, 4):
            assert writer.tell("insert", f"run-{index}", "10") == "ok"
        for _ in range(3):  # repeated passes converge, never double counting
            result = read()
            assert result.complete and total(result) == 130 and len(history()) == 4
        assert writer.tell("checkpoint") == "ok"  # TRUNCATE leaves a 0-byte -wal
        result = read()
        assert result.complete and total(result) == 130
    finally:
        writer.close()
    assert read().complete and total(read()) == 130


# ── failures: one case per code ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "name", "reason", "cause", "code_shown"),
    [
        (11, "SQLITE_CORRUPT", "malformed", "corrupt_database", None),
        (26, "SQLITE_NOTADB", "malformed", "corrupt_database", None),
        (18, "SQLITE_TOOBIG", "malformed", "oversize_or_type", None),
        (14, "SQLITE_CANTOPEN", "io_error", "cannot_open", "SQLITE_CANTOPEN"),
        (1, "SQLITE_ERROR", "io_error", "cannot_open", "SQLITE_ERROR"),
        (10, "SQLITE_IOERR", "io_error", "cannot_open", "SQLITE_IOERR"),
        (None, "", "io_error", "cannot_open", None),
    ],
)
def test_each_sqlite_error_code_is_classified(monkeypatch, code, name, reason, cause, code_shown):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    failing_query(monkeypatch, sqlite_error(code, name))
    extra = {} if code_shown is None else {"code": code_shown}
    refused(read(), reason, cause, S1, **extra)


@pytest.mark.parametrize("code", [5, 6, 261, 262])
def test_busy_and_locked_are_never_persisted(monkeypatch, code):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete
    failing_query(monkeypatch, sqlite_error(code, "SQLITE_BUSY"))
    result = read()
    assert result.reason == "locked" and cache().get("last_reason") is None


def test_auth_lands_as_schema_object_drift(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    failing_query(monkeypatch, sqlite_error(23, "SQLITE_AUTH"))
    drifted(read(), S1, "schema_object")


def test_interrupt_is_a_deadline_with_no_detail(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    failing_query(monkeypatch, sqlite_error(9, "SQLITE_INTERRUPT"))
    result = read()
    assert result.reason == "deadline" and "last_reason_detail" not in cache()


def test_the_read_deadline_interrupts_a_long_query(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin(f"run-{n}") for n in range(300)])
    armed = {"on": False}
    interruptions = []
    original, query = hu._expired, hu._query_cursor_sqlite

    def arm(*args):
        armed["on"] = True
        try:
            return query(*args)
        except sqlite3.Error as exc:
            interruptions.append(exc.sqlite_errorcode & 0xFF)
            raise

    monkeypatch.setattr(hu, "_query_cursor_sqlite", arm)
    monkeypatch.setattr(hu, "_expired", lambda deadline: armed["on"] or original(deadline))
    assert read().reason == "deadline"
    assert interruptions == [sqlite3.SQLITE_INTERRUPT]


def test_pragma_readback_mismatch_is_sqlite_pragma(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    real = sqlite3.connect

    class Proxy:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            setattr(self._conn, name, value)

        def execute(self, sql, *args):
            if sql == "PRAGMA query_only":
                return SimpleNamespace(fetchone=lambda: (0,))
            return self._conn.execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Proxy(real(*a, **k)))
    refused(read(), "io_error", "sqlite_pragma", None)


def test_missing_sqlite_module_is_sqlite_unavailable(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    monkeypatch.setitem(sys.modules, "sqlite3", None)
    refused(read(), "io_error", "sqlite_unavailable", None)


def test_without_consent_no_database_is_opened(monkeypatch):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])

    def forbidden(*_a, **_k):
        pytest.fail("an unconsented Cursor read opened a database")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    result = hu.read_cursor_usage(hu.CURSOR_STORE_PATH)
    assert result.reason == "no_metadata_ledger"


def test_the_real_store_guard_raises_runtime_error_through_read_cursor_usage(tmp_path, monkeypatch):
    home = tmp_path / "account-home"
    real = home / "Library/Application Support/com.conductor.app/cursor-sdk-store"
    make_store(real, S1, [fin("run-a")])
    monkeypatch.setattr(hu.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(home)))
    assert hu._is_real_cursor_store(real / S1)
    assert not hu._is_real_cursor_store(tmp_path / "elsewhere")
    with pytest.raises(RuntimeError, match="real Conductor store"):
        hu.read_cursor_usage(real, consented=True)
    assert not hu.CURSOR_CACHE_PATH.exists()


# ── hardening ─────────────────────────────────────────────────────────────

ALLOWED = {
    "run_id", "request_id", "status", "model", "model_params_json", "usage_json",
    "usage_ref", "finished_at", "cancelled_at", "expired_at", "updated_at",
}  # fmt: skip


def test_constant_sql_names_only_allowed_columns():
    for columns in (ALLOWED, ALLOWED - {"request_id", "model_params_json"}):
        sql = hu._cursor_sqlite_select(frozenset(columns))
        assert sql.startswith("SELECT ") and sql.endswith(" FROM runs")
        body = sql[len("SELECT ") : -len(" FROM runs")]
        named = set(re.findall(r"[a-z_]+", body)) - {"typeof"}
        assert named == columns
    # Never a content column, never another table, never a per-agent database.
    full = hu._cursor_sqlite_select(frozenset(ALLOWED)).lower()
    for forbidden in ("result", "error_code", "agents", "run_events", "blob", "store.db", "*"):
        assert forbidden not in full
    # usage_ref is read for presence only: typeof, never its value.
    assert "typeof(usage_ref)" in full and ", usage_ref" not in full


def test_source_never_selects_a_content_table():
    source = (ROOT / "src/mind_meld/host_usage.py").read_text()
    for forbidden in ("FROM agents", "FROM run_events", "store.db", "wal_checkpoint", "VACUUM"):
        assert forbidden not in source


# ── persisted metadata ────────────────────────────────────────────────────


def test_read_set_entries_are_validated_per_entry():
    data = {
        "version": hu.CACHE_VERSION,
        "sqlite_read": {
            "stores": {
                S1: "read",
                S2: "missing_required_column",
                S3: "bogus",
                "not-hex": "read",
                "ABCDEF0123456789": "read",
                5: "read",
            }
        },
    }
    assert hu._cursor_read_set(data) == {S1: "read", S2: "missing_required_column"}
    for junk in (None, [], "x", {"stores": []}, {"stores": None}, {}):
        assert hu._cursor_read_set({"version": hu.CACHE_VERSION, "sqlite_read": junk}) == {}
    assert hu._cursor_read_set({"version": 99, "sqlite_read": data["sqlite_read"]}) == {}


def test_a_failed_pass_keeps_earlier_entries_and_a_complete_pass_drops_vanished_ones():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    make_store(hu.CURSOR_STORE_PATH, S3, [fin("run-c")])
    assert read().complete and set(cache()["sqlite_read"]["stores"]) == {S1, S3}
    broken = hu.CURSOR_STORE_PATH / S2
    broken.mkdir()
    (broken / "index.db").write_bytes(b"not a database" * 100)
    assert read().reason == "malformed"
    assert set(cache()["sqlite_read"]["stores"]) == {S1, S3}  # earlier entries survive
    for path in broken.iterdir():
        path.unlink()
    broken.rmdir()
    for path in (hu.CURSOR_STORE_PATH / S3).iterdir():
        path.unlink()
    (hu.CURSOR_STORE_PATH / S3).rmdir()
    assert read().complete and set(cache()["sqlite_read"]["stores"]) == {S1}


def test_an_old_mm_round_trip_degrades_to_all_unread_and_recovers():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete and hu.cursor_usage_diag()["unread_sqlite_stores"] == 0
    data = cache()
    del data["sqlite_read"]  # an older mm rewrites the cache without keys it does not know
    hu.CURSOR_CACHE_PATH.write_text(json.dumps(data))
    assert hu.cursor_usage_diag()["unread_sqlite_stores"] == 1
    assert read().complete and hu.cursor_usage_diag()["unread_sqlite_stores"] == 0


def break_store(directory):
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    conn.execute("UPDATE runs SET usage_json='{bad'")
    conn.close()


def test_detail_lifecycle_locked_keeps_it_deadline_clears_it_a_clean_read_clears_both(monkeypatch):
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    break_store(directory)
    refused(read(), "malformed", "bad_json", S1)
    since = cache()["last_reason_since"]
    with monkeypatch.context() as ctx:
        failing_query(ctx, sqlite_error(5, "SQLITE_BUSY"))
        assert read().reason == "locked"
    assert cache()["last_reason"] == "malformed" and cache()["last_reason_since"] == since
    assert cache()["last_reason_detail"] == {"cause": "bad_json", "store": S1}
    with monkeypatch.context() as ctx:
        failing_query(ctx, sqlite_error(9, "SQLITE_INTERRUPT"))
        assert read().reason == "deadline"
    assert cache()["last_reason"] == "deadline" and "last_reason_detail" not in cache()
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    conn.execute("UPDATE runs SET usage_json=?", [usage()])
    conn.close()
    assert read().complete
    assert cache()["last_reason"] is None and "last_reason_detail" not in cache()


def test_a_permanent_prior_reason_is_carried_with_its_own_detail_not_this_passes():
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete
    data = cache()
    data["last_reason"] = "unsupported"
    data["last_reason_since"] = "2026-10-01T00:00:00+00:00"
    hu.CURSOR_CACHE_PATH.write_text(json.dumps(data))
    break_store(directory)
    assert read().reason == "malformed"
    after = cache()
    assert after["last_reason"] == "unsupported" and "last_reason_detail" not in after


def test_jsonl_and_spool_failures_persist_no_detail():
    directory = hu.CURSOR_STORE_PATH / "legacy"
    directory.mkdir(parents=True)
    (directory / "runs.ndjson").write_text("not json\n")
    assert read().reason == "malformed" and "last_reason_detail" not in cache()


def test_detail_is_read_tolerantly_and_the_code_falls_back_after_a_reload():
    base = {"version": hu.CACHE_VERSION, "last_reason": "io_error"}
    good = {"cause": "cannot_open", "store": S1, "code": "SQLITE_CANTOPEN"}
    assert hu._cursor_reason_detail({**base, "last_reason_detail": good}) == good
    reloaded = hu._cursor_reason_detail({**base, "last_reason_detail": {**good, "code": "lower!"}})
    assert reloaded == {"cause": "cannot_open", "store": S1}
    assert "unknown SQLite error" in cli._cursor_store_detail_clause(reloaded)
    for bad in (None, [], "x", {"cause": "bad_json", "store": S1}, {"cause": 5}, {"store": S1}):
        assert hu._cursor_reason_detail({**base, "last_reason_detail": bad}) is None
    assert (
        hu._cursor_reason_detail({"version": hu.CACHE_VERSION, "last_reason_detail": good}) is None
    )
    forged = {"cause": "cannot_open", "store": "../../etc", "file": "passwd", "code": "x y"}
    assert hu._cursor_reason_detail({**base, "last_reason_detail": forged}) == {
        "cause": "cannot_open",
        "store": None,
    }


# ── diag and status ───────────────────────────────────────────────────────


def test_diag_unread_and_unsupported_for_valid_missing_and_unreadable_caches():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    no_ref = RUNS_DDL.replace(" usage_ref TEXT,", "")
    make_store(hu.CURSOR_STORE_PATH, S2, [fin("run-b")], ddl=no_ref)
    missing = hu.cursor_usage_diag()
    assert missing["unread_sqlite_stores"] == 2
    assert missing["unsupported_sqlite_stores"] == {"count": 0, "causes": []}
    assert read().complete
    valid = hu.cursor_usage_diag()
    assert valid["unread_sqlite_stores"] == 0
    assert valid["unsupported_sqlite_stores"] == {"count": 1, "causes": ["missing_required_column"]}
    assert valid["last_reason_detail"] is None
    hu.CURSOR_CACHE_PATH.write_text("not json")
    unreadable = hu.cursor_usage_diag()
    assert unreadable["unread_sqlite_stores"] is None
    assert unreadable["unsupported_sqlite_stores"] is None


def test_the_writer_lock_does_not_make_the_counts_unknown():
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("run-a")])
    assert read().complete
    lock = hu.CURSOR_CACHE_PATH.with_name(hu.CURSOR_CACHE_PATH.name + ".lock")
    with open(lock, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # a push holds the sibling writer lock
        state = hu.cursor_usage_diag()
    assert state["cache_state"] == "ok" and state["unread_sqlite_stores"] == 0


def test_every_persisted_cause_has_a_catalog_entry_or_an_explicit_no_clause():
    assert set(cli._CURSOR_DETAIL_NO_CLAUSE) <= set(hu.CURSOR_SQLITE_READER_CAUSES)
    for cause in hu.CURSOR_SQLITE_READER_CAUSES:
        detail = {"cause": cause, "store": S1, "file": "index.db", "code": "SQLITE_ERROR"}
        clause = cli._cursor_store_detail_clause(detail)
        assert (clause is None) == (cause in cli._CURSOR_DETAIL_NO_CLAUSE), cause
        if clause:
            assert "pipx upgrade" not in clause and "delete" not in clause.lower()
    for cause, reason in hu.CURSOR_SQLITE_READER_CAUSES.items():
        assert reason in {"malformed", "io_error", "stale"}, cause
    corrupt = cli._cursor_store_detail_clause({"cause": "corrupt_database", "store": S1})
    assert "SQLite " in corrupt and errors.CURSOR_SQLITE_URL in corrupt


def test_the_url_anchor_resolves_in_the_readme():
    from tests.test_docs_routing import _heading_anchors

    assert errors.CURSOR_SQLITE_URL.endswith("#cursor-sqlite-stores")
    assert "cursor-sqlite-stores" in _heading_anchors(ROOT / "README.md")


def test_census_pin_matches_the_contract():
    contract = CONTRACT.read_text()
    assert hu.CURSOR_SQLITE_CENSUS_CONDUCTOR_VERSION == "0.90.1"
    assert hu.CURSOR_SQLITE_CENSUS_CONDUCTOR_VERSION in contract
    assert "SQLite census, 2026-10-09, Conductor 0.90.1" in contract
    assert hu.CURSOR_USAGE_CENSUS_CONDUCTOR_VERSION == "0.87.3"  # the JSONL pins are unchanged


@pytest.mark.parametrize("sqlite_first", [True, False])
@pytest.mark.parametrize("placeholder", [True, False])
def test_equal_mixed_format_duplicates_never_retract(sqlite_first, placeholder):
    sqlite_store, jsonl_store = (S1, S2) if sqlite_first else (S2, S1)
    directory = make_store(hu.CURSOR_STORE_PATH, sqlite_store, [fin("retained")])
    assert read().complete
    before = history()
    status, ref = ("FINISHED", "ref") if placeholder else ("CANCELLED", None)
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    try:
        conn.execute("UPDATE runs SET status=?, usage_json=NULL, usage_ref=?", (status, ref))
    finally:
        conn.close()
    legacy = {
        "runId": "retained",
        "status": status.lower(),
        "usage": None,
        "usageRef": ref,
        "model": {"id": "grok-4.7", "params": json.loads(PARAMS)},
        "endedAt": int(datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp() * 1000),
    }
    other = hu.CURSOR_STORE_PATH / jsonl_store
    other.mkdir()
    (other / "runs.ndjson").write_text(json.dumps(legacy) + "\n")
    assert read().complete and history() == before
    for store in (directory, other):
        for path in store.iterdir():
            path.unlink()
        store.rmdir()
    assert read().complete and history() == before


def test_wal_appearing_before_identity_baseline_refuses(monkeypatch):
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("seed")])
    original = hu._lstat_sqlite_files
    writers = []

    def snapshot_then_commit(*args):
        present = original(*args)
        if not writers:
            writer = sqlite3.connect(directory / "index.db", isolation_level=None)
            writers.append(writer)
            insert(writer, **fin("late"))
        return present

    monkeypatch.setattr(hu, "_lstat_sqlite_files", snapshot_then_commit)
    try:
        refused(read(), "stale", "identity_changed", S1)
        assert history() == {}
        assert read().complete and set(history()) == {key("seed"), key("late")}
    finally:
        for writer in writers:
            writer.close()


def test_writer_commit_during_schema_read_waits_for_next_snapshot(monkeypatch):
    writer = Writer(hu.CURSOR_STORE_PATH / S1)
    real_connect = sqlite3.connect
    replies = []

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)

        def during_schema(sql):
            if sql == "PRAGMA table_info(runs)" and not replies:
                replies.append(writer.tell("insert", "during-read", "10"))

        conn.set_trace_callback(during_schema)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        first = read()
        assert replies == ["ok"]
        assert first.complete and total(first) == 100 and len(history()) == 1
        second = read()
        assert second.complete and total(second) == 110 and len(history()) == 2
    finally:
        writer.close()


def test_jsonl_alias_conflict_beside_sqlite_has_no_sqlite_detail():
    for store, run in ((S1, "legacy-a"), (S2, "legacy-b")):
        directory = hu.CURSOR_STORE_PATH / store
        directory.mkdir(parents=True)
        row = {
            "runId": run,
            "requestId": "shared-request",
            "status": "finished",
            "usage": json.loads(usage(reasoning=0)),
            "model": {"id": "grok-4.7", "params": json.loads(PARAMS)},
            "endedAt": int(datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp() * 1000),
        }
        (directory / "runs.ndjson").write_text(json.dumps(row) + "\n")
    make_store(hu.CURSOR_STORE_PATH, S2)
    refused(read(), "malformed")
    assert history() == {}


@pytest.mark.parametrize("sqlite_first", [True, False])
def test_mixed_alias_conflict_has_sqlite_detail_in_either_order(sqlite_first):
    sqlite_store, jsonl_store = (S1, S2) if sqlite_first else (S2, S1)
    make_store(
        hu.CURSOR_STORE_PATH,
        sqlite_store,
        [fin("sqlite-run", request_id="shared-request")],
    )
    directory = hu.CURSOR_STORE_PATH / jsonl_store
    directory.mkdir()
    row = {
        "runId": "jsonl-run",
        "requestId": "shared-request",
        "status": "finished",
        "usage": json.loads(usage(reasoning=0)),
        "model": {"id": "grok-4.7", "params": json.loads(PARAMS)},
        "endedAt": int(datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp() * 1000),
    }
    (directory / "runs.ndjson").write_text(json.dumps(row) + "\n")
    assert read().reason == "malformed"
    assert cache().get("last_reason_detail", {}).get("cause") == "duplicate_request"
    assert history() == {}


@pytest.mark.parametrize("placement", ["same", "sqlite_first", "jsonl_first"])
@pytest.mark.parametrize("transition", ["pruned", "empty", "drift", "vanished"])
@pytest.mark.parametrize("placeholder", [False, True])
def test_sqlite_retention_survives_missing_rows_with_legacy_copy(
    placement, transition, placeholder
):
    sqlite_store = S2 if placement == "jsonl_first" else S1
    jsonl_store = sqlite_store if placement == "same" else (S1 if sqlite_store == S2 else S2)
    directory = make_store(hu.CURSOR_STORE_PATH, sqlite_store, [fin("retained")])
    assert read().complete
    before = history()
    if transition == "vanished":
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
    else:
        conn = sqlite3.connect(directory / "index.db", isolation_level=None)
        try:
            if transition == "pruned":
                conn.execute("DELETE FROM runs")
            elif transition == "empty":
                for table in ("runs", "agents", "run_events"):
                    conn.execute(f"DROP TABLE {table}")
            else:
                conn.execute("ALTER TABLE runs DROP COLUMN usage_ref")
        finally:
            conn.close()
    legacy = hu.CURSOR_STORE_PATH / jsonl_store
    legacy.mkdir(exist_ok=True)
    row = {
        "runId": "retained",
        "status": "finished" if placeholder else "cancelled",
        "usage": None,
        "usageRef": "ref" if placeholder else None,
        "model": {"id": "grok-4.7", "params": json.loads(PARAMS)},
        "endedAt": int(datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp() * 1000),
    }
    (legacy / "runs.ndjson").write_text(json.dumps(row) + "\n")
    assert read().complete and history() == before
    for store in hu.CURSOR_STORE_PATH.iterdir():
        for path in store.iterdir():
            path.unlink()
        store.rmdir()
    assert read().complete and history() == before


@pytest.mark.parametrize("reason", ["deadline", "locked", "stale", "malformed"])
def test_incomplete_pass_keeps_sqlite_counters_before_the_store_is_reached(monkeypatch, reason):
    directory = make_store(hu.CURSOR_STORE_PATH, S2, [fin("retained")])
    assert read().complete
    before = history()
    legacy = hu.CURSOR_STORE_PATH / S1
    legacy.mkdir()
    (legacy / "runs.ndjson").write_text(
        json.dumps({"runId": "retained", "status": "cancelled", "usage": None}) + "\n"
    )
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    try:
        conn.execute("UPDATE runs SET status='CANCELLED', usage_json=NULL")
    finally:
        conn.close()
    stage = hu._stage_cursor_directory

    def fail_later(path, deadline):
        if path.name == S2:
            raise hu._ReadFailure(reason)
        return stage(path, deadline)

    with monkeypatch.context() as patch:
        patch.setattr(hu, "_stage_cursor_directory", fail_later)
        assert read().reason == reason
        assert history() == before
    assert read().complete and history() == before


@pytest.mark.parametrize("invalid", [None, {}, True, [1], ["not-a-hash"]])
def test_invalid_sqlite_retention_metadata_refuses_without_rewriting(invalid):
    make_store(hu.CURSOR_STORE_PATH, S1, [fin("retained")])
    assert read().complete
    data = cache()
    data["sqlite_retained"] = invalid
    hu.CURSOR_CACHE_PATH.write_text(json.dumps(data))
    before = hu.CURSOR_CACHE_PATH.read_bytes()
    assert read().reason == "malformed"
    assert hu.CURSOR_CACHE_PATH.read_bytes() == before
    assert hu.cursor_usage_diag()["cache_state"] == "unreadable"


def test_sqlite_retention_metadata_tracks_committed_history_and_cutoff():
    directory = make_store(hu.CURSOR_STORE_PATH, S1, [fin("retained")])
    assert read().complete
    assert cache()["sqlite_retained"] == [key("retained")]
    broken = hu.CURSOR_STORE_PATH / S2
    broken.mkdir()
    (broken / "index.db").write_bytes(b"not a database" * 100)
    assert read().reason == "malformed"
    assert cache()["sqlite_retained"] == [key("retained")]
    (broken / "index.db").unlink()
    broken.rmdir()
    data = cache()
    data["runs"][key("retained")]["day"] = "2020-01-01"
    hu.CURSOR_CACHE_PATH.write_text(json.dumps(data))
    conn = sqlite3.connect(directory / "index.db", isolation_level=None)
    try:
        conn.execute("DELETE FROM runs")
    finally:
        conn.close()
    assert read().complete and history() == {}
    assert "sqlite_retained" not in cache()
