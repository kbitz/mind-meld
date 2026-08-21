<!-- /autoplan restore point: /Users/kb/.gstack/projects/kbitz-mind-meld/kbitz-extract-event-capture-autoplan-restore-20260815-143545.md -->

# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

---

## In Progress

_(none)_

---

## Current Plan

#### Group 24: Skill-link classification

##### Track 24A: Classify the wedge and stop re-arming it
_3 tasks . ~200 LOC . medium risk . 7 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/test_skill_link.py, tests/test_init_auto_pin.py, tests/test_init_events_backfill.py, tests/test_integration.py, docs/invariants/events-retro.md_
_out: 25A, 26A_
_read-first: docs/invariants/events-retro.md, src/mind_meld/skill_link.py_
_produces: a wedged link is classified by cause, and mm stops writing links that will dangle_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @tests/test_skill_link.py, @docs/invariants/events-retro.md · verify: pytest tests/test_skill_link.py -q_
- **Refuse an ephemeral skill source** -- `_resolve_retro_skill_src()` resolves from whichever interpreter ran `mm`, so an editable install in a Conductor workspace bakes a path Conductor later destroys. Add a durability predicate, an `ephemeral-source` status, and record the resolved source so drift is detectable without a resolve. _skill_link.py + tests, ~90 lines._ (M)
- **Split conflict into dangling-ours and foreign** -- one word currently covers four causes with four different remedies. Classify only; delete nothing. Rewrite both strings with cause, `readlink` output, and an exact remedy. Both tasks change the `SkillInstallStatus` set, so the invariant doc's status enumeration, its dangling/foreign state-machine lines, and its `install-skills` exit-code sentence all go stale in this track. _skill_link.py + cli.py + events-retro.md + tests, ~80 lines._ (M)
- **Fix the four None installer stubs** -- `test_init_auto_pin.py`, `test_init_events_backfill.py` (two sites), and `test_integration.py` return `None` where the installer returns a tuple, so a future consumer would raise `TypeError` into the swallowing `except Exception`. _tests, ~30 lines._ (S)

#### Group 25: Wedge visibility
_Depends on: Group 24_

##### Track 25A: Make a wedged link legible
_3 tasks . ~150 LOC . low risk . 6 files_
_touches: src/mind_meld/cli.py, README.md, docs/invariants/events-retro.md, tests/test_diag.py, tests/test_silent_failure_contract.py, tests/test_skill_link.py_
_blocked-by: Track 24A_
_out: 27A_
_read-first: 24A_
_produces: a broken link is discoverable from `mm diag` in one step, with a copy-pasteable fix_
_session: fresh · effort: medium · attach: @src/mind_meld/cli.py, @tests/test_diag.py, @tests/test_silent_failure_contract.py · verify: pytest tests/test_diag.py tests/test_silent_failure_contract.py -q_
- **Skill links block in mm diag** -- `mm diag` is cheap, passphrase-free, and already the documented triage surface; it has no skill-link section today. `mm status` costs a passphrase, a crypto init, a full local manifest, and a remote fetch — too expensive to consult about an `lstat`. _cli.py + tests, ~70 lines._ (M)
- **One mm status line only when broken** -- following the `_config_missing_recommended_excludes` precedent, and absorbing the deferred agent-coverage row. Never `events_degradations`: that field means the events tail lost data, `mm status` prints exactly one breadcrumb line, and a deliberate user file would pin it `degraded` forever while hiding real data loss. _cli.py + tests, ~50 lines._ (S)
- **README troubleshooting section** -- symptom, cause, fix, with commands. The ephemeral-workspace cause is currently written down only in a Python docstring and in the invariant doc — both maintainer-facing; no user-facing surface carries it. _README.md + events-retro.md, ~30 lines._ (S)

#### Group 26: Retro trends
_Depends on: Group 24, Group 25_

