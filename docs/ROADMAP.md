# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

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

_No partially shipped Groups remain after reconciling v0.14.2 through v0.14.5._

## Current Plan

_tombstone: 27_

#### Group 52: Terminal-control postcondition

_Depends on: none_

##### Track 52A: Enforce a no-control postcondition in the shared sanitizers
_2 tasks . ~80 LOC . medium risk . 6 files_
_touches: src/mind_meld/safety.py, src/mind_meld/retention.py, src/mind_meld/token_usage.py, tests/test_safe_str.py, tests/test_retention.py, tests/test_token_usage.py, docs/invariants/init-devices.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/init-devices.md, src/mind_meld/safety.py_
_produces: no string returned by `strip_terminal_escapes`, `safe_str`, `safe_text` or `safe_terminal_str` contains ESC or a C1 control; newlines and tabs in diff bodies survive_
_session: fresh · effort: medium · verify: ./bin/check tests/test_safe_str.py tests/test_conflictdiff.py tests/test_retention.py tests/test_token_usage.py tests/test_docs_routing.py_

_Source: [plan-eng-review:severity=major] filed by Track 50A /autoplan, 2026-09-06, priority P1. Verified at 38222ac: `strip_terminal_escapes` is a single `_ANSI_ESCAPE_RE.sub` pass, and its own docstring plus the v0.14.4 CHANGELOG record nested-escape hardening as the follow-up. A nested ST-terminated payload survives `safe_str` and `safe_text` as a complete OSC 52 sequence (review evidence `~/.gstack/projects/kbitz-mind-meld/50a-reproductions.json`, key `corrected_shared_rich_sink_probe`). Sink census at HEAD (`grep -c "safe_str(\|safe_text(" src/mind_meld/*.py`): cli.py 127, resolveflow.py 30, retention.py 26, skill_link.py 9, events_tail.py 8, conflictdiff.py 5. **Correction, 2026-09-06 /ship adversarial review (Claude + Codex passes, independently confirmed):** the prior text claimed "only three" of those are plain `sys.stderr.write` calls; direct read of every site puts the true count at 12 — 1 in retention.py, 2 in token_usage.py, 8 in events_tail.py (`:277`, `:382`, `:540`, `:986`, `:1045`, `:1082`, `:1106`, `:1187`), 1 in skill_link.py (`:1079`) — none behind Rich markup. Bullet 2 below intentionally narrows to the 3 originally named sites rather than silently growing to all 12: `events_tail.py` already collides with Track 55A's `_touches:`, and folding it in here would force a repack. The other 9 are deferred — see roadmap-future.md. Fix the helper, not the two hundred callers._

- **Define the postcondition once in safety.py** -- after the grammar strip, delete every remaining ESC (`\x1b`) and C1 control (`\x80`-`\x9f`) code point so no deletion can assemble a fresh introducer; a fixed number of regex passes is not the proof. Keep `\n` and `\t` for `safe_text` diff bodies; keep `safe_terminal_str`'s printable-only rule. Pin the review's nested probe (`"\x1b\x1b[31m]52;c;VEVTVA==\x1b\x1b[31m\\"`), a BEL-terminated nesting, and a bare 8-bit `\x9d` OSC against all four helpers through a captured `Console(file=StringIO(), force_terminal=True)`; assert on `repr` only and never replay a capture in a live terminal. Rewrite the three "recorded follow-up" docstrings and the invariant's sanitizer paragraph to state the postcondition. _safety.py + tests + init-devices.md, ~50 lines._ (M)
- **Route 3 of the 12 plain-stderr `safe_str` sites** -- `retention.py`'s `token cache gc failed` notice and `token_usage.py`'s oversize-line and unknown-model notices write to `sys.stderr` with no Rich markup, so markup escaping there only adds backslashes; switch them to `safe_terminal_str` and leave every `console.print` sink on `safe_str`. `conflictdiff` rendering is unchanged. The other 9 (`events_tail.py` x8, `skill_link.py` x1) are out of scope for this bullet — deferred to roadmap-future.md to avoid a `_touches:` collision with Track 55A. _retention.py + token_usage.py + tests, ~30 lines._ (S)

#### Group 53: Pull isolation

_Depends on: Group 52_

