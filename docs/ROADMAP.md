<!-- /autoplan restore point: /Users/kb/.gstack/projects/kbitz-mind-meld/kbitz-extract-event-capture-autoplan-restore-20260815-143545.md -->

# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

---

## In Progress

(none)

---

## Current Plan

The next batch begins from the v0.12.21 six-module `cli.py` extraction. Groups are launch batches emitted by `roadmap-pack`; document order is priority, while the Execution Map is the launch gate.

#### Group 17: Stabilize post-extraction seams

_Depends on: none_

##### Track 17A: Complete the three-agent skill installer
_4 tasks . ~140 LOC . medium risk . 3 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/test_skill_link.py_
_out: 18A_
_read-first: docs/invariants/events-retro.md_
_produces: One target registry and truthful per-agent installation results._
- **Table-drive agent targets** — make one target record own path, markers, and display name. _skill_link.py, ~50 lines._ (S)
- **Align repair gates with installation** — use the installed-agent predicate in every gate. _skill_link.py, ~25 lines._ (S)
- **Report partial installs honestly** — add the missing failure bucket and non-zero command exit. _skill_link.py, cli.py, ~35 lines._ (S)
- **Pin composition and notices** — cover gate/installer behavior and target-specific failures. _tests, ~30 lines._ (S)

##### Track 17B: Extract the shared event-capture path
_2 tasks . ~100 LOC . medium risk . 3 files_
_touches: src/mind_meld/events_tail.py, tests/test_events_budget_scope.py, tests/test_init_events_backfill.py_
_out: 18C_
_read-first: docs/invariants/events-retro.md_
_produces: One capture seam shared by push-tail and init-backfill._
- **Extract one capture primitive** — share gates, deadline setup, and snapshot assembly between tail and backfill. _events_tail.py, ~80 lines._ (M)
- **Remove duplicated token-cache looping** — preserve cache-lock semantics with one loop shape. _events_tail.py, ~20 lines._ (S)

##### Track 17C: Build the host-usage cache and Codex walker
_3 tasks . ~170 LOC . high risk . 3 files_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, tests/fixtures/host_sessions/_
_out: 18D, 18E_
_read-first: docs/invariants/events-retro.md_
_produces: An isolated, incremental Codex usage reader and the canonical host-family classifier._
- **Define host families and buckets** — make `host_usage` the only classifier authority. _host_usage.py, ~40 lines._ (S)
- **Read Codex cumulative session totals** — use the final `token_count` event per rollout. _host_usage.py + fixture, ~80 lines._ (M)
- **Store incremental state separately** — isolate host-token cache from Claude session-token cache. _host_usage.py + tests, ~50 lines._ (S)

##### Track 17E: Add PR identity to the retro aggregate
_2 tasks . ~55 LOC . low risk . 2 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py_
_out: 18E_
_read-first: docs/invariants/events-retro.md_
_produces: Deduplicated pull-request totals in the aggregate and snapshot metrics._
- **Extract PR numbers from commit subjects** — support the GitHub forms used by the repository. _aggregator.py, ~30 lines._ (S)
- **Persist aggregate PR metrics** — add compatible snapshot fields and pins. _aggregator.py + tests, ~25 lines._ (S)

#### Group 18: Use the new seams ∥ first model-card slice

_Depends on: Group 17_

##### Track 18A: Finish the post-decomposition CLI safety sweep
_4 tasks . ~220 LOC . medium risk . 5 files_
_blocked-by: Track 17A_
_touches: src/mind_meld/cli.py, src/mind_meld/events.py, src/mind_meld/config.py, tests/test_safe_str.py, tests/test_silent_failure_contract.py_
_out: 19A_
_read-first: CLAUDE.md, docs/invariants/events-retro.md_
_produces: Sanitized peer output and shared auto-command control flow._
- **Sanitize remaining peer-controlled output** — close CLI, events, and config print sites with escape-injection pins. _src + tests, ~45 lines._ (S)
- **Unify auto-command setup and tails** — remove duplicated setup and exception-tail control flow. _cli.py, ~90 lines._ (S)
- **Remove remaining function-local reimports** — retain only proven cycle boundaries. _cli.py, ~40 lines._ (S)
- **Collapse duplicate source walks and race handling** — preserve substantive-change and crypto-race contracts. _cli.py + tests, ~45 lines._ (S)

