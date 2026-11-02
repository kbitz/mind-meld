"""Local observations describe prior sync attempts, never current peer state."""

from __future__ import annotations

import json

import pytest

from mind_meld import config, fsutil, lockedjson, syncdiag
from tests import test_pullflow as pull_contract


@pytest.fixture
def observation_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    return tmp_path / "sync-observations.json"


def test_missing_and_malformed_snapshots_do_not_create_or_repair(observation_path):
    assert syncdiag.read_observations()["state"] == "missing"
    assert not observation_path.exists()
    observation_path.write_bytes(b"broken")
    assert syncdiag.read_observations()["state"] == "malformed"
    assert observation_path.read_bytes() == b"broken"


def test_pull_snapshot_retains_both_mtimes_and_is_explicitly_historical(observation_path):
    row = {
        "peer": "Peer B",
        "source": "notes",
        "path": "note.md",
        "local_mtime": "2026-10-31T12:00:00+00:00",
        "peer_mtime": "2026-10-30T12:00:00+00:00",
    }
    assert (
        syncdiag.record_pull_observations(
            [row], observed_at="2026-10-31T13:00:00+00:00", complete=False
        )
        is None
    )
    before = observation_path.stat()
    snapshot = syncdiag.read_observations()
    assert snapshot["pull"]["older"] == [row]
    assert snapshot["pull"]["observed_at"] == "2026-10-31T13:00:00+00:00"
    assert snapshot["pull"]["complete"] is False
    assert snapshot["pull"]["may_be_stale"] is True
    assert observation_path.stat().st_mtime_ns == before.st_mtime_ns


def test_anchor_counts_reset_selected_roots_and_preserve_unselected_roots(observation_path):
    a = {"source": "notes", "root": ".", "folder": "/notes", "state": "mismatch"}
    b = {"source": "other", "root": "mem", "folder": "/other/mem", "state": "mismatch"}
    assert syncdiag.record_push_anchor_observations([a, b]) == ([], None)
    repeated, error = syncdiag.record_push_anchor_observations([a])
    assert error is None and [r["source"] for r in repeated] == ["notes"]
    assert repeated[0]["count"] == 2
    assert syncdiag.record_push_anchor_observations([{**a, "state": "stable"}]) == ([], None)
    repeated, error = syncdiag.record_push_anchor_observations([b])
    assert error is None and repeated[0]["source"] == "other" and repeated[0]["count"] == 2


def test_cache_rejects_hostile_shapes_and_never_returns_terminal_controls(observation_path):
    observation_path.write_text(
        json.dumps(
            {
                "version": 1,
                "pull": {
                    "observed_at": "2026-10-31T13:00:00+00:00",
                    "complete": True,
                    "older": [
                        None,
                        {"peer": [], "source": "notes", "path": "../escape"},
                        {
                            "peer": "B\n\x1b[31m",
                            "source": "notes",
                            "path": "note.md",
                            "local_mtime": [],
                            "peer_mtime": "nonsense",
                        },
                    ],
                },
                "push": {
                    "records": [
                        None,
                        {
                            "source": "notes",
                            "root": ".",
                            "folder": "/n",
                            "state": "mismatch",
                            "count": True,
                        },
                    ]
                },
            }
        )
    )
    snapshot = syncdiag.read_observations()
    assert len(snapshot["pull"]["older"]) == 1
    row = snapshot["pull"]["older"][0]
    assert "\n" not in row["peer"] and "\x1b" not in row["peer"]
    assert row["local_mtime"] is None and row["peer_mtime"] is None
    assert snapshot["push"]["records"] == []


def test_stable_root_observation_is_not_rewritten_on_each_noop_push(observation_path):
    row = {"source": "notes", "root": "", "folder": "/notes", "state": "stable"}
    assert syncdiag.record_push_anchor_observations([row]) == ([], None)
    before = (observation_path.read_bytes(), observation_path.stat().st_mtime_ns)
    assert syncdiag.record_push_anchor_observations([row]) == ([], None)
    assert (observation_path.read_bytes(), observation_path.stat().st_mtime_ns) == before


