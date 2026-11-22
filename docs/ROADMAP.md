# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Cross-model memory setup and the Codex source fold shipped by v1.12.80. The owner will decide the path to 2.0 after using the Syncthing fleet. [PROGRESS.md](PROGRESS.md#acceptance) records achieved checkpoints.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **A reproducible bug the owner actually encountered is admitted, not deferred (owner, 2026-11-09).** After the manager said the codex `skills/gstack/*` exclusion was not in the two Tracks being carded, the owner asked "why isn't that fix in our current roadmap? Anything that is a reproducible bug we actually encountered should be current". A Future entry that records a bug seen on the owner's machines, with a cause found in the code, goes to Current Plan on the next regeneration; deferral is for work nobody has hit, design questions and claims nobody verified, and the manifestation gate still prunes defects nobody has hit.
- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval, introducers and mesh step (adding a device a fleet member's encrypted record names; spec amended 2026-11-18) on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration and scan requests are refused until the spec's revisit trigger is observed. Track 154A shipped retirement removal and fleet-name correction on mm-managed machines.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) documented in the shared output helpers.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared as of 2026-11-22. Both Tracks in the previous plan shipped and have independent receipts in [shipped history](roadmap-shipped.md).

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161_

### Group 162: Actionable qmd upgrade output ∥ load-stable capture tests

_Depends on: none_

**This group delivers:** a copyable qmd upgrade command when memory-index enablement refuses an old version, and two capture tests whose results do not depend on CI worker scheduling.

#### Track 162A: Print the qmd upgrade command when memory-index enablement refuses an old version
_3 tasks . ~100 LOC . low risk . 2 files_
**Outcome:** an owner whose qmd is too old can copy the last command printed by `mm memory-index on` to install a supported version, then retry enablement.
**Supports:** [SPEC](SPEC.md) O8.1 and O8.6 (shared output and a fix command last), and O9.4 (the owner can enable the index after setup). The owner hit the old-qmd refusal on macOS on 2026-11-22.
**Done when:** the real command path reports the installed version and minimum, exits 1 without enabling the index, and ends stderr with the copyable command selected by `memoryindex.install_hint()`.
**Acceptance:**
1. A stub reporting qmd 2.8.2 yields the shared error form with the detected version and minimum; the final line is `Run: <command>`, with the existing Bun or npm install command for that environment. The failed preflight leaves index settings and collections intact.
2. Retain the existing optional setup behavior and test for `mm init`: `initflow._ask_memory_setup` already prints the install command in the old-version branch and skips only the optional index prompt. Preserve working qmd enablement and read-only inspection.
3. With malformed successful qmd collection output, refresh records failure before collection edits or update. With an active refresh that releases the lock within the disable retry window, `mm memory-index off` persists the disabled setting and removes the managed collections.
_release: patch (ordinary release under SPEC)_
_touches: src/mind_meld/memoryindex.py, tests/test_memory_index.py_
_read-first: docs/SPEC.md O8 and O9.4, docs/invariants/README.md (shared output and memory-index routing), docs/invariants/sync.md (Memory index refresh), src/mind_meld/memoryindex.py (`_enable_index`, `install_hint`)_
_verify: ./bin/check tests/test_memory_index.py tests/test_initflow.py_

_Source: owner report relayed on 2026-11-22. At `301a420`, `_enable_index` passes prose to `core._error(fix=...)`; the bare status view formats the parenthetical version requirement. The optional `mm init` branch already calls `install_hint()`, with coverage in tests/test_initflow.py. The two coverage tasks come from Track 159A's review and coverage audit in docs/TODOS.md._

- Use the existing runtime-specific remedy on the old-version refusal and assert the exact final line and preserved state with the qmd stub. _memoryindex.py, tests/test_memory_index.py, ~45 lines._ (S)
- Pin refresh refusal using a qmd stub that exits zero with malformed list or show output. _tests/test_memory_index.py, ~30 lines._ (S)
- Pin successful disable after refresh releases the lock during the retry budget. _tests/test_memory_index.py, ~25 lines._ (S)

#### Track 162B: Make the attended-capture and usage-merge tests stable under CI load
_2 tasks . ~120 LOC . low risk . 2 files_
**Outcome:** the release checks still catch a broken attended capture boundary or usage-merge regression and avoid false failures caused by an overscheduled runner.
**Supports:** [SPEC](SPEC.md) O7.2 and O8.2 (attended cap and capture budget behavior), and AGENTS.md Testing (portable checks gate merges). Both timing tests were reported on 2026-11-22; the attended-capture test failed twice on macOS CI and passed on rerun.
**Done when:** the two named tests pass repeat runs under normal CI concurrency and controlled contention, with both sides of the capture boundary and a meaningful usage-merge regression check retained.
**Acceptance:**
1. `TestAttendedCaptureCap124A::test_attended_push_finishes_over_capture_budget_under_cap_and_aborts_when_equal` advances a controlled clock at the existing fixture seams. At equal cap and budget it still verifies warm-read and git deadline outcomes and the budget notice. At the larger cap it still verifies host rows, completed session and repository walks, absence of the notice, and cursor advancement.
2. `TestMergeUsageBucketPerf::test_merge_usage_bucket_under_2us_per_call` stops measuring scheduler pauses as merge cost. Preserve a guard against major regressions, for example by budgeting CPU time for the single-threaded loop, and make the test name, rationale and threshold agree. HEAD asserts 10 microseconds per call despite the name.
3. Record targeted and repeated parallel runs for both nodes, including the macOS CI result for the attended test. Keep production behavior and test coverage of the outcomes intact.
_release: patch (ordinary release under SPEC)_
_touches: tests/test_events_budget_scope.py, tests/test_token_usage.py_
_read-first: AGENTS.md Testing, docs/invariants/events-retro.md (attended budgets and usage buckets), tests/test_events_budget_scope.py (`TestAttendedCaptureCap124A`), tests/test_token_usage.py (`TestMergeUsageBucketPerf`)_
_verify: ./bin/check tests/test_events_budget_scope.py tests/test_token_usage.py_

_Source: 2026-11-22 flake report. At `301a420`, the cap fixture uses actual sleeps inside short simulated scans, and the merge microbenchmark times a wall-clock loop. No unfinished Track already covers either file._

- Replace the cap fixture's wall-clock waits with deterministic budget consumption at the test seams. _tests/test_events_budget_scope.py, ~80 lines._ (M)
- Remove scheduler noise from the usage-merge performance guard and align its name with the budget. _tests/test_token_usage.py, ~40 lines._ (S)

### Execution Map

```
- Group 162 ← {}
```

**Total: 1 group, 2 Tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (125 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
