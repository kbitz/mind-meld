"""Auto-upgrade nudge: check GitHub /tags for a newer release; nudge once per
24h via stderr; log self-version transitions to pullhistory.

Approach A "nudge-only" — mm NEVER invokes pipx itself. The nudge prints the
upgrade command; the user runs it. See docs/designs/auto-upgrade.md (or the CEO
plan archive at ~/.gstack/projects/kbitz-mind-meld/ceo-plans/) for the full
rationale on why subprocess pipx is deferred.

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
     correctness is bounded by upgrade-state's own flock. This is what lets
     `mm status` and other read-only commands also detect transitions.

Visible-failure stance: this is NOT a data-at-risk signal. Network failures
degrade silently because `_check_fleet_version_or_refuse` already backstops
mixed-version drift. The nudge uses prefix `mm: notice:` (NOT `mm: warning:`)
so the warning-class reader trust stays focused on data-at-risk signals only.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from mind_meld import __version__, fsutil, pullhistory

CACHE_DIR = Path.home() / ".config" / "mind-meld"
CACHE_PATH = CACHE_DIR / "upgrade-state.json"

# Tag-based version source. /tags returns up to 100 entries on page 1 with
# per_page=100; that's ~3 years of headroom at current release velocity.
# After 100 tags, the latest semver may not be on page 1 (GitHub /tags sort
# is unspecified); revisit then. See plan §"Pagination" for analysis.
TAGS_API_URL = "https://api.github.com/repos/kbitz/mind-meld/tags?per_page=100"
INSTALL_CMD_TEMPLATE = "pipx install --force git+https://github.com/kbitz/mind-meld.git@{tag}"

DEFAULT_THROTTLE = timedelta(hours=24)
DEFAULT_NUDGE_GAP = timedelta(hours=24)
DEFAULT_FAILURE_BACKOFF = timedelta(hours=4)
HTTP_TIMEOUT_SECONDS = 10
DEV_BUILD_SENTINEL = "0.0.0+dev"

# Within-process idempotency for transition detection. Set True after the
# first invocation of `run_transition_hook` per process so two `_get_config`
# calls in one mm invocation log at most one self-upgrade row. Reset on
# interpreter exit (next mm invocation re-runs cleanly).
_TRANSITION_DETECTED_THIS_INVOCATION = False

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
    """Return value of `check_for_upgrade`.

    state:
      "skip"               — dev build, opt-out, --no-check-version, or
                             cache-fresh-and-equal. Caller does nothing.
      "current"            — local version matches latest. No nudge.
      "upgrade-available"  — caller may nudge, gated by `should_nudge`.
      "unknown"            — network or parse failure; cached state used
                             where possible. Caller does nothing.
    """

    state: str
    local: str
    latest: str | None
    install_cmd: str | None
    should_nudge: bool = False  # True only for "upgrade-available" past gate


# ── Cache I/O (single file, single flock) ─────────────────────────────────


def _empty_cache() -> dict[str, Any]:
    return {
        "latest_version": None,
        "checked_at": None,
        "attempted_at": None,
        "last_nudged_version": None,
        "last_nudged_at": None,
        "last_seen_self_version": None,
    }


def _read_cache_locked(fd: int) -> dict[str, Any]:
    """Read+parse cache from an already-flocked fd. Treat unreadable / corrupt
    JSON as empty cache (first-run-equivalent), per plan §2 spec gap fix.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 1024 * 1024)
    except OSError:
        return _empty_cache()
    if not raw:
        return _empty_cache()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _empty_cache()
    if not isinstance(parsed, dict):
        return _empty_cache()
    # Backfill missing keys (forward-compat with future schema additions).
    base = _empty_cache()
    base.update({k: parsed.get(k, base[k]) for k in base})
    return base


def _write_cache_locked(fd: int, cache: dict[str, Any]) -> None:
    """Write cache JSON to the already-flocked fd. Best-effort.

    Truncates to 0 then writes — both under the same flock so torn writes
    are not observable. Failures are swallowed; cache is forensic, never
    block sync.
    """
    data = json.dumps(cache, sort_keys=True, indent=2).encode("utf-8")
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, data)
    except OSError:
        return


