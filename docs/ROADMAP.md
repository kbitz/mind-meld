# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups/Tracks are volatile and re-thought on each /roadmap run. A Group is a wave of PRs that lands together — Tracks within a Group must be set-disjoint on `_touches:_` footprints.

Originating sources for the upcoming plan: `/full-review` 2026-08-14 (50 findings across 8 clusters, two of them live regressions on shipped features) + the fleet model-mix design from earlier the same day. The fleet-model-mix Groups renumber because the two hotfixes take 13 and 14, and because the audit's SIZE check forced the old Tracks 14A (450 LOC) and 17A (350 LOC) to split.

---

## In Progress

(none)

---

## Current Plan

Two hotfixes ship first — both are regressions on shipped behavior, not deferred scope. Then the plan attacks the thing that has been forcing every recent plan into a single-file queue: `cli.py` is 8,673 lines, 52% of the package, and seven of the sweep's Tracks wanted to touch it. Extracting four cohesive modules out of it converts that serial chain into a genuinely parallel Group, and only after that does the docs re-anchor run — decomposition invalidates every path citation anyway, so re-anchoring first would be wasted work.

#### Group 13: Hotfix: the events tail dies on one bad byte

_Depends on: none_

`_read_cwd_from_latest_jsonl` reads text-mode and catches only `OSError`, so one invalid UTF-8 byte in any session jsonl raises `UnicodeDecodeError` past `_scan_one_project`, past `walk_session_metadata`, into `_run_events_tail`'s forensic wrapper — and the entire events tail is silently lost on every push until that file ages out. Reproduced. This is the exact bug class v0.12.15 fixed in `walk_jsonl_segment`; the fix reached one of the two readers of the same corpus. Every downstream Group reads that pipeline, so it gets fixed before more is built on it.

##### Track 13A: Binary-mode session reads + honest truncation signalling
_5 tasks . ~180 LOC . medium-high risk . events.py hot path_
_touches: src/mind_meld/events.py, src/mind_meld/conflictlog.py, tests/test_events.py, docs/invariants/events-retro.md_

- **Binary-mode `_read_cwd_from_latest_jsonl`** -- mirror `walk_jsonl_segment`'s binary read; widen the guard to `(OSError, ValueError)`. _events.py, ~25 lines._ (S)
- **Sweep the sibling text-mode readers** -- `events._last_mm_push_ts` and `conflictlog.read_records` share the text-mode/`OSError`-only shape; fix in the same pass so the third reader doesn't bite next quarter. _events.py + conflictlog.py, ~20 lines._ (XS)
- **Omit `skills_by_day` / `tokens_by_day` on a deadline-truncated project** -- today `_scan_one_project` writes `skills_by_day = {}`, and the D4 discriminator reads key-present-empty as the content signal "no Skill usage", so a truncated autopush renders a confident "0 skill invocations". Absence is already the breadcrumb; use it. Do NOT add a `skills_walk_complete` wire field — Future already holds that rejected design. _events.py, ~30 lines._ (S)
- **While we're in this file: two dead-code items** -- both narrow types in `except (CancelledError, FuturesTimeoutError, Exception)` are `Exception` subclasses, and `walk_git_projects`' future-result block is written twice with drifting except clauses. Riding along beats a second PR against the same hot path. _events.py, ~40 lines._ (S)
- **Regression pins + invariant doc** -- a jsonl with one bad byte does not kill the tail; a deadline-truncated walk raises `pre_skills_peers` instead of reporting zero. Document deadline exhaustion as the third `pre_skills_peers` population (the doc currently enumerates exactly two). _tests + events-retro.md, ~65 lines._ (S)

#### Group 14: Hotfix: codex/opencode sources push what they cannot pull

_Depends on: none_

v0.12.14 added `codex` / `opencode` default sources with `AGENTS.md` in `include_files` — the exact file host-config tooling symlinks. The walker follows the symlink and pushes; the escape guard resolves outside `base_path` and rejects it on **every** pull, forever, counting it in `outcomes["failed"]` and tripping the autopull `total_failed` warning. Reproduced end-to-end. Compounding it, `atomic_write_bytes`' `os.replace` acts on the link rather than the target, so a locally-symlinked synced path is silently replaced by a regular file — and mm's own skill installer plants symlinks in `~/.codex/skills`, which is now inside the sync surface.

