# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). The output fixes and the findings the developer approved from the 2026-11-15 full review (Tracks 135A to 147A), the real-machine automatic-update observation (135B) and the pending join request shown on fleet machines (148A) all shipped by v1.12.66 (independent receipts in shipped history). The plan below carries the three bugs the owner hit during the 2026-11-18 acceptance checks on the Syncthing fleet (Group 149). The owner will decide the path to 2.0 after using the Syncthing fleet for a while. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **A reproducible bug the owner actually encountered is admitted, not deferred (owner, 2026-11-09).** After the manager said the codex `skills/gstack/*` exclusion was not in the two Tracks being carded, the owner asked "why isn't that fix in our current roadmap? Anything that is a reproducible bug we actually encountered should be current". A Future entry that records a bug seen on the owner's machines, with a cause found in the code, goes to Current Plan on the next regeneration; deferral is for work nobody has hit, design questions and claims nobody verified, and the manifestation gate still prunes defects nobody has hit.
- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval and introducers on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration, removing dead machines and scan requests are refused until the spec's revisit trigger is observed.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) documented in the shared output helpers.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-18). No branch, session or open pull request is active for any Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148_

Group 149 carries the three bugs the owner hit on 2026-11-18 during the 2.0.0 acceptance checks (docs/SPEC.md O1 to O4) on mm 1.12.64, admitted under the standing constraint that a reproducible bug seen on the owner's machines goes to Current Plan. They share `events_tail.py`, `cli.py`, the invariant docs and the integration tests, so they ride one Track.

### Group 149: Quiet automatic push and machine-neutral output

_Depends on: none_

**This group delivers:** automatic push completes the host-usage reads it already makes on the owner's machines instead of recording `degraded` on every run, an attended push says so when it records something the owner must act on, and Linux machines are no longer called "this Mac".

#### Track 149A: Size autopush host reads from measured reads, print attended owner actions, and name machines rather than Macs
_3 tasks . ~320 LOC . medium risk . 24 files_
**Outcome:** On the owner's Macs and Linux machines, `mm autopush` finishes its Codex and Antigravity reads at their measured warm sizes, so `mm status` stops opening with "Latest push needs attention (result-degraded)" after ordinary agent sessions; an attended `mm push` that records an owner action prints it before its outcome line; and output on a Linux machine says "this machine" where it now says "this Mac".
**Supports:** MVP-1 on Syncthing (2.0.0 acceptance on one Mac and both Linux machines); docs/SPEC.md O7 (complete usage), O8.1 and O8.3 (print only when the owner must act; auto commands quiet), and the supported platforms (macOS and Arch-based Linux); the developer's decision of 2026-11-18 ("go for the docs worker") to card these bugs.
**Done when:** the acceptance list below passes: tests pin autopush defaults that complete readers at the measured warm read times, the attended push prints a recorded owner action, and no user-facing string calls the current machine a Mac.
_touches: src/mind_meld/config.py, src/mind_meld/events_tail.py, src/mind_meld/cli.py, src/mind_meld/statusflow.py, src/mind_meld/recaptureflow.py, src/mind_meld/pullflow.py, src/mind_meld/core.py, src/mind_meld/diffflow.py, src/mind_meld/sourceflow.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_config.py, tests/test_host_usage_snapshot.py, tests/test_silent_failure_contract.py, tests/test_integration.py, tests/test_diag.py, tests/test_pullflow.py, tests/test_pull_helpers.py, tests/test_recovery.py, tests/test_root_probers.py, tests/test_sourceflow.py, tests/test_statusflow.py, README.md, docs/invariants/events-retro.md, docs/invariants/sync.md_
_read-first: docs/invariants/README.md, docs/invariants/events-retro.md (the Track 101A paragraph, "One capture budget", and the `_run_events_tail` degradations contract), docs/invariants/sync.md (attended output rules), src/mind_meld/config.py (`DEFAULT_HOST_USAGE_AUTOPUSH_BUDGET_MS`), src/mind_meld/events_tail.py (`resolve_host_read_budget`, `HOST_READER_GRACE_MS`, `_capture_host_snapshot`, `_host_skip_phrase`, `_CURSOR_HOLD_DEGRADATION`), src/mind_meld/events.py (`CursorResolution`, `resolve_push_cursor`), src/mind_meld/cli.py (`_sync_success`)_
_session: fresh · effort: high · verify: ./bin/check --affected_

