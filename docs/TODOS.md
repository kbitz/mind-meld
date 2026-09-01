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

### [plan-eng-review:severity=high] Nine files hold OpenCode references no Track owns
- **Description:** A full repo grep at `620ae1e` finds 336 OpenCode references across 34 files. Tracks 36A, 36B and 37B between them own 25 of those files. Nine are unowned by any Track: `tests/test_module_boundaries.py` (10), `tests/fixtures/host_sessions/opencode/` (6 + a `legacy/` tree), `tests/test_init_events_backfill.py` (4), `src/mind_meld/skills/retro_fleet/aggregator.py` prose (4), `tests/conftest.py` (3), `src/mind_meld/skills/retro_fleet/SKILL.md` (3), `src/mind_meld/manifest.py` (3), `docs/designs/grok-build-usage-reader.md` (3), `tests/test_token_usage.py` (1).
- **Why:** Two are load-bearing, not cosmetic. `tests/test_module_boundaries.py:693` asserts `keys[:3] == ["claude","codex","opencode"]` and `:747` asserts a user-visible string; both break under **Track 37B**, whose `verify:` line does not include that file. `tests/fixtures/host_sessions/opencode/` is a whole fixture tree 36A's card gestures at ("keep any fixture a legacy-peer tolerance test in 36B will need") without owning.
- **Effort:** S
- **Priority:** P2
- **Context:** `/autoplan` Phase 1 CEO expansion E2, 2026-09-01, branch `kbitz/pin-opencode-wire-name`. 36B deliberately did not absorb these — pulling them in creates `_touches:` collisions with 36A and 37B inside a Group declared parallel.

### [plan-devex-review:severity=high] Track 37B needs a retired-source CLI story, not just "must not resurrect"
- **Description:** 37B's card requires only that `mm enable-source opencode` "must not resurrect a retired source". Both DX voices found that leaves two user-facing behaviours, and both are wrong. (a) With an explicit `[[sync.sources]]` entry — the documented state, since `mm disable-source` preserves the block by design (`cli.py:5751-5753`) — `enable-source` prints `Enabled source 'opencode' on this device.` and 37B's load-boundary injection silently puts it straight back. Success message, permanent no-op. (b) Without one, `_validate_source_name` (`cli.py:5699-5703`) describes a **retired** source as **"not yet known to mm (forward-compat for not-yet-shipped sources)"** and offers `--force`, which teaches the user how to bypass the retirement.
- **Why:** `mm sources` will honestly show the row, and its own docstring routes the reader straight into the broken verb. Error-message actionability scored 2/10, the lowest score in the review, with both voices agreeing.
- **Hypothesis (untested):** a retired-names branch at `cli.py:5688`, before the `name in valid` return, with `--force` explicitly unable to bypass a retired built-in. Codex's shape is the better one: `enable` refuses, `mm sources` shows an explicit `Retired` state, and **`disable` stays idempotently accepted** so cleanup scripts do not start failing.
- **Effort:** S
- **Priority:** P2
- **Context:** `/autoplan` Phase 2.5 DX, 2026-09-01. Also file against 37B: add `tests/test_module_boundaries.py` to its `verify:`, and take `pyproject.toml:13`'s stale `opencode` keyword (37B already touches that file; 36B must not, or it enters the release-serialization lane).

### [plan-devex-review:severity=medium] No deprecation notice anywhere when a supported agent is retired
- **Description:** A developer upgrades mm and sees nothing. No release note, no `mm: notice:`, no `mm status` or `mm diag` line, even though `mm sources` flips a row and a previously working source stops syncing. Upgrade-path safety scored 3/10 and 4/10 across the two DX voices.
- **Why:** The repo already has the pattern — `mm: notice:` lines and the `autopush` `no-sources` breadcrumb precedent, which exists for exactly this class of silent no-op. One line closes it.
- **Hypothesis (untested):** a one-time notice when a retired source or an mm-owned orphaned skill link is detected. Belongs in Track 37B, which is the Track that creates retirement.
- **Effort:** S
- **Priority:** P3
- **Context:** `/autoplan` Phase 2.5 DX, 2026-09-01.

### [plan-eng-review:severity=medium] Two cheap doc-integrity tests the suite is missing
- **Description:** (1) `CLAUDE.md` is a symlink to `AGENTS.md`. `tests/test_docs_routing.py:28` reads `ROOT / "CLAUDE.md"` and would keep passing if an agent `Write`s that path and silently replaces the symlink with a copy — the two files would then drift with nothing catching it. Add `assert (ROOT / "CLAUDE.md").is_symlink()`. (2) `README.md:491`'s uninstall loop is executable shell, not prose, and is the only thing that removes an mm-owned `retro-fleet` link; a prose sweep that deletes a path from it strands that link permanently. Pin the loop's contents.
- **Why:** Both are one-line assertions guarding failures that are invisible in CI today.
- **Effort:** S
- **Priority:** P3
- **Context:** `/autoplan` Phases 2.5 and 3, 2026-09-01. Found independently by three of the four review voices.

### [plan-eng-review:severity=medium] Decide whether unknown-enum tolerance is a general wire posture or a one-off
- **Description:** Conditional on the Track 36B decision. If the acceptor is made tolerant of unknown `token_sources` names, the same question applies to the other closed vocabularies on the host-usage wire: host **families** (`_accept_hosts_payload` rejects unknown families as `invalid_counter`), reader **reasons** (`_HOST_READ_REASONS`), and **model ids**. They should not all move together by default — rejecting an unknown host family is correct, because an unattributable counter is not a measurement. Write down which vocabularies are open and which are closed, and why, rather than leaving it to the next retirement.
- **Why:** Standing constraint: "a Track that puts a field on a wire must name its reader." The inverse deserves the same treatment — a Track that loosens a validator should say how far the loosening goes.
- **Effort:** M
- **Priority:** P3
- **Context:** `/autoplan` Phase 1 CEO "NOT in scope", 2026-09-01. Only file this if the Track 36B User Challenge resolves toward acceptor tolerance.

_Drained 2026-09-01 by `/roadmap`; the five items above were filed after that drain by `/autoplan` on branch `kbitz/pin-opencode-wire-name`._

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

_Last updated 2026-09-01 by Track 35A `/autoplan` (12 items appended; 13 appended 2026-08-30 by Track 34A; `/roadmap` has not drained since 2026-08-25)._
