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
# Cache path isolation lives in tests/conftest.py as the autouse
# `_isolate_token_cache` fixture (covers all test files, not just this one).
# ---------------------------------------------------------------------------


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
        by_day1, _skills1 = tu.get_or_compute(path, cache)
        assert by_day1["2026-05-01"]["input"] == 100
        assert str(path.resolve()) in cache
        # Hit (cache populated).
        by_day2, _skills2 = tu.get_or_compute(path, cache)
        assert by_day2 == by_day1

    def test_size_drift_triggers_remwalk(self, tmp_path: Path) -> None:
        path = tmp_path / "growing.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="a"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        # Append a second message.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_wrap(_assistant_msg(msg_id="b"))) + "\n")
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 200

    def test_concurrent_append_during_walk_skips_persist(self, tmp_path: Path, monkeypatch) -> None:
        """If size/mtime drift between pre-stat and post-stat, do NOT
        persist the (potentially partial) walk. Next push picks up the
        settled file."""
        path = tmp_path / "concurrent.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="a"))])
        cache: dict = {}

        original_walk = tu.walk_jsonl_segment

        def walk_then_append(p: Path, **kwargs):
            result = original_walk(p, **kwargs)
            # Simulate a concurrent append after the walk finished.
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_wrap(_assistant_msg(msg_id="b"))) + "\n")
            return result

        monkeypatch.setattr(tu, "walk_jsonl_segment", walk_then_append)
        by_day, _skills = tu.get_or_compute(path, cache)
        # Result is returned but cache NOT updated — next push will re-walk.
        assert by_day["2026-05-01"]["input"] == 100
        assert str(path.resolve()) not in cache

    def test_deadline_exceeded_returns_cached_or_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg())])
        cache: dict = {}
        # Deadline already in the past.
        import time as _time

        result = tu.get_or_compute(path, cache, deadline_monotonic=_time.monotonic() - 1)
        assert result == ({}, {})
        assert str(path.resolve()) not in cache

    def test_unreadable_file(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        cache: dict = {}
        result = tu.get_or_compute(path, cache)
        assert result == ({}, {})


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
            # "future" is not a known family, so this stays unresolvable
            # even with family-tier fallback in play.
            "claude-future-9-9": {
                "input": 1_000_000,
                "cache_create": 0,
                "cache_read": 0,
                "output": 0,
            },
        }
        total, per_model = tu.estimate_cost(by_model)
        # Only Opus 4.7 contributes: 1M * $5 = $5.
        assert total == pytest.approx(5.0)
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

    def test_claude_5_family_priced(self) -> None:
        """v0.12.13 regression: the entire Claude 5 family resolved to no
        price, so a fleet running Opus 5 / Fable 5 / Sonnet 5 costed $0 and
        the card printed ~$3.37 for a window the corrected table prices at
        ~$11,015."""
        by_model = {
            m: {"input": 1_000_000, "cache_create": 0, "cache_read": 0, "output": 0}
            for m in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-opus-4-8")
        }
        total, per_model = tu.estimate_cost(by_model)
        assert per_model["claude-opus-5"] == pytest.approx(5.0)
        assert per_model["claude-opus-4-8"] == pytest.approx(5.0)
        assert per_model["claude-sonnet-5"] == pytest.approx(3.0)
        assert per_model["claude-fable-5"] == pytest.approx(10.0)
        assert total == pytest.approx(23.0)

    def test_cache_write_priced_at_1h_ttl(self) -> None:
        """Cache writes bill at 2x input (1h TTL), not 1.25x (5m). Claude
        Code defaults to the 1h TTL; the old 1.25x understated every
        window carrying cache_create volume."""
        by_model = {
            "claude-opus-5": {
                "input": 0,
                "cache_create": 1_000_000,
                "cache_read": 1_000_000,
                "output": 0,
            }
        }
        total, _ = tu.estimate_cost(by_model)
        # 1M * ($5 * 2.0) + 1M * ($5 * 0.1) = $10.50
        assert total == pytest.approx(10.50)


# ---------------------------------------------------------------------------
# model_family / resolve_prices  (v0.12.13)
# ---------------------------------------------------------------------------


class TestModelFamily:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("claude-opus-5", "opus"),
            ("claude-opus-4-8", "opus"),
            ("claude-sonnet-5", "sonnet"),
            ("claude-fable-5", "fable"),
            ("claude-mythos-5", "mythos"),
            ("claude-haiku-4-5", "haiku"),
        ],
    )
    def test_known_families(self, model: str, expected: str) -> None:
        assert tu.model_family(model) == expected

    @pytest.mark.parametrize(
        "model",
        [
            "<synthetic>",
            "",
            "claude",
            "claude-opus",  # only 2 segments
            "claude-future-9-9",  # unknown family
            "gpt-4-turbo",  # not a claude id
            "opus-5",  # missing vendor prefix
        ],
    )
    def test_non_families_return_none(self, model: str) -> None:
        assert tu.model_family(model) is None

    def test_positional_match_not_substring(self) -> None:
        """TRUST BOUNDARY: model ids are peer-controlled and now drive a
        pricing decision. A substring test would bill this at Opus rates;
        the positional match reads position 1 ("haiku") and stops."""
        assert tu.model_family("claude-haiku-opus-4-5") == "haiku"
        assert tu.resolve_prices("claude-haiku-opus-4-5")["input"] == pytest.approx(1.0)

    def test_non_string_input(self) -> None:
        assert tu.model_family(None) is None  # type: ignore[arg-type]
        assert tu.model_family(123) is None  # type: ignore[arg-type]


