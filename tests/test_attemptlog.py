"""Private attempt-record validation and write-free rendering states."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from mind_meld import attemptlog, sidecar
from tests.test_reader_publication import NOW


def record(**updates):
    return {
        "attempted_at": NOW.isoformat(),
        "class": "published",
        "cause": None,
        "row_ts": NOW.isoformat(),
        "readers": {"codex": "contributed", "grok": "empty"},
        **updates,
    }


def save(value):
    sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    attemptlog.record_path().write_text(json.dumps(value))


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"class": "published"},
        record(attempted_at="2026-09-21"),
        record(row_ts="0001-01-01T00:00:00+01:00"),
        record(cause="wrong"),
        record(**{"class": "unverified", "cause": "invented"}),
        record(readers={"bad\x1b[2J": "contributed"}),
        record(readers={"grok": "dropped:invented"}),
        record(readers={"grok": {}}),
        record(cause=[]),
        record(**{"class": []}),
    ],
)
def test_malformed_record_is_corrupt(value):
    save(value)
    assert attemptlog.read() == (None, "corrupt")
    state = attemptlog.project(["codex", "grok"], None)
    assert state["latest_attempt_reason"] == "corrupt"
    assert attemptlog.render(state, age="unknown", path="local")[0] == (
        "Last recorded attended attempt: unknown "
        "(record corrupt — the next attended mm push rewrites it)"
    )


@pytest.mark.parametrize(
    "payload",
    [b"", b"not json", b"x" * (attemptlog._MAX_RECORD_BYTES + 1)],
    ids=["empty", "json", "large"],
)
def test_empty_unparseable_and_overlong_record(payload):
    save({})
    attemptlog.record_path().write_bytes(payload)
    assert attemptlog.read() == (None, "corrupt")


def test_missing_and_unreadable_records_are_distinct_without_writes(monkeypatch):
    missing = attemptlog.project(["codex"], None)
    assert missing["latest_attempt_reason"] == "missing"
    assert not attemptlog.record_path().exists()
    save(record())
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == attemptlog.record_path():
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    unreadable = attemptlog.project(["codex"], None)
    assert unreadable["latest_attempt_reason"] == "unreadable"
    assert (
        "record unreadable: ~/local"
        in attemptlog.render(unreadable, age="unknown", path="~/local")[0]
    )


@pytest.mark.parametrize(
    "kind,cause", [("published", None), ("prerequisites", "no-reader"), ("unverified", "changed")]
)
def test_closed_outcomes_roundtrip_private_file(kind, cause):
    outcome = attemptlog.CaptureOutcome(
        attempted_at=NOW.isoformat(), readers={"grok": "dropped:deadline"}
    )
    attemptlog.write(outcome.finish(kind, cause))
    written, reason = attemptlog.read()
    assert reason is None
    assert written["class"] == kind and written["cause"] == cause
    assert attemptlog.record_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["published", "push-failed"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_superseded_requires_newer_than_attempt_and_row_and_nonpublished(kind, offset):
    save(record(**{"class": kind}, row_ts=(NOW + timedelta(hours=1)).isoformat()))
    state = attemptlog.project(["grok"], (NOW + timedelta(hours=1, seconds=offset)).isoformat())
    assert state["latest_attempt_superseded"] == (kind != "published" and offset > 0)
    assert state["latest_attempt_readers"] == {"grok": "empty"}
    rendered = attemptlog.render(state, age="in the future", path="local")
    assert "(in the future)" in rendered[0]
    assert ("a later capture has since been recorded" in rendered[0]) == (
        kind != "published" and offset > 0
    )


def test_record_without_cause_key_is_corrupt_not_fatal():
    value = record()
    del value["cause"]
    save(value)
    assert attemptlog.read() == (None, "corrupt")
    assert attemptlog.project(["codex"], None)["latest_attempt"] == "unknown"


def test_stopped_preserves_computed_verdict_and_rowless_acceptance():
    published = attemptlog.CaptureOutcome(attempted_at=NOW.isoformat())
    published.finish("published")
    published.appended = (Path("day.jsonl"), {"ts": NOW.isoformat()})
    attemptlog.Attempt(outcome=published, content_accepted=False).stopped()
    assert (published.kind, published.cause) == ("published", None)

    norow = attemptlog.CaptureOutcome(attempted_at=NOW.isoformat())
    norow.finish("no-row")
    attemptlog.Attempt(outcome=norow, content_accepted=True).stopped()
    assert (norow.kind, norow.cause) == ("no-row", None)


def test_missing_row_timestamp_supersedes_only_a_later_nonpublished_attempt():
    save(record(**{"class": "no-row"}, row_ts=None, readers={"codex": "dropped:unsupported"}))
    later = attemptlog.project(["codex"], (NOW + timedelta(seconds=1)).isoformat())
    assert later["latest_attempt_superseded"] is True
    assert (
        "a later capture has since been recorded"
        in attemptlog.render(later, age="1 s ago", path="local")[0]
    )
    assert attemptlog.project(["codex"], NOW.isoformat())["latest_attempt_superseded"] is False


def test_missing_attempt_offers_push_only_when_refresh_is_ready():
    state = attemptlog.project(["codex"], None)
    ready = attemptlog.render(state, age="unknown", path="local", refresh_ready=True)[0]
    blocked = attemptlog.render(state, age="unknown", path="local", refresh_ready=False)[0]
    assert ready.endswith("no attended attempt recorded yet — run mm push)")
    assert blocked.endswith("no attended attempt recorded yet)")
    assert "run mm push" not in blocked


def test_healthy_attended_attempt_does_not_claim_superseded_by_autopush():
    save(record(readers={"codex": "contributed", "grok": "contributed"}))
    state = attemptlog.project(["codex", "grok"], (NOW + timedelta(days=1)).isoformat())
    assert attemptlog.render(state, age="1 d ago", path="local") == [
        f"Last recorded attended attempt: {NOW.isoformat()} (1 d ago) — published"
    ]
