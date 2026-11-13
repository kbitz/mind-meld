# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Seven more Tracks shipped through v1.12.42 (independent receipts in shipped history). Track 132A is active; the remaining output work and encountered bugs are planned below. The owner will decide the path to 2.0 after using the Syncthing fleet for a while. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

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

Track 132A is declared active by the manager’s 2026-11-13 brief (factory session for Track 132A); its complete card and ID are pinned. No other active pin is declared.

### Group 132: Quiet notices ∥ named peer remedies ∥ conflict settlement ∥ update observation and test

_Depends on: none_

**This group delivers:** Quiet recapture notices, consistent Antigravity advice, actionable peer remedies, durable conflict settlement, the open real-machine automatic-update observation and a stable detached-child test. Only Track 132A is in flight; its disjoint siblings may launch.

#### Track 132A: Keep recapture's own push quiet about nested repositories, and give the push and status the same Antigravity advice
_2 tasks . ~170 LOC . low risk . 5 files_
**Outcome:** An owner who runs `mm recapture` is not shown one "skipped: nested git repository" line per checkout, and the push notice and `mm status` give one advice, not two that contradict each other, when Antigravity's record format is one no release reads.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.1 ("nothing else is printed unless the owner must act") and O7.3. The owner, 2026-11-09: "Anything that is a reproducible bug we actually encountered should be current"; observed as 107 notice lines in one push and as `mm status` and the push notice disagreeing about Antigravity.
**Done when:** the acceptance list below passes: recapture's inner push prints no nested-repository line, a plain `mm push` and its `-v` and `--dry-run` forms behave as today, and the push notice and `mm status` agree for a format no release reads.
_touches: src/mind_meld/cli.py, src/mind_meld/events_tail.py, tests/test_integration.py, tests/test_silent_failure_contract.py, docs/invariants/events-retro.md_
_blocked-by: Track 129A, Track 131A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md ("Nested git checkouts are frozen"), docs/invariants/events-retro.md, src/mind_meld/cli.py (`_push_core`'s `diagnostics`, `show` and `on_skip`; `recapture`'s `_push_core` call), src/mind_meld/events_tail.py (`_host_skip_phrase`, `_no_release_reads_clause`)_
_produces: a recapture push that prints no nested-repository notices, and one advice for an unreadable Antigravity format_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: manager Ephesus 17's observations on mm 1.12.30, 2026-11-09 (Future entries "`mm recapture`'s own push prints one \"skipped: nested git repository\" notice per checkout (finding 7, corrected)" and the 2026-11-09 half of "Antigravity reader logs `dropped: unsupported` on this Mac", whose unsupported-cause half is Track 123B), promoted on the owner's rule above. Verified at `e04cfa0`: in `cli.py`, `_push_core` computes `show = not quiet and (diagnostics if diagnostics is not None else True)` and `on_skip` prints "mm: notice: skipped: nested git repository ..." when `show` is true; the attended push passes `diagnostics=verbose or dry_run` (so a plain `mm push` is silent, pinned by `tests/test_integration.py` for `push -v`, `push --dry-run` and `autopush`), while `recapture`'s inner `_push_core(config, passphrase, memory_kb, verbose=False, dry_run=False)` leaves `diagnostics` unset and therefore prints them; which command printed the observer's 107 lines is not confirmed (the manager's finding said every push; it is `mm recapture` or `mm push -v` by the code). For the advice: `_host_skip_phrase` reaches `_no_release_reads_clause` ("no newer mm can yet") only when the status or diag caller passes `no_release_reads`, while push, init and attempt-record callers keep the default and fall through to the permanent-reason text that recommends `mm update`._

- **Recapture's push prints no nested-repository lines** -- Pass `diagnostics=False` from recapture's inner `_push_core` (or the equivalent the plan prefers), so the notice stays what it is today for `mm push -v` and `--dry-run` and silent elsewhere; a test beside the existing three pins recapture. _src/mind_meld/cli.py, tests/test_integration.py, ~40 lines._ (S)
- **One advice for an Antigravity format no release reads** -- Have the push-side notice read the same cached upgrade view as `mm status` and `mm diag` (or drop the `mm update` suggestion for a reason no release reads), so the two surfaces agree whenever `no_release_reads` names the reader; tests pin both surfaces for the same fixture, and events-retro.md says where the push gets the view. _src/mind_meld/events_tail.py, tests/test_silent_failure_contract.py, docs/invariants/events-retro.md, ~130 lines._ (M)

**Acceptance:**

