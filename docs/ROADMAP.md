# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Cursor, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support was removed in v1.11.8; Groups 82–91, 94, 97 and 101 (resumable capture, affected-test selection, the status, Codex, pull, init and source-command moves, one capture budget, Syncthing-tolerant reads, explicit fleet creation and join, machine-local gstack files, shared Claude project memory, recapture's own walk budget, fast-forward pulls and propagated deletions, an honest remedy for unreadable host records and a fair attended usage sweep) shipped in v1.11.9–v1.12.5, and Tracks 102A to 106A (the Codex warm-read memo, discovery from every agent, published identities, finished discovery and the named Cursor ACP gap) in v1.12.6–v1.12.10; all live in shipped history; Grok models used through Cursor stay in the Grok row. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); Group 96 qualifies it after the cutover.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations, standalone Grok Build support (2026-10-19) and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision.
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed (spec, 2026-10-18).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`. Invite codes, mesh healing, relays, folder configuration through Syncthing's API and scan requests are refused until the spec's revisit trigger is observed.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line (spec O8, 2026-10-19).** A Track that adds output to push, pull or the auto commands must show it is the outcome, a failure with its fix, data at risk or an actionable warning; everything else goes to `mm status`, `mm diag` or `--verbose`.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-04). Tracks 105A (v1.12.6), 102A (v1.12.7), 104A (v1.12.8), 106A (v1.12.9) and 103A (v1.12.10) shipped and are recorded in shipped history.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 94, 97, 99, 100, 101, 102, 103, 104, 105, 106_

### Phase 5: mm 2.0 on Syncthing

**End-state:** The owner's Macs and two Linux machines sync over one Syncthing folder that carries the 1.x store, with fast-forward pulls, propagated deletions, every consented host's usage read within the attended budget, Cursor account usage read when the owner opts in, a memory index that no longer resurrects deleted pointers, Syncthing state in status, a bootstrap script and a cutover runbook; the factory can hand over between Macs; docs/SPEC.md O1 to O4 are accepted on the owner's own observation after following the runbook, and released as 2.0.0.
**Groups:** 107, 108, 98, 92, 95, 93

#### Group 107: Cursor account usage

_Depends on: none_

**This group delivers:** With the owner's opt-in, `retro-fleet` counts Cursor use through Paseo (ACP) and every other Cursor client from the account's own per-request usage, read with the cursor-agent login already on the machine.

##### Track 107A: Read Cursor account usage through cursor-agent's existing login, opt-in and read-only
_4 tasks . ~470 LOC . high risk . 14 files_
**Outcome:** After the owner opts in on a machine, `retro-fleet` counts the owner's Cursor requests, including ACP sessions driven by Paseo, from Cursor's account usage, each request once across the fleet and never twice with a hook-recorded `mm cursor-agent` run; `mm status` stops naming the ACP gap while the reader works, and names what broke with its fix when Cursor changes its format.
**Supports:** MVP-1, docs/SPEC.md O7.1 (Cursor's runs count under their model's family) and O7.3 (usage mm cannot read is named, never a silent zero); the 2.0.0 release line needs O7 (O7.4).
**Done when:** the first task establishes, without printing, logging or copying the token (store kind, presence and key names only), which token store cursor-agent uses on macOS (this Mac, live) and on Linux (from cursor-agent's own code at the installed version, and live when a Linux machine is reachable), and records the response shape of the read-only endpoints in `tests/fixtures/host_sessions/cursor_account/CONTRACT.md`: a census of real responses on this Mac using key and column names and counts only (rows, models, date span, which counters, whether they are inclusive or disjoint, which per-request identifiers exist), with the cursor-agent version and date; then the Track builds the reader, and if that task finds no store mm can read or no per-request counters it stops and asks the owner what to build instead of ending with a write-up; the reader lives in `src/mind_meld/hosts/cursor_account.py` behind the adapter contract (`tests/test_host_adapter_contract.py`), leaving `host_usage.py`'s Cursor reader unchanged (core-and-extensions constraint); it is off until the owner opts in on that machine, like other host usage consent (`config.HOST_USAGE_ONLY_CONSENT`); it calls only read-only usage endpoints (`cursor.com`'s `api/auth/me`, `api/usage-summary`, `api/usage` and the dashboard usage-events CSV) and nothing that writes; the token is held in memory for the read and never stored, and a test pins that no token string reaches config, caches, events, logs, `mm diag` or fixtures; README says a macOS Keychain prompt may appear once; it runs only on attended pushes and `mm recapture`, with network timeouts inside the attended capture budget, and autopush and previews never call the network; account usage is whole-account, so each request is counted once across machines and against hook-recorded runs and Conductor store rows, keyed on the identifier the census finds (the PR states the rule and a test pins it); a missing key, changed column, refused login or HTTP error fails closed with one line naming what broke and the one fixing command (O8) in `mm status` and `mm diag`, publishes no partial total and leaves other readers and the push's exit code alone; `retro-fleet` counts the requests under their model's family; the ACP gap notice in `mm status` and `retro-fleet` (Track 103A) shows only while the reader is off or failing, with the opt-in as its fix, and the status tests assert that notice alongside a successful scan and alongside a reader blocker; the PR records a live run on this Mac (opt in, an attended push, `mm diag --json` showing the reader complete, `mm retro-fleet 7d` showing Cursor ACP use); `./bin/check --affected` passes.
_touches: src/mind_meld/hosts/cursor_account.py (new), src/mind_meld/host_registry.py, src/mind_meld/config.py, src/mind_meld/events_tail.py, src/mind_meld/statusflow.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_cursor_account_usage.py (new), tests/test_host_adapter_contract.py, tests/test_module_boundaries.py, tests/test_diag.py, tests/test_retro_fleet_aggregator.py, tests/fixtures/host_sessions/cursor_account/ (new), docs/invariants/events-retro.md, README.md_
_out: 108A, 98A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, tests/fixtures/host_sessions/cursor/CONTRACT.md_
_session: fresh · effort: high · verify: ./bin/check --affected_

_Source: Karl, 2026-11-04, "yes", on the manager's recommendation that mm may read cursor-agent's existing login on this machine to read Cursor account usage, opt-in like other host usage, read-only usage endpoints only, the token held in memory and never stored, failing with a clear message if Cursor changes its format; and, after Track 103A shipped only the gap notice, "We did a whole track to document a cursor gap? That is dumb; I thought it was fixing cursor usage", so this is a build card. Evidence: Track 103A's survey (`~/.gstack/projects/kbitz-mind-meld/103A-cursor-usage-survey.md`, cursor-agent 2026.10.22): ACP fires no session or stop hook and sends no usage, the local stores hold no billed counters, and per-request usage events with four counters plus cost need the account session; steipete/CodexBar's `docs/cursor.md`, as the manager relays it, names the endpoints above and the token sources (Cursor.app's `state.vscdb`, browser cookies, cursor-agent's token on Linux); neither source has been verified against Cursor by mm, which is why the first task establishes the store and the shape. The owner does not use the Cursor app (103A survey, owner answer 1). Re-checked at `d3828c0`: `hosts/` holds only `codex.py`; the ACP notice is in `statusflow.py` ("Cursor runs through ACP front-ends such as Paseo are not counted") and `aggregator.CURSOR_ACP_GAP_NOTE`. The two status assertions come from the inbox test gap filed by Track 103A's review._

- **Establish the token store and census the responses (read-only)** -- _tests/fixtures/host_sessions/cursor_account/CONTRACT.md, ~60 lines._ (S)
- **The account reader and its opt-in** -- _src/mind_meld/hosts/cursor_account.py, src/mind_meld/host_registry.py, src/mind_meld/config.py, src/mind_meld/events_tail.py, tests/test_cursor_account_usage.py, tests/test_host_adapter_contract.py, tests/test_module_boundaries.py, ~250 lines._ (M)
- **Fleet deduplication, retro-fleet counting and the gap notice** -- _src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/statusflow.py, tests/test_retro_fleet_aggregator.py, tests/test_diag.py, ~120 lines._ (M)
- **Docs and the live run** -- _docs/invariants/events-retro.md, README.md, ~40 lines._ (S)

#### Group 108: Memory index after a pull

_Depends on: Group 107_

**This group delivers:** A pull that line-merges a Claude memory index no longer brings back pointers to memory files the owner deleted, or near-duplicate pointers to one file.

##### Track 108A: Drop dangling and duplicate MEMORY.md pointers after a line-union merge
_2 tasks . ~220 LOC . medium risk . 5 files_
**Outcome:** When the owner prunes a project's Claude memory on one machine and a pull line-merges that project's MEMORY.md with a peer's copy, the index keeps only lines that point at memory files still on disk, one line per file, so it stops re-growing pointers to deleted files.
**Supports:** MVP-1, docs/SPEC.md O1.1 (deletions propagate, and `MEMORY.md` merges automatically) for the Claude Code project memory row of "What mm syncs and reports".
**Done when:** after a pull that line-union merges a MEMORY.md, a line whose markdown link targets a relative `.md` file that does not exist in that memory directory once the pull's applies and deletions finish is dropped, and lines are kept at most one per linked file, preferring the local line; lines without a relative `.md` link are kept as today; the merge never deletes or resurrects memory files; `docs/invariants/conflicts.md`'s "Line-deletion limit" is updated to say what returns and what no longer can; tests cover a deleted file whose line returns from the peer, a duplicate pair for one file, a line with no link, and a link to a file the same pull adds; `./bin/check --affected` passes.
_touches: src/mind_meld/merge.py, src/mind_meld/pullflow.py, tests/test_merge.py, tests/test_pullflow.py, docs/invariants/conflicts.md_
_blocked-by: Track 107A_
_out: 98A_
_read-first: docs/invariants/README.md, docs/invariants/conflicts.md, docs/invariants/sync.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Karl, 2026-11-04, "yes", on the factory manager's proposal; evidence from gstack-extend's manager Denver 15, relayed at Karl's request: Denver deleted 14 of 21 files in `~/.claude/projects/-Users-kb-Documents-Dev-Personal-gstack-extend/memory/` and cut MEMORY.md to 8 lines; at 12:40Z an `mm pull`, right after kb-mbp upgraded mm and pulled and pushed, rewrote MEMORY.md with the old 24-line index plus the new pointer line, and the 14 deleted files stayed deleted; Karl confirmed it was an mm pull. Re-checked at `3d1e624`: `merge.merge_lines` set-unions and sorts the lines, `merge._merge_strategy` sends every `MEMORY.md` to it, `pullflow._apply_merge` writes the union per file during apply, and `_run_deletions` runs later in `_pull_core`, so the dangling check has to wait for the deletions; conflicts.md's "Line-deletion limit" says the deleted line returns on the both-changed row and the no-base fallback. Only MEMORY.md's own lines change; the files are untouched._

- **Tidy the merged index after applies and deletions** -- _src/mind_meld/merge.py, src/mind_meld/pullflow.py, tests/test_merge.py, tests/test_pullflow.py, ~190 lines._ (M)
- **Line-deletion limit** -- _docs/invariants/conflicts.md, ~30 lines._ (S)

#### Group 98: Bootstrap script

_Depends on: Group 107, Group 108_

**This group delivers:** One script readies a fresh Mac or Arch-based machine with Python, pipx, mm and the Syncthing package.

##### Track 98A: Add a bootstrap script that readies a machine for mm and Syncthing
_2 tasks . ~250 LOC . medium risk . 3 files_
**Outcome:** On a fresh Mac or Arch-based Linux machine, the owner runs one script and has Python, pipx, mm and the Syncthing package installed, then follows its printed next steps to pair machines, share the folder and run `mm init`.
**Supports:** MVP-1, docs/SPEC.md Constraints (the bootstrap script) and the 2.0.0 release line; Track 93A's runbook uses it.
**Done when:** `bin/bootstrap` on macOS stops with Homebrew's install instructions and a non-zero exit when `brew` is missing, and otherwise installs what is missing through Homebrew; on Arch-based Linux it installs what is missing with `pacman -S --needed`; any other system stops and names what is supported; it confirms Python 3.11 or newer and Syncthing 2.1 or newer, installs mm through pipx from `@latest` unless pipx already has it, and stops with the fix when `mm` on `PATH` is not pipx's copy; a rerun changes nothing and says so; it pairs no machine, shares no folder and writes no Syncthing configuration; success ends with those next steps, and a failure's last line says what broke and the one command that fixes it (O8); tests run it with fake `brew`, `pacman`, `pipx`, `python3`, `syncthing` and `mm` on `PATH` under a `tmp_path` home and cover macOS without Homebrew, macOS, Arch, an unsupported system, a rerun, a too-old Python or Syncthing and a shadowed `mm`; README's install section gives the one command that fetches and runs the script from the `latest` tag and says pairing and sharing stay manual; `./bin/check --affected` passes.
_touches: bin/bootstrap (new), tests/test_bootstrap.py (new), README.md_
_blocked-by: Track 97A, Track 107A, Track 108A_
_out: 92A, 93A_
_read-first: docs/SPEC.md, README.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Karl, 2026-11-01: "we need a bootstrap script before 2.0, so we should make a track for that"; docs/SPEC.md Constraints and supported conditions at 2a101d6 (Python 3.11+, Syncthing 2.1 or newer). Re-checked at 2a101d6: no `bin/bootstrap` exists; `upgrade.INSTALL_SPEC` and README install pipx from `@latest`; README's Linux section names Arch's `python-pipx`. The shadowed-`mm` stop comes from a stale `/opt/homebrew/bin/mm` that has shadowed pipx's copy on the owner's Macs. Keyring packages stay in README's Linux section: the spec's bullet does not list them, and unlocking the keyring is an accepted manual step._

- **Bootstrap script and its tests** -- _bin/bootstrap, tests/test_bootstrap.py, ~200 lines._ (M)
- **README install path** -- _README.md, ~50 lines._ (S)

#### Group 92: Syncthing status

_Depends on: Group 98_

**This group delivers:** Status shows Syncthing's state and each machine's latest push.

##### Track 92A: Report Syncthing state and each machine's latest push
_2 tasks . ~370 LOC . medium risk . 10 files_
**Outcome:** `mm status` tells the owner whether Syncthing is connected and in sync and whether this machine has applied each peer's latest push, and `mm diag` flags folder settings that would break sync.
**Supports:** MVP-1, docs/SPEC.md O2.2 and O3.2.
**Done when:** a new `syncthing.py` finds the local Syncthing configuration and reads its REST API read-only with timeouts (connected peers, folder state, and the folder options README lists); mm never writes Syncthing's configuration; pull records each peer's applied manifest time, and status shows each peer's last push and whether it is applied, warning in every mode when something has waited more than 24 h, not counting unplaced or old-form files, which are not pending; a published peer's manifest that has not arrived and an attended waiting count show in status; diag lints the folder options and `.stignore`; a missing or unreachable Syncthing is reported, not an error; tests use a fake REST server; `./bin/check` passes.
_touches: src/mind_meld/syncthing.py (new), src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/statedb.py, tests/test_syncthing.py (new), tests/test_diag.py, tests/test_module_boundaries.py, README.md, docs/invariants/README.md, docs/invariants/sync.md_
_blocked-by: Track 77A, Track 91A, Track 98A_
_out: 93A, 95A_
_read-first: docs/invariants/README.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_syncthing.py tests/test_diag.py tests/test_module_boundaries.py_

_Source: docs/SPEC.md O2.2 and O3.2; docs/archive/store-v2.md §10 folder options (current evidence; Syncthing option semantics verified against docs.syncthing.net on 2026-10-11 for the former Track 81A). Track 90A's Gate notes and `sync.md` exclude unplaced and old-form counts from O3.2 and leave the peer manifest and attended waiting count to this Track. `.stignore` is linted because no real machine gets it from `mm init` at cutover (Track 89A follow-up FU1)._

- **Read-only Syncthing client and diag lint** -- _src/mind_meld/syncthing.py, src/mind_meld/statusflow.py, tests/test_syncthing.py, tests/test_module_boundaries.py, docs/invariants/README.md, ~200 lines._ (M)
- **Each peer's push in status** -- _src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/statedb.py, tests/test_diag.py, README.md, ~150 lines._ (M)

#### Group 95: Factory handover

_Depends on: Group 92_

**This group delivers:** An opt-in `mmm` source carries mmm-factory's durable state between Macs.

##### Track 95A: Add an opt-in mmm source for factory handover
_2 tasks . ~220 LOC . medium risk . 7 files_
**Outcome:** After `mm enable-source mmm` on each Mac, the owner stops the factory on one Mac, pushes, pulls on another Mac and starts it there with the items, plans, cards, settings, passports, handoff notes, briefs and run ledgers in place; locks, watcher state, fields files, worker-ID lists, usage caches and other machine-local factory files stay behind.
**Supports:** docs/SPEC.md "What mm syncs and reports" mmm-factory row and [factory handover](SPEC.md#factory-handover) (owner decisions 2026-10-29).
**Done when:** a default `mmm` source on `~/.mmm` is opt-in like Muse (never active until `mm enable-source mmm` on that machine, or an explicit choice at `mm init` or `mm reconfigure-sources`); it syncs `items/` (each item's `<id>.json`, `.plan.md` and `.card.md`), the four run ledgers (`provenance.jsonl`, `hangs.jsonl`, `records.jsonl`, `picker.jsonl`) and, per project, `manager.toml`, the passport (`manager-passport.txt`), the handoff note (`driver-handoff.md`) and its timestamped archives, the manager's launch cards (`cards/`) and `briefs/`; it never syncs `*.lock`, `manager-watch.*`, `manager-status.json`, `manager-fields.json`, `manager-usage.json`, the worker-ID lists (`manager-docs-workers.txt`, `release-text-workers.txt`), the worker-side `mmm-<id>-card.md` and `mmm-<id>-autoplan-<n>.md` copies, SQLite files, `traces/`, `release/`, `review-compare/` or `picker-jev/`, and `~/.local/share/mmm-factory/` and `~/.config/mmm-factory/` are never sources; `~/.mmm` must be a real directory (a link is refused, as the factory requires); README documents enabling it, the single-Mac rule, that in-flight work does not transfer (finish or ship first), and that item records carry raw worker text to every machine with the source enabled; tests pin the allowlist against a fixture factory home; `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_sourceflow.py, tests/test_mmm_source.py (new), docs/invariants/sync.md, README.md_
_blocked-by: Track 90B, Track 92A, Track 94A_
_out: 93A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-10-29, including mmm-factory manager Ugarit's recommendation the owner accepted (exclude `traces/`, `release/`, `review-compare/` and the worker-ID lists; add `cards/`; `briefs/` per option (a), which the owner confirms at the PR that adds this card and which needs mmm-factory's managers to keep handoff briefs under `~/.mmm/projects/<slug>/briefs/`). Paths confirmed against mmm-factory's `docs/agent-runbook.md` "The factory home" at 93cf70a (v0.5.29) and its file-name constants (`manager.toml`, `manager-passport.txt`, `driver-handoff.md`, `manager-fields.json`, `manager-watch.json`, `manager-watch.owner`, `manager-watch.lock`, `manager-status.json`, `manager-usage.json`, `release-text-workers.txt`). Factory attribution in `retro-fleet` stays deferred (Future)._

