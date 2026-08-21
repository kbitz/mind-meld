<!-- /autoplan restore point: /Users/kb/.gstack/projects/kbitz-mind-meld/kbitz-extract-event-capture-autoplan-restore-20260815-143545.md -->

# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

---

## In Progress

_(none)_

---

## Current Plan

### Phase 2: Durable agent skill links

**End-state:** Every agent `retro-fleet` link points at an mm-owned constant path, a wedged link is named by `mm diag` in one step, a user can decline the write, and Grok gets a row without mm manufacturing consent.
**Groups:** 24, 25, 26, 27


#### Group 24: Durable skill store ∥ Retro trends

##### Track 24A: Own the skill store
_2 tasks . ~250 LOC . high risk . 10 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/conftest.py, tests/test_skill_link.py, tests/test_diag.py, tests/test_silent_failure_contract.py, docs/invariants/events-retro.md, pyproject.toml, CHANGELOG.md, docs/PROGRESS.md_
_out: 25A, 25B_
_read-first: docs/invariants/events-retro.md, src/mind_meld/skill_link.py_
_produces: agent links point at ~/.local/share/mind-meld/agent-skills/retro-fleet/; mm diag names a wedged link; live checkout links are never touched_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @tests/test_skill_link.py, @docs/invariants/events-retro.md · verify: /tmp/mm-24a-probe/bin/python -m pytest tests/test_skill_link.py tests/test_diag.py tests/test_silent_failure_contract.py -q_
- **Publish SKILL.md into an mm-owned store and migrate by liveness** -- copy SKILL.md only (never aggregator.py) into `~/.local/share/mind-meld/agent-skills/retro-fleet/` via `fsutil.atomic_write_bytes`; gate every link step on a verified non-empty publish in this run; `lstat`-refuse a symlink or regular file at the store dir and payload; catch `(OSError, StorageError)`; monotonic `packaging.version` then hash (republish on equal-version-differing-hash with a notice; never silent-downgrade); store-freshness term on the 24h gate; dedicated store flock; `dry_run=True` returns full classifications with zero writes. Migration 2x2: live checkout leave-alone+notice, live package migrate, dangling package repair, dangling checkout repair. Status set: `installed | unchanged | unavailable | dangling-ours | dangling-ours-legacy | foreign | failed`. `_skill_store_dir()` call-time seam + fifth pytest-guard entry. Quiet-gate: mutation only when `not quiet`. User-facing strings print `readlink` + cause + copy-paste fix. `mm diag` skill-links block + one-line `mm status` nag. `.mm-skill.json` sidecar. Land the 24 new tests and rewrite the ~25 invalidated pins. _skill_link.py + cli.py + conftest.py + tests, ~220 lines._ (L)
- **Release bookkeeping and the installer invariant** -- bump `pyproject.toml` + `CHANGELOG.md` + `docs/PROGRESS.md` (CI gate). Rewrite the `events-retro.md` installer section (status set, store path, liveness table, publish-before-link). One-paragraph reversal of "No card-level change gate is possible" so 24B can land in parallel. Rewrite `install_skills --help` (the pipx auto-update sentence is now a lie). _docs + pyproject, ~30 lines._ (S)

##### Track 24B: Make the trends section reach the shareable output
_2 tasks . ~120 LOC . medium risk . 5 files_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py, README.md, CHANGELOG.md, docs/PROGRESS.md_
_out: 25B_
_read-first: src/mind_meld/skills/retro_fleet/aggregator.py, docs/invariants/events-retro.md_
_produces: `## Trends vs last retro` renders on repeat runs, in the output the user keeps_
_session: fresh · effort: medium · attach: @src/mind_meld/skills/retro_fleet/aggregator.py, @docs/invariants/events-retro.md, @README.md · verify: /tmp/mm-24a-probe/bin/python -m pytest tests/test_retro_fleet_aggregator.py -q_
- **Break the save/compare circularity** -- `main()` renders the card iff `has_card_input` and saves the snapshot iff **not** `has_card_input`, and SKILL.md's second pass also passes `--no-save`, so pass 2 can only compare against the snapshot pass 1 wrote seconds earlier from an identical corpus. The delta set collapses to nothing and the section is skipped (`if not nonzero: return []`); a failed pass-1 save or a wall-clock `until` shift can leave a stray nonzero, so treat "always empty" as the overwhelming case rather than a guarantee. _aggregator.py + tests, ~80 lines._ (M)
- **User-facing two-pass docs** -- SKILL.md and README currently state the first-pass-only caveat. Reverse both to match the new baseline rule. The invariant-doc sentence is rewritten in 24A (220 lines from the installer section 24A already owns) so this Track does not touch `events-retro.md`. Bump `pyproject.toml` in this PR (not on `_touches:` — listing it would false-serialize against 24A; CHANGELOG + PROGRESS are the declared release files). _SKILL.md + README.md + tests, ~40 lines._ (S)

