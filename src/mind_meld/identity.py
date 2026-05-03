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

Cache schema (forward-compat, ``total=False`` shape):

    {
      "version": 1,
      "refreshed_at": "<ISO 8601 UTC>",
      "emails": ["a@b.c", "d@e.f"]    # lowercased, deduped, sorted
    }

Failure modes — every source independently degrades to "no emails from this
source." A subprocess crash, network failure, missing binary, or unauth state
yields an empty contribution; the cache still rebuilds with what was reachable.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mind_meld.lockedjson import locked_json_rmw

CACHE_PATH = Path("~/.config/mind-meld/identity-cache.json").expanduser()
CACHE_VERSION = 1
TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Subprocess budgets. Refresh is a one-off; these are upper bounds, not
# autopush-tight budgets.
_GH_TIMEOUT_S = 3.0
_GIT_GLOBAL_TIMEOUT_S = 2.0
_PER_REPO_TIMEOUT_S = 2.0
_PER_REPO_BUDGET_S = 5.0


def gather_local_identities(*, allow_refresh: bool = True) -> list[str]:
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
            if not _is_valid_cache(cache):
                cache.clear()
                cache.update(_default_cache())
            stale = _is_cache_stale(cache)
            if not stale or not allow_refresh:
                return list(cache.get("emails") or [])
            sys.stderr.write("mm: notice: refreshing identity cache (one-off)\n")
    except Exception:
        return []

    # Phase 2: slow subprocess gather, no flock held.
    emails = _do_full_gather()

    # Phase 3: brief write under lock. A concurrent caller may have written
    # a fresh cache while we were gathering; in that case use theirs and
    # skip our write (idempotent: same machine, same identity sources).
    return _persist_or_yield_concurrent(emails)


def refresh_identity_cache(*, force: bool = False) -> list[str]:
    """Refresh the identity cache and return the new list.

    ``force=True`` always rewrites the cache (used by ``mm init`` warm,
    ``mm refresh-identity``). ``force=False`` is a no-op when the cache is
    already fresh.

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
            if not _is_valid_cache(cache):
                cache.clear()
                cache.update(_default_cache())
            if not force and not _is_cache_stale(cache):
                return list(cache.get("emails") or [])
    except Exception:
        return []

    # Phase 2: slow subprocess gather, no flock held.
    emails = _do_full_gather()

    # Phase 3: write — but on ``force=False``, defer to a concurrent fresh
    # write if one landed. ``force=True`` always overwrites.
    if force:
        return _persist_force(emails)
    return _persist_or_yield_concurrent(emails)


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


def _is_valid_cache(cache: dict) -> bool:
    """Conservative shape check. Out-of-range version, non-list emails, or
    mistyped fields all fail and trigger a rebuild."""
    if not isinstance(cache, dict):
        return False
    if cache.get("version") != CACHE_VERSION:
        return False
    if not isinstance(cache.get("emails"), list):
        return False
    return True


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


def _do_full_gather() -> list[str]:
    """Run all four sources and return a deduped, lowercased, sorted list."""
    emails: set[str] = set()
    g = _gather_global_email()
    if g:
        emails.add(g)
    for e in _gather_per_repo_emails():
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
            timeout=_GIT_GLOBAL_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    email = result.stdout.strip().lower()
    return email or None


def _gather_per_repo_emails() -> set[str]:
    """For each discovered git root, read per-repo ``git config user.email``.

    Bounded by ``_PER_REPO_BUDGET_S`` total wall-clock and per-repo
    ``_PER_REPO_TIMEOUT_S``. Budget exhaustion returns whatever was
    collected so far.

    Crucially does NOT walk ``git log`` — only reads configured identity.
    Walking commits would pull in collaborator emails from shared repos and
    silently inflate the trust set.
    """
    try:
        from mind_meld.config import CONFIG_PATH, load_config
        from mind_meld.events import discover_git_roots
    except Exception:
        return set()

    try:
        cfg = load_config(CONFIG_PATH)
    except Exception:
        return set()

    try:
        roots, _errors = discover_git_roots(cfg if isinstance(cfg, dict) else {})
    except Exception:
        return set()

    if not roots:
        return set()

    deadline = time.monotonic() + _PER_REPO_BUDGET_S
    out: set[str] = set()
    for root in roots:
        if time.monotonic() > deadline:
            break
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "config", "user.email"],
                capture_output=True,
                text=True,
                timeout=_PER_REPO_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        email = result.stdout.strip().lower()
        if email:
            out.add(email)
    return out


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


__all__ = [
    "CACHE_PATH",
    "CACHE_VERSION",
    "TTL_SECONDS",
    "gather_local_identities",
    "refresh_identity_cache",
]
