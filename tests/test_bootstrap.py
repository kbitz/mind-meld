"""Behavioral and contract tests for bin/bootstrap (Track 98A)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from mind_meld import upgrade

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bin" / "bootstrap"
README = ROOT / "README.md"

README_BOOTSTRAP_CMD = (
    "mm_bootstrap=$(curl -fsSL https://raw.githubusercontent.com/kbitz/mind-meld/latest/bin/bootstrap)"
    ' && sh -c "$mm_bootstrap"'
)
HOMEBREW_INSTALL_CMD = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'

SYNCTHING_CONFIG_PATHS = (
    Path("Library/Application Support/Syncthing"),
    Path(".config/syncthing"),
    Path(".local/state/syncthing"),
)

_LOG_SNIPPET = """\
printf '%s' "{name}" >> "$MM_TEST_CALL_LOG"
for _arg in "$@"; do
  printf '\\t%s' "$_arg" >> "$MM_TEST_CALL_LOG"
done
printf '\\n' >> "$MM_TEST_CALL_LOG"
"""


def _write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_fake_script(path: Path, name: str, logic: str) -> Path:
    header = "#!/bin/sh\nset -eu\n" + _LOG_SNIPPET.format(name=name)
    return _write_executable(path, header + logic)


@dataclass
class BootstrapHarness:
    tmp_path: Path
    home: Path
    brewbin: Path
    shadow: Path
    pipxbin: Path
    sysbin: Path
    tools: Path
    venvs: Path
    call_log: Path
    pipx_state: Path
    os_release: Path

    def install_uname(self, value: str) -> None:
        _make_fake_script(self.sysbin / "uname", "uname", f'printf "%s\\n" "{value}"\n')

    def install_sudo(self) -> None:
        _make_fake_script(self.sysbin / "sudo", "sudo", 'exec "$@"\n')

    def install_python(self, directory: Path, version: str = "3.13.2") -> None:
        _make_fake_script(
            directory / "python3",
            "python3",
            f'printf "Python %s\\n" "{version}"\n',
        )

    def install_syncthing(self, directory: Path, version: str = "2.1.7") -> None:
        _make_fake_script(
            directory / "syncthing",
            "syncthing",
            f"""\
if [ "${{#}}" -eq 1 ] && [ "$1" = "--version" ]; then
  printf 'syncthing v%s "Gold Grasshopper" (go1.24.1 test-arch) build@syncthing.net\\n' "{version}"
  exit 0
fi
exit 2
""",
        )

    def install_git(self, directory: Path) -> None:
        _make_fake_script(directory / "git", "git", "exit 0\n")

    def install_mm(self, directory: Path) -> None:
        _make_fake_script(directory / "mm", "mm", "exit 0\n")

    def install_curl(self, *, serve: Path | None) -> None:
        # Serves `serve` like `curl -fsSL` would, or fails with curl's HTTP error exit.
        if serve is None:
            logic = "exit 22\n"
        else:
            logic = f"""\
while IFS= read -r _line || [ -n "$_line" ]; do
  printf '%s\\n' "$_line"
done < "{serve}"
"""
        _make_fake_script(self.sysbin / "curl", "curl", logic)

    def install_systemctl(self) -> None:
        _make_fake_script(
            self.sysbin / "systemctl",
            "systemctl",
            """\
if [ "${1:-}" = "--user" ] && [ "${2:-}" = "enable" ] \\
   && [ "${3:-}" = "--now" ] && [ "${4:-}" = "syncthing.service" ]; then
  exit "${MM_TEST_START_FAIL:-0}"
fi
exit 2
""",
        )

    def install_pgrep(self, *, found: bool) -> None:
        code = 0 if found else 1
        _make_fake_script(self.sysbin / "pgrep", "pgrep", f"exit {code}\n")

    def install_pipx(self, directory: Path) -> None:
        _make_fake_script(
            directory / "pipx",
            "pipx",
            """\