##### Track 14A: Symlink policy on both the push and apply paths
_4 tasks . ~150 LOC . high risk . push walker + pull apply path_
_touches: src/mind_meld/manifest.py, src/mind_meld/cli.py, src/mind_meld/config.py, tests/test_conflict_copy.py, tests/test_pull_helpers.py_

- **Skip symlinked entries in `walk_generic_source`** -- subtraction-first: never publish a file the apply path structurally cannot write. Preferred over a symlink carve-out in the escape guard, which would weaken the v0.11.21 traversal defense. _manifest.py, ~30 lines._ (S)
- **`_apply_incoming_file` refuses on a local symlink** -- treat "peer bytes vs. a local symlink" as a conflict with a breadcrumb, not a write. Covers the dangling-symlink case too, where `exists()` reports absent and `_apply_write` fires. _cli.py, ~30 lines._ (S)
- **Narrow the new sources' include surface** -- `skills/`, `commands/`, `agents/` are written by `bin/apply` and gstack `./setup --host auto` per-machine; give the codex/opencode entries the `exclude_patterns` the gstack source already needed, or narrow `include_dirs` to hand-authored surfaces. _config.py, ~20 lines._ (S)
- **Regression pins** -- symlinked `AGENTS.md` never enters the manifest; a local symlink survives a pull that would have clobbered it. _tests, ~70 lines._ (M)

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
- **Delete `walk_jsonl_token_buckets`** -- a "backwards-compat shim" for a single-repo CLI with no external consumers; every remaining caller is in the test file, and it is now a shim over a shim. _token_usage.py + tests, ~50 lines._ (S)

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

The unblocker. `cli.py` is 8,673 lines — 52% of `src/mind_meld` — and the audit's collision rule is file-granular, so every Track that touches it is forbidden from co-Grouping with every other Track that touches it. That single fact is what turned the previous regeneration into fourteen one-Track Groups. Four extractions convert the queue into Group 17's five-way parallel wave.

Sequenced after Group 14 because that hotfix edits the apply path; everything else waits for this Group so there is exactly one rebase, not four.

##### Track 16A: Extract four modules from cli.py
_5 tasks . ~250 LOC review surface (~2,400 lines moved) . medium risk . mechanical movement_
_touches: src/mind_meld/cli.py, src/mind_meld/skilllink.py, src/mind_meld/eventstail.py, src/mind_meld/resolveflow.py, src/mind_meld/gcreap.py, tests/_

Sizing note: the raw diff is ~2,400 lines, but it is *pure movement* — no logic changes, no behavior changes. The review question is "did anything other than imports change?", which is why the effort labels reflect review burden rather than diff volume. Each task must be verifiable by `git diff --find-renames` showing movement only.

- **`skilllink.py`** -- the retro-fleet installer family (currently ~L2749-3084): the six `_ensure_*` / `_*_check_due` wrappers, marker constants, `_emit_conflict_notice`, `_resolve_retro_skill_src`, `_config_dir`. ~340 lines. _(S)_
- **`eventstail.py`** -- `_enabled_claude_paths`, `_decide_token_walk_policy`, `_run_events_tail`, `_run_events_backfill` (currently ~L3085-3398). ~310 lines. _(S)_
- **`resolveflow.py`** -- conflict discovery and resolution (currently ~L6462-6810 and ~L7155-8036): `_find_conflict_files`, `_migrate_pre_inversion_conflict`, the `_promote_*` family, `_resolve_interactive_loop`, and the CONFLICT-TELEMETRY helpers that will be deleted wholesale at the rip-out. ~1,200 lines. _(S)_
- **`gcreap.py`** -- `_gc_token_cache`, `_gc_old_event_files`, `_gc_old_conflict_files`, `_sweep_local_tmp_files` (currently ~L5478-5560 and ~L8039-8131). ~250 lines. _(S)_
- **Wire and verify** -- `@app.command()` shells stay in `cli.py`; update imports, keep every public name re-exported from `cli` so existing tests and any external callers do not break; full suite green with zero test edits beyond import paths. _cli.py + tests, ~100 lines._ (S)

#### Group 17: Five-way parallel sweep across the new modules

Everything below was a separate serialized Group before the decomposition. Post-split each Track owns a different file, so this is one wave of five PRs.

##### Track 17A: Skill installer correctness + test isolation
_5 tasks . ~180 LOC . medium risk . skilllink.py + conftest_
_touches: src/mind_meld/skilllink.py, tests/conftest.py, tests/test_skill_link.py_

