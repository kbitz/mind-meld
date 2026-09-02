"""Behavioural tests for ./bin/check / bin/_check.py (Track 37A).

The driver is stdlib-only and lives outside the packaged package, so tests
load it by path. Do not run the full suite recursively from pytest — the
clean-clone case uses a one-node --tests scope.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_check():
    path = ROOT / "bin" / "_check.py"
    spec = importlib.util.spec_from_file_location("mm_check", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_python(bindir: Path, name: str, version: str) -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    path.write_text(
        f'#!/bin/sh\necho "{version}"\nexit 0\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_venv(venv: Path) -> Path:
    bindir = venv / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    py = bindir / "python"
    py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    (venv / ".mm-check-owner").write_text("mind-meld-bin-check\n", encoding="utf-8")
    return py


class TestArgv:
    def test_node_id_is_accepted_as_scope(self) -> None:
        mod = load_check()
        args = mod.parse_argv(["tests/x.py::TestC::test_n"])
        assert args.scope == ["tests/x.py::TestC::test_n"]
        assert args.pytest_extra == []

    def test_args_after_dashdash_pass_through_verbatim(self) -> None:
        mod = load_check()
        args = mod.parse_argv(["tests/x.py", "--", "-k", "conflict", "--maxfail=1"])
        assert args.scope == ["tests/x.py"]
        assert args.pytest_extra == ["-k", "conflict", "--maxfail=1"]

    def test_unknown_flag_before_dashdash_passes_through(self) -> None:
        mod = load_check()
        args = mod.parse_argv(["tests/x.py", "-k", "conflict"])
        assert args.scope == ["tests/x.py"]
        assert args.pytest_extra == ["-k", "conflict"]

    def test_default_scope_is_tests(self) -> None:
        mod = load_check()
        args = mod.parse_argv([])
        assert args.scope == ["tests/"]

    def test_xdist_args_serial_and_pdb_suppress_n(self, monkeypatch) -> None:
        mod = load_check()
        monkeypatch.setenv("MM_PYTEST_WORKERS", "4")
        py = Path(sys.executable)
        assert mod.xdist_args(py, serial=True, extra=[]) == []
        assert mod.xdist_args(py, serial=False, extra=["--pdb"]) == []
        assert mod.xdist_args(py, serial=False, extra=["-x"]) == []
        assert mod.xdist_args(py, serial=False, extra=["-n", "2"]) == []
        got = mod.xdist_args(py, serial=False, extra=[])
        if got:
            assert got == ["-n", "4"]

    def test_lint_and_tests_together_is_an_error(self, capsys) -> None:
        mod = load_check()
        with pytest.raises(SystemExit) as ei:
            mod.main(["--lint", "--tests"])
        assert ei.value.code != 0
        err = capsys.readouterr().err
        assert "--lint and --tests together" in err


class TestInterpreter:
    def test_prefers_3_13_when_present(self, tmp_path: Path, monkeypatch) -> None:
        mod = load_check()
        bindir = tmp_path / "bin"
        _stub_python(bindir, "python3.13", "3.13.15")
        _stub_python(bindir, "python3", "3.14.7")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("MM_PYTHON", raising=False)
        path, ver = mod.resolve_interpreter()
        assert path.endswith("python3.13")
        assert ver[:2] == (3, 13)

    def test_only_3_14_notices_and_succeeds(self, tmp_path: Path, monkeypatch, capsys) -> None:
        mod = load_check()
        bindir = tmp_path / "bin"
        _stub_python(bindir, "python3", "3.14.7")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("MM_PYTHON", raising=False)
        path, ver = mod.resolve_interpreter()
        assert ver[:2] == (3, 14)
        err = capsys.readouterr().err
        assert "interpreter" in err
        assert "notice:" in err
        assert "CI pins 3.13" in err

    def test_nothing_ge_311_fails_without_recommending_xcode_select(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        mod = load_check()
        bindir = tmp_path / "bin"
        _stub_python(bindir, "python3", "3.9.6")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("MM_PYTHON", raising=False)
        monkeypatch.delenv("CONDUCTOR_IS_LOCAL", raising=False)
        with pytest.raises(SystemExit) as ei:
            mod.resolve_interpreter()
        assert ei.value.code != 0
        err = capsys.readouterr().err
        assert "do not run xcode-select" in err
        assert ">= 3.11" in err or ">=3.11" in err

    def test_cloud_workspace_remedy_is_not_xcode_select(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        mod = load_check()
        bindir = tmp_path / "bin"
        _stub_python(bindir, "python3", "3.9.6")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("MM_PYTHON", raising=False)
        monkeypatch.setenv("CONDUCTOR_IS_LOCAL", "0")
        with pytest.raises(SystemExit):
            mod.resolve_interpreter()
        err = capsys.readouterr().err
        assert "Linux cloud workspace" in err
        assert "xcode-select --install" not in err


class TestBootstrap:
    def test_second_run_does_not_reinstall(self, tmp_path: Path, monkeypatch) -> None:
        """The property everything else rests on. pyvenv.cfg mtime would
        reinstall forever after the first pyproject.toml edit."""
        mod = load_check()
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        pip_calls: list[object] = []

        def fake_create(python: str, venv: Path) -> None:
            _fake_venv(venv)

        def fake_pip(python: str, repo: Path) -> None:
            pip_calls.append((python, repo))

        monkeypatch.setattr(mod, "_create_venv", fake_create)
        monkeypatch.setattr(mod, "_pip_install", fake_pip)
        monkeypatch.setattr(mod, "venv_healthy", lambda _p: True)

        python = sys.executable
        version = (3, 13, 0)
        mod.ensure_venv(root, python, version, rebuild=False)
        assert len(pip_calls) == 1
        mod.ensure_venv(root, python, version, rebuild=False)
        assert len(pip_calls) == 1, "second run reinstalled — stamp is broken"

    def test_pyproject_change_reinstalls(self, tmp_path: Path, monkeypatch) -> None:
        mod = load_check()
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        pip_calls: list[object] = []
        monkeypatch.setattr(mod, "_create_venv", lambda python, venv: _fake_venv(venv))
        monkeypatch.setattr(mod, "_pip_install", lambda python, repo: pip_calls.append(1))
        monkeypatch.setattr(mod, "venv_healthy", lambda _p: True)
        python, version = sys.executable, (3, 13, 0)
        mod.ensure_venv(root, python, version, rebuild=False)
        (root / "pyproject.toml").write_text("[project]\nname = 'y'\n", encoding="utf-8")
        mod.ensure_venv(root, python, version, rebuild=False)
        assert len(pip_calls) == 2, "stamp did not invalidate after pyproject.toml changed"

    def test_unowned_broken_venv_is_not_deleted(self, tmp_path: Path, monkeypatch, capsys) -> None:
        mod = load_check()
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        venv = root / ".venv"
        venv.mkdir()
        junk = venv / "not-ours"
        junk.write_text("keep me\n", encoding="utf-8")
        (venv / "bin").mkdir()
        py = venv / "bin" / "python"
        py.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        py.chmod(py.stat().st_mode | stat.S_IXUSR)

        monkeypatch.setattr(mod, "venv_healthy", lambda _p: False)
        with pytest.raises(SystemExit):
            mod.ensure_venv(root, sys.executable, (3, 13, 0), rebuild=False)
        assert junk.is_file(), "bin/check deleted a venv it does not own"
        err = capsys.readouterr().err
        assert "not owned" in err
        assert "mv .venv .venv.bak" in err

    def test_mm_venv_never_mutates(self, tmp_path: Path, monkeypatch, capsys) -> None:
        mod = load_check()
        venv = tmp_path / "external"
        venv.mkdir()
        marker = venv / "sentinel"
        marker.write_text("stay\n", encoding="utf-8")
        monkeypatch.setenv("MM_VENV", str(venv))
        with pytest.raises(SystemExit):
            mod._mm_venv_python()
        assert marker.is_file()
        assert list(venv.iterdir()) == [marker]
        err = capsys.readouterr().err
        assert "never mutated" in err or "validate-and-use" in err

    def test_two_runs_serialize_on_bootstrap_lock(self, tmp_path: Path) -> None:
        mod = load_check()
        order: list[str] = []

        def worker(tag: str) -> None:
            with mod.bootstrap_lock(tmp_path):
                order.append(f"in-{tag}")
                time.sleep(0.05)
                order.append(f"out-{tag}")

        threads = [
            threading.Thread(target=worker, args=("a",)),
            threading.Thread(target=worker, args=("b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Exclusive lock: one worker fully finishes before the other enters.
        assert order in (
            ["in-a", "out-a", "in-b", "out-b"],
            ["in-b", "out-b", "in-a", "out-a"],
        )


def test_help_lists_flags_and_non_compound_fallback() -> None:
    import subprocess

    proc = subprocess.run(
        [str(ROOT / "bin" / "check"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    out = proc.stdout
    for flag in (
        "--help",
        "--no-bootstrap",
        "--lint",
        "--tests",
        "--serial",
        "--rebuild",
        "MM_PYTHON",
        "MM_VENV",
    ):
        assert flag in out
    assert "pip install -e '.[dev]'" in out
    recipe = out.split("manual fallback", 1)[-1]
    commands = [
        line.strip() for line in recipe.splitlines() if line.strip().startswith(("python", "./"))
    ]
    assert commands, recipe
    for line in commands:
        assert ";" not in line
        assert "&&" not in line
        assert "||" not in line


def test_absolute_invocation_from_another_cwd(tmp_path: Path) -> None:
    import subprocess

    proc = subprocess.run(
        [str(ROOT / "bin" / "check"), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "usage: ./bin/check" in proc.stdout


def test_clean_clone_end_to_end(tmp_path: Path) -> None:
    """Temporary checkout, isolated HOME and pip cache, cheap test scope.

    Do not run the full suite recursively from pytest.
    """
    import subprocess

    dest = tmp_path / "clone"
    clone = subprocess.run(
        ["git", "clone", "--local", "--", str(ROOT), str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if clone.returncode != 0:
        pytest.skip("git clone --local failed: " + clone.stderr[-400:])
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["PIP_CACHE_DIR"] = str(tmp_path / "pip-cache")
    env.pop("VIRTUAL_ENV", None)
    env.pop("MM_VENV", None)
    env.pop("MM_CHECK_ROOT", None)
    env.pop("PYTEST_XDIST_WORKER", None)
    env.pop("PYTEST_XDIST_WORKER_COUNT", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [
            str(dest / "bin" / "check"),
            "--tests",
            "--serial",
            "tests/test_docs_routing.py::test_claude_md_is_a_symlink_to_agents_md",
        ],
        cwd=str(dest),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 and "pip install failed" in proc.stderr:
        pytest.skip("pip unavailable in isolated cache: " + proc.stderr[-400:])
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-500:]
    assert "interpreter" in proc.stderr
