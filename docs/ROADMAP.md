# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Everything carded through Track 154A (the join, prompt and fleet-device UX) shipped by v1.12.73 (independent receipts in shipped history), so SPEC O2 as amended on 2026-11-20 is built. Track 155A, the three deepsec-confirmed bugs, is in progress (draft PR #364). The plan below also carries three bugs the owner hit on 2026-11-20 (Track 156A), `mm pull` reporting nothing while Syncthing still delivers a peer's push (Track 157A), and cross-model memory, which the developer moved before 2.0 on 2026-11-21 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)): a qualification in isolated homes (Track 155B), then per-machine Codex memory copies (158A), one local qmd index over every provider's memory (159A), and qmd setup with the recommended pointer line (160A). The owner will decide the path to 2.0 after using the Syncthing fleet for a while.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **A reproducible bug the owner actually encountered is admitted, not deferred (owner, 2026-11-09).** After the manager said the codex `skills/gstack/*` exclusion was not in the two Tracks being carded, the owner asked "why isn't that fix in our current roadmap? Anything that is a reproducible bug we actually encountered should be current". A Future entry that records a bug seen on the owner's machines, with a cause found in the code, goes to Current Plan on the next regeneration; deferral is for work nobody has hit, design questions and claims nobody verified, and the manifestation gate still prunes defects nobody has hit.
- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval, introducers and mesh step (adding a device a fleet member's encrypted record names; spec amended 2026-11-18) on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration and scan requests are refused until the spec's revisit trigger is observed. Removing a retired machine and correcting fleet machines' Syncthing names on mm-managed machines were approved by the developer on 2026-11-20 and enter the spec through Track 154A's PR. Until it merges, the spec still refuses them.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) documented in the shared output helpers.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

Group 155 is active (declared by the Factory Manager, 2026-11-21): Track 155A is factory item `008f1cf4` on branch `mmm/008f1cf4`, draft PR #364, in its final gate. Its ID and Group label are pinned. Track 155B shares the Group because the packer bins it with 155A (no file in common); it has not started and may launch now.

### Group 155: Fixes from the deepsec scan ∥ cross-model memory qualification

_Depends on: none_

**This group delivers:** an edit made to a conflicted file while `mm resolve` waits at its prompt is no longer overwritten; the synced git-activity rows carry no local repository paths; a new fleet started with a relative storage path saves it as an absolute path. Separately, the owner learns whether fresh Codex and Claude Code sessions find each other's memory through qmd with only the recommended line, before any O9 code is written.

#### Track 155A: Keep edits made during `mm resolve`, keep repository paths off the synced git rows, and save an absolute new-fleet storage path
_3 tasks . ~250 LOC . medium risk . 13 files_
**Outcome:** the owner can resolve a conflict while an agent keeps working in that project without losing the agent's edit; the fleet's synced event log stops naming the folders where the owner keeps repositories; and a new fleet works from every directory whatever form of path the owner typed.
**Supports:** O1.1 (when two machines change the same file, mm keeps both versions until `mm resolve`), the docs/SPEC.md constraint that usage capture publishes "never ... filesystem paths", and O2's `mm init` new-fleet path. The developer admitted all three on 2026-11-20 (option (a): card the three confirmed bugs as one Track). None has been reported from real use yet.
**Done when:** tests show that an edit to the canonical file or the conflict copy after `mm resolve` showed the conflict survives each (l), (r) and (m) choice in both conflict-copy layouts, with both files left and a run-again line; a written `git-snapshot` row holds no `local_path` and no `skipped[].path`, while repository cursors still advance, an interrupted capture still reconciles and `mm recapture --dry-run` still names skipped repositories; and `mm init` in new-fleet mode saves a relative answer such as `Sync/mind-meld` as an absolute path.
**Acceptance:**
1. Suppose the canonical file or the conflict copy is edited after `mm resolve` shows the conflict and before the owner chooses (l), (r) or (m). Then that choice changes nothing, in both conflict-copy layouts (before and after the inversion): both files stay, and one O8 line names the changed file and `mm resolve` to run again.
2. A `git-snapshot` row written to the synced `mm-events` log holds no `local_path` and no `skipped[].path`. It keeps the remote URL, the commits and the typed skip reasons.
3. Repository cursors still advance, an interrupted cache commit still reconciles from this device's own event files, and `mm recapture --dry-run` still names skipped repositories. All three get the paths from local state, never from the wire.
4. `mm init` starting a new fleet saves a relative storage answer such as `Sync/mind-meld` as an absolute path, as joins already do.
5. docs/invariants/conflicts.md, docs/invariants/events-retro.md and docs/invariants/init-devices.md record the three rules, and README's `mm recapture --dry-run` wording matches if it changes.
_touches: src/mind_meld/resolveflow.py, src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/recaptureflow.py, src/mind_meld/initflow.py, tests/test_conflict_copy.py, tests/test_events.py, tests/test_recapture.py, tests/test_initflow.py, docs/invariants/conflicts.md, docs/invariants/events-retro.md, docs/invariants/init-devices.md, README.md_
_read-first: docs/invariants/conflicts.md (the `mm resolve` walk, `(p)romote` on the canonical-exists path, loop the parse not the dispatch), docs/invariants/events-retro.md (git-walk repository loss, recapture walks, cursor reconciliation), docs/invariants/init-devices.md (join record), docs/SPEC.md Constraints and O1, src/mind_meld/pullflow.py `_unchanged_since`, tests/test_module_boundaries.py_

