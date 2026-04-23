# Roadmap — Phase 1 (v0.x → v1.0 cleanup sweep)

Organized as **Groups > Tracks > Tasks**. A Group is a wave of PRs that land
together — parallel-safe within, dependency-ordered between. By default each
Group depends on the immediately preceding Group (single linear chain); Groups
5/6/7 are explicitly parallel-safe after Group 4, and Group 8 (release infra)
is parallel-safe with everything. Within a Group, Tracks must be fully
parallel-safe (set-disjoint `_touches:_` footprints). Each track is one plan +
implement session.

Items originate from the 2026-04-22 `/full-review` audit plus review follow-ups
accumulated across v0.5.1/v0.6.x/v0.7.x/v0.8.0 (notably /plan-eng-review on
2026-04-23 and /land-and-deploy on 2026-04-23). Every item targets the v1.0
release.

**Correctness foundation + Error discipline + Post-v0.5.1 follow-ups shipped
through v0.8.0.** Tri-state `ManifestFetch` recovery chain (v0.5.1), crypto v2
master_key + HKDF (v0.6.0), storage layer hardening via `fsutil` + `fcntl.flock`
(v0.6.1), walker conflict-file exclusion + `load_manifest` read-path boundary
(v0.6.2), Track 1A silent-failure cleanup + `--conflict-mode` unification
(v0.7.0), config eager validation + legacy cleanup (v0.7.1), and Group 2
pre-flight (`_merge_manifests` tiebreak, `mm diag`, `mm init` two-tier guard) +
Track 2A (`mm recover --abandon-manifest`, `_error()` stderr routing,
`list_devices` shape validation) (v0.8.0). See `docs/PROGRESS.md` for the full
version history.

---

## Group 1: cli.py hardening + dead code in manifest

Surgical CLI hardening (atomic renames, collision-resistant filenames, robust
GC) alongside manifest dead-code removal. Parallel-safe across files.

### Track 1A: cli.py surgical hardening + `_delete_files` removal
_6 tasks · ~1 day (human) / ~20 min (CC) · medium risk · [cli.py]_
_touches: src/mind_meld/cli.py, src/mind_meld/crypto.py, tests/test_conflict_copy.py, tests/test_storage_local.py, tests/test_track_1a.py_

- **`resolve --force` atomic rename** — `cli.py:1769-1775`: bare `Path.rename` fails across filesystems and has no tmp-then-rename safety. Use `shutil.move` or read-write-rename via `_atomic_write`; surface rename failure as a non-zero exit code. _src/mind_meld/cli.py, tests/test_conflict_copy.py, ~40 lines._ (S)
- **16-char device_id in conflict filenames** — `cli.py:54, 541`: `device_id[:8]` truncation risks collision between two machines in same-second conflict-of-a-conflict scenarios. Widen to 16-char prefix (or full device_id). _src/mind_meld/cli.py, ~15 lines._ (S)
- **GC logs malformed blob paths** — `cli.py:1482-1497`: `_do_gc` silently ignores blobs at unexpected depth; orphaned `.tmp` artifacts from crashed pushes accumulate forever. Log (and optionally reap) `data/**` files that don't match the expected `data/{device}/{sha}.enc` pattern; add a test. _src/mind_meld/cli.py, tests/test_storage_local.py, ~30 lines._ (S)
- **Remove `_delete_files` dead code** — `cli.py:624-638`: never called after the additive-only refactor (v0.3.0); a future maintainer will re-wire delete-on-pull behavior that the spec forbids. Delete the function. _src/mind_meld/cli.py, ~15 lines._ (S)
- **Drop unused `TOMBSTONE_TTL_DAYS` import** — `cli.py:35`: imported but never referenced. _src/mind_meld/cli.py, ~2 lines._ (S)
- **Full quiet-path audit in cli.py** — classify every `if not quiet:` gate as "verbose-only" vs "load-bearing signal." v0.7.0's silent-failure cleanup patched two known load-bearing gates (`_pull_core:1445` corrupt peer manifest, `_push_core:1297` sidecar write failure); the pattern is likely wider. Codex flagged during /plan-eng-review 2026-04-23. _src/mind_meld/cli.py, tests/test_track_1a.py, ~60 lines._ (S)

