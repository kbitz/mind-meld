# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

Audit configuration (recorded 2026-09-14): release-bearing Tracks here declare 9–14 files (source, tests, invariants, README, plus CHANGELOG / PROGRESS / pyproject) and this repo ships weight-6 sessions as one PR (53A landed 1,790 insertions on a weight-4 card, 57A 1,169 on weight 3), so the roadmap audit runs with `roadmap_max_files_per_track=16` and `roadmap_max_session_weight=6` set through gstack-extend's `bin/config`. The defaults (8 files, weight 4) fail every card in this plan and in the 2026-09-06 plan.

**mm supports three agents: Claude Code, Codex, and Grok Build.** OpenCode was dropped on 2026-09-01 by user decision; Groups 36 and 44 removed it (shipped v0.12.53 → v0.13.0). Do not re-add a fourth agent without a measured need — see the skill-link constraint below, which already refuses one on discovery grounds.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A, and it is why **Grok Build needs no skill link and no sync source** — probed 2026-09-01, `~/.grok/` has no `skills/`, `commands/` or `rules/` directory at all. Grok Build's entire mm surface is the usage reader.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Seven Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **A command that only exists to undo an automatic action is refused until the automatic action is shown to be correct.** v0.12.44 killed `mm uninstall-skills` this way: a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect — the installer recreated a link the user deleted. Fixing the installer made all three unnecessary. Before filing an inverse, check whether the forward action should have happened at all.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`. Two Tracks claiming one version merge cleanly in git, but only one tag can exist for that version — the second's code never gets tagged at all. A dev-dep-only `pyproject.toml` edit no longer force-pushes `latest`: `release.yml` compares `git rev-parse "$tag^{commit}"` to HEAD and skips with a warning. Serialization is still the cheap guard against the silent-lost-code case. See `docs/shared-infra.txt`.
- **The roadmap-staleness gate stays dead.** Track 28B was killed 2026-08-25 on the grounds that an empty Current Plan leaves nothing to drift. Groups 29–35 removed that ground, the question was re-put on 2026-08-25 with seven Groups in flight, and the answer was the same. Do not re-propose it; the design remains recorded in the Group 28 entry of `docs/roadmap-shipped.md` for whoever overrides this.
- **Discovery may read host logs locally, but an encoded cwd never goes on the wire.** Track 29A's prober is a two-level scan of `~/conductor/workspaces/*/*` whose only wire output is a canonical remote URL. Codex `turn_context.cwd` and Grok's URL-encoded session dir names would both yield more roots and are refused — recorded durably as a Non-goal at `docs/designs/host-parity.md:209`; reconsider only if a host supplies a metadata-only index. Confirmed 2026-08-25.
- **A Track that puts a field on a wire, in a cache, or in a log must name its reader in the same card, or declare the reader's Track by number.** Track 34A's review found FOUR producer-without-consumer instances in one pass: `degraded_sources` (shipped v0.12.47, zero readers), `git_capture` (shipped Track 30A, unread by the aggregator), `usageIsIncomplete` (discarded at cache normalization), and the SKILL.md decoder's missing fallback. Reinforced 2026-09-01 by the v0.12.51 conflict-log analysis, which found the conflict-decision collector had been deleted on a premise nobody read, and `synclog.py` still describing the pre-inversion direction four months after the inversion. A write with no reader is not half a feature, it is a liability that reads as one.
- **When a Track touches a reader, check the cache shape, not just the behaviour.** Track 34A verified all six of its card premises as TRUE-or-known-false and was still under-priced 2.5x, because premises describe behaviour while the cost sat in `host_usage._validated_grok_entry`, which normalizes every cached turn to `{key, day, model, usage}` and drops the rest. The existing "check the premise at drain time" constraint worked exactly as written and was insufficient.
- **A Track that prices, sums, or trends a counter must first prove the counter schema of every reader it consumes.** Added 2026-09-01. Track 35A's card was measured against HEAD, its premises were re-verified at drain time, and it was still going to ship a 7.40x error, because every existing constraint checks *behaviour* and *premises* while the defect sat in an undocumented property of the source formats. Codex CLI and Grok CLI report **inclusive** `input` (cache-read already inside it); Claude is **disjoint**. `grok-4.6` appeared under both schemas, so the semantics belong to the READER, not the model id. This is the "check the cache shape" constraint one layer further out: check the SOURCE shape.
- **A feature nobody uses is deleted, not repaired.** Added 2026-09-01. The OpenCode reader had been discarding its entire store since v0.12.30 and a Track was drafted to fix it; probing first showed `opencode.db` 19 days cold, `~/.config/opencode/` three weeks stale and composed entirely of symlinks into a git repo already under version control. The fix was real and the feature was not. Probe the artifact's liveness before pricing its repair.
- **A host usage reader may not ship against a synthetic fixture.** Added 2026-09-01. Its `CONTRACT.md` must record a live census against a real corpus and the host version it was taken from. `tests/fixtures/host_sessions/opencode/CONTRACT.md` said outright "the local machine did not have an OpenCode data directory" and "the SQLite schema is a minimal synthetic contract table." It was the only reader built that way and the only one that returned zero — the root cause Track 36A existed to delete (shipped v0.12.53).

