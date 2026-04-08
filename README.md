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
| `msync status` | Show local vs remote state |
| `msync devices` | List registered devices |
| `msync diff` | Dry-run: show what would change |
| `msync gc` | Delete orphaned blobs |
| `msync sources` | List configured sync sources |

### Syncing gstack

If `~/.gstack` is detected during `msync init`, it is automatically added as a sync source. gstack data (projects, analytics, retros, and config files) syncs alongside Claude Code data using the same encrypted push/pull workflow.

- gstack uses a **whitelist-based** walker: only configured `include_dirs` and `include_files` are synced.
- `.jsonl` files (review logs, analytics) are **merged** on pull instead of overwritten, preserving entries from both machines.
- To check configured sources: `msync sources`
- To pull only gstack data: `msync pull --source gstack`

No extra setup needed — if gstack is present, it syncs.

## Architecture

See [SPEC.md](SPEC.md) for full documentation.
