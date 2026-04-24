# Roadmap — Phase 1 (v0.x → v1.0 cleanup sweep)

Organized as **Groups > Tracks > Tasks**. Groups are sequential — complete
Group 1 before starting Group 2. Tracks within a group run in parallel
(file-ownership-disjoint). Pre-flight items are trivial fixes any agent can
pick up.

Items originate from the 2026-04-22 `/full-review` audit, review follow-ups
accumulated across v0.5.1/v0.6.x/v0.7.x/v0.8.x, and /plan-eng-review on
2026-04-23. Every item targets the v1.0 release.

**Correctness foundation + Error discipline + Post-v0.5.1 follow-ups + cli.py
hardening + manifest dead code all shipped through v0.8.2.** Tri-state
`ManifestFetch` recovery chain (v0.5.1), crypto v2 master_key + HKDF (v0.6.0),
storage layer hardening (v0.6.1), walker conflict-file exclusion + read-path
boundary (v0.6.2), silent-failure cleanup + `--conflict-mode` unification
(v0.7.0), config eager validation (v0.7.1), error-surface hardening + `mm diag`
+ `mm recover --abandon-manifest` (v0.8.0), cli.py surgical hardening (v0.8.1),
manifest dead code + v1 holdovers (v0.8.2). See `docs/PROGRESS.md` for the
full version history.

---

## Group 1: Decomposition + DRY

Break up overgrown functions and extract duplicated logic. Parallel-safe
across cli.py (Track 1A) and manifest.py + merge.py (Track 1B). Track 1C is
serial after 1A (cli.py follow-ups).

**Track 1A ✅ shipped in v0.8.4** (decomposition + storage-key portion of
pre-flight). Pre-flight's `constants.py` extraction + the cli.py literal-site
migration is still open.

**Pre-flight** (shared-infra; serial, one-at-a-time):
- Create `src/mind_meld/constants.py` and move `CONFLICT_INFIX`, `CONFLICT_AGE_DAYS`, `TOMBSTONE_TTL_DAYS`, `FORMAT_VERSION`. Add `_manifest_key(device_id)` / `_blob_key(device_id, sha)` (and a parser) in the storage package. Migrate the 6 string-literal construction sites in cli.py (lines 136, 222, 322, 591, 878, 1486) and the parse site. ~100 LOC across cli.py, manifest.py, storage/local.py, new constants.py. _Storage-key helpers + all construction/parse sites ✅ shipped in v0.8.4 (`src/mind_meld/storage/keys.py` with path-traversal validation). Remaining: `constants.py` for CONFLICT_INFIX / CONFLICT_AGE_DAYS / TOMBSTONE_TTL_DAYS / FORMAT_VERSION._

### Track 1A: Decompose overgrown cli.py functions ✅ shipped in v0.8.4
_2 tasks · ~1.5 days (human) / ~20 min (CC) · medium risk · [cli.py]_
_touches: src/mind_meld/cli.py, tests/test_integration.py_

- ✅ **Decompose `_pull_core` (247 lines)** — `cli.py:961-1208`: split into `_select_devices`, `_prefetch_manifests`, `_pull_one_source`, `_print_pull_summary` so the top-level reads as five orchestration calls. Also fix the double `list_devices` call (cli.py:994, 1008) while you're in there, and align `_predict_pull_outcome` return vocabulary with `ApplyOutcome` (cli.py:241-270). _src/mind_meld/cli.py, ~250 lines._ (L) _Shipped in v0.8.4 as 6 helpers (`_select_devices`, `_prefetch_manifests`, `_preflight_conflicts`, `_pull_one_source`, `_fsync_touched_parents`, `_print_pull_summary`); double `list_devices` fixed. Codex adversarial review flagged the planned `_predict_pull_outcome` vocabulary rename as a worse abstraction — reversed, vocabulary unchanged._
- ✅ **Decompose `_apply_incoming_file` (114 lines)** — `cli.py:447-561`: extract `_apply_write`, `_apply_merge`, `_apply_conflict` helpers; `_apply_incoming_file` dispatches via outcome classification. _src/mind_meld/cli.py, ~150 lines._ (M) _Shipped in v0.8.4; dispatcher shrank 125 → ~50 LOC._

