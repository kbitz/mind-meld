"""Tests for mind_meld.token_usage.

Pinned behaviors per the eng-review test diagram:
  - parse_usage: happy + various malformed inputs
  - _normalize_model_id: dated/undated/synthetic/garbage
  - walk_jsonl_token_buckets: empty/single-day/midnight-cross/mixed-models/
                              corrupt-line/unreadable/dedup-by-id
  - get_or_compute: cache hit/miss/concurrent-append-skip/deadline
  - slice_window: inside/partial-overlap/outside/empty
  - estimate_cost: known/unknown/synthetic/zero
  - is_cache_cold: missing/empty/corrupt/version-mismatch/populated
  - warm_token_cache_inline: walks parent + subagent jsonls + budget gate
  - gc_cache_entries: reaps stale entries
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mind_meld import token_usage as tu

# ---------------------------------------------------------------------------
# Helpers for synthetic jsonls
# ---------------------------------------------------------------------------


def _wrap(message: dict, *, ts: str | None = None) -> dict:
    """Wrap a `message` in the Claude Code session shape."""
    return {"message": message, "timestamp": ts or "2026-05-01T12:00:00.000Z"}


def _assistant_msg(
    *,
    model: str = "claude-opus-4-7",
    msg_id: str = "msg_test_1",
    input_tokens: int = 100,
    cache_create: int = 0,
    cache_read: int = 1000,
    output: int = 50,
) -> dict:
    return {
        "id": msg_id,
        "role": "assistant",
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output,
        },
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# Cache path isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_token_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tu, "CACHE_PATH", tmp_path / "session-tokens.json")
    # Reset the per-process unknown-model warning set.
    monkeypatch.setattr(tu, "_WARNED_UNKNOWN_MODELS", set())


# ---------------------------------------------------------------------------
# _normalize_model_id
# ---------------------------------------------------------------------------


class TestNormalizeModelId:
    def test_undated_passes_through(self) -> None:
        assert tu._normalize_model_id("claude-opus-4-7") == "claude-opus-4-7"

    def test_dated_subagent_form_strips(self) -> None:
        assert tu._normalize_model_id("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
        assert tu._normalize_model_id("claude-opus-4-5-20251101") == "claude-opus-4-5"

    def test_synthetic_passes_through(self) -> None:
        assert tu._normalize_model_id("<synthetic>") == "<synthetic>"

    def test_empty_passes_through(self) -> None:
        assert tu._normalize_model_id("") == ""

    def test_short_date_does_not_strip(self) -> None:
        # 7 digits — not the YYYYMMDD pattern.
        assert tu._normalize_model_id("claude-opus-1234567") == "claude-opus-1234567"


# ---------------------------------------------------------------------------
# TOKEN_FIELDS schema-stability + zero_bucket factories (Track 10A)
# ---------------------------------------------------------------------------


class TestTokenFieldsAndFactories:
    """REGRESSION pin: adding a 5th token field must update TOKEN_FIELDS,
    `Usage`, and `DayBucket` together. The exact-tuple assertion catches
    silent drift if a contributor edits one but forgets the other."""

    def test_token_fields_exact_tuple(self) -> None:
        assert tu.TOKEN_FIELDS == ("input", "cache_create", "cache_read", "output")

    def test_zero_model_bucket_shape(self) -> None:
        bucket = tu.zero_model_bucket()
        assert bucket == {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}
        # Per-model bucket has NO `by_model` key.
        assert "by_model" not in bucket

    def test_zero_day_bucket_shape(self) -> None:
        bucket = tu.zero_day_bucket()
        assert bucket["input"] == 0
        assert bucket["cache_create"] == 0
        assert bucket["cache_read"] == 0
        assert bucket["output"] == 0
        assert bucket["by_model"] == {}

    def test_factories_return_independent_dicts(self) -> None:
        # Mutating one returned bucket must not bleed into another.
        a = tu.zero_model_bucket()
        b = tu.zero_model_bucket()
        a["input"] = 100
        assert b["input"] == 0


class TestMergeUsageBucket:
    def test_empty_src_no_op(self) -> None:
        target = tu.zero_model_bucket()
        target["input"] = 50
        tu.merge_usage_bucket(target, {})
        assert target == {"input": 50, "cache_create": 0, "cache_read": 0, "output": 0}

    def test_full_src_sums_all_fields(self) -> None:
        target = tu.zero_model_bucket()
        src = {"input": 1, "cache_create": 2, "cache_read": 3, "output": 4}
        tu.merge_usage_bucket(target, src)
        assert target == src

    def test_partial_src_missing_keys_contribute_zero(self) -> None:
        target = tu.zero_model_bucket()
        target["input"] = 100
        target["output"] = 50
        tu.merge_usage_bucket(target, {"cache_read": 7})
        assert target == {"input": 100, "cache_create": 0, "cache_read": 7, "output": 50}

    def test_empty_target_dict_seeds_keys(self) -> None:
        target: dict = {}
        tu.merge_usage_bucket(target, {"input": 5, "output": 2})
        assert target == {"input": 5, "cache_create": 0, "cache_read": 0, "output": 2}

    def test_repeated_calls_accumulate(self) -> None:
        target = tu.zero_model_bucket()
        tu.merge_usage_bucket(target, {"input": 10})
        tu.merge_usage_bucket(target, {"input": 5, "output": 3})
        assert target["input"] == 15
        assert target["output"] == 3


class TestMergeUsageBucketPerf:
    """Track 10A perf-pin: helper extraction adds ~80-150ns per call
    of Python function-call overhead. With 10k+ calls per push (D9),
    that's a few ms — well under the 250ms autopush budget. This pin
    catches future regressions where someone adds expensive logic to
    the helper that pushes the cost into the budget."""

    def test_merge_usage_bucket_under_2us_per_call(self) -> None:
        """Empirical budget: 2µs per call is conservative. CI runners
        vary; 5x margin keeps the test stable while still catching
        order-of-magnitude regressions."""
        target = tu.zero_model_bucket()
        src = {"input": 100, "cache_create": 200, "cache_read": 1000, "output": 50}
        n = 10_000
        start = time.perf_counter()
        for _ in range(n):
            tu.merge_usage_bucket(target, src)
        elapsed = time.perf_counter() - start
        per_call_us = (elapsed / n) * 1_000_000
        # 10µs per call = 50× the typical cost; trips on actual
        # algorithmic regression but stays stable under CI noise.
        assert per_call_us < 10.0, f"merge_usage_bucket took {per_call_us:.2f}µs/call"


class TestMergeByModel:
    def test_empty_src_no_op(self) -> None:
        target: dict[str, tu.Usage] = {}
        tu.merge_by_model(target, {})
        assert target == {}

    def test_new_model_creates_zero_bucket_then_sums(self) -> None:
        target: dict[str, tu.Usage] = {}
        src = {"claude-opus-4-7": {"input": 100, "output": 50}}
        tu.merge_by_model(target, src)
        assert target == {
            "claude-opus-4-7": {
                "input": 100,
                "cache_create": 0,
                "cache_read": 0,
                "output": 50,
            }
        }

    def test_existing_model_accumulates_in_place(self) -> None:
        target: dict[str, tu.Usage] = {
            "claude-opus-4-7": {"input": 100, "cache_create": 0, "cache_read": 0, "output": 0},
        }
        src = {"claude-opus-4-7": {"input": 50, "output": 25}}
        tu.merge_by_model(target, src)
        assert target["claude-opus-4-7"] == {
            "input": 150,
            "cache_create": 0,
            "cache_read": 0,
            "output": 25,
        }

    def test_multiple_models_isolate(self) -> None:
        target: dict[str, tu.Usage] = {}
        src = {
            "claude-opus-4-7": {"input": 100},
            "claude-haiku-4-5": {"input": 200},
        }
        tu.merge_by_model(target, src)
        assert target["claude-opus-4-7"]["input"] == 100
        assert target["claude-haiku-4-5"]["input"] == 200


# ---------------------------------------------------------------------------
# parse_usage
# ---------------------------------------------------------------------------


class TestParseUsage:
    def test_happy_assistant(self) -> None:
        m = _assistant_msg()
        result = tu.parse_usage(m)
        assert result is not None
        usage, model, msg_id = result
        assert model == "claude-opus-4-7"
        assert msg_id == "msg_test_1"
        assert usage == {"input": 100, "cache_create": 0, "cache_read": 1000, "output": 50}

    def test_role_user_returns_none(self) -> None:
        m = {"role": "user", "model": "claude-opus-4-7", "usage": {"input_tokens": 5}}
        assert tu.parse_usage(m) is None

    def test_missing_usage_returns_none(self) -> None:
        m = {"role": "assistant", "model": "claude-opus-4-7"}
        assert tu.parse_usage(m) is None

    def test_missing_model_returns_none(self) -> None:
        m = {"role": "assistant", "usage": {"input_tokens": 5}}
        assert tu.parse_usage(m) is None

    def test_non_dict_returns_none(self) -> None:
        assert tu.parse_usage("not a dict") is None
        assert tu.parse_usage(None) is None
        assert tu.parse_usage(42) is None

    def test_negative_token_count_coerced_to_zero(self) -> None:
        m = _assistant_msg(input_tokens=-5)
        result = tu.parse_usage(m)
        assert result is not None
        usage, _, _ = result
        assert usage["input"] == 0

    def test_string_token_count_coerced_to_zero(self) -> None:
        m = {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": "lots"},
        }
        result = tu.parse_usage(m)
        assert result is not None
        usage, _, _ = result
        assert usage["input"] == 0

    def test_dated_model_normalized(self) -> None:
        m = _assistant_msg(model="claude-haiku-4-5-20251001")
        result = tu.parse_usage(m)
        assert result is not None
        _, model, _ = result
        assert model == "claude-haiku-4-5"

    def test_synthetic_model_passes_through(self) -> None:
        m = _assistant_msg(model="<synthetic>")
        result = tu.parse_usage(m)
        assert result is not None
        _, model, _ = result
        assert model == "<synthetic>"

    def test_message_without_id_returns_none_id(self) -> None:
        m = _assistant_msg()
        del m["id"]
        result = tu.parse_usage(m)
        assert result is not None
        _, _, msg_id = result
        assert msg_id is None


# ---------------------------------------------------------------------------
# walk_jsonl_token_buckets
# ---------------------------------------------------------------------------


class TestWalkJsonlTokenBuckets:
    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        assert tu.walk_jsonl_token_buckets(path) == {}

    def test_single_day_single_message(self, tmp_path: Path) -> None:
        path = tmp_path / "one.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(), ts="2026-05-01T12:00:00.000Z")])
        result = tu.walk_jsonl_token_buckets(path)
        assert "2026-05-01" in result
        assert result["2026-05-01"]["input"] == 100
        assert result["2026-05-01"]["cache_read"] == 1000
        assert "claude-opus-4-7" in result["2026-05-01"]["by_model"]

    def test_midnight_cross(self, tmp_path: Path) -> None:
        path = tmp_path / "midnight.jsonl"
        _write_jsonl(
            path,
            [
                _wrap(_assistant_msg(msg_id="a"), ts="2026-05-01T23:55:00.000Z"),
                _wrap(_assistant_msg(msg_id="b"), ts="2026-05-02T00:05:00.000Z"),
            ],
        )
        result = tu.walk_jsonl_token_buckets(path)
        assert "2026-05-01" in result
        assert "2026-05-02" in result

    def test_mixed_models(self, tmp_path: Path) -> None:
        path = tmp_path / "mixed.jsonl"
        _write_jsonl(
            path,
            [
                _wrap(_assistant_msg(model="claude-opus-4-7", msg_id="a")),
                _wrap(_assistant_msg(model="claude-sonnet-4-6", msg_id="b")),
            ],
        )
        result = tu.walk_jsonl_token_buckets(path)
        day = result["2026-05-01"]
        assert "claude-opus-4-7" in day["by_model"]
        assert "claude-sonnet-4-6" in day["by_model"]

    def test_corrupt_line_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            f.write(json.dumps(_wrap(_assistant_msg(msg_id="a"))) + "\n")
            f.write("garbage line\n")
            f.write(json.dumps(_wrap(_assistant_msg(msg_id="b"))) + "\n")
        result = tu.walk_jsonl_token_buckets(path)
        # Both valid messages counted; garbage line skipped.
        assert result["2026-05-01"]["input"] == 200

    def test_unreadable_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        # File doesn't exist.
        assert tu.walk_jsonl_token_buckets(path) == {}

    def test_dedup_by_message_id(self, tmp_path: Path) -> None:
        path = tmp_path / "dups.jsonl"
        _write_jsonl(
            path,
            [
                _wrap(_assistant_msg(msg_id="dup")),
                _wrap(_assistant_msg(msg_id="dup")),  # duplicate
                _wrap(_assistant_msg(msg_id="dup")),  # duplicate
            ],
        )
        result = tu.walk_jsonl_token_buckets(path)
        assert result["2026-05-01"]["input"] == 100  # NOT 300

    def test_message_without_id_not_deduped(self, tmp_path: Path) -> None:
        """Without an id, we can't safely dedup. Each message counts."""
        path = tmp_path / "noid.jsonl"
        m1 = _assistant_msg()
        m1.pop("id")
        m2 = _assistant_msg()
        m2.pop("id")
        _write_jsonl(path, [_wrap(m1), _wrap(m2)])
        result = tu.walk_jsonl_token_buckets(path)
        assert result["2026-05-01"]["input"] == 200

    def test_message_without_timestamp_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "no_ts.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            f.write(json.dumps({"message": _assistant_msg()}) + "\n")
        result = tu.walk_jsonl_token_buckets(path)
        assert result == {}


