# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm reports usage for Claude Code, Codex, Grok Build, and Cursor.** Cursor covers standalone runs, legacy Conductor `runs.ndjson` and Conductor SQLite stores (v1.7.0). Cursor usage capture adds no Cursor customization or memory source. Memory capture, recall and sync parity are planned separately in [the memory continuity design](designs/memory-continuity.md). The product authority is [docs/SPEC.md](SPEC.md): the selected target is the owner's fleet on the 2.0 Syncthing store, designed in [store-v2](designs/store-v2.md).

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills. Grok customization sync uses the existing source allowlisting skills/, commands/ and rules/; it is separate from skill-link discovery.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex/Grok session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening either requires a new user scope decision.
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **1.x is frozen at cutover (spec, 2026-10-11).** The GC repair and the scope trim are the last 1.x store changes. New work on the iCloud store, mtime-ordered apply or conflict sidecars is refused; file it against the 2.0 store or kill it.

---

## In Progress

No active branches, sessions or PRs were declared for this regeneration.

## Current Plan

_tombstone: 27, 70_

### Phase 4: mm 2.0 on Syncthing

**End-state:** The owner's macOS and Arch-based Linux machines sync agent context over one Syncthing folder of per-device, write-once encrypted commit logs, with the 1.x iCloud store deleted and the invariants rewritten; docs/SPEC.md outcomes O1 to O7 accepted on a live soak.
**Groups:** 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84

#### Group 71: Last 1.x hazard fix, test guard and the 2.0 foundations

_Depends on: none_

**This group delivers:** the 1.x GC stops reaping peers' blobs and every remedy says the right thing; the test suite can no longer write real mm state; the 2.0 store format and the fake Syncthing transport exist for the Tracks above them.

##### Track 71A: Stop the 1.x GC hazard and correct remedies
_3 tasks . ~220 LOC . medium risk . 12 files_
**Outcome:** The owner can run `mm push` and `mm gc` on the iCloud store without deleting another Mac's blobs, and every remedy mm prints names a step that works on this install.
**Supports:** MVP-1, docs/SPEC.md O1 prerequisite (the 1.x store must stay intact until cutover); release policy: patch.
**Done when:** `_do_gc` lists only `data/<me>/` and references only this device's manifest, pinned by a test where a peer's blobs arrive before its manifest; the root-salt drift error no longer says "Re-run `mm init`"; `unsupported` reader remedies say `mm update`; the lock-contention error links a README anchor through `errors.LOCK_HELD_URL`; `./bin/check` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/lockfile.py, src/mind_meld/errors.py, src/mind_meld/events_tail.py, tests/test_gc_events.py, tests/test_integration.py, tests/test_lockfile.py, tests/test_diag.py, tests/test_docs_routing.py, README.md, docs/invariants/sync.md, docs/invariants/auto-upgrade.md_
_out: 72B, 74A_
_read-first: docs/designs/store-v2.md, docs/invariants/sync.md, docs/invariants/auto-upgrade.md_
_produces: a GC that is safe under out-of-order delivery, and remedy text the 2.0 Tracks can keep_
_session: fresh · effort: medium · verify: ./bin/check tests/test_gc_events.py tests/test_integration.py tests/test_lockfile.py tests/test_diag.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md P0 (hazards verified at 98e9a69 and re-read at a8afa88: `_do_gc` builds `referenced_hashes` from every readable manifest, skips `missing` ones and reaps across all `data/<device>/`; push uploads only to `data/<me>/`, so each manifest references only its own directory); inbox items "Switch every reader's permanent-reason remedy to mm update" and "[plan-devex-review] Link the mm lock-contention error to a README troubleshooting anchor", both 2026-10-09._

- **Reap only this device's blobs** -- Build the reference set from this device's manifest and list only `data/<me>/`; keep the corrupt-manifest and newer-format refusals for this device's manifest only. Pin the hazard: a peer's blobs present with its manifest absent must survive `mm gc` and the post-push GC. Record the rule in the sync invariants. _src/mind_meld/cli.py, tests/test_gc_events.py, tests/test_integration.py, docs/invariants/sync.md, ~90 lines._ (M)
- **Correct the root-salt drift remedy** -- Replace the "Re-run `mm init`" sentence with the safe remedy: wait for the fleet's `mm-crypto-init` to reconcile and run `mm diag`; never re-key. Keep the fingerprint detail. _src/mind_meld/cli.py, tests/test_integration.py, ~30 lines._ (S)
- **Make remedies consistent** -- Replace the `pipx upgrade mind-meld` remedy in `events_tail._host_skip_phrase` and `cli._usage_capture_remedy` with `mm update`, updating the tests that pin the old text. Add `errors.LOCK_HELD_URL` before the final "To see the process, run:" line of the contention message and a README troubleshooting entry for "Another mm operation is running" (wait and rerun; answer a waiting prompt; let an update finish; never delete the lockfile or signal the holder; the `ps -p` recipe; an mm older than 1.5.2 printed "remove <path>": do not). Extend the exact-text, render and `test_error_url_anchors_resolve` tests. _src/mind_meld/events_tail.py, src/mind_meld/errors.py, src/mind_meld/lockfile.py, tests/test_diag.py, tests/test_lockfile.py, tests/test_docs_routing.py, README.md, docs/invariants/auto-upgrade.md, ~100 lines._ (S)

##### Track 71B: Guard real-state writes in the test suite
_2 tasks . ~160 LOC . low risk . 6 files_
**Outcome:** The owner can run the test suite, including the 2.0 simulator, without any test writing to the real `~/.config/mind-meld` or the real storage folder.
**Supports:** MVP-1, docs/SPEC.md O1 (the simulator runs real store code at scale) and the constraint that personal-use data is real data.
**Done when:** a write under the real config directory or the real storage root while `PYTEST_CURRENT_TEST` is set raises at the write site, derived from the account's home (`pwd`), not `HOME`; a test that star-imports a conftest without the `_isolate_*` fixtures fails loudly instead of writing; the routing README names the guard.
_touches: src/mind_meld/fsutil.py, src/mind_meld/lockedjson.py, tests/conftest.py, tests/test_fsutil.py, tests/test_lockedjson.py, docs/invariants/README.md_
_out: 71D_
_read-first: docs/invariants/init-devices.md, tests/conftest.py_
_produces: a write-site guard every later test module inherits without a fixture_
_session: fresh · effort: medium · verify: ./bin/check tests/test_fsutil.py tests/test_lockedjson.py tests/test_docs_routing.py_

