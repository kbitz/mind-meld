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
**Groups:** 29, 30, 31, 32, 33, 34, 35, 36

_Groups 29-31 shipped (v0.12.45 / .46 / .47) and moved to `docs/roadmap-shipped.md`. They answered the reported symptoms: git history was being lost, and Grok published nothing. Groups 32-36 are the reporting-parity half._

_Every Track below declares `pyproject.toml`, which is deliberately absent from `docs/shared-infra.txt`, so the packer serialises them one per Group. That is the standing constraint working as intended, not an accident of the draft._

#### Group 32: Codex per-turn reader

##### Track 32A: Read and dedup Codex usage per turn
_4 tasks . ~650 LOC . high risk . 5 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/cli.py, src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_host_usage.py, tests/test_host_usage_snapshot.py, docs/invariants/events-retro.md, README.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/events-retro.md (host-usage-snapshot + incremental-resume sections)_
_produces: Codex tokens land on the day they were spent, counted once across forked and resumed rollout files_
_session: fresh · effort: high · attach: @src/mind_meld/host_usage.py, @tests/test_host_usage.py · verify: pytest tests/test_host_usage.py tests/test_host_usage_snapshot.py tests/test_retro_fleet_aggregator.py; ruff check ._

_Re-scoped 2026-08-28 after `/autoplan` measured the shipped card's central premise FALSE against the live 746-rollout corpus. The card proposed summing `info.last_token_usage` and attributed the non-reconciling files to resumed sessions. Actual cause: **duplicate `token_count` records** (183 files, 414 records) where the total stays flat and `last` repeats. Only 4 files are resumes. The card proposed the estimator that causes the larger error._

_The bigger omission the card never named: **a rollout file is not a session.** 195 `turn_id` values span 244 of 746 files, sharing 85% of their ledger before diverging, so per-file summing double-counted **over half** the reported total. Fixing the estimator without fixing that would have made a ~2x-wrong number precise._

