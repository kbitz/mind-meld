# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

**mm supports three agents: Claude Code, Codex, and Grok Build.** OpenCode was dropped on 2026-09-01 by user decision and Group 36 removes it. Do not re-add a fourth agent without a measured need — see the skill-link constraint below, which already refuses one on discovery grounds.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A, and it is why **Grok Build needs no skill link and no sync source** — probed 2026-09-01, `~/.grok/` has no `skills/`, `commands/` or `rules/` directory at all. Grok Build's entire mm surface is the usage reader.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Seven Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **A command that only exists to undo an automatic action is refused until the automatic action is shown to be correct.** v0.12.44 killed `mm uninstall-skills` this way: a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect — the installer recreated a link the user deleted. Fixing the installer made all three unnecessary. Before filing an inverse, check whether the forward action should have happened at all.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`; two Tracks claiming one version force-advance `latest` to an untagged commit. See that file for the full argument.
- **The roadmap-staleness gate stays dead.** Track 28B was killed 2026-08-25 on the grounds that an empty Current Plan leaves nothing to drift. Groups 29–35 removed that ground, the question was re-put on 2026-08-25 with seven Groups in flight, and the answer was the same. Do not re-propose it; the design remains recorded in the Group 28 entry of `docs/roadmap-shipped.md` for whoever overrides this.
- **Discovery may read host logs locally, but an encoded cwd never goes on the wire.** Track 29A's prober is a two-level scan of `~/conductor/workspaces/*/*` whose only wire output is a canonical remote URL. Codex `turn_context.cwd` and Grok's URL-encoded session dir names would both yield more roots and are refused — see the Future bullet "Do not add a Codex or Grok sessions-snapshot". Confirmed 2026-08-25.
- **A Track that puts a field on a wire, in a cache, or in a log must name its reader in the same card, or declare the reader's Track by number.** Track 34A's review found FOUR producer-without-consumer instances in one pass: `degraded_sources` (shipped v0.12.47, zero readers), `git_capture` (shipped Track 30A, unread by the aggregator), `usageIsIncomplete` (discarded at cache normalization), and the SKILL.md decoder's missing fallback. Reinforced 2026-09-01 by the v0.12.51 conflict-log analysis, which found the conflict-decision collector had been deleted on a premise nobody read, and `synclog.py` still describing the pre-inversion direction four months after the inversion. A write with no reader is not half a feature, it is a liability that reads as one.
- **When a Track touches a reader, check the cache shape, not just the behaviour.** Track 34A verified all six of its card premises as TRUE-or-known-false and was still under-priced 2.5x, because premises describe behaviour while the cost sat in `host_usage._validated_grok_entry`, which normalizes every cached turn to `{key, day, model, usage}` and drops the rest. The existing "check the premise at drain time" constraint worked exactly as written and was insufficient.
- **A Track that prices, sums, or trends a counter must first prove the counter schema of every reader it consumes.** Added 2026-09-01. Track 35A's card was measured against HEAD, its premises were re-verified at drain time, and it was still going to ship a 7.40x error, because every existing constraint checks *behaviour* and *premises* while the defect sat in an undocumented property of the source formats. Codex CLI and Grok CLI report **inclusive** `input` (cache-read already inside it); Claude is **disjoint**. `grok-4.6` appeared under both schemas, so the semantics belong to the READER, not the model id. This is the "check the cache shape" constraint one layer further out: check the SOURCE shape.
- **A feature nobody uses is deleted, not repaired.** Added 2026-09-01. The OpenCode reader had been discarding its entire store since v0.12.30 and a Track was drafted to fix it; probing first showed `opencode.db` 19 days cold, `~/.config/opencode/` three weeks stale and composed entirely of symlinks into a git repo already under version control. The fix was real and the feature was not. Probe the artifact's liveness before pricing its repair.

---

## In Progress

_Nothing in flight._

---

## Current Plan

_tombstone: 27_

### Phase 3: Retro fidelity

**End-state:** `retro-fleet` reports what actually happened on the fleet, and reports all three supported agents the same way — tokens and API-equivalent cost split per model family, everything else aggregated across models.
**Groups:** 36, 40, 43

