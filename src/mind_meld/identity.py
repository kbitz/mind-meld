"""Author-email identity gathering + cached union for fleet-wide retro filtering.

The fleet-wide author-email trust set (v0.11.17) replaces the per-machine
``gather_author_emails`` filter with a fleet-wide union. Each machine emits its
locally-known emails in its ``mm-push`` event row's ``local_emails`` field; the
aggregator unions across all peers' rows to build the trust set so every peer
runs a retro against an identical filter set.

Locked design (per /plan-eng-review 2026-05-01):

* Module placement (D2): ``mind_meld.identity`` is its own module — parallels
  the v0.11.1 ``safety.py`` extraction. Aggregator keeps ``gather_author_emails``
  as a thin backwards-compat shim.
* Cache (D1): ``~/.config/mind-meld/identity-cache.json`` (mode 0600), flock-
  protected via ``lockedjson.locked_json_rmw``. Same primitive as
  ``token_usage`` and ``upgrade`` use.
* TTL: 7 days. Stale + cache-miss trigger a synchronous refresh on next read,
  preceded by a single ``mm: notice: refreshing identity cache (one-off)``
  line. Caller wears the latency once per TTL window. No background threads;
  no autopush-budget contortions — the user explicitly accepted the one-off
  slow path during the eng review.
* Init warm (D5): ``cli._run_events_backfill`` calls ``refresh_identity_cache
  (force=True)`` so the first push after ``mm init`` has a hot cache and is
  silent.
* ``[retro].author_emails`` semantics (D4): additive — config knob unions
  WITH the fleet trust set, never replaces it.

Sources gathered (union of four):

1. ``git config --global user.email`` — canonical "this user" email.
2. Per-repo ``git config user.email`` for each discovered git root —
   captures per-repo overrides where the user committed under a different
   identity for a specific project.
3. ``[retro].author_emails`` from mm ``config.toml`` — manual override list
   for historical / off-fleet identities (revoked addresses, etc.).
4. ``<id>+<login>@users.noreply.github.com`` derived from ``gh api user`` —
   GitHub web-merge / ``gh pr merge`` defaults to this form regardless of
   local git config; without it, retros lose most PR-merge attribution.

Trust-rooted: only emails sourced from CONFIGURED identities on the running
machine. Walking ``git log`` for every author email would silently include
collaborator emails on shared repos — not what we want.

Git environment: both Git reads use ``gitenv.scrubbed_git_env`` so inherited
repository and command-scoped overrides cannot change attribution. Global
and repository configuration still apply. Decode identifiers strictly: an
undecodable email is unreadable, never a replacement-character identity.

Cache schema (forward-compat, ``total=False`` shape):

    {
      "version": 2,
      "refreshed_at": "<ISO 8601 UTC>",
      "emails": ["a@b.c", "d@e.f"]    # lowercased, deduped, sorted
    }

Failure modes — every source independently degrades to "no emails from this
source." A subprocess crash, network failure, missing binary, or unauth state
yields an empty contribution; the cache still rebuilds with what was reachable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from mind_meld import gitenv, lockedjson
from mind_meld.lockedjson import locked_json_rmw

if TYPE_CHECKING:
    from mind_meld.events import GitRootDiscovery

CACHE_PATH = Path("~/.config/mind-meld/identity-cache.json").expanduser()
CACHE_VERSION = 2
TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Subprocess budgets. Refresh is a one-off; these are upper bounds, not
# autopush-tight budgets.
_GH_TIMEOUT_S = 3.0
_GIT_GLOBAL_TIMEOUT_S = 2.0
_PER_REPO_TIMEOUT_S = 2.0
_PER_REPO_BUDGET_S = 5.0
# Completeness is per thread. Two gathers run outside the flock, and a shared
# flag would let a finished gather mark a truncated one safe to persist.
_gather_tls = threading.local()


def gather_local_identities(
    *,
    allow_refresh: bool = True,
    diagnostic=None,
    root_discovery: GitRootDiscovery | None = None,
) -> list[str]:
    """Return the locally-known author emails as a sorted, lowercased list.

    Read-and-refresh contract:

    * Cache hit + fresh → return cached list (~1ms, single flock + read).
    * Cache miss / corrupt / stale AND ``allow_refresh=True`` → emit
      ``mm: notice: refreshing identity cache (one-off)`` to stderr, run
      full subprocess refresh, persist, return fresh list.
    * Cache miss / stale AND ``allow_refresh=False`` → return whatever the
      cache holds (possibly empty).

    Never raises. Pure-cache failures degrade to an empty list.

    Lock discipline (v0.11.19): the flock is held only for the brief
    read and (post-gather) write phases — NOT during ``_do_full_gather``.
    The full gather budgets up to ~10s of subprocess wall-clock
    (``_GIT_GLOBAL_TIMEOUT_S`` + ``_PER_REPO_BUDGET_S`` + ``_GH_TIMEOUT_S``);
    holding the flock across that window would block any concurrent
    autopush hook for the same duration. Release-acquire keeps the lock
    window in the millisecond range.
    """
    # Phase 1: brief read under lock. Decide whether refresh is needed.
    try:
        with locked_json_rmw(
            CACHE_PATH,
            default_factory=_default_cache,
            on_contention="block",
        ) as ljson:
            cache = ljson.data
            _normalize_cache(cache)
            stale = _is_cache_stale(cache)
            if not stale or not allow_refresh:
                return list(cache.get("emails") or [])
            try:
                message = "mm: notice: refreshing identity cache (one-off)\n"
                if diagnostic is None:
                    sys.stderr.write(message)
                else:
                    diagnostic(message, False)
            except (Exception, SystemExit):
                pass
    except Exception:
        return []

    # Phase 2: slow subprocess gather, no flock held.
    _note_gather_complete(True)
    emails = (
        _do_full_gather()
        if root_discovery is None
        else _do_full_gather(root_discovery=root_discovery)
    )

    if _identity_gather_incomplete(root_discovery):
        return _return_incomplete_union(emails)

    # Phase 3: brief write under lock. A concurrent caller may have written
    # a fresh cache while we were gathering; in that case use theirs and
    # skip our write (idempotent: same machine, same identity sources).
    return _persist_or_yield_concurrent(emails)


def refresh_identity_cache(
    *,
    force: bool = False,
    root_discovery: GitRootDiscovery | None = None,
) -> list[str]:
    """Refresh the identity cache and return the new list.

    ``force=True`` rewrites the cache (used by ``mm init`` warm,
    ``mm refresh-identity``) when discovery is complete. A supplied
    incomplete discovery returns a transient union without persisting the
    newly gathered emails or a fresh timestamp. Phase 1 may still normalize
    a well-formed older cache to the current version with ``refreshed_at=None``
    so the previous emails stay available. ``force=False`` is a no-op when
    the cache is already fresh.

    Returns the resulting email list. On failure, returns whatever was in
    the cache before (may be empty).

    Same lock discipline as ``gather_local_identities``: gather runs
    outside the flock so concurrent callers don't queue on subprocess wall-
    clock.
    """
    # Phase 1: brief read. Skip when fresh and not forced.
    try:
        with locked_json_rmw(
            CACHE_PATH,
            default_factory=_default_cache,
            on_contention="block",
        ) as ljson:
            cache = ljson.data
            _normalize_cache(cache)
            if not force and not _is_cache_stale(cache):
                return list(cache.get("emails") or [])
    except Exception:
        return []

    # Phase 2: slow subprocess gather, no flock held.
    _note_gather_complete(True)
    emails = (
        _do_full_gather()
        if root_discovery is None
        else _do_full_gather(root_discovery=root_discovery)
    )

    if _identity_gather_incomplete(root_discovery):
        return _return_incomplete_union(emails)

    # Phase 3: write — but on ``force=False``, defer to a concurrent fresh
    # write if one landed. ``force=True`` always overwrites.
    if force:
        return _persist_force(emails)
    return _persist_or_yield_concurrent(emails)


def _note_gather_complete(complete: bool) -> None:
    """Remember this thread's per-repo observation. Other gathers cannot see it."""
    _gather_tls.per_repo_complete = complete


