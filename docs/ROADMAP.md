# Roadmap — Phase 1 (v0.x → v1.0 cleanup sweep)

Organized as **Groups > Tracks > Tasks**. Groups are sequential — complete
Group 1 before starting Group 2. Tracks within a group run in parallel
(file-ownership-disjoint). Pre-flight items are trivial fixes any agent can
pick up.

Items originate from the 2026-04-22 `/full-review` audit, review follow-ups
accumulated across v0.5.1/v0.6.x/v0.7.x/v0.8.x, /plan-eng-review on
2026-04-23, and the 2026-04-24 first-pull conflict-UX session on kb-mbp.
Every item targets the v1.0 release.

**Correctness foundation, error discipline, decomposition + DRY (Tracks
1A/1B/1C), init flow + sync_log generalization (Group 2), test hygiene +
style polish (Group 3), CI infrastructure (Group 4), Track 5A's
auto-command + scope bug bundle (with Group 5's gstack `include_files`
preflight), and Track 5B's resolve/conflicts UX vocabulary unification
(with v0.9.0 BREAKING input-letter change) all shipped through v0.9.0.**
See `docs/PROGRESS.md` for the full version history. Group 1 is fully
shipped — its remaining `constants.py` preflight was dropped after a
`/plan-eng-review` cohesion check (2 of 4 constants are single-module,
extraction would split the cohesive
`FORMAT_VERSION`/`FORMAT_VERSION_LEGACY_V1` pair). Group 5 still in
flight: Track 5D adds two adversarial-review follow-ups that harden the
v0.8.15 Track 5A ship; Track 5C inverts the conflict default and adds
real merge backends (inherits a load-bearing subtask from Track 5B's CEO
review: handle pre-inversion `.sync-conflict-*` files via timestamp-based
mode detection or one-time migration).

---

## Group 1: Decomposition + DRY ✓ Complete

Tracks 1A/1B/1C all shipped (v0.8.4 / v0.8.5 / v0.8.6). The proposed
`constants.py` extraction preflight was dropped on 2026-04-24 after a
`/plan-eng-review` cohesion check: only `FORMAT_VERSION` and `CONFLICT_INFIX`
are genuinely cross-module of the four candidates, and even those pair
tightly with `FORMAT_VERSION_LEGACY_V1` (legacy-blob detection, crypto.py)
and `CONFLICT_PATTERN` (manifest.py) — siblings the original task missed.
`CONFLICT_AGE_DAYS` and `TOMBSTONE_TTL_DAYS` are single-module.
Centralization would have been net-new structure without a real cohesion
problem to solve.

_Track 1A (Decompose `_pull_core` + `_apply_incoming_file`) — ✓ Complete (v0.8.4). 2 tasks shipped._

_Track 1B (Walker + manifest + merge DRY) — ✓ Complete (v0.8.5). 3 tasks + 1 contract-change cleanup shipped._

_Track 1C (Post-1A cli.py follow-ups — diff-call-site DRY, GC blob-shape validation, autopull degraded breadcrumb) — ✓ Complete (v0.8.6). 3 tasks shipped._

---

## Group 2: Init flow + sync_log generalization + config polish ✓ Complete

Shipped across v0.8.7 + v0.8.8. Multi-source assumption lag resolved — `init` no
longer hardcodes `~/.claude` and `write_sync_log` is type-keyed instead of
name-keyed. Config polish from /plan-eng-review followed in v0.8.8.

_Track 2A (Init decomposition + DEFAULT_SOURCES reuse + sync_log generalization) — ✓ Complete (v0.8.7). 5 tasks + refinements shipped._

_Track 2B (Config polish — eng-review follow-ups: backfill path preservation + ConfigError prefix rename) — ✓ Complete (v0.8.8). 2 tasks shipped._

---

## Group 3: Test hygiene + style polish ✓ Complete

Shipped as v0.8.10. Pre-flight style polish (type hints, optional syntax,
placeholderless f-strings, keyring except-narrowing) + Track 3A test
improvements (CliRunner migration, combined E2E, lazy-import hoist) all
landed together. Codex `/review` caught a P0 keyring-propagation gap during
review and the fix shipped with 3 regression pins.

_Track 3A (CliRunner migration + combined push/pull/conflict E2E + 86 lazy-import hoists) — ✓ Complete (v0.8.10). 3 tasks shipped + 3 codex-follow-through pins._

---

## Group 4: Release infrastructure ✓ Complete

