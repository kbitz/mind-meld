# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm supports three agents: Claude Code, Codex, and Grok Build.** OpenCode was dropped on 2026-09-01 by user decision; Groups 36 and 44 removed it (shipped v0.12.53 → v0.13.0). Do not re-add a fourth agent without a measured need — see the skill-link constraint below, which already refuses one on discovery grounds.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A, and it is why **Grok Build needs no skill link and no sync source** — probed 2026-09-01, `~/.grok/` has no `skills/`, `commands/` or `rules/` directory at all. Grok Build's entire mm surface is the usage reader.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Seven Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **A command that only exists to undo an automatic action is refused until the automatic action is shown to be correct.** v0.12.44 killed `mm uninstall-skills` this way: a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect — the installer recreated a link the user deleted. Fixing the installer made all three unnecessary. Before filing an inverse, check whether the forward action should have happened at all.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`. Two Tracks claiming one version merge cleanly in git, but only one tag can exist for that version — the second's code never gets tagged at all. A dev-dep-only `pyproject.toml` edit no longer force-pushes `latest`: `release.yml` compares `git rev-parse "$tag^{commit}"` to HEAD and skips with a warning. Serialization is still the cheap guard against the silent-lost-code case. See `docs/shared-infra.txt`.
- **The roadmap-staleness gate stays dead.** Track 28B was killed 2026-08-25 on the grounds that an empty Current Plan leaves nothing to drift. Groups 29–35 removed that ground, the question was re-put on 2026-08-25 with seven Groups in flight, and the answer was the same. Do not re-propose it; the design remains recorded in the Group 28 entry of `docs/roadmap-shipped.md` for whoever overrides this.
- **Discovery may read host logs locally, but an encoded cwd never goes on the wire.** Track 29A's prober is a two-level scan of `~/conductor/workspaces/*/*` whose only wire output is a canonical remote URL. Codex `turn_context.cwd` and Grok's URL-encoded session dir names would both yield more roots and are refused — see the 2026-09-05 refusal record in `docs/TODOS.md`. Confirmed 2026-08-25.
- **A Track that puts a field on a wire, in a cache, or in a log must name its reader in the same card, or declare the reader's Track by number.** Track 34A's review found FOUR producer-without-consumer instances in one pass: `degraded_sources` (shipped v0.12.47, zero readers), `git_capture` (shipped Track 30A, unread by the aggregator), `usageIsIncomplete` (discarded at cache normalization), and the SKILL.md decoder's missing fallback. Reinforced 2026-09-01 by the v0.12.51 conflict-log analysis, which found the conflict-decision collector had been deleted on a premise nobody read, and `synclog.py` still describing the pre-inversion direction four months after the inversion. A write with no reader is not half a feature, it is a liability that reads as one.
- **When a Track touches a reader, check the cache shape, not just the behaviour.** Track 34A verified all six of its card premises as TRUE-or-known-false and was still under-priced 2.5x, because premises describe behaviour while the cost sat in `host_usage._validated_grok_entry`, which normalizes every cached turn to `{key, day, model, usage}` and drops the rest. The existing "check the premise at drain time" constraint worked exactly as written and was insufficient.
- **A Track that prices, sums, or trends a counter must first prove the counter schema of every reader it consumes.** Added 2026-09-01. Track 35A's card was measured against HEAD, its premises were re-verified at drain time, and it was still going to ship a 7.40x error, because every existing constraint checks *behaviour* and *premises* while the defect sat in an undocumented property of the source formats. Codex CLI and Grok CLI report **inclusive** `input` (cache-read already inside it); Claude is **disjoint**. `grok-4.6` appeared under both schemas, so the semantics belong to the READER, not the model id. This is the "check the cache shape" constraint one layer further out: check the SOURCE shape.
- **A feature nobody uses is deleted, not repaired.** Added 2026-09-01. The OpenCode reader had been discarding its entire store since v0.12.30 and a Track was drafted to fix it; probing first showed `opencode.db` 19 days cold, `~/.config/opencode/` three weeks stale and composed entirely of symlinks into a git repo already under version control. The fix was real and the feature was not. Probe the artifact's liveness before pricing its repair.
- **A host usage reader may not ship against a synthetic fixture.** Added 2026-09-01. Its `CONTRACT.md` must record a live census against a real corpus and the host version it was taken from. `tests/fixtures/host_sessions/opencode/CONTRACT.md` said outright "the local machine did not have an OpenCode data directory" and "the SQLite schema is a minimal synthetic contract table." It was the only reader built that way and the only one that returned zero — the root cause Track 36A existed to delete (shipped v0.12.53).

