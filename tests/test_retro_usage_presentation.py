"""One synthetic corpus for Track 58A's before/after and acceptance views."""

import json
from datetime import datetime, timezone
from pathlib import Path

from mind_meld import token_usage as tu
from mind_meld.skills.retro_fleet import aggregator as agg


def usage(input=0, cache_create=0, cache_read=0, output=0):
    return dict(input=input, cache_create=cache_create, cache_read=cache_read, output=output)


def day_bucket(models):
    bucket = tu.zero_day_bucket()
    for model, counters in models.items():
        tu.merge_usage_bucket(bucket, counters)
        bucket["by_model"][model] = counters.copy()
    return bucket


def presentation_data(state="populated"):
    since = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    until = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    data = agg.RetroData(window_days=7, since=since, until=until)
    ids = {"dev-a", "dev-b"}
    data.fleet = agg.FleetState(
        devices_known=2,
        devices_in_events=ids,
        devices_known_list=[{"device_id": d} for d in sorted(ids)],
    )
    events = []
    if state != "absent":
        for device, models in (
            ("dev-a", {"claude-sonnet-5": usage(1_000_000, 2_000_000, 3_000_000, 100_000)}),
            ("dev-b", {"claude-fable-5-1": usage(100_000, 1_000_000, 4_000_000, 100_000)}),
        ):
            events.append(
                dict(
                    v=2,
                    type="sessions-snapshot",
                    device=device,
                    ts=until.isoformat(),
                    projects=[
                        dict(
                            claude_dir="sample",
                            sessions=2,
                            last_session_at=until.isoformat(),
                            tokens_by_day={"2026-09-10": day_bucket(models)},
                            skills_by_day={},
                        )
                    ],
                )
            )
        for device, models in (
            (
                "dev-a",
                {
                    "gpt-6-astra": usage(1_000_000, 100_000, 2_000_000, 100_000),
                    "grok-4.6-build": usage(1_000_000),
                    "grok-unknown": usage(50_000),
                },
            ),
            ("dev-b", {"claude-opus-5": usage(1_000_000, 1_000_000)}),
        ):
            hosts = {}
            for model, counters in models.items():
                family = (
                    "grok"
                    if model.startswith("grok-")
                    else ("claude" if model.startswith("claude-") else "codex")
                )
                dest = hosts.setdefault(family, {}).setdefault("2026-09-10", usage())
                tu.merge_usage_bucket(dest, counters)
            event = dict(
                v=2,
                type="host-usage-snapshot",
                device=device,
                ts=until.isoformat(),
                hosts=hosts,
                token_sources=["codex", "grok"] if device == "dev-a" else ["codex"],
                active_days=["2026-09-10"],
                counter_semantics="disjoint-v1",
                tokens_by_day={"2026-09-10": day_bucket(models)},
            )
            if state == "degraded" and device == "dev-b":
                event["degraded_sources"] = ["grok"]
            events.append(event)
        if state == "degraded":
            events[0]["projects"].append(
                dict(claude_dir="cold", sessions=1, last_session_at=until.isoformat())
            )
    data.sessions, data.skills = agg.aggregate_sessions(events, since=since, until=until)
    data.host_inventory = agg.aggregate_host_usage(
        events, since=since, until=until, registered_ids=ids
    )
    return data


GOLDENS = Path(__file__).parent / "fixtures" / "retro_usage"


def health(out):
    """MM_HEALTH issues from a first-pass render."""
    if "<!-- MM_HEALTH -->" not in out:
        return []
    block = out.split("<!-- MM_HEALTH -->", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)["issues"]


def entry(out, code):
    return next((e for e in health(out) if e["code"] == code), None)


def agents(data):
    return agg.aggregate_agent_usage(data, machines_known=data.fleet.devices_known)


def row(data, key):
    return next((r for r in agents(data).rows if r.key == key), None)


def test_every_agent_reports_the_same_four_measures():
    """The 1.1 contract. Pre-1.1 Claude got a token table and a window-sum
    cost while Codex and Grok got per-machine day counts under a do-not-sum
    heading, which is what made the card read Claude-first."""
    data = presentation_data()
    keys = [r.key for r in agents(data).rows]
    assert keys == ["claude", "codex", "grok"]
    for r in agents(data).rows:
        assert r.tokens > 0
        assert r.active_days >= 1
        assert r.machines >= 1
    body = agg.format_retro(data)
    assert "| Agent | Tokens | Days | Machines | Est. cost | Top model |" in body


def test_claude_tokens_reconcile_with_the_four_session_counters():
    data = presentation_data()
    counts = [
        data.sessions.tokens_input,
        data.sessions.tokens_cache_create,
        data.sessions.tokens_cache_read,
        data.sessions.tokens_output,
    ]
    assert sum(counts) == 11_300_000
    assert row(data, "claude").tokens >= sum(counts)
    assert data.sessions.token_devices == {"dev-a": data.until, "dev-b": data.until}