#### Group 25: Target-list hygiene ∥ SKILL.md preflight
_Depends on: Group 24_

##### Track 25A: De-landmine the target list
_3 tasks . ~220 LOC . medium risk . 9 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/conftest.py, tests/test_skill_link.py, tests/test_module_boundaries.py, AGENTS.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 24A_
_out: 26A_
_read-first: 24A, tests/conftest.py_
_produces: a new agent row cannot escape test isolation or the real-home guard_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @tests/conftest.py, @tests/test_module_boundaries.py · verify: /tmp/mm-24a-probe/bin/python -m pytest tests/test_skill_link.py tests/test_module_boundaries.py -q_
- **Derive descriptors from one registry** -- keep a single patchable `name` to tilde-string mapping as the seam: `conftest._isolate_skill_links` redirects all roots with ONE `setattr` on `SKILL_ROOTS` and `test_module_boundaries` asserts that name, so a tuple of resolver callables would destroy the seam. Kill positional indexing and the fixed-arity annotation. _skill_link.py + cli.py, ~110 lines._ (M)
- **Registry-derived test isolation** -- conftest roots and `_is_real_agent_dir_under_pytest` paths both derive from the registry, pinned by a synthetic extra row that must be covered with zero other edits. The guard list carries a fifth store entry (24A) that is NOT a skills root; keep it as an explicit extra alongside the marker-dir extra. _conftest.py + tests, ~80 lines._ (M)
- **Pin marker literals and prefix uniqueness** -- on-disk names are not a uniform prefix (`.skill-link-checked` has none), so the Claude row carries an empty prefix and the test asserts the literal strings. Assert prefix uniqueness so one agent's marker cannot authorize another's target. Keep `_ensure_retro_skill_link_at`. Bump `pyproject.toml` in this PR (same VERSION-as-pyproject caveat as 24B). _tests + docs, ~30 lines._ (S)

##### Track 25B: SKILL.md Step 0 and README troubleshooting
_2 tasks . ~80 LOC . low risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/SKILL.md, README.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 24A, Track 24B_
_read-first: 24A, 24B_
_produces: SKILL.md fails closed when mm is gone; README names the store, the three links, and the uninstall leftover_
_session: fresh · effort: medium · attach: @src/mind_meld/skills/retro_fleet/SKILL.md, @README.md · verify: /tmp/mm-24a-probe/bin/python -m pytest tests/test_skill_link.py -q_
- **SKILL.md Step 0 preflight** -- `command -v mm` and `min_mm_version` from `.mm-skill.json` at the top of SKILL.md, before any mm invocation. Step 1's "don't treat a non-zero exit as fatal" must not cover a missing binary. _SKILL.md, ~40 lines._ (S)
- **README troubleshooting and uninstall** -- symptom/cause/fix; name the three links and the store; rewrite the no-clobber sentences at `:180` and `:281`. Bump `pyproject.toml` in this PR (same caveat as 24B). _README.md, ~40 lines._ (S)

#### Group 26: Install consent
_Depends on: Group 25_

