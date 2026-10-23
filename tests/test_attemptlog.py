"""Private attempt-record validation and write-free rendering states."""

import errno
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mind_meld import attemptlog, sidecar
from mind_meld.errors import StorageError
from tests.test_reader_publication import NOW


def record(**updates):
    return {
        "attempted_at": NOW.isoformat(),
        "class": "published",
        "cause": None,
        "row_ts": NOW.isoformat(),
        "readers": {"codex": "contributed", "agy": "empty"},
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
        record(readers={"agy": "dropped:invented"}),
        record(readers={"agy": {}}),
        record(cause=[]),
        record(**{"class": []}),
    ],
)
def test_malformed_record_is_corrupt(value):
    save(value)
    assert attemptlog.read() == (None, "corrupt")
    state = attemptlog.project(["codex", "agy"], None)
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
        attempted_at=NOW.isoformat(), readers={"agy": "dropped:deadline"}
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
    state = attemptlog.project(["agy"], (NOW + timedelta(hours=1, seconds=offset)).isoformat())
    assert state["latest_attempt_superseded"] == (kind != "published" and offset > 0)
    assert state["latest_attempt_readers"] == {"agy": "empty"}
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
    save(record(readers={"codex": "contributed", "agy": "contributed"}))
    state = attemptlog.project(["codex", "agy"], (NOW + timedelta(days=1)).isoformat())
    assert attemptlog.render(state, age="1 d ago", path="local") == [
        f"Last recorded attended attempt: {NOW.isoformat()} (1 d ago) — published"
    ]


@pytest.mark.parametrize(
    "cause", [None, "interrupted", "storage-error", "snapshot-refused", "error"]
)
def test_push_failure_causes_and_errno_roundtrip(cause):
    outcome = attemptlog.CaptureOutcome(attempted_at=NOW.isoformat())
    number = "ENOSPC" if cause == "storage-error" else None
    attemptlog.write(outcome.finish("push-failed", cause, errno_name=number))
    state = attemptlog.project([], None)
    assert state["latest_attempt"] == "push-failed"
    assert state["latest_attempt_cause"] == cause
    assert state["latest_attempt_errno"] == number
    if number:
        assert "(storage-error: ENOSPC)" in attemptlog.render(state, age="0 s ago", path="local")[0]


@pytest.mark.parametrize("number", [28, [], "ENOSPC /private/sensitive", "EINVENTED"])
def test_errno_rejects_numbers_messages_and_unknown_names(number):
    save(record(**{"class": "push-failed", "cause": "storage-error", "errno": number}))
    assert attemptlog.read() == (None, "corrupt")


@pytest.mark.parametrize(
    "kind,cause",
    [
        ("published", None),
        ("push-failed", None),
        ("push-failed", "interrupted"),
        ("push-failed", "snapshot-refused"),
        ("push-failed", "error"),
    ],
)
def test_errno_only_belongs_to_storage_failure(kind, cause):
    save(record(**{"class": kind, "cause": cause, "errno": "ENOSPC"}))
    assert attemptlog.read() == (None, "corrupt")


# attemptlog._valid tables as released in 1.5.2, pinned so later edits cannot
# change what the released reader accepts.
_RELEASED_1_5_CAUSES = {
    "published": {None},
    "not-published": {"exclude-patterns", "include-dirs", "file-absent", "row-missing"},
    "unverified": {
        "missing",
        "unreadable",
        "oversized-line",
        "changed",
        "revision-mismatch",
        "evidence-error",
    },
    "no-row": {None},
    "capture-failed": {None},
    "append-failed": {None},
    "max-file-size": {None},
    "prerequisites": {"disabled", "no-reader", "unavailable", "unknown"},
    "push-failed": {None},
}
_RELEASED_1_5_READERS = ("codex", "grok", "cursor")
_RELEASED_1_5_OUTCOMES = {"contributed", "empty", "partial", "absent"} | {
    f"dropped:{reason}"
    for reason in (
        "deadline",
        "io_error",
        "locked",
        "malformed",
        "partial",
        "stale",
        "unavailable",
        "unsupported",
    )
}


def _released_1_5_timestamp(raw: object) -> bool:
    if not isinstance(raw, str) or len(raw) > 64:
        return False
    try:
        return datetime.fromisoformat(raw).utcoffset() is not None
    except (ValueError, OverflowError):
        return False