_Source: inbox "[manual] Structural guard: integration tests must never write real ~/.config/mind-meld state", 2026-10-09. Manifested: on 2026-10-08 a worktree test run wrote 252 crypto errors into the real autopush/autopull logs and overwrote `last-autorun.json`; on 2026-10-11 `mm status` on device 889e42c0 still reports that phantom crypto error._

- **Fail loudly at the write site** -- Add one guard in the shared write helpers (`fsutil` atomic writes and `lockedjson`) that refuses paths under the real config directory and the real storage root when a test is running, following the `PYTEST_CURRENT_TEST` pattern already used by `crypto.store_passphrase_in_keyring`. Derive the home from the account database, never from `HOME`. _src/mind_meld/fsutil.py, src/mind_meld/lockedjson.py, ~100 lines._ (M)
- **Pin the bypass** -- Add a test that imports the CLI through a conftest-free path and proves the guard fires; document the guard beside the keyring guard in the routing README. _tests/conftest.py, tests/test_fsutil.py, tests/test_lockedjson.py, docs/invariants/README.md, ~60 lines._ (S)

##### Track 71C: Store format: fleet header, envelope 0x03 and codecs
_2 tasks . ~600 LOC . medium risk . 6 files_
**Outcome:** mm can seal and open 2.0 commits, snapshots and presence records that only the fleet's passphrase can read and that cannot be renamed, moved or replayed.
**Supports:** MVP-1, docs/SPEC.md O4 and O5.
**Done when:** `fleet.json` (format 3) round-trips with the keycheck; envelope 0x03 seals gzip payloads under per-kind subkeys with associated data `(fleet_id, device_id, kind, seq, part, nparts)` and a file renamed, moved between devices or replayed under another number fails to open; commit, snapshot and presence codecs round-trip with length-prefixed frames, the `prev` hash, `host` fingerprint and 8 MiB parts; the strict layout parser accepts only the §1 grammar and counts everything else as foreign; `store/` imports nothing from the CLI layer.
_touches: src/mind_meld/store/ (new), src/mind_meld/crypto.py, tests/test_store_format.py (new), tests/test_crypto.py, tests/test_module_boundaries.py, docs/invariants/store.md (new)_
_out: 73A, 75A_
_read-first: docs/designs/store-v2.md, docs/invariants/init-devices.md, src/mind_meld/crypto.py_
_produces: `store/` codecs and layout helpers every publish and apply Track builds on_
_session: fresh · effort: high · verify: ./bin/check tests/test_store_format.py tests/test_crypto.py tests/test_module_boundaries.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §1, §2 and T3._

- **Fleet header and envelope** -- Implement `fleet.json` with Argon2id parameters, salt and keycheck; derive per-kind subkeys from the master key; seal with AES-256-GCM, random nonce and bound associated data. Keep 1.x envelope 0x02 readable until Track 83A deletes it. _src/mind_meld/crypto.py, src/mind_meld/store/envelope.py, ~250 lines._ (M)
- **Codecs, parts and layout parser** -- Commit, snapshot and presence payload codecs with the §2 headers and entry frames; split payloads over 8 MiB into `NofM` parts; a layout parser for `dev/<device>/log|snap|presence` that treats conflict copies of commits or `fleet.json` as fork signals. Start `docs/invariants/store.md` with the format rules. _src/mind_meld/store/, tests/test_store_format.py, docs/invariants/store.md, ~350 lines._ (M)

##### Track 71D: Fake Syncthing transport and device harness
_2 tasks . ~500 LOC . medium risk . 1 files_
**Outcome:** The 2.0 Tracks can run N simulated devices against a transport that behaves like Syncthing, in one process, under `./bin/check`.
**Supports:** MVP-1, docs/SPEC.md O1 and O3 (verification gates the work).
**Done when:** the transport delivers whole files in random order with arbitrary delay, models the delete-then-rename ENOENT window, deletions, offline devices, a disk restored to an earlier copy, a cloned config and conflict copies on concurrent writes; each device has its own home, state and replica; a smoke test moves one file between two devices.
_touches: tests/sim/ (new)_
_out: 72A_
_read-first: docs/designs/store-v2.md, tests/conftest.py_
_produces: `tests/sim/` transport and harness that the state machine and every store Track reuse_
_session: fresh · effort: medium · verify: ./bin/check tests/sim_

_Source: docs/designs/store-v2.md §12 and T1._

- **Transport model** -- A fake replicated folder with per-file delivery, reordering, delay, the ENOENT replace window, deletion propagation, offline peers and `.sync-conflict-*` minting on concurrent writes to one path. _tests/sim/transport.py, ~250 lines._ (M)
- **Device harness** -- Per-device `tmp_path` home, config and storage replica; helpers to edit, delete, push, pull, restore from an earlier copy and clone a device, calling the real CLI entry points. _tests/sim/devices.py, ~250 lines._ (M)

#### Group 72: Scope trim and the replication invariants

_Depends on: Group 71_

**This group delivers:** generator output stops syncing on 1.x before any 2.0 snapshot exists, and the replication state machine asserts the six store invariants.

##### Track 72A: Replication state machine and invariants
_2 tasks . ~400 LOC . medium risk . 2 files_
**Outcome:** Every later store Track is gated by a property test that interleaves edits, deletes, pushes, pulls, compactions, joins, retires and delivery steps and asserts convergence, no silent loss, single writer, ordering never fails, termination and fork safety.
**Supports:** MVP-1, docs/SPEC.md O1, O3 and O5.
**Done when:** a `hypothesis` `RuleBasedStateMachine` over the harness asserts invariants 1 to 6 against the store interface (stubs until Tracks 75A to 78A land); a bounded run gates CI and a deep run sits behind an environment variable.
_touches: tests/sim/ (new), .github/workflows/ci.yml_
_blocked-by: Track 71D_
_out: 75A_
_read-first: docs/designs/store-v2.md, tests/sim/_
_produces: the invariant suite every store Track must keep green_
_session: fresh · effort: medium · verify: ./bin/check tests/sim_

_Source: docs/designs/store-v2.md §12 and T1._

