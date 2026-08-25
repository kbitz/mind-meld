# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Six Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **A command that only exists to undo an automatic action is refused until the automatic action is shown to be correct.** v0.12.44 killed `mm uninstall-skills` this way: a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect — the installer recreated a link the user deleted. Fixing the installer made all three unnecessary. Before filing an inverse, check whether the forward action should have happened at all.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`; two Tracks claiming one version force-advance `latest` to an untagged commit. See that file for the full argument.
- **The roadmap-staleness gate stays dead.** Track 28B was killed 2026-08-25 on the grounds that an empty Current Plan leaves nothing to drift. Groups 29–35 removed that ground, the question was re-put on 2026-08-25 with seven Groups in flight, and the answer was the same. Do not re-propose it; the design remains recorded in the Group 28 entry of `docs/roadmap-shipped.md` for whoever overrides this.
- **Discovery may read host logs locally, but an encoded cwd never goes on the wire.** Track 29A's prober is a two-level scan of `~/conductor/workspaces/*/*` whose only wire output is a canonical remote URL. Codex `turn_context.cwd` and Grok's URL-encoded session dir names would both yield more roots and are refused — see the Future bullet "Do not add a Codex or Grok sessions-snapshot". Confirmed 2026-08-25.

---

## In Progress

_Nothing in flight._

---

## Current Plan

_tombstone: 27_

### Phase 3: Retro fidelity

**End-state:** `retro-fleet` reports what actually happened on the fleet, and reports every agent the same way — tokens and API-equivalent cost split per model family, everything else aggregated across models.
**Groups:** 29, 30, 31, 32, 33, 34, 35

_Why this Phase exists: the shipped card is not merely incomplete, it is confidently wrong. Measured 2026-08-25 for the window 2026-08-18 → 2026-08-25, `mm retro-fleet 7d` reported **4 commits · 1 repo · 1 detected GitHub PR reference**. Ground truth on device `3a6c7dc9` alone, deduped on (canonical remote, sha): **41 commits · 4 repos · 26 distinct PR references**. mind-meld's own ten in-window commits (#140–#144) were absent from its own retro. Every premise below was measured this session against HEAD `0bb9cab` on the maintainer's fleet, not inherited from a prior plan._

#### Group 29: Repository discovery

##### Track 29A: Discover the repositories that exist
_3 tasks . ~215 LOC . medium risk . 4 files_
_touches: src/mind_meld/events.py, tests/test_events.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/events-retro.md (git-root discovery budget, Track 18C), docs/roadmap-future.md ("Do not add a Codex or Grok sessions-snapshot")_
_produces: root discovery returns every live git root on the machine inside the autopush budget_
_session: fresh · effort: high · attach: @src/mind_meld/events.py, @tests/test_events.py · verify: pytest tests/test_events.py tests/test_module_boundaries.py; ruff check ._

_Ordered first deliberately. Git-snapshot loss is **permanent** — the cursor moves on and the interval is never re-walked. Grok reader loss (Group 31) is **recoverable**, because `~/.grok/sessions/**/updates.jsonl` persists on disk and a later fix re-reads the full history. Fix the lossy path before the recoverable one._

