"""Pin `mm devices --format=json` schema for the retro-fleet skill consumer.

Group 8 / Architecture #3: the retro-fleet aggregator (Track 8A) shells out to
`mm devices --format=json` to compute the "M of N known devices" breadcrumb.
The schema below IS the contract — tests pin field names, types, null
semantics on missing optionals, and stable sort order.

Schema:
    [
        {
            "device_id":          str,
            "device_name":        str | null,
            "last_seen":          str | null,
            "last_seen_version":  str | null,
            "is_self":            bool
        },
        ...
    ]

Empty fleet returns ``[]``. Order is alphabetical by device_id (stable across
runs). Plain `print(json.dumps(...))` to stdout — Rich injects styling that
breaks the JSON contract, so the implementation deliberately bypasses
`console.print`.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from mind_meld import sidecar as sidecar_mod
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device, update_last_seen
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "devices-json-test-passphrase"
MEMORY_KB = 1024
runner = CliRunner()


def _setup(tmp_path, monkeypatch):
    """Standard fixture: tmp storage + config + crypto-init."""
    storage = tmp_path / "icloud"
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


def test_format_json_empty_fleet_returns_empty_list(tmp_path, monkeypatch):
    """Pre-init / empty storage emits ``[]`` — never crashes, never prints text."""
    _setup(tmp_path, monkeypatch)
    result = runner.invoke(app, ["devices", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == []


def test_format_json_single_device_schema(tmp_path, monkeypatch):
    """One device — schema fields present, types correct, is_self=True."""
    backend = _setup(tmp_path, monkeypatch)
    register_device(backend, "mac-a", "Mac A")

    result = runner.invoke(app, ["devices", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    rec = payload[0]
    # Schema keys (the contract):
    assert set(rec.keys()) == {
        "device_id",
        "device_name",
        "last_seen",
        "last_seen_version",
        "is_self",
    }
    assert rec["device_id"] == "mac-a"
    assert rec["device_name"] == "Mac A"
    # `last_seen` and `last_seen_version` are seeded only on push — register
    # alone leaves them absent, so the JSON contract emits them as null.
    assert rec["last_seen"] is None
    assert rec["last_seen_version"] is None
    assert rec["is_self"] is True


def test_format_json_multiple_devices_alphabetical(tmp_path, monkeypatch):
    """Multi-device fleet renders in alphabetical device_id order."""
    backend = _setup(tmp_path, monkeypatch)
    register_device(backend, "mac-c", "Mac C")
    register_device(backend, "mac-a", "Mac A")
    register_device(backend, "mac-b", "Mac B")

    result = runner.invoke(app, ["devices", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ids = [r["device_id"] for r in payload]
    # Alphabetical (stable across runs — load order is FS-dependent so
    # the implementation sorts).
    assert ids == sorted(ids)


def test_format_json_is_self_marks_only_local_device(tmp_path, monkeypatch):
    """``is_self`` is True only for the device matching config.device.id."""
    backend = _setup(tmp_path, monkeypatch)
    register_device(backend, "mac-a", "Mac A")
    register_device(backend, "mac-b", "Mac B")

    result = runner.invoke(app, ["devices", "--format", "json"])
    payload = json.loads(result.stdout)
    by_id = {r["device_id"]: r for r in payload}
    assert by_id["mac-a"]["is_self"] is True
    assert by_id["mac-b"]["is_self"] is False


def test_format_json_includes_last_seen_after_push(tmp_path, monkeypatch):
    """`update_last_seen` writes a string ISO timestamp + version that flow
    through to JSON unchanged."""
    backend = _setup(tmp_path, monkeypatch)
    register_device(backend, "mac-a", "Mac A")
    update_last_seen(backend, "mac-a")

    result = runner.invoke(app, ["devices", "--format", "json"])
    payload = json.loads(result.stdout)
    rec = next(r for r in payload if r["device_id"] == "mac-a")
    assert isinstance(rec["last_seen"], str)
    assert rec["last_seen"]  # non-empty
    # last_seen_version is the mm version string (e.g. "0.10.3")
    assert isinstance(rec["last_seen_version"], str)


def test_format_invalid_value_errors_cleanly(tmp_path, monkeypatch):
    """``--format=xml`` is a typo — exits non-zero with a helpful error."""
    _setup(tmp_path, monkeypatch)
    result = runner.invoke(app, ["devices", "--format", "xml"])
    assert result.exit_code != 0
    # Error goes to stderr; help text references the valid options.
    err = result.stderr if hasattr(result, "stderr") else result.output
    assert "format" in err.lower()


def test_format_table_default_unchanged(tmp_path, monkeypatch):
    """Default ``mm devices`` (no --format) still renders the Rich table.

    Regression pin: Group 8 added the flag without breaking the existing
    human-readable default. Tests pre-Group-8 that called `mm devices` with
    no args must continue working.
    """
    backend = _setup(tmp_path, monkeypatch)
    register_device(backend, "mac-a", "Mac A")
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 0
    # Table renders with the device_id; not parseable as JSON.
    assert "mac-a" in result.output
    # Confirm we did NOT accidentally fall through to JSON output.
    assert not result.stdout.strip().startswith("[")