##### Track 26A: Make the trends section reach the shareable output
_2 tasks . ~120 LOC . medium risk . 5 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, README.md, docs/invariants/events-retro.md_
_blocked-by: Track 24A_
_read-first: src/mind_meld/skills/retro_fleet/aggregator.py, docs/invariants/events-retro.md ("No card-level change gate is possible")_
_produces: `## Trends vs last retro` renders on repeat runs, in the output the user keeps_
_session: fresh · effort: medium · attach: @src/mind_meld/skills/retro_fleet/aggregator.py, @docs/invariants/events-retro.md, @README.md · verify: pytest tests/test_retro_fleet_aggregator.py -q_
- **Break the save/compare circularity** -- `main()` renders the card iff `has_card_input` and saves the snapshot iff **not** `has_card_input`, and SKILL.md's second pass also passes `--no-save`, so pass 2 can only compare against the snapshot pass 1 wrote seconds earlier from an identical corpus. The delta set collapses to nothing and the section is skipped (`if not nonzero: return []`); a failed pass-1 save or a wall-clock `until` shift can leave a stray nonzero, so treat "always empty" as the overwhelming case rather than a guarantee. _aggregator.py + tests, ~80 lines._ (M)
- **Overturn the invariant that says this is impossible** -- `docs/invariants/events-retro.md` currently states "No card-level change gate is possible" and "deltas belong to the save-enabled first pass" as a written invariant, and `README.md` states the first-pass-only behavior as a user-facing caveat. This track reverses both. Amend the invariant with the new baseline rule and delete the README caveat; do NOT land the code while the docs still say it cannot work. _SKILL.md + README.md + events-retro.md + tests, ~40 lines._ (S)

#### Group 27: Target-list hygiene
_Depends on: Group 25_

##### Track 27A: De-landmine the target list
_3 tasks . ~220 LOC . medium risk . 7 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/conftest.py, tests/test_skill_link.py, tests/test_module_boundaries.py, AGENTS.md, docs/invariants/events-retro.md_
_blocked-by: Track 25A_
_out: 28A_
_read-first: 25A, tests/conftest.py_
_produces: a new agent row cannot escape test isolation or the real-home guard_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @tests/conftest.py, @tests/test_module_boundaries.py · verify: pytest tests/test_skill_link.py tests/test_module_boundaries.py -q_
- **Derive descriptors from one registry** -- keep a single patchable `name` to tilde-string mapping as the seam: `conftest._isolate_skill_links` redirects all roots with ONE `setattr` on `SKILL_ROOTS` and `test_module_boundaries` asserts that name, so a tuple of resolver callables would destroy the seam. Kill positional indexing and the fixed-arity annotation. _skill_link.py + cli.py, ~110 lines._ (M)
- **Registry-derived test isolation** -- conftest roots and `_is_real_agent_dir_under_pytest` paths both derive from the registry, pinned by a synthetic extra row that must be covered with zero other edits. The guard list carries a fourth entry that is NOT a skills root (`~/.config/mind-meld`, the marker dir); deriving purely from the registry would drop it, so keep it as an explicit extra. _conftest.py + tests, ~80 lines._ (M)
- **Pin marker literals and prefix uniqueness** -- on-disk names are not a uniform prefix (`.skill-link-checked` has none), so the Claude row carries an empty prefix and the test asserts the literal strings. Assert prefix uniqueness so one agent's marker cannot authorize another's target. Keep `_ensure_retro_skill_link_at` — it is the only test proving the *installer* blocks the write rather than just printing (`test_touch_marker_refuses_the_real_config_dir` proves it for the marker path, so the claim is installer-specific). _tests + docs, ~30 lines._ (S)

#### Group 28: Install consent
_Depends on: Group 27_

##### Track 28A: Gate the writes the way the reads are gated
_2 tasks . ~160 LOC . medium risk . 7 files_
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, src/mind_meld/skill_link.py, tests/test_config.py, tests/test_skill_link.py, README.md, docs/invariants/events-retro.md_
_blocked-by: Track 27A_
_out: 29A_
_read-first: 27A, src/mind_meld/events_tail.py_
_produces: a user can decline mm writing into their agent config dirs_
_session: fresh · effort: high · attach: @src/mind_meld/config.py, @src/mind_meld/events_tail.py, @tests/test_config.py · verify: pytest tests/test_config.py tests/test_skill_link.py -q_
- **Skills auto-install opt-out** -- `HOST_READER_SOURCE_GATE` will not `stat` `~/.codex/sessions` without consent, but mm writes a symlink into `~/.codex/skills/` with none. Add the config key plus per-agent toggles, defaulted to today's behavior, and report "auto-install: disabled" rather than calling it degradation. _config.py + tests, ~90 lines._ (M)
- **Pass consent into the installer** -- a `may_create` frozenset derived from `get_sources(config)`. Note `init` calls the installer BEFORE it resolves sources; that ordering has to flip. The invariant doc currently documents the skills-dir `mkdir` as unconditional under an available root; that sentence becomes conditional here. _cli.py + skill_link.py + tests, ~70 lines._ (M)

#### Group 29: Repair authority
_Depends on: Group 28_