def _open_cache_fd() -> int | None:
    """Open (creating if needed) the cache file with exclusive flock.

    Returns the locked fd on success, None on failure (caller must no-op).
    Caller MUST call `os.close(fd)` (which releases the flock).
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    try:
        fd = os.open(str(CACHE_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass  # best-effort on filesystems without fchmod
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _release_cache_fd(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


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


def check_for_upgrade(
    config: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
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
    if _INVOCATION_SKIP:
        return UpgradeCheckResult(state="skip", local=local, latest=None, install_cmd=None)

    # Short-circuit: config opt-out.
    if config is not None:
        upgrade_cfg = config.get("upgrade", {})
        if isinstance(upgrade_cfg, dict) and upgrade_cfg.get("auto_check") is False:
            return UpgradeCheckResult(state="skip", local=local, latest=None, install_cmd=None)

    now = now or datetime.now(timezone.utc)

    fd = _open_cache_fd()
    if fd is None:
        # Can't even open the cache file — bail without nudging.
        return UpgradeCheckResult(state="unknown", local=local, latest=None, install_cmd=None)

    try:
        cache = _read_cache_locked(fd)
        cached_latest = cache.get("latest_version")

        # Decide whether to fetch.
        checked_at = _parse_iso(cache.get("checked_at"))
        attempted_at = _parse_iso(cache.get("attempted_at"))
        cache_fresh = checked_at is not None and (now - checked_at) < DEFAULT_THROTTLE
        backoff_active = attempted_at is not None and (now - attempted_at) < DEFAULT_FAILURE_BACKOFF

        if not cache_fresh and not backoff_active:
            # Stale cache + no recent failed attempt → fetch.
            try:
                tags = _fetch_tags()
                picked = _pick_latest_tag(tags)
                if picked is None:
                    # Empty array or all tags filtered. Treat as "unknown"
                    # but mark attempted_at so we don't hammer.
                    cache["attempted_at"] = now.isoformat()
                    _write_cache_locked(fd, cache)
                    return UpgradeCheckResult(
                        state="unknown", local=local, latest=cached_latest, install_cmd=None
                    )
                cached_latest = picked[0].lstrip("v")
                cache["latest_version"] = cached_latest
                cache["checked_at"] = now.isoformat()
                cache["attempted_at"] = now.isoformat()
                _write_cache_locked(fd, cache)
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                OSError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ):
                # Network or parse failure: update attempted_at only, fall
                # back to cached state.
                cache["attempted_at"] = now.isoformat()
                _write_cache_locked(fd, cache)
                if cached_latest is None:
                    return UpgradeCheckResult(
                        state="unknown", local=local, latest=None, install_cmd=None
                    )

        # Compare local vs cached_latest.
        if cached_latest is None:
            return UpgradeCheckResult(state="unknown", local=local, latest=None, install_cmd=None)

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
        # OR last_nudged_at + 24h past.
        install_cmd = INSTALL_CMD_TEMPLATE.format(tag=f"v{cached_latest}")
        last_nudged_version = cache.get("last_nudged_version")
        last_nudged_at = _parse_iso(cache.get("last_nudged_at"))
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
    finally:
        _release_cache_fd(fd)


def record_nudge(latest_version: str, *, now: datetime | None = None) -> None:
    """Mark that we've emitted a nudge for `latest_version`. Caller invokes
    this immediately after printing the `mm: notice:` line. Best-effort.
    """
    now = now or datetime.now(timezone.utc)
    fd = _open_cache_fd()
    if fd is None:
        return
    try:
        cache = _read_cache_locked(fd)
        cache["last_nudged_version"] = latest_version
        cache["last_nudged_at"] = now.isoformat()
        _write_cache_locked(fd, cache)
    finally:
        _release_cache_fd(fd)


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
    global _TRANSITION_DETECTED_THIS_INVOCATION

    if _TRANSITION_DETECTED_THIS_INVOCATION:
        return None
    if _INVOCATION_SKIP:
        return None
    if __version__ == DEV_BUILD_SENTINEL:
        return None

    fd = _open_cache_fd()
    if fd is None:
        return None
    try:
        cache = _read_cache_locked(fd)
        last_seen = cache.get("last_seen_self_version")
        # Always update the cached self-version, even on first-run / no
        # transition, so the next call has the right baseline.
        cache["last_seen_self_version"] = __version__
        _write_cache_locked(fd, cache)

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
        return (last_seen, __version__)
    finally:
        _release_cache_fd(fd)


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
    result = check_for_upgrade(config)
    if result.state != "upgrade-available" or not result.should_nudge:
        return
    if result.latest is None or result.install_cmd is None:
        return
    print(format_upgrade_message(result.local, result.latest, result.install_cmd), file=sys.stderr)
    record_nudge(result.latest)


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
    global _TRANSITION_DETECTED_THIS_INVOCATION, _INVOCATION_SKIP
    _TRANSITION_DETECTED_THIS_INVOCATION = False
    _INVOCATION_SKIP = False


__all__ = [
    "CACHE_DIR",
    "CACHE_PATH",
    "DEV_BUILD_SENTINEL",
    "INSTALL_CMD_TEMPLATE",
    "TAGS_API_URL",
    "UpgradeCheckResult",
    "check_for_upgrade",
    "detect_self_version_transition",
    "emit_nudge_if_due",
    "format_upgrade_message",
    "record_nudge",
    "run_transition_hook",
    "set_invocation_skip",
]


# Suppress unused-import warning for fsutil — kept available for future
# atomic-write needs (e.g., a self-version split file if we ever revisit D14).
_ = fsutil