_Source: the 2026-11-18 brief from the Factory Manager, relaying the developer's decision; observed on `mac-a` and `linux-a` running mm 1.12.64; premises re-verified at `1862ae1`. (1) On `mac-a`, `mm status` showed Codex "Last read allowed 250 ms; last complete read 266 ms ... 7536 of 7536 rollouts cached" and Antigravity "Last read allowed 50 ms; last complete read 104 ms ... 500 of 500 conversation DBs cached"; on `linux-a` the 01:28 UTC autopush skipped codex and agy on their deadlines, and Antigravity read "allowed 50 ms; last complete 153 ms". Every autopush then records `degraded`, and `mm status` on every machine opens with "Latest push needs attention (result-degraded); recovery is unproven. Run: mm diag". At `1862ae1` the autopush sweep is `config.DEFAULT_HOST_USAGE_AUTOPUSH_BUDGET_MS = 250` for all readers together, a later reader gets `events_tail.HOST_READER_GRACE_MS = 50`, and the deadline remedy in `events_tail._host_skip_phrase` tells the owner to raise `[retro] host_usage_autopush_budget_ms`. The developer's brief puts the fix in mm's defaults or budget logic, not in asking the owner to raise settings. Cross-document disagreement, resolved by this card: events-retro.md's Track 101A paragraph says autopush stays at "250 ms, 50 ms grace, never warms: no small budget completes Codex", from a 2026-11-02 warm Codex read of 740 to 820 ms; after Track 105A's settled-aggregate memoization `mac-a` measures 266 ms for all 7,536 rollouts, so a small budget now can complete it. (2) On `linux-a`, `mm status` printed "Continue capture on this Mac: mm push" (`statusflow.py`); at `1862ae1` `rg -n "this Mac|other Macs|every Mac|another Mac" src/` finds the same wording in printed lines, help text and degradation phrases in `statusflow.py`, `recaptureflow.py`, `pullflow.py`, `events_tail.py` (`_ROOT_DISCOVERY_EMPTY_DEGRADATION`), `core.py`, `cli.py` and `sourceflow.py`, plus the installed `retro_fleet/SKILL.md`. (3) On `linux-a`, an attended `mm push` printed only "Push: 4 content changes published." while `mm status` and `mm diag` recorded the same run as `degraded: result-degraded` with "retro cursor held at last complete capture. Run mm diag, then mm recapture 30d"; at `1862ae1` that phrase reaches only `token_usage._diagnostic` (a verbose-only sink on attended push), and `CursorResolution.held` means this push already walks from the last complete capture, so whether a hold is an owner action at all is part of the fix. Bug 3 is folded here because it shares `events_tail.py`, `cli.py` and the integration tests. Future test gaps whose `_touches:` trigger this card meets (among them "Deadline diagnostic fallback preserves a truthful remedy", "Peer cursor invalid timestamps", "Pending join notice beside folder settings warnings" and "mm diff's ... lines are unpinned") stay deferred: the card is at its session-weight limit, and the brief keeps Future unchanged._

