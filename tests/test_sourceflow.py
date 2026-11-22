"""Source command contracts exercised through the source owner, without cli.

Pins the outcome lines and exit codes of `sources`, `enable-source`,
`disable-source` and `reconfigure-sources` over a tmp_path config. The shells'
flags live in tests/test_source_toggle.py, which can import cli.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import config, core, seen_sources, sourceflow

app = typer.Typer()
app.command("sources")(sourceflow.sources)
app.command("disable-source")(sourceflow.disable_source)
app.command("enable-source")(sourceflow.enable_source)
app.command("reconfigure-sources")(sourceflow.reconfigure_sources)
runner = CliRunner()

DETAIL_LINES = {
    "grok": [
        "Syncs ~/.grok skills/, commands/, rules/, and memory-v2/ topics. "
        "Observations, sessions, SQLite indexes, MEMORY.md, settings, credentials, "
        "plugins and hooks are not synced.",
        "Grok keeps memory off by default; turn it on on each machine with "
        "[memory] enabled = true in ~/.grok/config.toml (or GROK_MEMORY=1).",
        "Run: mm disable-source grok",
    ],
    "muse": [
        "Syncs ~/.local/share/muse memory/ only. "
        "Session files are not synced. Prompts never leave this machine.",
        "Reads completed model-step token totals from local Muse session logs. "
        "Session files are not synced. Prompts never leave this machine.",
        "Run: mm disable-source muse",
    ],
    "agy": [
        "Syncs ~/.gemini/config skills/, rules/, AGENTS.md, and GEMINI.md. "
        "Session databases and settings are not synced.",
        "Reads completed model-step token totals from local Antigravity "
        "session databases. Prompts never leave this device.",
        "Run: mm disable-source agy",
    ],
}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Config with claude + gstack explicit sources over tmp_path; HOME is a tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mind_meld.host_usage.AGY_CONVERSATIONS_PATH",
        home / ".gemini/antigravity-cli/conversations",
    )
    storage = tmp_path / "storage"
    storage.mkdir()
    claude = tmp_path / ".claude"
    claude.mkdir()
    gstack = tmp_path / ".gstack"
    gstack.mkdir()
    config_path = tmp_path / "config.toml"
    config.save_config(
        {
            "device": {"id": "abc123", "name": "MacBook"},
            "storage": {"path": str(storage)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude), "type": "claude"},
                    {"name": "gstack", "path": str(gstack), "type": "generic"},
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        },
        config_path,
    )
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    # Private size fields, not the width property: the setter's undo pins
    # the computed size for the rest of the worker. Rich ignores a
    # width-only pin when TERM=dumb and wraps at 80 columns.
    monkeypatch.setattr(sourceflow.console, "_width", 240)
    monkeypatch.setattr(sourceflow.console, "_height", 50)
    return config_path


def _on_disk(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def _disabled(path):
    return _on_disk(path)["sync"].get("disabled_sources") or []


def _lines(text):
    return [line for line in text.splitlines() if line]


# ── default source (gstack) ───────────────────────────────────────────


def test_default_source_disable_enable_round_trip(cfg):
    result = runner.invoke(app, ["disable-source", "gstack"])
    assert result.exit_code == 0, result.output
    assert _lines(result.stdout) == [
        "Disable: source 'gstack' disabled on this device.",
        "Run: mm enable-source gstack",
    ]
    assert _disabled(cfg) == ["gstack"]

    before = cfg.read_bytes()
    again = runner.invoke(app, ["disable-source", "gstack"])
    assert again.exit_code == 0, again.output
    assert _lines(again.stdout) == ["Disable: source 'gstack' is already disabled."]
    assert cfg.read_bytes() == before

    result = runner.invoke(app, ["enable-source", "gstack"])
    assert result.exit_code == 0, result.output
    assert _lines(result.stdout) == ["Enable: source 'gstack' enabled on this device."]
    assert _disabled(cfg) == []

    again = runner.invoke(app, ["enable-source", "gstack"])
    assert again.exit_code == 0, again.output
    assert _lines(again.stdout) == ["Enable: source 'gstack' is already enabled."]


# ── opt-in sources (grok, muse, agy) ──────────────────────────────────


@pytest.mark.parametrize("name", ["grok", "muse", "agy"])
def test_opt_in_source_enable_then_disable(cfg, name):
    result = runner.invoke(app, ["enable-source", name])
    assert result.exit_code == 0, result.output
    assert _lines(result.stdout) == [
        f"Enable: source '{name}' enabled on this device.",
        *DETAIL_LINES[name],
    ]
    added = next(s for s in _on_disk(cfg)["sync"]["sources"] if s["name"] == name)
    assert added == config.get_default_source(name)
    assert name not in _disabled(cfg)

    again = runner.invoke(app, ["enable-source", name])
    assert again.exit_code == 0, again.output
    expected_again = [f"Enable: source '{name}' is already enabled."]
    if name == "grok":
        expected_again.extend(DETAIL_LINES["grok"][:2])
    assert _lines(again.stdout) == expected_again

    off = runner.invoke(app, ["disable-source", name])
    assert off.exit_code == 0, off.output
    assert _lines(off.stdout) == [
        f"Disable: source '{name}' disabled on this device.",
        f"Run: mm enable-source {name}",
    ]
    on_disk = _on_disk(cfg)
    assert name in _disabled(cfg)
    assert any(s["name"] == name for s in on_disk["sync"]["sources"])


def test_enable_grok_clears_disabled_sources_from_v1118_migration(cfg):
    raw = _on_disk(cfg)
    raw["sync"]["disabled_sources"] = ["grok", "opencode"]
    config.save_config(raw, cfg)

    result = runner.invoke(app, ["enable-source", "grok"])
    assert result.exit_code == 0, result.output
    on_disk = _on_disk(cfg)
    assert _disabled(cfg) == ["opencode"]
    added = next(s for s in on_disk["sync"]["sources"] if s["name"] == "grok")
    assert added == config.get_default_source("grok")
    assert config.grok_usage_consented(config.load_config(cfg)) is True

    off = runner.invoke(app, ["disable-source", "grok"])
    assert off.exit_code == 0, off.output
    on_disk = _on_disk(cfg)
    assert _disabled(cfg) == ["grok", "opencode"]
    assert any(s["name"] == "grok" for s in on_disk["sync"]["sources"])
    assert config.grok_usage_consented(config.load_config(cfg)) is False

    again = runner.invoke(app, ["enable-source", "grok"])
    assert again.exit_code == 0, again.output
    assert _disabled(cfg) == ["opencode"]
    assert config.grok_usage_consented(config.load_config(cfg)) is True


# ── names that are not accepted ───────────────────────────────────────


@pytest.mark.parametrize("command", ["enable-source", "disable-source"])
def test_unknown_source_name_exits_1_and_writes_nothing(cfg, command):
    before = cfg.read_bytes()
    result = runner.invoke(app, [command, "definitely-unknown"])
    assert result.exit_code == 1, result.output
    assert result.stderr.startswith("mm: error: unknown source 'definitely-unknown'")
    assert result.stdout == ""
    assert cfg.read_bytes() == before
    assert not seen_sources.seen_path().exists()


@pytest.mark.parametrize("command", ["enable-source", "disable-source"])
def test_unknown_source_name_with_force_is_accepted(cfg, command):
    valid = ", ".join(sourceflow._known_source_names(config.load_config(cfg)))
    result = runner.invoke(app, [command, "definitely-unknown", "--force"])
    assert result.exit_code == 0, result.output
    assert result.stderr == (
        f"mm: warning: 'definitely-unknown' is not a known source "
        f"(valid: {valid}); accepting via --force.\n"
    )
    assert "is not a known source" not in result.stdout


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("command", ["enable-source", "disable-source"])
def test_retired_source_exits_1_with_or_without_force(cfg, command, force):
    before = cfg.read_bytes()
    result = runner.invoke(app, [command, "opencode", *(["--force"] if force else [])])
    assert result.exit_code == 1, result.output
    assert "source 'opencode' was retired" in result.stderr
    assert cfg.read_bytes() == before


# ── failure behaviour ─────────────────────────────────────────────────


def test_config_write_failure_stops_before_success_output(cfg, monkeypatch):
    """A failed config write prints no outcome line and records nothing as seen."""

    def boom(updates, path=None):
        raise OSError("disk full")

    monkeypatch.setattr(sourceflow, "patch_config_on_disk", boom)
    before = cfg.read_bytes()
    result = runner.invoke(app, ["disable-source", "gstack"])
    assert result.exit_code == 1
    assert isinstance(result.exception, OSError)
    assert "Disable:" not in result.output
    assert "disabled source" not in result.output.lower()
    assert cfg.read_bytes() == before
    assert not seen_sources.seen_path().exists()


# ── sources ───────────────────────────────────────────────────────────


def _source_rows(output):
    """Table body rows keyed by source name: [name, path, type, enabled, files, excluded]."""
    rows = {}
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.strip("│").split("│")]
        if line.startswith("│") and len(cells) == 6:
            rows[cells[0]] = cells
    return rows


def test_sources_lists_configured_sources(cfg):
    result = runner.invoke(app, ["sources"])
    assert result.exit_code == 0, result.output
    assert "Configured Sources" in result.stdout
    rows = _source_rows(result.stdout)
    assert [rows[name][2] for name in ("claude", "gstack")] == ["claude", "generic"]
    assert [rows[name][3] for name in ("claude", "gstack")] == ["Y", "Y"]

    runner.invoke(app, ["disable-source", "gstack"])
    after = runner.invoke(app, ["sources"])
    assert after.exit_code == 0, after.output
    rows = _source_rows(after.stdout)
    assert [rows[name][3] for name in ("claude", "gstack")] == ["Y", "N"]


@pytest.mark.parametrize(
    "patterns,expected",
    [
        ({"exclude_patterns": ["projects/*/repo-mode.json"]}, ["2", "1"]),
        ({"include_patterns": ["projects/*/role.md"]}, ["1", "2"]),
    ],
)
def test_sources_counts_files_kept_and_excluded_by_source_patterns(cfg, patterns, expected):
    # Value: protects=mm sources reports kept and per-source-excluded file counts;
    # fails_when=the no-patterns recount walk is dropped or kept/excluded are swapped;
    # why_new=existing tests only assert the Excluded header; seam=none;
    # no_claim=global junk-list exclusion counts
    on_disk = _on_disk(cfg)
    by_name = {s["name"]: s for s in on_disk["sync"]["sources"]}
    app_dir = Path(by_name["gstack"]["path"]) / "projects" / "app"
    app_dir.mkdir(parents=True)
    for name in ("repo-mode.json", "role.md", "notes.md"):
        (app_dir / name).write_text("x")
    memory = Path(by_name["claude"]["path"]) / "projects" / "-x" / "memory"
    memory.mkdir(parents=True)
    (memory / "a.md").write_text("x")
    by_name["gstack"]["include_dirs"] = ["projects"]
    by_name["gstack"].update(patterns)
    config.save_config(on_disk, cfg)

    result = runner.invoke(app, ["sources"])
    assert result.exit_code == 0, result.output
    rows = _source_rows(result.stdout)
    assert rows["gstack"][4:] == expected
    assert rows["claude"][4:] == ["1", "—"]


# ── reconfigure-sources ───────────────────────────────────────────────


def test_reconfigure_abort_exits_1_and_writes_nothing(cfg, monkeypatch):
    def abort(prompt, default):
        raise typer.Abort()

    monkeypatch.setattr(core, "ask_yes_no", abort)
    before = cfg.read_bytes()
    result = runner.invoke(app, ["reconfigure-sources"])
    assert result.exit_code == 1, result.output
    assert _lines(result.stdout)[0] == "Reconfigure sources for this device."
    assert _lines(result.stderr) == [
        "mm: error: reconfigure aborted; no changes written.",
        "Run: mm reconfigure-sources",
    ]
    assert cfg.read_bytes() == before


def test_reconfigure_accepting_defaults_updates_the_configuration(cfg):
    result = runner.invoke(app, ["reconfigure-sources"], input="\n" * 8)
    assert "Sync 'claude' source" in result.stdout
    assert "Sync 'codex' source at ~/.codex? (not detected) [y/n] (Enter = n)" in result.stdout
    assert "codex-memory" not in result.stdout
    assert result.exit_code == 0, result.output
    assert _lines(result.stdout)[0] == "Reconfigure sources for this device."
    assert _lines(result.stdout)[-1] == "Reconfigure: source configuration updated."
    assert seen_sources.seen_path().exists()


def test_reconfigure_no_sources_configured_prints_shared_outcome(cfg, monkeypatch):
    monkeypatch.setattr(sourceflow, "DEFAULT_SOURCES", [])
    on_disk = _on_disk(cfg)
    on_disk["sync"]["sources"] = []
    config.save_config(on_disk, cfg)
    result = runner.invoke(app, ["reconfigure-sources"])
    assert result.exit_code == 0, result.output
    assert _lines(result.stdout) == ["Reconfigure: no sources configured to reconfigure."]


def test_sourceflow_outcomes_emit_no_ansi_and_preserve_brackets_under_forced_terminal(
    cfg, monkeypatch
):
    import io

    from rich.console import Console

    buf = io.StringIO()
    monkeypatch.setattr(
        sourceflow,
        "console",
        Console(file=buf, force_terminal=True, color_system="standard", width=240),
    )
    monkeypatch.setattr(core, "console", sourceflow.console)
    sourceflow.disable_source(name="custom[tag]", force=True)
    sourceflow.enable_source(name="custom[tag]", force=True)

    lines = buf.getvalue().splitlines()
    assert lines == [
        "Disable: source 'custom[tag]' disabled on this device.",
        "Run: mm enable-source custom[tag]",
        "Enable: source 'custom[tag]' enabled on this device.",
    ]
    assert "\x1b" not in buf.getvalue()


def test_sourceflow_contract_import_does_not_register_cli():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tests.test_sourceflow; import sys; assert 'mind_meld.cli' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("verb", ["enable-source", "disable-source"])
@pytest.mark.parametrize("force", [False, True])
def test_codex_memory_toggle_redirects_without_writing(cfg, verb, force):
    before_bytes = cfg.read_bytes()
    seen_before = seen_sources.seen_path().exists()
    args = [verb, "codex-memory", *(["--force"] if force else [])]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    err_lines = _lines(result.stderr)
    assert len(err_lines) == 1
    assert err_lines[0].startswith("mm: notice:")
    assert f"Run: mm {verb} codex" in err_lines[0]
    assert cfg.read_bytes() == before_bytes
    assert seen_sources.seen_path().exists() is seen_before


def test_sources_merges_codex_memory_count_into_codex_row(cfg, tmp_path):
    codex_dir = tmp_path / "codex-home"
    (codex_dir / "skills" / "my-skill").mkdir(parents=True)
    (codex_dir / "skills" / "my-skill" / "SKILL.md").write_text("# skill\n")
    (codex_dir / "memories_v2").mkdir(parents=True)
    (codex_dir / "memories_v2" / "MEMORY.md").write_text("# mem\n")
    (codex_dir / "memories_v2" / "memory_summary.md").write_text("# summary\n")

    codex_src = config.get_default_source("codex")
    assert codex_src is not None
    codex_src["path"] = str(codex_dir)
    on_disk = _on_disk(cfg)
    on_disk["sync"]["sources"].append(codex_src)
    config.save_config(on_disk, cfg)

    enabled_res = runner.invoke(app, ["sources"], env={"COLUMNS": "180"})
    assert enabled_res.exit_code == 0, enabled_res.output
    assert "codex-memory" not in enabled_res.stdout
    codex_lines = [line for line in enabled_res.stdout.splitlines() if "│ codex " in line]
    assert len(codex_lines) == 1
    # 1 skill file + 2 codex memory files = 3
    assert "3" in codex_lines[0]
    assert "Y" in codex_lines[0]

    on_disk["sync"]["disabled_sources"] = ["codex"]
    config.save_config(on_disk, cfg)
    disabled_res = runner.invoke(app, ["sources"], env={"COLUMNS": "180"})
    assert disabled_res.exit_code == 0, disabled_res.output
    assert "codex-memory" not in disabled_res.stdout
    codex_disabled_lines = [line for line in disabled_res.stdout.splitlines() if "│ codex " in line]
    assert len(codex_disabled_lines) == 1
    assert "3" in codex_disabled_lines[0]
    assert "N" in codex_disabled_lines[0]


def test_reconfigure_drops_stale_codex_memory_entry_and_disabled_name(cfg, tmp_path):
    on_disk = _on_disk(cfg)
    on_disk["sync"]["sources"].append(
        {"name": "codex-memory", "path": str(tmp_path / "old-codex"), "type": "codex-memory"}
    )
    on_disk["sync"]["disabled_sources"] = ["codex-memory", "gstack"]
    config.save_config(on_disk, cfg)

    result = runner.invoke(app, ["reconfigure-sources"], input="\n" * 8)
    assert result.exit_code == 0, result.output
    assert "codex-memory" not in result.stdout

    after = _on_disk(cfg)
    assert not any(s["name"] == "codex-memory" for s in after["sync"]["sources"])
    assert "codex-memory" not in after["sync"]["disabled_sources"]
    assert "gstack" in after["sync"]["disabled_sources"]
