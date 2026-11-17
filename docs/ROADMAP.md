# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Tracks 132G (manifest property tests under host load) and 132F (an empty `.git` directory freezing a synced folder) shipped in v1.12.49 and v1.12.50 (independent receipts in shipped history). The remaining output fixes and the findings the developer approved from the 2026-11-15 full review are planned below, followed by Track 148A (a pending join request shown on fleet machines), which the developer admitted on 2026-11-17. The owner will decide the path to 2.0 after using the Syncthing fleet for a while. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

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

Track 135A is active: factory item 1f4ea780 on branch `mmm/1f4ea780`, in review at `16ed841`, so Group 135 keeps its label and backfills 135B and 135C. Track 132F shipped in PR #327 (v1.12.50) and Track 132G in PR #326 (v1.12.49); Group 132 has no unfinished work left, so its observation Track 132B is now 135B (renames below).

### Group 135: Recapture quiet and one Antigravity advice ∥ pull failure warnings ∥ update observation

_Depends on: none_

**This group delivers:** A plain `mm recapture` prints only its own outcome and every surface gives the push's Antigravity advice (135A, active); every pull apply failure warns with its cause on stderr and the inline conflict prompt never overwrites an edit made while it waits (135C); and the open real-machine automatic-update observation (135B).

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
_Active at regeneration (2026-11-15, `f08f10b`): branch `mmm/1f4ea780` at `16ed841`. The branch also edits `src/mind_meld/attemptlog.py`, `README.md` and `docs/invariants/auto-upgrade.md`, which the card does not list; Track 135B edits `docs/invariants/auto-upgrade.md` only if its observation contradicts it, after 135A lands. Its prerequisite Track 132F shipped in PR #327 (v1.12.50)._

#### Track 135B: Observe one automatic update on a real machine (Track 124B's post-merge check)
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

_Check at regeneration, 2026-11-15 at `f08f10b`: unchanged on the Linux home machine (1.12.47; `checked_at` and `attempted_at` 2026-11-15T02:47:52Z; no auto-update.log entry after the 2026-11-11 upgrade). main is now 1.12.50, so that machine is three releases behind, and its first daily check after 2026-11-16T02:47Z is the next chance for 124B's code to update it. Relabeled from 132B by this regeneration (Group 132 has no other unfinished work); obligation unchanged._

