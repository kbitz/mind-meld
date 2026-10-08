"""Tests for mind_meld.lockfile — fcntl.flock-based locking.

Cross-process tests use subprocess.Popen + ready-file handshake to
avoid timing-dependent sleeps. The child imports lockfile INSIDE the
spawned interpreter so it never inherits the parent's _LOCK_FDS state.
"""

from __future__ import annotations

import errno
import fcntl
import io
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import typer
from rich.console import Console

from mind_meld import cli, lockfile
from mind_meld.errors import LockError
from mind_meld.lockfile import acquire_lock, release_lock

_WAIT = (
    "Wait for it to finish, then run this command again. If that operation is "
    "waiting for input in another terminal, answer it there; if its output says "
    "it is updating mm, let it finish, which can take several minutes. Do not "
    "delete the mm lockfile or kill the process; the lock is released when it exits."
)


def _pid_message(pid: int) -> str:
    return (
        f"Another mm operation is running (PID {pid}). {_WAIT} "
        f"To see the process, run:\n  ps -p {pid} -o etime=,tty=,command="
    )


def _unknown_message() -> str:
    return f"Another mm operation is running (PID unknown). {_WAIT}"


# Repo root for subprocess children to import from.
_REPO_SRC = str(Path(__file__).parent.parent / "src")


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll predicate with short intervals. Handshake primitive."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestLockfileBasics:
    def test_acquire_and_release(self, tmp_path):
        lock_path = tmp_path / "mind-meld.lock"
        acquire_lock(lock_path)
        assert lock_path.exists()
        assert lock_path.read_text().strip() == str(os.getpid())
        release_lock(lock_path)
        # The lockfile body remains on disk after release (diagnostic only).
        # Unlinking on release creates an advisory-lock race — see
        # lockfile.release_lock docstring. The stale PID is harmless; the
        # next acquire truncates before writing.
        assert lock_path.exists()

    def test_release_without_acquire_is_noop(self, tmp_path):
        lock_path = tmp_path / "mind-meld.lock"
        # No crash expected.
        release_lock(lock_path)

    def test_release_is_idempotent(self, tmp_path):
        lock_path = tmp_path / "mind-meld.lock"
        acquire_lock(lock_path)
        release_lock(lock_path)
        release_lock(lock_path)  # second call is a no-op