##### Track 53A: Contain apply exceptions without losing completed-file bookkeeping
_2 tasks . ~150 LOC . high risk . 4 files_
_touches: src/mind_meld/cli.py, tests/test_pull_helpers.py, tests/test_integration.py, tests/test_silent_failure_contract.py, docs/invariants/sync.md, docs/invariants/conflicts.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 52A_
_read-first: docs/invariants/sync.md, docs/invariants/conflicts.md_
_produces: one file's apply failure is a per-file `failed` outcome with sanitized context and the rest of the batch continues; files already applied keep their history, sync-log and directory fsync even when the pull still aborts; user abort still aborts_
_session: fresh · effort: high · verify: ./bin/check tests/test_pull_helpers.py tests/test_integration.py tests/test_silent_failure_contract.py tests/test_docs_routing.py_

_Source: [plan-eng-review:severity=major] filed by Track 51A /autoplan, 2026-09-06. Verified at 38222ac: `_download_and_apply` calls `_apply_incoming_file` with no exception boundary (the only `finally` stops the progress bar), so a raise discards the `outcomes` map and propagates out of `_pull_one_source`, which owns no pull-history, sync-log or fsync step itself — it only ever returns outcomes and touched parents (`_PerSourceResult`). Those three steps live in the caller, `_pull_core` (**correction, 2026-09-06 /ship adversarial review** — the prior text attributed them to `_pull_one_source`); when the raise prevents that return, `_pull_core` never receives this source's result and so never runs its own pull-history, sync-log or `fsutil.fsync_dir`-per-touched-parent step for it, and the remaining files, sources and peers stop. Reproduced at cc22b6c: batch `earlier.txt`, `blocked/inside.txt`, `later.txt` with a regular local file at `blocked`; the parent `mkdir` raises `FileExistsError` and `later.txt` never arrives (evidence `~/.gstack/projects/kbitz-mind-meld/51a-deferred-isolation-reproduction.json`). Autopull already prints an unexpected-error line and a failed breadcrumb: this is not a silent failure, it is a lost batch. v0.14.5 fixed one named exception (`TypeError` from mixed timestamps); this is the general boundary. Dependency is release serialization._

- **Classify apply exceptions at the boundary** -- inventory what `_apply_incoming_file` can raise before and after the canonical write is published (the `OSError` family from mkdir, write, rename and mtime restore; `MindMeldError`; decoder errors; `typer.Abort` / `KeyboardInterrupt`). Wrap the call so OS and mm errors become a per-file `failed` outcome with `safe_str` file context and the batch continues; user abort and programming errors still propagate. A failure raised after a successful write must not be reported as a failed write. Preserve the `_CANONICAL_WRITE_OUTCOMES` inline-bump invalidation on the success path. _cli.py + tests, ~80 lines._ (M)
- **Keep completed bookkeeping when the pull still aborts** -- when an exception does propagate out of `_download_and_apply`, `_pull_one_source` and `_pull_core` must still record the outcomes accumulated so far: pull history, sync-log, and the deferred `fsync_dir` of every touched parent, without draining abandoned keep-local decisions (the `_drain_inline_bumps` abort contract in conflicts.md). Pin three isolated multi-file cases: a normal write followed by the parent-file collision; an injected post-publication exception; an explicit user abort. Autopull's `mm: warning:` line and failed breadcrumb stay. _cli.py + integration tests, ~70 lines._ (M)

#### Group 54: Honest Codex diagnostics

_Depends on: Group 53_

##### Track 54A: Report failed Codex capture and remove obsolete reader helpers
_2 tasks . ~120 LOC + ~60 lines (del) . medium risk . 5 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/cli.py, tests/test_host_usage.py, tests/test_diag.py, tests/test_silent_failure_contract.py, README.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 53A_
_read-first: docs/invariants/events-retro.md_
_produces: status and diag distinguish a complete Codex capture from a cached inventory whose latest read failed_
_session: fresh · effort: medium · verify: ./bin/check tests/test_host_usage.py tests/test_diag.py tests/test_silent_failure_contract.py tests/test_docs_routing.py_

_Source: approved full-review C3/H3, 2026-09-05 UTC, plus the unused `_Terminal` branch from the former walker card; carded as Track 52A in the 2026-09-05 plan. A warm rollout followed by unsupported counters reproduces reader failure alongside `state=ready, pending=0`. Grok's v0.14.1 `last_reason` contract supplies the existing pattern. Re-verified at 38222ac: `codex_usage_diag` returns `state` / `pending` / `files_*` and no failure field; `_model_id` in host_usage.py has zero callers; `_terminal_from_record` is called only from `tests/test_host_usage.py`, so the `_Terminal` branch in `_aggregate` is unreachable in production. This fixes diagnosis without committing to the deferred per-record quarantine policy. Dependency is release serialization._

