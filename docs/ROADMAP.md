# Roadmap — Phase 1 (v0.x → v1.0)

Organized as **Groups > Tracks > Tasks**. A Group is a wave of PRs that land
together — parallel-safe within, dependency-ordered between. Tracks within a
group must be set-disjoint on `_touches:_` footprints. Pre-flight items are
trivial fixes any agent can pick up.

Items originate from the 2026-04-22 `/full-review` audit, review follow-ups
accumulated across v0.5.1/v0.6.x/v0.7.x/v0.8.x/v0.9.x/v0.10.0, /plan-eng-review
on 2026-04-23, the 2026-04-24 first-pull conflict-UX session, the v0.9.4 Track 5D
adversarial review, the 2026-04-25 v0.9.5 auto-upgrade /plan-ceo-review, and
`docs/designs/fleet-retro.md` (2026-04-27 design doc for v0.11.0).

**Cleanup-sweep set (Groups 1–5) shipped through v0.9.4.** v0.9.5
(auto-upgrade nudge), v0.9.6 (public-readiness scrub), and v0.10.0
(per-machine source toggle) shipped outside the original cleanup-sweep
plan — see PROGRESS.md and CHANGELOG.md. Three new Groups now planned:
**Group 6** (release infrastructure polish), **Group 7** (mm-events
foundation), and **Group 8** (retro-fleet skill — depends on Group 7).

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

## Group 6: Release infrastructure polish
_Depends on: none_

Single-track Group bundling release-discipline polish that pairs with the
v0.9.5 auto-upgrade nudge. Independent of the auto-upgrade code path
(nudge reads `/repos/.../tags`, not `/releases/latest`); ships independently
and doesn't block Group 7 or Group 8.

### Track 6A: GitHub Releases backfill
_1 task . ~30 min (CC) . low risk . no source code changes_
_touches:_

- **Backfill GitHub Releases for ~30 existing tags** -- create a release entry per tag using `gh release create vX.Y.Z --notes-from-tag` (or pulling notes from `CHANGELOG.md`). Unlocks RSS feed, downloadable-asset UX, and release-notes surfacing for users browsing the repo. Does not alter the auto-upgrade code path. _GitHub CLI invocation, 0 source LOC._ (S) [plan-ceo-review]

---

## Group 7: mm-events foundation (event capture)
_Depends on: none_

Implements the foundation for the fleet-aware retro per
`docs/designs/fleet-retro.md`: per-device JSONL event log written on every
push, capturing `mm-push`, `git-snapshot`, and `sessions-snapshot` events.
New `mm-events` source surfaces the events on synced storage; `_push_core`
tail-position write inside a hard time budget; `mm gc` reaps after 90 days
via existing tombstone plumbing. No external API dependency.