- **Per-turn estimator, and the pre-context buffer it forces** -- difference `total_token_usage` between consecutive readings; use `last_token_usage` only for a lineage's opening reading, where the counter may already carry a parent session's history. Reconciles exactly with the shipped number on 480 of 480 non-forked rollouts, across all four counters. The same change makes a dropped pre-`turn_context` prefix permanent, so those ledgers are buffered and attributed to the first model the file names: the old skip was justified by "totals are cumulative, so a later record restates them", the exact premise this Track deletes, and the prefix is 1,557 records worth 209,515,399 input tokens. A file whose ledgers are ALL pre-context still refuses. _host_usage.py, ~160 lines._ (S)
- **Cross-file turn dedup** -- key work by `(lineage, previous, current)` over connected components of `turn_id`. The hard half of the Track: deduping READINGS is wrong (two branches forking at 100 and reaching 130 and 150 would treat 130 as a waypoint and drop a branch), and Codex re-emits a turn's final reading as the next turn's first, so a turn is a lineage LINK rather than a bucket key. An opening reading has no predecessor and so no transition identity, and must be suppressed when another file already reached that cumulative. _host_usage.py, ~150 lines._ (M)
- **Cache shape, migration gate and resume carry** -- interned `turn_ids` / `days` / `models` tables, four-field resume carry, key-absence re-walk (NOT a `CACHE_VERSION` bump, which is shared with the Grok and OpenCode namespaces). _host_usage.py, ~150 lines._ (S)
- **Surfaces and the premise correction** -- `codex_usage_diag()`, an `mm status` rebuild line, and per-reason failure text (`migration` had inherited a generic retry promise and belongs to OpenCode's own storage migration, not to any mm cache). Plus the 8 documentation locations that assert the cumulative premise, one of which was already false for Grok as of v0.12.47. _cli.py + events_tail.py + docs, ~190 lines._ (S)

#### Group 33: Per-model host wire

_Depends on: Group 32_

##### Track 33A: Put per-model host usage on the wire
_3 tasks . ~230 LOC . high risk . 5 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/events.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 32A, docs/invariants/events-retro.md (Track 22A and 23A sections in full)_
_produces: the host-usage snapshot carries per-model per-day buckets a consumer may window and sum_
_blocked-by: Track 32A_
_session: fresh · effort: high · verify: pytest tests/test_retro_fleet_aggregator.py tests/test_events.py; ruff check ._

- **Grok stops collapsing to a family** -- per-model Grok data exists inside the reader and is discarded by `_aggregate` on the way out. Surface it in the shape Claude's `by_model` uses. The local corpus has one Grok model, so write the test against a synthetic two-model fixture. _host_usage.py, ~40 lines._ (S)
- **`HostUsageSnapshot` carries the per-model map as an ADDITIVE SIBLING key** -- **never by widening `hosts`.** `aggregator._copy_usage_bucket` validates a day bucket with an exact key-set match and a rejected bucket fails the WHOLE row, so an extra key makes every older peer drop the row and keep a stale one, fleet-wide. Bumping `EVENTS_SCHEMA_VERSION` does not rescue it: the acceptor compares against the current constant and would then reject the rows it had retained. `degraded_sources` (v0.12.47) is the pattern to copy. _events.py + host_usage.py, ~110 lines._ (M)
- **Revise the Track 23A renderer contract, explicitly** -- state the new premise, which prohibitions it retires, and which one SURVIVES: the cross-machine disjointness argument is independent of the counter shape. Do not delete an argument because a neighbouring one expired. _docs/invariants/events-retro.md, ~80 lines._ (M)

#### Group 34: Coverage reporting

_Depends on: Group 33_

##### Track 34A: Report the coverage the wire already carries
_2 tasks . ~140 LOC . medium risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/events.py, src/mind_meld/host_usage.py, tests/test_retro_fleet_aggregator.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 33A_
_produces: a dropped or partially-read host reader is visible on the card, not just in a breadcrumb_
_blocked-by: Track 33A_
_session: fresh · effort: medium · verify: pytest tests/test_retro_fleet_aggregator.py; ruff check ._

- **Render `degraded_sources` on the card** -- verified 2026-08-28: `grep -rn degraded_sources src/mind_meld/skills/retro_fleet/aggregator.py` returns **zero** matches and `_AcceptedHostRow` has no field for it. Track 31A's failure isolation reaches the autopush breadcrumb and never reaches the retro card, which is the one surface where a user would notice a reader had gone missing. A device whose reader was dropped renders as a normal latest snapshot with that host simply absent. _aggregator.py, ~80 lines._ (M)
- **A coverage state between complete and failed** -- 3 of 200 Grok turns carry `usageIsIncomplete: true` and drop `costUsdTicks`; one is the largest turn in the corpus. The reader validates counter presence inside `usage` and never inspects the key set, so these are accepted as complete accounting. `HostUsageResult` has no state between `complete=True` and `complete=False` to express partial fidelity. _host_usage.py + events.py, ~60 lines._ (S)

#### Group 35: Host pricing

_Depends on: Group 34_

##### Track 35A: Price the host model families
_2 tasks . ~110 LOC . medium risk . 4 files_
_touches: src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, tests/test_token_usage.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 34A, docs/invariants/events-retro.md (cost-estimation section)_
_produces: a Codex or Grok model resolves a price through the same predicate a Claude model does_
_blocked-by: Track 34A_
_session: fresh · effort: medium · verify: pytest tests/test_token_usage.py tests/test_retro_fleet_aggregator.py; ruff check ._

- **Entries for the models actually observed** -- **five families, not three.** Measured 2026-08-28: `gpt-5.6-terra` (7.61 B input lifetime), `gpt-5.6-sol` (165 M), `gpt-5.4` (79.0 M), `gpt-5.5` (75.3 M), plus `grok-4.6-build`. The prior card named three; `gpt-5.4` and `gpt-5.5` fall outside a 7d window today but inside 30d/90d, and `mm retro-fleet` takes an arbitrary window. `resolve_prices` must remain the ONLY predicate for "is this model priced"; `model_family` must keep matching positionally against a literal allowlist, because these ids are peer-controlled and now drive a pricing decision. Refresh `PRICING_LAST_UPDATED` in the same commit as any rate change. _token_usage.py, ~40 lines._ (S)
- **Host tokens reach the cost path** -- route per-model host buckets into `estimate_cost` and `_unpriced_token_summary`. `tests/test_retro_fleet_aggregator.py::test_host_tokens_do_not_reach_prior_period` is retired HERE, in the PR that makes it false, and said so in the commit body rather than letting a deleted pin look like an accident. Keep the `>=` versus `~` distinction: any unpriced volume makes the total a floor. _aggregator.py, ~70 lines._ (M)

#### Group 36: Unified reporting

_Depends on: Group 35_

##### Track 36A: Report every agent the same way
_2 tasks . ~200 LOC . medium risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 35A, 33A, docs/invariants/events-retro.md (Track 23A renderer contract, as revised)_
_produces: tokens and cost split per model family for every agent; commits, LOC and PRs aggregate across all of them_
_blocked-by: Track 35A_
_session: fresh · effort: high · verify: pytest tests/test_retro_fleet_aggregator.py; ruff check ._

_Phase 3's end-state lands here. Ordered BEFORE Groups 37-38 deliberately: the packer's first solution buried it behind two internal-quality Tracks, which is the wrong order for the only user-visible payoff in the Phase._

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
_session: fresh · effort: medium · verify: pytest tests/test_host_usage.py; ruff check ._

_Filed from Track 32A's `/review`. Measured 2026-08-28 on a 747-rollout / 694 MB corpus: 72,654 states, cache **0.40 MB → 13.48 MB**, json round-trip **56.3 ms of the 250 ms** autopush host budget. `locked_json_rmw` parses and re-serialises the whole file every push, so the cost is paid per push and scales linearly. At roughly 4x this corpus it consumes the budget and the reader can never converge — the same wedge `_validated_grok_entry` already has. **Trigger: round-trip above 100 ms, or cache above 25 MB.**_

- **Store per-state increments instead of absolute cumulatives** -- identity survives because both sides reconstruct from the same running sum, and increments are ~5 digits against ~8. Absorbs the filed Grok item: `_validated_grok_entry` requires `offset == size`, so a ledger that cannot be read end-to-end in one budget discards all its work forever — 60 ms wedges permanently at 15 files on the live corpus. Same persisted-offset fix `token_usage.walk_jsonl_segment` shipped for Claude in v0.12.15. _host_usage.py, ~90 lines._ (M)
- **Bound a single entry** -- a per-entry cap needs a degradation that keeps the file's tokens counted while dropping only its cross-file dedup. Refusing the file would re-create the fail-closed whole-store pathology Track 31A removed. `iter_bounded_lines` bounds line SIZE, not line COUNT; max observed is 1,234 states. _host_usage.py, ~50 lines._ (S)

#### Group 38: Walker substrate

_Depends on: Group 37_

##### Track 38A: One walker, three adapters
_3 tasks . ~400 LOC (net NEGATIVE) . medium risk . 4 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/token_usage.py, tests/test_host_usage.py, tests/test_token_usage.py, tests/test_module_boundaries.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 37A, 33A, docs/invariants/events-retro.md (incremental-resume section)_
_produces: one cache/resume/fingerprint implementation shared by Claude, Codex, Grok and OpenCode_
_blocked-by: Track 37A_
_session: fresh · effort: high · verify: pytest tests/test_host_usage.py tests/test_token_usage.py tests/test_module_boundaries.py; ruff check ._

_Measured at HEAD `a7ead32`: `token_usage.py` is 1,856 lines and `host_usage.py` is 1,730, and they are two implementations of one thing. `host_usage.py` imports five names from `token_usage` and used `DayBucket` zero times. Cache version, empty-cache, resume plan, cache-hit validation, head fingerprint, file identity, day extraction and counter coercion are each written two or three times. Four cache files where Claude has one. The genuinely host-specific surface is the record shape only — roughly 300 of those 1,730 lines._

- **Hoist the resume protocol** -- one file-identity + head/tail-digest + complete-line-offset implementation. Keep `iter_bounded_lines` where it is; it is already shared and is the proof the seam works. _host_usage.py + token_usage.py, ~150 lines._ (S)
- **Collapse the per-format readers to adapters** -- after Track 32A all three readers are per-turn with a dedup key (Claude `message.id`, Grok `_grok_terminal_key`, Codex `turn_id`). An adapter's whole job becomes: given a file, yield `(dedup_key, day, model, usage)`. `_aggregate` and `_aggregate_grok` become one function. _host_usage.py, ~180 lines (net negative)._ (M)
- **Retire the duplicated leaf helpers and pin the boundary** -- one day parser, one counter coercion, preserving the trust-boundary split the aggregator documents (`_safe_int` for peer-controlled events, the shared helper for trusted local reads). Extend `tests/test_module_boundaries.py` so a reintroduced private copy fails the build. _host_usage.py + tests, ~70 lines._ (M)


### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not gating.

Adjacency list (from the packer):
```
- Group 32 ← {}
- Group 33 ← {32}
- Group 34 ← {33}
- Group 35 ← {34}
- Group 36 ← {35}
- Group 37 ← {36}
- Group 38 ← {37}
```

Track detail per group:
```
Group 32: Codex per-turn reader
  +-- Track 32A ........... ~L . 4 tasks

Group 33: Per-model host wire
  +-- Track 33A ........... ~L . 3 tasks

Group 34: Coverage reporting
  +-- Track 34A ........... ~M . 2 tasks

Group 35: Host pricing
  +-- Track 35A ........... ~M . 2 tasks

Group 36: Unified reporting
  +-- Track 36A ........... ~L . 2 tasks

Group 37: Cache encoding
  +-- Track 37A ........... ~M . 2 tasks

Group 38: Walker substrate
  +-- Track 38A ........... ~L . 3 tasks
```

**Total: 2 phases . 7 groups . 7 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (63 items)

## Shipped

History: docs/roadmap-shipped.md
