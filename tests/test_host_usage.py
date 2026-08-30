"""Focused regression coverage for the private Codex host-usage reader.

Fixtures are synthetic and redacted. The named 2am regression is the
executable contract for cumulative replacement, deletion pruning, and refusal
to persist a partial discovery pass.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from mind_meld import host_usage as hu
from mind_meld import token_usage as tu

FIXTURES = Path(__file__).parent / "fixtures" / "host_sessions"


def _context(model: str = "gpt-5-codex", *, turn: str | None = None) -> dict:
    payload: dict = {"model": model}
    if turn is not None:
        payload["turn_id"] = turn
    return {
        "timestamp": "2026-08-14T23:59:58Z",
        "type": "turn_context",
        "payload": payload,
    }


_OMIT = object()


def _last_map(last: object) -> dict:
    """Build a `last_token_usage` map. Real records carry all four counters.

    An int is the input-only shorthand; a 4-tuple sets
    (input, cache_create, cache_read, output) so a fixture can exercise the
    opening-reading rule on every counter rather than just the one.
    """
    values = last if isinstance(last, tuple) else (last, 0, 0, 0)
    return {
        "input_tokens": values[0],
        "cache_write_input_tokens": values[1],
        "cached_input_tokens": values[2],
        "output_tokens": values[3],
    }


def _token(
    input_tokens: int,
    *,
    last: object = _OMIT,
    timestamp: str = "2026-08-15T00:00:01Z",
    cache_create: int = 0,
    cache_read: int = 0,
    output: int = 0,
    reasoning: int = 0,
) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cache_write_input_tokens": cache_create,
                    "cached_input_tokens": cache_read,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": input_tokens + output,
                },
                # Real Codex carries this on 100% of ledger records. Fixtures
                # that omit it exercise the cumulative fallback instead, so the
                # default mirrors the wire: `last` equals the running total,
                # which is what a session's FIRST reading actually reports.
                **({} if last is _OMIT else {"last_token_usage": _last_map(last)}),
            },
        },
    }


def _write_rollout(root: Path, name: str, records: list[dict], *, partial: bytes = b"") -> Path:
    path = root / "2026" / "08" / "14" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fp:
        for record in records:
            fp.write(json.dumps(record).encode("utf-8") + b"\n")
        fp.write(partial)
    return path


def _write_opencode_database(root: Path, records: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "opencode.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE message (data TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO message (data) VALUES (?)",
            [(json.dumps(record),) for record in records],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _opencode_message(
    message_id: str = "message-a",
    *,
    model: str = "gpt-5",
    completed: int = 1_755_216_001_000,
    input_tokens: int = 120,
    output: int = 30,
    cache_read: int = 20,
    cache_create: int = 10,
    role: str = "assistant",
) -> dict:
    return {
        "id": message_id,
        "role": role,
        "modelID": model,
        "time": {"completed": completed},
        "finish": "stop",
        "tokens": {
            "input": input_tokens,
            "output": output,
            "reasoning": 12,
            "cache": {"read": cache_read, "write": cache_create},
        },
    }


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "config" / "host-tokens.json"
    monkeypatch.setattr(hu, "CACHE_PATH", cache)
    return cache


@pytest.fixture
def isolated_adapter_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    grok_cache = tmp_path / "config" / "grok-host-tokens.json"
    opencode_cache = tmp_path / "config" / "opencode-host-tokens.json"
    monkeypatch.setattr(hu, "GROK_CACHE_PATH", grok_cache)
    monkeypatch.setattr(hu, "OPENCODE_CACHE_PATH", opencode_cache)
    return grok_cache, opencode_cache


class TestHostFamily:
    @pytest.mark.parametrize(
        ("model", "family"),
        [
            ("claude-opus-4-7", "claude"),
            ("CLAUDE-HAIKU-4-5", "claude"),
            ("gpt-5", "codex"),
            ("o1", "codex"),
            ("o3", "codex"),
            ("o4-mini", "codex"),
            ("gpt-5-codex", "codex"),
            ("not-codex-helper", "other"),
            ("grok-4", "grok"),
            ("openrouter/unknown", "other"),
        ],
    )
    def test_canonical_classifier(self, model: str, family: str) -> None:
        assert hu.host_family(model) == family


class TestCodexFixture:
    def test_per_turn_increments_land_on_the_day_they_were_spent(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """A session that spans midnight is split, not stamped on one day.

        This replaces `test_uses_final_cumulative_token_count_and_utc_day`,
        which pinned the OPPOSITE: the whole cumulative total attributed to the
        UTC day of the file's LAST record. That was faithful to a reader which
        kept only the final `total_token_usage`, and it is what made a day
        bucket mean "the lifetime of every session that last touched this
        machine that day" rather than "tokens spent that day".

        The fixture is one session with a ledger on each of two days. The sum
        is unchanged (200/20/40/60) because the same work is being counted;
        only its attribution moves. 8 of 746 rollouts on a real corpus span
        more than one UTC day.
        """
        root = tmp_path / "sessions"
        shutil.copytree(FIXTURES, root)

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.reason is None
        assert result.hosts == {
            "codex": {
                "2026-08-14": {
                    "input": 100,
                    "cache_create": 10,
                    "cache_read": 20,
                    "output": 30,
                },
                "2026-08-15": {
                    "input": 100,
                    "cache_create": 10,
                    "cache_read": 20,
                    "output": 30,
                },
            }
        }
        # The window total is preserved; only its distribution changed.
        totals = result.hosts["codex"]
        assert sum(day["input"] for day in totals.values()) == 200
        assert sum(day["output"] for day in totals.values()) == 60
        assert isolated_cache.exists()
        assert isolated_cache.stat().st_mode & 0o777 == 0o600
        assert str(root) not in isolated_cache.read_text(encoding="utf-8")
        # This must remain a separate cache namespace from Claude sessions.
        assert isolated_cache != tu.CACHE_PATH

    def test_reasoning_is_not_added_to_output_twice(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(
            root, "rollout-reasoning.jsonl", [_context(), _token(1, output=9, reasoning=7)]
        )

        result = hu.read_codex_usage(root)

        assert result.hosts["codex"]["2026-08-15"]["output"] == 9

    def test_model_context_after_token_is_not_retroactively_used(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-context-order.jsonl",
            [_context("gpt-5"), _token(10), _context("grok-4")],
        )

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert set(result.hosts) == {"codex"}

    def test_missing_model_before_token_is_incomplete(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """A ledger we saw and could attribute to NOTHING still refuses the
        store. Silently dropping it would under-report real usage — the
        opposite failure from the tolerated no-ledger shapes below."""
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-no-context.jsonl", [_token(10)])

        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="unsupported")


class TestOrdinaryCodexShapesAreNotRefused:
    """Refusing a routine shape costs the WHOLE store, not one file.

    Measured on a real 452-rollout machine before this fix: 167 rollouts (37%)
    failed, the scan died on the first one in 5ms, and Track 19A's all-or-
    nothing caller therefore published nothing at all while `mm status` sat at
    `degraded (codex unsupported)` — permanently, since `unsupported` is
    classified as never-retry. These pins are the regression guard.
    """

    def _null_info_token(self, timestamp: str = "2026-08-15T00:00:00Z") -> dict:
        """Codex's start-of-turn marker: a token_count with `info: null`."""
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": None},
        }

    def test_null_info_token_count_is_skipped_not_fatal(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """33% of rollouts on a live machine carried one of these."""
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-null-info.jsonl",
            [_context(), self._null_info_token(), _token(42, output=7)],
        )

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 42
        assert result.hosts["codex"]["2026-08-15"]["output"] == 7

    def test_absent_info_key_is_treated_like_null(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        marker = {
            "timestamp": "2026-08-15T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "token_count"},
        }
        _write_rollout(root, "rollout-no-info-key.jsonl", [_context(), marker, _token(5)])

        assert hu.read_codex_usage(root).hosts["codex"]["2026-08-15"]["input"] == 5

    def test_present_but_malformed_info_is_still_fatal(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """The discriminator is empty-marker vs broken-ledger. Widening the
        skip to "any info I can't parse" would let real usage vanish."""
        root = tmp_path / "sessions"
        broken = {
            "timestamp": "2026-08-15T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": "not-a-dict"},
        }
        _write_rollout(root, "rollout-broken-info.jsonl", [_context(), broken])

        assert hu.read_codex_usage(root).reason == "unsupported"

    def test_ledger_before_first_turn_context_is_skipped(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Live sessions open with token_counts before the first turn_context.
        Totals are cumulative, so the later attributable record restates them
        — the early prefix is droppable, not fatal."""
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-early-ledger.jsonl",
            [_token(10), _token(20), _context("gpt-5"), _token(30, output=4)],
        )

        result = hu.read_codex_usage(root)

        assert result.complete is True
        # The LAST attributable cumulative total wins — never a sum.
        assert result.hosts["codex"]["2026-08-15"] == {
            "input": 30,
            "cache_create": 0,
            "cache_read": 0,
            "output": 4,
        }

    def test_rollout_with_no_ledger_contributes_nothing(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """An abandoned session has no tokens. That is a fact about the file,
        not a reason to refuse every other rollout in the store."""
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-abandoned.jsonl", [_context()])
        _write_rollout(root, "rollout-real.jsonl", [_context(), _token(99)])

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 99

    def test_one_abandoned_rollout_does_not_hide_the_whole_store(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """The exact production failure: a single unreadable file anywhere in
        the tree zeroed fleet host analytics. Sorted discovery put the
        offender first, so `_scan_codex_root` died before reading anything."""
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-aaa-abandoned.jsonl", [_context()])
        for i in range(5):
            _write_rollout(root, f"rollout-zzz-{i}.jsonl", [_context(), _token(10)])

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 50

    def test_marker_only_rollout_is_no_ledger_not_a_refusal(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """An aborted turn leaves markers and nothing else.

        This is the mutation guard for the whole fix: moving
        ``saw_usage_ledger = True`` one line up (above the `_carries_usage`
        gate) reintroduces the 37%-refusal outage, and every other tolerance
        test still passes because they all pair a marker with a real ledger.
        """
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-aaa-marker-only.jsonl",
            [_context(), self._null_info_token(), self._null_info_token()],
        )
        # No turn_context at all: still a marker, still not a ledger.
        _write_rollout(root, "rollout-bbb-bare-marker.jsonl", [self._null_info_token()])
        _write_rollout(root, "rollout-zzz-real.jsonl", [_context(), _token(11)])

        result = hu.read_codex_usage(root)

        assert result.complete is True, result
        assert result.hosts["codex"]["2026-08-15"]["input"] == 11
        # All three are cached — the two marker-only files as no-ledger entries
        # so they stop costing a full re-parse on every scan.
        cached = json.loads(isolated_cache.read_text(encoding="utf-8"))["files"]
        assert len(cached) == 3
        no_ledger = [e for e in cached.values() if e.get("no_ledger")]
        assert len(no_ledger) == 2
        for entry in no_ledger:
            assert "day" not in entry and "model" not in entry and "usage" not in entry

    @pytest.mark.parametrize("info", [{}, {"total_token_usage": {}}])
    def test_empty_info_is_a_broken_ledger_not_a_marker(
        self, isolated_cache: Path, tmp_path: Path, info: dict
    ) -> None:
        """Pins the boundary next to the tolerated null marker: `info: {}` is
        a dict, so it is a ledger we saw and could not parse — fatal. If Codex
        ever emits `{}` as a start-of-turn marker, THIS is the test that has
        to change, deliberately."""
        root = tmp_path / "sessions"
        record = {
            "timestamp": "2026-08-15T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": info},
        }
        _write_rollout(root, "rollout-empty-info.jsonl", [_context(), record])

        assert hu.read_codex_usage(root).reason == "unsupported"

    def test_broken_ledger_before_any_turn_context_is_still_fatal(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """The model-attribution `continue` runs before `_terminal_from_record`,
        so a non-dict `info` arriving first used to slip past the refusal the
        docstring promised. Refusal now happens in `_carries_usage`."""
        root = tmp_path / "sessions"
        broken = {
            "timestamp": "2026-08-15T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": "not-a-dict"},
        }
        _write_rollout(root, "rollout-early-broken.jsonl", [broken, _context(), _token(10)])

        assert hu.read_codex_usage(root).reason == "unsupported"

    def test_no_ledger_rollout_caches_identity_only_and_hits_next_scan(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ledger-less rollout is cached as identity + fingerprint ONLY.

        It carries no day/model/usage, so `_aggregate` can never fabricate a
        family bucket from it — and an unchanged one is a cache hit rather
        than a full re-parse, which is what keeps a corpus converging.
        """
        root = tmp_path / "sessions"
        abandoned = _write_rollout(root, "rollout-abandoned.jsonl", [_context()])
        _write_rollout(root, "rollout-real.jsonl", [_context(), _token(1)])

        first = hu.read_codex_usage(root)
        assert first.complete is True
        assert set(first.hosts) == {"codex"}, "a no-ledger file must not create a bucket"

        entry = json.loads(isolated_cache.read_text(encoding="utf-8"))["files"][
            hu._cache_key(abandoned)
        ]
        assert entry["no_ledger"] is True
        assert "day" not in entry and "model" not in entry and "usage" not in entry

        # Unchanged on the next scan → cache hit, not a re-parse.
        monkeypatch.setattr(
            hu, "_read_full_rollout", lambda *a, **k: pytest.fail("no-ledger file was re-parsed")
        )
        second = hu.read_codex_usage(root)
        assert second.complete is True
        assert second.hosts == first.hosts

    def test_hand_edited_no_ledger_entry_cannot_smuggle_totals(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """`_aggregate` skips on the flag, so validation must strip anything
        riding along behind it rather than trusting the pair."""
        root = tmp_path / "sessions"
        abandoned = _write_rollout(root, "rollout-abandoned.jsonl", [_context()])
        assert hu.read_codex_usage(root).complete is True

        cache = json.loads(isolated_cache.read_text(encoding="utf-8"))
        cache["files"][hu._cache_key(abandoned)].update(
            {
                "day": "2026-08-15",
                "model": "gpt-5",
                "usage": {"input": 10**9, "cache_create": 0, "cache_read": 0, "output": 0},
            }
        )
        isolated_cache.write_text(json.dumps(cache), encoding="utf-8")

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts == {}, "smuggled totals must not reach the aggregate"


class TestCacheLifecycle:
    def test_unchanged_rollout_uses_cached_terminal(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-cached.jsonl", [_context(), _token(100)])
        first = hu.read_codex_usage(root)
        assert first.complete is True

        def should_not_reread(*args: object, **kwargs: object) -> object:
            raise AssertionError("unchanged rollout should be a cache hit")

        monkeypatch.setattr(hu, "_read_full_rollout", should_not_reread)
        second = hu.read_codex_usage(root)

        assert second == first

    def test_same_size_same_mtime_rewrite_replaces_terminal_total(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-rewrite.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).hosts["codex"]["2026-08-15"]["input"] == 200
        before = rollout.stat()

        _write_rollout(root, "rollout-rewrite.jsonl", [_context(), _token(400)])
        os.utime(rollout, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert rollout.stat().st_size == before.st_size
        assert rollout.stat().st_mtime_ns == before.st_mtime_ns

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 400

    def test_verified_append_resumes_from_cached_complete_line_offset(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-append.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).hosts["codex"]["2026-08-15"]["input"] == 200

        with rollout.open("ab") as fp:
            fp.write(json.dumps(_token(400)).encode("utf-8") + b"\n")

        def full_parse_would_violate_resume(*args: object, **kwargs: object) -> object:
            raise AssertionError("verified append should resume from the cache offset")

        monkeypatch.setattr(hu, "_read_full_rollout", full_parse_would_violate_resume)
        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 400

    def test_changed_append_provenance_falls_back_to_full_parse(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-provenance.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).complete is True

        # Keep the original byte length, then append. The cached tail and head
        # no longer prove this is an append, so it must receive a full parse.
        _write_rollout(root, "rollout-provenance.jsonl", [_context(), _token(300)])
        with rollout.open("ab") as fp:
            fp.write(json.dumps(_token(400)).encode("utf-8") + b"\n")

        full_parse = hu._read_full_rollout
        calls = 0

        def record_full_parse(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return full_parse(*args, **kwargs)

        monkeypatch.setattr(hu, "_read_full_rollout", record_full_parse)
        result = hu.read_codex_usage(root)

        assert calls == 1
        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 400

    def test_mutation_during_full_parse_preserves_existing_cache(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-race-full.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).complete is True
        cache_before = isolated_cache.read_bytes()
        _write_rollout(root, "rollout-race-full.jsonl", [_context(), _token(300)])

        regular_stat = hu._regular_stat
        calls = 0

        def mutate_after_full_parse(path: Path) -> os.stat_result:
            nonlocal calls
            calls += 1
            if calls == 2:
                _write_rollout(root, "rollout-race-full.jsonl", [_context(), _token(400)])
            return regular_stat(path)

        monkeypatch.setattr(hu, "_regular_stat", mutate_after_full_parse)
        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="stale")
        assert isolated_cache.read_bytes() == cache_before

    def test_mutation_after_append_proof_preserves_existing_cache(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-race-resume.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).complete is True
        cache_before = isolated_cache.read_bytes()
        with rollout.open("ab") as fp:
            fp.write(json.dumps(_token(300)).encode("utf-8") + b"\n")

        regular_stat = hu._regular_stat
        calls = 0

        def mutate_after_append_proof(path: Path) -> os.stat_result:
            nonlocal calls
            calls += 1
            if calls == 2:
                with rollout.open("ab") as fp:
                    fp.write(json.dumps(_token(400)).encode("utf-8") + b"\n")
            return regular_stat(path)

        monkeypatch.setattr(hu, "_regular_stat", mutate_after_append_proof)
        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="stale")
        assert isolated_cache.read_bytes() == cache_before

    def test_deadline_during_cache_fingerprinting_preserves_existing_cache(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-fingerprint-deadline.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).complete is True
        cache_before = isolated_cache.read_bytes()

        def expire_fingerprint(*args: object, **kwargs: object) -> object:
            raise hu._ReadFailure("deadline")

        monkeypatch.setattr(hu, "_fingerprint", expire_fingerprint)
        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="deadline")
        assert isolated_cache.read_bytes() == cache_before

    def test_2am_regression_partial_scan_never_replaces_or_prunes_cache(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Both hazards in this pin's name still hold; byte-equality does not.

        A partial pass must not REPLACE the map (that drops every entry it
        never reached) and must not PRUNE on an incomplete view of the
        directory (a file it never listed is not a file that was deleted).
        Merging preserves both.

        What a partial pass may now do is KEEP the per-file progress it
        verified. Refusing to was safe but non-convergent: on a 452-rollout
        machine every bounded scan re-parsed the same prefix, expired in the
        same place, and discarded it — six consecutive scans, zero bytes
        cached, so the corpus could never bootstrap. Each staged entry is a
        complete, fingerprinted parse of one stable file and is revalidated
        against dev/ino/size/mtime plus a head+tail digest before it is ever
        trusted, so keeping it cannot make a later total wrong.
        """
        root = tmp_path / "sessions"
        rollout_a = _write_rollout(root, "rollout-a.jsonl", [_context(), _token(200)])
        rollout_b = _write_rollout(root, "rollout-b.jsonl", [_context(), _token(100)])
        first = hu.read_codex_usage(root)
        assert first.hosts["codex"]["2026-08-15"]["input"] == 300
        keys_before = set(json.loads(isolated_cache.read_text(encoding="utf-8"))["files"])
        key_a, key_b = hu._cache_key(rollout_a), hu._cache_key(rollout_b)

        _write_rollout(root, "rollout-a.jsonl", [_context(), _token(400)])
        rollout_b.unlink()
        _write_rollout(root, "rollout-c.jsonl", [_context()], partial=b'{"type":"event_msg"')

        incomplete = hu.read_codex_usage(root)

        assert incomplete == hu.HostUsageResult({}, complete=False, reason="partial")
        files_after = json.loads(isolated_cache.read_text(encoding="utf-8"))["files"]
        # NOT replaced, NOT pruned: b was unlinked and never re-listed, and its
        # entry survives this incomplete pass untouched.
        assert set(files_after) == keys_before
        assert files_after[key_b]["states"][0][1][0] == 100
        # ...but a's verified re-parse is kept, which is the convergence half.
        assert files_after[key_a]["states"][0][1][0] == 400
        # A surviving entry for a deleted file is inert: aggregation walks the
        # DISK, never the cache, so b cannot contribute to any total.
        assert incomplete.hosts == {}

        (root / "2026" / "08" / "14" / "rollout-c.jsonl").unlink()
        stable = hu.read_codex_usage(root)
        assert stable.complete is True
        assert stable.hosts["codex"]["2026-08-15"]["input"] == 400
        assert rollout_a.exists()
        # Pruning is the COMPLETE pass's job, and it happened here.
        assert set(json.loads(isolated_cache.read_text(encoding="utf-8"))["files"]) == {key_a}

    def test_cold_corpus_converges_across_bounded_scans(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corpus too large for one bounded scan must still bootstrap.

        Before partial commits, this loop ran forever: each scan parsed the
        same prefix, expired, and threw it away. The acceptance bar is not
        merely "it finishes" — it is that the converged result equals what a
        single unbounded scan produces.
        """

        class _FakeTime:
            now = 1_000.0

            def monotonic(self) -> float:
                return self.now

        clock = _FakeTime()
        monkeypatch.setattr(hu, "time", clock)

        root = tmp_path / "sessions"
        for i in range(10):
            _write_rollout(root, f"rollout-{i:02d}.jsonl", [_context(), _token(10 + i)])

        real_full_read = hu._read_full_rollout
        parses: list[int] = [0]

        def timed_full_read(path: Path, before: object, deadline: float):
            parses[0] += 1
            clock.now += 0.1  # each cold parse costs 100ms of fake wall clock
            return real_full_read(path, before, deadline)

        monkeypatch.setattr(hu, "_read_full_rollout", timed_full_read)

        cached_entries, attempts, result = [], 0, None
        while attempts < 20:
            attempts += 1
            result = hu.read_codex_usage(root, deadline=clock.now + 0.25)
            cached_entries.append(len(json.loads(isolated_cache.read_text())["files"]))
            if result.complete:
                break

        assert result is not None and result.complete is True, "cold corpus never converged"
        assert attempts > 1, "budget too generous — this pin proves nothing in one pass"
        assert cached_entries == sorted(cached_entries), (
            f"cache must never lose ground between bounded scans: {cached_entries}"
        )
        # Not "exactly 10": the file in flight when the budget expires has
        # already been read, but `_fingerprint` then refuses to spend more I/O,
        # so it is re-parsed next scan. That waste is bounded at ONE file per
        # scan and is the deliberate price of checking the deadline everywhere.
        # The bound is what matters — the bug was re-parsing the whole prefix
        # every scan, which would put this in the 10*attempts range.
        assert parses[0] <= 10 + attempts, (
            f"at most one in-flight parse may be wasted per scan: "
            f"{parses[0]} parses over {attempts} scans of 10 files"
        )
        assert parses[0] < 10 * attempts, "the prefix is being re-parsed every scan"
        # The bar: converged-incremental is identical to one unbounded scan.
        assert result.hosts["codex"]["2026-08-15"]["input"] == sum(10 + i for i in range(10))

    def test_complete_scan_that_overran_its_budget_still_commits_the_cache(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one commit path with no other pin: the scan finished, then the
        budget expired. Refusing to publish is unchanged; throwing the work
        away as well is what non-convergence was made of."""
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-a.jsonl", [_context(), _token(200)])

        real_scan = hu._scan_codex_root
        blown = {"on": False}
        real_expired = hu._expired

        def scan_then_blow_the_budget(source_root, cached_files, deadline):
            outcome = real_scan(source_root, cached_files, deadline)
            blown["on"] = True
            return outcome

        monkeypatch.setattr(hu, "_scan_codex_root", scan_then_blow_the_budget)
        monkeypatch.setattr(hu, "_expired", lambda d: blown["on"] or real_expired(d))

        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="deadline")
        files = json.loads(isolated_cache.read_text(encoding="utf-8"))["files"]
        assert len(files) == 1, "a complete-but-overbudget scan must still keep its work"

        # ...and the work counts: the next in-budget scan re-parses nothing.
        monkeypatch.setattr(hu, "_expired", real_expired)
        monkeypatch.setattr(hu, "_scan_codex_root", real_scan)
        monkeypatch.setattr(
            hu, "_read_full_rollout", lambda *a, **k: pytest.fail("prefix was re-parsed")
        )
        assert hu.read_codex_usage(root).hosts["codex"]["2026-08-15"]["input"] == 200

    def test_uncacheable_rollouts_do_not_block_convergence(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ledger-less rollouts are cached identity-only, so their cost is paid
        ONCE rather than on every scan. Sorted first, they are exactly where an
        uncached re-parse would starve the budget forever — remove the
        `no_ledger` entry and this loop never converges."""

        class _FakeTime:
            now = 1_000.0

            def monotonic(self) -> float:
                return self.now

        clock = _FakeTime()
        monkeypatch.setattr(hu, "time", clock)

        root = tmp_path / "sessions"
        for i in range(4):  # sort AHEAD of the real ones
            _write_rollout(root, f"rollout-aaa-{i:02d}.jsonl", [_context()])
        for i in range(6):
            _write_rollout(root, f"rollout-zzz-{i:02d}.jsonl", [_context(), _token(10 + i)])

        real_full_read = hu._read_full_rollout

        def timed_full_read(path, before, deadline):
            clock.now += 0.1
            return real_full_read(path, before, deadline)

        monkeypatch.setattr(hu, "_read_full_rollout", timed_full_read)

        attempts, result = 0, None
        while attempts < 20:
            attempts += 1
            result = hu.read_codex_usage(root, deadline=clock.now + 0.25)
            if result.complete:
                break

        assert result is not None and result.complete is True, (
            "uncacheable rollouts starved the budget: the corpus never converged"
        )
        assert result.hosts["codex"]["2026-08-15"]["input"] == sum(10 + i for i in range(6))

    def test_warm_host_cache_inline_actually_populates_the_cache(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Every caller test mocks this away, so its one piece of real logic —
        turning a relative budget into an ABSOLUTE monotonic deadline — is
        otherwise unpinned. `deadline=budget_s` would make every warm an
        instant no-op and every mocked test would still pass."""
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-warm.jsonl", [_context(), _token(77)])

        result = hu.warm_host_cache_inline(root)

        assert result.complete is True, result
        assert result.hosts["codex"]["2026-08-15"]["input"] == 77
        assert json.loads(isolated_cache.read_text(encoding="utf-8"))["files"]

    def test_non_canonical_day_in_a_cached_entry_is_rejected(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """A cached `day` becomes a KEY in a synced row since Track 19A.
        `fromisoformat` alone would accept a datetime carrying this machine's
        UTC offset — a per-machine identifier the row must never carry."""
        root = tmp_path / "sessions"
        path = _write_rollout(root, "rollout-a.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).complete is True

        cache = json.loads(isolated_cache.read_text(encoding="utf-8"))
        cache["files"][hu._cache_key(path)]["day"] = "2026-08-15T23:59:59-07:00"
        isolated_cache.write_text(json.dumps(cache), encoding="utf-8")

        # Rejected as a cache hit -> re-parsed -> canonical day restored.
        assert hu.read_codex_usage(root).hosts["codex"] == {
            "2026-08-15": {"input": 200, "cache_create": 0, "cache_read": 0, "output": 0}
        }

    def test_deleted_rollout_is_pruned_after_complete_discovery(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-a.jsonl", [_context(), _token(200)])
        rollout_b = _write_rollout(root, "rollout-b.jsonl", [_context("grok-4"), _token(100)])
        assert set(hu.read_codex_usage(root).hosts) == {"codex", "grok"}

        rollout_b.unlink()
        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert set(result.hosts) == {"codex"}

    def test_partial_line_preserves_existing_cache(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-stable.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).complete is True
        cache_before = isolated_cache.read_bytes()

        _write_rollout(root, "rollout-stable.jsonl", [_context(), _token(300)], partial=b"{")
        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="partial")
        assert isolated_cache.read_bytes() == cache_before

    def test_lock_contention_returns_incomplete_without_mutating_cache(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-locked.jsonl", [_context(), _token(200)])
        assert hu.read_codex_usage(root).complete is True
        cache_before = isolated_cache.read_bytes()
        fd = os.open(str(isolated_cache), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            result = hu.read_codex_usage(root, deadline=time.monotonic() + 1.0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert result == hu.HostUsageResult({}, complete=False, reason="locked")
        assert isolated_cache.read_bytes() == cache_before


class TestFailureAndTraversalContracts:
    def test_missing_root_is_complete_empty_scan(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        result = hu.read_codex_usage(tmp_path / "missing")

        assert result == hu.HostUsageResult({}, complete=True)

    def test_expired_deadline_does_not_create_cache(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        result = hu.read_codex_usage(tmp_path / "sessions", deadline=time.monotonic() - 0.001)

        assert result == hu.HostUsageResult({}, complete=False, reason="deadline")
        assert not isolated_cache.exists()

    def test_malformed_and_unsupported_token_events_are_incomplete(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        malformed = root / "2026" / "08" / "14" / "rollout-malformed.jsonl"
        malformed.parent.mkdir(parents=True, exist_ok=True)
        malformed.write_text("not json\n", encoding="utf-8")
        assert hu.read_codex_usage(root).reason == "malformed"

        malformed.unlink()
        _write_rollout(root, "rollout-unsupported.jsonl", [_context(), _token(10)])
        unsupported_path = root / "2026" / "08" / "14" / "rollout-unsupported.jsonl"
        unsupported = json.loads(unsupported_path.read_text(encoding="utf-8").splitlines()[1])
        del unsupported["payload"]["info"]["total_token_usage"]["input_tokens"]
        unsupported_path.write_text(
            json.dumps(_context()) + "\n" + json.dumps(unsupported) + "\n", encoding="utf-8"
        )
        assert hu.read_codex_usage(root).reason == "unsupported"

    def test_foreign_and_symlink_files_are_not_rollouts(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        day = root / "2026" / "08" / "14"
        day.mkdir(parents=True)
        (day / "notes.jsonl").write_text("not json\n", encoding="utf-8")
        target = tmp_path / "target.jsonl"
        target.write_text("not json\n", encoding="utf-8")
        (day / "rollout-link.jsonl").symlink_to(target)

        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=True)


def _write_grok_session(
    root: Path,
    *,
    workspace: str = "workspace",
    session: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    lines: list[str] | None = None,
) -> Path:
    session_dir = root / workspace / session
    session_dir.mkdir(parents=True)
    path = session_dir / "updates.jsonl"
    path.write_text("".join(lines or []), encoding="utf-8")
    return path


def _grok_turn_usage_less(
    *,
    ts: int = 1786731043,
    prompt_id: str = "33333333-3333-3333-3333-333333333333",
    stop: str = "cancelled",
) -> str:
    return (
        json.dumps(
            {
                "method": "session/update",
                "timestamp": ts,
                "params": {
                    "update": {
                        "prompt_id": prompt_id,
                        "sessionUpdate": "turn_completed",
                        "stop_reason": stop,
                    }
                },
            }
        )
        + "\n"
    )


def _grok_turn(
    *,
    ts: int = 1786731043,
    prompt_id: str = "11111111-1111-1111-1111-111111111111",
    stop: str = "end_turn",
    input_tokens: int = 10,
    output: int = 6,
    reasoning: int = 2,
    cache_read: int = 3,
    cache_create: int = 1,
    model: str = "grok-4",
    extra_update: dict | None = None,
) -> str:
    usage = {
        "inputTokens": input_tokens,
        "outputTokens": output,
        "reasoningTokens": reasoning,
        "cachedReadTokens": cache_read,
        "cacheCreationTokens": cache_create,
        "totalTokens": input_tokens + output,
        "numTurns": 1,
        "modelUsage": {
            model: {
                "inputTokens": input_tokens,
                "outputTokens": output,
                "reasoningTokens": reasoning,
                "cachedReadTokens": cache_read,
                "cacheCreationTokens": cache_create,
                "totalTokens": input_tokens + output,
            }
        },
    }
    update = {
        "prompt_id": prompt_id,
        "sessionUpdate": "turn_completed",
        "stop_reason": stop,
        "usage": usage,
    }
    if extra_update:
        update.update(extra_update)
    return (
        json.dumps(
            {
                "method": "session/update",
                "timestamp": ts,
                "params": {"update": update},
            }
        )
        + "\n"
    )


class TestGrokUsage:
    def test_closed_default_does_not_open_the_store(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path, monkeypatch
    ) -> None:
        root = tmp_path / "sessions"
        shutil.copytree(FIXTURES / "grok" / "workspace", root / "workspace")
        opened: list[Path] = []
        real_open = Path.open

        def spy(self, *args, **kwargs):
            opened.append(self)
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", spy)

        result = hu.read_grok_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="no_metadata_ledger")
        assert opened == []
        assert not isolated_adapter_caches[0].exists()

    def test_census_fixtures_parse_as_the_contract_describes(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T4: the extra Grok 1.0.5 census fixtures stay loadable."""
        cases = {
            "usage-less": (True, {}),
            "cancelled-with-usage": (True, 10),
            "incomplete-usage": (True, 8),
            "no-ledger": (True, {}),
        }
        for name, (complete, expected) in cases.items():
            root = tmp_path / name
            shutil.copytree(FIXTURES / "grok" / name, root)
            result = hu.read_grok_usage(root, consented=True)
            assert result.complete is complete, name
            if expected == {}:
                assert result.hosts == {}, name
            else:
                assert result.hosts["grok"]["2026-08-14"]["input"] == expected, name

    def test_fixture_turn_is_a_per_prompt_total_with_reasoning_inside_output(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        shutil.copytree(FIXTURES / "grok" / "workspace", root / "workspace")

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts == {
            "grok": {
                "2026-08-14": {
                    "input": 10,
                    "cache_create": 1,
                    "cache_read": 3,
                    "output": 6,
                }
            }
        }
        cache = json.loads(isolated_adapter_caches[0].read_text(encoding="utf-8"))
        dumped = json.dumps(cache)
        assert "11111111-1111-1111-1111-111111111111" not in dumped
        assert "prompt_id" not in dumped
        assert str(root) not in dumped

    def test_two_turns_are_summed_not_replaced(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_grok_session(
            root,
            lines=[
                _grok_turn(input_tokens=100, output=10, reasoning=3, cache_read=0, cache_create=0),
                _grok_turn(
                    ts=1786817443,
                    prompt_id="22222222-2222-2222-2222-222222222222",
                    input_tokens=20,
                    output=4,
                    reasoning=1,
                    cache_read=0,
                    cache_create=0,
                ),
            ],
        )

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        days = result.hosts["grok"]
        assert days["2026-08-14"]["input"] == 100
        assert days["2026-08-15"]["input"] == 20
        assert days["2026-08-14"]["output"] == 10
        assert days["2026-08-15"]["output"] == 4

    def test_replayed_equal_prompt_counts_once(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        line = _grok_turn()
        _write_grok_session(root, lines=[line, line])

        result = hu.read_grok_usage(root, consented=True)

        assert result.hosts["grok"]["2026-08-14"]["input"] == 10

    def test_conflicting_duplicate_prompt_refuses(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_grok_session(
            root,
            lines=[
                _grok_turn(input_tokens=10, output=6, reasoning=2),
                _grok_turn(input_tokens=99, output=6, reasoning=2),
            ],
        )

        result = hu.read_grok_usage(root, consented=True)

        assert result == hu.HostUsageResult({}, complete=False, reason="unsupported")

    def test_multi_model_turn_counts_each_model_once(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        record = json.loads(_grok_turn())
        usage = record["params"]["update"]["usage"]
        usage["modelUsage"]["gpt-5"] = {
            "inputTokens": 4,
            "outputTokens": 2,
            "reasoningTokens": 0,
            "cachedReadTokens": 0,
            "cacheCreationTokens": 0,
            "totalTokens": 6,
        }
        _write_grok_session(root, lines=[json.dumps(record) + "\n"])

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts["grok"]["2026-08-14"]["input"] == 10
        assert result.hosts["codex"]["2026-08-14"]["input"] == 4

    def test_single_then_multi_model_replay_does_not_double_count(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        first = _grok_turn()
        replay = json.loads(first)
        replay["params"]["update"]["usage"]["modelUsage"]["gpt-5"] = {
            "inputTokens": 4,
            "outputTokens": 2,
            "reasoningTokens": 0,
            "cachedReadTokens": 0,
            "cacheCreationTokens": 0,
            "totalTokens": 6,
        }
        _write_grok_session(root, lines=[first, json.dumps(replay) + "\n"])

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts["grok"]["2026-08-14"]["input"] == 10
        assert result.hosts["codex"]["2026-08-14"]["input"] == 4

    def test_same_session_name_in_two_workspaces_does_not_refuse(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _write_grok_session(root, workspace="proj-a", session=session, lines=[_grok_turn()])
        _write_grok_session(
            root,
            workspace="proj-b",
            session=session,
            lines=[_grok_turn(input_tokens=7, output=3, reasoning=1, cache_read=0, cache_create=0)],
        )

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts["grok"]["2026-08-14"]["input"] == 17

    def test_extra_non_content_key_on_terminal_is_unsupported(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_grok_session(root, lines=[_grok_turn(extra_update={"durationMs": 12})])

        result = hu.read_grok_usage(root, consented=True)

        assert result.reason == "unsupported"

    def test_content_bearing_turn_is_ignored(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T2-6: content-bearing turns short-circuit BEFORE the key-set check.
        Load-bearing: the usage-less carve-out must not steal this path."""
        root = tmp_path / "sessions"
        _write_grok_session(
            root,
            lines=[
                _grok_turn(extra_update={"content": "do-not-read"}),
                _grok_turn(prompt_id="22222222-2222-2222-2222-222222222222"),
            ],
        )

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts["grok"]["2026-08-14"]["input"] == 10

    def test_reasoning_above_output_is_unsupported(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_grok_session(root, lines=[_grok_turn(output=2, reasoning=5)])

        result = hu.read_grok_usage(root, consented=True)

        assert result.reason == "unsupported"

    def test_incomplete_final_line_is_partial(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        path = _write_grok_session(root, lines=[_grok_turn()])
        path.write_bytes(path.read_bytes() + b'{"method":"session/update"')

        result = hu.read_grok_usage(root, consented=True)

        assert result.reason == "partial"

    def test_absent_root_and_expired_deadline_preserve_empty_vs_incomplete(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        grok_cache, _ = isolated_adapter_caches
        assert hu.read_grok_usage(tmp_path / "absent", consented=True) == hu.HostUsageResult(
            {}, complete=True
        )
        expired = hu.read_grok_usage(
            tmp_path / "absent", deadline=time.monotonic() - 0.001, consented=True
        )
        assert expired == hu.HostUsageResult({}, complete=False, reason="deadline")
        assert grok_cache.exists()
        assert json.loads(grok_cache.read_text(encoding="utf-8"))["complete_once"] is False

    def test_complete_scan_that_saw_files_sets_complete_once(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        shutil.copytree(FIXTURES / "grok" / "workspace", root / "workspace")

        hu.read_grok_usage(root, consented=True)

        assert hu.grok_completed_once() is True

    def test_empty_ledger_is_completed_zero_and_arms_complete_once(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_grok_session(root, lines=[])

        result = hu.read_grok_usage(root, consented=True)

        assert result == hu.HostUsageResult({}, complete=True)
        assert hu.grok_completed_once() is True

    def test_appended_turn_resumes_and_sums(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        path = _write_grok_session(root, lines=[_grok_turn()])
        first = hu.read_grok_usage(root, consented=True)
        assert first.hosts["grok"]["2026-08-14"]["input"] == 10

        path.write_text(
            path.read_text(encoding="utf-8")
            + _grok_turn(
                ts=1786817443,
                prompt_id="22222222-2222-2222-2222-222222222222",
                input_tokens=20,
                output=4,
                reasoning=1,
                cache_read=0,
                cache_create=0,
            ),
            encoding="utf-8",
        )
        second = hu.read_grok_usage(root, consented=True)

        assert second.complete is True
        assert second.hosts["grok"]["2026-08-14"]["input"] == 10
        assert second.hosts["grok"]["2026-08-15"]["input"] == 20

    def test_cancelled_stop_is_accepted_and_unknown_stop_is_not(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_grok_session(root, lines=[_grok_turn(stop="cancelled")])
        assert hu.read_grok_usage(root, consented=True).complete is True

        other = tmp_path / "other"
        _write_grok_session(other, lines=[_grok_turn(stop="interrupted")])
        assert hu.read_grok_usage(other, consented=True).reason == "unsupported"

    def test_iso_timestamp_is_attributed_to_utc_day(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_grok_session(root, lines=[_grok_turn(ts="2026-08-14T23:00:00Z")])

        result = hu.read_grok_usage(root, consented=True)

        assert result.hosts["grok"]["2026-08-14"]["input"] == 10

    def test_grok_home_env_selects_the_sessions_root(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "custom-grok"
        shutil.copytree(FIXTURES / "grok" / "workspace", home / "sessions" / "workspace")
        monkeypatch.setenv("GROK_HOME", str(home))

        result = hu.read_grok_usage(consented=True)

        assert result.complete is True
        assert result.hosts["grok"]["2026-08-14"]["input"] == 10

    @pytest.mark.parametrize("source_kind", ["file", "symlink"])
    def test_rejects_non_directory_or_symlink_session_roots(
        self,
        source_kind: str,
        isolated_adapter_caches: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "sessions"
        if source_kind == "file":
            root.write_text("transcript sentinel", encoding="utf-8")
        else:
            target = tmp_path / "real-sessions"
            target.mkdir()
            root.symlink_to(target, target_is_directory=True)

        result = hu.read_grok_usage(root, consented=True)

        assert result == hu.HostUsageResult({}, complete=False, reason="unsupported")

    def test_filesystem_error_is_incomplete_without_creating_a_cache(
        self, isolated_adapter_caches: tuple[Path, Path]
    ) -> None:
        class UnreadableRoot:
            def exists(self) -> bool:
                raise OSError("unreadable source")

        result = hu.read_grok_usage(UnreadableRoot(), consented=True)  # type: ignore[arg-type]

        assert result == hu.HostUsageResult({}, complete=False, reason="io_error")

    def test_absent_updates_jsonl_beside_a_healthy_session_completes(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T1-1: a session dir with summary.json and no ledger must not
        zero the scan. Must fail on HEAD (``_is_regular_non_symlink`` used
        to turn FileNotFoundError into ``io_error``)."""
        root = tmp_path / "sessions"
        _write_grok_session(root, session="healthy", lines=[_grok_turn()])
        missing = root / "workspace" / "no-ledger"
        missing.mkdir()
        (missing / "summary.json").write_text("{}", encoding="utf-8")

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts["grok"]["2026-08-14"]["input"] == 10

    def test_not_a_directory_on_speculative_probe_is_skipped(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T1-2: ENOTDIR on the speculative path is absence, not io_error."""
        parent = tmp_path / "file-not-dir"
        parent.write_text("x", encoding="utf-8")
        assert hu._is_regular_non_symlink(parent / "updates.jsonl") is False

    def test_permission_error_on_existing_ledger_is_still_io_error(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path, monkeypatch
    ) -> None:
        """T1-3: the guardrail on the narrowing. A file that exists but
        cannot be stated is a real failure."""
        root = tmp_path / "sessions"
        path = _write_grok_session(root, lines=[_grok_turn()])
        real_lstat = Path.lstat

        def boom(self: Path):
            if self == path:
                raise PermissionError("denied")
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", boom)

        result = hu.read_grok_usage(root, consented=True)

        assert result.reason == "io_error"

    def test_codex_rollout_vanishing_between_iterdir_and_lstat_is_skipped(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """T1-4: the Codex TOCTOU window. A rollout reaped after listing
        used to fail the whole scan; absence is now a skip."""
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-keep.jsonl", [_context(), _token(10)])
        _write_rollout(root, "rollout-gone.jsonl", [_context(), _token(99)])
        real_lstat = Path.lstat

        def maybe_gone(self: Path):
            if self.name == "rollout-gone.jsonl":
                raise FileNotFoundError("reaped")
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", maybe_gone)

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 10

    def test_usage_less_turn_is_skipped_and_sibling_is_counted(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T2-1: must fail on HEAD. The carve-out has to precede the key-set
        check or this record is ``unsupported`` and the sibling is lost."""
        root = tmp_path / "sessions"
        _write_grok_session(
            root,
            lines=[
                _grok_turn_usage_less(),
                _grok_turn(prompt_id="22222222-2222-2222-2222-222222222222"),
            ],
        )

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts["grok"]["2026-08-14"]["input"] == 10

    def test_usage_less_turn_alone_is_completed_empty(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T2-2: the anti-dead-code pin. A carve-out at the usage-handling
        site never runs — this file's only record fails the key-set check
        first. Must fail on HEAD."""
        root = tmp_path / "sessions"
        _write_grok_session(root, lines=[_grok_turn_usage_less()])

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts == {}

    def test_usage_less_then_restated_with_usage_counts_once(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T2-3: same prompt_id, first without usage, then with. No
        divergent-duplicate raise; counted once."""
        prompt = "11111111-1111-1111-1111-111111111111"
        root = tmp_path / "sessions"
        _write_grok_session(
            root,
            lines=[
                _grok_turn_usage_less(prompt_id=prompt),
                _grok_turn(prompt_id=prompt),
            ],
        )

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        assert result.hosts["grok"]["2026-08-14"]["input"] == 10

    def test_usage_less_record_appended_after_cached_parse_resumes(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T2-4: resume path handles a usage-less append."""
        root = tmp_path / "sessions"
        path = _write_grok_session(root, lines=[_grok_turn()])
        first = hu.read_grok_usage(root, consented=True)
        assert first.complete is True

        path.write_text(
            path.read_text(encoding="utf-8") + _grok_turn_usage_less(),
            encoding="utf-8",
        )
        second = hu.read_grok_usage(root, consented=True)

        assert second.complete is True
        assert second.hosts["grok"]["2026-08-14"]["input"] == 10
        cache = json.loads(isolated_adapter_caches[0].read_text(encoding="utf-8"))
        assert cache["usage_less_skipped"] == 1

    def test_usage_present_but_not_a_dict_is_still_unsupported(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T2-5: absence and malformed stay different."""
        root = tmp_path / "sessions"
        _write_grok_session(root, lines=[_grok_turn(extra_update={"usage": "nope"})])

        result = hu.read_grok_usage(root, consented=True)

        assert result.reason == "unsupported"

    def test_usage_less_skip_tally_is_per_record_and_on_the_cache(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """T2-7 / T2-8: tally increments per skipped record; no turn entry."""
        root = tmp_path / "sessions"
        _write_grok_session(
            root,
            lines=[
                _grok_turn_usage_less(prompt_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                _grok_turn_usage_less(
                    prompt_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", stop="end_turn"
                ),
                _grok_turn(),
            ],
        )

        result = hu.read_grok_usage(root, consented=True)

        assert result.complete is True
        cache = json.loads(isolated_adapter_caches[0].read_text(encoding="utf-8"))
        assert cache["usage_less_skipped"] == 2
        dumped = json.dumps(cache)
        assert "prompt_id" not in dumped
        for entry in cache["files"].values():
            assert len(entry["turns"]) == 1
        assert hu.grok_usage_diag()["usage_less_skipped"] == 2

    def test_partial_scan_persists_usage_less_skip_tally_for_diag(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path, monkeypatch
    ) -> None:
        """T2-9: a learned skip remains visible when the next file times out."""

        class _FakeTime:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = _FakeTime()
        monkeypatch.setattr(hu, "time", clock)
        root = tmp_path / "sessions"
        first = _write_grok_session(
            root,
            session="a-usage-less",
            lines=[_grok_turn_usage_less()],
        )
        _write_grok_session(root, session="b-modeled", lines=[_grok_turn()])
        real_full_read = hu._read_full_grok_file

        def timed_full_read(path, workspace, session_id, before, deadline):
            if path == first:
                entry = real_full_read(path, workspace, session_id, before, deadline)
                clock.now = 0.1
                return entry
            clock.now = 0.2
            return real_full_read(path, workspace, session_id, before, deadline)

        monkeypatch.setattr(hu, "_read_full_grok_file", timed_full_read)

        result = hu.read_grok_usage(root, deadline=0.15, consented=True)

        assert result == hu.HostUsageResult({}, complete=False, reason="deadline")
        cache = json.loads(isolated_adapter_caches[0].read_text(encoding="utf-8"))
        assert cache["usage_less_skipped"] == 1
        assert hu.grok_usage_diag()["usage_less_skipped"] == 1

    def test_grok_cold_corpus_converges_across_bounded_scans(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path, monkeypatch
    ) -> None:
        """X-1: Grok bounded-scan convergence. Mirror of the Codex pin.

        Measured floor on the live corpus (T1+T2 applied): 250 ms converges
        in 3 passes; 100 ms in 8; 60 ms wedges permanently (no mid-file
        checkpoint — ``_validated_grok_entry`` requires offset == size).
        This test pins convergence when the budget can cover at least one
        file per pass; a budget below per-file cost cannot progress.
        """

        class _FakeTime:
            now = 1_000.0

            def monotonic(self) -> float:
                return self.now

        clock = _FakeTime()
        monkeypatch.setattr(hu, "time", clock)

        root = tmp_path / "sessions"
        for i in range(8):
            _write_grok_session(
                root,
                session=f"session-{i:02d}",
                lines=[
                    _grok_turn(
                        prompt_id=f"{i:08d}-1111-1111-1111-111111111111",
                        input_tokens=10 + i,
                        output=2,
                        reasoning=0,
                        cache_read=0,
                        cache_create=0,
                    )
                ],
            )

        real_full_read = hu._read_full_grok_file
        parses: list[int] = [0]

        def timed_full_read(path, workspace, session_id, before, deadline):
            parses[0] += 1
            clock.now += 0.1
            return real_full_read(path, workspace, session_id, before, deadline)

        monkeypatch.setattr(hu, "_read_full_grok_file", timed_full_read)

        grok_cache = isolated_adapter_caches[0]
        cached_entries, attempts, result = [], 0, None
        while attempts < 20:
            attempts += 1
            result = hu.read_grok_usage(root, deadline=clock.now + 0.25, consented=True)
            cached_entries.append(len(json.loads(grok_cache.read_text())["files"]))
            if result.complete:
                break

        assert result is not None and result.complete is True, "cold corpus never converged"
        assert attempts > 1, "budget too generous — this pin proves nothing in one pass"
        assert cached_entries == sorted(cached_entries)
        assert parses[0] < 8 * attempts, "the prefix is being re-parsed every scan"
        assert result.hosts["grok"]["2026-08-14"]["input"] == sum(10 + i for i in range(8))

        clock.now = 2_000.0
        wedge_root = tmp_path / "wedge"
        _write_grok_session(wedge_root, lines=[_grok_turn()])
        wedge_entries: list[int] = []
        for _ in range(3):
            wedge = hu.read_grok_usage(wedge_root, deadline=clock.now + 0.05, consented=True)
            assert wedge.complete is False
            assert wedge.reason == "deadline"
            wedge_entries.append(len(json.loads(grok_cache.read_text())["files"]))
            clock.now += 1.0
        assert wedge_entries == [wedge_entries[0]] * len(wedge_entries)


class TestOpenCodeUsage:
    def test_reads_read_only_sqlite_projection_without_message_content(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        assistant = _opencode_message()
        assistant["parts"] = [{"text": "do-not-cache-or-return-this"}]
        in_progress = _opencode_message("message-b")
        del in_progress["time"]["completed"]
        wrapper = _opencode_message("message-c")
        wrapper["tokens"] = {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        del wrapper["finish"]
        _write_opencode_database(root, [assistant, in_progress, wrapper])

        result = hu.read_opencode_usage(root)

        assert result.complete is True
        assert result.hosts == {
            "codex": {
                "2025-08-15": {
                    "input": 120,
                    "cache_create": 10,
                    "cache_read": 20,
                    "output": 42,
                }
            }
        }
        assert result.tokens_by_day["2025-08-15"]["by_model"]["gpt-5"]["input"] == 120
        _, opencode_cache = isolated_adapter_caches
        assert opencode_cache.exists()
        assert opencode_cache.stat().st_mode & 0o777 == 0o600
        assert "do-not-cache-or-return-this" not in opencode_cache.read_text(encoding="utf-8")

    def test_refuses_legacy_message_fixture_without_deserializing_it(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        shutil.copytree(FIXTURES / "opencode" / "legacy", root)

        result = hu.read_opencode_usage(root)

        # Legacy message files are whole-transcript blobs with no metadata-only
        # projection — the same standing-property category as Grok, not a
        # failed read. See `Reason`.
        assert result == hu.HostUsageResult({}, complete=False, reason="no_metadata_ledger")

    def test_migration_schema_drift_and_busy_database_are_incomplete(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        _write_opencode_database(root, [_opencode_message()])
        (root / "storage" / "message").mkdir(parents=True)
        assert hu.read_opencode_usage(root).reason == "migration"

        shutil.rmtree(root / "storage")
        connection = sqlite3.connect(root / "opencode.db")
        try:
            connection.execute("DROP TABLE message")
            connection.execute("CREATE TABLE other (data TEXT NOT NULL)")
            connection.commit()
        finally:
            connection.close()
        assert hu.read_opencode_usage(root).reason == "unsupported"

        shutil.rmtree(root)
        database = _write_opencode_database(root, [_opencode_message()])
        writer = sqlite3.connect(database)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            result = hu.read_opencode_usage(root, deadline=time.monotonic() + 1.0)
        finally:
            writer.rollback()
            writer.close()
        assert result == hu.HostUsageResult({}, complete=False, reason="busy")

    def test_missing_source_is_empty_and_malformed_terminal_is_refused(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        assert hu.read_opencode_usage(root) == hu.HostUsageResult({}, complete=True)

        malformed = _opencode_message()
        malformed["tokens"]["input"] = "120"
        _write_opencode_database(root, [malformed])
        assert hu.read_opencode_usage(root).reason == "unsupported"

    def test_nonzero_terminal_without_known_finish_is_refused(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        recovered = _opencode_message()
        recovered["finish"] = "recovered"
        _write_opencode_database(root, [recovered])

        assert hu.read_opencode_usage(root).reason == "unsupported"

    def test_completed_zero_usage_response_is_refused_but_zero_wrapper_is_ignored(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        zero_response = _opencode_message()
        zero_response["tokens"] = {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        _write_opencode_database(root, [zero_response])

        assert hu.read_opencode_usage(root).reason == "unsupported"

    def test_direct_database_path_and_empty_directory_are_complete(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        database = _write_opencode_database(root, [_opencode_message()])

        direct = hu.read_opencode_usage(database)
        empty = hu.read_opencode_usage(tmp_path / "empty")

        assert direct.hosts["codex"]["2025-08-15"]["input"] == 120
        assert empty == hu.HostUsageResult({}, complete=True)
        assert isolated_adapter_caches[1].exists()

    def test_symlink_root_and_changed_database_are_incomplete_without_a_cache(
        self,
        isolated_adapter_caches: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "real-opencode"
        database = _write_opencode_database(target, [_opencode_message()])
        root = tmp_path / "opencode"
        root.symlink_to(target, target_is_directory=True)
        assert hu.read_opencode_usage(root).reason == "stale"

        before = database.stat()
        regular_stat = hu._regular_stat
        calls = 0

        def mutate_before_final_stat(path: Path) -> os.stat_result:
            nonlocal calls
            calls += 1
            if calls == 2:
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
            return regular_stat(path)

        monkeypatch.setattr(hu, "_regular_stat", mutate_before_final_stat)
        result = hu.read_opencode_usage(target)

        assert result == hu.HostUsageResult({}, complete=False, reason="stale")
        assert not isolated_adapter_caches[1].exists()

    def test_malformed_json_and_duplicate_terminal_ids_are_incomplete(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        malformed_root = tmp_path / "malformed"
        malformed_database = _write_opencode_database(malformed_root, [])
        connection = sqlite3.connect(malformed_database)
        try:
            connection.execute("INSERT INTO message (data) VALUES (?)", ('{"id":',))
            connection.commit()
        finally:
            connection.close()
        assert hu.read_opencode_usage(malformed_root).reason == "malformed"

        duplicate_root = tmp_path / "duplicate"
        _write_opencode_database(
            duplicate_root,
            [_opencode_message(), _opencode_message()],
        )
        result = hu.read_opencode_usage(duplicate_root)

        assert result == hu.HostUsageResult({}, complete=False, reason="malformed")
        assert not isolated_adapter_caches[1].exists()

    @pytest.mark.parametrize(
        ("completed", "expected_day"),
        [
            ("2025-08-15T00:00:01Z", "2025-08-15"),
            (1_755_216_001, "2025-08-15"),
            ("2025-08-15T00:00:01", None),
            ("not-a-timestamp", None),
        ],
    )
    def test_completion_timestamp_variants_are_attributed_or_refused(
        self,
        completed: object,
        expected_day: str | None,
        isolated_adapter_caches: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "opencode"
        _write_opencode_database(root, [_opencode_message(completed=completed)])

        result = hu.read_opencode_usage(root)

        if expected_day is None:
            assert result == hu.HostUsageResult({}, complete=False, reason="unsupported")
            assert not isolated_adapter_caches[1].exists()
        else:
            assert result.hosts["codex"][expected_day]["input"] == 120

    def test_error_rows_are_excluded_and_cache_contention_is_incomplete(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        errored = _opencode_message("message-error", input_tokens=999)
        errored["error"] = {"name": "provider_error"}
        _write_opencode_database(root, [_opencode_message(), errored])

        result = hu.read_opencode_usage(root)

        assert result.hosts["codex"]["2025-08-15"]["input"] == 120
        opencode_cache = isolated_adapter_caches[1]
        cache_before = opencode_cache.read_bytes()
        fd = os.open(str(opencode_cache), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            locked = hu.read_opencode_usage(root, deadline=time.monotonic() + 1.0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert locked == hu.HostUsageResult({}, complete=False, reason="locked")
        assert opencode_cache.read_bytes() == cache_before


class TestCodexTurnDedup:
    """Cross-file dedup, the half of per-turn accounting a single file cannot see.

    Measured on a real 746-rollout corpus: 195 ``turn_id`` values appear in
    more than one file, spanning 244 of them, sharing 85% of their ledger
    before diverging. Summing per file double-counted 55% of the total. These
    pins are the regression guard for that reduction.
    """

    def test_shared_turn_prefix_across_rollout_files_is_counted_once(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Two files forked from one conversation share their prefix.

        Both carry the SAME turn id and the same cumulative readings. The work
        happened once, so it is counted once. Summing the files would report
        200; keeping only one file would also report 100 here but is wrong for
        the divergent case below.
        """
        root = tmp_path / "sessions"
        shared = [_context(turn="turn-a"), _token(40, last=40), _token(100)]
        _write_rollout(root, "rollout-fork-1.jsonl", shared)
        _write_rollout(root, "rollout-fork-2.jsonl", shared)

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100

    def test_divergent_fork_tails_are_both_retained(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """The branch that "keep the longest file" would silently discard.

        Same prefix, then each file continues differently. Both tails are real
        work and both must survive: 100 shared + 50 + 30.
        """
        root = tmp_path / "sessions"
        prefix = [_context(turn="turn-a"), _token(40, last=40), _token(100)]
        _write_rollout(root, "rollout-fork-1.jsonl", [*prefix, _token(150)])
        _write_rollout(root, "rollout-fork-2.jsonl", [*prefix, _token(130)])

        result = hu.read_codex_usage(root)

        # 40 opening + 60 shared + 50 on one branch + 30 on the other.
        # Deduping READINGS instead would treat 130 as a waypoint to 150 and
        # report 150, silently dropping one branch's work.
        assert result.hosts["codex"]["2026-08-15"]["input"] == 180

    def test_turn_dedup_is_independent_of_file_order(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Reduction is over a SET of states, so scan order cannot change it.

        `_iter_rollouts` walks a sorted directory tree, but a partial scan
        commits whatever prefix it reached, so entries reach `_aggregate` in
        whatever order the cache happens to hold. An order-sensitive reduction
        would make the published number depend on how many passes it took.
        """
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        prefix = [_context(turn="turn-a"), _token(40, last=40), _token(100)]
        # `_iter_rollouts` walks a SORTED tree, so writing the same two files in
        # a different order proves nothing. Swap which branch lives under which
        # filename instead — then sorted traversal genuinely reverses the order
        # the two state sequences reach `_aggregate`.
        _write_rollout(root_a, "rollout-1.jsonl", [*prefix, _token(150)])
        _write_rollout(root_a, "rollout-2.jsonl", [*prefix, _token(130)])
        _write_rollout(root_b, "rollout-1.jsonl", [*prefix, _token(130)])
        _write_rollout(root_b, "rollout-2.jsonl", [*prefix, _token(150)])

        assert hu.read_codex_usage(root_a).hosts == hu.read_codex_usage(root_b).hosts
        assert hu.read_codex_usage(root_a).hosts["codex"]["2026-08-15"]["input"] == 180

    def test_turn_boundary_reemission_is_not_counted_twice(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Codex repeats a turn's final ledger as the next turn's first.

        Found on `rollout-2026-04-11T09-20-25` in the live corpus: a new
        `turn_context` at line 283, then a `token_count` at line 286 restating
        the previous turn's total (1,422,894) and its `last_token_usage`
        (73,158) verbatim. Chaining each turn independently restarts the second
        chain with that `last` and counts it twice — 473,932 input tokens over
        that one 71-record file.

        This is why a turn id is a LINEAGE LINK and not a bucket key: both
        turns share one cumulative number line, so the re-emitted reading is a
        state already in the set.
        """
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-two-turns.jsonl",
            [
                _context(turn="turn-a"),
                _token(40, last=40),
                _token(100),
                _context(turn="turn-b"),
                _token(100, last=60),  # verbatim re-emission of the last reading
                _token(160),
            ],
        )

        result = hu.read_codex_usage(root)

        # 160 is the session's terminal cumulative. Counting the re-emission
        # would report 220.
        assert result.hosts["codex"]["2026-08-15"]["input"] == 160

    def test_an_opening_already_reached_by_another_file_is_not_charged_again(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """A resumed file can OPEN at a total another file spent its way to.

        An opening reading has no predecessor, so it has no transition identity
        and cannot be deduped like every other reading. If a sibling in the same
        lineage already arrived at that exact cumulative by spending tokens,
        that arrival counted the work, and charging this file's
        `last_token_usage` on top counts it twice.

        Measured: 1 of 747 rollouts on the live corpus. Rare, and wrong.
        """
        root = tmp_path / "sessions"
        # Parent spends its way 40 -> 100 under the shared turn.
        _write_rollout(
            root,
            "rollout-parent.jsonl",
            [_context(turn="shared"), _token(40, last=40), _token(100)],
        )
        # Child resumes AT 100, claiming 60 of its own, then reaches 150.
        _write_rollout(
            root,
            "rollout-resumed.jsonl",
            [_context(turn="shared"), _token(100, last=60), _token(150)],
        )

        result = hu.read_codex_usage(root)

        # 40 + 60 (parent's own transition) + 50 (child's own). NOT 210.
        assert result.hosts["codex"]["2026-08-15"]["input"] == 150

    def test_duplicate_token_count_record_produces_zero_increment(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """183 of 746 corpus rollouts repeat a `token_count` (414 records).

        The total does not advance, so the host counted that turn once.
        Differencing the host's own counter cannot double-count it; summing
        `last_token_usage` would.
        """
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-dupe.jsonl",
            [_context(turn="t"), _token(40, last=40), _token(90), _token(90)],
        )

        result = hu.read_codex_usage(root)

        assert result.hosts["codex"]["2026-08-15"]["input"] == 90

    def test_unforked_rollout_reconciles_with_its_terminal_total(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """The auditability pin.

        For a file with no fork and no inherited parent total, the per-turn
        increments must sum to exactly the cumulative total the SHIPPED reader
        published. That is what makes this change checkable against the old
        number rather than merely different from it. Verified on 479 of 479
        in-scope rollouts of the live corpus, across all four counters.
        """
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-recon.jsonl",
            [
                _context(turn="t1"),
                _token(10, last=(10, 0, 0, 1), output=1),
                _token(35, output=4),
                _context(turn="t2"),
                _token(80, last=(45, 0, 0, 5), output=9),
            ],
        )

        result = hu.read_codex_usage(root)
        day = result.hosts["codex"]["2026-08-15"]

        assert day["input"] == 80
        assert day["output"] == 9

    def test_resumed_rollout_excludes_the_inherited_parent_total(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """4 corpus rollouts open with a total that already counts a parent.

        `total[0]` is 11,680,081 while `last_token_usage` is 89,789 on the real
        ones. The parent file already reported its own history, so counting the
        inherited figure again is the double-count this Track removes.
        """
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-resumed.jsonl",
            [_context(turn="child"), _token(5000, last=90), _token(5150)],
        )

        result = hu.read_codex_usage(root)

        # 90 of its own, then a 150 increment. NOT 5150.
        assert result.hosts["codex"]["2026-08-15"]["input"] == 240

    def test_counter_decrease_clamps_without_negative_usage(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Zero occurrences in 746 rollouts, so this is a contract not a case.

        Monotonicity of `total_token_usage` is an observation about today's
        Codex, not a documented guarantee. A reset, a compaction, or a schema
        change could break it, and a negative bucket would propagate onto the
        wire as an unsigned counter.
        """
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-reset.jsonl",
            [_context(turn="t"), _token(100, last=100), _token(40, last=40)],
        )

        result = hu.read_codex_usage(root)
        day = result.hosts["codex"]["2026-08-15"]

        assert day["input"] >= 0
        # Sorted union: 40 is the lower state so it opens the chain with its
        # own `last`, then 100 adds the 60 difference.
        assert day["input"] == 100


class TestCodexPreContextBuffer:
    """Ledgers seen before the first `turn_context` are buffered, not dropped.

    The shipped reader dropped them, justified by "totals are CUMULATIVE, so a
    later attributable record restates these tokens". Per-turn accounting
    deletes that premise. Live corpus: 7 rollouts, 1,557 such records,
    209,515,399 input tokens.
    """

    def test_pre_context_ledgers_are_attributed_not_dropped(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-early.jsonl",
            [_token(10, last=10), _token(20), _context(turn="t"), _token(30)],
        )

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 30

    def test_only_pre_context_ledgers_still_refuse_the_store(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """The buffer must NOT rescue an unattributable file.

        A ledger we saw and could attribute to NOTHING still refuses, exactly
        as `test_missing_model_before_token_is_incomplete` pins. Silently
        converting it to `no_ledger` would under-report real usage, which is
        the opposite failure from the tolerated marker shapes.
        """
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-no-context.jsonl", [_token(10), _token(20)])

        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="unsupported")


class TestCodexCacheMigration:
    def test_pre_track_cache_entry_forces_a_full_rewalk(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Absence of `states` is the version discriminator.

        A pre-Track entry carries `day`/`model`/`usage` and cannot seed a
        resume: it has no cumulative baseline, no turn id, and no pending
        buffer. Trusting it partially would silently mis-attribute; rejecting
        it costs one re-walk of that file. Deliberately NOT a `CACHE_VERSION`
        bump, which is shared with the Grok and OpenCode namespaces.
        """
        root = tmp_path / "sessions"
        path = _write_rollout(root, "rollout-old.jsonl", [_context(turn="t"), _token(50, last=50)])
        source = path.stat()
        isolated_cache.parent.mkdir(parents=True, exist_ok=True)
        isolated_cache.write_text(
            json.dumps(
                {
                    "version": hu.CACHE_VERSION,
                    "files": {
                        hu._cache_key(path): {
                            "dev": source.st_dev,
                            "ino": source.st_ino,
                            "size": source.st_size,
                            "mtime_ns": source.st_mtime_ns,
                            "head": "x" * 64,
                            "head_len": min(source.st_size, 4096),
                            "tail": "y" * 64,
                            "tail_len": min(source.st_size, 4096),
                            "offset": source.st_size,
                            "day": "2026-08-15",
                            "model": "gpt-5-codex",
                            "usage": {
                                "input": 999_999,
                                "cache_create": 0,
                                "cache_read": 0,
                                "output": 0,
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        result = hu.read_codex_usage(root)

        # The stale 999,999 is never trusted; the file is re-read.
        assert result.hosts["codex"]["2026-08-15"]["input"] == 50
        entry = json.loads(isolated_cache.read_text(encoding="utf-8"))["files"][hu._cache_key(path)]
        assert "states" in entry
        assert "usage" not in entry


class TestCodexWireShape:
    def test_host_day_buckets_stay_exactly_the_four_token_fields(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Fleet-safety pin. An extra key here drops the WHOLE row on peers.

        `aggregator._copy_usage_bucket` validates a host day bucket with
        `set(bucket) != set(TOKEN_FIELDS) -> reject`, an exact match, and a
        rejected bucket fails the entire `host-usage-snapshot` row rather than
        the one field. So adding `by_model` (or anything else) to this payload
        makes every peer running an older mm discard the row and keep a stale
        one, fleet-wide, until the last machine upgrades.

        Bumping `EVENTS_SCHEMA_VERSION` does not rescue it either: the acceptor
        compares against the current constant, so after a bump it also rejects
        the older rows it had retained. Per-model detail therefore ships in
        Track 33A as an ADDITIVE SIBLING key, the same shape v0.12.47 used for
        `degraded_sources` — never by widening this bucket.
        """
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-wire.jsonl",
            [_context(turn="t"), _token(40, last=40), _token(90)],
        )

        result = hu.read_codex_usage(root)

        assert result.hosts
        for days in result.hosts.values():
            for bucket in days.values():
                assert set(bucket) == set(tu.TOKEN_FIELDS)
        assert result.tokens_by_day
        for day, bucket in result.tokens_by_day.items():
            assert "by_model" in bucket
            assert set(bucket) == set(tu.TOKEN_FIELDS) | {"by_model"}


class TestHostUsageBuckets:
    def test_add_usage_updates_both_views_atomically(self) -> None:
        buckets = hu.HostUsageBuckets()
        hu._add_usage(
            buckets,
            "2026-08-15",
            "gpt-5",
            {"input": 10, "cache_create": 1, "cache_read": 2, "output": 3},
        )
        hu._add_usage(
            buckets,
            "2026-08-15",
            "grok-4",
            {"input": 4, "cache_create": 0, "cache_read": 0, "output": 1},
        )
        assert buckets.by_family["codex"]["2026-08-15"]["input"] == 10
        assert buckets.by_family["grok"]["2026-08-15"]["input"] == 4
        day = buckets.by_day["2026-08-15"]
        assert day["input"] == 14
        assert day["by_model"]["gpt-5"]["input"] == 10
        assert day["by_model"]["grok-4"]["input"] == 4

    def test_zero_transition_still_creates_a_day_key(self) -> None:
        buckets = hu.HostUsageBuckets()
        hu._add_usage(
            buckets,
            "2026-08-15",
            "gpt-5",
            {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0},
        )
        assert "2026-08-15" in buckets.by_family["codex"]
        assert "2026-08-15" in buckets.by_day
        assert buckets.by_day["2026-08-15"]["by_model"]["gpt-5"]["input"] == 0

    def test_codex_reader_emits_per_model(self, isolated_cache: Path, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-models.jsonl",
            [_context("gpt-5-codex", turn="t"), _token(40, last=40)],
        )
        result = hu.read_codex_usage(root)
        assert result.complete is True
        assert "gpt-5-codex" in result.tokens_by_day["2026-08-15"]["by_model"]
        assert (
            result.tokens_by_day["2026-08-15"]["input"]
            == result.hosts["codex"]["2026-08-15"]["input"]
        )

    def test_grok_two_model_fixture_emits_per_model(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "two-model"
        shutil.copytree(FIXTURES / "grok" / "two-model", root)
        result = hu.read_grok_usage(root, consented=True)
        assert result.complete is True
        models = result.tokens_by_day["2026-08-14"]["by_model"]
        assert set(models) == {"grok-4", "grok-3"}
        assert (
            result.hosts["grok"]["2026-08-14"]["input"]
            == models["grok-4"]["input"] + models["grok-3"]["input"]
        )

    def test_opencode_reader_emits_per_model(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "opencode"
        _write_opencode_database(root, [_opencode_message()])
        result = hu.read_opencode_usage(root)
        assert result.complete is True
        assert "gpt-5" in result.tokens_by_day["2025-08-15"]["by_model"]

    def test_per_model_view_is_derived_from_warm_cache_without_reread(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.12.48 cache already interns models; this Track must not re-walk."""
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-warm.jsonl",
            [_context("gpt-5-codex", turn="t"), _token(50, last=50)],
        )
        first = hu.read_codex_usage(root)
        assert first.complete is True
        assert "gpt-5-codex" in first.tokens_by_day["2026-08-15"]["by_model"]

        def should_not_reread(*args: object, **kwargs: object) -> object:
            raise AssertionError("warm v0.12.48 cache must not reopen the jsonl")

        monkeypatch.setattr(hu, "_read_full_rollout", should_not_reread)
        monkeypatch.setattr(hu, "_resume_rollout", should_not_reread)
        second = hu.read_codex_usage(root)
        assert second.complete is True
        assert second.tokens_by_day == first.tokens_by_day
        assert second.hosts == first.hosts

    def test_historical_day_bucket_is_stable_across_two_snapshots(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        path = _write_rollout(
            root,
            "rollout-stable.jsonl",
            [
                _context("gpt-5-codex", turn="t"),
                _token(20, last=20, timestamp="2026-08-14T12:00:00Z"),
            ],
        )
        first = hu.read_codex_usage(root)
        day = "2026-08-14"
        first_input = first.tokens_by_day[day]["input"]
        with path.open("ab") as fp:
            fp.write(
                json.dumps(_token(30, last=10, timestamp="2026-08-15T12:00:00Z")).encode("utf-8")
                + b"\n"
            )
        second = hu.read_codex_usage(root)
        assert second.tokens_by_day[day]["input"] == first_input
        assert "2026-08-15" in second.tokens_by_day

    def test_resuming_a_session_does_not_erase_a_prior_active_day(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-resume.jsonl",
            [
                _context("gpt-5-codex", turn="t"),
                _token(20, last=20, timestamp="2026-08-14T12:00:00Z"),
                _token(25, last=5, timestamp="2026-08-16T12:00:00Z"),
            ],
        )
        result = hu.read_codex_usage(root)
        assert "2026-08-14" in result.tokens_by_day
        assert "2026-08-16" in result.tokens_by_day
