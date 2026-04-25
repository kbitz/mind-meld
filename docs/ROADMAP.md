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
style polish (Group 3), and CI infrastructure (Group 4) all shipped through
v0.8.11.** See `docs/PROGRESS.md` for the full version history. Group 1
remains in-flight on its preflight `constants.py` extraction. Group 5
picks up the conflict-UX backlog from kb-mbp's first-pull, sequenced
P0-first within the Group: Track 5A ships the autopull silent-mode
regression + scope bugs, Track 5B relabels the resolve/conflicts UX, Track
5C inverts the conflict default and adds real merge.

---

## Group 1: Decomposition + DRY

Tracks 1A/1B/1C all shipped (v0.8.4 / v0.8.5 / v0.8.6). Only the preflight
`constants.py` extraction remains.

**Pre-flight** (shared-infra; serial, one-at-a-time):
- Create `src/mind_meld/constants.py` and move `CONFLICT_INFIX`, `CONFLICT_AGE_DAYS`, `TOMBSTONE_TTL_DAYS`, `FORMAT_VERSION`. ~30 LOC across cli.py, manifest.py, storage/local.py, new constants.py. _Storage-key helpers + all construction/parse sites already shipped in v0.8.4 (`src/mind_meld/storage/keys.py` with path-traversal validation)._

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
`autopush`. All ten tasks (+ pre-flight) share the same theme and
ride the same release window. Tracks 5A/5B/5C are sequenced serially
because they all touch `cli.py`, but in different functions — same
intra-Group serial pattern that Tracks 1A/1C and 2A/2B used in this
project. Audit's `COLLISIONS` finding on `cli.py` overlap is acknowledged
and tolerated (file-level granularity is too coarse for monolithic
`cli.py`; functions are disjoint in practice).

**Pre-flight** (shared-infra; serial, one-at-a-time):
- Add `retro-context.md` + `greptile-history.md` to gstack default `include_files` — `src/mind_meld/config.py:DEFAULT_SOURCES` gstack entry. Low-risk additive. Existing configs with explicit `sync.sources` need to add manually; one-line CHANGELOG note. _src/mind_meld/config.py, ~4 lines._ (XS) [manual]

### Track 5A: Auto-command silent-mode + scope bugs (P0 first)
_3 tasks · ~0.5 day (human) / ~25 min (CC) · low-medium risk · [cli.py auto + scope]_
_touches: src/mind_meld/cli.py, src/mind_meld/devices.py, src/mind_meld/manifest.py, SPEC.md, tests/_

Smallest batch, highest urgency — ships first so the autopull silent-mode
contract stops emitting stderr noise on every fresh Mac running the Claude
Code integration. Three discrete cli.py bugs that don't touch each other's
call sites.

- **P0 — `mm autopull` / `mm autopush` no longer exit silently on a fresh (un-initialized) machine** — `tests/test_integration.py::TestAutoCommands::test_autopull_no_config_exits_silently` and the autopush twin both fail. Tests assert `result.output == ""` when `CONFIG_PATH` points at non-existent file; observed `mm: push failed - init: config not found at <path> — run 'mm init' first.\n` on stderr. Pre-existing — not introduced by current branch. Likely cause: v0.8.1 visible-failure contract widening — `ConfigError` is now uniformly loud-on-stderr, but the "not initialized at all" case (literal `init: config not found` in `config.py:71`) is supposed to remain silent per CLAUDE.md's auto-command contract. Real-world impact: fresh Mac (or pre-`mm init`) sees stderr noise on every Claude Code session start if `mm autopull`/`autopush` is wired into hooks — exactly the kb-mbp first-pull scenario. Fix: (a) preflight `CONFIG_PATH.exists()` in autopull/autopush BEFORE `load_config` and short-circuit silently — keeps `ConfigError` loud for malformed-config. Add regression test exercising both quiet-on-no-config AND loud-on-malformed-config (existing test only covers no-config). _src/mind_meld/cli.py auto-command entrypoints + tests/test_integration.py::TestAutoCommands. ~15-30 lines._ (S) [BUG] **Priority: P0 — silent-mode contract regression, surfaces on every fresh Mac with the Claude Code integration.**
- **`_synced_scan_dirs` misses `include_files` sidecars on generic sources** — `cli.py:3231-3237` generic branch returns only resolved `include_dirs`. Trailing comment promises `+ base for single-file includes` but it's never implemented. Result: any conflict file produced for a top-level `include_files` entry (gstack default = `config.yaml`, `.completeness-intro-seen`, `.telemetry-prompted`, `.proactive-prompted`, `.welcome-seen`, `.codex-desc-healed`, all under `~/.gstack`) is invisible to `mm conflicts` / `mm resolve` / `mm gc --conflicts`. Reproduced 2026-04-24: kb-mbp first pull reported 6 conflicts; `mm conflicts` listed 5; missing was `~/.gstack/config.sync-conflict-20260424-233316-889e42c0.yaml`. Fix: in `_synced_scan_dirs`, after `include_dirs` loop, walk unique parents of `include_files` entries at **depth 0 only** (recursing would scan unsynced subtrees). Two viable shapes: (a) return `list[tuple[Path, bool]]` with recursive flag; (b) separate `_include_files_conflict_paths` helper computing per-entry candidate glob — more surgical. Test coverage: generic source with both include_dirs and include_files, conflicts in each, all three commands seeing the right count. Adjacent: verify `mm push` doesn't have parallel scope mismatch. _src/mind_meld/cli.py `_synced_scan_dirs` + `_find_conflict_files` + tests. ~30-50 lines._ (S) [BUG]
- **`_save_and_register` needs register-failure rollback** — `cli.py:_save_and_register` persists config → registers device → stores passphrase. If `register_device` raises (iCloud hiccup, transient `StorageError`) after `save_config` succeeded, local config now claims a `device_id` that `devices/` on storage doesn't contain. Peers scanning `devices/` via `_select_devices` never discover this device — push writes manifests under an ID no one is listening for. Pre-existing (Track 2A; codex adversarial during /review). Fix options: (a) swap order to register BEFORE save_config — config-without-device is its own ugly state; (b) on register failure, delete the saved config; (c) lazy self-repair in push that calls `register_device` if the device file is missing. Option (c) most robust. _src/mind_meld/cli.py, src/mind_meld/devices.py, ~40 lines._ (S) [review]

