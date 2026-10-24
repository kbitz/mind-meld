#!/usr/bin/env python3
"""stdlib-only driver for ./bin/check. See --help.

Cards describe verification SCOPE; they must not know where Python lives.
This file is not packaged (pyproject packages = src/mind_meld only).
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

POLICY_VERSION = 1
MIN_VERSION = (3, 11)
# CI runs 3.13 on macOS and 3.14 on Linux (Track 73A); either is preferred.
# 3.13 is tried first so a Mac with both keeps matching the macOS CI job
# (and its existing .venv); a 3.14-only Linux host still picks 3.14.
CI_VERSIONS = ((3, 13), (3, 14))
CANDIDATES = ("python3.13", "python3.14", "python3.12", "python3.11", "python3")
OWNER_NAME = ".mm-check-owner"
OWNER_BODY = "mind-meld-bin-check\n"
STAMP_NAME = ".mm-check-stamp"
LOCK_NAME = ".mm-check.lock"
PIP_TIMEOUT_S = 300

# --affected (Track 82B). State is per worktree and gitignored.
SELECT_DIR = ".mm-test-selection"
SELECT_OWNER = ".owner"
SELECT_RECORD = "record.json"
SELECT_FAILED = "failed.json"
SELECT_LOCK = ".lock"
SELECT_SCHEMA = 1
SELECT_DEFAULT_BASE = "origin/main"
# A change to any of these can alter what every test does: never narrow.
SELECT_CONFIG_PATHS = ("pyproject.toml", "bin/check", "bin/_check.py")

# Owned pytest process group (Track 82B). Catchable signals only.
OWNED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
GROUP_GRACE_S = 1.0  # leader gone, workers still exiting
GROUP_TERM_S = 5.0
GROUP_KILL_S = 3.0
SIGNALLED_LEADER_S = 10.0  # leader's time to exit after a forwarded signal
CLEANUP_FAILED_CODE = 70


class Args:
    __slots__ = (
        "help",
        "no_bootstrap",
        "lint",
        "tests_only",
        "serial",
        "rebuild",
        "affected",
        "refresh",
        "base",
        "scope_given",
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
        self.affected = False
        self.refresh = False
        self.base: str | None = None
        self.scope_given = False
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
        elif a == "--affected":
            args.affected = True
        elif a == "--refresh":
            args.refresh = True
        elif a == "--base" or a.startswith("--base="):
            if a == "--base":
                i += 1
                value = argv[i] if i < len(argv) else ""
            else:
                value = a.split("=", 1)[1]
            if not value or value.startswith("-"):
                die(
                    "--base needs a git ref",
                    "no ref followed --base",
                    "  ./bin/check --affected --base origin/main",
                )
            args.base = value
        elif a.startswith("-"):
            args.pytest_extra.extend(argv[i:])
            break
        else:
            args.scope.append(a)
            args.scope_given = True
        i += 1
    if not args.scope:
        args.scope = ["tests/"]
    return args


def _fix_no_python(found: list[str], platform: str | None = None) -> str:
    """Remedy chosen by OS; both name 3.14 (Track 73A)."""
    if platform is None:
        platform = sys.platform
    if platform == "darwin":
        lines = [
            "install Python 3.13 or 3.14 (3.12 / 3.11 also work) and put it on PATH.",
            "do not run xcode-select --install: /usr/bin/python3 is 3.9.6,",
            "below requires-python >=3.11, and that advice causes a second failure.",
        ]
    else:
        lines = [
            "install a distro Python 3.11+ (3.14 preferred) and put it on PATH.",
            "use your package manager (e.g. pacman / apt python3);",
            "brew / xcode-select advice does not apply on Linux.",
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
    """Prefer a CI interpreter (3.13, then 3.14) for parity. NOTICE,
    never hard-fail, on any other minor that satisfies requires-python
    (>=3.11, no upper bound). Classifiers are metadata, not a
    compatibility bound.
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
            "looked for " + ", ".join(CANDIDATES) + (f"; MM_PYTHON={explicit}" if explicit else ""),
            _fix_no_python(found_notes),
        )

    path, ver = picked
    log("interpreter %s (%d.%d.%d)" % (path, ver[0], ver[1], ver[2]))
    if ver[:2] not in CI_VERSIONS:
        log(
            "notice: CI uses Python 3.13 on macOS and 3.14 on Linux; "
            "requires-python is >=3.11 with no upper bound, so this is "
            "allowed. A local-green / CI-red mystery starts here if "
            "they diverge."
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
    try:
        proc = subprocess.run(
            [python, "-m", "pip", "install", "-e", ".[dev]"],
            cwd=str(root),
            check=False,
            timeout=PIP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        die(
            f"pip install timed out after {PIP_TIMEOUT_S}s",
            "PyPI hung, or a wheel compile ran long",
            "retry, or run the manual fallback:\n" + manual_recipe(python),
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
            if rebuild:
                die(
                    "--rebuild refused: .venv is not owned by bin/check",
                    f"no ownership marker at {venv / OWNER_NAME}; "
                    "refusing to delete a venv we did not create",
                    "  mv .venv .venv.bak\n  ./bin/check",
                )
            # Use it. Do not stamp, pip-install, or rmtree: claiming
            # ownership would let a later --rebuild / health miss delete
            # a developer-managed environment.
            log("using existing unowned .venv (will not rebuild or delete it)")
            return venv

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


def _child_leads_its_group(pid: int) -> bool | None:
    """True when this pid leads its group, False when it is alive in another, None when gone.

    killpg(pid) is the wrong probe. A new pid can equal some other process
    group id, so a successful killpg can signal that other group while this
    child is still alive. setsid also fails in that case, and the child stays
    in its parent's group.
    """
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return None
    except PermissionError:
        return False


def _signal_group(pgid: int, sig: int, *, confirmed: bool = False) -> None:
    """Signal the owned group; an already-gone group is success.

    Until this pid is its own leader, signal only that pid. killpg would hit
    whatever other group currently uses this number. After the leader was
    observed to own the group, killpg stays correct even once that pid is gone:
    workers can outlive it, and no other group can hold the id while they do.
    """
    if not confirmed:
        owns = _child_leads_its_group(pgid)
        if owns is None:
            return
        if not owns:
            try:
                os.kill(pgid, sig)
            except ProcessLookupError:
                pass
            return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _wait_until_signallable(pid: int, leader: subprocess.Popen | None, seconds: float) -> bool:
    """True once this pid is observed leading its group.

    A leader that has already exited returns False. Callers that saw True
    earlier keep that fact: workers can outlive the leader, and getpgid cannot
    confirm the group after the leader has been reaped.
    """
    deadline = time.monotonic() + seconds
    while True:
        if leader is not None:
            leader.poll()
            if leader.returncode is not None:
                return False
        owns = _child_leads_its_group(pid)
        if owns:
            return True
        if owns is None:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _only_zombies_in_proc(pgid: int, proc_root: Path = Path("/proc")) -> bool:
    """True on Linux when /proc shows pgid exists only as unreaped zombies."""
    if not proc_root.is_dir():
        return False
    found = False
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _, sep, rest = text.rpartition(")")
        if not sep:
            continue
        fields = rest.split()
        if len(fields) < 3:
            continue
        try:
            member_pgid = int(fields[2])
        except ValueError:
            continue
        if member_pgid == pgid:
            found = True
            if fields[0] != "Z":
                return False
    return found


def _group_alive(pgid: int, proc_root: Path = Path("/proc")) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if _only_zombies_in_proc(pgid, proc_root):
        return False
    return True


def _wait_group_gone(
    pgid: int,
    seconds: float,
    state: dict,
    *,
    allow_hurry: bool = True,
    leader: subprocess.Popen | None = None,
) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if leader is not None:
            leader.poll()
        if not _group_alive(pgid):
            return True
        if time.monotonic() >= deadline or (allow_hurry and state["count"] > 1):
            return not _group_alive(pgid)
        time.sleep(0.02)


def _ensure_leader_dead(leader: subprocess.Popen | None) -> str | None:
    """Kill the leader pid if it is still running.

    Group checks can report the group gone while this child is alive: a pid
    can match another group, and a /proc zombie scan can miss the leader.
    Success means this Popen has been reaped.
    """
    if leader is None or leader.poll() is not None:
        return None
    try:
        os.kill(leader.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        leader.wait(timeout=GROUP_KILL_S)
    except subprocess.TimeoutExpired:
        return "members still alive after SIGTERM and SIGKILL"
    return None


def _reap_group(pgid: int, state: dict, leader: subprocess.Popen | None = None) -> str | None:
    """End every member of the owned group. Returns a failure cause or None."""
    try:
        if "owns_group" not in state:
            state["owns_group"] = _wait_until_signallable(pgid, leader, GROUP_GRACE_S)
        if leader is not None:
            leader.poll()
        owns = _child_leads_its_group(pgid)
        if not state["owns_group"] and owns is not True:
            # Still our unreaped child, but not its own group. Signal that pid
            # only. killpg(pid) would be some other group that reused the number.
            if owns is None:
                return _ensure_leader_dead(leader)
            if leader is None:
                return "members still alive after SIGTERM and SIGKILL"
            try:
                os.kill(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                leader.wait(timeout=GROUP_KILL_S)
            except subprocess.TimeoutExpired:
                return "members still alive after SIGTERM and SIGKILL"
            return None
        if not _group_alive(pgid):
            return _ensure_leader_dead(leader)
        if not _wait_group_gone(pgid, GROUP_GRACE_S, state, leader=leader):
            _signal_group(pgid, signal.SIGTERM, confirmed=True)
            if not _wait_group_gone(pgid, GROUP_TERM_S, state, leader=leader):
                _signal_group(pgid, signal.SIGKILL, confirmed=True)
                if not _wait_group_gone(
                    pgid, GROUP_KILL_S, state, allow_hurry=False, leader=leader
                ):
                    return "members still alive after SIGTERM and SIGKILL"
    except OSError as exc:
        if _wait_group_gone(pgid, GROUP_KILL_S, state, allow_hurry=False, leader=leader):
            return _ensure_leader_dead(leader)
        return f"cannot signal the group: {exc}"
    return _ensure_leader_dead(leader)


def _leader_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _await_leader(proc: subprocess.Popen, state: dict) -> None:
    """Wait for the leader. After a forwarded signal it gets SIGNALLED_LEADER_S
    (or a second signal) before the group is TERMed, then KILLed."""
    first_seen = None
    while True:
        try:
            proc.wait(timeout=0.25)
            return
        except subprocess.TimeoutExpired:
            pass
        if state["first"] is None:
            continue
        if first_seen is None:
            first_seen = time.monotonic()
        if state["count"] > 1 or time.monotonic() - first_seen >= SIGNALLED_LEADER_S:
            try:
                _signal_group(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=GROUP_TERM_S)
            except subprocess.TimeoutExpired:
                try:
                    _signal_group(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
            return


def run_owned(argv: list[str], env: dict[str, str] | None = None) -> int:
    """Run argv as the leader of its own session and process group, and end
    the whole group before returning, on success, error and catchable signals.

    The first INT/TERM/HUP is forwarded to the group (held until the group is
    recorded if it lands during the spawn); repeated ones only hurry the
    bounded TERM/KILL teardown. The result is the leader's status, or 128+N for
    the first signal received.
    SIGKILL of this driver cannot run cleanup (no parent-death watchdog).
    """
    state = {"pgid": None, "first": None, "count": 0, "error": None, "forwarded": False}

    def forward(signum: int) -> None:
        state["forwarded"] = True
        try:
            _signal_group(state["pgid"], signum)
        except OSError as exc:
            state["error"] = f"cannot forward signal {signum}: {exc}"

    def handler(signum, _frame) -> None:
        state["count"] += 1
        if state["first"] is None:
            state["first"] = signum
        if state["pgid"] is not None and not state["forwarded"]:
            forward(signum)

    # No signal mask across the spawn: the child would inherit it and ignore
    # the signals its own tests send. A signal that lands before the group is
    # recorded is held in state and forwarded right after.
    main = threading.current_thread() is threading.main_thread()
    old = {}
    if main:
        for sig in OWNED_SIGNALS:
            old[sig] = signal.signal(sig, handler)
    try:
        proc = subprocess.Popen(argv, env=env, start_new_session=True)
        state["pgid"] = proc.pid
        state["owns_group"] = _wait_until_signallable(proc.pid, proc, GROUP_GRACE_S)
        if state["first"] is not None and not state["forwarded"]:
            forward(state["first"])
        try:
            _await_leader(proc, state)
        except BaseException:
            _reap_group(proc.pid, state, leader=proc)
            raise

        cause = _reap_group(proc.pid, state, leader=proc) or state["error"]
        result = _leader_status(proc.returncode)
        if state["first"] is not None:
            result = 128 + state["first"]
        if cause:
            die(
                "pytest process group could not be ended",
                f"group {proc.pid} (pytest result: exit {result}): {cause}",
                f"  ps -axo pid,pgid,command    # members have pgid {proc.pid}\n"
                "  end only the listed pytest/xdist pids that you started",
                code=CLEANUP_FAILED_CODE,
            )
        return result
    finally:
        if main:
            for sig, previous in old.items():
                signal.signal(sig, previous)


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


SELECT_PLUGIN = '''\
"""Generated by bin/_check.py for --affected. Do not edit."""

import json
import os
import sys

import pytest

_CFG = json.load(open(os.environ["MM_SELECT_CONFIG"], encoding="utf-8"))
_ROOT = _CFG["root"]
_PREFIX = _ROOT.rstrip("/") + "/"
_RECORD = _CFG["mode"] == "record"
_SELECT = _CFG["mode"] == "select"
_SPAWN_EVENTS = {"os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "os.exec"}
_SHELLS = ("sh", "bash", "zsh", "mm", "pytest", "check")
_current = None
_reads = {}
_opaque = set()
_collected = []
_totals = {"total": 0, "kept": 0}
_state = {
    "filtered": False,
    "filters": [],
    "own_deselect": False,
    "failed": set(),
    "passed": set(),
    "worker": False,
}
_cov = None


def _audit(event, args):
    cur = _current
    if cur is None:
        return
    if event == "open":
        path = args[0]
        if isinstance(path, str) and path.endswith(".py"):
            if not path.startswith("/"):
                path = os.path.join(os.getcwd(), path)
            path = os.path.normpath(path)
            if path.startswith(_PREFIX):
                rel = path[len(_PREFIX):]
                if not rel.startswith((".venv/", ".mm-test-selection/")):
                    _reads.setdefault(cur, set()).add(rel)
    elif event == "subprocess.Popen":
        try:
            argv = [str(a) for a in (args[1] or ())]
            exe = os.path.basename(str(args[0] or (argv[0] if argv else "")))
            if exe.startswith("python") or exe in _SHELLS or any(
                a.startswith(_PREFIX) for a in argv
            ):
                _opaque.add(cur)
        except Exception:
            _opaque.add(cur)
    elif event in _SPAWN_EVENTS:
        _opaque.add(cur)


if _RECORD:
    import coverage

    os.environ.setdefault("COVERAGE_CORE", "ctrace")
    _cov = coverage.Coverage(
        data_file=None,
        source=[os.path.join(_ROOT, "src", "mind_meld")],
        config_file=False,
    )
    _cov.set_option("run:core", "ctrace")
    _cov.start()
    sys.addaudithook(_audit)


def _is_worker(config):
    return hasattr(config, "workerinput")


def _is_controller(config):
    return not _is_worker(config) and getattr(config.option, "dist", "no") != "no"


def pytest_configure(config):
    _state["worker"] = _is_worker(config)
    if _RECORD or _SELECT:
        opt = config.option
        for name in (
            "keyword", "markexpr", "deselect", "ignore", "ignore_glob",
            "lf", "failedfirst", "stepwise", "collectonly", "maxfail",
        ):
            if getattr(opt, name, None):
                _state["filtered"] = True
                _state["filters"].append(name)
    if _RECORD and _is_controller(config):
        _cov.stop()


def pytest_deselected(items):
    if (_RECORD or _SELECT) and items and not _state["own_deselect"]:
        _state["filtered"] = True
        _state["filters"].append("deselected")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    total = len(items)
    if _SELECT:
        if _state["filtered"]:
            raise pytest.UsageError(
                "bin/check: --affected cannot run with pytest filters (%s)"
                % ", ".join(sorted(set(_state["filters"])))
            )
        ids = set(_CFG["ids"])
        files = set(_CFG["files"])
        keep, gone = [], []
        for i in items:
            wanted = i.nodeid in ids or i.nodeid.split("::", 1)[0] in files
            (keep if wanted else gone).append(i)
        if gone:
            _state["own_deselect"] = True
            try:
                config.hook.pytest_deselected(items=gone)
            finally:
                _state["own_deselect"] = False
        items[:] = keep
    _totals["total"] = total
    _totals["kept"] = len(items)
    _collected[:] = [i.nodeid for i in items]
    if _is_worker(config):
        config.workeroutput["mm_total"] = total
        config.workeroutput["mm_kept"] = len(items)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _current
    if _RECORD:
        _cov.switch_context(item.nodeid)
        _current = item.nodeid
    yield
    if _RECORD:
        _current = None
        _cov.switch_context("")


def pytest_runtest_logreport(report):
    if not _state["worker"]:
        if report.failed:
            _state["failed"].add(report.nodeid)
        elif report.when == "call" and report.passed:
            _state["passed"].add(report.nodeid)


def pytest_testnodedown(node, error):
    out = getattr(node, "workeroutput", None) or {}
    if "mm_total" in out and not _totals["total"]:
        _totals["total"] = out["mm_total"]
        _totals["kept"] = out["mm_kept"]


def _dump(path, payload):
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def _read_prior_failed(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {n for n in data if isinstance(n, str)} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if (
        _SELECT
        and not _state["filtered"]
        and exitstatus == 5
        and _totals["total"] > 0
        and _totals["kept"] == 0
    ):
        session.exitstatus = 0
        exitstatus = 0
    if _RECORD and not _is_controller(config):
        _cov.stop()
        data = _cov.get_data()
        ctx = {}
        for name in data.measured_files():
            if not name.startswith(_PREFIX):
                continue
            seen = set()
            for contexts in data.contexts_by_lineno(name).values():
                seen.update(c for c in contexts if c)
            if seen:
                ctx[name[len(_PREFIX):]] = sorted(seen)
        part = {
            "ctx": ctx,
            "reads": {k: sorted(v) for k, v in _reads.items()},
            "opaque": sorted(_opaque),
            "collected": list(_collected),
            "total": _totals["total"],
            "filtered": _state["filtered"],
            "filters": sorted(set(_state["filters"])),
        }
        _dump(os.path.join(_CFG["out_dir"], "part-%d.json" % os.getpid()), part)
    if not _is_worker(config) and not _state["filtered"] and _CFG.get("failed_path"):
        failed_path = _CFG["failed_path"]
        if exitstatus == 0 and not _state["failed"]:
            _dump(failed_path, [])
        else:
            prior = _read_prior_failed(failed_path)
            merged = ((prior - _state["passed"]) | _state["failed"]) - (
                _state["passed"] - _state["failed"]
            )
            _dump(failed_path, sorted(merged))


def pytest_terminal_summary(terminalreporter, config):
    if not _SELECT:
        return
    terminalreporter.write_line(
        "bin/check: affected: ran %d of %d collected tests" % (_totals["kept"], _totals["total"])
    )
'''


class SelectError(Exception):
    """A selector failure. The caller reports it and runs the full suite."""

    def __init__(self, operation: str, cause: str, code: str, action: str) -> None:
        super().__init__(f"{operation}: {cause}")
        self.operation = operation
        self.cause = cause
        self.code = code
        self.action = action


def select_error_report(err: SelectError, base: str, sel_dir: Path) -> None:
    log(f"affected: {err.operation} failed: {err.cause}")
    log(f"affected: code={err.code} base={base} cache={sel_dir}")
    log(f"affected: {err.action}")
    log("affected: running the full suite instead")


_GIT_SCRUB_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


def _git(root: Path, *args: str, op: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB_VARS}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SelectError(
            op, f"git did not run ({exc})", "git-unavailable", "install git, or run ./bin/check"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        raise SelectError(
            op,
            f"git exited {proc.returncode}: {detail[0] if detail else 'no output'}",
            "git-failed",
            "run from a git clone with the base ref present, or pass --base REF",
        )
    return proc.stdout


def resolve_merge_base(root: Path, base: str) -> str:
    _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", op="resolve base")
    return _git(root, "merge-base", base, "HEAD", op="merge base").decode().strip()


def changed_since(root: Path, merge_base: str) -> dict[str, str]:
    """Paths that differ from the merge base: committed, staged, unstaged and
    untracked. A rename arrives as a delete plus an add, so both paths show."""
    out = _git(root, "diff", "--name-status", "-z", "--no-renames", merge_base, op="changed files")
    parts = out.decode("utf-8", "surrogateescape").split("\0")
    changed: dict[str, str] = {}
    for i in range(0, len(parts) - 1, 2):
        changed[parts[i + 1]] = parts[i][:1]
    new = _git(root, "ls-files", "-z", "--others", "--exclude-standard", op="untracked files")
    for path in new.decode("utf-8", "surrogateescape").split("\0"):
        if path:
            changed[path] = "A"
    return {p: s for p, s in changed.items() if not p.startswith(SELECT_DIR + "/")}


def _module_called_names(node: ast.AST) -> set[str]:
    """Function names called at module or class scope (outside def bodies)."""
    called: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for expr in (
                *child.decorator_list,
                *child.args.defaults,
                *(d for d in child.args.kw_defaults if d is not None),
            ):
                for sub in ast.walk(expr):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                        called.add(sub.func.id)
            continue
        if isinstance(child, ast.ClassDef):
            called.update(_module_called_names(child))
            for expr in (
                *child.bases,
                *(k.value for k in child.keywords),
                *child.decorator_list,
            ):
                for sub in ast.walk(expr):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                        called.add(sub.func.id)
            continue
        for sub in ast.walk(child):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                called.add(sub.func.id)
    return called


def _strip_bodies(node: ast.AST, keep_bodies: set[str]) -> None:
    """Skeleton: keep all def headers (signatures, defaults, annotations and
    decorators run or bind at import time), keep leading docstrings on decorated
    defs (Typer/click inspect __doc__ at import time), and keep bodies of local
    functions called at module/class scope; replace other def bodies with Pass."""
    for field, value in ast.iter_fields(node):
        if not isinstance(value, list):
            continue
        kept = []
        for child in value:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name not in keep_bodies:
                    if (
                        child.decorator_list
                        and child.body
                        and isinstance(child.body[0], ast.Expr)
                        and isinstance(child.body[0].value, ast.Constant)
                        and isinstance(child.body[0].value.value, str)
                    ):
                        child.body = [child.body[0], ast.Pass()]
                    else:
                        child.body = [ast.Pass()]
            elif isinstance(child, ast.AST):
                _strip_bodies(child, keep_bodies)
            kept.append(child)
        setattr(node, field, kept)


def skeleton_digest(source: bytes) -> str | None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    _strip_bodies(tree, _module_called_names(tree))
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def inventory(
    root: Path, extra_paths: tuple[str, ...] | list[str] | set[str] = ()
) -> dict[str, str]:
    """sha256 of every Python file the selector reasons about, plus the
    runner/config files and any extra dirty paths, keyed by repo-relative path."""
    found: dict[str, str] = {}
    for top in ("src/mind_meld", "tests", "bin"):
        for path in sorted((root / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            digest = _sha(path)
            if digest:
                found[path.relative_to(root).as_posix()] = digest
    for name in SELECT_CONFIG_PATHS:
        digest = _sha(root / name)
        if digest:
            found[name] = digest
    for name in sorted(extra_paths):
        if name not in found and not name.startswith(SELECT_DIR + "/"):
            found[name] = _sha(root / name) or "missing"
    return found


def config_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    names = list(SELECT_CONFIG_PATHS)
    names += sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("conftest.py"))
    for name in sorted(set(names)):
        hasher.update(f"{name}={_sha(root / name)}\n".encode())
    return hasher.hexdigest()


ENV_PROBE = """\
import hashlib, importlib.metadata as m, json, os, sys
dists = sorted({"%s==%s" % (d.metadata["Name"], d.version) for d in m.distributions()})
try:
    import coverage
    cov = True
except ImportError:
    cov = False
fp = hashlib.sha256(
    ("\\n".join(dists) + sys.version + os.path.realpath(sys.executable)).encode()
).hexdigest()
print(json.dumps({"fp": fp, "coverage": cov}))
"""


def probe_env(tool: Path) -> dict:
    try:
        proc = subprocess.run(
            [str(tool), "-c", ENV_PROBE], capture_output=True, text=True, timeout=15, check=False
        )
        result = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        result = None
    if not isinstance(result, dict):
        raise SelectError(
            "probe tool environment",
            f"{tool} could not report its packages",
            "env-probe",
            "check that this interpreter runs, or run ./bin/check",
        )
    return result


def ensure_select_dir(root: Path) -> Path:
    sel = root / SELECT_DIR
    if sel.is_symlink():
        raise SelectError(
            "open cache",
            f"{sel} is a symlink",
            "cache-symlink",
            f"remove it yourself ({sel}); bin/check will not follow it",
        )
    owner = sel / SELECT_OWNER
    if sel.exists():
        if not owner.is_file() and any(sel.iterdir()):
            raise SelectError(
                "open cache",
                f"{sel} exists but bin/check did not create it",
                "cache-unowned",
                f"move it aside: mv {SELECT_DIR} {SELECT_DIR}.bak",
            )
    try:
        sel.mkdir(exist_ok=True)
        owner.write_text(OWNER_BODY, encoding="utf-8")
    except OSError as exc:
        raise SelectError(
            "open cache", str(exc), "cache-unwritable", f"make {sel} writable, or run ./bin/check"
        ) from exc
    return sel


def atomic_write_json(path: Path, payload: object) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def load_record(sel: Path, env: dict, config: str, merge_base: str) -> tuple[dict | None, str]:
    """A record is usable only if complete, readable and made for this
    interpreter, configuration and merge base. Returns (record, reason)."""
    path = sel / SELECT_RECORD
    if not path.is_file():
        return None, "no coverage record yet (first --affected run)"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        nodes = rec["nodes"]
        ok = (
            rec["schema"] == SELECT_SCHEMA
            and rec["complete"] is True
            and isinstance(nodes, list)
            and isinstance(rec["ctx"], dict)
            and isinstance(rec["reads"], dict)
            and isinstance(rec["opaque"], list)
            and isinstance(rec["files"], dict)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None, f"coverage record at {path} is unreadable or truncated"
    if not ok:
        return None, "coverage record has an unknown schema"
    if not nodes:
        return None, "coverage record holds no tests"
    if rec.get("env") != env["fp"]:
        return None, "interpreter or installed packages changed since the record"
    if rec.get("config") != config:
        return None, "runner, pyproject or conftest changed since the record"
    if rec.get("merge_base") != merge_base:
        return None, "merge base moved since the record"
    if not any(rec["ctx"].values()):
        return None, "record has no coverage of src/mind_meld in this worktree"
    return rec, ""


class Plan:
    __slots__ = ("full", "ids", "files", "total")

    def __init__(self, total: int) -> None:
        self.full: str | None = None
        self.ids: set[str] = set()
        self.files: set[str] = set()
        self.total = total

    @property
    def empty(self) -> bool:
        return self.full is None and not self.ids and not self.files


def _is_test_module(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        and name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _expand_test_importers(root: Path, initial_files: set[str]) -> set[str]:
    """Include test modules that import from any changed test module."""
    if not initial_files:
        return set()
    reverse: dict[str, set[str]] = {}
    for path in sorted((root / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        pkg = path.parent.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_bytes())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module and node.module.startswith("tests"):
                    base_target = node.module.replace(".", "/")
                    reverse.setdefault(base_target + ".py", set()).add(rel)
                    for alias in node.names:
                        reverse.setdefault(f"{base_target}/{alias.name}.py", set()).add(rel)
                elif node.level == 1:
                    if node.module:
                        reverse.setdefault(f"{pkg}/{node.module.replace('.', '/')}.py", set()).add(
                            rel
                        )
                    else:
                        for alias in node.names:
                            reverse.setdefault(f"{pkg}/{alias.name}.py", set()).add(rel)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("tests."):
                        reverse.setdefault(alias.name.replace(".", "/") + ".py", set()).add(rel)
    expanded = set(initial_files)
    queue = list(initial_files)
    while queue:
        cur = queue.pop()
        for dep in reverse.get(cur, ()):
            if dep not in expanded:
                expanded.add(dep)
                queue.append(dep)
    return expanded


def plan_selection(
    root: Path, rec: dict, merge_base: str, changed: dict[str, str], failed: list[str]
) -> Plan:
    """Which tests does the change reach? Anything this cannot prove narrow
    becomes a full run with its reason; uncertainty never selects zero."""
    nodes: list[str] = rec["nodes"]
    plan = Plan(len(nodes))
    recorded: dict[str, dict] = rec["files"]
    now = inventory(root, set(recorded) | set(changed))
    drifted = {p for p in set(recorded) | set(now) if recorded.get(p, {}).get("sha") != now.get(p)}
    paths = sorted(set(changed) | drifted)
    readers = rec["reads"]
    ctx = rec["ctx"]
    src_changed = False
    for path in paths:
        if path in SELECT_CONFIG_PATHS or path.endswith("conftest.py"):
            plan.full = f"runner, config or conftest changed: {path}"
        elif path.startswith("tests/") and not _is_test_module(path):
            plan.full = f"shared test input changed: {path}"
        elif not path.endswith(".py"):
            plan.full = f"non-Python file changed: {path}"
        elif _is_test_module(path):
            plan.files.add(path)
            plan.ids.update(nodes[i] for i in readers.get(path, []))
        elif path.startswith("src/mind_meld/"):
            reason = _module_scope_change(root, rec, merge_base, path, changed, path in drifted)
            if reason:
                plan.full = reason
            else:
                src_changed = True
                for table in (ctx, readers):
                    plan.ids.update(nodes[i] for i in table.get(path, []))
        else:
            plan.full = f"Python file outside src/ and tests/ changed: {path}"
        if plan.full:
            return plan
    if plan.files:
        expanded = _expand_test_importers(root, plan.files)
        for dep in expanded - plan.files:
            if not _is_test_module(dep):
                plan.full = f"shared test input imports changed test module: {dep}"
                return plan
        plan.files = expanded
    if src_changed:
        plan.ids.update(nodes[i] for i in rec["opaque"])
    recorded_nodes = set(nodes)
    for n in failed:
        f = n.split("::", 1)[0]
        if f in plan.files or n in recorded_nodes:
            plan.ids.add(n)
    # A node whose test file is gone cannot run; its file is already a change.
    plan.ids = {n for n in plan.ids if (root / n.split("::", 1)[0]).is_file()}
    plan.files = {f for f in plan.files if (root / f).is_file()}
    return plan


def _module_scope_change(
    root: Path, rec: dict, merge_base: str, path: str, changed: dict[str, str], drift: bool
) -> str | None:
    """None when only function bodies differ; otherwise why we cannot say."""
    here = root / path
    if not here.is_file():
        return f"module removed: {path}"
    if path in changed and changed[path] == "A":
        return f"module added: {path}"
    current = skeleton_digest(here.read_bytes())
    if current is None:
        return f"cannot parse {path}"
    if path in changed:
        old = _git(root, "show", f"{merge_base}:{path}", op="read base file")
        if skeleton_digest(old) != current:
            return f"module-scope change in {path}"
    if drift:
        entry = rec["files"].get(path)
        if not entry or entry.get("skel") != current:
            return f"module-scope change in {path} since the record"
    return None


def write_plugin(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mm_select_plugin.py").write_text(SELECT_PLUGIN, encoding="utf-8")


def pytest_env(directory: Path, config_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(directory) + (os.pathsep + old if old else "")
    env["MM_SELECT_CONFIG"] = str(config_path)
    return env


def _merge_parts(out_dir: Path) -> dict:
    parts = []
    for path in sorted(out_dir.glob("part-*.json")):
        parts.append(json.loads(path.read_text(encoding="utf-8")))
    if not parts:
        raise ValueError("no coverage part was written")
    for part in parts:
        if part["filtered"]:
            raise ValueError("pytest filtered the run (%s)" % ", ".join(part["filters"]))
    collected = parts[0]["collected"]
    if any(part["collected"] != collected for part in parts if part["collected"]):
        raise ValueError("workers collected different tests")
    if not collected:
        raise ValueError("collected no tests")
    return {
        "nodes": sorted(collected),
        "parts": parts,
    }


def build_record(
    root: Path, out_dir: Path, start: dict[str, str], env: dict, config: str, merge_base: str
) -> dict:
    merged = _merge_parts(out_dir)
    nodes = merged["nodes"]
    index = {n: i for i, n in enumerate(nodes)}
    ctx: dict[str, set[int]] = {}
    reads: dict[str, set[int]] = {}
    opaque: set[int] = set()
    for part in merged["parts"]:
        for path, contexts in part["ctx"].items():
            matched = {index[c] for c in contexts if c in index}
            if matched:
                ctx.setdefault(path, set()).update(matched)
        for node, paths in part["reads"].items():
            if node in index:
                for path in paths:
                    reads.setdefault(path, set()).add(index[node])
        opaque.update(index[n] for n in part["opaque"] if n in index)
    files = {}
    for path, sha in start.items():
        entry = {"sha": sha}
        if path.startswith("src/mind_meld/") and (root / path).is_file():
            entry["skel"] = skeleton_digest((root / path).read_bytes())
        files[path] = entry
    return {
        "schema": SELECT_SCHEMA,
        "complete": True,
        "env": env["fp"],
        "config": config,
        "merge_base": merge_base,
        "nodes": nodes,
        "ctx": {p: sorted(v) for p, v in ctx.items() if v},
        "reads": {p: sorted(v) for p, v in reads.items()},
        "opaque": sorted(opaque),
        "files": files,
    }


def full_pytest(
    args: Args, tool: Path, n_args: list[str], env: dict[str, str] | None = None
) -> int:
    name = "pytest " + " ".join(args.scope)
    log(name)
    return run_owned([str(tool), "-m", "pytest", *args.scope, *n_args], env)


def fallback_pytest(args: Args, sel: Path, tool: Path, n_args: list[str]) -> int:
    """Full-suite fallback for --affected that still tracks failed.json when sel is writable."""
    run = sel / f"fallback-{os.getpid()}"
    try:
        if not sel.is_dir() or sel.is_symlink():
            raise OSError("selection dir unavailable")
        write_plugin(run)
        cfg = run / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "mode": "fallback",
                    "root": str(sel.parent),
                    "failed_path": str(sel / SELECT_FAILED),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        shutil.rmtree(run, ignore_errors=True)
        return _clean_failed_on_green(sel, full_pytest(args, tool, n_args))
    try:
        return _clean_failed_on_green(
            sel,
            full_pytest(args, tool, ["-p", "mm_select_plugin", *n_args], pytest_env(run, cfg)),
        )
    finally:
        shutil.rmtree(run, ignore_errors=True)


def run_affected(args: Args, root: Path, tool: Path, n_args: list[str]) -> int:
    """--affected: returns the pytest result; selector trouble means a full run."""
    base = args.base or SELECT_DEFAULT_BASE
    sel = root / SELECT_DIR
    try:
        sel = ensure_select_dir(root)
        merge_base = resolve_merge_base(root, base)
        env = probe_env(tool)
        config = config_digest(root)
        record, why = load_record(sel, env, config, merge_base)
        if args.refresh:
            record, why = None, "refresh requested"
        if record is None:
            if not env["coverage"]:
                raise SelectError(
                    "record coverage",
                    f"the coverage package is missing from {tool}",
                    "coverage-missing",
                    "run ./bin/check once without MM_VENV/VIRTUAL_ENV to install dev deps",
                )
            log(f"affected: {why}; running the full suite to record coverage")
            return seed_record(args, root, sel, tool, n_args, env, config, merge_base)
        failed = _load_failed(sel)
        plan = plan_selection(root, record, merge_base, changed_since(root, merge_base), failed)
    except SelectError as err:
        select_error_report(err, base, sel)
        return fallback_pytest(args, sel, tool, n_args)
    except Exception as exc:
        err = SelectError("select tests", f"{type(exc).__name__}: {exc}", "unexpected", "report it")
        select_error_report(err, base, sel)
        return fallback_pytest(args, sel, tool, n_args)
    if plan.full:
        log(f"affected: {plan.full}; running the full suite")
        return fallback_pytest(args, sel, tool, n_args)
    if plan.empty:
        log(f"affected: ran 0 of {plan.total} recorded tests: nothing the record reaches changed")
        return 0
    log(
        f"affected: base {base} ({merge_base[:9]}): "
        f"{len(plan.ids)} recorded tests plus {len(plan.files)} changed test files"
    )
    return select_run(args, sel, tool, n_args, plan)


def _load_failed(sel: Path) -> list[str]:
    try:
        data = json.loads((sel / SELECT_FAILED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [n for n in data if isinstance(n, str)] if isinstance(data, list) else []


def _clean_failed_on_green(sel: Path, result: int) -> int:
    if result == 0:
        try:
            (sel / SELECT_FAILED).unlink()
        except OSError:
            pass
    return result


def select_run(args: Args, sel: Path, tool: Path, n_args: list[str], plan: Plan) -> int:
    run = sel / f"run-{os.getpid()}"
    try:
        write_plugin(run)
        cfg = run / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "mode": "select",
                    "root": str(sel.parent),
                    "ids": sorted(plan.ids),
                    "files": sorted(plan.files),
                    "failed_path": str(sel / SELECT_FAILED),
                }
            ),
            encoding="utf-8",
        )
        log("pytest " + " ".join(args.scope) + " (affected)")
        return run_owned(
            [str(tool), "-m", "pytest", *args.scope, "-p", "mm_select_plugin", *n_args],
            pytest_env(run, cfg),
        )
    finally:
        shutil.rmtree(run, ignore_errors=True)


def seed_record(
    args: Args,
    root: Path,
    sel: Path,
    tool: Path,
    n_args: list[str],
    env: dict,
    config: str,
    merge_base: str,
) -> int:
    fd = os.open(sel / SELECT_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            die(
                "another --affected run is recording coverage here",
                f"{sel / SELECT_LOCK} is held; the existing record is untouched",
                "  wait for it to finish, then re-run ./bin/check --affected",
            )
        cand = sel / f"candidate-{os.getpid()}"
        try:
            shutil.rmtree(cand, ignore_errors=True)
            write_plugin(cand)
            cfg = cand / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "mode": "record",
                        "root": str(root),
                        "out_dir": str(cand),
                        "failed_path": str(sel / SELECT_FAILED),
                    }
                ),
                encoding="utf-8",
            )
            extra = set(changed_since(root, merge_base))
            start = inventory(root, extra)
            log("pytest " + " ".join(args.scope) + " (recording coverage)")
            result = run_owned(
                [str(tool), "-m", "pytest", *args.scope, "-p", "mm_select_plugin", *n_args],
                pytest_env(cand, cfg),
            )
            if result != 0:
                log("affected: run was not green; the coverage record is unchanged")
                return result
            _clean_failed_on_green(sel, result)
            try:
                now_extra = extra | set(changed_since(root, merge_base))
                if inventory(root, now_extra) != start:
                    raise ValueError("files changed while the suite ran")
                record = build_record(root, cand, start, env, config, merge_base)
                if not any(record["ctx"].values()):
                    raise ValueError(f"no coverage of src/mind_meld under {root}")
                atomic_write_json(sel / SELECT_RECORD, record)
            except (OSError, ValueError, KeyError) as exc:
                log(f"affected: record not updated: {exc}")
                return result
            log(f"affected: recorded coverage for {len(record['nodes'])} tests")
            return result
        finally:
            shutil.rmtree(cand, ignore_errors=True)
    finally:
        os.close(fd)


def validate_affected(args: Args) -> None:
    if (args.refresh or args.base) and not args.affected:
        die(
            "--base and --refresh only work with --affected",
            "neither changes a plain run",
            "  ./bin/check --affected --refresh",
        )
    if args.affected:
        clash = [
            label
            for label, hit in (
                ("a test path", args.scope_given),
                ("pytest arguments", bool(args.pytest_extra)),
                ("--lint", args.lint),
            )
            if hit
        ]
        if clash:
            die(
                "--affected selects the tests itself",
                "it cannot be combined with " + ", ".join(clash),
                "  ./bin/check --affected          # tests the change reaches\n"
                "  ./bin/check tests/x.py -- -k n  # your own scope",
            )


def print_help() -> None:
    text = """\
usage: ./bin/check [paths...] [-- pytest-args]

Self-bootstrapping verification for mind-meld. Cards describe scope;
they must not know where Python lives.

Default: ruff check .  ->  ruff format --check .  ->  pytest tests/
Cheap gates first. Fail-fast; each tool's exit code is preserved.

While working and reviewing, use --affected: it runs only the tests whose
recorded coverage reaches the files changed since the merge base (plus new or
changed test files) and prints "ran N of M collected tests". CI and plain
./bin/check run the whole suite; --affected never replaces that before merge.
The first run (or a stale record, or --refresh) runs everything once to record
coverage in .mm-test-selection/ (ignored). Runner, config, conftest, non-Python
and module-scope changes run everything and say why.

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
  --affected       pytest only the tests the change reaches (not with paths,
                   pytest args or --lint)
  --base REF       merge-base ref for --affected (default: origin/main)
  --refresh        with --affected: re-record coverage from a full run

env:
  MM_PYTHON        interpreter for the venv (default: python3.13, then 3.14,
                   3.12, 3.11, python3). Printed unconditionally.
  MM_VENV          validate-and-use or fail; never mutated
  MM_PYTEST_WORKERS  xdist worker count (default: auto)
  VIRTUAL_ENV      treated like --no-bootstrap when set

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

    validate_affected(args)

    root = repo_root()
    try:
        os.chdir(root)
    except OSError as exc:
        die(
            f"cannot chdir to repo root {root}",
            str(exc),
            "invoke ./bin/check via its real path in a mind-meld clone",
        )

    skip_bootstrap = (
        bool(os.environ.get("MM_VENV")) or args.no_bootstrap or bool(os.environ.get("VIRTUAL_ENV"))
    )
    python: str | None = None
    bootstrapped: Path | None = None
    if skip_bootstrap:
        log("bootstrap skipped (--no-bootstrap, VIRTUAL_ENV, or MM_VENV)")
    else:
        python, version = resolve_interpreter()
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
    log("tool %s" % tool)

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
        if args.affected:
            name = "pytest (affected)"
            result = run_affected(args, root, Path(tool), n_args)
        else:
            name = "pytest " + " ".join(args.scope)
            log(name)
            result = run_owned([str(tool), "-m", "pytest", *args.scope, *n_args, *extra])
        if result != 0:
            die(
                f"{name} failed (exit {result})",
                "see pytest's output above",
                "see pytest's output above",
                code=result,
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("bin/check: interrupted\n")
        raise SystemExit(130) from None
