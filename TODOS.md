# TODOS

## P2 — Selective sync (per-project filtering)
**What:** Allow `sync.include` / `sync.exclude` in config.toml to filter which projects are synced.
**Why:** Users with dozens of Claude projects waste bandwidth syncing all when they only use 2-3 across machines.
**Context:** Syncing everything is fine for v1. Filter matching should use glob patterns against project directory names.
**Effort:** M | **Depends on:** Phase 1 complete

## P3 — Mtime-based hash caching
**What:** Cache local manifest with mtimes at `~/.config/memsync/local-manifest.json`. Only re-hash files whose mtime changed.
**Why:** Makes repeat pushes near-instant when nothing changed. Currently re-hashes every file on every push.
**Context:** Not needed until users have thousands of session files. Full re-hash is sub-second for typical usage. Mtime can be unreliable on some filesystems — may need fallback to full hash.
**Effort:** S | **Depends on:** Phase 1 complete