class TestResolvePrices:
    def test_exact_entry_overrides_family_by_value(self, monkeypatch) -> None:
        """Precedence pinned by VALUE, not object identity. PRICING ships
        empty (every current model prices at its family rate), so this
        injects a synthetic override whose rates genuinely differ —
        otherwise a resolution-order regression would be undetectable."""
        monkeypatch.setitem(tu.PRICING, "claude-opus-9", tu._tier(99.0, 999.0))
        assert tu.resolve_prices("claude-opus-9")["input"] == pytest.approx(99.0)
        # A sibling in the same family still takes the tier.
        assert tu.resolve_prices("claude-opus-8")["input"] == pytest.approx(5.0)

    def test_pricing_holds_no_redundant_entries(self) -> None:
        """PRICING is an OVERRIDE table. An entry identical to its family
        tier is pure duplication and recreates the multi-site drift this
        release exists to remove: an Opus rate change would need N
        identical edits plus the tier, and missing one prices some models
        at the old rate silently."""
        for model, card in tu.PRICING.items():
            family = tu.model_family(model)
            if family is None:
                continue
            assert card != tu.MODEL_FAMILY_TIERS[family], (
                f"{model} duplicates its family tier — delete it and let "
                f"MODEL_FAMILY_TIERS['{family}'] carry it."
            )

    def test_pre_4_5_opus_overrides_the_modern_tier(self) -> None:
        """The Opus family is NOT rate-uniform across generations: 4.0/4.1
        billed $15/$75, the tier dropped to $5/$25 at 4.5. Without the
        override these resolve to the modern tier and price 3x LOW under a
        confident `~`, where pre-v0.12.13 they were unpriced and raised a
        loud `>=`. Reachable — Opus 4.1 retired 2026-08-05 and by_day
        history runs 90 days."""
        for model in ("claude-opus-4-1", "claude-opus-4-0"):
            prices = tu.resolve_prices(model)
            assert prices["input"] == pytest.approx(15.0), model
            assert prices["output"] == pytest.approx(75.0), model
        # The generation the tier actually describes is unaffected.
        assert tu.resolve_prices("claude-opus-4-5")["input"] == pytest.approx(5.0)

    def test_truncated_id_does_not_bill(self) -> None:
        """`claude-opus-` splits to ["claude","opus",""] — length 3 with a
        recognized family, so it would satisfy a naive check and bill
        garbage at full Opus rates instead of degrading to unpriced."""
        assert tu.model_family("claude-opus-") is None
        assert tu.resolve_prices("claude-opus-") is None

    def test_returned_card_is_a_copy(self) -> None:
        """Rate cards are module-level and shared; handing out the live
        dict lets one caller corrupt pricing process-wide."""
        card = tu.resolve_prices("claude-opus-5")
        card["input"] = 999.0
        assert tu.resolve_prices("claude-opus-5")["input"] == pytest.approx(5.0)

    def test_retired_model_prices_at_current_family_tier(self) -> None:
        """The inaccuracy accepted in review, pinned so it can't silently
        change. Retired models resolve at CURRENT-generation family rates
        (Claude 3 Opus really billed $15/$75, not $5/$25). Tolerable only
        because retired models don't appear in live session data — see
        Invariant 2 in docs/invariants/events-retro.md."""
        assert tu.resolve_prices("claude-sonnet-3-7")["input"] == pytest.approx(3.0)
        assert tu.resolve_prices("claude-opus-3-0")["input"] == pytest.approx(5.0)

    def test_family_fallback_for_unlisted_model(self) -> None:
        """The whole point: a model released after this table was written
        prices in the right ballpark instead of silently costing $0."""
        assert "claude-opus-6" not in tu.PRICING
        prices = tu.resolve_prices("claude-opus-6")
        assert prices is not None
        assert prices["input"] == pytest.approx(5.0)
        assert prices["output"] == pytest.approx(25.0)

    def test_unparseable_stays_unpriced(self) -> None:
        assert tu.resolve_prices("claude-future-9-9") is None
        assert tu.resolve_prices("<synthetic>") is None

    def test_every_rate_card_has_all_token_fields(self) -> None:
        """estimate_cost indexes prices[k] for k in TOKEN_FIELDS — a rate
        card missing a field is a KeyError at render time."""
        for card in (*tu.PRICING.values(), *tu.MODEL_FAMILY_TIERS.values()):
            assert set(card) == set(tu.TOKEN_FIELDS)


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

    def test_invalid_utf8_treated_as_cold_not_raised(self) -> None:
        """v0.12.16 T3: a bad byte in the cache must report cold, not raise.

        Pre-fix `is_cache_cold` used `read_text(encoding="utf-8")` guarded
        only by OSError, so UnicodeDecodeError escaped. This runs on the
        events-tail path via `_decide_token_walk_policy`, so it killed the
        whole tail exactly like the session readers did.

        The `except UnicodeDecodeError` on the json.loads below it looked
        like the guard but was DEAD — json.loads on a `str` cannot raise it.
        Feeding bytes makes ValueError cover both cases for real.
        """
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_bytes(
            b'{"version": 1, "files": {"a": {"size": 1}}, "junk": "\xff\xfe"}' + b" " * 300
        )
        assert tu.is_cache_cold() is True

    def test_bom_prefixed_cache_treated_as_cold(self) -> None:
        """`is_cache_cold` must agree with the reader that actually loads
        this file. `json.loads` on BYTES accepts a utf-8 BOM (and utf-16/32);
        `lockedjson` decodes strict utf-8 and rejects them as corrupt. Pre-fix
        the two disagreed: a BOM cache reported WARM, so the inline warm was
        skipped, then the real read reset it as corrupt — no token data AND
        no warm cache. Caught by Codex adversarial review during /review.
        """
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": tu.CACHE_VERSION, "files": {"/a/b.jsonl": {"size": 1}}}
        tu.CACHE_PATH.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode() + b" " * 200)
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

    # The four tests below pin the explicit reap branches that were
    # previously uncovered (entry not a dict, by_day missing/empty,
    # wrong-version cache). Each asserts disk state in addition to the
    # `reaped` return value: the gc_cache_entries refactor onto
    # lock_and_get_files relies on in-place mutation of the yielded
    # `files` dict; a regression where `keep` is built but never
    # persisted would pass the return-value-only assertions.

    @staticmethod
    def _read_disk_cache() -> dict:
        """Read the on-disk cache JSON, post-gc."""
        return json.loads(tu.CACHE_PATH.read_text(encoding="utf-8"))

    def test_reaps_entry_not_a_dict(self, tmp_path: Path) -> None:
        # Hand-craft a cache where one entry is a non-dict (corruption).
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": tu.CACHE_VERSION,
                    "files": {"corrupt-key": "not-a-dict"},
                }
            )
        )
        reaped = tu.gc_cache_entries()
        assert reaped == 1
        on_disk = self._read_disk_cache()
        assert on_disk["version"] == tu.CACHE_VERSION
        assert on_disk["files"] == {}

    def test_reaps_entry_with_missing_by_day(self, tmp_path: Path) -> None:
        # Real path on disk so the Path.exists() check passes; entry
        # has no by_day field at all.
        live = tmp_path / "live.jsonl"
        live.touch()
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": tu.CACHE_VERSION,
                    "files": {str(live): {"size": 0, "mtime": 0.0}},
                }
            )
        )
        reaped = tu.gc_cache_entries()
        assert reaped == 1
        assert self._read_disk_cache()["files"] == {}

    def test_reaps_entry_with_empty_by_day(self, tmp_path: Path) -> None:
        live = tmp_path / "live.jsonl"
        live.touch()
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": tu.CACHE_VERSION,
                    "files": {str(live): {"size": 0, "mtime": 0.0, "by_day": {}}},
                }
            )
        )
        reaped = tu.gc_cache_entries()
        assert reaped == 1
        assert self._read_disk_cache()["files"] == {}

    def test_strips_unknown_top_level_keys_on_gc(self, tmp_path: Path) -> None:
        # Regression pin (Codex/Claude cross-model HIGH on Track 11A
        # review, 2026-05-10): pre-v0.12.4 gc_cache_entries did
        # `ljson.data.clear() + update({version, files})` which silently
        # stripped unknown top-level keys. The refactor onto
        # lock_and_get_files lost this property until v0.12.4 lifted
        # the root-sanitization into the wrapper. This test ensures
        # `mm gc` heals a cache with bonus top-level keys.
        live = tmp_path / "live.jsonl"
        live.touch()
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": tu.CACHE_VERSION,
                    "files": {
                        str(live): {
                            "size": 0,
                            "mtime": 0.0,
                            "by_day": {datetime.now(timezone.utc).date().isoformat(): {}},
                        }
                    },
                    "padding": "x" * 1000,
                    "future_field": {"nested": True},
                }
            )
        )
        tu.gc_cache_entries()
        on_disk = self._read_disk_cache()
        assert set(on_disk.keys()) == {"version", "files"}
        assert on_disk["version"] == tu.CACHE_VERSION
        # The live entry survives gc — only the bloat is stripped.
        assert str(live) in on_disk["files"]

    def test_wrong_version_cache_normalized_to_empty(self, tmp_path: Path) -> None:
        # Wrong-version cache pre-populated with stale entries. After
        # the refactor, lock_and_get_files clears the file shape to
        # `{"version": CACHE_VERSION, "files": {}}` BEFORE gc sees it,
        # so reaped == 0 (gc never iterates the old entries — they're
        # already gone). Disk state asserts the post-normalization
        # shape, which is the load-bearing observable.
        tu.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tu.CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": 999,
                    "files": {
                        "old-key-1": {"size": 1, "mtime": 1.0, "by_day": {"2020-01-01": {}}},
                        "old-key-2": {"size": 2, "mtime": 2.0, "by_day": {"2020-01-02": {}}},
                    },
                }
            )
        )
        reaped = tu.gc_cache_entries()
        assert reaped == 0
        on_disk = self._read_disk_cache()
        assert on_disk["version"] == tu.CACHE_VERSION
        assert on_disk["files"] == {}


