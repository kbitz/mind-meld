# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups/Tracks are volatile and re-thought on each /roadmap run. A Group is a wave of PRs that lands together — Tracks within a Group must be set-disjoint on `_touches:_` footprints.

Originating sources for the upcoming plan: `/full-review` 2026-08-14 (50 findings across 8 clusters, two of them live regressions on shipped features) + the fleet model-mix design from earlier the same day. The fleet-model-mix Groups renumber because the two hotfixes take 13 and 14, and because the audit's SIZE check forced the old Tracks 14A (450 LOC) and 17A (350 LOC) to split.

---

## In Progress

(none)

---

## Current Plan

The remaining plan attacks the thing that has been forcing every recent plan into a single-file queue: `cli.py` is 8,673 lines, 52% of the package, and seven of the sweep's Tracks wanted to touch it. Extracting four cohesive modules out of it converts that serial chain into a genuinely parallel Group, and only after that does the docs re-anchor run — decomposition invalidates every path citation anyway, so re-anchoring first would be wasted work.

#### Group 13: Hotfix: the events tail dies on one bad byte, and nothing says so

_Depends on: none. Nothing depends on Group 13 — see the sequencing note below._

`_read_cwd_from_latest_jsonl` reads text-mode and catches only `OSError`, so an invalid UTF-8 byte raises `UnicodeDecodeError` past `_scan_one_project`, past `walk_session_metadata`, into `_run_events_tail`'s wrapper, and the whole events tail is lost on every push. Reproduced.

**Rewritten 2026-08-14 after `/autoplan` (CEO + Design + Eng + DX, dual voices each, 0 disagreements on any dimension).** Premises were executed rather than assumed; 3 of 5 failed, and 6 defects absent from the original Track were found in the same code. The corrections that changed the shape of this Group:

- **Not silent.** `cli.py:3278` prints `mm: notice: events tail failed: ...` every push. And session jsonls never age out, so the original "until that file ages out" was wrong in the other direction. What IS silent is `mm status`, which reports `success` regardless (see the new task 6).
- **Trigger is narrower than "any bad byte".** `TextIOWrapper` decodes in ~8 KB chunks, so the bug fires when the bad byte falls in the first chunk containing or preceding the first `cwd`. Measured: raises at 2 lines apart, returns cleanly at 80 KB apart. Live corpus on the dev Mac: 37 project dirs, 8 jsonls, 18.2 MB, **zero** invalid UTF-8 bytes.
- **The deadline was the wrong fix.** Measured live cwd scan: 0.82 ms total, 0.06 ms worst project, **0.33% of the 250 ms autopush budget**. The real defect in that function is algorithmic (task 1). Prior learning `measure-before-locking-perf-decisions` (10/10) applied.
- **T3 was cut.** Its premise ("`get_or_compute` returns cached tokens but not cached skills") is false: `token_usage.py:1045-1056` returns both from the same entry, and `events.py:911` destructures one call. The only branch where skills come back `{}` is cold-cache, where `tokens_by_day` is `{}` too and `pre_token_peers` already fires. Omitting the key would also not stop the erasure it flags — latest-snapshot-wins still discards the prior complete snapshot. Superseded by the new snapshot-completeness Track in `## Future`.

Sequencing: the Execution Map has `Group 13 ← {}` and **no Group depends on 13**; Track 17E merely rebases onto it. Group 14 shipped as v0.12.17 and cleared the critical path for Groups 16/17/18.

Footprint note: the new `degraded`-breadcrumb task puts `cli.py` in Track 13A's footprint, which now overlaps Track 14A. That is a rebase, not a collision (the set-disjoint rule binds Tracks *within* a Group), and running Group 14 first makes it one rebase in the direction the dependency graph already wants. Group 16 then decomposes `cli.py`, so 13A must land before 16 or be re-anchored onto `eventstail.py`.

##### Track 13A: Binary-mode session reads + persistent degradation signal
_7 tasks . ~200 LOC . medium risk . events.py hot path + autopush breadcrumb_
_touches: src/mind_meld/events.py, src/mind_meld/token_usage.py, src/mind_meld/pullhistory.py, src/mind_meld/cli.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_events.py, tests/test_token_usage.py, tests/test_silent_failure_contract.py, docs/invariants/events-retro.md_

