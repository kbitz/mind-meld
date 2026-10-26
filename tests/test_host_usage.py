"""Focused regression coverage for the private Codex host-usage reader.

Fixtures are synthetic and redacted. The named 2am regression is the
executable contract for cumulative replacement, deletion pruning, and refusal
to persist a partial discovery pass.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from mind_meld import host_registry, lockedjson
from mind_meld import host_usage as hu
from mind_meld import token_usage as tu
from mind_meld.hosts import codex as codex_host
from tests import _host_usage_oracle as oracle

FIXTURES = Path(__file__).parent / "fixtures" / "host_sessions"


class TestCursorStandaloneUsage:
    def _hook(self, **updates):
        return {
            "hook_event_name": "stop",
            "status": "completed",
            "generation_id": "standalone-generation",
            "model": "grok-4.7-low",
            "input_tokens": 17_534,
            "cache_write_tokens": 0,
            "cache_read_tokens": 1_152,
            "output_tokens": 147,
            **updates,
        }

    def _history(self):
        """Fold queued completions (no Conductor store) and return retained runs."""
        hu.read_cursor_usage(consented=True)
        return json.loads(hu.CURSOR_CACHE_PATH.read_bytes())["runs"]

    def test_hook_retains_only_normalized_usage_and_deduplicates_replays(self):
        payload = self._hook(text="PRIVATE RESPONSE", user_email="PRIVATE EMAIL")
        assert hu.record_cursor_usage(payload)
        queued = hu.CURSOR_SPOOL_PATH.read_bytes()
        assert b"PRIVATE" not in queued
        assert b"standalone-generation" not in queued
        assert hu.CURSOR_SPOOL_PATH.stat().st_mode & 0o777 == 0o600
        assert not hu.CURSOR_CACHE_PATH.exists()
        assert hu.cursor_usage_diag()["pending_completions"] == 1
        first = self._history()
        assert not hu.CURSOR_SPOOL_PATH.exists()
        assert hu.cursor_usage_diag()["pending_completions"] == 0
        assert hu.record_cursor_usage(payload)
        assert self._history() == first
        history = hu.CURSOR_CACHE_PATH.read_bytes()
        assert b"PRIVATE" not in history and b"standalone-generation" not in history
        result = hu.read_cursor_usage(consented=True)
        assert result.complete
        day = datetime.now(timezone.utc).date().isoformat()
        assert result.hosts["grok"][day] == {
            "input": 16_382,
            "cache_create": 0,
            "cache_read": 1_152,
            "output": 147,
        }
        assert result.tokens_by_day[day]["by_model"]["grok-4.7"]["input"] == 16_382

    def test_print_results_are_already_disjoint(self):
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "request_id": "print-generation",
            "usage": {
                "inputTokens": 100,
                "cacheWriteTokens": 0,
                "cacheReadTokens": 200,
                "outputTokens": 10,
            },
            "result": "PRIVATE RESPONSE",
        }
        assert hu.record_cursor_usage(result, model="grok-4.7-low-fast")
        run = next(iter(self._history().values()))
        assert run["usage"]["input"] == 100
        assert run["model"] == "grok-4.7-fast"

    @pytest.mark.parametrize(
        "updates",
        [
            {"input_tokens": True},
            {"cache_read_tokens": 100_000},
            {"output_tokens": None},
            {"generation_id": ""},
            {"model": None},
        ],
    )
    def test_invalid_completion_never_creates_history(self, updates):
        with pytest.raises(lockedjson.InvalidJsonCache):
            hu.record_cursor_usage(self._hook(**updates))
        assert not hu.CURSOR_SPOOL_PATH.exists()
        assert not hu.CURSOR_CACHE_PATH.exists()

    def test_corrupt_history_is_preserved_with_its_queue(self):
        hu.CURSOR_CACHE_PATH.parent.mkdir(parents=True)
        hu.CURSOR_CACHE_PATH.write_bytes(b"corrupt history")
        assert hu.record_cursor_usage(self._hook())
        assert hu.read_cursor_usage(consented=True).reason == "malformed"
        assert hu.CURSOR_CACHE_PATH.read_bytes() == b"corrupt history"
        assert hu.cursor_usage_diag()["pending_completions"] == 1

    def test_queue_survives_a_crash_between_fold_and_cleanup(self, monkeypatch):
        hu.record_cursor_usage(self._hook())
        merging = hu._cursor_spool_merging_path()

        def crash(*_args, **_kwargs):
            raise KeyboardInterrupt  # dies after the durable history write

        with monkeypatch.context() as dying:
            dying.setattr(type(merging), "unlink", crash)
            with pytest.raises(KeyboardInterrupt):
                hu.read_cursor_usage(consented=True)
        assert merging.exists()
        first = json.loads(hu.CURSOR_CACHE_PATH.read_bytes())["runs"]
        hu.record_cursor_usage(self._hook(generation_id="later", output_tokens=1))
        result = hu.read_cursor_usage(consented=True)  # re-folds leftover once
        assert json.loads(hu.CURSOR_CACHE_PATH.read_bytes())["runs"] == first
        assert not merging.exists() and hu.CURSOR_SPOOL_PATH.exists()
        assert len(self._history()) == 2
        day = datetime.now(timezone.utc).date().isoformat()
        assert result.hosts["grok"][day]["input"] == 16_382

    @pytest.mark.parametrize("first", [b"", b'{"key":"torn\n'], ids=["empty", "torn"])
    def test_unusable_first_batch_never_wedges_a_storeless_mac(self, first):
        hu.CURSOR_SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        hu.CURSOR_SPOOL_PATH.write_bytes(first)
        assert hu.read_cursor_usage(consented=True).reason == "no_metadata_ledger"
        assert not hu._cursor_spool_merging_path().exists()
        assert hu.record_cursor_usage(self._hook())
        assert hu.read_cursor_usage(consented=True).complete
        assert len(self._history()) == 1
        assert hu.cursor_usage_diag()["pending_completions"] == 0

    def test_storeless_first_read_timeout_does_not_block_later_captures(self, monkeypatch):
        hu.CURSOR_SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        hu.CURSOR_SPOOL_PATH.write_bytes(b"")
        clock = iter([0.0, 1.0])
        with monkeypatch.context() as late:
            late.setattr(hu.time, "monotonic", lambda: next(clock, 10.0))
            assert hu.read_cursor_usage(deadline=5.0, consented=True).reason == "deadline"
        assert json.loads(hu.CURSOR_CACHE_PATH.read_bytes())["complete_once"] is False
        assert hu.record_cursor_usage(self._hook())
        result = hu.read_cursor_usage(consented=True)
        assert result.complete
        assert result.hosts["grok"]

    def test_failed_history_write_keeps_the_queue(self, monkeypatch):
        hu.record_cursor_usage(self._hook())

        def disk_full(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        with monkeypatch.context() as full:
            full.setattr(lockedjson.fsutil, "atomic_write_bytes", disk_full)
            assert hu.read_cursor_usage(consented=True).reason == "io_error"
        assert hu._cursor_spool_merging_path().exists()
        assert len(self._history()) == 1
        assert not hu._cursor_spool_merging_path().exists()

    def test_newer_spool_rows_refuse_and_keep_the_batch(self):
        hu.record_cursor_usage(self._hook())
        row = json.loads(hu.CURSOR_SPOOL_PATH.read_bytes())
        hu.CURSOR_SPOOL_PATH.write_text(json.dumps({**row, "v": 2}) + "\n")
        assert hu.read_cursor_usage(consented=True).reason == "unsupported"
        assert hu._cursor_spool_merging_path().exists()
        # Persisted like a store-side blocker, so status can name the upgrade.
        assert hu.cursor_usage_diag()["last_reason"] == "unsupported"

    def test_same_batch_replays_keep_the_first_day(self, monkeypatch):
        class Clock(datetime):
            current = datetime(2026, 10, 3, 23, 59, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current

        monkeypatch.setattr(hu, "datetime", Clock)
        hu.record_cursor_usage(self._hook())
        Clock.current = datetime(2026, 10, 4, 0, 1, tzinfo=timezone.utc)
        hu.record_cursor_usage(self._hook(output_tokens=200))
        run = next(iter(self._history().values()))
        assert (run["day"], run["usage"]["output"]) == ("2026-10-03", 200)

    @pytest.mark.parametrize(
        "config,expected",
        [
            (
                {
                    "model": {"modelId": "gpt-5"},
                    "selectedModel": {
                        "modelId": "grok-4.7",
                        "parameters": [{"id": "fast", "value": "false"}],
                    },
                },
                "gpt-5",
            ),
            (
                {
                    "model": {"modelId": "grok-4.7"},
                    "selectedModel": {
                        "modelId": "grok-4.7",
                        "parameters": [{"id": "fast", "value": "true"}],
                    },
                },
                "grok-4.7-fast",
            ),
            ({"model": {"modelId": "grok-4.7"}}, "grok-4.7-unspecified"),
            ({}, "cursor-unknown"),
        ],
        ids=["stale-selected", "matching-selected", "current-only", "unresolved"],
    )
    def test_print_model_follows_cursors_current_model(self, config, expected):
        hu.CURSOR_CLI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        hu.CURSOR_CLI_CONFIG_PATH.write_text(json.dumps(config))
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "request_id": "print-model",
            "usage": {
                "inputTokens": 1,
                "cacheWriteTokens": 0,
                "cacheReadTokens": 0,
                "outputTokens": 1,
            },
        }
        assert hu.record_cursor_usage(result, model=hu.cursor_cli_model(["-p", "x"]))
        run = next(iter(self._history().values()))
        assert run["model"] == expected
        if expected == "cursor-unknown":
            assert hu.host_family(expected) == "other"

    def test_last_model_option_wins_like_cursor(self):
        args = ["-p", "--model=gpt-5-codex", "--model", "grok-4.7-low", "--", "--model=x"]
        assert hu.cursor_cli_model(args) == "grok-4.7-low"

    def test_torn_queue_rows_are_skipped_not_wedging(self, capsys):
        hu.record_cursor_usage(self._hook())
        with hu.CURSOR_SPOOL_PATH.open("ab") as spool:
            spool.write(b'{"key":"torn')
        hu.record_cursor_usage(self._hook(generation_id="after-tear"))
        assert len(self._history()) == 2
        assert "skipped 1 unreadable Cursor completion" in capsys.readouterr().err

    def _conductor_row(self, *, status="finished", usage=True, **updates):
        row = {
            "runId": "conductor-run",
            "requestId": "standalone-generation",
            "status": status,
            "endedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
            "model": {"id": "grok-4.7", "params": [{"id": "fast", "value": "false"}]},
            "usage": {
                "inputTokens": 16_000,
                "cacheWriteTokens": 0,
                "cacheReadTokens": 1_152,
                "outputTokens": 150,
                "reasoningTokens": 0,
                "totalTokens": 17_302,
            },
            **updates,
        }
        if status == "running":
            row.update(usage=None, endedAt=None)
        elif not usage:
            row.update(usage=None, usageRef="usage-ref")
        return row

    def _conductor(self, tmp_path, *rows):
        root = tmp_path / "conductor"
        session = root / "session"
        session.mkdir(parents=True, exist_ok=True)
        (session / "runs.ndjson").write_text("".join(json.dumps(r) + "\n" for r in rows))
        return root

    def test_conductor_request_replaces_overlapping_hook_once(self, tmp_path):
        hu.record_cursor_usage(self._hook())
        root = self._conductor(tmp_path, self._conductor_row())
        day = datetime.now(timezone.utc).date().isoformat()
        result = hu.read_cursor_usage(root, consented=True)
        assert result.complete
        # Conductor's counters win over the hook's (input 16,382 / output 147).
        assert result.hosts["grok"][day]["input"] == 16_000
        assert result.hosts["grok"][day]["output"] == 150
        assert len(json.loads(hu.CURSOR_CACHE_PATH.read_bytes())["runs"]) == 1
        shutil.rmtree(root)
        assert hu.record_cursor_usage(self._hook())
        assert hu.read_cursor_usage(root, consented=True).hosts == result.hosts

    def test_pending_conductor_run_keeps_standalone_usage(self, tmp_path):
        hu.record_cursor_usage(self._hook())
        day = datetime.now(timezone.utc).date().isoformat()
        root = self._conductor(tmp_path, self._conductor_row(status="running"))
        assert hu.read_cursor_usage(root, consented=True).hosts["grok"][day]["input"] == 16_382
        root = self._conductor(tmp_path, self._conductor_row(usage=False))
        result = hu.read_cursor_usage(root, consented=True)
        assert result.hosts["grok"][day]["input"] == 16_382
        assert day in result.partial_days
        assert hu.record_cursor_usage(self._hook(output_tokens=200))
        assert hu.read_cursor_usage(root, consented=True).hosts["grok"][day]["output"] == 200
        root = self._conductor(tmp_path, self._conductor_row())
        assert hu.read_cursor_usage(root, consented=True).hosts["grok"][day]["input"] == 16_000

    @pytest.mark.parametrize(
        "request_ids,complete",
        [((None,), True), (("",), False), ((7,), False), (("same", "same"), False)],
    )
    def test_conductor_request_id_shapes(self, tmp_path, request_ids, complete):
        rows = [
            self._conductor_row(runId=f"run-{index}", requestId=request)
            for index, request in enumerate(request_ids)
        ]
        result = hu.read_cursor_usage(self._conductor(tmp_path, *rows), consented=True)
        assert result.complete is complete
        if not complete:
            assert result.reason == "malformed"

    def test_standalone_only_transient_blocker_does_not_stick(self, tmp_path, monkeypatch):
        hu.record_cursor_usage(self._hook())
        missing = tmp_path / "no-conductor"
        clock = iter([0.0, 1.0])
        with monkeypatch.context() as late:
            # A deadline that trips after the lock is taken persists its reason.
            late.setattr(hu.time, "monotonic", lambda: next(clock, 10.0))
            assert hu.read_cursor_usage(missing, deadline=5.0, consented=True).reason == "deadline"
        assert json.loads(hu.CURSOR_CACHE_PATH.read_bytes())["last_reason"] == "deadline"
        assert hu.read_cursor_usage(missing, consented=True).complete

    @pytest.mark.parametrize(
        "params,expected",
        [
            ([{"id": "fast", "value": "false"}], "grok-4.7"),
            ([{"id": "fast", "value": "true"}], "grok-4.7-fast"),
            ([], "grok-4.7-unspecified"),
        ],
    )
    def test_cli_config_model_matches_explicit_flag_policy(self, params, expected):
        hu.CURSOR_CLI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        hu.CURSOR_CLI_CONFIG_PATH.write_text(
            json.dumps({"selectedModel": {"modelId": "grok-4.7", "parameters": params}})
        )
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "request_id": "config-model",
            "usage": {
                "inputTokens": 1,
                "cacheWriteTokens": 0,
                "cacheReadTokens": 0,
                "outputTokens": 1,
            },
        }
        assert hu.record_cursor_usage(result, model=hu.cursor_cli_model(["-p", "hello"]))
        run = next(iter(self._history().values()))
        assert run["model"] == expected

    def test_completion_never_waits_for_a_push_reader_holding_history(self):
        lock = hu.CURSOR_CACHE_PATH.with_name(hu.CURSOR_CACHE_PATH.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)  # a push reader mid-scan, for any duration
        try:
            started = time.monotonic()
            assert hu.record_cursor_usage(self._hook())
            assert time.monotonic() - started < 1
            assert hu.read_cursor_usage(consented=True).reason == "locked"
        finally:
            os.close(fd)
        assert len(self._history()) == 1

    def test_real_hooks_file_is_refused_under_pytest(self, monkeypatch):
        import pwd

        real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cursor" / "hooks.json"
        for spelling in (real, real.parent / ".." / ".cursor" / "hooks.json"):
            spelled = Path(str(spelling).replace("/.cursor/", "/.cursor/./"))
            monkeypatch.setattr(hu, "CURSOR_HOOK_CONFIG_PATH", spelled)
            with pytest.raises(PermissionError):
                hu.configure_cursor_hook(enabled=True)

    @pytest.mark.parametrize(
        "ours",
        [[{"command": "mm capture-cursor-usage", "timeout": 5}], [hu.CURSOR_HOOK_ENTRY] * 2],
        ids=["stale", "duplicate"],
    )
    def test_hook_install_canonicalizes_stale_or_duplicate_entries(self, ours):
        path = hu.CURSOR_HOOK_CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": 1, "hooks": {"stop": [{"command": "existing-hook"}, *ours]}})
        )
        assert hu.configure_cursor_hook(enabled=True) is True
        stop = json.loads(path.read_text())["hooks"]["stop"]
        assert stop == [{"command": "existing-hook"}, hu.CURSOR_HOOK_ENTRY]
        assert hu.configure_cursor_hook(enabled=True) is False

    def test_hook_state_and_model_reads_never_block_or_raise(self):
        hu.CURSOR_HOOK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(hu.CURSOR_HOOK_CONFIG_PATH)
        os.mkfifo(hu.CURSOR_CLI_CONFIG_PATH)
        assert hu.cursor_hook_state() == "malformed"
        assert hu.cursor_cli_model(["-p", "x"]) is None
        deep = "[" * 200_000 + "]" * 200_000
        for path, key in ((hu.CURSOR_HOOK_CONFIG_PATH, "hooks"), (hu.CURSOR_CLI_CONFIG_PATH, "x")):
            path.unlink()
            path.write_text('{"version":1,"' + key + '":' + deep + "}")
        assert hu.cursor_hook_state() == "malformed"
        assert hu.cursor_cli_model(["-p", "x"]) is None

    @pytest.mark.parametrize("requests", [{"not-hex": "a" * 64}, {"a" * 64: 7}, []])
    def test_malformed_alias_map_refuses_without_reset(self, requests):
        hu.record_cursor_usage(self._hook())
        self._history()
        data = json.loads(hu.CURSOR_CACHE_PATH.read_bytes())
        data["requests"] = requests
        hu.CURSOR_CACHE_PATH.write_text(json.dumps(data))
        before = hu.CURSOR_CACHE_PATH.read_bytes()
        assert hu.record_cursor_usage(self._hook())
        assert hu.read_cursor_usage(consented=True).reason == "malformed"
        assert hu.cursor_usage_diag()["cache_state"] == "unreadable"
        assert hu.CURSOR_CACHE_PATH.read_bytes() == before

    def test_request_alias_conflicts_refuse_within_and_across_scans(self, tmp_path):
        root = self._conductor(tmp_path, self._conductor_row(runId="run-a"))
        (root / "other").mkdir()
        other = root / "other" / "runs.ndjson"
        other.write_text(json.dumps(self._conductor_row(runId="run-b")) + "\n")
        assert hu.read_cursor_usage(root, consented=True).reason == "malformed"
        other.unlink()
        assert hu.read_cursor_usage(root, consented=True).complete
        (root / "session" / "runs.ndjson").unlink()
        other.write_text(json.dumps(self._conductor_row(runId="run-b")) + "\n")
        before = json.loads(hu.CURSOR_CACHE_PATH.read_bytes())["runs"]
        assert hu.read_cursor_usage(root, consented=True).reason == "malformed"
        assert json.loads(hu.CURSOR_CACHE_PATH.read_bytes())["runs"] == before

    def test_hook_install_and_removal_preserve_other_hooks_and_keys(self):
        path = hu.CURSOR_HOOK_CONFIG_PATH
        path.parent.mkdir(parents=True)
        original = {
            "version": 1,
            "hooks": {"stop": [{"command": "existing-hook"}], "sessionStart": []},
            "unfamiliar": {"enabled": True},
        }
        path.write_text(json.dumps(original))
        hu.configure_cursor_hook(enabled=True)
        installed = json.loads(path.read_text())
        assert installed["hooks"]["stop"] == [
            {"command": "existing-hook"},
            {"command": "mm capture-cursor-usage", "timeout": 10},
        ]
        assert installed["unfamiliar"] == original["unfamiliar"]
        unchanged = path.read_bytes()
        hu.configure_cursor_hook(enabled=True)
        assert path.read_bytes() == unchanged
        hu.configure_cursor_hook(enabled=False)
        assert json.loads(path.read_text()) == original

    def test_invalid_hooks_are_not_overwritten(self):
        path = hu.CURSOR_HOOK_CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(b"invalid hooks")
        with pytest.raises(lockedjson.InvalidJsonCache):
            hu.configure_cursor_hook(enabled=True)
        assert path.read_bytes() == b"invalid hooks"

    def test_hook_diagnostic_does_not_create_state(self):
        parent = hu.CURSOR_HOOK_CONFIG_PATH.parent
        assert hu.cursor_usage_diag()["hook_state"] == "missing"
        assert not parent.exists()
        hu.configure_cursor_hook(enabled=True)
        before = {path: path.read_bytes() for path in parent.iterdir()}
        assert hu.cursor_usage_diag()["hook_state"] == "installed"
        assert {path: path.read_bytes() for path in parent.iterdir()} == before

    def test_duplicate_callback_keeps_its_original_utc_day(self, monkeypatch):
        first_day = datetime(2026, 10, 3, 23, 59, tzinfo=timezone.utc)

        class Clock(datetime):
            current = first_day

            @classmethod
            def now(cls, tz=None):
                return cls.current

        monkeypatch.setattr(hu, "datetime", Clock)
        hu.record_cursor_usage(self._hook())
        assert len(self._history()) == 1
        Clock.current = datetime(2026, 10, 4, 0, 1, tzinfo=timezone.utc)
        hu.record_cursor_usage(self._hook(output_tokens=200))
        run = next(iter(self._history().values()))
        assert run["day"] == "2026-10-03"
        assert run["usage"]["output"] == 200

    @pytest.mark.parametrize("status", ["aborted", "error"])
    def test_unfinished_hook_does_not_invent_usage(self, status):
        assert hu.record_cursor_usage(self._hook(status=status)) is False
        assert not hu.CURSOR_CACHE_PATH.exists()


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


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "config" / "host-tokens.json"
    monkeypatch.setattr(codex_host, "CACHE_PATH", cache)
    return cache


_OBSERVED = "2026-09-04T10:00:00+00:00"
_NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def _seed_blocker(cache, *, reason="unsupported", since=_OBSERVED, files=None):
    cache.parent.mkdir(parents=True, exist_ok=True)
    data = {
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


def _write_muse_session(root: Path, rid: str = "e1") -> Path:
    """One Muse step in today's day directory, inside the reader's window."""
    now = datetime.now(timezone.utc)
    path = root / now.strftime("%Y/%m/%d") / "s1" / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_tokens": 40,
        "cache_write_tokens": 0,
        "cache_read_tokens": 40,
        "reasoning_tokens": 3,
    }
    record = {
        "id": rid,
        "record_type": "event",
        "payload_type": "runtime.session",
        "recorded_at": str(int(now.timestamp() * 1_000_000)),
        "payload": {
            "event": {"kind": "model_completed", "model": "muse-wonder-1.0", "usage": usage}
        },
    }
    with path.open("a") as fp:
        fp.write(json.dumps(record) + "\n")
    return path


@pytest.mark.parametrize(
    "prior,since,result,expected",
    [
        ("unsupported", _OBSERVED, hu.HostUsageResult({}, True), (None, None)),
        (None, None, hu._incomplete("malformed"), ("malformed", _NOW.isoformat())),
        ("deadline", _OBSERVED, hu._incomplete("io_error"), ("io_error", _NOW.isoformat())),
        ("unsupported", _OBSERVED, hu._incomplete("deadline"), ("unsupported", _OBSERVED)),
        ("unsupported", _OBSERVED, hu._incomplete("io_error"), ("unsupported", _OBSERVED)),
        ("partial", _OBSERVED, hu._incomplete("partial"), ("partial", _OBSERVED)),
        ("unsupported", None, hu._incomplete("unsupported"), ("unsupported", _NOW.isoformat())),
        ("unsupported", _OBSERVED, hu._incomplete("locked"), ("unsupported", _OBSERVED)),
        (None, None, hu._incomplete("no_metadata_ledger"), (None, None)),
    ],
)
def test_carry_standing_reason(prior, since, result, expected):
    assert hu._carry_reason(prior, since, result, _NOW) == expected


@pytest.mark.parametrize("new_file", [False, True], ids=["warm-append", "cold-sibling"])
def test_codex_unsupported_is_visible_and_recovery_clears_it(
    new_file, isolated_cache, tmp_path, monkeypatch
):
    root = tmp_path / "sessions"
    monkeypatch.setattr(codex_host, "CODEX_SESSIONS_PATH", root)
    records = [_context(), _token(100)]
    _write_rollout(root, "rollout-a.jsonl", records)
    assert codex_host.read_codex_usage(root).complete
    cached = json.loads(isolated_cache.read_text())["files"]
    bad = _token(200)
    bad["payload"]["info"]["total_token_usage"]["input_tokens"] = "broken"
    name = "rollout-b.jsonl" if new_file else "rollout-a.jsonl"
    _write_rollout(root, name, [*records, bad])
    writer = Mock(wraps=lockedjson._write_json)
    monkeypatch.setattr(lockedjson, "_write_json", writer)
    assert codex_host.read_codex_usage(root).reason == "unsupported"
    writer.assert_called_once()
    data = json.loads(isolated_cache.read_text())
    assert data["files"] == cached
    assert data["version"] == hu.CACHE_VERSION == 1
    assert data["last_reason"] == "unsupported"
    assert hu._cached_reason_since(data) is not None
    diag = codex_host.codex_usage_diag()
    assert diag["state"] == ("migrating" if new_file else "ready")
    assert diag["pending"] == int(new_file)
    assert diag["last_reason"] == "unsupported"
    writer.reset_mock()
    assert codex_host.read_codex_usage(root).reason == "unsupported"
    writer.assert_not_called()
    _write_rollout(root, name, records)
    assert codex_host.read_codex_usage(root).complete
    assert codex_host.codex_usage_diag()["last_reason"] is None
    assert codex_host.codex_usage_diag()["last_reason_since"] is None


def test_legacy_codex_root_does_not_force_file_rewalk(isolated_cache, tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    _write_rollout(root, "rollout-old.jsonl", [_context(), _token(100)])
    before = codex_host.read_codex_usage(root)
    data = json.loads(isolated_cache.read_text())
    del data["last_reason"]
    del data["last_reason_since"]
    isolated_cache.write_text(json.dumps(data))
    monkeypatch.setattr(codex_host, "_read_full_rollout", lambda *a: pytest.fail("re-walked"))
    assert codex_host.read_codex_usage(root) == before
    assert json.loads(isolated_cache.read_text())["version"] == 1


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
            ("muse-wonder-1.0", "muse"),
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

        The fixture is one session with a ledger on each of two days. Inclusive
        input 100 contains cache_read 20 and cache_create 10, so the disjoint
        uncached input is 70 per day. 8 of 746 rollouts on a real corpus span
        more than one UTC day.
        """
        root = tmp_path / "sessions"
        shutil.copytree(FIXTURES, root)

        result = codex_host.read_codex_usage(root)

        assert result.complete is True
        assert result.reason is None
        assert result.hosts == {
            "codex": {
                "2026-08-14": {
                    "input": 70,
                    "cache_create": 10,
                    "cache_read": 20,
                    "output": 30,
                },
                "2026-08-15": {
                    "input": 70,
                    "cache_create": 10,
                    "cache_read": 20,
                    "output": 30,
                },
            }
        }
        # Disjoint uncached input: 70 + 70. Cache fields pass through.
        totals = result.hosts["codex"]
        assert sum(day["input"] for day in totals.values()) == 140
        assert sum(day["output"] for day in totals.values()) == 60
        assert result.partial_days == frozenset({"2026-08-14", "2026-08-15"})
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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        assert codex_host.read_codex_usage(root).hosts["codex"]["2026-08-15"]["input"] == 5

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

        assert codex_host.read_codex_usage(root).reason == "unsupported"

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        assert codex_host.read_codex_usage(root).reason == "unsupported"

    def test_broken_ledger_before_any_turn_context_is_still_fatal(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Model-attribution buffering runs before counter validation,
        so a non-dict `info` arriving first used to slip past the refusal the
        docstring promised. Refusal now happens in `_carries_usage`."""
        root = tmp_path / "sessions"
        broken = {
            "timestamp": "2026-08-15T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": "not-a-dict"},
        }
        _write_rollout(root, "rollout-early-broken.jsonl", [broken, _context(), _token(10)])

        assert codex_host.read_codex_usage(root).reason == "unsupported"

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

        first = codex_host.read_codex_usage(root)
        assert first.complete is True
        assert set(first.hosts) == {"codex"}, "a no-ledger file must not create a bucket"

        entry = json.loads(isolated_cache.read_text(encoding="utf-8"))["files"][
            hu._cache_key(abandoned)
        ]
        assert entry["no_ledger"] is True
        assert "day" not in entry and "model" not in entry and "usage" not in entry

        # Unchanged on the next scan → cache hit, not a re-parse.
        monkeypatch.setattr(
            codex_host,
            "_read_full_rollout",
            lambda *a, **k: pytest.fail("no-ledger file was re-parsed"),
        )
        second = codex_host.read_codex_usage(root)
        assert second.complete is True
        assert second.hosts == first.hosts

    def test_hand_edited_no_ledger_entry_cannot_smuggle_totals(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """`_aggregate` skips on the flag, so validation must strip anything
        riding along behind it rather than trusting the pair."""
        root = tmp_path / "sessions"
        abandoned = _write_rollout(root, "rollout-abandoned.jsonl", [_context()])
        assert codex_host.read_codex_usage(root).complete is True

        cache = json.loads(isolated_cache.read_text(encoding="utf-8"))
        cache["files"][hu._cache_key(abandoned)].update(
            {
                "day": "2026-08-15",
                "model": "gpt-5",
                "usage": {"input": 10**9, "cache_create": 0, "cache_read": 0, "output": 0},
            }
        )
        isolated_cache.write_text(json.dumps(cache), encoding="utf-8")

        result = codex_host.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts == {}, "smuggled totals must not reach the aggregate"


class TestCacheLifecycle:
    def test_unchanged_rollout_uses_cached_terminal(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-cached.jsonl", [_context(), _token(100)])
        first = codex_host.read_codex_usage(root)
        assert first.complete is True

        def should_not_reread(*args: object, **kwargs: object) -> object:
            raise AssertionError("unchanged rollout should be a cache hit")

        monkeypatch.setattr(codex_host, "_read_full_rollout", should_not_reread)
        second = codex_host.read_codex_usage(root)

        assert second == first

    def test_same_size_same_mtime_rewrite_replaces_terminal_total(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-rewrite.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).hosts["codex"]["2026-08-15"]["input"] == 200
        before = rollout.stat()

        _write_rollout(root, "rollout-rewrite.jsonl", [_context(), _token(400)])
        os.utime(rollout, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert rollout.stat().st_size == before.st_size
        assert rollout.stat().st_mtime_ns == before.st_mtime_ns

        result = codex_host.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 400

    def test_verified_append_resumes_from_cached_complete_line_offset(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-append.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).hosts["codex"]["2026-08-15"]["input"] == 200

        with rollout.open("ab") as fp:
            fp.write(json.dumps(_token(400)).encode("utf-8") + b"\n")

        def full_parse_would_violate_resume(*args: object, **kwargs: object) -> object:
            raise AssertionError("verified append should resume from the cache offset")

        monkeypatch.setattr(codex_host, "_read_full_rollout", full_parse_would_violate_resume)
        result = codex_host.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 400

    def test_changed_append_provenance_falls_back_to_full_parse(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-provenance.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).complete is True

        # Keep the original byte length, then append. The cached tail and head
        # no longer prove this is an append, so it must receive a full parse.
        _write_rollout(root, "rollout-provenance.jsonl", [_context(), _token(300)])
        with rollout.open("ab") as fp:
            fp.write(json.dumps(_token(400)).encode("utf-8") + b"\n")

        full_parse = codex_host._read_full_rollout
        calls = 0

        def record_full_parse(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return full_parse(*args, **kwargs)

        monkeypatch.setattr(codex_host, "_read_full_rollout", record_full_parse)
        result = codex_host.read_codex_usage(root)

        assert calls == 1
        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 400

    def test_mutation_during_full_parse_preserves_existing_cache(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-race-full.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).complete is True
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
        result = codex_host.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="stale")
        data = json.loads(isolated_cache.read_text())
        assert data["files"] == json.loads(cache_before)["files"]
        assert data["last_reason"] == result.reason
        assert hu._cached_reason_since(data) is not None

    def test_mutation_after_append_proof_preserves_existing_cache(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-race-resume.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).complete is True
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
        result = codex_host.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="stale")
        data = json.loads(isolated_cache.read_text())
        assert data["files"] == json.loads(cache_before)["files"]
        assert data["last_reason"] == result.reason
        assert hu._cached_reason_since(data) is not None

    def test_deadline_during_cache_fingerprinting_preserves_existing_cache(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-fingerprint-deadline.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).complete is True
        cache_before = isolated_cache.read_bytes()

        def expire_fingerprint(*args: object, **kwargs: object) -> object:
            raise hu._ReadFailure("deadline")

        monkeypatch.setattr(hu, "_fingerprint", expire_fingerprint)
        result = codex_host.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="deadline")
        data = json.loads(isolated_cache.read_text())
        assert data["files"] == json.loads(cache_before)["files"]
        assert data["last_reason"] == result.reason
        assert hu._cached_reason_since(data) is not None

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
        # Track 85A scans newest-first (name descending within a day): the
        # re-parsed rollout sorts first, the partial one last.
        rollout_a = _write_rollout(root, "rollout-z.jsonl", [_context(), _token(200)])
        rollout_b = _write_rollout(root, "rollout-b.jsonl", [_context(), _token(100)])
        first = codex_host.read_codex_usage(root)
        assert first.hosts["codex"]["2026-08-15"]["input"] == 300
        keys_before = set(json.loads(isolated_cache.read_text(encoding="utf-8"))["files"])
        key_a, key_b = hu._cache_key(rollout_a), hu._cache_key(rollout_b)

        _write_rollout(root, "rollout-z.jsonl", [_context(), _token(400)])
        rollout_b.unlink()
        _write_rollout(root, "rollout-a.jsonl", [_context()], partial=b'{"type":"event_msg"')

        incomplete = codex_host.read_codex_usage(root)

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

        (root / "2026" / "08" / "14" / "rollout-a.jsonl").unlink()
        stable = codex_host.read_codex_usage(root)
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
        monkeypatch.setattr(codex_host, "time", clock)

        root = tmp_path / "sessions"
        for i in range(10):
            _write_rollout(root, f"rollout-{i:02d}.jsonl", [_context(), _token(10 + i)])

        real_full_read = codex_host._read_full_rollout
        parses: list[int] = [0]

        def timed_full_read(path: Path, before: object, deadline: float):
            parses[0] += 1
            clock.now += 0.1  # each cold parse costs 100ms of fake wall clock
            return real_full_read(path, before, deadline)

        monkeypatch.setattr(codex_host, "_read_full_rollout", timed_full_read)

        cached_entries, attempts, result = [], 0, None
        while attempts < 20:
            attempts += 1
            result = codex_host.read_codex_usage(root, deadline=clock.now + 0.25)
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

        _seed_blocker(isolated_cache)
        real_scan = codex_host._scan_codex_root
        blown = {"on": False}
        real_expired = hu._expired

        def scan_then_blow_the_budget(source_root, cached_files, deadline, **kwargs):
            outcome = real_scan(source_root, cached_files, deadline, **kwargs)
            blown["on"] = True
            return outcome

        monkeypatch.setattr(codex_host, "_scan_codex_root", scan_then_blow_the_budget)
        monkeypatch.setattr(hu, "_expired", lambda d: blown["on"] or real_expired(d))

        result = codex_host.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="deadline")
        files = json.loads(isolated_cache.read_text(encoding="utf-8"))["files"]
        assert codex_host.codex_usage_diag()["last_reason"] == "deadline"
        assert codex_host.codex_usage_diag()["last_complete_ms"] is not None
        assert len(files) == 1, "a complete-but-overbudget scan must still keep its work"

        # ...and the work counts: the next in-budget scan re-parses nothing.
        monkeypatch.setattr(hu, "_expired", real_expired)
        monkeypatch.setattr(codex_host, "_scan_codex_root", real_scan)
        monkeypatch.setattr(
            codex_host, "_read_full_rollout", lambda *a, **k: pytest.fail("prefix was re-parsed")
        )
        assert codex_host.read_codex_usage(root).hosts["codex"]["2026-08-15"]["input"] == 200

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
        monkeypatch.setattr(codex_host, "time", clock)

        root = tmp_path / "sessions"
        for i in range(4):  # sort AHEAD of the real ones
            _write_rollout(root, f"rollout-aaa-{i:02d}.jsonl", [_context()])
        for i in range(6):
            _write_rollout(root, f"rollout-zzz-{i:02d}.jsonl", [_context(), _token(10 + i)])

        real_full_read = codex_host._read_full_rollout

        def timed_full_read(path, before, deadline):
            clock.now += 0.1
            return real_full_read(path, before, deadline)

        monkeypatch.setattr(codex_host, "_read_full_rollout", timed_full_read)

        attempts, result = 0, None
        while attempts < 20:
            attempts += 1
            result = codex_host.read_codex_usage(root, deadline=clock.now + 0.25)
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

        result = host_registry.warm_host_cache_inline(root)

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
        assert codex_host.read_codex_usage(root).complete is True

        cache = json.loads(isolated_cache.read_text(encoding="utf-8"))
        cache["files"][hu._cache_key(path)]["day"] = "2026-08-15T23:59:59-07:00"
        isolated_cache.write_text(json.dumps(cache), encoding="utf-8")

        # Rejected as a cache hit -> re-parsed -> canonical day restored.
        assert codex_host.read_codex_usage(root).hosts["codex"] == {
            "2026-08-15": {"input": 200, "cache_create": 0, "cache_read": 0, "output": 0}
        }

    def test_deleted_rollout_is_pruned_after_complete_discovery(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-a.jsonl", [_context(), _token(200)])
        rollout_b = _write_rollout(root, "rollout-b.jsonl", [_context("grok-4"), _token(100)])
        assert set(codex_host.read_codex_usage(root).hosts) == {"codex", "grok"}

        rollout_b.unlink()
        result = codex_host.read_codex_usage(root)

        assert result.complete is True
        assert set(result.hosts) == {"codex"}

    def test_partial_line_preserves_existing_cache(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-stable.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).complete is True
        cache_before = isolated_cache.read_bytes()

        _write_rollout(root, "rollout-stable.jsonl", [_context(), _token(300)], partial=b"{")
        result = codex_host.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=False, reason="partial")
        data = json.loads(isolated_cache.read_text())
        assert data["files"] == json.loads(cache_before)["files"]
        assert data["last_reason"] == result.reason
        assert hu._cached_reason_since(data) is not None

    def test_lock_contention_returns_incomplete_without_mutating_cache(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-locked.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).complete is True
        cache_before = isolated_cache.read_bytes()
        fd = os.open(str(isolated_cache), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            result = codex_host.read_codex_usage(root, deadline=time.monotonic() + 1.0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert result == hu.HostUsageResult({}, complete=False, reason="locked")
        assert isolated_cache.read_bytes() == cache_before


class TestFailureAndTraversalContracts:
    def test_missing_root_is_complete_empty_scan(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        result = codex_host.read_codex_usage(tmp_path / "missing")

        assert result == hu.HostUsageResult({}, complete=True)

    def test_expired_deadline_does_not_create_cache(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        result = codex_host.read_codex_usage(
            tmp_path / "sessions", deadline=time.monotonic() - 0.001
        )

        assert result == hu.HostUsageResult({}, complete=False, reason="deadline")
        assert not isolated_cache.exists()

    def test_malformed_and_unsupported_token_events_are_incomplete(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        malformed = root / "2026" / "08" / "14" / "rollout-malformed.jsonl"
        malformed.parent.mkdir(parents=True, exist_ok=True)
        malformed.write_text("not json\n", encoding="utf-8")
        assert codex_host.read_codex_usage(root).reason == "malformed"

        malformed.unlink()
        _write_rollout(root, "rollout-unsupported.jsonl", [_context(), _token(10)])
        unsupported_path = root / "2026" / "08" / "14" / "rollout-unsupported.jsonl"
        unsupported = json.loads(unsupported_path.read_text(encoding="utf-8").splitlines()[1])
        del unsupported["payload"]["info"]["total_token_usage"]["input_tokens"]
        unsupported_path.write_text(
            json.dumps(_context()) + "\n" + json.dumps(unsupported) + "\n", encoding="utf-8"
        )
        assert codex_host.read_codex_usage(root).reason == "unsupported"

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

        result = codex_host.read_codex_usage(root)

        assert result == hu.HostUsageResult({}, complete=True)


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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        assert (
            codex_host.read_codex_usage(root_a).hosts == codex_host.read_codex_usage(root_b).hosts
        )
        assert codex_host.read_codex_usage(root_a).hosts["codex"]["2026-08-15"]["input"] == 180

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)
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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)
        day = result.hosts["codex"]["2026-08-15"]

        assert day["input"] >= 0
        # Sorted union: 40 is the lower state so it opens the chain with its
        # own `last`, then 100 adds the 60 difference.
        assert day["input"] == 100


def _write_archived(name: str, records: list[dict]) -> Path:
    path = codex_host.CODEX_ARCHIVED_SESSIONS_PATH / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records))
    return path


def _codex_key(path: Path, base: Path) -> str:
    return hu._cache_key(path, root=base, canonical_root=base.resolve())


class TestCodexArchivedRollouts:
    """Codex ``thread/archive`` moves a rollout to the flat ``archived_sessions``.

    Paseo archives on merge, so without this root its Codex usage drops out of
    the next complete pass. The autouse isolation points both roots at
    per-test directories; ``root=None`` reads both.
    """

    def test_archived_only_rollout_is_counted(self) -> None:
        _write_archived("rollout-archived.jsonl", [_context(), _token(100)])

        result = codex_host.read_codex_usage()

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100

    def test_rollout_copied_to_both_roots_is_counted_once(self) -> None:
        records = [_context(turn="turn-a"), _token(40, last=40), _token(100)]
        _write_rollout(codex_host.CODEX_SESSIONS_PATH, "rollout-a.jsonl", records)
        _write_archived("rollout-a.jsonl", records)

        result = codex_host.read_codex_usage()

        assert result.complete is True
        assert len(json.loads(codex_host.CACHE_PATH.read_text())["files"]) == 2, (
            "both paths were read"
        )
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100

    def test_hard_link_without_turn_id_is_counted_once(self) -> None:
        """The per-file ``own`` lineage (dev:ino:size:mtime) is what collapses a
        rename seen at both paths when no turn names the session."""
        records = [_context(), _token(40, last=40), _token(100)]
        live = _write_rollout(codex_host.CODEX_SESSIONS_PATH, "rollout-a.jsonl", records)
        codex_host.CODEX_ARCHIVED_SESSIONS_PATH.mkdir(parents=True)
        os.link(live, codex_host.CODEX_ARCHIVED_SESSIONS_PATH / live.name)

        result = codex_host.read_codex_usage()

        assert result.complete is True
        assert len(json.loads(codex_host.CACHE_PATH.read_text())["files"]) == 2, (
            "both paths were read"
        )
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100

    def test_archive_reads_only_flat_regular_rollouts(self, tmp_path: Path) -> None:
        archive = codex_host.CODEX_ARCHIVED_SESSIONS_PATH
        _write_archived("rollout-real.jsonl", [_context(), _token(100)])
        _write_archived("notes.jsonl", [_context(), _token(1_000)])
        _write_archived("nested/rollout-nested.jsonl", [_context(), _token(10_000)])
        outside = tmp_path / "outside.jsonl"
        outside.write_bytes(
            b"".join(json.dumps(r).encode() + b"\n" for r in [_context(), _token(100_000)])
        )
        (archive / "rollout-link.jsonl").symlink_to(outside)

        result = codex_host.read_codex_usage()

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100

    def test_partial_pass_merges_and_complete_pass_prunes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live_root = codex_host.CODEX_SESSIONS_PATH
        archive = codex_host.CODEX_ARCHIVED_SESSIONS_PATH
        live = _write_rollout(live_root, "rollout-a.jsonl", [_context(), _token(100)])
        kept = _write_archived("rollout-b1.jsonl", [_context(), _token(10)])
        gone = _write_archived("rollout-b2.jsonl", [_context(), _token(1)])
        assert codex_host.read_codex_usage().complete is True
        keys = {
            "live": _codex_key(live, live_root),
            "kept": _codex_key(kept, archive),
            "gone": _codex_key(gone, archive),
        }
        assert set(json.loads(codex_host.CACHE_PATH.read_text())["files"]) == set(keys.values())

        # Learn something on the live root, then expire on reaching the archive.
        with live.open("ab") as fp:
            fp.write(json.dumps(_token(200)).encode("utf-8") + b"\n")
        gone.unlink()
        real_key, real_expired = hu._cache_key, hu._expired
        blown = {"on": False}

        def key_then_blow_at_archive(path, **kwargs):
            blown["on"] = blown["on"] or path.parent == archive
            return real_key(path, **kwargs)

        with monkeypatch.context() as patched:
            patched.setattr(hu, "_cache_key", key_then_blow_at_archive)
            patched.setattr(hu, "_expired", lambda d: blown["on"] or real_expired(d))
            assert codex_host.read_codex_usage().reason == "deadline"

        files = json.loads(codex_host.CACHE_PATH.read_text())["files"]
        assert files[keys["live"]]["size"] == live.stat().st_size, "the partial pass was written"
        assert set(files) == set(keys.values()), "a partial pass must merge, not replace"

        result = codex_host.read_codex_usage()
        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 210
        files = json.loads(codex_host.CACHE_PATH.read_text())["files"]
        assert set(files) == {keys["live"], keys["kept"]}

    def test_rollout_moved_between_passes_keeps_its_total(self) -> None:
        live = _write_rollout(
            codex_host.CODEX_SESSIONS_PATH, "rollout-a.jsonl", [_context(), _token(100)]
        )
        assert codex_host.read_codex_usage().hosts["codex"]["2026-08-15"]["input"] == 100
        live_key = _codex_key(live, codex_host.CODEX_SESSIONS_PATH)
        assert set(json.loads(codex_host.CACHE_PATH.read_text())["files"]) == {live_key}

        codex_host.CODEX_ARCHIVED_SESSIONS_PATH.mkdir(parents=True)
        archived = codex_host.CODEX_ARCHIVED_SESSIONS_PATH / live.name
        os.rename(live, archived)
        result = codex_host.read_codex_usage()

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100
        files = json.loads(codex_host.CACHE_PATH.read_text())["files"]
        assert set(files) == {_codex_key(archived, codex_host.CODEX_ARCHIVED_SESSIONS_PATH)}

    def test_rollout_archived_between_listings_is_read_from_the_archive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listed = codex_host.CODEX_SESSIONS_PATH / "2026" / "08" / "14" / "rollout-moved.jsonl"
        _write_archived(listed.name, [_context(), _token(100)])
        monkeypatch.setattr(
            codex_host,
            "_iter_rollout_days",
            lambda root, deadline: [("2026/08/14", listed.parent, [listed])],
        )

        result = codex_host.read_codex_usage()

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100

    @pytest.mark.parametrize(
        "case", ["no-archive-match", "archived-path-vanished", "live-permission-denied"]
    )
    def test_other_rollout_stat_failures_stay_io_error(
        self, case: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if case == "no-archive-match":
            listed = codex_host.CODEX_SESSIONS_PATH / "2026" / "08" / "14" / "rollout-moved.jsonl"
            _write_archived("rollout-other.jsonl", [_context(), _token(100)])
            monkeypatch.setattr(
                codex_host,
                "_iter_rollout_days",
                lambda root, deadline: [("2026/08/14", listed.parent, [listed])],
            )
        elif case == "archived-path-vanished":
            listed = codex_host.CODEX_ARCHIVED_SESSIONS_PATH / "rollout-gone.jsonl"
            monkeypatch.setattr(
                codex_host, "_iter_archived_rollouts", lambda root, deadline: iter([listed])
            )
        else:
            listed = codex_host.CODEX_SESSIONS_PATH / "2026" / "08" / "14" / "rollout-denied.jsonl"
            _write_archived(listed.name, [_context(), _token(100)])
            monkeypatch.setattr(
                codex_host,
                "_iter_rollout_days",
                lambda root, deadline: [("2026/08/14", listed.parent, [listed])],
            )
            real_stat = hu._regular_stat

            def denied_live_stat(path):
                if path == listed:
                    try:
                        raise PermissionError("denied")
                    except PermissionError as exc:
                        raise hu._ReadFailure("io_error") from exc
                return real_stat(path)

            monkeypatch.setattr(hu, "_regular_stat", denied_live_stat)

        result = codex_host.read_codex_usage()
        assert result.complete is False
        assert result.reason == "io_error"
        assert result.hosts == {}

    def test_archive_discovery_stops_after_expiration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "rollout-a.jsonl").write_text("")
        (archive / "rollout-b.jsonl").write_text("")
        expired = {"value": False}
        calls: list[str] = []
        real_regular = hu._is_regular_non_symlink

        def stat_then_expire(path, **kwargs):
            calls.append(path.name)
            found = real_regular(path, **kwargs)
            expired["value"] = True
            return found

        monkeypatch.setattr(hu, "_expired", lambda deadline: expired["value"])
        monkeypatch.setattr(hu, "_is_regular_non_symlink", stat_then_expire)

        with pytest.raises(hu._ReadFailure) as caught:
            list(codex_host._iter_archived_rollouts(archive, float("inf")))

        assert caught.value.reason == "deadline"
        assert calls == ["rollout-a.jsonl"]

    def test_unarchive_during_discovery_refuses_partial_totals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = codex_host.CODEX_ARCHIVED_SESSIONS_PATH
        archived = _write_archived("rollout-a.jsonl", [_context(), _token(100)])
        assert codex_host.read_codex_usage().complete is True
        key = _codex_key(archived, archive)
        target = codex_host.CODEX_SESSIONS_PATH / "2026" / "08" / "14" / archived.name
        real_children = hu._sorted_children

        def unarchive_after_listing(path, deadline):
            children = real_children(path, deadline)
            if path == archive and archived in children:
                target.parent.mkdir(parents=True, exist_ok=True)
                archived.rename(target)
            return children

        monkeypatch.setattr(hu, "_sorted_children", unarchive_after_listing)

        result = codex_host.read_codex_usage()

        assert result.complete is False
        assert result.reason == "io_error"
        assert result.hosts == {}
        assert key in json.loads(codex_host.CACHE_PATH.read_text())["files"]

    def test_diag_listed_archive_disappearance_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = codex_host.CODEX_ARCHIVED_SESSIONS_PATH
        archived = _write_archived("rollout-a.jsonl", [_context(), _token(100)])
        assert codex_host.read_codex_usage().complete is True
        target = codex_host.CODEX_SESSIONS_PATH / "2026" / "08" / "14" / archived.name
        real_children = Path.iterdir

        def unarchive_after_listing(path):
            children = list(real_children(path))
            if path == archive and archived in children:
                target.parent.mkdir(parents=True, exist_ok=True)
                archived.rename(target)
            return iter(children)

        monkeypatch.setattr(Path, "iterdir", unarchive_after_listing)

        diag = codex_host.codex_usage_diag()
        assert diag["cache_state"] == "ok"
        assert diag["files_on_disk"] is None

    def test_unarchive_between_inventories_refuses_partial_totals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = codex_host.CODEX_ARCHIVED_SESSIONS_PATH
        archived = _write_archived("rollout-a.jsonl", [_context(), _token(100)])
        assert codex_host.read_codex_usage().complete is True
        key = _codex_key(archived, archive)
        target = codex_host.CODEX_SESSIONS_PATH / "2026" / "08" / "14" / archived.name
        real_archive = codex_host._iter_archived_rollouts

        def unarchive_before_listing(root, deadline):
            if archived.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                archived.rename(target)
            return real_archive(root, deadline)

        monkeypatch.setattr(codex_host, "_iter_archived_rollouts", unarchive_before_listing)

        result = codex_host.read_codex_usage()

        assert result.complete is False
        assert result.reason == "io_error"
        assert result.hosts == {}
        assert key in json.loads(codex_host.CACHE_PATH.read_text())["files"]

        retried = codex_host.read_codex_usage()
        assert retried.complete is True
        assert retried.hosts["codex"]["2026-08-15"]["input"] == 100
        assert set(json.loads(codex_host.CACHE_PATH.read_text())["files"]) == {
            _codex_key(target, codex_host.CODEX_SESSIONS_PATH)
        }

    @pytest.mark.parametrize("error", [errno.EACCES, errno.EIO])
    @pytest.mark.parametrize("symlink_root", [False, True])
    def test_archive_root_stat_error_is_incomplete_not_absent(
        self, error: int, symlink_root: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = codex_host.CODEX_ARCHIVED_SESSIONS_PATH
        if symlink_root:
            actual = tmp_path / "archive-target"
            actual.mkdir()
            archive.parent.mkdir(parents=True)
            archive.symlink_to(actual, target_is_directory=True)
        archived = _write_archived("rollout-a.jsonl", [_context(), _token(100)])
        assert codex_host.read_codex_usage().complete is True
        key = _codex_key(archived, archive)
        real_stat = Path.stat

        def failed_archive_stat(path, *args, **kwargs):
            if path == archive:
                raise OSError(error, "archive root stat failed")
            return real_stat(path, *args, **kwargs)

        def python314_exists(path, *, follow_symlinks=True):
            # Python 3.14 suppresses every OSError in Path.exists(). Keep
            # this contract reproducible on the suite's Python 3.11+ floor.
            try:
                path.stat(follow_symlinks=follow_symlinks)
            except (OSError, ValueError):
                return False
            return True

        monkeypatch.setattr(Path, "stat", failed_archive_stat)
        monkeypatch.setattr(Path, "exists", python314_exists)

        result = codex_host.read_codex_usage()

        assert result.complete is False
        assert result.reason == "io_error"
        assert result.hosts == {}
        assert key in json.loads(codex_host.CACHE_PATH.read_text())["files"]
        diag = codex_host.codex_usage_diag()
        assert diag["cache_state"] == "ok"
        assert diag["files_on_disk"] is None

    def test_missing_archive_needs_only_one_live_inventory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_rollout(codex_host.CODEX_SESSIONS_PATH, "rollout-a.jsonl", [_context(), _token(100)])
        real_live = codex_host._iter_rollout_days
        calls: list[Path] = []

        def counted_live(root, deadline):
            calls.append(root)
            return real_live(root, deadline)

        monkeypatch.setattr(codex_host, "_iter_rollout_days", counted_live)

        result = codex_host.read_codex_usage()

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100
        assert calls == [codex_host.CODEX_SESSIONS_PATH]

    def test_explicit_root_ignores_the_archive(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        _write_rollout(root, "rollout-live.jsonl", [_context(), _token(100)])
        _write_archived("rollout-archived.jsonl", [_context(), _token(1_000)])

        result = codex_host.read_codex_usage(root)

        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100

    def test_diag_counts_archived_rollouts_on_disk(self) -> None:
        _write_rollout(
            codex_host.CODEX_SESSIONS_PATH, "rollout-live.jsonl", [_context(), _token(100)]
        )
        _write_archived("rollout-b1.jsonl", [_context(), _token(10)])
        _write_archived("rollout-b2.jsonl", [_context(), _token(1)])
        _write_archived("notes.jsonl", [_context(), _token(1)])
        _write_archived("nested/rollout-nested.jsonl", [_context(), _token(1)])
        assert codex_host.read_codex_usage().complete is True

        diag = codex_host.codex_usage_diag()

        assert diag["files_on_disk"] == 3
        assert diag["files_cached"] == 3
        assert diag["pending"] == 0
        assert diag["state"] == "ready"


_OLD_NS = int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp() * 1_000_000_000)


def _age(path: Path, ns: int = _OLD_NS) -> None:
    os.utime(path, ns=(ns, ns))


class TestCodexSettledDays85A:
    """Track 85A: newest-first scan, settled-day freeze, stat-only hits."""

    def test_old_idle_day_freezes_and_warm_scan_carries_without_opening(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-old.jsonl", [_context(turn="turn-1"), _token(200)])
        _age(rollout)
        first = codex_host.read_codex_usage(root)
        assert first.complete is True
        assert first.hosts["codex"]["2026-08-15"]["input"] == 200
        data = json.loads(isolated_cache.read_text(encoding="utf-8"))
        assert set(data["settled_days"]) == {"2026/08/14"}
        record = data["settled_days"]["2026/08/14"]
        assert record["count"] == 1
        # Settled days stay backed by files CacheEntry records.
        assert set(record["keys"]) == set(data["files"])
        assert codex_host.codex_usage_diag()["files_cached"] == 1

        def no_open(*args: object, **kwargs: object) -> object:
            raise AssertionError("settled day must not reopen its rollouts")

        monkeypatch.setattr(hu, "_fingerprint", no_open)
        monkeypatch.setattr(codex_host, "_read_full_rollout", no_open)
        monkeypatch.setattr(codex_host, "_resume_rollout", no_open)
        second = codex_host.read_codex_usage(root)
        assert second.complete is True
        assert second.hosts["codex"]["2026-08-15"]["input"] == 200

    def test_external_turn_id_blocks_freeze(self, isolated_cache: Path) -> None:
        rollout = _write_rollout(
            codex_host.CODEX_SESSIONS_PATH,
            "rollout-old.jsonl",
            [_context(turn="shared"), _token(200)],
        )
        _age(rollout)
        # The same turn in the flat archive makes the day externally shared.
        _write_archived("rollout-arch.jsonl", [_context(turn="shared"), _token(200)])
        result = codex_host.read_codex_usage()
        assert result.complete is True
        data = json.loads(isolated_cache.read_text(encoding="utf-8"))
        assert data["settled_days"] == {}

    def test_empty_turn_id_blocks_freeze(self, isolated_cache: Path, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-old.jsonl", [_context(), _token(200)])
        _age(rollout)
        assert codex_host.read_codex_usage(root).complete is True
        data = json.loads(isolated_cache.read_text(encoding="utf-8"))
        assert data["settled_days"] == {}

    def test_partial_scan_rolls_back_settled_mutations(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-old.jsonl", [_context(turn="turn-1"), _token(200)])
        _age(rollout)
        assert codex_host.read_codex_usage(root).complete is True
        before = json.loads(isolated_cache.read_text(encoding="utf-8"))["settled_days"]
        assert before

        _write_rollout(root, "rollout-new.jsonl", [_context()], partial=b'{"type":"event_msg"')
        incomplete = codex_host.read_codex_usage(root)
        assert incomplete.complete is False
        after = json.loads(isolated_cache.read_text(encoding="utf-8"))["settled_days"]
        assert after == before, "check_seq bumps and evictions must roll back"

    def test_digest_mismatch_evicts_and_rescans(self, isolated_cache: Path, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-old.jsonl", [_context(turn="turn-1"), _token(200)])
        _age(rollout)
        assert codex_host.read_codex_usage(root).complete is True

        _write_rollout(
            root,
            "rollout-old.jsonl",
            [_context(turn="turn-1"), _token(200), _token(350)],
        )
        _age(rollout, _OLD_NS + 1_000_000_000)
        result = codex_host.read_codex_usage(root)
        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 350

    def test_newest_first_parse_order(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        old_day = root / "2026" / "08" / "14"
        new_day = root / "2026" / "09" / "02"
        for day, name in ((old_day, "rollout-a.jsonl"), (new_day, "rollout-b.jsonl")):
            day.mkdir(parents=True)
            (day / name).write_bytes(
                b"".join(
                    json.dumps(record).encode("utf-8") + b"\n"
                    for record in [_context(turn=name), _token(100)]
                )
            )
        order: list[str] = []
        real_full = codex_host._read_full_rollout

        def recording(path, before, deadline):
            order.append(path.name)
            return real_full(path, before, deadline)

        monkeypatch.setattr(codex_host, "_read_full_rollout", recording)
        assert codex_host.read_codex_usage(root).complete is True
        assert order == ["rollout-b.jsonl", "rollout-a.jsonl"]

    def test_stat_only_hit_after_24h_idle_and_fingerprint_when_young(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-a.jsonl", [_context(), _token(200)])
        assert codex_host.read_codex_usage(root).complete is True

        calls: list[Path] = []
        real_fingerprint = hu._fingerprint

        def counting(path, source, deadline):
            calls.append(path)
            return real_fingerprint(path, source, deadline)

        monkeypatch.setattr(hu, "_fingerprint", counting)
        assert codex_host.read_codex_usage(root).complete is True
        assert calls, "a young cached rollout still gets its fingerprint checked"

        calls.clear()
        # Rewind the cached identity to match the aged stat: rewrite the
        # entry's mtime so the hit is stat-identical but >= 24h idle.
        data = json.loads(isolated_cache.read_text(encoding="utf-8"))
        (entry,) = data["files"].values()
        old_ns = _now_old_ns()
        os.utime(rollout, ns=(old_ns, old_ns))
        entry["mtime_ns"] = old_ns
        data["settled_days"] = {}
        isolated_cache.write_text(json.dumps(data), encoding="utf-8")
        assert codex_host.read_codex_usage(root).complete is True
        assert calls == [], "an idle >=24h cached rollout is a stat-only hit"

    def test_settled_day_addition_and_deletion_evict_via_key_set(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "sessions"
        first = _write_rollout(root, "rollout-a.jsonl", [_context(turn="t-a"), _token(100)])
        _age(first)
        assert codex_host.read_codex_usage(root).complete is True
        assert json.loads(isolated_cache.read_text())["settled_days"]

        # An addition inside the settled day must be seen on the next scan
        # without waiting for its digest-verification turn.
        second = _write_rollout(root, "rollout-b.jsonl", [_context(turn="t-b"), _token(150)])
        _age(second)
        result = codex_host.read_codex_usage(root)
        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 250

        # Deleting every rollout drops the day and its tokens entirely.
        first.unlink()
        second.unlink()
        result = codex_host.read_codex_usage(root)
        assert result.complete is True
        assert result.hosts == {}
        data = json.loads(isolated_cache.read_text())
        assert data["settled_days"] == {}
        assert data["files"] == {}

    def test_only_stalest_settled_day_digest_verified_per_scan(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sessions"
        for day_name, turn in (("14", "t-14"), ("15", "t-15")):
            day = root / "2026" / "08" / day_name
            day.mkdir(parents=True)
            path = day / f"rollout-{day_name}.jsonl"
            path.write_bytes(
                b"".join(
                    json.dumps(record).encode("utf-8") + b"\n"
                    for record in [_context(turn=turn), _token(100)]
                )
            )
            _age(path)
        assert codex_host.read_codex_usage(root).complete is True
        before = json.loads(isolated_cache.read_text())["settled_days"]
        assert set(before) == {"2026/08/14", "2026/08/15"}

        digests: list[str] = []
        real_digest = codex_host._day_disk_digest

        def counting(day, deadline):
            digests.append(day.name)
            return real_digest(day, deadline)

        monkeypatch.setattr(codex_host, "_day_disk_digest", counting)
        assert codex_host.read_codex_usage(root).complete is True
        assert len(digests) == 1, "exactly one digest verification per scan"
        after = json.loads(isolated_cache.read_text())["settled_days"]
        bumped = [rel for rel in before if after[rel]["check_seq"] != before[rel]["check_seq"]]
        assert len(bumped) == 1
        stalest = min(before, key=lambda rel: before[rel]["check_seq"])
        assert bumped == [stalest]

    def test_recent_idle_blocks_freeze(self, isolated_cache: Path, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-a.jsonl", [_context(turn="t"), _token(100)])
        recent_ns = int((datetime.now(timezone.utc).timestamp() - 3600) * 1_000_000_000)
        _age(rollout, recent_ns)
        assert codex_host.read_codex_usage(root).complete is True
        assert json.loads(isolated_cache.read_text())["settled_days"] == {}

    def test_malformed_settled_days_are_dropped(self, isolated_cache: Path, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        rollout = _write_rollout(root, "rollout-a.jsonl", [_context(turn="t"), _token(100)])
        _age(rollout)
        assert codex_host.read_codex_usage(root).complete is True
        data = json.loads(isolated_cache.read_text())
        record = data["settled_days"]["2026/08/14"]
        record["keys"] = record["keys"] * 2  # duplicates
        record["count"] = len(record["keys"])
        data["settled_days"]["not-a-day"] = record
        isolated_cache.write_text(json.dumps(data))
        result = codex_host.read_codex_usage(root)
        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"]["input"] == 100

    def test_budget_limited_scans_converge_newest_first(
        self, isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeTime:
            now = 1_000.0

            def monotonic(self) -> float:
                return self.now

        clock = _FakeTime()
        monkeypatch.setattr(hu, "time", clock)
        monkeypatch.setattr(codex_host, "time", clock)
        root = tmp_path / "sessions"
        for i in range(6):
            _write_rollout(root, f"rollout-{i:02d}.jsonl", [_context(), _token(10 + i)])
        real_full = codex_host._read_full_rollout

        def timed(path, before, deadline):
            clock.now += 0.1
            return real_full(path, before, deadline)

        monkeypatch.setattr(codex_host, "_read_full_rollout", timed)
        attempts, result = 0, None
        while attempts < 10:
            attempts += 1
            result = codex_host.read_codex_usage(root, deadline=clock.now + 0.25)
            if result.complete:
                break
        assert result is not None and result.complete is True
        assert attempts > 1
        assert result.hosts["codex"]["2026-08-15"]["input"] == 75


def _now_old_ns() -> int:
    return int((datetime.now(timezone.utc).timestamp() - 48 * 3600) * 1_000_000_000)


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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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

        result = codex_host.read_codex_usage(root)

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
        result = codex_host.read_codex_usage(root)
        assert result.complete is True
        assert "gpt-5-codex" in result.tokens_by_day["2026-08-15"]["by_model"]
        assert (
            result.tokens_by_day["2026-08-15"]["input"]
            == result.hosts["codex"]["2026-08-15"]["input"]
        )

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
        first = codex_host.read_codex_usage(root)
        assert first.complete is True
        assert "gpt-5-codex" in first.tokens_by_day["2026-08-15"]["by_model"]

        def should_not_reread(*args: object, **kwargs: object) -> object:
            raise AssertionError("warm v0.12.48 cache must not reopen the jsonl")

        monkeypatch.setattr(codex_host, "_read_full_rollout", should_not_reread)
        monkeypatch.setattr(codex_host, "_resume_rollout", should_not_reread)
        second = codex_host.read_codex_usage(root)
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
        first = codex_host.read_codex_usage(root)
        day = "2026-08-14"
        first_input = first.tokens_by_day[day]["input"]
        with path.open("ab") as fp:
            fp.write(
                json.dumps(_token(30, last=10, timestamp="2026-08-15T12:00:00Z")).encode("utf-8")
                + b"\n"
            )
        second = codex_host.read_codex_usage(root)
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
        result = codex_host.read_codex_usage(root)
        assert "2026-08-14" in result.tokens_by_day
        assert "2026-08-16" in result.tokens_by_day


class TestCounterSemantics:
    """Track 35A: inclusive readers emit disjoint buckets."""

    def test_reader_totals_reconcile_codex(self, isolated_cache, tmp_path, monkeypatch):
        """Pin inclusive reconciliation through cold, warm, and resumed reads.

        The real-shaped opening carries input 2,801,950 and output 11,813;
        cached input is included in that input and must not be added twice.
        """
        root = tmp_path / "sessions"
        model = "gpt-5.6-terra"
        record = _token(2_801_950, cache_read=2_668_288, output=11_813)
        path = _write_rollout(root, "rollout-totals.jsonl", [_context(model, turn="t"), record])
        expected = {"input": 133_662, "cache_create": 0, "cache_read": 2_668_288, "output": 11_813}

        def check(result, counters):
            assert result.complete
            assert result.hosts == {"codex": {"2026-08-15": counters}}
            day = result.tokens_by_day["2026-08-15"]
            assert {key: day[key] for key in tu.TOKEN_FIELDS} == counters
            assert day["by_model"] == {model: counters}

        check(codex_host.read_codex_usage(root), expected)
        monkeypatch.setattr(
            codex_host, "_read_full_rollout", lambda *a: pytest.fail("full re-walk")
        )
        check(codex_host.read_codex_usage(root), expected)
        with path.open("a") as fp:
            fp.write(json.dumps(_token(2_802_950, cache_read=2_668_388, output=11_833)) + "\n")
        check(
            codex_host.read_codex_usage(root),
            {**expected, "input": 134_562, "cache_read": 2_668_388, "output": 11_833},
        )

    def test_synthetic_nonzero_cache_create_still_reconciles(self) -> None:
        raw_input, cache_read, cache_create, output = 1_000, 100, 50, 10
        usage = hu._normalize_inclusive_usage(
            {
                "input": raw_input,
                "cache_read": cache_read,
                "cache_create": cache_create,
                "output": output,
            }
        )
        assert usage["input"] + usage["cache_read"] + usage["cache_create"] == raw_input
        assert usage["input"] == 850

    def test_malformed_counters_degrade_the_reader_not_the_row(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """cache_read + cache_create > input → that reader is incomplete."""
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-malformed.jsonl",
            [_context(turn="t"), _token(10, cache_read=20, cache_create=0)],
        )
        result = codex_host.read_codex_usage(root)
        assert result.complete is False
        assert result.reason == "malformed"
        assert result.hosts == {}

    def test_cache_create_greater_than_input_is_malformed(self) -> None:
        with pytest.raises(hu._ReadFailure) as caught:
            hu._normalize_inclusive_usage(
                {"input": 10, "cache_read": 0, "cache_create": 11, "output": 1}
            )
        assert caught.value.reason == "malformed"

    def test_cache_read_plus_create_greater_than_input_is_malformed(self) -> None:
        with pytest.raises(hu._ReadFailure) as caught:
            hu._normalize_inclusive_usage(
                {"input": 10, "cache_read": 6, "cache_create": 5, "output": 1}
            )
        assert caught.value.reason == "malformed"

    def test_aggregate_passes_disjoint_muse_turns_through_unnormalized(self):
        """The turns branch already carries disjoint buckets."""
        usage = {"input": 10, "cache_create": 2, "cache_read": 50, "output": 15}
        buckets = hu._aggregate(
            [{"turns": [{"day": "2026-08-15", "model": "muse-1", "usage": usage}]}]
        )
        assert buckets.by_family == {"muse": {"2026-08-15": usage}}
        day = buckets.by_day["2026-08-15"]
        assert {key: day[key] for key in tu.TOKEN_FIELDS} == usage
        assert day["by_model"] == {"muse-1": usage}

    def test_codex_reader_round_trips_to_per_machine_floor(
        self, isolated_cache: Path, tmp_path: Path
    ) -> None:
        """Reader normalization and its cache-write tripwire survive the wire."""
        from mind_meld import events
        from mind_meld.skills.retro_fleet import aggregator

        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "rollout-priced.jsonl",
            [
                _context(model="gpt-5.6-terra", turn="t"),
                _token(1_000_000, cache_read=100_000, cache_create=40_000),
            ],
        )
        result = codex_host.read_codex_usage(root)
        assert result.complete is True
        assert result.hosts["codex"]["2026-08-15"] == {
            "input": 860_000,
            "cache_create": 40_000,
            "cache_read": 100_000,
            "output": 0,
        }
        assert result.partial_days == frozenset({"2026-08-15"})

        since = datetime(2026, 8, 14, tzinfo=timezone.utc)
        until = datetime(2026, 8, 16, tzinfo=timezone.utc)
        row = events.make_host_usage_snapshot(
            device="dev-a",
            token_sources=("codex",),
            hosts=result.hosts,
            tokens_by_day=result.tokens_by_day,
            partial_days={"codex": result.partial_days},
            ts=datetime(2026, 8, 15, 12, tzinfo=timezone.utc),
        )
        assert row["counter_semantics"] == events.COUNTER_SEMANTICS_DISJOINT_V1
        assert row["partial_sources"] == ["codex"]

        data = aggregator.RetroData(window_days=2, since=since, until=until)
        data.host_inventory = aggregator.aggregate_host_usage(
            [json.loads(json.dumps(row))],
            since=since,
            until=until,
            registered_ids=None,
        )
        data.fleet = aggregator.FleetState(
            devices_in_events={"dev-a"},
            devices_known=1,
            devices_known_list=[{"device_id": "dev-a", "device_name": "dev-a"}],
        )
        output = aggregator.format_retro(data)
        # 1.1: one fleet-wide Agents row per agent, not a per-machine dollar
        # table. The reader normalization and its partial-day tripwire still
        # have to survive the wire and land on that row as a floor.
        agents = output.split("## Agents", 1)[1].split("## ", 1)[0]
        assert "| Codex | 1.0M | 1 | 1 | ≥$1.84 |" in agents
        usage = aggregator.aggregate_agent_usage(data, machines_known=1)
        codex = next(r for r in usage.rows if r.key == "codex")
        assert any("incomplete" in c and "codex" in c for c in codex.floor_causes)


_ORACLE_RULE = (
    "tests/_host_usage_oracle.py: frozen at v0.14.15/70abdee; do not edit; "
    "on an intentional contract change delete and re-freeze from the new release"
)


def _ordered_reduction(module, entries):
    try:
        buckets = module._aggregate(entries)
    except module._ReadFailure as exc:
        return ("failure", exc.reason)
    return (
        "complete",
        json.dumps([buckets.by_family, buckets.by_day, sorted(buckets.unattributable_days)]),
    )


def _oracle_entry(states, ino=1):
    return {
        "dev": 1,
        "ino": ino,
        "size": 100,
        "mtime_ns": 1,
        "head": "head",
        "head_len": 100,
        "tail": "tail",
        "tail_len": 100,
        "offset": 100,
        "turn_ids": ["shared", "next", "unrelated"],
        "days": ["2026-09-16", "2026-09-17"],
        "models": ["gpt-5-codex", "grok-4", "claude-sonnet-4"],
        "states": states,
        "last_turn": "next",
        "last_model": "gpt-5-codex",
        "last_total": [40, 0, 10, 8],
        "pending": [[[10, 0, 2, 1], "2026-09-17", [5, 0, 1, 0]]],
    }


@pytest.mark.parametrize(
    "shape",
    [
        "fork",
        "resume",
        "reemit",
        "t-to-t",
        "opening-disarm",
        "malformed-increment",
        "multi-model",
        "multi-day",
        "grok-turns",
    ],
)
def test_reducer_ordered_identity_against_frozen_oracle(shape):
    a = [0, [100, 0, 20, 10], 0, 0, [20, 0, 2, 3]]
    b = [0, [150, 0, 40, 20], 0, 0, None]
    entries = [_oracle_entry([a, b])]
    if shape == "fork":
        entries += [_oracle_entry([a, [0, [170, 0, 45, 25], 1, 1, None]], 2)]
    elif shape in {"resume", "opening-disarm"}:
        entries += [_oracle_entry([b, [1, [190, 0, 50, 25], 1, 2, None]], 2)]
    elif shape == "reemit":
        entries[0]["states"] += [[1, b[1], 1, 1, b[1]]]
    elif shape == "t-to-t":
        entries[0]["states"] += [b]
    elif shape == "malformed-increment":
        entries[0]["states"] += [[1, [160, 0, 80, 20], 1, 2, None]]
    elif shape == "multi-model":
        entries[0]["states"] += [[1, [200, 5, 40, 30], 0, 1, None]]
    elif shape == "multi-day":
        entries[0]["states"] += [[1, [200, 0, 40, 30], 1, 0, None]]
    elif shape == "grok-turns":
        entries.insert(
            0,
            {
                "turns": [
                    {
                        "day": "2026-09-17",
                        "model": "grok-4",
                        "usage": {"input": 2, "cache_read": 9},
                    },
                    {"day": "2026-09-16", "model": "gpt-5-codex", "usage": {"output": 7}},
                ]
            },
        )
    assert _ordered_reduction(hu, entries) == _ordered_reduction(oracle, entries), _ORACLE_RULE


@pytest.mark.parametrize("seed", range(200))
def test_seeded_lineages_match_frozen_oracle(seed):
    rng = random.Random(seed)
    entries = []
    for lineage in range(rng.randint(1, 4)):
        states = []
        total = [0, 0, 0, 0]
        for _ in range(rng.randint(3, 15)):
            delta = [rng.randint(0, 100), 0, rng.randint(0, 10), rng.randint(0, 20)]
            if rng.random() < 0.2:
                delta = [0, 0, 0, 0]
            total = [a + b for a, b in zip(total, delta)]
            states.append([rng.randrange(2), total, rng.randrange(2), rng.randrange(3), delta])
        for branch in range(rng.randint(2, 5)):
            cut = rng.randint(1, len(states))
            selected = states[:cut]
            if rng.random() < 0.5:  # resumed suffix, linked through turn ids
                selected = states[cut - 1 :]
            if rng.random() < 0.5:  # fork with divergent tail
                last = selected[-1][1]
                selected = selected + [
                    [1, [last[0] + 31, last[1], last[2] + 2, last[3] + 5], 1, 2, None]
                ]
            if rng.random() < 0.5:  # repeated final reading / turn boundary re-emission
                selected = selected + [selected[-1]]
            entry = _oracle_entry(selected, lineage * 10 + branch)
            entry["turn_ids"] = [f"lineage-{lineage}-a", f"lineage-{lineage}-b", "unused"]
            entries.append(entry)
    rng.shuffle(entries)
    for entry in entries:
        assert json.dumps(codex_host._validated_entry(entry)) == json.dumps(
            oracle._validated_entry(entry)
        ), (seed, _ORACLE_RULE)
    assert _ordered_reduction(hu, entries) == _ordered_reduction(oracle, entries), (
        seed,
        _ORACLE_RULE,
    )


def test_validator_tamper_corpus_matches_frozen_oracle():
    base = _oracle_entry([[0, [100, 0, 20, 10], 0, 0, [20, 0, 2, 3]]])
    mutations = []
    bad_values = [-1, hu._MAX_COUNTER + 1, True, 1.5, "1", None, [], [1, 2]]
    for key in (
        "dev",
        "ino",
        "size",
        "mtime_ns",
        "head_len",
        "tail_len",
        "offset",
        "head",
        "tail",
        "turn_ids",
        "days",
        "models",
        "last_turn",
        "last_model",
        "last_total",
    ):
        for bad in bad_values:
            mutations.append(([key], bad))
    for col in range(5):
        for bad in bad_values + [9999, [True, 0, 0, 0], [1, 2, 3, -1], [1, 2, 3, 4, 5]]:
            mutations.append((["states", 0, col], bad))
    for field in (
        ["states", 0, 1],
        ["states", 0, 4],
        ["pending", 0, 0],
        ["pending", 0, 2],
        ["last_total"],
    ):
        for col in range(4):
            for bad in bad_values:
                mutations.append((field + [col], bad))
    for col in range(3):
        for bad in bad_values + ["2026-02-30", "20260917"]:
            mutations.append((["pending", 0, col], bad))
    for field in ("pending", "states"):
        for bad in bad_values:
            mutations.append(([field, 0], bad))
        for bad in ([], [1], [[1, 2]], [[0, [1, 2, 3, 4], 0, 0, None, 1]]):
            mutations.append(([field], bad))
    for location, bad in mutations:
        entry = json.loads(json.dumps(base))
        parent = entry
        for key in location[:-1]:
            parent = parent[key]
        parent[location[-1]] = bad
        assert json.dumps(codex_host._validated_entry(entry)) == json.dumps(
            oracle._validated_entry(entry)
        ), (location, bad, _ORACLE_RULE)


@pytest.mark.parametrize("reader", ["codex", "muse"])
def test_appended_file_validates_cached_entry_once(reader, tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    if reader == "codex":
        path = _write_rollout(root, "rollout-a.jsonl", [_context(), _token(100)])

        def read():
            return codex_host.read_codex_usage(root)

        validator = "_validated_entry"
        extra = _token(150)
    else:
        path = _write_muse_session(root)
        assert hu.read_muse_usage(root, consented=True).complete
        _write_muse_session(root, rid="e2")
        spy = Mock(wraps=hu._validated_turn_entry)
        monkeypatch.setattr(hu, "_validated_turn_entry", spy)
        assert hu.read_muse_usage(root, consented=True).complete
        assert spy.call_count == 1
        return
    assert read().complete
    with path.open("a") as fp:
        fp.write(json.dumps(extra) + "\n")
    spy = Mock(wraps=getattr(codex_host, validator))
    monkeypatch.setattr(codex_host, validator, spy)
    assert read().complete
    assert spy.call_count == 1


@pytest.mark.parametrize("symlinked", [False, True])
def test_scan_cache_key_matches_canonical_path(symlinked, tmp_path, monkeypatch):
    root = tmp_path / "real"
    path = _write_rollout(root, "rollout-a.jsonl", [_context(), _token(100)])
    if symlinked:
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        path = link / path.relative_to(root)
        root = link
    expected = hashlib.sha256(os.fsencode(path.resolve())).hexdigest()
    assert hu._cache_key(path, root=root, canonical_root=root.resolve()) == expected
    assert codex_host.read_codex_usage(root).complete
    assert set(json.loads(codex_host.CACHE_PATH.read_text())["files"]) == {expected}


def test_fingerprint_opens_once_and_keeps_digests_and_short_read_failure(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    raw = b"abc123" * 2000
    path.write_bytes(raw)
    before = path.stat()
    real_open = Path.open
    opens = []

    def opened(self, *args, **kwargs):
        opens.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    fp = hu._fingerprint(path, before, float("inf"))
    assert opens == [path]
    assert fp.head == hashlib.sha256(raw[:4096]).hexdigest()
    assert fp.tail == hashlib.sha256(raw[-4096:]).hexdigest()
    path.write_bytes(b"short")
    with pytest.raises(hu._ReadFailure) as err:
        hu._fingerprint(path, before, float("inf"))
    assert err.value.reason == "stale"


def test_fingerprint_open_oserror_is_io_error(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"abc123" * 2000)
    before = path.stat()

    def boom(self, *args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(Path, "open", boom)
    with pytest.raises(hu._ReadFailure) as err:
        hu._fingerprint(path, before, float("inf"))
    assert err.value.reason == "io_error"


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("reader", ["codex", "muse"])
def test_host_cache_accepts_both_encodings_and_writes_compact(
    compact, reader, tmp_path, monkeypatch
):
    root = tmp_path / "sessions"
    if reader == "codex":
        _write_rollout(root, "rollout-a.jsonl", [_context(), _token(100)])
        cache, target_mod, full_reader = codex_host.CACHE_PATH, codex_host, "_read_full_rollout"
    else:
        _write_muse_session(root)
        cache, target_mod, full_reader = hu.MUSE_CACHE_PATH, hu, "_read_full_muse_file"

    def read():
        if reader == "codex":
            return codex_host.read_codex_usage(root)
        return hu.read_muse_usage(root, consented=True)

    first = read()
    data = json.loads(cache.read_text())
    indented = json.dumps(data, sort_keys=True, indent=2)
    cache.write_text(json.dumps(data, separators=(",", ":")) if compact else indented)
    monkeypatch.setattr(target_mod, full_reader, Mock(side_effect=AssertionError("cache miss")))
    assert read() == first
    raw = cache.read_text()
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    assert len(raw) < len(indented)


def test_reduction_overrun_commits_pruning_timing_and_deadline(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    _write_rollout(root, "rollout-a.jsonl", [_context(), _token(100)])
    deleted = _write_rollout(root, "rollout-b.jsonl", [_context(), _token(200)])
    assert codex_host.read_codex_usage(root).complete
    deleted.unlink()
    clock = [1000.0]
    original = hu._aggregate

    def slow_reduction(entries):
        buckets = original(entries)
        clock[0] += 0.3
        return buckets

    monkeypatch.setattr(hu.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hu, "_aggregate", slow_reduction)
    assert codex_host.read_codex_usage(root, deadline=1000.25).reason == "deadline"
    data = json.loads(codex_host.CACHE_PATH.read_text())
    assert len(data["files"]) == 1
    assert data["last_reason"] == "deadline"
    assert data["last_complete_ms"] == 300
    assert data["last_deadline_allotted_ms"] == 250
    assert datetime.fromisoformat(data["last_complete_at"]).tzinfo is not None


class TestCursorUsage67A:
    def _read(self, root):
        return hu.read_cursor_usage(root, consented=True)

    def _single(self, root):
        paths = sorted(root.glob("*/runs.ndjson"))
        row = json.loads(paths[0].read_text().splitlines()[0])
        for path in paths[1:]:
            path.unlink()
        self._write(paths[0], row)
        return paths[0], row

    def _write(self, path, *rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_real_census_disjoint_identity_and_pins(self, cursor_store, monkeypatch):
        def forbidden(*_args):
            pytest.fail("Cursor counters must never use the inclusive normalizer")

        monkeypatch.setattr(hu, "_normalize_inclusive_counters", forbidden)
        result = self._read(cursor_store)
        assert result.complete
        assert sum(tu.sum_bucket(day) for day in result.tokens_by_day.values()) == 32_729_640
        assert set(result.hosts) == {"grok"}
        for path in cursor_store.glob("*/runs.ndjson"):
            for line in path.read_text().splitlines():
                record = json.loads(line)
                usage = record["usage"]
                assert (
                    sum(
                        usage[k]
                        for k in (
                            "inputTokens",
                            "outputTokens",
                            "cacheReadTokens",
                            "cacheWriteTokens",
                        )
                    )
                    == usage["totalTokens"]
                )
                assert 0 <= usage["reasoningTokens"] <= usage["outputTokens"]
        contract = (FIXTURES / "cursor/CONTRACT.md").read_text()
        assert hu.CURSOR_USAGE_CENSUS_HOST_VERSION == "2026.09.18-9a7762b"
        assert hu.CURSOR_USAGE_CENSUS_CONDUCTOR_VERSION == "0.87.3"
        assert hu.CURSOR_USAGE_CENSUS_HOST_VERSION in contract
        assert hu.CURSOR_USAGE_CENSUS_CONDUCTOR_VERSION in contract
        assert hu.CURSOR_CACHE_PATH.stat().st_mode & 0o777 == 0o600
        cache = json.loads(hu.CURSOR_CACHE_PATH.read_text())
        assert cache["version"] == hu.CACHE_VERSION
        assert len(cache["runs"]) == 3
        assert all(len(key) == 64 for key in cache["runs"])

    def test_no_consent_performs_no_path_io(self, monkeypatch):
        def forbidden(*_args, **_kwargs):
            pytest.fail("unconsented reader touched a path")

        monkeypatch.setattr(Path, "lstat", forbidden)
        monkeypatch.setattr(Path, "open", forbidden)
        monkeypatch.setattr(Path, "mkdir", forbidden)
        result = hu.read_cursor_usage()
        assert not result.complete
        assert result.reason == "no_metadata_ledger"

    def test_bare_cli_absence_is_not_completed_empty(self):
        result = hu.read_cursor_usage(consented=True)
        assert not result.complete
        assert result.reason == "no_metadata_ledger"
        assert not hu.CURSOR_CACHE_PATH.exists()

    def test_expired_budget_does_not_touch_either_store(self, monkeypatch):
        def forbidden(*_args, **_kwargs):
            pytest.fail("an already-expired Cursor read touched disk")

        monkeypatch.setattr(Path, "lstat", forbidden)
        monkeypatch.setattr(Path, "mkdir", forbidden)
        result = hu.read_cursor_usage(deadline=time.monotonic() - 1, consented=True)
        assert not result.complete
        assert result.reason == "deadline"

    def test_running_rewrite_and_finished_revision_replace(self, cursor_store):
        path, row = self._single(cursor_store)
        original = json.loads(json.dumps(row))
        row.update(status="running", usage=None, endedAt=None)
        self._write(path, row)
        assert self._read(cursor_store).hosts == {}
        self._write(path, original)
        first = self._read(cursor_store)
        assert self._read(cursor_store) == first
        original["model"]["id"] = "gpt-6-astra"
        original["endedAt"] += 86_400_000
        original["usage"]["inputTokens"] += 100
        original["usage"]["totalTokens"] += 100
        self._write(path, original)
        revised = self._read(cursor_store)
        assert revised.complete
        assert set(revised.hosts) == {"codex"}
        assert set(revised.tokens_by_day) == {"2026-09-23"}
        assert tu.sum_bucket(revised.tokens_by_day["2026-09-23"]) == 25_891_871
        assert len(json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"]) == 1

    def test_completion_utc_day_owns_midnight_turn(self, cursor_store):
        path, row = self._single(cursor_store)
        row["startedAt"] = int(
            datetime(2026, 9, 22, 23, 59, tzinfo=timezone.utc).timestamp() * 1000
        )
        row["endedAt"] = row["startedAt"] + 120_000
        self._write(path, row)
        assert set(self._read(cursor_store).tokens_by_day) == {"2026-09-23"}

    def test_far_future_ended_at_is_malformed(self, cursor_store):
        path, row = self._single(cursor_store)
        row["endedAt"] = int(datetime(2026, 9, 25, tzinfo=timezone.utc).timestamp() * 1000)
        self._write(path, row)
        result = self._read(cursor_store)
        assert result.reason == "malformed"
        assert not result.complete
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == {}

    def test_one_day_ahead_still_buckets(self, cursor_store):
        path, row = self._single(cursor_store)
        row["endedAt"] = int(datetime(2026, 9, 24, 18, tzinfo=timezone.utc).timestamp() * 1000)
        self._write(path, row)
        assert set(self._read(cursor_store).tokens_by_day) == {"2026-09-24"}

    def test_seconds_scale_ended_at_is_malformed_not_reaped(self, cursor_store):
        path, row = self._single(cursor_store)
        row["endedAt"] = int(datetime(2026, 9, 23, tzinfo=timezone.utc).timestamp())
        self._write(path, row)
        result = self._read(cursor_store)
        assert result.reason == "malformed"
        assert not result.complete
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == {}

    def test_sqlite_store_listing_counts_only_hex_directories_by_name(self, cursor_store, tmp_path):
        sqlite_store = cursor_store / "0123456789abcdef"
        sqlite_store.mkdir()
        (sqlite_store / "index.db").write_bytes(b"SQLite format 3\x00")
        both = cursor_store / "fedcba9876543210"
        both.mkdir()
        (both / "index.db").touch()
        (both / "runs.ndjson").touch()
        not_conductors = cursor_store / "newer-conductor"  # not a sha256(workspace)[:16] name
        not_conductors.mkdir()
        (not_conductors / "index.db").touch()
        (cursor_store / "stray-file").touch()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "index.db").touch()
        (cursor_store / "0000000000000000").symlink_to(elsewhere)
        assert hu.cursor_sqlite_stores(cursor_store) == ["0123456789abcdef", "fedcba9876543210"]
        assert hu.unread_sqlite_stores(cursor_store) == 2  # SQLite-only and migrating
        assert hu.unread_sqlite_stores(cursor_store, {"0123456789abcdef": "read"}) == 1
        assert hu.unread_sqlite_stores(tmp_path / "absent") == 0

    def test_store_scan_errors_report_unknown(self, cursor_store, monkeypatch):
        class Entry:
            name = "0123456789abcdef"
            path = str(cursor_store / "0123456789abcdef")

            def is_dir(self, follow_symlinks=True):
                raise PermissionError("stat failed")

        class Entries(list):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(hu.os, "scandir", lambda root: Entries([Entry()]))
        assert hu.unread_sqlite_stores(cursor_store) is None

    @pytest.mark.parametrize("status", ["cancelled", "error", "queued"])
    def test_counterless_unfinished_rows_contribute_nothing(self, cursor_store, status):
        path, row = self._single(cursor_store)
        baseline = self._read(cursor_store)
        assert baseline.complete
        other = {**row, "runId": "other-" + status, "status": status, "usage": None}
        if status == "queued":
            other.pop("endedAt", None)
        self._write(path, row, other)
        result = self._read(cursor_store)
        assert result.complete
        assert result.hosts == baseline.hosts
        self._write(path, row, {**other, "usageRef": "usage-ref"})
        assert self._read(cursor_store).reason == "unsupported"

    @pytest.mark.parametrize("status", ["completed", "error", "cancelled", "renamed", None, {}])
    def test_unknown_or_newly_billable_status_refuses(self, cursor_store, status):
        path, row = self._single(cursor_store)
        row["status"] = status
        self._write(path, row)
        result = self._read(cursor_store)
        assert not result.complete
        assert result.reason == "unsupported"

    @pytest.mark.parametrize(
        "mutation", ["missing", "null", "identity", "reasoning", "bool", "model"]
    )
    def test_malformed_usage_never_becomes_absence(self, cursor_store, mutation):
        path, row = self._single(cursor_store)
        if mutation == "missing":
            del row["usage"]
        elif mutation == "null":
            row["usage"] = None
        elif mutation == "identity":
            row["usage"]["totalTokens"] += 1
        elif mutation == "reasoning":
            row["usage"]["reasoningTokens"] = row["usage"]["outputTokens"] + 1
        elif mutation == "bool":
            row["usage"]["inputTokens"] = True
        else:
            row["model"] = None
        self._write(path, row)
        result = self._read(cursor_store)
        assert result.reason == "malformed"
        assert not result.complete

    @pytest.mark.parametrize(
        ("model_id", "params", "reason"),
        [
            ("grok-4.7", [], "unsupported"),
            (
                "grok-4.7",
                [{"id": "fast", "value": "true"}, {"id": "fast", "value": "false"}],
                "unsupported",
            ),
            ("grok-4.7", [{"id": "fast", "value": True}], "unsupported"),
            ("gpt-6-astra", None, "malformed"),
        ],
    )
    def test_model_parameter_contract_refuses_unobserved_shapes(
        self, cursor_store, model_id, params, reason
    ):
        path, row = self._single(cursor_store)
        row["model"]["id"] = model_id
        row["model"]["params"] = params
        self._write(path, row)
        result = self._read(cursor_store)
        assert not result.complete
        assert result.reason == reason
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == {}

    def test_running_record_with_usage_reference_is_unsupported(self, cursor_store):
        path, row = self._single(cursor_store)
        row.update(status="running", usage=None, usageRef="pending")
        self._write(path, row)
        result = self._read(cursor_store)
        assert not result.complete
        assert result.reason == "unsupported"

    def test_invalid_run_identity_and_duplicate_within_file_refuse(self, cursor_store):
        path, row = self._single(cursor_store)
        row["runId"] = ""
        self._write(path, row)
        assert self._read(cursor_store).reason == "malformed"
        self._write(path, row | {"runId": "stable"}, row | {"runId": "stable"})
        assert self._read(cursor_store).reason == "malformed"

    def test_non_directory_store_is_unsupported_without_overwriting_cache(self, tmp_path):
        root = tmp_path / "not-a-directory"
        root.write_text("x")
        result = hu.read_cursor_usage(root, consented=True)
        assert not result.complete
        assert result.reason == "unsupported"
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["last_reason"] == "unsupported"

    def test_symlinked_workspace_ledger_is_ignored(self, cursor_store, tmp_path):
        for path in cursor_store.glob("*/runs.ndjson"):
            path.unlink()
        outside = tmp_path / "outside.ndjson"
        row = json.loads((FIXTURES / "cursor/session-1/runs.ndjson").read_text().splitlines()[0])
        outside.write_text(json.dumps(row) + "\n")
        linked = cursor_store / "linked"
        linked.mkdir()
        (linked / "runs.ndjson").symlink_to(outside)
        result = self._read(cursor_store)
        assert result.complete
        assert result.tokens_by_day == {}
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == {}

    def test_pruned_runs_survive_including_missing_store(self, cursor_store):
        first = self._read(cursor_store)
        for path in cursor_store.glob("*/runs.ndjson"):
            path.unlink()
        assert self._read(cursor_store) == first
        shutil.rmtree(cursor_store)
        assert self._read(cursor_store) == first

    def test_broken_store_disappearing_never_becomes_nothing_ever(self, cursor_store):
        path, row = self._single(cursor_store)
        row["status"] = "renamed-terminal"
        path.write_text(json.dumps(row) + "\n")
        assert self._read(cursor_store).reason == "unsupported"
        shutil.rmtree(cursor_store)
        assert self._read(cursor_store).reason == "unsupported"

    def test_retention_is_reader_owned_and_reverses_revisions(self, cursor_store):
        path, row = self._single(cursor_store)
        assert self._read(cursor_store).complete
        row["endedAt"] = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
        self._write(path, row)
        assert self._read(cursor_store).hosts == {}
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == {}
        assert hu.CURSOR_HOST_CACHE_RETENTION_DAYS == 90

    def test_torn_later_file_keeps_earlier_stable_runs(self, cursor_store):
        paths = sorted(cursor_store.glob("*/runs.ndjson"))
        first, second = paths[0], paths[1]
        kept = [json.loads(line) for line in first.read_text().splitlines()]
        second.write_bytes(second.read_bytes() + b'{"runId":"unfinished')
        assert self._read(cursor_store).reason == "malformed"
        cached = json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"]
        assert len(cached) == len(kept)
        assert all(run["usage"] is not None for run in cached.values())

    def test_success_then_malformed_store_loss_keeps_the_blocker(self, cursor_store):
        assert self._read(cursor_store).complete
        path, row = self._single(cursor_store)
        row["usage"] = {**row["usage"], "totalTokens": row["usage"]["totalTokens"] + 1}
        self._write(path, row)
        assert self._read(cursor_store).reason == "malformed"
        shutil.rmtree(cursor_store)
        result = self._read(cursor_store)
        assert result.reason == "malformed"
        assert not result.complete

    def test_success_then_unsupported_store_loss_keeps_the_blocker(self, cursor_store):
        assert self._read(cursor_store).complete
        path, row = self._single(cursor_store)
        row["status"] = "renamed-terminal"
        self._write(path, row)
        assert self._read(cursor_store).reason == "unsupported"
        shutil.rmtree(cursor_store)
        result = self._read(cursor_store)
        assert result.reason == "unsupported"
        assert not result.complete

    def test_torn_file_never_commits_prefix(self, cursor_store):
        path, _ = self._single(cursor_store)
        path.write_bytes(path.read_bytes() + b'{"runId":"unfinished')
        result = self._read(cursor_store)
        assert result.reason == "malformed"
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == {}

    def test_mutation_during_read_discards_revision(self, cursor_store, monkeypatch):
        path, row = self._single(cursor_store)
        assert self._read(cursor_store).complete
        prior = json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"]
        stat_fn = hu._regular_stat
        calls = 0

        def mutate(target):
            nonlocal calls
            calls += 1
            if calls == 2:
                row["usage"]["inputTokens"] += 100
                row["usage"]["totalTokens"] += 100
                self._write(path, row)
            return stat_fn(target)

        monkeypatch.setattr(hu, "_regular_stat", mutate)
        assert self._read(cursor_store).reason == "stale"
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == prior

    def test_short_passes_preserve_progress_but_need_not_converge(self, cursor_store, monkeypatch):
        original = hu._read_cursor_file
        parsed = False

        def read(path, deadline):
            nonlocal parsed
            result = original(path, deadline)
            parsed = True
            return result

        with monkeypatch.context() as limited:
            limited.setattr(hu, "_read_cursor_file", read)
            limited.setattr(hu, "_expired", lambda deadline: parsed)
            assert self._read(cursor_store).reason == "deadline"
            first = json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"]
            assert len(first) == 2
            parsed = False
            assert self._read(cursor_store).reason == "deadline"
            assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == first
        result = self._read(cursor_store)
        assert result.complete
        assert sum(tu.sum_bucket(day) for day in result.tokens_by_day.values()) == 32_729_640

    @pytest.mark.parametrize(
        "content", [b"", b"garbage", b"[]", b"{}", b'{"version":99,"runs":{}}']
    )
    def test_corrupt_authoritative_cache_is_not_reset(self, cursor_store, content):
        hu.CURSOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        hu.CURSOR_CACHE_PATH.write_bytes(content)
        assert not self._read(cursor_store).complete
        assert hu.CURSOR_CACHE_PATH.read_bytes() == content

    def test_malformed_cached_run_is_not_rewritten(self, cursor_store):
        assert self._read(cursor_store).complete
        cache = json.loads(hu.CURSOR_CACHE_PATH.read_text())
        first_run = next(iter(cache["runs"].values()))
        first_run["usage"]["outputTokens"] = True
        hu.CURSOR_CACHE_PATH.write_text(json.dumps(cache))
        malformed_bytes = hu.CURSOR_CACHE_PATH.read_bytes()

        result = self._read(cursor_store)

        assert not result.complete
        assert result.reason == "malformed"
        assert hu.CURSOR_CACHE_PATH.read_bytes() == malformed_bytes

    def test_cursor_diag_reports_malformed_cache_and_lock_contention(self, cursor_store):
        assert self._read(cursor_store).complete
        cache = json.loads(hu.CURSOR_CACHE_PATH.read_text())
        first_run = next(iter(cache["runs"].values()))
        first_run["usage"]["outputTokens"] = True
        hu.CURSOR_CACHE_PATH.write_text(json.dumps(cache))

        malformed = hu.cursor_usage_diag()
        assert malformed["cache_state"] == "unreadable"
        assert malformed["last_reason"] == "malformed"

        with hu.CURSOR_CACHE_PATH.open("rb") as fp:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            contended = hu.cursor_usage_diag()
        assert contended["cache_state"] == "unreadable"
        assert contended["last_reason"] == "locked"

    def test_ledger_replacement_between_stat_and_open_is_rejected(self, cursor_store, monkeypatch):
        path, _row = self._single(cursor_store)
        original_open = os.open
        replaced = False

        def replace_before_open(target, flags, mode=0o777, *args, **kwargs):
            nonlocal replaced
            if Path(target) == path and not replaced:
                replaced = True
                original = path.with_name(path.name + ".original")
                path.replace(original)
                path.write_text(original.read_text())
            return original_open(target, flags, mode, *args, **kwargs)

        monkeypatch.setattr(hu.os, "open", replace_before_open)

        with pytest.raises(hu._ReadFailure) as exc_info:
            hu._read_cursor_file(path, time.monotonic() + 5)

        assert replaced
        assert exc_info.value.reason == "stale"

    def test_durable_failure_preserves_previous_bytes_and_refuses_publication(
        self, cursor_store, monkeypatch
    ):
        from mind_meld import fsutil

        assert self._read(cursor_store).complete
        before = hu.CURSOR_CACHE_PATH.read_bytes()

        def fail(_fd):
            raise OSError(errno.EIO, "injected fsync failure")

        monkeypatch.setattr(fsutil, "_fsync_fd", fail)
        assert self._read(cursor_store).reason == "io_error"
        assert hu.CURSOR_CACHE_PATH.read_bytes() == before
        assert not list(hu.CURSOR_CACHE_PATH.parent.glob("*.tmp"))

    def test_sibling_lock_refuses_without_losing_history(self, cursor_store):
        assert self._read(cursor_store).complete
        before = hu.CURSOR_CACHE_PATH.read_bytes()
        with hu.CURSOR_CACHE_PATH.with_name(hu.CURSOR_CACHE_PATH.name + ".lock").open("rb") as fp:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert self._read(cursor_store).reason == "locked"
        assert hu.CURSOR_CACHE_PATH.read_bytes() == before

    def test_never_opens_content_siblings_or_bare_cli_database(self, cursor_store, monkeypatch):
        original = os.open
        opened = []

        def track(path, flags, mode=0o777, *args, **kwargs):
            opened.append(Path(path).name)
            return original(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(os, "open", track)
        assert self._read(cursor_store).complete
        assert "runs.ndjson" in opened
        assert {
            "agents.ndjson",
            "store.db",
            "checkpoints.ndjson",
            "run_events.ndjson",
            "prompt_history.json",
        }.isdisjoint(opened)

    def test_warming_calls_cursor_with_consent(self, monkeypatch):
        seen = []

        def read(root, *, deadline, consented):
            seen.append((root, deadline, consented))
            return hu.HostUsageResult({}, complete=True)

        monkeypatch.setattr(hu, "read_cursor_usage", read)
        assert host_registry.warm_host_cache_inline(reader="cursor").complete
        assert seen[0][2] is True

    def test_partially_learned_history_survives_store_pruning(self, cursor_store, monkeypatch):
        first = sorted(cursor_store.glob("*/runs.ndjson"))[0]
        reader = hu._read_cursor_file

        def fail_second(path, deadline):
            if path != first:
                raise hu._ReadFailure("deadline")
            return reader(path, deadline)

        with monkeypatch.context() as limited:
            limited.setattr(hu, "_read_cursor_file", fail_second)
            assert self._read(cursor_store).reason == "deadline"
        shutil.rmtree(cursor_store)
        result = self._read(cursor_store)
        assert not result.complete
        assert result.reason == "deadline"
        cache = json.loads(hu.CURSOR_CACHE_PATH.read_text())
        assert cache["complete_once"] is False
        assert sum(tu.sum_bucket(run["usage"]) for run in cache["runs"].values()) == 30_871_121

    def test_duplicate_ids_across_workspaces_count_once_and_conflicts_refuse(self, cursor_store):
        path, row = self._single(cursor_store)
        duplicate = cursor_store / "copied" / "runs.ndjson"
        duplicate.parent.mkdir()
        self._write(duplicate, row)
        result = self._read(cursor_store)
        assert result.complete
        assert sum(tu.sum_bucket(day) for day in result.tokens_by_day.values()) == 25_891_771
        row["usage"]["inputTokens"] += 1
        row["usage"]["totalTokens"] += 1
        self._write(duplicate, row)
        assert self._read(cursor_store).reason == "malformed"

    def test_rejected_running_copy_does_not_erase_cached_history(self, cursor_store):
        path, row = self._single(cursor_store)
        assert self._read(cursor_store).complete
        prior = json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"]
        running = dict(row)
        running["status"] = "running"
        running["usage"] = None
        early = cursor_store / "aaa" / "runs.ndjson"
        early.parent.mkdir()
        self._write(early, running)
        assert self._read(cursor_store).reason == "malformed"
        assert json.loads(hu.CURSOR_CACHE_PATH.read_text())["runs"] == prior

    def test_fifo_substituted_for_ledger_does_not_block(self, tmp_path, monkeypatch):
        fifo = tmp_path / "runs.ndjson"
        os.mkfifo(fifo)
        original_stat = hu._regular_stat

        def pretend_regular(path):
            try:
                return original_stat(path)
            except hu._ReadFailure:
                return path.lstat()

        monkeypatch.setattr(hu, "_regular_stat", pretend_regular)
        started = time.monotonic()
        with pytest.raises(hu._ReadFailure):
            hu._read_cursor_file(fifo, started + 5)
        assert time.monotonic() - started < 2

    def test_unobserved_models_keep_existing_family_classification(self, cursor_store):
        path, row = self._single(cursor_store)
        row["model"]["id"] = "composer-2.5"
        self._write(path, row)
        assert set(self._read(cursor_store).hosts) == {"other"}
        row["model"]["id"] = "auto"
        self._write(path, row)
        assert set(self._read(cursor_store).hosts) == {"other"}


# Captured from the pre-70A reader (HEAD 98e9a69) by running ``_read_cursor_file``
# over the real redacted census rows and synthetic mutations of one of them.
# Splitting ``_cursor_run`` into shared validators plus per-format adapters must
# not move a byte of the legacy JSONL projection: ``staged`` is run key -> day,
# model, counters and partial flag (or None for a non-contributing row),
# ``aliases`` is requestId hash -> run key, ``failure`` is the refusal reason.
_CURSOR_JSONL_GOLDEN = json.loads(
    r"""
{
 "cache-write-partial": {
  "aliases": {},
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": {
    "day": "2026-09-22",
    "model": "grok-4.7",
    "partial": true,
    "usage": {
     "cache_create": 10,
     "cache_read": 12505856,
     "input": 13345729,
     "output": 40186
    }
   }
  }
 },
 "cancelled": {
  "aliases": {},
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": null
  }
 },
 "error": {
  "aliases": {},
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": null
  }
 },
 "fail-bool-counter": {
  "failure": "malformed"
 },
 "fail-cancelled-counters": {
  "failure": "unsupported"
 },
 "fail-cancelled-usage-ref": {
  "failure": "unsupported"
 },
 "fail-empty-run-id": {
  "failure": "malformed"
 },
 "fail-fast-odd": {
  "failure": "unsupported"
 },
 "fail-fast-two": {
  "failure": "unsupported"
 },
 "fail-grok-no-fast": {
  "failure": "unsupported"
 },
 "fail-identity": {
  "failure": "malformed"
 },
 "fail-missing-usage": {
  "failure": "malformed"
 },
 "fail-null-usage-no-ref": {
  "failure": "malformed"
 },
 "fail-params-not-list": {
  "failure": "malformed"
 },
 "fail-reasoning": {
  "failure": "malformed"
 },
 "fail-renamed-status": {
  "failure": "unsupported"
 },
 "fail-seconds-ended-at": {
  "failure": "malformed"
 },
 "fast-true": {
  "aliases": {},
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": {
    "day": "2026-09-22",
    "model": "grok-4.7-fast",
    "partial": false,
    "usage": {
     "cache_create": 0,
     "cache_read": 12505856,
     "input": 13345729,
     "output": 40186
    }
   }
  }
 },
 "non-grok-no-fast": {
  "aliases": {},
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": {
    "day": "2026-09-22",
    "model": "gpt-6-astra",
    "partial": false,
    "usage": {
     "cache_create": 0,
     "cache_read": 12505856,
     "input": 13345729,
     "output": 40186
    }
   }
  }
 },
 "queued": {
  "aliases": {},
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": null
  }
 },
 "request-alias": {
  "aliases": {
   "9456bdfa12ea76959c94a3572f5d91c73d838622df0a8d9b4e815c276c6b7880":
    "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320"
  },
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": {
    "day": "2026-09-22",
    "model": "grok-4.7",
    "partial": false,
    "usage": {
     "cache_create": 0,
     "cache_read": 12505856,
     "input": 13345729,
     "output": 40186
    }
   }
  }
 },
 "session-1": {
  "aliases": {},
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": {
    "day": "2026-09-22",
    "model": "grok-4.7",
    "partial": false,
    "usage": {
     "cache_create": 0,
     "cache_read": 12505856,
     "input": 13345729,
     "output": 40186
    }
   },
   "f80ff27cb7faf413f94fd827200bfdeac81cf456bf0dcc2d41383f5c6abb7821": {
    "day": "2026-09-23",
    "model": "grok-4.7",
    "partial": false,
    "usage": {
     "cache_create": 0,
     "cache_read": 2343424,
     "input": 2618647,
     "output": 17279
    }
   }
  }
 },
 "session-3": {
  "aliases": {},
  "staged": {
   "b127355d22419f6e560369bb6c311c94425c7f8b2d67bf926fc879067cd75d3f": {
    "day": "2026-09-23",
    "model": "grok-4.7",
    "partial": false,
    "usage": {
     "cache_create": 0,
     "cache_read": 825088,
     "input": 1006863,
     "output": 26568
    }
   }
  }
 },
 "usage-ref-placeholder": {
  "aliases": {},
  "staged": {
   "bf7eda23e0443b077627f712759c4bc415f850d60c5219925f8026eed5f37320": {
    "day": "2026-09-22",
    "model": "grok-4.7",
    "partial": true,
    "usage": null
   }
  }
 }
}
"""
)


class TestCursorJsonlGolden70A:
    """The CRITICAL regression gate for the 70A shared-validator split."""

    @staticmethod
    def _cases():
        fixtures = FIXTURES / "cursor"
        base = json.loads((fixtures / "session-1/runs.ndjson").read_text().splitlines()[0])

        def variant(**changes):
            row = json.loads(json.dumps(base))
            for name, value in changes.items():
                if value is KeyError:
                    row.pop(name, None)
                else:
                    row[name] = value
            return row

        def usage(**changes):
            return {**base["usage"], **changes}

        total = base["usage"]["totalTokens"]
        grok = {"id": "grok-4.7", "params": [{"id": "fast", "value": "true"}]}
        return {
            "session-1": None,
            "session-3": None,
            "queued": variant(status="queued", usage=None, endedAt=KeyError),
            "cancelled": variant(status="cancelled", usage=None),
            "error": variant(status="error", usage=None),
            "usage-ref-placeholder": variant(usage=None, usageRef="ref-1"),
            "fast-true": variant(model=grok),
            "cache-write-partial": variant(
                usage=usage(cacheWriteTokens=10, totalTokens=total + 10)
            ),
            "request-alias": variant(requestId="req-1"),
            "non-grok-no-fast": variant(model={"id": "gpt-6-astra", "params": []}),
            "fail-renamed-status": variant(status="renamed"),
            "fail-grok-no-fast": variant(model={"id": "grok-4.7", "params": []}),
            "fail-params-not-list": variant(model={"id": "grok-4.7", "params": None}),
            "fail-fast-two": variant(
                model={
                    "id": "grok-4.7",
                    "params": [
                        {"id": "fast", "value": "true"},
                        {"id": "fast", "value": "false"},
                    ],
                }
            ),
            "fail-fast-odd": variant(
                model={"id": "grok-4.7", "params": [{"id": "fast", "value": "maybe"}]}
            ),
            "fail-identity": variant(usage=usage(totalTokens=total + 1)),
            "fail-reasoning": variant(
                usage=usage(reasoningTokens=base["usage"]["outputTokens"] + 1)
            ),
            "fail-missing-usage": variant(usage=KeyError),
            "fail-null-usage-no-ref": variant(usage=None),
            "fail-cancelled-usage-ref": variant(status="cancelled", usage=None, usageRef="ref"),
            "fail-cancelled-counters": variant(status="cancelled"),
            "fail-seconds-ended-at": variant(endedAt=1_790_109_894),
            "fail-bool-counter": variant(usage=usage(inputTokens=True)),
            "fail-empty-run-id": variant(runId=""),
        }

    def test_jsonl_projections_are_byte_identical_to_the_pre_split_reader(self, tmp_path):
        actual = {}
        for name, row in self._cases().items():
            path = tmp_path / f"{name}.ndjson"
            if row is None:
                path.write_bytes((FIXTURES / "cursor" / name / "runs.ndjson").read_bytes())
            else:
                path.write_text(json.dumps(row) + "\n")
            try:
                staged, aliases = hu._read_cursor_file(path, time.monotonic() + 5)
                actual[name] = {"staged": staged, "aliases": aliases}
            except hu._ReadFailure as exc:
                actual[name] = {"failure": exc.reason}
        assert set(actual) == set(_CURSOR_JSONL_GOLDEN)
        assert json.dumps(actual, sort_keys=True) == json.dumps(
            _CURSOR_JSONL_GOLDEN, sort_keys=True
        )


def test_not_a_directory_on_speculative_probe_is_skipped(tmp_path: Path) -> None:
    """T1-2: ENOTDIR on the speculative path is absence, not io_error."""
    parent = tmp_path / "file-not-dir"
    parent.write_text("x", encoding="utf-8")
    assert hu._is_regular_non_symlink(parent / "updates.jsonl") is False


def test_codex_rollout_vanishing_between_iterdir_and_lstat_is_skipped(
    isolated_cache: Path, tmp_path: Path, monkeypatch
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

    result = codex_host.read_codex_usage(root)

    assert result.complete is True
    assert result.hosts["codex"]["2026-08-15"]["input"] == 10
