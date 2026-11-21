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
def test_status_contract(inspection, monkeypatch, published):
    import os
    from types import SimpleNamespace

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
        stat = (root / "note.md").stat()
        os.utime(root / "note.md", (stat.st_atime + 60, stat.st_mtime + 60))
        mtime_only = runner.invoke(app, ["status"])
        assert mtime_only.exit_code == 0, mtime_only.output
        assert "Metadata-only changes pending (updated mtimes). Run: mm push" in mtime_only.stdout
        os.utime(root / "note.md", (stat.st_atime, stat.st_mtime))

        claude_dir = root.parent / ".claude"
        (claude_dir / "projects" / "-Users-user-app" / "memory").mkdir(parents=True)
        (claude_dir / "projects" / "-Users-user-app" / "memory" / "MEMORY.md").write_text("m\n")
        cfg = config.load_config()
        cfg["sync"]["claude_dir"] = str(claude_dir)
        cfg["sync"]["sources"].append(
            {
                "name": "claude",
                "type": "claude",
                "path": str(claude_dir),
                "include_dirs": ["projects"],
            }
        )
        config.save_config(cfg)
        snapshot_with_claude = manifest.build_manifest_v2(
            cfg["device"]["id"],
            cfg["device"]["name"],
            config.get_sources(cfg, bootstrap=False),
            cfg["sync"]["max_file_size"],
        )
        backend.put(
            manifest_key(cfg["device"]["id"]),
            crypto.encrypt(
                manifest.serialize_manifest(snapshot_with_claude), "contract-passphrase", 1024
            ),
        )
        monkeypatch.setattr(
            core,
            "project_form_state",
            lambda *a, **kw: SimpleNamespace(switch_held=False, due=True),
        )
        names_ready = runner.invoke(app, ["status"])
        assert names_ready.exit_code == 0, names_ready.output
        assert (
            "Project memory ready to publish under shared names. Run: mm push" in names_ready.stdout
        )

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

    def fake_codex_diag(*, count_logs: bool = False):
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
    assert "Continue capture on this machine: mm push" in out

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
    assert "Recover this machine's last 5 days: mm recapture 5d" in err_out
    assert "catching up (" not in err_out


def test_status_incomplete_discovery_with_zero_pending_has_no_positive_catching_up_count(
    tmp_path, monkeypatch
):
    from datetime import datetime, timedelta, timezone
    from io import StringIO

    from rich.console import Console

    from mind_meld import events, lockedjson

    cache_path = tmp_path / "git-capture.json"
    monkeypatch.setattr(config, "GIT_CAPTURE_CACHE_PATH", cache_path)
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=2)
    push_ts = now - timedelta(minutes=1)
    with lockedjson.locked_json_rmw(cache_path) as state:
        state.data.update(events._default_git_capture_cache())
        state.data.update({"incomplete_streak": 1, "repo_cursors": {}})
    assert state.data["incomplete_streak"] < 3

    mm_push = events.make_mm_push_event(
        device="dev-a",
        mm_version="1.0.0",
        sources=["claude", "mm-events"],
        discovery_errors=[],
        local_emails=["user@example.com"],
        git_capture=events.make_git_capture(
            since=since,
            discovery="partial",
            walk_budget_aborts=0,
            walk_errors=0,
        ),
    )
    mm_push["ts"] = push_ts.isoformat()
    events.write_push_event(events_dir, "dev-a", [mm_push])
    assert events.count_pending_repositories([], push_ts) == 0

    buf = StringIO()
    monkeypatch.setattr(statusflow, "console", Console(file=buf, force_terminal=False, width=100))
    sources = [{"name": "mm-events", "type": "mm-events", "path": str(tmp_path)}]
    assert statusflow._print_retro_capture_status(sources, "dev-a") is True
    out = " ".join(buf.getvalue().split())
    assert "Retro capture: 0 known repositories left to catch up." in out
    assert "Repository discovery is still catching up." in out
    assert "Continue capture on this machine: mm push" in out
    assert "catching up (" not in out


