"""Behavioural tests for ./bin/check / bin/_check.py (Track 37A).

The driver is stdlib-only and lives outside the packaged package, so tests
load it by path. Do not run the full suite recursively from pytest — the
clean-clone case uses a one-node --tests scope.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
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
    def test_mm_python_takes_precedence_over_preferred_path_candidate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        mod = load_check()
        bindir = tmp_path / "bin"
        explicit = _stub_python(bindir, "explicit-python", "3.12.9")
        _stub_python(bindir, "python3.13", "3.13.15")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setenv("MM_PYTHON", str(explicit))

        path, ver = mod.resolve_interpreter()

        assert path == str(explicit)
        assert ver == (3, 12, 9)

    @pytest.mark.parametrize("output", ["", "3", "three.thirteen", "3.13.bad"])
    def test_python_version_rejects_malformed_output(self, monkeypatch, output: str) -> None:
        mod = load_check()
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(["python"], 0, stdout=output),
        )

        assert mod.python_version("python") is None

    def test_prefers_3_13_when_both_ci_pythons_present(self, tmp_path: Path, monkeypatch) -> None:
        """A Mac with 3.13 and 3.14 keeps the macOS CI interpreter (Track 73A)."""
        mod = load_check()
        bindir = tmp_path / "bin"
        _stub_python(bindir, "python3.14", "3.14.7")
        _stub_python(bindir, "python3.13", "3.13.15")
        _stub_python(bindir, "python3", "3.12.9")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("MM_PYTHON", raising=False)
        path, ver = mod.resolve_interpreter()
        assert path.endswith("python3.13")
        assert ver[:2] == (3, 13)

    def test_driver_finds_named_python3_14(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A 3.14-only host (Linux CI shape) resolves via the named candidate."""
        mod = load_check()
        bindir = tmp_path / "bin"
        _stub_python(bindir, "python3.14", "3.14.7")
        _stub_python(bindir, "python3", "3.9.6")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("MM_PYTHON", raising=False)
        path, ver = mod.resolve_interpreter()
        assert path.endswith("python3.14")
        assert ver[:2] == (3, 14)
        err = capsys.readouterr().err
        assert "interpreter" in err
        # 3.14 is a CI interpreter now: no divergence notice.
        assert "notice:" not in err

    def test_non_ci_version_notice_names_both_ci_interpreters(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        mod = load_check()
        bindir = tmp_path / "bin"
        _stub_python(bindir, "python3.12", "3.12.9")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("MM_PYTHON", raising=False)
        _path, ver = mod.resolve_interpreter()
        assert ver[:2] == (3, 12)
        err = capsys.readouterr().err
        assert "notice:" in err
        assert "3.13 on macOS" in err
        assert "3.14 on Linux" in err

    # Extended by the /ship test coverage audit (pass 1): platform parametrization.
    # Value: protects=the driver's real no-Python exit prints the remedy for the
    # host OS; fails_when=resolve_interpreter or _fix_no_python stops reading
    # sys.platform; why_new=the OS test passes platform explicitly and this one
    # only checked "3.14", which both remedies contain; seam=none;
    # no_claim=a real Linux host (sys.platform is patched)
    @pytest.mark.parametrize(
        "platform,expect,forbid",
        [
            ("darwin", "do not run xcode-select --install", "distro Python"),
            ("linux", "install a distro Python 3.11+", "xcode-select --install"),
        ],
    )
    def test_nothing_ge_311_fails_naming_3_14(
        self, tmp_path: Path, monkeypatch, capsys, platform: str, expect: str, forbid: str
    ) -> None:
        mod = load_check()
        bindir = tmp_path / "bin"
        _stub_python(bindir, "python3", "3.9.6")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("MM_PYTHON", raising=False)
        monkeypatch.setattr(mod.sys, "platform", platform)
        with pytest.raises(SystemExit) as ei:
            mod.resolve_interpreter()
        assert ei.value.code != 0
        err = capsys.readouterr().err
        assert ">= 3.11" in err or ">=3.11" in err
        assert "python3.14" in err  # looked-for list names the new candidate
        assert "3.14" in err  # remedy names 3.14 on every OS
        assert expect in err
        assert forbid not in err

    def test_no_python_remedy_chosen_by_os(self) -> None:
        mod = load_check()
        darwin = mod._fix_no_python([], platform="darwin")
        assert "install Python 3.13 or 3.14" in darwin
        assert "do not run xcode-select --install" in darwin
        linux = mod._fix_no_python([], platform="linux")
        assert "distro Python 3.11+" in linux
        assert "3.14 preferred" in linux
        assert "xcode-select --install" not in linux

    def test_launcher_and_driver_candidate_parity(self) -> None:
        import re

        mod = load_check()
        text = (ROOT / "bin" / "check").read_text(encoding="utf-8")
        match = re.search(r"for cand in ([^;]+); do", text)
        assert match, "launcher candidate loop not found"
        assert tuple(match.group(1).split()) == mod.CANDIDATES
        assert "python3.14" in text

    @pytest.mark.parametrize(
        "uname,expect,forbid",
        [
            ("Darwin", "do not run xcode-select --install", "distro Python"),
            ("Linux", "install a distro Python 3.11+", "xcode-select --install"),
        ],
    )
    def test_launcher_no_python_remedy_by_os(
        self, tmp_path: Path, uname: str, expect: str, forbid: str
    ) -> None:
        """The POSIX launcher picks its no-Python remedy from `uname -s`."""
        bindir = tmp_path / "bin"
        bindir.mkdir(parents=True)
        dirname = shutil.which("dirname")
        assert dirname, "dirname not on PATH"
        (bindir / "dirname").symlink_to(dirname)
        stub = bindir / "uname"
        stub.write_text(f"#!/bin/sh\necho {uname}\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        proc = subprocess.run(
            ["/bin/sh", str(ROOT / "bin" / "check"), "--help"],
            env={"PATH": str(bindir), "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 1, proc.stderr
        assert "looked for python3.13, python3.14," in proc.stderr
        assert expect in proc.stderr
        assert forbid not in proc.stderr

    def test_launcher_finds_named_python3_14(self, tmp_path: Path) -> None:
        """The POSIX launcher execs the driver under a stub python3.14."""
        bindir = tmp_path / "bin"
        bindir.mkdir(parents=True)
        marker = tmp_path / "stub-argv.txt"
        stub = bindir / "python3.14"
        stub.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$MM_STUB_MARKER"\necho 3.14.7\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        # PATH holds only the stub and `dirname`: a host /usr/bin/python3.13
        # would otherwise win under the 3.13-first candidate order.
        dirname = shutil.which("dirname")
        assert dirname, "dirname not on PATH"
        (bindir / "dirname").symlink_to(dirname)
        env = os.environ.copy()
        env["PATH"] = str(bindir)
        env.pop("MM_PYTHON", None)
        env["MM_STUB_MARKER"] = str(marker)
        proc = subprocess.run(
            ["/bin/sh", str(ROOT / "bin" / "check"), "--help"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert marker.exists(), "launcher did not exec the python3.14 stub"
        assert "_check.py" in marker.read_text(encoding="utf-8")


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

    def test_unowned_healthy_venv_is_used_not_claimed(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A developer-managed .venv must stay unowned. Claiming it
        would let a later --rebuild or health miss rmtree it."""
        mod = load_check()
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        venv = root / ".venv"
        venv.mkdir()
        sentinel = venv / "keep-me"
        sentinel.write_text("stay\n", encoding="utf-8")
        (venv / "bin").mkdir()
        py = venv / "bin" / "python"
        py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        py.chmod(py.stat().st_mode | stat.S_IXUSR)

        monkeypatch.setattr(mod, "venv_healthy", lambda _p: True)

        def boom_create(*_a, **_k):
            raise AssertionError("must not create a venv when one already exists")

        pip_calls: list[object] = []
        monkeypatch.setattr(mod, "_create_venv", boom_create)
        monkeypatch.setattr(mod, "_pip_install", lambda *_a, **_k: pip_calls.append(1))

        got = mod.ensure_venv(root, sys.executable, (3, 13, 0), rebuild=False)
        assert got == venv
        assert sentinel.is_file(), "unowned venv was mutated"
        assert not (venv / ".mm-check-owner").is_file(), "claimed a venv we did not create"
        assert pip_calls == []
        err = capsys.readouterr().err
        assert "unowned" in err
        assert "will not rebuild or delete" in err

    def test_rebuild_refuses_unowned_venv(self, tmp_path: Path, monkeypatch, capsys) -> None:
        mod = load_check()
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        venv = root / ".venv"
        venv.mkdir()
        sentinel = venv / "keep-me"
        sentinel.write_text("stay\n", encoding="utf-8")
        (venv / "bin").mkdir()
        py = venv / "bin" / "python"
        py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        py.chmod(py.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setattr(mod, "venv_healthy", lambda _p: True)
        with pytest.raises(SystemExit) as ei:
            mod.ensure_venv(root, sys.executable, (3, 13, 0), rebuild=True)
        assert ei.value.code != 0
        assert sentinel.is_file(), "--rebuild deleted a venv it does not own"
        err = capsys.readouterr().err
        assert "not owned" in err
        assert "mv .venv .venv.bak" in err

    def test_pip_install_passes_a_timeout(self, tmp_path: Path, monkeypatch) -> None:
        mod = load_check()
        seen: dict[str, object] = {}

        def fake_run(*_a, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(["pip"], 0)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        mod._pip_install(sys.executable, tmp_path)
        assert seen.get("timeout") == mod.PIP_TIMEOUT_S

    def test_pip_install_failure_preserves_its_exit_code(self, tmp_path: Path, monkeypatch) -> None:
        mod = load_check()
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(["pip"], 47),
        )

        with pytest.raises(SystemExit) as ei:
            mod._pip_install(sys.executable, tmp_path)

        assert ei.value.code == 47

    def test_pip_install_timeout_has_manual_recovery(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        mod = load_check()

        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["pip"], mod.PIP_TIMEOUT_S)

        monkeypatch.setattr(mod.subprocess, "run", timeout)
        with pytest.raises(SystemExit):
            mod._pip_install(sys.executable, tmp_path)

        err = capsys.readouterr().err
        assert f"timed out after {mod.PIP_TIMEOUT_S}s" in err
        assert "manual fallback" in err

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

    def test_valid_mm_venv_is_used_without_bootstrap(self, tmp_path: Path, monkeypatch) -> None:
        mod = load_check()
        venv = tmp_path / "external"
        py = _fake_venv(venv)
        sentinel = venv / "sentinel"
        sentinel.write_text("stay\n", encoding="utf-8")
        monkeypatch.setenv("MM_VENV", str(venv))
        monkeypatch.setattr(mod, "venv_healthy", lambda _python: True)

        assert mod._mm_venv_python() == py
        assert sentinel.read_text(encoding="utf-8") == "stay\n"

    def test_owned_broken_venv_is_rebuilt(self, tmp_path: Path, monkeypatch) -> None:
        mod = load_check()
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        _fake_venv(root / ".venv")
        (root / ".venv" / "stale").write_text("old\n", encoding="utf-8")
        pip_calls: list[str] = []
        monkeypatch.setattr(mod, "venv_healthy", lambda _python: False)
        monkeypatch.setattr(mod, "_create_venv", lambda _python, venv: _fake_venv(venv))
        monkeypatch.setattr(mod, "_pip_install", lambda python, _root: pip_calls.append(python))

        rebuilt = mod.ensure_venv(root, sys.executable, (3, 13, 0), rebuild=False)

        assert rebuilt == root / ".venv"
        assert not (rebuilt / "stale").exists()
        assert mod.venv_owned(rebuilt)
        assert len(pip_calls) == 1

    def test_virtual_env_is_used_when_bootstrap_is_skipped(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        mod = load_check()
        venv = tmp_path / "external"
        py = _fake_venv(venv)
        args = mod.parse_argv(["--no-bootstrap"])
        monkeypatch.delenv("MM_VENV", raising=False)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        assert mod.tool_python(args, tmp_path, None) == py

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
        "--affected",
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


# --- Track 82B: --affected selection and owned pytest process groups ---------

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]

PROJECT_FILES = {
    "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n',
    ".gitignore": ".mm-test-selection/\n__pycache__/\n.pytest_cache/\n",
    "src/mind_meld/__init__.py": "",
    "src/mind_meld/leaf.py": "def leaf():\n    return 1\n",
    "src/mind_meld/shared.py": (
        "from mind_meld import leaf\n\n\ndef shared():\n    return leaf.leaf() + 1\n"
    ),
    "tests/conftest.py": (
        "import os\n\nimport pytest\n\n\n@pytest.fixture(autouse=True)\n"
        "def _ran(request):\n    log = os.environ.get('RAN_LOG')\n    if log:\n"
        "        with open(log, 'a') as fh:\n            fh.write(request.node.nodeid + '\\n')\n"
    ),
    "tests/test_leaf.py": (
        "import pytest\nfrom mind_meld import leaf\n\n\n@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_leaf(n):\n    assert leaf.leaf() == 1\n"
    ),
    "tests/test_shared.py": (
        "from mind_meld import shared\n\n\ndef test_shared():\n    assert shared.shared() == 2\n"
    ),
    "tests/test_other.py": "def test_other():\n    assert True\n",
}


def make_project(root: Path, files: dict[str, str] | None = None) -> Path:
    for name, body in {**PROJECT_FILES, **(files or {})}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run([*GIT, "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run([*GIT, "-C", str(root), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-q", "-m", "base"], check=True)
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def git(root: Path, *args: str) -> None:
    subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True)


def check(root: Path, *args: str, env: dict | None = None, timeout: float = 180):
    full = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VIRTUAL_ENV", "MM_VENV", "PYTEST_ADDOPTS", "MM_PYTEST_WORKERS")
    }
    full.update(MM_CHECK_ROOT=str(root), MM_PYTHON=sys.executable, RAN_LOG=str(ran_log(root)))
    full.update(env or {})
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "_check.py"), "--no-bootstrap", "--tests", *args],
        capture_output=True,
        text=True,
        cwd=root,
        env=full,
        timeout=timeout,
    )


def ran_log(root: Path) -> Path:
    return root.parent / (root.name + ".ran")


def ran(root: Path) -> set[str]:
    log = ran_log(root)
    seen = set(log.read_text().split()) if log.exists() else set()
    log.unlink(missing_ok=True)
    return seen


def both(proc) -> str:
    return proc.stdout + proc.stderr


LEAF_TESTS = {
    "tests/test_leaf.py::test_leaf[1]",
    "tests/test_leaf.py::test_leaf[2]",
    "tests/test_shared.py::test_shared",
}
ALL_TESTS = LEAF_TESTS | {"tests/test_other.py::test_other"}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("seeded")
    root = make_project(base / "proj")
    proc = check(root, "--affected", "--serial")
    assert proc.returncode == 0, both(proc)
    assert "recorded coverage for 4 tests" in proc.stderr
    assert ran(root) == ALL_TESTS
    return root


@pytest.fixture
def proj(seeded, tmp_path) -> Path:
    root = tmp_path / "proj"
    shutil.copytree(seeded, root, symlinks=True)
    return root


def edit(root: Path, name: str, old: str, new: str) -> None:
    path = root / name
    text = path.read_text()
    assert old in text
    stat_before = path.stat()
    path.write_text(text.replace(old, new))
    # Same-size edits inside one second would keep a stale .pyc valid.
    later = stat_before.st_mtime_ns + 10_000_000_000
    os.utime(path, ns=(later, later))


def affected(root: Path, *args: str, **kw):
    return check(root, "--affected", "--serial", *args, **kw)


class TestAffectedArgv:
    def test_flags_parse(self) -> None:
        mod = load_check()
        args = mod.parse_argv(["--affected", "--refresh", "--base", "main"])
        assert (args.affected, args.refresh, args.base) == (True, True, "main")
        assert mod.parse_argv(["--affected", "--base=dev"]).base == "dev"
        assert mod.parse_argv(["--affected"]).scope_given is False
        assert mod.parse_argv(["tests/x.py"]).scope_given is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["--affected", "tests/x.py"],
            ["--affected", "--", "-k", "x"],
            ["--affected", "-k", "x"],
            ["--affected", "--lint"],
            ["--refresh"],
            ["--base", "main"],
            ["--affected", "--base"],
        ],
    )
    def test_competing_scopes_and_orphan_flags_are_rejected_before_work(
        self, argv: list[str], monkeypatch
    ) -> None:
        mod = load_check()
        monkeypatch.setattr(mod, "resolve_interpreter", lambda: pytest.fail("bootstrapped"))
        with pytest.raises(SystemExit) as ei:
            mod.main(argv)
        assert ei.value.code != 0

    def test_ordinary_argv_is_unchanged(self) -> None:
        mod = load_check()
        mod.validate_affected(mod.parse_argv(["--affected", "--tests", "--serial"]))
        args = mod.parse_argv(["tests/x.py", "--tests", "--serial", "-k", "n"])
        assert (args.affected, args.refresh, args.base) == (False, False, None)
        assert args.scope == ["tests/x.py"] and args.pytest_extra == ["-k", "n"]

    def test_help_says_when_to_use_affected(self, capsys) -> None:
        mod = load_check()
        mod.main(["--help"])
        out = capsys.readouterr().out
        for needle in ("--affected", "--base", "--refresh", "CI"):
            assert needle in out


class TestAffectedSelection:
    def test_leaf_edit_runs_only_the_tests_that_reach_it_and_is_not_last_run_based(
        self, proj: Path
    ) -> None:
        edit(proj, "src/mind_meld/leaf.py", "return 1", "return int(1)")
        for _ in range(2):
            proc = affected(proj)
            assert proc.returncode == 0, both(proc)
            assert "ran 3 of 4 collected tests" in both(proc)
            assert ran(proj) == LEAF_TESTS

    def test_shared_edit_reaches_its_callers_only(self, proj: Path) -> None:
        edit(proj, "src/mind_meld/shared.py", "+ 1", "+ int(1)")
        assert affected(proj).returncode == 0
        assert ran(proj) == {"tests/test_shared.py::test_shared"}

    def test_committed_staged_unstaged_and_untracked_changes_all_count(self, proj: Path) -> None:
        git(proj, "checkout", "-q", "-b", "work")
        edit(proj, "src/mind_meld/leaf.py", "return 1", "return int(1)")
        git(proj, "commit", "-q", "-am", "leaf")
        edit(proj, "tests/test_other.py", "True", "1 == 1")
        git(proj, "add", "tests/test_other.py")
        (proj / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
        proc = affected(proj)
        assert proc.returncode == 0, both(proc)
        assert ran(proj) == ALL_TESTS | {"tests/test_new.py::test_new"}

    def test_deleted_test_is_a_proven_empty_selection(self, proj: Path) -> None:
        git(proj, "rm", "-q", "tests/test_other.py")
        proc = affected(proj)
        assert proc.returncode == 0, both(proc)
        assert "ran 0 of 4" in both(proc)
        assert ran(proj) == set()

    def test_renamed_test_runs_under_its_new_name(self, proj: Path) -> None:
        git(proj, "mv", "tests/test_other.py", "tests/test_renamed.py")
        assert affected(proj).returncode == 0
        assert ran(proj) == {"tests/test_renamed.py::test_other"}

    def test_unchanged_tree_runs_nothing_and_succeeds(self, proj: Path) -> None:
        proc = affected(proj)
        assert proc.returncode == 0
        assert "ran 0 of 4" in both(proc)
        assert ran(proj) == set()

    @pytest.mark.parametrize(
        ("name", "text", "why"),
        [
            (
                "src/mind_meld/leaf.py",
                "\nCONST = 1\n",
                "module-scope change in src/mind_meld/leaf.py",
            ),
            ("README.md", "hello\n", "non-Python file changed: README.md"),
            (
                "tests/conftest.py",
                "\n# x\n",
                "runner, pyproject or conftest changed since the record",
            ),
            ("pyproject.toml", "\n# x\n", "runner, pyproject or conftest changed since the record"),
            ("src/mind_meld/new.py", "X = 1\n", "module added: src/mind_meld/new.py"),
        ],
    )
    def test_changes_the_record_cannot_attribute_run_everything_and_say_why(
        self, proj: Path, name: str, text: str, why: str
    ) -> None:
        path = proj / name
        path.write_text((path.read_text() if path.exists() else "") + text)
        proc = affected(proj)
        assert proc.returncode == 0, both(proc)
        assert why in proc.stderr and "running the full suite" in proc.stderr
        assert ran(proj) == ALL_TESTS

    def test_tests_that_read_source_or_spawn_python_are_never_excluded(
        self, tmp_path: Path
    ) -> None:
        """Coverage cannot see source read as data or code run in a child."""
        reader = (
            "def test_reader():\n    assert 'def leaf' in open('src/mind_meld/leaf.py').read()\n"
        )
        child = (
            "import subprocess, sys\n\n\ndef test_child():\n"
            "    code = 'import sys; sys.path.insert(0, \"src\"); import mind_meld.leaf'\n"
            "    assert subprocess.run([sys.executable, '-c', code]).returncode == 0\n"
        )
        root = make_project(
            tmp_path / "proj", {"tests/test_reader.py": reader, "tests/test_child.py": child}
        )
        assert affected(root).returncode == 0
        ran(root)
        edit(root, "src/mind_meld/leaf.py", "return 1", "return int(1)")
        proc = affected(root)
        assert proc.returncode == 0, both(proc)
        assert ran(root) == LEAF_TESTS | {
            "tests/test_reader.py::test_reader",
            "tests/test_child.py::test_child",
        }

    def test_config_that_differs_from_the_merge_base_is_never_narrowed(
        self, tmp_path: Path
    ) -> None:
        root = make_project(tmp_path / "proj")
        edit(root, "tests/conftest.py", "import os", "import os  # edited")
        assert affected(root).returncode == 0
        ran(root)
        proc = affected(root)
        assert "conftest changed: tests/conftest.py" in proc.stderr
        assert ran(root) == ALL_TESTS

    def test_failures_are_reselected_until_they_pass(self, proj: Path) -> None:
        edit(proj, "src/mind_meld/leaf.py", "return 1", "return 2")
        assert affected(proj).returncode != 0
        ran(proj)
        git(proj, "checkout", "--", "src/mind_meld/leaf.py")
        proc = affected(proj)
        assert proc.returncode == 0, both(proc)
        assert ran(proj) == LEAF_TESTS
        assert "ran 0 of 4" in both(affected(proj))

    def test_collection_failure_is_not_converted_to_success(self, proj: Path) -> None:
        (proj / "tests" / "test_broken.py").write_text("def test_x(:\n")
        proc = affected(proj)
        assert proc.returncode != 0
        assert (proj / ".mm-test-selection" / "record.json").is_file()

    def test_xdist_record_and_selection(self, tmp_path: Path) -> None:
        root = make_project(tmp_path / "proj")
        env = {"MM_PYTEST_WORKERS": "2"}
        proc = check(root, "--affected", env=env)
        assert proc.returncode == 0, both(proc)
        assert ran(root) == ALL_TESTS
        record = json.loads((root / ".mm-test-selection" / "record.json").read_text())
        assert len(record["nodes"]) == 4
        edit(root, "src/mind_meld/leaf.py", "return 1", "return int(1)")
        proc = check(root, "--affected", env=env)
        assert proc.returncode == 0, both(proc)
        assert "ran 3 of 4 collected tests" in both(proc)
        assert ran(root) == LEAF_TESTS

    def test_refresh_rerecords_the_whole_suite(self, proj: Path) -> None:
        (proj / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
        proc = affected(proj, "--refresh")
        assert proc.returncode == 0, both(proc)
        assert "refresh requested" in proc.stderr
        record = json.loads((proj / ".mm-test-selection" / "record.json").read_text())
        assert len(record["nodes"]) == 5

    @pytest.mark.parametrize(
        ("mutate", "why"),
        [
            (lambda r, p: p.write_text(p.read_text()[:40]), "unreadable or truncated"),
            (lambda r, p: p.write_text(json.dumps({**r, "schema": 99})), "unknown schema"),
            (lambda r, p: p.write_text(json.dumps({**r, "env": "x"})), "interpreter or installed"),
            (lambda r, p: p.write_text(json.dumps({**r, "config": "x"})), "runner, pyproject"),
            (lambda r, p: p.write_text(json.dumps({**r, "nodes": []})), "holds no tests"),
            (lambda r, p: p.write_text(json.dumps({**r, "ctx": {}})), "no coverage of src"),
            (lambda r, p: p.write_text(json.dumps({**r, "complete": False})), "unknown schema"),
        ],
    )
    def test_unusable_record_runs_everything_with_the_reason_and_reseeds(
        self, proj: Path, mutate, why: str
    ) -> None:
        path = proj / ".mm-test-selection" / "record.json"
        mutate(json.loads(path.read_text()), path)
        proc = affected(proj)
        assert proc.returncode == 0, both(proc)
        assert why in proc.stderr
        assert ran(proj) == ALL_TESTS
        assert len(json.loads(path.read_text())["nodes"]) == 4

    def test_moved_merge_base_reseeds(self, proj: Path) -> None:
        git(proj, "commit", "-q", "--allow-empty", "-m", "moved")
        git(proj, "update-ref", "refs/remotes/origin/main", "HEAD")
        proc = affected(proj)
        assert "merge base moved" in proc.stderr
        assert ran(proj) == ALL_TESTS

    def test_filtered_run_never_becomes_the_baseline(self, tmp_path: Path) -> None:
        root = make_project(tmp_path / "proj")
        record = root / ".mm-test-selection" / "record.json"
        proc = affected(root, env={"PYTEST_ADDOPTS": "-k test_other"})
        assert proc.returncode == 0, both(proc)
        assert "record not updated" in proc.stderr and "keyword" in proc.stderr
        assert not record.exists()
        ran(root)
        assert affected(root).returncode == 0
        assert ran(root) == ALL_TESTS
        assert record.is_file()

    def test_inputs_changing_during_the_run_block_promotion(self, tmp_path: Path) -> None:
        touch = (
            "import os\n\n\ndef test_other():\n"
            "    with open(os.path.join(os.environ['PROJ'], 'src/mind_meld/leaf.py'), 'a') as f:\n"
            "        f.write('# changed mid-run\\n')\n"
        )
        root = make_project(tmp_path / "proj", {"tests/test_other.py": touch})
        proc = affected(root, env={"PROJ": str(root)})
        assert proc.returncode == 0, both(proc)
        assert "files changed while the suite ran" in proc.stderr
        assert not (root / ".mm-test-selection" / "record.json").exists()

    def test_a_second_recorder_is_refused_without_touching_the_baseline(self, proj: Path) -> None:
        import fcntl

        record = proj / ".mm-test-selection" / "record.json"
        before = record.read_bytes()
        fd = os.open(proj / ".mm-test-selection" / ".lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            proc = affected(proj, "--refresh")
        finally:
            os.close(fd)
        assert proc.returncode != 0
        assert "another --affected run is recording" in proc.stderr
        assert ran(proj) == set()
        assert record.read_bytes() == before

    def test_a_red_refresh_keeps_the_prior_record(self, proj: Path) -> None:
        record = proj / ".mm-test-selection" / "record.json"
        before = record.read_bytes()
        edit(proj, "tests/test_other.py", "assert True", "assert False")
        proc = affected(proj, "--refresh")
        assert proc.returncode != 0
        assert record.read_bytes() == before

    def test_unresolved_base_runs_everything_and_explains(self, proj: Path) -> None:
        proc = affected(proj, "--base", "nosuch")
        assert proc.returncode == 0, proc.stderr
        for needle in ("resolve base", "code=git-failed", "base=nosuch", "--base REF"):
            assert needle in proc.stderr
        assert "running the full suite instead" in proc.stderr
        assert ran(proj) == ALL_TESTS

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
    def test_unwritable_cache_falls_back_with_the_cause(self, tmp_path: Path) -> None:
        root = make_project(tmp_path / "proj")
        root.chmod(0o500)
        try:
            proc = affected(root)
        finally:
            root.chmod(0o700)
        assert proc.returncode == 0, both(proc)
        assert "code=cache-unwritable" in proc.stderr
        assert ran(root) == ALL_TESTS

    def test_missing_coverage_package_is_reported_and_falls_back(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        mod = load_check()
        root = make_project(tmp_path / "proj")
        monkeypatch.setattr(mod, "probe_env", lambda tool: {"fp": "x", "coverage": False})
        monkeypatch.setattr(mod, "full_pytest", lambda *a: 0)
        args = mod.parse_argv(["--affected"])
        assert mod.run_affected(args, root, Path(sys.executable), []) == 0
        err = capsys.readouterr().err
        assert "coverage-missing" in err and "running the full suite instead" in err

    def test_unexpected_selector_error_keeps_the_type_and_falls_back(
        self, proj: Path, monkeypatch, capsys
    ) -> None:
        mod = load_check()

        def boom(*_a):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mod, "changed_since", boom)
        monkeypatch.setattr(mod, "full_pytest", lambda *a: 7)
        args = mod.parse_argv(["--affected"])
        assert mod.run_affected(args, proj, Path(sys.executable), []) == 7
        err = capsys.readouterr().err
        assert "RuntimeError: kaboom" in err and "code=unexpected" in err

    def test_symlinked_or_foreign_cache_is_refused(self, tmp_path: Path) -> None:
        mod = load_check()
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / ".mm-test-selection").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(mod.SelectError) as ei:
            mod.ensure_select_dir(tmp_path)
        assert ei.value.code == "cache-symlink"
        other = tmp_path / "other"
        (other / ".mm-test-selection").mkdir(parents=True)
        (other / ".mm-test-selection" / "stuff").write_text("x")
        with pytest.raises(mod.SelectError) as ei:
            mod.ensure_select_dir(other)
        assert ei.value.code == "cache-unowned"


class TestSkeleton:
    @pytest.mark.parametrize(
        ("old", "new", "same"),
        [
            ("def f():\n    return 1\n", "def f():\n    return 2\n", True),
            ("def f():\n    return 1\n", "def f(a=3):\n    return 1\n", True),
            ("def f():\n    return 1\n", "def f():\n    return 1\n\n\ndef g():\n    pass\n", True),
            ("X = 1\n", "X = 2\n", False),
            ("import os\n", "import os\nimport sys\n", False),
            ("@d\ndef f():\n    return 1\n", "@d\ndef f():\n    return 2\n", True),
            ("@d\ndef f():\n    pass\n", "@e\ndef f():\n    pass\n", False),
            (
                "class A:\n    def m(self):\n        return 1\n",
                "class A:\n    def m(self):\n        return 2\n",
                True,
            ),
            ("class A:\n    x = 1\n", "class A:\n    x = 2\n", False),
        ],
    )
    def test_only_function_bodies_are_invisible(self, old: str, new: str, same: bool) -> None:
        mod = load_check()
        a, b = mod.skeleton_digest(old.encode()), mod.skeleton_digest(new.encode())
        assert (a == b) is same

    def test_unparseable_source_has_no_skeleton(self) -> None:
        assert load_check().skeleton_digest(b"def (:\n") is None


def _wait_gone(pid: int, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def sentinel():
    proc = subprocess.Popen(["sleep", "120"], start_new_session=True)
    yield proc
    proc.kill()
    proc.wait()


LEADER = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "sys.exit(int(sys.argv[2]))\n"
)


class TestOwnedGroup:
    def _leader(self, tmp_path: Path, code: int) -> tuple[list[str], Path]:
        script = tmp_path / "leader.py"
        script.write_text(LEADER)
        pidfile = tmp_path / "child.pid"
        return [sys.executable, str(script), str(pidfile), str(code)], pidfile

    @pytest.mark.parametrize("code", [0, 3, 5])
    def test_run_end_leaves_no_owned_worker_and_keeps_the_result(
        self, tmp_path: Path, monkeypatch, sentinel, code: int
    ) -> None:
        mod = load_check()
        monkeypatch.setattr(mod, "GROUP_GRACE_S", 0.05)
        argv, pidfile = self._leader(tmp_path, code)
        assert mod.run_owned(argv) == code
        assert _wait_gone(int(pidfile.read_text()))
        assert sentinel.poll() is None

    def test_group_that_already_ended_is_success(self, monkeypatch) -> None:
        mod = load_check()
        assert mod.run_owned([sys.executable, "-c", "raise SystemExit(4)"]) == 4

    def test_a_failed_spawn_signals_no_group(self, monkeypatch) -> None:
        mod = load_check()
        calls: list[tuple] = []
        monkeypatch.setattr(mod.os, "killpg", lambda *a: calls.append(a))
        with pytest.raises(FileNotFoundError):
            mod.run_owned(["/nonexistent/pytest"])
        assert calls == []

    @pytest.mark.parametrize("cause", ["stuck", "denied"])
    def test_cleanup_failure_is_distinct_and_names_the_owned_group(
        self, tmp_path: Path, monkeypatch, capsys, cause: str
    ) -> None:
        mod = load_check()
        for name in ("GROUP_GRACE_S", "GROUP_TERM_S", "GROUP_KILL_S"):
            monkeypatch.setattr(mod, name, 0.05)
        argv, pidfile = self._leader(tmp_path, 3)
        if cause == "stuck":
            monkeypatch.setattr(mod, "_signal_group", lambda *a: None)
            monkeypatch.setattr(mod, "_group_alive", lambda pgid: True)
        else:

            def denied(*_a):
                raise PermissionError("nope")

            monkeypatch.setattr(mod, "_signal_group", denied)
            monkeypatch.setattr(mod, "_group_alive", lambda pgid: True)
        try:
            with pytest.raises(SystemExit) as ei:
                mod.run_owned(argv)
        finally:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        assert ei.value.code == mod.CLEANUP_FAILED_CODE
        err = capsys.readouterr().err
        assert "pytest result: exit 3" in err and "group " in err
        assert "kill -9" not in err and "killall" not in err and "pkill" not in err

    def test_launcher_keeps_exec_so_signals_reach_the_driver(self, tmp_path: Path) -> None:
        stub = tmp_path / "py"
        pidfile = tmp_path / "pid"
        stub.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\n')
        stub.chmod(0o755)
        proc = subprocess.Popen(
            [str(ROOT / "bin" / "check")], env={**os.environ, "MM_PYTHON": str(stub)}
        )
        proc.wait(timeout=30)
        assert pidfile.read_text().strip() == str(proc.pid)


HANG = (
    "import os, signal, subprocess, sys, time\n\n\ndef test_hang():\n"
    "    ready = os.environ['READY']\n"
    "    code = 'import time; time.sleep(300)'\n"
    "    if os.environ.get('STUBBORN'):\n"
    "        for s in (signal.SIGINT, signal.SIGTERM):\n"
    "            signal.signal(s, signal.SIG_IGN)\n"
    "        code = ('import signal, time; [signal.signal(s, signal.SIG_IGN) for s in "
    "(signal.SIGINT, signal.SIGTERM)]; time.sleep(300)')\n"
    "    child = subprocess.Popen([sys.executable, '-c', code])\n"
    "    with open(os.path.join(ready, 'p%d' % os.getpid()), 'w') as fh:\n"
    "        fh.write('%d %d' % (os.getpid(), child.pid))\n"
    "    time.sleep(300)\n"
)


def _hang_project(tmp_path: Path, copies: int = 1) -> tuple[Path, Path]:
    root = tmp_path / "hang"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    for i in range(copies):
        (root / "tests" / f"test_hang{i}.py").write_text(HANG)
    ready = tmp_path / "ready"
    ready.mkdir()
    return root, ready


def _start_driver(root: Path, ready: Path, *args: str, env: dict | None = None):
    full = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VIRTUAL_ENV", "MM_VENV", "PYTEST_ADDOPTS", "MM_PYTEST_WORKERS")
    }
    full.update(MM_CHECK_ROOT=str(root), MM_PYTHON=sys.executable, READY=str(ready))
    full.update(env or {})
    return subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "_check.py"), "--no-bootstrap", "--tests", *args],
        env=full,
        cwd=root,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_ready(ready: Path, count: int, seconds: float = 60.0) -> list[int]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        files = list(ready.glob("p*"))
        if len(files) >= count and all(f.read_text().count(" ") for f in files):
            return [int(p) for f in files for p in f.read_text().split()]
        time.sleep(0.05)
    raise AssertionError("tests never signalled readiness")


class TestInterruptCleanup:
    @pytest.mark.parametrize(
        ("sig", "status"),
        [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)],
    )
    def test_signal_to_the_driver_ends_pytest_and_its_workers(
        self, tmp_path: Path, sentinel, sig, status: int
    ) -> None:
        root, ready = _hang_project(tmp_path)
        driver = _start_driver(root, ready, "--serial")
        pids = _wait_ready(ready, 1)
        os.kill(driver.pid, sig)
        assert driver.wait(timeout=60) == status
        assert all(_wait_gone(pid) for pid in pids)
        assert sentinel.poll() is None

    def test_real_xdist_workers_do_not_survive_an_interrupt(self, tmp_path: Path, sentinel) -> None:
        root, ready = _hang_project(tmp_path, copies=2)
        driver = _start_driver(root, ready, env={"MM_PYTEST_WORKERS": "2"})
        pids = _wait_ready(ready, 2)
        os.kill(driver.pid, signal.SIGINT)
        assert driver.wait(timeout=60) == 130
        assert all(_wait_gone(pid) for pid in pids)
        assert sentinel.poll() is None

    def test_repeated_interrupts_still_finish_cleanup_in_bounded_time(
        self, tmp_path: Path, sentinel
    ) -> None:
        root, ready = _hang_project(tmp_path)
        driver = _start_driver(root, ready, "--serial", env={"STUBBORN": "1"})
        pids = _wait_ready(ready, 1)
        os.kill(driver.pid, signal.SIGINT)
        time.sleep(0.5)
        os.kill(driver.pid, signal.SIGINT)
        assert driver.wait(timeout=60) == 130
        assert all(_wait_gone(pid) for pid in pids)
        assert sentinel.poll() is None

    def test_signal_during_startup_still_ends_cleanly(self, tmp_path: Path, sentinel) -> None:
        root, ready = _hang_project(tmp_path)
        driver = _start_driver(root, ready, "--serial")
        assert driver.stderr is not None
        for line in driver.stderr:
            if line.startswith("bin/check: pytest"):
                break
        os.kill(driver.pid, signal.SIGINT)
        assert driver.wait(timeout=60) == 130
        for f in ready.glob("p*"):
            assert all(_wait_gone(int(p)) for p in f.read_text().split())
        assert sentinel.poll() is None

    def test_pipe_stdin_and_pty_output_still_work_in_the_new_session(self, tmp_path: Path) -> None:
        import pty

        root = make_project(tmp_path / "proj")
        master, slave = pty.openpty()
        env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "MM_VENV")}
        env.update(MM_CHECK_ROOT=str(root), MM_PYTHON=sys.executable)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "bin" / "_check.py"),
                "--no-bootstrap",
                "--tests",
                "--serial",
            ],
            stdin=subprocess.PIPE,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=root,
        )
        os.close(slave)
        assert proc.stdin is not None
        proc.stdin.close()
        out = b""
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.close(master)
        assert proc.wait(timeout=30) == 0
        assert b"4 passed" in out
