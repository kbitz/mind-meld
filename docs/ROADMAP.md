# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Tracks 123A, 123B, 124A and 127A shipped (v1.12.31 to v1.12.35), and Track 124B shipped everything but its post-merge check on a real machine, which is now Track 125B. Track 125A is in flight. On 2026-11-10 the owner amended SPEC O8 ("go -- make sure all our manually run commands have consistently formatted output -- they all need to look like they come from the same tool"): push and pull end with one line per thing that happened, an update during push or pull is visible, and every hand-run command prints in one shared style (O8.1, O8.6). Tracks 129B, 130A, 130B and 133A carry that amendment; 129B also resolves the Future entry about the push outcome's usage suffix. The rest of Future stays deferred. The owner will decide the path to 2.0 after using the Syncthing fleet for a while. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

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
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) that Track 129B writes down.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

Track 125A is active on factory branch `mmm/f1aea97f` (item `f1aea97f`), declared 2026-11-10; it pins Group 125. Track 125B joins it in the same launch batch.

### Group 125: Scans resume, show progress and say "catching up" ∥ One automatic update observed on a real machine

_Depends on: none_

**This group delivers:** A scan that stops early resumes on the next attended push, a long attended push shows that it is working, and a push or `mm status` on that machine says "catching up (N left)" rather than "failed" or "dropped". The owner sees one of their machines update itself after a release through the release check Track 124B shipped.

