# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm syncs agent context and reports fleet usage for Claude Code, Codex, Grok Build, Muse and Antigravity.** [docs/SPEC.md](SPEC.md) is the product authority: its table says what each host gets, and the selected target is the owner's macOS and Linux fleet on Syncthing, reached by carrying the 1.x store onto a Syncthing folder. Each machine moves on its own schedule by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive (SPEC O2). Cross-model memory setup and the Codex source fold shipped by v1.12.80. The owner will decide the path to 2.0 after using the Syncthing fleet. [PROGRESS.md](PROGRESS.md#acceptance) records achieved checkpoints.

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

None declared.

## Current Plan

_tombstone: 27, 70, 71, 72, 73, 74, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161_

### Group 163: Mind Meld's own qmd index and agent setup

_Depends on: none_

**This group delivers:** agent memory search that leaves the owner's other qmd collections alone and gives agents a clear trigger and setup instructions.

#### Track 163A: Give Mind Meld its own qmd index and qualify agent recall
_4 tasks . ~210 LOC . medium risk . 7 files_
**Outcome:** The owner can keep a notes vault in qmd and use Mind Meld to search agent memories independently. Agents know when to search and how to surface conflicting memories.
**Supports:** [SPEC](SPEC.md) O8.6 and O9.2–6. The default qmd index is shared today, and incomplete agent setup can prevent the recall and contradiction checks that O9 requires.
**Done when:** enabling or refreshing moves legacy Mind Meld collections, regular operations and agent searches use the dedicated index, and the setup instructions and isolated Q1/Q2 results agree with the shipped commands.
**Acceptance:**
1. Select `--index mind-meld` for every routine index operation, including collection listing, inspection, reconciliation, removal, update and optional embed. Only the one-time cleanup uses the default `index`: the next successful `mm memory-index on` or enabled refresh creates and refreshes the desired collections in the named index, then removes only legacy `mm-` collections from the default index. Never update or embed the default or a project index. Keep cleanup retryable after a partial failure. A fresh installation and subsequent routine runs leave the default index alone. Leave every other collection and its search results intact. An isolated fixture with a notes collection in the default index and a project-local index verifies migration, search isolation, refresh, embed, read-only inspection and disable.
2. Use one recommended pointer line for setup and docs: “If the owner mentions something they discussed with an agent before and you can't find it in your own memory, search every agent's memory with `qmd --index mind-meld search "<key words>" -n 10` (`qmd --index mind-meld get <docid>` reads a hit). If memories disagree, show the owner each one with its path and ask which is right.” Update O9.3 in [SPEC](SPEC.md) and the [cross-model memory design](designs/cross-model-memory.md). The developer approves the SPEC wording at the implementation PR's merge gate.
3. After successful `mm memory-index on`, use shared O8 output to print the agent setup or link directly to the README's Agent setup subsection. Keep pointer edits by hand available for symlinked instruction files.
4. Add Agent setup under the README's Searching agent memory (qmd) section. Include the pointer line, the two Claude Code allow rules (`Bash(qmd --index mind-meld search:*)` and `Bash(qmd --index mind-meld get:*)`), and the Codex writable-root setting for the named index's actual cache folder only if criterion 5 demonstrates it is needed. Point existing users to replace the old pointer line, and use the named index in optional MCP examples.
5. Before documenting a Codex writable-root setting, try both named-index `search` and `get` under the normal Codex sandbox in an isolated home without the extra grant. If either fails because of filesystem permissions, verify the minimal cache-directory grant and record the evidence. If both work without it, omit that step from the README and CLI guidance.
6. Rerun Q1 and Q2 from the cross-model memory design with fresh Codex and Claude Code sessions, the revised line and the named index, in the isolated homes it prescribes. The owner prompts must mention a previous discussion with an agent and leave the answer in the seeded foreign memory. Verify cross-agent recall in both directions and that each agent surfaces both sides of the seeded contradiction with paths and asks which is right. Record commands, tool versions, sandbox settings, final pointer and results in that design. This covers O9.6's isolated evidence. The owner's Mac and Linux fleet acceptance remains separate.
7. At enablement, an installed qmd that exits nonzero, prints an unparseable version or times out still exits 1 with the failure detail and the copyable install command last. Assert that the failed preflight leaves config, index state and collections untouched.

_touches: src/mind_meld/memoryindex.py, tests/test_memory_index.py, tests/test_initflow.py, README.md, docs/SPEC.md, docs/designs/cross-model-memory.md, docs/invariants/sync.md_
_read-first: docs/SPEC.md (O8 and O9), docs/invariants/README.md, docs/invariants/sync.md (Memory index refresh), docs/invariants/init-devices.md (memory setup), docs/designs/cross-model-memory.md (Qualification)_
_verify: ./bin/check tests/test_memory_index.py tests/test_initflow.py_

_Source: developer decisions relayed on 2026-11-22. The existing `_GLOBAL_INDEX` in `memoryindex.py` selects the default index, and the README documents that sync refreshes other collections there. The design's Q1/Q2 results used the earlier trigger and show Codex with a cache grant. The version-probe test gap came from Track 162A's coverage audit._

- **Isolate qmd operations and move legacy collections.** Exercise the named index, bounded cleanup and retry with separate index fixtures. _memoryindex.py, tests/test_memory_index.py, tests/test_initflow.py, ~85 lines._ (M)
- **Revise the pointer and show the setup.** Update the canonical string, CLI success guidance, setup tests, README, SPEC and sync invariant. _memoryindex.py, tests/test_memory_index.py, tests/test_initflow.py, README.md, docs/SPEC.md, docs/invariants/sync.md, ~65 lines._ (S)
- **Check the Codex grant and rerun Q1/Q2.** Record whether the grant is needed and how both agents recalled and reported the seeds. _docs/designs/cross-model-memory.md, ~35 lines._ (S)
- **Pin failed qmd version probes.** Exercise nonzero exit, malformed version and timeout in the isolated preflight fixture. _tests/test_memory_index.py, ~25 lines._ (S)

### Execution Map

```
- Group 163 ← {}
```

**Total: 1 group, 1 Track remaining.**

---

## Future

Deferred: docs/roadmap-future.md (125 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
