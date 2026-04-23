# Roadmap — Phase 1 (v0.x → v1.0 cleanup sweep)

Organized as **Groups > Tracks > Tasks**. A Group is a wave of PRs that land
together — parallel-safe within, dependency-ordered between. By default each
Group depends on the immediately preceding Group (single linear chain); Groups
7/8/9 are explicitly parallel-safe after Group 6. Within a Group, Tracks must
be fully parallel-safe (set-disjoint `_touches:_` footprints). Each track is
one plan + implement session.

All items originated from the 2026-04-22 `/full-review` audit plus review
follow-ups accumulated across v0.5.1/v0.6.x. Every item targets the v1.0
release.

**Group 1 (Correctness foundation) shipped in v0.5.1–v0.6.3.** Track 1A tombstone
loss landed as the tri-state `ManifestFetch` recovery chain (v0.5.1). Track 1A
Task 2 (`_merge_manifests` union) was resolved via SPEC.md's "Merge invariants"
section — files UNION + tombstones newest-wins is load-bearing because the
walker is lossy; only tombstones drive deletion. Track 1B walker +
manifest-read-path hardening shipped in v0.6.2. Track 1B config eager validation
+ legacy cleanup shipped in v0.6.3. Track 1C crypto v2 shipped in v0.6.0.
Track 1D storage hardening shipped in v0.6.1.

---

## Group 1: Error discipline

Typed-error hygiene across the CLI and eager config validation. These don't
block user data integrity but they hide real bugs today. After the correctness
foundation work landed, the next concern is that failures surface clearly
instead of being swallowed.

### Track 1A: Silent failures in cli.py
_5 tasks · ~1 day (human) / ~20 min (CC) · low risk · [cli.py]_
_touches: src/mind_meld/cli.py, tests/test_integration.py_

- **Typed catches in autopull/autopush** — `cli.py:1824-1907`: bare `except Exception` hides real bugs behind one-line stderr. Differentiate `Mind MeldError` (expected, one-liner) from `Exception` (unexpected, log traceback to `~/.config/mind_meld/autopull.log` with rotation). Since autopull is what Claude Code runs in the background, hidden data-integrity issues are the worst outcome. _src/mind_meld/cli.py, ~80 lines._ (M)
- **Post-push auto-GC warning** — `cli.py:756-761`: `except Exception: pass` masks wrong-passphrase, storage-permission, and future refactor bugs. Catch `Mind MeldError` explicitly and emit a `[yellow]Warning:[/yellow]` line like the manifest-corruption warning; let unknown exceptions propagate. _src/mind_meld/cli.py, ~20 lines._ (S)
- **Unknown-source warning on pull** — `cli.py:1046-1053`: remote sources not configured locally are silently skipped (verbose-only dim message). Print a warning regardless of verbose and include `skipped_unknown_source` in the pull summary; silent-partition risk when source configuration drifts. _src/mind_meld/cli.py, ~30 lines._ (S)
- **Document `--no-prompt` keep-both behavior** — `cli.py:940-941`: script mode silently keep-boths conflicts with no terminal feedback. Document the three resolve modes in the pull docstring; add `--fail-on-conflict` for CI-style usage. _src/mind_meld/cli.py, ~30 lines._ (S)
- **Pull updates local `last_seen`** — push updates `last_seen` at `cli.py:882`, pull does not. A read-only device appears stale forever. Update on pull (or document "last_seen means last pushed" in a comment if the existing semantic is intentional). _src/mind_meld/cli.py, src/mind_meld/devices.py, ~20 lines._ (S)

### Track 1B: Config eager validation + legacy cleanup — **shipped v0.6.3**
_3 tasks · ~0.5 day (human) / ~10 min (CC) · low risk · [config.py]_
_touches: src/mind_meld/config.py, tests/test_config.py_

