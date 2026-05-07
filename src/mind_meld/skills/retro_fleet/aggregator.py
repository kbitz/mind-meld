"""Fleet-aware retro aggregator (Group 8 / Track 8A).

Reads mm-owned event JSONLs from every fleet device and produces a
glanceable markdown retro mirroring the gstack ``/retro`` shape.
Imported as ``mind_meld.skills.retro_fleet.aggregator``; the public CLI
surface is ``mm retro-fleet <window>`` (typer wrapper in
``cli.py:retro_fleet_cmd``). Direct ``python -m
mind_meld.skills.retro_fleet.aggregator <window>`` works from a development
checkout but is not what the SKILL.md or documented invocations use — pipx
installs put mind_meld in an isolated venv that bare ``python`` can't reach.

Inputs (all tolerant of missing / corrupt / unknown-field files):

* ``$MM_EVENTS_DIR`` (or ``~/.local/share/mind-meld/events``) — fleet events
  written by ``_run_events_tail`` on every push. v=2 sessions snapshots are
  full inventory (Group 8); v=1 are delta-semantic relics from pre-v0.11.0
  peers (surfaced in the Notes section, not summed into totals). v=2
  snapshots from v0.11.27+ peers carry ``skills_by_day`` per project;
  earlier v=2 peers omit it (surfaced as ``pre_skills_peers``).
* ``mm devices --format=json`` (subprocess) — for the "N of M known machines"
  header AND the phantom-event filter (see ``aggregate``). Failure degrades
  to all-events-counted with a "known-fleet count unavailable" note.

Aggregation rules:

* Git: dedup by ``(canonicalize_remote_url(remote), sha)``; sum LOC; group by
  repo for top-N.
* Sessions: pick LATEST v=2 snapshot per ``(device, source_root, claude_dir)``;
  sum across tuples. v=1 snapshots are NOT summed.
* Skills: walk the same latest-per-tuple set; merge each project's
  ``skills_by_day`` into a fleet-wide rollup, sliced to the retro window.
  Pre-v0.11.27 mm peers (``skills_by_day`` key absent on snapshot rows)
  are flagged into ``pre_skills_peers``; peers with the key present but
  empty (``{}``) are NOT flagged — empty signals "no Skill usage", not
  "peer doesn't emit" (D4 from /plan-eng-review 2026-05-06).
* mm-push: count by (device).
* Phantom-event filter: when ``mm devices --format=json`` succeeds, intersect
  event-producing IDs with the registered fleet so de-registered or test-
  leaked phantom IDs fall out of the rendered count. Stale event files age
  out via the existing 90-day retention.

Visible-failure contract: data-quality and diagnostic asides are
consolidated into the tail Notes section so the user sees them in one place
rather than scattered across each section's body.

Pre-v0.11.27 the Skills section read ``~/.gstack/analytics/skill-usage.jsonl``
locally and rendered "this machine only". That source was hostage to gstack's
analytics writer (which silently broke for the user 2026-04-26..2026-05-06)
and never aggregated across machines. Replaced by Claude Code session jsonls
which Claude Code writes as a side effect of running the Skill tool — same
data, fleet-wide, no separate writer to break.
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

DEFAULT_RETROS_DIR = Path("~/.local/share/mind-meld/retros").expanduser()
"""Local-only snapshot directory. NOT synced — retros are deterministic
across the fleet (post-v0.11.17 union filter), so a local cache suffices
for "trends vs last retro" deltas without the complexity of cross-fleet
snapshot reconciliation. Files: ``YYYY-MM-DD-N.json`` (sequence per day)."""

RETROS_RETENTION_DAYS = 365
"""Snapshot retention. Year-long ceiling — older snapshots aren't load-
bearing for any current trend computation (only the most recent matching-
window prior is consulted) but are preserved for the user's own forensic
use. Pruned best-effort on each save."""

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
TOP_N_HOURS = 5
"""Hourly histogram peak rows shown in markdown. The full 24-row table is
noise; the LLM gets enough signal from the top-N peaks to interpret
when-they-code patterns."""

CARD_WIDTH = 64
"""ASCII card total width including borders. Sized so a typical
``8 commits · 4 PRs · 2 repos · 2 machines`` line + a 50-char theme
bullet fits without truncation. Card contents are padded to this width
by ``_render_ascii_card`` so the right border aligns regardless of
LLM-supplied content."""

MAX_THEMES = 3
"""Cap on TOP WORK theme bullets in the card. SKILL.md asks the LLM
for "up to 3" — this enforces it at render time so a misbehaving caller
can't blow up the card height."""

CARD_INNER_WIDTH = CARD_WIDTH - 6  # ║ + 2 spaces + content + 2 spaces + ║ = 6
"""Usable content width inside the card. Themes/noteworthy strings
longer than this are truncated with an ellipsis suffix at render time."""


# ---------------------------------------------------------------------------
# Dataclasses — everything is structured so format_retro() can render
# deterministically and tests can assert on per-section values.
# ---------------------------------------------------------------------------


@dataclass
class CommitTypes:
    """Conventional-commit prefix breakdown. Anything that doesn't match
    a known prefix lands in ``other``. ``total`` mirrors ``GitAggregate.
    commits`` (kept here so percent rendering stays self-contained)."""

    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0


@dataclass
class CommitBursts:
    """45-min-gap clustering of commit timestamps. Bursts are clusters of
    commits separated by ≥45 minutes of inactivity; idleness mid-session
    (lunch, deep think, code-reading without commits) splits one cognitive
    session into two recorded bursts. Renamed from "sessions" to avoid
    collision with Claude Code "sessions" we already count and to set
    honest expectations: this counts commit clusters, not cognitive flow."""

    burst_count: int = 0
    deep: int = 0  # ≥50 min span
    medium: int = 0  # 20-50 min span
    micro: int = 0  # <20 min span (often one-shot commits)
    avg_minutes: float = 0.0


@dataclass
class ShipOfWeek:
    """Single highest-LOC commit in window. Purely deterministic — the
    LLM picks up the data and synthesizes the surrounding narrative."""

    repo: str = ""
    sha: str = ""
    subject: str = ""
    additions: int = 0
    deletions: int = 0
    has_data: bool = False


@dataclass
class WeeklyBucket:
    """One 7-day bucket inside a ≥14d window. ``week_start`` is the local
    YYYY-MM-DD anchor (Monday-aligned)."""

    week_start: str
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    active_days: int = 0