case "${1:-}" in
  list)
    if [ -s "$MM_TEST_PIPX_STATE" ]; then
      while IFS= read -r _line || [ -n "$_line" ]; do
        printf '%s\\n' "$_line"
      done < "$MM_TEST_PIPX_STATE"
    fi
    # Real pipx exits 1 when any venv has a problem, but still lists the rest.
    exit "${MM_TEST_PIPX_LIST_EXIT:-0}"
    ;;
  install)
    if [ "${MM_TEST_PIPX_FAIL:-0}" = "1" ]; then
      exit 1
    fi
    printf 'mind-meld 1.12.0\\n' > "$MM_TEST_PIPX_STATE"
    _venv_bin="$MM_TEST_PIPX_VENVS/mind-meld/bin"
    mkdir -p "$_venv_bin"
    printf '#!/bin/sh\\nprintf "mm\\\\n" >> "$MM_TEST_CALL_LOG"\\n' > "$_venv_bin/mm"
    chmod +x "$_venv_bin/mm"
    cat > "$_venv_bin/python" << 'EOF_VENV_PY'
#!/bin/sh
set -eu
printf '%s' "venv-python" >> "$MM_TEST_CALL_LOG"
for _arg in "$@"; do
  printf '\t%s' "$_arg" >> "$MM_TEST_CALL_LOG"
done
printf '\n' >> "$MM_TEST_CALL_LOG"
if [ "${MM_TEST_SYNCTHING_CHECK_EXIT:-0}" != "0" ]; then
  exit "${MM_TEST_SYNCTHING_CHECK_EXIT}"
fi
printf '%s\n' "${MM_TEST_SYNCTHING_CHECK:-pre-existing ok}"
exit 0
EOF_VENV_PY
    chmod +x "$_venv_bin/python"
    # Like pipx 1.18: an existing file at the app path is left in place and
    # install still exits 0, unless --force replaces it.
    if [ "${2:-}" = "--force" ] || ! [ -e "$MM_TEST_PIPX_BIN/mm" ]; then
      ln -sf "$_venv_bin/mm" "$MM_TEST_PIPX_BIN/mm"
    fi
    exit 0
    ;;
  environment)
    if [ "${MM_TEST_PIPX_ENV_FAIL:-0}" = "1" ]; then
      exit 1
    fi
    case "${3:-}" in
      PIPX_BIN_DIR) printf '%s\\n' "$MM_TEST_PIPX_BIN" ;;
      PIPX_LOCAL_VENVS) printf '%s\\n' "$MM_TEST_PIPX_VENVS" ;;
      *) exit 1 ;;
    esac
    exit 0
    ;;
  *)
    exit 2
    ;;
esac
""",
        )

    def install_brew(self) -> None:
        stage = self.tmp_path / "brew-stage"
        self.install_python(stage, "3.13.2")
        self.install_pipx(stage)
        self.install_syncthing(stage, "2.1.7")
        _make_fake_script(
            self.brewbin / "brew",
            "brew",
            f"""\
copy_exec() {{
  while IFS= read -r _line || [ -n "$_line" ]; do
    printf '%s\\n' "$_line"
  done < "$1" > "$2"
  chmod +x "$2"
}}
if [ "${{1:-}}" = "services" ] && [ "${{2:-}}" = "start" ] && [ "${{3:-}}" = "syncthing" ]; then
  exit "${{MM_TEST_START_FAIL:-0}}"
fi
if [ "${{1:-}}" = "install" ]; then
  if [ "${{MM_TEST_PKG_FAIL:-0}}" = "1" ]; then
    exit 1
  fi
  shift
  for _pkg in "$@"; do
    case "$_pkg" in
      python)
        if [ "${{MM_TEST_SKIP_BREW_PYTHON_LINK:-0}}" != "1" ]; then
          copy_exec "{stage}/python3" "{self.brewbin}/python3"
        fi
        ;;
      pipx)
        copy_exec "{stage}/pipx" "{self.brewbin}/pipx"
        ;;
      syncthing)
        copy_exec "{stage}/syncthing" "{self.brewbin}/syncthing"
        ;;
    esac
  done
  exit 0
fi
exit 2
""",
        )

    def install_pacman(self) -> None:
        stage = self.tmp_path / "pacman-stage"
        self.install_python(stage, "3.13.2")
        self.install_pipx(stage)
        self.install_syncthing(stage, "2.1.7")
        self.install_git(stage)
        _make_fake_script(
            self.sysbin / "pacman",
            "pacman",
            f"""\