##### Track 29A: Ownership-aware repair
_3 tasks . ~240 LOC . high risk . 5 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/test_skill_link.py, README.md, docs/invariants/events-retro.md_
_blocked-by: Track 28A_
_out: 30A_
_read-first: 28A, src/mind_meld/lockedjson.py_
_produces: mm can heal a link it provably wrote, and refuses everything else_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @src/mind_meld/lockedjson.py, @tests/test_skill_link.py · verify: pytest tests/test_skill_link.py -q_
- **Ledger-backed repair authority** -- a `readlink` tail plus a success marker proves only "points into some mind-meld tree" and "this target installed once"; neither binds the marker to the link, so a user's deliberate link into a second checkout would be deleted silently. Record the exact link text in a ledger via `lockedjson`; repair iff `os.readlink` byte-equals. _skill_link.py + tests, ~110 lines._ (M)
- **Race-safe replace** -- `dir_fd` plus `O_NOFOLLOW` pins directory identity so a swapped ancestor cannot redirect the write, and an atomic rename removes the replace window. POSIX has no atomic "unlink iff still pointing here", so document the residual. Reject symlinked skills dirs and ancestors for every row that exists at this point (Claude, Codex, OpenCode — the Grok row does not land until 29A). `config.grok_customization_dirs_exist` and `walk_grok_source` are the existing precedent for refusing a symlinked tree; match their posture. _skill_link.py + tests, ~90 lines._ (M)
- **Repair verbs and the reversed promise** -- check, repair, and force, mirroring `mm gc --dry-run`. Negative tests must patch `os.unlink`, because the existing no-unlink pin patches `Path.unlink` and would go blind. Repair reverses a no-clobber promise printed twice in README and twice in the invariant doc; all four change here. Do not fix only one site per file — the second invariant-doc site is the `install-skills` "leaves user files or foreign symlinks untouched" sentence, which is easy to miss. _cli.py + docs + tests, ~40 lines._ (S)

#### Group 30: Grok row
_Depends on: Group 29_

##### Track 30A: Add Grok as a registry row
_3 tasks . ~140 LOC . medium risk . 6 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/config.py, tests/test_skill_link.py, tests/test_config.py, docs/invariants/events-retro.md, docs/designs/host-parity.md_
_blocked-by: Track 29A_
_read-first: 27A, 28A, docs/designs/host-parity.md_
_produces: Grok gets the skill link without mm ever manufacturing Grok consent_
_session: fresh · effort: medium · attach: @src/mind_meld/skill_link.py, @src/mind_meld/config.py, @docs/designs/host-parity.md · verify: pytest tests/test_skill_link.py tests/test_config.py -q_
- **Install only where consent already exists** -- the installer's mkdir of `~/.grok/skills` flips `grok_customization_dirs_exist`, and that has two consequences with different preconditions. On a legacy or default config it auto-enables the grok sync source, which authorizes `read_grok_usage` — but only there: `get_sources` applies that filter solely while building `DEFAULT_SOURCES`, so an explicit `[[sync.sources]]` list is never appended to. On **any** config it also makes `_source_path_is_detected` label grok "detected" and default the `mm init` / `reconfigure-sources` prompt to Y. Test both shapes separately; do not assume the unconditional chain. Never create the directory. Assert the consequence, not the mechanism: after a full install, `grok_customization_dirs_exist()` is False AND `grok` is absent from `get_sources`. The second conjunct is vacuous on an explicit-`sync.sources` config (which `mm init` always writes), so the test needs a default-config fixture to have any teeth. Requires 27A first — otherwise running `pytest` writes the developer's real `~/.grok/skills` and manufactures the very consent this track exists to prevent. _skill_link.py + tests, ~70 lines._ (M)
- **One Grok home resolver, no env var** -- `GROK_HOME` stays a `host_usage` sessions-only override. A shared resolver honoring it would put an environment variable in charge of which directory `walk_grok_source` encrypts and publishes, and `conftest` deletes the variable, making that branch untestable by fixture. _config.py + tests, ~40 lines._ (S)
- **Per-row reasons and doc reconciliation** -- the "root is absent" reason is factually wrong under this gate, so reasons become per-row. Pin that the installed link yields zero manifest entries from `walk_grok_source`. Update host-parity.md's capability matrix AND its Plan C section — Plan C currently recommends `$GROK_HOME/skills` resolved at call time, which task 2 deliberately refuses, so leaving it unedited keeps the design doc recommending the thing this track rejects. Also the invariant doc's three-targets line. Record the manual host-load check: a green unit test proves the symlink, not that Grok loads the skill. _docs + tests, ~30 lines._ (S)

### Execution Map

