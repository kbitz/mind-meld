# mm 2.0 store: per-device encrypted logs over Syncthing

> Written 2026-10-09/10 against `98e9a69` (v1.6.0). Not yet admitted to the
> roadmap; `/roadmap` turns the Tracks below into cards. This design replaces
> the 1.x storage format and moves mm from iCloud Drive to Syncthing.
> Conflict policies, three-way merge and the resolver UI build on it: see
> [conflict policies](conflict-policies.md).
>
> **Maintainer decisions:**
> - 2026-10-09: breaking changes are acceptable in this MAJOR. Prefer the
>   most correct design over compatibility with 1.x internals.
> - 2026-10-09: an always-on peer is optional.
> - 2026-10-10: end-to-end encryption stays.
> - 2026-10-10: local sync state moves to SQLite; the existing usage caches
>   stay as they are.

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
| **Plaintext content hashes in blob names** | `cli.py:2803`, `storage/keys.py` | Filenames are sequence numbers. |
| **Claude project folders named after the absolute path** (`-Users-<name>-*` on macOS vs `-home-<name>-*` on Linux) | `manifest.py:1405`, `cli.py:5105` | Wire paths are relative to the home folder (§7). |
| **Every peer edit to an untouched file is a conflict**, because there is no last-synced base | `cli.py:2006-2021`, `cli.py:5101-5122`, `merge.py:185` | Fast-forward by version vector (§4). |
| **Line deletions in `MEMORY.md` and `.jsonl` never stick.** Union merge runs on every update, even one-sided ones. | `cli.py:2592-2593`, `merge.py:46-53` | Union runs only on concurrent versions. |

## 1. Layout

```
~/Sync/mind-meld/                     Syncthing folder id: mind-meld-<fleet8>
  fleet.json                          plaintext header; write-once
  .stignore                           local, written by mm: (?d).mm-tmp-*  (?d).DS_Store
  dev/<device>/log/000000001043.enc   commit; large ones split: 000000001043.2of3.enc
  dev/<device>/snap/000000001000.enc  full-state snapshot (same part rule)
  dev/<device>/presence.enc           small mutable record
```

Only names matching this grammar are read. Anything else, including Syncthing
temp files and `*.sync-conflict-*` copies, is foreign: `mm diag` counts it.
A conflict copy of a commit, or of `fleet.json`, is a **fork signal** and
refuses loudly (§9).

## 2. Formats

**`fleet.json`** is plaintext, because it is needed before any key exists:
`{format: 3, fleet_id, created_at, kdf: {argon2id memory_kb, time, parallelism}, salt, keycheck}`.
`fleet_id` is 16 random bytes. The master key is
`Argon2id(passphrase, salt)`, as in 1.x.

**Envelope 0x03.** Payloads are gzip-compressed, then sealed with AES-256-GCM
under per-kind subkeys derived from the master key, with a random nonce. The
associated data binds `(fleet_id, device_id, kind, seq, part, nparts)`, so a
file renamed, moved between devices or replayed under another number fails
authentication.

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
`cursors: {peer: seq}`, the peers' commits it reflects. Entries are this
device's full state:
- every live path with its content;
- every tombstone;
- concurrent versions it holds unresolved (marked `held`).

Tombstones are path plus vv and are never dropped, so there is no
resurrection horizon.

**Presence payload.** `{device_id, device_name, os, mm_version, host, syncthing_id, registered, last_active, published_seq, snapshot_seq, applied: {peer: seq}}`.
It is rewritten atomically (temp file plus rename) at the end of push and
pull when something changed, and at most hourly otherwise.

`mm devices --format json` projects presence onto the existing contract
fields: `device_id`, `device_name`, `registered`, `last_seen` (=
`last_active`) and `last_seen_version` (= `mm_version`). retro-fleet's
subprocess reader stays unchanged.

## 3. Local state: `~/.config/mind-meld/state.db`

SQLite through the standard-library `sqlite3`: WAL, `busy_timeout`, mode
0600, and a `user_version` schema number with forward-only migrations. The
mm lockfile still serializes commands, and SQLite transactions make each
step atomic.