##### Track 18C: Budget root discovery and define no-op heartbeat behavior
_2 tasks . ~85 LOC . medium risk . 3 files_
_blocked-by: Track 17B_
_touches: src/mind_meld/events_tail.py, tests/test_events_budget_scope.py, tests/test_events.py_
_out: 19A_
_read-first: docs/invariants/events-retro.md_
_produces: A call-scoped root-discovery cache with explicit no-op event policy._
- **Memoize and budget git-root discovery** — keep the cache call-scoped and report discovery degradation. _events_tail.py, ~55 lines._ (S)
- **Settle the substantive-change heartbeat** — make the no-op daily-event policy explicit and test it. _events_tail.py + tests, ~30 lines._ (S)

##### Track 18D: Read Grok Build and OpenCode usage
_2 tasks . ~130 LOC . high risk . 3 files_
_blocked-by: Track 17C_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, tests/fixtures/host_sessions/_
_out: 19A_
_read-first: docs/invariants/events-retro.md_
_produces: Fault-tolerant Grok and OpenCode usage adapters._
- **Add the Grok completed-turn reader** — ignore context-window totals and unfinished sessions. _host_usage.py + fixture, ~70 lines._ (M)
- **Add the OpenCode read-only reader** — tolerate missing or busy databases. _host_usage.py + tests, ~60 lines._ (S)

##### Track 18E: Render model-family rows from existing data
_2 tasks . ~115 LOC . medium risk . 3 files_
_blocked-by: Track 17C, Track 17E_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py_
_out: 21A_
_read-first: docs/invariants/events-retro.md_
_produces: A width-pinned MODELS card baseline with token and PR totals._
- **Use the host-usage classifier in the aggregator** — avoid a second model-family authority. _aggregator.py, ~30 lines._ (S)
- **Render the MODELS card baseline** — update output and skill copy within the width contract. _aggregator.py + skill + tests, ~85 lines._ (M)

#### Group 19: Host snapshot writer ∥ resolve-flow hygiene

_Depends on: Group 18_

##### Track 19A: Emit host-usage snapshots from the event tail
_3 tasks . ~150 LOC . medium risk . 4 files_
_blocked-by: Track 18A, Track 18C, Track 18D_
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/test_events.py, tests/test_init_events_backfill.py_
_out: 20A_
_read-first: docs/invariants/events-retro.md_
_produces: Additive host-usage snapshot rows from tail and init-backfill._
- **Define the additive snapshot schema** — include token sources, hosts, and canonical active days. _events.py, ~45 lines._ (S)
- **Write tail and backfill snapshots** — preserve cold-cache and dry-run omission semantics. _events_tail.py, ~75 lines._ (M)
- **Pin active-day attribution inputs** — keep cwd-only sessions token-visible but unattributable. _events.py + tests, ~30 lines._ (S)

#### Group 20: Host snapshot contract

_Depends on: Group 19_

##### Track 20A: Lock the host snapshot wire contract
_2 tasks . ~70 LOC . low risk . 3 files_
_blocked-by: Track 19A_
_touches: tests/test_events.py, tests/test_init_events_backfill.py, docs/invariants/events-retro.md_
_out: 21A_
_read-first: docs/invariants/events-retro.md_
_produces: A documented D4 contract that distinguishes omitted from empty snapshots._
- **Pin omission versus empty snapshots** — prevent a cold push from erasing a warm result. _tests, ~45 lines._ (S)
- **Document the wire invariants** — record source-specific counting and budget behavior. _docs, ~25 lines._ (S)

