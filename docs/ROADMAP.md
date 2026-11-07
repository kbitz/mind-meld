# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Cursor, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support, removed in v1.11.8, returned at parity with Claude Code and Codex by the owner's 2026-11-05 decision: its pricing, scoped source and consent, usage reader, pricing-override fix and memory-topic sync shipped (Tracks 109B, 109A, 110A, 111B and 111A). Groups 82–91, 94, 97 and 101 (resumable capture, affected-test selection, the status, Codex, pull, init and source-command moves, one capture budget, Syncthing-tolerant reads, explicit fleet creation and join, machine-local gstack files, shared Claude project memory, recapture's own walk budget, fast-forward pulls and propagated deletions, an honest remedy for unreadable host records and a fair attended usage sweep) shipped in v1.11.9–v1.12.5, and Tracks 102A to 106A (the Codex warm-read memo, discovery from every agent, published identities, finished discovery and the named Cursor ACP gap) in v1.12.6–v1.12.10, Tracks 108A, 98A and 92A (the memory index tidy, the bootstrap script and Syncthing state in status) in v1.12.11–v1.12.13, Tracks 109B, 109A and 110A (Grok 4.7 pricing, the restored grok source and its consent, and Grok Build usage) in v1.12.14–v1.12.16, and Tracks 111B and 111A (Grok pricing overrides across aliases and Grok Build memory topics) in v1.12.17–v1.12.18; all live in shipped history. Grok models used through Cursor stay in the Grok row beside Grok Build's. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); Group 96 qualifies it after the cutover.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed (spec, 2026-10-18).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`. Invite codes, mesh healing, relays, folder configuration through Syncthing's API and scan requests are refused until the spec's revisit trigger is observed.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line (spec O8, 2026-10-19).** A Track that adds output to push, pull or the auto commands must show it is the outcome, a failure with its fix, data at risk or an actionable warning; everything else goes to `mm status`, `mm diag` or `--verbose`.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-06). Tracks 111B (v1.12.17) and 111A (v1.12.18) shipped and are recorded in shipped history; Group 111 is retired.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111_

### Group 93: Cutover runbook

_Depends on: none_

**This group delivers:** A runbook and agent docs for moving the fleet onto Syncthing.

#### Track 93A: Write the cutover runbook and the Syncthing documentation
_3 tasks . ~450 LOC . low risk . 11 files_
**Outcome:** The owner has a step-by-step runbook to move the fleet from iCloud Drive to Syncthing and bring both Linux machines in, and every document an agent reads describes the Syncthing store.
**Supports:** MVP-1, docs/SPEC.md O1 to O4 (the cutover, and the owner's observation after it).
**Done when:** README covers installing on macOS and Arch with the bootstrap script, pairing, the folder options mm relies on, how `~/Sync/mind-meld` relates to Syncthing's Default Folder at `~/Sync`, and a cutover runbook: update every machine in any order (there is no fleet order since 1.12.0), except that a machine at 0.14.18 or older upgrades before its next attended `mm push` or `mm gc`, or stays off; stop hooks; move the store folder from iCloud Drive into the Syncthing folder; point `storage.path` at it; put mm's temporary-file line in `.stignore` on every machine, because the cutover never runs `mm init --new-fleet` and Syncthing does not sync that file; bring in each of the two Linux machines one at a time (bootstrap, pair, accept the folder, unlock the keyring before hooks run, `mm init`); retire a dead machine by moving its device entry aside as README's "Retiring a dead machine" says, never by deleting its directories and never while a machine at 1.7.1 or older can run; delete the iCloud folder as the runbook's last step, once every machine has joined and reports in sync; and an optional time-boxed rehearsal between a Linux machine and a Mac with a throwaway folder, temp `HOME` and throwaway passphrase, running `mm init` before and after delivery; the runbook carries Track 91A's waived manual check as an owner step at cutover: README's deletion-preview and recovery recipe on both active Macs, recovering a deleted file on its origin Mac, and `--verbose` flush timing, with what to record; it also carries Track 92A's three owner-deferred live checks as owner steps: `mm status` and `mm diag` against a real Syncthing 2.1 or newer (state, folder options, and Syncthing's config file unchanged by mm), the same with Syncthing stopped (exit 0, reported unreachable), and filing any endpoint or field difference seen as a bug; AGENTS.md Key Principles and root SPEC.md's storage sections describe the Syncthing folder; `sync.md` and `init-devices.md` drop iCloud-only rules; the pre-2026-10-19 Track IDs in the routing README, `init-devices.md` and `tests/test_pullhistory.py` are retired; root SPEC.md's `mm sources` steps and `sync.md`'s "all configured sources" sentence and `has_explicit_sources` wording match the code; README and `.github/workflows/ci.yml` stop citing a soak; `pyproject.toml` description and keywords drop iCloud; `tests/test_version.py` fails when `VERSION` and `pyproject.toml` differ; docs/SPEC.md's superseded-history entry for the store design names §13 (cutover) as evidence alongside §7, §10 and §11; `./bin/check` passes.
_touches: README.md, AGENTS.md, SPEC.md, docs/SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, tests/test_version.py, tests/test_pullhistory.py, .github/workflows/ci.yml_
_blocked-by: Track 89A, Track 91A, Track 92A, Track 98A_
_out: 96A_
_read-first: docs/SPEC.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py tests/test_version.py_

