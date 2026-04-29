# Roadmap — Phase 1 (v0.x → v1.0)

Organized as **Groups > Tracks > Tasks**. A Group is a wave of PRs that land
together — parallel-safe within, dependency-ordered between. Tracks within a
group must be set-disjoint on `_touches:_` footprints. Pre-flight items are
trivial fixes any agent can pick up.

Items originate from the 2026-04-22 `/full-review` audit, review follow-ups
accumulated across v0.5.1/v0.6.x/v0.7.x/v0.8.x/v0.9.x/v0.10.x/v0.11.x,
/plan-eng-review on 2026-04-23, the 2026-04-24 first-pull conflict-UX
session, the v0.9.4 Track 5D adversarial review, the 2026-04-25 v0.9.5
auto-upgrade /plan-ceo-review, and `docs/archive/fleet-retro.md` (2026-04-27
design doc; archived after v0.11.0 shipped).

**The original v0.x → v1.0 plan is complete.** Groups 1–8 shipped through
v0.11.0. Five releases shipped outside the original cleanup-sweep plan: v0.9.5
(auto-upgrade nudge), v0.9.6 (public-readiness scrub), v0.10.0 (per-machine
source toggle), v0.10.1 (Group 7 preflight — security/concurrency/correctness
sweep + mm-events default source), and v0.11.1 (conflict-prompt UX redesign —
`(b)oth` → `(s)kip` rename, three-number divergence summary, peer-controlled
`device_name` sanitization extension, init-time device-id collision detection,
extracted `safety.py` + `conflictdiff.py` modules). **Group 9** (post-v1.0
pull-performance + fresh-Mac onboarding polish) is the only in-flight work.

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

Surfaced 2026-04-24 from a fresh-Mac first-pull session: 286-file pull on a
fresh Mac landed 6 conflict copies with confusing labels, no inline
filenames, an over-truncated `mm conflicts` table, silence during the
~4-minute download, and a P0 contract regression on `mm autopull` /
`autopush`. Tracks 5A / 5B / 5C / 5E shipped through v0.9.2 (plus a
v0.9.3 hotfix patch adding `config.yaml` to the gstack source's default
`exclude_patterns`). Track 5D shipped as v0.9.4 — closes Group 5.

_Track 5A (Auto-command silent-mode + scope bugs + Group 5 preflight — P0 autopull/autopush silent-mode contract regression, `_synced_scan_dirs` undercount on generic-type sources, `_save_and_register` rollback, gstack `include_files` default add) — ✓ Complete (v0.8.15). 3 tasks shipped + 1 preflight._

_Track 5B (Pull / resolve / conflicts UX surfaces — vocabulary unification `(c)`/`(f)` → `(l)/(r)/(b)/(a)`, inline conflicted filenames in pull summary, `mm conflicts` table column rename + per-column wrap, Rich Progress widget for TTY, quiet-mode contract fix routing per-source conflicts to stderr) — ✓ Complete (v0.9.0). 4 tasks + scope expansions shipped. **BREAKING**: legacy `c`/`f` letters rejected loudly. 14 new tests (700 pass). Track 5E inherited the pre-inversion `.sync-conflict-*` migration subtask (D9 handoff)._

_Track 5C (`exclude_patterns` + log + migration UX — per-source glob list, consumer-boundary filter at `_pull_core` + `_push_core`, tombstone-suppression invariant, `mm log` JSONL writer/reader, `mm migrate-config` command, autopull/autopush missing-excludes breadcrumb, `mm sources` excluded-count column, `mm status` warning) — ✓ Complete (v0.9.1). Pivoted via /plan-ceo-review from "conflict inversion + real-merge backends" after the 2026-04-24 first-pull data showed 24/25 divergent files were per-machine artifacts that excludes prevent. Real-merge backend deferred to Future. Inversion split to Track 5E. 38 tests + 5 IRON RULE pins._

