# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Cursor, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support, removed in v1.11.8, returns at parity with Claude Code and Codex by the owner's 2026-11-05 decision: its pricing, scoped source and consent, and usage reader shipped (Tracks 109B, 109A and 110A), and its memory sync is Group 111. Groups 82–91, 94, 97 and 101 (resumable capture, affected-test selection, the status, Codex, pull, init and source-command moves, one capture budget, Syncthing-tolerant reads, explicit fleet creation and join, machine-local gstack files, shared Claude project memory, recapture's own walk budget, fast-forward pulls and propagated deletions, an honest remedy for unreadable host records and a fair attended usage sweep) shipped in v1.11.9–v1.12.5, and Tracks 102A to 106A (the Codex warm-read memo, discovery from every agent, published identities, finished discovery and the named Cursor ACP gap) in v1.12.6–v1.12.10, Tracks 108A, 98A and 92A (the memory index tidy, the bootstrap script and Syncthing state in status) in v1.12.11–v1.12.13, and Tracks 109B, 109A and 110A (Grok 4.7 pricing, the restored grok source and its consent, and Grok Build usage) in v1.12.14–v1.12.16; all live in shipped history. Grok models used through Cursor stay in the Grok row beside Grok Build's. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); Group 96 qualifies it after the cutover.

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

None declared (2026-11-05). Tracks 109B (v1.12.14), 109A (v1.12.15) and 110A (v1.12.16) shipped and are recorded in shipped history.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110_

### Phase 5: mm 2.0 on Syncthing

**End-state:** The owner's Macs and two Linux machines sync over one Syncthing folder that carries the 1.x store, with fast-forward pulls, propagated deletions, every consented host's usage read within the attended budget, Grok Build at parity with Claude Code and Codex, a memory index that no longer resurrects deleted pointers, Syncthing state in status, a bootstrap script and a cutover runbook; the factory can hand over between Macs; docs/SPEC.md O1 to O4 are accepted on the owner's own observation after following the runbook, and released as 2.0.0.
**Groups:** 111, 95, 93

#### Group 111: Grok Build memory

_Depends on: none_

**This group delivers:** Grok Build's global and per-repository memory is the same on every machine.

##### Track 111A: Sync Grok Build memory between machines
_4 tasks . ~320 LOC . high risk . 10 files_
**Outcome:** With the grok source enabled, what Grok Build remembers on one machine (its global memory and each repository's memory) is there on the owner's other machines after push and pull, in the same repository's memory wherever each machine keeps its checkout, while Grok's generated index and databases stay local.
**Supports:** MVP-1, docs/SPEC.md "What mm syncs and reports" Grok Build row (memory) and O1.2 (per-repository memory lands in the same project on macOS and Linux).
**Done when:** the first task establishes from Grok Build's documentation and a live check in a temporary `GROK_HOME` (never the owner's real store), with the version and date recorded in `docs/designs/host-parity.md`, how `memory-v2/workspaces/<project>-<hash>/` is named, and whether two checkouts of one repository at different paths, or on two machines, get the same name; if names match across machines the scopes sync under their own names, and if not, workspace memory is published under the repository's identity as Claude project memory is (git remote, else home-relative path) and mapped back to the local workspace directory, and if neither is possible the Track stops and asks the owner instead of shipping global memory alone; the walker adds, for `memory-v2/global/` and each workspace scope, only `topics/` and `observations/_inbox/` Markdown, never `MEMORY.md`, SQLite files, the search index or `archive/`; a peer's new observation reaches the other machine's inbox and a deletion propagates, so a Dream on the active machine archives it everywhere; tests pin the allowlist against a fixture store and the workspace mapping with two checkout paths; the recorded test gap below is closed in `tests/test_manifest.py`; `docs/invariants/sync.md` and README describe Grok memory sync and that Grok keeps memory off by default; `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/projectid.py, src/mind_meld/pullflow.py, tests/test_grok_memory.py (new), tests/test_manifest.py, tests/test_projectid.py, docs/invariants/sync.md, docs/designs/host-parity.md, README.md_
_blocked-by: Track 110A_
_out: 95A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/designs/host-parity.md_
_session: fresh · effort: high · verify: ./bin/check --affected_

