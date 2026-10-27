# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Cursor, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support was removed in v1.11.8; Groups 82–85 (resumable capture, affected-test selection, the status and Codex module moves, one capture budget) shipped in v1.11.9–v1.11.13 and live in shipped history; Grok models used through Cursor stay in the Grok row. Memory capture, recall and sync parity remain deferred ([the memory continuity design](designs/memory-continuity.md)).

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

No active branches, sessions or PRs were declared for this regeneration.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74_

### Phase 5: mm 2.0 on Syncthing

**End-state:** The owner's Macs and Linux machine sync over one Syncthing folder that carries the 1.x store, with Syncthing-tolerant reads, a join that never re-keys, shared Claude paths, fast-forward pulls, propagated deletions, Syncthing state in status and a cutover runbook; docs/SPEC.md O1 to O4 are accepted on a one-week soak and released as 2.0.0.
**Groups:** 86, 87, 88, 89, 90, 91, 92, 93

#### Group 86: Pull module

_Depends on: none_

**This group delivers:** `mm pull` and its apply path move into their own module.

##### Track 86A: Move pull and apply out of cli.py
_2 tasks . ~250 LOC . medium risk . 8 files_
**Outcome:** `mm pull` and `mm autopull`, including the per-file apply path, live in their own module behind a contract test, so Tracks 87A, 90A and 91A change and review that module instead of `cli.py`; nothing the owner sees changes.
**Supports:** MVP-1 delivery under docs/SPEC.md Constraints, "Core and extensions"; prerequisite for Tracks 87A (O3.1), 90A (O1.2) and 91A (O1.1).
**Done when:** `_pull_core`, `_download_and_apply`, `_apply_incoming_file` and the other helpers only `pull` and `autopull` call move to `pullflow.py`, and the shared helpers they call that are still in `cli.py` move to `core.py`; `cli.py` keeps the two command shells; `pullflow` never imports `cli`, and `tests/test_module_boundaries.py` lists it; a contract test drives a pull between two devices over `tmp_path` storage without importing `cli` and pins the `PullResult` counts, the outcome line and the exit codes; tests that patched moved helpers patch them at their new owner, and no existing assertion changes; no output, flag, exit code or storage change; the routing README and `sync.md` name the module; `./bin/check` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/core.py, src/mind_meld/pullflow.py (new), tests/test_pullflow.py (new), tests/test_pull_helpers.py, tests/test_silent_failure_contract.py, tests/test_fsutil.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/sync.md_
_blocked-by: Track 85A_
_out: 87A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, tests/test_module_boundaries.py_
_session: fresh · effort: high · verify: ./bin/check tests/test_pullflow.py tests/test_pull_helpers.py tests/test_silent_failure_contract.py tests/test_module_boundaries.py_

_Source: owner decision 2026-10-23 (docs/SPEC.md Constraints, "Core and extensions"). Measured at 0e46556 from the direct-call graph of `cli.py`: `pull`, `autopull` and the helpers only they call total about 2,600 lines, and they call 29 helpers shared with other commands._

- **Pull module** -- _src/mind_meld/cli.py, src/mind_meld/core.py, src/mind_meld/pullflow.py, tests/test_pull_helpers.py, tests/test_silent_failure_contract.py, tests/test_fsutil.py, ~2,600 lines moved._ (L)
- **Pull contract test and routing** -- _tests/test_pullflow.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/sync.md, ~250 lines._ (M)

#### Group 87: Syncthing-tolerant reads

_Depends on: Group 86_

**This group delivers:** Pull and status treat Syncthing's partial and foreign files as waiting or ignored.

