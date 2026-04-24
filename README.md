# Mind Meld

Sync Claude Code memory, todos, and gstack context across Macs via iCloud Drive. End-to-end encrypted. Supports multiple sync sources.

## Install

```bash
pipx install mind-meld
```

## Quick Start

```bash
mm init    # configure iCloud storage + passphrase
mm push    # upload memory/todos
mm pull    # download from another device
```

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
- "Silent" means no chatter on the happy path. Load-bearing degradation warnings — corrupt-manifest recovery, "no sync sources" misconfig, durability fsync failure, per-file pull failures — still reach stderr as a single `mm: warning: ...` line so a wedged background sync surfaces instead of rotting. Autopush also writes a `no-sources` breadcrumb (separate from `success`) when the config has no sync sources, so `mm status` and any monitoring on top of it can catch the wedge.

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

If `~/.gstack` is detected during `mm init`, it is automatically added as a sync source. gstack data (projects, analytics, retros, and config files) syncs alongside Claude Code data using the same encrypted push/pull workflow.

- gstack uses a **whitelist-based** walker: only configured `include_dirs` and `include_files` are synced.
- `.jsonl` files (review logs, analytics) are **merged** on pull instead of overwritten, preserving entries from both machines.
- To check configured sources: `mm sources`
- To pull only gstack data: `mm pull --source gstack`

No extra setup needed — if gstack is present, it syncs.

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