def _gather_was_complete() -> bool:
    return getattr(_gather_tls, "per_repo_complete", True)


def _identity_gather_incomplete(root_discovery: GitRootDiscovery | None) -> bool:
    """True when this observation must not become a fresh identity cache."""
    if root_discovery is not None and root_discovery.exceeded:
        return True
    return not _gather_was_complete()


def _return_incomplete_union(emails: list[str]) -> list[str]:
    """Return current cache plus partial discovery without refreshing cache.

    A root deadline proves only that repository observation was incomplete.
    Persisting a union here would make identities removed from config or a
    repository survive a full TTL as if the observation had been complete.
    The lock gives this one caller the latest concurrent cache, while leaving
    both cache bytes and refreshed_at untouched for a later complete refresh.
    """
    try:
        with locked_json_rmw(
            CACHE_PATH,
            default_factory=_default_cache,
            on_contention="block",
        ) as ljson:
            ljson.write_on_exit = False
            cache = ljson.data
            cached = cache.get("emails") if _is_valid_cache(cache) else []
            return sorted({*emails, *(cached or [])})
    except Exception:
        return list(emails)


def _persist_or_yield_concurrent(emails: list[str]) -> list[str]:
    """Phase-3 write helper. If a concurrent writer landed a fresh cache
    while we were gathering, use theirs; otherwise persist ours."""
    try:
        with locked_json_rmw(
            CACHE_PATH,
            default_factory=_default_cache,
            on_contention="block",
        ) as ljson:
            cache = ljson.data
            if _is_valid_cache(cache) and not _is_cache_stale(cache):
                return list(cache.get("emails") or [])
            cache["version"] = CACHE_VERSION
            cache["refreshed_at"] = datetime.now(timezone.utc).isoformat()
            cache["emails"] = emails
            return list(emails)
    except Exception:
        return list(emails)