Shipped as v0.8.11. Single `macos-latest` + Python 3.13 GitHub Actions
workflow runs `ruff check`, `ruff format --check`, `pytest tests/`, wheel
build + install + `mm --version` smoke, and asserts the real Keychain
backend loads. Ruff pinned at 0.15.12 with `E/F/W/I` ruleset (isort
enforcement locks Group 3's import hoisting).

_Track 4A (GitHub Actions CI workflow + ruff pin + README badge + 113-violation drift sweep) — ✓ Complete (v0.8.11). 1 task shipped._

---

## Group 5: Conflict UX & first-pull polish
_Depends on: none_

Surfaced 2026-04-24 from kb-mbp's first-pull session: 286-file pull on a
fresh Mac landed 6 conflict copies with confusing labels, no inline
filenames, an over-truncated `mm conflicts` table, silence during the
~4-minute download, and a P0 contract regression on `mm autopull` /
`autopush`. Tracks 5B/5C/5D are sequenced serially because they all touch
`cli.py` in different functions — same intra-Group serial pattern that
Tracks 1A/1C and 2A/2B used in this project. Track 5D ships next (smallest,
hardens 5A's just-shipped fixes), then 5B (UX surfaces), then 5C (conflict
inversion + real merge).

_Track 5A (Auto-command silent-mode + scope bugs + Group 5 preflight) — ✓ Complete (v0.8.15). 3 tasks shipped + gstack `include_files` default add._

### Track 5D: Track 5A adversarial-review follow-ups
_2 tasks · ~0.5 day (human) / ~30 min (CC) · low-medium risk · [cli.py + devices.py hardening]_
_touches: src/mind_meld/cli.py, src/mind_meld/devices.py, tests/_
_Sequencing: 5A shipped in v0.8.15; 5D hardens that surface and ships next, before 5B/5C._

Two adversarial-pass findings from the v0.8.15 `/review` cycle. Both target
exactly the code paths Track 5A just touched: `_find_conflict_files`
(extended in v0.8.15 with the depth-0 sibling-glob for `include_files`) and
`_save_and_register` (gained the rollback try/except in v0.8.15). Small
batch, ships before 5B/5C so the v0.8.15 fixes harden cleanly.

- **`_find_conflict_files` double-counts when an `include_files` entry sits inside an `include_dirs` directory** — surfaced by the v0.8.15 `/review` adversarial pass. If a user customizes their gstack source with `include_files: ["projects/notes.md"]` AND `include_dirs: ["projects"]` (nested), a conflict file at `projects/notes.sync-conflict-...md` gets visited twice: once via the `include_dirs` rglob (recursive scan from `projects/`) and once via the new depth-0 sibling-glob (parent_dir = `projects`, glob `notes.sync-conflict-*.md`). Result: duplicate rows in `mm conflicts`, `mm gc --conflicts` reaps the same path twice (`unlink` idempotent thanks to `missing_ok=True`, but the count printed to the user is wrong), and `mm resolve` would silently no-op on the second visit. The default config doesn't trigger this (all `include_files` entries are bare top-level dotfiles), so the practical risk is low — but adding a `seen: set[Path]` dedup pass at the top of `_find_conflict_files` is ~5 lines and removes the footgun. _src/mind_meld/cli.py + tests/test_conflict_copy.py, ~10-15 lines._ (XS) [review]
- **`_save_and_register` not crash-safe between `save_config` and `register_device`** — surfaced by the v0.8.15 `/review` adversarial pass. The new rollback try/except handles `(StorageError, OSError, MindMeldError)` from `register_device`, but only catches Python exceptions. A SIGKILL, OOM, or power loss in the window between `save_config()` returning and `register_device()` either succeeding or raising leaves the user with a local config claiming a `device_id` that storage's `devices/<id>.json` doesn't contain — exactly the orphan state the v0.8.15 rollback was supposed to prevent. Init's existing overwrite prompt (cli.py:1474) is the only safety net; a user who answers "no" stays orphaned silently. Two viable fixes: (a) swap order — `register_device` first (storage write), then `save_config` (local write); orphan storage entries are harmless and cleanable, but orphan local config silently breaks pushes. (b) Add a `device_registered: bool` sentinel committed only after register succeeds; init detects sentinel-missing and re-runs register on next start. (a) is simpler; (b) is more robust against future failure modes. _src/mind_meld/cli.py + possibly src/mind_meld/devices.py + tests, ~30-50 lines._ (S-M) [review]

### Track 5B: Pull / resolve / conflicts UX surfaces
_✓ Complete (v0.9.0). 4 tasks shipped + scope expansions per /plan-ceo-review (D3 vocabulary unification across resolve flow, D4 loud rejection of legacy `c`/`f`, D5 verbose unlocks inline-paths cap, D7 preface for parallel `(p)/(d)/(s)`, D9 dual-semantics handed to 5C, D10 indent hierarchy for multi-device disambig, D11 quiet-mode contract fix routes per-source conflicts/failures to stderr) and /plan-eng-review (5B-5C-REMAP-BOUNDARY markers in cli.py + test class so 5C’s inversion surfaces every assertion that needs to flip). 14 new tests (700 pass). **BREAKING**: input letters `c`/`f` for `mm resolve` now rejected loudly to stderr per visible-failure contract — pre-existing scripts must migrate to `l`/`r`. Track 5C inherits one explicit subtask (D9 handoff): handle pre-inversion `.sync-conflict-*` files via timestamp-based mode detection or one-time migration; without it, persisted conflict files at 5C ship would have mislabeled (l)/(r) labels and risk silent data loss._

### Track 5C: exclude_patterns + log + migration UX
_✓ Complete (v0.9.1). Per-source `exclude_patterns` glob list, consumer-boundary filter (`_pull_core` + `_push_core`), tombstone-suppression invariant, `mm log` JSONL writer + reader, `mm migrate-config` command, mm pull/push migration prompt + autopull/autopush breadcrumb (visible-failure contract), `mm sources` excluded-count column, `mm status` missing-excludes warning. 38 new tests including 5 IRON RULE regression pins (kb-mbp two-device case, tombstone-on-exclude-transition, tombstone-on-unexclude-transition, sidecar bypass guard, mm gc safety). Originally scoped to "conflict default inversion + real-merge backends"; pivoted via /plan-ceo-review on 2026-04-25 after analysing the kb-mbp 2026-04-24 first-pull data (25 divergent files, 24 of them per-machine artifacts that exclude_patterns prevents from ever conflicting). Real-merge backend deferred to Phase 2. Conflict inversion split out as Track 5E._

### Track 5E: Conflict default inversion (BREAKING)
_✓ Complete (v0.9.2). Inverted `_apply_conflict` (canonical = local; remote → sidecar) + strict pull-start fleet-version refusal (`mm pull` exits non-zero before any I/O if any peer's `last_seen_version < 0.9.2` or device.json is corrupt) + pre-inversion conflict-file migration to `v0-` prefix (lock-protected, mm pull / mm resolve only) + dual-mode resolve dispatch by filename prefix (`v0-` = pre-inversion ops; no prefix = post-inversion ops) + `mm conflicts` Mode column + `mm devices` Version column + `update_last_seen` writes `last_seen_version`. Added `packaging>=21.0` for `Version` parsing. 11 new tests in `TestInversion5E` pinning the IRON RULE regressions. 768 pass._

---

## Execution Map

Adjacency list (who depends on whom):

```
- Group 1 ← {}     ✓ Complete (Tracks A/B/C shipped; constants.py preflight dropped 2026-04-24)
- Group 2 ← {1}    ✓ Complete (v0.8.7 + v0.8.8)
- Group 3 ← {2}    ✓ Complete (v0.8.10)
- Group 4 ← {}     ✓ Complete (v0.8.11)
- Group 5 ← {}     (in-flight — Tracks 5A ✓ v0.8.15, 5B ✓ v0.9.0; 5D → 5C remain)
```

In-flight detail:

```
Group 5: Conflict UX & first-pull polish
  Track 5A ............... ✓ Complete (v0.8.15) ...... 3 tasks + preflight shipped
  Track 5B ............... ✓ Complete (v0.9.0) ....... 4 tasks + scope expansions shipped (BREAKING)
  Track 5C ............... ✓ Complete (v0.9.1) ....... 38 tests + 5 IRON RULE pins (exclude_patterns + log + migrate UX)
  Track 5E ............... ✓ Complete (v0.9.2) ....... 11 tests + IRON RULE pins (conflict inversion + fleet refusal, BREAKING)
  └── Track 5D ........... ~0.5d ..... 2 tasks .. _find_conflict_files dedup + _save_and_register crash-safety  [last]
```

**Active total: 1 in-flight Group . 1 track remaining . 2 tasks**
**Shipped: Groups 1, 2, 3, 4, and Group 5 Tracks 5A + 5B + 5C + 5E (+ Group 5 preflight) — see PROGRESS.md.**

---

## Future (Phase 2+)

- **Selective sync (`sync.include` / `sync.exclude`)** — per-project filtering so users with dozens of Claude projects can sync just the 2-3 they actively use across machines. Config schema + glob validation + walker integration + CLI flag surface. _src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/cli.py, ~180 lines._ (M) _Deferred because: no user demand signal yet; revisit on first support case from someone with dozens of projects who wants to sync just 2-3._

- **Mtime hash cache** — push-side perf: skip re-hashing files whose mtime hasn't changed since the last push. Per-device local cache at `~/.config/mind_meld/local-manifest.json` keyed by (mtime, size, sha). _src/mind_meld/cache.py (new), src/mind_meld/manifest.py, src/mind_meld/cli.py, ~210 lines._ (M) _Deferred because: motivating 4-minute-push problem on 1000 files was already solved by crypto v2 (process-scoped master key + HKDF). Revisit only if push latency becomes user-visible again._

- **Three-way merge base (stored last-synced hash)** — pull-side correctness upgrade: per-source, per-file last-synced hash at `~/.config/mind_meld/sync-state.json`. Distinguishes "remote changed, I didn't" from "we both changed" — fast-forward when only one side changed; conflict-copy only when both diverged from base. _src/mind_meld/sync_state.py (new), src/mind_meld/cli.py, ~310 lines._ (M-L) _Deferred because: correctness upgrade, not a fix — current Syncthing conflict-copy pattern works; no divergence-misclassification reports. Revisit if users report "it conflict-copied a file I didn't even touch." Becomes a hard prereq for upgrading Track 5B's `git merge-file` to a 3-way merge._

- **`mm rekey` passphrase rotation** — Format v2 makes `master_key` the rotation boundary but v2 blobs don't carry a `key_scheme` byte. Rotation requires format v3: either re-wrap `master_key` under the new passphrase, or re-encrypt every blob under a freshly-derived `master_key`. Completes the crypto story but requires a format bump and migration path. _src/mind_meld/crypto.py, src/mind_meld/cli.py, SPEC.md, ~200-400 lines._ (M-L) _Deferred because: post-1.0 P3 — requires format v3 and a migration dance; no users blocked pre-1.0._

- **Blob-directory as secondary peer-discovery in corrupt-manifest recovery** — in `_collect_peer_tombstones` (or sibling helper), when a peer's `devices/<id>.json` is corrupt or missing but `data/<id>/` has blobs and `manifests/<id>/*.enc` decrypts, promote the blob-dir-derived `device_id` to the peer list. Recovers tombstones from the otherwise-dropped peer. Widens the trust surface — blob-presence becomes load-bearing evidence of a peer's existence, not just a device-registry entry. _src/mind_meld/cli.py, ~30 lines._ (S) _Deferred because: observation-bar — land when the first real support case appears where corrupt `devices.json` masks a recoverable manifest. v0.8.0's `list_devices` shape-validation + warning is enough until then._

- **PyPI publish workflow** — `.github/workflows/release.yml` that builds + publishes to PyPI on git tag push. Uses `hatchling` build backend (already configured). Currently users install via `pip install -e .` from a local clone; PyPI distribution would let someone `pip install mind-meld` cleanly. Commits to a public package namespace (name squatting, can't easily rename); need to decide on trusted-publisher vs API token auth. Tests-green prereq satisfied by Group 4 (CI shipped v0.8.11). _.github/workflows/release.yml, ~50 lines._ (S) _Deferred because: observation-bar — land when "how do I install this" becomes friction. No user demand signal today._

- **Cross-device source rename drift partitions sync** — Track 2A's type-keyed sync-log fix addressed *same-device* renames. Cross-device, manifests are still keyed by `src_name` (`manifest.py`, `_pull_core`'s `local_sources_map[src_name]` lookup), so if device A renames "claude" → "work-claude" but B keeps "claude", B's pull skips A's manifest via the unknown-source warning path. Codex adversarial 2026-04-24. Fix: cross-device source identity needs to key off `(type, signature)` or similar, not raw name. Bigger design change — likely a follow-up track or a SPEC.md-documented known limitation for v1.0. _src/mind_meld/cli.py, src/mind_meld/manifest.py, SPEC.md, ~100 lines._ (M-L) _Deferred because: no fleet-rename incident yet; documenting as a known limitation is enough for v1.0. Reopen on first support case where two devices use mismatched source names._

---

## Unprocessed

Items awaiting triage by /roadmap. Added by other skills or manually.
