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
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import host_usage
from mind_meld import sidecar as sidecar_mod
from mind_meld.cli import app
from mind_meld.config import load_config, save_config
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
    monkeypatch.setattr("mind_meld.cli.DEFAULT_STORAGE_PATH", str(storage))
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
    # `_probe_claude` hardcodes Path.home()/.claude/projects, ignoring the
    # configured source path. Without this, diag reads the developer's
    # real corpus locally and is vacuous on CI.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    return storage, cfg_path, backend


# ── JSON mode ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("newer", [False, True])
def test_crypto_init_newer_version66a(tmp_path, monkeypatch, newer):
    from mind_meld import upgrade

    storage, _, _ = _setup(tmp_path, monkeypatch)
    if newer:
        (storage / "mm-crypto-init 2").write_bytes(b"\x03")
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    ci = json.loads(result.stdout)["crypto_init"]
    assert ci["newer_version"] == (3 if newer else None)
    assert ci["status"] == ("corrupt" if newer else "ok")
    if newer:
        result = runner.invoke(app, ["diag"])
        flat = " ".join(result.output.split())
        assert "newer_version: 3" in flat
        assert upgrade.INSTALL_CMD in flat


def _enable_capture_sources(tmp_path, *, readers):
    cfg = load_config()
    names = {s["name"] for s in cfg["sync"]["sources"]}
    for name in ("mm-events", *readers):
        if name in names:
            continue
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        cfg["sync"]["sources"].append(
            {
                "name": name,
                "path": str(root),
                "type": "grok" if name == "grok" else "generic",
                **({"include_dirs": ["events"]} if name != "grok" else {}),
            }
        )
    save_config(cfg)


@pytest.mark.parametrize(
    "state", ["ready", "disabled", "unavailable", "default-missing", "no-reader", "unknown"]
)
@pytest.mark.parametrize("inventory", ["deadline", "rebuild", "warming", "unsupported"])
@pytest.mark.parametrize("reader", ["codex", "grok"])
def test_capture_remedies_respect_readiness_without_bootstrapping(
    tmp_path, monkeypatch, state, inventory, reader
):
    from mind_meld import config as config_module

    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    _enable_capture_sources(tmp_path, readers=(reader,))
    cfg = load_config()
    root = tmp_path / "mm-events"
    if state in ("unavailable", "default-missing"):
        root = tmp_path / ("missing-custom" if state == "unavailable" else ".local/share/mind-meld")
        next(s for s in cfg["sync"]["sources"] if s["name"] == "mm-events")["path"] = str(root)
    elif state == "disabled":
        cfg["sync"]["disabled_sources"] = ["mm-events"]
    elif state == "no-reader":
        cfg["sync"]["disabled_sources"] = [reader]
    save_config(cfg)
    if state == "unknown":
        config_module.CONFIG_PATH.write_text("[invalid TOML")
    cache_state = {
        "complete_once": False,
        "usage_less_skipped": 0,
        "cache_state": "ok",
        "state": "migrating" if inventory == "warming" else "ready",
        "last_reason": inventory if inventory in ("deadline", "unsupported") else None,
        "files_pre_track": 1 if inventory == "rebuild" else 0,
        "pending": 2,
        "files_cached": 1,
        "files_on_disk": 3,
    }
    monkeypatch.setattr(host_usage, f"{reader}_usage_diag", lambda: cache_state)
    before = root.exists()
    commands = ["diag"] if state == "unknown" else ["status", "diag"]
    expected = "ready" if state == "default-missing" else state
    for command in commands:
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        if expected == "ready":
            assert ("pipx upgrade mind-meld" if inventory == "unsupported" else "mm push") in flat
            if inventory == "unsupported":
                assert "mm push" not in flat
        else:
            assert "mm push" not in flat
            if expected == "disabled":
                assert "mm enable-source mm-events" in flat
            elif expected == "unavailable":
                assert "Restore access to the configured mm-events folder" in flat
                assert "mm enable-source mm-events" not in flat
                assert "mm disable-source mm-events" not in flat
            elif expected == "unknown":
                assert "mm enable-source mm-events" not in flat
                assert "readiness is unknown" in flat
            elif expected == "no-reader":
                assert "No host reader is consented" in flat
                assert "Usage capture unavailable" in flat
                assert "mm enable-source codex" in flat
                assert "mm enable-source grok" in flat
        assert root.exists() == before
    diag = runner.invoke(app, ["diag", "--json"])
    assert diag.exit_code == 0, diag.output
    assert json.loads(diag.stdout)["host_publication"]["readiness"] == expected
    if not before:
        assert not root.parent.exists() if state == "default-missing" else not root.exists()


@pytest.mark.parametrize("other", ["deadline", "rebuild", "warming"])
def test_unsupported_reader_makes_all_ready_remedies_consistent(tmp_path, monkeypatch, other):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    _enable_capture_sources(tmp_path, readers=("codex", "grok"))
    monkeypatch.setattr(
        host_usage,
        "grok_usage_diag",
        lambda: {
            "last_reason": "unsupported",
            "complete_once": False,
            "usage_less_skipped": 0,
            "cache_state": "ok",
        },
    )
    monkeypatch.setattr(
        host_usage,
        "codex_usage_diag",
        lambda: {
            "last_reason": "deadline" if other == "deadline" else None,
            "state": "migrating" if other == "warming" else "ready",
            "files_pre_track": int(other == "rebuild"),
        },
    )
    for command in ("status", "diag"):
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "pipx upgrade mind-meld" in flat
        assert "mm push" not in flat


