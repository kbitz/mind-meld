# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm reports usage for Claude Code, Codex, Grok Build, Cursor and Muse.** Cursor covers standalone runs, legacy Conductor `runs.ndjson` and Conductor SQLite stores (v1.7.0). Cursor usage capture adds no Cursor customization or memory source. Muse (v1.9.0) is enabled only explicitly; it syncs `memory/` and classifies as Unclassified until the 2.x contract. Memory capture, recall and sync parity are planned separately in [the memory continuity design](designs/memory-continuity.md). The product authority is [docs/SPEC.md](SPEC.md): the selected target is the owner's fleet on the 2.0 Syncthing store, designed in [store-v2](designs/store-v2.md).

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills. Grok customization sync uses the existing source allowlisting skills/, commands/ and rules/; it is separate from skill-link discovery.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex/Grok session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening either requires a new user scope decision.
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **1.x is frozen at cutover (spec, 2026-10-11).** The GC repair and the scope trim are the last 1.x store changes. New work on the iCloud store, mtime-ordered apply or conflict sidecars is refused; file it against the 2.0 store or kill it.

- **Syncthing stays owner-managed (spec, 2026-10-18).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`. Invite codes, mesh healing, relays, folder configuration through Syncthing's API and scan requests are refused until the spec's revisit trigger is observed.
- **Simulator evidence is sized to the owner's fleet (spec, 2026-10-18).** A store Track proves itself with the bounded profile in CI; the deep profile runs once on the 2.0 release candidate. Further restore modeling, restore drills and native restore experiments are refused until a restore is observed on the fleet.

---

## In Progress

No active branches, sessions or PRs were declared for this regeneration.

## Current Plan

_tombstone: 27, 70, 71, 72, 73_

### Phase 4: mm 2.0 on Syncthing

**End-state:** The owner's macOS and Arch-based Linux machines sync agent context over one Syncthing folder of per-device, write-once encrypted commit logs, with the 1.x iCloud store deleted and the invariants rewritten; docs/SPEC.md outcomes O1 to O7 accepted on a live soak.
**Groups:** 74, 75, 76, 77, 78, 79, 80, 81, 82, 83

#### Group 74: Suite-wide test write guard

_Depends on: none_

**This group delivers:** no test, including the simulator once real store code runs in it, can write the owner's real mm state, agent homes or storage.

##### Track 74A: Guard raw test writes to real account state
_3 tasks . ~270 LOC . medium risk . 9 files_
**Outcome:** The owner can run the whole test suite, including the simulator with real store code, without any test writing to the real `~/.config/mind-meld`, a storage folder or an agent's home.
**Supports:** MVP-1, docs/SPEC.md O1 (the simulator runs real store code) and the constraint that personal-use data is real data; completes the retained Track 71B acceptance.
**Done when:** under pytest, every raw writer named in the routing README's C1 paragraph (`_log_unexpected`'s append, `LocalBackend.put_exclusive`/link and delete/unlink, caller-side mkdirs, direct lock creation, the `seen_sources` and `upgrade` state opens) refuses a path under the four real roots, a storage root configured in the real account config, or an agent destination (`~/.claude`, `~/.codex`, `~/.grok`, `~/.gstack`, `~/.local/share/muse`), with the home taken from `pwd`; a regression reproduces the 2026-10-08 `_log_unexpected` write and proves it refuses; the routing README's C1 paragraph records the closed guard; `fsutil.flock_append_jsonl` no longer takes `on_locked`; `./bin/check` passes.
_touches: src/mind_meld/fsutil.py, src/mind_meld/cli.py, src/mind_meld/seen_sources.py, src/mind_meld/upgrade.py, src/mind_meld/lockfile.py, src/mind_meld/storage/local.py, tests/conftest.py, tests/test_fsutil.py, docs/invariants/README.md_
_out: 75A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md, tests/conftest.py, src/mind_meld/fsutil.py_
_produces: a suite-wide refusal every later test module inherits, so simulator safety can be claimed_
_session: fresh · effort: high · verify: ./bin/check tests/test_fsutil.py tests/test_lockedjson.py tests/test_docs_routing.py_

_Source: inbox "[review] Direct test writers can still touch real account state" (P1; owner decision D4, 2026-10-11). Formerly Track 71B: PR #204 (v1.7.1, `adfcc50`) delivered the shared-helper guard, and its receipt records "OUT-71B Track outcome | DEFERRED BY USER (D4) … Track 71B not accepted; 71D safety not claimed". Manifested: the 2026-10-08 leak of 252 crypto errors into the real autopush and autopull logs went through `_log_unexpected`'s raw `open(path, "a+b")`, still unguarded at 223f1b8 (`cli.py` `_log_unexpected`). Inbox "[plan-ceo-review:track=73B] Remove `fsutil.flock_append_jsonl(on_locked=)`" (revisit trigger: this Track): Track 73B removed its only production caller; the parameter is still at 223f1b8._

- **Extend the guard to raw writers and roots** -- Guard the named raw writers at one structural point or at each writer (decided at /autoplan), and add configured storage roots and agent destinations to the refused set. `storage/local.py` needs coverage only until Track 82A deletes it. _src/mind_meld/fsutil.py, src/mind_meld/cli.py, src/mind_meld/seen_sources.py, src/mind_meld/upgrade.py, src/mind_meld/lockfile.py, src/mind_meld/storage/local.py, tests/conftest.py, ~150 lines._ (M)
- **Pin the incident and close C1** -- A regression for the 2026-10-08 `_log_unexpected` path and one per named writer; rewrite the routing README's C1 paragraph. _tests/test_fsutil.py, docs/invariants/README.md, ~100 lines._ (M)
- **Drop the dead `on_locked` hook** -- Remove `flock_append_jsonl(on_locked=)` and its test. _src/mind_meld/fsutil.py, tests/test_fsutil.py, ~25 lines (del)._ (S)

#### Group 75: Publish commits

_Depends on: Group 74_

**This group delivers:** `mm push` authors version-vectored entries from the ledger, writes self-contained, write-once commits to `dev/<me>/log/` and refreshes this device's presence; the simulator's publish and presence pieces become the real code.

##### Track 75A: Publish: ledger authoring, commits and presence
_2 tasks . ~770 LOC . high risk . 11 files_
**Outcome:** The owner's `mm push` writes one sealed commit per change set to this device's log, with content that travels inside the commit and a `prev` chain, and refreshes this device's presence record.
**Supports:** MVP-1, docs/SPEC.md O1, O3 and O4.
**Done when:** the §4 authoring table is implemented against the ledger (`{me: 1}` for new paths, incremented vectors for changes, tombstones for deletions guarded by `_prove_omitted_paths_absent`); commits follow store-v2 §5 as amended by Track 73C (a per-publication family drawn from an injected `family_source`, an intent row before the parts, exclusive create via `os.link` from `.mm-tmp-*` temp files with parts last, and re-sealing on an `EEXIST` retry); `state.db` migration 2 adds the 73C state (`own_log` family and digest, the applied-family set, `cursors.fork`, intent rows, `displaced` cause and peer), and receipts that sync logic reads live in their owning tables, never in forensic `history`; ledger, `own_log` and `history` rows commit in one transaction, and only publish owns `seq` and `vv + me`; a pending resolution publishes even with an unchanged sha and then clears pending; presence is written at the publish refresh; the scan reuses the strict walkers, exclusions, nested-checkout freezing and upload verification; before the first ledger row is written, the `StateDBCorrupt` remedy stops advising to move `state.db*` aside: mm preserves the database and refuses; the `publish` and `presence` registry pieces are real, behind a statedb-backed `read` and atomic `transaction` seam that the remaining model pieces share, directly callable with injected state, clock, machine id, replica, source and key entry points (no CliRunner or activation); the `SQLiteState` adapter keeps INV7's byte-integrity and causal checks with a metadata-only ledger, with cases for an unpublished edit, a deletion and a pending resolution; SQLite and real-process interruption evidence covers the publisher; the bounded simulator profile passes, and isolated `tests/sim` time is measured against its 180 s target.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/manifest.py, src/mind_meld/statedb.py, src/mind_meld/pullhistory.py, tests/test_publish.py (new), tests/test_statedb.py, tests/sim/, tests/test_integration.py, README.md, docs/invariants/store.md_
_blocked-by: Track 71C, Track 72A, Track 73B, Track 73C, Track 74A_
_out: 76A_
_read-first: docs/designs/store-v2.md, docs/invariants/store.md, docs/invariants/sync.md, tests/sim/README.md, src/mind_meld/manifest.py_
_produces: `store.publish`, the presence writer and the shared state seam the snapshot and apply Tracks extend_
_session: fresh · effort: high · verify: ./bin/check tests/test_publish.py tests/test_statedb.py tests/sim tests/test_integration.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §4 (authoring), §5 and T4; inbox "75A publish and presence must replace model pieces and supply the state seam" ([plan-eng-review:track=72A], presence writes moved here from Track 81A); PR #208 completion row 46 (isolated `tests/sim` ≤180 s deferred by the owner to 75A). Inbox "[plan-eng-review:track=73C] Amend later store cards for the 73C design rules" (the 75A rules, plus the schema items it filed against Track 73B, which shipped before them) and "[plan-ceo-review:track=73B] 75A card note: INV7 equivalence and the authoritative-state remedy" (P1; Eng G7 on receipt tables). The remedy text is `pullhistory.py` "move state.db and every state.db-* file aside together" at 223f1b8. The deep simulator profile runs once on the 2.0 release candidate (spec, 2026-10-18). `tests/sim/model/registry.py` names 75A as the owner of `publish` and `presence`._

- **Author and write commits** -- Scan strictly, diff against the ledger, author entries and tombstones, build the commit with `prev` and the host fingerprint, write the family with its intent row and exclusive create, add `state.db` migration 2, update the ledger in one transaction, and replace the move-aside remedy; preview (`--dry-run`) reports what would be authored and writes nothing. _src/mind_meld/store/publish.py, src/mind_meld/cli.py, src/mind_meld/manifest.py, src/mind_meld/statedb.py, src/mind_meld/pullhistory.py, tests/test_publish.py, tests/test_statedb.py, tests/test_integration.py, README.md, docs/invariants/store.md, ~600 lines._ (L)
- **Presence and the simulator seam** -- The presence writer at the publish refresh, the statedb-backed seam, the `SQLiteState` adapter with its INV7 cases, the real `publish` and `presence` registry entries and the recorded bounded and isolated runs. _src/mind_meld/store/, tests/sim/, ~170 lines._ (S)

#### Group 76: Snapshots and restore safety

_Depends on: Group 75_

**This group delivers:** devices snapshot and compact their own logs on the ratio rule from attended pushes only, and a restored, crashed or cloned device is detected and handled before it publishes.

##### Track 76A: Snapshots, compaction and own-storage reconciliation
_2 tasks . ~400 LOC . high risk . 6 files_
**Outcome:** A device's log stays bounded without any cross-device acknowledgement, and a machine restored from backup, interrupted mid-publish or cloned cannot fork the fleet or lose a recoverable edit.
**Supports:** MVP-1, docs/SPEC.md O5 and the accepted snapshot limitation.
**Done when:** a snapshot is written when the log since the last one outgrows that snapshot or after 200 commits, only from an attended `mm push` or `mm push --snapshot`, never from `autopush`; only families proven by `own_log`, an intent row or the snapshot chain are compacted, commits at or below the previous snapshot's seq are deleted after 14 days and the two newest snapshots are kept; the snapshot header carries `cursors` and `prev`; the §9 table as amended by Track 73C is implemented: restored-device detection holds publication as pending `own-records-in-flight` while the own presence, a peer's `applied[me]` or a peer snapshot cursor exceeds `own_log` and those records have not all arrived, then skips ahead with a forced snapshot once the folder has synced; heal of a lost own commit or snapshot, torn-slot closure, own-fork refusal with `mm rejoin` and own-snapshot recovery follow §5 and §9; a missing `dev/<me>/` with a non-empty `own_log` refuses; the `snapshot`, `compact` and `reconcile` registry pieces are real behind the shared seam with persisted observations, keeping write-once and own-namespace rules; real crash-interruption scenarios replace model protocol stops; the bounded profile passes; the `restore-current` and `fresh-replica-unreplicated` rows keep their owner-accepted dispositions.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_publish.py, tests/sim/, docs/invariants/store.md_
_blocked-by: Track 75A_
_out: 77A_
_read-first: docs/designs/store-v2.md, docs/invariants/store.md, tests/sim/README.md_
_produces: bounded logs and the restore, clone and fork rules the apply Tracks rely on_
_session: fresh · effort: high · verify: ./bin/check tests/test_publish.py tests/sim tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §6, §9 and T4 (snapshot half); inbox "76A must replace snapshot, compaction and own-storage reconciliation" ([plan-eng-review:track=72A]); inbox "[plan-eng-review:track=73C] Implement restored-device detection in the product (design and model delivered by 73C)" and the 76A part of "Amend later store cards for the 73C design rules" (heal, torn slot, own-fork rules, own-snapshot recovery, proven-only compaction). The time trigger was removed on 2026-10-11: a snapshot is about 330 MB sealed on device 889e42c0 and Syncthing gets no block deltas from sealed files._

- **Snapshots and compaction** -- Ratio and count triggers, attended-only, `--snapshot`, retention of the two newest snapshots, proven-only 14-day deletion of compacted commits, chain checks bounded to the retained window. _src/mind_meld/store/snapshot.py, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_publish.py, tests/sim/, docs/invariants/store.md, ~250 lines._ (M)
- **Own-storage reconciliation** -- At the start of push and pull read the own log listing and presence and apply the amended §9 table, including restored-device detection, heal and torn-slot closure; the simulator's restore, clone and crash actions must refuse or recover as specified. _src/mind_meld/store/publish.py, tests/sim/, ~150 lines._ (M)

#### Group 77: Apply peers' logs

_Depends on: Group 76_

**This group delivers:** `mm pull` reads each peer's commits in sequence, bootstraps from snapshots, and reports gaps as pending.

##### Track 77A: Apply cursors, gap rule and snapshot bootstrap
_2 tasks . ~350 LOC . high risk . 7 files_
**Outcome:** The owner's `mm pull` applies every peer's commits in order, picks up from a snapshot when older commits were compacted, and says "waiting for push #N from X" instead of failing when something has not arrived.
**Supports:** MVP-1, docs/SPEC.md O3.
**Done when:** the §6 reader rule as amended by Track 73C is implemented per peer (apply the next complete family in family order, treating incomplete and late families as pending; load the newest complete snapshot when the next commit is missing at or below its seq, raise every other cursor to the snapshot's cursors and apply the cover rule, with no chain-anchor wait; record `gap_since` when pending; stop with an always-visible warning on an unreadable commit and skip to a later snapshot); gap, parts and ENOENT states read as pending and hash chains are verified; a pending gap older than 24 h warns in every mode; a joiner bootstraps from the freshest complete peer snapshot; applied receipts carry the family digest per the receipts rule; the `read` registry piece is real behind the shared seam with persisted per-entry history so invariant 7 observes each pull action; simulator invariant 4 holds; the bounded profile passes; native transport stays a separate qualification.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_apply.py (new), tests/sim/, tests/test_integration.py, docs/invariants/store.md_
_blocked-by: Track 76A_
_out: 78A_
_read-first: docs/designs/store-v2.md, docs/invariants/store.md, tests/sim/README.md_
_produces: `store.apply` cursors the classification Track feeds entries through_
_session: fresh · effort: high · verify: ./bin/check tests/test_apply.py tests/sim tests/test_integration.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §6 (reader rule and cover rule), §8 (join bootstrap) and T5 (cursor half); inbox "77A must replace the peer reader behind observable injected entry points" ([plan-eng-review:track=72A]) and the 77A part of "[plan-eng-review:track=73C] Amend later store cards for the 73C design rules" (reader rule, cover rule, chain-anchor removal, receipts rule). The 2026-10-18 amendment removed the dependency on the killed retirement-design Track (formerly 74B)._

- **Cursors and the reader rule** -- Per-peer cursor advance in family order, the snapshot fallback and cover rule, `gap_seq` and `gap_since`, unreadable-commit handling and the 24 h data-at-risk warning, with the real `read` piece and its persisted history. _src/mind_meld/store/apply.py, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_apply.py, tests/sim/, docs/invariants/store.md, ~250 lines._ (M)
- **Snapshot bootstrap** -- Select the freshest complete peer snapshot for a device with no cursors, classify every entry through the next Track's rules, and raise cursors from the snapshot header. _src/mind_meld/store/apply.py, tests/test_apply.py, tests/test_integration.py, ~100 lines._ (S)

#### Group 78: Causal classification

_Depends on: Group 77_

**This group delivers:** peer edits to untouched files fast-forward, concurrent edits resolve symmetrically, deletions propagate, and replaced bytes land in `displaced`.

##### Track 78A: Causal classification, symmetric resolution and displaced
_1 tasks . ~520 LOC . high risk . 7 files_
**Outcome:** The owner sees a conflict only when two machines really changed the same file; everything else applies as a fast-forward, deletions stick, and nothing replaced is lost for 30 days.
**Supports:** MVP-1, docs/SPEC.md O1 and the accepted limitation on union merge of non-UTF-8 input.
**Done when:** the §4 classification table is implemented (same sha merges vectors without I/O and keeps an existing publication obligation; dominance fast-forwards or deletes with the old bytes in `displaced` and clears pending; local dominance ignores; concurrency, including equal vectors with unequal sha, resolves); resolution is `merge_jsonl` for `.jsonl`, `merge_lines` for `MEMORY.md`, newest by mtime with the sha tiebreak otherwise; union input that is not valid UTF-8 resolves newest-wins with the sha tiebreak and the loser in `displaced`, and `test_gap_non_utf8_union` passes as an ordinary test with its `KNOWN_DESIGN_GAPS` row and random-run gate removed; live wins over a tombstone, and the bootstrap inversion moves never-ledgered local files matching a peer tombstone to `displaced`; every resolution is a pure function of the unordered pair and writes `sha=resolved`, `vv=max(L,R)` and `pending=true`, leaving the `me` increment to publish; peer display names pass through `safety`; `mm displaced` lists and restores entries, including fork-tagged rows; the `classify` and `resolve` registry pieces are real with persisted per-entry actions; simulator invariants 1, 2 and 5 hold; the bounded profile passes.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/merge.py, src/mind_meld/statedb.py, tests/test_apply.py, tests/sim/, docs/invariants/store.md_
_blocked-by: Track 77A_
_out: 79A_
_read-first: docs/designs/store-v2.md, docs/designs/conflict-policies.md, docs/invariants/conflicts.md, src/mind_meld/merge.py, tests/sim/README.md_
_produces: the symmetric resolver contract conflict policies (2.1) extend_
_session: fresh · effort: high · verify: ./bin/check tests/test_apply.py tests/sim tests/test_merge.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §4 (classification, resolution, symmetry, bootstrap) and T5 (apply half); inbox "78A classification and resolution must honor the shared publication contract" ([plan-eng-review:track=72A], V7/E12/E13) and the 78A part of "[plan-eng-review:track=73C] Amend later store cards for the 73C design rules" (`mm displaced` restore and fork-tagged rows). The non-UTF-8 rule is the owner's 2026-10-18 decision (docs/SPEC.md accepted limitations), replacing the retirement-design Track formerly numbered 74B; `merge.py` still decodes union input with `errors="replace"` at 223f1b8. The live-versus-tombstone rule and the bootstrap inversion were added on 2026-10-11 after the fleet's 15,415 tombstones were measured._

- **Classify and resolve** -- Version-vector comparison against the ledger plus unpublished local edits, the four actions, the three symmetric resolvers with the non-UTF-8 fallback, the tombstone rules, the `displaced` writes, `mm displaced` and the real registry pieces; the merge module loses its LCS synthetic base once no caller remains. _src/mind_meld/store/apply.py, src/mind_meld/merge.py, src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_apply.py, tests/sim/, docs/invariants/store.md, ~520 lines._ (L)

#### Group 79: Portable paths and previews

_Depends on: Group 78_

**This group delivers:** Claude project folders sync between macOS and Linux homes, and `mm diff` and `--dry-run` work on the new store without writing.

##### Track 79A: Portable Claude paths and write-free previews
_2 tasks . ~300 LOC . medium risk . 7 files_
**Outcome:** The owner's Claude project memory for `~/src/<project>` is the same folder on a Mac and a Linux box, and previews show what a pull would do on the 2.0 store.
**Supports:** MVP-1, docs/SPEC.md O1 (cross-OS) and the preview write boundaries.
**Done when:** a `claude` project folder whose encoded name starts with the encoded home prefix is published as `projects/~-<rest>` and mapped back on apply, checked against the folder names Claude Code actually creates on macOS and on Linux; paths outside home stay absolute and no path aliases are added; `mm diff`, `mm pull --dry-run` and `mm push --dry-run` read the new store and change nothing but the lock file; `pullplan` predictions use cursors and vectors instead of mtimes.
_touches: src/mind_meld/store/, src/mind_meld/cli.py, src/mind_meld/pullplan.py, src/mind_meld/manifest.py, tests/test_pull_helpers.py, tests/test_integration.py, docs/invariants/store.md_
_blocked-by: Track 78A_
_out: 80A_
_read-first: docs/designs/store-v2.md, docs/invariants/sync.md, src/mind_meld/pullplan.py_
_produces: portable wire paths and previews the fleet Tracks can demonstrate_
_session: fresh · effort: medium · verify: ./bin/check tests/test_pull_helpers.py tests/test_integration.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §7 and T5 (preview half); Track 72A deferral V13 ("Check cross-OS context continuity on the owner's real layout before Track 79A … Do not add path aliases automatically"). On device 889e42c0 all 258 synced Claude project files live under `-Users-kb-`; 111 of 160 project folders are worktree paths that stay machine-specific by nature. The owner's Omarchy machine (2026-10-18) supplies the Linux folder names._

- **Home-relative wire paths** -- Encode and decode the `~-` prefix for the `claude` source on both OSes after checking Claude Code's real folder naming on each; leave other sources root-relative. _src/mind_meld/manifest.py, src/mind_meld/store/, tests/test_pull_helpers.py, ~120 lines._ (M)
- **Previews on the new store** -- Port `diff`, `pull --dry-run` and `push --dry-run` to cursors and the ledger; keep `COMMAND_INTENTS62` write boundaries. _src/mind_meld/pullplan.py, src/mind_meld/cli.py, tests/test_integration.py, docs/invariants/store.md, ~180 lines._ (M)

#### Group 80: Create and join a fleet

_Depends on: Group 79_

**This group delivers:** a fleet is created once, and every other machine joins after the owner pairs it in Syncthing.

##### Track 80A: Create and join a fleet
_2 tasks . ~550 LOC . high risk . 10 files_
**Outcome:** The owner runs `mm init --new-fleet` on one machine; on each other machine they pair it in Syncthing, accept the folder and run `mm join`, and the joiner never creates `fleet.json`.
**Supports:** MVP-1, docs/SPEC.md O2 and O4.
**Done when:** before the first `fleet.json` is written, the owner's decisions on binding the envelope version byte and the record family into the associated data, Unicode normalization of the passphrase, and an expected `fleet_id` argument to `unlock_fleet` are implemented and recorded in docs/invariants/store.md, with the T04 vectors regenerated once; the 2.0 default storage path is `~/Sync/mind-meld` on both OSes for new fleets, and a 1.x install is not moved; `--new-fleet` refuses unless the folder holds no `fleet.json` and no `dev/`, prompts for the passphrase twice, writes `fleet.json` exclusively, writes the `.stignore` and publishes a snapshot and presence; `mm join` tells the owner to pair the machine and accept the folder in Syncthing, waits for `fleet.json` with progress output, verifies the keycheck, records the fleet in config, writes the `.stignore`, bootstraps from the freshest complete peer snapshot, publishes its own snapshot and pulls; `mm rejoin` mints a new device id in the same fleet with `supersedes` and the 73C join tie-break; on macOS, init and join exclude the store folder and Syncthing's database from Time Machine, and the README gives the Linux equivalent; the simulator fixture honors a replica `.stignore`; clone timing checks hold, and the simulator's `clone_rule` harness assertion that stopped the 73C lifecycle deep run is fixed so the release-candidate deep run can complete; the `create_fleet`, `join` and `rejoin` registry pieces are real behind the shared seam with no model fallback under a real label; the bounded profile passes; the owner's native Omarchy run (pipx install, the keyring backend actually selected, login and reboot behavior, one hook run, manual recovery) is recorded in the PR before the Linux passphrase flow merges.
_touches: src/mind_meld/cli.py, src/mind_meld/store/, src/mind_meld/config.py, src/mind_meld/crypto.py, tests/test_fleet.py (new), tests/test_integration.py, tests/sim/, README.md, docs/invariants/init-devices.md, docs/invariants/store.md_
_blocked-by: Track 79A_
_out: 81A_
_read-first: docs/designs/store-v2.md, docs/invariants/init-devices.md, docs/invariants/store.md, tests/sim/README.md_
_produces: fleet creation and join flows the status and retirement Track completes_
_session: fresh · effort: high · verify: ./bin/check tests/test_fleet.py tests/test_store_format.py tests/sim tests/test_integration.py tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §8 and T6 (lifecycle half); inbox "80A fleet creation, join and rejoin must replace model registry entries" ([plan-eng-review:track=72A]); PR #203 backlog items 7, 11 and 12 (verified at 223f1b8: `unlock_fleet(data, passphrase)`, no passphrase normalization, AAD prefix `b"mm-record-v3\0"` with no version byte). Inbox: "[plan-eng-review:track=73C] Bind the record family into the associated data with the version byte", "Keep the store folder and Syncthing's database out of backups automatically", the 80A part of "Amend later store cards for the 73C design rules" (rejoin `supersedes`, join tie-break, re-seal on retry), "[manual] Track 80A owns the 2.0 default storage path ~/Sync/mind-meld" (UC1, 2026-10-16) and "[manual] Omarchy qualification checkpoint before Track 80A" (its trigger, a Linux machine on the owner's desk, was met by 2026-10-18). PR #215's verification record: the lifecycle deep run on `f544844` stopped at 638 examples on the `clone_rule` harness assertion (4515 s, exit 1), with the harness fix withheld; O1.1's release-candidate deep run needs it. `mm invite` and mesh healing are spec non-goals since 2026-10-18._

- **Wire decisions before the first fleet.json** -- Version byte and record family in the associated data, passphrase normalization, the expected `fleet_id`, regenerated T04 vectors. _src/mind_meld/crypto.py, src/mind_meld/store/, docs/invariants/store.md, ~100 lines._ (S)
- **new-fleet, join and rejoin** -- The three commands, the config `[fleet]` record, the default path, the passphrase and keycheck flow, `.stignore`, backup exclusion, the init-time refusal rules and the real registry pieces; previews of each must preserve their write boundaries. _src/mind_meld/cli.py, src/mind_meld/store/fleet.py, src/mind_meld/config.py, tests/test_fleet.py, tests/test_integration.py, tests/sim/, README.md, docs/invariants/init-devices.md, ~450 lines._ (L)

#### Group 81: Delivery receipts, Syncthing status and retirement

_Depends on: Group 80_

**This group delivers:** `mm status` says which machines have the latest push and what Syncthing is doing, `mm diag` lints the folder, `mm devices` keeps working for retro-fleet, and dead devices can be retired.

##### Track 81A: Delivery receipts, Syncthing status and retirement
_3 tasks . ~460 LOC . medium risk . 13 files_
**Outcome:** The owner sees "laptop has your latest push; desktop is 2 pushes behind, last active 3h ago" and Syncthing's state in `mm status`, `mm diag` flags folder settings that would break sync, `mm devices` keeps working for retro-fleet, and a machine that has not pushed for 30 days can be retired.
**Supports:** MVP-1, docs/SPEC.md O2.2, O3 and O7.
**Done when:** the pull path refreshes presence through Track 75A's writer when something changed and at most hourly otherwise; `mm status` renders delivery receipts, pending gaps and fork listings per peer; `syncthing.py` is a leaf module that discovers the API key and address from `config.xml` on both OSes (falling back to `syncthing paths`), only reads Syncthing's REST API with time-bounded requests, and works, with status saying so, when Syncthing is absent; `mm status` reports reachability, folder state, need counts and connected peers; `mm diag` lints the folder options (`sendreceive`, versioning off, `maxConflicts` 10, `ignorePerms=false`, `order=alphabetic`), the `.stignore`, foreign files (Syncthing's `.stfolder/` and `.stignore`, in-flight `.mm-tmp-*` and `.DS_Store` are not foreign), fork evidence beyond conflict copies, the backup exclusion and the Syncthing version (2.1 or newer), and changes none of them; the README lists the folder options the owner sets; `mm devices --format json` projects presence onto `device_id`, `device_name`, `registered`, `last_seen` and `last_seen_version` so retro-fleet's subprocess reader is unchanged; `mm devices retire <id>` requires 30 days of inactivity, then deletes only `dev/<id>/`; the retired device refuses on its next run; `KNOWN_DESIGN_GAPS["retire-delayed-presence"]` records the owner's 2026-10-18 disposition (inactivity-only retirement, a docs/SPEC.md accepted limitation) and stays pinned; the `retire` registry piece is real behind the shared seam with persisted action observations; the bounded profile passes.
_touches: src/mind_meld/syncthing.py (new), src/mind_meld/cli.py, src/mind_meld/store/, src/mind_meld/devices.py, tests/test_syncthing.py (new), tests/test_fleet.py, tests/test_integration.py, tests/test_diag.py, tests/test_module_boundaries.py, tests/sim/, README.md, docs/invariants/init-devices.md, docs/invariants/README.md_
_blocked-by: Track 80A_
_out: 82A_
_read-first: docs/designs/store-v2.md, docs/invariants/init-devices.md, docs/invariants/events-retro.md, docs/invariants/README.md, tests/sim/README.md_
_produces: delivery receipts, read-only Syncthing reporting and the retirement rule_
_session: fresh · effort: medium · verify: ./bin/check tests/test_syncthing.py tests/test_fleet.py tests/test_diag.py tests/test_module_boundaries.py tests/test_integration.py tests/test_retro_fleet_aggregator.py tests/sim tests/test_docs_routing.py_

_Source: docs/designs/store-v2.md §2 (presence), §8 (retire), §10 (status and diag) and T6/T7; inbox "81A retire and delivery status must preserve simulator ownership boundaries" ([plan-eng-review:track=72A], E25 moved presence writing to 75A); the 81A part and the Syncthing-Track (then 82A) part of "[plan-eng-review:track=73C] Amend later store cards for the 73C design rules" (receipt codec, status fork listing, diag fork evidence; the retire coverage tie-break fell with the coverage rule); PR #203 backlog item 3 (`.stfolder/`, `.mm-tmp-*` and `.DS_Store` classify as foreign; `layout.classify_keys` still exempts only `fleet.json` and `.stignore` at 223f1b8). The read-only half of the former Syncthing Track is folded in here; folder setup, scan-after-push, mesh healing and `mm relay add` are spec non-goals since 2026-10-18, and PR #203 item 8 (`syncthing_id` grammar before pairing) fell with mesh healing. Syncthing option semantics verified against docs.syncthing.net on 2026-10-11._

- **Receipts and the devices projection** -- Pull-time presence refresh, the status renderer for receipts, gaps and forks, the `devices --format json` projection and its contract test. _src/mind_meld/store/presence.py, src/mind_meld/cli.py, src/mind_meld/devices.py, tests/test_fleet.py, tests/test_integration.py, ~180 lines._ (M)
- **Read-only Syncthing status and diag** -- Config discovery, a read-only REST client with timeouts, the status section, and the diag lint for folder options, `.stignore`, foreign files, fork evidence, backup exclusion and version. _src/mind_meld/syncthing.py, src/mind_meld/cli.py, tests/test_syncthing.py, tests/test_diag.py, tests/test_module_boundaries.py, README.md, docs/invariants/README.md, ~200 lines._ (M)
- **mm devices retire** -- The inactivity check, the only write outside `dev/<me>/`, the retired device's own refusal, the gap row's disposition and the real `retire` piece. _src/mind_meld/cli.py, tests/test_fleet.py, tests/sim/, docs/invariants/init-devices.md, ~80 lines._ (S)

#### Group 82: Remove the 1.x store

_Depends on: Group 81_

**This group delivers:** the iCloud-era store, mtime-ordered apply and conflict sidecars are gone from the code.

##### Track 82A: Delete the 1.x store and mtime-ordered apply
_3 tasks . ~2500 LOC . medium risk . 11 files_
**Outcome:** mm 2.0 ships without the 1.x store, so no code path can read, repair or delete iCloud-era storage.
**Supports:** MVP-1, docs/SPEC.md constraint "2.0 never reads 1.x storage" and the 1.x freeze.
**Done when:** the `data/` blob store, `devices/` registry, `devices-write.lock`, crypto-init fetch and repair, `_do_gc` orphan reaping, last-push sidecar and corrupt-manifest recovery, `mm recover --abandon-manifest`, `brctl` auto-pin, the mtime skip gate, deferred inline bumps, `_bump_canonical_mtime_post_resolve`, `pullplan` mtime predictions, `.sync-conflict-*` minting, the v0/v1 eras, owner parsing, `_gc_old_conflict_files` and promote are deleted; the walker keeps a global exclusion for legacy `*.sync-conflict-*` names; envelope 0x02 reading is gone; their tests are deleted or rewritten; the module boundary test still passes.
_touches: src/mind_meld/cli.py, src/mind_meld/storage/, src/mind_meld/crypto.py, src/mind_meld/retention.py, src/mind_meld/conflictmtime.py, src/mind_meld/pullplan.py, src/mind_meld/resolveflow.py, src/mind_meld/devices.py, src/mind_meld/merge.py, src/mind_meld/manifest.py, tests/_
_blocked-by: Track 81A, Track 73A_
_out: 83A_
_read-first: docs/designs/store-v2.md, docs/invariants/README.md, docs/invariants/conflicts.md_
_produces: a code base the invariant rewrite can describe truthfully_
_session: fresh · effort: high · verify: ./bin/check_

_Source: docs/designs/store-v2.md "What 2.0 deletes" and T9 (code half). 2.0 has no `ask` outcome: unresolved concurrency takes the displaced newest-wins path until 2.1._

- **Delete the store** -- Remove `storage/local.py` conflict-copy detection, the blob store, the device registry and its lock, crypto-init fetch, repair plans and winner selection, `_do_gc` orphan reaping, the last-push sidecar and recovery chain, `recover --abandon-manifest` and `brctl` auto-pin. _src/mind_meld/storage/, src/mind_meld/cli.py, src/mind_meld/crypto.py, src/mind_meld/devices.py, ~1500 lines (del)._ (S)
- **Delete mtime apply and sidecars** -- Remove the skip gate, deferred bumps, post-resolve mtime bump, mtime predictions, sidecar minting, eras, owner parsing, conflict GC and promote; keep the legacy-name walker exclusion. _src/mind_meld/conflictmtime.py, src/mind_meld/resolveflow.py, src/mind_meld/retention.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, src/mind_meld/manifest.py, ~800 lines (del)._ (S)
- **Retire their tests** -- Delete or rewrite the tests that pinned the removed paths, keep the compat fixtures only where a refusal test still reads them, and keep `COMMAND_INTENTS62` current for the removed and added commands. _tests/, ~200 lines._ (M)

#### Group 83: Rewrite the documentation for 2.0

_Depends on: Group 82_

**This group delivers:** the invariants, the 2.x compatibility contract, the architecture reference, AGENTS.md and the package metadata describe the 2.0 store, and the README carries the cutover runbook.

##### Track 83A: Rewrite the invariants, SPEC and AGENTS.md for 2.0
_3 tasks . ~850 LOC . low risk . 11 files_
**Outcome:** An agent or contributor reading the routing README, the invariants, the architecture reference and AGENTS.md learns the 2.0 rules and none of the deleted 1.x ones, and the owner has a cutover runbook to follow.
**Supports:** MVP-1, docs/SPEC.md supporting-document map and release policy ("The 2.0 rewrite of the invariants defines the 2.x contract"); required before 2.0.0 is cut.
**Done when:** `sync.md`, `init-devices.md` and `conflicts.md` describe the 2.0 store, version vectors, symmetric resolution and the fleet lifecycle, with the collector ban kept; `store.md` is routed from the README and the import-direction leaf list names `store/`; `auto-upgrade.md` defines the Compatibility (2.x) contract that replaces the 1.x one; the root `SPEC.md` architecture and data-model sections, including the gstack default exclusions, describe the 2.0 store and keep the CLI contract strings the docs routing test pins; AGENTS.md carries the store-v2 "AGENTS.md changes at 2.0" principles and rules, names `store/layout.record_key` in the storage-key rule, drops `recover --abandon-manifest` and adds the new commands; store-v2 §11's remaining platform prose (README prerequisites, init help, "other Mac" strings, the root SPEC overview, AGENTS.md principles) and the `pyproject.toml` description, keywords and classifiers describe macOS and Arch-based Linux on Syncthing; the README Upgrading notes carry the 2.0 cutover runbook, whose step 1 re-measures the gstack set before the first snapshot and which lists the Syncthing pairing and folder-option steps the owner performs; `tests/test_docs_routing.py` passes.
_touches: docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/conflicts.md, docs/invariants/README.md, docs/invariants/store.md, docs/invariants/auto-upgrade.md, SPEC.md, AGENTS.md, README.md, pyproject.toml, src/mind_meld/cli.py_
_blocked-by: Track 82A_
_read-first: docs/designs/store-v2.md, docs/SPEC.md, docs/invariants/README.md, docs/invariants/auto-upgrade.md_
_produces: documentation a 2.0 release can be cut against_
_session: fresh · effort: high · verify: ./bin/check tests/test_docs_routing.py tests/test_module_boundaries.py_

_Source: docs/designs/store-v2.md T9 (docs half), §11, §13 and "AGENTS.md changes at 2.0"; docs/SPEC.md supporting-document map and release policy; PR #203 documentation debt (AGENTS.md storage-key rule omits `store/layout.record_key`; the routing README's leaf list omits `store/`), PR #207 documentation debt (root SPEC.md's gstack default list is stale) and the "[manual] Re-measure the gstack set at cutover step 1" drain record (Track 72B autoplan CEO E3). Inbox "[manual] Owner for store-v2 section 11 prose outside pyproject.toml" and "[manual] Track 84A owns pyproject.toml description, keywords and classifiers" (UC2, 2026-10-16; that Track is this one). The spec itself is not rewritten here; a changed target goes through /project-spec._

- **Invariants and routing** -- Rewrite the three invariant docs and the routing README rows for the store, apply and fleet modules, including the leaf list; keep a decision findable in shipped history only where one must survive the deletion. _docs/invariants/, ~400 lines._ (M)
- **The 2.x compatibility contract** -- Replace "Compatibility (1.x)" in the auto-upgrade invariants with the 2.x contract: the `fleet.json` format gate, the record envelope and the host-family set. _docs/invariants/auto-upgrade.md, ~100 lines._ (S)
- **SPEC, AGENTS.md, README and package prose** -- Replace the storage layout, manifest schema, encryption, conflict and gstack-default sections of the architecture reference; apply the AGENTS.md principle, rule and command changes; rewrite the §11 platform prose, init help strings and `pyproject.toml` metadata; write the cutover runbook into the README Upgrading notes, with the gstack re-measure as step 1. _SPEC.md, AGENTS.md, README.md, pyproject.toml, src/mind_meld/cli.py, ~350 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 74 ← {}
- Group 75 ← {74}
- Group 76 ← {75}
- Group 77 ← {76}
- Group 78 ← {77}
- Group 79 ← {78}
- Group 80 ← {79}
- Group 81 ← {80}
- Group 82 ← {81}
- Group 83 ← {82}
```