- **State machine with invariants 1 to 6** -- Actions: edit, delete, push, pull, compaction, join, retire, delivery step. Invariants: convergence after full delivery, no silent loss (every authored version current, dominated by a version whose author saw it, or in `displaced`), single writer, ordering never fails, termination, fork safety. _tests/sim/test_replication.py, ~350 lines._ (M)
- **Bounded CI run and deep run** -- Wire the bounded profile into the portable job and the deep profile behind `MM_SIM_DEEP=1`. _.github/workflows/ci.yml, ~50 lines._ (S)

##### Track 72B: Trim sync scope with default generator exclusions
_3 tasks . ~200 LOC . low risk . 8 files_
**Outcome:** The owner's gstack source stops uploading virtualenvs, app bundles, archives, QA evidence and fixtures, which cuts the synced set by about a third before the first 2.0 snapshot.
**Supports:** MVP-1, docs/SPEC.md O6; release policy: minor.
**Done when:** the default gstack `exclude_patterns` include `*/venv/*`, `*/.venv/*`, `*/node_modules/*`, `*.app/*`, `*/tools/*`, `*/fixture/*`, `*/fixtures/*`, `*/.qa-evidence/*` and the archive or binary extensions `*.zip`, `*.tar*`, `*.whl`, `*.so`, `*.dylib`, `*.pak`, `*.dmg`; `mm migrate-config` appends the missing ones and authors no tombstones; the README and sync invariants record the 2026-10-11 measurements; the review-receipt decision (store-v2 decision 6) is recorded either way.
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, tests/test_config.py, tests/test_integration.py, tests/test_source_toggle.py, tests/test_docs_routing.py, README.md, docs/invariants/sync.md_
_blocked-by: Track 71A_
_out: 73A, 73B, 74A_
_read-first: docs/designs/store-v2.md, docs/invariants/sync.md_
_produces: a sync set the 2.0 snapshots can afford_
_session: fresh · effort: medium · verify: ./bin/check tests/test_config.py tests/test_integration.py tests/test_source_toggle.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md P1 and "P1 demand" (measured 2026-10-11 on device 889e42c0: gstack is 32,615 of 33,518 synced files and 1.22 of 1.26 GB; venvs 62 MB, app bundles 59 MB, `tools/` 43 MB, archives and binaries 106 MB, QA evidence 19 MB; 178 files over 1 MiB hold 690 MB). Lowering `max_file_size` is not a lever: an oversize selected file refuses the whole push._

- **Default generator globs** -- Add the globs to the gstack defaults in `config.py` with a comment citing the measurement; exclusion authors no tombstones (sync invariants, tombstone suppression), so peers keep their copies. Decide `*/compact-snapshots/*` and `*/ranking-summaries/*` after checking whether any skill reads those receipts on another Mac; record the answer. _src/mind_meld/config.py, tests/test_config.py, ~80 lines._ (M)
- **migrate-config appends them** -- Extend the recommended-exclusion append so an existing config gains the new globs idempotently, preserving user entries; pin tombstone suppression for the newly excluded paths. _src/mind_meld/cli.py, tests/test_integration.py, tests/test_source_toggle.py, ~60 lines._ (S)
- **Document the measured demand** -- README configuration section and the "Generated files are not sync data" invariant gain the new globs and the numbers behind them. _README.md, docs/invariants/sync.md, tests/test_docs_routing.py, ~60 lines._ (S)

#### Group 73: Linux platform and local state

_Depends on: Group 71, Group 72_

**This group delivers:** mm runs its portable checks on Linux with a desktop keyring that hooks never prompt, and local sync state moves into SQLite.

##### Track 73A: Linux as a first-class platform
_2 tasks . ~300 LOC . medium risk . 9 files_
**Outcome:** The owner can install mm on an Arch-based Linux desktop, keep the passphrase in the Secret Service keyring, and have hooks skip quietly when the collection is locked.
**Supports:** MVP-1, docs/SPEC.md supported conditions (macOS and Arch-based Linux) and O2.
**Done when:** the default storage path is `~/Sync/mind-meld` on both OSes; auto commands on Linux check the collection's lock state first and record a `keyring-locked` breadcrumb instead of prompting; backend transport errors are caught; remedies name `secret-tool` beside `security`; an `ubuntu-latest` job runs `./bin/check` with the `F_FULLFSYNC` test fix and `~/Sync` in the protected-paths list; the Keychain and wheel smokes stay on macOS.
_touches: src/mind_meld/crypto.py, src/mind_meld/config.py, .github/workflows/ci.yml, pyproject.toml, tests/test_crypto.py, tests/test_fsutil.py, tests/test_memory_contract.py, README.md, docs/invariants/init-devices.md_
_blocked-by: Track 71C, Track 72B_
_out: 74A, 83A_
_read-first: docs/designs/store-v2.md, docs/invariants/init-devices.md, .github/workflows/ci.yml_
_produces: a Linux-qualified keyring path and a portable CI job_
_session: fresh · effort: medium · verify: ./bin/check tests/test_crypto.py tests/test_fsutil.py tests/test_memory_contract.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §11 and T8. The spec commits to Arch-based Linux (Omarchy); CI's Ubuntu runner is the portable Python check, not a second supported distribution._

- **Keyring and default path** -- Secret Service lock-state check in auto commands, transport-error handling, `secret-tool` remedies, the `~/Sync/mind-meld` default and `pyproject.toml` description and classifiers. The `brctl` auto-pin removal rides Track 83A. _src/mind_meld/crypto.py, src/mind_meld/config.py, pyproject.toml, tests/test_crypto.py, README.md, docs/invariants/init-devices.md, ~180 lines._ (M)
- **Portable CI on Linux** -- Add the `ubuntu-latest` portable job; fix `tests/test_fsutil.py` to set `F_FULLFSYNC` with `raising=False`; add `~/Sync` to the memory-contract harness's protected paths. _.github/workflows/ci.yml, tests/test_fsutil.py, tests/test_memory_contract.py, ~120 lines._ (M)