def test_status_and_diag_autorun_breadcrumb_matches_current_upgrade_cache_verdict(
    inspection, monkeypatch
):
    from datetime import datetime, timezone

    from mind_meld import events_tail, host_usage, sidecar, upgrade

    backend, root = inspection
    agy_dir = root.parent / "agy"
    agy_dir.mkdir()
    events_dir = root.parent / "mm-events"
    (events_dir / "events").mkdir(parents=True)

    cfg = config.load_config()
    cfg["sync"]["sources"].extend(
        [
            {"name": "agy", "type": "generic", "path": str(agy_dir), "include_files": []},
            {
                "name": "mm-events",
                "type": "generic",
                "path": str(events_dir),
                "include_dirs": ["events"],
                "exclude_patterns": [],
            },
        ]
    )
    config.save_config(cfg)
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

    monkeypatch.setattr(sidecar, "SIDECAR_DIR", root.parent)
    monkeypatch.setattr(
        host_usage,
        "agy_usage_diag",
        lambda **_kw: {
            "last_reason": "unsupported",
            "complete_once": False,
            "usage_less_skipped": 0,
            "cache_state": "ok",
        },
    )
    monkeypatch.setattr(
        upgrade,
        "cached_upgrade_view",
        lambda _cfg: upgrade.UpgradeCheckResult(
            state="current", local="9.0.0", latest="9.0.0", install_cmd=None
        ),
    )

    no_release_clause = (
        "Antigravity usage not captured: this mm can't read Antigravity's current record format, "
        "and no newer mm can yet. See `mm diag`, or run `mm disable-source agy` to stop retrying."
    )
    status_before = runner.invoke(app, ["status"])
    assert status_before.exit_code == 0, status_before.output
    assert status_before.stdout.splitlines()[0] == f"Usage not captured. {no_release_clause}"

    crumb_detail = events_tail._capture_skip_phrase(
        "agy", "unsupported", attended=False, config=cfg
    )
    (root.parent / "last-autorun.json").write_text(
        json.dumps(
            {
                "push": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "outcome": "degraded",
                    "detail": crumb_detail,
                }
            }
        )
    )

    status_after = runner.invoke(app, ["status"])
    assert status_after.exit_code == 0, status_after.output
    status_out = " ".join(status_after.stdout.split())
    assert f"Usage refresh: {no_release_clause}" in status_out
    assert f"Antigravity usage capture: enabled — {crumb_detail}" in status_out
    assert crumb_detail in status_out
    assert "mm update" not in status_out

    diag_after = runner.invoke(app, ["diag"])
    assert diag_after.exit_code == 0, diag_after.output
    diag_out = " ".join(diag_after.stdout.split())
    assert f"detail: {crumb_detail}" in diag_out
    assert "mm update" not in diag_out


