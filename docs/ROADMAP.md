# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Cursor, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support was removed in v1.11.8; Groups 82–90 and 94 (resumable capture, affected-test selection, the status, Codex, pull, init and source-command moves, one capture budget, Syncthing-tolerant reads, explicit fleet creation and join, machine-local gstack files, shared Claude project memory) shipped in v1.11.9–v1.12.1 and live in shipped history; Grok models used through Cursor stay in the Grok row. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); Group 96 qualifies it after the cutover.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations, standalone Grok Build support (2026-10-19) and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision.
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed (spec, 2026-10-18).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`. Invite codes, mesh healing, relays, folder configuration through Syncthing's API and scan requests are refused until the spec's revisit trigger is observed.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line (spec O8, 2026-10-19).** A Track that adds output to push, pull or the auto commands must show it is the outcome, a failure with its fix, data at risk or an actionable warning; everything else goes to `mm status`, `mm diag` or `--verbose`.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

Declared active (factory, 2026-11-01): Track 97A is in flight as factory item 9dd967eb on branch `mmm/9dd967eb`, with no PR yet. Its ID, Group and card are pinned.

#### Group 97: Recapture budget

_Depends on: none_

**This group delivers:** `mm recapture` walks git with its own budget, so its recovery works on busy repositories.

##### Track 97A: Give mm recapture a budget that can walk busy repositories
_1 task . ~80 LOC . low risk . 5 files_
**Outcome:** `mm recapture` recovers the owner's busiest repositories instead of skipping them on timeout, so the recovery command `mm status` prints works; push and autopush behave as they do today.
**Supports:** docs/SPEC.md retro reporting; the recovery line `mm status` prints when a git walk drops repositories.
**Done when:** `mm recapture` and `mm recapture --dry-run` walk git with their own budget, at least 30 s per repository and at least 120 s for the walk, set from one named constant pair in `events.py`; `walk_git_projects` no longer applies `PER_REPO_TIMEOUT_CAP_MS` to that budget; push and autopush keep their current walk budgets and per-repository cap unchanged, pinned by a test; a repository that still exceeds the recapture budget is reported as skipped with its reason and exit code as today; a test proves a repository slower than the old 2 s cap is captured by recapture and is still skipped by the push walk; README's "Dropped repositories" section and `docs/invariants/events-retro.md` describe the new budget and drop the claim that the preview runs at interactive budgets; `./bin/check --affected` passes.
_tier: routine_
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/test_events.py, README.md, docs/invariants/events-retro.md_
_out: 91A, 91B, 98A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: manager Ephesus 7's probe on 2026-11-01 at 33e91a1 on v1.12.1. `mm recapture --dry-run` skipped boltmail, gstack-extend, mind-meld and mmm-factory with reason `timeout` and captured 187 commit records over 30 days; `mm recapture 7d --dry-run` skipped only mmm-factory and captured 367. `events_tail._prepare_recapture` passes `WALK_TIME_BUDGET_INTERACTIVE_MS` (500), which `walk_git_projects` turns into about 333 ms per repository over 12 repositories, capped at `PER_REPO_TIMEOUT_CAP_MS` (2000). The same `git log --all --numstat -M -C` over 30 days, run alone, took 1.67 s on mmm-factory, 1.37 s on mind-meld, 1.05 s on gstack-extend and 0.26 s on boltmail, at load about 3._

_Launch note (manager Ephesus 7, 2026-11-01): the budget floors are the manager's, chosen so a 90-day window on this Mac fits with room to spare; they are not measured at 90 days. After release, the manager's check is `mm recapture --dry-run` on this Mac showing no `timeout` skips. Authorization: Karl, 2026-11-01, "go", on the recommendation to give recapture a limit in seconds and leave push's limits alone._

- **Recapture's own walk budget** -- _src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/test_events.py, README.md, docs/invariants/events-retro.md, ~80 lines._ (S)

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 94_

### Phase 5: mm 2.0 on Syncthing

**End-state:** The owner's Macs and two Linux machines sync over one Syncthing folder that carries the 1.x store, with fast-forward pulls, propagated deletions, Syncthing state in status, a bootstrap script and a cutover runbook; the factory can hand over between Macs; docs/SPEC.md O1 to O4 are accepted on the owner's own observation after following the runbook, and released as 2.0.0.
**Groups:** 91, 92, 93, 95, 98

#### Group 91: Fast-forward pulls ∥ Honest usage remedy

_Depends on: Group 97_

**This group delivers:** Peer edits fast-forward, conflicts need two real edits, deletions propagate, and `mm status` stops prescribing an update that cannot help.

##### Track 91A: Fast-forward peer edits and propagate deletions
_3 tasks . ~650 LOC . high risk . 16 files_
**Outcome:** A peer's change to a file the owner has not touched simply applies, a conflict appears only when both machines changed the file, deleted `MEMORY.md` lines stay deleted, and a file deleted on one machine is deleted on the others with a 30-day recovery copy.
**Supports:** MVP-1, docs/SPEC.md O1.1.
**Done when:** a `state.db` migration adds a per-peer last-synced base (`peer, source, path, sha`); `_apply_incoming_file` skips when the remote equals the base, fast-forwards with no union when the local file equals the base, and unions a mergeable file or writes a conflict copy only when both changed, falling back to today's rule when no base exists; a peer tombstone deletes the local file when it still equals the base and keeps the bytes recoverable for 30 days; bases are recorded after applied, matched and deleted outcomes, never after failed or waiting ones; `pullplan` predicts the same; `conflicts.md` and `sync.md` describe the rule; tests cover each branch and a sequence across a Mac and two Linux machines that share a home, so same-home and shared-name delivery both run; a push from a device whose home changed deletes nothing on a peer: `test_a_changed_home_mints_no_tombstone_and_settles` stays and a new test carries that push through a peer's pull; the plan states whether an older peer's old-form tombstone may delete, since once this ships a same-home older peer's tombstone can delete a local file that still equals its base, with 30-day recovery, and nothing guarantees older machines are gone; a test pins the choice; a conflict copy already kept is not a new outcome: when `_apply_conflict` finds its sidecar already holds the peer's bytes, pull counts no applied file and no conflict and repeats no warning, the file stays visible in `mm conflicts`, and a test pins a second pull's single outcome line (O8); with no base the fallback keeps today's rule, so a conflict that exists when this ships does not clear by itself, and README says the owner runs `mm resolve` on each, after which a test shows that file stops conflicting; `sync.md`'s first-switch limits and the re-check list below each state what they become when tombstones delete, and any that would delete bytes the fleet still wants is fixed or put to the owner before merge; losing the base has a recovery, and README's History database move-aside remedy, which expires when this Track stores a base, says it; `tests/test_pullflow.py`'s mtime and tombstone characterization tests move to the new rule; the recovery-copy writer calls `fsutil.refuse_real_state_write_under_pytest` before its first mutation and joins the raw-writer census; the provisional `state.db` tables each name a reader or an explicit no-reader, and the pre-2026-10-19 Track IDs in `sync.md`'s history-log section and `statedb.py` are retired (including `sync.md`'s deferred hold-escalation pointer, killed 2026-10-21 as unobserved); `./bin/check` passes.
_touches: src/mind_meld/statedb.py, src/mind_meld/pullflow.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, src/mind_meld/retention.py, tests/test_statedb.py, tests/test_merge.py, tests/test_additive_sync.py, tests/test_integration.py, tests/test_fsutil.py, tests/test_pullflow.py, tests/test_projectid.py, tests/test_conflict_copy.py, docs/invariants/conflicts.md, docs/invariants/sync.md, README.md_
_blocked-by: Track 87A, Track 90A, Track 97A_
_out: 92A, 93A, 98A_
_read-first: docs/invariants/README.md, docs/invariants/conflicts.md, docs/invariants/sync.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_statedb.py tests/test_merge.py tests/test_additive_sync.py tests/test_integration.py tests/test_pullflow.py tests/test_projectid.py tests/test_conflict_copy.py_

_Source: docs/archive/store-v2.md hazard rows "Every peer edit to an untouched file is a conflict" and "Line deletions in `MEMORY.md` and `.jsonl` never stick", re-checked at 2a101d6: `_apply_incoming_file` union-merges every update of a mergeable file, then keeps a newer local file or writes a conflict copy; pull never deletes local files (tombstones only stop re-download); `statedb` has an unused `ledger` table and no base. Home change: a plan note on the unapproved item 790729af said a changed home mints tombstones for every marked file. PR #245 closed that on the push side (`moved_folders` in `manifest_from_wire`, and the test above); what remains is proving it at the point where a tombstone starts to delete. Older peers: Track 90A plan c85d0d67, "After Track 91A" and Gate notes. Kept conflicts: observed by manager Ephesus 7 on 2026-11-01 on device 3771H-kb-ms, where every `mm pull` prints `Pull: 14 files applied (14 conflict copies)` and 14 warnings with nothing changing on disk; read at 2a101d6, not reproduced here: `_apply_conflict`'s dedup return gives `"conflicted"` with no write, and `PullResult.total_applied` adds `total_conflicted`._

_Re-check list, moved here from docs/TODOS.md on 2026-11-01 (Karl, 2026-10-31: 91A starts only after 1.12.0 has run on the owner's real store, with these re-checked then). (1) With `~/.claude/projects` missing and no push during the outage, whatever is still absent when it returns, including a peer's undelivered blob, mints a tombstone at the next push. (2) A push that loses `projects/` between the manifest load and the scan mints for every file it cannot see, identity-wide on a switched machine. (3) A file deleted on the old form, in a folder already gone at the switch, can return on any machine owning the project for its 30 days, and that machine republishes it. (4) A linked worktree's folder nominating a removed main folder makes its records identity-wide, and a live folder's old-form record publishes under whatever origin sits at that path at the switch. (5) The post-push GC gate only delays the sweep: a later listed push collects a dropped file's blobs with no tombstone, and explicit `mm gc` has no gate._

- **Base table and the fast-forward rule** -- _src/mind_meld/statedb.py, src/mind_meld/pullflow.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, tests/test_statedb.py, tests/test_merge.py, tests/test_additive_sync.py, tests/test_pullflow.py, tests/test_conflict_copy.py, ~350 lines._ (L)
- **Propagated deletions with recovery** -- _src/mind_meld/pullflow.py, src/mind_meld/retention.py, tests/test_integration.py, docs/invariants/conflicts.md, docs/invariants/sync.md, README.md, ~180 lines._ (M)
- **Deletion safety checks** -- _tests/test_projectid.py, tests/test_integration.py, docs/invariants/sync.md, ~120 lines._ (S)

##### Track 91B: Stop telling the owner to update when the newest mm cannot read a host's records
_1 task . ~90 LOC . low risk . 3 files_
**Outcome:** When a host's records have a format this mm cannot read and no newer release is known, `mm status` and the push usage line say updating will not help and give the step that does; `mm update` stays the remedy when a newer release exists.
**Supports:** MVP-1, docs/SPEC.md O8.1 (a failure names the command that fixes it) and O7.3.
**Done when:** `statusflow._usage_capture_remedy`, `mm status`'s `Usage capture needs a newer mm` verdict and `events_tail`'s `run mm update before refreshing usage` read the cached upgrade view without refreshing it; a cached newer release, or none known, keeps `mm update`; this release being the newest known names the reader, says no release reads its format yet and gives the existing stop remedy and `mm diag`; `mm status` stays read-only; `tests/test_diag.py` pins all three cases; `./bin/check --affected` passes.
_touches: src/mind_meld/statusflow.py, src/mind_meld/events_tail.py, tests/test_diag.py_
_blocked-by: Track 97A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md_
_session: fresh · effort: low · verify: ./bin/check --affected_

_Source: observed 2026-11-01 on this Mac at mm 1.12.1, the newest tag, with Muse Code 1.4.14: `mm diag --json` shows `muse.last_reason: unsupported` since 2026-10-21, and `mm status` prints `Usage refresh: Run mm update; a consented reader has an unsupported record format.` `host_usage.PERMANENT_REASONS` holds only `unsupported`, and `_usage_capture_needs_upgrade` reads no version. Making the Muse reader read those records is a separate item in docs/TODOS.md._

- **Remedy follows the cached newest release** -- _src/mind_meld/statusflow.py, src/mind_meld/events_tail.py, tests/test_diag.py, ~90 lines._ (S)

#### Group 98: Bootstrap script

_Depends on: Group 91, Group 97_

**This group delivers:** One script readies a fresh Mac or Arch-based machine with Python, pipx, mm and the Syncthing package.

##### Track 98A: Add a bootstrap script that readies a machine for mm and Syncthing
_2 tasks . ~250 LOC . medium risk . 3 files_
**Outcome:** On a fresh Mac or Arch-based Linux machine, the owner runs one script and has Python, pipx, mm and the Syncthing package installed, then follows its printed next steps to pair machines, share the folder and run `mm init`.
**Supports:** MVP-1, docs/SPEC.md Constraints (the bootstrap script) and the 2.0.0 release line; Track 93A's runbook uses it.
**Done when:** `bin/bootstrap` on macOS stops with Homebrew's install instructions and a non-zero exit when `brew` is missing, and otherwise installs what is missing through Homebrew; on Arch-based Linux it installs what is missing with `pacman -S --needed`; any other system stops and names what is supported; it confirms Python 3.11 or newer and Syncthing 2.1 or newer, installs mm through pipx from `@latest` unless pipx already has it, and stops with the fix when `mm` on `PATH` is not pipx's copy; a rerun changes nothing and says so; it pairs no machine, shares no folder and writes no Syncthing configuration; success ends with those next steps, and a failure's last line says what broke and the one command that fixes it (O8); tests run it with fake `brew`, `pacman`, `pipx`, `python3`, `syncthing` and `mm` on `PATH` under a `tmp_path` home and cover macOS without Homebrew, macOS, Arch, an unsupported system, a rerun, a too-old Python or Syncthing and a shadowed `mm`; README's install section gives the one command that fetches and runs the script from the `latest` tag and says pairing and sharing stay manual; `./bin/check --affected` passes.
_touches: bin/bootstrap (new), tests/test_bootstrap.py (new), README.md_
_blocked-by: Track 97A_
_out: 92A, 93A_
_read-first: docs/SPEC.md, README.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Karl, 2026-11-01: "we need a bootstrap script before 2.0, so we should make a track for that"; docs/SPEC.md Constraints and supported conditions at 2a101d6 (Python 3.11+, Syncthing 2.1 or newer). Re-checked at 2a101d6: no `bin/bootstrap` exists; `upgrade.INSTALL_SPEC` and README install pipx from `@latest`; README's Linux section names Arch's `python-pipx`. The shadowed-`mm` stop comes from a stale `/opt/homebrew/bin/mm` that has shadowed pipx's copy on the owner's Macs. Keyring packages stay in README's Linux section: the spec's bullet does not list them, and unlocking the keyring is an accepted manual step._

- **Bootstrap script and its tests** -- _bin/bootstrap, tests/test_bootstrap.py, ~200 lines._ (M)
- **README install path** -- _README.md, ~50 lines._ (S)

#### Group 92: Syncthing status

_Depends on: Group 91, Group 98_

**This group delivers:** Status shows Syncthing's state and each machine's latest push.

##### Track 92A: Report Syncthing state and each machine's latest push
_2 tasks . ~370 LOC . medium risk . 10 files_
**Outcome:** `mm status` tells the owner whether Syncthing is connected and in sync and whether this machine has applied each peer's latest push, and `mm diag` flags folder settings that would break sync.
**Supports:** MVP-1, docs/SPEC.md O2.2 and O3.2.
**Done when:** a new `syncthing.py` finds the local Syncthing configuration and reads its REST API read-only with timeouts (connected peers, folder state, and the folder options README lists); mm never writes Syncthing's configuration; pull records each peer's applied manifest time, and status shows each peer's last push and whether it is applied, warning in every mode when something has waited more than 24 h, not counting unplaced or old-form files, which are not pending; a published peer's manifest that has not arrived and an attended waiting count show in status; diag lints the folder options and `.stignore`; a missing or unreachable Syncthing is reported, not an error; tests use a fake REST server; `./bin/check` passes.
_touches: src/mind_meld/syncthing.py (new), src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/statedb.py, tests/test_syncthing.py (new), tests/test_diag.py, tests/test_module_boundaries.py, README.md, docs/invariants/README.md, docs/invariants/sync.md_
_blocked-by: Track 77A, Track 91A, Track 98A_
_out: 93A, 95A_
_read-first: docs/invariants/README.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_syncthing.py tests/test_diag.py tests/test_module_boundaries.py_

_Source: docs/SPEC.md O2.2 and O3.2; docs/archive/store-v2.md §10 folder options (current evidence; Syncthing option semantics verified against docs.syncthing.net on 2026-10-11 for the former Track 81A). Track 90A's Gate notes and `sync.md` exclude unplaced and old-form counts from O3.2 and leave the peer manifest and attended waiting count to this Track. `.stignore` is linted because no real machine gets it from `mm init` at cutover (Track 89A follow-up FU1)._

- **Read-only Syncthing client and diag lint** -- _src/mind_meld/syncthing.py, src/mind_meld/statusflow.py, tests/test_syncthing.py, tests/test_module_boundaries.py, docs/invariants/README.md, ~200 lines._ (M)
- **Each peer's push in status** -- _src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/statedb.py, tests/test_diag.py, README.md, ~150 lines._ (M)

#### Group 95: Factory handover

_Depends on: Group 92_

**This group delivers:** An opt-in `mmm` source carries mmm-factory's durable state between Macs.

##### Track 95A: Add an opt-in mmm source for factory handover
_2 tasks . ~220 LOC . medium risk . 7 files_
**Outcome:** After `mm enable-source mmm` on each Mac, the owner stops the factory on one Mac, pushes, pulls on another Mac and starts it there with the items, plans, cards, settings, passports, handoff notes, briefs and run ledgers in place; locks, watcher state, fields files, worker-ID lists, usage caches and other machine-local factory files stay behind.
**Supports:** docs/SPEC.md "What mm syncs and reports" mmm-factory row and [factory handover](SPEC.md#factory-handover) (owner decisions 2026-10-29).
**Done when:** a default `mmm` source on `~/.mmm` is opt-in like Muse (never active until `mm enable-source mmm` on that machine, or an explicit choice at `mm init` or `mm reconfigure-sources`); it syncs `items/` (each item's `<id>.json`, `.plan.md` and `.card.md`), the four run ledgers (`provenance.jsonl`, `hangs.jsonl`, `records.jsonl`, `picker.jsonl`) and, per project, `manager.toml`, the passport (`manager-passport.txt`), the handoff note (`driver-handoff.md`) and its timestamped archives, the manager's launch cards (`cards/`) and `briefs/`; it never syncs `*.lock`, `manager-watch.*`, `manager-status.json`, `manager-fields.json`, `manager-usage.json`, the worker-ID lists (`manager-docs-workers.txt`, `release-text-workers.txt`), the worker-side `mmm-<id>-card.md` and `mmm-<id>-autoplan-<n>.md` copies, SQLite files, `traces/`, `release/`, `review-compare/` or `picker-jev/`, and `~/.local/share/mmm-factory/` and `~/.config/mmm-factory/` are never sources; `~/.mmm` must be a real directory (a link is refused, as the factory requires); README documents enabling it, the single-Mac rule, that in-flight work does not transfer (finish or ship first), and that item records carry raw worker text to every machine with the source enabled; tests pin the allowlist against a fixture factory home; `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_sourceflow.py, tests/test_mmm_source.py (new), docs/invariants/sync.md, README.md_
_blocked-by: Track 90B, Track 92A, Track 94A_
_out: 93A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-10-29, including mmm-factory manager Ugarit's recommendation the owner accepted (exclude `traces/`, `release/`, `review-compare/` and the worker-ID lists; add `cards/`; `briefs/` per option (a), which the owner confirms at the PR that adds this card and which needs mmm-factory's managers to keep handoff briefs under `~/.mmm/projects/<slug>/briefs/`). Paths confirmed against mmm-factory's `docs/agent-runbook.md` "The factory home" at 93cf70a (v0.5.29) and its file-name constants (`manager.toml`, `manager-passport.txt`, `driver-handoff.md`, `manager-fields.json`, `manager-watch.json`, `manager-watch.owner`, `manager-watch.lock`, `manager-status.json`, `manager-usage.json`, `release-text-workers.txt`). Factory attribution in `retro-fleet` stays deferred (Future)._