_Groups 29-35 shipped (v0.12.45 → v0.12.52) and moved to `docs/roadmap-shipped.md`. They answered the reported symptoms — git history was being lost, Grok published nothing, Codex was double-counted, the card could not see a dropped reader, and host tokens were priced against the wrong counter schema._

_**Phase membership changed 2026-09-01.** Group 40 (host cache encoding) was Phase 4's first Group. It moves here because its actual content is "make Grok Build's data reach the card" — the `_validated_grok_entry` `offset == size` wedge is now the ONLY reason Grok is invisible on this fleet. That is retro fidelity, not reader consolidation. Phase 4 is left with one Group (42) and its wrapper is dropped; a Phase spans ≥2 Groups or it is just a Group with extra ceremony. Groups 37, 38, 39 and 41 are unphased — Phases are optional and a mixed lane does not need one._

#### Group 36: Three hosts

##### Track 36A: Delete the OpenCode usage reader
_2 tasks . ~250 LOC (all deletion) . low risk . delete-only_
_touches: src/mind_meld/host_usage.py, src/mind_meld/events_tail.py, tests/test_host_usage.py, tests/test_host_usage_snapshot.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_out: 36B, 37B_
_read-first: 31A, docs/invariants/events-retro.md (host-usage-snapshot section)_
_produces: mm stops carrying a reader that has never returned a row_
_session: fresh · effort: low · verify: pytest tests/; ruff check .; ruff format --check ._

_**User decision, 2026-09-01: mm supports Claude Code, Codex and Grok Build. OpenCode is dropped.** This Track replaces a drafted fix for the OpenCode `$.id` defect — `host_usage.py:1345` projects `json_extract(data, '$.id')` while OpenCode keeps `id` as a table column, so every row yields NULL and one bad row fails the whole store (**0 of 35** readable assistant rows, measured 2026-09-01). Deleting the reader dissolves that defect instead of fixing it._

_Probed before planning, and the probe is why this is a deletion and not a repair: `~/.local/share/opencode/opencode.db` was last written **2026-08-13**, 19 days cold. `~/.config/opencode/` has a directory mtime of **2026-08-14** and its entire contents are symlinks into `~/.dotfiles/agent-config/` plus generated per-host skill renders — the sync source has never carried a byte OpenCode authored, and what it did carry is the exact category `docs/invariants/sync.md` calls "generated files are not sync data"._

_**This Track is deletion-only on purpose**, which is what earns it the `max_files_per_track` exemption. The wire constant, the sync source, the skill link and the prose are three separate Tracks (36B, 37B) because each carries a different risk and only one of them is reversible for free._

- **Delete the reader** -- `_read_opencode_database`, `_scan_opencode_root`, `_opencode_terminal`, `_is_zero_opencode_ledger` and the OpenCode cache namespace in `host_usage.py` (41 references), plus the reader registration and source gate in `events_tail.py` (9). **Do NOT touch `events.HOST_USAGE_TOKEN_SOURCES` here** — that is 36B's job and removing it naively breaks the fleet. _host_usage.py + events_tail.py, ~150 lines (del)._ (S)
- **Delete its test coverage** -- 78 references in `tests/test_host_usage.py` and 71 in `tests/test_host_usage_snapshot.py`, deleted rather than ported. Keep any fixture that a legacy-peer tolerance test in 36B will need, and say so in the PR so 36B does not have to rebuild it. _tests, ~100 lines (del)._ (S)

##### Track 36B: Pin the OpenCode wire name and sweep the prose
_2 tasks . ~90 LOC . medium risk . 8 files_
_touches: src/mind_meld/events.py, src/mind_meld/cli.py, tests/test_events.py, README.md, SPEC.md, AGENTS.md, CLAUDE.md, docs/designs/host-parity.md_
_read-first: 36A, 33A_
_produces: a legacy peer keeps publishing accepted host rows after OpenCode is gone_
_session: fresh · effort: medium · verify: pytest tests/test_events.py tests/test_retro_fleet_aggregator.py; ruff check .; ruff format --check ._

