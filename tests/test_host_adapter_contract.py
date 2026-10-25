"""Runtime contract suite for host usage reader adapters (Track 84A).

Verifies the host reader adapter contract documented in
``docs/invariants/events-retro.md#host-reader-adapter-contract`` across the
extracted ``mind_meld.hosts.codex`` adapter and the reference in-place
``mind_meld.host_usage`` Muse reader, plus ``mind_meld.host_registry`` and
``mind_meld.events_tail`` dispatch boundaries.
"""

from __future__ import annotations

import errno
import fcntl
import gc
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args
from unittest.mock import Mock

import pytest

from mind_meld import events, events_tail, host_registry, lockedjson
from mind_meld import host_usage as hu
from mind_meld.hosts import codex as codex_host

_OBSERVED = "2026-09-04T10:00:00+00:00"
_NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
_CONTRACT_ANCHOR = "events-retro.md#host-reader-adapter-contract"


def _clause_msg(row: str, clause: str, detail: str) -> str:
    return f"[{row}] clause {clause}: {detail} ({_CONTRACT_ANCHOR})"


def _seed_blocker(
    cache: Path,
    *,
    reason: str | None = "unsupported",
    since: str | None = _OBSERVED,
    files: dict | None = None,
) -> dict:
    cache.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "version": hu.CACHE_VERSION,
        "last_reason": reason,
        "last_reason_since": since,
        "complete_once": True,
        "usage_less_skipped": 2,
        "files": files or {},
    }
    if cache == hu.MUSE_CACHE_PATH:
        data["muse_format"] = hu._MUSE_CACHE_FORMAT
    cache.write_text(json.dumps(data))
    return data


