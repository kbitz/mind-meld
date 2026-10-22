# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Cursor and Muse.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support is being removed; Grok models used through Cursor stay in the Grok row. Memory capture, recall and sync parity remain deferred ([the memory continuity design](designs/memory-continuity.md)).

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

---

## In Progress

No active branches, sessions or PRs were declared for this regeneration.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74_

### Phase 4: Quiet sync and complete usage

**End-state:** On today's store, the 1.x compatibility tripwires are gone, Muse has its own retro family, Grok Build support is removed, and retro capture completes within one budget, so docs/SPEC.md O7 holds on the owner's Macs. (Groups 75-78 shipped the O8 half in v1.11.3-v1.11.5.)
**Groups:** 79, 80, 81, 82, 83

#### Group 79: Compatibility contract and Muse family

_Depends on: none_

**This group delivers:** Muse gets its own retro row and the 1.x compatibility tripwires are gone.

##### Track 79A: Retire the compatibility contract and give Muse its own family
_3 tasks . ~260 LOC (+700 del) . medium risk . 9 files_
**Outcome:** Muse usage shows in its own Muse row in `retro-fleet`, and releases stop carrying the 1.x compatibility tripwires; only the storage-format refusal gates stay tested.
**Supports:** MVP-1, docs/SPEC.md O7.1 and the release policy's compatibility surface.
**Done when:** `test_closed_vocabularies`, the CLI-surface golden and its fixture, and `EXIT_EVIDENCE` with its checks are deleted, and the usage, rejected-value and abort exit tests move to `tests/test_integration.py`; the format-constant, frozen-reader and device-registry-writer tests stay; `auto-upgrade.md` "Compatibility (1.x)" becomes a short storage-format gates section, and README, the routing README, `init-devices.md` and `events-retro.md` stop citing the contract; `host_usage.HostFamily` and `AGENT_ROW_ORDER` gain `muse` and `muse-*` models classify as Muse; README Upgrading says an un-upgraded peer drops a snapshot carrying the new family until it updates; the release's CHANGELOG entry carries one line noting Track 76A's removal of the abandoned store code and simulator (sync behavior unchanged; v1.11.4 and v1.11.5 shipped without it); `./bin/check` passes.
_touches: tests/test_compat_contract.py, tests/fixtures/cli_surface_1_x.json, tests/fixtures/compat_1_0/README.md, src/mind_meld/host_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/_host_usage_oracle.py, tests/test_muse_usage.py, tests/test_host_usage.py, tests/test_retro_fleet_aggregator.py, tests/test_integration.py, docs/invariants/auto-upgrade.md, docs/invariants/README.md, docs/invariants/init-devices.md, docs/invariants/events-retro.md, README.md_
_blocked-by: Track 78A_
_out: 80A, 81A_
_read-first: docs/invariants/README.md, docs/invariants/auto-upgrade.md, docs/invariants/events-retro.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_compat_contract.py tests/test_muse_usage.py tests/test_host_usage.py tests/test_retro_fleet_aggregator.py_

_Source: Future "Add the `muse` host family" (promoted: its deferral rested on the 1.x contract that docs/SPEC.md retired on 2026-10-19); owner decision 2026-10-19 that compatibility is not a concern for a single user. At 408766d `aggregator._accept_hosts_payload` rejects an unknown family as `invalid_counter`._

- **Cut the contract tripwires** -- Delete the closed-vocabulary, CLI-golden and exit-evidence checks; move the surviving exit tests. _tests/test_compat_contract.py, tests/fixtures/cli_surface_1_x.json, tests/fixtures/compat_1_0/README.md, tests/test_integration.py, ~100 lines (+700 del)._ (M)
- **Add the Muse family** -- _src/mind_meld/host_usage.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/_host_usage_oracle.py, tests/test_muse_usage.py, tests/test_host_usage.py, tests/test_retro_fleet_aggregator.py, ~100 lines._ (M)
- **Rewrite the compatibility docs** -- _docs/invariants/auto-upgrade.md, docs/invariants/README.md, docs/invariants/init-devices.md, docs/invariants/events-retro.md, README.md, ~60 lines._ (S)