- **Stat before you spawn** -- verified 2026-08-25: `events._is_git_toplevel` (`events.py:612`) spawns `git -C <path> rev-parse --show-toplevel` for every candidate. Measured on this machine: **7.84 ms/call** for a live repo, **7.11 ms/call** for a path that no longer exists, against **0.0061 ms** for a `(path / ".git").exists()` probe — a 1300× ratio, and the dead path costs nearly as much as the live one. `_probe_claude` currently emits **57** candidates of which **50 do not exist** (archived Conductor workspaces), so most of the ~430 ms discovery cost is spent proving that deleted directories are not repositories. Add a cheap filesystem gate ahead of the subprocess; keep `git rev-parse` as the authority for candidates that pass it, because `.git` presence alone does not prove *toplevel* — a subdirectory of a worktree must still be rejected. _events.py, ~30 lines._ (S)
- **A prober for the workspaces that are actually live** -- verified 2026-08-25: at an unbounded budget, discovery finds **6** roots while the machine has **10** repositories with in-window commits. The gap is structural, not budgetary. `_probe_claude` (`events.py:517`) can only surface a repo that has a `~/.claude/projects` entry containing at least one jsonl with a `cwd` — `_read_cwd_from_latest_jsonl` sorts every jsonl by mtime and falls through the whole list, so it is robust to a stale newest file but blind when the entry does not exist or holds no jsonl at all. Both cases are live on this machine: four workspaces with in-window commits have **no** `~/.claude/projects` dir (`bolt/reading-pane-focus-fix`, `bolt/key-recognition-surface`, `bolt-email/track-1a-checks-2a-plan`, `mind-meld/retro-fleet-usage-gaps`), and `-Users-kb-Documents-Dev-Personal-bolt-email` has a dir holding only `memory/`. `_probe_gstack` needs `repo-mode.json`, present in **9 of 29** gstack project dirs. Meanwhile `~/conductor/workspaces/*/*` is a **two-level directory listing** naming **9** live workspaces with no subprocess and no log parsing. Add it as a third prober. **Constraint check, and it refuses the obvious alternative:** Codex `turn_context.cwd` and Grok's URL-encoded session dir names would both yield more roots and are deliberately not used — see the standing constraint above. Do not widen this task into host-log reading. _events.py, ~55 lines._ (M)
- **Re-tune the two budgets against the measurement, and record it** -- verified 2026-08-25 by calling `events.discover_git_roots` directly at varying deadlines: `50 ms` (`ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS`) → **0 roots, exceeded**; `100 ms` (`_INTERACTIVE_MS`) → **2 roots, exceeded**; `500 ms` → **6 roots, complete in ~430 ms**; no further roots at 2 s, 10 s or 30 s. Both shipped constants (`events.py:83-84`) sit below the floor at which discovery can return anything useful, which is why observed snapshots carry 0 or 2 projects. The first two tasks should bring a complete pass to roughly **60 ms**; set the constants from the post-fix measurement with margin rather than from the pre-fix one, and put the numbers in the invariant doc so the next regression is arithmetic instead of archaeology. The Track 18C budget was correct as a bound on a subprocess-per-candidate loop; it becomes the wrong bound once the loop is not subprocess-per-candidate. _events.py + docs/invariants/events-retro.md, ~20 lines (incl. tests)._ (S)

#### Group 30: Cursor integrity

_Depends on: Group 29_

##### Track 30A: Stop advancing the cursor past what was never walked
_2 tasks . ~160 LOC . high risk . 5 files_
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/cli.py, tests/test_events_tail.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 29A, docs/invariants/events-retro.md, AGENTS.md (autopush degraded-breadcrumb contract)_
_produces: a push whose discovery was incomplete cannot silently orphan that interval's commits_
_blocked-by: Track 29A_
_session: fresh · effort: high · attach: @src/mind_meld/events.py, @src/mind_meld/events_tail.py · verify: pytest tests/test_events_tail.py tests/test_events.py; ruff check ._

_Blocked-by 29A for a real reason, not for tidiness. `_last_mm_push_ts` already documents that a `None` return "rewinds the cursor to `now - INITIAL_CURSOR_LOOKBACK_DAYS` and re-walks 30 days of git history on every subsequent push, forever". A completeness gate landed **before** 29A would fire on nearly every push — because discovery is nearly always incomplete today — and reproduce exactly that pathology. The gate is only safe once a complete pass is the normal case._

- **Gate the cursor on discovery completeness** -- verified 2026-08-25 across the live event corpus: of the **6** `git-snapshot` rows in the window, **3 captured zero projects** (`3a6c7dc9` at 12:06:30Z; `889e42c0` at both of its in-window pushes). `discover_git_roots` already returns `GitRootDiscovery(roots, errors, exceeded)` with `exceeded=True` and the stable `GIT_ROOT_DISCOVERY_BUDGET_ERROR` string, and `walk_git_projects` already records per-repo `skipped` entries including `budget_abort` — the signal exists and is discarded at the cursor. `write_push_event`'s CT-4 order invariant (mm-push event LAST, so a partial write leaves the cursor unadvanced and the next push re-walks) is the mechanism to reuse: an incomplete discovery should leave the cursor where a partial write would have. Commits re-walked twice are already deduped at render on (canonical remote, sha), so over-walking costs time while under-walking costs data permanently. _events.py + events_tail.py, ~100 lines (incl. tests)._ (M)
- **The degradation has to reach the breadcrumb** -- `AGENTS.md` states the rule this task exists to satisfy: "Any new degradation detected in the tail MUST be appended to that list, not merely printed." `_run_events_tail` returns `list[str]`, `_push_core` carries it on `PushResult.events_degradations`, and `autopush` joins it into the `degraded` breadcrumb `detail`. An incomplete-discovery push is a degradation by that definition and currently emits nothing a monitor can see. Append it. Note the existing precedent for *not* over-reporting: `_decide_token_walk_policy` returning `False` is gated on `claude_paths` because it also means "no claude source enabled", which is a config shape rather than a loss — mirror that care, since a machine with no repositories at all is not degraded. _events_tail.py + cli.py + docs, ~60 lines._ (S)

