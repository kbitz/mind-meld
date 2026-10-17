# Replication fixtures

Start with the deterministic two-device **store-v2 model** smoke:

```sh
./bin/check tests/sim/test_scenarios.py::test_v2_smoke
```

Expected: one passing test and a terminal summary with Hypothesis 6.169.0,
all twelve pieces labelled `model`, and the active design gaps. This uses
real 71C codecs and envelope 0x03 with fixture keys; it establishes model
behavior. It does not establish native Syncthing, SQLite, actual process
crash, CI Keychain/wheel or live-soak acceptance. Cold first-process bootstrap
and warm smoke timings are recorded separately in the implementation handoff.
A dependency download is a separate setup cost, not a simulator step.

## Store-v2 suite

The existing portable CI job already includes this suite. No second job is
needed. Hypothesis is pinned: a version bump belongs in its own PR, and new
counterexamples need investigation rather than dismissal as tool drift.

```sh
# Bounded core, lifecycle, oracle clauses, mutants and deterministic scenarios.
./bin/check tests/sim tests/test_docs_routing.py
# One named scenario.
./bin/check tests/sim/test_scenarios.py::test_edit_delete_then_redelete
# Random deep search with the default example database.
MM_SIM_DEEP=1 ./bin/check tests/sim/test_replication.py
# Repeat a deep seed; bounded derandomization ignores seed selection.
MM_SIM_DEEP=1 ./bin/check --serial tests/sim/test_replication.py -- --hypothesis-seed=417
# Statistics and controller-side summary, including under xdist.
./bin/check --serial tests/sim/test_replication.py -- --hypothesis-show-statistics
```

`MM_SIM_DEEP` accepts unset, `0`, or `1`; anything else fails with a one-line
remedy inside the machine test. Settings are passed to
`run_state_machine_as_test`, never globally loaded. `sim-bounded` inherits
Hypothesis's `ci` profile: core 50 examples × 40 steps, lifecycle 30 × 50,
derandomized, `print_blob=True`, no deadline, `too_slow` suppressed.
`sim-deep` draws random seeds with 1000 × 100, the default database, and the
same diagnostic settings. A bounded run does **not** replay deep failures.
Executed constructors can exceed configured examples during shrinking or
replay; summaries distinguish configured limits from actual examples and
rule steps. Every run prints piece provenance and gaps. Machine summaries
also print wall time, mean store-action cost and mean settle cost. The local
60-second ceiling is report-only. Measure before tuning; lower examples
before steps if the suite exceeds its three-minute CI budget. The initial
60/40 example limits produced a 211.7-second scoped run under contention;
measured action/settle costs justified reducing examples to 50/30 while
retaining step limits, every reachability check and the deep budget.

`CoreMachine` runs core rules first; `LifecycleMachine` adds join, retire,
clone, rejoin, restore and protocol crash points. Each invocation accumulates
hard-state reachability independently of the invariant checks. Missing states
fail as `REACHABILITY`, naming macro rules, initialize witnesses or logical
time jumps as remedies. At least half of lifecycle examples must settle with
two participants; deterministic scenarios require at least two at quiescence.
Lifecycle teardown first drains and pulls so in-flight fork evidence surfaces,
then applies the documented `mm rejoin` remedy to refused devices before the
final settle; the summary's `teardown_remedy_examples` counts how many examples
needed it, so the floor's post-remedy meaning stays visible. With identities
exhausted (`MAX_IDS`) a refused device cannot rejoin, and an example left with
fewer than two participants settles without quiescent credit.

## Restore and torn-publish rules

Each rule in [store-v2](../../docs/designs/store-v2.md) §5, §6 and §9 has a
deterministic witness; copy a command to watch the rule hold. Mutant rows name
the invariant that fires first when the rule is broken. Nodes live in
[test_scenarios.py](test_scenarios.py) unless noted.