- ✅ **Eager source validation** — `_validate_sources` now runs from `_validate` whenever `sync.sources` is present, plus shape guards on every field so malformed TOML raises `ConfigError` at load time with a pointed message. Bonus: non-`ConfigError` exceptions from `_validate` / `_apply_defaults` (e.g. cyclic-symlink `.resolve()`) are normalized to `ConfigError` so `autopull` / `autopush` surface them via the typed-error branch instead of exiting 0 silently. _Shipped v0.6.3._
- ✅ **Delete Python 3.10 tomllib fallback** — `sys.version_info` gate + `tomli` import branch removed; unconditional `import tomllib`. _Shipped v0.6.3._
- ✅ **Scope legacy `claude_dir` defaulting** — `_apply_defaults` no longer injects a default `claude_dir`; expansion runs only when the field is present. `.expanduser().resolve()` now matches the canonical pattern at the 11 other call sites across `cli.py` / `manifest.py` / `storage/local.py` / `synclog.py`. `DEFAULT_SOURCES` and the auto-detected gstack fallback deliberately skip `.resolve()` to avoid cyclic-symlink failures at every command startup — walker resolves at use time. _Shipped v0.6.3._

Two follow-ups captured in `docs/TODOS.md` (both Codex findings from /plan-eng-review):
- Stop mutating config in `_apply_defaults`; compute expanded paths lazily in `get_sources` (avoids silent realpath rewrite on backfill save).
- Rich `ConfigError` with TOML line numbers on parse failure.

---

## Group 2: Post-v0.5.1 follow-ups

Review-cycle residue from v0.5.1 (corrupt-manifest recovery) and v0.6.0 (crypto
v2). Error-message and user-surface cleanups that were blocked on the tri-state
`ManifestFetch` and `mm-crypto-init` bootstrap landing — both now shipped, so
these are unblocked. Pre-flight holds three cli.py-only small additions; the
track handles the meatier signature + stderr-contract work.

**Pre-flight** (shared-infra; serial, one-at-a-time):
- **`_merge_manifests` timestamp tiebreak determinism** — `cli.py:_merge_manifests` is non-deterministic across devices when two conflict copies carry identical ISO timestamps (same-second double-write). Add a `(timestamp, device_id, content-hash)` composite sort key so merged manifests are reproducible across devices. _src/mind_meld/cli.py, ~15 lines._ (S)
- **`mm diag` subcommand** — dumps non-secret crypto state (format version bytes seen, `mm-crypto-init` fingerprint, argon2 params, cache state) for post-hoc debugging. After Track 1C a GCM-tag mismatch has three possible causes (wrong passphrase, wrong root_salt, corrupt blob); support triage needs a way to narrow it down without users posting raw blobs. _src/mind_meld/cli.py, ~40 lines._ (S)
- **`mm init --force` guard** — currently re-running `mm init` on a storage root with existing devices silently re-bootstraps and bricks existing blobs. Require `--i-know-what-im-doing` (or interactive "type BRICK to proceed") before overwriting live crypto state. _src/mind_meld/cli.py, ~30 lines._ (S)

### Track 2A: Error-surface follow-ups
_4 tasks · ~0.5 day (human) / ~15 min (CC) · low risk · [cli.py, devices.py]_
_touches: src/mind_meld/cli.py, src/mind_meld/devices.py, tests/test_integration.py, tests/test_recovery.py_