@dataclass
class GitAggregate:
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    repos_by_count: dict[str, int] = field(default_factory=dict)
    # Consecutive local-day commit streak ending at (or one day before)
    # ``until``. Computed from the FULL events buffer regardless of the
    # retro window — a 7d retro on a 30-day streak shows 30. Capped in
    # practice by the 90d events retention.
    streak_days: int = 0
    # Conventional-commit prefix mix.
    commit_types: CommitTypes = field(default_factory=CommitTypes)
    # 24-hour histogram in local time (key = hour 0..23).
    hourly: dict[int, int] = field(default_factory=dict)
    # Burst clustering — see CommitBursts docstring for noise caveat.
    bursts: CommitBursts = field(default_factory=CommitBursts)
    # Single biggest commit by (additions + deletions).
    ship: ShipOfWeek = field(default_factory=ShipOfWeek)
    # Empty unless window_days >= 14. Sorted oldest -> newest.
    weekly: list[WeeklyBucket] = field(default_factory=list)


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
    # ``available`` flips False ONLY when no peer's snapshot in the window
    # carries a ``skills_by_day`` key (every project on every device omits
    # the field — the fleet has not yet rolled to v0.11.27+ on any
    # contributing machine). Renderer emits "Skills section omitted" in
    # that state instead of "0 invocations". v0.11.27+ semantic:
    # "available=False" is mid-rollout-with-zero-uptake, NOT "tool
    # missing" as it was pre-v0.11.27.
    available: bool = True
    # Devices whose snapshot rows are missing the ``skills_by_day`` key
    # entirely (pre-v0.11.27 mm peers). KEY-ABSENT-vs-EMPTY-DICT is the
    # discriminator (D4 from /plan-eng-review 2026-05-06): empty dict
    # means "this project has sessions but no Skill blocks" — a content
    # signal, not a version signal — and does NOT flag the device.
    pre_skills_peers: set[str] = field(default_factory=set)


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


# Skip-counter category key. Kept as a constant so call-sites stay
# consistent and `format_retro` can map to a specific breadcrumb
# message. "events" covers mm-owned event JSONLs (real data quality
# signal). The pre-v0.11.27 ``skill_usage`` category was retired with
# the gstack-analytics reader.
SKIP_CATEGORY_EVENTS = "events"


@dataclass
class PriorRetroDelta:
    """Compact deltas between this retro and the most recent prior snapshot
    that ran with the same ``window_days``. Numbers are absolute deltas
    (now - prior); negative values mean a metric dropped. ``has_prior``
    flips True only when a matching snapshot was loaded — first-run retros
    skip the section."""

    has_prior: bool = False
    prior_date: str = ""
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    sessions: int = 0
    tokens_total: int = 0
    push_events: int = 0
    streak_days: int = 0


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
    # Per-category skip counters. Today only ``events`` (mm-owned event
    # parse errors). Pre-v0.11.27 also tracked ``skill_usage`` (gstack
    # analytics format issues) — retired with the gstack reader.
    # ``format_retro`` renders one breadcrumb per non-zero entry.
    skipped_per_source: dict[str, int] = field(default_factory=dict)
    # Backwards-compat summed view. Equals sum(skipped_per_source.values()).
    # Pre-existing tests assert on this field; new tests should drill into
    # skipped_per_source to verify category-specific behavior.
    skipped_lines: int = 0
    window_exceeds_retention: bool = False  # TODO#2 visible-failure breadcrumb
    # Trend deltas vs most recent matching-window snapshot. Populated by
    # ``main()`` from disk; unset (has_prior=False) for the first retro of
    # a given window or when the snapshot dir doesn't exist yet.
    prior: PriorRetroDelta = field(default_factory=PriorRetroDelta)


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


# Conventional-commit prefixes we surface explicitly. Anything else
# (including no prefix) lands in ``other``. Order matters: the prefix
# ``feat!`` (breaking change) and scoped variants like ``fix(cli):``
# both reduce to the bare keyword via ``_classify_commit_subject``.
_COMMIT_TYPE_KEYWORDS: tuple[str, ...] = (
    "feat",
    "fix",
    "refactor",
    "test",
    "chore",
    "docs",
    "perf",
    "style",
    "build",
    "ci",
    "revert",
)


_COMMIT_TYPE_RE = re.compile(r"^([a-z]+)(?:\([^)]*\))?!?:")


_CLASSIFY_LEN_CAP = 256
"""Conventional-commit prefixes live in the first ~30 chars of any
realistic subject. Cap at 256 to defend against pathological peer
subjects burning CPU on ``.lower()``/``.strip()``/regex over MB of
text. The classifier only inspects the prefix anyway."""


def _classify_commit_subject(subject: object) -> str:
    """Return the conventional-commit keyword (``feat``/``fix``/...) or
    ``other``. Tolerant of non-string / empty / unprefixed subjects.
    Inspects only the first ``_CLASSIFY_LEN_CAP`` chars."""
    if not isinstance(subject, str) or not subject:
        return "other"
    m = _COMMIT_TYPE_RE.match(subject[:_CLASSIFY_LEN_CAP].strip().lower())
    if m is None:
        return "other"
    kw = m.group(1)
    return kw if kw in _COMMIT_TYPE_KEYWORDS else "other"


_BURST_GAP_MINUTES = 45
"""Threshold for splitting commits into bursts. Industry-standard 45-min
gap; smaller values over-split, larger values stitch lunch-then-resume
back together. See gstack /retro for the precedent."""


def _classify_burst_size(span_minutes: float) -> str:
    """Bucket a burst's span into deep / medium / micro. Single-commit
    bursts have span 0 and land in ``micro`` — these are typically
    fire-and-forget commits (chore, version bump, hotfix)."""
    if span_minutes >= 50:
        return "deep"
    if span_minutes >= 20:
        return "medium"
    return "micro"


def _detect_bursts(commit_dts: list[datetime]) -> CommitBursts:
    """45-min-gap clustering. ``commit_dts`` MUST be the windowed,
    deduped, author-filtered set; the caller already applied those
    filters before passing them in.

    Honest framing: this counts commit clusters, not cognitive sessions.
    A real coding session that stops for lunch / debugging without
    commits / deep think will fragment into multiple bursts here. We
    accept the noise rather than adding speculative session-stitching
    heuristics; the LLM can interpret the number with that caveat in
    mind via the SKILL.md tone block."""
    out = CommitBursts()
    if not commit_dts:
        return out
    sorted_dts = sorted(commit_dts)
    burst_starts: list[datetime] = [sorted_dts[0]]
    burst_ends: list[datetime] = [sorted_dts[0]]
    gap = timedelta(minutes=_BURST_GAP_MINUTES)
    for prev, cur in zip(sorted_dts, sorted_dts[1:]):
        if cur - prev > gap:
            burst_starts.append(cur)
            burst_ends.append(cur)
        else:
            burst_ends[-1] = cur
    spans_minutes = [(e - s).total_seconds() / 60 for s, e in zip(burst_starts, burst_ends)]
    out.burst_count = len(spans_minutes)
    for span in spans_minutes:
        bucket = _classify_burst_size(span)
        if bucket == "deep":
            out.deep += 1
        elif bucket == "medium":
            out.medium += 1
        else:
            out.micro += 1
    out.avg_minutes = sum(spans_minutes) / len(spans_minutes) if spans_minutes else 0.0
    return out