def _persist_force(emails: list[str]) -> list[str]:
    """Phase-3 write helper for ``force=True``: always overwrite."""
    try:
        with locked_json_rmw(
            CACHE_PATH,
            default_factory=_default_cache,
            on_contention="block",
        ) as ljson:
            cache = ljson.data
            cache["version"] = CACHE_VERSION
            cache["refreshed_at"] = datetime.now(timezone.utc).isoformat()
            cache["emails"] = emails
            return list(emails)
    except Exception:
        return list(emails)


# ---------------------------------------------------------------------------
# Cache predicates.
# ---------------------------------------------------------------------------


def _default_cache() -> dict:
    return {"version": CACHE_VERSION, "refreshed_at": None, "emails": []}


def _has_email_list(cache: object) -> bool:
    return (
        isinstance(cache, dict)
        and isinstance(cache.get("emails"), list)
        and all(isinstance(email, str) for email in cache["emails"])
    )


def _normalize_cache(cache: dict) -> None:
    """Malformed caches reset; old versions keep identities but become stale.

    locked_json_rmw supplies a dict even for non-dict JSON and persists on
    exit. Keeping old emails here preserves the transient union if discovery
    is incomplete; a complete gather can authoritatively replace them.
    """
    if not _has_email_list(cache):
        cache.clear()
        cache.update(_default_cache())
    elif cache.get("version") != CACHE_VERSION:
        cache["version"] = CACHE_VERSION
        cache["refreshed_at"] = None


def _is_valid_cache(cache: dict) -> bool:
    """A current-version cache containing only string emails."""
    return _has_email_list(cache) and cache.get("version") == CACHE_VERSION


def read_cached_identities(*, require_refresh: bool = False) -> list[str] | None:
    """Read the previous identities without mutation; None means unknown.

    ``require_refresh=True`` is the capture filter's baseline: a default
    cache with ``emails: []`` and no ``refreshed_at`` is not a completed
    observation, and filtering on it would drop identities the following
    refresh is about to learn.
    """
    with lockedjson.locked_json_snapshot(CACHE_PATH) as snapshot:
        if snapshot.state == "valid" and _has_email_list(snapshot.data):
            if require_refresh:
                refreshed_at = snapshot.data.get("refreshed_at")
                if not isinstance(refreshed_at, str) or not refreshed_at:
                    return None
            return sorted(snapshot.data["emails"])
    return None


def _is_cache_stale(cache: dict) -> bool:
    """True iff ``refreshed_at`` is missing, malformed, or older than
    ``TTL_SECONDS``."""
    refreshed_at = cache.get("refreshed_at")
    if not isinstance(refreshed_at, str) or not refreshed_at:
        return True
    try:
        dt = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age >= TTL_SECONDS


