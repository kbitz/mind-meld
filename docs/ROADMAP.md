# Roadmap

State-organized execution plan: **Shipped** / **In Progress** / **Current Plan** / **Future**. Only shipped work has stable IDs; upcoming Groups/Tracks are volatile and re-thought on each /roadmap run. A Group is a wave of PRs that lands together — Tracks within a Group must be set-disjoint on `_touches:_` footprints.

Originating sources for the upcoming plan: 2026-05-10 `/full-review` (25 items) + carried-over Future bullets accumulated through v0.12.3.

---

## Shipped

### Phase 1: v0.x → v1.0 cleanup sweep ✓ Shipped (v0.11.24)

**End-state:** Decomposition + DRY, init flow, test hygiene, release infra, conflict UX, mm-events foundation, retro-fleet skill, post-1.0 polish — all shipped through v0.11.24.
**Groups:** 1, 2, 3, 4, 5, 6, 7, 8, 9, 10

#### Group 1: Decomposition + DRY ✓ Shipped (v0.8.4–v0.8.6)

The proposed `constants.py` extraction preflight was dropped on 2026-04-24 after a `/plan-eng-review` cohesion check: only `FORMAT_VERSION` and `CONFLICT_INFIX` are genuinely cross-module of the four candidates, and even those pair tightly with siblings the original task missed.

- Track 1A — _shipped (v0.8.4): decompose `_pull_core` + `_apply_incoming_file`. 2 tasks shipped._
- Track 1B — _shipped (v0.8.5): walker + manifest + merge DRY. 3 tasks + 1 contract-change cleanup shipped._
- Track 1C — _shipped (v0.8.6): post-1A cli.py follow-ups — diff-call-site DRY, GC blob-shape validation, autopull degraded breadcrumb. 3 tasks shipped._

#### Group 2: Init flow + sync_log generalization + config polish ✓ Shipped (v0.8.7–v0.8.8)

Multi-source assumption lag resolved — `init` no longer hardcodes `~/.claude` and `write_sync_log` is type-keyed instead of name-keyed.

- Track 2A — _shipped (v0.8.7): init decomposition + DEFAULT_SOURCES reuse + sync_log generalization. 5 tasks + refinements shipped._
- Track 2B — _shipped (v0.8.8): config polish — backfill path preservation + ConfigError prefix rename. 2 tasks shipped._

#### Group 3: Test hygiene + style polish ✓ Shipped (v0.8.10)

Pre-flight style polish (type hints, optional syntax, placeholderless f-strings, keyring except-narrowing) + Track 3A test improvements all landed together. Codex `/review` caught a P0 keyring-propagation gap during review.

- Track 3A — _shipped (v0.8.10): CliRunner migration + combined push/pull/conflict E2E + 86 lazy-import hoists. 3 tasks shipped + 3 codex-follow-through pins._

#### Group 4: Release infrastructure ✓ Shipped (v0.8.11)

Single `macos-latest` + Python 3.13 GitHub Actions workflow runs `ruff check`, `ruff format --check`, `pytest tests/`, wheel build + install + `mm --version` smoke, and asserts the real Keychain backend loads.

- Track 4A — _shipped (v0.8.11): GitHub Actions CI + ruff pin + README badge + 113-violation drift sweep. 1 task shipped._

#### Group 5: Conflict UX & first-pull polish ✓ Shipped (v0.8.15–v0.9.4)

Surfaced 2026-04-24 from a fresh-Mac first-pull session. Tracks 5A/5B/5C/5E shipped through v0.9.2 (plus a v0.9.3 hotfix patch); Track 5D shipped as v0.9.4.

