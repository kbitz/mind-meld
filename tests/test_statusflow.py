"""Command contracts exercised through the inspection owner, without cli."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import config, core, crypto, manifest, statusflow
from mind_meld.storage.keys import manifest_key
from mind_meld.storage.local import LocalBackend

app = typer.Typer()
app.command()(statusflow.status)
app.command()(statusflow.diag)
runner = CliRunner()

DIAG_KEYS = {
    "mm_version",
    "config",
    "crypto_init",
    "keyring",
    "root_salt_drift",
    "sidecar",
    "storage_inventory",
    "sync_scope",
    "last_autorun",
    "last_attended_runs",
    "sync_run_projection",
    "skill_links",
    "host_usage",
    "host_read_budgets",
    "host_publication",
    "discovery",
    "git_capture",
}


@pytest.fixture
def inspection(tmp_path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "note.md").write_text("context\n")
    backend = LocalBackend(tmp_path / "storage")
    crypto.bootstrap_crypto_init(backend, "contract-passphrase", argon2_memory_kb=1024)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    config.save_config(
        {
            "device": {"id": "contract-device", "name": "Contract Mac"},
            "storage": {"path": str(backend.root)},
            "sync": {
                "sources": [
                    {
                        "name": "notes",
                        "type": "generic",
                        "path": str(root),
                        "include_files": ["note.md"],
                    }
                ]
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
    )
    monkeypatch.setenv("MINDMELD_PASSPHRASE", "contract-passphrase")
    monkeypatch.setattr(statusflow, "DEFAULT_STORAGE_PATH", str(backend.root))
    return backend, root


@pytest.mark.parametrize("published", [False, True])
def test_status_contract(inspection, published):
    backend, root = inspection
    if published:
        cfg = config.load_config()
        core._init_crypto_session(backend, "contract-passphrase", cfg, read_only=True)
        snapshot = manifest.build_manifest_v2(
            cfg["device"]["id"],
            cfg["device"]["name"],
            config.get_sources(cfg, bootstrap=False),
            cfg["sync"]["max_file_size"],
        )
        backend.put(
            manifest_key(cfg["device"]["id"]),
            crypto.encrypt(manifest.serialize_manifest(snapshot), "contract-passphrase", 1024),
        )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == (
        "In sync with this device's last push."
        if published
        else "This device has not pushed yet. Run: mm push"
    )
    if published:
        (root / "note.md").write_text("changed context\n")
        changed = runner.invoke(app, ["status"])
        assert changed.exit_code == 0, changed.output
        assert changed.stdout.splitlines()[0] == (
            "0 new, 1 modified and 0 deleted waiting to push. Run: mm push"
        )


@pytest.mark.parametrize("failure", ["config", "crypto"])
def test_inspection_exit_contract(inspection, monkeypatch, failure):
    if failure == "config":
        config.CONFIG_PATH.write_text("[broken TOML")
    else:
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "wrong-passphrase")
    status = runner.invoke(app, ["status"])
    assert status.exit_code == 1, status.output
    assert "In sync" not in status.stdout
    diag = runner.invoke(app, ["diag", "--json"])
    assert diag.exit_code == 0, diag.output
    assert set(json.loads(diag.stdout)) == DIAG_KEYS


def test_diag_json_contract(inspection):
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    assert set(json.loads(result.stdout)) == DIAG_KEYS
    plain = runner.invoke(app, ["diag"])
    assert plain.exit_code == 0, plain.output
    assert "Mind Meld diag" in plain.stdout


def test_core_pauses_progress_without_cli_type_dependency():
    calls = []

    class Progress:
        @contextmanager
        def paused(self):
            calls.append("pause")
            try:
                yield
            finally:
                calls.append("resume")

    token = core._SYNC_OUTPUT.set({"progress": Progress()})
    try:
        with pytest.raises(RuntimeError, match="prompt failed"):
            with core._pause_sync_progress():
                calls.append("prompt")
                raise RuntimeError("prompt failed")
    finally:
        core._SYNC_OUTPUT.reset(token)
    assert calls == ["pause", "prompt", "resume"]