### Track 1B: Manifest dead code + v1-holdover cleanup
_4 tasks · ~1 day (human) / ~15 min (CC) · low risk · [manifest.py, manifest tests]_
_touches: src/mind_meld/manifest.py, tests/test_manifest.py, tests/test_additive_sync.py, tests/test_integration.py_

- **Delete `walk_directory` and `build_manifest`** — `manifest.py:182-209`: backward-compat aliases with no production callers; `test_manifest.py`/`test_integration.py`/`test_additive_sync.py` are the only users. Delete both and migrate the three test files to `walk_claude_source` / `build_manifest_v2`. _src/mind_meld/manifest.py, tests/test_manifest.py, tests/test_additive_sync.py, tests/test_integration.py, ~60 lines._ (M)
- **Drop redundant v1 `"files"` key in v2 manifests** — `manifest.py:340-360`, `cli.py:200-202`: `build_manifest_v2` and `_merge_manifests` both write a top-level `"files"` key that no downstream reader consumes; `normalize_manifest`'s v1→v2 promotion is the only real compat shim. (Note: `cli.py:200-202` change also belongs logically here, but touches cli.py — land it as part of Track 1A instead if Group 1 ordering is preserved.) _src/mind_meld/manifest.py, ~20 lines._ (S)
- **`DiffResult` → `@dataclass`** — `manifest.py:402-425`: hand-written `__init__`/`__repr__`, inconsistent with `PullResult`/`PushResult`. Convert to `@dataclass`. _src/mind_meld/manifest.py, ~30 lines._ (S)
- **`diff_manifests` → `diff_files(local_files, remote_files)`** — `manifest.py:428-459`: every call site wraps per-source data as `{"files": src_data["files"]}`; the single-source signature is a v1 holdover. Rename and take dicts directly; migrate call sites in cli.py (but note: the signature change forces a cli.py touch — coordinate with 1A). _src/mind_meld/manifest.py, ~50 lines._ (S)

> **Intra-Group coupling note:** Tasks "Drop redundant v1 `files` key" and `diff_files` rename both touch cli.py at call sites. Land them after 1A merges, or merge 1A+1B into a single track if your workflow prefers one PR.

---

## Group 2: Decomposition + DRY

Break up overgrown functions and extract duplicated logic. Parallel-safe
across cli.py and manifest.py + merge.py.

**Pre-flight** (shared-infra; serial, one-at-a-time):
- Create `src/mind_meld/constants.py` and move `CONFLICT_INFIX`, `CONFLICT_AGE_DAYS`, `TOMBSTONE_TTL_DAYS`, `FORMAT_VERSION`. Add `_manifest_key(device_id)` / `_blob_key(device_id, sha)` (and a parser) in the storage package. Migrate the 6 string-literal construction sites in cli.py (lines 136, 222, 322, 591, 878, 1486) and the parse site. ~100 LOC across cli.py, manifest.py, storage/local.py, new constants.py.

### Track 2A: Decompose overgrown cli.py functions
_2 tasks · ~1.5 days (human) / ~20 min (CC) · medium risk · [cli.py]_
_touches: src/mind_meld/cli.py, tests/test_integration.py_

- **Decompose `_pull_core` (247 lines)** — `cli.py:961-1208`: split into `_select_devices`, `_prefetch_manifests`, `_pull_one_source`, `_print_pull_summary` so the top-level reads as five orchestration calls. Also fix the double `list_devices` call (cli.py:994, 1008) while you're in there, and align `_predict_pull_outcome` return vocabulary with `ApplyOutcome` (cli.py:241-270). _src/mind_meld/cli.py, ~250 lines._ (L)
- **Decompose `_apply_incoming_file` (114 lines)** — `cli.py:447-561`: extract `_apply_write`, `_apply_merge`, `_apply_conflict` helpers; `_apply_incoming_file` dispatches via outcome classification. _src/mind_meld/cli.py, ~150 lines._ (M)

### Track 2B: Walker + manifest + merge DRY
_3 tasks · ~0.5 day (human) / ~12 min (CC) · low risk · [manifest.py, merge.py]_
_touches: src/mind_meld/manifest.py, src/mind_meld/merge.py, tests/test_manifest.py, tests/test_merge.py_