#### Group 21: Fleet host aggregation

_Depends on: Group 18, Group 20_

##### Track 21A: Merge host snapshots into the retro result
_3 tasks . ~95 LOC . medium risk . 3 files_
_blocked-by: Track 18E, Track 20A_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py_
_out: 22A_
_read-first: docs/invariants/events-retro.md_
_produces: Latest-present host results, Claude-only pricing, and honest fleet-gap notes._
- **Select the latest present host snapshot** — avoid warm-then-cold erasure. _aggregator.py, ~40 lines._ (S)
- **Keep Claude pricing isolated** — maintain a sibling host-token map. _aggregator.py, ~30 lines._ (S)
- **Explain mixed-fleet gaps** — distinguish no snapshot from an explicit empty one. _aggregator.py + skill + tests, ~25 lines._ (S)

#### Group 22: Model-card PR attribution

_Depends on: Group 21_

##### Track 22A: Attribute PRs and complete the model card
_2 tasks . ~90 LOC . medium risk . 3 files_
_blocked-by: Track 21A_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, docs/invariants/events-retro.md, tests/test_retro_fleet_aggregator.py_
_read-first: docs/invariants/events-retro.md_
_produces: Model-card PR attribution with explicit unknown and mixed outcomes._
- **Attribute each PR by same-day host activity** — render unknown and mixed outcomes honestly. _aggregator.py, ~50 lines._ (S)
- **Finish card rows and metrics** — preserve width and additive snapshot compatibility. _aggregator.py + tests + docs, ~40 lines._ (S)

### Execution Map

A Group may launch when every Group in its `←` set has landed; document order is priority, not a gate.

```
- Group 17 ← {}
- Group 18 ← {17}
- Group 19 ← {18}
- Group 20 ← {19}
- Group 21 ← {18, 20}
- Group 22 ← {21}
```

Track detail per group:

```
Group 17: Stabilize post-extraction seams
  +-- Track 17A ........... ~M . 4 tasks
  +-- Track 17B ........... ~M . 2 tasks
  +-- Track 17C ........... ~M . 3 tasks
  +-- Track 17E ........... ~S . 2 tasks

Group 18: Use the new seams ∥ first model-card slice
  +-- Track 18A ........... ~M . 4 tasks
  +-- Track 18C ........... ~M . 2 tasks
  +-- Track 18D ........... ~M . 2 tasks
  +-- Track 18E ........... ~M . 2 tasks

Group 19: Host snapshot writer ∥ resolve-flow hygiene
  +-- Track 19A ........... ~M . 3 tasks

Group 20: Host snapshot contract
  +-- Track 20A ........... ~S . 2 tasks

Group 21: Fleet host aggregation
  +-- Track 21A ........... ~M . 3 tasks

Group 22: Model-card PR attribution
  +-- Track 22A ........... ~M . 2 tasks
```

**Total: 0 phases . 6 groups . 12 tracks remaining.**

---

## Future