@pytest.fixture(params=["codex", "muse"])
def reader_case(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    name: str = request.param
    owner = {"codex": codex_host, "muse": hu}[name]
    cache = {"codex": codex_host.CACHE_PATH, "muse": hu.MUSE_CACHE_PATH}[name]
    root = tmp_path / "reader-sessions"
    monkeypatch.setattr(codex_host, "CODEX_SESSIONS_PATH", root)

    def read() -> hu.HostUsageResult:
        if name == "codex":
            return codex_host.read_codex_usage(root)
        return hu.read_muse_usage(root, consented=True)

    diag = {"codex": codex_host.codex_usage_diag, "muse": hu.muse_usage_diag}[name]

    def set_scan(result: hu.HostUsageResult, staged: dict | None = None, learned: bool = False):
        if name == "muse":
            # Muse also returns its frozen days: (result, live, frozen, learned, saw).
            output = (result, staged or {}, {}, learned, True)
        else:
            output = (result, staged or {}, learned)
        scan = Mock(return_value=output)
        monkeypatch.setattr(owner, f"_scan_{name}_root", scan)
        return scan

    return read, cache, diag, set_scan


def _row_from_cache(cache: Path) -> str:
    return "muse" if cache == hu.MUSE_CACHE_PATH else "codex"


@pytest.mark.parametrize("since", [None, "garbage", "2026-09-04T10:00:00"])
def test_migrated_reader_root_gains_first_observed_once(reader_case, since, monkeypatch):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    _seed_blocker(cache, since=since)
    set_scan(hu._incomplete("unsupported"))
    carry = hu._carry_reason
    monkeypatch.setattr(hu, "_carry_reason", lambda a, b, c, now, **kw: carry(a, b, c, _NOW, **kw))
    writer = Mock(wraps=lockedjson._write_json)
    monkeypatch.setattr(lockedjson, "_write_json", writer)
    assert read().reason == "unsupported", _clause_msg(
        row, "standing_reason", "unsupported returned"
    )
    writer.assert_called_once()
    assert diag()["last_reason_since"] == _NOW.isoformat(), _clause_msg(
        row, "standing_reason", "last_reason_since backfilled once"
    )
    writer.reset_mock()
    assert read().reason == "unsupported", _clause_msg(row, "standing_reason", "repeat unsupported")
    writer.assert_not_called()
    assert diag()["last_reason_since"] == _NOW.isoformat(), _clause_msg(
        row, "standing_reason", "last_reason_since stable without rewrite"
    )


@pytest.mark.parametrize("learned", [False, True])
def test_failed_reader_writes_only_when_pair_changes_or_file_learned(
    reader_case, learned, monkeypatch
):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    _seed_blocker(cache)
    staged = {"learned": {"no_ledger": True}} if learned else {}
    set_scan(hu._incomplete("deadline"), staged, learned)
    writer = Mock(wraps=lockedjson._write_json)
    monkeypatch.setattr(lockedjson, "_write_json", writer)
    assert read().reason == "deadline", _clause_msg(row, "cache_write_gate", "deadline returned")
    assert writer.call_count == int(learned), _clause_msg(
        row, "cache_write_gate", "writes only when learned or reason changed"
    )
    assert json.loads(cache.read_text())["files"] == staged, _clause_msg(
        row, "cache_write_gate", "staged files preserved"
    )
    assert diag()["last_reason"] == "unsupported", _clause_msg(
        row, "standing_reason", "permanent blocker preserved over transient deadline"
    )
    assert diag()["last_reason_since"] == _OBSERVED, _clause_msg(
        row, "standing_reason", "permanent blocker timestamp preserved"
    )


def test_pre_lock_expiry_never_creates_cache(reader_case, monkeypatch):
    read, cache, _diag, set_scan = reader_case
    row = _row_from_cache(cache)
    scan = set_scan(hu.HostUsageResult({}, True))
    monkeypatch.setattr(hu, "_expired", lambda d: True)
    assert read().reason == "deadline", _clause_msg(row, "pre_lock_deadline", "expired before lock")
    assert not cache.exists(), _clause_msg(row, "pre_lock_deadline", "cache not created")
    scan.assert_not_called()


def test_post_lock_expiry_records_deadline_once_without_scanning(reader_case, monkeypatch):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    files = {"kept": {"no_ledger": True, "usage_less_skipped": 2}}
    _seed_blocker(cache, reason=None, since=None, files=files)
    scan = set_scan(hu.HostUsageResult({}, True))
    writer = Mock(wraps=lockedjson._write_json)
    monkeypatch.setattr(lockedjson, "_write_json", writer)
    for _attempt in range(2):
        ticks = iter([False, True])
        monkeypatch.setattr(hu, "_expired", lambda d: next(ticks))
        assert read().reason == "deadline", _clause_msg(
            row, "post_lock_deadline", "deadline reason"
        )
        scan.assert_not_called()
        assert writer.call_count == 1, _clause_msg(
            row, "post_lock_deadline", "records deadline once without rewrite"
        )
        data = json.loads(cache.read_text())
        assert data["files"] == files, _clause_msg(
            row, "post_lock_deadline", "prior cached files retained"
        )
        assert diag()["last_reason"] == "deadline", _clause_msg(
            row, "post_lock_deadline", "diag exposes deadline"
        )
        if "complete_once" in data:
            assert data["complete_once"] is True
            # Muse has no usage-less records, so its root tally is always 0.
            assert data["usage_less_skipped"] == (0 if cache == hu.MUSE_CACHE_PATH else 2)


def test_complete_then_expired_records_deadline_and_keeps_cache(reader_case, monkeypatch):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    _seed_blocker(cache)
    set_scan(hu.HostUsageResult({}, True), {"learned": {"no_ledger": True}}, True)
    ticks = iter([False, False, True])
    monkeypatch.setattr(hu, "_expired", lambda d: next(ticks))
    assert read().reason == "deadline", _clause_msg(
        row, "post_scan_deadline", "expired after scan demotes to deadline"
    )
    assert "learned" in json.loads(cache.read_text())["files"], _clause_msg(
        row, "post_scan_deadline", "completed scan entries committed"
    )
    assert diag()["last_reason"] == "deadline", _clause_msg(
        row, "post_scan_deadline", "deadline replaces prior blocker after complete scan"
    )
    assert diag()["last_reason_since"] != _OBSERVED


@pytest.mark.parametrize("write_state", ["intact", "empty", "partial"])
@pytest.mark.parametrize("complete", [False, True])
def test_cache_write_failure_notices_and_preserves_scan_result(
    reader_case, write_state, complete, monkeypatch, capsys
):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    _seed_blocker(cache)
    before = cache.read_bytes()
    result = hu.HostUsageResult({}, True) if complete else hu._incomplete("partial")
    set_scan(result, {"learned": {"no_ledger": True}}, True)
    real_write = lockedjson._write_json

    def fail_write(fd, data, **kwargs):
        if write_state != "intact":
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            if write_state == "partial":
                os.write(fd, b'{"files":')
        return OSError(errno.ENOSPC, "sensitive path must never be printed")

    monkeypatch.setattr(lockedjson, "_write_json", fail_write)
    assert read() is result, _clause_msg(
        row, "cache_write_failure", "scan result returned despite cache write error"
    )
    assert capsys.readouterr().err == "mm: notice: host token cache write failed: ENOSPC\n", (
        _clause_msg(row, "cache_write_failure", "errno-only notice emitted")
    )
    if write_state != "intact":
        assert diag()["cache_state"] == ("missing" if write_state == "empty" else "unreadable")
        assert diag()["last_reason"] is None
    else:
        assert cache.read_bytes() == before
    monkeypatch.setattr(lockedjson, "_write_json", real_write)
    set_scan(hu._incomplete("unsupported"))
    assert read().reason == "unsupported"
    assert diag()["last_reason"] == "unsupported"


def test_slow_commit_does_not_reclassify_completed_read(reader_case, monkeypatch):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    _seed_blocker(cache)
    result = hu.HostUsageResult({}, True)
    set_scan(result)
    expired = False
    real_write = lockedjson._write_json

    def commit_after_deadline(fd, data, **kwargs):
        nonlocal expired
        expired = True
        return real_write(fd, data, **kwargs)

    monkeypatch.setattr(hu, "_expired", lambda d: expired)
    monkeypatch.setattr(lockedjson, "_write_json", commit_after_deadline)
    assert read() is result, _clause_msg(
        row, "commit_timing", "slow cache commit does not reclassify completed scan"
    )
    assert diag()["last_reason"] is None


def test_contended_reader_keeps_prior_blocker(reader_case):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    _seed_blocker(cache)
    scan = set_scan(hu.HostUsageResult({}, True))
    before = cache.read_bytes()
    with cache.open("r+") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX)
        assert read().reason == "locked", _clause_msg(
            row, "lock_contention", "contended lock returns reason='locked'"
        )
    scan.assert_not_called()
    assert cache.read_bytes() == before, _clause_msg(
        row, "lock_contention", "contended lock never mutates cache"
    )
    assert diag()["last_reason"] == "unsupported"