@pytest.mark.parametrize("reader", ["codex", "grok"])
def test_unconsented_cached_reader_does_not_deny_siblings_consent(tmp_path, monkeypatch, reader):
    _setup(tmp_path, monkeypatch)
    _enable_capture_sources(tmp_path, readers=("grok" if reader == "codex" else "codex",))
    monkeypatch.setattr(
        host_usage,
        f"{reader}_usage_diag",
        lambda: {
            "last_reason": "deadline",
            "complete_once": False,
            "usage_less_skipped": 0,
            "cache_state": "ok",
            "files_pre_track": 1,
        },
    )
    result = runner.invoke(app, ["diag"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "No host reader is consented" not in flat
    assert f"{reader} usage reading is not consented" in flat
    assert f"Run mm enable-source {reader}" in flat


@pytest.mark.parametrize(
    "selected,available,consented,expected",
    [
        (None, [], True, "unknown"),
        ([], [], True, "disabled"),
        ([{"name": "mm-events"}], [], True, "unavailable"),
        ([{"name": "mm-events"}], [{"name": "mm-events"}], False, "no-reader"),
        ([{"name": "mm-events"}], [{"name": "mm-events"}], True, "ready"),
    ],
)
def test_capture_readiness_is_pure(selected, available, consented, expected, monkeypatch):
    from mind_meld import config

    monkeypatch.setattr(config, "resolve_sources", lambda *a, **kw: pytest.fail("resolution"))
    assert (
        config.usage_capture_readiness(selected, available, reader_consented=consented) == expected
    )


@pytest.mark.parametrize(
    "mode", ["absent", "stale", "empty", "degraded", "unreadable", "root-unreadable"]
)
def test_host_publication_states_on_both_surfaces(tmp_path, monkeypatch, mode):
    import builtins
    from datetime import datetime, timedelta, timezone

    from mind_meld import events

    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    root = tmp_path / "mm-events"
    daydir = root / "events"
    daydir.mkdir(parents=True)
    codex = tmp_path / "codex"
    codex.mkdir()
    cfg = load_config()
    cfg["sync"]["sources"] = [
        {"name": "mm-events", "path": str(root), "type": "generic", "include_dirs": ["events"]},
        {"name": "codex", "path": str(codex), "type": "generic"},
    ]
    cfg["retro"] = {"grok_host_usage": True}
    save_config(cfg)
    now = datetime.now(timezone.utc)
    ts = now - timedelta(days=5) if mode in ("stale", "unreadable") else now
    if mode != "absent":
        row = events.make_host_usage_snapshot(
            device="not-trusted",
            hosts={},
            ts=ts,
            token_sources=["grok"] if mode == "degraded" else ["codex", "grok"],
            degraded_sources=["codex"] if mode == "degraded" else [],
        )
        (daydir / f"mac-a-{ts.date()}.jsonl").write_text(json.dumps(row) + "\n")
    if mode == "unreadable":
        current = daydir / f"mac-a-{now.date()}.jsonl"
        real = builtins.open

        def read(path, *args, **kwargs):
            if Path(path) == current:
                raise PermissionError("denied")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", read)
    if mode == "root-unreadable":
        real_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == daydir:
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
    diag = runner.invoke(app, ["diag", "--json"])
    assert diag.exit_code == 0, diag.output
    pub = json.loads(diag.stdout)["host_publication"]
    assert pub["publication"] == pub["latest_attempt"] == "unknown"
    assert pub["state"] == (
        "absent" if mode == "absent" else "unknown" if "unreadable" in mode else "recorded"
    )
    if mode == "empty":
        assert pub["empty"] is True
        assert pub["readers"]["codex"] == "contributed"
    if mode == "stale":
        assert pub["age_days"] == 5
    if mode == "degraded":
        assert pub["readers"]["codex"] == "degraded"
    for command in (["status"], ["diag"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "Host usage last recorded capture" in flat
        assert "Last recorded attended attempt: unknown" in flat
        assert flat.index("Host usage last recorded capture") < flat.index(
            "Grok usage capture" if command == ["status"] else "grok consented"
        )
        if mode == "empty":
            assert "completed, no usage" in flat
        if mode != "empty":
            assert "mm push" in flat
        if mode == "absent":
            assert "no capture in the last 90 days" in flat
        if "unreadable" in mode:
            assert "~/mm-events/events" in flat


def test_host_publication_config_unknown(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, with_config=False)
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    pub = json.loads(result.stdout)["host_publication"]
    assert pub["state"] == "unknown"
    assert "config invalid" in pub["error"]


def test_deeply_nested_sidecar_does_not_crash_status_or_diag(tmp_path, monkeypatch):
    """`_host_publication` reads the sidecar to check publication evidence.
    `sidecar.read()` only catches (OSError, ManifestError) around
    `json.loads` — a deeply nested JSON document raises `RecursionError`
    directly, which is neither. Must degrade (no manifest evidence), not
    crash a diagnostic command."""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    sidecar_mod.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    sidecar_mod.sidecar_path().write_text("[" * 10_000 + "]" * 10_000)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    diag = runner.invoke(app, ["diag", "--json"])
    assert diag.exit_code == 0, diag.output


def test_status_opens_each_day_once_even_without_host_rows(tmp_path, monkeypatch):
    import builtins
    from datetime import datetime, timedelta, timezone

    from mind_meld import events

    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    root = tmp_path / "mm-events"
    (root / "events").mkdir(parents=True)
    cfg = load_config()
    cfg["sync"]["sources"] = [
        {"name": "mm-events", "type": "generic", "path": str(root), "include_dirs": ["events"]}
    ]
    save_config(cfg)
    now = datetime.now(timezone.utc)
    paths = []
    for delta in range(3):
        ts = now - timedelta(days=delta)
        path = root / "events" / f"mac-a-{ts.date()}.jsonl"
        path.write_text(
            json.dumps(events.make_mm_push_event(device="mac-a", mm_version="1", ts=ts)) + "\n"
        )
        paths.append(path)
    opened = []
    real = builtins.open

    def read(path, *args, **kwargs):
        if Path(path) in paths:
            opened.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", read)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert sorted(opened) == sorted(paths)
    assert "Refresh on this Mac" not in result.output


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
        "host_skill_discovery",
        "host_usage",
        "host_read_budgets",
        "host_publication",
        "discovery",
        "git_capture",
    ):
        assert section in payload, f"missing {section}"
    grok_hu = payload["host_usage"]["grok"]
    assert set(grok_hu) == {
        "consented",
        "complete_once",
        "usage_less_skipped",
        "last_reason",
        "last_reason_since",
        "last_complete_ms",
        "last_complete_at",
        "last_deadline_allotted_ms",
        "cache_state",
        "model_count",
        "models",
        "files_cached",
        "files_on_disk",
    }

    assert set(payload["host_usage"]["codex"]) == {
        "cache_state",
        "state",
        "files_cached",
        "files_migrated",
        "files_pre_track",
        "files_on_disk",
        "pending",
        "model_count",
        "models",
        "last_reason",
        "last_reason_since",
        "last_complete_ms",
        "last_complete_at",
        "last_deadline_allotted_ms",
    }


@pytest.mark.parametrize("reader", ["codex", "grok"])
@pytest.mark.parametrize(
    "reason,since,version,expected_reason,expected_since",
    [
        ("unsupported", "2026-09-04T01:00:00+02:00", 1, "unsupported", "2026-09-03T23:00:00+00:00"),
        ("deadline", None, 1, "deadline", None),
        ("malformed", "invalid", 1, "malformed", None),
        ("io_error", "2026-09-04T01:00:00", 1, "io_error", None),
        ("stale", "\x1b[31m\n" * 2000, 1, "stale", None),
        ("partial", "\ud800", 1, "partial", None),
        (None, "2026-09-04T01:00:00Z", 1, None, None),
        ("locked", "2026-09-04T01:00:00Z", 1, None, None),
        ("no_metadata_ledger", "2026-09-04T01:00:00Z", 1, None, None),
        ("unsupported\nforged", None, 1, None, None),
        ({"bad": "reason"}, None, 1, None, None),
        ("unsupported", "2026-09-04T01:00:00Z", 2, None, None),
    ],
)
def test_diag_blocker_fields_are_validated_and_rendered(
    tmp_path, monkeypatch, reader, reason, since, version, expected_reason, expected_since
):
    _setup(tmp_path, monkeypatch)
    _enable_capture_sources(tmp_path, readers=(reader,))
    cache = host_usage.CACHE_PATH if reader == "codex" else host_usage.GROK_CACHE_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "version": version,
                "files": {},
                "complete_once": True,
                "last_reason": reason,
                "last_reason_since": since,
            }
        )
    )
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    state = json.loads(result.stdout)["host_usage"][reader]
    assert state["last_reason"] == expected_reason
    assert state["last_reason_since"] == expected_since
    plain = runner.invoke(app, ["diag"])
    assert plain.exit_code == 0, plain.output
    text = " ".join(plain.output.split())
    assert f"{reader} cache inventory:" in text
    if reader == "grok" and version != host_usage.CACHE_VERSION:
        assert "grok ledgers cached: unknown of" in text
        assert "grok usage read blocker:" not in text
        return
    assert f"{reader} usage read blocker:" in text
    if expected_reason is None:
        assert f"{reader} usage read blocker: none" in text
    else:
        assert f"({reader} {expected_reason})" in text
    if expected_reason == "unsupported":
        assert "pipx upgrade mind-meld" in text
    if expected_reason == "deadline":
        assert "Last read allowed unknown ms; last complete read unknown ms" in text
    if expected_since:
        assert "(first observed 2026-09-03 UTC)" in text
    else:
        assert "first observed" not in text
    assert "\x1b" not in plain.output