def test_host_rows_sum_across_machines():
    """The fleet row adds every contributing machine's in-window ledger."""
    data = presentation_data()
    for key in ("codex", "grok"):
        expected = 0
        machines = set()
        for device, snap in data.host_inventory.by_device.items():
            for _day, bucket in (snap.lifetime_by_family.get(key) or {}).items():
                if tu.sum_bucket(bucket) > 0:
                    expected += tu.sum_bucket(bucket)
                    machines.add(device)
        assert row(data, key).tokens == expected, key
        assert row(data, key).machines == len(machines), key


def test_host_claude_models_are_not_silently_dropped():
    """dev-b's host ledger carries ``claude-opus-5``. The row means "usage of
    this model family across the fleet", so the host side adds into the Claude
    row rather than vanishing because Claude Code already produced one."""
    data = presentation_data()
    host_claude = sum(
        tu.sum_bucket(bucket)
        for snap in data.host_inventory.by_device.values()
        for _day, bucket in (snap.lifetime_by_family.get("claude") or {}).items()
    )
    assert host_claude > 0, "corpus must exercise the collision"
    session_only = (
        data.sessions.tokens_input
        + data.sessions.tokens_cache_create
        + data.sessions.tokens_cache_read
        + data.sessions.tokens_output
    )
    assert row(data, "claude").tokens == session_only + host_claude


def test_degraded_reader_floors_its_agent_and_names_the_cause():
    data = presentation_data("degraded")
    grok = row(data, "grok")
    assert agg._agent_row_cost(grok)[1] is True
    assert any("failed" in c for c in grok.floor_causes)
    out = agg.format_retro(data)
    assert "≥$" in out
    grok_floor = next(e for e in health(out) if e["code"] == "cost_floor" and e["agent"] == "Grok")
    assert "a host reader failed" in grok_floor["detail"]


def test_unpriced_model_is_counted_in_tokens_and_excluded_from_cost():
    data = presentation_data()
    grok = row(data, "grok")
    assert grok.tokens > 0
    causes = agg.agent_row_floor_causes(grok)
    assert any("unpriced" in c and "grok-unknown" in c for c in causes)
    out = agg.format_retro(data)
    assert "grok-unknown" in entry(out, "cost_floor")["detail"]


def test_claude_coverage_floor_does_not_floor_a_healthy_host_row():
    """Per-agent floors are independent. Pre-1.1 one machine's floor condition
    set the rate basis for every priced cell in the per-machine section."""
    from dataclasses import replace

    data = presentation_data("degraded")
    data.host_inventory.by_device.pop("dev-a")
    snap = data.host_inventory.by_device["dev-b"]
    data.host_inventory.by_device["dev-b"] = replace(
        snap,
        degraded=(),
        tokens_by_day={"2026-09-10": day_bucket({"gpt-6-astra": usage(1_000_000)})},
        lifetime_by_family={"codex": {"2026-09-10": usage(1_000_000)}},
    )
    assert agg._agent_row_cost(row(data, "claude"))[1] is True
    assert agg._agent_row_cost(row(data, "codex"))[1] is False
    out = agg.format_retro(data)
    assert "| Codex | 1.0M | 1 | 1 | ~$" in out


def test_model_names_are_bounded_and_defanged():
    data = presentation_data()
    models = {f"claude-opus-{i:03d}": usage(i + 1) for i in range(200)}
    hostile = "claude-opus-999\x1b[31m|`\n" + "x" * 200
    models[hostile] = usage(1_000_000)
    data.sessions.tokens_by_model = models
    out = agg.format_retro(data)
    assert hostile not in out and "\x1b" not in out
    notes = " ".join(agg._extrapolation_notes(models, scope="Claude"))
    assert "Models priced by family extrapolation" in notes
    assert "(+193 more)" in notes
    assert "(+" not in " ".join(
        agg._extrapolation_notes({m: usage(1) for m in tu.VERIFIED_MODEL_IDS}, scope="Claude")
    )
    # The top-model cell is one bounded name, never a table of 200.
    assert len(agg._agent_top_model(row(data, "claude"))) <= 40


def stale_data(*, exactly_at_since=False, empty=False):
    data = presentation_data("absent")
    day = data.since.date().isoformat()
    ts = data.since if exactly_at_since else data.since.replace(hour=1)
    models = {} if empty else {"gpt-6-astra": usage(1_000_000)}
    ev = dict(
        v=2,
        type="host-usage-snapshot",
        ts=ts.isoformat(),
        device="dev-a",
        token_sources=["codex"],
        active_days=[] if empty else [day],
        hosts={} if empty else {"codex": {day: usage(1_000_000)}},
        tokens_by_day={} if empty else {day: day_bucket(models)},
        counter_semantics="disjoint-v1",
    )
    data.host_inventory = agg.aggregate_host_usage(
        [ev], since=data.since, until=data.until, registered_ids={"dev-a"}
    )
    return data


