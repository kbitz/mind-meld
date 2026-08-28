"""Track 19A / 31A — the events tail's host-usage capture.

``tests/test_host_usage.py`` pins the READERS. This file pins the CALLER
policy those readers were written for: a snapshot when any consulted reader
completes, other readers dropped and declared on failure, and no row at all
when no reader completed (or the sweep expired before any ran). Never an
invented zero.

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

    def test_a_failed_reader_does_not_stop_the_remaining_readers(self):
        """Track 31A: reader-scoped isolation. A grok failure used to
        short-circuit opencode and discard Codex. Now every reader runs."""
        log: list = []
        readers = _readers(
            ("codex", _recording_reader(_complete({"codex": {"d": _usage(5)}}), log, "codex")),
            ("grok", _recording_reader(_incomplete("malformed"), log, "grok")),
            ("opencode", _recording_reader(_complete(), log, "opencode")),
        )

        capture = events_tail._capture_host_usage(readers, deadline=1_000.0, now=lambda: 0.0)

        assert [name for name, _ in log] == ["codex", "grok", "opencode"]
        assert capture.complete is True
        assert capture.hosts == {"codex": {"d": _usage(5)}}
        assert capture.token_sources == ("codex", "opencode")
        assert capture.dropped == (("grok", "malformed"),)

    def test_completed_data_is_kept_when_a_sibling_read_fails(self):
        """T3-1: `unsupported` drops Grok, declared; Codex still publishes.
        Must fail on HEAD (the sweep used to omit the whole row)."""
        capture = events_tail._capture_host_usage(
            _readers(
                ("codex", lambda **_kw: _complete({"codex": {"2026-08-15": _usage(100)}})),
                ("grok", lambda **_kw: _incomplete("unsupported")),
            ),
            deadline=1_000.0,
            now=lambda: 0.0,
        )
        assert capture.hosts == {"codex": {"2026-08-15": _usage(100)}}
        assert capture.token_sources == ("codex",)
        assert capture.dropped == (("grok", "unsupported"),)

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

    def test_reader_exception_is_contained_and_does_not_veto_siblings(self):
        """Contained HERE, not at the tail's outer guard: that guard would
        also discard the git and session rows already captured, and the
        terminal mm-push row with them. Track 31A: the raise is a reader
        failure; other readers still publish."""
        log: list = []
        readers = _readers(
            ("codex", _recording_reader(_complete({"codex": {"d": _usage(1)}}), log, "codex")),
            ("grok", _recording_reader(RuntimeError("synthetic reader crash"), log, "grok")),
            ("opencode", _recording_reader(_complete(), log, "opencode")),
        )

        capture = events_tail._capture_host_usage(readers, deadline=1_000.0, now=lambda: 0.0)

        assert capture.complete is True
        assert capture.hosts == {"codex": {"d": _usage(1)}}
        assert capture.dropped == (("grok", "unavailable"),)
        assert [name for name, _ in log] == ["codex", "grok", "opencode"]

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
    def test_expiry_before_any_reader_is_still_a_sweep_veto(self):
        """T3-6: sweep expired before ANY reader was invoked → no row.
        Explicit decision: this stays a veto. Expiry after some readers
        completed is degraded, not veto — see the next test."""
        log: list = []
        names = ["codex", "grok", "opencode"]
        readers = _readers(
            *[(names[i], _recording_reader(_complete(), log, names[i])) for i in range(3)]
        )

        capture = events_tail._capture_host_usage(deadline=10.0, readers=readers, now=lambda: 100.0)

        assert capture.hosts is None
        assert capture.reason == "deadline"
        assert capture.reader == "codex"
        assert capture.invoked is False
        assert capture.dropped == ()
        assert log == []

    def test_expiry_after_some_readers_publishes_them_and_drops_the_rest(self):
        """Codex completed, then the deadline hit before Grok: publish Codex,
        declare remaining readers as deadline. Not a veto."""
        log: list = []
        clock = {"t": 0.0}
        names = ["codex", "grok", "opencode"]

        def reader_for(index: int):
            def read(*, deadline):
                log.append(names[index])
                clock["t"] = 100.0
                return _complete({"codex": {"2026-08-15": _usage(1)}} if index == 0 else {})

            return read

        readers = _readers(*[(names[i], reader_for(i)) for i in range(3)])
        capture = events_tail._capture_host_usage(
            deadline=10.0, readers=readers, now=lambda: clock["t"]
        )

        assert capture.complete is True
        assert capture.token_sources == ("codex",)
        assert capture.dropped == (("grok", "deadline"), ("opencode", "deadline"))
        assert log == ["codex"]

    def test_expiry_after_a_failed_reader_keeps_it_declared(self):
        """An invoked failure is not a pre-invoke sweep veto.

        The actual failed reader must stay in ``dropped`` so the interactive
        retry warms it, rather than incorrectly charging the next reader.
        """
        clock = {"t": 0.0}
        log: list[str] = []

        def codex(*, deadline):
            log.append("codex")
            clock["t"] = 100.0
            return _incomplete("deadline")

        capture = events_tail._capture_host_usage(
            deadline=10.0,
            readers=_readers(
                ("codex", codex),
                ("grok", lambda **_kw: pytest.fail("Grok must not run")),
                ("opencode", lambda **_kw: pytest.fail("OpenCode must not run")),
            ),
            now=lambda: clock["t"],
        )

        assert capture.hosts is None
        assert capture.reader == "codex"
        assert capture.reason == "deadline"
        assert capture.invoked is True
        assert capture.dropped == (
            ("codex", "deadline"),
            ("grok", "deadline"),
            ("opencode", "deadline"),
        )
        assert log == ["codex"]

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
        assert types == ["git-snapshot", "host-usage-snapshot", "mm-push"], (
            "content rows must survive a host reader drop; isolation publishes the others"
        )
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert "grok" not in row["token_sources"]
        assert row["degraded_sources"] == ["grok"]
        assert degradations == [
            "host-usage snapshot skipped (grok unsupported) — "
            "content sync and git/session capture unaffected. "
            "grok's log format changed in a way this version cannot read. "
            "Upgrade mm, or run `mm disable-source grok` to stop retrying."
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

        omitted_sources = _sources(events_root, hosts=())
        _stub_hosts(monkeypatch, grok=_incomplete("unsupported"))
        omitted_degradations = events_tail._run_events_tail(
            _tail_config(omitted_sources, grok=True),
            omitted_sources,
            "dev-a",
            dry_run=False,
            quiet=True,
        )
        assert omitted_degradations == [
            "host-usage snapshot skipped (grok unsupported) — "
            "content sync and git/session capture unaffected. "
            "grok's log format changed in a way this version cannot read. "
            "Upgrade mm, or run `mm disable-source grok` to stop retrying."
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

        types = [r["type"] for r in _rows(events_root)]
        assert "mm-push" in types
        assert degradations[0].startswith("host-usage snapshot skipped (codex unavailable)")

    @pytest.mark.parametrize(
        "reason",
        sorted(set(get_args(_mm_host_usage.Reason)) - events_tail._HOST_ABSENT_REASONS),
    )
    def test_every_failure_reason_drops_that_reader_and_publishes_others(
        self, tmp_path, monkeypatch, reason
    ):
        """T3 rewrite: a single reader failure no longer omits the row.
        Codex still publishes; the failed reader is named in degradations
        and ``degraded_sources``."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(9)}}),
            opencode=_incomplete(reason),
        )

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=True
        )

        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["token_sources"] == ["codex"]
        assert row["degraded_sources"] == ["opencode"]
        assert "opencode" not in row["token_sources"]
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
        """FLIP (Track 31A): degradations used to be empty (carve-out silence).
        Isolation still publishes Codex, but now NAMES grok."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(11)}}),
            grok=_incomplete("deadline"),
        )

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {"codex": {"2026-08-15": _usage(11)}}
        assert "grok" not in row["token_sources"]
        assert row["degraded_sources"] == ["grok"]
        assert any("grok deadline" in d for d in degradations)

    def test_post_success_grok_deadline_still_publishes_others(self, tmp_path, monkeypatch):
        """FLIP: used to omit the row after complete_once latched. Isolation
        makes the latch irrelevant — Codex publishes, grok is declared."""
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

        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["token_sources"] == ["codex", "opencode"]
        assert row["degraded_sources"] == ["grok"]
        assert any("grok deadline" in d for d in degradations)

    def test_grok_only_failure_still_omits_the_row(self, tmp_path, monkeypatch):
        """FLIP re-derived: grok is the only reader, so no sibling completed
        → still no row. Isolation does not invent a zero."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root, hosts=())
        _stub_fast_walks(monkeypatch)
        _stub_hosts(monkeypatch, grok=_incomplete("deadline"))

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        assert not [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert degradations[0].startswith("host-usage snapshot skipped (grok deadline)")

    @pytest.mark.parametrize("reason", ["malformed", "unsupported", "stale"])
    def test_grok_hard_fail_drops_grok_and_publishes_others(self, tmp_path, monkeypatch, reason):
        """FLIP: used to omit the row for malformed/unsupported/stale even
        pre-latch. Now Codex publishes and grok is declared."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(11)}}),
            grok=_incomplete(reason),
        )

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {"codex": {"2026-08-15": _usage(11)}}
        assert row["degraded_sources"] == ["grok"]
        assert any(f"grok {reason}" in d for d in degradations)

    def test_all_readers_fail_omits_the_row(self, tmp_path, monkeypatch):
        """T3-5: isolation does not invent a row when nobody completed."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_incomplete("malformed"),
            grok=_incomplete("unsupported"),
            opencode=_incomplete("busy"),
        )

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        assert not [r for r in _rows(events_root) if r["type"] == "host-usage-snapshot"]
        assert any("codex malformed" in d for d in degradations)
        assert any("grok unsupported" in d for d in degradations)
        assert any("opencode busy" in d for d in degradations)

    def test_codex_fails_grok_completes_is_symmetric(self, tmp_path, monkeypatch):
        """T3-8: isolation is per-reader, not Grok-special."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_incomplete("unsupported"),
            grok=_complete({"grok": {"2026-08-15": _usage(3)}}),
        )

        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )

        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["token_sources"] == ["grok", "opencode"]
        assert row["degraded_sources"] == ["codex"]
        assert "grok" not in row["degraded_sources"]
        assert any("codex unsupported" in d for d in degradations)

    def test_degraded_sources_is_disjoint_subsequence(self, tmp_path, monkeypatch):
        """T3-3: shape pin. Names only, never a reason string or path."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root)
        _stub_fast_walks(monkeypatch)
        _stub_hosts(
            monkeypatch,
            codex=_complete({"codex": {"2026-08-15": _usage(1)}}),
            grok=_incomplete("unsupported"),
        )
        degradations = events_tail._run_events_tail(
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=True
        )
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        universe = list(_mm_events.HOST_USAGE_TOKEN_SOURCES)
        degraded = row["degraded_sources"]
        assert degraded == [s for s in universe if s in set(degraded)]
        assert set(degraded).isdisjoint(row["token_sources"])
        assert all(isinstance(s, str) and "/" not in s for s in degraded)
        dumped = json.dumps(row)
        assert "unsupported" not in dumped
        assert degradations  # T3-2: the drop reaches degradations

    def test_permanent_skip_phrase_carries_a_fix_clause(self):
        """T3-9: permanent branch used to append nothing."""
        phrase = events_tail._host_skip_phrase("grok", "unsupported")
        assert "Upgrade mm" in phrase
        assert "mm disable-source grok" in phrase
        assert "A later substantive push will retry" not in phrase
        assert "; " not in phrase

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

    def test_interactive_push_warms_a_dropped_grok_reader_then_retries(self, tmp_path, monkeypatch):
        """X-2: the warm target comes from ``dropped``, not the no-row labels."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root, hosts=("codex", "grok", "opencode"))
        _stub_fast_walks(monkeypatch)
        state = {"warm": False}
        calls: list[str] = []

        def codex(*, deadline):
            calls.append("codex")
            return _complete()

        def grok(*, deadline, consented=False):
            calls.append("grok")
            return _complete() if state["warm"] else _incomplete("deadline")

        def opencode(*, deadline):
            calls.append("opencode")
            return _complete()

        def warm(*, reader):
            calls.append(f"warm:{reader}")
            state["warm"] = True
            return _complete()

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", codex)
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", grok)
        monkeypatch.setattr(_mm_host_usage, "read_opencode_usage", opencode)
        monkeypatch.setattr(_mm_host_usage, "warm_host_cache_inline", warm)

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=False
        )

        assert calls == [
            "codex",
            "grok",
            "opencode",
            "warm:grok",
            "grok",
        ]
        assert degradations == []
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["token_sources"] == ["codex", "grok", "opencode"]
        assert "degraded_sources" not in row

    def test_warm_retry_preserves_completed_first_pass_readers(self, tmp_path, monkeypatch):
        """A warm Grok retry must not replace prior Codex/OpenCode totals.

        The second Codex call deliberately raises: retrying every reader and
        replacing the first capture would drop its completed totals. Only the
        deadline-dropped Grok reader is safe to retry.
        """
        events_root = tmp_path / "events_root"
        sources = _sources(events_root, hosts=("codex", "grok", "opencode"))
        _stub_fast_walks(monkeypatch)
        state = {"warm": False}
        calls: list[str] = []

        def codex(*, deadline):
            calls.append("codex")
            if state["warm"]:
                raise AssertionError("completed Codex reader must not be retried")
            return _complete({"codex": {"2026-08-15": _usage(3)}})

        def grok(*, deadline, consented=False):
            calls.append("grok")
            if not state["warm"]:
                return _incomplete("deadline")
            return _complete({"grok": {"2026-08-15": _usage(5)}})

        def opencode(*, deadline):
            calls.append("opencode")
            return _complete({"codex": {"2026-08-15": _usage(4)}})

        def warm(*, reader):
            calls.append(f"warm:{reader}")
            state["warm"] = True
            return _complete()

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", codex)
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", grok)
        monkeypatch.setattr(_mm_host_usage, "read_opencode_usage", opencode)
        monkeypatch.setattr(_mm_host_usage, "warm_host_cache_inline", warm)

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=False
        )

        assert calls == ["codex", "grok", "opencode", "warm:grok", "grok"]
        assert degradations == []
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {
            "codex": {"2026-08-15": _usage(7)},
            "grok": {"2026-08-15": _usage(5)},
        }
        assert row["token_sources"] == ["codex", "grok", "opencode"]
        assert "degraded_sources" not in row

    def test_warm_retry_keeps_first_pass_data_when_the_reader_still_fails(
        self, tmp_path, monkeypatch
    ):
        """An unsuccessful retry declares Grok without discarding Codex."""
        events_root = tmp_path / "events_root"
        sources = _sources(events_root, hosts=("codex", "grok"))
        _stub_fast_walks(monkeypatch)
        calls: list[str] = []

        def codex(*, deadline):
            calls.append("codex")
            return _complete({"codex": {"2026-08-15": _usage(3)}})

        def grok(*, deadline, consented=False):
            calls.append("grok")
            return _incomplete("deadline")

        def warm(*, reader):
            calls.append(f"warm:{reader}")
            return _complete()

        monkeypatch.setattr(_mm_host_usage, "read_codex_usage", codex)
        monkeypatch.setattr(_mm_host_usage, "read_grok_usage", grok)
        monkeypatch.setattr(_mm_host_usage, "warm_host_cache_inline", warm)

        degradations = events_tail._run_events_tail(
            {"sync": {"sources": sources}}, sources, "dev-a", dry_run=False, quiet=False
        )

        assert calls == ["codex", "grok", "warm:grok", "grok"]
        assert len(degradations) == 1
        assert degradations[0].startswith("host-usage snapshot skipped (grok deadline)")
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert row["hosts"] == {"codex": {"2026-08-15": _usage(3)}}
        assert row["token_sources"] == ["codex"]
        assert row["degraded_sources"] == ["grok"]

    def test_preinvoke_retry_keeps_the_initial_deadline_declarations(self):
        """A retry that never starts cannot replace first-pass outcomes."""
        initial = events_tail.HostUsageCapture(
            {"codex": {"2026-08-15": _usage(3)}},
            token_sources=("codex",),
            dropped=(("grok", "deadline"), ("opencode", "deadline")),
            invoked=True,
        )
        retry = events_tail.HostUsageCapture(None, "grok", "deadline", invoked=False)

        merged = events_tail._merge_warm_retry_capture(
            initial,
            retry,
            readers=_readers(
                ("codex", lambda **_kw: _complete()),
                ("grok", lambda **_kw: _complete()),
                ("opencode", lambda **_kw: _complete()),
            ),
            retried_names={"grok", "opencode"},
        )

        assert merged.hosts == {"codex": {"2026-08-15": _usage(3)}}
        assert merged.token_sources == ("codex",)
        assert merged.dropped == (("grok", "deadline"), ("opencode", "deadline"))

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
        row = next(r for r in _rows(events_root) if r["type"] == "host-usage-snapshot")
        assert "codex" not in row["token_sources"]
        assert row["degraded_sources"] == ["codex"]
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
            _tail_config(sources, grok=True), sources, "dev-a", dry_run=False, quiet=False
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
        types = [r["type"] for r in _rows(events_root)]
        assert "mm-push" in types
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
