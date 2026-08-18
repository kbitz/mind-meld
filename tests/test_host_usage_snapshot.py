"""Track 19A — the events tail's all-or-nothing host-usage capture.

``tests/test_host_usage.py`` pins the READERS. This file pins the CALLER
policy those readers were written for: one snapshot when every built-in reader
completes, no row at all when any of them does not, and never a partial total
or an invented zero in between.

Every test here injects or monkeypatches the readers. None of them may touch a
real ``~/.codex/sessions``, ``~/.grok/sessions``, or OpenCode database — the
autouse ``_isolate_host_usage`` fixture in ``conftest.py`` is the backstop, and
a test that needs data supplies it explicitly.

Read ``docs/invariants/events-retro.md`` before changing any of this.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest

from mind_meld import events as _mm_events
from mind_meld import events_tail
from mind_meld import host_usage as _mm_host_usage
from mind_meld import identity as _mm_identity


def _usage(input_tokens: int = 0, cache_create: int = 0, cache_read: int = 0, output: int = 0):
    return {
        "input": input_tokens,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "output": output,
    }


def _complete(hosts: dict | None = None) -> _mm_host_usage.HostUsageResult:
    return _mm_host_usage.HostUsageResult(hosts if hosts is not None else {}, complete=True)


def _incomplete(reason: str) -> _mm_host_usage.HostUsageResult:
    return _mm_host_usage.HostUsageResult({}, complete=False, reason=reason)


def _recording_reader(result, log: list, name: str):
    """A reader that records the deadline it was handed, then answers."""

    def read(*, deadline):
        log.append((name, deadline))
        if isinstance(result, Exception):
            raise result
        return result

    return read


def _readers(*pairs):
    return tuple(pairs)


# ---------------------------------------------------------------------------
# The orchestration seam: order, short-circuit, deadline, containment, merge.
# ---------------------------------------------------------------------------


class TestReaderOrchestration:
    def test_all_readers_run_in_order_under_one_explicit_deadline(self):
        """Every invoked reader gets the SAME absolute deadline, explicitly.

        Falling through to ``host_usage``'s 5-second default would be ~20x an
        entire autopush walk budget, spent on optional analytics.
        """
        log: list = []
        readers = _readers(
            ("codex", _recording_reader(_complete(), log, "codex")),
            ("grok", _recording_reader(_complete(), log, "grok")),
            ("opencode", _recording_reader(_complete(), log, "opencode")),
        )

        capture = events_tail._capture_host_usage(readers, deadline=1_000.0, now=lambda: 0.0)

        assert [name for name, _ in log] == ["codex", "grok", "opencode"]
        assert {deadline for _, deadline in log} == {1_000.0}
        assert capture.complete is True

    def _enabled(self, *names: str) -> list[dict]:
        return [{"name": n, "path": f"/tmp/{n}", "type": "generic"} for n in names]

    def test_default_readers_are_the_three_built_ins_in_fixed_order(self):
        assert [
            name
            for name, _ in events_tail._default_host_readers(
                self._enabled("codex", "opencode"), grok_consented=True
            )
        ] == ["codex", "grok", "opencode"]

    def test_readers_are_gated_on_the_user_enabling_that_source(self):
        """A host's local store is only read when the user enabled that host as
        a sync source — the same consent gate the Claude session walk has always
        had via `_enabled_claude_paths`. Without it, declining the `codex`
        source still got `~/.codex/sessions` parsed and the totals published."""
        assert [n for n, _ in events_tail._default_host_readers(self._enabled("codex"))] == [
            "codex",
        ]
        assert [n for n, _ in events_tail._default_host_readers(self._enabled("opencode"))] == [
            "opencode",
        ]
        # If this is ever `["grok"]` again, an 18D reader opens ~/.grok with
        # no opt-in.
        assert [n for n, _ in events_tail._default_host_readers([])] == []
        grok_only = events_tail._default_host_readers([], grok_consented=True)
        assert [n for n, _ in grok_only] == ["grok"]
        grok_source = events_tail._default_host_readers(self._enabled("grok"))
        assert [n for n, _ in grok_source] == ["grok"]
        assert events_tail.HOST_READER_SOURCE_GATE["grok"] == "grok"

    def test_consented_grok_reader_passes_consented_true(self, monkeypatch):
        seen: dict[str, bool] = {}

        def read(*, deadline, consented=False):
            seen["consented"] = consented
            return _complete()

        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", read)
        readers = events_tail._default_host_readers([], grok_consented=True)
        readers[0][1](deadline=1_000.0)
        assert seen == {"consented": True}

    def test_source_authorized_grok_reader_passes_consented_true(self, monkeypatch):
        seen: dict[str, bool] = {}

        def read(*, deadline, consented=False):
            seen["consented"] = consented
            return _complete()

        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", read)
        readers = events_tail._default_host_readers(self._enabled("grok"))
        readers[0][1](deadline=1_000.0)
        assert seen == {"consented": True}

    def test_pre_success_transients_are_real_reader_reasons(self):
        assert events_tail._GROK_PRE_SUCCESS_TRANSIENTS <= (
            events_tail._HOST_READ_REASONS | {events_tail._HOST_UNKNOWN_REASON}
        )

    def test_gate_map_covers_every_built_in_reader(self):
        """A new reader added without a gate entry would read a host store with
        no consent check at all."""
        assert set(events_tail.HOST_READER_SOURCE_GATE) == set(_mm_events.HOST_USAGE_TOKEN_SOURCES)

    def test_default_readers_resolve_at_call_time(self, monkeypatch):
        """Module-qualified lookup, so patching ``host_usage.read_codex_usage``
        actually reaches the tail. A from-import would bind events_tail's own
        global and the patch would be dead."""
        sentinel = _complete({"codex": {"2026-08-15": _usage(1)}})
        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", lambda **_kw: sentinel)
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", lambda **_kw: _complete())
        monkeypatch.setattr(_mm_host_usage, "read_opencode_usage", lambda **_kw: _complete())

        capture = events_tail._capture_host_usage(
            events_tail._default_host_readers(
                self._enabled("codex", "opencode"), grok_consented=True
            ),
            deadline=1_000.0,
            now=lambda: 0.0,
        )

        assert capture.hosts == {"codex": {"2026-08-15": _usage(1)}}

    def test_completed_empty_scan_is_complete(self):
        """No local host data is a fact, not a failure — the caller writes an
        explicit ``hosts: {}`` row and stays silent."""
        capture = events_tail._capture_host_usage(
            deadline=1_000.0,
            readers=_readers(("codex", lambda **_kw: _complete())),
            now=lambda: 0.0,
        )
        assert capture.complete is True
        assert capture.hosts == {}
        assert capture.reason == ""

    def test_first_failure_short_circuits_the_remaining_readers(self):
        """All-or-nothing for FAILURES: once the snapshot cannot ship, the
        later readers are wasted work on the push hot path."""
        log: list = []
        readers = _readers(
            ("codex", _recording_reader(_complete({"codex": {"d": _usage(5)}}), log, "codex")),
            ("grok", _recording_reader(_incomplete("malformed"), log, "grok")),
            ("opencode", _recording_reader(_complete(), log, "opencode")),
        )

        capture = events_tail._capture_host_usage(readers, deadline=1_000.0, now=lambda: 0.0)

        assert [name for name, _ in log] == ["codex", "grok"]
        assert capture.complete is False
        assert capture.hosts is None
        assert (capture.reader, capture.reason) == ("grok", "malformed")

    def test_completed_data_is_discarded_when_a_read_actually_fails(self):
        """`unsupported` means "real usage exists here that I could not read",
        so publishing without it would silently under-report. It keeps the
        veto — unlike `no_metadata_ledger` below."""
        capture = events_tail._capture_host_usage(
            _readers(
                ("codex", lambda **_kw: _complete({"codex": {"2026-08-15": _usage(100)}})),
                ("grok", lambda **_kw: _incomplete("unsupported")),
            ),
            deadline=1_000.0,
            now=lambda: 0.0,
        )
        assert capture.hosts is None

    def test_a_source_with_no_usage_ledger_is_dropped_not_a_veto(self):
        """Premise revised 2026-08-16. A store that can never hold a metadata
        ledger (Grok's transcript stream) is an ABSENT source, not a failed
        read — merely having Grok installed used to make the row unpublishable
        forever and pin `mm status` at `degraded`, destroying that breadcrumb
        as a signal for real sync degradation."""
        log: list = []
        capture = events_tail._capture_host_usage(
            _readers(
                (
                    "codex",
                    _recording_reader(
                        _complete({"codex": {"2026-08-15": _usage(100)}}), log, "codex"
                    ),
                ),
                ("grok", _recording_reader(_incomplete("no_metadata_ledger"), log, "grok")),
                ("opencode", _recording_reader(_complete(), log, "opencode")),
            ),
            deadline=1_000.0,
            now=lambda: 0.0,
        )

        assert [name for name, _ in log] == ["codex", "grok", "opencode"], (
            "an absent source must not short-circuit the readers after it"
        )
        assert capture.complete is True
        assert capture.hosts == {"codex": {"2026-08-15": _usage(100)}}
        assert capture.token_sources == ("codex", "opencode"), (
            "the row names only what contributed — grok is absent, not silent"
        )

    def test_absent_reasons_are_real_reader_reasons(self):
        """`_HOST_ABSENT_REASONS` hardcodes a literal while `_HOST_READ_REASONS`
        is derived. A rename on the host_usage side would empty it silently and
        turn every Grok machine back into a total veto."""
        assert events_tail._HOST_ABSENT_REASONS
        assert events_tail._HOST_ABSENT_REASONS <= events_tail._HOST_READ_REASONS
        assert not (events_tail._HOST_ABSENT_REASONS & events_tail._HOST_PERMANENT_REASONS), (
            "absent and permanent-failure are different verdicts and must not overlap"
        )

    def test_reader_exception_is_contained_as_an_incomplete_outcome(self):
        """Contained HERE, not at the tail's outer guard: that guard would
        also discard the git and session rows already captured, and the
        terminal mm-push row with them."""
        log: list = []
        readers = _readers(
            ("codex", _recording_reader(_complete(), log, "codex")),
            ("grok", _recording_reader(RuntimeError("synthetic reader crash"), log, "grok")),
            ("opencode", _recording_reader(_complete(), log, "opencode")),
        )

        capture = events_tail._capture_host_usage(readers, deadline=1_000.0, now=lambda: 0.0)

        assert capture.hosts is None
        assert capture.reader == "grok"
        assert capture.reason == "unavailable"
        assert [name for name, _ in log] == ["codex", "grok"]

    def test_reader_exception_text_never_reaches_the_outcome(self):
        """A raw exception can carry a path, a query, or transcript bytes."""
        capture = events_tail._capture_host_usage(
            deadline=1_000.0,
            readers=_readers(
                ("codex", _recording_reader(OSError("/Users/kb/.codex/sessions/secret"), [], "c"))
            ),
            now=lambda: 0.0,
        )
        assert "secret" not in capture.reason
        assert "/Users" not in capture.reason

    def test_unrecognized_reason_is_normalized_to_a_closed_vocabulary(self):
        """The reason lands in a user-visible notice and breadcrumb, so it is
        not a pass-through string."""
        capture = events_tail._capture_host_usage(
            deadline=1_000.0,
            readers=_readers(
                (
                    "codex",
                    lambda **_kw: _mm_host_usage.HostUsageResult(
                        {}, complete=False, reason="\x1b[31mnot-a-real-reason"
                    ),
                )
            ),
            now=lambda: 0.0,
        )
        assert capture.reason == "unavailable"

    def test_reason_vocabulary_tracks_host_usage(self):
        """Derived from ``host_usage.Reason`` so a new reader reason can never
        silently degrade to `unavailable`."""
        assert "unsupported" in events_tail._HOST_READ_REASONS
        assert "busy" in events_tail._HOST_READ_REASONS
        assert events_tail._HOST_UNKNOWN_REASON not in events_tail._HOST_READ_REASONS

    def test_permanent_reasons_are_real_reader_reasons(self):
        """`_HOST_PERMANENT_REASONS` hardcodes a literal while its sibling is
        derived. A rename on the host_usage side would empty it silently, and
        every omission would start promising a retry that never comes."""
        assert events_tail._HOST_PERMANENT_REASONS
        assert events_tail._HOST_PERMANENT_REASONS <= events_tail._HOST_READ_REASONS

    def test_built_in_constant_matches_the_full_reader_set(self):
        """`events.HOST_USAGE_TOKEN_SOURCES` documents the universe of readers
        and their order. `events` cannot import `events_tail` (cycle), so
        nothing but this pin holds the two together — a fourth reader added on
        one side only would go undocumented on the other."""
        assert [
            name
            for name, _ in events_tail._default_host_readers(
                self._enabled(*_mm_events.HOST_USAGE_TOKEN_SOURCES),
                grok_consented=True,
            )
        ] == list(_mm_events.HOST_USAGE_TOKEN_SOURCES)

    def test_only_codex_and_grok_are_warmable(self):
        """The warm gate keys on this set. OpenCode's adapter cache stores no
        totals, so it is not warmable."""
        assert events_tail.WARMABLE_HOST_READERS == frozenset({"codex", "grok"})
        assert events_tail.WARMABLE_HOST_READERS <= set(_mm_events.HOST_USAGE_TOKEN_SOURCES)


class TestAdditiveMerge:
    def test_codex_and_opencode_family_day_collision_sums_all_four_counters(self):
        """OpenCode classifies GPT models into the SAME canonical ``codex``
        family, so a (family, UTC day) collision between the two readers is
        ordinary. A shallow map update would drop whichever ran first."""
        capture = events_tail._capture_host_usage(
            deadline=1_000.0,
            readers=_readers(
                (
                    "codex",
                    lambda **_kw: _complete(
                        {"codex": {"2026-08-15": _usage(10, 1, 2, 3), "2026-08-14": _usage(7)}}
                    ),
                ),
                ("grok", lambda **_kw: _complete()),
                (
                    "opencode",
                    lambda **_kw: _complete(
                        {
                            "codex": {"2026-08-15": _usage(100, 10, 20, 30)},
                            "other": {"d": _usage(1)},
                        }
                    ),
                ),
            ),
            now=lambda: 0.0,
        )

        assert capture.hosts == {
            "codex": {
                "2026-08-15": _usage(110, 11, 22, 33),
                "2026-08-14": _usage(7),
            },
            "other": {"d": _usage(1)},
        }

    def test_merge_does_not_mutate_a_reader_result(self):
        codex_hosts = {"codex": {"2026-08-15": _usage(10)}}
        events_tail._capture_host_usage(
            deadline=1_000.0,
            readers=_readers(
                ("codex", lambda **_kw: _complete(codex_hosts)),
                ("opencode", lambda **_kw: _complete({"codex": {"2026-08-15": _usage(5)}})),
            ),
            now=lambda: 0.0,
        )
        assert codex_hosts == {"codex": {"2026-08-15": _usage(10)}}


class TestHostDeadline:
    @pytest.mark.parametrize("expire_before", [0, 1, 2])
    def test_expiry_before_any_reader_short_circuits_safely(self, expire_before):
        """Exhaustion before the first, middle, or last reader omits the row
        without invoking anything further — and without touching the separate
        session-walk budget, which was already snapshotted."""
        log: list = []
        clock = {"t": 0.0}
        names = ["codex", "grok", "opencode"]

        def reader_for(index: int):
            def read(*, deadline):
                log.append(names[index])
                # Time passes inside each reader; the (index)th one is the
                # first to find the deadline already blown.
                clock["t"] = 100.0 if index + 1 == expire_before else clock["t"]
                return _complete()

            return read

        clock["t"] = 100.0 if expire_before == 0 else 0.0
        readers = _readers(*[(names[i], reader_for(i)) for i in range(3)])

        capture = events_tail._capture_host_usage(
            deadline=10.0, readers=readers, now=lambda: clock["t"]
        )

        assert capture.hosts is None
        assert capture.reason == "deadline"
        assert capture.reader == names[expire_before]
        assert log == names[:expire_before]

    def test_budgets_are_separate_from_the_walk_budget(self):
        """Deliberately its own pair of constants: reusing the walk budget
        would let a busy host store redefine the session-walk notice."""
        assert events_tail.HOST_USAGE_READ_BUDGET_AUTOPUSH_MS == 250
        assert events_tail.HOST_USAGE_READ_BUDGET_INTERACTIVE_MS == 500
        assert (
            events_tail.HOST_USAGE_READ_BUDGET_AUTOPUSH_MS != _mm_host_usage.DEFAULT_READ_BUDGET_S
        )

    def test_capture_passes_a_bounded_deadline_to_the_readers(self, tmp_path, monkeypatch):
        """The deadline reaching the readers is derived from the host budget,
        not from ``host_usage``'s 5s default."""
        seen: list[float] = []

        def read(*, deadline, consented=False):
            seen.append(deadline)
            return _complete()

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", read)
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", read)
        monkeypatch.setattr(_mm_host_usage, "read_opencode_usage", read)
        _stub_fast_walks(monkeypatch)

        import time as _time

        before = _time.monotonic()
        events_tail._capture_event_snapshots(
            {},
            [],
            "dev-a",
            since=datetime.now(timezone.utc),
            budget_ms=500,
            host_budget_ms=250,
            prepare_token_cache=lambda: None,
            host_readers=events_tail._default_host_readers(
                [{"name": n, "type": "generic"} for n in ("codex", "opencode")],
                grok_consented=True,
            ),
        )
        after = _time.monotonic()

        assert len(seen) == 3
        assert len(set(seen)) == 1, "every reader must share one absolute deadline"
        assert before + 0.25 <= seen[0] <= after + 0.25


# ---------------------------------------------------------------------------
# Tail and backfill wiring.
# ---------------------------------------------------------------------------


def _stub_fast_walks(monkeypatch, git_rows: list | None = None):
    monkeypatch.setattr(
        _mm_events,
        "discover_git_roots",
        lambda _c, **_kw: _mm_events.GitRootDiscovery((), (), False),
    )
    monkeypatch.setattr(
        _mm_events,
        "walk_git_projects",
        lambda roots, since, total_budget_ms: list(git_rows or []),
    )
    monkeypatch.setattr(
        _mm_identity,
        "gather_local_identities",
        lambda *, allow_refresh=True, root_discovery=None: [],
    )
    monkeypatch.setattr(
        _mm_identity,
        "refresh_identity_cache",
        lambda *, force=False, root_discovery=None: [],
    )


def _sources(
    events_root: Path,
    claude_dir: Path | None = None,
    *,
    hosts: tuple[str, ...] = ("codex", "opencode"),
) -> list[dict]:
    """Resolved-source list. Host readers are CONSENT-GATED on these names, so
    a test that expects the codex/opencode readers to run must enable them —
    pass ``hosts=()`` to exercise the gate itself."""
    sources: list[dict] = []
    if claude_dir is not None:
        sources.append({"name": "claude", "path": str(claude_dir), "type": "claude"})
    for host in hosts:
        sources.append({"name": host, "path": str(events_root / host), "type": "generic"})
    sources.append(
        {
            "name": "mm-events",
            "path": str(events_root),
            "type": "generic",
            "include_dirs": ["events"],
            "exclude_patterns": [],
        }
    )
    return sources


def _tail_config(sources, *, grok: bool = False) -> dict:
    config: dict = {"sync": {"sources": sources}}
    if grok:
        config["retro"] = {"grok_host_usage": True}
    return config


def _stub_hosts(monkeypatch, codex=None, grok=None, opencode=None, calls: list | None = None):
    def make(name, result):
        def read(*, deadline, consented=False):
            if calls is not None:
                calls.append(name)
            if isinstance(result, Exception):
                raise result
            return result

        return read

    monkeypatch.setattr(_mm_host_usage, "read_codex_usage", make("codex", codex or _complete()))
    monkeypatch.setattr(_mm_host_usage, "read_grok_usage", make("grok", grok or _complete()))
    monkeypatch.setattr(
        _mm_host_usage, "read_opencode_usage", make("opencode", opencode or _complete())
    )


def _rows(events_root: Path) -> list[dict]:
    files = sorted((events_root / "events").glob("*.jsonl"))
    return [json.loads(ln) for f in files for ln in f.read_text().splitlines() if ln.strip()]


class TestTailWiring:
    def test_row_order_is_git_sessions_host_then_mm_push_last(self, tmp_path, monkeypatch):
        """CT-4 still holds: the optional host row sits before the terminal
        ``mm-push``, never displacing it."""
        events_root = tmp_path / "events_root"
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        sources = _sources(events_root, claude_dir)
        _stub_fast_walks(
            monkeypatch,
            git_rows=[
                {
                    "v": _mm_events.EVENTS_SCHEMA_VERSION,
                    "type": "git-snapshot",
                    "ts": "2026-08-15T00:00:00+00:00",
                    "device": "",
                    "projects": [],
                    "skipped": [],
                }
            ],
        )
        monkeypatch.setattr(_mm_events, "walk_session_metadata", lambda *a, **kw: [])
        monkeypatch.setattr(events_tail, "_decide_token_walk_policy", lambda paths, *, quiet: False)
        _stub_hosts(monkeypatch, codex=_complete({"codex": {"2026-08-15": _usage(9)}}))

        assert events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        ) == ["token walk skipped, so tokens and skills are missing"]

        assert [r["type"] for r in _rows(events_root)] == [
            "git-snapshot",
            "sessions-snapshot",
            "host-usage-snapshot",
            "mm-push",
        ]

    def test_healthy_empty_scan_writes_a_row_and_stays_silent(self, tmp_path, monkeypatch, capsys):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(monkeypatch)

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        host_rows = [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert len(host_rows) == 1
        assert host_rows[0]["hosts"] == {}
        assert host_rows[0]["active_days"] == []
        assert host_rows[0]["device"] == "dev-a"
        assert degradations == []
        assert "host-usage" not in capsys.readouterr().err

    def test_healthy_scan_carries_the_merged_reader_payload(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(10, 1, 2, 3)}}),
            opencode=_complete({"codex": {"2026-08-15": _usage(1, 1, 1, 1)}}),
        )

        events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {"codex": {"2026-08-15": _usage(11, 2, 3, 4)}}
        assert row["active_days"] == ["2026-08-15"]
        assert row["token_sources"] == ["codex", "grok", "opencode"]

    def test_incomplete_scan_omits_the_row_but_keeps_every_other_row(
        self, tmp_path, monkeypatch, capsys
    ):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(
            monkeypatch,
            git_rows=[
                {
                    "v": _mm_events.EVENTS_SCHEMA_VERSION,
                    "type": "git-snapshot",
                    "ts": "2026-08-15T00:00:00+00:00",
                    "device": "",
                    "projects": [],
                    "skipped": [],
                }
            ],
        )
        _stub_hosts(monkeypatch, grok=_incomplete("unsupported"))

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        types = [r["type"] for r in _rows(events_root)]
        assert types == ["git-snapshot", "mm-push"], "content rows must survive a host omission"
        assert degradations == [
            "host-usage snapshot skipped (grok unsupported) — "
            "content sync and git/session capture unaffected"
        ]
        assert f"mm: notice: {degradations[0]}" in capsys.readouterr().err

    def test_complete_omitted_then_complete_empty_preserves_wire_history(
        self, tmp_path, monkeypatch
    ):
        """The future consumer sees only completed observations.

        A failed middle capture must not write a synthetic zero or erase the
        prior complete row. A later completed empty scan is a new whole-device
        observation with its own coverage, not a carry-forward of the warm
        row's sources.
        """
        events_root = tmp_path / "events_root"
        _stub_fast_walks(monkeypatch)

        warm_sources = _sources(events_root, hosts=("codex",))
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(9)}}),
        )
        assert (
            events_tail._run_events_tail(
                {"sync": {"sources": warm_sources}},
                warm_sources,
                "dev-a",
                dry_run=False,
                quiet=True,
            )
            == []
        )

        _stub_hosts(monkeypatch, grok=_incomplete("unsupported"))
        omitted_degradations = events_tail._run_events_tail(
            _tail_config(warm_sources, grok=True),
            warm_sources,
            "dev-a",
            dry_run=False,
            quiet=True,
        )
        assert omitted_degradations == [
            "host-usage snapshot skipped (grok unsupported) — "
            "content sync and git/session capture unaffected"
        ]

        empty_sources = _sources(events_root, hosts=())
        _stub_hosts(monkeypatch, grok=_incomplete("no_metadata_ledger"))
        assert (
            events_tail._run_events_tail(
                {"sync": {"sources": empty_sources}},
                empty_sources,
                "dev-a",
                dry_run=False,
                quiet=True,
            )
            == []
        )

        rows = _rows(events_root)
        host_rows = [row for row in rows if row["type"] == "host-usage-snapshot"]
        assert len(host_rows) == 2
        assert all(row["ts"] for row in host_rows)
        assert [{key: value for key, value in row.items() if key != "ts"} for row in host_rows] == [
            {
                "v": _mm_events.EVENTS_SCHEMA_VERSION,
                "type": "host-usage-snapshot",
                "device": "dev-a",
                "token_sources": ["codex"],
                "hosts": {"codex": {"2026-08-15": _usage(9)}},
                "active_days": ["2026-08-15"],
            },
            {
                "v": _mm_events.EVENTS_SCHEMA_VERSION,
                "type": "host-usage-snapshot",
                "device": "dev-a",
                "token_sources": [],
                "hosts": {},
                "active_days": [],
            },
        ]
        assert [row["type"] for row in rows] == [
            "host-usage-snapshot",
            "mm-push",
            "mm-push",
            "host-usage-snapshot",
            "mm-push",
        ]

    def test_reader_exception_does_not_lose_the_mm_push_row(self, tmp_path, monkeypatch):
        """The cursor must still advance: a broken host store cannot make the
        next push re-walk 30 days of git history."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(monkeypatch, codex=RuntimeError("synthetic crash"))

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        assert [r["type"] for r in _rows(events_root)] == ["mm-push"]
        assert degradations == [
            "host-usage snapshot skipped (codex unavailable) — "
            "content sync and git/session capture unaffected. "
            "A later substantive push will retry"
        ]

    @pytest.mark.parametrize(
        "reason",
        sorted(set(get_args(_mm_host_usage.Reason)) - events_tail._HOST_ABSENT_REASONS),
    )
    def test_every_failure_reason_omits_the_whole_row(self, tmp_path, monkeypatch, reason):
        """Every reason that means "I could not read data that exists" keeps
        the all-or-nothing veto. `_HOST_ABSENT_REASONS` is excluded here and
        covered by its own test — that set is the ONLY carve-out."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(monkeypatch, opencode=_incomplete(reason))

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        assert not [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert len(degradations) == 1
        assert degradations[0].startswith(f"host-usage snapshot skipped (opencode {reason})")
        # Permanent vs transient: never promise a retry for a failure a later
        # push cannot fix.
        promises_retry = "A later substantive push will retry" in degradations[0]
        assert promises_retry is (reason not in events_tail._HOST_PERMANENT_REASONS)

    def test_an_absent_source_publishes_a_row_and_no_degradation(self, tmp_path, monkeypatch):
        """The whole point of the revised premise: a machine whose Grok store
        holds no ledger still publishes its Codex totals, and `mm status` stays
        clean instead of reading `degraded` forever."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(42)}}),
            grok=_incomplete("no_metadata_ledger"),
        )

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {"codex": {"2026-08-15": _usage(42)}}
        assert row["token_sources"] == ["codex", "opencode"]
        assert degradations == []

    def test_consent_off_never_invokes_grok(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        calls: list[str] = []
        _stub_hosts(monkeypatch, calls=calls)

        events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        assert "grok" not in calls
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert "grok" not in row["token_sources"]

    def test_pre_success_grok_deadline_drops_grok_and_publishes_others(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(11)}}),
            grok=_incomplete("deadline"),
        )
        monkeypatch.setattr(_mm_host_usage, "grok_completed_once", lambda: False)

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {"codex": {"2026-08-15": _usage(11)}}
        assert "grok" not in row["token_sources"]
        assert degradations == []

    def test_post_success_grok_deadline_omits_the_row(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(11)}}),
            grok=_incomplete("deadline"),
        )
        monkeypatch.setattr(_mm_host_usage, "grok_completed_once", lambda: True)

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        assert not [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert degradations[0].startswith("host-usage snapshot skipped (grok deadline)")

    def test_pre_success_grok_only_keeps_the_veto(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root, hosts=())
        _stub_fast_walks(monkeypatch)
        _stub_hosts(monkeypatch, grok=_incomplete("deadline"))
        monkeypatch.setattr(_mm_host_usage, "grok_completed_once", lambda: False)

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        assert not [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert degradations[0].startswith("host-usage snapshot skipped (grok deadline)")

    @pytest.mark.parametrize("reason", ["malformed", "unsupported", "stale"])
    def test_pre_success_hard_fail_still_omits_the_row(self, tmp_path, monkeypatch, reason):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(11)}}),
            grok=_incomplete(reason),
        )
        monkeypatch.setattr(_mm_host_usage, "grok_completed_once", lambda: False)

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        assert not [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert degradations[0].startswith(f"host-usage snapshot skipped (grok {reason})")

    def test_pre_invoke_grok_deadline_stays_a_sweep_veto(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        monkeypatch.setattr(_mm_host_usage, "grok_completed_once", lambda: False)
        captures: list[tuple[tuple[str, ...], bool]] = []

        def fake_capture(readers, *, deadline, now=None):
            names = tuple(name for name, _ in readers)
            invoked = "grok" not in names
            captures.append((names, invoked))
            if "grok" in names:
                return events_tail.HostUsageCapture(None, "grok", "deadline", invoked=False)
            return events_tail.HostUsageCapture(
                {"codex": {"2026-08-15": _usage(11)}}, token_sources=("codex",)
            )

        monkeypatch.setattr(events_tail, "_capture_host_usage", fake_capture)

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        assert captures == [(("codex", "grok", "opencode"), False)]
        assert not [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert degradations[0].startswith("host-usage snapshot skipped (grok deadline)")

    def test_disabled_host_source_is_never_read_and_never_claimed(self, tmp_path, monkeypatch):
        """Consent gate end-to-end: no `codex` source → the reader is not
        invoked, and the row does not claim codex was consulted."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root, hosts=("opencode",))
        _stub_fast_walks(monkeypatch)
        calls: list[str] = []
        _stub_hosts(monkeypatch, calls=calls)

        events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        assert "codex" not in calls, "an un-enabled host store must not be read"
        assert "grok" not in calls, "Grok is not consulted without opt-in"
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["token_sources"] == ["opencode"]

    @pytest.mark.parametrize("reason", sorted(get_args(_mm_host_usage.Reason)))
    def test_degradation_phrase_is_safe_and_splittable(self, reason):
        phrase = events_tail._host_skip_phrase("grok", reason)
        assert "; " not in phrase, (
            "`; ` is the breadcrumb join separator — a phrase containing it "
            "makes the joined `mm status` detail ambiguous to split"
        )
        assert "content sync and git/session capture unaffected" in phrase
        assert "\x1b" not in phrase

    def test_dry_run_touches_no_host_reader(self, tmp_path, monkeypatch):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        calls: list[str] = []
        _stub_hosts(monkeypatch, calls=calls)

        assert (
            events_tail._run_events_tail(
                {"sync": {"sources": sources}}, sources, "dev-a", dry_run=True, quiet=True
            )
            == []
        )
        assert calls == []
        assert not (events_root / "events").exists()

    def test_unresolved_mm_events_source_touches_no_host_reader(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        sources = [{"name": "claude", "path": str(claude_dir), "type": "claude"}]
        calls: list[str] = []
        _stub_hosts(monkeypatch, calls=calls)

        assert (
            events_tail._run_events_tail(
                {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
            )
            == []
        )
        assert calls == []


class TestColdCacheWarmAndRetry:
    """A cold corpus does not fit the per-capture budget (573 ms measured
    against a 250/500 ms bound), so an attended command may spend one warm.

    The gate is a FAILED bounded attempt, not an "is it cold?" predicate:
    that costs nothing on the happy path, needs no persisted marker, and a
    machine with no host data never warms because its first attempt completes.
    """

    def _flaky_codex(self, monkeypatch, calls: list[str]):
        """Codex that only succeeds once the cache has been warmed."""
        state = {"warm": False}

        def read(*, deadline):
            calls.append("read-warm" if state["warm"] else "read-cold")
            if not state["warm"]:
                return _incomplete("deadline")
            return _complete({"codex": {"2026-08-15": _usage(7)}})

        def warm(*_a, **_kw):
            calls.append("warm")
            state["warm"] = True
            return _complete()

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", read)
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", lambda **_kw: _complete())
        monkeypatch.setattr(_mm_host_usage, "read_opencode_usage", lambda **_kw: _complete())
        monkeypatch.setattr(_mm_host_usage, "warm_host_cache_inline", warm)

    def test_interactive_push_warms_then_retries_and_publishes(self, tmp_path, monkeypatch, capsys):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        calls: list[str] = []
        self._flaky_codex(monkeypatch, calls)

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=False
        )

        assert calls == ["read-cold", "warm", "read-warm"]
        assert degradations == []
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {"codex": {"2026-08-15": _usage(7)}}
        assert "warming host usage cache" in capsys.readouterr().err

    def test_autopush_never_warms(self, tmp_path, monkeypatch):
        """An unattended hook must not spend seconds on optional analytics.
        A cold autopush converges instead, because an aborted host scan now
        keeps its per-file progress."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        calls: list[str] = []
        self._flaky_codex(monkeypatch, calls)

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        assert calls == ["read-cold"], "autopush must not warm or retry"
        assert not [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert len(degradations) == 1
        assert degradations[0].startswith("host-usage snapshot skipped (codex deadline)")

    def test_healthy_capture_never_pays_for_a_warm(self, tmp_path, monkeypatch, capsys):
        """Zero cost on the happy path — the whole reason the gate is a failed
        attempt rather than a coldness heuristic."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        calls: list[str] = []
        _stub_hosts(monkeypatch, calls=calls)
        monkeypatch.setattr(
            _mm_host_usage,
            "warm_host_cache_inline",
            lambda *_a, **_kw: calls.append("warm") or _complete(),
        )

        events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=False
        )

        assert "warm" not in calls
        assert "warming host usage cache" not in capsys.readouterr().err

    @pytest.mark.parametrize("reason", ["unsupported", "locked", "malformed", "busy"])
    def test_only_a_deadline_triggers_the_warm(self, tmp_path, monkeypatch, reason):
        """Warming cannot fix a refused, locked, or malformed store — spending
        seconds on it would be a guess dressed up as a remedy."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        calls: list[str] = []
        _stub_hosts(monkeypatch, grok=_incomplete(reason), calls=calls)
        monkeypatch.setattr(
            _mm_host_usage,
            "warm_host_cache_inline",
            lambda *_a, **_kw: calls.append("warm") or _complete(),
        )

        events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=False
        )

        assert "warm" not in calls

    def test_a_warm_that_cannot_finish_does_not_buy_a_retry(self, tmp_path, monkeypatch):
        """The reader gate alone does not bound the repeat cost.

        Once the corpus outgrows even the warm's own (much larger) budget, the
        outcome stays (deadline, codex) forever, so both halves of the gate keep
        passing. If the warm could not finish, the bounded read that follows
        cannot either — paying for it just adds latency to a push that will
        publish nothing. Left unguarded this is ~6s on EVERY interactive push.
        """
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        reads: list[str] = []

        def read(*, deadline):
            reads.append("read")
            return _incomplete("deadline")

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", read)
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", lambda **_kw: _complete())
        monkeypatch.setattr(_mm_host_usage, "read_opencode_usage", lambda **_kw: _complete())
        # The warm runs but cannot finish either.
        monkeypatch.setattr(
            _mm_host_usage, "warm_host_cache_inline", lambda *_a, **_kw: _incomplete("deadline")
        )

        events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=False
        )

        assert reads == ["read"], "an unfinishable warm must not buy a bounded retry"

    def test_reader_exception_type_reaches_stderr(self, tmp_path, monkeypatch, capsys):
        """The breadcrumb reason stays a closed vocabulary, but a swallowed
        exception with NO type is indistinguishable from a benign transient —
        every other swallow in this module carries `type(e).__name__`."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(monkeypatch, codex=RuntimeError("synthetic reader crash"))

        events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        err = capsys.readouterr().err
        assert "host reader codex raised: RuntimeError" in err

    def test_failed_warm_is_forensic_and_the_push_still_completes(
        self, tmp_path, monkeypatch, capsys
    ):
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(monkeypatch, codex=_incomplete("deadline"))

        def boom(*_a, **_kw):
            raise RuntimeError("synthetic warm failure")

        monkeypatch.setattr(_mm_host_usage, "warm_host_cache_inline", boom)

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=False
        )

        err = capsys.readouterr().err
        assert "host usage cache warm failed: RuntimeError" in err
        assert "events tail failed" not in err, "a failed warm must not abort the tail"
        assert [r["type"] for r in _rows(events_root)] == ["mm-push"]
        assert len(degradations) == 1

    def test_init_backfill_warms(self, tmp_path, monkeypatch):
        """Init is attended and unbudgeted, and already warms the token cache
        inline — so the first push after install inherits a hot host cache."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        calls: list[str] = []
        self._flaky_codex(monkeypatch, calls)

        events_tail._run_events_backfill({"sync": {"sources": sources}}, sources, "dev-a")

        assert calls == ["read-cold", "warm", "read-warm"]
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {"codex": {"2026-08-15": _usage(7)}}


class TestHostCaptureDoesNotDisturbTheWalkBudget:
    def test_slow_host_read_does_not_trip_the_walk_notice(self, tmp_path, monkeypatch, capsys):
        """``walk_done`` is snapshotted BEFORE host capture, so host time can
        never masquerade as a slow session walk — the same boundary v0.12.9
        established for the identity gather."""
        import time as _time

        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        monkeypatch.setattr(_mm_events, "WALK_TIME_BUDGET_AUTOPUSH_MS", 10)
        # The HOST budget is not what this pin is about. Left at its real
        # 250ms, three 50ms sleeps leave only 100ms of headroom, and a loaded
        # CI runner turns this into a `deadline` omission that fails on
        # `degradations == []` for a reason unrelated to the walk boundary.
        monkeypatch.setattr(events_tail, "HOST_USAGE_READ_BUDGET_AUTOPUSH_MS", 60_000)

        def slow_read(*, deadline):
            _time.sleep(0.05)
            return _complete()

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", slow_read)
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", slow_read)
        monkeypatch.setattr(_mm_host_usage, "read_opencode_usage", slow_read)

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        err = capsys.readouterr().err
        assert "events tail failed" not in err
        assert "events tail budget exceeded" not in err
        assert degradations == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
