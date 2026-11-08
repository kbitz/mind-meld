"""Contract tests pinning `mm devices` table and JSON output (Track 119A)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from mind_meld import __version__
from mind_meld import sidecar as sidecar_mod
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device, update_last_seen
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "devices-command-test-passphrase"
MEMORY_KB = 1024
runner = CliRunner()


def _setup(tmp_path, monkeypatch):
    storage = tmp_path / "sync-store"
    storage.mkdir()
    backend = LocalBackend(storage)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)

    cfg_path = tmp_path / "config.toml"
    save_config(
        {
            "device": {"id": "mac-a", "name": "Mac A"},
            "storage": {"path": str(storage)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "claude",
                        "path": str(tmp_path / "claude"),
                        "type": "claude",
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        cfg_path,
    )
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
    monkeypatch.setattr(sidecar_mod, "SIDECAR_DIR", tmp_path / "sidecar")
    return backend


def test_devices_table_empty_fleet(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runner.invoke(app, ["devices"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert result.stdout == "No devices registered.\n"


def test_devices_table_two_devices_columns_and_markers(tmp_path, monkeypatch):
    backend = _setup(tmp_path, monkeypatch)
    register_device(backend, "mac-a", "Mac A")
    update_last_seen(backend, "mac-a")
    register_device(backend, "linux-b", "Linux B")

    result = runner.invoke(app, ["devices"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert any("Registered Devices" in line for line in lines)
    header_line = next(line for line in lines if "Name" in line and "Last Push" in line)
    for col in ("Name", "ID", "Last Push", "Version"):
        assert col in header_line

    mac_row = next(line for line in lines if "mac-a" in line)
    linux_row = next(line for line in lines if "linux-b" in line)
    assert "Mac A" in mac_row
    assert __version__ in mac_row
    assert "← this device" in mac_row
    assert "—" not in mac_row

    assert "Linux B" in linux_row
    assert linux_row.count("—") == 2
    assert "← this device" not in linux_row


def test_devices_table_sanitizes_peer_device_name(tmp_path, monkeypatch):
    backend = _setup(tmp_path, monkeypatch)
    register_device(backend, "mac-a", "Mac A")
    register_device(
        backend,
        "linux-b",
        "Linux [red]Markup[/red] \x1b]52;c;cGF5bG9hZA==\x07\x1b[2JPeer",
    )

    result = runner.invoke(app, ["devices"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "\x1b]52" not in result.stdout
    assert "\x1b[2J" not in result.stdout
    linux_row = next(line for line in result.stdout.splitlines() if "linux-b" in line)
    assert "[red]Markup[/red]" in linux_row
    assert "Peer" in linux_row


def test_devices_format_json_exact_bytes_and_sort(tmp_path, monkeypatch):
    backend = _setup(tmp_path, monkeypatch)
    out_empty = runner.invoke(app, ["devices", "--format", "json"])
    assert out_empty.exit_code == 0, out_empty.output
    assert out_empty.stdout == "[]\n"

    register_device(backend, "mac-a", "Mac A")
    out_one = runner.invoke(app, ["devices", "--format", "json"])
    assert out_one.exit_code == 0, out_one.output
    expected_one = (
        '[{"device_id": "mac-a", "device_name": "Mac A", '
        '"is_self": true, "last_seen": null, "last_seen_version": null}]\n'
    )
    assert out_one.stdout == expected_one

    register_device(backend, "linux-b", "Linux B")
    out_two = runner.invoke(app, ["devices", "--format", "json"])
    assert out_two.exit_code == 0, out_two.output
    parsed = json.loads(out_two.stdout)
    assert [r["device_id"] for r in parsed] == ["linux-b", "mac-a"]
    assert parsed[0] == {
        "device_id": "linux-b",
        "device_name": "Linux B",
        "is_self": False,
        "last_seen": None,
        "last_seen_version": None,
    }


def test_devices_format_case_insensitive_and_invalid(tmp_path, monkeypatch):
    backend = _setup(tmp_path, monkeypatch)
    register_device(backend, "mac-a", "Mac A")

    upper = runner.invoke(app, ["devices", "--format", "JSON"])
    assert upper.exit_code == 0, upper.output
    assert json.loads(upper.stdout)[0]["device_id"] == "mac-a"

    invalid = runner.invoke(app, ["devices", "--format", "xml"])
    assert invalid.exit_code == 1
    err = invalid.stderr if hasattr(invalid, "stderr") and invalid.stderr else invalid.output
    assert "--format must be 'table' or 'json', got 'xml'" in err
