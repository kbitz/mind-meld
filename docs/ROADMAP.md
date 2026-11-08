# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. On 2026-11-07 the owner dropped the fleet-wide cutover: there is no migration tool, each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet, a Mac not yet moved keeps running on iCloud Drive, and a first install sets Syncthing up itself when Syncthing still has its default configuration (SPEC O2). Tracks 113A to 116A have shipped (v1.12.21 to v1.12.24), which completes the Cursor removal; Tracks 117A to 120A deliver O2's setup, approval and per-machine move; Track 121A is the owner-gated git-history hostname scrub, a condition of 2.0.0 and of making the repository public again. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval and introducers on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration, removing dead machines and scan requests are refused until the spec's revisit trigger is observed.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line (spec O8, 2026-10-19).** A Track that adds output to push, pull or the auto commands must show it is the outcome, a failure with its fix, data at risk or an actionable warning; everything else goes to `mm status`, `mm diag` or `--verbose`.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-07). No branch, session or open pull request is active for any Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116_

### Group 117: Syncthing default-configuration check and writer

_Depends on: none_

**This group delivers:** mm can tell whether Syncthing has its default configuration and can make O2's Syncthing changes where it manages Syncthing, never elsewhere.

#### Track 117A: Detect Syncthing's default configuration and give mm a Syncthing writer
_4 tasks . ~720 LOC . medium risk . 6 files_
**Outcome:** mm can tell whether this machine's Syncthing has its default configuration and, on a machine where mm manages Syncthing, can create mm's folder with its options, add a device, accept a folder offer, list pending devices, mark a device as introducer and remove the stock Default Folder through Syncthing's local API; every write refuses on a machine whose Syncthing the owner manages.
**Supports:** MVP-1; docs/SPEC.md O2.1 (default configuration; mm manages Syncthing only where it found the default configuration at setup), O2.4 (no change to a pre-existing configuration) and O2.6 (no other Syncthing writes). Necessary prerequisite of the setup and approval Tracks.
**Done when:** `syncthing.py` classifies a configuration as default exactly when it has no remote device and no folder except Syncthing's stock Default Folder that is shared with no device and empty on disk, ignoring GUI and other option changes, and reports which device or folder made it pre-existing (names through the caller's `safety` sanitizer, never raw); a separate writer exposes only the operations O2.2 and O2.3 need (create mm's folder with every `FOLDER_OPTIONS` value, add a device, accept a folder offer from a named device, read pending devices, set a device's introducer flag, remove the unshared empty stock Default Folder) and refuses unless the caller passes mm's record that it manages Syncthing on this machine; the existing read-only functions keep their behavior and the module stays standard-library only; tests run against a local stand-in for Syncthing's REST API (no real Syncthing, no network beyond loopback) and cover default, each pre-existing shape, a non-empty or shared Default Folder, API errors and timeouts, and the writer's refusal without the managed record; the API key is never printed or logged; the four recorded Syncthing client test gaps get tests (certificate acceptance and rejection and successful or slow TLS responses; a redirect refused with zero contact to its destination; a total request budget shorter than one request's timeout or exhausted across endpoints; an oversized config or HTTP body rejected at its byte ceiling); `docs/invariants/sync.md` and the routing rows replace "never mutates Syncthing's configuration" with the writer's exact limits; `./bin/check --affected` passes.
_touches: src/mind_meld/syncthing.py, tests/test_syncthing.py, tests/test_syncthing_write.py (new), tests/syncthing_folder.py, docs/invariants/sync.md, docs/invariants/README.md_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/invariants/init-devices.md_
_session: fresh · effort: high · verify: ./bin/check --affected_

_Source: owner decisions 2026-11-07 (Karl: "it should set up syncthing IF syncthing had not otherwise been configured -- if there is a pre-existing syncthing config (that's not simply the default), then it should give the user a docs guide"; "No devices, no folders" chosen as the definition of default; "Auto full mesh" chosen, so the writer sets introducer flags). Verified at `488094e`: `syncthing.py`'s docstring says it "never mutates Syncthing's configuration or state" and reads `config.xml` and the GUI REST API (`read_state`, `config_dirs`, `FOLDER_OPTIONS`). Test gaps: docs/roadmap-future.md "Test gap: Syncthing HTTPS certificate and response cases", "... redirect destination isolation", "... total request budget" and "... input byte ceilings" (`[review]`, Track 92A, `51999d4`), placed here because this Track's `_touches:` cover their trigger paths; combined into one task to stay inside the size cap._

