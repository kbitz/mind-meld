# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Cross-model memory (O9) shipped through v1.12.84 (dedicated `mind-meld` qmd index) and achieved owner-fleet acceptance on 2026-11-24 ([PROGRESS.md](PROGRESS.md#acceptance)). The owner will decide the path to 2.0 after using the Syncthing fleet.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **A reproducible bug the owner actually encountered is admitted, not deferred (owner, 2026-11-09).** After the manager said the codex `skills/gstack/*` exclusion was not in the two Tracks being carded, the owner asked "why isn't that fix in our current roadmap? Anything that is a reproducible bug we actually encountered should be current". A Future entry that records a bug seen on the owner's machines, with a cause found in the code, goes to Current Plan on the next regeneration; deferral is for work nobody has hit, design questions and claims nobody verified, and the manifestation gate still prunes defects nobody has hit.
- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval, introducers and mesh step (adding a device a fleet member's encrypted record names; spec amended 2026-11-18) on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration and scan requests are refused until the spec's revisit trigger is observed. Track 154A shipped retirement removal and fleet-name correction on mm-managed machines.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) documented in the shared output helpers.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-27). No branch, session or open pull request is active for any Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168_

### Group 169: Transient pull progress and push project scan display

_Depends on: none_

**This group delivers:** continuous transient status-line progress through the end of `mm pull` (matching `mm push` instead of leaving a blank line with a blinking cursor) and live per-project scan status during `mm push`'s `"Scanning sources"` step.

#### Track 169A: Keep pull progress live to completion and show scanned projects during push
_2 tasks . ~150 LOC . low risk . 9 files_
_id: wi-cbceba_
**Outcome:** Attended `mm pull` keeps its transient terminal status line active with elapsed timing through post-apply cleanup, Syncthing mesh checks, memory-index refresh, and update checks instead of dropping to a blank blinking cursor before the `Pull:` line, and attended `mm push` shows each project as `"Scanning sources"` walks it on that same single transient line.
**Supports:** [SPEC](SPEC.md) O8 (transient terminal progress that clears before outcome lines, no new persistent lines, silent auto commands); developer request, 2026-11-27.
**Done when:** attended `mm pull` shows live per-step elapsed progress from `"Unlocking storage"` through `_pull_core` post-apply cleanup and `_attended_pull`'s post-pull tail (`meshflow.run`, `notice_pending_syncthing_join`, `_sync_memory_index`, `_sync_update` release check) without a blank gap before `Pull:`, attended `mm push` displays the project currently being scanned on the single transient `"Scanning sources"` line, `autopull` / `autopush` / `--verbose` / non-terminal output stay unchanged, and `./bin/check --affected` passes.
**Acceptance:**
1. Enable live per-step elapsed time for `mm pull` in `cli.py:_sync_outcome` (`include_elapsed=True`, matching `mm push`).
2. Eliminate the blank-line / blinking-cursor gap before `mm pull` finishes: stop `pullflow.py:_pull_core`'s `finally:` from permanently calling `core._stop_sync_progress()` when `_SYNC_OUTPUT` owns the spinner (pause around `_print_pull_summary` instead, stopping only when `_SYNC_OUTPUT` is absent); add a post-apply finishing step in `_pull_core` covering deletions, retired Codex copy cleanup, `MEMORY.md` tidying, `fsync`, ledger flush/commit, and cursor writes; and keep the spinner alive through `cli.py:_attended_pull`'s post-`_pull_core` tail (`meshflow.run`'s `"Checking Syncthing fleet devices"`, `pullflow.notice_pending_syncthing_join`, `_sync_memory_index` / `memoryindex.refresh`, and `_sync_update`'s release check, pausing or stopping cleanly before `_installer_progress`, prompts, warnings, and final outcome lines). Keep `cli.py:push`'s tail (`_sync_memory_index` / `_sync_update`) consistent with the same lifecycle so neither command drops to a blank cursor during post-sync tail work.
3. Add an optional read-only `on_progress` callback to `manifest.build_manifest_v2`, `walk_source`, and the walkers (`walk_claude_source`, `walk_generic_source`, `walk_grok_source`, and source-level reporting for the remaining walkers), wired from `cli.py:_push_core`'s `"Scanning sources"` step only when attended transient progress is active, so the single transient status line updates with the source/project currently being scanned (for example `claude` `projects/<dir>`, `gstack` `projects/<slug>`, `grok` `memory-v2/<scope>`). Sanitize displayed names via `safety.safe_terminal_str` and throttle redraws through `_SyncProgress`'s existing non-forced 10 Hz render gate so rapid updates neither corrupt the terminal nor slow the scan.
4. Preserve SPEC O8 and existing contracts: zero new persistent lines on stdout or stderr, `autopull` and `autopush` remain silent, `--verbose` and non-terminal runs stay unaffected, `manifest.py` keeps its leaf/lower-layer import boundaries (`tests/test_module_boundaries.py`), and exception/summary teardown invariants in `docs/invariants/sync.md` hold.
5. Add targeted tests in `tests/test_integration.py`, `tests/test_pullflow.py`, and `tests/test_manifest.py` pinning pull's post-apply and post-`_pull_core` progress continuity, elapsed display, push's per-project scan callback/rendering and sanitization, and silent hook/non-terminal behavior; update `docs/invariants/README.md` and `docs/invariants/sync.md` for any new exported progress symbol.

_touches: src/mind_meld/core.py, src/mind_meld/cli.py, src/mind_meld/pullflow.py, src/mind_meld/manifest.py, tests/test_integration.py, tests/test_pullflow.py, tests/test_manifest.py, docs/invariants/README.md, docs/invariants/sync.md_
_read-first: docs/SPEC.md, docs/invariants/README.md, docs/invariants/sync.md, src/mind_meld/core.py (_SyncProgress), src/mind_meld/pullflow.py (_pull_core), src/mind_meld/cli.py (_sync_outcome, _attended_pull, _push_core), src/mind_meld/manifest.py (build_manifest_v2)_
_verify: ./bin/check --affected_

_Source: developer request, 2026-11-27 ("make mm pull similarly verbose to mm push ... nothing on the status line, just a blinking cursor" and "scanning sources portion of mm push should display the projects it's scanning"); verified against 5f7f691._

- **Keep transient pull progress alive through post-apply and post-pull tail steps.** Enable elapsed timing on pull, keep `_SyncProgress` alive past `_pull_core` through `meshflow.run`, `notice_pending_syncthing_join`, `_sync_memory_index`, and `_sync_update` (and align push's post-lock tail), and add pull progress tests. _src/mind_meld/core.py, src/mind_meld/cli.py, src/mind_meld/pullflow.py, tests/test_pullflow.py, tests/test_integration.py, ~85 lines._ (M)
- **Show scanned projects on push's transient `"Scanning sources"` status line.** Add a read-only `on_progress` callback to `manifest.py` walkers, wire it from `_push_core` into a sanitized non-forced `_SyncProgress` update, add manifest/integration tests, and update invariant routing docs if new symbols are added. _src/mind_meld/manifest.py, src/mind_meld/core.py, src/mind_meld/cli.py, tests/test_manifest.py, tests/test_integration.py, docs/invariants/README.md, docs/invariants/sync.md, ~65 lines._ (M)

### Execution Map

```
- Group 169 ← {}
```

**Total: 1 group, 1 Track remaining.**

## Future

Deferred: docs/roadmap-future.md (47 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