| Rule | Witness (copy-paste) |
|---|---|
| Names carry a per-publication family; grouping by `(device, kind, seq, family)` (§1, §2) | `./bin/check tests/test_store_format.py -- -k TestLayout71C` |
| A torn own slot closes with a fresh complete family, even with zero entries; closure waits for the folder to sync, and an intent row restored from a backup does not prove the slot torn (§5) | `./bin/check tests/sim/test_scenarios.py::test_gap_partial_own_commit tests/sim/test_scenarios.py::test_torn_slot_closes_with_a_zero_entry_commit tests/sim/test_scenarios.py::test_intent_row_restored_from_a_backup_does_not_prove_a_torn_slot` |
| An incomplete own family the device cannot prove it wrote is in flight while its records may still arrive; once the folder is synced and no receipt claims the seq, only an attended push (or `mm push --snapshot`) supersedes it, and `autopush` stays pending (§5, §9) | `./bin/check tests/sim/test_scenarios.py -- -k stays_pending`; attended escape: `./bin/check tests/sim/test_scenarios.py -- -k "unproven_torn_slot_waits or operator_snapshot_supersedes"`; unproven-supersede mutant: `./bin/check tests/sim/test_mutants.py -- -k in_flight_witness` |
| `EEXIST` retries under a fresh family; exhaustion is loud; no part is ever overwritten (§5) | `./bin/check tests/sim/test_scenarios.py -- -k exclusive_create`; overwrite mutant (INV4): `./bin/check tests/sim/test_mutants.py -- -k overwrite-part` |
| Only complete families count; an unreadable family beside a complete one is ignored with a warning (§6) | `./bin/check tests/sim/test_scenarios.py -- -k "torn_family_beside or unreadable_family_beside"`; mixing mutant (INV4): `./bin/check tests/sim/test_mutants.py -- -k mix-families` |
| Apply order follows names; equal digests are one record (§6) | `./bin/check tests/sim/test_scenarios.py -- -k "same_slot_families or renamed_copy"` |
| A late family applies once; a peer fork warns and never refuses (§6) | `./bin/check tests/sim/test_scenarios.py::test_gap_restore_saved tests/sim/test_scenarios.py::test_gap_restore_current`; skip mutant (INV1): `./bin/check tests/sim/test_mutants.py -- -k skip-late-family`; refusing mutant (INV4): `./bin/check tests/sim/test_mutants.py -- -k reader-refuses-peer-fork`; double-apply mutant (INV7): `./bin/check tests/sim/test_mutants.py -- -k reapply-applied-family` |
| A snapshot cursor covers only applied or chain families; there is no chain-anchor wait (§6) | `./bin/check tests/sim/test_scenarios.py -- -k "snapshot_cursor_covers or applies_without_a_chain_anchor"`; count-incomplete mutant (INV7): `./bin/check tests/sim/test_mutants.py -- -k count-incomplete` |
| A dominated post-restore edit is kept in `displaced` and the warning repeats until expiry or restore (§4, §6) | `./bin/check tests/sim/test_scenarios.py -- -k "fork_displaced or dominating_version_arrives"` |
| Only the forked device refuses: `fork-restore`, `fork-clone`, `fork` — a foreign own snapshot is a clone too (§9) | `./bin/check tests/sim/test_invariants.py::test_own_chain_refusal_ground_truth tests/sim/test_scenarios.py::test_clone_before_first_presence_commit_fork_scope_and_rejoin tests/sim/test_scenarios.py::test_snapshot_only_clone_refuses_as_a_clone` |
| `state` restore replays own records with no fork (§9) | `./bin/check tests/sim/test_scenarios.py::test_state_restore_replays_own_records_and_converges` |
| `fresh-replica` restore waits on Syncthing, never advises rejoin; a retired device refuses once synced (§9) | `./bin/check tests/sim/test_scenarios.py -- -k "fresh_replica_restore or retire_age"`; detection-disabled kill: `./bin/check tests/sim/test_mutants.py -- -k detection_witness` |
| Skip-ahead when own records never return (§9) | `./bin/check tests/sim/test_scenarios.py::test_attended_push_skips_ahead_when_own_records_never_return` |
| Own-snapshot recovery from a backup older than the compaction floor (§9) | `./bin/check tests/sim/test_scenarios.py -- -k compaction_floor` |
| Compaction deletes only proven own families (§6) | `./bin/check tests/sim/test_scenarios.py::test_compaction_deletes_only_families_it_can_prove`; unproven-delete mutant (INV2): `./bin/check tests/sim/test_mutants.py -- -k delete-unproven-incomplete` |
| Heal and `torn-own-slot` reporting; replay is contiguous and heal never snapshots over an in-flight hole (§9) | `./bin/check tests/sim/test_scenarios.py -- -k "lost_own_records or below_a_complete or torn_own_slot_is or partial_copy_is_not_covered"`; disabled-heal mutant (INV1): `./bin/check tests/sim/test_mutants.py -- -k disable-heal` |
| Accepted current-policy residuals stay pinned (§9) | `./bin/check tests/sim/test_scenarios.py -- -k restore_current_residual` |
| Model texts and reason codes match the design verbatim (§6, §9) | `./bin/check tests/sim/test_scenarios.py -- -k verbatim` |

## V2 API

`StoreFleet` is separate from the legacy `SimFleet`. It never imports the CLI
or legacy harness. Its roots and write guard are explicit. `add_device` allocates
local fixture storage and a fresh transport index; `join` is a store operation.
Transport names are alphanumeric, identities are deterministic eight-hex
values, and runs permit four devices and eight total identities (four
devices plus four rejoins). The whole-run
part cap is derived in [store_fleet.py](store_fleet.py) from the content/path,
vector and cursor caps; snapshots remain below the 512-part format limit.

| API / input | Result and effects | Clock |
|---|---|---|
| `StoreFleet(root, write_guard=guard).bootstrap()` | Create fleet once, deliver header, join A/B; no legacy config or records | Read logical now |
| `fleet.add_device(name)` | Unjoined device, independent source/state/replica roots, fresh transport index | None |
| `device.edit(path, bytes, mtime=None)` / `delete(path)` | User intent in the oracle, then source mutation; edits may reuse an old mtime | Read logical now |
| `fleet.action(name, 'push'/'pull'/'publish_snapshot'/'compact', **flags)` | Outcome with status, seq, peer/gap, reason, warnings and hard states; persisted state/history and source/replica observations | Read logical now |
| `fleet.action(name, 'join')` | Wait for header or bootstrap freshest complete peer snapshot, snapshot and pull | Read logical now |
| `fleet.action(actor, 'retire', target=id)` | Pending unless age and active snapshot coverage qualify; remove target namespace only | Read logical now |
| `fleet.rejoin(device)` | Allocate a fresh ID; registered rejoin owns the observed identity/state reset and bootstrap, preserving displaced/history | Read logical now |
| `fleet.snapshot(device)` | Opaque local disk **backup**, including persisted state and index; requires idle device | None |
| `fleet.restore(device, backup, scope='disk', index_policy='saved'/'current')` | Restore the whole disk; reset the saved index or retain the current one; last-copy oracle exclusion is narrowly witnessed | None |
| `fleet.restore(device, backup, scope='state')` | Roll back sources and state only; the replica and the transport index stay intact (no `index_policy`) | None |
| `fleet.restore(device, backup, scope='fresh-replica')` | Roll back sources and state, empty the replica and reset the transport index; peers refill it (no `index_policy`) | None |
| `fleet.next_family()` / `fleet.force_families(*families)` / `fleet.family_hook` | The adapter's `family_source`: `sha256(run salt ‖ fleet counter)[:8]`, deterministic, non-monotonic and held by the fleet, never on a device disk; the hook returns chosen families first, and `family_hook` runs on every draw so a name can collide on purpose | None |
| `fleet.folder_synced(name)` | The adapter's `folder_synced`: no transport deliveries other than presence refreshes are pending to this device (Syncthing idle, nothing needed); a presence arrival never completes a record, so it does not hold the gate | None |
| `fleet.clone(device, name)` | Copy disk/index/id, new live hardware identity; no legacy config | None |
| `fleet.advance(seconds)` | Advance one logical seconds clock for every protocol time field | Explicit advance |
| `fleet.transport.advance(seconds)` | Advance delivery scheduling only | Transport clock only |
| `fleet.drain()` / `settle(fleet, min_participants=2)` | Bounded drain; settle finishes replacements, onlines all, passes delays, then push/drain/pull/drain rounds | Transport advance only |

