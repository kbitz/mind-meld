"""Muse host-usage reader: strict terminal-ledger contract.

Mirrors the Grok reader tests' posture (tests/test_host_usage.py) at
the smallest durable size: fixture totals, closed-default consent,
drift refusal, duplicate handling, and cache resume.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mind_meld import host_usage as hu

FIXTURES = Path(__file__).parent / "fixtures" / "host_sessions" / "muse"


@pytest.fixture
def muse_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "config" / "muse-host-tokens.json"
    monkeypatch.setattr(hu, "MUSE_CACHE_PATH", cache)
    return cache


def _record(rid: str, usage: dict, *, model: str = "muse-wonder-1.0", extra: dict | None = None):
    event = {"kind": "model_completed", "duration_ms": 5, "model": model, "usage": usage}
    if extra:
        event.update(extra)
    return {
        "id": rid,
        "record_type": "event",
        "payload_type": "runtime.session",
        # 2026-10-13T12:00:00Z in microseconds.
        "recorded_at": str(
            int(datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp() * 1_000_000)
        ),
        "payload": {"event": event},
    }


def _usage(**overrides) -> dict:
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_tokens": 40,
        "cache_write_tokens": 0,
        "cache_read_tokens": 40,
        "reasoning_tokens": 3,
    }
    usage.update(overrides)
    return usage


def _write_session(root: Path, records: list[dict], session: str = "s1") -> Path:
    path = root / "2026" / "10" / "13" / session / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_fixture_totals_are_disjoint_and_attribution_is_not_counted(muse_cache):
    result = hu.read_muse_usage(FIXTURES, consented=True)
    assert result.complete
    assert result.hosts == {
        "other": {
            "2026-10-13": {"input": 1200, "cache_create": 0, "cache_read": 500, "output": 170},
            "2026-10-14": {"input": 0, "cache_create": 0, "cache_read": 300, "output": 30},
        }
    }
    assert result.tokens_by_day["2026-10-13"]["by_model"]["muse-wonder-full-1.0"] == {
        "input": 700,
        "cache_create": 0,
        "cache_read": 500,
        "output": 120,
    }
    assert result.partial_days == frozenset()


def test_unconsented_read_never_touches_the_store(muse_cache, tmp_path):
    missing = tmp_path / "no-such-root"
    result = hu.read_muse_usage(missing)
    assert not result.complete
    assert result.reason == "no_metadata_ledger"
    assert not muse_cache.exists()


def test_missing_root_is_a_completed_empty_scan(muse_cache, tmp_path):
    result = hu.read_muse_usage(tmp_path / "absent", consented=True)
    assert result.complete
    assert result.hosts == {}


@pytest.mark.parametrize(
    "usage",
    [
        _usage(cached_tokens=41),  # cached must duplicate cache_read
        _usage(reasoning_tokens=11),  # reasoning must stay within output
        {**_usage(), "surprise_tokens": 1},  # unknown counter key
    ],
)
def test_counter_drift_refuses_the_reader(muse_cache, tmp_path, usage):
    _write_session(tmp_path, [_record("e1", usage)])
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert not result.complete
    assert result.reason == "unsupported"


def test_unknown_event_key_refuses_the_reader(muse_cache, tmp_path):
    _write_session(tmp_path, [_record("e1", _usage(), extra={"new_field": 1})])
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert not result.complete
    assert result.reason == "unsupported"


def test_equal_duplicate_counts_once_and_conflict_refuses(muse_cache, tmp_path):
    record = _record("e1", _usage())
    _write_session(tmp_path, [record, record])
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-10-13"]["input"] == 60

    conflict = _record("e1", _usage(input_tokens=200))
    _write_session(tmp_path, [record, conflict])
    refused = hu.read_muse_usage(tmp_path, consented=True)
    assert not refused.complete
    assert refused.reason == "unsupported"


def test_append_resume_picks_up_new_steps(muse_cache, tmp_path):
    path = _write_session(tmp_path, [_record("e1", _usage())])
    first = hu.read_muse_usage(tmp_path, consented=True)
    assert first.hosts["other"]["2026-10-13"]["output"] == 10

    with path.open("a") as fp:
        fp.write(json.dumps(_record("e2", _usage(output_tokens=7))) + "\n")
    second = hu.read_muse_usage(tmp_path, consented=True)
    assert second.complete
    assert second.hosts["other"]["2026-10-13"]["output"] == 17
    assert second.hosts["other"]["2026-10-13"]["input"] == 120


def test_nonzero_cache_write_marks_the_day_partial(muse_cache, tmp_path):
    _write_session(tmp_path, [_record("e1", _usage(cache_write_tokens=5))])
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.partial_days == frozenset({"2026-10-13"})
    assert result.hosts["other"]["2026-10-13"]["cache_create"] == 5


def test_symlinked_session_log_is_never_read(muse_cache, tmp_path):
    real = _write_session(tmp_path, [_record("e1", _usage())], session="real")
    link_dir = tmp_path / "2026" / "10" / "13" / "linked"
    link_dir.mkdir(parents=True)
    (link_dir / "session.jsonl").symlink_to(real)
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-10-13"]["output"] == 10


def test_muse_models_stay_unclassified_in_1x():
    # A "muse" host family is a MAJOR under the 1.x compatibility
    # contract; the reader name is the MINOR. Models classify as
    # "other" until 2.0 rewrites the contract.
    assert hu.host_family("muse-wonder-full-1.0") == "other"
    assert hu.host_family("MUSE-Spark-1.3") == "other"


def test_muse_snapshot_row_is_accepted_by_the_aggregator(muse_cache):
    """Writer -> wire -> acceptor, the MINOR contract end to end.

    The muse reader name rides in ``token_sources`` (retained by older
    peers as an unknown-but-bounded name) while its models stay in the
    closed ``other`` family, so no 1.x peer rejects the row.
    """
    from mind_meld import events
    from mind_meld.skills.retro_fleet import aggregator

    result = hu.read_muse_usage(FIXTURES, consented=True)
    row = events.make_host_usage_snapshot(
        device="dev-muse",
        hosts=result.hosts,
        token_sources=("muse",),
        tokens_by_day=result.tokens_by_day,
    )
    accepted = aggregator._accept_host_usage_snapshot(dict(row))
    assert isinstance(accepted, aggregator._AcceptedHostRow), accepted
    assert accepted.consulted == ("muse",)
    assert accepted.lifetime_by_family["other"]["2026-10-13"]["output"] == 170


def test_muse_contract_census_pin_matches_src_constant():
    contract = (FIXTURES / "CONTRACT.md").read_text(encoding="utf-8")
    assert f"**Host version: Muse {hu.MUSE_USAGE_CENSUS_HOST_VERSION}**" in contract
