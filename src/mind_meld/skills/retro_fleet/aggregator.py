"""Fleet-aware retro aggregator (Group 8 / Track 8A).

Reads mm-owned event JSONLs from every fleet device, gstack analytics on the
rendering machine, and produces a glanceable markdown retro mirroring the
gstack ``/retro`` shape. Imported as ``mind_meld.skills.retro_fleet.aggregator``
or invoked as ``python -m mind_meld.skills.retro_fleet.aggregator <window>``.

Inputs (all tolerant of missing / corrupt / unknown-field files):

* ``$MM_EVENTS_DIR`` (or ``~/.local/share/mind-meld/events``) — fleet events
  written by ``_run_events_tail`` on every push. v=2 sessions snapshots are
  full inventory (Group 8); v=1 are delta-semantic relics from pre-v0.11.0
  peers (surfaced in the Notes section, not summed into totals).
* ``~/.gstack/analytics/skill-usage.jsonl`` — gstack-owned, schema-dependency
  is load-bearing per the design doc; reader degrades to "Skills section
  omitted" on absence and tolerates unknown fields.
* ``mm devices --format=json`` (subprocess) — for the "N of M known machines"
  header AND the phantom-event filter (see ``aggregate``). Failure degrades
  to all-events-counted with a "known-fleet count unavailable" note.

Aggregation rules:

* Git: dedup by ``(canonicalize_remote_url(remote), sha)``; sum LOC; group by
  repo for top-N.
* Sessions: pick LATEST v=2 snapshot per ``(device, source_root, claude_dir)``;
  sum across tuples. v=1 snapshots are NOT summed.
* Skills: as-rendered on this machine; locked-output breadcrumb says so.
* mm-push: count by (device).
* Phantom-event filter: when ``mm devices --format=json`` succeeds, intersect
  event-producing IDs with the registered fleet so de-registered or test-
  leaked phantom IDs fall out of the rendered count. Stale event files age
  out via the existing 90-day retention.

Visible-failure contract: data-quality and diagnostic asides are
consolidated into the tail Notes section so the user sees them in one place
rather than scattered across each section's body.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mind_meld import identity

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

V2_SCHEMA_VERSION = 2
"""sessions-snapshot schema version that the aggregator treats as full
inventory. v=1 is delta-semantic and excluded from sessions totals — see
events.py's EVENTS_SCHEMA_VERSION docstring for the cross-model-review
rationale."""

WINDOW_PATTERN = re.compile(r"^(\d+)d$")
"""Window argument: ``7d``, ``30d``, etc. Days only — hours/weeks/months
deferred to v2."""

TOP_N_REPOS = 5
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


@dataclass
class SessionsAggregate:
    total_sessions: int = 0
    projects: int = 0
    ephemeral_sessions: int = 0
    ephemeral_projects: int = 0
    # devices still emitting v=1 sessions snapshots (pre-v0.11.0 peers).
    pre_v2_peers: set[str] = field(default_factory=set)
    # Token totals across the retro window, sliced from per-project
    # `tokens_by_day` maps. Empty defaults preserve "fleet has no token-
    # aware peers" behavior cleanly.
    tokens_input: int = 0
    tokens_cache_create: int = 0
    tokens_cache_read: int = 0
    tokens_output: int = 0
    tokens_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    # Devices on v=2 schema but emitting snapshots WITHOUT `tokens_by_day`.
    # Sniffed at aggregate time: any project with sessions > 0 and no
    # tokens_by_day flags the device. Pre-v0.11.14 peers (mixed-fleet
    # rollout window) AND any peer with cold token cache appear here.
    pre_token_peers: set[str] = field(default_factory=set)


@dataclass
class PushesAggregate:
    push_events: int = 0
    devices_with_pushes: set[str] = field(default_factory=set)
    discovery_errors: list[str] = field(default_factory=list)


@dataclass
class SkillsAggregate:
    invocations: int = 0
    by_skill: dict[str, int] = field(default_factory=dict)
    available: bool = True  # False when ~/.gstack/analytics/skill-usage.jsonl is absent


@dataclass
class FleetState:
    # Set of device IDs with events in the window, intersected with the
    # registered fleet (`mm devices --format=json`) when available. Phantom
    # IDs from de-registered or test-leaked devices fall out at this filter
    # rather than surfacing as an "N machine(s) (M registered)" inconsistency
    # banner. When `mm devices` fails, falls back to all event-producing IDs.
    devices_in_events: set[str] = field(default_factory=set)
    devices_known: int | None = None  # None = `mm devices --format=json` failed; degrade gracefully
    devices_known_list: list[dict] = field(default_factory=list)
    # Count of unregistered device IDs that produced events in the window.
    # Surfaced as a one-line note in the Notes section so the user knows
    # phantom-event files exist on disk (reaped by the 90-day TTL).
    unregistered_event_devices: int = 0


# Skip-counter category keys. Kept as constants so call-sites stay
# consistent and `format_retro` can map each category to a specific
# breadcrumb message. "events" covers mm-owned event JSONLs (real data
# quality signal); "skill_usage" covers gstack-owned analytics files
# (foreign data, format may diverge from JSONL).
SKIP_CATEGORY_EVENTS = "events"
SKIP_CATEGORY_SKILL_USAGE = "skill_usage"


@dataclass
class RetroData:
    window_days: int
    since: datetime
    until: datetime
    git: GitAggregate = field(default_factory=GitAggregate)
    sessions: SessionsAggregate = field(default_factory=SessionsAggregate)
    pushes: PushesAggregate = field(default_factory=PushesAggregate)
    skills: SkillsAggregate = field(default_factory=SkillsAggregate)
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
    # Actual path used for the foreign-format skill-usage reader — threaded
    # into the breadcrumb so a custom skill_usage_path renders honestly
    # instead of always pointing at ~/.gstack/analytics/...
    skill_usage_path: Path | None = None
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
                out.commits += 1
                out.additions += _safe_int(c.get("add"))
                out.deletions += _safe_int(c.get("del"))
                if remote:
                    out.repos_by_count[remote] = out.repos_by_count.get(remote, 0) + 1
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
    for (device, _source_root, _claude_dir), (_ts, proj) in latest.items():
        sessions = _safe_int(proj.get("sessions"))
        ephemeral = bool(proj.get("ephemeral", False))
        out.total_sessions += sessions
        out.projects += 1
        if ephemeral:
            out.ephemeral_sessions += sessions
            out.ephemeral_projects += 1

        # Token aggregation (v0.11.14+). Slice tokens_by_day to [since, until]
        # and merge into the running totals.
        tokens_by_day = proj.get("tokens_by_day")
        if isinstance(tokens_by_day, dict) and tokens_by_day:
            _merge_token_window(out, tokens_by_day, since=since, until=until)
        elif sessions > 0:
            # Sessions exist but tokens_by_day is missing/empty. Either a
            # pre-v0.11.14 peer (no field) or a peer whose token cache is
            # cold (autopush gate skipped the token walk this push). Same
            # user-visible signal: "tokens incomplete: device X."
            out.pre_token_peers.add(device)
    return out


def _merge_token_window(
    out: SessionsAggregate,
    tokens_by_day: dict,
    *,
    since: datetime,
    until: datetime,
) -> None:
    """Sum the day buckets whose YYYY-MM-DD key falls in [since, until] into
    ``out``'s token fields. Honest "tokens consumed THIS WINDOW" semantics
    (per /plan-eng-review D6 — codex caught the per-window accuracy gap)."""
    since_d = since.astimezone(timezone.utc).date().isoformat()
    until_d = until.astimezone(timezone.utc).date().isoformat()
    for day_key, bucket in tokens_by_day.items():
        if not isinstance(day_key, str) or not (since_d <= day_key <= until_d):
            continue
        if not isinstance(bucket, dict):
            continue
        out.tokens_input += _safe_int(bucket.get("input"))
        out.tokens_cache_create += _safe_int(bucket.get("cache_create"))
        out.tokens_cache_read += _safe_int(bucket.get("cache_read"))
        out.tokens_output += _safe_int(bucket.get("output"))
        for model, mbucket in (bucket.get("by_model") or {}).items():
            if not isinstance(model, str) or not isinstance(mbucket, dict):
                continue
            mtarget = out.tokens_by_model.setdefault(
                model,
                {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0},
            )
            mtarget["input"] += _safe_int(mbucket.get("input"))
            mtarget["cache_create"] += _safe_int(mbucket.get("cache_create"))
            mtarget["cache_read"] += _safe_int(mbucket.get("cache_read"))
            mtarget["output"] += _safe_int(mbucket.get("output"))


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
    """Backwards-compat shim around ``mind_meld.identity.gather_local_
    identities``. Returns the running machine's locally-known emails as a
    frozenset.

    Pre-v0.11.17 this function performed all four subprocess walks itself
    and returned a per-machine filter. Post-v0.11.17 the canonical store
    of "my emails" is ``mind_meld.identity``, which provides a flock-
    protected 7d-TTL cache so push tail and retro render share state.
    This shim preserves the old API for library callers who were importing
    ``gather_author_emails`` directly. The retro filter itself is now a
    fleet-wide UNION (see ``aggregate``); this function only contributes
    the local-machine slice.
    """
    return frozenset(identity.gather_local_identities(allow_refresh=True))


def aggregate_local_emails_from_events(events: Iterable[dict]) -> set[str]:
    """Walk every ``mm-push`` event row and union its ``local_emails`` field
    into a single fleet-wide trust set.

    Pre-v0.11.17 mm-push rows have no ``local_emails`` key at all and
    contribute nothing. Post-v0.11.17 rows carry the emitting machine's
    locally-known emails. The union is the same on every machine after
    sync, so retros become deterministic across the fleet.

    Tolerant: bad shapes (non-list, non-string entries) are silently
    skipped. Lowercased and deduped at the entry level — the emitter
    already lowercases, but defense in depth keeps a misbehaving peer
    from poisoning the union with case-variant duplicates.
    """
    out: set[str] = set()
    for ev in events:
        if ev.get("type") != "mm-push":
            continue
        emails = ev.get("local_emails")
        if not isinstance(emails, list):
            continue
        for e in emails:
            if isinstance(e, str) and e:
                out.add(e.lower())
    return out


# ---------------------------------------------------------------------------
# Top-level orchestration.
# ---------------------------------------------------------------------------


def aggregate(
    *,
    events_dir: Path,
    window_days: int,
    author_emails: frozenset[str] | None,
    skill_usage_path: Path,
    now: datetime | None = None,
) -> RetroData:
    """Read every input source, aggregate per the locked rules, return the
    structured retro data. ``format_retro(data)`` renders it.

    ``author_emails`` semantics (v0.11.17 — fleet-wide trust set):

    * ``None`` — filter explicitly disabled. ``--no-author-filter``
      passes None. All commits in window are rendered.
    * non-None ``frozenset[str]`` — running machine's locally-known
      emails. The aggregator UNIONS this with every peer's
      ``local_emails`` from their ``mm-push`` event rows to build the
      fleet-wide trust set, then filters with the union. Pre-v0.11.17
      peers omit ``local_emails`` and contribute nothing to the union;
      the running machine's local set covers them via fallback.

    Two machines that have pushed-and-pulled produce identical retros
    because the fleet union is identical on every machine after sync.
    """
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=window_days)

    # Per-category skip counters. Discriminates mm-owned event parse errors
    # from gstack-owned analytics format issues so the tail breadcrumb can
    # name the affected file instead of conflating them under one count.
    skip_counter: dict[str, int] = {}

    events = list(_read_events(events_dir, skip_counter=skip_counter))
    skill_avail, skill_events = _read_skill_usage(skill_usage_path, skip_counter=skip_counter)

    # Fleet-wide trust set (v0.11.17): union every peer's ``local_emails``
    # field across all mm-push events on disk, then OR-in the running
    # machine's local set. Result is identical on every machine after
    # sync, so retros become deterministic across the fleet.
    if author_emails is None:
        effective_emails: frozenset[str] = frozenset()
    else:
        fleet_emails = aggregate_local_emails_from_events(events)
        effective_emails = frozenset(author_emails | fleet_emails)

    git = aggregate_git(events, since=since, until=until, author_emails=effective_emails)
    sessions = aggregate_sessions(events, since=since, until=until)
    pushes = aggregate_pushes(events, since=since, until=until)
    skills = aggregate_skills(skill_avail, skill_events, since=since, until=until)

    devices_known, devices_known_list = get_known_devices()

    # All event-producing devices in the window — pre-filter superset.
    raw_devices_in_events: set[str] = set(pushes.devices_with_pushes)
    for ev in events:
        d = ev.get("device")
        if isinstance(d, str) and d and _within_window(ev.get("ts"), since, until):
            raw_devices_in_events.add(d)

    # Phantom-event filter (post-v0.11.11). Phantom event files persist for
    # the 90-day retention window after a device is de-registered (or after
    # a leaked test creates a synthetic device id), and pre-filter would
    # surface them as "Activity from N machines (M registered)" — the user-
    # facing complaint the v0.11.10 test-isolation fix did NOT address.
    # When `mm devices --format=json` succeeds, intersect to currently-
    # registered IDs (gate on `devices_known is not None`, not on the list
    # being non-empty — a successful read against a zeroed fleet should
    # still filter, not fall through to the raw set). Stale event files
    # age out via the existing 90-day TTL in `_gc_old_event_files`. Falls
    # back to the raw set only when the registry is unavailable so a
    # transient `mm devices` failure never zeros the retro.
    if devices_known is not None:
        registered_ids = {
            d.get("device_id")
            for d in devices_known_list
            if isinstance(d, dict) and isinstance(d.get("device_id"), str)
        }
        devices_in_events = raw_devices_in_events & registered_ids
        unregistered = len(raw_devices_in_events - registered_ids)
    else:
        devices_in_events = raw_devices_in_events
        unregistered = 0

    return RetroData(
        window_days=window_days,
        since=since,
        until=until,
        git=git,
        sessions=sessions,
        pushes=pushes,
        skills=skills,
        fleet=FleetState(
            devices_in_events=devices_in_events,
            devices_known=devices_known,
            devices_known_list=devices_known_list,
            unregistered_event_devices=unregistered,
        ),
        skipped_per_source=dict(skip_counter),
        skipped_lines=sum(skip_counter.values()),
        skill_usage_path=skill_usage_path,
        window_exceeds_retention=window_days > EVENTS_RETENTION_DAYS,
    )


# ---------------------------------------------------------------------------
# Markdown renderer — locked output format per docs/designs/fleet-retro.md.
# ---------------------------------------------------------------------------


_REPO_URL_MAX_LEN = 60
"""Length threshold above which ``_shorten_repo_url`` compresses middle path
segments to ``[...]``. Enterprise-style URLs that embed UUID-shaped
repository identifiers in the path can clock in at ~135 chars and leak
identifying context into retros that may be shared publicly; typical
``github.com/org/repo`` URLs are well under 30 and pass through unchanged."""

_REPO_URL_MIN_SEGMENTS_TO_COMPRESS = 4
"""Minimum number of slash-separated parts (host + path segments) required to
trigger middle compression. A canonical 2-path-segment URL like
``github.com/org/repo`` has 3 parts and is therefore exempt regardless of
length — there's nothing meaningful to compress between host and last segment.
``gitlab.com/group/subgroup/repo`` (3 path segments / 4 parts) is the first
shape that compresses, and only if it also exceeds the length threshold."""


def _shorten_repo_url(canonical: str, max_len: int = _REPO_URL_MAX_LEN) -> str:
    """Render-only compression of long canonical URLs.

    Returns ``canonical`` unchanged when under ``max_len`` OR when the path has
    fewer than 3 segments after the host (covers GitHub / Bitbucket / basic
    GitLab regardless of length — those have a single ``org/repo`` path and
    nothing meaningful to compress between host and last segment). Otherwise
    returns ``<host>/[...]/<last-segment>``. The canonical URL itself is
    preserved as the dedup key in ``repos_by_count`` — this only affects the
    markdown rendered by ``format_retro``.
    """
    if not canonical or len(canonical) <= max_len:
        return canonical
    parts = canonical.split("/")
    if len(parts) < _REPO_URL_MIN_SEGMENTS_TO_COMPRESS:
        return canonical
    return f"{parts[0]}/[...]/{parts[-1]}"


def _format_token_count(n: int) -> str:
    """Compact token count.

    ``8_800_000_000`` → ``8.8B``,
    ``12_400_000`` → ``12.4M``,
    ``142_000`` → ``142k``,
    ``898`` → ``898``.
    One decimal place at each scale boundary."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _render_token_block(lines: list[str], sessions: SessionsAggregate) -> None:
    """Append the v0.11.14+ token-usage block to ``lines``. Renders ONLY when
    the fleet has any token data this window — otherwise no-op (clean
    fresh-fleet output).

    Format (4 lines of data + 1 caveat footer):

      - Tokens this window: 12.4M in / 87.3M cache_read / 142k out
      - Cache hit ratio:    87%
      - Estimated cost:     ~$24.10 (Sonnet $18, Opus $6)
      - Per-model:          Sonnet, Opus
      - *Cost estimates do not account for subscription plan pricing.*
    """
    from mind_meld.token_usage import SUBSCRIPTION_CAVEAT, estimate_cost

    total_in = sessions.tokens_input
    total_cc = sessions.tokens_cache_create
    total_cr = sessions.tokens_cache_read
    total_out = sessions.tokens_output
    total_all = total_in + total_cc + total_cr + total_out
    if total_all == 0:
        return  # no token data — hide block entirely

    lines.append(
        f"- Tokens this window: {_format_token_count(total_in)} in / "
        f"{_format_token_count(total_cr)} cache_read / "
        f"{_format_token_count(total_out)} out"
    )

    # Cache hit ratio = cache_read / (cache_read + cache_create + input).
    # Output tokens are excluded — they're produced, not consumed via cache.
    consumed = total_cr + total_cc + total_in
    if consumed > 0:
        hit_ratio = total_cr / consumed
        lines.append(f"- Cache hit ratio:    {hit_ratio:.0%}")

    total_cost, per_model_cost = estimate_cost(sessions.tokens_by_model)
    if total_cost > 0:
        # Sort per-model by cost descending; render compact "Sonnet $X, Opus $Y".
        per_model_sorted = sorted(per_model_cost.items(), key=lambda kv: kv[1], reverse=True)
        per_model_str = ", ".join(
            f"{_short_model_name(m)} ${c:,.0f}" for m, c in per_model_sorted if c >= 1.0
        )
        cost_line = f"- Estimated cost:     ~${total_cost:,.2f}"
        if per_model_str:
            cost_line += f" ({per_model_str})"
        lines.append(cost_line)

    # Per-model session-name breadcrumb (which model families were active).
    # Filter out <synthetic> — Claude Code internal turns aren't a user-
    # facing model choice; including it in the list confuses the user.
    active_models = sorted(m for m in sessions.tokens_by_model.keys() if m != "<synthetic>")
    if active_models:
        short_names = ", ".join(_short_model_name(m) for m in active_models)
        lines.append(f"- Per-model:          {short_names}")

    lines.append(f"- *{SUBSCRIPTION_CAVEAT}*")