def _monday_of(d: datetime) -> str:
    """Local Monday ISO date for the week containing ``d``."""
    local = d.astimezone().date()
    return (local - timedelta(days=local.weekday())).isoformat()


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
    window_days: int = 0,
) -> GitAggregate:
    """Walk git-snapshot events, dedup commits by ``(canonical, sha)``,
    apply the window + author filter, return totals.

    ``author_emails`` may be empty/None to disable the filter.

    Streak collection runs in the same loop but bypasses the window filter
    so the rendered streak reflects current state, not the retro window.
    Author filter still applies — a third-party PR-merge commit shouldn't
    keep the user's personal streak alive.

    ``window_days`` is consulted ONLY for whether to emit weekly buckets
    (skipped when <14d); zero is fine for the typical 7d retro path.
    """
    canonicalize = _import_canonicalize()
    seen_keys: set[tuple[str, str]] = set()
    streak_seen: set[tuple[str, str]] = set()
    streak_days_set: set[str] = set()
    out = GitAggregate()
    burst_dts: list[datetime] = []
    weekly_by_start: dict[str, WeeklyBucket] = {}
    weekly_active_days: dict[str, set[str]] = {}
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
                commit_dt = _parse_iso(c.get("date"))
                if commit_dt is None:
                    continue
                if author_emails:
                    ae = c.get("author_email")
                    if not isinstance(ae, str) or ae.lower() not in author_emails:
                        continue
                key = (remote, sha)

                # Streak: collect unique commit-days across the entire
                # events buffer, deduped fleet-wide via (remote, sha).
                if key not in streak_seen:
                    streak_seen.add(key)
                    streak_days_set.add(_local_day_iso(commit_dt))

                # Windowed totals.
                if not (since <= commit_dt <= until):
                    continue
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                add = _safe_int(c.get("add"))
                dlt = _safe_int(c.get("del"))
                out.commits += 1
                out.additions += add
                out.deletions += dlt
                if remote:
                    out.repos_by_count[remote] = out.repos_by_count.get(remote, 0) + 1

                # Commit-type mix.
                kw = _classify_commit_subject(c.get("subject"))
                out.commit_types.counts[kw] = out.commit_types.counts.get(kw, 0) + 1
                out.commit_types.total += 1

                # Hourly distribution (local time).
                local_hour = commit_dt.astimezone().hour
                out.hourly[local_hour] = out.hourly.get(local_hour, 0) + 1

                # Burst clustering input.
                burst_dts.append(commit_dt)

                # Ship of the week — biggest single commit by add+del.
                size = add + dlt
                ship = out.ship
                if not ship.has_data or size > (ship.additions + ship.deletions):
                    subject_raw = c.get("subject")
                    out.ship = ShipOfWeek(
                        repo=remote,
                        sha=sha[:7] if isinstance(sha, str) else "",
                        subject=subject_raw if isinstance(subject_raw, str) else "",
                        additions=add,
                        deletions=dlt,
                        has_data=True,
                    )

                # Weekly bucket (only used when window_days >= 14).
                if window_days >= 14:
                    week_start = _monday_of(commit_dt)
                    bucket = weekly_by_start.setdefault(week_start, WeeklyBucket(week_start))
                    bucket.commits += 1
                    bucket.additions += add
                    bucket.deletions += dlt
                    days_set = weekly_active_days.setdefault(week_start, set())
                    days_set.add(_local_day_iso(commit_dt))
    out.streak_days = _compute_streak(streak_days_set, until)
    out.bursts = _detect_bursts(burst_dts)
    if window_days >= 14:
        for week_start, bucket in weekly_by_start.items():
            bucket.active_days = len(weekly_active_days.get(week_start, set()))
        out.weekly = sorted(weekly_by_start.values(), key=lambda b: b.week_start)
    return out


def _local_day_iso(dt: datetime) -> str:
    """``YYYY-MM-DD`` in the system's local timezone. Used for streak day
    keys so a late-night commit shows up "today" instead of leaking into
    "tomorrow" via UTC drift."""
    return dt.astimezone().date().isoformat()


