# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm reports usage for Claude Code, Codex, Grok Build, and Cursor via Conductor.** Cursor usage capture adds no Cursor customization or memory source. Memory capture, recall and sync parity are planned separately in [the memory continuity design](designs/memory-continuity.md).

Standing constraints are admission criteria for new Tracks. Apply the [planning rules](../AGENTS.md#planning) when drafting or draining work; the [constraint history](roadmap-shipped.md#planning-constraint-history) records their rationale.

- **Host integrations require measured demand and live qualification.** A host usage reader's CONTRACT.md must record a real corpus census and the observed host version. Synthetic test cases can extend coverage but cannot supply that qualification.
- **Skill links require a discovery gap.** Before adding an agent row, prove the host does not already discover ~/.claude/skills. Grok customization sync uses the existing source allowlisting skills/, commands/ and rules/; it is separate from skill-link discovery.
- **Coordinate release versions and landing.** Implementation may proceed in parallel. Reconcile version bumps against the latest main and serialize release-bearing merges so two changes cannot claim the same release version. Keep pyproject.toml outside [shared infrastructure](shared-infra.txt).
- **Host discovery must not publish filesystem paths or transcripts.** Local discovery may read host logs, but encoded cwd paths never go on the events wire. Codex/Grok session attribution from decoded cwd remains refused; reconsider only with a metadata-only project index.
- **Explicit scope refusals remain in force.** OpenCode integrations and the roadmap-staleness gate remain dropped by user decision. Reopening either requires a new user scope decision.
- **Publication-status precision requires an observed user problem.** A status/diag publication-evidence Track needs a wrong or permanently unknown status line a user actually saw. A synthetic probe alone does not admit that work.

---

## In Progress

No active branches, sessions or PRs were declared for this regeneration.

## Current Plan

_tombstone: 27_

#### Group 69: Self-update qualification and atomic-write contract

_Depends on: none_

##### Track 69A: Qualify real mm-driven self-updates
_2 tasks . ~120 LOC . medium risk . 1 documentation file_
_touches: docs/designs/self-update-qualification.md (new)_
_out: 69B_
_read-first: docs/invariants/auto-upgrade.md, README.md, tests/test_self_update.py_
_produces: attributable S13 evidence for foreground, detached and explicit upgrades, or an exact pending prerequisite without crediting a simulated update_
_session: fresh · effort: medium · verify: ./bin/check tests/test_self_update.py tests/test_docs_routing.py_

_Source: PR #189's approved S13 post-release obligation and preparation receipt https://github.com/kbitz/mind-meld/pull/189#issuecomment-5919279593, 2026-09-30; released 2026-10-01. v1.4.0 is the following release. The 2026-10-03 read-only local inventory reports mm 1.4.0 with pipx's bare repository URL, so this installation is neither behind the target nor an exact @latest automatic-update candidate. That observation proves no S13 outcome._

- **Prepare and verify the real trial prerequisites** -- Retain all three S13 checks from the receipt. Identify updater-enabled installs genuinely behind a newer released target, verify the recorded exact own @latest spec for automatic arms, and prepare bounded attended runs with install, lock, command, log and next-invocation receipts. An already-current install, bootstrap install, changed metadata or fake installer does not prove self-update. If no suitable install is available, record the prerequisite as pending and the retry at a following release; S13 stays required and unpassed. _docs/designs/self-update-qualification.md, ~70 lines._ (M)
- **Run each eligible update arm and preserve the outcome** -- For attended push/pull, observe sync completion and foreground pipx under the mm lock; for autopull/autopush, observe prompt hook return, detached output and independent mm-lock availability; for explicit mm update, verify the known target and the current-version no-installer rerun. Each arm needs an eligible starting install and records the installed metadata plus the next mm --version. Keep unavailable arms pending; synthetic checks and an already-current version cannot fill them. File reproduced defects separately with their evidence. _docs/designs/self-update-qualification.md, ~50 lines._ (S)

##### Track 69B: Clarify atomic-write publication failures
_2 tasks . ~80 LOC . low risk . 3 files_
_touches: src/mind_meld/fsutil.py, tests/test_fsutil.py, docs/invariants/sync.md_
_out: 69A_
_read-first: docs/invariants/sync.md, docs/invariants/init-devices.md, docs/invariants/events-retro.md, docs/invariants/auto-upgrade.md, tests/test_fsutil.py, tests/test_memory_contract.py_
_produces: a truthful atomic-write failure contract and a symbol-based audit of durable callers; any reproduced caller defect is filed separately_
_session: fresh · effort: medium · verify: ./bin/check tests/test_fsutil.py tests/test_lockedjson.py tests/test_storage_local.py tests/test_attemptlog.py tests/test_memory_contract.py tests/test_docs_routing.py_

_Source: [review:severity=informational,files=src/mind_meld/fsutil.py], docs/TODOS.md, Track 68A review decision D3, 2026-10-02; verified at a3c5050. fsutil.atomic_write_bytes calls os.replace before fsync_dir; tests/test_fsutil.py::TestAtomicWriteBytes.test_parent_fsync_failure_is_fatal and tests/test_memory_contract.py::test_C2_real_atomic_helper_failure_reopens_actual_coherent_bytes already exercise publication followed by an error._

- **Correct the failure guarantee** -- Document the publication boundary in atomic_write_bytes: failures before replacement preserve the old target; a parent-directory fsync failure after replacement leaves published bytes with unconfirmed durability. Cleanup does not roll back publication. Keep the existing regression coverage; add a case only for a concrete gap discovered by the caller audit. _src/mind_meld/fsutil.py, ~30 lines._ (S)
- **Audit durable callers before proposing repairs** -- Trace explicit and conditional fsync=True consumers, including LocalBackend.put, sidecar.write, locked_json_durable_rmw, attemptlog and the CLI durable copy helper, through their StorageError handlers. Record whether each handler reopens current bytes, stops, or assumes the old state remains. Cite paths and symbols in the sync invariant; file any demonstrated stale-state/rewrite defect in TODOS with its reproduction instead of expanding this contract card across the consumers. _docs/invariants/sync.md, ~50 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 69 ← {}
```

Track detail per group:

```
Group 69: Self-update qualification and atomic-write contract
  +-- Track 69B ........... ~M . 2 tasks
  +-- Track 69A ........... ~M . 2 tasks
```

**Total: 1 group . 2 tracks remaining.**

Memory transport remains deferred. Track 68A shipped an accepted tests-only
no-route result; native trials remain 0/48 UNSTARTED/INCONCLUSIVE. Complete
live qualification and retain the contract's model limits and Q1–Q18 gates
before promoting transport, fleet parity, sharing or Cursor integration.

---

## Future

Deferred: docs/roadmap-future.md (96 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
