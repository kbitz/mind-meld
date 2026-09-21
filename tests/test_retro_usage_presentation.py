"""One synthetic corpus for Track 58A's before/after and acceptance views."""

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


def test_models_total_reconciles_with_four_field_body():
    data = presentation_data()
    total = sum(n for _, n in agg._aggregate_model_families(data.sessions.tokens_by_model))
    counts = [
        data.sessions.tokens_input,
        data.sessions.tokens_cache_create,
        data.sessions.tokens_cache_read,
        data.sessions.tokens_output,
    ]
    assert total == sum(counts) == 11_300_000
    body = agg.format_retro(data, name="Example")
    assert "Claude: 11.3M tokens" in body
    assert "| All models | 1.1M | 3.0M | 7.0M | 200.0k |" in body
    assert data.sessions.token_devices == {"dev-a": data.until, "dev-b": data.until}


def test_floor_basis_and_row_sum_for_both_sections():
    data = presentation_data("degraded")
    total, rows = agg._section_costs(data.sessions.tokens_by_model, floor=True)
    assert total == sum(rows.values())
    assert total <= agg._section_costs(data.sessions.tokens_by_model, floor=False)[0]
    out = agg.format_retro(data)
    claude = out.split("## Claude Code activity")[1].split("## Skills")[0]
    assert "| >=$8.60 |" in claude and "| >=$19.50 |" in claude and "| >=$28.10 |" in claude
    assert "~$" not in claude
    host = out.split("## API list-rate equivalent (per machine)")[1].split("## mm sync")[0]
    assert "| dev-b | >=$11.25 |" in host  # floor writes, not the $15 estimate
    assert "| grok-unknown | — |" in host
    assert "~$" not in host and "$4.00" not in host
    assert "at most $4.00" in out.split("## Notes")[1]
    assert "does not sum to the row" in host


def test_one_machine_floor_changes_other_machine_cards_without_claiming_failure():
    out = agg.format_retro(presentation_data())
    assert "| dev-b | >=$11.25 |" in out
    notes = out.split("## Notes")[1]
    assert "floor rates throughout the per-machine section" in notes
    assert "equivalent for `dev-b` is a floor" not in notes
    assert "a host reader failed" not in notes


def test_claude_coverage_floor_does_not_floor_priced_host_section():
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
    out = agg.format_retro(data)
    claude = out.split("## Claude Code activity")[1].split("## Skills")[0]
    host = out.split("## API list-rate equivalent (per machine)")[1].split("## mm sync")[0]
    assert ">=$" in claude and "~$" not in claude
    assert "| dev-b | ~$" in host


def test_unpriced_claude_row_is_unavailable_and_total_is_floor():
    data = presentation_data()
    data.sessions.tokens_by_model["unknown-model"] = usage(20)
    data.sessions.tokens_input += 20
    out = agg.format_retro(data)
    section = out.split("## Claude Code activity")[1].split("## Skills")[0]
    assert "| unknown-model | 20 | 0 | 0 | 0 | — |" in section
    assert "~$" not in section
    assert ">=$28.10" in section


def test_model_tables_and_extrapolation_notes_are_bounded_and_safe():
    data = presentation_data()
    models = {f"claude-opus-{i:03d}": usage(i + 1) for i in range(200)}
    hostile = "claude-opus-999\x1b[31m|`\n" + "x" * 200
    models[hostile] = usage(1_000_000)
    data.sessions.tokens_by_model = models
    data.sessions.tokens_input = sum(tu.sum_bucket(u) for u in models.values())
    out = agg.format_retro(data)
    section = out.split("## Claude Code activity")[1].split("## Skills")[0]
    assert "(+196 more)" in section
    assert len([line for line in section.splitlines() if line.startswith("| ")]) == 8
    notes = " ".join(agg._extrapolation_notes(models, scope="Claude Code"))
    assert "Models priced by family extrapolation" in notes
    assert "The ~ or >= marker may include this assumption." in notes
    assert "(+193 more)" in notes
    assert hostile not in out and "\x1b" not in out
    assert "(+" not in " ".join(
        agg._extrapolation_notes({m: usage(1) for m in tu.VERIFIED_MODEL_IDS}, scope="Claude Code")
    )
    snap = data.host_inventory.by_device["dev-b"]
    snap.tokens_by_day = {"2026-09-10": day_bucket(models)}
    _, costs = agg._section_costs(models, floor=True)
    rows = agg._per_model_cost_summary(snap.device, models, costs, floor=True)
    blob = "\n".join(rows)
    assert len(rows) == 6 and "(+196 more)" in rows[-1]
    assert all(len(row) <= 80 for row in rows)
    assert hostile not in blob and "\x1b" not in blob


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


