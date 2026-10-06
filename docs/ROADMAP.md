# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm reports usage for Claude Code, Codex, Grok Build, and Cursor.** Cursor supports standalone completed runs and legacy Conductor `runs.ndjson`; the SQLite reader is planned below. Cursor usage capture adds no Cursor customization or memory source. Memory capture, recall and sync parity are planned separately in [the memory continuity design](designs/memory-continuity.md).

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

### Group 70: Cursor usage and operator guidance

_Depends on: none_

#### Track 70A: Read Cursor usage from Conductor SQLite
_1 task . ~500 LOC . medium risk . 10 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/cli.py, tests/test_host_usage.py, tests/test_diag.py, tests/test_integration.py, tests/fixtures/host_sessions/cursor/, docs/invariants/events-retro.md, docs/invariants/sync.md, README.md, AGENTS.md_
_out: 70B_
_read-first: tests/fixtures/host_sessions/cursor/CONTRACT.md, docs/invariants/events-retro.md, docs/invariants/sync.md_
_produces: qualified SQLite completion metadata in the existing Cursor history and fleet usage, with legacy and standalone deduplication preserved_
_session: fresh · effort: high · verify: ./bin/check tests/test_host_usage.py tests/test_diag.py tests/test_integration.py tests/test_retro_usage_presentation.py tests/test_docs_routing.py_

_Source: [ship:files=src/mind_meld/host_usage.py|src/mind_meld/cli.py], PR #192 user decision D7=C, 2026-10-04; tests/fixtures/host_sessions/cursor/CONTRACT.md records the live Conductor 0.90.1 schema. At b70f70d, _iter_cursor_ledgers selects only runs.ndjson and unread_cursor_stores only counts index.db entries. A 2026-10-06 local directory inventory found no remaining index.db stores on this Mac; this does not overturn the recorded user-observed gap or qualify a new reader._

- **Count qualified SQLite completions through the existing Cursor reader** -- Obtain a real, version-pinned corpus of the `runs` table before accepting its schema, statuses, disjoint counters and completion timestamps. Read only the required usage metadata through a bounded, read-only SQLite connection; never fetch transcripts, blobs, credentials, agent stores or copy databases into fixtures/sync. Feed stable completed observations into the existing authoritative history, preserving request-ID dedup against legacy and standalone records, retention through source pruning, consent, deadlines and honest malformed/unsupported outcomes. Inspect cache normalization and migration so request identity and model parameters survive to their existing consumers; do not add unused wire fields or change the deferred pricing policy. Replace the unread-store notice only where the store is actually supported. Verify redacted real-schema fixtures, overlapping identities, live/pruned stores, failed reads and consent refusal; record live acceptance separately from deterministic checks. Update the source contract and command/invariant documentation. _src/mind_meld/host_usage.py, src/mind_meld/cli.py and declared tests/docs, ~500 lines._ (L)

#### Track 70B: Correct lock and install guidance
_2 tasks . ~70 LOC . low risk . 3 files_
_touches: src/mind_meld/lockfile.py, tests/test_lockfile.py, docs/invariants/auto-upgrade.md_
_out: 70A_
_read-first: docs/invariants/auto-upgrade.md, docs/designs/self-update-qualification.md_
_produces: contention guidance that preserves the lock inode and an accurate distinction between mm install policy and Git ref movement_
_session: fresh · effort: low · verify: ./bin/check tests/test_lockfile.py tests/test_self_update.py tests/test_docs_routing.py_

_Source: [plan-eng-review:severity=medium], the two diagnostic follow-ups filed by Track 69A, 2026-10-03. At b70f70d, acquire_lock still says to remove the lock, while release_lock documents why unlinking defeats inode-based exclusion; auto-upgrade.md says pipx upgrade can never move a classifier-pinned install, although detect_install includes bare own URLs and moving refs._

