# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

---

## In Progress

### Phase 2: Durable agent skill links

**End-state:** Every agent `retro-fleet` link points at an mm-owned constant path, a wedged link is named by `mm diag` in one step, a user can decline the write *and* remove it again, and Grok gets a row without mm manufacturing consent.
**Groups:** 24 (✓ shipped), 25, 26, 27

#### Group 25: Registry hygiene ∥ SKILL.md preflight ∥ Install consent

##### Track 25A: Make agent enumeration complete and policy-ready ✓ Shipped (v0.12.40)

##### Track 25B: SKILL.md Step 0 and README troubleshooting
_2 tasks . ~110 LOC . low risk . 6 files_
_touches: src/mind_meld/skills/retro_fleet/SKILL.md, README.md, tests/test_docs_routing.py, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 24A, 24B, 25A_
_produces: `/retro-fleet` refuses before Step 1 when `mm` is missing or broken; README carries the missing-block symptom Step 0 cannot reach_
_session: fresh · effort: medium · attach: @src/mind_meld/skills/retro_fleet/SKILL.md, @README.md · verify: pytest tests/test_docs_routing.py tests/test_skill_link.py; ruff check ._
- **SKILL.md Step 0 preflight** -- one terminal rule (only stage 0A stops the run), a standalone `command -v mm` gate that also stops on a resolvable-but-broken binary, and a stage that relays the upgrade notice `upgrade.emit_nudge_if_due` already prints at the tail of interactive `mm push`. Step 1's skip clause is re-scoped to name Step 1, and the two hardcoded `v0.12.37` literals come out. **No version comparison**: `min_mm_version` is written by the same binary whose freshness is in question, cannot know which SKILL.md the agent loaded (`live-checkout` is a supported status), and two matching stale numbers read as verification. _SKILL.md, ~50 lines._ (S)
- **README troubleshooting and uninstall** -- lead `## Troubleshooting` with the real symptom ("retro output is missing a block, unexpectedly empty, or older than expected"), rewrite `:315` (its symptom becomes wrong once Step 0 refuses up front), correct `:313`'s claim about what `mm diag --json` proves, add the `key` field, de-count `:131` / `:181` / `:282`, and append the agent-restart clause to every `mm install-skills` remedy. _README.md + tests, ~60 lines._ (S)

##### Track 25C: Gate the writes the way the reads are gated
_2 tasks . ~170 LOC . medium risk . 8 files_
_touches: src/mind_meld/config.py, src/mind_meld/cli.py, src/mind_meld/skill_link.py, tests/test_config.py, tests/test_skill_link.py, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md_
_out: 26A, 27A_
_read-first: 25A, src/mind_meld/events_tail.py_
_produces: a user can decline mm writing into their agent config dirs_
_session: fresh · effort: high · attach: @src/mind_meld/config.py, @src/mind_meld/events_tail.py, @tests/test_config.py · verify: pytest tests/test_config.py tests/test_skill_link.py; ruff check ._
- **Skills auto-install opt-out** -- `HOST_READER_SOURCE_GATE` will not `stat` `~/.codex/sessions` without consent, but mm writes a symlink into `~/.codex/skills/` with none. Add the config key plus per-agent toggles, defaulted to today's behavior, and report "auto-install: disabled" rather than calling it degradation. _config.py + tests, ~90 lines._ (M)
- **Pass consent into the installer** -- a `may_create` frozenset derived from `get_sources(config)`, keyed on 25A's `AgentRow.key`. Add the `consent_source` field to the row here, where it has a policy to attach. Note `init` calls the installer BEFORE it resolves sources; that ordering has to flip. The invariant doc currently documents the skills-dir `mkdir` as unconditional under an available root; that sentence becomes conditional here. _cli.py + skill_link.py + tests, ~80 lines._ (M)

---

## Current Plan

#### Group 26: Symmetric uninstall

_Depends on: Group 25_

