"""Self-update: check GitHub release tags for a newer release, install it
through pipx when that is safe, otherwise nudge once per 24h via stderr; log
self-version transitions to pullhistory.

Two ways mm runs pipx (v1.3.0), and they are deliberately not the same command:

* Automatic (tail of pull/push/autopull/autopush, `[upgrade] auto_install`):
  ONLY the in-place `pipx upgrade <venv>`, and ONLY when the recorded install
  spec is `INSTALL_SPEC`. pipx does not delete the venv on an in-place
  upgrade failure. Every other install shape keeps the nudge.
* Explicit (`mm update`): the same in-place upgrade, plus the `--force`
  reinstall that moves a tag-pinned install onto the release branch. pipx
  DELETES the venv when a forced reinstall fails, so that command never runs
  unattended.

See docs/invariants/auto-upgrade.md.

Version source: `git ls-remote --tags --refs` over `REPO_URL`, the same URL
pip clones for `INSTALL_SPEC`, using the machine's own git configuration and
credentials in a non-interactive environment. The repository tags releases
v0.3.0..vX.Y.Z without GitHub Releases; we read the complete tag list, filter
to non-prerelease non-local-version semver tags, and take the maximum via
packaging.Version with the leading `v` stripped.

Cache: single JSON file at ~/.config/mind-meld/upgrade-state.json, fcntl-flocked
on every read+modify+write so transition detection is race-correct under two
concurrent mm processes. The single-file design replaced an earlier two-file
draft — Codex outside voice caught a read-modify-write race in the split design
where `self-version` had no flock domain.

Lock-order invariants (load-bearing):
  1. NEVER acquire the mm lockfile while holding upgrade-state's flock.
  2. RELEASE upgrade-state's flock BEFORE writing state.db history; its write
     transaction is always the innermost lock. Never acquire the mm lockfile
     or upgrade-state flock inside it. History may write outside the mm lock;
     provisional ledger/cursors/own_log/displaced writes require the mm lock.
  3. Transition detection runs OUTSIDE the mm lockfile by design — its
     correctness is bounded by upgrade-state's own flock. Status and push
     preview skip this hook so inspection does not consume transitions.

Visible-failure stance: this is NOT a data-at-risk signal. Check and automatic
install failures degrade silently on sync commands (`_check_fleet_version_or_refuse`
already backstops mixed-version drift) and are recorded in `upgrade-state.json`
for `mm status`, `mm diag`, and `mm update`. The nudge uses prefix `mm: notice:`
(NOT `mm: warning:`) so the warning-class reader trust stays focused on
data-at-risk signals only.
"""

from __future__ import annotations

import codecs
import errno
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from mind_meld import __version__, fsutil, lockfile, pullhistory, updateprogress
from mind_meld.errors import LockError
from mind_meld.lockedjson import locked_json_rmw, locked_json_snapshot
from mind_meld.safety import safe_terminal_str, strip_terminal_escapes

CACHE_DIR = Path.home() / ".config" / "mind-meld"
CACHE_PATH = CACHE_DIR / "upgrade-state.json"

# Upgrade command tracks the moving `latest` branch (force-advanced to each
# tagged release by .github/workflows/release.yml), NOT a frozen `@vX.Y.Z` tag.
# A git `@<ref>` is a URL fragment, not a PEP508 version specifier, so pipx's
# `parse_specifier_for_upgrade` keeps it verbatim on `pipx upgrade`: a tag ref
# re-resolves to the same frozen commit forever (the historical lock — install
# stuck on one version), while a branch ref re-resolves to its advancing HEAD.
# The `--force` reinstall both lands the latest release AND rewrites a
# previously tag-pinned install's recorded URL onto `@latest`, after which a
# plain `pipx upgrade mind-meld` works.
# Tag checks list `refs/tags/*` directly from `REPO_URL` with `git ls-remote`
# so the check uses the same git configuration, `insteadOf` rewrites, and
# credentials as pip's clone of `INSTALL_SPEC`, with no pagination cap.
# See docs/invariants/auto-upgrade.md.
REPO_SPEC = "git+https://github.com/kbitz/mind-meld.git"
REPO_URL = REPO_SPEC.removeprefix("git+")
INSTALL_SPEC = f"{REPO_SPEC}@latest"
INSTALL_CMD = f"pipx install --force {INSTALL_SPEC}"

DEFAULT_THROTTLE = timedelta(hours=24)
DEFAULT_NUDGE_GAP = timedelta(hours=24)
DEFAULT_FAILURE_BACKOFF = timedelta(hours=4)
CHECK_TIMEOUT_SECONDS = 10
SSH_CONFIG_TIMEOUT_SECONDS = 5
DEV_BUILD_SENTINEL = "0.0.0+dev"

CHECK_FAILURE_CAUSES = ("not-readable", "network", "unparseable", "git-missing")
INSTALL_FAILURE_CAUSES = ("not-tracking", "pipx-missing", "install-failed")

# One automatic install attempt per release per day. A newer release resets
# the gate; a failed attempt falls back to the nudge until the gap elapses.
DEFAULT_INSTALL_RETRY_GAP = timedelta(hours=24)
# After the installer exits, this window gives its metadata time to settle.
# A live install's separate kernel lock always overrides this age heuristic.
INSTALL_GRACE = timedelta(minutes=10)
PIPX_TIMEOUT_SECONDS = 600
PIPX_STOP_GRACE_SECONDS = 5
# The progress capture is parsed, never shown. pip lays its counters out for
# this width, so a narrow window cannot wrap them out of the parser's reach.
PTY_ROWS = 50
PTY_COLUMNS = 200
PIPX_METADATA_NAME = "pipx_metadata.json"
PACKAGE_NAME = "mind-meld"
UPDATE_LOG_NAME = "auto-update.log"
INSTALL_LOCK_NAME = "install.lock"
INSTALL_LOCK_RETRY_DELAYS = (0, 0.01, 0.05, 0.1)
# Hooks can run with a thinner PATH than the login shell that installed pipx.
_PIPX_FALLBACK_PATHS = ("/opt/homebrew/bin/pipx", "/usr/local/bin/pipx", "~/.local/bin/pipx")

# Within-process idempotency for transition detection. Set True after the
# first invocation of `run_transition_hook` per process so two `_get_config`
# calls in one mm invocation log at most one self-upgrade row. Reset on
# interpreter exit (next mm invocation re-runs cleanly).
_TRANSITION_DETECTED_THIS_INVOCATION = False

# Set to the (old, new) tuple by `detect_self_version_transition` whenever a
# real transition is observed in this process. Read by `_run_events_tail` to
# decide whether to grant a one-time extended budget for the inline token-
# cache warm (v0.11.14+ — first push after an mm upgrade gets ~5s instead
# of the normal 250/500ms autopush/interactive budget). None on the steady-
# state path (no transition this process). Reset on interpreter exit.
_LAST_TRANSITION_SEEN: tuple[str, str] | None = None

# Set by the global `--no-check-version` Typer flag in cli.py:_main.
# When True, all upgrade-module side effects no-op for this invocation.
_INVOCATION_SKIP = False


def set_invocation_skip(skip: bool) -> None:
    """Wire-up for the `--no-check-version` CLI flag.

    Called from cli.py:_main once at startup. When True, both
    `check_for_upgrade` and `run_transition_hook` short-circuit to no-ops
    for the remainder of this process.
    """
    global _INVOCATION_SKIP
    _INVOCATION_SKIP = skip


# ── Result types ──────────────────────────────────────────────────────────


@dataclass
class UpgradeCheckResult:
    """Return value of `check_for_upgrade` and `cached_upgrade_view`.

    state:
      "skip"               — dev build, opt-out, --no-check-version, or
                             cache-fresh-and-equal. Caller does nothing.
      "current"            — local version matches latest. No nudge.
      "upgrade-available"  — caller may nudge, gated by `should_nudge`.
      "unknown"            — missing, malformed, or contended cache
                             (`cached_upgrade_view`); network or parse
                             failure (`check_for_upgrade`). Caller does
                             nothing.

    `cache_state` names why a cache-only view is unknown (`missing`,
    `malformed`, `lock_failed`) or `valid` when the cache parsed.
    `checked_at` / `stale` carry the cache's age for status display.
    `install_attempt` is the automatic install's state for `latest`
    (`in-flight` / `failed`), None when none was made (`cached_upgrade_view`).
    `check_failure` / `check_failure_at` and `install_failure` /
    `install_failure_at` carry the recorded failure cause and timestamp.
    """

    state: str
    local: str
    latest: str | None
    install_cmd: str | None
    should_nudge: bool = False  # True only for "upgrade-available" past gate
    checked_at: datetime | None = None
    stale: bool = False
    cache_state: str | None = None
    install_attempt: str | None = None
    check_failure: str | None = None
    check_failure_at: datetime | None = None
    install_failure: str | None = None
    install_failure_at: datetime | None = None