_Track 5E (Conflict default inversion — inverted `_apply_conflict` (canonical = local; remote → sidecar), strict pull-start fleet-version refusal, pre-inversion `v0-` filename migration, dual-mode resolve dispatch by filename prefix, `mm conflicts` Mode column, `mm devices` Version column, `update_last_seen` writes `last_seen_version`) — ✓ Complete (v0.9.2). **BREAKING**: pre-v0.9.2 peers refused at pull start. Added `packaging>=21.0`. 11 tests in `TestInversion5E`. 768 pass._

_v0.9.3 hotfix patch (post-Track-5C): added `config.yaml` to the gstack source's default `exclude_patterns` (gstack's version-check tracking, machine-local). Existing installs need `mm migrate-config` to pick it up._

_Track 5D (Track 5A adversarial-review follow-ups — `_find_conflict_files` tuple-key dedup over the `include_dirs` rglob × `include_files` sibling-glob overlap, `_save_and_register` → `_register_and_save` order swap with best-effort cleanup so a SIGKILL/OOM/power-loss window between writes leaves an inert orphan instead of an inverse half-state, and a new `_ensure_device_registered` push-time self-heal that retroactively fixes any pre-v0.9.4 victims of the v0.8.15..v0.9.3 half-state) — ✓ Complete (v0.9.4). 2 tasks + 1 codex-follow-through self-heal hook. 15 new tests (5 dedup, 7 register/save, 3 self-heal); 789 pass._

---

## Group 6: Release infrastructure polish ✓ Complete
_Depends on: none_

Single-track Group bundling release-discipline polish that pairs with the
v0.9.5 auto-upgrade nudge. Independent of the auto-upgrade code path
(nudge reads `/repos/.../tags`, not `/releases/latest`); ships independently
and doesn't block Group 7 or Group 8.

_Track 6A (GitHub Releases backfill — 36 release entries created via `gh release create`, one per existing tag v0.1.0..v0.10.0; bodies pulled from the matching `## [X.Y.Z]` CHANGELOG section, with v0.9.2 picking up its `— BREAKING` suffix in the title and v0.8.10 falling back to the tagged commit subject since CHANGELOG never carried an entry for it; v0.10.0 marked Latest. Unlocks the RSS feed, release-notes surfacing, and downloadable-asset UX for repo browsers) — ✓ Complete (2026-04-27). 0 source LOC._

---

## Group 7: mm-events foundation (event capture) ✓ Complete
_Depends on: none_

Implements the foundation for the fleet-aware retro per
`docs/archive/fleet-retro.md`: per-device JSONL event log written on every
push, capturing `mm-push`, `git-snapshot`, and `sessions-snapshot` events.
New `mm-events` source surfaces the events on synced storage; `_push_core`
HEAD-position write inside a hard time budget; `mm gc` reaps after 90 days
via existing tombstone plumbing. No external API dependency.

_Group 7 preflight — ✓ Complete (v0.10.1, 2026-04-27). 8 items shipped: `safe_str()`/`safe_text()` peer-controlled string sanitization sweep (~30 print sites), pull-time case-collision detection, `register_device` create-only via `put_exclusive`, `update_last_seen` flock, `walk_generic_source`/`_find_conflict_files` filesystem-identity dedup, `mm-events` default source + `MM_INTERNAL_SOURCE_NAMES` frozenset, `src/mind_meld/skills/` placeholder subpackage. See PROGRESS.md and CHANGELOG.md._

_Track 7A (`events.py` module — `canonicalize_remote_url`, `walk_git_projects` budget+concurrency, `walk_session_metadata` Conductor-ephemeral detection, cursor + write_push_event under flock) — ✓ Complete (v0.10.2, 2026-04-28). 4 tasks shipped._

