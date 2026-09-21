"""Track 65A: reader evidence, retained windows and publication proofs."""

import builtins
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mind_meld import cli, events, events_tail, host_usage, token_usage
from mind_meld.skills.retro_fleet import aggregator

NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
TODAY = NOW.date().isoformat()
OLD = "2026-09-01"


def host_row(*, hosts=None, **kwargs):
    return events.make_host_usage_snapshot(
        device="local",
        hosts={"other": {TODAY: token_usage.zero_model_bucket()}} if hosts is None else hosts,
        token_sources=["codex", "grok"],
        ts=NOW,
        **kwargs,
    )


def publication(row, readers=("codex", "grok")):
    _key, projected = aggregator.local_host_capture_candidate(row, until=NOW)
    scan = events.EventScan(rows={"host-usage-snapshot": projected})
    return events.project_host_publication(scan, readers, None, now=NOW)


@pytest.mark.parametrize(
    "hosts,usage_days,expected",
    [
        (None, {"codex": [TODAY], "grok": []}, ["grok"]),
        ({}, {"codex": [], "grok": []}, ["codex", "grok"]),
        (None, {"codex": [TODAY], "grok": [TODAY]}, []),
        (None, None, None),
        ({}, None, ["codex", "grok"]),
    ],
)
def test_reader_empty_presence_and_legacy_inference(hosts, usage_days, expected):
    row = host_row(hosts=hosts, usage_days=usage_days)
    assert ("empty_sources" in row) == (usage_days is not None)
    pub = publication(row)
    assert pub["empty_readers"] == expected
    assert pub["readers"] == {"codex": "contributed", "grok": "contributed"}
    assert pub["empty"] == (hosts == {})
    assert not pub["coverage_invalid"]


@pytest.mark.parametrize(
    "hosts,empty,partial",
    [
        (None, ["bad\x1b[2J"], []),
        (None, ["future_reader"], []),
        (None, ["codex", "grok"], []),
        ({}, [], []),
        ({}, ["grok"], []),
        (None, ["codex"], ["codex"]),
        (None, None, []),
    ],
)
def test_invalid_empty_claim_drops_field_keeps_row_and_legacy_fallback(hosts, empty, partial):
    row = {**host_row(hosts=hosts), "empty_sources": empty, "partial_sources": partial}
    accepted = aggregator._accept_host_usage_snapshot(row)
    assert not isinstance(accepted, aggregator.HostReject)
    assert accepted.empty_sources is None
    assert accepted.empty_reason == "invalid_coverage"
    pub = publication(row)
    assert pub["coverage_invalid"]
    assert pub["empty_readers"] == (["codex", "grok"] if hosts == {} else None)
    assert "bad" not in json.dumps(pub)


def test_empty_names_intersect_consent_and_live_reader_allowlist():
    row = {**host_row(hosts={}), "token_sources": ["codex", "grok", "future_reader"]}
    row["empty_sources"] = row["token_sources"]
    assert publication(row, ("grok", "future_reader"))["empty_readers"] == ["grok"]


def test_reader_days_trim_with_partial_days_and_count_zero_buckets():
    row = host_row(
        hosts={
            "codex": {OLD: token_usage.zero_model_bucket()},
            "other": {TODAY: token_usage.zero_model_bucket()},
        },
        usage_days={"codex": [OLD], "grok": [TODAY]},
        partial_days={"codex": [OLD]},
        max_days=1,
    )
    assert row["empty_sources"] == ["codex"]
    assert "partial_sources" not in row
    assert events.host_reader_outcomes(row, ["codex", "grok"]) == {
        "codex": "empty",
        "grok": "contributed",
    }


def test_production_capture_preserves_day_sets_through_warm_retry(monkeypatch):
    monkeypatch.setattr(
        host_usage,
        "warm_host_cache_inline",
        lambda **kw: host_usage.HostUsageResult({}, complete=True),
    )
    readers = (
        (
            "codex",
            lambda **kw: host_usage.HostUsageResult(
                {"other": {TODAY: token_usage.zero_model_bucket()}}, complete=True
            ),
        ),
        ("grok", lambda **kw: host_usage.HostUsageResult({}, complete=False, reason="deadline")),
    )
    capture, rows = events_tail._capture_host_snapshot(
        "local",
        readers,
        host_budget_ms=500,
        warm_host_cache=lambda name: None,
    )
    assert capture.usage_days == {"codex": frozenset({TODAY}), "grok": frozenset()}
    assert rows[0]["empty_sources"] == ["grok"]


