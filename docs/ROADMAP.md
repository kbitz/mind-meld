# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Cursor, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support, removed in v1.11.8, returns at parity with Claude Code and Codex by the owner's 2026-11-05 decision (Groups 109 to 111). Groups 82–91, 94, 97 and 101 (resumable capture, affected-test selection, the status, Codex, pull, init and source-command moves, one capture budget, Syncthing-tolerant reads, explicit fleet creation and join, machine-local gstack files, shared Claude project memory, recapture's own walk budget, fast-forward pulls and propagated deletions, an honest remedy for unreadable host records and a fair attended usage sweep) shipped in v1.11.9–v1.12.5, and Tracks 102A to 106A (the Codex warm-read memo, discovery from every agent, published identities, finished discovery and the named Cursor ACP gap) in v1.12.6–v1.12.10, and Tracks 108A, 98A and 92A (the memory index tidy, the bootstrap script and Syncthing state in status) in v1.12.11–v1.12.13; all live in shipped history. Grok models used through Cursor stay in the Grok row beside Grok Build's. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); Group 96 qualifies it after the cutover.

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

None declared (2026-11-05). Tracks 108A (v1.12.11), 98A (v1.12.12) and 92A (v1.12.13) shipped and are recorded in shipped history; Track 107A (the Cursor account usage reader) was killed the same day.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108_

### Phase 5: mm 2.0 on Syncthing

**End-state:** The owner's Macs and two Linux machines sync over one Syncthing folder that carries the 1.x store, with fast-forward pulls, propagated deletions, every consented host's usage read within the attended budget, Grok Build at parity with Claude Code and Codex, a memory index that no longer resurrects deleted pointers, Syncthing state in status, a bootstrap script and a cutover runbook; the factory can hand over between Macs; docs/SPEC.md O1 to O4 are accepted on the owner's own observation after following the runbook, and released as 2.0.0.
**Groups:** 109, 110, 111, 95, 93

#### Group 109: Grok source ∥ Grok pricing

_Depends on: none_

**This group delivers:** `mm enable-source grok` syncs Grok Build's skills, commands and rules again and becomes the Grok usage consent, and the Grok row prices Cursor's Grok 4.7 effort and Fast runs and Grok Build's model.

