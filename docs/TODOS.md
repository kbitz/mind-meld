# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Item format (load-bearing — `/roadmap` cannot see items written any other way)

Every item is an **H3 heading** carrying a bracketed source tag, optionally
followed by attribute bullets and free-form prose:

```markdown
### [full-review:severity=critical] Short title, not a paragraph
- **Description:** reviewer's framing of the issue (full-review, review)
- **Symptom:** what was observed (pair-review, investigate)
- **Repro:** numbered steps to re-verify before fixing
- **Why:** problem statement (manual, ship — you authored it and stand behind it)
- **Hypothesis (untested):** a direction to investigate, not a fix to apply
- **Effort:** S | M | L
- **Priority:** P1 | P2 | P3
- **Context:** provenance, branch, prior decisions

Free-form prose, measurements, snippets — preserved verbatim, not parsed.
```

`<source>` is one of `pair-review`, `full-review`, `review`, `review-apparatus`,
`test-plan`, `investigate`, `ship`, `manual`, `discovered`, `plan-ceo-review`,
`plan-eng-review`. Keys are lowercase `[a-z-]+`; values may not contain `[`, `]`,
`,` or `;` (pipe-separate file lists: `files=a.py|b.py`). A missing tag routes as
`[manual]`. Full grammar: `gstack-extend/docs/source-tag-contract.md`.

**Why this is load-bearing.** `bin/roadmap-audit` counts items by matching `###`
headings inside `## Unprocessed`. A flat `- **Bold title.** …` bullet is
invisible to it — the section reports `ITEMS: 0` and `/roadmap` skips the drain
entirely. That is not hypothetical: **38 items filed as bullets between
2026-08-30 and 2026-09-01 were reported as `ITEMS: 0` for three days** and were
only drained because a human noticed the roadmap looked stale. If you append
here by hand, use the H3 form.

## Unprocessed

