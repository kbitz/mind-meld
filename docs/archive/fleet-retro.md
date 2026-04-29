# Fleet-Aware Retro: mm Event Log + retro-fleet Skill

> Targets v0.11.0 (minor; additive). Branch: `feat/fleet-retro`.
> Supplements `docs/designs/sync-gstack-context.md` (multi-source architecture)
> and `docs/designs/source-toggle.md` (per-machine source toggle, v0.10.0).

## Problem

gstack's `/retro` skill reads `~/.claude/projects/*/*.jsonl` (Claude Code
session logs) and runs `git log` on locally-cloned repos. mm explicitly does
not sync session JSONLs (only `memory/` and `todos/` per project), and
git-log queries only see what each machine has cloned + pulled. Result on a
multi-Mac fleet:

- Sessions from other machines are invisible.
- Git stats reflect only this machine's clones (often stale or missing
  entirely for a given repo).
- Conductor workspace paths get decoded but don't exist on the local
  machine, so their sessions are silently dropped from totals.
- Output is presented as fleet-wide truth; in practice it's
  whichever-machine-you-ran-it-on truth.

The user-facing fix is a retro that mirrors gstack `/retro`'s output but is
genuinely accurate in aggregate across all machines.

## Solution overview

Two coordinated pieces in one mm release:

1. **mm-side event log** — every push appends 1+N+M lines to a per-device
   daily JSONL under a new mm-owned synced source. Three event types per
   push: `mm-push` (sync activity), `git-snapshot` (recent commits per repo
   with canonical remote URL), `sessions-snapshot` (per-project session
   metadata).

2. **`retro-fleet` skill shipped in the mm wheel** — symlinked into
   `~/.claude/skills/` at `mm init`. Reads the synced event log across all
   devices, dedups by `(remote_url, sha)` for git accuracy, and renders
   `/retro`-shaped markdown.

No gstack changes required. No external API dependency.

## Design constraints

- **Output:** glanceable + shareable markdown, paste-able into iMessage /
  email. Not a dashboard.
- **Parity** with gstack `/retro`'s format and feel — mirror what works.
- **Aggregate accurate, not per-machine.** Fleet-wide totals only; no
  per-machine breakdown.
- **mm-owned data lives in mm-owned scope.** No writing to gstack's
  directory tree.
- **Honor the silent-fast contract.** `mm autopush` must remain non-blocking;
  expensive ops have a hard time budget with silent skip on exceed.

## 1. New `mm-events` source

```python
# config.py — DEFAULT_SOURCES addition
{
    "name": "mm-events",
    "path": "~/.local/share/mind-meld",
    "type": "generic",
    "include_dirs": ["events"],
    "exclude_patterns": [],
}
```

Files live at `~/.local/share/mind-meld/events/<device>-<YYYY-MM-DD>.jsonl`.
Subdir nesting plays cleanly with the existing `walk_generic_source` (avoids
the `include_dirs: ["."]` pathlib quirk).

**Migration semantics:**
- New installs get `mm-events` automatically (DEFAULT_SOURCES).
- Existing installs (with explicit `[[sync.sources]]` blocks) opt in via
  `mm migrate-config` — established v0.9.1 pattern, surfaced by `mm status`.
- `mm disable-source mm-events` per v0.10.0 stops emitting on that machine;
  other machines see "Fleet incomplete: events from N of M known devices"
  in retro output.

**Path-existence bootstrap:** `events.py` creates
`~/.local/share/mind-meld/events/` (mode 0700, parents=True) before first
event write — guarantees `Path.exists()` returns True so `get_sources()`
doesn't drop the source on a fresh machine.

## 2. Event log schema (v=1)

Per-device daily JSONL. One line per event. Required fields: `v`, `type`,
`ts`, `device`. Forward-compat: unknown fields tolerated by the reader.

```jsonl
{"v":1,"type":"mm-push","ts":"<iso>","device":"<uuid>","mm_version":"0.11.0","sources":[{"name":"claude","added":4,"modified":2,"removed":0,"projects":["<slug>","<slug>"]}]}
{"v":1,"type":"git-snapshot","ts":"<iso>","device":"<uuid>","projects":[{"remote":"<canonical-url>","local_path":"<abs-path>","commits":[{"sha":"<sha>","date":"<iso>","author_email":"<email>","subject":"<subject>","files":4,"add":50,"del":12}]}]}
{"v":1,"type":"sessions-snapshot","ts":"<iso>","device":"<uuid>","projects":[{"claude_dir":"<encoded-dir>","sessions":3,"total_kb":1200,"last_session_at":"<iso>","ephemeral":false}]}
```