### Track 1B: Walker + manifest + merge DRY ✅ shipped in v0.8.5
_3 tasks + 1 contract-change cleanup · ~0.5 day (human) / ~12 min (CC) · low risk · [manifest.py, merge.py]_
_touches: src/mind_meld/manifest.py, src/mind_meld/merge.py, tests/test_manifest.py_

- ✅ **Extract `_record_file` helper** — per-file "exclude → stat → size-check → hash → record" block duplicated verbatim between `walk_claude_source` and `walk_generic_source`. Shipped in v0.8.5 as a maximalist helper that also owns `relative_to(base)` (codex adversarial review caught the original signature, which would have left rel-path computation duplicated across call sites). _src/mind_meld/manifest.py_. (S) _Shipped in v0.8.5._
- ✅ **Extract `_is_active_tombstone(info, cutoff) -> bool` helper** — `generate_tombstones` (carry-forward) and `collect_tombstones` (fleet aggregation) both duplicated the fromisoformat + tzinfo-UTC guard + cutoff compare + `(ValueError, TypeError)` handling. Shipped as the full predicate (codex adversarial review correctly flagged the originally-planned parse-only `_parse_tombstone_ts` as beneath the abstraction threshold — extracting just the parse would have left the cutoff-comparison and try/except duplicated). _src/mind_meld/manifest.py_. (S) _Shipped in v0.8.5._
- ✅ **`merge.py` dispatch + join helpers** — `should_merge`/`merge_file` duplicated strategy classification; `merge_jsonl`/`merge_lines` shared an identical join-lines tail. Shipped as `_merge_strategy(rel_path) -> Callable | None` (direct callable, no registry — codex review correctly flagged the original `_STRATEGIES` dict as YAGNI machinery for two predicates) + `_join_lines(lines)` helper. _src/mind_meld/merge.py_. (S) _Shipped in v0.8.5._
- ✅ **Drop redundant `normalize_manifest(remote_manifest)` call at manifest.py:607 + enforce caller contract at runtime** — added during /plan-eng-review 2026-04-24 after auditing all three caller paths (`_fetch_remote_manifest` → `load_manifest`; `sidecar.read` → explicit normalize; peer-fallback synthetic dict) and finding the call was positionally wrong (ran AFTER the carry-forward loop had already consumed tombstone keys). /review cross-model adversarial (Claude + Codex, 2026-04-24) independently found that the dropped call was also doing load-bearing v1→v2 `files`→`sources` promotion right before new-tombstone detection — so a hand-built v1-shaped dict would silently produce zero new tombstones (delete-propagation loss) instead of the original `claude:<path>` entries. Fixed by enforcing the contract at runtime: `generate_tombstones` now raises `ManifestError` on a v1-shaped `remote_manifest` instead of failing silently. Tests `TestGenerateTombstonesContract::test_raises_on_v1_shaped_input` and `test_none_remote_still_allowed` pin the contract. _src/mind_meld/manifest.py, tests/test_manifest.py_. (S) _Shipped in v0.8.5._

### Track 1C: Post-1A cli.py follow-ups ✅ shipped in v0.8.6
_3 tasks · ~0.5 day (human) / ~15 min (CC) · low risk · [cli.py]_
_touches: src/mind_meld/cli.py, src/mind_meld/storage/keys.py, tests/test_preflight.py, tests/test_track_1c.py (new)_

- **diff-call-site DRY pass** — `cli.py:1454-1459, 1843-1868, 2101-2112, 2464-2473`: the four `diff_files` call sites share a per-source iteration pattern but diverge on filtering (push filters by `has_changes`, status/diff filter by `--source` arg, pull builds local_files by hashing). Track 1A's `_pull_core` decomposition resolves one of the four; the other three (push, status, diff) still carry the boilerplate. Candidate primitive: a helper that takes local + remote source dicts and yields `(src_name, src_data, remote_src, diff)` tuples, callers filter. [plan-eng-review 2026-04-23] _src/mind_meld/cli.py, ~40 lines._ (S)
- **GC: validate blob shape, not just depth** — `_do_gc` (cli.py:2660-2698) now flags wrong-depth `data/` entries (v0.8.1 fix), but a 3-segment path with bogus middle/leaf still gets reaped as an "orphan" if not in `referenced_hashes`. Examples: `data//foo.enc` (empty device_id), `data/dev/not-a-sha.enc` (non-hex leaf). Add a stricter validator: device_id segment matches `[0-9a-f]{8,}`, leaf matches `[0-9a-f]{64}`. [codex-adversarial 2026-04-23] _src/mind_meld/cli.py, ~20 lines._ (S)
- **Autopull breadcrumb: `degraded` outcome on durability fsync failure** — `_pull_core` (cli.py:1978-1990) warns to stderr when `fsutil.fsync_dir` fails during the deferred-durability commit (v0.8.1 fix), but the result still has no `durability_degraded` field, so `autopull` writes `outcome: "success"` to the breadcrumb. A user reading `mm status` only sees "success" while recently-pulled renames may not survive crash/power loss. Mirrors the autopush "no-sources" breadcrumb fix: thread `durability_degraded` through `PullResult`, surface as `outcome: "degraded"` in autopull. [codex-adversarial 2026-04-23] _src/mind_meld/cli.py, ~25 lines._ (S)

