# MemSync

Sync Claude Code memory, todos, and gstack context across Macs via iCloud Drive. End-to-end encrypted. Supports multiple sync sources.

## Install

```bash
pipx install memsync
```

## Quick Start

```bash
msync init    # configure iCloud storage + passphrase
msync push    # upload memory/todos
msync pull    # download from another device
```

## Claude Code Integration

MemSync includes `autopull` and `autopush` commands designed for Claude Code — they run silently, never prompt, and output a single line summary (or nothing if already in sync).

Add the following to your **global** `~/.claude/CLAUDE.md` to have Claude automatically sync at the start and end of each conversation:

```markdown
# MemSync

At the **start of each conversation**, run:

\`\`\`bash
msync autopull
\`\`\`

- **No output:** Already in sync. Continue silently.
- **Any output:** Tell the user what was synced.

At the **end of each conversation** (when the user is wrapping up, says goodbye,
or you've completed the requested task), run:

\`\`\`bash
msync autopush
\`\`\`

- **No output:** Nothing to push. Say goodbye normally.
- **Any output:** Tell the user what was pushed.

If `msync` is not installed, both commands will fail silently — no action needed.
```

### How it works

- `msync autopull` checks all other registered devices for changes and applies them locally. It writes a `.memsync-log.md` breadcrumb to each affected project so Claude Code knows what changed.
- `msync autopush` builds a manifest of local memory/todos, diffs against the last push, and uploads only what changed.
- Both commands acquire a lockfile, never prompt for input, and exit gracefully on any error (so they never block Claude Code).

### Manual commands

| Command | Description |
|---------|-------------|
| `msync init` | Configure device, storage path, passphrase |
| `msync push` | Push with verbose output |
| `msync pull` | Pull with verbose output |
| `msync pull --resolve-interactive` | Pick a winner per-file at pull time instead of auto keep-both |
| `msync pull --no-prompt` | Never prompt on conflicts — always keep-both (for scripting) |
| `msync status` | Show local vs remote state |
| `msync devices` | List registered devices |
| `msync diff` | Dry-run: show what would change (annotates each file with write / merge / skip / conflict) |
| `msync gc` | Delete orphaned blobs |
| `msync gc --conflicts` | Also delete `.sync-conflict-*` files older than 30 days |
| `msync sources` | List configured sync sources |
| `msync conflicts` | List unresolved `.sync-conflict-*` files with age and canonical sibling |
| `msync resolve [PATH]` | Interactively pick a winner for conflict files (shows unified diff) |

### Syncing gstack

If `~/.gstack` is detected during `msync init`, it is automatically added as a sync source. gstack data (projects, analytics, retros, and config files) syncs alongside Claude Code data using the same encrypted push/pull workflow.

- gstack uses a **whitelist-based** walker: only configured `include_dirs` and `include_files` are synced.
- `.jsonl` files (review logs, analytics) are **merged** on pull instead of overwritten, preserving entries from both machines.
- To check configured sources: `msync sources`
- To pull only gstack data: `msync pull --source gstack`

No extra setup needed — if gstack is present, it syncs.

## Handling conflicts

If you edit the same file on two machines before syncing, `msync pull` never destroys your local edits. It follows the Syncthing convention: the incoming remote version wins the canonical path, and your local version is preserved as `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device>.<ext>` sitting next to it. If your local file is newer than the remote (by mtime), pull leaves it alone — convergence happens on the next push.

Managing conflicts:

- `msync conflicts` — list every `.sync-conflict-*` file across your sources, with age and canonical sibling.
- `msync resolve` — walk each conflict interactively. Shows a unified diff and prompts: keep canonical / force conflict to canonical / keep both / abort. Acquires the msync lockfile so autopull can't race your decision.
- `msync pull --resolve-interactive` — prompt per-conflict during the pull itself instead of auto keep-both.
- `msync gc --conflicts` — reap stale conflict files older than 30 days.
- `msync diff` — predicts each modified file's pull outcome (write / merge / skip / conflict) before you run pull.

## Architecture

See [SPEC.md](SPEC.md) for full documentation.
