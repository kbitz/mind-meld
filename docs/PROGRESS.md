# Progress

## Version history

| Version | Released | Headline |
|---------|----------|----------|
| 0.8.2   | 2026-04-23 | Track 1B (Group 1): manifest dead-code cleanup + v1-holdover removal (delete `walk_directory`/`build_manifest`, drop top-level `"files"` mirror, `diff_manifests` → `diff_files`, `DiffResult` → `@dataclass`) |
| 0.8.1   | 2026-04-23 | Track 1A (Group 1): cli.py surgical hardening (resolve exit-code propagation, conflict_filename ValueError on empty device_id, GC malformed-blob visibility, quiet-path audit for autopull/autopush recovery + no-sources + fsync failures, total_failed surfacing) |
| 0.8.0   | 2026-04-23 | Group 2 pre-flight + Track 2A: error-surface hardening (`_merge_manifests` tiebreak, `mm diag`, `mm init` two-tier guard, `mm recover --abandon-manifest`, `_error()` stderr routing, `list_devices` shape validation) |
| 0.7.1   | 2026-04-23 | Track 1B: config eager validation + legacy cleanup (bad `config.toml` now fails at load time with typed `ConfigError`) |
| 0.7.0   | 2026-04-23 | Track 1A: silent-failure cleanup in `autopull`/`autopush` + `--conflict-mode` unification (BREAKING: `mm pull --no-prompt` / `--resolve-interactive` removed) |
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

- **Correctness foundation:** ✅ shipped in v0.5.1–v0.6.2. Tri-state
  `ManifestFetch` recovery chain (v0.5.1), crypto v2 master_key + HKDF
  (v0.6.0), storage layer hardening via `fsutil` + `fcntl.flock` (v0.6.1),
  walker conflict-file exclusion + `load_manifest` read-path boundary
  (v0.6.2). `_merge_manifests` union + tombstones-newest-wins resolved via
  SPEC.md "Merge invariants" (the walker is lossy, so UNION is correct).
- **Error discipline:** ✅ shipped in v0.7.0–v0.7.1. Silent-failure cleanup
  in `autopull`/`autopush` + `--conflict-mode` unification (v0.7.0). Config
  eager validation + legacy cleanup — bad `config.toml` fails at load time
  with typed `ConfigError` (v0.7.1).
- **Post-v0.5.1 follow-ups:** ✅ shipped in v0.8.0. `_merge_manifests`
  tiebreak, `mm diag`, `mm init` two-tier guard, `mm recover
  --abandon-manifest`, `_error()` stderr routing, `list_devices` shape
  validation.
- **cli.py surgical hardening:** ✅ shipped in v0.8.1. `resolve --force`
  exit-code propagation, 16-char device_id in conflict filenames, GC
  malformed-blob visibility, quiet-path audit for autopull/autopush recovery
  + no-sources + fsync failures, `total_failed` surfacing.
- **Manifest dead code + v1 holdovers:** ✅ shipped in v0.8.2. Removed
  `walk_directory`/`build_manifest` backward-compat aliases, dropped top-level
  `"files"` mirror in v2 manifests, converted `DiffResult` to `@dataclass`,
  renamed `diff_manifests` → `diff_files`.
- **Roadmap slimmed for v1.0 (2026-04-23).** Groups 5/6/7 (selective sync,
  mtime hash cache, three-way merge base) moved to Future — all were labeled
  P2/P3 with no user demand, and mtime cache's motivating problem was
  already solved by crypto v2. Style nits collapsed into pre-flight. Three
  follow-up items from TODOS.md slotted into new Track 1C. Now 4 groups / 7
  tracks / 19 tasks (down from 8 groups / 12 tracks / 38 tasks).
- **Group 1 (Decomposition + DRY):** in progress. Pre-flight (constants.py +
  storage key helpers) and Track 1A (`_pull_core` + `_apply_incoming_file`
  decomposition) active.
- **Group 2 (Init flow + sync_log generalization + config polish):** queued.
- **Group 3 (Test hygiene + style polish):** queued.
- **Group 4 (Release infrastructure, GitHub Actions CI):** queued —
  parallel-safe with everything; can land anytime.

### Version source of truth

`pyproject.toml` is the single source of truth for the release number.
`src/mind_meld/__init__.py` reads it at import time via
`importlib.metadata.version("mind-meld")` and falls back to `"0.0.0+dev"`
when the package is not installed (source-tree runs). There is no
separate `VERSION` file.