#### Group 80: Grok Build usage reader

_Depends on: Group 79_

**This group delivers:** Pushes stop reading Grok Build logs; Cursor's Grok models still price.

##### Track 80A: Remove the Grok Build usage reader
_3 tasks . ~150 LOC (+3,800 del) . medium risk . 13 files_
**Outcome:** Pushes stop spending time and output on Grok Build's logs, while Grok models used through Cursor still classify and price in the Grok row.
**Supports:** MVP-1, docs/SPEC.md host table, O7.2 and O8.2 (one capture budget).
**Done when:** `read_grok_usage` and its helpers, `host_skill_discovery.py`, the grok reader gate, consent and warm entries, the status and diag grok blocks, and the Grok Build fixtures and tests are gone; `HostFamily` "grok", the `grok-` prefix check, Grok pricing (grok-4.6 kept for retained rows) and the Cursor model refiners stay, and a test shows a Cursor grok-4.7 run still prices in the Grok row; README, the routing README and `events-retro.md` drop the reader; `./bin/check` passes.
_touches: src/mind_meld/host_usage.py, src/mind_meld/host_skill_discovery.py, src/mind_meld/events_tail.py, src/mind_meld/events.py, src/mind_meld/cli.py, src/mind_meld/errors.py, tests/test_host_usage.py, tests/test_host_usage_snapshot.py, tests/test_host_skill_discovery.py, tests/fixtures/host_sessions/grok/, tests/_host_usage_oracle.py, tests/test_docs_routing.py, tests/conftest.py, docs/invariants/events-retro.md, docs/invariants/README.md, README.md_
_blocked-by: Track 79A_
_out: 81A, 82A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_host_usage.py tests/test_host_usage_snapshot.py tests/test_docs_routing.py_

_Source: owner decision 2026-10-19. `mm diag` on 889e42c0 the same day: Cursor models cached `grok-4.7`; Grok Build ledgers 224 of 224, models `grok-4.6-build` and `grok-4.7-build`; the push printed "reading grok usage beyond … budget" and "grok coverage is partial"._

- **Delete the reader and skill discovery** -- _src/mind_meld/host_usage.py, src/mind_meld/host_skill_discovery.py, src/mind_meld/events_tail.py, src/mind_meld/events.py, src/mind_meld/cli.py, src/mind_meld/errors.py, tests/test_host_usage.py, tests/test_host_usage_snapshot.py, tests/test_host_skill_discovery.py, tests/fixtures/host_sessions/grok/, tests/_host_usage_oracle.py, tests/test_docs_routing.py, tests/conftest.py, ~3,800 lines (del)._ (S)
- **Keep Cursor's Grok models priced** -- _src/mind_meld/host_usage.py, tests/test_host_usage.py, ~150 lines._ (M)
- **Docs** -- _docs/invariants/events-retro.md, docs/invariants/README.md, README.md, ~50 lines._ (S)

#### Group 81: Grok Build sync source

_Depends on: Group 79, Group 80_

**This group delivers:** The Grok Build source is gone without breaking old configs or peers.

