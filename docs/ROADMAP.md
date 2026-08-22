<!-- /autoplan restore point: /Users/kb/.gstack/projects/kbitz-mind-meld/kbitz-extract-event-capture-autoplan-restore-20260815-143545.md -->

# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

---

## In Progress

### Phase 2: Durable agent skill links

**End-state:** Every agent `retro-fleet` link points at an mm-owned constant path, a wedged link is named by `mm diag` in one step, a user can decline the write, and Grok gets a row without mm manufacturing consent.
**Groups:** 24, 25, 26, 27

#### Group 24: Durable skill store ∥ Deterministic retro trends

##### Track 24A: Own the skill store ✓ Shipped (v0.12.38)
_2 tasks . ~250 LOC . high risk . 10 files_

Published the mm-owned `retro-fleet` skill store, migrated live links safely, and made broken links diagnosable without touching an ephemeral checkout.

##### Track 24B: Compute deterministic prior-period retro trends (PR #138)
_1 task . ~600 LOC net . high risk . 17 files_
_touches: AGENTS.md, CHANGELOG.md, README.md, SPEC.md, docs/PROGRESS.md, docs/TODOS.md, docs/invariants/events-retro.md, pyproject.toml, src/mind_meld/cli.py, src/mind_meld/retention.py, src/mind_meld/skills/retro_fleet/SKILL.md, src/mind_meld/skills/retro_fleet/aggregator.py, tests/conftest.py, tests/test_docs_routing.py, tests/test_retention.py, tests/test_retro_fleet_aggregator.py, tests/test_retro_fleet_cli.py_
_out: 26A_
_read-first: docs/invariants/events-retro.md, src/mind_meld/skills/retro_fleet/aggregator.py_
_produces: `## Trends vs prior <N>d`, a fleet-deterministic prior/current table derived from the synced event corpus rather than local command history_
_session: PR #138 · verify: pytest tests/; ruff check .; ruff format --check ._
- **Replace machine-local snapshot trends with a prior equal-period comparison** -- delete the snapshot subsystem, use one in-memory event pass for both periods, keep `--no-save` as a hidden compatible no-op, reap safe snapshot leftovers, and update every user-facing and invariant surface. UTC day keys, eligible-copy deduplication, coverage proof, and unavailable states make the output truthful across the fleet. _aggregator.py + CLI + docs + tests, ~600 net lines._ (L)

---

## Current Plan

#### Group 25: Target-list hygiene
_Depends on: Group 24_

##### Track 25A: De-landmine the target list
_3 tasks . ~220 LOC . medium risk . 9 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/conftest.py, tests/test_skill_link.py, tests/test_module_boundaries.py, AGENTS.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 24A_
_out: 26B_
_read-first: 24A, tests/conftest.py_
_produces: a new agent row cannot escape test isolation or the real-home guard_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @tests/conftest.py, @tests/test_module_boundaries.py · verify: /tmp/mm-24a-probe/bin/python -m pytest tests/test_skill_link.py tests/test_module_boundaries.py -q_
- **Derive descriptors from one registry** -- keep a single patchable `name` to tilde-string mapping as the seam: `conftest._isolate_skill_links` redirects all roots with ONE `setattr` on `SKILL_ROOTS` and `test_module_boundaries` asserts that name, so a tuple of resolver callables would destroy the seam. Kill positional indexing and the fixed-arity annotation. _skill_link.py + cli.py, ~110 lines._ (M)
- **Registry-derived test isolation** -- conftest roots and `_is_real_agent_dir_under_pytest` paths both derive from the registry, pinned by a synthetic extra row that must be covered with zero other edits. The guard list carries a fifth store entry (24A) that is NOT a skills root; keep it as an explicit extra alongside the marker-dir extra. _conftest.py + tests, ~80 lines._ (M)
- **Pin marker literals and prefix uniqueness** -- on-disk names are not a uniform prefix (`.skill-link-checked` has none), so the Claude row carries an empty prefix and the test asserts the literal strings. Assert prefix uniqueness so one agent's marker cannot authorize another's target. Keep `_ensure_retro_skill_link_at`. Bump `pyproject.toml` in this PR (same VERSION-as-pyproject caveat as 24B). _tests + docs, ~30 lines._ (S)

#### Group 26: SKILL.md preflight ∥ Install consent
_Depends on: Group 24, Group 25_

##### Track 26A: SKILL.md Step 0 and README troubleshooting
_2 tasks . ~80 LOC . low risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/SKILL.md, README.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 24B_
_read-first: 24A, 24B_
_produces: SKILL.md fails closed when mm is gone; README names the store, the three links, and the uninstall leftover_
_session: fresh · effort: medium · attach: @src/mind_meld/skills/retro_fleet/SKILL.md, @README.md · verify: /tmp/mm-24a-probe/bin/python -m pytest tests/test_skill_link.py -q_
- **SKILL.md Step 0 preflight** -- `command -v mm` and `min_mm_version` from `.mm-skill.json` at the top of SKILL.md, before any mm invocation. Step 1's "don't treat a non-zero exit as fatal" must not cover a missing binary. _SKILL.md, ~40 lines._ (S)
- **README troubleshooting and uninstall** -- symptom/cause/fix; name the three links and the store; rewrite the no-clobber sentences at `:180` and `:281`. Bump `pyproject.toml` in this PR (same VERSION-as-pyproject caveat as 24B). _README.md, ~40 lines._ (S)

##### Track 26B: Gate the writes the way the reads are gated
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
_blocked-by: Track 26B_
_read-first: 25A, 26B, docs/designs/host-parity.md_
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
- Group 26 ← {24, 25}
- Group 27 ← {26}
```

Track detail per group:
```
Group 24: Durable skill store ∥ Retro trends
  +-- Track 24A ........... ✓ shipped v0.12.38
  +-- Track 24B ........... PR #138 · ~L . 1 task

Group 25: Target-list hygiene
  +-- Track 25A ........... ~M . 3 tasks

Group 26: SKILL.md preflight ∥ Install consent
  +-- Track 26A ........... ~S . 2 tasks
  +-- Track 26B ........... ~M . 2 tasks

Group 27: Grok row
  +-- Track 27A ........... ~M . 3 tasks
```

**Total: 3 groups . 4 tracks queued after the in-flight PR.**

---

## Future

Deferred: docs/roadmap-future.md (53 items)

## Shipped

History: docs/roadmap-shipped.md