@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("enabled", [True, False])
def test_reader_pauses_gc_and_restores_caller_state(reader_case, monkeypatch, error, enabled):
    read, cache, _diag, _set_scan = reader_case
    row = _row_from_cache(cache)
    prior = gc.isenabled()
    thresholds = gc.get_threshold()
    starts = []

    def callback(phase, info):
        if phase == "start":
            starts.append(info)

    def scan(*args, **kwargs):
        assert not gc.isenabled(), _clause_msg(row, "gc_pause", "gc disabled during scan")
        before = len(starts)
        cycles = []
        for _ in range(100):
            cycle = []
            cycle.append(cycle)
            cycles.append(cycle)
        assert len(starts) == before
        if error:
            raise error()
        if cache == hu.MUSE_CACHE_PATH:
            return (hu.HostUsageResult({}, True), {}, {}, False, False)
        return (hu.HostUsageResult({}, True), {}, False)

    owner, name = {
        codex_host.CACHE_PATH: (codex_host, "_scan_codex_root"),
        hu.MUSE_CACHE_PATH: (hu, "_scan_muse_root"),
    }[cache]
    monkeypatch.setattr(owner, name, scan)
    try:
        gc.set_threshold(1, 1, 1)
        gc.callbacks.append(callback)
        (gc.enable if enabled else gc.disable)()
        if error:
            with pytest.raises(error):
                read()
        else:
            assert read().complete
        assert gc.isenabled() is enabled, _clause_msg(
            row, "gc_pause", "caller gc state restored on exit"
        )
    finally:
        gc.callbacks.remove(callback)
        gc.set_threshold(*thresholds)
        (gc.enable if prior else gc.disable)()


