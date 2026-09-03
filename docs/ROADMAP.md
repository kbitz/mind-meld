# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm supports three agents: Claude Code, Codex, and Grok Build.** OpenCode was dropped on 2026-09-01 by user decision; Groups 36 and 44 removed it (shipped v0.12.53 → v0.13.0). Do not re-add a fourth agent without a measured need — see the skill-link constraint below, which already refuses one on discovery grounds.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A, and it is why **Grok Build needs no skill link and no sync source** — probed 2026-09-01, `~/.grok/` has no `skills/`, `commands/` or `rules/` directory at all. Grok Build's entire mm surface is the usage reader.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Seven Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **A command that only exists to undo an automatic action is refused until the automatic action is shown to be correct.** v0.12.44 killed `mm uninstall-skills` this way: a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect — the installer recreated a link the user deleted. Fixing the installer made all three unnecessary. Before filing an inverse, check whether the forward action should have happened at all.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`. Two Tracks claiming one version merge cleanly in git, but only one tag can exist for that version — the second's code never gets tagged at all. A dev-dep-only `pyproject.toml` edit no longer force-pushes `latest`: `release.yml` compares `git rev-parse "$tag^{commit}"` to HEAD and skips with a warning. Serialization is still the cheap guard against the silent-lost-code case. See `docs/shared-infra.txt`.
- **The roadmap-staleness gate stays dead.** Track 28B was killed 2026-08-25 on the grounds that an empty Current Plan leaves nothing to drift. Groups 29–35 removed that ground, the question was re-put on 2026-08-25 with seven Groups in flight, and the answer was the same. Do not re-propose it; the design remains recorded in the Group 28 entry of `docs/roadmap-shipped.md` for whoever overrides this.
- **Discovery may read host logs locally, but an encoded cwd never goes on the wire.** Track 29A's prober is a two-level scan of `~/conductor/workspaces/*/*` whose only wire output is a canonical remote URL. Codex `turn_context.cwd` and Grok's URL-encoded session dir names would both yield more roots and are refused — see the Future bullet "Do not add a Codex or Grok sessions-snapshot". Confirmed 2026-08-25.
- **A Track that puts a field on a wire, in a cache, or in a log must name its reader in the same card, or declare the reader's Track by number.** Track 34A's review found FOUR producer-without-consumer instances in one pass: `degraded_sources` (shipped v0.12.47, zero readers), `git_capture` (shipped Track 30A, unread by the aggregator), `usageIsIncomplete` (discarded at cache normalization), and the SKILL.md decoder's missing fallback. Reinforced 2026-09-01 by the v0.12.51 conflict-log analysis, which found the conflict-decision collector had been deleted on a premise nobody read, and `synclog.py` still describing the pre-inversion direction four months after the inversion. A write with no reader is not half a feature, it is a liability that reads as one.
- **When a Track touches a reader, check the cache shape, not just the behaviour.** Track 34A verified all six of its card premises as TRUE-or-known-false and was still under-priced 2.5x, because premises describe behaviour while the cost sat in `host_usage._validated_grok_entry`, which normalizes every cached turn to `{key, day, model, usage}` and drops the rest. The existing "check the premise at drain time" constraint worked exactly as written and was insufficient.
- **A Track that prices, sums, or trends a counter must first prove the counter schema of every reader it consumes.** Added 2026-09-01. Track 35A's card was measured against HEAD, its premises were re-verified at drain time, and it was still going to ship a 7.40x error, because every existing constraint checks *behaviour* and *premises* while the defect sat in an undocumented property of the source formats. Codex CLI and Grok CLI report **inclusive** `input` (cache-read already inside it); Claude is **disjoint**. `grok-4.6` appeared under both schemas, so the semantics belong to the READER, not the model id. This is the "check the cache shape" constraint one layer further out: check the SOURCE shape.
- **A feature nobody uses is deleted, not repaired.** Added 2026-09-01. The OpenCode reader had been discarding its entire store since v0.12.30 and a Track was drafted to fix it; probing first showed `opencode.db` 19 days cold, `~/.config/opencode/` three weeks stale and composed entirely of symlinks into a git repo already under version control. The fix was real and the feature was not. Probe the artifact's liveness before pricing its repair.
- **A host usage reader may not ship against a synthetic fixture.** Added 2026-09-01. Its `CONTRACT.md` must record a live census against a real corpus and the host version it was taken from. `tests/fixtures/host_sessions/opencode/CONTRACT.md` said outright "the local machine did not have an OpenCode data directory" and "the SQLite schema is a minimal synthetic contract table." It was the only reader built that way and the only one that returned zero — the root cause Track 36A exists to delete.