- **cli.py micro-cleanups** — retain only the unresolved `_resolve_mm_events_dir` and status-enum follow-up; Track 18A owns the import and `_empty_outcomes` portions.
- **`_resolve_interactive_loop` decomposition** — collector removal fired its old trigger, but the 630-line function still needs a dedicated discovery/design pass before code commitment.
- **Two-machine test bootstrap duplication** — consolidate the repeated setup only when adjacent test work touches both modules.
- **Cold-cache budget leftovers** — retain identity-refresh and per-jsonl deadline concerns; Track 18C owns root discovery.
- **identity.py micro-DRY and token-cache pins** — keep as low-priority hygiene.
- **v0.11.17 doc-drift cleanup** — re-evaluate only with an invariant-doc pass.
- **Incremental-resume accepted divergences** — act only on evidence from a corpus census.
- **Future-clamped peer mtime** — advisory watch item.
- **`_promote_target_will_sync` ignores `exclude_patterns`** — rare exclude-glob miss.
- **Similarity classifier and silent merge** — blocked on a real collector dataset.
- **Peers never resolved against can be mtime-skipped by the drain** — watch after Group 12's shipped fix.
- **Abort transactionality** — pre-existing torn-state concern.
- **Price-cache TTL split** — wire-format work that competes with token-cache ownership.
- **Model-ID variant suffix aliases** — defer until a real variant appears in a census.
- **Rendered-cost sanity ceiling** — presentation safeguard without an observed bad value.
- **Parallel blob fetch** — revisit after user-reported sustained slow pulls.
- **Selective sync** — wait for a user with a large-project filtering need.
- **Mtime hash cache** — revisit only if push latency becomes user-visible again.
- **Three-way merge base** — wait for a divergence-misclassification report.
- **`mm rekey` passphrase rotation** — post-1.0 format-v3 migration work.
- **Blob-directory peer recovery** — wait for a real corrupt-manifest support case.
- **PyPI publish workflow** — wait for a distribution-demand signal.
- **Cross-device source rename identity** — known limitation until an incident demonstrates it.
- **Explicit upgrade check** — add only when cached status is insufficient.
- **Subprocess pipx upgrade** — revisit after nudge UX proves inadequate.
- **`MM_NO_VERSION_CHECK=1`** — add only for demonstrated CI ergonomics.
- **GitHub-tag pagination** — deferred until tag count approaches the API page limit.
- **Autouse devices-write-lock coupling** — forward-defense only.
- **`[retro].deny_emails`** — wait for a credential or account-hygiene need.
- **Snapshot-level completeness** — separate data-model design; do not conflate with host snapshots.
- **Retro-card machine/cause diagnostics** — defer until the baseline model card lands.
- **`mm diag` discoverability and analytics** — keep scoped to the existing command rather than adding a new one.
- **Diagnostic-string quality pass** — adopt only with a broader user-facing notice pass.
- **Per-verb or sticky autorun breadcrumbs** — the v0.12.16 signal is an improvement but still overwritable.
- **CT-4 enforcement and short-write handling** — storage-boundary hardening outside this batch.
- **Residual Track 16A coverage gaps** — three defensive or theoretical cases from the ship audit. _Source: [ship] coverage audit 2026-08-15._
- **Pre-0.11 PROGRESS backfill** — nine historical release rows; the parity gate already prevents recurrence. _Source: [ship] 2026-08-15._

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
- Track 5C — _shipped (v0.9.1): exclude_patterns + log + migration UX. 38 tests + 5 IRON RULE pins. Pivoted via /plan-ceo-review from conflict inversion + real-merge backends._
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
- _Mid-upgrade peer pre-v0.11.0 breadcrumb persists after upgrade [adversarial 2026-04-28]: window naturally moves past v=1 snapshots within 7-30 days._

#### Group 9: Pull performance + fresh-Mac onboarding ✓ Shipped (v0.11.23)

Surfaced 2026-04-27 from a pull-perf dogfood session on kb's 349C-kb-ms. Scope reduced via /plan-eng-review (2026-05-06) from a 2-task plan paired with 150-line parallel-fetch optimization to a 5-line auto-pin nudge — once storage is pinned, `mm pull` reads resident blobs and is already fast (<5s on 1449-blob workload).

- Track 9A — _shipped (v0.11.23): auto-pin iCloud storage on `mm init` via `brctl download`. 1 task shipped._

#### Group 10: Token-usage post-ship cleanup ✓ Shipped (v0.11.24)

Four DRY + perf items deferred from /ship pre-landing reviews of the v0.11.14+ token-usage work. All scoped to internal hygiene — no public-API change, no user-visible behavior change.

- Track 10A — _shipped (v0.11.24): token-usage DRY + perf polish. Consolidated 4 bucket-merge sites behind `merge_usage_bucket` / `merge_by_model` + `TOKEN_FIELDS` constant + `zero_day_bucket` / `zero_model_bucket` factories. 4 tasks shipped._

