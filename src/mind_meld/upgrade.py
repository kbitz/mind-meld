"""Self-update: check GitHub /tags for a newer release, install it through pipx
when that is safe, otherwise nudge once per 24h via stderr; log self-version
transitions to pullhistory.

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

Version source: pyproject.toml on main (raw.githubusercontent.com... actually,
NO — switched to the /tags API in eng review). The repo has tags v0.3.0..vX.Y.Z
but no GitHub Releases. We fetch /tags (public, no auth), filter to
non-prerelease semver tags, take the max via packaging.Version. Tag prefix `v`
stripped before comparison.

Cache: single JSON file at ~/.config/mind-meld/upgrade-state.json, fcntl-flocked
on every read+modify+write so transition detection is race-correct under two
concurrent mm processes. The single-file design replaced an earlier two-file
draft — Codex outside voice caught a read-modify-write race in the split design
where `self-version` had no flock domain.

Lock-order invariants (load-bearing):
  1. NEVER acquire the mm lockfile while holding upgrade-state's flock.
  2. RELEASE upgrade-state's flock BEFORE appending to pullhistory.jsonl.
  3. Transition detection runs OUTSIDE the mm lockfile by design — its
     correctness is bounded by upgrade-state's own flock. Status and push
     preview skip this hook so inspection does not consume transitions.

Visible-failure stance: this is NOT a data-at-risk signal. Network failures
degrade silently because `_check_fleet_version_or_refuse` already backstops
mixed-version drift. The nudge uses prefix `mm: notice:` (NOT `mm: warning:`)
so the warning-class reader trust stays focused on data-at-risk signals only.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from mind_meld import __version__, lockfile, pullhistory
from mind_meld.errors import LockError
from mind_meld.lockedjson import locked_json_rmw, locked_json_snapshot
from mind_meld.safety import safe_terminal_str

CACHE_DIR = Path.home() / ".config" / "mind-meld"
CACHE_PATH = CACHE_DIR / "upgrade-state.json"

# Tag-based version source. /tags returns up to 100 entries on page 1 with
# per_page=100; that's ~3 years of headroom at current release velocity.
# After 100 tags, the latest semver may not be on page 1 (GitHub /tags sort
# is unspecified); revisit then. See plan §"Pagination" for analysis.
TAGS_API_URL = "https://api.github.com/repos/kbitz/mind-meld/tags?per_page=100"
# Upgrade command tracks the moving `latest` branch (force-advanced to each
# tagged release by .github/workflows/release.yml), NOT a frozen `@vX.Y.Z` tag.
# A git `@<ref>` is a URL fragment, not a PEP508 version specifier, so pipx's
# `parse_specifier_for_upgrade` keeps it verbatim on `pipx upgrade`: a tag ref
# re-resolves to the same frozen commit forever (the historical lock — install
# stuck on one version), while a branch ref re-resolves to its advancing HEAD.
# The `--force` reinstall both lands the latest release AND rewrites a
# previously tag-pinned install's recorded URL onto `@latest`, after which a
# plain `pipx upgrade mind-meld` works. See docs/invariants/auto-upgrade.md.
REPO_SPEC = "git+https://github.com/kbitz/mind-meld.git"
INSTALL_SPEC = f"{REPO_SPEC}@latest"
INSTALL_CMD = f"pipx install --force {INSTALL_SPEC}"

DEFAULT_THROTTLE = timedelta(hours=24)
DEFAULT_NUDGE_GAP = timedelta(hours=24)
DEFAULT_FAILURE_BACKOFF = timedelta(hours=4)
HTTP_TIMEOUT_SECONDS = 10
DEV_BUILD_SENTINEL = "0.0.0+dev"

# One automatic install attempt per release per day. A newer release resets
# the gate; a failed attempt falls back to the nudge until the gap elapses.
DEFAULT_INSTALL_RETRY_GAP = timedelta(hours=24)
# A detached install reports nothing back. Inside this window an unfinished
# attempt reads as still running, so a second hook neither re-spawns pipx nor
# calls a healthy install failed. Past it, still being behind means it failed.
INSTALL_GRACE = timedelta(minutes=10)
PIPX_TIMEOUT_SECONDS = 600
PIPX_STOP_GRACE_SECONDS = 5
PIPX_METADATA_NAME = "pipx_metadata.json"
PACKAGE_NAME = "mind-meld"
UPDATE_LOG_NAME = "auto-update.log"
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
    }


def _normalize_cache(parsed: dict[str, Any]) -> dict[str, Any]:
    """Backfill missing keys onto a parsed cache (forward-compat with future
    schema additions). Pre-extraction this lived inside the read helper;
    the lockedjson helper hands us the raw parsed dict, so the backfill
    moved here."""
    base = _empty_cache()
    base.update({k: parsed.get(k, base[k]) for k in base})
    return base


# ── Tag-list HTTP adapter (testable seam) ─────────────────────────────────


def _fetch_tags(url: str = TAGS_API_URL) -> list[dict[str, Any]]:
    """Fetch the GitHub /tags response. Network adapter for testability.

    Tests monkeypatch THIS function; tests of the function itself patch
    `urllib.request.urlopen`. This split keeps `check_for_upgrade` tests
    free of HTTP details while still letting `_fetch_tags` be exercised
    against the real wire shape.

    Raises urllib.error.URLError, urllib.error.HTTPError, OSError, or
    json.JSONDecodeError. Caller catches and treats as "unknown" outcome.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"mm/{__version__}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read()
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, list):
        raise json.JSONDecodeError("expected JSON array", "", 0)
    return parsed


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
        latest = snapshot.data.get("latest_version")
        checked_at = _parse_iso(snapshot.data.get("checked_at"))
        if not isinstance(latest, str) or checked_at is None:
            return UpgradeCheckResult("unknown", local, None, None, cache_state="malformed")
        try:
            available = Version(latest) > Version(local)
        except InvalidVersion:
            return UpgradeCheckResult("unknown", local, None, None, cache_state="malformed")
        attempt = _attempt_state(snapshot.data, latest, now) if available else None
    return UpgradeCheckResult(
        "upgrade-available" if available else "current",
        local,
        latest,
        INSTALL_CMD if available else None,
        checked_at=checked_at,
        stale=now - checked_at >= DEFAULT_THROTTLE,
        cache_state="valid",
        install_attempt=attempt,
    )