- Track 5A — _shipped (v0.8.15): auto-command silent-mode + scope bugs + Group 5 preflight. 3 tasks + 1 preflight._
- Track 5B — _shipped (v0.9.0): pull / resolve / conflicts UX surfaces. 4 tasks + scope expansions. **BREAKING.** 14 new tests._
- Track 5C — _shipped (v0.9.1): exclude_patterns + log + migration UX. 38 tests + 5 IRON RULE pins. Pivoted via /plan-ceo-review from "conflict inversion + real-merge backends."_
- Track 5D — _shipped (v0.9.4): Track 5A adversarial-review follow-ups — `_find_conflict_files` dedup, `_register_and_save` order swap with cleanup, push-time self-heal. 15 new tests._
- Track 5E — _shipped (v0.9.2): conflict default inversion. **BREAKING.** Added `packaging>=21.0`. 11 tests in `TestInversion5E`._
- _v0.9.3 hotfix patch (post-Track-5C): added `config.yaml` to gstack source's default `exclude_patterns`._

#### Group 6: Release infrastructure polish ✓ Shipped (2026-04-27)

Single-track Group bundling release-discipline polish. Independent of the auto-upgrade code path.

- Track 6A — _shipped: GitHub Releases backfill — 36 release entries created via `gh release create`, one per existing tag v0.1.0..v0.10.0. 0 source LOC._

#### Group 7: mm-events foundation (event capture) ✓ Shipped (v0.10.1–v0.10.3)

Implements the foundation for the fleet-aware retro per `docs/archive/fleet-retro.md`: per-device JSONL event log written on every push.

- Group 7 preflight — _shipped (v0.10.1): security/concurrency/correctness sweep + mm-events default source. 8 items shipped._
- Track 7A — _shipped (v0.10.2): events.py module — canonicalize_remote_url, walk_git_projects budget+concurrency, walk_session_metadata Conductor-ephemeral detection, cursor + write_push_event under flock. 4 tasks shipped._
- Track 7B — _shipped (v0.10.3): _push_core wiring + gc retention. 3 tasks shipped._

**Hotfix:** `get_sources` bootstrap fires on every read-only command — _shipped v0.11.2 (2026-04-29). Module-level `_BOOTSTRAP_WARNED_PATHS` short-circuits subsequent calls in same process; pinned by regression test._

#### Group 8: retro-fleet skill (consumer) ✓ Shipped (v0.11.0)

User-facing surface of the fleet-aware retro: a Claude Code skill shipped in the mm wheel and symlinked into `~/.claude/skills/` at `mm init`.

- Track 8A — _shipped (v0.11.0): SKILL.md + aggregator + symlink installer + CI smoke test. ~1100 LOC; 1107/1107 tests pass; 13 review-applied lock-ins from /plan-eng-review._

**Hotfix** (post-ship; serial):
- _Aggregator `mm-events` custom-path notice [adversarial 2026-04-28]: print `mm: notice:` from aggregator when default events dir is empty but config has non-default `mm-events` path._
- _Aggregator memory: streams + filters per-file rather than slurping entire corpus [adversarial 2026-04-28]._
- _walk_session_metadata budget: pin benchmark + bump budget OR reintroduce mtime fast-path [adversarial 2026-04-28]._
- _Sessions dedup key: include source root path to avoid encoded-name collisions across multiple Claude source roots [adversarial 2026-04-28]._
- _Mid-upgrade peer "pre-v0.11.0" breadcrumb persists after upgrade [adversarial 2026-04-28]: window naturally moves past v=1 snapshots within 7-30 days._

#### Group 9: Pull performance + fresh-Mac onboarding ✓ Shipped (v0.11.23)

Surfaced 2026-04-27 from a pull-perf dogfood session on kb's 349C-kb-ms. Scope reduced via /plan-eng-review (2026-05-06) from a 2-task plan paired with 150-line parallel-fetch optimization to a 5-line auto-pin nudge — once storage is pinned, `mm pull` reads resident blobs and is already fast (<5s on 1449-blob workload).

- Track 9A — _shipped (v0.11.23): auto-pin iCloud storage on `mm init` via `brctl download`. 1 task shipped._

#### Group 10: Token-usage post-ship cleanup ✓ Shipped (v0.11.24)

Four DRY + perf items deferred from /ship pre-landing reviews of the v0.11.14+ token-usage work. All scoped to internal hygiene — no public-API change, no user-visible behavior change.

- Track 10A — _shipped (v0.11.24): token-usage DRY + perf polish. Consolidated 4 bucket-merge sites behind `merge_usage_bucket` / `merge_by_model` + `TOKEN_FIELDS` constant + `zero_day_bucket` / `zero_model_bucket` factories. 4 tasks shipped._

