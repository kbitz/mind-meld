#!/usr/bin/env python3
"""stdlib-only driver for ./bin/check. See --help.

Cards describe verification SCOPE; they must not know where Python lives.
This file is not packaged (pyproject packages = src/mind_meld only).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

POLICY_VERSION = 1
MIN_VERSION = (3, 11)
PREFERRED_VERSION = (3, 13)
CANDIDATES = ("python3.13", "python3.12", "python3.11", "python3")
OWNER_NAME = ".mm-check-owner"
OWNER_BODY = "mind-meld-bin-check\n"
STAMP_NAME = ".mm-check-stamp"
LOCK_NAME = ".mm-check.lock"


class Args:
    __slots__ = (
        "help",
        "no_bootstrap",
        "lint",
        "tests_only",
        "serial",
        "rebuild",
        "scope",
        "pytest_extra",
    )

    def __init__(self) -> None:
        self.help = False
        self.no_bootstrap = False
        self.lint = False
        self.tests_only = False
        self.serial = False
        self.rebuild = False
        self.scope: list[str] = []
        self.pytest_extra: list[str] = []


def repo_root() -> Path:
    env = os.environ.get("MM_CHECK_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def parse_argv(argv: list[str]) -> Args:
    """`--` is the authoritative boundary. Node IDs are valid scope.

    Do not validate every arg as a path: `tests/x.py::TestC::test_n`
    fails `test -e`, so per-arg existence checks reject real pytest
    selectors. Unknown dash-args before `--` pass through to pytest.
    """
    args = Args()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            args.pytest_extra.extend(argv[i + 1 :])
            break
        if a in ("-h", "--help"):
            args.help = True
        elif a == "--no-bootstrap":
            args.no_bootstrap = True
        elif a == "--lint":
            args.lint = True
        elif a == "--tests":
            args.tests_only = True
        elif a == "--serial":
            args.serial = True
        elif a == "--rebuild":
            args.rebuild = True
        elif a.startswith("-"):
            args.pytest_extra.extend(argv[i:])
            break
        else:
            args.scope.append(a)
        i += 1
    if not args.scope:
        args.scope = ["tests/"]
    return args


def is_cloud_workspace() -> bool:
    return os.environ.get("CONDUCTOR_IS_LOCAL") == "0"


def _fix_no_python(found: list[str]) -> str:
    lines = [
        "install Python 3.13 (or 3.12 / 3.11) and put it on PATH.",
        "do not run xcode-select --install: /usr/bin/python3 is 3.9.6,",
        "below requires-python >=3.11, and that advice causes a second failure.",
    ]
    if is_cloud_workspace():
        lines = [
            "install Python 3.13 (or 3.12 / 3.11) and put it on PATH.",
            "this is a Linux cloud workspace; brew / xcode-select will not help.",
        ]
    if found:
        lines.insert(0, "found: " + "; ".join(found))
    return "\n".join("  " + line for line in lines)


def manual_recipe(python: str | None = None) -> str:
    py = python or "python3.13"
    return "\n".join(
        [
            f"  {py} -m venv .venv",
            "  ./.venv/bin/python -m pip install -e '.[dev]'",
            "  ./.venv/bin/ruff check .",
            "  ./.venv/bin/ruff format --check .",
            "  ./.venv/bin/python -m pytest tests/",
        ]
    )


def die(problem: str, cause: str, fix: str, code: int = 1) -> None:
    sys.stderr.write(f"bin/check: {problem}\n")
    sys.stderr.write(f"cause: {cause}\n")
    sys.stderr.write(f"fix:\n{fix}\n")
    raise SystemExit(code)


def log(msg: str) -> None:
    sys.stderr.write(f"bin/check: {msg}\n")
    sys.stderr.flush()


def python_version(python: str) -> tuple[int, int, int] | None:
    try:
        proc = subprocess.run(
            [python, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.strip().split(".")
    if len(parts) < 2:
        return None
    try:
        major, minor = int(parts[0]), int(parts[1])
        micro = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return None
    return major, minor, micro


def _which(name: str) -> str | None:
    return shutil.which(name)


def resolve_interpreter() -> tuple[str, tuple[int, int, int]]:
    """Prefer 3.13 for CI parity. NOTICE, never hard-fail, on any other
    minor that satisfies requires-python (>=3.11, no upper bound).
    Classifiers are metadata, not a compatibility bound.
    """
    explicit = os.environ.get("MM_PYTHON")
    ordered: list[str] = []
    if explicit:
        ordered.append(explicit)
    for cand in CANDIDATES:
        found = cand if os.path.isabs(cand) and os.path.exists(cand) else _which(cand)
        if found and found not in ordered:
            ordered.append(found)

    found_notes: list[str] = []
    picked: tuple[str, tuple[int, int, int]] | None = None
    for path in ordered:
        ver = python_version(path)
        if ver is None:
            found_notes.append(f"{path} (could not read version)")
            continue
        label = "%d.%d.%d" % ver
        if ver[:2] < MIN_VERSION:
            found_notes.append(f"{path} ({label}, below >=3.11)")
            continue
        picked = (path, ver)
        break

    if picked is None:
        die(
            "no Python >= 3.11 on PATH",
            "looked for python3.13, python3.12, python3.11, python3"
            + (f"; MM_PYTHON={explicit}" if explicit else ""),
            _fix_no_python(found_notes),
        )

    path, ver = picked
    log("interpreter %s (%d.%d.%d)" % (path, ver[0], ver[1], ver[2]))
    if ver[:2] != PREFERRED_VERSION:
        log(
            "notice: CI pins 3.13; requires-python is >=3.11 with no upper "
            "bound, so this is allowed. A local-green / CI-red mystery "
            "starts here if they diverge."
        )
    return path, ver


def stamp_digest(pyproject: Path, python: str, version: tuple[int, int, int]) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"policy=%d\n" % POLICY_VERSION)
    hasher.update(("python=%s\n" % os.path.realpath(python)).encode())
    hasher.update(b"version=%d.%d.%d\n" % version)
    hasher.update(b"pyproject=")
    hasher.update(hashlib.sha256(pyproject.read_bytes()).digest())
    return hasher.hexdigest()


def venv_owned(venv: Path) -> bool:
    marker = venv / OWNER_NAME
    try:
        return marker.read_text(encoding="utf-8") == OWNER_BODY
    except OSError:
        return False


def mark_owned(venv: Path) -> None:
    (venv / OWNER_NAME).write_text(OWNER_BODY, encoding="utf-8")


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_healthy(python: Path) -> bool:
    try:
        proc = subprocess.run(
            [str(python), "-c", "import pytest, ruff, mind_meld"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


@contextmanager
def bootstrap_lock(root: Path):
    # Sibling of .venv, not inside it: python -m venv refuses a non-empty
    # directory, and a lock file inside a rmtree'd venv would be a new
    # inode for the next waiter (two "exclusive" locks).
    fd = os.open(root / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _pip_install(python: str, root: Path) -> None:
    log("pip install -e '.[dev]'")
    proc = subprocess.run(
        [python, "-m", "pip", "install", "-e", ".[dev]"],
        cwd=str(root),
        check=False,
    )
    if proc.returncode != 0:
        die(
            f"pip install failed (exit {proc.returncode})",
            "offline, PyPI 5xx, or a wheel that will not build on this interpreter",
            "manual fallback (quote '.[dev]' — zsh globs it):\n" + manual_recipe(python),
            code=proc.returncode,
        )


def _create_venv(python: str, venv: Path) -> None:
    log(f"creating venv at {venv}")
    proc = subprocess.run([python, "-m", "venv", str(venv)], check=False)
    if proc.returncode != 0:
        die(
            f"venv create failed (exit {proc.returncode})",
            "disk full, read-only workspace, or the venv module is missing",
            manual_recipe(python),
            code=proc.returncode,
        )
    mark_owned(venv)


def ensure_venv(root: Path, python: str, version: tuple[int, int, int], rebuild: bool) -> Path:
    venv = root / ".venv"
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        die(
            f"no pyproject.toml at {root}",
            "MM_CHECK_ROOT / the launcher did not resolve to the repo root",
            "invoke via ./bin/check from a clone of mind-meld",
        )
    digest = stamp_digest(pyproject, python, version)
    stamp = venv / STAMP_NAME

    with bootstrap_lock(root):
        py = venv_python(venv)
        exists = py.is_file()
        owned = venv_owned(venv)
        healthy = exists and venv_healthy(py)
        stamp_ok = False
        if stamp.is_file():
            try:
                stamp_ok = stamp.read_text(encoding="utf-8").strip() == digest
            except OSError:
                stamp_ok = False

        if exists and not owned:
            if not healthy:
                die(
                    ".venv exists but is unusable and not owned by bin/check",
                    f"no ownership marker at {venv / OWNER_NAME}; "
                    "refusing to delete a venv we did not create",
                    "  mv .venv .venv.bak\n  ./bin/check",
                )
            mark_owned(venv)
            owned = True
            log("claimed existing .venv (was healthy, unowned)")

        if rebuild and owned:
            log("rebuilding .venv (--rebuild)")
            if venv.exists():
                shutil.rmtree(venv)
            _create_venv(python, venv)
            py = venv_python(venv)
            _pip_install(str(py), root)
            stamp.write_text(digest + "\n", encoding="utf-8")
            return venv

        if not exists:
            if venv.exists() and owned:
                shutil.rmtree(venv)
            elif venv.exists() and any(venv.iterdir()):
                die(
                    ".venv exists but has no python and is not owned by bin/check",
                    "refusing to delete a venv we did not create",
                    "  mv .venv .venv.bak\n  ./bin/check",
                )
            _create_venv(python, venv)
            py = venv_python(venv)
            _pip_install(str(py), root)
            stamp.write_text(digest + "\n", encoding="utf-8")
            return venv

        if owned and not healthy:
            log("notice: .venv looks broken, rebuilding")
            shutil.rmtree(venv)
            _create_venv(python, venv)
            py = venv_python(venv)
            _pip_install(str(py), root)
            stamp.write_text(digest + "\n", encoding="utf-8")
            return venv

        if owned and not stamp_ok:
            log("notice: .venv stamp stale (declared inputs changed), reinstalling")
            _pip_install(str(py), root)
            stamp.write_text(digest + "\n", encoding="utf-8")
            return venv

        return venv


def _mm_venv_python() -> Path | None:
    raw = os.environ.get("MM_VENV")
    if not raw:
        return None
    venv = Path(raw).expanduser().resolve()
    py = venv_python(venv)
    if not py.is_file() or not venv_healthy(py):
        die(
            "MM_VENV is set but is not a usable environment",
            f"{venv} is missing pytest/ruff/mind_meld (MM_VENV is validate-and-use, never mutated)",
            "unset MM_VENV to let bin/check manage .venv, or run\n"
            "  .venv/bin/python -m pip install -e '.[dev]'\n"
            "inside that environment yourself",
        )
    return py


def tool_python(args: Args, root: Path, bootstrapped: Path | None) -> Path:
    mm = _mm_venv_python()
    if mm is not None:
        return mm
    if args.no_bootstrap or os.environ.get("VIRTUAL_ENV"):
        venv_env = os.environ.get("VIRTUAL_ENV")
        if venv_env:
            return venv_python(Path(venv_env))
        explicit = os.environ.get("MM_PYTHON")
        if explicit:
            return Path(explicit)
        local = venv_python(root / ".venv")
        if local.is_file():
            return local
        return Path(sys.executable)
    assert bootstrapped is not None
    return venv_python(bootstrapped)


def xdist_args(python: Path, serial: bool, extra: list[str]) -> list[str]:
    if serial:
        return []
    if any(
        a in ("-x", "--pdb", "-n") or a.startswith("--pdb") or a.startswith("-n") for a in extra
    ):
        return []
    try:
        proc = subprocess.run(
            [str(python), "-c", "import xdist"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    workers = os.environ.get("MM_PYTEST_WORKERS", "auto")
    return ["-n", workers]


def run_tool(argv: list[str], name: str, fix: str) -> None:
    log(name)
    proc = subprocess.run(argv, check=False)
    if proc.returncode != 0:
        die(
            f"{name} failed (exit {proc.returncode})",
            f"see {name}'s output above",
            fix,
            code=proc.returncode,
        )


def print_help() -> None:
    text = """\