---

## In Progress

_Nothing in flight. Groups 36, 37 and 44 shipped (v0.12.53 → v0.13.0, five PRs, 2026-09-01/02) and moved to `docs/roadmap-shipped.md`._

---

## Current Plan

_tombstone: 27_

#### Group 45: Conflict sidecar forensics

_Depends on: none_

##### Track 45A: Find and fix what deletes conflict sidecars
_3 tasks . ~220 LOC . high risk . 5 files_
_touches: src/mind_meld/cli.py, src/mind_meld/resolveflow.py, tests/test_conflict_copy.py, docs/invariants/conflicts.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/conflicts.md (the inversion and sidecar-dedup sections in full)_
_produces: the deleter is named, and fixed if it is mm_
_session: fresh · effort: high · verify: ./bin/check tests/test_conflict_copy.py tests/test_docs_routing.py_

_Diagnose FIRST, then fix. The prohibition is on shipping a blind mitigation — a retry-on-vanish that papers over whatever is deleting user data — NOT on fixing the cause once it is named. If the cause is in mm, it is fixed in this PR. If the fix turns out to be large, it splits to a follow-up Track rather than bloating this card; if the cause is external, task 2 is discharged and says so._

_Measured 2026-09-01: `mm autopull` at 11:40:40 UTC logged 25 `conflicted` outcomes to `pull-history.jsonl`, and `.mind-meld-log.md` recorded a sidecar for `memory/user_path_order.md`. **Zero `.sync-conflict-*` files exist anywhere on disk** and `mm conflicts` reports none. Parent-directory mtimes across all three affected trees (`~/.claude/projects/*/memory/`, `~/.codex/skills/.system/**`, `~/.gstack/projects/*/`) read 07:41-07:42 local — the signature of a create followed by a delete, one minute after the pull. If real, the peer's bytes are discarded with no recoverable trail while the sync log tells the user to run `mm resolve`._

_Ruled out BY PROBE, not by reasoning — do not re-spend the session on these. The write path is correct (driving `cli._apply_conflict` in a tmp dir produces the sidecar under installed 0.12.50). The `mm gc` reapers are only reachable from the `gc` command, and `_gc_old_conflict_files` additionally requires `--conflicts` plus a 30-day bar. `retention._sweep_local_tmp_files` is scoped to `data/<device>/` and `manifests/<device>/` in the storage tree, never local source trees. `_existing_post_inversion_sidecars_from_peer` globs anchored to `canonical.stem`, so it cannot reap a sibling's sidecar. `bin/apply` in the agent-config repo contains no `rmtree` / `unlink` / `rsync --delete`. Nothing in the 0.12.35-0.12.50 changelog touches the sidecar write path — and the machine self-upgraded 0.12.34.1 → 0.12.50 one second before the observed pull, so reproduce on BOTH versions before concluding the cause is external._

_**One dependency edge deleted 2026-09-01.** This card previously carried `blocked-by` against the walker-substrate Track, an edge neither card's tasks support: sidecar forensics touches `cli.py` / `resolveflow.py`, the walker work touches `host_usage.py` / `token_usage.py`. It was collision bookkeeping read as a dependency, and it had buried "find what is deleting user data" behind two refactors. Losing user data outranks a net-negative refactor — which is also why this Group now leads the plan: the 2026-09-03 regen declared the release-slot serialization explicitly so the packer puts forensics in layer 0 instead of ordering by packIdent._

