# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Six Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`; two Tracks claiming one version force-advance `latest` to an untagged commit. See that file for the full argument.

---

## In Progress

### Phase 2: Durable agent skill links

**End-state:** Every agent `retro-fleet` link points at an mm-owned constant path, a wedged link is named by `mm diag` in one step, a user can decline the write *and* remove it again, and Grok's access to the skill is a verified `mm diag` fact rather than a fourth link mm maintains.
**Groups:** 24 (✓ shipped), 25 (✓ shipped), 26 (✓ shipped), 28

_The fourth clause originally read "and Grok gets a row without mm manufacturing consent". v0.12.43 established the opposite — Grok discovers `~/.claude/skills` natively, so the row is refused by the exit criterion above. Restated rather than dropped: the end-state it names is met, by other means. Three of four clauses are shipped; symmetric uninstall (Track 28A) is the remaining one, and its Group has no shipped Tracks yet, so it sits under Current Plan below._

---

## Current Plan

_tombstone: 27_

#### Group 28: Symmetric uninstall ∥ Roadmap staleness gate

##### Track 28A: Give the installer an inverse
_3 tasks . ~200 LOC . medium risk . 8 files_
_touches: src/mind_meld/skill_link.py, src/mind_meld/cli.py, tests/test_skill_link.py, README.md, docs/invariants/events-retro.md, CHANGELOG.md, docs/PROGRESS.md, pyproject.toml_
_read-first: 25C, docs/invariants/events-retro.md_
_produces: a user can remove `/retro-fleet` and have it stay removed without hand-editing `config.toml`_
_session: fresh · effort: high · attach: @src/mind_meld/skill_link.py, @src/mind_meld/cli.py, @tests/test_skill_link.py · verify: pytest tests/test_skill_link.py tests/test_config.py; ruff check ._
- **`mm uninstall-skills`, registry-driven** -- verified 2026-08-25: `uninstall_skills` / `uninstall-skills` has zero hits in `src/` and `tests/`; the only hit in the repo is `README.md:271`, which forward-references it as unshipped ("edit that list by hand until `mm uninstall-skills` ships"). Iterate `AGENT_ROWS`, apply the installer's own ownership rule — only unlink what `readlink` proves points at the mm-owned store, never touch a file of the user's — and report per-row outcomes. Mirror `mm install-skills --agent KEY`, which already exists and already persists a grant. _skill_link.py + cli.py, ~90 lines._ (M)
- **Removal has to stick** -- **premise corrected 2026-08-25.** This task previously claimed "there is no supported way to decline the skill short of leaving a foreign file at the path." False since v0.12.42: `[skills] maintain_links` and `[skills] agents` exist and `skill_link.consented_agent_keys` is the one derivation, so declining is supported — by hand-editing TOML. The real remaining gap is narrower, and is the one `README.md:271` names: no command writes that key, so a plain unlink is undone by the next interactive push, because the installer's absent-target → symlink branch still runs for a consented row. Reuse the persistence path `install-skills --agent` already has rather than inventing a second opt-out. A command that only unlinks is worse than none. _skill_link.py + cli.py + tests, ~70 lines._ (M)
- **README off the hardcoded path loop** -- **anchor corrected twice on 2026-08-25: `:339` → `:375` → `:381`.** The copy-pasteable three-path loop is now `README.md:381` (`for l in ~/.claude/skills/retro-fleet ~/.codex/skills/retro-fleet ~/.config/opencode/skills/retro-fleet; do`); v0.12.42 and v0.12.43 moved it to `:375`, then the same-day skill-link docs commit added a Troubleshooting entry above it and moved it again. **Grep the loop body, not the line** — this anchor has now drifted three times in four days and will drift again before the Track runs. Replace with `mm uninstall-skills` run *before* `pipx uninstall` — the only ordering where the registry still exists, which `README.md:379-380` states in prose ("there is no `mm` left after `pipx uninstall` to ask"). `README.md:271`'s forward reference stops being true in this PR and must be updated in it; note rule 6 grew a "**Unmaintained is not dead:**" body on 2026-08-25, so that clause is now embedded in longer prose about what declining costs, and the rewrite has to keep the surrounding argument coherent rather than just deleting the trailing "until ... ships". _README.md + docs, ~40 lines._ (S)

##### Track 28B: Stop the roadmap going stale between Tracks
_2 tasks . ~120 LOC . low risk . 4 files_
_touches: tests/test_docs_routing.py, AGENTS.md, docs/ROADMAP.md, docs/PROGRESS.md_
_read-first: AGENTS.md:104, tests/test_docs_routing.py:256_
_produces: a PR that ships a Track cannot leave ROADMAP.md describing that Track as unshipped_
_session: fresh · effort: medium · attach: @tests/test_docs_routing.py, @AGENTS.md · verify: pytest tests/test_docs_routing.py; ruff check ._
- **Roadmap-staleness gate** -- sixth occurrence. The Future bullet this promotes named four (23B, 24B, 25A, 25B); the 2026-08-25 regen found two more, and they are the worst kind: Tracks 25B and 25C shipped as v0.12.41 and v0.12.42 and were still listed unshipped at HEAD, and the then-26A's premise had rotted *because* 25C landed. Model on `tests/test_docs_routing.py:256` `test_every_changelog_version_has_a_progress_row` — same problem, already solved once: a convention line alone failed twice (v0.11.24, v0.11.27), a tree-only pytest in the PR fixed it. Concrete candidate, feasibility-checked 2026-08-25: a **release drift budget** — parse `✓ Shipped (vX.Y.Z)` and `(vX–vY)` markers from `docs/ROADMAP.md` + `docs/roadmap-shipped.md`, parse `## [X.Y.Z]` from `CHANGELOG.md` (the precedent test already does exactly this), fail when the newest CHANGELOG release runs more than N releases ahead of the newest marker. Drift on 2026-08-25 was 3 (newest marker `v0.12.40`, CHANGELOG `0.12.43`), so the gate would have failed the v0.12.42 PR. Fallback if a tree-only invariant proves too blunt: a CI diff gate requiring `docs/ROADMAP.md` in any PR that bumps `pyproject.toml`. Pick one — a tree-only pytest cannot see PR intent, which is why the drift budget measures the symptom rather than the act. _tests/test_docs_routing.py, ~80 lines._ (M)
- **Convention line beside the PROGRESS-row convention** -- `AGENTS.md:104` is the anchor and has exactly this shape ("the row goes in the SAME PR"). Add the roadmap-marking convention beside it and have it name the new gate the way `:104` names its own. `CLAUDE.md` is a symlink to `AGENTS.md` — declare only `AGENTS.md`. _AGENTS.md, ~40 lines._ (S)

_Track 28B rooms in this Group by packer bin — no `_touches:` overlap with 28A outside shared infra — not by theme._

### Execution Map

A Group may launch when every Group in its ← set has landed, regardless
of document order; document order is priority, not a gate.

Adjacency list (from the packer):
```
- Group 28 ← {}
```

Track detail per group:
```
Group 28: Symmetric uninstall ∥ Roadmap staleness gate
  +-- Track 28A ........... ~M+M+S . 3 tasks
  +-- Track 28B ........... ~M+S . 2 tasks
```

**Total: 1 group . 2 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (57 items)

## Shipped

History: docs/roadmap-shipped.md