def _compute_streak(commit_days: set[str], until: datetime) -> int:
    """Consecutive local days ending at or just before ``until`` with at
    least one commit. GitHub-style grace day: if today has no commits but
    yesterday does, start counting from yesterday so an in-progress day
    doesn't break the streak."""
    if not commit_days:
        return 0
    cursor = until.astimezone().date()
    if cursor.isoformat() not in commit_days:
        cursor = cursor - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in commit_days:
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak


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
) -> tuple[SessionsAggregate, SkillsAggregate]:
    """Pick the LATEST v=2 sessions-snapshot per (device, source_root,
    claude_dir) within the window, then filter to projects whose
    ``last_session_at`` falls inside the window — sum across that filtered
    set. v=1 snapshots flag the device as pre-v2 but contribute zero to
    totals.

    Returns ``(SessionsAggregate, SkillsAggregate)`` since both views are
    derived from the same per-project iteration: token / session counts
    on the sessions side, ``skills_by_day`` rollup on the skills side.
    Splitting the iteration would double the work for no benefit (D5 from
    /plan-eng-review 2026-05-06).

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
    # ``available`` flips True the moment any project carries the
    # ``skills_by_day`` key (regardless of whether the dict is empty);
    # stays False when EVERY contributing project lacks the field
    # (whole-fleet pre-v0.11.27). Renderer uses this to switch between
    # "Skills section omitted" and the rendered counts.
    skills = SkillsAggregate(available=False)
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

        # Skill aggregation (v0.11.27+). KEY-ABSENT-vs-EMPTY-DICT is the
        # discriminator (D4 from /plan-eng-review 2026-05-06). Absent
        # ⇒ peer on pre-v0.11.27 mm; empty ⇒ "no Skill usage in window"
        # which is a content signal, not a version signal.
        if "skills_by_day" not in proj:
            skills.pre_skills_peers.add(device)
        else:
            skills.available = True  # at least one project ships the field
            skills_by_day = proj["skills_by_day"]
            if isinstance(skills_by_day, dict):
                _merge_skill_window(skills, skills_by_day, since=since, until=until)

    return out, skills


def _merge_skill_window(
    out: SkillsAggregate,
    skills_by_day: dict,
    *,
    since: datetime,
    until: datetime,
) -> None:
    """Sum the day buckets whose YYYY-MM-DD key falls in [since, until]
    into ``out``'s skill fields. Honest "skills invoked THIS WINDOW"
    semantics — mirrors ``_merge_token_window``.

    Tolerant of every shape: non-string day keys, non-dict buckets, non-
    string skill names, non-int counts (coerced via ``_safe_int``).
    Skill name sanitization happens at RENDER time, not here — raw bytes
    must match across machines for fleet aggregation to be deterministic
    (the same trust-boundary placement as ``tokens_by_model`` / model id
    sanitization)."""
    since_d = since.astimezone(timezone.utc).date().isoformat()
    until_d = until.astimezone(timezone.utc).date().isoformat()
    for day_key, bucket in skills_by_day.items():
        if not isinstance(day_key, str) or not (since_d <= day_key <= until_d):
            continue
        if not isinstance(bucket, dict):
            continue
        for skill, count in bucket.items():
            if not isinstance(skill, str) or not skill:
                continue
            n = _safe_int(count)
            if n <= 0:
                continue
            out.invocations += n
            out.by_skill[skill] = out.by_skill.get(skill, 0) + n


def _merge_token_window(
    out: SessionsAggregate,
    tokens_by_day: dict,
    *,
    since: datetime,
    until: datetime,
) -> None:
    """Sum the day buckets whose YYYY-MM-DD key falls in [since, until] into
    ``out``'s token fields. Honest "tokens consumed THIS WINDOW" semantics
    (per /plan-eng-review D6 — codex caught the per-window accuracy gap).

    Top-level totals (``tokens_input`` / ``tokens_output`` / etc.) derive
    from per-model entries EXCLUDING ``COST_EXCLUDED_MODELS`` so the
    rendered "Tokens this window" line shares the same basis as the cost
    estimate. ``<synthetic>`` rows are Claude Code's internal tool-execution
    turns that don't actually call the API — they belong neither in cost
    nor in the user-facing total. ``tokens_by_model`` retains every peer-
    reported entry so the unpriced-model breadcrumb in ``format_retro``
    can surface volume that was excluded from the cost line."""
    from mind_meld.token_usage import COST_EXCLUDED_MODELS, zero_model_bucket

    since_d = since.astimezone(timezone.utc).date().isoformat()
    until_d = until.astimezone(timezone.utc).date().isoformat()
    for day_key, bucket in tokens_by_day.items():
        if not isinstance(day_key, str) or not (since_d <= day_key <= until_d):
            continue
        if not isinstance(bucket, dict):
            continue
        for model, mbucket in (bucket.get("by_model") or {}).items():
            if not isinstance(model, str) or not isinstance(mbucket, dict):
                continue
            # Bespoke loop kept (NOT merge_by_model from token_usage): peer-
            # controlled events cross a trust boundary, so every field flows
            # through `_safe_int`. token_usage's helper is for trusted local
            # data and intentionally skips that hardening. /plan-eng-review
            # cross-model tension #3 (2026-05-06).
            mtarget = out.tokens_by_model.setdefault(model, zero_model_bucket())
            in_ = _safe_int(mbucket.get("input"))
            cc = _safe_int(mbucket.get("cache_create"))
            cr = _safe_int(mbucket.get("cache_read"))
            outp = _safe_int(mbucket.get("output"))
            mtarget["input"] += in_
            mtarget["cache_create"] += cc
            mtarget["cache_read"] += cr
            mtarget["output"] += outp
            if model in COST_EXCLUDED_MODELS:
                continue
            out.tokens_input += in_
            out.tokens_cache_create += cc
            out.tokens_cache_read += cr
            out.tokens_output += outp


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
# Skills aggregation — see ``aggregate_sessions``: skills come from the
# same per-project iteration. v0.11.27+ removed the standalone
# ``aggregate_skills`` reader (was: gstack analytics file, this-machine-
# only). The fleet-wide source is each peer's ``skills_by_day`` field
# on the v=2 sessions-snapshot event.
# ---------------------------------------------------------------------------


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

    # Per-category skip counters. Today only ``events`` (mm-owned event
    # parse errors). The pre-v0.11.27 ``skill_usage`` category was
    # retired with the gstack-analytics reader.
    skip_counter: dict[str, int] = {}

    events = list(_read_events(events_dir, skip_counter=skip_counter))

    # Fleet-wide trust set (v0.11.17): union every peer's ``local_emails``
    # field across all mm-push events on disk, then OR-in the running
    # machine's local set. Result is identical on every machine after
    # sync, so retros become deterministic across the fleet.
    if author_emails is None:
        effective_emails: frozenset[str] = frozenset()
    else:
        fleet_emails = aggregate_local_emails_from_events(events)
        effective_emails = frozenset(author_emails | fleet_emails)

    git = aggregate_git(
        events,
        since=since,
        until=until,
        author_emails=effective_emails,
        window_days=window_days,
    )
    sessions, skills = aggregate_sessions(events, since=since, until=until)
    pushes = aggregate_pushes(events, since=since, until=until)

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


def _unpriced_token_summary(tokens_by_model: dict[str, dict[str, int]]) -> tuple[int, int]:
    """Return ``(total_tokens, model_count)`` for models present in the fleet
    data but absent from ``PRICING`` and not in ``COST_EXCLUDED_MODELS``.

    Same semantics as ``estimate_cost``'s skip path — if a model isn't
    priced, its tokens still count toward displayed totals but contribute
    zero to cost. Surfaced as a Notes line so the cost line is honestly
    flagged as a lower bound when older or unrecognized model ids show up
    in jsonls."""
    from mind_meld.token_usage import COST_EXCLUDED_MODELS, PRICING, TOKEN_FIELDS

    total = 0
    n = 0
    for model, mbucket in (tokens_by_model or {}).items():
        if model in PRICING or model in COST_EXCLUDED_MODELS:
            continue
        if not isinstance(mbucket, dict):
            continue
        for k in TOKEN_FIELDS:
            total += _safe_int(mbucket.get(k))
        n += 1
    return total, n


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
    char class for markdown safety. Use for SHORT identifiers (skill
    names, model names, sha) where conservative bucketing is fine.
    For prose-shaped strings (commit subjects, LLM-supplied themes) use
    ``_safe_prose`` instead — this whitelist mangles punctuation."""
    from mind_meld.safety import safe_str

    cleaned = safe_str(s) if isinstance(s, str) else ""
    # Whitelist: alphanumerics, dots, dashes, underscores, parens, spaces.
    # Anything else (newlines, backticks, angle brackets, pipes) becomes "_".
    return re.sub(r"[^A-Za-z0-9._\-() ]", "_", cleaned)


_PROSE_CTRL_RE = re.compile(
    # C0 controls + DEL.
    r"[\x00-\x1f\x7f]"
    # Unicode line/paragraph separators that markdown renderers may
    # honor as line breaks (NEL, line-sep, paragraph-sep).
    r"|[  ]"
    # BiDi formatting characters: LRE/RLE/PDF/LRO/RLO/LRI/RLI/FSI/PDI.
    # U+202E (RLO) is the canonical "reverse downstream rendering"
    # smuggling vector — a peer commit subject containing RLO flips
    # the visual order of subsequent text in pasted output (Slack,
    # Telegram, terminals). Defang at trust-boundary entry.
    r"|[‪-‮⁦-⁩]"
)
"""Strip C0 controls + DEL + Unicode line/paragraph separators + BiDi
formatting characters after ``safe_str`` to defend against newline /
tab / RTL-override smuggling that ``rich.markup.escape`` doesn't drop.
Belt-and-braces for prose strings rendered into single-line markdown
bullets."""

