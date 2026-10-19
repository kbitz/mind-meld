# Mind Meld — Project spec

## Authority
This is the current authority for product scope, target and release policy.
Supporting documents own only the roles listed below. Superseded documents are
historical rationale; do not load them for routine planning or recover TODOs
from them. Consult a named historical source only to answer a specific question.

Adopted 2026-10-11 by `/project-spec`; amended 2026-10-18. Rewritten 2026-10-19
by `/project-spec` to refocus the product: context sync and fleet usage
reporting are the two products, 2.0 carries the existing store onto Syncthing
instead of building the store-v2 log design, standalone Grok Build support is
removed, the 1.x compatibility contract is retired, and command output follows
one rule (O8). Earlier text lives in `git log -- docs/SPEC.md`.

## Purpose
Mind Meld (`mm`) does two things for one person who codes with AI agents on
several machines:

1. **Sync.** It keeps the agents' context identical on every machine, with no
   server and nobody else able to read it: Claude Code memory, Codex skills,
   Muse memory and gstack project state. Finish on one machine and `mm push`;
   start on another and `mm pull`, and it is all there.
2. **Usage reporting.** `retro-fleet` reports what the agents did across every
   machine as one fleet: tokens, cost, active days and commits from Claude
   Code, Codex, Cursor and Muse.

A command either succeeds, in one line, or says what broke and how to fix it.

2.0 moves the transport from iCloud Drive to a Syncthing folder so the
owner's Linux machine joins the fleet. It keeps the 1.x store (each machine's
encrypted file list plus encrypted content) and fixes what breaks on
Syncthing.

## What mm syncs and reports

| Agent | Synced between machines | Usage in `retro-fleet` |
|---|---|---|
| Claude Code | Project memory and `todos/` under `~/.claude` (CLAUDE.md, agents and commands stay in git) | Tokens, cost, sessions and skills from local session logs |
| Codex | `skills/`, `plugins/`, `AGENTS.md` | Tokens and cost from session rollouts, archived ones included |
| Cursor | Nothing: no user-authored tree exists, and Cursor reads `~/.claude/skills` | Runs from Conductor and the enrolled `cursor-agent` CLI, counted under each model's family (Grok, Claude, GPT) |
| Muse | `memory/`, after `mm enable-source muse` | Tokens and cost, as its own `muse` family |
| gstack, gstack-extend | Project state, analytics and retros; generator output excluded | — |
| mm | Its activity log, which `retro-fleet` reads | Git commits per machine |

Intentional gaps: Cursor runs through an ACP front-end such as Paseo report
no usage (no source exists); Codex memory does not sync (no qualified route,
Track 68A); session transcripts and credentials never sync. Standalone Grok
Build is no longer supported: its sync source and usage reader are removed.
Grok models used through Cursor still appear in the Grok row.

## Audience and target
- Current users: the owner only. Observed 2026-10-19: three registered Macs
  (one inactive since 2026-08-11) and one Arch-based (Omarchy) Linux machine
  that waits for 2.0 to join.
- Audience ceiling: potentially-public. The repository is MIT and forkable and
  the README stays readable by a newcomer, but no alpha group or public stage
  is planned.
- Selected target: MVP-1, the owner's fleet of macOS and Arch-based Linux
  machines syncing over Syncthing, with complete usage reporting and quiet,
  honest output.
- Supported conditions: macOS and Arch-based Linux (Omarchy); Syncthing 2.1 or
  newer on every machine; Python 3.11+ and pipx; a desktop keyring (Keychain,
  or an unlocked Secret Service collection); a handful of machines with no
  always-on peer; sync sets of about 40k files and 1.3 GB; attended `mm push`
  and `mm pull` plus the documented Claude Code and Codex hooks. Until
  cutover, the Macs sync over iCloud Drive on 1.x.
- Current achieved stage and evidence: see PROGRESS.md `## Acceptance`.

## Stage outcomes
| Stage | Intended users and usable outcome | Planned? |
|---|---|---|
| MVP-1 | The owner syncs agent context across their own macOS and Linux machines over Syncthing, end-to-end encrypted, and sees the whole fleet's agent usage | yes (selected) |
| MVP-2 | A small supported alpha group runs their own fleets | no |
| public-beta | Public users install from PyPI or GitHub with support | no |
| public-release | Public users receive stable compatibility commitments | no |

MVP-1 on the 1.x iCloud store, macOS only, was achieved with v1.0.0 on
2026-09-22; the outcomes below redefine MVP-1 for Syncthing and Linux.

## Selected target acceptance
### O1: Sync converges across macOS and Linux over Syncthing
The owner edits on any machine; after push, delivery and pull, every machine
holds the same synced files.
1. A peer's change to a file this machine has not changed since its last sync
   applies directly, with no conflict. Deletions propagate, and a file removed
   because a peer deleted it stays recoverable for 30 days. When two machines
   changed the same file, mm keeps both versions until `mm resolve`;
   `MEMORY.md` and `.jsonl` files merge automatically.