**Pre-flight** (shared-infra; serial, one-at-a-time):
- ANSI-escape sanitization on peer-supplied paths -- wrap `rel_path` print sites in `cli.py:917,962,1082,1096` (and Track 5B's pull-summary additions) with `rich.markup.escape()`. A compromised peer could plant escape chars in synced filenames; current rendering passes them through. _src/mind_meld/cli.py, ~6 sites × 1 LOC._ (XS) [plan-ceo-review]
- `walk_generic_source` double-hash dedup -- when an `include_files` entry sits inside an `include_dirs` directory, the file gets hashed twice (last-write-wins on identical bytes; wasted CPU/IO grows with file size and overlap count). Dedup `collected_paths` via `seen: set[Path]` before the hashing loop, mirroring the Track 5D pattern. _src/mind_meld/manifest.py:310, ~5 lines + 1 test._ (XS) [ship adversarial 2026-04-25]
- `_find_conflict_files` case-insensitive dedup -- normalize via `os.path.normcase(str(conflict_path))` on macOS for the dedup key (preserve original `conflict_path` in the emitted hit). Very low likelihood — requires user config with mismatched case AND nested `include_files`. _src/mind_meld/cli.py, ~3 lines._ (XS) [ship adversarial 2026-04-25]
- `register_device` `registered:` timestamp preservation -- fetch-then-write so the iCloud `.icloud` placeholder TOCTOU self-heal path doesn't overwrite first-registration time. The `registered` field semantically means "first registration"; current code regresses to "last registration." _src/mind_meld/devices.py:30, ~5 lines + 1 test._ (XS) [ship adversarial 2026-04-25]
- pyproject.toml hatchling `force-include` for `src/mind_meld/skills` -- ships skill files inside the wheel via package data. Required by Group 8's symlink installer to find files via `importlib.resources`. _pyproject.toml, ~3 lines._ (XS)
- DEFAULT_SOURCES `mm-events` entry -- adds the new mm-owned synced source pointing at `~/.local/share/mind-meld/events/`. Path-existence bootstrap creates the dir (mode 0700, parents=True) before first event write. _src/mind_meld/config.py, ~10 lines._ (XS)

### Track 7A: events.py module
_4 tasks . ~1 day (human) / ~30 min (CC) . medium risk . src/mind_meld/events.py (new)_
_touches: src/mind_meld/events.py, tests/test_events.py_

Pure-logic foundation. Fully isolated from cli.py wiring.

- **`canonicalize_remote_url` table-tested function** -- normalize git remote URLs to `<host>/<org>/<repo>` form: strip scheme, user/auth segment, port, trailing `.git`; lowercase host, preserve path case. Required for `(remote, sha)` dedup across machines. Multi-remote repos: only `origin` captured (v1 limitation); cherry-picked commits counted separately (v1 limitation). _src/mind_meld/events.py, ~30 lines + 5-row table test._ (S)
- **`walk_git_projects` with budget + concurrency** -- discovers `.git/` dirs under configured roots; `ThreadPoolExecutor(max_workers=8)`; per-repo subprocess timeout `max(500, total_budget_remaining // repos_remaining)` capped at 2000ms. Single git command per repo: `git -C <path> log --since=<iso> --numstat -M -C --format='%x1e%H%x09%aI%x09%ae%x09%s'`. Parser handles binary rows (`-\t-\tpath`), rename rows (`old => new` → preserve `new`), empty/merge commits with no numstat rows. Per-repo failures: silent skip + emit one `git_snapshot_skip` line for forensic trail. _src/mind_meld/events.py, ~120 lines + parser tests._ (M)
- **`walk_session_metadata` with Conductor-workspace detection** -- `os.scandir`-based 2-level walk of `~/.claude/projects/*/`. Sets `ephemeral: True` if decoded project path matches `*/conductor/workspaces/*` (matched on the *decoded path string*, NOT path existence — Conductor workspaces are routinely destroyed). Perf target: <500ms for 10k files. _src/mind_meld/events.py, ~60 lines + path-pattern test._ (S)
- **Cursor read/write + write_push_event with mandatory flock** -- `~/.config/mind-meld/event-cursor.json` (mode 0600, fcntl.flock) and per-device daily JSONL (mode 0600, O_APPEND under flock). Lock-order invariant: cursor flock is INNERMOST; release before any other lock acquisition. First-run cursor returns `now - 30d`. _src/mind_meld/events.py, ~50 lines + concurrent-writer test._ (S)

### Track 7B: _push_core wiring + gc retention
_3 tasks . ~0.5 day (human) / ~20 min (CC) . medium risk . src/mind_meld/cli.py_
_touches: src/mind_meld/cli.py, tests/test_integration.py_

Integrates events.py into the push hot path with a hard time budget.

- **`_push_core` tail-position event write** -- after `upgrade.emit_nudge_if_due()`, call `events.write_push_event(...)`. Captures 1× `mm-push` event + N× `git-snapshot` (per-repo) + M× `sessions-snapshot` (per-project). Preserves the v0.9.5 nudge tail-position invariant (events are after the nudge — local file IO doesn't stack with cold-cache HTTP latency). Gated on `not dry_run`. _src/mind_meld/cli.py, ~25 lines._ (S)
- **Time budget: 500ms interactive / 250ms autopush, with silent-skip-on-exceed** -- partial events already written are kept; remaining capture aborts silently. Cursor advanced to `now` regardless (some commits in abort window may be missed; retro is forensic-only — perfect commit accounting requires retry/resume infrastructure that's overkill for v1). Skill output includes "budget abort on <date>" breadcrumb when applicable. Failure handling uses `mm: notice:` (NOT `mm: warning:`) per CLAUDE.md curated taxonomy. _src/mind_meld/cli.py + src/mind_meld/events.py, ~20 lines + budget-exceed test._ (S)
- **`mm gc` 90-day events retention via tombstone reuse** -- gc deletes JSONL files older than 90 days locally; the next push generates deletion tombstones via the existing tombstone-on-absent-file plumbing (which propagates to peers as normal). No new GC layer; consistent with truth-based-manifests invariant. _src/mind_meld/cli.py, ~10 lines + reaping test._ (XS)

---

## Group 8: retro-fleet skill (consumer)

The user-facing surface of the fleet-aware retro (depends on Group 7's
event log existing): a skill shipped in the mm
wheel and symlinked into `~/.claude/skills/` at `mm init`. Reads the synced
event log across all devices, dedups by `(canonical_remote_url, sha)`, and
renders gstack `/retro`-shaped markdown. Skill is acknowledged as an
mm-internal API consumer; schema versioning lives in `events.py` and the
skill's reader. Locked output format owned by mm (not coupled to gstack
evolution).

### Track 8A: SKILL.md + symlink installer + CI smoke test
_4 tasks . ~1.5 days (human) / ~45 min (CC) . high risk . src/mind_meld/skills/retro-fleet/SKILL.md, src/mind_meld/cli.py_
_touches: src/mind_meld/skills/retro-fleet/SKILL.md, src/mind_meld/cli.py, tests/test_skill_link.py_

- **`retro-fleet/SKILL.md` content with locked output format** -- skill file that runs Python (or jq) directly to read mm-owned files. Reads `~/.local/share/mind-meld/events/*-*.jsonl` (mm-owned, schema-versioned) + `~/.gstack/analytics/skill-usage.jsonl` + `~/.gstack/analytics/eureka.jsonl` + `~/.gstack/retros/*.json` (read-only, **schema dependency is load-bearing — reader must tolerate missing fields and never crash if gstack ships a breaking change**). Aggregation: git dedup by `(canonical_remote_url, sha)`; sessions sum across `claude_dir` with ephemeral split; skills counts marked "this machine only". Author filter via `git config --global user.email` + optional `[retro].author_emails` config (NO derived-email persistence — avoids cross-machine "which Mac wins" footgun). Locked output format per design doc. _src/mind_meld/skills/retro-fleet/SKILL.md, ~150 lines._ (M)
- **`_ensure_retro_skill_link` symlink installer** -- two-state op via `importlib.resources.files("mind_meld") / "skills" / "retro-fleet"`. Skip if `~/.claude/skills/` doesn't exist (no Claude Code installed); reuse existing symlink if target matches; emit `mm: notice:` and skip if a non-symlink exists at the target. Hatchling default = unzipped wheels, so `files()` returns a real Path. _src/mind_meld/cli.py, ~30 lines._ (S)
- **Wire installer into `mm init` (always) + `_push_core` head (24h-TTL gated)** -- `~/.config/mind-meld/.skill-link-checked` touch-mtime gate keeps autopush hot path negligible (one `os.stat` on the marker, skip if recent). NOT called from the v0.9.5 transition hook (lock-order rules: NEVER acquire mm lockfile while holding upgrade-state's flock). `pipx reinstall` self-heal via the 24h check on the next push. _src/mind_meld/cli.py, ~15 lines._ (XS)
- **CI smoke test pinning wheel ships skill files + release-checklist manual smoke** -- `assert (importlib.resources.files("mind_meld") / "skills" / "retro-fleet" / "SKILL.md").is_file()` in `tests/test_wheel.py` + one-line addition to release checklist requiring manual verification that Claude Code follows the symlink and loads `retro-fleet`. Catches a future build-backend change to zipped wheels before users do. _tests/test_wheel.py + release checklist, ~15 lines._ (XS)

---

## Execution Map

Adjacency list (who depends on whom):

```
- Group 1 ← {}     ✓ Complete (Tracks A/B/C shipped; constants.py preflight dropped 2026-04-24)
- Group 2 ← {1}    ✓ Complete (v0.8.7 + v0.8.8)
- Group 3 ← {2}    ✓ Complete (v0.8.10)
- Group 4 ← {}     ✓ Complete (v0.8.11)
- Group 5 ← {}     ✓ Complete (5A/5B/5C/5E shipped through v0.9.2 + v0.9.3 hotfix + 5D shipped v0.9.4)
- Group 6 ← {}     active (release infrastructure polish; ships independently)
- Group 7 ← {}     active (mm-events foundation — fleet-retro v0.11.0)
- Group 8 ← {7}    blocked on 7 (retro-fleet skill consumer)
```

Track detail per active group:

```
Group 6: Release infrastructure polish
  +-- Track 6A ........... ~30 min (CC) .. 1 task

Group 7: mm-events foundation
  Pre-flight .............. ~30 min (6 items)
  +-- Track 7A ........... ~30 min (CC) .. 4 tasks .. events.py module
  +-- Track 7B ........... ~20 min (CC) .. 3 tasks .. _push_core wiring + gc

Group 8: retro-fleet skill
  +-- Track 8A ........... ~45 min (CC) .. 4 tasks .. skill + symlink + CI
```

**Active total: 3 in-flight Groups (6, 7, 8). 4 Tracks. 12 tasks + 6 pre-flight items.**
**Shipped: Groups 1, 2, 3, 4, 5 (Tracks 5A + 5B + 5C + 5D + 5E + Group 5 preflight + v0.9.3 hotfix) — see PROGRESS.md.**

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

---

## Unprocessed

Items awaiting triage by /roadmap. Added by other skills or manually.