---

## In Progress

### Phase 3: Retro fidelity

**End-state:** The retro presents model usage consistently, states each value's collection scope and coverage, and adds only verified estimates without changing accounting semantics.
**Groups:** 57, 58

Group 57 (Grok pricing) shipped as v0.14.11 and lives in `docs/roadmap-shipped.md`. Group 58 closes the Phase.

#### Group 58: Verified rates and usage presentation

_Depends on: none_

##### Track 58A: Refresh verified rates, re-pin the Grok census, and make model usage easier to read
_3 tasks . ~275 LOC . medium risk . 8 files_
_touches: src/mind_meld/token_usage.py, src/mind_meld/host_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_token_usage.py, tests/test_host_usage.py, tests/test_retro_fleet_aggregator.py, tests/fixtures/host_sessions/grok/CONTRACT.md, docs/invariants/events-retro.md, README.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/events-retro.md (Track 23A renderer contract, cost-estimation section), tests/fixtures/host_sessions/grok/CONTRACT.md, Group 57's entry in docs/roadmap-shipped.md_
_produces: every priced Claude model resolves through a rate card verified on a recorded date, the Grok reader contract names the host version its census actually ran on, and the retro presents usage consistently with explicit source logs, machine scope, observation time, window and coverage_
_session: fresh · effort: high · verify: ./bin/check tests/test_token_usage.py tests/test_host_usage.py tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py_

_Merged 2026-09-14 by user decision (D1 = A): the 2026-09-06 plan's Track 58A (usage presentation) absorbed the Anthropic rate refresh and the Grok census re-pin so one PR closes the retro-fidelity work. The before/after example needs the corrected rates anyway, so the first two tasks land before the third inside the same session._

_Source (rates and census): `[plan-ceo-review]` and `[plan-eng-review]` items filed by the Track 57A /autoplan, 2026-09-14. Verified at a7d9bca: `MODEL_FAMILY_TIERS["sonnet"]` is `_tier(3.0, 15.0)` under a comment calling `$2/$10` an introductory rate that ended 2026-08-31; `fable` and `mythos` are `_tier(10.0, 50.0)`, so a `claude-fable-5-1` record derives a $1.00 cache read through `_CACHE_READ_MULT`; `PRICING_LAST_UPDATED` is `2026-08-11`; `GROK_USAGE_CENSUS_HOST_VERSION` is `"1.0.13"` in `host_usage.py` and the contract header says the same, although Track 57A's census ran on Grok 1.0.25. The pricing page was re-read by /roadmap on 2026-09-14 and agrees with the filer's 2026-09-10 reading: Sonnet 5 is $2/$10 as standard price ("the previously scheduled increase to $3/$15 on September 1, 2026 will not occur"); Sonnet 4.6, 4.5 and 4 stay $3/$15; Fable 5.1 and Mythos 5.1 cache hits are $0.25 (0.025x) while Fable 5 and Mythos 5 keep $1 (0.1x). Both affected models are in the local Claude token cache, so the Claude fleet line is currently overstated. Re-read the page again at implementation time; a number in this card is a premise, not a source._

_Source (presentation): former Track 50A (2026-09-03 numbering), carded as Track 55A in the 2026-09-05 plan and Track 58A in the 2026-09-06 plan, approved full-review H4, and the user-approved pre-existing-roadmap assessment on 2026-09-05. Re-verified at a7d9bca: `_render_agent_inventory` and `AGENT_FAMILY_ROWS` remain; `aggregate` materializes event rows directly and `_read_events` (one definition in `aggregator.py`, zero callers) is still unused. v0.14.11 changed the surface this card renders: `_render_host_economics` carries a vendor-provenance header, `_long_context_cause` explains a Grok `>=` floor, and Grok always renders `>=` with a model-scoped at-most figure only when coverage allows it — the before/after example must include that shipped rendering. Track 57A supplied verified rates or an explicit unpriced result, not permission to change aggregation._

