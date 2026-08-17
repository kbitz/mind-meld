<!-- /autoplan restore point: /Users/kb/.gstack/projects/kbitz-mind-meld/kbitz-extract-event-capture-autoplan-restore-20260815-143545.md -->

# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

---

## In Progress

_(none)_

---

## Current Plan

#### Group 22: Host snapshot merge ∥ Grok customization source

##### Track 22A: Merge accepted host snapshots into the retro result
_3 tasks . ~150 LOC . medium risk . 3 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, docs/invariants/events-retro.md, tests/test_retro_fleet_aggregator.py_
_out: 22B, 23A, 23B_
_read-first: docs/invariants/events-retro.md_
_produces: latest complete host snapshot per device, window-sliced, with coverage_
_session: fresh · effort: high · attach: @src/mind_meld/skills/retro_fleet/aggregator.py, @tests/test_retro_fleet_aggregator.py, @docs/invariants/events-retro.md · verify: pytest tests/test_retro_fleet_aggregator.py -q_
- **Accept only whole, valid device views** -- validate the host-snapshot wire contract and select the latest complete row per device without per-source carry-forward. _aggregator.py + tests, ~65 lines._ (M)
- **Slice and merge host usage by window** -- retain host-family UTC buckets independently of Claude session tokens, then merge selected device views. _aggregator.py + tests, ~50 lines._ (M)
- **Carry coverage forward honestly** -- expose consulted sources and snapshot as_of state so absence, opt-out, and stale observations never become zero. _aggregator.py + invariant/tests, ~35 lines._ (S)

##### Track 22B: Add a grok-custom allowlisted sync source
_3 tasks . ~140 LOC . high risk . 6 files_
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, tests/test_config.py, tests/test_source_toggle.py, docs/invariants/sync.md, README.md_
_out: 22A, 23B_
_read-first: docs/designs/host-parity.md, src/mind_meld/config.py_
_produces: grok-custom DEFAULT_SOURCES allowlist; grok remains a usage-only name_
_session: fresh · effort: high · attach: @src/mind_meld/config.py, @tests/test_config.py, @docs/designs/host-parity.md · verify: pytest tests/test_config.py tests/test_source_toggle.py -q_
- **Lock the allowlist from a live GROK_HOME inspect** -- include only skills/, commands/, and rules/; leave hooks/ and plugins/ out until inspected. _design + config comments, ~20 lines._ (S)
- **Add grok-custom to DEFAULT_SOURCES** -- generated-skill excludes matching Codex; a sync source named grok stays a ConfigError. _config.py, cli.py, tests, ~80 lines._ (M)
- **Pin a mixed fixture uploads nothing secret** -- sessions/, auth.json, config.toml, chat_history.jsonl, logs, and worktrees stay off the wire. _tests, ~40 lines._ (S)

#### Group 23: MODELS card coverage ∥ Grok skill-link
_Depends on: Group 22_

##### Track 23A: Render Claude and host model families without false precision
_3 tasks . ~110 LOC . medium risk . 3 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py_
_blocked-by: Track 22A_
_out: 23B_
_read-first: 22A, docs/invariants/events-retro.md_
_produces: MODELS card with Claude session totals beside coverage-aware host families_
_session: fresh · effort: medium · attach: @src/mind_meld/skills/retro_fleet/aggregator.py, @src/mind_meld/skills/retro_fleet/SKILL.md, @tests/test_retro_fleet_aggregator.py · verify: pytest tests/test_retro_fleet_aggregator.py -q_
- **Compose a display-only family view** -- show Claude session totals beside Codex/Grok/other host totals without feeding host values into Claude API cost estimation. _aggregator.py + tests, ~45 lines._ (S)
- **Name coverage in the card and narrative** -- replace the Claude-only claim with concise snapshot coverage, missing-device, opt-out, and freshness context. _aggregator.py + SKILL.md + tests, ~45 lines._ (S)
- **Preserve the card contract** -- keep width, deterministic ordering, and all existing Claude-only fallback behavior pinned. _aggregator.py + tests, ~20 lines._ (S)

##### Track 23B: Install retro-fleet into Grok skills
_2 tasks . ~100 LOC . medium risk . 4 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/test_skill_link.py, docs/invariants/events-retro.md_
_blocked-by: Track 22A, Track 22B_
_out: 23A_
_read-first: 22A, 22B, docs/designs/host-parity.md, src/mind_meld/skill_link.py_
_produces: fourth SkillTarget at GROK_HOME/skills; no grok sync source_
_session: fresh · effort: medium · attach: @src/mind_meld/skill_link.py, @tests/test_skill_link.py · verify: pytest tests/test_skill_link.py -q_
- **Add a Grok SkillTarget** -- resolve GROK_HOME/skills at call time with own 24h markers and the no-clobber state machine. _skill_link.py + tests, ~70 lines._ (M)
- **Report the fourth target** -- mm install-skills lists Grok; do not create a grok sync source. _cli.py + events-retro.md + tests, ~30 lines._ (S)

