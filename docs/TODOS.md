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

### [plan-eng-review:severity=major] Contain apply exceptions without losing completed-file bookkeeping

- **What:** Define per-file exception containment and partial-pull durability together, so one failed apply does not discard completed outcomes or prevent unrelated downloads.
- **Why:** `_download_and_apply` calls `_apply_incoming_file` without an exception boundary. An exception prevents its outcomes from reaching `_pull_one_source` and `_pull_core`; remaining files/sources/peers stop, the in-flight source's history and sync-log updates are skipped, and the normal deferred directory-fsync phase is bypassed. Autopull does report an unexpected-error line and a failed breadcrumb, so this is not a claim of completely silent failure.
- **Repro:** At `cc22b6c`, use an isolated encrypted LocalBackend batch ordered `earlier.txt`, `blocked/inside.txt`, `later.txt`, with a regular local file occupying `blocked`. The first file is written; `_apply_incoming_file`'s parent `mkdir` raises `FileExistsError`; the last file is absent and the blocking local file is preserved. The loss of the returned outcome map is reproduced; skipped source bookkeeping and deferred fsync are traced in `_pull_one_source` and `_pull_core`. No data loss after a power failure was simulated.
- **Context:** Track 51A `/autoplan`, 2026-09-06, branch `kbitz/51a-mixed-timestamp-pull`. The track fixes heterogeneous JSONL sort keys and narrowly handles decoder `RecursionError`; it does not claim general apply isolation. Evidence: `~/.gstack/projects/kbitz-mind-meld/51a-deferred-isolation-reproduction.json`. The same review's mixed-ts encrypted batch reproduction is in `51a-integration-reproduction.json`.
- **Pros:** Later files can still arrive, completed writes keep honest history, and deferred durability no longer depends on every apply returning normally.
- **Cons:** A blanket catch can misreport an exception after a successful write, lose touched-parent information, hide programming defects, or swallow intentional abort semantics. Failure classification, outcome ownership and finalization must be designed together.
- **Next step:** Inventory exceptions before and after publication; specify which become per-file failures and which still abort. Add isolated multi-file tests for a normal write followed by the parent-file collision, an injected post-publication exception, and explicit user abort. Require useful sanitized file context, accurate failed/degraded reporting, preservation of already-completed outcomes, and directory durability without draining abandoned keep-local decisions.
- **Depends on:** Coordinate with Track 51A's classifier fix; no live fleet scan or mutation is needed. Preserve the existing deferred-inline-bump abort contract in `docs/invariants/conflicts.md`.
- **Effort:** M
- **Priority:** P2

### [plan-eng-review:severity=major] Harden existing Rich and multiline sanitizer consumers against nested escapes

- **What:** Define and enforce a terminal-control postcondition for the existing `strip_terminal_escapes`, `safe_str` and `safe_text` consumers, preserving the intended formatting of multiline content.
- **Why:** The current sanitizer performs one regex substitution. Removing an embedded CSI can assemble a fresh OSC 52 sequence after the regex has passed that position. A captured Rich console probe confirms that a deliberately nested ST-terminated payload survives both `safe_str` and `safe_text` as a complete terminal sequence.
- **Repro:** At `0c1a969`, pass the escaped Python string `"\x1b\x1b[31m]52;c;VEVTVA==\x1b\x1b[31m\\"` through either helper and render into `Console(file=StringIO(), force_terminal=True, color_system=None)`. Inspect only `repr`/JSON of the capture: it contains `"\x1b]52;c;VEVTVA==\x1b\\"`. Never replay the capture in a live terminal.
- **Context:** Track 50A `/autoplan`, 2026-09-06, branch `kbitz/sanitize-storage-filenames`. The recommended plan adds a stricter plain-field helper for the two storage rejection warnings and one malformed-blob GC warning. It does not change existing Rich/diff renderer semantics or repair their other callers. Evidence is saved at `~/.gstack/projects/kbitz-mind-meld/50a-reproductions.json`, under `corrected_shared_rich_sink_probe`; the preceding attempt with an extra literal `]` did not reproduce the complete ST sequence and is retained as a negative control.
- **Pros:** Closes the same confirmed escape-composition mechanism across the remaining terminal sinks instead of fixing one payload spelling at a time.
- **Cons:** A blanket printable-only conversion would escape meaningful newlines and formatting characters in diff bodies; a fixed number of regex passes is not a proof against arbitrary nesting. The shared contract and affected callers need explicit review.
- **Next step:** Inventory Rich versus plain versus multiline sinks, choose a postcondition that prevents executable ESC/C1 output while retaining application-owned layout, and add captured final-render regressions for nested BEL/ST sequences. Include existing plain-stderr users of `safe_str` such as `retention.py` and `token_usage.py` when deciding routing; do not infer that Rich markup escaping makes terminal controls inert.
- **Depends on:** No release dependency; coordinate helper naming with the Track 50A plan. No live fleet mutation is needed to reproduce or test this.
- **Effort:** M
- **Priority:** P1


### [plan-eng-review:severity=minor] Decide whether unchanged pulls should retry stale conflict-copy cleanup