#### Track 135C: Warn on stderr for every pull apply failure, and recheck the file before the inline conflict prompt overwrites it
_4 tasks . ~260 LOC . medium risk . 4 files_
**Outcome:** When a pulled file cannot be applied (an unreadable blob, a decrypt failure, a rejected path or a content mismatch), `mm pull` and `mm autopull` print the cause on stderr in the shared warning form, quiet mode included, and an edit made to a file while `mm pull --conflicts prompt` waits for an answer is never replaced.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8 (data-at-risk warnings are never quiet-gated) and O1; the developer's approval of /full-review findings C1 and C2 (2026-11-15 triage).
**Done when:** the acceptance list below passes: all five pre-apply failures warn on stderr in quiet and interactive modes, the inline prompt rechecks the file before keep-remote and merge write it, and the two placed test gaps have tests.
_touches: src/mind_meld/pullflow.py, tests/test_pullflow.py, tests/test_pull_helpers.py, docs/invariants/sync.md_
_read-first: docs/invariants/README.md, docs/invariants/sync.md ("Every apply failure uses `_warn_apply_failure`" and the never-quiet-gate rule), src/mind_meld/pullflow.py (`_download_and_apply`, `_warn_apply_failure`, `_ApplyReporter`, `_apply_fast_forward`, the inline prompt's `keep-remote` and merge branches)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: two /full-review findings at `3289de2`, approved by the developer in the 2026-11-15 triage and re-verified at `f08f10b`. C1 (necessary): five branches in `_download_and_apply` (a bad blob key, a blob read `MindMeldError` that prints "blob missing" only under `--verbose` on stdout, a decrypt failure other than `NewerFormatError`, a rejected path escape and a content-check mismatch) append to `outcomes["failed"]` without `_warn_apply_failure` and print their cause only `if not quiet`, so autopull shows "N failed: <paths>" with no cause. C2: the inline prompt's keep-remote and merge branches write the canonical file after a prompt that can wait indefinitely, without the `before` lstat recheck `_apply_fast_forward` makes, so an edit made meanwhile is lost with no copy; the same class in `mm resolve` stays in Future. Neither was reproduced; both are placed on the developer's approval, C1 because it breaks the never-quiet-gate rule, C2 because it loses user data. Two Future test gaps whose trigger this card meets ride along; "interrupted pull skips the memory-index tidy" also meets it but stays deferred at the session-weight limit, and "verbose memory-index tidy diagnostics" is placed on Track 142A._

- **Every apply failure warns** -- Begin the reporter before the five checks and route each through `reporter.failed(...)`, deleting the per-branch `if not quiet` prints rather than adding another reporting layer; a blob read error names what failed, not "blob missing"; the verbose summary's "Fix the warning above" then has a warning above it. _src/mind_meld/pullflow.py, tests/test_pullflow.py, tests/test_pull_helpers.py, docs/invariants/sync.md, ~90 lines._ (M)
- **Recheck before the inline prompt overwrites** -- Keep-remote goes through `_apply_fast_forward` (which rechecks `before`, publishes and restores mtime) and keeps both when it returns None; merge gets the same recheck before it writes, removing the duplicated write branch. _src/mind_meld/pullflow.py, tests/test_pullflow.py, ~90 lines._ (M)
- **Prefetch publication test** -- Close the Future test gap "canonical publication after pull prefetch": replace the canonical manifest after prefetch and before the cursor commit with different content at the same timestamp, and assert the pull uses the identity it read. _tests/test_pullflow.py, ~40 lines._ (S)
- **Late tidy flush test** -- Close the Future test gap "late memory-index tidy flush failure": a parent-flush failure after the real replacement keeps the visible bytes, prints one notice and leaves the counts unchanged. _tests/test_pullflow.py, ~40 lines._ (S)

**Acceptance:**

1. Each of the five pre-apply failures prints one `mm: warning:` line with its cause on stderr under `mm autopull` and an interactive `mm pull`; a test pins each.
2. No apply-failure cause prints on stdout or only under `--verbose`.
3. An edit made to the canonical file while the inline prompt waits survives keep-remote and merge (the pull keeps both); a test pins each.
4. Both placed test gaps have a test.
5. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

---

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134_

Groups 137 to 147 carry the 2026-11-15 full review's findings the developer approved (its sets C1 to C7). Most of them collide only through shared invariant docs, core and the CLI, so they run one Group after another; an `_blocked-by:` edge on each keeps the necessary output fixes ahead of the dead-code deletions.

### Group 136: Truthful conflict reaper and conflict-command lines

_Depends on: Group 135_

**This group delivers:** `mm gc --conflicts` stops calling kept conflict copies a failure, the conflict commands' remaining lines use the shared style, and `mm resolve`'s same-file refusal never advises deleting a copy it could not read when the original is a link to it.

#### Track 136A: Make gc's conflict reaper and the conflict commands' last two lines tell the truth in the shared style
_5 tasks . ~230 LOC . low risk . 8 files_
**Outcome:** `mm gc --conflicts` with an unresolved conflict copy reports the copy it kept as kept, not as a permissions or locks failure, and still prints its `Gc:` outcome; an unmatched `mm resolve PATH` ends with a `Run:` fix, the `mm conflicts` listing ends in the shared forms, and when `mm resolve` cannot read a same-file copy it picks the repair from the original, so it never advises deleting a file's only name because of that read.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.1 and O8.6 (the developer, 2026-11-10: "they all need to look like they come from the same tool"); the developer's request in the 2026-11-15 brief to fold Track 132D's D5 into an upcoming Track ("can d5 be folded into an upcoming track?").
**Done when:** the acceptance list below passes: kept copies are not remediation, wrong fix commands are gone, both conflict-command lines use the shared helpers, and an unreadable copy's repair follows the original, with tests pinning the new text.
_touches: src/mind_meld/retention.py, src/mind_meld/gcflow.py, src/mind_meld/cli.py, src/mind_meld/resolveflow.py, tests/test_conflict_copy.py, tests/test_gc_events.py, tests/test_integration.py, docs/invariants/conflicts.md_
_blocked-by: Track 132D, Track 135A_
_read-first: docs/invariants/README.md, docs/invariants/conflicts.md (the same-file refusal paragraph), src/mind_meld/retention.py (`ReapOutcome.needs_remediation`, `_render_reap_outcome`), src/mind_meld/gcflow.py, src/mind_meld/cli.py (`conflicts`, `resolve`), src/mind_meld/resolveflow.py (`_links_through`, the same-file refusal in `_resolve_interactive_loop`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: three inbox items filed by the reviews of Tracks 133A (`d9180e7`) and 134A (`411ffde`), verified at `888ada2`. `ReapOutcome.needs_remediation` returns `bool(self.failed or self.skipped or self.repair_failed)`, and the reaper counts deliberate keeps (a live conflict older than 30 days, a `v0-` sidecar, an unreadable age) as `skipped`, so `gcflow` (`elif any(o.needs_remediation ...)`) prints "Fix permissions or locks" with `Run: mm gc -v` (no `--conflicts`) in place of the `Gc:` line; any machine with one old unresolved conflict hits it on the normal path. `cli.py` still ends an unmatched `mm resolve PATH` with `core._error(f"No conflict file matching: {path}")` and no fix, and the `mm conflicts` listing with the markup hint "...or delete files manually with [bold]rm[/bold]." Ordered after Track 135A (shared `cli.py` and `tests/test_integration.py`); its prerequisite Track 132D shipped in PR #324 (v1.12.48). Re-verified at `c0158f0`: `needs_remediation`, gc's "Fix permissions or locks" warning, the unmatched-path error and the markup hint are unchanged by 132D. Also folds in Track 132D's recorded gc test gap (below)._

_Fold, 2026-11-15 brief: the Future entry "mm resolve's same-file refusal advises deleting a copy whose own lstat fails" (Track 132D's D5, backlogged as option B). Verified at `f08f10b`: the refusal calls `_links_through(canonical, cpath)`, whose first step `os.lstat(target)` reads the copy; if that read fails, the outer `except OSError` returns False and the refusal prints `delete <copy>`, which deletes the file's only name when the copy is a single-link regular file and the original a symlink to it. mm itself deletes nothing. The three Future test gaps on `resolveflow.py` and `tests/test_conflict_copy.py` (no-original promote whose unlink fails, keep-both promote warning after a failed print, the same-file refusal's hard-link and lstat-pinning cases) meet their trigger here but stay deferred: this card is at the five-task and session-weight limits._

- **Kept copies are not failures** -- Count deliberate keeps separately from skips the owner must fix; only real failures warn, the warning's fix names `mm gc --conflicts -v`, and the `Gc:` outcome line still prints. _src/mind_meld/retention.py, src/mind_meld/gcflow.py, tests/test_conflict_copy.py, tests/test_gc_events.py, ~90 lines._ (M)
- **Conflict-command lines in the shared style** -- An unmatched `mm resolve PATH` ends with `Run: mm conflicts`; the `mm conflicts` listing ends with a shared `Run: mm resolve` line and a `mm: notice:` for pre-v0.9.2 copies; update the test that pins today's unmatched-path stderr. _src/mind_meld/cli.py, tests/test_integration.py, ~60 lines._ (S)
- **Empty-PATH witness** -- Close the recorded test gap from Track 134A: spy on `resolveflow._find_conflict_files` in `test_resolve_empty_path_argument_errors_instead_of_walking` and assert it found the planted sidecar. _tests/test_integration.py, ~20 lines._ (S)
- **gc same-file shapes** -- Close the recorded test gap from Track 132D: parametrize `TestGcOldConflictFiles::test_canonical_symlinked_to_its_sidecar_is_never_reaped` over a canonical symlinked to its sidecar, a sidecar symlinked to its canonical and a hard-linked pair, asserting nothing is reaped. _tests/test_conflict_copy.py, ~20 lines._ (S)
- **An unreadable copy's repair follows the original** -- When the copy's own `os.lstat` fails, decide the repair from the original alone: an original that is a symlink gets `move <copy> over <original>`, otherwise `delete <copy>`; state the rule in conflicts.md's same-file paragraph and pin it with a test that injects the copy's `lstat` failure while the original is a symlink to a single-link copy. _src/mind_meld/resolveflow.py, tests/test_conflict_copy.py, docs/invariants/conflicts.md, ~40 lines._ (S)

**Acceptance:**

1. `mm gc --conflicts` with one unresolved conflict older than 30 days prints no permissions-or-locks warning and still prints its `Gc:` outcome; a test pins it.
2. A real reap failure still warns, and its fix line names `--conflicts`; a test pins it.
3. An unmatched `mm resolve PATH` ends with `Run: mm conflicts` and exits 1; the `mm conflicts` listing has no Rich-markup prose hint; tests pin both.
4. The empty-PATH resolve test fails if discovery finds no sidecar.
5. `mm gc --conflicts` keeps both names for all three same-file shapes; a parametrized test pins each.
6. With the copy's `os.lstat` failing and the original a symlink to it, the refusal names `move <copy> over <original>`, not `delete <copy>`; a test pins it, and docs/invariants/conflicts.md states the rule.
7. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 137: Shared-style warnings for migration, gc, init and sources

_Depends on: Group 135, Group 136_

**This group delivers:** the migrate-config reminder, gc's warnings and refusals, and the backfill, init and source lines all print in the shared form with their fix last, including the two output findings the review marked necessary.

#### Track 137A: Bring the migration, gc, backfill, init and source warnings to the shared style
_5 tasks . ~250 LOC . low risk . 13 files_
**Outcome:** The `mm migrate-config` reminder that piped `mm push`, `mm pull`, `mm recapture` and every `--dry-run` print, gc's malformed-blob warning (which can appear inside an attended push), gc's newer-format, missing-manifest and completeness refusals, and the backfill, init cleanup and `enable-source --force` warnings all use the shared `mm: warning:` or `mm: error:` form on stderr with the fix last; source outcome lines name their verb once.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.1 and O8.6; the developer's approval of /full-review findings C3 (2026-11-15 triage), including its two necessary findings.
**Done when:** the acceptance list below passes: every line named in the tasks uses the shared helpers, tests pin the new text, and the placed test gap has a test.
_touches: src/mind_meld/core.py, src/mind_meld/gcflow.py, src/mind_meld/initflow.py, src/mind_meld/sourceflow.py, tests/test_output_style.py, tests/test_config.py, tests/test_gstack_extend_analytics.py, tests/test_source_toggle.py, tests/test_sourceflow.py, tests/test_additive_sync.py, tests/test_gc_events.py, tests/test_integration.py, docs/invariants/sync.md_
_blocked-by: Track 135C, Track 136A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md (gc wording), src/mind_meld/core.py (`_warning`, `_error`, `_maybe_prompt_migration`, `_newer_format_message`), src/mind_meld/gcflow.py_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C3 at `3289de2`, approved in the 2026-11-15 triage (its install-skills and recover items stay uncarded, TODOS.md drain of 2026-11-10); verified at `f08f10b`. `core._maybe_prompt_migration`'s non-TTY and dry-run branch prints `[yellow]warning:[/yellow] ... Run [bold]mm migrate-config[/bold] to update.` (necessary); `gcflow` prints `  [yellow]warning:[/yellow] N blob(s) at unexpected path depth` on stdout, and `_do_gc` also runs as attended push's post-publication GC (necessary); gc hand-builds its newer-format refusal ("Upgrade with: ...") and puts "then retry 'mm gc'" and "then retry GC" mid-sentence; `core.py`'s backfill warning and `sourceflow.py`'s unknown-source warning are raw `print`s, and `initflow.py`'s cleanup warning prints exception text through Rich markup; source outcomes read "Disable: Disabled source ...". The Future test gap "unrecognized migration directory shapes" meets its trigger here and rides along; "Shared diagnostic stream failures" and "Delayed measured progress retains counters" also meet it but stay deferred at the session-weight limit ("Shared diagnostic stream failures" is placed on Track 140A)._

- **Migration reminder** -- The non-TTY and dry-run branch of `_maybe_prompt_migration` uses `_warning(..., fix="mm migrate-config")`; update the tests that pin the old text. _src/mind_meld/core.py, tests/test_output_style.py, tests/test_config.py, tests/test_gstack_extend_analytics.py, tests/test_source_toggle.py, tests/test_integration.py, ~40 lines._ (S)
- **gc's warnings and refusals** -- The malformed-blob summary goes to stderr in the shared form, and inside attended push follows O8.1 (printed only when the owner must act, otherwise recorded for `mm status` and `mm diag`); the newer-format refusal uses `core._newer_format_message` plus gc's own "Leave it in place"; `_skip_missing` and `_refuse_unless_complete` pass `fix=` and reuse `refusal_suffix`, keeping the wording sync.md pins. _src/mind_meld/gcflow.py, tests/test_additive_sync.py, tests/test_gc_events.py, tests/test_integration.py, docs/invariants/sync.md, ~90 lines._ (M)
- **Three raw warnings** -- The backfill warning goes through `core._warning` (which sanitizes and pauses progress), init's cleanup-failure warning stops passing exception text through Rich markup, and `enable-source --force`'s unknown-source warning uses `core._warning`; add tests for the two that have none. _src/mind_meld/core.py, src/mind_meld/initflow.py, src/mind_meld/sourceflow.py, tests/test_output_style.py, ~40 lines._ (S)
- **Source outcome verbs** -- "Disable: source 'x' disabled on this device." and the same for enable and already-disabled; update the tests that pin these strings. _src/mind_meld/sourceflow.py, tests/test_sourceflow.py, tests/test_source_toggle.py, tests/test_integration.py, ~40 lines._ (S)
- **Migration directory shapes test** -- Close the Future test gap "unrecognized migration directory shapes": the retired-directory migration guard with an absent, scalar or mixed-type `include_dirs`. _tests/test_gstack_extend_analytics.py, ~30 lines._ (S)

**Acceptance:**

1. Piped `mm push` and `mm push --dry-run` with missing excludes print one `mm: warning:` line ending in `Run: mm migrate-config` on stderr; a test pins it.
2. gc's malformed-blob summary never prints on stdout, and an attended push prints it only under O8.1's rule; tests pin both.
3. gc's newer-format, missing-manifest and completeness lines end with their fix as the last line; tests pin them.
4. The backfill, init cleanup and unknown-source warnings use the shared form, and exception text is not interpreted as markup; tests pin each.
5. Source outcome lines name the verb once; the placed test gap has a test.
6. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 138: Shared-style update and log lines ∥ Antigravity reader guard

_Depends on: Group 137_

**This group delivers:** `mm update`, `mm log` and the autorun breadcrumbs speak the shared style with one phrase for maintenance failures, and the Antigravity reader stops rewriting its cache on every failed pass while the readers drop exceptions and fields nothing raises or reads.

#### Track 138A: Bring mm update, mm log and the autorun breadcrumbs to the shared style
_5 tasks . ~220 LOC . low risk . 13 files_
**Outcome:** `mm update`'s errors end with the install or reinstall command as their own `Run:` line and its successes are `Update:` outcome lines; every automatic-update notice comes from the shared builder; `mm log`'s too-new database error puts `Run: mm update` on its own line; and autopull, autopush and `mm status` call maintenance failures by one name.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.6; the developer's approval of /full-review findings C3 (2026-11-15 triage).
**Done when:** the acceptance list below passes: the named lines use the shared helpers, one phrase serves both breadcrumbs, and the placed test gap has a test.
_touches: src/mind_meld/upgrade.py, src/mind_meld/cli.py, src/mind_meld/pullhistory.py, src/mind_meld/statusflow.py, tests/test_self_update.py, tests/test_upgrade.py, tests/test_lockfile.py, tests/test_integration.py, tests/test_pullhistory.py, tests/test_attemptlog.py, tests/test_silent_failure_contract.py, tests/test_syncdiag.py, tests/test_pull_result.py_
_blocked-by: Track 137A_
_read-first: docs/invariants/README.md, docs/invariants/auto-upgrade.md, src/mind_meld/outputstyle.py, src/mind_meld/upgrade.py (`update_or_nudge`), src/mind_meld/cli.py (`_sync_update`, `update`, the autopull and autopush breadcrumbs), src/mind_meld/pullhistory.py (the `StateDBTooNew` line)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C3 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. `update_or_nudge` hand-builds "mm: notice: updating mm ..." and "mm: notice: updated mm ..." beside four `_notice_line` notices; `cli._sync_update` appends the literal "mm: notice: automatic update did not complete. Run: mm update"; `mm update`'s errors embed their remedy mid-text and its interrupt path is a raw `print`; `pullhistory` returns `_error_line(body) + " " + _fix_line('mm update')` on one line; the autopull breadcrumb says "N follow-up failure(s)" and the autopush one "N post-publication maintenance failure(s)". `cli.py` and `statusflow.py` change under Track 135A; re-read them after it lands. The Future test gap "git executable disappears after discovery" meets its trigger here and rides along; the other `upgrade.py` gaps are placed on Track 146A or stay deferred at the limit._

- **Update notices from the shared builder** -- Both hand-built notices in `update_or_nudge` and `_sync_update`'s literal use `outputstyle._notice_line(..., fix=...)`. _src/mind_meld/upgrade.py, src/mind_meld/cli.py, tests/test_self_update.py, tests/test_lockfile.py, tests/test_upgrade.py, ~40 lines._ (S)
- **`mm update`'s errors, successes and interrupt** -- Errors pass `fix=` (the install or reinstall command) so `Run:` comes last, successes are `Update:` outcome lines, and the interrupt path uses the shared helper. _src/mind_meld/cli.py, tests/test_self_update.py, tests/test_integration.py, ~90 lines._ (M)
- **`mm log`'s too-new error** -- Return the error and its `Run: mm update` as two lines, or record in the routing README why `mm log` needs one string. _src/mind_meld/pullhistory.py, tests/test_pullhistory.py, ~20 lines._ (S)
- **One name for maintenance failures** -- After checking that no reader matches the breadcrumb text, both breadcrumbs and `mm status` use one phrase. _src/mind_meld/cli.py, src/mind_meld/statusflow.py, tests/test_attemptlog.py, tests/test_silent_failure_contract.py, tests/test_syncdiag.py, tests/test_pull_result.py, tests/test_integration.py, ~30 lines._ (S)
- **Git disappears test** -- Close the Future test gap "git executable disappears after discovery": the release check classifies a `FileNotFoundError` after `find_git` succeeds. _tests/test_upgrade.py, ~30 lines._ (S)

**Acceptance:**

1. Every `mm: notice:` line `update_or_nudge` and `_sync_update` print comes from `outputstyle._notice_line`; tests pin the text.
2. Each `mm update` error ends with its fix on its own `Run:` line, and success prints an `Update:` outcome line; tests pin both.
3. `mm log` with a too-new history database prints `Run: mm update` on its own last line, or the exception is recorded.
4. Autopull and autopush breadcrumbs and `mm status` name maintenance failures with one phrase; tests pin it.
5. The placed test gap has a test; `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

#### Track 138B: Give the Antigravity reader the shared failed-pass guard, and drop dead reader exceptions and fields
_5 tasks . ~220 LOC . low risk . 11 files_
**Outcome:** A failed Antigravity read no longer rewrites its usage cache on every autopush, all four host readers catch the same exceptions, the Grok cache stops persisting a field nothing reads, and the unused lock helper, exception class and `_utc_day` leave the code.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md's host table (Antigravity usage) and AGENTS.md ("Name a reader for every persisted field"); the developer's approval of /full-review findings C7 and C4 (2026-11-15 triage).
**Done when:** the acceptance list below passes: Antigravity skips the failed-pass write like Codex, Grok and Muse, no reader catches an exception that cannot be raised, and the placed test gap has a test.
_touches: src/mind_meld/host_usage.py, src/mind_meld/hosts/grok.py, src/mind_meld/lockedjson.py, tests/test_agy_usage.py, tests/test_grok_usage.py, tests/test_lockedjson.py, tests/test_fsutil.py, tests/test_write_guard_inventory.py, docs/invariants/events-retro.md, docs/invariants/sync.md, docs/invariants/README.md_
_blocked-by: Track 137A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md (the host adapter contract, clause 4), src/mind_meld/host_usage.py (`read_agy_usage`, `_skip_failed_cache_write`, `_NoCacheCommit`), src/mind_meld/hosts/grok.py, src/mind_meld/lockedjson.py_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C7 and C4 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. `read_agy_usage` is the only reader without the `_skip_failed_cache_write` / `_NoCacheCommit` guard (Codex in `hosts/codex.py`, Grok in `hosts/grok.py` and Muse in `host_usage.py` use it), and clause 4 lists only those three; Antigravity (`host_usage.py`) and Grok (`hosts/grok.py`) catch `(OSError, StorageError, InvalidJsonCache)` though `locked_json_rmw` degrades corrupt JSON instead of raising; Grok writes `census_host_version` and nothing reads it, while Antigravity gates reuse on the same field; `locked_json_durable_rmw` and `host_usage._utc_day` have no production caller. The Future test gap "retained SQLite diagnostic code filtering" meets its trigger here and rides along; "Antigravity cross-type turn deduplication" and the two Grok usage-reader gaps also meet it but stay deferred at the five-task limit._

- **Antigravity skips the failed-pass write** -- `read_agy_usage` uses the shared guard, after checking whether `last_error_detail` and `error_db_count` count as learned; clause 4 names all four readers. _src/mind_meld/host_usage.py, tests/test_agy_usage.py, docs/invariants/events-retro.md, ~90 lines._ (M)
- **Dead exception types and lock helper** -- Antigravity and Grok catch only what can be raised; delete `InvalidJsonCache` and `locked_json_durable_rmw` with their tests and routing rows. _src/mind_meld/host_usage.py, src/mind_meld/hosts/grok.py, src/mind_meld/lockedjson.py, tests/test_lockedjson.py, tests/test_fsutil.py, tests/test_write_guard_inventory.py, docs/invariants/sync.md, docs/invariants/README.md, ~60 lines._ (S)
- **One census-version policy** -- Grok stops writing `census_host_version` (its `_GROK_CACHE_FORMAT` already invalidates) or gates reuse on it as Antigravity does; events-retro.md states the one policy for turn readers. _src/mind_meld/hosts/grok.py, tests/test_grok_usage.py, docs/invariants/events-retro.md, ~20 lines._ (S)
- **Uncalled `_utc_day`** -- Delete it and its write-guard inventory row. _src/mind_meld/host_usage.py, tests/test_write_guard_inventory.py, ~20 lines (del)._ (S)
- **SQLite code filter test** -- Close the Future test gap "retained SQLite diagnostic code filtering": allowed, malformed and overlong SQLite error codes in Antigravity's causes. _tests/test_agy_usage.py, ~40 lines._ (S)

**Acceptance:**

1. A failed Antigravity pass leaves its cache file unchanged; a test pins it, and clause 4 lists all four readers.
2. No host reader catches `InvalidJsonCache` or `StorageError`; `locked_json_durable_rmw`, `InvalidJsonCache` and `_utc_day` are gone with their tests and doc rows.
3. No reader cache persists a field nothing reads.
4. The placed test gap has a test; `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 139: One host set, diagnostic sink and reader list

_Depends on: Group 138_

**This group delivers:** adding a host means editing one opt-in set, one diagnostic sink and one reader list instead of finding every copy.

#### Track 139A: Keep one opt-in host set, one diagnostic sink and one host reader list
_4 tasks . ~230 LOC . low risk . 16 files_
**Outcome:** The opt-in host sources are named once, host diagnostics flow through one sink with one `degraded` default, and the host reader order lives in one list that the contract tests read.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md's host table (adding or removing a host); the developer's approval of /full-review findings C7 and C4 (2026-11-15 triage).
**Done when:** the acceptance list below passes: one constant, one sink and one list, with the contract tests pointed at the list production iterates.
_touches: src/mind_meld/config.py, src/mind_meld/sourceflow.py, src/mind_meld/host_registry.py, src/mind_meld/statusflow.py, src/mind_meld/host_usage.py, src/mind_meld/token_usage.py, src/mind_meld/events_tail.py, src/mind_meld/events.py, tests/test_agy_usage.py, tests/test_host_usage_snapshot.py, tests/test_token_usage.py, tests/test_host_adapter_contract.py, tests/test_grok_usage.py, tests/test_diag.py, docs/invariants/README.md, docs/invariants/events-retro.md_
_blocked-by: Track 138A, Track 138B_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, src/mind_meld/config.py (the opt-in source test), src/mind_meld/host_registry.py, src/mind_meld/token_usage.py (`_DIAGNOSTIC`, `diagnostic_scope`), src/mind_meld/events.py (`ACTIVE_HOST_READERS`, `HOST_USAGE_TOKEN_SOURCES`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C7 and C4 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. The opt-in set is spelled in `config.py` (`type not in ("grok", "muse") and name != "agy"`), twice in `sourceflow.py`, in `host_registry.py` and in `statusflow.py`; the `_DIAGNOSTIC` ContextVar sink is copied in `host_usage.py`, `token_usage.py` and `events_tail.py`; `ACTIVE_HOST_READERS` matches `HOST_USAGE_TOKEN_SOURCES`, which events_tail actually iterates, and only tests read it; `_default_host_readers`' docstring still names `host_usage.read_codex_usage`, which moved to `hosts/codex.py`. `events_tail.py` and `statusflow.py` change under Track 135A; re-read them after it lands. Future test gaps on `events_tail.py`, `host_usage.py` and `statusflow.py` meet their triggers here but stay deferred at the session-weight limit._

- **One opt-in host set** -- One constant replaces the four spellings, or `codex_usage_diag` takes the same `count_logs` keyword so the registry's special case goes. _src/mind_meld/config.py, src/mind_meld/sourceflow.py, src/mind_meld/host_registry.py, src/mind_meld/statusflow.py, tests/test_diag.py, ~40 lines._ (S)
- **One diagnostic sink** -- Keep the sink in `token_usage`, which `host_usage` and `events_tail` already import, delete the two copies and settle the one `degraded` default. _src/mind_meld/token_usage.py, src/mind_meld/host_usage.py, src/mind_meld/events_tail.py, tests/test_agy_usage.py, tests/test_host_usage_snapshot.py, tests/test_token_usage.py, ~90 lines._ (M)
- **One reader list** -- Delete `ACTIVE_HOST_READERS` and point the contract tests at `HOST_USAGE_TOKEN_SOURCES`, fixing its doc rows. _src/mind_meld/events.py, tests/test_host_adapter_contract.py, tests/test_grok_usage.py, tests/test_diag.py, tests/test_host_usage_snapshot.py, docs/invariants/README.md, docs/invariants/events-retro.md, ~90 lines._ (M)
- **Codex reader docstring** -- `_default_host_readers`' docstring names `hosts/codex.py`. _src/mind_meld/events_tail.py, ~5 lines._ (S)

**Acceptance:**

1. The opt-in host set is defined once and every reader of it uses that definition.
2. One `_DIAGNOSTIC` sink exists; `host_usage` and `events_tail` use it; tests pin its default.
3. `ACTIVE_HOST_READERS` is gone and the contract tests read `HOST_USAGE_TOKEN_SOURCES`.
4. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 140: One outcome printer

_Depends on: Group 139_

**This group delivers:** every hand-run command prints its outcome through one helper with one policy for a failed write, and the output builders have one home.

#### Track 140A: Print outcome lines through one helper, call the output builders directly, and record the leaf convention
_4 tasks . ~250 LOC . low risk . 14 files_
**Outcome:** Every outcome line goes through one `core` printer that, like `mm resolve` since Track 132D, does not turn a failed stdout write after the work is done into a crash; callers use `outputstyle`'s line builders directly; and the rule for leaf modules' literal `mm:` prefixes is written down.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.6 ("documented in the shared output helpers"); the developer's approval of /full-review findings C3 (2026-11-15 triage).
**Done when:** the acceptance list below passes: one printer, no `core` pass-throughs, a recorded leaf rule, and the placed test gap has a test.
_touches: src/mind_meld/core.py, src/mind_meld/outputstyle.py, src/mind_meld/resolveflow.py, src/mind_meld/cli.py, src/mind_meld/devicesflow.py, src/mind_meld/gcflow.py, src/mind_meld/initflow.py, src/mind_meld/pullflow.py, src/mind_meld/recaptureflow.py, src/mind_meld/sourceflow.py, src/mind_meld/statusflow.py, tests/test_output_style.py, tests/test_module_boundaries.py, docs/invariants/README.md_
_blocked-by: Track 139A_
_read-first: docs/invariants/README.md (the output rows), docs/invariants/conflicts.md (`_print_outcome`), src/mind_meld/outputstyle.py, src/mind_meld/core.py (`_error_line`, `_fix_line`, `_outcome_line`, `_fix_for`, `_warning`, `_notice`, `_print_error`), src/mind_meld/resolveflow.py (`_print_outcome`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C3 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. Only `resolveflow` wraps the outcome print (`_print_outcome`, tolerating `OSError`); the other sites call `console.print(core._outcome_line(...), markup=False, highlight=False, soft_wrap=True)` directly; `core._error_line`, `_fix_line`, `_outcome_line` and `_fix_for` only forward to `outputstyle`, called through `core.` from cli, devicesflow, gcflow, initflow, pullflow, recaptureflow, resolveflow, sourceflow and statusflow. About 100 literal `mm: notice:` / `mm: warning:` / `mm: error:` prefixes remain in leaf modules (events_tail has 20 to 25); the review asked to settle that convention once, and this card records the cheaper option it named instead of migrating them. Two choices here are this regeneration's, open to the developer's veto at plan review: best-effort outcome printing everywhere, and the recorded leaf convention. The Future test gap "Shared diagnostic stream failures" meets its trigger here and rides along; "No-argument help keeps framework behavior" also meets it but stays deferred at the session-weight limit._

- **One outcome printer** -- Promote `resolveflow._print_outcome` to `core` (best-effort on `OSError`) and replace the repeated `console.print(core._outcome_line(...), ...)` calls with it. _src/mind_meld/core.py, src/mind_meld/resolveflow.py, src/mind_meld/cli.py, src/mind_meld/devicesflow.py, src/mind_meld/gcflow.py, src/mind_meld/initflow.py, src/mind_meld/pullflow.py, src/mind_meld/sourceflow.py, tests/test_output_style.py, ~100 lines._ (M)
- **Builders called directly** -- Callers use `outputstyle._error_line`, `_fix_line`, `_outcome_line` and `_fix_for` module-qualified; delete the four `core` pass-throughs and keep `_warning`, `_notice` and `_print_error`, which do I/O. _src/mind_meld/core.py, src/mind_meld/cli.py, src/mind_meld/devicesflow.py, src/mind_meld/gcflow.py, src/mind_meld/initflow.py, src/mind_meld/pullflow.py, src/mind_meld/recaptureflow.py, src/mind_meld/resolveflow.py, src/mind_meld/sourceflow.py, src/mind_meld/statusflow.py, tests/test_module_boundaries.py, ~100 lines._ (M)
- **Leaf convention** -- Record in the routing README's output rows that a leaf module may keep a literal `mm: notice:`, `mm: warning:` or `mm: error:` prefix that matches the shared form, and that new leaf code uses the `outputstyle` builders; migrate none of the existing sites. _docs/invariants/README.md, ~10 lines._ (S)
- **Stream failure test** -- Close the Future test gap "Shared diagnostic stream failures": a warning and a notice whose stream fails during write or flush. _tests/test_output_style.py, ~30 lines._ (S)

**Acceptance:**

1. No `console.print(core._outcome_line(` call remains; one `core` printer prints every outcome line, and a test pins that an `OSError` from it does not change the command's result.
2. `core` defines no `_error_line`, `_fix_line`, `_outcome_line` or `_fix_for`; `tests/test_module_boundaries.py` passes.
3. The routing README states the leaf prefix rule.
4. The placed test gap has a test; `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 141: Dead events, devices and storage helpers

_Depends on: Group 140_

**This group delivers:** events, devices and storage code without the names no production code calls.

#### Track 141A: Delete dead events, devices and storage helpers
_4 tasks . ~220 LOC . low risk . 18 files_
**Outcome:** The events module loses its unread constant, orphaned docstring, unreferenced helper, test-only cursor wrapper and private timestamp alias; device listing has one path; and `is_foreign_key` is gone, with the docs that named them corrected.
**Supports:** MVP-1 on Syncthing; AGENTS.md ("Name a reader for every persisted field") and the project's single-user rule (no compatibility code); the developer's approval of /full-review findings C4, C5 and C6 (2026-11-15 triage).
**Done when:** the acceptance list below passes: each named symbol is deleted or folded, its tests point at the production entry point, and no doc names a deleted symbol.
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/devices.py, src/mind_meld/core.py, src/mind_meld/devicesflow.py, src/mind_meld/storage/local.py, tests/test_events.py, tests/test_reader_publication.py, tests/test_init_events_backfill.py, tests/test_track_30a.py, tests/test_write_guard_inventory.py, tests/test_devices.py, tests/test_module_boundaries.py, tests/test_pull_helpers.py, tests/test_safe_str.py, docs/invariants/events-retro.md, docs/invariants/sync.md, docs/invariants/README.md_
_blocked-by: Track 140A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, src/mind_meld/events.py, src/mind_meld/devices.py (`list_devices`, `list_devices_with_drops`, `_list_devices_impl`, `inventory_devices`), src/mind_meld/core.py (`_list_devices_warn`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C4, C5 and C6 at `3289de2`, approved in the 2026-11-15 triage; reference counts verified at `f08f10b`. `DISCOVERY_ADVANCE` and `_read_gitconfig_user_email` have no reference outside their definitions; `EventScan.cached_hash` and `last_push_ts` are read only by tests; `_parse_aware_ts = parse_aware_ts` is used at about 30 sites; `list_devices_with_drops` is read only by `tests/test_devices.py` and stale `tests/test_module_boundaries.py` entries; devices docstrings name a nonexistent `cli._list_devices_with_warnings`; `storage/local.is_foreign_key` has no caller; `devicesflow` imports `ConfigError` through `mind_meld.config`. `events_tail.py` changes under Track 135A; re-read it after it lands._

- **Dead events names** -- Delete `DISCOVERY_ADVANCE`, `_read_gitconfig_user_email` and `EventScan.cached_hash` (after checking that `scan.hashes` still has a production reader), and move the orphaned `MAX_DISCOVERY_REJECT_SAMPLE` docstring under its constant. _src/mind_meld/events.py, tests/test_events.py, tests/test_reader_publication.py, ~40 lines (del)._ (S)
- **One timestamp parser name, no cursor wrapper** -- Replace the `_parse_aware_ts` alias with `parse_aware_ts` and delete the alias; tests call `resolve_push_cursor(...).since`, and `last_push_ts` goes with its mentions in events_tail comments and events-retro.md. _src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/test_events.py, tests/test_init_events_backfill.py, tests/test_track_30a.py, tests/test_write_guard_inventory.py, docs/invariants/events-retro.md, ~90 lines._ (M)
- **One device-listing path** -- Delete `list_devices_with_drops` with its test and boundary entries, fold `_list_devices_impl`'s `on_drop` loop into `core._list_devices_warn`, correct the docstrings, and import `ConfigError` from `mind_meld.errors` in devicesflow. _src/mind_meld/devices.py, src/mind_meld/core.py, src/mind_meld/devicesflow.py, tests/test_devices.py, tests/test_module_boundaries.py, tests/test_pull_helpers.py, tests/test_safe_str.py, docs/invariants/sync.md, ~80 lines._ (M)
- **`is_foreign_key`** -- Delete it and its mentions in the routing README and sync.md. _src/mind_meld/storage/local.py, docs/invariants/README.md, docs/invariants/sync.md, ~10 lines (del)._ (S)

**Acceptance:**

1. None of `DISCOVERY_ADVANCE`, `_read_gitconfig_user_email`, `cached_hash`, `last_push_ts`, `_parse_aware_ts`, `list_devices_with_drops`, `_list_devices_impl` or `is_foreign_key` appears in `src/`, `tests/` or `docs/invariants/`.
2. Device listing warns on drops exactly as before; the existing device tests pass.
3. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 142: Dead pull helpers and one content-hash check

_Depends on: Group 141_

**This group delivers:** pull and pull-plan code without test-only wrappers and dead members, and one 64-hex content-hash check.

#### Track 142A: Delete dead pull and pull-plan helpers, and keep one content-hash check
_5 tasks . ~180 LOC . low risk . 14 files_
**Outcome:** Pull's test-only `_preflight_conflicts`, `PullResult.total_older`, `Ledger.held_newest_first` and `_ledger_verdict`'s unused parameter are gone, one predicate decides what a content hash is, and test_pullflow loses its unused helpers.
**Supports:** MVP-1 on Syncthing; the project's single-user rule (no compatibility code); the developer's approval of /full-review findings C4, C5 and C6 (2026-11-15 triage).
**Done when:** the acceptance list below passes: each named symbol is gone, its tests use the production path, one hash predicate remains, and the placed test gap has a test.
_touches: src/mind_meld/pullflow.py, src/mind_meld/pullplan.py, src/mind_meld/manifest.py, src/mind_meld/storage/keys.py, src/mind_meld/core.py, tests/test_pullflow.py, tests/test_pull_helpers.py, tests/test_docs_routing.py, tests/test_module_boundaries.py, tests/test_pull_result.py, tests/test_conflict_copy.py, tests/test_pullplan.py, docs/invariants/README.md, docs/invariants/sync.md_
_blocked-by: Track 141A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, src/mind_meld/pullflow.py (`_pull_core`, `_plan_pull`, `_fail_mode_refusals`, `_preflight_conflicts`, `_ledger_verdict`), src/mind_meld/pullplan.py (`valid_hash`), src/mind_meld/manifest.py (`_is_content_hash`), src/mind_meld/storage/keys.py_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C4, C5 and C6 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. `_pull_core` calls `_plan_pull` and `_fail_mode_refusals` directly while `_preflight_conflicts` serves tests and the routing README; `total_older` and `held_newest_first` are read only by tests; `_ledger_verdict` takes the peer from `reporter.peer_id`, not its `remote_device_id`; `pullplan.valid_hash` has the same body as `manifest._is_content_hash`, and `storage/keys.py` has a third 64-hex check; `_shape_remote` and `OLD_RELS` in `tests/test_pullflow.py` have no reference. The Future test gap "verbose memory-index tidy diagnostics" meets its trigger here and rides along; "interrupted pull skips the memory-index tidy" stays deferred at the limit._

- **`_preflight_conflicts`** -- Point its tests at `_plan_pull` plus `_fail_mode_refusals` and delete it with its routing rows. _src/mind_meld/pullflow.py, tests/test_pull_helpers.py, tests/test_docs_routing.py, tests/test_module_boundaries.py, docs/invariants/README.md, ~60 lines._ (M)
- **Dead pull members** -- Delete `PullResult.total_older` and `Ledger.held_newest_first`, and drop `_ledger_verdict`'s `remote_device_id`. _src/mind_meld/pullflow.py, src/mind_meld/pullplan.py, tests/test_pull_result.py, tests/test_pullplan.py, tests/test_conflict_copy.py, docs/invariants/sync.md, ~20 lines._ (S)
- **One content-hash predicate** -- Keep one 64-hex check in the lowest module `pullplan`, `manifest` and `storage/keys.py` can all import, and delete the other two. _src/mind_meld/storage/keys.py, src/mind_meld/manifest.py, src/mind_meld/pullplan.py, src/mind_meld/core.py, src/mind_meld/pullflow.py, tests/test_pullplan.py, ~30 lines._ (S)
- **Unused test helpers** -- Delete `_shape_remote` and `OLD_RELS`. _tests/test_pullflow.py, ~15 lines (del)._ (S)
- **Verbose tidy test** -- Close the Future test gap "verbose memory-index tidy diagnostics": assert the singular and plural wording and the quiet suppression. _tests/test_pullflow.py, ~40 lines._ (S)

**Acceptance:**

1. None of `_preflight_conflicts`, `total_older`, `held_newest_first` or `_shape_remote` remains; `_ledger_verdict` has no `remote_device_id`.
2. One content-hash predicate exists and every former caller uses it.
3. The placed test gap has a test; `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 143: Dead history and spool helpers, and state.db's unread tables

_Depends on: Group 142_

**This group delivers:** history and file-utility code without test-only readers and the Cursor-era spool, and a state.db without the three tables nothing reads.

#### Track 143A: Delete dead history and spool helpers, and drop state.db's unread tables
_4 tasks . ~300 LOC . medium risk . 13 files_
**Outcome:** `pullhistory.read_records` and `maintain_best_effort` leave the code, the rotatable JSONL spool unused since Cursor's removal is gone, and a state.db migration drops the `ledger`, `own_log` and `displaced` tables along with the uncalled `expire_displaced`.
**Supports:** MVP-1 on Syncthing; AGENTS.md ("Name a reader for every persisted field"); the developer's approval of /full-review findings C4 (2026-11-15 triage), with the note that dropping the tables needs a state.db migration.
**Done when:** the acceptance list below passes: the helpers are gone with their tests and doc rows, and a migrated database has no unread tables.
_touches: src/mind_meld/pullhistory.py, src/mind_meld/fsutil.py, src/mind_meld/statedb.py, tests/test_pullhistory.py, tests/test_pullflow.py, tests/test_integration.py, tests/test_upgrade.py, tests/test_fsutil.py, tests/test_write_guard_inventory.py, tests/test_statedb.py, docs/invariants/sync.md, docs/invariants/events-retro.md, docs/invariants/README.md_
_blocked-by: Track 142A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md (state.db tables, the writers' recipe), src/mind_meld/statedb.py (`SCHEMA_VERSION`, the migration list, `StateDBTooNew`), src/mind_meld/pullhistory.py, src/mind_meld/fsutil.py_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C4 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. `read_records` (with its legacy-JSONL branch) is read only by tests (58 references in four files); `maintain_best_effort` has no caller though sync.md presents it as the writers' recipe; `append_rotatable_jsonl` and `rotate_jsonl` have had no production caller since Track 115A removed Cursor; `statedb.py` (`SCHEMA_VERSION = 1`) creates `ledger`, `own_log` and `displaced`, which nothing reads, and `expire_displaced` / `DISPLACED_MAX_AGE` have no caller. A migrated database makes an older mm on the same machine refuse it with the existing `StateDBTooNew` error, which the single-user rule accepts._

- **`read_records` leaves src** -- Tests use `pullhistory.query` or a test helper; delete `read_records` and its legacy-JSONL branch. _src/mind_meld/pullhistory.py, tests/test_pullhistory.py, tests/test_pullflow.py, tests/test_integration.py, tests/test_upgrade.py, docs/invariants/sync.md, docs/invariants/events-retro.md, ~100 lines._ (M)
- **`maintain_best_effort`** -- Delete it and the sync.md recipe that presents it. _src/mind_meld/pullhistory.py, tests/test_pullhistory.py, tests/test_integration.py, docs/invariants/sync.md, ~30 lines (del)._ (S)
- **Rotatable JSONL spool** -- Delete `append_rotatable_jsonl` and `rotate_jsonl` with their tests and routing rows. _src/mind_meld/fsutil.py, tests/test_fsutil.py, tests/test_write_guard_inventory.py, docs/invariants/README.md, docs/invariants/sync.md, ~120 lines (del)._ (S)
- **Drop the three unread tables** -- After confirming which tables released binaries read, a schema migration drops `ledger`, `own_log` and `displaced`; delete `expire_displaced` and `DISPLACED_MAX_AGE`; sync.md describes the remaining tables. _src/mind_meld/statedb.py, tests/test_statedb.py, docs/invariants/sync.md, ~80 lines._ (M)

**Acceptance:**

1. `read_records`, `maintain_best_effort`, `append_rotatable_jsonl`, `rotate_jsonl`, `expire_displaced` and `DISPLACED_MAX_AGE` are gone from `src/`, `tests/` and `docs/invariants/`.
2. A version-1 database migrates without the three tables, keeps its history and cursors, and a test pins it; an older schema reader refuses the new version with `StateDBTooNew`.
3. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 144: Token-usage and skill-link leftovers

_Depends on: Group 143_

**This group delivers:** token-usage and skill-link modules without compatibility wrappers, aliases and an unrendered constant.

#### Track 144A: Delete token-usage and skill-link compatibility leftovers
_3 tasks . ~150 LOC . low risk . 8 files_
**Outcome:** `token_usage` loses `SUBSCRIPTION_CAVEAT` and its three test-only wrappers, and `skill_link` loses its four compatibility marker aliases and the test-only `skill_targets`, with tests pinned to the production entry points.
**Supports:** MVP-1 on Syncthing; the project's single-user rule (no compatibility code); the developer's approval of /full-review findings C4 (2026-11-15 triage).
**Done when:** the acceptance list below passes: the named symbols are gone and their tests use the production functions and `AGENT_ROWS`.
_touches: src/mind_meld/token_usage.py, src/mind_meld/skill_link.py, tests/conftest.py, tests/test_token_usage.py, tests/test_skill_link.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/events-retro.md_
_blocked-by: Track 143A_
_read-first: docs/invariants/README.md, src/mind_meld/token_usage.py (`walk_jsonl_segment`, `reap_cache_entries`, `__all__`), src/mind_meld/skill_link.py (`AGENT_ROWS`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C4 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. `SUBSCRIPTION_CAVEAT` appears only in `token_usage.py`'s `__all__`; `walk_jsonl_buckets`, `slice_window` and `gc_cache_entries` have no production caller; `skill_link` keeps four marker aliases "so nothing that imported the old module-level names breaks", read only by tests, and `skill_targets` claims to feed `install_skills_cmd`, which reads `AGENT_ROWS`._

- **`SUBSCRIPTION_CAVEAT`** -- Delete the constant and its `__all__` entry. _src/mind_meld/token_usage.py, ~5 lines (del)._ (S)
- **Token-usage wrappers** -- Move their tests to `walk_jsonl_segment` and `reap_cache_entries` and delete `walk_jsonl_buckets`, `slice_window` and `gc_cache_entries` with their doc rows. _src/mind_meld/token_usage.py, tests/conftest.py, tests/test_token_usage.py, docs/invariants/README.md, docs/invariants/events-retro.md, ~80 lines._ (M)
- **Skill-link aliases** -- Tests pin `AGENT_ROWS` values directly; delete the four aliases and `skill_targets` with its doc rows. _src/mind_meld/skill_link.py, tests/test_skill_link.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/events-retro.md, ~60 lines._ (M)

**Acceptance:**

1. None of the named symbols appears in `src/` or `docs/invariants/`; the tests exercise the production functions.
2. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 145: retro-fleet --no-save and retro leftovers

_Depends on: Group 144_

**This group delivers:** `retro-fleet` without its hidden no-op flag, and retro, seen-sources and test code without unused names.

#### Track 145A: Retire `retro-fleet --no-save`, and delete retro, seen-sources and test-helper leftovers
_5 tasks . ~180 LOC . low risk . 13 files_
**Outcome:** `mm retro-fleet --no-save` is gone (with a README Upgrading note), the aggregator loses an unread constant and a pass-through, `seen_sources.write()` can no longer bring back the lost-acknowledgment race, and the obsolete `.gitkeep` and four unused test helpers are deleted.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md ("One user, no compatibility work"); the developer's approval of /full-review findings C4, C5 and C6 (2026-11-15 triage).
**Done when:** the acceptance list below passes: the flag and named symbols are gone, tests use `acknowledge()`, and the placed test gap has a test.
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/seen_sources.py, src/mind_meld/cli.py, src/mind_meld/skills/.gitkeep, tests/test_retro_fleet_aggregator.py, tests/test_retro_fleet_cli.py, tests/test_seen_sources.py, tests/test_fsutil.py, tests/test_wheel.py, tests/test_integration.py, tests/test_manifest_fuzz.py, README.md, docs/invariants/events-retro.md_
_blocked-by: Track 144A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, src/mind_meld/cli.py (`retro_fleet`), src/mind_meld/skills/retro_fleet/aggregator.py (the `--no-save` branch, `_host_counter_ok`), src/mind_meld/seen_sources.py (`acknowledge`, `write`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C4, C5 and C6 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. `retro-fleet --no-save` is a hidden no-op kept since v0.12.39 for stale skill copies, which `mm install-skills` now manages; `HOST_SNAPSHOT_MIN_VERSION` is named only in events-retro.md; `_host_counter_ok` only calls `host_usage._is_valid_counter`; `seen_sources.write()` has no production caller and its atomic replace would defeat `acknowledge`'s in-place lock; `src/mind_meld/skills/.gitkeep` predates `__init__.py`; `claude_dir_b`, `wild_tombstone_value_strategy` and `_health_codes` have no reference. The Future test gap "Pricing confirmation model summary cap" meets its trigger here and rides along._

- **`--no-save`** -- Remove the hidden option from the CLI and the aggregator's matching branch, with a README Upgrading note. _src/mind_meld/cli.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_cli.py, tests/test_retro_fleet_aggregator.py, README.md, ~40 lines._ (S)
- **Aggregator leftovers** -- Delete `HOST_SNAPSHOT_MIN_VERSION` (events-retro.md keeps the version) and inline `_host_counter_ok`. _src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, ~20 lines._ (S)
- **`seen_sources.write()`** -- Delete it and point its tests at `acknowledge()`. _src/mind_meld/seen_sources.py, tests/test_seen_sources.py, tests/test_fsutil.py, ~40 lines._ (S)
- **Obsolete scaffolding** -- Delete `src/mind_meld/skills/.gitkeep` (correcting test_wheel's docstring) and the unused `claude_dir_b`, `wild_tombstone_value_strategy` and `_health_codes`. _src/mind_meld/skills/.gitkeep, tests/test_wheel.py, tests/test_integration.py, tests/test_manifest_fuzz.py, tests/test_retro_fleet_aggregator.py, ~20 lines (del)._ (S)
- **Pricing summary cap test** -- Close the Future test gap "Pricing confirmation model summary cap": a confirmation that reaches the eight-name display cap and its omitted-count suffix. _tests/test_retro_fleet_cli.py, ~30 lines._ (S)

**Acceptance:**

1. `mm retro-fleet --no-save` is rejected as an unknown option, and README's Upgrading section says so.
2. None of the named symbols or files remains; the seen-sources tests use `acknowledge()`.
3. The placed test gap has a test; `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 146: Dead parameters and Track-named tests

_Depends on: Group 145_

**This group delivers:** signatures without parameters nobody reads, and test files named by topic.

#### Track 146A: Drop dead parameters and the test-only `anchor_pending`, and name the Track-named test files by topic
_5 tasks . ~240 LOC . low risk . 24 files_
**Outcome:** `_recover_prior_manifest` no longer takes a `quiet` it must never honor, four more functions lose parameters they ignore, `anchor_pending` is gone and sync.md names what push actually calls, and the two test files named after a Track or review are named by topic.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8 (data-at-risk warnings are never quiet-gated) and the single-user rule; the developer's approval of /full-review findings C4, C5 and C6 (2026-11-15 triage).
**Done when:** the acceptance list below passes: the parameters are gone at definition and call sites, `anchor_pending` is gone with its doc mentions, the two files are renamed, and the two placed test gaps have tests.
_touches: src/mind_meld/core.py, src/mind_meld/cli.py, src/mind_meld/upgrade.py, src/mind_meld/statusflow.py, src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/conftest.py, tests/test_recovery.py, tests/test_recover.py, tests/test_silent_failure_contract.py, tests/test_upgrade.py, tests/test_diag.py, tests/test_pullflow.py, tests/test_events.py, tests/test_events_budget_scope.py, tests/test_init_events_backfill.py, tests/test_host_usage_snapshot.py, tests/test_retro_fleet_aggregator.py, tests/test_integration.py, tests/test_push_review91.py, tests/test_track_30a.py, tests/test_docs_routing.py, docs/invariants/sync.md, docs/invariants/README.md_
_blocked-by: Track 145A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md (the never-quiet-gate rule, the no-op check), src/mind_meld/core.py (`_recover_prior_manifest`, `anchor_pending`, `observe_anchors`), src/mind_meld/cli.py (`_quarantine_corrupt_manifest`)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C4, C5 and C6 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. `_recover_prior_manifest` accepts `quiet` and deliberately never reads it; `_quarantine_corrupt_manifest` ignores `backend`; `detect_self_version_transition` ignores `config` and `now`; `_peer_states` ignores `syncthing_state`; `walk_session_metadata` keeps an ignored `since` "for API stability" with one caller; push inlines `any(row["pending"] for row in observe_anchors(...))` while sync.md says `anchor_pending` joins the no-op check; `tests/test_track_30a.py` and `tests/test_push_review91.py` are named after a Track and a review. `statusflow.py` and `events_tail.py` change under Track 135A; re-read them after it lands. The Future test gaps "git cancellation cleanup" and "inherited git transfer and batch settings" meet their trigger here and ride along; the other gaps on `upgrade.py`, `events_tail.py` and `statusflow.py` that meet it stay deferred at the five-task limit._

- **Dead parameters** -- Drop `_recover_prior_manifest`'s `quiet`, `_quarantine_corrupt_manifest`'s `backend`, `detect_self_version_transition`'s `config` and `now`, `_peer_states`' `syncthing_state` and `walk_session_metadata`'s `since`, with their call sites and tests. _src/mind_meld/core.py, src/mind_meld/cli.py, src/mind_meld/upgrade.py, src/mind_meld/statusflow.py, src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/conftest.py, tests/test_recovery.py, tests/test_recover.py, tests/test_silent_failure_contract.py, tests/test_upgrade.py, tests/test_diag.py, tests/test_pullflow.py, tests/test_events.py, tests/test_events_budget_scope.py, tests/test_init_events_backfill.py, tests/test_host_usage_snapshot.py, tests/test_retro_fleet_aggregator.py, tests/test_integration.py, ~100 lines._ (M)
- **`anchor_pending`** -- Delete it, switch `tests/test_push_review91.py` to `observe_anchors`, and correct sync.md's no-op sentence and the routing rows. _src/mind_meld/core.py, tests/test_push_review91.py, docs/invariants/sync.md, docs/invariants/README.md, ~30 lines._ (S)
- **Topic names for Track-named tests** -- Rename `tests/test_track_30a.py` and `tests/test_push_review91.py` by topic, or merge them into topic files, and update any routing that names them. _tests/test_track_30a.py, tests/test_push_review91.py, tests/test_docs_routing.py, docs/invariants/README.md, ~20 lines._ (S)
- **Git cancellation test** -- Close the Future test gap "git cancellation cleanup": a `KeyboardInterrupt` through `_run_git` still ends the process group. _tests/test_upgrade.py, ~30 lines._ (S)
- **Inherited git settings test** -- Close the Future test gap "inherited git transfer and batch settings": existing batch-mode options and custom HTTP low-speed settings pass through `_no_prompt_environment`. _tests/test_upgrade.py, ~30 lines._ (S)

**Acceptance:**

1. None of the five dropped parameters remains at a definition or call site; recovery warnings still print in quiet mode, and a test pins it.
2. `anchor_pending` is gone and sync.md names the inline `observe_anchors` check.
3. No test file is named after a Track or review.
4. Both placed test gaps have tests; `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 147: cli's compatibility re-exports

_Depends on: Group 146_

**This group delivers:** a CLI module that re-exports nothing, as the module-boundary test already says it should.

#### Track 147A: Delete cli's compatibility re-exports
_3 tasks . ~280 LOC . low risk . 23 files_
**Outcome:** `cli.py` no longer carries its `noqa: F401` compatibility imports from `core`, `statusflow` and `safety`; tests reach those names through their owner modules; and the test-only `_read_autorun_breadcrumbs` is gone.
**Supports:** MVP-1 on Syncthing; AGENTS.md's cross-cutting rule (imports flow from `cli` to extracted modules; call moved symbols module-qualified so owner patches reach callers) and `tests/test_module_boundaries.py` ("never a compatibility re-export"); the developer's approval of /full-review findings C6 and C4 (2026-11-15 triage).
**Done when:** the acceptance list below passes: the import blocks are gone, every affected test patches or reads the owner module, and the placed test gap has a test.
_touches: src/mind_meld/cli.py, src/mind_meld/statusflow.py, tests/test_attemptlog.py, tests/test_config.py, tests/test_crypto.py, tests/test_diag.py, tests/test_fsutil.py, tests/test_integration.py, tests/test_lockfile.py, tests/test_module_boundaries.py, tests/test_projectid.py, tests/test_pullflow.py, tests/test_pull_helpers.py, tests/test_reader_publication.py, tests/test_root_probers.py, tests/test_safe_str.py, tests/test_track_30a.py, tests/test_silent_failure_contract.py, tests/test_conflict_copy.py, tests/test_pull_result.py, tests/test_manifest.py, tests/test_docs_routing.py, tests/benchmarks/test_kdf_timing.py_
_blocked-by: Track 146A_
_read-first: docs/invariants/README.md, tests/test_module_boundaries.py (the re-export rule), src/mind_meld/cli.py (the compatibility import blocks)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: /full-review findings C6 and C4 at `3289de2`, approved in the 2026-11-15 triage; verified at `f08f10b`. `cli.py`'s "Compatibility imports" block and the `safe_text` "re-exported for backwards-compat" import bring about 95 names the cli body does not use; tests in about 20 files reach some of them as `cli._X` or through `mind_meld.cli._X` patch strings. `_read_autorun_breadcrumbs` wraps `_read_autorun_evidence()["runs"]` and is reached only through the cli re-export. `tests/test_track_30a.py` is renamed by Track 146A; use its new name. The Future test gap "upgrade diagnostic fallback fields" meets its trigger here and rides along._

- **Tests reach owner modules** -- Repoint every test that reaches a compatibility name through `cli` (attribute access or patch string) to the owner module, then delete the `noqa: F401` import blocks and the `safe_text` re-export. _src/mind_meld/cli.py, tests/ files listed in `_touches:`, ~220 lines._ (L)
- **`_read_autorun_breadcrumbs`** -- Delete the wrapper; its tests read `_read_autorun_evidence()["runs"]`. _src/mind_meld/statusflow.py, tests/test_silent_failure_contract.py, tests/test_pullflow.py, tests/test_integration.py, tests/test_module_boundaries.py, ~20 lines._ (S)
- **Upgrade diagnostic fallback test** -- Close the Future test gap "upgrade diagnostic fallback fields": `mm diag`'s Upgrade check section for unknown or unavailable views, no record and missing timestamps. _tests/test_diag.py, ~30 lines._ (S)

**Acceptance:**

1. `cli.py` has no `noqa: F401` compatibility import; `tests/test_module_boundaries.py` passes.
2. No test reaches a non-cli name through `cli`; `_read_autorun_breadcrumbs` is gone.
3. The placed test gap has a test; `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Group 148: Pending join requests on fleet machines

_Depends on: Group 147_

**This group delivers:** a fleet machine where mm manages Syncthing tells the owner, in `mm status` and an attended `mm pull`, that a new machine is waiting to be approved.

#### Track 148A: Show a pending Syncthing join request in mm status and an attended mm pull
_3 tasks . ~200 LOC . low risk . 10 files_
**Outcome:** When a new machine has run `mm init` and asked to share mm's folder, the owner sees it on the next `mm status` or attended `mm pull` on a fleet machine where mm manages Syncthing, with the approval command to run, instead of the join waiting silently until the owner thinks to check.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O2.3 (approval with `mm devices approve`) and O2.6 (`mm status` and `mm diag` read and report Syncthing's state); O8.1 (a line the owner must act on). Admitted by the developer's direct word on PR #342, 2026-11-17: "342 as-is is fine, but maybe we should address your issue with idea 2?"
**Done when:** the acceptance list below passes: status and an attended pull each print one shared-style line while Syncthing lists a pending device, print nothing when it lists none or cannot be read, autopull stays silent, and the invariants and README say so.
_touches: src/mind_meld/syncthing.py, src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/cli.py, tests/test_syncthing.py, tests/test_statusflow.py, tests/test_pullflow.py, docs/invariants/init-devices.md, docs/invariants/sync.md, README.md_
_blocked-by: Track 147A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md (`mm devices approve` steps 7 to 9, "No automatic pairing"), docs/invariants/sync.md (the Track 117A writer paragraph), src/mind_meld/syncthing.py (`SyncthingWriter.pending_devices`, `read_state`, `SyncthingState`), src/mind_meld/statusflow.py (`_syncthing_status_lines`), src/mind_meld/devicesflow.py (`approve`, read only)_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: sessionpipe comparison, 2026-11-17 (fleet onboarding idea 2), filed and deferred in PR #342; promoted in the same PR on the developer's word above. The Factory Manager's case: the read already exists (`mm devices approve` gets pending devices from `SyncthingWriter.pending_devices()`, a GET of Syncthing's pending-devices endpoint), and without a prompt a join waits until the owner thinks to check. Verified at `fa7f1c9`: only `devicesflow.approve` calls `pending_devices()`; `mm status` renders `syncthing.read_state` through `_syncthing_status_lines` with no pending-device field; `mm pull` reads no Syncthing state. Ordered after Track 147A, which also edits `cli.py` and `statusflow.py`._

- **GET-only pending-device read for inspection** -- Give the inspection path a pending-device count from a GET of Syncthing's pending-devices endpoint (the read `SyncthingWriter.pending_devices` sends), as a `SyncthingState` field or a GET-only helper under the same `ManagedRecord` gate as `SyncthingWriter`, within `read_state`'s existing timeout and budget; a failed read leaves the count unknown and never changes `read_state`'s other fields. _src/mind_meld/syncthing.py, tests/test_syncthing.py, ~50 lines._ (S)
- **One line in status and in an attended pull** -- On a machine where mm manages Syncthing, `mm status` adds one Syncthing line and an attended `mm pull` prints one shared-style notice (O8.6), for example `Syncthing: 1 device is asking to join. To approve it, run mm devices approve <device-id> with the ID the new machine printed.` Print the count only: pending device IDs and names stay unprinted, as `mm devices approve` already requires, so approval still rests on the ID pasted from the new machine (O2.3's exact match). `mm autopull`, `mm autopush`, previews and owner-managed machines print nothing; an unknown count prints nothing (status and diag already report an unreachable Syncthing). Pull's read adds no wait when Syncthing is unreachable beyond `read_state`'s budget. _src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/cli.py, tests/test_statusflow.py, tests/test_pullflow.py, ~110 lines._ (M)
- **Invariants and README** -- Record in init-devices.md that inspection reads pending devices GET-only and prints only a count, keeping "No automatic pairing" (no push, pull or auto command accepts a device); add the read to sync.md's writer paragraph and README's Syncthing paragraph and `mm status` description. _docs/invariants/init-devices.md, docs/invariants/sync.md, README.md, ~40 lines._ (S)

**Acceptance:**

1. Against the Syncthing stand-in, with one or more pending devices on a managed machine, `mm status` prints one line naming the count and `mm devices approve <device-id>`, and an attended `mm pull` prints the same notice once; tests pin both.
2. No pending device ID or name appears in either output; a test seeds a peer-controlled name and ID and asserts neither is printed.
3. With no pending device, an unreachable or erroring Syncthing, an owner-managed machine, `mm autopull`, and `mm pull --dry-run`, nothing new prints; tests pin each.
4. No new code path calls a Syncthing write route; the existing write-route guard and "No automatic pairing" tests still pass.
5. docs/invariants/init-devices.md, docs/invariants/sync.md and README.md describe the read and the line.
6. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Execution Map

A Group may launch when every Group in its ← set has landed. Document order is priority, not gating.

```
- Group 135 ← {}
- Group 136 ← {135}
- Group 137 ← {135, 136}
- Group 138 ← {137}
- Group 139 ← {138}
- Group 140 ← {139}
- Group 141 ← {140}
- Group 142 ← {141}
- Group 143 ← {142}
- Group 144 ← {143}
- Group 145 ← {144}
- Group 146 ← {145}
- Group 147 ← {146}
- Group 148 ← {147}
```

**Total: 0 phases . 14 groups . 16 tracks remaining.**

The 2.0 cut still needs the owner's O1 to O4 Syncthing observations on one Mac and both Linux machines, with O6 to O8 holding, recorded in PROGRESS.md Acceptance. A Mac not yet moved holds nothing up; publication is a separate owner decision. Retiring the pre-v0.9.2 conflict format, which the developer approved for 2.0, waits in Future for that line.

---

## Future

Deferred: docs/roadmap-future.md (58 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
