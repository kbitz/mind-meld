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

Declared 2026-11-24 by the Factory Manager's brief: Track 165C is in planning (item `715b9168`, branch `mmm/715b9168`), split from Track 165A (item `67e29a92`, draft PR #382 on branch `mmm/67e29a92`, which stays open as reference material) on the developer's word, 2026-11-24: "split is fine". Its prerequisite Track 165B shipped in v1.12.86 (PR #383).

### Group 165: Background push-capture catch-up

_Depends on: none_

**This group delivers:** background catch-up for deadline-stopped autopush capture so usage and Git capture stay complete without slowing agent hooks or requiring manual attended pushes.

#### Track 165C: Finish deadline-stopped autopush capture in the background (re-plan)
_2 tasks . ~300 LOC . medium risk . 14 files_
**Outcome:** When an autopush stops usage or Git capture at its deadline, the remainder finishes in the background without the owner running an attended push and without a hook waiting.
**Supports:** [SPEC](SPEC.md) O7 (fleet usage reporting) and O8 (silent hooks and honest status). Split from Track 165A (item `67e29a92`, draft PR #382) on the developer's word, 2026-11-24: "split is fine". Track 165B shipped the timing and per-provider deadline half in v1.12.86 (PR #383); this Track re-plans the background catch-up half from a simpler design (item `715b9168`, branch `mmm/715b9168`, currently in planning; draft PR #382 stays open as reference material).
**Done when:** set by the active plan for item `715b9168`; at minimum, a deadline-stopped autopush hands off the remainder, at most one catch-up runs per machine, the catch-up never prompts, prints nothing and never changes the autopush exit code, and `mm status` / `mm diag` show its last result.
**Acceptance (starting points for active planning item `715b9168`; final criteria set by the plan):**
1. Prefer a capture-only catch-up: it advances caches and cursors with the attended budgets, and the next push (auto or attended) publishes. Weigh this against Track 165A's reason for publishing in the catch-up (the day's last session staying unpublished overnight), and state which the plan chooses and why.
2. Read draft PR #382's review receipts and its branch's `docs/TODOS.md` follow-ups (the four remaining catch-up follow-ups, after Track 165B resolved timing record duplication and warm-retry ceiling) as the list of failure modes the design must rule out, and name each one in the plan with how the design avoids it.
3. `mm status` stops advising a manual attended push for a backlog the catch-up is handling or has just handled, without hiding any other degradation (O8).
4. Build on Track 165B's timing records (`pushtimings.py`) to measure catch-up runs.

_blocked-by: Track 165B_
_touches: src/mind_meld/events_tail.py, src/mind_meld/cli.py, src/mind_meld/config.py, src/mind_meld/statusflow.py, src/mind_meld/events.py, src/mind_meld/pushtimings.py, tests/test_events.py, tests/test_events_budget_scope.py, tests/test_host_usage_snapshot.py, tests/test_integration.py, tests/test_statusflow.py, tests/test_diag.py, README.md, docs/invariants/events-retro.md_
_read-first: docs/invariants/events-retro.md, docs/invariants/sync.md (autopush, detached memory-index refresh), AGENTS.md, PR #382 (diff, review receipts, TODOS additions)_
_verify: ./bin/check --affected_

_Source: developer decision, 2026-11-24 ("split is fine"); Track 165A item `67e29a92` and draft PR #382 (branch `mmm/67e29a92`); active planning item `715b9168` (branch `mmm/715b9168`)._

- **Re-plan and implement background catch-up handoff and execution.** Hand off deadline-stopped autopush capture to a single-instance quiet background catch-up using Track 165B timing records, avoiding the concurrency failure modes documented on PR #382. _events_tail.py, cli.py, config.py, events.py, pushtimings.py, tests, ~180 lines._ (M)
- **Update status, diagnostics and documentation for background catch-up.** Suppress manual-push advice while catch-up handles a backlog, report the last catch-up result in `mm status` / `mm diag`, and update README and invariants. _statusflow.py, tests, README.md, docs/invariants/events-retro.md, ~120 lines._ (M)

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164_

No unstarted Groups queued. Track 165C (Group 165 above) is the only active Track.

### Execution Map

```
- Group 165 ← {}
```

**Total: 1 group, 1 Track remaining.**

---

## Future

Deferred: docs/roadmap-future.md (136 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