##### Track 73B: Local state database
_2 tasks . ~500 LOC . medium risk . 8 files_
**Outcome:** mm keeps its ledger, cursors, own-log, displaced versions and history in one SQLite file, and `mm log` reads from it.
**Supports:** MVP-1, docs/SPEC.md O1 (the ledger is what makes fast-forwards possible).
**Done when:** `statedb.py` is a leaf module opening `~/.config/mind-meld/state.db` with WAL, `busy_timeout`, mode 0600 and a `user_version` forward-only migration; the `meta`, `ledger`, `cursors`, `own_log`, `displaced` and `history` tables exist with their documented readers; `pull-history.jsonl{,.1}` is imported once, best effort; `mm log` reads `history`, and push `uploaded` rows can no longer crowd pull outcomes out of it; the module boundary test covers it.
_touches: src/mind_meld/statedb.py (new), src/mind_meld/pullhistory.py, src/mind_meld/cli.py, tests/test_statedb.py (new), tests/test_pullhistory.py, tests/test_integration.py, docs/invariants/sync.md, docs/invariants/README.md_
_blocked-by: Track 72B_
_out: 75A_
_read-first: docs/designs/store-v2.md, src/mind_meld/lockedjson.py, src/mind_meld/pullhistory.py_
_produces: the `statedb` API the publish and apply Tracks write through_
_session: fresh · effort: high · verify: ./bin/check tests/test_statedb.py tests/test_pullhistory.py tests/test_integration.py tests/test_module_boundaries.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §3 and T2; Future bullet "Push `uploaded` rows crowd pull outcomes out of pull-history" (promoted: the `history` table is the policy it asked for)._

- **Schema and migrations** -- Tables, indexes, `user_version` migrations, connection helper honoring the mm lockfile, and the `displaced` 30-day expiry. _src/mind_meld/statedb.py, tests/test_statedb.py, docs/invariants/sync.md, docs/invariants/README.md, ~300 lines._ (M)
- **History import and mm log** -- Import both rotation files once; keep upload rows in a bounded summary so pull outcomes survive; point `mm log` at the table and keep `pullhistory` writers until Track 83A removes them. _src/mind_meld/cli.py, src/mind_meld/pullhistory.py, tests/test_pullhistory.py, tests/test_integration.py, ~200 lines._ (M)

#### Group 74: Durable-write failure handling

_Depends on: Group 72, Group 73_

**This group delivers:** init cleanup consistent with visible config, non-fatal crypto backfill failures, and handled recover failures without losing prior evidence.

##### Track 74A: Preserve coherent state after config publication failures
_2 tasks . ~250 LOC . medium risk . 9 files_
**Outcome:** An init whose config save fails after publication keeps a registration that matches the visible config, and crypto backfill or recover storage faults warn instead of masking the original error.
**Supports:** MVP-1, docs/SPEC.md O5 (recovery must not corrupt state); carried forward from the 2026-10-06 plan.
**Done when:** `_register_and_save` no longer deletes `devices/<id>.json` on every save exception but only when the config is proven unpublished; crypto backfill tolerates the helper's `StorageError` with an always-stderr warning; `recover` reports a handled quarantine failure; the failure contract documents are corrected; the routing README names any new invariant.
_touches: src/mind_meld/cli.py, src/mind_meld/config.py, tests/test_config.py, tests/test_integration.py, tests/test_recover.py, tests/test_crypto.py, tests/test_silent_failure_contract.py, docs/invariants/sync.md, docs/invariants/init-devices.md_
_blocked-by: Track 72B_
_read-first: docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, src/mind_meld/fsutil.py_
_produces: init cleanup consistent with visible config, non-fatal crypto backfill failures, and handled recover failures without losing prior evidence_
_session: fresh · effort: high · verify: ./bin/check tests/test_config.py tests/test_integration.py tests/test_recover.py tests/test_crypto.py tests/test_silent_failure_contract.py tests/test_fsutil.py tests/test_docs_routing.py_

_Source: [plan-eng-review:severity=medium,files=src/mind_meld/cli.py|src/mind_meld/config.py] and [review:severity=low,files=src/mind_meld/cli.py], Track 69B's separately filed caller defects, 2026-10-03/04; inbox "[review:severity=minor] Check Track 71A's invariant documentation routing", 2026-10-09 (routing README added to read-first). Re-verified at a8afa88 on 2026-10-11: `_register_and_save` still runs `backend.delete(dev_key)` inside `except Exception` on any save failure; `_init_crypto_session` backfill catches `OSError` and `ConfigError` but not `StorageError`. Formerly Track 71A (2026-10-06 plan); renamed by this regeneration._

- **Preserve registration when config may already be published** -- Reproduce pre-replacement file-flush and post-replacement parent-flush failures in isolated state. Make the cleanup decision reflect publication instead of treating every save exception as proof that the local pointer is absent; preserve existing pre-publication cleanup and the original failure. Define conservative handling for unreadable or ambiguous current state without claiming durability or rolling back published bytes. Verify init retry, storage guard and later registration self-heal with isolated Keychain substitutes, then correct the affected failure contract. _src/mind_meld/cli.py, src/mind_meld/config.py and declared tests/docs, ~170 lines._ (M)
- **Handle helper write errors at their intended boundaries** -- Make crypto backfill tolerate the helper's `StorageError` with an always-stderr warning, and make recover report its handled quarantine failure. Cover pre- and post-publication faults and a retry after a visible quarantine copy; preserve source bytes, prior copies and exit semantics. Do not infer the publication stage from exception messages or claim that a raised write restored old state. _src/mind_meld/cli.py and declared tests/docs, ~80 lines._ (M)

#### Group 75: Publish commits

_Depends on: Group 71, Group 72, Group 73, Group 74_

**This group delivers:** `mm push` authors version-vectored entries from the ledger and writes self-contained, write-once commits to `dev/<me>/log/`.

##### Track 75A: Publish: ledger authoring and commits
_1 tasks . ~500 LOC . high risk . 8 files_
**Outcome:** The owner's `mm push` writes one sealed commit per change set to this device's log, with content that travels inside the commit and a `prev` chain.
**Supports:** MVP-1, docs/SPEC.md O1 and O4.
**Done when:** the §4 authoring table is implemented against the ledger (`{me: 1}` for new paths, incremented vectors for changes, tombstones for deletions guarded by `_prove_omitted_paths_absent`); the commit is written with exclusive create via `os.link` from `.mm-tmp-*` temp files, parts last; ledger, `own_log` and `history` rows commit in one transaction; the scan reuses the strict walkers, exclusions, nested-checkout freezing and upload verification; the simulator's publish actions use it.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/manifest.py, src/mind_meld/statedb.py, tests/test_publish.py (new), tests/sim/, tests/test_integration.py, docs/invariants/store.md_
_blocked-by: Track 73B, Track 71C, Track 72A, Track 74A_
_out: 76A_
_read-first: docs/designs/store-v2.md, docs/invariants/store.md, docs/invariants/sync.md, src/mind_meld/manifest.py_
_produces: `store.publish` the snapshot and apply Tracks extend_
_session: fresh · effort: high · verify: ./bin/check tests/test_publish.py tests/sim tests/test_integration.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §4 (authoring), §5 and T4. Tracks 75A and 77A share `cli.py` and `store/`, so their landings are serialized by the Group chain._