---

## In Progress

_No partially shipped Groups remain after reconciling v0.14.0 and v0.14.1._

## Current Plan

_tombstone: 27_

#### Group 48: Hotfix: Preserve conflict copies

_Depends on: none_

##### Track 48A: Preserve conflict ownership and failed replacements
_2 tasks . ~140 LOC . high risk . 4 files_
_touches: src/mind_meld/cli.py, src/mind_meld/manifest.py, tests/test_conflict_copy.py, tests/test_manifest.py, docs/invariants/conflicts.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/conflicts.md, docs/invariants/sync.md_
_produces: deduplication touches only the matching canonical file and a failed replacement preserves the prior conflict copy_
_session: fresh · effort: high · verify: ./bin/check tests/test_conflict_copy.py tests/test_manifest.py tests/test_docs_routing.py_

_Source: approved full-review findings R2/R3, 2026-09-05 UTC. The deletion behavior was introduced by `f4bf6dd` (v0.11.5), so this is a regression on shipped preservation behavior. Reproduced at `8be81ce`: a conflict for `notes.md` deletes the sidecar of `notes.sync-conflict-log.md`; separately, an injected ENOSPC during replacement leaves no sidecars. These mechanisms remain after v0.14.0. The former 45A claim that the prefix glob cannot match another canonical is false; this does not establish that either mechanism caused every historical disappearance._

- **Match exact canonical ownership** -- replace the stem-prefix inference with ownership parsed from the final conflict suffix; retain peer and era checks. Cover double-infix canonical names, literal glob characters, extensions, and a same-content sidecar belonging to a different file so false dedup is caught as well as false deletion. _cli.py + manifest.py + regression tests, ~80 lines._ (M)
- **Preserve the old copy until replacement succeeds** -- remove stale copies only after a replacement has been written successfully; failure must preserve the previous peer bytes and surface the existing failure outcome. Pin injected write failure, successful replacement, unchanged dedup, and unlink failure. Preserve the peer-mtime and v1 filename contracts. _cli.py + regression tests, ~60 lines._ (M)

#### Group 49: Snapshot integrity

_Depends on: Group 48_

##### Track 49A: Publish complete, content-consistent snapshots
_2 tasks . ~200 LOC . high risk . 5 files_
_touches: src/mind_meld/cli.py, src/mind_meld/manifest.py, tests/test_integration.py, tests/test_manifest.py, tests/test_pull_helpers.py, docs/invariants/sync.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 48A_
_read-first: docs/invariants/sync.md_
_produces: every advertised hash identifies the uploaded bytes and a failed read never becomes evidence of deletion_
_session: fresh · effort: high · verify: ./bin/check tests/test_integration.py tests/test_manifest.py tests/test_pull_helpers.py tests/test_docs_routing.py_

_Source: approved full-review R1/C2, 2026-09-05 UTC. Reproduced at `8be81ce`: two files scanned with identical content share a blob key; editing one before `_upload_changed_blobs` makes a peer receive its new bytes for the untouched file too. A separate hash/read error omits an existing path and `_push_core` publishes a tombstone that suppresses restoration. Existing exclude and symlink filters do not cover failed reads. The dependency puts the preservation regression first and serializes the shared release claim; it is not a new data dependency._