#### Group 31: Grok reader tolerance

_Depends on: Group 29_

##### Track 31A: Make Grok usage survive an unmodeled record
_3 tasks . ~180 LOC . low risk . 3 files_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/events-retro.md, docs/designs/grok-build-usage-reader.md_
_produces: an enabled Grok source publishes real per-day token totals instead of `io_error`_
_session: fresh · effort: medium · attach: @src/mind_meld/host_usage.py, @tests/test_host_usage.py · verify: pytest tests/test_host_usage.py; ruff check ._

_Sibling of Group 30, not sequential with it — both become launchable when Group 29 lands, and the packer emitted them in the same layer. Document order here is priority, not a gate._

_Two independent defects, one shared root cause: the Grok reader treats every unmodeled-but-benign variant as fatal for the entire scan. Both were found by running the reader against the real local corpus with `consented=True`._

- **An absent optional file is not an I/O error** -- verified 2026-08-25: `host_usage._is_regular_non_symlink` (`host_usage.py:1111`) calls `path.lstat()` and converts **every** `OSError` — including `FileNotFoundError` — into `_ReadFailure("io_error")`, which `_iter_grok_ledgers` (`host_usage.py:524`) propagates out of the whole scan. That call site probes a **speculative** path (`session / "updates.jsonl"`). Measured on the local corpus: **78** Grok session dirs have that file and **1** does not, and that single dir makes `read_grok_usage` return `io_error` with zero data. The Codex walker shares the helper but does not fire today, because its candidates (`host_usage.py:1089`) come from a real `iterdir()` listing filtered by `_ROLLOUT_NAME`. That is a likelihood argument, not immunity — `iterdir()` then `lstat()` is a TOCTOU window, so a rollout reaped by a concurrent `codex` between the two calls takes the Codex scan down the same way. The fix below closes that race for both walkers, which is a second reason to prefer it over special-casing the Grok call site. Narrow the tolerance to absence (`FileNotFoundError`, `NotADirectoryError`) → `False`, and keep every other `OSError` fatal — a permission error on a file that exists is still a real failure the all-or-nothing publication contract must see. _host_usage.py + tests, ~50 lines._ (S)
- **A cancelled turn spent no tokens** -- verified 2026-08-25 by surveying every `turn_completed` record in the local corpus: **193** records, of which **189** carry the exact key set `_GROK_TERMINAL_KEYS` and **4** carry `{prompt_id, sessionUpdate, stop_reason}` with **no `usage` key**. All four have `stop_reason: "cancelled"` and `_meta.cancelTrigger: "esc"` — the user pressed Escape before the model produced anything. `_grok_turns_from_record` (`host_usage.py:681`) asserts `set(update) != _GROK_TERMINAL_KEYS` → fatal `unsupported`, so **4 records out of 193 (2%) destroy 100% of Grok reporting**. Accept a `usage`-less `turn_completed` as a zero-token skip (`return None`, the same disposition the function already uses for a non-terminal update). Do **not** relax the key-set check generally: an *extra* unknown key still means the wire grew a field this reader has not validated, and that must stay fatal. _host_usage.py, ~25 lines (incl. tests)._ (S)
- **Audit the rest of the fail-closed surface** -- root-cause pass, because two instances in one reader is a pattern rather than two bugs. The Grok path contains thirteen `_ReadFailure("unsupported")` raises (`host_usage.py:656-734`, counted 2026-08-25) covering key sets, `stop_reason` vocabulary, counter presence, counter types, id byte-lengths and the `reasoning > output` sanity check. Classify each: **genuinely malformed** (a counter that is not an integer, a duplicate terminal key whose payload disagrees) stays fatal; **unmodeled-but-benign variant** (a shape the host may legitimately emit that carries no tokens) becomes a per-record skip. Record the classification beside the constants so the next Grok release does not re-litigate it. Expected outcome after patching both defects above, measured 2026-08-25: complete scan, **78** ledgers, **8** populated day keys, one model (`grok-4.6-build`), **~1.47 B** tokens lifetime and **~800 M** in the 7-day window — all of it currently rendered as nothing. _host_usage.py + tests, ~140 lines._ (M)