- **`_recover_prior_manifest` refuse-message rewrite** — current refuse message says "Run 'mm init' if storage is unrecoverable" but `mm init` doesn't delete the corrupt manifest, it generates a new device_id (which silently zeroes the deletion record fleet-wide). Either rewrite the guidance to say "delete `manifests/<device_id>/manifest.json.enc` from storage", or add a dedicated `mm recover --reset-manifest` subcommand. _src/mind_meld/cli.py, ~20 lines (message) or ~60 lines (subcommand)._ (S-M)
- **`_error()` stderr routing** — `_error()` writes via `console.print`, which goes to stdout. In `autopush`/`autopull` quiet mode this violates the "silent, one-line output" contract documented for `mm autopull`/`mm autopush` in README.md, so failures currently emit both a rich stdout line AND the outer plain-text stderr line — confusing Claude Code integration. Route `_error` output to stderr. _src/mind_meld/cli.py, ~15 lines._ (S)
- **`list_devices` corrupt entries warning** — `devices.py:53` silently drops corrupt `devices/*.json` entries; if a peer's `devices.json` is corrupt but its manifest is intact, peer fallback misses that peer and the corruption-recovery chain silently loses a data source. Log a warning per dropped peer, and consider blob directory listing as a secondary peer-discovery path. _src/mind_meld/devices.py, src/mind_meld/cli.py (`_collect_peer_tombstones`), ~40 lines._ (M)
- **cli.py `Optional[X]` signature audit** — Codex flagged during Track 1A review that `_fetch_remote_manifest`'s `-> X | None` conflated "not-found" vs "error" and was the visible symptom of a wider pattern. Now that the tri-state migration has landed, sweep other `-> X | None` / `-> Optional[X]` signatures in `cli.py` and classify: (a) None is genuinely one meaning, (b) None conflates ≥2 states and callers would benefit from an explicit result type, (c) borderline — document the meaning of None in the docstring. Bound by time, not completeness. _src/mind_meld/cli.py, ~60 lines._ (S-M)

---

## Group 3: cli.py hardening + dead code in manifest

Surgical CLI hardening (atomic renames, collision-resistant filenames, robust
GC) alongside manifest dead-code removal. Parallel-safe across files.

### Track 3A: cli.py surgical hardening + `_delete_files` removal
_5 tasks · ~1 day (human) / ~15 min (CC) · medium risk · [cli.py]_
_touches: src/mind_meld/cli.py, tests/test_conflict_copy.py, tests/test_storage_local.py_

- **`resolve --force` atomic rename** — `cli.py:1769-1775`: bare `Path.rename` fails across filesystems and has no tmp-then-rename safety. Use `shutil.move` or read-write-rename via `_atomic_write`; surface rename failure as a non-zero exit code. _src/mind_meld/cli.py, tests/test_conflict_copy.py, ~40 lines._ (S)
- **16-char device_id in conflict filenames** — `cli.py:54, 541`: `device_id[:8]` truncation risks collision between two machines in same-second conflict-of-a-conflict scenarios. Widen to 16-char prefix (or full device_id). _src/mind_meld/cli.py, ~15 lines._ (S)
- **GC logs malformed blob paths** — `cli.py:1482-1497`: `_do_gc` silently ignores blobs at unexpected depth; orphaned `.tmp` artifacts from crashed pushes accumulate forever. Log (and optionally reap) `data/**` files that don't match the expected `data/{device}/{sha}.enc` pattern; add a test. _src/mind_meld/cli.py, tests/test_storage_local.py, ~30 lines._ (S)
- **Remove `_delete_files` dead code** — `cli.py:624-638`: never called after the additive-only refactor (v0.3.0); a future maintainer will re-wire delete-on-pull behavior that the spec forbids. Delete the function. _src/mind_meld/cli.py, ~15 lines._ (S)
- **Drop unused `TOMBSTONE_TTL_DAYS` import** — `cli.py:35`: imported but never referenced. _src/mind_meld/cli.py, ~2 lines._ (S)

### Track 3B: Manifest dead code + v1-holdover cleanup
_4 tasks · ~1 day (human) / ~15 min (CC) · low risk · [manifest.py, manifest tests]_
_touches: src/mind_meld/manifest.py, tests/test_manifest.py, tests/test_additive_sync.py, tests/test_integration.py_