@pytest.mark.parametrize("reader", ["codex", "grok"])
@pytest.mark.parametrize("state", ["missing", "unreadable"])
def test_diag_omits_blocker_line_when_cache_is_not_ok(tmp_path, monkeypatch, reader, state):
    _setup(tmp_path, monkeypatch)
    cache = host_usage.CACHE_PATH if reader == "codex" else host_usage.GROK_CACHE_PATH
    if state == "unreadable":
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{broken")
    result = runner.invoke(app, ["diag"])
    assert result.exit_code == 0, result.output
    assert f"{reader} usage read blocker" not in result.output
    assert "blocker: none" not in result.output


@pytest.mark.parametrize("reader", ["codex", "grok"])
def test_diag_legacy_root_emits_null_blocker_fields(tmp_path, monkeypatch, reader):
    _setup(tmp_path, monkeypatch)
    cache = host_usage.CACHE_PATH if reader == "codex" else host_usage.GROK_CACHE_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"version": 1, "files": {}}))
    before = cache.read_bytes()
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    state = json.loads(result.stdout)["host_usage"][reader]
    assert state["last_reason"] is None
    assert state["last_reason_since"] is None
    assert cache.read_bytes() == before


def test_diag_host_usage_does_not_open_the_host_store(tmp_path, monkeypatch):
    """Diag counts ledger paths but never opens their contents."""
    _setup(tmp_path, monkeypatch)
    ledger = tmp_path / "grok-sessions" / "workspace" / "session" / "updates.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("not a log to parse")
    monkeypatch.setattr(host_usage, "GROK_SESSIONS_PATH", ledger.parents[2])
    real_open = Path.open

    def checked_open(path, *args, **kwargs):
        assert path != ledger, "diag must not open a host log"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["host_usage"]["grok"]["cache_state"] in {"missing", "ok", "unreadable"}
    assert payload["host_usage"]["grok"]["files_on_disk"] == 1
    grok = payload["host_usage"]["grok"]
    assert "model_count" in grok
    assert isinstance(grok["models"], list)
    assert "model_count" in payload["host_usage"]["codex"]