_Open questions for planning (mmm-factory's manager, 2026-11-02; record only, the card's scope is unchanged): (1) should syncing `~/.mmm` be an explicit command that refuses unless every factory project is quiet, rather than riding autopull and autopush; (2) projects running on different machines at once versus the single-Mac rule the card documents; (3) whether line union is right for the factory's JSONL ledgers; (4) absolute repo paths inside item JSON when one record moves between a Mac home and a Linux home; (5) Track 91A's `.mind-meld-anchor` generation dotfile in `~/.mmm` and real deletions propagating under `~/.mmm` now that tombstones delete._

- **The mmm source and its allowlist** -- _src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_mmm_source.py, ~150 lines._ (M)
- **Handover docs** -- _README.md, docs/invariants/sync.md, tests/test_sourceflow.py, ~70 lines._ (S)

#### Group 93: Cutover runbook

_Depends on: Group 98, Group 92, Group 95_

**This group delivers:** A runbook and agent docs for moving the fleet onto Syncthing.

##### Track 93A: Write the cutover runbook and the Syncthing documentation
_3 tasks . ~450 LOC . low risk . 11 files_
**Outcome:** The owner has a step-by-step runbook to move the fleet from iCloud Drive to Syncthing and bring both Linux machines in, and every document an agent reads describes the Syncthing store.
**Supports:** MVP-1, docs/SPEC.md O1 to O4 (the cutover, and the owner's observation after it).
**Done when:** README covers installing on macOS and Arch with the bootstrap script, pairing, the folder options mm relies on, how `~/Sync/mind-meld` relates to Syncthing's Default Folder at `~/Sync`, and a cutover runbook: update every machine in any order (there is no fleet order since 1.12.0), except that a machine at 0.14.18 or older upgrades before its next attended `mm push` or `mm gc`, or stays off; stop hooks; move the store folder from iCloud Drive into the Syncthing folder; point `storage.path` at it; put mm's temporary-file line in `.stignore` on every machine, because the cutover never runs `mm init --new-fleet` and Syncthing does not sync that file; bring in each of the two Linux machines one at a time (bootstrap, pair, accept the folder, unlock the keyring before hooks run, `mm init`); retire a dead machine by moving its device entry aside as README's "Retiring a dead machine" says, never by deleting its directories and never while a machine at 1.7.1 or older can run; delete the iCloud folder as the runbook's last step, once every machine has joined and reports in sync; and an optional time-boxed rehearsal between a Linux machine and a Mac with a throwaway folder, temp `HOME` and throwaway passphrase, running `mm init` before and after delivery; the runbook carries Track 91A's waived manual check as an owner step at cutover: README's deletion-preview and recovery recipe on both active Macs, recovering a deleted file on its origin Mac, and `--verbose` flush timing, with what to record; AGENTS.md Key Principles and root SPEC.md's storage sections describe the Syncthing folder; `sync.md` and `init-devices.md` drop iCloud-only rules; the pre-2026-10-19 Track IDs in the routing README, `init-devices.md` and `tests/test_pullhistory.py` are retired; root SPEC.md's `mm sources` steps and `sync.md`'s config-patch row (`_set_grok_host_usage` no longer exists), "all configured sources" sentence and `has_explicit_sources` wording match the code; README and `.github/workflows/ci.yml` stop citing a soak; `pyproject.toml` description and keywords drop iCloud; `tests/test_version.py` fails when `VERSION` and `pyproject.toml` differ; docs/SPEC.md's superseded-history entry for the store design names §13 (cutover) as evidence alongside §7, §10 and §11; `./bin/check` passes.
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
- Group 107 ← {}
- Group 108 ← {107}
- Group 98 ← {107, 108}
- Group 92 ← {98}
- Group 95 ← {92}
- Group 93 ← {98, 92, 95}
- Group 96 ← {93}
```

Track detail per group:

```
Group 107: Cursor account usage
  +-- Track 107A .......... ~L . 4 tasks
Group 108: Memory index after a pull
  +-- Track 108A .......... ~M . 2 tasks
Group 98: Bootstrap script
  +-- Track 98A ........... ~M . 2 tasks
Group 92: Syncthing status
  +-- Track 92A ........... ~L . 2 tasks
Group 95: Factory handover
  +-- Track 95A ........... ~L . 2 tasks
Group 93: Cutover runbook
  +-- Track 93A ........... ~L . 3 tasks
Group 96: Cross-model memory qualification
  +-- Track 96A ........... ~M . 1 task
```

**Total: 7 groups . 7 tracks remaining.**

On 2026-11-04 the owner approved the Cursor account usage reader (Track 107A) and put it first: Track 103A found that only the account session gives per-request Cursor usage, and the owner wants that usage read, not another investigation. The same day the owner approved Track 108A on gstack-extend's manager's evidence that an `mm pull` line-merged a pruned MEMORY.md back to its old index, and put it right after Track 107A and ahead of Track 98A; 108A shares no file with 107A, so `_blocked-by: Track 107A` on 108A and `Track 108A` on 98A hold that order. Tracks 99A and 100A (the Muse reader move and fix) were killed the same day: the refusal that admitted them has not recurred since the 2026-11-02 budget fix (`mm diag --json` on this Mac at 1.12.10: `muse.last_reason` null, 1,086 of 1,097 files cached), and they reopen if Muse's `last_reason` reads `unsupported` again. Track 98A's `_blocked-by:` moves from the killed Track 100A to Track 107A to hold the owner's order; both also edit README. Tracks 98A, 92A, 95A and 93A all edit `README.md` and run in that order: 95A lands before the cutover runbook (93A) so the runbook can cover factory handover, and 98A lands before it so the runbook can use the script. Track 77A stays on Track 92A; the shipped Tracks 87A, 89A, 90A, 90B, 91A, 94A and 97A stay on the cards that named them. Track IDs 92A to 96A and 98A keep the meaning they had before this regeneration, because `docs/invariants/sync.md` and other documents cite them, which is why Group numbers do not follow execution order.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on their own observation on one Mac and both Linux machines after following Group 93's runbook, with O6 to O8 holding, the bootstrap script shipped and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance`. Problems are fixed as they appear. Cross-model memory (O9) is qualified after the cutover by Group 96; its implementation waits in Future until that qualification passes.

---

## Future

Deferred: docs/roadmap-future.md (17 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
