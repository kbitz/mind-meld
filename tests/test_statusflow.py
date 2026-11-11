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
    "syncthing",
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


def test_track_125a_status_codex_deadline_shows_catching_up_and_errors_stay_failures(
    inspection, monkeypatch
):
    from mind_meld.hosts import codex as codex_host

    _backend, root = inspection
    codex_dir = root.parent / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "AGENTS.md").write_text("rules\n")
    cfg = config.load_config()
    cfg["sync"]["sources"].append(
        {"name": "codex", "type": "generic", "path": str(codex_dir), "include_files": ["AGENTS.md"]}
    )
    mm_events_dir = root.parent / "mm-events"
    (mm_events_dir / "events").mkdir(parents=True, exist_ok=True)
    cfg["sync"]["sources"].append(
        {
            "name": "mm-events",
            "type": "generic",
            "path": str(mm_events_dir),
            "include_dirs": ["events"],
        }
    )
    config.save_config(cfg)

    def fake_codex_diag():
        on_disk = 14
        cached = 10
        return {
            "state": "ready",
            "cache_state": "ok",
            "complete_once": True,
            "last_reason": "deadline",
            "last_reason_since": "2026-10-01T00:00:00+00:00",
            "last_deadline_allotted_ms": 250,
            "last_complete_ms": 300,
            "last_complete_at": "2026-10-01T00:00:00+00:00",
            "files_cached": cached,
            "files_on_disk": on_disk,
            "pending": on_disk - cached,
            "settled_days": 0,
            "files_pre_track": 0,
            "usage_less_skipped": 0,
            "model_count": 1,
            "models": ["gpt-5-codex"],
        }

    monkeypatch.setattr(codex_host, "codex_usage_diag", fake_codex_diag)
    diag_pending = codex_host.codex_usage_diag()["pending"]
    assert diag_pending == 4

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert f"Codex usage capture: codex usage catching up ({diag_pending} left)" in flat

    for error_reason in ("partial", "unavailable", "unsupported"):
        blocker = statusflow._host_usage_blocker(
            "codex",
            {
                "last_reason": error_reason,
                "last_reason_since": "2026-10-01T00:00:00+00:00",
                "files_cached": 10,
                "files_on_disk": 14,
            },
            readiness="ready",
        )
        assert f"host-usage snapshot skipped (codex {error_reason})" in blocker
        assert "catching up" not in blocker


def test_track_125a_status_git_capture_catching_up_and_error_shows_failure(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from io import StringIO

    from rich.console import Console

    from mind_meld import events, lockedjson

    cache_path = tmp_path / "git-capture.json"
    monkeypatch.setattr(config, "GIT_CAPTURE_CACHE_PATH", cache_path)
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    repos = []
    for name in ("repo-a", "repo-b"):
        r = tmp_path / name
        (r / ".git" / "objects").mkdir(parents=True)
        (r / ".git" / "refs").mkdir(parents=True)
        (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        repos.append(r.resolve())

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=5)
    push_ts = now - timedelta(minutes=1)
    with lockedjson.locked_json_rmw(cache_path) as state:
        state.data.update(events._default_git_capture_cache())
        state.data.update(
            {
                "incomplete_streak": 1,
                "repo_cursors": {str(r): since.isoformat() for r in repos},
            }
        )
    mm_push = events.make_mm_push_event(
        device="dev-a",
        mm_version="1.0.0",
        sources=["claude", "mm-events"],
        discovery_errors=[],
        local_emails=["user@example.com"],
        git_capture=events.make_git_capture(
            since=since,
            discovery="complete",
            walk_budget_aborts=2,
            walk_errors=0,
        ),
    )
    mm_push["ts"] = push_ts.isoformat()
    events.write_push_event(events_dir, "dev-a", [mm_push])

    expected_pending = events.count_pending_repositories(repos, push_ts)
    assert expected_pending == 2

    buf = StringIO()
    monkeypatch.setattr(statusflow, "console", Console(file=buf, force_terminal=False, width=100))
    sources = [{"name": "mm-events", "type": "mm-events", "path": str(tmp_path)}]
    assert statusflow._print_retro_capture_status(sources, "dev-a") is True
    out = " ".join(buf.getvalue().split())
    assert f"Retro capture: catching up ({expected_pending} left)" in out
    assert "Continue capture on this Mac: mm push" in out

    # Error-only stop (walk_budget_aborts=0, walk_errors=1) shows failure and recapture remedy.
    buf_err = StringIO()
    monkeypatch.setattr(
        statusflow, "console", Console(file=buf_err, force_terminal=False, width=100)
    )
    mm_push_err = dict(mm_push)
    mm_push_err["ts"] = now.isoformat()
    mm_push_err["git_capture"] = events.make_git_capture(
        since=since,
        discovery="complete",
        walk_budget_aborts=0,
        walk_errors=1,
    )
    events.write_push_event(events_dir, "dev-a", [mm_push_err])
    assert statusflow._print_retro_capture_status(sources, "dev-a") is True
    err_out = " ".join(buf_err.getvalue().split())
    assert "Retro capture: incomplete since" in err_out
    assert "1 repository was skipped." in err_out
    assert "mm recapture" in err_out
    assert "catching up (" not in err_out