# ---------------------------------------------------------------------------
# Full gather — runs the four sources, unions, sorts.
# ---------------------------------------------------------------------------


def _do_full_gather(*, root_discovery: GitRootDiscovery | None = None) -> list[str]:
    """Run all four sources and return a deduped, lowercased, sorted list."""
    emails: set[str] = set()
    g = _gather_global_email()
    if g:
        emails.add(g)
    for e in _gather_per_repo_emails(root_discovery=root_discovery):
        emails.add(e)
    for e in _gather_config_author_emails():
        emails.add(e)
    n = _gather_gh_noreply_email()
    if n:
        emails.add(n)
    return sorted(emails)


def _gather_global_email() -> str | None:
    """``git config --global user.email`` or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=gitenv.scrubbed_git_env(),
            timeout=_GIT_GLOBAL_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    email = result.stdout.strip().lower()
    return email or None


def _worktree_checkouts(root: Path) -> list[Path]:
    """Every checkout of ``root``'s object store, ``[root]`` on any failure.

    Discovery elects one representative per object store, but a linked
    worktree may carry its own ``config.worktree`` email that
    ``git -C <representative> config user.email`` cannot see. Asking git
    itself keeps includeIf/worktree-config resolution effective per checkout.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=gitenv.scrubbed_git_env(),
            timeout=_PER_REPO_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return [root]
    if result.returncode != 0:
        return [root]
    checkouts: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            checkout = Path(line[len("worktree ") :].strip())
            if checkout not in checkouts:
                checkouts.append(checkout)
    return checkouts or [root]