A Group may launch when every Group in its `←` set has landed, regardless
of document order; document order is priority, not a gate.

```
- Group 24 ← {}
- Group 25 ← {24}
- Group 26 ← {24, 25}
- Group 27 ← {25}
- Group 28 ← {27}
- Group 29 ← {28}
- Group 30 ← {29}
```

Track detail per group:

```
Group 24: Skill-link classification
  +-- Track 24A ........... ~M . 3 tasks

Group 25: Wedge visibility
  +-- Track 25A ........... ~M . 3 tasks

Group 26: Retro trends
  +-- Track 26A ........... ~M . 2 tasks

Group 27: Target-list hygiene
  +-- Track 27A ........... ~M . 3 tasks

Group 28: Install consent
  +-- Track 28A ........... ~M . 2 tasks

Group 29: Repair authority
  +-- Track 29A ........... ~M . 3 tasks

Group 30: Grok row
  +-- Track 30A ........... ~M . 3 tasks
```

**Total: 0 phases . 7 groups . 7 tracks remaining.**

Six waves, and there is no parallelism left to harvest. Every skill-link track
collides in `src/mind_meld/skill_link.py`, and once the tracks that amend
`docs/invariants/events-retro.md` are named honestly, that doc becomes the second
shared hot file — it is touched by 24A, 25A, 26A, 27A, 28A and 29A. The retro-trends
work (26A) is topologically independent of the installer chain but still serializes
behind 25A on that file, which is why it earned its own Group instead of riding
along with 24A. Two workspaces cannot both edit the invariant doc, so the honest
plan is a chain.

---

## Future