### [manual] Track 36B card amendments (post-autoplan; already implemented on this branch)
- **Description:** 36B shipped as delete-the-name + tolerant acceptor, not the carded pin. Next `/roadmap` regen must rewrite the 36B card (and 36A's `read-first`) to match. Do not hand-edit `docs/ROADMAP.md`.
- **Why:** The autoplan review rejected the pin. The implementation is on `kbitz/pin-opencode-wire-name`. The live card still describes the rejected mechanism.
- **Effort:** S
- **Priority:** P2
- **Context:** `/autoplan` 2026-09-01. Settled amendments: `read-first:` inverts (36B before 36A — `release.yml` force-pushes `latest` on 36A's version bump); `verify:` becomes `pytest tests/`; `touches:` drops `CLAUDE.md` (symlink) and adds `aggregator.py`, `tests/test_retro_fleet_aggregator.py`, `src/mind_meld/skills/retro_fleet/SKILL.md`; mechanism is delete `"opencode"` from `HOST_USAGE_TOKEN_SOURCES` and retain unknown names in `_token_sources_subsequence`; file count 8 → 7; observed live shape is `["codex","opencode"]`, not `["codex","grok","opencode"]`. The two cheap doc-integrity tests named in the same review (CLAUDE.md symlink, README uninstall loop) are already in `tests/test_docs_routing.py`.

### [plan-eng-review:severity=high] Nine files hold OpenCode references no Track owns
- **Description:** A full repo grep at `620ae1e` finds 336 OpenCode references across 34 files. Tracks 36A, 36B and 37B between them own 25 of those files. Nine are unowned by any Track: `tests/test_module_boundaries.py` (10), `tests/fixtures/host_sessions/opencode/` (6 + a `legacy/` tree), `tests/test_init_events_backfill.py` (4), `src/mind_meld/skills/retro_fleet/aggregator.py` prose (4), `tests/conftest.py` (3), `src/mind_meld/skills/retro_fleet/SKILL.md` (3), `src/mind_meld/manifest.py` (3), `docs/designs/grok-build-usage-reader.md` (3), `tests/test_token_usage.py` (1).
- **Why:** Two are load-bearing, not cosmetic. `tests/test_module_boundaries.py:693` asserts `keys[:3] == ["claude","codex","opencode"]` and `:747` asserts a user-visible string; both break under **Track 37B**, whose `verify:` line does not include that file. `tests/fixtures/host_sessions/opencode/` is a whole fixture tree 36A's card gestures at ("keep any fixture a legacy-peer tolerance test in 36B will need") without owning.
- **Effort:** S
- **Priority:** P2
- **Context:** `/autoplan` Phase 1 CEO expansion E2, 2026-09-01, branch `kbitz/pin-opencode-wire-name`. 36B deliberately did not absorb these — pulling them in creates `_touches:` collisions with 36A and 37B inside a Group declared parallel.

### [manual:severity=high] Track 37B needs a retired-source CLI story, not just "must not resurrect"
- **Description:** 37B's card requires only that `mm enable-source opencode` "must not resurrect a retired source". Both DX voices found that leaves two user-facing behaviours, and both are wrong. (a) With an explicit `[[sync.sources]]` entry — the documented state, since `mm disable-source` preserves the block by design (`cli.py:5751-5753`) — `enable-source` prints `Enabled source 'opencode' on this device.` and 37B's load-boundary injection silently puts it straight back. Success message, permanent no-op. (b) Without one, `_validate_source_name` (`cli.py:5699-5703`) describes a **retired** source as **"not yet known to mm (forward-compat for not-yet-shipped sources)"** and offers `--force`, which teaches the user how to bypass the retirement.
- **Why:** `mm sources` will honestly show the row, and its own docstring routes the reader straight into the broken verb. Error-message actionability scored 2/10, the lowest score in the review, with both voices agreeing.
- **Hypothesis (untested):** a retired-names branch at `cli.py:5688`, before the `name in valid` return, with `--force` explicitly unable to bypass a retired built-in. Codex's shape is the better one: `enable` refuses, `mm sources` shows an explicit `Retired` state, and **`disable` stays idempotently accepted** so cleanup scripts do not start failing.
- **Effort:** S
- **Priority:** P2
- **Context:** `/autoplan` Phase 2.5 DX, 2026-09-01. Also file against 37B: add `tests/test_module_boundaries.py` to its `verify:`, and take `pyproject.toml:13`'s stale `opencode` keyword (37B already touches that file; 36B must not, or it enters the release-serialization lane).

### [manual:severity=medium] No deprecation notice anywhere when a supported agent is retired
- **Description:** A developer upgrades mm and sees nothing. No release note, no `mm: notice:`, no `mm status` or `mm diag` line, even though `mm sources` flips a row and a previously working source stops syncing. Upgrade-path safety scored 3/10 and 4/10 across the two DX voices.
- **Why:** The repo already has the pattern — `mm: notice:` lines and the `autopush` `no-sources` breadcrumb precedent, which exists for exactly this class of silent no-op. One line closes it.
- **Hypothesis (untested):** a one-time notice when a retired source or an mm-owned orphaned skill link is detected. Belongs in Track 37B, which is the Track that creates retirement.
- **Effort:** S
- **Priority:** P3
- **Context:** `/autoplan` Phase 2.5 DX, 2026-09-01.

### [plan-eng-review:severity=medium] Decide whether unknown-enum tolerance is a general wire posture or a one-off
- **Description:** Conditional on the Track 36B decision. If the acceptor is made tolerant of unknown `token_sources` names, the same question applies to the other closed vocabularies on the host-usage wire: host **families** (`_accept_hosts_payload` rejects unknown families as `invalid_counter`), reader **reasons** (`_HOST_READ_REASONS`), and **model ids**. They should not all move together by default — rejecting an unknown host family is correct, because an unattributable counter is not a measurement. Write down which vocabularies are open and which are closed, and why, rather than leaving it to the next retirement.
- **Why:** Standing constraint: "a Track that puts a field on a wire must name its reader." The inverse deserves the same treatment — a Track that loosens a validator should say how far the loosening goes.
- **Effort:** M
- **Priority:** P3
- **Context:** `/autoplan` Phase 1 CEO "NOT in scope", 2026-09-01. Only file this if the Track 36B User Challenge resolves toward acceptor tolerance.

_Drained 2026-09-01 by `/roadmap`; the five items above were filed after that drain by `/autoplan` on branch `kbitz/pin-opencode-wire-name`._

### [plan-eng-review:severity=critical] `release.yml` force-pushes `latest` to untagged commits on ANY pyproject edit
- **Description:** `release.yml`'s final step, "Advance latest branch to released commit" (`:120-139`), has **no `if:` guard at all**. The workflow triggers on `paths: [pyproject.toml, CHANGELOG.md]`. With no version bump: `Read version` succeeds (`0.12.53`), `Check tag + release existence` returns `tag_exists=true` / `release_exists=true`, `Extract CHANGELOG body` still succeeds because the `## [0.12.53]` section is already present, the tag and Release steps skip via their existence checks, and then `git push origin "+HEAD:refs/heads/latest"` runs unconditionally.
- **Why:** `latest` is the ref `pipx install …@latest` and the auto-upgrade nudge track — `upgrade.py:66` prints that exact command to users. So a dev-dependency-only edit publishes an untagged commit that self-reports an already-released version, and `cli.py:_check_fleet_version_or_refuse` classifies peers by the version **string**, so it cannot see the divergence. The step's own comment claims "`latest` only ever points at released commits" — true only by accident of every prior pyproject edit being a version bump. `docs/shared-infra.txt:8-13` documents the mechanism but frames it as a two-Tracks-one-version problem, not a dev-dep problem.
- **Repro:** read `.github/workflows/release.yml:22-27` and `:120-139`, then trace each step's guard. Confirmed `origin/latest` == `f7aa935` == tag `v0.12.53` today.
- **Hypothesis (untested):** guard the step on the tag resolving to HEAD — a shell comparison of `git rev-parse "$tag^{commit}"` against `git rev-parse HEAD`, evaluated **after** the tag-creation step. An `if: steps.check.outputs.tag_exists == 'true'` inverts it and would skip latest-advance on every genuine first release. `^{commit}` is required because `:106 git tag "$tag"` creates lightweight tags. Emit a `::warning::` on skip, or the same-version-twice case becomes a silent non-advance. Also guard `:78` Extract CHANGELOG, which is now the only unguarded step that can turn main's Release run red on a non-release trigger. Fixing this obsoletes `shared-infra.txt`'s stated rationale and the `ROADMAP.md` standing constraint that cites it — serialization is still correct, but for a different reason, so correct both in the same wave.
- **Effort:** S
- **Priority:** P1
- **Context:** `/autoplan` Phase 2.5 DX finding X3, 2026-09-01, branch `kbitz/fresh-workspace-verify`. Live defect in `main`, not introduced by any planned Track — but it blocks the pytest-xdist item below, which is the first non-release-bearing change that would touch `pyproject.toml`.

### [plan-eng-review:severity=critical] Three tests pass or fail on the LENGTH of the temp directory path
- **Description:** `tests/test_init_auto_pin.py::TestAutoPinStorageForIcloud`'s three `*_falls_back_to_finder_notice` cases assert `"Keep Downloaded" in out` against Rich-wrapped output. Rich folds **between the two words** at certain rendered line lengths, and the rendered length is a function of terminal width AND `tmp_path` length.
- **Why:** These are green today only because the default `--basetemp` happens to land outside a fold window. A GitHub macOS runner with a different TMPDIR could turn the suite red on a commit that changed nothing. The test's own comment at `:108-110` already diagnoses it — *"Rich wraps long paths at arbitrary characters in non-TTY capture mode; the meaningful UX signal is that the Finder tip surfaces, not the exact path rendering"* — and then asserts on a splittable phrase anyway.
- **Repro:** measured 2026-09-01 in `.venv` (3.13.15). Serial, no xdist, `--basetemp=/tmp/mmb2-<72 chars of padding>` → **3 failed, 3 passed**. Same, `<52 chars>` → 6 passed. Under `-n auto` with no COLUMNS → 3 failed. Under `COLUMNS=200 -n auto` with default basetemp → 3124 passed, but with `--basetemp=/tmp/mmb-<40 chars>` → **3 failed again**.
- **Hypothesis (untested):** fix the assertions, not the environment. Either normalize whitespace before asserting (`" ".join(out.split())`) or use this repo's existing house pattern for width-sensitive assertions — an explicit `Console(file=buf, force_terminal=False, width=200)`, as at `tests/test_safe_str.py:75,86,101` and `tests/test_conflictdiff.py:230`. **Do NOT pin `COLUMNS`**: it does not fix the defect (proven above), matches no existing pattern in this repo, changes rendering for all 3124 tests, and only applies when the invoker sets it — so it fails for anyone running `pytest -k` directly or from an IDE. Add a hostile-path-length regression case with the fix.
- **Effort:** S
- **Priority:** P1
- **Context:** `/autoplan` Phase 3 eng, 2026-09-01. Surfaced while evaluating pytest-xdist; the eng voice overturned the review's own proposed `COLUMNS` fix and re-probing confirmed the defect is independent of xdist. Live in `main`.

### [manual:severity=high] Track 37A should be reshaped and split: one verification command, not a venv bootstrap
- **Description:** The carded Track (a bootstrap script + a `.conductor/` entry + rewriting 10 roadmap cards with `./.venv/bin/…`) fails its own `produces:` line. Three independent blockers, not one: (a) `pytest` and `ruff` are not on PATH at all in a fresh workspace; (b) **9 of 10 `verify:` fields are `;`-chained**, which this fleet's global shell rules forbid outright, so no card is pasteable regardless of PATH — including 36A's already-"fixed" one; (c) relative `./…` invocation depends on a cwd that resets between agent Bash calls, and the rules forbid `cd`.
- **Why:** Six voices across three `/autoplan` phases converged independently: the deliverable is **one self-bootstrapping command** (`bin/check [paths…]`), so cards describe verification scope rather than where Python lives. Rewriting `./.venv/bin/…` into ten generated cards duplicates environment topology and guarantees drift; it also makes local **diverge** from CI, which runs bare `pytest`. The corrected shape is a shell launcher plus a stdlib-only Python driver: `flock` does not exist on macOS (verified, `which -a flock` exits 1), `/bin/bash` is 3.2.57, and this repo already owns `fcntl` locking in `fsutil.py` / `lockfile.py` / `lockedjson.py`.
- **Hypothesis (untested):** split into Tracks with real edges. **W** guard `release.yml` (item above). **X** `bin/check` + `bin/_check.py` + `AGENTS.md` + `README.md ## Development` + `tests/test_docs_routing.py` + `docs/designs/track-37A.md`. **Y** normalize the 10 `verify:` fields (via `/roadmap` only). **Z** pytest-xdist + fix the three width-coupled assertions. **V** CI decomposition and packaging isolation. **U** the optional `.conductor/` hook. Edges: W before Z; X before Y, Z, V, U; Z before V. **Z must NOT sit in Group 37** — Track 37B declares `pyproject.toml` (`docs/ROADMAP.md:93`), that path is deliberately absent from `docs/shared-infra.txt`, and Group 37 is declared parallel, so the overlap fails the collision audit and destroys 37A's own stated reason for rooming with 37B.
- **Effort:** M
- **Priority:** P2
- **Context:** `/autoplan` 2026-09-01, branch `kbitz/fresh-workspace-verify`. Full review (1,280 lines) at `~/.gstack/projects/kbitz-mind-meld/kbitz-fresh-workspace-verify-autoplan-plan-20260901.md`; test plan at `…/kb-kbitz-fresh-workspace-verify-test-plan-20260901-160000.md`; 22 aggregated tasks at `~/.gstack/projects/kbitz-mind-meld/tasks-{ceo,devex,eng}-review-20260901-204411.jsonl`. **Root cause of the bare `pytest` lines:** `AGENTS.md:95` says `Run: pytest tests/`, and `/roadmap`'s card template has no `verify:` field, so the authoring agent re-derives it from the doc every regeneration (at `620ae1e` all 10 lines were bare). Fix the doc, not the generated file. Re-price the card: **M / medium risk**, not S / low — the `~45 lines` estimate was carried unchanged through two reshapes while the requirement list grew to 14 items; honest size is ~150-250 lines of driver plus ~150-300 lines of behavioural tests.

### [manual:severity=medium] pytest-xdist cuts the suite roughly 4x, and the suite is 96% of a verification cycle
- **Description:** `pytest-xdist -n auto` takes the suite from **121s to ~32s** (observed 31.5s / 34.6s / 35.5s on a 12-core Mac — card the direction, not a decimal). Not currently in `[project.optional-dependencies].dev`.
- **Why:** Environment setup is 4.6s warm / 8.5s cold, i.e. **under 4% of a full verification cycle**. The suite is the other 96%. Track 37A was scoped entirely around the 4%. Both DX voices independently said the Track is only working on the right problem if it includes this.
- **Repro:** `./.venv/bin/pip install pytest-xdist`, then `./.venv/bin/python -m pytest tests/ -q -n auto`. Exactly 3 failures, all the width-coupled tests filed above — not an xdist incompatibility.
- **Hypothesis (untested):** its own Track, **after** the `release.yml` guard, because it touches `pyproject.toml`. Bound the worker count (`-n "${MM_PYTEST_WORKERS:-auto}"`): N parallel Conductor workspaces at `-n auto` give 12N workers, which is untested, and the timing-sensitive surface is real (`tests/test_events_budget_scope.py:266` `time.sleep(0.12)`, `:232/:288` `time.sleep(0.7)`, `tests/test_integration.py:2257` a 5.0s deadline, `tests/test_lockfile.py:32-36` a spin-wait). Provide `--serial` for `-x` / `--pdb` work.
- **Effort:** S
- **Priority:** P2
- **Context:** `/autoplan` Phase 2.5 DX finding X2, 2026-09-01.

### [plan-eng-review:severity=medium] `ci.yml`'s wheel smoke and keyring assert must never become developer-facing
- **Description:** `ci.yml` runs **six** verification concerns, not three: a keyring-backend assert (`:29-36`), a `No module imports cli` grep (`:41-56`), ruff check, ruff format, pytest, and a wheel build + `mm --version` + `python -m mind_meld.cli --version` + `devices --format json` smoke (`:67-91`). Any claim that one developer command reproduces CI is false, and two of those steps are actively dangerous locally.
- **Why:** `:71 python -m pip install --force-reinstall dist/*.whl` replaces the editable install **in the same environment**; with this project's `src/` layout `import mind_meld` then resolves to site-packages and a developer's edits silently stop taking effect, so tests go green against stale code. `:85 python -m mind_meld.cli devices --format json` is written for a runner where mm is uninitialized — on a developer machine where `~/.config/mind-meld/config.toml` exists it reaches **real iCloud storage and the real Keychain during a verification run**. And the keyring assert tests runner provisioning, so it is a hard false failure in a Conductor **cloud** (Linux) workspace.
- **Hypothesis (untested):** `bin/check` owns only the three portable checks. Keyring stays a macOS-CI-only step. Wheel qualification moves to a dedicated script or CI block that builds into a **fresh disposable venv** with an isolated config/storage/keychain context — or drops `devices --format json` for a parser unit test and keeps only the hermetic `--version` smokes. Delete the `No module imports cli` grep outright: `tests/test_module_boundaries.py::test_no_module_under_src_imports_cli` is authoritative and its `_imports_cli` helper already returns line numbers, so the grep's only claimed advantage is gone. Note its filter anchor `'^src/mind_meld/cli.py:'` is relative-path-shaped and would silently stop matching under absolute-path resolution.
- **Effort:** M
- **Priority:** P2
- **Context:** `/autoplan` Phase 2.5 DX finding X1 and Phase 3 eng, 2026-09-01. Both eng voices independently called a single `bin/check` owning all six a god-script.

### [manual:severity=medium] `/roadmap` does not own the `verify:` field it regenerates
- **Description:** Every mind-meld card carries `_session: … · verify: …`, but `/roadmap`'s card template has **no `verify:` field** — verified against the resolved path (`~/.claude/skills/gstack-extend/skills/roadmap.md`), whose four "verify" hits are all prose about checking claims against git. The Current Plan is regenerated whole, so the field's content is re-authored by the drafting agent every run. At `620ae1e` it emitted 10 of 10 bare-`pytest` lines.
- **Why:** A field its own generator does not know about is not a supported contract. It also means any test policing `ROADMAP.md`'s `verify:` strings is a tripwire on the generator's normal output — the reason the `/autoplan` review killed that idea and moved the fix to `AGENTS.md`. The competing hypothesis (the regenerator copies prior art, which would explain how `_session:` has survived since ~v0.12.34 despite appearing in no template) is at least as strong, and cuts the same way: fix the doc AND normalize the strings once.
- **Hypothesis (untested):** either teach the skill the field formally (a project-level roadmap config it reads), or stop putting executable commands in generated prose and carry only a scope (`verify: tests/test_config.py`) with the runner implied by `AGENTS.md`. The second was the most interesting option nobody in the review proposed.
- **Effort:** S
- **Priority:** P2
- **Context:** `/autoplan` 2026-09-01. Cross-cutting: affects every future card, not just Track 37A. Method note worth keeping — the first grep that "proved" this read **zero bytes**, because `~/.claude/skills/roadmap/` holds a single symlink and BSD `grep -r` does not follow symlinks found during recursion.

### [manual:severity=low] No dependency lockfile, and an automatic setup hook would make an unpinned install unattended
- **Description:** `pyproject.toml` declares six `>=` ranges (`typer>=0.9`, `cryptography>=42.0`, `argon2-cffi>=23.1`, `keyring>=25.0`, `rich>=13.0`, `packaging>=21.0`) with no lockfile and no hashes. A `.conductor/settings.toml` setup hook would resolve them against live PyPI automatically on every workspace creation, removing the human's chance to notice.
- **Why:** Content-hash staleness detection — which the review recommends — answers "does this venv match the declared inputs," **not** "is this environment reproducible." Reproducibility needs a lockfile. Worth stating so nobody believes hashing bought more than it did.
- **Effort:** M
- **Priority:** P3
- **Context:** `/autoplan` Phase 1 CEO threat S2 and Phase 3 eng, 2026-09-01. Pre-existing in kind (CI already runs an unpinned `pip install -e .[dev]`); what a setup hook changes is that it becomes automatic.

### [manual:severity=low] Decide whether mind-meld standardizes on `uv` — but not under a workspace fix
- **Description:** `uv`, `hatch` and `tox` all solve "make the test command work from a clean checkout," and `hatchling` is already this project's build backend. None of the three is installed on this machine.
- **Why:** Codex's rebuttal holds: `hatchling` as a *build backend* does not mean Hatch environments are adopted, and every option trades one machine prerequisite for another. "Do not smuggle in a package-manager migration under a workspace fix." The `bin/check` interface is what holds the option cheaply — if the project ever adopts `uv`, the script's body changes and all ten cards, CI, Conductor and `AGENTS.md` keep working unchanged.
- **Effort:** M
- **Priority:** P3
- **Context:** `/autoplan` Phase 1 CEO, cross-model tension T2, 2026-09-01. Resolved toward refusal; filed so the option is recorded rather than lost.

_The eight items above were filed 2026-09-01 by `/autoplan` on branch `kbitz/fresh-workspace-verify` (Track 37A review). The two `[plan-eng-review:severity=critical]` items are live defects in `main` and are independent of any planned Track._

## Drain records

`/roadmap` drain, 38 items on 2026-09-01 (first drain since 2026-08-25; the
2026-08-30 Track 34A batch and the 2026-09-01 Track 35A batch had both gone
un-drained because the audit's `## UNPROCESSED` parser counts `[source:key=val]`
tags and this repo files `_Source: ..._` italics — it had been reporting
`ITEMS: 0` against 38 live bullets):

- **15 placed or applied.** Three new Tracks: 36A (remove OpenCode), 37A
  (workspace bootstrap), 42A (git-environment scrub, filed as S2 on 2026-08-25).
  Card amendments applied: `read-first: 34A` onto Track 38A, the struck
  "OpenCode must keep reporting an honest empty" instruction on Track 41A, the
  corrected `host_usage.py` line count on Track 40A (1,730 filed -> 2,617
  measured), and the deleted sidecar-forensics -> walker-substrate edge. Two new
  standing constraints: prove the counter schema of every reader you consume,
  and delete an unused feature rather than repairing it.
- **12 discharged** (already true at HEAD; the authored-false rate for this run
  is 12/27 = 44%). `git_capture` is read by the aggregator, twice-filed as E6.
  Groups 32 and 33 are already in shipped history, twice-filed. The Track 34A
  `blocked-by: 33A` edge no longer exists to re-derive, twice-filed. The counter-
  schema double-count, the `_tier()` pricing trap, the `PRICING_LAST_UPDATED`
  split, the today's-rates disclosure and both Track 35A card corrections all
  shipped in v0.12.52. Recapture rows already excluded from the zero-capture
  note. Both 2026-08-30 standing-constraint candidates were already in
  ROADMAP.md. Host per-model materialization is already written into Track 40A
  task 3.
- **9 deferred** to `docs/roadmap-future.md` with full context: the merged
  `~/.claude/projects` growth pair, `WALK_TIME_BUDGET_AUTOPUSH_MS`, N6
  subdirectory recovery, `mm diag` path reflow, the held xAI rate table, Grok's
  `costUsdTicks`, and stable `## Notes` codes. Three existing bullets were edited
  in place rather than duplicated (`_iter_jsonl` bounding, `--demo`,
  store-vs-binary skew).
- **2 killed.** "The retro aggregator reads synced event lines with no size
  bound" as filed named `aggregator._iter_event_objects`, a symbol that has
  **never existed** in this repo (`grep` 0 hits, `git log -S` 0 commits); the
  defect is real under the correct symbol `_iter_jsonl` and was merged into the
  existing Future bullet. "Grok is invisible for TWO independent reasons" was a
  coordination fact rather than work; one of its two halves was dissolved by
  removing OpenCode and the other is Track 38A, which names it.

Two items were resolved by user decision rather than by analysis: **mm supports
Claude Code, Codex and Grok Build; OpenCode is dropped** (2026-09-01). That
replaced the drafted OpenCode `$.id` fix with a removal, and dissolved one of the
two blockers on Track 35A's held xAI rates.

Track 28A `/autoplan` drain, 1 item on 2026-08-25:

- 1 discharged: "Retire the 0.12.42 policy-transition machinery" shipped inside
  Track 28A (v0.12.44). Evidence: `grep -rn "maybe_emit_policy_transition|
  declined_owned_link_rows|policy_transition_text|_POLICY_TRANSITION_MARKER|
  _join_display_names" src/ tests/` returns 0. Two corrections to the item as
  filed: its symbol list was incomplete (`_join_display_names` and 5 stale
  `__all__` entries were also dead), and its instruction to delete the README
  troubleshooting entry was **overruled** — `mm devices` shows 2 of 3 fleet
  machines on 0.12.13 and 0.12.34.1, neither of which ever ran a version that
  could emit the notice, so the README entry is the only explanation they will
  reach. Its stated rationale ("28A gives users a supported way to decline")
  was also wrong: 28A shipped no such command. The retirement was right for a
  different reason.
- 0 placed. 0 deferred. 0 killed.

Regen drain, 2026-08-25 — nothing from the inbox, which was already empty. Recorded
because the run's whole yield came from reconciling against git rather than from
filed items:

- 2 Tracks closed from ground truth: 25B shipped as v0.12.41 and 25C as v0.12.42,
  both still listed unshipped at HEAD three releases later. Group 25 → Shipped.
- 1 Group minted for unplanned shipped work: v0.12.43's Grok skill-discovery probe
  became Group 26, so the 27A kill has a visible cause.
- 1 Track killed: 27A (Grok row). v0.12.43 shipped the opposite conclusion plus a
  written exit criterion that refuses the row. Group 27 tombstoned.
- 1 item promoted from `docs/roadmap-future.md`: "Regenerate the roadmap AFTER a
  Track lands" → Track 28B. Sixth occurrence; the deferral reason ("a process
  convention, not a Track") is refuted by this repo's own PROGRESS-row history,
  where a convention line failed twice and a pytest fixed it.
- 2 of 3 leftover task premises on the old 26A had rotted and were rewritten with
  this-turn evidence rather than re-emitted. 0 discharged.

Track 25B `/autoplan` drain, 5 items on 2026-08-22:

- 1 placed: `mm uninstall-skills` became **Track 26A** (new Group 26, between
  Install consent and the Grok row). Placed rather than deferred because the
  installer's `absent target -> symlink -> installed` branch re-creates a
  manually deleted link on the next interactive push, so there is currently no
  supported way to decline the skill — and shipping the Grok row first would
  orphan a fourth link on every uninstall.
- 4 deferred to `docs/roadmap-future.md`: the `mm skill-run --protocol N`
  handshake, the `mm status` store-vs-binary skew nag, the README agent-name
  doc-lint, and the process fix for regenerating the roadmap after a Track
  lands rather than only before.
- 0 killed. 0 discharged.

Drain record, 7 items from the 2026-08-18 Track 23A pass:

- 1 placed: the `## Trends vs last retro` bug became Track 24B. Its revised
  deterministic prior-period design is in flight in PR #138; it removes the
  save/compare circularity and machine-local snapshot baseline.
- 1 discharged: the `mm status` agent-coverage row was absorbed into Track 25A
  (2026-08-21 regen: the one-line nag now lives on Track 24A with the store).
- 5 deferred to `docs/roadmap-future.md`: demo/fixture path,
  `--dump-host-usage` rename, bare-integer retro window, retired-device pruning,
  and reset-aware snapshot deltas.
- 0 killed.

Track 24B drain, 3 items on 2026-08-22:

- 2 deferred to `docs/roadmap-future.md`: machine-readable retro export and a
  bounded binary `_iter_jsonl` reader.
- 1 killed: `--no-trends` is an explicit non-goal; empty current windows already
  suppress the section, and otherwise the trend table is intentional output.

Host-parity inbox drained 2026-08-17: Grok allowlist shipped in Track 22B;
Codex/Grok sessions-snapshot refuse → Future. The Grok skill-link item routed to
Track 23B, which was dissolved on 2026-08-20 after failing its `/autoplan`
premise gate; 2026-08-21 regen places it as Track 27A behind Groups 24-26
(Approach B deleted Group 29).

Track 25A `/autoplan` drain, 1 item on 2026-08-22:

- 1 deferred to `docs/roadmap-future.md`: unify the seven per-agent enumerations
  across five modules. The `/autoplan` run also falsified Track 25A's premise
  (pytest never writes the real `~/.grok/skills`; the defect is silent `zip()`
  truncation), so 25A was retitled and re-scoped, Group 24 moved to Shipped, and
  the packer re-roomed the old 26A with 25A as Track 25B.
- 0 placed from the inbox: `## Unprocessed` was already empty.

_Last updated 2026-09-01 by Track 37A `/autoplan` (8 items appended, inbox now 13; 5 appended earlier the same day by the Track 36B review; `/roadmap` last drained 2026-09-01)._