- **Carry failure state through diagnosis** -- apply the persistent outcome contract to the Codex reader and its status/diag consumers in one change. Preserve a permanent failure across transient failures; successful recovery clears it. Keep diagnostic reads cache-only and passphrase-free, and retain independent Grok success. _host_usage.py + cli.py + tests and user-facing reference, ~120 lines._ (M)
- **Delete obsolete reader paths** -- reconfirm and remove `_model_id`, the test-only `_terminal_from_record`/`_Terminal` path, and tests that exercise only that obsolete representation. Keep the production `_TurnState` cumulative union and inclusive-counter normalization unchanged. Replace live comments that still promise a universal adapter with the actual deferred-work reference. _host_usage.py + test_host_usage.py, ~60 lines (del)._ (S)

#### Group 55: Git capture integrity and cleanup

_Depends on: Group 54_

##### Track 55A: Scrub the git environment for mm's git subprocesses
_2 tasks . ~40 LOC + ~50 lines (del) . low risk . 7 files_
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/token_usage.py, tests/test_events.py, tests/test_init_events_backfill.py, tests/test_module_boundaries.py, tests/test_track_30a.py, AGENTS.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 54A_
_read-first: docs/invariants/events-retro.md_
_produces: each git candidate reports its own history and event-capture documentation points at the code that runs_
_session: fresh · effort: medium · verify: ./bin/check tests/test_events.py tests/test_init_events_backfill.py tests/test_module_boundaries.py tests/test_track_30a.py tests/test_docs_routing.py_

_Source: former Track 48A (2026-09-03 numbering), carded as Track 53A in the 2026-09-05 plan, plus approved full-review H1/H2. Re-verified at 38222ac: both `subprocess.run` calls in `events.py` (`_walk_one_repo` and `_origin_remote_url`) pass no `env=`, so they inherit git repository-redirection variables while filesystem discovery ignores them. `_last_mm_push_ts` and `_run_events_recapture` have no runtime callers: the active paths are `resolve_push_cursor` and the CLI's `_prepare_recapture` orchestration. Dependency is release serialization._

- **Scrub repository-redirection variables** -- pass an environment that cannot redirect either git subprocess to another repository. Use an isolated decoy-repo test with `GIT_DIR` and the related repository-selection variables to verify both history and remote attribution. _events.py + tests, ~40 lines._ (S)
- **Remove the unused event helpers** -- delete `_last_mm_push_ts`, `_run_events_recapture` and its export; **re-point** (do NOT delete) the references in invariants, AGENTS.md, token_usage's reader documentation and adjacent tests. Both `_last_mm_push_ts` facts in `docs/invariants/events-retro.md` survive the deletion and move to successors: the bounded-read table row at `:86` belongs to `_iter_mm_push_objs`, and the "returning `None` is NOT a benign fallback" cursor-rewind hazard at `:93` belongs to `resolve_push_cursor` / `CursorResolution.used_floor`. Drop the now-vacuous `assert "_run_events_recapture" not in src` at `tests/test_track_30a.py:678` (it sat at `:636` before v0.14.3 grew that file); keep its live `_prepare_recapture` and `recapture(` siblings. Preserve the exercised cursor and recapture behavior; no new generic walker. _events.py + events_tail.py and documentation, ~50 lines (del)._ (S)

#### Group 56: Dry-run honesty

_Depends on: Group 55_

##### Track 56A: Make push dry-run setup honor the no-mutation contract
_1 task . ~110 LOC . medium risk . 6 files_
_touches: src/mind_meld/cli.py, src/mind_meld/config.py, src/mind_meld/upgrade.py, tests/test_config.py, tests/test_integration.py, tests/test_silent_failure_contract.py, tests/test_upgrade.py, README.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 55A_
_read-first: docs/invariants/events-retro.md, docs/invariants/init-devices.md_
_produces: `mm push --dry-run` writes nothing: no config patch, no fingerprint persist, no mm-events directory, no upgrade-cache write-through, no conditional self-upgrade history entry; where a truthful preview needs setup it refuses and names the command that performs it_
_session: fresh · effort: medium · verify: ./bin/check tests/test_config.py tests/test_integration.py tests/test_silent_failure_contract.py tests/test_upgrade.py tests/test_docs_routing.py_

