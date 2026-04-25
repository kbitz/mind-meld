# Roadmap — Phase 1 (v0.x → v1.0 cleanup sweep)

Organized as **Groups > Tracks > Tasks**. Groups are sequential — complete
Group 1 before starting Group 2. Tracks within a group run in parallel
(file-ownership-disjoint). Pre-flight items are trivial fixes any agent can
pick up.

Items originate from the 2026-04-22 `/full-review` audit, review follow-ups
accumulated across v0.5.1/v0.6.x/v0.7.x/v0.8.x, /plan-eng-review on
2026-04-23, and the 2026-04-24 first-pull conflict-UX session on kb-mbp.
Every item targets the v1.0 release.

**Groups 1-5 fully shipped.** Tracks 5A / 5B / 5C / 5E shipped through
v0.9.2, the v0.9.3 hotfix patch added `config.yaml` to the gstack source's
default `exclude_patterns`, and Track 5D shipped as v0.9.4 (adversarial-
review follow-ups hardening the v0.8.15 Track 5A ship: `_find_conflict_files`
tuple-key dedup + `_register_and_save` order swap + push-time self-heal).
**v0.9.5 ships the auto-upgrade nudge** (out-of-scope for the v1.0 cleanup
sweep, but included here as a reference point — see CHANGELOG.md for the
full feature description and CLAUDE.md "Auto-upgrade nudge" + "Release
discipline" sections for architecture). See `docs/PROGRESS.md` for the
full version history. Group 1's `constants.py` preflight was dropped after
a `/plan-eng-review` cohesion check (2 of 4 constants are single-module,
extraction would split the cohesive `FORMAT_VERSION`/`FORMAT_VERSION_LEGACY_V1`
pair).

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

## Group 5: Conflict UX & first-pull polish ✓ Complete
_Depends on: none_

Surfaced 2026-04-24 from kb-mbp's first-pull session: 286-file pull on a
fresh Mac landed 6 conflict copies with confusing labels, no inline
filenames, an over-truncated `mm conflicts` table, silence during the
~4-minute download, and a P0 contract regression on `mm autopull` /
`autopush`. Tracks 5A / 5B / 5C / 5E shipped through v0.9.2 (plus a
v0.9.3 hotfix patch adding `config.yaml` to the gstack source's default
`exclude_patterns`). Track 5D shipped as v0.9.4 — closes Group 5.

_Track 5A (Auto-command silent-mode + scope bugs + Group 5 preflight — P0 autopull/autopush silent-mode contract regression, `_synced_scan_dirs` undercount on generic-type sources, `_save_and_register` rollback, gstack `include_files` default add) — ✓ Complete (v0.8.15). 3 tasks shipped + 1 preflight._

_Track 5B (Pull / resolve / conflicts UX surfaces — vocabulary unification `(c)`/`(f)` → `(l)/(r)/(b)/(a)`, inline conflicted filenames in pull summary, `mm conflicts` table column rename + per-column wrap, Rich Progress widget for TTY, quiet-mode contract fix routing per-source conflicts to stderr) — ✓ Complete (v0.9.0). 4 tasks + scope expansions shipped. **BREAKING**: legacy `c`/`f` letters rejected loudly. 14 new tests (700 pass). Track 5E inherited the pre-inversion `.sync-conflict-*` migration subtask (D9 handoff)._

_Track 5C (`exclude_patterns` + log + migration UX — per-source glob list, consumer-boundary filter at `_pull_core` + `_push_core`, tombstone-suppression invariant, `mm log` JSONL writer/reader, `mm migrate-config` command, autopull/autopush missing-excludes breadcrumb, `mm sources` excluded-count column, `mm status` warning) — ✓ Complete (v0.9.1). Pivoted via /plan-ceo-review from "conflict inversion + real-merge backends" after kb-mbp first-pull data showed 24/25 divergent files were per-machine artifacts that excludes prevent. Real-merge backend deferred to Future. Inversion split to Track 5E. 38 tests + 5 IRON RULE pins._

_Track 5E (Conflict default inversion — inverted `_apply_conflict` (canonical = local; remote → sidecar), strict pull-start fleet-version refusal, pre-inversion `v0-` filename migration, dual-mode resolve dispatch by filename prefix, `mm conflicts` Mode column, `mm devices` Version column, `update_last_seen` writes `last_seen_version`) — ✓ Complete (v0.9.2). **BREAKING**: pre-v0.9.2 peers refused at pull start. Added `packaging>=21.0`. 11 tests in `TestInversion5E`. 768 pass._

_v0.9.3 hotfix patch (post-Track-5C): added `config.yaml` to the gstack source's default `exclude_patterns` (gstack's version-check tracking, machine-local). Existing installs need `mm migrate-config` to pick it up._

_Track 5D (Track 5A adversarial-review follow-ups — `_find_conflict_files` tuple-key dedup over the `include_dirs` rglob × `include_files` sibling-glob overlap, `_save_and_register` → `_register_and_save` order swap with best-effort cleanup so a SIGKILL/OOM/power-loss window between writes leaves an inert orphan instead of an inverse half-state, and a new `_ensure_device_registered` push-time self-heal that retroactively fixes any pre-v0.9.4 victims of the v0.8.15..v0.9.3 half-state) — ✓ Complete (v0.9.4). 2 tasks + 1 codex-follow-through self-heal hook. 15 new tests (5 dedup, 7 register/save, 3 self-heal); 789 pass._

---

## Execution Map

Adjacency list (who depends on whom):

```
- Group 1 ← {}     ✓ Complete (Tracks A/B/C shipped; constants.py preflight dropped 2026-04-24)
- Group 2 ← {1}    ✓ Complete (v0.8.7 + v0.8.8)
- Group 3 ← {2}    ✓ Complete (v0.8.10)
- Group 4 ← {}     ✓ Complete (v0.8.11)
- Group 5 ← {}     ✓ Complete (5A/5B/5C/5E shipped through v0.9.2 + v0.9.3 hotfix + 5D shipped v0.9.4)
```

**Active total: 0 in-flight Groups. Phase 1 complete — see Future for Phase 2+ items.**
**Shipped: Groups 1, 2, 3, 4, 5 (Tracks 5A + 5B + 5C + 5D + 5E + Group 5 preflight + v0.9.3 hotfix) — see PROGRESS.md.**

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
