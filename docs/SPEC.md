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
one rule (O8). Amended 2026-10-23 to add the core-and-extensions engineering
constraint; outcomes, target and release policy are unchanged. Amended
2026-10-29 by `/project-spec` with three owner decisions: cross-model memory
is planned after 2.0 as O9 with its own MINOR checkpoint; mmm-factory's
durable state syncs through an opt-in source for single-Mac handover; and
gstack's machine-local files leave the gstack source. The selected target is
unchanged. Amended 2026-11-01 by `/project-spec` to correct three facts: the
owner has two Linux machines, not one; Claude project memory is shared by git
remote since 1.12.0; and auto-update does not reach every machine. Amended
2026-11-01 by `/project-spec` to drop the timed live trial before 2.0.0, so
problems are fixed as they appear, and to require a bootstrap script that
installs Python, pipx, mm and the Syncthing package before 2.0.0. Outcomes and
target are unchanged; the 2.0.0 release line's conditions follow. Amended
2026-11-05 by `/project-spec` to bring Grok Build back at parity with Claude
Code and Codex (owner, 2026-11-05: "go, it needs to bring grok to parity w/
the other models"): the revisit trigger of its 2026-10-19 removal is met,
because the owner now runs Grok Build directly, through Paseo, on the same
subscription as Cursor. Grok Build joins the sync and usage host lists in
Purpose, the table, O1.2 and O7; target, stages and release policy are
unchanged. Amended 2026-11-06 by `/project-spec` with two owner decisions
from Track 111A's live checks on Grok Build 1.0.77: only Grok memory topics
sync, and a repository with no `origin` or a folder that is not a repository
reaches other machines only as an inert copy. Target, outcomes and release
policy are unchanged. Amended again 2026-11-06 by `/project-spec` to defer
the opt-in `mmm` source (owner: "hm, don't love that -- let's future this
card and move on to the next one"): mm does not carry `~/.mmm/`, and the
factory moves with mmm-factory's own tool; target, outcomes and release
policy are unchanged. Amended 2026-11-07 by `/project-spec` with two owner
decisions. Cursor support is removed completely (owner: "cursor -- removed
totally, don't worry about old usage, as if we never supported"): Cursor
leaves Purpose, the table, O7 and the gaps and non-goals that existed only
for it, and Grok models run through Cursor no longer reach the Grok row. The
owner's machine hostnames must be removed from the repository's git history
before 2.0.0 and before the repository is public again (owner: "that scrub
is required before we go public again and hit 2.0 fyi"), which adds a
condition to the 2.0.0 release line. Audience, target and the other outcomes
are unchanged. Amended again 2026-11-07 by `/project-spec` to drop the
fleet-wide cutover (owner: "we don't need a migrate tool, because i'm the only
user ... migration for me can be; uninstall mm and reinstall, register the
machine w/ our fleet and boom"). Each machine moves on its own schedule by
reinstalling mm and joining the Syncthing fleet, which may be started fresh
on any machine; a machine not yet moved keeps running on iCloud Drive and holds
nothing up; the iCloud store's usage history need not carry over. A first
install sets Syncthing up itself when Syncthing still has its default
configuration, and otherwise points to a guide (owner: "it should set up
syncthing IF syncthing had not otherwise been configured"). O2 is rewritten
for setup, pairing and approval, and the constraints, known limits, non-goals
and 2.0.0 release line lose the cutover. Audience, target, the other outcomes
and the hostname-scrub condition are unchanged. Amended 2026-11-08 at the
owner's direct confirmation to drop the git-history hostname scrub as a
condition of 2.0.0 and of making the repository public again: GitHub keeps
pull-request refs and cached views of old commits that a force-push cannot
clean, so the rewrite could not remove them (owner: "I didn't realize there
was no way to scrub the PRs, so basically this is a pointless exercise, we
should just make sure it stays impersonal moving forward?"). The current tree
still names machines by role. Amended 2026-11-09 by `/project-spec` so an
attended `mm push` run in a terminal may finish its capture scans up to a scan
cap of tens of seconds (owner: "2 Yes", then "ok, that makes sense"): O7.2 and
O8.2 change and no other outcome, target or release policy does. Amended
2026-11-10 by `/project-spec` to rewrite O8.1 and add O8.6, on the owner's
words: "I do think that it should show the same update installer when it's
updating, like when we run mm update -- it shouldn't update silently if that
makes sense"; "when we push and pull, should we show the same spinner? should
we change update to look more like push/pull? just want things to be
consistent"; "I'm fine w/ multiple lines at the end if there's more than one
outcome -- it doesn't have to be one line"; "i think you probably overindexed
at one point -- eveyrthing used to be overly verbose -- there is a sensible
middle ground"; and "go -- make sure all our manually run commands have
consistently formatted output -- they all need to look like they come from
the same tool". Push and pull end with one line per thing that happened,
including mm updating itself, and every hand-run command shares one output
style; Purpose's one-line sentence follows. O8.2 to O8.5, the target,
audience and release policy are unchanged. Amended 2026-11-14 by
`/project-spec` on the owner's decision that the repository is public ("change
the docs"): Audience and target now says so, and the README install returns to
the anonymous curl one-liner. Outcomes, target, audience ceiling and release
policy are unchanged. Earlier text lives in `git log -- docs/SPEC.md`.

## Purpose
Mind Meld (`mm`) does two things for one person who codes with AI agents on
several machines:

1. **Sync.** It keeps the agents' context identical on every machine, with no
   server and nobody else able to read it: Claude Code memory, Codex skills,
   Grok Build memory and skills, Muse memory, Antigravity skills and rules,
   and gstack project state. Finish on one machine and `mm push`;
   start on another and `mm pull`, and it is all there.
2. **Usage reporting.** `retro-fleet` reports what the agents did across every
   machine as one fleet: tokens, cost, active days and commits from Claude
   Code, Codex, Grok Build, Muse and Antigravity.

A command either says briefly what happened, or says what broke and how to fix
it, and every command says it in the same style.

2.0 moves the transport from iCloud Drive to a Syncthing folder so the
owner's two Linux machines join the fleet. It keeps the 1.x store (each
machine's encrypted file list plus encrypted content) and fixes what breaks on
Syncthing. There is no fleet-wide cutover and no migration tool: each machine
moves when the owner reinstalls mm on it and joins the Syncthing fleet, and a
first install sets Syncthing up so it just works.

## What mm syncs and reports

| Agent | Synced between machines | Usage in `retro-fleet` |
|---|---|---|
| Claude Code | Project memory and `todos/` under `~/.claude` (CLAUDE.md, agents and commands stay in git) | Tokens, cost, sessions and skills from local session logs |
| Codex | `skills/`, `plugins/`, `AGENTS.md` | Tokens and cost from session rollouts, archived ones included |
| Grok Build | `skills/`, `commands/` and `rules/` under `~/.grok`, and its memory topics (each scope's `topics/` Markdown, global and per repository), after `mm enable-source grok`; pending observations stay on the machine that captured them | Tokens and cost from local session records, runs through Paseo (ACP) included, in the Grok row |
| Muse | `memory/`, after `mm enable-source muse` | Tokens and cost, as its own `muse` family |
| Antigravity | `skills/`, `rules/`, `AGENTS.md`, `GEMINI.md` under `~/.gemini/config` | Tokens and cost from conversation steps, as its own `agy` family |
| gstack, gstack-extend | Project state, analytics and retros; generator output and machine-local files (factory manager files, evidence ledgers, browser profiles, pair-review resume copies) excluded | — |
| mmm-factory | Nothing: `~/.mmm/` stays local, and the factory moves between hosts with mmm-factory's own tool ([factory handover](#factory-handover)) | — (factory workers' tokens count under their host's row) |
| mm | Its activity log, which `retro-fleet` reads | Git commits per machine |

Intentional gaps: Codex memory does not sync until cross-model memory (O9,
after 2.0); session transcripts and credentials never sync. Grok
Build's `config.toml`, credentials, hooks and plugins stay local, as Claude
Code's and Codex's whole-file settings do; its generated memory index
(`MEMORY.md`) and memory databases are rebuilt on each machine and never sync.
Grok Build reads `~/.claude/skills`, so mm keeps no Grok skill link. The
Grok row counts Grok Build's own runs only.

## Audience and target
- Current users: the owner only. Observed 2026-10-19: three registered Macs
  (one inactive since 2026-08-11). The owner stated on 2026-10-31 that they
  have two Arch-based (Omarchy) Linux machines, which wait for 2.0 to join.
- Audience ceiling: potentially-public. The code is MIT and the README stays
  readable by a newcomer, but no alpha group or public stage is planned. The
  repository is public and stays public (owner's decision, 2026-11-14); it
  installs with no GitHub login.
- Selected target: MVP-1, the owner's fleet of macOS and Arch-based Linux
  machines syncing over Syncthing, with complete usage reporting and quiet,
  honest output.
- Supported conditions: macOS and Arch-based Linux (Omarchy); Syncthing 2.1 or
  newer on every machine; Python 3.11+ and pipx, installed by the bootstrap
  script if missing; a desktop keyring (Keychain, or an unlocked Secret
  Service collection); a handful of machines with no always-on peer; sync sets
  of about 40k files and 1.3 GB; attended `mm push` and `mm pull` plus the
  documented Claude Code and Codex hooks. A Mac not yet moved to Syncthing
  keeps syncing over iCloud Drive until the owner moves it; it holds no other
  machine up.
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
Cross-model memory (O9) is planned after 2.0 for the same owner fleet. It is
a capability, not an audience stage, and does not gate 2.0.

## Selected target acceptance
### O1: Sync converges across macOS and Linux over Syncthing
The owner edits on any machine; after push, delivery and pull, every machine
holds the same synced files.
1. A peer's change to a file this machine has not changed since its last sync
   applies directly, with no conflict. Deletions propagate, and a file removed
   because a peer deleted it stays recoverable for 30 days. When two machines
   changed the same file, mm keeps both versions until `mm resolve`;
   `MEMORY.md` and `.jsonl` files merge automatically.
2. Claude project memory for a project with a network git `origin` lands in
   the same project on macOS and on Linux, wherever each machine keeps its
   checkout. A project without one lands by its home-relative path when it
   sits under the home folder. Grok Build's per-repository memory follows
   Grok's own scope names, which Grok derives from the git remote, so a
   repository with a network `origin` lands in the same memory on every
   machine; a repository without one, or a folder that is not a
   repository, arrives elsewhere only as an inert copy (an accepted
   limitation; see Known limits).
3. A file another program is writing during a push keeps its previously
   published version and goes out with a later push; it never stops the rest
   of the push.
4. Evidence: integration tests over a local folder that delivers files late
   and out of order, and the owner's own observation on one Mac and both
   Linux machines with real hooks once each has moved onto the Syncthing
   fleet (O2.5), recorded in PROGRESS.md `## Acceptance`. Problems are fixed
   as they appear.

### O2: A machine joins the Syncthing fleet from a fresh install
Owner decisions of 2026-11-07: no migration tool; mm sets Syncthing up only
when Syncthing still has its default configuration; joining takes one pasted
device ID on each side; every fleet machine links to every other.
1. **Default configuration.** Syncthing has its default configuration when it
   has no paired device and no folder except its stock Default Folder,
   unshared and empty. Changes to the GUI or other options do not count. Any
   paired device or any other folder is a pre-existing configuration. mm
   manages Syncthing on a machine only if it found the default configuration
   there when it set Syncthing up; the owner manages Syncthing on every other
   machine.
2. **Setup.** On a machine whose Syncthing has its default configuration, the
   install starts Syncthing at login and mm configures it. `mm init
   --new-fleet`, on macOS or Linux, creates the shared folder with mm's folder
   options. `mm init` on a joining machine asks for one fleet machine's
   Syncthing device ID, pairs with it, accepts the fleet folder with mm's
   options and prints this machine's own device ID. mm may remove the
   unshared, empty stock Default Folder so mm's folder is not nested inside
   it.
3. **Approval.** On a fleet machine where mm manages Syncthing, the owner
   runs one mm command with the new machine's pasted device ID (for example
   `mm devices approve <device-id>`). mm accepts only an exact match among
   Syncthing's pending devices, then adds the device and shares the fleet
   folder; it never accepts a device on its own. Besides that exact match, it
   only finishes an approval mm started on an earlier run: a device that
   exactly matches the pasted ID, has the shape mm's add step writes and does
   not yet share the fleet folder. The two paired machines mark
   each other as Syncthing introducers, so every machine where mm manages
   Syncthing connects to every other with no further step. On a fleet
   machine where the owner manages Syncthing, the owner approves the new
   machine in Syncthing as the README guide describes.
4. **Pre-existing configuration.** On a machine whose Syncthing has a
   pre-existing configuration, mm changes nothing in Syncthing and points the
   owner to the README guide for configuring Syncthing to work with mm
   (pairing, sharing the folder and its options).
5. **Join and move.** Once the folder is shared, `mm init` joins: it never
   creates a key header, verifies the passphrase and pulls. If the fleet's
   key header has not arrived, it stops without writing anything and says to
   wait for Syncthing; the owner reruns `mm init` once it has. Creating a
   new fleet refuses on a folder that already holds one. The owner moves a
   machine from iCloud Drive by uninstalling mm, reinstalling it with the
   bootstrap script and joining (or starting the fleet with `mm init
   --new-fleet`). The machine's agent files stay in its home and merge
   through the pull rules of O1.1: no local agent file is lost, and a file
   that differs from the fleet's copy without a proven last-synced base is
   kept both ways until `mm resolve`.
6. `mm status` and `mm diag` read and report Syncthing's state (connected
   peers, the folder's sync state and the folder options mm relies on).
   Outside criteria 2 and 3, mm never changes Syncthing's configuration.
7. Evidence: tests against a stand-in for Syncthing's local API cover setup,
   approval, the exact-match refusal and the pre-existing-configuration
   guide; an integration test joins a machine that holds iCloud-era state to
   a fresh fleet without losing a file; and the owner observes setup, join
   and approval on their own machines, recorded in PROGRESS.md
   `## Acceptance`.

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
   inspecting the folder on the owner's machines.

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
   for each agent family (Claude, Codex, Grok, Muse and Antigravity), summed
   across the fleet from Claude Code, Codex, Grok Build, Muse and
   Antigravity. Grok Build's runs count under their model's family.
2. Every attended push captures this machine's usage and git activity. A
   push run in a terminal runs each capture scan (repository discovery, the git
   walk, the token and session scans and the host usage readers) to completion,
   up to a scan cap that is a setting (default tens of seconds). Work a scan
   leaves unfinished at the cap resumes on later pushes, so capture is complete
   again within three attended pushes without a recovery command. A run that
   stops early reports "catching up (N left)", not a failure. Capture that stays
   incomplete longer is a failure that `mm status` reports with its fix.
3. Usage mm cannot read is named as a coverage gap in the retro and in
   `mm status`, never shown as a silent zero.
4. Evidence: a `retro-fleet` run on the owner's fleet showing complete capture
   from every active machine, recorded in PROGRESS.md `## Acceptance`.

### O8: Every command reports its outcomes briefly, in one style
1. `mm push` and `mm pull` end with a short summary: one line for each thing
   that actually happened, such as what moved or mm updating itself, and
   nothing for what didn't happen. On failure the last thing printed says
   what broke and the command that fixes it, and interactive commands exit
   non-zero. Nothing else is printed unless the owner must act: data at risk,
   or an actionable warning such as O6's growth warning. Routine detail
   (timings, counts of skipped or unchanged items, capture internals) stays
   in `--verbose`, `mm status` and `mm diag`. Progress shows on a terminal
   while it runs and clears when done.
2. Usage and activity capture never changes a push's outcome and prints
   nothing during a push, except a transient progress line on a terminal once a
   push has run longer than a few seconds, which clears before the outcome
   line. An attended `mm push` spends up to the scan cap (O7.2) on capture, and
   that cap, not the capture budget (default 5 s), bounds its time. `mm autopush`
   and `mm autopull`, the agent hooks, keep their small budgets and never wait
   on the cap, so capture never blocks an agent session.
3. `mm autopush` and `mm autopull` stay silent unless the owner must act;
   their outcomes appear in `mm status`.
4. `mm status` opens with a one-line verdict (in sync, changes waiting, or
   the action needed) and shows only the latest run of each kind.
5. Evidence: tests pin the success, failure and degraded-capture output, and
   the owner's own pushes.
6. Every command the owner runs by hand presents its output in one shared
   style (the same spinner, outcome lines, warning and error forms, notice
   prefix and fix-command form), so they all read as one tool. The agent
   hooks stay silent (O8.3).

## Planned after 2.0

### O9: Cross-model memory
What one agent learned on any of the owner's machines can be found by the
other agents on every machine (Claude Code, Codex, and agents that read
`AGENTS.md`), by searching, without mm copying a memory into another agent's
store. Design: [cross-model memory](designs/cross-model-memory.md).
1. Each machine publishes its own Codex memory files as per-machine copies,
   one writer per file. Peers receive them outside every host's native store,
   so they never conflict, and forgetting follows the source machine's own
   regeneration.
2. Static "search, don't read" pointers in the owner's `AGENTS.md` and
   `CLAUDE.md`, installed once with the owner's approval, say where the other
   agents' memories are and to search them rather than load them. mm never
   writes into generated memory files.
3. Phase 2, not part of acceptance: a per-machine search index (qmd) over the
   same markdown, never synced and rebuilt after `mm pull`, when grep proves
   too blunt or the owner accepts its dependencies.
4. Evidence: two live checks in isolated homes before implementation (fresh
   Codex and Claude sessions follow the pointer and find the other agent's
   memory by search; regenerating per-machine copies does not churn sync),
   then the owner's own sessions on two machines, recorded in PROGRESS.md
   `## Acceptance`.

## Constraints and accepted limitations
- End-to-end encryption is mandatory; no plaintext sync data reaches storage.
- Sync is allowlisted; session transcripts, credentials and whole-file
  settings that may hold credentials stay local.
- Explicit push and pull; mm watches no files. Syncthing moves the files.
  mm configures Syncthing only as O2 describes, on a machine whose Syncthing
  has its default configuration; otherwise the owner manages Syncthing and mm
  reads and reports its state.
- No storage file is rewritten by more than one machine.
- Usage capture reads host data without changing it and publishes only usage
  metadata (tokens, models, times, commit counts), never prompts,
  transcripts or filesystem paths.
- One user, no compatibility contract. mm refuses storage written in a format
  it cannot read, with an upgrade remedy; that is the only compatibility
  promise. Commands, flags, config keys, output and host families can change
  in any release, with a README Upgrading note. Auto-update upgrades a pipx
  `@latest` install at the end of its next `push`, `pull`, `autopull` or
  `autopush`, at most once a day. It does not reach a machine that runs none
  of them, is pinned to a tag, has turned `auto_install` off, or is refused
  for newer storage. `mm devices` shows each machine's version; the owner
  runs `mm update` on any machine that lags. Until the owner's last Mac has
  moved, a release keeps working on an iCloud Drive storage path, so a Mac
  not yet moved keeps syncing whatever release auto-update gives it.
- Core and extensions (owner, 2026-10-23). Host adapters (each agent's sync
  source and usage reader) and command groups are extensions to a small
  shared core, so a change is tested and reviewed against the piece it
  touches, not the whole project. Before a Track changes a host adapter or
  command group that still lives in `cli.py` or `host_usage.py`, that piece
  moves into its own module behind a contract test, with no behavior change.
  Code no planned change touches stays where it is; there is no one-shot
  rewrite. While working and reviewing, run only the tests whose recorded
  coverage reaches the changed code; the full suite runs in CI before merge.
- The owner's machine hostnames, or fragments of them, never appear in the
  repository: files name machines by role (2026-11-06), and code, tests,
  fixtures and docs use placeholders instead of a real username, email or
  person's name. The git history is not rewritten (owner, 2026-11-08):
  GitHub keeps pull-request refs and cached views of old commits that a
  force-push cannot clean.
- A bootstrap script installs Python, pipx, mm and the Syncthing package on
  macOS and Arch-based Linux, starts Syncthing at login where Syncthing has
  its default configuration (O2.1), stops with instructions if Homebrew is
  missing on macOS, and ships before 2.0.0.
- Accepted manual steps: pasting a fleet machine's Syncthing device ID into
  `mm init` and the new machine's into the approval command; configuring
  Syncthing with the README guide on a machine with a pre-existing Syncthing
  configuration; moving each machine off iCloud Drive by uninstalling and
  reinstalling mm; `mm resolve` for real conflicts; unlocking the Linux
  keyring before hooks run; re-joining a machine restored from backup or
  cloned; removing a dead machine from the fleet, in Syncthing on every
  machine that still lists it (introducers re-add it otherwise).
- Known limits: a project outside the home folder with no network git
  `origin` stays machine-specific. Grok Build memory for a repository with
  no `origin`, or for a folder that is not a repository, reaches other
  machines only as an inert copy that Grok there does not use, because Grok
  names those scopes from the checkout path (owner, 2026-11-06). Grok's
  pending observations stay on the machine that captured them until a Dream
  folds them into topics: before switching machines, run `/flush` and then
  `/dream` in Grok, then `mm push`, and run `mm pull` before starting Grok on
  the other machine (owner, 2026-11-06). mm does not detect a restored or
  cloned machine, which can republish stale files until it is re-joined.
  Content blob names are content hashes: they show whether two machines hold
  identical bytes, not the bytes. While some machines are on iCloud Drive and
  others on Syncthing, the two stores exchange nothing: a machine's changes
  reach the other store only when it moves. `retro-fleet` on the Syncthing
  fleet does not show the iCloud store's usage history. iCloud Drive is not
  supported once the owner's last Mac has moved.

## Non-goals and later work
- Windows and non-Arch Linux distributions. Revisit when such a machine needs
  to join the fleet.
- Multi-user collaboration, a GUI, real-time file watching, syncing session
  transcripts, walking a host home root. Unchanged exclusions.
- mm-managed Syncthing beyond O2's setup, approval and introducers: invite
  codes, relays, changing a pre-existing Syncthing configuration, removing
  dead machines and scan requests after push. Revisit when the fleet grows
  past a handful of machines, or pairing or folder settings cause an
  observed missed sync.
- A migration tool or fleet-wide cutover from iCloud Drive, and carrying the
  iCloud store's usage history into the Syncthing fleet (owner, 2026-11-07:
  "we don't need a migrate tool, because i'm the only user"). Revisit when a
  second person needs to move an existing fleet.
- Detecting restored, cloned or forked machines. Revisit when one damages
  fleet data in real use.
- Per-path conflict policies and a richer resolver. Revisit when conflicts
  become a recurring manual burden after 2.0.
- Syncing Grok Build plugins (`~/.grok/plugins/`, which Grok trusts
  automatically) and hooks. No such user-authored files exist on the owner's
  machines (2026-11-05). Revisit when the owner keeps a plugin or hook there
  that other machines need.
- Cross-model memory before 2.0, and writing into any host's native memory
  store at any time. O9 starts after 2.0.0.
- <a id="factory-handover"></a>Carrying mmm-factory's durable state
  (`~/.mmm/`) through mm, for backup or for handover between hosts (owner,
  2026-11-06: "hm, don't love that -- let's future this card and move on to
  the next one"). The opt-in `mmm` source planned on 2026-10-29 would
  re-upload a whole multi-megabyte ledger on each change and grow the
  Syncthing folder until `mm gc`. mm does not carry `~/.mmm/` today; the
  factory moves with mmm-factory's own tool (`migrate-home`,
  `mmm-manager launch --from`), which needs no mm. Revisit when the owner
  asks again for an off-machine backup or handover of the factory home, or
  mmm-factory's own move tool cannot carry the factory between hosts; the
  planned card and its findings wait in [Future](roadmap-future.md).
- Concurrent factory managers on two Macs, and moving in-flight factory
  work. Revisit when mmm-factory supports concurrent managers.
- Cursor: mm neither syncs nor reports it (owner, 2026-11-07: "removed
  totally"). Usage older releases already published is left as it is, with
  no migration. Revisit when the owner decides to bring Cursor back.
- Qualification ceremonies for shipped features: the self-update
  qualification (S13), Track 70A's two-Mac acceptance and the separate O6
  acceptance record were dropped on 2026-10-19. A feature in daily use counts
  as working; misbehavior is filed as a bug.
- Public beta. Revisit when a second person asks to run mm.

## Release policy
- Owner-only, before any public stage: ordinary releases are PATCH;
  documentation-only changes take no bump.
- Capability checkpoints, each a MINOR when accepted: **quiet output** (O8),
  **complete usage** (O7 criteria 1 to 3 on the owner's machines) and, after
  2.0, **cross-model memory** (O9 criteria 1, 2 and 4; owner, 2026-10-29).
- Stage versions: MVP-1 on iCloud (macOS) = 1.0.0, achieved. MVP-1 on
  Syncthing (macOS and Linux) = **2.0.0**: O1 to O4 accepted on the
  owner's own observation on one Mac and both Linux machines in the
  Syncthing fleet, with O6 to O8 holding, the bootstrap script and O2's
  Syncthing setup and approval shipped. MAJOR
  because the transport changes. A Mac not yet moved keeps running on iCloud
  Drive and does not hold 2.0.0 or any other machine up; a machine may move
  before 2.0.0 on any release that has the Syncthing support it needs.
- Strict SemVer: not used. The 1.x compatibility contract that applied it
  from 1.0.0 is retired as of 2026-10-19. Strict SemVer would start at
  public-beta, which is not planned.
- Compatibility surface: storage-format gates only. An mm refuses newer or
  unknown storage formats with an upgrade remedy. A change that needs every
  machine upgraded before it syncs is at least a MINOR and says so in the
  README Upgrading notes.
- Version format: three components in `pyproject.toml`, mirrored by a
  root `VERSION` file holding the same `x.y.z`; a release bump changes both.
  The release baseline is the last commit that changed `VERSION` on the base
  branch.
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
  - [Host parity](designs/host-parity.md): usage-family rationale and the
    Grok Build parity notes of 2026-11-05. This spec's table owns host
    scope; the design's Cursor material is historical as of 2026-11-07.
  - [Multi-source sync](designs/sync-gstack-context.md): shipped design still
    cited by invariants.
  - [Cross-model memory](designs/cross-model-memory.md): O9's design and
    its qualification record.
  - [Memory continuity](designs/memory-continuity.md) and the
    [Codex memory contract](designs/codex-memory-contract.md): Track 68A
    evidence. Their cross-agent design is superseded by cross-model memory
    as of 2026-10-29.
- Superseded history:
  - [Grok usage reader](archive/grok-build-usage-reader.md): design of the
    Grok Build reader retired on 2026-10-22. The restored reader qualifies
    against the installed Grok Build and records its own contract; consult
    the archive only for the retired reader's deduplication and counter
    rules.
  - [Store-v2 design](archive/store-v2.md): superseded as the 2.0 design on
    2026-10-19. Evidence: the "Why a new store" hazard table (the Syncthing
    problems the 1.x store must handle), §7 portable paths, §10 Syncthing
    folder options, §11 Linux and §13 cutover.
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