##### Track 81A: Remove the Grok Build sync source
_3 tasks . ~150 LOC (+600 del) . medium risk . 8 files_
**Outcome:** mm no longer offers or syncs a Grok Build customization source, and a config or an un-upgraded peer that still lists one keeps syncing everything else.
**Supports:** MVP-1, docs/SPEC.md host table (standalone Grok Build removed, owner decision 2026-10-19).
**Done when:** the `grok` default source, `walk_grok_source`, the `grok` source type and its enable, disable and reconfigure branches are gone; a retired-name filter (`opencode`, `grok`) applies to configured sources and to peers' manifests on pull, so an explicit `type = "grok"` source neither wedges push nor marks autopull degraded; `mm migrate-config` removes a leftover grok source without minting tombstones; README and `sync.md` drop the source; `docs/designs/grok-build-usage-reader.md` moves to `docs/archive/` with a superseded notice, and its links in docs/SPEC.md and AGENTS.md follow; `./bin/check` passes.
_touches: src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/cli.py, src/mind_meld/resolveflow.py, tests/test_source_toggle.py, tests/test_config.py, tests/test_manifest.py, tests/test_integration.py, docs/invariants/sync.md, README.md, docs/designs/grok-build-usage-reader.md, docs/archive/grok-build-usage-reader.md (new), docs/SPEC.md, AGENTS.md_
_blocked-by: Track 79A, Track 80A_
_out: 82A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_source_toggle.py tests/test_config.py tests/test_manifest.py tests/test_integration.py_

_Source: owner decision 2026-10-19 ("i rarely use grok independently anymore"). At 408766d `load_config` does not validate source types, so an explicit grok source would reach the walker dispatch and raise `ManifestError("unknown source type 'grok'")` on every push once the type is deleted._

- **Retired-name filter and migration** -- _src/mind_meld/config.py, src/mind_meld/cli.py, tests/test_config.py, tests/test_integration.py, ~120 lines._ (M)
- **Delete the source type** -- _src/mind_meld/config.py, src/mind_meld/manifest.py, src/mind_meld/cli.py, src/mind_meld/resolveflow.py, tests/test_source_toggle.py, tests/test_manifest.py, ~600 lines (del)._ (S)
- **Docs and the reader's design archive** -- _docs/invariants/sync.md, README.md, docs/designs/grok-build-usage-reader.md, docs/archive/grok-build-usage-reader.md, docs/SPEC.md, AGENTS.md, ~50 lines._ (S)

#### Group 82: Git capture catch-up

_Depends on: Group 80, Group 81_

**This group delivers:** Retro commit capture completes on its own across pushes.

##### Track 82A: Let git capture catch up across pushes
_2 tasks . ~800 LOC . medium risk . 10 files_
**Outcome:** Retro commit counts for every machine become complete again on their own within a few pushes, without the owner running `mm recapture`.
**Supports:** MVP-1, docs/SPEC.md O7.2.
**Done when:** a persistent project index (directory mtime, newest session-log stat, cwd, git root) lets discovery re-probe only new or changed project directories and resume where it stopped; a per-repository git cursor walks the stalest roots first and keeps a cut-off root's cursor for the next push; discovery counts as complete once every listed directory has an index entry, so the retro cursor advances and the identity cache is written; `mm status` says how many repositories are left instead of advising `mm recapture`, which stays for windows beyond retention; a test runs successive budget-limited pushes over more projects than one budget covers and reaches complete capture; `./bin/check` passes.
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/identity.py, src/mind_meld/config.py, src/mind_meld/cli.py, tests/test_events.py, tests/test_track_30a.py, tests/test_events_budget_scope.py, tests/test_identity.py, tests/test_diag.py, docs/invariants/events-retro.md, README.md_
_blocked-by: Track 81A, Track 80A_
_out: 83A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_events.py tests/test_track_30a.py tests/test_events_budget_scope.py tests/test_identity.py tests/test_diag.py_

_Source: observed 2026-10-19 on 889e42c0: `mm diag` "Git-root discovery … budget_ms 50 … status exceeded … roots 0" and git capture `discovery: partial`, `since 2026-10-09`, `advances_cursor: False`; `mm status` "Retro capture: incomplete since 2026-10-09". At 408766d `events.discover_git_roots` probes `~/.claude/projects/*` in the same order every push under `ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS = 50` and `ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS = 100`, and keeps nothing between pushes._

