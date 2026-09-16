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

### [plan-eng-review] Distinguish explicitly requested host captures on the wire

- **Why:** Track 61A shares the existing host snapshot schema. An `origin` field
  could distinguish flag-driven captures, but needs its own producer/consumer
  justification rather than being inferred from timestamps or push counts.
- **Effort:** S
- **Priority:** P3
- **Context:** Track 61A approved eng review; no wire change in this Track.

### [plan-devex-review] Host read-budget override

- **Why:** Warm capture can outgrow the autopush read budget. Extend the existing
  proposal in `docs/roadmap-future.md` (“Make the host-usage read budget
  configurable”, originally line 47); do not create a competing design.
- **Effort:** S
- **Priority:** P2
- **Context:** Track 61A documents the ~900–1,000 Codex-rollout ceiling. Explicit
  capture/retry is the current remedy; no override is implemented here.

### [plan-eng-review] Audit forensic append callers for silent-write assumptions

- **Why:** `flock_append_jsonl` intentionally ignores write failures by default.
  Track 61A requests strict outcomes only for its user-requested capture. Audit
  the other callers for success claims that actually require a written row.
- **Effort:** S
- **Priority:** P3
- **Context:** Track 61A strict append gate; preserve best-effort forensic callers
  unless an explicit caller contract requires otherwise.

### [ship:severity=informational] Reconcile docs/ROADMAP.md for Track 59A + 60A

- **Why:** the plan completion audit for PR #177 found all 28 implementation
  and test items DONE, but `docs/ROADMAP.md` still lists Track 59A and Track
  60A as in progress rather than shipped. This repo's convention (confirmed
  twice before) is that only `/roadmap` writes `ROADMAP.md` — never a hand
  edit during `/ship` or `/implement`.
- **Effort:** S
- **Priority:** P1
- **Context:** Deferred from plan:
  `/Users/kb/.gstack/projects/kbitz-mind-meld/ceo-plans/2026-09-15-track-59a.md`.
  User chose to ship v0.14.13 and reconcile the roadmap in a follow-up
  `/roadmap` run (2026-09-15 decision).

### [ship:severity=informational] Extract a shared _open_crypto_session helper

- **Why:** the `pending: list[str] = []` / `_init_crypto_session(..., pending=pending)`
  / `for note in pending: console.print(safe_str(note))` triplet is duplicated
  verbatim across `pull`, `status`, `diff`, `gc`, and `recapture` in `cli.py`.
  A thin wrapper removes ~18-20 lines and keeps the five call sites from
  drifting apart.
- **Hypothesis (untested):** wrap it as `_open_crypto_session(backend,
  passphrase, config, read_only) -> int`, returning `memory_kb` and printing
  pending notes internally.
- **Effort:** S
- **Priority:** P3
- **Context:** PR #177 pre-landing review (maintainability specialist).
  Deferred rather than touching 5 call sites with subtly different
  `read_only` arguments right before shipping.

### [ship:severity=informational] Minor redundant path/hash re-reads in mm-events + crypto repair

- **Why:** two small, explicitly non-blocking redundancies found in review:
  `_bootstrap_mm_events_path` re-derives a normalized-path comparison that
  `resolve_sources` already computed; `apply_crypto_init_repair` re-reads and
  re-hashes the canonical file and every conflict copy that
  `fetch_crypto_init` just read moments earlier. Both are bounded by tiny
  file counts/sizes (crypto-init candidates, home-dir ancestor depth), not by
  peer or history size — real-world cost is negligible.
- **Hypothesis (untested):** thread the already-computed normalized path /
  already-read bytes through instead of re-deriving — EXCEPT the final
  immediate-pre-unlink re-read in `apply_crypto_init_repair`, which is a
  deliberate TOCTOU guard and must stay exactly as-is.
- **Effort:** S
- **Priority:** P3
- **Context:** PR #177 pre-landing review (performance specialist).

### [ship:severity=informational] Disambiguate historical device IDs with shared display prefixes

- **Why:** the shared eight-character machine label keeps Agent activity,
  economics, and model subtotals consistent, but two accepted historical IDs
  with the same prefix still look identical. Calculations use full IDs and
  remain separate; newly generated IDs are already eight characters.
- **Repro:** `_device_table_label("abcdefgh-first")` and
  `_device_table_label("abcdefgh-second")` both return `abcdefgh`.
- **Hypothesis (untested):** derive one collision-aware label map for all three
  views while preserving their terminal width and sanitization contracts.
- **Effort:** S
- **Priority:** P3
- **Context:** PR #176 Greptile follow-up, `aggregator.py` machine-label helper.
  User chose to ship v0.14.12 with this display limitation on 2026-09-15.