usage: ./bin/check [paths...] [-- pytest-args]

Self-bootstrapping verification for mind-meld. Cards describe scope;
they must not know where Python lives.

Default: ruff check .  ->  ruff format --check .  ->  pytest tests/
Cheap gates first. Fail-fast; each tool's exit code is preserved.

A scoped pytest still lints the whole repo. That is intended — ruff is
~0.07s — but --tests would otherwise mislead. Use --tests to skip lint,
--lint to skip pytest.

-- is the authoritative argv boundary. Node IDs are valid scope
(tests/foo.py::TestC::test_n). Do not quote-validate every arg as a path.

flags:
  --help           this message
  --no-bootstrap   use the current environment; never create or mutate .venv
  --lint           ruff check + ruff format --check only
  --tests          pytest only (no ruff)
  --serial         do not pass -n auto (for -x / --pdb)
  --rebuild        recreate an owned .venv, then install

env:
  MM_PYTHON        interpreter for the venv (default: python3.13, then 3.12,
                   3.11, python3). Printed unconditionally.
  MM_VENV          validate-and-use or fail; never mutated
  MM_PYTEST_WORKERS  xdist worker count (default: auto)
  VIRTUAL_ENV      treated like --no-bootstrap when set
  CONDUCTOR_IS_LOCAL  0 = cloud workspace; remedy lines drop brew advice