1. `mm recapture` on a fixture with nested checkouts prints no "skipped: nested git repository" line; a test pins it.
2. `mm push`, `mm push -v`, `mm push --dry-run` and `mm autopush` print exactly what they print today for nested checkouts; the existing tests still pass unchanged.
3. For an Antigravity record format no release reads, the push notice and `mm status` give the same advice (neither recommends `mm update` when the other says no newer mm can read it); a test pins both for one fixture.
4. For a format a newer release does read, both surfaces still recommend `mm update`; a test pins it.
5. docs/invariants/events-retro.md records where the push-side notice gets the upgrade view.
6. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

#### Track 132B: Observe one automatic update on a real machine (Track 124B's post-merge check)
_1 task . ~10 LOC . low risk . 1 file_
**Outcome:** The owner has seen one of their own machines, on a tracking install one release behind, update mm on its next push or autopush with no `mm update`, using the credential-based release check Track 124B shipped (v1.12.34).
**Supports:** MVP-1 on Syncthing; docs/SPEC.md Constraints ("Auto-update upgrades a pipx `@latest` install at the end of its next `push`, `pull`, `autopull` or `autopush`, at most once a day"). Track 124B's acceptance criterion 9, its only open obligation.
**Done when:** the observation (`mm --version` before and after, the `upgrade-state.json` fields and `mm status`, placeholders only) is recorded as Track 124B's post-merge row.
_touches: docs/invariants/auto-upgrade.md_
_session: fresh · effort: low · verify: ./bin/check --affected_