copy_exec() {{
  while IFS= read -r _line || [ -n "$_line" ]; do
    printf '%s\\n' "$_line"
  done < "$1" > "$2"
  chmod +x "$2"
}}
if [ "${{1:-}}" = "-S" ]; then
  if [ "${{MM_TEST_PKG_FAIL:-0}}" = "1" ]; then
    exit 1
  fi
  shift
  for _pkg in "$@"; do
    case "$_pkg" in
      --needed) ;;
      python)
        copy_exec "{stage}/python3" "{self.sysbin}/python3"
        ;;
      python-pipx)
        copy_exec "{stage}/pipx" "{self.sysbin}/pipx"
        ;;
      syncthing)
        copy_exec "{stage}/syncthing" "{self.sysbin}/syncthing"
        ;;
      git)
        copy_exec "{stage}/git" "{self.sysbin}/git"
        ;;
    esac
  done
  exit 0
fi
exit 2
""",
        )

    def write_os_release(self, content: str) -> None:
        self.os_release.write_text(content, encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.call_log.exists():
            return []
        rows: list[list[str]] = []
        for line in self.call_log.read_text(encoding="utf-8").splitlines():
            if line:
                rows.append(line.split("\t"))
        return rows

    def clear_calls(self) -> None:
        if self.call_log.exists():
            self.call_log.unlink()

    def env(
        self,
        *,
        include_shadow: bool = False,
        include_pipxbin: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        path_parts = [str(self.brewbin)]
        if include_shadow:
            path_parts.append(str(self.shadow))
        if include_pipxbin:
            path_parts.append(str(self.pipxbin))
        path_parts.extend([str(self.sysbin), str(self.tools)])
        env = {
            "HOME": str(self.home),
            "PATH": ":".join(path_parts),
            "MM_TEST_CALL_LOG": str(self.call_log),
            "MM_TEST_PIPX_STATE": str(self.pipx_state),
            "MM_TEST_PIPX_BIN": str(self.pipxbin),
            "MM_TEST_PIPX_VENVS": str(self.venvs),
            "MM_BOOTSTRAP_OS_RELEASE": str(self.os_release),
        }
        if extra_env:
            env.update(extra_env)
        return env

    def run(
        self,
        *,
        include_shadow: bool = False,
        include_pipxbin: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = self.env(
            include_shadow=include_shadow,
            include_pipxbin=include_pipxbin,
            extra_env=extra_env,
        )
        # Criterion 9: run by path, not through `sh`.
        proc = subprocess.run(
            [str(BOOTSTRAP)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            self._assert_success_invariants(proc.stdout)
        else:
            self._assert_failure_invariants(proc.stdout)
        return proc

    def _assert_success_invariants(self, output: str) -> None:
        st_calls = [row[1:] for row in self.calls() if row[0] == "syncthing"]
        assert st_calls, "successful run must check syncthing --version"
        assert all(args == ["--version"] for args in st_calls), st_calls
        for rel in SYNCTHING_CONFIG_PATHS:
            assert not (self.home / rel).exists(), f"unexpected Syncthing path: {rel}"
        lines = [line for line in output.splitlines() if line.strip()]
        assert lines, "expected output on stdout"
        assert all(line.startswith("bootstrap: ") for line in lines), lines

    def _assert_failure_invariants(self, output: str) -> None:
        lines = [line for line in output.splitlines() if line.strip()]
        assert lines, "expected failure output"
        assert "Run: " in lines[-1], lines[-1]


@pytest.fixture
def harness(tmp_path: Path) -> BootstrapHarness:
    home = tmp_path / "home"
    brewbin = tmp_path / "brewbin"
    shadow = tmp_path / "shadow"
    pipxbin = home / ".local" / "bin"
    sysbin = tmp_path / "sysbin"
    tools = tmp_path / "tools"
    venvs = home / ".local" / "pipx" / "venvs"
    for directory in (home, brewbin, shadow, pipxbin, sysbin, tools, venvs):
        directory.mkdir(parents=True, exist_ok=True)

    for tool_name in ("sed", "id", "chmod", "ln", "mkdir", "cat"):
        resolved = shutil.which(tool_name)
        assert resolved, f"required tool {tool_name} not found"
        (tools / tool_name).symlink_to(resolved)

    h = BootstrapHarness(
        tmp_path=tmp_path,
        home=home,
        brewbin=brewbin,
        shadow=shadow,
        pipxbin=pipxbin,
        sysbin=sysbin,
        tools=tools,
        venvs=venvs,
        call_log=tmp_path / "calls.log",
        pipx_state=tmp_path / "pipx-state.txt",
        os_release=tmp_path / "os-release",
    )
    h.install_sudo()
    h.install_systemctl()
    return h


def _last_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines
    return lines[-1]


class TestBootstrapContract:
    def test_script_is_executable_posix_sh(self) -> None:
        assert BOOTSTRAP.exists()
        mode = BOOTSTRAP.stat().st_mode
        assert mode & stat.S_IXUSR
        first_line = BOOTSTRAP.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == "#!/bin/sh"

    def test_install_spec_matches_upgrade_module(self) -> None:
        script_text = BOOTSTRAP.read_text(encoding="utf-8")
        match = re.search(r'^MM_INSTALL_SPEC="([^"]+)"$', script_text, re.M)
        assert match is not None, "MM_INSTALL_SPEC literal missing in bin/bootstrap"
        assert match.group(1) == upgrade.INSTALL_SPEC

    def test_readme_install_section_points_to_bootstrap_on_latest(self) -> None:
        text = README.read_text(encoding="utf-8")
        match = re.search(r"^## Install\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
        assert match is not None, "## Install section missing from README.md"
        body = match.group("body")
        assert README_BOOTSTRAP_CMD in body
        url_match = re.search(
            r"https://raw\.githubusercontent\.com/kbitz/mind-meld/(?P<ref>[^/]+)/(?P<path>[^\s\")]+)",
            body,
        )
        assert url_match is not None
        assert url_match.group("ref") == "latest"
        rel_path = url_match.group("path")
        assert rel_path == "bin/bootstrap"
        assert (ROOT / rel_path).is_file()
        assert "manual" in body.lower()
        assert "Syncthing" in body


class TestBootstrapMacOS:
    def test_macos_without_homebrew_stops_with_install_instructions(
        self, harness: BootstrapHarness
    ) -> None:
        harness.install_uname("Darwin")
        proc = harness.run()
        assert proc.returncode == 1
        last = _last_line(proc.stdout)
        assert "Homebrew is not installed" in last
        assert f"Run: {HOMEBREW_INSTALL_CMD}" in last
        assert harness.calls() == [["uname", "-s"]]

    def test_fresh_macos_installs_missing_and_mm(self, harness: BootstrapHarness) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        # Xcode-like Python 3.9.6 in sysbin counts as missing on macOS.
        harness.install_python(harness.sysbin, "3.9.6")

        proc = harness.run()
        assert proc.returncode == 0, proc.stdout

        calls = harness.calls()
        brew_calls = [row for row in calls if row[0] == "brew"]
        assert brew_calls == [["brew", "install", "python", "pipx", "syncthing"]]
        pipx_install_calls = [row[1:] for row in calls if row[0] == "pipx" and row[1] == "install"]
        assert pipx_install_calls == [["install", upgrade.INSTALL_SPEC]]

        expected_ready = (
            "bootstrap: installed python pipx syncthing mm; "
            "Python 3.13, Syncthing 2.1.7 and mm are ready."
        )
        assert expected_ready in proc.stdout
        assert "mm init" in _last_line(proc.stdout)

    def test_macos_rerun_changes_nothing(self, harness: BootstrapHarness) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.sysbin, "3.9.6")

        first = harness.run()
        assert first.returncode == 0, first.stdout

        harness.clear_calls()
        second = harness.run()
        assert second.returncode == 0, second.stdout

        calls = harness.calls()
        assert not [row for row in calls if row[0] in {"brew", "pacman"}]
        assert not [row for row in calls if row[0] == "pipx" and row[1] == "install"]
        expected_noop = (
            "bootstrap: nothing to change: Python 3.13, pipx, "
            "Syncthing 2.1.7 and mm are already installed."
        )
        assert expected_noop in second.stdout
        assert "mm init" in _last_line(second.stdout)

    def test_macos_too_old_syncthing_stops_with_brew_upgrade(
        self, harness: BootstrapHarness
    ) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.0.9")

        proc = harness.run()
        assert proc.returncode == 1
        assert "Run: brew upgrade syncthing" in _last_line(proc.stdout)
        assert not [row for row in harness.calls() if row[0] == "pipx"]

    def test_macos_python_still_old_after_brew_install_advises_shellenv(
        self, harness: BootstrapHarness
    ) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.sysbin, "3.9.6")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.1.7")

        proc = harness.run(extra_env={"MM_TEST_SKIP_BREW_PYTHON_LINK": "1"})
        assert proc.returncode == 1
        assert 'Run: eval "$(brew shellenv)"' in _last_line(proc.stdout)
        assert not [row for row in harness.calls() if row[0] == "pipx"]


class TestBootstrapArch:
    @pytest.mark.parametrize(
        "os_release_text",
        [
            'NAME="Arch Linux"\nID=arch\n',
            'NAME="EndeavourOS"\nID=endeavouros\nID_LIKE=arch\n',
            'NAME="Omarchy"\nID=omarchy\nID_LIKE="arch"\n',
        ],
    )
    def test_arch_installs_missing_with_pacman_needed(
        self, harness: BootstrapHarness, os_release_text: str
    ) -> None:
        harness.install_uname("Linux")
        harness.write_os_release(os_release_text)
        harness.install_pacman()

        proc = harness.run()
        assert proc.returncode == 0, proc.stdout

        calls = harness.calls()
        pacman_calls = [row for row in calls if row[0] == "pacman"]
        assert pacman_calls == [
            ["pacman", "-S", "--needed", "python", "python-pipx", "syncthing", "git"]
        ]
        pipx_install_calls = [row[1:] for row in calls if row[0] == "pipx" and row[1] == "install"]
        assert pipx_install_calls == [["install", upgrade.INSTALL_SPEC]]
        assert "mm init" in _last_line(proc.stdout)

    def test_arch_installs_only_missing_packages(self, harness: BootstrapHarness) -> None:
        harness.install_uname("Linux")
        harness.write_os_release("ID=arch\n")
        harness.install_pacman()
        harness.install_python(harness.sysbin, "3.13.2")
        harness.install_git(harness.sysbin)

        proc = harness.run()
        assert proc.returncode == 0, proc.stdout

        pacman_calls = [row for row in harness.calls() if row[0] == "pacman"]
        assert pacman_calls == [["pacman", "-S", "--needed", "python-pipx", "syncthing"]]

    def test_arch_too_old_python_stops_with_pacman_syu(self, harness: BootstrapHarness) -> None:
        harness.install_uname("Linux")
        harness.write_os_release("ID=arch\n")
        harness.install_pacman()
        harness.install_python(harness.sysbin, "3.10.14")
        harness.install_pipx(harness.sysbin)
        harness.install_syncthing(harness.sysbin, "2.1.7")
        harness.install_git(harness.sysbin)

        proc = harness.run()
        assert proc.returncode == 1
        assert "pacman -Syu" in _last_line(proc.stdout)
        assert not [row for row in harness.calls() if row[0] == "pipx"]


class TestBootstrapUnsupportedAndErrors:
    @pytest.mark.parametrize(
        "uname_val,os_release_text,expected_label",
        [
            ("Linux", "ID=ubuntu\nID_LIKE=debian\n", "ubuntu"),
            ("FreeBSD", "", "FreeBSD"),
        ],
    )
    def test_unsupported_system_stops_and_names_supported(
        self,
        harness: BootstrapHarness,
        uname_val: str,
        os_release_text: str,
        expected_label: str,
    ) -> None:
        harness.install_uname(uname_val)
        harness.write_os_release(os_release_text)
        harness.install_brew()
        harness.install_pacman()

        proc = harness.run()
        assert proc.returncode == 1
        last = _last_line(proc.stdout)
        assert expected_label in last
        assert "macOS and Arch-based Linux" in last
        assert f"Run: pipx install {upgrade.INSTALL_SPEC}" in last
        assert not [row for row in harness.calls() if row[0] in {"brew", "pacman", "pipx"}]

    def test_shadowed_mm_stops_with_ensurepath_prepend_force(
        self, harness: BootstrapHarness
    ) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.1.7")
        harness.install_mm(harness.shadow)

        proc = harness.run(include_shadow=True)
        assert proc.returncode == 1
        last = _last_line(proc.stdout)
        assert str(harness.shadow / "mm") in last
        assert str(harness.pipxbin / "mm") in last
        assert "Run: pipx ensurepath --prepend --force" in last

    def test_mm_not_on_path_stops_with_ensurepath(self, harness: BootstrapHarness) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.1.7")

        proc = harness.run(include_pipxbin=False)
        assert proc.returncode == 1
        last = _last_line(proc.stdout)
        assert str(harness.pipxbin) in last
        assert last.endswith("Run: pipx ensurepath")

    def test_package_manager_failure_repeats_failed_command(
        self, harness: BootstrapHarness
    ) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)

        proc = harness.run(extra_env={"MM_TEST_PKG_FAIL": "1"})
        assert proc.returncode == 1
        last = _last_line(proc.stdout)
        assert last.endswith("brew install syncthing failed. Run: brew install syncthing")

    def test_pipx_install_failure_stops_with_pipx_install_command(
        self, harness: BootstrapHarness
    ) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.1.7")

        proc = harness.run(extra_env={"MM_TEST_PIPX_FAIL": "1"})
        assert proc.returncode == 1
        last = _last_line(proc.stdout)
        assert last.endswith(f"pipx could not install mm. Run: pipx install {upgrade.INSTALL_SPEC}")

    def test_foreign_mm_in_pipx_bin_dir_stops_with_force_reinstall(
        self, harness: BootstrapHarness
    ) -> None:
        # Another installer (uv, pip --user) already owns ~/.local/bin/mm;
        # pipx leaves it in place and exits 0, so the path alone proves nothing.
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.1.7")
        harness.install_mm(harness.pipxbin)

        proc = harness.run()
        assert proc.returncode == 1
        last = _last_line(proc.stdout)
        assert str(harness.pipxbin / "mm") in last
        assert last.endswith(f"Run: pipx install --force {upgrade.INSTALL_SPEC}")

    def test_pipx_list_problem_still_finds_installed_mm(self, harness: BootstrapHarness) -> None:
        # `pipx list` exits 1 when any other venv is broken, but still lists
        # mind-meld; a rerun must not reinstall or claim it installed mm.
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.1.7")

        first = harness.run()
        assert first.returncode == 0, first.stdout

        harness.clear_calls()
        second = harness.run(extra_env={"MM_TEST_PIPX_LIST_EXIT": "1"})
        assert second.returncode == 0, second.stdout
        assert not [row for row in harness.calls() if row[0] == "pipx" and row[1] == "install"]
        assert "bootstrap: nothing to change:" in second.stdout

    def test_missing_pipx_link_stops_with_force_reinstall(self, harness: BootstrapHarness) -> None:
        # pipx lists mind-meld but its ~/.local/bin/mm link is gone (for
        # example the user deleted a foreign copy by hand): only a forced
        # reinstall relinks it, not `pipx ensurepath`.
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.1.7")

        first = harness.run()
        assert first.returncode == 0, first.stdout
        (harness.pipxbin / "mm").unlink()

        second = harness.run()
        assert second.returncode == 1
        last = _last_line(second.stdout)
        assert str(harness.pipxbin / "mm") in last
        assert last.endswith(f"Run: pipx install --force {upgrade.INSTALL_SPEC}")

    @pytest.mark.parametrize(
        "uname_val,expected_fix",
        [("Darwin", "brew reinstall pipx"), ("Linux", "sudo pacman -S python-pipx")],
    )
    def test_unreadable_pipx_environment_stops_with_pipx_reinstall(
        self, harness: BootstrapHarness, uname_val: str, expected_fix: str
    ) -> None:
        harness.install_uname(uname_val)
        harness.write_os_release("ID=arch\n")
        harness.install_brew()
        harness.install_pacman()
        for directory in (harness.brewbin, harness.sysbin):
            harness.install_python(directory, "3.13.2")
            harness.install_pipx(directory)
            harness.install_syncthing(directory, "2.1.7")
        harness.install_git(harness.sysbin)

        proc = harness.run(extra_env={"MM_TEST_PIPX_ENV_FAIL": "1"})
        assert proc.returncode == 1
        last = _last_line(proc.stdout)
        if uname_val == "Linux" and os.geteuid() == 0:
            expected_fix = expected_fix.removeprefix("sudo ")  # root needs no sudo
        assert last.endswith(f"Run: {expected_fix}")


class TestBootstrapDownload:
    def _run_readme_command(self, harness: BootstrapHarness) -> subprocess.CompletedProcess[str]:
        # The README command calls `sh` by name.
        real_sh = shutil.which("sh")
        assert real_sh, "required tool sh not found"
        (harness.tools / "sh").symlink_to(real_sh)
        return subprocess.run(
            ["/bin/sh", "-c", README_BOOTSTRAP_CMD],
            env=harness.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    def test_readme_command_fails_when_download_fails(self, harness: BootstrapHarness) -> None:
        # A failed download must stop with curl's failure status, not run an
        # empty script and report success.
        harness.install_uname("Darwin")
        harness.install_curl(serve=None)

        proc = self._run_readme_command(harness)
        assert proc.returncode != 0, proc.stdout
        assert [row[0] for row in harness.calls()] == ["curl"]

    def test_readme_command_runs_downloaded_script(self, harness: BootstrapHarness) -> None:
        harness.install_uname("Darwin")
        harness.install_curl(serve=BOOTSTRAP)

        proc = self._run_readme_command(harness)
        assert proc.returncode == 1
        assert "Homebrew is not installed" in _last_line(proc.stdout)
        assert [row[0] for row in harness.calls()] == ["curl", "uname"]

    @pytest.mark.parametrize("cut", ["quarter", "half", "three-quarters", "all-but-last-line"])
    def test_truncated_download_runs_nothing(self, harness: BootstrapHarness, cut: str) -> None:
        # Should a cut-off copy ever reach `sh -c`, it must define at most some
        # functions and run no command.
        harness.install_uname("Darwin")
        harness.install_brew()
        text = BOOTSTRAP.read_text(encoding="utf-8")
        fractions = {"quarter": 0.25, "half": 0.5, "three-quarters": 0.75}
        if cut == "all-but-last-line":
            partial = text.rstrip("\n").rsplit("\n", 1)[0] + "\n"
        else:
            partial = text[: int(len(text) * fractions[cut])]

        proc = subprocess.run(
            ["/bin/sh", "-c", partial],
            env=harness.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert harness.calls() == [], proc.stdout
        assert "bootstrap:" not in proc.stdout


class TestBootstrapSyncthingStart118A:
    def _ready_macos(self, harness: BootstrapHarness) -> None:
        harness.install_uname("Darwin")
        harness.install_brew()
        harness.install_python(harness.brewbin, "3.13.2")
        harness.install_pipx(harness.brewbin)
        harness.install_syncthing(harness.brewbin, "2.1.7")
        harness.install_pgrep(found=False)

    def _ready_arch(self, harness: BootstrapHarness) -> None:
        harness.install_uname("Linux")
        harness.write_os_release("ID=arch\n")
        harness.install_pacman()
        harness.install_python(harness.sysbin, "3.13.2")
        harness.install_pipx(harness.sysbin)
        harness.install_syncthing(harness.sysbin, "2.1.7")
        harness.install_git(harness.sysbin)
        harness.install_pgrep(found=False)

    @pytest.mark.parametrize("check_val", ["default not-found", "no-config not-found"])
    def test_macos_default_or_no_config_not_running_starts_syncthing(
        self, harness: BootstrapHarness, check_val: str
    ) -> None:
        self._ready_macos(harness)
        proc = harness.run(extra_env={"MM_TEST_SYNCTHING_CHECK": check_val})
        assert proc.returncode == 0, proc.stdout
        brew_starts = [
            row for row in harness.calls() if row == ["brew", "services", "start", "syncthing"]
        ]
        assert len(brew_starts) == 1
        assert (
            "started Syncthing and set it to start at login (brew services start syncthing)."
            in proc.stdout
        )
        assert (
            "pairs no machine, shares no folder and writes no Syncthing configuration"
            not in proc.stdout
        )
        last = _last_line(proc.stdout)
        assert "mm init --new-fleet" in last and "mm init" in last

    @pytest.mark.parametrize("check_val", ["default unreachable", "no-config not-found"])
    def test_arch_default_or_no_config_not_running_starts_syncthing(
        self, harness: BootstrapHarness, check_val: str
    ) -> None:
        self._ready_arch(harness)
        proc = harness.run(extra_env={"MM_TEST_SYNCTHING_CHECK": check_val})
        assert proc.returncode == 0, proc.stdout
        systemctl_calls = [row for row in harness.calls() if row[0] == "systemctl"]
        assert systemctl_calls == [["systemctl", "--user", "enable", "--now", "syncthing.service"]]
        assert (
            "started Syncthing and set it to start at login "
            "(systemctl --user enable --now syncthing.service)." in proc.stdout
        )
        last = _last_line(proc.stdout)
        assert "mm init --new-fleet" in last and "mm init" in last

    @pytest.mark.parametrize("check_val", ["pre-existing ok", "pre-existing unreachable"])
    def test_pre_existing_does_not_start_and_names_guide(
        self, harness: BootstrapHarness, check_val: str
    ) -> None:
        self._ready_macos(harness)
        proc = harness.run(extra_env={"MM_TEST_SYNCTHING_CHECK": check_val})
        assert proc.returncode == 0, proc.stdout
        assert not [
            row for row in harness.calls() if row == ["brew", "services", "start", "syncthing"]
        ]
        assert (
            "Syncthing already has its own configuration, so this script leaves it alone; "
            'see README "Configuring Syncthing yourself".' in proc.stdout
        )
        last = _last_line(proc.stdout)
        assert "mm init --new-fleet" in last and "mm init" in last

    @pytest.mark.parametrize(
        ("check_val", "pgrep_found"),
        [
            ("default ok", False),
            ("default unauthorized", False),
            ("default error", False),
            ("default not-found", True),
        ],
    )
    def test_already_running_via_api_or_pgrep_never_starts_a_second_instance(
        self, harness: BootstrapHarness, check_val: str, pgrep_found: bool
    ) -> None:
        self._ready_macos(harness)
        harness.install_pgrep(found=pgrep_found)
        proc = harness.run(extra_env={"MM_TEST_SYNCTHING_CHECK": check_val})
        assert proc.returncode == 0, proc.stdout
        assert not [
            row for row in harness.calls() if row == ["brew", "services", "start", "syncthing"]
        ]
        assert "Syncthing is already running; not starting another." in proc.stdout
        last = _last_line(proc.stdout)
        assert "mm init --new-fleet" in last and "mm init" in last

    @pytest.mark.parametrize(
        ("platform_name", "expected_cmd"),
        [
            ("Darwin", "brew services start syncthing"),
            ("Linux", "systemctl --user enable --now syncthing.service"),
        ],
    )
    def test_failed_start_stops_with_start_command(
        self, harness: BootstrapHarness, platform_name: str, expected_cmd: str
    ) -> None:
        if platform_name == "Darwin":
            self._ready_macos(harness)
        else:
            self._ready_arch(harness)
        proc = harness.run(
            extra_env={
                "MM_TEST_SYNCTHING_CHECK": "default not-found",
                "MM_TEST_START_FAIL": "1",
            }
        )
        assert proc.returncode == 1, proc.stdout
        last = _last_line(proc.stdout)
        assert last.endswith(f"{expected_cmd} failed. Run: {expected_cmd}")

    @pytest.mark.parametrize(
        ("check_val", "check_exit"),
        [
            ("unknown error", "0"),
            ("", "1"),
        ],
    )
    def test_check_failure_does_not_start_and_still_exits_zero(
        self, harness: BootstrapHarness, check_val: str, check_exit: str
    ) -> None:
        self._ready_macos(harness)
        proc = harness.run(
            extra_env={
                "MM_TEST_SYNCTHING_CHECK": check_val,
                "MM_TEST_SYNCTHING_CHECK_EXIT": check_exit,
            }
        )
        assert proc.returncode == 0, proc.stdout
        assert not [
            row for row in harness.calls() if row == ["brew", "services", "start", "syncthing"]
        ]
        assert (
            "could not check Syncthing's configuration, so it was not started; "
            "mm diag shows why." in proc.stdout
        )
        last = _last_line(proc.stdout)
        assert "mm init --new-fleet" in last and "mm init" in last
