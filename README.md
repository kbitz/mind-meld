# Mind Meld

[![CI](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml/badge.svg)](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml)

Sync AI coding-agent context, skills, and gstack activity across Macs via iCloud Drive. End-to-end encrypted. Supports Claude Code, Codex, and OpenCode.

## Install

```bash
pipx install git+https://github.com/kbitz/mind-meld.git@latest
```

Not on PyPI — install straight from GitHub. The `@latest` ref is a branch the release workflow force-advances to each tagged release, so you always get the newest *released* version (never untagged work-in-progress off `main`) **and** plain `pipx upgrade` keeps working (see below).

## Upgrading

```bash
pipx upgrade mind-meld
```

That's it. Because the install tracks the moving `latest` branch (not a frozen tag), `pipx upgrade` re-resolves it to the newest release and lands it.

**Stuck on an old version?** If you ever installed or upgraded with the old `--force …@vX.Y.Z` form, your install is pinned to that exact tag — pipx re-resolves the frozen ref on every `pipx upgrade` and reports your current version as "latest" forever. A git tag never moves; a branch does. Run this once to switch onto the `latest` branch, after which plain `pipx upgrade mind-meld` works:

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@latest
```

(This is exactly the command mm's auto-upgrade nudge prints.)

**Need to roll back?** Install a specific older release by tag:

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@v0.12.20
```

Nothing on disk needs migrating — config, manifests, blobs, and the events log
are unchanged across the 0.12.x line, so a rollback is just a reinstall. Note
the caveat above applies: a pinned tag stops tracking `latest`, so re-run the
`@latest` command when you want to resume upgrades.

## Quick Start

```bash
mm init    # configure iCloud storage + passphrase
mm push    # upload memory/todos
mm pull    # download from another device
```

Config lives at `~/.config/mind-meld/config.toml` — not tied to your current directory. Install `mm` anywhere, run from anywhere; it always syncs the sources configured in your global config (`~/.claude` and `~/.gstack` by default).

## Setting up a second (or third) Mac

1. `pipx install git+https://github.com/kbitz/mind-meld.git@latest` on the new machine.
2. `mm init` — point it at the **same iCloud folder** as your first Mac and enter the **same passphrase**. This registers the new device against the existing roster.
3. `mm pull` — downloads everything the other machine(s) have pushed.
4. `mm push` — uploads anything this machine has that the others don't.

Push and pull on each Mac over time and the state converges toward the union of all three. Nothing is destroyed:

