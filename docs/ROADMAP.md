# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

Audit configuration (recorded 2026-09-14): release-bearing Tracks here declare 9–14 files (source, tests, invariants, README, plus CHANGELOG / PROGRESS / pyproject) and this repo ships weight-6 sessions as one PR (53A landed 1,790 insertions on a weight-4 card, 57A 1,169 on weight 3), so the roadmap audit runs with raised caps set through gstack-extend's `bin/config`. The defaults (8 files, weight 4) fail every card in this plan and in the 2026-09-06 plan. The setting is machine-local (`~/.gstack-extend/config`), so it differs by Mac: 16 files / weight 6 where this paragraph was first written, 24 files / weight 5 on the Mac that ran the 2026-09-17 regeneration. Re-read it with `bin/config get` before trusting a SIZE verdict.

**mm supports three agents: Claude Code, Codex, and Grok Build.** OpenCode was dropped on 2026-09-01 by user decision; Groups 36 and 44 removed it (shipped v0.12.53 → v0.13.0). Do not re-add a fourth agent without a measured need — see the skill-link constraint below, which already refuses one on discovery grounds.

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

Nothing is partially shipped. Groups 63–65 shipped as v0.14.16–v0.14.18 and live in `docs/roadmap-shipped.md`.

## Current Plan

_tombstone: 27_

#### Group 66: Cut 1.0

_Depends on: none_

##### Track 66A: Cut v1.0.0: retire the pre-1.0 conflict alias and converge the failed-read cache write
_3 tasks . ~120 LOC . low risk . 12 files_
_touches: src/mind_meld/resolveflow.py, src/mind_meld/cli.py, src/mind_meld/host_usage.py, tests/test_conflict_copy.py, tests/test_host_usage.py, docs/invariants/conflicts.md, docs/invariants/events-retro.md, SPEC.md, README.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/conflicts.md (the `(b)oth` → `(s)kip` alias paragraph), docs/invariants/events-retro.md (standing read blockers; "Optional cache timing fields (Track 63A)"), AGENTS.md (Source Layout, import direction, testing)_
_produces: v1.0.0, with the one deprecation the code and docs promise to remove "at 1.0" removed, and a host read that is steadily over budget writing its cache once instead of on every autopush_
_session: fresh · effort: medium · verify: ./bin/check tests/test_conflict_copy.py tests/test_host_usage.py tests/test_diag.py tests/test_silent_failure_contract.py tests/test_docs_routing.py_

_Source: the 1.0 goal (user, 2026-09-21) and `[ship]` "host_usage.py: last_deadline_allotted_ms jitter can defeat the failed-pass write-skip" (P2, PR #181 /ship adversarial review, 2026-09-18). Verified at cdffc34: the alias is the only pre-1.0 shim in the tree — `resolveflow._LEGACY_SKIP_ALIAS_NOTICE` says "alias removed at 1.0", `_normalize_legacy_skip_choice_and_warn` is called from `resolveflow.py:827` and `cli.py:1797`, `cli.py:9004`'s docstring, `docs/invariants/conflicts.md:59`, `SPEC.md:569` / `:579` and `README.md:521` all describe it as living "until 1.0", and `grep -rn 'pre-1\.0\|until 1\.0' src docs/invariants` finds nothing else. `_carry_read_timing` computes `last_deadline_allotted_ms` as `round((deadline - started) * 1000)` where `deadline` is the caller's monotonic clock plus budget and `started` is the callee's own `time.monotonic()`, and `_skip_failed_cache_write` compares the whole timing map for equality, so scheduling latency between the two reads flips the value by ±1 ms between otherwise-identical over-budget passes and the failed-pass skip never converges; both readers share the helper (`host_usage.py:413`, `:775`). README carries no beta or pre-release statement, so nothing there needs removing for 1.0._

- **Retire the `b` / `both` alias** -- delete `_LEGACY_SKIP_ALIAS_NOTICE` and `_normalize_legacy_skip_choice_and_warn` with both call sites, so `b` and `both` fall through to each prompt's existing unknown-choice re-prompt and join the loud-rejected pre-v0.9.0 `c` / `f` letters. Rewrite the four alias tests in `tests/test_conflict_copy.py` (the `"both"` prompt cases near lines 1397–1438 and the `["b", "both"]` parametrize near 2935–3009) as rejection pins. Update the `cli.py:9004` docstring, the conflicts invariant paragraph, both SPEC.md sentences and README's `mm resolve` line to say the alias was removed at 1.0. _resolveflow.py + cli.py + test_conflict_copy.py + conflicts.md + SPEC.md + README.md, ~60 lines (del)._ (S)
- **Converge the failed-pass cache write** -- make `_skip_failed_cache_write`'s comparison insensitive to sub-budget jitter: compare `last_deadline_allotted_ms` on the caller's nominal budget (or a coarse bucket) while still persisting and displaying the value events-retro.md records. Decide at review which of the two the invariant keeps; the persisted formula is documented, so changing it needs the doc updated in the same commit. Pin: two consecutive over-budget passes whose fake clocks differ by 1 ms between caller and callee write the cache once. _host_usage.py + test_host_usage.py + events-retro.md, ~40 lines._ (S)
- **Release 1.0.0** -- CHANGELOG entry (the alias removal is the one BREAKING line), PROGRESS row, `pyproject.toml` to 1.0.0. Run the targeted checks above, then the full `./bin/check`. _CHANGELOG.md + docs/PROGRESS.md + pyproject.toml, ~20 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless of document order; document order is priority, not gating.

Adjacency from gstack-extend's `roadmap-pack` tool on the drafted Track (identical to the audit's GROUP_DEPS after apply; this is the `/roadmap` skill's own packer, not a script in this repo's `bin/`):

```
- Group 66 ← {}
```

Track detail per group:

```
Group 66: Cut 1.0
  +-- Track 66A ........... ~M . 3 tasks
```

**Total: 1 group . 1 track remaining.**

---

## Future

Deferred: docs/roadmap-future.md (93 items)

## Shipped

History: docs/roadmap-shipped.md