### Group 11: Token-cache + cold-cache correctness fixes ✓ Shipped (v0.12.4–v0.12.5)

The two `necessary` correctness fixes from /full-review 2026-05-10. Loose Group — post-1.0, outside the v0.x → v1.0 sweep above.

- Track 11A — _shipped (v0.12.5): token-cache invariant ownership consolidation — autouse `_isolate_token_cache` fixture + `gc_cache_entries` routed through `lock_and_get_files`. Cross-model HIGH unknown-top-level-key-stripping regression caught and fixed during /review. 5 new pinning tests._
- Track 11B — _shipped (v0.12.4): cosmetic-only Skills incomplete breadcrumb admits cold-cache push as a second cause. Original Option B 3-LOC events.py fix rejected during /plan-eng-review — would cause latest-snapshot-wins data erasure on warm-then-cold push ordering._

### Group 12: inline keep-canonical mtime bump ✓ Shipped (v0.12.7)

Deferred the inline `keep-canonical` mtime bump to end-of-pull-batch so `mm pull --conflict-mode prompt` choosing local propagates across the fleet without mid-walk later-peer skip.

- Track 12A — _shipped (v0.12.7): `pending_inline_bumps` drained after all peer walks. 13 tests in `TestResolveLocalMtimeBump`._

### Group 14: Symlink policy on both push and apply paths ✓ Shipped (v0.12.17)

Child symlinks are now local routing rather than sync content: generic walkers omit them, pull preserves live and dangling local links, and source roots may still be symlinked. Prior-manifest filtering prevents this policy change from generating deletion tombstones; generated Codex/OpenCode skill trees are excluded without excluding hand-authored skills.

- Track 14A — _shipped (v0.12.17): tombstone-safe child-symlink suppression, pull-time link preservation, generated-skill exclusions, invariant documentation, and regression coverage._

### Group 13: Hotfix: events-tail malformed-byte resilience ✓ Shipped (v0.12.16)

- Track 13A — _shipped (v0.12.16): binary-safe session reads, degradation breadcrumb, and regression coverage._

### Group 15: Post-ship cleanup sweep ✓ Shipped (v0.12.18–v0.12.20)

- Track 15A — _shipped (v0.12.18): remove obsolete token-usage exports and migrate their test coverage._
- Track 15B — _shipped (v0.12.20): remove frozen state-path constants and the dead upgrade import._
- Track 15C — _shipped (v0.12.19): make retro-aggregator imports explicit and remove the obsolete importer._

### Group 16: cli.py decomposition ✓ Shipped (v0.12.21)

- Track 16A — _shipped (v0.12.21): remove the unused collector, extract six cohesive modules, tighten isolation and routing gates, and update release documentation._

### Track 18B: Centralize conflict diff rendering and choice normalization ✓ Shipped (v0.12.31)

- Track 18B — _shipped (v0.12.31): one capped terminal-safe renderer and one compatibility-choice normalization path, with the existing 60- and 80-entry prompt windows preserved. 3 tasks shipped._

### Track 19B: Finish small resolveflow cleanup after the rendering seam ✓ Shipped (v0.12.32)

- Track 19B — _shipped (v0.12.32): hoisted the resolver's residual manifest imports, flattened the already-continued merge branch, corrected the skip-default safety contract, and pinned continuation after a failed first merge write._

### Track 17B: Shared event capture ✓ Shipped (v0.12.25)

- Track 17B — _shipped (v0.12.25): extracted one private event-snapshot path for push and init while retaining their separate policies, writes, and budgets._

### Track 17D: Honest GC reaper dry-runs ✓ Shipped (v0.12.23)

- Track 17D — _shipped (v0.12.23): make every retention reaper plan before mutation, make token-cache preview read-only under a shared lock, report candidates and failures truthfully, and pin dry-run byte/metadata preservation plus partial-delete behavior._