- **`.jsonl` files and `MEMORY.md`** deep-merge by line-union (deduped, sorted by `ts`). Entries from all machines accumulate — this is why telemetry, learnings, and timeline files stay coherent across devices.
- **Other divergent files** use mtime-skip: if your local file is newer than the remote, pull leaves it alone. Otherwise the remote wins the canonical path and your local version is preserved as `<stem>.sync-conflict-<ts>-<device>.<ext>` sitting next to it (Syncthing convention). See [Handling conflicts](#handling-conflicts) below.
- **Deletions** propagate via tombstones in the manifest — delete a file on one machine, and `mm pull` on the others removes it cleanly.

First-run-from-divergent-state is explicitly supported: if each Mac already has its own memory/todos/analytics before you first run `mm init`, the three-way sync will merge the JSONLs, download missing files, and flag any true content conflicts as `.sync-conflict-*` for you to triage with `mm resolve`.

## Fast pulls (auto-pin)

`mm init` automatically pins your iCloud storage folder so blobs stay resident on this Mac. Without pinning, iCloud may evict cold blobs to save local disk and `mm pull` then blocks on iCloud File Provider materialization — fine over time, but slow on a fresh Mac's first sync.

Auto-pin runs `brctl download <storage_path>` (Apple's iCloud File Provider CLI) once at init. It's non-destructive, idempotent, and asynchronous: brctl returns immediately while iCloud materializes files in the background. You'll see a `Storage pinned for fast pulls` line on success, or a Finder right-click tip if `brctl` errors.

If you ever want to undo the pin (free up local disk):

```bash
brctl evict ~/Library/Mobile\ Documents/com~apple~CloudDocs/mind-meld
```

…or in Finder, right-click the folder and choose **Remove Download**.

If your storage path is **not** under iCloud Drive (e.g. a custom local folder or a different cloud sync), auto-pin is silently skipped — the slow-pull case only applies to iCloud-managed paths.

## Claude Code Integration

Mind Meld includes `autopull` and `autopush` commands designed for Claude Code — they run silently, never prompt, and output a single line summary (or nothing if already in sync).

Add the following to your **global** `~/.claude/CLAUDE.md` to have Claude automatically sync at the start and end of each conversation:

```markdown
# Mind Meld

At the **start of each conversation**, run:

\`\`\`bash
mm autopull
\`\`\`

- **No output:** Already in sync. Continue silently.
- **Any output:** Tell the user what was synced.

At the **end of each conversation** (when the user is wrapping up, says goodbye,
or you've completed the requested task), run:

\`\`\`bash
mm autopush
\`\`\`

- **No output:** Nothing to push. Say goodbye normally.
- **Any output:** Tell the user what was pushed.

If `mm` is not installed, both commands will fail silently — no action needed.
```

### How it works

- `mm autopull` checks all other registered devices for changes and applies them locally. It writes a `.mind-meld-log.md` breadcrumb to each affected project so Claude Code knows what changed.
- `mm autopush` builds a manifest of local memory/todos, diffs against the last push, and uploads only what changed.
- Both commands acquire a lockfile, never prompt for input, and exit gracefully on any error (so they never block Claude Code).
- "Silent" means no chatter on the happy path. Load-bearing degradation warnings — corrupt-manifest recovery, "no sync sources" misconfig, durability fsync failure, per-file pull failures — still reach stderr as a single `mm: warning: ...` line so a wedged background sync surfaces instead of rotting. Autopush writes a `no-sources` breadcrumb (separate from `success`) when the config has no sync sources. Both auto commands also write a `degraded` breadcrumb (separate from `success`) when an otherwise-successful run lost data: autopull on fsync durability failure, corrupt peer manifest, unknown source from a peer, or per-file apply failure; autopush (v0.12.16) when the fleet-retro events tail failed, exceeded its walk budget, or published no token/skill data because the token cache was cold or locked. The `detail` field enumerates which signals fired. `mm status` and any monitoring on top of it can catch both wedge and partial-degradation cases. The one wedge no breadcrumb can report is the command never running at all — an `ImportError` at module scope, say, which dies before typer's runner and writes nothing — so since v0.12.21 `mm status` also marks any autorun breadcrumb older than 48 hours as `stale — no autorun in Nh` instead of reporting the last `success` forever.
- **Auto-upgrade nudge (v0.9.5).** Once per 24h, `mm pull` / `mm push` (including the autopull/autopush variants) check GitHub for a newer release tag and emit a single `mm: notice: <old> → <new> available — run pipx install --force git+...@latest` line on stderr if you're behind. `mm` never invokes pipx itself; you run the printed command. The command tracks the moving `latest` branch (not a frozen tag), so it always lands the newest release and — crucially — rewrites any previously tag-pinned install's recorded URL onto `@latest`, after which plain `pipx upgrade mind-meld` works (see [Upgrading](#upgrading)). Disable with `--no-check-version` for one invocation, or set `[upgrade] auto_check = false` in `~/.config/mind-meld/config.toml` to disable persistently. The `notice:` prefix is distinct from `warning:` (reserved for data-at-risk signals). This is a leading-edge complement to the v0.9.2 fleet-version refusal, which only fires after a newer peer pushes data — the nudge fires before that, ideally making the refusal a backstop nobody hits.

## Codex and OpenCode Integration

Mind Meld treats Codex and OpenCode as first-class peers of Claude Code:

- `codex` syncs `~/.codex/AGENTS.md`, `skills/`, and `plugins/`.
- `opencode` syncs `~/.config/opencode/` customizations: rules, agents, commands, modes, plugins, skills, and tools.
- Account credentials, session databases, logs, tool output, and whole-file `config.toml` / `opencode.json{,c}` settings are not sync sources. Those settings can contain inline provider or MCP credentials, so they stay local until Mind Meld can safely filter individual fields.
- The bundled `/retro-fleet` skill installs for Claude Code, Codex, and OpenCode when you run `mm init`, `mm push`, or `mm install-skills`. OpenCode's Claude compatibility remains useful for existing gstack skills, but its own skill link works even when compatibility is disabled.

Symlinks inside any sync source are local routing, not portable content: Mind Meld does not upload them and leaves existing local links untouched on pull, including dangling links and linked directories. A source root itself may be a symlink.

Fresh installs are asked about the `codex` and `opencode` sources during `mm init`. Existing installations remain opt-in:

```bash
mm enable-source codex
mm enable-source opencode
```

Then give each agent the same lifecycle contract. For Codex, add this to `~/.codex/AGENTS.md` (or merge it into your existing global guidance):

```markdown
# Mind Meld

At the start of each conversation, run `mm autopull`.
At the end of each completed task or conversation, run `mm autopush`.

If either command has output, summarize it for the user. If it is silent, continue silently.
```

OpenCode reads `~/.claude/CLAUDE.md` as its global fallback, so this works immediately when Claude compatibility is enabled. If you use `OPENCODE_DISABLE_CLAUDE_CODE*` or have `~/.config/opencode/AGENTS.md`, put the same block there. This makes every agent feed the same gstack and `mm-events` history used by `retro-fleet`; Claude-only session and token breakdowns remain explicitly labeled as Claude-only.

### Manual commands

| Command | Description |
|---------|-------------|
| `mm --version` | Print the installed version and exit |
| `mm init` | Configure device, storage path, passphrase |
| `mm push` | Push with verbose output |
| `mm pull` | Pull with verbose output |
| `mm pull --conflict-mode prompt` | Pick a winner per-file at pull time instead of auto keep-both |
| `mm pull --conflict-mode fail` | Preflight all files; exit 3 (no writes) if any would conflict — for CI |
| `mm status` | Show local vs remote state, plus the last `autopull` / `autopush` breadcrumb — flagged `stale` when nothing has auto-run in 48h |
| `mm devices` | List registered devices |
| `mm devices --format=json` | Same data as a JSON array on stdout — for scripting (used by `/retro-fleet`) |
| `mm diff` | Dry-run: show what would change (annotates each file with write / merge / skip / conflict) |
| `mm gc` | Delete orphaned blobs and run local retention cleanup |
| `mm gc --dry-run` | Preview orphan blobs plus temporary, events, and token-cache retention cleanup without deleting; each reaper reports candidates and any repairs or skips |
| `mm gc --conflicts` | Also delete `.sync-conflict-*` files older than 30 days |
| `mm sources` | List configured sync sources |
| `mm log` | Query the per-file pull/push history. Filter with `--source`, `--since`, `--action {written\|merged\|skipped\|conflicted\|excluded\|uploaded\|failed}`, `--verb {pull\|push}`, `--limit`; `--format {jsonl\|table}` |
| `mm migrate-config` | Append any missing recommended `exclude_patterns` to your existing `[[sync.sources]]` entries. Idempotent and preserves your customizations; `--dry-run` to preview, `--yes` to skip the prompt |
| `mm refresh-identity` | Force-refresh the cached author-email set that decides which fleet commits count as yours. `--json` prints the resolved set |
| `mm conflicts` | List unresolved `.sync-conflict-*` files with age and canonical sibling |
| `mm resolve [PATH]` | Interactively pick a winner for conflict files (shows unified diff). Exits 1 if any per-conflict rename/unlink/read fails so CI / scripts can detect partial failure (the walk still continues through every conflict). |
| `mm retro-fleet [WINDOW]` | Render the fleet retrospective markdown to stdout (default `7d`). The `/retro-fleet` Claude Code skill calls this under the hood; safe to run directly for scripted exports (`mm retro-fleet 30d > /tmp/retro.md`). `--no-author-filter` renders every fleet commit instead of just yours. |
| `mm install-skills` | Force-check the `retro-fleet` skill link for Claude Code, Codex, and OpenCode. It installs only missing links and reports every agent's outcome; conflicts, including dangling or foreign links, are never overwritten. Use it for fresh-machine setup or after deliberately removing a stale link. |

### Syncing gstack

If `~/.gstack` is detected during `mm init`, it is automatically added as a sync source. gstack uses a **whitelist walker** — unlike the Claude source (which has hardcoded subdirs), the gstack source only syncs the directories and files you explicitly list.

**Defaults out of the box:**

- `include_dirs`: `projects/`, `analytics/`, `retros/`
- `include_files`: `retro-context.md`, `greptile-history.md`, `.completeness-intro-seen`, `.telemetry-prompted`, `.proactive-prompted`, `.welcome-seen`, `.codex-desc-healed`
- `exclude_patterns`: `config.yaml`, `projects/*/repo-mode.json`, `projects/*/land-deploy-confirmed`, `analytics/.last-sync-*` (per-machine artifacts that churn-conflict on every pull — `config.yaml` holds gstack's version-check tracking; `analytics/.last-sync-*` are per-machine cursor files tracking each device's progress through gstack's local analytics jsonls)

This covers the common cross-machine cases — in particular, `/retro global` sees activity from all your Macs because `analytics/skill-usage.jsonl`, `analytics/eureka.jsonl`, and `projects/<slug>/timeline.jsonl` are all `.jsonl` files that **set-union merge** on pull (deduped, sorted by `ts`). Append-only telemetry from 3 machines converges cleanly into one timeline.

**Not synced by default** (machine-local by design): `sessions/`, `sidebar-sessions/`, `slug-cache/`, `worktrees/`, `builder-profile.jsonl`, `developer-profile.json`. If you want any of these on every Mac, add them to your config (see below).

**Adding files or dirs:** edit `~/.config/mind-meld/config.toml` and extend the gstack source. For example, to sync the writing-style prompt marker and a custom notes file:

```toml
[[sync.sources]]
name = "gstack"
path = "~/.gstack"
type = "generic"
include_dirs = ["projects", "analytics", "retros"]
include_files = [
    "retro-context.md",
    "greptile-history.md",
    ".completeness-intro-seen",
    ".telemetry-prompted",
    ".proactive-prompted",
    ".welcome-seen",
    ".codex-desc-healed",
    ".writing-style-prompted",   # added (your custom extra)
]
exclude_patterns = [
    "config.yaml",
    "projects/*/repo-mode.json",
    "projects/*/land-deploy-confirmed",
]
```

Supplying `sync.sources` replaces the defaults wholesale — copy the full list, don't just add your extras. Run `mm sources` to confirm the resolved source list.

**Useful flags:**

- `mm sources` — show the configured source list with their `Enabled` state and file counts.
- `mm pull --source gstack` — pull only the gstack source (skip Claude).

### Syncing gstack-extend

If `~/.gstack-extend/` is detected during `mm init`, it is automatically added as a sync source — sibling of the `gstack` treatment above. The whitelist walker is scoped to `projects/` only; per-machine bookkeeping at the root (`config`, `just-upgraded-from`, `update-snoozed`) is excluded by construction. Anything `gstack-extend` skills (pair-review, test-plan, full-review) persist under `~/.gstack-extend/projects/<slug>/` rides this same source so cross-machine resume keeps working as the gstack-extend feature surface grows.

Existing installs see this as a `New source available: gstack-extend` hint on next `mm status`. Opt in with `mm enable-source gstack-extend` or dismiss with `mm disable-source gstack-extend` — same shape as every other source toggle.

## Disabling sources per machine

`config.toml` lives at `~/.config/mind-meld/` and is never synced — making it the natural home for per-device preferences. To turn off a source on one machine without affecting the others:

```bash
mm disable-source gstack       # this Mac only; iCloud peers untouched
mm enable-source gstack        # turn it back on
```

The on/off state lives in `[sync].disabled_sources = ["gstack"]`. Disabling does NOT delete your `[[sync.sources]]` entry — re-enabling preserves any customizations like `include_dirs` or `exclude_patterns`.

`mm sources` shows the toggle state as an `Enabled` column. `mm status` calls out disabled sources in a one-line breadcrumb so future-you doesn't forget gstack is off and re-debug "why isn't this syncing".

**Forward-compat for not-yet-shipped sources.** When `mm` adds a new source to its defaults, upgraders don't get auto-enrolled — `mm status` surfaces a one-shot enable hint. To pre-disable a name before it ships:

```bash
mm disable-source future-agent --force   # accepts unknown names for forward-compat
```

`mm reconfigure-sources` re-runs the picker against your current config + new defaults, in case you want to revisit every choice at once.

## Fleet retro (`/retro-fleet`)

Mind Meld v0.11.0 ships a Claude Code skill that stitches engineering activity from every Mac in your fleet into one accurate retrospective. Every `mm push` writes a per-device daily JSONL row (commit metadata, sessions count, sync activity) to the synced `mm-events` source, so any machine can read the union and produce a fleet-wide picture.

Inside Claude Code:

```text
/retro-fleet 7d     # last week, default if you omit the window
/retro-fleet 30d    # last month
/retro-fleet 90d    # last quarter (the retention ceiling)
```

The skill renders a paste-ready markdown retro — drop it into iMessage, Slack, or email. Commits are deduped across machines via `(canonical remote URL, sha)` so the same PR landed once but pushed from two laptops counts as one.

**v0.12.0 output shape.** A pixel-aligned ASCII card sits at the top — single-line NOTEWORTHY plus three TOP WORK theme bullets the skill synthesizes from the underlying data — followed by the full markdown body (commit-type mix, peak hours, commit bursts, ship-of-the-window, weekly buckets when window ≥14d). On repeat runs with the same window a `## Trends vs last retro` block surfaces deltas vs the most recent matching snapshot stored at `~/.local/share/mind-meld/retros/`. The card is generated via a two-pass flow: first invocation emits an `MM_THEMES_PROMPT` JSON sidecar, the skill synthesizes themes + noteworthy, then re-invokes `mm retro-fleet <window> --theme … --noteworthy … --name … --no-save` to render the final card. Direct CLI users (no skill) get the body without the card.

Under the hood the skill invokes `mm retro-fleet <window>` (v0.11.22+) — the same CLI surface is available directly for scripted exports (`mm retro-fleet 30d > /tmp/retro.md`) or terminal use, just without the LLM judgment layer the skill adds (natural-language window parsing, error translation). The earlier `python -m mind_meld.skills.retro_fleet.aggregator` form is a development-checkout fallback only; pipx-installed mm lives in an isolated venv that bare `python` / `python3` can't import from, so the skill's documented invocation routes through the `mm` console-script (always on PATH wherever mm is installed).

**Token usage and cost (v0.11.14).** Under the "Claude Code activity" section the retro now answers: how much did Claude Code consume this window, was it Sonnet- or Opus-heavy, did the cache do its job, what would this have cost at API list rates. The numbers come from `~/.claude/projects/<encoded>/*.jsonl` plus subagent jsonls under `<session-uuid>/subagents/agent-*.jsonl` (subagents contribute to the parent project's totals — ~50% of usage on a heavy fleet — but don't double-count as separate sessions). The cache lives at `~/.config/mind-meld/session-tokens.json`, warms inline on `mm init` and the first interactive `mm push` (~3 seconds, telegraphed via `mm: warming token cache (one-time, ~3s)...`), and is reaped by `mm gc` once a jsonl disappears or its tokens are older than 90 days. Cost estimates use API list prices and explicitly say so — they don't account for subscription plan pricing.

Session jsonls only ever grow, so from v0.12.15 each push re-reads only the bytes appended since the last one rather than the whole file. That's what stopped `mm push` periodically printing `mm: notice: events tail budget exceeded` on machines with a lot of large sessions. If your token cache has gone stale from long-deleted workspaces, `mm gc` reaps those entries and shrinks what every push has to read.

The skill is auto-installed on `mm init` — at `~/.claude/skills/retro-fleet` for Claude Code, and at the Codex and OpenCode equivalents (see [Codex and OpenCode Integration](#codex-and-opencode-integration)) — and each push can create a missing link (24h-TTL gated, a handful of syscalls per available agent in steady state). Each agent's link is tracked separately, so a problem on one never suppresses checks for another. A dangling or foreign link is a deliberate no-clobber conflict: Mind Meld never unlinks it automatically, because another process could replace it with your file. Remove that link yourself, then run `mm install-skills` (or wait for the next push) to create the bundled one.

**Caveats the output is honest about:**

- Asking for a window longer than 90 days surfaces a tail breadcrumb — `mm gc` reaps event files older than `EVENTS_RETENTION_DAYS` (90), so that's the data ceiling.
- Peers still on pre-v0.11.0 emit the older v=1 sessions-snapshot schema (delta semantics). The aggregator omits their session counts honestly rather than overcounting; you'll see `Sessions count incomplete: peer X is on pre-v0.11.0` until they upgrade.
- Devices that haven't pushed during the window are flagged as fleet-incomplete instead of silently dropped.

To filter to your own commits only, the skill consults `git config --global user.email` plus any `[retro].author_emails` aliases in `~/.config/mind-meld/config.toml`. Pass `--no-author-filter` to render every fleet commit. To override the events directory (custom `mm-events` path), set `MM_EVENTS_DIR=/path/to/events` before invoking.

## Handling conflicts

If you edit the same file on two machines before syncing, `mm pull` never destroys your local edits. It follows the Syncthing convention: the incoming remote version wins the canonical path, and your local version is preserved as `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device>.<ext>` sitting next to it. If your local file is newer than the remote (by mtime), pull leaves it alone — convergence happens on the next push.

Managing conflicts:

- `mm conflicts` — list every `.sync-conflict-*` file across your sources, with age and canonical sibling.
- `mm resolve` — walk each conflict interactively. Shows color LOCAL/REMOTE banners (with peer-name attribution when the conflict file's device prefix matches a registered peer), created/modified timestamps for each side plus a `-> SIDE is newer by N` recency verdict, a 3-number divergence summary, the unified diff, and prompts: `(m)erge` (accept LCS-merged result) / `(l)ocal` (keep your edits) / `(r)emote` (overwrite with peer's bytes) / `(n)ewer` (keep whichever was modified more recently) / `(p)romote` (keep BOTH — give the conflict file its own first-class filename) / `(s)kip` (leave both files) / `(a)bort` (stop the walk). The default key is always `(s)kip` — Enter never auto-accepts a merge or a recency guess. The merge uses LCS(local, remote) as a synthetic ancestor so additive edits on either side land cleanly; same-region edits show as `<<<<<<<` markers and (m) stays available. Binary content suppresses (m); `(n)ewer` is offered only when both sides' mtimes are readable and re-prompts on an exact tie (it never guesses). The remote side's "created" is shown as `pulled` — it is the local sync time, not the peer's real creation (the manifest carries only modified time). Acquires the mm lockfile so autopull can't race your decision. Pre-1.0 letters `b` / `both` are aliased to `(s)kip` with a one-time stderr notice.
- `mm pull --conflict-mode prompt` — prompt per-conflict during the pull itself instead of auto keep-both. Shows the same per-side timestamps + recency verdict (display only — no `(n)ewer` shortcut here, since pull already keeps your file when it is the newer one).
- `mm pull --conflict-mode fail` — preflight all files; if any would conflict, print the list and exit 3 (no writes) so CI can block on human review. Exit 3 is distinct from typer's usage-error exit 2, so a stale script still passing the removed `--no-prompt` flag can't be mistaken for a conflict refusal.
- `mm gc --conflicts` — reap stale conflict files older than 30 days.
- `mm diff` — predicts each modified file's pull outcome (write / merge / skip / conflict) before you run pull.

## Architecture

See [SPEC.md](SPEC.md) for full documentation.