def test_complete_late_timing_prunes_and_preserves_deadline_since(reader_case, monkeypatch):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    _seed_blocker(cache, files={"deleted": {}})
    set_scan(hu.HostUsageResult({}, True), {"live": {"no_ledger": True}})
    for idx in range(2):
        clock = iter([10.0, 10.3])  # reader entry, result ready
        expiry = iter([False, False, True])
        monkeypatch.setattr(hu.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(hu, "_expired", lambda d: next(expiry))
        assert read().reason == "deadline", _clause_msg(row, "read_timing", "late deadline")
        data = json.loads(cache.read_text())
        assert set(data["files"]) == {"live"}
        assert data["last_complete_ms"] == 300
        assert data["last_deadline_allotted_ms"] == 5000
        assert data["last_reason"] == "deadline"
        assert data["last_reason_since"] != _OBSERVED
        assert diag()["last_complete_at"] == data["last_complete_at"]
        if idx == 0:
            since = data["last_reason_since"]
        else:
            assert data["last_reason_since"] == since


def test_timing_carries_on_partial_write_and_drops_allotment_when_clear(reader_case, monkeypatch):
    read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    data = _seed_blocker(cache, reason="deadline")
    data.update(last_complete_ms=197, last_complete_at=_OBSERVED, last_deadline_allotted_ms=250)
    cache.write_text(json.dumps(data))
    set_scan(hu._incomplete("partial"), {"new": {}}, True)
    assert read().reason == "partial", _clause_msg(row, "read_timing", "partial reason")
    after = json.loads(cache.read_text())
    assert after["last_complete_ms"] == 197
    assert after["last_complete_at"] == _OBSERVED
    assert "last_deadline_allotted_ms" not in after
    set_scan(hu.HostUsageResult({}, True))
    assert read().complete
    assert diag()["last_reason"] is None
    assert diag()["last_complete_ms"] is not None


def test_unchanged_deadline_rewrites_when_allotted_budget_changes(reader_case, monkeypatch):
    _read, cache, diag, set_scan = reader_case
    row = _row_from_cache(cache)
    data = _seed_blocker(cache, reason="deadline")
    data.update(last_complete_ms=197, last_complete_at=_OBSERVED, last_deadline_allotted_ms=250)
    cache.write_text(json.dumps(data))
    set_scan(hu._incomplete("deadline"), {}, False)
    clock = [1000.0]
    monkeypatch.setattr(hu.time, "monotonic", lambda: clock[0])
    if cache == codex_host.CACHE_PATH:
        result = codex_host.read_codex_usage(deadline=1000.4)
    else:
        result = hu.read_muse_usage(consented=True, deadline=1000.4)
    assert result.reason == "deadline", _clause_msg(row, "read_timing", "deadline reason")
    after = json.loads(cache.read_text())
    assert after["last_reason"] == "deadline"
    assert after["last_reason_since"] == _OBSERVED
    assert after["last_deadline_allotted_ms"] == 400
    assert after["last_complete_ms"] == 197
    assert diag()["last_deadline_allotted_ms"] == 400


@pytest.mark.parametrize("reason", ["unsupported", "partial", None])
def test_allotment_is_hidden_unless_reason_is_deadline(reader_case, reason):
    _read, cache, diag, _set_scan = reader_case
    row = _row_from_cache(cache)
    data = _seed_blocker(cache, reason=reason or "unsupported")
    if reason is None:
        data["last_reason"] = None
    data.update(last_deadline_allotted_ms=250, last_complete_ms=197)
    cache.write_text(json.dumps(data))
    assert diag()["last_deadline_allotted_ms"] is None, _clause_msg(
        row, "read_timing", "allotment hidden unless last_reason is deadline"
    )


@pytest.mark.parametrize("bad", [-1, 86_400_001, True, 1.5, "197", None])
def test_invalid_timing_fields_are_unknown(reader_case, bad):
    _read, cache, diag, _set_scan = reader_case
    row = _row_from_cache(cache)
    data = _seed_blocker(cache, reason="deadline")
    data.update(last_complete_ms=bad, last_deadline_allotted_ms=bad, last_complete_at=bad)
    cache.write_text(json.dumps(data))
    view = diag()
    assert view["last_complete_ms"] is None, _clause_msg(
        row, "read_timing", "invalid last_complete_ms rejected"
    )
    assert view["last_deadline_allotted_ms"] is None
    assert view["last_complete_at"] is None


@pytest.mark.parametrize("row", ["codex", "muse"])
def test_consent_gate_and_closed_default(row: str, tmp_path: Path):
    """Consent is enforced at the source gate or closed-default reader parameter."""
    assert events_tail.HOST_READER_SOURCE_GATE[row] == row, _clause_msg(
        row, "consent", "source gate maps reader to enabled sync source"
    )
    assert events_tail._default_host_readers([]) == (), _clause_msg(
        row, "consent", "unconsented reader omitted from default sweep"
    )
    chosen = events_tail._default_host_readers([{"name": row, "type": "generic"}])
    assert [name for name, _ in chosen] == [row], _clause_msg(
        row, "consent", "enabled source selects reader"
    )
    if row == "muse":
        unconsented = hu.read_muse_usage(tmp_path / "muse", consented=False)
        assert not unconsented.complete and unconsented.reason == "no_metadata_ledger", _clause_msg(
            row, "consent", "closed-default unconsented call returns no_metadata_ledger"
        )
        assert not hu.MUSE_CACHE_PATH.exists(), _clause_msg(
            row, "consent", "unconsented call does not touch cache"
        )


@pytest.mark.parametrize("row", ["codex", "muse"])
@pytest.mark.parametrize("failure_mode", ["incomplete", "exception"])
def test_failure_isolation_keeps_sibling_reader_publishing(row: str, failure_mode: str):
    """A failing or raising reader is dropped with a closed-vocabulary reason; siblings publish."""
    sibling = "muse" if row == "codex" else "codex"
    day = "2026-09-09"
    bucket = {"input": 10, "cache_create": 0, "cache_read": 0, "output": 5}

    def failing_reader(*, deadline: float):
        if failure_mode == "exception":
            raise RuntimeError("synthetic reader crash")
        return hu._incomplete("unsupported")

    def healthy_sibling(*, deadline: float):
        return hu.HostUsageResult({sibling: {day: bucket}}, complete=True)

    readers = (
        ((row, failing_reader), (sibling, healthy_sibling))
        if row == "codex"
        else ((sibling, healthy_sibling), (row, failing_reader))
    )
    capture = events_tail._capture_host_usage(readers, deadline=1000.0, now=lambda: 0.0)
    closed_reasons = set(get_args(hu.Reason)) | {events_tail._HOST_UNKNOWN_REASON}
    assert capture.complete, _clause_msg(
        row, "failure_isolation", "sibling completion keeps capture complete"
    )
    assert capture.token_sources == (sibling,), _clause_msg(
        row, "failure_isolation", "only healthy sibling listed in token_sources"
    )
    assert len(capture.dropped) == 1 and capture.dropped[0][0] == row, _clause_msg(
        row, "failure_isolation", "failing reader recorded in dropped"
    )
    assert capture.dropped[0][1] in closed_reasons, _clause_msg(
        row, "failure_isolation", "dropped reason stays in closed vocabulary"
    )


@pytest.mark.parametrize("row", ["codex", "muse"])
def test_registry_and_diag_completeness(row: str):
    """Registry tables, diag keys, coldness predicate, and unknown-reader error."""
    assert tuple(host_registry.HOST_READER_DIAGS) == events.ACTIVE_HOST_READERS, _clause_msg(
        row, "registry", "HOST_READER_DIAGS matches ACTIVE_HOST_READERS in order"
    )
    assert set(events_tail.WARMABLE_HOST_READERS) <= set(events.ACTIVE_HOST_READERS), _clause_msg(
        row, "registry", "WARMABLE_HOST_READERS is a subset of ACTIVE_HOST_READERS"
    )
    assert set(events_tail.HOST_READER_SOURCE_GATE) == set(events.ACTIVE_HOST_READERS), _clause_msg(
        row, "registry", "HOST_READER_SOURCE_GATE covers ACTIVE_HOST_READERS"
    )
    spec = host_registry.HOST_READER_DIAGS[row]
    assert callable(getattr(spec.owner, spec.function)), _clause_msg(
        row, "registry", "diag function resolves on declared owner module"
    )
    state = host_registry.reader_usage_diag(row)
    required_keys = {
        "cache_state",
        "last_reason",
        "last_reason_since",
        "model_count",
        "models",
        "files_cached",
        "files_on_disk",
        spec.ready_key,
    }
    assert required_keys <= set(state), _clause_msg(
        row, "registry", f"diag state carries required keys {sorted(required_keys)}"
    )
    assert host_registry.reader_cache_cold(row, state) is True, _clause_msg(
        row, "registry", "missing cache reports cold=True"
    )
    assert (
        host_registry.reader_cache_cold(row, {**state, spec.ready_key: spec.ready_value}) is False
    ), _clause_msg(row, "registry", "ready value reports cold=False")
    with pytest.raises(
        ValueError,
        match="unknown host reader 'unknown-reader'; register it in host_registry.py",
    ):
        host_registry.warm_host_cache_inline(reader="unknown-reader")


@pytest.mark.parametrize("row", ["codex", "muse"])
def test_call_time_owner_lookup_reaches_dispatch_sites(row: str, monkeypatch: pytest.MonkeyPatch):
    """Patching the owner module after import intercepts warm, default sweep, and diag."""
    spec = host_registry.HOST_READER_DIAGS[row]
    read_name = f"read_{row}_usage"
    sentinel_result = hu.HostUsageResult(
        {row: {"2026-09-09": {"input": 42, "cache_create": 0, "cache_read": 0, "output": 1}}},
        complete=True,
    )
    sentinel_diag = {"cache_state": "sentinel", spec.ready_key: spec.ready_value}
    monkeypatch.setattr(spec.owner, read_name, lambda *_a, **_kw: sentinel_result)
    monkeypatch.setattr(spec.owner, spec.function, lambda *_a, **_kw: sentinel_diag)

    assert host_registry.warm_host_cache_inline(reader=row) is sentinel_result, _clause_msg(
        row, "call_time_lookup", "warm_host_cache_inline resolves reader on owner at call time"
    )
    assert host_registry.reader_usage_diag(row) is sentinel_diag, _clause_msg(
        row, "call_time_lookup", "reader_usage_diag resolves diag on owner at call time"
    )
    readers = dict(events_tail._default_host_readers([{"name": row, "type": "generic"}]))
    assert readers[row](deadline=1000.0) is sentinel_result, _clause_msg(
        row, "call_time_lookup", "_default_host_readers resolves reader on owner at call time"
    )


def test_hosts_path_globals_isolated_under_tmp_path(tmp_path: Path):
    """Every Path-valued module global on mind_meld.hosts.* resolves under tmp_path in tests."""
    checked = 0
    for attr, value in vars(codex_host).items():
        if isinstance(value, Path):
            checked += 1
            assert value.is_relative_to(tmp_path), _clause_msg(
                "codex", "path_isolation", f"codex.{attr}={value} must resolve under tmp_path"
            )
    assert checked >= 3


@pytest.mark.parametrize("mod", ["mind_meld.hosts.codex", "mind_meld.host_registry"])
def test_fresh_subprocess_import_never_loads_cli(mod: str):
    """Importing an adapter or host_registry in a fresh interpreter never loads mind_meld.cli."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, {mod}; assert 'mind_meld.cli' not in sys.modules, list(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, _clause_msg(mod, "subprocess_no_cli", proc.stderr or proc.stdout)
