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

## P3 — Three-way merge with stored last-synced base
**What:** Track per-file "last-synced hash" in local state. On pull, use it as the merge base to distinguish "remote changed, I didn't" (safe overwrite) from "we both changed since last sync" (real conflict).
**Why:** The current design (mtime + hash) can produce false conflicts when clocks drift or when mtime is preserved across copies. A stored base makes conflict detection deterministic.
**Pros:** Correct-by-construction conflict detection. Enables smarter auto-resolution (e.g., fast-forward when one side hasn't changed).
**Cons:** Schema change — new state file `~/.config/memsync/sync-state.json` tracking per-source, per-file last-synced hashes. More moving parts, more corruption modes.
**Context:** Conflict-copy preservation (shipped in kbitz/sync-conductor-ctx) is a cheaper approximation that catches the important cases via the Syncthing model + mtime-skip. Revisit if real-world usage shows false-positive conflicts from clock drift or copy-preserves-mtime scenarios.
**Effort:** M | **Depends on:** none
