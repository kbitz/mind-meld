# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). The output fixes and the findings the developer approved from the 2026-11-15 full review (Tracks 135A to 147A), the real-machine automatic-update observation (135B), the pending join request shown on fleet machines (148A) and the three bugs the owner hit during the 2026-11-18 acceptance checks on the Syncthing fleet (149A) all shipped by v1.12.67 (independent receipts in shipped history). The plan below carries the two tests that failed intermittently in 149A's release and CI runs on 2026-11-18 (Group 150). The owner will decide the path to 2.0 after using the Syncthing fleet for a while. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **A reproducible bug the owner actually encountered is admitted, not deferred (owner, 2026-11-09).** After the manager said the codex `skills/gstack/*` exclusion was not in the two Tracks being carded, the owner asked "why isn't that fix in our current roadmap? Anything that is a reproducible bug we actually encountered should be current". A Future entry that records a bug seen on the owner's machines, with a cause found in the code, goes to Current Plan on the next regeneration; deferral is for work nobody has hit, design questions and claims nobody verified, and the manifestation gate still prunes defects nobody has hit.
- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval and introducers on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration, removing dead machines and scan requests are refused until the spec's revisit trigger is observed.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) documented in the shared output helpers.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-18). No branch, session or open pull request is active for any Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149_

Group 150 carries the two tests that failed intermittently on 2026-11-18 in Track 149A's release script (`./bin/check` on `linux-a`) and in its pull request's CI (`ci`, macOS), each passing on rerun; neither touched code Track 149A changed. They are admitted under the standing constraint that a reproducible bug seen on the owner's machines or CI goes to Current Plan, and share one Track because both are test-only timing fixes.

### Group 150: Deterministic refresh-record and grandchild-kill tests

_Depends on: none_

**This group delivers:** the release script and CI stop going red on two tests that pass alone, so a red run on either means a real regression.

#### Track 150A: Make the `--affected` refresh test and the release check's grandchild-kill test deterministic
_2 tasks . ~60 LOC . low risk . 3 files_
**Outcome:** The owner's release script and GitHub CI no longer fail on `tests/test_bin_check.py`'s refresh-then-revert test or `tests/test_upgrade.py`'s timeout grandchild test under load; each waits on, or asserts, the condition it proves instead of assuming a timing, and still fails when the behavior it pins breaks.
**Supports:** MVP-1 on Syncthing (docs/SPEC.md release policy: every release runs the full `./bin/check`, and CI runs it before merge; AGENTS.md Testing); the developer's decision of 2026-11-18 ("ok for a longshoreman") to card the two flakes hit that day, under the standing constraint that a reproducible bug seen on the owner's machines or CI goes to Current Plan.
**Done when:** the acceptance list below passes: each test is made deterministic without a retry loop around its assertion and without weakening what it proves, and `src/mind_meld/upgrade.py` is unchanged.
_touches: tests/test_bin_check.py, tests/test_upgrade.py, bin/_check.py_
_read-first: AGENTS.md (Testing: `./bin/check`, `--affected` and `--refresh`), bin/_check.py (`seed_record`, `build_record`, `_merge_parts`, `plan_selection`, `inventory`, `changed_since`), tests/test_bin_check.py (`seeded`, `proj`, `affected`, `TestAffectedSelection`), docs/invariants/README.md (routes `upgrade.py` to docs/invariants/auto-upgrade.md; `bin/check` has no invariant row, its contract is AGENTS.md Testing), docs/invariants/auto-upgrade.md ("No prompts and bounded time": `_run_git` process-group cleanup), src/mind_meld/upgrade.py (`_run_git`)_
_session: fresh · effort: medium · verify: ./bin/check tests/test_bin_check.py tests/test_upgrade.py_

