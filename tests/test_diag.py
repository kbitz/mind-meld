"""Tests for `mm diag` — Group 2 Pre-flight 2.

`mm diag` dumps non-secret state for support triage. Critical contract:
- NEVER emit raw root_salt, master_key, keycheck_blob, passphrase, peer
  device_ids, or any other secret.
- Delegate tri-state reads to `fetch_crypto_init` and `sidecar.read` so the
  command agrees with the recovery chain about what each state means.
- `--json` produces valid parseable JSON with the same fields as plain text.
- Must run even when the local config is broken (it's a diagnostic for that
  exact case).
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from mind_meld import sidecar as sidecar_mod
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend

PASSPHRASE = "diag-test-passphrase"
MEMORY_KB = 1024
runner = CliRunner()


def _setup(tmp_path, monkeypatch, *, with_config=True, with_crypto_init=True):
    """Shared setup: tmp storage + config + optional crypto bootstrap."""
    storage = tmp_path / "icloud"
    storage.mkdir()
    backend = LocalBackend(storage)
    if with_crypto_init:
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)

    cfg_path = tmp_path / "config.toml"
    if with_config:
        # Use save_config so the shape matches what load_config expects.
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

    # Isolate sidecar/breadcrumb to tmp_path.
    sc_dir = tmp_path / "sidecar"
    monkeypatch.setattr(sidecar_mod, "SIDECAR_DIR", sc_dir)

    return storage, cfg_path, backend


# ── JSON mode ────────────────────────────────────────────────────────────


def test_diag_json_is_valid_json(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mm_version"]
    assert payload["config"]["device_id"] == "mac-a"


def test_diag_json_includes_all_expected_sections(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runner.invoke(app, ["diag", "--json"])
    payload = json.loads(result.stdout)
    for section in (
        "mm_version",
        "config",
        "crypto_init",
        "root_salt_drift",
        "sidecar",
        "storage_inventory",
        "last_autorun",
        "skill_links",
    ):
        assert section in payload, f"missing {section}"


# ── Plain text mode ──────────────────────────────────────────────────────


def test_diag_plain_text_is_human_readable(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runner.invoke(app, ["diag"])
    assert result.exit_code == 0, result.output
    # Section headers.
    assert "Config" in result.stdout
    assert "mm-crypto-init" in result.stdout
    assert "Sidecar" in result.stdout
    assert "Storage inventory" in result.stdout
    assert "Skill links" in result.stdout


# ── Secrets boundary ─────────────────────────────────────────────────────


def test_diag_json_never_leaks_secrets(tmp_path, monkeypatch):
    """The allowlist from the command docstring: NEVER include raw
    root_salt bytes, master_key, keycheck, passphrase, or peer device_ids.

    We check by ensuring none of these field names appear anywhere in the
    serialized JSON, plus a positive check that the fingerprint (which IS
    safe to include) does appear.
    """
    _setup(tmp_path, monkeypatch)
    # Register a peer so "peer device_ids" is a real risk surface.
    backend = LocalBackend(tmp_path / "icloud")
    register_device(backend, "peer-decafbad", "Peer Mac")

    result = runner.invoke(app, ["diag", "--json"])
    payload_str = result.stdout.lower()

    # These substrings would only appear if we leaked the corresponding
    # raw value or its field name.
    for banned in (
        "master_key",
        "keycheck",
        "keycheck_blob",
        "passphrase",
        'root_salt":',  # raw bytes (JSON quote) — fingerprint uses root_salt_fp
        # Peer device_ids: the registered "peer-decafbad" must not appear.
        "peer-decafbad",
    ):
        assert banned not in payload_str, (
            f"secrets-boundary violation: {banned!r} found in diag JSON output"
        )

    # Positive check: the fingerprint (safe) IS present.
    assert "root_salt_fp" in payload_str


def test_diag_plain_text_never_leaks_secrets(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    backend = LocalBackend(tmp_path / "icloud")
    register_device(backend, "peer-cafebabe", "Peer")

    result = runner.invoke(app, ["diag"])
    out = result.stdout.lower()
    for banned in ("master_key", "keycheck", "passphrase", "peer-cafebabe"):
        assert banned not in out


# ── Degraded scenarios ───────────────────────────────────────────────────


def test_diag_handles_missing_crypto_init(tmp_path, monkeypatch):
    """mm-crypto-init not bootstrapped yet — diag must still emit state,
    not crash."""
    _setup(tmp_path, monkeypatch, with_crypto_init=False)
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["crypto_init"]["status"] == "missing"


def test_diag_handles_missing_config(tmp_path, monkeypatch):
    """No config on disk — diag falls back to DEFAULT_STORAGE_PATH and still
    runs. This is the primary use case for the command (debugging why
    config won't load)."""
    _setup(tmp_path, monkeypatch, with_config=False)
    # The cfg_path monkeypatch points at a non-existent file.
    result = runner.invoke(app, ["diag", "--json"])
    # Exit 0 — diag must be robust to config failures.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["config"]["state"].startswith("error")


def test_diag_detects_root_salt_drift(tmp_path, monkeypatch):
    """Config's root_salt_fp doesn't match storage — drift signal must show."""
    storage, cfg_path, backend = _setup(tmp_path, monkeypatch)
    # Inject a wrong root_salt_fp into config.
    import tomllib

    raw = cfg_path.read_text()
    # Append a spurious crypto.root_salt_fp — if one already exists we
    # overwrite by saving via save_config.
    cfg = tomllib.loads(raw)
    cfg.setdefault("crypto", {})["root_salt_fp"] = "deadbeef" * 8
    save_config(cfg, cfg_path)

    result = runner.invoke(app, ["diag", "--json"])
    payload = json.loads(result.stdout)
    assert payload["root_salt_drift"] == "mismatch"


# ── Storage inventory ───────────────────────────────────────────────────


def test_diag_counts_peers(tmp_path, monkeypatch):
    """Seed two peer data prefixes — diag reports the count, not the IDs."""
    storage, _cfg, backend = _setup(tmp_path, monkeypatch)
    backend.put("data/peer-aaaaaaaa/001.enc", b"stub")
    backend.put("data/peer-bbbbbbbb/002.enc", b"stub")
    backend.put("manifests/peer-aaaaaaaa/manifest.json.enc", b"stub")

    result = runner.invoke(app, ["diag", "--json"])
    payload = json.loads(result.stdout)
    assert payload["storage_inventory"]["data_peer_count"] == 2
    assert payload["storage_inventory"]["manifest_peer_count"] == 1
    # And those peer IDs must NOT appear in the output.
    assert "peer-aaaaaaaa" not in result.stdout
    assert "peer-bbbbbbbb" not in result.stdout