_Source: Karl, 2026-11-05, "go, it needs to bring grok to parity w/ the other models": Claude Code memory syncs, so Grok Build memory does (docs/SPEC.md table and O1.2 as amended 2026-11-05). Evidence: Grok Build 1.0.70's `docs/user-guide/13-memory.md` (memory lives in `~/.grok/memory-v2/global/` and `workspaces/<project>-<hash>/`, each with `topics/`, `observations/_inbox/`, a generated `MEMORY.md` and Grok's own SQLite and index files; "Clones and worktrees of the same repository share one workspace scope"; memory is off by default); this Mac holds a populated `memory-v2/` with per-worktree workspace names such as `mmm-05c9f851-98fbfdda` beside `mmm-factory-cd72c70b`. A quick check of SHA-256 over the checkout path, remote URL and root commit did not reproduce the 8-hex `<hash>` for `mmm-factory` or `mind-meld`, so the naming is unknown. Re-checked at 027840b: Claude project memory is mapped by `projectid.py` since 1.12.0._

- **Workspace naming check** -- _docs/designs/host-parity.md, ~30 lines._ (S)
- **Memory walker and workspace mapping** -- _src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/projectid.py, src/mind_meld/pullflow.py, tests/test_grok_memory.py, tests/test_manifest.py, tests/test_projectid.py, ~240 lines._ (M)
- **Test gap: unreadable Grok source root** -- re-verify first, then assert that a failing stat of the Grok source root makes the strict snapshot refuse and the permissive walk return nothing (the existing strict scandir test covers a different boundary). _tests/test_manifest.py, ~20 lines._ (S)
- **Docs** -- _docs/invariants/sync.md, README.md, ~30 lines._ (S)

#### Group 95: Factory handover

_Depends on: Group 111_

**This group delivers:** An opt-in `mmm` source carries mmm-factory's durable state between Macs.