##### Track 87A: Make storage reads tolerate Syncthing
_2 tasks . ~190 LOC . medium risk . 10 files_
**Outcome:** On a Syncthing folder, pull and status ignore Syncthing's temporary and conflict files, retry a file Syncthing is replacing, and report content that has not arrived as waiting rather than failed.
**Supports:** MVP-1, docs/SPEC.md O3.1 and O1.4 (late, out-of-order delivery).
**Done when:** `LocalBackend.list_keys` skips dot-prefixed and Syncthing temporary names and `find_conflict_copies` recognizes `*.sync-conflict-*`; a device file counts only when its name is that device's key; `mm diag` counts foreign files; `LocalBackend.get` retries a vanished file briefly and a vanished file reads as missing, not corrupt; a device entry that vanishes mid-read is skipped with a notice instead of refusing the pull; a missing blob yields a `waiting` outcome that is not a failure and does not mark autopull degraded, and pull says what it is waiting for; tests use a local folder that delivers files late and swaps them mid-read; `./bin/check` passes.
_touches: src/mind_meld/storage/local.py, src/mind_meld/devices.py, src/mind_meld/errors.py, src/mind_meld/pullflow.py, src/mind_meld/core.py, src/mind_meld/statusflow.py, tests/test_storage_local.py, tests/test_devices.py, tests/test_diag.py, tests/test_pull_result.py, docs/invariants/sync.md_
_blocked-by: Track 85A, Track 86A_
_out: 88A, 89A, 91A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/archive/store-v2.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_storage_local.py tests/test_devices.py tests/test_diag.py tests/test_pull_result.py_

_Source: docs/archive/store-v2.md "Why a new store" hazards, re-checked at 408766d: `_download_and_apply` counts a missing blob as `failed`; `_fetch_remote_manifest` checks `exists` and then `get`, so a swapped file reads as corrupt; `devices.list_devices_with_drops` drops an unreadable entry and `_check_fleet_version_or_refuse` then refuses the pull; `devices._list_devices_impl` accepts any `*.json`; `LocalBackend.find_conflict_copies` knows only iCloud and Dropbox names._

- **Ignore foreign files and retry swaps** -- _src/mind_meld/storage/local.py, src/mind_meld/devices.py, src/mind_meld/core.py, src/mind_meld/statusflow.py, tests/test_storage_local.py, tests/test_devices.py, tests/test_diag.py, ~110 lines._ (M)
- **A waiting outcome** -- _src/mind_meld/errors.py, src/mind_meld/pullflow.py, tests/test_pull_result.py, docs/invariants/sync.md, ~80 lines._ (M)

#### Group 88: Init module

_Depends on: Group 87_

**This group delivers:** `mm init` moves into its own module.

##### Track 88A: Move init out of cli.py
_2 tasks . ~150 LOC . low risk . 7 files_
**Outcome:** `mm init` lives in its own module behind a contract test, so Track 89A changes and reviews that module instead of `cli.py`; nothing the owner sees changes.
**Supports:** MVP-1 delivery under docs/SPEC.md Constraints, "Core and extensions"; prerequisite for Track 89A (O2.1).
**Done when:** `init`'s body and the helpers only it calls move to `initflow.py`, and any shared helpers it calls that are still in `cli.py` move to `core.py`; `cli.py` keeps the command shell; `initflow` never imports `cli`, and `tests/test_module_boundaries.py` lists it; a contract test runs init against an empty and a populated `tmp_path` folder without importing `cli` and pins its outcome and exit codes; tests that patched moved helpers patch them at their new owner, and no existing assertion changes; no output, flag or exit code change; the routing README and `init-devices.md` name the module; `./bin/check` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/core.py, src/mind_meld/initflow.py (new), tests/test_initflow.py (new), tests/test_init_auto_pin.py, tests/test_init_events_backfill.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/init-devices.md_
_blocked-by: Track 87A_
_out: 89A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md, tests/test_module_boundaries.py_
_session: fresh · effort: medium · verify: ./bin/check tests/test_initflow.py tests/test_init_auto_pin.py tests/test_module_boundaries.py_

_Source: owner decision 2026-10-23 (docs/SPEC.md Constraints, "Core and extensions"). Measured at 0e46556 from the direct-call graph of `cli.py`: `init` and the helpers only it calls total about 580 lines, and they call 5 helpers shared with other commands._

- **Init module** -- _src/mind_meld/cli.py, src/mind_meld/core.py, src/mind_meld/initflow.py, tests/test_init_auto_pin.py, tests/test_init_events_backfill.py, ~580 lines moved._ (M)
- **Init contract test and routing** -- _tests/test_initflow.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/init-devices.md, ~150 lines._ (S)

#### Group 89: Fleet creation and join

_Depends on: Group 87, Group 88_

**This group delivers:** Only a new fleet creates the key header, and iCloud defaults go.