_Source: the Factory Manager's brief of 2026-11-18, relaying the developer's decision; run output treated as data, premises re-verified at `6431854`. (1) The release script's full `./bin/check` (xdist, `linux-a`, Python 3.14.8, Track 149A's v1.12.67 release commit) failed `TestAffectedSelection::test_reverting_dirty_non_python_file_seeded_during_refresh_triggers_full_run` at its `assert "non-Python file changed: README.md" in proc.stderr`, which saw `bin/check: affected: ran 0 of 4 recorded tests: nothing the record reaches changed`; 9,414 other tests passed, and the test alone passed 3 of 3 on the same tree. The brief's mtime hypothesis does not hold at HEAD: selection compares SHA-256 content (`inventory`, `_sha`), not timestamps. The untracked `README.md` written before `--refresh` enters `changed_since` (`git ls-files --others`), so a refresh that writes its record stores it, and `plan_selection` sees it as drifted once unlinked. The observed line is what a run sees when `README.md` never reached the record. The one route found at HEAD: `seed_record` returns the green pytest result (exit 0) after logging `affected: record not updated: <reason>` (`_merge_parts` or `build_record` raising, or "files changed while the suite ran"), which leaves the module-scoped `seeded` fixture's record in place, and the test asserts only the refresh's exit code. (2) GitHub Actions `ci` (macOS, Python 3.13) on PR #350's head `9fdf6fee` failed `TestFetchTagsGitAdapter124B::test_timeout_kills_sleeping_grandchild_and_records_network` at `with pytest.raises(ProcessLookupError): os.kill(grandchild_pid, 0)` with "DID NOT RAISE" (1 failed, 9,412 passed); the job's rerun passed. `upgrade._run_git` sends `SIGKILL` to the child's process group on timeout and stops checking once `os.killpg(pid, 0)` raises `ProcessLookupError` or `PermissionError` (macOS reports a group of only zombies as `EPERM`). The killed grandchild's parent dies with it, so the grandchild is re-parented and stays a zombie until init or launchd reaps it, and `os.kill(pid, 0)` on a zombie succeeds. The test assumes reaping is immediate; mm cannot reap a process that is not its child, and auto-upgrade.md promises bounded group cleanup, not reaping. The product code is correct, so the fix is test-only (`upgrade.py` is on the review-depth protected list). Inbox test gaps from Track 149A's review do not share these files and stay deferred._

- **Make the refresh-then-revert test fail at its cause and remove the cause** -- In `tests/test_bin_check.py`, assert that the `--refresh` step actually wrote its record (its `affected: recorded coverage for 4 tests` line, or the record now listing `README.md`), in this test and in `test_fallback_full_run_records_failed_tests_for_subsequent_affected_run`, so a refresh that keeps the old record fails there with its `record not updated` reason. Then find which `record not updated` reason a loaded xdist run hits, by forcing it (for example a slow or failing coverage part, or a file touched mid-run), and remove it where it lives: in the test's setup if the fixture causes it, or in `bin/_check.py`'s record path if a green refresh can lose its record for a reason unrelated to the change. Do not wrap either assertion in a retry. _tests/test_bin_check.py, bin/_check.py, ~45 lines._ (M)
- **Wait for the killed grandchild to be reaped, not for timing luck** -- In `tests/test_upgrade.py`, after `check_for_upgrade` returns, wait with a bounded deadline (well under the grandchild's 60 s sleep) until `os.kill(grandchild_pid, 0)` raises `ProcessLookupError`, so a killed-but-unreaped zombie is not mistaken for a survivor while a grandchild that escaped the kill still fails the test. Keep the `elapsed < 5.0`, `state == "unknown"` and `check_failure == "network"` assertions unchanged, and leave `src/mind_meld/upgrade.py` alone. _tests/test_upgrade.py, ~15 lines._ (S)

**Acceptance:**

1. The refresh steps of both `TestAffectedSelection` refresh-then-revert tests assert that the record was rewritten, and a test-local forced `record not updated` path fails at that assertion with the reason in its message.
2. The PR names the `record not updated` reason a loaded run hit, shows it forced, and removes it; the refresh-then-revert test then passes under that forced condition (or the condition can no longer arise), with no retry around its assertions.
3. The grandchild test no longer depends on the grandchild having been reaped when `check_for_upgrade` returns, and still fails when the grandchild survives the timeout (shown in the PR by a variant whose grandchild calls `os.setsid()`, so the group kill misses it).
4. `git diff --stat` shows no change to `src/mind_meld/upgrade.py`.
5. `./bin/check tests/test_bin_check.py tests/test_upgrade.py` passes, and the full `./bin/check` passes in CI.

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of document order; document order is priority, not gating.

Adjacency list (from the packer / GROUP_DEPS — not document order):
```
- Group 150 ← {}
```

Track detail per group:
```
Group 150: Deterministic refresh-record and grandchild-kill tests
  +-- Track 150A ......... ~M . 2 tasks
```

**Total: 1 group . 1 track remaining.**

Tracks 135A, 135C, 136A, 137A, 138B, 138A, 139A, 140A, 141A, 142A, 143A, 144A, 145A, 146A, 147A, 148A and 149A shipped in v1.12.51 to v1.12.67, and Track 135B (Track 124B's real-machine automatic-update observation) merged as a documentation-only change between them (independent receipts in shipped history). IDs 95A and 96A stay unused because Future entries cite them.

The 2.0.0 cut is not a Track, and its path is open. SPEC's release line for 2.0.0 is O1 to O4 accepted on the owner's own observation on one Mac and both Linux machines in the Syncthing fleet, with O6 to O8 holding, recorded in PROGRESS.md `## Acceptance`, which records no such checkpoint yet. A Mac not yet moved keeps running on iCloud Drive and holds nothing up. Problems found while machines move are fixed as they appear; Group 149 (v1.12.67) was the first batch of them. Retiring the pre-v0.9.2 conflict format, which the developer approved for 2.0, waits in Future for that line.

---

## Future

Deferred: docs/roadmap-future.md (78 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
