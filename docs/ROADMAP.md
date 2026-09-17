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

---

## In Progress

Nothing is partially shipped. The retro-fidelity work closed with Group 58 at v0.14.12; Groups 58–62 live in `docs/roadmap-shipped.md`.

## Current Plan

_tombstone: 27_

#### Group 63: Codex read headroom

_Depends on: none_

##### Track 63A: Keep the warm Codex read inside its budget
_2 tasks . ~150 LOC . medium risk . 5 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/events_tail.py, src/mind_meld/config.py, src/mind_meld/cli.py, tests/test_host_usage.py, tests/test_host_usage_snapshot.py, tests/test_events_budget_scope.py, tests/test_config.py, tests/test_diag.py, docs/invariants/events-retro.md, README.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/events-retro.md (host-usage-snapshot section, standing read blockers), Group 57's entry in docs/roadmap-shipped.md (the 50 ms grace floor), ~/.gstack/projects/kbitz-mind-meld/63a-probes/_
_produces: a warm Codex read over this fleet's corpus completes inside the autopush budget or the operator has one documented lever that makes it, and `mm diag` shows the effective budget beside the last warm-pass time_
_session: fresh · effort: high · verify: ./bin/check tests/test_host_usage.py tests/test_host_usage_snapshot.py tests/test_events_budget_scope.py tests/test_config.py tests/test_diag.py tests/test_docs_routing.py_