- **Delete `walk_directory` and `build_manifest`** — `manifest.py:182-209`: backward-compat aliases with no production callers; `test_manifest.py`/`test_integration.py`/`test_additive_sync.py` are the only users. Delete both and migrate the three test files to `walk_claude_source` / `build_manifest_v2`. _src/mind_meld/manifest.py, tests/test_manifest.py, tests/test_additive_sync.py, tests/test_integration.py, ~60 lines._ (M)
- **Drop redundant v1 `"files"` key in v2 manifests** — `manifest.py:340-360`, `cli.py:200-202`: `build_manifest_v2` and `_merge_manifests` both write a top-level `"files"` key that no downstream reader consumes; `normalize_manifest`'s v1→v2 promotion is the only real compat shim. (Note: `cli.py:200-202` change also belongs logically here, but touches cli.py — land it as part of Track 3A instead if Group 3 ordering is preserved.) _src/mind_meld/manifest.py, ~20 lines._ (S)
- **`DiffResult` → `@dataclass`** — `manifest.py:402-425`: hand-written `__init__`/`__repr__`, inconsistent with `PullResult`/`PushResult`. Convert to `@dataclass`. _src/mind_meld/manifest.py, ~30 lines._ (S)
- **`diff_manifests` → `diff_files(local_files, remote_files)`** — `manifest.py:428-459`: every call site wraps per-source data as `{"files": src_data["files"]}`; the single-source signature is a v1 holdover. Rename and take dicts directly; migrate call sites in cli.py (but note: the signature change forces a cli.py touch — coordinate with 3A). _src/mind_meld/manifest.py, ~50 lines._ (S)

> **Intra-Group coupling note:** Tasks "Drop redundant v1 `files` key" and `diff_files` rename both touch cli.py at call sites. Land them after 3A merges, or merge 3A+3B into a single track if your workflow prefers one PR.

---

## Group 4: Decomposition + DRY

Break up overgrown functions and extract duplicated logic. Parallel-safe
across cli.py and manifest.py + merge.py.

**Pre-flight** (shared-infra; serial, one-at-a-time):
- Create `src/mind_meld/constants.py` and move `CONFLICT_INFIX`, `CONFLICT_AGE_DAYS`, `TOMBSTONE_TTL_DAYS`, `FORMAT_VERSION`. Add `_manifest_key(device_id)` / `_blob_key(device_id, sha)` (and a parser) in the storage package. Migrate the 6 string-literal construction sites in cli.py (lines 136, 222, 322, 591, 878, 1486) and the parse site. ~100 LOC across cli.py, manifest.py, storage/local.py, new constants.py.

### Track 4A: Decompose overgrown cli.py functions
_2 tasks · ~1.5 days (human) / ~20 min (CC) · medium risk · [cli.py]_
_touches: src/mind_meld/cli.py, tests/test_integration.py_

- **Decompose `_pull_core` (247 lines)** — `cli.py:961-1208`: split into `_select_devices`, `_prefetch_manifests`, `_pull_one_source`, `_print_pull_summary` so the top-level reads as five orchestration calls. Also fix the double `list_devices` call (cli.py:994, 1008) while you're in there, and align `_predict_pull_outcome` return vocabulary with `ApplyOutcome` (cli.py:241-270). _src/mind_meld/cli.py, ~250 lines._ (L)
- **Decompose `_apply_incoming_file` (114 lines)** — `cli.py:447-561`: extract `_apply_write`, `_apply_merge`, `_apply_conflict` helpers; `_apply_incoming_file` dispatches via outcome classification. _src/mind_meld/cli.py, ~150 lines._ (M)

### Track 4B: Walker + manifest + merge DRY
_3 tasks · ~0.5 day (human) / ~12 min (CC) · low risk · [manifest.py, merge.py]_
_touches: src/mind_meld/manifest.py, src/mind_meld/merge.py, tests/test_manifest.py, tests/test_merge.py_

- **Extract `_record_file` helper** — `manifest.py:143-177, 255-285`: 30 lines of per-file "stat → exclude → size-check → hash → record mtime/size/sha" duplicated verbatim between `walk_claude_source` and `walk_generic_source`. _src/mind_meld/manifest.py, ~50 lines._ (S)
- **`_parse_tombstone_ts(iso_str)` helper** — `manifest.py:488-497, 553-563`: `generate_tombstones` and `collect_tombstones` both parse `deleted_at` with the same fromisoformat-add-utc-compare dance. _src/mind_meld/manifest.py, ~30 lines._ (S)
- **`merge.py` dispatch + join helpers** — `merge.py:16-35, 64, 80`: `should_merge`/`merge_file` duplicate strategy classification; `merge_jsonl`/`merge_lines` share an identical join-lines tail. Introduce `_merge_strategy(rel_path)` dispatch and `_join_lines(lines)` helper. _src/mind_meld/merge.py, tests/test_merge.py, ~40 lines._ (S)

