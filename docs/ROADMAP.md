# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Cursor, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Standalone Grok Build support was removed in v1.11.8; Groups 82–90, 94 and 97 and Track 91A (resumable capture, affected-test selection, the status, Codex, pull, init and source-command moves, one capture budget, Syncthing-tolerant reads, explicit fleet creation and join, machine-local gstack files, shared Claude project memory, recapture's own walk budget, fast-forward pulls and propagated deletions) shipped in v1.11.9–v1.12.3 and live in shipped history; Grok models used through Cursor stay in the Grok row. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); Group 96 qualifies it after the cutover.

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

Declared active (manager, 2026-11-02): Track 91B is in flight as factory item 53b6eda2, with no PR yet. Its ID, its owning Group 91 and its card are pinned. The bootstrap script (Track 91C until this regeneration) now waits for the Muse reader fix, so it leaves Group 91 and takes back its earlier ID, Track 98A.

#### Group 91: Honest usage remedy

_Depends on: none_

**This group delivers:** `mm status` stops prescribing an update that cannot help.

##### Track 91B: Stop telling the owner to update when the newest mm cannot read a host's records
_1 task . ~90 LOC . low risk . 3 files_
**Outcome:** When a host's records have a format this mm cannot read and no newer release is known, `mm status` and the push usage line say updating will not help and give the step that does; `mm update` stays the remedy when a newer release exists.
**Supports:** MVP-1, docs/SPEC.md O8.1 (a failure names the command that fixes it) and O7.3.
**Done when:** `statusflow._usage_capture_remedy`, `mm status`'s `Usage capture needs a newer mm` verdict and `events_tail`'s `run mm update before refreshing usage` read the cached upgrade view without refreshing it; a cached newer release, or none known, keeps `mm update`; this release being the newest known names the reader, says no release reads its format yet and gives the existing stop remedy and `mm diag`; `mm status` stays read-only; `tests/test_diag.py` pins all three cases; `./bin/check --affected` passes.
_touches: src/mind_meld/statusflow.py, src/mind_meld/events_tail.py, tests/test_diag.py_
_blocked-by: Track 97A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md_
_session: fresh · effort: low · verify: ./bin/check --affected_

_Source: observed 2026-11-01 on this Mac at mm 1.12.1, the newest tag, with Muse Code 1.4.14: `mm diag --json` shows `muse.last_reason: unsupported` since 2026-10-21, and `mm status` prints `Usage refresh: Run mm update; a consented reader has an unsupported record format.` `host_usage.PERMANENT_REASONS` holds only `unsupported`, and `_usage_capture_needs_upgrade` reads no version. Making the Muse reader read those records is a separate item in docs/TODOS.md._

- **Remedy follows the cached newest release** -- _src/mind_meld/statusflow.py, src/mind_meld/events_tail.py, tests/test_diag.py, ~90 lines._ (S)

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 94, 97_

### Phase 5: mm 2.0 on Syncthing

**End-state:** The owner's Macs and two Linux machines sync over one Syncthing folder that carries the 1.x store, with fast-forward pulls, propagated deletions, Muse usage read again, Syncthing state in status, a bootstrap script and a cutover runbook; the factory can hand over between Macs; docs/SPEC.md O1 to O4 are accepted on the owner's own observation after following the runbook, and released as 2.0.0.
**Groups:** 91, 99, 100, 98, 92, 95, 93

#### Group 99: Muse reader move

_Depends on: Group 91_

**This group delivers:** The Muse usage reader lives in its own adapter module, with no change in what it reads or reports, so the fix that follows touches one module.

##### Track 99A: Move the Muse usage reader into its own adapter module
_1 task . ~900 LOC . medium risk . 8 files_
**Outcome:** The Muse reader sits in `hosts/muse.py` behind the same adapter contract as the Codex reader, so Track 100A can change how Muse records are read without editing `host_usage.py`; nothing the owner sees changes.
**Supports:** MVP-1, docs/SPEC.md O7.1 (Muse usage), through the core-and-extensions constraint (a move-only Track just before a Track that changes a host adapter in `host_usage.py`).
**Done when:** `read_muse_usage`, `muse_usage_diag`, `muse_sessions_root`, `MUSE_SESSIONS_PATH`, `MUSE_CACHE_PATH`, `MUSE_USAGE_CENSUS_HOST_VERSION` and the Muse-only helpers (`_scan_muse_root`, `_freeze_muse_day`, `_validated_muse_frozen`, `_iter_muse_day_dirs`, `_muse_day_logs`, `_muse_now`, `_muse_turn_from_record`, `_validate_muse_counters` and the `_MUSE_*` constants) live in `src/mind_meld/hosts/muse.py`; callers in `host_registry.py`, `statusflow.py`, `events_tail.py` and the tests call them module-qualified; `tests/test_host_adapter_contract.py` runs its parametrized contract over Muse; `tests/test_module_boundaries.py` keeps `hosts/` modules importing neither `cli` nor each other; the Muse cache file, its format marker and every Muse total are unchanged, and `test_muse_contract_census_pin_matches_src_constant` reads the moved constant; the routing README and `events-retro.md` name the new module; `./bin/check` passes.
_touches: src/mind_meld/host_usage.py, src/mind_meld/hosts/muse.py (new), src/mind_meld/host_registry.py, src/mind_meld/statusflow.py, src/mind_meld/events_tail.py, tests/, docs/invariants/README.md, docs/invariants/events-retro.md_
_blocked-by: Track 91B_
_out: 100A_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md, tests/fixtures/host_sessions/muse/CONTRACT.md_
_produces: `hosts/muse.py` owns the Muse reader; `host_usage.py` keeps only shared helpers and the other readers._
_session: fresh · effort: medium · verify: ./bin/check_