##### Track 95A: Add an opt-in mmm source for factory handover
_2 tasks . ~220 LOC . medium risk . 7 files_
**Outcome:** After `mm enable-source mmm` on each Mac, the owner stops the factory on one Mac, pushes, pulls on another Mac and starts it there with the items, plans, cards, settings, passports, handoff notes, briefs and run ledgers in place; locks, watcher state, fields files, worker-ID lists, usage caches and other machine-local factory files stay behind.
**Supports:** docs/SPEC.md "What mm syncs and reports" mmm-factory row and [factory handover](SPEC.md#factory-handover) (owner decisions 2026-10-29).
**Done when:** a default `mmm` source on `~/.mmm` is opt-in like Muse (never active until `mm enable-source mmm` on that machine, or an explicit choice at `mm init` or `mm reconfigure-sources`); it syncs `items/` (each item's `<id>.json`, `.plan.md` and `.card.md`), the four run ledgers (`provenance.jsonl`, `hangs.jsonl`, `records.jsonl`, `picker.jsonl`) and, per project, `manager.toml`, the passport (`manager-passport.txt`), the handoff note (`driver-handoff.md`) and its timestamped archives, the manager's launch cards (`cards/`) and `briefs/`; it never syncs `*.lock`, `manager-watch.*`, `manager-status.json`, `manager-fields.json`, `manager-usage.json`, the worker-ID lists (`manager-docs-workers.txt`, `release-text-workers.txt`), the worker-side `mmm-<id>-card.md` and `mmm-<id>-autoplan-<n>.md` copies, SQLite files, `traces/`, `release/`, `review-compare/` or `picker-jev/`, and `~/.local/share/mmm-factory/` and `~/.config/mmm-factory/` are never sources; `~/.mmm` must be a real directory (a link is refused, as the factory requires); README documents enabling it, the single-Mac rule, that in-flight work does not transfer (finish or ship first), that item records carry raw worker text to every machine with the source enabled, and that mm must not write `~/.mmm` while mmm-factory's own move tool (`migrate-home`, `mmm-manager launch --from`) copies it: the owner does not run `mm pull` on the target machine until the factory move finishes; tests pin the allowlist against a fixture factory home; `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_sourceflow.py, tests/test_mmm_source.py (new), docs/invariants/sync.md, README.md_
_blocked-by: Track 90B, Track 92A, Track 94A, Track 111A_
_out: 93A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-10-29, including mmm-factory manager Ugarit's recommendation the owner accepted (exclude `traces/`, `release/`, `review-compare/` and the worker-ID lists; add `cards/`; `briefs/` per option (a), which the owner confirms at the PR that adds this card and which needs mmm-factory's managers to keep handoff briefs under `~/.mmm/projects/<slug>/briefs/`). Paths confirmed against mmm-factory's `docs/agent-runbook.md` "The factory home" at 93cf70a (v0.5.29) and its file-name constants (`manager.toml`, `manager-passport.txt`, `driver-handoff.md`, `manager-fields.json`, `manager-watch.json`, `manager-watch.owner`, `manager-watch.lock`, `manager-status.json`, `manager-usage.json`, `release-text-workers.txt`). Factory attribution in `retro-fleet` stays deferred (Future)._

_Factory move tool (Karl, 2026-11-05, "both are fine, that way mmm can migrate without the mm prereq, but we can keep machines in sync, seems fine"): mmm-factory's own move (`migrate-home`, `mmm-manager launch --from`) and this source both stay, so the factory can move without mm installed; the README line above keeps mm from writing `~/.mmm` while that copy runs._

_Open questions for planning (mmm-factory's manager, 2026-11-02; record only, the card's scope is unchanged): (1) should syncing `~/.mmm` be an explicit command that refuses unless every factory project is quiet, rather than riding autopull and autopush; (2) projects running on different machines at once versus the single-Mac rule the card documents; (3) whether line union is right for the factory's JSONL ledgers; (4) absolute repo paths inside item JSON when one record moves between a Mac home and a Linux home; (5) Track 91A's `.mind-meld-anchor` generation dotfile in `~/.mmm` and real deletions propagating under `~/.mmm` now that tombstones delete._

- **The mmm source and its allowlist** -- _src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_mmm_source.py, ~150 lines._ (M)
- **Handover docs** -- _README.md, docs/invariants/sync.md, tests/test_sourceflow.py, ~70 lines._ (S)

#### Group 93: Cutover runbook

_Depends on: Group 95_

**This group delivers:** A runbook and agent docs for moving the fleet onto Syncthing.

##### Track 93A: Write the cutover runbook and the Syncthing documentation
_3 tasks . ~450 LOC . low risk . 11 files_
**Outcome:** The owner has a step-by-step runbook to move the fleet from iCloud Drive to Syncthing and bring both Linux machines in, and every document an agent reads describes the Syncthing store.
**Supports:** MVP-1, docs/SPEC.md O1 to O4 (the cutover, and the owner's observation after it).
**Done when:** README covers installing on macOS and Arch with the bootstrap script, pairing, the folder options mm relies on, how `~/Sync/mind-meld` relates to Syncthing's Default Folder at `~/Sync`, and a cutover runbook: update every machine in any order (there is no fleet order since 1.12.0), except that a machine at 0.14.18 or older upgrades before its next attended `mm push` or `mm gc`, or stays off; stop hooks; move the store folder from iCloud Drive into the Syncthing folder; point `storage.path` at it; put mm's temporary-file line in `.stignore` on every machine, because the cutover never runs `mm init --new-fleet` and Syncthing does not sync that file; bring in each of the two Linux machines one at a time (bootstrap, pair, accept the folder, unlock the keyring before hooks run, `mm init`); retire a dead machine by moving its device entry aside as README's "Retiring a dead machine" says, never by deleting its directories and never while a machine at 1.7.1 or older can run; delete the iCloud folder as the runbook's last step, once every machine has joined and reports in sync; and an optional time-boxed rehearsal between a Linux machine and a Mac with a throwaway folder, temp `HOME` and throwaway passphrase, running `mm init` before and after delivery; the runbook carries Track 91A's waived manual check as an owner step at cutover: README's deletion-preview and recovery recipe on both active Macs, recovering a deleted file on its origin Mac, and `--verbose` flush timing, with what to record; it also carries Track 92A's three owner-deferred live checks as owner steps: `mm status` and `mm diag` against a real Syncthing 2.1 or newer (state, folder options, and Syncthing's config file unchanged by mm), the same with Syncthing stopped (exit 0, reported unreachable), and filing any endpoint or field difference seen as a bug; AGENTS.md Key Principles and root SPEC.md's storage sections describe the Syncthing folder; `sync.md` and `init-devices.md` drop iCloud-only rules; the pre-2026-10-19 Track IDs in the routing README, `init-devices.md` and `tests/test_pullhistory.py` are retired; root SPEC.md's `mm sources` steps and `sync.md`'s "all configured sources" sentence and `has_explicit_sources` wording match the code; README and `.github/workflows/ci.yml` stop citing a soak; `pyproject.toml` description and keywords drop iCloud; `tests/test_version.py` fails when `VERSION` and `pyproject.toml` differ; docs/SPEC.md's superseded-history entry for the store design names §13 (cutover) as evidence alongside §7, §10 and §11; `./bin/check` passes.
_touches: README.md, AGENTS.md, SPEC.md, docs/SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, tests/test_version.py, tests/test_pullhistory.py, .github/workflows/ci.yml_
_blocked-by: Track 89A, Track 91A, Track 92A, Track 95A, Track 98A_
_out: 96A_
_read-first: docs/SPEC.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py tests/test_version.py_

_Source: docs/SPEC.md O1 to O4 and the accepted manual cutover step; docs/archive/store-v2.md §11 (Linux) and §13 (cutover) as evidence. Runbook order and the retire and upgrade lines: Track 90A plan c85d0d67 Gate notes, its `[challenge]` C1 (Karl, Option A) and README's "Retiring a dead machine". Track 91A's manual check: PR #251 matrix row D1, deferred by Karl on 2026-11-02 ("Explicitly defer the manual check.") and placed here by the manager's account of that run. `.stignore`, the Default Folder note and the rehearsal: Track 89A review record FU1, FU4 and FU7. Stale docs: PR #246 review receipt, re-checked at 2a101d6 (`rg _set_grok_host_usage` matches only `sync.md`). Drift check: Karl, "sure", 2026-10-28; at 2a101d6 `tests/test_version.py` checks that no source reads `VERSION` and nothing compares it with `pyproject.toml`._

- **Runbook and Syncthing setup** -- _README.md, ~270 lines._ (M)
- **Agent-facing docs** -- _AGENTS.md, SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, .github/workflows/ci.yml, ~170 lines._ (M)
- **Version drift check** -- _tests/test_version.py, ~20 lines._ (S)

#### Group 96: Cross-model memory qualification

_Depends on: Group 93_

**This group delivers:** Two live checks prove or refute the cross-model memory design before implementation.

##### Track 96A: Qualify cross-model memory in isolated homes
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
- Group 111 ← {}
- Group 95 ← {111}
- Group 93 ← {95}
- Group 96 ← {93}
```

Track detail per group:

```
Group 111: Grok Build memory
  +-- Track 111A .......... ~L . 4 tasks
Group 95: Factory handover
  +-- Track 95A ........... ~L . 2 tasks
Group 93: Cutover runbook
  +-- Track 93A ........... ~L . 3 tasks
Group 96: Cross-model memory qualification
  +-- Track 96A ........... ~M . 1 task
```

**Total: 4 groups . 4 tracks remaining.**

On 2026-11-05 the owner restored Grok Build at parity with Claude Code and Codex ("go, it needs to bring grok to parity w/ the other models"). The same day Track 109B priced Grok 4.7 (v1.12.14), Track 109A restored the scoped `grok` source and its consent (v1.12.15) and Track 110A read Grok Build usage into `retro-fleet` (v1.12.16); Track 111A, Grok Build memory sync, now leads the plan, and its `_blocked-by: Track 110A` is satisfied. Track 107A (the Cursor account usage reader) was killed on 2026-11-05: after the account moved to the X unified subscription around 2026-10-21, Cursor exposes per-request usage only through the account's billing export, and the owner chose local-only capture for consistency with the other providers ("A, but we don't need the separate line"); it reopens when Cursor's ACP sends per-call usage, and docs/SPEC.md's non-goal for Cursor via ACP front-ends carries that trigger. Track 95A waits on Track 111A (both edit `config.py`); Tracks 111A, 95A and 93A all edit `README.md` and run in that order, so the cutover runbook (93A) can cover factory handover and Grok. Track IDs 93A, 95A and 96A keep the meaning they had before the 2026-11-05 regeneration, because `docs/invariants/sync.md` and other documents cite them, which is why Group numbers do not follow execution order.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on their own observation on one Mac and both Linux machines after following Group 93's runbook, with O6 to O8 holding, the bootstrap script shipped and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance`. Problems are fixed as they appear. Cross-model memory (O9) is qualified after the cutover by Group 96; its implementation waits in Future until that qualification passes.

---

## Future

Deferred: docs/roadmap-future.md (39 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