- **Default-configuration classifier and its tests** -- _src/mind_meld/syncthing.py, tests/test_syncthing.py, tests/syncthing_folder.py, ~200 lines._ (M)
- **Syncthing writer limited to O2's operations, with the managed-record gate and its tests** -- _src/mind_meld/syncthing.py, tests/test_syncthing_write.py (new), ~350 lines._ (M)
- **Recorded Syncthing client test gaps** -- _tests/test_syncthing.py, ~120 lines._ (S)
- **Sync invariants and routing rows** -- _docs/invariants/sync.md, docs/invariants/README.md, ~50 lines._ (S)

### Group 118: Syncthing setup from the bootstrap script and `mm init`

_Depends on: Group 117_

**This group delivers:** A fresh machine with a default Syncthing configuration gets Syncthing started at login and set up by `mm init`, for a new fleet or a join; a machine with its own Syncthing configuration gets the README guide instead.

#### Track 118A: Set Syncthing up from the bootstrap script and `mm init` on a default configuration
_3 tasks . ~670 LOC . medium risk . 9 files_
**Outcome:** On a machine whose Syncthing has its default configuration, the bootstrap script starts Syncthing at login, `mm init --new-fleet` creates the shared folder with mm's options, and `mm init` on a joining machine asks for one fleet machine's Syncthing device ID, pairs with it as an introducer, accepts the fleet folder with mm's options, prints this machine's own device ID for the approval step and joins once the key header arrives; on a pre-existing configuration mm changes nothing in Syncthing and points to a new README guide.
**Supports:** MVP-1; docs/SPEC.md O2.1, O2.2 ("the install starts Syncthing at login"), O2.4, O2.5 (join, `--new-fleet` on macOS or Linux) and Constraints (the bootstrap script starts Syncthing at login where Syncthing has its default configuration).
**Done when:** `initflow.init` checks Syncthing's configuration before it writes anything; on default it records in mm's local config that mm manages Syncthing on this machine, then `--new-fleet` creates the folder at the storage path (removing the unshared empty stock Default Folder first when mm's folder would be nested inside it) before its existing key-header bootstrap, and the join path asks for a fleet machine's device ID (validated as a Syncthing device ID before any write), adds that device as introducer, accepts its fleet folder offer with mm's options, prints this machine's device ID and the approval command to run on the fleet machine, and then follows the existing join (it never creates a key header and stops with "wait for Syncthing" until the header arrives); on a pre-existing configuration it writes nothing to Syncthing, prints the README guide's heading and continues with the existing join or `--new-fleet` once the folder is shared; Syncthing not running or its API unreachable stops with the start command and writes nothing; re-running `mm init` after any partial step continues without duplicating a device or folder; README's Install section says the bootstrap script starts Syncthing at login where Syncthing has its default configuration, and README gains the guide for a pre-existing Syncthing configuration (pairing, sharing the folder, the five folder options, `.stignore`, approving a new machine by hand) and describes the setup; `cli.py`'s `init` help matches; tests cover new fleet, join, pre-existing, Syncthing down, an invalid pasted ID and a rerun against the Syncthing stand-in from Track 117A; `docs/invariants/init-devices.md` records the new order of writes; `bin/bootstrap` runs `brew services start syncthing` on macOS or `systemctl --user enable --now syncthing.service` on Arch only when Syncthing has no configuration yet or the default-configuration check from Track 117A reports default (the plan chooses how the shell script asks; it never parses Syncthing's XML itself), never starts a second Syncthing where one is already running (including a Syncthing.app or other non-brew instance it can detect), prints what broke and the one fixing command when a start fails, and its closing message drops "This script pairs no machine, shares no folder and writes no Syncthing configuration" and tells the owner to run `mm init --new-fleet` or `mm init`; `tests/test_bootstrap.py` covers default, pre-existing, already-running and failed-start cases with stubbed commands; `./bin/check --affected` passes.
_touches: src/mind_meld/initflow.py, src/mind_meld/cli.py, src/mind_meld/config.py, tests/test_initflow.py, tests/test_config.py, bin/bootstrap, tests/test_bootstrap.py, README.md, docs/invariants/init-devices.md_
_blocked-by: Track 117A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md, docs/invariants/sync.md, docs/SPEC.md, docs/roadmap-future.md_
_session: fresh · effort: high · verify: ./bin/check --affected_