_**This Track exists because the obvious cleanup is a fleet-wide data-loss bug.** `aggregator._token_sources_subsequence` (`aggregator.py:1510`) validates a peer's `token_sources` list as a **subsequence of `events.HOST_USAGE_TOKEN_SOURCES`**, and returns `None` — `"invalid_token_sources"`, rejecting the WHOLE row — the moment it meets a name outside that tuple. A Mac on older mm keeps emitting `["codex", "grok", "opencode"]` for the full 90-day retention window. Deleting `"opencode"` from the tuple would therefore drop every legacy peer's entire host row, which is precisely the fail-the-whole-row pathology Track 33A documented and Track 31A removed._

_So the decision is: **the wire keeps the name; only the reader goes.** This is the same shape as every other version discriminator in this codebase — additive, key-absence, never remove. The tuple entry becomes a legacy marker with a comment saying why it may not be deleted, and a test pins it so a future cleanup pass cannot quietly do the tempting thing. Deliberately **not** release-bearing: no behaviour changes, so no version bump, which is why it rooms with 36A instead of costing a wave._

- **Pin the constant** -- keep `"opencode"` in `HOST_USAGE_TOKEN_SOURCES` with a comment naming `_token_sources_subsequence` as the reason, and add a regression test asserting a legacy `["codex", "grok", "opencode"]` snapshot is still accepted whole. Update `cli.py`'s two `enable-source` docstring references so the help text stops offering a host that no longer exists. _events.py + cli.py + tests, ~50 lines._ (M)
- **Sweep the prose** -- `README.md` (13 references), `SPEC.md` (5), `AGENTS.md` (3), `CLAUDE.md` (3), `docs/designs/host-parity.md` (7). Leaving docs that describe a removed host is worse than leaving the code, because the code is at least honest about returning nothing. `host-parity.md` is a design doc whose premise just changed from four hosts to three — correct it in place or archive it, do not leave it asserting the old shape. _docs, ~40 lines._ (S)

#### Group 37: Workspace bootstrap ∥ Source retirement

_Depends on: Group 36_

##### Track 37A: Make a fresh workspace able to run `verify:`
_2 tasks . ~60 LOC . low risk . 3 files_
_touches: .conductor/ (new), README.md, docs/ROADMAP.md_
_read-first: 36B_
_produces: every Track's `verify:` line is runnable in a fresh Conductor workspace_
_session: fresh · effort: low · verify: run the script from a clean clone, then run any Track's `verify:` line_

_Premise verified in a live workspace at `727f9cd`: no `.venv`, no `.conductor/`, no `bin/`, no `Makefile`; `python3` resolves to Homebrew 3.14 and `import mind_meld` raises `ModuleNotFoundError`. **Every card in this plan carries a `verify: pytest …` line that cannot run as written on a fresh workspace.** Reaching a baseline currently takes `python3.13 -m venv .venv` + `pip install -e .[dev]` by hand, rediscovered per workspace. Not release-bearing, which is why it rooms with 37B._

- **One bootstrap script** -- create the venv against a supported interpreter and `pip install -e .[dev]`, idempotent on re-run. Must not assume `python` is on PATH; macOS ships `python3` only, and Homebrew's `python3` is not necessarily a supported minor — resolve the interpreter explicitly rather than trusting the first match. _new script, ~40 lines._ (S)
- **Wire it to Conductor and document it** -- a `.conductor/` setup entry so a new workspace bootstraps itself, plus one README line for the non-Conductor path. _.conductor/ + README.md, ~20 lines._ (S)

##### Track 37B: Retire the OpenCode sync source and skill link
_2 tasks . ~120 LOC . medium risk . 4 files_
_touches: src/mind_meld/config.py, src/mind_meld/skill_link.py, tests/test_config.py, tests/test_skill_link.py, docs/invariants/sync.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_out: 39A_
_read-first: 36A, 28A, docs/invariants/sync.md, `cli._filter_disabled_sources` docstring (the P0 tombstone footgun, in full)_
_produces: an unsupported source stops syncing on every Mac without anyone running a command_
_session: fresh · effort: medium · verify: pytest tests/test_config.py tests/test_skill_link.py tests/test_source_toggle.py tests/test_integration.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_**No migration command. User decision 2026-09-01: mm ignores sources it does not support.** An earlier draft of this card required `mm migrate-config` plus a documented `mm disable-source opencode`, which is per-machine manual work to un-ship a feature the user never used. Retirement is automatic instead._