### Execution Map

A Group may launch when every Group in its `←` set has landed, regardless
of document order; document order is priority, not a gate.

```
- Group 22 ← {}
- Group 23 ← {22}
```

Track detail per group:

```
Group 22: Host snapshot merge ∥ Grok customization source
  +-- Track 22A ........... ~M . 3 tasks
  +-- Track 22B ........... ~M . 3 tasks

Group 23: MODELS card coverage ∥ Grok skill-link
  +-- Track 23A ........... ~M . 3 tasks
  +-- Track 23B ........... ~S . 2 tasks
```

**Total: 0 phases . 2 groups . 4 tracks remaining.**

---

## Future

- **cli.py micro-cleanups** — retain only the unresolved `_resolve_mm_events_dir` and status-enum follow-up; Track 18A owns the import and `_empty_outcomes` portions.
- **`_resolve_interactive_loop` decomposition** — collector removal fired its old trigger, but the 630-line function still needs a dedicated discovery/design pass before code commitment.
- **Two-machine test bootstrap duplication** — consolidate the repeated setup only when adjacent test work touches both modules.
- **Cold-cache budget leftovers** — revisit identity-refresh and per-jsonl deadline concerns only with fresh measurements; root discovery is shipped.
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
- **Host-usage cache GC reaper** — extend `mm gc` and its dry-run path to remove stale Codex, Grok, and OpenCode cache entries without weakening complete-pass pruning. _Source: unprocessed host-cache follow-up 2026-08-17._
- **Active host-session degradation policy** — consider skipping a stale or partial final rollout when its next completed record restates usage; preserve all-or-nothing publication until that proof exists. _Source: unprocessed host-usage follow-up 2026-08-17._
- **Warm host-scan scaling** — revisit fingerprint-every-file cost with a measured corpus before the 250 ms autopush budget becomes user-visible. _Source: unprocessed host-cache follow-up 2026-08-17._
- **Machine-readable GC outcomes** — expose orphan-blob outcomes only when an automation or audit consumer needs them; Track 17D's reaper scope stays as shipped. _Source: unprocessed GC follow-up 2026-08-17._
- **Do not add a Codex or Grok sessions-snapshot** — Claude's sessions-snapshot walk stays Claude-only. Codex rollouts and Grok session dirs are not a metadata-only project ledger; encoded cwd is a path and must not go on the wire. Promote only if a host ships a metadata-only project index. Session-transcript sync stays refused for every host. _Source: [manual] host-parity 2026-08-17._

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

### Group 17: Post-extraction and host-reader foundations ✓ Shipped (v0.12.22–v0.12.25)

- Track 17A — _shipped (v0.12.24): descriptor-driven Claude/Codex/OpenCode skill installation with truthful partial results._
- Track 17B — _shipped (v0.12.25): one private event-capture path shared by push and init while retaining their separate policies, writes, and budgets._
- Track 17C — _shipped (v0.12.24): canonical host-family classification, isolated incremental Codex usage cache, and bounded local reader._
- Track 17D — _shipped (v0.12.23): retention reapers plan before mutation, including byte/metadata-preserving dry-run coverage._
- Track 17E — _shipped (v0.12.22): distinct repository-qualified GitHub PR identity in retro aggregation._

### Group 19: Host snapshot writer and resolver hygiene ✓ Shipped (v0.12.32–v0.12.33)

- Track 19A — _shipped (v0.12.33): additive, all-or-nothing host-usage snapshots from the event tail and init backfill._
- Track 19B — _shipped (v0.12.32): resolver continuation and skip-default cleanup following the rendering seam._

### Group 20: Host snapshot wire contract ✓ Shipped (unreleased)

- Track 20A — _shipped: documented and pinned the complete, coverage-aware snapshot contract that distinguishes omission from an explicit empty observation._

### Group 18: Finish the non-Claude host-usage foundation ✓ Shipped (v0.12.26–v0.12.34)

The host reader, model-family card baseline, root budgets, CLI safety work,
and the Grok v1 terminal-usage reader have landed.

- Track 18A — _shipped (v0.12.26): post-decomposition CLI safety sweep._
- Track 18B — _shipped (v0.12.31): centralize conflict diff rendering and choice normalization._
- Track 18C — _shipped (v0.12.28): budget root discovery and no-op heartbeat behavior._
- Track 18E — _shipped (v0.12.29): render model-family rows from existing data._
- Track 18D — _shipped (v0.12.34): read Grok Build's v1 terminal usage ledger._

### Group 21: Opt in and publish Grok usage snapshots ✓ Shipped (v0.12.34)

- Track 21A — _shipped (v0.12.34): gate and publish trusted Grok usage. `mm enable-source grok` is a usage bit, not a sync source._