# ── Cache I/O (single file, single flock — via mind_meld.lockedjson) ──────


def _empty_cache() -> dict[str, Any]:
    return {
        "latest_version": None,
        "checked_at": None,
        "attempted_at": None,
        "last_nudged_version": None,
        "last_nudged_at": None,
        "last_seen_self_version": None,
        "install_attempt_version": None,
        "install_attempt_at": None,
        "install_attempt_outcome": None,
        "check_failure_cause": None,
        "check_failure_at": None,
        "install_failure_cause": None,
        "install_failure_at": None,
    }


def _normalize_cache(parsed: dict[str, Any]) -> dict[str, Any]:
    """Backfill missing keys onto a parsed cache (forward-compat with future
    schema additions). Pre-extraction this lived inside the read helper;
    the lockedjson helper hands us the raw parsed dict, so the backfill
    moved here."""
    base = _empty_cache()
    base.update({k: parsed.get(k, base[k]) for k in base})
    return base


class _CheckFailed(Exception):
    """Tag-check failure with a fixed cause token."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def _unexpected_cause(error: BaseException) -> str:
    name = type(error).__name__
    if name.isidentifier() and len(name) <= 64:
        return f"unexpected:{name}"
    return "unexpected:Exception"


def _valid_cause(value: Any, allowed: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    if value in allowed:
        return value
    if value.startswith("unexpected:"):
        cls_name = value.removeprefix("unexpected:")
        if cls_name and cls_name.isidentifier() and len(cls_name) <= 64:
            return value
    return None


def problem_text(cause: str, *, stage: str = "check") -> tuple[str, str]:
    """Return `(what_failed, fix_command)` for a recorded check or install cause."""
    if cause == "not-readable":
        return (
            "mm could not read the release tags with this machine's git credentials",
            "Run: gh auth setup-git",
        )
    if cause == "network":
        return (
            "Could not reach GitHub to check the latest release",
            "Run: mm update",
        )
    if cause == "unparseable":
        return (
            "GitHub listed no release tag mm could read",
            "Run: mm update",
        )
    if cause == "git-missing":
        return (
            "git was not found on PATH",
            "Install git, then run: mm update",
        )
    if cause == "not-tracking":
        return (
            "this mm was not installed by pipx from the release branch, "
            "so it does not update itself",
            "Run: mm update",
        )
    if cause == "pipx-missing":
        return (
            "pipx was not found, so mm cannot update itself",
            "Install pipx, then run: mm update",
        )
    if cause == "install-failed":
        return (
            "automatic update did not complete",
            "Run: mm update",
        )
    if cause.startswith("unexpected:"):
        raw_cls = cause.removeprefix("unexpected:")
        cls_name = (
            raw_cls
            if raw_cls and raw_cls.isidentifier() and len(raw_cls) <= 64
            else (safe_terminal_str(raw_cls) or "Exception")
        )
        return (
            f"unexpected {stage} error ({cls_name})",
            "Run: mm update",
        )
    return (
        f"unexpected {stage} error",
        "Run: mm update",
    )


# ── Tag-list git adapter (testable seam) ──────────────────────────────────


def find_git() -> str | None:
    """Locate git on the process PATH only (never fallback to /usr/bin/git)."""
    return shutil.which("git")


def _refuse_git_under_pytest() -> None:
    """Prevent unstubbed tests from reaching a real git remote, credential
    helper, or network. Separate from `_refuse_under_pytest` so pipx tests
    that disable the pipx guard never enable a real git subprocess."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise OSError("refusing to run git under pytest")