2. Claude project memory for a project under the home folder lands in the
   same project on macOS and on Linux.
3. A file another program is writing during a push keeps its previously
   published version and goes out with a later push; it never stops the rest
   of the push.
4. Evidence: integration tests over a local folder that delivers files late
   and out of order, and a one-week live soak with one Mac and the Linux
   machine on the 2.0 release candidate with real hooks, recorded in
   PROGRESS.md `## Acceptance`.

### O2: A machine joins the fleet with one mm command after Syncthing pairing
1. On a machine paired in Syncthing with the shared folder accepted, one mm
   command joins the fleet: it waits for the fleet's key header to arrive,
   never creates one, verifies the passphrase and pulls. Creating a new fleet
   refuses on a folder that already holds one. Observed on the soak machines.
2. `mm status` and `mm diag` read and report Syncthing's state (connected
   peers, the folder's sync state and the folder options mm relies on). mm
   never changes Syncthing's configuration.

### O3: Partial sync is a reported state, never an error
1. Content that has not arrived yet, a file Syncthing is replacing, and
   Syncthing's own temporary and conflict files read as pending or are
   ignored, never as failures. Pull applies what has arrived and says what it
   is still waiting for.
2. `mm status` shows each machine's last push and whether this machine has
   applied it. Anything pending for more than 24 h warns in every mode.

### O4: Nothing readable reaches the folder
1. Every file list and all file content are encrypted client-side with
   AES-256-GCM before they reach the folder. Only the fleet's key-derivation
   header (salt, KDF parameters, key check) and the device registry (machine
   names, mm versions, last-seen times) are plaintext. Proven by tests and by
   inspecting the folder on the soak machines.

O5 (restore, clone and fork safety) was removed on 2026-10-19 with the
store-v2 design: re-joining a restored or cloned machine is an accepted
manual step.

### O6: Sync scope is allowlisted and excludes generator output
1. Session transcripts, credentials and generator output (virtualenvs, app
   bundles, archives, QA evidence) never sync; the default exclusions shipped
   in 1.8.0. A push that adds an unusually large number of files under one
   subtree names that subtree and how to exclude it.

### O7: Fleet usage reporting is complete and accurate
1. `retro-fleet` reports tokens, cost, active days, machines and top model
   for each agent family (Claude, Codex, Grok and Muse), summed across the
   fleet from Claude Code, Codex, Cursor and Muse. Cursor's runs count under
   their model's family.
2. Every attended push captures this machine's usage and git activity within
   one total time budget. Work left unfinished resumes on later pushes, so
   capture is complete again within three attended pushes without a recovery
   command. Capture that stays incomplete longer is a failure that `mm status`
   reports with its fix.
3. Usage mm cannot read is named as a coverage gap in the retro and in
   `mm status`, never shown as a silent zero.
4. Evidence: a `retro-fleet` run on the soak fleet showing complete capture
   from every active machine, recorded in PROGRESS.md `## Acceptance`.

### O8: Every command reports one outcome
1. `mm push` and `mm pull` end with one line on success that says what moved.
   On failure the last thing printed says what broke and the one command that
   fixes it, and interactive commands exit non-zero. Nothing else is printed
   unless the owner must act: data at risk, or an actionable warning such as
   O6's growth warning. Progress and diagnostics appear with `--verbose`, in
   `mm status` and in `mm diag`.
2. Usage and activity capture never changes a push's outcome and prints
   nothing during a push. Its total time per attended push stays within one
   configured budget (default 5 s).
3. `mm autopush` and `mm autopull` stay silent unless the owner must act;
   their outcomes appear in `mm status`.
4. `mm status` opens with a one-line verdict (in sync, changes waiting, or
   the action needed) and shows only the latest run of each kind.
5. Evidence: tests pin the success, failure and degraded-capture output, and
   the owner's own pushes during the soak.

## Constraints and accepted limitations
- End-to-end encryption is mandatory; no plaintext sync data reaches storage.
- Sync is allowlisted; session transcripts, credentials and whole-file
  settings that may hold credentials stay local.
- Explicit push and pull; mm watches no files. Syncthing moves the files and
  the owner manages Syncthing; mm reads and reports its state.
- No storage file is rewritten by more than one machine.
- Usage capture reads host data without changing it and publishes only usage
  metadata (tokens, models, times, commit counts), never prompts,
  transcripts or filesystem paths.
- One user, no compatibility contract. mm refuses storage written in a format
  it cannot read, with an upgrade remedy; that is the only compatibility
  promise. Commands, flags, config keys, output and host families can change
  in any release, with a README Upgrading note. Every machine stays current
  through auto-update.
