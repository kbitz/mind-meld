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
- `include_files`: `config.yaml`, `.completeness-intro-seen`, `.telemetry-prompted`, `.proactive-prompted`, `.welcome-seen`, `.codex-desc-healed`

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
    "config.yaml",
    ".completeness-intro-seen",
    ".telemetry-prompted",
    ".proactive-prompted",
    ".welcome-seen",
    ".codex-desc-healed",
    "retro-context.md",       # added
    "greptile-history.md",    # added
]
```

Supplying `sync.sources` replaces the defaults wholesale — copy the full list, don't just add your extras. Run `mm sources` to confirm the resolved source list.

**Useful flags:**

- `mm sources` — show the resolved source list after config + auto-detection.
- `mm pull --source gstack` — pull only the gstack source (skip Claude).

## Handling conflicts

If you edit the same file on two machines before syncing, `mm pull` never destroys your local edits. It follows the Syncthing convention: the incoming remote version wins the canonical path, and your local version is preserved as `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device>.<ext>` sitting next to it. If your local file is newer than the remote (by mtime), pull leaves it alone — convergence happens on the next push.

Managing conflicts:

- `mm conflicts` — list every `.sync-conflict-*` file across your sources, with age and canonical sibling.
- `mm resolve` — walk each conflict interactively. Shows a unified diff and prompts: keep canonical / force conflict to canonical / keep both / abort. Acquires the mm lockfile so autopull can't race your decision.
- `mm pull --conflict-mode prompt` — prompt per-conflict during the pull itself instead of auto keep-both.
- `mm pull --conflict-mode fail` — preflight all files; if any would conflict, print the list and exit 3 (no writes) so CI can block on human review. Exit 3 is distinct from typer's usage-error exit 2, so a stale script still passing the removed `--no-prompt` flag can't be mistaken for a conflict refusal.
- `mm gc --conflicts` — reap stale conflict files older than 30 days.
- `mm diff` — predicts each modified file's pull outcome (write / merge / skip / conflict) before you run pull.

## Architecture

See [SPEC.md](SPEC.md) for full documentation.
