"""Fleet-aware retro aggregator (Group 8 / Track 8A).

Reads mm-owned event JSONLs from every fleet device, gstack analytics on the
rendering machine, and produces a glanceable markdown retro mirroring the
gstack ``/retro`` shape. Imported as ``mind_meld.skills.retro_fleet.aggregator``
or invoked as ``python -m mind_meld.skills.retro_fleet.aggregator <window>``.

Inputs (all tolerant of missing / corrupt / unknown-field files):

* ``$MM_EVENTS_DIR`` (or ``~/.local/share/mind-meld/events``) — fleet events
  written by ``_run_events_tail`` on every push. v=2 sessions snapshots are
  full inventory (Group 8); v=1 are delta-semantic relics from pre-v0.11.0
  peers (counted into "Fleet incomplete" breadcrumb, not into totals).
* ``~/.gstack/analytics/skill-usage.jsonl`` — gstack-owned, schema-dependency
  is load-bearing per the design doc; reader degrades to "Skills section
  omitted" on absence and tolerates unknown fields.
* ``~/.gstack/analytics/eureka.jsonl`` — same tolerance.
* ``mm devices --format=json`` (subprocess) — for the "M of N known devices"
  breadcrumb. Failure degrades to "events from N devices" without the M.

Aggregation rules (locked in /plan-eng-review):

* Git: dedup by ``(canonicalize_remote_url(remote), sha)``; sum LOC; group by
  repo for top-N.
* Sessions: pick LATEST v=2 snapshot per ``(device, claude_dir)``; sum across
  tuples. v=1 snapshots are NOT summed (delta semantics would inflate counts);
  they only contribute to the "pre-v0.11.0 peer" breadcrumb.
* Skills: as-rendered on this machine; locked-output breadcrumb says so.
* Eureka: union, window-filtered.
* mm-push: count by (device).

Visible-failure contract: skipped lines / files surface as a tail breadcrumb
("Note: N events skipped due to parse errors") so the user sees data quality
instead of a silently-truncated retro. A retro window exceeding
``EVENTS_RETENTION_DAYS`` (90) prints a "Note: window exceeds N-day events
retention" line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — kept in lockstep with mm's source-of-truth values.
# ---------------------------------------------------------------------------

EVENTS_RETENTION_DAYS = 90
"""Mirrors ``mind_meld.cli.EVENTS_RETENTION_DAYS``. Kept as a separate
constant here so the aggregator is importable without dragging in the cli
module's heavyweight imports. Pinned by ``test_retro_fleet_aggregator``'s
``test_retention_constant_matches_cli`` so a future bump catches drift."""

DEFAULT_EVENTS_DIR = Path("~/.local/share/mind-meld/events").expanduser()
"""Default events directory. Override with ``MM_EVENTS_DIR`` env var
(CQ#2 from /plan-eng-review). The bootstrap path matches what
``config.py:_bootstrap_mm_events_path`` materializes on first ``get_sources()``
call."""

GSTACK_ANALYTICS_DIR = Path("~/.gstack/analytics").expanduser()
GSTACK_RETROS_DIR = Path("~/.gstack/retros").expanduser()

V2_SCHEMA_VERSION = 2
"""sessions-snapshot schema version that the aggregator treats as full
inventory. v=1 is delta-semantic and excluded from sessions totals — see
events.py's EVENTS_SCHEMA_VERSION docstring for the cross-model-review
rationale."""

WINDOW_PATTERN = re.compile(r"^(\d+)d$")
"""Window argument: ``7d``, ``30d``, etc. Days only — hours/weeks/months
deferred to v2."""

TOP_N_REPOS = 5
TOP_N_PROJECTS = 5
TOP_N_SKILLS = 10

JSON_STREAM_MAX_BYTES = 50 * 1024 * 1024
"""Cap on the foreign-format reader's whole-file slurp. Mirrors mm's default
``max_file_size``. A runaway gstack analytics file beyond this is treated as
unparseable rather than slurped into memory — the breadcrumb still surfaces."""


# ---------------------------------------------------------------------------
# Dataclasses — everything is structured so format_retro() can render
# deterministically and tests can assert on per-section values.
# ---------------------------------------------------------------------------


@dataclass
class GitAggregate:
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    repos_by_count: dict[str, int] = field(default_factory=dict)
    cherrypick_pairs: int = 0  # informational; same subject, different shas


@dataclass
class SessionsAggregate:
    total_sessions: int = 0
    total_kb: int = 0
    projects: int = 0
    ephemeral_sessions: int = 0
    ephemeral_projects: int = 0
    # (decoded_path, sessions, ephemeral) — top-N most active projects.
    most_active: list[tuple[str, int, bool]] = field(default_factory=list)
    # devices still emitting v=1 sessions snapshots (pre-v0.11.0 peers).
    pre_v2_peers: set[str] = field(default_factory=set)


@dataclass
class PushesAggregate:
    push_events: int = 0
    pull_events: int = 0  # placeholder — events log doesn't currently emit pulls (v2)
    devices_with_pushes: set[str] = field(default_factory=set)
    discovery_errors: list[str] = field(default_factory=list)


@dataclass
class SkillsAggregate:
    invocations: int = 0
    by_skill: dict[str, int] = field(default_factory=dict)
    available: bool = True  # False when ~/.gstack/analytics/skill-usage.jsonl is absent


@dataclass
class EurekaAggregate:
    moments: list[tuple[str, str, str]] = field(default_factory=list)  # (insight, project, ts)
    available: bool = True


@dataclass
class FleetState:
    devices_in_events: set[str] = field(default_factory=set)
    devices_known: int | None = None  # None = `mm devices --format=json` failed; degrade gracefully
    devices_known_list: list[dict] = field(default_factory=list)


# Skip-counter category keys. Kept as constants so call-sites stay
# consistent and `format_retro` can map each category to a specific
# breadcrumb message. "events" covers mm-owned event JSONLs (real data
# quality signal); "skill_usage" / "eureka" cover gstack-owned analytics
# files (foreign data, format may diverge from JSONL).
SKIP_CATEGORY_EVENTS = "events"
SKIP_CATEGORY_SKILL_USAGE = "skill_usage"
SKIP_CATEGORY_EUREKA = "eureka"


@dataclass
class RetroData:
    window_days: int
    since: datetime
    until: datetime
    git: GitAggregate = field(default_factory=GitAggregate)
    sessions: SessionsAggregate = field(default_factory=SessionsAggregate)
    pushes: PushesAggregate = field(default_factory=PushesAggregate)
    skills: SkillsAggregate = field(default_factory=SkillsAggregate)
    eureka: EurekaAggregate = field(default_factory=EurekaAggregate)
    fleet: FleetState = field(default_factory=FleetState)
    # Per-category skip counters. Discriminates mm-owned event parse errors
    # (real data quality signal) from gstack-owned analytics format issues
    # (foreign file format, gstack's bug to fix). format_retro renders one
    # breadcrumb per non-zero entry, naming the affected file.
    skipped_per_source: dict[str, int] = field(default_factory=dict)
    # Backwards-compat summed view. Equals sum(skipped_per_source.values()).
    # Pre-existing tests assert on this field; new tests should drill into
    # skipped_per_source to verify category-specific behavior.
    skipped_lines: int = 0
    # Actual paths used for foreign-format readers — threaded into the
    # breadcrumb so a custom skill_usage_path / eureka_path renders honestly
    # instead of always pointing at ~/.gstack/analytics/...
    skill_usage_path: Path | None = None
    eureka_path: Path | None = None
    window_exceeds_retention: bool = False  # TODO#2 visible-failure breadcrumb


# ---------------------------------------------------------------------------
# Tolerant readers (CQ#3 from /plan-eng-review).
# ---------------------------------------------------------------------------


def _bump(skip_counter: dict[str, int], category: str, n: int = 1) -> None:
    skip_counter[category] = skip_counter.get(category, 0) + n


def _iter_jsonl(path: Path, *, skip_counter: dict[str, int], category: str) -> Iterator[dict]:
    """Yield each parseable JSON object from a strict JSONL file.

    Used for mm-owned event files (events.py guarantees one JSON object
    per line, single-line, no trailing whitespace beyond a newline).
    Tolerant of: missing file (yields nothing, counted as skip), per-line
    decode errors (skipped, counted), invalid UTF-8 bytes (replaced via
    ``errors="replace"`` so the file still parses), empty lines, non-dict
    objects (skipped, counted). Never raises.
    """
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        _bump(skip_counter, category)
        return
    with f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                _bump(skip_counter, category)
                continue
            if not isinstance(obj, dict):
                _bump(skip_counter, category)
                continue
            yield obj


def _iter_json_stream(path: Path, *, skip_counter: dict[str, int], category: str) -> Iterator[dict]:
    """Yield JSON objects from a file that may be JSONL or multi-line JSON.

    Used for gstack-owned analytics files where the on-disk format isn't
    fully under mm's control. Reads the whole file as text, then walks it
    with ``json.JSONDecoder.raw_decode`` — handles single-line-per-object
    (canonical JSONL) AND pretty-printed objects spanning multiple lines.
    On a malformed chunk, advances to the next newline and retries (so a
    single broken record doesn't poison the rest of the file).

    Each parseable dict is yielded; non-dict values, malformed chunks,
    and file-open failures bump ``skip_counter[category]``. Never raises.

    Files larger than ``JSON_STREAM_MAX_BYTES`` bump the skip counter and
    return without slurping — protects against a runaway analytics file
    spiking aggregator memory.
    """
    try:
        if path.stat().st_size > JSON_STREAM_MAX_BYTES:
            _bump(skip_counter, category)
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _bump(skip_counter, category)
        return
    decoder = json.JSONDecoder()
    pos = 0
    n = len(text)
    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            # Skip the unparseable chunk. Advance to the next newline so
            # one bad record doesn't suppress the rest of the file. If no
            # newline remains, the rest of the file is unrecoverable.
            nl = text.find("\n", pos)
            _bump(skip_counter, category)
            if nl == -1:
                return
            pos = nl + 1
            continue
        if isinstance(obj, dict):
            yield obj
        else:
            _bump(skip_counter, category)
        pos = end


def _read_events(events_dir: Path, *, skip_counter: dict[str, int]) -> Iterator[dict]:
    """Iterate every line of every ``*.jsonl`` under ``events_dir``.

    Per-file tolerance: an unreadable file bumps the skip counter and
    continues. Per-line tolerance: torn / non-JSON lines bump the skip
    counter and continue. Glob failure (rare; would need a vanished
    parent dir) bumps the counter once and returns.
    """
    if not events_dir.is_dir():
        return
    try:
        files = sorted(events_dir.glob("*.jsonl"))
    except OSError:
        _bump(skip_counter, SKIP_CATEGORY_EVENTS)
        return
    for f in files:
        yield from _iter_jsonl(f, skip_counter=skip_counter, category=SKIP_CATEGORY_EVENTS)


def _read_skill_usage(path: Path, *, skip_counter: dict[str, int]) -> tuple[bool, list[dict]]:
    """Return (available, events). available=False when the file is absent
    (no gstack on this machine — section omitted from output)."""
    if not path.is_file():
        return False, []
    return True, list(
        _iter_json_stream(path, skip_counter=skip_counter, category=SKIP_CATEGORY_SKILL_USAGE)
    )


def _read_eureka(path: Path, *, skip_counter: dict[str, int]) -> tuple[bool, list[dict]]:
    if not path.is_file():
        return False, []
    return True, list(
        _iter_json_stream(path, skip_counter=skip_counter, category=SKIP_CATEGORY_EUREKA)
    )


# ---------------------------------------------------------------------------
# Window filtering — events are timestamped UTC ISO 8601.
# ---------------------------------------------------------------------------


def _parse_iso(ts: object) -> datetime | None:
    """Parse ISO 8601. Returns None on any failure (forward-compat)."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _within_window(ts: object, since: datetime, until: datetime) -> bool:
    parsed = _parse_iso(ts)
    if parsed is None:
        return False
    return since <= parsed <= until


# ---------------------------------------------------------------------------
# Git aggregation — dedup by (canonical_remote_url, sha).
# ---------------------------------------------------------------------------


def _import_canonicalize() -> "callable":
    """Lazy import so tests can run without the full mind_meld install if
    they monkeypatch this. The aggregator's contract is to use mm's own
    canonicalization so dedup keys agree fleet-wide."""
    from mind_meld.events import canonicalize_remote_url

    return canonicalize_remote_url


def aggregate_git(
    events: Iterable[dict],
    *,
    since: datetime,
    until: datetime,
    author_emails: frozenset[str] | None,
) -> GitAggregate:
    """Walk git-snapshot events, dedup commits by ``(canonical, sha)``,
    apply the window + author filter, return totals.

    ``author_emails`` may be empty/None to disable the filter.
    """
    canonicalize = _import_canonicalize()
    seen_keys: set[tuple[str, str]] = set()
    seen_subjects: dict[str, int] = defaultdict(int)
    out = GitAggregate()
    for ev in events:
        if ev.get("type") != "git-snapshot":
            continue
        projects = ev.get("projects")
        if not isinstance(projects, list):
            continue
        for proj in projects:
            if not isinstance(proj, dict):
                continue
            remote_raw = proj.get("remote")
            remote = canonicalize(remote_raw) if isinstance(remote_raw, str) else ""
            commits = proj.get("commits")
            if not isinstance(commits, list):
                continue
            for c in commits:
                if not isinstance(c, dict):
                    continue
                sha = c.get("sha")
                if not isinstance(sha, str) or not sha:
                    continue
                if not _within_window(c.get("date"), since, until):
                    continue
                if author_emails:
                    ae = c.get("author_email")
                    if not isinstance(ae, str) or ae.lower() not in author_emails:
                        continue
                key = (remote, sha)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                subject = c.get("subject")
                if isinstance(subject, str) and subject:
                    seen_subjects[subject] += 1
                out.commits += 1
                out.additions += _safe_int(c.get("add"))
                out.deletions += _safe_int(c.get("del"))
                if remote:
                    out.repos_by_count[remote] = out.repos_by_count.get(remote, 0) + 1
    # Cherry-pick informational: same subject ≥ 2× across distinct shas.
    out.cherrypick_pairs = sum(1 for n in seen_subjects.values() if n > 1)
    return out


def _safe_int(x: object) -> int:
    """Tolerant int conversion — returns 0 on any non-integer input."""
    if isinstance(x, bool):
        return 0  # bool is int in Python; we don't want True → 1 silently
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        try:
            return int(x)
        except ValueError:
            return 0
    return 0


# ---------------------------------------------------------------------------
# Sessions aggregation — v=2 latest per (device, claude_dir); v=1 surfaces
# as "pre-v0.11.0 peer" breadcrumb only.
# ---------------------------------------------------------------------------


def aggregate_sessions(
    events: Iterable[dict],
    *,
    since: datetime,
    until: datetime,
) -> SessionsAggregate:
    """Pick the LATEST v=2 sessions-snapshot per (device, source_root,
    claude_dir) within the window, then filter to projects whose
    ``last_session_at`` falls inside the window — sum across that filtered
    set. v=1 snapshots flag the device as pre-v2 but contribute zero to
    totals.

    Three-tuple key (Group 8 hotfix). Pre-fix the key was ``(device,
    claude_dir)`` where ``claude_dir`` is the encoded directory NAME. Two
    configured ``type: claude`` source roots that both contain a project
    encoded as e.g. ``-Users-kb-Documents-foo`` would silently overwrite
    each other in ``latest``. The added ``source_root`` component preserves
    them as distinct entries.

    Coalesce pass for rollout: pre-fix records on synced storage have no
    ``source_root`` field (treated as ``""``); post-fix records carry the
    populated path. During the rollout window both shapes coexist for the
    same project — without coalescing, both keys are kept and sessions
    double-count for the upgrade week. The pass drops ``(device, "",
    claude_dir)`` keys when ``(device, "<root>", claude_dir)`` exists for
    the same device, preserving distinct populated source_roots (the
    legitimate two-source-root case the fix is for).

    Two-stage window filter (cross-model adversarial review fix). Pre-fix,
    the aggregator only filtered by the snapshot event's ``ts`` field, so a
    7-day retro could include sessions whose mtime was 60 days old as long
    as the device pushed today (the snapshot inherits ``ts=now`` regardless
    of when the underlying sessions were active). v=2 full-inventory makes
    that worse because every snapshot carries every session's count.

    The fix: snapshot ``last_session_at`` is the max mtime across the
    project's jsonls. Projects whose ``last_session_at`` is older than
    ``since`` had no Claude Code activity in this window — exclude them.
    Projects with ``last_session_at`` inside the window stay; their session
    counts are included. The count is "sessions on currently-active
    projects," which honestly overcounts when sessions span pre-window
    boundaries but stops the silent all-time-data-in-7-day-retro corruption.
    """
    out = SessionsAggregate()
    # latest[(device, source_root, claude_dir)] = (ts_dt, project_dict)
    latest: dict[tuple[str, str, str], tuple[datetime, dict]] = {}
    for ev in events:
        if ev.get("type") != "sessions-snapshot":
            continue
        device = ev.get("device")
        if not isinstance(device, str) or not device:
            continue
        ts_dt = _parse_iso(ev.get("ts"))
        if ts_dt is None or not (since <= ts_dt <= until):
            continue
        v = ev.get("v")
        if v != V2_SCHEMA_VERSION:
            # v=1 (or anything else) — record as pre-v2 peer, don't sum.
            out.pre_v2_peers.add(device)
            continue
        projects = ev.get("projects")
        if not isinstance(projects, list):
            continue
        for proj in projects:
            if not isinstance(proj, dict):
                continue
            claude_dir = proj.get("claude_dir")
            if not isinstance(claude_dir, str) or not claude_dir:
                continue
            source_root_raw = proj.get("source_root", "")
            source_root = source_root_raw if isinstance(source_root_raw, str) else ""
            key = (device, source_root, claude_dir)
            prior = latest.get(key)
            if prior is None or prior[0] < ts_dt:
                latest[key] = (ts_dt, proj)

    # Coalesce: drop (device, "", claude_dir) keys ONLY when a populated
    # sibling (device, "<root>", claude_dir) exists AND that sibling's ts is
    # at least as fresh as the legacy record. Without the freshness guard,
    # an older populated record can erase a newer legacy record (codex
    # adversarial review caught this — a downgrade or interleaved-fleet
    # push leaves the newer data in the legacy key, and the populated
    # sibling may itself be window-filtered out, returning zero sessions
    # for an active project). Preserves legitimate distinct source_roots
    # (the original bug fix); collapses pre-fix records during rollout
    # only when the post-fix successor is the same age or newer.
    populated_max_ts: dict[tuple[str, str], datetime] = {}
    for (d, sr, c), (ts, _) in latest.items():
        if sr:
            prior = populated_max_ts.get((d, c))
            if prior is None or ts > prior:
                populated_max_ts[(d, c)] = ts
    to_drop = [
        (d, sr, c)
        for (d, sr, c), (ts, _) in latest.items()
        if sr == "" and (d, c) in populated_max_ts and populated_max_ts[(d, c)] >= ts
    ]
    for k in to_drop:
        del latest[k]

    # Stage 2: filter the latest-per-tuple set by `last_session_at` falling
    # inside the retro window. A project whose most recent session activity
    # predates `since` had no Claude Code activity in this window — its
    # snapshot's full-inventory count would inflate the totals with all-time
    # data. Drop those projects (the device still appears in fleet counts
    # because its mm-push event matched, just contributes 0 sessions).
    filtered_latest: dict[tuple[str, str, str], tuple[datetime, dict]] = {}
    for key, (ts_dt, proj) in latest.items():
        last_at = _parse_iso(proj.get("last_session_at"))
        if last_at is None:
            # Snapshot has no last_session_at — likely a freshly-bootstrapped
            # source with zero jsonls. Empty data is honest; include with 0.
            filtered_latest[key] = (ts_dt, proj)
            continue
        if last_at < since:
            # Project was inactive across the entire retro window.
            continue
        filtered_latest[key] = (ts_dt, proj)
    latest = filtered_latest

    # Aggregate the "latest per tuple" set.
    project_decoded_to_sessions: dict[tuple[str, bool], int] = defaultdict(int)
    for (_device, _source_root, _claude_dir), (_ts, proj) in latest.items():
        sessions = _safe_int(proj.get("sessions"))
        total_kb = _safe_int(proj.get("total_kb"))
        ephemeral = bool(proj.get("ephemeral", False))
        out.total_sessions += sessions
        out.total_kb += total_kb
        out.projects += 1
        if ephemeral:
            out.ephemeral_sessions += sessions
            out.ephemeral_projects += 1
        # Aggregate per-decoded-path for "most active" — falls back to
        # encoded claude_dir when cwd was unavailable upstream.
        decoded = proj.get("decoded_path")
        if not isinstance(decoded, str) or not decoded:
            decoded = proj.get("claude_dir") or ""
        project_decoded_to_sessions[(decoded, ephemeral)] += sessions
    # Top by sessions.
    out.most_active = sorted(
        ((d, n, eph) for (d, eph), n in project_decoded_to_sessions.items()),
        key=lambda t: t[1],
        reverse=True,
    )[:TOP_N_PROJECTS]
    return out


# ---------------------------------------------------------------------------
# Pushes aggregation — count mm-push events by device.
# ---------------------------------------------------------------------------


def aggregate_pushes(
    events: Iterable[dict],
    *,
    since: datetime,
    until: datetime,
) -> PushesAggregate:
    out = PushesAggregate()
    discovery_errors_seen: set[str] = set()
    for ev in events:
        if ev.get("type") != "mm-push":
            continue
        if not _within_window(ev.get("ts"), since, until):
            continue
        device = ev.get("device")
        if isinstance(device, str) and device:
            out.devices_with_pushes.add(device)
        out.push_events += 1
        # discovery_errors are forensic; collect distinct strings to surface.
        errs = ev.get("discovery_errors")
        if isinstance(errs, list):
            for e in errs:
                if isinstance(e, str) and e and e not in discovery_errors_seen:
                    discovery_errors_seen.add(e)
                    out.discovery_errors.append(e)
    return out


# ---------------------------------------------------------------------------
# Skills aggregation — gstack analytics, single machine view.
# ---------------------------------------------------------------------------


def aggregate_skills(
    available: bool,
    events: Iterable[dict],
    *,
    since: datetime,
    until: datetime,
) -> SkillsAggregate:
    out = SkillsAggregate(available=available)
    if not available:
        return out
    for ev in events:
        if not _within_window(ev.get("ts"), since, until):
            continue
        skill = ev.get("skill")
        if not isinstance(skill, str) or not skill:
            continue
        out.invocations += 1
        out.by_skill[skill] = out.by_skill.get(skill, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Eureka aggregation — gstack-owned, window-filtered.
# ---------------------------------------------------------------------------


def aggregate_eureka(
    available: bool,
    events: Iterable[dict],
    *,
    since: datetime,
    until: datetime,
) -> EurekaAggregate:
    out = EurekaAggregate(available=available)
    if not available:
        return out
    for ev in events:
        if not _within_window(ev.get("ts"), since, until):
            continue
        insight = ev.get("insight")
        if not isinstance(insight, str) or not insight:
            continue
        project = ev.get("branch") or ev.get("skill") or "?"
        ts = ev.get("ts")
        out.moments.append(
            (
                insight,
                str(project)[:60],
                str(ts)[:10] if isinstance(ts, str) else "?",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Fleet device count — subprocess `mm devices --format=json`.
# ---------------------------------------------------------------------------


def get_known_devices() -> tuple[int | None, list[dict]]:
    """Invoke mm via ``python -m mind_meld.cli devices --format=json``.
    Degrade to (None, []) on ANY failure (not initialized, JSON parse
    error, etc.). The retro renders "events from N devices" without the
    "of M" tail when the count is None.

    Adversarial-review fix: invoke via ``sys.executable -m mind_meld.cli``
    instead of bare ``["mm", ...]``. This binds the subprocess to the SAME
    Python interpreter / venv that's running the aggregator, sidestepping
    PATH-hijacking concerns and venv-version skew (a stale ``mm`` binary
    earlier in PATH would otherwise be invoked instead of the aggregator's
    sibling install).
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mind_meld.cli", "devices", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, []
    if result.returncode != 0:
        return None, []
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, []
    if not isinstance(records, list):
        return None, []
    return len(records), records


# ---------------------------------------------------------------------------
# Author email gathering.
# ---------------------------------------------------------------------------


def gather_author_emails() -> frozenset[str]:
    """Collect every email address that should count as "the user's commits"
    for the retro window. Returns lowercased set; empty set means filter
    is disabled.

    Trust-rooted: only emails sourced from CONFIGURED identities on
    machines the user controls. The aggregator filter is matched against
    ``author_email`` on each captured commit, so any email in this set
    will pull commits authored under that identity into the retro. A
    broad ``git log`` walk that grabs every author/committer in
    discovered repos would include collaborator emails on shared repos
    (their PRs / pull-merged commits sit in the local history once you
    pull) and silently inflate retros with their work as yours. Trust-
    rooted scoping eliminates that class of false-positive entirely.

    Four sources are unioned:

    1. ``git config --global user.email`` — the canonical "this user"
       email on this machine.
    2. ``git config user.email`` from each discovered git root
       (``_per_repo_user_emails``) — captures per-repo overrides where
       the user has explicitly configured a different identity for a
       specific project (e.g., a dotfiles repo using a personal email
       where the global default is a work email).
    3. ``[retro].author_emails`` in mm config.toml — manual override
       list, per-machine, for identities the user has used historically
       (different machine, since-revoked address) that aren't currently
       configured anywhere on this machine.
    4. ``<id>+<login>@users.noreply.github.com`` derived from
       ``gh api user`` (``_gh_noreply_email``) — PR-merges via the
       GitHub web UI set author to this form regardless of local git
       config; without source #4 those commits would silently fall out
       of the filter. Uniquely the user's — the ``<id>`` and
       ``<login>`` together can't collide with a collaborator's
       noreply form, so including this email in the trust set is safe
       on shared repos.

    Note: this set may *under*-count if the user has identities in use
    on machines outside this one's reach (e.g., committing as
    ``karl@personal`` on the iMac but the MacBook only has
    ``kb@work`` configured locally and pulls iMac events via mm).
    Workaround: list every identity in ``[retro].author_emails`` on
    each machine. Future improvement: sync the trust set across the
    fleet via mm-events (deferred).
    """
    emails: set[str] = set()
    # 1. git config --global user.email
    try:
        result = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            ge = result.stdout.strip().lower()
            if ge:
                emails.add(ge)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    # 2. Per-repo user.email from each discovered git root.
    for e in _per_repo_user_emails():
        emails.add(e)
    # 3. mm config.toml — [retro].author_emails (per-machine; documented).
    cfg_emails = _read_config_author_emails()
    for e in cfg_emails:
        if isinstance(e, str) and e:
            emails.add(e.lower())
    # 4. GitHub noreply form derived from `gh api user`.
    noreply = _gh_noreply_email()
    if noreply:
        emails.add(noreply.lower())
    return frozenset(emails)


# Time budget for the per-repo `git config user.email` reads. One subprocess
# per discovered repo. Bounded so a wedged filesystem or pathologically
# slow git invocation doesn't turn the retro into a multi-second wait.
_PER_REPO_SCAN_BUDGET_SECONDS = 5.0
_PER_REPO_GIT_TIMEOUT_SECONDS = 2.0


def _per_repo_user_emails() -> set[str]:
    """For each discovered git root, read ``git config user.email``.
    Returns lowercased set; empty set on any failure.

    ``git config user.email`` (no ``--local``) returns the per-repo
    override if one is set, else falls through to the global. The
    union dedups against the global already added in
    ``gather_author_emails`` so duplicates don't matter; what we
    actually want is to catch the per-repo overrides where the user
    has configured a different identity for a specific project.

    Crucially does NOT walk ``git log`` — only reads configured
    identity. Walking commits would pull in collaborator emails from
    shared repos (their PRs/pulled-in commits sit in local history),
    silently inflating retros with their work as yours. Trust-rooted
    scoping defends against that class of false-positive.

    Bounded total wall-clock at ``_PER_REPO_SCAN_BUDGET_SECONDS`` and
    per-repo timeout at ``_PER_REPO_GIT_TIMEOUT_SECONDS``; budget
    exhaustion returns whatever was collected so far.
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

    deadline = time.monotonic() + _PER_REPO_SCAN_BUDGET_SECONDS
    out: set[str] = set()
    for root in roots:
        if time.monotonic() > deadline:
            break
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "config", "user.email"],
                capture_output=True,
                text=True,
                timeout=_PER_REPO_GIT_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        email = result.stdout.strip().lower()
        if email:
            out.add(email)
    return out


def _gh_noreply_email() -> str | None:
    """Derive ``<id>+<login>@users.noreply.github.com`` from the local
    ``gh`` CLI's authenticated user, or None on any failure.

    Best-effort: a missing ``gh`` binary, unauthenticated session,
    network hiccup, or unexpected JSON shape all return None. Retro
    callers union the result into the trust set; an absent return
    narrows the filter (PR-merges authored under the noreply form
    silently fall out) but doesn't break aggregation.

    Why include this at all: GitHub's web-merge UI (and ``gh pr merge``
    by default) sets author = the per-user noreply form regardless of
    local git config. Without this entry in the trust set, a user who
    lands most work via PR-merge sees retros that drop the bulk of
    their own activity. The per-user ``<id>+<login>`` form is unique
    to one GitHub user, so including it in the trust set does NOT
    open the collaborator-leak hole that a broad ``git log`` walk
    would.
    """
    try:
        result = subprocess.run(
            ["gh", "api", "user"],
            capture_output=True,
            text=True,
            timeout=3,
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
    uid = data.get("id")
    login = data.get("login")
    if not isinstance(uid, int) or not isinstance(login, str) or not login:
        return None
    return f"{uid}+{login}@users.noreply.github.com"


def _read_config_author_emails() -> list[str]:
    """Best-effort read of mm config.toml's ``[retro].author_emails``.
    Tolerates missing / malformed config — returns []."""
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
    return [a for a in aliases if isinstance(a, str)]


# ---------------------------------------------------------------------------
# Top-level orchestration.
# ---------------------------------------------------------------------------


def aggregate(
    *,
    events_dir: Path,
    window_days: int,
    author_emails: frozenset[str],
    skill_usage_path: Path,
    eureka_path: Path,
    now: datetime | None = None,
) -> RetroData:
    """Read every input source, aggregate per the locked rules, return the
    structured retro data. ``format_retro(data)`` renders it."""
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=window_days)

    # Per-category skip counters. Discriminates mm-owned event parse errors
    # from gstack-owned analytics format issues so the tail breadcrumb can
    # name the affected file instead of conflating them under one count.
    skip_counter: dict[str, int] = {}

    events = list(_read_events(events_dir, skip_counter=skip_counter))
    skill_avail, skill_events = _read_skill_usage(skill_usage_path, skip_counter=skip_counter)
    eureka_avail, eureka_events = _read_eureka(eureka_path, skip_counter=skip_counter)

    git = aggregate_git(events, since=since, until=until, author_emails=author_emails)
    sessions = aggregate_sessions(events, since=since, until=until)
    pushes = aggregate_pushes(events, since=since, until=until)
    skills = aggregate_skills(skill_avail, skill_events, since=since, until=until)
    eureka = aggregate_eureka(eureka_avail, eureka_events, since=since, until=until)

    devices_known, devices_known_list = get_known_devices()

    # Devices that have produced events in the window.
    devices_in_events: set[str] = set(pushes.devices_with_pushes)
    for ev in events:
        d = ev.get("device")
        if isinstance(d, str) and d and _within_window(ev.get("ts"), since, until):
            devices_in_events.add(d)

    return RetroData(
        window_days=window_days,
        since=since,
        until=until,
        git=git,
        sessions=sessions,
        pushes=pushes,
        skills=skills,
        eureka=eureka,
        fleet=FleetState(
            devices_in_events=devices_in_events,
            devices_known=devices_known,
            devices_known_list=devices_known_list,
        ),
        skipped_per_source=dict(skip_counter),
        skipped_lines=sum(skip_counter.values()),
        skill_usage_path=skill_usage_path,
        eureka_path=eureka_path,
        window_exceeds_retention=window_days > EVENTS_RETENTION_DAYS,
    )


# ---------------------------------------------------------------------------
# Markdown renderer — locked output format per docs/designs/fleet-retro.md.
# ---------------------------------------------------------------------------


def format_retro(data: RetroData) -> str:
    """Render the locked-format markdown retro. Output is paste-ready for
    iMessage / email — single-message length when realistic data is present."""
    lines: list[str] = []
    lines.append(
        f"# Retro: {data.since.date().isoformat()} → {data.until.date().isoformat()} "
        f"({data.window_days}d)"
    )
    lines.append("")

    # Activity-across-N-machines header.
    n_in_events = len(data.fleet.devices_in_events)
    if data.fleet.devices_known is not None:
        m_known = data.fleet.devices_known
        if n_in_events > m_known:
            # Data inconsistency: more device IDs in the events log than are
            # registered in storage. The honest read is "events came from N
            # devices, M of which are currently registered." Pre-v0.11.9 this
            # rendered as "N of M known machines" which read as a counting bug
            # (e.g. "33 of 3"). The usual cause is stale events from
            # de-registered or test-leaked phantom device IDs (events retain
            # for 90 days; devices/<id>.json can disappear sooner).
            extra = n_in_events - m_known
            lines.append(
                f"**Activity from {n_in_events} machine(s) ({m_known} currently registered)**"
            )
            lines.append(
                f"*Fleet inconsistency: {extra} device id(s) in events but not in "
                f"`mm devices`. Likely stale or phantom — run `mm gc --conflicts` "
                f"and check for old event files in `~/.local/share/mind-meld/events/`.*"
            )
        else:
            lines.append(f"**Activity across {n_in_events} of {m_known} known machines**")
            if n_in_events < m_known:
                missing = m_known - n_in_events
                lines.append(
                    f"*Fleet incomplete: {missing} device(s) haven't pushed events in this window.*"
                )
    else:
        # mm devices unavailable — degrade gracefully.
        lines.append(
            f"**Activity across {n_in_events} machine(s)** *(known-fleet count unavailable)*"
        )
    if data.sessions.pre_v2_peers:
        n_pre = len(data.sessions.pre_v2_peers)
        lines.append(
            f"*Sessions count incomplete: {n_pre} peer(s) on pre-v0.11.0 — "
            f"upgrade for accurate session totals.*"
        )
    lines.append("")

    # Code shipped.
    lines.append("## Code shipped")
    lines.append(
        f"- {data.git.commits} commits across {len(data.git.repos_by_count)} repos "
        f"(deduped across machines)"
    )
    lines.append(f"- +{data.git.additions:,} / -{data.git.deletions:,} LOC")
    if data.git.repos_by_count:
        top_repos = sorted(data.git.repos_by_count.items(), key=lambda kv: kv[1], reverse=True)[
            :TOP_N_REPOS
        ]
        formatted = ", ".join(f"{r} ({n})" for r, n in top_repos)
        lines.append(f"- Top repos: {formatted}")
    if data.git.cherrypick_pairs:
        lines.append(
            f"- *Note: {data.git.cherrypick_pairs} commit subject(s) appear under "
            f"multiple SHAs (cherry-picks counted separately).*"
        )
    lines.append("")

    # Claude Code activity.
    lines.append("## Claude Code activity")
    if data.sessions.total_sessions == 0 and not data.sessions.pre_v2_peers:
        lines.append("- No Claude Code sessions captured in this window.")
    else:
        non_eph = data.sessions.total_sessions - data.sessions.ephemeral_sessions
        eph = data.sessions.ephemeral_sessions
        lines.append(
            f"- {data.sessions.total_sessions} sessions across "
            f"{data.sessions.projects} projects "
            f"({eph} in ephemeral Conductor workspaces, counted separately)"
        )
        mb = data.sessions.total_kb / 1024
        lines.append(f"- {mb:,.1f} MB total session content")
        if data.sessions.most_active:
            formatted = ", ".join(
                f"{p} ({n}{' ephemeral' if eph else ''})" for p, n, eph in data.sessions.most_active
            )
            lines.append(f"- Most active: {formatted}")
        # Defensive: mention non_eph in passing if both buckets non-zero.
        _ = non_eph  # suppress unused-var lint
    lines.append("")

    # Skills used (this machine only).
    lines.append(f"## Skills used ({data.skills.invocations} invocations) — *this machine only*")
    if not data.skills.available:
        lines.append("- *gstack analytics not found on this machine — section omitted.*")
    elif data.skills.invocations == 0:
        lines.append("- No skill invocations captured.")
    else:
        top = sorted(data.skills.by_skill.items(), key=lambda kv: kv[1], reverse=True)[
            :TOP_N_SKILLS
        ]
        formatted = ", ".join(f"/{s} ({n})" for s, n in top)
        lines.append(f"- {formatted}")
    lines.append("")

    # Eureka moments.
    lines.append(f"## Eureka moments ({len(data.eureka.moments)})")
    if not data.eureka.available:
        lines.append("- *gstack eureka log not found on this machine — section omitted.*")
    elif not data.eureka.moments:
        lines.append("- No eureka moments captured.")
    else:
        for insight, project, ts in data.eureka.moments[:TOP_N_SKILLS]:
            lines.append(f'- "{insight}" ({project}, {ts})')
    lines.append("")

    # mm sync activity.
    lines.append("## mm sync activity")
    lines.append(
        f"- {data.pushes.push_events} pushes across "
        f"{len(data.pushes.devices_with_pushes)} device(s)"
    )
    if data.pushes.discovery_errors:
        n_errs = len(data.pushes.discovery_errors)
        lines.append(
            f"- *{n_errs} discovery error(s) recorded — see mm: notice: stderr breadcrumbs.*"
        )
    lines.append("")

    # Tail breadcrumbs (TODO#1, TODO#2 visible-failure contract). One line
    # per affected file so the user knows where to look — conflating mm
    # events with foreign gstack files under one count was misleading.
    n_events = data.skipped_per_source.get(SKIP_CATEGORY_EVENTS, 0)
    n_skill = data.skipped_per_source.get(SKIP_CATEGORY_SKILL_USAGE, 0)
    n_eureka = data.skipped_per_source.get(SKIP_CATEGORY_EUREKA, 0)
    if n_events:
        lines.append(
            f"*Note: {n_events} event(s) skipped due to parse errors in mm event log. "
            f"Output may be incomplete.*"
        )
    if n_skill:
        skill_label = str(data.skill_usage_path) if data.skill_usage_path else "skill-usage.jsonl"
        lines.append(
            f"*Note: {n_skill} record(s) skipped in {skill_label} "
            f"(gstack file format issue, not mm).*"
        )
    if n_eureka:
        eureka_label = str(data.eureka_path) if data.eureka_path else "eureka.jsonl"
        lines.append(
            f"*Note: {n_eureka} record(s) skipped in {eureka_label} "
            f"(gstack file format issue, not mm).*"
        )
    # Backward-compat fallback: a manually-constructed RetroData with
    # skipped_lines set but no per-source breakdown (older callers, tests
    # constructing RetroData directly) would otherwise render no breadcrumb
    # at all. Aggregate() always populates per-source so this only fires
    # for foreign callers — keeps the visible-failure contract intact.
    if not data.skipped_per_source and data.skipped_lines:
        lines.append(
            f"*Note: {data.skipped_lines} record(s) skipped due to parse errors. "
            f"Output may be incomplete.*"
        )
    if data.window_exceeds_retention:
        lines.append(
            f"*Note: requested {data.window_days}d window exceeds the "
            f"{EVENTS_RETENTION_DAYS}-day events retention. Older days are "
            f"reaped by `mm gc` and will not appear in this retro.*"
        )

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------


def _resolve_events_dir() -> Path:
    """``MM_EVENTS_DIR`` env override (CQ#2) wins; falls back to default."""
    override = os.environ.get("MM_EVENTS_DIR")
    if override:
        return Path(override).expanduser()
    return DEFAULT_EVENTS_DIR


def _read_mm_events_config_path() -> Path | None:
    """Best-effort read of mm config.toml's ``mm-events`` source ``path``
    field. Returns the expanded Path or None on any failure (config absent,
    malformed, no mm-events source, mm-events disabled per-machine).
    Mirrors ``_read_config_author_emails``'s tolerant pattern — never
    raises.

    Disabled-source gate: if ``[sync].disabled_sources`` contains
    ``mm-events``, the user has explicitly opted out per-machine. Return
    None so ``_emit_custom_path_notice_if_due`` stays silent — a notice
    nudging them to set ``MM_EVENTS_DIR`` for a source they disabled
    fails the visible-failure contract."""
    try:
        from mind_meld.config import CONFIG_PATH, load_config

        cfg = load_config(CONFIG_PATH)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    sync = cfg.get("sync")
    if not isinstance(sync, dict):
        return None
    disabled_raw = sync.get("disabled_sources")
    disabled = set(disabled_raw) if isinstance(disabled_raw, list) else set()
    if "mm-events" in disabled:
        return None
    sources = sync.get("sources")
    if not isinstance(sources, list):
        return None
    for src in sources:
        if not isinstance(src, dict):
            continue
        if src.get("name") != "mm-events":
            continue
        path = src.get("path")
        if not isinstance(path, str) or not path:
            return None
        return Path(path).expanduser()
    return None


def _emit_custom_path_notice_if_due(events_dir: Path) -> None:
    """Emit a one-line ``mm: notice:`` to stderr when the user has an
    ``mm-events`` source configured at a non-default path AND
    ``MM_EVENTS_DIR`` is unset — surfaces the silent-empty-retro hazard
    flagged by adversarial review.

    Gated to the CLI entry point (called from ``main()``); library callers
    of ``aggregate()`` never see the notice. Silent in every other
    scenario: env override set, config matches default, config unreadable,
    no mm-events source configured."""
    if os.environ.get("MM_EVENTS_DIR"):
        return
    if events_dir != DEFAULT_EVENTS_DIR:
        return  # called via env override (already returned above) or non-default param path
    cfg_path = _read_mm_events_config_path()
    if cfg_path is None:
        return  # no mm-events source configured (pre-v0.10.1) or unreadable config
    if cfg_path == DEFAULT_EVENTS_DIR.parent:
        return  # config matches default base path; nothing to point out
    sys.stderr.write(
        f"mm: notice: mm-events source configured at {cfg_path} but "
        f"MM_EVENTS_DIR is unset; retro will read from {DEFAULT_EVENTS_DIR}. "
        f"Set MM_EVENTS_DIR={cfg_path}/events to override.\n"
    )


def _parse_window(s: str) -> int:
    m = WINDOW_PATTERN.match(s)
    if m is None:
        raise argparse.ArgumentTypeError(
            f"window must be of the form Nd (e.g. '7d', '30d'); got {s!r}"
        )
    n = int(m.group(1))
    if n <= 0:
        raise argparse.ArgumentTypeError(f"window must be positive; got {n}")
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="retro-fleet",
        description="Fleet-aware retro for the mm event log.",
    )
    parser.add_argument(
        "window",
        type=_parse_window,
        help="Retro window (Nd, e.g. '7d', '30d'). Days only.",
    )
    parser.add_argument(
        "--no-author-filter",
        action="store_true",
        help="Disable author email filtering (renders ALL fleet commits).",
    )
    args = parser.parse_args(argv)

    events_dir = _resolve_events_dir()
    _emit_custom_path_notice_if_due(events_dir)
    author_emails = frozenset() if args.no_author_filter else gather_author_emails()
    data = aggregate(
        events_dir=events_dir,
        window_days=args.window,
        author_emails=author_emails,
        skill_usage_path=GSTACK_ANALYTICS_DIR / "skill-usage.jsonl",
        eureka_path=GSTACK_ANALYTICS_DIR / "eureka.jsonl",
    )
    sys.stdout.write(format_retro(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