- **Replace unsafe contention advice** -- Tell the operator to wait for the holder or resolve that process, without recommending removal of a live lock. Preserve acquisition/release behavior and crash recovery by kernel flock lifetime. Exercise contention from an independent child on a temporary lock and assert that the diagnostic keeps the inode safe. _src/mind_meld/lockfile.py, tests/test_lockfile.py, ~50 lines._ (S)
- **Qualify pinned-install prose** -- Distinguish an immutable tag pin, an arbitrary moving ref, a bare repository URL and pipx's own hold flag. State that mm's conservative automatic-update policy selects only its exact @latest spec; it does not prove every other own spec is immovable by pipx. Preserve detect_install, update_argv and explicit recovery policy. _docs/invariants/auto-upgrade.md, ~20 lines._ (S)

### Group 71: Durable-write failure handling

_Depends on: Group 70_

#### Track 71A: Preserve coherent state after config publication failures
_2 tasks . ~250 LOC . medium risk . 9 files_
_touches: src/mind_meld/cli.py, src/mind_meld/config.py, tests/test_config.py, tests/test_integration.py, tests/test_recover.py, tests/test_crypto.py, tests/test_silent_failure_contract.py, docs/invariants/sync.md, docs/invariants/init-devices.md_
_blocked-by: Track 69B, Track 70A_
_read-first: docs/invariants/sync.md, docs/invariants/init-devices.md, src/mind_meld/fsutil.py_
_produces: init cleanup consistent with visible config, non-fatal crypto backfill failures, and handled recover failures without losing prior evidence_
_session: fresh · effort: high · verify: ./bin/check tests/test_config.py tests/test_integration.py tests/test_recover.py tests/test_crypto.py tests/test_silent_failure_contract.py tests/test_fsutil.py tests/test_docs_routing.py_

_Source: [plan-eng-review:severity=medium,files=src/mind_meld/cli.py|src/mind_meld/config.py] and [review:severity=low,files=src/mind_meld/cli.py], Track 69B's separately filed caller defects, 2026-10-03/04. At b70f70d, _register_and_save still deletes dev_key on every save exception; _init_crypto_session and recover still miss StorageError at the backfill/quarantine seams. Track 69B's completed audit is a satisfied prerequisite. Track 70A precedes this repair to serialize their shared CLI, integration-test and sync-invariant edits._

- **Preserve registration when config may already be published** -- Reproduce pre-replacement file-flush and post-replacement parent-flush failures in isolated state. Make the cleanup decision reflect publication instead of treating every save exception as proof that the local pointer is absent; preserve existing pre-publication cleanup and the original failure. Define conservative handling for unreadable/ambiguous current state without claiming durability or rolling back published bytes. Verify init retry, storage guard and later registration self-heal with isolated Keychain substitutes, then correct the affected failure contract. _src/mind_meld/cli.py, src/mind_meld/config.py and declared tests/docs, ~170 lines._ (M)
- **Handle helper write errors at their intended boundaries** -- Make crypto backfill tolerate the helper's StorageError with an always-stderr warning, and make recover report its handled quarantine failure. Cover both pre/post-publication faults and a retry after a visible quarantine copy; preserve source bytes, prior copies and exit semantics. Do not infer publication phase from exception messages or claim that a raised write restored old state. _src/mind_meld/cli.py and declared tests/docs, ~80 lines._ (M)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's roadmap-pack output on the candidate:

```
- Group 70 ← {}
- Group 71 ← {70}
```

Track detail per group:

```
Group 70: Cursor usage and operator guidance
  +-- Track 70A ........... ~L . 1 task
  +-- Track 70B ........... ~S . 2 tasks
Group 71: Durable-write failure handling
  +-- Track 71A ........... ~L . 2 tasks
```

**Total: 2 groups . 3 tracks remaining.**

Real self-update qualification remains **S13 REQUIRED / PENDING, 0/3**.
The completed Track 69A guide owns the run recipes; its next-release execution
obligation is retained in Future with an owner and retry event. Memory transport
also remains deferred: Track 68A delivered the accepted tests-only no-route
result, with native trials **0/48 UNSTARTED/INCONCLUSIVE**.

---

## Future

Deferred: docs/roadmap-future.md (100 items)

## Shipped

<a id="track-68a-qualify-codex-memory-portability-and-recall"></a>
History: docs/roadmap-shipped.md
