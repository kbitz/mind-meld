"""Live Secret Service test (Track 73A) — real gnome-keyring over D-Bus.

Opt-in only: runs when MM_LIVE_SECRET_SERVICE=1 (the secret-service-live
CI job, or a local Linux box with dbus + gnome-keyring + libsecret).
With the flag set, missing tools FAIL the test — never skip — so a
provisioning regression cannot masquerade as a pass.

Each scenario group runs a child (`<this file> child <scenario>`) under
its own `dbus-run-session` with temp HOME/XDG dirs and an environment
built from scratch. The child starts gnome-keyring-daemon as a tracked
process, exercises crypto's non-interactive reader against the real
service, and prints exactly one JSON result line on stdout. The parent
kills and reaps the child's whole process group on every outcome.

This file refuses to touch a real desktop session: the child asserts
its session bus differs from the parent's before doing anything.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_LIVE = os.environ.get("MM_LIVE_SECRET_SERVICE") == "1"
live_only = pytest.mark.skipif(
    not _LIVE, reason="live Secret Service test requires MM_LIVE_SECRET_SERVICE=1"
)

_CHILD_TIMEOUT_S = 60
_PROMPT_METHODS = ("Unlock", "Prompt", "CreateCollection")


# ── parent-side harness ──────────────────────────────────────────────


def _kill_process_group(proc: subprocess.Popen, *, timeout: float = 5.0) -> None:
    """SIGKILL the child's whole process group and reap the leader, bounded.

    The group is killed even when the leader already exited: a member
    (dbus-daemon, gnome-keyring-daemon) can outlive it and hold the pipes.
    The PGID stays valid while any member lives.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _pid_running(pid: int) -> bool:
    """True while `pid` exists and is not a zombie awaiting its reaper.

    A container whose PID 1 never reaps orphans keeps a killed grandchild
    as a zombie, and kill(pid, 0) still succeeds on zombies.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # /proc/<pid>/stat: the state follows the parenthesized comm.
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        try:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return True
        if not state:
            return False
    return not state.startswith("Z")


def _run_live_child(scenario: str, tmp_path: Path) -> dict:
    for tool in ("dbus-run-session", "gnome-keyring-daemon"):
        if shutil.which(tool) is None:
            pytest.fail(
                f"MM_LIVE_SECRET_SERVICE=1 but {tool} is not on PATH; "
                "install dbus + gnome-keyring (see README Development)"
            )
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    for d in (home, tmp_path / "xdg-data", tmp_path / "xdg-config", runtime):
        d.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    # Environment built from scratch: no inherited PYTHON_KEYRING_BACKEND,
    # MINDMELD_PASSPHRASE, PYTEST_CURRENT_TEST or DBUS_SESSION_BUS_ADDRESS.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": str(home),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_RUNTIME_DIR": str(runtime),
        "MM_PARENT_DBUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
    }
    proc = subprocess.Popen(
        [
            "dbus-run-session",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "child",
            scenario,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=_CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            _out, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stderr = "<pipes still held after the process-group kill>"
        pytest.fail(
            f"live child {scenario!r} timed out after {_CHILD_TIMEOUT_S}s; "
            f"stderr tail:\n{(stderr or '')[-2000:]}"
        )
    finally:
        _kill_process_group(proc)
    lines = [line for line in stdout.splitlines() if line.strip()]
    payload: dict = {}
    if lines:
        try:
            payload = json.loads(lines[-1])
        except ValueError:
            payload = {"unparsed": lines[-1][-500:]}
    # The child's own JSON (its error and the checks it reached) separates
    # a provisioning failure from a behavioral one; show it on every failure.
    assert proc.returncode == 0, (
        f"live child {scenario!r} exited {proc.returncode}; result: {payload}; "
        f"stderr tail:\n{stderr[-2000:]}"
    )
    assert lines, f"live child {scenario!r} printed no result line; stderr:\n{stderr[-2000:]}"
    assert payload.get("ok") is True, f"live child {scenario!r} payload: {payload}"
    failed = [k for k, v in payload.get("checks", {}).items() if v is not True]
    assert not failed, f"live child {scenario!r} failed checks: {failed} ({payload})"
    return payload


@live_only
def test_live_secret_service_main(tmp_path):
    """Unlocked read, secret-tool parity, locked skip, negative control."""
    payload = _run_live_child("main", tmp_path)
    assert payload["scenario"] == "main"


@live_only
def test_live_secret_service_no_default_collection(tmp_path):
    """Fresh session with no default collection -> no-default-collection."""
    payload = _run_live_child("no-default", tmp_path)
    assert payload["scenario"] == "no-default"


def test_process_group_teardown_kills_whole_group():
    """Portable: the teardown helper kills grandchildren, not just the leader."""
    child_code = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(g.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        grandchild_pid = int(proc.stdout.readline().strip())
        assert _pid_running(grandchild_pid)  # alive before teardown
        _kill_process_group(proc)
        assert proc.poll() is not None
        _assert_gone(grandchild_pid)
    finally:
        _kill_process_group(proc)


def test_process_group_teardown_after_the_leader_exited():
    """Portable: a member that outlives the leader is still killed."""
    child_code = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdout=subprocess.DEVNULL)\n"
        "print(g.pid, flush=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        grandchild_pid = int(proc.stdout.readline().strip())
        proc.wait(timeout=10)  # leader gone; grandchild still in the group
        assert _pid_running(grandchild_pid)
        _kill_process_group(proc)
        _assert_gone(grandchild_pid)
    finally:
        _kill_process_group(proc)


def _assert_gone(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while _pid_running(pid):
        if time.monotonic() > deadline:
            raise AssertionError(f"pid {pid} survived process-group teardown")
        time.sleep(0.05)


# ── child side (runs under dbus-run-session; never under pytest) ────


class _Sentinel(Exception):
    """Raised by the armed call spy when a prompt method is reached."""


def _child_fail(checks: dict, scenario: str, error: str) -> int:
    print(json.dumps({"scenario": scenario, "ok": False, "checks": checks, "error": error}))
    return 1


def _wait_service_owned(deadline_s: float = 10.0) -> None:
    from contextlib import closing

    import secretstorage
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import Proxy

    deadline = time.monotonic() + deadline_s
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with closing(secretstorage.dbus_init()) as conn:
                if Proxy(message_bus, conn).NameHasOwner("org.freedesktop.secrets")[0] == 1:
                    return
        except Exception as e:  # daemon still starting
            last = e
        time.sleep(0.1)
    raise RuntimeError(f"org.freedesktop.secrets not owned in {deadline_s}s: {last!r}")


def _start_daemon(*, unlock: bool) -> subprocess.Popen:
    args = ["gnome-keyring-daemon", "--foreground", "--components=secrets"]
    if unlock:
        args.append("--unlock")
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    if unlock:
        assert proc.stdin is not None
        proc.stdin.write("throwaway-live-test-password\n")
        proc.stdin.flush()
        proc.stdin.close()
    return proc


def _stop_daemon(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _install_call_spy():
    """Spy on secretstorage.util.DBusAddressWrapper.call (class attribute)."""
    from secretstorage.util import DBusAddressWrapper

    real_call = DBusAddressWrapper.call
    records: list[str] = []
    armed = {"on": False}

    def spy_call(self, method, signature, *body):
        records.append(method)
        if armed["on"] and method in _PROMPT_METHODS:
            raise _Sentinel(method)
        return real_call(self, method, signature, *body)

    DBusAddressWrapper.call = spy_call
    return records, armed


def _ss_member():
    import keyring
    from keyring.backends.SecretService import Keyring as SSKeyring

    from mind_meld import crypto

    backend = keyring.get_keyring()
    for member in crypto._keyring_members(backend):
        if isinstance(member, SSKeyring):
            return member
    raise RuntimeError(f"no Secret Service backend member in {backend!r}")


def _child_preflight(checks: dict) -> None:
    """Refuse to run anywhere but inside our own dbus-run-session."""
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    parent_bus = os.environ.get("MM_PARENT_DBUS_ADDRESS", "")
    checks["bus_differs_from_parent"] = bool(bus) and bus != parent_bus
    if not checks["bus_differs_from_parent"]:
        raise RuntimeError("child bus missing or identical to parent's; refusing to run")
    for forbidden in ("PYTHON_KEYRING_BACKEND", "PYTEST_CURRENT_TEST"):
        if forbidden in os.environ:
            raise RuntimeError(f"inherited {forbidden}; environment was not built fresh")
    checks["fresh_environment"] = "MINDMELD_PASSPHRASE" not in os.environ


def _child_main_scenario() -> int:
    checks: dict = {}
    daemon = None
    try:
        _child_preflight(checks)
        records, armed = _install_call_spy()
        daemon = _start_daemon(unlock=True)
        _wait_service_owned()

        import keyring

        from mind_meld import crypto
        from mind_meld.errors import KeyringUnavailableError

        member = _ss_member()

        # Unlocked: keyring writes the item, the narrow reader reads it.
        keyring.set_password("mind-meld", "passphrase", "live-test-pw")
        checks["set_then_read"] = crypto._read_secret_service(member) == "live-test-pw"

        # secret-tool parity: an item stored by libsecret is readable.
        # Delete keyring's item first so the read can only be satisfied by
        # the secret-tool item, whether or not its store replaces keyring's.
        keyring.delete_password("mind-meld", "passphrase")
        secret_tool = shutil.which("secret-tool")
        if secret_tool is None:
            raise RuntimeError("secret-tool missing; install libsecret")
        subprocess.run(
            [
                secret_tool,
                "store",
                "--label=mind-meld",
                "service",
                "mind-meld",
                "username",
                "passphrase",
                "application",
                "Python keyring library",
            ],
            input="tool-stored-pw",
            text=True,
            check=True,
            timeout=15,
        )
        checks["secret_tool_read"] = crypto._read_secret_service(member) == "tool-stored-pw"

        # Lock the collection. The spy resets here: nothing below this
        # point may reach Unlock / Prompt / CreateCollection.
        from contextlib import closing

        import secretstorage

        with closing(secretstorage.dbus_init()) as conn:
            collection = secretstorage.Collection(conn)
            collection.lock()
            checks["lock_applied"] = collection.is_locked() is True
        records.clear()

        try:
            crypto.get_passphrase(non_interactive=True)
            checks["locked_raises"] = False
        except KeyringUnavailableError as e:
            checks["locked_raises"] = e.reason == "locked"

        os.environ["MINDMELD_PASSPHRASE"] = "env-fallback-pw"
        try:
            checks["locked_env_fallback"] = (
                crypto.get_passphrase(non_interactive=True) == "env-fallback-pw"
            )
        finally:
            del os.environ["MINDMELD_PASSPHRASE"]

        with closing(secretstorage.dbus_init()) as conn:
            checks["still_locked_afterwards"] = secretstorage.Collection(conn).is_locked() is True
        checks["no_prompt_methods"] = not any(m in _PROMPT_METHODS for m in records)

        # Negative control: keyring's own read on the locked collection
        # DOES reach Unlock (the spy raises instead of prompting).
        armed["on"] = True
        try:
            keyring.get_password("mind-meld", "passphrase")
            checks["negative_control_reaches_unlock"] = False
        except _Sentinel:
            checks["negative_control_reaches_unlock"] = True
        finally:
            armed["on"] = False

        print(json.dumps({"scenario": "main", "ok": True, "checks": checks}))
        return 0
    except Exception as e:
        return _child_fail(checks, "main", repr(e))
    finally:
        if daemon is not None:
            _stop_daemon(daemon)


def _child_no_default_scenario() -> int:
    checks: dict = {}
    daemon = None
    try:
        _child_preflight(checks)
        records, _armed = _install_call_spy()
        # No --unlock: a fresh HOME has no default collection. If this
        # gnome-keyring build creates one eagerly, remove the alias so
        # the no-default state is what gets exercised either way.
        daemon = _start_daemon(unlock=False)
        _wait_service_owned()

        from contextlib import closing

        import secretstorage
        import secretstorage.exceptions
        from secretstorage.defines import SS_PATH, SS_PREFIX
        from secretstorage.util import DBusAddressWrapper

        with closing(secretstorage.dbus_init()) as conn:
            try:
                secretstorage.Collection(conn)
            except secretstorage.exceptions.ItemNotFoundException:
                checks["default_absent"] = True
            else:
                service = DBusAddressWrapper(SS_PATH, SS_PREFIX + "Service", conn)
                service.call("SetAlias", "so", "default", "/")
                try:
                    secretstorage.Collection(conn)
                    checks["default_absent"] = False
                except secretstorage.exceptions.ItemNotFoundException:
                    checks["default_absent"] = True

        from mind_meld import crypto
        from mind_meld.errors import KeyringUnavailableError

        try:
            crypto.get_passphrase(non_interactive=True)
            checks["no_default_raises"] = False
        except KeyringUnavailableError as e:
            checks["no_default_raises"] = e.reason == "no-default-collection"
        checks["no_prompt_methods"] = not any(m in _PROMPT_METHODS for m in records)

        print(json.dumps({"scenario": "no-default", "ok": True, "checks": checks}))
        return 0
    except Exception as e:
        return _child_fail(checks, "no-default", repr(e))
    finally:
        if daemon is not None:
            _stop_daemon(daemon)


def _child_entry(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "child":
        print("usage: test_secret_service_live.py child <scenario>", file=sys.stderr)
        return 2
    if argv[2] == "main":
        return _child_main_scenario()
    if argv[2] == "no-default":
        return _child_no_default_scenario()
    print(f"unknown scenario {argv[2]!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_child_entry(sys.argv))