#### Group 32: Codex per-turn reader

_Depends on: Group 29, Group 31_

##### Track 32A: Read Codex usage per turn
_2 tasks . ~260 LOC . high risk . 3 files_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 31A, docs/invariants/events-retro.md (host-usage-snapshot section)_
_produces: Codex totals are a real per-day, per-model distribution rather than one cumulative session figure_
_session: fresh · effort: high · attach: @src/mind_meld/host_usage.py, @tests/test_host_usage.py · verify: pytest tests/test_host_usage.py tests/test_retro_fleet_aggregator.py; ruff check ._

_Ordered after 31A by the packer's `host_usage.py` collision, and that order is the useful one: `_is_regular_non_symlink` is shared by both walkers, so the shared helper should be correct before the Codex walker is rewritten on top of it._

- **`last_token_usage`, bucketed by its own timestamp** -- verified 2026-08-25 against the local corpus. `_terminal_from_record` (`host_usage.py:1336`) reads only `info.total_token_usage`, and `_read_rollout` keeps only the **last** such record per rollout, which is what makes the result a cumulative session figure stamped on one last-touch day — and in turn what the invariant doc's "window slice over-counts at the recent edge" caveat is describing. But every `token_count` record **also** carries `info.last_token_usage` with its own `record["timestamp"]`, and `turn_context.model` carries the real id (note the model sits on `payload` while the record type sits on the record). Summing the per-record deltas reconciles **exactly** with `total_token_usage` on **497 of 664** local rollouts; the **152** that do not reconcile are **resumed** sessions, where the delta is the *correct* answer because the parent's tokens were already counted in the parent file — so this change also removes a double-count the current reader has. Measured window totals, which no card has ever shown: `gpt-5.6-terra` 2,810,214,100 in / 2,732,963,328 cached / 7,356,512 out; `gpt-5.6-sol` 57,393,935 in / 51,426,304 cached / 530,047 out. Keep the existing strictness posture — `_counter`'s required/optional split and the `_MAX_COUNTER` bound still apply per record. _host_usage.py + tests, ~200 lines._ (L)
- **Old cache entries re-walk once, without a version bump** -- the cache entry shape changes from one `(day, model, usage)` terminal to a per-day per-model map, so pre-Track entries must be detected and re-walked. Use **key absence** on the size/mtime hit, not a `CACHE_VERSION` bump: the repo has made this call twice already for the same reason and written down why — the v0.11.27 `"skills_by_day" not in entry` gate (D2) and the v0.12.15 `offset`/`head` absence gate, both chosen because a version bump would discard valid token data that is expensive to rebuild. Preserve the `no_ledger` convergence entry: its whole purpose is that a ledger-less rollout costs a stat instead of a re-parse forever, pinned by `test_uncacheable_rollouts_do_not_block_convergence`. _host_usage.py, ~60 lines._ (S)

#### Group 33: Per-model host wire

_Depends on: Group 30, Group 32_

##### Track 33A: Put per-model host usage on the wire
_3 tasks . ~230 LOC . high risk . 5 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/events.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 32A, docs/invariants/events-retro.md (Track 22A and 23A sections in full)_
_produces: the host-usage snapshot carries per-model per-day buckets a consumer may window and sum_
_blocked-by: Track 32A_
_session: fresh · effort: high · attach: @src/mind_meld/host_usage.py, @src/mind_meld/events.py, @docs/invariants/events-retro.md · verify: pytest tests/test_retro_fleet_aggregator.py tests/test_events.py; ruff check ._

