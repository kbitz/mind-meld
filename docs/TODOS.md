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

### [review:files=src/mind_meld/cli.py|tests/test_self_update.py] Test gap: Deferred update output stream failures
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** The deferred update emitter suppresses stream flush/write failures, but no test populates its update display while the output stream fails.
- **Hypothesis (untested):** Extend terminal sync coverage with an output stream that fails during the deferred update line, then assert no retry or sync exit-code change. Re-verify the reachable seam first.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/cli.py:1214
- **Evidence:** Read-only coverage audit of Track 130A found 29 of 30 changed paths protected, with no failure reproduced for this path.
- **Context:** /review-and-prep for Track 130A, branch `mmm/29c37ce6`, at `459828f`, 2026-11-12; PR link belongs to its review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/skills/retro_fleet/aggregator.py|tests/test_retro_usage_presentation.py] Test gap: Malformed registry container guards
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Peer-version tests vary version values and conflicting records, but do not directly exercise malformed registry containers, records or device IDs.
- **Hypothesis (untested):** Extend the registry evidence matrix to confirm invalid shapes remain unknown; re-verify the reachable inputs first.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/skills/retro_fleet/aggregator.py:204
- **Evidence:** Read-only coverage audit of Track 129A found untested early guards, with no product failure reproduced.
- **Context:** /review-and-prep for Track 129A, branch `mmm/bbafbd93`, 2026-11-12; PR link belongs to its review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/skills/retro_fleet/aggregator.py|tests/test_retro_fleet_cli.py] Test gap: Pricing confirmation model summary cap
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Successful pricing confirmations have exact-output coverage with at most two model IDs, but no case reaches the eight-name display cap and omitted-count suffix.
- **Hypothesis (untested):** Extend the existing confirmation test with nine distinct IDs and assert eight sorted names, `(+1 more)`, all nine persisted cards, one stderr outcome and unchanged stdout; re-verify first.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/skills/retro_fleet/aggregator.py:5085
- **Evidence:** Testing specialist and coverage audit of `e2093c2` found the gap statically; the focused 1,002-test run passed and no incorrect cap behavior was reproduced.
- **Context:** /review-and-prep for Track 128A, branch `mmm/b55d92a7`, 2026-11-11; the PR link belongs to the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/token_usage.py|tests/test_token_usage.py] Recheck Fable and Mythos 5.2 cards when observed
- **Description:** The vendor pricing page lists Fable/Mythos 5.2 at rates different from the bundled family fallback. Track 128A is limited to three Claude 5.5 cards; no fleet use of the 5.2 IDs was established during its review.
- **Hypothesis (untested):** If either ID appears in the owner's fleet, verify its exact spelling and current vendor rates before adding a distinct card and verification entry.
- **Evidence:** Read-only adversarial review of `e2093c2` and the Anthropic pricing page on 2026-11-11. Current fallback and extrapolation notices behave as designed; this is not a demonstrated regression.
- **Context:** /review-and-prep for Track 128A. This can wait for observed fleet use; that observation is the revisit trigger, not the model's availability alone.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/core.py|tests/test_output_style.py] Test gap: Shared diagnostic stream failures
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Shared warnings and notices have exact-output coverage, but no direct check of a stream failing during their write or flush.
- **Hypothesis (untested):** Extend existing output tests with a failing stream and verify command results and completed effects survive; re-verify the current sink first.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/core.py:168
- **Evidence:** Read-only coverage audit at `e1c64b0`; existing broken-stream coverage targets the final outcome emitter. No diagnostic-stream failure was reproduced.
- **Context:** /review-and-prep for Track 129B, branch `mmm/c2c48f3f`, 2026-11-11; PR link belongs to the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/cli.py|tests/test_output_style.py] Test gap: No-argument help keeps framework behavior
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Parser tests cover explicit help and invalid options or commands, but not the root invocation with no arguments and its NoArgsIsHelpError exception path.
- **Hypothesis (untested):** Extend the parser table with a no-argument invocation and verify ordinary help and its exit semantics without a shared error; re-verify the installed framework behavior first.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/cli.py:274
- **Evidence:** Read-only coverage audit at `e1c64b0`; repository search found no no-argument invocation test. No incorrect help output or exit was demonstrated.
- **Context:** /review-and-prep for Track 129B, branch `mmm/c2c48f3f`, 2026-11-11; PR link belongs to the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/attemptlog.py|tests/test_attemptlog.py] Test gap: Catch-up detail vocabulary boundaries
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** The saved-run roundtrip covers Codex and Git positive counts plus a path-bearing suffix, but not every registered reader, unknown readers or malformed counts.
- **Hypothesis (untested):** Extend the existing roundtrip table with all supported readers and zero, negative, nondecimal and near-match inputs; verify the current exact pattern first.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/attemptlog.py:421
- **Evidence:** Static repair-delta coverage audit at `e1be42a`; exact regex and identical four-reader producer/allowlist sets inspected, no incorrect acceptance or rejection demonstrated.
- **Context:** /review-and-prep for PR #310, 2026-11-11. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)


### [review:files=src/mind_meld/attemptlog.py|tests/test_attemptlog.py] Test gap: Pure formatter lock-access guard
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** The run-detail purity regression spies on diagnostic inventories but not the separate Codex cache-lock probe.
- **Hypothesis (untested):** Extend that test with a lock-access spy so reintroducing a lock probe without an inventory is independently detected; re-verify the current pure formatter first.
- **Upstream severity:** INFORMATIONAL
- **Found in:** tests/test_attemptlog.py:697
- **Evidence:** Outside challenge of `e1be42a`; current formatter has no filesystem or lock calls and no defect was reproduced. Existing test failed before the consent repair and passed afterward.
- **Context:** /review-and-prep for PR #310, 2026-11-11. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)


### [review:files=src/mind_meld/core.py|tests/test_integration.py] Test gap: Delayed measured progress retains counters
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Delayed startup renders the phase and counters retained before the display becomes visible.
- **Hypothesis (untested):** Extend a terminal test with counter advances before forcing the delay to expire.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/core.py
- **Evidence:** Static coverage audit at `97ae07a`: no specific regression-catching test found, no failure reproduced for this behavior.
- **Context:** /review-and-prep for Track 125A, branch `mmm/f1aea97f`, 2026-11-11; PR link will be in the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/events_tail.py|tests/test_host_usage_snapshot.py] Test gap: Reader completion counts on failed and absent reads
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Raised, absent and incomplete readers each advance the transient completed-reader count.
- **Hypothesis (untested):** Extend the existing reader progress fixture with each continue path.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/events_tail.py
- **Evidence:** Static coverage audit at `97ae07a`: no specific regression-catching test found, no failure reproduced for this behavior.
- **Context:** /review-and-prep for Track 125A, branch `mmm/f1aea97f`, 2026-11-11; PR link will be in the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/events_tail.py|tests/test_host_usage_snapshot.py] Test gap: Nested progress scope restores its prior sink
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Nested or exception-exiting progress scopes restore the previous ContextVar sink.
- **Hypothesis (untested):** Extend the scope test with nested and exception exits.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/events_tail.py
- **Evidence:** Static coverage audit at `97ae07a`: no specific regression-catching test found, no failure reproduced for this behavior.
- **Context:** /review-and-prep for Track 125A, branch `mmm/f1aea97f`, 2026-11-11; PR link will be in the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/events_tail.py|tests/test_host_usage_snapshot.py] Test gap: Deadline diagnostic fallback preserves a truthful remedy
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Lookup errors, a busy Codex cache and malformed diagnostic counts keep the existing unknown-count remedy.
- **Hypothesis (untested):** Exercise failing and malformed lookup results and the nonblocking cache-lock branch.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/events_tail.py
- **Evidence:** Static coverage audit at `97ae07a`: no specific regression-catching test found, no failure reproduced for this behavior.
- **Context:** /review-and-prep for Track 125A, branch `mmm/f1aea97f`, 2026-11-11; PR link will be in the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/events_tail.py|tests/test_silent_failure_contract.py] Test gap: Git pending count cutoff fallbacks
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** A zero post-commit count retries the old cutoff and a skipped append counts against the original cutoff.
- **Hypothesis (untested):** Extend the degradation fixture with zero-count and append-not-committed outcomes.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/events_tail.py
- **Evidence:** Static coverage audit at `97ae07a`: no specific regression-catching test found, no failure reproduced for this behavior.
- **Context:** /review-and-prep for Track 125A, branch `mmm/f1aea97f`, 2026-11-11; PR link will be in the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)

### [review:files=src/mind_meld/statusflow.py|tests/test_statusflow.py] Test gap: Zero pending capture omits catching-up prefix
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** The status line does not invent a positive catching-up count when pending repositories is zero.
- **Hypothesis (untested):** Extend the status fixture with zero pending and an incomplete discovery state.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/statusflow.py
- **Evidence:** Static coverage audit at `97ae07a`: no specific regression-catching test found, no failure reproduced for this behavior.
- **Context:** /review-and-prep for Track 125A, branch `mmm/f1aea97f`, 2026-11-11; PR link will be in the review receipt. Revisit in the next Track touching these files.
- **Effort:** ? (user triages in /roadmap)


## Drain records

### Roadmap regeneration — 2026-11-10 (SPEC O8 amended: output consistency; Tracks 123A, 123B, 124A and 127A recorded)

Composed with the `/project-spec` amendment of 2026-11-10 that rewrites O8.1 and adds O8.6 on the owner's words ("go -- make sure all our manually run commands have consistently formatted output -- they all need to look like they come from the same tool"). Run from manager Ephesus 20's brief. Verified at `a6f3e73` (v1.12.35). Track 125A is declared active on factory branch `mmm/f1aea97f` and keeps its label; its approved plan is not changed.