_Source: promoted from roadmap-future.md "Warm Codex read headroom and the healthy-pass cache rewrite" ([plan-eng-review], Track 57A /autoplan 2026-09-14 P2, merged with Track 54A /autoplan 2026-09-09 P3), whose trigger — "a warm pass above 200 ms, or a corpus above ~900 rollouts" — has fired on both halves; merged with inbox `[plan-devex-review]` "Host read-budget override" (Track 61A /autoplan, P2), which says to extend that proposal rather than compete with it. Measured by /roadmap on 2026-09-17 at 66b5293, installed mm 0.14.15, against a scratch copy of the cache (`63a-probes/mm_codex_budget_probe.py`, the deferred bullet's own Repro): 1,052 rollouts under `~/.codex/sessions`, 1,014 cache entries; a warm `read_codex_usage` completes in 384 ms under a 5 s deadline and returns `deadline` on 3 of 3 attempts at `HOST_USAGE_READ_BUDGET_AUTOPUSH_MS = 250`, each spending ~390 ms to publish nothing; `HOST_USAGE_READ_BUDGET_INTERACTIVE_MS = 500` leaves ~116 ms. The same read measured 186 ms over 752 rollouts on 2026-09-09. Profile so far (`63a-probes/mm_codex_cache_profile.py`): `json.loads` of the cache is 35 ms, so load is not the cost; the file is 15.75 MB on disk and 4.63 MB compact because it is written with `indent=2`; `states` is 4.05 MB of the compact bytes (88%). The live cache's `last_reason` is `None` because interactive pushes still complete, and autopush is not wired on this Mac (`autopush.log` last written 2026-08-15), so today's exposure is the interactive budget: once it is breached no push publishes Codex and `mm push --capture-usage` becomes the only path. The "Host cache encoding trigger" (>100 ms load or >25 MB) and "Per-entry `states` cap" bullets stay deferred in roadmap-future.md; their gates have not fired, and the numbers above are the fresh reading for both. Re-run both probes at implementation time; a number in this card is a premise, not a source._

- **Profile the warm pass, then stop paying for what did not change** -- split the 384 ms into load, per-file fingerprint (a stat plus two 4 KB digests per cache hit) and write-back before choosing a fix. The bounded fix the deferred bullet names is skipping the unchanged-cache rewrite on a healthy pass: `read_codex_usage` commits `locked.data` on every complete pass and `locked_json_rmw` writes on every normal exit. Compact serialization is the other cheap lever if the write dominates. Not fixes: skipping the fingerprint on a metadata match (`test_same_size_same_mtime_rewrite_replaces_terminal_total` pins it) and a newest-first walk (Codex prunes its own sessions, so the corpus is not monotonic). A warm pass must still return exactly what a cold full read returns. Pin a warm pass over a 1,000-entry cache that performs no write when nothing changed, and record the before/after split in events-retro.md. _host_usage.py + tests + invariants, ~80 lines._ (M)
- **Give the operator one read-budget lever** -- if the profile leaves the warm pass linear in rollout count, no constant survives the corpus. Settle at review between a validated `[retro]` override read where `events_tail._capture_host_usage` picks its budget, and a budget derived from the cached entry count; autopush stays bounded either way and `HOST_READER_GRACE_MS` stays intact so Grok is not starved again. The setting names its reader in this card: `mm diag` prints the effective budget beside the last warm-pass time. Replace README's "There is no read-budget override" paragraph and the `deadline` remedy row. _events_tail.py + config.py + cli.py + tests + docs, ~70 lines._ (M)

#### Group 64: Usage-only capture

_Depends on: Group 63_

##### Track 64A: A usage refresh is not a push, and exit 4 says what it skipped
_4 tasks . ~150 LOC . medium risk . 3 files_
_touches: src/mind_meld/cli.py, src/mind_meld/events_tail.py, src/mind_meld/skills/retro_fleet/aggregator.py, tests/test_integration.py, tests/test_events.py, tests/test_retro_fleet_aggregator.py, tests/test_silent_failure_contract.py, tests/test_diag.py, docs/invariants/events-retro.md, README.md, SPEC.md, AGENTS.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 63A_
_read-first: docs/invariants/events-retro.md (cursor gate + recapture; host-usage-snapshot section), docs/invariants/sync.md, Group 61's entry in docs/roadmap-shipped.md_
_produces: `mm push --capture-usage` adds nothing to the retro's push counts or zero-repository note however often it runs, and every exit-4 message and document says whether content was pushed_
_session: fresh · effort: medium · verify: ./bin/check tests/test_integration.py tests/test_events.py tests/test_retro_fleet_aggregator.py tests/test_silent_failure_contract.py tests/test_diag.py tests/test_docs_routing.py_

_Source: `[ship:severity=informational]` "Five smaller mm push --capture-usage rough edges from adversarial review", items 1 and 2 plus its reopened third finding (Codex structured review, P2), PR #178 /ship, 2026-09-16; the SPEC.md task is documentation debt recorded by the PR #179 /ship doc-sync and never filed. Verified at 66b5293: `events_tail._run_events_tail` writes `[*capture.git_rows, *capture.session_rows, *capture.host_rows, *([] if suppress_host_capture else [mm_event])]` — the flag drops only the terminal `mm-push` row, so the cursor never advances and every invocation re-emits the same git and session rows; `aggregator.aggregate_git` counts every in-window `git-snapshot` whose `origin` is not `GIT_SNAPSHOT_ORIGIN_RECAPTURE` into `snap_total` / `snap_zero`, so a repo-less Mac's usage refresh lands in the retro's zero-repository "N of M pushes" note. `_push_captured_usage` raises `typer.Exit(4)` from its "No usage snapshot written" and "Usage snapshot append failed" branches before `_push_core` runs, while its docstring, AGENTS.md and README describe exit 4 as content sync being otherwise fine; `test_requested_capture_failure_never_calls_push` pins the ordering as intended, so the words are wrong, not the order. `_host_publication` builds its reader list from `_default_host_readers` alone and `_print_host_publication` prints "Refresh on this Mac: mm push --capture-usage" without checking that mm-events is a selected source, the condition `_prepare_usage_capture` refuses on. `grep -c "recapture\|capture-usage" SPEC.md` returns 0._

- **A requested capture writes usage and nothing else** -- settle one direction at review: skip git and session capture entirely under the flag (usage-only; also removes the duplicate rows and the wasted walk), or mark those rows with an origin `aggregate_git` excludes from push counts the way it already excludes recapture. v0.14.14's own rule is the test: a usage refresh must not count as a push. Under the first direction a `--capture-usage` run that also uploads content records no git rows; the content still syncs and the next ordinary push captures git from the unmoved cursor. Pin: a repo-less Mac runs the flag twice, the retro's zero-repository note and push totals do not move, and no duplicate session rows appear. _events_tail.py + aggregator.py + tests + invariants, ~70 lines._ (M)
- **Exit 4 before the push says content was not pushed** -- both pre-push exit-4 branches add "Content was not pushed; run mm push". Correct the `_push_captured_usage` docstring, the AGENTS.md push-flags paragraph, README's exit-code table and events-retro.md so exit 4 distinguishes "capture failed before the push" from "pushed, capture not in the accepted manifest". _cli.py + docs, ~30 lines._ (S)
- **Do not recommend a command that will refuse** -- when mm-events is not a selected source, status and diag name `mm enable-source mm-events` instead of the flag. _cli.py + tests, ~25 lines._ (S)
- **List the two missing commands in SPEC.md** -- add `mm recapture` and `mm push --capture-usage` to the CLI command reference with the exit codes this Track settles. _SPEC.md, ~25 lines._ (S)

#### Group 65: Reader-scoped publication evidence

_Depends on: Group 64_

##### Track 65A: Reader-scoped publication evidence on status and diag
_4 tasks . ~200 LOC . medium risk . 4 files_
_touches: src/mind_meld/events.py, src/mind_meld/events_tail.py, src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/cli.py, tests/test_events.py, tests/test_host_usage_snapshot.py, tests/test_integration.py, tests/test_diag.py, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, README.md, AGENTS.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 64A_
_read-first: docs/invariants/events-retro.md (host-usage-snapshot section, coverage states), docs/invariants/sync.md, Group 61's entry in docs/roadmap-shipped.md_
_produces: `mm status` and `mm diag` label each host reader from that reader's own recorded result, report uncertainty when any retained day file that could hold the winning host row is unreadable, and bind the publication receipt to the bytes that were parsed_
_session: fresh · effort: high · verify: ./bin/check tests/test_events.py tests/test_host_usage_snapshot.py tests/test_integration.py tests/test_diag.py tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py_

_Source: `[ship:severity=informational]` "Per-reader 'completed, no usage' is whole-row-scoped on the mm status/diag read path" (P2); items 3, 4 and 5 of the rough-edges entry; and `[ship]` "Investigate a claimed TOCTOU on the accepted-manifest digest check" — all PR #178 /ship, 2026-09-16. Verified at 66b5293: `aggregator.local_host_capture_candidate` emits `"empty": not row.lifetime_by_family`, one row-level boolean, and `cli._print_host_publication` appends "; completed, no usage" to every reader marked contributed when it is set, so a mixed sweep labels no reader empty and an all-empty sweep labels every reader empty; the live-push path already holds the right signal, `HostUsageCapture.empty`, recorded per reader before the family-keyed merge, and `make_host_usage_snapshot` writes no per-reader field. `latest_event_rows` marks a type uncertain only when the failing file has no winner yet or won in it — sound for `mm-push`'s `(-delta, index)` key, not for host rows, which `_host_row_order_key` orders by `row.as_of`. `recorded_row_revision` confirms the row through `_iter_typed_objs`, discards the digest that function computed from the bytes it parsed, then re-opens the file through `hash_file`. `_iter_mm_push_objs` runs the same digest into a throwaway `EventScan`. `EventScan.cached_hash` calls `path.resolve()` for every file of every source through `status`'s `diagnostic_hash` lambda although `scan.hashes` only ever holds mm-events day files, and `manifest`'s per-file hash step reports any `OSError` from that callback as "read error" and drops the file._

- **Put each reader's own empty result on the row** -- `make_host_usage_snapshot` gains an allowlist-safe per-reader list written from `capture.empty` (reader names only, never token payload). Its readers, in this card: `local_host_capture_candidate` → `project_host_publication` → `_print_host_publication`, which stop deriving emptiness from `lifetime_by_family`. Do not key off family-name membership: `host_family()` classifies by model-id prefix, so a Codex model it does not recognize lands under `other` — the first /ship fix made exactly this mistake. A row from an older Mac without the key renders its coverage with no empty claim. Aggregator acceptance tolerates the key across a mixed fleet (`_accept_optional_source_list` is the precedent). Pin mixed, all-empty and legacy rows. _events.py + events_tail.py + aggregator.py + cli.py + tests, ~110 lines._ (M)
- **An unreadable older day file marks a host row uncertain** -- for a type whose winner is chosen by a selector rather than file order, any read failure in the retained window makes that type uncertain. `mm-push` keeps its filename-order rule. _events.py + tests, ~30 lines._ (S)
- **Bind the receipt to the bytes that were parsed** -- `recorded_row_revision` returns the revision `_iter_typed_objs` computed in the same pass instead of re-hashing, which removes the claimed race by construction rather than settling whether it is reachable. One behaviour changes and needs a decision at review: a day file holding an oversized skipped line has no single-pass revision, so the receipt would report unpublished where `hash_file` reports published; status already reports that file as unknown. _events.py + tests, ~25 lines._ (S)
- **Stop hashing where nothing reads the hash** -- the cursor walk skips the digest when no caller wants a revision, and `cached_hash` checks the filename shape (or keys on the unresolved path) before `resolve()`, so a `resolve()` failure can no longer drop a non-event file from the status manifest. _events.py + cli.py + tests, ~35 lines._ (S)

### Execution Map

**This adjacency is RELEASE order, not launch order.** Every edge is release serialization on `pyproject.toml`: three cards claim three consecutive versions, and only one tag can exist per version. The cards also share `cli.py`, `events_tail.py` and `aggregator.py`, so the packer separates them anyway. Tracks may be worked in parallel Conductor workspaces; only their version slots serialize, and document order is priority. 63A is first because it has a clock: the warm Codex read is at 384 ms of the 500 ms interactive budget, up from 186 ms eight days earlier.

Adjacency from gstack-extend's `roadmap-pack` tool on the drafted Tracks (identical to the audit's GROUP_DEPS after apply; this is the `/roadmap` skill's own packer, not a script in this repo's `bin/`):

```
- Group 63 ← {}
- Group 64 ← {63}
- Group 65 ← {64}
```

Track detail per group:

```
Group 63: Codex read headroom
  +-- Track 63A ........... ~L . 2 tasks
Group 64: Usage-only capture
  +-- Track 64A ........... ~L . 4 tasks
Group 65: Reader-scoped publication evidence
  +-- Track 65A ........... ~L . 4 tasks
```

**Total: 3 groups . 3 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (93 items)

## Shipped

History: docs/roadmap-shipped.md
