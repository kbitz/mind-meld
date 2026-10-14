"""Muse host-usage reader: strict terminal-ledger contract.

Mirrors the Grok reader tests' posture (tests/test_host_usage.py) at
the smallest durable size: fixture totals, closed-default consent,
drift refusal, duplicate handling, and cache resume.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from mind_meld import host_usage as hu

FIXTURES = Path(__file__).parent / "fixtures" / "host_sessions" / "muse"
NOW = datetime(2026, 10, 14, 18, tzinfo=timezone.utc)
"""The reader's pinned clock: keeps the dated fixtures inside the 90-day window."""


@pytest.fixture
def muse_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "config" / "muse-host-tokens.json"
    monkeypatch.setattr(hu, "MUSE_CACHE_PATH", cache)
    monkeypatch.setattr(hu, "_muse_now", lambda: NOW)
    return cache


def _record(
    rid: str,
    usage: dict,
    *,
    model: str = "muse-wonder-1.0",
    extra: dict | None = None,
    at: datetime = datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
):
    event = {"kind": "model_completed", "duration_ms": 5, "model": model, "usage": usage}
    if extra:
        event.update(extra)
    return {
        "id": rid,
        "record_type": "event",
        "payload_type": "runtime.session",
        "recorded_at": str(int(at.timestamp() * 1_000_000)),
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


def _write_session(
    root: Path, records: list[dict], session: str = "s1", *, day: str = "2026/10/13"
) -> Path:
    path = root / day / session / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _age(path: Path, delta: timedelta) -> None:
    """Set a log's mtime to ``NOW - delta`` (the reader's idle clock)."""
    stamp = (NOW - delta).timestamp()
    os.utime(path, (stamp, stamp))


def _settled_session(root: Path, rid: str, day: str, *, session: str = "s1", **usage) -> Path:
    """A log in an old day directory, idle long enough to freeze."""
    y, m, d = (int(part) for part in day.split("/"))
    at = datetime(y, m, d, 12, tzinfo=timezone.utc)
    path = _write_session(root, [_record(rid, _usage(**usage), at=at)], session, day=day)
    _age(path, timedelta(days=3))
    return path


def _cache(path: Path) -> dict:
    return json.loads(path.read_text())


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


def test_duplicate_envelope_id_under_another_model_refuses(muse_cache, tmp_path):
    # One envelope is one step: the model is not part of the key, so a
    # repeated id with a different model is a conflict, not a second step.
    _write_session(
        tmp_path,
        [_record("e1", _usage()), _record("e1", _usage(), model="muse-spark-1.3")],
    )
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert not result.complete
    assert result.reason == "unsupported"


def test_step_restated_in_another_log_counts_once_and_conflict_refuses(muse_cache, tmp_path):
    record = _record("e1", _usage())
    _write_session(tmp_path, [record], session="s1")
    _write_session(tmp_path, [record], session="s1/subagent/c1")
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-10-13"]["input"] == 60

    _write_session(tmp_path, [_record("e1", _usage(output_tokens=11))], session="s2")
    refused = hu.read_muse_usage(tmp_path, consented=True)
    assert not refused.complete
    assert refused.reason == "unsupported"


@pytest.mark.parametrize(
    ("stamp", "reason"),
    [
        pytest.param("1000000000000000", "malformed", id="pre-2020"),
        pytest.param("1791892800", "malformed", id="seconds-scale"),
        pytest.param("4102444800000000", "malformed", id="year-2100"),
        # Past int()'s 4,300-digit limit: malformed, never a raw ValueError.
        pytest.param("1" * 5000, "malformed", id="over-digit-limit"),
        pytest.param("2026-10-13T12:00:00Z", "unsupported", id="iso-text"),
        pytest.param(None, "unsupported", id="missing"),
    ],
)
def test_recorded_at_out_of_bounds_refuses(muse_cache, tmp_path, stamp, reason):
    record = _record("e1", _usage())
    if stamp is None:
        del record["recorded_at"]
    else:
        record["recorded_at"] = stamp
    _write_session(tmp_path, [record])
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert not result.complete
    assert result.reason == reason


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


def test_unchanged_log_is_a_stat_only_hit_and_append_resumes(muse_cache, tmp_path, monkeypatch):
    path = _write_session(tmp_path, [_record("e1", _usage())])
    first = hu.read_muse_usage(tmp_path, consented=True)
    with monkeypatch.context() as m:
        # A hit neither re-reads nor re-fingerprints an unchanged log.
        m.setattr(hu, "_read_muse_file", Mock(side_effect=AssertionError("cache miss")))
        m.setattr(hu, "_fingerprint", Mock(side_effect=AssertionError("reopened")))
        assert hu.read_muse_usage(tmp_path, consented=True) == first
    with path.open("a") as fp:
        fp.write(json.dumps(_record("e2", _usage(output_tokens=7))) + "\n")
    with monkeypatch.context() as m:
        m.setattr(hu, "_read_full_muse_file", Mock(side_effect=AssertionError("full re-read")))
        second = hu.read_muse_usage(tmp_path, consented=True)
    assert second.hosts["other"]["2026-10-13"]["output"] == 17


def test_rewritten_log_rewalks_without_double_counting(muse_cache, tmp_path):
    path = _write_session(tmp_path, [_record("e1", _usage())])
    hu.read_muse_usage(tmp_path, consented=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "".join(json.dumps(r) + "\n" for r in [_record("e1", _usage()), _record("e2", _usage())])
    )
    os.replace(tmp, path)  # new inode, grown
    assert hu.read_muse_usage(tmp_path, consented=True).hosts["other"]["2026-10-13"]["input"] == 120
    path.write_text(json.dumps(_record("e2", _usage())) + "\n")  # same inode, shrunk
    assert hu.read_muse_usage(tmp_path, consented=True).hosts["other"]["2026-10-13"]["input"] == 60
    with path.open("r+") as fp:  # same inode, grown, head changed
        fp.truncate(0)
        fp.write(
            json.dumps(_record("e3", _usage(output_tokens=4)))
            + "\n"
            + json.dumps(_record("e4", _usage(output_tokens=5)))
            + "\n"
        )
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.hosts["other"]["2026-10-13"] == {
        "input": 120,
        "cache_create": 0,
        "cache_read": 80,
        "output": 9,
    }


def test_cache_write_is_subtracted_from_inclusive_input(muse_cache, tmp_path):
    _write_session(tmp_path, [_record("e1", _usage(cache_write_tokens=5))])
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.hosts["other"]["2026-10-13"] == {
        "input": 55,
        "cache_create": 5,
        "cache_read": 40,
        "output": 10,
    }
    _write_session(tmp_path, [_record("e2", _usage(input_tokens=30))], session="s2")
    refused = hu.read_muse_usage(tmp_path, consented=True)
    assert not refused.complete
    assert refused.reason == "malformed"


def test_walk_descends_only_literal_subagent_dirs(muse_cache, tmp_path):
    _write_session(tmp_path, [_record("e1", _usage())])
    _write_session(tmp_path, [_record("x1", _usage())], session="s1/other/child")
    _write_session(tmp_path, [_record("g1", _usage())], session="s1/subagent/c1/subagent/g1")
    _write_session(tmp_path, [_record("s3e", _usage())], session="s3")
    day = tmp_path / "2026" / "10" / "13"
    (day / "s1" / "subagent" / "link").symlink_to(day / "s3", target_is_directory=True)
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    # s1 + grandchild g1 + s3 once; other/ and the symlinked child are never walked.
    assert result.hosts["other"]["2026-10-13"]["input"] == 180


def test_settled_day_freezes_and_is_never_reopened(muse_cache, tmp_path, monkeypatch):
    _settled_session(tmp_path, "old", "2026/10/10")
    _write_session(tmp_path, [_record("live", _usage(output_tokens=4))])
    first = hu.read_muse_usage(tmp_path, consented=True)
    assert first.complete
    cache = _cache(muse_cache)
    assert set(cache["frozen"]) == {"2026/10/10"}
    assert cache["frozen"]["2026/10/10"]["logs"] == 1
    assert cache["frozen"]["2026/10/10"]["days"]["2026-10-10"]["muse-wonder-1.0"]["input"] == 60
    assert len(cache["files"]) == 1  # only the live day keeps per-turn entries

    with monkeypatch.context() as m:
        m.setattr(hu, "_read_muse_file", Mock(side_effect=AssertionError("reopened")))
        again = hu.read_muse_usage(tmp_path, consented=True)
    assert again == first
    assert again.hosts["other"]["2026-10-10"]["input"] == 60
    diag = hu.muse_usage_diag(count_logs=False)
    assert diag["files_cached"] == 2
    assert diag["models"] == ["muse-wonder-1.0"]
    assert diag["files_on_disk"] is None


def test_only_settled_idle_days_freeze(muse_cache, tmp_path):
    today = _write_session(tmp_path, [_record("t", _usage())], day="2026/10/14")
    _age(today, timedelta(days=3))  # idle, but its day is still live
    recent = _settled_session(tmp_path, "r", "2026/10/10")
    _age(recent, timedelta(hours=1))  # an old day still being appended
    hu.read_muse_usage(tmp_path, consented=True)
    assert _cache(muse_cache)["frozen"] == {}


def test_frozen_day_that_grows_is_unfrozen_and_recounted(muse_cache, tmp_path):
    path = _settled_session(tmp_path, "old", "2026/10/10")
    hu.read_muse_usage(tmp_path, consented=True)
    assert "2026/10/10" in _cache(muse_cache)["frozen"]

    at = datetime(2026, 10, 10, 13, tzinfo=timezone.utc)
    with path.open("a") as fp:  # a resumed session appends to its old log
        fp.write(json.dumps(_record("late", _usage(output_tokens=5), at=at)) + "\n")
    _age(path, timedelta(hours=1))
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-10-10"]["output"] == 15
    assert _cache(muse_cache)["frozen"] == {}


def test_short_budget_rotates_one_frozen_day_per_pass(muse_cache, tmp_path, monkeypatch):
    for index, day in enumerate(("2026/10/08", "2026/10/09", "2026/10/10")):
        _settled_session(tmp_path, f"d{index}", day)
    hu.read_muse_usage(tmp_path, consented=True)
    assert set(_cache(muse_cache)["frozen"]) == {"2026/10/08", "2026/10/09", "2026/10/10"}

    walked: list[str] = []
    real = hu._muse_day_logs

    def spy(day, deadline):
        walked.append("/".join(day.parts[-3:]))
        return real(day, deadline)

    monkeypatch.setattr(hu, "_muse_day_logs", spy)
    # A budget that fits one single-log frozen day per pass.
    monkeypatch.setattr(hu, "_MUSE_VERIFY_LOG_COST_S", 3.0)
    for _ in range(3):
        hu.read_muse_usage(tmp_path, consented=True)
    # Just-frozen days first, newest first among equals: all three in three passes.
    assert walked == ["2026/10/10", "2026/10/09", "2026/10/08"]


def test_attended_budget_checks_every_frozen_day(muse_cache, tmp_path):
    """A pruned, a resumed and a new log in three frozen days: all caught in one pass."""
    pruned = _settled_session(tmp_path, "a", "2026/10/08", session="s1")
    _settled_session(tmp_path, "b", "2026/10/08", session="s2")
    resumed = _settled_session(tmp_path, "c", "2026/10/09")
    _settled_session(tmp_path, "d", "2026/10/10")
    first = hu.read_muse_usage(tmp_path, consented=True)
    assert set(_cache(muse_cache)["frozen"]) == {"2026/10/08", "2026/10/09", "2026/10/10"}
    assert first.hosts["other"]["2026-10-08"]["input"] == 120

    pruned.unlink()
    with resumed.open("a") as fp:
        late = _record(
            "c2", _usage(output_tokens=5), at=datetime(2026, 10, 9, 13, tzinfo=timezone.utc)
        )
        fp.write(json.dumps(late) + "\n")
    _age(resumed, timedelta(days=3))
    _settled_session(tmp_path, "e", "2026/10/10", session="s9")
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-10-08"]["input"] == 60
    assert result.hosts["other"]["2026-10-09"]["output"] == 15
    assert result.hosts["other"]["2026-10-10"]["input"] == 120


def test_deadline_while_unfreezing_keeps_the_frozen_day(muse_cache, tmp_path, monkeypatch):
    path = _settled_session(tmp_path, "old", "2026/10/10")
    hu.read_muse_usage(tmp_path, consented=True)
    frozen = _cache(muse_cache)["frozen"]
    with path.open("a") as fp:
        late = _record("late", _usage(), at=datetime(2026, 10, 10, 13, tzinfo=timezone.utc))
        fp.write(json.dumps(late) + "\n")
    with monkeypatch.context() as m:
        m.setattr(hu, "_read_full_muse_file", Mock(side_effect=hu._ReadFailure("deadline")))
        assert hu.read_muse_usage(tmp_path, consented=True).reason == "deadline"
    assert _cache(muse_cache)["frozen"] == frozen
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-10-10"]["input"] == 120


def test_history_copied_into_a_later_session_refuses(muse_cache, tmp_path):
    """A step older than its session cannot be new spend; frozen days hold no keys."""
    _settled_session(tmp_path, "old", "2026/10/10")
    hu.read_muse_usage(tmp_path, consented=True)
    copied = _record("old", _usage(), at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
    _write_session(tmp_path, [copied], "resumed", day="2026/10/14")
    refused = hu.read_muse_usage(tmp_path, consented=True)
    assert not refused.complete
    assert refused.reason == "unsupported"


def test_days_sharing_a_step_stay_live_and_count_it_once(muse_cache, tmp_path):
    shared = _record("shared", _usage(), at=datetime(2026, 10, 9, 23, tzinfo=timezone.utc))
    for day in ("2026/10/09", "2026/10/10"):  # the copy sits one day after, within the lead
        _age(_write_session(tmp_path, [shared], day=day), timedelta(days=3))
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-10-09"]["input"] == 60
    assert _cache(muse_cache)["frozen"] == {}


def test_pre_release_cache_layout_is_rebuilt(muse_cache, tmp_path):
    _write_session(tmp_path, [_record("e1", _usage())])
    hu.read_muse_usage(tmp_path, consented=True)
    cache = _cache(muse_cache)
    del cache["muse_format"]
    for entry in cache["files"].values():  # an older key scheme, same counters
        entry["turns"][0]["key"] = "0" * 64
    muse_cache.write_text(json.dumps(cache))
    rebuilt = hu.read_muse_usage(tmp_path, consented=True)
    assert rebuilt.hosts["other"]["2026-10-13"]["input"] == 60
    cache = _cache(muse_cache)
    assert cache["muse_format"] == hu._MUSE_CACHE_FORMAT
    assert all(entry["turns"][0]["key"] != "0" * 64 for entry in cache["files"].values())


def test_muse_row_is_accepted_by_a_pre_muse_peer(muse_cache, monkeypatch):
    from mind_meld import events
    from mind_meld.skills.retro_fleet import aggregator

    result = hu.read_muse_usage(FIXTURES, consented=True)
    row = events.make_host_usage_snapshot(
        device="dev-muse",
        hosts=result.hosts,
        token_sources=("codex", "grok", "cursor", "muse"),
        tokens_by_day=result.tokens_by_day,
    )
    # v1.8.2's writer universe: "muse" is an unknown but bounded name there.
    monkeypatch.setattr(events, "HOST_USAGE_TOKEN_SOURCES", ("codex", "grok", "cursor"))
    accepted = aggregator._accept_host_usage_snapshot(dict(row))
    assert isinstance(accepted, aggregator._AcceptedHostRow), accepted
    assert accepted.consulted == ("codex", "grok", "cursor", "muse")


def test_a_check_that_does_not_fit_leaves_the_pass_complete(muse_cache, tmp_path, monkeypatch):
    _settled_session(tmp_path, "old", "2026/10/10")
    first = hu.read_muse_usage(tmp_path, consented=True)
    seq = _cache(muse_cache)["frozen"]["2026/10/10"]["check_seq"]

    monkeypatch.setattr(hu, "_MUSE_VERIFY_LOG_COST_S", 1e9)
    assert hu.read_muse_usage(tmp_path, consented=True) == first
    assert _cache(muse_cache)["frozen"]["2026/10/10"]["check_seq"] == seq

    monkeypatch.setattr(hu, "_MUSE_VERIFY_LOG_COST_S", 0.0)
    real = hu._muse_day_logs

    def out_of_time(day, deadline):
        if day.parts[-3:] == ("2026", "10", "10"):
            raise hu._ReadFailure("deadline")
        return real(day, deadline)

    monkeypatch.setattr(hu, "_muse_day_logs", out_of_time)
    assert hu.read_muse_usage(tmp_path, consented=True) == first  # stop checking, not fail
    # Sent to the back of the queue so one slow day cannot hog every pass.
    assert _cache(muse_cache)["frozen"]["2026/10/10"]["check_seq"] > seq


def test_tail_reserve_grows_with_the_live_window(muse_cache, tmp_path, monkeypatch):
    """Checks stop early enough for dedup and aggregation of every live step."""
    _settled_session(tmp_path, "old", "2026/10/10")
    _write_session(tmp_path, [_record(f"l{n}", _usage()) for n in range(50)])
    hu.read_muse_usage(tmp_path, consented=True)
    seq = _cache(muse_cache)["frozen"]["2026/10/10"]["check_seq"]
    monkeypatch.setattr(hu, "_MUSE_VERIFY_LOG_COST_S", 0.0)
    monkeypatch.setattr(hu, "_MUSE_TAIL_STEP_COST_S", 1e9)  # no budget left after the tail
    assert hu.read_muse_usage(tmp_path, consented=True).complete
    assert _cache(muse_cache)["frozen"]["2026/10/10"]["check_seq"] == seq  # not checked


def test_a_session_removed_mid_walk_is_absent(tmp_path, monkeypatch):
    day = tmp_path / "2026" / "10" / "13"
    for session in ("s1", "s2"):
        (day / session).mkdir(parents=True)
        (day / session / "session.jsonl").write_text("{}\n")
    real = os.scandir

    def vanishing(path):
        if os.fspath(path).endswith("/s1"):
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(hu.os, "scandir", vanishing)
    logs = hu._muse_day_logs(day, deadline=float("inf"))
    assert [rel for rel, _ in logs] == ["s2/session.jsonl"]


def test_consecutive_settled_days_all_freeze(muse_cache, tmp_path):
    """Every settled, idle day freezes; none waits on its neighbours."""
    for index, day in enumerate(("2026/10/09", "2026/10/10", "2026/10/11", "2026/10/12")):
        _settled_session(tmp_path, f"c{index}", day)
    hu.read_muse_usage(tmp_path, consented=True)
    assert set(_cache(muse_cache)["frozen"]) == {
        "2026/10/09",
        "2026/10/10",
        "2026/10/11",
        "2026/10/12",
    }


# Generated by the /ship test coverage audit (pass 1).
# Value: protects=a Muse log read mid-append is partial, and its step counts once the line is
# newline-terminated; fails_when=_read_muse_file drops its fp.tell() offset check, so the torn
# line is skipped and the cached resume offset lands past it; why_new=only Grok pins a partial
# tail, Muse's line loop is its own; seam=none; no_claim=none beyond protects
def test_line_caught_mid_append_is_partial_then_counted(muse_cache, tmp_path):
    path = _write_session(tmp_path, [_record("e1", _usage())])
    tail = json.dumps(_record("e2", _usage(output_tokens=7)))
    with path.open("a") as fp:
        fp.write(tail[:40])
    torn = hu.read_muse_usage(tmp_path, consented=True)
    assert not torn.complete
    assert torn.reason == "partial"

    with path.open("a") as fp:
        fp.write(tail[40:] + "\n")
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-10-13"]["output"] == 17
    assert result.hosts["other"]["2026-10-13"]["input"] == 120


def _negative_counter(record: dict) -> None:
    record["days"]["2026-10-10"]["muse-wonder-1.0"]["input"] = -1000


def _missing_counter(record: dict) -> None:
    del record["days"]["2026-10-10"]["muse-wonder-1.0"]["output"]


def _noncanonical_day(record: dict) -> None:
    record["days"] = {"2026-10-1": record["days"]["2026-10-10"]}


# Generated by the /ship test coverage audit (pass 1).
# Value: protects=a malformed frozen-day cache record is never published: its day is recounted
# from the logs and refrozen intact; fails_when=_validated_muse_frozen accepts a bad counter or
# day key; why_new=no test tampers the Muse frozen block (the shared tamper corpus covers file
# entries); seam=none; no_claim=a well-formed but inflated record (trusted by design)
@pytest.mark.parametrize("tamper", [_negative_counter, _missing_counter, _noncanonical_day])
def test_malformed_frozen_record_is_recounted_from_logs(muse_cache, tmp_path, tamper):
    _settled_session(tmp_path, "old", "2026/10/10")
    first = hu.read_muse_usage(tmp_path, consented=True)
    cache = _cache(muse_cache)
    original = json.loads(json.dumps(cache["frozen"]))
    tamper(cache["frozen"]["2026/10/10"])
    muse_cache.write_text(json.dumps(cache))

    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result == first
    assert result.hosts["other"]["2026-10-10"]["input"] == 60
    assert _cache(muse_cache)["frozen"] == original


# Generated by the /ship test coverage audit (pass 1).
# Value: protects=freezing a day never changes its published totals: a step restated in a
# same-day subagent log still counts once after the freeze; fails_when=_freeze_muse_day stops
# deduping turn keys, doubling the frozen total; why_new=freeze tests use unique ids per log and
# cross-day copies stay live; seam=none; no_claim=none beyond protects
def test_freezing_keeps_a_same_day_restated_step_counted_once(muse_cache, tmp_path, monkeypatch):
    for session in ("s1", "s1/subagent/c1"):
        _settled_session(tmp_path, "shared", "2026/10/10", session=session)
    first = hu.read_muse_usage(tmp_path, consented=True)
    assert first.complete
    assert first.hosts["other"]["2026-10-10"]["input"] == 60
    frozen = _cache(muse_cache)["frozen"]["2026/10/10"]
    assert frozen["logs"] == 2
    assert frozen["days"]["2026-10-10"]["muse-wonder-1.0"]["input"] == 60

    with monkeypatch.context() as m:
        m.setattr(hu, "_read_muse_file", Mock(side_effect=AssertionError("reopened")))
        assert hu.read_muse_usage(tmp_path, consented=True) == first


def test_old_day_directories_are_read_and_counted(muse_cache, tmp_path, monkeypatch):
    """No calendar floor: history older than the wire window is still read once."""
    _settled_session(tmp_path, "ancient", "2026/07/01")
    _write_session(tmp_path, [_record("live", _usage())])
    monkeypatch.setattr(hu, "MUSE_SESSIONS_PATH", tmp_path)
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert set(result.hosts["other"]) == {"2026-07-01", "2026-10-13"}
    assert hu.muse_usage_diag(count_logs=True)["files_on_disk"] == 2
    assert hu.muse_usage_diag()["files_on_disk"] is None  # fail closed


def test_a_frozen_day_whose_directory_is_gone_is_evicted(muse_cache, tmp_path):
    _write_session(tmp_path, [_record("live", _usage())])
    hu.read_muse_usage(tmp_path, consented=True)
    cache = _cache(muse_cache)
    gone = {"input": 1, "cache_create": 0, "cache_read": 0, "output": 1}
    cache["frozen"]["2026/07/02"] = {
        "digest": "0" * 64,
        "logs": 1,
        "check_seq": 0,
        "days": {"2026-07-02": {"muse-wonder-1.0": gone}},
        "partial_days": [],
        "keys": [],
    }
    muse_cache.write_text(json.dumps(cache))
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert set(result.hosts["other"]) == {"2026-10-13"}
    assert "2026/07/02" not in _cache(muse_cache)["frozen"]


def test_a_session_resumed_months_later_is_counted(muse_cache, tmp_path):
    """A frozen day stays checkable at any age, so a resumed old session counts."""
    path = _settled_session(tmp_path, "june", "2026/06/01")
    hu.read_muse_usage(tmp_path, consented=True)
    assert "2026/06/01" in _cache(muse_cache)["frozen"]
    now_step = _record("october", _usage(output_tokens=5), at=NOW - timedelta(hours=1))
    with path.open("a") as fp:
        fp.write(json.dumps(now_step) + "\n")
    _age(path, timedelta(hours=1))
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    assert result.hosts["other"]["2026-06-01"]["input"] == 60
    assert result.hosts["other"]["2026-10-14"]["output"] == 5


def test_a_copy_into_an_existing_older_session_counts_once(muse_cache, tmp_path):
    """A frozen day keeps its keys while a live session could still accept a copy."""
    original = _record("orig", _usage(), at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
    old = _write_session(tmp_path, [original], day="2026/10/10")
    _age(old, timedelta(days=3))
    live_step = _record("x", _usage(), at=datetime(2026, 10, 11, 12, tzinfo=timezone.utc))
    live = _write_session(tmp_path, [live_step], "s2", day="2026/10/11")  # fresh: stays live
    hu.read_muse_usage(tmp_path, consented=True)
    frozen = _cache(muse_cache)["frozen"]
    assert set(frozen) == {"2026/10/10"}
    assert frozen["2026/10/10"]["keys"]  # 10/11 is live and accepts 10/10 steps

    with live.open("a") as fp:
        fp.write(json.dumps(original) + "\n")
    counted = hu.read_muse_usage(tmp_path, consented=True)
    assert counted.complete
    assert counted.hosts["other"]["2026-10-10"]["input"] == 60  # once, not twice
    assert "2026/10/10" not in _cache(muse_cache)["frozen"]  # shared step keeps it live


def test_a_conflicting_copy_of_a_frozen_step_refuses(muse_cache, tmp_path):
    original = _record("orig", _usage(), at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
    _age(_write_session(tmp_path, [original], day="2026/10/10"), timedelta(days=3))
    live_step = _record("x", _usage(), at=datetime(2026, 10, 11, 12, tzinfo=timezone.utc))
    live = _write_session(tmp_path, [live_step], "s2", day="2026/10/11")
    hu.read_muse_usage(tmp_path, consented=True)
    altered = _record(
        "orig", _usage(input_tokens=500), at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    )
    with live.open("a") as fp:
        fp.write(json.dumps(altered) + "\n")
    refused = hu.read_muse_usage(tmp_path, consented=True)
    assert not refused.complete
    assert refused.reason == "unsupported"


def test_frozen_keys_drop_once_no_session_can_accept_a_copy(muse_cache, tmp_path):
    _settled_session(tmp_path, "old", "2026/10/10")
    hu.read_muse_usage(tmp_path, consented=True)  # freezes with keys
    hu.read_muse_usage(tmp_path, consented=True)  # no live day: 10/10 is uncopyable
    assert _cache(muse_cache)["frozen"]["2026/10/10"]["keys"] == []
    copied = _record("old", _usage(), at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
    _write_session(tmp_path, [copied], "resumed", day="2026/10/13")
    refused = hu.read_muse_usage(tmp_path, consented=True)  # the step-lead rule refuses
    assert not refused.complete
    assert refused.reason == "unsupported"


def test_idle_live_days_do_not_hold_frozen_keys_open(muse_cache, tmp_path):
    """An idle live pair (a detected copy that never freezes) must not pin key retention."""
    shared = _record("pair", _usage(), at=datetime(2026, 3, 1, 23, tzinfo=timezone.utc))
    for day in ("2026/03/01", "2026/03/02"):
        _age(_write_session(tmp_path, [shared], day=day), timedelta(days=200))
    for index, day in enumerate(("2026/10/01", "2026/10/05", "2026/10/10")):
        _settled_session(tmp_path, f"k{index}", day)
    result = hu.read_muse_usage(tmp_path, consented=True)
    assert result.complete
    frozen = _cache(muse_cache)["frozen"]
    assert set(frozen) == {"2026/10/01", "2026/10/05", "2026/10/10"}  # the pair stays live
    # First read over old history stores no keys: nothing open can accept a copy.
    assert all(record["keys"] == [] for record in frozen.values())
    assert result.hosts["other"]["2026-03-01"]["input"] == 60  # the pair counts once


def test_status_diag_path_never_walks_muse_sessions(muse_cache, tmp_path, monkeypatch):
    """Without a floor the on-disk count walks all history: only mm diag pays for it."""
    _write_session(tmp_path, [_record("e1", _usage())])
    monkeypatch.setattr(hu, "MUSE_SESSIONS_PATH", tmp_path)
    monkeypatch.setattr(hu, "_count_muse_ledgers", Mock(side_effect=AssertionError("walked")))
    hu.read_muse_usage(tmp_path, consented=True)
    assert hu.reader_usage_diag("muse")["files_on_disk"] is None