- **Reproduce under filesystem instrumentation** -- drive a real conflicting two-device pull under `fs_usage` (or an audit hook) scoped to the three trees, and name the process that unlinks. This task's deliverable is a named cause, recorded in `docs/invariants/conflicts.md` whichever way it resolves. _tests + throwaway harness, ~60 lines._ (M)
- **Fix it if it is mm** -- conditional on task 1. Scope is deliberately unsized because the cause is unknown; that is the honest state of this card, not an omission. If the deleter is an mm code path, it is fixed here with a regression pin in `tests/test_conflict_copy.py`. If it is external (another tool pruning a directory mm writes into), the finding is documented and mm's defence is task 3 alone. **A fix landing outside `_touches:` (e.g. `fsutil.py`, `retention.py`, `synclog.py`) means widen it and re-pack**, per the documented drift process; those were NOT pre-declared speculatively, because the probe ruled the reapers and the tmp sweeper out. _location unknown, ~100 lines._ (M)
- **Make the loss detectable rather than silent** -- ships REGARDLESS of what task 1 finds, and is the reason this Track is not purely investigative. A `conflicted` outcome whose sidecar is absent immediately afterwards is a condition mm can assert: one `exists()` stat on the path just written converts a silent discard into the visible-failure contract's `mm: warning:` line. Per that contract this warning reaches stderr even in quiet mode — it signals data-at-risk, so do NOT gate it behind `if not quiet:`. _cli.py, ~60 lines._ (S)

### Phase 3: Retro fidelity

**End-state:** `retro-fleet` reports what actually happened on the fleet, and reports all three supported agents the same way — tokens and API-equivalent cost split per model family, everything else aggregated across models.
**Groups:** 46, 50