_Source: Track 124B (PR #307, merged 2026-11-10 at `ea8215b`, v1.12.34) met acceptance criteria 1 to 8; the PR names criterion 9 "the explicitly post-merge real-machine observation". Observed 2026-11-11 01:55 UTC on `linux-a`: `auto-update.log` records an automatic `pipx upgrade` from 1.12.30 to 1.12.35, run by 1.12.30, which predates 124B's check, so it does not satisfy criterion 9. The next release after 1.12.35 gives the first chance for 124B's own code to do it._

_Latest check, 2026-11-13 at `bbbe943`: the Linux home machine's upgrade-state.json still has checked_at and attempted_at 2026-11-11T01:55:05Z, latest_version and last_seen_self_version 1.12.35; the file has not advanced since 2026-11-11 while HEAD is 1.12.42. Code does not prove this machine was inactive: upgrade.check_for_upgrade returns before cache access for a dev build, invocation skip or auto_check=false; cache I/O can also fail. A failed fetch normally advances attempted_at and records a cause, so the unchanged attempted_at is not evidence of an ordinary recent network failure. cli.autopush can return from _auto_command_scope or no-sources before update_or_nudge; reaching the normal tail does check even without content changes. Establish which path ran using noncredential local run evidence before diagnosing a repair; no update was induced during regeneration._

- **Record the observation** -- After the next release, on one machine on a tracking install one release behind, let its next push or autopush update mm, and record `mm --version` before and after, the `upgrade-state.json` fields and `mm status` (placeholders only) as Track 124B's post-merge row; if it does not update, file the cause in TODOS.md. _docs/invariants/auto-upgrade.md only if the observation contradicts it, ~50 lines._ (S)

#### Track 132C: Name every old peer in retro recovery advice
_2 tasks . ~155 LOC . low risk . 2 files_
**Outcome:** The owner can tell exactly which peer needs upgrading, even when rejected rows include unidentified devices; coverage notes read naturally.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O7.3 and O8.6, plus the owner-approved 2026-11-13 regeneration brief.
**Done when:** Exact-output tests cover one old peer, all-old, mixed, and named-old plus unidentified rejected rows; named peers are explicit, unknown peers are never claimed old, and malformed registry inputs remain unknown.
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_usage_presentation.py_

_read-first: docs/invariants/README.md, docs/invariants/events-retro.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Owner 2026-11-12 “1”; shadow/129A/carbon-final.txt (data) and the regeneration brief. _attended_usage_remedy's all-old branch never computes labels, and sessions_incomplete emits only a peer count. Verified against `bbbe943` on 2026-11-13._

- **Name and separate remedies** -- Name proven-old peers in rejected, all-old, mixed and sessions_incomplete notes; unidentified/current/unknown peers get push-or-delivery advice without an unsupported upgrade claim. Remove duplicated “for” and awkward “then last reported” joins; preserve version gates and counters. _src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_usage_presentation.py, ~110 lines._ (M)
- **Malformed registry containers** -- Close the recorded test gap from Track 129A: malformed containers, records and IDs stay unknown and never generate an upgrade recommendation. _src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_usage_presentation.py, ~45 lines._ (S)

**Acceptance:** Exact-output tests cover one old peer, all-old, mixed, and named-old plus unidentified rejected rows; named peers are explicit, unknown peers are never claimed old, and malformed registry inputs remain unknown. `./bin/check --affected` passes; full portable and platform checks remain CI obligations.

#### Track 132D: Settle resolved conflicts before reporting to stdout
_2 tasks . ~170 LOC . low risk . 2 files_
**Outcome:** A closed output pipe after resolving a file cannot turn a completed disk operation into a false failure or leave its pull ledger unsettled.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O7.3 and O8.6, plus the owner-approved 2026-11-13 regeneration brief.
**Done when:** Each successful disk mutation remains counted and settled after output failure; no false rename/delete failure and no repeated conflict on the next pull; genuine disk failures stay failures; nested Grok symlinks are skipped.
_touches: src/mind_meld/resolveflow.py, tests/test_conflict_copy.py_

_read-first: docs/invariants/README.md, docs/invariants/conflicts.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Owner 2026-11-12 “1 a”; shadow/130B/carbon-final.txt (data). Six mutation branches print inside except-OSError scope before incrementing resolved; BrokenPipeError is an OSError. This predates Track 130B. Verified against `bbbe943` on 2026-11-13._

- **Separate disk effects from reporting** -- For every rename/unlink resolution branch, account for committed disk effects and settle _record_resolution independently of stdout; preserve genuine filesystem failures and avoid retrying a completed mutation. Pin BrokenPipeError and EIO after local/remote and orphan choices with ledger assertions. _src/mind_meld/resolveflow.py, tests/test_conflict_copy.py, ~130 lines._ (M)
- **Nested Grok directory links** -- Close the existing Future test gap: a linked directory nested within a real Grok skills directory yields no conflict hit through the link. _src/mind_meld/resolveflow.py, tests/test_conflict_copy.py, ~40 lines._ (S)

**Acceptance:** Each successful disk mutation remains counted and settled after output failure; no false rename/delete failure and no repeated conflict on the next pull; genuine disk failures stay failures; nested Grok symlinks are skipped. `./bin/check --affected` passes; full portable and platform checks remain CI obligations.

#### Track 132E: Wait for the detached pipx test child to finish writing
_1 tasks . ~55 LOC . low risk . 1 files_
**Outcome:** The portable suite stops intermittently parsing an empty or partial result from its detached synthetic child.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O7.3 and O8.6, plus the owner-approved 2026-11-13 regeneration brief.
**Done when:** The detached test waits for complete JSON under a bounded deadline and still proves its existing subprocess contract.
_touches: tests/test_self_update.py_

_read-first: docs/invariants/README.md, docs/invariants/auto-upgrade.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Encountered test failure in the regeneration brief: TestPipxSeams.test_pipx_subprocesses_receive_no_prompt_env_closed_stdin_and_new_session[True]. The parent polls only exists(), while Path.write_text creates the child output before completing its JSON write. Verified against `bbbe943` on 2026-11-13._

- **Wait for complete child evidence** -- Use bounded completion evidence rather than file existence; retain assertions on closed stdin, new session and noninteractive environment. Make incomplete output retryable and genuine timeout fail with useful evidence; never run real pipx. _tests/test_self_update.py, ~55 lines._ (S)

**Acceptance:** The detached test waits for complete JSON under a bounded deadline and still proves its existing subprocess contract. `./bin/check --affected` passes; full portable and platform checks remain CI obligations.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131_

### Group 133: Remaining command output

_Depends on: Group 132_

**This group delivers:** Status, gc, diff, log, recapture and update notices use the shared output style, with deferred-stream and zero-count tests.

#### Track 133A: Bring status, gc, diff, log, recapture and the update notices to the shared style
_4 tasks . ~310 LOC . low risk . 11 files_
**Outcome:** An owner who runs `mm status`, `mm gc`, `mm diff`, `mm log` or `mm recapture`, or reads the update notice a push prints, sees the same outcome, warning, error and fix-command forms as push and pull.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.6 as amended 2026-11-10. The owner, 2026-11-10: "they all need to look like they come from the same tool".
**Done when:** the acceptance list below passes: each site the survey names prints in the shared style, with tests pinning the new text.
_touches: src/mind_meld/cli.py, src/mind_meld/retention.py, src/mind_meld/pullhistory.py, src/mind_meld/statusflow.py, src/mind_meld/upgrade.py, tests/test_retention.py, tests/test_gc_events.py, tests/test_pullhistory.py, tests/test_statusflow.py, tests/test_self_update.py, tests/test_integration.py_
_blocked-by: Track 130A, Track 132A_
_read-first: docs/invariants/README.md (the output style Track 129B wrote), src/mind_meld/cli.py (`gc`, `diff_cmd`, `recapture`), src/mind_meld/retention.py, src/mind_meld/pullhistory.py, src/mind_meld/statusflow.py, src/mind_meld/upgrade.py (`format_upgrade_message`, `_auto_install`'s failure notice)_
_produces: status, gc, diff, log, recapture and the update notices in the shared style_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: the survey in Track 129B's source note, verified at `a6f3e73`. Sites that do not follow the shared style once 129B has moved `core._error`: `mm gc` ends `[bold green]GC complete.[/bold green]` (`cli.py`) and `retention.py` warns in `[yellow]` with "rerun `mm gc`"; `mm recapture` prints a literal `[red]Error:[/red]` and ends `[bold green]Recapture complete.`; `mm diff` warns `[yellow]Warning:[/yellow] remote manifest for ...`; `mm log` (`pullhistory.py`) gives "run `mm update`"; `mm status` gives "(run `mm push`)" in two places beside its own `Run: mm diag`; the update nudge ends "— run `<install command>`" and the failed automatic update "run `mm update`, or `<reinstall>`" (`upgrade.py`). `install-skills` already prints `mm: error:` and `recover` errors through `core._error`, so neither is in this Track._

- **Errors, warnings and fix commands** -- Move the `gc`, `diff`, `recapture`, `log` and `status` warnings and errors listed above, and the update nudge and failure notices, to the shared helpers, with each fix command as `Run: <command>` printed last. _src/mind_meld/cli.py, src/mind_meld/retention.py, src/mind_meld/pullhistory.py, src/mind_meld/statusflow.py, src/mind_meld/upgrade.py, tests/test_retention.py, tests/test_pullhistory.py, tests/test_statusflow.py, tests/test_self_update.py, ~140 lines._ (M)
- **Outcome lines** -- `mm gc` and `mm recapture` end with an outcome line in the shared form instead of bold green "complete" lines; counts the owner does not act on move behind `-v`. _src/mind_meld/cli.py, tests/test_gc_events.py, tests/test_integration.py, ~80 lines._ (S)

- **Deferred update stream failures** -- Close the recorded no-defect test gap from Track 130A (`459828f`): populate the deferred display, fail its write and flush, and assert no installer retry or changed sync exit code. _src/mind_meld/cli.py, tests/test_self_update.py, ~45 lines._ (S)
- **Zero pending capture** -- Close the recorded no-defect test gap from PR #310 (`97ae07a`): incomplete discovery with zero pending repositories must not invent a positive catching-up prefix in status. _src/mind_meld/statusflow.py, tests/test_statusflow.py, ~45 lines._ (S)

**Acceptance:**

1. Each site in the source note prints the shared form; no `[yellow]Warning:`, `[red]Error:` or "run `...`" fix form remains in the touched files; tests pin each command's new text.
2. `mm gc` and `mm recapture` end with one outcome line in the shared form; tests pin both.
3. `mm status` still opens with its one-line verdict (O8.4); a test pins it.
4. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

5. Deferred-update stream failures preserve the sync result without retry; zero pending capture has no positive catching-up count.

### Group 134: Empty conflict outcomes

_Depends on: Group 133_

**This group delivers:** Conflicts and resolve use the shared style even when there is nothing to resolve.

#### Track 134A: Use shared outcomes when conflicts and resolve find nothing
_1 tasks . ~50 LOC . low risk . 2 files_
**Outcome:** An owner with no conflict files sees the shared plain outcome style from both conflict commands.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O7.3 and O8.6, plus the owner-approved 2026-11-13 regeneration brief.
**Done when:** Both empty commands print one shared plain outcome with no green markup, keep exit semantics and perform no inspection writes.
_touches: src/mind_meld/cli.py, tests/test_integration.py_
_blocked-by: Track 133A_
_read-first: docs/invariants/README.md, docs/invariants/README.md (shared output style)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Regeneration brief item 2; Track 130B intentionally excluded these CLI lines. Correction: both sites currently say [green]No conflict files.[/green], not “No unresolved conflicts.” Verified against `bbbe943` on 2026-11-13._

- **Unify empty results** -- Use the existing shared outcome helper in conflicts and resolve empty-result paths, with exact output and exit-code assertions; inspection remains read-only. _src/mind_meld/cli.py, tests/test_integration.py, ~50 lines._ (S)

**Acceptance:** Both empty commands print one shared plain outcome with no green markup, keep exit semantics and perform no inspection writes. `./bin/check --affected` passes; full portable and platform checks remain CI obligations.

### Execution Map

A Group may launch when every Group in its ← set has landed. Document order is priority, not gating.

```
- Group 132 ← {}
- Group 133 ← {132}
- Group 134 ← {133}
```

**Total: 0 phases . 3 groups . 7 tracks remaining.**

The 2.0 cut still needs the owner’s O1 to O4 Syncthing observations on one Mac and both Linux machines, with O6 to O8 holding, recorded in PROGRESS.md Acceptance. A Mac not yet moved holds nothing up; publication is a separate owner decision.

---

## Future

Deferred: docs/roadmap-future.md (49 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