- **Extract `_record_file` helper** — `manifest.py:143-177, 255-285`: 30 lines of per-file "stat → exclude → size-check → hash → record mtime/size/sha" duplicated verbatim between `walk_claude_source` and `walk_generic_source`. _src/mind_meld/manifest.py, ~50 lines._ (S)
- **`_parse_tombstone_ts(iso_str)` helper** — `manifest.py:488-497, 553-563`: `generate_tombstones` and `collect_tombstones` both parse `deleted_at` with the same fromisoformat-add-utc-compare dance. _src/mind_meld/manifest.py, ~30 lines._ (S)
- **`merge.py` dispatch + join helpers** — `merge.py:16-35, 64, 80`: `should_merge`/`merge_file` duplicate strategy classification; `merge_jsonl`/`merge_lines` share an identical join-lines tail. Introduce `_merge_strategy(rel_path)` dispatch and `_join_lines(lines)` helper. _src/mind_meld/merge.py, tests/test_merge.py, ~40 lines._ (S)

---

## Group 3: Init flow + sync_log generalization + config polish

Multi-source assumption lag — `init` still hardcodes `~/.claude` and
`write_sync_log` is claude-specific. Resolving this makes mm genuinely
multi-source in day-to-day usage. Track 3B picks up the two config-polish
follow-ups from /plan-eng-review on 2026-04-23.

### Track 3A: Init decomposition + DEFAULT_SOURCES reuse + sync_log generalization
_5 tasks · ~1.5 days (human) / ~25 min (CC) · medium risk · [cli.py, config.py, synclog.py]_
_touches: src/mind_meld/cli.py, src/mind_meld/config.py, src/mind_meld/synclog.py, tests/test_integration.py, tests/test_synclog.py_