### Track 5B: Pull / resolve / conflicts UX surfaces
_4 tasks · ~0.5 day (human) / ~25 min (CC) · low risk · [cli.py UX]_
_touches: src/mind_meld/cli.py_
_Depends on: Track 5A landing first to clear the cli.py merge surface — disjoint functions but git-coordination cost is real._

- **Relabel `mm resolve` interactive prompt to user terms** — `cli.py:3629-3633`: today's `(c)anonical / (f)orce conflict → canonical / (b)oth / (a)bort` reads as internal jargon. User can't tell that "canonical" holds *remote* bytes and "conflict" holds *local* bytes, so picking (c) silently throws away local edits. Relabel to `(l)ocal / (r)emote / (b)oth [default] / (a)bort` with a one-line preface naming which side is which. Also fix the parallel `(p)romote / (d)elete / (s)kip` jargon at `cli.py:3568-3576`. Pair with the inversion task (Track 5C) so labels match file-on-disk reality post-flip. Lower-risk to ship independently first. _src/mind_meld/cli.py `_resolve_interactive_loop`, ~20 lines._ (XS) [manual]
- **Pull summary lists conflicted (and failed) files inline** — `_print_pull_summary` cli.py:2197-2250: per-source counts already accumulate paths in `r.outcomes["conflicted"]` / `["failed"]` but never display them. User has to run `mm conflicts` just to learn *which* 6 files conflicted. Fix: when `src_conflicted > 0` (and not quiet), list each path under the per-source line; same for failed. Cap at 20 with `... and N more` overflow. Print path relative to source's base — source name in header already disambiguates. _src/mind_meld/cli.py `_print_pull_summary`, ~10-15 lines._ (XS) [manual]
- **`mm conflicts` table: stop truncating + drop jargon labels** — Screenshot 2026-04-24 shows `kbitz-cl…` / `kbitz-fi…` at typical terminal width; both columns repeat the same `~/.gstack/projects/...` parent. Fix surface (cli.py:3289-3317): (c) rename "Conflict"/"Canonical" labels to "local"/"remote" (matches user's mental model + pairs with inversion); (d) Rich `Table(no_wrap=False, overflow="fold")` so paths wrap rather than truncate. Lowest-risk standalone change is just (c)+(d). Layout rework — collapse `~`, drop one column with separate suffix column — can follow when convenient. _src/mind_meld/cli.py `conflicts()`, ~20-40 lines._ (S) [manual]
- **`mm pull` progress output during download loop** — `cli.py:1074-1112`'s `_download_and_apply` runs silently between the per-device header (cli.py:2390) and the final summary. On first-pull-on-a-new-Mac, `backend.get(bkey)` blocks on iCloud placeholder materialization; kb-mbp 2026-04-24 saw 286 files / 263s wall, no output, visually identical to a hung process. `--verbose` doesn't help — verbose paths fire AFTER each file's blocking read. Fix: Rich `Progress` for TTY (`from rich.progress import Progress`), plain "downloading N/total" counter every K files for non-TTY, no output for `quiet=True` (autopull). Display bytes-transferred — already tracked. _src/mind_meld/cli.py `_download_and_apply` and `_pull_one_source`, ~30 lines._ (S) [manual]

### Track 5C: Conflict default inversion + real-merge backends
_2 tasks · ~3-5 days (human) / ~1.5 hr (CC) · medium-high risk · [merge.py + conflict semantics]_
_touches: src/mind_meld/merge.py, src/mind_meld/cli.py, SPEC.md, CHANGELOG.md, tests/_
_Depends on: Track 5B landing first so the inversion can ride the relabeled prompt copy without a label-mismatch interim state. Highest-risk task in the Group; ships last._

- **Invert conflict-resolution default: keep local at canonical, route remote to `.sync-conflict-*`** — `_apply_conflict` (cli.py:921-964) on `--conflict-mode keep-both` (default) currently renames local → sidecar and writes remote bytes to canonical. Wrong default for two reasons: (1) asymmetric blast radius — local is known-working on this machine, remote is unknown-from-elsewhere; (2) the visible `.sync-conflict-*` file should hold the *surprising* version, not the working one. mtime-skip already handles "local newer," so conflict path only fires when remote is newer or mtimes equal — but "remote newer" ≠ "remote correct for this machine." Surface area: `_apply_conflict` body cli.py:932-963; preflight message cli.py:736; `--conflict-mode keep-both` docstring cli.py:1777; `_prompt_conflict_choice` labels cli.py:1024-1042; tests; SPEC.md if it documents direction; CLAUDE.md "Syncthing convention" mention (the change moves *toward* Syncthing's actual convention). Open question: hard flip (BREAKING) or `--conflict-prefer {local,remote}` flag with default `local`? Strong opinion the flip is correct; weaker on exposing the knob. _src/mind_meld/cli.py + tests + SPEC.md + CHANGELOG; ~80-150 lines including tests._ (M) [manual]
- **`mm resolve`: add real merge so output looks like one machine did all the work** — Today only picks a winner. Auto-merge at pull time (`should_merge`, merge.py:41-44) covers only `.jsonl` (set-union by `ts`) and `MEMORY.md` (line-union); every other text file becomes a sidecar with no merge path. Pragmatic hybrid: per-filetype dispatch in `merge.py` extending `should_merge` — code/JSON/text via `git merge-file` (universally available on macOS, two-way using one side as base since mm doesn't currently store ancestor); prose (`.md` non-MEMORY, `.txt`) via Claude API merge behind explicit `--ai-merge` opt-in flag (project context: this *is* mind-meld syncing memory + notes, prose-heavy); binaries fall back to pick-a-winner. `mm resolve` gains (m)erge option. Surface: merge.py dispatch + git-merge-file wrapper + optional Anthropic backend; `_apply_conflict` tries merge before conflict-rename; `_resolve_interactive_loop` (m) action; config + opt-in; tests per backend; SPEC.md + CHANGELOG. Open: track ancestor (3-way) or stay 2-way? Integrate AI merge or separate `mm merge --ai`? Hard prereq: inversion lands first so "local side" semantics are clear. _src/mind_meld/merge.py + cli.py + optional new module + config + tests + SPEC.md. ~300-500 lines depending on whether AI merge ships v1._ (L) [manual]

---

## Execution Map

Adjacency list (who depends on whom):

```
- Group 1 ← {}     (constants.py preflight, parallel with Group 5)
- Group 2 ← {1}    ✓ Complete (v0.8.7 + v0.8.8)
- Group 3 ← {2}    ✓ Complete (v0.8.10)
- Group 4 ← {}     ✓ Complete (v0.8.11)
- Group 5 ← {}     (in-flight — Tracks 5A → 5B → 5C, serialized in cli.py)
```

In-flight detail:

```
Group 1: Decomposition + DRY
  Pre-flight .............. ~30 min .. constants.py extraction
  └── (Tracks 1A/1B/1C all ✓ Complete)

Group 5: Conflict UX & first-pull polish
  Pre-flight .............. ~5 min ... gstack include_files default add
  ├── Track 5A ........... ~0.5d ..... 3 tasks .. autopull silent-mode (P0) + scope + register-rollback   [ships first]
  ├── Track 5B ........... ~0.5d ..... 4 tasks .. relabel + summary + table + progress                    [ships second]
  └── Track 5C ........... ~3-5d ..... 2 tasks .. invert default + real-merge backends                    [ships last]
```

**Active total: 2 in-flight Groups . 3 tracks . 9 tasks (+ 2 pre-flight items)**
**Shipped: Groups 1 (Tracks A/B/C), 2, 3, 4 — see PROGRESS.md.**

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