def test_empty_presence_list_and_invalid_reason_break_ties_deterministically():
    rows = [
        host_row(),
        host_row(usage_days={"codex": [TODAY], "grok": [TODAY]}),
        host_row(usage_days={"codex": [TODAY], "grok": []}),
        {**host_row(), "empty_sources": ["grok", "codex"]},
    ]
    candidates = [aggregator.local_host_capture_candidate(row, until=NOW) for row in rows]
    assert len({key for key, _ in candidates}) == len(rows)
    assert max(candidates, key=lambda item: item[0]) == max(
        reversed(candidates), key=lambda item: item[0]
    )


def test_receipt_single_open_exhausts_and_normalizes_row(tmp_path, monkeypatch):
    row = {**host_row(), "token_sources": ("codex", "grok")}
    path = tmp_path / "local.jsonl"
    payload = (json.dumps(row) + '\n{"later":true}\n').encode()
    path.write_bytes(payload)
    real_open = builtins.open
    opens = []

    def counted(path, *args, **kwargs):
        opens.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counted)
    revision = events.recorded_row_revision(path, row)
    assert opens == [path]
    assert revision == events.RowRevision(True, hashlib.sha256(payload).hexdigest(), None)
    scan_revision = {}
    list(events._iter_typed_objs(path, {row["type"]}, events.EventScan(), scan_revision))
    assert revision.digest == scan_revision["sha256"]