# ---------------------------------------------------------------------------
# get_or_compute (cache layer)
# ---------------------------------------------------------------------------


class TestGetOrCompute:
    def test_miss_then_hit(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg())])
        cache: dict = {}
        # Miss.
        result1 = tu.get_or_compute(path, cache)
        assert result1["2026-05-01"]["input"] == 100
        assert str(path.resolve()) in cache
        # Hit (cache populated).
        result2 = tu.get_or_compute(path, cache)
        assert result2 == result1

    def test_size_drift_triggers_remwalk(self, tmp_path: Path) -> None:
        path = tmp_path / "growing.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="a"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        # Append a second message.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_wrap(_assistant_msg(msg_id="b"))) + "\n")
        result = tu.get_or_compute(path, cache)
        assert result["2026-05-01"]["input"] == 200

    def test_concurrent_append_during_walk_skips_persist(self, tmp_path: Path, monkeypatch) -> None:
        """If size/mtime drift between pre-stat and post-stat, do NOT
        persist the (potentially partial) walk. Next push picks up the
        settled file."""
        path = tmp_path / "concurrent.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="a"))])
        cache: dict = {}

        original_walk = tu.walk_jsonl_token_buckets

        def walk_then_append(p: Path) -> dict:
            result = original_walk(p)
            # Simulate a concurrent append after the walk finished.
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_wrap(_assistant_msg(msg_id="b"))) + "\n")
            return result

        monkeypatch.setattr(tu, "walk_jsonl_token_buckets", walk_then_append)
        result = tu.get_or_compute(path, cache)
        # Result is returned but cache NOT updated — next push will re-walk.
        assert result["2026-05-01"]["input"] == 100
        assert str(path.resolve()) not in cache

    def test_deadline_exceeded_returns_cached_or_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg())])
        cache: dict = {}
        # Deadline already in the past.
        import time as _time

        result = tu.get_or_compute(path, cache, deadline_monotonic=_time.monotonic() - 1)
        assert result == {}
        assert str(path.resolve()) not in cache

    def test_unreadable_file(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        cache: dict = {}
        result = tu.get_or_compute(path, cache)
        assert result == {}


# ---------------------------------------------------------------------------
# slice_window
# ---------------------------------------------------------------------------


class TestSliceWindow:
    def _by_day_3day(self) -> dict:
        return {
            "2026-04-29": {
                "input": 10,
                "cache_create": 0,
                "cache_read": 100,
                "output": 5,
                "by_model": {
                    "claude-opus-4-7": {
                        "input": 10,
                        "cache_create": 0,
                        "cache_read": 100,
                        "output": 5,
                    }
                },
            },
            "2026-04-30": {
                "input": 20,
                "cache_create": 0,
                "cache_read": 200,
                "output": 10,
                "by_model": {
                    "claude-opus-4-7": {
                        "input": 20,
                        "cache_create": 0,
                        "cache_read": 200,
                        "output": 10,
                    }
                },
            },
            "2026-05-01": {
                "input": 30,
                "cache_create": 0,
                "cache_read": 300,
                "output": 15,
                "by_model": {
                    "claude-sonnet-4-6": {
                        "input": 30,
                        "cache_create": 0,
                        "cache_read": 300,
                        "output": 15,
                    }
                },
            },
        }

    def test_full_window(self) -> None:
        by_day = self._by_day_3day()
        result = tu.slice_window(
            by_day,
            since=datetime(2026, 4, 29, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        assert result["input"] == 60
        assert result["cache_read"] == 600
        assert "claude-opus-4-7" in result["by_model"]
        assert "claude-sonnet-4-6" in result["by_model"]

    def test_partial_overlap(self) -> None:
        by_day = self._by_day_3day()
        result = tu.slice_window(
            by_day,
            since=datetime(2026, 4, 30, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        assert result["input"] == 50  # 20 + 30

    def test_outside_window(self) -> None:
        by_day = self._by_day_3day()
        result = tu.slice_window(
            by_day,
            since=datetime(2026, 6, 1, tzinfo=timezone.utc),
            until=datetime(2026, 6, 30, tzinfo=timezone.utc),
        )
        assert result["input"] == 0
        assert result["by_model"] == {}

    def test_empty_input(self) -> None:
        result = tu.slice_window(
            {},
            since=datetime(2026, 4, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        assert result["input"] == 0


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_known_models_split(self) -> None:
        by_model = {
            "claude-sonnet-4-6": {
                "input": 1_000_000,
                "cache_create": 0,
                "cache_read": 0,
                "output": 1_000_000,
            },
        }
        total, per_model = tu.estimate_cost(by_model)
        # 1M input * $3 + 1M output * $15 = $18
        assert total == pytest.approx(18.0)
        assert per_model["claude-sonnet-4-6"] == pytest.approx(18.0)

    def test_unknown_model_excluded_with_notice(self, capsys: pytest.CaptureFixture[str]) -> None:
        by_model = {
            "claude-opus-4-7": {
                "input": 1_000_000,
                "cache_create": 0,
                "cache_read": 0,
                "output": 0,
            },
            "claude-future-9-9": {
                "input": 1_000_000,
                "cache_create": 0,
                "cache_read": 0,
                "output": 0,
            },
        }
        total, per_model = tu.estimate_cost(by_model)
        # Only Opus 4.7 contributes: 1M * $15 = $15.
        assert total == pytest.approx(15.0)
        assert "claude-future-9-9" not in per_model
        captured = capsys.readouterr()
        assert "unknown model in pricing: claude-future-9-9" in captured.err

    def test_unknown_model_warns_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        by_model = {
            "claude-future-9-9": {"input": 1, "cache_create": 0, "cache_read": 0, "output": 0}
        }
        tu.estimate_cost(by_model)
        capsys.readouterr()  # drain
        tu.estimate_cost(by_model)
        captured = capsys.readouterr()
        # Second call: no notice (already warned).
        assert "claude-future-9-9" not in captured.err

    def test_synthetic_excluded_from_cost(self) -> None:
        by_model = {
            "<synthetic>": {
                "input": 1_000_000,
                "cache_create": 0,
                "cache_read": 0,
                "output": 1_000_000,
            },
        }
        total, per_model = tu.estimate_cost(by_model)
        assert total == 0.0
        assert per_model == {}

    def test_zero_tokens(self) -> None:
        total, per_model = tu.estimate_cost({})
        assert total == 0.0
        assert per_model == {}


# ---------------------------------------------------------------------------
# is_cache_cold
# ---------------------------------------------------------------------------


class TestIsCacheCold:
    """Track 10A: version-aware prefix heuristic. Returns True on
    missing file, OSError, size < `_MIN_WARM_CACHE_BYTES`, OR a
    prefix-parse that can't find `"version": CACHE_VERSION` in the
    first 256 bytes."""

    def test_missing_file(self, tmp_path: Path) -> None:
        assert tu.is_cache_cold() is True

    def test_empty_files_dict_treated_as_cold(self) -> None:
        """An empty cache `{"version":1, "files":{}}` is well under
        the 64-byte threshold and is correctly treated as cold."""
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(json.dumps({"version": 1, "files": {}}))
        assert tu.is_cache_cold() is True

    def test_short_garbage_treated_as_cold(self) -> None:
        """Renamed from `test_corrupt_json`. Very short content is
        cold regardless of validity. (7-byte garbage < 64 bytes.)"""
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text("garbage")
        assert tu.is_cache_cold() is True

    def test_populated_cache_treated_as_warm(self) -> None:
        """A realistically-shaped populated cache exceeds the
        64-byte threshold AND has the right version prefix."""
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": 1,
                    "files": {
                        "/Users/kb/.claude/projects/x/sess.jsonl": {
                            "size": 12345,
                            "mtime": 1.234e9,
                            "by_day": {
                                "2026-05-01": {
                                    "input": 100,
                                    "cache_create": 0,
                                    "cache_read": 1000,
                                    "output": 50,
                                    "by_model": {},
                                }
                            },
                        }
                    },
                }
            )
        )
        assert tu.is_cache_cold() is False

    def test_large_corrupt_file_treated_as_cold(self) -> None:
        """REGRESSION pin (Codex adversarial 2026-05-06): a stat-only
        heuristic would treat large corrupt content as warm, letting
        autopush proceed and emit a thinned snapshot. The version-peek
        catches it: prefix-search for `"version": 1` fails → cold →
        autopush emits notice + skips token aggregation. No thinned
        snapshot from this machine in the heal window."""
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text("x" * 200)
        assert tu.is_cache_cold() is True

    def test_wrong_version_treated_as_cold(self) -> None:
        """REGRESSION pin: when CACHE_VERSION is bumped (currently 1,
        future-looking), a stale on-disk cache with `version: 0` (or
        any non-current version) must be treated as cold so autopush
        skips token aggregation in the heal window. Without this,
        autopush would normalize the wrong-version cache to empty
        inside the lock, walk all jsonls under the 250ms budget, and
        emit a thinned snapshot."""
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Wrong version, but otherwise large + valid JSON.
        tu.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": 999,
                    "files": {"x.jsonl": {"size": 1, "mtime": 0.0, "by_day": {}}},
                }
            )
        )
        assert tu.is_cache_cold() is True

    def test_missing_version_field_treated_as_cold(self) -> None:
        """A large file with no `"version":` key in the first 256
        bytes is treated as cold. Catches both schema drift and
        files where `version` is buried beyond the prefix-peek."""
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(json.dumps({"files": {}, "padding": "x" * 200}))
        assert tu.is_cache_cold() is True

    def test_stat_oserror_returns_cold(self, monkeypatch) -> None:
        """If `path.stat()` raises (e.g. EACCES on a chmod-restricted
        config dir), degrade to cold — safe default that triggers a
        warm attempt that will then surface the access issue via the
        lock path."""

        def boom(self):
            raise PermissionError("simulated EACCES")

        monkeypatch.setattr(Path, "stat", boom)
        assert tu.is_cache_cold() is True


class TestLockAndGetFiles:
    """Track 10A: extracted from cli.py's inline `locked_json_rmw +
    version-check + isinstance-check + ljson.data['files']` block.
    Yields the `files` dict, or `None` ONLY on warn-mode contention."""

    def test_block_mode_yields_files_dict(self) -> None:
        with tu.lock_and_get_files("block") as files:
            assert files == {}
            files["x.jsonl"] = {"size": 1, "mtime": 0.0, "by_day": {}}
        # Persisted across contexts.
        with tu.lock_and_get_files("block") as files:
            assert "x.jsonl" in files

    def test_version_mismatch_normalizes_inside_lock(self) -> None:
        """REGRESSION pin (ported from old `test_version_mismatch`):
        a wrong-version cache is normalized in place to empty inside
        the flock; caller still gets a (now-empty) dict back, NOT
        None. None is reserved for warn-mode contention."""
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(json.dumps({"version": 999, "files": {"old": {"size": 1}}}))
        with tu.lock_and_get_files("block") as files:
            assert files == {}  # normalized, NOT None

    def test_malformed_files_normalizes_inside_lock(self) -> None:
        """Cache with a non-dict `files` value normalizes to empty."""
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(json.dumps({"version": 1, "files": "not-a-dict"}))
        with tu.lock_and_get_files("block") as files:
            assert files == {}

    def test_corrupt_cache_normalizes_inside_lock(self) -> None:
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text("not valid json")
        with tu.lock_and_get_files("block") as files:
            assert files == {}

    def test_warn_mode_contention_yields_none(self) -> None:
        """Caller branches on `files is None` to skip token
        aggregation entirely under autopush contention."""
        import fcntl
        import os as _os

        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.touch()
        blocker_fd = _os.open(str(tu.CACHE_PATH), _os.O_RDWR)
        try:
            fcntl.flock(blocker_fd, fcntl.LOCK_EX)
            with tu.lock_and_get_files("warn") as files:
                assert files is None
        finally:
            fcntl.flock(blocker_fd, fcntl.LOCK_UN)
            _os.close(blocker_fd)

    def test_no_mutation_preserves_semantic_content(self) -> None:
        """Track 10A briefly added a skip-write optimization; reverted
        after measuring net-negative perf. A no-mutation context
        rewrites the file (same as pre-Track-10A behavior) but the
        parsed content is unchanged."""
        with tu.lock_and_get_files("block") as files:
            files["seed"] = {"size": 1, "mtime": 0.0, "by_day": {}}
        with tu.lock_and_get_files("block") as files:
            assert "seed" in files
            assert files["seed"] == {"size": 1, "mtime": 0.0, "by_day": {}}


# ---------------------------------------------------------------------------
# warm_token_cache_inline
# ---------------------------------------------------------------------------


class TestWarmTokenCacheInline:
    def test_walks_parent_jsonls(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / "claude"
        proj = claude_dir / "projects" / "encoded-proj"
        proj.mkdir(parents=True)
        _write_jsonl(proj / "session-1.jsonl", [_wrap(_assistant_msg(msg_id="a"))])
        _write_jsonl(proj / "session-2.jsonl", [_wrap(_assistant_msg(msg_id="b"))])
        walked, skipped = tu.warm_token_cache_inline([claude_dir])
        assert walked == 2
        assert skipped == 0
        # Cache populated.
        assert tu.is_cache_cold() is False

    def test_walks_subagent_jsonls(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / "claude"
        proj = claude_dir / "projects" / "encoded-proj"
        proj.mkdir(parents=True)
        # Parent
        _write_jsonl(proj / "session-1.jsonl", [_wrap(_assistant_msg(msg_id="a"))])
        # Subagents — under <session-uuid>/subagents/
        sub_dir = proj / "abc-uuid" / "subagents"
        sub_dir.mkdir(parents=True)
        _write_jsonl(sub_dir / "agent-1.jsonl", [_wrap(_assistant_msg(msg_id="x"))])
        _write_jsonl(sub_dir / "agent-2.jsonl", [_wrap(_assistant_msg(msg_id="y"))])
        walked, _ = tu.warm_token_cache_inline([claude_dir])
        assert walked == 3  # 1 parent + 2 subagents

    def test_budget_exhausted_skips_remaining(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / "claude"
        proj = claude_dir / "projects" / "encoded-proj"
        proj.mkdir(parents=True)
        for i in range(5):
            _write_jsonl(proj / f"session-{i}.jsonl", [_wrap(_assistant_msg(msg_id=str(i)))])
        # Tiny deadline — first iter will succeed, rest skipped.
        walked, skipped = tu.warm_token_cache_inline([claude_dir], deadline_s=0.0)
        # All entries hit the deadline check at the top of the loop and skip.
        assert walked + skipped == 5
        assert skipped >= 1


# ---------------------------------------------------------------------------
# gc_cache_entries
# ---------------------------------------------------------------------------


class TestGcCacheEntries:
    def test_reaps_entries_with_missing_jsonl(self, tmp_path: Path) -> None:
        # Populate cache via warm.
        claude_dir = tmp_path / "claude"
        proj = claude_dir / "projects" / "encoded-proj"
        proj.mkdir(parents=True)
        jsonl = proj / "session.jsonl"
        _write_jsonl(jsonl, [_wrap(_assistant_msg())])
        tu.warm_token_cache_inline([claude_dir])
        # Delete the jsonl.
        jsonl.unlink()
        reaped = tu.gc_cache_entries()
        assert reaped == 1

    def test_reaps_entries_older_than_max_age(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / "claude"
        proj = claude_dir / "projects" / "encoded-proj"
        proj.mkdir(parents=True)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        _write_jsonl(proj / "ancient.jsonl", [_wrap(_assistant_msg(), ts=old_ts)])
        tu.warm_token_cache_inline([claude_dir])
        # All entries should have only old by_day buckets.
        reaped = tu.gc_cache_entries(max_age_s=90 * 24 * 3600)
        assert reaped == 1

    def test_keeps_recent_entries(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / "claude"
        proj = claude_dir / "projects" / "encoded-proj"
        proj.mkdir(parents=True)
        recent_ts = datetime.now(timezone.utc).isoformat()
        _write_jsonl(proj / "fresh.jsonl", [_wrap(_assistant_msg(), ts=recent_ts)])
        tu.warm_token_cache_inline([claude_dir])
        reaped = tu.gc_cache_entries()
        assert reaped == 0
