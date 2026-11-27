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
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval and mesh step (adding a device a fleet member's encrypted record names, clearing stale introducer flags on existing fleet peers mm paired, removing retired devices and correcting fleet device names; spec amended 2026-11-18 and 2026-11-27 to drop introducer marks on mm-managed machines) on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration and scan requests are refused until the spec's revisit trigger is observed. Track 154A shipped retirement removal and fleet-name correction on mm-managed machines.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) documented in the shared output helpers.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.
- **Factory card conventions (2026-11-27).** Every Current Plan card keeps a `_tier: routine|standard|novel_` line. Any `_blocked-by:` line in Current Plan names Track IDs (for example `_blocked-by: 169A_`), never work-item IDs (`wi-...`), or is omitted when Group order already covers the dependency.

---

## In Progress

None declared (2026-11-27). No branch, session or open pull request is active for any Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170_

None. Tracks 169B (v1.12.91, [PR #394](https://github.com/kbitz/mind-meld/pull/394), `4562a8c`), 169A (v1.12.92, [PR #395](https://github.com/kbitz/mind-meld/pull/395), `5957cef`), and 170A (v1.12.93, [PR #396](https://github.com/kbitz/mind-meld/pull/396), `86e1bcd`) merged, and Groups 169 and 170 are retired (removing Track 170A's work-item-ID dependency line). All 20 review test-gap intake records under `docs/todos/` were drained to Future, and re-checking the 47 existing Future entries found no encountered reproducible bug with a cause found in the code and no met revisit trigger that admits a Track. The owner will choose the next Track, and the path to 2.0, after using the Syncthing fleet.

### Execution Map

No Groups remain.

**Total: 0 groups . 0 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (67 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