_Boundary verified at f10bf34 and unchanged at a7d9bca: `SessionsAggregate` holds Claude fleet-window totals; `HostUsageInventory` retains accepted snapshots per machine. Day-bucket slicing is NOT one function: `_windowed_host_by_model` (one caller, `_device_economics_cell`) slices `tokens_by_day` only, while `lifetime_by_family` is sliced inline in `_render_agent_inventory` and in the rhythm view, clamped by `_snapshot_day_ceiling` alone. A renderer rewrite must account for all three sites. Contributing readers are recorded per machine, but the wire has no reader-to-model-family attribution. Presentation must preserve those distinctions. New accounting schemas, cross-machine deduplication (deferred again 2026-09-14, see roadmap-future.md) or reader-to-model wire attribution need a separately justified proposal._

- **Refresh the Anthropic rate card** -- re-read the published pricing table, record the date in `PRICING_LAST_UPDATED`, and make the table say what the page says: set the `sonnet` tier to $2/$10 and add `PRICING` overrides at `_tier(3.0, 15.0)` for the Sonnet 4.x ids that normalize into the tier and still bill the old rate (the Opus 4.1 precedent); add full four-field `claude-fable-5-1` and `claude-mythos-5-1` cards with a $0.25 cache read, which `_tier`'s 0.1x multiple cannot express, and leave the `fable` / `mythos` tiers for the 5.0 models. Claim nothing the page does not say. `test_pricing_holds_no_redundant_entries` still passes; update the per-model expectations pinned in `tests/test_token_usage.py`, the "introductory" sentence in events-retro.md, and README's rate-provenance line. _token_usage.py + tests + docs, ~40 lines._ (S)
- **Re-pin the Grok census** -- record Track 57A's 1.0.25 census in the contract (185 ledgers, 312 terminal records, the same four `turn_completed` key sets and two `usage` key sets as 1.0.13, zero drift across 12 patch releases) and move `GROK_USAGE_CENSUS_HOST_VERSION` and its pin test to 1.0.25. Reader behaviour is unchanged. _host_usage.py + CONTRACT.md + the pin test, ~15 lines._ (S)
- **Clarify usage without changing accounting** -- first show a compact before/after rendered example with Claude session data at the refreshed rates, two machines of host inventory, an unpriced Grok model, a Grok `>=` floor and a degraded reader. Use it to settle consistent naming and layout while showing each value's collection scope, observation time, window and coverage. Reuse existing aggregations: host counters never enter the Claude fleet sum; retained inventory stays distinguishable from in-window activity; per-machine host estimates retain the do-not-sum rule. Do not imply per-host model attribution the wire lacks. Preserve the existing global git metrics, unavailable/partial/degraded disclosures, retired-reader tolerance, two-pass skill decoder and no-new-summary-row constraint. Delete the unused `_read_events` iterator and correct its documentation. Pin populated, absent and degraded views plus the one-materialization prior-period path. _aggregator.py + SKILL.md + tests, ~220 lines._ (L)

## Current Plan

_tombstone: 27_

#### Group 59: mm-events root ownership

_Depends on: Group 58_

##### Track 59A: Create the mm-events root only from init and a verified real push
_2 tasks . ~120 LOC . medium risk . 5 files_
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, tests/test_config.py, tests/test_integration.py, tests/test_silent_failure_contract.py, docs/invariants/sync.md, docs/invariants/events-retro.md, README.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 58A_
_read-first: docs/invariants/sync.md (complete snapshots), docs/invariants/events-retro.md (read-only source resolution)_
_produces: a real push never publishes the deletion of previously published event files because mm itself recreated an empty mm-events root, and read-only commands never create the root_
_session: fresh · effort: high · verify: ./bin/check tests/test_config.py tests/test_integration.py tests/test_silent_failure_contract.py tests/test_docs_routing.py_