- **cli.py micro-cleanups** — retain only the unresolved `_resolve_mm_events_dir` and status-enum follow-up; Track 18A owns the import and `_empty_outcomes` portions.
- **`_resolve_interactive_loop` decomposition** — collector removal fired its old trigger, but the 630-line function still needs a dedicated discovery/design pass before code commitment.
- **Two-machine test bootstrap duplication** — consolidate the repeated setup only when adjacent test work touches both modules.
- **Cold-cache budget leftovers** — revisit identity-refresh and per-jsonl deadline concerns only with fresh measurements; root discovery is shipped.
- **identity.py micro-DRY and token-cache pins** — keep as low-priority hygiene.
- **v0.11.17 doc-drift cleanup** — re-evaluate only with an invariant-doc pass.
- **Incremental-resume accepted divergences** — act only on evidence from a corpus census.
- **Future-clamped peer mtime** — advisory watch item.
- **`_promote_target_will_sync` ignores `exclude_patterns`** — rare exclude-glob miss.
- **Similarity classifier and silent merge** — blocked on a real collector dataset.
- **Peers never resolved against can be mtime-skipped by the drain** — watch after Group 12's shipped fix.
- **Abort transactionality** — pre-existing torn-state concern.
- **Price-cache TTL split** — wire-format work that competes with token-cache ownership.
- **Model-ID variant suffix aliases** — defer until a real variant appears in a census.
- **Rendered-cost sanity ceiling** — presentation safeguard without an observed bad value.
- **Parallel blob fetch** — revisit after user-reported sustained slow pulls.
- **Selective sync** — wait for a user with a large-project filtering need.
- **Mtime hash cache** — revisit only if push latency becomes user-visible again.
- **Three-way merge base** — wait for a divergence-misclassification report.
- **`mm rekey` passphrase rotation** — post-1.0 format-v3 migration work.
- **Blob-directory peer recovery** — wait for a real corrupt-manifest support case.
- **PyPI publish workflow** — wait for a distribution-demand signal.
- **Cross-device source rename identity** — known limitation until an incident demonstrates it.
- **Explicit upgrade check** — add only when cached status is insufficient.
- **Subprocess pipx upgrade** — revisit after nudge UX proves inadequate.
- **`MM_NO_VERSION_CHECK=1`** — add only for demonstrated CI ergonomics.
- **GitHub-tag pagination** — deferred until tag count approaches the API page limit.
- **Autouse devices-write-lock coupling** — forward-defense only.
- **`[retro].deny_emails`** — wait for a credential or account-hygiene need.
- **Snapshot-level completeness** — separate data-model design; do not conflate with host snapshots.
- **Retro-card machine/cause diagnostics** — defer until the baseline model card lands.
- **`mm diag` discoverability and analytics** — keep scoped to the existing command rather than adding a new one.
- **Diagnostic-string quality pass** — adopt only with a broader user-facing notice pass.
- **Per-verb or sticky autorun breadcrumbs** — the v0.12.16 signal is an improvement but still overwritable.
- **CT-4 enforcement and short-write handling** — storage-boundary hardening outside this batch.
- **Residual Track 16A coverage gaps** — three defensive or theoretical cases from the ship audit. _Source: [ship] coverage audit 2026-08-15._
- **Host-usage cache GC reaper** — extend `mm gc` and its dry-run path to remove stale Codex, Grok, and OpenCode cache entries without weakening complete-pass pruning. _Source: unprocessed host-cache follow-up 2026-08-17._
- **Active host-session degradation policy** — consider skipping a stale or partial final rollout when its next completed record restates usage; preserve all-or-nothing publication until that proof exists. _Source: unprocessed host-usage follow-up 2026-08-17._
- **Warm host-scan scaling** — revisit fingerprint-every-file cost with a measured corpus before the 250 ms autopush budget becomes user-visible. _Source: unprocessed host-cache follow-up 2026-08-17._
- **Machine-readable GC outcomes** — expose orphan-blob outcomes only when an automation or audit consumer needs them; Track 17D's reaper scope stays as shipped. _Source: unprocessed GC follow-up 2026-08-17._
- **Do not add a Codex or Grok sessions-snapshot** — Claude's sessions-snapshot walk stays Claude-only. Codex rollouts and Grok session dirs are not a metadata-only project ledger; encoded cwd is a path and must not go on the wire. Promote only if a host ships a metadata-only project index. Session-transcript sync stays refused for every host. _Source: [manual] host-parity 2026-08-17._
- **Deterministic demo/fixture path for `retro-fleet`** — fresh-clone time-to-first-output is 10-30 min and nondeterministic (3.11+ interpreter, venv, editable install, `mm init`, an enabled host source, a substantive push, two aggregator passes). A `--demo` flag over a bundled synthetic corpus would make the card reproducible in three commands. The test suite is the current deterministic path and is adequate for CI, so this is ergonomics, not correctness. _Source: [23A] inbox 2026-08-18._
- **Un-hide and rename `--dump-host-usage`** — hidden from `--help`, and documented only in CHANGELOG and the maintainer-facing invariant doc, so the primary forensic hatch is undiscoverable, and the name reads like spend when it is retained inventory. Prefer `--host-inventory-json`, keeping the old flag as an alias. _Source: [23A] inbox 2026-08-18._
- **Accept a bare integer retro window** — `mm retro-fleet 7` is rejected while `7d` works. The skill translates natural language so agents never hit it; direct CLI users do. _Source: [23A] inbox 2026-08-18._
- **Deregister/prune retired devices** — a retired-but-registered Mac inflates every "N of M machines" denominator forever, which is the root cause 23A's coverage wording worked around rather than fixed. Wants `mm devices --prune` or a staleness nudge. _Source: [23A] inbox 2026-08-18._
- **Reset-aware per-device snapshot deltas** — the only honest route to real per-agent window spend, versus the lower-bound day counts 23A ships. Needs at least two retained snapshots per device (22A keeps only the latest), counter-reset detection, and a new wire/consumer invariant. A data-layer track, not a renderer change. _Source: [23A] inbox 2026-08-18._
- **Two releases share the version string `0.11.23`** — `CHANGELOG.md` carries `## [0.11.23] - 2026-05-06` and `## [0.11.23] - 2026-05-05` as separate releases; `docs/PROGRESS.md` charts only the 2026-05-05 one, so the 2026-05-06 "auto-pin iCloud storage on `mm init`" release has no row. `test_every_changelog_version_has_a_progress_row` matches on version string, so the gate cannot see this and reports parity. Needs a decision: renumber one release, or teach the gate to key on (version, date). _Source: /ship adversarial review 2026-08-21._
- **Fleet-wide skill-link visibility** — markers and links are per-machine, so nothing answers "which of my Macs has a wedged link", which is the real question behind surfacing a wedge. One field on the mm-push event would let the retro card answer it. Deferred as a wire-format change, outside the installer's blast radius. _Source: /autoplan Phase 3 eng review 2026-08-20._

## Shipped

### Phase 1: v0.x → v1.0 cleanup sweep ✓ Shipped (v0.11.24)

**End-state:** Decomposition + DRY, init flow, test hygiene, release infra, conflict UX, mm-events foundation, retro-fleet skill, post-1.0 polish — all shipped through v0.11.24.
**Groups:** 1, 2, 3, 4, 5, 6, 7, 8, 9, 10

#### Group 1: Decomposition + DRY ✓ Shipped (v0.8.4–v0.8.6)

