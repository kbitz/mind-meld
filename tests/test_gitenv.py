"""Git environment policy and the structural gate for every Git subprocess."""

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mind_meld import gitenv

SRC = Path(__file__).resolve().parents[1] / "src"


def test_scrub_drops_repo_overrides_and_preserves_user_settings():
    survivors = {
        "PATH": "/custom/bin",
        "HOME": "/home/test",
        "GIT_EXEC_PATH": "/custom/git",
        "GIT_CONFIG_GLOBAL": "/custom/gitconfig",
        "GIT_CONFIG_SYSTEM": "/custom/system",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_SSH_COMMAND": "ssh -i /custom/key",
        "XDG_CONFIG_HOME": "/custom/config",
    }
    base = dict.fromkeys(gitenv.GIT_REPO_LOCAL_ENV_VARS, "redirect")
    base.update(survivors)
    base.update(GIT_CONFIG_KEY_0="user.email", GIT_CONFIG_VALUE_0="decoy@example.com")
    base.update(GIT_CONFIG_KEY_42="orphan", GIT_CONFIG_VALUE_42="orphan")
    base.update(LC_ALL="fr_FR.UTF-8", LANGUAGE="fr")
    before = base.copy()
    result = gitenv.scrubbed_git_env(base)
    assert result == {**survivors, "LC_ALL": "C", "LANGUAGE": "C"}
    assert result is not base
    assert base == before


def test_default_reads_current_environment_without_mutating_it(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/decoy")
    monkeypatch.setenv("MM_ENV_PROBE", "first")
    before = dict(os.environ)
    first = gitenv.scrubbed_git_env()
    assert "GIT_DIR" not in first
    assert first["MM_ENV_PROBE"] == "first"
    assert dict(os.environ) == before
    monkeypatch.setenv("MM_ENV_PROBE", "second")
    assert gitenv.scrubbed_git_env()["MM_ENV_PROBE"] == "second"
    assert gitenv.scrubbed_git_env({}) == {"LC_ALL": "C", "LANGUAGE": "C"}


def test_repo_local_env_vars_cover_installed_git():
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    result = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        capture_output=True,
        encoding="utf-8",
        env=gitenv.scrubbed_git_env(),
        check=True,
    )
    missing = set(result.stdout.splitlines()) - gitenv.GIT_REPO_LOCAL_ENV_VARS
    assert not missing, f"Add Git's new repository-local variables to gitenv: {sorted(missing)}"


def test_gitenv_imports_nothing_from_mind_meld():
    tree = ast.parse(Path(gitenv.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("mind_meld") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert not (node.module or "").startswith("mind_meld")


def _git_spawn_violations(source: str, filename: str) -> list[str]:
    """Inspect literal Git argv; require literal argv in its current owners."""
    violations = []
    canonical_env = ast.dump(ast.parse("gitenv.scrubbed_git_env()", mode="eval").body)
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr in {"run", "Popen"}
        ):
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            if Path(filename).name in {"events.py", "identity.py"}:
                violations.append(f"{filename}:{node.lineno}: use list-literal argv")
            continue
        argv = node.args[0].elts
        if not argv or not isinstance(argv[0], ast.Constant) or argv[0].value != "git":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        env = kwargs.get("env")
        if env is None or ast.dump(env) != canonical_env:
            violations.append(f"{filename}:{node.lineno}: pass env=gitenv.scrubbed_git_env()")
        encoding = kwargs.get("encoding")
        if not isinstance(encoding, ast.Constant) or encoding.value != "utf-8":
            violations.append(f'{filename}:{node.lineno}: pass encoding="utf-8"')
    return violations


@pytest.mark.parametrize("call", ["run", "Popen"])
@pytest.mark.parametrize("env", ["", ", env=None", ", env=os.environ"])
def test_git_spawn_gate_rejects_unscrubbed_environment(call, env):
    source = f'subprocess.{call}(["git", "log"], encoding="utf-8"{env})'
    assert _git_spawn_violations(source, "probe.py") == [
        "probe.py:1: pass env=gitenv.scrubbed_git_env()"
    ]


def test_git_spawn_gate_accepts_canonical_call():
    assert not _git_spawn_violations(
        'subprocess.run(["git"], env=gitenv.scrubbed_git_env(), encoding="utf-8")',
        "probe.py",
    )


def test_git_spawn_gate_requires_encoding_and_literal_argv():
    assert _git_spawn_violations(
        'subprocess.run(["git"], env=gitenv.scrubbed_git_env())', "probe.py"
    ) == ['probe.py:1: pass encoding="utf-8"']
    for owner in ("events.py", "identity.py"):
        assert _git_spawn_violations("subprocess.run(argv)", owner) == [
            f"{owner}:1: use list-literal argv"
        ]


def test_all_git_subprocesses_use_scrubbed_environment():
    violations = []
    for path in sorted(SRC.rglob("*.py")):
        violations.extend(_git_spawn_violations(path.read_text(), str(path.relative_to(SRC))))
    assert not violations, "\n".join(violations)