Every store action reconstructs its adapter from injected roots, state seam,
keys, clock, live machine-id provider, `family_source` and `folder_synced`. Only unlocked keys keyed by the
header bytes are cached in the model. File state commits ledger, own log and
history atomically through one replacement; it promises seam operations, not
a database schema or durability under power loss. Transport scans use a
measured stat-signature fast path (inode, size, mtime, ctime and mode), with
cache invalidation on index restore; observations read complete trees without
transport ignores. Scans lstat children, reject even ignored symlinks and
never follow them. This finite sequential fixture does not prove hostile
filesystem rename-race safety.

## Invariants

Invariants 1–7 are enforced; 7 extends store-v2 §12. The independent oracle
never imports model decisions or merge normalization. Its histories are not
limited by the transport's 200-entry trace. All allocated devices remain
possible preservation witnesses, including refused, unjoined and retired ones.
Only joined, non-retired, non-refused devices participate in convergence.
Unpublished edits on excluded devices remain local preservation obligations;
they are not published fleet causal versions.

| ID / name | Design / SPEC | When / checker | Primary mutant |
|---|---|---|---|
| INV1 convergence | §4/6/12; O1 | Quiescence / `inv1_convergence` | Drop later peer commit |
| INV2 no-silent-loss | §4/9/12; O1/O5 | Every step + Q / `Oracle.check` | Overwrite unpublished edit; lose path while bytes survive elsewhere |
| INV3 single-writer | §1/8/12; O4 | Every store action / `inv3_single_writer`, `replica_integrity` | Peer namespace write; plaintext/invalid envelope |
| INV4 ordering-never-fails | §6/9/12; O3/O5 | Store observation / `inv4_ordering_never_fails` | Missing becomes error; refuse everything; a reader that refuses a peer fork |
| INV5 termination | §4/12; O1 | Settle bounded by 3 × participants + 2 / `settle` | Local-wins ping-pong |
| INV6 fork-safety | §9/12; O5 | Store observation / `inv6_fork_safety` | Skip live host/fork checks |
| INV7 causal-fidelity | §4; O1.1 | Every store observation + Q / `inv7_causal_fidelity`, `inv7_observed_inputs`, `inv7_quiescent` | Missing unpublished increment; forged history/effects; treat dominance as concurrent; resurrect tombstone; count an incomplete family; apply a family twice |

Same-sha actions merge vectors without source I/O. Equal vectors with different
sha resolve concurrently and emit counter reuse. A pending resolution stores
`vv=max(L,R)`; only its next publish increments `me`, even if bytes match the
ledger sha. Fast-forwards clear pending. Store resolutions never count as user
supersession. During execution a complete readable replica record, or a family
complete over the replicas' merged parts, can preserve an edit; at quiescence
it must be current, displaced, expired after 30 days, or transitively
user-superseded. Restore excludes a version only when the
restored device had its last file/record/displaced witness and rollback removed
it. Path and source identities always match. Complete authenticated conflict-copy
families also count as transient records; incomplete or corrupt copies do not.
Deleting an already absent file creates no phantom authored version. Expiry
reachability requires an observed transition from retained bytes to an expired
row at least 30 days old; an empty compaction earns no coverage.

INV7 reconstructs local inputs from the before-ledger and source bytes, including
the unpublished `+me`, then binds each classification to authenticated record
entries. It threads multiple entries through the action and reconciles the final
source, ledger and sequence cursors. Dropped classifications and forged results
cannot pass by agreeing with their own history. Bootstrap inversion applies only
to the initial snapshot of a fresh join/rejoin. Existing drop-later and path-loss
mutants now trip INV7 earlier; their original INV1/INV2 clauses are independently
checked on the same broken state.