- **Make content addressing describe one snapshot** -- investigate refusing a changed input or using one consistent snapshot for bytes, digest, size and mtime. Do not discard the freshly computed digest or advertise a skipped/missing upload. Pin the two-path shared-hash reproduction and failure before manifest publication; retain the encrypted-storage invariant. _cli.py + integration tests, ~100 lines._ (M)
- **Separate incomplete scans from deletions** -- choose the smallest safe failure policy before adding recovery machinery: a read error must not enter deletion comparison as a missing file. Pin unreadable-existing versus genuinely deleted files, source scoping, existing exclusion behavior, recovery manifests, and a visible autopush failure/degradation. Any failure signal produced by the walker must be consumed by `_push_core` in this change. _manifest.py + cli.py + tests, ~100 lines._ (M)

#### Group 50: Terminal-safe recovery warnings

_Depends on: Group 49_

##### Track 50A: Sanitize rejected storage filenames
_1 task . ~40 LOC . low risk . 3 files_
_touches: src/mind_meld/storage/local.py, tests/test_storage_local.py, tests/test_recovery.py, docs/invariants/init-devices.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 49A_
_read-first: docs/invariants/init-devices.md_
_produces: a rejected manifest filename or validator exception cannot inject terminal controls through a warning_
_session: fresh · effort: medium · verify: ./bin/check tests/test_storage_local.py tests/test_recovery.py tests/test_safe_str.py tests/test_docs_routing.py_

_Source: approved full-review C1, 2026-09-05 UTC. Captured stderr at `8be81ce` contains an OSC 52 sequence from a Dropbox-shaped conflict filename even when validation returns false. The existing sanitizer removes it. The dependency serializes the release after snapshot integrity; the sanitizer itself has no dependency on that implementation._

- **Reuse the terminal sanitizer at both warning sites** -- sanitize candidate paths and exception text in `LocalBackend.find_conflict_copies`, retaining the useful warning. Exercise validator-false and validator-raised branches and the manifest-fetch caller using captured output; never execute the control sequence in a terminal. _storage/local.py + tests, ~40 lines._ (S)

#### Group 51: Deterministic JSONL merge

_Depends on: Group 50_

##### Track 51A: Keep mixed timestamp types from aborting pull
_1 task . ~60 LOC . medium risk . 3 files_
_touches: src/mind_meld/merge.py, tests/test_merge.py, tests/test_integration.py, docs/invariants/conflicts.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 50A_
_read-first: docs/invariants/conflicts.md_
_produces: valid JSONL with heterogeneous timestamp values merges deterministically and does not abort later downloads_
_session: fresh · effort: medium · verify: ./bin/check tests/test_merge.py tests/test_integration.py tests/test_docs_routing.py_

_Source: approved full-review R4, 2026-09-05 UTC. `_extract_ts` returns arbitrary JSON values into `merge_jsonl`'s sort. Numeric and string timestamps reproduce a TypeError out of `_download_and_apply`, preventing the next ordinary file from downloading. The existing UTF-8 replacement behavior is explicitly accepted by `test_non_utf8_graceful` and is outside this fix. Dependency is release serialization._

- **Use supported comparable sort keys** -- investigate retaining timestamp ordering for the documented string form and using the existing deterministic lexical fallback for other values. Preserve every input line, full-line tie breaking, idempotence and ordering across hash seeds. Pin heterogeneous values through the real download/apply path and verify the following file is downloaded. _merge.py + tests, ~60 lines._ (M)

#### Group 52: Honest Codex diagnostics

_Depends on: Group 51_

##### Track 52A: Report failed Codex capture and remove obsolete reader helpers
_2 tasks . ~120 LOC + ~60 lines (del) . medium risk . 5 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/cli.py, tests/test_host_usage.py, tests/test_diag.py, tests/test_silent_failure_contract.py, README.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 51A_
_read-first: docs/invariants/events-retro.md_
_produces: status and diag distinguish a complete Codex capture from a cached inventory whose latest read failed_
_session: fresh · effort: medium · verify: ./bin/check tests/test_host_usage.py tests/test_diag.py tests/test_silent_failure_contract.py tests/test_docs_routing.py_

