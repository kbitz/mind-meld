# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Five more Tracks shipped through v1.12.47 (independent receipts in shipped history). Track 132D is active in review; two encountered bugs (gstack-extend run history frozen by an empty `.git` directory, and a manifest property test failing under host load) and the remaining output fixes are planned below. The owner will decide the path to 2.0 after using the Syncthing fleet for a while. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

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

Track 132D is declared active by the 2026-11-15 brief (PR #324, factory branch `mmm/e01a68d2`, in review); its card and ID are pinned. No other active pin is declared. Track 132B waits on its real-machine observation.

### Group 132: Encountered sync and test bugs ∥ conflict settlement ∥ update observation

_Depends on: none_

**This group delivers:** gstack-extend run history and any folder holding an empty `.git` placeholder sync again, the manifest property tests stop failing falsely on a loaded host, resolved conflicts settle even when stdout fails, and the open real-machine automatic-update observation. Only Track 132D is in flight; its disjoint siblings may launch.

#### Track 132B: Observe one automatic update on a real machine (Track 124B's post-merge check)
_1 task . ~10 LOC . low risk . 1 file_
**Outcome:** The owner has seen one of their own machines, on a tracking install one release behind, update mm on its next push or autopush with no `mm update`, using the credential-based release check Track 124B shipped (v1.12.34).
**Supports:** MVP-1 on Syncthing; docs/SPEC.md Constraints ("Auto-update upgrades a pipx `@latest` install at the end of its next `push`, `pull`, `autopull` or `autopush`, at most once a day"). Track 124B's acceptance criterion 9, its only open obligation.
**Done when:** the observation (`mm --version` before and after, the `upgrade-state.json` fields and `mm status`, placeholders only) is recorded as Track 124B's post-merge row.
_touches: docs/invariants/auto-upgrade.md_
_session: fresh · effort: low · verify: ./bin/check --affected_

_Source: Track 124B (PR #307, merged 2026-11-10 at `ea8215b`, v1.12.34) met acceptance criteria 1 to 8; the PR names criterion 9 "the explicitly post-merge real-machine observation". Observed 2026-11-11 01:55 UTC on `linux-a`: `auto-update.log` records an automatic `pipx upgrade` from 1.12.30 to 1.12.35, run by 1.12.30, which predates 124B's check, so it does not satisfy criterion 9. The next release after 1.12.35 gives the first chance for 124B's own code to do it._

_Latest check, 2026-11-13 at `bbbe943`: the Linux home machine's upgrade-state.json still has checked_at and attempted_at 2026-11-11T01:55:05Z, latest_version and last_seen_self_version 1.12.35; the file has not advanced since 2026-11-11 while HEAD is 1.12.42. Code does not prove this machine was inactive: upgrade.check_for_upgrade returns before cache access for a dev build, invocation skip or auto_check=false; cache I/O can also fail. A failed fetch normally advances attempted_at and records a cause, so the unchanged attempted_at is not evidence of an ordinary recent network failure. cli.autopush can return from _auto_command_scope or no-sources before update_or_nudge; reaching the normal tail does check even without content changes. Establish which path ran using noncredential local run evidence before diagnosing a repair; no update was induced during regeneration._

_Check at regeneration, 2026-11-15 at `888ada2`: the Linux home machine now runs 1.12.47, and its upgrade-state.json has checked_at and attempted_at 2026-11-15T02:47:52Z with latest_version and last_seen_self_version 1.12.47 and no recorded cause, so the release check is running again. Its auto-update.log has no entry after the 2026-11-11 upgrade to 1.12.35, so the move from 1.12.35 to 1.12.47 is not recorded as an automatic update by 124B's code and criterion 9 stays open; that machine is now current, not one release behind._

- **Record the observation** -- After the next release, on one machine on a tracking install one release behind, let its next push or autopush update mm, and record `mm --version` before and after, the `upgrade-state.json` fields and `mm status` (placeholders only) as Track 124B's post-merge row; if it does not update, file the cause in TODOS.md. _docs/invariants/auto-upgrade.md only if the observation contradicts it, ~50 lines._ (S)

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

#### Track 132F: Stop an empty `.git` directory from freezing a synced folder (gstack-extend run history stopped syncing)
_3 tasks . ~170 LOC . medium risk . 8 files_
**Outcome:** Each machine's gstack-extend run history (`~/.gstack-extend/analytics/stage-runs/<host>.jsonl`, written owner-only) reaches every other machine again, and an empty placeholder `.git` directory anywhere in a synced source no longer stops that subtree from publishing or receiving.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O1 (sync converges across macOS and Linux) and O6 (the allowlisted gstack-extend run history). Encountered: the Linux home machine's own file is current (last row 2026-11-15T02:36Z) while its copy of the laptop's file stops at 2026-11-09T04:07Z, and the laptop's copy of the home machine's file stops on 2026-11-09.
**Done when:** the acceptance list below passes: an empty `.git` directory freezes nothing, a real checkout (`.git/HEAD`, or a `.git` file) freezes exactly as today, and a previously frozen entry publishes its current bytes once the marker is found to be empty.
_touches: src/mind_meld/manifest.py, tests/conftest.py, tests/test_manifest.py, tests/test_integration.py, tests/test_diag.py, tests/test_grok_memory.py, tests/test_track_30a.py, docs/invariants/sync.md_
_read-first: docs/invariants/README.md, docs/invariants/sync.md ("Nested git checkouts are frozen, never published"), src/mind_meld/manifest.py (`_directory_is_git_repo`, `nested_repo_root`, `nested_repo_roots_for_paths`), src/mind_meld/cli.py (`_freeze_nested_checkout_entries`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: the 2026-11-15 roadmap brief, encountered bug 1 (gstack-extend's manager, confirmed by the manager on the Linux home machine). The brief's hypotheses were the 0600 mode or replace-by-rename; the code shows neither. Verified at `888ada2`: `manifest._directory_is_git_repo` returns `stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode)` for `<dir>/.git` without checking that a repository is there, so `nested_repo_root` treats `analytics/` as a nested checkout; push then copies the prior entries forward unchanged and pull never writes inside it (docs/invariants/sync.md). On the Linux home machine `~/.gstack-extend/analytics/.git` is an empty directory created 2026-11-10 18:17Z, beside other empty placeholders made in the same second (the pattern of a sandboxed agent run, not gstack-extend); the last-push manifest still holds that machine's file at its 2026-11-09 size, and every pull since 2026-11-11 logs `gstack-extend analytics excluded`. Killed: mm never reads file mode on scan, allowlist or pull, and gstack-extend's `telemetry.append_row` only appends with `O_APPEND|O_CREAT`, mode 0600, never renames and writes only its own host's file. The two hosts whose files still sync have 0644 files only because those are copies mm pulled. Whether the laptop also has the placeholder was not checked; the home machine's freeze alone explains both directions it reports. Workaround until this ships: `rmdir ~/.gstack-extend/analytics/.git` on an affected machine, then `mm push`._

- **A repository needs `.git/HEAD`** -- Count a `.git` directory as a nested checkout only when `lstat` finds `HEAD` inside it (a file or symlink); keep accepting a regular `.git` file (worktrees, submodules); in strict mode keep refusing on probe errors other than not-found. The same predicate feeds pull's `nested_repo_roots_for_paths`, so both directions recover. File modes are unchanged: mm writes no mode on the files it reads, and the anchor stays 0600. _src/mind_meld/manifest.py, docs/invariants/sync.md, ~25 lines._ (S)
- **Fixtures make real markers** -- Replace bare `.git` `mkdir` calls that stand for a nested checkout with a helper that also writes `.git/HEAD`. _tests/conftest.py, tests/test_manifest.py, tests/test_integration.py, tests/test_diag.py, tests/test_grok_memory.py, tests/test_track_30a.py, ~80 lines._ (M)
- **Empty-marker cases** -- Add `TestNestedRepositories` cases (empty `.git` directory freezes nothing; `.git/HEAD` and a `.git` file still freeze; unreadable `HEAD` probe still refuses in strict mode) and one integration test where a frozen entry from the prior manifest publishes its current bytes with no tombstones and a peer receives them. _tests/test_manifest.py, tests/test_integration.py, ~70 lines._ (S)

**Acceptance:**

1. An empty `<root>/sub/.git/` directory freezes nothing: files under `sub/` publish on push and peer files under it are written on pull; a test pins both.
2. A `.git` directory with `HEAD`, and a regular `.git` file, still freeze and stay excluded exactly as today; the existing freeze and delete-convergence tests pass.
3. An integration test: an entry frozen in the prior manifest publishes its current bytes once the `.git` is found to be empty, with no tombstones, and a peer receives them.
4. In strict mode, an unreadable `.git/HEAD` probe still refuses with the existing snapshot error.
5. docs/invariants/sync.md says what counts as a nested checkout.
6. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

#### Track 132G: Keep the manifest property tests from failing on a loaded host
_1 task . ~25 LOC . low risk . 1 file_
**Outcome:** The developer's `./bin/check --affected` runs stop failing falsely when two verifies overlap on one host, while the manifest property tests keep checking the same properties with the same example counts.
**Supports:** MVP-1 on Syncthing; the release discipline in AGENTS.md (`./bin/check` is the verification entry point). Encountered on 2026-11-14: Hypothesis `HealthCheck.too_slow` failed both Track 132C's and Track 132D's implement verify at a load average of about 24; each passed when run alone.
**Done when:** every Hypothesis test in `tests/test_manifest_fuzz.py` runs with no deadline and with `HealthCheck.too_slow` suppressed, keeps its current `max_examples`, and the file still passes `./bin/check tests/test_manifest_fuzz.py`.
_touches: tests/test_manifest_fuzz.py_
_read-first: tests/test_manifest_fuzz.py_
_session: fresh · effort: low · verify: ./bin/check tests/test_manifest_fuzz.py_

_Source: the 2026-11-15 roadmap brief, encountered bug 2. The three tests named there are `test_load_manifest_round_trip_preserves_keys`, `test_v1_promotion_migrates_bare_tombstone_keys` and `test_normalize_never_crashes_on_arbitrary_dict`. Verified at `888ada2`: every `@settings(...)` in the file sets only `max_examples` (100 or 200), so Hypothesis's default 200 ms deadline and its `too_slow` health check apply; no `register_profile` exists in `tests/conftest.py`. `tests/test_safe_str.py` also uses Hypothesis but has not failed, so it is out of scope._

- **Loaded-host settings** -- Add one file-local settings object (`deadline=None`, `suppress_health_check=[HealthCheck.too_slow]`) and apply it to each `@settings` in the file, keeping each test's `max_examples`; no strategy or assertion changes. _tests/test_manifest_fuzz.py, ~25 lines._ (S)

**Acceptance:**

1. Every `@given` test in `tests/test_manifest_fuzz.py` has `deadline=None` and suppresses `HealthCheck.too_slow`, with its current `max_examples`.
2. No strategy, assertion or test is removed or weakened.
3. `./bin/check tests/test_manifest_fuzz.py` passes, and the full `./bin/check` passes in CI.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 133, 134_

### Group 135: Recapture quiet and one Antigravity advice

_Depends on: Group 132_

**This group delivers:** A plain `mm recapture` prints only its own outcome, and status, diag and recapture give the same Antigravity advice as the push.

#### Track 135A: Quiet recapture's inner push, and give status, diag and recapture the push's Antigravity advice
_4 tasks . ~230 LOC . low risk . 8 files_
**Outcome:** A plain `mm recapture` ends with its own outcome line and no "Push complete." block, timings or counts, and `mm status`, `mm diag` and `mm recapture` no longer suggest `mm update` beside a verdict that says no newer mm can read an unsupported reader's format.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.1 ("nothing else is printed unless the owner must act") and O8.6, finishing what Tracks 132A and 133A started on the same surfaces.
**Done when:** the acceptance list below passes: non-verbose recapture prints no inner-push summary, `-v` still shows it, and the stored auto-push detail, diag's last-autorun block and recapture's tail agree with the status verdict for a format no release reads.
_touches: src/mind_meld/recaptureflow.py, src/mind_meld/cli.py, src/mind_meld/events_tail.py, src/mind_meld/statusflow.py, tests/test_integration.py, tests/test_silent_failure_contract.py, tests/test_statusflow.py, tests/test_init_events_backfill.py, docs/invariants/events-retro.md_
_blocked-by: Track 132F_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, docs/invariants/sync.md ("Nested git checkouts are frozen"), src/mind_meld/recaptureflow.py (the `push_core(...)` call), src/mind_meld/cli.py (`_push_core`'s `diagnostics` and `result_observer`), src/mind_meld/events_tail.py (`_host_skip_phrase`, `_no_release_reads_clause`), src/mind_meld/statusflow.py (the `Last auto-push` breadcrumb render)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: four inbox items filed by the reviews of Tracks 132A (`88cb0b0`) and 133A (`d9180e7`), verified at `888ada2`. Recapture: `recaptureflow` calls `push_core(config, passphrase, memory_kb, verbose=False, dry_run=False, report_nested_repositories=False)` with neither `diagnostics` nor `result_observer`, so `_push_core` keeps its display and prints "Push complete." (`cli.py`) and its counts before `Recapture:`; running a non-verbose `mm recapture` shows it. Advice: the review probe (an autopush, then `mm status`, `agy` unsupported, current cache) printed the stored "run `mm update`" detail on the `Last auto-push` line beside "no newer mm can yet", identically at the merge base; recapture's tail capture writes the same default sentence. This is the same disagreement the developer saw on 2026-11-09 (Track 132A), on the surfaces 132A left alone. Ordered after Track 132F, which shares `tests/test_integration.py`, so the encountered sync bug lands first. Deferred test gap "Git pending count cutoff fallbacks" (events_tail.py, tests/test_silent_failure_contract.py) meets its trigger here but stays in Future because this card is at the session-weight limit._

- **Recapture's inner push is quiet** -- Run recapture's push with its display off unless `-v`, so only recapture's own outcome (and any failure with its fix) prints; `-v` keeps today's inner detail. _src/mind_meld/recaptureflow.py, src/mind_meld/cli.py, tests/test_integration.py, ~50 lines._ (S)
- **One advice everywhere for a format no release reads** -- Make the autopush breadcrumb detail, diag's last-autorun block and recapture's tail notice use the cached upgrade view the push and status already read, so none recommends `mm update` when the view says no newer release reads the format; a format a newer release reads still gets `mm update`. _src/mind_meld/events_tail.py, src/mind_meld/statusflow.py, tests/test_statusflow.py, tests/test_silent_failure_contract.py, docs/invariants/events-retro.md, ~110 lines._ (M)
- **Recapture positive control** -- Close the recorded test gap from Track 132A: give `test_recapture_and_plain_push_stay_silent_on_nested_checkouts` an observation that recapture's own push reached the source (for example an ordinary file outside the checkout that it must publish). _tests/test_integration.py, ~30 lines._ (S)
- **Current-cache advice cases** -- Close the recorded test gap from Track 132A: pin recapture's and init's unsupported-reader notice with a current cache, and add a two-reader case to the push/status parity test. _tests/test_init_events_backfill.py, tests/test_silent_failure_contract.py, ~40 lines._ (S)

**Acceptance:**

1. A non-verbose `mm recapture` prints no "Building manifest", "Uploading", "Push complete." or count lines from its inner push; `mm recapture -v` still does; a test pins both.
2. With an unsupported reader and a current cache saying no release reads its format, `mm status` after an autopush, plain-text `mm diag` and `mm recapture` give the same advice as the status verdict; a test pins each.
3. For a format a newer release reads, every one of those surfaces still recommends `mm update`; a test pins it.
4. The recapture nested-checkout test fails if recapture's push skips the source.
5. docs/invariants/events-retro.md says where each surface gets the upgrade view.
6. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 136: Truthful conflict reaper and conflict-command lines

_Depends on: Group 132, Group 135_

**This group delivers:** `mm gc --conflicts` stops calling kept conflict copies a failure, and the conflict commands' remaining lines use the shared style.

#### Track 136A: Make gc's conflict reaper and the conflict commands' last two lines tell the truth in the shared style
_3 tasks . ~170 LOC . low risk . 6 files_
**Outcome:** `mm gc --conflicts` with an unresolved conflict copy reports the copy it kept as kept, not as a permissions or locks failure, and still prints its `Gc:` outcome; an unmatched `mm resolve PATH` ends with a `Run:` fix, and the `mm conflicts` listing ends in the shared forms.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.1 and O8.6 (the developer, 2026-11-10: "they all need to look like they come from the same tool").
**Done when:** the acceptance list below passes: kept copies are not remediation, wrong fix commands are gone, and both conflict-command lines use the shared helpers, with tests pinning the new text.
_touches: src/mind_meld/retention.py, src/mind_meld/gcflow.py, src/mind_meld/cli.py, tests/test_conflict_copy.py, tests/test_gc_events.py, tests/test_integration.py_
_blocked-by: Track 132D, Track 135A_
_read-first: docs/invariants/README.md, docs/invariants/conflicts.md, src/mind_meld/retention.py (`ReapOutcome.needs_remediation`, `_render_reap_outcome`), src/mind_meld/gcflow.py, src/mind_meld/cli.py (`conflicts`, `resolve`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: three inbox items filed by the reviews of Tracks 133A (`d9180e7`) and 134A (`411ffde`), verified at `888ada2`. `ReapOutcome.needs_remediation` returns `bool(self.failed or self.skipped or self.repair_failed)`, and the reaper counts deliberate keeps (a live conflict older than 30 days, a `v0-` sidecar, an unreadable age) as `skipped`, so `gcflow` (`elif any(o.needs_remediation ...)`) prints "Fix permissions or locks" with `Run: mm gc -v` (no `--conflicts`) in place of the `Gc:` line; any machine with one old unresolved conflict hits it on the normal path. `cli.py` still ends an unmatched `mm resolve PATH` with `core._error(f"No conflict file matching: {path}")` and no fix, and the `mm conflicts` listing with the markup hint "...or delete files manually with [bold]rm[/bold]." Ordered after Track 135A (shared `cli.py` and `tests/test_integration.py`) and blocked by Track 132D, whose PR #324 also changes `cli.py`, `retention.py` and `tests/test_conflict_copy.py`._

- **Kept copies are not failures** -- Count deliberate keeps separately from skips the owner must fix; only real failures warn, the warning's fix names `mm gc --conflicts -v`, and the `Gc:` outcome line still prints. _src/mind_meld/retention.py, src/mind_meld/gcflow.py, tests/test_conflict_copy.py, tests/test_gc_events.py, ~90 lines._ (M)
- **Conflict-command lines in the shared style** -- An unmatched `mm resolve PATH` ends with `Run: mm conflicts`; the `mm conflicts` listing ends with a shared `Run: mm resolve` line and a `mm: notice:` for pre-v0.9.2 copies; update the test that pins today's unmatched-path stderr. _src/mind_meld/cli.py, tests/test_integration.py, ~60 lines._ (S)
- **Empty-PATH witness** -- Close the recorded test gap from Track 134A: spy on `resolveflow._find_conflict_files` in `test_resolve_empty_path_argument_errors_instead_of_walking` and assert it found the planted sidecar. _tests/test_integration.py, ~20 lines._ (S)

**Acceptance:**

1. `mm gc --conflicts` with one unresolved conflict older than 30 days prints no permissions-or-locks warning and still prints its `Gc:` outcome; a test pins it.
2. A real reap failure still warns, and its fix line names `--conflicts`; a test pins it.
3. An unmatched `mm resolve PATH` ends with `Run: mm conflicts` and exits 1; the `mm conflicts` listing has no Rich-markup prose hint; tests pin both.
4. The empty-PATH resolve test fails if discovery finds no sidecar.
5. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Execution Map

A Group may launch when every Group in its ← set has landed. Document order is priority, not gating.

```
- Group 132 ← {}
- Group 135 ← {132}
- Group 136 ← {132, 135}
```

**Total: 0 phases . 3 groups . 6 tracks remaining.**

The 2.0 cut still needs the owner’s O1 to O4 Syncthing observations on one Mac and both Linux machines, with O6 to O8 holding, recorded in PROGRESS.md Acceptance. A Mac not yet moved holds nothing up; publication is a separate owner decision.

---

## Future

Deferred: docs/roadmap-future.md (54 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