##### Track 89A: Let only a new fleet create the key header, and drop iCloud defaults
_3 tasks . ~180 LOC . high risk . 10 files_
**Outcome:** A machine joining over Syncthing waits for the fleet's key header instead of minting a second one, and new installs default to a Syncthing folder instead of iCloud Drive.
**Supports:** MVP-1, docs/SPEC.md O2.1 and the cutover.
**Done when:** `mm init --new-fleet` creates `mm-crypto-init` only in an empty folder and writes `.stignore` with mm's temporary-file patterns; plain `mm init` joins: with no key header it stops and says to wait for Syncthing, never writes one, then verifies the passphrase and pulls; `apply_crypto_init_repair` never rewrites the canonical file and removes only byte-identical copies; the missing-key-header remedy no longer says to run `mm init`; the default storage path is `~/Sync/mind-meld` on macOS and Linux, and `_auto_pin_storage_for_icloud`, the iCloud default and iCloud wording are removed; tests cover init on an empty, an unsynced and a populated folder; the new `.stignore` writer calls `fsutil.refuse_real_state_write_under_pytest` before its first mutation and joins the raw-writer census; `./bin/check` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/initflow.py, src/mind_meld/core.py, src/mind_meld/crypto.py, src/mind_meld/config.py, tests/test_crypto.py, tests/test_init_auto_pin.py, tests/test_initflow.py, tests/test_integration.py, tests/test_fsutil.py, docs/invariants/init-devices.md, README.md_
_blocked-by: Track 87A, Track 88A_
_out: 90A, 93A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_crypto.py tests/test_initflow.py tests/test_integration.py_

_Source: docs/archive/store-v2.md hazard "Init on an unsynced folder mints a second root_salt", re-checked at 408766d: `init` treats a `"missing"` crypto-init fetch as a first device and `crypto.bootstrap_crypto_init` writes a new salt; `_init_storage_guard` warns only when blobs or manifests already exist; `crypto.apply_crypto_init_repair` rewrites the canonical file on push and pull; `_init_crypto_session` still says "Run 'mm init'" when the file is missing._

- **New fleet versus join** -- _src/mind_meld/cli.py, src/mind_meld/initflow.py, src/mind_meld/crypto.py, tests/test_crypto.py, tests/test_initflow.py, tests/test_integration.py, ~100 lines._ (M)
- **Write-once key header** -- _src/mind_meld/crypto.py, src/mind_meld/core.py, tests/test_crypto.py, ~40 lines._ (S)
- **Drop the iCloud defaults** -- _src/mind_meld/initflow.py, src/mind_meld/config.py, tests/test_init_auto_pin.py, docs/invariants/init-devices.md, README.md, ~40 lines (+120 del)._ (S)

#### Group 90: Portable Claude paths

_Depends on: Group 89_

**This group delivers:** Claude project memory is one folder on macOS and Linux.

##### Track 90A: Share Claude project memory between macOS and Linux paths
_2 tasks . ~270 LOC . high risk . 6 files_
**Outcome:** The owner's Claude project memory for `~/src/<project>` is one folder on a Mac and on the Linux machine, instead of separate `-Users-<name>-…` and `-home-<name>-…` copies.
**Supports:** MVP-1, docs/SPEC.md O1.2.
**Done when:** the Claude walker publishes home-encoded project folder names in a home-relative form and pull maps them back to the local encoding; tombstone keys and `is_tombstoned` use the same form, so the switch mints no deletions; `synclog` project parsing follows; a test round-trips a project between a macOS-style and a Linux-style home; README Upgrading says every machine must update before the first push in the new form; `./bin/check` passes.
_touches: src/mind_meld/manifest.py, src/mind_meld/pullflow.py, src/mind_meld/synclog.py, tests/test_manifest.py, tests/test_integration.py, tests/test_synclog.py, docs/invariants/sync.md, README.md_
_blocked-by: Track 89A_
_out: 91A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/archive/store-v2.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_manifest.py tests/test_integration.py tests/test_synclog.py_

_Source: docs/archive/store-v2.md §7 (current evidence) and its hazard row on absolute-path folder names; at 408766d `manifest.walk_claude_source` publishes `projects/-Users-<name>-…` wire paths._

- **Home-relative wire paths** -- _src/mind_meld/manifest.py, src/mind_meld/pullflow.py, tests/test_manifest.py, tests/test_integration.py, ~150 lines._ (M)
- **Tombstones and history follow** -- _src/mind_meld/manifest.py, src/mind_meld/synclog.py, tests/test_synclog.py, docs/invariants/sync.md, README.md, ~120 lines._ (M)

#### Group 91: Fast-forward pulls

_Depends on: Group 87, Group 90_