- **Grok stops collapsing to a family** -- `_grok_terminal_key` (`host_usage.py:742`) already hashes the model into the turn key and each accepted turn already carries `"model"`, so per-model Grok data exists inside the reader and is discarded by `_aggregate_grok` (`host_usage.py:792`) on the way out. Surface it in the same shape Claude's `by_model` uses, so the two do not need separate consumers. The local corpus has exactly one Grok model (`grok-4.6-build`, 189 turns), so the per-model map will be single-entry here — write the test against a synthetic two-model fixture, not against the corpus. _host_usage.py, ~40 lines._ (S)
- **`HostUsageSnapshot` carries the per-model map** -- extend the wire shape and the acceptor. `_accept_host_usage_snapshot` and `_tie_break_key` must both keep reading `events.EVENTS_SCHEMA_VERSION` rather than a literal — the invariant doc is explicit that a hardcoded `2` would make mm reject its own freshly written rows fleet-wide on the first bump. Leave `aggregate_sessions`' own `V2_SCHEMA_VERSION = 2` literal alone: the doc says the v1→v2 sessions transition has delta semantics attached and warns specifically against "fixing" that inconsistency. Peers that predate this Track must degrade to a named coverage state, not to a zero. _events.py + host_usage.py + tests, ~110 lines._ (M)
- **Revise the Track 23A renderer contract, explicitly** -- this is a **documented premise change, not a relaxation**, and it must read that way. The contract's forbidden list ("summing `lifetime_by_family[family][day]` buckets as 'tokens this window', summing across machines at all") and its "Isolation, pinned by test" clause are correct **given a reader that produces cumulative session totals**. Group 32 replaces that reader, so the premise no longer holds and the derived prohibitions no longer follow. State the new premise, state which prohibitions it retires and why, and — importantly — state which one **survives**: the doc's disjointness argument (host stores sit outside every mm sync source, so a migrated home directory can put overlapping history under two device ids) is independent of the counter shape and still argues against a naive cross-machine sum. Do not delete an argument merely because a neighbouring one expired. _docs/invariants/events-retro.md, ~80 lines._ (M)

#### Group 34: Host pricing

_Depends on: Group 33_

##### Track 34A: Price the host model families
_2 tasks . ~110 LOC . medium risk . 4 files_
_touches: src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, tests/test_token_usage.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 33A, docs/invariants/events-retro.md (cost-estimation section)_
_produces: a Codex or Grok model resolves a price through the same predicate a Claude model does_
_blocked-by: Track 33A_
_session: fresh · effort: medium · attach: @src/mind_meld/token_usage.py, @tests/test_token_usage.py · verify: pytest tests/test_token_usage.py tests/test_retro_fleet_aggregator.py; ruff check ._

- **Entries for the models actually observed** -- add `PRICING` and/or `MODEL_FAMILY_TIERS` coverage for the families this fleet runs, verified present in the local corpus 2026-08-25: `gpt-5.6-terra`, `gpt-5.6-sol` (Codex) and `grok-4.6-build` (Grok). `resolve_prices` must remain the **only** predicate for "is this model priced" — the invariant doc records that a second `model in PRICING` test was already removed once and must not come back — and `model_family` must keep matching positionally against a literal allowlist, because these ids are peer-controlled and now drive a pricing decision. Refresh `PRICING_LAST_UPDATED` in the same commit as any rate change, since the card prints it as a verification date. _token_usage.py, ~40 lines._ (S)
- **Host tokens reach the cost path** -- route the per-model host buckets into `estimate_cost` and `_unpriced_token_summary`. This is where `tests/test_retro_fleet_aggregator.py:3979` `test_host_tokens_do_not_reach_prior_period` is deliberately retired, against the revised contract from 33A — retire it in the PR that makes it false, and say so in the commit body rather than letting a deleted pin look like an accident. Keep the `>=` versus `~` distinction the doc argues for: any unpriced volume makes the total a floor, and a Bedrock-style id (`us.anthropic.…`) still fails the `claude-` prefix check, so the "unavailable — no model in this window could be priced" branch stays reachable. _aggregator.py + tests, ~70 lines._ (M)

#### Group 35: Unified reporting

_Depends on: Group 34_

