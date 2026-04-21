# CLAUDE.md

## Project
MemSync (msync) — CLI tool for syncing ~/.claude session data and ~/.gstack context across Macs via iCloud Drive. Supports multiple configurable sync sources.

## Stack
Python 3.11+, typer, cryptography, argon2-cffi, keyring, rich.

## Key Principles
- No API server. CLI talks directly to iCloud Drive via the local filesystem.
- Single storage backend: local folder at `~/Library/Mobile Documents/com~apple~CloudDocs/memsync`, synced by iCloud.
- **End-to-end encrypted.** All data (sessions, manifests, artifacts) encrypted client-side with AES-256-GCM before touching storage. The storage layer never sees plaintext. This is a hard invariant — no code path may write unencrypted session data to storage.
- **Scoped sync.** Only syncs `memory/` and `todos/` within each project — not sessions, settings, or other git-tracked files.
- **Truth-based manifests.** Manifests are complete snapshots of local state. Deletions propagate automatically — no separate prune step.
- **Conflict resolution.** Detects and resolves iCloud and Dropbox-style conflict copies on manifest files. For source files with divergent local edits, preserves the local version as `<stem>.sync-conflict-<ts>-<device>.<ext>` (Syncthing convention) so edits are never destroyed. Mtime-skip: if the local file is newer than remote, pull leaves it alone.
- **Sync log.** After pull, writes `.memsync-log.md` per project so Claude Code knows what changed from other machines.
- Manifest-based diffing: SHA-256 hash every file, only upload/download changes.
- Content-addressed storage: blobs stored by hash, not by path.
- Gzip compression before encryption. Versioned blob format (v0x01).

## Source Layout
src/memsync/{cli,manifest,crypto,errors,devices,config,lockfile,synclog,merge}.py
src/memsync/storage/{local}.py

## Testing
pytest. Use tmp_path for local backend. Run: `pytest tests/`

## Commands
msync init | push | pull | status | devices | diff | gc | sources | conflicts | resolve | autopull | autopush

Pull flags: `--resolve-interactive` (prompt per-file), `--no-prompt` (script mode, always keep-both).
GC flags: `--conflicts` (also reap `.sync-conflict-*` files older than 30 days).

## Auto Commands (for Claude Code integration)
- `msync autopull` — silent pull, one-line output, never prompts, graceful on errors
- `msync autopush` — silent push, one-line output, never prompts, graceful on errors
- Both exit silently if msync is not initialized or no changes exist
- See README.md "Claude Code Integration" section for CLAUDE.md snippet

## Spec
See SPEC.md for full architecture and data model.
See docs/designs/memsync-v1.md for design decisions from spec review.
See docs/designs/sync-gstack-context.md for multi-source sync design (gstack support).