_Source: [plan-eng-review:severity=moderate] filed by Track 49A /autoplan DX and Eng review, 2026-09-06. Verified at 38222ac: `push` calls `_maybe_prompt_migration` and then `_init_crypto_session` (which persists a missing `root_salt_fp` through `patch_config_on_disk`) before `_push_core(..., dry_run=True)`; `config.resolve_sources` runs `_bootstrap_mm_events_path` (a `mkdir`) unconditionally; the `--dry-run` help text admits all three. The `dry_run` no-op invariant in events-retro.md names this setup repair as the separate follow-up and forbids weakening the publication half to match it. **Widened, 2026-09-06 /ship adversarial review (Codex pass, independently verified by direct read):** two more unconditional-or-near-unconditional writes bracket `_push_core` outside those three. `_get_config()` (`cli.py:383`) calls `upgrade.run_transition_hook`, which appends a `pullhistory` self-upgrade record whenever a version transition is detected (`upgrade.py:463`) — not gated on `dry_run` at all. `push()`'s tail (`cli.py:3284`) calls `upgrade.emit_nudge_if_due`, whose `check_for_upgrade` does an unconditional write-through persist of the upgrade-cache JSON on every call via `locked_json_rmw` (`upgrade.py:267`, comment: "write-through: helper persists ljson.data") — this fires on every `mm push --dry-run`, not just the 1x/24h HTTP-fetch path the docstring's latency note might suggest. `acquire_lock()`'s lockfile PID write (`lockfile.py`) is a third pre-`_push_core` write but is deliberately left out of this card's scope: it is ephemeral process-coordination state, not persisted config/history data, and every command (including read-only ones like `mm status`) already takes the same lock. This is a source audit, not a claimed live mutation. Dependency is release serialization._

- **Make setup read-only under --dry-run** -- thread a read-only flag through the five setup/tail sites: report a pending config migration and name `mm migrate-config` instead of prompting; keep the fingerprint backfill in memory only; report "would create" for a missing mm-events directory instead of creating it; skip the `run_transition_hook` pullhistory append (the transition will still be recorded on the next non-dry-run push); skip `emit_nudge_if_due`'s cache write-through (an in-memory-only check may still print the nudge text). Where the preview cannot be truthful without setup, refuse with the exact command. Snapshot config, source directories, the upgrade cache and storage before and after in isolated CLI tests for a missing fingerprint, a missing mm-events root, a pending migration, a pending version transition, and a due upgrade nudge. Update the help text and the invariant's setup caveat. Do not route a dry-run through recover or reset. Leave `acquire_lock`'s PID write untouched (see Source). _cli.py + config.py + upgrade.py + tests, ~110 lines._ (M)

### Phase 3: Retro fidelity

**End-state:** The retro presents model usage consistently, states each value's collection scope and coverage, and adds only verified Grok estimates without changing accounting semantics.
**Groups:** 57, 58

#### Group 57: Grok pricing

_Depends on: Group 56_

##### Track 57A: Price verified Grok usage
_2 tasks . ~100 LOC . medium risk . 4 files_
_touches: src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_token_usage.py, tests/test_retro_fleet_aggregator.py, README.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 56A_
_read-first: docs/invariants/events-retro.md, tests/fixtures/host_sessions/grok/CONTRACT.md_
_produces: verified Grok model ids have sourced, bounded API-equivalent estimates through resolve_prices; unverifiable aliases remain unpriced_
_session: fresh · effort: high · verify: ./bin/check tests/test_token_usage.py tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py_

_Source: Track 46A pricing deferral (2026-09-04) and the matching Future item; carded as Track 54A in the 2026-09-05 plan. v0.14.1 restored ingestion without the proposed cache rewrite; neither the retired OpenCode bug nor `offset == size` is an active pricing prerequisite. Re-verified at 38222ac: token_usage.py still records xAI as HELD with no `grok-4.6-build` alias and no xAI tier. Pricing is still absent by decision. Re-census actual model ids and counter semantics when implementing, and verify current xAI primary-source rates and context thresholds; do not copy stale numbers into the table. Dependency is release serialization._

- **Verify the rate contract and add exact model mappings** -- establish the current observed Grok model ids and disjoint counters, then add only supported aliases/rates through `resolve_prices`, with a vendor-specific verification date. An unverifiable model alias stays unpriced. Do not infer rates from arbitrary peer model prefixes or decode the unverified `costUsdTicks` unit. _token_usage.py + tests, ~60 lines._ (M)
- **Keep estimate limits visible** -- test the existing per-machine consumer with Grok data, including unknown models and any context-length surcharge that aggregate counters cannot reconstruct. Such uncertainty must remain a floor or unavailable estimate, never an exact bill or a fleet sum. Carry the disclosure into the reference and invariant docs. _aggregator.py + tests, ~40 lines._ (S)

