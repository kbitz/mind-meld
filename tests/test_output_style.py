"""Tests for the shared output style (Track 129B, docs/SPEC.md O8)."""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

import mind_meld.cli as cli_module
from mind_meld import config as config_module
from mind_meld import core
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend
from tests.conftest import MEMORY_KB, PASSPHRASE, _redirect_lock, _redirect_sidecar

runner = CliRunner()
SRC_DIR = Path(core.__file__).resolve().parent


def test_output_style_helpers_pin_exact_forms(capsys):
    assert core._error_line("x") == "mm: error: x"
    assert core._fix_line("mm diag") == "Run: mm diag"
    assert core._outcome_line("push", "no content changes") == "Push: no content changes."
    assert core._outcome_line("pull", "no changes") == "Pull: no changes."

    core._print_error("x", fix="mm init")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "mm: error: x\nRun: mm init\n"

    core._warning("x")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "mm: warning: x\n"

    core._notice("x")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "mm: notice: x\n"


def test_print_error_writes_plain_text_even_when_stderr_forces_terminal(monkeypatch):
    buf = io.StringIO()
    forced_tty = Console(file=buf, force_terminal=True, color_system="truecolor")
    monkeypatch.setattr(core, "stderr_console", forced_tty)
    monkeypatch.setattr("sys.stderr", buf)

    core._print_error("bad [bold]thing[/bold]", fix="mm init")

    rendered = buf.getvalue()
    assert "\x1b[" not in rendered
    assert rendered == "mm: error: bad [bold]thing[/bold]\nRun: mm init\n"


def test_fix_for_extracts_remedy_and_preserves_unmatched_text():
    missing = (
        "init: config not found at /Users/user/.config/mind-meld/config.toml — run 'mm init' first."
    )
    assert core._fix_for(missing, None) == (
        "init: config not found at /Users/user/.config/mind-meld/config.toml",
        "mm init",
    )
    assert core._fix_for("Not initialized. Run mm init first", None) == (
        "Not initialized",
        "mm init",
    )
    assert core._fix_for(
        "Config is missing recommended excludes, then run mm migrate-config.", None
    ) == (
        "Config is missing recommended excludes",
        "mm migrate-config",
    )
    assert core._fix_for("Unrelated failure.", "mm diag") == ("Unrelated failure.", "mm diag")
    assert core._fix_for("Unrelated failure.", None) == ("Unrelated failure.", None)


@pytest.mark.parametrize(
    ("argv", "expected_error"),
    [
        (["status"], "mm: error: init: config not found at {cfg}\nRun: mm init\n"),
        (["devices"], "mm: error: init: config not found at {cfg}\nRun: mm init\n"),
        (
            ["push"],
            "mm: error: Push failed [setup-error]: init: config not found at {cfg}; "
            "no content published.\nRun: mm init\n",
        ),
    ],
)
def test_no_config_commands_emit_shared_error_and_fix(tmp_path, monkeypatch, argv, expected_error):
    cfg_path = tmp_path / "missing" / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_path)

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0].startswith("mm: error: ")
    assert lines[-1] == "Run: mm init"
    assert result.stderr == expected_error.format(cfg=cfg_path)


def test_no_legacy_rich_warning_or_error_banners_in_core_or_push_pull():
    core_tree = ast.parse((SRC_DIR / "core.py").read_text())
    forbidden = ("[yellow]Warning:", "[red]Error:")
    for node in ast.walk(core_tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for tag in forbidden:
                assert tag not in node.value, f"core.py:{node.lineno} still contains {tag!r}"

    cli_tree = ast.parse((SRC_DIR / "cli.py").read_text())
    target_funcs = {
        "push",
        "pull",
        "_attended_pull",
        "_push_core",
        "_sync_success",
        "_sync_failure",
        "_emit_sync_outcome",
        "_sync_update",
        "_warn_push_growth",
        "_ensure_device_registered",
        "_record_push_anchor_observations",
    }
    found = set()
    for item in cli_tree.body:
        if isinstance(item, ast.FunctionDef) and item.name in target_funcs:
            found.add(item.name)
            for node in ast.walk(item):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for tag in forbidden:
                        assert tag not in node.value, (
                            f"cli.py:{item.name}:{node.lineno} still contains {tag!r}"
                        )
    assert found == target_funcs


def test_attended_push_with_no_sources_warns_on_stderr_and_prints_outcome(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.toml"

    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    monkeypatch.setattr(config_module, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_path)
    _redirect_lock(monkeypatch, tmp_path)
    _redirect_sidecar(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_module.upgrade, "run_transition_hook", lambda *a, **k: None)
    monkeypatch.setattr(cli_module.upgrade, "update_or_nudge", lambda *a, **k: None)

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-a", "mac-a")

    save_config(
        {
            "device": {"id": "dev-a", "name": "mac-a"},
            "storage": {"backend": "local", "path": str(storage_dir)},
            "sync": {
                "claude_dir": str(tmp_path / "absent-claude"),
                "sources": [],
                "disabled_sources": ["mm-events"],
                "max_file_size": 10 * 1024 * 1024,
                "scheduling": False,
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        cfg_path,
    )

    result = runner.invoke(app, ["push"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "Push: no enabled sources.\n"
    assert "mm: warning: no sync sources found. Run: mm init\n" in result.stderr


@pytest.mark.parametrize(
    ("argv", "expected_prefix", "expected_fix"),
    [
        (["push", "--bogus"], "mm: error: No such option: --bogus", "Run: mm push --help"),
        (["--bogus"], "mm: error: No such option: --bogus", "Run: mm --help"),
        (["devices", "bogus"], "mm: error: No such command 'bogus'.", "Run: mm devices --help"),
        (["autopush", "--bogus"], "mm: error: No such option: --bogus", "Run: mm autopush --help"),
    ],
)
def test_usage_errors_emit_shared_error_and_fix_without_panel(argv, expected_prefix, expected_fix):
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "╭" not in result.stderr
    assert "Usage:" not in result.stderr
    lines = result.stderr.splitlines()
    assert lines[0].startswith(expected_prefix)
    assert lines[-1] == expected_fix
