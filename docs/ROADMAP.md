# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

---

## In Progress

_(none — Group 24 shipped in full, and Groups 25-27 have no shipped Tracks yet.)_

---

## Current Plan

### Phase 2: Durable agent skill links

**End-state:** Every agent `retro-fleet` link points at an mm-owned constant path, a wedged link is named by `mm diag` in one step, a user can decline the write, and Grok gets a row without mm manufacturing consent.
**Groups:** 24 (✓ shipped), 25, 26, 27

#### Group 25: Registry hygiene ∥ SKILL.md preflight

##### Track 25A: Make agent enumeration complete and policy-ready
_3 tasks . ~270 LOC . low risk . 11 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/conftest.py, tests/test_skill_link.py, tests/test_module_boundaries.py, AGENTS.md, docs/designs/host-parity.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_out: 26A, 27A_
_read-first: 24A, docs/invariants/events-retro.md, `~/.gstack/projects/kbitz-mind-meld/kbitz-de-landmine-target-list-autoplan-plan-20260822-102140.md` (the settled spec is its `FINAL SETTLED SHAPE` and final-gate sections)_
_produces: adding one `AgentRow` and nothing else leaves the suite green, proven for a synthetic row across descriptors, isolation, the real-home guard, diagnosis (both branches), and the CLI_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @tests/conftest.py, @tests/test_module_boundaries.py · verify: pytest tests/; ruff check .; ruff format --check ._
- **One canonical registry and a test seam that starts empty** -- `AGENT_ROWS` (public; `~`-relative str roots, never `Path`; literal marker names, no prefix rule; lowercase `key` matching `HOST_READER_SOURCE_GATE`'s vocabulary for 3 of 4 rows) plus `_TEST_SKILL_ROOT_OVERRIDES`, **empty in production**. Descriptors read `.get(row.key, row.skills_root)` — no `zip` between independently sized collections and no positional index anywhere. `_real_guard_paths()` derives from `AGENT_ROWS`, NEVER from the override map: derive the guard from the object conftest patches and its target set becomes the tmp paths, leaving it blind to all three real agent dirs with a green suite. Validation fails CLOSED at runtime (an import-time raise bricks `mm --version` / `mm status` / `mm diag`, whose docstring says it must run when everything else is broken) and asserts hard in tests: `~/`-relative, unique key / root / display name, 2N distinct marker literals. _skill_link.py, ~90 lines._ (M)
- **Registry-derived isolation and the synthetic row as the instrument** -- conftest exports `redirect_skill_paths(monkeypatch, tmp_path, *, extra_rows=())` which patches rows and overrides together so they are never observable mismatched; the autouse fixture and the synthetic test share it. `test_real_home_guard_fires` keeps LITERAL paths and its two negatives (the tripwire); its derived companion computes expectations independently rather than reusing `_home_relative`. The synthetic row lives in `test_module_boundaries.py` with a root that exists nowhere — `~/.grok` exists on this machine and the autouse fixture runs before any in-test patch — and covers all eight consumers. Migrate the nine arity-bound expectations in `test_skill_link.py`: measured, without this a new row still produces 10 red tests even with everything else done. _conftest + tests, ~150 lines._ (M)
- **Consumers and docs off the three-agent assumption** -- delete the six test-only singular adapters (~49 call sites; keep `_ensure_retro_skill_link_at` and give it `display_name` so it stops claiming "Claude Code" for an arbitrary target); count-free unavailable message at `cli.py:5701` and a replaced (not deleted) `--help` sentence; `key` on BOTH `diagnose_skill_links` dict branches, including the literal `error` branch; `MM_SKILLS_DIR` gated on `PYTEST_CURRENT_TEST` and rejected loudly outside; contributor contract in the module docstring and one clause in the `AGENTS.md` Source Layout row; `host-parity.md` Plan C repointed at `AGENT_ROWS` with the `$GROK_HOME` variant dropped. Bump `pyproject.toml` in this PR. _cli + docs, ~30 lines._ (S)

##### Track 25B: SKILL.md Step 0 and README troubleshooting
_2 tasks . ~80 LOC . low risk . 4 files_
_touches: src/mind_meld/skills/retro_fleet/SKILL.md, README.md, CHANGELOG.md, docs/PROGRESS.md_
_read-first: 24A, 24B_
_produces: SKILL.md fails closed when mm is gone; README names the store, the agent links, and the uninstall leftover_
_session: fresh · effort: medium · attach: @src/mind_meld/skills/retro_fleet/SKILL.md, @README.md · verify: pytest tests/test_skill_link.py; ruff check ._
- **SKILL.md Step 0 preflight** -- `command -v mm` and `min_mm_version` from `.mm-skill.json` at the top of SKILL.md, before any mm invocation. Step 1's "don't treat a non-zero exit as fatal" must not cover a missing binary. _SKILL.md, ~40 lines._ (S)
- **README troubleshooting and uninstall** -- symptom/cause/fix; name the store and the agent links without hardcoding a count; rewrite the no-clobber sentences at `:180` and `:281`. `README.md:313` already documents the `mm diag --json` row contents and gains the `key` field 25A adds. Bump `pyproject.toml` in this PR. _README.md, ~40 lines._ (S)

#### Group 26: Install consent
_Depends on: Group 25_

##### Track 26A: Gate the writes the way the reads are gated
_2 tasks . ~170 LOC . medium risk . 8 files_
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, src/mind_meld/skill_link.py, tests/test_config.py, tests/test_skill_link.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 25A_
_out: 27A_
_read-first: 25A, src/mind_meld/events_tail.py_
_produces: a user can decline mm writing into their agent config dirs_
_session: fresh · effort: high · attach: @src/mind_meld/config.py, @src/mind_meld/events_tail.py, @tests/test_config.py · verify: pytest tests/test_config.py tests/test_skill_link.py; ruff check ._
- **Skills auto-install opt-out** -- `HOST_READER_SOURCE_GATE` will not `stat` `~/.codex/sessions` without consent, but mm writes a symlink into `~/.codex/skills/` with none. Add the config key plus per-agent toggles, defaulted to today's behavior, and report "auto-install: disabled" rather than calling it degradation. _config.py + tests, ~90 lines._ (M)
- **Pass consent into the installer** -- a `may_create` frozenset derived from `get_sources(config)`, keyed on 25A's `AgentRow.key`. Add the `consent_source` field to the row here, where it has a policy to attach — 25A's `/autoplan` gate cut it precisely because it equalled `key` with no consumer. Note `init` calls the installer BEFORE it resolves sources; that ordering has to flip. The invariant doc currently documents the skills-dir `mkdir` as unconditional under an available root; that sentence becomes conditional here. Bump `pyproject.toml` in this PR. _cli.py + skill_link.py + tests, ~80 lines._ (M)

#### Group 27: Grok row
_Depends on: Group 26_

##### Track 27A: Add Grok as a registry row
_3 tasks . ~140 LOC . medium risk . 8 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/config.py, tests/test_skill_link.py, tests/test_config.py, docs/invariants/events-retro.md, docs/designs/host-parity.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 26A_
_read-first: 25A, 26A, docs/designs/host-parity.md_
_produces: Grok gets the skill link without mm ever manufacturing Grok consent_
_session: fresh · effort: medium · attach: @src/mind_meld/skill_link.py, @src/mind_meld/config.py, @docs/designs/host-parity.md · verify: pytest tests/test_skill_link.py tests/test_config.py; ruff check ._
- **Install only where consent already exists** -- the installer's mkdir of `~/.grok/skills` flips `grok_customization_dirs_exist` (`skills` is in `manifest.GROK_SYNCED_SUBDIRS`), and that has two consequences with different preconditions. On a legacy or default config it auto-enables the grok sync source, which authorizes `read_grok_usage` — but only there: `get_sources` applies that filter solely while building `DEFAULT_SOURCES`, so an explicit `[[sync.sources]]` list is never appended to. On **any** config it also makes `_source_path_is_detected` label grok "detected" and default the `mm init` / `reconfigure-sources` prompt to Y. Test both shapes separately; do not assume the unconditional chain. Never create the directory. Assert the consequence, not the mechanism: after a full install, `grok_customization_dirs_exist()` is False AND `grok` is absent from `get_sources`. _skill_link.py + tests, ~70 lines._ (M)
- **One Grok home resolver, no env var** -- `GROK_HOME` stays a `host_usage` sessions-only override. A shared resolver honoring it would put an environment variable in charge of which directory `walk_grok_source` encrypts and publishes, and `conftest` deletes the variable, making that branch untestable by fixture. 25A already dropped the `$GROK_HOME/skills` variant from `host-parity.md` Plan C for the same reason: the real-home guard cannot express a runtime env root. _config.py + tests, ~40 lines._ (S)
- **Per-row reasons and doc reconciliation** -- the "root is absent" reason is factually wrong under this gate, so reasons become per-row. Pin that the installed link yields zero manifest entries from `walk_grok_source` (belt-and-braces: `manifest.py:530` already skips every symlink). Update host-parity.md's capability matrix. Record the manual host-load check: a green unit test proves the symlink, not that Grok loads the skill. Bump `pyproject.toml` in this PR. _docs + tests, ~30 lines._ (S)

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not a gate.

Adjacency list (from the packer):
```
- Group 25 ← {}
- Group 26 ← {25}
- Group 27 ← {26}
```

Track detail per group:
```
Group 25: Registry hygiene ∥ SKILL.md preflight
  +-- Track 25A ........... ~M+M+S . 3 tasks
  +-- Track 25B ........... ~S+S . 2 tasks

Group 26: Install consent
  +-- Track 26A ........... ~M+M . 2 tasks

Group 27: Grok row
  +-- Track 27A ........... ~M+S+S . 3 tasks
```

**Total: 3 groups . 4 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (54 items)

## Shipped

History: docs/roadmap-shipped.md