---

## In Progress

(none)

---

## Current Plan

Originating from /full-review on 2026-05-10: 8 root-cause clusters → 25 tasks. Three clusters tagged `necessary`; rest tagged `nice-to-have`. Groups 11 + 12 are parallel-safe (disjoint footprints). The chain 13 → 14 → 15 → 16 is a serial cli.py/identity.py/token_usage.py file-collision chain — single-Track Groups serialized are honest about this; trying to parallelize would force file overlap and break the disjoint-footprint rule.

### Group 11: Token-cache + cold-cache correctness fixes

_Depends on: none_

The two `necessary` correctness fixes from /full-review that don't touch cli.py — disjoint footprints, parallel-safe.

#### Track 11A: Token-cache invariant ownership gaps
_2 tasks . ~50 LOC . low risk . tests/conftest.py + src/mind_meld/token_usage.py_
_touches: tests/conftest.py, src/mind_meld/token_usage.py_

- **Add autouse `_isolate_token_cache` fixture** -- mirroring `_isolate_identity_cache` shape; redirects `token_usage.CACHE_PATH` to `tmp_path` and resets `_WARNED_UNKNOWN_MODELS`. Drop the redundant per-file fixture in `test_token_usage.py`. Closes the local-pytest-runs-pollute-real-cache gap. _tests/conftest.py + tests/test_token_usage.py, ~10 lines._ (XS)
- **Refactor `gc_cache_entries` to use `lock_and_get_files`** -- routes the GC cache mutator through the wrapper extracted in v0.11.24 to consolidate cache-shape invariants. Restores the "single owner" claim in `lock_and_get_files`'s docstring. Verify keep/drop replacement of `cache["files"]` survives the refactor. _src/mind_meld/token_usage.py:938-978, ~30 lines._ (S)

#### Track 11B: skills_by_day cold-cache D4 violation
_1 task . ~3 LOC . low risk . src/mind_meld/events.py_
_touches: src/mind_meld/events.py_

- **Always set `meta["skills_by_day"] = {}` regardless of `token_cache_files`** -- drop the conditional gate in `_scan_one_project` that skips the assignment on cold cache. Restores the D4 discriminator (key-absent vs empty-dict) so v0.11.27+ devices on cold cache aren't misclassified as pre-v0.11.27 peers. _src/mind_meld/events.py:848-858, ~3 lines._ (XS)

### Group 12: events-tail/backfill consolidation

_Depends on: none_

Single-Track Group. The 90% structural duplication between `_run_events_tail` and `_run_events_backfill` is the load-bearing finding from /full-review; the gate-timing and re-walk follow-ons land alongside.

#### Track 12A: events-tail/backfill consolidation + push integration
_4 tasks . ~150 LOC (sized at 50% headroom for review-induced expansion) . medium risk . src/mind_meld/cli.py_
_touches: src/mind_meld/cli.py_

- **Extract `_capture_events_snapshot(...)` helper** -- pull out the 90% shared structure between `_run_events_tail` and `_run_events_backfill` (gate, deadline math, claude_paths walk, agg_projects, s_rows). Both call sites assemble own write-list. OR fold backfill behavior into `mode={"tail","backfill"}` parameter on a single function. _src/mind_meld/cli.py:2814-3041, ~80 lines reducible._ (M)
- **Lift token-cache `files_dict` resolution above the duplicated walk loop** -- the two branches under `if do_token_walk` differ only by one arg; loop body is byte-identical. Use `nullcontext(None)` pattern to drop ~10 duplicated lines. _src/mind_meld/cli.py:2877-2894, ~10 lines._ (XS)
- **Substantive-change gate timing** -- gate sees pre-tail manifest; on UTC midnight rollover with zero source changes, no daily mm-push row lands. Verify whether monitoring/retro depends on a daily heartbeat row; either lift gate when cursor >24h stale OR document that no-op pushes don't advance the cursor. _src/mind_meld/cli.py:3104-3174, investigative._ (S)
- **Make `_run_events_tail` return a bool; gate source re-walk on it** -- skip the post-tail re-walk when tail didn't write (or threw). Confirm cost is below noise via wall-clock measurement before adding the signal. _src/mind_meld/cli.py:3180-3191, ~10 lines._ (XS)

