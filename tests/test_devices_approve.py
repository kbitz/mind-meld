"""Tests for approving Syncthing devices by full ID or interactive discovery."""

from __future__ import annotations

import ast
import fcntl
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import config, core, syncthing
from mind_meld.cli import app
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend

runner = CliRunner()
FULL_ID_APPROVE_WORDS = ["mm", "devices", "approve", "<device-id>"]
JOIN_CONTINUES = (
    "The waiting mm init continues on the joining machine. If it stopped there, Run: mm init"
)


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


def test_approve_pending_device_adds_and_shares_folder(fake_syncthing, tmp_path, monkeypatch):
    my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "peer-host\x1b[31m", "address": "192.0.2.9:22000"}
    }

    res = runner.invoke(app, ["devices", "approve", joining_id])

    assert res.exit_code == 0, res.output
    assert res.output == (
        f"Approve: approved {joining_id}: Syncthing here shares the fleet folder with it. "
        f"{JOIN_CONTINUES}\n"
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
        ("PATCH", "/rest/config/folders/mind-meld"),
    ]
    assert fake_syncthing.bodies[0][2]["deviceID"] == joining_id
    assert fake_syncthing.bodies[1][2]["devices"] == [
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
        "0" * 56,
        "AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAA1",
        "1234567-1234567-1234567-1234567-1234567-1234567-1234567-1234567",
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
    assert res.output.rsplit("'", 2)[1].split() == FULL_ID_APPROVE_WORDS
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
        f"devices approve: Syncthing added {joining_id} but did not confirm sharing the "
        "fleet folder with it (unreachable/"
    ) in text1
    assert f"Start Syncthing, then run 'mm devices approve {joining_id}' again to finish." in text1
    assert text1.endswith(f"Run: {core._syncthing_start_command()}")
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [("POST", "/rest/config/devices")]
    assert joining_id not in fake_syncthing.responses["/rest/cluster/pending/devices"]

    # Restart Syncthing on the same port and state; the rerun finishes sharing only.
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
        f"Approve: finished approving {joining_id}: Syncthing here now shares the "
        "fleet folder with it (it was already added). "
        f"{JOIN_CONTINUES}\n"
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
        ),
    ]

    fake_syncthing.bodies.clear()
    third = runner.invoke(app, ["devices", "approve", joining_id])
    assert third.exit_code == 0, third.output
    assert "already approved" in third.output
    assert fake_syncthing.bodies == []


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
        f"devices approve: Syncthing added {joining_id} but did not confirm sharing the "
        "fleet folder with it (error/http-500)."
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
        "fleet folder with it (it was already added). "
        f"{JOIN_CONTINUES}\n"
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
        f"Approve: finished approving {joining_id}: Syncthing here now shares the fleet folder"
    )
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [
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
@pytest.mark.parametrize("failing", ["share_folder"])
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
    assert (
        f"devices approve: Syncthing added {joining_id} but did not confirm sharing the "
        "fleet folder with it (error/http-500)."
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
    assert second.output.startswith(
        f"Approve: finished approving {joining_id}: Syncthing here now shares the "
        "fleet folder with it (it was already added)."
    )
    assert fake_syncthing.bodies == [folder_patch]


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


def _scan_pairing_reach(
    rel: str,
    source: str,
    *,
    pairing_methods: set[str],
    raw_writes: set[str],
    entry_points: set[tuple[str, str]],
) -> tuple[set[tuple[str, str]], set[tuple[str, str, str]]]:
    """Scan ``source`` for pairing-method references and flow entry-point reach.

    Follows module aliases (``import ... as``, ``from ... import ... as``,
    variable assignments), ``from``-imported symbols, and attribute chains
    (``mind_meld.devicesflow.approve``, ``self.meshflow.run``).
    """
    flow_modules = {mod for mod, _ in entry_points}
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def scope_of(node: ast.AST) -> str:
        scope: list[str] = []
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope.append(cur.name)
        return ".".join(reversed(scope)) or "<module>"

    module_aliases: dict[str, str] = {m: m for m in flow_modules}
    symbol_aliases: dict[str, str] = {}
    stem = Path(rel).stem
    for mod, func in entry_points:
        if stem == mod:
            symbol_aliases[func] = f"{mod}.{func}"

    def _attr_chain(node: ast.AST) -> list[str]:
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        parts.reverse()
        return parts

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for mod in flow_modules:
                    if alias.name in (mod, f"mind_meld.{mod}"):
                        if alias.asname:
                            module_aliases[alias.asname] = mod
        elif isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            if mod_name in ("mind_meld", ""):
                for alias in node.names:
                    if alias.name in flow_modules:
                        module_aliases[alias.asname or alias.name] = alias.name
            for mod in flow_modules:
                if mod_name in (mod, f"mind_meld.{mod}"):
                    for alias in node.names:
                        symbol_aliases[alias.asname or alias.name] = f"{mod}.{alias.name}"

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            val = node.value
            if val is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not target_names:
                continue
            chain = _attr_chain(val)
            if len(chain) == 1 and chain[0] in module_aliases:
                for tname in target_names:
                    module_aliases[tname] = module_aliases[chain[0]]
            elif len(chain) >= 2:
                for idx in range(len(chain) - 1):
                    seg, nxt = chain[idx], chain[idx + 1]
                    canon_mod = module_aliases.get(seg, seg if seg in flow_modules else None)
                    if canon_mod in flow_modules:
                        for tname in target_names:
                            symbol_aliases[tname] = f"{canon_mod}.{nxt}"

    call_sites: set[tuple[str, str]] = set()
    entry_refs: set[tuple[str, str, str]] = set()

    for node in ast.walk(tree):
        where = scope_of(node)
        if where == "<module>":
            def_scope = (
                node.name
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else "<module>"
            )
        else:
            def_scope = where
        pairing = (isinstance(node, ast.Attribute) and node.attr in pairing_methods) or (
            isinstance(node, ast.Constant) and node.value in pairing_methods
        )
        raw = (isinstance(node, ast.Name) and node.id in raw_writes) or (
            isinstance(node, ast.Attribute) and node.attr in raw_writes
        )
        if pairing or raw:
            if rel == "syncthing.py" and (raw or def_scope.split(".")[0] == "SyncthingWriter"):
                continue
            call_sites.add((rel, where))

        if isinstance(node, ast.Attribute):
            chain = _attr_chain(node)
            for idx in range(len(chain) - 1):
                seg, nxt = chain[idx], chain[idx + 1]
                canon_mod = module_aliases.get(seg, seg if seg in flow_modules else None)
                if canon_mod is not None and (
                    (canon_mod, nxt) in entry_points or canon_mod == "meshflow"
                ):
                    if (
                        rel == "meshflow.py"
                        and canon_mod == "meshflow"
                        and (canon_mod, nxt) not in entry_points
                    ):
                        continue
                    entry_refs.add((rel, where, f"{canon_mod}.{nxt}"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node:
                continue
            canon = symbol_aliases.get(node.id)
            if canon is not None:
                c_mod, c_fn = canon.split(".", 1)
                if (c_mod, c_fn) in entry_points or c_mod == "meshflow":
                    entry_refs.add((rel, where, canon))

    return call_sites, entry_refs


# Value: protects=the same vouched knock stays pending in pull preview and pairs under lock in sync;
# fails_when=preview touches Syncthing or a real mutation moves outside the sync lock;
# why_new=older preview and lock tests do not observe eligible peers and mesh writes; seam=none;
# no_claim=serialization with manual approval or Syncthing's own introductions (loopback stand-in).
def test_no_push_pull_or_auto_command_ever_accepts_a_device(fake_syncthing, tmp_path, monkeypatch):
    from mind_meld import crypto, devices

    src_root = Path(__file__).resolve().parents[1] / "src" / "mind_meld"
    pairing_methods = {
        "pending_devices",
        "approval_state",
        "mesh_snapshot",
        "add_device",
        "clear_introducer",
        "share_folder",
        "accept_folder_offer",
        "remove_device",
        "set_device_name",
    }
    raw_writes = {"_send_json", "_WRITE_ROUTES"}
    entry_points = {
        ("devicesflow", "approve"),
        ("initflow", "_managed_join"),
        ("initflow", "init"),
        ("meshflow", "run"),
        ("meshflow", "_run_inner"),
    }
    call_sites: set[tuple[str, str]] = set()
    entry_refs: set[tuple[str, str, str]] = set()
    clear_sites: set[tuple[str, str]] = set()
    for path in sorted(src_root.rglob("*.py")):
        rel = str(path.relative_to(src_root))
        source = path.read_text(encoding="utf-8")
        sites, refs = _scan_pairing_reach(
            rel,
            source,
            pairing_methods=pairing_methods,
            raw_writes=raw_writes,
            entry_points=entry_points,
        )
        call_sites.update(sites)
        entry_refs.update(refs)
        c_sites, _ = _scan_pairing_reach(
            rel,
            source,
            pairing_methods={"clear_introducer"},
            raw_writes=set(),
            entry_points=entry_points,
        )
        clear_sites.update(c_sites)

        # No source file may define/reference mark_introducer or write {"introducer": True}.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name != "mark_introducer", f"{rel} defines mark_introducer"
            elif isinstance(node, ast.Attribute):
                assert node.attr != "mark_introducer", f"{rel} references mark_introducer"
            elif isinstance(node, ast.Name):
                assert node.id != "mark_introducer", f"{rel} references mark_introducer"
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value != "mark_introducer", f"{rel} names mark_introducer"
            elif isinstance(node, ast.Dict):
                for key_node, val_node in zip(node.keys, node.values, strict=False):
                    if (
                        isinstance(key_node, ast.Constant)
                        and key_node.value == "introducer"
                        and isinstance(val_node, ast.Constant)
                        and val_node.value is True
                    ):
                        raise AssertionError(f"{rel} constructs {{'introducer': True}}")

    assert call_sites == {
        ("devicesflow.py", "approve"),
        ("initflow.py", "_managed_join"),
        ("meshflow.py", "_run_inner"),
    }
    assert clear_sites == {("meshflow.py", "_run_inner")}
    # The three pairing paths are reachable only from their authorized callers.
    assert entry_refs == {
        ("cli.py", "devices_approve", "devicesflow.approve"),
        ("cli.py", "init", "initflow.init"),
        ("initflow.py", "init", "initflow._managed_join"),
        ("cli.py", "push", "meshflow.run"),
        ("cli.py", "_attended_pull", "meshflow.run"),
        ("cli.py", "autopush", "meshflow.run"),
        ("cli.py", "autopull", "meshflow.run"),
        ("meshflow.py", "run", "meshflow._run_inner"),
    }

    # Prove the widened AST reach detector catches aliases, from-imports and attribute chains.
    for snippet, expected_ref in (
        ("from mind_meld import meshflow as mf\ndef rogue(): mf.run()", "meshflow.run"),
        (
            "from mind_meld.devicesflow import approve as ap\ndef rogue(): ap()",
            "devicesflow.approve",
        ),
        (
            "import mind_meld.devicesflow\ndef rogue(): mind_meld.devicesflow.approve()",
            "devicesflow.approve",
        ),
        ("def rogue(holder): holder.meshflow.run()", "meshflow.run"),
        ("from mind_meld import meshflow\ndef rogue():\n f = meshflow.run\n f()", "meshflow.run"),
    ):
        _, syn_refs = _scan_pairing_reach(
            "rogue.py",
            snippet,
            pairing_methods=pairing_methods,
            raw_writes=raw_writes,
            entry_points=entry_points,
        )
        assert ("rogue.py", "rogue", expected_ref) in syn_refs, snippet

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

    # Behavioral check 1: uninitialized autopull and autopush send no write request.
    my_id, vouched_id, unvouched_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        vouched_id: {"name": "mac-b"},
        unvouched_id: {"name": "stranger-box"},
    }
    assert not config.CONFIG_PATH.exists()

    for cmd in ("autopull", "autopush"):
        res = runner.invoke(app, [cmd])
        assert res.exit_code == 0, res.output
    assert fake_syncthing.bodies == []

    # Behavioral check 2: on an initialized fleet with a managed record and two
    # pending devices (one named by a fleet record, one not), push, pull,
    # autopush and autopull each add and share the folder with the first
    # (never setting introducer) and leave the second pending with no write.
    storage_dir = tmp_path / "sync"
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects").mkdir(parents=True, exist_ok=True)
    events_dir = tmp_path / "mm-events"
    (events_dir / "events").mkdir(parents=True, exist_ok=True)
    backend = LocalBackend(str(storage_dir))
    passphrase = "test-fleet-passphrase"
    memory_kb = 1024
    init_fetch = crypto.bootstrap_crypto_init(backend, passphrase, memory_kb)
    root_salt = init_fetch.root_salt
    assert root_salt is not None
    crypto.set_crypto_session(root_salt, memory_kb)
    register_device(backend, "dev-a", "mac-a")
    register_device(backend, "dev-b", "mac-b")
    devices.publish_record(backend, "dev-a", my_id, passphrase, memory_kb)
    devices.publish_record(backend, "dev-b", vouched_id, passphrase, memory_kb)
    config.save_config(
        {
            "device": {"id": "dev-a", "name": "mac-a"},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "sources": [
                    {"name": "claude", "type": "claude", "path": str(claude_dir)},
                    {"name": "mm-events", "type": "generic", "path": str(events_dir)},
                ],
                "max_file_size": 10 * 1024 * 1024,
            },
            "crypto": {
                "argon2_memory_kb": memory_kb,
                "root_salt_fp": crypto.root_salt_fingerprint(root_salt),
            },
        }
    )
    monkeypatch.setenv("MINDMELD_PASSPHRASE", passphrase)

    lock_held_during_writes: list[bool] = []

    def observe_sync_lock(_method, _path, _body):
        with config.LOCK_PATH.open("r+b") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_held_during_writes.append(True)
            else:
                lock_held_during_writes.append(False)

    fake_syncthing.on_write = observe_sync_lock
    for cmd in ("push", "pull", "autopush", "autopull"):
        fake_syncthing.set_folder(storage_dir, peers=[])
        fake_syncthing.responses["/rest/cluster/pending/devices"] = {
            vouched_id: {"name": "vouched-peer"},
            unvouched_id: {"name": "unvouched-stranger"},
        }
        fake_syncthing.requests.clear()
        fake_syncthing.bodies.clear()
        lock_held_during_writes.clear()

        if cmd == "pull":
            preview = runner.invoke(app, ["pull", "--dry-run"])
            assert preview.exit_code == 0, preview.output
            assert fake_syncthing.requests == []
            assert vouched_id in fake_syncthing.responses["/rest/cluster/pending/devices"]

        res = runner.invoke(app, [cmd])
        assert res.exit_code == 0, f"{cmd} failed: {res.output}"
        assert fake_syncthing.bodies == [
            (
                "POST",
                "/rest/config/devices",
                {
                    "deviceID": vouched_id,
                    "name": "mac-b",
                    "addresses": ["dynamic"],
                    "introducer": False,
                    "autoAcceptFolders": False,
                    "paused": False,
                },
            ),
            (
                "PATCH",
                "/rest/config/folders/mind-meld",
                {
                    "devices": [
                        {"deviceID": my_id},
                        {"deviceID": vouched_id},
                    ]
                },
            ),
        ], f"{cmd} bodies mismatch"
        assert lock_held_during_writes == [True, True], cmd
        assert unvouched_id in fake_syncthing.responses["/rest/cluster/pending/devices"]
        assert vouched_id not in fake_syncthing.responses["/rest/cluster/pending/devices"]


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
    monkeypatch.setattr(core, "console", devicesflow.console)

    devicesflow.approve(joining_id)
    devicesflow.approve(joining_id)

    lines = buf.getvalue().splitlines()
    assert lines == [
        f"Approve: approved {joining_id}: Syncthing here shares the fleet folder with it. "
        f"{JOIN_CONTINUES}",
        f"Approve: {joining_id} is already approved: Syncthing on this machine already "
        "shares the fleet folder with it.",
    ]
    assert "\x1b" not in buf.getvalue()


def test_approve_no_arg_name_short_id_ambiguity_and_fleet_context(
    fake_syncthing, tmp_path, monkeypatch
):
    """Track 154A AC1, AC2, C4, X2, X3, X4, X12, X13, X14, X15, X17, E11, E12, E17."""
    from mind_meld import crypto, devices

    my_id, peer_id, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    joining_2 = _valid_syncthing_id("E")
    vouched_id = _valid_syncthing_id("F")
    retired_id = _valid_syncthing_id("G")

    # 1. Empty or whitespace argument refused before any Syncthing request
    fake_syncthing.requests.clear()
    for blank in ("", "   ", "\t"):
        res_blank = runner.invoke(app, ["devices", "approve", blank])
        assert res_blank.exit_code == 1
        assert "cannot be empty" in res_blank.output
    assert fake_syncthing.requests == []

    # 2. No pending devices: exits 0 on non-TTY and TTY alike
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {}
    res_empty = runner.invoke(app, ["devices", "approve"])
    assert res_empty.exit_code == 0, res_empty.output
    assert (
        "Approve: no pending devices are asking to join. Nothing was changed." in res_empty.output
    )

    # 3. Off-terminal discovery lists names and short IDs, then requires the full ID.
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {joining_id: {"name": "cachyos"}}
    res_nontty_noarg = runner.invoke(app, ["devices", "approve"])
    assert res_nontty_noarg.exit_code == 1
    assert f"cachyos ({joining_id[:7]})" in res_nontty_noarg.output
    assert joining_id not in res_nontty_noarg.output
    assert res_nontty_noarg.output.rstrip().splitlines()[-1].split() == [
        "Run:",
        *FULL_ID_APPROVE_WORDS,
    ]
    assert fake_syncthing.bodies == []

    for arg in ("cachyos", joining_id[:7]):
        res_nontty_arg = runner.invoke(app, ["devices", "approve", arg])
        assert res_nontty_arg.exit_code == 1
        assert "approving by name or short ID needs a terminal" in res_nontty_arg.output
        assert res_nontty_arg.output.rstrip().splitlines()[-1].split() == [
            "Run:",
            *FULL_ID_APPROVE_WORDS,
        ]
        assert joining_id not in res_nontty_arg.output
        assert fake_syncthing.bodies == []

    # Enable terminal mode for interactive tests
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)

    # 4. Single pending device on TTY: decline with 'n' exits 0 with Nothing was changed
    res_decline = runner.invoke(app, ["devices", "approve"], input="n\n")
    assert res_decline.exit_code == 0, res_decline.output
    assert f"Approve cachyos ({joining_id[:7]})? [y/n] (Enter = y)" in res_decline.output
    assert "Approve: Nothing was changed." in res_decline.output
    assert fake_syncthing.bodies == []

    # 5. Approve by short-ID prefix (case-insensitive, 4+ chars) on TTY
    res_short = runner.invoke(app, ["devices", "approve", joining_id[:4].lower()], input="\n")
    assert res_short.exit_code == 0, res_short.output
    assert f"Approve cachyos ({joining_id[:7]})? [y/n] (Enter = y)" in res_short.output
    assert f"Approve: approved cachyos ({joining_id})" in res_short.output
    assert res_short.output.rstrip().endswith(JOIN_CONTINUES)
    assert len(fake_syncthing.bodies) == 2

    # 6. Configured & already shared device queried by name or short ID -> already approved (exit 0)
    fake_syncthing.bodies.clear()
    fake_syncthing.responses["/rest/config/devices"][-1]["name"] = "cachyos"
    res_already = runner.invoke(app, ["devices", "approve", "cachyos"])
    assert res_already.exit_code == 0, res_already.output
    assert f"Approve: cachyos ({joining_id[:7]}) is already approved" in res_already.output
    assert fake_syncthing.bodies == []

    # 7. Configured half-finished writer_shape device queried by name/short-ID -> directs to full ID
    half_id = _valid_syncthing_id("H")
    fake_syncthing.responses["/rest/config/devices"].append(
        {
            "deviceID": half_id,
            "name": "half-box",
            "addresses": ["dynamic"],
            "introducer": False,
            "autoAcceptFolders": False,
            "paused": False,
        }
    )
    res_half = runner.invoke(app, ["devices", "approve", "half-box"])
    assert res_half.exit_code == 1
    assert f"Run: mm devices approve {half_id}" in res_half.output

    # 8. 1-3 character argument matches only as an exact device name, not a prefix
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {joining_2: {"name": "omarchy-2"}}
    res_short_prefix = runner.invoke(app, ["devices", "approve", joining_2[:3]])
    assert res_short_prefix.exit_code == 1
    assert "short-ID prefixes need at least 4 characters" in res_short_prefix.output
    assert f"omarchy-2 ({joining_2[:7]})" in res_short_prefix.output
    assert res_short_prefix.output.rstrip().endswith(f"Run: mm devices approve {joining_2[:7]}")

    # 9. Multiple pending devices: ambiguity refusal on name/prefix arg, and interactive chooser
    # Construct a second device ID sharing the first 4 chars with joining_2
    raw56 = joining_2.replace("-", "")
    alt_chunk0 = raw56[:4] + ("B" if raw56[4] != "B" else "C") + raw56[5:13]
    alt_group0 = alt_chunk0 + syncthing._luhn32(alt_chunk0)
    alt56 = alt_group0 + raw56[14:]
    joining_2b = "-".join(alt56[i : i + 7] for i in range(0, 56, 7))
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_2: {"name": "twin-box"},
        joining_2b: {"name": "twin-box"},
    }
    res_ambig = runner.invoke(app, ["devices", "approve", "twin-box"])
    assert res_ambig.exit_code == 1
    assert "matches 2 pending devices" in res_ambig.output
    assert res_ambig.output.rstrip().endswith("Run: mm devices approve <short ID>")
    prefix_collision = runner.invoke(app, ["devices", "approve", joining_2[:4]])
    assert prefix_collision.exit_code == 1
    assert "matches 2 pending devices" in prefix_collision.output
    assert prefix_collision.output.rstrip().endswith(
        "Run: mm devices approve <longer short-ID prefix>"
    )
    unmatched = runner.invoke(app, ["devices", "approve", "ZZZZ"])
    assert unmatched.exit_code == 1
    assert f"twin-box ({joining_2[:7]})" in unmatched.output
    assert f"twin-box ({joining_2b[:7]})" in unmatched.output
    assert unmatched.output.rstrip().endswith("Run: mm devices approve <short ID>")
    assert fake_syncthing.bodies == []

    # Chooser: Enter cancels (exit 0); invalid choice re-asks once then approves 7-char prefix
    res_choose_cancel = runner.invoke(app, ["devices", "approve"], input="\n")
    assert res_choose_cancel.exit_code == 0, res_choose_cancel.output
    assert "Approve: Nothing was changed." in res_choose_cancel.output

    fake_syncthing.bodies.clear()
    res_choose_ok = runner.invoke(app, ["devices", "approve"], input=f"twin-box\n{joining_2[:7]}\n")
    assert res_choose_ok.exit_code == 0, res_choose_ok.output
    assert "Enter one listed device name or short ID" in res_choose_ok.output
    assert f"Approve: approved twin-box ({joining_2})" in res_choose_ok.output
    assert len(fake_syncthing.bodies) == 2

    # 10. Fleet context: vouched knocker explained & filtered, unpublished fleet labeled,
    # retired knocker flagged (retired) and refused by name/short-ID
    storage_dir = tmp_path / "sync"
    backend = LocalBackend(str(storage_dir))
    passphrase = "approve-context-pass"
    init_fetch = crypto.bootstrap_crypto_init(backend, passphrase, 1024)
    assert init_fetch.root_salt is not None
    crypto.set_crypto_session(init_fetch.root_salt, 1024)
    register_device(backend, "dev-a", "mac-a")
    register_device(backend, "dev-vouched", "mac-vouched")
    register_device(backend, "dev-unpub", "unpub-laptop")
    devices.publish_record(backend, "dev-a", my_id, passphrase, 1024)
    devices.publish_record(backend, "dev-vouched", vouched_id, passphrase, 1024)
    devices.write_retirement_record(backend, "dev-ret", "old-laptop", retired_id, passphrase, 1024)
    config.save_config(
        {
            "device": {"id": "dev-a", "name": "mac-a"},
            "storage": {"path": str(storage_dir)},
            "sync": {"sources": [], "max_file_size": 1024 * 1024},
            "crypto": {
                "argon2_memory_kb": 1024,
                "root_salt_fp": crypto.root_salt_fingerprint(init_fetch.root_salt),
            },
        }
    )
    monkeypatch.setenv("MINDMELD_PASSPHRASE", passphrase)

    unpub_knocker = _valid_syncthing_id("J")
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        vouched_id: {"name": "mac-vouched"},
        unpub_knocker: {"name": "unpub-laptop"},
        retired_id: {"name": "old-laptop"},
    }
    res_ctx = runner.invoke(app, ["devices", "approve"], input="\n")
    assert res_ctx.exit_code == 0, res_ctx.output
    flat_ctx = " ".join(res_ctx.output.split())
    assert (
        f"mac-vouched ({vouched_id[:7]}) is asking to join and a fleet record vouches for it; "
        "the next push or pull pairs it. Run: mm pull"
    ) in flat_ctx
    assert (
        f"unpub-laptop ({unpub_knocker[:7]}) (fleet machine without a published Syncthing "
        "record; on it run 'mm update', then 'mm push')"
    ) in flat_ctx
    assert f"old-laptop ({retired_id[:7]}) (retired)" in flat_ctx

    # An explicit name resolves across all pending IDs, including vouched peers.
    fake_syncthing.responses["/rest/cluster/pending/devices"][unpub_knocker]["name"] = "mac-vouched"
    fake_syncthing.bodies.clear()
    ambiguous = runner.invoke(app, ["devices", "approve", "mac-vouched"], input="\n")
    assert ambiguous.exit_code == 1
    assert "matches 2 pending devices" in ambiguous.output
    assert vouched_id[:7] in ambiguous.output and unpub_knocker[:7] in ambiguous.output
    assert fake_syncthing.bodies == []

    # A failure reading fleet labels must not lose a separately readable retirement.
    def unavailable(*args, **kwargs):
        raise OSError("fleet records unavailable")

    fake_syncthing.responses["/rest/cluster/pending/devices"] = {retired_id: {"name": "old-laptop"}}
    with monkeypatch.context() as m:
        m.setattr(devices, "read_fleet_records", unavailable)
        failed_read = runner.invoke(app, ["devices", "approve"], input="\n")
    assert failed_read.exit_code == 1
    assert "could not read fleet records" in failed_read.output
    assert f"old-laptop ({retired_id[:7]}) (retired)" in failed_read.output
    assert fake_syncthing.bodies == []

    # Approving retired device by name or short ID refuses and points to full ID
    res_ret_refuse = runner.invoke(app, ["devices", "approve", "old-laptop"])
    assert res_ret_refuse.exit_code == 1
    assert "is a retired fleet device" in res_ret_refuse.output
    assert f"Run: mm devices approve {retired_id}" in res_ret_refuse.output

    # Approving retired device by full ID still works
    fake_syncthing.bodies.clear()
    res_ret_full = runner.invoke(app, ["devices", "approve", retired_id])
    assert res_ret_full.exit_code == 0, res_ret_full.output
    assert f"Approve: approved {retired_id}" in res_ret_full.output
    assert len(fake_syncthing.bodies) == 2


