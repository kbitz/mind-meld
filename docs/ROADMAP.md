# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Six Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **A command that only exists to undo an automatic action is refused until the automatic action is shown to be correct.** v0.12.44 killed `mm uninstall-skills` this way: a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect — the installer recreated a link the user deleted. Fixing the installer made all three unnecessary. Before filing an inverse, check whether the forward action should have happened at all.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`; two Tracks claiming one version force-advance `latest` to an untagged commit. See that file for the full argument.
- **The roadmap-staleness gate stays dead.** Track 28B was killed 2026-08-25 on the grounds that an empty Current Plan leaves nothing to drift. Groups 29–35 removed that ground, the question was re-put on 2026-08-25 with seven Groups in flight, and the answer was the same. Do not re-propose it; the design remains recorded in the Group 28 entry of `docs/roadmap-shipped.md` for whoever overrides this.
- **Discovery may read host logs locally, but an encoded cwd never goes on the wire.** Track 29A's prober is a two-level scan of `~/conductor/workspaces/*/*` whose only wire output is a canonical remote URL. Codex `turn_context.cwd` and Grok's URL-encoded session dir names would both yield more roots and are refused — see the Future bullet "Do not add a Codex or Grok sessions-snapshot". Confirmed 2026-08-25.
- **A Track that puts a field on a wire, in a cache, or in a log must name its reader in the same card, or declare the reader's Track by number.** Track 34A's review found FOUR producer-without-consumer instances in one pass: `degraded_sources` (shipped v0.12.47, zero readers), `git_capture` (shipped Track 30A, unread by the aggregator), `usageIsIncomplete` (discarded at cache normalization), and the SKILL.md decoder's missing fallback. Reinforced 2026-09-01 by the v0.12.51 conflict-log analysis, which found the conflict-decision collector had been deleted on a premise nobody read, and `synclog.py` still describing the pre-inversion direction four months after the inversion. A write with no reader is not half a feature, it is a liability that reads as one.
- **When a Track touches a reader, check the cache shape, not just the behaviour.** Track 34A verified all six of its card premises as TRUE-or-known-false and was still under-priced 2.5x, because premises describe behaviour while the cost sat in `host_usage._validated_grok_entry`, which normalizes every cached turn to `{key, day, model, usage}` and drops the rest. The existing "check the premise at drain time" constraint worked exactly as written and was insufficient.

---

## In Progress

_Nothing in flight._

---

## Current Plan

_tombstone: 27_

### Phase 3: Retro fidelity

**End-state:** `retro-fleet` reports what actually happened on the fleet, and reports every agent the same way — tokens and API-equivalent cost split per model family, everything else aggregated across models.
**Groups:** 29, 30, 31, 32, 33, 34, 35, 36

_Groups 29-34 shipped (v0.12.45 / .46 / .47 / .48 / .49 / .50) and moved to `docs/roadmap-shipped.md`. They answered the reported symptoms — git history was being lost, Grok published nothing, Codex was double-counted, and the card could not see a dropped reader. Groups 35-36 are the remaining reporting-parity half._

_Every Track below declares `pyproject.toml`, which is deliberately absent from `docs/shared-infra.txt`, so the packer serialises them one per Group. That is the standing constraint working as intended, not an accident of the draft._

#### Group 35: Host pricing

_**In flight 2026-09-01** — workspace `price-host-model-families`, branch `kbitz/price-host-model-families`, no commits yet. **ID frozen for that reason**, which is also why Groups 36-38 keep their numbers and the two new Groups below took 39 and 40 rather than renumbering the plan. Do not restructure this card from another workspace: further amendments belong to Track 35A's own PR, or to the next `/roadmap` run after it merges._

##### Track 35A: Price the host model families
_2 tasks . ~110 LOC . medium risk . 5 files_
_touches: src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, tests/test_token_usage.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 34A, docs/invariants/events-retro.md (cost-estimation section)_
_produces: a Codex model resolves a price through the same predicate a Claude model does_
_session: fresh · effort: medium · verify: pytest tests/test_token_usage.py tests/test_retro_fleet_aggregator.py tests/test_host_usage.py tests/test_host_usage_snapshot.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

