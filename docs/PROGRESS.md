# Progress

## Version history

| Version | Released | Headline |
|---------|----------|----------|
| 0.8.13  | 2026-04-24 | Docs: log 8 conflict-UX TODOs from kb-mbp's first-pull session — `mm resolve` prompt label jargon, pull summary missing inline conflicted filenames, `mm conflicts` table truncation, P0 `mm autopull`/`autopush` silent-mode contract regression on un-initialized machines, `_synced_scan_dirs` missing `include_files` sidecars on generic sources, "real merge" via `git merge-file` + opt-in Claude API for prose, and the load-bearing "invert conflict default" (local stays canonical, remote routes to `.sync-conflict-*`). All triaged into new Group 5 (Conflict UX & first-pull polish) — see ROADMAP.md. |
| 0.8.12  | 2026-04-24 | Docs: README install command corrected to `pip install -e .` from a local clone (project is not on PyPI; PyPI publish is a Future item). |
| 0.8.11  | 2026-04-24 | Group 4 / Track 4A — GitHub Actions CI workflow. Single `macos-latest` + Python 3.13 job runs `ruff check`, `ruff format --check`, `pytest tests/`, wheel build + install + `mm --version` smoke, and asserts the real Keychain backend loads (guards against silent `fail.Keyring` fallback). Ruff pinned at 0.15.12 in dev deps with `[tool.ruff]` config + `E/F/W/I` rule set (isort enforcement locks Group 3's import hoisting). One-time fix-drift commit swept 113 violations clean. README CI badge added. No path filter (avoids the branch-protection pending-forever footgun for path-skipped required checks). |
| 0.8.10  | 2026-04-24 | Group 3 — Test hygiene + style polish. Pre-flight (17 `backend: LocalBackend` hints in cli.py; 6 `Optional[X]` → `X \| None` + `Optional` dropped from typing import; 10 placeholderless f-strings stripped via AST audit; `crypto.py` keyring `except Exception` narrowed to `(KeyringError, ImportError)` + 7 regression pins in `test_crypto.py`). Track 3A (TestPushPullRoundTrip migrated from direct-API to `CliRunner.invoke`; new combined push→pull→conflict→tombstone E2E in test_integration.py; `test_deletion_propagation` renamed to `test_deletion_not_propagated_in_additive_model`; 86 lazy in-function imports hoisted across test_integration.py + test_conflict_copy.py). Codex adversarial during `/review` caught a P0 gap: `_auto_command_setup` + `_get_passphrase_or_exit` caught only `CryptoError`, so non-KeyringError propagation would have crashed uncaught — fixed in-line with 3 more regression pins before merge. 669 tests passing (+11 from baseline 658), zero behavior regressions. |
| 0.8.9   | 2026-04-24 | Docs: multi-machine usage guide (PR #27) — README explains that `mm` reads config from `~/.config/mind-meld/config.toml`, adds "Setting up a second (or third) Mac" bootstrap recipe, and documents three-way convergence (line-union merge for .jsonl/MEMORY.md, mtime-skip with `.sync-conflict-*` for other divergent files, tombstone-propagated deletions). Expanded "Syncing gstack" with default `include_dirs`/`include_files` lists, set-union merge behavior for `analytics/*.jsonl`, machine-local file enumeration (sessions/, builder-profile.jsonl), and the `sync.sources` wholesale-replacement caveat. No code or behavior changes. |
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
- **Group 1 (Decomposition + DRY):** Tracks 1A ✅ v0.8.4, 1B ✅ v0.8.5, 1C
  ✅ v0.8.6 all shipped. Remaining: only the pre-flight `constants.py`
  extraction (CONFLICT_INFIX, CONFLICT_AGE_DAYS, TOMBSTONE_TTL_DAYS,
  FORMAT_VERSION) — storage-key helpers already shipped in v0.8.4.
- **Group 2 (Init flow + sync_log generalization + config polish):** ✓ Complete
  — Track 2A ✅ v0.8.7 (init decomposed into 4 helpers, `DEFAULT_SOURCES`
  reused, `write_sync_log` keyed off source type), Track 2B ✅ v0.8.8
  (backfill path preservation via `patch_config_on_disk`, ConfigError
  prefix rename `init:` → `config:` on non-init paths).
- **Group 3 (Test hygiene + style polish):** ✓ Complete — pre-flight + Track
  3A ✅ v0.8.10 (CliRunner migration, combined push/pull/conflict E2E, 86
  lazy-import hoists, type hints, `Optional[X]` → `X | None`,
  placeholderless f-string strip, keyring `except Exception` narrowed to
  `(KeyringError, ImportError)`). Codex `/review` caught a P0 keyring-
  propagation gap pre-merge; fix shipped with 3 regression pins.
- **Group 4 (Release infrastructure, GitHub Actions CI):** ✓ Complete —
  Track 4A ✅ v0.8.11 (single macos+py3.13 job: ruff check + format-check
  + pytest + wheel smoke + Keychain-backend load assert; ruff pinned
  0.15.12; 113-violation drift sweep; README badge).
- **Group 5 (Conflict UX & first-pull polish):** queued — surfaced
  2026-04-24. 9 tasks across 3 serialized tracks (all touch `cli.py` in
  different functions; intra-Group `Depends on:` annotations express the
  ordering, same pattern as prior Tracks 1A/1C and 2A/2B). **Track 5A
  (Auto-command + scope bugs, ships first):** P0 autopull silent-mode
  contract regression + `_synced_scan_dirs` missing `include_files`
  sidecars + `_save_and_register` rollback. **Track 5B (UX surfaces,
  ships second):** relabel `mm resolve` prompt to user terms + inline
  conflicted filenames in pull summary + `mm conflicts` table fixes +
  download progress. **Track 5C (Conflict semantics, ships last):**
  invert default so local stays canonical and remote routes to
  `.sync-conflict-*` + real merge backends (`git merge-file` + opt-in
  Claude API for prose). + 1 pre-flight (gstack `include_files` default
  add). See ROADMAP.md.

### Version source of truth

`pyproject.toml` is the single source of truth for the release number.
`src/mind_meld/__init__.py` reads it at import time via
`importlib.metadata.version("mind-meld")` and falls back to `"0.0.0+dev"`
when the package is not installed (source-tree runs). There is no
separate `VERSION` file.