def test_short_approval_rechecks_pending_request_after_confirmation(
    fake_syncthing, tmp_path, monkeypatch
):
    _, _, joining = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    pending = "/rest/cluster/pending/devices"
    fake_syncthing.responses[pending] = {joining: {"name": "linux-b"}}
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)

    def dismiss_during_confirmation(prompt, *, default):
        assert default
        fake_syncthing.responses[pending] = {}
        return True

    monkeypatch.setattr(core, "ask_yes_no", dismiss_during_confirmation)
    result = runner.invoke(app, ["devices", "approve", joining[:7]])
    assert result.exit_code == 1
    assert "no longer pending" in result.output
    assert fake_syncthing.bodies == []


@pytest.mark.parametrize("argument", [[], ["linux-b"]], ids=["no-argument", "name"])
def test_pending_approval_accepts_enter_when_optional_context_is_unavailable(
    fake_syncthing, tmp_path, monkeypatch, argument
):
    from mind_meld import crypto

    _, _, joining = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    config.save_config(
        {
            "device": {"id": "dev-a", "name": "mac-a"},
            "storage": {"path": str(tmp_path / "sync")},
            "sync": {"sources": [], "max_file_size": 1024 * 1024},
        }
    )

    def unavailable(*, non_interactive):
        assert non_interactive
        raise RuntimeError("simulated backend failure")

    monkeypatch.setattr(crypto, "get_passphrase", unavailable)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {joining: {"name": "linux-b"}}
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)
    result = runner.invoke(app, ["devices", "approve", *argument], input="\n")
    assert result.exit_code == 0, result.output
    assert f"Approve linux-b ({joining[:7]})? [y/n] (Enter = y)" in result.output
    assert result.output.count("could not read fleet records") == (0 if argument else 1)
    assert [method for method, _, _ in fake_syncthing.bodies] == ["POST", "PATCH"]