def test_stale_on_window_start_day_contributes_nothing_but_is_not_zero():
    data = stale_data()
    view = agg._agent_rhythm_view(
        data.host_inventory, since=data.since, until=data.until, machines_known=2
    )
    assert view.machines_with_activity == 0 and not view.any_activity
    assert row(data, "codex") is None
    out = agg.format_retro(data, name="Example")
    assert "AGENTS (0 of 2 registered machines)" in out
    assert "$0" not in out
    snap = data.host_inventory.by_device["dev-a"]
    assert agg._windowed_host_by_model(snap, "2026-09-07", "2026-09-14") == ({}, False)
    assert agg.window_bounds(snap, "2026-09-07", "2026-09-14") is None
    assert (
        "Agent-log snapshots all predate this window"
        in entry(agg.format_retro(data), "agent_coverage")["detail"]
    )


def test_stale_empty_family_is_not_zero():
    data = stale_data(empty=True)
    assert row(data, "codex") is None
    assert "$0" not in agg.format_retro(data)


def test_snapshot_exactly_at_since_keeps_first_day():
    data = stale_data(exactly_at_since=True)
    view = agg._agent_rhythm_view(
        data.host_inventory, since=data.since, until=data.until, machines_known=2
    )
    assert view.machines_with_activity == 1 and view.any_activity
    codex = row(data, "codex")
    assert codex.active_days == 1 and codex.tokens == 1_000_000
    out = agg.format_retro(data)
    assert "| Codex | 1.0M | 1 | 1 | ~$10.00 |" in out
    assert "all predate" not in out


def test_currency_bounds_on_both_sides_of_precision_transition():
    for amount, floor, estimate, ceiling in (
        (99.994, "$99.99", "$99.99", "$100"),
        (99.996, "$99.99", "$100", "$100"),
        (100.006, "$100", "$100", "$101"),
        (100.60, "$100", "$101", "$101"),
        (0.0001, "$0.00", "$0.00", "$0.01"),
    ):
        assert agg._format_usd(amount, bound="floor") == floor
        assert agg._format_usd(amount) == estimate
        assert agg._format_usd(amount, bound="ceiling") == ceiling
        assert float(floor[1:]) <= amount <= float(ceiling[1:])


def test_card_short_dollars_never_round_past_a_floor():
    """A figure printed under ``≥`` must not round UP past the bound it
    claims; the card and the body table must agree on the same number."""
    for amount in (546.5, 999.95, 1249.0, 2239.4):
        short = agg._format_usd_short(amount, floor=True)
        value = float(short.lstrip("$").rstrip("k"))
        if short.endswith("k"):
            value *= 1000
        assert value <= amount + 1e-9, (short, amount)


def test_empty_device_label_falls_back_rather_than_rendering_blank():
    from dataclasses import replace

    data = presentation_data()
    snap = data.host_inventory.by_device["dev-a"]
    data.host_inventory.by_device = {"": replace(snap, device="")}
    labels = agg.device_labels(data.fleet)
    assert agg.device_label("", labels) == "(unnamed)"
    agg.format_retro(data)  # must not raise


def test_fleet_refresh_remedies_require_the_attended_producer_version():
    data = stale_data()
    snap = data.host_inventory.by_device["dev-a"]
    remedies = [
        agg._host_detail_phrase("absent", None),
        *agg._agent_coverage_notes(presentation_data("absent")),
        *agg._host_reader_coverage_notes([snap]),
    ]
    assert len(remedies) >= 3
    for remedy in remedies:
        assert f"upgrade mind-meld to {agg.ATTENDED_USAGE_MIN_VERSION}+" in remedy
        assert "verify with `mm --version`, then run `mm push`" in remedy


