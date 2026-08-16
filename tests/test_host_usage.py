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


def _context(model: str = "gpt-5-codex") -> dict:
    return {
        "timestamp": "2026-08-14T23:59:58Z",
        "type": "turn_context",
        "payload": {"model": model},
    }


def _token(
    input_tokens: int,
    *,
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
                }
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
    def test_uses_final_cumulative_token_count_and_utc_day(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        shutil.copytree(FIXTURES, root)

        result = hu.read_codex_usage(root)

        assert result.complete is True
        assert result.reason is None
        assert result.hosts == {
            "codex": {
                "2026-08-15": {
                    "input": 200,
                    "cache_create": 20,
                    "cache_read": 40,
                    "output": 60,
                }
            }
        }
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
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-no-context.jsonl", [_token(10)])

        result = hu.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="unsupported")


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
        root = tmp_path / "sessions"
        rollout_a = _write_rollout(root, "rollout-a.jsonl", [_context(), _token(200)])
        rollout_b = _write_rollout(root, "rollout-b.jsonl", [_context(), _token(100)])
        first = hu.read_codex_usage(root)
        assert first.hosts["codex"]["2026-08-15"]["input"] == 300
        cache_before = isolated_cache.read_bytes()

        _write_rollout(root, "rollout-a.jsonl", [_context(), _token(400)])
        rollout_b.unlink()
        _write_rollout(root, "rollout-c.jsonl", [_context()], partial=b'{"type":"event_msg"')

        incomplete = hu.read_codex_usage(root)

        assert incomplete == hu.HostUsageResult({}, complete=False, reason="partial")
        assert isolated_cache.read_bytes() == cache_before

        (root / "2026" / "08" / "14" / "rollout-c.jsonl").unlink()
        stable = hu.read_codex_usage(root)
        assert stable.complete is True
        assert stable.hosts["codex"]["2026-08-15"]["input"] == 400
        assert rollout_a.exists()

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


class TestGrokUsage:
    def test_refuses_persisted_conversation_stream_without_reading_it(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        shutil.copytree(FIXTURES / "grok" / "workspace", root / "workspace")

        result = hu.read_grok_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="unsupported")
        grok_cache, opencode_cache = isolated_adapter_caches
        assert not grok_cache.exists()
        assert not opencode_cache.exists()

    def test_absent_root_and_expired_deadline_preserve_empty_vs_incomplete(
        self, isolated_adapter_caches: tuple[Path, Path], tmp_path: Path
    ) -> None:
        grok_cache, _ = isolated_adapter_caches
        assert hu.read_grok_usage(tmp_path / "absent") == hu.HostUsageResult({}, complete=True)
        expired = hu.read_grok_usage(tmp_path / "absent", deadline=time.monotonic() - 0.001)
        assert expired == hu.HostUsageResult(
            {}, complete=False, reason="deadline"
        )
        assert grok_cache.exists()


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

        assert result == hu.HostUsageResult(
            {
                "codex": {
                    "2025-08-15": {
                        "input": 120,
                        "cache_create": 10,
                        "cache_read": 20,
                        "output": 42,
                    }
                }
            },
            complete=True,
        )
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

        assert result == hu.HostUsageResult({}, complete=False, reason="unsupported")

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
