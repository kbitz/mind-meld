"""Tests for `mm devices approve <device-id>` (Track 119A)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import config, core, syncthing
from mind_meld.cli import app
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend

runner = CliRunner()


def _valid_syncthing_id(seed_char: str) -> str:
    chunk13 = seed_char * 13
    group14 = chunk13 + syncthing._luhn32(chunk13)
    full56 = group14 * 4
    return "-".join(full56[i : i + 7] for i in range(0, 56, 7))


def _setup_managed_fleet(fake_syncthing, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    my_id = _valid_syncthing_id("A")
    peer_id = _valid_syncthing_id("B")
    joining_id = _valid_syncthing_id("C")
    fake_syncthing.my_id = my_id
    fake_syncthing.peer_id = peer_id
    fake_syncthing.write_config()
    fake_syncthing.set_folder(
        tmp_path / "sync",
        peers=[(peer_id, "mac-b", True)],
    )
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {}
    config.save_syncthing_record(my_id)
    fake_syncthing.requests.clear()
    fake_syncthing.bodies.clear()
    return my_id, peer_id, joining_id


def test_approve_pending_device_adds_marks_introducer_and_shares_folder(
    fake_syncthing, tmp_path, monkeypatch
):
    my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "peer-host\x1b[31m", "address": "192.0.2.9:22000"}
    }

    res = runner.invoke(app, ["devices", "approve", joining_id])

    assert res.exit_code == 0, res.output
    assert res.output == (
        f"Approve: approved {joining_id}: Syncthing here shares the fleet folder with it "
        "and marks it as introducer. On the new machine, Run: mm init.\n"
    )
    assert fake_syncthing.bodies == [
        (
            "POST",
            "/rest/config/devices",
            {
                "deviceID": joining_id,
                "name": "",
                "addresses": ["dynamic"],
                "introducer": False,
                "autoAcceptFolders": False,
                "paused": False,
            },
        ),
        ("PATCH", f"/rest/config/devices/{joining_id}", {"introducer": True}),
        (
            "PATCH",
            "/rest/config/folders/mind-meld",
            {
                "devices": [
                    {"deviceID": my_id},
                    {"deviceID": peer_id},
                    {"deviceID": joining_id},
                ]
            },
        ),
    ]


@pytest.mark.parametrize("transform", ["lower", "dashless_lower"])
def test_approve_accepts_lowercase_and_dashless_pending_id(
    fake_syncthing, tmp_path, monkeypatch, transform
):
    my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box", "address": "192.0.2.9:22000"}
    }
    pasted = joining_id.lower() if transform == "lower" else joining_id.replace("-", "").lower()

    res = runner.invoke(app, ["devices", "approve", pasted])

    assert res.exit_code == 0, res.output
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [
        ("POST", "/rest/config/devices"),
        ("PATCH", f"/rest/config/devices/{joining_id}"),
        ("PATCH", "/rest/config/folders/mind-meld"),
    ]
    assert fake_syncthing.bodies[0][2]["deviceID"] == joining_id
    assert fake_syncthing.bodies[2][2]["devices"] == [
        {"deviceID": my_id},
        {"deviceID": peer_id},
        {"deviceID": joining_id},
    ]


def test_approve_repeat_after_success_and_peer_introduced_device_report_already_approved(
    fake_syncthing, tmp_path, monkeypatch
):
    _my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }

    first = runner.invoke(app, ["devices", "approve", joining_id])
    assert first.exit_code == 0, first.output
    fake_syncthing.bodies.clear()

    second = runner.invoke(app, ["devices", "approve", joining_id])
    assert second.exit_code == 0, second.output
    assert second.output == (
        f"Approve: {joining_id} is already approved: Syncthing on this machine already "
        "shares the fleet folder with it.\n"
    )
    assert fake_syncthing.bodies == []

    # peer_id is configured and shared in set_shape() with introducer=False
    # (as when another fleet machine introduced it).
    third = runner.invoke(app, ["devices", "approve", peer_id])
    assert third.exit_code == 0, third.output
    assert f"Approve: {peer_id} is already approved" in third.output
    assert fake_syncthing.bodies == []


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-device-id",
        "AAAAAAA-AAAAAAA",
        "AAAAAAA",
    ],
)
def test_approve_refuses_malformed_id_with_no_syncthing_requests(
    fake_syncthing, tmp_path, monkeypatch, bad_id
):
    _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)

    res = runner.invoke(app, ["devices", "approve", bad_id])

    assert res.exit_code == 1
    text = " ".join(res.output.split())
    assert "is not a Syncthing device ID" in text
    assert "Nothing was changed." in text
    assert "mm devices approve <device-id>" in text
    assert fake_syncthing.requests == []
    assert fake_syncthing.bodies == []


def test_approve_refuses_near_match_bad_check_digit_and_near_match_valid_check_digits(
    fake_syncthing, tmp_path, monkeypatch
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }

    # 1. One character flipped -> bad check digit, zero Syncthing requests.
    bad_check = ("D" if joining_id[0] != "D" else "E") + joining_id[1:]
    res_bad = runner.invoke(app, ["devices", "approve", bad_check])
    assert res_bad.exit_code == 1
    text_bad = " ".join(res_bad.output.split())
    assert "check digit mismatch" in text_bad
    assert "Nothing was changed." in text_bad
    assert fake_syncthing.requests == []
    assert fake_syncthing.bodies == []

    # 2. One data character changed in group 1 and its check character recomputed
    # -> valid Syncthing ID that is a near-match to joining_id, refused because
    # it is not an exact match in pending_devices.
    raw56 = joining_id.replace("-", "")
    alt_chunk0 = ("D" if raw56[0] != "D" else "E") + raw56[1:13]
    alt_group0 = alt_chunk0 + syncthing._luhn32(alt_chunk0)
    alt56 = alt_group0 + raw56[14:]
    near_valid = "-".join(alt56[i : i + 7] for i in range(0, 56, 7))
    assert syncthing.is_device_id(near_valid)
    assert near_valid != joining_id

    res_near = runner.invoke(app, ["devices", "approve", near_valid])
    assert res_near.exit_code == 1
    text_near = " ".join(res_near.output.split())
    assert f"{near_valid} is not among Syncthing's pending devices" in text_near
    assert "Nothing was changed." in text_near
    assert joining_id not in res_near.output
    assert "joining-box" not in res_near.output
    assert fake_syncthing.bodies == []


def test_approve_refuses_non_pending_id_and_never_prints_other_pending_devices(
    fake_syncthing, tmp_path, monkeypatch
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    other_pending = _valid_syncthing_id("D")
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        other_pending: {"name": "stranger-machine", "address": "198.51.100.4:22000"}
    }

    res = runner.invoke(app, ["devices", "approve", joining_id])

    assert res.exit_code == 1
    text = " ".join(res.output.split())
    assert f"{joining_id} is not among Syncthing's pending devices" in text
    assert "Nothing was changed." in text
    assert other_pending not in res.output
    assert "stranger-machine" not in res.output
    assert fake_syncthing.bodies == []


def test_approve_resumes_after_syncthing_stops_between_add_and_share(
    fake_syncthing, tmp_path, monkeypatch
):
    my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    fake_syncthing.stop_after("POST", "/rest/config/devices")

    first = runner.invoke(app, ["devices", "approve", joining_id])

    assert first.exit_code == 1
    text1 = " ".join(first.output.split())
    assert (
        f"devices approve: Syncthing added {joining_id} but did not confirm marking it as "
        "introducer or sharing the fleet folder with it (unreachable/"
    ) in text1
    assert f"Start Syncthing, then run 'mm devices approve {joining_id}' again to finish." in text1
    assert text1.endswith(f"Run: {core._syncthing_start_command()}")
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [("POST", "/rest/config/devices")]
    assert joining_id not in fake_syncthing.responses["/rest/cluster/pending/devices"]

    # Restart Syncthing on the same port and state; the rerun finishes introducer + share.
    fake_syncthing.start()
    fake_syncthing.bodies.clear()
    writer = syncthing.SyncthingWriter(syncthing.ManagedRecord(device_id=my_id))
    mid_state = writer.approval_state(config.SYNCTHING_FOLDER_ID, joining_id)
    assert mid_state.configured is True
    assert mid_state.writer_shape is True
    assert mid_state.introducer is False
    assert mid_state.shared is False

    second = runner.invoke(app, ["devices", "approve", joining_id])

    assert second.exit_code == 0, second.output
    assert second.output == (
        f"Approve: finished approving {joining_id}: Syncthing here now marks it as "
        "introducer and shares the fleet folder with it. On the new machine, Run: mm init.\n"
    )
    assert fake_syncthing.bodies == [
        ("PATCH", f"/rest/config/devices/{joining_id}", {"introducer": True}),
        (
            "PATCH",
            "/rest/config/folders/mind-meld",
            {
                "devices": [
                    {"deviceID": my_id},
                    {"deviceID": peer_id},
                    {"deviceID": joining_id},
                ]
            },
        ),
    ]

    fake_syncthing.bodies.clear()
    third = runner.invoke(app, ["devices", "approve", joining_id])
    assert third.exit_code == 0, third.output
    assert "already approved" in third.output
    assert fake_syncthing.bodies == []


def test_approve_resumes_after_syncthing_stops_between_introducer_and_share(
    fake_syncthing, tmp_path, monkeypatch
):
    my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    fake_syncthing.stop_after("PATCH", f"/rest/config/devices/{joining_id}")

    first = runner.invoke(app, ["devices", "approve", joining_id])

    assert first.exit_code == 1
    text1 = " ".join(first.output.split())
    assert (
        f"devices approve: Syncthing added {joining_id} and marked it as introducer, "
        "but did not confirm sharing the fleet folder with it (unreachable/"
    ) in text1
    assert text1.endswith(f"Run: {core._syncthing_start_command()}")

    fake_syncthing.start()
    fake_syncthing.bodies.clear()

    second = runner.invoke(app, ["devices", "approve", joining_id])

    assert second.exit_code == 0, second.output
    assert second.output == (
        f"Approve: finished approving {joining_id}: Syncthing here now shares the "
        "fleet folder with it (it was already marked as introducer). "
        "On the new machine, Run: mm init.\n"
    )
    assert fake_syncthing.bodies == [
        (
            "PATCH",
            "/rest/config/folders/mind-meld",
            {
                "devices": [
                    {"deviceID": my_id},
                    {"deviceID": peer_id},
                    {"deviceID": joining_id},
                ]
            },
        )
    ]


def test_approve_share_http_500_reports_mm_diag_and_rerun_finishes_share_only(
    fake_syncthing, tmp_path, monkeypatch
):
    my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    fake_syncthing.write_responses[("PATCH", "/rest/config/folders/mind-meld")] = 500

    first = runner.invoke(app, ["devices", "approve", joining_id])

    assert first.exit_code == 1
    text1 = " ".join(first.output.split())
    assert (
        f"devices approve: Syncthing added {joining_id} and marked it as introducer, "
        "but did not confirm sharing the fleet folder with it (error/http-500)."
    ) in text1
    assert (
        f"Once Syncthing accepts the change, run 'mm devices approve {joining_id}' "
        "again to finish. Run: mm diag"
    ) in text1
    assert text1.endswith("Run: mm diag")

    del fake_syncthing.write_responses[("PATCH", "/rest/config/folders/mind-meld")]
    fake_syncthing.bodies.clear()

    second = runner.invoke(app, ["devices", "approve", joining_id])

    assert second.exit_code == 0, second.output
    assert second.output == (
        f"Approve: finished approving {joining_id}: Syncthing here now shares the "
        "fleet folder with it (it was already marked as introducer). "
        "On the new machine, Run: mm init.\n"
    )
    assert fake_syncthing.bodies == [
        (
            "PATCH",
            "/rest/config/folders/mind-meld",
            {
                "devices": [
                    {"deviceID": my_id},
                    {"deviceID": peer_id},
                    {"deviceID": joining_id},
                ]
            },
        )
    ]


def test_approve_add_response_lost_after_apply_reports_unconfirmed_and_rerun_finishes(
    fake_syncthing, tmp_path, monkeypatch
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    real_add = syncthing.SyncthingWriter.add_device

    def add_then_lose_response(self, device_id, name=""):
        # Syncthing applies the POST, then the response never arrives.
        assert real_add(self, device_id, name) == syncthing.WriteResult("ok", None)
        return syncthing.WriteResult("unreachable", "timeout")

    with monkeypatch.context() as lost:
        lost.setattr(syncthing.SyncthingWriter, "add_device", add_then_lose_response)
        first = runner.invoke(app, ["devices", "approve", joining_id])

    assert first.exit_code == 1
    text1 = " ".join(first.output.split())
    assert (
        f"devices approve: Syncthing did not confirm adding {joining_id} "
        "(unreachable/timeout), so it may or may not have added it."
    ) in text1
    assert "Nothing was changed" not in text1
    assert f"Start Syncthing, then run 'mm devices approve {joining_id}' again to finish." in text1
    assert text1.endswith(f"Run: {core._syncthing_start_command()}")
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [("POST", "/rest/config/devices")]

    fake_syncthing.bodies.clear()
    second = runner.invoke(app, ["devices", "approve", joining_id])

    assert second.exit_code == 0, second.output
    assert second.output.startswith(
        f"Approve: finished approving {joining_id}: Syncthing here now marks it as introducer "
        "and shares the fleet folder"
    )
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [
        ("PATCH", f"/rest/config/devices/{joining_id}"),
        ("PATCH", "/rest/config/folders/mind-meld"),
    ]


def test_approve_add_rejected_by_syncthing_reports_unconfirmed_and_rerun_approves(
    fake_syncthing, tmp_path, monkeypatch
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    fake_syncthing.write_responses[("POST", "/rest/config/devices")] = 500

    first = runner.invoke(app, ["devices", "approve", joining_id])

    assert first.exit_code == 1
    text1 = " ".join(first.output.split())
    assert f"Syncthing did not confirm adding {joining_id} (error/http-500)" in text1
    assert (
        f"Once Syncthing accepts the change, run 'mm devices approve {joining_id}' "
        "again to finish. Run: mm diag"
    ) in text1
    assert text1.endswith("Run: mm diag")
    assert joining_id in fake_syncthing.responses["/rest/cluster/pending/devices"]

    del fake_syncthing.write_responses[("POST", "/rest/config/devices")]
    fake_syncthing.bodies.clear()

    second = runner.invoke(app, ["devices", "approve", joining_id])

    assert second.exit_code == 0, second.output
    assert second.output.startswith(f"Approve: approved {joining_id}:")
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [
        ("POST", "/rest/config/devices"),
        ("PATCH", f"/rest/config/devices/{joining_id}"),
        ("PATCH", "/rest/config/folders/mind-meld"),
    ]


@pytest.mark.parametrize(
    "result,expected",
    [
        (("not-found", None), "Syncthing is not running on this machine"),
        (("refused", "not-managed"), "mm does not manage Syncthing on this machine"),
    ],
)
def test_approve_add_stopped_before_sending_changes_nothing(
    fake_syncthing, tmp_path, monkeypatch, result, expected
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    monkeypatch.setattr(
        syncthing.SyncthingWriter,
        "add_device",
        lambda self, device_id, name="": syncthing.WriteResult(*result),
    )

    res = runner.invoke(app, ["devices", "approve", joining_id])

    assert res.exit_code == 1
    text = " ".join(res.output.split())
    assert expected in text
    assert "did not confirm" not in text
    assert fake_syncthing.bodies == []
    assert joining_id in fake_syncthing.responses["/rest/cluster/pending/devices"]


@pytest.mark.parametrize("preset_introducer", [False, True])
@pytest.mark.parametrize("failing", ["mark_introducer", "share_folder"])
def test_approve_resume_write_failure_reports_unconfirmed_step_and_rerun_finishes(
    fake_syncthing, tmp_path, monkeypatch, preset_introducer, failing
):
    my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    # A half-finished approval: configured in add_device's shape, not pending,
    # not sharing the fleet folder; introducer already set or not.
    fake_syncthing.responses["/rest/config/devices"].append(
        {
            "deviceID": joining_id,
            "name": "",
            "addresses": ["dynamic"],
            "introducer": preset_introducer,
            "autoAcceptFolders": False,
            "paused": False,
        }
    )

    with monkeypatch.context() as broken:
        broken.setattr(
            syncthing.SyncthingWriter,
            failing,
            lambda self, *args: syncthing.WriteResult("error", "http-500"),
        )
        first = runner.invoke(app, ["devices", "approve", joining_id])

    assert first.exit_code == 1
    text1 = " ".join(first.output.split())
    if failing == "mark_introducer":
        assert (
            f"devices approve: Syncthing added {joining_id} but did not confirm marking it as "
            "introducer or sharing the fleet folder with it (error/http-500)."
        ) in text1
    else:
        assert (
            f"devices approve: Syncthing added {joining_id} and marked it as introducer, "
            "but did not confirm sharing the fleet folder with it (error/http-500)."
        ) in text1
    assert f"run 'mm devices approve {joining_id}' again to finish" in text1
    assert text1.endswith("Run: mm diag")
    assert not any(m == "POST" for m, _p, _b in fake_syncthing.bodies)

    fake_syncthing.bodies.clear()
    second = runner.invoke(app, ["devices", "approve", joining_id])

    assert second.exit_code == 0, second.output
    folder_patch = (
        "PATCH",
        "/rest/config/folders/mind-meld",
        {"devices": [{"deviceID": my_id}, {"deviceID": peer_id}, {"deviceID": joining_id}]},
    )
    if preset_introducer or failing == "share_folder":
        assert second.output.startswith(
            f"Approve: finished approving {joining_id}: Syncthing here now shares the "
            "fleet folder with it (it was already marked as introducer)."
        )
        assert fake_syncthing.bodies == [folder_patch]
    else:
        assert second.output.startswith(
            f"Approve: finished approving {joining_id}: Syncthing here now marks it as "
            "introducer and shares the fleet folder with it."
        )
        assert fake_syncthing.bodies == [
            ("PATCH", f"/rest/config/devices/{joining_id}", {"introducer": True}),
            folder_patch,
        ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("paused", True),
        ("autoAcceptFolders", True),
        ("addresses", ["tcp://192.0.2.1:22000"]),
        ("introducedBy", "SOME-OTHER-DEVICE"),
    ],
)
def test_approve_refuses_configured_unshared_device_not_in_writer_shape(
    fake_syncthing, tmp_path, monkeypatch, field, value
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    entry = {
        "deviceID": joining_id,
        "name": "hand-added",
        "addresses": ["dynamic"],
        "introducer": False,
        "autoAcceptFolders": False,
        "paused": False,
        field: value,
    }
    fake_syncthing.responses["/rest/config/devices"].append(entry)

    res = runner.invoke(app, ["devices", "approve", joining_id])

    assert res.exit_code == 1
    text = " ".join(res.output.split())
    assert "was not added by an unfinished 'mm devices approve'" in text
    assert core.SYNCTHING_GUIDE_HEADING in text
    assert "Nothing was changed." in text
    assert fake_syncthing.bodies == []


def test_approve_refuses_own_device_id_without_syncthing_requests(
    fake_syncthing, tmp_path, monkeypatch
):
    my_id, _peer_id, _joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)

    res = runner.invoke(app, ["devices", "approve", my_id])

    assert res.exit_code == 1
    text = " ".join(res.output.split())
    assert "that is this machine's own device ID" in text
    assert "Nothing was changed." in text
    assert fake_syncthing.requests == []
    assert fake_syncthing.bodies == []


def test_approve_refuses_when_no_record_or_record_names_another_device(
    fake_syncthing, tmp_path, monkeypatch
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    config.syncthing_record_path().unlink()

    res_none = runner.invoke(app, ["devices", "approve", joining_id])
    assert res_none.exit_code == 1
    text_none = " ".join(res_none.output.split())
    assert "mm does not manage Syncthing on this machine" in text_none
    assert core.SYNCTHING_GUIDE_HEADING in text_none
    assert "Approving a new machine by hand on a fleet machine" in text_none
    assert fake_syncthing.requests == []
    assert fake_syncthing.bodies == []

    config.save_syncthing_record(_valid_syncthing_id("Z"))
    res_other = runner.invoke(app, ["devices", "approve", joining_id])
    assert res_other.exit_code == 1
    text_other = " ".join(res_other.output.split())
    assert "mm does not manage Syncthing on this machine" in text_other
    assert core.SYNCTHING_GUIDE_HEADING in text_other
    assert [(m, p) for m, p, _ in fake_syncthing.requests] == [("GET", "/rest/system/status")]
    assert fake_syncthing.bodies == []


def test_approve_refuses_malformed_syncthing_record_toml(fake_syncthing, tmp_path, monkeypatch):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    rec_path = config.syncthing_record_path()
    rec_path.write_text("[syncthing\nmanaged_device_id = 'broken'\n", encoding="utf-8")

    res = runner.invoke(app, ["devices", "approve", joining_id])

    assert res.exit_code == 1
    text = " ".join(res.output.split())
    assert "Fix or remove that file" in text
    assert "Nothing was changed." in text
    assert fake_syncthing.requests == []
    assert fake_syncthing.bodies == []


def test_approve_refuses_when_syncthing_is_down_or_config_dir_missing(
    fake_syncthing, tmp_path, monkeypatch
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.close()

    res_down = runner.invoke(app, ["devices", "approve", joining_id])
    assert res_down.exit_code == 1
    text_down = " ".join(res_down.output.split())
    assert "Syncthing is not running on this machine" in text_down
    assert "Nothing was changed." in text_down
    assert text_down.endswith(f"Run: {core._syncthing_start_command()}")
    assert fake_syncthing.bodies == []

    monkeypatch.setenv("MM_SYNCTHING_CONFIG_DIR", str(tmp_path / "missing-syncthing-dir"))
    fake_syncthing.requests.clear()
    res_missing = runner.invoke(app, ["devices", "approve", joining_id])
    assert res_missing.exit_code == 1
    text_missing = " ".join(res_missing.output.split())
    assert "Syncthing is not running on this machine" in text_missing
    assert "Nothing was changed." in text_missing
    assert text_missing.endswith(f"Run: {core._syncthing_start_command()}")
    assert fake_syncthing.requests == []


@pytest.mark.parametrize("status_code", [403, 500])
def test_approve_refuses_on_http_error_reading_config_devices(
    fake_syncthing, tmp_path, monkeypatch, status_code
):
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/config/devices"] = status_code

    res = runner.invoke(app, ["devices", "approve", joining_id])

    assert res.exit_code == 1
    text = " ".join(res.output.split())
    assert "could not read Syncthing's configuration" in text
    assert "Nothing was changed." in text
    assert text.endswith("Run: mm diag")
    assert fake_syncthing.bodies == []


def test_approve_refuses_when_fleet_folder_missing_or_has_bad_option(
    fake_syncthing, tmp_path, monkeypatch
):
    _my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/config/folders"] = []

    res_missing = runner.invoke(app, ["devices", "approve", joining_id])
    assert res_missing.exit_code == 1
    text_missing = " ".join(res_missing.output.split())
    assert "has no fleet folder ('mind-meld') with mm's folder options" in text_missing
    assert "Nothing was changed." in text_missing
    assert text_missing.endswith("Run: mm diag")
    assert fake_syncthing.bodies == []

    fake_syncthing.set_folder(
        tmp_path / "sync",
        folder_type="receiveonly",
        peers=[(peer_id, "mac-b", True)],
    )
    fake_syncthing.bodies.clear()

    res_bad = runner.invoke(app, ["devices", "approve", joining_id])
    assert res_bad.exit_code == 1
    text_bad = " ".join(res_bad.output.split())
    assert "has no fleet folder ('mind-meld') with mm's folder options" in text_bad
    assert "Nothing was changed." in text_bad
    assert text_bad.endswith("Run: mm diag")
    assert fake_syncthing.bodies == []


def test_devices_table_and_json_still_dispatch_through_group(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    backend = LocalBackend(storage)
    register_device(backend, "dev-self0001", "mac-a")
    cfg = {
        "device": {"id": "dev-self0001", "name": "mac-a"},
        "storage": {"backend": "local", "path": str(storage)},
        "sync": {"sources": [], "max_file_size": 1_048_576},
        "crypto": {"argon2_memory_kb": 65536},
    }
    monkeypatch.setattr("mind_meld.core._get_config", lambda **_kw: cfg)
    monkeypatch.setattr("mind_meld.devicesflow.get_backend", lambda _cfg: backend)

    res_table = runner.invoke(app, ["devices"])
    assert res_table.exit_code == 0, res_table.output
    assert "Registered Devices" in res_table.output
    assert "mac-a" in res_table.output

    res_json = runner.invoke(app, ["devices", "--format", "json"])
    assert res_json.exit_code == 0, res_json.output
    parsed = json.loads(res_json.output)
    assert parsed == [
        {
            "device_id": "dev-self0001",
            "device_name": "mac-a",
            "is_self": True,
            "last_seen": None,
            "last_seen_version": None,
        }
    ]


def test_no_push_pull_or_auto_command_ever_accepts_a_device(fake_syncthing, tmp_path, monkeypatch):
    src_root = Path(__file__).resolve().parents[1] / "src" / "mind_meld"
    pairing_methods = {
        "pending_devices",
        "approval_state",
        "add_device",
        "mark_introducer",
        "share_folder",
        "accept_folder_offer",
    }
    raw_writes = {"_send_json", "_WRITE_ROUTES"}
    entry_points = {
        ("devicesflow", "approve"),
        ("initflow", "_managed_join"),
        ("initflow", "init"),
    }
    call_sites: set[tuple[str, str]] = set()
    entry_refs: set[tuple[str, str, str]] = set()
    for path in sorted(src_root.rglob("*.py")):
        rel = str(path.relative_to(src_root))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def scope_of(node, parents=parents):
            scope: list[str] = []
            cur = node
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    scope.append(cur.name)
            return ".".join(reversed(scope)) or "<module>"

        for node in ast.walk(tree):
            # Every reference, not only a direct call, anywhere in the module
            # (class methods and module scope included): an alias such as
            # `m = writer.add_device` or `getattr(writer, "add_device")` counts.
            pairing = (isinstance(node, ast.Attribute) and node.attr in pairing_methods) or (
                isinstance(node, ast.Constant) and node.value in pairing_methods
            )
            raw = (isinstance(node, ast.Name) and node.id in raw_writes) or (
                isinstance(node, ast.Attribute) and node.attr in raw_writes
            )
            if pairing or raw:
                where = scope_of(node)
                # Inside the writer module only SyncthingWriter's own methods may
                # name a pairing operation; its send helpers own the raw routes.
                if rel == "syncthing.py" and (raw or where.split(".")[0] == "SyncthingWriter"):
                    continue
                call_sites.add((rel, where))
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and (node.value.id, node.attr) in entry_points
            ):
                entry_refs.add((rel, scope_of(node), f"{node.value.id}.{node.attr}"))
            elif isinstance(node, ast.Name) and node.id == "_managed_join":
                entry_refs.add((rel, scope_of(node), "initflow._managed_join"))

    assert call_sites == {
        ("devicesflow.py", "approve"),
        ("initflow.py", "_managed_join"),
    }
    # The two pairing paths are reachable only from their own commands.
    assert entry_refs == {
        ("cli.py", "devices_approve", "devicesflow.approve"),
        ("cli.py", "init", "initflow.init"),
        ("initflow.py", "init", "initflow._managed_join"),
    }

    cli_tree = ast.parse((src_root / "cli.py").read_text(encoding="utf-8"))
    sync_funcs = {"push", "pull", "autopull", "autopush", "_push_core", "_auto_command_setup"}
    found = set()
    for top in cli_tree.body:
        if isinstance(top, ast.FunctionDef) and top.name in sync_funcs:
            found.add(top.name)
            for n in ast.walk(top):
                refs = set()
                if isinstance(n, ast.Name):
                    refs.add(n.id)
                elif isinstance(n, ast.ImportFrom):
                    refs.add(n.module or "")
                    refs.update(a.name for a in n.names)
                elif isinstance(n, ast.Import):
                    refs.update(a.name for a in n.names)
                assert not any("devicesflow" in r for r in refs), (
                    f"cli.py:{top.name} references devicesflow"
                )
    assert found == sync_funcs

    # Behavioral check: autopull and autopush with a managed record, a pending
    # device and no mm config send no Syncthing write request.
    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    assert not config.CONFIG_PATH.exists()

    for cmd in ("autopull", "autopush"):
        res = runner.invoke(app, [cmd])
        assert res.exit_code == 0, res.output
    assert fake_syncthing.bodies == []


def test_approve_outcome_lines_emit_no_ansi_under_forced_terminal(
    fake_syncthing, tmp_path, monkeypatch
):
    import io

    from rich.console import Console

    from mind_meld import devicesflow

    _my_id, _peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    buf = io.StringIO()
    monkeypatch.setattr(
        devicesflow,
        "console",
        Console(file=buf, force_terminal=True, color_system="standard", width=240),
    )

    devicesflow.approve(joining_id)
    devicesflow.approve(joining_id)

    lines = buf.getvalue().splitlines()
    assert lines == [
        f"Approve: approved {joining_id}: Syncthing here shares the fleet folder with it "
        "and marks it as introducer. On the new machine, Run: mm init.",
        f"Approve: {joining_id} is already approved: Syncthing on this machine already "
        "shares the fleet folder with it.",
    ]
    assert "\x1b" not in buf.getvalue()