_PROSE_LEN_CAP = 4096
"""Cap commit subjects / LLM-supplied prose at 4 KiB before sanitization.
A peer-planted 10 MB subject would otherwise burn CPU + RAM in the
regex sub on every retro render. 4 KiB fits ~50 lines of typical commit
subject — a conservative ceiling well above any realistic message."""


def _safe_prose(s: str) -> str:
    """Defang escapes + Rich markup but preserve prose punctuation
    (colons, slashes, hashes, em-dashes). Use for commit subjects and
    LLM-supplied theme / noteworthy lines where readability matters and
    the conservative ``_safe_short`` whitelist would over-mangle.

    Strips BiDi formatting (U+202E and friends) and Unicode line/
    paragraph separators on top of ASCII C0/DEL — peer-controlled
    subjects can otherwise flip downstream rendered text or smuggle
    line breaks past the single-line bullet contract.

    Length-capped at ``_PROSE_LEN_CAP`` before sanitization so a
    pathologically long peer subject doesn't burn CPU on the regex."""
    from mind_meld.safety import safe_str

    if not isinstance(s, str):
        return ""
    if len(s) > _PROSE_LEN_CAP:
        s = s[:_PROSE_LEN_CAP]
    cleaned = safe_str(s)
    return _PROSE_CTRL_RE.sub(" ", cleaned)


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


def _truncate(s: str, max_len: int) -> str:
    """Truncate ``s`` to ``max_len`` characters, ending with ``…`` when
    a cut occurs. Idempotent for already-short strings."""
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return "…"
    return s[: max_len - 1] + "…"


def _card_line(content: str) -> str:
    """Pad ``content`` to the card's inner width and wrap with borders."""
    safe = _truncate(content, CARD_INNER_WIDTH)
    return f"║  {safe.ljust(CARD_INNER_WIDTH)}  ║"