### [plan-ceo-review] Price Claude fast mode when it is first used

- **Why:** Fast mode bills Opus 5 and Opus 4.8 at $10/$50, twice the standard tier, and prompt-caching multipliers
  stack on top (platform.claude.com pricing, read 2026-09-14). mm prices every Opus token at the standard tier, so
  a fast-mode window under-reports by up to 2x under a confident `~`. Track 58A ships the disclosure — the rendered
  rate legend, README and `docs/invariants/events-retro.md` all say fast-mode turns are priced at standard rates —
  but not the detector.
- **Evidence:** a census of 228 session jsonls touched in the last 45 days (26,339 assistant usage rows) found
  `usage.speed` present on 22,042 rows, **all `"standard"`**, and **absent on 4,297** (absence of the field, not
  evidence about when those rows were written). `parse_usage` discards `speed`, and the sessions-snapshot wire
  carries no speed field, so fleet-wide exposure cannot be measured from the wire today.
- **Trigger (human, deliberately not a machine trigger):** you knowingly run Claude Code in fast mode, or Anthropic
  extends fast mode beyond Opus 5 / 4.8. The /autoplan gate chose this trigger over building a detector for a
  condition observed zero times.
- **Hypothesis (untested):** the cheapest honest shape is local-only — record fast-mode days in the token cache and
  report them in `mm diag`, with no wire field and no marker change.
- **Context / cost, measured during the 58A review:** the cache path is the expensive part, and both eng voices
  flagged it independently. The shape check must land in BOTH cache-reuse gates — `get_or_compute`'s size/mtime hit
  AND `_resume_plan` — because a pre-58A entry carries `offset`/`head` and would otherwise resume and persist
  "zero fast turns" forever without ever inspecting history. It also needs a counter schema (a day-set cannot supply
  counts), `MAX_BY_DAY_DAYS` trimming, message-id dedup, concurrent-append handling, and full-walk-vs-incremental
  equivalence tests. `JsonlSegment` is a 4-field NamedTuple, so every construction site changes. A fleet-wide wire
  flag was rejected separately: `speed` is absent on 16% of rows, so it would ship a third coverage state
  (detected / checked-and-absent / unknown) that nothing models, plus new wire content the 58A card requires a
  separate proposal for.
- **Effort:** M
- **Priority:** P3

### [ship:severity=informational] Per-reader "completed, no usage" is whole-row-scoped on the mm status/diag read path

- **Why:** `/ship`'s pre-landing review found the SAME bug shape in two places. The
  live-push console print (`cli.py:_push_captured_usage`, ~line 3499) checked
  whole-capture `capture.hosts` truthiness instead of the specific reader's own
  contribution, so a reader that itself completed with zero usage inherited
  "contributed" whenever a sibling reader in the same sweep had real data. That
  instance was fixed and pinned in this PR
  (`test_capture_outcome_labels_each_reader_independently`). The second instance
  is NOT fixed: `mm status`/`mm diag`'s "; completed, no usage" suffix
  (`cli.py:_print_host_publication`, `state.get("empty")`) is fed by a single
  row-level `"empty"` boolean (`aggregator.py:local_host_capture_candidate`,
  `"empty": not row.lifetime_by_family`) applied uniformly to every reader
  tagged "contributed" — the same blind spot, on the persisted-row read path.
- **Hypothesis (untested):** thread a per-reader (per-family) emptiness signal
  through `local_host_capture_candidate` instead of one row-wide boolean — e.g.
  `"empty_readers": [name for name in row.consulted if name not in
  row.lifetime_by_family]` — then have `events.project_host_publication` and
  `cli._print_host_publication` key the suffix off reader membership in that
  list. Must stay allowlist-safe (coverage/booleans only, never raw token
  payload — see the adapter's existing "never the host token payload" docstring
  rule) and handle a row already synced from an older Mac that lacks the new
  key (graceful "unknown," not a crash or a silent wrong label).
- **Effort:** M
- **Priority:** P2
- **Context:** PR #178 (Track 61A) pre-landing review, maintainability +
  checklist + plan-completion-audit specialists converged independently on the
  live-push instance; maintainability additionally traced this second,
  unfixed instance. Deferred rather than threading a new field through
  events.py + aggregator.py + cli.py and reasoning through mixed-fleet
  backward compatibility during `/ship`.


## Drain records

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
| 18 | Decide whether interactive `mm pull` should exit non-zero when files failed to apply | defer → docs/roadmap-future.md | User kept exit 0 at the 53A gate; `pull()` still exits 0 with `Pull incomplete:`. |
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

_Last updated 2026-09-14 by /roadmap; the inbox is empty. Prior drain records are historical._
