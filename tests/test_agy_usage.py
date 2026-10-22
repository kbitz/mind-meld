"""Google Antigravity (`agy`) host-usage reader tests.

Covers:
- census pin match against CONTRACT.md
- closed-by-default consent
- happy path single DB and multi-model gen_metadata
- status 3 usage-less skip
- zero-token and <synthetic> skip
- token_usage on step_type != 15 refusal as unsupported
- hot journal refusal as stale
- concurrent WAL drift refusal as stale
- length limit and deadline progress handler
- turn deduplication across DBs by bot_id
- conflicting bot_id refusal as malformed
- thinking + response counter invariant
- warm cache hit reuse
- agy_usage_diag behavior (missing cache, ok cache, count_logs=True/False)
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mind_meld import host_usage as hu

CONTRACT_PATH = Path(__file__).parent / "fixtures" / "host_sessions" / "agy" / "CONTRACT.md"


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val >= 0x80:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _encode_field(fnum: int, wtype: int, val: bytes | int) -> bytes:
    header = _encode_varint((fnum << 3) | wtype)
    if wtype == 0:
        return header + _encode_varint(int(val))
    elif wtype == 2:
        return header + _encode_varint(len(val)) + bytes(val)
    raise ValueError(f"Unsupported wtype {wtype}")


def _make_gen_metadata(models: dict[int, str]) -> bytes:
    records = []
    for enum_id, slug in models.items():
        sub_bytes = _encode_field(3, 0, enum_id) + _encode_field(19, 2, slug.encode("utf-8"))
        records.append(_encode_field(1, 2, sub_bytes))
    return b"".join(records)


def _make_step_metadata(
    ts: int = 1792692712,
    *,
    model_enum: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_create: int = 0,
    cache_read: int = 0,
    bot_id: str | None = None,
    thinking: int | None = None,
    response: int | None = None,
) -> bytes:
    tag1_bytes = _encode_field(1, 0, ts)
    meta = _encode_field(1, 2, tag1_bytes)
    if bot_id is not None:
        tu_parts = []
        if model_enum is not None:
            tu_parts.append(_encode_field(1, 0, model_enum))
        tu_parts.append(_encode_field(2, 0, input_tokens))
        tu_parts.append(_encode_field(3, 0, output_tokens))
        if cache_create != 0:
            tu_parts.append(_encode_field(4, 0, cache_create))
        if cache_read != 0:
            tu_parts.append(_encode_field(5, 0, cache_read))
        tu_parts.append(_encode_field(7, 2, bot_id.encode("utf-8")))
        if thinking is not None:
            tu_parts.append(_encode_field(9, 0, thinking))
        if response is not None:
            tu_parts.append(_encode_field(10, 0, response))
        meta += _encode_field(9, 2, b"".join(tu_parts))
    return meta


def _create_agy_db(
    db_path: Path,
    models: dict[int, str],
    steps: list[tuple[int, int, int, bytes | None]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
    conn.execute(
        "CREATE TABLE steps ("
        "idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB)"
    )
    gen_data = _make_gen_metadata(models)
    conn.execute(
        "INSERT INTO gen_metadata (idx, data, size) VALUES (1, ?, ?)", (gen_data, len(gen_data))
    )
    for idx, stype, status, meta in steps:
        conn.execute(
            "INSERT INTO steps (idx, step_type, status, metadata) VALUES (?, ?, ?, ?)",
            (idx, stype, status, meta),
        )
    conn.commit()
    conn.close()


def test_agy_contract_census_pin_matches_src_constant():
    assert CONTRACT_PATH.exists()
    content = CONTRACT_PATH.read_text(encoding="utf-8")
    assert f"Antigravity {hu.AGY_USAGE_CENSUS_HOST_VERSION}" in content


def test_unconsented_returns_incomplete(tmp_path: Path):
    root = tmp_path / "conversations"
    root.mkdir()
    result = hu.read_agy_usage(root, consented=False)
    assert not result.complete
    assert result.reason == "no_metadata_ledger"


def test_happy_path_single_db(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "conv1.db"
    meta = _make_step_metadata(
        1792692712,  # 2026-10-22
        model_enum=1318,
        input_tokens=1000,
        output_tokens=200,
        cache_read=500,
        cache_create=0,
        bot_id="bot-uuid-1",
        thinking=150,
        response=50,
    )
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])

    result = hu.read_agy_usage(root, consented=True)
    assert result.complete
    assert result.hosts == {
        "agy": {
            "2026-10-22": {
                "input": 1000,
                "output": 200,
                "cache_read": 500,
                "cache_create": 0,
            }
        }
    }
    assert "gemini-3.8-flash" in result.tokens_by_day["2026-10-22"]["by_model"]
    assert result.partial_days == frozenset()


def test_multi_model_gen_metadata(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "multi.db"
    meta1 = _make_step_metadata(
        1792692712,
        model_enum=1417,
        input_tokens=2000,
        output_tokens=300,
        cache_read=100,
        bot_id="bot-argon-1",
    )
    meta2 = _make_step_metadata(
        1792692712,
        model_enum=1320,
        input_tokens=500,
        output_tokens=50,
        cache_read=50,
        bot_id="bot-flash-1",
    )
    _create_agy_db(
        db_path,
        {1417: "gemini-4-argon", 1320: "gemini-3.8-flash"},
        [(1, 15, 3, meta1), (2, 15, 3, meta2)],
    )

    result = hu.read_agy_usage(root, consented=True)
    assert result.complete
    day = result.tokens_by_day["2026-10-22"]
    assert day["input"] == 2500
    assert day["output"] == 350
    assert "gemini-4-argon" in day["by_model"]
    assert "gemini-3.8-flash" in day["by_model"]


def test_status_3_usage_less_skip(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "skip.db"
    # Step 1: step_type 15, status 3, without tag 9
    meta_without_tu = _make_step_metadata(1792692712, bot_id=None)
    # Step 2: step_type 15, status 3, with valid tag 9
    meta_with_tu = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=100,
        output_tokens=20,
        bot_id="bot-step-2",
    )
    _create_agy_db(
        db_path,
        {1318: "gemini-3.8-flash"},
        [(1, 15, 3, meta_without_tu), (2, 15, 3, meta_with_tu)],
    )

    result = hu.read_agy_usage(root, consented=True)
    assert result.complete
    diag = hu.agy_usage_diag()
    assert diag["usage_less_skipped"] == 1


def test_zero_token_and_synthetic_skip(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "zero_and_synth.db"
    # Zero token step
    zero_meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=0,
        output_tokens=0,
        cache_read=0,
        cache_create=0,
        bot_id="bot-zero",
    )
    # Synthetic step
    synth_meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=100,
        output_tokens=50,
        bot_id="<synthetic>turn1",
    )
    _create_agy_db(
        db_path,
        {1318: "gemini-3.8-flash"},
        [(1, 15, 3, zero_meta), (2, 15, 3, synth_meta)],
    )

    result = hu.read_agy_usage(root, consented=True)
    assert result.complete
    assert result.hosts == {}


def test_token_usage_on_unexpected_step_type_refusal(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "bad_step_type.db"
    # step_type 14 (not 15) with nonzero tokens
    meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=100,
        output_tokens=50,
        bot_id="bot-bad",
    )
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [(1, 14, 3, meta)])

    result = hu.read_agy_usage(root, consented=True)
    assert not result.complete
    assert result.reason == "unsupported"
    diag = hu.agy_usage_diag()
    assert diag["last_error_detail"] == {"cause": "token_usage_on_unexpected_step_type"}


def test_hot_journal_refusal(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "conv.db"
    meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=100,
        output_tokens=50,
        bot_id="bot-hot",
    )
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])

    # Create a non-empty journal sidecar
    journal = root / "conv.db-journal"
    journal.write_bytes(b"some hot journal bytes")

    result = hu.read_agy_usage(root, consented=True)
    assert not result.complete
    assert result.reason == "stale"
    diag = hu.agy_usage_diag()
    assert diag["last_error_detail"] == {"cause": "hot_journal"}


def test_concurrent_wal_drift_refusal(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "wal_drift.db"
    meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=100,
        output_tokens=50,
        bot_id="bot-wal",
    )
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])

    # Simulate modification during open by hooking closing or a query
    original_agy_fingerprint = hu._agy_fingerprint

    def drifting_fingerprint(present, db_name):
        main_fp, wal_fp = original_agy_fingerprint(present, db_name)
        # return a mutated mtime_ns on second call (post-close)
        if getattr(drifting_fingerprint, "called", False):
            return ((main_fp[0], main_fp[1], main_fp[2], main_fp[3] + 1000), wal_fp)
        drifting_fingerprint.called = True
        return (main_fp, wal_fp)

    with patch.object(hu, "_agy_fingerprint", side_effect=drifting_fingerprint):
        result = hu.read_agy_usage(root, consented=True)
        assert not result.complete
        assert result.reason == "stale"
        diag = hu.agy_usage_diag()
        assert diag["last_error_detail"] == {"cause": "identity_changed"}


def test_length_limit(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "toobig.db"
    # Create large metadata > 16 MiB
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [])
    conn = sqlite3.connect(str(db_path))
    # Insert a 17 MiB blob
    big_blob = b"x" * (17 * 1024 * 1024)
    conn.execute(
        "INSERT INTO steps (idx, step_type, status, metadata) VALUES (1, 15, 3, ?)", (big_blob,)
    )
    conn.commit()
    conn.close()

    result = hu.read_agy_usage(root, consented=True)
    assert not result.complete
    assert result.reason == "malformed"


def test_progress_handler_deadline(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "deadline.db"
    meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=100,
        output_tokens=50,
        bot_id="bot-dl",
    )
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])

    # Already expired deadline
    result = hu.read_agy_usage(root, deadline=time.monotonic() - 1.0, consented=True)
    assert not result.complete
    assert result.reason == "deadline"


def test_turn_deduplication_by_bot_id(tmp_path: Path):
    root = tmp_path / "conversations"
    # Two DBs with identical bot_id
    meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=500,
        output_tokens=100,
        cache_read=200,
        bot_id="shared-bot-turn",
    )
    _create_agy_db(root / "c1.db", {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])
    _create_agy_db(root / "c2.db", {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])

    result = hu.read_agy_usage(root, consented=True)
    assert result.complete
    # Counted only once, not twice
    assert result.hosts["agy"]["2026-10-22"]["input"] == 500


def test_conflicting_bot_id_refusal(tmp_path: Path):
    root = tmp_path / "conversations"
    # Two DBs with same bot_id but different tokens
    meta1 = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=500,
        output_tokens=100,
        bot_id="shared-bot-turn",
    )
    meta2 = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=900,
        output_tokens=100,
        bot_id="shared-bot-turn",
    )
    _create_agy_db(root / "c1.db", {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta1)])
    _create_agy_db(root / "c2.db", {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta2)])

    result = hu.read_agy_usage(root, consented=True)
    assert not result.complete
    assert result.reason == "malformed"
    diag = hu.agy_usage_diag()
    assert diag["last_error_detail"] == {"cause": "bot_id_conflict"}


def test_thinking_response_invariant(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "mismatch.db"
    # output=100, thinking=80, response=50 (80+50=130 != 100)
    meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=100,
        output_tokens=100,
        bot_id="bot-mismatch",
        thinking=80,
        response=50,
    )
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])

    result = hu.read_agy_usage(root, consented=True)
    assert not result.complete
    assert result.reason == "malformed"
    diag = hu.agy_usage_diag()
    assert diag["last_error_detail"] == {"cause": "counter_invariant_violation"}


def test_cache_create_partial_days(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "cache_write.db"
    meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=1000,
        output_tokens=100,
        cache_create=50,
        bot_id="bot-cache-create",
    )
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])

    result = hu.read_agy_usage(root, consented=True)
    assert result.complete
    assert result.partial_days == frozenset({"2026-10-22"})


def test_warm_cache_hit_avoids_reopen(tmp_path: Path):
    root = tmp_path / "conversations"
    db_path = root / "conv.db"
    meta = _make_step_metadata(
        1792692712,
        model_enum=1318,
        input_tokens=1000,
        output_tokens=100,
        bot_id="bot-warm",
    )
    _create_agy_db(db_path, {1318: "gemini-3.8-flash"}, [(1, 15, 3, meta)])

    # First read: populates cache
    res1 = hu.read_agy_usage(root, consented=True)
    assert res1.complete

    # Verify cache is populated
    cache_data = json.loads(hu.AGY_CACHE_PATH.read_text(encoding="utf-8"))
    assert "conv.db" in cache_data["files"]

    # Second read: mock _read_single_agy_db to verify it is NOT called
    with patch.object(hu, "_read_single_agy_db") as mock_read:
        res2 = hu.read_agy_usage(root, consented=True)
        assert res2.complete
        assert not mock_read.called
        assert res2.hosts == res1.hosts


def test_agy_usage_diag_views(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "conversations"
    root.mkdir()
    monkeypatch.setattr(hu, "AGY_CONVERSATIONS_PATH", root)

    # Missing cache
    diag_missing = hu.agy_usage_diag()
    assert diag_missing["cache_state"] == "missing"
    assert diag_missing["files_on_disk"] is None

    # Missing cache with count_logs=True
    diag_missing_count = hu.agy_usage_diag(count_logs=True)
    assert diag_missing_count["files_on_disk"] == 0

    # Create 2 DBs
    _create_agy_db(root / "a.db", {1318: "gemini-3.8-flash"}, [])
    _create_agy_db(root / "b.db", {1318: "gemini-3.8-flash"}, [])

    # Read usage to populate cache
    hu.read_agy_usage(root, consented=True)

    # Valid cache with count_logs=False
    diag_ok = hu.agy_usage_diag(count_logs=False)
    assert diag_ok["cache_state"] == "ok"
    assert diag_ok["complete_once"] is True
    assert diag_ok["files_cached"] == 2
    assert diag_ok["files_on_disk"] is None

    # Valid cache with count_logs=True
    diag_ok_counted = hu.agy_usage_diag(count_logs=True)
    assert diag_ok_counted["files_on_disk"] == 2