- **One existence predicate across all gates** -- the three self-heal gates use three pre-checks and none matches the installer, so installing Codex after mm is healthy for Claude never yields a link — permanently, not for 24h. Reproduced. Align on `agent_dir.exists()`, which `install_skills_cmd` already uses. _skilllink.py, ~40 lines._ (S)
- **Table-driven targets** -- replace six near-identical wrappers, six marker constants, and seven hardcoded path sites with one `_SKILL_TARGETS` tuple, so a fourth agent is a row not three functions. _skilllink.py, ~60 lines reducible._ (S)
- **Third bucket + non-zero exit on partial install** -- a target whose `symlink_to` raised lands in neither `installed` nor `conflicts` and one success returns 0; pre-v0.12.14 the single-target form always hit `Exit(1)`. Add `not_installed`, flatten the four-conditional tail. _skilllink.py, ~35 lines._ (S)
- **`_isolate_skill_links` autouse fixture** -- the installer mkdirs and symlinks into three real user dirs and `_config_dir()` hardcodes `~/.config/mind-meld`; `pytest` on a dev Mac mutates the developer's real `~/.codex` and `~/.config/opencode`. Consider deleting `_config_dir()` in favour of `config.CONFIG_DIR` so one setattr covers the markers. While here, check whether `test_gc_events.py`'s real-lock exposure wants the same treatment. _tests/conftest.py, ~30 lines._ (S)
- **Composition test + `exist_ok` + per-target notices** -- pin the gate×installer composition `test_skill_link.py` currently misses; add `parents=True, exist_ok=True` to the one mkdir of 22 without it; interpolate `safe_str(str(target))` into the three failure notices that don't say which agent failed. _skilllink.py + tests, ~40 lines._ (S)

##### Track 17B: Make every gc reaper honest
_3 tasks . ~100 LOC . low risk . gcreap.py_
_touches: src/mind_meld/gcreap.py, tests/test_conflict_copy.py_

- **Collapse the two conflict/event reapers onto one loop** -- `_gc_old_conflict_files` only increments `reaped` inside `if not dry_run:`, so `--conflicts --dry-run` always reports "would reap 0"; its ~90%-identical mirror has the `else` branch. Collapse rather than patch the increment so the next divergence can't happen. _gcreap.py, ~50 lines reducible._ (S)
- **`_gc_token_cache` honors `--dry-run`** -- it prints "dry-run; skipping" and reports nothing while every sibling prints `would delete (age Nd): <path>`; its own comment promises the opposite. The reap predicate is pure, so a read-only pass is cheap. _gcreap.py, ~30 lines._ (S)
- **Assert the reaper counts** -- the dry-run test discarded the return value, which is why the always-zero count shipped. _tests, ~20 lines._ (XS)

##### Track 17C: Conflict-prompt rendering DRY
_3 tasks . ~100 LOC . low risk . resolveflow.py + conflictdiff.py_
_touches: src/mind_meld/resolveflow.py, src/mind_meld/conflictdiff.py_

- **Move diff colouring into `conflictdiff.render_diff_lines(diff, cap)`** -- the loop is duplicated verbatim across both prompt sites with silently drifted caps (60 vs 80). This is the leaf-rendering shape the module exists for; the site-level dispatch CLAUDE.md protects is the choice logic, not the colouring. _conflictdiff.py + resolveflow.py, ~40 lines._ (S)
- **Resolve the `b`/`both` alias** -- copy-pasted into both prompts, both comments say "removed at 1.0". Decide: delete now, or one shared `_normalize_conflict_choice`. _resolveflow.py, ~20 lines._ (S)
- **Drop the pointless `else`** -- `if not merge_available: ... continue` followed by an `else:` wrapping 40 lines that only adds an indentation level. _resolveflow.py, ~10 lines._ (XS)

##### Track 17D: Events-tail consolidation + budget the root discovery
_4 tasks . ~250 LOC . medium risk . eventstail.py_
_touches: src/mind_meld/eventstail.py_