_Source: docs/SPEC.md O1 to O4 and the accepted manual cutover step; docs/archive/store-v2.md §11 (Linux) and §13 (cutover) as evidence. Runbook order and the retire and upgrade lines: Track 90A plan c85d0d67 Gate notes, its `[challenge]` C1 (Karl, Option A) and README's "Retiring a dead machine". Track 91A's manual check: PR #251 matrix row D1, deferred by Karl on 2026-11-02 ("Explicitly defer the manual check.") and placed here by the manager's account of that run. `.stignore`, the Default Folder note and the rehearsal: Track 89A review record FU1, FU4 and FU7. Stale docs: PR #246 review receipt, re-checked at 2a101d6 (`rg _set_grok_host_usage` matches only `sync.md`). Drift check: Karl, "sure", 2026-10-28; at 2a101d6 `tests/test_version.py` checks that no source reads `VERSION` and nothing compares it with `pyproject.toml`._

- **Runbook and Syncthing setup** -- _README.md, ~270 lines._ (M)
- **Agent-facing docs** -- _AGENTS.md, SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, .github/workflows/ci.yml, ~170 lines._ (M)
- **Version drift check** -- _tests/test_version.py, ~20 lines._ (S)

### Group 96: Cross-model memory qualification

_Depends on: Group 93_

**This group delivers:** Two live checks prove or refute the cross-model memory design before implementation.

#### Track 96A: Qualify cross-model memory in isolated homes
_1 task . ~120 LOC . low risk . 1 file_
**Outcome:** The owner knows whether the approved cross-model memory design works with the current Codex and Claude Code before any implementation is carded: fresh sessions follow a "search, don't read" pointer to the other agent's memory, and per-machine Codex copies do not churn sync.
**Supports:** docs/SPEC.md O9.4 (qualification before implementation), after 2.0.
**Done when:** `docs/designs/cross-model-memory.md` records Q1 and Q2 run in temporary `HOME` and `CODEX_HOME` directories (never the owner's real stores), with the observed codex-cli and Claude Code versions; Q1: a fresh Codex session finds a Claude-origin memory through an `AGENTS.md` pointer and a fresh Claude session finds a Codex-origin memory through a `CLAUDE.md` pointer, by search, without loading `MEMORY.md` whole, and which file Codex loads at startup is recorded; Q2: after Codex regenerates its memory files on one machine, a push publishes only that machine's copies and a peer applies them with no conflict copy and no write into any native store; the chosen Codex file set is recorded; a failed check is written up for the owner and no implementation Track is proposed; no product code changes.
_touches: docs/designs/cross-model-memory.md_
_blocked-by: Track 93A_
_read-first: docs/designs/cross-model-memory.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py_

_Source: owner decision 2026-10-29 ("yes, I want cross-model memory", post-2.0, qualification card after the cutover); probe `~/.gstack/projects/kbitz-mind-meld/cross-model-memory-probe-20261029.md` on codex-cli 0.164.0._

- **Run Q1 and Q2 and record them** -- _docs/designs/cross-model-memory.md, ~120 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 93 ← {}
- Group 96 ← {93}
```

Track detail per group:

```
Group 93: Cutover runbook
  +-- Track 93A ........... ~L . 3 tasks
Group 96: Cross-model memory qualification
  +-- Track 96A ........... ~M . 1 task
```

**Total: 2 groups . 2 tracks remaining.**

On 2026-11-06 Tracks 111B (Grok pricing overrides across aliases, v1.12.17) and 111A (Grok Build memory topics, v1.12.18) shipped, completing Grok Build's return at parity, and Group 111 is retired. Track 111A shipped with two owner scope decisions from its live checks on Grok Build 1.0.77, now recorded in docs/SPEC.md: only memory topics sync, so observations become topics on the sending machine (`/flush`, `/dream`, `mm push`) before handoff; and a repository with no `origin`, or a folder that is not a repository, reaches other machines only as an inert copy. The same day the owner moved Track 95A (the opt-in mmm source for factory handover) to Future after seeing its plan's costs ("hm, don't love that -- let's future this card and move on to the next one"): mm does not carry `~/.mmm`, and the factory moves between hosts with mmm-factory's own tool, so Group 95 is gone and the cutover runbook makes no factory-handover promise. Track 93A (the cutover runbook) is next; its `_blocked-by:` prerequisites have all shipped, and 96A follows the cutover. Track IDs 93A and 96A keep the meaning they had before the 2026-11-05 regeneration, because other documents cite them, which is why Group numbers do not follow execution order.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on their own observation on one Mac and both Linux machines after following Group 93's runbook, with O6 to O8 holding, the bootstrap script shipped and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance`. Problems are fixed as they appear. Cross-model memory (O9) is qualified after the cutover by Group 96; its implementation waits in Future until that qualification passes.

---

## Future

Deferred: docs/roadmap-future.md (40 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