| Item | Disposition |
|---|---|
| Brief 1: one progress display for push, pull and update; an update during an attended push or pull is visible | place → Track 130A, blocked by Track 125A (both change `core._SyncProgress`) and by Track 129B (the shared style) |
| Brief 2: consistency pass over every hand-run command | place → Tracks 129B (name the style; errors, warnings, Typer usage errors; push and pull summaries), 133A (status, gc, diff, log, recapture, update notices) and 130B (init, devices, sources, conflicts, resolve, pull's warnings). Survey at `a6f3e73` in 129B's source note. `install-skills` (already `mm: error:`) and `recover` (errors through `core._error`, which 129B moves) are not carded |
| Future: "Reconcile SPEC O8.2 with the push outcome line's usage suffix" | place → Track 129B: the target changed (amended O8.1 calls capture internals routine detail), which answers the entry's question; the suffix moves to `--verbose` and status |
| Track 124B, merged as PR #307 (v1.12.34) with acceptance criteria 1 to 8, 10 and 11 VERIFIED | stays active as Track 125B for criterion 9 only (the post-merge real-machine observation). The automatic update logged on `linux-a` at 2026-11-11 01:55 UTC (1.12.30 to 1.12.35) ran from 1.12.30, before 124B's check, so it does not satisfy criterion 9 |
| Test gap: managed join wait duration contract; poll error arrivals; unknown peer count; timeout reclassification (PR #308) | place → Track 130B as one S task (its `_touches:` cover `src/mind_meld/initflow.py` and `tests/test_initflow.py`) |
| Test gap: attended cap with a legacy sweep preference (Track 124A) | place → Track 131A (covers `src/mind_meld/events_tail.py` and `tests/test_host_usage_snapshot.py`). Track 125A also covers both but is in flight with an approved plan |
| Test gaps: git executable disappears after discovery; git cancellation cleanup; inherited git transfer and batch settings; malformed SSH config fallback (Track 124B) | defer → Future: no Track touches both `src/mind_meld/upgrade.py` and `tests/test_upgrade.py` |
| Test gap: upgrade diagnostic fallback fields (Track 124B) | defer → Future: no Track touches both `src/mind_meld/statusflow.py` and `tests/test_diag.py` |
| Test gaps: bootstrap numeric version ordering; bootstrap unverifiable version after upgrade (Track 123A) | defer → Future: no Track touches `bin/bootstrap` |
| Test gap: Antigravity cross-type turn deduplication (Track 123B) | defer → Future: no Track touches `src/mind_meld/host_usage.py` |
| Future test gaps whose triggers Track 130B now meets: canonical publication after pull prefetch; late memory-index tidy flush failure; verbose memory-index tidy diagnostics; interrupted pull skips the memory-index tidy; nested Grok conflict-directory symlink; approve when the pending-device read fails | defer, stay: placing them would take Track 130B past the session-weight cap of 6; its implementer may close them while adding tests to those files |

**Shipped and recorded:** Tracks 123B (PR #304, v1.12.31), 123A (PR #305, v1.12.32; the owner waived its Mac live check M1), 124A (PR #306, v1.12.33) and 127A (PR #308, v1.12.35), each with its review receipt's completion matrix. Groups 123, 124 and 127 are retired.

**Renames:** 124B (remainder) → 125B, 126A → 131A, 130A → 132A; new Tracks 129B, 130A, 130B and 133A.

13 inbox items: **5 placed, 8 deferred, 0 killed, 0 discharged**; 1 Future entry promoted. Authored-false rate: 0 / (5 + 0) = 0%.

**Future:** 31 entries before; 1 promoted and removed, 8 added: 38 after.

**Version:** documentation-only change; no bump. The SPEC amendment changes O8's definition; the quiet-output checkpoint stays unaccepted, so the next code release stays PATCH.

### Roadmap promotion — 2026-11-09 (the bootstrap fix, the attended push's scans and the encountered bugs)

Run from manager Ephesus 17's brief on two owner decisions of 2026-11-09. First, lifting the 2026-11-08 hold for two Tracks: "1 - sure", on the manager's recommended order (the bootstrap fix, then attended push finishing its scans), and, on the second Track, "ok, that makes sense" after the manager explained that the hooks already run `mm autopush` and `mm autopull` with their own small budgets. Second, a rule: "why isn't that fix in our current roadmap? Anything that is a reproducible bug we actually encountered should be current", recorded in ROADMAP.md's standing constraints. Inbox empty: **0 drained**. Verification baseline: `e04cfa0` (v1.12.30). Each row below was checked against that tree; the Track cards carry the cited code.

| Item | Disposition |
|---|---|
| Brief 1: `bin/bootstrap` leaves an outdated mm and does not ensure Syncthing starts at login (findings 1, 2) | place → Track 123A |
| README install one-liner 404s while the repository is private (finding 3) | place → folded into Track 123A (same three files: `bin/bootstrap`, `tests/test_bootstrap.py`, `README.md`; a few docs lines) |
| Brief 2: `mm recapture` does not clear the git-walk-dropped advice (finding 5), with the retro's `git_budget` fold | place → Track 124A. Correction to the entry's hypothesis: recapture does advance the per-repository cursors when its capture is complete (`events_tail._commit_recorded_git_rows`), so the card starts from a reproduction |
| Brief 2: an attended push finishes its scans within a cap; no new flag | place → Track 124A (cap as a `[retro]` setting; no flag, decision recorded in the card). The attended-push size (weight 8) is above the cap of 6, so the brief's second Track is three: 124A (cap and the looping advice), 125A (resume, progress, "catching up" on the push and in status), 126A (the wire field and the retro's wording) |
| R2: Codex reader reported "failed" while catching up | place → 124A (finish the backlog), 125A (wording), 126A (wire and retro) |
| Finding 4: managed `mm init` stops for the key header right after accepting the folder | place → Track 127A. Observed once and not shown to recur, but the cause is deterministic in code (`fetch_crypto_init` once, no wait) and the owner's rule covers it |
| Finding 8: the codex source syncs `skills/gstack/*` | place → Track 127A, with the `agy` check and the byte-identical conflict-copy question as acceptance steps |
| Antigravity `unsupported` cause (`token_usage_on_unexpected_step_type`) leaves its usage absent from the retro (finding 6 / R2 Antigravity part) | place → Track 123B (needs a census of real Antigravity data, per the ROADMAP constraint) |
| Antigravity advice contradicts itself between the push notice and `mm status` (finding 6) | place → Track 130A |
| Finding 7: recapture's own push prints one nested-repository notice per checkout | place → Track 130A. The manager's finding said every push prints them; by the code only `recapture`'s inner `_push_core` (and `mm push -v`) does, and which command printed the 107 lines is unconfirmed |
| R1: cost floor computed from family-extrapolated Claude 5.5 rates | place → Track 128A |
| R6 (confirmation part): `--set-model-price` prints nothing on success | place → Track 128A. The code is `aggregator.py`'s handling of the flag; `update_pricing_cache` itself is in `token_usage.py` |
| R3 (attribution part): `cost_floor` causes repeated and without machine names | place → Track 129A |
| R4: wrong remedy ("upgrade mind-meld to v0.14.17+ on that Mac") for a peer whose push has not arrived | place → Track 129A |
| R5: commit-type classifier files version-prefixed subjects under `other` | place → Track 129A |
| R3 (contagion part): a failed reader floors every family | defer, stays in Future (narrowed): a deliberate, documented rule, so a design question, not a bug |
| R6 (rate tiers for overrides), R7 (Muse alias "verified" label), R8 (per-vendor verification date) | defer, stay in Future: the tier claims for `gpt-6-astra` and Haiku 5.5 are the manager's, unverified; R7 and R8 are design questions |
| `mm status` hung inside an agent session | defer, stays: seen once on 2026-11-05 in an agent session, unconfirmed, not on the owner's machines on 2026-11-09 |
| Unpriced Cursor-era Grok rows get an upgrade remedy that cannot fix them | defer, stays: logged from a plan decision on 2026-11-08, not encountered on the owner's machines |
| Test gaps whose triggers the new Tracks' `_touches:` meet: "bootstrap Arch install paths" and "bootstrap version, os-release and pipx-link stops" (Track 123A), "retained SQLite diagnostic code filtering" (Track 123B) | defer, stay: the owner said everything else stays in Future; the Tracks' implementers may close them if they are adding the tests anyway |
| New: reconcile SPEC O8.2 and O7.2 with the attended scan cap | defer → Future "Reconcile SPEC O8.2 and O7.2 with the attended scan cap", trigger Track 124A merging |

**Future:** 42 entries before; 11 promoted and removed, 2 narrowed (R3, R6 to R8), 1 added: 32 after. SPEC, CHANGELOG and PROGRESS are untouched.

### Roadmap inbox drain — 2026-11-09, second batch (retro-fleet feedback: 8 findings, 3 not findings)

Added to the same PR on the owner's word ("yes", 2026-11-09, asked whether to add these to #300). The owner ran `mm retro-fleet 7d` on a Mac (`mac-b`) and passed back a ten-item list; manager Ephesus 17 then ran it on the Linux desktop (`linux-a`), compared the two and checked each item against HEAD. Each premise was re-checked at `98a9131` (v1.12.30); the manager's numbers from the two machines are cited as its observations and were not re-run. The owner's standing direction holds: nothing is placed in Current Plan (it is empty), and every finding is deferred with a revisit trigger. **11 items: 0 placed, 8 deferred (as 6 new Future entries and one addition to an existing entry), 2 killed, 1 folded into an existing entry, 0 discharged; 2 premises narrowed.** Future: 36 → 42 entries.

| Item | Disposition |
|---|---|
| R1. Claude cost floor (`≥`) overstated: ≥$8,093 on `mac-b` and ≥$8,089 on `linux-a` against ≥$4,220 with real rates | defer → "Cost floor (`≥`) is computed from family-extrapolated Claude rates that can overstate it". Verified: no 5.5 Claude id in `VERIFIED_MODEL_IDS`, so they resolve through `MODEL_FAMILY_TIERS` (opus $5/$25, sonnet $2/$10, haiku $1/$5) and render under the floor marker, while the invariants define `≥` only as a floor "under bundled assumptions". The overrides-do-not-sync half is documented in README (they live only on the machine), so it is recorded as context, not a defect |
| R2. Codex reader reported "failed" while catching up (`state: migrating`, 852 of 1,016 files cached, 850 ms per push); the reason does not reach `MM_HEALTH` | defer → "Retro-fleet reports a Codex reader that is still catching up as "failed"". Verified: `_reader_issue_note` words every degraded reader as failed and the wire's `degraded_sources` carries names only, while the push side already prints "N of M cached". Antigravity's `unsupported` cause (`token_usage_on_unexpected_step_type`) extends the existing Antigravity entry |
| R3. `cost_floor` causes duplicated and unattributed; a Codex or Antigravity failure turns Claude's and Muse's cost into `≥` | defer → "Retro-fleet `cost_floor` causes are unattributed and repeated, and any reader failure floors every agent". Premise narrowed: the missing machine names and the repeated sentences are confirmed, but the contagion is deliberate (`_reader_scope`, and the invariants say failed-reader coverage applies to every family because the missing models cannot be attributed), so what is recorded is the open design question, not a defect |
| R4. Wrong remedy ("upgrade mind-meld to v0.14.17+ on that Mac") for a peer whose push has not arrived | defer → "Retro-fleet gives a peer whose push has not arrived the wrong remedy". Verified: `_attended_usage_remedy()` is one fixed string at nine call sites and the note names no machine |
| R5. Commit-type classifier misses version-prefixed subjects (312 of 944 commits `other`) | defer → "The commit-type classifier files version-prefixed subjects under `other`". Verified by running the pattern: `v0.0.7.0 feat: ...` does not match; the 312 figure is the manager's, and the direction says to measure on a real corpus first |
| R6. Price override cannot express rate tiers; no confirmation of the cache write | defer → "Local price overrides cannot express rate tiers, confirm nothing, and one stale vendor holds the whole refresh". Verified: one cache-create field, no success output, no long-context override; the `gpt-6-astra` and Haiku 5.5 tier claims are the manager's |
| R7. Muse pricing label | defer → same entry, narrowed as the manager said: not a silent fallback (`PRICING_FAMILY_BY_MODEL` maps `muse-wonder-full-1.0` to `muse-wonder-1.0` on purpose); the open question is whether "verified" is honest for an alias |
| R8. One stale vendor sets `refresh_due`; `--mark-pricing-verified` needs all five vendors | defer → same entry. Verified in `_render_health_block` and `update_pricing_cache`: one whole-table date |
| Not a finding: "Trends never available" | kill (explained, expected): the README's move step 3 sets the iCloud-era events log aside by design, so the Syncthing fleet's log starts 2026-11-09, and `_build_period_comparison` already says "Unavailable: the event log starts ... they appear once the log covers N days" (14 days for a 7-day window, about 2026-11-23). Computing trends from history inside snapshots is the manager's optional idea; no demand, so not recorded |
| Not a finding: "8 active days in a 7d window" | kill (explained, documented): the window is an instant 7 x 24 h and day keys are UTC calendar days, so the numerator can reach `window_days + 1`; docs/invariants/events-retro.md ("Day keys are UTC calendar days") records it and says to render no ratio against it |
| Not a finding: "Git walk ran out of budget on `linux-a`" (`git_budget`) | folded into the first batch's finding 5 entry (same root cause: the push walk's 500 ms budget against recapture's 120 s); the retro's `git_budget` note reads `walk_budget_aborts` and does not mention recapture |

### Roadmap inbox drain — 2026-11-09 (8 findings from the owner's move to Syncthing)

Run by `/roadmap` from a longshoreman brief relayed by manager Ephesus 17 after the owner moved four machines (two Macs from iCloud Drive, a Linux desktop and an Omarchy laptop) to Syncthing on 2026-11-09, following README "Moving a machine from iCloud Drive to Syncthing" and "Setting up another machine" (the owner: "then yes, let's launch a longshoreman to update the roadmap"). The findings were observed on mm 1.12.30; each premise was re-checked at `98a9131` (v1.12.30). The owner's standing direction of 2026-11-08 holds: no Track starts and no path to 2.0 is chosen until the Syncthing fleet has been used for a while, so nothing is placed in Current Plan, and the manager's recommendation to make findings 1 and 2 the first Track is recorded in its Future entry as a recommendation, not a decision. The inbox was empty when the run began; the findings are recorded here and drained in the same run. **8 findings: 0 placed, 8 deferred (as 6 new Future entries and one addition to an existing entry), 0 killed, 0 discharged; 1 premise corrected.** Future: 30 → 36 entries.

| Finding | Disposition |
|---|---|
| 1. `bin/bootstrap` never upgrades an installed mm, so `classify_config` (v1.12.25) is missing on an older mm and Syncthing is not started | defer → "Bootstrap leaves an outdated mm in place and does not ensure Syncthing starts at login (findings 1 and 2)". Premise verified in `main` and `start_syncthing_if_default`; the old `mm init`'s failure to join is the manager's observation (that release is not in this tree). Would qualify for placement (a bounded repair to O2's per-machine move); held only by the owner's direction and because all four machines are already moved |
| 2. `bin/bootstrap` does not ensure Syncthing starts at login | defer → same entry (same file, recommended as one Track with finding 1) |
| 3. README's install one-liner returns 404 for the private repository | defer → "README's install one-liner returns HTTP 404 while the repository is private (finding 3)". Privacy verified from docs/SPEC.md and the README line; the 404 itself is the manager's observation, not re-fetched |
| 4. A managed `mm init` join stops for the key header right after the folder is accepted | defer → "A managed `mm init` join can stop for the key header one moment after Syncthing accepts the folder (finding 4)". Verified: `init` fetches the header once and `_stop_for_missing_header` has no wait; the stop already writes nothing and names the rerun, so the gap is one rerun |
| 5. The git-walk-dropped advice loops after `mm recapture` | defer → "`mm recapture` does not clear the git-walk-dropped advice (finding 5)", with an untested budget hypothesis from `events.py` constants (push walk 500 ms against recapture's 120 s) |
| 6. Antigravity advice contradicts itself (`mm status` against the push notice) | defer → added to the existing "Antigravity reader logs `dropped: unsupported` on this Mac" entry, with its trigger widened to a Track touching `events_tail.py`; verified in `_no_release_reads_clause` and `_host_skip_phrase` |
| 7. Every push prints one nested-repository notice per checkout | defer, premise corrected → "`mm recapture`'s own push prints one "skipped: nested git repository" notice per checkout (finding 7, corrected)". At `98a9131` a plain `mm push` is silent (`diagnostics=verbose or dry_run`, matching sync.md and README); the call that prints without being asked is recapture's inner `_push_core` |
| 8. The codex source syncs gstack's generated Codex install (`skills/gstack/*`) | defer → "The codex source syncs gstack's generated Codex install, `skills/gstack/*` (finding 8)". Verified in `config.py`'s codex entry. Also checked: gstack-extend's generated Codex skills are dropped by their `.extend-root` marker or are symlinks, so they need no new glob; the `agy` entry is unchecked (no Antigravity install on the machine used); the byte-identical conflict copy is recorded as an open question |

### Roadmap inbox drain — 2026-11-08 (18 items; six folded into Track 122A)

Run by `/roadmap` from manager Ephesus 16's brief after the 2026-11-08 cleanup left the inbox undrained (the owner: "roadmap should have processed the todos though"). Verification baseline: `cf2e127` (v1.12.29). Within the owner's earlier instruction ("once we use the new syncing mechanism for a bit, we will determine how to get to 2.0 and what future items we actually want to pursue"), nothing is promoted from Future and no path to 2.0 is chosen; Track 122A stays the only Current Plan Track. The owner answered the PR's three review flags on 2026-11-08 ("1 kill / 2 fold in / 3 kill it"): the two approve edge cases stay killed, the six `mm init` test gaps fold into Track 122A's card, and one existing Future entry is killed (below). Every premise was re-checked at `cf2e127`: no source or test file named by an item has changed since it was filed. **Inbox: 18 drained: 6 placed, 10 deferred (as 6 Future entries, grouped where items share the same paths), 2 killed, 0 discharged. Future: 1 killed.** Authored-false rate: 0 / (6 + 0) = 0%. Future: 25 → 30 entries.

| Item | Disposition |
|---|---|
| `[review]` Test gap: Unsupported Syncthing GUI address during init | place → Track 122A, fourth task "Init integration tests for six Syncthing setup paths" (M). Its `_touches:` already cover `initflow.py` and `test_initflow.py`; the owner approved changing the card ("fold in") and treats the session-weight cap as guidance, so the card stands at weight 7 against the cap of 6 and the audit's SIZE check flags it |
| `[review]` Test gap: Init routing of Syncthing writer failures | place → Track 122A, same task |
| `[review]` Test gap: Managed join ignore-file write failure | place → Track 122A, same task |
| `[review]` Test gap: Malformed managed record at init | place → Track 122A, same task |
| `[review]` Test gap: Stale management record on default Syncthing | place → Track 122A, same task |
| `[review]` Test gap: Managed join default-folder refusal | place → Track 122A, same task |
| `[review]` Test gap: Writer HTTPS transport | defer → "Test gaps: Syncthing writer transport and response handling (3)"; no Track covers `syncthing.py` and `test_syncthing_write.py` |
| `[review]` Test gap: Malformed writer config lists | defer → same entry |
| `[review]` Test gap: Post-write verification read failures | defer → same entry |
| `[review]` Concurrent approvals can drop a fleet-folder device | kill (kept by the owner, "1 kill"): unobserved (reproduced only with in-memory doubles, not against a real Syncthing); reversible (a rerun of `mm devices approve <id>` re-adds the device, as the item says); reopen if it manifests. Track 119A's plan chose "takes no lock"; a second Syncthing writer that can run beside approve (batch approval, automatic pairing) would be new scope and should revisit this race in its own plan |
| `[review]` Approve can report approved while Syncthing cannot save its config | kill (kept by the owner, "1 kill"): unobserved (a read of Syncthing's confighandler, not reproduced); reversible (after the disk or permission fault is fixed and Syncthing restarts, a rerun of approve sees the device unapproved and approves it); reopen if it manifests |
| `[review]` Test gap: Configured auto commands with a pending device | defer → "Test gaps: never-auto-accept gate reach (2)"; no Track covers `test_devices_approve.py` and `cli.py`. Upstream CRITICAL, but a recorded test gap asserts no failure and admits no Track on its own |
| `[review]` Test gap: Pairing entry points reached through imports or aliases | defer → same entry |
| `[review]` Test gap: Pairing gate exemptions inside the writer module | defer → "Test gaps: Syncthing writer reads behind `mm devices approve` (2)"; no Track covers `syncthing.py` and `test_devices_approve.py`. Upstream CRITICAL, same reasoning |
| `[review]` Test gap: Approval state when the folders read fails | defer → same entry |
| `[review]` Test gap: Writer shape with an empty introducedBy | defer → "Test gap: writer shape with an empty introducedBy"; premise re-checked: the only `introducedBy: ""` in the tests is a folder-device entry in `test_syncthing_write.py`, not a device-config entry |
| `[review]` Test gap: Approve when the pending-device read fails | defer → "Test gap: approve when the pending-device read fails" |
| `[manual]` Unpriced Cursor-era Grok rows get an upgrade remedy that cannot fix them | defer → same title. Observed in real fleet data (Track 116A plan decision d4), but wording only: no data at risk and the owner already knows the remedy does not apply. Track 122A touches `aggregator.py` for placeholders only, so the item's own trigger is not met |

**Future killed (1):** "Bootstrap hardening candidates from the Track 98A review": unobserved and reversible (14 Low review findings, each still ending with a Run: fix), so the manifestation gate kills rather than defers it; killed by the owner, 2026-11-08 ("kill it"). Reopen any finding a real bootstrap run hits as a new item with that evidence.

**Future otherwise left as it is (24):** the brief limits this run to the inbox.

**Version:** documentation-only change; no bump under SPEC's release policy. No `## Acceptance` entry is added.

### Roadmap cleanup regeneration — 2026-11-08 (Syncthing Tracks shipped, history scrub dropped)

Run by `/roadmap` as a cleanup from manager Ephesus 16's brief on the owner's 2026-11-08 decisions. Verification baseline: `5eba9e4` (v1.12.29). The owner asked for a cleanup only ("once we use the new syncing mechanism for a bit, we will determine how to get to 2.0 and what future items we actually want to pursue"), so Future is unchanged (25 entries, none promoted, killed or rewritten), no path to 2.0 is chosen, and the 18 Unprocessed inbox items above stay held for a later full regeneration. **Leftover plan: 4 recorded as shipped, 1 dropped by the owner. Brief: 1 placed.**

| Item | Disposition |
|---|---|
| Track 117A: Detect Syncthing's default configuration and give mm a Syncthing writer | shipped: receipt added (PR #289, `3a76b91`, v1.12.25) |
| Track 118A: Set Syncthing up from the bootstrap script and `mm init` on a default configuration | shipped: receipt added (PR #291, `92d2763`, v1.12.26) |
| Track 119A: Move `mm devices` into its own module, then approve a joining machine | shipped: receipt added (PR #292, `0dad433`, 23 of 23 VERIFIED, v1.12.28) |
| Track 120A: Replace the cutover runbook with the per-machine move, and prove a move loses no file | shipped: receipt added (PR #294, `5eba9e4`, 13 of 13 VERIFIED, v1.12.29) |
| Track 121A: Rewrite the git history to remove the owner's machine hostnames | killed: dropped by the owner, 2026-11-08 ("I didn't realize there was no way to scrub the PRs, so basically this is a pointless exercise, we should just make sure it stays impersonal moving forward?", then "Yes"). GitHub keeps pull-request refs and cached views of old commits that a force-push cannot clean. docs/SPEC.md drops it as a condition of 2.0.0 and of making the repository public again; Group 121 is retired (tombstoned) and its README note, PR #295, is closed unmerged. Do not re-propose without a new owner decision |
| Brief: keep the tree impersonal from here on (owner, 2026-11-08) | place: Track 122A (one-time placeholder cleanup of the current tree and no hostname default for `mm init`'s device name); the forward rule is added to AGENTS.md. The owner refused a committed list of names, a CI check and an Actions-secret or factory-side scanner ("The ci check would be part of the repo though, so we aren't doing that"; "Neither of those") |

**Version:** documentation-only change; no bump under SPEC's release policy. The amended 2.0.0 release line loses a condition and adds no checkpoint; no `## Acceptance` entry is added.

### Roadmap regeneration — 2026-11-07 (no cutover: per-machine move and Syncthing setup)

Run by `/project-spec` and a full `/roadmap` regeneration from manager Ephesus 13's brief on the owner's 2026-11-07 decisions. Verification baseline: `488094e` (v1.12.23). docs/SPEC.md drops the fleet-wide cutover and migration tool (each machine moves by reinstalling mm and joining the Syncthing fleet; a Mac not yet moved keeps running on iCloud Drive) and rewrites O2 for first-install Syncthing setup when Syncthing has its default configuration, approval by pasted device ID and an introducer mesh. Tracks 113A, 114A and 115A are recorded as shipped and Groups 113 to 115 retired. In Progress and Current Plan were regenerated whole: Track 116A (kept, with one inbox item), Tracks 117A to 120A (new: Syncthing writer; bootstrap start at login and `mm init` setup; the move-only `mm devices` move and approval; per-machine move) and Track 121A (the history scrub, previously 117A, still last). The owner asked for the fewest Tracks the size audit allows (the owner, 2026-11-07: "Do we need this many separate tracks?", then "Revise as discussed"), so the first candidate's separate bootstrap and move-only Tracks were folded in. **Inbox: 6 drained: 1 placed, 3 deferred, 0 killed, 2 discharged. Leftover plan: 3 recorded as shipped, 2 kept. Brief: 3 placed. Future re-derived: 26 entries, 4 placed, 1 narrowed (removed and re-added), 1 trigger restated, 2 kept deferred by owner decision despite a met trigger, 3 added, so 26 → 25.** Authored-false rate: 2 / (8 + 2) = 20%.

| Item | Disposition |
|---|---|
| Track 113A: Sync gstack-extend's per-host run history | shipped: receipt added (PR #284, `d4a104b`, 8 of 8 VERIFIED) |
| Track 114A: Remove `mm cursor-agent`, Cursor's hook command and Cursor usage consent | shipped: receipt added (PR #285, `4b36180`, 16 of 16 VERIFIED) |
| Track 115A: Remove the Cursor usage reader | shipped: receipt added (PR #286, `488094e`, 19 of 19 VERIFIED) |
| Track 116A: Remove Cursor from git-root discovery, pricing provenance and retro-fleet | keep: premises re-verified at `488094e`; gains the inbox item below and `src/mind_meld/events.py`. It then shipped (PR #287, `7ea2cc0`, v1.12.24) while this PR was open; the merge of main records its receipt and retires Group 116 |
| Track 117A (previous plan): Rewrite the git history to remove the owner's machine hostnames | keep as Track 121A, blocked also by Track 120A so it stays last |
| Brief: no migration tool, per-machine move by reinstall and join (the owner, 2026-11-07) | place: Track 120A (README per-machine move replacing the cutover runbook, and a no-loss join test) |
| Brief: mm sets Syncthing up when it has its default configuration, guide otherwise (the owner, 2026-11-07) | place: Tracks 117A (classifier and writer) and 118A (bootstrap starts Syncthing at login, `mm init` setup and the guide) |
| Brief: approval by one pasted device ID, exact match, never automatic; auto full mesh (the owner, 2026-11-07) | place: Track 119A (a move-only first task for `mm devices`, as core-and-extensions requires, then `mm devices approve` and introducers) |
| `[review]` Cursor leftovers after Track 114A (aggregator note offers `cursor`; `disable-source cursor --force` leaves the prober running) | place → Track 116A, which discharged it (see the Track 116A drain record below): re-verified at `488094e` (`aggregator.py` "No agent-log reader contributed" note; `events._enabled_root_probers` `prober.name == "cursor"` branch) |
| `[review]` Track 115A card lists the push cursor's hold and coverage warnings as Cursor symbols | discharged@488094e: Track 115A's plan preserved them (completion matrix criterion 3, "preserve challenged progress warnings"); `_CURSOR_HOLD_DEGRADATION` and `_CURSOR_COVERAGE_DEGRADATION` remain in `events_tail.py` |
| `[review]` Cursor-removal doc nits for Track 115A | discharged@488094e: README's Track 114A Upgrading note now qualifies machines with no consented host reader; `events_tail._HOST_DISPLAY_NAMES` has no `cursor` entry |
| `[review]` Test gap: retained SQLite diagnostic code filtering | defer: no planned Track touches `src/mind_meld/host_usage.py` and `tests/test_agy_usage.py`; revisit when a Track's `_touches:` cover them |
| `[review]` Test gap: retired Cursor attended-attempt upgrade | defer: no planned Track touches `src/mind_meld/attemptlog.py` and `tests/test_attemptlog.py`; revisit when a Track's `_touches:` cover them |
| `[review]` Test gap: unrecognized migration directory shapes | defer: no planned Track touches `src/mind_meld/core.py` and `tests/test_gstack_extend_analytics.py`; revisit when a Track's `_touches:` cover them |

**Future placements (4):** the four Syncthing client test gaps (HTTPS certificate and response cases, redirect destination isolation, total request budget, input byte ceilings) → Track 117A, combined into one task to stay inside the size cap; each trigger was "a Track's `_touches:` cover" its paths. "Test gap: bootstrap Arch install paths" and "Test gap: bootstrap version, os-release and pipx-link stops" stay deferred, unchanged, by the owner's decision to keep Track 118A lean, although its `_touches:` (`bin/bootstrap`, `tests/test_bootstrap.py`) meet their trigger.

**Future reassessed for the target change (2):** "mm-managed Syncthing: invite codes, mesh healing, relays, folder setup and scan-after-push" is narrowed to "mm-managed Syncthing beyond O2: ..." with its full text kept, because SPEC O2 now covers folder setup, approval and the introducer mesh. "Qualify cross-model memory in isolated homes (Track 96A)" keeps its text; its trigger no longer names the dropped cutover runbook.

**Reassessed killed record:** "Two distinct valid key headers" (killed 2026-11-05, reopen "before anyone creates a new fleet on a Syncthing folder") meets its reopen condition now that the fleet may start fresh. It stays killed: still unobserved and reversible (a machine re-joins), and the planned join path never writes a key header (`_bootstrap_new_fleet` is its only writer, reached only by `--new-fleet` in an empty folder).

**Future kept (19, including the two bootstrap test gaps above):** the remaining entries were re-derived on the 2026-11-07 regeneration that preceded this one; none of their triggers is met by the new Tracks, and the manifestation gate prunes none (the unlink guard is the irreversible quadrant; the Antigravity and `mm status` hang entries were observed).

**Version:** documentation-only change; no bump. The amended release policy changes the 2.0.0 conditions and adds no checkpoint; no `## Acceptance` entry is added.

### Track 116A drain — 2026-11-07

Drained by Track 116A (`/implement`, branch `mmm/fc43e378`):

| Item | Disposition |
|---|---|
| `[review]` Cursor leftovers after Track 114A: dead `enable-source cursor` hint and a no-op `disable-source cursor --force` | discharged in Track 116A: `rootprobes.py` removes the `cursor` prober, `events._enabled_root_probers` drops `[retro] cursor_host_usage`, and `aggregator.py`'s no-reader note names `codex`, `grok`, `muse` and `agy` |

### Roadmap regeneration — 2026-11-07 (Cursor removed, history scrub before 2.0, Future cleanup)

Run by `/project-spec` and a full `/roadmap` regeneration from manager Ephesus 9's brief on the owner's 2026-11-06 and 2026-11-07 decisions. Verification baseline: `fd23f25` (v1.12.20). docs/SPEC.md removes Cursor support completely ("cursor -- removed totally, don't worry about old usage, as if we never supported") and makes the git-history hostname scrub a 2.0.0 release condition and a precondition for making the repository public again ("that scrub is required before we go public again and hit 2.0 fyi"). Track 93A (PR #280, v1.12.20) is recorded as shipped against its completion matrix and Group 93 is retired; Track 112A was already recorded. In Progress and Current Plan were regenerated whole: Track 113A (gstack-extend per-host run history, the manager's launch card), Tracks 114A to 116A (Cursor removal, serialized because each deletes what the next one's callers use), and Track 117A (the owner-gated history rewrite, last). Track 96A moved to Future: SPEC says O9 starts after the cutover, which has not run. **Inbox empty: 0 drained. 5 brief and leftover plan items: 1 recorded as shipped, 3 placed, 1 deferred, 0 killed, 0 discharged. Future re-derived: 41 entries, 11 killed, 5 placed (two of them the gstack-extend and history-scrub entries the brief also names), 25 kept, and 1 added, so 41 → 26.** Authored-false rate: 0 / (6 + 0) = 0%.

| Item | Disposition |
|---|---|
| Track 93A: Write the cutover runbook and the Syncthing documentation | shipped: receipt added (PR #280, `fd23f25`, 17 of 17 VERIFIED) |
| Track 96A: Qualify cross-model memory in isolated homes | defer: SPEC plans O9 after 2.0 and says "O9 starts after the cutover"; card kept in Future; revisit when the owner completes the cutover and PROGRESS.md records O1 to O4 on Syncthing |
| Brief: sync gstack-extend's per-host run history (the owner, 2026-11-06, "yes make it true"; trigger met by gstack-extend v0.71.0) | place: Track 113A with the launch card's text, replacing the Future entry |
| Brief: remove Cursor completely (the owner, 2026-11-06 and 2026-11-07) | place: Tracks 114A (commands, hook command and consent), 115A (the usage reader) and 116A (git-root prober, pricing provenance, retro-fleet) |
| Brief: the git-history hostname scrub gates 2.0.0 and going public (the owner, 2026-11-07) | place: Track 117A, owner-gated, replacing the Future entry |

**Future kills (11):**

| Entry | Reason |
|---|---|
| Reader-agnostic quarantine and drift classification (Track 46B) | unobserved; reversible (host records stay on disk and are re-read once a reader is fixed); its premise names the retired Grok reader (`_GROK_STOPS` and `_classify_grok_update` are gone at `fd23f25`); reopen if a drift manifests |
| `[retro] codex_host_usage` knob | no SPEC outcome needs it and no demand was ever observed; AGENTS.md's planning rule asks for demonstrated demand before a removal control |
| `mm diag` lists repo-local `GIT_*` variables (E6) | no SPEC outcome needs it; no confusion report since 2026-09-09, and `mm recapture --dry-run` already names the repository and the reason |
| Establish Cursor's memory capability and portability contract | Cursor support removed (SPEC, 2026-11-07); docs/designs/memory-continuity.md's link to it becomes plain text |
| Revisit Cursor auto/composer classification only after observed use | Cursor support removed |
| Read Conductor's tmpdir or env-override Cursor stores | Cursor support removed |
| Overdue counterless-FINISHED tracking and an SQLite authorizer for the Cursor adapter | Cursor support removed; Track 115A deletes the adapter |
| Report the largest synced subtrees in 2.0 status/diag | no SPEC outcome needs sizes: O6's shipped growth warning names the subtree when a push adds many files, and the cutover runbook copies the store whole without a re-measure |
| Per-Track factory cost in `retro-fleet` | no SPEC outcome needs it (factory workers' tokens already count under their host's row), and the `~/.mmm` source it was filed with is itself deferred; reopen on the owner's request |
| Deflake the load-sensitive tests in `tests/test_self_update.py` | reversible: a serial rerun passes, as AGENTS.md's load rule says; no CI failure recorded |
| `test_xdist_record_and_selection` times out under heavy load | reversible: a rerun clears it, as AGENTS.md's load rule says; never seen at low load |

**Future placements (5):** "Sync gstack-extend's run history" → Track 113A; "Rewrite git history to remove the owner's machine hostnames" → Track 117A; the recorded test gaps "reconfigure reenrolls disabled Grok" → Track 114A, "Grok diagnostic unknown-state branches" → Track 115A and "complete Grok row with cache writes suppresses ceiling" → Track 116A, each because that Track's `_touches:` cover the gap's paths.

**Future kept (25):** mm-managed Syncthing and per-path conflict policies (SPEC non-goals with their triggers); the opt-in mmm source (SPEC's factory-handover non-goal cites it); the O8.2 usage-suffix reconciliation (the quiet-output checkpoint, which 2.0.0's O8 condition meets); implementing cross-model memory (O9); the Antigravity `dropped: unsupported` reader (O7.4's acceptance run); `mm status` hanging in an agent session (observed once; O8.4); guarding raw unlinks (irreversible loss of the owner's files if it manifests); bootstrap hardening (the cutover's Linux joins are its first real use); retro-fleet by identity category (its old trigger, Track 104A shipping, was met; the owner kept it with a new trigger, "2 - a": revisit when he asks for a work or personal view in a retro); and 15 recorded test gaps whose `_touches:` triggers no new Track meets.

**Future:** 41 → 26 entries; all 5 entries that lacked a revisit trigger were killed, so none lacks one now.

**Version:** documentation-only change; no bump. The amended release policy adds a 2.0.0 condition and no checkpoint; no `## Acceptance` entry is added.

### Roadmap record pass — 2026-11-07 (Track 112A shipped)

Run by `/roadmap` from manager Ephesus 9's brief on the owner's 2026-11-06 and 2026-11-07 decisions. Verification baseline: `ffbfaa7` (v1.12.19). Track 112A (scrub the owner's machine hostnames from the public repository) ran outside the plan on the owner's instruction ("yes, they need to be scrubbed -- that's not great") and is recorded as shipped against PR #281's completion matrix; it keeps its ID, the first free integer after shipped history, and Group 112 is retired. The release script refuses a head that already changes CHANGELOG.md, so PR #281 deferred its CHANGELOG half; this record PR applies the manager's saved CHANGELOG patch, which changes only old entries and adds none. The owner then made the repository private and deferred a history rewrite. The current plan is unchanged: Track 93A, then 96A. **Inbox empty: 0 drained. 1 brief item: 0 placed, 1 deferred, 0 killed, 0 discharged.** Authored-false rate: 0 / (0 + 0), no placements.

| Item | Disposition |
|---|---|
| Brief: rewrite git history to remove the owner's machine hostnames (owner, 2026-11-07, "don't have to do it now though") | defer: needs a force-push to the protected `main` and the owner's explicit go at the time; revisit when the owner asks for it, or before the repository is made public again |

**Future:** 1 entry added (40 → 41); 5 older entries still lack a revisit trigger, and the trigger migration was not run.

**Version:** documentation-only change; no bump.

### Roadmap record pass — 2026-11-06 (Track 95A to Future)

Run by `/project-spec` and `/roadmap` from manager Ephesus 9's brief on the owner's 2026-11-06 decision about Track 95A, the opt-in `mmm` source for factory handover (factory item 83c9d3b4). Shown the plan's costs, the owner decided: "hm, don't love that -- let's future this card and move on to the next one". No code was written. Verification baseline: `da9620d` (v1.12.18). docs/SPEC.md now says mm does not carry `~/.mmm/` and the factory moves with mmm-factory's own tool; its "Factory handover" constraint moved to Non-goals with a revisit trigger, keeping the `factory-handover` anchor. Group 95 is removed from the plan; Track 93A drops Track 95A from `_blocked-by:` and is next, then 96A. **Inbox empty: 0 drained. 1 leftover plan item: 0 placed, 1 deferred, 0 killed, 0 discharged.** Authored-false rate: 0 / (0 + 0), no placements.

| Item | Disposition |
|---|---|
| Track 95A: Add an opt-in mmm source for factory handover | defer (owner, 2026-11-06): card text, cost evidence (ledgers of 9.8, 3.5 and 2.0 MB re-uploaded whole on each change; about 1,087 files and 91 MB on the first push) and the plan's main findings (backup-first framing; `practices.md` and `settings.toml`; autopull on a move target) kept in Future; revisit when the owner asks again for an off-machine backup or handover of the factory home, or mmm-factory's own move tool cannot carry the factory between hosts |

**Future:** 1 entry added (39 → 40); 5 older entries still lack a revisit trigger, and the trigger migration was not run. The "Test gap: reconfigure reenrolls disabled Grok" entry still says Track 95A touches `sourceflow.py`; its text is kept as written, and its `_touches:` trigger is unaffected.

**Version:** documentation-only change; no bump.

### Roadmap record pass — 2026-11-06 (Group 111 shipped)

Run by `/project-spec` and `/roadmap` from manager Ephesus 9's brief on the owner's 2026-11-06 decisions. Verification baseline: `065e9cb` (v1.12.18). Tracks 111B (v1.12.17, PR #276) and 111A (v1.12.18, PR #277) were recorded as shipped against their PRs' completion matrices, 111A against its scope as amended by the owner; Group 111 is retired. docs/SPEC.md records the two owner decisions from 111A's live checks (topics only; no-origin and non-repository scopes arrive elsewhere as inert copies). **1 inbox item drained: 0 placed, 1 deferred, 0 killed, 0 discharged.** One Future entry was discharged by Track 111B. Authored-false rate, counting the Future discharge: 1 / (0 + 1) = 100%.

| Item | Disposition |
|---|---|
| Test gap: complete Grok row with cache writes suppresses ceiling | defer: no remaining Track touches the aggregator; re-checked at `065e9cb`, the only cache-write cases are partial |
| Future: Test gap: Grok cause lead under a local pricing override | discharge@`533df89`: its premise (a model with an active override has no detail) is false since Track 111B, and `test_long_context_cause_lead_under_local_pricing_override_111b` pins the lead under an override |
| Brief: sync gstack-extend's run history (owner, 2026-11-06, "yes make it true") | defer: waits for gstack-extend's per-host `analytics/stage-runs-<host>.jsonl` release |

**Future:** 2 entries added and 1 discharged (38 → 39); 5 older entries still lack a revisit trigger, and the trigger migration was not run. Two Grok test-gap entries whose `_touches:` triggers name files Track 111A changed outside its card (`resolveflow.py` with `tests/test_conflict_copy.py`; `sourceflow.py` with `tests/test_source_toggle.py`) stay deferred: 111A added no test for either gap, and no remaining Track covers their paths.

**Version:** documentation-only change; no bump.

### Roadmap record pass — 2026-11-05 (Grok Tracks shipped)

Run by `/roadmap` from manager Ephesus 8's brief on the owner's 2026-11-05 word ("Merge it then do any docs pr you need then pause"). Verification baseline: `580cd1c` (v1.12.16). Tracks 109B (v1.12.14, PR #271), 109A (v1.12.15, PR #272) and 110A (v1.12.16, PR #273) were recorded as shipped against their PRs' completion matrices; Groups 109 and 110 are retired. Track 95A gained a Done-when line: README tells the owner not to run `mm pull` on the target machine until mmm-factory's own move tool (`migrate-home`, `mmm-manager launch --from`) finishes copying `~/.mmm` (the owner, 2026-11-05: "both are fine, that way mmm can migrate without the mm prereq, but we can keep machines in sync, seems fine"). **11 inbox items drained: 1 placed, 10 deferred, 0 killed, 0 discharged** (8 filed by the three Tracks' reviews, 3 from the brief). Authored-false rate: 0 / (1 + 0) = 0%.

| Item | Disposition |
|---|---|
| Test gap: unreadable Grok source root | place → Track 111A, which touches `manifest.py` and `tests/test_manifest.py`; re-checked at `580cd1c`: `walk_grok_source`'s root `stat` error branch has no test |
| Test gaps: Grok usage file size boundaries; Grok filesystem failure boundaries | defer (two entries): no Track touches `hosts/grok.py` |
| Test gap: Grok diagnostic unknown-state branches | defer: no Track touches `statusflow.py` |
| Test gap: nested Grok conflict-directory symlink | defer: no Track touches `resolveflow.py` |
| Test gap: reconfigure reenrolls disabled Grok | defer: Track 95A touches `sourceflow.py` but not `tests/test_source_toggle.py` |
| Test gap: Grok cause lead under a local pricing override | defer: no Track touches the aggregator |
| Local pricing overrides miss Grok aliases and drop the long-context floor | defer with its filed trigger: display-only, and 109B's dates make today's cached override inactive |
| Brief: `test_xdist_record_and_selection` timed out at 180 s twice under heavy load | defer: a rerun clears it; revisit if it times out at low load or blocks a release again |
| Brief: Antigravity reader `dropped: unsupported` on this Mac | defer: pre-existing, named rather than silent; no evidence of impact on the owner yet |
| Brief: `mm status` hung inside an agent session | defer: seen once in an agent session, cause unconfirmed |

**Future:** 10 entries added (29 → 39); 5 older entries still lack a revisit trigger, and the trigger migration was not run.

**Version:** documentation-only change; no bump.


### Roadmap drain — 2026-11-05 (Grok Build parity)

Run by `/project-spec` with `/roadmap` from manager Ephesus 8's brief on the owner's 2026-11-05 decision ("go, it needs to bring grok to parity w/ the other models"); the owner approved the candidate with "1" (plugins stay local). Verification baseline: `027840b` (v1.12.13). Tracks 108A (v1.12.11, PR #266), 98A (v1.12.12, PR #267) and 92A (v1.12.13, PR #268) were recorded as shipped. **14 inbox items drained: 1 placed, 0 killed, 13 deferred, 0 discharged**; one test gap returned from killed Track 107A is placed again, and one Future entry is promoted.

| Item | Disposition |
|---|---|
| Brief: Grok Build at parity with Claude Code and Codex | place → Tracks 109A (source and consent), 110A (usage reader) and 111A (memory sync) |
| Brief: price Cursor's `grok-4.7-high-fast`, `-xhigh`, `-xhigh-fast` | place → Track 109B, which also promotes the Future entry "Price Cursor Grok Fast after a real census" (trigger met by those runs) and prices `grok-4.7-build` |
| Current Plan: Track 107A, the Cursor account usage reader | kill: after the account moved to the X unified subscription around 2026-10-21, per-request usage is only in the account's billing export, and the owner chose local-only capture ("A, but we don't need the separate line"); reopen when Cursor's ACP sends per-call usage |
| Test gap: Unknown Syncthing status counters | place → Track 110A, which touches `statusflow.py` and `tests/test_diag.py` |
| Test gap: ACP notice after successful capture or a reader blocker (returned from Track 107A) | place → Track 110A, same files |
| Test gap: Canonical publication after pull prefetch | defer: no Track covers `pullflow.py` with `tests/test_pullflow.py` |
| Test gaps: Syncthing HTTPS certificate and response cases; redirect destination isolation; total request budget; input byte ceilings | defer (four entries): no Track touches `syncthing.py` |
| Test gap: Peer cursor invalid timestamps | defer: no Track covers its four paths |
| Test gap: remaining nonqualifying link targets | defer: no Track touches `merge.py` |
| Test gaps: late tidy flush failure; verbose tidy diagnostics; interrupted pull skips index tidy | defer (three entries): no Track covers `pullflow.py` with `tests/test_pullflow.py` |
| Test gaps: bootstrap Arch install paths; bootstrap version, os-release and pipx-link stops | defer (two entries): no Track touches `bin/bootstrap` |
| Bootstrap hardening candidates from the Track 98A review (14 Low findings) | defer to Track 93A's cutover runbook (bootstrap's first real use); revisit when a real bootstrap run hits one (factory manager's change, accepted with the owner's option 1, 2026-11-05) |

**Future:** "Price Cursor Grok Fast after a real census" promoted into Track 109B. "Render `retro-fleet` for one identity category" still has its trigger met (Track 104A shipped); not required by a SPEC outcome, it stays deferred until the owner asks.

**Version:** documentation-only change; no bump.

### Roadmap drain — 2026-11-04

Run from manager Ephesus 8's brief and addendum on the owner's 2026-11-04 decisions: card the Cursor account usage reader ("yes") and put it next; no more investigation-only Tracks whose premise is known or stale ("We did a whole track to document a cursor gap? That is dumb; I thought it was fixing cursor usage."). 5 inbox items: **1 placed, 0 deferred, 4 killed, 0 discharged**; 2 Current Plan Tracks killed. Authored-false rate: 0 / (1 + 0) = 0%. Verification baseline: `d3828c0` (v1.12.10). Tracks 105A (v1.12.6, PR #259), 102A (v1.12.7, PR #260), 104A (v1.12.8, PR #261), 106A (v1.12.9, PR #262) and 103A (v1.12.10, PR #263) were recorded in shipped history; the brief named only 103A and 106A, and main showed the other three merged and still carded.

| Item | Disposition |
|---|---|
| Brief: an opt-in Cursor account usage reader through cursor-agent's existing login | place → Track 107A, first in the Current Plan |
| Test gap: ACP notice after successful capture or a reader blocker (Track 103A review) | place → Track 107A, which touches both files and rewrites that notice |
| Warm autopush and `mm diag` discovery may not fit their 50 ms budget | kill: unobserved; reversible (the next attended push or `mm recapture` completes discovery); reopen if `mm status` reports the cursor held, or `mm diag` reads `partial`, after an attended push recorded discovery `complete` |
| `mm refresh-identity` persists discovery without the mm lock and reports a cut gather as refreshed | kill: unobserved; reversible (the discovery cache is rebuilt by the next pass); reopen if a refresh-identity run overlaps a push, or the gather measures over 4 s on any Mac |
| Prober errors read as unfinished discovery, and identity refreshes from partial passes | kill: unobserved; reversible (wrong wording, and identities recompute on the next complete pass); reopen if `mm diag` shows a prober error while `mm status` shows the discovery wording, or a retro misses a repository-local email once the index is warm |
| A round resumed from a saved listing can read complete without seeing records added since | kill: unobserved in real use (a probe reproduced it); reversible (`mm recapture <window>` recovers the commits); reopen if a newly found repository misses commits between its first session and the push that found it |
| Current Plan: Track 99A, move the Muse usage reader into its own module | kill: it existed only to prepare Track 100A; reopen with 100A |
| Current Plan: Track 100A, find why the Muse reader refuses this Mac's records | kill: premise stale; the refusal has not recurred since the 2026-11-02 budget fix (manager's `mm diag --json` on this Mac at 1.12.10, 2026-11-04: `muse.last_reason` null, last complete read 2026-11-04T03:31Z, 1,086 of 1,097 files cached), and its card said to kill it if the refusal did not recur; reopen if Muse's `last_reason` reads `unsupported` again |

**Open observation (not an inbox item):** on 2026-11-04 the manager's `mm refresh-identity --json` on this Mac listed `probe@localhost`; its source is not yet found. Recorded on Track 106A's shipped entry.

**Future:** not reassessed. "Render `retro-fleet` for one identity category" has its trigger met (Track 104A shipped) and waits for the owner's reassessment; it is not promoted.

**Version:** documentation-only regeneration; no bump.

### Roadmap promotion — 2026-11-02 (Codex warm read)

Run from manager Ephesus 8's brief on the owner's "ok, keep going" (2026-11-02), on the recommendation to build Track 101A as planned and card the real Codex fix next. Inbox empty: **0 drained**. Verification baseline: `3cd462c` (v1.12.5). Tracks 91B (v1.12.4, PR #254) and 101A (v1.12.5, PR #257) were recorded independently in shipped history against their completion matrices; 101A's post-release rows are closed by the manager's `mm diag --json` on this Mac after the owner's attended push. Groups 91 and 101 are tombstoned and In Progress is empty. New: Track 105A (memoize settled Codex records' totals and slim their cache entries), placed first; Track 102A gains `_blocked-by: Track 105A` because both edit `events-retro.md` and `tests/test_module_boundaries.py`, with its scope unchanged. Future stays at 17 items (byte-identical); 6 entries still lack an inline revisit trigger, and the trigger migration was not run.

| Item | Disposition |
|---|---|
| Brief: the Codex warm read still grows with archived states (aggregate, cache load and write, entry validation) after Track 101A | place → Track 105A |

### Roadmap promotion — 2026-11-02 (usage capture)

Run from manager Ephesus 8's brief on the owner's "go, and we need to fix the cursor usage gap" (2026-11-02, after `mm retro-fleet` showed no usage outside Claude) and its addendum on the owner's question whether every project worked through Paseo is tracked. Inbox empty: **0 drained**. Verification baseline: `df7d1e8` (v1.12.3), re-checked on this Mac with `mm diag --json`. Three Tracks are carded after the pinned Track 91B, in the owner's order: Track 101A (one slow usage reader can no longer starve the others, and the attended default fits the corpus), Track 102A (discovery finds projects in every enabled provider's logs, then in gstack, Paseo and mmm when installed, per the owner's scope for the card) and Track 103A (find and qualify a source for Paseo-driven and interactive Cursor usage, stopping for the owner if none gives per-session tokens). Track 99A gains `_blocked-by: Track 103A` to hold that order; its scope is unchanged. One Future entry is folded into Track 103A because its trigger is met (cursor-agent is now 2026.10.22), so Future goes from 17 to 16. Premise note: `git diff v1.11.20 v1.12.3` leaves the usage readers unchanged, so Track 101A's first task tests the 1.12.x-regression reading instead of assuming it. 6 Future entries still lack an inline revisit trigger; the trigger migration was not run.

| Item | Disposition |
|---|---|
| Brief: Codex and Antigravity reads end on `deadline` every attended push since 2026-11-01 | place → Track 101A |
| Addendum: 6 of 13 Paseo projects reach discovery only through this Mac's `[retro] repo_roots` pins | place → Track 102A |
| Manager: Muse `unsupported` cleared after the 17:06 UTC attended push with no code change | note on Track 100A's card; its first task re-checks the premise, kill if it does not recur |
| Brief: Cursor usage via Paseo (ACP) and the Cursor app is not captured | place → Track 103A |
| Future: Re-check Cursor ACP (Paseo) capture after each cursor-agent upgrade | place → Track 103A, first task (trigger met) |

### Roadmap promotion — 2026-11-02 (Muse reader)

Run from manager Ephesus 8's brief on the owner's "ok sounds good" (2026-11-02) to card the Muse usage reader next, ahead of the bootstrap script. Inbox empty: **0 drained**. One Future entry promoted, "Read Muse Code 1.4.14's usage records (the reader has stopped on `unsupported` since 2026-10-21)", so Future goes from 18 to 17. Verification baseline: `927d84a` (v1.12.3); the symptom was re-observed that day at mm 1.12.3 (`muse.last_reason: unsupported` since 2026-10-21, 486 of 1,036 files cached). It becomes two Tracks, because the core-and-extensions constraint puts a move-only Track before any change to a reader in `host_usage.py`: Track 99A moves the Muse reader into `hosts/muse.py`, and Track 100A reads the cause on this Mac (read-only), re-censuses the corpus into the Muse CONTRACT.md and fixes the reader. Track 91B stays pinned (factory item 53b6eda2) with its card unchanged. The bootstrap script gains `_blocked-by: Track 100A` to hold the owner's order, leaves Group 91 and takes back its earlier ID (91C → 98A); Tracks 92A and 93A name it under the new ID, with no other change. No Tracks shipped since `927d84a`. 6 Future entries still lack an inline revisit trigger; the trigger migration was offered, not run.

| Item | Disposition |
|---|---|
| Future: the Muse usage reader has stopped on `unsupported` since 2026-10-21 | place → Tracks 99A (move-only) and 100A (cause, census, fix); trigger met: the owner chose to fix it before the 2.0.0 cut, which needs O7 |

### Roadmap drain — 2026-11-02

Run from manager Ephesus 8's brief on the owner's "go for it" after Track 91A merged. 10 inbox items: **0 placed, 2 deferred, 8 killed, 0 discharged**. Authored-false rate: 0 / (0 + 0), no placements. Verification baseline: `5a814c3` (v1.12.3). Tracks 97A (v1.12.2, PR #249) and 91A (v1.12.3, PR #251) were recorded independently in shipped history against their verified completion matrices; Group 97 is tombstoned, and Group 91 stays active for Track 91B (factory item 53b6eda2). Track 98A packs beside 91B now that 91A no longer holds `README.md`, so it is Track 91C. Track 91A's deferred manual check (matrix row D1) is placed on Track 93A's runbook. mmm-factory's open questions are recorded on Track 95A's card. Future grows from 16 to 18; 6 entries still lack an inline revisit trigger, and the trigger migration was offered, not run.

| Inbox item | Disposition |
|---|---|
| The Muse usage reader has stopped on `unsupported` since 2026-10-21 | defer → Future, with the trigger "before the complete-usage checkpoint or 2.0.0, or when a probe reads the cause"; observed, but the cause is unread and a fix needs a census and a move-only Track first |
| `mm disable-source --force` with a control character leaves `config.toml` unparseable | kill: unobserved outside a crafted name; reversible (the error names the file and a hand edit recovers it); reopen if a source name ever arrives from anything but the owner's own argv |
| A source command can traceback after its config write landed | kill: unobserved (read from code); reversible (the write landed, a rerun shows it); reopen on any report of a traceback from a source command |
| `mm sources` shows a raw traceback for a bad source path | kill: unobserved in real use (`~nosuchuser` was crafted); reversible (a crash with a clear error); reopen if the owner hits it |
| `enable-source cursor` installs the hook before writing consent | kill: unobserved; inconsequential (the hook records nothing without consent); reopen at the next Cursor capture change if it manifests |
| Stale comment and help text left by Track 90B | kill: docs nit, no defect; reopen at the next change to either file |
| Two distinct valid key headers | kill: unobserved; the cutover moves one existing store and never runs `--new-fleet`; reopen when a second distinct `mm-crypto-init` copy is seen in a store, or before anyone creates a new fleet on a Syncthing folder |
| A corrupt prior manifest still suggests `mm init` | kill: unobserved wording; reversible; reopen when the message is seen in real use |
| Deflake the load-sensitive tests | defer → Future; the owner undecided; trigger: the owner decides, or a CI run fails from it |
| Tidy `manifest._local_state_parent_prefix` and test its two untested branches | kill: advisory, no defect or outcome; reopen at the next change to the browser-profile marker walk |

**Also checked:** every `git grep -nE 'Track (8|9)[0-9][AB]'` hit outside `docs/ROADMAP.md` and `docs/archive` names a shipped Track (82A to 91A, 94A, 97A), a live card (91B, 92A, 93A, 95A, 96A), or, in this file's dated drain records, a pre-2026-10-19 ID kept as history. The one live pre-2026-10-19 reference, Track 80A in `docs/invariants/init-devices.md`, is already on Track 93A's retirement list.

**Version:** documentation-only regeneration; no bump.

### Roadmap drain — 2026-11-01

Run from manager Ephesus 7's brief on the owner's "go". 14 inbox items: **5 placed, 0 deferred, 9 killed, 0 discharged**. Authored-false rate: 0 / (5 + 0) = 0%. Verification baseline: `2a101d6` (v1.12.1). Tracks 88A (v1.11.18, PR #240), 94A (v1.11.19, PR #242), 89A (v1.11.20, PR #243), 90A (v1.12.0, PR #245) and 90B (v1.12.1, PR #246) were recorded independently in shipped history against their verified completion matrices, with v1.11.15 and v1.11.16 under additional release history; Groups 88, 89, 90 and 94 are tombstoned. Track 97A is pinned in In Progress (factory item 9dd967eb). New Tracks: 98A (bootstrap), 91B (remedy on the newest release). Future stays at 16 items (byte-identical); 6 entries lack an inline revisit trigger, and the trigger migration was offered, not run.

| Inbox item | Disposition |
|---|---|
| A missing `projects/` is silent, and a partial restore still reads as deletion | place → Track 91A re-check list |
| A file deleted on the old form can return on any machine after the first switch | place → Track 91A re-check list |
| An old-form record can still bind a project at the switch | place → Track 91A re-check list |
| Losing `projects/` mid-push publishes false deletion records | place → Track 91A re-check list |
| The post-push GC gate delays the sweep, and `mm gc` has no gate | place → Track 91A re-check list |
| A held machine with mtime drift reads as metadata-only | kill: unobserved; inconsequential (a status wording; the hold line still prints); reopen if it manifests |
| The Git spawn gate cannot see a built argv | kill: unobserved; inconsequential (`projectid._git_origin` passes the scrubbed env by reading, and the count in `events-retro.md` is a doc nit); reopen if it manifests |
| The project index lists `projects/` with no deadline | kill: unobserved; reversible (a hang a rerun or a kill clears); reopen if a command hangs on a network or removable `~/.claude` |
| `sidecar.read` validates less than `load_manifest` | kill: unobserved; reversible (needs a damaged sidecar plus a corrupt own manifest); reopen if `last-push.json` is reported damaged |
| Own-manifest recovery can leave a deletion record under its old spelling | kill: unobserved; reversible (no bytes lost, three rare conditions together); the limit stays in `sync.md`; reopen if deleted memory returns after a recovered push |
| `mm diff` lists a renamed project's files as `+ push` | kill: unobserved; inconsequential; reopen if it misleads the owner |
| Bound the new peer manifest reads in `mm status` and `mm diag` | kill: unobserved; reversible, and the iCloud placeholder cause goes at cutover; reopen if either command stalls on Syncthing |
| Measure the project index on a Mac hook with macOS privacy prompts | kill: unobserved; reversible. `mm diag --json` on this Mac on 2026-11-01 shows `project_index.state: shared`, `timed_out: false`; reopen if `timed_out` is ever true |
| Shared helpers offered by Track 90A review | kill: advisory, no defect or outcome; reopen when one of them is next changed and the duplication costs something |

**Brief backlog** (the manager's list; each claim checked at `2a101d6`):

| Item | Disposition |
|---|---|
| PR #246 receipt: `_toml_value` control characters | file → inbox; reproduced in a temp HOME on v1.12.1 |
| PR #246 receipt: `_record_seen` traceback, `mm sources` bare exceptions, `enable-source cursor` order | file → inbox; `~nosuchuser` reproduced, the rest read from code |
| PR #246 receipt: stale docs (`SPEC.md` `mm sources` steps, `sync.md` config-patch row, "all configured sources", `has_explicit_sources`) | place → Track 93A |
| PR #246 receipt and 90B plan: `cli.py` help text, `tests/conftest.py` comment | file → inbox; the 90B plan's line 569 still holds |
| 90B plan: `_set_grok_host_usage` in `sync.md` (now line 457) | place → Track 93A; no definition in `src/` |
| 90B plan: unread `tests/fixtures/cli_surface_1_x.json` | discharged@`fb0e12d`: PR #226 deleted the file, so the plan's claim was already false at its own base |
| 90A Gate notes: 93A runbook items and the C1 line | place → Track 93A; README's "Retiring a dead machine" already holds the retire steps |
| 90A Gate notes: O3.2's 24 h warning excludes unplaced and old-form counts | place → Track 92A; `sync.md` assigns it there |
| 90A plan: older peer's tombstone can delete once 91A ships | place → Track 91A |
| Plan note: guard for a device whose home changes | place → Track 91A, narrowed: PR #245 closed the push side (`test_a_changed_home_mints_no_tombstone_and_settles`), so 91A proves it through a peer's pull |
| FU1, FU4, FU7 | place → Track 93A (FU1's diag half is already Track 92A's `.stignore` lint) |
| FU3, FU5 | file → inbox |
| VERSION and `pyproject.toml` drift check (the owner, "sure") | place → Track 93A, which already edits `pyproject.toml`; no such check exists today |
| Deflake load-sensitive tests | file → inbox (the owner undecided) |
| `_local_state_parent_prefix` tidy and its two untested branches | file → inbox |
| `mm status` says `mm update` for an unsupported format on the newest release | place → Track 91B; observed. The reader's own failure is filed as a separate P1 item |
| Addendum: a kept conflict is reported as applied on every pull, and existing conflicts do not clear under the no-base fallback | place → Track 91A; observed on one of the owner's Macs by the manager, code path read at `2a101d6`. Decision on the card: the owner runs `mm resolve` on existing ones |

**Also placed from this run's `git grep` of Track 8x and 9x:** Track 91A owes the `state.db` move-aside recovery README assigns it and `tests/test_pullflow.py`'s characterization tests; Track 92A owes the peer manifest and attended waiting count `sync.md` assigns it; Track 93A drops the soak wording at README's Linux section and `ci.yml`. References to shipped Tracks in code, tests and invariant docs are history and stay, and the `init-devices.md` line naming Track 80A goes with Track 93A's retirement of the old IDs there, moved from Track 91A to keep it inside the file cap.

**Version:** documentation-only regeneration; no bump. Quiet output (O8) and complete usage (O7) remain unaccepted, so the next code release stays PATCH unless a checkpoint is accepted.

### Roadmap drain — 2026-10-27

2 inbox items: **0 placed, 0 deferred, 1 killed, 1 discharged**. Authored-false rate: 1 / (0 + 1) = 100%. Verification baseline: `beb4cb0` (v1.11.13). Tracks 82A (v1.11.9, PR #229), 82B (v1.11.10, PR #230), 83A (v1.11.11, PR #231), 84A (v1.11.12, PR #232) and 85A (v1.11.13, PR #233) were recorded independently in shipped history against their verified plan matrices (23/23, AC1–AC6, 10/10, 35/35, M1–M14); Groups 82–85 are tombstoned and Phase 4 is complete. The eight leftover Tracks keep their numbers (86A–93A): the first free Group integer after shipped history is 86, and the packer emits the eight serial singleton bins `86A | 87A | 88A | 89A | 90A | 91A | 92A | 93A`. Group-level dependencies were regenerated from those bins (86 now depends on none; 87 on 86 only); satisfied `_blocked-by:` edges keep their canonical shipped IDs. Premises for all eight remaining Tracks were re-verified against `beb4cb0` (`_pull_core`/`_download_and_apply`/`_apply_incoming_file`/`init` still in `cli.py`; no `pullflow.py`, `initflow.py` or `syncthing.py`; no per-peer base table in `statedb.py`; the iCloud default still in `config.py`). Future stays at 17 items (byte-identical); 7 entries still lack an inline revisit trigger and the trigger migration was offered, not run. No active pins were declared; Track 82A's pin is released by its shipment.

| Inbox item | Disposition |
|---|---|
| [plan-eng-review:track=84A] Settle Track 85A's Codex settled-day approach under core-and-extensions | discharge — discharged@`beb4cb0`: Track 85A (PR #233) implements `files`-backed `settled_days` plus ≥24h stat-only cache hits in `hosts/codex.py` exactly as the item's own resolution note (autoplan-lean `d3`, `d8`) prescribed, leaving Muse's freeze code in `host_usage.py` untouched; `settled_days` is live in `src/mind_meld/hosts/codex.py` at HEAD |
| [plan-eng-review:track=85A] Mid-rollout line-offset checkpointing if a single cold Codex rollout ever exceeds capture_budget_ms | kill: unobserved; reversible (a deadline-skipped read leaves totals stale but recomputable and visible in `mm status`/`mm diag`, and later pushes resume per-file); reopen if it manifests — an observed `codex deadline` skip where one rollout's cold parse alone exceeds `[retro] capture_budget_ms` |

**Version:** documentation-only regeneration; no bump. Quiet output (O8) and complete usage (O7) checkpoints remain unaccepted — neither v1.11.9–v1.11.13 claims one — so the next code release stays PATCH under the spec's release policy unless the owner accepts the complete-usage checkpoint on fleet evidence.

### Roadmap drain — 2026-10-23

0 inbox items: **0 placed, 0 deferred, 0 killed, 0 discharged**. Verification baseline: `fb0e12d` (v1.11.8). Out-of-band releases v1.11.6 (PR #224, `5dc484b`, segment-aware `include_patterns` allowlist for gstack sync) and v1.11.7 (PR #225, `02d671f`, attended push/pull terminal progress spinner) are recorded under `## Additional release history` in `docs/roadmap-shipped.md`. Tracks 79A, 80A and 81A shipped together with full Antigravity (`agy`) host parity and OpenCode skill-link reaper removal in v1.11.8 (PR #226, `fb0e12d`) and are recorded independently in `docs/roadmap-shipped.md`; Groups 79–81 are tombstoned. Two doc stragglers from Track 79A (`docs/invariants/auto-upgrade.md`'s remaining `cli_surface_1_x.json` release-tripwire bullet and `CHANGELOG.md`'s Track 76A abandoned store/simulator removal note) are recorded in the 79A receipt and cleaned up in a separate docs commit immediately after this regeneration. The eight leftover Tracks keep their numbers (82A–89A): the first free Group integer after shipped history is 82, and the packer emits the eight serial singleton bins `82A | 83A | 84A | 85A | 86A | 87A | 88A | 89A`. Premises for all eight remaining Tracks were re-verified against `fb0e12d`. Future stays at 17 items (byte-identical). No active pins were declared.

**Version:** documentation-only regeneration; no bump. Quiet output (O8) and complete usage (O7) checkpoints remain unaccepted, so the next code release stays PATCH under the spec's release policy.

### Roadmap drain — 2026-10-21

5 inbox items: **3 placed, 0 deferred, 2 killed, 0 discharged**. Authored-false rate: 0 / (3 + 0) = 0%. Verification baseline: `be13eab` (v1.11.5). Tracks 75A (v1.11.3, PR #219), 76A (PR #220, first released in v1.11.4), 77A (v1.11.4, PR #221) and 78A (v1.11.5, PR #222) were recorded independently in shipped history; Groups 75–78 are tombstoned. The eleven leftover Tracks keep their numbers (79A–89A): the packer emits the same eleven serial bins. Track 78A's `; usage published` suffix against SPEC O8.2 is deferred to Future until the quiet-output checkpoint decision; neither v1.11.4 nor v1.11.5 claims that checkpoint. No active pins were declared.

| Inbox item | Disposition |
|---|---|
| [plan-eng-review:track=76A] Repair archived-design pointers at the next roadmap regeneration | place → this regeneration: Tracks 84A–89A now cite `docs/archive/store-v2.md`; naming §13 in docs/SPEC.md's evidence list rides Track 89A |
| [plan-eng-review:track=76A] Mention the abandoned code removal in the next release | place → Track 79A's CHANGELOG entry (v1.11.4 and v1.11.5 shipped without the line; verified at `be13eab`) |
| [review:track=76A] Retire pre-2026-10-19 Track IDs from the state.db invariants and their siblings | place → Track 87A (the obligation-binding half landed in PR #220; the reader-or-no-reader rewrite rides the first non-history `state.db` writer) |
| [plan-eng-review:track=75A] Name held and refusing files in status and escalate a file held on every push | kill: held outcomes already persist count-level in Track 77A's run record; escalation and refusing-path persistence are unobserved publication-status precision, refused by the standing constraint; reversible — reopen if the same held count repeats with no way to name the file |
| [discovered:track=75A] Marker discovery still refuses when a listed entry vanishes before its stat | kill: unobserved; reversible (one refused push, rerun succeeds); reopen if a push refuses with `disappeared while being read` on a non-source path |

**Version:** documentation-only regeneration; no bump. Quiet output (O8) and complete usage (O7) checkpoints remain unaccepted, so the next code release stays PATCH under the spec's release policy.

### Roadmap drain — 2026-10-19

Composed with the `/project-spec` rewrite of 2026-10-19. Owner decisions: 2.0 carries the 1.x store onto Syncthing instead of building store-v2; fleet usage reporting is a co-equal product ("keep and invest"); standalone Grok Build support is removed; Muse gets its own family; the 1.x compatibility contract is retired; command output follows SPEC O8; S13, Track 70A's item 14B and the O6 acceptance record are dropped.

2 inbox items: **1 placed, 1 deferred, 0 killed, 0 discharged**. Authored-false rate: 0 / (1 + 0) = 0%. Verification baseline: `408766d` (v1.11.2). Track 74A (v1.11.2, PR #217) was recorded independently in shipped history and Group 74 is tombstoned. No active pins were declared.

| Inbox item | Disposition |
|---|---|
| [plan-eng-review:track=74A] Guard raw unlinks outside the 74A inventory | defer → Future (its own trigger: an observed test-run deletion or near-miss, or a new storage writer adopting the pattern) |
| [roadmap:track=74A] Narrow the 74A and 71B safety claims and carry the guard obligation to 75A | place → the 74A receipt records the narrowed claim; the guard obligation moves to the new writers in Tracks 85A (`.stignore`) and 87A (deletion recovery copies), since the store-v2 publish Track it named is killed |

**Leftover plan** (the 2026-10-18 Tracks, all built on store-v2): 75A publish, 76A snapshots and restore safety, 77A apply cursors, 78A causal classification and 82A delete the 1.x store are killed by the spec rewrite. Their still-wanted outcomes are re-carded on the 1.x store: "waiting, not failed" in 84A, fast-forward from a last-synced base in 87A, iCloud removal in 85A. 79A's portable Claude paths become 86A (its store-v2 previews fall with the store). 80A's create and join becomes 85A on the 1.x key header. 81A's read-only Syncthing status becomes 88A; its `mm devices retire` is killed (the cutover runbook removes a dead machine by hand). 83A's documentation rewrite becomes 89A, with the store invariants in 76A and the compatibility section in 79A. Those IDs are recycled for the new plan: old and new 75A–83A are unrelated. Tracks with no prior card: 75A (manifested refused push), 77A and 78A (O8), 79A to 81A (owner decisions) and 82A and 83A (O7.2, manifested capture gaps).

**Future:** 28 → 16. Killed 12:
- Grok publishes its own billed cost: Grok Build reader removed.
- Shared host-reader resume primitives: internal refactor with no outcome; Track 83A restructures the Codex reader.
- Claude and Codex memory parity qualification, live memory qualification, memory onboarding clock: qualification chain dropped with the owner's obligations decision; Codex memory sync stays deferred and decides its own gates when promoted.
- S13, Track 70A's 14B, O6 acceptance record: dropped by the owner.
- 2.1 conflict policies and the 2.2 resolver: designed on store-v2's version vectors; replaced by one new bullet.
- Fake Syncthing transport conformance: the simulator is deleted (Track 76A).
- Active-membership coverage before retire: `mm devices retire` is no longer planned.

Promoted 2: the interactive `mm pull` exit code → Track 78A (its exit-0 decision rested on the retired contract); the `muse` host family → Track 79A. Added 2: per-path conflict policies (replacing the 2.1/2.2 bullets) and the deferred inbox item. Two kept bullets were edited during reassessment: "mm-managed Syncthing" now points at Track 88A instead of the killed 81A, and "Share project memories between Claude and Codex" gained a trigger because the parity gate it named was killed. Every other bullet is byte-identical.

**Documents:** docs/SPEC.md rewritten; store-v2.md superseded in place (tests/sim reads it until Track 76A archives it); conflict-policies.md and self-update-qualification.md moved to docs/archive/ (the latter with a redirect for frozen shipped history); notices in host-parity.md and the auto-upgrade "Compatibility (1.x)" section; README's versioning paragraph and AGENTS.md's project, output and detail-map lines updated.

**Version:** documentation-only regeneration; no bump.

### Roadmap drain — 2026-10-18

Composed with the `/project-spec` amendment of 2026-10-18 (owner-approved trims: Syncthing stays owner-managed, inactivity-only retirement, newest-wins for non-UTF-8 union input, deep simulator run once on the release candidate, no further restore modeling, Future items without an explicit revisit trigger killed).

25 inbox items: **10 placed, 0 deferred, 13 killed, 2 discharged**. Authored-false rate: 2 / (10 + 2) = 17% (both discharges are gap-recording items whose rows already exist). Verification baseline: `223f1b8` (v1.11.1). Tracks 73A (v1.11.0, PR #213), 73B (v1.10.0, PR #214) and 73C (v1.11.1, PR #215) were recorded independently in shipped history, with v1.9.1 (PR #212) under additional release history; Group 73 is tombstoned. Leftover plan: the retirement-design Track (74B) was killed by the amendment, with its non-UTF-8 decision moved to 78A and its coverage question deferred to Future; the Syncthing Track (82A) was split, its read-only half folded into 81A and its managed half deferred. Future 53 → 28 (27 killed: 26 with no explicit revisit trigger, some of which named only a demand condition, and the scheduled deep run, superseded by the amended O1.1; 2 added). No active pins were declared.

| Inbox item | Disposition |
|---|---|
| [plan-eng-review:track=73C] Model Syncthing 2.1 counter dominance for restored-index rewrites | kill: restore modeling refused by the 2026-10-18 spec amendment; native conformance stays in Future ("Check the fake Syncthing transport against a real Syncthing") |
| [plan-eng-review:track=73C] Keep the store folder and Syncthing's database out of backups automatically | place → Track 80A (exclusion at init and join, Linux guidance) and Track 81A (diag reports it) |
| [plan-eng-review:track=73C] Implement restored-device detection in the product (design and model delivered by 73C) | place → Track 76A |
| [plan-eng-review:track=73C] Add a restore drill to the soak and cutover runbook | kill: restore drills are a spec non-goal since 2026-10-18 (revisit when a restore is observed on the fleet) |
| [plan-eng-review:track=73C] Relay versions lost by a current-policy rollback | kill: relays are a spec non-goal since 2026-10-18; the residual stays owner-accepted in `KNOWN_DESIGN_GAPS["restore-current"]` |
| [plan-eng-review:track=73C] Run a native Syncthing restore experiment on the soak | kill: native restore experiments are a spec non-goal since 2026-10-18 |
| [plan-eng-review:track=73C] Bind the record family into the associated data with the version byte | place → Track 80A (wire decisions before the first fleet.json) |
| [plan-eng-review:track=73C] Amend later store cards for the 73C design rules | place → Tracks 75A (with the 73B schema items, since 73B shipped), 76A, 77A, 78A, 80A and 81A; the Syncthing Track's (then 82A) diag item moved to 81A; the retire coverage tie-break fell with the coverage rule |
| [plan-eng-review:track=73C] Current-policy restore residuals stay pinned (`restore-current` row) | discharged@223f1b8: `tests/sim/invariants.py` `KNOWN_DESIGN_GAPS["restore-current"]` carries the owner's uc2a disposition; backup exclusion lands in Track 80A |
| [review:track=73C] Reader and reporting refinements deferred from the 73C review | kill: unobserved model findings; reversible (a warning or a boundary, no bytes lost per the item); reopen if it manifests when 77A implements the reader |
| [review:track=73C] Fresh-replica restore can empty the only copy of an unreplicated own record (`fresh-replica-unreplicated` row) | discharged@223f1b8: `KNOWN_DESIGN_GAPS["fresh-replica-unreplicated"]` carries the owner's 2026-10-16 disposition; the item existed to satisfy the triage rule |
| [review:track=73C] Simulator hardening and cost items from the 73C review | kill: simulator evidence is sized to the owner's fleet (spec, 2026-10-18); model-only defects, unobserved; reopen per item if one misleads a store Track |
| [manual] Bound the non-interactive keyring read with a timeout backstop | kill: unobserved; reversible (a hook stall on an unsupported keyring); reopen if it manifests |
| [manual] Omarchy qualification checkpoint before Track 80A (for /roadmap) | place → Track 80A Done-when (trigger met: the owner's Linux machine exists, 2026-10-18) |
| [manual] Proposed owner acceptance for Track 73A at the 2.0 soak — PENDING owner confirmation | kill: never confirmed by the owner; the O1.2 soak record covers any manual keyring step on the Linux machine |
| [manual] Owner for store-v2 section 11 prose outside pyproject.toml (for /roadmap) | place → Track 83A |
| [manual] Attended commands that agents run can block on a locked Linux keyring | kill: unobserved; reversible; reopen if observed on the soak machine |
| [manual] Track 80A owns the 2.0 default storage path ~/Sync/mind-meld (for /roadmap, UC1) | place → Track 80A |
| [manual] Track 84A owns pyproject.toml description, keywords and classifiers (for /roadmap, UC2) | place → Track 83A (formerly numbered 84A) |
| [manual] Audit Python 3.14 Path.exists / is_dir / is_file callers for swallowed errors | kill: unobserved; reversible (2.0 deletions keep bytes in `displaced` for 30 days, and the strict walkers already use explicit stat calls per the item); reopen if it manifests |
| [manual] macOS hook with a locked Keychain and no UI reports no-passphrase, not keyring-locked | kill: unobserved; inconsequential (remedy wording); reopen if an SSH or launchd hook records it |
| [plan-ceo-review:track=73B] Report state.db health in `mm diag` | kill: unobserved; forensic; reopen if `mm log` history goes missing |
| [plan-ceo-review:track=73B] 75A card note: INV7 equivalence and the authoritative-state remedy | place → Track 75A (INV7 adapter cases; preserve-and-refuse remedy before the first ledger row; receipt tables) |
| [plan-ceo-review:track=73B] Remove `fsutil.flock_append_jsonl(on_locked=)` | place → Track 74A (revisit trigger was 74A) |
| [review:track=73B] Import legacy history only under the mm lock | kill: unobserved; forensic only per the item; reopen on a stale-binary notice with a clean `which -a mm` |

### Roadmap drain — 2026-10-15

16 inbox items: **14 placed, 2 deferred, 0 killed, 0 discharged**. Authored-false rate: 0 / (14 + 0) = 0%. Verification baseline: `573fb12` (v1.9.0). Tracks 71A (v1.7.2, PR #205), 71C (v1.7.3, PR #203), 71D (PR #206), 72A (v1.8.1, PR #208) and 72B (v1.8.0, PR #207) were recorded independently in shipped history, with v1.8.2 (PR #209) and v1.9.0 (Muse, PR #210) under additional release history. Track 71B (PR #204, v1.7.1) is **not** recorded: its receipt says "Track 71B not accepted" and the owner deferred C1 to a follow-up card, so its remainder continues as Track 74A. Groups 71 and 72 are tombstoned. No active pins were declared.

| Inbox item | Disposition |
|---|---|
| [review] Direct test writers can still touch real account state | place → Track 74A (formerly 71B; manifested 2026-10-08 through `_log_unexpected`) |
| 75A publish and presence must replace model pieces and supply the state seam | place → Track 75A (presence writes move from 81A) |
| 76A must replace snapshot, compaction and own-storage reconciliation | place → Track 76A |
| 77A must replace the peer reader behind observable injected entry points | place → Track 77A |
| 78A classification and resolution must honor the shared publication contract | place → Track 78A |
| 80A fleet creation, join and rejoin must replace model registry entries | place → Track 80A |
| 81A retire and delivery status must preserve simulator ownership boundaries | place → Track 81A |
| 2.0.0 acceptance requires no model pieces and explicit gap disposition | place → ROADMAP Phase 4 note on the 2.0.0 cut (acceptance condition, not a Track) |
| 81A must define active membership before retirement under delayed presence | place → Track 74B (design and model), implemented by Track 81A |
| Decide saved-policy restore fork recovery without losing a recoverable edit | place → Track 73C (design and model), implemented by Track 76A |
| Decide current-policy restore recovery after fleet-wide deletion and seq reuse | place → Track 73C, implemented by Track 76A |
| Preserve non-UTF-8 union bytes or obtain an explicit outcome decision | place → Track 74B, implemented by Track 78A |
| Specify recovery of a partial exclusively created own commit | place → Track 73C, implemented by Tracks 75A and 76A |
| [manual] Report the largest synced subtrees in 2.0 status/diag | defer → Future |
| [manual] Re-measure the gstack set at cutover step 1 | place → Track 84A (cutover runbook step 1). Method kept here: walk `~/.gstack/{projects,analytics,retros}` with the walker's global EXCLUDED list and current gstack globs, gzip each file at the default level for sealed size, prune nested checkouts; reader-check any kept tree over about 20 MB sealed before adding a glob. Baselines 2026-10-12 on the measured Mac: a defect-candidates runs tree 101.5 MB raw / 22.5 MB sealed; `kbitz-mind-meld/gate-diff` 24.1 MB raw. |
| [manual] Choose a 2.0 mechanism that ends ad hoc gstack glob batches | defer → Future |

**Leftover plan:** former Track 74A ("Preserve coherent state after config publication failures") is killed by owner-approved scope amendment: its defects were reproduced only with injected flush failures, the card itself says registration self-heals, and the device registry, crypto-init backfill and `mm recover --abandon-manifest` it repairs are 1.x code that Track 83A deletes (spec 1.x freeze). Re-verified at `573fb12`: `_register_and_save` still deletes `devices/<id>.json` on any save exception, and crypto backfill still lets `StorageError` through.

**Shipped-PR backlogs** (filed only in PR bodies and receipt comments): PR #205's eight 1.x items (heal hardening, own-GC in autopush, pull conflict-copy cleanup, crypto-init and corrupt-manifest `mm init` remedies, own-manifest diag status, coverage gaps G1 and G2, root SPEC "bytes freed") are killed under the 1.x freeze. PR #203's D3 backlog: the version byte in the AAD, passphrase normalization and an expected `fleet_id` on unlock go to Track 80A; `.stfolder/` and other markers counted as foreign, and `syncthing_id` grammar and freshness go to Track 82A; mixed part-count families and torn multipart recovery go to Track 73C; the AGENTS.md storage-key rule and the routing leaf list go to Track 84A; the rest (fork names, `mtime` bounds, drive-prefix and case collisions, decode peak, bounded reads, `StoreError` hierarchy, decode-once and simplification advisories, compression side channel, coverage gaps, Python 3.11 timestamp verdict) are killed as unobserved and reversible or inconsequential. PR #206: the `tests/sim/README.md` link is discharged@`42f4f57`; the `.stignore` fixture check goes to Track 82A; native Syncthing conformance is deferred to Future; docstring plan IDs, `_busy`/`ready_at`, scan cost, how-to docs and the `_patch_legacy` Muse paths are killed. PR #208: X3 (scheduled deep run) is deferred; V13 (cross-OS Claude path check) goes to Track 79A; row 46 (isolated `tests/sim` time) goes to Track 75A. PR #207: O6 acceptance is held in Future as REQUIRED / PENDING; root SPEC's gstack default list goes to Track 84A; the `PYTEST_ADDOPTS` leak into the nested simulator runner (seen twice, in pregate diagnostics only) is killed. PR #210's (a)–(h) are killed as unobserved and reversible (no standing Muse blocker, no observed pruning or duplicate envelope id); Linux Muse paths fall under O7 acceptance on the soak.

**Future membership:** 94 → 53. The manifestation gate killed 39 unobserved defect claims, the 1.x freeze killed 6 (`_resolve_interactive_loop`, two-machine test bootstrap, abort transactionality, machine-readable diag conflicts, pull-history failure reasons, `_open_crypto_session`), and "README prerequisites and a timed two-Mac preview/apply walkthrough" is discharged@`2ddaa39` (README prerequisites line; the walkthrough exists; the timing remainder is iCloud onboarding that 2.0 replaces). Five appended: O6 acceptance, native Syncthing conformance, the scheduled deep run, and the two deferred inbox items. "Overdue counterless-FINISHED tracking and an SQLite authorizer" is kept intact: its authorizer half is privacy hardening. Every other bullet is byte-identical. The pointer count was also stale (93 for 94 bullets after PR #210).

**Version:** documentation-only regeneration; no bump.

### Roadmap drain — 2026-10-11

8 inbox items: **4 placed, 4 deferred, 0 killed, 0 discharged**. Authored-false rate: 0 / (4 + 0) = 0%. Verification baseline: `a8afa88` (v1.7.0). This regeneration composed with `/project-spec`: docs/SPEC.md now selects MVP-1 on the 2.0 Syncthing store for macOS and Arch-based Linux, 1.x is frozen at cutover, and the two store designs supply 17 new Tracks. Tracks 70A (v1.7.0, PR #201) and 70B (v1.5.2, PR #199) were recorded independently in shipped history; 70A's two-Mac acceptance (14B) stays REQUIRED / PENDING in Future, not waived. Former Track 71A was re-verified at HEAD and renamed 74A. No active pins were declared.

| Inbox item | Disposition |
|---|---|
| Check Track 71A's invariant documentation routing | place → Track 74A read-first (`docs/invariants/README.md`) |
| Read Conductor's tmpdir/env-override Cursor stores if one is ever observed | defer → Future |
| Switch every reader's permanent-reason remedy to `mm update` | place → Track 71A |
| Per-store failure isolation for malformed or io_error Cursor stores | defer → Future |
| Structural guard: integration tests must never write real `~/.config/mind-meld` state | place → Track 71B (manifested 2026-10-08; still visible in `mm status` on 889e42c0 on 2026-10-11) |
| Overdue counterless-FINISHED tracking and an SQLite authorizer | defer → Future |
| Re-check Cursor ACP (Paseo) capture after each cursor-agent upgrade | defer → Future |
| [plan-devex-review] Link the mm lock-contention error to a README troubleshooting anchor | place → Track 71A (the unknown `plan-devex-review` source tag is drained with it) |

**Future membership against the new target:** 100 → 93. Twelve bullets killed because the 1.x store, mtime-ordered apply and conflict sidecars are retired at 2.0 by the spec decision of 2026-10-11 (Future-clamped peer mtime; `_promote_target_will_sync` exclude miss; mtime-skipped peers; parallel blob fetch; blob-directory peer recovery; devices-write-lock coupling; machine-readable GC outcomes; pull-history `sidecar=` plumbing; conflict GC retention age; stale conflict-copy cleanup on unchanged pulls; historical blob integrity inventory; and "Three-way merge base", absorbed by the 2.1 policies deferral). Two promoted into Tracks: "Deregister/prune retired devices" → Track 81A (`mm devices retire`); "Push `uploaded` rows crowd pull outcomes" → Track 73B (the `history` table). Seven appended: 2.1 policies, 2.2 resolver, 70A's 14B acceptance, and the four deferred inbox items. Every other bullet is byte-identical. "Nothing wires `mm autopush` on the maintainer fleet" was re-observed on device 889e42c0 on 2026-10-11 (no Claude, Codex, launchd or cron hook); its text is unchanged.

**Documents:** root `SPEC.md` keeps the 1.x architecture with an in-place authority notice (tests and code cite its path); `docs/designs/mind-meld-v1.md` moved to `docs/archive/` with a superseded notice; AGENTS.md gained the product-scope bridge. The audit's archive advisories for the live design docs are overridden as before: they cite external host versions or carry live obligations. Documentation-only regeneration needs no version bump.


### Roadmap drain — 2026-10-06

8 inbox items: **5 placed, 3 deferred, 0 killed, 0 discharged**. Authored-false rate: 0 / (5 + 0) = 0%. Verification baseline: `b70f70d`. Tracks 69A/69B discharged independently against approved scope and attributable merged receipts; 69A's pending fallback closes its guide deliverable, while S13 remains REQUIRED / PENDING, 0/3 in Future. Two new Groups contain three Tracks. No active pins declared. All 96 prior Future bullets remain byte-identical; the three deferred inbox entries and required S13 execution follow-up make 100.

| Inbox item | Disposition |
|---|---|
| Replace unsafe lock-removal contention advice | place → Track 70B |
| Clarify broad pinned-install invariant wording | place → Track 70B |
| Read Conductor 0.90.1's SQLite Cursor run store | place → Track 70A |
| Price Cursor models that carry bracketed parameters | defer → Future |
| Init deletes device registration after a published config save fails | place → Track 71A |
| Two handlers catch OSError for helper writes that raise StorageError | place → Track 71A |
| A paused terminal pauses a streaming mm update | defer → Future |
| Teach the mm update progress parser uv's wording | defer → Future |

Current source confirms the five placed defects/gaps. SQLite source demand is the live observation and explicit follow-up decision in PR #192; no remaining SQLite stores were found locally on 2026-10-06, so fresh real-corpus/live qualification remains an implementation gate. Deferred pricing and terminal concurrency preserve the user's decisions; uv progress awaits a real backend transcript/use. Existing Future triggers were reviewed against the index and changes since `e9e56db`; no other promotion or completion was established. The inherited memory evidence remains NO QUALIFIED ROUTE, native 0/48 UNSTARTED/INCONCLUSIVE.

Both former Track IDs remain frozen in independent history, Group 69 is retired, and new work starts at 70. No existing unfinished ID is renamed. The same-file sequence is SQLite reader before durable CLI repair. The archive suggestions refer to host versions, historical baselines or documents with live obligations; none is moved. PROGRESS covers every tagged/changelog version. Documentation-only regeneration needs no version bump.


### Roadmap drain — 2026-10-03

One inbox item: **1 placed, 0 deferred, 0 killed, 0 discharged**. Authored-false rate for the inbox: 0 / (1 + 0) = **0%**. Verification baseline: `a3c5050` (v1.4.0); this regeneration changes documentation only.

| Inbox item | Disposition / destination | Evidence |
|---|---|---|
| [review:severity=informational,files=src/mind_meld/fsutil.py] atomic_write_bytes docstring promises an untouched target after a post-rename failure | place → Track 69B | fsutil.atomic_write_bytes replaces before parent fsync; the existing tests/test_fsutil.py::TestAtomicWriteBytes.test_parent_fsync_failure_is_fatal and Track 68A C2 exercise published bytes followed by an error. The docstring still promises the old target survives every failure. Audit consumers before filing a separate demonstrated caller defect. |

**Shipped reconciliation:** Track 68A, PR #190 / `a3c5050`, is archived under its accepted tests-only no-route scope and the attributable 88-row receipt (87 verified, A39 user-deferred by D8). D7 model limits and Q1–Q18 remain promotion gates. PR #189 / `3642f6f` delivers v1.3.0 self-update with no declared Track ID; its required, unpassed S13 post-release testing is placed in Track 69A. No active pins were declared; Group 68 is retired/reserved. No former live Track is renamed to either new card.

**Future membership:** 94 existing bullets retained byte-for-byte; “Explicit upgrade check” and “Subprocess pipx upgrade” are discharged@3642f6f because mm update forces discovery and update_or_nudge runs pipx. The obsolete “cli.py micro-cleanups” work order is **killed**, not discharged: `_resolve_mm_events_dir` has no current definition anywhere in src/tests, and the remaining “status-enum follow-up” names no target or acceptance; reopen only with a current symbol and concrete benefit. Its former import/_empty_outcomes portions were already assigned to historical Track 18A. Two scoped follow-ups are appended: complete live memory qualification with its existing prerequisites/owners, and D8 cold-reader onboarding timing. **97 → 96**. No transport, parity, bridge or Cursor gate is promoted by the no-route receipt.


### Roadmap drain — 2026-09-30

7 inbox items: **1 placed, 6 deferred, 0 killed, 0 discharged**. Inbox authored-false rate: 0 / (1 + 0) = **0%**. Verification baseline: `c43b777` (v1.2.0), even with `origin/main`. The five memory work packages remain linked to [the formal plan](designs/memory-continuity.md); qualification is executable now, and the remaining four have explicit promotion gates. Forgetting F1-F4, echo prevention E1-E4 and hostile-content checks H1-H4 are required during prototyping and initial implementation, not deferred cleanup.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Qualify Codex memory portability and recall | place → [Track 68A](ROADMAP.md#track-68a-qualify-codex-memory-portability-and-recall) | Codex memories are absent from DEFAULT_SOURCES; native export/import and fresh-session recall remain unqualified. |
| 2 | Deliver Codex memory sync across Macs | defer → [Future](roadmap-future.md#codex-memory-sync) | Promote after Track 68A establishes a supported export/recall contract, retirement and ancestry semantics, and concrete implementation footprints; size the production work from that evidence. |
| 3 | Qualify Claude and Codex memory parity across the fleet | defer → Future; the memory-fleet-parity bullet was removed on 2026-10-19 when that qualification was killed | Promote after the Codex transport/recall implementation passes its deterministic gates; requires live two-Mac access and the same scenarios for Claude and Codex. |
| 4 | Share project memories between Claude and Codex | defer → [Future](roadmap-future.md#cross-agent-memory) | Promote after the same-agent fleet parity gate passes; keep both cross-agent directions and the forgetting/echo gates in the initial bridge. |
| 5 | Establish Cursor's memory capability and portability contract | defer → [Future](roadmap-future.md#cursor-memory-contract) | Promote discovery after Track 68A supplies the reusable adapter requirements; implementation follows Codex parity and the qualified shared-view contract. Cursor discovery does not block Claude/Codex sharing. |
| 6 | Price Cursor Grok Fast after a real census | defer → [Future](roadmap-future.md#cursor-fast-pricing) | Keep the real observed Fast-run census trigger. The committed Cursor CONTRACT.md still records fast=true as unobserved; this regeneration establishes no newer census and adds no pricing. |
| 7 | Revisit Cursor auto/composer classification only after observed use | defer → [Future](roadmap-future.md#cursor-auto-classification) | Keep the real observed auto/composer-run trigger and explicit compatibility decision. The committed Cursor CONTRACT.md records neither in the initial corpus; usage capture alone does not authorize customization sync. |

**Former active plan:** 66A's alias removal and 1.0 cut are `discharged@2ddaa39`; the compatibility refusal behavior shipped beyond the old card. Its cache-jitter task is **killed**, not discharged as a fix: the implementation's live probe did not reproduce the premise for reader 0 (zero writes in 39 steady failed passes; the later-reader allowance mechanism is documented but unobserved; `docs/invariants/events-retro.md`, Track 66A probe). Group 66 moves to shipped history.

**Additional shipped work:** `c76f41d` (v1.1.0 unified agent reporting) and `c43b777` (v1.2.0 Cursor usage) both called themselves 67A. Group 67 records both by version and title, without rewriting historical references or pretending two PRs were one Track session. No ID renames; 68A is new.

**Future membership:** 91 existing bullets retained verbatim, 2 discharged, 6 appended: **93 → 97**. “Stable note codes for all 19 `## Notes` strings” and “Retro-card machine/cause diagnostics” are `discharged@c76f41d`: `aggregator._render_health_block` emits coded `MM_HEALTH` issues with machine/cause details and remedies, `retro_fleet/SKILL.md` consumes them, and `tests/test_docs_routing.py` checks the interface. The latter had no more specific residual requirement to carry forward. All other deferred entries retain their existing text and triggers; none is promoted on an unmeasured premise.

**Advisory override:** keep `host-parity.md`, `memory-continuity.md` and `sync-gstack-context.md` in place. The archive heuristic mistakes external host versions or historical baseline references for completed mm plans; these remain active source documents. PROGRESS covers every tagged/changelog version. Documentation only; no version bump.

### Roadmap drain — 2026-09-21

10 inbox items from the Track 63A–65A /autoplan reviews and the PR #181 /ship run, drained under the user's cue "as few Tracks as possible, ideally one, or kill": **2 placed, 4 deferred, 1 killed, 3 discharged**. Authored-false rate: 3 / (2 + 3) = 60%; all three discharges are roadmap-reconcile items the shipping Tracks filed against themselves, not stale observations. Verification baseline: `cdffc34` (v0.14.18). Ground truth closed all three Current Plan Tracks first: 63A (v0.14.16 `77338bd`), 64A (v0.14.17 `a12096c`), 65A (v0.14.18 `cdffc34`); Groups 63–65 are appended to `docs/roadmap-shipped.md`. The regenerated plan is one Group with one Track, and that Track is the v1.0.0 cut.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Standing constraint: publication-evidence precision needs a user-observed wrong status | place → ROADMAP.md standing constraint | A rule that refuses Tracks belongs in the header, not in a Track. |
| 2 | Discharge "Say on the wire whether a host capture was requested" when 65A's T5 ships | discharged@cdffc34 | `attemptlog.py` exists; `cli.py:6236` / `:6264` render the attempt from the local record. The Future bullet is deleted. |
| 3 | Re-card shipped 63A/64A and 65A in one /roadmap run | discharged@cdffc34 | This run. |
| 4 | Autopush read-budget predictor | kill | Its own context rejects the only mechanism it could name (a count-derived budget), and the observation it wants already exists: `mm diag` prints the sweep estimate (`_host_read_sweep_line`) and the standing `deadline` blocker with `last_complete_ms` and `last_deadline_allotted_ms`. |
| 5 | Sealed or incremental lineage tier | defer → docs/roadmap-future.md | Real design, real prerequisite; measured 2.4x / 2x headroom against its own trigger. |
| 6 | Codex `token_usage_record` drift tripwire | defer → docs/roadmap-future.md | Concrete form of the 46B drift trigger; the reader reads every rollout in the census today, and a drift would show on status as `completed, no usage in snapshot`. |
| 7 | Host cache encoding trigger restated and over-cap notice | defer (datum merged into the existing bullet); `too_large` notice killed | Same topic as the 2026-09-05 bullet; the notice is forward defence for a condition 16x away. |
| 8 | Per-reader carry-forward in the fleet aggregator | defer → docs/roadmap-future.md | v0.14.16 chose device-wide replacement on purpose; 65A's `empty_sources` is the data a change would need; trigger stated. |
| 9 | Track 63A's ROADMAP.md card was hand-edited instead of drained by /roadmap | discharged@cdffc34 | This run is the formal re-card; the shipped entry supersedes the hand-edited card. |
| 10 | host_usage.py: last_deadline_allotted_ms jitter can defeat the failed-pass write-skip | place → Track 66A | Premise verified by reading `_carry_read_timing` and `_skip_failed_cache_write` at cdffc34: two independent monotonic reads, whole-map equality. |

**Leftover Future bullets discharged by v0.14.16–v0.14.18** (deleted from `docs/roadmap-future.md`): "Say on the wire whether a host capture was requested" (discharged@cdffc34, above); "Re-read standing host-usage blockers on an interactive no-op push (T3-B)" (discharged@a12096c — every attended push captures, `--capture-usage` removed); "`warm_host_cache_inline`'s docstring cites a stale measurement" (discharged@77338bd — `grep 573 host_usage.py` returns 0 hits; the docstring cites the 2026-09-17 qualification).

**Former active plan:** all three 2026-09-17 IDs shipped under their own numbers; nothing recycled, no renames table. Group 66 is the first free number after shipped history.

| 2026-09-17 ID | Title | 2026-09-21 disposition |
|---|---|---|
| 63A | Keep the warm Codex read inside its budget | shipped as 63A (v0.14.16) |
| 64A | A usage refresh is not a push, and exit 4 says what it skipped | shipped as 64A (v0.14.17); re-scoped at review — `--capture-usage` removed, every attended push captures |
| 65A | Reader-scoped publication evidence on status and diag | shipped as 65A (v0.14.18), with a fifth task (the attended-attempt record) added at the gate |

New Track: 66A (Cut v1.0.0: retire the pre-1.0 conflict alias and converge the failed-read cache write). Future: 93 → 93 (three discharged, three appended, one amended). Shipped history is append-only. Not acted on: the audit's archive finding for `docs/designs/sync-gstack-context.md` (a live design doc cited from CLAUDE.md and AGENTS.md), same call as 2026-09-17. Version recommendation when 66A ships: MAJOR (1.0.0).


### Roadmap drain — 2026-09-17

14 inbox items from the Track 58A–62A /autoplan reviews and the PR #176–#178 /ship runs: **4 placed, 8 deferred, 1 killed, 1 discharged**. Authored-false rate: 1 / (4 + 1) = 20%, and the one discharge is the roadmap reconcile this run performs rather than a stale observation. Verification baseline: `66b5293` (v0.14.15). Ground truth closed five Tracks first: 58A (v0.14.12 `dfc7a09`), 59A and 60A (v0.14.13 `e7eb301`, one PR by user decision), 61A (v0.14.14 `5079b3f`), 62A (v0.14.15 `66b5293`); Groups 58–62 are appended to `docs/roadmap-shipped.md` and Phase 3 (Retro fidelity) is complete.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | pull-history rotation is dominated by push `uploaded` rows | defer → docs/roadmap-future.md | `pullhistory._ROTATE_BYTES = 1_000_000`; a logging-contract change needs a policy first. |
| 2 | Preview/apply parity tests for `gc --dry-run` and `recapture --dry-run` | defer → docs/roadmap-future.md | No parity or twin-state test in `tests/test_retention.py` or `tests/test_track_30a.py`; tests only, P3. |
| 3 | Prerequisites and a timed two-Mac preview/apply walkthrough in README | defer → docs/roadmap-future.md | Single-operator audience; time it at the next real onboarding. |
| 4 | Distinguish explicitly requested host captures on the wire | defer → docs/roadmap-future.md | No reader named; the one candidate is status's "Latest attempt: unknown (no attempt receipt)" line. |
| 5 | Host read-budget override | place → Track 63A | Its own instruction was to extend the Future proposal, and that proposal's trigger fired (below). |
| 6 | Audit forensic append callers for silent-write assumptions | defer → docs/roadmap-future.md, audit performed | `flock_append_jsonl` has two callers; of `write_push_event`'s four call sites only `cli.recapture` makes a success claim on a non-strict write. The bullet carries the finding. |
| 7 | Reconcile docs/ROADMAP.md for Track 59A + 60A | discharged@e7eb301 | This run records Groups 59 and 60 in `docs/roadmap-shipped.md`. |
| 8 | Extract a shared `_open_crypto_session` helper | defer → docs/roadmap-future.md | Six sites at HEAD, not the five filed; take it with the next change to one of them. |
| 9 | Minor redundant path/hash re-reads in mm-events + crypto repair | kill | The filer measured the cost as negligible and bounded by design (crypto-init candidates, home-dir ancestor depth), and one of the two re-reads sits beside a deliberate pre-unlink guard. No trigger can fire. |
| 10 | Disambiguate historical device IDs with shared display prefixes | defer → docs/roadmap-future.md | Display only; `_DEVICE_TABLE_LABEL_WIDTH = 8`; trigger is a real prefix collision. |
| 11 | Price Claude fast mode when it is first used | defer → docs/roadmap-future.md | Human trigger as filed; `usage.speed` was `"standard"` on all 22,042 rows that carried it. |
| 12 | Per-reader "completed, no usage" is whole-row-scoped on the mm status/diag read path | place → Track 65A | `local_host_capture_candidate` still emits `"empty": not row.lifetime_by_family`. |
| 13 | Five smaller mm push --capture-usage rough edges from adversarial review | place → Track 64A (items 1, 2 and the reopened third finding) and Track 65A (items 3, 4, 5) | Every sub-claim re-verified at 66b5293; the third finding's harm is `aggregate_git`'s `snap_total` / `snap_zero`, which exclude only recapture-origin rows. |
| 14 | Investigate a claimed TOCTOU on the accepted-manifest digest check | place → Track 65A | `_iter_typed_objs` already computes a single-pass digest that `recorded_row_revision` discards; using it is cheaper than settling reachability. |

**Promoted from Future on a fired trigger.** "Warm Codex read headroom and the healthy-pass cache rewrite" named "a warm pass above 200 ms, or a corpus above ~900 rollouts". Measured 2026-09-17 on a scratch copy of the cache: 1,052 rollouts, 384 ms warm, `deadline` on 3 of 3 attempts at the 250 ms autopush budget, 116 ms left under the 500 ms interactive budget; 186 ms over 752 rollouts on 2026-09-09. Probes are kept in `~/.gstack/projects/kbitz-mind-meld/63a-probes/`. It is Track 63A, first in priority, and not a Hotfix: interactive pushes still publish Codex and autopush is not wired on the measuring Mac.

**Recovered, never filed.** The PR #179 /ship doc-sync noted that SPEC.md's command reference lacks `mm recapture` and `mm push --capture-usage` (`grep -c` returns 0 at 66b5293). It rides Track 64A, which settles the exit codes that entry must state.

**Former active plan:** IDs below refer to the 2026-09-14 plan. All five shipped under their own numbers, so nothing recycled and there is no renames table.

| 2026-09-14 ID | Title | 2026-09-17 disposition |
|---|---|---|
| 58A | Refresh verified rates, re-pin the Grok census, and make model usage easier to read | shipped as 58A (v0.14.12); census pinned to 1.0.30, not the carded 1.0.25 |
| 59A | Create the mm-events root only from init and a verified real push | shipped as 59A (v0.14.13); the refusal task was reversed at the gate — a missing event file is a deletion |
| 60A | Inspection commands observe storage and the upgrade cache without repairing them | shipped as 60A (v0.14.13), inside 59A's PR |
| 61A | Refresh host usage from an attended push and say what the last push published | shipped as 61A (v0.14.14) |
| 62A | Make the other previews write-free | shipped as 62A (v0.14.15), with the multi-peer pull predictor |

New Tracks: 63A (Codex read headroom), 64A (usage-only capture), 65A (reader-scoped publication evidence). Future: 86 → 93 (one promoted, eight appended). Shipped history is append-only. ROADMAP.md's audit-configuration paragraph now says the caps are machine-local (16 / 6 where it was written, 24 / 5 on the Mac that ran this regeneration). Not acted on: the audit's archive finding for `docs/designs/sync-gstack-context.md` (a live design doc cited from AGENTS.md).


### Roadmap drain — 2026-09-14

19 inbox items from the Track 52A–57A /autoplan reviews: **8 placed, 11 deferred, 0 discharged, 0 killed**. Authored-false rate: 0 / (8 + 0) = 0%. Verification baseline: `a7d9bca` (v0.14.11). Ground truth closed six Tracks first: 52A (v0.14.6 `bb39230`), 53A (v0.14.7 `fe21a57`), 54A (v0.14.8 `735ed02`), 55A (v0.14.9 `d85e504`), 56A (v0.14.10 `dbc926e`), 57A (v0.14.11 `a7d9bca`); Groups 52–57 are appended to `docs/roadmap-shipped.md`. The pricing page was re-read on 2026-09-14 before item 6 was placed.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Record the Grok costUsdTicks census against the "Grok publishes its own billed cost" Future item | defer → Future bullet edited in place | The bullet still said "needs its own census first"; the 303-turn census is now recorded on it. |
| 2 | Re-pin the Grok usage census from 1.0.13 to 1.0.25 | place → Track 58A | `GROK_USAGE_CENSUS_HOST_VERSION = "1.0.13"` in host_usage.py; the contract header agrees. |
| 3 | Tripwire when a Codex request reaches OpenAI's 272K long-context tier | defer → docs/roadmap-future.md | Forward defence; `model_context_window` is not read by host_usage.py (0 hits). |
| 4 | First-class attended usage refresh (`mm push --capture-usage`) | place → Track 61A | `_push_core` returns before the events tail on "Nothing to push"; README sends users to `mm recapture 1d`. |
| 5 | Profile and shrink the Codex host-cache round trip | defer → merged into the retitled "Warm Codex read headroom" bullet | Trigger not yet fired (0.18–0.20 s of 0.25 s); `read_codex_usage` commits `locked.data` on every complete pass. |
| 6 | Refresh Anthropic rates: Sonnet 5 $2/$10, Fable 5.1 cache hits 0.025x | place → Track 58A | `sonnet` tier is `_tier(3.0, 15.0)`; `fable` tier derives a $1.00 cache read; the pricing page confirms both claims and adds that Fable 5 / Mythos 5 keep 0.1x. |
| 7 | Cross-machine dedup of host usage by turn key | defer → docs/roadmap-future.md | New wire content plus an accounting schema; needs its own proposal. |
| 8 | `mm status`: say whether the last push published each host reader | place → Track 61A | status prints "grok prior successful scan"; nothing reads a row's `token_sources` back. |
| 9 | Make the other previews write-free (56B) | place → Track 62A | Five commands call `_get_config()` / `_init_crypto_session` with the defaults; pull's conflict migration and excluded rows have no knob. |
| 10 | mm-events bootstrap masks the missing-published-root refusal on the real push (E4) | place → Track 59A | `resolve_sources(..., bootstrap=not dry_run)` precedes `_refuse_unavailable_selected_sources` in `_push_core`; `get_sources` defaults `bootstrap=True`. |
| 11 | Inspection commands repair shared storage (E7) | place → Track 60A | `status` → `_init_crypto_session` default `read_only=False`; `diag` and init's probe → `fetch_crypto_init(backend)` default `repair=True`; deletion precedes `verify_passphrase`. |
| 12 | `mm status` fetches over the network despite "reads cache only" (E3b) | place → Track 60A | `status` calls `upgrade.check_for_upgrade(config)`, which write-throughs via `locked_json_rmw`. |
| 13 | Re-read standing host-usage blockers on an interactive no-op push (T3-B) | defer → docs/roadmap-future.md | Trigger unchanged; Track 61A gives the attended path. |
| 14 | Autopush warm-read headroom and the healthy-pass rewrite on large Codex corpora | defer → merged into the retitled "Warm Codex read headroom" bullet | Same measurement as item 5; one bullet, one trigger. |
| 15 | Add a `[retro] codex_host_usage` knob | defer → docs/roadmap-future.md | No `codex_host_usage` in config.py; no user need demonstrated. |
| 16 | Complete the deferred plain-stderr display audit beyond direct safe_str calls | defer → merged into the plain-stderr bullet | The item's own instruction; the builders it names exist. The bullet's "55A ships" trigger fired 2026-09-09 and it stays deferred as display quality. |
| 17 | Carry a sanitized failure reason on pull-history `failed` rows | defer → docs/roadmap-future.md | `pullhistory.append` has `sidecar=` and no `detail=`. |
| 18 | Decide whether interactive `mm pull` should exit non-zero when files failed to apply | defer → docs/roadmap-future.md | User kept exit 0 at the 53A gate. Attended pull now exits 1 with `Pull incomplete [result-degraded]:`. |
| 19 | `mm diag` lists repo-local `GIT_*` variables present in mm's own environment | defer → docs/roadmap-future.md | No `scrubbed_git_env` in cli.py (0 hits); deferred by the 55A /autoplan as E6. |

**Former active plan:** IDs below refer to the 2026-09-06 plan. Six of its seven Tracks shipped; the survivor kept its number and absorbed the provenance refresh by user decision (D1 = A, "5 PRs"), so one PR closes Phase 3. The audit's caps were raised for this repo (`roadmap_max_files_per_track=16`, `roadmap_max_session_weight=6`, via gstack-extend `bin/config`) because release-bearing cards here declare 9–14 files and shipped Tracks routinely land 10x their carded size as one PR (53A: 1,790 insertions on a weight-4 card).

| 2026-09-06 ID | Title | 2026-09-14 disposition / ID |
|---|---|---|
| 52A | Enforce a no-control postcondition in the shared sanitizers | shipped as 52A (v0.14.6) |
| 53A | Contain apply exceptions without losing completed-file bookkeeping | shipped as 53A (v0.14.7) |
| 54A | Report failed Codex capture and remove obsolete reader helpers | shipped as 54A (v0.14.8) |
| 55A | Scrub the git environment for mm's git subprocesses | shipped as 55A (v0.14.9) |
| 56A | Make push dry-run setup honor the no-mutation contract | shipped as 56A (v0.14.10) |
| 57A | Price verified Grok usage | shipped as 57A (v0.14.11) |
| 58A | Make model usage easier to read without changing what totals mean | 58A, retitled "Refresh verified rates, re-pin the Grok census, and make model usage easier to read" (premises re-verified; v0.14.11's Grok `>=` rendering added to the example) |

New Tracks: 59A (mm-events root ownership, E4), 60A (inspection without repair, E7 + E3b), 61A (attended host-usage refresh), 62A (write-free previews, 56B). Future: 79 → 86 (three bullets edited in place, seven appended). Shipped history is append-only. Not acted on: the audit's archive advisory for `docs/designs/sync-gstack-context.md` (a live design doc cited from AGENTS.md).


### Roadmap drain — 2026-09-06

5 inbox items, all `[plan-eng-review]` from the Track 48A–51A /autoplan reviews: **3 placed, 2 deferred, 0 discharged, 0 killed**. Authored-false rate: 0 / (3 + 0) = 0%. Verification baseline: `38222ac` (v0.14.5). Ground truth closed four Tracks first: 48A (v0.14.2 `09d98ab`), 49A (v0.14.3 `0c1a969`), 50A (v0.14.4 `cc22b6c`), 51A (v0.14.5 `38222ac`); Groups 48–51 are appended to `docs/roadmap-shipped.md`.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Contain apply exceptions without losing completed-file bookkeeping | place → Track 53A | `_download_and_apply` still calls `_apply_incoming_file` with no exception boundary at 38222ac; the only `finally` stops the progress bar. |
| 2 | Harden existing Rich and multiline sanitizer consumers against nested escapes | place → Track 52A | `strip_terminal_escapes` is one `re.sub` pass; its docstring and the v0.14.4 CHANGELOG record the follow-up. 12 plain-stderr `safe_str` sites exist (not 3 — corrected 2026-09-06 /ship review); the card names 3 in scope and defers the other 9 to roadmap-future.md. |
| 3 | Decide whether unchanged pulls should retry stale conflict-copy cleanup | defer → docs/roadmap-future.md | Policy choice with a pinned test (`test_conflict_copy.py::TestConflictPublishThenCleanup::test_cleanup_failure_then_unchanged_retry_leaves_extras`), not a defect. |
| 4 | Audit historical blob integrity and design verified targeted re-upload | defer → docs/roadmap-future.md | v0.14.3's per-file pull check is the detector and has not fired; the trigger is a `content check failed:` line on any Mac. Inventory and repair designs are recorded on the bullet. |
| 5 | Make push dry-run setup honor the existing no-mutation contract | place → Track 56A | `push` runs `_maybe_prompt_migration` and the fingerprint persist before `_push_core(dry_run=True)`; `resolve_sources` mkdirs mm-events unconditionally. |

**Former active plan:** IDs below refer to the 2026-09-05 plan. Priority order changed: the two new defects outrank the queued Codex-diagnostics and git-environment cards, so those four recycled. Old IDs are dated lineage in each card's `_Source:` line; nothing was blind-remapped, and the 2026-09-05 records below keep their own numbering.

| 2026-09-05 ID | Title | 2026-09-06 disposition / ID |
|---|---|---|
| 48A | Preserve conflict ownership and failed replacements | shipped as 48A (v0.14.2) |
| 49A | Publish complete, content-consistent snapshots | shipped as 49A (v0.14.3) |
| 50A | Sanitize rejected storage filenames | shipped as 50A (v0.14.4) |
| 51A | Keep mixed timestamp types from aborting pull | shipped as 51A (v0.14.5) |
| 52A | Report failed Codex capture and remove obsolete reader helpers | 54A (premises re-verified) |
| 53A | Scrub the git environment for mm's git subprocesses | 55A (premises re-verified; the `test_track_30a.py` assert moved from `:636` to `:678`) |
| 54A | Price verified Grok usage | 57A |
| 55A | Make model usage easier to read without changing what totals mean | 58A |

New Tracks: 52A (sanitizer postcondition), 53A (pull isolation), 56A (dry-run honesty). Future: 76 → 78; one existing bullet's citation moved from "Track 52A" to the title with dated lineage. `docs/invariants/events-retro.md` gained the 2026-09-06 hop on the unified-reporting ID trail. Shipped history is append-only. Not acted on: the audit's archive advisory for `docs/designs/sync-gstack-context.md` (a live design doc cited from AGENTS.md; v0.14.5 merely edited it).

### Approved roadmap refinements — 2026-09-05

Follow-up to the pre-existing-roadmap assessment at `f10bf34`, applied at the user's request. Current Plan keeps eight Tracks and their IDs. Track 53A remains the reproduced git-environment fix. Track 54A makes its requirement to leave unverifiable model aliases unpriced explicit. Track 55A is narrowed to presentation with preserved accounting boundaries; its title changes from “Report every agent the same way” to “Make model usage easier to read without changing what totals mean,” and Group 55 becomes “Usage presentation.” The Phase 3 end-state follows that scope. No schedule or file-footprint changes.

Six Future entries removed: three completed-goal entries discharged and three proposed designs killed. Two retained entries narrowed; 74 others remain verbatim. Future count: **82 → 76**. No new inbox items and no changes to shipped history.

| Removed Future entry | Disposition | Evidence / decision |
|---|---|---|
| Per-verb or sticky autorun breadcrumbs | discharged@01726ba | Per-verb storage shipped in v0.12.45. The push and pull entries are independent. A sticky-error extension is not implied by that completed request; require a demonstrated same-verb visibility problem and an explicit recovery rule before proposing one. |
| Un-hide and rename `--dump-host-usage` | discharged@69f95b2 | Public help and README expose the flag since v0.12.49. Reject the cosmetic rename/alias rather than keeping a completed discoverability request open. |
| Unify the seven per-agent enumerations | killed | Skill installation, sync consent, active readers, wire compatibility and model families are different domains. Their differing membership is required behavior; a universal registry would couple unrelated policies. Consolidate only a demonstrated duplicate within one domain. |
| Doc-lint: no agent-name triple in README prose | killed | Naming the supported agents is useful documentation. A phrase ban would enforce style rather than factual correctness. Keep factual routing and link assertions in the existing documentation checks. |
| `--dump-host-usage` is the subsystem's only forensic tool and is invisible. | discharged@69f95b2 | The public command is visible and documented. Coverage fields are also exposed by the current inventory dump. The reproduced Codex current-failure diagnosis belongs to Track 52A, not another unhide/rename project. |
| An empty host-usage row can overwrite a populated one under strict latest-wins. | killed | A completed empty observation is legitimate state. The proposed nonempty-wins guard would retain stale data after a genuine empty scan or opt-out. Failed capture must not fabricate a complete zero; fix that producer if reproduced. The completed-empty/failed-omission distinction is explicit in docs/invariants/events-retro.md and test_complete_omitted_then_complete_empty_preserves_wire_history. |

The closed unhide items do not authorize a cosmetic flag rename or alias. The closed per-verb item does not authorize sticky errors. Keep any independently reproduced diagnostic defect scoped to its actual consumer; Track 52A already owns Codex's false-ready diagnosis.

**Retained but narrowed:** host-cache GC now covers only current Codex/Grok caches, has no already-shipped Group 32 prerequisite, and needs measured accumulation beyond normal complete-pass pruning. Grok consent remains unresolved: consider usage-only operation through the existing verb first, preserving explicit customization sources; no commitment to three new CLI surfaces.

**Other deferred work:** measured performance gates and the separate reader-quarantine design remain in place. External skill/configuration work keeps its provenance until ownership transfer is recorded; this change does not modify fleet configuration.


### Roadmap drain — 2026-09-05

28 inbox items: **12 placed, 10 deferred, 6 discharged, 0 killed**. The 11 approved full-review findings are all placed. Authored-false rate for the inbox: 6 / (12 + 6) = **33.3%**; this measures already-shipped observations, not rejected ideas. Verification baseline: `2024ff6` (runtime code unchanged from `8be81ce`).

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Intra-file resume for a Grok ledger that exceeds one read budget | defer → docs/roadmap-future.md | The reader still lacks an intra-file checkpoint; the filed >1 GB / three repeated interactive deadline trigger has not been demonstrated. |
| 2 | Host cache encoding trigger (deferred from original Track 46A) | defer → docs/roadmap-future.md | Keep the measured >100 ms or >25 MB gate. The dated filing measured 23.3 ms and 4.11 MB, below both gates. |
| 3 | Per-entry `states` cap on the Codex host cache | defer → docs/roadmap-future.md | No demonstrated pathological entry; preserve totals and cross-file dedup semantics before deciding a cap. |
| 4 | Add the xAI / `grok-4.6-build` price tier | place → Track 54A | Pricing remains absent in resolve_prices; ingestion recovery shipped at 8be81ce. |
| 5 | Ask whether Grok/Codex expose a supported usage surface | defer → docs/roadmap-future.md | Strategic vendor-surface research has no dependency on the immediate correctness repairs. |
| 6 | Reader-agnostic quarantine and drift classification (Track 46B) | defer → docs/roadmap-future.md | Retain the explicit next-drift / unsupported-after-upgrade trigger. The Codex diagnostic defect is placed separately and does not require quarantine. |
| 7 | Stale Track/version literals in code and test comments (15 across 10 files) | defer → docs/roadmap-future.md | Correct remaining historical comments alongside the files being changed; do not create a standalone code PR or carry the old count forward. |
| 8 | `_ensure_inversion_marker` makes every new Mac mis-migrate 100% of its conflict sidecars | discharged@f2624fb → v0.14.0 | Filename birth/era now drives migration; peer mtime remains the peer clock. |
| 9 | README documents the pre-inversion conflict direction, and contradicts itself two paragraphs later | discharged@f2624fb → v0.14.0 | README now says local remains canonical and remote bytes go to the conflict copy. |
| 10 | `_migrate_pre_inversion_conflict` uses `find()` where every sibling parser uses `rindex()` — unbounded `v0-` accretion | discharged@f2624fb → v0.14.0 | _migrate_pre_inversion_conflict now parses the final infix with rindex. |
| 11 | `mm gc --conflicts` can reap a live conflict, prints no paths, and has no preview by default | discharged@f2624fb → v0.14.0 | GC preserves live/recoverable copies, prints deletion paths, and bare --dry-run previews conflicts. Configurable age remains a separate deferred item. |
| 12 | `mm status` and `mm diag` never surface unresolved conflicts | defer → docs/roadmap-future.md | Status coverage shipped; defer the residual machine-readable diag conflict inventory until a consumer needs it. |
| 13 | `pullhistory.append_entry` has a `sidecar=` parameter that no caller ever passes | defer → docs/roadmap-future.md | Forensic filename plumbing remains useful but spans the apply outcome and pull accumulator; it is not needed to prevent the reproduced deletions. |
| 14 | Pull side does not reject conflict-shaped rel_paths from a peer manifest | discharged@f2624fb → v0.14.0 | _filter_excluded_paths rejects conflict-shaped names and .extend-root even with no configured excludes. |
| 15 | Conflict discovery walks trees that `exclude_patterns` removed from sync | discharged@f2624fb → v0.14.0 | The requested surface-asymmetry documentation exists in docs/invariants/conflicts.md; discovery intentionally still finds excluded copies. |
| 16 | Conflict GC retention age should be configurable | defer → docs/roadmap-future.md | Live and recoverable copies are now preserved regardless of age; configurable retention of redundant copies is lower priority. |
| 17 | `synclog.write_sync_log` writes `.mind-meld-log.md` for claude sources only | defer → docs/roadmap-future.md | Generalizing project-log layout is separate from the preservation fixes; status already exposes unresolved conflicts. |
| 18 | Sidecar deduplication deletes another canonical file’s conflict | place → Track 48A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 19 | Failed sidecar replacement removes the previous recoverable copy | place → Track 48A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 20 | Upload can replace an untouched file with another file’s bytes | place → Track 49A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 21 | Rejected manifest filenames bypass terminal sanitization | place → Track 50A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 22 | Unreadable existing files become deletion tombstones | place → Track 49A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 23 | Mixed JSONL timestamp types abort the remaining pull batch | place → Track 51A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 24 | Codex diagnostics report ready after an unsupported read | place → Track 52A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 25 | Delete the unused pre-refactor push-cursor wrapper | place → Track 53A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 26 | Delete the unreachable second recapture writer | place → Track 53A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 27 | Remove the orphan host model-id validator | place → Track 52A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 28 | Delete the unused retro event iterator | place → Track 55A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |

**Full-review code provenance:** ROADMAP.md's `_Source:` lines cite the review's own R/C/H codes, which exist only in the review transcript — and Conductor workspaces are ephemeral. The code groups resolve against rows 18-28 above by destination Track: **R2/R3** → 48A (rows 18-19), **R1/C2** → 49A (rows 20, 22), **C1** → 50A (row 21), **R4** → 51A (row 23), **C3/H3** → 52A (rows 24, 27), **H1/H2** → 53A (rows 25-26), **H4** → 55A (row 28). Letter-to-row assignment *within* a pair was not recorded; the Track cards carry the reproduction detail, so nothing depends on it.

**Former active plan:** IDs below refer to the 2026-09-03 plan; they are historical, not current routing keys.

| Old task | Title | Disposition | Evidence or reason |
|---|---|---|---|
| 45A.1 | Reproduce under filesystem instrumentation | kill | Not discharged: no cause was named and `docs/invariants/conflicts.md` records none. Dropped by explicit decision on 2026-09-03 on three supports, only one of which is unconditional. (a) `mm pull` re-materializes a vanished sidecar because the pull diff hashes the live local file — **but only while the mtime-skip gate stays shut**: `cli.py:1871` returns `"skipped"` once the local file is newer than remote, so after the user edits their canonical the conflict path never runs again and nothing is re-materialized. Do not read this as self-healing. (b) 47A plus existing config excludes remove 22 of the 25 affected paths from sync — scope reduction, not repair; 3 remain. (c) The `mm status` conflicts line (`cli.py:4523`) makes any recurrence visible — this one holds unconditionally and is what the kill actually rests on. Honest residual: f2624fb's clock fix does NOT explain the 2026-09-01 event (those peer files were ~12 days old and never crossed the 30-day GC bar); cause unknown, may not be mm. Re-open only on a fresh recurrence observed through `mm status`. |
| 45A.2 | Fix it if it is mm | discharge | f2624fb fixed the demonstrated migration/GC defects; independent new review failures are placed in 48A. |
| 45A.3 | Immediate post-write existence warning | kill | A stat immediately after writing neither prevents the reproduced replacement loss nor proves a future deletion; fix the actual mechanisms in 48A. |
| 46A.1 | Per-state increment encoding and Grok resume | defer | Split into existing inbox cache-encoding and intra-file-resume Future entries; the dated trigger measurement does not justify active work. |
| 46A.2 | Bound a single entry | defer | Existing inbox states-cap entry preserves the totals-versus-dedup contract. |
| 47A.1 | Marker-aware directory skip | discharge | f2624fb shipped normalized marker prefixes, include-root preservation and tombstone suppression. |
| 47A.2 | Exclude pair-review state only | discharge | f2624fb excludes session.yaml while preserving prose artifacts. |
| 48A.1 | Scrub git subprocess environments | place | 53A; both history and remote subprocesses still inherit repository-redirection variables. |
| 49A.1 | Hoist the resume protocol | defer | New Future entry requires a measured shared filesystem seam; no renderer depends on it. |
| 49A.2 | Collapse readers into one per-turn adapter | kill | The common tuple discards cumulative Codex state required for cross-file accounting; Grok and Claude have different identities. |
| 49A.3a | Remove proven obsolete local reader helpers | place | 52A removes the test-only Terminal path alongside full-review H3; preserve production TurnState accounting. |
| 49A.3b | Share leaf coercions and add a boundary guard | defer | Retained with the measured resume seam; avoid policing an abstraction that has not earned its place. |
| 49A.3c | Bound interned model detail | defer | New Future entry preserves day totals and measures cardinality first; distinct from the states cap. |
| 50A.1 | One block, every agent | place | 55A; retains coverage, legacy-name tolerance and the verified pricing dependency on 54A. |
| 50A.2 | Everything else aggregates across models | place | 55A; part of the same renderer outcome, with no new summary-card row. |

**Future membership:** 70 existing deferred bullets retained verbatim; pricing promoted to 54A; the three entries below removed from the queue. Twelve deferred entries added (10 from the inbox, two scoped remnants of the old walker card), leaving 82. Refusals remain policy; removing their queue entries does not authorize them.

- **No tooling migration hidden inside a workspace fix:** keep the existing bin/check interface; do not infer a uv/Hatch/tox migration from hatchling being the build backend. Original refusal: [manual], 2026-09-01.
- **No collector-dependent similarity classifier/silent merge:** the v0.12.51 analysis cancelled this auto-resolver, and [the conflict invariants](invariants/conflicts.md#collector-removal-and-auto-resolver-cancellation) forbid resurrecting the collector. It is not waiting for a dataset.
- **No Codex/Grok sessions-snapshot:** local discovery is not permission to publish encoded cwd or transcripts. Claude's sessions snapshot stays Claude-only; reconsider only if a host supplies a metadata-only index. Original refusal: host-parity [manual], 2026-08-17. The roadmap's standing wire-privacy constraint remains in force.

**ID lineage:**

| Previous ID (2026-09-03 plan) | Disposition / new ID (2026-09-05 plan) |
|---|---|
| 45A: sidecar forensics | Shipped work recorded as 45A; new confirmed replacement defects → 48A; immediate-stat mitigation killed |
| 46A: cache encoding | Re-scoped shipped Grok repair recorded as 46A; original encoding/resume/cap work → Future |
| 47A: sync surface | Shipped as 47A |
| 48A: git environment | 53A |
| 49A: one walker, two adapters | Blanket adapter recipe killed; measured filesystem sharing/model bounds → Future; obsolete helpers → 52A |
| 50A: unified reporting | 55A |


Drained 2026-09-02 by Track 37A implementation: 5 discharged (release.yml guard,
width-coupled tests, xdist, CI isolation, bin/check — the six-Track split was
killed), 4 placed (36B amendments, unowned OpenCode files, 44A CLI verbs, 44A
retirement notice), 4 deferred (see docs/roadmap-future.md).


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
- 2026-10-29 (/project-spec + /roadmap): 5 drained, 0 placed — 4 killed (pull-warning remedies: unobserved, reversible, reopen if it manifests; time-to-green stopwatch: its 87A window passed; changer-tagged inventories for 88A: 88A is already planned and between review and ship; refresh_due for un-overridable model IDs: unobserved, reversible, reopen if it manifests), 1 discharged (parallelize 87A if 86A slips: discharged@6a49f66, both landed).

_Last updated 2026-10-06 by /roadmap: eight inbox items drained; approved 69A/69B deliverables archived; S13 remains required and unpassed. Prior drain records are historical._
