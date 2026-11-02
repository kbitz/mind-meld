"""Command contracts exercised through the inspection owner, without cli."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import config, core, crypto, manifest, pullplan, statedb, statusflow, syncdiag
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
    "project_index",
    "sync_scope",
    "fast_forward",
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


# ── Track 91A: the read-only `fast_forward` section of mm diag ─────────


def _diag_fast_forward():
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["fast_forward"]


def test_diag_fast_forward_reports_an_absent_ledger_and_creates_no_database(inspection):
    _, root = inspection
    assert not statedb.db_path().exists()
    section = _diag_fast_forward()
    assert section["ledger"] == "absent"
    assert section["peer_base_rows"] == {}
    assert section["seen_version_rows"] == 0
    # The source lists its own folder (include_files), which has no token yet.
    assert section["anchors"] == {"notes": {".": "no token"}}
    plain = runner.invoke(app, ["diag"])
    assert plain.exit_code == 0, plain.output
    assert "Fast-forward ledger" in plain.stdout
    assert "state:         absent" in plain.stdout
    assert "scan root notes:.: no token" in plain.stdout
    # Inspection never creates state: no database, and no token written.
    assert not statedb.db_path().exists()
    assert not (root / manifest.ANCHOR_NAME).exists()


def test_diag_fast_forward_counts_rows_per_peer_and_reads_scan_root_tokens(inspection):
    _, root = inspection
    ledger = pullplan.Ledger()
    ledger.settle("peer-one", "notes", "note.md", "a" * 64)
    ledger.settle("peer-one", "notes", "other.md", "b" * 64)
    ledger.settle("peer-two", "notes", "note.md", "c" * 64)
    assert core.commit_ledger(ledger) is None
    (root / manifest.ANCHOR_NAME).write_text("0123456789abcdef0123456789abcdef\n")
    before = statedb.db_path().read_bytes()

    section = _diag_fast_forward()

    assert section["ledger"] == "ok"
    assert section["peer_base_rows"] == {"peer-one": 2, "peer-two": 1}
    assert section["seen_version_rows"] == 3
    assert section["anchors"] == {"notes": {".": "token present"}}
    plain = runner.invoke(app, ["diag"])
    assert "last-synced files from peer-one: 2" in plain.stdout
    assert "last-synced files from peer-two: 1" in plain.stdout
    # Read only: the database file is byte-identical afterwards.
    assert statedb.db_path().read_bytes() == before


def test_diag_fast_forward_reports_missing_scan_root_and_database_failures(inspection):
    _, root = inspection
    cfg = config.load_config()
    cfg["sync"]["sources"] = [
        {"name": "docs", "type": "generic", "path": str(root), "include_dirs": ["gone"]}
    ]
    config.save_config(cfg)
    statedb.DB_DIR.mkdir(parents=True, exist_ok=True)
    statedb.db_path().write_bytes(b"not a database")

    section = _diag_fast_forward()

    assert section["ledger"] == "corrupt"
    assert section["peer_base_rows"] == {}
    assert section["anchors"] == {"docs": {"gone": "missing folder"}}
    # The damaged file is left exactly as found.
    assert statedb.db_path().read_bytes() == b"not a database"


@pytest.mark.parametrize("selection", [{"include_files": ["note.md"]}, {"include_dirs": ["."]}])
def test_diag_reads_an_anchor_through_a_symlinked_source_root(inspection, selection):
    _, root = inspection
    alias = root.parent / "notes-alias"
    alias.symlink_to(root, target_is_directory=True)
    cfg = config.load_config()
    source = {"name": "notes", "type": "generic", "path": str(alias), **selection}
    cfg["sync"]["sources"] = [source]
    config.save_config(cfg)
    published = {"sources": {"notes": {"files": {}}}}
    assert core.publish_anchors(published, [source], {}) == []
    anchor = root / manifest.ANCHOR_NAME
    before = (anchor.read_bytes(), anchor.stat().st_mtime_ns)

    assert _diag_fast_forward()["anchors"] == {"notes": {".": "token present"}}

    assert alias.is_symlink()
    assert (anchor.read_bytes(), anchor.stat().st_mtime_ns) == before


def test_diag_does_not_read_an_anchor_below_a_descendant_symlink(inspection, monkeypatch):
    _, root = inspection
    target = root.parent / "outside"
    target.mkdir()
    manifest.write_anchor(target, "", "a" * 32)
    (root / "linked").symlink_to(target, target_is_directory=True)
    cfg = config.load_config()
    cfg["sync"]["sources"] = [
        {"name": "notes", "type": "generic", "path": str(root), "include_dirs": ["linked"]}
    ]
    config.save_config(cfg)

    def refuse_anchor_read(*args, **kwargs):
        pytest.fail("diagnostics must not read an anchor through a descendant symlink")

    monkeypatch.setattr(manifest, "read_anchor", refuse_anchor_read)
    assert _diag_fast_forward()["anchors"] == {"notes": {"linked": "missing folder"}}


def test_diag_lists_last_observed_older_edits_with_both_mtimes_without_writes(
    inspection, monkeypatch
):
    old = "2026-10-28T10:00:00+00:00"
    local = "2026-10-29T10:00:00+00:00"
    assert (
        syncdiag.record_pull_observations(
            [
                {
                    "peer": "Peer B",
                    "source": "notes",
                    "path": "note.md",
                    "local_mtime": local,
                    "peer_mtime": old,
                }
            ],
            observed_at="2026-10-29T12:00:00+00:00",
        )
        is None
    )
    path = syncdiag.cache_path()
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)

    def refuse_decrypt(*args, **kwargs):
        pytest.fail("diag must not decrypt peer manifests")

    monkeypatch.setattr(crypto, "decrypt_manifest", refuse_decrypt)
    section = _diag_fast_forward()["observations"]
    assert section["pull"]["may_be_stale"] is True
    assert section["pull"]["older"][0]["peer_mtime"] == old
    plain = runner.invoke(app, ["diag"])
    assert plain.exit_code == 0, plain.output
    output = " ".join(plain.output.split())
    assert "Last pull observation: 2026-10-29T12:00:00+00:00" in output
    assert "may be stale; peer files not rechecked" in output
    assert "Peer B/notes/note.md" in output
    assert f"local mtime: {local}; peer mtime: {old}" in output
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == before


def test_diag_without_observations_does_not_create_a_cache(inspection):
    assert not syncdiag.cache_path().exists()
    assert _diag_fast_forward()["observations"]["state"] == "missing"
    assert not syncdiag.cache_path().exists()


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