@pytest.mark.parametrize(
    "failure", ["missing", "unreadable", "oversized-line", "changed", "deleted"]
)
def test_single_pass_revision_reasons(tmp_path, monkeypatch, failure):
    row = host_row()
    path = tmp_path / f"local-{TODAY}.jsonl"
    path.write_text(json.dumps(row) + "\n")
    if failure == "missing":
        path.unlink()
    elif failure == "unreadable":
        real_open = builtins.open

        def denied(target, *args, **kwargs):
            if Path(target) == path:
                raise PermissionError("denied")
            return real_open(target, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", denied)
    elif failure == "oversized-line":
        monkeypatch.setattr(token_usage, "MAX_JSONL_LINE_BYTES", 1024)
        with path.open("ab") as stream:
            stream.write(b"x" * 2048 + b"\n")
    else:
        real_stat = Path.stat

        def changed(target, *args, **kwargs):
            if target == path:
                if failure == "deleted":
                    path.unlink()
                else:
                    path.write_text("changed after read\n")
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", changed)
    revision = events.recorded_row_revision(path, row)
    assert revision.digest is None
    assert revision.reason == ("changed" if failure == "deleted" else failure)


@pytest.mark.parametrize(
    "proof", ["exclude-patterns", "include-dirs", "file-absent", "row-missing"]
)
def test_nonpublication_requires_proof_and_config_precedes_file_read(tmp_path, monkeypatch, proof):
    row = host_row()
    path = tmp_path / "local.jsonl"
    path.write_bytes(b'{"torn":\n')
    src = {"include_dirs": ["events"]}
    rel = f"events/{path.name}"
    manifest = {
        "sources": {
            "mm-events": {"files": {rel: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}
        }
    }
    if proof == "exclude-patterns":
        src["exclude_patterns"] = ["events/*"]
    elif proof == "include-dirs":
        src["include_dirs"] = []
    elif proof == "file-absent":
        manifest["sources"]["mm-events"]["files"] = {}
    if proof != "row-missing":
        monkeypatch.setattr(
            events, "recorded_row_revision", lambda *args: pytest.fail("read after proof")
        )
    assert cli._usage_publication_verdict(src, path, row, manifest) == ("not-published", proof)


def test_accepted_oversized_bytes_are_unverified_in_receipt_and_status(tmp_path, monkeypatch):
    row = host_row()
    path = tmp_path / f"local-{TODAY}.jsonl"
    path.write_bytes((json.dumps(row) + "\n").encode() + b"x" * 2048 + b"\n")
    manifest = {
        "sources": {
            "mm-events": {
                "files": {
                    f"events/{path.name}": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                }
            }
        }
    }
    monkeypatch.setattr(token_usage, "MAX_JSONL_LINE_BYTES", 1024)
    assert cli._usage_publication_verdict({"include_dirs": ["events"]}, path, row, manifest) == (
        "unverified",
        "oversized-line",
    )
    scan = events.latest_event_rows(
        tmp_path,
        "local",
        {"host-usage-snapshot"},
        now=NOW,
        selectors={
            "host-usage-snapshot": lambda row: aggregator.local_host_capture_candidate(
                row, until=NOW
            )
        },
    )
    assert len(scan.files["host-usage-snapshot"]) == 2
    pub = events.project_host_publication(scan, ["codex", "grok"], manifest, now=NOW)
    assert pub["publication"] == "unverified"
    assert pub["publication_reason"] == "oversized-line"


@pytest.mark.parametrize("failure", ["read", "exists"])
@pytest.mark.parametrize(
    "winner_ts,failed_day,uncertain",
    [
        ("2026-09-20T23:59:57+00:00", "2026-09-20", True),  # midnight straddle
        ("2026-09-21T23:00:00+00:00", "2026-09-20", True),  # rollback within skew
        ("2026-09-20T12:00:00+00:00", "2026-09-20", True),  # equal-ts tie
        ("2026-09-21T12:00:00+00:00", "2026-09-19", False),
        ("0001-01-01T00:00:00+00:00", "2026-09-19", True),  # saturating underflow
    ],
)
def test_host_uncertainty_uses_projected_time_and_saturated_day_margin(
    tmp_path, monkeypatch, failure, winner_ts, failed_day, uncertain
):
    row = {**host_row(), "ts": winner_ts}
    (tmp_path / f"local-{TODAY}.jsonl").write_text(json.dumps(row) + "\n")
    failed = tmp_path / f"local-{failed_day}.jsonl"
    failed.write_text(json.dumps(row) + "\n")
    original = builtins.open if failure == "read" else Path.exists

    def denied(path, *args, **kwargs):
        if Path(path) == failed:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(
        builtins if failure == "read" else Path, "open" if failure == "read" else "exists", denied
    )

    def selector(row):
        _key, projected = aggregator.local_host_capture_candidate(row, until=NOW)
        return ("opaque-order",), projected

    scan = events.latest_event_rows(
        tmp_path,
        "local",
        {"host-usage-snapshot"},
        now=NOW,
        selectors={"host-usage-snapshot": selector},
        day_margins={"host-usage-snapshot": aggregator._HOST_FUTURE_SKEW},
    )
    assert ("host-usage-snapshot" in scan.uncertain_types) is uncertain
    assert len(scan.files["host-usage-snapshot"]) == 2


@pytest.mark.parametrize(
    "row_ts,expected",
    [
        ("2026-09-21T12:00:00+00:00", "2026-09-21"),  # > margin rollback
        ("2026-09-18T23:59:00-03:00", "2026-09-19"),  # use UTC row day
        ("2026-09-17T12:00:00+00:00", "2026-09-18"),  # append day wins
    ],
)
def test_writer_enforces_batch_row_to_file_day_contract(tmp_path, monkeypatch, row_ts, expected):
    class RolledBack(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW - timedelta(days=3)

    monkeypatch.setattr(events, "datetime", RolledBack)
    row = {**host_row(), "ts": row_ts}
    earlier = {**row, "ts": "2026-09-01T00:00:00+00:00"}
    path = events.write_push_event(tmp_path, "local", [row, earlier], strict=True)
    assert path.name == f"local-{expected}.jsonl"
    assert [json.loads(line) for line in path.read_text().splitlines()] == [row, earlier]


def test_cursor_finds_terminal_row_in_the_next_day_file(tmp_path, monkeypatch):
    rolled = datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return rolled

    monkeypatch.setattr(events, "datetime", Clock)
    host = events.make_host_usage_snapshot(
        device="local",
        hosts={},
        token_sources=["codex"],
        ts=rolled + timedelta(minutes=2),
    )
    push = events.make_mm_push_event(
        device="local", mm_version="0.14.18", sources=["mm-events"], discovery_errors=[]
    )
    path = events.write_push_event(tmp_path, "local", [host, push], strict=True)
    assert path.name == "local-2026-09-21.jsonl"
    cursor = events.resolve_push_cursor(tmp_path, "local", now=rolled + timedelta(seconds=1))
    assert cursor.used_floor is False
    assert cursor.since == datetime.fromisoformat(push["ts"])


def test_host_scan_reads_the_next_day_a_rolled_back_clock_would_write(tmp_path, monkeypatch):
    rolled = NOW - timedelta(hours=13)  # 2026-09-20 23:00 UTC
    row = {**host_row(), "ts": (rolled + timedelta(hours=2)).isoformat()}

    class RolledBack(datetime):
        @classmethod
        def now(cls, tz=None):
            return rolled

    monkeypatch.setattr(events, "datetime", RolledBack)
    path = events.write_push_event(tmp_path, "local", [row], strict=True)
    assert path.name == "local-2026-09-21.jsonl"
    scan = events.latest_event_rows(
        tmp_path,
        "local",
        {"host-usage-snapshot"},
        now=rolled,
        selectors={
            "host-usage-snapshot": lambda item: aggregator.local_host_capture_candidate(
                item, until=rolled
            )
        },
        day_margins={"host-usage-snapshot": aggregator._HOST_FUTURE_SKEW},
    )
    assert scan.rows["host-usage-snapshot"]["ts"] == row["ts"]


def test_diagnostic_hash_cache_uses_inode_and_never_resolves_paths(tmp_path, monkeypatch):
    import os

    path = tmp_path / f"local-{TODAY}.jsonl"
    alias = tmp_path / "alias.jsonl"
    path.write_text(json.dumps(host_row()) + "\n")
    os.link(path, alias)
    monkeypatch.setattr(Path, "resolve", lambda *a, **kw: pytest.fail("resolved cache path"))
    scan = events.latest_event_rows(tmp_path, "local", {"host-usage-snapshot"}, now=NOW)
    stat = path.stat()
    assert set(scan.hashes) == {(stat.st_dev, stat.st_ino)}
    assert scan.cached_hash(alias, alias.stat()) == hashlib.sha256(path.read_bytes()).hexdigest()
    other = tmp_path / "not-events.md"
    other.write_text("other")
    assert scan.cached_hash(other, other.stat()) is None
    path.write_text("changed")
    assert scan.cached_hash(alias, alias.stat()) is None


@pytest.mark.parametrize("example", ["healthy", "mixed-empty", "failed-refresh", "unverified"])
def test_readme_host_usage_recipe_matches_fixed_width_console(tmp_path, monkeypatch, example):
    import io
    import re
    import textwrap

    from rich.console import Console

    from mind_meld import attemptlog

    row = host_row(
        usage_days={"codex": [TODAY], "grok": [] if example == "mixed-empty" else [TODAY]}
    )
    outcomes = {
        "codex": "contributed",
        "grok": "empty" if example == "mixed-empty" else "contributed",
    }
    attempt = attemptlog.CaptureOutcome(attempted_at=NOW.isoformat(), readers=outcomes)
    attempt.appended = (tmp_path / "day.jsonl", row)
    attempt.finish("published")
    if example == "failed-refresh":
        row["ts"] = (NOW - timedelta(hours=1)).isoformat()
        attempt.appended = None
        attempt.readers = {"codex": "dropped:unsupported", "grok": "dropped:deadline"}
        attempt.finish("no-row")
    if example == "unverified":
        attempt.finish("unverified", "oversized-line")
    attemptlog.write(attempt)
    state = publication(row)
    state.update(publication="published", readiness="ready")
    if example == "unverified":
        state.update(
            state="unknown",
            error="~/.local/share/mind-meld/events/local-2026-09-21.jsonl",
            publication="unverified",
            publication_reason="oversized-line",
        )
    state.update(attemptlog.project(["codex", "grok"], state["ts"]))
    monkeypatch.setattr(events_tail, "host_read_age", lambda timestamp: "0 s ago")
    output = io.StringIO()
    # Height must be set too: on a dumb terminal Rich ignores width alone and
    # wraps at 80 columns, which breaks these exact-output pins.
    monkeypatch.setattr(
        cli, "console", Console(file=output, width=160, height=40, color_system=None)
    )
    cli._print_host_publication(state, reader_states={})
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    match = re.search(rf"<!-- usage-example:{example} -->\n```text\n(.*?)\n```", readme, re.S)
    assert match is not None, f"missing README example {example}"
    assert textwrap.dedent(output.getvalue()).rstrip() == match[1]


@pytest.mark.parametrize(
    "reason,remedy",
    [
        (
            "changed",
            "the day file changed while mm verified it; check publication read-only with mm status",
        ),
        (
            "missing",
            "the day file was moved or deleted before verification; check mm status, "
            "and the next attended mm push writes a new row",
        ),
        (
            "unreadable",
            "mm could not read ~/events/local.jsonl; restore read access, then check mm status",
        ),
        (
            "oversized-line",
            "~/events/local.jsonl holds a line over 16 MiB that mm did not write, "
            "so publication cannot be verified from this file; content sync is unaffected and "
            "mm status shows publication unverified for it",
        ),
        (
            "revision-mismatch",
            "the day file changed after the push accepted it; "
            "check publication read-only with mm status",
        ),
    ],
)
def test_unverified_receipt_literal_remedies_and_attempt_agree(monkeypatch, reason, remedy):
    import io

    from rich.console import Console

    from mind_meld import attemptlog

    row = host_row()
    path = Path.home() / "events/local.jsonl"
    manifest = {"sources": {"mm-events": {"files": {"events/local.jsonl": {"sha256": "accepted"}}}}}
    monkeypatch.setattr(
        events,
        "recorded_row_revision",
        lambda *a: events.RowRevision(
            True,
            "changed-bytes" if reason == "revision-mismatch" else None,
            None if reason == "revision-mismatch" else reason,
        ),
    )
    output = io.StringIO()
    monkeypatch.setattr(
        cli,
        "stderr_console",
        Console(file=output, width=1000, height=40, color_system=None),
    )
    attempt = attemptlog.Attempt(outcome=attemptlog.CaptureOutcome())
    assert not cli._report_usage_publication(
        [{"name": "mm-events", "include_dirs": ["events"]}],
        (path, row),
        manifest,
        cli.PushResult(),
        verbose=False,
        attempt=attempt,
    )
    assert output.getvalue().strip() == (
        f"mm: warning: Usage capture (unverified: {reason}): {remedy}. "
        f"User content was already up to date. See {cli.HOST_USAGE_CAPTURE_URL}"
    )
    assert (attempt.outcome.kind, attempt.outcome.cause) == ("unverified", reason)


def test_case_variant_manifest_key_still_proves_publication(tmp_path):
    row = host_row()
    path = tmp_path / "local.jsonl"
    path.write_text(json.dumps(row) + "\n")
    stored = f"Events/{path.name}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"sources": {"mm-events": {"files": {stored: {"sha256": digest}}}}}
    assert cli._usage_publication_verdict({"include_dirs": ["Events"]}, path, row, manifest) == (
        "published",
        None,
    )


def test_include_files_selects_day_file_and_missing_manifest_is_not_proof(tmp_path):
    row = host_row()
    path = tmp_path / "local.jsonl"
    path.write_text(json.dumps(row) + "\n")
    rel = f"events/{path.name}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"sources": {"mm-events": {"files": {rel: {"sha256": digest}}}}}
    src = {"include_dirs": [], "include_files": [rel]}
    assert cli._usage_publication_verdict(src, path, row, manifest) == ("published", None)
    with pytest.raises(ValueError, match="accepted manifest unavailable"):
        cli._usage_publication_verdict(src, path, row, None)


def test_row_missing_remedy_preserves_a_copy_before_repair(tmp_path, monkeypatch):
    import io

    from rich.console import Console

    from mind_meld import attemptlog

    row = host_row()
    path = tmp_path / f"local-{TODAY}.jsonl"
    path.write_bytes(b'{"torn":\n')
    rel = f"events/{path.name}"
    manifest = {
        "sources": {
            "mm-events": {"files": {rel: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}
        }
    }
    output = io.StringIO()
    monkeypatch.setattr(
        cli,
        "stderr_console",
        Console(file=output, width=1000, height=40, color_system=None),
    )
    attempt = attemptlog.Attempt(outcome=attemptlog.CaptureOutcome())
    assert not cli._report_usage_publication(
        [{"name": "mm-events", "include_dirs": ["events"]}],
        (path, row),
        manifest,
        cli.PushResult(),
        verbose=False,
        attempt=attempt,
    )
    text = output.getvalue()
    assert "(not-published: row-missing)" in text
    assert "preserve a copy outside mm-events" in text
    assert (attempt.outcome.kind, attempt.outcome.cause) == ("not-published", "row-missing")


def test_winner_in_a_failing_file_before_the_margin_is_uncertain(tmp_path, monkeypatch):
    row = {**host_row(), "ts": NOW.isoformat()}
    path = tmp_path / "local-2026-09-01.jsonl"
    path.write_bytes((json.dumps(row) + "\n").encode() + b"x" * 2048 + b"\n")
    monkeypatch.setattr(token_usage, "MAX_JSONL_LINE_BYTES", 1024)
    scan = events.latest_event_rows(
        tmp_path,
        "local",
        {"host-usage-snapshot"},
        now=NOW,
        selectors={"host-usage-snapshot": lambda item: (("k",), item)},
        day_margins={"host-usage-snapshot": timedelta(hours=24)},
    )
    assert "host-usage-snapshot" in scan.uncertain_types
    assert events.project_host_publication(scan, ["codex"], None, now=NOW)["state"] == "unknown"