def test_status_reports_pending_syncthing_join_by_count_without_printing_id_or_name(
    inspection, fake_syncthing, monkeypatch
):
    from mind_meld import devices, syncthing
    from mind_meld.errors import StorageError

    def _valid_id(seed_char: str) -> str:
        chunk13 = seed_char * 13
        group14 = chunk13 + syncthing._luhn32(chunk13)
        full56 = group14 * 4
        return "-".join(full56[i : i + 7] for i in range(0, 56, 7))

    backend, _ = inspection
    joining_a = _valid_id("J")
    joining_b = _valid_id("K")
    hostile_name = "hostile-joiner-[bold red]pwn[/bold red]-\x1b]52;c;ZHVtbXk=\x07-\x1b[2J"

    config.save_syncthing_record(fake_syncthing.my_id)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_a: {"name": hostile_name, "address": "192.0.2.11:22000"},
    }
    fake_syncthing.requests.clear()

    res_one = runner.invoke(app, ["status"])
    assert res_one.exit_code == 0, res_one.output
    lines_one = res_one.stdout.splitlines()
    st_lines_one = [line for line in lines_one if line.startswith("  Syncthing:")]
    assert st_lines_one == [
        "  Syncthing: connected 2 of 2 devices; up to date",
        "  Syncthing: 1 device is asking to join. Run: mm devices approve",
    ]
    combined_one = res_one.stdout + res_one.stderr
    assert joining_a not in combined_one
    assert "hostile-joiner" not in combined_one
    assert "\x1b" not in combined_one
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []

    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_a: {"name": hostile_name},
        joining_b: {"name": "linux-b"},
    }
    fake_syncthing.requests.clear()

    res_two = runner.invoke(app, ["status"])
    assert res_two.exit_code == 0, res_two.output
    st_lines_two = [line for line in res_two.stdout.splitlines() if line.startswith("  Syncthing:")]
    assert st_lines_two == [
        "  Syncthing: connected 2 of 2 devices; up to date",
        "  Syncthing: 2 devices are asking to join. Run: mm devices approve",
    ]
    combined_two = res_two.stdout + res_two.stderr
    assert joining_a not in combined_two
    assert joining_b not in combined_two
    assert "hostile-joiner" not in combined_two
    assert "linux-b" not in combined_two
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []

    # Vouched pending device is subtracted from the count (2 pending, 1 vouched -> reports 1)
    cfg = config.load_config()
    core._init_crypto_session(backend, "contract-passphrase", cfg, read_only=True)
    devices.register_device(backend, "peer-a", "mac-b")
    assert devices.publish_record(backend, "peer-a", joining_a, "contract-passphrase", 1024)
    fake_syncthing.requests.clear()

    res_filtered = runner.invoke(app, ["status"])
    assert res_filtered.exit_code == 0, res_filtered.output
    st_lines_filtered = [
        line for line in res_filtered.stdout.splitlines() if line.startswith("  Syncthing:")
    ]
    assert st_lines_filtered == [
        "  Syncthing: connected 2 of 2 devices; up to date",
        "  Syncthing: 1 device is asking to join. Run: mm devices approve",
    ]
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []

    # When records cannot be read, status falls back to the full pending count (2)
    orig_read_fleet_records = statusflow.read_fleet_records

    def _fail_records(*_a, **_kw):
        raise StorageError("records unreadable")

    monkeypatch.setattr(statusflow, "read_fleet_records", _fail_records)
    fake_syncthing.requests.clear()

    res_fallback = runner.invoke(app, ["status"])
    assert res_fallback.exit_code == 0, res_fallback.output
    st_lines_fallback = [
        line for line in res_fallback.stdout.splitlines() if line.startswith("  Syncthing:")
    ]
    assert st_lines_fallback == [
        "  Syncthing: connected 2 of 2 devices; up to date",
        "  Syncthing: 2 devices are asking to join. Run: mm devices approve",
    ]
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []

    # All pending devices vouched -> no pending-join line
    monkeypatch.setattr(statusflow, "read_fleet_records", orig_read_fleet_records)
    devices.register_device(backend, "peer-b", "linux-b")
    assert devices.publish_record(backend, "peer-b", joining_b, "contract-passphrase", 1024)
    fake_syncthing.requests.clear()

    res_all_vouched = runner.invoke(app, ["status"])
    assert res_all_vouched.exit_code == 0, res_all_vouched.output
    st_lines_all_vouched = [
        line for line in res_all_vouched.stdout.splitlines() if line.startswith("  Syncthing:")
    ]
    assert st_lines_all_vouched == ["  Syncthing: connected 2 of 2 devices; up to date"]
    assert "mm devices approve" not in res_all_vouched.stdout + res_all_vouched.stderr
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []

    # Knocking unpublished fleet machine + superseded registration + genuinely new knocker
    import json

    from mind_meld.storage.keys import device_key

    joining_c = _valid_id("L")
    joining_d = _valid_id("M")
    # Unpublished fleet machine "cachyos" whose name matches joining_c
    devices.register_device(backend, "peer-c", "cachyos")
    # Old superseded registration "old11111" for "linux-b" (registered earlier than peer-b)
    backend.put(
        device_key("old11111"),
        json.dumps(
            {
                "device_id": "old11111",
                "device_name": "linux-b",
                "registered": "2026-01-01T00:00:00+00:00",
                "last_seen": "2026-01-02T00:00:00+00:00",
            }
        ).encode("utf-8"),
    )
    backend.put(
        device_key("peer-b"),
        json.dumps(
            {
                "device_id": "peer-b",
                "device_name": "linux-b",
                "registered": "2026-02-01T00:00:00+00:00",
            }
        ).encode("utf-8"),
    )
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_c: {"name": "CachyOS"},
        joining_d: {"name": "unverified-new-box"},
    }
    res_mixed = runner.invoke(app, ["status"])
    assert res_mixed.exit_code == 0, res_mixed.output
    st_lines_mixed = [
        line for line in res_mixed.stdout.splitlines() if line.startswith("  Syncthing:")
    ]
    assert st_lines_mixed == [
        "  Syncthing: connected 2 of 2 devices; up to date",
        (
            "  Syncthing: registered fleet machine cachyos is asking to join and has not "
            "published a Syncthing ID yet; on that machine, run 'mm update', then 'mm push'. "
            "Run: mm update"
        ),
        "  Syncthing: 1 device is asking to join. Run: mm devices approve",
        (
            "  Syncthing: old linux-b registration old11111 superseded by peer-b. "
            "Run: mm devices retire old11111"
        ),
    ]
    assert "unverified-new-box" not in res_mixed.stdout + res_mixed.stderr
    assert joining_c not in res_mixed.stdout + res_mixed.stderr
    assert joining_d not in res_mixed.stdout + res_mixed.stderr

    from mind_meld.storage.keys import syncthing_record_key

    backend.delete(syncthing_record_key("peer-b"))
    unpublished = runner.invoke(app, ["status"])
    assert unpublished.exit_code == 0, unpublished.output
    assert "linux-b, linux-b have not published a Syncthing ID yet" in unpublished.stdout
    assert "superseded by peer-b" not in unpublished.stdout