def _released_1_5_valid(record: object) -> bool:
    """attemptlog._valid as released in 1.5.2, when push-failed took only None."""
    if not isinstance(record, dict):
        return False
    kind, readers = record.get("class"), record.get("readers")
    if "cause" not in record or not isinstance(kind, str) or kind not in _RELEASED_1_5_CAUSES:
        return False
    cause = record["cause"]
    if cause is not None and not isinstance(cause, str):
        return False
    if cause not in _RELEASED_1_5_CAUSES[kind]:
        return False
    if not _released_1_5_timestamp(record.get("attempted_at")):
        return False
    if record.get("row_ts") is not None and not _released_1_5_timestamp(record["row_ts"]):
        return False
    return isinstance(readers, dict) and all(
        name in _RELEASED_1_5_READERS
        and isinstance(outcome, str)
        and outcome in _RELEASED_1_5_OUTCOMES
        for name, outcome in readers.items()
    )


@pytest.mark.parametrize(
    "kind,cause,accepted",
    [
        ("published", None, True),
        ("push-failed", None, True),
        ("push-failed", "interrupted", False),
        ("push-failed", "storage-error", False),
        ("push-failed", "snapshot-refused", False),
        ("push-failed", "error", False),
    ],
)
def test_released_reader_tolerates_new_records_without_crashing(kind, cause, accepted):
    outcome = attemptlog.CaptureOutcome(attempted_at=NOW.isoformat())
    number = "ENOSPC" if cause == "storage-error" else None
    attemptlog.write(outcome.finish(kind, cause, errno_name=number))
    written = json.loads(attemptlog.record_path().read_text())
    # 1.5.2 ignores the added errno key and reports a new cause as corrupt.
    assert _released_1_5_valid(written) is accepted


def test_storage_errno_uses_explicit_os_cause_without_persisting_messages():
    try:
        try:
            raise OSError(errno.ENOSPC, "sensitive message", "/private/sensitive")
        except OSError as exc:
            raise StorageError("backend wrapper") from exc
    except StorageError as exc:
        assert attemptlog.storage_errno(exc) == "ENOSPC"
    assert attemptlog.storage_errno(OSError("ENOSPC is just message text")) is None


# Track 77A: sync executions, independent of host capture receipts.
def sync_record(**updates):
    return attemptlog.make_run(
        **{"outcome": "success", "cause": "ok", "completed_at": NOW.isoformat(), **updates}
    )


def evidence(runs=None, state="valid"):
    return {"state": state, "runs": runs or {}}


@pytest.mark.parametrize("verb", ["push", "pull"])
def test_auto_degraded_cause_matches_completed_result(verb):
    # Value: protects=truthful auto degradation cause and unchanged raw evidence;
    # fails_when=degraded execution is called command refusal;
    # why_new=existing tests assert unresolved but not this cause; seam=none;
    # no_claim=native hook or fleet acceptance.
    from mind_meld import cli

    raw = {
        verb: {
            "timestamp": NOW.isoformat(),
            "outcome": "degraded",
            "detail": "1 post-publication maintenance failure(s)",
        }
    }
    before = json.dumps(raw, sort_keys=True)
    projected = attemptlog.project_runs(evidence(raw), evidence(state="missing"), now=NOW)
    assert projected[verb]["selected"]["outcome"] == "degraded"
    assert projected[verb]["selected"]["cause"] == "result-degraded"
    assert projected[verb]["unresolved"]
    verdict = cli._status_verdict(
        source=None,
        blocking=None,
        conflicts=0,
        projection=projected,
        counts=(0, 0, 0),
        metadata=False,
        first_push=False,
    )
    assert f"Latest {verb} needs attention (result-degraded)" in verdict
    assert verdict.endswith("Run: mm diag")
    assert json.dumps(raw, sort_keys=True) == before


@pytest.mark.parametrize("mixed", [False, True])
def test_auto_discovery_only_remains_advisory(mixed):
    from mind_meld import events_tail

    detail = events_tail._ROOT_DISCOVERY_EMPTY_DEGRADATION
    if mixed:
        detail += "; another degradation"
    auto = evidence(
        {"push": {"timestamp": NOW.isoformat(), "outcome": "degraded", "detail": detail}}
    )
    projection = attemptlog.project_runs(auto, evidence(state="missing"), now=NOW)
    assert projection["push"]["unresolved"] is mixed


def test_equal_timestamp_write_obeys_generated_bound():
    old = sync_record()
    old["degradations"] = ["界" * 384] * 8
    path = attemptlog.runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "push": old}))
    attemptlog.write_run("push", sync_record(outcome="failed", cause="storage-error"))
    assert path.stat().st_size <= 16 * 1024
    stored = attemptlog.read_runs()
    assert stored["state"] == "valid"
    assert stored["runs"]["push"]["ordering_unknown"] is True
    assert stored["runs"]["push"]["omitted_details"] > 0


