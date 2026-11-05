"""Grok Build usage reader (Track 110A): the ``usage.json`` source contract.

Pins the source choice and counter semantics from
``tests/fixtures/host_sessions/grok/CONTRACT.md`` against sanitized fixtures, the
closed-by-default consent gate, empty-session handling, cross-file dedup, resumable
progress, failure isolation and the Grok row in the fleet retro.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from mind_meld import events, events_tail, host_registry
from mind_meld import host_usage as hu
from mind_meld import token_usage as tu
from mind_meld.hosts import grok as grok_host
from mind_meld.skills.retro_fleet import aggregator as agg

FIXTURES = Path(__file__).parent / "fixtures" / "host_sessions" / "grok"
SESSIONS = FIXTURES / "sessions"
SRC = Path(__file__).resolve().parent.parent / "src" / "mind_meld"

A1 = "00000000-0000-4000-8000-00000000a001"
A2 = "00000000-0000-4000-8000-00000000a002"
A3 = "00000000-0000-4000-8000-00000000a003"
A4 = "00000000-0000-4000-8000-00000000a004"

BUILD = "grok-4.7-build"

# Disjoint expectations derived by hand from the fixture rows (CONTRACT.md semantics):
# input = inputTokens - cachedReadTokens, cache_read = cachedReadTokens, output = outputTokens
# (reasoning is inside output), the fork's repeated rows counted once.
EXPECTED_DAYS = {
    "2026-11-03": {"input": 1100, "cache_create": 0, "cache_read": 1900, "output": 300},
    "2026-11-04": {"input": 2100, "cache_create": 0, "cache_read": 2400, "output": 450},
}


def _read(root: Path = SESSIONS, **kwargs) -> hu.HostUsageResult:
    return grok_host.read_grok_usage(root, consented=True, **kwargs)


def _copy_tree(tmp_path: Path, *names: str) -> Path:
    """A writable copy of the fixture sessions; ``names`` limits it to those cwd dirs."""
    dest = tmp_path / "sessions"
    for cwd in names or ("cwd-a", "cwd-b"):
        shutil.copytree(SESSIONS / cwd, dest / cwd)
    return dest


def _usage_file(root: Path, sid: str) -> Path:
    return next(root.glob(f"*/{sid}/usage.json"))


def _record_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Paths opened through ``os.open`` or ``open`` (the reader's only two doors)."""
    opened: list[str] = []
    real_os_open = os.open
    real_open = open

    def os_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_os_open(path, *args, **kwargs)

    def builtin_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr("builtins.open", builtin_open)
    return opened


def _cache() -> dict:
    return json.loads(grok_host.CACHE_PATH.read_text())


# --- Contract pin and totals -------------------------------------------------


def test_contract_census_pin_matches_src_constant():
    text = (FIXTURES / "CONTRACT.md").read_text()
    assert f"Grok Build {grok_host.GROK_USAGE_CENSUS_HOST_VERSION}" in text
    assert "usage.json" in text and "per turn" in text.lower()


def test_fixture_totals_pin_source_choice_and_semantics():
    result = _read()
    assert result.complete and result.reason is None
    # Rows are summed per turn: the fork (A3) repeats A2's two rows, which count once;
    # ``session`` (inherited history) is never read.
    assert result.hosts == {"grok": EXPECTED_DAYS}
    # Reasoning is inside output: 300 on 2026-11-03 is the output sum, not output + reasoning.
    assert result.hosts["grok"]["2026-11-03"]["output"] == 100 + 200
    # Uncached input = input - cachedRead - cacheCreate.
    assert result.hosts["grok"]["2026-11-03"]["input"] == (1000 - 400) + (2000 - 1500)
    by_model = result.tokens_by_day["2026-11-04"]["by_model"]
    assert set(by_model) == {BUILD, "grok-4.7"}
    assert by_model["grok-4.7"] == {
        "input": 200,
        "cache_create": 0,
        "cache_read": 100,
        "output": 30,
    }
    assert result.partial_days == frozenset()