def check_for_upgrade(
    config: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> UpgradeCheckResult:
    """Return whether an upgrade is available; honor cache; respect opt-outs.

    Cache is the FIRST gate — no HTTP unless cache is stale (older than
    `DEFAULT_THROTTLE`) AND last attempt is older than `DEFAULT_FAILURE_BACKOFF`.
    Network failures degrade silently (return cached state or "unknown")
    because `_check_fleet_version_or_refuse` backstops the data-at-risk case.

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

            if force or (not cache_fresh and not backoff_active):
                # Stale cache + no recent failed attempt → fetch.
                try:
                    tags = _fetch_tags()
                    picked = _pick_latest_tag(tags)
                    if picked is None:
                        # Empty array or all tags filtered. Treat as "unknown"
                        # but mark attempted_at so we don't hammer.
                        ljson.data["attempted_at"] = now.isoformat()
                        return UpgradeCheckResult(
                            state="unknown",
                            local=local,
                            latest=cached_latest,
                            install_cmd=None,
                        )
                    cached_latest = picked[0].lstrip("v")
                    ljson.data["latest_version"] = cached_latest
                    ljson.data["checked_at"] = now.isoformat()
                    ljson.data["attempted_at"] = now.isoformat()
                except (
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                    HTTPException,
                    OSError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                ):
                    # Network or parse failure: update attempted_at only, fall
                    # back to cached state. A forced check never answers from
                    # a cache it just failed to refresh.
                    ljson.data["attempted_at"] = now.isoformat()
                    if cached_latest is None or force:
                        return UpgradeCheckResult(
                            state="unknown", local=local, latest=cached_latest, install_cmd=None
                        )

            # Compare local vs cached_latest.
            if cached_latest is None:
                return UpgradeCheckResult(
                    state="unknown", local=local, latest=None, install_cmd=None
                )

            try:
                local_v = Version(local)
                latest_v = Version(cached_latest)
            except InvalidVersion:
                return UpgradeCheckResult(
                    state="unknown", local=local, latest=cached_latest, install_cmd=None
                )

            if latest_v <= local_v:
                return UpgradeCheckResult(
                    state="current", local=local, latest=cached_latest, install_cmd=None
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
      "pinned"       — pipx install of this repo at another ref, usually a
                       frozen tag. `pipx upgrade` can never move it; `mm
                       update` reinstalls it onto the release branch.
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
      "failed"     — pipx exited non-zero, timed out, or could not start.
    """

    status: str
    old: str | None
    new: str | None
    detail: str = ""
    output: str = ""
    now_tracking: bool = False


def _refuse_under_pytest() -> None:
    """Same guard as `crypto.store_passphrase_in_keyring`: PYTEST_CURRENT_TEST
    is inherited by subprocesses, so no test layer can reach a real pipx by
    forgetting to stub. Tests replace `_run_pipx` / `_spawn_pipx` wholesale."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise OSError("refusing to run pipx under pytest")


def _run_pipx(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Foreground pipx. Subprocess seam: tests monkeypatch THIS function."""
    _refuse_under_pytest()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=_pipx_environment(argv),
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=PIPX_TIMEOUT_SECONDS)
    except BaseException as error:
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
            proc.poll()
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                output, _ = proc.communicate(timeout=PIPX_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.poll()
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    output, _ = proc.communicate(timeout=PIPX_STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired as lingering:
                    # A descendant outside our group can retain the pipe. Do not
                    # let that extend cleanup indefinitely after killing pipx.
                    output = lingering.output or ""
                    if isinstance(output, bytes):
                        output = output.decode("utf-8", errors="replace")
                    if proc.stdout is not None:
                        proc.stdout.close()
                    proc.wait(timeout=PIPX_STOP_GRACE_SECONDS)
            # Closed pipes and a reaped parent do not prove its children stopped:
            # a child can ignore SIGINT and redirect its output. Check the group
            # independently, allowing the same grace before killing survivors.
            while True:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= stop_deadline:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                time.sleep(min(0.05, max(0, stop_deadline - time.monotonic())))
            if isinstance(error, subprocess.TimeoutExpired):
                error.output = output
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout=output)


def _pipx_environment(argv: list[str]) -> dict[str, str]:
    """Target the running venv, even when the shell selected another pipx home."""
    prefix = _install_prefix()
    if prefix.parent.name != "venvs":
        raise OSError("cannot identify this install's pipx home")
    env = os.environ.copy()
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


def run_update(install: InstallInfo, pipx: str, *, latest: str | None = None) -> UpdateOutcome:
    """Run pipx in the foreground and read the result back from its metadata.

    Never raises for a pipx failure. The caller owns the mm lock and all
    user-facing output; this only runs the command and classifies it.
    """
    argv = update_argv(pipx, install)
    old = install.version
    try:
        proc = _run_pipx(argv)
    except subprocess.TimeoutExpired as e:
        output = e.output if isinstance(e.output, str) else ""
        detail = f"pipx did not finish within {PIPX_TIMEOUT_SECONDS}s"
        return UpdateOutcome("failed", old, None, detail, output)
    except OSError as e:
        return UpdateOutcome("failed", old, None, f"could not run pipx: {e}")
    output = proc.stdout or ""
    if proc.returncode != 0:
        detail = _last_line(output) or f"pipx exited {proc.returncode}"
        return UpdateOutcome("failed", old, None, detail, output)
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


def _claim_install_attempt(latest: str, *, now: datetime | None = None) -> str:
    """Claim the one automatic attempt for `latest` under the cache flock.

    Returns "claimed" (the caller must now run or spawn pipx), "in-flight",
    "failed" (inside the retry gap), or "unavailable" when the claim could not
    be recorded. Unrecorded means unclaimed: without the stamp every later
    pull and push would start its own pipx.
    """
    now = now or datetime.now(timezone.utc)
    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                return "unavailable"
            cache = _normalize_cache(ljson.data)
            state = _attempt_state(cache, latest, now)
            at = _parse_iso(cache.get("install_attempt_at"))
            retry_due = at is not None and now - at >= DEFAULT_INSTALL_RETRY_GAP
            if state == "in-flight" or (state == "failed" and not retry_due):
                ljson.write_on_exit = False
                return state
            cache["install_attempt_version"] = latest
            cache["install_attempt_at"] = now.isoformat()
            cache["install_attempt_outcome"] = None
            ljson.data.clear()
            ljson.data.update(cache)
        return "claimed" if ljson.write_error is None else "unavailable"
    except OSError:
        return "unavailable"


def _record_install_failed(latest: str) -> None:
    """Mark the claimed attempt failed now, without waiting out the grace."""
    try:
        with locked_json_rmw(CACHE_PATH, default_factory=_empty_cache) as ljson:
            if not ljson.is_locked:
                return
            cache = _normalize_cache(ljson.data)
            if cache.get("install_attempt_version") == latest:
                cache["install_attempt_outcome"] = "failed"
            ljson.data.clear()
            ljson.data.update(cache)
    except OSError:
        return


def update_or_nudge(config: dict[str, Any] | None, *, attended: bool) -> None:
    """Tail seam for pull / push / autopull / autopush.

    Installs the newer release when `[upgrade] auto_install` is on and this
    install tracks the release branch; otherwise, or when that attempt failed,
    prints the same nudge `emit_nudge_if_due` does. `attended=True` runs pipx
    in the foreground and says so; the hooks spawn it detached and stay silent.

    The caller has finished its sync. Nothing here may change its outcome: an
    install failure is a `mm: notice:`, never an exception or an exit code.
    """
    try:
        result = check_for_upgrade(config)
        if result.state != "upgrade-available" or result.latest is None:
            return
        try:
            handled, failed = _auto_install(config, result, attended=attended)
        except Exception:
            handled, failed = False, False
        if not handled:
            _nudge_if_due(result, failed_install=failed)
    except (Exception, KeyboardInterrupt):
        # Checking, cache I/O, progress and the fallback nudge are all optional
        # once the caller has completed its sync, including a closed stderr.
        return


def _auto_install(
    config: dict[str, Any] | None, result: UpgradeCheckResult, *, attended: bool
) -> tuple[bool, bool]:
    """Returns (handled, failed): handled suppresses the nudge for this run;
    failed makes the nudge say the automatic update did not complete."""
    assert result.latest is not None
    if not auto_install_enabled(config):
        return False, False
    install = detect_install()
    if install.kind != "tracking":
        return False, False
    if Version(install.version or "") >= Version(result.latest):
        return True, False
    pipx = find_pipx()
    if pipx is None:
        return False, False
    argv = update_argv(pipx, install)

    if not attended:
        # The hook still holds the mm lock here and exits right after; the
        # detached pipx outlives it, so nothing can hold the lock for it.
        claim = _claim_install_attempt(result.latest)
        if claim == "claimed":
            try:
                _spawn_pipx(argv, update_log_path())
            except (Exception, KeyboardInterrupt):
                _record_install_failed(result.latest)
                return False, True
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
            return False, False
        if Version(install.version or "") >= Version(result.latest):
            return True, False
        argv = update_argv(pipx, install)
        claim = _claim_install_attempt(result.latest)
        if claim != "claimed":
            return claim == "in-flight", claim == "failed"
        shown = " ".join(["pipx", *argv[1:]])
        print(
            f"mm: notice: updating mm {result.local} → {result.latest} ({shown})…",
            file=sys.stderr,
        )
        outcome = run_update(install, pipx, latest=result.latest)
    except KeyboardInterrupt:
        if claim == "claimed":
            _record_install_failed(result.latest)
        _write_update_log(argv, "Automatic update cancelled.\n")
        print(
            "mm: notice: automatic update cancelled; sync is complete. "
            f"If mm is now missing, reinstall with: {reinstall_cmd(install)}",
            file=sys.stderr,
        )
        return True, False
    except Exception:
        if claim == "claimed":
            _record_install_failed(result.latest)
        return False, claim == "claimed"
    finally:
        lockfile.release_lock()
    _write_update_log(argv, outcome.output)
    if outcome.status == "updated":
        print(
            f"mm: notice: updated mm {outcome.old} → {outcome.new}; the next mm command runs it",
            file=sys.stderr,
        )
        return True, False
    _record_install_failed(result.latest)
    reason = outcome.detail if outcome.status == "failed" else "pipx found no newer build"
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
    "DEV_BUILD_SENTINEL",
    "INSTALL_CMD",
    "INSTALL_SPEC",
    "InstallInfo",
    "TAGS_API_URL",
    "UpdateOutcome",
    "UpgradeCheckResult",
    "auto_install_enabled",
    "check_for_upgrade",
    "cached_upgrade_view",
    "detect_install",
    "detect_self_version_transition",
    "emit_nudge_if_due",
    "find_pipx",
    "format_upgrade_message",
    "record_nudge",
    "reinstall_cmd",
    "run_transition_hook",
    "run_update",
    "set_invocation_skip",
    "update_argv",
    "update_log_path",
    "update_or_nudge",
]
