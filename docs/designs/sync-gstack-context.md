# Multi-Source Sync: gstack context across machines

> Generated from CEO plan review on 2026-04-08.
> Supplements SPEC.md with the multi-source architecture and gstack sync decisions.

## Problem

Mind Meld syncs only `~/.claude/projects/*/memory/` and `*/todos/`. The `~/.gstack/` directory contains accumulated AI developer context (learnings, design docs, CEO plans, review logs, timelines, analytics, retros, config) that compounds over time and is lost when switching machines.

## Solution: Configurable Sync Sources

Replace the hardcoded single-source walker with configurable `[[sync.sources]]` in config.toml. Each source defines a name, path, type, and include patterns.

## Scope Decisions

| # | Proposal | Decision | Reasoning |
|---|----------|----------|-----------|
| 1 | JSONL merge strategy | ACCEPTED | Concurrent gstack usage loses append-only data without it |
| 2 | Per-source pull/status/diff flags | ACCEPTED | --source for troubleshooting (NOT push) |
| 3 | Source-aware sync log for gstack | SKIPPED | gstack writes to its own dirs, would overwrite |
| 4 | Auto-detect sources on init | ACCEPTED | Zero friction for existing gstack users |
| 5 | mm sources command | ACCEPTED | Lists configured sync sources with status |
| 6 | Per-source status breakdown | ACCEPTED | Changes grouped by source in mm status |
| 7 | Default source configs | ACCEPTED | Built-in definitions for claude/gstack |

## Cross-Model Tension Resolutions (Claude + Codex)

| Tension | Resolution |
|---------|-----------|
| Silent partition on upgrade | v2 manifest includes BOTH `files` (v1 compat) AND `sources` (v2) |
| Blacklist vs whitelist for gstack | Whitelist (include_dirs + include_files). Safer default. |
| --source on push | Removed. Push always pushes all sources. |
| Hash-then-read race | Read file once, hash + encrypt same bytes. |

## Manifest Schema (v2)

v2 manifests include BOTH `files` and `sources` for seamless backward compat:

```json
{
  "device_id": "a1b2c3d4",
  "device_name": "MacBook Pro",
  "timestamp": "2026-04-08T12:00:00Z",
  "files": { "...claude files only for v1 compat..." },
  "sources": {
    "claude": { "base_path": "/Users/alice/.claude", "files": { "..." } },
    "gstack": { "base_path": "/Users/alice/.gstack", "files": { "..." } }
  }
}
```

- Old code reads `files`, gets claude data, syncs normally.
- New code reads `sources`, gets everything.
- New code reading v1 manifests: falls back to `files`, wraps as claude source.

## Config Schema

```toml
[[sync.sources]]
name = "claude"
path = "~/.claude"
type = "claude"

[[sync.sources]]
name = "gstack"
path = "~/.gstack"
type = "generic"
include_dirs = ["projects", "analytics", "retros"]
include_files = ["config.yaml", "retro-context.md", "greptile-history.md",
                 ".completeness-intro-seen", ".telemetry-prompted",
                 ".proactive-prompted", ".welcome-seen", ".codex-desc-healed"]
```

Old `sync.claude_dir` auto-converts to single claude source on load.

## Source Types

- **"claude"**: existing walker (projects/*/memory|todos). Unchanged.
- **"generic"**: new walker. Walks only include_dirs recursively + include_files at root. Whitelist-based.

## JSONL Merge Strategy

On pull, `.jsonl` files use merge instead of overwrite:
1. Union of lines (byte-exact dedup after whitespace strip)
2. Records whose JSON object carries a string `ts` sort first, by `(ts, original line)`; since v0.14.5 a non-string `ts` (number, bool, null, array, object) is not a sort key
3. Every other line (missing or non-string `ts`, non-object JSON, malformed text) follows, sorted lexicographically by the whole line

Merged result becomes new truth on next push. SPEC.md "JSONL Merge on Pull" and `docs/invariants/conflicts.md` "JSONL line-union ordering" hold the full contract, including the numeric-only reorder and the mixed-version rollout note.

## Key Architecture Notes

- **Hash-then-read fix:** Read file once into bytes, hash + encrypt same bytes. Eliminates race for live JSONL files.
- **--source flag:** Pull/status/diff only. Push always pushes all. Deletions scoped to selected source.
- **GC:** Updated to iterate `sources.*.files`. Uses backward-compat shim for v1 manifests.
- **Sync logs:** Claude source only. Generic sources don't produce sync logs.

## Deferred

- Source-aware sync log for gstack (gstack manages its own project dirs)