def test_populated_absent_degraded_goldens_at_terminal_widths(monkeypatch):
    import os
    import time
    from io import StringIO

    from rich.console import Console
    from rich.markdown import Markdown

    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        for width in (80, 120):
            monkeypatch.setenv("COLUMNS", str(width))
            for state in ("populated", "absent", "degraded"):
                out = agg.format_retro(presentation_data(state), name="Example")
                assert out == (GOLDENS / f"{state}.md").read_text()
                stream = StringIO()
                console = Console(file=stream, width=width, color_system=None)
                console.print(Markdown(out))
                assert all(len(line) <= width for line in stream.getvalue().splitlines())
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def cursor_capture_view(cursor_store, mode):
    """Mutate real metadata; drive the reader, capture, writer, acceptor and render."""
    from mind_meld import events_tail
    from mind_meld import host_usage as hu

    paths = sorted(cursor_store.glob("*/runs.ndjson"))
    records = [json.loads(line) for path in paths for line in path.read_text().splitlines()]
    for path in paths[1:]:
        path.unlink()
    records = records[:2] if mode in {"mixed-fast", "usage-ref"} else records[:1]
    if mode in {"fast-only", "mixed-fast"}:
        records[0]["model"]["params"][0]["value"] = "true"
    if mode == "cache-write":
        records[0]["usage"]["cacheWriteTokens"] = 10
        records[0]["usage"]["totalTokens"] += 10
    if mode in {"usage-ref", "usage-ref-only"}:
        records[0]["usage"] = None
        records[0]["usageRef"] = "synthetic-unresolved-ref"
        if len(records) > 1:
            records[0]["endedAt"] = records[1]["endedAt"]
    paths[0].write_text("".join(json.dumps(r) + "\n" for r in records))
    readers = events_tail._default_host_readers([], cursor_consented=True)
    if mode == "usage-ref-only":

        def codex(*, deadline):
            return hu.HostUsageResult(
                {"codex": {"2026-09-23": usage(100)}},
                complete=True,
                tokens_by_day={"2026-09-23": day_bucket({"gpt-6-astra": usage(100)})},
            )

        readers = (("codex", codex), *readers)
    capture, rows = events_tail._capture_host_snapshot("cursor-mac", readers, host_budget_ms=5000)
    assert len(rows) == 1
    since = datetime(2026, 9, 21, tzinfo=timezone.utc)
    until = datetime(2026, 9, 23, 23, tzinfo=timezone.utc)
    rows[0]["ts"] = until.isoformat()
    data = agg.RetroData(window_days=7, since=since, until=until)
    data.fleet = agg.FleetState(
        devices_known=1,
        devices_in_events={"cursor-mac"},
        devices_known_list=[{"device_id": "cursor-mac"}],
    )
    data.host_inventory = agg.aggregate_host_usage(
        rows, since=since, until=until, registered_ids={"cursor-mac"}
    )
    assert data.host_inventory.by_device
    return capture, rows[0], data, agg.format_retro(data)


def test_cursor_fast_only_preserves_tokens_but_has_no_price(cursor_store):
    capture, published, data, out = cursor_capture_view(cursor_store, "fast-only")
    assert capture.complete
    assert published["token_sources"] == ["cursor"]
    assert published["counter_semantics"] == "disjoint-v1"
    assert row(data, "grok").counters_known
    assert row(data, "grok").tokens == 25_891_771
    assert agg._agent_row_cost(row(data, "grok")) == (None, False)
    assert entry(out, "cost_unavailable")
    assert "—" in out


def test_cursor_mixed_fast_prices_only_standard_subtotal(cursor_store):
    _, _, data, out = cursor_capture_view(cursor_store, "mixed-fast")
    grok = row(data, "grok")
    assert grok.tokens == 30_871_121
    total, is_floor = agg._agent_row_cost(grok)
    assert is_floor
    assert abs(total - (2_618_647 * 2 + 2_343_424 * 0.5 + 17_279 * 6) / 1_000_000) < 1e-9
    assert entry(out, "cost_floor")
    assert "≥" in out
    assert "grok-4.7-fast" in out
    assert "Grok's logs do not record" not in out
    assert "per-request context tier" in out


def test_cursor_cache_write_is_visible_partial_not_suppressed(cursor_store):
    _, published, data, out = cursor_capture_view(cursor_store, "cache-write")
    assert published["partial_sources"] == ["cursor"]
    assert row(data, "grok").tokens == 25_891_781
    assert "Cursor via Conductor" in out
    assert entry(out, "cost_floor")
    assert "at most" not in out


def test_cursor_usage_ref_with_known_same_day_usage_publishes_partial(cursor_store):
    _, published, data, out = cursor_capture_view(cursor_store, "usage-ref")
    assert published["partial_sources"] == ["cursor"]
    assert row(data, "grok").tokens == 4_979_350
    assert "Cursor via Conductor" in out
    assert entry(out, "cost_floor")


def test_cursor_usage_ref_only_cannot_disappear_as_completed_empty(cursor_store):
    capture, published, data, out = cursor_capture_view(cursor_store, "usage-ref-only")
    assert capture.dropped == (("cursor", "partial"),)
    assert published["token_sources"] == ["codex"]
    assert published["degraded_sources"] == ["cursor"]
    assert "cursor" not in published["empty_sources"]
    assert row(data, "grok") is None
    assert "Cursor via Conductor" in out
    assert entry(out, "cost_floor")