- **Project index and resumable discovery** -- _src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/identity.py, src/mind_meld/config.py, tests/test_events.py, tests/test_events_budget_scope.py, tests/test_identity.py, ~450 lines._ (L)
- **Per-repository cursor and status** -- _src/mind_meld/events.py, src/mind_meld/cli.py, tests/test_track_30a.py, tests/test_diag.py, docs/invariants/events-retro.md, README.md, ~350 lines._ (M)

#### Group 83: One capture budget

_Depends on: Group 82_

**This group delivers:** Usage and activity capture fits one budget and stays complete.

##### Track 83A: Fit the session walk and Codex reads in one capture budget
_3 tasks . ~650 LOC . medium risk . 8 files_
**Outcome:** Each attended push spends at most one configured capture budget (default 5 s) on usage and activity, and Claude session and Codex usage totals stay complete as the logs grow.
**Supports:** MVP-1, docs/SPEC.md O7.2 and O8.2.
**Done when:** one `[retro] capture_budget_ms` (default 5000) replaces the separate discovery, walk and host-reader budgets for attended push, with autopush keeping a small fixed share; the Claude session walk rotates its start, reuses unchanged projects' metadata from Track 82A's index, and takes its end mark before the cache write; the Codex reader reads newest rollouts first and freezes settled days the way the Muse reader does, so a warm push reads only recent days; tests show complete totals across successive budget-limited pushes; `events-retro.md` and README describe the budget; `./bin/check` passes.
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/host_usage.py, src/mind_meld/config.py, tests/test_events.py, tests/test_host_usage.py, tests/test_token_usage.py, tests/test_host_usage_snapshot.py, docs/invariants/events-retro.md, README.md_
_blocked-by: Track 82A_
_out: 84A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_events.py tests/test_host_usage.py tests/test_token_usage.py tests/test_host_usage_snapshot.py_

_Source: observed 2026-10-19 on 889e42c0: "host-usage snapshot skipped (codex deadline) … Last read allowed 5000 ms; last complete read 2456 ms (4 d ago); 3075 of 3459 rollouts cached" and "events tail budget exceeded". At 408766d `host_usage._scan_codex_root` lists rollouts oldest-first and fingerprints every cached file each pass; `events.walk_session_metadata` stops in a fixed scandir order._

- **One capture budget** -- _src/mind_meld/events_tail.py, src/mind_meld/config.py, tests/test_host_usage_snapshot.py, ~150 lines._ (M)
- **Rotating session walk** -- _src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/test_events.py, tests/test_token_usage.py, ~200 lines._ (M)
- **Codex newest-first with settled days** -- _src/mind_meld/host_usage.py, tests/test_host_usage.py, docs/invariants/events-retro.md, README.md, ~300 lines._ (M)

### Phase 5: mm 2.0 on Syncthing

**End-state:** The owner's Macs and Linux machine sync over one Syncthing folder that carries the 1.x store, with Syncthing-tolerant reads, a join that never re-keys, shared Claude paths, fast-forward pulls, propagated deletions, Syncthing state in status and a cutover runbook; docs/SPEC.md O1 to O4 are accepted on a one-week soak and released as 2.0.0.
**Groups:** 84, 85, 86, 87, 88, 89

#### Group 84: Syncthing-tolerant reads

_Depends on: Group 83_

**This group delivers:** Pull and status treat Syncthing's partial and foreign files as waiting or ignored.