_Source: approved full-review C3/H3, 2026-09-05 UTC, plus the unused `_Terminal` branch from the former walker card. A warm rollout followed by unsupported counters reproduces reader failure alongside `state=ready, pending=0`. Grok's v0.14.1 `last_reason` contract supplies the existing pattern. `_model_id` has no callers; `_terminal_from_record` and the `_Terminal` aggregation branch are test-only. This fixes diagnosis without committing to the deferred per-record quarantine policy. Dependency is release serialization._

- **Carry failure state through diagnosis** -- apply the persistent outcome contract to the Codex reader and its status/diag consumers in one change. Preserve a permanent failure across transient failures; successful recovery clears it. Keep diagnostic reads cache-only and passphrase-free, and retain independent Grok success. _host_usage.py + cli.py + tests and user-facing reference, ~120 lines._ (M)
- **Delete obsolete reader paths** -- reconfirm and remove `_model_id`, the test-only `_terminal_from_record`/`_Terminal` path, and tests that exercise only that obsolete representation. Keep the production `_TurnState` cumulative union and inclusive-counter normalization unchanged. Replace live comments that still promise a universal adapter with the actual deferred-work reference. _host_usage.py + test_host_usage.py, ~60 lines (del)._ (S)

#### Group 53: Git capture integrity and cleanup

_Depends on: Group 52_

##### Track 53A: Scrub the git environment for mm's git subprocesses
_2 tasks . ~40 LOC + ~50 lines (del) . low risk . 6 files_
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/token_usage.py, tests/test_events.py, tests/test_init_events_backfill.py, tests/test_module_boundaries.py, AGENTS.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 52A_
_read-first: docs/invariants/events-retro.md_
_produces: each git candidate reports its own history and event-capture documentation points at the code that runs_
_session: fresh · effort: medium · verify: ./bin/check tests/test_events.py tests/test_init_events_backfill.py tests/test_module_boundaries.py tests/test_docs_routing.py_

_Source: former Track 48A (2026-09-03 numbering), plus approved full-review H1/H2. `_walk_one_repo` and `_origin_remote_url` still inherit git repository-redirection variables; filesystem discovery ignores them. `_last_mm_push_ts` and `_run_events_recapture` have no runtime callers: the active paths are `resolve_push_cursor` and the CLI's `_prepare_recapture` orchestration. Dependency is release serialization._

- **Scrub repository-redirection variables** -- pass an environment that cannot redirect either git subprocess to another repository. Use an isolated decoy-repo test with `GIT_DIR` and the related repository-selection variables to verify both history and remote attribution. _events.py + tests, ~40 lines._ (S)
- **Remove the unused event helpers** -- delete `_last_mm_push_ts`, `_run_events_recapture` and its export; correct references in invariants, AGENTS.md, token_usage's reader documentation and adjacent tests. Preserve the exercised cursor and recapture behavior; no new generic walker. _events.py + events_tail.py and documentation, ~50 lines (del)._ (S)

### Phase 3: Retro fidelity

**End-state:** The retro presents model usage consistently, states each value's collection scope and coverage, and adds only verified Grok estimates without changing accounting semantics.
**Groups:** 54, 55

#### Group 54: Grok pricing

_Depends on: Group 53_

##### Track 54A: Price verified Grok usage
_2 tasks . ~100 LOC . medium risk . 4 files_
_touches: src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_token_usage.py, tests/test_retro_fleet_aggregator.py, README.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 53A_
_read-first: docs/invariants/events-retro.md, tests/fixtures/host_sessions/grok/CONTRACT.md_
_produces: verified Grok model ids have sourced, bounded API-equivalent estimates through resolve_prices; unverifiable aliases remain unpriced_
_session: fresh · effort: high · verify: ./bin/check tests/test_token_usage.py tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py_