#### Track 125A: Resume stopped scans, show progress, and word a deadline stop "catching up (N left)" on the push and in status
_2 tasks . ~350 LOC . medium risk . 9 files_
**Outcome:** An owner whose attended push stops a scan early (the cap in Track 124A, a very large store) sees the next push pick up where it stopped, sees a transient progress line while a push takes a while, and reads "catching up (N left)" in the push notice and in `mm status` instead of "failed", "skipped" or "dropped"; real failures still say failed.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O7.2 (unfinished work resumes on later pushes), O7.3 (usage mm cannot read is a named coverage gap, not a silent zero) and O8.1 (progress appears transiently; an actionable warning's fix works; a progress state is not a failure). The owner, 2026-11-09: "1 - sure" (the hold lifted for the Tracks carded here), on the manager's second Track whose outcome includes "showing progress when it takes a while" and "a run that stops early reports 'catching up (N left)' rather than 'failed' in push notices, `mm status` and the retro's MM_HEALTH".
**Done when:** the acceptance list below passes: each scan's early stop resumes on the next attended push, a long push on a terminal shows a transient progress line, and a deadline stop reads "catching up (N left)" in the push notice and `mm status` while errors still read failed.
_touches: src/mind_meld/events_tail.py, src/mind_meld/core.py, src/mind_meld/statusflow.py, tests/test_host_usage_snapshot.py, tests/test_statusflow.py, tests/test_silent_failure_contract.py, tests/test_integration.py, README.md, docs/invariants/events-retro.md_
_out: 131A_
_blocked-by: Track 124A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, src/mind_meld/core.py (`_SyncProgress`, `_sync_progress_phase`), src/mind_meld/events_tail.py (`_host_skip_phrase`, `_git_walk_degradation`), src/mind_meld/statusflow.py (the Codex "warming" line, `count_pending_repositories` use), src/mind_meld/hosts/codex.py (the `migrating` state and `pending`)_
_produces: resumable scans with a progress line and "catching up (N left)" wording on the push and in `mm status`_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: manager Ephesus 17's observations on mm 1.12.30 and retro-fleet run on `linux-a`, 2026-11-09 (Future entry "Retro-fleet reports a Codex reader that is still catching up as "failed" (retro-fleet item R2)", whose first direction lands here, with the retro's side in Track 131A). Verified at `e04cfa0`: `_host_skip_phrase` already separates reason `deadline` and prints "N of M rollouts cached" when `HostReadEvidence` is present, but its text still begins "host-usage snapshot skipped (... deadline)"; `_git_walk_degradation` words budget aborts and errors alike as "git walk dropped {n} repositories this push"; `statusflow` prints "Codex usage capture: warming — N rollouts not yet scanned" for `state: migrating`; `events.count_pending_repositories` gives the repositories left; `_SyncProgress` (`core.py`) is a transient, TTY-only progress display that clears before the outcome line, and `_push_core` already sets its "Recording activity" step before the tail; README's Fleet-retro paragraph states the per-repository resumption, which this Track proves per scan rather than assumes. Not re-observed here: the Codex reader's `files_cached` 852 of `files_on_disk` 1016 on `linux-a` (the manager's reading of `mm diag --json`)._

- **Prove each scan resumes, and show progress while a push takes a while** -- For each scan an early stop can interrupt (git walk per-repository cursors, root-discovery rotation, the Codex per-file cache and the other host readers' caches, the session walk's index), a test stops it partway and shows the next attended push continues from the stored state and finishes within three pushes; where one does not, fix it. When an attended push has run longer than a few seconds on a terminal, the existing transient `_SyncProgress` line names the step and a count (repositories walked, readers read); nothing remains on screen after it clears, and a push that finishes quickly shows none (O8.1). _src/mind_meld/events_tail.py, src/mind_meld/core.py, tests/test_host_usage_snapshot.py, tests/test_integration.py, ~200 lines._ (M)
- **Word a deadline stop "catching up (N left)" in the push notice and in `mm status`** -- A host reader stopped by its deadline, and a git walk stopped by its budget, read "catching up (N left)" with the count of rollouts, sessions, conversation databases or repositories left, where today's text says skipped or dropped; an error, a permanent reason or an unsupported format keeps its failure wording; README's coverage text follows. _src/mind_meld/events_tail.py, src/mind_meld/statusflow.py, tests/test_silent_failure_contract.py, tests/test_statusflow.py, README.md, docs/invariants/events-retro.md, ~150 lines._ (M)

**Acceptance:**

1. For the git walk, root discovery, the Codex reader, another host reader and the session walk, a test stops the scan partway and shows the next attended push continues from the stored state and finishes within three pushes; a scan that did not resume is fixed in this Track.
2. On a terminal, an attended push that runs longer than the progress delay shows a transient line with the step and a count that clears before the outcome line; a push that finishes inside the delay, a push with output piped and a `--verbose` run leave no extra line; tests pin the three cases (O8.1 unchanged: one outcome line).
3. A Codex reader stopped by its deadline with items left reads "catching up (N left)" in the push notice and in `mm status`, with N matching `mm diag`'s `host_usage.codex.pending`; a reader that errored, hit a permanent reason or reads an unsupported format still reads as a failure; tests pin each.
4. A git walk stopped by its budget with repositories left reads "catching up (N left)" with N matching `events.count_pending_repositories`; a walk error still reads as an error; tests pin both.
5. `mm autopush` and `mm autopull` produce no new output and keep recording their `degraded` breadcrumbs with the wording the invariants require; tests pin it.
6. README's coverage text and `docs/invariants/events-retro.md` match the new wording.
7. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

#### Track 125B: Observe one automatic update on a real machine (Track 124B's post-merge check)
_1 task . ~10 LOC . low risk . 1 file_
**Outcome:** The owner has seen one of their own machines, on a tracking install one release behind, update mm on its next push or autopush with no `mm update`, using the credential-based release check Track 124B shipped (v1.12.34).
**Supports:** MVP-1 on Syncthing; docs/SPEC.md Constraints ("Auto-update upgrades a pipx `@latest` install at the end of its next `push`, `pull`, `autopull` or `autopush`, at most once a day"). Track 124B's acceptance criterion 9, its only open obligation.
**Done when:** the observation (`mm --version` before and after, the `upgrade-state.json` fields and `mm status`, placeholders only) is recorded as Track 124B's post-merge row.
_touches: docs/invariants/auto-upgrade.md_
_session: fresh · effort: low · verify: ./bin/check --affected_

_Source: Track 124B (PR #307, merged 2026-11-10 at `ea8215b`, v1.12.34) met acceptance criteria 1 to 8; the PR names criterion 9 "the explicitly post-merge real-machine observation". Observed 2026-11-11 01:55 UTC on `linux-a`: `auto-update.log` records an automatic `pipx upgrade` from 1.12.30 to 1.12.35, run by 1.12.30, which predates 124B's check, so it does not satisfy criterion 9. The next release after 1.12.35 gives the first chance for 124B's own code to do it._

- **Record the observation** -- After the next release, on one machine on a tracking install one release behind, let its next push or autopush update mm, and record `mm --version` before and after, the `upgrade-state.json` fields and `mm status` (placeholders only) as Track 124B's post-merge row; if it does not update, file the cause in TODOS.md. _docs/invariants/auto-upgrade.md only if the observation contradicts it, ~50 lines._ (S)


## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 127_

### Group 128: Retro cost is honest

_Depends on: Group 125_

**This group delivers:** A retro prices the Claude 5.5 models and keeps an extrapolated rate out of its `≥` floor.

#### Track 128A: Price the Claude 5.5 models and keep an extrapolated rate out of the retro's `≥` floor
_2 tasks . ~300 LOC . medium risk . 6 files_
**Outcome:** An owner reading `mm retro-fleet` sees a Claude cost on `mac-b` and `linux-a` that agrees without a local override and whose `≥` really is a floor: the 5.5 Opus, Sonnet and Haiku models have their own rates, and a model still priced by family extrapolation is never counted into a `≥` figure at a rate that can overstate it. Saving a price override prints that it was saved.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O7.3 and O8.1 (a figure the owner relies on must mean what it says). The owner, 2026-11-09: "Anything that is a reproducible bug we actually encountered should be current"; observed as ≥$8,093 on `mac-b` and ≥$8,089 on `linux-a` against ≥$4,220 after `mac-b` set local rates.
**Done when:** the acceptance list below passes: the three 5.5 ids are priced from a dated vendor reading, a `≥` figure contains no extrapolated rate, and a saved override prints one confirmation line without touching stdout.
_touches: src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_token_usage.py, tests/test_retro_fleet_aggregator.py, tests/test_retro_fleet_cli.py, docs/invariants/events-retro.md, README.md_
_blocked-by: Track 127A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md ("Pricing", the rate-marker rules), README.md ("Pricing refresh and local overrides"), src/mind_meld/token_usage.py (`VERIFIED_MODEL_IDS`, `MODEL_FAMILY_TIERS`, `PRICING_LAST_UPDATED`, `floor_prices`, `update_pricing_cache`), src/mind_meld/skills/retro_fleet/aggregator.py (`_agent_row_cost`, `_extrapolation_notes`, the `--set-model-price` handling in `main`)_
_produces: a retro whose `≥` cost excludes extrapolated rates, with the 5.5 models priced and overrides confirmed_
_session: fresh · effort: high · verify: ./bin/check --affected_

_Source: manager Ephesus 17's retro-fleet comparison of `mac-b` and `linux-a`, 2026-11-09 (Future entry "Cost floor (`≥`) is computed from family-extrapolated Claude rates that can overstate it (retro-fleet item R1)", and the confirmation sentence of "Local price overrides cannot express rate tiers, confirm nothing, and one stale vendor holds the whole refresh (retro-fleet items R6, R7, R8)"), promoted on the owner's rule above. Verified at `e04cfa0`: `VERIFIED_MODEL_IDS` in `token_usage.py` lists `claude-opus-5` and `claude-sonnet-5` and no 5.5 id (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`), so those resolve through `MODEL_FAMILY_TIERS` and the aggregator reports them as `pricing_extrapolated`; `PRICING_LAST_UPDATED` is 2026-09-14; docs/invariants/events-retro.md defines `≥` as "a floor of a priced subtotal under bundled assumptions" and says estimates "may extrapolate". The owner's reading of the vendor page for the 5.5 models, the rates `mac-b` set (not re-read here): Opus 5.5 $4 in / $0.20 cache read / $5 five-minute write / $20 out; Sonnet 5.5 $2 / $0.10 / $2.50 / $10; Haiku 5.5 $0.10 / $0.01 / $0.125 / $0.50 per million tokens. The implementer reads the vendor page itself, records its date, and stops to ask if it cannot, rather than copy these numbers; the one-hour write rates the pricing basis uses for estimates come from the same page. In `aggregator.py`'s `main`, `token_usage.update_pricing_cache(...)` runs for `--set-model-price` and its success path prints nothing (only the `OSError` path writes to stderr). Deliberately left in Future: rate tiers in overrides (cache-write durations, long-context cards) and the per-vendor verification date, which are design questions or the manager's unverified claims, and the Muse alias question (R7)._

- **Price the 5.5 models and keep extrapolated rates out of `≥`** -- Add the Opus 5.5, Sonnet 5.5 and Haiku 5.5 cards to the bundled table from the vendor page (date `PRICING_LAST_UPDATED` and the vendor date, add the ids to `VERIFIED_MODEL_IDS`); for any model that still resolves through `MODEL_FAMILY_TIERS`, the `≥` subtotal (`_agent_row_cost`, `floor_prices`) must not use the extrapolated rate: the plan chooses between leaving that model out of the floor and pricing it at the family's lowest rate, and states which, so a floor never overstates; docs/invariants/events-retro.md's pricing rules say so. _src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_token_usage.py, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, ~230 lines._ (L)
- **Confirm a saved price override** -- A successful `--set-model-price` or `--mark-pricing-verified` writes one line to stderr naming the models saved (or the verification date set) and the cache path's location class, never to stdout, so `mm retro-fleet 7d > retro.md` keeps its output; README's pricing section shows the line. _src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_cli.py, README.md, ~70 lines._ (S)

**Acceptance:**

1. `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-haiku-5-5` resolve through their own cards (input, cache read, cache write at both the five-minute and one-hour rates, output) taken from the vendor page with its date recorded in the PR, are in `VERIFIED_MODEL_IDS`, and no longer appear under `pricing_extrapolated`; tests pin each id's resolved rates.
2. A retro over the same fixture on two machines, one with a local override equal to the bundled card and one without, prints the same Claude cost; a test pins it.
3. A model that still resolves through `MODEL_FAMILY_TIERS` contributes nothing to a `≥` figure at an extrapolated rate: it is left out of the floor or priced at the lowest rate the plan names, a test pins the figure against a fixture whose extrapolated rate would overstate it, and the extrapolated model is still named in `MM_HEALTH`.
4. docs/invariants/events-retro.md states the rule for extrapolated models under `≥`.
5. A successful `--set-model-price` and a successful `--mark-pricing-verified` each write one confirmation line to stderr and nothing new to stdout; the failure path's text is unchanged; tests pin all three.
6. README's "Pricing refresh and local overrides" shows the confirmation line.
7. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.


### Group 129: Retro notes name the machine and the right remedy ∥ One output style for every command

_Depends on: Group 125, Group 128_

**This group delivers:** The retro's floor causes and peer remedy name the machine, and version-prefixed commits classify by type. Every hand-run command prints errors, warnings, notices and fix commands in one written-down form, and `mm push` and `mm pull` end with one line per thing that actually happened.

#### Track 129A: Name the machine in the retro's floor causes and peer remedy, and classify version-prefixed commits
_3 tasks . ~320 LOC . low risk . 4 files_
**Outcome:** A retro on one machine says which machine each `cost_floor` cause belongs to without repeating the same sentence, tells the owner of a peer whose push has not arrived to run `mm push` there or wait for Syncthing rather than to "upgrade mind-meld to v0.14.17+ on that Mac", and counts commits whose subject starts with a version (`v1.2.3 fix(core): y`) under their real type instead of `other`.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O7.3 and O8.1 (a remedy must fix what it names). The owner, 2026-11-09: "Anything that is a reproducible bug we actually encountered should be current"; observed in the retro runs on `mac-b` and `linux-a`.
**Done when:** the acceptance list below passes: each floor cause names its machine and same-kind causes merge, the missing-snapshot note names machines and the right remedy for the state, and a version-prefixed subject classifies by its type.
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, tests/test_retro_usage_presentation.py, docs/invariants/events-retro.md_
_blocked-by: Track 128A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, src/mind_meld/skills/retro_fleet/aggregator.py (`aggregate_agent_usage`'s `per_family` loop and `_reader_scope`, `_attended_usage_remedy`, the `devices_without_accepted_row` notes, `_COMMIT_TYPE_RE`, `_classify_commit_subject`)_
_produces: retro notes that name machines and fitting remedies, and a commit classifier that strips a leading version_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: manager Ephesus 17's retro-fleet feedback and runs on `mac-b` and `linux-a`, 2026-11-09 (Future entries "Retro-fleet `cost_floor` causes are unattributed and repeated, and any reader failure floors every agent (retro-fleet item R3)", whose attribution half is promoted here, "Retro-fleet gives a peer whose push has not arrived the wrong remedy: \"upgrade mind-meld to v0.14.17+ on that Mac\" (retro-fleet item R4)" and "The commit-type classifier files version-prefixed subjects under `other` (retro-fleet item R5)"), promoted on the owner's rule above. Verified at `e04cfa0`: the `cost_floor` strings are built per snapshot from reader lists alone ("a host reader failed (" + readers + ")") with no `device_label`, so two machines with different reader lists yield two near-identical sentences; `_attended_usage_remedy()` is one fixed string ("upgrade mind-meld to v0.14.17+ on that Mac, verify with `mm --version`, then run `mm push`", `ATTENDED_USAGE_MIN_VERSION`) used at nine call sites, including the `devices_without_accepted_row` notes, which name no machine, while `fleet_incomplete` names the machine and says to run `mm push` there; `_COMMIT_TYPE_RE = ^([a-z]+)(?:\([^)]*\))?!?:` matches `feat: x` and `fix(core): y` but not `v0.0.7.0 feat: add command palette` (the manager saw 312 of 944 commits as `other` on one run; the pattern's behavior checked at `98a9131`, the count not re-run). Left in Future: R3's design question, whether a reader that never reads a family's records should floor that family's row (the contagion is deliberate and documented in events-retro.md), because it changes a documented rule._

- **Name the machine in `cost_floor` causes and merge repeats** -- Each cause says which machine's snapshot it came from (through `device_label`) and same-kind causes merge into one sentence per reader set; the rule that a failed reader floors every family is unchanged. _src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, ~100 lines._ (M)
- **Give a peer whose row is missing the remedy that fits** -- The missing-snapshot notes name the machines, say "run `mm push` on the machine, or wait for Syncthing to deliver" when its row is merely missing, keep the version advice only for a peer known to run an older mm (the plan settles what the data shows about a peer's version), and say "machine" where `_attended_usage_remedy()` says "Mac"; all nine call sites follow. _src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, tests/test_retro_usage_presentation.py, ~130 lines._ (M)
- **Classify a version-prefixed commit subject by its type** -- Strip a leading version token (`v` plus digits and dots) before matching `_COMMIT_TYPE_RE`, inside `_CLASSIFY_LEN_CAP`; measure how many `other` commits move on the owner's real fleet data first and record the counts (numbers only) in the PR; events-retro.md's classifier sentence follows. _src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, ~50 lines._ (S)

**Acceptance:**

1. A fixture with two machines whose snapshots have different failed readers yields `cost_floor` causes that name each machine and merge same-kind causes into one sentence per reader set; a test pins the text, and a test pins that a failed reader still floors every family.
2. A machine whose row has not arrived is named in the missing-snapshot note with "run `mm push` on the machine, or wait for Syncthing to deliver"; the version advice appears only for a peer the data shows runs an older mm; no note says "Mac" for a machine; tests pin each.
3. `v1.2.3 fix(core): y`, `v0.0.7.0 feat: add command palette` and `fix(core): y` classify as `fix`, `feat` and `fix`; a subject longer than `_CLASSIFY_LEN_CAP` and a subject that is only a version still classify without error; tests pin each.
4. The PR records how many `other` commits moved on real data (numbers only).
5. docs/invariants/events-retro.md describes the version strip in its classifier sentence.
6. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

#### Track 129B: Name the shared output style, and end push and pull with one line per thing that happened
_2 tasks . ~300 LOC . medium risk . 10 files_
**Outcome:** An owner who runs any mm command by hand sees errors, warnings, notices and fix commands in one form, including a mistyped option, and an `mm push` or `mm pull` that ends with one line for each thing that actually happened and nothing for what did not; routine counts and capture detail move to `--verbose`, `mm status` and `mm diag`.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.1 and O8.6 as amended 2026-11-10. The owner, 2026-11-10: "make sure all our manually run commands have consistently formatted output -- they all need to look like they come from the same tool" and "I'm fine w/ multiple lines at the end if there's more than one outcome".
**Done when:** the acceptance list below passes: the style is written down once and every form has one helper, `core._error`, core's warnings and Typer's usage errors print through them, and the push and pull summaries follow amended O8.1.
_touches: src/mind_meld/core.py, src/mind_meld/cli.py, tests/test_output_style.py (new), tests/test_integration.py, tests/test_projectid.py, tests/test_pullflow.py, tests/test_silent_failure_contract.py, README.md, docs/invariants/README.md, docs/invariants/sync.md_
_blocked-by: Track 125A_
_read-first: docs/SPEC.md (O8), docs/invariants/README.md, docs/invariants/sync.md, src/mind_meld/core.py (`_error`, `_sync_warning`, `_sync_diagnostic`, the `[yellow]Warning:` sites), src/mind_meld/cli.py (`_sync_success`, `_sync_failure`, `_emit_sync_outcome`, `_push_core`'s warnings, the verbose `Push complete.` ending)_
_produces: the shared output style (written down once) and its helpers, which every later output Track uses_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: the owner's words of 2026-11-10 (docs/SPEC.md O8 amendment). Survey of hand-run commands' output at `a6f3e73`, run with the installed mm 1.12.35 in a temporary HOME and read from the code: errors take four forms: `core._error` prints `Error: <message>` through Rich (red, wrapped at the terminal width, the fix inline as "run 'mm init' first", seen on `mm status`, `devices`, `gc`, `diff`, `sources`, `conflicts`, `enable-source` and `recover`); `mm push` and `mm pull` print one unwrapped `Push failed [setup-error]: ...; no content published. Run: mm init`; `install-skills` prints `mm: error: ...` (10 sites in `cli.py`); a mistyped option prints Typer's boxed `╭─ Error ─╮` panel. Warnings take two: `[yellow]Warning:[/yellow]` on stdout (4 sites each in `core.py`, `cli.py` and `pullflow.py`) and `mm: warning:` on stderr (64 sites). Fix commands take three: `Run: <command>` (push, pull, `statusflow.py` 21, `initflow.py` 8, `devicesflow.py` 5), "run `<command>`" (`retention.py`, `statusflow.py`, `upgrade.py`, `pullhistory.py`, `events_tail.py`) and "run '<command>'" (`core._error`'s config message). Notices already share `mm: notice:`. Success lines: `mm push` prints plain `Push: ...` while `--verbose` ends `[bold green]Push complete.`; `mm update`, `mm gc` and `mm recapture` end in bold green. The push outcome appends `; usage published` (`_sync_success`) and the pull outcome appends `; N skipped`, which amended O8.1 calls routine detail. This Track promotes the Future entry "Reconcile SPEC O8.2 with the push outcome line's usage suffix", whose question amended O8.1 answers._

- **Write the shared style down once and print every error, warning and notice through it** -- Name the forms in README's output section and in the routing table: the outcome line, `mm: error:`, `mm: warning:` and `mm: notice:` on stderr, and the fix command as `Run: <command>` printed last (the forms most commands already use; the plan confirms or amends them against the survey above). Give each form one helper beside `core._error`; route `core._error`, core's `[yellow]Warning:` sites and the push's own warning lines through them, and make Typer's usage errors print the same error form instead of its panel. _src/mind_meld/core.py, src/mind_meld/cli.py, tests/test_output_style.py (new), tests/test_silent_failure_contract.py, README.md, docs/invariants/README.md, ~170 lines._ (M)
- **End push and pull with one line per thing that happened** -- `_sync_success` prints one line for each thing that happened (what moved, project memory published under shared names, files held) and drops what did not; `; usage published` and the pull's skipped count move to `--verbose`, `mm status` and `mm diag`; a run that moved nothing keeps its single line; `--verbose` ends with the same summary instead of `Push complete.`; failures keep what broke and the fix command last. _src/mind_meld/cli.py, tests/test_integration.py, tests/test_projectid.py, tests/test_pullflow.py, docs/invariants/sync.md, ~130 lines._ (M)

**Acceptance:**

1. README and the routing table name the outcome, error, warning, notice and fix-command forms; a test prints each through its helper and pins the text.
2. `mm status`, `mm devices` and `mm push` run with no config give the same error form, with the fix command `Run: mm init` as the last line; a test pins all three.
3. `mm push --bogus` prints the shared error form with the usage fix, not Typer's panel, and exits 2; a test pins it.
4. No `[yellow]Warning:` or `[red]Error:` remains in `core.py` or in `cli.py`'s push and pull paths; tests pin the new forms.
5. A push that publishes content and usage prints one line for the content and no `usage published`; `mm push -v` still shows the usage publication; a pull that applies files and skips some prints no skipped count, which `mm pull -v` and `mm status` still show; a push or pull that moves nothing prints its one line; tests pin each.
6. A push or pull failure still ends with what broke and `Run: <command>` as the last thing printed and exits non-zero; `mm autopush` and `mm autopull` print nothing new; tests pin both.
7. The Future entry "Reconcile SPEC O8.2 with the push outcome line's usage suffix" is resolved by this Track; the PR says so and leaves the quiet-output checkpoint for the owner to accept.
8. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.


### Group 130: Push, pull and update share one progress display ∥ Setup, sources and conflict commands in the shared style

_Depends on: Group 125, Group 129_

**This group delivers:** `mm push`, `mm pull` and `mm update` show the same spinner, and an update during an attended push or pull shows as a step and its own `Updated mm X -> Y` line. `mm init`, `mm devices`, `mm sources`, `mm conflicts`, `mm resolve` and the pull's warnings use the shared style.

#### Track 130A: Give push, pull and update one progress display, and show an update that runs during a push or pull
_2 tasks . ~300 LOC . medium risk . 8 files_
**Outcome:** An owner sees the same spinner whether they run `mm push`, `mm pull` or `mm update`; it clears when done and is followed by the outcome line. When an attended push or pull updates mm, the update shows as a step in that spinner and adds its own `Updated mm X -> Y` line after the sync's line, instead of updating silently.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.1 and O8.6 as amended 2026-11-10. The owner, 2026-11-10: "I do think that it should show the same update installer when it's updating, like when we run mm update -- it shouldn't update silently" and "when we push and pull, should we show the same spinner? should we change update to look more like push/pull?"
**Done when:** the acceptance list below passes: one progress class serves all three commands, `mm update`'s progress clears before its outcome line, and an attended update during push or pull shows its step and its own outcome line while hooks, piped output and `--verbose` print what they print today.
_touches: src/mind_meld/core.py, src/mind_meld/cli.py, src/mind_meld/upgrade.py, tests/test_self_update.py, tests/test_integration.py, README.md, docs/invariants/auto-upgrade.md_
_blocked-by: Track 125A, Track 129B_
_read-first: docs/invariants/README.md, docs/invariants/auto-upgrade.md ("Self-update (v1.3.0)"), src/mind_meld/core.py (`_SyncProgress`, `_sync_progress_phase`), src/mind_meld/cli.py (`_run_update_with_progress`, `update`, `_sync_update`, `_sync_outcome`), src/mind_meld/upgrade.py (`update_or_nudge`, `_auto_install`, `run_update`'s `on_progress`)_
_produces: one progress display for push, pull and update, and an `Updated mm X -> Y` outcome line after an attended sync that updated mm_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: the owner's words of 2026-11-10. Verified at `a6f3e73`: `core._SyncProgress` (transient, TTY-only, cleared before the outcome line) says in its docstring it copies `_run_update_with_progress`'s Rich columns, ticker and failure isolation, while `cli._run_update_with_progress` builds its own non-transient `Progress`; `mm update` prints `Updating mm X → Y (pipx ...)…` before the spinner and `[bold green]Updated mm X → Y.` after it. During an attended push or pull, `cli._sync_update` calls `upgrade.update_or_nudge(config, attended=True, diagnostics=verbose)`; `_auto_install` runs `run_update` with no `on_progress` and prints its `updating` and `updated` notices only when `diagnostics` is true, so a non-verbose attended update shows no step and no line. This Track changes `core._SyncProgress`, which Track 125A is changing now, hence the block._

- **One progress display for push, pull and update** -- Make `mm update` run its installer through the same progress class as push and pull (transient, terminal-only, cleared when done) and delete the duplicate in `_run_update_with_progress`; the installer's phases and counters become the spinner's step and detail; `mm update` then prints its outcome line in the shared style (`Updated mm X -> Y`, or that mm is already current), and its failures keep what broke and the fix command last. _src/mind_meld/core.py, src/mind_meld/cli.py, tests/test_self_update.py, ~170 lines._ (M)
- **Show an update during an attended push or pull** -- On a terminal without `--verbose`, an automatic update during `mm push` or `mm pull` shows as a step in the sync's spinner (the installer's progress feeds it) and prints `Updated mm X -> Y` after the sync's outcome line; a failed update prints what broke and `Run: mm update` last without changing the sync's exit code. `mm autopush` and `mm autopull` stay silent; piped output and `--verbose` print what they print today. README and the auto-upgrade invariants describe it. _src/mind_meld/upgrade.py, src/mind_meld/cli.py, tests/test_self_update.py, tests/test_integration.py, README.md, docs/invariants/auto-upgrade.md, ~130 lines._ (M)

**Acceptance:**

1. `_run_update_with_progress` no longer builds its own `Progress`; push, pull and update use one progress class; a test pins it.
2. On a terminal, `mm update` shows a transient spinner that clears, then one outcome line (`Updated mm X -> Y`, or already current); with output piped it shows no spinner and the same outcome line; a failed update ends with what broke and the fix command; tests pin each.
3. On a terminal without `--verbose`, an attended `mm push` and `mm pull` that update mm show an update step in the spinner and print `Updated mm X -> Y` after the sync's outcome line; a failed update prints what broke and `Run: mm update` last and the exit code is the sync's; tests pin each.
4. `mm autopush` and `mm autopull` print nothing for an update; piped attended runs and `--verbose` runs print exactly what they print today; tests pin each.
5. README and `docs/invariants/auto-upgrade.md` describe the visible update.
6. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

#### Track 130B: Bring init, devices, sources, conflicts and resolve, and pull's own warnings, to the shared style
_3 tasks . ~300 LOC . low risk . 10 files_
**Outcome:** An owner who runs `mm init`, `mm devices`, `mm devices approve`, `mm sources`, `mm enable-source`, `mm disable-source`, `mm conflicts` or `mm resolve`, or reads a warning from `mm pull`, sees the same outcome, warning, error and fix-command forms as `mm push`.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O8.6 as amended 2026-11-10. The owner, 2026-11-10: "they all need to look like they come from the same tool".
**Done when:** the acceptance list below passes: each site the survey names prints in the shared style, with tests pinning the new text.
_touches: src/mind_meld/initflow.py, src/mind_meld/devicesflow.py, src/mind_meld/sourceflow.py, src/mind_meld/resolveflow.py, src/mind_meld/pullflow.py, tests/test_initflow.py, tests/test_devices_approve.py, tests/test_sourceflow.py, tests/test_conflict_copy.py, tests/test_pullflow.py_
_blocked-by: Track 129B_
_read-first: docs/invariants/README.md (the output style Track 129B wrote), docs/invariants/conflicts.md, src/mind_meld/resolveflow.py, src/mind_meld/pullflow.py (the `[yellow]Warning:` sites and `Pull refused:`), src/mind_meld/initflow.py, src/mind_meld/devicesflow.py, src/mind_meld/sourceflow.py_
_produces: init, devices, sources, conflicts, resolve and pull's warnings in the shared style_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: the survey in Track 129B's source note, verified at `a6f3e73`. Sites that do not follow the shared style once 129B has moved `core._error`: `pullflow.py` prints four `[yellow]Warning:[/yellow]` lines (peer and unknown-source warnings) and `[red]Pull refused:[/red]`; `resolveflow.py` prints per-file `[red]promote failed:`, `[red]delete failed:` and `[red]read failed:` lines beside four `mm: warning:` lines, and its successes in `[green]`; `initflow.py` (3), `devicesflow.py` (1) and `sourceflow.py` (4) print outcome lines in `[green]` where push prints plain text. `initflow.py` and `devicesflow.py` already end their stops with `Run: <command>`, so their fix commands are not in this Track. `sourceflow.py`'s `[red]N[/red]`/`[green]Y[/green]` table cells are a table, not a message, and stay unless the plan's style says otherwise._

- **Errors and warnings** -- Move pull's `[yellow]Warning:` lines and `Pull refused:` and resolve's per-file failure lines to the shared warning and error forms, keeping data-at-risk warnings visible in quiet mode. _src/mind_meld/pullflow.py, src/mind_meld/resolveflow.py, tests/test_pullflow.py, tests/test_conflict_copy.py, ~120 lines._ (M)
- **Outcome lines** -- `init`, `devices approve`, `sources`, `enable-source`, `disable-source` and `resolve` end with outcome lines in the shared form. _src/mind_meld/initflow.py, src/mind_meld/devicesflow.py, src/mind_meld/sourceflow.py, src/mind_meld/resolveflow.py, tests/test_initflow.py, tests/test_devices_approve.py, tests/test_sourceflow.py, ~100 lines._ (S)
- **Close four recorded managed-join test gaps** -- In `tests/test_initflow.py`, pin the wait's 120 one-second pauses with independent values rather than production constants; inject corrupt, newer-format and unreadable headers after polling starts (immediate refusal, no Mind Meld state written); supply an unknown peer count on a managed rerun (stops without polling); and deliver in-flight or fleet entries during the wait, or make the folder unreadable at timeout, checking the current-state remedy with no writes. Recorded gaps, no demonstrated defect; filed by /review-and-prep on PR #308 at `13239eb`, still open at `a6f3e73`. _tests/test_initflow.py, ~80 lines._ (S)

**Acceptance:**

1. Each site in the source note prints the shared form; no `[yellow]Warning:` or `[red]` message prefix remains in the touched files outside tables; tests pin each command's new text.
2. A pull's data-at-risk warning still prints in quiet mode and under `mm autopull`; a test pins it.
3. The four managed-join cases in the third task have tests in `tests/test_initflow.py`.
4. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.


### Group 131: The retro says "catching up" too

_Depends on: Group 125, Group 129_

**This group delivers:** A fleet retro on any machine tells "a peer's reader is still catching up" apart from "a peer's reader failed", without running `mm diag` on the peer.

#### Track 131A: Carry a reader's progress to the retro and word it "catching up (N left)" in `MM_HEALTH`
_3 tasks . ~330 LOC . medium risk . 9 files_
**Outcome:** A `retro-fleet` run on one machine says "catching up (N left)" for a peer's host-usage reader or git walk that stopped early on a deadline, says it needs nothing beyond that peer's next attended push, and still says failed for a reader that failed; the Codex token total stays marked a floor until the backlog finishes.
**Supports:** MVP-1 on Syncthing; docs/SPEC.md O7.3 (a coverage gap is named, never a silent zero) and O7.4 (the fleet's retro shows complete capture from every active machine). The owner, 2026-11-09: "1 - sure" (the hold lifted for the Tracks carded here), on the manager's second Track whose outcome includes the retro's `MM_HEALTH` saying "catching up (N left)".
**Done when:** the acceptance list below passes: a pushed row carries the progress only when a reader stopped on its deadline with items left, the retro words it "catching up (N left)" and keeps "failed" for the rest, and a row from a machine without the field reads exactly as today.
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_events.py, tests/test_host_usage_snapshot.py, tests/test_retro_fleet_aggregator.py, tests/test_retro_usage_presentation.py, README.md, docs/invariants/events-retro.md_
_blocked-by: Track 125A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, src/mind_meld/events.py (`make_host_usage_snapshot`, `host_reader_outcomes`, `EVENTS_SCHEMA_VERSION`), src/mind_meld/skills/retro_fleet/aggregator.py (`_reader_issue_note`, `_host_reader_coverage_notes`, the `git_budget` note)_
_produces: an additive progress field on pushed host rows and a "catching up (N left)" retro wording_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: manager Ephesus 17's retro-fleet run on `linux-a`, 2026-11-09 (Future entry "Retro-fleet reports a Codex reader that is still catching up as "failed" (retro-fleet item R2)", whose third direction lands here). Verified at `e04cfa0`: `aggregator._reader_issue_note("degraded", ...)` words every degraded reader "Host-usage reader(s) ... failed on the latest push from ..." because `degraded_sources` on the wire carries reader names only (`events.make_host_usage_snapshot`, `host_reader_outcomes`), and the aggregator's `git_budget` note reads `git_capture.walk_budget_aborts`, a count with no repositories-left figure. The wire field is new, so the AGENTS.md planning rules apply: the reader is named (the aggregator's coverage notes and its `git_budget` note), the counter semantics are stated (a per-reader, per-machine count of items left, never summed across readers or machines and never added to a token total), and older peers' rows without the field keep today's wording. Whether the field needs an `EVENTS_SCHEMA_VERSION` change or stays additive under the existing rule is for the plan to settle from `docs/invariants/events-retro.md`. The missing-snapshot remedy (R4) is Track 129A, not this one._

- **Carry the progress onto the wire** -- An additive field on a pushed host-usage row (and a repositories-left figure beside `git_capture`'s budget-abort count) records, per reader, the items left when the reader stopped on its deadline; absent when nothing is left or the reader failed; the aggregator is its named reader, and the invariants record its meaning. _src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/test_events.py, tests/test_host_usage_snapshot.py, docs/invariants/events-retro.md, ~150 lines._ (M)
- **Word it in the retro's `MM_HEALTH`** -- `_reader_issue_note` and the `git_budget` note say "catching up (N left)" for the readers and machines whose latest row carries the field, say it needs no action beyond that machine's next attended push, and keep "failed" for the rest; rows without the field read as today; README's coverage text follows. _src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_retro_fleet_aggregator.py, tests/test_retro_usage_presentation.py, README.md, ~150 lines._ (M)
- **Close the recorded attended-cap precedence test gap** -- Extend the existing precedence test in `tests/test_host_usage_snapshot.py` so an explicit `attended_capture_cap_ms` with the legacy `host_usage_interactive_budget_ms` and no `capture_budget_ms` pins the sweep preference, the stashed cap and the partitioned tail. Recorded gap, no demonstrated defect; filed by /review-and-prep for Track 124A at `6fb045c`, still open at `a6f3e73`. _tests/test_host_usage_snapshot.py, ~30 lines._ (S)
**Acceptance:**

1. A pushed host row carries the progress field only when a reader stopped on its deadline with items left; a failed reader, a permanent reason and a finished reader carry none; tests pin each.
2. A row from a machine without the field is accepted and read exactly as today (the reader still reads "failed" in the retro); a test pins it.
3. `docs/invariants/events-retro.md` records the field, its reader and its semantics (per reader and per machine, never summed across readers or machines or added to a token total), and the decision on `EVENTS_SCHEMA_VERSION`.
4. The retro's `MM_HEALTH` on another machine says "catching up (N left)" for a peer whose latest row carries the field, names the machine, says nothing more is needed than that machine's next attended push, and says "failed" for a degraded reader without it; the Codex token total is still marked a floor; tests pin each.
5. The `git_budget` note says "catching up" with repositories left for a peer whose row carries the figure and keeps today's text otherwise; a test pins both.
6. README's coverage text matches the new wording.
7. The attended-cap and legacy-sweep combination has a test in `tests/test_host_usage_snapshot.py`.
8. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.


### Group 132: Quiet and consistent notices

_Depends on: Group 129, Group 130, Group 131_

**This group delivers:** `mm recapture` no longer prints one nested-repository line per checkout, and the push and `mm status` give the same advice for an Antigravity record no release can read.

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


### Group 133: Status, gc, diff, log and recapture in the shared style

_Depends on: Group 130, Group 132_

**This group delivers:** `mm status`, `mm gc`, `mm diff`, `mm log`, `mm recapture` and the update notices a push prints use the shared style.

#### Track 133A: Bring status, gc, diff, log, recapture and the update notices to the shared style
_2 tasks . ~220 LOC . low risk . 11 files_
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

**Acceptance:**

1. Each site in the source note prints the shared form; no `[yellow]Warning:`, `[red]Error:` or "run `...`" fix form remains in the touched files; tests pin each command's new text.
2. `mm gc` and `mm recapture` end with one outcome line in the shared form; tests pin both.
3. `mm status` still opens with its one-line verdict (O8.4); a test pins it.
4. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.


### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of document order; document order is priority, not gating.

```
- Group 125 ← {}
- Group 128 ← {125}
- Group 129 ← {125, 128}
- Group 130 ← {125, 129}
- Group 131 ← {125, 129}
- Group 132 ← {129, 130, 131}
- Group 133 ← {130, 132}
```

```
Group 125: Scans resume, show progress and say "catching up" ∥ One automatic update observed on a real machine   (in progress)
  +-- Track 125A ......... ~M . 2 tasks
  +-- Track 125B ......... ~S . 1 task

Group 128: Retro cost is honest
  +-- Track 128A ......... ~L . 2 tasks

Group 129: Retro notes name the machine and the right remedy ∥ One output style for every command
  +-- Track 129A ......... ~M . 3 tasks
  +-- Track 129B ......... ~M . 2 tasks

Group 130: Push, pull and update share one progress display ∥ Setup, sources and conflict commands in the shared style
  +-- Track 130A ......... ~M . 2 tasks
  +-- Track 130B ......... ~M . 3 tasks

Group 131: The retro says "catching up" too
  +-- Track 131A ......... ~M . 3 tasks

Group 132: Quiet and consistent notices
  +-- Track 132A ......... ~M . 2 tasks

Group 133: Status, gc, diff, log and recapture in the shared style
  +-- Track 133A ......... ~M . 2 tasks
```

**Total: 0 phases . 7 groups . 10 tracks remaining.**

Tracks 123A, 123B, 124A and 127A shipped (v1.12.32, v1.12.31, v1.12.33 and v1.12.35) and Groups 123, 124 and 127 are retired; Track 124B shipped v1.12.34 with its post-merge check carried as Track 125B. IDs 95A and 96A stay unused because Future entries cite them.

The 2.0.0 cut is not a Track, and its path is open: the owner will decide how to get to 2.0 after using the Syncthing fleet for a while. SPEC's release line for 2.0.0 is O1 to O4 accepted on the owner's own observation on one Mac and both Linux machines in the Syncthing fleet, with O6 to O8 holding, recorded in PROGRESS.md `## Acceptance`. A Mac not yet moved keeps running on iCloud Drive and holds nothing up. Making the repository public again is a separate owner decision. Problems found while machines move are fixed as they appear.

---

## Future

Deferred: docs/roadmap-future.md (38 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
