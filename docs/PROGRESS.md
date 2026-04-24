# Progress

## Version history

| Version | Released | Headline |
|---------|----------|----------|
| 0.8.5   | 2026-04-24 | Track 1B (Group 1) — Walker + manifest + merge DRY: `_record_file` helper (collapses walk_claude_source + walk_generic_source per-file blocks), `_is_active_tombstone` helper (collapses generate_tombstones carry-forward + collect_tombstones aggregation), `_merge_strategy` + `_join_lines` helpers. Also drops redundant `normalize_manifest` call at manifest.py:607 — the call was positionally wrong (ran after the carry-forward loop had already consumed tombstone keys) and all three caller paths already satisfy the load-path invariant. `generate_tombstones` now enforces the caller contract at runtime: a v1-shaped dict (no `"sources"` key) raises `ManifestError` rather than silently producing zero tombstones. Two review passes landed corrections pre-merge: /plan-eng-review codex outside-voice (5 design corrections — `_record_file` signature taking `(path, base)`; direct `_merge_strategy -> Callable` instead of a registry; full-predicate `_is_active_tombstone` instead of parse-only helper; exact on_skip reason strings pinned in tests; Task 4 contract change owned with a test); /review cross-model adversarial (Claude + Codex independently confirmed a silent-delete-propagation regression on raw v1 input — fixed by promoting the documented contract to a runtime `ManifestError` guard). 10 new tests, 571 pass, zero regression against any v2-normalized caller. |
| 0.8.4   | 2026-04-23 | Group 2 pre-flight + Track 2A: storage-key helpers (`storage/keys.py` with path-traversal validation) + cli.py decomposition (`_pull_core` → 6 helpers + single print-owner, `_apply_incoming_file` → 3 per-outcome helpers). Internal refactor, zero user-visible behavior change; two codex-found regressions (`had_changes` exclusion of `unchanged`, per-file `blob_key` validation) caught and fixed pre-merge. |
| 0.8.3   | 2026-04-23 | Public-release prep: adds MIT `LICENSE` file (closes the gap where `pyproject.toml` declared `license = "MIT"` but the text wasn't shipped) and scrubs the placeholder `/Users/kb/` username out of SPEC.md, sync-gstack-context design doc, and one test fixture. No runtime behavior change. |
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
- **Storage-key helpers + cli.py decomposition:** ✅ shipped in v0.8.4.
  `storage/keys.py` (manifest_key/blob_key/device_key/parse_blob_key) with
  path-traversal validation at construction; `_pull_core` decomposed into
  `_select_devices`/`_prefetch_manifests`/`_preflight_conflicts`/
  `_pull_one_source`/`_fsync_touched_parents`/`_print_pull_summary`;
  `_apply_incoming_file` split into `_apply_write`/`_apply_merge`/
  `_apply_conflict`. Two codex-found regressions (had_changes excluding
  `unchanged`, per-file blob_key ValueError isolation) fixed before merge.
- **Roadmap slimmed for v1.0 (2026-04-23).** Groups 5/6/7 (selective sync,
  mtime hash cache, three-way merge base) moved to Future — all were labeled
  P2/P3 with no user demand, and mtime cache's motivating problem was
  already solved by crypto v2. Style nits collapsed into pre-flight. Three
  follow-up items from TODOS.md slotted into new Track 1C. Now 4 groups / 7
  tracks / 19 tasks (down from 8 groups / 12 tracks / 38 tasks).
- **Public-release prep:** ✅ shipped in v0.8.3. MIT `LICENSE` file added
  + `/Users/kb/` placeholder scrubbed from SPEC / design doc / test fixture.
- **Group 1 (Decomposition + DRY):** Track 1A ✅ shipped in v0.8.4
  (decomposition + storage-key portion of pre-flight). Track 1B ✅ shipped
  in v0.8.5 (`_record_file`, `_is_active_tombstone`, `_merge_strategy` +
  `_join_lines`, plus dropped redundant `normalize_manifest` at
  manifest.py:607 with the contract change owned by a dedicated test).
  Remaining: pre-flight `constants.py` extraction (CONFLICT_INFIX,
  CONFLICT_AGE_DAYS, TOMBSTONE_TTL_DAYS, FORMAT_VERSION) and the cli.py
  literal-site migration; Track 1C (post-1A cli.py follow-ups) queued.
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