_Source: owner decisions 2026-11-07 (setup when Syncthing has its default configuration, the guide otherwise; one pasted device ID to join; auto full mesh through introducers). Verified at `488094e`: `initflow.init(new_fleet=...)`, `_bootstrap_new_fleet` (the only key-header writer), `_classify_folder` and `_write_stignore` exist; `cli.py`'s `init` docstring says to "share the folder in Syncthing and run 'mm init' on the other machines"; `bin/bootstrap` prints `start_syncthing_cmd` (`brew services start syncthing`, `systemctl --user enable --now syncthing.service`) as a next step and says "This script pairs no machine, shares no folder and writes no Syncthing configuration". The bootstrap change was folded in from a separate Track by the owner (Karl, 2026-11-07: "Do we need this many separate tracks?", then "Revise as discussed"). docs/roadmap-future.md's "Bootstrap hardening candidates" (item 5: a second, brew-installed Syncthing for Syncthing.app users) is the already-running case; the two bootstrap test-gap entries stay deferred by the owner's same decision even though this Track's `_touches:` meet their trigger._

- **Setup and join through Syncthing's API on a default configuration** -- _src/mind_meld/initflow.py, src/mind_meld/config.py, tests/test_initflow.py, tests/test_config.py, ~380 lines._ (L)
- **Bootstrap starts Syncthing at login on a default configuration, never twice** -- _bin/bootstrap, tests/test_bootstrap.py, ~120 lines._ (S)
- **README setup description, the pre-existing-configuration guide and init invariants** -- _README.md, src/mind_meld/cli.py, docs/invariants/init-devices.md, ~160 lines._ (S)

### Group 119: Approving a joining machine

_Depends on: Group 117, Group 118_

**This group delivers:** `mm devices` moves into its own module, and the owner approves a new machine with one pasted device ID, after which every machine where mm manages Syncthing links to every other.

#### Track 119A: Move `mm devices` into its own module, then approve a joining machine with `mm devices approve <device-id>`
_3 tasks . ~580 LOC . medium risk . 10 files_
**Outcome:** On a fleet machine where mm manages Syncthing, the owner pastes the new machine's device ID into `mm devices approve <device-id>`; mm accepts that exact pending device, shares the fleet folder with it and marks it as introducer, so the new machine connects to every machine where mm manages Syncthing; nothing else is ever accepted.
**Supports:** MVP-1; docs/SPEC.md O2.3 (exact-match approval, never automatic, introducers) and O2.6; the core-and-extensions constraint, met by a move-only first task in its own commit (the owner folded the separate move-only Track into this one, 2026-11-07).
**Done when:** first, in a move-only commit with no behavior change, the body of `cli.py`'s `devices` command (table and `--format json` output, `_list_devices_warn` use, the `--format` validation) moves to a new `src/mind_meld/devicesflow.py`, `cli.py` keeps only the registration that calls it module-qualified, and a contract test pins `mm devices` table output and the `--format json` keys (`device_id`, `device_name`, `last_seen`, `last_seen_version`, nulls for missing fields, `[]` for an empty fleet) that retro-fleet's aggregator reads; then `mm devices approve <device-id>` (in `devicesflow.py`, registered from `cli.py` module-qualified, `mm devices` and `mm devices --format json` unchanged) accepts only a device whose full ID exactly matches an entry in Syncthing's pending devices, adds it, shares the fleet folder with it and sets it as introducer; a malformed ID, an ID that is not pending, or a near-match changes nothing and says what to check, exit 1; on a machine where mm does not manage Syncthing it changes nothing and names the README guide's approval steps; Syncthing down stops with the start command; an approve that is repeated after success reports the machine already approved, exit 0; no push, pull or auto command ever accepts a device; `tests/test_integration.py`'s `COMMAND_INTENTS62` and `tests/test_module_boundaries.py` list the command and the new module; `docs/invariants/README.md`'s module map and routing rows name `devicesflow.py`; tests cover each case against the Syncthing stand-in; README's command list and fleet sections describe it; `docs/invariants/init-devices.md` records it; `./bin/check --affected` passes.
_touches: src/mind_meld/devicesflow.py (new), src/mind_meld/cli.py, tests/test_devices_command.py (new), tests/test_devices_approve.py (new), tests/test_integration.py, tests/test_module_boundaries.py, README.md, AGENTS.md, docs/invariants/init-devices.md, docs/invariants/README.md_
_blocked-by: Track 117A, Track 118A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md, docs/invariants/sync.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner, 2026-11-07, choosing approval by pasted ID ("on the existing fleet machine the owner runs one mm command with that pasted ID (e.g. mm devices approve <device-id>), and mm accepts only an exact match among Syncthing's pending devices, then adds the device and shares the fleet folder. Never auto-accept.") and "Auto full mesh". AGENTS.md's Commands line gains the command, which is why it is touched. It follows Track 118A because approval needs the record, written at setup, that mm manages Syncthing on this machine. Verified at `488094e`: `cli.py` defines `devices` as a single `@app.command()` (`def devices(fmt: ...)`), and `tests/test_module_boundaries.py` lists `devices` in its command inventory; AGENTS.md "Core and extensions" and docs/SPEC.md Constraints require the move before the change._

