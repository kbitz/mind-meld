# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive, and a first install sets Syncthing up itself when Syncthing still has its default configuration (SPEC O2). Tracks 117A to 120A have shipped (v1.12.25 to v1.12.29), which completes O2's setup, approval and per-machine move. On 2026-11-08 the owner dropped the git-history hostname scrub (formerly Track 121A): GitHub keeps pull-request refs and cached views of old commits that a force-push cannot clean, so SPEC no longer makes it a condition of 2.0.0 or of making the repository public again, and the tree instead stays impersonal from here on (Track 122A). The owner will decide the path to 2.0 and which Future items to pursue after using the Syncthing fleet for a while; this plan promotes nothing from Future. Cross-model memory is planned after 2.0 ([docs/SPEC.md](SPEC.md) O9, [design](designs/cross-model-memory.md)); its qualification, Track 96A, waits in Future for 2.0.0's acceptance.

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

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121_

### Group 122: Impersonal tree

_Depends on: none_

**This group delivers:** The repository's current files carry placeholders instead of the owner's personal identifiers, and `mm init` stops proposing the machine's hostname as its device name; six of `mm init`'s Syncthing setup paths gain integration tests.

#### Track 122A: Replace personal identifiers in the tree with placeholders, and stop defaulting the device name to the hostname
_4 tasks . ~650 LOC . low risk . 26 files_
**Outcome:** Anyone reading the repository's current files, tests and fixtures finds placeholders (`/Users/user/`, `-Users-user-`, `user@example.com`, "the owner", role names such as `mac-a`) where the owner's username, email, company name or first name used to be, and a fresh `mm init` no longer offers the machine's hostname as the device name it publishes to the fleet. Six `mm init` Syncthing setup paths whose lower-level behavior was already tested now have init integration tests that pin their stops and the absence of writes.
**Supports:** MVP-1; docs/SPEC.md Constraints (files name machines by role; code, tests, fixtures and docs use placeholders), as amended 2026-11-08 when the owner dropped the git-history scrub in favour of keeping the tree impersonal from here on.
**Done when:** a search of every tracked file, `docs/archive/` included, for each kind of personal identifier (the owner's username in real paths such as `/Users/<user>/` and in Claude-encoded `-Users-<user>-` project folders, a real email address, a company name, the owner's first name), driven by a list kept outside the repository and never committed, printed, or pasted into the PR or a log, prints nothing except the GitHub handle in install URLs and repository links, which stays; replacements are placeholders such as `/Users/user/`, `-Users-user-`, `user@example.com` and "the owner", and a quoted owner decision keeps its words with the speaker written as "the owner"; fixtures whose expected values derive from a replaced path (encoded project folders, project IDs, hashes, sorted output) are updated so every test asserts what it asserted before; LICENSE's copyright line and `pyproject.toml`'s author field are left as they are unless the owner decides otherwise when the plan is approved; PROGRESS.md's `## Acceptance` section is left byte-identical; `initflow._default_device_name` no longer calls `socket.gethostname()` and the `mm init` device-name prompt offers no hostname-derived default (the plan chooses between no default and a generic role-style one such as the OS kind), re-init keeps an existing device's name, and a test pins that the default never equals the hostname; `docs/invariants/init-devices.md` and README describe the new default; `tests/test_initflow.py` gains init integration cases for the six setup paths named in the fourth task, each asserting the stop or continuation the code has today and no Syncthing, registration or config write where the path refuses (a case that finds a real defect is filed as a TODO, not fixed here); `./bin/check --affected` passes, and the full `./bin/check` passes in CI.
_touches: src/mind_meld/initflow.py, src/mind_meld/synclog.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/conftest.py, tests/test_additive_sync.py, tests/test_config.py, tests/test_conflict_copy.py, tests/test_events.py, tests/test_gc_events.py, tests/test_host_usage_snapshot.py, tests/test_integration.py, tests/test_manifest.py, tests/test_merge.py, tests/test_move_join.py, tests/test_projectid.py, tests/test_recovery.py, tests/test_retro_fleet_aggregator.py, tests/test_self_update.py, tests/test_silent_failure_contract.py, tests/test_token_usage.py, tests/test_initflow.py, tests/test_module_boundaries.py, .gstack/secret-dispositions.json, README.md, CHANGELOG.md, docs/_
_read-first: docs/invariants/README.md, docs/invariants/init-devices.md, docs/SPEC.md_
_session: fresh · effort: medium · verify: ./bin/check --affected_

