"""One synthetic corpus for Track 58A's before/after and acceptance views."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mind_meld import events
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
        devices_known_list=(
            [{"device_id": "dev-a", "last_seen_version": "0.14.16"}, {"device_id": "dev-b"}]
            if state == "absent"
            else [{"device_id": d} for d in sorted(ids)]
        ),
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
    assert "dev-b: a host reader failed (Grok Build)" in grok.floor_causes
    out = agg.format_retro(data)
    assert "≥$" in out
    grok_floor = next(e for e in health(out) if e["code"] == "cost_floor" and e["agent"] == "Grok")
    assert "dev-b: a host reader failed (Grok Build)" in grok_floor["detail"]
    claude_floor = next(
        e for e in health(out) if e["code"] == "cost_floor" and e["agent"] == "Claude"
    )
    assert "token coverage is incomplete on dev-a" in claude_floor["detail"]


def test_catching_up_reader_floors_its_agent_and_names_catching_up_cause():
    from dataclasses import replace

    data = presentation_data("degraded")
    snap = data.host_inventory.by_device["dev-b"]
    data.host_inventory.by_device["dev-b"] = replace(
        snap,
        degraded=("codex",),
        pending_left=(("codex", 3),),
        tokens_by_day={"2026-09-10": day_bucket({"gpt-6-astra": usage(1_000_000)})},
        lifetime_by_family={"codex": {"2026-09-10": usage(1_000_000)}},
    )
    codex = row(data, "codex")
    assert agg._agent_row_cost(codex)[1] is True
    assert "dev-b: a host reader is still catching up (codex)" in codex.floor_causes
    assert not any("a host reader failed" in c for c in codex.floor_causes)
    out = agg.format_retro(data)
    assert "≥$" in out
    codex_floor = next(
        e for e in health(out) if e["code"] == "cost_floor" and e["agent"] == "Codex"
    )
    assert "dev-b: a host reader is still catching up (codex)" in codex_floor["detail"]
    assert "a host reader failed" not in codex_floor["detail"]


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
    data.fleet.devices_known_list = [{"device_id": "dev-a"}, {"device_id": "dev-b"}]
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
    # Without proven-old registry evidence, remedies ask to push (or wait for
    # Syncthing delivery on missing snapshots) and never nag to upgrade.
    data = stale_data()
    snap = data.host_inventory.by_device["dev-a"]
    unknown_absent = presentation_data("absent")
    unknown_absent.fleet.devices_known_list = [{"device_id": "dev-a"}, {"device_id": "dev-b"}]
    unknown_remedies = [
        agg._host_detail_phrase("absent", None, device="dev-a", fleet=data.fleet),
        *agg._agent_coverage_notes(unknown_absent),
        *agg._host_reader_coverage_notes([snap], fleet=data.fleet),
    ]
    assert len(unknown_remedies) >= 3
    for remedy in unknown_remedies:
        assert "run `mm push` on the machine" in remedy
        assert "upgrade mind-meld" not in remedy
        assert "Mac" not in remedy
    assert any(
        "dev-a, dev-b" in r
        and "run `mm push` on the machine, or wait for Syncthing to deliver" in r
        for r in agg._agent_coverage_notes(unknown_absent)
    )

    # Proven-old registry evidence (< v0.14.17) triggers the upgrade remedy.
    old_fleet = agg.FleetState(
        devices_known=2,
        devices_in_events={"dev-a", "dev-b"},
        devices_known_list=[
            {"device_id": "dev-a", "device_name": "mac-a", "last_seen_version": "0.14.16"},
            {"device_id": "dev-b", "device_name": "arch-b", "last_seen_version": "v0.14.16"},
        ],
    )
    old_absent = presentation_data("absent")
    old_absent.fleet = old_fleet
    old_remedies = [
        agg._host_detail_phrase("absent", None, device="dev-a", fleet=old_fleet),
        *agg._agent_coverage_notes(old_absent),
        *agg._host_reader_coverage_notes([snap], fleet=old_fleet),
    ]
    for remedy in old_remedies:
        assert f"upgrade mind-meld to {agg.ATTENDED_USAGE_MIN_VERSION}+ on that machine" in remedy
        assert "verify with `mm --version`, then run `mm push`" in remedy
        assert "Mac" not in remedy

    # Version parsing matrix: below/at/above floor, v-prefix, missing/null/malformed/
    # non-string/overlong, and conflicting duplicate registry records.
    matrix_fleet = agg.FleetState(
        devices_known=9,
        devices_known_list=[
            {"device_id": "old-bare", "last_seen_version": "0.14.16"},
            {"device_id": "old-v", "last_seen_version": "v0.14.16"},
            {"device_id": "at-floor", "last_seen_version": "0.14.17"},
            {"device_id": "at-floor-v", "last_seen_version": "v0.14.17"},
            {"device_id": "above", "last_seen_version": "1.2.0"},
            {"device_id": "missing"},
            {"device_id": "null", "last_seen_version": None},
            {"device_id": "malformed", "last_seen_version": "not-a-version"},
            {"device_id": "non-str", "last_seen_version": 1416},
            {"device_id": "overlong", "last_seen_version": "0." + ("1" * 300)},
            {"device_id": "conflict", "last_seen_version": "0.14.16"},
            {"device_id": "conflict", "last_seen_version": "0.14.17"},
        ],
    )
    versions = agg._device_versions(matrix_fleet)
    assert agg._is_peer_below_attended_floor("old-bare", versions) is True
    assert agg._is_peer_below_attended_floor("old-v", versions) is True
    for current_or_unknown in (
        "at-floor",
        "at-floor-v",
        "above",
        "missing",
        "null",
        "malformed",
        "non-str",
        "overlong",
        "conflict",
        "unregistered",
    ):
        assert agg._is_peer_below_attended_floor(current_or_unknown, versions) is False, (
            current_or_unknown
        )

    # Mixed group: only the proven-old subset receives upgrade advice.
    mixed_notes = agg._agent_coverage_notes(presentation_data("absent"))
    assert len(mixed_notes) == 1
    assert "dev-a, dev-b" in mixed_notes[0]
    assert (
        "for dev-b, run `mm push` on the machine, or wait for Syncthing to deliver"
        in mixed_notes[0]
    )
    assert (
        f"for dev-a (last reported push below {agg.ATTENDED_USAGE_MIN_VERSION}), "
        f"upgrade mind-meld to {agg.ATTENDED_USAGE_MIN_VERSION}+ on that machine" in mixed_notes[0]
    )
    assert "Mac" not in mixed_notes[0]


# Value: protects=historical session formats do not prove a peer currently needs upgrading;
# fails_when=session or token health bypasses evidence or upgrades a current/unknown peer;
# why_new=existing first-pass tests assert coverage details but not these remedies; seam=none;
# no_claim=synthetic session and registry evidence, not live peer binary state.
@pytest.mark.parametrize("case", ["unknown", "current", "old", "mixed"])
def test_session_and_token_health_remedies_require_peer_version_evidence(case):
    data = presentation_data()
    data.sessions.pre_v2_peers = {"dev-a", "dev-b"}
    data.sessions.pre_token_peers = {"dev-a", "dev-b"}
    records = [
        {"device_id": "dev-a", "device_name": "machine-a"},
        {"device_id": "dev-b", "device_name": "machine-b"},
    ]
    if case != "unknown":
        records[0]["last_seen_version"] = "1.2.0" if case == "current" else "0.14.16"
        records[1]["last_seen_version"] = "0.14.16" if case == "old" else "1.2.0"
    data.fleet.devices_known_list = records
    entries = {e["code"]: e for e in health(agg.format_retro(data))}
    assert "published pre-v0.11.0" in entries["sessions_incomplete"]["detail"]
    assert "omitted rather than double-counted" in entries["sessions_incomplete"]["detail"]
    assert "machine-a, machine-b" in entries["tokens_incomplete"]["detail"]
    for code in ("sessions_incomplete", "tokens_incomplete"):
        remedy = entries[code]["remedy"]
        assert "Mac" not in remedy
        if case in {"old", "mixed"}:
            assert "last reported push" in remedy
            assert "upgrade mind-meld to v0.14.17+" in remedy
            assert "verify with `mm --version`, then run `mm push`" in remedy
        else:
            assert "upgrade" not in remedy
            assert "run `mm push` on the machine" in remedy
        if case == "mixed":
            assert "for machine-b, run `mm push` on the machine;" in remedy
            assert "for machine-a (last reported push below v0.14.17), upgrade" in remedy
    assert "run `mm diag` if it persists" in entries["tokens_incomplete"]["remedy"]


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


def test_long_context_cause_distinguishes_grok_47_from_grok_46_build():
    bucket = {"input": 100_000, "cache_create": 0, "cache_read": 0, "output": 0}
    grok47_cause = agg._long_context_cause({"grok-4.7-build": dict(bucket)}, incomplete=False)
    assert grok47_cause is not None
    assert "Per-turn totals cannot recover the per-request context tier" in grok47_cause
    assert "Grok's logs do not record" not in grok47_cause

    build_cause = agg._long_context_cause({"grok-4.6-build": dict(bucket)}, incomplete=False)
    assert build_cause is not None
    assert "Grok's logs do not record per-request prompt sizes" in build_cause
    assert "Per-turn totals cannot recover" not in build_cause


def test_long_context_cause_lead_follows_only_models_with_a_detail():
    bucket = {"input": 100_000, "cache_create": 0, "cache_read": 0, "output": 0}
    zero = {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}
    cause = agg._long_context_cause(
        {"grok-4.6-build": dict(bucket), "grok-4.7-build": dict(zero)}, incomplete=False
    )
    assert cause is not None
    assert cause.startswith("Grok's logs do not record per-request prompt sizes")
    assert "grok-4.7-build" not in cause


def test_retro_fleet_renders_legacy_cursor_rows_unchanged():
    since = datetime(2026, 9, 21, tzinfo=timezone.utc)
    until = datetime(2026, 9, 23, 23, tzinfo=timezone.utc)
    day = "2026-09-23"
    counters = usage(input=13_345_729, cache_read=12_505_856, output=40_186)
    rows = [
        events.make_host_usage_snapshot(
            device="dev-a",
            ts=until,
            hosts={"grok": {day: counters}},
            tokens_by_day={day: day_bucket({"grok-4.7": counters})},
            token_sources=("cursor",),
            usage_days={"cursor": [day]},
        ),
        events.make_host_usage_snapshot(
            device="dev-b",
            ts=until,
            hosts={"codex": {day: usage(50)}},
            tokens_by_day={day: day_bucket({"gpt-6-astra": usage(50)})},
            token_sources=("codex",),
            usage_days={"codex": [day]},
        ),
    ]
    # Current writers filter retired readers from coverage fields; retained rows do not.
    rows[1]["degraded_sources"] = ["cursor"]
    before = copy.deepcopy(rows)
    ids = frozenset({"dev-a", "dev-b"})
    data = agg.RetroData(window_days=7, since=since, until=until)
    data.fleet = agg.FleetState(
        devices_known=2,
        devices_in_events=set(ids),
        devices_known_list=[{"device_id": d} for d in sorted(ids)],
    )
    data.host_inventory = agg.aggregate_host_usage(
        rows, since=since, until=until, registered_ids=ids
    )
    assert set(data.host_inventory.by_device) == ids
    assert data.host_inventory.by_device["dev-a"].consulted == ("cursor",)
    assert data.host_inventory.by_device["dev-b"].degraded == ("cursor",)
    grok = row(data, "grok")
    assert grok.tokens == 25_891_771
    total, is_floor = agg._agent_row_cost(grok)
    assert is_floor is True
    expected = (13_345_729 * 2 + 12_505_856 * 0.5 + 40_186 * 6) / 1_000_000
    assert abs(total - expected) < 1e-9
    assert agg._reader_display_labels(("cursor",)) == (agg._UNKNOWN_READER_LABEL,)
    out = agg.format_retro(data)
    assert "≥" in out
    assert "grok-4.7" in out
    assert "per-request context tier" in out
    assert "unknown/retired reader" in out
    assert "Cursor (Conductor + enrolled CLI)" not in out
    assert entry(out, "cost_floor")
    assert rows == before


def _single_grok_retro_111b(models):
    since = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    until = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    day = "2026-09-10"
    data = agg.RetroData(window_days=7, since=since, until=until)
    ids = {"dev-a"}
    data.fleet = agg.FleetState(
        devices_known=1,
        devices_in_events=ids,
        devices_known_list=[{"device_id": "dev-a"}],
    )
    total = usage()
    for counters in models.values():
        tu.merge_usage_bucket(total, counters)
    event = dict(
        v=2,
        type="host-usage-snapshot",
        device="dev-a",
        ts=until.isoformat(),
        hosts={"grok": {day: total}},
        token_sources=["grok"],
        active_days=[day],
        counter_semantics="disjoint-v1",
        tokens_by_day={day: day_bucket(models)},
    )
    data.host_inventory = agg.aggregate_host_usage(
        [event], since=since, until=until, registered_ids=ids
    )
    return data


def test_pricing_cache_hermetic_path_111b(tmp_path):
    import os

    assert tu.PRICING_CACHE_PATH.is_relative_to(tmp_path)
    assert os.environ["MM_PRICING_CACHE_PATH"] == str(tu.PRICING_CACHE_PATH)


def test_grok_override_rendered_floor_and_cause_111b():
    tu.update_pricing_cache({"grok-4.7": (3.0, 0.75, 0.0, 9.0)})
    data = _single_grok_retro_111b({"grok-4.7-build": usage(input=1_000_000, output=100_000)})
    grok = row(data, "grok")
    total, is_floor = agg._agent_row_cost(grok)
    assert is_floor is True
    assert abs(total - 3.90) < 1e-9
    assert any(
        "grok-4.7-build" in c
        and "Per-turn totals cannot recover the per-request context tier" in c
        and "at most" not in c
        for c in grok.floor_causes
    )
    out = agg.format_retro(data)
    assert "≥" in out
    grok_floor = next(e for e in health(out) if e["code"] == "cost_floor" and e["agent"] == "Grok")
    assert "grok-4.7-build" in grok_floor["detail"]
    assert "at most" not in grok_floor["detail"]


def test_grok_override_equal_to_bundled_base_keeps_at_most_111b():
    tu.update_pricing_cache({"grok-4.7": (2.0, 0.5, 0.0, 6.0)})
    data = _single_grok_retro_111b({"grok-4.7-build": usage(input=1_000_000, output=100_000)})
    grok = row(data, "grok")
    total, is_floor = agg._agent_row_cost(grok)
    assert is_floor is True
    assert abs(total - 2.60) < 1e-9
    assert any(
        "grok-4.7-build" in c and "at most $5.20 at the long-context tier" in c
        for c in grok.floor_causes
    )


def test_grok_46_override_reaches_build_with_grok_build_lead_111b():
    tu.update_pricing_cache({"grok-4.6": (3.0, 0.75, 0.0, 9.0)})
    data = _single_grok_retro_111b({"grok-4.6-build": usage(input=1_000_000)})
    grok = row(data, "grok")
    total, is_floor = agg._agent_row_cost(grok)
    assert is_floor is True
    assert abs(total - 3.00) < 1e-9
    assert any(
        "Grok's logs do not record per-request prompt sizes" in c
        and "grok-4.6-build" in c
        and "at most" not in c
        for c in grok.floor_causes
    )


def test_long_context_cause_lead_under_local_pricing_override_111b():
    tu.update_pricing_cache({"grok-4.7": (5.0, 1.25, 0.0, 15.0)})
    b = usage(input=100_000)
    cause = agg._long_context_cause(
        {"grok-4.6-build": dict(b), "grok-4.7-build": dict(b)}, incomplete=False
    )
    assert cause is not None
    assert cause.startswith("Per-turn totals cannot recover")
    assert "grok-4.6-build" in cause
    assert "grok-4.7-build" in cause


def test_retained_grok_47_fast_tokens_count_but_are_unpriced():
    build_usage = usage(input=1_000_000, output=100_000)
    fast_usage = usage(input=500_000, output=50_000)
    data = _single_grok_retro_111b({"grok-4.7-build": build_usage, "grok-4.7-fast": fast_usage})
    grok = row(data, "grok")
    assert grok.tokens == 1_650_000
    total, is_floor = agg._agent_row_cost(grok)
    assert is_floor is True
    assert abs(total - 2.60) < 1e-9
    causes = agg.agent_row_floor_causes(grok)
    assert any("grok-4.7-fast" in c and "unpriced" in c for c in causes)
    assert not any("grok-4.7-fast" in c and "at most" in c for c in grok.floor_causes)


@pytest.mark.parametrize("bundled_equal_override", [False, True])
def test_complete_grok_row_with_cache_writes_keeps_detail_without_ceiling(bundled_equal_override):
    if bundled_equal_override:
        tu.update_pricing_cache({"grok-4.7": (2.0, 0.5, 0.0, 6.0)})
    with_writes = usage(input=1_000_000, cache_create=10_000, output=100_000)
    data = _single_grok_retro_111b({"grok-4.7-build": with_writes})
    snap = data.host_inventory.by_device["dev-a"]
    assert snap.partial == () and snap.degraded == ()
    grok = row(data, "grok")
    cause = agg._long_context_cause(grok.by_model, incomplete=False)
    assert cause is not None
    assert "`grok-4.7-build`: $2.60 at the base tier" in cause
    assert "at most" not in cause
    out = agg.format_retro(data)
    grok_floor = next(e for e in health(out) if e["code"] == "cost_floor" and e["agent"] == "Grok")
    assert "grok-4.7-build" in grok_floor["detail"]
    assert "at most" not in grok_floor["detail"]

    without_writes = usage(input=1_000_000, cache_create=0, output=100_000)
    control = agg._long_context_cause({"grok-4.7-build": without_writes}, incomplete=False)
    assert control is not None and "at most" in control