manual fallback (separate lines; quote '.[dev]' — macOS zsh globs it):
"""
    sys.stdout.write(text)
    sys.stdout.write(manual_recipe() + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_argv(list(sys.argv[1:] if argv is None else argv))
    if args.help:
        print_help()
        return 0
    if args.lint and args.tests_only:
        die(
            "--lint and --tests together do nothing",
            "each flag skips the other, so both would skip every check and exit 0",
            "  ./bin/check           # lint then pytest\n"
            "  ./bin/check --lint    # ruff only\n"
            "  ./bin/check --tests   # pytest only",
        )

    root = repo_root()
    try:
        os.chdir(root)
    except OSError as exc:
        die(
            f"cannot chdir to repo root {root}",
            str(exc),
            "invoke ./bin/check via its real path in a mind-meld clone",
        )

    python, version = resolve_interpreter()

    mm_venv = os.environ.get("MM_VENV")
    bootstrapped: Path | None = None
    if mm_venv:
        pass
    elif args.no_bootstrap or os.environ.get("VIRTUAL_ENV"):
        log("bootstrap skipped (--no-bootstrap or VIRTUAL_ENV)")
    else:
        bootstrapped = ensure_venv(root, python, version, rebuild=args.rebuild)

    tool = tool_python(args, root, bootstrapped)
    if not tool.is_file() and not os.path.isabs(str(tool)):
        which = shutil.which(str(tool))
        if which:
            tool = Path(which)
    if not Path(tool).exists() and shutil.which(str(tool)) is None:
        die(
            f"tool interpreter not found: {tool}",
            "bootstrap did not produce a venv python, and --no-bootstrap had nothing to use",
            manual_recipe(python),
        )

    run_lint = not args.tests_only
    run_tests = not args.lint
    ruff = [str(tool), "-m", "ruff"]

    if run_lint:
        run_tool(
            [*ruff, "check", "."],
            "ruff check .",
            f"  {tool} -m ruff check --fix .",
        )
        run_tool(
            [*ruff, "format", "--check", "."],
            "ruff format --check .",
            f"  {tool} -m ruff format .",
        )

    if run_tests:
        extra = args.pytest_extra
        n_args = xdist_args(Path(tool), args.serial, extra)
        run_tool(
            [str(tool), "-m", "pytest", *args.scope, *n_args, *extra],
            "pytest " + " ".join(args.scope),
            "see pytest's output above",
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("bin/check: interrupted\n")
        raise SystemExit(130) from None
