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
from mind_meld import core, outputstyle
from mind_meld.cli import app
from mind_meld.config import save_config
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend
from tests.conftest import MEMORY_KB, PASSPHRASE, _redirect_lock, _redirect_sidecar

runner = CliRunner()
SRC_DIR = Path(core.__file__).resolve().parent


def test_output_style_helpers_pin_exact_forms(capsys):
    assert outputstyle._error_line("x") == "mm: error: x"
    assert outputstyle._fix_line("mm diag") == "Run: mm diag"
    assert outputstyle._warning_line("x") == "mm: warning: x"
    assert outputstyle._warning_line("x", fix="mm diag") == "mm: warning: x Run: mm diag"
    assert outputstyle._notice_line("x") == "mm: notice: x"
    assert outputstyle._notice_line("x", fix="mm update") == "mm: notice: x Run: mm update"
    assert outputstyle._outcome_line("push", "no content changes") == "Push: no content changes."
    assert outputstyle._outcome_line("pull", "no changes") == "Pull: no changes."
    assert (
        outputstyle._outcome_line("gc", "0 orphaned blobs deleted")
        == "Gc: 0 orphaned blobs deleted."
    )
    assert (
        outputstyle._outcome_line("recapture", "0 commit records found")
        == "Recapture: 0 commit records found."
    )
    assert outputstyle._fix_for("No config — run 'mm init'") == ("No config", "mm init")

    core._print_error("x", fix="mm init")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "mm: error: x\nRun: mm init\n"

    core._warning("x")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "mm: warning: x\n"

    core._warning("x", fix="mm diag")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "mm: warning: x Run: mm diag\n"

    core._notice("x")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "mm: notice: x\n"

    core._notice("x", fix="mm update")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "mm: notice: x Run: mm update\n"


def test_core_defines_no_outputstyle_builder_passthroughs_and_src_calls_outputstyle():
    deleted_names = ("_error_line", "_fix_line", "_outcome_line", "_fix_for")
    for name in deleted_names:
        assert not hasattr(core, name), f"core still defines {name}"

    for py_path in sorted(SRC_DIR.rglob("*.py")):
        rel = py_path.relative_to(SRC_DIR)
        tree = ast.parse(py_path.read_text(), filename=str(rel))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "core"
                and node.attr in deleted_names
            ):
                raise AssertionError(f"{rel}:{node.lineno} still references core.{node.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "print"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "console"
                and node.args
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Attribute)
                and node.args[0].func.attr == "_outcome_line"
            ):
                raise AssertionError(
                    f"{rel}:{node.lineno} calls console.print(..._outcome_line(...)) directly"
                )


@pytest.mark.parametrize("exc", [BrokenPipeError("broken pipe"), OSError("stdout failed")])
def test_print_outcome_swallows_oserror_and_propagates_system_exit(monkeypatch, exc):
    class _StubConsole:
        def __init__(self, to_raise: BaseException) -> None:
            self.to_raise = to_raise
            self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def print(self, *args: object, **kwargs: object) -> None:
            self.calls.append((args, kwargs))
            raise self.to_raise

    stub = _StubConsole(exc)
    monkeypatch.setattr(core, "console", stub)

    assert core._print_outcome(outputstyle._outcome_line("gc", "0 orphaned blobs deleted")) is None
    assert stub.calls == [
        (
            ("Gc: 0 orphaned blobs deleted.",),
            {"markup": False, "highlight": False, "soft_wrap": True},
        )
    ]

    exit_stub = _StubConsole(SystemExit(1))
    monkeypatch.setattr(core, "console", exit_stub)
    with pytest.raises(SystemExit) as excinfo:
        core._print_outcome(outputstyle._outcome_line("resolve", "resolved 1 of 1"))
    assert excinfo.value.code == 1


def test_print_outcome_keeps_its_owner_console_through_a_wrapper(monkeypatch):
    # Value: protects=the printer owns its output destination; fails_when=caller
    # globals redirect output; why_new=existing pins rebind core; seam=none.
    owned_lines = []
    other_output = io.StringIO()
    monkeypatch.setattr(core.console, "print", lambda line, **kwargs: owned_lines.append(line))
    monkeypatch.setitem(globals(), "console", Console(file=other_output))

    def wrapped(line):
        core._print_outcome(line)

    core._print_outcome("Resolve: direct.")
    wrapped("Resolve: wrapped.")

    assert owned_lines == ["Resolve: direct.", "Resolve: wrapped."]
    assert other_output.getvalue() == ""