### Group 13: cli.py micro-cleanups

The five necessary cli.py polish items from /full-review that can't run in parallel with Group 12's cli.py refactor. Track 13B isolates the upgrade.py dead-fsutil delete so it can land alongside.

#### Track 13A: cli.py polish — helper reuse + dup lookups + dead local imports + marker convention
_5 tasks . ~50 LOC . low risk . src/mind_meld/cli.py_
_touches: src/mind_meld/cli.py_

- **Replace `_download_and_apply` outcomes literal with `_empty_outcomes()`** -- helper exists at line 3647; literal at 1745 is direct duplication. _src/mind_meld/cli.py:1745-1753, ~8 lines._ (XS)
- **Delete redundant local re-imports of `json/secrets/datetime/hashlib`** -- 8 sites; all already imported at module scope. Verify no name shadowing was intended before deleting. _src/mind_meld/cli.py:4432, 4580, 4722, 5922, 7094, 7163, 6344-6345, 880, ~12 lines._ (XS)
- **Extract `_resolve_mm_events_dir(sources) -> Path | None` helper** -- `mm_events_src` lookup duplicated at three sites; `events_dir = Path(mm_events_src["path"]).expanduser() / "events"` also duplicated. _src/mind_meld/cli.py:2837, 2958, 6980, ~15 lines._ (XS)
- **Have `_ensure_retro_skill_link` return a status enum; drop `install_skills_cmd` post-check** -- post-check duplicates ~15 lines of state-machine logic that already exists inside the helper's five-branch state machine. _src/mind_meld/cli.py:5675-5696, ~15 lines._ (XS)
- **Skill-link marker filename convention** -- `.skill-link-checked` / `.skill-link-conflict` mix dot-prefix in non-dot config dir. Either drop the leading dot (consistent with siblings) OR move markers into a `markers/` subdir. _src/mind_meld/cli.py:2557-2706, ~5 lines._ (XS)

#### Track 13B: upgrade.py dead `fsutil` import
_1 task . ~3 LOC . low risk . src/mind_meld/upgrade.py_
_touches: src/mind_meld/upgrade.py_

- **Delete dead `fsutil` import + `_ = fsutil` rebinding** -- comment "kept available for future atomic-write needs" admits no current use; YAGNI. _src/mind_meld/upgrade.py:46, 539-541, ~3 lines._ (XS)

### Group 14: safe_str discipline drift on stderr exception sites

_Depends on: Group 11, Group 13_

Three sites format `{e}` directly without `safe_str(e)` discipline. Even though today's exceptions come from local subprocesses, the visible-failure contract says "always sanitize." events.py is part of the events-tail path that handles peer-rendered strings. Depends on Group 11 (events.py file collision) and Group 13 (cli.py file collision).

#### Track 14A: safe_str hardening at three stderr sites
_3 tasks . ~10 LOC . low risk . src/mind_meld/events.py + src/mind_meld/cli.py + src/mind_meld/config.py_
_touches: src/mind_meld/events.py, src/mind_meld/cli.py, src/mind_meld/config.py_

- **Wrap `walk_git_projects` failure breadcrumb `{e}` with `safe_str`** -- import `safe_str` from `mind_meld.safety`; cli.py:2931 et al. all use this discipline. _src/mind_meld/events.py:562-565, ~3 lines._ (XS)
- **Switch `_ensure_device_registered` to plain `print(..., file=sys.stderr)` + `safe_str(e)`** -- Rich `stderr_console.print(...)` interprets `[red]…[/red]` markup; embedded `{e}` is not safe_str-defended. _src/mind_meld/cli.py:2548-2553, ~5 lines._ (XS)
- **Wrap `_bootstrap_mm_events_path` `{e}` with `safe_str`** -- bring in line with the surrounding contract. _src/mind_meld/config.py:400-407, ~3 lines._ (XS)

### Group 15: Cold-cache wall-clock budget polish