def _short_model_name(model: str) -> str:
    """Compact model id for render — ``claude-opus-4-7`` → ``Opus 4.7``,
    ``claude-sonnet-4-6`` → ``Sonnet 4.6``, ``<synthetic>`` → ``synthetic``.

    Unknown shapes are sanitized via ``safety.safe_str`` before render —
    ``model`` strings cross the sync boundary (peer's Claude Code jsonl →
    SessionMetadata.tokens_by_day.by_model → mm-events file → this peer's
    aggregator output → LLM-consumed markdown). A locally-compromised
    Claude Code on peer A could plant a model string containing markdown
    control chars or terminal escapes; without sanitization those would
    flow into the rendered retro. Mirrors the v0.10.1 trust-boundary
    sweep that ``safety.py`` was extracted to centralize."""
    if not isinstance(model, str):
        return ""
    if model == "<synthetic>":
        return "synthetic"
    parts = model.split("-")
    if len(parts) >= 4 and parts[0] == "claude":
        # Family/version both come from the peer-controlled string but
        # are bucketed into known character classes by the split — still
        # defang via safe_str to defend against future schema drift.
        family = _safe_short(parts[1].capitalize())
        version = _safe_short(".".join(parts[2:4]))
        return f"{family} {version}"
    return _safe_short(model)


