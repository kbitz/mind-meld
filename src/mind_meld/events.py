"""Per-push event capture for fleet-aware retro (Group 7 — Track 7A).

Every push appends 1 + N + M events to a per-device daily JSONL file under
the mm-events synced source. Four event types per push:

  - mm-push           one per push, the cursor anchor for the next push
  - git-snapshot      per-repo commit metadata (deduped fleet-wide by canonical
                      remote URL + sha at retro render time)
  - sessions-snapshot per-project Claude Code session metadata
  - host-usage-snapshot  OPTIONAL (Track 19A): aggregate non-Claude host token
                      totals per (family, UTC day). All-or-nothing — the row is
                      absent entirely when any host reader came back incomplete,
                      so absence means "unknown", never zero.

The events log itself is the cursor (no separate cursor file): the most
recent `mm-push` event's `ts` answers "since when do I scan?" on the next
push. Pattern matches `pullhistory.py` (the log file is the state of truth).

Same-push upload semantics. Track 7B's wiring runs the events tail at the
HEAD of `_push_core` (BEFORE `build_manifest_v2`), so the just-written
events file IS picked up by the manifest build and uploaded as part of the
SAME push. (Pre-Track-7B prototypes ran a true tail-position write that
required next-push lag; the production wiring eliminated that lag — see
CLAUDE.md "Events tail in _push_core" for the locked invariants.)

Trust boundary. The events tail runs on every push that uploads bytes
(v0.12.2 substantive-change gate). Truly empty `mm push` invocations —
no user-source diffs, no corrupt-manifest recovery — skip the tail and
return "Nothing to push" without writing a row. Pre-v0.12.2 the tail
fired at the HEAD of `_push_core` unconditionally, which made every
empty push report a phantom "1 file uploaded" (the events file itself)
and ship that row to peers; the gate eliminates that churn while
keeping the cursor accurate (no-op pushes never advanced it anyway).

Init-time backfill. `events_tail._run_events_backfill` (moved out of cli.py in
Track 16A) runs at the end of `mm init` and writes a 30-day git-snapshot, a
full sessions-snapshot, and the optional host-usage-snapshot, but NO mm-push
event. Lets retro-fleet work immediately after init without waiting for the
first push. The aggregator dedups commits via (canonical_remote_url, sha), so
the first real push re-walking the same 30-day window is harmless. Note the
host row is NOT a 30-day slice like its siblings: the readers aggregate the
whole local corpus, so the row carries the most recent `MAX_BY_DAY_DAYS` days
regardless of the backfill window.

Schema (v=1, total=False — forward-compat readers tolerate unknown fields):
see TypedDict definitions below.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
)
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence, TypedDict
from urllib.parse import urlsplit

from mind_meld import fsutil, token_usage
from mind_meld.config import MM_INTERNAL_SOURCE_NAMES
from mind_meld.safety import strip_terminal_escapes

# ---------------------------------------------------------------------------
# Module-level named constants (C3). Track 7B imports the budget pair to
# avoid redefining the numbers on the wiring side.
# ---------------------------------------------------------------------------

INITIAL_CURSOR_LOOKBACK_DAYS = 30
WALK_TIME_BUDGET_INTERACTIVE_MS = 500
WALK_TIME_BUDGET_AUTOPUSH_MS = 250
ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS = 100
ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS = 50
MAX_GIT_WORKERS = 8
PER_REPO_TIMEOUT_FLOOR_MS = 200
PER_REPO_TIMEOUT_CAP_MS = 2000
MAX_DISCOVERY_REJECT_SAMPLE = 64
"""Cap on (reason, path) reject records retained on GitRootDiscovery.

Diag --json exposes this sample; the text renderer uses reject_counts, which
are not capped. 52 dead Claude project cwds was the live corpus on the
machine that motivated Track 29A; 64 leaves a small margin without letting
an unbounded ~/.claude/projects tree enlarge the frozen result."""

EVENTS_SCHEMA_VERSION = 2
"""Bumped from 1 → 2 for Group 8 (v0.11.0).

v=1 sessions-snapshot rows shipped by Track 7A had DELTA semantics: only
sessions whose mtime advanced since the cursor were counted. Naive sum of v=1
snapshots double-counted a single chat that was touched across pushes; latest-
only-wins undercounted by losing prior windows. Codex outside-voice review
caught the trap during /plan-eng-review for Group 8.

v=2 sessions-snapshot is FULL INVENTORY: every jsonl in the projects tree is
counted regardless of mtime. Aggregator picks the LATEST v=2 snapshot per
(device, claude_dir) — gives an accurate point-in-time sessions count for the
rendering machine's view of the fleet.

mm-push and git-snapshot rows keep delta semantics (commits since last push,
dedup-by-sha aggregator side). Only sessions-snapshot changed semantics.