_Source: deepsec 2.3.16 security scan of 2026-11-20 (Codex model, partial coverage: about 25 of 75 source files), triage outside the repository, confirmed items C1 to C3; the developer's decision the same day, relayed by the Factory Manager. Verified at `ab86b98`. C1: `resolveflow._resolve_interactive_loop` reads both files, waits at `typer.prompt("  Choice", ...)`, then (r) post-inversion runs `cpath.rename(canonical)`, (l) pre-inversion renames over canonical, (l) post-inversion and (r) pre-inversion unlink the conflict copy, and (m) runs `fsutil.atomic_write_bytes(canonical, merged_bytes, ...)`; none compares either file with what was shown, and no recovery copy is kept. The inline pull path re-checks with `pullflow._unchanged_since` (size, mtime, ctime, inode). C2: `events.walk_git_projects` builds each project with `"local_path": str(root)` and each skip with `{"path": str(root), ...}`; `events_tail` writes `capture.git_rows` into the synced `mm-events` log; docs/SPEC.md Constraints say usage capture publishes "never ... filesystem paths". [challenge] The triage names one local reader of these fields; there are three: `events._apply_repo_cursors_to_cache`, the own-file replay that reads `git-snapshot` rows back from this device's event files to reconcile an interrupted cache commit, and `recaptureflow`'s dry-run, which prints each skipped path. docs/invariants/events-retro.md already records that "these paths already ride the synced git-snapshot row". The paths must reach those readers locally, not over the wire. Rows already published keep their paths; whether to rewrite this device's own event files is for the plan stage, under the rule that no storage file is rewritten by more than one machine. C3: `initflow.init` absolutizes a relative storage answer only `if not new_fleet`, and saves `storage_path` into `config["storage"]["path"]`; `config.py` resolves that path against the working directory on every load. [challenge] The triage says the condition "came in with #355". Before #355 (`0bec1e4`) neither mode absolutized; #355 fixed joins and left new fleets as they were. The root cause is older than #355. The default `~/Sync/mind-meld` is unaffected. The triage's lower-priority millisecond windows in pre-inversion migration and promotion are deferred to Future with the pull-apply windows. Recorded test gaps in Future whose triggers name `resolveflow.py`, `initflow.py` or `events_tail.py` with their test files stay there: one more S task would put this Track over the session-weight cap of 6._

- **Re-check the conflict pair before `mm resolve` writes** -- in `resolveflow._resolve_interactive_loop`, record the `lstat` identity of the canonical file and the conflict copy when the conflict is shown, and compare both (as `pullflow._unchanged_since` does; share the helper without breaking tests/test_module_boundaries.py) immediately before each rename, unlink and merge write of the (l), (r) and (m) choices. On a mismatch, change nothing, keep both files and print one O8 line naming the file that changed and `mm resolve` to run again. Tests in tests/test_conflict_copy.py cover each choice in both conflict-copy layouts. Update docs/invariants/conflicts.md. _resolveflow.py, tests/test_conflict_copy.py, docs/invariants/conflicts.md, ~110 lines._ (M)
- **Keep repository paths off the synced git-snapshot rows** -- stop writing `local_path` and `skipped[].path` into `git-snapshot` rows, keeping the remote URL, commits and typed skip reasons. Give the three local readers (`_apply_repo_cursors_to_cache`, the own-file replay and the recapture dry-run) the paths through local state, the in-memory capture or the local cursor cache, never the wire. Test that a written row holds no path while cursors, reconciliation and the dry-run list still work. Update docs/invariants/events-retro.md and README's `mm recapture --dry-run` wording if it changes. _events.py, events_tail.py, recaptureflow.py, tests/test_events.py, tests/test_recapture.py, docs/invariants/events-retro.md, README.md, ~120 lines._ (M)
- **Save an absolute storage path when starting a new fleet** -- in `initflow.init`, absolutize a relative storage answer in both modes, and add a new-fleet test with a relative answer in tests/test_initflow.py. Note the rule beside the join record's in docs/invariants/init-devices.md. _initflow.py, tests/test_initflow.py, docs/invariants/init-devices.md, ~20 lines._ (S)

#### Track 155B: Qualify cross-model memory through qmd in isolated homes
_1 task . ~150 LOC . low risk . 1 file_
**Outcome:** before any O9 code is written, the owner knows whether fresh Codex and Claude Code sessions, given only the recommended pointer line, find another agent's memory through `qmd search` and surface a contradiction instead of hiding it, and which Codex memory files the per-machine copies should carry.
**Supports:** docs/SPEC.md O9.6 (qualification before implementation), planned before 2.0 on the developer's word (2026-11-21: "1 c"). It replaces the Future card for the 2026-10-29 design's qualification (formerly Track 96A), which was retired with that design.
**Done when:** docs/designs/cross-model-memory.md records Q1, Q2 and Q3, run in temporary `HOME`, `CODEX_HOME` and XDG folders, with the observed codex-cli, Claude Code and qmd versions, the commands the agents ran and the final pointer line. A failed Q1 or Q2 is written up for the owner, and no product code changes.
**Acceptance:**
1. Every check runs with a temporary `HOME`, `CODEX_HOME` and XDG folders. Nothing reads or writes the owner's real Claude, Codex, Grok or Muse stores, qmd index or credential files. Sessions authenticate through an API key in the environment or a fresh login inside the temporary home.
2. Q1: a fresh Codex session, with only the pointer line in its `AGENTS.md`, answers a question that only a seeded Claude-origin memory answers, by running `qmd search`. A fresh Claude Code session, with only the line in its `CLAUDE.md`, finds a seeded Codex-origin memory the same way. The result is recorded either way, with the commands each session ran.
3. Q2: with two contradicting memories seeded from different providers, each session reports both, with their paths, and asks the owner instead of choosing one.
4. Q3: in an isolated `CODEX_HOME` with Codex's `memories` feature on, the design records the files Codex writes and which it regenerates wholesale, and chooses the copy set. If Codex writes no memory within the session budget, the default set (`memory_summary.md`, `MEMORY.md`, `rollout_summaries/*.md`) is kept and the reason recorded.
5. docs/designs/cross-model-memory.md records the results and versions and the final pointer line. A failed Q1 or Q2 sends the design back to the owner, and Tracks 158A, 159A and 160A stay blocked. No product code changes.
_touches: docs/designs/cross-model-memory.md_
_out: 158A_
_read-first: docs/designs/cross-model-memory.md (Qualification, Design 5 and 6), docs/SPEC.md O9_