##### Track 35A: Report every agent the same way
_2 tasks . ~200 LOC . medium risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 34A, 33A, docs/invariants/events-retro.md (Track 23A renderer contract, as revised)_
_produces: tokens and cost split per model family for every agent; commits, LOC and PRs aggregate across all of them_
_blocked-by: Track 34A_
_session: fresh · effort: high · attach: @src/mind_meld/skills/retro_fleet/aggregator.py, @tests/test_retro_fleet_aggregator.py · verify: pytest tests/test_retro_fleet_aggregator.py; ruff check ._

- **One block, every agent** -- the shipped card renders Claude through `_render_token_block` (tokens in / cache_read / out, cache hit ratio, estimated cost, per-model breadcrumb) and Codex through `_render_agent_inventory`'s "Per-machine diagnostic counters; not weekly spend and never safe to sum across machines" table, with no cost and both Codex and OpenCode collapsed into one `Codex models` row. That asymmetry was a consequence of the reader, and Groups 32–33 remove the cause. Replace the diagnostic table with a per-model-family tokens-and-cost block shaped like `_render_token_block`, and drop `AGENT_FAMILY_ROWS`' `Claude (via agents)` disambiguation once there is one models block rather than two. Coverage reporting is **not** optional collateral: `_agent_coverage_notes` exists because "a vanished block must never be the diagnostic interface — seven distinct causes would otherwise render identically as nothing", and that argument survives this Track unchanged. OpenCode must keep reporting an honest empty — verified 2026-08-25, `read_opencode_usage` returns `complete=True` with no hosts because `~/.local/share/opencode` does not exist, which is a completed empty scan and not a gap. _aggregator.py + SKILL.md + tests, ~160 lines._ (L)
- **Everything else aggregates across models** -- audit the remaining sections so no non-token metric is split per agent or per model: commits, LOC, streak, commit-type mix, peak hours, bursts, ship-of-window, top repos, PR references and the trends table are all cross-model fleet aggregates and must render as one number each. `_render_ascii_card` already reads this way; the Track's job is to keep it that way while a second models block is being removed, and to pin it with a test so a later per-agent split is a build failure rather than a review catch. Note the standing 24B decision that the card gets no new row — it is width-constrained at 64 chars with five blocks already competing. _aggregator.py + tests, ~40 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not gating.

Adjacency list (from the packer):
```
- Group 29 ← {}
- Group 30 ← {29}
- Group 31 ← {29}
- Group 32 ← {29, 31}
- Group 33 ← {30, 32}
- Group 34 ← {33}
- Group 35 ← {34}
```

Track detail per group:
```
Group 29: Repository discovery
  +-- Track 29A ........... ~L . 3 tasks

Group 30: Cursor integrity
  +-- Track 30A ........... ~M . 2 tasks

Group 31: Grok reader tolerance
  +-- Track 31A ........... ~L . 3 tasks

Group 32: Codex per-turn reader
  +-- Track 32A ........... ~L . 2 tasks

Group 33: Per-model host wire
  +-- Track 33A ........... ~L . 3 tasks

Group 34: Host pricing
  +-- Track 34A ........... ~M . 2 tasks

Group 35: Unified reporting
  +-- Track 35A ........... ~L . 2 tasks
```

**Total: 1 phase . 7 groups . 7 tracks remaining.**

_Every Track here declares `pyproject.toml`, so each is release-bearing and the packer serialises them one per Group — the standing constraint working as intended. Groups 29 and 31 alone answer the reported symptoms; Groups 32–35 are the reporting-parity half, so the plan has a natural stopping point after Group 31 if priorities change._

_**Two unordered collisions are accepted deliberately, not overlooked**, and `STYLE_LINT` names both (`30A ∥ 31A`, `30A ∥ 32A`). Those pairs touch disjoint modules — 30A is `events.py` / `events_tail.py` / `cli.py`, 31A and 32A are `host_usage.py` — so their only overlap is the release itself. Declaring `_blocked-by` between them would fabricate a dependency to silence a lint, which is the defect that killed Track 23B. Ship them in whichever order is convenient; 30A is listed first only because cursor loss is permanent._

---

## Future

Deferred: docs/roadmap-future.md (57 items)

## Shipped

History: docs/roadmap-shipped.md