---

## Group 5: Init flow + sync_log generalization

Multi-source assumption lag — `init` still hardcodes `~/.claude` and
`write_sync_log` is claude-specific. Resolving this makes mm genuinely
multi-source in day-to-day usage.

### Track 5A: Init decomposition + DEFAULT_SOURCES reuse + sync_log generalization
_5 tasks · ~1.5 days (human) / ~25 min (CC) · medium risk · [cli.py, config.py, synclog.py]_
_touches: src/mind_meld/cli.py, src/mind_meld/config.py, src/mind_meld/synclog.py, tests/test_integration.py, tests/test_synclog.py_

- **Decompose `init` (85 lines)** — `cli.py:645-730`: extract `_prompt_passphrase()`, `_maybe_add_gstack_source(config)`, `_save_and_register(config)` helpers. _src/mind_meld/cli.py, ~120 lines._ (M)
- **Reuse `DEFAULT_SOURCES` in init** — `cli.py:663-668, 704-720`: `init()` hardcodes `"~/.claude"`, `52_428_800`, `65_536`, and re-inlines the full gstack source dict already in `config.DEFAULT_SOURCES`. Import and reuse `DEFAULT_CLAUDE_DIR` / `DEFAULT_MAX_FILE_SIZE` / `DEFAULT_ARGON2_MEMORY_KB`, and pick the gstack entry out of `DEFAULT_SOURCES`. _src/mind_meld/cli.py, src/mind_meld/config.py, ~40 lines._ (S)
- **Init refuses to finish without a source path** — `cli.py:696-723`: current flow writes `sync.claude_dir = "~/.claude"` unconditionally; a pure-gstack user ends with `get_sources() == []` and push says "No sync sources found". Prompt for which sources to enable with auto-detect defaults; refuse to finish init if no source path exists on disk. _src/mind_meld/cli.py, tests/test_integration.py, ~60 lines._ (M)
- **`write_sync_log` keyed off source type** — `cli.py:1147-1158`: called only when `src_name == "claude"`. A user renaming the claude source in config (spec doesn't forbid it) breaks sync-log entirely. Key off `src_cfg["type"] == "claude"` instead. _src/mind_meld/cli.py, ~15 lines._ (S)
- **`synclog.py` param rename `claude_dir` → `base_path`** — `synclog.py:16, 34-36`: naming lags the multi-source rename; `projects/` layout stays claude-specific but is now explicitly scoped. Rename the parameter, document the claude-only semantic, scope the caller. _src/mind_meld/synclog.py, tests/test_synclog.py, ~30 lines._ (S)

---

## Group 6: Test hygiene + style polish

Parallel-safe across tests and cli.py (style nits only).

### Track 6A: Test improvements
_3 tasks · ~0.5 day (human) / ~10 min (CC) · low risk · [tests/]_
_touches: tests/test_integration.py, tests/test_conflict_copy.py_

- **Migrate `TestPushPullRoundTrip` to CLI invocation** — `tests/test_integration.py:53-163`: bypasses the CLI entirely; uses `build_manifest`/`encrypt`/`storage.put` by hand. Does not exercise `_pull_core` or `_apply_incoming_file`. Replace with `runner.invoke(app, ["push"|"pull"])` like `TestMultiSourceSync`. Once migrated, the `build_manifest` import drops too. Add a CLI-driven end-to-end test covering push→pull→conflict→tombstone propagation together. _tests/test_integration.py, ~100 lines._ (M)
- **Rename misleading test** — `tests/test_integration.py:103-163`: `test_deletion_propagation` is named after pre-additive behavior but the body asserts the opposite. Rename to `test_deletion_not_propagated_in_additive_model`. _tests/test_integration.py, ~5 lines._ (S)
- **Hoist lazy imports** — `tests/test_conflict_copy.py` (~11 sites) and `tests/test_integration.py` (~7 sites): in-function imports of `mind_meld.cli` helpers and `pathlib.Path`/`shutil` redeclared despite module-level blocks. Hoist to the top. _tests/test_conflict_copy.py, tests/test_integration.py, ~30 lines._ (S)

### Track 6B: Style nits in cli.py
_3 tasks · ~0.5 day (human) / ~10 min (CC) · low risk · [cli.py]_
_touches: src/mind_meld/cli.py_

- **Type hints on helper `backend` params** — `cli.py:108, 116, 124, 241, 273, 298, 332, 360, 381, 447, 564, 624, 766, 961`: helpers accept untyped `backend`, while `devices.py:13, 30, 43` types it as `LocalBackend`. Add `backend: LocalBackend` hints matching devices.py. _src/mind_meld/cli.py, ~20 lines._ (S)
- **Standardize optional syntax** — `cli.py`: 6 `Optional[X]` and 11 `X | None` despite `from __future__ import annotations`. Standardize on `X | None` and drop `Optional` from the typing import. _src/mind_meld/cli.py, ~15 lines._ (S)
- **Drop placeholderless f-strings + keyring bare-except narrow** — `cli.py:897, 1246, 1377, 1502, 1864`: literal strings marked `f"..."` with no interpolation. Also narrow `crypto.py:97, 104, 120, 147` keyring bare-except to `keyring.errors.KeyringError` + `ImportError`. _src/mind_meld/cli.py, src/mind_meld/crypto.py, ~20 lines._ (S)

---

## Group 7: Selective sync (P2)

Per-project filtering so users with dozens of Claude projects can sync just
the 2-3 they actively use across machines. Pre-existing P2 item; deferred
until post-cleanup so walker code is stable.

### Track 7A: `sync.include` / `sync.exclude` config
_3 tasks · ~1.5 days (human) / ~20 min (CC) · medium risk · [config.py, manifest.py]_
_touches: src/mind_meld/config.py, src/mind_meld/manifest.py, tests/test_config.py, tests/test_manifest.py_

- **Schema + validation for include/exclude globs** — add `sync.include` / `sync.exclude` arrays to config.toml schema; validate glob patterns; document precedence (exclude wins over include). _src/mind_meld/config.py, tests/test_config.py, ~80 lines._ (M)
- **Walker applies filters** — integrate the filters into `walk_claude_source` / `walk_generic_source` so filtered projects never get hashed. _src/mind_meld/manifest.py, tests/test_manifest.py, ~60 lines._ (S)
- **CLI flag surface** — expose as `--include` / `--exclude` runtime overrides on push/pull; document in README. _src/mind_meld/cli.py, README.md, ~40 lines._ (S)

---

## Group 8: Mtime hash cache (P3)
_Depends on: Group 6 (Test hygiene + style polish)_

Push-side perf: skip re-hashing files whose mtime hasn't changed since the
last push. Parallel to Group 7 — both depend on Group 6 but touch different
code paths.

### Track 8A: Local mtime→hash cache
_3 tasks · ~1.5 days (human) / ~25 min (CC) · medium risk · [manifest.py, new local state]_
_touches: src/mind_meld/manifest.py, src/mind_meld/cache.py, tests/test_manifest.py_

- **`~/.config/mind_meld/local-manifest.json` cache** — design + write a per-device local cache: path → (mtime, size, sha). Fallback semantics: if mtime is unreliable (network drives, clock drift), invalidate the cache entry. _src/mind_meld/cache.py, ~100 lines._ (M)
- **Walker reads/writes the cache** — on hash, check cache first; on hash-miss or mtime-change, re-hash and update. _src/mind_meld/manifest.py, tests/test_manifest.py, ~80 lines._ (M)
- **`mm gc --local-cache`** — GC reaper for stale cache entries (paths that no longer exist on disk). _src/mind_meld/cli.py, ~30 lines._ (S)

---

## Group 9: Three-way merge base (P3)
_Depends on: Group 6 (Test hygiene + style polish)_

Pull-side correctness upgrade: track "last-synced hash" per file to
distinguish "remote changed, I didn't" from "we both changed." Revisit item
per v0.4.0 context note. Parallel to Groups 7-8.

### Track 9A: Stored last-synced hash
_3 tasks · ~2 days (human) / ~30 min (CC) · high risk · [cli.py, new state file]_
_touches: src/mind_meld/cli.py, src/mind_meld/sync_state.py, tests/test_conflict_copy.py_

- **`~/.config/mind_meld/sync-state.json`** — per-source, per-file last-synced hashes. Schema, persistence, corruption recovery. _src/mind_meld/sync_state.py, ~100 lines._ (M)
- **Three-way conflict detection** — on pull, compare local hash, remote hash, and last-synced base. Fast-forward when only one side changed; conflict only when both diverged from base. _src/mind_meld/cli.py, tests/test_conflict_copy.py, ~150 lines._ (M)
- **Migration path** — first-run bootstraps the base from current local hashes; document how the upgrade interacts with existing `.sync-conflict-*` files. _src/mind_meld/cli.py, docs/designs/three-way-merge.md, ~60 lines._ (M)

---

## Execution Map

Adjacency list (who depends on whom):

```
- Group 1 ← {}
- Group 2 ← {1}
- Group 3 ← {2}
- Group 4 ← {3}
- Group 5 ← {4}
- Group 6 ← {5}
- Group 7 ← {6}
- Group 8 ← {6}   (parallel to 7)
- Group 9 ← {6}   (parallel to 7, 8)
```

Track detail per group:

```
Group 1: Error discipline
  ├── Track 1A ............ ~1 day ... 5 tasks ... silent failures (cli.py)
  └── Track 1B ............ ~0.5d .... 3 tasks ... config eager validation

Group 2: Post-v0.5.1 follow-ups
  Pre-flight ............... ~1 hr (3 cli.py-only additions)
  └── Track 2A ............ ~0.5d .... 4 tasks ... error-surface follow-ups

Group 3: cli.py hardening + dead code in manifest
  ├── Track 3A ............ ~1 day ... 5 tasks ... cli.py surgical hardening
  └── Track 3B ............ ~1 day ... 4 tasks ... manifest dead code

Group 4: Decomposition + DRY
  Pre-flight ............... ~2 hr (constants + storage key helpers)
  ├── Track 4A ............ ~1.5d .... 2 tasks ... decompose _pull_core + _apply_incoming_file
  └── Track 4B ............ ~0.5d .... 3 tasks ... walker + manifest + merge DRY

Group 5: Init flow + sync_log generalization
  └── Track 5A ............ ~1.5d .... 5 tasks ... init + sync_log

Group 6: Test hygiene + style polish
  ├── Track 6A ............ ~0.5d .... 3 tasks ... tests (CLI-driven, rename, hoist)
  └── Track 6B ............ ~0.5d .... 3 tasks ... style nits (cli.py)

Group 7: Selective sync (P2)
  └── Track 7A ............ ~1.5d .... 3 tasks ... sync.include/exclude

Group 8: Mtime hash cache (P3)
  └── Track 8A ............ ~1.5d .... 3 tasks ... local mtime→hash cache

Group 9: Three-way merge base (P3)
  └── Track 9A ............ ~2 days .. 3 tasks ... stored last-synced hash
```

**Total: 9 groups · 13 tracks · 40 tasks** (+ 4 pre-flight items)

---

## Future (Phase 2+)

- **`mm rekey` passphrase rotation** — Format v2 makes `master_key` the rotation boundary but v2 blobs don't carry a `key_scheme` byte (dropped per /plan-ceo-review simplification). Rotation requires format v3: either re-wrap `master_key` under the new passphrase, or re-encrypt every blob under a freshly-derived `master_key`. Completes the crypto story but requires a format bump and migration path; explicitly deferred at plan-ceo-review on 2026-04-22. _src/mind_meld/crypto.py, src/mind_meld/cli.py, SPEC.md, ~200-400 lines._ (M-L) _Deferred because: post-1.0 P3 — requires format v3 and a migration dance; no users blocked pre-1.0._

---

## Unprocessed

Items awaiting triage by /roadmap. Added by other skills or manually.