---

## Group 2: Init flow + sync_log generalization + config polish

Multi-source assumption lag — `init` still hardcodes `~/.claude` and
`write_sync_log` is claude-specific. Resolving this makes mm genuinely
multi-source in day-to-day usage. Track 2B picks up the two config-polish
follow-ups from /plan-eng-review on 2026-04-23.

### Track 2A: Init decomposition + DEFAULT_SOURCES reuse + sync_log generalization
_5 tasks · ~1.5 days (human) / ~25 min (CC) · medium risk · [cli.py, config.py, synclog.py]_
_touches: src/mind_meld/cli.py, src/mind_meld/config.py, src/mind_meld/synclog.py, tests/test_integration.py, tests/test_synclog.py_

- **Decompose `init` (85 lines)** — `cli.py:645-730`: extract `_prompt_passphrase()`, `_maybe_add_gstack_source(config)`, `_save_and_register(config)` helpers. _src/mind_meld/cli.py, ~120 lines._ (M)
- **Reuse `DEFAULT_SOURCES` in init** — `cli.py:663-668, 704-720`: `init()` hardcodes `"~/.claude"`, `52_428_800`, `65_536`, and re-inlines the full gstack source dict already in `config.DEFAULT_SOURCES`. Import and reuse `DEFAULT_CLAUDE_DIR` / `DEFAULT_MAX_FILE_SIZE` / `DEFAULT_ARGON2_MEMORY_KB`, and pick the gstack entry out of `DEFAULT_SOURCES`. _src/mind_meld/cli.py, src/mind_meld/config.py, ~40 lines._ (S)
- **Init refuses to finish without a source path** — `cli.py:696-723`: current flow writes `sync.claude_dir = "~/.claude"` unconditionally; a pure-gstack user ends with `get_sources() == []` and push says "No sync sources found". Prompt for which sources to enable with auto-detect defaults; refuse to finish init if no source path exists on disk. _src/mind_meld/cli.py, tests/test_integration.py, ~60 lines._ (M)
- **`write_sync_log` keyed off source type** — `cli.py:1147-1158`: called only when `src_name == "claude"`. A user renaming the claude source in config (spec doesn't forbid it) breaks sync-log entirely. Key off `src_cfg["type"] == "claude"` instead. _src/mind_meld/cli.py, ~15 lines._ (S)
- **`synclog.py` param rename `claude_dir` → `base_path`** — `synclog.py:16, 34-36`: naming lags the multi-source rename; `projects/` layout stays claude-specific but is now explicitly scoped. Rename the parameter, document the claude-only semantic, scope the caller. _src/mind_meld/synclog.py, tests/test_synclog.py, ~30 lines._ (S)

### Track 2B: Config polish — eng-review follow-ups
_2 tasks · ~0.5 day (human) / ~12 min (CC) · low risk · [config.py, cli.py]_
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, tests/test_config.py_
_Depends on: Track 2A landing first so the `cli.py:1076` config-read pattern is consolidated. Otherwise the refactor fights with in-flight init work._

- **Stop mutating config in `_apply_defaults`; compute expanded paths lazily in `get_sources`** — rework so `load → save` round-trip preserves human-readable forms (e.g. `~/.claude` stays `~/.claude`). Expansion + `.resolve()` happen at use sites only. Backfill save at `cli.py:227-233` silently rewrites user's TOML from `~/.claude` to the canonical absolute path on first-run-after-upgrade — Codex flagged this during /plan-eng-review 2026-04-23 as a UX footgun. v0.7.1's `.resolve()` addition extends the footgun to symlink dereference; the proper fix is to not mutate config at load time at all. Requires updating all readers of `config["sync"]["claude_dir"]` to re-expand at use site. _src/mind_meld/config.py, src/mind_meld/cli.py, ~60 lines._ (M)
- **Rich `ConfigError` with TOML line numbers on parse failure** — when `tomllib.load()` raises `TOMLDecodeError` (config.py:68-69), extract the line number (`.line` / `.column`) and include it in the `ConfigError` message. Current "config: failed to parse /path — <raw msg>" is serviceable but not pinpoint; a hand-edited `sync.sources` block with a syntax error should tell the user exactly which line. Low payoff (most configs are Claude Code-driven, not hand-edited) but cheap. _src/mind_meld/config.py, tests/test_config.py, ~15 lines._ (S)

---

## Group 3: Test hygiene + style polish

CLI-driven end-to-end coverage + lint polish. Style nits collapse into
pre-flight; the CLI E2E migration is the one load-bearing piece.

**Pre-flight** (any agent, <30 min each, style-only):
- Type hints on helper `backend` params — `cli.py:108, 116, 124, 241, 273, 298, 332, 360, 381, 447, 564, 624, 766, 961`: helpers accept untyped `backend`, while `devices.py:13, 30, 43` types it as `LocalBackend`. Add `backend: LocalBackend` hints matching devices.py.
- Standardize optional syntax — `cli.py`: 6 `Optional[X]` and 11 `X | None` despite `from __future__ import annotations`. Standardize on `X | None` and drop `Optional` from the typing import.
- Drop placeholderless f-strings + narrow keyring bare-except — `cli.py:897, 1246, 1377, 1502, 1864`: literal strings marked `f"..."` with no interpolation. Also narrow `crypto.py:97, 104, 120, 147` keyring bare-except to `keyring.errors.KeyringError` + `ImportError`.

### Track 3A: Test improvements
_3 tasks · ~0.5 day (human) / ~10 min (CC) · low risk · [tests/]_
_touches: tests/test_integration.py, tests/test_conflict_copy.py_

- **Migrate `TestPushPullRoundTrip` to CLI invocation** — `tests/test_integration.py:53-163`: bypasses the CLI entirely; uses `build_manifest`/`encrypt`/`storage.put` by hand. Does not exercise `_pull_core` or `_apply_incoming_file`. Replace with `runner.invoke(app, ["push"|"pull"])` like `TestMultiSourceSync`. Once migrated, the `build_manifest` import drops too. Add a CLI-driven end-to-end test covering push→pull→conflict→tombstone propagation together. _tests/test_integration.py, ~100 lines._ (M)
- **Rename misleading test** — `tests/test_integration.py:103-163`: `test_deletion_propagation` is named after pre-additive behavior but the body asserts the opposite. Rename to `test_deletion_not_propagated_in_additive_model`. _tests/test_integration.py, ~5 lines._ (S)
- **Hoist lazy imports** — `tests/test_conflict_copy.py` (~11 sites) and `tests/test_integration.py` (~7 sites): in-function imports of `mind_meld.cli` helpers and `pathlib.Path`/`shutil` redeclared despite module-level blocks. Hoist to the top. _tests/test_conflict_copy.py, tests/test_integration.py, ~30 lines._ (S)

---

## Group 4: Release infrastructure

CI plumbing that's been ad-hoc to date. The project currently has no CI on
main — every push trusts the human (or Claude session) to have run `pytest`
locally. For a tool whose whole job is "never silently eat user deletions
across machines," the absence of CI on main is a real risk surface.
Parallel-safe with every other group.

### Track 4A: GitHub Actions CI workflow
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
- Group 4 ← {}    (parallel to all — release infra)
```

Track detail per group:

```
Group 1: Decomposition + DRY
  Pre-flight .............. ~2 hr (constants + storage key helpers) [storage keys ✅ v0.8.4]
  ├── Track 1A ........... ~1.5d .. 2 tasks .. decompose _pull_core + _apply_incoming_file [✅ v0.8.4]
  ├── Track 1B ........... ~0.5d .. 3 tasks .. walker + manifest + merge DRY [✅ v0.8.5]
  └── Track 1C ........... ~0.5d .. 3 tasks .. post-1A cli.py follow-ups [✅ v0.8.6]

Group 2: Init flow + sync_log generalization + config polish
  ├── Track 2A ........... ~1.5d .. 5 tasks .. init + sync_log
  └── Track 2B ........... ~0.5d .. 2 tasks .. config polish (eng-review follow-ups)

Group 3: Test hygiene + style polish
  Pre-flight .............. 3 items (type hints, optional syntax, f-strings)
  └── Track 3A ........... ~0.5d .. 3 tasks .. tests (CLI-driven, rename, hoist)

Group 4: Release infrastructure
  └── Track 4A ........... ~1-2 hr .. 1 task .. GitHub Actions CI
```

**Total: 4 groups · 7 tracks · 19 tasks (+ 4 pre-flight items)**

---

## Future (Phase 2+)

- **Selective sync (`sync.include` / `sync.exclude`)** — per-project filtering so users with dozens of Claude projects can sync just the 2-3 they actively use across machines. Config schema + glob validation + walker integration + CLI flag surface. _src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/cli.py, ~180 lines._ (M) _Deferred because: no user demand signal yet; revisit on first support case from someone with dozens of projects who wants to sync just 2-3._

- **Mtime hash cache** — push-side perf: skip re-hashing files whose mtime hasn't changed since the last push. Per-device local cache at `~/.config/mind_meld/local-manifest.json` keyed by (mtime, size, sha). _src/mind_meld/cache.py (new), src/mind_meld/manifest.py, src/mind_meld/cli.py, ~210 lines._ (M) _Deferred because: motivating 4-minute-push problem on 1000 files was already solved by crypto v2 (process-scoped master key + HKDF). Revisit only if push latency becomes user-visible again._

- **Three-way merge base (stored last-synced hash)** — pull-side correctness upgrade: per-source, per-file last-synced hash at `~/.config/mind_meld/sync-state.json`. Distinguishes "remote changed, I didn't" from "we both changed" — fast-forward when only one side changed; conflict-copy only when both diverged from base. _src/mind_meld/sync_state.py (new), src/mind_meld/cli.py, ~310 lines._ (M-L) _Deferred because: correctness upgrade, not a fix — current Syncthing conflict-copy pattern works; no divergence-misclassification reports. Revisit if users report "it conflict-copied a file I didn't even touch."_

- **`mm rekey` passphrase rotation** — Format v2 makes `master_key` the rotation boundary but v2 blobs don't carry a `key_scheme` byte (dropped per /plan-ceo-review simplification). Rotation requires format v3: either re-wrap `master_key` under the new passphrase, or re-encrypt every blob under a freshly-derived `master_key`. Completes the crypto story but requires a format bump and migration path. _src/mind_meld/crypto.py, src/mind_meld/cli.py, SPEC.md, ~200-400 lines._ (M-L) _Deferred because: post-1.0 P3 — requires format v3 and a migration dance; no users blocked pre-1.0._

- **Blob-directory as secondary peer-discovery in corrupt-manifest recovery** — in `_collect_peer_tombstones` (or a sibling helper), when a peer's `devices/<id>.json` is corrupt or missing but `data/<id>/` has blobs and `manifests/<id>/*.enc` decrypts, promote the blob-dir-derived `device_id` to the peer list. Recovers tombstones from the otherwise-dropped peer. Widens the trust surface — blob-presence becomes load-bearing evidence of a peer's existence, not just a device-registry entry. _src/mind_meld/cli.py, ~30 lines._ (S) _Deferred because: observation-bar — land when the first real support case appears where corrupt `devices.json` masks a recoverable manifest. v0.8.0's `list_devices` shape-validation + warning is enough until then._

- **PyPI publish workflow** — `.github/workflows/release.yml` that builds + publishes to PyPI on git tag push (e.g. `v0.8.0` → trigger). Uses `hatchling` build backend (already configured in `pyproject.toml`). Currently users install via `pip install -e .` from a local clone; PyPI distribution would let someone `pip install mind-meld` cleanly. Commits to a public package namespace (name squatting, can't easily rename); need to decide on trusted-publisher vs API token auth. Depends on Group 4 (CI) landing first (tests green before publishing anything). _.github/workflows/release.yml, ~50 lines._ (S) _Deferred because: observation-bar — land when "how do I install this" becomes friction. No user demand signal today._

---

## Unprocessed

Items awaiting triage by /roadmap. Added by other skills or manually.