**Pull-merge interaction:** these are `.jsonl` files, so they participate
in `merge.py`'s `merge_jsonl` strategy (set-union, ts-sorted) on pull.
Single-writer-per-file (device-id in filename) makes this a no-op in
steady state. Documented so a future "switch to .ndjson" change doesn't
silently break ordering.

## 3. URL canonicalization

Pure function, table-tested. Required for `(remote, sha)` dedup to work
across machines.

| Input | Output |
|---|---|
| `https://github.com/<org>/<repo>.git` | `github.com/<org>/<repo>` |
| `git://github.com/<org>/<repo>.git` | `github.com/<org>/<repo>` |
| `ssh://git@github.com:22/<org>/<repo>.git` | `github.com/<org>/<repo>` |
| `git@github.com:<org>/<repo>.git` (scp-form) | `github.com/<org>/<repo>` |
| `https://x-access-token:TOKEN@<host>/<path>` | `<host>/<path>` |

Rules: strip scheme; strip user/auth segment; strip port; strip trailing
`.git`; lowercase host; preserve case in path.

**Multi-remote repos:** only `origin` is captured.
**Cherry-picked commits:** different SHAs, counted separately. Acceptable
v1 limitation.

## 4. `events.py` module

Exports:

- `canonicalize_remote_url(url: str) -> str` — pure function, table-tested
  per the spec above.

- `walk_git_projects(roots: list[Path], since: datetime, total_budget_ms: int) -> list[GitSnapshot]`
  — discovers `.git/` dirs under configured roots; concurrency
  `ThreadPoolExecutor(max_workers=8)`. Per-repo subprocess timeout
  computed lazily: `max(500, total_budget_remaining // repos_remaining)`,
  capped at 2000ms. Single git command per repo:

  ```
  git -C <path> log --since=<iso> --numstat -M -C \
    --format='%x1e%H%x09%aI%x09%ae%x09%s'
  ```

  Parser handles binary rows (`-\t-\tpath` → `add=0, del=0`), rename rows
  (`old => new` → preserve `new`), and empty/merge commits with no numstat
  rows (commit recorded with `files=0, add=0, del=0`). Per-repo failures:
  silent skip + emit one `git_snapshot_skip` line for forensic trail.
  Whole-walk failure: `mm: notice:` (forensic, not data-at-risk).

- `walk_session_metadata(claude_dir: Path, since: datetime) -> list[SessionSnapshot]`
  — `os.scandir`-based 2-level walk. Sets `ephemeral: True` if the decoded
  project path matches `*/conductor/workspaces/*` (matched on the *decoded
  path string*, NOT path existence — Conductor workspaces are routinely
  destroyed). Perf target: <500ms for 10k files.

- `read_cursor() / write_cursor()` — `~/.config/mind-meld/event-cursor.json`
  (mode 0600). **fcntl.flock MANDATORY** (concurrent autopush from agent
  hooks is a real race). Schema: `{"v":1,"last_event_ts":"<iso>"}`. First
  run: returns `now - 30d`. Lock-order: cursor flock is INNERMOST; release
  before any other lock acquisition.

- `write_push_event(events_dir: Path, device_id: str, events: list[dict]) -> None`
  — appends N lines to today's per-device JSONL. **fcntl.flock MANDATORY
  on the events file.** Atomic append via `O_APPEND` write under flock.

## 5. Wiring in `_push_core` tail

```
... existing _push_core logic ...
upload_changed_blobs() ✓ existing
upgrade.emit_nudge_if_due() ✓ existing v0.9.5
events.write_push_event(...)  ← NEW (events become the new tail)
```

The v0.9.5 nudge stays at its existing relative-tail position. Events are
*after* the nudge — local file IO doesn't stack with cold-cache HTTP
latency the way the nudge does, so the documented tail-position invariant
is preserved.

**Time budget:** 500ms total (interactive) / 250ms total (autopush). On
exceed, partial events already written are kept, remaining capture aborts
silently.

**Failure handling:** `mm: notice:` (NOT `mm: warning:`) — preserves the
curated-warning taxonomy from CLAUDE.md (warnings = data-at-risk only;
event capture is forensic-only).