_**Reuse `disabled_sources`; do not invent a second mechanism.** Dropping a source from the walk without also stripping it from the manifest is a documented **P0 footgun** — `cli._filter_disabled_sources`'s docstring spells it out: `generate_tombstones` diffs prior against new, the new manifest no longer carries the source, so every file in it gets a deletion tombstone and **peers pull those tombstones and delete the content fleet-wide.** That filter already exists, is already applied at both consumer boundaries (`_pull_core`, `_push_core`), and is already asymmetric in the right way (sources stripped, prior tombstones preserved, per the 2026-04-25 Codex review). A retired source must route through it._

_**Preferred shape: inject at the load boundary, not the call sites.** Adding retired names to `disabled_sources` inside `load_config` means both consumers are automatically correct and `cli.py` is not touched at all — which is also what keeps this Track inside `max_files_per_track`. **Verify one side effect before committing to it:** `mm enable-source` / `disable-source` save the config back, so an injected name could get persisted to disk. Persisting is arguably fine — it is what a migration would have written — but it must be a decision, not an accident, and `mm enable-source opencode` must not resurrect a retired source._

- **Retire the source name** -- remove the `opencode` entry from `config.DEFAULT_SOURCES` and its half of `_GENERATED_HOST_SKILL_GLOBS` (the five globs are duplicated across the codex and opencode entries; only the opencode copy goes). Add a retired-names set beside `MM_INTERNAL_SOURCE_NAMES` and fold it into the effective `disabled_sources`. **Scope it to retired names only** — `_validate_sources` deliberately does not reject unknown source names, because `[[sync.sources]]` is user-extensible and `gstack` / `gstack-extend` are legitimate non-host entries; a rule like "ignore anything not in `DEFAULT_SOURCES`" would silently stop syncing a user's own custom source. Pin the tombstone behaviour with a test that retires a source holding files and asserts the push emits **no** new tombstones for it. _config.py, ~70 lines._ (M)
- **Drop the skill-link row** -- remove the `opencode` row from `skill_link.AGENT_ROWS`, which owns `~/.config/opencode/skills/retro-fleet` and its two markers. This is mm retiring a link **it** owns, which is NOT the user-deletion case Track 28A's guard protects — read that guard first so the two paths stay distinguishable, and leave an orphaned link on disk rather than growing a reaper for a directory mm does not own. _skill_link.py, ~50 lines._ (M)

#### Group 38: Conflict sidecar forensics

_Depends on: Group 36, Group 37_

##### Track 38A: Find and fix what deletes conflict sidecars
_3 tasks . ~220 LOC . high risk . 5 files_
_touches: src/mind_meld/cli.py, src/mind_meld/resolveflow.py, tests/test_conflict_copy.py, docs/invariants/conflicts.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: docs/invariants/conflicts.md (the inversion and sidecar-dedup sections in full)_
_produces: the deleter is named, and fixed if it is mm_
_session: fresh · effort: high · verify: pytest tests/test_conflict_copy.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Diagnose FIRST, then fix. The prohibition is on shipping a blind mitigation — a retry-on-vanish that papers over whatever is deleting user data — NOT on fixing the cause once it is named. If the cause is in mm, it is fixed in this PR. If the fix turns out to be large, it splits to a follow-up Track rather than bloating this card; if the cause is external, task 2 is discharged and says so._

_Measured 2026-09-01: `mm autopull` at 11:40:40 UTC logged 25 `conflicted` outcomes to `pull-history.jsonl`, and `.mind-meld-log.md` recorded a sidecar for `memory/user_path_order.md`. **Zero `.sync-conflict-*` files exist anywhere on disk** and `mm conflicts` reports none. Parent-directory mtimes across all three affected trees (`~/.claude/projects/*/memory/`, `~/.codex/skills/.system/**`, `~/.gstack/projects/*/`) read 07:41-07:42 local — the signature of a create followed by a delete, one minute after the pull. If real, the peer's bytes are discarded with no recoverable trail while the sync log tells the user to run `mm resolve`._