def _format_loc_short(n: int) -> str:
    """Compact LOC formatting for the card. ``3247`` → ``3.2k``,
    ``142_000`` → ``142k``. Plain digit string under 1000."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n // 1_000}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _render_ascii_card(
    data: RetroData,
    *,
    name: str | None,
    themes: list[str],
    noteworthy: str,
) -> list[str]:
    """Render the screenshot-friendly ASCII card. Pure padding — every
    line is forced to the same width so the right border aligns. LLM-
    supplied strings (``themes``, ``noteworthy``, ``name``) are passed
    through ``_safe_short`` to defang stray control chars and through
    ``_truncate`` so an over-long entry doesn't blow the layout.

    Returns the lines (without trailing newlines) so the caller can
    interleave with the markdown body."""
    horizontal = "═" * (CARD_WIDTH - 2)
    out: list[str] = []
    out.append(f"╔{horizontal}╗")

    header_name = _safe_prose(name) if isinstance(name, str) and name else ""
    header_date = (
        f"{data.since.astimezone().date().isoformat()} → "
        f"{data.until.astimezone().date().isoformat()}"
    )
    if header_name:
        header = f"{header_name} · {header_date}"
    else:
        header = header_date
    out.append(_card_line(header))
    out.append(f"╠{horizontal}╣")

    n_devices = len(data.fleet.devices_in_events)
    machines_word = "machine" if n_devices == 1 else "machines"
    out.append(
        _card_line(
            f"{data.git.commits} commits · "
            f"{len(data.git.repos_by_count)} repos · "
            f"{n_devices} {machines_word}"
        )
    )
    streak_part = f" · {data.git.streak_days}-day streak" if data.git.streak_days > 0 else ""
    out.append(
        _card_line(
            f"+{_format_loc_short(data.git.additions)} / "
            f"-{_format_loc_short(data.git.deletions)} LOC{streak_part}"
        )
    )
    out.append(_card_line(""))

    if noteworthy:
        out.append(_card_line("NOTEWORTHY"))
        out.append(_card_line(_safe_prose(noteworthy)))
        out.append(_card_line(""))

    # Cap themes at MAX_THEMES — SKILL.md says "up to 3" but nothing
    # else enforces it; a power user (or a misbehaving LLM) passing 50
    # ``--theme`` flags would otherwise produce a 50-line card.
    if themes:
        out.append(_card_line("TOP WORK"))
        for theme in themes[:MAX_THEMES]:
            out.append(_card_line(f"• {_safe_prose(theme)}"))

    out.append(f"╚{horizontal}╝")
    return out


def _render_commit_types(commit_types: CommitTypes) -> list[str]:
    """Sorted-by-count commit-type breakdown, with percent. Shape:
    ``feat 12 (40%) · fix 8 (27%) · ...``. Single-line so it doesn't
    bloat the markdown."""
    if commit_types.total <= 0 or not commit_types.counts:
        return []
    items = sorted(commit_types.counts.items(), key=lambda kv: kv[1], reverse=True)
    parts = [f"{kw} {n} ({n / commit_types.total:.0%})" for kw, n in items if n > 0]
    return [f"- Mix: {' · '.join(parts)}"] if parts else []


def _render_hourly(hourly: dict[int, int]) -> list[str]:
    """Top-N peak hours with simple bar. Renders nothing on empty input.
    Hours rendered as zero-padded local-time HH:00."""
    if not hourly:
        return []
    items = sorted(hourly.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N_HOURS]
    if not items or items[0][1] == 0:
        return []
    peak_count = items[0][1]
    lines = ["- Peak hours (local time):"]
    for hour, n in sorted(items, key=lambda kv: kv[0]):
        bar_width = max(1, int(round(20 * n / peak_count)))
        lines.append(f"  - {hour:02d}:00  {n:>3}  {'█' * bar_width}")
    return lines


def _render_bursts(bursts: CommitBursts) -> list[str]:
    """One-line burst summary. Honest framing: 'commit bursts' not
    'sessions' — captures clusters separated by 45-min idleness."""
    if bursts.burst_count <= 0:
        return []
    return [
        f"- Commit bursts: {bursts.burst_count} "
        f"(deep {bursts.deep} · medium {bursts.medium} · micro {bursts.micro}) "
        f"· avg span {bursts.avg_minutes:.0f}min"
    ]


def _render_ship(ship: ShipOfWeek) -> list[str]:
    """Single highest-LOC commit. Subject is sanitized at render time —
    it crosses the trust boundary from peer-controlled events into
    LLM-consumed markdown. Prose-friendly defang preserves punctuation
    (``:`` / ``/`` / ``#``) that ``_safe_short`` would otherwise mangle."""
    if not ship.has_data:
        return []
    repo = _shorten_repo_url(_safe_repo_url(ship.repo)) if ship.repo else ""
    subject_safe = _safe_prose(ship.subject) if ship.subject else ""
    repo_part = f" in {repo}" if repo else ""
    sha_part = f" `{_safe_short(ship.sha)}`" if ship.sha else ""
    return [
        f"- Ship of the window:{sha_part}"
        f" +{ship.additions:,} / -{ship.deletions:,} LOC{repo_part}"
        f" — {subject_safe}"
    ]


def _render_weekly(weekly: list[WeeklyBucket]) -> list[str]:
    """Week-over-week breakdown for ≥14d windows. Markdown table."""
    if not weekly:
        return []
    lines = [
        "- Week-over-week:",
        "  | Week of    | Commits | +LOC    | -LOC   | Active days |",
        "  |------------|--------:|--------:|-------:|------------:|",
    ]
    for b in weekly:
        lines.append(
            f"  | {b.week_start} | {b.commits:>7} | "
            f"+{b.additions:>6,} | -{b.deletions:>5,} | {b.active_days:>11} |"
        )
    return lines


def _render_prior_delta(prior: PriorRetroDelta) -> list[str]:
    """Trends-vs-last-retro table. Skips when no prior snapshot exists."""
    if not prior.has_prior:
        return []
    rows = [
        ("Commits", prior.commits),
        ("+LOC", prior.additions),
        ("-LOC", prior.deletions),
        ("Sessions", prior.sessions),
        ("Tokens", prior.tokens_total),
        ("Pushes", prior.push_events),
        ("Streak", prior.streak_days),
    ]
    nonzero = [(label, delta) for label, delta in rows if delta != 0]
    if not nonzero:
        # No changes is the right answer — emitting "no metric changed"
        # as a stranded bullet pollutes more than it informs. Skip the
        # whole section and let the rest of the report stand on its own.
        return []
    lines = [f"## Trends vs last retro ({prior.prior_date or 'prior'})", ""]
    for label, delta in nonzero:
        arrow = "↑" if delta > 0 else "↓"
        lines.append(f"- {label}: {arrow}{abs(delta):,}")
    lines.append("")
    return lines


def _render_themes_prompt(data: RetroData) -> list[str]:
    """Emit a fenced JSON block with the raw material the LLM needs to
    synthesize themes + noteworthy line for the second-pass card.
    Includes commit subjects keyed by repo and a few aggregate stats.

    Kept at the END of the markdown so a casual reader can ignore it; the
    SKILL.md instructs the LLM to read it, synthesize, and re-invoke
    ``mm retro-fleet`` with ``--theme`` / ``--noteworthy`` flags. Block
    is tagged with ``<!-- MM_THEMES_PROMPT -->`` so the SKILL.md can
    locate it deterministically."""

    # Repo URLs go through the same defang-then-shorten pipeline as the
    # markdown body so a long-canonical URL doesn't survive into the
    # JSON sidecar (would defeat the privacy-preserving compression in
    # ``_shorten_repo_url``). Same trust-boundary rationale as the body.
    def _safe_repo(r: str) -> str:
        return _shorten_repo_url(_safe_repo_url(r)) if r else ""

    payload = {
        "window_days": data.window_days,
        "since": data.since.astimezone().date().isoformat(),
        "until": data.until.astimezone().date().isoformat(),
        "commits": data.git.commits,
        "additions": data.git.additions,
        "deletions": data.git.deletions,
        "top_repos": [
            _safe_repo(r)
            for r, _ in sorted(data.git.repos_by_count.items(), key=lambda kv: kv[1], reverse=True)[
                :TOP_N_REPOS
            ]
        ],
        "ship": (
            {
                "repo": _safe_repo(data.git.ship.repo),
                "subject": _safe_prose(data.git.ship.subject),
                "additions": data.git.ship.additions,
                "deletions": data.git.ship.deletions,
            }
            if data.git.ship.has_data
            else None
        ),
    }
    return [
        "<!-- MM_THEMES_PROMPT -->",
        "```json",
        json.dumps(payload, indent=2),
        "```",
    ]


def format_retro(
    data: RetroData,
    *,
    name: str | None = None,
    themes: list[str] | None = None,
    noteworthy: str = "",
) -> str:
    """Render the markdown retro. Output is paste-ready for iMessage / email
    — single-message length when realistic data is present.

    ``themes`` / ``noteworthy`` / ``name`` are LLM-supplied via the second
    pass of the two-pass card flow. When any of them is non-empty/non-None
    an ASCII screenshot card is rendered at the TOP of the output; without
    them, the markdown body still includes a ``MM_THEMES_PROMPT`` block at
    the END to feed the next pass. Pure markdown body (no card) is
    rendered when the caller is a non-skill consumer (the test fixture
    path that just wants data).

    Section layout (post-v0.12.0):

    * (Optional) ASCII card with stats + NOTEWORTHY + TOP WORK themes.
    * Header — date range + activity-across-N-machines line.
    * Trends vs last retro — delta block when a prior snapshot exists.
    * Code shipped — commits, LOC, top repos, commit-type mix, peak hours,
      commit bursts, ship-of-the-window.
    * Week-over-week — bucketed table when window_days >= 14.
    * Claude Code activity — sessions and token block.
    * Skills used — fleet-wide invocation rollup.
    * mm sync activity — push counts.
    * Notes — every aside consolidated.
    * MM_THEMES_PROMPT — JSON sidecar for LLM theme synthesis.
    """
    lines: list[str] = []
    notes: list[str] = []

    themes_list = list(themes) if themes else []
    has_card_input = bool(themes_list) or bool(noteworthy) or bool(name)
    if has_card_input:
        lines.extend(_render_ascii_card(data, name=name, themes=themes_list, noteworthy=noteworthy))
        lines.append("")

    # Header date matches the card's local-time framing — using
    # ``data.since.date()`` directly returns the naive UTC date, which
    # diverges from the card by a day near UTC boundaries.
    lines.append(
        f"# Retro: {data.since.astimezone().date().isoformat()} → "
        f"{data.until.astimezone().date().isoformat()} "
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

    # Trends vs last retro (only when a matching-window snapshot exists).
    delta_lines = _render_prior_delta(data.prior)
    if delta_lines:
        lines.extend(delta_lines)

    # Code shipped.
    lines.append("## Code shipped")
    lines.append(
        f"- {data.git.commits} commits across {len(data.git.repos_by_count)} repos "
        f"(deduped across machines)"
    )
    lines.append(f"- +{data.git.additions:,} / -{data.git.deletions:,} LOC")
    if data.git.streak_days > 0:
        lines.append(f"- {data.git.streak_days}-day commit streak")
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
    lines.extend(_render_commit_types(data.git.commit_types))
    lines.extend(_render_hourly(data.git.hourly))
    lines.extend(_render_bursts(data.git.bursts))
    lines.extend(_render_ship(data.git.ship))
    lines.extend(_render_weekly(data.git.weekly))
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
        # Projects-count was misleading: Claude Code keys session storage by
        # encoded cwd, so each Conductor workspace and git worktree counts as
        # a distinct "project" even though they trace back to ~10 real repos.
        # The repo count under "Code shipped" already covers the useful
        # signal; surface ephemeral as an inline qualifier on sessions
        # instead of a Notes aside.
        if data.sessions.ephemeral_sessions:
            lines.append(
                f"- {data.sessions.total_sessions} sessions, "
                f"{data.sessions.ephemeral_sessions} of which are in ephemeral "
                f"Conductor workspaces"
            )
        else:
            lines.append(f"- {data.sessions.total_sessions} sessions")
        # Token block — only renders when fleet has any token data. Hides
        # cleanly on a fresh fleet so empty zeros don't pollute output.
        _render_token_block(lines, data.sessions)
    lines.append("")

    # Skills used — fleet-wide as of v0.11.27 (was: this-machine-only via
    # gstack analytics file). Sanitize each skill name at render time —
    # peer-controlled string crossing the trust boundary into LLM-consumed
    # markdown. Same defense-in-depth as model-name sanitization in the
    # token block.
    lines.append(f"## Skills used ({data.skills.invocations} invocations)")
    if not data.skills.available:
        lines.append(
            "- *No fleet device has shipped skill data yet — section omitted "
            "(upgrade peers to v0.11.27+ for fleet-wide skill counts).*"
        )
    elif data.skills.invocations == 0:
        lines.append("- No skill invocations captured.")
    else:
        top = sorted(data.skills.by_skill.items(), key=lambda kv: kv[1], reverse=True)[
            :TOP_N_SKILLS
        ]
        formatted = ", ".join(f"/{_safe_short(s)} ({n})" for s, n in top)
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
    if data.skills.pre_skills_peers:
        n_skills = len(data.skills.pre_skills_peers)
        notes.append(
            f"Skills incomplete: {n_skills} peer(s) on pre-v0.11.27 — upgrade for "
            f"accurate skill totals."
        )
    # Unpriced-model breadcrumb. Models present in the fleet's
    # ``tokens_by_model`` but missing from the pricing table contribute to
    # the displayed token totals (they're real API traffic) but are skipped
    # by ``estimate_cost``. Surface the volume so a reader knows the cost
    # line is an under-estimate rather than authoritative.
    unpriced_tokens, unpriced_models = _unpriced_token_summary(data.sessions.tokens_by_model)
    if unpriced_tokens > 0:
        notes.append(
            f"{_format_token_count(unpriced_tokens)} tokens from {unpriced_models} unpriced "
            f"model(s) excluded from cost estimate."
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
    if n_events:
        notes.append(
            f"{n_events} event(s) skipped due to parse errors in mm event log. "
            f"Output may be incomplete."
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

    # First-pass artifact for the two-pass card flow. Only rendered when
    # the caller did NOT supply card content — a second-pass render
    # (which carries themes/noteworthy/name) is the final shareable
    # output and should not include the synthesis prompt block.
    if not has_card_input:
        lines.append("")
        lines.extend(_render_themes_prompt(data))

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


def _resolve_retros_dir() -> Path:
    """``MM_RETROS_DIR`` env override (parallel to ``MM_EVENTS_DIR``);
    falls back to default. Test isolation hook."""
    override = os.environ.get("MM_RETROS_DIR")
    if override:
        return Path(override).expanduser()
    return DEFAULT_RETROS_DIR


def _retro_to_snapshot(data: RetroData) -> dict:
    """Serialize a ``RetroData`` to the JSON-on-disk shape. Stores ONLY
    the fields needed for trend deltas — keeping the file small and
    forward-compatible (a future field can be added without breaking
    older readers, which simply ignore unknown keys)."""
    return {
        "schema_version": 1,
        "window_days": data.window_days,
        "since": data.since.isoformat(),
        "until": data.until.isoformat(),
        "metrics": {
            "commits": data.git.commits,
            "additions": data.git.additions,
            "deletions": data.git.deletions,
            "streak_days": data.git.streak_days,
            "sessions": data.sessions.total_sessions,
            "tokens_total": (
                data.sessions.tokens_input
                + data.sessions.tokens_cache_create
                + data.sessions.tokens_cache_read
                + data.sessions.tokens_output
            ),
            "push_events": data.pushes.push_events,
        },
    }


_SNAPSHOT_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d+)\.json$")
"""Snapshot filename: ``YYYY-MM-DD-NNN.json``. Sequence is zero-padded to
3 digits so lexical sort agrees with numeric sort up to 999 retros/day —
``-002`` sorts before ``-010``, where the un-padded ``-2`` would sort
after ``-10``. Pre-fix, ``_load_prior_snapshot`` returned the wrong
"most recent" once a single day exceeded 9 retros."""

_SNAPSHOT_SEQ_DIGITS = 3
_SNAPSHOT_SEQ_MAX = 10**_SNAPSHOT_SEQ_DIGITS - 1  # 999


def _save_snapshot(data: RetroData, retros_dir: Path) -> Path | None:
    """Persist a JSON snapshot for trend deltas. Returns the saved path or
    None on failure. Failure is forensic-only — emits a single
    ``mm: notice:`` to stderr and returns; the retro render proceeds.

    Race-safe: uses ``O_CREAT|O_EXCL`` so two concurrent runs picking the
    same sequence number can't silently overwrite each other. On
    collision the seq advances and we retry. Pre-fix, the
    ``len(existing) + 1`` heuristic was a TOCTOU bug — both runs
    computed the same seq and the second ``write_text`` clobbered the
    first."""
    try:
        retros_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        sys.stderr.write(f"mm: notice: retro snapshot dir unwritable ({exc}); skipping save\n")
        return None
    today = data.until.astimezone().date().isoformat()
    # Sequence number: pick max(existing seqs for today) + 1. Zero-padded
    # to keep lex sort numerically correct.
    try:
        existing = list(retros_dir.glob(f"{today}-*.json"))
    except OSError:
        existing = []
    max_seq = 0
    for f in existing:
        m = _SNAPSHOT_FILENAME_RE.match(f.name)
        if m and m.group(1) == today:
            try:
                n = int(m.group(2))
            except ValueError:
                continue
            if n > max_seq:
                max_seq = n
    payload = json.dumps(_retro_to_snapshot(data), indent=2) + "\n"
    seq = max_seq + 1
    path: Path | None = None
    while seq <= _SNAPSHOT_SEQ_MAX:
        candidate = retros_dir / f"{today}-{seq:0{_SNAPSHOT_SEQ_DIGITS}d}.json"
        try:
            # O_EXCL: race-safe; raises FileExistsError if another writer
            # took this seq first. Bump seq and retry.
            with open(
                candidate,
                "x",
                encoding="utf-8",
            ) as f:
                f.write(payload)
            path = candidate
            break
        except FileExistsError:
            seq += 1
            continue
        except OSError as exc:
            sys.stderr.write(f"mm: notice: retro snapshot write failed ({exc}); skipping\n")
            return None
    if path is None:
        sys.stderr.write(
            f"mm: notice: retro snapshot seq exhausted for {today} "
            f"(>{_SNAPSHOT_SEQ_MAX} retros in one day); skipping save\n"
        )
        return None
    _prune_old_snapshots(retros_dir)
    return path


def _prune_old_snapshots(retros_dir: Path) -> None:
    """Best-effort prune of snapshots older than ``RETROS_RETENTION_DAYS``.
    Reaped by FILENAME date (the date is intrinsic, mtime is not — same
    rationale as ``_gc_old_event_files``). Silent on every failure.
    Filenames not matching the canonical shape are left alone."""
    try:
        files = list(retros_dir.glob("*.json"))
    except OSError:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETROS_RETENTION_DAYS)).date()
    for f in files:
        m = _SNAPSHOT_FILENAME_RE.match(f.name)
        if m is None:
            continue
        try:
            file_date = datetime.fromisoformat(m.group(1)).date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                f.unlink()
            except OSError:
                continue


_SNAPSHOT_MAX_BYTES = 1_000_000
"""Cap individual snapshot file reads at 1 MiB. A typical snapshot is
<1 KiB; a 1 MB file would already be 1000× normal. Defends against a
corrupt / fs-recovery / planted file from blowing up memory before
``json.loads`` even fails."""


def _load_prior_snapshot(retros_dir: Path, window_days: int) -> dict | None:
    """Return the most recent snapshot dict whose ``window_days`` matches.
    None when no matching snapshot exists or directory missing.
    Tolerant of corrupt JSON / unreadable files (skipped).

    Sorts by parsed ``(date, seq)`` tuple, NOT by lexical filename.
    Pre-fix, ``sorted(..., reverse=True)`` ordered ``-9.json`` AFTER
    ``-10.json`` (because lex sort puts longer strings first in
    reverse), so once a single day produced 10+ retros, "most recent"
    returned a stale snapshot. Filenames now zero-pad to 3 digits at
    write time AND the loader parses+sorts by tuple — both layers of
    defense."""
    if not retros_dir.is_dir():
        return None
    try:
        files = list(retros_dir.glob("*.json"))
    except OSError:
        return None
    parsed: list[tuple[str, int, Path]] = []
    for f in files:
        m = _SNAPSHOT_FILENAME_RE.match(f.name)
        if m is None:
            continue
        try:
            seq = int(m.group(2))
        except ValueError:
            continue
        parsed.append((m.group(1), seq, f))
    # Newest first by (date, seq) — both numeric / lex-stable.
    parsed.sort(reverse=True)
    for _date, _seq, f in parsed:
        try:
            if f.stat().st_size > _SNAPSHOT_MAX_BYTES:
                continue
            obj = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("window_days") != window_days:
            continue
        return obj
    return None


def _compute_prior_delta(data: RetroData, prior: dict | None) -> PriorRetroDelta:
    """Build a ``PriorRetroDelta`` from a snapshot dict. Tolerant of missing
    fields (treated as zero) so a v1 snapshot read by a future v2 renderer
    degrades cleanly."""
    if prior is None:
        return PriorRetroDelta()
    metrics = prior.get("metrics", {}) if isinstance(prior, dict) else {}
    if not isinstance(metrics, dict):
        return PriorRetroDelta()
    until_raw = prior.get("until") if isinstance(prior, dict) else None
    prior_date = ""
    if isinstance(until_raw, str):
        try:
            prior_date = datetime.fromisoformat(until_raw.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            prior_date = ""
    now_tokens = (
        data.sessions.tokens_input
        + data.sessions.tokens_cache_create
        + data.sessions.tokens_cache_read
        + data.sessions.tokens_output
    )
    return PriorRetroDelta(
        has_prior=True,
        prior_date=prior_date,
        commits=data.git.commits - _safe_int(metrics.get("commits")),
        additions=data.git.additions - _safe_int(metrics.get("additions")),
        deletions=data.git.deletions - _safe_int(metrics.get("deletions")),
        sessions=data.sessions.total_sessions - _safe_int(metrics.get("sessions")),
        tokens_total=now_tokens - _safe_int(metrics.get("tokens_total")),
        push_events=data.pushes.push_events - _safe_int(metrics.get("push_events")),
        streak_days=data.git.streak_days - _safe_int(metrics.get("streak_days")),
    )


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


_MAX_WINDOW_DAYS = 3650
"""10-year ceiling on the window. Well above the 90-day events retention
that bounds real data; defends ``timedelta(days=...)`` against
``OverflowError`` on absurd input like ``1000000000d``."""


def _parse_window(s: str) -> int:
    m = WINDOW_PATTERN.match(s)
    if m is None:
        raise argparse.ArgumentTypeError(
            f"window must be of the form Nd (e.g. '7d', '30d'); got {s!r}"
        )
    n = int(m.group(1))
    if n <= 0:
        raise argparse.ArgumentTypeError(f"window must be positive; got {n}")
    if n > _MAX_WINDOW_DAYS:
        raise argparse.ArgumentTypeError(
            f"window must be ≤ {_MAX_WINDOW_DAYS}d (10 years); got {n}d"
        )
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
    parser.add_argument(
        "--theme",
        action="append",
        default=[],
        help=(
            "LLM-supplied TOP WORK theme line for the ASCII card. Pass "
            "up to three times. Triggers second-pass card rendering."
        ),
    )
    parser.add_argument(
        "--noteworthy",
        default="",
        help="LLM-supplied NOTEWORTHY line for the ASCII card.",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Optional name shown in the ASCII card header (e.g. 'kb').",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help=(
            "Skip writing a snapshot to ~/.local/share/mind-meld/retros/. "
            "Useful for the second pass (the first pass already saved)."
        ),
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
    )

    # Trend deltas vs the most recent matching-window snapshot. Loading
    # before saving so today's snapshot doesn't compare against itself.
    retros_dir = _resolve_retros_dir()
    prior = _load_prior_snapshot(retros_dir, args.window)
    data.prior = _compute_prior_delta(data, prior)

    # Persist a snapshot for next time. Skipped on the second pass so a
    # single retro session doesn't write twice (the first-pass save is
    # the canonical record for trend deltas).
    has_card_input = bool(args.theme) or bool(args.noteworthy) or bool(args.name)
    if not args.no_save and not has_card_input:
        _save_snapshot(data, retros_dir)

    sys.stdout.write(
        format_retro(
            data,
            name=args.name or None,
            themes=list(args.theme),
            noteworthy=args.noteworthy,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