- **Extract `_capture_events_snapshot(...)`** -- pull out the 90% shared structure between `_run_events_tail` and `_run_events_backfill` (gate, deadline math, claude_paths walk, agg_projects, s_rows). The in-code comment admits the deadline-refresh bug was fixed twice, once per copy. _eventstail.py, ~80 lines reducible._ (M)
- **Lift token-cache `files_dict` above the duplicated walk loop** -- the two branches under `if do_token_walk` differ only by one arg; `nullcontext(None)` drops ~10 duplicated lines. _eventstail.py, ~10 lines._ (XS)
- **Budget the root discovery, and stop paying for it twice** -- `discover_git_roots` runs with no wall-clock budget (~107 serial `git rev-parse` spawns on the measured Mac), is invisible to the budget notice because `deadline` is reset after it, and runs a second time from `identity._gather_per_repo_emails` on a cold cache. Memoize per-process first — that alone halves the cold path. Also delete the dead `deadline` assignment at the head of both walkers, and surface or drop `warm_token_cache_inline`'s discarded `(walked, skipped)` counts. _eventstail.py, ~50 lines._ (S)
- **Substantive-change gate timing** -- the gate sees the pre-tail manifest; on UTC midnight rollover with zero source changes, no daily mm-push row lands. Verify whether monitoring depends on a daily heartbeat row; either lift the gate when the cursor is >24h stale OR document that no-op pushes don't advance it. _eventstail.py, investigative._ (S)

##### Track 17E: What's left in cli.py
_5 tasks . ~250 LOC . low-medium risk . cli.py_
_touches: src/mind_meld/cli.py, tests/test_silent_failure_contract.py_