Track detail per group:

```
Group 74: Suite-wide test write guard
  +-- Track 74A ........... ~L . 3 tasks
Group 75: Publish commits
  +-- Track 75A ........... ~L . 2 tasks
Group 76: Snapshots and restore safety
  +-- Track 76A ........... ~L . 2 tasks
Group 77: Apply peers' logs
  +-- Track 77A ........... ~M . 2 tasks
Group 78: Causal classification
  +-- Track 78A ........... ~L . 1 task
Group 79: Portable paths and previews
  +-- Track 79A ........... ~L . 2 tasks
Group 80: Create and join a fleet
  +-- Track 80A ........... ~L . 2 tasks
Group 81: Delivery receipts, Syncthing status and retirement
  +-- Track 81A ........... ~L . 3 tasks
Group 82: Remove the 1.x store
  +-- Track 82A ........... ~L . 3 tasks
Group 83: Rewrite the documentation for 2.0
  +-- Track 83A ........... ~L . 3 tasks
```

**Total: 10 groups . 10 tracks remaining.**

The 2.0.0 cut itself is not a Track: it is the acceptance of docs/SPEC.md O1
to O7 on the one-week Mac plus Linux soak, including one deep simulator run on
the release candidate (O1.1), recorded in PROGRESS.md `## Acceptance` by the
owner, followed by the cutover runbook in store-v2 §13. The approved Track 72A
condition also applies: 2.0.0 needs zero model pieces in
`tests/sim/model/registry.py` and an empty or owner-accepted
`KNOWN_DESIGN_GAPS` table; a green simulator alone accepts none of O1, O3 or
O5, and native transport, SQLite crash evidence and the soak keep their own
gates. Conflict policies (2.1) and the resolver (2.2) wait in Future for that
acceptance. Owed acceptance outside this plan, in Future with owners and
triggers: O6 for the 1.8.0 scope trim (every active Mac plus the owner's
confirmation), Track 70A's two-Mac check (item 14B), and real self-update
qualification, **S13 REQUIRED / PENDING, 0/3**. Memory transport also remains
deferred: Track 68A delivered the accepted tests-only no-route result, with
native trials **0/48 UNSTARTED/INCONCLUSIVE**.

---

## Future

Deferred: docs/roadmap-future.md (28 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