def test_status_pending_syncthing_join_stays_silent_for_zero_unmanaged_and_errors(
    inspection, fake_syncthing
):
    joining_a = "JOINAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA"

    # 1. Managed machine with 0 pending devices
    config.save_syncthing_record(fake_syncthing.my_id)
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {}
    res_zero = runner.invoke(app, ["status"])
    assert res_zero.exit_code == 0, res_zero.output
    assert [line for line in res_zero.stdout.splitlines() if line.startswith("  Syncthing:")] == [
        "  Syncthing: connected 2 of 2 devices; up to date"
    ]
    assert "mm devices approve" not in res_zero.stdout + res_zero.stderr

    # 2. Managed machine when /rest/cluster/pending/devices errors
    fake_syncthing.responses["/rest/cluster/pending/devices"] = 500
    res_err = runner.invoke(app, ["status"])
    assert res_err.exit_code == 0, res_err.output
    assert [line for line in res_err.stdout.splitlines() if line.startswith("  Syncthing:")] == [
        "  Syncthing: connected 2 of 2 devices; up to date"
    ]
    assert "mm devices approve" not in res_err.stdout + res_err.stderr

    # 3. Owner-managed machine (absent/mismatched record) and malformed syncthing.toml
    fake_syncthing.responses["/rest/cluster/pending/devices"] = {
        joining_a: {"name": "linux-b"},
    }
    record_file = config.syncthing_record_path()
    record_file.unlink()
    fake_syncthing.requests.clear()
    res_unmanaged = runner.invoke(app, ["status"])
    assert res_unmanaged.exit_code == 0, res_unmanaged.output
    assert "mm devices approve" not in res_unmanaged.stdout + res_unmanaged.stderr
    assert "/rest/cluster/pending/devices" not in [req[1] for req in fake_syncthing.requests]

    config.save_syncthing_record(fake_syncthing.peer_id)
    fake_syncthing.requests.clear()
    res_mismatch = runner.invoke(app, ["status"])
    assert res_mismatch.exit_code == 0, res_mismatch.output
    assert "mm devices approve" not in res_mismatch.stdout + res_mismatch.stderr
    assert "/rest/cluster/pending/devices" not in [req[1] for req in fake_syncthing.requests]

    record_file.write_text("not = [valid toml", encoding="utf-8")
    fake_syncthing.requests.clear()
    res_malformed = runner.invoke(app, ["status"])
    assert res_malformed.exit_code == 0, res_malformed.output
    assert "mm devices approve" not in res_malformed.stdout + res_malformed.stderr
    assert "/rest/cluster/pending/devices" not in [req[1] for req in fake_syncthing.requests]

    # 4. Unreachable Syncthing on a managed machine
    config.save_syncthing_record(fake_syncthing.my_id)
    fake_syncthing.close()
    res_down = runner.invoke(app, ["status"])
    assert res_down.exit_code == 0, res_down.output
    assert "mm devices approve" not in res_down.stdout + res_down.stderr
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []


def test_src_mac_phrases_appear_only_in_comments_or_internal_docstrings():
    """Track 149A: user-facing output, CLI --help, and retro_fleet/SKILL.md use
    machine-neutral terms; any match of `this Mac|other Macs|every Mac|another Mac`
    under `src/` lives only in `#` comments or internal docstrings."""
    import ast
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[1] / "src"
    four_phrases = re.compile(r"this Mac|other Macs|every Mac|another Mac")
    user_facing_mac = re.compile(
        r"this\s+Mac|other\s+Macs|every\s+Mac|another\s+Mac|"
        r"\bNo\s+Mac\b|\ba\s+Mac\b|leave\s+the\s+Mac"
    )

    skill_md = (src_root / "mind_meld" / "skills" / "retro_fleet" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert four_phrases.search(skill_md) is None
    assert re.search(r"all my Macs|the right Mac|the rendering Mac", skill_md) is None

    for py_path in sorted(src_root.rglob("*.py")):
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_path))
        internal_doc_nodes: set[int] = set()
        internal_doc_lines: set[int] = set()

        def mark_doc(body, *, is_cli_command: bool = False):
            for idx, stmt in enumerate(body):
                if idx == 0 and is_cli_command:
                    continue
                if (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    doc_node = stmt.value
                    internal_doc_nodes.add(id(doc_node))
                    for lineno in range(
                        doc_node.lineno, (doc_node.end_lineno or doc_node.lineno) + 1
                    ):
                        internal_doc_lines.add(lineno)

        mark_doc(tree.body)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_cmd = py_path.name == "cli.py" and any(
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ("command", "callback")
                    for dec in node.decorator_list
                )
                mark_doc(node.body, is_cli_command=is_cmd)

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in internal_doc_nodes
            ):
                match = user_facing_mac.search(node.value)
                assert match is None, (
                    f"{py_path}:{node.lineno} has user-facing Mac phrase {match.group(0)!r}"
                )

        for lineno, line in enumerate(source.splitlines(), start=1):
            if four_phrases.search(line):
                assert line.lstrip().startswith("#") or lineno in internal_doc_lines, (
                    f"{py_path}:{lineno} matches four-phrase Mac regex outside "
                    f"comment/internal docstring: {line!r}"
                )