| Table | Holds | Readers |
|---|---|---|
| `meta` | device id, fleet id, host, schema version | every command |
| `ledger` | `(source, path) → sha, vv, mtime, deleted`: the version each local file corresponds to | push (authoring), pull (classification), preview |
| `cursors` | `peer → applied_seq, gap_seq, gap_since` | pull, status, presence |
| `own_log` | `seq → prev, parts, bytes, created_at` | push, compaction, restore detection |
| `displaced` | replaced local versions (bytes, reason, time), kept 30 days | `mm displaced`, gc |
| `history` | per-file push/pull outcomes; imported once from `pull-history.jsonl{,.1}` | `mm log`, `mm conflicts --suggest` |

[Conflict policies](conflict-policies.md) adds `base` (three-way merge bases)
and `conflicts` (held versions). Usage, identity, upgrade and token caches
stay in their existing JSON files.

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

Per-path rules, three-way merge and `ask` arrive with conflict policies (2.1).

**Symmetry invariant (load-bearing).** Every automatic resolution is a pure
function of the unordered pair of versions (plus the base, from 2.1). Two
devices that resolve the same pair independently produce identical bytes,
the same-sha rule collapses their vectors, and resolution terminates. A
resolver that prefers "local" would ping-pong forever.

**Bootstrap.** With no ledger (a fresh 2.0 device), every file is authored
as `{me: 1}` in the first snapshot. Identical files across devices collapse
through the same-sha rule; only real differences resolve, once.

## 5. Publish (push)

1. Lock, then reconcile own storage (§9).
2. Scan sources strictly and diff against the ledger. If nothing changed,
   refresh presence if due and stop.
3. Build commit `seq = last + 1` with `prev`. Content is the exact scanned
   bytes, verified unchanged before sealing (the existing upload-verification
   rule).
4. Write each part with exclusive create (`os.link`; temp files named
   `.mm-tmp-*`). The commit exists once its last part exists.
5. In one transaction: ledger updates, `own_log` row, history rows. A crash
   between steps 4 and 5 is recovered by §9's own-log replay.
6. Best effort: ask Syncthing to rescan `dev/<me>`. Write presence. Take a
   snapshot and compact if due (§6).

## 6. Snapshots and compaction

A device writes a snapshot when 200 commits, 7 days or 64 MiB of log have
accumulated since its last one. It deletes its own commits at or below its
**previous** snapshot's seq once they are 14 days old, and keeps its two
newest snapshots.

**Reader rule** for peer `P`, with `next = cursor[P] + 1`:

| Situation | Action |
|---|---|
| commit `next` is complete | apply it |
| `next` is missing and `next ≤` P's newest complete snapshot seq | load that snapshot: classify every entry; set `cursor[P]`; raise each other cursor to `max(cursor[Q], snapshot.cursors[Q])` |
| `next` is missing and beyond P's snapshot | pending; record `gap_since` |
| `next` is complete but unreadable (authentication or parse failure) | stop at it with an always-visible warning; skip to a later snapshot when one appears |

The owner's remedy for an unreadable commit is `mm push --snapshot`.

This rule needs no cross-device acknowledgements and never confuses
"compacted" with "in flight". Presence `applied` cursors exist only for
status. A pending gap becomes a visible warning in every mode after 24 h,
per the data-at-risk rule.

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
presence from storage.

| Observation | Meaning | Action |
|---|---|---|
| Own commits beyond `own_log`, same `host` | Crash after publication, restore from backup, or lost `state.db` | Apply own commits like a peer's, via `cursor[me]`: local files fast-forward to this device's own newer state; then continue |
| Own commits with a different `host` | Two machines share this device id (copied config or a migrated Mac) | Refuse; `mm rejoin` mints a new device id in the same fleet |
| `prev` chain broken, or a conflict copy of a commit or `fleet.json` | Fork | Refuse with `mm diag` detail; no automatic repair |

## 10. Syncthing integration

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
- restoring a device's disk to an earlier copy, and a cloned config;
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

1. **Final 1.x round.** On every Mac, still on iCloud, `mm pull && mm push`,
   and resolve until `mm conflicts` is empty. Run the
   [conflict census](conflict-policies.md#appendix-c0-census-script) first.
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

**Order:**
- P0 now.
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