- **Hoist the cwd call out of the per-file loop** -- `events.py:836` guards on `if cwd is None` INSIDE the per-file loop while the helper takes the project dir, so when no `cwd` exists anywhere the helper re-runs for every jsonl, each time re-doing `iterdir` + N `stat` + a full read of every file. The `# first one wins` comment at `:835` is false in exactly that path. Measured: 20 files → 20 calls → **400 file opens**; synthetic N-scaling gives 15 ms at N=10, 1.44 s at N=100, **13.2 s at N=300 for one project** against a 250 ms budget. Use a `cwd_scanned` flag or hoist above the loop. This is the actual bug the original task 1 was circling. _events.py, ~5 lines._ (XS)
- **Binary-mode `_read_cwd_from_latest_jsonl`, through the existing bounded reader** -- mirror `walk_jsonl_segment`; widen to `(OSError, ValueError)`. Route through `token_usage._iter_bounded_lines` (promote it to a public name) rather than hand-rolling a second binary reader: the naive `open(path, "rb")` + `for line in fp:` reproduces exactly the OOM that `_iter_bounded_lines`' 16 MiB `readline` cap exists to prevent, on the same corpus. **Must land with task 1** — today the raise is what truncates the quadratic walk, so removing it without the hoist ships a latency regression disguised as hardening. _events.py + token_usage.py, ~25 lines._ (S)
- **Sweep the readers that actually matter** -- `events._last_mm_push_ts`, plus two the original sweep missed: `token_usage.is_cache_cold` (`token_usage.py:1454`) raises on a bad byte AND carries a **dead** `except UnicodeDecodeError` arm (`json.loads` on a `str` cannot raise it) which is why it read as handled; it runs on the events-tail path via `_decide_token_walk_policy`, so it kills the tail identically. And `pullhistory._yield_lines` (`pullhistory.py:226`), same shape. `conflictlog.read_records` is **dropped** from the sweep: local-only, never synced, written by `conflictlog.py:60` with `ensure_ascii=True`, file absent on the dev Mac, module marked TEMPORARY with a scheduled rip-out. _events.py + token_usage.py + pullhistory.py, ~30 lines._ (S)
- **Fix the double-collect, THEN clean the dead tuple** -- on budget abort `walk_git_projects` re-collects every completed future: block 1 (`events.py:540-550`) drains `as_completed`, then block 2 (`:551-566`) iterates ALL of `futures.items()` and re-appends every `fut.done()`. Measured with 4 roots / 2 slow / 300 ms: `projects` has 4 rows, 2 unique. Every completed repo's commit list is serialised twice, gzipped, encrypted, uploaded, and replicated to every peer. Track a `collected: set[Future]` and skip it in the timeout handler. **Do NOT extract a shared helper first** — the obvious refactor preserves the double-append and hides it, which is what the original "dedupe the two blocks" task would have done. The two `except` sites are NOT redundant and both stay: the inner guards `fut.result()` per future, the outer guards the pump and is what marks `budget_abort`. Only then delete the redundant tuple in `except (CancelledError, FuturesTimeoutError, Exception)` — both are `Exception` subclasses (note in the commit message that `concurrent.futures.CancelledError` is NOT `asyncio.CancelledError`, which is `BaseException`-derived, and that `FuturesTimeoutError` aliases builtin `TimeoutError` → `OSError`, so never narrow a guard here to `OSError`). _events.py, ~45 lines._ (S)
- **`autopush` writes a `degraded` breadcrumb** -- `cli.py:8636` writes `_write_autorun_breadcrumb("push", "success")` unconditionally, so the exact failure this Track fixes reports as success in `mm status`. `autopull` already solved this at `cli.py:8538-8549` with a `degradations` list and a `degraded` outcome; `autopush` has four outcomes and no `degraded`. `_run_events_tail` returns its reasons (tail failed / budget exceeded / token cache cold), `_push_core` threads them, `autopush` writes `("push", "degraded", "; ".join(reasons))`. **This is the root-cause fix** — CLAUDE.md already documents the argument for the `no-sources` case ("without this, `mm status` only sees `outcome: "success"` forever and monitoring on top of it never catches the wedge") and it was never applied to the events tail. Third instance of "mm degrades quietly and nobody notices" after v0.12.13 and v0.12.15; CHANGELOG v0.12.13 concedes a breadcrumb "fired for four unpriced models across the whole v0.12.x line and nobody saw it". _cli.py, ~40 lines._ (M)
- **Stop the retro refreshing through the worst budget path** -- `skills/retro_fleet/SKILL.md:64` runs `mm autopush` (250 ms budget, and `_decide_token_walk_policy`'s cold-cache branch drops both `tokens_by_day` and `skills_by_day`) immediately before the retro reads that snapshot. `mm push` gets 500 ms and warms the cache. Safe on a non-TTY: `_maybe_prompt_migration` short-circuits to a stderr warning. _SKILL.md, 1 line._ (XS)
- **Regression pins that can actually fail** -- bad-byte pins across every hardened reader, written **bad-byte-first / valid-line-second** (valid-first passes trivially post-fix and proves only "didn't crash"). Plus five the original list missed: cwd-scan **call-count == 1** (catches task 1; the existing `test_pathological_session_walk_no_cwd_anywhere` uses ONE jsonl so N=1 and it structurally cannot see it); oversize-single-line (catches a naive binary port); `_last_mm_push_ts` **cursor preserved**, not merely non-raising (a bare `return None` also doesn't raise, and silently rewinds the cursor 30 days forever); duplicate-projects on budget abort (the existing pin at `test_events.py:358-375` makes ALL repos slow, so `projects == []` and it cannot catch it); and the `degraded` breadcrumb contract in `test_silent_failure_contract.py`. **Dropped:** the deadline pin (pins a parameter, not a bound — it passes whether the check sits before or after `iterdir()`/`sorted()` and whether the helper runs once or 300 times) and the `pre_skills_peers` pin (dies with T3). Full artifact: `~/.gstack/projects/kbitz-mind-meld/kb-kbitz-binary-session-reads-eng-review-test-plan-20260814-213324.md`. _tests + events-retro.md, ~110 lines._ (M)

### Phase 3: cli.py decomposition + correctness sweep

**End-state:** `cli.py` is no longer a 8,673-line bottleneck that serializes every plan; the seams v0.12.14 and v0.12.15 left behind are closed; the test suite stops writing to the developer's real agent config dirs; and the invariant docs route to code that exists.
**Groups:** 15, 16, 17, 18

#### Group 15: Disjoint cleanups that never touch cli.py

_Depends on: none_

Three genuinely parallel Tracks, deliberately scoped to files nothing else in flight owns, so they can land while the hotfixes are in review. Pure deletion — nothing here is additive.

##### Track 15A: token_usage dead type + shim-over-a-shim
_2 tasks . ~100 LOC . low risk . token_usage.py_
_touches: src/mind_meld/token_usage.py, tests/test_token_usage.py_

- **`CacheEntry` is both dead and wrong** -- zero references anywhere, and it omits the v0.12.15 `offset` / `head` / `head_len` / `tail_msg_ids` keys `get_or_compute` actually writes, so `_resume_plan`'s isinstance gauntlet is the only surviving documentation of the on-disk shape. Delete it, or correct it and annotate the entry `get_or_compute` builds so it cannot drift again. _token_usage.py, ~30 lines._ (S)
- **Delete `walk_jsonl_token_buckets`** -- a "backwards-compat shim" for a single-repo CLI with no external consumers; every remaining caller is in the test file, and it is now a shim over a shim. Both this and `CacheEntry` are listed in `token_usage.__all__`, so each deletion is a two-line change — "zero references" is true of call sites, not of the export list. _token_usage.py + tests, ~50 lines._ (S)

##### Track 15B: Dead constants beside their call-time resolvers
_2 tasks . ~50 LOC . low risk . three small modules_
_touches: src/mind_meld/pullhistory.py, src/mind_meld/seen_sources.py, src/mind_meld/upgrade.py_

- **`HISTORY_PATH` and `SEEN_PATH`** -- zero references, and each sits beside the call-time resolver its isolation fixture depends on; a future caller picking the import-time-frozen one silently breaks `_isolate_pullhistory`. Also check whether `_rotated_path()` can stop being production code that exists only for tests. _2 modules, ~30 lines._ (S)
- **`upgrade.py`'s `_ = fsutil` keeper** -- a dead import, plus a statement whose only job is defeating ruff F401, plus a comment explaining why the dead code is there, for a feature (D14) never revisited. _upgrade.py, ~10 lines._ (XS)

##### Track 15C: Aggregator import hygiene
_2 tasks . ~50 LOC . low risk . aggregator.py_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py_

- **Hoist eight function-local imports** -- three import overlapping names (`COST_EXCLUDED_MODELS` twice, `safe_str` three times). Keep only a genuine cycle-breaker, with a comment saying which cycle. _aggregator.py, ~35 lines._ (S)
- **Delete `_import_canonicalize`** -- its stated rationale ("tests can run without the full mind_meld install") died when the module started importing `mind_meld.identity` at top level; it runs on every `aggregate_git` call and its return annotation is the string literal `"callable"`. Check for monkeypatching first. _aggregator.py, ~15 lines._ (XS)

#### Group 16: Break cli.py into cohesive modules

_Depends on: Group 14_

The unblocker. `cli.py` is 8,673 lines — 44% of the 19,697-line package, and just over half of it once the `skills/retro_fleet` subtree is set aside — and the audit's collision rule is file-granular, so every Track that touches it is forbidden from co-Grouping with every other Track that touches it. That single fact is what turned the previous regeneration into fourteen one-Track Groups. Four extractions convert the queue into Group 17's five-way parallel wave.

Sequenced after Group 14 because that hotfix edits the apply path; everything else waits for this Group so there is exactly one rebase, not four.

##### Track 16A: Extract four modules from cli.py
_5 tasks . ~250 LOC review surface (~2,400 lines moved) . medium risk . mechanical movement_
_touches: src/mind_meld/cli.py, src/mind_meld/skilllink.py, src/mind_meld/eventstail.py, src/mind_meld/resolveflow.py, src/mind_meld/gcreap.py, tests/test_skill_link.py, tests/test_init_events_backfill.py, tests/test_conflict_copy.py_

Sizing note: the raw diff is ~2,400 lines, but it is *pure movement* — no logic changes, no behavior changes. The review question is "did anything other than imports change?", which is why the effort labels reflect review burden rather than diff volume.

**Verification, and why the obvious mechanisms do not work here.** `git diff --find-renames` detects whole-blob renames; `cli.py` survives this Track with ~6,200 lines, so rename detection reports nothing and proves nothing. Use `git diff --color-moved=zebra --color-moved-ws=allow-indentation-change`, plus a scripted byte-equality check that pulls each moved function's text from the base revision (`git show origin/main:src/mind_meld/cli.py`) and compares it against the new module.

"Full suite green with no test edits" is worse than useless as a gate — it is actively misleading. ~15 test sites monkeypatch `mind_meld.cli.<name>` for functions this Track moves (`_resolve_retro_skill_src` at `test_skill_link.py:74,392`; `_skill_links_check_due` at `:579`; `_ensure_retro_skill_links` at `test_init_events_backfill.py:234,286`; `_run_events_backfill` at `:231`; `_find_conflict_files` and `_decide_token_walk_policy` elsewhere). Once the real function lives in a new module and `cli` merely re-exports the name, each patch rebinds a dead alias while production resolves its own module global — and the test still passes. Two of those are **no-op stubs for the skill installer**, so a silently-dead patch means `mm init` tests run the real installer against the developer's real `HOME`, reintroducing in this Group precisely the leak Track 17A exists to close.

- **`skilllink.py`** -- the retro-fleet installer family, starting at `_ensure_retro_skill_link` (currently ~L2798-3084): the six `_ensure_*` / `_*_check_due` wrappers, marker constants, `_emit_conflict_notice`, `_resolve_retro_skill_src`, `_config_dir`. ~285 lines. **Do NOT pull in `_ensure_device_registered` (L2749).** It sits immediately above the family and looks adjacent, but it is Track 5D device self-heal, it belongs to the init-devices invariant surface, and `docs/invariants/init-devices.md` routes on its name. _(S)_
- **`eventstail.py`** -- `_enabled_claude_paths`, `_decide_token_walk_policy`, `_run_events_tail`, `_run_events_backfill` (currently ~L3085-3398). ~310 lines. _(S)_
- **`resolveflow.py`** -- conflict discovery and resolution (currently ~L6462-6832 and ~L7155-8038): `_find_conflict_files`, `_migrate_pre_inversion_conflict`, the `_promote_*` family through `_promote_target_will_sync`, `_resolve_interactive_loop`, and the CONFLICT-TELEMETRY helpers that will be deleted wholesale at the rip-out. ~1,250 lines. The `@app.command()` shells (`conflicts`, `resolve`, `recover`, `conflict-log-backfill`) stay in `cli.py`. _(S)_
- **`gcreap.py`** -- `_gc_token_cache`, `_sweep_local_tmp_files`, `_gc_old_event_files`, `_gc_old_conflict_files` (currently ~L5478-5560 and ~L8039-8132). ~180 lines. _(S)_
- **Wire, re-point the monkeypatches, verify** -- `@app.command()` shells stay in `cli.py`; update imports and keep every public name re-exported from `cli` for external callers. Then audit every `monkeypatch.setattr(cli_module, ...)` / `monkeypatch.setattr("mind_meld.cli.X", ...)` site and re-point it at the new owning module — a re-export keeps them *passing* while they patch nothing. Add a pin that fails if an `mm init` / `mm push` test ever reaches the real skill installer, so a dead stub can never again go unnoticed. _cli.py + tests, ~140 lines._ (S)

#### Group 17: Five-way parallel sweep across the new modules

**Gated on Group 16.** Every Track below names a module that does not exist until the decomposition lands — `skilllink.py`, `gcreap.py`, `resolveflow.py`, `eventstail.py`. Do not start these against the pre-split `cli.py`. Everything here was a separate serialized Group before the decomposition; post-split each Track owns a different file, so this is one wave of five PRs.

##### Track 17A: Skill installer correctness + test isolation
_5 tasks . ~180 LOC . medium risk . skilllink.py + conftest_
_touches: src/mind_meld/skilllink.py, tests/conftest.py, tests/test_skill_link.py_

- **One existence predicate across all gates** -- the three self-heal gates use three pre-checks and none matches the installer they gate. Install Codex after mm is already healthy for Claude and you get no link until the Claude success marker ages past `SKILL_LINK_TTL_SECONDS` (24h), because `_skill_links_check_due` is an `OR` across the three gates — so the real symptom is a **≤24h delay, not a permanent failure**. (Two /full-review agents both called this permanent; the adversarial pass caught that they were wrong. The fix is unchanged; the priority is lower.) Align on `agent_dir.exists()`, which `install_skills_cmd` already uses. _skilllink.py, ~40 lines._ (S)
- **Table-driven targets** -- replace six near-identical wrappers, six marker constants, and seven hardcoded path sites with one `_SKILL_TARGETS` tuple, so a fourth agent is a row not three functions. _skilllink.py, ~60 lines reducible._ (S)
- **Third bucket + non-zero exit on partial install** -- a target whose `symlink_to` raised lands in neither `installed` nor `conflicts` and one success returns 0; pre-v0.12.14 the single-target form always hit `Exit(1)`. Add `not_installed`, flatten the four-conditional tail. _skilllink.py, ~35 lines._ (S)
- **`_isolate_skill_links` autouse fixture** -- the installer mkdirs and symlinks into three real user dirs; `pytest` on a dev Mac mutates the developer's real `~/.codex` and `~/.config/opencode` (reproduced). Two traps. **Do NOT swap `_config_dir()` for `config.CONFIG_DIR`**: `_config_dir()` is `Path("~/.config/mind-meld").expanduser()`, re-resolved per call, while `CONFIG_DIR` is `Path.home() / ...` frozen at import — and `CONFIG_PATH`/`LOCK_PATH` derive from it at import, so one setattr does not move them. That is the same import-time-freeze hazard Track 15B is deleting two constants for. **And do NOT implement the fixture as a suite-wide `monkeypatch.setenv("HOME", ...)`**: `importlib.metadata.version("mind-meld")` resolves from the HOME-derived user site-packages, so a moved HOME degrades `__version__` to `0.0.0+dev` and trips `_check_fleet_version_or_refuse` — 12 tests in `test_integration.py` fail. Patch the three target-path resolvers and the marker dir directly. While here, fold in `test_gc_events.py`'s real-lock exposure — conftest's `_redirect_lock` is a plain helper, not autouse, which is why that test grabs the developer's real `mind-meld.lock`. _tests/conftest.py, ~45 lines._ (S)
- **Composition test + `exist_ok` + per-target notices** -- pin the gate×installer composition `test_skill_link.py` currently misses; add `parents=True, exist_ok=True` to the one mkdir of 22 without it; interpolate `safe_str(str(target))` into the three failure notices that don't say which agent failed. _skilllink.py + tests, ~40 lines._ (S)

##### Track 17B: Make every gc reaper honest
_3 tasks . ~100 LOC . low risk . gcreap.py_
_touches: src/mind_meld/gcreap.py, tests/test_gc_events.py_

- **Collapse the two conflict/event reapers onto one loop** -- `_gc_old_conflict_files` only increments `reaped` inside `if not dry_run:`, so `--conflicts --dry-run` always reports "would reap 0"; its ~90%-identical mirror has the `else` branch. Collapse rather than patch the increment so the next divergence can't happen. _gcreap.py, ~50 lines reducible._ (S)
- **`_gc_token_cache` honors `--dry-run`** -- it prints "dry-run; skipping" and reports nothing while every sibling prints `would delete (age Nd): <path>`; its own comment promises the opposite. The reap predicate is pure, so a read-only pass is cheap. _gcreap.py, ~30 lines._ (S)
- **Assert the reaper counts — all three** -- the dry-run test discarded the return value, which is why the always-zero count shipped; do not repeat it for the other two. Pin the conflict-reaper dry-run count, a regression pin that the event-reaper count is unchanged by the collapse, and a first-ever `_gc_token_cache` dry-run test (it currently has **zero** references under `tests/`). Co-locate all three in `test_gc_events.py` alongside the mirror reaper the collapse merges with, rather than leaving them split across two files. _tests, ~45 lines._ (S)

##### Track 17C: Conflict-prompt rendering DRY
_3 tasks . ~100 LOC . low risk . resolveflow.py + conflictdiff.py_
_touches: src/mind_meld/resolveflow.py, src/mind_meld/conflictdiff.py, tests/test_conflictdiff.py, tests/test_conflict_copy.py_

- **Move diff colouring into `conflictdiff.render_diff_lines(diff, cap)`** -- the loop is duplicated verbatim across both prompt sites with silently drifted caps (60 vs 80). This is the leaf-rendering shape the module exists for; the site-level dispatch CLAUDE.md protects is the choice logic, not the colouring. _conflictdiff.py + resolveflow.py, ~40 lines._ (S)
- **Resolve the `b`/`both` alias** -- copy-pasted into both prompts, both comments say "removed at 1.0". Decide: delete now, or one shared `_normalize_conflict_choice`. _resolveflow.py, ~20 lines._ (S)
- **Drop the pointless `else`** -- `if not merge_available: ... continue` followed by an `else:` wrapping 40 lines that only adds an indentation level. _resolveflow.py, ~10 lines._ (XS)
- **Pin the new primitive, and settle the alias** -- every other `conflictdiff` leaf (`render_prompt`, `render_banner`, `count_divergent_lines`) is individually pinned, so `render_diff_lines` ships with a table test including the unified cap. The `b`/`both` alias is already pinned twice in `test_conflict_copy.py` (~:1112 and ~:1132); deleting it is a user-visible keystroke removal, so either update both pins or delete them deliberately and say so in the CHANGELOG. _tests, ~40 lines._ (S)

##### Track 17D: Events-tail consolidation + budget the root discovery
_4 tasks . ~250 LOC . medium risk . eventstail.py_
_touches: src/mind_meld/eventstail.py, tests/test_events_budget_scope.py_

- **Extract `_capture_events_snapshot(...)`** -- pull out the 90% shared structure between `_run_events_tail` and `_run_events_backfill` (gate, deadline math, claude_paths walk, agg_projects, s_rows). The in-code comment admits the deadline-refresh bug was fixed twice, once per copy. _eventstail.py, ~80 lines reducible._ (M)
- **Lift token-cache `files_dict` above the duplicated walk loop** -- the two branches under `if do_token_walk` differ only by one arg; `nullcontext(None)` drops ~10 duplicated lines. _eventstail.py, ~10 lines._ (XS)
- **Budget the root discovery, and stop paying for it twice** -- `discover_git_roots` runs with no wall-clock budget (~107 serial `git rev-parse` spawns on the measured Mac), is invisible to the budget notice because `deadline` is reset after it, and runs a second time from `identity._gather_per_repo_emails` on a cold cache. Memoize first — that alone halves the cold path — but make the memo **call-scoped** (threaded through `_capture_events_snapshot`), not a module-level cache. A module global here is strictly worse than the `_WARNED_UNKNOWN_MODELS` set conftest already resets in `_isolate_token_cache`: the first test to populate it would freeze the git-root list for the whole process and make `test_events*.py` ordering-dependent. If it must be module-level, ship the autouse reset in the same PR. Also delete the dead `deadline` assignment at the head of both walkers, and surface or drop `warm_token_cache_inline`'s discarded `(walked, skipped)` counts. _eventstail.py, ~50 lines._ (S)
- **Substantive-change gate timing** -- the gate sees the pre-tail manifest; on UTC midnight rollover with zero source changes, no daily mm-push row lands. Verify whether monitoring depends on a daily heartbeat row; either lift the gate when the cursor is >24h stale OR document that no-op pushes don't advance it. _eventstail.py, investigative._ (S)

##### Track 17E: What's left in cli.py
_5 tasks . ~250 LOC . low-medium risk . cli.py_
_touches: src/mind_meld/cli.py, src/mind_meld/events.py, src/mind_meld/config.py, tests/test_silent_failure_contract.py, tests/test_safe_str.py_

The `safe_str` sweep reaches two sites outside `cli.py`, so the footprint includes `events.py` and `config.py`. Neither collides inside Group 17 (17A–17D own `skilllink`/`gcreap`/`resolveflow`/`eventstail`), but note Track 13A also edits `events.walk_git_projects` — Group 13 is a hotfix and lands first, so this Track rebases onto it.

- **`safe_str` the two missed peer-controlled print sites** -- `status` prints peer `device_name`/`device_id` raw into a Rich console (which interprets markup and passes escapes through), and `_print_pull_summary` emits `device_name`, `src_name` and `rel_path` unsanitized 30 lines below blocks in the same function that sanitize them. Fold in the three stderr sites promoted here out of Future while the sweep is open: `events.walk_git_projects`, `cli._ensure_device_registered`, `config._bootstrap_mm_events_path`. _cli.py + events.py + config.py, ~30 lines._ (S)
- **Interactive counterpart of `_auto_command_setup`** -- four commands repeat the config/passphrase/lock preamble; the auto pair was migrated, the interactive half never was. _cli.py, ~60 lines reducible._ (S)
- **`_auto_command_tail(verb, refused_outcome)`** -- the `autopull`/`autopush` except-tails differ by one verb and one breadcrumb label; verify `test_silent_failure_contract.py` doesn't pin them separately. Also collapse the `enable_source`/`disable_source` 7-line preamble. _cli.py, ~45 lines._ (S)
- **Thirteen function-local re-imports** -- nine shadowing module-scope `hashlib`/`json`/`secrets`/`datetime`, four re-importing the conflict-filename helpers. Ruff F811 cannot see function-local shadowing, so they survive lint indefinitely. Also rename the `sidecar` loop variable that shadows the module, and call `_empty_outcomes()` at its only caller. _cli.py, ~45 lines._ (S)
- **Two small correctness collapses, both pinned** -- `_push_core` walks `iter_source_diffs` twice per push, once only to compute `has_substantive`; the events re-walk runs between the two calls, so pin that `has_substantive` still flips correctly on a single walk rather than just "confirming" it by eye. `_bootstrap_or_verify_crypto`'s lost-race branch reimplements line-for-line the fall-through its own comment claims it takes. Escape-injection pins for the task-1 sites go in `test_safe_str.py`, mirroring its existing banner tests for peer-controlled `filename` / `device_name`. _cli.py + tests, ~70 lines._ (S)

#### Group 18: Docs drift + invariant re-anchor

Runs last in the sweep on purpose: the decomposition moves nearly every cited symbol, so re-anchoring before Group 16 would be work done twice.

##### Track 18A: Make the routing table land on code that exists
_4 tasks . ~150 LOC . low risk . docs only_
_touches: CLAUDE.md, docs/invariants/events-retro.md, docs/invariants/sync.md, docs/invariants/conflicts.md, docs/PROGRESS.md_

- **Re-anchor the pointer table on the post-decomposition modules** -- every routing row currently names `cli.py:<function>`; after Group 16 the owning module is `skilllink.py` / `eventstail.py` / `resolveflow.py` / `gcreap.py`. Rewrite the table and add the Source Layout entries for the four new modules. _CLAUDE.md, ~50 lines._ (S)
- **Replace line-number citations with function-name citations** -- every pinned line has drifted (`aggregator.py:1862`→~1969, `830`→~814, `cli.py:2886`→3094, `5688`→7407, `1115`→1164, sync.md's `1581`→1834), and the decomposition invalidates the rest. The routing table already uses function names successfully; finish the job so the next refactor doesn't re-break it. _CLAUDE.md + invariants, ~40 lines._ (S)
- **Regenerate the skill-installer invariant section, document the new sources and `fsutil`** -- the section still describes one target, two markers, one check-due function, and a "skills-dir-absent → silent skip" branch that v0.12.14 replaced with dir creation. Nothing anywhere mentions the `codex`/`opencode` `DEFAULT_SOURCES` entries despite `config.py:DEFAULT_SOURCES` being a routed invariant surface, and the Source Layout omits `fsutil.py` — the only module missing — despite it owning the flock-append/atomic-write conventions four modules route through. _events-retro.md + CLAUDE.md, ~45 lines._ (S)
- **Backfill the v0.12.12 PROGRESS row** -- the table jumps 0.12.13 → 0.12.11. Third occurrence after v0.11.24 and v0.11.27, which CLAUDE.md already names, so note whether a `pull_request`-triggered check is the durable fix. _docs/PROGRESS.md, ~15 lines._ (XS)

### Phase 2: Fleet model mix

**End-state:** The retro-fleet card shows Claude / Codex / Grok by token volume and by pull-request count, fleet-wide, with mixed-fleet honesty.
**Groups:** 19, 20, 21, 22, 23, 24

Card rows are host families (not SKUs). OpenCode classifies by model id, not as its own row. Claude tokens stay on the existing `sessions-snapshot`. Codex / Grok Build / OpenCode land in a new `host-usage-snapshot` event. Pull-request identity is unique `#N` from git-snapshot subjects (this fleet's `/ship` squash convention). Host attribution is same-day session ∩ repo, with `unknown` / `mixed` when the signal is weak.

#### Group 19: Card + pull-request totals from existing data

_Depends on: Group 15_

User-visible first slice. No new walkers, no wire change. Sequenced after Group 15 only because Track 15C touches the same file.

##### Track 19A: ASCII card MODELS block + subject pull-request parser
_4 tasks . ~180 LOC . medium risk . aggregator + skill + tests_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py_

- **`extract_pr_numbers(subject)`** -- parse `\(#(\d+)\)`, bare `#(\d+)`, and `Merge pull request #(\d+)`. Dedup by number across remotes in-window. Pin this repo's squash-subject shape (`feat: … (#105)`). _aggregator.py, ~25 lines + tests._ (S)
- **`host_family(model)` in aggregator** -- `claude-*` → claude; `gpt-*` / `o1` / `o3` / `o4-*` / `*codex*` → codex; `grok-*` → grok; else other. OpenCode-hosted `claude-*` counts as claude. Track 23A will switch this import to `host_usage` once that module exists. _aggregator.py, ~20 lines + table tests._ (S)
- **Card + stats line** -- commits, pull-request total, repos, machines. MODELS section rolls Claude `tokens_by_model` by family; hide a host row at 0 tokens and 0 pull requests. `CARD_WIDTH` 64 pin stays green. Rename markdown `## Claude Code activity` → `## Model mix`. _aggregator.py + SKILL.md, ~80 lines._ (M)
- **GitAggregate fields** -- `prs: int`, `prs_by_host: dict[str, int]` (all `unknown` until Track 24A). Snapshot `metrics` grows `prs` additively. _aggregator.py, ~20 lines._ (XS)

#### Group 20: Codex walker + isolated host-token cache

_Depends on: none_

Fully independent of everything above — a new module and a new cache file. Starts immediately, in parallel with the hotfixes. Must not touch `session-tokens.json` (`is_cache_cold` is global and load-bearing).

##### Track 20A: `host_usage.py` foundation + Codex walker
_3 tasks . ~170 LOC . high risk . new module + fixtures_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, tests/fixtures/host_sessions/_

- **`host_family` + DayBucket mapping** -- same classifier as 19A, owned here as the long-run source of truth. Map each host's usage fields onto `TOKEN_FIELDS` (`input` / `cache_create` / `cache_read` / `output`). Reasoning tokens fold into `output`, documented. _host_usage.py, ~40 lines._ (S)
- **Codex walker** -- `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Last `event_msg.payload.type == "token_count"` `total_token_usage` per file is the session total (cumulative stream — summing every event is the bug we must not ship). Model from `turn_context.payload.model`. Incremental by size/mtime. _host_usage.py + redacted fixture, ~80 lines._ (M)
- **Isolated cache** -- `~/.config/mind-meld/host-tokens.json` via `lockedjson`. Deadline gate at the start of each session file. Cold+autopush skip is a CLI policy (Track 22A), not this module. _host_usage.py + tests, ~50 lines._ (S)

#### Group 21: Grok Build + OpenCode walkers

Second half of the walker work, split out because the old single Track was 450 LOC and each parser has substantial unique surface and its own fixture.

##### Track 21A: Grok Build and OpenCode session parsers
_2 tasks . ~130 LOC . high risk . host_usage + fixtures_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, tests/fixtures/host_sessions/_

- **Grok Build walker** -- `~/.grok/sessions/<cwd>/<id>/updates.jsonl` `sessionUpdate: "turn_completed"` only. Ignore `_meta.totalTokens` (context-window size, not billed usage). Skip in-progress sessions with no completed turns. Model from `summary.current_model_id` / `usage.modelUsage`. _host_usage.py + fixture, ~70 lines._ (M)
- **OpenCode walker** -- `~/.local/share/opencode/opencode.db` read-only + busy timeout. Missing or locked DB skips that host, does not raise. Classify by `modelID`, not by app name. _host_usage.py, ~60 lines._ (S)

#### Group 22: `host-usage-snapshot` on the wire

_Depends on: Group 17, Group 21_

New event type, additive on `v=2`, no `EVENTS_SCHEMA_VERSION` bump. D4: skip emits nothing (absence); walked-and-empty emits the row with `token_sources` present. Waits on Group 17 so the new walk grafts onto the consolidated `eventstail.py`, not onto two copies.

##### Track 22A: Emit host-usage-snapshot from tail and backfill
_5 tasks . ~220 LOC . medium risk . events + eventstail + invariants_
_touches: src/mind_meld/events.py, src/mind_meld/eventstail.py, docs/invariants/events-retro.md, tests/test_events.py, tests/test_init_events_backfill.py_

- **TypedDict + writer** -- `HostUsageSnapshot` (`token_sources`, `hosts[family].tokens_by_day`, `hosts[family].active_days`). `active_days` values are canonical remotes, never raw home paths. _events.py, ~40 lines._ (S)
- **Tail / backfill policy** -- after the Claude `walk_done` snapshot (do not move it — v0.12.9), reset a fresh host deadline. Mirror `_decide_token_walk_policy` against `host-tokens.json`. Cold+autopush → no row. `dry_run` → no-op. Forensic `try/except`. Init backfill writes the snapshot (still no mm-push row). _eventstail.py, ~80 lines._ (M)
- **`active_days` at emit** -- cwd → canonical remote via existing git helpers; drop cwd-only sessions from attribution, keep their tokens. _events.py, ~30 lines._ (S)
- **D4 pins** -- skip does not emit; warm-then-cold does not write an empty row that would latest-wins-wipe a prior snapshot. Aggregator fallback to the last present snapshot per device is Track 23A, but the emit side must not create the wipe. _tests/test_events.py, ~40 lines._ (S)
- **Invariant doc** -- host-usage-snapshot, cache isolation, Codex cumulative, Grok `turn_completed`, budget reset, D4 skip. _docs/invariants/events-retro.md, ~30 lines._ (XS)

#### Group 23: Aggregator host-token merge

_Depends on: Group 19, Group 22_

Lights up Codex / Grok card rows after one fleet push. Claude cost line stays isolated so unpriced GPT/Grok volume does not flip it to `>=`.

##### Track 23A: Latest snapshot per device, sibling token map, mixed-fleet breadcrumb
_3 tasks . ~95 LOC . medium risk . aggregator + skill + tests_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py_

- **Latest host snapshot per device** -- fall back to the most recent present `host-usage-snapshot` when the newest push omitted the row. Pin that warm-then-cold does not show empty hosts. _aggregator.py, ~40 lines._ (S)
- **Sibling token map** -- `host_tokens_by_model` so `estimate_cost` still runs only on Claude. Roll up both maps via `host_family` imported from `host_usage`. _aggregator.py, ~30 lines._ (S)
- **`pre_host_peers`** -- key-absence of any host-usage-snapshot for a `v=2` device. Notes: `Host mix incomplete: N peer(s) on pre-host-usage mm OR with cold host-token cache`. Empty `hosts` with `token_sources` present is not flagged. _aggregator.py + SKILL.md, ~25 lines._ (S)

#### Group 24: Pull-request attribution + card fill-in

Final slice.

##### Track 24A: Attribution heuristic, host rows, snapshot metrics
_2 tasks . ~90 LOC . medium risk . aggregator + tests_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, docs/invariants/events-retro.md, tests/test_retro_fleet_aggregator.py_

- **Attribution** -- for each unique `#N`, `pr_day` = max commit date, `pr_repo` = canonical remote. Hosts whose `active_days[pr_day]` contains that remote (Claude: project-day in `tokens_by_day` mapped to the same remote) are candidates. 0 → `unknown`, 1 → that family, 2+ → `mixed`. Notes line names the heuristic. _aggregator.py, ~50 lines._ (S)
- **Card fill-in + snapshot metrics** -- Codex/Grok/unknown/mixed rows; persist per-host token totals additively on the v1 snapshot. Width pins. _aggregator.py + tests, ~40 lines._ (S)

### Execution Map

Adjacency list:
```
- Group 13 ← {}
- Group 14 ← {}
- Group 15 ← {}
- Group 16 ← {14}
- Group 17 ← {16}
- Group 18 ← {17}
- Group 19 ← {15}
- Group 20 ← {}
- Group 21 ← {20}
- Group 22 ← {17,21}
- Group 23 ← {19,22}
- Group 24 ← {23}
```

Track detail per group:
```
Group 13: Hotfix: events tail dies on one bad byte (run AFTER Group 14)
  +-- Track 13A ........... ~M . 7 tasks

Group 14: Hotfix: codex/opencode push what they cannot pull
  +-- Track 14A ........... ~M . 4 tasks

Group 15: Disjoint cleanups that never touch cli.py
  +-- Track 15A ........... ~S . 2 tasks
  +-- Track 15B ........... ~S . 2 tasks
  +-- Track 15C ........... ~S . 2 tasks

Group 16: Break cli.py into cohesive modules
  +-- Track 16A ........... ~M . 5 tasks

Group 17: Five-way parallel sweep across the new modules
  +-- Track 17A ........... ~M . 5 tasks
  +-- Track 17B ........... ~S . 3 tasks
  +-- Track 17C ........... ~S . 3 tasks
  +-- Track 17D ........... ~M . 4 tasks
  +-- Track 17E ........... ~M . 5 tasks

Group 18: Docs drift + invariant re-anchor
  +-- Track 18A ........... ~S . 4 tasks

Group 19: Card + pull-request totals
  +-- Track 19A ........... ~M . 4 tasks

Group 20: Codex walker + isolated cache
  +-- Track 20A ........... ~M . 3 tasks

Group 21: Grok + OpenCode walkers
  +-- Track 21A ........... ~M . 2 tasks

Group 22: host-usage-snapshot wire
  +-- Track 22A ........... ~M . 5 tasks

Group 23: Aggregator host merge
  +-- Track 23A ........... ~S . 3 tasks

Group 24: Pull-request attribution + card
  +-- Track 24A ........... ~S . 2 tasks
```

**Total: 2 phases . 12 groups . 18 tracks remaining.**

---

## Future

- **cli.py micro-cleanups (old 14A/14B)** — `_resolve_mm_events_dir`, skill-link status enum, marker filename convention. _Source: /full-review 2026-05-10. Reduced: `_empty_outcomes` reuse and the dead local re-imports were promoted into Track 17E; the `upgrade.py` `fsutil` import into Track 15B._
- **`_resolve_interactive_loop` decomposition** — 630 lines / 74 branch nodes, the largest function in the repo; four separable phases plus interleaved telemetry. _Source: /full-review 2026-08-14. Deferred on a real dependency, not a punt: the CONFLICT-TELEMETRY row construction is woven through it and disappears on its own at the rip-out, so doing this now means doing it twice. Trigger: after the collector is removed._
- **`merge.similarity_ratio` shares `lcs_merge`'s split preamble** — the docstring hand-enforces "MUST match exactly" where a shared helper would make drift impossible. _Source: /full-review 2026-08-14. Same trigger — the function is scheduled to die with the collector, so deleting it later is cheaper than extracting a helper now._
- **Two-machine test bootstrap duplicated across two modules** — `tests/test_pull_result.py` and `tests/test_silent_failure_contract.py` carry the same 12-line block; conftest already owns the single-machine `_setup_real_config`. _Source: /full-review 2026-08-14._
- **Cold-cache budget leftovers (old 16A remainder)** — `allow_refresh=False` on autopush; `_FULL_GATHER_BUDGET_S` on identity gather; per-jsonl deadline in the token merge loop. _Source: /full-review + v0.12.9. The unbudgeted `discover_git_roots` half was promoted into Track 17D._
- **identity.py micro-DRY + token-cache test pins (old 17A/B/C)** — unify `_persist`; load config once in `_do_full_gather`; `gc_cache_entries` `max_age_s=0`; positive cache-isolation test. _Source: Track 11A eng-review._
- **v0.11.17 doc-drift cleanup (old 18A)** — events-retro dead-name list; aggregator docstring; `walk_session_metadata(since)` unused param. _Source: /full-review._
- **Incremental-resume accepted divergences** — tool_use id not seeded across segments; final line without trailing newline never counted. Evidence-triggered only (census). _Verified 2026-08-14: `seen_tool_ids` still initialises empty at `token_usage.py:702`; both divergences stand. Source: [review] inbox._
- **Rip out CONFLICT-TELEMETRY collector** — after Phase 2 bands validate (≥25 real decisions or 60 days). _Verified 2026-08-14: the collector shipped v0.12.12 on 2026-07-30 (15 days ago) and `~/.config/mind-meld/conflict-decisions.jsonl` does not exist on this Mac — **zero decisions collected**. The ≥25-decision trigger is not tracking; only the 60-day bar (~2026-09-28) will fire, and it will fire with no dataset. Worth deciding then whether the similarity classifier below should be killed rather than deferred. Source: [plan-eng-review] inbox._
- **Future-clamped peer mtime can mislead `(n)ewer`** — advisory watch. _Verified 2026-08-14: the `_restore_mtime_best_effort` clamp is intact. Source: [plan-eng-review] inbox._
- **`_promote_target_will_sync` ignores `exclude_patterns`** — rare exclude-glob miss. _Verified 2026-08-14: still present in `cli.py` as `_promote_target_will_sync` (the inbox cited a line number that had already drifted — the citation-drift fix is Track 18A). Note Group 16 moves this function into `resolveflow.py`. Source: [review] PR #97._
- **Phase 2 similarity classifier + silent merge** — blocked on collector data. _Verified 2026-08-14: no `classify_divergence` / `DivergenceClass` anywhere in src. See the collector note above — the blocking dataset is not materialising. Source: [plan-eng-review] inbox._
- **Peers we never resolved against can be mtime-skipped by the drain** — watch now that 12A shipped. _Verified 2026-08-14: drain machinery present at `cli.py:1545`. Source: [plan-eng-review] inbox._
- **Abort transactionality** — pre-existing torn-state. _Verified 2026-08-14: `typer.Abort()` still propagates out of `_pull_core`'s try block. Source: [review] inbox._
- **Price cache writes per-TTL (5m vs 1h)** — wire-format change; competes with host-usage on `token_usage.py`. _Verified 2026-08-14: `_CACHE_WRITE_MULT = 2.0` is still a single constant and `parse_usage` still reads only `cache_creation_input_tokens`. Source: [plan-eng-review] inbox._
- **`test_gc_events.py` touches the real mind-meld.lock** — flake against autopush. _Verified 2026-08-14: the test still runs `CliRunner` without conftest's `_redirect_lock`, which is a plain helper rather than an autouse fixture. Pairs naturally with Track 17A's `_isolate_skill_links` work, whose last task already carries the reciprocal pointer. Source: [review] inbox._
- **Model-id variant suffixes alias onto base model** — no real variant ids in census. _Verified 2026-08-14: `parts[2:4]` at `aggregator.py:1455`, `parts[1]` at `token_usage.py:534`. Source: [review] inbox._
- **No sanity ceiling on rendered cost figure** — presentation-layer. _Verified 2026-08-14: `_MAX_SAFE_TOKENS` clamps per field, nothing bounds the rendered total. Source: [review] inbox._
- **Parallel blob fetch in `_download_and_apply`** — wrap per-file `backend.get(bkey)` in `concurrent.futures.ThreadPoolExecutor(max_workers=8)` + `as_completed` (mirror `events.py:walk_git_projects`'s shape). Submit-all-upfront pattern (D2): all N futures live in the executor at once with peak memory ≈ N × avg_enc_blob_size (~70MB on the measured 1449-blob workload, ~500MB at 10k blobs). Measured 7.3× speedup (509ms → 143ms per blob) on a fresh-Mac iCloud-cold pull. _Source: /plan-eng-review 2026-05-06. Deferred because Track 9A's auto-pin prevents the slow-pull case at source. Revisit on user-reported sustained slow pull (>30s) AFTER auto-pin OR fleet hits 10k+ blobs._

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

- **Snapshot-level completeness, and never let an incomplete snapshot replace a complete one** — _supersedes the former "skills-walk-complete signal as explicit schema field" item; promoted from Future to a named Track candidate by `/autoplan` 2026-08-14, where all four voices across three phases converged on it independently._ Completeness is a property of a **scan**, not of one key. Add a snapshot-level marker (`truncated: bool` / `completed_projects` / `total_projects`) on the v=2 sessions-snapshot (additive, total=False, no schema bump), and change `aggregate_sessions` so the latest **complete** snapshot per `(device, source_root, claude_dir)` wins for aggregation rather than the latest snapshot outright. Three things fall out that nothing else can deliver: (a) key-absence stops accumulating meanings — it currently encodes pre-v0.11.27 peer OR cold cache OR lock contention, each with a different correct action, and one bit cannot carry four states; (b) the erasure stops — demonstrated during review, a warm push carrying 8 real invocations followed by a truncated push loses all 8 under latest-wins, and the rejected T3 "omit the key" variant only converts that from silent to flagged, never prevents it; (c) **per-project containment in `walk_session_metadata` becomes implementable** — `SessionsSnapshot` has no `skipped` field (unlike `GitSnapshot`, `events.py:139-146`), so a `try/except` around `_scan_one_project` today would convert a loud whole-walk failure into a silent per-project one. Also in scope here: `walk_session_metadata`'s `except OSError` returns `[snapshot]` with `projects` still `[]`, discarding every project already scanned (`events.py:757` vs `:772-775`); and the `ephemeral`-from-encoded-name question, which needs a stated false-positive policy first because `/a/conductor/workspaces/x` and `/a-conductor/workspaces/x` encode identically. Do NOT reintroduce the rejected 3-LOC fix (dropping `if token_cache_files is not None:` at `events.py:_scan_one_project`) — it causes latest-snapshot-wins erasure on warm-then-cold ordering. See CHANGELOG v0.12.4, `docs/invariants/events-retro.md`, and the /autoplan artifact `~/.gstack/projects/kbitz-mind-meld/kb-kbitz-binary-session-reads-plan-20260814-213324.md`.

- **Retro card diagnostics that name the machine and the cause** — the two "incomplete" Notes lines render `{n} peer(s)`, a bare count, while `SKILL.md:232` and `:238` both claim "the Notes section names them". `FleetAggregate.devices_known_list` already carries the id→name map. Worse, `aggregator.py:1932`'s single-device fallback prints "No fleet device has shipped skill data yet — upgrade peers to v0.11.27+", which is indefensible on a current single-Mac install, and `aggregator.py:1972` names causes that cannot apply to a truncated push. Separately, `aggregate_git` never reads the git snapshot's `skipped` array, so `budget_abort` repos silently deflate the card's **headline** commit count with no signal on any surface. _Source: /autoplan DX phase 2026-08-14. Deferred out of Track 13A because `aggregator.py` collides with Track 15C — sequence, don't co-Group._

- **`mm diag` is undiscoverable; `mm status` should carry the analytics line** — `mm diag` exists at `cli.py:5176` with `--json` and a secrets allowlist, and appears in neither CLAUDE.md's command list nor the README. Do NOT add `mm doctor` (both /autoplan DX voices rejected it — a third health-ish command fragments discovery). Instead: document `diag`, extend `_collect_diag_state` with an events block (`token_cache_cold`, cache entry count, per-device events-file mtime, unpriced models), and let Track 13A's `degraded` breadcrumb carry the one line that belongs in `mm status`. _Source: /autoplan DX phase 2026-08-14._

- **Diagnostic string quality pass** — five `mm: notice:` sites name the symptom and stop: `cli.py:3276` ("events tail budget exceeded" — doesn't say a partial snapshot was still published), `cli.py:3278` (exception class as user-facing text), `events.py:569` (leaks a private function name, and is the only notice in that file not run through `safe_str`), plus the two aggregator strings above. Rule to adopt: problem + consequence + next command, or don't emit. _Source: /autoplan DX phase 2026-08-14._

- **`last-autorun.json` is shared by pull and push, so a `degraded` push breadcrumb is overwritten by the next autopull** — v0.12.16 made `autopush` write `degraded` when the events tail loses data, but both auto commands write the same single sidecar (`_write_autorun_breadcrumb` at `cli.py`), and `mm status` renders whichever ran last. A degraded push followed by a routine autopull (or by a no-op push, which returns `None` and writes `success`) hides the signal before the user sees it. The fix is per-verb breadcrumb state, or a separate sticky events-tail record cleared only when the tail itself succeeds. _Source: /review Codex adversarial 2026-08-14. Deferred — the breadcrumb is a real improvement over the unconditional `success` it replaced; this is the next increment, not a reason to hold it._

- **Git discovery errors and per-repo `skipped` entries never reach the degradations list** — `walk_git_projects` folds per-repo timeouts and failures into the snapshot's `skipped` array and catches whole-walk exceptions internally; `discover_git_roots` returns `errs`. `_run_events_tail` treats all of it as success, so a push where every repo timed out still reports healthy. The data is already on the wire — this is a matter of inspecting `errs` and each snapshot's `skipped` and appending. Note `aggregate_git` also never reads `skipped`, so budget-aborted repos silently deflate the retro card's headline commit count (tracked separately under the retro-diagnostics item above). _Source: /review Codex adversarial 2026-08-14._

- **`write_push_event` does not enforce CT-4; `flock_append_jsonl` ignores short writes** — the mm-push-last ordering invariant is documented at `events.py:989` and trusted across every caller rather than enforced at the writer. `fsutil.flock_append_jsonl` concatenates the batch into one `os.write` without looping on short writes or checking the returned byte count, so a short write can leave a torn row. _Source: /autoplan eng phase (Codex) 2026-08-14. Deferred — real gaps, but not hotfix scope._

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

### Group 11: Token-cache + cold-cache correctness fixes ✓ Shipped (v0.12.4–v0.12.5)

The two `necessary` correctness fixes from /full-review 2026-05-10. Loose Group — post-1.0, outside the v0.x → v1.0 sweep above.

- Track 11A — _shipped (v0.12.5): token-cache invariant ownership consolidation — autouse `_isolate_token_cache` fixture + `gc_cache_entries` routed through `lock_and_get_files`. Cross-model HIGH (unknown-top-level-key-stripping regression) caught and fixed during /review. 5 new pinning tests._
- Track 11B — _shipped (v0.12.4): cosmetic-only "Skills incomplete" breadcrumb admits cold-cache push as a second cause. Original Option B (3-LOC `events.py` fix) rejected during /plan-eng-review — would cause latest-snapshot-wins data erasure on warm-then-cold push ordering._

### Group 12: inline keep-canonical mtime bump ✓ Shipped (v0.12.7)

Deferred the inline `keep-canonical` mtime bump to end-of-pull-batch so `mm pull --conflict-mode prompt` choosing (l)ocal propagates across the fleet without mid-walk later-peer skip.

- Track 12A — _shipped (v0.12.7): `pending_inline_bumps` drained after all peer walks. 13 tests in `TestResolveLocalMtimeBump`._

### Group 14: Symlink policy on both push and apply paths ✓ Shipped (v0.12.17)

Child symlinks are now local routing rather than sync content: generic walkers omit them, pull preserves live and dangling local links, and source roots may still be symlinked. Prior-manifest filtering prevents this policy change from generating deletion tombstones; generated Codex/OpenCode skill trees are excluded without excluding hand-authored skills.

- Track 14A — _shipped (v0.12.17): tombstone-safe child-symlink suppression, pull-time link preservation, generated-skill exclusions, invariant documentation, and regression coverage._