The proposed `constants.py` extraction preflight was dropped on 2026-04-24 after a `/plan-eng-review` cohesion check: only `FORMAT_VERSION` and `CONFLICT_INFIX` are genuinely cross-module of the four candidates, and even those pair tightly with siblings the original task missed.

- Track 1A — _shipped (v0.8.4): decompose `_pull_core` + `_apply_incoming_file`. 2 tasks shipped._
- Track 1B — _shipped (v0.8.5): walker + manifest + merge DRY. 3 tasks + 1 contract-change cleanup shipped._
- Track 1C — _shipped (v0.8.6): post-1A cli.py follow-ups — diff-call-site DRY, GC blob-shape validation, autopull degraded breadcrumb. 3 tasks shipped._

#### Group 2: Init flow + sync_log generalization + config polish ✓ Shipped (v0.8.7–v0.8.8)

Multi-source assumption lag resolved — `init` no longer hardcodes `~/.claude` and `write_sync_log` is type-keyed instead of name-keyed.

- Track 2A — _shipped (v0.8.7): init decomposition + DEFAULT_SOURCES reuse + sync_log generalization. 5 tasks + refinements shipped._
- Track 2B — _shipped (v0.8.8): config polish — backfill path preservation + ConfigError prefix rename. 2 tasks shipped._

#### Group 3: Test hygiene + style polish ✓ Shipped (v0.8.10)

Pre-flight style polish (type hints, optional syntax, placeholderless f-strings, keyring except-narrowing) + Track 3A test improvements all landed together. Codex `/review` caught a P0 keyring-propagation gap during review.

- Track 3A — _shipped (v0.8.10): CliRunner migration + combined push/pull/conflict E2E + 86 lazy-import hoists. 3 tasks shipped + 3 codex-follow-through pins._

#### Group 4: Release infrastructure ✓ Shipped (v0.8.11)

Single `macos-latest` + Python 3.13 GitHub Actions workflow runs `ruff check`, `ruff format --check`, `pytest tests/`, wheel build + install + `mm --version` smoke, and asserts the real Keychain backend loads.

- Track 4A — _shipped (v0.8.11): GitHub Actions CI + ruff pin + README badge + 113-violation drift sweep. 1 task shipped._

#### Group 5: Conflict UX & first-pull polish ✓ Shipped (v0.8.15–v0.9.4)

Surfaced 2026-04-24 from a fresh-Mac first-pull session. Tracks 5A/5B/5C/5E shipped through v0.9.2 (plus a v0.9.3 hotfix patch); Track 5D shipped as v0.9.4.

- Track 5A — _shipped (v0.8.15): auto-command silent-mode + scope bugs + Group 5 preflight. 3 tasks + 1 preflight._
- Track 5B — _shipped (v0.9.0): pull / resolve / conflicts UX surfaces. 4 tasks + scope expansions. **BREAKING.** 14 new tests._
- Track 5C — _shipped (v0.9.1): exclude_patterns + log + migration UX. 38 tests + 5 IRON RULE pins. Pivoted via /plan-ceo-review from conflict inversion + real-merge backends._
- Track 5D — _shipped (v0.9.4): Track 5A adversarial-review follow-ups — `_find_conflict_files` dedup, `_register_and_save` order swap with cleanup, push-time self-heal. 15 new tests._
- Track 5E — _shipped (v0.9.2): conflict default inversion. **BREAKING.** Added `packaging>=21.0`. 11 tests in `TestInversion5E`._
- _v0.9.3 hotfix patch (post-Track-5C): added `config.yaml` to gstack source's default `exclude_patterns`._

#### Group 6: Release infrastructure polish ✓ Shipped (2026-04-27)

Single-track Group bundling release-discipline polish. Independent of the auto-upgrade code path.

- Track 6A — _shipped: GitHub Releases backfill — 36 release entries created via `gh release create`, one per existing tag v0.1.0..v0.10.0. 0 source LOC._

#### Group 7: mm-events foundation (event capture) ✓ Shipped (v0.10.1–v0.10.3)

Implements the foundation for the fleet-aware retro per `docs/archive/fleet-retro.md`: per-device JSONL event log written on every push.

- Group 7 preflight — _shipped (v0.10.1): security/concurrency/correctness sweep + mm-events default source. 8 items shipped._
- Track 7A — _shipped (v0.10.2): events.py module — canonicalize_remote_url, walk_git_projects budget+concurrency, walk_session_metadata Conductor-ephemeral detection, cursor + write_push_event under flock. 4 tasks shipped._
- Track 7B — _shipped (v0.10.3): _push_core wiring + gc retention. 3 tasks shipped._

