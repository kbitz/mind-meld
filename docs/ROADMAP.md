# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Owner-fleet acceptance for O1 through O4, O7, and O9 is recorded in [PROGRESS.md](PROGRESS.md#acceptance), with O6 through O8 holding and bootstrap plus O2 setup shipped; one encountered bug Track remains in Current Plan before the 2.0.0 release.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **A reproducible bug the owner actually encountered is admitted, not deferred (owner, 2026-11-09).** After the manager said the codex `skills/gstack/*` exclusion was not in the two Tracks being carded, the owner asked "why isn't that fix in our current roadmap? Anything that is a reproducible bug we actually encountered should be current". A Future entry that records a bug seen on the owner's machines, with a cause found in the code, goes to Current Plan on the next regeneration; deferral is for work nobody has hit, design questions and claims nobody verified, and the manifestation gate still prunes defects nobody has hit.
- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval and mesh step (adding a device a fleet member's encrypted record names, clearing stale introducer flags on existing fleet peers mm paired, removing retired devices and correcting fleet device names; spec amended 2026-11-18 and 2026-11-27 to drop introducer marks on mm-managed machines) on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration and scan requests are refused until the spec's revisit trigger is observed. Track 154A shipped retirement removal and fleet-name correction on mm-managed machines.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) documented in the shared output helpers.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.
- **Factory card conventions (2026-11-27).** Every Current Plan card keeps a `_tier: routine|standard|novel_` line. Any `_blocked-by:` line in Current Plan names Track IDs (for example `_blocked-by: 169A_`), never work-item IDs (`wi-...`), or is omitted when Group order already covers the dependency.

---

## In Progress

None declared (2026-11-28). No branch, session or open pull request is active for any Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170_

### Group 171: Accurate usage-published reporting on attended `mm push`

_Depends on: none_

**This group delivers:** honest usage reporting on attended `mm push`, so the `Push:` outcome line prints `; usage published` only when published usage totals changed since this machine's last published snapshot instead of on every run.

#### Track 171A: Report `; usage published` on attended `mm push` only when usage totals changed
_3 tasks . ~135 LOC . low risk . 10 files_
_id: wi-57dfef_
_tier: standard_
**Outcome:** Attended `mm push` prints `; usage published` on its `Push:` outcome line only when the published usage totals changed since this machine's last published snapshot; back-to-back pushes on an idle machine with no new agent usage omit the usage clause beside content, metadata, selection, or repair updates and print `; no new usage` when nothing else changed (or when readers are catching up without new usage), matching [SPEC](SPEC.md) O8.
**Supports:** [SPEC](SPEC.md) O8 (O8.1: one line per thing that actually happened and nothing for what didn't happen; O8.2) and O7 (accurate fleet usage reporting); encountered bug standing constraint (developer report: `"it seems like mm push says that there's new usage published no matter what..."`).
**Done when:** a probe of two consecutive attended `mm push` runs on an idle machine (isolated temp `HOME` or the real fleet) confirms the before-fix behavior and identifies every path setting `PushResult.usage_published = True` on unchanged usage; after the fix, consecutive attended pushes with unchanged agent usage print `; usage published` only when published usage totals actually changed since this machine's last published snapshot (otherwise omitting the clause beside content changes or printing `; no new usage` on idle pushes or when readers are catching up without new usage); `PushResult.host_usage_published`, `attemptlog`, `mm status`/`mm diag` publication receipts, catching-up reader suffixes, queued catch-up publication, `--verbose` diagnostics, and silent `mm autopush` stay intact; targeted tests and docs pin the updated behavior; and `./bin/check --affected` passes.

Verified at `0182968`: in `src/mind_meld/cli.py`, `_sync_success` appends `; usage published{catchup_suffix}` whenever `result.usage_published` is true and only emits `; no new usage{catchup_suffix}` when `result.usage_published` is false (and the lead is `"no content changes"` or `result.catching_up_readers` is non-empty). On every attended non-dry-run push, `_push_core` calls `_capture_attended_usage` -> `events_tail._capture_host_snapshot` -> `events.make_host_usage_snapshot` (stamped with a fresh `ts`) and unconditionally appends the row via `events.write_push_event`, setting `captured.appended`. That modifies the daily `mm-events` JSONL file, trips the substantive gate, and after manifest acceptance `_report_usage_publication` returns `True` and sets `result.host_usage_published = True` and `result.usage_published = True` (and similarly on the early no-substantive path); separately, `_has_new_event_bytes` marks `usage_published = True` on any non-`unverified_host_path` `mm-events` file hash change.

1. Probe two consecutive attended `mm push` runs on an idle machine (isolated temp `HOME` per `AGENTS.md` `## Testing`, or observing the real fleet) and trace what changes between push 1 and push 2 across `_capture_attended_usage`, `events.write_push_event`, `_has_new_event_bytes`, `events_tail._run_events_tail`, and `_report_usage_publication`, confirming every code path that flips `PushResult.usage_published` to `True` when agent usage totals have not changed.
2. Fix usage-change detection so `PushResult.usage_published` is `True` only when published usage totals changed since this machine's last published snapshot (comparing the published host usage totals / coverage state against this machine's prior published `host-usage-snapshot` via `events.latest_event_rows` or prior accepted manifest state, and verifying how `events_tail._run_events_tail` / `_has_new_event_bytes` treat unchanged `sessions-snapshot` / `git-snapshot` vs queued catch-up rows), while keeping `PushResult.host_usage_published`, `attemptlog`, `mm status`/`mm diag` publication proof (`project_host_publication`), `catching_up_readers`, `--verbose`, and silent `mm autopush` intact.
3. Add regression tests in `tests/test_integration.py` (and unit tests in `tests/test_events.py` / `tests/test_reader_publication.py` as needed) pinning back-to-back idle attended pushes, idle push after real usage growth, content-only push with unchanged usage, queued catch-up publication, and catching-up readers without new usage; update `docs/invariants/sync.md`, `docs/invariants/events-retro.md`, `docs/invariants/README.md`, and `README.md` to match the changed-usage rule.

