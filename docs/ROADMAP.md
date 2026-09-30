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

Nothing is partially shipped. The 1.0 release and both releases historically
labeled 67A (v1.1.0 unified reporting and v1.2.0 Cursor usage) are recorded in
[shipped history](roadmap-shipped.md).

## Current Plan

_tombstone: 27_

#### Group 68: Qualify memory continuity

_Depends on: none_

##### Track 68A: Qualify Codex memory portability and recall
_3 tasks . ~350 LOC . medium risk . 3 files plus sanitized fixtures_
_touches: docs/designs/memory-continuity.md, docs/designs/codex-memory-contract.md (new), tests/fixtures/host_memories/codex/ (new), tests/test_memory_contract.py (new)_
_read-first: docs/designs/memory-continuity.md (native qualification and forgetting/echo contracts), docs/invariants/sync.md (generated files, consent, deletion proof and previews), docs/invariants/conflicts.md (MEMORY.md merge dispatch), AGENTS.md (testing and source ownership)_
_produces: a versioned live-source contract, sanitized fixtures, repeatable recall evidence, and a qualified native-import or explicitly separate mm recall design that the transport implementation can consume_
_session: fresh · effort: high · verify: ./bin/check tests/test_memory_contract.py tests/test_docs_routing.py_

_Source: [manual] user request, 2026-09-30; docs/TODOS.md drain record of the same date. At c43b777, config.DEFAULT_SOURCES selects Codex skills/plugins/AGENTS.md, not memories; merge._merge_strategy line-unions any MEMORY.md. The formal plan records local extraction, with consolidation and receiving-host recall still unqualified. Neither adding memories to include_dirs nor transferring generated handbooks is a qualified implementation._

- **Measure the native lifecycle and recall boundary** -- Follow the formal plan's CLI/Conductor census using the installed versions and an isolated host home. Observe an actual Mind Meld memory through extraction, consolidation, fresh-session recall, import into a populated store, regeneration, and capture/use opt-out. Record reliable project attribution, durable source identities, withdrawal, imported ancestry and unsupported cases in a source contract; commit only sanitized structural fixtures. _docs/designs/codex-memory-contract.md + tests/fixtures/host_memories/codex/, ~180 lines._ (M)
- **Prototype the forgetting and echo contracts** -- Use disposable stores to exercise F1-F4 and E1-E4 from the formal plan: offline resurrection, correction/regeneration, interrupted control publication, unknown absence, repeated transit, paraphrases, genuine new feedback, and missing ancestry. Keep deterministic protocol checks separate from live host recall evidence. Prove the chosen recall/export boundary; if imported ancestry is lost, require suppression of automatic re-export before delivering foreign context. Prototype output cannot claim production sync or native recall that the host did not demonstrate. _tests/test_memory_contract.py + source contract, ~130 lines._ (M)
- **Record the integration choice and promotion gate** -- Select a supported native import only if it passes; otherwise specify the mm-owned recall integration and its limitations. Freeze export identity, retirement and ancestry semantics; name the receiving consumer and enrollment boundary; update the formal plan with evidence and concrete implementation footprints. Failed host gates remain unresolved and block transport promotion. Transport, two-Mac parity, the Claude/Codex bridge and Cursor discovery remain linked follow-ons, sized from this evidence at the next regeneration. _docs/designs/memory-continuity.md + source contract, ~40 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of
document order; document order is priority, not gating.

Adjacency from gstack-extend's `roadmap-pack` output on the drafted Track:

```
- Group 68 ← {}
```

Track detail per group:

```
Group 68: Qualify memory continuity
  +-- Track 68A ........... ~L . 3 tasks
```

**Total: 1 group . 1 track remaining.**

The [formal plan](designs/memory-continuity.md#dependency-order) preserves the
full sequence: qualification → Codex transport/recall → two-Mac parity →
Claude/Codex sharing, with Cursor discovery following qualification. Only
qualification is ready to execute; later work is retained in Future with
explicit promotion gates. Forgetting and echo prevention are mandatory
prototype and release gates throughout.

---

## Future

Deferred: docs/roadmap-future.md (97 items)

## Shipped

History: docs/roadmap-shipped.md