def test_fork_repeating_parent_rows_counts_each_turn_once(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    with_fork = _read(root)
    shutil.rmtree(_usage_file(root, A3).parent)
    without_fork = _read(root)
    third = EXPECTED_DAYS["2026-11-04"]
    # Dropping the fork removes only its own third turn (input 400, cache_read 100, output 50).
    assert without_fork.hosts["grok"]["2026-11-04"] == {
        "input": third["input"] - 400,
        "cache_create": 0,
        "cache_read": third["cache_read"] - 100,
        "output": third["output"] - 50,
    }
    assert with_fork.hosts["grok"]["2026-11-03"] == without_fork.hosts["grok"]["2026-11-03"]


def test_repeated_reads_and_resume_count_each_turn_once(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    first = _read(root)
    assert _read(root).hosts == first.hosts
    # Resume appends a row to the same directory and replaces the file.
    path = _usage_file(root, A1)
    doc = json.loads(path.read_text())
    row = copy.deepcopy(doc["turns"][0])
    row.update(turnNumber=2, endedAt="2026-11-04T18:00:00.000000+00:00")
    doc["turns"].append(row)
    path.write_text(json.dumps(doc))
    resumed = _read(root)
    day = resumed.hosts["grok"]["2026-11-04"]
    prior = first.hosts["grok"]["2026-11-04"]
    assert day["input"] == prior["input"] + 600
    assert day["cache_read"] == prior["cache_read"] + 400
    assert day["output"] == prior["output"] + 100
    assert resumed.hosts["grok"]["2026-11-03"] == first.hosts["grok"]["2026-11-03"]


def test_conflicting_duplicate_turn_is_malformed(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    twin = root / "cwd-a" / "00000000-0000-4000-8000-00000000a0ff"
    twin.mkdir()
    doc = json.loads(_usage_file(root, A2).read_text())
    for target in (doc["turns"][0], doc["turns"][0]["modelUsage"][BUILD]):
        target["inputTokens"] += 1
        target["totalTokens"] += 1
    doc["turns"][0]["modelUsage"][BUILD]["inputTokens"] = doc["turns"][0]["inputTokens"]
    doc["turns"][0]["modelUsage"][BUILD]["totalTokens"] = doc["turns"][0]["totalTokens"]
    (twin / "usage.json").write_text(json.dumps(doc))
    result = _read(root)
    assert not result.complete and result.reason == "malformed"
    assert result.hosts == {}


def test_nonzero_cache_creation_marks_the_day_partial(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    path = _usage_file(root, A1)
    doc = json.loads(path.read_text())
    for target in (doc["turns"][0], doc["turns"][0]["modelUsage"][BUILD]):
        target["cacheCreationTokens"] = 50
    path.write_text(json.dumps(doc))
    result = _read(root)
    assert result.complete
    assert result.partial_days == frozenset({"2026-11-03"})


# --- Consent: the never-open rule --------------------------------------------


def _exploding_root() -> Mock:
    root = Mock(spec=Path)
    for name in ("stat", "lstat", "resolve", "iterdir", "is_dir", "exists", "is_symlink"):
        getattr(root, name).side_effect = AssertionError(f"{name} touched an unconsented root")
    return root


def test_unconsented_call_never_touches_the_root_or_cache():
    root = _exploding_root()
    result = grok_host.read_grok_usage(root)
    assert not result.complete and result.reason == "no_metadata_ledger"
    assert root.mock_calls == []
    assert not grok_host.CACHE_PATH.exists()


def test_default_sweep_without_consent_never_touches_the_root(monkeypatch):
    root = _exploding_root()
    monkeypatch.setattr(grok_host, "GROK_SESSIONS_PATH", root)
    sources = [{"name": "grok", "type": "grok"}]
    assert events_tail._default_host_readers(sources) == ()
    assert events_tail._default_host_readers(sources, grok_consented=False) == ()
    assert root.mock_calls == []
    chosen = events_tail._default_host_readers([], grok_consented=True)
    assert [name for name, _ in chosen] == ["grok"]
    assert root.mock_calls == []  # selection alone never reads


def test_reader_opens_only_usage_json_files(tmp_path, monkeypatch):
    root = _copy_tree(tmp_path)
    opened = _record_opens(monkeypatch)
    _read(root)
    under_root = [p for p in opened if p.startswith(str(root))]
    assert under_root, "the reader must have opened the usage files"
    assert {Path(p).name for p in under_root} == {"usage.json"}


# --- Empty sessions ----------------------------------------------------------


def test_empty_sessions_are_cached_zero_contributions_and_not_reread(tmp_path, monkeypatch):
    root = _copy_tree(tmp_path, "cwd-b")
    opened = _record_opens(monkeypatch)
    result = _read(root)
    assert result.complete and result.hosts == {}
    assert [p for p in opened if p.startswith(str(root))] == []
    data = _cache()
    assert data["usage_less_skipped"] == 3 and len(data["files"]) == 3
    assert data["complete_once"] is True
    opened.clear()
    assert _read(root).complete
    assert [p for p in opened if p.startswith(str(root))] == []


def test_unchanged_usage_file_is_not_reread(tmp_path, monkeypatch):
    root = _copy_tree(tmp_path, "cwd-a")
    _read(root)
    opened = _record_opens(monkeypatch)
    assert _read(root).hosts == {"grok": EXPECTED_DAYS}
    assert [p for p in opened if p.startswith(str(root))] == []


def test_symlinked_session_directory_and_usage_file_are_never_followed(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    outside = tmp_path / "outside"
    shutil.copytree(SESSIONS / "cwd-a" / A1, outside)
    (root / "cwd-a" / "00000000-0000-4000-8000-00000000a0aa").symlink_to(outside)
    (root / ".cwd").write_text("not a directory")
    linked = root / "cwd-a" / "00000000-0000-4000-8000-00000000a0bb"
    linked.mkdir()
    (linked / "usage.json").symlink_to(outside / "usage.json")
    assert _read(root).hosts == {"grok": EXPECTED_DAYS}


def test_missing_root_is_a_completed_empty_scan(tmp_path):
    result = _read(tmp_path / "absent")
    assert result.complete and result.hosts == {}


# --- Publication carries only family, model, day, counters -------------------


def test_snapshot_and_cache_carry_no_ids_paths_or_cost(tmp_path):
    root = _copy_tree(tmp_path)
    capture, rows = events_tail._capture_host_snapshot(
        "dev-a",
        (
            (
                "grok",
                lambda *, deadline: grok_host.read_grok_usage(
                    root, deadline=deadline, consented=True
                ),
            ),
        ),
        host_budget_ms=30_000,
    )
    assert capture.complete and len(rows) == 1
    published = json.dumps(rows[0], sort_keys=True)
    assert rows[0]["hosts"] == {"grok": EXPECTED_DAYS}
    assert rows[0]["token_sources"] == ["grok"]
    cached = grok_host.CACHE_PATH.read_text()
    for blob in (published, cached):
        for forbidden in (
            "00000000-0000-4000",
            "cwd-a",
            "cwd-b",
            "costUsd",
            "sessionId",
            str(tmp_path),
            "hook_execution",
        ):
            assert forbidden not in blob, forbidden
    assert grok_host.CACHE_PATH.stat().st_mode & 0o777 == 0o600
    assert set(rows[0]["hosts"]["grok"]["2026-11-03"]) == {
        "input",
        "cache_create",
        "cache_read",
        "output",
    }


# --- Validation: fail closed for this reader alone ---------------------------


def _mutate(tmp_path: Path, change) -> hu.HostUsageResult:
    root = _copy_tree(tmp_path, "cwd-a")
    path = _usage_file(root, A1)
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc))
    return _read(root)


def test_unknown_extra_keys_at_every_level_still_read(tmp_path):
    def add_extras(doc):
        doc["newTopKey"] = {"nested": 1}
        doc["session"]["newSessionKey"] = "x"
        doc["session"]["modelUsage"][BUILD]["newSessionModelKey"] = 1.5
        row = doc["turns"][0]
        row["elapsedMs"] = 12
        row["modelUsage"][BUILD]["newModelKey"] = [1, 2]

    result = _mutate(tmp_path, add_extras)
    assert result.complete
    assert result.hosts == {"grok": EXPECTED_DAYS}


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("turns"),
        lambda d: d.update(turns={"not": "a list"}),
        lambda d: d["turns"][0].pop("endedAt"),
        lambda d: d["turns"][0].pop("turnNumber"),
        lambda d: d["turns"][0].pop("inputTokens"),
        lambda d: d["turns"][0].pop("modelUsage"),
        lambda d: d["turns"][0]["modelUsage"][BUILD].pop("cachedReadTokens"),
        lambda d: d["turns"][0].update(totalTokens=d["turns"][0]["totalTokens"] + 1),
        lambda d: d["turns"][0]["modelUsage"][BUILD].update(totalTokens=1),
        lambda d: d["turns"].append("not an object"),
    ],
)
def test_missing_key_or_broken_identity_is_unsupported(tmp_path, change):
    result = _mutate(tmp_path, change)
    assert not result.complete and result.reason == "unsupported"
    assert result.hosts == {}


def _set_both(doc, key, value):
    doc["turns"][0][key] = value
    doc["turns"][0]["modelUsage"][BUILD][key] = value


@pytest.mark.parametrize(
    "change",
    [
        lambda d: _set_both(d, "outputTokens", "100"),
        lambda d: _set_both(d, "cachedReadTokens", -1),
        lambda d: _set_both(d, "cachedReadTokens", 1.5),
        lambda d: _set_both(d, "cachedReadTokens", True),
        lambda d: _set_both(d, "cachedReadTokens", 2**53 + 1),
        lambda d: _set_both(d, "reasoningTokens", 101),
        lambda d: _set_both(d, "cachedReadTokens", 1001),
        lambda d: d["turns"][0].update(endedAt="2026-11-03T10:00:00"),
        lambda d: d["turns"][0].update(endedAt="not a time"),
        lambda d: d["turns"][0].update(endedAt="2019-01-01T00:00:00+00:00"),
        lambda d: d["turns"][0].update(endedAt="2999-01-01T00:00:00+00:00"),
        lambda d: d["turns"][0].update(turnNumber=-1),
        lambda d: d["turns"][0]["modelUsage"].update({"": d["turns"][0]["modelUsage"][BUILD]}),
        lambda d: d["turns"][0]["modelUsage"].update(
            {"grok-" + "x" * 300: d["turns"][0]["modelUsage"][BUILD]}
        ),
        lambda d: d["turns"][0]["modelUsage"][BUILD].update(inputTokens=999, totalTokens=1099),
    ],
)
def test_bad_counter_time_model_or_sum_is_malformed(tmp_path, change):
    result = _mutate(tmp_path, change)
    assert not result.complete and result.reason == "malformed"
    assert result.hosts == {}


def test_bad_json_is_malformed(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    _usage_file(root, A1).write_text("{not json")
    result = _read(root)
    assert not result.complete and result.reason == "malformed"


@pytest.mark.parametrize("ended_at", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_utc_conversion_overflow_is_a_persisted_malformed_failure(tmp_path, ended_at):
    result = _mutate(tmp_path, lambda doc: doc["turns"][0].update(endedAt=ended_at))
    assert not result.complete and result.reason == "malformed"
    assert result.hosts == {}
    assert grok_host.grok_usage_diag()["last_reason"] == "malformed"


def test_surrogate_model_is_a_persisted_malformed_failure(tmp_path):
    def change(doc):
        models = doc["turns"][0]["modelUsage"]
        models["\ud800"] = models.pop(BUILD)

    result = _mutate(tmp_path, change)
    assert not result.complete and result.reason == "malformed"
    assert result.hosts == {}
    assert grok_host.grok_usage_diag()["last_reason"] == "malformed"


@pytest.mark.parametrize("field,value", [("day", "2026-02-31"), ("model", "\ud800")])
def test_invalid_cached_turn_is_reread_before_publication(tmp_path, monkeypatch, field, value):
    root = _copy_tree(tmp_path, "cwd-a")
    assert _read(root).complete
    data = _cache()
    next(iter(data["files"].values()))["turns"][0][field] = value
    grok_host.CACHE_PATH.write_text(json.dumps(data))
    real_read = grok_host._read_usage_file
    opened = []

    def read(path, before):
        opened.append(path)
        return real_read(path, before)

    monkeypatch.setattr(grok_host, "_read_usage_file", read)
    result = _read(root)
    assert result.complete and result.hosts == {"grok": EXPECTED_DAYS}
    assert len(opened) == 1


def test_file_replaced_while_reading_is_stale(tmp_path, monkeypatch):
    root = _copy_tree(tmp_path, "cwd-a")
    target = _usage_file(root, A1)
    real = grok_host._read_usage_file

    def racing(path, before):
        data = real(path, before)
        if path == target:
            path.write_text(path.read_text() + " ")
        return data

    monkeypatch.setattr(grok_host, "_read_usage_file", racing)
    result = _read(root)
    assert not result.complete and result.reason == "stale"
    monkeypatch.setattr(grok_host, "_read_usage_file", real)
    assert _read(root).complete


def test_unsupported_is_a_standing_blocker_that_survives_transient_failures(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    _usage_file(root, A1).write_text('{"turns": "x"}')
    assert _read(root).reason == "unsupported"
    assert grok_host.grok_usage_diag()["last_reason"] == "unsupported"
    assert _read(root, deadline=time.monotonic() - 1).reason == "deadline"
    assert grok_host.grok_usage_diag()["last_reason"] == "unsupported"
    _usage_file(root, A1).write_text((SESSIONS / "cwd-a" / A1 / "usage.json").read_text())
    assert _read(root).complete
    assert grok_host.grok_usage_diag()["last_reason"] is None


def test_unsupported_grok_leaves_sibling_reader_publishing_and_names_the_fix(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    _usage_file(root, A1).write_text("{}")

    def codex(*, deadline):
        return hu.HostUsageResult(
            {
                "codex": {
                    "2026-11-03": {"input": 5, "cache_create": 0, "cache_read": 0, "output": 1}
                }
            },
            complete=True,
        )

    def grok(*, deadline):
        return grok_host.read_grok_usage(root, deadline=deadline, consented=True)

    capture, rows = events_tail._capture_host_snapshot(
        "dev-a", (("codex", codex), ("grok", grok)), host_budget_ms=30_000
    )
    assert capture.token_sources == ("codex",)
    assert capture.dropped == (("grok", "unsupported"),)
    assert rows[0]["hosts"] == {
        "codex": {"2026-11-03": {"input": 5, "cache_create": 0, "cache_read": 0, "output": 1}}
    }
    assert rows[0]["degraded_sources"] == ["grok"]
    phrase = events_tail._host_skip_phrase("grok", "unsupported", readiness="ready")
    assert "`mm update`" in phrase and "mm disable-source grok" in phrase
    assert "content sync and git/session capture unaffected" in phrase


# --- Budget, resumable progress and warming ----------------------------------


def test_deadline_mid_scan_keeps_staged_sessions_and_resumes(tmp_path, monkeypatch):
    root = _copy_tree(tmp_path)
    processed: list[Path] = []
    tripped = False
    real = grok_host._session_entry

    def counting(path, cached):
        nonlocal tripped
        entry = real(path, cached)
        processed.append(path)
        tripped = len(processed) >= 3
        return entry

    real_expired = hu._expired
    monkeypatch.setattr(grok_host, "_session_entry", counting)
    monkeypatch.setattr(hu, "_expired", lambda deadline: tripped)
    partial = _read(root)
    assert not partial.complete and partial.reason == "deadline" and partial.hosts == {}
    assert len(_cache()["files"]) == 3
    assert _cache()["last_reason"] == "deadline"

    monkeypatch.setattr(grok_host, "_session_entry", real)
    monkeypatch.setattr(hu, "_expired", real_expired)
    opened = _record_opens(monkeypatch)
    final = _read(root)
    assert final.complete and final.hosts == {"grok": EXPECTED_DAYS}
    read_now = [Path(p).parent.name for p in opened if p.startswith(str(root))]
    # The three sessions staged by the first pass are not re-read; the fourth usage file is.
    assert read_now == [A4]
    assert _cache()["last_reason"] is None and len(_cache()["files"]) == 7


def test_pre_lock_deadline_creates_no_cache(tmp_path):
    root = _copy_tree(tmp_path)
    result = _read(root, deadline=time.monotonic() - 1)
    assert result.reason == "deadline"
    assert not grok_host.CACHE_PATH.exists()


def test_pre_v1_11_8_cache_at_the_path_is_discarded(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    grok_host.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    grok_host.CACHE_PATH.write_text(
        json.dumps(
            {
                "version": hu.CACHE_VERSION,
                "complete_once": True,
                "last_reason": "unsupported",
                "last_reason_since": "2026-09-01T00:00:00+00:00",
                "files": {"old": {"turns": []}},
            }
        )
    )
    assert _read(root).complete
    data = _cache()
    assert data["grok_format"] == grok_host._GROK_CACHE_FORMAT
    assert "old" not in data["files"] and data["last_reason"] is None


def test_retired_cache_metadata_is_discarded_even_when_first_scan_hits_deadline(
    tmp_path, monkeypatch
):
    root = _copy_tree(tmp_path, "cwd-a")
    grok_host.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    grok_host.CACHE_PATH.write_text(
        json.dumps(
            {
                "version": hu.CACHE_VERSION,
                "complete_once": True,
                "last_reason": "unsupported",
                "last_reason_since": "2026-09-01T00:00:00+00:00",
                "last_complete_ms": 123,
                "files": {"old": {"turns": []}},
            }
        )
    )
    before = grok_host.grok_usage_diag()
    assert not before["complete_once"] and before["last_reason"] is None
    assert before["last_complete_ms"] is None
    monkeypatch.setattr(
        grok_host, "_scan_grok_root", lambda *args: (hu._incomplete("deadline"), {})
    )
    assert _read(root).reason == "deadline"
    data = _cache()
    assert data["grok_format"] == grok_host._GROK_CACHE_FORMAT
    assert data["files"] == {} and not data["complete_once"]
    after = grok_host.grok_usage_diag()
    assert after["last_reason"] == "deadline" and after["last_complete_ms"] is None


def _stub_warm(monkeypatch):
    warmed: list[str] = []

    def warm(reader, **_kwargs):
        warmed.append(reader)
        return hu.HostUsageResult({}, complete=True)

    monkeypatch.setattr(host_registry, "warm_host_cache_inline", warm)
    return warmed


def test_attended_capture_warms_a_grok_deadline_and_autopush_never_does(monkeypatch):
    monkeypatch.setattr(grok_host, "read_grok_usage", lambda *a, **kw: hu._incomplete("deadline"))
    readers = events_tail._default_host_readers([], grok_consented=True)
    assert [name for name, _ in readers] == ["grok"]
    warmed = _stub_warm(monkeypatch)
    events_tail._capture_host_snapshot("dev-a", readers, host_budget_ms=250, warm_host_cache=None)
    assert warmed == []
    capture, _rows = events_tail._capture_host_snapshot(
        "dev-a", readers, host_budget_ms=250, warm_host_cache=lambda name: None
    )
    assert warmed == ["grok"]
    assert capture.complete


@pytest.mark.parametrize("quiet", [False, True])
def test_events_tail_warms_grok_only_when_attended(tmp_path, monkeypatch, quiet):
    from mind_meld import identity

    root = tmp_path / "events-root"
    sources = [
        {"name": "mm-events", "path": str(root), "type": "generic", "include_dirs": ["events"]}
    ]
    config = {"sync": {"sources": sources}, "retro": {"grok_host_usage": True}}
    monkeypatch.setattr(
        events, "discover_git_roots", lambda *a, **kw: events.GitRootDiscovery((), (), False)
    )
    monkeypatch.setattr(events, "walk_git_projects", lambda *a, **kw: [])
    monkeypatch.setattr(identity, "gather_local_identities", lambda **kw: [])
    monkeypatch.setattr(identity, "refresh_identity_cache", lambda **kw: [])
    seen = []

    def read(*, deadline, consented=False):
        seen.append(consented)
        return hu._incomplete("deadline")

    monkeypatch.setattr(grok_host, "read_grok_usage", read)
    warmed = _stub_warm(monkeypatch)
    degradations = events_tail._run_events_tail(
        config, sources, "dev-a", dry_run=False, quiet=quiet
    )
    assert seen == [True]
    assert warmed == ([] if quiet else ["grok"])
    rows = [
        json.loads(line)
        for path in (root / "events").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    host_rows = [row for row in rows if row["type"] == "host-usage-snapshot"]
    if quiet:
        assert host_rows == []
        assert any("grok deadline" in text for text in degradations)
    else:
        assert host_rows[0]["token_sources"] == ["grok"]


def test_warm_host_cache_inline_reads_grok_consented(tmp_path):
    root = _copy_tree(tmp_path, "cwd-a")
    result = host_registry.warm_host_cache_inline(root, reader="grok")
    assert result.complete and "grok" in result.hosts


# --- Registry, wire slot and call sites --------------------------------------


def test_grok_returns_to_its_pre_v1_11_8_slot_after_codex():
    expected = ("codex", "grok", "cursor", "muse", "agy")
    assert events.ACTIVE_HOST_READERS == expected
    assert events.HOST_USAGE_TOKEN_SOURCES == expected
    assert tuple(host_registry.HOST_READER_DIAGS) == expected
    assert host_registry.HOST_READER_DIAGS["grok"].label == "Grok Build"
    assert events_tail.HOST_READER_SOURCE_GATE["grok"] == "grok"
    assert "grok" in events_tail.WARMABLE_HOST_READERS


def test_every_default_host_readers_call_site_passes_the_same_grok_verdict():
    """Push capture, init backfill, recapture notice, status and diag agree on Grok consent."""
    sites = []
    for path in sorted(SRC.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "_default_host_readers":
                continue
            keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
            sites.append((path.name, node.lineno, keywords.get("grok_consented")))
    assert len(sites) == 5, sites
    for file, line, verdict in sites:
        assert verdict is not None and verdict.endswith("grok_usage_consented(config)"), (
            file,
            line,
            verdict,
        )


def test_grok_consent_follows_the_config_verdict_in_the_status_publication(tmp_path):
    from mind_meld import statusflow

    cfg = {"device": {"id": "dev-a"}, "sync": {"sources": []}, "retro": {"grok_host_usage": True}}
    scan = events.EventScan()
    assert "grok" in statusflow._host_publication(cfg, [], scan, selected=[])["readers"]
    cfg["retro"] = {}
    assert "grok" not in statusflow._host_publication(cfg, [], scan, selected=[])["readers"]
    cfg["sync"]["sources"] = [{"name": "grok", "type": "grok", "path": str(tmp_path)}]
    assert "grok" in statusflow._host_publication(cfg, [], scan, selected=[])["readers"]


def test_diag_reports_cache_inventory_and_session_counts(tmp_path, monkeypatch):
    root = _copy_tree(tmp_path)
    blank = grok_host.grok_usage_diag()
    assert blank["cache_state"] == "missing" and blank["complete_once"] is False
    _read(root)
    cache_only = grok_host.grok_usage_diag()
    assert cache_only["cache_state"] == "ok" and cache_only["complete_once"] is True
    assert cache_only["files_cached"] == 7 and cache_only["files_on_disk"] is None
    assert cache_only["usage_less_skipped"] == 3
    assert cache_only["models"] == ["grok-4.7", BUILD] and cache_only["model_count"] == 2
    assert cache_only["last_complete_ms"] is not None
    monkeypatch.setattr(grok_host, "GROK_SESSIONS_PATH", root)
    assert grok_host.grok_usage_diag(count_logs=True)["files_on_disk"] == 7
    assert host_registry.reader_usage_diag("grok", count_logs=True)["files_on_disk"] == 7
    assert host_registry.reader_usage_diag("grok")["files_on_disk"] is None


# --- The Grok row in the fleet retro -----------------------------------------

_UNTIL = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
_SINCE = _UNTIL - timedelta(days=7)
_RETRO_DAY = "2026-11-04"
_IDS = {"dev-a", "dev-b"}


def _retro_usage(input=0, cache_create=0, cache_read=0, output=0) -> dict:
    return {
        "input": input,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "output": output,
    }


def _host_event(device: str, model: str, counters: dict, **extra) -> dict:
    bucket = tu.zero_day_bucket()
    tu.merge_usage_bucket(bucket, counters)
    bucket["by_model"][model] = dict(counters)
    return {
        "v": 2,
        "type": "host-usage-snapshot",
        "device": device,
        "ts": _UNTIL.isoformat(),
        "hosts": {hu.host_family(model): {_RETRO_DAY: dict(counters)}},
        "token_sources": ["grok"],
        "active_days": [_RETRO_DAY],
        "counter_semantics": "disjoint-v1",
        "tokens_by_day": {_RETRO_DAY: bucket},
        **extra,
    }


def _retro_data(events: list[dict], ids=_IDS) -> agg.RetroData:
    data = agg.RetroData(window_days=7, since=_SINCE, until=_UNTIL)
    data.fleet = agg.FleetState(
        devices_known=len(ids),
        devices_in_events=set(ids),
        devices_known_list=[{"device_id": d} for d in sorted(ids)],
    )
    data.sessions, data.skills = agg.aggregate_sessions(events, since=_SINCE, until=_UNTIL)
    data.host_inventory = agg.aggregate_host_usage(
        events, since=_SINCE, until=_UNTIL, registered_ids=set(ids)
    )
    return data


def _grok_row(data: agg.RetroData):
    rows = agg.aggregate_agent_usage(data, machines_known=data.fleet.devices_known).rows
    return next(r for r in rows if r.key == "grok")


def test_grok_build_and_cursor_grok_runs_sum_into_one_priced_grok_row():
    counters = _retro_usage(input=10_000, cache_read=20_000, output=1_000)
    assert BUILD in tu.GROK_47_MODEL_IDS
    mixed = _retro_data(
        [
            _host_event("dev-a", BUILD, counters),
            _host_event("dev-b", "grok-4.7", counters, token_sources=["cursor"]),
        ]
    )
    row = _grok_row(mixed)
    assert row.tokens == 2 * sum(counters.values()) and row.machines == 2
    # Both model ids price from the grok-4.7 card: the same tokens under the Cursor id alone cost
    # exactly the same as the Grok Build ones.
    reference = _retro_data(
        [_host_event("dev-a", "grok-4.7", _retro_usage(20_000, 0, 40_000, 2_000))],
        ids={"dev-a"},
    )
    cost, floor = agg._agent_row_cost(row)
    ref_cost, ref_floor = agg._agent_row_cost(_grok_row(reference))
    assert cost == ref_cost and cost > 0
    assert floor is ref_floor, (row.floor_causes,)
    assert "Grok Build" not in "".join(row.floor_causes)


def test_degraded_grok_reader_is_named_in_coverage_notes():
    data = _retro_data(
        [
            _host_event("dev-a", BUILD, _retro_usage(input=1000)),
            _host_event(
                "dev-b",
                "grok-4.7",
                _retro_usage(input=1000),
                token_sources=["cursor"],
                degraded_sources=["grok"],
            ),
        ]
    )
    out = agg.format_retro(data)
    assert "Grok Build" in out
    assert agg._agent_row_cost(_grok_row(data))[1] is True


@pytest.mark.parametrize(
    ("sources", "accepted"),
    [
        (["codex", "grok", "cursor"], True),  # retained mm 1.11.7 rows
        (["codex", "cursor", "grok"], False),  # out of the fixed order
        (["codex", "grok", "cursor", "muse", "agy"], True),
    ],
)
def test_retained_token_source_order_is_accepted_only_in_the_restored_slot(sources, accepted):
    event = _host_event("dev-a", BUILD, _retro_usage(input=1000), token_sources=sources)
    inventory = agg.aggregate_host_usage(
        [event], since=_SINCE, until=_UNTIL, registered_ids={"dev-a"}
    )
    assert ("dev-a" in inventory.by_device) is accepted