_Source: the Future entry "Read Muse Code 1.4.14's usage records" (filed 2026-11-01, P1; deferred by PR #252 because the reader still lives in `host_usage.py`), promoted by Karl on 2026-11-02 ("ok sounds good"). Re-checked at 927d84a: the Muse reader is `host_usage.py`'s "Muse reader" section (lines 2334 to 3182, about 850 lines); `host_usage.`-qualified Muse symbols appear in `host_registry.py`, `statusflow.py`, `events_tail.py`, `tests/conftest.py` and eleven test files (`test_host_adapter_contract.py`, `test_module_boundaries.py`, `test_muse_usage.py`, `test_diag.py`, `test_host_usage.py`, `test_host_usage_snapshot.py`, `test_integration.py`, `test_silent_failure_contract.py`, `test_source_toggle.py`, `test_init_events_backfill.py` and `conftest.py`), so the card touches `tests/` as a directory to stay within the file cap. Pattern: Track 84A's Codex move (PR #232). Blocked by Track 91B because both edit `statusflow.py`, `events_tail.py` and `tests/test_diag.py`._

- **Move the reader behind the adapter contract** -- _src/mind_meld/hosts/muse.py, src/mind_meld/host_usage.py, callers and tests, ~900 lines._ (L)

#### Group 100: Muse usage read again

_Depends on: Group 99_

**This group delivers:** `retro-fleet`'s Muse row moves again, because the reader reads the records Muse Code writes now.

##### Track 100A: Find why the Muse reader refuses this Mac's records, re-census and fix it
_3 tasks . ~250 LOC . medium risk . 4 files_
**Outcome:** Attended pushes on this Mac read Muse Code's current usage records again, so `retro-fleet`'s Muse row is no longer frozen at 2026-10-19 and the cause of the refusal is written down.
**Supports:** MVP-1, docs/SPEC.md O7.1 (each consented host's usage is read) and the 2.0.0 release line, which needs O7 (O7.4: complete capture from every active machine).
**Done when:** the first task names, from a read-only run of the reader against this Mac's real `~/.local/share/muse/sessions` (mm caches and state untouched: a temporary `HOME` pointed at the real sessions tree, or an in-process call with a throwaway cache path), which refusal in `hosts/muse.py` fires and on which record shape, using key names, counts and dates only, never record content, and records the Muse Code version observed; `tests/fixtures/host_sessions/muse/CONTRACT.md` gains a new census of this Mac's corpus (file, envelope and `model_completed` counts, key sets, counter relations, models, duplicate-id count and span) with that host version, and `MUSE_USAGE_CENSUS_HOST_VERSION` matches it; the reader accepts the shape the census found, with synthetic tests that pin it and keep the existing refusals for malformed records; if the cause is a genuine contradiction between logs rather than a new shape, the Track stops before changing a counting rule and writes the finding up for the owner in the PR; the PR records a live run on this Mac after the fix where `mm diag --json` shows `muse.last_reason` cleared and `files_cached` equal to `files_on_disk`, across as many attended pushes or `mm recapture` runs as the capture budget needs; `./bin/check --affected` passes.
_touches: src/mind_meld/hosts/muse.py, tests/test_muse_usage.py, tests/fixtures/host_sessions/muse/, docs/invariants/events-retro.md_
_blocked-by: Track 99A_
_out: 98A_
_read-first: tests/fixtures/host_sessions/muse/CONTRACT.md, docs/invariants/events-retro.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: the same Future entry (observed 2026-11-01 on this Mac at mm 1.12.1 with Muse Code 1.4.14). Re-observed 2026-11-02 at mm 1.12.3: `mm diag --json` shows `muse.last_reason: unsupported` since 2026-10-21T18:25:41Z, `last_complete_at` 2026-10-19, 486 of 1,036 files cached, and the latest attempt `dropped:deadline`. CONTRACT.md was rewritten for Muse 1.4.14 shapes on 2026-10-28 (PR #237, v1.11.16) from synthetic cases only; its census is still the 2026-10-14 Muse 1.4.7 corpus. Muse refusals carry no cause today (`_incomplete("unsupported")` on a cross-log disagreement and `_ReadFailure("unsupported")` in record validation), so the cause is unread._

- **Read the cause on this Mac (read-only)** -- _no repository change; findings go into the census, ~0 lines._ (S)
- **Re-census the corpus into CONTRACT.md** -- _tests/fixtures/host_sessions/muse/CONTRACT.md, ~60 lines._ (S)
- **Teach the reader the census shape** -- _src/mind_meld/hosts/muse.py, tests/test_muse_usage.py, docs/invariants/events-retro.md, ~190 lines._ (M)

#### Group 98: Bootstrap script

_Depends on: Group 100_

**This group delivers:** One script readies a fresh Mac or Arch-based machine with Python, pipx, mm and the Syncthing package.

##### Track 98A: Add a bootstrap script that readies a machine for mm and Syncthing
_2 tasks . ~250 LOC . medium risk . 3 files_
**Outcome:** On a fresh Mac or Arch-based Linux machine, the owner runs one script and has Python, pipx, mm and the Syncthing package installed, then follows its printed next steps to pair machines, share the folder and run `mm init`.
**Supports:** MVP-1, docs/SPEC.md Constraints (the bootstrap script) and the 2.0.0 release line; Track 93A's runbook uses it.
**Done when:** `bin/bootstrap` on macOS stops with Homebrew's install instructions and a non-zero exit when `brew` is missing, and otherwise installs what is missing through Homebrew; on Arch-based Linux it installs what is missing with `pacman -S --needed`; any other system stops and names what is supported; it confirms Python 3.11 or newer and Syncthing 2.1 or newer, installs mm through pipx from `@latest` unless pipx already has it, and stops with the fix when `mm` on `PATH` is not pipx's copy; a rerun changes nothing and says so; it pairs no machine, shares no folder and writes no Syncthing configuration; success ends with those next steps, and a failure's last line says what broke and the one command that fixes it (O8); tests run it with fake `brew`, `pacman`, `pipx`, `python3`, `syncthing` and `mm` on `PATH` under a `tmp_path` home and cover macOS without Homebrew, macOS, Arch, an unsupported system, a rerun, a too-old Python or Syncthing and a shadowed `mm`; README's install section gives the one command that fetches and runs the script from the `latest` tag and says pairing and sharing stay manual; `./bin/check --affected` passes.
_touches: bin/bootstrap (new), tests/test_bootstrap.py (new), README.md_
_blocked-by: Track 97A, Track 100A_
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
- Group 91 ← {}
- Group 99 ← {91}
- Group 100 ← {99}
- Group 98 ← {100}
- Group 92 ← {98}
- Group 95 ← {92}
- Group 93 ← {98, 92, 95}
- Group 96 ← {93}
```

Track detail per group:

```
Group 91: Honest usage remedy          (in progress)
  +-- Track 91B ........... ~S . 1 task
Group 99: Muse reader move
  +-- Track 99A ........... ~L . 1 task
Group 100: Muse usage read again
  +-- Track 100A .......... ~L . 3 tasks
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

**Total: 8 groups . 8 tracks remaining.**

The owner put the Muse usage reader ahead of the bootstrap script on 2026-11-02, because Muse usage is frozen and 2.0.0 needs O7. Track 99A moves the reader out of `host_usage.py` first, as the core-and-extensions constraint requires, and waits for Track 91B because both edit `statusflow.py`, `events_tail.py` and `tests/test_diag.py`; Track 100A then reads the cause, re-censuses and fixes the reader in the moved module. The bootstrap script carries `_blocked-by: Track 100A` to hold that order (it shares no file with the Muse Tracks), so it leaves Group 91 and takes back Track 98A, its ID before PR #252. Tracks 98A, 92A, 95A and 93A all edit `README.md` and run in that order: 95A lands before the cutover runbook (93A) so the runbook can cover factory handover, and 98A lands before it so the runbook can use the script. Track 77A stays on Track 92A; the shipped Tracks 87A, 89A, 90A, 90B, 91A, 94A and 97A stay on the cards that named them. Track IDs 92A to 96A keep the meaning they had before this regeneration, because `docs/invariants/sync.md` and other documents cite them, which is why Group numbers do not follow execution order.

The 2.0.0 cut is not a Track. It is the owner's acceptance of docs/SPEC.md O1 to O4 on their own observation on one Mac and both Linux machines after following Group 93's runbook, with O6 to O8 holding, the bootstrap script shipped and a `retro-fleet` run showing complete capture from every active machine (O7.4), recorded in PROGRESS.md `## Acceptance`. Problems are fixed as they appear. Cross-model memory (O9) is qualified after the cutover by Group 96; its implementation waits in Future until that qualification passes.

---

## Future

Deferred: docs/roadmap-future.md (17 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