- **`safe_str` the two missed peer-controlled print sites** -- `status` prints peer `device_name`/`device_id` raw into a Rich console (which interprets markup and passes escapes through), and `_print_pull_summary` emits `device_name`, `src_name` and `rel_path` unsanitized 30 lines below blocks in the same function that sanitize them. Fold in the three stderr sites already in Future (`walk_git_projects`, `_ensure_device_registered`, `_bootstrap_mm_events_path`) while the sweep is open. _cli.py, ~30 lines._ (S)
- **Interactive counterpart of `_auto_command_setup`** -- four commands repeat the config/passphrase/lock preamble; the auto pair was migrated, the interactive half never was. _cli.py, ~60 lines reducible._ (S)
- **`_auto_command_tail(verb, refused_outcome)`** -- the `autopull`/`autopush` except-tails differ by one verb and one breadcrumb label; verify `test_silent_failure_contract.py` doesn't pin them separately. Also collapse the `enable_source`/`disable_source` 7-line preamble. _cli.py, ~45 lines._ (S)
- **Thirteen function-local re-imports** -- nine shadowing module-scope `hashlib`/`json`/`secrets`/`datetime`, four re-importing the conflict-filename helpers. Ruff F811 cannot see function-local shadowing, so they survive lint indefinitely. Also rename the `sidecar` loop variable that shadows the module, and call `_empty_outcomes()` at its only caller. _cli.py, ~45 lines._ (S)
- **Two small correctness collapses** -- `_push_core` walks `iter_source_diffs` twice per push, once only to compute `has_substantive` (confirm the events re-walk between the two calls doesn't depend on it); and `_bootstrap_or_verify_crypto`'s lost-race branch reimplements line-for-line the fall-through its own comment claims it takes. _cli.py, ~45 lines._ (S)

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
Group 13: Hotfix: events tail dies on one bad byte
  +-- Track 13A ........... ~M . 5 tasks

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

- **cli.py micro-cleanups (old 14A/14B)** — `_resolve_mm_events_dir`, skill-link status enum, marker filename convention. _Source: /full-review 2026-05-10. Reduced: `_empty_outcomes` reuse, dead local re-imports, and the `upgrade.py` `fsutil` import were promoted into Groups 17 and 18._
- **`_resolve_interactive_loop` decomposition** — 630 lines / 74 branch nodes, the largest function in the repo; four separable phases plus interleaved telemetry. _Source: /full-review 2026-08-14. Deferred on a real dependency, not a punt: the CONFLICT-TELEMETRY row construction is woven through it and disappears on its own at the rip-out, so doing this now means doing it twice. Trigger: after the collector is removed._
- **`merge.similarity_ratio` shares `lcs_merge`'s split preamble** — the docstring hand-enforces "MUST match exactly" where a shared helper would make drift impossible. _Source: /full-review 2026-08-14. Same trigger — the function is scheduled to die with the collector, so deleting it later is cheaper than extracting a helper now._
- **Two-machine test bootstrap duplicated across two modules** — `tests/test_pull_result.py` and `tests/test_silent_failure_contract.py` carry the same 12-line block; conftest already owns the single-machine `_setup_real_config`. _Source: /full-review 2026-08-14._
- **Cold-cache budget leftovers (old 16A remainder)** — `allow_refresh=False` on autopush; `_FULL_GATHER_BUDGET_S` on identity gather; per-jsonl deadline in the token merge loop. _Source: /full-review + v0.12.9. The unbudgeted `discover_git_roots` half was promoted into Track 23A._
- **identity.py micro-DRY + token-cache test pins (old 17A/B/C)** — unify `_persist`; load config once in `_do_full_gather`; `gc_cache_entries` `max_age_s=0`; positive cache-isolation test. _Source: Track 11A eng-review._
- **v0.11.17 doc-drift cleanup (old 18A)** — events-retro dead-name list; aggregator docstring; `walk_session_metadata(since)` unused param. _Source: /full-review._
- **Incremental-resume accepted divergences** — tool_use id not seeded across segments; final line without trailing newline never counted. Evidence-triggered only (census). _Verified 2026-08-14: `seen_tool_ids` still initialises empty at `token_usage.py:702`; both divergences stand. Source: [review] inbox._
- **Rip out CONFLICT-TELEMETRY collector** — after Phase 2 bands validate (≥25 real decisions or 60 days). _Verified 2026-08-14: the collector shipped v0.12.12 on 2026-07-30 (15 days ago) and `~/.config/mind-meld/conflict-decisions.jsonl` does not exist on this Mac — **zero decisions collected**. The ≥25-decision trigger is not tracking; only the 60-day bar (~2026-09-28) will fire, and it will fire with no dataset. Worth deciding then whether the similarity classifier below should be killed rather than deferred. Source: [plan-eng-review] inbox._
- **Future-clamped peer mtime can mislead `(n)ewer`** — advisory watch. _Verified 2026-08-14: the `_restore_mtime_best_effort` clamp is intact. Source: [plan-eng-review] inbox._
- **`_promote_target_will_sync` ignores `exclude_patterns`** — rare exclude-glob miss. _Verified 2026-08-14: still present, now at `cli.py:6810` (the inbox cited 6624 — line drift, which Track 19A addresses). Source: [review] PR #97._
- **Phase 2 similarity classifier + silent merge** — blocked on collector data. _Verified 2026-08-14: no `classify_divergence` / `DivergenceClass` anywhere in src. See the collector note above — the blocking dataset is not materialising. Source: [plan-eng-review] inbox._
- **Peers we never resolved against can be mtime-skipped by the drain** — watch now that 12A shipped. _Verified 2026-08-14: drain machinery present at `cli.py:1545`. Source: [plan-eng-review] inbox._
- **Abort transactionality** — pre-existing torn-state. _Verified 2026-08-14: `typer.Abort()` still propagates out of `_pull_core`'s try block. Source: [review] inbox._
- **Price cache writes per-TTL (5m vs 1h)** — wire-format change; competes with host-usage on `token_usage.py`. _Verified 2026-08-14: `_CACHE_WRITE_MULT = 2.0` is still a single constant and `parse_usage` still reads only `cache_creation_input_tokens`. Source: [plan-eng-review] inbox._
- **`test_gc_events.py` touches the real mind-meld.lock** — flake against autopush. _Verified 2026-08-14: the test still runs `CliRunner` without conftest's `_redirect_lock`, which is a plain helper rather than an autouse fixture. Pairs naturally with Track 15A's `_isolate_skill_links` work if that lands first. Source: [review] inbox._
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

- **Skills-walk-complete signal as explicit schema field** — add `skills_walk_complete: bool` on the v=2 sessions-snapshot project metadata (additive, total=False, no schema bump); aggregator picks the latest snapshot per `(device, source_root, claude_dir)` where `skills_walk_complete=True` for skills aggregation, falls back to flagging `skills_incomplete_peers` when no completed walk exists in window. Three-state discriminator (pre-v0.11.27 / complete / skipped) replaces v0.12.4's cosmetic-only breadcrumb. _Source: /plan-eng-review 2026-05-10 (deferred from Track 11B Option B). Track 13A takes the cheaper absence-based path for the deadline case, which may retire this entirely. Do NOT reintroduce the rejected 3-LOC `events.py:_scan_one_project` fix (dropping `if token_cache_files is not None:`) — it causes latest-snapshot-wins data erasure on warm-then-cold push ordering. See CHANGELOG v0.12.4 and `docs/invariants/events-retro.md`._

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
