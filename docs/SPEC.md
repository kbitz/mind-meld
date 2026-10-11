# Mind Meld — Project spec

## Authority
This is the current authority for product scope, target and release policy.
Supporting documents own only the roles listed below. Superseded documents are
historical rationale; do not load them for routine planning or recover TODOs
from them. Consult a named historical source only to answer a specific question.

Adopted 2026-10-11 by `/project-spec`, replacing the product sections of the
root `SPEC.md` (now the 1.x architecture reference) and the 2026-03-18 review
record archived as `docs/archive/mind-meld-v1.md`.

## Purpose
Mind Meld (`mm`) keeps one person's AI coding-agent context identical across
the machines they work on, with no server and nobody else able to read it:
Claude Code memory and project notes, Codex and Grok customization, gstack
project state, and mm's own activity log. The complete workflow: finish on one
machine and `mm push`; start on another and `mm pull`, and the memory, skills
and gstack state are there; `retro-fleet` then reports agent usage across every
machine as one fleet.

2.0 moves the transport from iCloud Drive to a Syncthing folder so Linux
machines join the fleet and the store stops fighting an eventually consistent,
per-file transport. The design is [the 2.0 store](designs/store-v2.md).

## Audience and target
- Current users: the owner only. Observed 2026-10-11: three registered Macs
  (one inactive since 2026-08-11), no Linux machine yet.
- Audience ceiling: potentially-public. The repository is MIT and forkable and
  the README stays readable by a newcomer, but no alpha group or public stage
  is planned.
- Selected target: MVP-1, the owner's fleet of macOS and Arch-based Linux
  machines on the 2.0 store.
- Supported conditions: macOS and Arch-based Linux (Omarchy); Syncthing 2.1 or
  newer on every machine; Python 3.11+ and pipx; a desktop keyring (Keychain,
  or an unlocked Secret Service collection); up to a handful of machines with
  no always-on peer; sync sets of the order of 35k files and 1.3 GB raw before
  the scope trim; attended `mm push` and `mm pull` plus the documented Claude
  Code and Codex hooks. The owner installs Syncthing, runs the cutover runbook,
  resolves conflicts with `mm resolve`, retires dead devices and unlocks the
  Linux keyring by hand, following mm's remedies.
- Current achieved stage and evidence: see PROGRESS.md `## Acceptance`.

## Stage outcomes
| Stage | Intended users and usable outcome | Planned? |
|---|---|---|
| MVP-1 | The owner syncs agent context across their own macOS and Linux machines over Syncthing, end-to-end encrypted, with honest status | yes (selected) |
| MVP-2 | A small supported alpha group runs their own fleets | no |
| public-beta | Public users install from PyPI or GitHub with support | no |
| public-release | Public users receive stable compatibility commitments | no |

MVP-1 on the 1.x iCloud store, macOS only, was achieved with v1.0.0 on
2026-09-22; the 2.0 outcomes below redefine MVP-1 for the new transport.

## Selected target acceptance
### O1: Sync converges across macOS and Linux over Syncthing
The owner edits on any machine; after push, delivery and pull every machine
holds identical synced files.
1. Peer edits to files this machine never touched apply as fast-forwards;
   concurrent edits resolve symmetrically and terminate; deletions propagate
   and replaced bytes stay in `displaced` for 30 days. Proven by the
   replication simulator's invariants in CI.
2. Live soak: one Mac and one Linux machine run the 2.0 release candidate for
   one week with real hooks. The observed convergence, resolution events and
   any manual step are recorded in PROGRESS.md `## Acceptance`.

### O2: A machine joins the fleet in two commands
1. `mm init --new-fleet` on the first machine; `mm invite` there and
   `mm join <code>` on each other machine. The joiner bootstraps from a peer
   snapshot, never writes `fleet.json`, and mesh healing pairs it with every
   fleet device. Observed on the soak machines.
2. Syncthing's presence and folder settings are checked and reported by
   `mm status` and `mm diag`, never assumed.

### O3: Partial sync is a reported state, never an error
1. A gap in a peer's log reads as pending; `mm status` names which machines
   have received the latest push; a gap older than 24 h warns in every mode.
2. Arrival order never produces a failure (simulator invariant 4).

### O4: Nothing readable reaches the folder
1. Every commit, snapshot and presence record is sealed client-side with
   AES-256-GCM under envelope 0x03 with bound associated data. Only
   `fleet.json` (salt, KDF parameters, keycheck) is plaintext. Proven by tests
   and by inspecting the folder on the soak machines.

### O5: Restore, clone and fork cannot corrupt the fleet
1. A restored machine catches up from its own log; a cloned device id refuses
   and `mm rejoin` recovers; a fork refuses loudly (simulator invariant 6).

### O6: Sync scope is allowlisted and excludes generator output
1. Session transcripts, credentials and generator output (virtualenvs, app
   bundles, archives, QA evidence) never sync. Default `exclude_patterns`
   cover the trees measured on 2026-10-11, and `migrate-config` appends them
   without authoring tombstones. Ships on 1.x before the first 2.0 snapshot.

### O7: Fleet usage reporting survives the transport change
1. `retro-fleet` reports Claude, Codex, Grok and Cursor usage across the fleet
   as it does today, and `mm devices --format json` keeps its contract fields,
   projected from presence records.