_Source: the developer's decisions of 2026-11-20 and 2026-11-21 quoted in docs/SPEC.md's 2026-11-21 amendment, and the Factory Manager's brief: "fresh Codex and Claude sessions in isolated homes (temporary HOME and CODEX_HOME, never the owner's real stores) find another agent's memory through qmd with only the recommended pointer line". Verified 2026-11-20 on `linux-a`: qmd 2.8.3 is installed (`qmd --version`); `qmd search` returned two contradicting memories from two collections in a temporary `HOME`; codex-cli 0.171.0 lists `memories stable false`._

- **Run Q1 to Q3 and record them** -- seed the memories, build the planned `mm-` collections in the temporary home's qmd index, run the fresh sessions, and write the results into the design's Qualification section. Session: fresh, effort medium; verify `./bin/check tests/test_docs_routing.py`. _docs/designs/cross-model-memory.md, ~150 lines._ (M)

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154_

The plan carries two kinds of work beside Group 155. The first is bugs the owner hit: the reinstall command that fails on pipx's uv backend, a stale removal-wait line in `mm status` and `mm init`'s non-actionable notices (Track 156A, placed on the developer's word: "2 a"), and `mm pull` printing nothing to apply while Syncthing still delivers a peer's push (Track 157A). Track 156A waits for Track 155A, which is in flight and changes the same files.

The second is cross-model memory (docs/SPEC.md O9), which the developer moved before 2.0 ("1 c"). Its qualification (Track 155B) gates the three implementation Tracks, which run in a chain: per-machine Codex memory copies (158A), one local qmd index over every provider's memory (159A), and qmd setup with the recommended pointer line (160A). Keyword search is the baseline and embeddings are opt-in ("3 a").

### Group 156: Observed sync and setup bugs

_Depends on: Group 155_

**This group delivers:** mm's printed reinstall command and the automatic pinned reinstall work on pipx's uv backend; `mm status` drops a removal-wait line once the removal is done; `mm init` stops printing capture notices the owner cannot act on.

#### Track 156A: Reinstall through pipx's uv backend, clear resolved removal waits from `mm status`, and keep init's capture notices out of its output
_5 tasks . ~260 LOC . low risk . 14 files_
**Outcome:** the owner can copy and run any reinstall command mm prints, and a pinned install reinstalls itself, when pipx runs on its uv backend; `mm status` stops saying "Syncthing device removals wait for <machine>" once the removal has happened; and `mm init` ends with its outcome, with capture notices the owner cannot act on recorded for `mm status` and `mm diag` instead.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.1 (nothing is printed unless the owner must act; routine detail stays in `mm status` and `mm diag`), O8.4 (status shows only the latest run of each kind) and the auto-update constraint (pinned installs). The developer observed all three on 2026-11-20 and placed them on the Current Plan (verbatim, 2026-11-21: "2 a"), under the standing rule on reproducible bugs. Items 2 and 3 are also in PR #362's review backlog.
**Done when:** tests pin the printed reinstall command and the automatic pinned reinstall's argument list and environment for pipx's uv and venv backends; a later push, pull, autopush or autopull that finds no removal waiting clears the removal-wait line from `mm status`; `mm init`'s events backfill prints none of the no-action capture notices, and `mm status` and `mm diag` show them.
**Acceptance:**
1. Every reinstall command mm prints (`upgrade.INSTALL_CMD`, `upgrade.reinstall_cmd`) and the automatic reinstall of a pinned install (`upgrade.update_argv` for `install.kind == "pinned"`) succeed when pipx uses its uv backend with an existing venv, and still succeed on pipx's default venv backend. Each printed command stays one copyable line for bash and zsh.
2. Tests stub the subprocess and pin the printed text and the automatic reinstall's argument list and environment for both backends.
3. While a retired device's removal still waits, `mm status` keeps the "Syncthing device removals wait for <machine>" line, refreshed by the latest push, pull, autopush or autopull, not only the last attended push. Once a later run finds nothing waiting, the line is gone from `mm status`. The auto commands stay silent.
4. During `mm init`, `events_tail._run_events_backfill` prints none of the capture notices that need no action from the owner: "git repository discovery hit its time budget", "events backfill budget exceeded", and a "host-usage snapshot skipped" notice that was skipped for a deadline or budget. A notice carrying a fix the owner must run keeps printing in the shared notice form (O8.1). The no-action notices are recorded, and `mm status` and `mm diag` show them until the next attended push replaces them.
5. The two Future test gaps placed below are closed: init keeps the default `mm update` advice when reading the cached upgrade view raises, and mesh breadcrumbs with several omitted details keep their priority and their display in `mm status`.
6. docs/invariants/auto-upgrade.md (the reinstall command), docs/invariants/events-retro.md (init backfill notices) and README's install and Upgrading text match the new behavior.
_blocked-by: Track 155A_
_touches: src/mind_meld/upgrade.py, src/mind_meld/meshflow.py, src/mind_meld/attemptlog.py, src/mind_meld/statusflow.py, src/mind_meld/events_tail.py, src/mind_meld/initflow.py, tests/test_upgrade.py, tests/test_self_update.py, tests/test_mesh.py, tests/test_statusflow.py, tests/test_init_events_backfill.py, docs/invariants/auto-upgrade.md, docs/invariants/events-retro.md, README.md_
_read-first: docs/SPEC.md O8 and the auto-update constraint, docs/invariants/auto-upgrade.md, docs/invariants/events-retro.md (init backfill), src/mind_meld/upgrade.py (`INSTALL_CMD`, `update_argv`, `reinstall_cmd`), src/mind_meld/meshflow.py (the "Syncthing device removals wait for" degradations), src/mind_meld/attemptlog.py (the sanitized degradation pattern), src/mind_meld/events_tail.py `_run_events_backfill`_

_Source: the developer's observations of 2026-11-20, relayed by the Factory Manager on 2026-11-20 and placed on the Current Plan on 2026-11-21 ("2 a"), and PR #362's review backlog (merged 2026-11-21) for items 2 and 3. The developer observed that `pipx install --force` fails when pipx runs on its uv backend: an existing venv is refused unless `UV_VENV_CLEAR=1` is set. Verified at `cf5edc0`: `upgrade.INSTALL_CMD` is `pipx install --force <spec>`, and `update_argv` and `reinstall_cmd` build `install --force` argument lists. `meshflow.py` composes the "Syncthing device removals wait for ..." degradations, and `attemptlog.py` admits them to the run record. `_run_events_backfill` prints "mm: notice: git repository discovery hit its time budget: ..." and "mm: notice: events backfill budget exceeded" through `token_usage._diagnostic`, and composes "host-usage snapshot skipped (...)". Its own comment says "Init has no `mm-push` row and no autorun breadcrumb, so the notice is the only surface available"._

- **Reinstall on both pipx backends** -- in `upgrade.update_argv`, run the pinned reinstall with `UV_VENV_CLEAR=1` in its environment. Make `INSTALL_CMD` and `reinstall_cmd` print a form that works on both backends, `UV_VENV_CLEAR=1 pipx install --force ...` unless the plan finds a form pipx accepts on both. Pin both backends in tests/test_upgrade.py and tests/test_self_update.py, and update docs/invariants/auto-upgrade.md and README. _upgrade.py, tests/test_upgrade.py, tests/test_self_update.py, docs/invariants/auto-upgrade.md, README.md, ~70 lines._ (S)
- **Let the latest mesh run replace the removal-wait line** -- record the mesh pass's removal outcome from every push, pull, autopush and autopull so `mm status` shows the latest one, and drop the line when nothing waits. Test the waiting, cleared and auto-command cases in tests/test_mesh.py and tests/test_statusflow.py. _meshflow.py, attemptlog.py, statusflow.py, tests/test_mesh.py, tests/test_statusflow.py, ~80 lines._ (S)
- **Record init's no-action capture notices for `mm status` and `mm diag`** -- in `events_tail._run_events_backfill`, record the budget and deadline notices instead of printing them during init, keep actionable notices printed, and show the record in `statusflow` until the next attended push. Test in tests/test_init_events_backfill.py and tests/test_statusflow.py, and update docs/invariants/events-retro.md. _events_tail.py, initflow.py, statusflow.py, tests/test_init_events_backfill.py, tests/test_statusflow.py, docs/invariants/events-retro.md, ~90 lines._ (S)
- **Test gap: init's no-release notice when reading the cached upgrade view fails** -- add a case beside `test_incomplete_scan_uses_no_release_notice_when_upgrade_cache_is_current` that makes `upgrade.cached_upgrade_view` raise, and assert init succeeds and its advice keeps `mm update`. _Source: Future entry of the same title (`[review:files=src/mind_meld/events_tail.py|tests/test_init_events_backfill.py]`, Track 135A)._ _tests/test_init_events_backfill.py, ~25 lines._ (S)
- **Test gap: Mesh breadcrumbs with several omitted details** -- add several complete diagnostic clauses and long device names to the `meshflow.compose_hook_breadcrumb` fixtures, and check the detail priority and how status displays the omissions. _Source: Future entry of the same title (`[review:files=src/mind_meld/meshflow.py|tests/test_mesh.py]`, Track 152A)._ _tests/test_mesh.py, ~30 lines._ (S)

### Group 157: Pull waits for Syncthing delivery

_Depends on: Group 155_

**This group delivers:** after a peer pushes, one `mm pull` waits briefly for Syncthing to deliver the files and applies them, or says Syncthing is still delivering and to run it again, instead of printing nothing to apply.

#### Track 157A: Tell the owner when Syncthing is still delivering a peer's push instead of reporting nothing to pull
_4 tasks . ~280 LOC . medium risk . 9 files_
**Outcome:** the owner runs `mm pull` once after a peer pushes. While Syncthing is still delivering, pull waits a few seconds and applies what arrives, or ends by saying Syncthing is still delivering N files and to run `mm pull` again. It never reports nothing to apply while files are still on their way.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O3.1 ("Pull applies what has arrived and says what it is still waiting for") and O8.1. The developer (verbatim, 2026-11-20): "I have never had mm pull fail or say waiting, it just says 0 files pulled, and then you have to run it several times in succession until they show up". Admitted under the standing rule on reproducible bugs the owner hit.
**Done when:** tests against the Syncthing API stand-in show an attended pull waiting while the fleet folder still needs files and then applying them; a wait that ends with files still needed printing the delivering line with `mm pull` and exiting 0; a pull without a terminal and `mm autopull` never waiting; and an unreachable API or an iCloud Drive storage path leaving today's output unchanged.
**Acceptance:**
1. Before reading peer manifests, an attended `mm pull` on a terminal reads the fleet folder's Syncthing state through `syncthing.read_state` (folder state and `needFiles`). It does this whether mm or the owner manages Syncthing, using read-only API calls.
2. When Syncthing reports files still needed (a need count above zero, or the folder syncing or preparing to sync), pull shows a transient progress line, "waiting for Syncthing to deliver N files". It waits until the count reaches zero, up to a short bound (a setting, default 15 s), and then pulls.
3. When the bound ends with files still needed, pull applies what has arrived. Its outcome adds one line saying Syncthing is still delivering N files, ending with `Run: mm pull`, and it exits 0, because waiting is not an error (O3.1). Pull never prints only "nothing to apply" while Syncthing reports files still needed.
4. A pull without a terminal does not wait, and reports the delivering line as in criterion 3. `mm autopull` never waits and prints nothing for this: it records the pending delivery, and `mm status` shows it until a later pull or the folder catches up.
5. When Syncthing's API is unreachable, unauthorized or not configured, or the storage path is not a Syncthing folder (a Mac still on iCloud Drive), pull behaves as today and adds no output. `mm diag` keeps reporting the API state.
6. The two Future test gaps placed below are closed: summary counts across peers and deletion-only pulls, and plural held counts with mixed source outcomes.
7. docs/invariants/sync.md (Syncthing-tolerant reads and waiting) and README's pull text describe the wait and the delivering line.
_touches: src/mind_meld/pullflow.py, src/mind_meld/syncthing.py, src/mind_meld/cli.py, src/mind_meld/statusflow.py, src/mind_meld/config.py, tests/test_pullflow.py, tests/test_statusflow.py, docs/invariants/sync.md, README.md_
_read-first: docs/SPEC.md O3 and O8, docs/invariants/sync.md "Syncthing-tolerant reads and waiting (Track 87A)", src/mind_meld/syncthing.py `read_state` (`files_to_fetch`, `folder_state`), src/mind_meld/pullflow.py `_pull_core`, `WaitingPeer`, `waiting_clause` and `_print_pull_summary`_

_Source: the developer's report of 2026-11-20, relayed by the Factory Manager the same day. Verified at `cf5edc0`: pull's waiting clause counts only blobs named by peer manifests that have already arrived (Track 87A `WaitingPeer`). A push whose manifest Syncthing has not delivered yet therefore leaves nothing to apply and no wait, and `_print_pull_summary` prints "nothing to apply". `syncthing.read_state` already reads `needFiles`, `needBytes` and the folder state from `/rest/db/status`. Pull calls it only after applying, for the pending-join notice on managed machines (`pullflow.notice_pending_syncthing_join`); otherwise only `mm status`, `mm diag` and the mesh step call it._

- **Wait for Syncthing's delivery before pulling, and say when it is still delivering** -- in `_pull_core` (through a parameter the `cli.pull` shell passes when attended on a terminal), read `syncthing.read_state`, wait with the bounded progress line, and add the delivering line to the outcome. Add the bound to config, and test it against the API stand-in in tests/test_pullflow.py. _pullflow.py, syncthing.py, cli.py, config.py, tests/test_pullflow.py, ~170 lines._ (M)
- **Record a pending delivery from autopull for `mm status`** -- record the need count without waiting, and show it in the `mm status` verdict until it clears. Update docs/invariants/sync.md and README. _pullflow.py, cli.py, statusflow.py, tests/test_statusflow.py, docs/invariants/sync.md, README.md, ~60 lines._ (S)
- **Test gap: summary counts across peers and deletion-only pulls** -- extend the two-peer and deletion fixtures to pin count-bearing labels, and check that a deletion-only result produces no source header. _Source: Future entry of the same title (`[review:files=src/mind_meld/pullflow.py|tests/test_pullflow.py]`, Track 151B)._ _tests/test_pullflow.py, ~25 lines._ (S)
- **Test gap: plural held counts and mixed source outcomes** -- exercise one peer and source with several held files, and check the plural count and when `Up to date with` appears beside other outcomes. _Source: Future entry of the same title (`[review:files=src/mind_meld/pullflow.py|tests/test_pullflow.py]`, Track 151B)._ _tests/test_pullflow.py, ~25 lines._ (S)

### Group 158: Per-machine Codex memory copies

_Depends on: Group 155, Group 156_

**This group delivers:** each machine's Codex memory reaches every other machine as a read-only, per-machine copy in mm's own folder, which forgets what the source machine's Codex forgets and never touches any machine's Codex store.

#### Track 158A: Publish each machine's Codex memory as per-machine copies
_3 tasks . ~420 LOC . medium risk . 12 files_
**Outcome:** what Codex remembers on one of the owner's machines is on disk on every other machine, in mm's own folder, ready for the memory index, without conflicts and without mm writing into any Codex store.
**Supports:** docs/SPEC.md O9.1 (Codex memories roam as per-machine copies, one writer per file, outside every native store). The developer, 2026-11-20: "o9 - why just codex?". The other providers already sync.
**Done when:** tests show a push publishing only this machine's Codex memory files under its device ID, a pull placing each peer's files in that peer's copies folder, a source-side deletion or rewrite propagating with no conflict copy, a forged path refused, a retired machine's copies removed, and nothing written under any `CODEX_HOME`.
**Acceptance:**
1. A new `codex-memory` source reads this machine's Codex memory files (the set Track 155B chose) from `CODEX_HOME/memories/` without writing there, and publishes them under this machine's device ID. Codex's `.git`, its SQLite files and files outside the set never sync.
2. At pull, each peer's files land under `~/.local/share/mind-meld/codex-memory/<peer device id>/`. A file in a peer's manifest whose path does not start with that peer's own device ID is refused, so each file has one writer. Nothing is written into any `CODEX_HOME`.
3. When the source machine's Codex drops or rewrites a memory, the next push and pull remove or replace that copy on every peer with no conflict copy. Regenerating identical content publishes nothing.
4. The copies of a machine retired with `mm devices retire` are removed from every peer at its next pull.
5. `mm init` offers the source when `CODEX_HOME/memories/` exists (Enter = yes, as for other detected sources). `mm enable-source codex-memory` and `mm disable-source codex-memory` work, and disabling it on one machine deletes nothing fleet-wide.
6. `mm sources` and `mm status` list the source. docs/SPEC.md's table (the Codex row and the intentional-gaps line), root SPEC.md, README's Codex section and docs/invariants/sync.md describe it.
_touches: src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/pullflow.py, src/mind_meld/initflow.py, src/mind_meld/sourceflow.py, tests/test_codex_memory_source.py (new), tests/test_initflow.py, tests/test_sourceflow.py, docs/invariants/sync.md, docs/SPEC.md, SPEC.md, README.md_
_blocked-by: Track 155B_
_out: 159A_
_read-first: docs/designs/cross-model-memory.md (Design 2), docs/SPEC.md O1.1 and O9, docs/invariants/README.md rows for the touched modules, docs/invariants/sync.md (truth-based manifests, tombstone suppression), src/mind_meld/manifest.py (`MUSE_SYNCED_SUBDIRS`, `GROK_MEMORY_ROOT` and the source walkers), src/mind_meld/config.py (`DEFAULT_CODEX_DIR`, `DEFAULT_SOURCES`)_

_Source: the developer's decisions of 2026-11-20 and 2026-11-21 (docs/SPEC.md O9) and the brief: "Codex memories, which each machine regenerates, roam as per-machine copies (one writer per file) in an mm-owned directory outside every native store". Codex observations: 2026-10-29 with codex-cli 0.164.0 on `mac-a` (archived design) and 2026-11-20 with codex-cli 0.171.0 on `linux-a` (`memories stable false`). `~/.local/share/mind-meld/` is mm's existing data folder (`retention.DEFAULT_RETROS_DIR`, the retro-fleet skill target)._

- **Publish this machine's Codex memory under its device ID** -- add the `codex-memory` source: a walker that reads the chosen set from `CODEX_HOME/memories/` and publishes relative paths prefixed with this device's ID, never walking the copies folder, and its config entry. _config.py, manifest.py, tests/test_codex_memory_source.py, ~180 lines._ (M)
- **Place peers' copies in mm's folder, one writer per file** -- for this source, apply peer files under `~/.local/share/mind-meld/codex-memory/<peer id>/`, refuse a path outside the peer's own prefix, remove a retired machine's copies, and test deletion and rewrite propagation with no conflict copy. _pullflow.py, tests/test_codex_memory_source.py, ~180 lines._ (M)
- **Offer it in `mm init` and the source commands, and document it** -- detection in `initflow._prompt_sources` and `sourceflow`, plus the docs listed above. _initflow.py, sourceflow.py, tests/test_initflow.py, tests/test_sourceflow.py, docs/invariants/sync.md, docs/SPEC.md, SPEC.md, README.md, ~60 lines._ (S)

### Group 159: Local memory index

_Depends on: Group 158_

**This group delivers:** every machine where the owner turns it on keeps one qmd index over every provider's memory and the Codex copies, refreshed after each sync, so any agent can search everything any agent learned on any machine.

#### Track 159A: Keep one local qmd index over every provider's memory, refreshed after each sync
_2 tasks . ~480 LOC . medium risk . 11 files_
**Outcome:** on a machine with the index on, `qmd search` finds Claude, Grok, Muse and Codex memories from every machine, and a memory synced in or removed on the last push or pull is found or gone, without the owner rebuilding anything.
**Supports:** docs/SPEC.md O9.2 (one local index, derived, never synced, refreshed after each `mm push` and `mm pull`) and O9.3 (each hit names its provider and, for Codex, its machine). The developer, 2026-11-20: "as we sync memories across machines, they will get incorporated into local store naturally, or removed".
**Done when:** tests with a stub `qmd` on `PATH` show mm creating, reconciling and removing only its `mm-` collections, attended push and pull refreshing after the lock is released, the auto commands starting the refresh detached without waiting, a missing or failing qmd leaving the sync's outcome and exit code unchanged with one `mm status` line and its fix, and `mm memory-index` showing the state and turning the index on and off.
**Acceptance:**
1. With the index on, mm keeps these qmd collections through qmd's own CLI, only for enabled sources whose folder exists: `mm-claude` (`~/.claude/projects`, mask `*/memory/**/*.md`), `mm-grok` (`~/.grok/memory-v2`, mask `**/topics/**/*.md`), `mm-muse` (the Muse root's `memory/`), `mm-codex-<this machine>` (`CODEX_HOME/memories`, the chosen set) and one `mm-codex-<peer>` per peer copies folder. Machine names pass through `safety` and are reduced to `[a-z0-9-]`. mm adds, changes or removes only collections named `mm-…`, and never edits qmd's files directly.
2. Attended `mm push` and `mm pull` reconcile the collections and run `qmd update` after releasing the sync lock, printing nothing on success. With embeddings on, they then start `qmd embed` in the background.
3. `mm autopush` and `mm autopull` start the same refresh detached, never wait for it and print nothing (O8.2, O8.3).
4. After a peer's memory file is added, changed or deleted and pulled, a `qmd search` for it reflects the change. A peer's collection appears with its copies folder and leaves with it.
5. A missing qmd or a failed or timed-out refresh never changes a sync's outcome or exit code. `mm status` shows one line with its fix, and `mm diag` shows the qmd version, mm's collections and the last refresh's time and result.
6. `mm memory-index` shows the index state. `mm memory-index on` and `mm memory-index off` turn it on and off, and off removes only `mm-` collections. The choice and the embeddings choice live in this machine's `config.toml` and never sync. The command is added to AGENTS.md's command list and to the integration tests' `COMMAND_INTENTS62`.
7. The new `memoryindex.py` module is listed in the docs/invariants/README.md module map and passes tests/test_module_boundaries.py. The `cli` shells call it module-qualified, beside `_sync_update`. docs/invariants/sync.md states the refresh rule, and README documents the index and the optional `claude mcp add qmd -- qmd mcp` and `codex mcp add qmd -- qmd mcp` commands, which mm never runs.
_touches: src/mind_meld/memoryindex.py (new), src/mind_meld/cli.py, src/mind_meld/config.py, src/mind_meld/statusflow.py, tests/test_memory_index.py (new), tests/test_integration.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/sync.md, AGENTS.md, README.md_
_blocked-by: Track 158A_
_out: 160A_
_read-first: docs/designs/cross-model-memory.md (Design 3, 4, 5 and 8), docs/SPEC.md O8 and O9, docs/invariants/README.md (module map and boundaries), src/mind_meld/cli.py `_sync_update` and the `push`, `pull`, `autopush` and `autopull` shells, tests/test_module_boundaries.py_

