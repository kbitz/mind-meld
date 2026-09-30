"""Tests for self-update: `mm update` and the automatic install at the tail of
pull / push / autopull / autopush.

Covers:
  - detect_install: every install shape, including unreadable pipx metadata.
  - run_update: outcome read back from pipx metadata, never from the process.
  - The attempt gate: one automatic attempt per release per day, atomic claim.
  - update_or_nudge: attended foreground vs detached hook, and every reason it
    falls back to the nudge.
  - `mm update`: refusals, the pinned-install reinstall, exit codes.
  - The four CLI seams pass the right `attended` flag; previews never call it.

No test here can reach a real pipx: `_run_pipx` / `_spawn_pipx` refuse under
pytest, and the tests replace them wholesale.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead

import pytest
from typer.testing import CliRunner

from mind_meld import config as config_module
from mind_meld import lockedjson, lockfile, upgrade
from mind_meld.cli import app
from tests.conftest import _setup_real_config

runner = CliRunner()

PIPX = "/fake/bin/pipx"
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_upgrade_flags():
    upgrade._reset_for_tests()
    yield
    upgrade._reset_for_tests()


def _set_version(monkeypatch, version: str) -> None:
    monkeypatch.setattr("mind_meld.__version__", version)
    monkeypatch.setattr("mind_meld.upgrade.__version__", version)


def _stub_tags(monkeypatch, tag_names: list[str]) -> None:
    monkeypatch.setattr(upgrade, "_fetch_tags", lambda *a: [{"name": n} for n in tag_names])


def _write_metadata(venv, *, spec, version, package="mind-meld", pinned=False, suffix="") -> None:
    venv.mkdir(parents=True, exist_ok=True)
    (venv / upgrade.PIPX_METADATA_NAME).write_text(
        json.dumps(
            {
                "main_package": {
                    "package": package,
                    "package_or_url": spec,
                    "package_version": version,
                    "pinned": pinned,
                    "suffix": suffix,
                },
                "pipx_metadata_version": "0.12",
            }
        )
    )


@pytest.fixture
def pipx_install(monkeypatch, tmp_path):
    """A fake pipx venv for the running mm, one release behind the newest tag."""

    def make(*, spec=upgrade.INSTALL_SPEC, version="1.2.0", latest="1.3.0", **meta):
        venv = tmp_path / "pipx" / "venvs" / "mind-meld"
        _write_metadata(venv, spec=spec, version=version, **meta)
        monkeypatch.setattr(upgrade, "_install_prefix", lambda: venv)
        monkeypatch.setattr(upgrade, "find_pipx", lambda: PIPX)
        _set_version(monkeypatch, version)
        _stub_tags(monkeypatch, [f"v{latest}"])
        return venv

    return make


def _fake_run(monkeypatch, venv, *, lands=None, spec=upgrade.INSTALL_SPEC, returncode=0, output=""):
    """Replace foreground pipx. `lands` is the version pipx records on success."""
    calls = []

    def run(argv):
        calls.append(list(argv))
        if lands is not None and returncode == 0:
            _write_metadata(venv, spec=spec, version=lands)
        return subprocess.CompletedProcess(argv, returncode, stdout=output)

    monkeypatch.setattr(upgrade, "_run_pipx", run)
    return calls


def _fake_spawn(monkeypatch, *, raises=None):
    calls = []

    def spawn(argv, log_path):
        if raises is not None:
            raise raises
        calls.append((list(argv), log_path))

    monkeypatch.setattr(upgrade, "_spawn_pipx", spawn)
    return calls


def _cache() -> dict:
    return json.loads(upgrade.CACHE_PATH.read_text())


# ── detect_install ────────────────────────────────────────────────────────


class TestDetectInstall:
    @pytest.mark.parametrize(
        "spec, meta, kind",
        [
            (upgrade.INSTALL_SPEC, {}, "tracking"),
            (f"{upgrade.REPO_SPEC}@v1.1.0", {}, "pinned"),
            (upgrade.REPO_SPEC, {}, "pinned"),
            (upgrade.INSTALL_SPEC, {"pinned": True}, "pipx-pinned"),
            ("git+https://github.com/someone/mind-meld.git@latest", {}, "foreign"),
            ("/Users/kb/dev/mind-meld", {}, "foreign"),
            # A lookalike ref must not read as the release branch.
            (f"{upgrade.REPO_SPEC}.evil@latest", {}, "foreign"),
            # mm injected into another package's venv is not ours to upgrade.
            (upgrade.INSTALL_SPEC, {"package": "other-tool"}, "foreign"),
        ],
    )
    def test_classifies_pipx_installs(self, pipx_install, spec, meta, kind):
        pipx_install(spec=spec, **meta)
        info = upgrade.detect_install()
        assert info.kind == kind
        assert info.venv_name == "mind-meld"
        assert info.version == "1.2.0"

    def test_no_metadata_is_not_pipx(self, monkeypatch, tmp_path):
        _set_version(monkeypatch, "1.2.0")
        monkeypatch.setattr(upgrade, "_install_prefix", lambda: tmp_path)
        assert upgrade.detect_install() == upgrade.InstallInfo("not-pipx")

    def test_dev_build_never_reads_metadata(self, monkeypatch):
        _set_version(monkeypatch, upgrade.DEV_BUILD_SENTINEL)
        monkeypatch.setattr(upgrade, "_install_prefix", lambda: pytest.fail("read prefix"))
        assert upgrade.detect_install().kind == "dev"

    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            "[]",
            "{}",
            '{"main_package": null}',
            '{"main_package": {"package": "mind-meld"}}',
            '{"main_package": {"package": "mm", "package_or_url": 3, "package_version": ""}}',
        ],
    )
    def test_unreadable_metadata_is_left_alone(self, monkeypatch, tmp_path, body):
        _set_version(monkeypatch, "1.2.0")
        (tmp_path / upgrade.PIPX_METADATA_NAME).write_text(body)
        monkeypatch.setattr(upgrade, "_install_prefix", lambda: tmp_path)
        assert upgrade.detect_install().kind == "foreign"

    def test_real_test_environment_is_not_a_pipx_install(self):
        """The belt under every other guard: the suite's own interpreter must
        never look updatable, so no un-stubbed test can reach pipx."""
        assert upgrade.detect_install().kind in ("not-pipx", "dev")

    @pytest.mark.parametrize("suffix", [None, 3, "-other"])
    def test_mismatched_or_invalid_suffix_is_left_alone(self, pipx_install, suffix):
        pipx_install(suffix=suffix)
        assert upgrade.detect_install().kind == "foreign"


class TestFindPipx:
    def test_path_wins(self, monkeypatch):
        monkeypatch.setattr(upgrade.shutil, "which", lambda name: "/on/path/pipx")
        assert upgrade.find_pipx() == "/on/path/pipx"

    def test_falls_back_to_known_dirs(self, monkeypatch, tmp_path):
        fallback = tmp_path / "pipx"
        fallback.write_text("#!/bin/sh\n")
        fallback.chmod(0o755)
        plain = tmp_path / "not-executable"
        plain.write_text("")
        plain.chmod(0o644)
        monkeypatch.setattr(upgrade.shutil, "which", lambda name: None)
        monkeypatch.setattr(upgrade, "_PIPX_FALLBACK_PATHS", (str(plain), str(fallback)))
        assert upgrade.find_pipx() == str(fallback)

    def test_none_when_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(upgrade.shutil, "which", lambda name: None)
        monkeypatch.setattr(upgrade, "_PIPX_FALLBACK_PATHS", (str(tmp_path / "missing"),))
        assert upgrade.find_pipx() is None


class TestUpdateArgv:
    def test_recovery_command_targets_the_running_home(self, pipx_install, monkeypatch, tmp_path):
        pipx_install()
        venv = tmp_path / "pipx home" / "venvs" / "mind-meld-rollback"
        _write_metadata(
            venv, spec=f"{upgrade.REPO_SPEC}@v1.2.0", version="1.2.0", suffix="-rollback"
        )
        monkeypatch.setattr(upgrade, "_install_prefix", lambda: venv)
        monkeypatch.setenv("PIPX_HOME", str(tmp_path / "other-pipx"))
        argv = shlex.split(upgrade.reinstall_cmd(upgrade.detect_install()))
        assert argv[0] == f"PIPX_HOME={venv.parent.parent}"
        assert argv[1:] == [
            "pipx",
            "install",
            "--force",
            upgrade.INSTALL_SPEC,
            "--suffix=-rollback",
        ]

    def test_tracking_upgrades_in_place(self):
        info = upgrade.InstallInfo("tracking", "mind-meld", upgrade.INSTALL_SPEC, "1.2.0")
        assert upgrade.update_argv(PIPX, info) == [PIPX, "upgrade", "mind-meld"]

    def test_pinned_reinstalls_onto_the_release_branch(self):
        info = upgrade.InstallInfo("pinned", "mind-meld", f"{upgrade.REPO_SPEC}@v1.1.0", "1.1.0")
        argv = upgrade.update_argv(PIPX, info)
        assert argv == [PIPX, "install", "--force", upgrade.INSTALL_SPEC]
        # The nudge and README quote INSTALL_CMD; the two must stay one command.
        assert " ".join(["pipx", *argv[1:]]) == upgrade.INSTALL_CMD


# ── the pipx seams ────────────────────────────────────────────────────────


class TestPipxSeams:
    def test_cleanup_stops_a_child_after_its_parent_and_pipes_exit(
        self, pipx_install, monkeypatch, tmp_path
    ):
        pipx_install()
        monkeypatch.setattr(upgrade, "_refuse_under_pytest", lambda: None)
        monkeypatch.setattr(upgrade, "PIPX_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(upgrade, "PIPX_STOP_GRACE_SECONDS", 0.2)
        ready = tmp_path / "ready"
        child_code = (
            "import signal, sys, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "Path(sys.argv[1]).touch()\n"
            "time.sleep(30)\n"
        )
        code = (
            "import signal, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGINT, lambda signum, frame: sys.exit(0))\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}, sys.argv[1]], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "while not Path(sys.argv[1]).exists(): time.sleep(0.01)\n"
            "print(child.pid, flush=True)\n"
            "child.wait()\n"
        )
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            upgrade._run_pipx([sys.executable, "-c", code, str(ready)])
        child_pid = int(caught.value.output.splitlines()[0])
        try:
            state = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True
            ).stdout.strip()
            assert not state or state.startswith("Z"), f"installer child still running: {state}"
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @pytest.mark.parametrize("owns_destination", [False, True])
    def test_forced_reinstall_refuses_another_homes_executable(
        self, pipx_install, monkeypatch, tmp_path, owns_destination
    ):
        venv = pipx_install(spec=f"{upgrade.REPO_SPEC}@v1.2.0")
        own_app = venv / "bin" / "mm"
        own_app.parent.mkdir()
        own_app.write_text("our installed executable")
        foreign_app = tmp_path / "other-pipx" / "venvs" / "mind-meld" / "bin" / "mm"
        foreign_app.parent.mkdir(parents=True)
        foreign_app.write_text("other installed executable")
        bin_dir = tmp_path / "other-bin"
        bin_dir.mkdir()
        destination = bin_dir / "mm"
        destination.symlink_to(own_app if owns_destination else foreign_app)
        monkeypatch.setenv("PIPX_HOME", str(tmp_path / "other-pipx"))
        monkeypatch.setenv("PIPX_BIN_DIR", str(bin_dir))
        argv = upgrade.update_argv(PIPX, upgrade.detect_install())
        if owns_destination:
            assert upgrade._pipx_environment(argv)["PIPX_HOME"] == str(venv.parent.parent)
        else:
            with pytest.raises(OSError, match="belongs to another install"):
                upgrade._pipx_environment(argv)
        assert destination.resolve() == (own_app if owns_destination else foreign_app)
        assert foreign_app.read_text() == "other installed executable"

    def test_timeout_allows_graceful_installer_cleanup(self, pipx_install, monkeypatch):
        pipx_install()
        monkeypatch.setattr(upgrade, "_refuse_under_pytest", lambda: None)
        monkeypatch.setattr(upgrade, "PIPX_TIMEOUT_SECONDS", 1)
        code = (
            "import signal, time\n"
            "def stop(signum, frame):\n"
            "    print('cleanly stopped', flush=True)\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGINT, stop)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            upgrade._run_pipx([sys.executable, "-c", code])
        assert "cleanly stopped" in caught.value.output

    def test_real_run_refuses_under_pytest(self):
        with pytest.raises(OSError, match="under pytest"):
            upgrade._run_pipx(["pipx", "--version"])

    def test_real_spawn_refuses_under_pytest_before_touching_the_log(self, tmp_path):
        log = tmp_path / "auto-update.log"
        with pytest.raises(OSError, match="under pytest"):
            upgrade._spawn_pipx(["pipx", "--version"], log)
        assert not log.exists()

    @pytest.mark.parametrize("detached", [False, True])
    def test_targets_the_running_pipx_home(self, pipx_install, monkeypatch, tmp_path, detached):
        venv = pipx_install()
        monkeypatch.setenv("PIPX_HOME", str(tmp_path / "other-pipx"))
        # Run only a synthetic Python child, never pipx.
        monkeypatch.setattr(upgrade, "_refuse_under_pytest", lambda: None)
        argv = [sys.executable, "-c", "import os\nprint(os.environ['PIPX_HOME'])"]
        expected = str(venv.parent.parent)
        if not detached:
            assert upgrade._run_pipx(argv).stdout.strip() == expected
            return
        log = tmp_path / "child.log"
        upgrade._spawn_pipx(argv, log)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.read_text().splitlines()[-1] == expected:
                break
            time.sleep(0.05)
        assert log.read_text().splitlines()[-1] == expected

    def test_timeout_stops_installer_descendants(self, pipx_install, monkeypatch):
        pipx_install()
        monkeypatch.setattr(upgrade, "_refuse_under_pytest", lambda: None)
        monkeypatch.setattr(upgrade, "PIPX_TIMEOUT_SECONDS", 2)
        code = (
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time\\ntime.sleep(30)'])\n"
            "print(child.pid, flush=True)\n"
            "child.wait()\n"
        )
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            upgrade._run_pipx([sys.executable, "-c", code])
        output = caught.value.output
        if isinstance(output, bytes):
            output = output.decode()
        child_pid = int(output.splitlines()[0])
        try:
            state = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True
            ).stdout.strip()
            assert not state or state.startswith("Z"), f"installer child still running: {state}"
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class TestRunUpdate:
    @pytest.mark.parametrize("body", ["not json", '{"main_package": {}}'])
    def test_success_without_readable_metadata_is_incomplete(self, pipx_install, monkeypatch, body):
        venv = pipx_install()
        install = upgrade.detect_install()

        def run(argv):
            (venv / upgrade.PIPX_METADATA_NAME).write_text(body)
            return subprocess.CompletedProcess(argv, 0, stdout="done\n")

        monkeypatch.setattr(upgrade, "_run_pipx", run)
        outcome = upgrade.run_update(install, PIPX)
        assert outcome.status == "failed"
        assert "could not verify" in outcome.detail

    def test_updated_reads_the_new_version_from_metadata(self, pipx_install, monkeypatch):
        venv = pipx_install()
        calls = _fake_run(monkeypatch, venv, lands="1.3.0", output="upgraded package\n")
        outcome = upgrade.run_update(upgrade.detect_install(), PIPX)
        assert calls == [[PIPX, "upgrade", "mind-meld"]]
        assert (outcome.status, outcome.old, outcome.new) == ("updated", "1.2.0", "1.3.0")
        assert outcome.now_tracking is False

    def test_unchanged_when_pipx_lands_nothing(self, pipx_install, monkeypatch):
        venv = pipx_install()
        _fake_run(monkeypatch, venv, output="already at latest version\n")
        outcome = upgrade.run_update(upgrade.detect_install(), PIPX)
        assert (outcome.status, outcome.new) == ("unchanged", "1.2.0")

    def test_failed_exit_carries_pipxs_last_line(self, pipx_install, monkeypatch):
        venv = pipx_install()
        _fake_run(
            monkeypatch, venv, lands="1.3.0", returncode=1, output="cloning\n\nfatal: no route\n"
        )
        outcome = upgrade.run_update(upgrade.detect_install(), PIPX)
        assert (outcome.status, outcome.detail) == ("failed", "fatal: no route")
        assert outcome.new is None

    def test_silent_failure_names_the_exit_code(self, pipx_install, monkeypatch):
        venv = pipx_install()
        _fake_run(monkeypatch, venv, returncode=2)
        assert upgrade.run_update(upgrade.detect_install(), PIPX).detail == "pipx exited 2"

    @pytest.mark.parametrize(
        "error, detail",
        [
            (subprocess.TimeoutExpired(["pipx"], 600), "did not finish within"),
            (FileNotFoundError("pipx vanished"), "could not run pipx"),
        ],
    )
    def test_timeout_and_missing_binary_do_not_raise(
        self, pipx_install, monkeypatch, error, detail
    ):
        pipx_install()

        def run(argv):
            raise error

        monkeypatch.setattr(upgrade, "_run_pipx", run)
        outcome = upgrade.run_update(upgrade.detect_install(), PIPX)
        assert outcome.status == "failed"
        assert detail in outcome.detail

    def test_reinstall_at_the_same_version_still_counts_when_it_starts_tracking(
        self, pipx_install, monkeypatch
    ):
        venv = pipx_install(spec=f"{upgrade.REPO_SPEC}@v1.2.0")
        _fake_run(monkeypatch, venv, lands="1.2.0")
        outcome = upgrade.run_update(upgrade.detect_install(), PIPX)
        assert (outcome.status, outcome.now_tracking) == ("updated", True)


# ── config ────────────────────────────────────────────────────────────────


class TestAutoInstallSetting:
    @pytest.mark.parametrize(
        "config, enabled",
        [
            (None, True),
            ({}, True),
            ({"upgrade": {}}, True),
            ({"upgrade": {"auto_install": True}}, True),
            ({"upgrade": {"auto_install": False}}, False),
            ({"upgrade": {"auto_install": "false"}}, False),
            ({"upgrade": {"auto_install": 1}}, False),
            ({"upgrade": "broken"}, False),
        ],
    )
    def test_only_a_literal_true_or_absence_enables(self, config, enabled):
        assert upgrade.auto_install_enabled(config) is enabled

    @pytest.mark.parametrize(
        "written, loaded",
        [(None, True), ("true", True), ("false", False), ('"false"', False), ("0", False)],
    )
    def test_load_config_defaults_on_and_fails_closed(self, tmp_path, written, loaded):
        path = tmp_path / "config.toml"
        body = (
            '[device]\nid = "dev-a"\nname = "Mac A"\n'
            f'[storage]\npath = "{tmp_path / "storage"}"\n'
            '[[sync.sources]]\nname = "claude"\ntype = "claude"\n'
            f'path = "{tmp_path / "claude"}"\n'
        )
        if written is not None:
            body += f"[upgrade]\nauto_install = {written}\n"
        path.write_text(body)
        config = config_module.load_config(path)
        assert config["upgrade"]["auto_install"] is loaded
        assert config["upgrade"]["auto_check"] is True


# ── the attempt gate ──────────────────────────────────────────────────────


class TestAttemptGate:
    def test_simultaneous_claims_start_only_one_attempt(self, tmp_path):
        start = tmp_path / "start"
        ready = [tmp_path / f"ready-{index}" for index in range(2)]
        code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from datetime import datetime\n"
            "from mind_meld import upgrade\n"
            "upgrade.CACHE_PATH = Path(sys.argv[1])\n"
            "Path(sys.argv[3]).touch()\n"
            "deadline = time.monotonic() + 10\n"
            "while not Path(sys.argv[2]).exists():\n"
            "    if time.monotonic() > deadline: raise RuntimeError('start barrier timed out')\n"
            "    time.sleep(0.01)\n"
            "now = datetime.fromisoformat(sys.argv[4])\n"
            "print(upgrade._claim_install_attempt('1.3.0', now=now))\n"
        )
        children = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(upgrade.CACHE_PATH),
                    str(start),
                    str(path),
                    NOW.isoformat(),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for path in ready
        ]
        try:
            deadline = time.monotonic() + 10
            while not all(path.exists() for path in ready) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert all(path.exists() for path in ready)
            start.touch()
            outcomes = []
            for child in children:
                output, error = child.communicate(timeout=10)
                assert child.returncode == 0, error
                outcomes.append(output.strip())
            assert sorted(outcomes) == ["claimed", "in-flight"]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    @pytest.mark.parametrize("attended", [False, True])
    def test_failed_claim_write_never_runs_pipx(self, pipx_install, monkeypatch, attended):
        venv = pipx_install()
        runs = _fake_run(monkeypatch, venv, lands="1.3.0")
        spawns = _fake_spawn(monkeypatch)
        result = upgrade.UpgradeCheckResult(
            "upgrade-available", "1.2.0", "1.3.0", upgrade.INSTALL_CMD
        )
        monkeypatch.setattr(upgrade, "check_for_upgrade", lambda config: result)
        monkeypatch.setattr(
            lockedjson, "_write_json", lambda *a, **kw: OSError("claim persistence failed")
        )
        assert upgrade._claim_install_attempt("1.3.0", now=NOW) == "unavailable"
        upgrade.update_or_nudge({}, attended=attended)
        assert runs == [] and spawns == []
        assert upgrade.CACHE_PATH.read_text() == ""

    def test_first_claim_wins_and_stamps_the_cache(self):
        assert upgrade._claim_install_attempt("1.3.0", now=NOW) == "claimed"
        cache = _cache()
        assert cache["install_attempt_version"] == "1.3.0"
        assert cache["install_attempt_at"] == NOW.isoformat()
        assert cache["install_attempt_outcome"] is None

    def test_second_claim_inside_the_grace_is_in_flight(self):
        upgrade._claim_install_attempt("1.3.0", now=NOW)
        later = NOW + upgrade.INSTALL_GRACE - timedelta(seconds=1)
        assert upgrade._claim_install_attempt("1.3.0", now=later) == "in-flight"
        assert _cache()["install_attempt_at"] == NOW.isoformat()

    def test_still_behind_after_the_grace_is_failed_until_the_retry_gap(self):
        upgrade._claim_install_attempt("1.3.0", now=NOW)
        assert upgrade._claim_install_attempt("1.3.0", now=NOW + upgrade.INSTALL_GRACE) == "failed"
        almost = NOW + upgrade.DEFAULT_INSTALL_RETRY_GAP - timedelta(seconds=1)
        assert upgrade._claim_install_attempt("1.3.0", now=almost) == "failed"
        due = NOW + upgrade.DEFAULT_INSTALL_RETRY_GAP
        assert upgrade._claim_install_attempt("1.3.0", now=due) == "claimed"
        assert _cache()["install_attempt_at"] == due.isoformat()

    def test_recorded_failure_does_not_wait_out_the_grace(self):
        upgrade._claim_install_attempt("1.3.0", now=NOW)
        upgrade._record_install_failed("1.3.0")
        assert upgrade._claim_install_attempt("1.3.0", now=NOW + timedelta(seconds=5)) == "failed"

    def test_a_newer_release_resets_the_gate(self):
        upgrade._claim_install_attempt("1.3.0", now=NOW)
        upgrade._record_install_failed("1.3.0")
        assert upgrade._claim_install_attempt("1.3.1", now=NOW + timedelta(minutes=1)) == "claimed"
        assert _cache()["install_attempt_outcome"] is None

    def test_failure_for_another_release_is_not_recorded(self):
        upgrade._claim_install_attempt("1.3.1", now=NOW)
        upgrade._record_install_failed("1.3.0")
        assert _cache()["install_attempt_outcome"] is None

    def test_a_clock_that_moved_back_cannot_wedge_the_gate(self):
        upgrade._claim_install_attempt("1.3.0", now=NOW + timedelta(days=30))
        assert upgrade._claim_install_attempt("1.3.0", now=NOW) == "claimed"

    def test_claim_preserves_the_rest_of_the_cache(self):
        upgrade.CACHE_PATH.write_text(
            json.dumps({"latest_version": "1.3.0", "last_seen_self_version": "1.2.0"})
        )
        upgrade._claim_install_attempt("1.3.0", now=NOW)
        cache = _cache()
        assert cache["latest_version"] == "1.3.0"
        assert cache["last_seen_self_version"] == "1.2.0"

    def test_unwritable_cache_is_unclaimed(self, monkeypatch):
        def refuse(*a, **kw):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(upgrade, "locked_json_rmw", refuse)
        assert upgrade._claim_install_attempt("1.3.0", now=NOW) == "unavailable"

    def test_cached_view_reports_the_attempt(self, monkeypatch):
        _set_version(monkeypatch, "1.2.0")
        base = {"latest_version": "1.3.0", "checked_at": NOW.isoformat()}
        upgrade.CACHE_PATH.write_text(json.dumps(base))
        assert upgrade.cached_upgrade_view({}, now=NOW).install_attempt is None
        attempt = {"install_attempt_version": "1.3.0", "install_attempt_at": NOW.isoformat()}
        upgrade.CACHE_PATH.write_text(json.dumps(base | attempt))
        view = upgrade.cached_upgrade_view({}, now=NOW + timedelta(minutes=1))
        assert view.install_attempt == "in-flight"
        view = upgrade.cached_upgrade_view({}, now=NOW + timedelta(hours=30))
        assert view.install_attempt == "failed"
        # An attempt at an older release says nothing about this one.
        upgrade.CACHE_PATH.write_text(json.dumps(base | attempt | {"latest_version": "1.4.0"}))
        assert upgrade.cached_upgrade_view({}, now=NOW).install_attempt is None


# ── update_or_nudge ───────────────────────────────────────────────────────


class TestAttendedAutoUpdate:
    @pytest.mark.parametrize("attended", [False, True])
    def test_already_advanced_metadata_spends_no_attempt(self, pipx_install, monkeypatch, attended):
        venv = pipx_install(version="1.3.0")
        _set_version(monkeypatch, "1.2.0")
        runs = _fake_run(monkeypatch, venv)
        spawns = _fake_spawn(monkeypatch)
        upgrade.update_or_nudge({}, attended=attended)
        assert runs == [] and spawns == []
        assert _cache()["install_attempt_version"] is None

    def test_broken_nudge_cannot_escape_the_sync_tail(self, pipx_install, monkeypatch):
        pipx_install()

        def broken_nudge(*args, **kwargs):
            raise BrokenPipeError("closed stderr")

        monkeypatch.setattr(upgrade, "_nudge_if_due", broken_nudge)
        upgrade.update_or_nudge({"upgrade": {"auto_install": False}}, attended=True)

    def test_error_after_claim_marks_the_attempt_failed(self, pipx_install, monkeypatch):
        pipx_install()

        def broken_run(argv):
            raise RuntimeError("installer adapter failed")

        monkeypatch.setattr(upgrade, "_run_pipx", broken_run)
        upgrade.update_or_nudge({}, attended=True)
        assert _cache()["install_attempt_outcome"] == "failed"
        lockfile.acquire_lock()
        lockfile.release_lock()

    def test_intermediate_release_is_incomplete(self, pipx_install, monkeypatch, capsys):
        venv = pipx_install(latest="1.4.0")
        _fake_run(monkeypatch, venv, lands="1.3.0")
        upgrade.update_or_nudge({}, attended=True)
        assert "did not complete" in capsys.readouterr().err
        assert _cache()["install_attempt_outcome"] == "failed"

    def test_cancelled_update_preserves_completed_sync(self, pipx_install, monkeypatch, capsys):
        pipx_install()

        def cancel(argv):
            raise KeyboardInterrupt

        monkeypatch.setattr(upgrade, "_run_pipx", cancel)
        try:
            upgrade.update_or_nudge({}, attended=True)
        except KeyboardInterrupt:
            pytest.fail("automatic update cancellation escaped the completed sync")
        assert _cache()["install_attempt_outcome"] == "failed"
        error = capsys.readouterr().err
        assert "cancelled" in error
        assert upgrade.INSTALL_CMD in error
        assert "cancelled" in upgrade.update_log_path().read_text().lower()
        lockfile.acquire_lock()
        lockfile.release_lock()

    def test_updates_in_the_foreground_and_says_so(self, pipx_install, monkeypatch, capsys):
        venv = pipx_install()
        held = []

        def run(argv):
            # The swap happens under the mm lock, claimed before pipx starts.
            with pytest.raises(Exception, match="already holds the lock"):
                lockfile.acquire_lock()
            held.append(_cache()["install_attempt_version"])
            _write_metadata(venv, spec=upgrade.INSTALL_SPEC, version="1.3.0")
            return subprocess.CompletedProcess(argv, 0, stdout="upgraded\n")

        monkeypatch.setattr(upgrade, "_run_pipx", run)
        upgrade.update_or_nudge({}, attended=True)
        captured = capsys.readouterr()
        assert held == ["1.3.0"]
        assert captured.out == ""
        lines = captured.err.splitlines()
        assert lines == [
            "mm: notice: updating mm 1.2.0 → 1.3.0 (pipx upgrade mind-meld)…",
            "mm: notice: updated mm 1.2.0 → 1.3.0; the next mm command runs it",
        ]
        lockfile.acquire_lock()  # released afterwards
        lockfile.release_lock()
        assert upgrade.update_log_path().read_text().endswith("upgraded\n")

    def test_failure_is_a_notice_and_never_an_exception(self, pipx_install, monkeypatch, capsys):
        venv = pipx_install()
        calls = _fake_run(monkeypatch, venv, returncode=1, output="fatal: no route to host\n")
        upgrade.update_or_nudge({}, attended=True)
        err = capsys.readouterr().err
        assert "automatic update to 1.3.0 did not complete (fatal: no route to host)" in err
        assert "`mm update`" in err and upgrade.INSTALL_CMD in err
        assert _cache()["install_attempt_outcome"] == "failed"
        # The same run does not also print the plain nudge, and the next run
        # neither retries pipx nor repeats itself inside the gates.
        assert err.count("mm: notice:") == 2
        upgrade.update_or_nudge({}, attended=True)
        assert capsys.readouterr().err == ""
        assert len(calls) == 1
        lockfile.acquire_lock()
        lockfile.release_lock()

    def test_pipx_landing_nothing_is_reported_as_incomplete(
        self, pipx_install, monkeypatch, capsys
    ):
        venv = pipx_install()
        _fake_run(monkeypatch, venv)
        upgrade.update_or_nudge({}, attended=True)
        assert "did not complete (pipx found no newer build)" in capsys.readouterr().err
        assert _cache()["install_attempt_outcome"] == "failed"

    def test_busy_lock_skips_without_spending_the_attempt(self, pipx_install, monkeypatch, capsys):
        venv = pipx_install()
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        lockfile.acquire_lock()
        try:
            upgrade.update_or_nudge({}, attended=True)
        finally:
            lockfile.release_lock()
        assert calls == []
        assert capsys.readouterr().err == ""
        assert _cache()["install_attempt_version"] is None
        upgrade.update_or_nudge({}, attended=True)
        assert len(calls) == 1

    def test_unexpected_error_falls_back_to_the_nudge(self, pipx_install, monkeypatch, capsys):
        pipx_install()

        def explode():
            raise RuntimeError("detect blew up")

        monkeypatch.setattr(upgrade, "detect_install", explode)
        upgrade.update_or_nudge({}, attended=True)
        err = capsys.readouterr().err
        assert err.strip() == upgrade.format_upgrade_message("1.2.0", "1.3.0", upgrade.INSTALL_CMD)


class TestUnattendedAutoUpdate:
    def test_spawns_detached_once_and_stays_silent(self, pipx_install, monkeypatch, capsys):
        pipx_install()
        spawns = _fake_spawn(monkeypatch)
        monkeypatch.setattr(upgrade, "_run_pipx", lambda argv: pytest.fail("hook ran foreground"))
        upgrade.update_or_nudge({}, attended=False)
        upgrade.update_or_nudge({}, attended=False)  # a second hook, seconds later
        assert spawns == [([PIPX, "upgrade", "mind-meld"], upgrade.update_log_path())]
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""

    def test_works_while_the_hook_still_holds_the_lock(self, pipx_install, monkeypatch):
        pipx_install()
        spawns = _fake_spawn(monkeypatch)
        lockfile.acquire_lock()
        try:
            upgrade.update_or_nudge({}, attended=False)
        finally:
            lockfile.release_lock()
        assert len(spawns) == 1

    def test_still_behind_after_the_grace_nudges_with_the_log(
        self, pipx_install, monkeypatch, capsys
    ):
        pipx_install()
        spawns = _fake_spawn(monkeypatch)
        stale = datetime.now(timezone.utc) - upgrade.INSTALL_GRACE - timedelta(minutes=1)
        upgrade._claim_install_attempt("1.3.0", now=stale)
        upgrade.update_or_nudge({}, attended=False)
        err = capsys.readouterr().err
        assert spawns == []
        assert err.startswith("mm: notice: 1.2.0 → 1.3.0 available")
        assert "automatic update did not complete" in err
        assert str(upgrade.update_log_path()) in err
        # Nudge gate: once per 24h, not on every hook.
        upgrade.update_or_nudge({}, attended=False)
        assert capsys.readouterr().err == ""

    def test_spawn_failure_nudges_now(self, pipx_install, monkeypatch, capsys):
        pipx_install()
        _fake_spawn(monkeypatch, raises=OSError("fork failed"))
        upgrade.update_or_nudge({}, attended=False)
        assert "automatic update did not complete" in capsys.readouterr().err
        assert _cache()["install_attempt_outcome"] == "failed"


class TestFallsBackToTheNudge:
    """Every reason the automatic path declines leaves the pre-1.3 nudge intact."""

    def _assert_plain_nudge(self, capsys):
        err = capsys.readouterr().err
        assert err.strip() == upgrade.format_upgrade_message("1.2.0", "1.3.0", upgrade.INSTALL_CMD)
        assert _cache()["install_attempt_version"] is None

    @pytest.mark.parametrize("attended", [True, False])
    def test_setting_off(self, pipx_install, monkeypatch, capsys, attended):
        pipx_install()
        monkeypatch.setattr(upgrade, "detect_install", lambda: pytest.fail("read the install"))
        upgrade.update_or_nudge({"upgrade": {"auto_install": False}}, attended=attended)
        self._assert_plain_nudge(capsys)

    @pytest.mark.parametrize(
        "spec, meta",
        [
            (f"{upgrade.REPO_SPEC}@v1.2.0", {}),  # the README rollback pin
            (upgrade.INSTALL_SPEC, {"pinned": True}),
            ("/Users/kb/dev/mind-meld", {}),
        ],
    )
    @pytest.mark.parametrize("attended", [True, False])
    def test_install_that_does_not_track_the_release_branch(
        self, pipx_install, monkeypatch, capsys, spec, meta, attended
    ):
        pipx_install(spec=spec, **meta)
        monkeypatch.setattr(upgrade, "_run_pipx", lambda argv: pytest.fail("ran pipx"))
        monkeypatch.setattr(upgrade, "_spawn_pipx", lambda *a: pytest.fail("spawned pipx"))
        upgrade.update_or_nudge({}, attended=attended)
        self._assert_plain_nudge(capsys)

    def test_not_a_pipx_install(self, monkeypatch, tmp_path, capsys):
        _set_version(monkeypatch, "1.2.0")
        _stub_tags(monkeypatch, ["v1.3.0"])
        monkeypatch.setattr(upgrade, "_install_prefix", lambda: tmp_path)
        upgrade.update_or_nudge({}, attended=True)
        self._assert_plain_nudge(capsys)

    def test_pipx_not_found(self, pipx_install, monkeypatch, capsys):
        pipx_install()
        monkeypatch.setattr(upgrade, "find_pipx", lambda: None)
        upgrade.update_or_nudge({}, attended=False)
        self._assert_plain_nudge(capsys)


class TestNothingToDo:
    @pytest.mark.parametrize("attended", [True, False])
    def test_current_release_is_silent(self, pipx_install, monkeypatch, capsys, attended):
        pipx_install(latest="1.2.0")
        monkeypatch.setattr(upgrade, "detect_install", lambda: pytest.fail("read the install"))
        upgrade.update_or_nudge({}, attended=attended)
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("skip", ["flag", "config"])
    def test_check_opt_outs_also_stop_the_install(self, pipx_install, monkeypatch, capsys, skip):
        venv = pipx_install()
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        monkeypatch.setattr(upgrade, "_fetch_tags", lambda *a: pytest.fail("opted out, fetched"))
        upgrade.set_invocation_skip(skip == "flag")
        upgrade.update_or_nudge({"upgrade": {"auto_check": skip != "config"}}, attended=True)
        assert calls == []
        assert capsys.readouterr().err == ""


# ── mm update ─────────────────────────────────────────────────────────────


def _flat(text: str) -> str:
    return " ".join(text.split())


class TestUpdateCommand:
    @pytest.mark.parametrize(
        "imported, installed, expected_runs", [("1.2.0", "1.3.0", 0), ("1.3.0", "1.2.0", 1)]
    )
    def test_completion_uses_installed_not_imported_version(
        self, pipx_install, monkeypatch, imported, installed, expected_runs
    ):
        venv = pipx_install(version=installed)
        _set_version(monkeypatch, imported)
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert len(calls) == expected_runs
        assert upgrade.detect_install().version == "1.3.0"

    def test_completed_update_during_check_never_force_reinstalls(self, pipx_install, monkeypatch):
        venv = pipx_install(spec=f"{upgrade.REPO_SPEC}@v1.2.0")
        calls = _fake_run(monkeypatch, venv)

        def tags(*args):
            _write_metadata(venv, spec=upgrade.INSTALL_SPEC, version="1.3.0")
            return [{"name": "v1.3.0"}]

        monkeypatch.setattr(upgrade, "_fetch_tags", tags)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert calls == []

    @pytest.mark.parametrize("pinned", [False, True])
    def test_cancellation_prints_the_reinstall_remedy(self, pipx_install, monkeypatch, pinned):
        pipx_install(spec=f"{upgrade.REPO_SPEC}@v1.2.0" if pinned else upgrade.INSTALL_SPEC)

        def cancel(argv):
            raise KeyboardInterrupt

        monkeypatch.setattr(upgrade, "_run_pipx", cancel)
        result = runner.invoke(app, ["update"])
        assert result.exit_code != 0
        assert upgrade.INSTALL_CMD in " ".join(result.stderr.split())

    @pytest.mark.parametrize("pinned, lands", [(False, "1.3.0"), (True, "1.2.0")])
    def test_changed_install_below_tagged_target_exits_1(
        self, pipx_install, monkeypatch, pinned, lands
    ):
        spec = f"{upgrade.REPO_SPEC}@v1.2.0" if pinned else upgrade.INSTALL_SPEC
        venv = pipx_install(spec=spec, latest="1.4.0")
        _fake_run(monkeypatch, venv, lands=lands)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1, result.output
        assert "1.4.0 is tagged" in _flat(result.stderr)

    def test_pinned_suffix_targets_the_same_environment(self, pipx_install, monkeypatch):
        original = pipx_install()
        venv = original.with_name("mind-meld-rollback")
        _write_metadata(
            venv, spec=f"{upgrade.REPO_SPEC}@v1.2.0", version="1.2.0", suffix="-rollback"
        )
        monkeypatch.setattr(upgrade, "_install_prefix", lambda: venv)
        calls = []

        def run(argv):
            calls.append(argv)
            _write_metadata(venv, spec=upgrade.INSTALL_SPEC, version="1.3.0", suffix="-rollback")
            return subprocess.CompletedProcess(argv, 0, stdout="upgraded\n")

        monkeypatch.setattr(upgrade, "_run_pipx", run)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert "--suffix=-rollback" in calls[0]
        assert (
            json.loads((original / upgrade.PIPX_METADATA_NAME).read_text())["main_package"][
                "package_version"
            ]
            == "1.2.0"
        )

    def test_updates_a_tracking_install(self, pipx_install, monkeypatch):
        venv = pipx_install()
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert calls == [[PIPX, "upgrade", "mind-meld"]]
        assert "Updating mm 1.2.0 → 1.3.0 (pipx upgrade mind-meld)" in _flat(result.stdout)
        assert "Updated mm 1.2.0 → 1.3.0." in result.stdout
        lockfile.acquire_lock()
        lockfile.release_lock()

    def test_needs_no_config(self, pipx_install, monkeypatch, tmp_path):
        venv = pipx_install()
        _fake_run(monkeypatch, venv, lands="1.3.0")
        monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "absent.toml")
        assert runner.invoke(app, ["update"]).exit_code == 0

    def test_up_to_date_runs_nothing(self, pipx_install, monkeypatch):
        venv = pipx_install(latest="1.2.0")
        calls = _fake_run(monkeypatch, venv)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert calls == []
        assert _flat(result.stdout) == "mm 1.2.0 is up to date."

    def test_ignores_the_throttle_and_every_opt_out(self, pipx_install, monkeypatch, tmp_path):
        venv = pipx_install()
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        # A fresh cache saying "current" and a recent failed fetch would both
        # stop the background check; the explicit command checks anyway.
        now = datetime.now(timezone.utc).isoformat()
        upgrade.CACHE_PATH.write_text(
            json.dumps({"latest_version": "1.2.0", "checked_at": now, "attempted_at": now})
        )
        result = runner.invoke(app, ["--no-check-version", "update"])
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert _cache()["latest_version"] == "1.3.0"

    def test_unreachable_github_still_lets_pipx_decide(self, pipx_install, monkeypatch):
        venv = pipx_install()

        def offline(*a):
            raise OSError("offline")

        monkeypatch.setattr(upgrade, "_fetch_tags", offline)
        # A stale cache must not answer for a check that just failed.
        upgrade.CACHE_PATH.write_text(
            json.dumps({"latest_version": "1.2.0", "checked_at": "2026-01-01T00:00:00+00:00"})
        )
        calls = _fake_run(monkeypatch, venv)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert "already the latest release pipx can see" in _flat(result.stdout)

    def test_pinned_install_is_reinstalled_onto_the_release_branch(self, pipx_install, monkeypatch):
        venv = pipx_install(spec=f"{upgrade.REPO_SPEC}@v1.2.0")
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert calls == [[PIPX, "install", "--force", upgrade.INSTALL_SPEC]]
        text = _flat(result.stdout)
        assert "Updated mm 1.2.0 → 1.3.0." in text
        assert "now tracks the release branch" in text

    def test_pinned_and_current_explains_the_pin_without_reinstalling(
        self, pipx_install, monkeypatch
    ):
        venv = pipx_install(spec=f"{upgrade.REPO_SPEC}@v1.2.0", latest="1.2.0")
        calls = _fake_run(monkeypatch, venv)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert calls == []
        text = _flat(result.stdout)
        assert "mm 1.2.0 is up to date." in text
        assert f"pinned to {upgrade.REPO_SPEC}@v1.2.0" in text
        assert upgrade.INSTALL_CMD in text

    def test_pinned_install_is_never_force_reinstalled_on_a_guess(self, pipx_install, monkeypatch):
        venv = pipx_install(spec=f"{upgrade.REPO_SPEC}@v1.2.0")

        def offline(*a):
            raise OSError("offline")

        monkeypatch.setattr(upgrade, "_fetch_tags", offline)
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        assert calls == []
        assert "Could not reach GitHub" in _flat(result.stderr)

    def test_failed_update_exits_1_and_shows_pipx_output(self, pipx_install, monkeypatch):
        venv = pipx_install()
        _fake_run(monkeypatch, venv, returncode=1, output="cloning\nfatal: no route to host\n")
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        err = _flat(result.stderr)
        assert "cloning fatal: no route to host" in err
        assert "Update did not complete: fatal: no route to host." in err
        assert upgrade.INSTALL_CMD in err
        lockfile.acquire_lock()
        lockfile.release_lock()

    def test_failed_forced_reinstall_says_how_to_get_mm_back(self, pipx_install, monkeypatch):
        venv = pipx_install(spec=f"{upgrade.REPO_SPEC}@v1.2.0")
        _fake_run(monkeypatch, venv, returncode=1, output="fatal: no route to host\n")
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        err = _flat(result.stderr)
        recovery = upgrade.reinstall_cmd(upgrade.detect_install())
        expected = f"If mm is now missing, reinstall with: {recovery}"
        assert "".join(expected.split()) in "".join(err.split())

    def test_tagged_but_not_installable_exits_1(self, pipx_install, monkeypatch):
        venv = pipx_install()
        _fake_run(monkeypatch, venv)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        assert "pipx found no build newer than 1.2.0, but 1.3.0 is tagged" in _flat(result.stderr)

    def test_refuses_while_another_mm_holds_the_lock(self, pipx_install, monkeypatch):
        venv = pipx_install()
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        other = subprocess.Popen(
            [
                "python3",
                "-c",
                "import fcntl, sys, time; f = open(sys.argv[1], 'w'); "
                "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)",
                str(lockfile.LOCK_PATH),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert other.stdout.readline().strip() == "locked"
            result = runner.invoke(app, ["update"])
        finally:
            other.kill()
            other.wait()
        assert result.exit_code == 1
        assert calls == []
        assert "Another mm operation is running" in _flat(result.stderr)

    @pytest.mark.parametrize(
        "spec, meta, phrase",
        [
            (upgrade.INSTALL_SPEC, {"pinned": True}, "held by pipx pin"),
            ("/Users/kb/dev/mind-meld", {}, "installed from /Users/kb/dev/mind-meld"),
        ],
    )
    def test_refuses_installs_it_does_not_own(self, pipx_install, monkeypatch, spec, meta, phrase):
        venv = pipx_install(spec=spec, **meta)
        calls = _fake_run(monkeypatch, venv, lands="1.3.0")
        monkeypatch.setattr(upgrade, "_fetch_tags", lambda *a: pytest.fail("refused, yet fetched"))
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        assert calls == []
        assert phrase in _flat(result.stderr)

    def test_refuses_a_non_pipx_install_with_the_install_command(self, monkeypatch, tmp_path):
        _set_version(monkeypatch, "1.2.0")
        monkeypatch.setattr(upgrade, "_install_prefix", lambda: tmp_path)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        err = _flat(result.stderr)
        assert "was not installed by pipx" in err
        assert upgrade.INSTALL_CMD in err

    def test_refuses_a_dev_build(self, monkeypatch):
        _set_version(monkeypatch, upgrade.DEV_BUILD_SENTINEL)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        assert "source-tree build" in _flat(result.stderr)

    def test_pipx_missing(self, pipx_install, monkeypatch):
        pipx_install()
        monkeypatch.setattr(upgrade, "find_pipx", lambda: None)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        assert "pipx was not found" in _flat(result.stderr)


# ── the CLI seams ─────────────────────────────────────────────────────────


@pytest.fixture
def seam(tmp_path, monkeypatch):
    _setup_real_config(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        upgrade, "update_or_nudge", lambda config, *, attended: calls.append(attended)
    )
    return calls


class TestSeams:
    @pytest.mark.parametrize("command", ["push", "pull", "autopush", "autopull"])
    def test_interrupted_tag_response_preserves_sync(
        self, tmp_path, monkeypatch, pipx_install, command
    ):
        from mind_meld import sidecar

        _setup_real_config(tmp_path, monkeypatch)
        pipx_install()

        def interrupted(*args):
            raise IncompleteRead(b"synthetic", 100)

        monkeypatch.setattr(upgrade, "_fetch_tags", interrupted)
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        if command.startswith("auto"):
            breadcrumb = json.loads((sidecar.SIDECAR_DIR / "last-autorun.json").read_text())
            assert breadcrumb[command.removeprefix("auto")]["outcome"] != "failed"

    def test_cancelled_push_never_starts_an_update(self, tmp_path, monkeypatch):
        from mind_meld import cli

        _setup_real_config(tmp_path, monkeypatch)
        calls = []

        def cancel(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_push_core", cancel)
        monkeypatch.setattr(upgrade, "update_or_nudge", lambda *args, **kwargs: calls.append(1))
        assert runner.invoke(app, ["push"]).exit_code != 0
        assert calls == []

    @pytest.mark.parametrize(
        "command, attended",
        [("push", True), ("pull", True), ("autopush", False), ("autopull", False)],
    )
    def test_each_sync_command_reaches_the_tail_once(self, seam, command, attended):
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        assert seam == [attended]

    @pytest.mark.parametrize("command", ["push", "pull"])
    def test_previews_never_update(self, seam, command):
        result = runner.invoke(app, [command, "--dry-run"])
        assert result.exit_code == 0, result.output
        assert seam == []

    @pytest.mark.parametrize("command", ["push", "autopush"])
    def test_a_failed_update_never_changes_the_sync_exit_code(
        self, tmp_path, monkeypatch, pipx_install, command
    ):
        _setup_real_config(tmp_path, monkeypatch)
        venv = pipx_install()
        _fake_run(monkeypatch, venv, returncode=1, output="fatal: no route to host\n")
        _fake_spawn(monkeypatch, raises=OSError("fork failed"))
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output
        assert "did not complete" in result.stderr

    def test_status_names_a_failed_automatic_update(self, tmp_path, monkeypatch):
        _setup_real_config(tmp_path, monkeypatch)
        _set_version(monkeypatch, "1.2.0")
        now = datetime.now(timezone.utc)
        cache = {"latest_version": "1.3.0", "checked_at": now.isoformat()}
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        assert "Automatic update" not in runner.invoke(app, ["status"]).stdout
        cache |= {
            "install_attempt_version": "1.3.0",
            "install_attempt_at": (now - timedelta(hours=1)).isoformat(),
        }
        upgrade.CACHE_PATH.write_text(json.dumps(cache))
        text = _flat(runner.invoke(app, ["status"]).stdout)
        assert "Automatic update did not complete: run mm update to see why." in text