**Hotfix:** `get_sources` bootstrap fires on every read-only command — _shipped v0.11.2 (2026-04-29). Module-level `_BOOTSTRAP_WARNED_PATHS` short-circuits subsequent calls in same process; pinned by regression test._

#### Group 8: retro-fleet skill (consumer) ✓ Shipped (v0.11.0)

User-facing surface of the fleet-aware retro: a Claude Code skill shipped in the mm wheel and symlinked into `~/.claude/skills/` at `mm init`.

- Track 8A — _shipped (v0.11.0): SKILL.md + aggregator + symlink installer + CI smoke test. ~1100 LOC; 1107/1107 tests pass; 13 review-applied lock-ins from /plan-eng-review._

**Hotfix** (post-ship; serial):
- _Aggregator `mm-events` custom-path notice [adversarial 2026-04-28]: print `mm: notice:` from aggregator when default events dir is empty but config has non-default `mm-events` path._
- _Aggregator memory: streams + filters per-file rather than slurping entire corpus [adversarial 2026-04-28]._
- _walk_session_metadata budget: pin benchmark + bump budget OR reintroduce mtime fast-path [adversarial 2026-04-28]._
- _Sessions dedup key: include source root path to avoid encoded-name collisions across multiple Claude source roots [adversarial 2026-04-28]._
- _Mid-upgrade peer pre-v0.11.0 breadcrumb persists after upgrade [adversarial 2026-04-28]: window naturally moves past v=1 snapshots within 7-30 days._

#### Group 9: Pull performance + fresh-Mac onboarding ✓ Shipped (v0.11.23)

Surfaced 2026-04-27 from a pull-perf dogfood session on kb's 349C-kb-ms. Scope reduced via /plan-eng-review (2026-05-06) from a 2-task plan paired with 150-line parallel-fetch optimization to a 5-line auto-pin nudge — once storage is pinned, `mm pull` reads resident blobs and is already fast (<5s on 1449-blob workload).

- Track 9A — _shipped (v0.11.23): auto-pin iCloud storage on `mm init` via `brctl download`. 1 task shipped._

#### Group 10: Token-usage post-ship cleanup ✓ Shipped (v0.11.24)

Four DRY + perf items deferred from /ship pre-landing reviews of the v0.11.14+ token-usage work. All scoped to internal hygiene — no public-API change, no user-visible behavior change.

- Track 10A — _shipped (v0.11.24): token-usage DRY + perf polish. Consolidated 4 bucket-merge sites behind `merge_usage_bucket` / `merge_by_model` + `TOKEN_FIELDS` constant + `zero_day_bucket` / `zero_model_bucket` factories. 4 tasks shipped._

### Group 11: Token-cache + cold-cache correctness fixes ✓ Shipped (v0.12.4–v0.12.5)

The two `necessary` correctness fixes from /full-review 2026-05-10. Loose Group — post-1.0, outside the v0.x → v1.0 sweep above.

- Track 11A — _shipped (v0.12.5): token-cache invariant ownership consolidation — autouse `_isolate_token_cache` fixture + `gc_cache_entries` routed through `lock_and_get_files`. Cross-model HIGH unknown-top-level-key-stripping regression caught and fixed during /review. 5 new pinning tests._
- Track 11B — _shipped (v0.12.4): cosmetic-only Skills incomplete breadcrumb admits cold-cache push as a second cause. Original Option B 3-LOC events.py fix rejected during /plan-eng-review — would cause latest-snapshot-wins data erasure on warm-then-cold push ordering._

### Group 12: inline keep-canonical mtime bump ✓ Shipped (v0.12.7)

Deferred the inline `keep-canonical` mtime bump to end-of-pull-batch so `mm pull --conflict-mode prompt` choosing local propagates across the fleet without mid-walk later-peer skip.

- Track 12A — _shipped (v0.12.7): `pending_inline_bumps` drained after all peer walks. 13 tests in `TestResolveLocalMtimeBump`._

### Group 14: Symlink policy on both push and apply paths ✓ Shipped (v0.12.17)

Child symlinks are now local routing rather than sync content: generic walkers omit them, pull preserves live and dangling local links, and source roots may still be symlinked. Prior-manifest filtering prevents this policy change from generating deletion tombstones; generated Codex/OpenCode skill trees are excluded without excluding hand-authored skills.

- Track 14A — _shipped (v0.12.17): tombstone-safe child-symlink suppression, pull-time link preservation, generated-skill exclusions, invariant documentation, and regression coverage._

### Group 13: Hotfix: events-tail malformed-byte resilience ✓ Shipped (v0.12.16)