##### Track 84A: Make storage reads tolerate Syncthing
_2 tasks . ~190 LOC . medium risk . 8 files_
**Outcome:** On a Syncthing folder, pull and status ignore Syncthing's temporary and conflict files, retry a file Syncthing is replacing, and report content that has not arrived as waiting rather than failed.
**Supports:** MVP-1, docs/SPEC.md O3.1 and O1.4 (late, out-of-order delivery).
**Done when:** `LocalBackend.list_keys` skips dot-prefixed and Syncthing temporary names and `find_conflict_copies` recognizes `*.sync-conflict-*`; a device file counts only when its name is that device's key; `mm diag` counts foreign files; `LocalBackend.get` retries a vanished file briefly and a vanished file reads as missing, not corrupt; a device entry that vanishes mid-read is skipped with a notice instead of refusing the pull; a missing blob yields a `waiting` outcome that is not a failure and does not mark autopull degraded, and pull says what it is waiting for; tests use a local folder that delivers files late and swaps them mid-read; `./bin/check` passes.
_touches: src/mind_meld/storage/local.py, src/mind_meld/devices.py, src/mind_meld/errors.py, src/mind_meld/cli.py, tests/test_storage_local.py, tests/test_devices.py, tests/test_diag.py, tests/test_pull_result.py, docs/invariants/sync.md_
_blocked-by: Track 83A_
_out: 85A, 87A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/archive/store-v2.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_storage_local.py tests/test_devices.py tests/test_diag.py tests/test_pull_result.py_

_Source: docs/archive/store-v2.md "Why a new store" hazards, re-checked at 408766d: `_download_and_apply` counts a missing blob as `failed`; `_fetch_remote_manifest` checks `exists` and then `get`, so a swapped file reads as corrupt; `devices.list_devices_with_drops` drops an unreadable entry and `_check_fleet_version_or_refuse` then refuses the pull; `devices._list_devices_impl` accepts any `*.json`; `LocalBackend.find_conflict_copies` knows only iCloud and Dropbox names._

- **Ignore foreign files and retry swaps** -- _src/mind_meld/storage/local.py, src/mind_meld/devices.py, src/mind_meld/cli.py, tests/test_storage_local.py, tests/test_devices.py, tests/test_diag.py, ~110 lines._ (M)
- **A waiting outcome** -- _src/mind_meld/errors.py, src/mind_meld/cli.py, tests/test_pull_result.py, docs/invariants/sync.md, ~80 lines._ (M)

#### Group 85: Fleet creation and join

_Depends on: Group 84_

**This group delivers:** Only a new fleet creates the key header, and iCloud defaults go.

##### Track 85A: Let only a new fleet create the key header, and drop iCloud defaults
_3 tasks . ~180 LOC . high risk . 6 files_
**Outcome:** A machine joining over Syncthing waits for the fleet's key header instead of minting a second one, and new installs default to a Syncthing folder instead of iCloud Drive.
**Supports:** MVP-1, docs/SPEC.md O2.1 and the cutover.
**Done when:** `mm init --new-fleet` creates `mm-crypto-init` only in an empty folder and writes `.stignore` with mm's temporary-file patterns; plain `mm init` joins: with no key header it stops and says to wait for Syncthing, never writes one, then verifies the passphrase and pulls; `apply_crypto_init_repair` never rewrites the canonical file and removes only byte-identical copies; the missing-key-header remedy no longer says to run `mm init`; the default storage path is `~/Sync/mind-meld` on macOS and Linux, and `_auto_pin_storage_for_icloud`, the iCloud default and iCloud wording are removed; tests cover init on an empty, an unsynced and a populated folder; the new `.stignore` writer calls `fsutil.refuse_real_state_write_under_pytest` before its first mutation and joins the raw-writer census; `./bin/check` passes.
_touches: src/mind_meld/cli.py, src/mind_meld/crypto.py, src/mind_meld/config.py, tests/test_crypto.py, tests/test_init_auto_pin.py, tests/test_integration.py, tests/test_fsutil.py, docs/invariants/init-devices.md, README.md_
_blocked-by: Track 84A_
_out: 86A, 89A_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_crypto.py tests/test_integration.py_

_Source: docs/archive/store-v2.md hazard "Init on an unsynced folder mints a second root_salt", re-checked at 408766d: `init` treats a `"missing"` crypto-init fetch as a first device and `crypto.bootstrap_crypto_init` writes a new salt; `_init_storage_guard` warns only when blobs or manifests already exist; `crypto.apply_crypto_init_repair` rewrites the canonical file on push and pull; `_init_crypto_session` still says "Run 'mm init'" when the file is missing._