_Source: owner decisions 2026-11-08, relayed by manager Ephesus 16: drop the history scrub ("we should just make sure it stays impersonal moving forward?", then "Yes"); no committed list of names, no CI check and no Actions-secret or factory-side scanner ("The ci check would be part of the repo though, so we aren't doing that"; "Neither of those"). Verified at `5eba9e4`: `initflow._default_device_name` returns `socket.gethostname()` and feeds `typer.prompt("Device name", default=...)`; `tests/test_module_boundaries.py` lists `_default_device_name`; the listed files are the tracked files where a search by kind finds an identifier today (counted, not quoted). The forward-looking rule lives in AGENTS.md (added with this regeneration). The fourth task folds in six recorded `[review]` test gaps from docs/TODOS.md (PR #291's coverage audit at `81504a4`; Track 118A's outside review at `30dd2b5`), placed here by the owner on 2026-11-08 ("fold in"), who treats the session-weight cap as guidance for this card; their symbols were re-checked at `cf2e127`._

- **Placeholders in tests, fixtures and code** -- _src/mind_meld/synclog.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/, ~300 lines._ (M)
- **Placeholders in docs, history records and the approval file** -- _README.md, CHANGELOG.md, docs/, .gstack/secret-dispositions.json, ~150 lines._ (M)
- **No hostname default for the device name** -- _src/mind_meld/initflow.py, tests/test_initflow.py, tests/test_module_boundaries.py, docs/invariants/init-devices.md, README.md, ~50 lines._ (S)
- **Init integration tests for six Syncthing setup paths** -- no demonstrated defect in any; re-verify each first. (1) An unsupported Syncthing GUI address or offline pre-existing configuration continues without Syncthing writes (`_check_syncthing`); (2) each transport, path-overlap and generic refusal the writer returns ends init with a controlled stop and its fixing command (`_require_syncthing`); (3) an ignore-file write failure during a managed join, after passphrase verification, leaves no registration or config write (`init`); (4) a malformed management record (the loader raises ConfigError) stops init before prompts or writes (`_check_syncthing`); (5) a stale management record on a default configuration is replaced by the current local device ID before the first managed write (`_check_syncthing`, `_save_syncthing_record_or_stop`); (6) a managed join stops without accepting the offer when the stock folder becomes occupied or shared after classification (`_managed_join`, `_require_syncthing`). _tests/test_initflow.py, ~150 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 122 ← {}
```

Track detail per group:

```
Group 122: Impersonal tree
  +-- Track 122A .......... ~L . 4 tasks
```

**Total: 1 group . 1 track remaining.**

Tracks 117A, 118A, 119A and 120A shipped (v1.12.25, v1.12.26, v1.12.28 and v1.12.29; v1.12.27 was the test-suite retirement, not a Track) and Groups 117 to 120 are retired. Track 121A, the git-history hostname scrub, was dropped by the owner on 2026-11-08 and Group 121 is retired with it, so the new Track takes 122A. IDs 95A and 96A stay unused because Future entries cite them.

The 2.0.0 cut is not a Track, and its path is open: the owner will decide how to get to 2.0 after using the Syncthing fleet for a while. SPEC's release line for 2.0.0 is O1 to O4 accepted on the owner's own observation on one Mac and both Linux machines in the Syncthing fleet, with O6 to O8 holding, recorded in PROGRESS.md `## Acceptance`. A Mac not yet moved keeps running on iCloud Drive and holds nothing up. Making the repository public again is a separate owner decision. Problems found while machines move are fixed as they appear.

---

## Future

Deferred: docs/roadmap-future.md (30 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