def _read_checkout_email(checkout: Path) -> str | None:
    """``git config user.email`` for one checkout, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "config", "user.email"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=gitenv.scrubbed_git_env(),
            timeout=_PER_REPO_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    email = result.stdout.strip().lower()
    return email or None


def _gather_per_repo_emails(
    *,
    root_discovery: GitRootDiscovery | None = None,
) -> set[str]:
    """For each discovered git root, read per-repo ``git config user.email``.

    Bounded by ``_PER_REPO_BUDGET_S`` total wall-clock and per-repo
    ``_PER_REPO_TIMEOUT_S``. The representative's email is read before the
    next root's worktree list, so a slow listing cannot erase identities
    already collected. Budget exhaustion returns that partial set and marks
    this thread's gather incomplete; callers must not refresh the cache from it.

    Crucially does NOT walk ``git log`` — only reads configured identity.
    Walking commits would pull in collaborator emails from shared repos and
    silently inflate the trust set.
    """

    def finish(found: set[str], complete: bool) -> set[str]:
        _note_gather_complete(complete)
        return found

    if root_discovery is not None:
        roots, _errors = root_discovery
    else:
        try:
            from mind_meld.config import CONFIG_PATH, load_config
            from mind_meld.events import discover_git_roots
        except Exception:
            return finish(set(), False)

        try:
            cfg = load_config(CONFIG_PATH)
        except Exception:
            return finish(set(), False)

        try:
            roots, _errors = discover_git_roots(cfg if isinstance(cfg, dict) else {})
        except Exception:
            return finish(set(), False)

    if not roots:
        return finish(set(), True)

    deadline = time.monotonic() + _PER_REPO_BUDGET_S
    out: set[str] = set()
    seen_checkouts: set[str] = set()

    def take(checkout: Path) -> bool:
        if time.monotonic() > deadline:
            return False
        key = str(checkout)
        if key in seen_checkouts:
            return True
        seen_checkouts.add(key)
        email = _read_checkout_email(checkout)
        if email:
            out.add(email)
        return time.monotonic() <= deadline

    complete = True
    for root in roots:
        if not take(Path(root)):
            complete = False
            break
        if time.monotonic() > deadline:
            complete = False
            break
        for checkout in _worktree_checkouts(Path(root)):
            if not take(checkout):
                complete = False
                break
        if not complete:
            break
    return finish(out, complete)


def _gather_config_author_emails() -> list[str]:
    """``[retro].author_emails`` from mm ``config.toml`` — additive override."""
    try:
        from mind_meld.config import CONFIG_PATH, load_config

        cfg = load_config(CONFIG_PATH)
    except Exception:
        return []
    retro = cfg.get("retro") if isinstance(cfg, dict) else None
    if not isinstance(retro, dict):
        return []
    aliases = retro.get("author_emails")
    if not isinstance(aliases, list):
        return []
    out: list[str] = []
    for a in aliases:
        if isinstance(a, str) and a:
            out.append(a.lower())
    return out


def _gather_gh_noreply_email() -> str | None:
    """Derive ``<id>+<login>@users.noreply.github.com`` from local ``gh api
    user`` or None on any failure.

    Why include this: GitHub web-merge / ``gh pr merge`` defaults to this
    form regardless of local git config. Without it, retros lose most
    PR-merge attribution. The per-user ``<id>+<login>`` form is unique to
    one GitHub user, so including it in the trust set does NOT open a
    collaborator-leak hole.

    ``id`` accepted as either ``int`` (github.com canonical shape) OR
    decimal-digit ``str`` (some GitHub Enterprise instances return string-
    encoded ids when the underlying numeric value would otherwise overflow
    a JSON Number safely-representable bound). Reject everything else.
    """
    try:
        result = subprocess.run(
            ["gh", "api", "user"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    uid = _coerce_gh_uid(data.get("id"))
    login = data.get("login")
    if uid is None or not isinstance(login, str) or not login:
        return None
    return f"{uid}+{login}@users.noreply.github.com".lower()


def _coerce_gh_uid(raw: object) -> int | None:
    """Accept ``int`` or decimal-digit ``str``; reject bools and anything
    else. Returns the integer value or None."""
    if isinstance(raw, bool):
        # bool is a subclass of int — reject explicitly so a hostile
        # response can't smuggle ``True`` / ``False`` into the email form.
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.isdigit():
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _github_noreply_checked() -> bool:
    """True when a completed cache already recorded a noreply lookup."""
    with lockedjson.locked_json_snapshot(CACHE_PATH) as snapshot:
        if snapshot.state != "valid" or not isinstance(snapshot.data, dict):
            return False
        checked = snapshot.data.get("github_noreply_checked_at")
        if not isinstance(checked, str) or not checked:
            return False
        try:
            dt = datetime.fromisoformat(checked)
        except ValueError:
            return False
        if dt.tzinfo is None:
            return False
        return (datetime.now(timezone.utc) - dt).total_seconds() < TTL_SECONDS


def _record_github_noreply(email: str | None) -> None:
    """Remember one noreply lookup without pretending a full refresh ran."""
    try:
        with locked_json_rmw(
            CACHE_PATH,
            default_factory=_default_cache,
            on_contention="block",
        ) as ljson:
            cache = ljson.data
            refreshed_at = cache.get("refreshed_at")
            # A missing file becomes the default cache under the lock. Writing
            # that would invent a baseline. An old completed cache can remember
            # the check; otherwise every push retries ``gh`` until refresh lands.
            if not _is_valid_cache(cache) or not isinstance(refreshed_at, str) or not refreshed_at:
                ljson.write_on_exit = False
                return
            cache["github_noreply_checked_at"] = datetime.now(timezone.utc).isoformat()
            if email:
                current = cache.get("emails")
                emails = list(current) if isinstance(current, list) else []
                if email not in emails:
                    cache["emails"] = sorted({*emails, email})
    except Exception:
        return


def union_checked_github_noreply(emails: tuple[str, ...]) -> tuple[str, ...]:
    """Add this machine's GitHub noreply address when the walk would otherwise drop it.

    The capture filter runs before identity refresh. A completed cache that
    predates ``gh`` auth has no noreply entry, and ``git log --author`` then
    skips web-merge commits while the cursor advances. One bounded lookup
    fills that hole; a failure is remembered for the cache TTL, including
    when ``refreshed_at`` is already old, so autopush does not pay for
    ``gh`` on every push. A never-refreshed cache is left untouched. Tests
    skip the lookup: the suite isolates identity and must not call the network.
    """
    if not emails or any(email.endswith("@users.noreply.github.com") for email in emails):
        return emails
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return emails
    if _github_noreply_checked():
        return emails
    found = _gather_gh_noreply_email()
    _record_github_noreply(found)
    if not found:
        return emails
    return tuple(sorted({*emails, found}))


__all__ = [
    "CACHE_PATH",
    "CACHE_VERSION",
    "TTL_SECONDS",
    "gather_local_identities",
    "read_cached_identities",
    "refresh_identity_cache",
]