def test_migrated_outcome_site_keeps_result_when_print_raises_oserror(monkeypatch):
    class _BrokenConsole:
        def print(self, *args: object, **kwargs: object) -> None:
            raise OSError("closed stdout")

    monkeypatch.setattr(core, "console", _BrokenConsole())
    install = cli_module.upgrade.InstallInfo(
        kind="tracking",
        version="1.2.3",
        spec=cli_module.upgrade.INSTALL_SPEC,
        venv_name="mind-meld",
    )

    assert cli_module._print_update_current(install) is None


@pytest.mark.parametrize("fail_mode", ["write", "flush"])
def test_warning_and_notice_swallow_stderr_stream_failures(monkeypatch, capsys, fail_mode):
    class _BrokenStream:
        def write(self, _s: str) -> int:
            if fail_mode == "write":
                raise OSError("stderr write failed")
            return len(_s)

        def flush(self) -> None:
            if fail_mode == "flush":
                raise OSError("stderr flush failed")

    class _FakeStderrConsole:
        file = _BrokenStream()

    monkeypatch.setattr(core, "stderr_console", _FakeStderrConsole())

    assert core._warning("x") is None
    assert core._notice("x") is None
    captured = capsys.readouterr()
    assert captured.out == ""


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
    assert outputstyle._fix_for(missing, None) == (
        "init: config not found at /Users/user/.config/mind-meld/config.toml",
        "mm init",
    )
    assert outputstyle._fix_for("Not initialized. Run mm init first", None) == (
        "Not initialized",
        "mm init",
    )
    assert outputstyle._fix_for(
        "Config is missing recommended excludes, then run mm migrate-config.", None
    ) == (
        "Config is missing recommended excludes",
        "mm migrate-config",
    )
    assert outputstyle._fix_for("Unrelated failure.", "mm diag") == (
        "Unrelated failure.",
        "mm diag",
    )
    assert outputstyle._fix_for("Unrelated failure.", None) == ("Unrelated failure.", None)


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


@pytest.mark.parametrize("argv", [["push"], ["push", "--dry-run"]])
def test_piped_push_and_dry_run_with_missing_excludes_warn_once_on_stderr(
    tmp_path, monkeypatch, argv
):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.toml"
    gstack_dir = tmp_path / "gstack"
    gstack_dir.mkdir()

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
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(gstack_dir),
                        "type": "generic",
                        "include_dirs": ["."],
                        "exclude_patterns": [],
                    }
                ],
                "disabled_sources": ["mm-events"],
                "max_file_size": 10 * 1024 * 1024,
                "scheduling": False,
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
        },
        cfg_path,
    )

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert "config missing recommended excludes" not in result.stdout
    assert result.stderr.splitlines() == [
        "mm: warning: config missing recommended excludes for source(s) gstack. "
        "Run: mm migrate-config"
    ]


def test_backfill_config_error_warns_through_shared_helper_without_markup(
    tmp_path, monkeypatch, capsys
):
    from mind_meld.errors import ConfigError

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)

    cfg = {
        "device": {"id": "dev-a", "name": "mac-a"},
        "storage": {"backend": "local", "path": str(storage_dir)},
        "sync": {"sources": [], "max_file_size": 10 * 1024 * 1024},
        "crypto": {"argon2_memory_kb": MEMORY_KB},
    }

    def bad_patch(_updates, path=None):
        raise ConfigError("[red]bad[/red] [\x1b[31mboom\nnext")

    monkeypatch.setattr(core, "patch_config_on_disk", bad_patch)

    returned_kb = core._init_crypto_session(backend, PASSPHRASE, cfg, read_only=False)

    captured = capsys.readouterr()
    assert returned_kb == MEMORY_KB
    assert cfg["crypto"].get("root_salt_fp")
    assert captured.out == ""
    assert "\x1b" not in captured.err
    assert captured.err.splitlines() == [
        "mm: warning: backfill skipped — [red]bad[/red] [boom\\nnext"
    ]


def test_init_cleanup_failure_warns_through_shared_helper_without_markup(
    tmp_path, monkeypatch, capsys
):
    from unittest.mock import MagicMock

    from mind_meld import initflow
    from mind_meld.errors import StorageError

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(initflow, "register_device", lambda *a, **kw: None)

    def boom_save(_c, path=None):
        raise OSError("disk full")

    monkeypatch.setattr(initflow, "save_config", boom_save)
    backend = MagicMock()
    backend.delete.side_effect = StorageError("[red]boom[/red] [\x1b[31m")

    config = {
        "device": {"id": "d1", "name": "mac-a"},
        "storage": {"path": str(tmp_path)},
    }
    with pytest.raises(OSError, match="disk full"):
        initflow._register_and_save(
            config, backend=backend, device_id="d1", device_name="mac-a", passphrase="pw"
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "\x1b" not in captured.err
    assert captured.err.splitlines() == [
        "mm: warning: cleanup of devices/d1.json after save_config failure failed "
        "(StorageError): [red]boom[/red] ["
    ]