_Ruled out BY PROBE, not by reasoning — do not re-spend the session on these. The write path is correct (driving `cli._apply_conflict` in a tmp dir produces the sidecar under installed 0.12.50). The `mm gc` reapers are only reachable from the `gc` command, and `_gc_old_conflict_files` additionally requires `--conflicts` plus a 30-day bar. `retention._sweep_local_tmp_files` is scoped to `data/<device>/` and `manifests/<device>/` in the storage tree, never local source trees. `_existing_post_inversion_sidecars_from_peer` globs anchored to `canonical.stem`, so it cannot reap a sibling's sidecar. `bin/apply` in the agent-config repo contains no `rmtree` / `unlink` / `rsync --delete`. Nothing in the 0.12.35-0.12.50 changelog touches the sidecar write path — and the machine self-upgraded 0.12.34.1 → 0.12.50 one second before the observed pull, so reproduce on BOTH versions before concluding the cause is external._

_**One dependency edge deleted 2026-09-01.** This card previously carried `blocked-by` against the walker-substrate Track, an edge neither card's tasks support: sidecar forensics touches `cli.py` / `resolveflow.py`, the walker work touches `host_usage.py` / `token_usage.py`. It was collision bookkeeping read as a dependency, and it had buried "find what is deleting user data" behind two refactors. Losing user data outranks a net-negative refactor._

- **Reproduce under filesystem instrumentation** -- drive a real conflicting two-device pull under `fs_usage` (or an audit hook) scoped to the three trees, and name the process that unlinks. This task's deliverable is a named cause, recorded in `docs/invariants/conflicts.md` whichever way it resolves. _tests + throwaway harness, ~60 lines._ (M)
- **Fix it if it is mm** -- conditional on task 1. Scope is deliberately unsized because the cause is unknown; that is the honest state of this card, not an omission. If the deleter is an mm code path, it is fixed here with a regression pin in `tests/test_conflict_copy.py`. If it is external (another tool pruning a directory mm writes into), the finding is documented and mm's defence is task 3 alone. **A fix landing outside `_touches:` (e.g. `fsutil.py`, `retention.py`, `synclog.py`) means widen it and re-pack**, per the documented drift process; those were NOT pre-declared speculatively, because the probe ruled the reapers and the tmp sweeper out. _location unknown, ~100 lines._ (M)
- **Make the loss detectable rather than silent** -- ships REGARDLESS of what task 1 finds, and is the reason this Track is not purely investigative. A `conflicted` outcome whose sidecar is absent immediately afterwards is a condition mm can assert: one `exists()` stat on the path just written converts a silent discard into the visible-failure contract's `mm: warning:` line. Per that contract this warning reaches stderr even in quiet mode — it signals data-at-risk, so do NOT gate it behind `if not quiet:`. _cli.py, ~60 lines._ (S)

#### Group 39: Sync surface

_Depends on: Group 37_

##### Track 39A: Narrow the sync surface structurally
_2 tasks . ~180 LOC . medium risk . 4 files_
_touches: src/mind_meld/manifest.py, src/mind_meld/config.py, tests/test_manifest.py, tests/test_config.py, docs/invariants/sync.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 37B, docs/invariants/sync.md ("Generated files are not sync data", added v0.12.51)_
_produces: a generated directory drops out of sync without anyone adding a glob for it_
_blocked-by: Track 37B_
_session: fresh · effort: medium · verify: pytest tests/test_manifest.py tests/test_config.py tests/test_integration.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Both halves are follow-ups v0.12.51 named as its own known limitations. That release excluded 43 of 88 recorded conflicts by glob; these two close the parts a glob cannot express. **`blocked-by: 37B` is a real edge, not sequencing:** 37B deletes the opencode copy of `_GENERATED_HOST_SKILL_GLOBS`, and this Track rewrites the mechanism those globs implement. Running it first would re-add the copy 37B is removing._