- **What:** Evaluate a bounded retry of same-owner/same-peer post-inversion cleanup when the current remote bytes already have a sidecar.
- **Why:** After a replacement publishes successfully but unlinking the older sidecar fails, an identical later pull returns early. The older differing revision can remain indefinitely; GC correctly preserves it while it diverges from canonical.
- **Repro:** In an isolated fixture, save peer R1, publish peer R2 while injecting an unlink error for R1, then restore permissions and pull R2 again. Both copies remain because the identical-content branch performs no cleanup. Pinned by `tests/test_conflict_copy.py::TestConflictPublishThenCleanup::test_cleanup_failure_then_unchanged_retry_leaves_extras`, which also verifies both revisions remain available to the resolver.
- **Context:** Track 48A /autoplan, 2026-09-05, branch kbitz/48a-conflict-ownership at 64c301d. The hotfix intentionally keeps this no-mutation branch. This is a policy choice, not another failed-publication bug: R2 is saved and mm resolve can inspect both revisions. Do not call differing revisions duplicates or infer safe deletion from sidecar mtime (the peer clock).
- **Pros:** A transient cleanup failure would not leave old revisions forever under a latest-per-peer policy.
- **Cons:** Adds deletion to a formerly unchanged path and could discard edits made directly in a managed sidecar; scope and user expectation need an explicit decision.
- **Hypothesis (untested):** Reuse the shared exact-owner/era/peer predicate only after verifying a matching current sidecar exists; preserve it and every unverifiable candidate. Compare this with retaining the existing manual resolve path before implementing.
- **Depends on:** The preservation/ownership hotfix and its cleanup-failure → unchanged-retry → resolve regression, implemented in v0.14.2. The cleanup policy decision remains open.
- **Effort:** S
- **Priority:** P3

### [plan-eng-review:severity=major] Audit historical blob integrity and design verified targeted re-upload

- **What:** Design a bounded, read-only integrity inventory for referenced encrypted blobs, then a separate targeted re-upload path that restores a mismatched key only from verified authoritative local bytes.
- **Why:** Track 49A prevents new stale-digest uploads and rejects mismatched incoming plaintext. It does not inspect unchanged stored blobs, and an unchanged push or metadata-only touch does not upload them again. Updated receivers can therefore repeatedly reject a historically poisoned key with no supported targeted repair command.
- **Evidence:** The 49A isolated reproduction at 09d98ab stores changed plaintext under a prior shared digest key, so an untouched sibling resolves to the wrong bytes. This proves the mechanism, not that any particular live fleet blob is corrupt. `cli.py:_upload_changed_blobs` and the hash-based diff determine which keys are rewritten.
- **Context:** Track 49A /autoplan, 2026-09-06, branch kbitz/49a-complete-snapshots. Durable plan: `~/.gstack/projects/kbitz-mind-meld/kbitz-49a-complete-snapshots-plan.md`; evidence: `49a-reproductions.json`. This is distinct from the Future snapshot-completeness data-model item.
- **Pros:** Establishes actual historical damage and gives users a precise recovery path without changing meaningful file contents to force a new hash.
- **Cons:** Reading cloud blobs can materialize data and cost time; replacement authority and multi-reference keys need explicit handling. Inventory must be bounded and distinguish unavailable from mismatched; repair must preserve recoverable bytes.
- **Next step:** Specify a report keyed by device/source/path/digest, with valid, unavailable and mismatched states. Require the candidate repair bytes to hash to the advertised key, encrypt before storage, and verify the result. Do not auto-select a peer copy or run fleet-wide mutation as part of discovery.
- **Depends on:** Track 49A's prospective producer/receiver checks; an inventory and explicit authority decision before any repair is implemented or run.
- **Effort:** M
- **Priority:** P2

### [plan-eng-review:severity=moderate] Make push dry-run setup honor the existing no-mutation contract

- **What:** Audit and gate setup mutations before `mm push --dry-run` reaches its already-gated publication core.
- **Why:** `docs/invariants/events-retro.md` requires a non-mutating preview, but the current command can offer/apply migration, persist a missing crypto fingerprint and bootstrap the mm-events source directory before the dry-run gates. Track 49A adds strict scan/deletion diagnostics; it must not describe the whole current command as read-only or weaken the existing invariant to match this defect.
- **Evidence:** Code trace at 09d98ab: `cli.py:push` calls `_maybe_prompt_migration` and `_init_crypto_session` before `_push_core(..., dry_run=True)`; `get_sources` invokes `_bootstrap_mm_events_path` unconditionally. This is a source audit, not a claimed live mutation reproduction.
- **Context:** Track 49A /autoplan DX/Eng review, 2026-09-06. Its tests protect blob/manifest/last_seen/event publication and add no new preview writes; the broader setup repair is separate. Do not route a dry-run through destructive recover/reset commands.
- **Pros:** Makes the documented preview safe and predictable for troubleshooting and automation.
- **Cons:** Requires a deliberate read-only crypto/source setup contract and migration-prompt behavior; simply skipping initialization can produce a misleading preview.
- **Next step:** Add isolated CLI tests for missing fingerprint, missing mm-events root and pending config migration, snapshot all config/source/storage paths, then implement read-only setup or an explicit actionable refusal where a truthful preview needs initialization.
- **Depends on:** Coordinate with Track 49A's shared source-resolution helper; preserve its strictness and publication guarantees.
- **Effort:** M
- **Priority:** P2

## Drain records

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
- **No collector-dependent similarity classifier/silent merge:** the v0.12.51 analysis cancelled this auto-resolver, and AGENTS.md forbids resurrecting the collector. It is not waiting for a dataset.
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
- 0 placed from the inbox: `## Unprocessed` was already empty.

_Last updated 2026-09-06 by Track 51A /autoplan; five items remain open: apply exception containment/durability, shared terminal sanitizer hardening, conflict-cleanup policy, historical blob integrity/repair, and push dry-run setup. Prior drain records are historical._