@pytest.mark.parametrize("files", [None, {}, {"one": {}}])
def test_diag_grok_ledger_count_replaces_duplicate_cache_line(tmp_path, monkeypatch, files):
    _setup(tmp_path, monkeypatch)
    if files is not None:
        cache = host_usage.GROK_CACHE_PATH
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"version": host_usage.CACHE_VERSION, "files": files}))
    result = runner.invoke(app, ["diag"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    count = "unknown" if files is None else str(len(files))
    assert f"grok ledgers cached: {count} of 0" in text
    assert "grok cache:" not in text


def test_diag_reports_cached_grok_usage_less_tally(tmp_path, monkeypatch):
    """T2-10: diag is a cache-only view, including a partial scan's tally."""
    _setup(tmp_path, monkeypatch)
    host_usage.GROK_CACHE_PATH.parent.mkdir(parents=True)
    host_usage.GROK_CACHE_PATH.write_text(
        json.dumps(
            {
                "version": host_usage.CACHE_VERSION,
                "complete_once": False,
                "usage_less_skipped": 3,
                "files": {},
            }
        ),
        encoding="utf-8",
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("diag must not open the Grok host store")

    monkeypatch.setattr("mind_meld.host_usage.read_grok_usage", boom)
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["host_usage"]["grok"]["usage_less_skipped"] == 3
    assert payload["host_usage"]["grok"]["last_reason"] is None
    assert "grok usage read blocker" not in result.output


def test_diag_renders_persisted_last_reason(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _enable_capture_sources(tmp_path, readers=("grok",))
    host_usage.GROK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    host_usage.GROK_CACHE_PATH.write_text(
        json.dumps(
            {
                "version": host_usage.CACHE_VERSION,
                "complete_once": False,
                "usage_less_skipped": 0,
                "last_reason": "unsupported",
                "files": {},
            }
        ),
        encoding="utf-8",
    )
    json_result = runner.invoke(app, ["diag", "--json"])
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.stdout)
    assert payload["host_usage"]["grok"]["last_reason"] == "unsupported"
    plain = runner.invoke(app, ["diag"])
    assert plain.exit_code == 0, plain.output
    assert "grok usage read blocker" in plain.output
    assert "pipx upgrade mind-meld" in plain.output


def test_diag_grok_consent_matches_an_auto_detected_resolved_source(tmp_path, monkeypatch):
    """Legacy auto-detection is a real source gate, not merely a config hint."""
    _, cfg_path, _ = _setup(tmp_path, monkeypatch)
    cfg = load_config(cfg_path)
    cfg["sync"].pop("sources")
    save_config(cfg, cfg_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".grok" / "skills").mkdir(parents=True)

    result = runner.invoke(app, ["diag", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["host_usage"]["grok"]["consented"] is True


def test_diag_grok_consent_excludes_an_unresolved_explicit_source(tmp_path, monkeypatch):
    """A config entry filtered out by get_sources() cannot authorize a read."""
    _, cfg_path, _ = _setup(tmp_path, monkeypatch)
    cfg = load_config(cfg_path)
    cfg["sync"]["sources"].append(
        {"name": "grok", "path": str(tmp_path / "missing-grok"), "type": "grok"}
    )
    save_config(cfg, cfg_path)

    result = runner.invoke(app, ["diag", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["host_usage"]["grok"]["consented"] is False


def test_diag_plain_text_names_cached_models(tmp_path, monkeypatch):
    """v0.12.49 promises "local per-model counts on mm diag". The plain-text
    block is the surface a user pastes into support; a JSON-only field does
    not discharge that."""
    _setup(tmp_path, monkeypatch)
    host_usage.GROK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    host_usage.GROK_CACHE_PATH.write_text(
        json.dumps(
            {
                "version": host_usage.CACHE_VERSION,
                "complete_once": True,
                "usage_less_skipped": 0,
                "files": {
                    "a": {
                        "dev": 1,
                        "ino": 2,
                        "size": 3,
                        "mtime_ns": 4,
                        "head_len": 0,
                        "tail_len": 0,
                        "offset": 3,
                        "head": "",
                        "tail": "",
                        "turns": [
                            {
                                "key": "k" * 64,
                                "day": "2026-08-15",
                                "model": "grok-4",
                                "usage": {
                                    "input": 1,
                                    "cache_create": 0,
                                    "cache_read": 0,
                                    "output": 1,
                                },
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["diag"])

    assert result.exit_code == 0, result.output
    assert "grok models cached:" in result.output
    assert "grok-4" in result.output
    assert "codex models cached:" in result.output


def test_diag_models_line_branches():
    """`_diag_models_line` is peer-log-derived text on a support surface.

    The COUNT stays exact while the list truncates, so a capped render can
    never be read as the whole set. Non-int / non-list come straight off a
    JSON cache a host wrote, so they are shapes, not hypotheticals.
    """
    from mind_meld.cli import _DIAG_MODELS_SHOWN, _diag_models_line

    assert _diag_models_line({}) == "0"
    assert _diag_models_line({"model_count": 0, "models": []}) == "0"
    assert (
        _diag_models_line({"model_count": 2, "models": ["gpt-5", "grok-4"]}) == "2 (gpt-5, grok-4)"
    )

    n = _DIAG_MODELS_SHOWN + 3
    line = _diag_models_line({"model_count": n, "models": [f"m{i}" for i in range(n)]})
    assert line.startswith(f"{n} (")
    assert "+3 more" in line
    assert line.count(",") == _DIAG_MODELS_SHOWN  # 5 separators + the "+N more" comma

    # Peer-written cache shapes: never raise, never render a bogus count.
    assert _diag_models_line({"model_count": None, "models": None}) == "0"
    assert _diag_models_line({"model_count": True, "models": ["x"]}) == "0"
    assert _diag_models_line({"model_count": 1, "models": "not-a-list"}) == "1"
    # Terminal escapes are stripped, not passed through to the console.
    assert "\x1b" not in _diag_models_line({"model_count": 1, "models": ["a\x1b[31mb"]})
    # A newline would forge an extra field into a block users paste into
    # support chats; `safe_str` alone leaves it. Found by Codex adversarial
    # review. Same whitelist `aggregator._safe_short` applies to the same
    # class of string — the two surfaces must not disagree.
    forged = _diag_models_line({"model_count": 1, "models": ["gpt-5\ngrok cache:      ok"]})
    assert "\n" not in forged
    assert "\r" not in forged


def test_diag_json_reports_cached_codex_models(tmp_path, monkeypatch):
    """The Codex half of the model diag, with a populated cache.

    `_diag_model_ids` reads the interned `models` table v0.12.48 already
    wrote, which is what makes this field free of a re-walk.
    """
    _setup(tmp_path, monkeypatch)
    host_usage.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    host_usage.CACHE_PATH.write_text(
        json.dumps(
            {
                "version": host_usage.CACHE_VERSION,
                "files": {
                    "a": {"models": ["gpt-5-codex", "gpt-5"], "states": []},
                    "b": {"models": ["gpt-5"], "states": []},
                    "c": {"no_ledger": True},
                    "d": "not-a-dict",
                },
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["diag", "--json"])

    assert result.exit_code == 0, result.output
    codex = json.loads(result.stdout)["host_usage"]["codex"]
    assert codex["model_count"] == 2, "deduped across files"
    assert codex["models"] == ["gpt-5", "gpt-5-codex"], "sorted, not insertion order"


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
    assert "Host skill discovery" in result.stdout
    assert "Host usage" in result.stdout
    assert "grok prior successful scan" in result.stdout
    assert "grok last scan" not in result.stdout
    assert "Git-root discovery" in result.stdout
    assert "Git capture" in result.stdout
    assert "recorded" in result.stdout
    assert "fresh" in result.stdout


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
        "local_emails",
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
    for banned in ("master_key", "keycheck", "passphrase", "peer-cafebabe", "local_emails"):
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
    storage, _cfg_path, _backend = _setup(tmp_path, monkeypatch, with_config=False)
    observed_roots = []

    def isolated_backend(root):
        root = Path(root)
        observed_roots.append(root)
        assert root == storage
        return LocalBackend(root)

    monkeypatch.setattr("mind_meld.cli.LocalBackend", isolated_backend)
    # The cfg_path monkeypatch points at a non-existent file.
    result = runner.invoke(app, ["diag", "--json"])
    # Exit 0 — diag must be robust to config failures.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["config"]["state"].startswith("error")
    assert payload["host_usage"]["grok"]["consented"] is None
    assert observed_roots == [storage]


def test_diag_handles_unresolvable_explicit_source(tmp_path, monkeypatch):
    """Source resolution failures are config errors, never a diag crash."""
    storage, cfg_path, _backend = _setup(tmp_path, monkeypatch)
    loop = tmp_path / "source-loop"
    loop.symlink_to(loop)
    save_config(
        {
            "device": {"id": "mac-a", "name": "Mac A"},
            "storage": {"path": str(storage)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [{"name": "claude", "path": str(loop), "type": "claude"}],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        cfg_path,
    )

    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["config"]["state"].startswith("error: config: failed to resolve")
    assert all(
        row["maintain_links"].startswith("unknown (config invalid:")
        for row in payload["skill_links"]
    )
    assert "host_skill_discovery" in payload
    assert payload["host_skill_discovery"].get("host") == "grok"
    assert "host_usage" in payload
    assert payload["host_usage"]["grok"]["consented"] is None
    assert all("claude_skills_compat" not in row for row in payload["skill_links"])


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


# ── Discovery (Track 29A) ────────────────────────────────────────────────


class TestDiagDiscovery:
    def test_discovery_block_present_in_text_and_json(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        text = runner.invoke(app, ["diag"])
        assert text.exit_code == 0, text.output
        assert "Git-root discovery" in text.stdout
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        assert "discovery" in payload
        assert payload["discovery"]["budget_ms"] == 50

    def test_discovery_runs_at_the_autopush_budget(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        captured: list[float] = []
        from mind_meld import events as events_mod

        real = events_mod.discover_git_roots

        def spy(config, *, deadline_monotonic=None):
            captured.append(deadline_monotonic)
            return real(config, deadline_monotonic=deadline_monotonic)

        monkeypatch.setattr(events_mod, "discover_git_roots", spy)
        monkeypatch.setattr("mind_meld.cli.events.discover_git_roots", spy)
        import time as time_mod

        monkeypatch.setattr(time_mod, "monotonic", lambda: 1000.0)
        monkeypatch.setattr("mind_meld.cli.time.monotonic", lambda: 1000.0)
        runner.invoke(app, ["diag", "--json"])
        assert captured, "discover_git_roots was not called"
        remaining = captured[0] - 1000.0
        assert remaining == pytest.approx(0.050)

    def test_rejects_are_counts_by_reason_not_paths(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        from mind_meld.events import GitRootDiscovery

        dead = tmp_path / "dead-workspace"
        monkeypatch.setattr(
            "mind_meld.cli.events.discover_git_roots",
            lambda _cfg, **_kw: GitRootDiscovery(
                (),
                (),
                False,
                (),
                (("gone", dead),),
                (("gone", 1),),
                ("claude",),
            ),
        )
        text = runner.invoke(app, ["diag"])
        assert text.exit_code == 0, text.output
        assert "1 gone" in text.stdout
        assert str(dead) not in text.stdout

    def test_full_paths_only_in_json(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        from mind_meld.events import GitRootDiscovery

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "mind_meld.cli.events.discover_git_roots",
            lambda _cfg, **_kw: GitRootDiscovery(
                (repo,),
                (),
                False,
                (("claude", repo),),
                (),
                (),
                ("claude",),
            ),
        )
        text = runner.invoke(app, ["diag"])
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        assert payload["discovery"]["roots"] == [str(repo)]
        # Text uses ~-relative; the raw tmp_path absolute must not be required.
        assert "rejects:" in text.stdout

    def test_no_prober_ran_is_distinct_from_found_nothing(self, tmp_path, monkeypatch):
        storage, cfg_path, _backend = _setup(tmp_path, monkeypatch)
        save_config(
            {
                "device": {"id": "mac-a", "name": "Mac A"},
                "storage": {"path": str(storage)},
                "sync": {
                    "max_file_size": 52_428_800,
                    "sources": [
                        {
                            "name": "gstack",
                            "path": str(tmp_path / "gstack"),
                            "type": "generic",
                            "include_dirs": ["projects"],
                        }
                    ],
                },
                "crypto": {"argon2_memory_kb": MEMORY_KB},
            },
            cfg_path,
        )
        (tmp_path / "gstack" / "projects").mkdir(parents=True)
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        assert payload["discovery"]["status"] == "no-prober"
        assert payload["discovery"]["probers_ran"] == []
        text = runner.invoke(app, ["diag"])
        assert "none ran" in text.stdout

        monkeypatch.setattr(
            "mind_meld.cli.events.discover_git_roots",
            lambda _cfg, **_kw: __import__(
                "mind_meld.events", fromlist=["GitRootDiscovery"]
            ).GitRootDiscovery((), (), False, (), (), (), ("claude",)),
        )
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        assert payload["discovery"]["status"] == "empty"
        assert payload["discovery"]["probers_ran"] == ["claude"]

    def test_exceeded_discovery_is_not_reported_as_no_prober(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        from mind_meld.events import GIT_ROOT_DISCOVERY_BUDGET_ERROR, GitRootDiscovery

        monkeypatch.setattr(
            "mind_meld.cli.events.discover_git_roots",
            lambda _cfg, **_kw: GitRootDiscovery((), (GIT_ROOT_DISCOVERY_BUDGET_ERROR,), True),
        )
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        assert payload["discovery"]["status"] == "exceeded"
        assert payload["discovery"]["probers_ran"] == []

    def test_discovery_paths_pass_through_safe_str(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        from mind_meld.events import GitRootDiscovery

        nasty = tmp_path / "[red]boom"
        nasty.mkdir()
        monkeypatch.setattr(
            "mind_meld.cli.events.discover_git_roots",
            lambda _cfg, **_kw: GitRootDiscovery(
                (nasty,),
                (),
                False,
                (("claude", nasty),),
                (),
                (),
                ("claude",),
            ),
        )
        text = runner.invoke(app, ["diag"])
        assert text.exit_code == 0, text.output
        assert "[red]boom" in text.stdout or "boom" in text.stdout
        # Rich must not interpret the markup as a style close.
        assert "\\[red\\]boom" in text.stdout or "boom" in text.stdout

    def test_diag_does_not_read_real_home_under_pytest(self, tmp_path, monkeypatch):
        real_home = Path.home()
        _setup(tmp_path, monkeypatch)
        assert Path.home() == tmp_path
        assert Path.home() != real_home
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        real_projects = str(real_home / ".claude" / "projects")
        for raw in payload["discovery"]["roots"]:
            assert not str(raw).startswith(real_projects)

    def test_reject_sample_is_capped(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        from mind_meld import events as events_mod
        from mind_meld.events import GitRootDiscovery

        sample = tuple(("gone", tmp_path / f"dead{i}") for i in range(80))
        monkeypatch.setattr(
            "mind_meld.cli.events.discover_git_roots",
            lambda _cfg, **_kw: GitRootDiscovery(
                (),
                (),
                False,
                (),
                sample[: events_mod.MAX_DISCOVERY_REJECT_SAMPLE],
                (("gone", 80),),
                ("claude",),
            ),
        )
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        sample = payload["discovery"]["rejects"]["sample"]
        assert len(sample) == events_mod.MAX_DISCOVERY_REJECT_SAMPLE
        assert payload["discovery"]["rejects"]["counts"]["gone"] == 80

    def test_discovery_runs_once_not_twice(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        calls = {"n": 0}
        from mind_meld.events import GitRootDiscovery

        def spy(_cfg, **_kw):
            calls["n"] += 1
            return GitRootDiscovery((), (), False, (), (), (), ("claude",))

        monkeypatch.setattr("mind_meld.cli.events.discover_git_roots", spy)
        runner.invoke(app, ["diag", "--json"])
        assert calls["n"] == 1
        calls["n"] = 0
        runner.invoke(app, ["diag"])
        assert calls["n"] == 1


class TestDiagGitCapture:
    def test_recorded_and_fresh_differ(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        from mind_meld.config import load_config, save_config

        _setup(tmp_path, monkeypatch)
        events_root = tmp_path / "mm-events"
        (events_root / "events").mkdir(parents=True)
        cfg = load_config()
        cfg["sync"]["sources"].append(
            {
                "name": "mm-events",
                "type": "generic",
                "path": str(events_root),
                "include_dirs": ["events"],
                "exclude_patterns": [],
            }
        )
        save_config(cfg)
        ts = datetime.now(timezone.utc)
        row = {
            "type": "mm-push",
            "ts": ts.isoformat(),
            "mm_version": "0.12.45",
            "local_emails": ["secret@example.com"],
            "git_capture": {
                "since": ts.isoformat(),
                "discovery": "partial",
                "walk_budget_aborts": 1,
                "walk_errors": 0,
            },
        }
        (events_root / "events" / f"mac-a-{ts.date().isoformat()}.jsonl").write_text(
            json.dumps(row) + "\n"
        )
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        cap = payload["git_capture"]
        assert cap["recorded"]["discovery"] == "partial"
        assert cap["recorded"]["mm_version"] == "0.12.45"
        assert cap["recorded"]["walk_budget_aborts"] == 1
        assert cap["fresh"]["discovery"] in ("complete", "empty", "not-run", "partial")
        assert (
            cap["fresh"]["discovery"] != cap["recorded"]["discovery"] or cap["fresh"]["roots"] != 0
        )
        text = runner.invoke(app, ["diag"]).stdout
        assert "recorded" in text
        assert "fresh" in text
        assert "secret@example.com" not in text
        assert "local_emails" not in json.dumps(payload)

    def test_legacy_row_without_git_capture(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        from mind_meld.config import load_config, save_config

        _setup(tmp_path, monkeypatch)
        events_root = tmp_path / "mm-events"
        (events_root / "events").mkdir(parents=True)
        cfg = load_config()
        cfg["sync"]["sources"].append(
            {
                "name": "mm-events",
                "type": "generic",
                "path": str(events_root),
                "include_dirs": ["events"],
                "exclude_patterns": [],
            }
        )
        save_config(cfg)
        ts = datetime.now(timezone.utc)
        row = {"type": "mm-push", "ts": ts.isoformat(), "mm_version": "0.12.44"}
        (events_root / "events" / f"mac-a-{ts.date().isoformat()}.jsonl").write_text(
            json.dumps(row) + "\n"
        )
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        rec = payload["git_capture"]["recorded"]
        assert rec["discovery"] is None
        assert rec["advances_cursor"] is True

    def test_peer_text_is_sanitized_and_clamped(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        from mind_meld.config import load_config, save_config

        _setup(tmp_path, monkeypatch)
        events_root = tmp_path / "mm-events"
        (events_root / "events").mkdir(parents=True)
        cfg = load_config()
        cfg["sync"]["sources"].append(
            {
                "name": "mm-events",
                "type": "generic",
                "path": str(events_root),
                "include_dirs": ["events"],
                "exclude_patterns": [],
            }
        )
        save_config(cfg)
        ts = datetime.now(timezone.utc)
        row = {
            "type": "mm-push",
            "ts": ts.isoformat(),
            "mm_version": "\x1b[31m" + ("v" * 200),
            "git_capture": {"discovery": "complete", "since": ts.isoformat()},
        }
        (events_root / "events" / f"mac-a-{ts.date().isoformat()}.jsonl").write_text(
            json.dumps(row) + "\n"
        )
        payload = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)
        ver = payload["git_capture"]["recorded"]["mm_version"]
        assert "\x1b" not in ver
        assert len(ver) <= 128


@pytest.mark.parametrize(
    "case,total,margin",
    [
        ("both", 215, "35 ms spare"),
        ("over", 268, "over by 18 ms"),
        ("codex-only", 197, "53 ms spare"),
        ("grok-only", 18, "232 ms spare"),
        ("missing-read", None, "a reader has no complete read"),
        ("none", None, "no reader enabled"),
    ],
)
def test_diag_host_read_sweep_uses_only_resolved_consented_readers(
    tmp_path, monkeypatch, case, total, margin
):
    from datetime import datetime, timedelta, timezone

    from mind_meld import cli, events_tail

    _setup(tmp_path, monkeypatch)
    config = load_config()
    codex = tmp_path / "codex"
    codex.mkdir()
    config["sync"]["sources"] = (
        [{"name": "codex", "path": str(codex), "type": "generic"}]
        if case not in {"none", "grok-only"}
        else []
    )
    config["retro"] = {
        "grok_host_usage": case not in {"none", "codex-only"},
        "host_usage_interactive_budget_ms": 500,
    }
    save_config(config)
    _enable_capture_sources(tmp_path, readers=())
    at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    for reader, ms in (("codex", 250 if case == "over" else 197), ("grok", 18)):
        path = host_usage.CACHE_PATH if reader == "codex" else host_usage.GROK_CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "files": {},
                    "last_complete_ms": None if case == "missing-read" and reader == "grok" else ms,
                    "last_complete_at": at,
                    "last_reason": "deadline",
                    "last_deadline_allotted_ms": 250,
                }
            )
        )

    def never_read(*args, **kwargs):
        pytest.fail("diag must not read host logs")

    monkeypatch.setattr(host_usage, "read_codex_usage", never_read)
    monkeypatch.setattr(host_usage, "read_grok_usage", never_read)
    selection = []
    real_select = events_tail._default_host_readers

    def select(*args, **kwargs):
        result = real_select(*args, **kwargs)
        selection.append(tuple(name for name, _ in result))
        return result

    monkeypatch.setattr(events_tail, "_default_host_readers", select)
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["host_read_budgets"] == {
        "autopush_ms": 250,
        "autopush_source": "default",
        "interactive_ms": 500,
        "interactive_source": "config",
        "warm_ms": 5000,
    }
    assert tuple(payload["host_publication"]["readers"]) in selection
    assert payload["host_usage"]["codex"]["last_complete_at"] == at
    assert payload["host_usage"]["codex"]["last_deadline_allotted_ms"] == 250
    line = cli._host_read_sweep_line(payload)
    if total is None:
        assert line == f"unknown ({margin})"
    else:
        assert line == (
            f"{total} ms of the 250 ms autopush budget, {margin} "
            "(excludes cache writes and the later-reader grace; not a publication check)"
        )
    result = runner.invoke(app, ["diag"])
    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert "host read budgets: autopush 250 ms (default) · interactive 500 ms (config)" in plain
    assert "host read sweep estimate: " + line in plain
    if case != "over":
        assert (
            f"codex last complete read: scan 197 ms (excludes cache write), 3 h ago ({at})" in plain
        )
    assert "read it without that budget, then `mm diag`" not in plain
    if case in {"both", "codex-only"}:
        assert "Last read allowed 250 ms; last complete read 197 ms" in plain
    elif case == "over":
        assert "Last read allowed 250 ms; last complete read 250 ms" in plain
    elif case == "grok-only":
        assert "Last read allowed 250 ms; last complete read 18 ms" in plain


@pytest.mark.parametrize("invalid", [False, True])
def test_diag_unknown_host_read_budget_without_usable_config(tmp_path, monkeypatch, invalid):
    _storage, cfg_path, _backend = _setup(tmp_path, monkeypatch, with_config=invalid)
    if invalid:
        config = load_config()
        config["retro"] = {"host_usage_autopush_budget_ms": 99999}
        save_config(config)
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["host_read_budgets"] == {
        "autopush_ms": None,
        "autopush_source": None,
        "interactive_ms": None,
        "interactive_source": None,
        "warm_ms": 5000,
    }
    result = runner.invoke(app, ["diag"])
    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert "host read budgets: unknown (config unavailable)" in plain
    assert "host read sweep estimate: unknown (config unavailable)" in plain
    assert "codex last complete read: unknown" in plain
    assert "grok last complete read: unknown" in plain


@pytest.mark.parametrize("reader", ["codex", "grok"])
@pytest.mark.parametrize(
    "timestamp", ["2999-01-01T00:00:00+00:00", "bad\nforged", "2026-09-17", None]
)
def test_diag_last_complete_read_future_and_invalid_dates(tmp_path, monkeypatch, reader, timestamp):
    _setup(tmp_path, monkeypatch)
    path = host_usage.CACHE_PATH if reader == "codex" else host_usage.GROK_CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": 1, "files": {}, "last_complete_ms": 197, "last_complete_at": timestamp}
        )
    )
    result = runner.invoke(app, ["diag"])
    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    suffix = (
        "in the future (2999-01-01T00:00:00+00:00)"
        if timestamp and timestamp.startswith("2999")
        else "unknown (unknown)"
    )
    assert f"{reader} last complete read: scan 197 ms (excludes cache write), {suffix}" in plain
    assert "forged" not in plain


@pytest.mark.parametrize("reader", ["codex", "grok", "cursor"])
def test_registered_reader_remedy_uses_own_diag67a(tmp_path, monkeypatch, reader):
    from mind_meld import cli, events

    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    _enable_capture_sources(tmp_path, readers=() if reader == "cursor" else (reader,))
    cfg = load_config()
    cfg.setdefault("retro", {})["cursor_host_usage"] = reader == "cursor"
    save_config(cfg)
    assert tuple(host_usage.HOST_READER_DIAGS) == events.ACTIVE_HOST_READERS
    for name in host_usage.HOST_READER_DIAGS:
        monkeypatch.setattr(
            host_usage,
            f"{name}_usage_diag",
            lambda name=name: {
                "cache_state": "ok",
                "state": "ready",
                "complete_once": True,
                "usage_less_skipped": 0,
                "last_reason": None if name == reader else "unsupported",
            },
        )
    for command in ("status", "diag"):
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        # Other readers' cached unsupported formats cannot poison this consent set.
        assert "pipx upgrade mind-meld" not in " ".join(result.output.split())
    monkeypatch.setattr(
        host_usage,
        f"{reader}_usage_diag",
        lambda: {
            "cache_state": "ok",
            "state": "ready",
            "complete_once": True,
            "usage_less_skipped": 0,
            "last_reason": "unsupported",
        },
    )
    for command in ("status", "diag"):
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        assert "pipx upgrade mind-meld" in " ".join(result.output.split())
    if reader == "cursor":
        assert "Cursor via Conductor" in result.output
        assert "cursor_host_usage = false" in " ".join(result.output.split())
        assert "disable-source cursor" not in result.output
    with monkeypatch.context() as ready:
        ready.setattr(
            host_usage,
            f"{reader}_usage_diag",
            lambda: {
                "state": "ready",
                "complete_once": True,
                "last_reason": None,
            },
        )
        # The recapture bridge must use the same reader's own readiness test.
        lines = []
        ready.setattr(cli.console, "print", lambda *args, **kw: lines.append(args))
        cli._notice_recapture_host_usage(cfg, cfg["sync"]["sources"])
        assert lines == []


def test_cursor_diag_is_read_only_and_exposes_own_cache(cursor_store, tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert host_usage.read_cursor_usage(consented=True).complete
    cache = host_usage.CURSOR_CACHE_PATH
    before = (cache.read_bytes(), cache.stat().st_mtime_ns)

    def forbidden(*args, **kwargs):
        pytest.fail("diag opened the Cursor host ledger")

    monkeypatch.setattr(host_usage, "_read_cursor_file", forbidden)
    result = runner.invoke(app, ["diag", "--json"])
    assert result.exit_code == 0, result.output
    state = json.loads(result.stdout)["host_usage"]["cursor"]
    assert state["runs_cached"] == 3
    assert state["complete_once"] is True
    assert state["consented"] is False
    assert state["models"] == ["grok-4.7"]
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before