def test_status_legacy_attended_removal_wait_renders_until_mesh_record_supersedes(
    inspection, monkeypatch
):
    """Track 156A AC3 & AC7: pre-change attended run records with a wait line still
    render when last-mesh.json is missing, and stop resurrecting the wait once a
    valid last-mesh.json exists."""
    from mind_meld import attemptlog, sidecar

    _, root = inspection
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", root.parent)
    wait_line = (
        "Syncthing device removals wait for linux-c (dev-c) to publish "
        "its Syncthing ID; run 'mm update' and 'mm push' on linux-c"
    )
    legacy_run = attemptlog.make_run(
        outcome="degraded",
        cause="result-degraded",
        degradations=[
            wait_line,
            "the Syncthing record of device peer1 is from a newer mm. Run: mm update",
        ],
        completed_at="2026-11-20T10:00:00+00:00",
    )
    attemptlog.write_run("push", legacy_run)

    # 1. No last-mesh.json yet (right after upgrade): legacy attended wait still renders
    assert attemptlog.read_mesh_waits()["state"] == "missing"
    res_legacy = runner.invoke(app, ["status"])
    assert res_legacy.exit_code == 0, res_legacy.output
    flat_legacy = " ".join(res_legacy.stdout.split())
    assert wait_line in flat_legacy
    assert "the Syncthing record of device peer1 is from a newer mm. Run: mm update" in flat_legacy

    # 2. Valid last-mesh.json with empty waits suppresses the legacy attended wait line
    attemptlog.write_mesh_waits([], completed_at="2026-11-20T10:05:00+00:00")
    res_cleared = runner.invoke(app, ["status"])
    assert res_cleared.exit_code == 0, res_cleared.output
    flat_cleared = " ".join(res_cleared.stdout.split())
    assert "Syncthing device removals wait for" not in flat_cleared
    assert "the Syncthing record of device peer1 is from a newer mm. Run: mm update" in flat_cleared

    # 3. diag --json exposes last_mesh under syncthing
    res_diag = runner.invoke(app, ["diag", "--json"])
    assert res_diag.exit_code == 0, res_diag.output
    assert json.loads(res_diag.stdout)["syncthing"]["last_mesh"] == {
        "state": "valid",
        "completed_at": "2026-11-20T10:05:00+00:00",
        "waits": [],
        "omitted_details": 0,
    }


def test_newer_mesh_record_hides_only_removal_waits_from_a_legacy_hook(inspection, monkeypatch):
    from mind_meld import attemptlog, sidecar

    _, root = inspection
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", root.parent)
    wait = (
        "Syncthing device removals wait for linux; peer (dev-c) to publish "
        "its Syncthing ID; run 'mm update' and 'mm push' on linux; peer"
    )
    before = "2 file(s) failed"
    after = "Syncthing mesh step stopped (timeout). Run: mm diag"
    old_detail = f"{before}; {wait}; {after}"
    core._autorun_breadcrumb_path().write_text(
        json.dumps(
            {
                "pull": {
                    "timestamp": "2026-11-20T10:00:00+00:00",
                    "outcome": "degraded",
                    "detail": old_detail,
                }
            }
        )
    )

    initial = runner.invoke(app, ["status"])
    assert initial.exit_code == 0, initial.output
    assert wait in " ".join(initial.stdout.split())

    attemptlog.write_mesh_waits([], completed_at="2026-11-20T10:05:00+00:00")
    for command in (["status"], ["diag"]):
        cleared = runner.invoke(app, command)
        assert cleared.exit_code == 0, cleared.output
        assert f"{before}; {after}" in " ".join(cleared.stdout.split())
        assert "Syncthing device removals wait for" not in cleared.stdout
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["last_autorun"]["pull"]["detail"] == f"{before}; {after}"
    assert old_detail in core._autorun_breadcrumb_path().read_text()


