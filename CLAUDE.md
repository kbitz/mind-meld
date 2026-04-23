# CLAUDE.md

## Project
Mind Meld (mm) — CLI tool for syncing ~/.claude session data and ~/.gstack context across Macs via iCloud Drive. Supports multiple configurable sync sources.

## Stack
Python 3.11+, typer, cryptography, argon2-cffi, keyring, rich.

## Key Principles
- No API server. CLI talks directly to iCloud Drive via the local filesystem.
- Single storage backend: local folder at `~/Library/Mobile Documents/com~apple~CloudDocs/mind-meld`, synced by iCloud.
- **End-to-end encrypted.** All data (sessions, manifests, artifacts) encrypted client-side with AES-256-GCM before touching storage. The storage layer never sees plaintext. This is a hard invariant — no code path may write unencrypted session data to storage.
- **Scoped sync.** Only syncs `memory/` and `todos/` within each project — not sessions, settings, or other git-tracked files.
- **Truth-based manifests.** Manifests are complete snapshots of local state. Deletions propagate automatically — no separate prune step.
- **Conflict resolution.** Detects and resolves iCloud and Dropbox-style conflict copies on manifest files. For source files with divergent local edits, preserves the local version as `<stem>.sync-conflict-<ts>-<device>.<ext>` (Syncthing convention) so edits are never destroyed. Mtime-skip: if the local file is newer than remote, pull leaves it alone.
- **Sync log.** After pull, writes `.mind-meld-log.md` per project so Claude Code knows what changed from other machines.
- Manifest-based diffing: SHA-256 hash every file, only upload/download changes.
- Content-addressed storage: blobs stored by hash, not by path.
- Gzip compression before encryption. Versioned blob format (v0x01).

## Source Layout
src/mind_meld/{cli,manifest,crypto,errors,devices,config,lockfile,synclog,merge,sidecar}.py
src/mind_meld/storage/{local}.py

Version source of truth: `pyproject.toml` (read by `__init__.py` via `importlib.metadata.version("mind-meld")`, fallback `"0.0.0+dev"` for uninstalled source-tree runs). No `VERSION` file.

## Testing
pytest. Use tmp_path for local backend. Run: `pytest tests/`

## Commands
mm --version | init | push | pull | status | devices | diff | gc | sources | conflicts | resolve | autopull | autopush

Pull flags: `--resolve-interactive` (prompt per-file), `--no-prompt` (script mode, always keep-both).
GC flags: `--conflicts` (also reap `.sync-conflict-*` files older than 30 days).

## Corrupt-manifest recovery (load-bearing)
`_fetch_remote_manifest` returns a tri-state `ManifestFetch(status: "ok"|"missing"|"corrupt", manifest)`. On `corrupt`, `push` runs a recovery chain before writing a new manifest: (1) local sidecar at `~/.config/mind-meld/last-push.json` (preserves this device's fresh deletions), (2) peer-manifest tombstone aggregation (propagated deletions only), (3) refuse with actionable error. Never treat corrupt as empty — that silently un-deletes files fleet-wide. `mm gc` refuses when any peer manifest is corrupt (referenced blobs may still be live). See SPEC.md "Manifest corruption recovery" and "Merge invariants" for the full invariant.

## Manifest read-path invariant (load-bearing)
Every manifest loaded from bytes/disk MUST go through `manifest.load_manifest(bytes) -> dict`, which composes `deserialize_manifest + normalize_manifest` plus full inner-shape validation. The function guarantees the returned dict has dict-typed `sources` and `tombstones`, each source has a dict `files`, and each tombstone value is a dict. Malformed manifests raise `ManifestError` at the load boundary instead of crashing downstream consumers (`_merge_manifests`, `collect_tombstones`, `generate_tombstones`, the diff loop) with `AttributeError`. `_fetch_remote_manifest` already catches `ManifestError` and falls through to the recovery chain, so a malformed peer manifest degrades to a clean "corrupt" status. Do NOT add a new manifest-load path that bypasses `load_manifest` (sidecar.read uses `deserialize_manifest + structural-check + normalize_manifest` deliberately, to preserve the anti-tampering guard on raw input).

## Auto Commands (for Claude Code integration)
- `mm autopull` — silent pull, one-line output, never prompts, graceful on errors
- `mm autopush` — silent push, one-line output, never prompts, graceful on errors
- Both exit silently if mm is not initialized (no config) or no changes exist
- `ConfigError` (bad `config.toml`) surfaces as a one-line stderr message — not a silent exit. This is the visible-failure contract: truly unexpected errors still degrade silently via the generic `except Exception` fallback, but malformed config is loud so users don't wedge their background sync without noticing. Relies on `load_config` normalizing non-`ConfigError` exceptions (e.g. cyclic-symlink `.resolve()` failures) into `ConfigError` at the load boundary — do not bypass that by calling `_validate` / `_apply_defaults` directly from a new call site.
- See README.md "Claude Code Integration" section for CLAUDE.md snippet

## Spec
See SPEC.md for full architecture and data model.
See docs/designs/mind-meld-v1.md for design decisions from spec review.
See docs/designs/sync-gstack-context.md for multi-source sync design (gstack support).