# ---------------------------------------------------------------------------
# Skill detection (v0.11.27, fleet-skill-counts plan tests #1-#4 + D2)
# ---------------------------------------------------------------------------


_TOOL_ID_COUNTER = [0]


def _skill_block(skill: str, *, tool_id: str | None = None) -> dict:
    """Synthesize a Claude Code ``tool_use`` block for the Skill tool.

    Tool ids are generated unique by default (mirrors Anthropic's
    ``toolu_*`` format guarantee). Tests that need a deliberate-retry
    fixture (same tool_id reused) pass ``tool_id=...`` explicitly."""
    if tool_id is None:
        _TOOL_ID_COUNTER[0] += 1
        tool_id = f"toolu_test_{_TOOL_ID_COUNTER[0]}"
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": "Skill",
        "input": {"skill": skill, "args": ""},
    }


def _assistant_with_blocks(blocks: list[dict], *, msg_id: str = "msg_a") -> dict:
    """Assistant message with content blocks AND minimal usage so the
    walker's existing token-side parse_usage() also accepts it. Mirrors
    the real Claude Code jsonl shape captured during /plan-eng-review."""
    return {
        "id": msg_id,
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": blocks,
        "usage": {
            "input_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 1,
        },
    }


class TestSkillDetection:
    def test_two_skill_blocks_same_day_dedup_aware(self, tmp_path: Path) -> None:
        """Plan test #1 (revised): TWO distinct Skill invocations
        (different ``tool_use.id``s) within the same message both count;
        a duplicate ``tool_use.id`` retry is deduped to one. Pinned by
        the smoke-test bug we caught — Claude Code emits multiple jsonl
        lines for the same ``message.id`` under streaming-iteration
        semantics, so dedup MUST be by ``tool_use.id``, not ``message.id``."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _wrap(
                    _assistant_with_blocks(
                        [
                            _skill_block("ship", tool_id="toolu_a"),
                            _skill_block("ship", tool_id="toolu_b"),
                        ],
                        msg_id="m1",
                    ),
                    ts="2026-05-01T12:00:00Z",
                ),
                # Duplicate tool_use.id retry (same toolu_a) — DEDUPED.
                _wrap(
                    _assistant_with_blocks([_skill_block("ship", tool_id="toolu_a")], msg_id="m1"),
                    ts="2026-05-01T12:00:01Z",
                ),
            ],
        )
        _by_day, skills = tu.walk_jsonl_buckets(path)
        # toolu_a + toolu_b counted; the second toolu_a retry deduped.
        assert skills == {"2026-05-01": {"ship": 2}}

    def test_skill_dedup_by_tool_id_not_message_id(self, tmp_path: Path) -> None:
        """REGRESSION pin for the smoke-test bug: when two jsonl entries
        share the same ``message.id`` but carry different content blocks
        (the Claude Code streaming-iteration shape), each entry's
        ``tool_use`` blocks must be counted independently. Pre-fix the
        walker deduped by ``message.id`` and dropped the second iteration."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                # Iteration 1: text only, no Skill.
                _wrap(
                    _assistant_with_blocks(
                        [{"type": "text", "text": "thinking..."}], msg_id="msg_X"
                    ),
                    ts="2026-05-01T12:00:00Z",
                ),
                # Iteration 2: same message.id, but a Skill tool_use.
                # Pre-fix this got skipped (msg_X already seen). Post-fix
                # the skill detection runs independently of message dedup.
                _wrap(
                    _assistant_with_blocks(
                        [_skill_block("plan-eng-review", tool_id="toolu_p1")],
                        msg_id="msg_X",
                    ),
                    ts="2026-05-01T12:00:01Z",
                ),
            ],
        )
        _by_day, skills = tu.walk_jsonl_buckets(path)
        assert skills == {"2026-05-01": {"plan-eng-review": 1}}

    def test_non_skill_tool_use_blocks_ignored(self, tmp_path: Path) -> None:
        """Plan test #2: Edit/Bash/etc tool_use blocks must not count.
        Token aggregation still works on the same message."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _wrap(
                    _assistant_with_blocks(
                        [
                            {"type": "tool_use", "name": "Edit", "input": {"file": "x"}},
                            {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
                            _skill_block("plan-eng-review"),
                        ],
                        msg_id="m1",
                    ),
                    ts="2026-05-01T12:00:00Z",
                ),
            ],
        )
        by_day, skills = tu.walk_jsonl_buckets(path)
        assert skills == {"2026-05-01": {"plan-eng-review": 1}}
        # Tokens still recorded for the same message.
        assert by_day["2026-05-01"]["input"] == 1

    def test_malformed_skill_blocks_skipped(self, tmp_path: Path) -> None:
        """Plan test #3: every malformed shape skipped without raising."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _wrap(
                    _assistant_with_blocks(
                        [
                            # ``input`` missing
                            {"type": "tool_use", "name": "Skill"},
                            # ``input`` not a dict
                            {"type": "tool_use", "name": "Skill", "input": "ship"},
                            # ``input.skill`` non-string
                            {"type": "tool_use", "name": "Skill", "input": {"skill": 42}},
                            # ``input.skill`` empty string
                            {"type": "tool_use", "name": "Skill", "input": {"skill": ""}},
                            # ``input.skill`` missing
                            {"type": "tool_use", "name": "Skill", "input": {}},
                            # block itself not a dict
                        ],
                        msg_id="m1",
                    ),
                    ts="2026-05-01T12:00:00Z",
                ),
            ],
        )
        _by_day, skills = tu.walk_jsonl_buckets(path)
        # Empty-bucket pruning drops days with no skills entirely.
        assert skills == {}

    def test_walk_jsonl_buckets_returns_tuple_shape(self, tmp_path: Path) -> None:
        """Plan test #4: tuple shape from new walker; shim returns single."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_with_blocks([_skill_block("ship")], msg_id="m1"))])
        result = tu.walk_jsonl_buckets(path)
        assert isinstance(result, tuple) and len(result) == 2
        # Shim drops the skills view (back-compat with pre-v0.11.27 callers).
        shim_result = tu.walk_jsonl_token_buckets(path)
        assert isinstance(shim_result, dict)


class TestCacheShapeUpgradeGate:
    def test_d2_old_entry_without_skills_field_triggers_rewalk(self, tmp_path: Path) -> None:
        """D2 from /plan-eng-review 2026-05-06: a pre-v0.11.27 cache entry
        with ``by_day`` populated but no ``skills_by_day`` key must trigger
        a re-walk on the next ``get_or_compute``, populating both views.
        Token data must survive the rebuild byte-identical to what the
        walk re-derives.

        Locks in the surgical fix: per-entry presence check (NOT a
        version bump that would invalidate token data fleet-wide)."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_with_blocks([_skill_block("ship")], msg_id="m1"))])
        # Pre-populate cache with the OLD shape: by_day only, no skills_by_day.
        st = path.stat()
        cache: dict = {
            str(path.resolve()): {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "by_day": {"2026-05-01": {"input": 999, "by_model": {}}},  # stale token data
                # NB: no "skills_by_day" key — pre-v0.11.27 shape
            }
        }
        by_day, skills = tu.get_or_compute(path, cache)
        # Re-walk must have happened — entry now has skills_by_day populated.
        entry = cache[str(path.resolve())]
        assert "skills_by_day" in entry
        assert entry["skills_by_day"] == {"2026-05-01": {"ship": 1}}
        # Returned views reflect the FRESH walk, not the stale by_day.
        assert by_day["2026-05-01"]["input"] == 1
        assert skills == {"2026-05-01": {"ship": 1}}

    def test_full_shape_cache_hit_no_rewalk(self, tmp_path: Path, monkeypatch) -> None:
        """Plan test #6: cache entry has BOTH fields and matching size/mtime
        → cached views returned without calling the walker."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_with_blocks([_skill_block("ship")], msg_id="m1"))])
        # Populate cache via real walk first.
        cache: dict = {}
        tu.get_or_compute(path, cache)
        # Now monkeypatch the walker to detect any unwanted re-walk.
        called = {"n": 0}

        def boom(p: Path, **kwargs):
            called["n"] += 1
            return tu.JsonlSegment({}, {}, 0, ())

        monkeypatch.setattr(tu, "walk_jsonl_segment", boom)
        # Second call should hit the cache and NOT call the walker.
        by_day, skills = tu.get_or_compute(path, cache)
        assert called["n"] == 0
        assert "2026-05-01" in by_day
        assert skills == {"2026-05-01": {"ship": 1}}


# ---------------------------------------------------------------------------
# Incremental resume (v0.12.15)
# ---------------------------------------------------------------------------


def _append_lines(path: Path, entries: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _entry(path: Path, cache: dict) -> dict:
    return cache[str(path.resolve())]


class TestIncrementalResume:
    """The events tail must cost O(bytes appended), not O(file size).

    Every test here holds the same contract: whatever the file did
    between pushes, the merged result equals a single full walk.
    """

    def test_append_parses_only_the_tail(self, tmp_path: Path, monkeypatch) -> None:
        """The resumed walk must not re-read bytes it already accounted
        for — that is the entire point of the change."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id=f"m{i}")) for i in range(20)])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        first_offset = _entry(path, cache)["offset"]
        assert first_offset == path.stat().st_size

        _append_lines(path, [_wrap(_assistant_msg(msg_id="tail"))])

        seen: dict = {}
        original = tu.walk_jsonl_segment

        def spy(p: Path, **kwargs):
            seen.update(kwargs)
            return original(p, **kwargs)

        monkeypatch.setattr(tu, "walk_jsonl_segment", spy)
        by_day, _skills = tu.get_or_compute(path, cache)

        assert seen["start_offset"] == first_offset
        assert by_day["2026-05-01"]["input"] == 21 * 100
        assert _entry(path, cache)["offset"] == path.stat().st_size

    def test_append_matches_full_walk(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_with_blocks([_skill_block("ship")], msg_id="m1"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        _append_lines(
            path,
            [_wrap(_assistant_with_blocks([_skill_block("retro")], msg_id="m2"))],
        )
        got = tu.get_or_compute(path, cache)
        assert got == tu.walk_jsonl_buckets(path)
        assert got[1] == {"2026-05-01": {"ship": 1, "retro": 1}}

    def test_straddling_message_iterations_not_double_counted(self, tmp_path: Path) -> None:
        """Claude Code writes one jsonl line per model iteration under a
        SHARED message.id, each restating the same cumulative usage. When
        the resume point lands between two iterations, the seeded tail ids
        are what stop the second segment counting the message again."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="streaming"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        assert _entry(path, cache)["tail_msg_ids"] == ["streaming"]

        # Later iterations of the SAME message land after the resume point.
        _append_lines(path, [_wrap(_assistant_msg(msg_id="streaming"))] * 3)
        by_day, _skills = tu.get_or_compute(path, cache)

        assert by_day["2026-05-01"]["input"] == 100
        assert by_day == tu.walk_jsonl_buckets(path)[0]

    def test_partial_trailing_line_deferred_then_counted_once(self, tmp_path: Path) -> None:
        """A half-written line is not parsed and not counted toward the
        resume offset, so the next push reads it whole — exactly once."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="complete"))])
        settled = path.stat().st_size
        # Claude Code is mid-write: a line with no terminating newline.
        half = json.dumps(_wrap(_assistant_msg(msg_id="inflight")))[:40]
        with path.open("a", encoding="utf-8") as f:
            f.write(half)

        cache: dict = {}
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 100
        assert _entry(path, cache)["offset"] == settled

        # The write completes.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_wrap(_assistant_msg(msg_id="inflight")))[40:] + "\n")
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 200
        assert by_day == tu.walk_jsonl_buckets(path)[0]

    def test_truncated_file_falls_back_to_full_walk(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id=f"m{i}")) for i in range(5)])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        # Rewritten shorter — every accounted byte is now meaningless.
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="only"))])
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 100

    def test_same_length_rewrite_caught_by_head_fingerprint(self, tmp_path: Path) -> None:
        """A rewrite that lands at >= the cached size would read as an
        append on size alone. The head fingerprint is what catches it."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="old", input_tokens=100))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        # Same byte length, different content, different head.
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="new", input_tokens=777))])
        assert path.stat().st_size  # sanity
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 777
        assert by_day == tu.walk_jsonl_buckets(path)[0]

    def test_pre_v0_12_15_entry_hits_cache_without_rewalk(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Entries written before resume fields existed must NOT be
        force-re-walked on a size/mtime hit. They upgrade shape on their
        next real miss — no fleet-wide re-parse storm on upgrade day."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        st = path.stat()
        cache: dict = {
            str(path.resolve()): {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "by_day": {"2026-05-01": {"input": 42, "by_model": {}}},
                "skills_by_day": {},
                # NB: no "offset" / "head" / "tail_msg_ids"
            }
        }
        called = {"n": 0}

        def boom(p: Path, **kwargs):
            called["n"] += 1
            return tu.JsonlSegment({}, {}, 0, ())

        monkeypatch.setattr(tu, "walk_jsonl_segment", boom)
        by_day, _skills = tu.get_or_compute(path, cache)
        assert called["n"] == 0
        assert by_day["2026-05-01"]["input"] == 42

    def test_pre_v0_12_15_entry_upgrades_shape_on_miss(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        st = path.stat()
        cache: dict = {
            str(path.resolve()): {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "by_day": {"2026-05-01": {"input": 42, "by_model": {}}},
                "skills_by_day": {},
            }
        }
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        by_day, _skills = tu.get_or_compute(path, cache)
        entry = _entry(path, cache)
        assert "offset" in entry and "head" in entry
        # Full re-walk, so the bogus cached 42 is discarded — not merged.
        assert by_day["2026-05-01"]["input"] == 200

    def test_concurrent_append_leaves_cached_entry_untouched(self, tmp_path: Path) -> None:
        """The resume path merges into COPIES. A drift-detected walk must
        not leave the surviving entry double-counted."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        before = json.dumps(_entry(path, cache), sort_keys=True)

        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        original = tu.walk_jsonl_segment

        def walk_then_append(p: Path, **kwargs):
            result = original(p, **kwargs)
            _append_lines(p, [_wrap(_assistant_msg(msg_id="m3"))])
            return result

        import unittest.mock as _mock

        with _mock.patch.object(tu, "walk_jsonl_segment", walk_then_append):
            by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 200
        assert json.dumps(_entry(path, cache), sort_keys=True) == before

    def test_invalid_utf8_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """Pre-v0.12.15 the text-mode walker raised UnicodeDecodeError out
        through the whole events tail on a single bad byte."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        with path.open("ab") as f:
            f.write(b'{"message": "\xff\xfe bad bytes"}\n')
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        by_day, _skills = tu.get_or_compute(path, {})
        assert by_day["2026-05-01"]["input"] == 200

    def test_resume_after_oversize_line_keeps_offset_aligned(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A skipped oversize line still advances the offset past its
        bytes, so the following append is accounted exactly once."""
        monkeypatch.setattr(tu, "MAX_JSONL_LINE_BYTES", 512)
        monkeypatch.setattr(tu, "_DRAIN_CHUNK_BYTES", 64)
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"junk": "x" * 4000}) + "\n")
        cache: dict = {}
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 100
        assert _entry(path, cache)["offset"] == path.stat().st_size

        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 200

    def test_oversize_line_followed_by_real_line(self, tmp_path: Path, monkeypatch) -> None:
        """Exercises `_drain_to_newline`'s seek-rewind. Mutation testing
        found that deleting `fp.seek(-over, SEEK_CUR)` left the suite green:
        every oversize test put the junk line at EOF, so the final drain
        chunk landed exactly on the newline and `over` was always 0.

        With a real line AFTER the junk, the drain over-reads into it. If
        the stream isn't rewound, that line is swallowed while `pos` still
        counts it — the persisted offset lands mid-line and the next resume
        skips or double-counts. Direct equivalence break."""
        monkeypatch.setattr(tu, "MAX_JSONL_LINE_BYTES", 512)
        monkeypatch.setattr(tu, "_DRAIN_CHUNK_BYTES", 64)
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"junk": "x" * 4000}) + "\n")
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])

        cache: dict = {}
        by_day, _skills = tu.get_or_compute(path, cache)
        # The line after the junk must be counted exactly once.
        assert by_day["2026-05-01"]["input"] == 200
        assert _entry(path, cache)["offset"] == path.stat().st_size

        # And the resume from that offset must stay aligned.
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m3"))])
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 300
        assert by_day == tu.walk_jsonl_buckets(path)[0]

    def test_eof_inside_oversize_line_does_not_advance(self, tmp_path: Path, monkeypatch) -> None:
        """Claude Code mid-write of a huge line: `_drain_to_newline` hits
        EOF before any newline and returns None. The offset must not move."""
        monkeypatch.setattr(tu, "MAX_JSONL_LINE_BYTES", 512)
        monkeypatch.setattr(tu, "_DRAIN_CHUNK_BYTES", 64)
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        settled = path.stat().st_size
        with path.open("a", encoding="utf-8") as f:
            f.write("y" * 4000)  # no terminating newline — still being written

        cache: dict = {}
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 100
        assert _entry(path, cache)["offset"] == settled

    def test_trim_interacts_correctly_with_incremental_merge(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A cached day aging out of the MAX_BY_DAY_DAYS window while a new
        day merges in. Never executed in the suite before — `_trim_by_day:
        never trim` survived mutation."""
        monkeypatch.setattr(tu, "MAX_BY_DAY_DAYS", 2)
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _wrap(_assistant_msg(msg_id="d1"), ts="2026-05-01T12:00:00.000Z"),
                _wrap(
                    _assistant_with_blocks([_skill_block("ship")], msg_id="s1"),
                    ts="2026-05-01T12:00:00.000Z",
                ),
                _wrap(_assistant_msg(msg_id="d2"), ts="2026-05-02T12:00:00.000Z"),
            ],
        )
        cache: dict = {}
        tu.get_or_compute(path, cache)
        _append_lines(
            path,
            [
                _wrap(_assistant_msg(msg_id="d3"), ts="2026-05-03T12:00:00.000Z"),
                _wrap(
                    _assistant_with_blocks([_skill_block("retro")], msg_id="s2"),
                    ts="2026-05-03T12:00:00.000Z",
                ),
            ],
        )
        by_day, skills = tu.get_or_compute(path, cache)
        # Oldest day trimmed out; incremental result still equals a full walk.
        assert set(by_day) == {"2026-05-02", "2026-05-03"}
        assert (by_day, skills) == tu.walk_jsonl_buckets(path)

    def test_tail_ids_capped_and_recent_biased(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        n = tu.TAIL_MSG_ID_LOOKBACK * 3
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id=f"m{i}")) for i in range(n)])
        tu.get_or_compute(path, cache := {})
        ids = _entry(path, cache)["tail_msg_ids"]
        assert len(ids) == tu.TAIL_MSG_ID_LOOKBACK
        assert ids[-1] == f"m{n - 1}"

    def test_tail_ids_survive_append_with_no_assistant_messages(self, tmp_path: Path) -> None:
        """A push whose new bytes are pure user turns must not drop the
        straddle guard for the message still in flight."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="streaming"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        _append_lines(path, [{"message": {"role": "user"}, "timestamp": "2026-05-01T12:00:00Z"}])
        tu.get_or_compute(path, cache)
        assert _entry(path, cache)["tail_msg_ids"] == ["streaming"]
        # And the guard still works.
        _append_lines(path, [_wrap(_assistant_msg(msg_id="streaming"))])
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 100


class TestCarryTailIds:
    def test_reseen_id_moves_to_the_end(self) -> None:
        """Recency is the point of the window. An id seen again in this
        segment must not keep its stale position — otherwise the trim
        evicts exactly the id most likely to straddle the next boundary.

        Asserts the EXACT tuple, not just length/membership. A weaker
        assertion passes even when the seed keeps its stale copy, which
        emits a DUPLICATE into `tail_msg_ids`; `_resume_plan` then rejects
        that entry on its uniqueness check and the file falls back to a
        full walk forever — silently, on exactly the actively-streaming
        sessions this feature exists to speed up. Same silent-degradation
        shape as the fixed-window head-probe bug."""
        seed = tuple(f"m{i}" for i in range(tu.TAIL_MSG_ID_LOOKBACK))
        # m0 is the OLDEST seed entry, and it recurs at the end of this
        # segment alongside one brand-new id.
        got = tu._carry_tail_ids(seed, ["new", "m0"])
        assert (
            got
            == ("m1", "m2", "m3", "m4", "m5", "m6", "m7", "new", "m0")[-tu.TAIL_MSG_ID_LOOKBACK :]
        )
        assert len(set(got)) == len(got), "a duplicate here poisons the persisted entry"

    def test_output_is_always_unique(self) -> None:
        """`_resume_plan` rejects an entry whose tail_msg_ids contain a
        duplicate, so emitting one is equivalent to disabling resume."""
        got = tu._carry_tail_ids(("a", "b"), ["b", "a", "b"])
        assert len(set(got)) == len(got)
        assert set(got) == {"a", "b"}

    def test_empty_segment_preserves_seed(self) -> None:
        seed = ("a", "b")
        assert tu._carry_tail_ids(seed, []) == seed

    def test_window_is_capped(self) -> None:
        got = tu._carry_tail_ids((), [f"m{i}" for i in range(100)])
        assert len(got) == tu.TAIL_MSG_ID_LOOKBACK
        assert got[-1] == "m99"

    def test_oversize_id_is_dropped_not_truncated(self) -> None:
        """A jsonl line may be up to MAX_JSONL_LINE_BYTES, so message.id
        is unbounded peer-controlled input. Eight huge ids would push the
        cache past lockedjson's 64 MiB read ceiling and wedge the fleet
        into a permanent cold walk. Dropped, not truncated — truncation
        could alias two distinct ids and silently under-count."""
        huge = "x" * (tu._MAX_TAIL_MSG_ID_LEN + 1)
        got = tu._carry_tail_ids((), ["ok", huge])
        assert got == ("ok",)

    def test_oversize_id_dropped_from_seed_too(self) -> None:
        huge = "y" * (tu._MAX_TAIL_MSG_ID_LEN + 1)
        got = tu._carry_tail_ids((huge, "keep"), ["new"])
        assert got == ("keep", "new")

    def test_boundary_length_is_kept(self) -> None:
        exact = "z" * tu._MAX_TAIL_MSG_ID_LEN
        assert tu._carry_tail_ids((), [exact]) == (exact,)


class TestResumePlanRejection:
    """`_resume_plan` is the single gate on resume eligibility. Anything
    it can't fully validate must fall back to a full walk — a wrong
    resume silently produces wrong token numbers forever."""

    def _warm(self, tmp_path: Path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        return path, cache, _entry(path, cache)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda e: e.pop("offset"), id="no-offset"),
            pytest.param(lambda e: e.pop("head"), id="no-head"),
            pytest.param(lambda e: e.update(offset=-1), id="negative-offset"),
            pytest.param(lambda e: e.update(offset=True), id="bool-offset"),
            pytest.param(lambda e: e.update(offset=10**9), id="offset-past-eof"),
            pytest.param(lambda e: e.update(head=""), id="empty-head"),
            pytest.param(lambda e: e.update(head=123), id="non-str-head"),
            pytest.param(lambda e: e.update(by_day="nope"), id="malformed-by-day"),
            pytest.param(lambda e: e.update(skills_by_day=None), id="malformed-skills"),
            pytest.param(lambda e: e.update(tail_msg_ids="nope"), id="tail-not-list"),
            pytest.param(lambda e: e.update(tail_msg_ids=[1, 2]), id="tail-non-str"),
            pytest.param(lambda e: e.update(size="nope"), id="non-int-size"),
            pytest.param(lambda e: e.pop("head_len"), id="no-head-len"),
            pytest.param(lambda e: e.update(head_len=1), id="shrunk-head-len"),
            pytest.param(lambda e: e.update(head_len=0), id="zero-head-len"),
            pytest.param(lambda e: e.update(head_len=True), id="bool-head-len"),
            pytest.param(lambda e: e.update(tail_msg_ids=["a", "a"]), id="tail-duplicates"),
            pytest.param(lambda e: e.update(tail_msg_ids=[""]), id="tail-empty-str"),
            pytest.param(
                lambda e: e.update(tail_msg_ids=["x" * 129]),
                id="tail-overlong",
            ),
            pytest.param(
                lambda e: e.update(tail_msg_ids=[f"m{i}" for i in range(99)]),
                id="tail-too-many",
            ),
        ],
    )
    def test_corrupt_entry_falls_back_to_full_walk(self, tmp_path: Path, mutate) -> None:
        path, cache, entry = self._warm(tmp_path)
        # Poison the cached buckets so a resume (rather than a full walk)
        # would be visible in the result.
        entry["by_day"] = {"2026-05-01": {"input": 99999, "by_model": {}}}
        mutate(entry)
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        by_day, _skills = tu.get_or_compute(path, cache)
        # Full walk => the poison is discarded and both messages counted.
        assert by_day["2026-05-01"]["input"] == 200

    def _isolating_entry(self, path: Path, entry: dict, *, offset: int, size: int) -> None:
        """Rewrite an entry so that ONLY the size/offset guard can reject it.

        Mutation testing showed the three size/offset clauses were mutually
        masking: every test that claimed to pin one of them actually died on
        the `head_len != head_probe_len(offset)` check or on a fingerprint
        mismatch, so deleting the ENTIRE guard line left the suite green.
        This helper recomputes `head_len` and `head` to be canonical for the
        planted offset, removing every other reason to reject."""
        entry["offset"] = offset
        entry["size"] = size
        probe = tu.head_probe_len(offset)
        entry["head_len"] = probe
        entry["head"] = tu.head_fingerprint(path, probe)

    def test_offset_beyond_recorded_size_rejected(self, tmp_path: Path) -> None:
        """An offset past the size it was recorded against is corrupt: the
        bytes between recorded-size and offset were never in any bucket.
        Resuming there would skip them silently."""
        path, cache, entry = self._warm(tmp_path)
        entry["by_day"] = {"2026-05-01": {"input": 99999, "by_model": {}}}
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        # offset is valid against the CURRENT file but past the recorded
        # size. Everything else about the entry is canonical.
        self._isolating_entry(path, entry, offset=300, size=1)
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 200

    def test_offset_past_current_eof_rejected(self, tmp_path: Path) -> None:
        path, cache, entry = self._warm(tmp_path)
        entry["by_day"] = {"2026-05-01": {"input": 99999, "by_model": {}}}
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        size_now = path.stat().st_size
        self._isolating_entry(path, entry, offset=size_now + 5_000, size=size_now + 5_000)
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 200

    def test_shrunk_file_rejected_even_when_head_still_matches(self, tmp_path: Path) -> None:
        """The shrink clause, isolated. A file that lost bytes but kept its
        head would otherwise resume against buckets covering bytes that are
        no longer there."""
        path = tmp_path / "session.jsonl"
        # Big enough that head_len caps at _HEAD_PROBE_BYTES, so a prefix
        # truncation that stays above the cap keeps the probe readable and
        # matching — otherwise the short-read guard catches it first and the
        # shrink clause stays unexercised.
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id=f"m{i}")) for i in range(200)])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        entry = _entry(path, cache)
        assert entry["head_len"] == tu._HEAD_PROBE_BYTES
        entry["by_day"] = {"2026-05-01": {"input": 99999, "by_model": {}}}
        data = path.read_bytes()
        assert len(data) > tu._HEAD_PROBE_BYTES * 3
        cut = data.index(b"\n", len(data) // 2) + 1
        path.write_bytes(data[:cut])
        assert tu.head_fingerprint(path, entry["head_len"]) == entry["head"]
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day == tu.walk_jsonl_buckets(path)[0]
        assert by_day["2026-05-01"]["input"] != 99999

    def test_shrunk_head_len_rejected_even_when_digest_agrees(self, tmp_path: Path) -> None:
        """The head_len EQUALITY clause, isolated. `shrunk-head-len` in the
        matrix above dies on a digest mismatch, not on the equality check —
        so the clause survived mutation. Here the digest is recomputed over
        the shrunken window, so the entry is self-consistent and ONLY the
        `head_len != head_probe_len(offset)` check can reject it. Without
        that check a one-byte probe would 'pass' while proving nothing."""
        path, cache, entry = self._warm(tmp_path)
        entry["by_day"] = {"2026-05-01": {"input": 99999, "by_model": {}}}
        entry["head_len"] = 1
        entry["head"] = tu.head_fingerprint(path, 1)
        assert tu.head_fingerprint(path, entry["head_len"]) == entry["head"]
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 200

    def test_valid_entry_does_resume(self, tmp_path: Path) -> None:
        """Guard against the rejection matrix above degenerating into
        'always full walk' — the resume path must still fire."""
        path, cache, _entry_ = self._warm(tmp_path)
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        seen: dict = {}
        original = tu.walk_jsonl_segment

        def spy(p: Path, **kwargs):
            seen.update(kwargs)
            return original(p, **kwargs)

        import unittest.mock as _mock

        with _mock.patch.object(tu, "walk_jsonl_segment", spy):
            by_day, _skills = tu.get_or_compute(path, cache)
        assert seen.get("start_offset", 0) > 0
        assert by_day["2026-05-01"]["input"] == 200


class TestReadFailureDoesNotPersist:
    def test_io_failure_leaves_cache_untouched(self, tmp_path: Path, monkeypatch) -> None:
        """A failed read must NOT persist. Both stats bracket the failure
        and agree, so persisting would pin the current size/mtime to
        buckets that never saw the current bytes — a permanent cache hit
        that stops counting the session forever."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])

        def boom(*args, **kwargs):
            raise OSError("simulated read failure")

        monkeypatch.setattr("builtins.open", boom)
        cache: dict = {}
        by_day, skills = tu.get_or_compute(path, cache)
        assert (by_day, skills) == ({}, {})
        assert cache == {}

    def test_io_failure_on_resume_keeps_prior_entry(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        before = json.dumps(_entry(path, cache), sort_keys=True)

        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        monkeypatch.setattr(
            tu, "walk_jsonl_segment", lambda p, **kw: tu.JsonlSegment({}, {}, 0, (), ok=False)
        )
        tu.get_or_compute(path, cache)
        # Entry unchanged — the next push re-walks and picks up m2.
        assert json.dumps(_entry(path, cache), sort_keys=True) == before


class TestBucketMergeHelpers:
    def test_merge_token_days_sums_flat_fields_and_by_model(self) -> None:
        target: dict = {}
        src = {
            "2026-05-01": {
                "input": 10,
                "cache_create": 1,
                "cache_read": 2,
                "output": 3,
                "by_model": {"claude-opus-5": {"input": 10, "output": 3}},
            }
        }
        tu.merge_token_days(target, src)
        tu.merge_token_days(target, src)
        assert target["2026-05-01"]["input"] == 20
        assert target["2026-05-01"]["output"] == 6
        assert target["2026-05-01"]["by_model"]["claude-opus-5"]["input"] == 20

    def test_merge_token_days_tolerates_missing_by_model(self) -> None:
        target: dict = {}
        tu.merge_token_days(target, {"2026-05-01": {"input": 5}})
        assert target["2026-05-01"]["input"] == 5
        assert target["2026-05-01"]["by_model"] == {}

    def test_malformed_nested_buckets_are_skipped_not_raised(self) -> None:
        """Both callers read from the on-disk cache. One malformed entry
        raising here would take down the events tail on EVERY push while
        the poisoned entry survives — a permanent outage from one bad key."""
        target: dict = {}
        tu.merge_token_days(
            target,
            {
                "2026-05-01": "not-a-dict",
                "2026-05-02": {"input": 4, "by_model": "not-a-dict"},
                "2026-05-03": {"input": 1, "by_model": {"m": "not-a-dict"}},
            },
        )
        assert "2026-05-01" not in target
        assert target["2026-05-02"]["input"] == 4
        assert target["2026-05-02"]["by_model"] == {}
        assert target["2026-05-03"]["by_model"] == {}

        skills: dict = {}
        tu.merge_skill_days(
            skills,
            {"2026-05-01": "nope", "2026-05-02": {"ship": 1, "bad": "x", "flag": True}},
        )
        assert "2026-05-01" not in skills
        assert skills["2026-05-02"] == {"ship": 1}

    def test_non_int_bucket_value_does_not_raise(self) -> None:
        """v0.12.15 made the on-disk cache a merge SOURCE, so one non-int
        value in one cached bucket would raise TypeError out of
        get_or_compute. `_run_events_tail` catches that as `events tail
        failed` on EVERY push while the poisoned entry survives — a
        permanent outage from one bad key. Non-ints contribute 0."""
        target: dict = {}
        tu.merge_token_days(
            target,
            {"2026-05-01": {"input": "99999", "output": 7, "by_model": {"m": {"input": None}}}},
        )
        assert target["2026-05-01"]["input"] == 0
        assert target["2026-05-01"]["output"] == 7
        assert target["2026-05-01"]["by_model"]["m"]["input"] == 0

    def test_non_int_value_in_cached_entry_survives_resume(self, tmp_path: Path) -> None:
        """End-to-end shape of the same defect: a poisoned cache entry must
        degrade, not take down the events tail."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        cache: dict = {}
        tu.get_or_compute(path, cache)
        _entry(path, cache)["by_day"]["2026-05-01"]["input"] = "corrupt"
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])
        by_day, _skills = tu.get_or_compute(path, cache)
        assert by_day["2026-05-01"]["input"] == 100  # poison dropped, new line counted

    def test_merge_skill_days_accumulates(self) -> None:
        target: dict = {}
        tu.merge_skill_days(target, {"2026-05-01": {"ship": 1, "retro": 2}})
        tu.merge_skill_days(target, {"2026-05-01": {"ship": 3}, "2026-05-02": {"qa": 1}})
        assert target == {"2026-05-01": {"ship": 4, "retro": 2}, "2026-05-02": {"qa": 1}}

    def test_events_aggregator_uses_the_shared_helpers(self) -> None:
        """Pins the consolidation: events.py must not re-grow a hand-rolled
        copy of the merge loops. See the
        mirrored-predicate-drifts-when-one-side-gains-logic pitfall — this
        module has shipped that bug twice."""
        import inspect

        from mind_meld import events

        src = inspect.getsource(events._aggregate_jsonl_views_for_project)
        assert "merge_token_days" in src
        assert "merge_skill_days" in src
        assert "setdefault(day" not in src