#### Group 58: Usage presentation

_Depends on: Group 57_

##### Track 58A: Make model usage easier to read without changing what totals mean
_1 task . ~220 LOC . medium risk . 3 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 57A_
_read-first: docs/invariants/events-retro.md_
_produces: consistent usage presentation with explicit source logs, machine scope, observation time, window and coverage_
_session: fresh · effort: high · verify: ./bin/check tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py_

_Source: former Track 50A (2026-09-03 numbering), carded as Track 55A in the 2026-09-05 plan, approved full-review H4, and the user-approved pre-existing-roadmap assessment on 2026-09-05. Re-verified at 38222ac: `_render_agent_inventory` and `AGENT_FAMILY_ROWS` remain; `aggregate` materializes event rows directly and `_read_events` has zero callers. The cache-encoding and shared-walker prerequisites stay removed. Track 57A supplies verified rates or an explicit unpriced result, not permission to change aggregation._

_Boundary verified at f10bf34 and unchanged at 38222ac: `SessionsAggregate` holds Claude fleet-window totals; `HostUsageInventory` retains accepted snapshots per machine. Day-bucket slicing is NOT one function: `_windowed_host_by_model` (one caller, `_device_economics_cell`) slices `tokens_by_day` only, while `lifetime_by_family` is sliced inline in `_render_agent_inventory` and in the rhythm view, clamped by `_snapshot_day_ceiling` alone. A renderer rewrite must account for all three sites. Contributing readers are recorded per machine, but the wire has no reader-to-model-family attribution. Presentation must preserve those distinctions. New accounting schemas, cross-machine deduplication or reader-to-model wire attribution need a separately justified proposal._

- **Clarify usage without changing accounting** -- first show a compact before/after rendered example with Claude session data, two machines of host inventory, an unpriced Grok model and a degraded reader. Use it to settle consistent naming and layout while showing each value's collection scope, observation time, window and coverage. Reuse existing aggregations: host counters never enter the Claude fleet sum; retained inventory stays distinguishable from in-window activity; per-machine host estimates retain the do-not-sum rule. Do not imply per-host model attribution the wire lacks. Preserve the existing global git metrics, unavailable/partial/degraded disclosures, retired-reader tolerance, two-pass skill decoder and no-new-summary-row constraint. Delete the unused `_read_events` iterator and correct its documentation. Pin populated, absent and degraded views plus the one-materialization prior-period path. _aggregator.py + SKILL.md + tests, ~220 lines._ (L)

### Execution Map

**This adjacency is RELEASE order, not launch order.** Every edge below is release serialization on `pyproject.toml`: seven cards claim seven consecutive versions, and only one tag can exist per version. None of the edges is a data dependency except 57A → 58A, which records which Grok models have verified prices and which remain unpriced. Tracks may be worked in parallel Conductor workspaces; only their version slots serialize, and document order is priority. The 2026-09-05 map's open question (two small pull fixes queued behind ~340 LOC of integrity work) resolved itself: all four shipped within one day as v0.14.2–v0.14.5.

Adjacency from gstack's `roadmap-pack` tool on the drafted Tracks (identical to the audit's GROUP_DEPS after apply; this is the `/roadmap` skill's own packer, not a script in this repo's `bin/`):

```
- Group 52 ← {}
- Group 53 ← {52}
- Group 54 ← {53}
- Group 55 ← {54}
- Group 56 ← {55}
- Group 57 ← {56}
- Group 58 ← {57}
```

Track detail per group:

```
Group 52: Terminal-control postcondition
  +-- Track 52A ........... ~M . 2 tasks
Group 53: Pull isolation
  +-- Track 53A ........... ~L . 2 tasks
Group 54: Honest Codex diagnostics
  +-- Track 54A ........... ~M . 2 tasks
Group 55: Git capture integrity and cleanup
  +-- Track 55A ........... ~S . 2 tasks
Group 56: Dry-run honesty
  +-- Track 56A ........... ~M . 1 task
Group 57: Grok pricing                        (Retro fidelity)
  +-- Track 57A ........... ~M . 2 tasks
Group 58: Usage presentation                  (Retro fidelity)
  +-- Track 58A ........... ~L . 1 task
```

**Total: 7 groups . 7 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (79 items)

## Shipped

History: docs/roadmap-shipped.md
