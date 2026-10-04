# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

Audit configuration (recorded 2026-09-14): release-bearing Tracks here declare 9–14 files (source, tests, invariants, README, plus CHANGELOG / PROGRESS / pyproject) and this repo ships weight-6 sessions as one PR (53A landed 1,790 insertions on a weight-4 card, 57A 1,169 on weight 3), so the roadmap audit runs with raised caps set through gstack-extend's `bin/config`. The defaults (8 files, weight 4) fail every card in this plan and in the 2026-09-06 plan. The setting is machine-local (`~/.gstack-extend/config`), so it differs by Mac: 16 files / weight 6 where this paragraph was first written, 24 files / weight 5 on the Mac that ran the 2026-09-17 regeneration. Re-read it with `bin/config get` before trusting a SIZE verdict.

**mm reports usage for Claude Code, Codex, Grok Build, and Cursor via Conductor.** Cursor usage capture shipped in v1.2.0; it adds no Cursor customization or memory source. Memory capture, recall and sync parity are planned separately in [the memory continuity design](designs/memory-continuity.md). OpenCode remains dropped by the 2026-09-01 user decision (Groups 36 and 44, v0.12.53 → v0.13.0). New host integrations still require a measured need and must satisfy the skill-discovery constraint below.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A, and it is why **Grok Build needs no skill link and no sync source** — probed 2026-09-01, `~/.grok/` has no `skills/`, `commands/` or `rules/` directory at all. Grok Build's entire mm surface is the usage reader.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Seven Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **A command that only exists to undo an automatic action is refused until the automatic action is shown to be correct.** v0.12.44 killed `mm uninstall-skills` this way: a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect — the installer recreated a link the user deleted. Fixing the installer made all three unnecessary. Before filing an inverse, check whether the forward action should have happened at all.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`. Two Tracks claiming one version merge cleanly in git, but only one tag can exist for that version — the second's code never gets tagged at all. A dev-dep-only `pyproject.toml` edit no longer force-pushes `latest`: `release.yml` compares `git rev-parse "$tag^{commit}"` to HEAD and skips with a warning. Serialization is still the cheap guard against the silent-lost-code case. See `docs/shared-infra.txt`.
- **The roadmap-staleness gate stays dead.** Track 28B was killed 2026-08-25 on the grounds that an empty Current Plan leaves nothing to drift. Groups 29–35 removed that ground, the question was re-put on 2026-08-25 with seven Groups in flight, and the answer was the same. Do not re-propose it; the design remains recorded in the Group 28 entry of `docs/roadmap-shipped.md` for whoever overrides this.
- **Discovery may read host logs locally, but an encoded cwd never goes on the wire.** Track 29A's prober is a two-level scan of `~/conductor/workspaces/*/*` whose only wire output is a canonical remote URL. Codex `turn_context.cwd` and Grok's URL-encoded session dir names would both yield more roots and are refused — recorded durably as a Non-goal at `docs/designs/host-parity.md:209`; reconsider only if a host supplies a metadata-only index. Confirmed 2026-08-25.
- **A Track that puts a field on a wire, in a cache, or in a log must name its reader in the same card, or declare the reader's Track by number.** Track 34A's review found FOUR producer-without-consumer instances in one pass: `degraded_sources` (shipped v0.12.47, zero readers), `git_capture` (shipped Track 30A, unread by the aggregator), `usageIsIncomplete` (discarded at cache normalization), and the SKILL.md decoder's missing fallback. Reinforced 2026-09-01 by the v0.12.51 conflict-log analysis, which found the conflict-decision collector had been deleted on a premise nobody read, and `synclog.py` still describing the pre-inversion direction four months after the inversion. A write with no reader is not half a feature, it is a liability that reads as one.
- **When a Track touches a reader, check the cache shape, not just the behaviour.** Track 34A verified all six of its card premises as TRUE-or-known-false and was still under-priced 2.5x, because premises describe behaviour while the cost sat in `host_usage._validated_grok_entry`, which normalizes every cached turn to `{key, day, model, usage}` and drops the rest. The existing "check the premise at drain time" constraint worked exactly as written and was insufficient.
- **A Track that prices, sums, or trends a counter must first prove the counter schema of every reader it consumes.** Added 2026-09-01. Track 35A's card was measured against HEAD, its premises were re-verified at drain time, and it was still going to ship a 7.40x error, because every existing constraint checks *behaviour* and *premises* while the defect sat in an undocumented property of the source formats. Codex CLI and Grok CLI report **inclusive** `input` (cache-read already inside it); Claude is **disjoint**. `grok-4.6` appeared under both schemas, so the semantics belong to the READER, not the model id. This is the "check the cache shape" constraint one layer further out: check the SOURCE shape.
- **A feature nobody uses is deleted, not repaired.** Added 2026-09-01. The OpenCode reader had been discarding its entire store since v0.12.30 and a Track was drafted to fix it; probing first showed `opencode.db` 19 days cold, `~/.config/opencode/` three weeks stale and composed entirely of symlinks into a git repo already under version control. The fix was real and the feature was not. Probe the artifact's liveness before pricing its repair.
- **A host usage reader may not ship against a synthetic fixture.** Added 2026-09-01. Its `CONTRACT.md` must record a live census against a real corpus and the host version it was taken from. `tests/fixtures/host_sessions/opencode/CONTRACT.md` said outright "the local machine did not have an OpenCode data directory" and "the SQLite schema is a minimal synthetic contract table." It was the only reader built that way and the only one that returned zero — the root cause Track 36A existed to delete (shipped v0.12.53).
- **Precision work on status or diag publication evidence requires a wrong or permanently unknown status line a user actually saw.** Added 2026-09-21. v0.14.14, v0.14.16, v0.14.17 and v0.14.18 were four consecutive releases on host-usage publication evidence, every one drained from /ship adversarial passes; the Track 65A probes showed two of its four carded tasks were reachable only by synthetic setup (an unreadable older day file; a `resolve()` failure). A probe result is not a user observation. Source: Track 65A /autoplan decision 16, `~/.gstack/projects/kbitz-mind-meld/ceo-plans/2026-09-21-track-65a.md`.

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