- **Move the `devices` command body and pin its output (move-only commit)** -- _src/mind_meld/cli.py, src/mind_meld/devicesflow.py (new), tests/test_devices_command.py (new), tests/test_module_boundaries.py, docs/invariants/README.md, ~180 lines._ (M)
- **The approve command with exact-match acceptance and introducer marking** -- _src/mind_meld/devicesflow.py, src/mind_meld/cli.py, tests/test_devices_approve.py (new), tests/test_integration.py, tests/test_module_boundaries.py, ~320 lines._ (M)
- **README and AGENTS.md command lists and the device invariants** -- _README.md, AGENTS.md, docs/invariants/init-devices.md, ~70 lines._ (S)

### Group 120: Per-machine move

_Depends on: Group 118, Group 119_

**This group delivers:** README describes moving one machine at a time off iCloud Drive instead of a cutover, and a test proves a moved machine loses no file.

#### Track 120A: Replace the cutover runbook with the per-machine move, and prove a move loses no file
_3 tasks . ~450 LOC . medium risk . 4 files_
**Outcome:** README tells the owner how to move one machine at a time off iCloud Drive (uninstall mm, reinstall with the bootstrap script, then `mm init` to join or `mm init --new-fleet` to start the Syncthing fleet) and keeps no cutover runbook; an integration test shows that a machine holding iCloud-era mm state and agent files that have drifted from the fleet's joins a fresh fleet without losing or overwriting a local file it cannot prove is older.
**Supports:** MVP-1; docs/SPEC.md O2.5 (move by reinstall and join; no local agent file lost), O2.7 (the integration-test evidence) and the Constraints and Known limits that replace the cutover.
**Done when:** README's "Moving the fleet from iCloud Drive to Syncthing (cutover runbook)" section and its links are replaced by a short per-machine move section (what stays in the home, that a Mac not yet moved keeps syncing over iCloud Drive, that the two stores exchange nothing until a machine moves, that `retro-fleet` on Syncthing does not show the iCloud store's history, and that the old iCloud folder can be deleted once the last Mac has moved); AGENTS.md and root `SPEC.md` stop calling iCloud Drive "the pre-cutover path"; a new integration test builds a machine with a populated state database and synced history from one store, edits some agent files, then joins a separate fresh store whose peer holds other versions of some of those files, and shows that every local file still exists with its bytes or as a kept-both conflict, that only files with a proven last-synced base fast-forward, and that no file is deleted; if the test exposes loss, the Track stops and reports it to the owner rather than widening scope; `./bin/check --affected` passes.
_touches: README.md, AGENTS.md, SPEC.md, tests/test_move_join.py (new)_
_blocked-by: Track 118A, Track 119A_
_read-first: docs/SPEC.md, docs/invariants/sync.md, docs/invariants/conflicts.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner, 2026-11-07: "we don't need a migrate tool ... migration for me can be; uninstall mm and reinstall, register the machine w/ our fleet and boom, that machine is now g2g on syncthing", confirmed with the manager's reading (no fleet-wide cutover, the user's agent files merge on join through the existing first-run-from-divergent-state path). Verified at `488094e`: README has "## Moving the fleet from iCloud Drive to Syncthing (cutover runbook)" and links to it from its storage and add-a-machine sections; AGENTS.md's Key Principles and root SPEC.md say "iCloud Drive is the pre-cutover path only"; the pull ledger (`peer_base`, `seen_version`) is keyed per peer and path (docs/invariants/sync.md "Fast-forward, propagated deletions and anchors")._

- **Per-machine move section in place of the cutover runbook** -- _README.md, ~150 lines (net negative)._ (M)
- **AGENTS.md and root SPEC.md wording** -- _AGENTS.md, SPEC.md, ~10 lines._ (S)
- **Integration test: a machine with prior-store state joins a fresh fleet without loss** -- _tests/test_move_join.py (new), ~250 lines._ (M)

### Group 121: Git-history hostname scrub

_Depends on: Group 120_

**This group delivers:** A git history free of the owner's machine hostnames, the last carded condition for 2.0.0 and for making the repository public again.

