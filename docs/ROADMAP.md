# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Everything carded through Track 160A (cross-model memory's qualification, Codex memory copies, the local qmd index and its setup, and the four bugs carded with them) shipped by v1.12.79 (independent receipts in shipped history). The plan below folds Codex memory into the `codex` source on the developer's decision of 2026-11-22 (Track 161A) and makes the affected-selection tests deterministic under load (Track 161B). The owner will decide the path to 2.0 after using the Syncthing fleet for a while.

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **A reproducible bug the owner actually encountered is admitted, not deferred (owner, 2026-11-09).** After the manager said the codex `skills/gstack/*` exclusion was not in the two Tracks being carded, the owner asked "why isn't that fix in our current roadmap? Anything that is a reproducible bug we actually encountered should be current". A Future entry that records a bug seen on the owner's machines, with a cause found in the code, goes to Current Plan on the next regeneration; deferral is for work nobody has hit, design questions and claims nobody verified, and the manifestation gate still prunes defects nobody has hit.
- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening any of them requires a new user scope decision. Standalone Grok Build support, dropped on 2026-10-19, was reopened by such a decision on 2026-11-05 (docs/SPEC.md). Cursor support, removed by the owner on 2026-11-07, is refused the same way: reopening it needs a new owner decision (docs/SPEC.md).
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.
- **Syncthing stays owner-managed outside O2 (spec, 2026-11-07).** mm discovers Syncthing and reads its state for `mm status` and `mm diag`, and changes Syncthing's configuration only for O2's setup, approval, introducers and mesh step (adding a device a fleet member's encrypted record names; spec amended 2026-11-18) on a machine where it found the default configuration. Invite codes, relays, changing a pre-existing configuration and scan requests are refused until the spec's revisit trigger is observed. Removing a retired machine and correcting fleet machines' Syncthing names on mm-managed machines were approved by the developer on 2026-11-20 and enter the spec through Track 154A's PR. Until it merges, the spec still refuses them.
- **One user, no compatibility work (spec, 2026-10-19).** Commands, config, output and host families may change with a README Upgrading note; only the storage-format refusal gates are kept. A Track that exists only to preserve compatibility is refused.
- **Output earns its line and uses the shared style (spec O8, amended 2026-11-10).** A Track that adds output to push or pull must show it is something that actually happened, a failure with its fix, data at risk or an actionable warning; routine detail (timings, counts of skipped or unchanged items, capture internals) goes to `--verbose`, `mm status` or `mm diag`. The auto commands stay silent unless the owner must act. Any new output on a hand-run command uses the shared style (O8.6) documented in the shared output helpers.
- **Core and extensions (spec, 2026-10-23).** Before a Track changes a host adapter or command group that still lives in `cli.py` or `host_usage.py`, a move-only Track just before it moves that piece into its own module behind a contract test, with no behavior change. No Track moves code that no planned Track changes; there is no one-shot rewrite.

---

## In Progress

None declared (2026-11-21). Tracks 156A to 160A, the previous plan, merged as v1.12.75 to v1.12.79 (PRs #366 to #371) and are recorded in shipped history; no branch, session or open pull request is active for either new Track.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160_

The plan carries two Tracks. Track 161A folds Codex memory into the `codex` source on the developer's decision, so a machine that syncs Codex syncs its memory copies with no separate source or `mm init` question. Track 161B makes the nested-pytest selection tests in tests/test_bin_check.py deterministic after they failed a transport verify and a release suite under load on 2026-11-21.

### Group 161: Codex memory with the codex source ∥ deterministic selection tests

_Depends on: none_

**This group delivers:** Codex memory copies travel with the `codex` source on every machine, with existing configs and peers on 1.12.76 to 1.12.79 carrying on unchanged; and a full `./bin/check` at the host's default worker count stops failing on the affected-selection tests, which print the nested run's output when they do fail.

#### Track 161A: Sync Codex memory as part of the `codex` source, with no separate source or init question
_3 tasks . ~400 LOC . medium risk . 14 files_
**Outcome:** on every machine with the `codex` source enabled, push publishes this machine's Codex memory copies and pull places peers' copies, with no `codex-memory` source to enable and no extra `mm init` question. Existing configs and fleet peers still on 1.12.76 to 1.12.79 carry on without losing or duplicating a copy.
**Supports:** docs/SPEC.md O9.1 (Codex memories roam as per-machine copies) and the host table's Codex row. The developer (verbatim, 2026-11-22): "I don't want to enable a separate source for codex memory, if you have codex enabled, then memory should be covered automatically". The developer approved amending the host table's Codex row, the README and the invariant docs in this Track's PR.
**Done when:** tests show a config that lists only `codex` publishing and placing Codex memory copies under the existing wire keys; a config that lists `codex-memory` folded by `mm migrate-config` with no deletion records; `mm init` asking no Codex memory question; `mm enable-source codex-memory` and `mm disable-source codex-memory` pointing at `codex`; the memory index still building its `mm-codex-…` collections from a `codex`-only config; and a manifest published by 1.12.79's `codex-memory` source applying unchanged.
**Acceptance:**
1. When the `codex` source is selected, source resolution (`config.resolve_sources` and `_configured_sources`) adds the internal Codex memory source with the `codex` source's path, so push publishes this machine's Codex memory files and pull places peers' copies. The file set (`memory_summary.md`, `MEMORY.md`, `rollout_summaries/*.md`), the folder resolution (`memories_v2/`, then `memories/`) and the rule that nothing is written into any `CODEX_HOME` stay as Track 157A shipped them. When `codex` is not selected, neither happens.
2. The wire layout stays as it is: manifests keep the `codex-memory` source key with `<device id>/<path>` entries and `codex-memory:` deletion records, and storage keeps the `codex-memory/` objects. A machine that had `codex-memory` enabled publishes byte-identical manifest entries after upgrading, so peers see no deletion and no conflict copy. Peers on 1.12.76 to 1.12.79 with `codex-memory` enabled keep exchanging copies with upgraded peers, and peers without it ignore the source as they do today (pull reads only selected sources). No storage-format gate changes, and no machine has to upgrade first.
3. Existing configs fold through `mm migrate-config`, following the `RETIRED_SOURCE_NAMES` precedent. Until it runs, a `[[sync.sources]]` entry named `codex-memory` is ignored at resolution (memory follows `codex`) and a `codex-memory` name in `disabled_sources` has no effect. `mm migrate-config` removes the entry and the name, and its preview lists both. No deletion record is minted, so peers keep their copies.
4. `codex-memory` leaves `OPT_IN_SOURCE_NAMES`, and `mm init` no longer asks about it (`initflow._prompt_sources`). `mm enable-source codex-memory` and `mm disable-source codex-memory` change nothing and print one line in the shared style saying Codex memory follows the `codex` source, with `mm enable-source codex` or `mm disable-source codex` as the fix. `mm disable-source codex` stops publishing Codex memory without deletion records, as disabling `codex-memory` does today. `mm sources` shows Codex memory as part of `codex`, not as its own row.
5. The memory index keeps finding the copies: `memoryindex` selects the resolved internal source by name or type, so a config that lists only `codex` still yields `mm-codex-<this machine>` and one `mm-codex-<peer>` per copies folder, pinned by a test in tests/test_memory_index.py. `mm conflicts` and `mm resolve` keep scanning the copies root.
6. docs/SPEC.md's host-table Codex row drops "when enabled (`mm enable-source codex-memory`)" and says the copies come with the `codex` source. Root SPEC.md (the `codex-memory` type and the source list), README's Codex section and an Upgrading note (the fold and `mm migrate-config`), and docs/invariants/sync.md ("Codex memory copies") describe the fold.
_release: patch (SPEC release policy: ordinary releases are PATCH; no storage-format change and no machine must upgrade first)_
_touches: src/mind_meld/config.py, src/mind_meld/core.py, src/mind_meld/initflow.py, src/mind_meld/sourceflow.py, tests/test_config.py, tests/test_codex_memory_source.py, tests/test_initflow.py, tests/test_source_toggle.py, tests/test_sourceflow.py, tests/test_memory_index.py, docs/invariants/sync.md, docs/SPEC.md, SPEC.md, README.md_
_read-first: docs/SPEC.md O9.1 and the host table, docs/invariants/README.md rows for the touched modules, docs/invariants/sync.md ("Codex memory copies (Track 157A)" and "Memory index refresh (Track 159A)"), src/mind_meld/config.py (`OPT_IN_SOURCE_NAMES`, `DEFAULT_SOURCES`, `RETIRED_SOURCE_NAMES`, `_configured_sources`), src/mind_meld/core.py (`_migrate_config_core`), src/mind_meld/manifest.py (`codex_memory_to_wire`), src/mind_meld/memoryindex.py (the `codex-memory` branch)_

_Source: the developer's decision of 2026-11-22, relayed by the Factory Manager's brief, which asked the card to decide migration, the index and the wire layout against HEAD. Verified at `91fa6bf` (v1.12.79): `config.OPT_IN_SOURCE_NAMES` holds `codex-memory`; `DEFAULT_SOURCES` carries a `codex-memory` entry (type `codex-memory`, path `~/.codex`) that `initflow._prompt_sources` asks about with every default; `manifest.codex_memory_to_wire` maps local keys to `<device id>/<path>` under `sources["codex-memory"]`; `pullflow`, `resolveflow` and `memoryindex` pick the source by name or type `codex-memory` from `resolve_sources`, so an entry derived there reaches all three; `core._migrate_config_core` already drops `RETIRED_SOURCE_NAMES` blocks, the precedent for the fold. Decisions: fold through `mm migrate-config` rather than keep a permanent alias, because the name stays meaningful only on the wire; leave the index code alone, because it reads the resolved source; keep the wire layout so mixed-version peers interoperate during the upgrade. A machine whose `disabled_sources` lists `codex-memory` starts publishing its Codex memory once `codex` is enabled, as the developer's decision implies._

- **Derive Codex memory from the `codex` source** -- in `config` resolution, add the internal entry when `codex` is selected; drop `codex-memory` from `OPT_IN_SOURCE_NAMES` and from init's prompt list; ignore a listed `codex-memory` entry and disabled name. Test publish and place from a `codex`-only config, and a byte-identical manifest against a 1.12.79-shaped one. _config.py, initflow.py, tests/test_config.py, tests/test_codex_memory_source.py, tests/test_initflow.py, ~170 lines._ (M)
- **Fold existing configs and redirect the source commands** -- make `core._migrate_config_core` remove `codex-memory` entries and disabled names with its preview, and make `sourceflow`'s enable, disable and `mm sources` output point at `codex`. _core.py, sourceflow.py, tests/test_config.py, tests/test_source_toggle.py, tests/test_sourceflow.py, ~140 lines._ (M)
- **Pin the index and document the fold** -- the `codex`-only index test and the docs listed in criterion 6. _tests/test_memory_index.py, docs/invariants/sync.md, docs/SPEC.md, SPEC.md, README.md, ~90 lines._ (S)

#### Track 161B: Make the `--affected` selection tests deterministic under machine load
_3 tasks . ~150 LOC . low risk . 3 files_
**Outcome:** a full `./bin/check` at the host's default worker count (16 on `linux-a`) no longer fails a transport verify or a release suite on the nested-pytest selection tests in tests/test_bin_check.py, and a real failure there prints the nested run's output.
**Supports:** the release discipline (`./bin/check` gates each release script and transport verify; docs/invariants/auto-upgrade.md) and AGENTS.md Testing; a flaky test costs every Track that meets it a rerun. Admitted under the standing rule on reproducible bugs encountered: the Factory Manager observed these failures on 2026-11-21 in a transport verify and a release-commit suite.
**Done when:** the PR records the cause found, with the failing assertion and the nested output or the injection that reproduces it; the fix removes that cause without weakening an assertion; and `MM_PYTEST_WORKERS=16 ./bin/check` passes three consecutive runs on a 16-CPU Linux machine with each run's load average recorded.
**Acceptance:**
1. The cause is found and recorded in the PR. Reproduce `TestAffectedSelection::test_moved_merge_base_reseeds` and `TestAffectedSelection::test_deleted_test_is_a_proven_empty_selection` failing under load (the full suite at 16 workers, or the class under a recorded CPU load), capture the failing assertion with the nested run's stdout and stderr, and name the load-sensitive step. Candidates at HEAD: the nested `bin/_check.py`'s wall-clock bounds (`SELECT_SUBPROCESS_TIMEOUT_S` = 60 s for `_git` and the environment probe, 8 s in `python_version`, 30 s in `venv_healthy`, 15 s for the xdist probe) and the tests' 180 s `check()` timeout, any of which can change the selection route or its message under load.
2. The two worker deaths are explained in the PR: whether the xdist worker crash and the nested pytest's exit 137 (SIGKILL) came from the kernel's OOM killer (kernel log), from `bin/check`'s own process-group teardown, or from a timeout.
3. The fix makes the selection tests deterministic for the cause found: a bound they depend on is replaced by a completion signal or a deterministic injection, or nested-pytest tests stop multiplying with the outer worker count (for example, grouped onto one worker). What `./bin/check --affected` does for the owner does not change.
4. No assertion is weakened: each test keeps its selected node set, its reason string and its exit-code check, and no test is skipped, marked flaky or retried.
5. Every assertion on a nested run in `TestAffectedSelection` and the `seeded` fixture includes the nested run's output (`both(proc)`), and `test_moved_merge_base_reseeds` also checks the nested exit code.
6. `MM_PYTEST_WORKERS=16 ./bin/check` passes three consecutive runs on a 16-CPU Linux machine, each run's load average recorded in the PR, and `./bin/check --tests --serial tests/test_bin_check.py` passes.
_release: patch (SPEC release policy: ordinary releases are PATCH)_
_touches: tests/test_bin_check.py, bin/_check.py, tests/conftest.py_
_read-first: AGENTS.md Testing, tests/test_bin_check.py (`check`, `seeded`, `proj`, `TestAffectedSelection`), bin/_check.py (`SELECT_SUBPROCESS_TIMEOUT_S`, `_git`, `probe_env`, `xdist_args`, the process-group teardown), docs/roadmap-shipped.md Track 150A (the previous determinism fix in these files)_

_Source: the Factory Manager's report of 2026-11-21: `test_moved_merge_base_reseeds` failed a transport verify, `test_deleted_test_is_a_proven_empty_selection` failed a release-commit suite (a retry passed), and two full-suite runs at 16 workers died with an xdist worker crash and a nested pytest exit 137 with no test failure (they passed at 6 workers and on a third run). The failing assertions' messages were not captured. Verified at `91fa6bf`: both tests exist; `check()` drops `MM_PYTEST_WORKERS` and these tests pass `--serial`, so each nested run is serial and the load comes from the outer workers (`xdist_args` defaults to `-n auto`, 16 on `linux-a`); `test_moved_merge_base_reseeds` asserts its reason string without the nested output or exit code._

- **Reproduce under load and fix the cause** -- capture the failing assertions with nested output, name the load-sensitive step, and make it deterministic. _tests/test_bin_check.py, bin/_check.py, ~90 lines._ (M)
- **Explain the worker deaths and bound nested concurrency if needed** -- read the kernel log for OOM kills around a 16-worker run, and group the nested-pytest tests onto one worker if they are resource kills. _tests/conftest.py, bin/_check.py, ~30 lines._ (S)
- **Print the nested output on every selection assertion** -- add `both(proc)` to each nested-run assertion and the exit-code check to `test_moved_merge_base_reseeds`. _tests/test_bin_check.py, ~30 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of document order; document order is priority, not gating.

Adjacency list (from the packer / GROUP_DEPS — not document order):
```
- Group 161 ← {}
```

Track detail per group:
```
Group 161: Codex memory with the codex source ∥ deterministic selection tests
  +-- Track 161A ........... ~L . 3 tasks
  +-- Track 161B ........... ~M . 3 tasks
```

**Total: 1 group . 2 tracks remaining.**

Tracks 135A to 160A shipped in v1.12.51 to v1.12.79, and Tracks 135B and 156B merged as documentation-only changes between them (independent receipts in shipped history). IDs 95A and 96A stay unused because Future entries and old records cite them.


---

## Future

Deferred: docs/roadmap-future.md (116 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
