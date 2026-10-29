# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Cursor, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support was removed in v1.11.8; Groups 82–87 (resumable capture, affected-test selection, the status, Codex and pull module moves, one capture budget, Syncthing-tolerant reads) shipped in v1.11.9–v1.11.17 and live in shipped history; Grok models used through Cursor stay in the Grok row. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); Group 96 qualifies it after the cutover.

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

Declared active (factory, 2026-10-29): Track 88A is between review and ship as [PR #240](https://github.com/kbitz/mind-meld/pull/240) (branch `mmm/4c490f01`). Its ID and Group are pinned; only its downstream references (`_out:`, and the init-changing Track now numbered 90A) follow this run's renumbering.

#### Group 88: Init module

_Depends on: none_

**This group delivers:** `mm init` moves into its own module.

##### Track 88A: Move init out of cli.py
_2 tasks . ~150 LOC . low risk . 7 files_
**Outcome:** `mm init` lives in its own module behind a contract test, so Track 90A changes and reviews that module instead of `cli.py`; nothing the owner sees changes.
**Supports:** MVP-1 delivery under docs/SPEC.md Constraints, "Core and extensions"; prerequisite for Track 90A (O2.1).
**Done when:** `init`'s body and the helpers only it calls move to `initflow.py`, and any shared helpers it calls that are still in `cli.py` move to `core.py`; `cli.py` keeps the command shell; `initflow` never imports `cli`, and `tests/test_module_boundaries.py` lists it; a contract test runs init against an empty and a populated `tmp_path` folder without importing `cli` and pins its outcome and exit codes; tests that patched moved helpers patch them at their new owner, and no existing assertion changes; no output, flag or exit code change; the routing README and `init-devices.md` name the module; `./bin/check` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/core.py, src/mind_meld/initflow.py (new), tests/test_initflow.py (new), tests/test_init_auto_pin.py, tests/test_init_events_backfill.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/init-devices.md_
_blocked-by: Track 87A_
_out: 89A, 90A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md, tests/test_module_boundaries.py_
_session: fresh · effort: medium · verify: ./bin/check tests/test_initflow.py tests/test_init_auto_pin.py tests/test_module_boundaries.py_

_Source: owner decision 2026-10-23 (docs/SPEC.md Constraints, "Core and extensions"). Measured at 0e46556 from the direct-call graph of `cli.py`: `init` and the helpers only it calls total about 580 lines, and they call 5 helpers shared with other commands._

- **Init module** -- _src/mind_meld/cli.py, src/mind_meld/core.py, src/mind_meld/initflow.py, tests/test_init_auto_pin.py, tests/test_init_events_backfill.py, ~580 lines moved._ (M)
- **Init contract test and routing** -- _tests/test_initflow.py, tests/test_module_boundaries.py, docs/invariants/README.md, docs/invariants/init-devices.md, ~150 lines._ (S)

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87_

### Phase 5: mm 2.0 on Syncthing

**End-state:** The owner's Macs and Linux machine sync over one Syncthing folder that carries the 1.x store, with Syncthing-tolerant reads, a join that never re-keys, shared Claude paths, fast-forward pulls, propagated deletions, Syncthing state in status and a cutover runbook; gstack's machine-local files no longer sync and the factory can hand over between Macs; docs/SPEC.md O1 to O4 are accepted on a one-week soak and released as 2.0.0.
**Groups:** 89, 90, 91, 92, 93, 94, 95

#### Group 89: gstack sync scope

_Depends on: Group 88_

**This group delivers:** Machine-local gstack files (factory manager files, evidence ledgers, a browser profile, resume copies) stop syncing.

##### Track 89A: Keep machine-local gstack files and browser profiles out of sync
_3 tasks . ~230 LOC . medium risk . 7 files_
**Outcome:** The owner's Macs stop syncing files that belong to one machine: gstack's factory manager files, the live factory handoff, evidence ledgers and pair-review resume copies, and any Chromium browser profile (saved logins included) in any generic source. Shared question history, the taste profile, analytics and design documents keep syncing.
**Supports:** MVP-1, docs/SPEC.md O6.1 (credentials never sync) and the "What mm syncs and reports" gstack row (owner decisions 2026-10-29).
**Done when:** the generic walker skips any directory holding a `Local State` file at its root (a Chromium user-data directory) in every generic source, the same way `manifest.marker_skip_globs` skips `.extend-root` directories, so no folder name is hard-coded; `config._GSTACK_INCLUDE_PATTERNS` no longer admits `projects/*/manager-*.json`, `projects/*/*-evidence.jsonl` or the live `projects/*/driver-handoff.md` (the timestamped `driver-handoff-*.md` archives stay); `projects/*/pair-review/*` narrows to `deploy.md`, `report.md` and `parked-bugs.md`, so `session.from-*` resolve copies and stray scripts stay local; artifact directories such as `projects/*/designs/**` narrow to document file types (md, html, png, json, css and similar); `question-log.jsonl`, `question-events.jsonl`, `question-preferences.json`, `taste-profile.json` and `analytics/**` still sync, and the comment beside them records the owner's 2026-10-29 decision and gstack's line-based telemetry-sync cursor hazard; on a Mac whose config already pins the previous default lists, `mm migrate-config` offers the narrowed lists, and README Upgrading says the generic rule replaces the owner's local `projects/*/chrome-profile/*` stopgap exclude, that narrowing deletes nothing on any Mac, and how to remove already-synced profile copies by hand; `sync.md` names the removed readers and the marker skip; tests pin each pattern in and out and a fixture profile under `designs/` and under `pair-review/branches/`; `./bin/check --affected` passes.
_touches: src/mind_meld/manifest.py, src/mind_meld/config.py, src/mind_meld/core.py, tests/test_manifest.py, tests/test_config.py, docs/invariants/sync.md, README.md_
_blocked-by: Track 88A_
_out: 90A, 94A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-10-29 on the gstack sync audit (`~/.gstack/projects/kbitz-mind-meld/gstack-sync-audit-20261029.md`) and the generic browser-profile rule ("1 & 2 are fine"). Re-checked at 6a49f66: `_GSTACK_INCLUDE_PATTERNS` lists `projects/*/manager-*.json`, `projects/*/*-evidence.jsonl`, `projects/*/driver-handoff*.md`, `projects/*/pair-review/*` and `projects/*/designs/**`; one Mac's `config.toml` pins that list explicitly, and `core._compute_recommended_includes_diff` proposes the default only when the key is absent. Observed profiles on one Mac: `projects/kbitz-fintra/designs/track-17f/chrome-profile/` (176 synced files, including `Login Data`) and, under `projects/getpaseo-paseo/pair-review/branches/…/runtime/`, `signed-profile/` and `desktop/.dev/user-data/`. Outside the profile, that Mac's `designs/` held md 203, json 7, png 4, html 4, css 1, cjs 1._

- **Skip browser profiles in every generic source** -- _src/mind_meld/manifest.py, tests/test_manifest.py, docs/invariants/sync.md, ~80 lines._ (M)
- **Narrow the gstack lists** -- _src/mind_meld/config.py, tests/test_config.py, tests/test_manifest.py, ~80 lines._ (M)
- **Reach existing configs** -- _src/mind_meld/core.py, tests/test_config.py, README.md, ~70 lines._ (S)

#### Group 90: Fleet creation and join

_Depends on: Group 88, Group 89_

**This group delivers:** Only a new fleet creates the key header, and iCloud defaults go.

##### Track 90A: Let only a new fleet create the key header, and drop iCloud defaults
_3 tasks . ~180 LOC . high risk . 10 files_
**Outcome:** A machine joining over Syncthing waits for the fleet's key header instead of minting a second one, and new installs default to a Syncthing folder instead of iCloud Drive.
**Supports:** MVP-1, docs/SPEC.md O2.1 and the cutover.
**Done when:** `mm init --new-fleet` creates `mm-crypto-init` only in an empty folder and writes `.stignore` with mm's temporary-file patterns; plain `mm init` joins: with no key header it stops and says to wait for Syncthing, never writes one, then verifies the passphrase and pulls; `apply_crypto_init_repair` never rewrites the canonical file and removes only byte-identical copies; the missing-key-header remedy no longer says to run `mm init`; the default storage path is `~/Sync/mind-meld` on macOS and Linux, and `_auto_pin_storage_for_icloud`, the iCloud default and iCloud wording are removed; tests cover init on an empty, an unsynced and a populated folder; the new `.stignore` writer calls `fsutil.refuse_real_state_write_under_pytest` before its first mutation and joins the raw-writer census; `./bin/check` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/initflow.py, src/mind_meld/core.py, src/mind_meld/crypto.py, src/mind_meld/config.py, tests/test_crypto.py, tests/test_init_auto_pin.py, tests/test_initflow.py, tests/test_integration.py, tests/test_fsutil.py, docs/invariants/init-devices.md, README.md_
_blocked-by: Track 87A, Track 88A, Track 89A_
_out: 91A, 91B, 95A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_crypto.py tests/test_initflow.py tests/test_integration.py_

_Source: docs/archive/store-v2.md hazard "Init on an unsynced folder mints a second root_salt", re-checked at 408766d: `init` treats a `"missing"` crypto-init fetch as a first device and `crypto.bootstrap_crypto_init` writes a new salt; `_init_storage_guard` warns only when blobs or manifests already exist; `crypto.apply_crypto_init_repair` rewrites the canonical file on push and pull; `_init_crypto_session` still says "Run 'mm init'" when the file is missing._

- **New fleet versus join** -- _src/mind_meld/cli.py, src/mind_meld/initflow.py, src/mind_meld/crypto.py, tests/test_crypto.py, tests/test_initflow.py, tests/test_integration.py, ~100 lines._ (M)
- **Write-once key header** -- _src/mind_meld/crypto.py, src/mind_meld/core.py, tests/test_crypto.py, ~40 lines._ (S)
- **Drop the iCloud defaults** -- _src/mind_meld/initflow.py, src/mind_meld/config.py, tests/test_init_auto_pin.py, docs/invariants/init-devices.md, README.md, ~40 lines (+120 del)._ (S)

#### Group 91: Portable Claude paths ∥ Source commands module

_Depends on: Group 90_

**This group delivers:** Claude project memory is one folder on macOS and Linux, and the source commands move into their own module.

##### Track 91A: Share Claude project memory between macOS and Linux paths
_2 tasks . ~270 LOC . high risk . 6 files_
**Outcome:** The owner's Claude project memory for `~/src/<project>` is one folder on a Mac and on the Linux machine, instead of separate `-Users-<name>-…` and `-home-<name>-…` copies.
**Supports:** MVP-1, docs/SPEC.md O1.2.
**Done when:** the Claude walker publishes home-encoded project folder names in a home-relative form and pull maps them back to the local encoding; tombstone keys and `is_tombstoned` use the same form, so the switch mints no deletions; `synclog` project parsing follows; a test round-trips a project between a macOS-style and a Linux-style home; README Upgrading says every machine must update before the first push in the new form; `./bin/check` passes.
_touches: src/mind_meld/manifest.py, src/mind_meld/pullflow.py, src/mind_meld/synclog.py, tests/test_manifest.py, tests/test_integration.py, tests/test_synclog.py, docs/invariants/sync.md, README.md_
_blocked-by: Track 90A_
_out: 92A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/archive/store-v2.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_manifest.py tests/test_integration.py tests/test_synclog.py_

_Source: docs/archive/store-v2.md §7 (current evidence) and its hazard row on absolute-path folder names; at 408766d `manifest.walk_claude_source` publishes `projects/-Users-<name>-…` wire paths._

- **Home-relative wire paths** -- _src/mind_meld/manifest.py, src/mind_meld/pullflow.py, tests/test_manifest.py, tests/test_integration.py, ~150 lines._ (M)
- **Tombstones and history follow** -- _src/mind_meld/manifest.py, src/mind_meld/synclog.py, tests/test_synclog.py, docs/invariants/sync.md, README.md, ~120 lines._ (M)

##### Track 91B: Move the source commands out of cli.py
_2 tasks . ~150 LOC . low risk . 8 files_
**Outcome:** `mm sources`, `mm enable-source`, `mm disable-source` and `mm reconfigure-sources` live in their own module behind a contract test, so Track 94A adds the `mmm` source there instead of in `cli.py`; nothing the owner sees changes.
**Supports:** MVP-1 delivery under docs/SPEC.md Constraints, "Core and extensions"; prerequisite for Track 94A (factory handover).
**Done when:** the four command bodies and the helpers only they call move to `sourceflow.py`, and shared helpers still in `cli.py` move to `core.py`; `cli.py` keeps the command shells; `sourceflow` never imports `cli`, and `tests/test_module_boundaries.py` lists it; a contract test enables and disables a default source and an opt-in source over `tmp_path` config without importing `cli` and pins the outcome lines and exit codes; tests that patched moved helpers patch them at their new owner, and no existing assertion changes; no output, flag or exit-code change; the routing README names the module; `./bin/check --affected` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/core.py, src/mind_meld/sourceflow.py (new), tests/test_sourceflow.py (new), tests/test_source_toggle.py, tests/test_muse_source.py, tests/test_module_boundaries.py, docs/invariants/README.md_
_blocked-by: Track 90A_
_out: 94A_
_read-first: docs/invariants/README.md, tests/test_module_boundaries.py_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: docs/SPEC.md Constraints, "Core and extensions". Re-checked at 6a49f66: `sources`, `disable-source`, `enable-source` and `reconfigure-sources` are `cli.py` commands, and `enable_source` hard-codes the opt-in names `{"muse", "agy"}`._

- **Source command module** -- _src/mind_meld/cli.py, src/mind_meld/core.py, src/mind_meld/sourceflow.py, tests/test_source_toggle.py, tests/test_muse_source.py, ~300 lines moved._ (M)
- **Source contract test and routing** -- _tests/test_sourceflow.py, tests/test_module_boundaries.py, docs/invariants/README.md, ~150 lines._ (S)

#### Group 92: Fast-forward pulls

_Depends on: Group 91_

**This group delivers:** Peer edits fast-forward, conflicts need two real edits, and deletions propagate.

##### Track 92A: Fast-forward peer edits and propagate deletions
_2 tasks . ~450 LOC . high risk . 9 files_
**Outcome:** A peer's change to a file the owner has not touched simply applies, a conflict appears only when both machines changed the file, deleted `MEMORY.md` lines stay deleted, and a file deleted on one machine is deleted on the others with a 30-day recovery copy.
**Supports:** MVP-1, docs/SPEC.md O1.1.
**Done when:** a `state.db` migration adds a per-peer last-synced base (`peer, source, path, sha`); `_apply_incoming_file` skips when the remote equals the base, fast-forwards with no union when the local file equals the base, and unions a mergeable file or writes a conflict copy only when both changed, falling back to today's rule when no base exists; a peer tombstone deletes the local file when it still equals the base and keeps the bytes recoverable for 30 days; bases are recorded after applied, matched and deleted outcomes, never after failed or waiting ones; `pullplan` predicts the same; `conflicts.md` and `sync.md` describe the rule; tests cover each branch and a three-machine sequence; the recovery-copy writer calls `fsutil.refuse_real_state_write_under_pytest` before its first mutation and joins the raw-writer census; the provisional `state.db` tables each name a reader or an explicit no-reader, and the pre-2026-10-19 Track IDs in `sync.md`'s history-log section, `statedb.py`, the routing README, `init-devices.md` and `tests/test_pullhistory.py` are retired (including `sync.md`'s deferred hold-escalation pointer, killed 2026-10-21 as unobserved); `./bin/check` passes.
_touches: src/mind_meld/statedb.py, src/mind_meld/pullflow.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, src/mind_meld/retention.py, tests/test_statedb.py, tests/test_merge.py, tests/test_additive_sync.py, tests/test_integration.py, tests/test_fsutil.py, docs/invariants/conflicts.md, docs/invariants/sync.md, README.md, docs/invariants/README.md, docs/invariants/init-devices.md, tests/test_pullhistory.py_
_blocked-by: Track 87A, Track 91A_
_out: 93A, 95A_
_read-first: docs/invariants/README.md, docs/invariants/conflicts.md, docs/invariants/sync.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_statedb.py tests/test_merge.py tests/test_additive_sync.py tests/test_integration.py_

_Source: docs/archive/store-v2.md hazard rows "Every peer edit to an untouched file is a conflict" and "Line deletions in `MEMORY.md` and `.jsonl` never stick", re-checked at 408766d: `_apply_incoming_file` union-merges every update of a mergeable file, then keeps a newer local file or writes a conflict copy; pull never deletes local files (tombstones only stop re-download); `statedb` has an unused `ledger` table and no base._

- **Base table and the fast-forward rule** -- _src/mind_meld/statedb.py, src/mind_meld/pullflow.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, tests/test_statedb.py, tests/test_merge.py, tests/test_additive_sync.py, ~300 lines._ (L)
- **Propagated deletions with recovery** -- _src/mind_meld/pullflow.py, src/mind_meld/retention.py, tests/test_integration.py, docs/invariants/conflicts.md, docs/invariants/sync.md, README.md, ~150 lines._ (M)

#### Group 93: Syncthing status

_Depends on: Group 92_

**This group delivers:** Status shows Syncthing's state and each machine's latest push.

##### Track 93A: Report Syncthing state and each machine's latest push
_2 tasks . ~350 LOC . medium risk . 9 files_
**Outcome:** `mm status` tells the owner whether Syncthing is connected and in sync and whether this machine has applied each peer's latest push, and `mm diag` flags folder settings that would break sync.
**Supports:** MVP-1, docs/SPEC.md O2.2 and O3.2.
**Done when:** a new `syncthing.py` finds the local Syncthing configuration and reads its REST API read-only with timeouts (connected peers, folder state, and the folder options README lists); mm never writes Syncthing's configuration; pull records each peer's applied manifest time, and status shows each peer's last push and whether it is applied, warning in every mode when something has waited more than 24 h; diag lints the folder options and `.stignore`; a missing or unreachable Syncthing is reported, not an error; tests use a fake REST server; `./bin/check` passes.
_touches: src/mind_meld/syncthing.py (new), src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/statedb.py, tests/test_syncthing.py (new), tests/test_diag.py, tests/test_module_boundaries.py, README.md, docs/invariants/README.md_
_blocked-by: Track 77A, Track 92A_
_out: 94A, 95A_
_read-first: docs/invariants/README.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_syncthing.py tests/test_diag.py tests/test_module_boundaries.py_

_Source: docs/SPEC.md O2.2 and O3.2; docs/archive/store-v2.md §10 folder options (current evidence; Syncthing option semantics verified against docs.syncthing.net on 2026-10-11 for the former Track 81A)._

- **Read-only Syncthing client and diag lint** -- _src/mind_meld/syncthing.py, src/mind_meld/statusflow.py, tests/test_syncthing.py, tests/test_module_boundaries.py, docs/invariants/README.md, ~200 lines._ (M)
- **Each peer's push in status** -- _src/mind_meld/statusflow.py, src/mind_meld/pullflow.py, src/mind_meld/statedb.py, tests/test_diag.py, README.md, ~150 lines._ (M)

#### Group 94: Factory handover

_Depends on: Group 89, Group 91, Group 93_

**This group delivers:** An opt-in `mmm` source carries mmm-factory's durable state between Macs.

##### Track 94A: Add an opt-in mmm source for factory handover
_2 tasks . ~220 LOC . medium risk . 7 files_
**Outcome:** After `mm enable-source mmm` on each Mac, the owner stops the factory on one Mac, pushes, pulls on another Mac and starts it there with the items, plans, cards, settings, passports, handoff notes, briefs and run ledgers in place; locks, watcher state, fields files, worker-ID lists, usage caches and other machine-local factory files stay behind.
**Supports:** docs/SPEC.md "What mm syncs and reports" mmm-factory row and [factory handover](SPEC.md#factory-handover) (owner decisions 2026-10-29).
**Done when:** a default `mmm` source on `~/.mmm` is opt-in like Muse (never active until `mm enable-source mmm` on that machine, or an explicit choice at `mm init` or `mm reconfigure-sources`); it syncs `items/` (each item's `<id>.json`, `.plan.md` and `.card.md`), the four run ledgers (`provenance.jsonl`, `hangs.jsonl`, `records.jsonl`, `picker.jsonl`) and, per project, `manager.toml`, the passport (`manager-passport.txt`), the handoff note (`driver-handoff.md`) and its timestamped archives, the manager's launch cards (`cards/`) and `briefs/`; it never syncs `*.lock`, `manager-watch.*`, `manager-status.json`, `manager-fields.json`, `manager-usage.json`, the worker-ID lists (`manager-docs-workers.txt`, `release-text-workers.txt`), the worker-side `mmm-<id>-card.md` and `mmm-<id>-autoplan-<n>.md` copies, SQLite files, `traces/`, `release/`, `review-compare/` or `picker-jev/`, and `~/.local/share/mmm-factory/` and `~/.config/mmm-factory/` are never sources; `~/.mmm` must be a real directory (a link is refused, as the factory requires); README documents enabling it, the single-Mac rule, that in-flight work does not transfer (finish or ship first), and that item records carry raw worker text to every machine with the source enabled; tests pin the allowlist against a fixture factory home; `./bin/check --affected` passes.
_touches: src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_sourceflow.py, tests/test_mmm_source.py (new), docs/invariants/sync.md, README.md_
_blocked-by: Track 89A, Track 91B, Track 93A_
_out: 95A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-10-29, including mmm-factory manager Ugarit's recommendation the owner accepted (exclude `traces/`, `release/`, `review-compare/` and the worker-ID lists; add `cards/`; `briefs/` per option (a), which the owner confirms at the PR that adds this card and which needs mmm-factory's managers to keep handoff briefs under `~/.mmm/projects/<slug>/briefs/`). Paths confirmed against mmm-factory's `docs/agent-runbook.md` "The factory home" at 93cf70a (v0.5.29) and its file-name constants (`manager.toml`, `manager-passport.txt`, `driver-handoff.md`, `manager-fields.json`, `manager-watch.json`, `manager-watch.owner`, `manager-watch.lock`, `manager-status.json`, `manager-usage.json`, `release-text-workers.txt`). Factory attribution in `retro-fleet` stays deferred (Future)._

- **The mmm source and its allowlist** -- _src/mind_meld/config.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_mmm_source.py, ~150 lines._ (M)
- **Handover docs** -- _README.md, docs/invariants/sync.md, tests/test_sourceflow.py, ~70 lines._ (S)

#### Group 95: Cutover runbook

_Depends on: Group 90, Group 92, Group 93, Group 94_

**This group delivers:** A runbook and agent docs for moving the fleet onto Syncthing.

##### Track 95A: Write the cutover runbook and the Syncthing documentation
_2 tasks . ~400 LOC . low risk . 7 files_
**Outcome:** The owner has a step-by-step runbook to move the fleet from iCloud Drive to Syncthing and bring the Linux machine in, and every document an agent reads describes the Syncthing store.
**Supports:** MVP-1, docs/SPEC.md O1 to O4 (cutover and soak).
**Done when:** README covers installing Syncthing on macOS and Arch, pairing, the folder options mm relies on, and a cutover runbook (update every machine, stop hooks, move the store folder from iCloud Drive into the Syncthing folder, point `storage.path` at it, join the Linux machine with `mm init`, remove a dead machine's directories by hand, delete the iCloud folder two weeks later); AGENTS.md Key Principles and root SPEC.md's storage sections describe the Syncthing folder; `sync.md` and `init-devices.md` drop iCloud-only rules; `pyproject.toml` description and keywords drop iCloud; docs/SPEC.md's superseded-history entry for the store design names §13 (cutover) as evidence alongside §7, §10 and §11; `./bin/check` passes.
_touches: README.md, AGENTS.md, SPEC.md, docs/SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml_
_blocked-by: Track 90A, Track 92A, Track 93A, Track 94A_
_out: 96A_
_read-first: docs/SPEC.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py_

_Source: docs/SPEC.md O1 to O4 and the accepted manual cutover step; docs/archive/store-v2.md §11 (Linux) and §13 (cutover) as evidence._

- **Runbook and Syncthing setup** -- _README.md, ~250 lines._ (M)
- **Agent-facing docs** -- _AGENTS.md, SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, ~150 lines._ (M)

#### Group 96: Cross-model memory qualification

_Depends on: Group 95_

**This group delivers:** Two live checks prove or refute the cross-model memory design before implementation.

##### Track 96A: Qualify cross-model memory in isolated homes
_1 task . ~120 LOC . low risk . 1 file_
**Outcome:** The owner knows whether the approved cross-model memory design works with the current Codex and Claude Code before any implementation is carded: fresh sessions follow a "search, don't read" pointer to the other agent's memory, and per-machine Codex copies do not churn sync.
**Supports:** docs/SPEC.md O9.4 (qualification before implementation), after 2.0.
**Done when:** `docs/designs/cross-model-memory.md` records Q1 and Q2 run in temporary `HOME` and `CODEX_HOME` directories (never the owner's real stores), with the observed codex-cli and Claude Code versions; Q1: a fresh Codex session finds a Claude-origin memory through an `AGENTS.md` pointer and a fresh Claude session finds a Codex-origin memory through a `CLAUDE.md` pointer, by search, without loading `MEMORY.md` whole, and which file Codex loads at startup is recorded; Q2: after Codex regenerates its memory files on one machine, a push publishes only that machine's copies and a peer applies them with no conflict copy and no write into any native store; the chosen Codex file set is recorded; a failed check is written up for the owner and no implementation Track is proposed; no product code changes.
_touches: docs/designs/cross-model-memory.md_
_blocked-by: Track 95A_
_read-first: docs/designs/cross-model-memory.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py_

_Source: owner decision 2026-10-29 ("yes, I want cross-model memory", post-2.0, qualification card after the cutover); probe `~/.gstack/projects/kbitz-mind-meld/cross-model-memory-probe-20261029.md` on codex-cli 0.164.0._

- **Run Q1 and Q2 and record them** -- _docs/designs/cross-model-memory.md, ~120 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 88 ← {}
- Group 89 ← {88}
- Group 90 ← {88, 89}
- Group 91 ← {90}
- Group 92 ← {91}
- Group 93 ← {92}
- Group 94 ← {89, 91, 93}
- Group 95 ← {90, 92, 93, 94}
- Group 96 ← {95}
```

Track detail per group:

```
Group 88: Init module          (in progress)
  +-- Track 88A ........... ~M . 2 tasks
Group 89: gstack sync scope
  +-- Track 89A ........... ~L . 3 tasks
Group 90: Fleet creation and join
  +-- Track 90A ........... ~L . 3 tasks
Group 91: Portable Claude paths ∥ Source commands module
  +-- Track 91A ........... ~L . 2 tasks
  +-- Track 91B ........... ~M . 2 tasks
Group 92: Fast-forward pulls
  +-- Track 92A ........... ~L . 2 tasks
Group 93: Syncthing status
  +-- Track 93A ........... ~L . 2 tasks
Group 94: Factory handover
  +-- Track 94A ........... ~L . 2 tasks
Group 95: Cutover runbook
  +-- Track 95A ........... ~L . 2 tasks
Group 96: Cross-model memory qualification
  +-- Track 96A ........... ~M . 1 task
```

**Total: 9 groups . 10 tracks remaining.**

Almost every Track edits `README.md`, `config.py`, `cli.py` or an invariants doc another Track also edits, so the packer emits one chain with one parallel pair (91A ∥ 91B). The gstack scope fix (89A) goes first after the pinned init move because the owner asked for it now and it shares `config.py` and `core.py` with the init and join Tracks. The source-command move (91B) sits just before the `mmm` source (94A) under docs/SPEC.md's core-and-extensions constraint, and 94A lands before the cutover runbook (95A) so the runbook can cover factory handover. Track 77A stays on Track 93A, and the shipped Track 87A stays on Tracks 88A, 90A and 92A.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on the one-week Mac plus Linux soak, with O6 to O8 holding and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance` after following Group 95's runbook. Cross-model memory (O9) is qualified after the cutover by Group 96; its implementation waits in Future until that qualification passes.

---

## Future

Deferred: docs/roadmap-future.md (16 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