_Depends on: Group 11, Group 14_

The events-tail's 250ms/500ms budget bounds git+sessions walks but several other slow synchronous calls run inside it. Depends on Group 11 (token_usage.py file collision) and Group 14 (cli.py file collision). On cold caches the wall-clock can blow past 10s with a misleading "events tail budget exceeded" notice that is actually the identity gather.

#### Track 15A: Events-tail wall-clock budget collisions on cold caches
_4 tasks . ~100 LOC . medium risk . src/mind_meld/cli.py + src/mind_meld/identity.py + src/mind_meld/token_usage.py_
_touches: src/mind_meld/cli.py, src/mind_meld/identity.py, src/mind_meld/token_usage.py_

- **Pass `allow_refresh=False` from autopush context** -- cold-cache autopush emits empty `local_emails: []`; next interactive push does the refresh. Aggregator's mixed-fleet code already tolerates pre-v0.11.17 peers with no field. _src/mind_meld/cli.py:2916-2923, ~15 lines._ (XS)
- **Fix misleading "events tail budget exceeded" notice on cold-identity-cache pushes** -- move identity gather BEFORE the budget computation, OR exclude identity-gather wall-clock from the budget, OR document the false-positive. _src/mind_meld/cli.py:2916, ~10 lines._ (XS)
- **Add `_FULL_GATHER_BUDGET_S` overall deadline to `_do_full_gather`** -- per-step budgets compound to ~10s; add top-level (e.g. 8s) and short-circuit remaining sources when elapsed. Move `gh api user` BEFORE per-repo loop so slow gh doesn't starve per-repo budget. _src/mind_meld/identity.py:248-261, ~30 lines._ (S)
- **Add per-jsonl deadline check inside `_aggregate_jsonl_views_for_project` merge loop** -- the per-project deadline only fires between projects; one large project can exceed budget. Either deadline-check the merge loop or reduce per-jsonl walk budget when cumulative time elapsed approaches deadline. _src/mind_meld/token_usage.py:879-908, ~20 lines._ (S)

### Group 16: identity.py micro-DRY

Two small DRY gaps remaining from the v0.11.17 identity.py extraction.

#### Track 16A: identity.py micro-DRY
_2 tasks . ~30 LOC . low risk . src/mind_meld/identity.py_
_touches: src/mind_meld/identity.py_

- **Replace `_persist_or_yield_concurrent` and `_persist_force` with one `_persist(emails, *, force: bool)` helper** -- 90% identical; only the "use theirs if fresh" check differs. Saves ~15 lines. _src/mind_meld/identity.py:169-203, ~15 lines._ (S)
- **Have `_do_full_gather` load config once and pass dict to both gather helpers** -- `_gather_per_repo_emails` and `_gather_config_author_emails` independently load config; net 2x load per refresh. _src/mind_meld/identity.py:292-301, 333-340, ~10 lines._ (XS)

### Group 17: Documentation drift around v0.11.17 identity extraction

_Depends on: Group 11, Group 14_

The v0.11.17 split left stale doc references behind, and one parameter that was kept "for API stability" has accumulated `noqa: ARG001` that misleads readers. Depends on Group 11 + Group 14 (both touch events.py); Groups 15 and 16 are unrelated and can land in any order relative to this.

#### Track 17A: Doc drift cleanup
_3 tasks . ~10 LOC . low risk . docs/invariants/events-retro.md + src/mind_meld/skills/retro_fleet/aggregator.py + src/mind_meld/events.py_
_touches: docs/invariants/events-retro.md, src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/events.py_

- **Update events-retro.md dead-name list** -- references `aggregator._read_config_author_emails` / `_per_repo_user_emails` / `_gh_noreply_email` (none exist post-v0.11.17). Rename to current `identity.py` symbols OR remove parenthetical entirely. _docs/invariants/events-retro.md:286, ~3 lines._ (XS)
- **Rename docstring reference at `aggregator.py:2164`** -- `_read_mm_events_config_path` docstring references nonexistent `_read_config_author_emails`. _src/mind_meld/skills/retro_fleet/aggregator.py:2164, ~3 lines._ (XS)
- **Either remove or rename `walk_session_metadata` `since` parameter** -- kept "for API stability" with `# noqa: ARG001`; misleading. Subtraction-first: remove (callers stop computing for sessions walk). OR rename to `_unused_since` per the v=3-future-defense comment. _src/mind_meld/events.py:700-771, ~5 lines._ (XS)