##### Track 26A: Give the installer an inverse
_3 tasks . ~200 LOC . medium risk . 8 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/test_skill_link.py, README.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_blocked-by: Track 25C_
_out: 27A_
_read-first: 25C, 25A, docs/invariants/events-retro.md_
_produces: a user can remove `/retro-fleet` and have it stay removed_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @src/mind_meld/cli.py, @tests/test_skill_link.py · verify: pytest tests/test_skill_link.py; ruff check ._
- **`mm uninstall-skills`, registry-driven** -- `mm install-skills` has no inverse, so removal falls back to a hardcoded shell loop in the README over paths `AGENT_ROWS` already owns. Iterate the registry, apply the same ownership rule the installer follows (only unlink what `readlink` proves is mm's store link; never touch a file of the user's), and report per-row outcomes. _skill_link.py + cli.py, ~90 lines._ (M)
- **Removal has to stick** -- the installer's `absent target -> symlink -> installed` branch re-creates a manually deleted link on the next interactive push, so today there is no supported way to decline the skill short of leaving a foreign file at the path. Persist the decision through 26A's consent key rather than inventing a second opt-out. A command that only unlinks would be reinstalled by the next push and is worse than none. _skill_link.py + tests, ~70 lines._ (M)
- **README off the hardcoded path loop** -- `README.md:339` enumerates three link paths in a copy-pasteable snippet; a fourth row orphans a link silently. Replace with `mm uninstall-skills` run before `pipx uninstall`, which is the only ordering where the registry is still available. _README.md + docs, ~40 lines._ (S)

#### Group 27: Grok row

_Depends on: Group 26_

##### Track 27A: Add Grok as a registry row
_3 tasks . ~160 LOC . medium risk . 9 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/config.py, tests/test_skill_link.py, tests/test_config.py, README.md, docs/invariants/events-retro.md, docs/designs/host-parity.md, CHANGELOG.md, docs/PROGRESS.md_
_blocked-by: Track 26A_
_read-first: 25A, 25C, 26A, docs/designs/host-parity.md_
_produces: Grok gets the skill link without mm ever manufacturing Grok consent, and removing it works the same way every other row does_
_session: fresh · effort: medium · attach: @src/mind_meld/skill_link.py, @src/mind_meld/config.py, @docs/designs/host-parity.md · verify: pytest tests/test_skill_link.py tests/test_config.py; ruff check ._
- **Install only where consent already exists** -- the installer's mkdir of `~/.grok/skills` flips `grok_customization_dirs_exist` (`skills` is in `manifest.GROK_SYNCED_SUBDIRS`), and that has two consequences with different preconditions. On a legacy or default config it auto-enables the grok sync source, which authorizes `read_grok_usage` — but only there: `get_sources` applies that filter solely while building `DEFAULT_SOURCES`, so an explicit `[[sync.sources]]` list is never appended to. On **any** config it also makes `_source_path_is_detected` label grok "detected" and default the `mm init` / `reconfigure-sources` prompt to Y. Test both shapes separately; do not assume the unconditional chain. Never create the directory. Assert the consequence, not the mechanism: after a full install, `grok_customization_dirs_exist()` is False AND `grok` is absent from `get_sources`. _skill_link.py + tests, ~70 lines._ (M)
- **One Grok home resolver, no env var** -- `GROK_HOME` stays a `host_usage` sessions-only override. A shared resolver honoring it would put an environment variable in charge of which directory `walk_grok_source` encrypts and publishes, and `conftest` deletes the variable, making that branch untestable by fixture. 25A already dropped the `$GROK_HOME/skills` variant from `host-parity.md` Plan C for the same reason: the real-home guard cannot express a runtime env root. _config.py + tests, ~40 lines._ (S)
- **Per-row reasons and doc reconciliation** -- the "root is absent" reason is factually wrong under this gate, so reasons become per-row. Pin that the installed link yields zero manifest entries from `walk_grok_source` (belt-and-braces: `manifest.py:530` already skips every symlink). Update `host-parity.md`'s capability matrix and the README's agent-list prose. Record the manual host-load check: a green unit test proves the symlink, not that Grok loads the skill. _docs + tests, ~50 lines._ (S)

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
Group 25: Registry hygiene ∥ SKILL.md preflight ∥ Install consent  (in progress)
  +-- Track 25A ........... ✓ shipped (v0.12.40)
  +-- Track 25B ........... ~S+S . 2 tasks
  +-- Track 25C ........... ~M+M . 2 tasks

Group 26: Symmetric uninstall
  +-- Track 26A ........... ~M+M+S . 3 tasks

Group 27: Grok row
  +-- Track 27A ........... ~M+S+S . 3 tasks
```

**Total: 3 groups . 4 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (58 items)

## Shipped

History: docs/roadmap-shipped.md