Mixed-fleet transition: peers on pre-v0.11.0 still emit v=1 sessions rows.
The retro-fleet aggregator treats v=1 sessions as below-threshold and surfaces
'sessions count low for peer X — pre-v0.11.0' as part of the fleet-incomplete
breadcrumb. Numbers are honestly low, never overcounted. Once the fleet rolls
to v0.11.0, every peer emits v=2 and the count is exact."""

_GIT_LOG_FORMAT = "%x1e%H%x09%cI%x09%ae%x09%s"
"""Record-separator (\\x1e) prefix + tab-delimited fields:
SHA, commit-date (ISO 8601), author email, subject. Commit date matches
`git log --since` filter window (CT-13)."""


@dataclass(frozen=True)
class GitRootDiscovery:
    """A call-scoped, partially-successful git-root discovery result.

    ``roots`` and ``errors`` are immutable so capture and identity can share
    the same observation without a module cache. Iteration remains compatible
    with the historic ``roots, errors = discover_git_roots(config)`` form.

    Extra fields after ``exceeded`` are diagnostic-only (``mm diag``). They
    MUST stay off the mm-push wire: ``make_mm_push_event`` takes
    ``discovery_errors`` (the ``errors`` tuple) and must not be handed
    ``attribution`` or reject paths. ``attribution`` is a tuple, not a dict,
    so the frozen dataclass's immutability guarantee still holds.
    """

    roots: tuple[Path, ...]
    errors: tuple[str, ...]
    exceeded: bool = False
    attribution: tuple[tuple[str, Path], ...] = ()
    rejects: tuple[tuple[str, Path], ...] = ()
    reject_counts: tuple[tuple[str, int], ...] = ()
    probers_ran: tuple[str, ...] = ()

    def __iter__(self):
        yield list(self.roots)
        yield list(self.errors)


GIT_ROOT_DISCOVERY_BUDGET_ERROR = "git root discovery exceeded its time budget"


# ---------------------------------------------------------------------------
# v=1 schema (TypedDict). Functional form for GitCommit handles the `del`
# Python reserved word so the JSON field name stays "del" (CT-8).
# ---------------------------------------------------------------------------

GitCommit = TypedDict(
    "GitCommit",
    {
        "sha": str,
        "date": str,
        "author_email": str,
        "subject": str,
        "files": int,
        "add": int,
        "del": int,
    },
    total=False,
)


class GitSnapshotProject(TypedDict, total=False):
    remote: str
    local_path: str
    commits: list[GitCommit]


class GitSnapshotSkip(TypedDict, total=False):
    path: str
    reason: str


class GitSnapshot(TypedDict, total=False):
    v: int
    type: str
    ts: str
    device: str
    projects: list[GitSnapshotProject]
    skipped: list[GitSnapshotSkip]


class SessionMetadata(TypedDict, total=False):
    claude_dir: str
    source_root: str
    sessions: int
    total_kb: int
    last_session_at: str
    ephemeral: bool
    # Token usage (v0.11.14+, additive on v=2 schema). Day-bucketed so the
    # rendering machine slices to its retro window. Capped at 90 days at
    # write time. Subagent jsonl tokens are summed into this same bucket
    # (parent-session attribution) but do NOT bump sessions/total_kb/
    # last_session_at — those preserve their parent-only semantics.
    # See src/mind_meld/token_usage.py for the schema of each day bucket
    # ({input, cache_create, cache_read, output, by_model: {model: {...}}}).
    tokens_by_day: dict[str, dict]
    # Skill invocations (v0.11.27+, additive on v=2 schema). Same walk as
    # tokens. Shape: ``{YYYY-MM-DD: {skill_name: count}}``. Subagent
    # invocations attribute to parent project bucket. KEY-PRESENT-VALUE-
    # EMPTY semantics are load-bearing: aggregator's mixed-fleet detector
    # uses ``"skills_by_day" not in proj`` to flag the union of pre-
    # v0.11.27 peers AND v0.11.27+ peers whose skill walk was skipped
    # this push (cold token cache + autopush, or warn-mode flock
    # contention — both leave ``token_cache_files=None`` at
    # ``_scan_one_project`` so the field stays absent on the wire). An
    # empty dict means "this project had sessions but no Skill usage in
    # the captured window" — a content signal, not a version signal.
    # See docs/invariants/events-retro.md "Two populations" section
    # (semantic widened v0.12.4 post-/plan-eng-review 2026-05-10).
    skills_by_day: dict[str, dict[str, int]]


class SessionsSnapshot(TypedDict, total=False):
    v: int
    type: str
    ts: str
    device: str
    projects: list[SessionMetadata]


class MmPushEvent(TypedDict, total=False):
    v: int
    type: str
    ts: str
    device: str
    mm_version: str
    sources: list[str]
    discovery_errors: list[str]
    # local_emails (v0.11.17+, additive on v=2 schema). The locally-known
    # author-email trust set on the emitting machine. The retro-fleet
    # aggregator unions this across every peer's mm-push events to build a
    # fleet-wide filter set, replacing the pre-v0.11.17 per-machine gather
    # that produced different retros on each machine. Forward-compat:
    # absent on pre-v0.11.17 peers; aggregator falls back to local gather
    # for those rows. See mind_meld.identity.
    local_emails: list[str]


HOST_USAGE_TOKEN_SOURCES: tuple[str, ...] = ("codex", "grok", "opencode")
"""Every built-in host reader, in the fixed order ``events_tail`` invokes them.

