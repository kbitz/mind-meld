# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive, and a first install sets Syncthing up itself when Syncthing still has its default configuration (SPEC O2). Tracks 117A to 120A have shipped (v1.12.25 to v1.12.29), which completes O2's setup, approval and per-machine move, and Track 122A (v1.12.30) has since replaced the tree's personal identifiers with placeholders; the Current Plan is empty. On 2026-11-08 the owner dropped the git-history hostname scrub (formerly Track 121A): GitHub keeps pull-request refs and cached views of old commits that a force-push cannot clean, so SPEC no longer makes it a condition of 2.0.0 or of making the repository public again, and the tree instead stays impersonal from here on (Track 122A; the owner dropped its device-name change on 2026-11-09, so `mm init` keeps its hostname default). The owner will decide the path to 2.0 and which Future items to pursue after using the Syncthing fleet for a while; the plan promotes nothing from Future. On 2026-11-09 the owner moved four machines (two Macs from iCloud Drive, a Linux desktop and an Omarchy laptop) to Syncthing; the eight findings from that move are deferred in Future (bootstrap upgrade and start-at-login, the private-repository install line, the key-header wait, the recapture advice loop, Antigravity's contradictory advice, recapture's nested-repository notices and the codex `skills/gstack/*` exclusion), and the manager's recommendation to take the bootstrap pair first waits for the owner's choice. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval and introducers on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration, removing dead machines and scan requests are refused until the spec's revisit trigger is observed.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line (spec O8, 2026-10-19).** A Track that adds output to push, pull or the auto commands must show it is the outcome, a failure with its fix, data at risk or an actionable warning; everything else goes to `mm status`, `mm diag` or `--verbose`.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-08). No branch, session or open pull request is active for any Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122_

None. Track 122A, the only Track the 2026-11-08 plan carried, shipped in v1.12.30 and Group 122 is retired, so nothing is queued. The owner will choose the next Track, and the path to 2.0, after using the Syncthing fleet for a while; Future holds the deferred work, including the findings from the 2026-11-09 move.

### Execution Map

No Groups remain.

**Total: 0 groups . 0 tracks remaining.**

Tracks 117A, 118A, 119A, 120A and 122A shipped (v1.12.25, v1.12.26, v1.12.28, v1.12.29 and v1.12.30; v1.12.27 was the test-suite retirement, not a Track) and Groups 117 to 120 and 122 are retired. Track 121A, the git-history hostname scrub, was dropped by the owner on 2026-11-08 and Group 121 is retired with it. IDs 95A and 96A stay unused because Future entries cite them.

The 2.0.0 cut is not a Track, and its path is open: the owner will decide how to get to 2.0 after using the Syncthing fleet for a while. SPEC's release line for 2.0.0 is O1 to O4 accepted on the owner's own observation on one Mac and both Linux machines in the Syncthing fleet, with O6 to O8 holding, recorded in PROGRESS.md `## Acceptance`. A Mac not yet moved keeps running on iCloud Drive and holds nothing up. Making the repository public again is a separate owner decision. Problems found while machines move are fixed as they appear.

---

## Future

Deferred: docs/roadmap-future.md (36 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
