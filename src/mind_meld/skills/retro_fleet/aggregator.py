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
    skipped_lines: int = 0  # TODO#1 visible-failure breadcrumb
    window_exceeds_retention: bool = False  # TODO#2 visible-failure breadcrumb


# ---------------------------------------------------------------------------
# Tolerant readers (CQ#3 from /plan-eng-review).
# ---------------------------------------------------------------------------


def _iter_jsonl(path: Path, *, skip_counter: list[int]) -> Iterator[dict]:
    """Yield each parseable JSON object from a JSONL file.

    Tolerant of: missing file (yields nothing, counted as skip), per-line
    decode errors (skipped, counted), invalid UTF-8 bytes (replaced via
    ``errors="replace"`` so the file still parses), empty lines, non-dict
    objects (skipped, counted). Never raises.

    Adversarial-review fixes:
    * ``errors="replace"`` so a corrupt-byte sequence in one line doesn't
      raise UnicodeDecodeError and crash the whole retro.
    * File-level open failures bump ``skip_counter`` instead of being
      silently invisible (visible-failure contract).
    """
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        skip_counter[0] += 1
        return
    with f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                skip_counter[0] += 1
                continue
            if not isinstance(obj, dict):
                skip_counter[0] += 1
                continue
            yield obj


def _read_events(events_dir: Path, *, skip_counter: list[int]) -> Iterator[dict]:
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
        skip_counter[0] += 1
        return
    for f in files:
        yield from _iter_jsonl(f, skip_counter=skip_counter)


def _read_skill_usage(path: Path, *, skip_counter: list[int]) -> tuple[bool, list[dict]]:
    """Return (available, events). available=False when the file is absent
    (no gstack on this machine — section omitted from output)."""
    if not path.is_file():
        return False, []
    return True, list(_iter_jsonl(path, skip_counter=skip_counter))


def _read_eureka(path: Path, *, skip_counter: list[int]) -> tuple[bool, list[dict]]:
    if not path.is_file():
        return False, []
    return True, list(_iter_jsonl(path, skip_counter=skip_counter))


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
    """Pick the LATEST v=2 sessions-snapshot per (device, claude_dir) within
    the window, then filter to projects whose ``last_session_at`` falls inside
    the window — sum across that filtered set. v=1 snapshots flag the device
    as pre-v2 but contribute zero to totals.

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
    # latest[(device, claude_dir)] = (ts_dt, project_dict)
    latest: dict[tuple[str, str], tuple[datetime, dict]] = {}
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
            key = (device, claude_dir)
            prior = latest.get(key)
            if prior is None or prior[0] < ts_dt:
                latest[key] = (ts_dt, proj)

    # Stage 2: filter the latest-per-tuple set by `last_session_at` falling
    # inside the retro window. A project whose most recent session activity
    # predates `since` had no Claude Code activity in this window — its
    # snapshot's full-inventory count would inflate the totals with all-time
    # data. Drop those projects (the device still appears in fleet counts
    # because its mm-push event matched, just contributes 0 sessions).
    filtered_latest: dict[tuple[str, str], tuple[datetime, dict]] = {}
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
    for (_device, _claude_dir), (_ts, proj) in latest.items():
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
    """Read ``git config --global user.email`` + optional ``[retro]
    .author_emails`` from mm config.toml. Returns lowercased set; empty
    set means filter is disabled.
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
    # 2. mm config.toml — [retro].author_emails (per-machine; documented).
    cfg_emails = _read_config_author_emails()
    for e in cfg_emails:
        if isinstance(e, str) and e:
            emails.add(e.lower())
    return frozenset(emails)


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

    # Single skip counter shared across all readers — surfaces in the tail
    # breadcrumb regardless of which file produced parse errors.
    skip_counter = [0]

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
        skipped_lines=skip_counter[0],
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

    # Tail breadcrumbs (TODO#1, TODO#2 visible-failure contract).
    if data.skipped_lines:
        lines.append(
            f"*Note: {data.skipped_lines} event(s) skipped due to parse errors. "
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