##### Track 109A: Bring back the scoped grok source and its consent
_3 tasks . ~260 LOC . medium risk . 11 files_
**Outcome:** On a machine where the owner runs `mm enable-source grok`, mm syncs Grok Build's `skills/`, `commands/` and `rules/` under `~/.grok` like Codex's customizations, and that enabled source is the consent the Grok Build usage reader (Track 110A) checks; `mm disable-source grok` stops both.
**Supports:** MVP-1, docs/SPEC.md "What mm syncs and reports" Grok Build row (sync column) and O1, after the owner's 2026-11-05 parity decision; prerequisite for Track 110A's consent.
**Done when:** `grok` leaves `config.RETIRED_SOURCE_NAMES` (`opencode` stays) and is a default source of `type: "grok"` on `~/.grok` whose walker is hardcoded to `skills/`, `commands/` and `rules/`, refusing child links, hard links and every other path under `~/.grok` (sessions, `memory-v2/`, `config.toml`, `auth.json`, `trusted_folders.toml`, `plugins/`, `hooks/`, logs) as Track 22B's walker did; conflict discovery in `resolveflow` is limited to the same directories; the source never activates on its own: only `mm enable-source grok`, or an explicit choice at `mm init` or `mm reconfigure-sources`, turns it on, and enabling removes `grok` from `disabled_sources`, where `mm migrate-config` put it on machines that ran it after v1.11.8; `core._migrate_config_core` no longer removes `grok` blocks, and migrate-config's docstring in `cli.py` says so (no behavior in `cli.py` changes); a config that still holds a pre-v1.11.8 `grok` block or the `[retro] grok_host_usage` bit loads without error and is treated as enabled, with no new compatibility path beyond that; a public helper reports whether Grok usage is consented (the source is enabled), for Track 110A to read; tests pin the allowlist (a nested `skills/<link>` to `sessions/`, `config.toml`, `auth.json`, `memory-v2/` and `plugins/` upload zero files), enable and disable round-trips including the `disabled_sources` case, and a config carrying the old block; `docs/invariants/sync.md` documents the restored source (and its config-patch row matches the code); README's sources section lists `grok`; `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, src/mind_meld/core.py, src/mind_meld/sourceflow.py, src/mind_meld/manifest.py, src/mind_meld/resolveflow.py, src/mind_meld/cli.py, tests/test_config.py, tests/test_sourceflow.py, tests/test_manifest.py, docs/invariants/sync.md, README.md_
_out: 110A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/designs/host-parity.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Karl, 2026-11-05, "go, it needs to bring grok to parity w/ the other models"; docs/SPEC.md table as amended 2026-11-05; docs/designs/host-parity.md "Grok Build parity (2026-11-05)" and Plan B (the allowlist Track 22B shipped). Re-checked at 027840b: `config.RETIRED_SOURCE_NAMES` is `{"grok", "opencode"}`; `config.py` refuses `type: 'grok'` ("no longer supported"); `core._migrate_config_core` adds retired names to `disabled_sources`; the walker and resolveflow branches were removed by fb0e12d (PR #226, v1.11.8), whose parent holds the previous implementation. `~/.grok/skills`, `commands/` and `rules/` do not exist on this Mac (Grok reads `~/.claude/skills`), so the source carries nothing here today; parity is the owner's decision, not observed demand._

- **Restore the source, walker and consent helper** -- _src/mind_meld/config.py, src/mind_meld/sourceflow.py, src/mind_meld/manifest.py, src/mind_meld/resolveflow.py, tests/test_config.py, tests/test_sourceflow.py, tests/test_manifest.py, ~200 lines._ (M)
- **Un-retire grok in migrate-config** -- _src/mind_meld/core.py, src/mind_meld/cli.py, tests/test_config.py, ~30 lines._ (S)
- **Docs** -- _docs/invariants/sync.md, README.md, ~30 lines._ (S)

##### Track 109B: Price Grok 4.7 effort and Fast variants and Grok Build's model id
_3 tasks . ~180 LOC . medium risk . 5 files_
**Outcome:** `retro-fleet` prices Cursor's `grok-4.7-high-fast`, `grok-4.7-xhigh` and `grok-4.7-xhigh-fast` runs, Fast included, and Grok Build's `grok-4.7-build` runs, so the Grok row shows a cost instead of a floor and push stops logging "unknown model in pricing" for them.
**Supports:** MVP-1, docs/SPEC.md O7.1 (tokens and cost for each family) and O7.3 (no silent gaps).
**Done when:** the first task records in `tests/fixtures/host_sessions/cursor/CONTRACT.md` a census of the Grok 4.7 ids Cursor runs now write on this machine (ids, Fast flag, counts and date; no prompts), updating its "Never observed: fast=true" line, and finds which reader path hands these ids to pricing unnormalized (host-usage normalization already maps `grok-4.7-(low|medium|high|xhigh)(-fast)` in one path); the PR verifies on the day Cursor's and xAI's published Grok 4.7 rates, including Fast (recorded in 2026-09 as 2x base for standard and 3x for long context) and the model behind Grok Build, and records source and date beside each rate card; Fast tokens are priced from that published rate, ending the "deliberately unpriced" Fast policy of 1.2.0, with the PR stating whether Fast is a pseudo-id card or a parameter on the base card; `grok-4.7-build` gets a card or an alias as `grok-4.6-build` has; an id with no published rate on the day stops the Track and goes to the owner rather than getting a guessed price; tests pin each id's card or alias, the Fast multipliers and that no "unknown model in pricing" notice is logged for these ids; `./bin/check --affected` passes.
_touches: src/mind_meld/token_usage.py, src/mind_meld/host_usage.py, tests/test_token_usage.py, tests/test_host_usage.py, tests/fixtures/host_sessions/cursor/CONTRACT.md_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, tests/fixtures/host_sessions/cursor/CONTRACT.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: factory manager's brief, 2026-11-05: Cursor's `grok-4.7-high-fast`, `grok-4.7-xhigh` and `grok-4.7-xhigh-fast` log "unknown model in pricing" and the Grok row shows only a floor. Promotes the Future entry "Price Cursor Grok Fast after a real census" (`docs/roadmap-future.md`, [plan-ceo-review], drained 2026-09-30), whose trigger, a real observed Fast run, is met by those log lines. Re-checked at 027840b: the notice is in `token_usage.py` ("mm: notice: unknown model in pricing"); `token_usage` has cards for `grok-4.7` and `grok-4.6` and the alias `grok-4.6-build` → `grok-4.6`; `host_usage.py` maps `grok-4.7-(low|medium|high|xhigh)(-fast)?` to `grok-4.7` or `grok-4.7-fast` in one path; CHANGELOG 1.2.0 keeps Fast tokens unpriced. `grok-4.7-build` comes from the manager's 2026-11-05 Grok Build probe._

- **Census and the unnormalized path** -- _tests/fixtures/host_sessions/cursor/CONTRACT.md, src/mind_meld/host_usage.py, tests/test_host_usage.py, ~60 lines._ (S)
- **Verify the published rates** -- _src/mind_meld/token_usage.py, ~30 lines._ (S)
- **Rate cards, Fast pricing and aliases** -- _src/mind_meld/token_usage.py, tests/test_token_usage.py, ~90 lines._ (M)

#### Group 110: Grok Build usage

_Depends on: Group 109_

**This group delivers:** `retro-fleet` counts Grok Build usage, Paseo runs included, in the Grok row, and status and diag report its reader.

##### Track 110A: Read Grok Build usage into retro-fleet, Paseo runs included
_5 tasks . ~620 LOC . high risk . 13 files_
**Outcome:** With the grok source enabled on a machine, `retro-fleet` counts that machine's Grok Build tokens and list-rate cost in the Grok row, including runs driven through Paseo's `grok` provider, each turn once; `mm status` and `mm diag` report the reader like Codex's, and empty sessions cost nothing.
**Supports:** MVP-1, docs/SPEC.md O7.1 (Grok Build in the fleet usage sources) and O7.3 (usage mm cannot read is named, never a silent zero); the complete-usage checkpoint and the 2.0.0 release line need O7.
**Done when:** the first task records in `tests/fixtures/host_sessions/grok/CONTRACT.md` a census of real Grok Build sessions on the machine the Track runs on, or on the Mac Studio when that machine holds none (Grok Build version, date, session count, empty-session count, which files exist, key names and counts only, never prompts or paths), and from it chooses `updates.jsonl` (`turn_completed` records) or `usage.json` (per-turn rows with `endedAt` and per-model `modelUsage`) as the one usage source, states why, and establishes whether the counters are per-turn or session-cumulative and whether `reasoning` is inside `output`; a test pins that choice and both semantics against a sanitized fixture; the reader lives in `src/mind_meld/hosts/grok.py` behind the adapter contract (`tests/test_host_adapter_contract.py`) and leaves `host_usage.py` unchanged (core-and-extensions constraint); it runs only when Track 109A's helper reports the grok source enabled, and never opens `~/.grok` otherwise; it reads only the chosen file, never chat history or other session files, and publishes only the existing encrypted host-usage aggregate (family, model, UTC day, token counters), never `costUsdTicks`, paths or session identifiers; a session with no usage (Paseo leaves two-line `updates.jsonl` files) is a cached zero contribution, not a refusal, and is not re-read on later pushes while unchanged; each turn counts once across resumes, replays and repeated pushes; it fits the attended capture budget with resumable progress and never warms on autopush; a malformed or changed format fails closed for this reader alone, with one line naming what broke and the fix in `mm status` and `mm diag` (O8), publishing no partial total and leaving other readers and the push's exit code alone; `retro-fleet` adds Grok Build tokens to the Grok row next to Cursor's Grok runs, priced from `token_usage` rate cards; two recorded status test gaps are closed in the same status tests (below); README and `docs/invariants/events-retro.md` describe the reader and its consent; the PR records a live run on the machine it ran on (enable the source, one attended push, `mm diag --json` showing the reader complete, `mm retro-fleet 7d` showing Grok Build use); `./bin/check --affected` passes.
_touches: src/mind_meld/hosts/grok.py (new), src/mind_meld/host_registry.py, src/mind_meld/events_tail.py, src/mind_meld/statusflow.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_grok_usage.py (new), tests/test_host_adapter_contract.py, tests/test_module_boundaries.py, tests/test_diag.py, tests/fixtures/host_sessions/grok/ (new), docs/invariants/events-retro.md, docs/invariants/README.md, README.md_
_blocked-by: Track 109A_
_out: 111A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, docs/designs/host-parity.md, docs/archive/grok-build-usage-reader.md (only for the retired reader's deduplication and counter rules)_
_session: fresh · effort: high · verify: ./bin/check --affected_

_Source: Karl, 2026-11-05, "go, it needs to bring grok to parity w/ the other models", and 2026-11-04, "what i'll probably do is switch from cursor-agent to grok build (uses same sub)". Evidence: the factory manager's probe on this Mac, 2026-11-05, Grok Build 1.0.70: one prompt through Paseo's `grok` provider (ACP) wrote under `~/.grok/sessions/<cwd>/<session>/` an `updates.jsonl` with one `sessionUpdate: "turn_completed"` record carrying usage and a `usage.json` with session totals, per-model `modelUsage` (`grok-4.7-build`) and per-turn rows with `endedAt` (input, output, cachedRead, cacheCreation and reasoning tokens, modelCalls, costUsdTicks); Paseo also created 26 empty sessions (two-line `updates.jsonl`, no usage). `~/.grok/sessions` holds 56 cwd directories on this Mac (2026-11-05). Re-checked at 027840b: `hosts/` holds only `codex.py`; `host_registry.READERS` has codex, cursor, muse and agy; the `grok` family and Cursor's Grok pricing remain in `host_usage.host_family` and `token_usage`. The retired reader (removed by fb0e12d, v1.11.8) read `updates.jsonl` and treated counters as inclusive. Test gaps: "Unknown Syncthing status counters" from `docs/TODOS.md` (`[review:files=src/mind_meld/statusflow.py|tests/test_diag.py]`, PR #268 at `b1b05bb`), and "ACP notice after successful capture or a reader blocker" (Track 103A's review, PR #263), which went to Track 107A at `3d1e624` and returns here with 107A killed; at 027840b `statusflow.py` still prints "Cursor runs through ACP front-ends such as Paseo are not counted"._

- **Census and source choice** -- _tests/fixtures/host_sessions/grok/CONTRACT.md, ~60 lines._ (S)
- **The reader behind the adapter contract** -- _src/mind_meld/hosts/grok.py, src/mind_meld/host_registry.py, src/mind_meld/events_tail.py, tests/test_grok_usage.py, tests/test_host_adapter_contract.py, tests/test_module_boundaries.py, ~380 lines._ (M)
- **Status, diag and the Grok row** -- _src/mind_meld/statusflow.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_diag.py, ~100 lines._ (M)
- **Status test gaps** -- re-verify first, then assert (1) that unknown `needFiles` or `pullErrors` beside an idle or syncing Syncthing folder state keep the actual state and invent neither zero queued files nor a healthy status, and (2) the Cursor ACP gap notice beside a successful scan and beside a reader blocker. _tests/test_diag.py, ~40 lines._ (S)
- **Docs and the live run** -- _docs/invariants/events-retro.md, docs/invariants/README.md, README.md, ~60 lines._ (S)

#### Group 111: Grok Build memory

_Depends on: Group 110_

**This group delivers:** Grok Build's global and per-repository memory is the same on every machine.

##### Track 111A: Sync Grok Build memory between machines
_3 tasks . ~300 LOC . high risk . 9 files_
**Outcome:** With the grok source enabled, what Grok Build remembers on one machine (its global memory and each repository's memory) is there on the owner's other machines after push and pull, in the same repository's memory wherever each machine keeps its checkout, while Grok's generated index and databases stay local.
**Supports:** MVP-1, docs/SPEC.md "What mm syncs and reports" Grok Build row (memory) and O1.2 (per-repository memory lands in the same project on macOS and Linux).
**Done when:** the first task establishes from Grok Build's documentation and a live check in a temporary `GROK_HOME` (never the owner's real store), with the version and date recorded in `docs/designs/host-parity.md`, how `memory-v2/workspaces/<project>-<hash>/` is named, and whether two checkouts of one repository at different paths, or on two machines, get the same name; if names match across machines the scopes sync under their own names, and if not, workspace memory is published under the repository's identity as Claude project memory is (git remote, else home-relative path) and mapped back to the local workspace directory, and if neither is possible the Track stops and asks the owner instead of shipping global memory alone; the walker adds, for `memory-v2/global/` and each workspace scope, only `topics/` and `observations/_inbox/` Markdown, never `MEMORY.md`, SQLite files, the search index or `archive/`; a peer's new observation reaches the other machine's inbox and a deletion propagates, so a Dream on the active machine archives it everywhere; tests pin the allowlist against a fixture store and the workspace mapping with two checkout paths; `docs/invariants/sync.md` and README describe Grok memory sync and that Grok keeps memory off by default; `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/projectid.py, src/mind_meld/pullflow.py, tests/test_grok_memory.py (new), tests/test_manifest.py, tests/test_projectid.py, docs/invariants/sync.md, docs/designs/host-parity.md, README.md_
_blocked-by: Track 110A_
_out: 95A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/designs/host-parity.md_
_session: fresh · effort: high · verify: ./bin/check --affected_

_Source: Karl, 2026-11-05, "go, it needs to bring grok to parity w/ the other models": Claude Code memory syncs, so Grok Build memory does (docs/SPEC.md table and O1.2 as amended 2026-11-05). Evidence: Grok Build 1.0.70's `docs/user-guide/13-memory.md` (memory lives in `~/.grok/memory-v2/global/` and `workspaces/<project>-<hash>/`, each with `topics/`, `observations/_inbox/`, a generated `MEMORY.md` and Grok's own SQLite and index files; "Clones and worktrees of the same repository share one workspace scope"; memory is off by default); this Mac holds a populated `memory-v2/` with per-worktree workspace names such as `mmm-05c9f851-98fbfdda` beside `mmm-factory-cd72c70b`. A quick check of SHA-256 over the checkout path, remote URL and root commit did not reproduce the 8-hex `<hash>` for `mmm-factory` or `mind-meld`, so the naming is unknown. Re-checked at 027840b: Claude project memory is mapped by `projectid.py` since 1.12.0._

- **Workspace naming check** -- _docs/designs/host-parity.md, ~30 lines._ (S)
- **Memory walker and workspace mapping** -- _src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/projectid.py, src/mind_meld/pullflow.py, tests/test_grok_memory.py, tests/test_manifest.py, tests/test_projectid.py, ~240 lines._ (M)
- **Docs** -- _docs/invariants/sync.md, README.md, ~30 lines._ (S)

#### Group 95: Factory handover

_Depends on: Group 111_

**This group delivers:** An opt-in `mmm` source carries mmm-factory's durable state between Macs.

##### Track 95A: Add an opt-in mmm source for factory handover
_2 tasks . ~220 LOC . medium risk . 7 files_
**Outcome:** After `mm enable-source mmm` on each Mac, the owner stops the factory on one Mac, pushes, pulls on another Mac and starts it there with the items, plans, cards, settings, passports, handoff notes, briefs and run ledgers in place; locks, watcher state, fields files, worker-ID lists, usage caches and other machine-local factory files stay behind.
**Supports:** docs/SPEC.md "What mm syncs and reports" mmm-factory row and [factory handover](SPEC.md#factory-handover) (owner decisions 2026-10-29).
**Done when:** a default `mmm` source on `~/.mmm` is opt-in like Muse (never active until `mm enable-source mmm` on that machine, or an explicit choice at `mm init` or `mm reconfigure-sources`); it syncs `items/` (each item's `<id>.json`, `.plan.md` and `.card.md`), the four run ledgers (`provenance.jsonl`, `hangs.jsonl`, `records.jsonl`, `picker.jsonl`) and, per project, `manager.toml`, the passport (`manager-passport.txt`), the handoff note (`driver-handoff.md`) and its timestamped archives, the manager's launch cards (`cards/`) and `briefs/`; it never syncs `*.lock`, `manager-watch.*`, `manager-status.json`, `manager-fields.json`, `manager-usage.json`, the worker-ID lists (`manager-docs-workers.txt`, `release-text-workers.txt`), the worker-side `mmm-<id>-card.md` and `mmm-<id>-autoplan-<n>.md` copies, SQLite files, `traces/`, `release/`, `review-compare/` or `picker-jev/`, and `~/.local/share/mmm-factory/` and `~/.config/mmm-factory/` are never sources; `~/.mmm` must be a real directory (a link is refused, as the factory requires); README documents enabling it, the single-Mac rule, that in-flight work does not transfer (finish or ship first), and that item records carry raw worker text to every machine with the source enabled; tests pin the allowlist against a fixture factory home; `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_sourceflow.py, tests/test_mmm_source.py (new), docs/invariants/sync.md, README.md_
_blocked-by: Track 90B, Track 92A, Track 94A, Track 111A_
_out: 93A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-10-29, including mmm-factory manager Ugarit's recommendation the owner accepted (exclude `traces/`, `release/`, `review-compare/` and the worker-ID lists; add `cards/`; `briefs/` per option (a), which the owner confirms at the PR that adds this card and which needs mmm-factory's managers to keep handoff briefs under `~/.mmm/projects/<slug>/briefs/`). Paths confirmed against mmm-factory's `docs/agent-runbook.md` "The factory home" at 93cf70a (v0.5.29) and its file-name constants (`manager.toml`, `manager-passport.txt`, `driver-handoff.md`, `manager-fields.json`, `manager-watch.json`, `manager-watch.owner`, `manager-watch.lock`, `manager-status.json`, `manager-usage.json`, `release-text-workers.txt`). Factory attribution in `retro-fleet` stays deferred (Future)._

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
- Group 109 ← {}
- Group 110 ← {109}
- Group 111 ← {110}
- Group 95 ← {111}
- Group 93 ← {95}
- Group 96 ← {93}
```

Track detail per group:

```
Group 109: Grok source ∥ Grok pricing
  +-- Track 109A .......... ~M . 3 tasks
  +-- Track 109B .......... ~M . 3 tasks
Group 110: Grok Build usage
  +-- Track 110A .......... ~L . 5 tasks
Group 111: Grok Build memory
  +-- Track 111A .......... ~M . 3 tasks
Group 95: Factory handover
  +-- Track 95A ........... ~L . 2 tasks
Group 93: Cutover runbook
  +-- Track 93A ........... ~L . 3 tasks
Group 96: Cross-model memory qualification
  +-- Track 96A ........... ~M . 1 task
```

**Total: 6 groups . 7 tracks remaining.**

On 2026-11-05 the owner restored Grok Build at parity with Claude Code and Codex ("go, it needs to bring grok to parity w/ the other models"), after saying on 2026-11-04 that they would switch from cursor-agent to Grok Build on the same subscription, and the Grok Tracks lead the plan. They are expected to start on the owner's Linux machine after the factory moves there. Track 109A comes first because the enabled `grok` source is the usage reader's consent; Track 110A's `_blocked-by: Track 109A` holds that, and Track 111A follows 110A so usage, the owner's main need, lands before memory sync. Track 109B (pricing) is disjoint and runs beside 109A. Track 107A (the Cursor account usage reader) was killed on 2026-11-05: after the account moved to the X unified subscription around 2026-10-21, Cursor exposes per-request usage only through the account's billing export, and the owner chose local-only capture for consistency with the other providers ("A, but we don't need the separate line"); it reopens when Cursor's ACP sends per-call usage, and docs/SPEC.md's non-goal for Cursor via ACP front-ends carries that trigger. Track 95A now also waits on Track 111A (both edit `config.py`); Tracks 109A, 110A, 111A, 95A and 93A all edit `README.md` and run in that order, so the cutover runbook (93A) can cover factory handover and Grok. Track IDs 93A, 95A and 96A keep the meaning they had before this regeneration, because `docs/invariants/sync.md` and other documents cite them, which is why Group numbers do not follow execution order.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on their own observation on one Mac and both Linux machines after following Group 93's runbook, with O6 to O8 holding, the bootstrap script shipped and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance`. Problems are fixed as they appear. Cross-model memory (O9) is qualified after the cutover by Group 96; its implementation waits in Future until that qualification passes.

---

## Future

Deferred: docs/roadmap-future.md (29 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