_Source: Track 46A pricing deferral (2026-09-04) and the matching Future item. v0.14.1 restored ingestion without the proposed cache rewrite; neither the retired OpenCode bug nor `offset == size` is an active pricing prerequisite. Pricing is still absent by decision. Re-census actual model ids and counter semantics when implementing, and verify current xAI primary-source rates and context thresholds; do not copy stale numbers into the table. Dependency is release serialization._

- **Verify the rate contract and add exact model mappings** -- establish the current observed Grok model ids and disjoint counters, then add only supported aliases/rates through `resolve_prices`, with a vendor-specific verification date. An unverifiable model alias stays unpriced. Do not infer rates from arbitrary peer model prefixes or decode the unverified `costUsdTicks` unit. _token_usage.py + tests, ~60 lines._ (M)
- **Keep estimate limits visible** -- test the existing per-machine consumer with Grok data, including unknown models and any context-length surcharge that aggregate counters cannot reconstruct. Such uncertainty must remain a floor or unavailable estimate, never an exact bill or a fleet sum. Carry the disclosure into the reference and invariant docs. _aggregator.py + tests, ~40 lines._ (S)

#### Group 55: Usage presentation

_Depends on: Group 54_

##### Track 55A: Make model usage easier to read without changing what totals mean
_1 task . ~220 LOC . medium risk . 3 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 54A_
_read-first: docs/invariants/events-retro.md_
_produces: consistent usage presentation with explicit source logs, machine scope, observation time, window and coverage_
_session: fresh · effort: high · verify: ./bin/check tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py_

_Source: former Track 50A (2026-09-03 numbering), approved full-review H4, and the user-approved pre-existing-roadmap assessment on 2026-09-05. `_render_agent_inventory` and `AGENT_FAMILY_ROWS` remain; `aggregate` materializes event rows directly and `_read_events` is unused. The cache-encoding and shared-walker prerequisites stay removed. Track 54A supplies verified rates or an explicit unpriced result, not permission to change aggregation._

_Boundary verified at f10bf34: `SessionsAggregate` holds Claude fleet-window totals; `HostUsageInventory` retains accepted snapshots per machine, whose day buckets are sliced by `_windowed_host_by_model` and clamped by `_snapshot_day_ceiling`. Contributing readers are recorded per machine, but the wire has no reader-to-model-family attribution. Presentation must preserve those distinctions. New accounting schemas, cross-machine deduplication or reader-to-model wire attribution need a separately justified proposal._

- **Clarify usage without changing accounting** -- first show a compact before/after rendered example with Claude session data, two machines of host inventory, an unpriced Grok model and a degraded reader. Use it to settle consistent naming and layout while showing each value's collection scope, observation time, window and coverage. Reuse existing aggregations: host counters never enter the Claude fleet sum; retained inventory stays distinguishable from in-window activity; per-machine host estimates retain the do-not-sum rule. Do not imply per-host model attribution the wire lacks. Preserve the existing global git metrics, unavailable/partial/degraded disclosures, retired-reader tolerance, two-pass skill decoder and no-new-summary-row constraint. Delete the unused `_read_events` iterator and correct its documentation. Pin populated, absent and degraded views plus the one-materialization prior-period path. _aggregator.py + SKILL.md + tests, ~220 lines._ (L)

### Execution Map

A Group may launch when every Group in its dependency set has landed. The open hotfix blocks other work; release-bearing Tracks then serialize on `pyproject.toml`. The 54A → 55A edge also records which Grok models have verified prices and which remain unpriced.

Adjacency from the roadmap audit's GROUP_DEPS output:

```
- Group 48 ← {}
- Group 49 ← {48}
- Group 50 ← {49}
- Group 51 ← {50}
- Group 52 ← {51}
- Group 53 ← {52}
- Group 54 ← {53}
- Group 55 ← {54}
```

**Total: 8 groups . 8 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (76 items)

## Shipped

History: docs/roadmap-shipped.md