This is the FULL set — the universe of readers that can exist. A row's
``token_sources`` is the per-push SUBSET that actually contributed, which is
what lets a consumer tell "this host reported nothing" apart from "this host
was never consulted" (not enabled as a sync source, or its store holds no usage
ledger). Do not serialize this constant into a row; see
``make_host_usage_snapshot``."""


class HostUsageSnapshot(TypedDict, total=False):
    """Aggregate non-Claude host token totals (Track 19A, additive on v=2).

    ``hosts`` is a canonical ``host_usage.HostTokens`` map,
    ``{host_family: {UTC-day: Usage}}``, structurally typed here so ``events``
    keeps no dependency on the reader module — ``host_usage`` remains the sole
    payload and model-family authority. This row carries no ``cwd``, project
    attribution, model IDs outside the canonical family buckets, or per-source
    status.

    All-or-nothing by construction: a row exists only when EVERY reader named
    in ``token_sources`` completed. ``hosts == {}`` is a real completed empty
    observation only for that row's coverage; ``token_sources == []`` means no
    reader contributed, never fleet-wide zero. An incomplete sweep omits the
    whole row (``host_usage.HostUsageResult`` draws that line; ``events_tail``
    honors it). An ABSENT row is no new complete observation — not a zero, not
    a state update, and not proof of a particular failure.

    **A day bucket is NOT "tokens spent that day", and it is NOT stable across
    snapshots.** Read this before building anything on it. Each host session
    file reports a CUMULATIVE total, and the reader attributes that whole total
    to the UTC day of the file's LAST record. So a bucket is "the lifetime
    totals of every session that last touched this machine on that day" —
    measured on a real corpus, 63 of 440 rollouts land on a different day than
    they started, and one day showed 3.4B tokens because 91 sessions' lifetimes
    collapsed onto it. Resuming an old session MOVES its entire total into the
    new day, so a fixed day's value can DECREASE between two consecutive
    snapshots.

    The only safe read is the latest ACCEPTED row per device as a whole,
    point-in-time ``as_of`` view. It replaces the prior device view; a consumer
    cannot carry forward individual sources because the payload deliberately
    merges some readers into one family. Do NOT diff snapshots, sum them, or
    treat ``active_days`` as a per-day activity series. ``active_days`` names
    the days present in ``hosts``, nothing more. The complete acceptance and
    deterministic-selection rules live in ``docs/invariants/events-retro.md``.
    """

    v: int
    type: str
    ts: str
    device: str
    token_sources: list[str]
    hosts: dict[str, dict[str, token_usage.Usage]]
    active_days: list[str]


# ---------------------------------------------------------------------------
# canonicalize_remote_url — pure function, table-tested.
# ---------------------------------------------------------------------------

# scp-form: user@host:path (no scheme, NO `//` after the colon, NO `:port`).
# Examples: git@github.com:org/repo.git, hg@bitbucket.org:org/repo
_SCP_FORM = re.compile(r"^(?P<user>[^@:/\s]+)@(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")


def canonicalize_remote_url(url: str) -> str:
    """Normalize a git remote URL to ``<host>/<path>`` form for fleet dedup.

    Strips: scheme, userinfo (user / user:password / token), port, query
    string, fragment, trailing ``.git``, leading slash. Lowercases the host
    while preserving path case. Returns "" on malformed input.

    Security (CT-10): credential-laden inputs MUST NOT survive the
    canonicalization. Tokens, x-access-token, OAuth user:password, and
    query-string ``?token=`` / ``?access_token=`` are all stripped.

    >>> canonicalize_remote_url("https://github.com/foo/bar.git")
    'github.com/foo/bar'
    >>> canonicalize_remote_url("git@github.com:foo/bar.git")
    'github.com/foo/bar'
    >>> canonicalize_remote_url("https://x-access-token:T@github.com/foo/bar.git?token=abc")
    'github.com/foo/bar'
    """
    if not isinstance(url, str):
        return ""
    s = url.strip()
    if not s:
        return ""

    # scp-form (no scheme) is special: urlsplit can't parse `git@host:path`
    # cleanly because it interprets `host:path` as `scheme:path`.
    m = _SCP_FORM.match(s)
    if m:
        host = m.group("host").lower()
        path = m.group("path").lstrip("/")
        return _strip_dot_git(f"{host}/{path}")

    # Has a scheme. Use urlsplit to peel off userinfo + port + query +
    # fragment. urlsplit needs a scheme to populate netloc correctly; if
    # the input is a bare host/path with no scheme, fall back to manual
    # parsing.
    if "://" not in s:
        # Bare `host/path` form — accept it as already-canonical-ish.
        return _strip_dot_git(s.lstrip("/"))

    parts = urlsplit(s)
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    path = (parts.path or "").lstrip("/")
    # urlsplit drops query and fragment automatically; just discard.
    return _strip_dot_git(f"{host}/{path}" if path else host)


def _strip_dot_git(s: str) -> str:
    if s.endswith(".git"):
        return s[: -len(".git")]
    if s.endswith("/"):
        return s.rstrip("/")
    return s


# ---------------------------------------------------------------------------
# discover_git_roots — multi-prober registry (A1).
# ---------------------------------------------------------------------------


def discover_git_roots(
    config: dict,
    *,
    deadline_monotonic: float | None = None,
) -> GitRootDiscovery:
    """Discover git roots with one cooperative, call-scoped deadline.

    A complete result is distinct from an incomplete empty observation:
    exceeded is true and GIT_ROOT_DISCOVERY_BUDGET_ERROR is present whenever
    the deadline cuts discovery short. The deadline is cooperative: it is
    checked before each filesystem, JSONL, and validation step, but cannot
    interrupt an individual operating-system filesystem call already running.

    Existing callers may continue to unpack the result as roots, errors.
    New callers should retain and pass the result itself so a same-invocation
    identity refresh does not rediscover roots.
    """
    if deadline_monotonic is None:
        deadline_monotonic = time.monotonic() + ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS / 1000.0

    errors: list[str] = []
    roots: list[Path] = []
    seen: set[Path] = set()
    exceeded = False
    attribution: list[tuple[str, Path]] = []
    reject_sample: list[tuple[str, Path]] = []
    reject_tally: dict[str, int] = {}
    probers_ran: list[str] = []

    def deadline_expired() -> bool:
        return time.monotonic() >= deadline_monotonic

    def mark_exceeded() -> None:
        nonlocal exceeded
        exceeded = True

    def record_reject(reason: str, path: Path) -> None:
        reject_tally[reason] = reject_tally.get(reason, 0) + 1
        if len(reject_sample) < MAX_DISCOVERY_REJECT_SAMPLE:
            reject_sample.append((reason, path))

    def validate(candidates: list[Path], source: str) -> bool:
        """Classify candidates in order. Return false if the deadline ends."""
        for candidate in candidates:
            if deadline_expired():
                mark_exceeded()
                return False
            try:
                resolved = candidate.resolve()
            except PermissionError:
                record_reject("unreadable", candidate)
                continue
            except OSError:
                record_reject("gone", candidate)
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if deadline_expired():
                mark_exceeded()
                return False
            if _classify_git_root(resolved):
                roots.append(resolved)
                attribution.append((source, resolved))
            else:
                record_reject(_reject_reason(resolved), resolved)
            if deadline_expired():
                mark_exceeded()
                return False
        return True

    # Explicit roots take precedence over automatic registries: a large stale
    # host corpus must not starve the user-configured escape hatch.
    try:
        manual_raw = (config.get("retro", {}) or {}).get("repo_roots") or []
        manual_candidates: list[Path] = []
        for raw in manual_raw:
            if deadline_expired():
                mark_exceeded()
                break
            if isinstance(raw, str):
                manual_candidates.append(Path(raw).expanduser())
        if manual_candidates:
            probers_ran.append("manual")
        if not exceeded:
            validate(manual_candidates, "manual")
    except Exception as e:
        errors.append(f"manual prober: {type(e).__name__}: {e}")

    enabled_sources = set() if exceeded or deadline_expired() else _enabled_source_names(config)
    probers: list[tuple[str, Callable[..., list[Path]]]] = []
    # Track 29A deleted `_probe_gstack`: every live `repo-mode.json` carries
    # `{mode, top_pct, authors, total, computed}` — none of `repo_root` /
    # `repo_path` / `root`. A "gstack" enabled source is now a no-op for
    # discovery; the only automatic prober is `_probe_claude`.
    if "claude" in enabled_sources:
        probers.append(("claude", _probe_claude))

    for name, prober in probers:
        if exceeded or deadline_expired():
            mark_exceeded()
            break
        try:
            candidates = prober(deadline_monotonic=deadline_monotonic)
            probers_ran.append(name)
            if not validate(candidates, name):
                break
        except Exception as e:
            if name not in probers_ran:
                probers_ran.append(name)
            errors.append(f"{name} prober: {type(e).__name__}: {e}")
            continue

    if deadline_expired():
        mark_exceeded()
    if exceeded:
        errors.append(GIT_ROOT_DISCOVERY_BUDGET_ERROR)
    return GitRootDiscovery(
        tuple(roots),
        tuple(errors),
        exceeded,
        tuple(attribution),
        tuple(reject_sample),
        tuple(sorted(reject_tally.items())),
        tuple(probers_ran),
    )


def _enabled_source_names(config: dict) -> set[str]:
    """Resolve enabled source names without importing config.get_sources
    (which has its own bootstrap + filesystem probing semantics). We just
    need to know which probers to run — a strict mismatch with config
    semantics is forensic, not data integrity.
    """
    sync_cfg = (config or {}).get("sync", {}) or {}
    explicit = sync_cfg.get("sources")
    if isinstance(explicit, list):
        names = {s.get("name") for s in explicit if isinstance(s, dict)}
    else:
        # Default sources include claude (and gstack if ~/.gstack exists, and
        # mm-events). Only "claude" has a discovery prober after Track 29A.
        names = {"claude"}
        if (Path.home() / ".gstack").exists():
            names.add("gstack")
    disabled = set(sync_cfg.get("disabled_sources") or [])
    return {n for n in names if isinstance(n, str) and n not in disabled}


def _probe_claude(*, deadline_monotonic: float | None = None) -> list[Path]:
    """Read Claude project cwd evidence until the shared deadline expires."""
    base = Path.home() / ".claude" / "projects"
    try:
        if _deadline_expired(deadline_monotonic) or not base.exists():
            return []
    except OSError:
        return []
    out: list[Path] = []
    try:
        project_dirs = iter(base.iterdir())
        while not _deadline_expired(deadline_monotonic):
            try:
                proj_dir = next(project_dirs)
            except StopIteration:
                break
            if not proj_dir.is_dir() or _deadline_expired(deadline_monotonic):
                continue
            cwd = _read_cwd_from_latest_jsonl(proj_dir, deadline_monotonic=deadline_monotonic)
            if cwd:
                out.append(Path(cwd).expanduser())
            if _deadline_expired(deadline_monotonic):
                break
    except OSError:
        return out
    return out


def _read_cwd_from_latest_jsonl(
    proj_dir: Path,
    *,
    deadline_monotonic: float | None = None,
) -> str | None:
    """Find a cwd in the newest readable JSONL before the shared deadline.

    Binary bounded-line reading keeps malformed UTF-8 and pathological lines
    local to one record. The deadline is checked before each directory, file,
    and line operation; a call already in progress remains cooperative.
    """
    if _deadline_expired(deadline_monotonic):
        return None
    jsonls: list[tuple[float, Path]] = []
    try:
        candidates = iter(proj_dir.iterdir())
        while not _deadline_expired(deadline_monotonic):
            try:
                candidate = next(candidates)
            except StopIteration:
                break
            if candidate.suffix != ".jsonl":
                continue
            try:
                jsonls.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
    except OSError:
        return None
    if _deadline_expired(deadline_monotonic):
        return None
    jsonls.sort(key=lambda pair: pair[0], reverse=True)
    for _mtime, jsonl_path in jsonls:
        if _deadline_expired(deadline_monotonic):
            return None
        try:
            with open(jsonl_path, "rb") as file_handle:
                lines = iter(
                    token_usage.iter_bounded_lines(
                        file_handle,
                        str(jsonl_path),
                        0,
                        label="session cwd reader",
                        yield_final_partial=True,
                    )
                )
                while not _deadline_expired(deadline_monotonic):
                    try:
                        raw, _end = next(lines)
                    except StopIteration:
                        break
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except ValueError:
                        continue
                    cwd = obj.get("cwd") if isinstance(obj, dict) else None
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return None


def _deadline_expired(deadline_monotonic: float | None) -> bool:
    return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic


def _classify_git_root(path: Path) -> bool:
    """Return True when ``path`` looks like a git work tree.

    Accepts a normal clone (``.git`` directory containing ``HEAD``) and a
    worktree or submodule (``.git`` file whose first line is ``gitdir:``
    pointing at an existing git dir). Wrapped in ``OSError`` because on
    Python 3.11 (the declared floor) ``Path.exists()`` / ``Path.is_dir()``
    raise ``PermissionError`` on an unreadable directory; on 3.13+ they
    return False. CI runs 3.13 only, so this cannot reproduce there.
    """
    try:
        git = path / ".git"
        if git.is_dir():
            return (git / "HEAD").is_file()
        if git.is_file():
            return _gitfile_points_at_gitdir(git)
        return False
    except OSError:
        return False


def _gitfile_points_at_gitdir(gitfile: Path) -> bool:
    """True when a ``.git`` file is a plausible gitdir pointer, not garbage.

    The ``HEAD`` / ``gitdir:`` sniff is load-bearing: a stray ``.git``
    directory or a garbage ``.git`` file would otherwise be admitted, cost a
    ~10 ms failed ``git log``, leak the file body through
    ``skipped[].reason``, and inflate ``n_repos`` so every real repo's
    subprocess timeout shrinks.
    """
    try:
        raw = gitfile.read_bytes()
    except OSError:
        return False
    line = raw.split(b"\n", 1)[0]
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.startswith("gitdir: "):
        return False
    target = text[len("gitdir: ") :].strip()
    if not target:
        return False
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = gitfile.parent / gitdir
    try:
        if gitdir.is_dir():
            return (gitdir / "HEAD").is_file()
        return False
    except OSError:
        return False


def _reject_reason(path: Path) -> str:
    """Map a classified-negative path to a diag reason class."""
    try:
        if not path.exists():
            return "gone"
        return "not-a-repo"
    except OSError:
        return "unreadable"


# ---------------------------------------------------------------------------
# walk_git_projects — bounded fan-out over `git log --since`.
# ---------------------------------------------------------------------------


def walk_git_projects(
    roots: list[Path],
    since: datetime,
    total_budget_ms: int,
) -> list[GitSnapshot]:
    """Capture commits since `since` across `roots`. Returns one
    GitSnapshot per push (a SINGLE row aggregating all projects), so the
    pull-merge set-union semantics in `merge_jsonl` continue to work
    cleanly when two devices push at the same wall-clock instant.

    Wall-time enforcement (CT-6): submit all repos to a ThreadPoolExecutor,
    then pump `as_completed(timeout=remaining)`. On TimeoutError, cancel
    pending futures (running ones can't be cancelled but are orphaned;
    process exit reaps them when mm push exits).

    Per-repo timeout (A2):
        max(PER_REPO_TIMEOUT_FLOOR_MS,
            (total_budget_ms * MAX_GIT_WORKERS) // repos_remaining)
        capped at PER_REPO_TIMEOUT_CAP_MS

    Per-repo failures fold into the row's `skipped` field (A5). Whole-walk
    failure surfaces as `mm: notice:` and we return an empty/partial row.
    """
    ts_now = datetime.now(timezone.utc).isoformat()
    snapshot: GitSnapshot = {
        "v": EVENTS_SCHEMA_VERSION,
        "type": "git-snapshot",
        "ts": ts_now,
        "device": "",  # caller fills in via write_push_event composition
    }
    projects: list[GitSnapshotProject] = []
    skipped: list[GitSnapshotSkip] = []

    if not roots:
        snapshot["projects"] = projects
        snapshot["skipped"] = skipped
        return [snapshot]

    n_repos = len(roots)
    per_repo_timeout_ms = min(
        PER_REPO_TIMEOUT_CAP_MS,
        max(
            PER_REPO_TIMEOUT_FLOOR_MS,
            (total_budget_ms * MAX_GIT_WORKERS) // n_repos,
        ),
    )

    deadline = time.monotonic() + (total_budget_ms / 1000.0)
    since_iso = since.isoformat()

    executor = ThreadPoolExecutor(max_workers=MAX_GIT_WORKERS)
    try:
        futures: dict[Future, Path] = {
            executor.submit(_walk_one_repo, root, since_iso, per_repo_timeout_ms): root
            for root in roots
        }
        # Futures already drained by the pump below. The budget-abort handler
        # MUST skip these (v0.12.16): it iterates all of `futures.items()`,
        # so without this set every repo that finished before the timeout was
        # collected a SECOND time — its full commit list serialised twice into
        # the row, then gzipped, encrypted, uploaded and replicated to every
        # peer. Measured pre-fix with 4 roots / 2 slow / 300ms: 4 project rows,
        # 2 unique. The retro card survived it only because `aggregate_git`
        # dedups on (canonical_remote, sha).
        collected: set[Future] = set()
        try:
            for fut in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
                root = futures[fut]
                collected.add(fut)
                # `except Exception` alone: CancelledError and
                # FuturesTimeoutError are both Exception subclasses, so naming
                # them added nothing. NOTE two near-misses worth keeping in
                # mind before touching this clause — concurrent.futures.
                # CancelledError is NOT asyncio.CancelledError (that one is
                # BaseException-derived and would escape), and
                # FuturesTimeoutError aliases builtin TimeoutError, which is an
                # OSError subclass, so never narrow this to OSError.
                try:
                    proj, err = fut.result(timeout=0)
                except Exception as e:
                    skipped.append({"path": str(root), "reason": f"{type(e).__name__}: {e}"})
                    continue
                if err:
                    skipped.append({"path": str(root), "reason": err})
                if proj is not None:
                    projects.append(proj)
        except FuturesTimeoutError:
            # Budget exhausted at the PUMP (`as_completed` itself raised, from
            # the for-statement rather than the loop body — which is why this
            # handler is separate from the per-future one above and why the two
            # blocks are not interchangeable: only this one marks budget_abort).
            # Cancel pending; collect what's done and not already drained.
            for fut, root in futures.items():
                if fut in collected:
                    continue
                if fut.done():
                    try:
                        proj, err = fut.result(timeout=0)
                    except Exception as e:
                        skipped.append({"path": str(root), "reason": f"{type(e).__name__}: {e}"})
                        continue
                    if err:
                        skipped.append({"path": str(root), "reason": err})
                    if proj is not None:
                        projects.append(proj)
                else:
                    fut.cancel()
                    skipped.append({"path": str(root), "reason": "budget_abort"})
    except Exception as e:
        sys.stderr.write(
            "mm: notice: walk_git_projects whole-walk failure "
            f"({type(e).__name__}: {strip_terminal_escapes(str(e))})\n"
        )
    finally:
        # Don't wait for orphaned subprocesses: they'll be killed on process
        # exit. shutdown(wait=False) lets us return promptly even if a
        # future is still running.
        executor.shutdown(wait=False, cancel_futures=True)

    snapshot["projects"] = projects
    snapshot["skipped"] = skipped
    return [snapshot]


def _walk_one_repo(
    root: Path,
    since_iso: str,
    timeout_ms: int,
) -> tuple[GitSnapshotProject | None, str | None]:
    """Run `git log --since --numstat -M -C` and parse output. Returns
    (project, error). On any failure, project is None and error is set."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                f"--since={since_iso}",
                "--numstat",
                "-M",
                "-C",
                f"--format={_GIT_LOG_FORMAT}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000.0,
        )
    except subprocess.TimeoutExpired:
        return None, "TimeoutExpired"
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    if result.returncode != 0:
        return None, f"git log rc={result.returncode}: {result.stderr.strip()[:200]}"

    remote = _origin_remote_url(root)
    commits = _parse_git_log_numstat(result.stdout)
    proj: GitSnapshotProject = {
        "remote": canonicalize_remote_url(remote) if remote else "",
        "local_path": str(root),
        "commits": commits,
    }
    return proj, None


