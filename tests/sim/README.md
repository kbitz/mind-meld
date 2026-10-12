# Replication fixtures

## Prerequisites

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
| Restore/index mismatch | [FAQ: folder marker](https://docs.syncthing.net/users/faq.html#i-am-seeing-the-error-message-folder-marker-missing-what-do-i-do) | Explicit saved/current index policies and retained queue; not native backup/recovery conformance. |

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
six store invariants. Tracks 75A/77A adapt push/pull; 76A owns actual hardware,
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