def test_observation_writer_refuses_protected_state_before_creating_anything(
    synthetic_account_home, monkeypatch
):
    path = synthetic_account_home / ".config/mind-meld/sync-observations.json"
    monkeypatch.setattr(syncdiag, "cache_path", lambda: path)
    with pytest.raises(fsutil.PytestRealStateWriteError):
        syncdiag.record_pull_observations([])
    assert not path.parent.exists()


def test_cache_symlink_is_neither_read_nor_overwritten(observation_path, tmp_path):
    target = tmp_path / "other.json"
    target.write_text('{"keep":true}')
    observation_path.symlink_to(target)
    assert syncdiag.read_observations()["state"] == "unreadable"
    assert syncdiag.record_pull_observations([]) == "unavailable"
    assert target.read_text() == '{"keep":true}'


def test_writer_failure_is_returned_without_hiding_existing_observations(
    observation_path, monkeypatch
):
    assert syncdiag.record_pull_observations([]) is None
    before = observation_path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("read only")

    monkeypatch.setattr(lockedjson, "locked_json_rmw", fail)
    assert syncdiag.record_pull_observations([]) == "unavailable"
    assert observation_path.read_bytes() == before


def test_real_pull_records_compared_mtimes_and_dry_run_preserves_observation(
    tmp_path, monkeypatch, observation_path
):
    fleet = pull_contract.fleet.__wrapped__(tmp_path)
    pull_contract._seed(fleet, {"note.md": b"peer content\n"})
    pull_contract._put_local(fleet, "note.md", b"local content\n", 1_800_000_000)
    pull_contract._record(("note.md", b"local content\n"))

    result, _, _ = pull_contract._run(fleet, monkeypatch, "direct")
    assert len(result.older) == 1
    row = syncdiag.read_observations()["pull"]["older"][0]
    assert row["peer"] == "Peer B" and row["path"] == "note.md"
    assert row["local_mtime"] == result.older[0].local_mtime
    assert row["peer_mtime"] == result.older[0].peer_mtime
    assert row["local_mtime"] > row["peer_mtime"]
    before = (observation_path.read_bytes(), observation_path.stat().st_mtime_ns)

    pull_contract._run(fleet, monkeypatch, "direct", dry_run=True)

    assert (observation_path.read_bytes(), observation_path.stat().st_mtime_ns) == before


def test_real_pull_reports_cache_failure_as_maintenance_degradation(
    tmp_path, monkeypatch, observation_path
):
    fleet = pull_contract.fleet.__wrapped__(tmp_path)
    pull_contract._seed(fleet, {"note.md": b"peer content\n"})
    monkeypatch.setattr(syncdiag, "record_pull_observations", lambda *a, **kw: "unavailable")

    result, _, output = pull_contract._run(fleet, monkeypatch, "quiet")

    assert result.total_written == 1
    assert result.maintenance_failures == 1
    assert "sync observations were not saved" in output[1]


def test_autopull_observation_failure_does_not_claim_recovery_cleanup_failed(
    tmp_path, monkeypatch, observation_path
):
    from typer.testing import CliRunner

    from mind_meld import cli
    from tests.conftest import _redirect_lock, _redirect_sidecar

    fleet = pull_contract.fleet.__wrapped__(tmp_path)
    pull_contract._seed(fleet, {"note.md": b"peer content\n"})
    config.save_config(fleet.config)
    _redirect_lock(monkeypatch, tmp_path)
    local_state = _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", pull_contract.PASSPHRASE)
    monkeypatch.setattr(syncdiag, "record_pull_observations", lambda *a, **kw: "unavailable")

    result = CliRunner().invoke(cli.app, ["autopull"])

    assert result.exit_code == 0, result.output
    assert (fleet.local / "note.md").read_bytes() == b"peer content\n"
    breadcrumb = json.loads((local_state / "last-autorun.json").read_text())["pull"]
    assert breadcrumb["outcome"] == "degraded"
    assert breadcrumb["detail"] == "1 follow-up failure(s)"
    assert "recovery cleanup" not in breadcrumb["detail"]