- Accepted manual steps: installing Syncthing, pairing machines and sharing
  the folder with the README's options; running the cutover runbook once;
  `mm resolve` for real conflicts; unlocking the Linux keyring before hooks
  run; re-joining a machine restored from backup or cloned; removing a dead
  machine from the fleet.
- Known limits: paths outside the home folder stay machine-specific. mm does
  not detect a restored or cloned machine, which can republish stale files
  until it is re-joined. Content blob names are content hashes: they show
  whether two machines hold identical bytes, not the bytes. iCloud Drive is
  not supported after cutover.

## Non-goals and later work
- Windows and non-Arch Linux distributions. Revisit when such a machine needs
  to join the fleet.
- Multi-user collaboration, a GUI, real-time file watching, syncing session
  transcripts, walking a host home root. Unchanged exclusions.
- mm-managed Syncthing: invite codes, mesh healing, relays, folder setup
  through Syncthing's API and scan requests after push. Revisit when the fleet
  grows past a handful of machines, or manual pairing or folder settings cause
  an observed missed sync.
- Detecting restored, cloned or forked machines. Revisit when one damages
  fleet data in real use.
- Per-path conflict policies and a richer resolver. Revisit when conflicts
  become a recurring manual burden after 2.0.
- Standalone Grok Build support, removed 2026-10-19. Revisit when the owner
  uses Grok Build directly again.
- Codex and cross-agent memory sync. Revisit when Codex ships a portable
  memory store or an mm-owned route qualifies (Track 68A evidence).
- Usage for Cursor run through ACP front-ends such as Paseo. Revisit when
  `cursor-agent`'s ACP mode exposes usage.
- Qualification ceremonies for shipped features: the self-update
  qualification (S13), Track 70A's two-Mac acceptance and the separate O6
  acceptance record were dropped on 2026-10-19. A feature in daily use counts
  as working; misbehavior is filed as a bug.
- Public beta. Revisit when a second person asks to run mm.

## Release policy
- Owner-only, before any public stage: ordinary releases are PATCH;
  documentation-only changes take no bump.
- Capability checkpoints, each a MINOR when accepted: **quiet output** (O8)
  and **complete usage** (O7 criteria 1 to 3 on the owner's machines).
- Stage versions: MVP-1 on iCloud (macOS) = 1.0.0, achieved. MVP-1 on
  Syncthing (macOS and Linux) = **2.0.0**: O1 to O4 accepted after the soak,
  with O6 to O8 holding. MAJOR because the transport changes and every
  machine must upgrade before cutover.
- Strict SemVer: not used. The 1.x compatibility contract that applied it
  from 1.0.0 is retired as of 2026-10-19. Strict SemVer would start at
  public-beta, which is not planned.
- Compatibility surface: storage-format gates only. An mm refuses newer or
  unknown storage formats with an upgrade remedy. A change that needs every
  machine upgraded before it syncs is at least a MINOR and says so in the
  README Upgrading notes.
- Version format: three components in `pyproject.toml`; there is no `VERSION`
  file, so the release baseline is the last commit that changed
  `pyproject.toml` on the base branch.
- Breaking changes: allowed in any release, disclosed in the README Upgrading
  notes and CHANGELOG.
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
  - Root [`SPEC.md`](../SPEC.md): the architecture, data model and CLI
    reference for the store that 2.0 carries onto Syncthing.
  - [Invariants](invariants/README.md): module map and mandatory per-topic
    rules. Their "Compatibility (1.x)" section is retired by this spec except
    its storage-format gates.
  - [Store-v2 design](designs/store-v2.md): superseded as the 2.0 design on
    2026-10-19. Current only as evidence: the "Why a new store" hazard table
    (the Syncthing problems the 1.x store must handle), §7 portable paths,
    §10 Syncthing folder options and §11 Linux.
  - [Host parity](designs/host-parity.md): usage-family rationale. This
    spec's table owns host scope.
  - [Grok usage reader](designs/grok-build-usage-reader.md): the Grok Build
    reader until it is removed.
  - [Multi-source sync](designs/sync-gstack-context.md): shipped design still
    cited by invariants.
  - [Memory continuity](designs/memory-continuity.md) and the
    [Codex memory contract](designs/codex-memory-contract.md): Track 68A
    evidence behind the deferred memory work.
- Superseded history:
  - [Conflict policies](archive/conflict-policies.md): the 2.1 policies and
    2.2 resolver designed on store-v2's version vectors; superseded
    2026-10-19 with that design.
  - [Self-update qualification](archive/self-update-qualification.md): S13
    run recipes; the obligation was dropped on 2026-10-19.
  - [docs/archive/mind-meld-v1.md](archive/mind-meld-v1.md): the 2026-03-18
    review of the original spec; read it only for why early decisions were
    made.
  - The other `docs/archive/` designs (crypto v2, fleet retro, source toggle,
    Track 7A): shipped-design rationale.