def _run_git(
    argv: list[str], *, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[bytes]:
    """Run git in a detached session with stdin closed and no controlling tty.

    Subprocess seam: tests replace THIS function wholesale, or turn off
    `_refuse_git_under_pytest` and point `find_git` at a synthetic script.
    """
    _refuse_git_under_pytest()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tempfile.gettempdir(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        stop_deadline = time.monotonic() + 1.0
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
        try:
            proc.wait(timeout=max(0.0, stop_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        while time.monotonic() < stop_deadline:
            try:
                os.killpg(proc.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.02)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout=stdout, stderr=stderr)


def _no_prompt_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Build a non-interactive environment for git and pipx subprocesses."""
    env = dict(base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["SSH_ASKPASS_REQUIRE"] = "never"
    env["GCM_INTERACTIVE"] = "never"
    if not env.get("GIT_HTTP_LOW_SPEED_LIMIT"):
        env["GIT_HTTP_LOW_SPEED_LIMIT"] = "1000"
    if not env.get("GIT_HTTP_LOW_SPEED_TIME"):
        env["GIT_HTTP_LOW_SPEED_TIME"] = "60"

    existing_ssh_cmd = env.get("GIT_SSH_COMMAND", "").strip()
    if existing_ssh_cmd:
        if "BatchMode=yes" not in existing_ssh_cmd:
            env["GIT_SSH_COMMAND"] = f"{existing_ssh_cmd} -o BatchMode=yes"
        else:
            env["GIT_SSH_COMMAND"] = existing_ssh_cmd
    elif env.get("GIT_SSH", "").strip():
        env.pop("GIT_SSH_COMMAND", None)
    else:
        ssh_base = "ssh"
        git = find_git()
        if git is not None:
            probe_env = dict(env)
            probe_env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
            try:
                proc = _run_git(
                    [git, "config", "--get", "core.sshCommand"],
                    env=probe_env,
                    timeout=min(SSH_CONFIG_TIMEOUT_SECONDS, CHECK_TIMEOUT_SECONDS),
                )
                if proc.returncode == 0 and proc.stdout:
                    raw = (
                        proc.stdout.decode("utf-8", errors="replace")
                        if isinstance(proc.stdout, bytes)
                        else str(proc.stdout)
                    ).strip()
                    if raw and "\n" not in raw and "\t" not in raw and "refs/" not in raw:
                        ssh_base = raw
            except Exception:
                ssh_base = "ssh"
        if "BatchMode=yes" in ssh_base:
            env["GIT_SSH_COMMAND"] = ssh_base
        else:
            env["GIT_SSH_COMMAND"] = f"{ssh_base} -o BatchMode=yes"
    return env


_NOT_READABLE_PATTERNS = (
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "authentication failed",
    "repository not found",
    "permission denied",
    "host key verification failed",
    "returned error: 401",
    "returned error: 403",
)


def _classify_git_failure(stderr: str) -> str:
    """Map git stderr to a fixed cause string without retaining stderr text."""
    lowered = stderr.lower()
    for pattern in _NOT_READABLE_PATTERNS:
        if pattern in lowered:
            return "not-readable"
    return "network"


def _fetch_tags() -> list[dict[str, Any]]:
    """List release tags from `REPO_URL` using the machine's git configuration.

    Tests monkeypatch THIS function or `_run_git`. Returns `[{"name": "vX.Y.Z"}, ...]`
    with peeled (`^{}`) and non-tag refs excluded.
    """
    git = find_git()
    if git is None:
        raise _CheckFailed("git-missing")
    env = _no_prompt_environment(os.environ)
    env["LC_ALL"] = "C"
    try:
        proc = _run_git(
            [git, "ls-remote", "--tags", "--refs", REPO_URL],
            env=env,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise _CheckFailed("network") from error
    except FileNotFoundError as error:
        raise _CheckFailed("git-missing") from error
    except OSError as error:
        raise _CheckFailed(_unexpected_cause(error)) from error

    if proc.returncode != 0:
        stderr = (
            proc.stderr.decode("utf-8", errors="replace")
            if isinstance(proc.stderr, bytes)
            else str(proc.stderr or "")
        )
        raise _CheckFailed(_classify_git_failure(stderr))

    stdout = (
        proc.stdout.decode("utf-8", errors="replace")
        if isinstance(proc.stdout, bytes)
        else str(proc.stdout or "")
    )
    tags: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        name = ref.removeprefix("refs/tags/")
        if not name or name.endswith("^{}"):
            continue
        tags.append({"name": name})
    return tags


# ── Tag selection: max-semver, skip prerelease + local versions ───────────


def _pick_latest_tag(tags: list[dict[str, Any]]) -> tuple[str, Version] | None:
    """Return (raw_tag_name, parsed_Version) of the highest non-prerelease
    non-local-version tag. Returns None if no valid tag exists.

    Filtering:
      - strip leading `v`
      - InvalidVersion → silently skipped
      - Version.is_prerelease (rc/alpha/beta/dev) → skipped
      - Version.local is not None (e.g. 0.9.4+local) → skipped because
        packaging sorts +local > non-local, which would falsely become latest
    """
    best: tuple[str, Version] | None = None
    for entry in tags:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            v = Version(name.lstrip("v"))
        except InvalidVersion:
            continue
        if v.is_prerelease or v.local is not None:
            continue
        if best is None or v > best[1]:
            best = (name, v)
    return best


# ── Public: check_for_upgrade ─────────────────────────────────────────────


def cached_upgrade_view(
    config: dict[str, Any] | None = None, *, now: datetime | None = None
) -> UpgradeCheckResult:
    """Inspect the last check without network, cache writes, or waiting on a writer."""
    local = __version__
    upgrade_cfg = (config or {}).get("upgrade", {})
    if (
        local == DEV_BUILD_SENTINEL
        or _INVOCATION_SKIP
        or (isinstance(upgrade_cfg, dict) and upgrade_cfg.get("auto_check") is False)
    ):
        return UpgradeCheckResult("skip", local, None, None)
    now = now or datetime.now(timezone.utc)
    with locked_json_snapshot(CACHE_PATH, blocking=False) as snapshot:
        if snapshot.state != "valid" or snapshot.data is None:
            return UpgradeCheckResult("unknown", local, None, None, cache_state=snapshot.state)
        check_failure = _valid_cause(snapshot.data.get("check_failure_cause"), CHECK_FAILURE_CAUSES)
        check_failure_at = (
            _parse_iso(snapshot.data.get("check_failure_at")) if check_failure else None
        )
        install_failure = _valid_cause(
            snapshot.data.get("install_failure_cause"), INSTALL_FAILURE_CAUSES
        )
        install_failure_at = (
            _parse_iso(snapshot.data.get("install_failure_at")) if install_failure else None
        )
        latest = snapshot.data.get("latest_version")
        checked_at = _parse_iso(snapshot.data.get("checked_at"))
        available = False
        valid_latest = False
        if isinstance(latest, str) and checked_at is not None:
            try:
                available = Version(latest) > Version(local)
                valid_latest = True
            except InvalidVersion:
                valid_latest = False
        attempt = (
            _attempt_state(snapshot.data, latest, now) if (valid_latest and available) else None
        )

    if install_failure is not None:
        if not auto_install_enabled(config) or (valid_latest and not available):
            install_failure = None
            install_failure_at = None
        elif install_failure == "not-tracking" and detect_install().kind == "tracking":
            install_failure = None
            install_failure_at = None

    if not valid_latest or not isinstance(latest, str) or checked_at is None:
        return UpgradeCheckResult(
            "unknown",
            local,
            None,
            None,
            cache_state="malformed",
            check_failure=check_failure,
            check_failure_at=check_failure_at,
            install_failure=install_failure,
            install_failure_at=install_failure_at,
        )

    if attempt is not None:
        install = detect_install()
        if install.kind != "tracking" or Version(install.version or "") >= Version(latest):
            attempt = None
        elif _install_is_running():
            attempt = "in-flight"
    return UpgradeCheckResult(
        "upgrade-available" if available else "current",
        local,
        latest,
        INSTALL_CMD if available else None,
        checked_at=checked_at,
        stale=now - checked_at >= DEFAULT_THROTTLE,
        cache_state="valid",
        install_attempt=attempt,
        check_failure=check_failure,
        check_failure_at=check_failure_at,
        install_failure=install_failure,
        install_failure_at=install_failure_at,
    )


def check_for_upgrade(
    config: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> UpgradeCheckResult:
    """Return whether an upgrade is available; honor cache; respect opt-outs.

    Cache is the FIRST gate — no tag fetch unless cache is stale (older than
    `DEFAULT_THROTTLE`) AND last attempt is older than `DEFAULT_FAILURE_BACKOFF`.
    Check failures degrade silently (return cached state or "unknown") and record
    their cause in `upgrade-state.json` because `_check_fleet_version_or_refuse`
    backstops the data-at-risk case.

    Short-circuits (return state="skip"):
      - dev build (__version__ == "0.0.0+dev")
      - --no-check-version flag (set via `set_invocation_skip(True)`)
      - config has [upgrade] auto_check = false

    `force=True` is `mm update`: the user asked, so the opt-outs, the 24h
    throttle and the failure backoff do not apply. A dev build still skips.

    The `should_nudge` field is True only when state == "upgrade-available"
    AND (last_nudged_version != latest OR last_nudged_at + 24h is past).
    Caller is responsible for actually emitting the stderr line and updating
    last_nudged_at via `record_nudge`.
    """
    local = __version__
    install_cmd: str | None = None

    # Short-circuit: dev build.
    if local == DEV_BUILD_SENTINEL:
        return UpgradeCheckResult(state="skip", local=local, latest=None, install_cmd=None)

    # Short-circuit: --no-check-version flag.
    if _INVOCATION_SKIP and not force:
        return UpgradeCheckResult(state="skip", local=local, latest=None, install_cmd=None)

    # Short-circuit: config opt-out.
    if config is not None and not force:
        upgrade_cfg = config.get("upgrade", {})
        if isinstance(upgrade_cfg, dict) and upgrade_cfg.get("auto_check") is False:
            return UpgradeCheckResult(state="skip", local=local, latest=None, install_cmd=None)

    now = now or datetime.now(timezone.utc)

    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                # Helper degraded gracefully (only in non-block modes; we use
                # default block mode here, so this path is unreachable today).
                # Future-proof: bail without nudging.
                return UpgradeCheckResult(
                    state="unknown", local=local, latest=None, install_cmd=None
                )
            cache = _normalize_cache(ljson.data)
            ljson.data.clear()
            ljson.data.update(cache)  # write-through: helper persists ljson.data
            cached_latest = cache.get("latest_version")

            # Decide whether to fetch.
            checked_at = _parse_iso(cache.get("checked_at"))
            attempted_at = _parse_iso(cache.get("attempted_at"))
            cache_fresh = checked_at is not None and (now - checked_at) < DEFAULT_THROTTLE
            backoff_active = (
                attempted_at is not None and (now - attempted_at) < DEFAULT_FAILURE_BACKOFF
            )

            failure_cause: str | None = None
            fetched_ok = False
            if force or (not cache_fresh and not backoff_active):
                # Stale cache + no recent failed attempt → fetch.
                try:
                    tags = _fetch_tags()
                    picked = _pick_latest_tag(tags)
                    if picked is None:
                        raise _CheckFailed("unparseable")
                    cached_latest = picked[0].lstrip("v")
                    ljson.data["latest_version"] = cached_latest
                    ljson.data["checked_at"] = now.isoformat()
                    ljson.data["attempted_at"] = now.isoformat()
                    ljson.data["check_failure_cause"] = None
                    ljson.data["check_failure_at"] = None
                    fetched_ok = True
                except _CheckFailed as error:
                    failure_cause = error.cause
                except OSError:
                    failure_cause = "network"
                except ValueError:
                    failure_cause = "unparseable"
                except Exception as error:
                    failure_cause = _unexpected_cause(error)

                if failure_cause is not None:
                    # Check failure: update attempted_at and record the cause,
                    # then fall back to cached state. A forced check never
                    # answers from a cache it just failed to refresh.
                    ljson.data["attempted_at"] = now.isoformat()
                    ljson.data["check_failure_cause"] = failure_cause
                    ljson.data["check_failure_at"] = now.isoformat()
                    if cached_latest is None or force:
                        return UpgradeCheckResult(
                            state="unknown",
                            local=local,
                            latest=cached_latest,
                            install_cmd=None,
                            check_failure=failure_cause,
                            check_failure_at=now,
                        )

            failure_at = now if failure_cause is not None else None

            # Compare local vs cached_latest.
            if cached_latest is None:
                return UpgradeCheckResult(
                    state="unknown",
                    local=local,
                    latest=None,
                    install_cmd=None,
                    check_failure=failure_cause,
                    check_failure_at=failure_at,
                )

            try:
                local_v = Version(local)
                latest_v = Version(cached_latest)
            except InvalidVersion:
                return UpgradeCheckResult(
                    state="unknown",
                    local=local,
                    latest=cached_latest,
                    install_cmd=None,
                    check_failure=failure_cause,
                    check_failure_at=failure_at,
                )

            if latest_v <= local_v:
                if fetched_ok:
                    ljson.data["install_failure_cause"] = None
                    ljson.data["install_failure_at"] = None
                return UpgradeCheckResult(
                    state="current",
                    local=local,
                    latest=cached_latest,
                    install_cmd=None,
                    check_failure=failure_cause,
                    check_failure_at=failure_at,
                )

            # Upgrade available. Apply nudge gate: last_nudged_version != latest
            # OR last_nudged_at + 24h past. The command is version-independent
            # (tracks the `latest` branch); the target version lives in the
            # nudge message text, not the command.
            install_cmd = INSTALL_CMD
            last_nudged_version = ljson.data.get("last_nudged_version")
            last_nudged_at = _parse_iso(ljson.data.get("last_nudged_at"))
            version_changed = last_nudged_version != cached_latest
            gap_elapsed = last_nudged_at is None or (now - last_nudged_at) >= DEFAULT_NUDGE_GAP
            should_nudge = version_changed or gap_elapsed

            return UpgradeCheckResult(
                state="upgrade-available",
                local=local,
                latest=cached_latest,
                install_cmd=install_cmd,
                should_nudge=should_nudge,
                check_failure=failure_cause,
                check_failure_at=failure_at,
            )
    except OSError:
        # mkdir / open failure — can't even create the cache file. Bail
        # without nudging (matches pre-extraction _open_cache_fd None path).
        return UpgradeCheckResult(state="unknown", local=local, latest=None, install_cmd=None)


def record_nudge(latest_version: str, *, now: datetime | None = None) -> None:
    """Mark that we've emitted a nudge for `latest_version`. Caller invokes
    this immediately after printing the `mm: notice:` line. Best-effort.
    """
    now = now or datetime.now(timezone.utc)
    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                return
            cache = _normalize_cache(ljson.data)
            cache["last_nudged_version"] = latest_version
            cache["last_nudged_at"] = now.isoformat()
            ljson.data.clear()
            ljson.data.update(cache)
    except OSError:
        return


# ── Self-version transition detection ─────────────────────────────────────


def detect_self_version_transition(
    config: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[str, str] | None:
    """Compare __version__ against last_seen_self_version; return (old, new)
    on transition, None otherwise.

    The read+compare+write happens under a single flock on
    upgrade-state.json — eliminates the read-modify-write race two
    concurrent mm processes would have if they each checked the file
    independently.

    Within-invocation idempotency: returns None on second call within the
    same process via `_TRANSITION_DETECTED_THIS_INVOCATION`. Cross-invocation
    idempotency: the side-effect write to cache means a subsequent mm
    invocation post-upgrade no-ops because cache.last_seen_self_version
    already matches __version__.

    Short-circuits to None:
      - dev build (don't log spurious downgrade transitions when contributors
        switch between source-tree and an installed mm)
      - --no-check-version flag

    First-run path (cache absent OR last_seen_self_version is None): writes
    initial seed (current __version__), returns None (no log row).
    """
    global _TRANSITION_DETECTED_THIS_INVOCATION, _LAST_TRANSITION_SEEN

    if _TRANSITION_DETECTED_THIS_INVOCATION:
        return None
    if _INVOCATION_SKIP:
        return None
    if __version__ == DEV_BUILD_SENTINEL:
        return None

    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                return None
            cache = _normalize_cache(ljson.data)
            last_seen = cache.get("last_seen_self_version")
            # Always update the cached self-version, even on first-run / no
            # transition, so the next call has the right baseline.
            cache["last_seen_self_version"] = __version__
            ljson.data.clear()
            ljson.data.update(cache)

            if last_seen is None:
                # First-run seed — no transition logged.
                _TRANSITION_DETECTED_THIS_INVOCATION = True
                return None

            if last_seen == __version__:
                _TRANSITION_DETECTED_THIS_INVOCATION = True
                return None

            # Transition detected. Mark idempotency flag BEFORE returning so a
            # caller that re-invokes within the same process doesn't double-log.
            _TRANSITION_DETECTED_THIS_INVOCATION = True
            _LAST_TRANSITION_SEEN = (last_seen, __version__)
            return (last_seen, __version__)
    except OSError:
        return None


# ── Shared transition hook (D6: shared helper, NOT _get_config refactor) ──


def run_transition_hook(config: dict[str, Any]) -> None:
    """Single entry point for transition detection. Safe to call after any
    successful load_config(). Within-process idempotent. Silent on failure.

    Each of the 3 load_config call sites in cli.py (_get_config,
    _auto_command_setup, init_cmd) invokes this AFTER its own load_config
    succeeds. Codex outside voice (D6) caught that refactoring those three
    callers through _get_config would break the silent-on-missing-config
    contract that autopull/autopush depend on; this shared helper preserves
    each caller's distinct error policy while still centralizing the hook
    logic.
    """
    transition = detect_self_version_transition(config)
    if transition is None:
        return
    old, new = transition
    device_id = config.get("device", {}).get("id", "unknown")
    try:
        pullhistory.append_self_upgrade(device=device_id, old_version=old, new_version=new)
    except Exception:
        # Forensic log failure must not block sync. The pullhistory module
        # already swallows OSError internally; this is a defensive belt
        # against any future signature change.
        pass


# ── Nudge formatting + emission ───────────────────────────────────────────


def format_upgrade_message(local: str, latest: str, install_cmd: str) -> str:
    """Produce the one-line nudge for stderr.

    Plain str output — caller MUST emit via `print(..., file=sys.stderr)`,
    NOT `rich.console.Console.print`. Rich would interpret the backticks /
    brackets in `install_cmd` as markup. Pinned by a regression test.
    """
    return f"mm: notice: {local} → {latest} available — run `{install_cmd}`"


def emit_nudge_if_due(config: dict[str, Any] | None) -> None:
    """Run the check, print the nudge if due, record it. Silent on no-nudge.

    Call this at the TAIL of pull/push code paths (after main work) so the
    cold-cache fetch latency (~500ms 1x/24h) doesn't stack on sync latency.
    Always silent unless an upgrade is genuinely available AND the gate
    permits re-emission.
    """
    _nudge_if_due(check_for_upgrade(config))


def _nudge_if_due(result: UpgradeCheckResult, *, failed_install: bool = False) -> None:
    if result.state != "upgrade-available" or not result.should_nudge:
        return
    if result.latest is None or result.install_cmd is None:
        return
    message = format_upgrade_message(result.local, result.latest, result.install_cmd)
    if failed_install:
        message += f" (the automatic update did not complete; see {update_log_path()})"
    print(message, file=sys.stderr)
    record_nudge(result.latest)


# ── Self-update: install detection ────────────────────────────────────────


@dataclass(frozen=True)
class InstallInfo:
    """How the running mm was installed, as far as self-update is concerned.

    kind:
      "dev"          — source-tree run with no installed distribution.
      "not-pipx"     — no pipx metadata beside the interpreter (plain venv,
                       editable checkout, a stale Homebrew copy).
      "tracking"     — pipx install recorded at `INSTALL_SPEC`. The only kind
                       the automatic path touches.
      "pinned"       — pipx install of this repo at a ref other than
                       INSTALL_SPEC. A fixed ref (a release tag, which this
                       repo's release workflow never moves; any other tag,
                       fixed by convention; a commit SHA, immutable) is
                       re-resolved to the same commit by `pipx upgrade`. An
                       arbitrary branch ref, or a bare repository URL (the
                       default branch `main`, which may carry untagged WIP),
                       can move under `pipx upgrade` or `pipx upgrade-all`
                       (observed 2026-10-07 in pipx 1.17.11
                       commands/upgrade.py: `_upgrade_package` returns PINNED
                       for a `pipx pin` hold and otherwise passes
                       `parse_specifier_for_upgrade`, which keeps a Git
                       `@ref` verbatim). That hold is the separate
                       "pipx-pinned" kind, checked first by `detect_install`,
                       which pipx refuses to upgrade. The automatic path acts
                       only on the exact INSTALL_SPEC by policy. `mm update`
                       still reinstalls this kind onto the release branch.
      "pipx-pinned"  — held by `pipx pin`; pipx itself refuses to upgrade it.
      "foreign"      — pipx install from anywhere else (a fork, a local path),
                       or metadata this mm cannot read. Left alone.

    `version` is what pipx recorded, which is how a finished update is read
    back: the running process keeps its old `__version__`.
    """

    kind: str
    venv_name: str | None = None
    spec: str | None = None
    version: str | None = None
    suffix: str = ""


def _install_prefix() -> Path:
    """The running interpreter's environment root. Seam: tests point this at
    a fake pipx venv instead of rewriting `sys.prefix` for the whole process."""
    return Path(sys.prefix)


def detect_install() -> InstallInfo:
    """Classify the running install. Reads one file; writes nothing."""
    if __version__ == DEV_BUILD_SENTINEL:
        return InstallInfo("dev")
    prefix = _install_prefix()
    try:
        raw = (prefix / PIPX_METADATA_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return InstallInfo("not-pipx")
    except (OSError, UnicodeDecodeError):
        return InstallInfo("foreign", venv_name=prefix.name)
    try:
        main = json.loads(raw)["main_package"]
        name, spec, version = main["package"], main["package_or_url"], main["package_version"]
        held = main.get("pinned") is True
        suffix = main.get("suffix", "")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return InstallInfo("foreign", venv_name=prefix.name)
    if not all(isinstance(value, str) and value for value in (name, spec, version)):
        return InstallInfo("foreign", venv_name=prefix.name)
    try:
        Version(version)
    except InvalidVersion:
        return InstallInfo("foreign", venv_name=prefix.name)
    if (
        canonicalize_name(name) != PACKAGE_NAME
        or not isinstance(suffix, str)
        or prefix.parent.name != "venvs"
        or prefix.name != PACKAGE_NAME + suffix
    ):
        # mm injected into another package's venv: that venv is not ours to upgrade.
        kind = "foreign"
    elif held:
        kind = "pipx-pinned"
    elif spec == INSTALL_SPEC:
        kind = "tracking"
    elif spec == REPO_SPEC or spec.startswith(REPO_SPEC + "@"):
        kind = "pinned"
    else:
        kind = "foreign"
    return InstallInfo(kind, venv_name=prefix.name, spec=spec, version=version, suffix=suffix)


def find_pipx() -> str | None:
    """Absolute path to pipx, or None. PATH first, then the usual install dirs."""
    found = shutil.which("pipx")
    if found:
        return found
    for candidate in _PIPX_FALLBACK_PATHS:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def update_argv(pipx: str, install: InstallInfo) -> list[str]:
    """The pipx command for this install: in place when it tracks the release
    branch, the forced reinstall for a classified pin only."""
    if install.kind == "tracking" and install.venv_name:
        return [pipx, "upgrade", install.venv_name]
    if install.kind == "pinned" and install.venv_name:
        argv = [pipx, "install", "--force", INSTALL_SPEC]
        if install.suffix:
            argv.append(f"--suffix={install.suffix}")
        return argv
    raise ValueError("this install cannot be updated through pipx")


def update_log_path() -> Path:
    """Where pipx's output from the last automatic attempt is kept."""
    return CACHE_DIR / UPDATE_LOG_NAME


def reinstall_cmd(install: InstallInfo) -> str:
    """Recovery command for this environment, including its optional suffix."""
    argv = ["pipx", "install", "--force", INSTALL_SPEC]
    if install.suffix:
        argv.append(f"--suffix={install.suffix}")
    home = shlex.quote(str(_install_prefix().parent.parent))
    return f"PIPX_HOME={home} {shlex.join(argv)}"


# ── Self-update: running pipx ─────────────────────────────────────────────


@dataclass(frozen=True)
class UpdateOutcome:
    """Result of one foreground pipx run.

    status:
      "updated"    — pipx exited 0 and the recorded version or spec moved.
      "unchanged"  — pipx exited 0 and nothing moved.
      "busy"       — another installer holds the lock; this one did not start.
      "failed"     — pipx exited non-zero, timed out, or could not start.
    """

    status: str
    old: str | None
    new: str | None
    detail: str = ""
    output: str = ""
    now_tracking: bool = False


def _install_is_running() -> bool:
    """Read-only, nonblocking probe; status never creates an installer lock."""
    try:
        fd = os.open(str(CACHE_DIR / INSTALL_LOCK_NAME), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        return False
    finally:
        os.close(fd)


class _PipxBusy(OSError):
    """Installer contention is a refusal, never an installation failure."""


@contextmanager
def _pipx_install_lock() -> Iterator[int]:
    """Exclude all mm installers for the lifetime of their pipx process.

    The child inherits only this descriptor, never the mm lock. Closing the
    parent's copy leaves its flock alive in the detached child. Do not use
    LOCK_UN here: that would release the shared lock before the child exits.
    Never unlink the file, which would let contenders lock different inodes.
    """
    fsutil.refuse_real_state_write_under_pytest(CACHE_DIR / INSTALL_LOCK_NAME)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CACHE_DIR / INSTALL_LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        for delay in INSTALL_LOCK_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if delay == INSTALL_LOCK_RETRY_DELAYS[-1]:
                    raise _PipxBusy(
                        "another mm update is still running; retry after it finishes"
                    ) from error
        yield fd
    finally:
        os.close(fd)


def _refuse_under_pytest() -> None:
    """Same guard as `crypto.store_passphrase_in_keyring`: PYTEST_CURRENT_TEST
    is inherited by subprocesses, so no test layer can reach a real pipx by
    forgetting to stub. Tests replace `_run_pipx` / `_spawn_pipx` wholesale."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise OSError("refusing to run pipx under pytest")


class _PipxOutputStream:
    """Capture a terminal without forwarding its raw output to the user."""

    def __init__(self, on_output: Callable[[str], None]) -> None:
        self.on_output: Callable[[str], None] | None = on_output
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.chunks: list[str] = []
        self.eof = False
        self.reader, self.writer = pty.openpty()
        try:
            dimensions = struct.pack("HHHH", PTY_ROWS, PTY_COLUMNS, 0, 0)
            try:
                fcntl.ioctl(self.writer, termios.TIOCSWINSZ, dimensions)
            except OSError:
                pass  # Progress is still readable at the terminal's default width.
            try:
                select.select([self.reader], [], [], 0)
            except ValueError as error:
                # Past FD_SETSIZE the drain loop could not watch it mid-install;
                # find out before pipx starts and keep the pipe capture instead.
                raise OSError("progress terminal cannot be watched") from error
        except BaseException:
            self.close_writer()
            self.close_reader()
            raise

    def close_writer(self) -> None:
        if self.writer != -1:
            os.close(self.writer)
            self.writer = -1

    def close_reader(self) -> None:
        if self.reader != -1:
            os.close(self.reader)
            self.reader = -1

    def captured(self) -> str:
        return "".join(self.chunks)

    def communicate(self, proc: subprocess.Popen[str], timeout: float) -> tuple[str, None]:
        deadline = time.monotonic() + timeout
        while not self.eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout, output=self.captured())
            readable, _, _ = select.select([self.reader], [], [], min(remaining, 0.1))
            if not readable:
                continue
            try:
                data = os.read(self.reader, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""  # macOS PTYs report EOF as EIO.
            self.eof = not data
            decoded = self.decoder.decode(data, final=self.eof)
            if decoded:
                self.chunks.append(decoded)
                if self.on_output is not None:
                    self.on_output(decoded)
        try:
            proc.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            error.output = self.captured()
            raise
        return self.captured(), None


@contextmanager
def _pipx_output_stream(
    on_output: Callable[[str], None] | None,
) -> Iterator[_PipxOutputStream | None]:
    stream = None
    if on_output is not None:
        try:
            stream = _PipxOutputStream(on_output)
        except OSError:
            pass  # No free PTY: the pipe capture still installs, without live counters.
    try:
        yield stream
    finally:
        if stream is not None:
            stream.close_writer()
            stream.close_reader()


@contextmanager
def _hangup_ignored() -> Iterator[None]:
    """Keep the reader alive when the user's terminal closes mid-install.

    pipx fails once nothing reads its output: a streaming upgrade cannot write
    to its PTY, and a piped reinstall's step messages hit a closed pipe inside
    the transaction that removes the venv. The display stops drawing; mm keeps
    reading until pipx exits. A no-op handler, unlike SIG_IGN, is not inherited
    across exec, so pipx and its children keep the default disposition.
    """
    handled = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGHUP, lambda signum, frame: None) if handled else None
    try:
        yield
    finally:
        if handled:
            signal.signal(signal.SIGHUP, previous if previous is not None else signal.SIG_DFL)


def _signal_pipx_group(proc: subprocess.Popen[str], sig: int, stop_deadline: float) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        # macOS can reject an exiting child before WNOHANG sees its exit.
        # Reap within the existing stop budget; a still-live parent is a refusal.
        try:
            proc.wait(timeout=max(0, stop_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise error
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass


def _run_pipx(
    argv: list[str], *, on_output: Callable[[str], None] | None = None
) -> subprocess.CompletedProcess[str]:
    """Foreground pipx. Progress callbacks receive captured terminal chunks.

    Subprocess seam: tests monkeypatch THIS function. Without a callback the
    existing pipe capture remains unchanged, including automatic updates.
    """
    _refuse_under_pytest()
    with (
        _pipx_install_lock() as install_fd,
        _pipx_output_stream(on_output) as stream,
        _hangup_ignored(),
    ):
        # Popen can be interrupted after creating the isolated child but before
        # returning its handle. Defer launch-phase Ctrl-C until cleanup owns it.
        launch_interrupted = False
        launch_sigint = None

        def defer_interrupt(signum, frame):
            nonlocal launch_interrupted
            launch_interrupted = True

        if threading.current_thread() is threading.main_thread():
            launch_sigint = signal.getsignal(signal.SIGINT)
            if launch_sigint != signal.SIG_IGN:
                signal.signal(signal.SIGINT, defer_interrupt)
        try:
            env = _pipx_environment(argv)
            if stream is not None:
                env["COLUMNS"] = str(PTY_COLUMNS)  # pipx passes its width on to pip.
                # pipx re-raises a failed tee of pip's output after pip finishes,
                # before recording metadata; the reader decodes UTF-8 anyway.
                env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stream.writer if stream is not None else subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                env=env,
                start_new_session=True,
                pass_fds=(install_fd,),
            )
        except BaseException:
            if launch_sigint is not None:
                signal.signal(signal.SIGINT, launch_sigint)
            raise

        def communicate(timeout: float) -> tuple[str, None]:
            if stream is not None:
                return stream.communicate(proc, timeout)
            return proc.communicate(timeout=timeout)

        try:
            if stream is not None:
                stream.close_writer()  # pipx holds the only writer now; EOF needs that.
            if launch_sigint is not None:
                signal.signal(signal.SIGINT, launch_sigint)
            if launch_interrupted:
                raise KeyboardInterrupt
            output, _ = communicate(PIPX_TIMEOUT_SECONDS)
        except BaseException as error:
            if stream is not None:
                stream.on_output = None  # Cleanup captures bytes without re-entering a failed UI.
            # A second Ctrl-C must not release the mm lock with pipx still running.
            # Python dispatches signals only on the main thread.
            previous_sigint = None
            if threading.current_thread() is threading.main_thread():
                previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                # Give pipx and its pip/git/build children a bounded chance to stop
                # before escalating. Keep the mm lock throughout cleanup.
                stop_deadline = time.monotonic() + PIPX_STOP_GRACE_SECONDS
                # Reap an exited parent even when an escaped child keeps stdout open.
                # macOS can reject signaling a group containing only the zombie parent.
                finished_returncode = proc.poll()
                _signal_pipx_group(proc, signal.SIGINT, stop_deadline)
                # Any drain failure escalates like a timeout: the kill and the group
                # check below must not depend on reading the installer's output.
                try:
                    output, _ = communicate(PIPX_STOP_GRACE_SECONDS)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    proc.poll()
                    _signal_pipx_group(proc, signal.SIGKILL, stop_deadline)
                    try:
                        output, _ = communicate(PIPX_STOP_GRACE_SECONDS)
                    except (subprocess.TimeoutExpired, OSError, ValueError) as lingering:
                        # A descendant outside our group can retain the pipe. Do not
                        # let that extend cleanup indefinitely after killing pipx.
                        output = getattr(lingering, "output", None) or (
                            stream.captured() if stream is not None else ""
                        )
                        if isinstance(output, bytes):
                            output = output.decode("utf-8", errors="replace")
                        if proc.stdout is not None:
                            proc.stdout.close()
                        if stream is not None:
                            stream.close_reader()
                        proc.wait(timeout=PIPX_STOP_GRACE_SECONDS)
                # Closed pipes and a reaped parent do not prove its children stopped:
                # a child can ignore SIGINT and redirect its output. Check the group
                # independently, allowing the same grace before killing survivors.
                while True:
                    try:
                        os.killpg(proc.pid, 0)
                    except (ProcessLookupError, PermissionError):
                        break  # macOS reports a group of only zombies as EPERM.
                    if time.monotonic() >= stop_deadline:
                        _signal_pipx_group(proc, signal.SIGKILL, stop_deadline)
                        break
                    time.sleep(min(0.05, max(0, stop_deadline - time.monotonic())))
                if isinstance(error, subprocess.TimeoutExpired):
                    error.output = output
            finally:
                if previous_sigint is not None:
                    signal.signal(signal.SIGINT, previous_sigint)
            if isinstance(error, subprocess.TimeoutExpired) and finished_returncode is not None:
                # A completed installer can have a descendant retaining stdout.
                # Cleanup stays bounded; the caller still verifies its metadata.
                return subprocess.CompletedProcess(argv, finished_returncode, stdout=output)
            raise
        return subprocess.CompletedProcess(argv, proc.returncode, stdout=output)


def _pipx_environment(argv: list[str]) -> dict[str, str]:
    """Target the running venv, even when the shell selected another pipx home."""
    prefix = _install_prefix()
    if prefix.parent.name != "venvs":
        raise OSError("cannot identify this install's pipx home")
    env = _no_prompt_environment(os.environ)
    env["PIPX_HOME"] = str(prefix.parent.parent)
    if argv[1:2] == ["install"] and "--force" in argv:
        install = detect_install()
        if install.kind != "pinned":
            raise OSError("the install changed before reinstall; retry mm update")
        # pipx force-exposes apps too. A different home/bin selection must
        # never redirect a sibling install's executable to this venv.
        bin_dir = Path(env.get("PIPX_BIN_DIR") or Path.home() / ".local" / "bin").expanduser()
        bin_dir = bin_dir.resolve()
        # Self-managed pipx can infer another bin directory. Bind the one
        # checked here so --force cannot expose apps into an unchecked home.
        env["PIPX_BIN_DIR"] = str(bin_dir)
        destination = bin_dir / f"mm{install.suffix}"
        if destination.exists() or destination.is_symlink():
            if destination.resolve() != (prefix / "bin" / "mm").resolve():
                raise OSError(
                    f"pipx executable destination {destination} belongs to another install"
                )
    return env


def _spawn_pipx(argv: list[str], log_path: Path) -> None:
    """Detached pipx for the unattended hooks. Subprocess seam, as above.

    Its own session, so a hook runner tearing down the process group does not
    kill a half-finished install; output goes to the log, so the hook's pipes
    close when mm exits. Nothing waits on it.
    """
    _refuse_under_pytest()
    with _pipx_install_lock() as install_fd:
        fsutil.refuse_real_state_write_under_pytest(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, _log_header(argv).encode("utf-8"))
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(install_fd,),
                env=_pipx_environment(argv),
            )
        finally:
            os.close(fd)


def _log_header(argv: list[str]) -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"{stamp} mm {__version__}: {' '.join(argv)}\n"


def _write_update_log(argv: list[str], output: str) -> None:
    """Keep the last foreground automatic attempt's output. Best-effort."""
    try:
        path = update_log_path()
        fsutil.refuse_real_state_write_under_pytest(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as log:
            log.write(_log_header(argv) + output)
    except OSError:
        pass


def _last_line(output: str) -> str:
    """pipx's closing line, for a one-line notice. It can quote a git remote,
    so it is made terminal-safe here rather than at each print site."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return safe_terminal_str(lines[-1]) if lines else ""


_ERASED_LINE = re.compile(r"\r\x1b\[[02]?K|\x1b\[2K")


def _final_frames(output: str) -> str:
    """What a terminal capture finally showed: the last frame of each redrawn
    line, erased frames dropped, spinner backspaces applied, no remaining
    control characters."""
    lines = []
    for raw in output.split("\n"):
        line = strip_terminal_escapes(_ERASED_LINE.split(raw)[-1])
        shown: list[str] = []
        for char in line.rstrip("\r").rsplit("\r", 1)[-1]:
            if char == "\b":
                if shown:
                    shown.pop()
            elif char == "\t" or (char >= " " and char != "\x7f"):
                shown.append(char)
        lines.append("".join(shown))
    return "\n".join(lines)


class _ProgressRelay:
    """Best-effort display: its first failure turns it off, never the installer.

    KeyboardInterrupt (Ctrl-C while drawing) still propagates to pipx cleanup;
    SystemExit is a display failure, because Rich reports a closed pipe that way.
    """

    def __init__(self, on_progress: Callable[[updateprogress.UpdateProgress], None]) -> None:
        self._on_progress: Callable[[updateprogress.UpdateProgress], None] | None = on_progress
        self._parser = updateprogress.PipxProgressParser()

    def feed(self, chunk: str) -> None:
        self._relay(lambda: self._parser.feed(chunk))

    def finish(self) -> None:
        self._relay(self._parser.finish)

    def show(self, phase: str) -> None:
        self._relay(lambda: [updateprogress.UpdateProgress(phase)])

    def _relay(self, states: Callable[[], list[updateprogress.UpdateProgress]]) -> None:
        if self._on_progress is None:
            return
        try:
            for state in states():
                self._on_progress(state)
        except (Exception, SystemExit):  # Rich reports a closed pipe as SystemExit.
            self._on_progress = None


def run_update(
    install: InstallInfo,
    pipx: str,
    *,
    latest: str | None = None,
    on_progress: Callable[[updateprogress.UpdateProgress], None] | None = None,
) -> UpdateOutcome:
    """Run pipx in the foreground and read the result back from its metadata.

    Never raises for a pipx failure. The caller owns the mm lock and all
    user-facing output; this only runs the command and classifies it.
    on_progress receives measured installer progress and phase changes on a
    best-effort basis; raw output stays captured for failure diagnostics.
    Only the in-place upgrade streams: pipx removes the venv when a forced
    reinstall fails, and streaming pipx fails once nothing reads its terminal.
    """
    argv = update_argv(pipx, install)
    old = install.version
    relay = _ProgressRelay(on_progress) if on_progress is not None else None
    streamed = relay is not None and argv[1:2] == ["upgrade"]
    try:
        if streamed:
            assert relay is not None
            proc = _run_pipx(argv, on_output=relay.feed)
            relay.finish()
        else:
            if relay is not None:
                relay.show("Reinstalling")
            proc = _run_pipx(argv)
    except _PipxBusy as error:
        return UpdateOutcome("busy", old, None, str(error))
    except subprocess.TimeoutExpired as e:
        output = e.output if isinstance(e.output, str) else ""
        output = _final_frames(output) if streamed else output
        detail = f"pipx did not finish within {PIPX_TIMEOUT_SECONDS}s"
        return UpdateOutcome("failed", old, None, detail, output)
    except (OSError, ValueError) as e:
        return UpdateOutcome("failed", old, None, f"could not run pipx: {e}")
    output = proc.stdout or ""
    output = _final_frames(output) if streamed else output
    if proc.returncode != 0:
        detail = _last_line(output) or f"pipx exited {proc.returncode}"
        return UpdateOutcome("failed", old, None, detail, output)
    if relay is not None:
        relay.show("Verifying")
    after = detect_install()
    if after.kind != "tracking" or after.version is None:
        return UpdateOutcome(
            "failed", old, after.version, "could not verify the updated install", output
        )
    try:
        installed_version = Version(after.version)
        target_version = Version(latest) if latest is not None else None
    except InvalidVersion:
        return UpdateOutcome(
            "failed", old, after.version, "could not verify the installed version", output
        )
    now_tracking = install.kind != "tracking" and after.kind == "tracking"
    moved = (after.version is not None and after.version != old) or now_tracking
    if moved and target_version is not None and installed_version < target_version:
        return UpdateOutcome(
            "failed",
            old,
            after.version,
            f"pipx installed {after.version}, but {latest} is tagged; retry in a minute",
            output,
            now_tracking,
        )
    if moved:
        _record_install_problem(None)
    return UpdateOutcome(
        "updated" if moved else "unchanged",
        old,
        after.version,
        _last_line(output),
        output,
        now_tracking,
    )


# ── Self-update: the automatic path ───────────────────────────────────────


def auto_install_enabled(config: dict[str, Any] | None) -> bool:
    """`[upgrade] auto_install`, on only when absent or literally true.

    `load_config` normalizes the key; this also accepts a raw dict so a caller
    holding an unnormalized config cannot turn the feature on by accident.
    """
    upgrade_cfg = (config or {}).get("upgrade", {})
    return isinstance(upgrade_cfg, dict) and upgrade_cfg.get("auto_install", True) is True


def _attempt_state(cache: dict[str, Any], latest: str, now: datetime) -> str | None:
    """State of the automatic install attempt recorded for `latest`.

    None when no attempt targets this release (or its timestamp is in the
    future — a clock that moved back must not wedge the gate shut).
    """
    at = _parse_iso(cache.get("install_attempt_at"))
    if cache.get("install_attempt_version") != latest or at is None or at > now:
        return None
    if cache.get("install_attempt_outcome") == "failed" or now - at >= INSTALL_GRACE:
        return "failed"
    return "in-flight"


def _record_install_problem(cause: str | None, *, now: datetime | None = None) -> None:
    """Record or clear why an automatic install did not run. Best-effort.

    Never call while another `locked_json_rmw(CACHE_PATH)` block is open in the
    same process.
    """
    target_at = (now or datetime.now(timezone.utc)).isoformat() if cause is not None else None
    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                return
            cache = _normalize_cache(ljson.data)
            if (
                cache.get("install_failure_cause") == cause
                and cache.get("install_failure_at") == target_at
            ):
                ljson.write_on_exit = False
                return
            cache["install_failure_cause"] = cause
            cache["install_failure_at"] = target_at
            ljson.data.clear()
            ljson.data.update(cache)
    except Exception:
        return


def _record_check_problem(cause: str, *, now: datetime | None = None) -> None:
    """Best-effort fallback when `check_for_upgrade` raises an unexpected exception."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                return
            cache = _normalize_cache(ljson.data)
            cache["check_failure_cause"] = cause
            cache["check_failure_at"] = now_iso
            ljson.data.clear()
            ljson.data.update(cache)
    except Exception:
        return


def _claim_install_attempt(latest: str, *, now: datetime | None = None) -> str:
    """Claim the one automatic attempt for `latest` under the cache flock.

    Returns "claimed" (the caller must now run or spawn pipx), "in-flight",
    "failed" (inside the retry gap), or "unavailable" when the claim could not
    be recorded. Unrecorded means unclaimed: without the stamp every later
    pull and push would start its own pipx.
    """
    if _install_is_running():
        return "in-flight"
    now = now or datetime.now(timezone.utc)
    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                return "unavailable"
            cache = _normalize_cache(ljson.data)
            state = _attempt_state(cache, latest, now)
            at = _parse_iso(cache.get("install_attempt_at"))
            retry_due = at is not None and now - at >= DEFAULT_INSTALL_RETRY_GAP
            if state == "in-flight":
                ljson.write_on_exit = False
                return state
            if state == "failed" and not retry_due:
                if (
                    cache.get("install_attempt_outcome") is not None
                    or cache.get("install_failure_cause") is not None
                ):
                    ljson.write_on_exit = False
                else:
                    cache["install_failure_cause"] = "install-failed"
                    cache["install_failure_at"] = cache.get("install_attempt_at") or now.isoformat()
                    ljson.data.clear()
                    ljson.data.update(cache)
                return state
            cache["install_attempt_version"] = latest
            cache["install_attempt_at"] = now.isoformat()
            cache["install_attempt_outcome"] = None
            ljson.data.clear()
            ljson.data.update(cache)
        return "claimed" if ljson.write_error is None else "unavailable"
    except OSError:
        return "unavailable"


def _record_install_failed(
    latest: str, *, cause: str | None = "install-failed", now: datetime | None = None
) -> None:
    """Mark the claimed attempt failed now, without waiting out the grace."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                return
            cache = _normalize_cache(ljson.data)
            if cache.get("install_attempt_version") == latest:
                cache["install_attempt_outcome"] = "failed"
            if cause is not None:
                cache["install_failure_cause"] = cause
                cache["install_failure_at"] = now_iso
            ljson.data.clear()
            ljson.data.update(cache)
    except Exception:
        return


def update_step_label(old: str | None, new: str | None) -> str:
    """Initial transient spinner label for an installer step."""
    return safe_terminal_str(
        f"Updating mm {old} → {new}" if new is not None else f"Updating mm {old}"
    )


def updated_line(old: str | None, new: str | None) -> str:
    """Stdout outcome line after a completed mm update."""
    return f"Updated mm {old} → {new}."


@dataclass
class SyncUpdateDisplay:
    """Terminal display seam for an attended push/pull update step.

    The caller prints `outcomes` and `problems` after the sync's outcome lines,
    and `installing` must not raise for a display failure.
    """

    installing: Callable[
        [str], AbstractContextManager[Callable[[updateprogress.UpdateProgress], None] | None]
    ]
    outcomes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def update_or_nudge(
    config: dict[str, Any] | None,
    *,
    attended: bool,
    diagnostics: bool = True,
    display: SyncUpdateDisplay | None = None,
) -> None:
    """Tail seam for pull / push / autopull / autopush.

    Installs the newer release when `[upgrade] auto_install` is on and this
    install tracks the release branch; otherwise, or when that attempt failed,
    prints the same nudge `emit_nudge_if_due` does. `attended=True` runs pipx
    in the foreground and says so; `display` shows the foreground install in a
    transient spinner and collects its outcome or problem line for the caller
    to emit after the sync's outcome line; the hooks spawn it detached and
    stay silent.

    The caller has finished its sync. Nothing here may change its outcome: an
    install failure is a `mm: notice:`, never an exception or an exit code.
    """
    stage = "check"
    try:
        result = check_for_upgrade(config)
        if result.state != "upgrade-available" or result.latest is None:
            return
        stage = "install"
        try:
            handled, failed = _auto_install(
                config, result, attended=attended, diagnostics=diagnostics, display=display
            )
        except Exception as error:
            _record_install_problem(_unexpected_cause(error))
            handled, failed = False, False
        stage = "nudge"
        if not handled:
            _nudge_if_due(result, failed_install=failed)
    except KeyboardInterrupt:
        return
    except Exception as error:
        # Checking, cache I/O, progress and the fallback nudge are all optional
        # once the caller has completed its sync, including a closed stderr.
        if stage == "check":
            _record_check_problem(_unexpected_cause(error))
        elif stage == "install":
            _record_install_problem(_unexpected_cause(error))
        return


def _auto_install(
    config: dict[str, Any] | None,
    result: UpgradeCheckResult,
    *,
    attended: bool,
    diagnostics: bool = True,
    display: SyncUpdateDisplay | None = None,
) -> tuple[bool, bool]:
    """Returns (handled, failed): handled suppresses the nudge for this run;
    failed makes the nudge say the automatic update did not complete."""
    assert result.latest is not None
    if not auto_install_enabled(config):
        return False, False
    install = detect_install()
    if install.kind != "tracking":
        _record_install_problem("not-tracking")
        return False, False
    if Version(install.version or "") >= Version(result.latest):
        _record_install_problem(None)
        return True, False
    pipx = find_pipx()
    if pipx is None:
        _record_install_problem("pipx-missing")
        return False, False
    argv = update_argv(pipx, install)

    if not attended:
        # The hook still holds the mm lock here and exits right after; the
        # detached pipx outlives it, so nothing can hold the lock for it.
        claim = _claim_install_attempt(result.latest)
        if claim == "claimed":
            try:
                _spawn_pipx(argv, update_log_path())
            except _PipxBusy:
                return True, False
            except KeyboardInterrupt:
                _record_install_failed(result.latest, cause=None)
                return False, True
            except Exception:
                _record_install_failed(result.latest)
                return False, True
            _record_install_problem(None)
        return claim in ("claimed", "in-flight"), claim == "failed"

    # Attended: the caller released the mm lock. Take it back for the swap so
    # no sync starts against a half-replaced package, and take it BEFORE the
    # claim: mm lock, then the cache flock, never the reverse.
    try:
        lockfile.acquire_lock()
    except LockError:
        return True, False  # another mm is mid-sync; the next pull or push retries
    claim = None
    try:
        install = detect_install()
        if install.kind != "tracking":
            _record_install_problem("not-tracking")
            return False, False
        if Version(install.version or "") >= Version(result.latest):
            _record_install_problem(None)
            return True, False
        argv = update_argv(pipx, install)
        claim = _claim_install_attempt(result.latest)
        if claim != "claimed":
            return claim == "in-flight", claim == "failed"
        if display is None:
            shown = " ".join(["pipx", *argv[1:]])
            if diagnostics:
                print(
                    f"mm: notice: updating mm {result.local} → {result.latest} ({shown})…",
                    file=sys.stderr,
                )
            outcome = run_update(install, pipx, latest=result.latest)
        else:
            with display.installing(update_step_label(result.local, result.latest)) as on_progress:
                outcome = run_update(install, pipx, latest=result.latest, on_progress=on_progress)
    except KeyboardInterrupt:
        if claim == "claimed":
            _record_install_failed(result.latest, cause=None)
        _write_update_log(argv, "Automatic update cancelled.\n")
        cancel_line = (
            "mm: notice: automatic update cancelled; sync outcome is reported separately. "
            f"If mm is now missing, reinstall with: {reinstall_cmd(install)}"
        )
        if display is not None:
            display.problems.append(cancel_line)
        else:
            print(cancel_line, file=sys.stderr)
        return True, False
    except Exception as error:
        if claim == "claimed":
            _record_install_failed(result.latest, cause=None)
        _record_install_problem(_unexpected_cause(error))
        if display is not None and claim == "claimed":
            display.problems.append(
                f"mm: notice: automatic update to {result.latest} did not complete "
                f"({type(error).__name__}). Run: mm update"
            )
            record_nudge(result.latest)
            return True, False
        return False, claim == "claimed"
    finally:
        lockfile.release_lock()
    if outcome.status == "busy":
        return True, False
    _write_update_log(argv, outcome.output)
    if outcome.status == "updated":
        _record_install_problem(None)
        if display is not None:
            display.outcomes.append(updated_line(outcome.old, outcome.new))
        elif diagnostics:
            print(
                f"mm: notice: updated mm {outcome.old} → {outcome.new}; "
                "the next mm command runs it",
                file=sys.stderr,
            )
        return True, False
    _record_install_failed(result.latest)
    reason = outcome.detail if outcome.status == "failed" else "pipx found no newer build"
    if display is not None:
        display.problems.append(
            f"mm: notice: automatic update to {result.latest} did not complete "
            f"({reason}). Run: mm update"
        )
    else:
        print(
            f"mm: notice: automatic update to {result.latest} did not complete ({reason}) — "
            f"run `mm update`, or `{reinstall_cmd(install)}`",
            file=sys.stderr,
        )
    record_nudge(result.latest)
    return True, False


# ── Helpers ───────────────────────────────────────────────────────────────


def _parse_iso(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Re-export for tests that need to reset between cases.
def _reset_for_tests() -> None:
    """Clear within-process state. Tests call this in autouse fixtures."""
    global _TRANSITION_DETECTED_THIS_INVOCATION, _INVOCATION_SKIP, _LAST_TRANSITION_SEEN
    _TRANSITION_DETECTED_THIS_INVOCATION = False
    _INVOCATION_SKIP = False
    _LAST_TRANSITION_SEEN = None


def last_transition_seen() -> tuple[str, str] | None:
    """Return the (old, new) version tuple from the most recent transition
    detected in this process, or None on the steady-state path. Used by
    ``_run_events_tail`` to grant a one-time extended warm budget for the
    token cache after an mm upgrade."""
    return _LAST_TRANSITION_SEEN


__all__ = [
    "CACHE_DIR",
    "CACHE_PATH",
    "CHECK_FAILURE_CAUSES",
    "DEV_BUILD_SENTINEL",
    "INSTALL_CMD",
    "INSTALL_FAILURE_CAUSES",
    "INSTALL_SPEC",
    "InstallInfo",
    "REPO_URL",
    "SyncUpdateDisplay",
    "UpdateOutcome",
    "UpgradeCheckResult",
    "auto_install_enabled",
    "check_for_upgrade",
    "cached_upgrade_view",
    "detect_install",
    "detect_self_version_transition",
    "emit_nudge_if_due",
    "find_git",
    "find_pipx",
    "format_upgrade_message",
    "problem_text",
    "record_nudge",
    "reinstall_cmd",
    "run_transition_hook",
    "run_update",
    "set_invocation_skip",
    "update_argv",
    "update_log_path",
    "update_or_nudge",
    "update_step_label",
    "updated_line",
]