- **Marker-aware directory skip in the walker** -- v0.12.51 excludes gstack-extend's per-host skill renders BY NAME because `exclude_patterns` are fnmatch globs against a relative path and cannot express "skip the directory CONTAINING this file." gstack-extend already drops `.extend-root` in every dir it renders. Until the walker can see it, every new gstack-extend skill silently starts conflicting fleet-wide until someone adds a glob. Touches `manifest.walk_generic_source`: read the tombstone-suppression invariant first, because a marker skip must not generate deletion tombstones, exactly as adding a glob must not. All four scenarios are pinned in `tests/test_integration.py::TestExcludePatterns5C`. _manifest.py + config.py, ~120 lines._ (M)
- **Exclude the pair-review state machine only** -- `projects/*/pair-review/session.yaml` (8 of the 88 conflicts) is a live per-machine state machine and definitionally cannot be shared. The prose artifacts (`deploy.md`, `report.md`, `parked-bugs.md` — 23 more conflicts) STAY in scope: pair-review advertises cross-machine resume as a feature, so excluding them removes capability rather than noise. **The measurement that makes that call defensible: across the same window these paths took 31 conflicts against 178 mtime-skips**, so the existing local-is-newer gate already absorbs 85% of the collisions and the residual does not justify removing a feature. The fuller fix is device-scoped artifact paths (`pair-review/<device>/`) in GSTACK, not an mm exclusion — file that upstream rather than absorbing it here. _config.py, ~60 lines._ (S)

#### Group 40: Cache encoding

_Depends on: Group 36, Group 37, Group 39_

##### Track 40A: Shrink the host cache encoding
_2 tasks . ~140 LOC . medium risk . 3 files_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_out: 42A, 43A_
_read-first: 32A, 34A, 36A_
_produces: the host cache stops scaling its per-push cost with corpus size, and Grok Build completes a scan_
_blocked-by: Track 36A, Track 37B_
_session: fresh · effort: medium · verify: pytest tests/test_host_usage.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Filed from Track 32A's `/review`. Measured 2026-08-28 on a 747-rollout / 694 MB corpus: 72,654 states, cache **0.40 MB → 13.48 MB**, json round-trip **56.3 ms of the 250 ms** autopush host budget. `locked_json_rmw` parses and re-serialises the whole file every push, so the cost is paid per push and scales linearly. At roughly 4x this corpus it consumes the budget and the reader can never converge. **Trigger: round-trip above 100 ms, or cache above 25 MB.** Gate confirmed 2026-09-01 at `host_usage.py:1118`, written `offset != size`._

_**This is now the ONLY thing making Grok Build invisible.** `mm diag` reports `grok prior successful scan: no` on device `889e42c0` despite `[retro] grok_host_usage = true`. Track 35A's gate held the xAI rate table behind two blockers; Group 36 dissolved the other one by deleting OpenCode, so this Track alone unblocks Grok pricing. It is also the reason the user's third supported agent currently reports nothing._