def test_interactive_approve_stops_on_device_changed_when_paused_before_share(
    fake_syncthing, tmp_path, monkeypatch
):
    _, _, joining = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {joining: {"name": "linux-b"}}
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)

    def pause_after_add(method, path, _body):
        if method == "POST" and path == "/rest/config/devices":
            for dev in fake_syncthing.responses["/rest/config/devices"]:
                if dev["deviceID"] == joining:
                    dev["paused"] = True

    fake_syncthing.on_write = pause_after_add
    res = runner.invoke(app, ["devices", "approve", "linux-b"], input="\n")
    assert res.exit_code == 1
    text = " ".join(res.output.split())
    assert (
        f"devices approve: Syncthing added {joining} but did not confirm sharing the "
        "fleet folder with it (refused/device-changed)."
    ) in text
    assert [(m, p) for m, p, _ in fake_syncthing.bodies] == [("POST", "/rest/config/devices")]


@pytest.mark.parametrize("dashless", [False, True])
def test_off_terminal_discovery_keeps_full_ids_out_of_announced_names(
    fake_syncthing, tmp_path, monkeypatch, dashless
):
    _, _, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    name_id = joining_id.replace("-", "").lower() if dashless else joining_id
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": f"joining-box {name_id}"}
    }
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: False)
    res = runner.invoke(app, ["devices", "approve"])
    assert res.exit_code == 1 and fake_syncthing.bodies == []
    assert "joining-box" in res.output and joining_id[:7] in res.output
    assert name_id not in res.output