- **Entries for the models actually observed** -- **five families, not three.** Measured 2026-08-28: `gpt-5.6-terra` (7.61 B input lifetime), `gpt-5.6-sol` (165 M), `gpt-5.4` (79.0 M), `gpt-5.5` (75.3 M), plus `grok-4.6-build`. The prior card named three; `gpt-5.4` and `gpt-5.5` fall outside a 7d window today but inside 30d/90d, and `mm retro-fleet` takes an arbitrary window. `resolve_prices` must remain the ONLY predicate for "is this model priced"; `model_family` must keep matching positionally against a literal allowlist, because these ids are peer-controlled and now drive a pricing decision. Refresh `PRICING_LAST_UPDATED` in the same commit as any rate change. **Do NOT reach for `_tier()`** — its docstring derives cache read/write as fixed multiples of input, which is an ANTHROPIC billing property. Write literal four-field `PRICING` overrides for `gpt-*`, or non-Anthropic cache tokens are mispriced. _token_usage.py, ~40 lines._ (S)
- **Host tokens reach the cost path** -- route per-model host buckets into `estimate_cost` and `_unpriced_token_summary`. Keep the `>=` versus `~` distinction: any unpriced volume makes the total a floor, and so does a machine listed in Track 34A's `partial_sources`, whose cost line is a FLOOR rather than an estimate for a second and independent reason. _aggregator.py, ~70 lines._ (M)

_**Amendment 1 (`/roadmap`, 2026-09-01) — a hazardous instruction struck.** The task above previously ended: "`tests/test_retro_fleet_aggregator.py::test_host_tokens_do_not_reach_prior_period` is retired HERE, in the PR that makes it false." **Struck.** Verified at `11b7806`: the test lives at `tests/test_retro_fleet_aggregator.py:3979` (nested in a class, so the bare `::name` node-id does not even resolve) and **passes**. `_aggregate_git_period_pair` (aggregator.py:1214) iterates only `git-snapshot` rows and `PriorPeriod` is pinned to four integers, so neither task makes it false. As written the card instructed deleting a passing guard. Filed by Track 35A's own `/autoplan` (0A/P4); applied here because that pass explicitly declined to hand-edit this file._

_**Amendment 2 (`/roadmap`, 2026-09-01) — `verify:` widened from two files to seven.** The amended Track's reader-level work exercises `tests/test_host_usage.py` and `tests/test_host_usage_snapshot.py`, and the PROGRESS row is enforced by `tests/test_docs_routing.py`. **`_touches:` was deliberately NOT widened to match.** If Amendment 3's Grok ingestion work lands in `host_usage.py`, that is 35A's own call to declare in its PR — widen `_touches:` there and re-pack, per the documented drift process. Pre-widening it from another workspace would violate this Group's own "do not restructure from another workspace" note, and it pushed the Track over `max_files_per_track` in the audit._

_**Amendment 3 (gate decision, 2026-09-01) — Grok RATES are held out of this Track.** Both `/autoplan` voices challenged the roadmap order (Codex: "shipping pricing before Grok telemetry works is backwards") and the user ruled: ship the Codex-side work now, hold the Grok rate entries. So `resolve_prices("grok-4.6-build")` returns `None` after 35A **by decision, not by omission** — do not read it as an oversight and do not "finish" it. Grok **ingestion** normalization still ships here; only the rates are held. Two blockers must clear first: the `_validated_grok_entry` `offset != size` wedge (Group 37) and the OpenCode `$.id` defect filed in the inbox. The follow-up must carry xAI's 200K-context threshold, which doubles the rate on ALL tokens in a request and makes every Grok figure a permanent floor._

#### Group 36: Unified reporting

_Depends on: Group 35_

##### Track 36A: Report every agent the same way
_2 tasks . ~200 LOC . medium risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 35A, 33A, docs/invariants/events-retro.md (Track 23A renderer contract, as revised)_
_produces: tokens and cost split per model family for every agent; commits, LOC and PRs aggregate across all of them_
_blocked-by: Track 35A_
_session: fresh · effort: high · verify: pytest tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Phase 3's end-state lands here. Premise re-verified 2026-09-01: `_render_agent_inventory` and `AGENT_FAMILY_ROWS` are both still present in `aggregator.py`._