class TestSameProcessSemantics:
    def test_acquire_twice_same_process_raises(self, tmp_path):
        lock_path = tmp_path / "mind-meld.lock"
        acquire_lock(lock_path)
        try:
            with pytest.raises(LockError, match="already holds"):
                acquire_lock(lock_path)
        finally:
            release_lock(lock_path)

    def test_resolved_path_aliasing_symlink(self, tmp_path):
        """Same lockfile via symlink must collide on the same key."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)

        via_real = real_dir / "mind-meld.lock"
        via_link = link_dir / "mind-meld.lock"

        acquire_lock(via_real)
        try:
            # Same physical file via the symlinked directory must be
            # recognized as the same lock.
            with pytest.raises(LockError, match="already holds"):
                acquire_lock(via_link)
        finally:
            release_lock(via_real)

    def test_different_paths_different_locks(self, tmp_path):
        """Two distinct lockfiles can be held concurrently."""
        lock_a = tmp_path / "a.lock"
        lock_b = tmp_path / "b.lock"

        acquire_lock(lock_a)
        try:
            acquire_lock(lock_b)  # must succeed
            release_lock(lock_b)
        finally:
            release_lock(lock_a)


class TestCrossProcess:
    def _child_script(
        self, lock_path: Path, ready_marker: Path, behavior: str = "wait_then_exit"
    ) -> str:
        """Build a child script that imports lockfile fresh."""
        return textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {_REPO_SRC!r})
            from pathlib import Path
            from mind_meld.lockfile import acquire_lock, release_lock

            lp = Path({str(lock_path)!r})
            acquire_lock(lp)
            Path({str(ready_marker)!r}).write_text("ready")
            if {behavior!r} == "wait_then_exit":
                sys.stdin.read()  # blocks until parent closes stdin
                release_lock(lp)
            elif {behavior!r} == "block_forever":
                while True:
                    time.sleep(60)
        """).strip()

    def test_child_holds_lock_blocks_parent(self, tmp_path):
        lock_path = tmp_path / "mind-meld.lock"
        ready_marker = tmp_path / "ready"
        script = self._child_script(lock_path, ready_marker)

        child = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for(lambda: ready_marker.exists()), "child never signaled ready"
            # Parent cannot acquire while child holds it.
            with pytest.raises(LockError, match="Another mm operation"):
                acquire_lock(lock_path)
        finally:
            if child.stdin:
                child.stdin.close()
            child.wait(timeout=5)

    def test_child_dies_parent_can_acquire(self, tmp_path):
        """flock auto-release on process exit: after SIGKILL, parent acquires."""
        lock_path = tmp_path / "mind-meld.lock"
        ready_marker = tmp_path / "ready"
        script = self._child_script(lock_path, ready_marker, "block_forever")

        child = subprocess.Popen([sys.executable, "-c", script])
        try:
            assert _wait_for(lambda: ready_marker.exists())
            # Confirm child holds it.
            with pytest.raises(LockError):
                acquire_lock(lock_path)
            # Kill child.
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

        # Kernel released the flock on process death.
        acquire_lock(lock_path)
        try:
            assert lock_path.read_text().strip() == str(os.getpid())
        finally:
            release_lock(lock_path)

    def test_blocked_parent_sees_child_pid(self, tmp_path):
        """The LockError message should include the holding process's PID."""
        lock_path = tmp_path / "mind-meld.lock"
        ready_marker = tmp_path / "ready"
        script = self._child_script(lock_path, ready_marker)

        child = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
        )
        try:
            assert _wait_for(lambda: ready_marker.exists())
            with pytest.raises(LockError) as exc_info:
                acquire_lock(lock_path)
            assert f"PID {child.pid}" in str(exc_info.value)
        finally:
            if child.stdin:
                child.stdin.close()
            child.wait(timeout=5)