def test_stale_on_window_start_day_is_unavailable_everywhere_but_retained():
    data = stale_data()
    view = agg._agent_rhythm_view(
        data.host_inventory, since=data.since, until=data.until, machines_known=2
    )
    assert view.machines_with_activity == 0 and not view.any_activity
    out = agg.format_retro(data, name="Example")
    assert "AGENT LOGS (0 of 2 machines with agent activity)" in out
    assert "| dev-a | Codex | 2026-09-07 | stale | 1.0M | — |" in out
    assert "| dev-a | — |" in out
    assert "Agent-log snapshots all predate this window" in out
    snap = data.host_inventory.by_device["dev-a"]
    assert agg._windowed_host_by_model(snap, "2026-09-07", "2026-09-14") == ({}, False)
    assert agg.window_bounds(snap, "2026-09-07", "2026-09-14") is None


def test_stale_empty_family_is_not_zero():
    out = agg.format_retro(stale_data(empty=True))
    assert "| dev-a | — | 2026-09-07 | stale | 0 | — |" in out


def test_snapshot_exactly_at_since_keeps_first_day():
    data = stale_data(exactly_at_since=True)
    view = agg._agent_rhythm_view(
        data.host_inventory, since=data.since, until=data.until, machines_known=2
    )
    assert view.machines_with_activity == 1 and view.any_activity
    out = agg.format_retro(data)
    assert "| dev-a | Codex | 2026-09-07 | current | 1.0M | 1.0M |" in out
    assert "| dev-a | ~$10.00 |" in out
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


def test_empty_device_label_keeps_unnamed_parens():
    from dataclasses import replace

    data = presentation_data()
    snap = data.host_inventory.by_device["dev-a"]
    data.host_inventory.by_device = {"": replace(snap, device="")}
    out = agg.format_retro(data)
    assert "(unnamed)" in out
    assert "(unnamed |" not in out
    assert out.count("| (unnamed) |") >= 2


def test_fleet_refresh_remedies_require_the_attended_producer_version():
    data = stale_data()
    snap = data.host_inventory.by_device["dev-a"]
    _, economics_notes, _, _ = agg._device_economics_cell(
        snap, data.since.date().isoformat(), data.until.date().isoformat()
    )
    remedies = [
        agg._host_detail_phrase("absent", None),
        *economics_notes,
        *agg._agent_coverage_notes(presentation_data("absent")),
        *agg._host_reader_coverage_notes([snap]),
    ]
    assert len(remedies) >= 4
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
                assert all(len(line) <= 80 for line in out.splitlines() if line.startswith("|"))
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


def test_section_floor_does_not_let_zero_rows_evict_positive_rows():
    from dataclasses import replace

    data = presentation_data()
    positive = data.host_inventory.by_device["dev-a"]
    data.host_inventory.by_device = {"zzz": replace(positive, device="zzz")}
    for i in range(agg.MAX_AGENT_INVENTORY_MACHINES):
        device = f"aaa-{i}"
        data.host_inventory.by_device[device] = replace(
            positive,
            device=device,
            lifetime_by_family={},
            tokens_by_day={},
        )
    lines, _ = agg._render_host_economics(data)
    assert "| zzz | >=$20.25 |" in lines


def test_floor_trigger_keeps_its_cause_when_outside_machine_display_cap():
    from dataclasses import replace

    data = presentation_data()
    positive = data.host_inventory.by_device["dev-b"]
    data.host_inventory.by_device = {
        f"aaa-{i}": replace(positive, device=f"aaa-{i}")
        for i in range(agg.MAX_AGENT_INVENTORY_MACHINES)
    }
    data.host_inventory.by_device["zzz"] = replace(
        positive,
        device="zzz",
        lifetime_by_family={},
        tokens_by_day={},
        degraded=("grok",),
    )
    lines, notes = agg._render_host_economics(data)
    assert "| zzz | >=$0.00 |" not in lines
    assert "| aaa-0 | >=$11.25 |" in lines
    assert any("for `zzz` is a floor" in note and "a host reader failed" in note for note in notes)