- **New fleet versus join** -- _src/mind_meld/cli.py, src/mind_meld/crypto.py, tests/test_crypto.py, tests/test_integration.py, ~100 lines._ (M)
- **Write-once key header** -- _src/mind_meld/crypto.py, tests/test_crypto.py, ~40 lines._ (S)
- **Drop the iCloud defaults** -- _src/mind_meld/cli.py, src/mind_meld/config.py, tests/test_init_auto_pin.py, docs/invariants/init-devices.md, README.md, ~40 lines (+120 del)._ (S)

#### Group 86: Portable Claude paths

_Depends on: Group 85_

**This group delivers:** Claude project memory is one folder on macOS and Linux.

##### Track 86A: Share Claude project memory between macOS and Linux paths
_2 tasks . ~270 LOC . high risk . 6 files_
**Outcome:** The owner's Claude project memory for `~/src/<project>` is one folder on a Mac and on the Linux machine, instead of separate `-Users-<name>-…` and `-home-<name>-…` copies.
**Supports:** MVP-1, docs/SPEC.md O1.2.
**Done when:** the Claude walker publishes home-encoded project folder names in a home-relative form and pull maps them back to the local encoding; tombstone keys and `is_tombstoned` use the same form, so the switch mints no deletions; `synclog` project parsing follows; a test round-trips a project between a macOS-style and a Linux-style home; README Upgrading says every machine must update before the first push in the new form; `./bin/check` passes.
_touches: src/mind_meld/manifest.py, src/mind_meld/cli.py, src/mind_meld/synclog.py, tests/test_manifest.py, tests/test_integration.py, tests/test_synclog.py, docs/invariants/sync.md, README.md_
_blocked-by: Track 85A_
_out: 87A_
_read-first: docs/invariants/README.md, docs/invariants/sync.md, docs/archive/store-v2.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_manifest.py tests/test_integration.py tests/test_synclog.py_

_Source: docs/archive/store-v2.md §7 (current evidence) and its hazard row on absolute-path folder names; at 408766d `manifest.walk_claude_source` publishes `projects/-Users-<name>-…` wire paths._

- **Home-relative wire paths** -- _src/mind_meld/manifest.py, src/mind_meld/cli.py, tests/test_manifest.py, tests/test_integration.py, ~150 lines._ (M)
- **Tombstones and history follow** -- _src/mind_meld/manifest.py, src/mind_meld/synclog.py, tests/test_synclog.py, docs/invariants/sync.md, README.md, ~120 lines._ (M)

#### Group 87: Fast-forward pulls

_Depends on: Group 84, Group 86_

**This group delivers:** Peer edits fast-forward, conflicts need two real edits, and deletions propagate.

