# Progress

## Version history

| Version | Released | Headline |
|---------|----------|----------|
| 0.6.2   | 2026-04-23 | Track 1B: walker conflict-file exclusion + manifest read-path hardening (`load_manifest` boundary) |
| 0.6.1   | 2026-04-23 | Track 1D: storage layer hardening (`fsutil`, `fcntl.flock`, deferred-durability pull) |
| 0.6.0   | 2026-04-22 | Track 1C: crypto v2 — process-scoped `master_key` + HKDF-SHA256 per-file keys |
| 0.5.1   | 2026-04-22 | Corrupt-manifest recovery chain (sidecar → peers → refuse) + `mm --version` |
| 0.5.0   | 2026-04-22 | Rename memsync/msync → mind-meld/mm |
| 0.4.0   | 2026-04-21 | Conflict-copy preservation + mtime-skip for pull |
| 0.3.0   | (pre-v0.4) | Additive-only sync model with tombstones and conflict resolution |
| 0.2.0   | (pre-v0.3) | Multi-source sync with gstack support |
| 0.1.x   | (pre-v0.2) | iCloud-only backend, autopull/autopush for Claude Code; scoped sync to memory/todos |

## Current phase: pre-1.0 cleanup sweep (v0.x → v1.0)

All work is driving toward a single v1.0 release that cleans up correctness
bugs, decomposition debt, and multi-source assumption lag from the early PRs,
then layers on three pre-1.0 features (selective sync, mtime hash cache,
three-way merge base).

See `docs/ROADMAP.md` for the structured Groups > Tracks > Tasks plan.

### Where we are

- **Correctness foundation (was Group 1):** ✅ shipped in v0.5.1–v0.6.2.
  Tri-state `ManifestFetch` recovery chain (v0.5.1), crypto v2 master_key +
  HKDF (v0.6.0), storage layer hardening via `fsutil` + `fcntl.flock`
  (v0.6.1), and walker conflict-file exclusion + `load_manifest` read-path
  boundary (v0.6.2). Track 1A Task 2 (`_merge_manifests` union) resolved via
  SPEC.md "Merge invariants" — files UNION + tombstones newest-wins is the
  correct policy because the walker is lossy.
- **Group 1 (Error discipline):** not started — next up.
- **Group 2 (Post-v0.5.1 follow-ups):** not started — error-surface cleanups
  and small CLI safety additions unblocked by v0.5.1/v0.6.0 landing.
- **Groups 3–6 (Refactor + hygiene):** not started — queued.
- **Groups 7–9 (P2/P3 features, parallel-safe after Group 6):** not started.

### Version source of truth

`pyproject.toml` is the single source of truth for the release number.
`src/mind_meld/__init__.py` reads it at import time via
`importlib.metadata.version("mind-meld")` and falls back to `"0.0.0+dev"`
when the package is not installed (source-tree runs). There is no
separate `VERSION` file.