### Execution Map

Adjacency list:
```
- Group 11 ← {}
- Group 12 ← {}
- Group 13 ← {12}
- Group 14 ← {13}
- Group 15 ← {14}
- Group 16 ← {15}
- Group 17 ← {14}
```

Track detail per group:
```
Group 11: Token-cache + cold-cache correctness
  +-- Track 11A ........... ~S . 2 tasks
  +-- Track 11B ........... ~XS . 1 task

Group 12: events-tail/backfill consolidation
  +-- Track 12A ........... ~M . 4 tasks

Group 13: cli.py micro-cleanups
  +-- Track 13A ........... ~S . 5 tasks
  +-- Track 13B ........... ~XS . 1 task

Group 14: safe_str discipline drift
  +-- Track 14A ........... ~XS . 3 tasks

Group 15: Cold-cache wall-clock budget polish
  +-- Track 15A ........... ~M . 4 tasks

Group 16: identity.py micro-DRY
  +-- Track 16A ........... ~S . 2 tasks

Group 17: Doc drift around v0.11.17 identity extraction
  +-- Track 17A ........... ~XS . 3 tasks
```

**Total: 0 phases . 7 groups . 8 tracks . 25 tasks remaining.**

---

## Future

- **Parallel blob fetch in `_download_and_apply`** — wrap per-file `backend.get(bkey)` in `concurrent.futures.ThreadPoolExecutor(max_workers=8)` + `as_completed` (mirror `events.py:walk_git_projects`'s shape). Submit-all-upfront pattern (D2): all N futures live in the executor at once with peak memory ≈ N × avg_enc_blob_size (~70MB on the measured 1449-blob workload, ~500MB at 10k blobs). Measured 7.3× speedup (509ms → 143ms per blob) on a fresh-Mac iCloud-cold pull. _Source: /plan-eng-review 2026-05-06. Deferred because Track 9A's auto-pin prevents the slow-pull case at source. Parallelization only helps users on a non-iCloud storage path or those whose auto-pin was revoked. Revisit on user-reported sustained slow pull (>30s) AFTER auto-pin OR fleet hits 10k+ blobs._

- **Selective sync (`sync.include` / `sync.exclude`)** — per-project filtering so users with dozens of Claude projects can sync just the 2-3 they actively use across machines. _Source: triage. Deferred because no user demand signal yet; revisit on first support case from someone with dozens of projects._

- **Mtime hash cache** — push-side perf: skip re-hashing files whose mtime hasn't changed since the last push. Per-device local cache at `~/.config/mind_meld/local-manifest.json` keyed by (mtime, size, sha). _Source: triage. Deferred because crypto v2 already solved the motivating push-latency problem (process-scoped master key + HKDF). Revisit only if push latency becomes user-visible again._

- **Three-way merge base (stored last-synced hash)** — pull-side correctness upgrade: per-source, per-file last-synced hash. Distinguishes "remote changed, I didn't" from "we both changed" — fast-forward when only one side changed; conflict-copy only when both diverged from base. Hard prereq for upgrading Track 5B's `git merge-file` to a 3-way merge. _Source: triage. Deferred because no divergence-misclassification reports today. Revisit if users report "it conflict-copied a file I didn't even touch."_

- **`mm rekey` passphrase rotation** — Format v2 makes `master_key` the rotation boundary but v2 blobs don't carry a `key_scheme` byte. Rotation requires format v3: either re-wrap `master_key` under the new passphrase, or re-encrypt every blob under a freshly-derived `master_key`. _Source: SPEC. Deferred as post-1.0 P3 — requires format v3 + migration dance; no users blocked pre-1.0._

- **Blob-directory as secondary peer-discovery in corrupt-manifest recovery** — in `_collect_peer_tombstones`, when a peer's `devices/<id>.json` is corrupt or missing but `data/<id>/` has blobs and `manifests/<id>/*.enc` decrypts, promote the blob-dir-derived `device_id` to the peer list. Widens the trust surface — blob-presence becomes load-bearing evidence of a peer's existence. _Source: adversarial. Deferred until first real support case appears where corrupt `devices.json` masks a recoverable manifest._

- **PyPI publish workflow** — `.github/workflows/release.yml` that builds + publishes to PyPI on git tag push. Uses `hatchling` build backend (already configured). Currently users install via `pip install -e .` from a local clone; PyPI distribution would let someone `pip install mind-meld` cleanly. _Source: triage. Deferred until "how do I install this" becomes friction. No user demand signal today._

- **Cross-device source rename drift partitions sync** — Track 2A's type-keyed sync-log fix addressed *same-device* renames. Cross-device, manifests are still keyed by `src_name`, so if device A renames "claude" → "work-claude" but B keeps "claude", B's pull skips A's manifest via the unknown-source warning path. Cross-device source identity needs to key off `(type, signature)` or similar, not raw name. _Source: codex adversarial 2026-04-24. Deferred because no fleet-rename incident yet; documenting as a known limitation for v1.0 is enough._

- **`mm upgrade-info` (or `mm version --check`) explicit-check command** — Today the auto-upgrade nudge fires once per 24h on autopull/autopush/interactive paths and `mm status` surfaces cached state. There's no "check NOW" command. Cleanest shape likely `mm status --refresh` (a flag on the existing command) rather than a new top-level command. _Source: /plan-ceo-review. Deferred — original write-up says "Watch for real demand before designing — ship the baseline, see if `mm status` is enough."_

- **Approach B: subprocess pipx upgrade execution** — v0.9.5 ships nudge-only ("Approach A"). Approach B would add `mm upgrade` running pipx as subprocess so the user doesn't type the install command themselves. Real complexity: managed-pipx detection (Homebrew / asdf), rollback ambiguity if install fails partway, TTY detection for interactive prompts. _Source: /plan-ceo-review. Deferred until Approach A has been in production ≥1 release cycle and printed-command UX feels insufficient._

- **`MM_NO_VERSION_CHECK=1` env var as alternate CI override** — Redundant with the `--no-check-version` flag. _Source: /plan-ceo-review. Deferred — add only if env-var ergonomics surface as real demand (e.g., a CI hook that wants to set the override once for all mm invocations)._

- **Pagination beyond 100 tags for `/repos/kbitz/mind-meld/tags`** — `upgrade.py` fetches with `?per_page=100` (max). At current release velocity (30 tags / 6 months) this gives ~3 years of headroom. Past 100 tags, latest detection may miss the highest semver if GitHub's tag sort places older tags on page 1. _Source: /plan-eng-review. Deferred — ~3 years of headroom before the cap matters._

- **`tests/conftest.py::_isolate_devices_write_lock` couples every test to `mind_meld.devices` import** — autouse fixture imports `mind_meld.devices` so it can monkeypatch `DEVICES_WRITE_LOCK`. Couples otherwise-independent tests (test_wheel.py, test_version.py) to the devices module's import chain. Forward-defense fix: scope the fixture narrower (consumed explicitly by tests touching devices), OR move `DEVICES_WRITE_LOCK` to a config-style constants module. _Source: ship pre-landing review 2026-04-27. Deferred as forward-defense — no real-world failure has surfaced from the coupling._

- **`[retro].deny_emails` subtractive override** — fleet-wide author-email trust set (v0.11.17 `identity.py`) is additive only via union of every peer's `local_emails`. To remove an email (stolen credential, wrong-account commit, deprecated alias) the user must wait for the 90-day events retention to age it out. Add `[retro].deny_emails: list[str]` config knob; aggregator subtracts the denylist after the additive union. Symmetric with the existing `[retro].author_emails` additive knob. _Source: /plan-eng-review (defer-tagged). Deferred — symmetric design ready when demand surfaces (credential leak, account hygiene)._

## Unprocessed

Items awaiting triage by /roadmap. Added by other skills or manually.