**Cursor advancement under budget abort:** if `walk_git_projects` aborts
mid-walk, repos that didn't complete may have unscanned commits. Cursor is
advanced to `now` regardless — accepting that some commits in the abort
window may be missed from future retros. Rationale: retro is forensic-only,
not data-at-risk; perfect commit accounting requires retry/resume
infrastructure that's overkill for v1. Skill output includes a "note:
budget abort on <date> — some commits may be missed" breadcrumb when
applicable.

Gated on `not dry_run`.

## 6. `retro-fleet` skill

Ships in mm wheel at `src/mind_meld/skills/retro-fleet/SKILL.md`.

**API contract:** the skill is a markdown file that runs Python (or jq)
directly to read mm-owned files. Skill is acknowledged as an mm-internal
API consumer — schema versioning lives in `events.py` and the skill's
reader.

Reads:
- `~/.local/share/mind-meld/events/*-*.jsonl` (mm-owned, schema-versioned)
- `~/.gstack/analytics/skill-usage.jsonl` (gstack-owned; **schema
  dependency is load-bearing** — reader must tolerate missing fields and
  never crash if gstack ships a breaking change)
- `~/.gstack/analytics/eureka.jsonl` (same caveat)
- `~/.gstack/retros/*.json` (read-only consumer)

Aggregation:
- Git: dedup by `(canonical_remote_url, sha)`; sum LOC; group by repo for
  "top repos."