Carried forward unchanged from 1.x: client-side encryption; no API server or
database service; pipx install with self-update; MIT; a newcomer-readable
README; host-interchangeable retro usage (root `SPEC.md`, Host
Interchangeability).

## Constraints and accepted limitations
- End-to-end encryption is mandatory; no plaintext sync data reaches storage.
- Sync is allowlisted; session databases, credentials and whole-file settings
  that may hold credentials stay local.
- A device writes only its own directory; owners alone compact their logs;
  explicit `mm devices retire` is the only cross-device delete.
- Explicit push and pull; mm watches no files. Syncthing transports, mm decides.
- Every automatic conflict resolution is a symmetric function of the versions.
- Accepted manual steps: Syncthing install and pairing confirmation, the
  cutover runbook, `mm resolve`, retiring dead devices, unlocking the Linux
  keyring before hooks run.
- Known limits: paths outside the home folder stay machine-specific; a
  snapshot is a device's whole state (about 330 MB sealed today) and is taken
  only from an attended push; tombstones are never dropped; 2.0 never reads
  1.x storage.
- 1.x is frozen at cutover: the GC repair and the scope trim are the last 1.x
  releases; after 2.0 ships, 1.x gets no fixes and the iCloud folder is
  deleted two weeks after cutover.

## Non-goals and later work
- Windows and non-Arch Linux distributions. Revisit when such a machine needs
  to join the fleet.
- Multi-user collaboration, a GUI, real-time file watching, syncing session
  transcripts, walking a host home root. Unchanged exclusions.
- Importing 1.x storage into 2.0. Cutover is a fresh start.
- Syncthing untrusted or encrypted peers. Revisit when syncthing#10528 and
  syncthing#10424 close.
- Conflict policies (2.1) and the resolver (2.2), in [conflict
  policies](designs/conflict-policies.md). Later outcomes; they do not gate 2.0.
  Revisit when 2.0.0 is accepted.
- Memory transport (Track 68A: NO QUALIFIED ROUTE) and real self-update
  qualification (S13): existing obligations retained in
  `docs/roadmap-future.md` with their own triggers; neither gates 2.0.
- Public beta. Revisit when a second person asks to run mm.

## Release policy
- Strict SemVer has applied since 1.0.0, chosen before any public stage, on
  the [Compatibility (1.x) contract](invariants/auto-upgrade.md#compatibility-1x).
  The 2.0 rewrite of the invariants defines the 2.x contract.
- Capability checkpoints: the 1.x GC repair and remedy corrections are a
  patch; the scope trim (O6) is a minor; **2.0.0** is O1 to O7 accepted after
  the soak, a MAJOR because storage and the device registry change and
  cutover is a fresh start; 2.1 is conflict policies; 2.2 is the resolver.
- Stage versions: MVP-1 (iCloud, macOS) = 1.0.0, achieved; MVP-1 (Syncthing,
  macOS + Linux) = 2.0.0.
- Compatibility surface: the 1.x contract until cutover. Both sides of the
  format change refuse safely through the crypto-init version byte.
- Version format: three components in `pyproject.toml`; there is no `VERSION`
  file, so the release baseline is the last commit that changed
  `pyproject.toml` on the base branch.
- Breaking changes: MAJOR, disclosed in the README Upgrading notes, with the
  cutover runbook as the recovery path.
- Evidence and records: PROGRESS.md `## Acceptance` for stages and
  checkpoints; release rows per the
  [release discipline](invariants/auto-upgrade.md#release-discipline-enforced-by-mm-auto-upgrade).

## Deferral policy
- Destination: `docs/roadmap-future.md`.
- Promotion: a bullet's trigger is observed, then it is reassessed against the
  selected target.
- Preserve: source and evidence, why it waits, and the revisit trigger.

## Supporting documents
- Current:
  - [2.0 store design](designs/store-v2.md): the store, hazards, lifecycle,
    verification and the Track list it proposes.
  - [Conflict policies](designs/conflict-policies.md): 2.1 and 2.2 design.
  - Root [`SPEC.md`](../SPEC.md): the 1.x architecture, data model and CLI
    contract that invariants, tests and code cite; the 2.0 docs Track replaces
    its storage sections.
  - [Invariants](invariants/README.md): module map and mandatory per-topic
    rules.
  - [Host parity](designs/host-parity.md): host interchangeability contract.
  - [Grok usage reader](designs/grok-build-usage-reader.md),
    [multi-source sync](designs/sync-gstack-context.md): shipped designs still
    cited by invariants.
  - [Memory continuity](designs/memory-continuity.md) and the
    [Codex memory contract](designs/codex-memory-contract.md): Track 68A
    evidence behind the deferred memory work.
  - [Self-update qualification](designs/self-update-qualification.md): the
    pending S13 run recipes.
- Superseded history:
  - [docs/archive/mind-meld-v1.md](archive/mind-meld-v1.md): the 2026-03-18
    review of the original spec (rebrand, scope additions). Replaced by this
    spec on 2026-10-11; read it only for why early decisions were made.
  - The existing `docs/archive/` designs (crypto v2, fleet retro, source
    toggle, Track 7A): shipped-design rationale.