def _safe_short(s: str) -> str:
    """Strip terminal escapes + Rich markup, then bucket to a conservative
    char class for markdown safety."""
    from mind_meld.safety import safe_str

    cleaned = safe_str(s) if isinstance(s, str) else ""
    # Whitelist: alphanumerics, dots, dashes, underscores, parens, spaces.
    # Anything else (newlines, backticks, angle brackets, pipes) becomes "_".
    return re.sub(r"[^A-Za-z0-9._\-() ]", "_", cleaned)


def _safe_repo_url(s: str) -> str:
    """Strip terminal escapes + bucket to a URL-safe char class.

    Same trust-boundary class as ``_safe_short``: ``canonical_remote_url``
    is peer-controlled (each peer's ``git remote get-url origin`` flows
    through ``canonicalize_remote_url`` into ``GitAggregate.repos_by_count``
    and out into the LLM-consumed retro markdown). v0.11.14 closed this
    gap for model strings; repo URLs were the residual surface.

    Whitelist allows valid canonical-URL characters
    (alphanumerics, ``.`` ``-`` ``/`` ``_`` ``~``); anything else
    — newlines, backticks, angle brackets, pipes, square brackets,
    surviving control bytes — becomes ``_``."""
    from mind_meld.safety import strip_terminal_escapes

    cleaned = strip_terminal_escapes(s) if isinstance(s, str) else ""
    return re.sub(r"[^A-Za-z0-9._\-/~]", "_", cleaned)