def _origin_remote_url(root: Path) -> str:
    """Return the `origin` remote URL or empty string on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _parse_git_log_numstat(out: str) -> list[GitCommit]:
    """Parse `git log --format=<RS>%H\\t%cI\\t%ae\\t%s --numstat` output.

    Records are RS-prefixed (\\x1e). Numstat rows follow each format line:
      <add>\\t<del>\\t<path>          # text file
      -\\t-\\t<path>                  # binary file (treated as 0/0)
      <add>\\t<del>\\t<old> => <new>  # rename (preserve <new>)

    Empty/merge commits emit a format line with no numstat rows; recorded
    as files=0, add=0, del=0.
    """
    commits: list[GitCommit] = []
    if not out:
        return commits
    # Split on the record separator. First chunk before the first \x1e is
    # empty (or whitespace) since the format string LEADS with \x1e.
    chunks = out.split("\x1e")
    for chunk in chunks:
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        header = lines[0]
        try:
            sha, date, ae, subject = header.split("\t", 3)
        except ValueError:
            continue
        files = 0
        add = 0
        rem = 0
        for ln in lines[1:]:
            if not ln.strip():
                continue
            parts = ln.split("\t", 2)
            if len(parts) < 3:
                continue
            a, d, _ = parts
            files += 1
            if a == "-":
                a = "0"
            if d == "-":
                d = "0"
            try:
                add += int(a)
                rem += int(d)
            except ValueError:
                continue
        commit: GitCommit = {
            "sha": sha,
            "date": date,
            "author_email": ae,
            "subject": subject,
            "files": files,
            "add": add,
            "del": rem,
        }
        commits.append(commit)
    return commits


# ---------------------------------------------------------------------------
# walk_session_metadata — Conductor-aware 2-level scandir walk.
# ---------------------------------------------------------------------------

_CONDUCTOR_PATTERN = re.compile(r"/conductor/workspaces/")


def walk_session_metadata(
    claude_dir: Path,
    since: datetime,  # noqa: ARG001 — kept for API stability; v=2 ignores it (see EVENTS_SCHEMA_VERSION docstring)
    *,
    deadline_monotonic: float | None = None,
    token_cache_files: dict | None = None,
) -> list[SessionsSnapshot]:
    """Walk ``<claude_dir>/projects/<encoded>/*.jsonl`` aggregating per-project
    session metadata. Returns one SessionsSnapshot row aggregating all
    projects (single row mirrors walk_git_projects' shape).

    v=2 FULL INVENTORY (Group 8). Every jsonl is counted regardless of mtime.
    The ``since`` parameter is retained for API stability but ignored — the
    aggregator picks the LATEST snapshot per (device, claude_dir) so a snap-
    shot's value is its current point-in-time view of the local sessions, not
    a delta against the prior cursor. See EVENTS_SCHEMA_VERSION docstring for
    the cross-model-review rationale that drove this change.

    `ephemeral: True` when the decoded project path matches
    `*/conductor/workspaces/*` — matched on the decoded path string, NOT
    on path existence (Conductor workspaces are routinely destroyed).

    Performance target: <500ms for 10k files via os.scandir 2-level walk.

    Codex C4: ``_read_cwd_from_latest_jsonl`` reads jsonl files line-by-line
    until a ``cwd`` field appears, so a single pathological project (no cwd
    anywhere, large jsonls) can blow the wall-clock budget. The optional
    ``deadline_monotonic`` (a ``time.monotonic()`` value) is checked at the
    top of each project iteration and aborts the scandir loop. Track 7B's
    wiring side passes the same deadline shared with walk_git_projects.

    Token aggregation (v0.11.14+): when ``token_cache_files`` is provided —
    the ``files`` sub-dict of a locked ``token_usage`` cache — each scanned
    project's ``tokens_by_day`` is populated by summing parent + subagent
    jsonl token usage. When ``None``, no token data is added (used by tests
    that don't exercise the token path, and by autopush when the cache is
    cold).
    """
    ts_now = datetime.now(timezone.utc).isoformat()
    snapshot: SessionsSnapshot = {
        "v": EVENTS_SCHEMA_VERSION,
        "type": "sessions-snapshot",
        "ts": ts_now,
        "device": "",
        "projects": [],
    }
    projects_root = claude_dir / "projects"
    if not projects_root.is_dir():
        return [snapshot]

    source_root = str(claude_dir)
    out: list[SessionMetadata] = []
    try:
        with os.scandir(projects_root) as proj_iter:
            for proj_entry in proj_iter:
                if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
                    break
                if not proj_entry.is_dir(follow_symlinks=False):
                    continue
                meta = _scan_one_project(
                    proj_entry,
                    source_root=source_root,
                    token_cache_files=token_cache_files,
                    deadline_monotonic=deadline_monotonic,
                )
                if meta is not None:
                    out.append(meta)
    except OSError:
        return [snapshot]

    snapshot["projects"] = out
    return [snapshot]


def _scan_one_project(
    proj_entry: os.DirEntry,
    *,
    source_root: str,
    token_cache_files: dict | None = None,
    deadline_monotonic: float | None = None,
) -> SessionMetadata | None:
    """One project dir → SessionMetadata. Returns None if the dir has no
    qualifying jsonl files.

    v=2 full-inventory: counts every .jsonl regardless of mtime.

    ``source_root`` is the str of the parent claude_dir passed to
    ``walk_session_metadata`` — distinguishes (device, claude_dir) tuples
    that share an encoded project name across two configured ``type:
    claude`` source roots. The aggregator keys on ``(device, source_root,
    claude_dir)`` to avoid silent overwrite on encoded-name collision.

    Token aggregation (v0.11.14+): when ``token_cache_files`` is provided,
    parent + subagent jsonls are walked into the per-jsonl cache and the
    resulting per-day buckets are merged into ``tokens_by_day``. Subagent
    jsonls live one level deeper at ``<session-uuid>/subagents/*.jsonl``;
    they contribute to ``tokens_by_day`` ONLY (not ``sessions``,
    ``total_kb``, or ``last_session_at`` — those preserve parent-only
    semantics).

    Skill aggregation (v0.11.27+): same walk also populates
    ``skills_by_day`` from each assistant ``tool_use`` block with
    ``name == "Skill"``. Subagent skill invocations roll into the
    parent project's bucket via the same attribution rule. Empty dict
    (project has sessions but no Skill blocks) is preserved as a KEY-
    PRESENT-VALUE-EMPTY signal — content, not version. KEY-ABSENT (the
    branch below where ``token_cache_files is None``) covers two
    populations: pre-v0.11.27 peers AND v0.11.27+ peers whose skill
    walk was skipped this push. The aggregator's ``pre_skills_peers``
    discriminator flags both. See docs/invariants/events-retro.md
    (D4 from /plan-eng-review 2026-05-06; widened 2026-05-10)."""
    sessions = 0
    total_bytes = 0
    last_mtime = 0.0
    parent_jsonls: list[Path] = []
    subagent_jsonls: list[Path] = []
    try:
        with os.scandir(proj_entry.path) as f_iter:
            for f_entry in f_iter:
                if f_entry.name.endswith(".jsonl") and f_entry.is_file(follow_symlinks=False):
                    try:
                        st = f_entry.stat()
                    except OSError:
                        continue
                    sessions += 1
                    total_bytes += st.st_size
                    if st.st_mtime > last_mtime:
                        last_mtime = st.st_mtime
                    parent_jsonls.append(Path(f_entry.path))
                elif f_entry.is_dir(follow_symlinks=False):
                    # Look for <session-uuid>/subagents/*.jsonl one level deeper.
                    subagent_jsonls.extend(_collect_subagent_jsonls(Path(f_entry.path)))
    except OSError:
        return None
    if sessions == 0:
        return None
    # EXACTLY ONE cwd scan per project (v0.12.16). This call used to sit
    # INSIDE the loop above, guarded by `if cwd is None` and commented
    # "first one wins". That comment held only when a cwd was actually
    # found: the helper takes the PROJECT dir, not the file, so when no
    # jsonl in the project carries a `cwd` the guard never flipped and the
    # helper re-scanned the whole directory once per jsonl — iterdir +
    # N stat + a full read of every file, N times over. Measured before
    # the hoist: 20 jsonls -> 20 calls -> 400 file opens; 15ms at N=10,
    # 1.44s at N=100, 13.2s at N=300 for a SINGLE project, against a
    # 250ms autopush budget. Keep this call out of any per-file loop.
    cwd = _read_cwd_from_latest_jsonl(Path(proj_entry.path))
    decoded_path = cwd or proj_entry.name  # fallback to encoded name
    last_iso = datetime.fromtimestamp(last_mtime, tz=timezone.utc).isoformat() if last_mtime else ""
    meta: SessionMetadata = {
        "claude_dir": proj_entry.name,
        "source_root": source_root,
        "sessions": sessions,
        "total_kb": total_bytes // 1024,
        "last_session_at": last_iso,
        "ephemeral": bool(_CONDUCTOR_PATTERN.search(decoded_path)),
    }
    if token_cache_files is not None:
        tokens_by_day, skills_by_day = _aggregate_jsonl_views_for_project(
            parent_jsonls + subagent_jsonls,
            token_cache_files,
            deadline_monotonic=deadline_monotonic,
        )
        meta["tokens_by_day"] = tokens_by_day
        # KEY-PRESENT-VALUE-EMPTY is the load-bearing D4 content
        # signal: an empty dict here means "we walked, no Skill blocks
        # found." When the gate above is False (cold token cache +
        # autopush, or warn-mode flock contention), we deliberately
        # leave the key absent — DO NOT add a synthetic ``meta[
        # "skills_by_day"] = {}`` else branch. Latest-snapshot-wins at
        # aggregator.py:aggregate_sessions would silently overwrite a
        # warm T1 snapshot's populated skills with a cold T2 ``{}``.
        # The aggregator's ``pre_skills_peers`` flags absent-on-wire
        # for both pre-v0.11.27 and skipped-walk peers; breadcrumb at
        # ``aggregator.py:format_retro`` admits the ambiguity. See
        # docs/invariants/events-retro.md "Why not always set" section.
        meta["skills_by_day"] = skills_by_day
    return meta


def _collect_subagent_jsonls(session_dir: Path) -> list[Path]:
    """Yield jsonls under ``<session_dir>/subagents/``. Returns empty list
    if no such dir or on any I/O failure."""
    sub = session_dir / "subagents"
    if not sub.is_dir():
        return []
    out: list[Path] = []
    try:
        with os.scandir(sub) as it:
            for entry in it:
                if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                    out.append(Path(entry.path))
    except OSError:
        return []
    return out


def _aggregate_jsonl_views_for_project(
    jsonls: list[Path],
    token_cache_files: dict,
    *,
    deadline_monotonic: float | None,
) -> tuple[dict[str, dict], dict[str, dict[str, int]]]:
    """Walk each jsonl through the per-jsonl cache and merge per-day
    views. Returns ``(tokens_by_day, skills_by_day)`` — both keyed by
    ``YYYY-MM-DD``.

    Subagent jsonls are passed in flat alongside parent jsonls; both
    contribute to the same project's tokens AND skills (parent-project
    attribution, mirroring the existing token rule)."""
    merged_tokens: dict[str, dict] = {}
    merged_skills: dict[str, dict[str, int]] = {}
    for jl in jsonls:
        by_day, skills_by_day = token_usage.get_or_compute(
            jl,
            token_cache_files,
            deadline_monotonic=deadline_monotonic,
        )
        token_usage.merge_token_days(merged_tokens, by_day)
        token_usage.merge_skill_days(merged_skills, skills_by_day)
    return merged_tokens, merged_skills


# ---------------------------------------------------------------------------
# Cursor (derived) + event writer.
# ---------------------------------------------------------------------------


def last_push_ts(events_dir: Path, device_id: str) -> datetime:
    """Return the ts of the most recent ``mm-push`` event for this device.

    Reverse-scans up to ``INITIAL_CURSOR_LOOKBACK_DAYS`` daily files for the
    given device. On first run / events absent / no mm-push found, returns
    ``now - INITIAL_CURSOR_LOOKBACK_DAYS``.

    Pattern matches pullhistory.py: the log file IS the state of truth. No
    separate cursor file means no separate flock domain (A3 + A4
    elimination)."""
    default = datetime.now(timezone.utc) - timedelta(days=INITIAL_CURSOR_LOOKBACK_DAYS)
    if not events_dir.is_dir():
        return default
    today = datetime.now(timezone.utc).date()
    for delta in range(0, INITIAL_CURSOR_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=delta)
        path = events_dir / f"{device_id}-{day.isoformat()}.jsonl"
        if not path.is_file():
            continue
        ts = _last_mm_push_ts(path)
        if ts is not None:
            return ts
    return default


def _last_mm_push_ts(path: Path) -> datetime | None:
    """Read `path` and return the ts of the LAST `{"type":"mm-push", ...}`
    line, or None if no such line exists. Reads forward (small daily files),
    keeping last-match semantics so the most recent push wins.

    BINARY and BOUNDED (v0.12.16), same rationale as
    ``_read_cwd_from_latest_jsonl``. This file lives under the SYNCED
    mm-events source, so its bytes can arrive via the pull apply path and
    ``merge.merge_jsonl`` rather than only from this device's own writer —
    which is exactly why it goes through ``token_usage.iter_bounded_lines``
    rather than a bare ``for raw in f``. The latter lets Python extend its
    buffer to newline-or-EOF, so one oversized line from a corrupt or
    hostile peer file would be slurped whole on every push.

    Returning ``None`` here is NOT a benign fallback: it rewinds the cursor
    to ``now - INITIAL_CURSOR_LOOKBACK_DAYS`` and re-walks 30 days of git
    history on every subsequent push, forever. Per-line tolerance keeps a
    single bad byte from costing the cursor."""
    last: datetime | None = None
    try:
        with open(path, "rb") as f:
            for raw, _end in token_usage.iter_bounded_lines(
                f,
                str(path),
                0,
                label="events cursor reader",
                yield_final_partial=True,  # one-shot read, see the cwd reader
            ):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    # Malformed JSON and invalid utf-8 are both ValueError.
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "mm-push":
                    continue
                ts_s = obj.get("ts")
                if not isinstance(ts_s, str):
                    continue
                try:
                    last = datetime.fromisoformat(ts_s)
                except ValueError:
                    continue
    except OSError:
        return None
    return last


def write_push_event(
    events_dir: Path,
    device_id: str,
    events: list[dict],
) -> None:
    """Append `events` to today's per-device JSONL.

    Order invariant (CT-4): caller MUST construct `events` with the
    ``mm-push`` event LAST. Partial write before the mm-push appends →
    cursor doesn't advance → next push re-walks the range (deduped at
    retro render via canonical (remote, sha)). The single flock window is
    best-effort batching, NOT transactionality.

    File mode 0o600. Per-day naming: ``events/<device>-<YYYY-MM-DD>.jsonl``.
    """
    if not events:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    safe_device = _safe_device_filename(device_id)
    path = events_dir / f"{safe_device}-{today}.jsonl"
    lines = [json.dumps(e, sort_keys=True).encode("utf-8") for e in events]
    fsutil.flock_append_jsonl(path, lines, mode=0o600)


_DEVICE_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_device_filename(device_id: str) -> str:
    """Conservative filename sanitizer for the per-device daily file name.
    The device_id is normally a UUID (already filename-safe), but defending
    against future schemes lets us avoid surprises if device_id becomes
    user-controlled."""
    out = _DEVICE_FILENAME_SAFE.sub("_", device_id)
    return out or "unknown"


def make_mm_push_event(
    *,
    device: str,
    mm_version: str,
    sources: list[str] | None = None,
    discovery_errors: list[str] | None = None,
    local_emails: list[str] | None = None,
    ts: datetime | None = None,
) -> MmPushEvent:
    """Construct an mm-push event row. Caller appends as the LAST element
    of the events list passed to write_push_event (CT-4).

    ``sources`` is the list of resolved source NAMES (not dicts) that
    participated in this push. ``MM_INTERNAL_SOURCE_NAMES`` are filtered
    out: ``mm-events`` is mm-owned infrastructure, not user-meaningful
    fleet activity, so it shouldn't show up in the retro skill's source
    enumeration (Codex C7). Group 8's retro skill enumerates per-source
    content stats from the synced manifest at retro time (D1D), so this
    field stays a names-only list (Codex C2: ``iter_source_diffs(skip_un
    changed=True)`` makes per-source counts unreliable on the no-content
    push path).

    ``local_emails`` (v0.11.17+) is the locally-known author-email trust
    set, emitted so the retro-fleet aggregator can union across peers and
    produce identical retros on every machine. Pass ``None`` (or omit) to
    keep the field off the event row entirely — pre-v0.11.17 callers and
    cold-cache emitters get the same wire shape as before. An empty list
    is emitted as ``"local_emails": []`` (explicit "machine had nothing to
    contribute," distinguishable from "pre-v0.11.17 peer")."""
    filtered = [s for s in (sources or []) if s not in MM_INTERNAL_SOURCE_NAMES]
    event: MmPushEvent = {
        "v": EVENTS_SCHEMA_VERSION,
        "type": "mm-push",
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "device": device,
        "mm_version": mm_version,
        "sources": filtered,
        "discovery_errors": discovery_errors or [],
    }
    if local_emails is not None:
        event["local_emails"] = list(local_emails)
    return event


def make_host_usage_snapshot(
    *,
    device: str,
    hosts: dict[str, dict[str, token_usage.Usage]],
    token_sources: Sequence[str],
    ts: datetime | None = None,
    max_days: int = token_usage.MAX_BY_DAY_DAYS,
) -> HostUsageSnapshot:
    """Construct a ``host-usage-snapshot`` row from COMPLETED reader output.

    Pure: the caller (``events_tail._capture_event_snapshots``) has already
    established that every reader it CONSULTED returned ``complete=True`` and
    has merged their ``hosts`` maps. This function adds no classification of
    its own — it does not parse model IDs, consult a session's ``cwd``,
    attribute activity to a project, or invent a bucket for a host that
    reported none. ``host_usage`` owns all of that.

    ``token_sources`` is the per-push list of readers that actually
    contributed, NOT the built-in set: a host the user has not enabled as a
    sync source is never read, and one whose store cannot hold a usage ledger
    is dropped. Emitting the full constant instead would claim coverage the
    sweep did not have, and would make "this host reported nothing" and "this
    host was never consulted" indistinguishable on the wire.

    No ``EVENTS_SCHEMA_VERSION`` bump: the type is additive and every existing
    consumer already skips event types it does not know.

    **The payload is capped at the most recent ``max_days`` UTC days.** The
    host readers aggregate the WHOLE local corpus — ``_iter_rollouts`` has no
    ``since`` and the OpenCode query has no date predicate — so without this
    the row would carry the machine's entire lifetime of host activity, and
    would carry it again on every substantive push, into a synced
    content-addressed file that is re-uploaded whole. Every sibling is already
    bounded: git rows by ``since``, ``tokens_by_day`` by the same
    ``MAX_BY_DAY_DAYS``, and the day files themselves by
    ``retention.EVENTS_RETENTION_DAYS``. Measured unbounded on a real machine
    at 37 days / 4,147 bytes after five months, growing linearly forever.

    ``active_days`` is the sorted union of the UTC-day keys across families —
    the canonical day inputs a later consumer needs, derived here so it is not
    re-derived (differently) per renderer, and derived AFTER the cap so it can
    never advertise a day the payload dropped. The payload is copied so a
    caller that keeps merging into its own dict cannot mutate an already-built
    row.
    """
    payload: dict[str, dict[str, token_usage.Usage]] = {
        family: {day: dict(usage) for day, usage in days.items()}  # type: ignore[misc]
        for family, days in hosts.items()
    }
    # Cap on the UNION of days, not per family: capping each family separately
    # would keep a different window per host and make cross-host day totals
    # incomparable. ISO-8601 dates lex-sort as they date-sort (same reasoning
    # as `token_usage._trim_by_day`).
    all_days = sorted({day for days in payload.values() for day in days}, reverse=True)
    if len(all_days) > max_days:
        keep = set(all_days[:max_days])
        payload = {
            family: {day: usage for day, usage in days.items() if day in keep}
            for family, days in payload.items()
        }
        payload = {family: days for family, days in payload.items() if days}
    return {
        "v": EVENTS_SCHEMA_VERSION,
        "type": "host-usage-snapshot",
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "device": device,
        "token_sources": list(token_sources),
        "hosts": payload,
        "active_days": sorted({day for days in payload.values() for day in days}),
    }