class TestFailurePaths:
    def test_flock_eintr_retries_once_then_succeeds(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "mind-meld.lock"
        call_count = {"n": 0}
        real_flock = fcntl.flock

        def flaky_flock(fd, op):
            call_count["n"] += 1
            if call_count["n"] == 1 and (op & fcntl.LOCK_EX):
                raise InterruptedError("EINTR simulated")
            return real_flock(fd, op)

        monkeypatch.setattr(lockfile.fcntl, "flock", flaky_flock)
        acquire_lock(lock_path)
        try:
            assert call_count["n"] >= 2  # first EINTR + second success
        finally:
            release_lock(lock_path)

    def test_flock_eintr_twice_fails(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "mind-meld.lock"

        def always_eintr(fd, op):
            raise InterruptedError("EINTR always")

        monkeypatch.setattr(lockfile.fcntl, "flock", always_eintr)
        with pytest.raises(LockError, match="interrupted"):
            acquire_lock(lock_path)

    def test_pid_write_failure_releases_flock(self, tmp_path, monkeypatch):
        """If PID write fails after flock, the lock must be released cleanly."""
        lock_path = tmp_path / "mind-meld.lock"
        real_write = os.write

        def bad_write(fd, data):
            raise OSError(errno.EIO, "simulated write failure")

        monkeypatch.setattr(lockfile.os, "write", bad_write)
        with pytest.raises(LockError, match="PID write failed"):
            acquire_lock(lock_path)

        # After the failure, a subsequent acquire must succeed — the
        # failed attempt released the lock.
        monkeypatch.setattr(lockfile.os, "write", real_write)
        acquire_lock(lock_path)
        try:
            assert lock_path.read_text().strip() == str(os.getpid())
        finally:
            release_lock(lock_path)

    def test_open_failure_raises_lock_error(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "mind-meld.lock"

        def bad_open(path, flags, mode=0o777):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(lockfile.os, "open", bad_open)
        with pytest.raises(LockError, match="Could not open"):
            acquire_lock(lock_path)


class TestStatePostRelease:
    def test_release_clears_lock_fds_entry(self, tmp_path):
        """After release, _LOCK_FDS must not retain the entry."""
        lock_path = tmp_path / "mind-meld.lock"
        acquire_lock(lock_path)
        key = lockfile._resolve_key(lock_path)
        assert key in lockfile._LOCK_FDS
        release_lock(lock_path)
        assert key not in lockfile._LOCK_FDS

    def test_release_does_not_unlink_lockfile(self, tmp_path):
        """CRITICAL regression: unlinking on release creates the classic
        advisory-lock race where a post-release opener flocks the live
        inode and a later opener creates+flocks a fresh inode — two
        exclusive locks on the same path via different inodes.

        See release_lock() docstring for the full failure sequence.
        """
        lock_path = tmp_path / "mind-meld.lock"
        acquire_lock(lock_path)
        assert lock_path.exists()
        release_lock(lock_path)
        assert lock_path.exists(), "Lockfile must NOT be unlinked on release — creates inode race"

    def test_reacquire_after_release_works(self, tmp_path):
        """Re-acquiring after release must succeed and rewrite PID."""
        lock_path = tmp_path / "mind-meld.lock"
        acquire_lock(lock_path)
        release_lock(lock_path)
        # Second acquire succeeds, PID body is updated cleanly.
        acquire_lock(lock_path)
        try:
            assert lock_path.read_text().strip() == str(os.getpid())
        finally:
            release_lock(lock_path)


class TestContentionGuidance:
    def test_held_lock_names_the_child_and_keeps_the_inode(self, tmp_path):
        lock_path = tmp_path / "mind-meld.lock"
        ready_marker = tmp_path / "ready"
        script = TestCrossProcess()._child_script(lock_path, ready_marker)
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for(lambda: ready_marker.exists()), "child never signaled ready"
            inode = os.stat(lock_path).st_ino
            with pytest.raises(LockError) as exc_info:
                acquire_lock(lock_path)
            assert str(exc_info.value) == _pid_message(child.pid)
            assert lock_path.exists()
            assert os.stat(lock_path).st_ino == inode
            with pytest.raises(LockError):
                acquire_lock(lock_path)
        finally:
            if child.stdin:
                child.stdin.close()
            child.wait(timeout=5)

        acquire_lock(lock_path)
        try:
            assert os.stat(lock_path).st_ino == inode
        finally:
            release_lock(lock_path)

    @pytest.mark.parametrize("body", ["", "0", "-1", "abc"])
    def test_unusable_body_says_pid_unknown(self, tmp_path, body):
        lock_path = tmp_path / "mind-meld.lock"
        script = textwrap.dedent(
            """
            import fcntl, sys
            path, body = sys.argv[1], sys.argv[2]
            handle = open(path, "w")
            handle.write(body)
            handle.flush()
            fcntl.flock(handle, fcntl.LOCK_EX)
            print("locked", flush=True)
            sys.stdin.read()
            """
        ).strip()
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path), body],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "locked"
            with pytest.raises(LockError) as exc_info:
                acquire_lock(lock_path)
            assert str(exc_info.value) == _unknown_message()
        finally:
            if child.stdin:
                child.stdin.close()
            child.wait(timeout=5)

    @pytest.mark.parametrize("width", [80, 60])
    def test_error_render_keeps_the_ps_line(self, monkeypatch, width):
        pid = 424242
        text = _pid_message(pid)
        buf = io.StringIO()
        monkeypatch.setattr(cli, "stderr_console", Console(file=buf, width=width))
        with pytest.raises(typer.Exit):
            cli._error(text)
        wanted = f"ps -p {pid} -o etime=,tty=,command="
        assert [line.strip() for line in buf.getvalue().splitlines()].count(wanted) == 1