- **One block, every agent** -- replace `_render_agent_inventory`'s diagnostic table with a per-model-family tokens-and-cost block shaped like `_render_token_block`, and drop `AGENT_FAMILY_ROWS`' `Claude (via agents)` disambiguation once there is one models block rather than two. Coverage reporting is not optional collateral: `_agent_coverage_notes` exists because "a vanished block must never be the diagnostic interface". OpenCode must keep reporting an honest empty. _aggregator.py + SKILL.md, ~160 lines._ (L)
- **Everything else aggregates across models** -- commits, LOC, streak, commit-type mix, peak hours, bursts, ship-of-window, top repos, PR references and the trends table are cross-model fleet aggregates and must render as one number each. Pin it so a later per-agent split is a build failure rather than a review catch. Note the standing 24B decision that the card gets no new row. _aggregator.py, ~40 lines._ (S)

### Phase 4: Reader consolidation

**End-state:** one walker, one cache and one resume protocol behind three thin per-format adapters, so the same bug class stops being fixed once per reader.
**Groups:** 37, 38

_Both Groups are internal quality with no user-visible output. They are sequenced after Phase 3 so the reporting payoff is not held hostage to a refactor._

#### Group 37: Cache encoding

_Depends on: Group 36_

##### Track 37A: Shrink the host cache encoding
_2 tasks . ~140 LOC . medium risk . 3 files_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 32A_
_produces: the host cache stops scaling its per-push cost with corpus size_
_blocked-by: Track 36A_
_session: fresh · effort: medium · verify: pytest tests/test_host_usage.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Filed from Track 32A's `/review`. Measured 2026-08-28 on a 747-rollout / 694 MB corpus: 72,654 states, cache **0.40 MB → 13.48 MB**, json round-trip **56.3 ms of the 250 ms** autopush host budget. `locked_json_rmw` parses and re-serialises the whole file every push, so the cost is paid per push and scales linearly. At roughly 4x this corpus it consumes the budget and the reader can never converge. **Trigger: round-trip above 100 ms, or cache above 25 MB.** Gate confirmed 2026-09-01 at `host_usage.py:1118`, written `offset != size`._

- **Store per-state increments instead of absolute cumulatives** -- identity survives because both sides reconstruct from the same running sum, and increments are ~5 digits against ~8. Absorbs the filed Grok item: `_validated_grok_entry` requires `offset == size`, so a ledger that cannot be read end-to-end in one budget discards all its work forever — 60 ms wedges permanently at 15 files on the live corpus. Same persisted-offset fix `token_usage.walk_jsonl_segment` shipped for Claude in v0.12.15. **This is one of the two blockers on Track 35A's held Grok rates.** _host_usage.py, ~90 lines._ (M)
- **Bound a single entry** -- a per-entry cap needs a degradation that keeps the file's tokens counted while dropping only its cross-file dedup. Refusing the file would re-create the fail-closed whole-store pathology Track 31A removed. `iter_bounded_lines` bounds line SIZE, not line COUNT; max observed is 1,234 states. _host_usage.py, ~50 lines._ (S)

#### Group 38: Walker substrate

_Depends on: Group 37_

##### Track 38A: One walker, three adapters
_3 tasks . ~400 LOC (net NEGATIVE) . medium risk . 4 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/token_usage.py, tests/test_host_usage.py, tests/test_token_usage.py, tests/test_module_boundaries.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 37A, 33A, docs/invariants/events-retro.md (incremental-resume section)_
_produces: one cache/resume/fingerprint implementation shared by Claude, Codex, Grok and OpenCode_
_blocked-by: Track 37A_
_session: fresh · effort: high · verify: pytest tests/test_host_usage.py tests/test_token_usage.py tests/test_module_boundaries.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_**Literal corrected 2026-09-01.** This card said `host_usage.py` is 1,730 lines. Measured at `11b7806`: **2,559** — it grew 829 lines across Tracks 32A/33A/34A, all of which landed in it. `token_usage.py` is 1,856, which the card had right. The premise DIRECTION strengthens (more duplication, not less) and the stale number is not carried forward. `host_usage.py` imports five names from `token_usage`; cache version, empty-cache, resume plan, cache-hit validation, head fingerprint, file identity, day extraction and counter coercion are each written two or three times, across four cache files where Claude has one. The genuinely host-specific surface is the record shape only._