def format_retro(data: RetroData) -> str:
    """Render the markdown retro. Output is paste-ready for iMessage / email
    — single-message length when realistic data is present.

    Section layout (post-v0.11.12 polish):

    * Header — date range + activity-across-N-machines line. No inline notes.
    * Code shipped — commits, LOC, top repos as a sub-bulleted list.
    * Claude Code activity — sessions and project count only. Token usage
      is a deferred follow-up.
    * Skills used — this-machine-only invocation rollup.
    * mm sync activity — push counts.
    * Notes — every aside (fleet-incomplete, pre-v2 peers, parse errors,
      retention, phantom-event count, ephemeral split, discovery errors)
      consolidated. Section omitted entirely when there's nothing to note.
    """
    lines: list[str] = []
    notes: list[str] = []

    lines.append(
        f"# Retro: {data.since.date().isoformat()} → {data.until.date().isoformat()} "
        f"({data.window_days}d)"
    )
    lines.append("")

    # Activity-across-N-machines header. Phantom events are filtered out at
    # aggregate() so the count reflects the active fleet.
    n_in_events = len(data.fleet.devices_in_events)
    if data.fleet.devices_known is not None:
        m_known = data.fleet.devices_known
        lines.append(f"**Activity across {n_in_events} of {m_known} known machines**")
        if n_in_events < m_known:
            missing = m_known - n_in_events
            notes.append(
                f"Fleet incomplete: {missing} registered device(s) haven't pushed "
                f"events in this window."
            )
    else:
        lines.append(f"**Activity across {n_in_events} machine(s)**")
        notes.append("Known-fleet count unavailable (`mm devices --format=json` failed).")
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
        lines.append("- Top repos:")
        for r, n in top_repos:
            # Defang BEFORE shorten: ``_shorten_repo_url`` adds a trusted
            # ``[...]`` placeholder that the URL-safe whitelist would
            # otherwise bucket to ``_..._``.
            lines.append(f"  - {_shorten_repo_url(_safe_repo_url(r))} ({n})")
    lines.append("")

    # Claude Code activity. Per-user feedback v0.11.12: drop MB total,
    # "counted separately" parenthetical, and Most active list — they were
    # noise. v0.11.14 adds the token-usage block (raw counts, cache hit
    # ratio, cost equivalent) when the fleet has any token data; the
    # subscription caveat is the closing footer.
    lines.append("## Claude Code activity")
    if data.sessions.total_sessions == 0 and not data.sessions.pre_v2_peers:
        lines.append("- No Claude Code sessions captured in this window.")
    else:
        lines.append(
            f"- {data.sessions.total_sessions} sessions across {data.sessions.projects} projects"
        )
        if data.sessions.ephemeral_sessions:
            notes.append(
                f"{data.sessions.ephemeral_sessions} of those sessions are in ephemeral "
                f"Conductor workspaces."
            )
        # Token block — only renders when fleet has any token data. Hides
        # cleanly on a fresh fleet so empty zeros don't pollute output.
        _render_token_block(lines, data.sessions)
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

    # mm sync activity.
    lines.append("## mm sync activity")
    lines.append(
        f"- {data.pushes.push_events} pushes across "
        f"{len(data.pushes.devices_with_pushes)} device(s)"
    )
    lines.append("")

    # Collect remaining notes — most live in this block (rather than in
    # render-site appends above) because they describe data quality, not
    # activity. Order: data-trust signals first, then visibility/diagnostic.
    if data.sessions.pre_v2_peers:
        n_pre = len(data.sessions.pre_v2_peers)
        notes.append(
            f"Sessions count incomplete: {n_pre} peer(s) on pre-v0.11.0 — upgrade for "
            f"accurate session totals."
        )
    if data.sessions.pre_token_peers:
        n_token = len(data.sessions.pre_token_peers)
        notes.append(
            f"Tokens incomplete: {n_token} peer(s) on pre-v0.11.14 OR with cold token "
            f"cache — upgrade and/or run `mm push` on those machines for accurate "
            f"token totals."
        )
    if data.fleet.unregistered_event_devices:
        notes.append(
            f"{data.fleet.unregistered_event_devices} unregistered device id(s) had "
            f"events in this window (filtered out). Stale event files reap automatically "
            f"after {EVENTS_RETENTION_DAYS} days."
        )
    if data.pushes.discovery_errors:
        notes.append(
            f"{len(data.pushes.discovery_errors)} discovery error(s) recorded — see "
            f"mm: notice: stderr breadcrumbs."
        )
    n_events = data.skipped_per_source.get(SKIP_CATEGORY_EVENTS, 0)
    n_skill = data.skipped_per_source.get(SKIP_CATEGORY_SKILL_USAGE, 0)
    if n_events:
        notes.append(
            f"{n_events} event(s) skipped due to parse errors in mm event log. "
            f"Output may be incomplete."
        )
    if n_skill:
        skill_label = str(data.skill_usage_path) if data.skill_usage_path else "skill-usage.jsonl"
        notes.append(
            f"{n_skill} record(s) skipped in {skill_label} (gstack file format issue, not mm)."
        )
    # Backward-compat fallback for foreign callers that set skipped_lines
    # without populating skipped_per_source. aggregate() always populates
    # per-source, so this only fires for hand-built RetroData.
    if not data.skipped_per_source and data.skipped_lines:
        notes.append(
            f"{data.skipped_lines} record(s) skipped due to parse errors. Output may be incomplete."
        )
    if data.window_exceeds_retention:
        notes.append(
            f"Requested {data.window_days}d window exceeds the {EVENTS_RETENTION_DAYS}-day "
            f"events retention. Older days are reaped by `mm gc` and will not appear."
        )

    if notes:
        lines.append("## Notes")
        for n in notes:
            lines.append(f"- {n}")
        lines.append("")

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
    # ``None`` means filter is explicitly disabled (post-v0.11.17 semantics);
    # an empty frozenset would still be unioned with fleet emails inside
    # ``aggregate``. Wire ``--no-author-filter`` to None so the user's
    # intent (render every commit) survives the union.
    author_emails: frozenset[str] | None = None if args.no_author_filter else gather_author_emails()
    data = aggregate(
        events_dir=events_dir,
        window_days=args.window,
        author_emails=author_emails,
        skill_usage_path=GSTACK_ANALYTICS_DIR / "skill-usage.jsonl",
    )
    sys.stdout.write(format_retro(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
