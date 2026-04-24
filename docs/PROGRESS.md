# Progress

## Version history

| Version | Released | Headline |
|---------|----------|----------|
| 0.8.1   | 2026-04-23 | Track 1B (Group 1): manifest dead-code cleanup + v1-holdover removal (delete `walk_directory`/`build_manifest`, drop top-level `"files"` mirror, `diff_manifests` → `diff_files`, `DiffResult` → `@dataclass`) |
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

- **Correctness foundation (was Group 1):** ✅ shipped in v0.5.1–v0.6.2.
  Tri-state `ManifestFetch` recovery chain (v0.5.1), crypto v2 master_key +
  HKDF (v0.6.0), storage layer hardening via `fsutil` + `fcntl.flock`
  (v0.6.1), and walker conflict-file exclusion + `load_manifest` read-path
  boundary (v0.6.2). Track 1A Task 2 (`_merge_manifests` union) resolved via
  SPEC.md "Merge invariants" — files UNION + tombstones newest-wins is the
  correct policy because the walker is lossy.
- **Error discipline (was Group 1, now removed from roadmap):** ✅ complete.
  Track 1A (silent failures in cli.py + `--conflict-mode` unification) shipped
  in v0.7.0. Track 1B (config eager validation + legacy cleanup) shipped in
  v0.7.1.
- **Post-v0.5.1 follow-ups (was Group 2, now removed from roadmap):** ✅
  complete. Group 2 pre-flight (`_merge_manifests` tiebreak, `mm diag`, `mm
  init` two-tier guard) + Track 2A (`mm recover --abandon-manifest`, `_error()`
  stderr routing, `list_devices` shape validation) shipped in v0.8.0.
- **Roadmap renumbered after v0.8.0.** Groups 1+2 removed (entirely shipped);
  former Groups 3–9 are now Groups 1–7. New Group 8 (Release infrastructure,
  CI workflow) added. New Track 3B (Config polish — eng-review follow-ups)
  added. New Track 1A task (full quiet-path audit in cli.py) added.
- **Group 1 (cli.py hardening + manifest dead code):** not started — queued.
- **Groups 2–4 (Decomposition + DRY, init flow + config polish, test hygiene
  + style polish):** not started — queued.
- **Groups 5–7 (P2/P3 features, parallel-safe after Group 4):** not started.
- **Group 8 (Release infrastructure, GitHub Actions CI):** not started —
  parallel-safe with everything; can land anytime.

### Version source of truth

`pyproject.toml` is the single source of truth for the release number.
`src/mind_meld/__init__.py` reads it at import time via
`importlib.metadata.version("mind-meld")` and falls back to `"0.0.0+dev"`
when the package is not installed (source-tree runs). There is no
separate `VERSION` file.