- **Hoist the resume protocol** -- one file-identity + head/tail-digest + complete-line-offset implementation. Keep `iter_bounded_lines` where it is; it is already shared and is the proof the seam works. _host_usage.py + token_usage.py, ~150 lines._ (S)
- **Collapse the per-format readers to adapters** -- after Track 32A all three readers are per-turn with a dedup key (Claude `message.id`, Grok `_grok_terminal_key`, Codex `turn_id`). An adapter's whole job becomes: given a file, yield `(dedup_key, day, model, usage)`. `_aggregate` and `_aggregate_grok` become one function. _host_usage.py, ~180 lines (net negative)._ (M)
- **Retire the duplicated leaf helpers and pin the boundary** -- one day parser, one counter coercion, preserving the trust-boundary split the aggregator documents (`_safe_int` for peer-controlled events, the shared helper for trusted local reads). Extend `tests/test_module_boundaries.py` so a reintroduced private copy fails the build. Also bound the cache's interned `models` table: `host_usage._add_usage` materializes every distinct model the local corpus has ever seen into `by_day[day]["by_model"]`, and the 32/day and 64/row caps only apply later in `events._cap_by_model`. Capping in the READER is not the fix — it would break the "day totals stay whole" invariant that makes `day_total - sum(by_model)` an honest residual. _host_usage.py + tests, ~70 lines._ (M)

#### Group 39: Conflict sidecar forensics

_Depends on: Group 38_

##### Track 39A: Find and fix what deletes conflict sidecars
_3 tasks . ~220 LOC . high risk . 5 files_
_touches: src/mind_meld/cli.py, src/mind_meld/resolveflow.py, tests/test_conflict_copy.py, docs/invariants/conflicts.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/conflicts.md (the inversion and sidecar-dedup sections in full)_
_produces: the deleter is named, and fixed if it is mm_
_blocked-by: Track 38A_
_session: fresh · effort: high · verify: pytest tests/test_conflict_copy.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Diagnose FIRST, then fix. The prohibition is on shipping a blind mitigation — a retry-on-vanish that papers over whatever is deleting user data — NOT on fixing the cause once it is named. If the cause is in mm, it is fixed in this PR. If the fix turns out to be large, it splits to a follow-up Track rather than bloating this card; if the cause is external, task 2 is discharged and says so._

_Measured 2026-09-01: `mm autopull` at 11:40:40 UTC logged 25 `conflicted` outcomes to `pull-history.jsonl`, and `.mind-meld-log.md` recorded a sidecar for `memory/user_path_order.md`. **Zero `.sync-conflict-*` files exist anywhere on disk** and `mm conflicts` reports none. Parent-directory mtimes across all three affected trees (`~/.claude/projects/*/memory/`, `~/.codex/skills/.system/**`, `~/.gstack/projects/*/`) read 07:41-07:42 local — the signature of a create followed by a delete, one minute after the pull. If real, the peer's bytes are discarded with no recoverable trail while the sync log tells the user to run `mm resolve`._

_Ruled out BY PROBE, not by reasoning — do not re-spend the session on these. The write path is correct (driving `cli._apply_conflict` in a tmp dir produces the sidecar under installed 0.12.50). The `mm gc` reapers are only reachable from the `gc` command, and `_gc_old_conflict_files` additionally requires `--conflicts` plus a 30-day bar. `retention._sweep_local_tmp_files` is scoped to `data/<device>/` and `manifests/<device>/` in the storage tree, never local source trees. `_existing_post_inversion_sidecars_from_peer` globs anchored to `canonical.stem`, so it cannot reap a sibling's sidecar. `bin/apply` in the agent-config repo contains no `rmtree` / `unlink` / `rsync --delete`. Nothing in the 0.12.35-0.12.50 changelog touches the sidecar write path — and the machine self-upgraded 0.12.34.1 → 0.12.50 one second before the observed pull, so reproduce on BOTH versions before concluding the cause is external._