def test_tty_discovery_detects_denied_fleet_record_listing(fake_syncthing, tmp_path, monkeypatch):
    import os

    from mind_meld import crypto, devices

    my_id, _, joining_id = _setup_managed_fleet(fake_syncthing, tmp_path, monkeypatch)
    root = tmp_path / "sync"
    backend = LocalBackend(str(root))
    passphrase = "fixture-passphrase"
    memory_kb = 1024
    monkeypatch.setattr(crypto, "MIN_ARGON2_MEMORY_KB", memory_kb, raising=False)
    fetch = crypto.bootstrap_crypto_init(backend, passphrase, memory_kb)
    assert fetch.root_salt is not None
    crypto.set_crypto_session(fetch.root_salt, memory_kb)
    register_device(backend, "dev-a", "mac-a")
    devices.publish_record(backend, "dev-a", my_id, passphrase, memory_kb)
    config.save_config(
        {
            "device": {"id": "dev-a", "name": "mac-a"},
            "storage": {"path": str(root)},
            "sync": {"sources": [], "max_file_size": 1024},
            "crypto": {
                "argon2_memory_kb": memory_kb,
                "root_salt_fp": crypto.root_salt_fingerprint(fetch.root_salt),
            },
        }
    )
    monkeypatch.setattr(crypto, "get_passphrase", lambda **opts: passphrase)
    monkeypatch.setattr(core, "_both_streams_are_terminals", lambda: True)
    real_scandir = os.scandir

    def deny_fleet_records(path):
        if not isinstance(path, int) and Path(path) == root / "syncthing":
            raise PermissionError("fixture denies the fleet record listing")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_fleet_records)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_id: {"name": "joining-box"}
    }
    res = runner.invoke(app, ["devices", "approve"], input="n\n")
    assert res.exit_code == 0 and fake_syncthing.bodies == []
    assert f"Approve joining-box ({joining_id[:7]})?" in res.output
    assert "could not read fleet records" in res.output