_Source: the developer's decisions of 2026-11-20 and 2026-11-21 (docs/SPEC.md O9), the brief ("the index is derived, never synced, and refreshed after mm pull"), and measurements of qmd 2.8.3 in a temporary `HOME` on `linux-a` on 2026-11-20. Adding a 400-file collection took 0.5 s, `qmd update` took 0.2 s, `qmd search` took 0.16 s and a bare start took 0.14 s. `--mask` selected memory files only, a missing folder was refused, and `qmd update` dropped a deleted file. The refresh after push is added to the developer's after-pull rule so that a memory written on this machine reaches its other agents at the session-end hook._

- **Add `memoryindex.py`: collections, refresh and `mm memory-index`** -- reconcile the `mm-` collections from the enabled sources and fleet machines, run `qmd update` inline on attended runs and detached from the hooks, start `qmd embed` in the background when embeddings are on, and add the command and its config. Test with a stub `qmd` in tests/test_memory_index.py, and add the command to tests/test_integration.py and AGENTS.md. _memoryindex.py, cli.py, config.py, tests/test_memory_index.py, tests/test_integration.py, tests/test_module_boundaries.py, AGENTS.md, ~400 lines._ (L)
- **Report the index in `mm status` and `mm diag`, and document it** -- one status line with its fix on a failed refresh, the diag detail, and the module-map, invariant and README text. _statusflow.py, docs/invariants/README.md, docs/invariants/sync.md, README.md, ~80 lines._ (S)