_Source: [plan-eng-review:severity=moderate] E4, filed by the Track 56A /autoplan, 2026-09-10 (probe s2b in `~/.gstack/projects/kbitz-mind-meld/56a-reproductions.json`). Verified at a7d9bca: `_push_core` calls `resolve_sources(config, strict=True, bootstrap=not dry_run)` and only later `_refuse_unavailable_selected_sources(resolution, remote_manifest, sources)`, so a real push recreates a deleted root before the refusal can see it and then tombstones every event file this Mac published, bypassing sync.md's "a missing previously populated selected root refuses the whole push"; `get_sources` defaults to `bootstrap=True` and is called from status, diag, autopull, pull, resolve and the source-toggle verbs, so the usual state after a root loss is "root present, `events/` gone", which previews and publishes as a plain `- N deleted`. Pull never removes local bytes for a tombstone, so peers keep their copies, but once the tombstones are published this Mac cannot restore them through `mm pull` until `TOMBSTONE_TTL_DAYS` (30) expires. Both CEO voices rejected mirroring the behaviour in the preview through an `assume_empty` option; the Claude eng voice identified the common "`events/` gone" form. v0.14.10's preview refusal is the message to reuse._

- **Only init and a verified real push create the root** -- flip `resolve_sources` / `get_sources` to `bootstrap=False` by default (a missing mm-owned root is already an empty walk with its name in `would_create`) and opt in at exactly the places that write into it: `init` before its backfill, and the publishing paths of `_push_core` (autopush included) and `recapture`, after the check below. Walk the ~20 `get_sources` call sites in cli.py and events_tail.py and pin that status, diag, autopull, pull and the source-toggle verbs no longer `mkdir`; `recapture`'s availability check must not read a non-bootstrapped missing root as "disabled on this Mac". _config.py + cli.py + tests, ~50 lines._ (M)
- **Refuse before recreating a previously populated root** -- in the real push, when mm-events is in `would_create`, or its root exists but holds none of the files the recovered prior manifest lists for it, refuse with a `SnapshotError` that names the two honest ways out: `mm pull` to restore the published event files from a peer while the prior manifest still lists them, or accept the deletion through the existing `mm disable-source mm-events` / `mm enable-source mm-events` path, which drops the entries without minting tombstones. Only then create the root. Re-word v0.14.10's preview refusal to match; keep interactive exit 1 and autopush exit 0 with the typed stderr line and failed breadcrumb. Record the "root present, `events/` gone" transition state in sync.md. _cli.py + tests + docs, ~70 lines._ (M)

#### Group 60: Inspection without repair

_Depends on: Group 59_

##### Track 60A: Inspection commands observe storage and the upgrade cache without repairing them
_2 tasks . ~120 LOC . medium risk . 7 files_
_touches: src/mind_meld/crypto.py, src/mind_meld/cli.py, src/mind_meld/upgrade.py, tests/test_crypto.py, tests/test_diag.py, tests/test_upgrade.py, tests/test_integration.py, docs/invariants/init-devices.md, docs/invariants/auto-upgrade.md, README.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 59A_
_read-first: docs/invariants/init-devices.md (push-preview crypto setup), docs/invariants/auto-upgrade.md (Seam 3)_
_produces: `mm status` and `mm diag` write nothing to shared storage, local config or the upgrade cache and make no network call; every other command reconciles `mm-crypto-init` only after the drift check and passphrase verification pass_
_session: fresh · effort: medium · verify: ./bin/check tests/test_crypto.py tests/test_diag.py tests/test_upgrade.py tests/test_integration.py tests/test_docs_routing.py_