- **Author and write commits** -- Scan strictly, diff against the ledger, author entries and tombstones, build the commit with `prev` and the host fingerprint, write parts with exclusive create, update the ledger in one transaction, refresh presence when something changed; preview (`--dry-run`) reports what would be authored and writes nothing. _src/mind_meld/store/publish.py, src/mind_meld/cli.py, src/mind_meld/manifest.py, src/mind_meld/statedb.py, tests/test_publish.py, tests/sim/, tests/test_integration.py, docs/invariants/store.md, ~500 lines._ (L)

#### Group 76: Snapshots and restore safety

_Depends on: Group 75_

**This group delivers:** devices snapshot and compact their own logs on the ratio rule from attended pushes only, and a restored, crashed or cloned device is detected before it publishes.

##### Track 76A: Snapshots, compaction and own-storage reconciliation
_2 tasks . ~350 LOC . high risk . 6 files_
**Outcome:** A device's log stays bounded without any cross-device acknowledgement, and a machine restored from backup or cloned cannot fork the fleet.
**Supports:** MVP-1, docs/SPEC.md O5 and the accepted snapshot limitation.
**Done when:** a snapshot is written when the log since the last one outgrows that snapshot or after 200 commits, only from an attended `mm push` or `mm push --snapshot`, never from `autopush`; commits at or below the previous snapshot's seq are deleted after 14 days and the two newest snapshots are kept; the snapshot header carries `cursors` and `prev`; chain checks run only inside the retained window; the §9 table is implemented (own commits beyond `own_log` with the same host are applied and `seq` resumes after storage's newest commit; a different host refuses with `mm rejoin`; a missing `dev/<me>/` with a non-empty `own_log` refuses; a broken chain or conflict copy refuses with `mm diag` detail).
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_publish.py, tests/sim/, docs/invariants/store.md_
_blocked-by: Track 75A_
_out: 77A_
_read-first: docs/designs/store-v2.md, docs/invariants/store.md_
_produces: bounded logs and the restore, clone and fork rules the apply Tracks rely on_
_session: fresh · effort: high · verify: ./bin/check tests/test_publish.py tests/sim tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §6, §9 and T4 (snapshot half). The time trigger was removed on 2026-10-11: a snapshot is about 330 MB sealed on device 889e42c0 and Syncthing gets no block deltas from sealed files._

- **Snapshots and compaction** -- Ratio and count triggers, attended-only, `--snapshot`, retention of the two newest snapshots, 14-day deletion of compacted commits, chain checks bounded to the retained window. _src/mind_meld/store/snapshot.py, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_publish.py, tests/sim/, docs/invariants/store.md, ~250 lines._ (M)
- **Own-storage reconciliation** -- At the start of push and pull read the own log listing and presence and apply the §9 table; the simulator's restore and clone actions must refuse or recover as specified. _src/mind_meld/store/publish.py, tests/sim/, ~100 lines._ (S)

#### Group 77: Apply peers' logs

_Depends on: Group 76_

**This group delivers:** `mm pull` reads each peer's commits in sequence, bootstraps from snapshots, and reports gaps as pending.