@pytest.mark.parametrize("unexplained", [0, 1])
def test_cleared_legacy_attended_wait_does_not_mask_other_degradation_counts(
    inspection, monkeypatch, unexplained
):
    # Protects a clear across verbs with historical, production-shaped counts.
    # Fails when the old wait alone keeps status actionable or when clearing
    # a wait also discards an unrelated degradation. The older fixture carries
    # mixed text rather than the aggregate diagnostic count. Seam: none.
    from mind_meld import attemptlog, sidecar

    _, root = inspection
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", root.parent)
    wait = "Syncthing device removals wait for a complete fleet registry read. Run: mm diag"
    legacy = attemptlog.make_run(
        outcome="degraded",
        cause="result-degraded",
        counts={"events_degradations": 1 + unexplained},
        degradations=[wait],
        completed_at="2026-11-20T10:00:00+00:00",
    )
    attemptlog.write_run("pull", legacy)
    attemptlog.write_mesh_waits([], completed_at="2026-11-20T10:05:00+00:00")

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert wait not in status.stdout
    assert ("Latest pull needs attention" in status.stdout.splitlines()[0]) == bool(unexplained)
    assert attemptlog.read_runs()["runs"]["pull"] == legacy


def test_status_and_diag_display_init_notices_until_attended_capture_replaces_them(
    inspection, monkeypatch
):
    """Track 156A AC4 & AC7: no-action init notices display in mm status and mm diag
    when no attended capture exists (or when the attended capture is older than init),
    and stop rendering once an attended capture is recorded at or after init's
    completed_at."""
    from mind_meld import attemptlog, events_tail, sidecar

    _, root = inspection
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", root.parent)

    init_t = "2026-11-20T10:00:00+00:00"
    deadline_phrase = events_tail._host_skip_phrase(
        "codex", "deadline", verb="init", readiness="ready", attended=True
    )
    attemptlog.write_init_notices(
        [
            events_tail._INIT_BACKFILL_BUDGET_NOTICE,
            events_tail._INIT_ROOT_DISCOVERY_NOTICE,
            deadline_phrase,
        ],
        completed_at=init_t,
    )

    # 1. With no attended capture record yet, both status and plain-text diag render the 3 notices
    for cmd in (["status"], ["diag"]):
        res = runner.invoke(app, cmd)
        assert res.exit_code == 0, res.output
        flat = " ".join(res.stdout.split())
        assert events_tail._INIT_BACKFILL_BUDGET_NOTICE in flat
        assert events_tail._INIT_ROOT_DISCOVERY_NOTICE in flat
        assert "host-usage snapshot skipped (codex deadline)" in flat

    diag_json = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
    assert diag_json["host_publication"]["init_notices"]["state"] == "valid"
    assert len(diag_json["host_publication"]["init_notices"]["notices"]) == 3

    # 2. Pre-existing older attended capture record (< init_t): init notices still render
    attemptlog.write(
        attemptlog.CaptureOutcome(attempted_at="2026-11-20T09:55:00+00:00").finish("no-row")
    )
    res_older = runner.invoke(app, ["status"])
    assert res_older.exit_code == 0, res_older.output
    assert events_tail._INIT_BACKFILL_BUDGET_NOTICE in " ".join(res_older.stdout.split())

    # 3. Attended capture at or after init_t replaces the init notices on both status and diag
    attemptlog.write(
        attemptlog.CaptureOutcome(attempted_at="2026-11-20T10:01:00+00:00").finish("published")
    )
    for cmd in (["status"], ["diag"]):
        res_replaced = runner.invoke(app, cmd)
        assert res_replaced.exit_code == 0, res_replaced.output
        flat_replaced = " ".join(res_replaced.stdout.split())
        assert events_tail._INIT_BACKFILL_BUDGET_NOTICE not in flat_replaced
        assert events_tail._INIT_ROOT_DISCOVERY_NOTICE not in flat_replaced
        assert "host-usage snapshot skipped (codex deadline)" not in flat_replaced
    replaced_json = runner.invoke(app, ["diag", "--json"])
    assert replaced_json.exit_code == 0, replaced_json.output
    assert json.loads(replaced_json.stdout)["host_publication"]["init_notices"] == {
        "state": "superseded",
        "completed_at": init_t,
        "notices": [],
        "omitted_details": 0,
    }