_Track 7B (`_push_core` wiring + gc retention — `_run_events_tail` HEAD-position with 250ms autopush / 500ms interactive budget plumbed to `walk_session_metadata`'s `deadline_monotonic`, `_gc_old_event_files` reaps by filename date not mtime, fleet retention via tombstone propagation) — ✓ Complete (v0.10.3, 2026-04-28). 3 tasks shipped._

**Hotfix** (post-ship fixes; serial, one-at-a-time):
- **`get_sources` bootstrap fires on every read-only command [pull-perf:source=ship,ts=2026-04-27]** — `_bootstrap_mm_events_path` runs from `get_sources()` (~11 sites including read-only `mm sources` / `mm status` / `mm conflicts` / `mm diff` / `mm log` plus `_get_setup`). Users with chmod-restricted home see `mm: warning:` spam on every invocation (no rate-limiting, no de-dup). Fix: gate bootstrap to mutator commands (push/pull/init/migrate-config), or add once-per-process suppression (`_pull_core` calls `get_sources` twice via `_get_setup`). Pre-filed in CLAUDE.md as known UX rough edge during v0.10.1. _src/mind_meld/config.py, ~10 lines._ (XS)

---

## Group 8: retro-fleet skill (consumer) ✓ Complete

The user-facing surface of the fleet-aware retro: a Claude Code skill
shipped in the mm wheel and symlinked into `~/.claude/skills/` at `mm init`,
reading the synced event log across all devices, dedupping commits by
`(canonical_remote_url, sha)`, picking the latest sessions-snapshot per
`(device, claude_dir)`, and rendering gstack `/retro`-shaped markdown with
locked output format owned by mm.

_Track 8A (SKILL.md + aggregator + symlink installer + CI smoke test) — ✓ Complete (v0.11.0, 2026-04-28). All planned tasks shipped + 6 review-applied scope expansions; ~1100 LOC; 1107/1107 tests pass; 13 review-applied lock-ins from /plan-eng-review. `mm devices --format=json` surface added (stable schema). `EVENTS_SCHEMA_VERSION` bumped 1 → 2 (sessions-snapshot now FULL INVENTORY; latest-per-tuple is honest). Symlink installer 5-branch state machine including dangling-symlink branch (pipx-reinstall recovery). Two-marker 24h-TTL gate (`.skill-link-checked` + `.skill-link-conflict`) with fail-open on EACCES/EIO. Mixed-fleet handling surfaces "Sessions count incomplete: peer X on pre-v0.11.0" instead of overcounting. Full notes in CHANGELOG.md, PROGRESS.md, CLAUDE.md._

**Hotfix** (post-ship fixes; serial, one-at-a-time):
- **Aggregator `mm-events` custom-path notice [ship:source=adversarial,ts=2026-04-28]** — power users with `path: <custom>` on the `mm-events` sync source get a silently-empty retro unless they also set `MM_EVENTS_DIR`. Fix: when `_resolve_events_dir()` returns the default and config has a non-default `mm-events` path, print `mm: notice:` from the aggregator pointing at the env override. _src/mind_meld/skills/retro_fleet/aggregator.py, ~15 lines._ (XS)
- **Aggregator reads entire event corpus into memory before filtering [ship:source=adversarial,ts=2026-04-28]** — `aggregate()` does `events = list(_read_events(...))` upfront then filters per-event. With v=2 full-inventory snapshots × 90-day retention × N machines, file count + per-event size could OOM the skill on a heavily-used fleet (~10MB on kb's 3-Mac fleet today is fine; revisit if memory becomes user-visible). Fix sketch: stream + filter per-file, accumulate only window-matching events. _src/mind_meld/skills/retro_fleet/aggregator.py, ~40 lines._ (S)
- **`walk_session_metadata` v=2 full-inventory may consistently exceed events-tail budget [ship:source=adversarial,ts=2026-04-28]** — pre-v=2's mtime-since-cursor filter dropped untouched projects from the walk; v=2 walks every project every push (cap is wall-clock budget, not work-skipping). Users with 200+ Claude Code project dirs may consistently see `mm: notice: events tail budget exceeded`. Fix: pin a benchmark + bump budget if real fleets blow it, OR reintroduce a fast-path that early-returns when no jsonl mtime advanced past the previous snapshot's `last_session_at` (covers steady-state without losing v=2 correctness). _src/mind_meld/events.py, ~30 lines._ (S)
- **Sessions dedup key drops data on encoded-name collision across multiple Claude source roots [ship:source=adversarial,ts=2026-04-28]** — aggregator dedups by `(device, claude_dir)` where `claude_dir` is just the encoded directory name. If a user configures two `type: claude` source roots and both contain a project encoded as e.g. `-Users-kb-Documents-foo`, one snapshot's data overwrites the other in `latest[(device, claude_dir)]`. Edge case (most users have one Claude root). Fix: include source root path in the snapshot's `claude_dir` or in the dedup key. _src/mind_meld/events.py + src/mind_meld/skills/retro_fleet/aggregator.py, ~25 lines._ (S)
- **Mid-upgrade peer "pre-v0.11.0" breadcrumb persists after the peer upgrades [ship:source=adversarial,ts=2026-04-28]** — peer that emitted v=1 sessions snapshots within the retro window will show in `pre_v2_peers` even after upgrading. Breadcrumb stays accurate (their v=1 snapshots ARE incomplete) but appears misleading once the fleet has finished upgrading. Acceptable v1 behavior; window naturally moves past v=1 snapshots within 7-30 days. Document if dogfood reveals as confusing. _src/mind_meld/skills/retro_fleet/aggregator.py, ~5 lines._ (XS)

---

## Group 9: Pull performance + fresh-Mac onboarding
_Depends on: none_

Surfaced 2026-04-27 from a pull-perf dogfood session on kb's 349C-kb-ms:
670/1449 blobs (46%) were iCloud cloud-only (`st_blocks == 0`); sequential
`backend.get()` measured 509–1050ms per blob, parallel-x8 measured 143ms per
blob — **7.3× speedup**, fully network-bound (File Provider supports concurrent
downloads natively). Push isn't affected (only reads files it just wrote;
always resident). Group covers the parallelization fix plus a paired
onboarding nudge so fresh Macs don't see slow first pulls until iCloud
materializes blobs.

### Track 9A: Parallel blob fetch + brctl pin nudge
_2 tasks . ~0.5 day (human) / ~25 min (CC) . medium risk . src/mind_meld/cli.py_
_touches: src/mind_meld/cli.py, tests/test_integration.py_

- **Parallelize blob fetches in `_download_and_apply` (cli.py:1320)** -- wrap the per-file `backend.get(bkey)` call in `concurrent.futures.ThreadPoolExecutor(max_workers=8)` + `as_completed` (NOT `map` — one slow blob shouldn't gate the rest). Keep decrypt + `_apply_incoming_file` single-threaded (cheap, GIL-friendly, preserves the existing per-file try/except + `outcomes` dict semantics). Care: error/skip outcome ordering under reordering, and the progress bar's `_advance` callback must remain thread-safe (Rich `Progress.advance` is). Measured 7.3× speedup on 1449-blob fresh-Mac pull. _src/mind_meld/cli.py, ~150 lines (executor wrap + outcome-ordering preservation + concurrency test + thread-safety regression pin)._ (M)
- **`mm init` post-success `brctl download` nudge** -- print one-line `mm: notice:` after init success: `Tip: keep blobs local with: brctl download "<storage_path>/data" (or right-click → Keep Downloaded in Finder)`. README "Claude Code Integration" / FAQ section update. Surfaces the iCloud-pinning knob without making decisions for the user. Even with parallelization, a freshly-set-up Mac will see slow first pulls until iCloud materializes blobs. _src/mind_meld/cli.py, ~5 lines + README section._ (XS)

---

## Execution Map

Adjacency list (who depends on whom):

```
- Group 1 ← {}     ✓ Complete (Tracks A/B/C shipped; constants.py preflight dropped 2026-04-24)
- Group 2 ← {1}    ✓ Complete (v0.8.7 + v0.8.8)
- Group 3 ← {2}    ✓ Complete (v0.8.10)
- Group 4 ← {}     ✓ Complete (v0.8.11)
- Group 5 ← {}     ✓ Complete (5A/5B/5C/5E shipped through v0.9.2 + v0.9.3 hotfix + 5D shipped v0.9.4)
- Group 6 ← {}     ✓ Complete (Track 6A — GitHub Releases backfill, 2026-04-27)
- Group 7 ← {}     ✓ Complete (preflight v0.10.1 + Track 7A v0.10.2 + Track 7B v0.10.3)
- Group 8 ← {7}    ✓ Complete (Track 8A v0.11.0)
- Group 9 ← {}     active (pull performance + fresh-Mac onboarding)
```

Track detail per active group:

```
Group 9: Pull performance + fresh-Mac onboarding
  +-- Track 9A ........... ~25 min (CC) .. 2 tasks .. parallel fetch + brctl nudge
```

**Active total: 1 in-flight Group (9). 1 Track. 2 tasks.**
**Original v0.x → v1.0 plan complete: Groups 1–8 shipped through v0.11.0. See PROGRESS.md.**

---

## Future (Phase 2+)

Items triaged but deferred. Not organized into Groups/Tracks.

- **Selective sync (`sync.include` / `sync.exclude`)** — per-project filtering so users with dozens of Claude projects can sync just the 2-3 they actively use across machines. Config schema + glob validation + walker integration + CLI flag surface. _src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/cli.py, ~180 lines._ (M) _Deferred because: no user demand signal yet; revisit on first support case from someone with dozens of projects who wants to sync just 2-3._

- **Mtime hash cache** — push-side perf: skip re-hashing files whose mtime hasn't changed since the last push. Per-device local cache at `~/.config/mind_meld/local-manifest.json` keyed by (mtime, size, sha). _src/mind_meld/cache.py (new), src/mind_meld/manifest.py, src/mind_meld/cli.py, ~210 lines._ (M) _Deferred because: motivating 4-minute-push problem on 1000 files was already solved by crypto v2 (process-scoped master key + HKDF). Revisit only if push latency becomes user-visible again._

- **Three-way merge base (stored last-synced hash)** — pull-side correctness upgrade: per-source, per-file last-synced hash at `~/.config/mind_meld/sync-state.json`. Distinguishes "remote changed, I didn't" from "we both changed" — fast-forward when only one side changed; conflict-copy only when both diverged from base. _src/mind_meld/sync_state.py (new), src/mind_meld/cli.py, ~310 lines._ (M-L) _Deferred because: correctness upgrade, not a fix — current Syncthing conflict-copy pattern works; no divergence-misclassification reports. Revisit if users report "it conflict-copied a file I didn't even touch." Becomes a hard prereq for upgrading Track 5B's `git merge-file` to a 3-way merge._

- **`mm rekey` passphrase rotation** — Format v2 makes `master_key` the rotation boundary but v2 blobs don't carry a `key_scheme` byte. Rotation requires format v3: either re-wrap `master_key` under the new passphrase, or re-encrypt every blob under a freshly-derived `master_key`. Completes the crypto story but requires a format bump and migration path. _src/mind_meld/crypto.py, src/mind_meld/cli.py, SPEC.md, ~200-400 lines._ (M-L) _Deferred because: post-1.0 P3 — requires format v3 and a migration dance; no users blocked pre-1.0._

- **Blob-directory as secondary peer-discovery in corrupt-manifest recovery** — in `_collect_peer_tombstones` (or sibling helper), when a peer's `devices/<id>.json` is corrupt or missing but `data/<id>/` has blobs and `manifests/<id>/*.enc` decrypts, promote the blob-dir-derived `device_id` to the peer list. Recovers tombstones from the otherwise-dropped peer. Widens the trust surface — blob-presence becomes load-bearing evidence of a peer's existence, not just a device-registry entry. _src/mind_meld/cli.py, ~30 lines._ (S) _Deferred because: observation-bar — land when the first real support case appears where corrupt `devices.json` masks a recoverable manifest. v0.8.0's `list_devices` shape-validation + warning is enough until then._

- **PyPI publish workflow** — `.github/workflows/release.yml` that builds + publishes to PyPI on git tag push. Uses `hatchling` build backend (already configured). Currently users install via `pip install -e .` from a local clone; PyPI distribution would let someone `pip install mind-meld` cleanly. Commits to a public package namespace (name squatting, can't easily rename); need to decide on trusted-publisher vs API token auth. Tests-green prereq satisfied by Group 4 (CI shipped v0.8.11). _.github/workflows/release.yml, ~50 lines._ (S) _Deferred because: observation-bar — land when "how do I install this" becomes friction. No user demand signal today._

- **Cross-device source rename drift partitions sync** — Track 2A's type-keyed sync-log fix addressed *same-device* renames. Cross-device, manifests are still keyed by `src_name` (`manifest.py`, `_pull_core`'s `local_sources_map[src_name]` lookup), so if device A renames "claude" → "work-claude" but B keeps "claude", B's pull skips A's manifest via the unknown-source warning path. Codex adversarial 2026-04-24. Fix: cross-device source identity needs to key off `(type, signature)` or similar, not raw name. Bigger design change — likely a follow-up track or a SPEC.md-documented known limitation for v1.0. _src/mind_meld/cli.py, src/mind_meld/manifest.py, SPEC.md, ~100 lines._ (M-L) _Deferred because: no fleet-rename incident yet; documenting as a known limitation is enough for v1.0. Reopen on first support case where two devices use mismatched source names._

- **`mm upgrade-info` (or `mm version --check`) explicit-check command** — Today the auto-upgrade nudge fires once per 24h on autopull/autopush/interactive-pull/interactive-push and `mm status` surfaces cached state. There's no "check NOW" command. If users want one, the cleanest shape is likely `mm status --refresh` (a flag on the existing command) rather than a new top-level command. _Effort: S._ (S) [plan-ceo-review] _Deferred because: original write-up explicitly says "Watch for real demand before designing — ship the baseline, see if `mm status` is enough."_

- **Approach B: subprocess pipx upgrade execution** — v0.9.5 ships nudge-only ("Approach A"). Approach B would add `mm upgrade` running pipx as a subprocess so the user doesn't type the install command themselves. Real complexity: managed-pipx detection (Homebrew / asdf), rollback ambiguity if the install fails partway, TTY detection for interactive Y/n prompts. Process replacement itself is fine (`execvp("pipx", ...)` works); the deferred work is UX + edge cases. _Effort: M._ (M) [plan-ceo-review] _Deferred because: original write-up says "Revisit only after Approach A has been in production for ≥1 release cycle and the printed-command UX feels insufficient."_

- **`MM_NO_VERSION_CHECK=1` env var as alternate CI override** — Redundant with the `--no-check-version` flag. Add only if env-var ergonomics surface as real demand (e.g., a CI hook that wants to set the override once for all mm invocations). _Effort: XS._ (XS) [plan-ceo-review] _Deferred because: original write-up says "Add only if env-var ergonomics surface as real demand."_

- **Pagination beyond 100 tags for `/repos/kbitz/mind-meld/tags`** — `upgrade.py` fetches with `?per_page=100` (max). At current release velocity (30 tags / 6 months) this gives ~3 years of headroom. Past 100 tags, latest detection may miss the highest semver if GitHub's tag sort places older tags on page 1. Revisit when tag count crosses ~80; either add Link-header pagination or trust GitHub's creation-desc default. _Effort: S._ (S) [plan-eng-review] _Deferred because: ~3 years of headroom before the cap matters._

- **`tests/conftest.py::_isolate_devices_write_lock` couples every test to `mind_meld.devices` import** — autouse fixture imports `mind_meld.devices` so it can monkeypatch `DEVICES_WRITE_LOCK`. Couples otherwise-independent tests (`test_wheel.py`, `test_version.py`) to the devices module's import chain. A future bug wedging devices.py at import time would break unrelated tests, masking the real root cause. Forward-defense fix: scope the fixture narrower (`@pytest.fixture` consumed explicitly by tests touching devices), OR move `DEVICES_WRITE_LOCK` to a config-style constants module redirectable without touching devices. _Effort: XS._ (XS) [ship pre-landing review 2026-04-27] _Deferred because: forward-defense; no real-world failure has surfaced from the coupling. Land if a devices.py import bug ever masks unrelated test failures._

---

## Unprocessed

Items awaiting triage by /roadmap. Added by other skills or manually.