_Source: [plan-ceo-review:severity=moderate] E7 and [plan-ceo-review:severity=minor] E3b, both filed by the Track 56A /autoplan, 2026-09-10 (probe s10 in `56a-reproductions.json`; E7 raised by Codex CEO #3/#4 and Claude CEO #5, reinforced by the Claude DX voice and both eng voices). Verified at a7d9bca: `fetch_crypto_init` canonicalizes the lex-smallest-salt candidate and deletes every conflict copy, unreadable ones included, before `_init_crypto_session` runs its drift check and `verify_passphrase`; `status` calls `_init_crypto_session(backend, passphrase, config)` with the default `read_only=False`, and `diag` and init's storage probe call `fetch_crypto_init(backend)` with the default `repair=True`; `status` then calls `upgrade.check_for_upgrade(config)`, which fetches over HTTP when the cache is stale and rewrites `upgrade-state.json` through `locked_json_rmw` on every call — v0.14.10 corrected the "reads cache only, no network call" comment and Seam 3 line to describe that behaviour rather than fix it. A conflict copy with a different salt is a different encryption lineage; deleting it before anything is verified is irreversible and fleet-wide, and a user inspecting a broken sync reasonably expects `mm status` to preserve evidence. v0.14.10's `repair=` parameter, `CryptoInitRepairPlan`, `read_only=` and `pending=` are the seams. Decision carried by this card: inspection commands may not repair at all._

- **Repair follows verification** -- `_init_crypto_session` fetches with `repair=False` for every caller, runs the drift check and passphrase verification on the winner, and only then applies the repair plan for mutating commands; `status`, `diag` and init's storage probe pass `read_only=True` / `repair=False`, print the pending plan through the existing `repair_note` seam instead of acting on it, and stop persisting a missing fingerprint (in-memory backfill only). The drift error names pending conflict copies in every mode, not only under `read_only`. A command that could not verify the passphrase never deletes a copy. Pin: status and diag with two differing-salt copies leave both on disk; push with a wrong passphrase leaves both; push with the right passphrase reconciles after `verify_passphrase`. _crypto.py + cli.py + tests, ~80 lines._ (M)
- **Give status a cache-only upgrade view** -- `mm status` reads the upgrade cache through `locked_json_snapshot` and neither fetches nor rewrites it; `check_for_upgrade`'s fetch and write-through stay on push, pull, autopull and autopush, whose cache is what status now reports. Restore the "reads cache only, no network call" promise at the status seam and in Seam 3 of auto-upgrade.md, which v0.14.10 rewrote to match the defect. _cli.py + upgrade.py + tests, ~40 lines._ (S)

#### Group 61: Attended host-usage refresh

_Depends on: Group 60_

##### Track 61A: Refresh host usage from an attended push and say what the last push published
_2 tasks . ~130 LOC . medium risk . 7 files_
_touches: src/mind_meld/cli.py, src/mind_meld/events_tail.py, src/mind_meld/events.py, tests/test_integration.py, tests/test_host_usage_snapshot.py, tests/test_diag.py, tests/test_silent_failure_contract.py, docs/invariants/events-retro.md, README.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 60A_
_read-first: docs/invariants/events-retro.md (cursor gate + recapture; host-usage-snapshot section), Group 57's entry in docs/roadmap-shipped.md_
_produces: a converged Mac can refresh its published host usage with one attended command that is not a git verb, and `mm status` says per consented reader whether the last push published it_
_session: fresh · effort: medium · verify: ./bin/check tests/test_integration.py tests/test_host_usage_snapshot.py tests/test_diag.py tests/test_silent_failure_contract.py tests/test_docs_routing.py_

_Source: two `[plan-devex-review]` items filed by the Track 57A /autoplan, 2026-09-14, and the Track 54A T3-B deferral (2026-09-09), whose automatic no-op-push re-read stays deferred (roadmap-future.md). Verified at a7d9bca: `_push_core` prints "Nothing to push" and returns before `events_tail._run_events_tail` when nothing substantive changed, so a converged Mac never refreshes host usage; README's "Grok usage in fleet retro" section and the `deadline` remedy row send users to `mm recapture 1d`, a git-only primitive that requires discovered git roots and exits 4 on partial git recovery, and events-retro.md records it as "the bridge on a converged Mac". For 3.5 weeks `mm status` read "grok prior successful scan: yes" while 0 of 5 host rows carried a Grok token, because nothing reads a row's `token_sources` back. `host-usage-snapshot` is an existing row type with an existing reader (`aggregator._accept_host_usage_snapshot`); this Track adds no wire field._

- **`mm push --capture-usage`** -- on an attended push, run the existing warm/retry host capture with interactive budgets first, write exactly one `host-usage-snapshot` row into mm-events, then run the ordinary push path so the substantive-change gate passes on that row — the recapture shape, with no disjunct on the gate. The events tail must not write a second host row for the same push. Refuse under `--dry-run`, when no reader is consented, or when mm-events is unresolved; autopush never captures this way; `mm recapture` stays git-only. Replace the `mm recapture 1d` bridge in README (the Grok section and the `deadline` remedy row) and in the invariant, and re-word v0.14.8's "the next push that uploads a change retries" to name the flag. Do not add an automatic no-op-push re-read. _cli.py + events_tail.py + tests + docs, ~90 lines._ (M)
- **Say what the last push published** -- `mm status` reads this device's newest `host-usage-snapshot` row locally (a `latest_mm_push_row`-shaped reader, fail-open) and prints, per consented reader, whether it contributed (`token_sources`), was partial or degraded, or has never been published; `mm diag --json` carries the same under the existing host keys with their README pins. No wire change. _cli.py + events.py + tests, ~40 lines._ (S)

#### Group 62: Write-free previews

_Depends on: Group 61_

##### Track 62A: Make the other previews write-free
_1 task . ~250 LOC . medium risk . 6 files_
_touches: src/mind_meld/cli.py, src/mind_meld/resolveflow.py, tests/test_integration.py, tests/test_silent_failure_contract.py, tests/test_track_30a.py, tests/test_retention.py, docs/invariants/sync.md, docs/invariants/events-retro.md, docs/invariants/auto-upgrade.md, docs/invariants/conflicts.md, README.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 61A_
_read-first: 59A, 60A, docs/invariants/events-retro.md (`dry_run` no-op contract)_
_produces: `mm pull --dry-run`, `mm gc --dry-run`, `mm recapture --dry-run`, `mm migrate-config --dry-run` and `mm diff` change nothing on disk or in storage, and a registry ratchet keeps every future `dry_run` parameter inside that contract_
_session: fresh · effort: high · verify: ./bin/check tests/test_integration.py tests/test_silent_failure_contract.py tests/test_track_30a.py tests/test_retention.py tests/test_docs_routing.py_

_Source: [plan-ceo-review:severity=moderate] 56B, filed by the Track 56A /autoplan, 2026-09-10 (probes s8, s9, s11, s12, s13 in `56a-reproductions.json`; plan `~/.gstack/projects/kbitz-mind-meld/ceo-plans/2026-09-10-track-56a.md`). Verified at a7d9bca: all five commands promise no writes in their help or output and all five run `_get_config()` and `_init_crypto_session` with the defaults, so they patch a missing fingerprint, append the transition-hook pull-history row and rewrite the upgrade cache; `recapture --dry-run` said "nothing written" after writing four things until v0.14.10 corrected only the wording. Pull has writes no setup knob touches: `resolveflow._find_conflict_files(config, migrate_pre_inversion=True)` renames pre-v0.9.2 conflict files, and the `action="excluded"` pull-history loop is gated only on `not quiet`. 56A's first draft widened to all six previews and both CEO voices rejected it as under-inventoried; this card carries the inventory. After 59A and 60A the setup helpers no longer bootstrap or repair on their own, which shrinks this to the per-command writes._

- **Inventory each preview's own writes, then thread the read-only knobs** -- for each of the five commands, list every write on its path (setup helpers, conflict migration, pull-history rows, upgrade cache, mm-events root), then pass v0.14.10's knobs (`_get_config(read_only=)`, `_init_crypto_session(read_only=, pending=)`, `resolve_sources(bootstrap=False)`, the nudge gate) and gate the writes no knob covers: pull's conflict-file migration and its `excluded` history rows. `recapture --dry-run` must not read a non-bootstrapped missing root as "disabled on this Mac". Reuse 56A's audit-hook contract fixture per command with deep fixtures (a peer, excluded paths, a pre-v0.9.2 conflict file). Add the registry ratchet: every command with a `dry_run` parameter is in the contract list or explicitly exempted, and `mm diff` gets a manual entry. Each preview's help text says what stays untouched and ends with the lock-qualified completion line push already prints. _cli.py + resolveflow.py + tests + invariants, ~250 lines._ (L)

### Execution Map

**This adjacency is RELEASE order, not launch order.** Every edge is release serialization on `pyproject.toml`: five cards claim five consecutive versions, and only one tag can exist per version. One edge is also a data dependency: 59A / 60A → 62A (the setup helpers stop bootstrapping and repairing on their own before the previews are inventoried). Tracks may be worked in parallel Conductor workspaces; only their version slots serialize, and document order is priority. Retro fidelity finishes first: its one remaining card corrects a fleet line that is overstated today and lands the longest-carried card; the defect cards behind it can be implemented meanwhile.

Adjacency from gstack's `roadmap-pack` tool on the drafted Tracks (identical to the audit's GROUP_DEPS after apply; this is the `/roadmap` skill's own packer, not a script in this repo's `bin/`):

```
- Group 58 ← {}
- Group 59 ← {58}
- Group 60 ← {59}
- Group 61 ← {60}
- Group 62 ← {61}
```

Track detail per group:

```
Group 58: Verified rates and usage presentation   (Retro fidelity)
  +-- Track 58A ........... ~L . 3 tasks
Group 59: mm-events root ownership
  +-- Track 59A ........... ~L . 2 tasks
Group 60: Inspection without repair
  +-- Track 60A ........... ~M . 2 tasks
Group 61: Attended host-usage refresh
  +-- Track 61A ........... ~M . 2 tasks
Group 62: Write-free previews
  +-- Track 62A ........... ~L . 1 task
```

**Total: 5 groups . 5 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (86 items)

## Shipped

History: docs/roadmap-shipped.md
