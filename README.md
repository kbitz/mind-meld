# Mind Meld

[![CI](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml/badge.svg)](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml)

Sync Claude Code memory, todos, and gstack context across Macs via iCloud Drive. End-to-end encrypted. Supports multiple sync sources.

## Install

```bash
pipx install git+https://github.com/kbitz/mind-meld.git
```

Not on PyPI — install straight from GitHub.

## Quick Start

```bash
mm init    # configure iCloud storage + passphrase
mm push    # upload memory/todos
mm pull    # download from another device
```

Config lives at `~/.config/mind-meld/config.toml` — not tied to your current directory. Install `mm` anywhere, run from anywhere; it always syncs the sources configured in your global config (`~/.claude` and `~/.gstack` by default).

## Setting up a second (or third) Mac

1. `pipx install git+https://github.com/kbitz/mind-meld.git` on the new machine.
2. `mm init` — point it at the **same iCloud folder** as your first Mac and enter the **same passphrase**. This registers the new device against the existing roster.
3. `mm pull` — downloads everything the other machine(s) have pushed.
4. `mm push` — uploads anything this machine has that the others don't.

Push and pull on each Mac over time and the state converges toward the union of all three. Nothing is destroyed:

- **`.jsonl` files and `MEMORY.md`** deep-merge by line-union (deduped, sorted by `ts`). Entries from all machines accumulate — this is why telemetry, learnings, and timeline files stay coherent across devices.
- **Other divergent files** use mtime-skip: if your local file is newer than the remote, pull leaves it alone. Otherwise the remote wins the canonical path and your local version is preserved as `<stem>.sync-conflict-<ts>-<device>.<ext>` sitting next to it (Syncthing convention). See [Handling conflicts](#handling-conflicts) below.
- **Deletions** propagate via tombstones in the manifest — delete a file on one machine, and `mm pull` on the others removes it cleanly.

First-run-from-divergent-state is explicitly supported: if each Mac already has its own memory/todos/analytics before you first run `mm init`, the three-way sync will merge the JSONLs, download missing files, and flag any true content conflicts as `.sync-conflict-*` for you to triage with `mm resolve`.

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
- "Silent" means no chatter on the happy path. Load-bearing degradation warnings — corrupt-manifest recovery, "no sync sources" misconfig, durability fsync failure, per-file pull failures — still reach stderr as a single `mm: warning: ...` line so a wedged background sync surfaces instead of rotting. Autopush writes a `no-sources` breadcrumb (separate from `success`) when the config has no sync sources. Autopull writes a `degraded` breadcrumb (separate from `success`) when any of four conditions fire during an otherwise-successful pull: fsync durability failure, corrupt peer manifest, unknown source from a peer, or per-file apply failure. The `detail` field enumerates which signals fired. `mm status` and any monitoring on top of it can catch both wedge and partial-degradation cases.
- **Auto-upgrade nudge (v0.9.5).** Once per 24h, `mm pull` / `mm push` (including the autopull/autopush variants) check GitHub for a newer release tag and emit a single `mm: notice: a newer mind-meld is available — run pipx install --force git+...@vX.Y.Z` line on stderr if you're behind. `mm` never invokes pipx itself; you run the printed command. Disable with `--no-check-version` for one invocation, or set `[upgrade] auto_check = false` in `~/.config/mind-meld/config.toml` to disable persistently. The `notice:` prefix is distinct from `warning:` (reserved for data-at-risk signals). This is a leading-edge complement to the v0.9.2 fleet-version refusal, which only fires after a newer peer pushes data — the nudge fires before that, ideally making the refusal a backstop nobody hits.

### Manual commands

| Command | Description |
|---------|-------------|
| `mm --version` | Print the installed version and exit |
| `mm init` | Configure device, storage path, passphrase |
| `mm push` | Push with verbose output |
| `mm pull` | Pull with verbose output |
| `mm pull --conflict-mode prompt` | Pick a winner per-file at pull time instead of auto keep-both |
| `mm pull --conflict-mode fail` | Preflight all files; exit 3 (no writes) if any would conflict — for CI |
| `mm status` | Show local vs remote state |
| `mm devices` | List registered devices |
| `mm devices --format=json` | Same data as a JSON array on stdout — for scripting (used by `/retro-fleet`) |
| `mm diff` | Dry-run: show what would change (annotates each file with write / merge / skip / conflict) |
| `mm gc` | Delete orphaned blobs |
| `mm gc --conflicts` | Also delete `.sync-conflict-*` files older than 30 days |
| `mm sources` | List configured sync sources |
| `mm conflicts` | List unresolved `.sync-conflict-*` files with age and canonical sibling |
| `mm resolve [PATH]` | Interactively pick a winner for conflict files (shows unified diff). Exits 1 if any per-conflict rename/unlink/read fails so CI / scripts can detect partial failure (the walk still continues through every conflict). |

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

## Disabling sources per machine

`config.toml` lives at `~/.config/mind-meld/` and is never synced — making it the natural home for per-device preferences. To turn off a source on one machine without affecting the others:

```bash
mm disable-source gstack       # this Mac only; iCloud peers untouched
mm enable-source gstack        # turn it back on
```

The on/off state lives in `[sync].disabled_sources = ["gstack"]`. Disabling does NOT delete your `[[sync.sources]]` entry — re-enabling preserves any customizations like `include_dirs` or `exclude_patterns`.

`mm sources` shows the toggle state as an `Enabled` column. `mm status` calls out disabled sources in a one-line breadcrumb so future-you doesn't forget gstack is off and re-debug "why isn't this syncing".

**Forward-compat for not-yet-shipped sources.** When `mm` adds a new source to its defaults (e.g. codex in a future release), upgraders don't get auto-enrolled — `mm status` surfaces a one-shot `New source available: codex. Run mm enable-source codex to sync.` hint. To pre-disable a name before it ships:

```bash
mm disable-source codex --force   # accepts unknown names for forward-compat
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

**Token usage and cost (v0.11.14).** Under the "Claude Code activity" section the retro now answers: how much did Claude Code consume this window, was it Sonnet- or Opus-heavy, did the cache do its job, what would this have cost at API list rates. The numbers come from `~/.claude/projects/<encoded>/*.jsonl` plus subagent jsonls under `<session-uuid>/subagents/agent-*.jsonl` (subagents contribute to the parent project's totals — ~50% of usage on a heavy fleet — but don't double-count as separate sessions). The cache lives at `~/.config/mind-meld/session-tokens.json`, warms inline on `mm init` and the first interactive `mm push` (~3 seconds, telegraphed via `mm: warming token cache (one-time, ~3s)...`), and is reaped by `mm gc` once a jsonl disappears or its tokens are older than 90 days. Cost estimates use API list prices and explicitly say so — they don't account for subscription plan pricing.

The skill is auto-installed at `~/.claude/skills/retro-fleet` on `mm init`, and self-heals every push (24h-TTL gated, ~1 syscall in steady state) so a `pipx reinstall` rebuild can't leave you with a dangling symlink. If you already have your own file at that path, mm leaves it alone and prints a one-time `mm: notice:` so you know.

**Caveats the output is honest about:**

- Asking for a window longer than 90 days surfaces a tail breadcrumb — `mm gc` reaps event files older than `EVENTS_RETENTION_DAYS` (90), so that's the data ceiling.
- Peers still on pre-v0.11.0 emit the older v=1 sessions-snapshot schema (delta semantics). The aggregator omits their session counts honestly rather than overcounting; you'll see `Sessions count incomplete: peer X is on pre-v0.11.0` until they upgrade.
- Devices that haven't pushed during the window are flagged as fleet-incomplete instead of silently dropped.

To filter to your own commits only, the skill consults `git config --global user.email` plus any `[retro].author_emails` aliases in `~/.config/mind-meld/config.toml`. Pass `--no-author-filter` to render every fleet commit. To override the events directory (custom `mm-events` path), set `MM_EVENTS_DIR=/path/to/events` before invoking.

## Handling conflicts

If you edit the same file on two machines before syncing, `mm pull` never destroys your local edits. It follows the Syncthing convention: the incoming remote version wins the canonical path, and your local version is preserved as `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device>.<ext>` sitting next to it. If your local file is newer than the remote (by mtime), pull leaves it alone — convergence happens on the next push.

Managing conflicts:

- `mm conflicts` — list every `.sync-conflict-*` file across your sources, with age and canonical sibling.
- `mm resolve` — walk each conflict interactively. Shows color LOCAL/REMOTE banners (with peer-name attribution when the conflict file's device prefix matches a registered peer), a 3-number divergence summary, the unified diff, and prompts: `(m)erge` (accept LCS-merged result; default when the merge is clean of conflict markers) / `(l)ocal` (keep your edits) / `(r)emote` (overwrite with peer's bytes) / `(s)kip` (leave both files) / `(a)bort` (stop the walk). The merge uses LCS(local, remote) as a synthetic ancestor so additive edits on either side land cleanly; same-region edits show as `<<<<<<<` markers and the (m) option stays available but is not the default. Binary content suppresses (m). Acquires the mm lockfile so autopull can't race your decision. Pre-1.0 letters `b` / `both` are aliased to `(s)kip` with a one-time stderr notice.
- `mm pull --conflict-mode prompt` — prompt per-conflict during the pull itself instead of auto keep-both.
- `mm pull --conflict-mode fail` — preflight all files; if any would conflict, print the list and exit 3 (no writes) so CI can block on human review. Exit 3 is distinct from typer's usage-error exit 2, so a stale script still passing the removed `--no-prompt` flag can't be mistaken for a conflict refusal.
- `mm gc --conflicts` — reap stale conflict files older than 30 days.
- `mm diff` — predicts each modified file's pull outcome (write / merge / skip / conflict) before you run pull.

## Architecture

See [SPEC.md](SPEC.md) for full documentation.