- Track 13A — _shipped (v0.12.16): binary-safe session reads, degradation breadcrumb, and regression coverage._

### Group 15: Post-ship cleanup sweep ✓ Shipped (v0.12.18–v0.12.20)

- Track 15A — _shipped (v0.12.18): remove obsolete token-usage exports and migrate their test coverage._
- Track 15B — _shipped (v0.12.20): remove frozen state-path constants and the dead upgrade import._
- Track 15C — _shipped (v0.12.19): make retro-aggregator imports explicit and remove the obsolete importer._

### Group 16: cli.py decomposition ✓ Shipped (v0.12.21)

- Track 16A — _shipped (v0.12.21): remove the unused collector, extract six cohesive modules, tighten isolation and routing gates, and update release documentation._

### Group 17: Post-extraction and host-reader foundations ✓ Shipped (v0.12.22–v0.12.25)

- Track 17A — _shipped (v0.12.24): descriptor-driven Claude/Codex/OpenCode skill installation with truthful partial results._
- Track 17B — _shipped (v0.12.25): one private event-capture path shared by push and init while retaining their separate policies, writes, and budgets._
- Track 17C — _shipped (v0.12.24): canonical host-family classification, isolated incremental Codex usage cache, and bounded local reader._
- Track 17D — _shipped (v0.12.23): retention reapers plan before mutation, including byte/metadata-preserving dry-run coverage._
- Track 17E — _shipped (v0.12.22): distinct repository-qualified GitHub PR identity in retro aggregation._

### Group 19: Host snapshot writer and resolver hygiene ✓ Shipped (v0.12.32–v0.12.33)

- Track 19A — _shipped (v0.12.33): additive, all-or-nothing host-usage snapshots from the event tail and init backfill._
- Track 19B — _shipped (v0.12.32): resolver continuation and skip-default cleanup following the rendering seam._

### Group 20: Host snapshot wire contract ✓ Shipped (unreleased)

- Track 20A — _shipped: documented and pinned the complete, coverage-aware snapshot contract that distinguishes omission from an explicit empty observation._

### Group 18: Finish the non-Claude host-usage foundation ✓ Shipped (v0.12.26–v0.12.34)

The host reader, model-family card baseline, root budgets, CLI safety work,
and the Grok v1 terminal-usage reader have landed.

- Track 18A — _shipped (v0.12.26): post-decomposition CLI safety sweep._
- Track 18B — _shipped (v0.12.31): centralize conflict diff rendering and choice normalization._
- Track 18C — _shipped (v0.12.28): budget root discovery and no-op heartbeat behavior._
- Track 18E — _shipped (v0.12.29): render model-family rows from existing data._
- Track 18D — _shipped (v0.12.34): read Grok Build's v1 terminal usage ledger._

### Group 21: Opt in and publish Grok usage snapshots ✓ Shipped (v0.12.34)

- Track 21A — _shipped (v0.12.34): gate and publish trusted Grok usage. At 21A, `mm enable-source grok` was usage-only; Track 22B later added its scoped sync source._

### Group 22: Host snapshot merge ∥ Grok customization source ✓ Shipped (v0.12.35–v0.12.36)

- Track 22A — _shipped (v0.12.36): retain each device's latest complete host-usage snapshot as last-known-good inventory. Host-family day maps stay whole, never sliced or summed into fleet window spend. Hidden `mm retro-fleet --dump-host-usage` for forensics._
- Track 22B — _shipped (v0.12.35): `mm enable-source grok` adds the one scoped `type = "grok"` source and retains the 21A host-usage consent bit. The walker syncs only `skills/`, `commands/`, and `rules/`; sessions, credentials, config, bundled files, and child links remain local._

### Group 23: MODELS card coverage ✓ Shipped (v0.12.37)

- Track 23A — _shipped (v0.12.37): the `AGENT LOGS` block reports per-model-family activity rhythm and never magnitude, `MODELS` states its own provenance in the header, and an absent block always names its cause._

_Track 23B ("Install retro-fleet into Grok skills") was dissolved on 2026-08-20
after failing its `/autoplan` premise gate 6/6 across two models: the installer's
mkdir would have manufactured the Grok consent signal, the 4-file scope estimate
ran against an 11-file empirical baseline, and its `blocked-by` edge on 22A was
fabricated. Its intent survives as Group 29; the prerequisites it was missing are
Groups 24 through 28. Review:
`~/.gstack/projects/kbitz-mind-meld/kbitz-track-23b-autoplan-phase1-ceo-20260820.md`._