_**`read-first: 34A` added 2026-09-01** (filed from Track 34A's `/autoplan`, undrained since 2026-08-30): 34A persists a partial marker in the Grok cache entry, and this Track rewrites that exact encoding. The marker becomes one more field the increment encoding must carry through, and it was raised by neither review voice at the time._

- **Store per-state increments instead of absolute cumulatives** -- identity survives because both sides reconstruct from the same running sum, and increments are ~5 digits against ~8. Absorbs the filed Grok item: `_validated_grok_entry` requires `offset == size`, so a ledger that cannot be read end-to-end in one budget discards all its work forever — 60 ms wedges permanently at 15 files on the live corpus. Same persisted-offset fix `token_usage.walk_jsonl_segment` shipped for Claude in v0.12.15. _host_usage.py, ~90 lines._ (M)
- **Bound a single entry** -- a per-entry cap needs a degradation that keeps the file's tokens counted while dropping only its cross-file dedup. Refusing the file would re-create the fail-closed whole-store pathology Track 31A removed. `iter_bounded_lines` bounds line SIZE, not line COUNT; max observed is 1,234 states. _host_usage.py, ~50 lines._ (S)

#### Group 41: Git environment hygiene

_Depends on: Group 36, Group 37, Group 38_

##### Track 41A: Scrub the git environment for mm's git subprocesses
_1 task . ~40 LOC . low risk . 2 files_
_touches: src/mind_meld/events.py, tests/test_events.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 36B, docs/invariants/events-retro.md_
_produces: an inherited `GIT_DIR` can no longer misattribute one repo's commits to another_
_session: fresh · effort: low · verify: pytest tests/test_events.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Filed as S2 from Track 29A's `/autoplan` (2026-08-25) and re-verified at `727f9cd`: neither `subprocess.run` in `events.py` (`:1011` git log, `:1053` `remote.origin.url`) passes an `env=`, so `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_COMMON_DIR` are inherited. After Track 29A, classification is a `.git` **stat** that ignores the environment while `git log` still honours it, so every `.git`-bearing candidate can emit the env repo's `(canonical_remote, sha)` paired with its own `local_path` — the aggregator's exact dedup key. Exotic, but `autopush` runs from a hook whose environment mm does not control, which is precisely the unattended path._

- **Pass an explicitly scrubbed environment** -- strip the `GIT_*` variables that redirect repository resolution from both call sites, with a test that sets `GIT_DIR` to a decoy repo and asserts the walk still reports the candidate's own history. _events.py, ~40 lines._ (S)

#### Group 42: Walker substrate

_Depends on: Group 40_

##### Track 42A: One walker, two adapters
_3 tasks . ~350 LOC (net NEGATIVE) . medium risk . 4 files_
_touches: src/mind_meld/host_usage.py, src/mind_meld/token_usage.py, tests/test_host_usage.py, tests/test_token_usage.py, tests/test_module_boundaries.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 40A, 36A, 33A, docs/invariants/events-retro.md (incremental-resume section)_
_produces: one cache/resume/fingerprint implementation shared by Claude, Codex and Grok Build_
_blocked-by: Track 40A_
_session: fresh · effort: high · verify: pytest tests/test_host_usage.py tests/test_token_usage.py tests/test_module_boundaries.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_**Retitled and re-scoped 2026-09-01** from "three adapters" — Group 36 deletes the OpenCode reader, so the host side is Codex and Grok Build over the shared Claude walker. One of the four cache files goes with it, which is why the estimate dropped from ~400 to ~350 lines even as the premise strengthened._

_**Literal corrected 2026-09-01.** This card said `host_usage.py` is 1,730 lines. Measured **2,617** at `727f9cd` — it grew across Tracks 32A/33A/34A/35A, all of which landed in it. `token_usage.py` is 1,953, which the card had at 1,856. The premise DIRECTION strengthens (more duplication, not less) and the stale numbers are not carried forward. `host_usage.py` imports five names from `token_usage`; cache version, empty-cache, resume plan, cache-hit validation, head fingerprint, file identity, day extraction and counter coercion are each written two or three times. The genuinely host-specific surface is the record shape only._

- **Hoist the resume protocol** -- one file-identity + head/tail-digest + complete-line-offset implementation. Keep `iter_bounded_lines` where it is; it is already shared and is the proof the seam works. _host_usage.py + token_usage.py, ~150 lines._ (S)
- **Collapse the per-format readers to adapters** -- after Track 32A both remaining host readers are per-turn with a dedup key (Claude `message.id`, Grok `_grok_terminal_key`, Codex `turn_id`). An adapter's whole job becomes: given a file, yield `(dedup_key, day, model, usage)`. `_aggregate` and `_aggregate_grok` become one function. _host_usage.py, ~150 lines (net negative)._ (M)
- **Retire the duplicated leaf helpers and pin the boundary** -- one day parser, one counter coercion, preserving the trust-boundary split the aggregator documents (`_safe_int` for peer-controlled events, the shared helper for trusted local reads). Extend `tests/test_module_boundaries.py` so a reintroduced private copy fails the build. Also bound the cache's interned `models` table: `host_usage._add_usage` (`:2225`) materializes every distinct model the local corpus has ever seen into `by_day[day]["by_model"]`, and the 32/day and 64/row caps only apply later in `events._cap_by_model` (`:1820`, `:1829`). Capping in the READER is not the fix — it would break the "day totals stay whole" invariant that makes `day_total - sum(by_model)` an honest residual. _host_usage.py + tests, ~50 lines._ (M)

#### Group 43: Unified reporting

_Depends on: Group 36, Group 37, Group 40, Group 42_

##### Track 43A: Report every agent the same way
_2 tasks . ~200 LOC . medium risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 36A, 36B, 40A, 33A, docs/invariants/events-retro.md (Track 23A renderer contract, as revised)_
_produces: tokens and cost split per model family for all three agents; commits, LOC and PRs aggregate across all of them_
_blocked-by: Track 36A, Track 37B, Track 40A_
_session: fresh · effort: high · verify: pytest tests/test_retro_fleet_aggregator.py tests/test_docs_routing.py; ruff check .; ruff format --check ._

_Phase 3's end-state lands here. Premise re-verified 2026-09-01 at `727f9cd`: `_render_agent_inventory` and `AGENT_FAMILY_ROWS` are both still present in `aggregator.py`._

_**One instruction struck 2026-09-01.** The card previously read "OpenCode must keep reporting an honest empty." That empty was a **defect, not an absence of data** — the reader held 35 readable assistant rows and discarded all of them. As written the card would have pinned a bug as intended behaviour in a regression test, which is the same failure mode as Track 35A's struck instruction to retire a passing guard, one Track later. The instruction is moot now that Group 36 removes OpenCode, but it is why this Track is gated on 36A, 37B and 40A: **it renders three real agents, not two real and one empty.**_

_Note the asymmetry 36B creates and this Track must respect: OpenCode is gone from the reader and the config but **still a legal name on the wire**, because a legacy peer's row is rejected whole if it is not. The renderer must therefore ignore an inbound `opencode` source silently rather than surfacing it as a fourth agent or as a coverage gap._

- **One block, every agent** -- replace `_render_agent_inventory`'s diagnostic table with a per-model-family tokens-and-cost block shaped like `_render_token_block`, and drop `AGENT_FAMILY_ROWS`' `Claude (via agents)` disambiguation once there is one models block rather than two. Coverage reporting is not optional collateral: `_agent_coverage_notes` exists because "a vanished block must never be the diagnostic interface". An agent with genuinely no data still reports an honest empty — but verify per agent that the empty is absence, not a discard. _aggregator.py + SKILL.md, ~160 lines._ (L)
- **Everything else aggregates across models** -- commits, LOC, streak, commit-type mix, peak hours, bursts, ship-of-window, top repos, PR references and the trends table are cross-model fleet aggregates and must render as one number each. Pin it so a later per-agent split is a build failure rather than a review catch. Note the standing 24B decision that the card gets no new row. _aggregator.py, ~40 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not gating.

Adjacency list (from the packer):
```
- Group 36 ← {}
- Group 37 ← {36}
- Group 38 ← {36, 37}
- Group 39 ← {37}
- Group 40 ← {36, 37, 39}
- Group 41 ← {36, 37, 38}
- Group 42 ← {40}
- Group 43 ← {36, 37, 40, 42}
```

Track detail per group:
```
Group 36: Three hosts
  +-- Track 36A ........... ~S . 2 tasks (delete-only)
  +-- Track 36B ........... ~M . 2 tasks

Group 37: Workspace bootstrap | Source retirement
  +-- Track 37A ........... ~S . 2 tasks
  +-- Track 37B ........... ~M . 2 tasks

Group 38: Conflict sidecar forensics
  +-- Track 38A ........... ~L . 3 tasks

Group 39: Sync surface
  +-- Track 39A ........... ~M . 2 tasks

Group 40: Cache encoding
  +-- Track 40A ........... ~M . 2 tasks

Group 41: Git environment hygiene
  +-- Track 41A ........... ~S . 1 task

Group 42: Walker substrate
  +-- Track 42A ........... ~L . 3 tasks

Group 43: Unified reporting
  +-- Track 43A ........... ~L . 2 tasks
```

**Total: 8 groups . 10 tracks remaining. Critical path: 6 waves.**

---

## Future

Deferred: docs/roadmap-future.md (70 items)

## Shipped

History: docs/roadmap-shipped.md