_touches: src/mind_meld/cli.py, src/mind_meld/events.py, src/mind_meld/events_tail.py, tests/test_integration.py, tests/test_reader_publication.py, tests/test_events.py, docs/invariants/README.md, docs/invariants/sync.md, docs/invariants/events-retro.md, README.md_
_read-first: docs/SPEC.md, docs/invariants/README.md, docs/invariants/sync.md, docs/invariants/events-retro.md, src/mind_meld/cli.py (_sync_success, _capture_attended_usage, _report_usage_publication, _has_new_event_bytes, _push_core), src/mind_meld/events.py (make_host_usage_snapshot, latest_event_rows, project_host_publication), src/mind_meld/events_tail.py (_capture_host_snapshot, _run_events_tail)_
_verify: ./bin/check --affected_

_Source: developer bug report relayed by Factory Manager, 2026-11-28 ("it seems like mm push says that there's new usage published no matter what..."); verified against 0182968._

- **Probe two consecutive idle attended pushes to confirm what sets `usage_published`.** Reproduce back-to-back attended `mm push` runs on an idle machine (isolated temp `HOME` or real fleet) and trace `_capture_attended_usage`, `_has_new_event_bytes`, `_run_events_tail`, and `_report_usage_publication` so the fix targets observed behavior. _src/mind_meld/cli.py, src/mind_meld/events_tail.py, tests/test_integration.py, ~25 lines._ (S)
- **Set `PushResult.usage_published` only when published usage totals changed since the last published snapshot.** Compare published usage totals against this machine's last published snapshot in `events.py` / `cli.py` (and `events_tail.py` if needed) without breaking `host_usage_published`, `attemptlog`, `mm status`/`mm diag` receipts, `catching_up_readers`, `--verbose`, or silent `autopush`. _src/mind_meld/cli.py, src/mind_meld/events.py, src/mind_meld/events_tail.py, ~65 lines._ (M)
- **Add back-to-back idle-push regression tests and update invariant docs and README.** Pin idle consecutive pushes, real usage growth, content-only pushes with unchanged usage, and catching-up readers without new usage, and align `docs/invariants/{sync,events-retro,README}.md` and `README.md`. _tests/test_integration.py, tests/test_reader_publication.py, tests/test_events.py, docs/invariants/README.md, docs/invariants/sync.md, docs/invariants/events-retro.md, README.md, ~45 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not gating.

Adjacency list (from the packer / GROUP_DEPS — not document order):
```
- Group 171 ← {}
```

Track detail per group:
```
Group 171: Accurate usage-published reporting on attended `mm push`
  +-- Track 171A ........... ~M . 3 tasks
```

**Total: 1 group . 1 track remaining.**

---

## Future

Deferred: docs/roadmap-future.md (54 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