- **Reproduce under filesystem instrumentation** -- drive a real conflicting two-device pull under `fs_usage` (or an audit hook) scoped to the three trees, and name the process that unlinks. This task's deliverable is a named cause, recorded in `docs/invariants/conflicts.md` whichever way it resolves. _tests + throwaway harness, ~60 lines._ (M)
- **Fix it if it is mm** -- conditional on task 1. Scope is deliberately unsized because the cause is unknown; that is the honest state of this card, not an omission. If the deleter is an mm code path, it is fixed here with a regression pin in `tests/test_conflict_copy.py`. If it is external (another tool pruning a directory mm writes into), the finding is documented and mm's defence is task 3 alone. `_touches:` declares only the surface the diagnosis already points at — the apply path in `cli.py` and the resolver. **A fix landing outside it (e.g. `fsutil.py`, `retention.py`, `synclog.py`) means widen `_touches:` and re-pack**, per the documented drift process; those were NOT pre-declared speculatively, because the probe ruled the reapers and the tmp sweeper out. _location unknown, ~100 lines._ (M)
- **Make the loss detectable rather than silent** -- ships REGARDLESS of what task 1 finds, and is the reason this Track is not purely investigative. A `conflicted` outcome whose sidecar is absent immediately afterwards is a condition mm can assert: one `exists()` stat on the path just written converts a silent discard into the visible-failure contract's `mm: warning:` line. Per that contract this warning reaches stderr even in quiet mode — it signals data-at-risk, so do NOT gate it behind `if not quiet:`. _cli.py, ~60 lines._ (S)

#### Group 40: Sync surface

_Depends on: Group 39_

##### Track 40A: Narrow the sync surface structurally
_2 tasks . ~180 LOC . medium risk . 4 files_
_touches: src/mind_meld/manifest.py, src/mind_meld/config.py, tests/test_manifest.py, tests/test_config.py, docs/invariants/sync.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 39A, docs/invariants/sync.md ("Generated files are not sync data", added v0.12.51)_
_produces: a generated directory drops out of sync without anyone adding a glob for it_
_blocked-by: Track 39A_
_session: fresh · effort: medium · verify: pytest tests/test_manifest.py tests/test_config.py tests/test_integration.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Both halves are follow-ups v0.12.51 named as its own known limitations. That release excluded 44 of 88 recorded conflicts by glob; these two close the parts a glob cannot express._

- **Marker-aware directory skip in the walker** -- v0.12.51 excludes gstack-extend's per-host skill renders BY NAME (`config._GENERATED_HOST_SKILL_GLOBS`, five globs duplicated across the codex and opencode entries) because `exclude_patterns` are fnmatch globs against a relative path and cannot express "skip the directory CONTAINING this file." gstack-extend already drops `.extend-root` in every dir it renders — 14 carry one today (7 skills x 2 hosts). Until the walker can see it, every new gstack-extend skill silently starts conflicting fleet-wide until someone adds a glob. Touches `manifest.walk_generic_source`: read the tombstone-suppression invariant first, because a marker skip must not generate deletion tombstones, exactly as adding a glob must not. All four scenarios are pinned in `tests/test_integration.py::TestExcludePatterns5C`. _manifest.py + config.py, ~120 lines._ (M)
- **Exclude the pair-review state machine only** -- `projects/*/pair-review/session.yaml` (8 of the 88 conflicts) is a live per-machine state machine and definitionally cannot be shared. The prose artifacts (`deploy.md`, `report.md`, `parked-bugs.md` — 23 more conflicts) STAY in scope: pair-review advertises cross-machine resume as a feature, so excluding them removes capability rather than noise. **The measurement that makes that call defensible: across the same window these paths took 31 conflicts against 178 mtime-skips**, so the existing local-is-newer gate already absorbs 85% of the collisions and the residual does not justify removing a feature. Recomputed 2026-09-01 from `pull-history.jsonl{,.1}`; an earlier draft of this figure said 176. The fuller fix is device-scoped artifact paths (`pair-review/<device>/`) in GSTACK, not an mm exclusion — file that upstream rather than absorbing it here. _config.py, ~60 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not gating.

Adjacency list (from the packer):
```
- Group 35 ← {}
- Group 36 ← {35}
- Group 37 ← {36}
- Group 38 ← {37}
- Group 39 ← {38}
- Group 40 ← {39}
```

Track detail per group:
```
Group 35: Host pricing            (in flight)
  +-- Track 35A ........... ~M . 2 tasks

Group 36: Unified reporting
  +-- Track 36A ........... ~L . 2 tasks

Group 37: Cache encoding
  +-- Track 37A ........... ~M . 2 tasks

Group 38: Walker substrate
  +-- Track 38A ........... ~L . 3 tasks

Group 39: Conflict sidecar forensics
  +-- Track 39A ........... ~L . 3 tasks

Group 40: Sync surface
  +-- Track 40A ........... ~M . 2 tasks
```

**Total: 2 phases . 6 groups . 6 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (63 items)

## Shipped

History: docs/roadmap-shipped.md