#### Track 121A: Rewrite the git history to remove the owner's machine hostnames (owner-gated)
_3 tasks . ~30 LOC . high risk . 1 file_
**Outcome:** No commit, tag or other reachable ref in the repository contains any of the owner's machine hostnames or a fragment of one, so 2.0.0 can be cut and the repository can be made public again; installs keep upgrading from the same tag names.
**Supports:** MVP-1; docs/SPEC.md's 2.0.0 release line and its hostname constraint (owner, 2026-11-07: "that scrub is required before we go public again and hit 2.0 fyi").
**Done when:** the rewrite runs on a fresh mirror clone, never on a working checkout, with `git filter-repo` or an equivalent tool driven by a replacement list kept outside the repository and never committed, printed, or pasted into the PR or a log; before any push, a search of every blob, commit message, tag message and ref name in the rewritten mirror for each listed name prints nothing; every release tag, `latest` included, points at its rewritten commit under the same name; **the owner gives an explicit go in the session before any push**, lifts `main`'s force-push protection and restores it afterwards, and runs or approves the force-push of `main` and the tags; afterwards a search of a fresh clone prints nothing, and `mm update` on one machine reinstalls from `@latest` at the same version; README's Upgrading section tells every other clone and worktree to re-clone rather than rebase, and says pipx installs need no action; the refs GitHub keeps outside the owner's control (pull-request refs and cached views of old commits) are listed for the owner, who asks GitHub Support to purge them or accepts them before the repository goes public; no hostname appears in the PR, a commit message, README or any record the Track keeps; the Track does not make the repository public; `./bin/check tests/test_docs_routing.py` passes.
_touches: README.md_
_blocked-by: Track 113A, Track 116A, Track 120A_
_read-first: docs/SPEC.md, docs/roadmap-shipped.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_docs_routing.py_

_Source: owner, 2026-11-06 ("we will need to scrub the git history at some point, don't have to do it now though") and 2026-11-07 ("that scrub is required before we go public again and hit 2.0 fyi"), relayed by manager Ephesus 9; docs/SPEC.md's 2.0.0 release line and hostname constraint as amended 2026-11-07; Track 112A's receipt (the current tree is scrubbed; past commits are not). Promoted from docs/roadmap-future.md "Rewrite git history to remove the owner's machine hostnames". It runs after every other carded Track so no open branch has to be rebased onto the rewritten history; the 2026-11-07 regeneration adds the Syncthing setup and move Tracks ahead of it for that reason._

- **Rewrite and verify on a mirror clone** -- _a fresh mirror outside every checkout; the replacement list stays outside the repository._ (M)
- **Owner go, force-push of `main` and the tags, and the post-push checks** -- _the owner's go and push; one machine's `mm update`._ (S)
- **README Upgrading note for existing clones and worktrees** -- _README.md, ~20 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 117 ← {}
- Group 118 ← {117}
- Group 119 ← {117, 118}
- Group 120 ← {118, 119}
- Group 121 ← {120}
```

Track detail per group:

```
Group 117: Syncthing default-configuration check and writer
  +-- Track 117A .......... ~L . 4 tasks
Group 118: Syncthing setup from the bootstrap script and `mm init`
  +-- Track 118A .......... ~L . 3 tasks
Group 119: Approving a joining machine
  +-- Track 119A .......... ~L . 3 tasks
Group 120: Per-machine move
  +-- Track 120A .......... ~L . 3 tasks
Group 121: Git-history hostname scrub
  +-- Track 121A .......... ~M . 3 tasks
```

**Total: 5 groups . 5 tracks remaining.**

Tracks 113A, 114A, 115A and 116A shipped (v1.12.21, v1.12.22, v1.12.23 and v1.12.24) and Groups 113 to 116 are retired. The Syncthing Tracks take 117A to 120A, so the history scrub, Track 117A in the previous plan, is now Track 121A and still runs last so no open branch has to be rebased onto the rewritten history. The owner asked for the fewest Tracks the size audit allows (2026-11-07): the bootstrap start-at-login change rides in Track 118A, and the move-only `mm devices` module move is Track 119A's first task in its own commit rather than a separate Track. IDs 95A and 96A stay unused because Future entries cite them.

The 2.0.0 cut is not a Track. Carded work before it: Tracks 117A to 121A, ending with the history rewrite, which needs the owner's explicit go and their force-push. The owner's own steps: move each machine onto the Syncthing fleet by reinstalling mm and joining (README's per-machine move from Track 120A; machines may move earlier on the current release by pairing by hand), with the first machine running `mm init --new-fleet`; run `retro-fleet` on the Syncthing fleet for O7.4; and confirm O1 to O4 on their own observation on one Mac and both Linux machines, with O6 to O8 holding, recorded in PROGRESS.md `## Acceptance`. A Mac not yet moved keeps running on iCloud Drive and holds nothing up. Making the repository public again is a separate owner decision that waits for Track 121A. Problems found while machines move are fixed as they appear.

---

## Future

Deferred: docs/roadmap-future.md (25 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
