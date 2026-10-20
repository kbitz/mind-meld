> SUPERSEDED as the 2.0 design on 2026-10-19 by the [current project
> spec](../SPEC.md): 2.0 carries the 1.x store onto Syncthing instead. Current
> only as evidence for that work: the "Why a new store" hazard table, §7
> portable paths, §10 Syncthing folder options and §11 Linux. Everything else
> is historical rationale, not a source of current scope, scheduling or release
> policy.
>
> Track 76A removed the abandoned code and simulator; `MM_SIM_DEEP` was
> removed with the simulator. Its model, invariants and scenarios encoded
> store-v2 rules. Its fake transport and two-device harness
> (`tests/sim/transport.py`, `disk.py`, `devices.py` and their tests) imported
> no store code and drove the real 1.x CLI; they are retrievable below.
> Fake-Syncthing conformance was killed on 2026-10-19, so they are reference,
> not a planned rig.
>
> Retrieval: the deleted code and tests were present at
> `e013df3e85c7b797fe0aeb351da0e5695e000c7a` (Track 76A's base on `main`).
> List the paths, then inspect a file without creating a worktree:
>
> ```sh
> git ls-tree -r --name-only e013df3e85c7b797fe0aeb351da0e5695e000c7a -- src/mind_meld/store tests/sim tests/test_store_format.py docs/invariants/store.md
> git show e013df3e85c7b797fe0aeb351da0e5695e000c7a:src/mind_meld/store/envelope.py
> ```

# mm 2.0 store: per-device encrypted logs over Syncthing

> Written 2026-10-09/10 against `98e9a69` (v1.6.0). Admitted to
> [the roadmap](../ROADMAP.md) as Phase 4 on 2026-10-11. This design replaces
> the 1.x storage format and moves mm from iCloud Drive to Syncthing.
> Conflict policies, three-way merge and the resolver UI build on it: see
> [conflict policies](../archive/conflict-policies.md).
>
> **Maintainer decisions:**
> - 2026-10-09: breaking changes are acceptable in this MAJOR. Prefer the
>   most correct design over compatibility with 1.x internals.
> - 2026-10-09: an always-on peer is optional.
> - 2026-10-10: end-to-end encryption stays.
> - 2026-10-10: local sync state moves to SQLite; the existing usage caches
>   stay as they are.
> - 2026-10-11: trim the sync scope before 2.0. Generator output (venvs, app
>   bundles, archives, QA evidence) is not sync data; see P1.
> - 2026-10-18: size 2.0 to the owner's fleet ([project spec](../SPEC.md)
>   amendment). The owner pairs machines and sets the folder options in
>   Syncthing; mm reads and reports Syncthing's state. Invite codes, mesh
>   healing, relays, folder setup through the REST API and scan-after-push
>   (§8, §10, the §5 step 6 rescan, §13's invite step, the `invite` and
>   `relay` commands under "AGENTS.md changes at 2.0", the Outcome's "two
>   commands", and decisions 2 and 5) stay here as deferred design. `mm devices
>   retire` checks 30 days of inactivity only, without the §8 coverage rule;
>   union input that is not UTF-8 resolves newest-wins with the loser in
>   `displaced`.

## Outcome

Every Mac and Linux machine shares one Syncthing folder. Adding a machine
takes two commands. A change propagates as a fast-forward unless two
machines really edited the same file. A partly synced folder is a normal
state that mm reports honestly, never an error. `mm status` says which
machines have received your latest push. iCloud Drive is retired.

## Principles

The store follows the transport's rules instead of fighting them. Syncthing
guarantees only that each **file** appears whole: no ordering across files,
no multi-file atomicity, no single writer.

1. **One file is one commit.** Nothing in storage references another file
   that may not have arrived yet. A push's content travels inside its commit.
2. **Write-once.** Commits and snapshots are never modified. The only
   mutable file is each device's small, self-contained presence record.
3. **Single writer.** A device writes only `dev/<itself>/`. The one
   exception is `fleet.json`, created exactly once by `mm init --new-fleet`.
4. **Order comes from sequence numbers, not arrival.** Readers apply each
   peer's commits contiguously and stop at a gap.
5. **Causal versions decide.** Every path version carries a version vector.
   Modification times never decide correctness.
6. **Owners alone compact.** No device ever reasons about deleting another
   device's data. Explicit `mm devices retire` is the only exception.
7. **Transport-agnostic.** These rules make mm correct on any per-file,
   eventually consistent replicated folder. Syncthing-specific code only
   automates setup and reports status.

## Why a new store

The 1.x store is per-device full-snapshot manifests plus a shared
content-addressed blob store, built for iCloud. Under Syncthing it fails in
ways that patches can only narrow. Verified against HEAD and Syncthing
v2.1.6 on 2026-10-09:

| 1.x hazard | Evidence | Under the 2.0 store |
|---|---|---|
| **Init on an unsynced folder mints a second `root_salt`.** Syncthing keeps the newer copy and renames the fleet's real one to a conflict name mm cannot see. The remedy text then tells users to re-run `mm init`, which re-keys every machine and orphans every blob. | `cli.py:3465-3494`, `cli.py:525-532`, `crypto.py:436`, `storage/local.py:36-44` | `fleet.json` is created only by `--new-fleet` on an empty folder. A joiner waits for it to arrive and never writes it. |
| **GC deletes peers' blobs** whose manifests have not arrived. Syncthing carries the deletion back to the owner. Possible on iCloud today. | `cli.py:7707-7801`, `cli.py:3934-3958` | Owners compact only their own logs. Nothing else is deleted. |
| **A manifest arrives before its blobs**, which yields a per-file `failed` and a false `degraded`. | `cli.py:2818-2825` | Commits are self-contained. A gap reads "waiting for push #N from X". |
| **Brief ENOENT while Syncthing replaces a file.** Syncthing deletes the old file, then renames the new one into place. | Syncthing `performFinish` | Commits are never replaced. |
| **Foreign files in listings** (temp files, conflict copies), and conflict copies counted as devices | `storage/local.py:121-134`, `devices.py:296-324` | A strict layout parser; everything else is counted as foreign in `mm diag`. |
| **Restored or cloned machine** | `devices.py:231` | Commit headers carry a hashed machine fingerprint and a hash chain (§9). |
| **Plaintext content hashes in blob names** | `cli.py:2805`, `storage/keys.py` | Filenames are sequence numbers. |
| **Claude project folders named after the absolute path** (`-Users-<name>-*` on macOS vs `-home-<name>-*` on Linux) | `manifest.py:1405`, `cli.py:5105` | Wire paths are relative to the home folder (§7). |
| **Every peer edit to an untouched file is a conflict**, because there is no last-synced base | `cli.py:2006-2021`, `cli.py:5101-5122`, `merge.py:185` | Fast-forward by version vector (§4). |
| **Line deletions in `MEMORY.md` and `.jsonl` never stick.** Union merge runs on every update, even one-sided ones. | `cli.py:2592-2593`, `merge.py:46-53` | Union runs only on concurrent versions. |

## 1. Layout

```
~/Sync/mind-meld/                     Syncthing folder id: mind-meld-<fleet8>
  fleet.json                          plaintext header; write-once
  .stignore                           local, written by mm: (?d).mm-tmp-*  (?d).DS_Store
  dev/<device>/log/000000001043.3f9a1c2e.enc   commit; large ones split: 000000001043.3f9a1c2e.2of3.enc
  dev/<device>/snap/000000001000.7b04d6e1.enc  full-state snapshot (same part rule)
  dev/<device>/presence.enc           small mutable record
```

Only names matching this grammar are read. Anything else, including Syncthing
temp files and `*.sync-conflict-*` copies, is foreign: `mm diag` counts it.

**Family.** A commit or snapshot name carries a *family*: eight lowercase hex
characters drawn fresh for each publication. All parts of one publication share
one family and one part count, and one family name has exactly one part count.
A slot `(device, kind, seq)` can hold several families, and each is a distinct
record. Syncthing 2.1 resolves a rewrite of an existing name with
`max(old + 1, now)` counters, so a restored device that republishes a seq under
the old name silently dominates it and no conflict copy appears. A new family
makes that rewrite a new file instead. The family is a name, not a secret or an
ordering: apply order is a pure function of names (§6), and nothing may assume
that a later family sorts later.

A conflict copy of a commit, or of `fleet.json`, is still a **fork signal**.
What a fork signal does depends on who sees it (§6, §9): the forked device
refuses; its peers warn and keep applying.

## 2. Formats

**`fleet.json`** is plaintext, because it is needed before any key exists:
`{format: 3, fleet_id, created_at, kdf: {argon2id memory_kb, time, parallelism}, salt, keycheck}`.
`fleet_id` is 16 random bytes. The master key is
`Argon2id(passphrase, salt)`, as in 1.x.

**Envelope 0x03.** Payloads are gzip-compressed, then sealed with AES-256-GCM
under per-kind subkeys derived from the master key, with a random nonce. The
associated data binds `(fleet_id, device_id, kind, seq, part, nparts)`, so a
file renamed to another fleet, device, kind, seq, part or part count fails
authentication. The family is not in the associated data: the payload digest is
the record identity for de-duplication and fork comparison, and binding the
family comes with the next version byte.

**Commit payload.** Length-prefixed frames:
- **Header:** `{fleet_id, device_id, host, seq, prev, created_at, mm_version, entries}`.
  - `host` is `sha256(fleet_id ‖ machine-id)[:16]`; the machine id is
    `/etc/machine-id` on Linux and `IOPlatformUUID` on macOS.
  - `prev` is the sha256 of the previous commit's payload.
- **Each entry:** a metadata frame `{source, path, vv, sha, size, mtime, deleted}`,
  then a raw content frame (absent for deletions). `sha` is the plaintext
  sha256 and is verified after decrypting. A payload over 8 MiB is split
  into parts. A commit is applicable only when all `nparts` are present.

**Snapshot payload.** The same framing. The header adds
`cursors: {peer: seq}`, the peers' commits it reflects, and `prev`, the
payload hash of commit `seq`, so the chain stays checkable after compaction.
Entries are this device's full state:
- every live path with its content;
- every tombstone;
- concurrent versions it holds unresolved (marked `held`).

Tombstones are path plus vv and are never dropped, so there is no
resurrection horizon. This Mac's last-push sidecar holds 15,415 today, a few
MB sealed; a horizon keyed to every peer's snapshot cursors can follow if
that ever matters.

**Presence payload.** `{device_id, device_name, os, mm_version, host, syncthing_id, registered, last_active, published_seq, snapshot_seq, applied: {peer: seq}}`.
It is rewritten atomically (temp file plus rename) at the end of push and
pull when something changed, and at most hourly otherwise. A device whose own
records are still arriving does not rewrite it (§9).

**Delivery receipts.** A receipt names the digest of the family a peer applied
at a seq, and `mm status` says "has your latest push" only when a peer's
receipt matches the owner's newest `own_log` seq *and* digest, so a peer that
applied a pre-restore record under the same seq does not count. The presence
field that carries the digest arrives with delivery receipts; until then
`applied` holds only the seq.

`mm devices --format json` projects presence onto the existing contract
fields: `device_id`, `device_name`, `registered`, `last_seen` (=
`last_active`) and `last_seen_version` (= `mm_version`). retro-fleet's
subprocess reader stays unchanged.

## 3. Local state: `~/.config/mind-meld/state.db`

SQLite through the lazily imported standard-library `sqlite3`: rollback journal
(`journal_mode=DELETE` only at file creation), `busy_timeout=5000`, mode 0600,
`synchronous=FULL` without fullfsync. History has no hard macOS power-loss
durability promise. 75A re-decides journal mode and fullfsync with measured
ledger data; connect never resets a later migration's journal mode.
Forward-only atomic migrations use user_version; meta.compat_version gates
only incompatible reader changes. Newer compatible schemas remain usable.
The mm lockfile serializes commands; each write transaction is the innermost
lock. Read-only inspection creates nothing and closes before output.

73B activates history on 1.x. Exact columns, readers and migrations are in
[the history schema routing table](../invariants/sync.md#schema-columns-and-readers)
and statedb.py's migration 1; the other tables are empty and provisional
until their owning Tracks, which may re-create them without raising the
history compatibility gate.

| Table | Holds | Readers |
|---|---|---|
| meta | compatibility, import marker, pending import, legacy file stats (future fleet keys arrive with their owners) | statedb, mm log |
| `ledger` | `(source, path) → sha, canonical vv, size, mtime, deleted, pending`; no content: the version each local file corresponds to | 75A publish, 78A classification, preview |
| `cursors` | `peer → applied_seq, hash, gap_seq, gap_since, fork (kind, first_seen)` | 77A pull, 81A status, 75A presence, diag |
| `applied` | `(peer, seq, family, digest)` for every family applied or covered | pull |
| `own_log` | `seq → family, digest, prev, parts, bytes, created_at` | 75A publish, 76A compaction/reconciliation, heal, restore detection |
| `intents` | `(kind, seq, family, nparts)`, one row per attempted publication family | publish, reconcile, compaction |
| `displaced` | replaced local versions (bytes, reason, time, cause, peer); bytes cleared at 30 days, metadata and expired flag retained | `mm displaced`, gc, status, the fork warning, simulator INV2 |
| history | per-file outcomes, one-time legacy JSONL import, newest 20,000 rows per known class (no shared rotation) | mm log only; sync receipts need their own retained state |

`applied` keeps a few dozen bytes per applied record for the life of the peer's
namespace, so a late, duplicate or resurrected family is always recognised by
its name and digest. `cursors.fork` is written once, by the first fork evidence
for a peer (§6), and `mm status` and `mm diag` list the peer until its
namespace is retired. `displaced.cause` is `fork` for a version a peer's fork
displaced (§4) and `peer` names that peer.

[Conflict policies](../archive/conflict-policies.md) in 2.1 adds base (three-way merge
bases), conflicts (held versions), and `mm conflicts --suggest`. Usage,
identity, upgrade and token caches stay in their existing JSON files.

## 4. Causal versions

A version vector `vv` maps device ids to counters. A tombstone is a version
with no content. The ledger records, for each path, the version its local
file is believed to be.

**Authoring (push).** For each strictly scanned path with local sha `s`
(`_prove_omitted_paths_absent` still guards deletions):

| Ledger | Authored entry |
|---|---|
| no entry | `{me: 1}` |
| `ledger.sha == s` | nothing; unchanged |
| `ledger.sha != s` | `ledger.vv` with `me` incremented |
| file absent, `ledger` live | tombstone with `ledger.vv` + `me` |

Logs carry only **authored** changes; adopting a peer's version writes no
commit. Snapshots carry full state, so any single device's snapshot can
bootstrap a joiner.

**Classification (pull).** Each incoming entry `R` is compared with the local
version `L`, which is `ledger.vv`, plus `me` if the local bytes differ from
`ledger.sha` (an unpublished edit):

| Comparison | Action |
|---|---|
| same sha | merge vectors into the ledger; no I/O |
| `R` dominates `L` | **fast-forward**: write, or delete (a delete moves the old bytes to `displaced`) |
| `L` dominates `R` | ignore |
| concurrent | resolve (below); the result is authored with `max(L, R)` + `me`, so it dominates both and propagates as a fast-forward |

**Resolution in 2.0** uses the merges that exist today, both symmetric:
- `.jsonl`: `merge_jsonl`;
- `MEMORY.md`: `merge_lines`;
- anything else: **newest** (higher mtime; ties broken by higher sha). The
  loser goes to `displaced`.
- **live versus tombstone:** the live version wins. A tombstone has no
  mtime, so `newest` cannot apply, and an edit concurrent with a delete is
  the one case where silent loss is possible. The deleter sees the file
  return as a fast-forward and may delete it again; that delete then
  dominates. Bootstrap below is the only exception.

Per-path rules, three-way merge and `ask` arrive with conflict policies (2.1).

**Counter reuse after a restore.** An equal vector with a different sha is
concurrent: a restored device can republish counters it already used, and the
two versions resolve like any concurrent pair. Limitation: a version derived
from a pre-restore edit can dominate a post-restore edit of the same path.
Readers holding the dominating version ignore the post-restore edit and, when
fork evidence exists for that peer (§6), record it in `displaced` with
`cause = fork`, so the fork warning names it and its expiry. The rule keeps every
entry of that peer's families at or after the first fork seq that classifies as
`ignore`, including when the evidence arrives *after* the entry was ignored, so it
also keeps a dominated ancestor (the pre-restore version the dominating one was
derived from); the cost is a few extra `displaced` rows for 30 days. An entry is
retained once, and never again after its row expires. The restored
device keeps the post-restore edit current until it rejoins (§9), and rejoin
lists it in `displaced` with its expiry.

**Symmetry invariant (load-bearing).** Every automatic resolution is a pure
function of the unordered pair of versions (plus the base, from 2.1). Two
devices that resolve the same pair independently produce identical bytes,
the same-sha rule collapses their vectors, and resolution terminates. A
resolver that prefers "local" would ping-pong forever.

**Bootstrap.** With no ledger (a fresh 2.0 device), every file is authored
as `{me: 1}` in the first snapshot. Identical files across devices collapse
through the same-sha rule; only real differences resolve, once. One rule is
inverted at bootstrap: a peer tombstone beats a never-ledgered local file,
whose bytes go to `displaced`. Without it, every file still lingering on a
joiner would resurrect fleet-wide against the fleet's 15k tombstones. A
machine that missed the final 1.x round (§13) therefore joins only after its
sources are wiped, or is retired instead.

## 5. Publish (push)

1. Lock, then reconcile own storage (§9).
2. Scan sources strictly and diff against the ledger. If nothing changed and no
   own slot is torn (step 4), refresh presence if due and stop.
3. Build commit `seq = last + 1` with `prev`. Content is the exact scanned
   bytes, verified unchanged before sealing (the existing upload-verification
   rule).
4. Draw a fresh family. Record an **intent row** `(kind, seq, family, nparts)`
   in local state, then write each part with exclusive create (`os.link`; temp
   files named `.mm-tmp-*`). The commit exists once its last part exists. On
   `EEXIST` for any part, the parts already written stay (a torn family of this
   device's own), the publisher draws a fresh family and links the same sealed
   parts under the new names (the family is not in the associated data, so no
   re-seal is needed); after 3 attempts it fails loudly: "could not allocate a
   fresh record name for seq N after 3 attempts; another writer may share this
   device id; run `mm diag`". It never overwrites an existing file.
5. In one transaction: ledger updates, `own_log` row (with the family and
   digest), the published family's intent row cleared, history rows. A crash
   between steps 4 and 5 is recovered by §9's own-log replay.
6. Best effort: ask Syncthing to rescan `dev/<me>`. Write presence. In an
   attended push only, take a snapshot and compact if due (§6); `autopush`
   never snapshots.

**Torn publish.** A crash can leave an own slot with only some of a family's
parts. The next slot `seq` is *torn* when it holds no complete own commit
family and every incomplete family at it is named by an intent row: this device
provably started it. A torn slot is closed by writing a **fresh complete
family at the same seq**, re-authored from the unchanged ledger and the current
files, even when that commit has zero entries (peers wait on the seq
otherwise). Closure waits for the folder to sync (Syncthing idle with nothing
needed for record files; a peer's presence refresh alone never completes a
record and does not hold the gate), exactly like skip-ahead: an intent row proves some copy of this state
started the family, and a backup can carry the row back after the slot was
later closed, so while redelivery may still complete the slot the push stays
pending `own-records-in-flight` instead of superseding it into a fork. The torn family's parts are never completed, overwritten or deleted
by the push, and they stay readable to a peer that already holds them. The same
rule applies to a snapshot: an incomplete own snapshot family at the current
seq that an intent row names is closed by a fresh family. Any other incomplete
own family at the slot, one the device cannot prove it wrote, is in flight
(records still arriving after a restore): the push is pending
(`own-records-in-flight`) and never writes at that seq (§9) — with one escape:
once the folder is synced and no receipt claims the seq, an *attended* push (or
`mm push --snapshot` for a snapshot slot) may supersede it like a torn slot,
because its missing parts can only ever come from this device and intent rows
themselves can be rolled back by a state restore; `autopush` stays pending. If
a clone later completes the old family, the two families fork visibly (§9).

**Readers need no digest or total to tell a torn publish from a pending one.**
Both look the same to a reader, an incomplete family, and both resolve the same
way: wait for the missing parts, or for the owner's fresh family or a snapshot
that covers the seq (§6). `IncompleteParts` carries nothing more.

## 6. Snapshots and compaction

A device writes a snapshot when the log since its last snapshot outgrows
that snapshot, or after 200 commits, and only from an attended `mm push` or
`mm push --snapshot`. There is no time trigger: a snapshot is this device's
whole state (about 330 MB sealed for this Mac's 33k files), Syncthing gets no
block deltas from sealed files, and at this fleet's cadence of roughly two
pushes a day a weekly trigger would ship that to every peer every week. Nor
does a hook snapshot: sealing 33k files under the lock would blow the
`autopush` budget.

**Compaction deletes only what the owner can prove it wrote.** A family is
proven when `own_log` names it (a commit), when this device recorded it as its
own snapshot, or when an intent row names it (a torn or superseded attempt). Among those it deletes commit families at or below its **previous**
snapshot's seq once they are 14 days old, and snapshot families older than its
two newest distinct snapshot seqs (one family per seq counts). It never deletes
a family it cannot prove it wrote, at any seq: that may be the last copy of a
record a restored device is still waiting for.

**Slot rule.** A slot `(device, kind, seq)` may hold several families. Each is
judged from its names alone: a family is complete when every index of its one
part count is present. Only complete families are opened, and only a complete
readable family counts.

- An *incomplete* family is pending and ignored while a complete family exists
  at the slot; if it later completes it is a late family (below).
- An *unreadable* family (authentication or parse failure, or one family name
  with differing part counts) beside a complete readable family is ignored with
  a `mm diag` warning, for readers and for the owner. Alone at a slot it keeps
  the stalled-unreadable rule in the table below.
- Two complete commit families at one slot with *different payload digests* are
  fork evidence. Equal digests are one record, however the files are named.
- A snapshot slot may hold two complete families; that is not fork evidence.
  Fallback and retirement coverage use the newest complete snapshot, by
  (seq, `created_at`, family); a joiner picks by (`created_at`, device id, seq,
  family).

**Reader rule** for peer `P`, with `next = cursor[P] + 1`:

| Situation | Action |
|---|---|
| a complete readable family exists at `next` | apply every one not yet applied, once each (keyed by payload digest), in family-name order; the cursor moves to `next` |
| `next` has no complete readable family and `next ≤` P's newest complete snapshot seq | load that snapshot: classify every entry; set `cursor[P]`; raise each other cursor to `max(cursor[Q], snapshot.cursors[Q])` |
| `next` has no complete family and is beyond P's snapshot | pending; record `gap_since` |
| `next` has an unreadable family and no complete one | stop at it with an always-visible warning; skip to a later snapshot when one appears |

The owner's remedy for an unreadable commit is `mm push --snapshot`.

**Late families.** A complete readable family at or below the cursor that the
reader has neither applied nor covered is applied when it is first seen
complete (a restored device republishing a seq the reader already applied, or a
family that finished arriving after the reader moved past its slot). It is
applied exactly once, and the cursor does not move. A late family's `prev` is
checked once, when it is applied.

**Cover rule.** When a reader loads a peer's own snapshot, a commit family at a
covered slot counts as *covered* only if its digest was applied, or it lies on
the `prev` chain of that peer's snapshot (the snapshot's `prev` names the commit
at its seq; walk each retained family's `prev` down by digest, recording a seq
only when a retained family at that seq carries the named digest — a digest is
never assigned to a seq by arithmetic, so a skip-ahead commit whose `prev`
names an older seq ends the walk there). A cursor raised
for `Q` by *another* device's snapshot covers no family of `Q`'s. Every other
complete family at a covered slot is a late family. So "covered" means
incorporated, never merely "at or below a seq", and an unknown family that was
incomplete or complete when the cursor jumped is still applied once complete.

**Chain check.** A family's `prev` is compared with *every* applied family at
`seq - 1` (any match passes), or with that peer's own snapshot `prev` when
`seq - 1` is covered by that snapshot; when neither exists it is not checked.
There is no chain-anchor wait: a commit after a cursor raised by another
device's snapshot applies immediately.

**Fork evidence is a warning, never a refusal.** A reader treats these as
evidence about a peer: a second complete family with a different digest at a
slot, a `prev` that fails its check, a commit conflict copy under that peer,
and an applied family that vanished from storage while a different digest holds
its slot. The reader keeps applying every record it can. The first pull that
sees evidence writes `cursors.fork (kind, first_seen)` and warns in every mode,
and `mm status` / `mm diag` list the peer until its namespace is retired.
While any version the fork displaced (§4) is unexpired and unrestored, **every**
push and pull, attended or automatic, warns again with the peer, the displaced
count and the earliest expiry date, and leaves a breadcrumb that `mm status`
shows. Remedy texts (peer names go through `safety`):

- two complete families with different digests: "<peer> has two different
  records for push #<seq> (restored from a backup?); both versions were kept;
  run `mm rejoin` on <peer>"; when the families come from more than one host:
  "<peer> is used by more than one machine (record <seq> exists twice); run `mm
  rejoin` on each machine using this id";
- an applied family missing and a different digest at its slot: "<peer>
  republished an earlier sequence (restored from a backup?); both versions were
  kept";
- a `prev` that fails its check: "<peer>'s record <seq> does not follow the
  record applied before it; both versions were kept; run `mm diag`, then `mm
  rejoin` on <peer>";
- a commit conflict copy: "<peer>'s log has a conflicting copy of record <seq>;
  run `mm diag`, then `mm rejoin` on <peer>".

When displaced versions are at risk each text ends "; N displaced versions kept
until <date>; see `mm displaced` before then".

Once a rejoined device's presence names the superseded id (a field that arrives
with `rejoin`), readers show "<old id> rejoined as <new id>; old records kept for
history; retire after <date> with `mm devices retire <old id>`" instead of an
active fork. The displaced-expiry alert stays separate.

This rule needs no cross-device acknowledgements and never confuses
"compacted" with "in flight". Presence `applied` cursors exist only for
status. A pending gap becomes a visible warning in every mode after 24 h,
per the data-at-risk rule. Only the forked device refuses (§9).

Chain checks run only inside the retained window: the oldest surviving
commit's `prev` is compared with the newest snapshot's `prev` at that seq,
never with a compacted file.

## 7. Portable paths

Wire paths are `(source, rel_path)`. For the `claude` source, a project
folder whose encoded name starts with the encoded home prefix plus `-`
(`-Users-<name>-` or `-home-<name>-`; Claude maps `/` and `.` to `-`) is published
as `projects/~-<rest>`. Apply maps `~` back to the local encoded home. Paths
outside home stay absolute and machine-specific.

This assumes projects live at the same home-relative paths on every machine
(for example `~/src/<project>`). A `[[sync.path_aliases]]` table can follow
when someone needs different layouts per machine. Other sources are already root-relative and portable.

## 8. Fleet lifecycle

> Scope (2026-10-18): `mm invite`, mesh healing and the retire coverage rule
> below are deferred design; see the maintainer decisions.

- **`mm init --new-fleet [PATH]`.** Refuses unless the folder holds no
  `fleet.json` and no `dev/`. Prompts for the passphrase twice, writes
  `fleet.json` exclusively, publishes a snapshot and presence, and sets up
  Syncthing (§10).
- **`mm invite`.** Prints a code: base32 of fleet id, this device's
  Syncthing id, folder id and a checksum. It then waits up to 10 minutes for
  the joiner to appear as a pending Syncthing device. It shows that device's
  name and id and asks for confirmation before accepting and sharing the
  folder.
- **`mm join <code>`.** Ensures Syncthing is running, adds the inviter and
  creates the folder with the §10 settings and `.stignore`. It waits for
  `fleet.json` with progress output, and **never creates it**. Then it asks
  for the passphrase, verifies the keycheck and records the fleet in config.
  It bootstraps from the freshest complete peer snapshot, publishes its own
  snapshot and pulls.
- **Mesh healing.** During pull, mm adds any fleet device whose presence
  carries a Syncthing id this Syncthing does not know, and shares the folder.
  Pairing with one machine fills in the whole mesh.
  `[syncthing] manage = false` turns it off.
- **`mm devices retire <id>`.** The only write outside `dev/<me>`. It
  requires the device to be inactive for 30 days or more, and every active
  device's newest snapshot to cover its last seq. It then deletes
  `dev/<id>/`.

## 9. Restore, crash and clone safety

At the start of every push and pull, mm reads its own log listing and
presence from storage, and its peers' presence and snapshots for what they say
about this device. It decides in a fixed order: **(1)** refuse an own fork or a
foreign host, **(2)** the own-directory row, **(3)** replay own records,
**(4)** restored-device detection, **(5)** a torn own slot, **(6)** heal. Only
the forked device refuses; its peers warn and keep applying (§6). Every message
below is shown verbatim and ends with "See
docs/designs/store-v2.md#9-restore-crash-and-clone-safety.".

| Observation | Meaning | Action and text |
|---|---|---|
| Own commits beyond `own_log`, same `host` | Crash after publication, restore from backup, or lost `state.db` | Apply own commits like a peer's, via `cursor[me]`: local files fast-forward to this device's own newer state; then continue, with `seq` resuming after the newest commit in storage and `prev` chained from it. If storage holds a complete own **snapshot** beyond `own_log`, load it first like a peer snapshot (classify every entry, set `cursor[me]`), anchor `own_log` at its seq and `prev`, then replay retained own commits after it in seq order, so a backup older than the compaction floor loses nothing. Replay is contiguous: it stops at the first seq holding no complete own family (a hole left by partial redelivery) and never jumps it — restored-device detection owns the wait for the missing seqs, and no later snapshot is taken from a ledger that skipped a record its `prev` chain would mark covered for readers |
| `dev/<me>/` absent while `own_log` is non-empty | This device was retired (§8) and Syncthing applied the deletion, or the folder was lost or is refilling after a restore | While Syncthing still has items for this folder: pending, `own-records-in-flight`: "this device's records have not arrived yet (restored from a backup?); waiting for Syncthing to restore them". Only when the folder is synced and `dev/<me>/` is still absent: refuse, `missing-own-directory`: "this device's folder is gone from storage and Syncthing has nothing left to deliver (retired?); its newest publish was <date>; run `mm rejoin`" |
| Own commits or an own snapshot with a different `host` | Two machines share this device id (copied config or a migrated Mac) | Refuse, `fork-clone`: "another machine is using this device id; run `mm rejoin` on each machine using it" |
| Own chain diverged on this host: a complete own family whose digest differs from `own_log` at its seq, a second complete own family with a different digest, or an own `prev` that does not chain to `own_log` | A restored copy republished a seq its earlier self already published | Refuse, `fork-restore`: "this Mac republished records another copy of it already published (restored from a backup?); run `mm rejoin` on this Mac" |
| A conflict copy of an own commit or of `fleet.json` | Transport fork | Refuse, `fork`: "a conflicting copy of this device's log or of fleet.json exists; run `mm diag`, then `mm rejoin` on this Mac" |
| Someone claims own records this device does not have (below) | This device was restored and its records are still arriving | Pending, `own-records-in-flight`: "this device's records through push #<N> have not arrived yet (restored from a backup?); waiting" |
| An incomplete own family that an intent row names, at the next slot | The last push was interrupted | Pull reports pending, `torn-own-slot`: "the last push was interrupted; the next push completes it"; the next push closes the slot (§5) |
| The own snapshot at `snapshot_seq`, or an own family above it, is no longer complete in storage | Another machine's rollback deleted records this device published | Heal, `own-records-lost`: "records this device published are missing from storage; the next `mm push` publishes a full snapshot (about the size of this device's synced data)" |

`mm rejoin` continues under a new device id with the local bytes preserved as
current or `displaced`; the old namespace stays readable until it is retired,
and rejoin lists what it displaces, with expiry, tagged like a peer fork's.

**Restored-device detection.** Before any publication (a commit, a snapshot or
a rewrite of this device's own presence), reconcile computes `claimed`, the
maximum of this device's own presence `published_seq` (when readable and from
the same host), every peer's presence `applied[me]` and every complete peer
snapshot's `cursors[me]` (read once per snapshot family and remembered by
name). If `claimed` exceeds the newest `own_log` seq and this device's records
through `claimed` are not all complete in storage (own commit families at those
seqs, or an own snapshot covering them), push is pending with
`own-records-in-flight`. A pull still applies peers and replays own records but
does not rewrite presence. Waiting is correct while records are arriving; if
they never return, the fallback is **skip-ahead**: once the folder is synced
(Syncthing idle with nothing needed for record files, as in §5) and the records
are still incomplete, the
next *attended* push publishes at `claimed + 1` with `prev` set to the digest of
the newest complete own family, and a forced snapshot at that seq, so readers
waiting on the missing seqs fall back to the snapshot. Skip-ahead never writes
over a family already occupying its target: it advances past slots holding a
complete family (observed in storage, not thereby replayed — receipts alone
could never grow the claim past a record no peer applied) and still waits at
an incomplete one, and never past the largest seq the record grammar can name:
receipts can name seqs, they cannot invent them.
`autopush` stays pending
with an always-visible warning until then. A device that publishes before any of
its own records or a peer's receipt has arrived forks its id (row four); that is
the one residual in the restore guidance below.

**Heal.** While `dev/<me>/` exists, a missing own snapshot at `snapshot_seq` or
a vanished family `own_log` recorded above it (that is not a proven torn slot)
makes the next *attended* push publish a forced snapshot. A seq with no
`own_log` row is a hole replay has not reached — in flight, never a heal:
healing over it would publish a snapshot whose ledger never incorporated that
record while its `prev` chain marks the record covered for readers.
`autopush` and `pull` show the warning until then. Commits at or
below `snapshot_seq` never trigger heal (compaction removes them by design), and
an own presence behind own state only rewrites presence.

### Restore guidance

mm models what a restore leaves behind; it cannot make a store-folder restore
safe. The supported setups differ by scope:

| Scope | What rolls back | Outcome |
|---|---|---|
| State and sources only (the Syncthing folder and its database are intact) | `state.db` and source files | Row one replays own records; no fork, no rejoin |
| Everything including an empty store folder (a new disk, or a folder and Syncthing database restored together) | Everything; peers refill the folder | Row two waits for Syncthing, detection waits for own records, then row one replays; converges with no fork |
| Store folder restored under a live Syncthing database that remembers newer state | The folder only, behind the index | **Unsupported.** Syncthing can delete or never re-announce records the index believes it already delivered |
| Store folder restored with a fresh Syncthing database | Old mutable files re-announced as new | **Unsupported.** Presence is rewritten by its owner, and resurrected compacted families are skipped by name |

An unsupported restore is prevented by keeping the store folder and Syncthing's
database out of backups. When one happens anyway, late families reconcile a
restored device that republishes over its lost seqs and heal closes peers' gaps,
but three residuals remain: a record no device had applied when the restore
deleted it is gone fleet-wide; a backup taken before `fleet.json` arrived
deletes it fleet-wide, so every device waits; and versions readers applied from
records the rollback deleted stay divergent until the restored device
republishes over them. These are accepted as limits of an unsupported setup.

**After a restore:**

1. Before backups, exclude `~/Sync/mind-meld` and Syncthing's database
   directory (as printed by `syncthing paths`) from them. Do not exclude
   `config.xml` or its keys.
2. After a restore and before any agent session, comment out the mm hook lines
   listed in the README integration section.
3. Start Syncthing and wait until `mm status` shows no `own-records-in-flight`
   line and Syncthing reports the folder idle with nothing needed.
4. Run `mm pull`, then restore the hook lines.
5. If no peer is reachable, stay at step 3 and do not push. If no peer can
   return, run `mm rejoin`.

A machine that publishes before Syncthing has returned any of its own records
or a peer's receipt forks its device id and refuses until `mm rejoin`. An edit
made after such a restore can lose to a version derived from the pre-restore
copy; mm keeps it in `displaced` and warns until it expires (§4, §6).

## 10. Syncthing integration

> Scope (2026-10-18): 2.0 implements discovery, read-only status and the
> `mm diag` lint. Folder setup, scan-after-push, mesh healing and the relay
> below are deferred design; see the maintainer decisions.

`syncthing.py` is a leaf module with no CLI imports.

- **Discovery.** API key and address from `config.xml`.
  - Linux checks `$XDG_CONFIG_HOME/syncthing`, `~/.config/syncthing`,
    `$XDG_STATE_HOME/syncthing`, then `~/.local/state/syncthing`.
  - macOS uses `~/Library/Application Support/Syncthing`.
  - Fall back to `syncthing paths`.
  - Absent or unreachable means mm still works and status says so.
- **Folder settings it applies.**
  - Path `~/Sync/mind-meld`, which avoids iCloud Drive and macOS's protected
    Documents, Desktop and Downloads folders.
  - `sendreceive`; versioning off; `maxConflicts` 10 (never 0, which
    silently deletes losers); `ignorePerms=false`, so 0600 survives;
    `order=alphabetic`.
  - The `.stignore` from §1.
- **After push.** `POST /rest/db/scan?folder=…&sub=dev/<me>`, time-bounded,
  so peers hear about the push immediately.
- **`mm status`.**
  - Delivery receipts from presence: "laptop has your latest push;
    desktop is 2 pushes behind, last active 3h ago".
  - Pending gaps per peer.
  - Syncthing reachability, folder state, need counts and connected peers.
  - Push still exits 0 on local publication.
- **`mm diag`.** Lints the folder settings above, foreign-file counts, fork
  signals and the Syncthing version (≥ 2.1).
- **Relay (optional).** One always-on machine runs plain Syncthing, with no
  mm, as a `receiveonly` peer that auto-accepts the folder from fleet
  devices. It only ever holds sealed files.
  - `mm relay add <syncthing-id>` records it in this device's presence, and
    mesh healing pairs every device with it.
  - Not an "untrusted (encrypted)" Syncthing device: that mode is still
    beta, with open v2 delete bugs (syncthing#10528, #10424).

## 11. Linux as a first-class platform

- **Default storage path** `~/Sync/mind-meld` on both OSes. Remove
  `_auto_pin_storage_for_icloud` and `brctl`.
- **Keyring.**
  - In auto commands on Linux, check the Secret Service collection's lock
    state first. If it is locked, skip with a `keyring-locked` breadcrumb;
    never open an unlock prompt from a hook.
  - Catch the backend's transport errors.
  - Remedies give `secret-tool` next to `security`.
  - Verify on supported desktops that the default collection is unlocked
    before hooks run (for example gnome-keyring unlocked through PAM at
    login).
- **CI.** Add an `ubuntu-latest` job for the portable `./bin/check`; keep
  the Keychain and wheel smokes on macOS. Fix `tests/test_fsutil.py:190-242`
  (`monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)`). Add
  `~/Sync` to the harness's protected-paths list
  (`tests/test_memory_contract.py:1252`).
- **Prose.** `pyproject.toml` description and keywords, README
  prerequisites, init help, "other Mac" strings, the SPEC overview, AGENTS.md
  principles.

## 12. Verification: prove it before trusting it

**Replication simulator (built first).** `tests/sim/` runs the **real** store
and apply code for N devices. Each device has its own `tmp_path` home,
`state.db` and storage replica, joined by a fake transport that models
Syncthing:
- whole-file delivery, in random order, with arbitrary delays;
- the delete-then-rename ENOENT window, and deletions;
- devices going offline;
- restoring a device to an earlier copy: the whole disk (with the transport's
  saved or current index), only its state and sources, or a fresh empty replica
  that peers refill; and a cloned config;
- conflict copies on concurrent writes to one path.

A `hypothesis` `RuleBasedStateMachine` interleaves the following actions:
- edits, deletes, pushes and pulls;
- compactions, joins and retires;
- delivery steps.

It asserts:
1. **Convergence.** After full delivery and repeated push/pull until
   quiescent, every device's synced files and ledger shas are identical.
2. **No silent loss.** Every authored version is current, dominated by a
   version whose author saw it, or present in `displaced`.
3. **Single writer.** No device writes outside `dev/<self>/`, except
   `fleet.json` at `--new-fleet` and `retire`.
4. **Ordering is never a failure.** Missing data produces only `pending`.
5. **Termination.** Resolution does not ping-pong.
6. **Fork safety.** A clone or fork refuses rather than publishing.

A bounded run gates CI; a deep run sits behind an environment variable.

**Live soak.** One Mac plus one Linux machine on the release candidate for a
week before cutover, with real hooks.

## 13. Cutover (operator, after the 2.0 release candidate soaks)

1. **Final 1.x round.** On every Mac, still on iCloud and with P1's scope
   trim installed, `mm pull && mm push`, and resolve until `mm conflicts` is
   empty. A Mac that skips this round joins later only with wiped sources,
   or is retired (§4 Bootstrap).
2. **Install Syncthing.** Version ≥ 2.1 everywhere.
   - Linux: the distribution package or Syncthing's own apt repository
     (for example `pacman -S syncthing`), then
     `systemctl --user enable --now syncthing.service`.
   - macOS: `brew install syncthing && brew services start syncthing`.
3. **Create the fleet (Mac A).** Upgrade to 2.0 and run
   `mm init --new-fleet`. The passphrase can stay the same.
4. **Join everything else.** Each other machine runs `mm join <code>` from
   `mm invite`. Each publishes a snapshot; identical files collapse; real
   differences resolve once, with losers in `displaced`. Linux machines join
   the same way.
5. **Keep iCloud as a backup.** Leave the iCloud folder untouched for two
   weeks, then delete it.

There is no copy of the old store, no `brctl` materialization and no
split-brain window: 2.0 never reads 1.x storage.

## What 2.0 deletes

- **The 1.x store:** `storage/local.py` conflict-copy detection, the `data/`
  blob store, the `devices/` registry and `devices-write.lock`, crypto-init
  fetch, repair plans and winner selection, `_do_gc` orphan reaping, the
  last-push sidecar and corrupt-manifest recovery chain,
  `mm recover --abandon-manifest`, and `brctl` auto-pin.
- **Mtime-ordered apply:** the skip gate (`cli.py:2595-2621`), Track 12A's
  deferred inline bumps, `_bump_canonical_mtime_post_resolve`, and
  `pullplan`'s mtime predictions.
- **Sidecar conflicts:** `.sync-conflict-*` minting, the v0/v1 eras and
  migration, owner parsing, `_gc_old_conflict_files`, and promote.
  - 2.0 has no `ask` outcome: unresolved concurrency takes the displaced
    newest-wins path until 2.1.
  - The walker keeps a global exclusion for legacy `*.sync-conflict-*` names.
- **`pull-history.jsonl`**, imported into `history`.

**Kept:** source walkers and scoping, nested-checkout freezing, exclusions,
rel-path and symlink guards, case-collision handling, mtime restore on write,
mm-events, host usage, skill links, auto-upgrade, the lockfile, `safety`
sanitization and preview write boundaries.

## Tracks

Every `verify:` uses `./bin/check <scope>`. `/roadmap` applies the effective
SIZE limits when it turns these into cards.

| Track | Delivers | Main files | Release | Size |
|---|---|---|---|---|
| **P0** Stop today's two hazards | `_do_gc` reaps only `data/<me>/`; the root-salt drift remedy stops saying "Re-run `mm init`" | `cli.py`, tests | 1.6.1 | S |
| **P1** Trim sync scope | Track 72B's resolved 16-glob list: `projects/*/venv/*`, `projects/*/.venv/*`, `projects/*/*.app/*`, `projects/*/.qa-evidence/*`, `*.zip`, `*.tar`, `*.tgz`, `*.tar.gz`, `*.tar.xz`, `*.tar.bz2`, `*.tar.zst`, `*.whl`, `*.so`, `*.dylib`, `*.pak`, `*.dmg`; `migrate-config` appends them. `node_modules` is already globally excluded; `*.tar*` overmatches notes; `tools/` and `fixture(s)/` contain real source and fixtures, so those proposed globs were dropped. [Measurement](../invariants/sync.md#bulk-generator-output) | `config.py`, `cli.py`, README, tests | 1.8.0 | S |
| **T1** Replication simulator | Fake Syncthing transport, device harness, state machine with invariants 1–6 (against stubs until T3–T5 land) | `tests/sim/` | 2.0 | M |
| **T2** Local state DB | `statedb.py`: schema, migrations, history import, `mm log` on SQLite | `statedb.py`, `pullhistory.py`, `cli.py:log` | 2.0 | M |
| **T3** Store format | `fleet.json`, envelope 0x03 with associated data, commit/snapshot/presence codecs, parts, strict layout parser | new `store/` package, `crypto.py` | 2.0 | M |
| **T4** Publish | Ledger authoring, commits, own-storage reconciliation (§9), snapshots and compaction | `store/`, `cli.py:_push_core`, `manifest.py` (scan reuse) | 2.0 | L |
| **T5** Apply | Cursors, gap rule, snapshot bootstrap, causal classification, fast-forward writes and deletes, 2.0 resolvers and `displaced`, portable Claude paths, existing guards, previews (`mm diff`, `--dry-run`) | `store/`, `cli.py` pull path, `pullplan.py` | 2.0 | XL; split apply / preview |
| **T6** Fleet lifecycle | `init --new-fleet`, `invite`, `join`, `rejoin`, `devices`/`retire`, presence, delivery receipts, `devices --format json` projection | `cli.py`, `store/`, `config.py` | 2.0 | L |
| **T7** Syncthing integration | `syncthing.py`: discovery, folder setup, mesh healing, scan-after-push, status and diag | new `syncthing.py`, `cli.py` | 2.0 | M |
| **T8** Linux platform | §11 | `crypto.py`, `config.py`, `ci.yml`, tests, docs | 2.0 (keyring and CI may ship in 1.6.x) | M |
| **T9** Remove 1.x store and rewrite docs | "What 2.0 deletes"; rewrite `sync.md`, `init-devices.md`, `conflicts.md`, SPEC, AGENTS.md; update `docs/invariants/README.md` routing | many | 2.0 | L |
| **T10** Soak and cutover | §12 live soak and §13 runbook (operator) | none | after 2.0 RC | S |

**P1 demand** (this Mac, 2026-10-11): gstack is 32,615 of 33,518 synced
files and 1.22 of 1.26 GB. Venvs hold 62 MB, app bundles 59 MB, `tools/`
43 MB, archives and binaries 106 MB, QA evidence 19 MB. Beyond those, 178
files over 1 MiB hold 690 MB, 455 MB of it JSON receipts under
`review-and-prep/`, mostly `compact-snapshots/` and `ranking-summaries/`;
see decision 6. Lowering `max_file_size` is not a lever: an oversize
selected file refuses the whole push (README, "Snapshot failures").
Exclusion globs author no tombstones, so peers keep their copies (sync
invariants, tombstone suppression).

**Order:**
- P0 and P1 now, P1 before the first 2.0 snapshot.
- T1 and T2 in parallel.
- Then T3 → T4 → T5 → T6 → T7. T8 runs anytime. T9 comes last before
  release. Then T10.
- Conflict policies C2–C3 follow as 2.1–2.2.

T4 and T5 share `cli.py` and the `store/` package, so serialize their
landings.

## Decisions (recommended defaults marked)

1. **Deletions propagate** as fast-forward deletes, with the old bytes kept
   in `displaced` for 30 days. Today a deleted file lives on forever on
   other machines. *Recommended: yes.*
2. **mm manages Syncthing** (folder setup, invite/join, mesh healing) by
   default, with `[syncthing] manage = false` to opt out.
   *Recommended: yes.*
3. **Fresh-start cutover**, with no import of 1.x storage.
   *Recommended: yes.*
4. **Hashed machine fingerprint in commit headers** for clone detection.
   *Recommended: yes.*
5. **Relay** as a plain `receiveonly` Syncthing peer, not an untrusted
   device. *Recommended: yes*, when an always-on machine is available.
6. **gstack review receipts: kept in scope (2026-10-13).** No skill reads or
   writes `compact-snapshots/` or `ranking-summaries/`, but an active project's
   notes cite the receipts as durable evidence. These names describe one
   review run's directories rather than a generator class; Track 72B adds no
   glob for them. Retire them by deleting that run's directory when its work
   closes. The [bulk-output measurement](../invariants/sync.md#bulk-generator-output)
   records the accepted trim separately from retained receipts.

## Considered and rejected

- **Shared SQLite in the synced folder.** A multi-writer mutable file is the
  worst case for Syncthing.
- **A per-device SQLite file.** WAL data can be missing from the main file;
  Syncthing can scan mid-transaction; encrypted pages defeat block deltas.
- **Hardening the 1.x store.** That means pending blobs, a GC grace period,
  ENOENT retries, conflict-copy winner rules and clone generations: each
  narrows a hazard that the log design removes.
- **Syncthing syncing source trees directly.** No union merge, no path
  translation, no scoping, and its own `.sync-conflict` files.
- **A git repository as the store.** Refs arriving before objects is the
  same ordering problem, and encrypted git remotes amount to this log design.
- **A CRDT library (Automerge).** Agents edit files directly, so mm would
  diff into the CRDT anyway. That is equivalent to a three-way merge with a
  heavy dependency.
- **One Syncthing folder per device**, with a `sendonly` owner. Syncthing
  would enforce single writer, but at the cost of N folders. It is optional
  hardening once mm automates pairing; correctness does not need it.

## AGENTS.md changes at 2.0

- **Key Principles.**
  - Storage is a Syncthing folder of per-device, write-once encrypted
    commits (replacing "Single storage backend: iCloud…").
  - Changes propagate by version vector (replacing "Truth-based manifests"
    and the conflict-sidecar principle).
  - Owners alone compact their logs.
- **Cross-cutting rules.**
  - Build storage names only through the `store/` layout helpers (replacing
    `storage/keys.py`).
  - Local sync state goes through `statedb`.
  - Every automatic resolver is symmetric.
- **Commands.** Add `invite`, `join`, `rejoin`, `relay`, `displaced` and
  `devices retire`; drop `recover --abandon-manifest`.