- **The mmm source and its allowlist** -- _src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_mmm_source.py, ~150 lines._ (M)
- **Handover docs** -- _README.md, docs/invariants/sync.md, tests/test_sourceflow.py, ~70 lines._ (S)

#### Group 93: Cutover runbook

_Depends on: Group 91, Group 92, Group 95, Group 98_

**This group delivers:** A runbook and agent docs for moving the fleet onto Syncthing.

##### Track 93A: Write the cutover runbook and the Syncthing documentation
_3 tasks . ~450 LOC . low risk . 11 files_
**Outcome:** The owner has a step-by-step runbook to move the fleet from iCloud Drive to Syncthing and bring both Linux machines in, and every document an agent reads describes the Syncthing store.
**Supports:** MVP-1, docs/SPEC.md O1 to O4 (the cutover, and the owner's observation after it).
**Done when:** README covers installing on macOS and Arch with the bootstrap script, pairing, the folder options mm relies on, how `~/Sync/mind-meld` relates to Syncthing's Default Folder at `~/Sync`, and a cutover runbook: update every machine in any order (there is no fleet order since 1.12.0), except that a machine at 0.14.18 or older upgrades before its next attended `mm push` or `mm gc`, or stays off; stop hooks; move the store folder from iCloud Drive into the Syncthing folder; point `storage.path` at it; put mm's temporary-file line in `.stignore` on every machine, because the cutover never runs `mm init --new-fleet` and Syncthing does not sync that file; bring in each of the two Linux machines one at a time (bootstrap, pair, accept the folder, unlock the keyring before hooks run, `mm init`); retire a dead machine by moving its device entry aside as README's "Retiring a dead machine" says, never by deleting its directories and never while a machine at 1.7.1 or older can run; delete the iCloud folder as the runbook's last step, once every machine has joined and reports in sync; and an optional time-boxed rehearsal between a Linux machine and a Mac with a throwaway folder, temp `HOME` and throwaway passphrase, running `mm init` before and after delivery; AGENTS.md Key Principles and root SPEC.md's storage sections describe the Syncthing folder; `sync.md` and `init-devices.md` drop iCloud-only rules; the pre-2026-10-19 Track IDs in the routing README, `init-devices.md` and `tests/test_pullhistory.py` are retired; root SPEC.md's `mm sources` steps and `sync.md`'s config-patch row (`_set_grok_host_usage` no longer exists), "all configured sources" sentence and `has_explicit_sources` wording match the code; README and `.github/workflows/ci.yml` stop citing a soak; `pyproject.toml` description and keywords drop iCloud; `tests/test_version.py` fails when `VERSION` and `pyproject.toml` differ; docs/SPEC.md's superseded-history entry for the store design names §13 (cutover) as evidence alongside §7, §10 and §11; `./bin/check` passes.
_touches: README.md, AGENTS.md, SPEC.md, docs/SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, tests/test_version.py, tests/test_pullhistory.py, .github/workflows/ci.yml_
_blocked-by: Track 89A, Track 91A, Track 92A, Track 95A, Track 98A_
_out: 96A_
_read-first: docs/SPEC.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py tests/test_version.py_

_Source: docs/SPEC.md O1 to O4 and the accepted manual cutover step; docs/archive/store-v2.md §11 (Linux) and §13 (cutover) as evidence. Runbook order and the retire and upgrade lines: Track 90A plan c85d0d67 Gate notes, its `[challenge]` C1 (Karl, Option A) and README's "Retiring a dead machine". `.stignore`, the Default Folder note and the rehearsal: Track 89A review record FU1, FU4 and FU7. Stale docs: PR #246 review receipt, re-checked at 2a101d6 (`rg _set_grok_host_usage` matches only `sync.md`). Drift check: Karl, "sure", 2026-10-28; at 2a101d6 `tests/test_version.py` checks that no source reads `VERSION` and nothing compares it with `pyproject.toml`._

- **Runbook and Syncthing setup** -- _README.md, ~270 lines._ (M)
- **Agent-facing docs** -- _AGENTS.md, SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, .github/workflows/ci.yml, ~170 lines._ (M)
- **Version drift check** -- _tests/test_version.py, ~20 lines._ (S)

#### Group 96: Cross-model memory qualification

_Depends on: Group 93_

**This group delivers:** Two live checks prove or refute the cross-model memory design before implementation.

##### Track 96A: Qualify cross-model memory in isolated homes
_1 task . ~120 LOC . low risk . 1 file_
**Outcome:** The owner knows whether the approved cross-model memory design works with the current Codex and Claude Code before any implementation is carded: fresh sessions follow a "search, don't read" pointer to the other agent's memory, and per-machine Codex copies do not churn sync.
**Supports:** docs/SPEC.md O9.4 (qualification before implementation), after 2.0.
**Done when:** `docs/designs/cross-model-memory.md` records Q1 and Q2 run in temporary `HOME` and `CODEX_HOME` directories (never the owner's real stores), with the observed codex-cli and Claude Code versions; Q1: a fresh Codex session finds a Claude-origin memory through an `AGENTS.md` pointer and a fresh Claude session finds a Codex-origin memory through a `CLAUDE.md` pointer, by search, without loading `MEMORY.md` whole, and which file Codex loads at startup is recorded; Q2: after Codex regenerates its memory files on one machine, a push publishes only that machine's copies and a peer applies them with no conflict copy and no write into any native store; the chosen Codex file set is recorded; a failed check is written up for the owner and no implementation Track is proposed; no product code changes.
_touches: docs/designs/cross-model-memory.md_
_blocked-by: Track 93A_
_read-first: docs/designs/cross-model-memory.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py_

_Source: owner decision 2026-10-29 ("yes, I want cross-model memory", post-2.0, qualification card after the cutover); probe `~/.gstack/projects/kbitz-mind-meld/cross-model-memory-probe-20261029.md` on codex-cli 0.164.0._

- **Run Q1 and Q2 and record them** -- _docs/designs/cross-model-memory.md, ~120 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 97 ← {}
- Group 91 ← {97}
- Group 98 ← {91, 97}
- Group 92 ← {91, 98}
- Group 95 ← {92}
- Group 93 ← {91, 92, 95, 98}
- Group 96 ← {93}
```

Track detail per group:

```
Group 97: Recapture budget          (in progress)
  +-- Track 97A ........... ~S . 1 task
Group 91: Fast-forward pulls ∥ Honest usage remedy
  +-- Track 91A ........... ~L . 3 tasks
  +-- Track 91B ........... ~S . 1 task
Group 98: Bootstrap script
  +-- Track 98A ........... ~M . 2 tasks
Group 92: Syncthing status
  +-- Track 92A ........... ~L . 2 tasks
Group 95: Factory handover
  +-- Track 95A ........... ~L . 2 tasks
Group 93: Cutover runbook
  +-- Track 93A ........... ~L . 3 tasks
Group 96: Cross-model memory qualification
  +-- Track 96A ........... ~M . 1 task
```

**Total: 7 groups . 8 tracks remaining.**

Tracks 97A, 91A, 98A, 92A, 95A and 93A all edit `README.md`, so the packer emits a chain with one parallel pair (91A ∥ 91B). Track 98A follows 91A only because of that shared file; nothing in the script depends on 91A's behaviour. Track 97A leads because it is already in flight, which is why 91A and 98A carry an order-only edge on it, and 92A waits on 98A for the same reason, so the script lands early. Track 95A lands before the cutover runbook (93A) so the runbook can cover factory handover, and 98A lands before it so the runbook can use the script. Track 77A stays on Track 92A; the shipped Tracks 87A, 89A, 90A, 90B and 94A stay on the cards that named them. Track IDs 91A to 96A keep the meaning they had before this regeneration, so tests, README and invariant docs that cite them stay correct; the new Tracks take IDs that were never used (97A, 98A, 91B), which is why Group numbers do not follow execution order.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on their own observation on one Mac and both Linux machines after following Group 93's runbook, with O6 to O8 holding, the bootstrap script shipped and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance`. Problems are fixed as they appear. Cross-model memory (O9) is qualified after the cutover by Group 96; its implementation waits in Future until that qualification passes.

---

## Future

Deferred: docs/roadmap-future.md (16 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