##### Track 77A: Apply cursors, gap rule and snapshot bootstrap
_2 tasks . ~350 LOC . high risk . 7 files_
**Outcome:** The owner's `mm pull` applies every peer's commits in order, picks up from a snapshot when older commits were compacted, and says "waiting for push #N from X" instead of failing when something has not arrived.
**Supports:** MVP-1, docs/SPEC.md O3.
**Done when:** the §6 reader rule is implemented per peer (apply the next complete commit; load the newest complete snapshot when the next commit is missing at or below its seq and raise every other cursor to the snapshot's cursors; record `gap_since` when pending; stop with an always-visible warning on an unreadable commit and skip to a later snapshot); a pending gap older than 24 h warns in every mode; a joiner bootstraps from the freshest complete peer snapshot; simulator invariant 4 holds.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_apply.py (new), tests/sim/, tests/test_integration.py, docs/invariants/store.md_
_blocked-by: Track 76A_
_out: 78A_
_read-first: docs/designs/store-v2.md, docs/invariants/store.md_
_produces: `store.apply` cursors the classification Track feeds entries through_
_session: fresh · effort: high · verify: ./bin/check tests/test_apply.py tests/sim tests/test_integration.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §6 (reader rule), §8 (join bootstrap) and T5 (cursor half)._

- **Cursors and the reader rule** -- Per-peer cursor advance, the snapshot fallback, `gap_seq` and `gap_since`, unreadable-commit handling and the 24 h data-at-risk warning. _src/mind_meld/store/apply.py, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_apply.py, tests/sim/, docs/invariants/store.md, ~250 lines._ (M)
- **Snapshot bootstrap** -- Select the freshest complete peer snapshot for a device with no cursors, classify every entry through the next Track's rules, and raise cursors from the snapshot header. _src/mind_meld/store/apply.py, tests/test_apply.py, tests/test_integration.py, ~100 lines._ (S)

#### Group 78: Causal classification

_Depends on: Group 77_

**This group delivers:** peer edits to untouched files fast-forward, concurrent edits resolve symmetrically, deletions propagate, and replaced bytes land in `displaced`.

##### Track 78A: Causal classification, symmetric resolution and displaced
_1 tasks . ~500 LOC . high risk . 7 files_
**Outcome:** The owner sees a conflict only when two machines really changed the same file; everything else applies as a fast-forward, deletions stick, and nothing replaced is lost for 30 days.
**Supports:** MVP-1, docs/SPEC.md O1.
**Done when:** the §4 classification table is implemented (same sha merges vectors; dominance fast-forwards or deletes with the old bytes in `displaced`; local dominance ignores; concurrency resolves); resolution is `merge_jsonl` for `.jsonl`, `merge_lines` for `MEMORY.md`, newest by mtime with the sha tiebreak otherwise, live wins over a tombstone, and the bootstrap inversion moves never-ledgered local files matching a peer tombstone to `displaced`; every resolution is a pure function of the unordered pair and the result is authored with `max(L, R)` plus `me`; `mm displaced` lists and restores entries; simulator invariants 1, 2 and 5 hold.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/merge.py, src/mind_meld/statedb.py, tests/test_apply.py, tests/sim/, docs/invariants/store.md_
_blocked-by: Track 77A_
_out: 79A_
_read-first: docs/designs/store-v2.md, docs/designs/conflict-policies.md, docs/invariants/conflicts.md, src/mind_meld/merge.py_
_produces: the symmetric resolver contract conflict policies (2.1) extend_
_session: fresh · effort: high · verify: ./bin/check tests/test_apply.py tests/sim tests/test_merge.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §4 (classification, resolution, symmetry, bootstrap) and T5 (apply half). The live-versus-tombstone rule and the bootstrap inversion were added on 2026-10-11 after the fleet's 15,415 tombstones were measured._

- **Classify and resolve** -- Version-vector comparison against the ledger plus unpublished local edits, the four actions, the three symmetric resolvers, the tombstone rules, the `displaced` writes and `mm displaced`; the merge module loses its LCS synthetic base once no caller remains. _src/mind_meld/store/apply.py, src/mind_meld/merge.py, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_apply.py, tests/sim/, docs/invariants/store.md, ~500 lines._ (L)

#### Group 79: Portable paths and previews

_Depends on: Group 78_

**This group delivers:** Claude project folders sync between macOS and Linux homes, and `mm diff` and `--dry-run` work on the new store without writing.

##### Track 79A: Portable Claude paths and write-free previews
_2 tasks . ~300 LOC . medium risk . 7 files_
**Outcome:** The owner's Claude project memory for `~/src/<project>` is the same folder on a Mac and a Linux box, and previews show what a pull would do on the 2.0 store.
**Supports:** MVP-1, docs/SPEC.md O1 (cross-OS) and the preview write boundaries.
**Done when:** a `claude` project folder whose encoded name starts with the encoded home prefix is published as `projects/~-<rest>` and mapped back on apply; paths outside home stay absolute; `mm diff`, `mm pull --dry-run` and `mm push --dry-run` read the new store and change nothing but the lock file; `pullplan` predictions use cursors and vectors instead of mtimes.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/pullplan.py, src/mind_meld/manifest.py, tests/test_pull_helpers.py, tests/test_integration.py, docs/invariants/store.md_
_blocked-by: Track 78A_
_out: 80A_
_read-first: docs/designs/store-v2.md, docs/invariants/sync.md, src/mind_meld/pullplan.py_
_produces: portable wire paths and previews the fleet Tracks can demonstrate_
_session: fresh · effort: medium · verify: ./bin/check tests/test_pull_helpers.py tests/test_integration.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §7 and T5 (preview half). On device 889e42c0 all 258 synced Claude project files live under `-Users-kb-`; 111 of 160 project folders are worktree paths that stay machine-specific by nature._

- **Home-relative wire paths** -- Encode and decode the `~-` prefix for the `claude` source on both OSes; leave other sources root-relative. _src/mind_meld/manifest.py, src/mind_meld/store/, tests/test_pull_helpers.py, ~120 lines._ (M)
- **Previews on the new store** -- Port `diff`, `pull --dry-run` and `push --dry-run` to cursors and the ledger; keep `COMMAND_INTENTS62` write boundaries. _src/mind_meld/pullplan.py, src/mind_meld/cli.py, tests/test_integration.py, docs/invariants/store.md, ~180 lines._ (M)

#### Group 80: Create and join a fleet

_Depends on: Group 79_

**This group delivers:** a fleet is created once and every other machine joins with one code.

##### Track 80A: Create and join a fleet
_1 tasks . ~500 LOC . high risk . 8 files_
**Outcome:** The owner runs `mm init --new-fleet` on one machine and `mm join <code>` on each other, and the joiner never creates `fleet.json`.
**Supports:** MVP-1, docs/SPEC.md O2.
**Done when:** `--new-fleet` refuses unless the folder holds no `fleet.json` and no `dev/`, prompts for the passphrase twice, writes `fleet.json` exclusively and publishes a snapshot and presence; `mm invite` prints a base32 code (fleet id, this device's Syncthing id, folder id, checksum) and waits for the joiner to appear; `mm join` waits for `fleet.json`, verifies the keycheck, records the fleet in config, bootstraps from the freshest peer snapshot, publishes its own snapshot and pulls; `mm rejoin` mints a new device id in the same fleet; the Syncthing pairing steps call the next Track's module or report its absence.
_touches: src/mind_meld/cli.py, src/mind_meld/store/, src/mind_meld/config.py, src/mind_meld/crypto.py, tests/test_fleet.py (new), tests/test_integration.py, README.md, docs/invariants/init-devices.md_
_blocked-by: Track 79A_
_out: 81A_
_read-first: docs/designs/store-v2.md, docs/invariants/init-devices.md_
_produces: fleet creation and join flows the presence and Syncthing Tracks complete_
_session: fresh · effort: high · verify: ./bin/check tests/test_fleet.py tests/test_integration.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §8 and T6 (lifecycle half)._

- **new-fleet, invite, join and rejoin** -- The four commands, the invite code format, the config `[fleet]` record, the passphrase and keycheck flow, and the init-time refusal rules; previews of each must preserve their write boundaries. _src/mind_meld/cli.py, src/mind_meld/store/fleet.py, src/mind_meld/config.py, src/mind_meld/crypto.py, tests/test_fleet.py, tests/test_integration.py, README.md, docs/invariants/init-devices.md, ~500 lines._ (L)

#### Group 81: Presence and retirement

_Depends on: Group 80_

**This group delivers:** `mm status` says which machines have the latest push, and dead devices can be retired safely.

##### Track 81A: Presence, delivery receipts and device retirement
_2 tasks . ~350 LOC . medium risk . 7 files_
**Outcome:** The owner sees "laptop has your latest push; desktop is 2 pushes behind, last active 3h ago" in `mm status`, `mm devices` keeps working for retro-fleet, and a Mac that has not pushed for 30 days can be retired.
**Supports:** MVP-1, docs/SPEC.md O3 and O7.
**Done when:** presence is rewritten atomically at the end of push and pull when something changed and at most hourly otherwise; `mm status` renders delivery receipts and pending gaps per peer; `mm devices --format json` projects presence onto `device_id`, `device_name`, `registered`, `last_seen` and `last_seen_version` so retro-fleet's subprocess reader is unchanged; `mm devices retire <id>` requires 30 days of inactivity and every active device's newest snapshot to cover the device's last seq, then deletes `dev/<id>/`.
_touches: src/mind_meld/cli.py, src/mind_meld/store/, src/mind_meld/devices.py, tests/test_fleet.py, tests/test_integration.py, README.md, docs/invariants/init-devices.md_
_blocked-by: Track 80A_
_out: 82A_
_read-first: docs/designs/store-v2.md, docs/invariants/init-devices.md, docs/invariants/events-retro.md_
_produces: presence records the Syncthing Track reads for mesh healing_
_session: fresh · effort: medium · verify: ./bin/check tests/test_fleet.py tests/test_integration.py tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §2 (presence), §8 (retire), §10 (status) and T6 (presence half); Future bullet "Deregister/prune retired devices" (promoted: `mm devices retire` is the prune it asked for)._

- **Presence and receipts** -- Presence codec writes, the status renderer, the `devices --format json` projection and its contract test. _src/mind_meld/store/presence.py, src/mind_meld/cli.py, src/mind_meld/devices.py, tests/test_fleet.py, tests/test_integration.py, README.md, ~250 lines._ (M)
- **mm devices retire** -- The inactivity and snapshot-coverage checks, the only write outside `dev/<me>/`, and the retired device's own refusal on its next run. _src/mind_meld/cli.py, tests/test_fleet.py, docs/invariants/init-devices.md, ~100 lines._ (S)

#### Group 82: Syncthing integration

_Depends on: Group 81_

**This group delivers:** mm sets up and lints the Syncthing folder, heals the mesh, and asks Syncthing to scan after every push.

##### Track 82A: Syncthing integration
_2 tasks . ~450 LOC . medium risk . 7 files_
**Outcome:** The owner pairs each machine with one other and mm fills in the rest of the mesh; `mm status` and `mm diag` report Syncthing's state and the folder settings.
**Supports:** MVP-1, docs/SPEC.md O2 and O3.
**Done when:** `syncthing.py` is a leaf module that discovers the API key and address from `config.xml` on both OSes (falling back to `syncthing paths`) and works, with status saying so, when Syncthing is absent; folder setup applies `~/Sync/mind-meld`, `sendreceive`, versioning off, `maxConflicts` 10, `ignorePerms=false`, `order=alphabetic` and the `.stignore`; push posts a time-bounded `db/scan` for `dev/<me>`; pull adds any fleet device whose presence carries an unknown Syncthing id unless `[syncthing] manage = false`; `mm relay add` records a `receiveonly` peer; `mm diag` lints the folder settings, foreign files, fork signals and the Syncthing version.
_touches: src/mind_meld/syncthing.py (new), src/mind_meld/cli.py, tests/test_syncthing.py (new), tests/test_module_boundaries.py, tests/test_diag.py, README.md, docs/invariants/README.md_
_blocked-by: Track 81A_
_out: 83A_
_read-first: docs/designs/store-v2.md, docs/invariants/README.md, src/mind_meld/upgrade.py_
_produces: the Syncthing automation the cutover runbook relies on_
_session: fresh · effort: high · verify: ./bin/check tests/test_syncthing.py tests/test_module_boundaries.py tests/test_diag.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §10 and T7. Syncthing option semantics verified against docs.syncthing.net on 2026-10-11 (`maxConflicts` 0 disables conflict copies; `order` accepts `alphabetic`). Untrusted peers stay excluded while syncthing#10528 and #10424 are open._

- **Discovery, folder setup and scan-after-push** -- Config discovery, REST client with timeouts, folder creation and lint, `.stignore`, and the post-push scan. _src/mind_meld/syncthing.py, tests/test_syncthing.py, tests/test_module_boundaries.py, ~250 lines._ (M)
- **Mesh healing, relay, status and diag** -- Pending-device acceptance during `invite`, mesh healing during pull, `mm relay add`, and the status and diag sections. _src/mind_meld/syncthing.py, src/mind_meld/cli.py, tests/test_diag.py, README.md, docs/invariants/README.md, ~200 lines._ (M)

#### Group 83: Remove the 1.x store

_Depends on: Group 73, Group 82_

**This group delivers:** the iCloud-era store, mtime-ordered apply and conflict sidecars are gone from the code.

##### Track 83A: Delete the 1.x store and mtime-ordered apply
_3 tasks . ~2500 LOC . medium risk . 11 files_
**Outcome:** mm 2.0 ships without the 1.x store, so no code path can read, repair or delete iCloud-era storage.
**Supports:** MVP-1, docs/SPEC.md constraint "2.0 never reads 1.x storage" and the 1.x freeze.
**Done when:** the `data/` blob store, `devices/` registry, `devices-write.lock`, crypto-init fetch and repair, `_do_gc` orphan reaping, last-push sidecar and corrupt-manifest recovery, `mm recover --abandon-manifest`, `brctl` auto-pin, the mtime skip gate, deferred inline bumps, `_bump_canonical_mtime_post_resolve`, `pullplan` mtime predictions, `.sync-conflict-*` minting, the v0/v1 eras, owner parsing, `_gc_old_conflict_files` and promote are deleted; the walker keeps a global exclusion for legacy `*.sync-conflict-*` names; envelope 0x02 reading is gone; their tests are deleted or rewritten; the module boundary test still passes.
_touches: src/mind_meld/cli.py, src/mind_meld/storage/, src/mind_meld/crypto.py, src/mind_meld/retention.py, src/mind_meld/conflictmtime.py, src/mind_meld/pullplan.py, src/mind_meld/resolveflow.py, src/mind_meld/devices.py, src/mind_meld/merge.py, src/mind_meld/manifest.py, tests/_
_blocked-by: Track 82A, Track 73A_
_out: 84A_
_read-first: docs/designs/store-v2.md, docs/invariants/README.md, docs/invariants/conflicts.md_
_produces: a code base the invariant rewrite can describe truthfully_
_session: fresh · effort: high · verify: ./bin/check_

_Source: docs/designs/store-v2.md "What 2.0 deletes" and T9 (code half). 2.0 has no `ask` outcome: unresolved concurrency takes the displaced newest-wins path until 2.1._

- **Delete the store** -- Remove `storage/local.py` conflict-copy detection, the blob store, the device registry and its lock, crypto-init fetch, repair plans and winner selection, `_do_gc` orphan reaping, the last-push sidecar and recovery chain, `recover --abandon-manifest` and `brctl` auto-pin. _src/mind_meld/storage/, src/mind_meld/cli.py, src/mind_meld/crypto.py, src/mind_meld/devices.py, ~1500 lines (del)._ (S)
- **Delete mtime apply and sidecars** -- Remove the skip gate, deferred bumps, post-resolve mtime bump, mtime predictions, sidecar minting, eras, owner parsing, conflict GC and promote; keep the legacy-name walker exclusion. _src/mind_meld/conflictmtime.py, src/mind_meld/resolveflow.py, src/mind_meld/retention.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, src/mind_meld/manifest.py, ~800 lines (del)._ (S)
- **Retire their tests** -- Delete or rewrite the tests that pinned the removed paths, keep the compat fixtures only where a refusal test still reads them, and keep `COMMAND_INTENTS62` current for the removed and added commands. _tests/, ~200 lines._ (M)

#### Group 84: Rewrite the documentation for 2.0

_Depends on: Group 83_

**This group delivers:** the invariants, the architecture reference and AGENTS.md describe the 2.0 store, and the project spec's supporting-document map points at them.

##### Track 84A: Rewrite the invariants, SPEC and AGENTS.md for 2.0
_2 tasks . ~700 LOC . low risk . 8 files_
**Outcome:** An agent or contributor reading the routing README, the invariants, the architecture reference and AGENTS.md learns the 2.0 rules and none of the deleted 1.x ones.
**Supports:** MVP-1, docs/SPEC.md supporting-document map; required before 2.0.0 is cut.
**Done when:** `sync.md`, `init-devices.md` and `conflicts.md` describe the 2.0 store, version vectors, symmetric resolution and the fleet lifecycle, with the collector ban kept; `store.md` is routed from the README; the root `SPEC.md` architecture and data-model sections describe the 2.0 store and keep the CLI contract strings the docs routing test pins; AGENTS.md carries the store-v2 "AGENTS.md changes at 2.0" principles and rules, drops `recover --abandon-manifest` and adds the new commands; the README Upgrading notes carry the 2.0 cutover runbook; `tests/test_docs_routing.py` passes.
_touches: docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/conflicts.md, docs/invariants/README.md, docs/invariants/store.md, SPEC.md, AGENTS.md, README.md_
_blocked-by: Track 83A_
_read-first: docs/designs/store-v2.md, docs/SPEC.md, docs/invariants/README.md_
_produces: documentation a 2.0 release can be cut against_
_session: fresh · effort: high · verify: ./bin/check tests/test_docs_routing.py tests/test_module_boundaries.py_

_Source: docs/designs/store-v2.md T9 (docs half) and "AGENTS.md changes at 2.0"; docs/SPEC.md supporting-document map. The spec itself is not rewritten here; a changed target goes through /project-spec._

- **Invariants and routing** -- Rewrite the three invariant docs and the routing README rows for the store, apply and fleet modules; keep a decision findable in shipped history only where one must survive the deletion. _docs/invariants/, ~400 lines._ (M)
- **SPEC, AGENTS.md and README** -- Replace the storage layout, manifest schema, encryption and conflict sections of the architecture reference; apply the AGENTS.md principle, rule and command changes; write the cutover runbook into the README Upgrading notes. _SPEC.md, AGENTS.md, README.md, ~300 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 71 ← {}
- Group 72 ← {71}
- Group 73 ← {71, 72}
- Group 74 ← {72, 73}
- Group 75 ← {71, 72, 73, 74}
- Group 76 ← {75}
- Group 77 ← {76}
- Group 78 ← {77}
- Group 79 ← {78}
- Group 80 ← {79}
- Group 81 ← {80}
- Group 82 ← {81}
- Group 83 ← {73, 82}
- Group 84 ← {83}
```

Track detail per group:

```
Group 71: Last 1.x hazard fix, test guard and the 2.0 foundations
  +-- Track 71A ........... ~M . 3 tasks
  +-- Track 71B ........... ~M . 2 tasks
  +-- Track 71C ........... ~L . 2 tasks
  +-- Track 71D ........... ~L . 2 tasks
Group 72: Scope trim and the replication invariants
  +-- Track 72A ........... ~M . 2 tasks
  +-- Track 72B ........... ~M . 3 tasks
Group 73: Linux platform and local state
  +-- Track 73A ........... ~L . 2 tasks
  +-- Track 73B ........... ~L . 2 tasks
Group 74: Durable-write failure handling
  +-- Track 74A ........... ~L . 2 tasks
Group 75: Publish commits
  +-- Track 75A ........... ~L . 1 task
Group 76: Snapshots and restore safety
  +-- Track 76A ........... ~M . 2 tasks
Group 77: Apply peers' logs
  +-- Track 77A ........... ~M . 2 tasks
Group 78: Causal classification
  +-- Track 78A ........... ~L . 1 task
Group 79: Portable paths and previews
  +-- Track 79A ........... ~L . 2 tasks
Group 80: Create and join a fleet
  +-- Track 80A ........... ~L . 1 task
Group 81: Presence and retirement
  +-- Track 81A ........... ~M . 2 tasks
Group 82: Syncthing integration
  +-- Track 82A ........... ~L . 2 tasks
Group 83: Remove the 1.x store
  +-- Track 83A ........... ~L . 3 tasks
Group 84: Rewrite the documentation for 2.0
  +-- Track 84A ........... ~L . 2 tasks
```

**Total: 14 groups . 19 tracks remaining.**

The 2.0.0 cut itself is not a Track: it is the acceptance of docs/SPEC.md O1
to O7 on the one-week Mac plus Linux soak, recorded in PROGRESS.md
`## Acceptance` by the owner, followed by the cutover runbook in store-v2 §13.
Conflict policies (2.1) and the resolver (2.2) wait in Future for that
acceptance. Real self-update qualification remains **S13 REQUIRED / PENDING,
0/3**, and Track 70A's two-Mac acceptance (item 14B) remains owed, both in
Future with owners and triggers. Memory transport also remains deferred:
Track 68A delivered the accepted tests-only no-route result, with native
trials **0/48 UNSTARTED/INCONCLUSIVE**.

---

## Future

Deferred: docs/roadmap-future.md (93 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