@pytest.mark.parametrize("verb", ["push", "pull"])
def test_sync_run_private_roundtrip_call_time_and_repair(tmp_path, monkeypatch, verb):
    destination = tmp_path / "new-sidecar"
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", destination)
    assert attemptlog.read_runs() == evidence(state="missing")
    assert not destination.exists()
    attemptlog.write_run(verb, sync_record(counts={"total_failed": 2}))
    path = attemptlog.runs_path()
    assert path.parent == destination
    assert path.stat().st_mode & 0o777 == 0o600
    assert attemptlog.read_runs()["runs"][verb]["counts"] == {"total_failed": 2}
    path.write_text("{")
    attemptlog.write_run(verb, sync_record())
    assert attemptlog.read_runs()["state"] == "valid"
    assert not path.with_suffix(".json.lock").exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"counts": {"held": True}},
        {"counts": {"held": -1}},
        {"counts": {"held": float("inf")}},
        {"counts": {"held": 2**54}},
        {"counts": {"paths": 1}},
        {"outcome": "interrupted"},
        {"cause": "invented"},
        {"completed_at": "2026-01-01"},
        {"content_accepted": 1},
        {"scope": {"bad": "x"}},
    ],
)
def test_sync_run_rejects_invalid_closed_schema(updates):
    with pytest.raises(ValueError):
        sync_record(**updates)


