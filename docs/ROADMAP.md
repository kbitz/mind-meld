# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Everything through Track 93A (the cutover runbook, v1.12.20) has shipped and lives in shipped history. The owner removed Cursor support on 2026-11-07 ("cursor -- removed totally, don't worry about old usage, as if we never supported"); Tracks 114A to 116A delete it. The same day the owner made the git-history hostname scrub a condition of 2.0.0 and of making the repository public again (Track 117A). Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for the cutover.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed (spec, 2026-10-18).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`. Invite codes, mesh healing, relays, folder configuration through Syncthing's API and scan requests are refused until the spec's revisit trigger is observed.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line (spec O8, 2026-10-19).** A Track that adds output to push, pull or the auto commands must show it is the outcome, a failure with its fix, data at risk or an actionable warning; everything else goes to `mm status`, `mm diag` or `--verbose`.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-07). No branch, session or open pull request is active for any Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112_

### Group 113: gstack-extend run history

_Depends on: none_

**This group delivers:** Every machine's gstack-extend run history reaches the owner's other machines.

#### Track 113A: Sync gstack-extend's per-host run history
_1 task . ~90 LOC . low risk . 5 files_
_tier: routine_
**Outcome:** With the default `gstack-extend` source, every machine's gstack-extend run history (`~/.gstack-extend/analytics/stage-runs/<host>.jsonl`, one file per machine) reaches the owner's other machines after push and pull, so gstack-extend's readers there see every machine's rows; nothing else under `analytics/` syncs.
**Supports:** docs/SPEC.md "What mm syncs and reports" `gstack, gstack-extend` row ("Project state, analytics and retros"), made true on the owner's 2026-11-06 decision ("yes make it true"); docs/roadmap-future.md "Sync gstack-extend's run history", whose trigger (gstack-extend shipping per-host files) is met by gstack-extend v0.71.0.
**Done when:** the default `gstack-extend` source in `config.py` also syncs `analytics/stage-runs/*.jsonl` (regular files directly in that folder, the same walker rules as the source's other paths: no links, no nested folders unless the code already allows them), and nothing else under `analytics/`: not the historical single-file `analytics/stage-runs.jsonl` (each machine has its own file of that same name, so syncing it would collide), `skill-usage.jsonl`, `workflow-metrics/` or any other file; an existing config whose `gstack-extend` entry is the old default picks up the new path the way earlier default-path additions did (follow the code's existing migration or recommended-include mechanism; do not invent a new one); tests pin the allowlist against a fixture `~/.gstack-extend` holding two machines' per-host files plus every excluded shape, and that two machines' files with different names never conflict; `docs/invariants/sync.md` describes the path and why the historical file stays local; README's source table names it; no machine hostname appears in tests, fixtures or docs (use neutral names such as `mac-a.jsonl`, `linux-a.jsonl`); `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, tests/test_config.py, tests/test_gstack_extend_analytics.py (new), docs/invariants/sync.md, README.md_
_out: 114A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: Karl, 2026-11-06, "yes make it true" (Denver 16's report that SPEC's row says analytics sync while `config.py`'s gstack-extend source includes only `projects/`). Trigger: Denver 17, 2026-11-07, gstack-extend v0.71.0 (PR #213, 7640a38) writes provenance rows to `~/.gstack-extend/analytics/stage-runs/<host>.jsonl`; gstack-extend's `docs/telemetry.md` (installed copy, "Execution provenance") says `<host>` is the configured `host_id` or the sanitized lowercase short nodename, each file is created 0600, a symlink at `stage-runs/` or at the file is not written, historical rows remain in `analytics/stage-runs.jsonl`, and its reader joins `stage-runs.jsonl` plus `stage-runs/*.jsonl`. On one of the owner's machines at 2026-11-07 12:55Z, `analytics/` holds the historical file and `workflow-metrics/`; `stage-runs/` does not exist yet (it is created at the first finish after the upgrade). Card text from the manager's launch card (`~/.mmm/projects/kbitz-mind-meld/cards/track-gse-analytics.md`), placed by the 2026-11-07 regeneration; moving pre-v0.71.0 rows out of the historical file is gstack-extend's job, not this Track's._

- **The per-host analytics path and its tests and docs** -- _the five files above, ~90 lines._ (S)

### Group 114: Cursor commands and consent

_Depends on: Group 113_

**This group delivers:** No mm command knows Cursor: `mm cursor-agent`, the hook command and Cursor usage consent are gone.

#### Track 114A: Remove `mm cursor-agent`, Cursor's hook command and Cursor usage consent
_4 tasks . ~450 LOC . medium risk . 15 files_
**Outcome:** Cursor no longer appears in any mm command: `mm cursor-agent` and the hidden `capture-cursor-usage` hook command are gone, `mm enable-source`, `mm disable-source` and `mm reconfigure-sources` no longer know a `cursor` name, and no push, autopush, recapture, `mm status` or `mm diag` reads or reports Cursor usage.
**Supports:** MVP-1; docs/SPEC.md O7.1 (usage summed from Claude Code, Codex, Grok Build, Muse and Antigravity) and its Cursor non-goal (owner, 2026-11-07: "cursor -- removed totally, don't worry about old usage, as if we never supported").
**Done when:** `cli.py` has no `cursor-agent` or `capture-cursor-usage` command and passes no Cursor consent to capture; `sourceflow.py` drops `_toggle_cursor_usage` and treats `cursor` as an unknown source name; `config.py` drops the Cursor entry of `HOST_USAGE_ONLY_CONSENT`, `cursor_host_usage_enabled` and the `[retro] cursor_host_usage` validation, and a `config.toml` that still holds that key loads without error; `events_tail.py` never selects the Cursor reader; `statusflow.py` drops the Cursor capture rows, hook state and remedies; `tests/test_integration.py`'s `COMMAND_INTENTS62` and `tests/test_module_boundaries.py`'s command and symbol inventories drop the removed names, and the tests of the removed commands and consent go; README drops `mm cursor-agent`, Cursor consent and the hook from its command, source and usage sections and adds an Upgrading note telling the owner to delete mm's `capture-cursor-usage` stop entry from `~/.cursor/hooks.json` on any machine that enabled Cursor usage; AGENTS.md's Project line and command list and root SPEC.md's CLI reference drop Cursor and the two commands; `docs/invariants/` rows for the removed symbols go; the recorded test gap "reconfigure reenrolls disabled Grok" gets its test (accepting Grok in `mm reconfigure-sources` after it was disabled restores the canonical block and consent, removes `grok` from `disabled_sources` and keeps other disabled names); the Cursor reader in `host_usage.py` may stay, unreachable, until Track 115A; `./bin/check --affected` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/sourceflow.py, src/mind_meld/config.py, src/mind_meld/events_tail.py, src/mind_meld/statusflow.py, tests/test_integration.py, tests/test_module_boundaries.py, tests/test_write_guard_inventory.py, tests/test_source_toggle.py, tests/test_config.py, tests/test_diag.py, README.md, AGENTS.md, SPEC.md, docs/invariants/_
_blocked-by: Track 113A_
_out: 115A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/invariants/events-retro.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions relayed by manager Ephesus 9, 2026-11-06 ("we still need to remove support for cursor") and 2026-11-07 ("cursor -- removed totally, don't worry about old usage, as if we never supported"); docs/SPEC.md as amended 2026-11-07. Symbols verified at `fd23f25`: `cli.py` `capture_cursor_usage`, `cursor_agent`, `_cursor_usage_consented`; `sourceflow.py` `_toggle_cursor_usage`; `config.py` `HOST_USAGE_ONLY_CONSENT`, `cursor_host_usage_enabled`. Test gap: docs/TODOS.md `[review]`, Track 109A, drained 2026-11-05 and placed here because this Track touches `src/mind_meld/sourceflow.py` and `tests/test_source_toggle.py`. The core-and-extensions move-first rule does not apply: this Track deletes the Cursor surface rather than changing an adapter that stays._

- **Delete `mm cursor-agent` and `capture-cursor-usage` with their tests** -- _src/mind_meld/cli.py, tests/test_integration.py, tests/test_module_boundaries.py, tests/test_write_guard_inventory.py, ~250 lines (del)._ (S)
- **Remove Cursor usage consent from the source commands, config, capture and status** -- _src/mind_meld/sourceflow.py, src/mind_meld/config.py, src/mind_meld/events_tail.py, src/mind_meld/statusflow.py, src/mind_meld/cli.py, tests/test_source_toggle.py, tests/test_config.py, tests/test_diag.py, ~120 lines._ (M)
- **Test gap: reconfigure re-enrols disabled Grok** -- _tests/test_source_toggle.py, ~40 lines._ (S)
- **README, AGENTS.md, root SPEC.md and invariant docs** -- _README.md, AGENTS.md, SPEC.md, docs/invariants/, ~60 lines._ (S)

### Group 115: Cursor usage reader removal

_Depends on: Group 114_

**This group delivers:** mm holds no Cursor usage reader, so the Grok row counts only Grok Build's own runs.

#### Track 115A: Remove the Cursor usage reader
_4 tasks . ~2,400 LOC . medium risk . 10 files_
**Outcome:** mm no longer contains a Cursor usage reader: the Conductor `runs.ndjson` and SQLite `index.db` readers, the standalone completion spool, the `~/.cursor/hooks.json` editor and Cursor's caches are gone, so new Grok usage comes only from Grok Build and no capture outcome, event, status line or diagnostic names a Cursor reader.
**Supports:** MVP-1; docs/SPEC.md O7.1 and O7.3 (no Cursor source, so no Cursor coverage gap) and its Cursor non-goal (owner, 2026-11-07).
**Done when:** `host_usage.py` keeps the shared reader kit and the Muse and Antigravity readers and drops every Cursor reader, the SQLite adapter, hook editing (`configure_cursor_hook`), completion recording (`record_cursor_usage`, `cursor_cli_model`), the spool and cache paths and the Cursor-only parts of the host-family classifier; `host_registry.py` has no `cursor` entry; `events.py`'s `ACTIVE_HOST_READERS` and `HOST_USAGE_TOKEN_SOURCES` and `events_tail.py`'s `WARMABLE_HOST_READERS` and reader labels drop `cursor`, and the Cursor hold and coverage degradations go from `events_tail.py` and `attemptlog.py`; `errors.py` drops `CURSOR_SQLITE_URL` and README its Cursor SQLite stores section; `statusflow.py` keeps no Cursor-only branch; `retro-fleet` still renders a fleet whose older rows name a `cursor` token source, and those rows are left as they are, with no migration (owner, 2026-11-07); `tests/test_cursor_sqlite.py` and `tests/fixtures/host_sessions/cursor/` are deleted, and other tests drop their Cursor cases, fixtures and `tests/conftest.py` path patches; the recorded test gap "Grok diagnostic unknown-state branches" gets its tests (unreadable Grok config or cache text and the deadline inventory refresh in `mm status` and `mm diag` report unknown, never ready); `docs/invariants/` (events-retro, the module map and the routing rows) describes no Cursor reader; `./bin/check --affected` passes.
_touches: src/mind_meld/host_usage.py, src/mind_meld/host_registry.py, src/mind_meld/events_tail.py, src/mind_meld/events.py, src/mind_meld/attemptlog.py, src/mind_meld/errors.py, src/mind_meld/statusflow.py, tests/, docs/invariants/, README.md_
_blocked-by: Track 114A_
_out: 116A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, tests/fixtures/host_sessions/cursor/CONTRACT.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-11-06 and 2026-11-07 as for Track 114A. Symbols verified at `fd23f25`: `host_usage.py` `read_cursor_usage`, `_read_cursor_sqlite`, `configure_cursor_hook`, `record_cursor_usage`, `cursor_cli_model`, `CURSOR_HOOK_CONFIG_PATH`; `host_registry.py` `HOST_READER_DIAGS["cursor"]`; `events.py` `ACTIVE_HOST_READERS`, `HOST_USAGE_TOKEN_SOURCES`; `events_tail.py` `_CURSOR_HOLD_DEGRADATION`, `_CURSOR_COVERAGE_DEGRADATION`; `errors.py` `CURSOR_SQLITE_URL`. `statedb.CursorRow` and the git-capture `repo_cursors` are pull and walk cursors, not Cursor, and stay. Test gap: docs/TODOS.md `[review]`, Track 110A, drained 2026-11-05 and placed here because this Track touches `src/mind_meld/statusflow.py` and `tests/` (which holds `tests/test_diag.py`). The `tests/` entry is a folder because the deleted symbols reach test files the code does not list in advance. The core-and-extensions move-first rule does not apply: the Cursor adapter is deleted, not changed._

- **Delete the Cursor reader, its SQLite adapter, hook editor and spool with their tests and fixtures** -- _src/mind_meld/host_usage.py, tests/test_host_usage.py, tests/test_cursor_sqlite.py, tests/fixtures/host_sessions/cursor/, tests/conftest.py, ~2,100 lines (del)._ (S)
- **Remove Cursor from the reader registry, capture tail, event constants, attempt log, errors and status** -- _src/mind_meld/host_registry.py, src/mind_meld/events_tail.py, src/mind_meld/events.py, src/mind_meld/attemptlog.py, src/mind_meld/errors.py, src/mind_meld/statusflow.py, tests/, ~150 lines._ (M)
- **Test gap: Grok diagnostic unknown-state branches** -- _tests/test_diag.py, ~60 lines._ (S)
- **Invariant docs and README** -- _docs/invariants/, README.md, ~100 lines._ (S)

### Group 116: Cursor traces in discovery, pricing and retro

_Depends on: Group 115_

**This group delivers:** Nothing else in mm reads Cursor's files or names Cursor.

#### Track 116A: Remove Cursor from git-root discovery, pricing provenance and retro-fleet
_4 tasks . ~450 LOC . low risk . 15 files_
**Outcome:** Nothing else in mm reads Cursor's files or names Cursor: git-root discovery no longer opens `~/.cursor/acp-sessions` or Conductor's Cursor SDK store, Grok prices cite xAI rather than Cursor, and `retro-fleet` and its SKILL.md no longer mention Cursor or print the Cursor ACP gap note.
**Supports:** MVP-1; docs/SPEC.md O7.1 and O7.3 and its Cursor non-goal (owner, 2026-11-07).
**Done when:** `rootprobes.py` has no Cursor prober (the `_cursor_*` helpers, `_CONDUCTOR_STORE` and its `PROBERS` row), and `fsutil.py`'s pytest account-state guard and `tests/conftest.py` drop `.cursor`; `token_usage.py` drops `PRICING_CURSOR_LAST_UPDATED` and its Cursor citations, the Grok 4.7 cards that Grok Build's ids use keep their rates with xAI provenance, and a card only Cursor ids used goes; `aggregator.py` drops `CURSOR_ACP_GAP_NOTE`, `_cursor_acp_gap_notes`, the Cursor pricing-provenance field and its Cursor comments; the retro SKILL.md drops Cursor; the recorded test gap "complete Grok row with cache writes suppresses ceiling" gets its test (positive `cache_create`, `incomplete=False`, no override or a bundled-equal one, the model detail kept and `at most` absent), replacing the Cursor cache-write case; `docs/invariants/`, `docs/designs/host-parity.md` (its Cursor material marked historical) and README's pricing provenance match; outside CHANGELOG.md, `docs/archive/`, `docs/roadmap-shipped.md`, docs/TODOS.md drain records and docs/PROGRESS.md, `git grep -n -i -w cursor` finds only the unrelated pull, manifest and walk cursors; `./bin/check --affected` passes.
_touches: src/mind_meld/rootprobes.py, src/mind_meld/fsutil.py, src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_root_probers.py, tests/conftest.py, tests/test_token_usage.py, tests/test_retro_fleet_aggregator.py, tests/test_retro_usage_presentation.py, tests/test_grok_usage.py, tests/test_muse_usage.py, docs/invariants/, docs/designs/host-parity.md, README.md_
_blocked-by: Track 115A_
_out: 117A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, docs/designs/host-parity.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-11-06 and 2026-11-07 as for Track 114A. Symbols verified at `fd23f25`: `rootprobes.py` `_cursor_acp_root`, `_cursor_ndjson`, `_CONDUCTOR_STORE`, `RootProber("cursor", ...)`; `fsutil.py`'s guarded `.cursor` root; `token_usage.py` `PRICING_CURSOR_LAST_UPDATED`, `_builtin_verified_date`; `aggregator.py` `CURSOR_ACP_GAP_NOTE`, `_cursor_acp_gap_notes`, `cursor_verified`. Test gap: docs/TODOS.md `[review]`, Track 111B, drained 2026-11-06 and placed here because this Track touches `src/mind_meld/skills/retro_fleet/aggregator.py` and `tests/test_retro_usage_presentation.py`; the only cache-write cases it names are Cursor's and a partial Grok case._

- **Delete the Cursor git-root prober and its tests** -- _src/mind_meld/rootprobes.py, src/mind_meld/fsutil.py, tests/test_root_probers.py, tests/conftest.py, ~300 lines (del)._ (S)
- **Re-source Grok pricing provenance to xAI and remove Cursor from retro-fleet** -- _src/mind_meld/token_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_token_usage.py, tests/test_retro_fleet_aggregator.py, tests/test_grok_usage.py, tests/test_muse_usage.py, ~120 lines._ (M)
- **Test gap: complete Grok row with cache writes suppresses ceiling** -- _tests/test_retro_usage_presentation.py, ~50 lines._ (S)
- **Invariant docs, host-parity design and README** -- _docs/invariants/, docs/designs/host-parity.md, README.md, ~60 lines._ (S)

### Group 117: Git-history hostname scrub

_Depends on: Group 113, Group 116_

**This group delivers:** A git history free of the owner's machine hostnames, the last carded condition for 2.0.0 and for making the repository public again.

#### Track 117A: Rewrite the git history to remove the owner's machine hostnames (owner-gated)
_3 tasks . ~30 LOC . high risk . 1 file_
**Outcome:** No commit, tag or other reachable ref in the repository contains any of the owner's machine hostnames or a fragment of one, so 2.0.0 can be cut and the repository can be made public again; installs keep upgrading from the same tag names.
**Supports:** MVP-1; docs/SPEC.md's 2.0.0 release line and its hostname constraint (owner, 2026-11-07: "that scrub is required before we go public again and hit 2.0 fyi").
**Done when:** the rewrite runs on a fresh mirror clone, never on a working checkout, with `git filter-repo` or an equivalent tool driven by a replacement list kept outside the repository and never committed, printed, or pasted into the PR or a log; before any push, a search of every blob, commit message, tag message and ref name in the rewritten mirror for each listed name prints nothing; every release tag, `latest` included, points at its rewritten commit under the same name; **the owner gives an explicit go in the session before any push**, lifts `main`'s force-push protection and restores it afterwards, and runs or approves the force-push of `main` and the tags; afterwards a search of a fresh clone prints nothing, and `mm update` on one machine reinstalls from `@latest` at the same version; README's Upgrading section tells every other clone and worktree to re-clone rather than rebase, and says pipx installs need no action; the refs GitHub keeps outside the owner's control (pull-request refs and cached views of old commits) are listed for the owner, who asks GitHub Support to purge them or accepts them before the repository goes public; no hostname appears in the PR, a commit message, README or any record the Track keeps; the Track does not make the repository public; `./bin/check tests/test_docs_routing.py` passes.
_touches: README.md_
_blocked-by: Track 113A, Track 116A_
_read-first: docs/SPEC.md, docs/roadmap-shipped.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_docs_routing.py_

_Source: owner, 2026-11-06 ("we will need to scrub the git history at some point, don't have to do it now though") and 2026-11-07 ("that scrub is required before we go public again and hit 2.0 fyi"), relayed by manager Ephesus 9; docs/SPEC.md's 2.0.0 release line and hostname constraint as amended 2026-11-07; Track 112A's receipt (the current tree is scrubbed; past commits are not). Promoted from docs/roadmap-future.md "Rewrite git history to remove the owner's machine hostnames". It runs after every other carded Track so no open branch has to be rebased onto the rewritten history._

- **Rewrite and verify on a mirror clone** -- _a fresh mirror outside every checkout; the replacement list stays outside the repository._ (M)
- **Owner go, force-push of `main` and the tags, and the post-push checks** -- _the owner's go and push; one machine's `mm update`._ (S)
- **README Upgrading note for existing clones and worktrees** -- _README.md, ~20 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 113 ← {}
- Group 114 ← {113}
- Group 115 ← {114}
- Group 116 ← {115}
- Group 117 ← {113, 116}
```

Track detail per group:

```
Group 113: gstack-extend run history
  +-- Track 113A .......... ~S . 1 task
Group 114: Cursor commands and consent
  +-- Track 114A .......... ~L . 4 tasks
Group 115: Cursor usage reader removal
  +-- Track 115A .......... ~L . 4 tasks
Group 116: Cursor traces in discovery, pricing and retro
  +-- Track 116A .......... ~L . 4 tasks
Group 117: Git-history hostname scrub
  +-- Track 117A .......... ~M . 3 tasks
```

**Total: 5 groups . 5 tracks remaining.**

Track 93A (the cutover runbook) shipped in v1.12.20 and Group 93 is retired. Track 96A (cross-model memory qualification) moved to Future: docs/SPEC.md plans O9 after 2.0 and says it starts after the cutover, which is the owner's own step and has not run. Track 113A keeps the ID its launch card gave it; Tracks 114A to 117A take the next free numbers, and IDs 95A and 96A stay unused because Future entries cite them.

The 2.0.0 cut is not a Track. Carded work before it: Tracks 113A to 117A, ending with the history rewrite, which needs the owner's explicit go and their force-push. The owner's own steps: confirm that the runbook's roles (the store Mac, another Mac, the first and second Linux machines) match the real machines in `mm devices`; run the README cutover runbook, including its step 7 owner checks; run `retro-fleet` on the fleet for O7.4; and confirm O1 to O4 on their own observation on one Mac and both Linux machines, with O6 to O8 holding, recorded in PROGRESS.md `## Acceptance`. Making the repository public again is a separate owner decision that waits for Track 117A. Problems found during the cutover are fixed as they appear.

---

## Future

Deferred: docs/roadmap-future.md (26 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