##### Track 87A: Fast-forward peer edits and propagate deletions
_2 tasks . ~450 LOC . high risk . 9 files_
**Outcome:** A peer's change to a file the owner has not touched simply applies, a conflict appears only when both machines changed the file, deleted `MEMORY.md` lines stay deleted, and a file deleted on one machine is deleted on the others with a 30-day recovery copy.
**Supports:** MVP-1, docs/SPEC.md O1.1.
**Done when:** a `state.db` migration adds a per-peer last-synced base (`peer, source, path, sha`); `_apply_incoming_file` skips when the remote equals the base, fast-forwards with no union when the local file equals the base, and unions a mergeable file or writes a conflict copy only when both changed, falling back to today's rule when no base exists; a peer tombstone deletes the local file when it still equals the base and keeps the bytes recoverable for 30 days; bases are recorded after applied, matched and deleted outcomes, never after failed or waiting ones; `pullplan` predicts the same; `conflicts.md` and `sync.md` describe the rule; tests cover each branch and a three-machine sequence; the recovery-copy writer calls `fsutil.refuse_real_state_write_under_pytest` before its first mutation and joins the raw-writer census; the provisional `state.db` tables each name a reader or an explicit no-reader, and the pre-2026-10-19 Track IDs in `sync.md`'s history-log section, `statedb.py`, the routing README, `init-devices.md` and `tests/test_pullhistory.py` are retired (including `sync.md`'s deferred hold-escalation pointer, killed 2026-10-21 as unobserved); `./bin/check` passes.
_touches: src/mind_meld/statedb.py, src/mind_meld/cli.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, src/mind_meld/retention.py, tests/test_statedb.py, tests/test_merge.py, tests/test_additive_sync.py, tests/test_integration.py, tests/test_fsutil.py, docs/invariants/conflicts.md, docs/invariants/sync.md, README.md, docs/invariants/README.md, docs/invariants/init-devices.md, tests/test_pullhistory.py_
_blocked-by: Track 84A, Track 86A_
_out: 88A, 89A_
_read-first: docs/invariants/README.md, docs/invariants/conflicts.md, docs/invariants/sync.md_
_session: fresh · effort: high · verify: ./bin/check tests/test_statedb.py tests/test_merge.py tests/test_additive_sync.py tests/test_integration.py_

_Source: docs/archive/store-v2.md hazard rows "Every peer edit to an untouched file is a conflict" and "Line deletions in `MEMORY.md` and `.jsonl` never stick", re-checked at 408766d: `_apply_incoming_file` union-merges every update of a mergeable file, then keeps a newer local file or writes a conflict copy; pull never deletes local files (tombstones only stop re-download); `statedb` has an unused `ledger` table and no base._

- **Base table and the fast-forward rule** -- _src/mind_meld/statedb.py, src/mind_meld/cli.py, src/mind_meld/pullplan.py, src/mind_meld/merge.py, tests/test_statedb.py, tests/test_merge.py, tests/test_additive_sync.py, ~300 lines._ (L)
- **Propagated deletions with recovery** -- _src/mind_meld/cli.py, src/mind_meld/retention.py, tests/test_integration.py, docs/invariants/conflicts.md, docs/invariants/sync.md, README.md, ~150 lines._ (M)

#### Group 88: Syncthing status

_Depends on: Group 87_

**This group delivers:** Status shows Syncthing's state and each machine's latest push.

##### Track 88A: Report Syncthing state and each machine's latest push
_2 tasks . ~350 LOC . medium risk . 7 files_
**Outcome:** `mm status` tells the owner whether Syncthing is connected and in sync and whether this machine has applied each peer's latest push, and `mm diag` flags folder settings that would break sync.
**Supports:** MVP-1, docs/SPEC.md O2.2 and O3.2.
**Done when:** a new `syncthing.py` finds the local Syncthing configuration and reads its REST API read-only with timeouts (connected peers, folder state, and the folder options README lists); mm never writes Syncthing's configuration; pull records each peer's applied manifest time, and status shows each peer's last push and whether it is applied, warning in every mode when something has waited more than 24 h; diag lints the folder options and `.stignore`; a missing or unreachable Syncthing is reported, not an error; tests use a fake REST server; `./bin/check` passes.
_touches: src/mind_meld/syncthing.py (new), src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_syncthing.py (new), tests/test_diag.py, tests/test_module_boundaries.py, README.md, docs/invariants/README.md_
_blocked-by: Track 77A, Track 87A_
_out: 89A_
_read-first: docs/invariants/README.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_syncthing.py tests/test_diag.py tests/test_module_boundaries.py_

_Source: docs/SPEC.md O2.2 and O3.2; docs/archive/store-v2.md §10 folder options (current evidence; Syncthing option semantics verified against docs.syncthing.net on 2026-10-11 for the former Track 81A)._

- **Read-only Syncthing client and diag lint** -- _src/mind_meld/syncthing.py, src/mind_meld/cli.py, tests/test_syncthing.py, tests/test_module_boundaries.py, docs/invariants/README.md, ~200 lines._ (M)
- **Each peer's push in status** -- _src/mind_meld/cli.py, src/mind_meld/statedb.py, tests/test_diag.py, README.md, ~150 lines._ (M)

#### Group 89: Cutover runbook

_Depends on: Group 85, Group 87, Group 88_

**This group delivers:** A runbook and agent docs for moving the fleet onto Syncthing.

##### Track 89A: Write the cutover runbook and the Syncthing documentation
_2 tasks . ~400 LOC . low risk . 7 files_
**Outcome:** The owner has a step-by-step runbook to move the fleet from iCloud Drive to Syncthing and bring the Linux machine in, and every document an agent reads describes the Syncthing store.
**Supports:** MVP-1, docs/SPEC.md O1 to O4 (cutover and soak).
**Done when:** README covers installing Syncthing on macOS and Arch, pairing, the folder options mm relies on, and a cutover runbook (update every machine, stop hooks, move the store folder from iCloud Drive into the Syncthing folder, point `storage.path` at it, join the Linux machine with `mm init`, remove a dead machine's directories by hand, delete the iCloud folder two weeks later); AGENTS.md Key Principles and root SPEC.md's storage sections describe the Syncthing folder; `sync.md` and `init-devices.md` drop iCloud-only rules; `pyproject.toml` description and keywords drop iCloud; docs/SPEC.md's superseded-history entry for the store design names §13 (cutover) as evidence alongside §7, §10 and §11; `./bin/check` passes.
_touches: README.md, AGENTS.md, SPEC.md, docs/SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml_
_blocked-by: Track 85A, Track 87A, Track 88A_
_read-first: docs/SPEC.md, docs/archive/store-v2.md_
_session: fresh · effort: medium · verify: ./bin/check tests/test_docs_routing.py_

_Source: docs/SPEC.md O1 to O4 and the accepted manual cutover step; docs/archive/store-v2.md §11 (Linux) and §13 (cutover) as evidence._

- **Runbook and Syncthing setup** -- _README.md, ~250 lines._ (M)
- **Agent-facing docs** -- _AGENTS.md, SPEC.md, docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/README.md, pyproject.toml, ~150 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 79 ← {}
- Group 80 ← {79}
- Group 81 ← {79, 80}
- Group 82 ← {80, 81}
- Group 83 ← {82}
- Group 84 ← {83}
- Group 85 ← {84}
- Group 86 ← {85}
- Group 87 ← {84, 86}
- Group 88 ← {87}
- Group 89 ← {85, 87, 88}
```

Track detail per group:

```
Group 79: Compatibility contract and Muse family
  +-- Track 79A ........... ~L . 3 tasks
Group 80: Grok Build usage reader
  +-- Track 80A ........... ~L . 3 tasks
Group 81: Grok Build sync source
  +-- Track 81A ........... ~L . 3 tasks
Group 82: Git capture catch-up
  +-- Track 82A ........... ~L . 2 tasks
Group 83: One capture budget
  +-- Track 83A ........... ~L . 3 tasks
Group 84: Syncthing-tolerant reads
  +-- Track 84A ........... ~L . 2 tasks
Group 85: Fleet creation and join
  +-- Track 85A ........... ~L . 3 tasks
Group 86: Portable Claude paths
  +-- Track 86A ........... ~L . 2 tasks
Group 87: Fast-forward pulls
  +-- Track 87A ........... ~L . 2 tasks
Group 88: Syncthing status
  +-- Track 88A ........... ~L . 2 tasks
Group 89: Cutover runbook
  +-- Track 89A ........... ~L . 2 tasks
```

**Total: 2 phases . 11 groups . 11 tracks remaining.**

Nearly every Track edits `cli.py` and README, so the plan stays one serial chain; the explicit `_blocked-by` edges set its priority: host cleanup and usage capture first, then Syncthing.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on the one-week Mac plus Linux soak, with O6 to O8 holding and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance` after following Group 89's runbook. Deferred work, including Codex memory sync and per-path conflict policies, waits in Future with its triggers.

---

## Future

Deferred: docs/roadmap-future.md (17 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