##### Track 26A: Gate the writes the way the reads are gated
_2 tasks . ~160 LOC . medium risk . 8 files_
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, src/mind_meld/skill_link.py, tests/test_config.py, tests/test_skill_link.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 25A_
_out: 27A_
_read-first: 25A, src/mind_meld/events_tail.py_
_produces: a user can decline mm writing into their agent config dirs_
_session: fresh · effort: high · attach: @src/mind_meld/config.py, @src/mind_meld/events_tail.py, @tests/test_config.py · verify: /tmp/mm-24a-probe/bin/python -m pytest tests/test_config.py tests/test_skill_link.py -q_
- **Skills auto-install opt-out** -- `HOST_READER_SOURCE_GATE` will not `stat` `~/.codex/sessions` without consent, but mm writes a symlink into `~/.codex/skills/` with none. Add the config key plus per-agent toggles, defaulted to today's behavior, and report "auto-install: disabled" rather than calling it degradation. _config.py + tests, ~90 lines._ (M)
- **Pass consent into the installer** -- a `may_create` frozenset derived from `get_sources(config)`. Note `init` calls the installer BEFORE it resolves sources; that ordering has to flip. The invariant doc currently documents the skills-dir `mkdir` as unconditional under an available root; that sentence becomes conditional here. Bump `pyproject.toml` in this PR (same caveat as 24B). _cli.py + skill_link.py + tests, ~70 lines._ (M)

#### Group 27: Grok row
_Depends on: Group 26_

##### Track 27A: Add Grok as a registry row
_3 tasks . ~140 LOC . medium risk . 8 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/config.py, tests/test_skill_link.py, tests/test_config.py, docs/invariants/events-retro.md, docs/designs/host-parity.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 26A_
_read-first: 25A, 26A, docs/designs/host-parity.md_
_produces: Grok gets the skill link without mm ever manufacturing Grok consent_
_session: fresh · effort: medium · attach: @src/mind_meld/skill_link.py, @src/mind_meld/config.py, @docs/designs/host-parity.md · verify: /tmp/mm-24a-probe/bin/python -m pytest tests/test_skill_link.py tests/test_config.py -q_
- **Install only where consent already exists** -- the installer's mkdir of `~/.grok/skills` flips `grok_customization_dirs_exist`, and that has two consequences with different preconditions. On a legacy or default config it auto-enables the grok sync source, which authorizes `read_grok_usage` — but only there: `get_sources` applies that filter solely while building `DEFAULT_SOURCES`, so an explicit `[[sync.sources]]` list is never appended to. On **any** config it also makes `_source_path_is_detected` label grok "detected" and default the `mm init` / `reconfigure-sources` prompt to Y. Test both shapes separately; do not assume the unconditional chain. Never create the directory. Assert the consequence, not the mechanism: after a full install, `grok_customization_dirs_exist()` is False AND `grok` is absent from `get_sources`. Requires 25A first — otherwise running `pytest` writes the developer's real `~/.grok/skills`. _skill_link.py + tests, ~70 lines._ (M)
- **One Grok home resolver, no env var** -- `GROK_HOME` stays a `host_usage` sessions-only override. A shared resolver honoring it would put an environment variable in charge of which directory `walk_grok_source` encrypts and publishes, and `conftest` deletes the variable, making that branch untestable by fixture. _config.py + tests, ~40 lines._ (S)
- **Per-row reasons and doc reconciliation** -- the "root is absent" reason is factually wrong under this gate, so reasons become per-row. Pin that the installed link yields zero manifest entries from `walk_grok_source`. Update host-parity.md's capability matrix AND its Plan C section — Plan C currently recommends `$GROK_HOME/skills` resolved at call time, which task 2 deliberately refuses. Also the invariant doc's three-targets line. Record the manual host-load check: a green unit test proves the symlink, not that Grok loads the skill. Bump `pyproject.toml` in this PR (same caveat as 24B). _docs + tests, ~30 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not a gate.

Adjacency list (from the packer):
```
- Group 24 ← {}
- Group 25 ← {24}
- Group 26 ← {25}
- Group 27 ← {26}
```

Track detail per group:
```
Group 24: Durable skill store ∥ Retro trends
  +-- Track 24A ........... ~L . 2 tasks
  +-- Track 24B ........... ~M . 2 tasks

Group 25: Target-list hygiene ∥ SKILL.md preflight
  +-- Track 25A ........... ~M . 3 tasks
  +-- Track 25B ........... ~S . 2 tasks

Group 26: Install consent
  +-- Track 26A ........... ~M . 2 tasks

Group 27: Grok row
  +-- Track 27A ........... ~M . 3 tasks
```

**Total: 4 groups . 6 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (51 items)

## Shipped

History: docs/roadmap-shipped.md