**This group delivers:** Peer edits fast-forward, conflicts need two real edits, and deletions propagate.

##### Track 91A: Fast-forward peer edits and propagate deletions
_2 tasks . ~450 LOC . high risk . 9 files_
**Outcome:** A peer's change to a file the owner has not touched simply applies, a conflict appears only when both machines changed the file, deleted `MEMORY.md` lines stay deleted, and a file deleted on one machine is deleted on the others with a 30-day recovery copy.
**Supports:** MVP-1, docs/SPEC.md O1.1.
**Done when:** a `state.db` migration adds a per-peer last-synced base (`peer, source, path, sha`); `_apply_incoming_file` skips when the remote equals the base, fast-forwards with no union when the local file equals the base, and unions a mergeable file or writes a conflict copy only when both changed, falling back to today's rule when no base exists; a peer tombstone deletes the local file when it still equals the base and keeps the bytes recoverable for 30 days; bases are recorded after applied, matched and deleted outcomes, never after failed or waiting ones; `pullplan` predicts the same; `conflicts.md` and `sync.md` describe the rule; tests cover each branch and a three-machine sequence; the recovery-copy writer calls `fsutil.refuse_real_state_write_under_pytest` before its first mutation and joins the raw-writer census; the provisional `state.db` tables each name a reader or an explicit no-reader, and the pre-2026-10-19 Track IDs in `sync.md`'s history-log section, `statedb.py`, the routing README, `init-devices.md` and `tests/test_pullhistory.py` are retired (including `sync.md`'s deferred hold-escalation pointer, killed 2026-10-21 as unobserved); `./bin/check` passes.
_touches: src/mind_meld/statedb.py, src/mind_meld/pullflow.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, src/mind_meld/retention.py, tests/test_statedb.py, tests/test_merge.py, tests/test_additive_sync.py, tests/test_integration.py, tests/test_fsutil.py, docs/invariants/conflicts.md, docs/invariants/sync.md, README.md, docs/invariants/README.md, docs/invariants/init-devices.md, tests/test_pullhistory.py_
_blocked-by: Track 87A, Track 90A_
_out: 92A, 93A_
_read-first: docs/invariants/README.md, docs/invariants/conflicts.md, docs/invariants/sync.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_statedb.py tests/test_merge.py tests/test_additive_sync.py tests/test_integration.py_

_Source: docs/archive/store-v2.md hazard rows "Every peer edit to an untouched file is a conflict" and "Line deletions in `MEMORY.md` and `.jsonl` never stick", re-checked at 408766d: `_apply_incoming_file` union-merges every update of a mergeable file, then keeps a newer local file or writes a conflict copy; pull never deletes local files (tombstones only stop re-download); `statedb` has an unused `ledger` table and no base._

- **Base table and the fast-forward rule** -- _src/mind_meld/statedb.py, src/mind_meld/pullflow.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, tests/test_statedb.py, tests/test_merge.py, tests/test_additive_sync.py, ~300 lines._ (L)
- **Propagated deletions with recovery** -- _src/mind_meld/pullflow.py, src/mind_meld/retention.py, tests/test_integration.py, docs/invariants/conflicts.md, docs/invariants/sync.md, README.md, ~150 lines._ (M)

#### Group 92: Syncthing status

_Depends on: Group 91_

**This group delivers:** Status shows Syncthing's state and each machine's latest push.

##### Track 92A: Report Syncthing state and each machine's latest push
_2 tasks . ~350 LOC . medium risk . 9 files_
**Outcome:** `mm status` tells the owner whether Syncthing is connected and in sync and whether this machine has applied each peer's latest push, and `mm diag` flags folder settings that would break sync.
**Supports:** MVP-1, docs/SPEC.md O2.2 and O3.2.
**Done when:** a new `syncthing.py` finds the local Syncthing configuration and reads its REST API read-only with timeouts (connected peers, folder state, and the folder options README lists); mm never writes Syncthing's configuration; pull records each peer's applied manifest time, and status shows each peer's last push and whether it is applied, warning in every mode when something has waited more than 24 h; diag lints the folder options and `.stignore`; a missing or unreachable Syncthing is reported, not an error; tests use a fake REST server; `./bin/check` passes.
_touches: src/mind_meld/syncthing.py (new), src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/statedb.py, tests/test_syncthing.py (new), tests/test_diag.py, tests/test_module_boundaries.py, README.md, docs/invariants/README.md_
_blocked-by: Track 77A, Track 91A_
_out: 93A_
_read-first: docs/invariants/README.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_syncthing.py tests/test_diag.py tests/test_module_boundaries.py_

_Source: docs/SPEC.md O2.2 and O3.2; docs/archive/store-v2.md §10 folder options (current evidence; Syncthing option semantics verified against docs.syncthing.net on 2026-10-11 for the former Track 81A)._

- **Read-only Syncthing client and diag lint** -- _src/mind_meld/syncthing.py, src/mind_meld/statusflow.py, tests/test_syncthing.py, tests/test_module_boundaries.py, docs/invariants/README.md, ~200 lines._ (M)
- **Each peer's push in status** -- _src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/statedb.py, tests/test_diag.py, README.md, ~150 lines._ (M)

#### Group 93: Cutover runbook

_Depends on: Group 89, Group 91, Group 92_

**This group delivers:** A runbook and agent docs for moving the fleet onto Syncthing.

##### Track 93A: Write the cutover runbook and the Syncthing documentation
_2 tasks . ~400 LOC . low risk . 7 files_
**Outcome:** The owner has a step-by-step runbook to move the fleet from iCloud Drive to Syncthing and bring the Linux machine in, and every document an agent reads describes the Syncthing store.
**Supports:** MVP-1, docs/SPEC.md O1 to O4 (cutover and soak).
**Done when:** README covers installing Syncthing on macOS and Arch, pairing, the folder options mm relies on, and a cutover runbook (update every machine, stop hooks, move the store folder from iCloud Drive into the Syncthing folder, point `storage.path` at it, join the Linux machine with `mm init`, remove a dead machine's directories by hand, delete the iCloud folder two weeks later); AGENTS.md Key Principles and root SPEC.md's storage sections describe the Syncthing folder; `sync.md` and `init-devices.md` drop iCloud-only rules; `pyproject.toml` description and keywords drop iCloud; docs/SPEC.md's superseded-history entry for the store design names §13 (cutover) as evidence alongside §7, §10 and §11; `./bin/check` passes.
_touches: README.md, AGENTS.md, SPEC.md, docs/SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml_
_blocked-by: Track 89A, Track 91A, Track 92A_
_read-first: docs/SPEC.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py_

_Source: docs/SPEC.md O1 to O4 and the accepted manual cutover step; docs/archive/store-v2.md §11 (Linux) and §13 (cutover) as evidence._

- **Runbook and Syncthing setup** -- _README.md, ~250 lines._ (M)
- **Agent-facing docs** -- _AGENTS.md, SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, ~150 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 86 ← {}
- Group 87 ← {86}
- Group 88 ← {87}
- Group 89 ← {87, 88}
- Group 90 ← {89}
- Group 91 ← {87, 90}
- Group 92 ← {91}
- Group 93 ← {89, 91, 92}
```

Track detail per group:

```
Group 86: Pull module
  +-- Track 86A ........... ~L . 2 tasks
Group 87: Syncthing-tolerant reads
  +-- Track 87A ........... ~L . 2 tasks
Group 88: Init module
  +-- Track 88A ........... ~M . 2 tasks
Group 89: Fleet creation and join
  +-- Track 89A ........... ~L . 3 tasks
Group 90: Portable Claude paths
  +-- Track 90A ........... ~L . 2 tasks
Group 91: Fast-forward pulls
  +-- Track 91A ........... ~L . 2 tasks
Group 92: Syncthing status
  +-- Track 92A ........... ~L . 2 tasks
Group 93: Cutover runbook
  +-- Track 93A ........... ~L . 2 tasks
```

**Total: 8 groups . 8 tracks remaining.**

Every Track edits a module another Track also edits, and each move-only Track (86A, 88A) sits just before the first Track that changes the code it moves, per docs/SPEC.md's core-and-extensions constraint, so the packer emits one serial chain: pull module → Syncthing-tolerant reads → init module → fleet creation → portable paths → fast-forward pulls → Syncthing status → cutover runbook. Satisfied prerequisites on shipped Tracks (82A, 84A, 85A, 77A) stay written on the cards that named them.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on the one-week Mac plus Linux soak, with O6 to O8 holding and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance` after following Group 93's runbook. Deferred work, including Codex memory sync and per-path conflict policies, waits in Future with its triggers.

---

## Future

Deferred: docs/roadmap-future.md (17 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