_Group 36 shipped (v0.12.53 + #156) and moved to `docs/roadmap-shipped.md`, joining Groups 29-35 (v0.12.45 → v0.12.52). The Phase's remaining members are Group 46 (cache encoding — the `offset == size` wedge is now the ONLY reason Grok Build is invisible on this fleet) and Group 50 (unified reporting, where the end-state lands). Groups 45, 47, 48 and 49 are unphased — Phases are optional and a mixed lane does not need one._

#### Group 46: Cache encoding

_Depends on: Group 45_

##### Track 46A: Shrink the host cache encoding
_2 tasks . ~140 LOC . medium risk . 3 files_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_out: 49A, 50A_
_read-first: 32A, 34A, 36A_
_produces: the host cache stops scaling its per-push cost with corpus size, and Grok Build completes a scan_
_session: fresh · effort: medium · verify: ./bin/check tests/test_host_usage.py tests/test_docs_routing.py_

_Filed from Track 32A's `/review`. Measured 2026-08-28 on a 747-rollout / 694 MB corpus: 72,654 states, cache **0.40 MB → 13.48 MB**, json round-trip **56.3 ms of the 250 ms** autopush host budget. `locked_json_rmw` parses and re-serialises the whole file every push, so the cost is paid per push and scales linearly. At roughly 4x this corpus it consumes the budget and the reader can never converge. **Trigger: round-trip above 100 ms, or cache above 25 MB.** Gate re-confirmed 2026-09-03 at `84f3b04` in `host_usage._validated_grok_entry` (`host_usage.py:1114`), written `value["offset"] != value["size"]` at `:1120`; the same expression also guards the Codex cache at `:2121`, but that is `_validated_entry`, a separate validator — grep the symbol, not the line._

_**This is now the ONLY thing making Grok Build invisible.** `mm diag` reports `grok prior successful scan: no` on device `889e42c0` despite `[retro] grok_host_usage = true`. Track 35A's gate held the xAI rate table behind two blockers; Group 36 (shipped v0.12.53) dissolved the other one by deleting OpenCode, so this Track alone unblocks Grok pricing. It is also the reason the user's third supported agent currently reports nothing._

_**`read-first: 34A` added 2026-09-01** (filed from Track 34A's `/autoplan`, undrained since 2026-08-30): 34A persists a partial marker in the Grok cache entry, and this Track rewrites that exact encoding. The marker becomes one more field the increment encoding must carry through, and it was raised by neither review voice at the time._

- **Store per-state increments instead of absolute cumulatives** -- identity survives because both sides reconstruct from the same running sum, and increments are ~5 digits against ~8. Absorbs the filed Grok item: `_validated_grok_entry` requires `offset == size`, so a ledger that cannot be read end-to-end in one budget discards all its work forever — 60 ms wedges permanently at 15 files on the live corpus. Same persisted-offset fix `token_usage.walk_jsonl_segment` shipped for Claude in v0.12.15. _host_usage.py, ~90 lines._ (M)
- **Bound a single entry** -- a per-entry cap needs a degradation that keeps the file's tokens counted while dropping only its cross-file dedup. Refusing the file would re-create the fail-closed whole-store pathology Track 31A removed. `iter_bounded_lines` bounds line SIZE, not line COUNT; max observed is 1,234 states. _host_usage.py, ~50 lines._ (S)

#### Group 47: Sync surface

_Depends on: Group 45_

##### Track 47A: Narrow the sync surface structurally
_2 tasks . ~180 LOC . medium risk . 4 files_
_touches: src/mind_meld/manifest.py, src/mind_meld/config.py, tests/test_manifest.py, tests/test_config.py, docs/invariants/sync.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 44A, docs/invariants/sync.md ("Generated files are not sync data", added v0.12.51)_
_produces: a generated directory drops out of sync without anyone adding a glob for it_
_blocked-by: Track 45A_
_session: fresh · effort: medium · verify: ./bin/check tests/test_manifest.py tests/test_config.py tests/test_integration.py tests/test_docs_routing.py_

_Both halves are follow-ups v0.12.51 named as its own known limitations. That release excluded 43 of 88 recorded conflicts by glob; these two close the parts a glob cannot express. **The former `blocked-by: 44A` edge is discharged:** 44A shipped in v0.12.55 and removed the opencode `DEFAULT_SOURCES` entry, so `_GENERATED_HOST_SKILL_GLOBS` now has a single codex consumer (`config.py`) — the exact mechanism this Track rewrites. The `_blocked-by: Track 45A_` that replaces it is release-slot serialization on the shared `pyproject.toml` version claim (data-loss forensics ships first), NOT a semantic dependency — do not read it as one at the next regen._

- **Marker-aware directory skip in the walker** -- v0.12.51 excludes gstack-extend's per-host skill renders BY NAME because `exclude_patterns` are fnmatch globs against a relative path and cannot express "skip the directory CONTAINING this file." gstack-extend already drops `.extend-root` in every dir it renders. Until the walker can see it, every new gstack-extend skill silently starts conflicting fleet-wide until someone adds a glob. Touches `manifest.walk_generic_source`: read the tombstone-suppression invariant first, because a marker skip must not generate deletion tombstones, exactly as adding a glob must not. All four scenarios are pinned in `tests/test_integration.py::TestExcludePatterns5C`. _manifest.py + config.py, ~120 lines._ (M)
- **Exclude the pair-review state machine only** -- `projects/*/pair-review/session.yaml` (8 of the 88 conflicts) is a live per-machine state machine and definitionally cannot be shared. The prose artifacts (`deploy.md`, `report.md`, `parked-bugs.md` — 23 more conflicts) STAY in scope: pair-review advertises cross-machine resume as a feature, so excluding them removes capability rather than noise. **The measurement that makes that call defensible: across the same window these paths took 31 conflicts against 178 mtime-skips**, so the existing local-is-newer gate already absorbs 85% of the collisions and the residual does not justify removing a feature. The fuller fix is device-scoped artifact paths (`pair-review/<device>/`) in GSTACK, not an mm exclusion — file that upstream rather than absorbing it here. _config.py, ~60 lines._ (S)

#### Group 48: Git environment hygiene

_Depends on: Group 45, Group 46_

##### Track 48A: Scrub the git environment for mm's git subprocesses
_1 task . ~40 LOC . low risk . 2 files_
_touches: src/mind_meld/events.py, tests/test_events.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 36B, docs/invariants/events-retro.md_
_produces: an inherited `GIT_DIR` can no longer misattribute one repo's commits to another_
_session: fresh · effort: low · verify: ./bin/check tests/test_events.py tests/test_docs_routing.py_

_Filed as S2 from Track 29A's `/autoplan` (2026-08-25) and re-verified at `84f3b04`: neither `subprocess.run` in `events.py` (`:1025` git log, `:1067` `remote.origin.url`) passes an `env=`, so `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_COMMON_DIR` are inherited. After Track 29A, classification is a `.git` **stat** that ignores the environment while `git log` still honours it, so every `.git`-bearing candidate can emit the env repo's `(canonical_remote, sha)` paired with its own `local_path` — the aggregator's exact dedup key. Exotic, but `autopush` runs from a hook whose environment mm does not control, which is precisely the unattended path._

- **Pass an explicitly scrubbed environment** -- strip the `GIT_*` variables that redirect repository resolution from both call sites, with a test that sets `GIT_DIR` to a decoy repo and asserts the walk still reports the candidate's own history. _events.py, ~40 lines._ (S)

#### Group 49: Walker substrate

_Depends on: Group 46, Group 47_

##### Track 49A: One walker, two adapters
_3 tasks . ~350 LOC (net NEGATIVE) . medium risk . 4 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/token_usage.py, tests/test_host_usage.py, tests/test_token_usage.py, tests/test_module_boundaries.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 46A, 36A, 33A, docs/invariants/events-retro.md (incremental-resume section)_
_produces: one cache/resume/fingerprint implementation shared by Claude, Codex and Grok Build_
_blocked-by: Track 46A_
_session: fresh · effort: high · verify: ./bin/check tests/test_host_usage.py tests/test_token_usage.py tests/test_module_boundaries.py tests/test_docs_routing.py_

_**Retitled and re-scoped 2026-09-01** from "three adapters" — Group 36 (shipped v0.12.53) deleted the OpenCode reader, so the host side is Codex and Grok Build over the shared Claude walker. One of the four cache files went with it, which is why the estimate dropped from ~400 to ~350 lines even as the premise strengthened._

_**Literals re-measured 2026-09-03 at `84f3b04`.** `host_usage.py` is **2,383** lines (the 36A deletion took it from the 2,645 measured at `727f9cd`; it had grown across Tracks 32A/33A/34A/35A). `token_usage.py` is **1,941**. The premise DIRECTION holds: `host_usage.py` imports five names from `token_usage`; cache version, empty-cache, resume plan, cache-hit validation, head fingerprint, file identity, day extraction and counter coercion are each written two or three times. The genuinely host-specific surface is the record shape only._

- **Hoist the resume protocol** -- one file-identity + head/tail-digest + complete-line-offset implementation. Keep `iter_bounded_lines` where it is; it is already shared and is the proof the seam works. _host_usage.py + token_usage.py, ~150 lines._ (S)
- **Collapse the per-format readers to adapters** -- after Track 32A both remaining host readers are per-turn with a dedup key (Claude `message.id`, Grok `_grok_terminal_key`, Codex `turn_id`). An adapter's whole job becomes: given a file, yield `(dedup_key, day, model, usage)`. `_aggregate` and `_aggregate_grok` become one function. _host_usage.py, ~150 lines (net negative)._ (M)
- **Retire the duplicated leaf helpers and pin the boundary** -- one day parser, one counter coercion, preserving the trust-boundary split the aggregator documents (`_safe_int` for peer-controlled events, the shared helper for trusted local reads). Extend `tests/test_module_boundaries.py` so a reintroduced private copy fails the build. Also bound the cache's interned `models` table: `host_usage._add_usage` (`:1975`) materializes every distinct model the local corpus has ever seen into `by_day[day]["by_model"]`, and the 32/day and 64/row caps only apply later in `events._cap_by_model` (`:1809`). Capping in the READER is not the fix — it would break the "day totals stay whole" invariant that makes `day_total - sum(by_model)` an honest residual. _host_usage.py + tests, ~50 lines._ (M)

#### Group 50: Unified reporting

_Depends on: Group 46, Group 47, Group 49_

##### Track 50A: Report every agent the same way
_2 tasks . ~200 LOC . medium risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 36A, 36B, 46A, 33A, docs/invariants/events-retro.md (Track 23A renderer contract, as revised)_
_produces: tokens and cost split per model family for all three agents; commits, LOC and PRs aggregate across all of them_
_blocked-by: Track 46A_
_session: fresh · effort: high · verify: ./bin/check tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py_

_Phase 3's end-state lands here. Premise re-verified 2026-09-03 at `84f3b04`: `_render_agent_inventory` (`aggregator.py:3373`) and `AGENT_FAMILY_ROWS` (`:184`) are both still present in `aggregator.py`._

_**One instruction struck 2026-09-01.** The card previously read "OpenCode must keep reporting an honest empty." That empty was a **defect, not an absence of data** — the reader held 35 readable assistant rows and discarded all of them. As written the card would have pinned a bug as intended behaviour in a regression test, which is the same failure mode as Track 35A's struck instruction to retire a passing guard, one Track later. The instruction is moot now that Groups 36 and 44 removed OpenCode (shipped v0.12.53 → v0.13.0), but it is why this Track stays gated on 46A: **it renders three real agents, not two real and one empty.**_

_**One instruction discharged 2026-09-03.** The card used to require widening the inbound `token_sources` tolerance to `degraded_sources` and `partial_sources`. That shipped in #156 (`999d54b`): `_accept_optional_source_list` reuses `_token_sources_subsequence` for all three lists, and the acceptor now RETAINS unknown names (bounded by `_TOKEN_SOURCE_ID_RE`) instead of rejecting the whole row — duplicates and known-name-out-of-order stay fatal. What remains for THIS Track is the renderer's half of the asymmetry: `opencode` is gone from the reader and the config but still a legal name on the wire, so an inbound retained name from a legacy peer must be ignored silently rather than surfaced as a fourth agent or a coverage gap._

- **One block, every agent** -- replace `_render_agent_inventory`'s diagnostic table with a per-model-family tokens-and-cost block shaped like `_render_token_block`, and drop `AGENT_FAMILY_ROWS`' `Claude (via agents)` disambiguation once there is one models block rather than two. Coverage reporting is not optional collateral: `_agent_coverage_notes` exists because "a vanished block must never be the diagnostic interface". An agent with genuinely no data still reports an honest empty — but verify per agent that the empty is absence, not a discard. _aggregator.py + SKILL.md, ~160 lines._ (L)
- **Everything else aggregates across models** -- commits, LOC, streak, commit-type mix, peak hours, bursts, ship-of-window, top repos, PR references and the trends table are cross-model fleet aggregates and must render as one number each. Pin it so a later per-agent split is a build failure rather than a review catch. Note the standing 24B decision that the card gets no new row. _aggregator.py, ~40 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not gating.

Adjacency list (from the packer):
```
- Group 45 ← {}
- Group 46 ← {45}
- Group 47 ← {45}
- Group 48 ← {45, 46}
- Group 49 ← {46, 47}
- Group 50 ← {46, 47, 49}
```

Track detail per group:
```
Group 45: Conflict sidecar forensics
  +-- Track 45A ........... ~L . 3 tasks

Group 46: Cache encoding
  +-- Track 46A ........... ~M . 2 tasks

Group 47: Sync surface
  +-- Track 47A ........... ~M . 2 tasks

Group 48: Git environment hygiene
  +-- Track 48A ........... ~S . 1 task

Group 49: Walker substrate
  +-- Track 49A ........... ~L . 3 tasks

Group 50: Unified reporting
  +-- Track 50A ........... ~L . 2 tasks
```

**Total: 6 groups . 6 tracks remaining. Critical path: 4 waves through 45A → 46A → 49A → 50A.**

---

## Future

Deferred: docs/roadmap-future.md (74 items)

## Shipped

History: docs/roadmap-shipped.md