Two amendments to the checkers came with the family names. **INV4** no longer
accepts a reader's refusal of a peer's fork, nor the chain-anchor warning:
only the forked device refuses, and a refusal must be this device's own `fork`,
`fork-restore`, `fork-clone` or `missing-own-directory`, read from disk and live
hardware. **INV7** receipts name the family: a family applied at the next seq
moves the cursor, and one applied at or below the cursor (a second family at the
slot, or a late one) is legal only when its digest is not already applied and
leaves the cursor alone. Every other clause is unchanged, and every existing
mutant is still killed by the same invariant. One oracle correction came from the
deep search: a user deletion that no record carries and whose author's tree has
the file back (a restore, or the replay of the restored device's own record, undid
it) exists nowhere and is not a quiescent convergence target; a published deletion,
or one still in effect in its author's tree, stays one. INV2 already preserves every
user deletion, so its restore exclusion is unchanged. A second came the same way: a
disk restore also records, by exact identity (bytes under their vector,
carried by a ledger row, a live displaced row or a record complete over the
replicas' merged parts), the versions only the restored device carried and the
rollback removed. INV7 stops demanding those. INV2's line-set exclusion cannot see
this loss for a union file: a version that deleted lines looks held by any superset
elsewhere. A fresh-replica restore's loss is not excused this way; it stays the
pinned `fresh-replica-unreplicated` gap. Both restore judgments count a family whose
parts are split across other replicas as held, since the transport reassembles it,
and a family one replica holds completely stays held even when another replica has
a different part under the same name.
Ground truth applies §9 row 1's anchor: a complete own snapshot beyond `own_log`
anchors its seq at its `prev`, so a family there with another digest, or a `prev`
above it that does not chain to it, is `fork-restore`. The checkers group parts by
`(device, kind, seq, family)` through `layout.group_record_names` but keep their
own opening and completeness logic, independent of the model's `View`.

Replica audits authenticate canonical records and known fork signals. The
transport can also produce encrypted presence/snapshot conflict copies,
including nested copies. These must have the transport's exact conflict-name
grammar and open under their original canonical record identity; they do not
acquire commit/fleet fork semantics. Empty input is checked structurally;
nonempty pool markers (at least 16 bytes) must never appear in replica bytes.

## Known design gaps

The authoritative table is [KNOWN_DESIGN_GAPS in invariants.py](invariants.py).
Each row has a design section, SPEC outcome, dependent Tracks, owner decision,
strict regression node and the exact randomly generated case it gates. The
row/strict-xfail mapping is tested one to one. Restore recovery and the partial
own commit are designed and ordinary tests now (store-v2 §5, §6, §9; see
[Restore and torn-publish rules](#restore-and-torn-publish-rules)). Four rows
remain.

- `restore-current`: a current-policy store restore (the store folder rolled
  back under a live Syncthing index) is an **unsupported setup**, owner-accepted
  2026-10-16 and prevented by keeping the store folder and Syncthing's database
  out of backups; revisit when relay or backup exclusion lands. One strict
  parametrized node pins its three residuals at their first-firing invariants:
  an unapplied record deleted fleet-wide (INV2), a backup from before
  `fleet.json` arrived (INV4: every device waits), and applied versions the
  restored device never republishes over (INV1). Random current-policy restores
  stay gated by this row.
- `non-utf8-union`: non-UTF-8 union input loses bytes.
- `retire-delayed-presence`: retirement while an active peer's refreshed
  presence is delayed. Local presence cannot establish the full active set, and
  a covering snapshot need not reach that peer through the gap-driven reader
  rule. Random retirement keeps E4's fully delivered, snapshot-covered
  precondition; the delayed-presence schedule is pinned separately.
- `fresh-replica-unreplicated`: emptying a replica whose own records no peer
  holds yet destroys their only copy, owner-accepted 2026-10-16. Random
  fresh-replica restores require every own record file to exist on a peer;
  revisit when recovery can refuse or wait for replication.

Only the designated boundary's checked invariant becomes `ExpectedDesignGap`;
earlier or unrelated invariant failures cannot satisfy an xfail. Random restore
runs the saved, `state` and `fresh-replica` scopes (a `fresh-replica` restore needs a
surviving peer copy of the fleet, because a store with no surviving replica is lost,
not restored); a device a restore forks
(publishing before any of its own records or a peer's receipt has arrived)
refuses, and the lifecycle machine applies the documented remedy, `mm rejoin`,
before its final settle.

Example row shape (the table itself remains the source of truth):

```python
"restore-current": {
    "section": "§4, §9 rows 1/4",
    "outcome": "O1/O5", "tracks": "75A/76A/78A/80A",
    "decision": "owner-accepted 2026-10-16 (73C gate uc2a): unsupported setup, ...",
    "node": "test_scenarios.py::test_gap_restore_current_residual",
    "gates": "restore current",
}
```

Triage first: a model/oracle bug gets a regression and a fix. A confirmed
violation, silence or contradiction in store-v2 gets an unchanged invariant,
a `strict=True` xfail constrained to the expected invariant, a named random
precondition gate, and an inbox item in [docs/TODOS.md](../../docs/TODOS.md).
Remove the gate and strict pin when the design fix lands. A gap blocks the
owning Track's acceptance and 2.0.0 unless the owner records an explicit
decision. Green model CI with nonempty gaps cannot accept SPEC O1, O3 or O5.

Conservative readings, registered before search:

- Partial own commit: never complete, overwrite or delete a part. A full record
  before local state commit replays via §9. An incomplete own family that an
  intent row names was started by this device and is closed by a fresh complete
  family at the same seq; any other incomplete own family is in flight and the
  push waits. Partial snapshots are not bootstrap candidates; complete commits
  still permit convergence. Deep search later found that a state restore can
  drop the intent row and wedge the slot, so once the folder is synced and no
  receipt claims the seq, an attended push supersedes it like a torn slot;
  `autopush` keeps waiting.
- A still-alive retiree must be inactive at least 30 days. Active means an
  unretired presence newer than 30 days, plus the actor; every such device's
  newest complete snapshot must cover the retiree's last advertised/named seq.
  This local-presence reading now has a confirmed E23 gap: delayed refreshes can
  omit an active peer. Its strict regression keeps INV1 unchanged and blocks
  81A/2.0.0 acceptance pending a design decision, with 76A/77A dependencies.
- Freshest complete peer snapshot means greatest logical `created_at`, then
  device id and seq as deterministic ties. No ciphertext byte breaks a tie.
- Before `fleet.json`, join/push waits; fork signals still take precedence.
- Equal vv with unequal sha is concurrent, never dominance. It remains a
  design question for §4/9 despite safe model classification.
- Snapshots are write-once per family. Adopting a peer entry alone does not
  overwrite an existing snapshot; retirement coverage requires a genuinely newer
  snapshot. A snapshot raises other seq cursors and covers none of their
  families; a later commit of theirs applies at once, its `prev` unchecked when
  neither an applied family nor their own snapshot names the previous record.
- A corrupted own commit with a trusted own-log row stalls visibly; forced
  snapshot may seal trusted current state. Without trusted state, the model
  does not fabricate recovery. The deterministic corruption scenario propagates
  corruption to the owner and asserts both its reaction and the remedy.

## Failure replay

All rule arguments are plain repr-able pool/device/choice indices and seconds.
Paste the printed program into a **named deterministic regression before fixing
it**, retain observations, and set scoped roots and guard. No manual import of
`tests/conftest.py` is needed. Subagents or standalone runners must use a temp
HOME. The ordinary pytest invocation obtains the existing isolation fixtures.
For a replay blob or seed, decorate the no-argument factory with `reproduce_failure`
or `seed`, then call `run_state_machine_as_test(..., settings=...)` under the
same scoped context; a seed selects the deep profile only.

```python
def test_regression_example(tmp_path, sim_write_guard, monkeypatch):
    from mind_meld.store import format as codec
    from tests.sim.store_fleet import PART_BYTES
    from tests.sim.test_replication import ACTIVE, Invocation, CoreMachine
    monkeypatch.setattr(codec, 'MAX_PART_BYTES', PART_BYTES)
    token = ACTIVE.set(Invocation(tmp_path / 'replay', sim_write_guard))
    state = CoreMachine()
    try:
        # Replace these lines with the complete printed program.
        state.core_witness(case=0)
        state.observations_hold()
        state.edit(device=0, path=0, content=1, old_mtime=False)
        state.observations_hold()
    finally:
        try:
            state.teardown()
        finally:
            ACTIVE.reset(token)
```

An annotated failure has this form:

```text
INV2 no-silent-loss store-v2 §4/9/12 O1/O5;
subject=('claude', 'notes.txt', 7, 'a'); step=24;
expected/observed={last_holders: [], path_history: [...], quiescent: true};
last-moving piece=classify:real;
next=fix the model; replay tests/sim/README.md#failure-replay;
KNOWN_DESIGN_GAPS={...}; tests/sim/README.md#known-design-gaps
```

`subject` identifies source/path/authored event/device. The oracle's complete
path history and last holders explain loss; per-pull evidence includes vectors,
expected action, actual action and I/O. Refusal evidence compares the outcome
with independent disk/live-host ground truth. If a mutant survives, its error
names the invariant it was meant to kill. Unexpected invariant failures do not
qualify as the expected strict design gap.

## Replace a model piece

[registry.py](model/registry.py) owns all twelve entries and their typed call
contract. Collection validates signatures and provenance; real-labelled pieces
must execute, with no model fallback. The seam offers `read()` and atomic
`transaction()` over ledger, cursors, own log, displaced and persisted history.
An adapter accepts state seam, clock, live machine-id provider, replica/source
roots, fleet keys, a `family_source` (a fresh 8-hex family per publication) and a
`folder_synced` callback directly, without `CliRunner` or `activate()`. The real
publisher draws from an injected `family_source`; the seam also carries the
write-ahead `intents`, the `applied` family set and `cursors.fork`.

Read/reconcile/snapshot application writes one `record-applied` history receipt
(`peer`, `kind`, `seq`, `family`) after its per-entry `pull-entry` rows, in the same state
transaction as cursor advancement. INV7 authenticates that record, requires one
classification per entry, and reconciles all cursor and own-log changes. Publish
retains its `publish` receipt; these are checker inputs, never unchecked proof.
Each `pull-entry` that writes or deletes source bytes records `writer` from
`context.current_piece` (for example `classify:real`). After the action, the
wrapper checks that label against the dispatched registry piece and records
every checked write in the oracle, including intermediate writes and deletions.
The last source writer supplies the diagnostic label; actions without source
writes retain their execution trace. Causal checks still reconstruct inputs and
compare persisted results with observed files before accepting write history;
no model-to-oracle callback supplies observations.
The registered rejoin receives `new_id`, persists `meta.device_id`, and records
the old/new identities with the reset. The wrapper reads that field to adopt
the identity and validate namespace writes; the harness only allocates the ID.
All operations accept `context, **kwargs`; classify, resolve, retire and rejoin
also have their explicit required keyword arguments validated at collection.

| After Track | Pieces expected to be real, cumulatively |
|---|---|
| 75A | publish, presence; statedb-backed seam shared with remaining models |
| 76A | + snapshot, compact, reconcile; real crash-interruption cases |
| 77A | + read |
| 78A | + classify, resolve; persisted per-entry actions, E12 resolution contract |
| 80A | + create_fleet, join, rejoin |
| 81A | + retire; delivery receipt/status work remains with 81A |

Swap publish for real code (illustrative future interface; it does not exist
in product code yet):

```diff
+from mind_meld.store.publish import publish as real_publish
 registry = default_registry()
-registry['publish'] = Piece(model.publish, 'model', '75A', check)
+registry['publish'] = Piece(real_publish, 'real', '75A', check)
+fleet = StoreFleet(root, write_guard=guard, registry=registry,
+                   state_factory=SQLiteState)
```

The independent publisher fixture in
[test_mixed_publish_transaction_then_third_device_model_pull](test_scenarios.py)
seals real-shaped commits without calling model publish, commits pending-ledger
increments/own log/history together, and lets model pull on a third device read
the same seam. Its temporary `real` label tests dispatch, **not production
implementation**. Default runs still report every piece as model. Every promotion
runs bounded checks and deep search and records duration/result in its handoff.

A test-side executable model provides decisions to test before store Tracks
exist. Promoting its core now would ship unqualified behavior; a differential
oracle has no independent real implementation to compare; exhaustive small-scope
search would not cover these file-shaped crash/delivery histories at the stated
caps. Later Tracks may promote pure core functions, after qualification, with
peer names routed through `safety`. The I/O shell is never promoted. This suite
makes no claim of cryptographic writer authentication; fleet-key holders can
forge headers. SQLite transactions, real-process crash interruption, native
Syncthing and the mixed-OS soak remain their respective implementation/acceptance
gates. 2.0.0 needs zero model pieces and empty or explicitly owner-accepted gaps.

## Prerequisites

The sections below describe the legacy 1.x CLI fixtures.


Use a source checkout, Python 3.11+ and `./bin/check` (which installs the
existing dev dependencies). No Syncthing daemon, network, OS keyring account
or new dependency is needed by the fixtures.

**Real bootstrap and CLI execution require Track 71B's delivered write-site
guard.** `conftest._lookup_write_guard` binds
`fsutil.refuse_real_state_write_under_pytest` and checks its active pytest
marker and classification of the fixture root before bootstrap or activation.
No preflight file is written. If the actual symbol is missing/unbound, the
real-CLI fixture fails before bootstrap with the problem, cause and remedy.
Do not skip/xfail the smoke, bypass the gate, or land a red full suite.

The delivered guard protects shared-helper writes to account config, iCloud
and Syncthing storage, and local mm data. Its [documented C1 gaps](../../docs/invariants/README.md#test-write-guard)
in direct writers and other destinations remain a 71B acceptance obligation;
its presence does not prove suite-wide safety. Device path redirection and
the finite whole-fixture isolation assertions below supply separate harness
proof. No universal filesystem-monitoring claim is made.

Transport-only checks need no real CLI bootstrap:

```sh
./bin/check tests/sim/test_transport.py
```

With the prerequisite delivered and bound, start with:

```sh
./bin/check tests/sim/test_devices.py::test_one_file_two_devices
./bin/check tests/sim
./bin/check
```

`sim_fleet` is function-scoped: one bootstrap, devices `a` and `b`, seed `0`,
independent home/state/replica roots below that test's `tmp_path`. It calls
real current **1.x** CLI/scanning/encryption/storage/apply code. The fake
replaces transport and external host effects. This smoke does not establish
2.0 correctness or native Syncthing, Keychain/wheel CI or live-soak acceptance.

This excerpt is from `test_one_file_two_devices`; delivery is an explicit
step between push and pull:

```python
def test_one_file_two_devices(sim_fleet):
    a, b = sim_fleet.device("a"), sim_fleet.device("b")
    a.edit("projects/-sim-project/memory/note.md", b"hello from a\n")
    pushed = a.push()
    assert pushed.exit_code == 0, pushed.output
    assert not (b.source_root / "projects/-sim-project/memory/note.md").exists()
    assert sim_fleet.transport.drain().quiescent
    pulled = b.pull()
    assert pulled.exit_code == 0, pulled.output
    assert (
        b.source_root / "projects/-sim-project/memory/note.md"
    ).read_bytes() == b"hello from a\n"
    assert a.replica_root != b.replica_root and a.state_root != b.state_root
    assert a.home != b.home and a.hardware_id != b.hardware_id
```

## API and defaults

| Operation | Result and effect |
|---|---|
| `SimFleet(tmp_path, write_guard=guard, seed=0).bootstrap(names=("a", "b"))` | Fleet; guarded `_bootstrap_legacy` once, identical seeds/common transport baseline, no pending deliveries. Device count comes from names. |
| `fleet.device(name)` | Device with `home`, `source_root`, `state_root`, `config_path`, `replica_root`, `mm_id`, `hardware_id`. |
| `device.edit(relative_path, bytes, mtime=None)` / `delete(relative_path)` | `None`; source mutation only. Default mtime is fixed epoch + a fleet-wide monotonic edit counter; explicit mtimes use `os.utime` and do not move product time, so a stamp over 60 s ahead of it is clamped by mm as clock skew. |
| `device.push()` / `pull()` / `invoke(args)` | Original real `CliRunner.Result`, including exit status, stdout, stderr and exception. Scans that replica after command return; never delivers. |
| `transport.scan(device)` | Tuple of immutable new event IDs for that replica's changed bytes/metadata and true deletions. Accepts a device or transport name. |
| `pending()` | Stable tuple of `Choice(event, src, dst, path, ready_at, stage, reasons)`; blocked choices remain visible. Author and source holder are separate. |
| `deliver(choice)` | One whole-file delivery; bool says whether it newly recorded receipt. Replay is safe. Current eligibility is rechecked even for an older saved choice. |
| `delay(choice, delta)` / `advance(delta)` | `None`; finite nonnegative virtual seconds. Delay is per event/destination; advancing never delivers or moves product time. |
| `set_online(device, bool)` | `None`; changes transfer availability without dropping work. |
| `choose()` | Optional seed-local random ready choice, or `None`. Uses the same visible choices; no global RNG. |
| `drain(max_steps=1000)` | `Drain(delivered, remaining)`; deterministic ready steps only, lowest event ID first, so captured intermediate versions are delivered before newer ones. Use explicit choices to skip them. `quiescent` means nothing remains, including open replacements. Never sleeps or advances time. |
| `begin_replace(choice)` / `finish_replace(token)` | Open `Replacement` token / `None`. Begin requires an existing destination and new live event. Finish is a local action, including offline. |
| `fleet.snapshot(device)` | Frozen complete local-disk bytes/file metadata, directories and per-device transport index. |
| `restore(device, snapshot, index_policy="saved" or "current")` | `None`; explicit policy required. Global event records and peers remain; `saved` resets this device's holdings to its saved knowledge, while `current` retains them. |
| `clone(device, name)` | Independent device with inherited index and mm identity, fresh hardware/transport identity and narrowly rebased config/sidecar fields. No event authored by copying alone. |

## Scheduling

Use explicit choices for adversarial tests and eventual Hypothesis shrinking:

```python
transport = sim_fleet.transport
step = next(c for c in transport.pending() if c.dst == "b" and not c.reasons)
transport.delay(step, 5)
transport.advance(5)  # Still no delivery.
transport.deliver(step)
```

For an existing destination, exercise a real missing pathname:

```python
import pytest

b = sim_fleet.device("b")
step = next(
    c for c in transport.pending()
    if c.dst == "b" and not c.reasons
    and (b.replica_root / c.path).is_file()
    and transport.events[c.event].file is not None
)
destination = b.replica_root / step.path
old = destination.read_bytes()
token = transport.begin_replace(step)
with pytest.raises(FileNotFoundError):
    destination.read_bytes()
assert transport.scan("b") == ()  # Scheduled absence is not a deletion.
transport.finish_replace(token)
assert destination.read_bytes() == transport.events[step.event].file.data
```

The example assumes no competing local edit. A new local file during the gap
is captured against pre-gap ancestry and reconciled, preserving both differing
versions. A token exclusively owns its destination/path; competing deliveries
cannot publish there, including through a generated conflict copy. Acceptance
may discover a blocked copy after preserving other copies; the original delivery
stays pending with `replacement-in-progress`, and drain leaves the token open.
Finish it explicitly, then retry remaining work. Duplicate finish is an error.

Remaining reasons are `offline`, `delayed`, `replacement-in-progress` and
`step-limit`; only otherwise-ready work receives `step-limit`. A remaining
result is not success at quiescence. An online holder
can relay both a file and a tombstone while its author is offline. Captured
bytes never reread a later source revision. Event IDs are unique for the
fleet lifetime; observed ancestry, rather than numerical ordering, decides
causality. Stale and duplicate delivery cannot resurrect a causal deletion.

## Lifecycle

Snapshot, restore and clone require **idle**, not merely offline: no active
CLI context in the process and no unfinished incoming replacement on that
device. Errors identify the device/event and say to exit activation or call
`finish_replace` before retrying. No token is silently cancelled.

`saved` restores snapshot index knowledge along with disk bytes and resets
that device's event holdings to its saved knowledge. New edits cannot reuse
event IDs or pretend to have observed undone versions. `current`
keeps current transport knowledge while rolling disk bytes back; the next
scan authors the changed files and deletions, including files created after
the snapshot. Both policies preserve global event records and other devices'
holdings. After a `saved` restore, an event absent from the saved index is
deliverable only if another device holds it; old choices naming the restored
device as its source are rejected. An offline write rolled back before any
peer receives it has no holder and cannot later transfer on reconnection.
Already opened replacement tokens still finish locally from their captured
payload. This retained-history model is a deliberate approximation, not a
native index-backup protocol or proof of recovery of unsynced writes.

Copying preserves file bytes, mode, mtime, and empty directories with their
modes (device root included). Database,
WAL and SHM names are copied together as opaque files after owned handles
close; this does not prove SQLite recovery. Failed staging keeps the original
disk/index, annotates the original I/O error and permits retry. A failed
directory rollback names the retained backup. Restore commits disk and index
before removing its backup; a cleanup failure names that backup and leaves a
consistent device. Restore/clone never follow a
symlink. Clone rebases only config storage/source paths and sidecar source
`base_path`; user text, other fields and replica ciphertext stay unchanged.
Path-keyed caches (token/host usage, events) therefore still name the
original roots and may rescan in the clone. A clone that fails to parse or
rebase is removed and never registered.

## Activation

Devices run sequentially within one process, across fleets as well. Nested
activation fails before changing state. An activation entered without exit
fails the `sim_fleet` test at teardown, which undoes its patches; its late
unwind never clears a later activation. `MM_PYTEST_WORKERS` controls separate
pytest processes; each worker must own independent temporary roots.

Activation loads the installed version/path owners before changing HOME and
XDG values, redirects the explicit root-conftest path inventory plus the
import-time storage/iCloud defaults (`diag` falls back to them on broken
config), gives crypto and warn-once state a fresh per-activation copy as a new
process would, and restores globals/environment in `finally`. It mutates
the mm-events default dict in place and preserves its default-path matcher.
The installed version is asserted unchanged. Keyring, updater (including the
release-tag fetch, which fails as offline), diag's Grok skill probe, skill
installation/retirement, init's `brctl` iCloud pin and external identity/root
discovery (including the `git`/`gh` refresh behind init and
`refresh-identity`) are stubbed. Unsupported through `invoke`:
`mm cursor-agent` (spawns a real host binary) and `mm retro-fleet` (its
aggregator fixes the events directory at first import, launches an unstubbed
child `mm devices`, and windows by the real clock). Source scans, AES-GCM random nonces, storage writes and apply decisions remain
real. Skill installation cannot create symlinks in lifecycle snapshots.

Whole-`tmp_path` inventory comparison permits changes only beneath the active
device root. Existing symlinks beneath that root are rejected before activation
patches or writes; a new or changed symlink anywhere is a failure. There is no
extra allowlist. Directory entries/modes and file
bytes/modes/mtimes are compared; directory mtimes are excluded because parent
mtimes also change when allowed children change. This finite assertion is
additional isolation proof; 71B separately protects real account/storage
write sites. Original command exceptions retain isolation notes; returned
CLI failures remain accessible on an isolation error's `cli_result`.

The current time audit scopes owning `datetime` references for manifest
tombstones, retention/usage windows, pull prediction, pull history, per-project
sync logs, capture attempts and promote names. `conflictmtime` gets its own
time reference for clamping/bumping. Crypto backup, identity-cache and upgrade
stamps, and deadline/elapsed timers, stay real. Product time is the fleet
stamp counter: it advances on fixture edits, on each replica file a command
changed, and on each scanned deletion. It never follows `advance()`, but a
schedule that changes when deletions are scanned shifts later stamps, so
shrinking a schedule can move them. Commands that change no replica file
share a timestamp (same-second conflict names then take the product's random
suffix); tombstone expiry and retention windows are not reachable. Future
2.0 retention/identity clocks have no implementation here.

## Paths

Empty, absolute, traversing and symlinked paths, duplicate/overlapping roots
and occupied clone destinations are refused before mutation. Use a contained
relative path and a fresh device name. Bad inputs raise `ValueError`; busy
activation/lifecycle/scheduling raises `RuntimeError`; I/O errors retain their
original exception and device/path notes. See the section named in the error.

## Failure replay

Ordinary failed tests using `sim_fleet` receive a pytest report section with
seed, the last 200 actions, explicit event/source/destination/path/stage
choices, pending reasons and the latest real CLI Result. Unrelated tests do
not receive it. Replay the same fixture edits and explicit choice sequence,
or use the seed with the same actions and `choose()`. The finite suffix alone
does not reconstruct a longer truncated scenario; 72A owns shrinking. No
persistent report, daemon or background thread is created.

## Model sources and approximations

Document-backed describes the primary source, not tested native conformance.
No daemon conformance has been run. The owner and consuming store-Track
implementer must revisit an approximation when a correctness claim depends
on it; the existing live soak does not by itself calibrate every hazard.

| Behavior | Primary source | Fixture approximation / limit |
|---|---|---|
| Whole-file replacement | [Syncthing syncing: temporary files](https://docs.syncthing.net/users/syncing.html#temporary-files) | Complete bytes publish via local rename; no block-transfer protocol. |
| ENOENT window | [Store design §12](../../docs/designs/store-v2.md#12-verification-prove-it-before-trusting-it) | Deliberate adversarial unlink/pause/replace. High-level Syncthing docs do not establish this gap's frequency. |
| Conflicts | [Conflicting changes](https://docs.syncthing.net/users/syncing.html#conflicting-changes), [2.1.0 `WinsConflict`](https://github.com/syncthing/syncthing/blob/v2.1.0/lib/protocol/bep_fileinfo.go#L208-L226) | Preserves differing concurrent bytes and propagates copies; deterministic fixture timestamp/identity, not native device-ID arithmetic. |
| Deletes | [FAQ: synced changes](https://docs.syncthing.net/users/faq.html#what-is-syncthing) | Explicit tombstone events and retained ancestry; no native index implementation. |
| Offline transfer / relay | [FAQ: synchronization](https://docs.syncthing.net/users/faq.html#is-synchronization-fast) | Online holders relay captured full payloads; no topology/discovery/network timings. |
| Restore/index mismatch | [FAQ: folder marker](https://docs.syncthing.net/users/faq.html#i-am-seeing-the-error-message-folder-marker-missing-what-do-i-do), [2.1.0 `Counter`/`max(old+1, now)`](https://github.com/syncthing/syncthing/blob/v2.1.0/lib/protocol/vector.go) | Explicit saved/current index policies and retained queue, plus the `state` and `fresh-replica` scopes; not native backup/recovery conformance. Syncthing 2.1 gives a rewrite of an existing name the counter `max(old+1, now)`, so a restored device's rewrite silently dominates and mints no conflict copy; the fixture's same-name conflict copies are therefore optimistic for mutable files, and record names carry a per-publication family so commits are immune. Presence keeps one name. |

For concurrent heads, higher captured mtime wins, then smaller lexical
transport identity; same-author ties use event ID. These fixture keys never
use encrypted payload hashes. Equal content collapses without a conflict.
If deletion wins, edited bytes survive in a conflict copy; if the edit wins,
they stay canonical. New authored edits inherit all observed path ancestry.
For this project's Syncthing >=2.1 target, the newer-mtime rule also applies
to edit/delete concurrency. Verified against the 2.1.0 source:
[`WinsConflict`](https://github.com/syncthing/syncthing/blob/v2.1.0/lib/protocol/bep_fileinfo.go#L208-L226),
its [newer-deletion regression](https://github.com/syncthing/syncthing/blob/v2.1.0/lib/protocol/conflict_test.go#L13-L29),
and [database ordering](https://github.com/syncthing/syncthing/blob/v2.1.0/internal/db/sqlite/folderdb_update.go#L554-L590).
The [1.29.7 comparator](https://github.com/syncthing/syncthing/blob/v1.29.7/lib/protocol/bep_fileinfo.go#L141-L168)
preferred modifications over concurrent deletions; that older rule is not
used here. A deletion's mtime remains its scan-time fixture stamp; this
source comparison establishes the ordering rule, not native conformance.
`begin_replace` accepts only an event that would win the canonical path; a
losing version is delivered as a conflict copy without a rename gap.
Conflict names use `filename.sync-conflict-date-time-modifiedBy.ext`, with
fixed epoch + losing event ID as time and its author as `modifiedBy`. A copy
is minted once per receiving device; afterwards its own edits and deletion
govern it, and later concurrency never regenerates it. If an unindexed file
already holds that name (for example after a `current`-policy restore), the
copy meets it under the same rule, so a differing loser gets a nested copy.

Unmodeled path shapes: a directory replaced by a file (deleting children
leaves the empty directory, so the file cannot publish), case- or
Unicode-normalization aliases that one filesystem treats as one name, and
conflict names over the 255-byte component limit. The first and last raise
an I/O error that stays pending; aliases produce phantom events. Keep fixture
paths clear of them.

The exact scan ignore set is the simulator-owned `.mm-tmp-*` namespace,
`.DS_Store`, `.stfolder`, `.stignore`, `.stversions`, and Syncthing's
`.syncthing.*` / `~syncthing~*` namespaces. Ignored directories are pruned.
Ordinary `*.tmp` and conflict copies remain opaque transport files. The mm
product's independent source allowlist/exclusions continue to apply on push.
Native Syncthing ignores only its own markers and temporaries; `.mm-tmp-*` and
`.DS_Store` are ignored here because the store design has mm's own
`.stignore` exclude them. The fixture does not read a replica `.stignore`, so
it cannot detect a missing or wrong one. Store Tracks that write `.stignore`
must add that check.

## Later owners and evidence

71D supplies the apparatus for SPEC O1/O3. Track 72A owns Hypothesis and the
seven store invariants. Tracks 75A/77A adapt push/pull; 76A owns actual hardware,
clone refusal and restore behavior; 80A replaces `_bootstrap_legacy`; 83A
removes legacy wiring. The synthetic `hardware_id` is provisional fixture
metadata until its actual product reader lands.

Record ordinary check durations and measured size in the implementation
handoff. Human time to first useful result, cold bootstrap and per-step costs
remain unmeasured unless observed. Under five minutes is an onboarding target,
not a timing gate or a machine-specific README benchmark. Portable fixture
passes remain separate from hosted macOS Keychain/wheel CI, future 2.0
invariants, native transport conformance and the owner's week-long Mac/Linux
soak.