@pytest.mark.parametrize(
    "payload,state",
    [
        (b"", "corrupt"),
        (b"{", "corrupt"),
        (b"[]", "corrupt"),
        (b"{}", "corrupt"),
        (b"[" * 2000 + b"]" * 2000, "corrupt"),
        (b"x" * 16385, "oversized"),
    ],
)
def test_sync_run_bad_reads_never_repair(payload, state):
    path = attemptlog.runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    before = path.stat()
    assert attemptlog.read_runs() == evidence(state=state)
    assert path.read_bytes() == payload
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_sync_run_read_denied_and_contended_are_unknown(monkeypatch):
    import fcntl
    import os

    from mind_meld import lockedjson

    attemptlog.write_run("push", sync_record())
    fd = os.open(attemptlog.runs_path(), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert attemptlog.read_runs() == evidence(state="contended")
        with pytest.raises(lockedjson.LockContended):
            attemptlog.write_run("pull", sync_record())
    finally:
        os.close(fd)
    real = lockedjson.os.open

    def denied(path, *args, **kwargs):
        if path == str(attemptlog.runs_path()):
            raise PermissionError("secret /private/path")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(lockedjson.os, "open", denied)
    assert attemptlog.read_runs() == evidence(state="unreadable")


@pytest.mark.parametrize("order", [("push", "pull"), ("pull", "push")])
def test_controlled_writers_retain_both_verbs(order):
    import threading

    # Separate workers, explicitly ordered critical sections without sleeps.
    first_done = threading.Event()
    errors = []

    def writer(verb, wait):
        try:
            if wait:
                assert first_done.wait(5)
            attemptlog.write_run(verb, sync_record())
        except BaseException as exc:
            errors.append(exc)
        finally:
            if not wait:
                first_done.set()

    workers = [
        threading.Thread(target=writer, args=(verb, bool(i))) for i, verb in enumerate(order)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()
    assert not errors
    assert set(attemptlog.read_runs()["runs"]) == {"push", "pull"}


@pytest.mark.parametrize("verb", ["push", "pull"])
@pytest.mark.parametrize("reverse", [False, True])
def test_completion_order_wins_over_writer_order(verb, reverse):
    old = sync_record(outcome="failed", cause="crypto-error")
    new = sync_record(completed_at=(NOW + timedelta(seconds=1)).isoformat())
    for value in [new, old] if reverse else [old, new]:
        attemptlog.write_run(verb, value)
    stored = attemptlog.read_runs()["runs"][verb]
    assert stored == {**new, "ordering_unknown": reverse}
    projected = attemptlog.project_runs(
        evidence(state="missing"), attemptlog.read_runs(), now=NOW + timedelta(seconds=5)
    )[verb]
    assert projected["unknown_reason"] == ("ambiguous" if reverse else None)


@pytest.mark.parametrize("verb", ["push", "pull"])
def test_clock_rollback_keeps_failure_order_unknown_until_later_completion(verb):
    first = sync_record()
    attemptlog.write_run(verb, first)
    attemptlog.write_run(
        verb,
        sync_record(
            outcome="failed",
            cause="storage-error",
            completed_at=(NOW - timedelta(seconds=2)).isoformat(),
        ),
    )
    later_now = NOW + timedelta(seconds=5)
    state = attemptlog.project_runs(
        evidence(state="missing"), attemptlog.read_runs(), now=later_now
    )[verb]
    assert state["selected"]["completed_at"] == first["completed_at"]
    assert state["unknown_reason"] == "ambiguous"
    attemptlog.write_run(verb, sync_record(completed_at=(NOW + timedelta(seconds=1)).isoformat()))
    state = attemptlog.project_runs(
        evidence(state="missing"), attemptlog.read_runs(), now=later_now
    )[verb]
    assert state["unknown_reason"] is None
    assert state["unresolved"] is False


def test_equal_completion_writers_preserve_uncertainty():
    attemptlog.write_run("push", sync_record())
    attemptlog.write_run("push", sync_record(outcome="failed", cause="storage-error"))
    state = attemptlog.project_runs(evidence(state="missing"), attemptlog.read_runs(), now=NOW)
    assert state["push"]["unknown_reason"] == "ambiguous"


@pytest.mark.parametrize("verb", ["push", "pull"])
@pytest.mark.parametrize("offset,kind", [(-1, "auto"), (0, "attended"), (1, "attended")])
def test_projection_ordering_and_other_verb(verb, offset, kind):
    auto = evidence({verb: {"timestamp": NOW.isoformat(), "outcome": "crypto-error"}})
    record = sync_record(completed_at=(NOW + timedelta(seconds=offset)).isoformat())
    projected = attemptlog.project_runs(auto, evidence({verb: record}), now=NOW + timedelta(days=1))
    assert projected[verb]["kind"] == kind
    assert projected[verb]["unresolved"] == (offset <= 0)
    assert projected[verb]["unknown_reason"] == ("ambiguous" if offset == 0 else None)
    other = "pull" if verb == "push" else "push"
    assert projected[other]["selected"] is None


@pytest.mark.parametrize("timestamp", ["bad", "2026-01-01", (NOW + timedelta(days=2)).isoformat()])
def test_projection_uncertain_auto_never_clears_failure(timestamp):
    attended = evidence({"push": sync_record(outcome="failed", cause="storage-error")})
    state = attemptlog.project_runs(
        evidence({"push": {"timestamp": timestamp, "outcome": "success"}}), attended, now=NOW
    )
    assert state["push"]["unknown_reason"] == "ambiguous"
    assert state["push"]["unresolved"]


@pytest.mark.parametrize("latest", ["attended", "auto"])
def test_later_success_resolves_failure_symmetrically(latest):
    old = NOW - timedelta(seconds=1)
    attended = sync_record(
        outcome="success" if latest == "attended" else "failed",
        cause="ok" if latest == "attended" else "storage-error",
        completed_at=(NOW if latest == "attended" else old).isoformat(),
    )
    auto = {
        "timestamp": (NOW if latest == "auto" else old).isoformat(),
        "outcome": "success" if latest == "auto" else "crypto-error",
    }
    state = attemptlog.project_runs(evidence({"push": auto}), evidence({"push": attended}), now=NOW)
    assert not state["push"]["unresolved"]
    assert state["push"]["kind"] == latest


@pytest.mark.parametrize("scope", [{"source": "claude"}, {"from_device": "mac-a"}])
def test_scoped_success_does_not_establish_unscoped_recovery(scope):
    auto = evidence(
        {"pull": {"timestamp": (NOW - timedelta(seconds=1)).isoformat(), "outcome": "failed"}}
    )
    state = attemptlog.project_runs(auto, evidence({"pull": sync_record(scope=scope)}), now=NOW)[
        "pull"
    ]
    assert state["limited_coverage"] and state["unresolved"]
    assert state["unknown_reason"] == "limited-coverage"


@pytest.mark.parametrize("failure", [False, True])
def test_skip_never_proves_recovery_but_does_not_invent_failure(failure):
    auto = evidence(
        {
            "pull": {
                "timestamp": (NOW - timedelta(seconds=1)).isoformat(),
                "outcome": "failed" if failure else "success",
            }
        }
    )
    attended = evidence({"pull": sync_record(outcome="skipped", cause="lock-held")})
    state = attemptlog.project_runs(auto, attended, now=NOW)["pull"]
    assert state["unresolved"] == failure
    assert state["kind"] == "attended"


def test_run_detail_inventory_is_bounded_and_private():
    from mind_meld import events_tail

    details = [
        "event append skipped (max-file-size)",
        events_tail._CURSOR_HOLD_DEGRADATION,
        events_tail._ROOT_DISCOVERY_DEGRADATION,
        events_tail._git_walk_degradation(1, 2),
        "events walk exceeded its 500ms budget",
        "token cache was locked, so tokens and skills are missing",
        "token walk skipped, so tokens and skills are missing",
        "events tail failed (OSError)",
    ]
    result = sync_record(degradations=details * 20)
    assert len(result["degradations"]) == 8
    assert result["omitted_details"] == 152
    custom = (
        "mm-events folder /Users/private/secret is missing; plug it in, create it, or run "
        "mm disable-source mm-events. Skipping mm-events for this sync."
    )
    result = sync_record(
        degradations=[custom, "\x1b[2Jforged\nline /secret", "unknown future phrase"]
    )
    assert "mm-events folder is missing" in result["degradations"][0]
    assert result["omitted_details"] == 2
    assert "/Users" not in json.dumps(result) and "secret" not in json.dumps(result)
    attemptlog.write_run("push", result)
    assert attemptlog.runs_path().stat().st_size <= 16384


def test_run_write_error_is_not_claimed_success(monkeypatch):
    from mind_meld import lockedjson

    monkeypatch.setattr(lockedjson, "_write_json", lambda *a, **k: OSError(errno.ENOSPC, "private"))
    with pytest.raises(OSError) as failure:
        attemptlog.write_run("push", sync_record())
    assert failure.value.errno == errno.ENOSPC


def test_sync_writer_preserves_pytest_guard_baseexception(monkeypatch):
    from mind_meld import fsutil

    monkeypatch.setattr(sidecar, "SIDECAR_DIR", Path("/synthetic/denied"))

    def refused(path):
        raise fsutil.PytestRealStateWriteError("test refusal")

    monkeypatch.setattr(fsutil, "refuse_real_state_write_under_pytest", refused)
    with pytest.raises(fsutil.PytestRealStateWriteError):
        attemptlog.write_run("push", sync_record())


def test_sync_torn_write_becomes_unknown_and_later_writer_can_repair(monkeypatch):
    import os

    from mind_meld import lockedjson

    attemptlog.write_run("push", sync_record())

    def torn(fd, data, **kwargs):
        os.ftruncate(fd, 0)
        os.write(fd, b"{")
        return OSError(errno.ENOSPC, "private")

    with monkeypatch.context() as patch:
        patch.setattr(lockedjson, "_write_json", torn)
        with pytest.raises(OSError):
            attemptlog.write_run("pull", sync_record())
    assert attemptlog.read_runs()["state"] == "corrupt"
    attemptlog.write_run("pull", sync_record())
    assert set(attemptlog.read_runs()["runs"]) == {"pull"}


def test_host_skip_details_keep_safe_reason_and_remedy_without_path():
    from mind_meld import events_tail

    for reason in ("deadline", "unsupported", "io_error"):
        record = sync_record(
            degradations=[
                events_tail._host_skip_phrase("codex", reason, readiness="ready", attended=True)
            ]
        )
        assert record["degradations"], record
        assert reason in record["degradations"][0]
        assert "/" not in json.dumps(record)


@pytest.mark.parametrize("auto_outcome", [None, [], {}, 1, "new-unknown-outcome"])
def test_invalid_auto_classification_is_uncertain_not_a_crash(auto_outcome):
    state = attemptlog.project_runs(
        evidence({"push": {"timestamp": NOW.isoformat(), "outcome": auto_outcome}}),
        evidence(state="missing"),
        now=NOW,
    )
    assert state["push"]["unknown_reason"] == "corrupt"
    assert state["push"]["unresolved"]


def test_equivalent_offset_and_z_instants_remain_equal_and_uncertain():
    from datetime import timezone

    auto = evidence({"push": {"timestamp": "2026-09-01T02:00:00+02:00", "outcome": "crypto-error"}})
    attended = evidence({"push": sync_record(completed_at="2026-09-01T00:00:00Z")})
    state = attemptlog.project_runs(auto, attended, now=datetime(2026, 9, 2, tzinfo=timezone.utc))
    assert state["push"]["unknown_reason"] == "ambiguous"
    assert state["push"]["unresolved"]


def test_generated_bound_clamps_existing_expensive_detail_with_honest_omissions():
    record = sync_record()
    record["degradations"] = ["字" * 384] * 8
    path = attemptlog.runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "push": record}))
    assert path.stat().st_size > 16384
    attemptlog.write_run("pull", sync_record())
    assert path.stat().st_size <= 16384
    runs = attemptlog.read_runs()["runs"]
    assert len(runs["push"]["degradations"]) + runs["push"]["omitted_details"] == 8
    assert set(runs) == {"push", "pull"}