class TestHeadFingerprint:
    def test_ignores_bytes_past_the_probe_window(self, tmp_path: Path) -> None:
        """Only the probed prefix identifies the file — appends past it
        must not change the digest, or every append looks like a rewrite."""
        a = tmp_path / "a.jsonl"
        b = tmp_path / "b.jsonl"
        head = b"same head\n" + b"h" * tu._HEAD_PROBE_BYTES
        a.write_bytes(head + b"x" * 100)
        b.write_bytes(head + b"y" * 9000)
        n = tu._HEAD_PROBE_BYTES
        assert tu.head_fingerprint(a, n) == tu.head_fingerprint(b, n)

    def test_differs_on_head_change(self, tmp_path: Path) -> None:
        a = tmp_path / "a.jsonl"
        a.write_bytes(b"one\n")
        first = tu.head_fingerprint(a, 4)
        a.write_bytes(b"two\n")
        assert tu.head_fingerprint(a, 4) != first

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert tu.head_fingerprint(tmp_path / "nope.jsonl", 16) is None

    def test_short_read_returns_none(self, tmp_path: Path) -> None:
        """Fewer bytes on disk than the probe claims means the file
        shrank — it is not the file we fingerprinted."""
        a = tmp_path / "a.jsonl"
        a.write_bytes(b"tiny")
        assert tu.head_fingerprint(a, 4096) is None

    def test_zero_probe_returns_none(self, tmp_path: Path) -> None:
        a = tmp_path / "a.jsonl"
        a.write_bytes(b"x")
        assert tu.head_fingerprint(a, 0) is None

    def test_probe_len_never_exceeds_offset(self) -> None:
        """A probe reaching past the accounted region would cover bytes
        an append can change, so the entry would never resume."""
        assert tu.head_probe_len(0) == 0
        assert tu.head_probe_len(10) == 10
        assert tu.head_probe_len(tu._HEAD_PROBE_BYTES * 3) == tu._HEAD_PROBE_BYTES
        assert tu.head_probe_len(-5) == 0

    def test_short_file_still_resumes_across_appends(self, tmp_path: Path) -> None:
        """Regression: a fixed 4 KiB probe made every file smaller than
        the window re-fingerprint differently on each append, silently
        degrading to a full walk forever."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        assert path.stat().st_size < tu._HEAD_PROBE_BYTES
        cache: dict = {}
        tu.get_or_compute(path, cache)
        _append_lines(path, [_wrap(_assistant_msg(msg_id="m2"))])

        seen: dict = {}
        original = tu.walk_jsonl_segment

        def spy(p: Path, **kwargs):
            seen.update(kwargs)
            return original(p, **kwargs)

        import unittest.mock as _mock

        with _mock.patch.object(tu, "walk_jsonl_segment", spy):
            by_day, _skills = tu.get_or_compute(path, cache)
        assert seen.get("start_offset", 0) > 0
        assert by_day["2026-05-01"]["input"] == 200

    def test_fingerprint_read_is_bracketed_by_the_stability_stat(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The pre/post stat pair must BRACKET the probe read. Reading the
        fingerprint after the final stat leaves a window in which the file
        is replaced and we persist the old buckets under the REPLACEMENT's
        fingerprint — an entry that later licenses a resume into a file
        none of its buckets ever saw."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        cache: dict = {}

        original = tu.head_fingerprint

        def swap_then_fingerprint(p: Path, n: int):
            # Simulate the file being replaced at the exact moment we
            # fingerprint it. The post-walk stat must notice.
            _write_jsonl(p, [_wrap(_assistant_msg(msg_id="zzz", input_tokens=777))] * 4)
            return original(p, n)

        monkeypatch.setattr(tu, "head_fingerprint", swap_then_fingerprint)
        tu.get_or_compute(path, cache)
        # Drift detected => nothing persisted => no entry claiming the
        # replacement's identity with the original's buckets.
        assert cache == {}

    def test_unreadable_head_persists_legacy_shape(self, tmp_path: Path, monkeypatch) -> None:
        """No fingerprint → no resume fields, so the next miss walks in
        full rather than trusting an offset we can't validate."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_wrap(_assistant_msg(msg_id="m1"))])
        monkeypatch.setattr(tu, "head_fingerprint", lambda p, n: None)
        cache: dict = {}
        tu.get_or_compute(path, cache)
        entry = _entry(path, cache)
        assert "offset" not in entry
        assert entry["by_day"]["2026-05-01"]["input"] == 100