- **Autopush host reads fit their measured warm reads** -- Re-derive the autopush sweep default and the later-reader floor from the reads above (Codex 266 ms over 7,536 rollouts, Antigravity 104 ms and 153 ms over 500 DBs), with headroom for corpus growth, keeping O8.2's rule that automatic commands keep small budgets and never wait on the attended cap; keep config validation (100 to 5,000 ms, at most the attended ceilings) consistent with the new default. The deadline remedy stops presenting `[retro] host_usage_autopush_budget_ms` as the owner's fix for a read the default should cover; a deadline still records `degraded` or catching-up exactly as today. Update events-retro.md's Track 101A paragraph and the README budget rows. _src/mind_meld/config.py, src/mind_meld/events_tail.py, tests/test_config.py, tests/test_host_usage_snapshot.py, tests/test_silent_failure_contract.py, README.md, docs/invariants/events-retro.md, ~150 lines._ (M)
- **An attended push prints the owner action it records** -- When an attended `mm push` records a degraded run whose detail names an owner action, print one `mm: notice:` line with that fix before the outcome line, per O8.1; and decide from `resolve_push_cursor` whether a held cursor whose current capture completed can miss commits: if not, record it without the `mm recapture 30d` remedy (and without `degraded`), otherwise keep the remedy and print it. _src/mind_meld/events_tail.py, src/mind_meld/cli.py, tests/test_silent_failure_contract.py, tests/test_integration.py, docs/invariants/sync.md, ~60 lines._ (S)
- **Name machines, not Macs** -- In printed output, help text, degradation phrases and the installed retro-fleet skill text, replace "this Mac", "this Mac's", "other Macs", "every Mac" and "another Mac" with machine-neutral wording; leave code comments, internal docstrings and statements that are about macOS itself (Keychain, iCloud Drive) alone; update the tests and invariant or README passages that quote the old strings. _src/mind_meld/statusflow.py, src/mind_meld/recaptureflow.py, src/mind_meld/pullflow.py, src/mind_meld/events_tail.py, src/mind_meld/core.py, src/mind_meld/cli.py, src/mind_meld/diffflow.py, src/mind_meld/sourceflow.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_config.py, tests/test_diag.py, tests/test_integration.py, tests/test_pullflow.py, tests/test_pull_helpers.py, tests/test_recovery.py, tests/test_root_probers.py, tests/test_sourceflow.py, tests/test_statusflow.py, docs/invariants/events-retro.md, ~110 lines._ (M)

**Acceptance:**

1. A test runs the autopush host sweep with readers taking the measured warm times (Codex 266 ms, then Antigravity 153 ms) under the default budget and finds both complete, with no `deadline` drop and no `degraded` breadcrumb.
2. A reader that still overruns the default records `deadline` and `degraded` (or catching-up) as before, and its status line no longer tells the owner to raise `host_usage_autopush_budget_ms` as the fix; a test pins the text.
3. Config validation accepts the new default and still rejects out-of-range values; events-retro.md and the README state the new defaults and the measured reads behind them.
4. An attended `mm push` that records a degraded run with an owner action prints one `mm: notice:` line naming the fix before its `Push:` outcome; a held cursor whose capture completed either prints its remedy or is no longer recorded as degraded, as the cursor analysis decides; tests pin both.
5. `rg -n "this Mac|other Macs|every Mac|another Mac" src/` finds only code comments and internal docstrings, and tests pin the new status and recapture lines.
6. `./bin/check --affected` passes, and the full `./bin/check` passes in CI.

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of document order; document order is priority, not gating.

Adjacency list (from the packer / GROUP_DEPS — not document order):
```
- Group 149 ← {}
```

Track detail per group:
```
Group 149: Quiet automatic push and machine-neutral output
  +-- Track 149A ......... ~XL . 3 tasks
```

**Total: 1 group . 1 track remaining.**

Tracks 135A, 135C, 136A, 137A, 138B, 138A, 139A, 140A, 141A, 142A, 143A, 144A, 145A, 146A, 147A and 148A shipped in v1.12.51 to v1.12.66, and Track 135B (Track 124B's real-machine automatic-update observation) merged as a documentation-only change between them (independent receipts in shipped history). IDs 95A and 96A stay unused because Future entries cite them.

The 2.0.0 cut is not a Track, and its path is open. SPEC's release line for 2.0.0 is O1 to O4 accepted on the owner's own observation on one Mac and both Linux machines in the Syncthing fleet, with O6 to O8 holding, recorded in PROGRESS.md `## Acceptance`, which records no such checkpoint yet. A Mac not yet moved keeps running on iCloud Drive and holds nothing up. Problems found while machines move are fixed as they appear; Group 149 is the first batch of them. Retiring the pre-v0.9.2 conflict format, which the developer approved for 2.0, waits in Future for that line.

---

## Future

Deferred: docs/roadmap-future.md (76 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