- Sessions: sum across `claude_dir`; split ephemeral/non-ephemeral counts.
- Skills: as-rendered on this machine (gstack analytics aren't currently
  in mm-owned synced scope; retro output includes "Skill counts: this
  machine only" breadcrumb).

**Author filter:** read `git config --global user.email` at retro time on
the rendering machine. Optional `[retro].author_emails: list[str]` config
for aliases (work + personal email, etc.). NO persistence of derived emails
to mm config — avoids cross-machine "which Mac wins" footgun. Captured
events tag commits with `author_email` already; filter happens at render.

**Locked output format** (mm owns the format; not coupled to gstack
evolution):

```markdown
# Retro: <date> → <date> (Nd)

**Activity across N machines** (<device>, <device>, <device>)
*Fleet incomplete: events from M of N known devices.*  ← only when applicable

## Code shipped
- N commits across N repos (deduped across machines)
- +N / -N LOC
- Top repos: <repo> (N), <repo> (N), <repo> (N)

## Claude Code activity
- N sessions across N projects (M in ephemeral Conductor workspaces, counted separately)
- N MB total session content
- Most active: <project> (N), <project> (N ephemeral)

## Skills used (N invocations) — *this machine only*
- /<skill> (N), /<skill> (N), ...

## Eureka moments (N)
- "<insight>" (<project>, <date>)
- ...

## mm sync activity
- N pushes, N pulls, N MB transferred
```

## 7. Symlink installer

Two-state operation: ensure target points where it should, or skip on
precondition failure.

```python
def _ensure_retro_skill_link() -> None:
    # Hatchling default = unzipped wheels, so files() returns a real Path.
    # CI smoke test catches it before users do if a future build backend
    # ships zipped wheels.
    skill_src = Path(
        str(importlib.resources.files("mind_meld") / "skills" / "retro-fleet")
    )
    target = Path("~/.claude/skills/retro-fleet").expanduser()
    skills_dir = target.parent

    if not skills_dir.exists():
        return  # silent skip; no Claude Code installed

    if target.is_symlink() and target.resolve() == skill_src.resolve():
        return  # already correct

    if target.exists() or target.is_symlink():
        sys.stderr.write(
            f"mm: notice: skill at {target} exists; not replacing\n"
        )
        return

    target.symlink_to(skill_src)
```

**Called from:**
- `mm init` (always)
- `_push_core` head, **gated by 24h-TTL idempotency file** at
  `~/.config/mind-meld/.skill-link-checked` (touch-mtime). Once per 24h —
  not "once per session." Keeps autopush hot path negligible: one
  `os.stat` on the marker, skip if recent.

**NOT called** from the v0.9.5 transition hook — that hook has documented
lock-order rules (NEVER acquire mm lockfile while holding upgrade-state's
flock) and is scoped to "transition detection," not file-system side
effects.

**Pipx behavior:** `pipx upgrade mind-meld` rewrites the package directory
in place; symlink path is stable, files at the path update. `pipx
reinstall` rebuilds the venv; the symlink target may be momentarily
stale, but the next push self-heals via the 24h-TTL check.

**Manual smoke test required before shipping:** verify Claude Code follows
the symlink when loading skills. Pin a one-time validation step in the
release checklist.

## 8. Package data in `pyproject.toml` (hatchling)

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/mind_meld/skills" = "mind_meld/skills"
```

CI test pinned:

```python
import importlib.resources
assert (
    importlib.resources.files("mind_meld") / "skills" / "retro-fleet" / "SKILL.md"
).is_file()
```

## 9. `mm gc` retention

Hardcoded 90-day retention for events files. Reaping happens at the
**local-filesystem layer**: `gc` deletes JSONL files older than 90 days
locally; the next push generates deletion tombstones via the existing
tombstone mechanism (which propagates to peers as normal). No new GC
layer; reuses existing tombstone-on-absent-file plumbing. Consistent with
CLAUDE.md "truth-based manifests" invariant.

## 10. Tests

- URL canonicalization round-trip table (5 forms × edge cases including
  port, user@host, scp-form, query-string auth)
- Event log append + parse round-trip; v1 schema validation
- Aggregator dedup correctness (same SHA from two devices counted once)
- Symlink installer: target-already-correct, target-conflict-skipped,
  target-missing-creates, fresh-machine-no-claude-dir-skips
- Cursor flock contention: two writers, no deadlock, both lines persisted
- Events file flock under concurrent autopush simulation
- `_push_core` integration: dry_run gating, budget exceed → silent skip,
  partial-write recovery
- Conductor-workspace path detection (path-pattern match, not existence-check)
- `mm gc` retention reaping + tombstone generation
- Empty/merge commit parsing (no numstat rows between `%x1e` separators)

## Success criteria

- `mm retro-fleet 7d` after a week of normal multi-Mac usage produces:
  - **Total commits across all machines (deduped)** matches manual
    `git log --author=$ME` summed across known repos on a known
    source-of-truth machine.
  - **Total sessions** matches sum of `~/.claude/projects/*/*.jsonl`
    counts across all machines.
  - **Skill invocations** matches `~/.gstack/analytics/skill-usage.jsonl`
    on the rendering machine (single-machine count, with breadcrumb).
  - **Output renders cleanly** in iMessage paste preview (single-message
    length, no truncation).
- mm push hot path overhead under 500ms (interactive) / 250ms (autopush)
  even with 30 repos. Budget exceed → silent skip. Pinned in perf test.
- `mm init` on a fresh machine drops the skill symlink in one command.
- `pipx upgrade mind-meld` + first subsequent push self-heals broken
  symlink without user intervention (24h TTL gate).
- Retro on day 1 (incomplete fleet) shows "Fleet incomplete: events from
  N of M known devices" — graceful degrade, not silent error.

## Distribution

- mm distribution unchanged; ships via pipx/pip from PyPI; skill files
  travel inside the wheel via hatchling `force-include`.
- Symlink approach: `pipx upgrade mind-meld` is the entire upgrade path
  because the symlink target's *file path* is stable across version
  updates within the same venv (hatchling default = unzipped wheels).
  `pipx reinstall` mitigated by self-heal.
- CI: existing workflow + new test validating wheel ships skill files via
  `importlib.resources`.
- Release checklist: manual smoke test that Claude Code follows the
  symlink and loads `retro-fleet`.

## Out of scope (v2 candidates)

- **`mm doctor`** symlink-health diagnostic command — gated on whether
  v1 self-heal proves sufficient.
- **`--save` flag** for the skill that persists to
  `~/.gstack/retros/global-fleet-<date>-<device>.json` — v1 is `mm
  retro-fleet 7d > /tmp/foo.md` if persistence is wanted.
- **Obsidian vault tracking** — different surface (not git, not currently
  in mm sync scope). Add if v1 dogfood reveals the gap.
- **Author email config bootstrapping** beyond the manual-aliases pattern
  — re-evaluate after first month of dogfooding.
- **`claude-sessions` full-content sync** as opt-in source — measured at
  ~90MB/machine on a typical setup. Metadata-only event log captures the
  retro signal at <1% of the cost; full-content sync is only worth it if
  cross-machine session-content access becomes a separate use case
  (continuity, archival).