### Group 160: qmd setup and the pointer line

_Depends on: Group 159_

**This group delivers:** a fresh install sets qmd up the way it sets Syncthing up, `mm init` offers the memory index and, separately, semantic search, and setup recommends the one instruction line that sends every agent to search memory before assuming.

#### Track 160A: Set up qmd and the memory index from bootstrap and `mm init`, and recommend the pointer line
_3 tasks . ~320 LOC . medium risk . 7 files_
**Outcome:** on a new or rejoining machine, the bootstrap script installs qmd if it is missing. `mm init` offers the memory index and semantic search, and offers to add the pointer line to the owner's global `CLAUDE.md` and `AGENTS.md`, or prints it for a file that lives in the owner's config repository. Agents on that machine then search memory before assuming.
**Supports:** docs/SPEC.md O9.3 (the recommended line, added only with the owner's approval) and O9.4 (setup). The developer, 2026-11-20: "we would want to facilitate installed qmd just like we do syncthing on setup" and "reaching for the search tool -- we would want to recommend a line in agents.md as part of setup". Embeddings are opt-in ("3 a").
**Done when:** tests show the bootstrap installing qmd through Bun, through npm with a per-user prefix, and through Node from pacman or Homebrew when neither runtime exists, leaving an existing qmd alone and continuing past a failure. They also show `mm init` asking the index, embeddings and pointer-line questions with the listed Enter defaults, keeping them in saved join answers, appending the line once to a regular file and only printing it for a symlinked one.
**Acceptance:**
1. After Syncthing, `bin/bootstrap` installs qmd when `qmd --version` does not run: with `bun install -g @tobilu/qmd` when Bun exists, otherwise with npm into a per-user prefix. When neither Node 22+ nor Bun exists, it installs Node with pacman on Arch or Homebrew on macOS. It never downloads qmd's models and never uses sudo for qmd itself. It leaves an existing qmd alone. A failure prints the manual command and the rest of the install continues.
2. When qmd runs, `mm init` asks `Index agent memories for search with qmd? [y/n]`, and Enter = yes. Yes sets up the collections and runs the first refresh through Track 159A's code. When qmd does not run, init prints the install command and skips the question.
3. After a yes, init asks `Also download qmd's embedding model (334 MB) for semantic search? [y/n]`, and Enter = no. Yes records the choice and starts `qmd embed` in the background.
4. Init shows the pointer line and its target files (`~/.claude/CLAUDE.md` and `CODEX_HOME/AGENTS.md`) and asks `Add this line? [y/n]`, with Enter = no, because it writes to files mm does not own. Yes appends the line once to each regular file inside the home folder, and a rerun adds no second copy. For a symlink, or a file that resolves outside the home folder, init prints the line and the resolved path and changes nothing.
5. A join asks these questions with the others before it waits and keeps the answers in the saved join answers (O2.5). A resumed join reuses them.
6. The Future test gap placed below is closed: the bootstrap's Arch install paths.
7. README's install and setup sections and docs/invariants/init-devices.md (the init questions) describe the new steps.
_touches: bin/bootstrap, src/mind_meld/initflow.py, src/mind_meld/memoryindex.py, tests/test_bootstrap.py, tests/test_initflow.py, docs/invariants/init-devices.md, README.md_
_blocked-by: Track 159A_
_read-first: docs/designs/cross-model-memory.md (Design 5 and 7), docs/SPEC.md O2.5 and O9, docs/invariants/init-devices.md (the join's saved answers, the `[y/n]` helper), bin/bootstrap (`ensure_syncthing_at_login` and the install steps), src/mind_meld/initflow.py (`init`, `_prompt_sources`)_

_Source: the developer's decisions of 2026-11-20 and 2026-11-21 (docs/SPEC.md O9) and the brief: "Setup facilitates installing qmd the way bin/bootstrap and mm init facilitate Syncthing (check qmd 2.8.3 runs on this Arch box ... note its Bun/Node and embedding-model needs)". Verified 2026-11-20 on `linux-a`. qmd 2.8.3 is the npm package `@tobilu/qmd` (engines `node >=22.0.0`), installed with Bun's global install and linked from `~/.local/bin/qmd`. Node 26 and Bun 1.4 are present. No qmd model had been downloaded. The default models are 334 MB (embedding), 639 MB (reranker) and 1.28 GB (query expansion), sizes read from Hugging Face. Both global instruction files on `linux-a` are symlinks into the owner's config repository. [challenge] The pointer-line question defaults to no although the line was recommended, because it writes to files mm does not own; the developer can overrule this in review._

- **Install qmd from the bootstrap script** -- add the qmd step after Syncthing, with the Bun, npm and Node branches and the failure path. Test each branch in tests/test_bootstrap.py and document it in README. _bin/bootstrap, tests/test_bootstrap.py, README.md, ~120 lines._ (M)
- **Ask the index, embeddings and pointer-line questions in `mm init`** -- add the three questions with their defaults, the saved-answer handling, the idempotent append and the symlink print-only path. Test in tests/test_initflow.py and update docs/invariants/init-devices.md. _initflow.py, memoryindex.py, tests/test_initflow.py, docs/invariants/init-devices.md, ~180 lines._ (M)
- **Test gap: bootstrap Arch install paths** -- assert that a non-root Arch run sends pacman through sudo, then cover a root run, a failed pacman install, the pacman-missing stop and a rerun on Arch. _Source: Future entry of the same title (docs/TODOS.md `[review]` test gap, Track 98A)._ _tests/test_bootstrap.py, ~40 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of document order; document order is priority, not gating.

Adjacency list (from the packer / GROUP_DEPS — not document order):
```
- Group 155 ← {}
- Group 156 ← {155}
- Group 157 ← {155}
- Group 158 ← {155, 156}
- Group 159 ← {158}
- Group 160 ← {159}
```

Track detail per group:
```
Group 155: Fixes from the deepsec scan ∥ cross-model memory qualification   (in progress)
  +-- Track 155A ........... ~L . 3 tasks
  +-- Track 155B ........... ~M . 1 task

Group 156: Observed sync and setup bugs
  +-- Track 156A ........... ~L . 5 tasks

Group 157: Pull waits for Syncthing delivery
  +-- Track 157A ........... ~L . 4 tasks

Group 158: Per-machine Codex memory copies
  +-- Track 158A ........... ~L . 3 tasks

Group 159: Local memory index
  +-- Track 159A ........... ~L . 2 tasks

Group 160: qmd setup and the pointer line
  +-- Track 160A ........... ~L . 3 tasks
```

**Total: 6 groups . 7 tracks remaining.**

Tracks 135A to 154A shipped in v1.12.51 to v1.12.73, and Track 135B (Track 124B's real-machine automatic-update observation) merged as a documentation-only change between them (independent receipts in shipped history). IDs 95A and 96A stay unused because Future entries and old records cite them; the qualification formerly carded as 96A returns as Track 155B.

---

## Future

Deferred: docs/roadmap-future.md (103 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