- **Decompose `init` (85 lines)** — `cli.py:645-730`: extract `_prompt_passphrase()`, `_maybe_add_gstack_source(config)`, `_save_and_register(config)` helpers. _src/mind_meld/cli.py, ~120 lines._ (M)
- **Reuse `DEFAULT_SOURCES` in init** — `cli.py:663-668, 704-720`: `init()` hardcodes `"~/.claude"`, `52_428_800`, `65_536`, and re-inlines the full gstack source dict already in `config.DEFAULT_SOURCES`. Import and reuse `DEFAULT_CLAUDE_DIR` / `DEFAULT_MAX_FILE_SIZE` / `DEFAULT_ARGON2_MEMORY_KB`, and pick the gstack entry out of `DEFAULT_SOURCES`. _src/mind_meld/cli.py, src/mind_meld/config.py, ~40 lines._ (S)
- **Init refuses to finish without a source path** — `cli.py:696-723`: current flow writes `sync.claude_dir = "~/.claude"` unconditionally; a pure-gstack user ends with `get_sources() == []` and push says "No sync sources found". Prompt for which sources to enable with auto-detect defaults; refuse to finish init if no source path exists on disk. _src/mind_meld/cli.py, tests/test_integration.py, ~60 lines._ (M)
- **`write_sync_log` keyed off source type** — `cli.py:1147-1158`: called only when `src_name == "claude"`. A user renaming the claude source in config (spec doesn't forbid it) breaks sync-log entirely. Key off `src_cfg["type"] == "claude"` instead. _src/mind_meld/cli.py, ~15 lines._ (S)
- **`synclog.py` param rename `claude_dir` → `base_path`** — `synclog.py:16, 34-36`: naming lags the multi-source rename; `projects/` layout stays claude-specific but is now explicitly scoped. Rename the parameter, document the claude-only semantic, scope the caller. _src/mind_meld/synclog.py, tests/test_synclog.py, ~30 lines._ (S)

### Track 3B: Config polish — eng-review follow-ups
_2 tasks · ~0.5 day (human) / ~12 min (CC) · low risk · [config.py, cli.py]_
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, tests/test_config.py_
_Depends on: Track 3A landing first so the `cli.py:1076` config-read pattern is consolidated. Otherwise the refactor fights with in-flight init work._

- **Stop mutating config in `_apply_defaults`; compute expanded paths lazily in `get_sources`** — rework so `load → save` round-trip preserves human-readable forms (e.g. `~/.claude` stays `~/.claude`). Expansion + `.resolve()` happen at use sites only. Backfill save at `cli.py:227-233` silently rewrites user's TOML from `~/.claude` to the canonical absolute path on first-run-after-upgrade — Codex flagged this during /plan-eng-review 2026-04-23 as a UX footgun. v0.7.1's `.resolve()` addition extends the footgun to symlink dereference; the proper fix is to not mutate config at load time at all. Requires updating all readers of `config["sync"]["claude_dir"]` to re-expand at use site. _src/mind_meld/config.py, src/mind_meld/cli.py, ~60 lines._ (M)
- **Rich `ConfigError` with TOML line numbers on parse failure** — when `tomllib.load()` raises `TOMLDecodeError` (config.py:68-69), extract the line number (`.line` / `.column`) and include it in the `ConfigError` message. Current "config: failed to parse /path — <raw msg>" is serviceable but not pinpoint; a hand-edited `sync.sources` block with a syntax error should tell the user exactly which line. Low payoff (most configs are Claude Code-driven, not hand-edited) but cheap. _src/mind_meld/config.py, tests/test_config.py, ~15 lines._ (S)

---

## Group 4: Test hygiene + style polish

Parallel-safe across tests and cli.py (style nits only).

### Track 4A: Test improvements
_3 tasks · ~0.5 day (human) / ~10 min (CC) · low risk · [tests/]_
_touches: tests/test_integration.py, tests/test_conflict_copy.py_

- **Migrate `TestPushPullRoundTrip` to CLI invocation** — `tests/test_integration.py:53-163`: bypasses the CLI entirely; uses `build_manifest`/`encrypt`/`storage.put` by hand. Does not exercise `_pull_core` or `_apply_incoming_file`. Replace with `runner.invoke(app, ["push"|"pull"])` like `TestMultiSourceSync`. Once migrated, the `build_manifest` import drops too. Add a CLI-driven end-to-end test covering push→pull→conflict→tombstone propagation together. _tests/test_integration.py, ~100 lines._ (M)
- **Rename misleading test** — `tests/test_integration.py:103-163`: `test_deletion_propagation` is named after pre-additive behavior but the body asserts the opposite. Rename to `test_deletion_not_propagated_in_additive_model`. _tests/test_integration.py, ~5 lines._ (S)
- **Hoist lazy imports** — `tests/test_conflict_copy.py` (~11 sites) and `tests/test_integration.py` (~7 sites): in-function imports of `mind_meld.cli` helpers and `pathlib.Path`/`shutil` redeclared despite module-level blocks. Hoist to the top. _tests/test_conflict_copy.py, tests/test_integration.py, ~30 lines._ (S)

### Track 4B: Style nits in cli.py
_3 tasks · ~0.5 day (human) / ~10 min (CC) · low risk · [cli.py]_
_touches: src/mind_meld/cli.py_

- **Type hints on helper `backend` params** — `cli.py:108, 116, 124, 241, 273, 298, 332, 360, 381, 447, 564, 624, 766, 961`: helpers accept untyped `backend`, while `devices.py:13, 30, 43` types it as `LocalBackend`. Add `backend: LocalBackend` hints matching devices.py. _src/mind_meld/cli.py, ~20 lines._ (S)
- **Standardize optional syntax** — `cli.py`: 6 `Optional[X]` and 11 `X | None` despite `from __future__ import annotations`. Standardize on `X | None` and drop `Optional` from the typing import. _src/mind_meld/cli.py, ~15 lines._ (S)
- **Drop placeholderless f-strings + keyring bare-except narrow** — `cli.py:897, 1246, 1377, 1502, 1864`: literal strings marked `f"..."` with no interpolation. Also narrow `crypto.py:97, 104, 120, 147` keyring bare-except to `keyring.errors.KeyringError` + `ImportError`. _src/mind_meld/cli.py, src/mind_meld/crypto.py, ~20 lines._ (S)

---

## Group 5: Selective sync (P2)

Per-project filtering so users with dozens of Claude projects can sync just
the 2-3 they actively use across machines. Pre-existing P2 item; deferred
until post-cleanup so walker code is stable.

### Track 5A: `sync.include` / `sync.exclude` config
_3 tasks · ~1.5 days (human) / ~20 min (CC) · medium risk · [config.py, manifest.py]_
_touches: src/mind_meld/config.py, src/mind_meld/manifest.py, tests/test_config.py, tests/test_manifest.py_

- **Schema + validation for include/exclude globs** — add `sync.include` / `sync.exclude` arrays to config.toml schema; validate glob patterns; document precedence (exclude wins over include). _src/mind_meld/config.py, tests/test_config.py, ~80 lines._ (M)
- **Walker applies filters** — integrate the filters into `walk_claude_source` / `walk_generic_source` so filtered projects never get hashed. _src/mind_meld/manifest.py, tests/test_manifest.py, ~60 lines._ (S)
- **CLI flag surface** — expose as `--include` / `--exclude` runtime overrides on push/pull; document in README. _src/mind_meld/cli.py, README.md, ~40 lines._ (S)

---

## Group 6: Mtime hash cache (P3)
_Depends on: Group 4 (Test hygiene + style polish)_

Push-side perf: skip re-hashing files whose mtime hasn't changed since the
last push. Parallel to Group 5 — both depend on Group 4 but touch different
code paths.

### Track 6A: Local mtime→hash cache
_3 tasks · ~1.5 days (human) / ~25 min (CC) · medium risk · [manifest.py, new local state]_
_touches: src/mind_meld/manifest.py, src/mind_meld/cache.py, tests/test_manifest.py_

- **`~/.config/mind_meld/local-manifest.json` cache** — design + write a per-device local cache: path → (mtime, size, sha). Fallback semantics: if mtime is unreliable (network drives, clock drift), invalidate the cache entry. _src/mind_meld/cache.py, ~100 lines._ (M)
- **Walker reads/writes the cache** — on hash, check cache first; on hash-miss or mtime-change, re-hash and update. _src/mind_meld/manifest.py, tests/test_manifest.py, ~80 lines._ (M)
- **`mm gc --local-cache`** — GC reaper for stale cache entries (paths that no longer exist on disk). _src/mind_meld/cli.py, ~30 lines._ (S)

---

## Group 7: Three-way merge base (P3)
_Depends on: Group 4 (Test hygiene + style polish)_

Pull-side correctness upgrade: track "last-synced hash" per file to
distinguish "remote changed, I didn't" from "we both changed." Revisit item
per v0.4.0 context note. Parallel to Groups 5-6.

### Track 7A: Stored last-synced hash
_3 tasks · ~2 days (human) / ~30 min (CC) · high risk · [cli.py, new state file]_
_touches: src/mind_meld/cli.py, src/mind_meld/sync_state.py, tests/test_conflict_copy.py_

- **`~/.config/mind_meld/sync-state.json`** — per-source, per-file last-synced hashes. Schema, persistence, corruption recovery. _src/mind_meld/sync_state.py, ~100 lines._ (M)
- **Three-way conflict detection** — on pull, compare local hash, remote hash, and last-synced base. Fast-forward when only one side changed; conflict only when both diverged from base. _src/mind_meld/cli.py, tests/test_conflict_copy.py, ~150 lines._ (M)
- **Migration path** — first-run bootstraps the base from current local hashes; document how the upgrade interacts with existing `.sync-conflict-*` files. _src/mind_meld/cli.py, docs/designs/three-way-merge.md, ~60 lines._ (M)

---

## Group 8: Release infrastructure

CI plumbing that's been ad-hoc to date. The project currently has no CI on
main — every push trusts the human (or Claude session) to have run `pytest`
locally. For a tool whose whole job is "never silently eat user deletions
across machines," the absence of CI on main is a real risk surface.
Parallel-safe with Groups 5/6/7 (P2/P3 features); no dependency on Group 4.

### Track 8A: GitHub Actions CI workflow
_1 task · ~1-2 hours (human) / ~10 min (CC) · low risk · [.github/workflows/]_
_touches: .github/workflows/test.yml (new)_

- **Add `.github/workflows/test.yml`** — runs `pytest tests/` on every push to main and every PR. Matrix across the Python versions in `pyproject.toml` classifiers (3.11, 3.12). First CI run will surface latent flakes hiding in the local-only workflow; macOS keyring tests need stubbing or skipping on Linux runners via the existing `MINDMELD_PASSPHRASE` env-var path (`test_crypto.py`, `test_integration.py` reach into keyring indirectly via `store_passphrase_in_keyring`). _.github/workflows/test.yml, ~30 lines._ (S)

---

## Execution Map

Adjacency list (who depends on whom):

```
- Group 1 ← {}
- Group 2 ← {1}
- Group 3 ← {2}
- Group 4 ← {3}
- Group 5 ← {4}
- Group 6 ← {4}   (parallel to 5)
- Group 7 ← {4}   (parallel to 5, 6)
- Group 8 ← {}    (parallel to all — release infra)
```

Track detail per group:

```
Group 1: cli.py hardening + dead code in manifest
  ├── Track 1A ............ ~1 day ... 6 tasks ... cli.py surgical hardening
  └── Track 1B ............ ~1 day ... 4 tasks ... manifest dead code

Group 2: Decomposition + DRY
  Pre-flight ............... ~2 hr (constants + storage key helpers)
  ├── Track 2A ............ ~1.5d .... 2 tasks ... decompose _pull_core + _apply_incoming_file
  └── Track 2B ............ ~0.5d .... 3 tasks ... walker + manifest + merge DRY

Group 3: Init flow + sync_log generalization + config polish
  ├── Track 3A ............ ~1.5d .... 5 tasks ... init + sync_log
  └── Track 3B ............ ~0.5d .... 2 tasks ... config polish (eng-review follow-ups)

Group 4: Test hygiene + style polish
  ├── Track 4A ............ ~0.5d .... 3 tasks ... tests (CLI-driven, rename, hoist)
  └── Track 4B ............ ~0.5d .... 3 tasks ... style nits (cli.py)

Group 5: Selective sync (P2)
  └── Track 5A ............ ~1.5d .... 3 tasks ... sync.include/exclude

Group 6: Mtime hash cache (P3)
  └── Track 6A ............ ~1.5d .... 3 tasks ... local mtime→hash cache

Group 7: Three-way merge base (P3)
  └── Track 7A ............ ~2 days .. 3 tasks ... stored last-synced hash

Group 8: Release infrastructure
  └── Track 8A ............ ~1-2 hr .. 1 task .... GitHub Actions CI
```

**Total: 8 groups · 12 tracks · 38 tasks (+ 1 pre-flight item)**

---

## Future (Phase 2+)

- **`mm rekey` passphrase rotation** — Format v2 makes `master_key` the rotation boundary but v2 blobs don't carry a `key_scheme` byte (dropped per /plan-ceo-review simplification). Rotation requires format v3: either re-wrap `master_key` under the new passphrase, or re-encrypt every blob under a freshly-derived `master_key`. Completes the crypto story but requires a format bump and migration path; explicitly deferred at plan-ceo-review on 2026-04-22. _src/mind_meld/crypto.py, src/mind_meld/cli.py, SPEC.md, ~200-400 lines._ (M-L) _Deferred because: post-1.0 P3 — requires format v3 and a migration dance; no users blocked pre-1.0._

- **Blob-directory as secondary peer-discovery in corrupt-manifest recovery** — in `_collect_peer_tombstones` (or a sibling helper), when a peer's `devices/<id>.json` is corrupt or missing but `data/<id>/` has blobs and `manifests/<id>/*.enc` decrypts, promote the blob-dir-derived `device_id` to the peer list. Recovers tombstones from the otherwise-dropped peer. Codex flagged during /plan-eng-review 2026-04-23 that `list_devices()` silently dropping a peer masks a recoverable manifest. Widens the trust surface — blob-presence becomes load-bearing evidence of a peer's existence, not just a device-registry entry. _src/mind_meld/cli.py, ~30 lines._ (S) _Deferred because: observation-bar — land when the first real support case appears where corrupt `devices.json` masks a recoverable manifest. v0.8.0's `list_devices` shape-validation + warning is enough until then._

- **PyPI publish workflow** — `.github/workflows/release.yml` that builds + publishes to PyPI on git tag push (e.g. `v0.8.0` → trigger). Uses `hatchling` build backend (already configured in `pyproject.toml`). Currently users install via `pip install -e .` from a local clone; PyPI distribution would let someone `pip install mind-meld` cleanly. Commits to a public package namespace (name squatting, can't easily rename); need to decide on trusted-publisher vs API token auth. Depends on Group 8 / Track 8A landing first (tests green before publishing anything). _.github/workflows/release.yml, ~50 lines._ (S) _Deferred because: observation-bar — land when "how do I install this" becomes friction. No user demand signal today._

---

## Unprocessed

Items awaiting triage by /roadmap. Added by other skills or manually.