def test_status_verdict_syncthing_pending_priority():
    from mind_meld import statusflow

    clean_projection = {
        "pull": {"unknown_reason": None, "unresolved": False, "selected": None},
        "push": {"unknown_reason": None, "unresolved": False, "selected": None},
    }
    base = dict(
        source=None,
        blocking=None,
        conflicts=0,
        projection=clean_projection,
        counts=(0, 0, 0),
        metadata=False,
        first_push=False,
    )
    pending_msg = "Syncthing is still delivering 3 files. Run: mm pull"

    # Empty string keeps today's verdict byte-identical.
    assert statusflow._status_verdict(**base) == "In sync with this device's last push."
    # Pending delivery wins over in-sync and overdue peers.
    assert statusflow._status_verdict(**base, syncthing_pending=pending_msg) == pending_msg
    assert (
        statusflow._status_verdict(
            **base,
            syncthing_pending=pending_msg,
            peers_overdue="Waiting more than 24 h for Mac B's push of 2026-01-01. Run: mm pull",
        )
        == pending_msg
    )
    # Higher-priority verdicts still win over pending delivery.
    assert (
        statusflow._status_verdict(
            **{**base, "blocking": "Blocked. Run: mm diag"}, syncthing_pending=pending_msg
        )
        == "Blocked. Run: mm diag"
    )
    assert (
        statusflow._status_verdict(**{**base, "conflicts": 2}, syncthing_pending=pending_msg)
        == "2 unresolved conflicts. Run: mm resolve"
    )
    assert (
        statusflow._status_verdict(
            **base,
            capture_action="Capture needs attention. Run: mm recapture",
            syncthing_pending=pending_msg,
        )
        == "Capture needs attention. Run: mm recapture"
    )
    assert (
        statusflow._status_verdict(**base, left_out="1 file held", syncthing_pending=pending_msg)
        == "Not everything is published: 1 file held"
    )


def test_status_shows_pending_syncthing_delivery_and_clears_on_catchup(inspection, fake_syncthing):
    from mind_meld import cli

    backend, _ = inspection
    cfg = config.load_config()
    memory_kb = core._init_crypto_session(backend, "contract-passphrase", cfg, read_only=True)
    own = manifest.build_manifest_v2(
        cfg["device"]["id"], cfg["device"]["name"], config.resolve_sources(cfg).available
    )
    backend.put(
        manifest_key(cfg["device"]["id"]),
        crypto.encrypt(manifest.serialize_manifest(own), "contract-passphrase", memory_kb),
    )
    fake_syncthing.set_folder(backend.root, state="syncing", need_files=3, need_bytes=120)
    auto = runner.invoke(cli.app, ["autopull"])
    assert auto.exit_code == 0, auto.output
    assert not auto.stdout and not auto.stderr
    assert (
        "Syncthing is still delivering 3 files."
        in statusflow._read_autorun_evidence()["runs"]["pull"]["detail"]
    )
    fake_syncthing.requests.clear()

    res_pending = runner.invoke(app, ["status"])
    assert res_pending.exit_code == 0, res_pending.output
    assert "Syncthing is still delivering 3 files. Run: mm pull" in res_pending.stdout
    assert "3 files to fetch" in res_pending.stdout
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []

    # Catching up clears the verdict without another pull.
    fake_syncthing.set_folder(backend.root, state="idle", need_files=0, need_bytes=0)
    fake_syncthing.requests.clear()

    res_idle = runner.invoke(app, ["status"])
    assert res_idle.exit_code == 0, res_idle.output
    assert "Syncthing is still delivering" not in res_idle.stdout
    assert "files to fetch" not in res_idle.stdout
    assert "Last auto-pull" in res_idle.stdout
    assert "up to date" in res_idle.stdout
    assert all(req[0] == "GET" for req in fake_syncthing.requests)
    assert fake_syncthing.bodies == []
