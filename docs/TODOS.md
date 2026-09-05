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

### [plan-eng-review] Intra-file resume for a Grok ledger that exceeds one read budget
- **Why:** `_read_grok_file` has no intra-file partial stage. A `deadline` mid-file raises and discards `last_offset`, so a single ledger larger than the remaining budget can never be read. Track 46A's card named this as `_validated_grok_entry` `offset == size`; that validator never rejected (0 of 42 live entries). The real hazard is the missing mid-file checkpoint.
- **Repro:** largest live ledger 18.1 MB of 261 MB total / 983 ms full scan → ~265 MB/s. Wall is ~1.3 GB in one file against `DEFAULT_READ_BUDGET_S = 5.0`.
- **Trigger:** any single `updates.jsonl` > 1 GB, or a `deadline` that recurs across 3+ interactive warms.
- **Effort:** M
- **Priority:** P3
- **Context:** filed by Track 46A /autoplan, 2026-09-04. Claude eng voice #3.

### [plan-eng-review] Host cache encoding trigger (deferred from original Track 46A)
- **Why:** `locked_json_rmw` parses and re-serialises the whole host cache every push, so cost scales with corpus size. The original 46A card's own gate does not fire.
- **Trigger:** json round-trip above 100 ms, or cache above 25 MB.
- **Repro:** measured 2026-09-04 on device 889e42c0 after a converged push: Codex `host-tokens.json` 4.11 MB / 23.3 ms / 20,047 states / 716 rollouts. Linear in states, so 25 MB ≈ 122k states ≈ 4,350 rollouts at the current mean (~6x headroom). Codex prunes its own sessions, so the corpus is not monotonic.
- **Effort:** S
- **Priority:** P3
- **Context:** filed by Track 46A /autoplan, 2026-09-04. Original card premises falsified by live probe. `host_usage._CacheEntry` docstring points here.

### [plan-eng-review] Per-entry `states` cap on the Codex host cache
- **Why:** a per-entry cap needs a degradation that keeps the file's tokens counted while dropping only its cross-file dedup. Refusing the file would re-create the fail-closed whole-store pathology Track 31A removed. `iter_bounded_lines` bounds line SIZE, not line COUNT. Max observed 2026-09-04 is 441 states/entry (card said 1,234, which was the 2026-08-28 corpus).
- **Effort:** S
- **Priority:** P3
- **Context:** filed by Track 46A /autoplan, 2026-09-04. This is its own item. Do NOT fold into Track 49A: that card bounds `_add_usage`'s `by_model` materialization and says capping in the READER is not the fix. Different symbol, opposite conclusion.

### [plan-eng-review] Add the xAI / `grok-4.6-build` price tier
- **Why:** Track 35A's gate (D1, 2026-09-01) still owns pricing. Track 46A discharged the two false blockers named at `token_usage.py` (`offset == size` wedge never fired; OpenCode `$.id` defect died with the reader in v0.12.53) and restored Grok ingestion. Until this lands the retro renders Grok tokens with no cost. `resolve_prices("grok-4.6-build")` returning None is now a decision, not a reader defect.
- **Effort:** S
- **Priority:** P2
- **Context:** filed by Track 46A /autoplan, 2026-09-04. Claude CEO voice #10.

### [plan-eng-review] Ask whether Grok/Codex expose a supported usage surface
- **Why:** mm exact-match-parses undocumented, weekly-shipping private formats of tools it does not control, fail-closed, with no version negotiation. Both 46A review voices flagged that no approach questioned private-file parsing. Strategic, out of scope for a 3-day outage fix.
- **Effort:** M
- **Priority:** P3
- **Context:** filed by Track 46A /autoplan, 2026-09-04.

### [plan-eng-review] Reader-agnostic quarantine and drift classification (Track 46B)
- **Why:** 46A tolerated exactly one additive key (`elapsed_ms`) so a 3-day outage could end. The shape that caused it is unchanged: ~13 exact-match detectors, any of which turns one additive upstream field into a total silent store abort. `_GROK_STOPS` is a closed 2-value enum and is the likely next one. The durable fix is per-record quarantine plus a `drift_skipped` counter, so a drifted record costs one record instead of the whole agent.
- **Scope:** per-record quarantine; `drift_skipped`; truthful coverage on day-disjoint drift (the C1 snapshot keep-set fix); a parser epoch so a later allowlist recovers tokens from a byte-identical ledger without a cache bump; widen `partial_sources` (drift is not "the host declared"). Reader-agnostic — Codex has the same detector shape.
- **Trigger:** the next additive-key drift on either host, or any `unsupported` that survives one `pipx upgrade`.
- **Effort:** L
- **Priority:** P2
- **Context:** filed by Track 46A /ship, 2026-09-04 (plan item N5, missed by the 46A TODO sweep). Until this is carded, 46B exists only as prose in `_classify_grok_update`'s docstring, `tests/fixtures/host_sessions/grok/CONTRACT.md`, and `docs/invariants/events-retro.md:229`.

### [ship:severity=minor] Stale Track/version literals in code and test comments (15 across 10 files)
- **Description:** the 2026-09-03 renumber (Groups 45-50) fixed the stale cross-references found in docs/, but the same epoch-rot lives in code and test comments a docs-only PR does not touch. Route per file, not as one batch: fix each literal inside the Track that already declares the file (`host_usage.py`/`token_usage.py` + their tests → 46A or 49A; `config.py` → 47A; `cli.py` → 45A); the four retirement docstrings live in test files no upcoming card declares — fold them into whichever of those Tracks widens cheapest at drain time, per the documented drift process. **Completeness criterion is the grep, not this list:** `grep -rn "Track 37A\|Track 37B\|Track 39A\|Track 42A" src tests` — every hit must either name a live card or carry dated lineage. (`Track 37A` hits in `tests/test_bin_check.py:1` and `tests/test_docs_routing.py:16`/`:666` are the CORRECT shipped verification-command Track — leave those.)
- **Items:** wedge-card literals at `token_usage.py` / `test_token_usage.py` **discharged in Track 46A** (2026-09-04): the comment now records that both named blockers are false. Walker substrate, now **49A** (was 42A): `src/mind_meld/host_usage.py:25`/`:279`/`:1062`/`:1785`, `tests/test_host_usage.py:2598`. Source retirement, now **44A** (was 37B): `src/mind_meld/config.py:53`, `src/mind_meld/cli.py:6058`, `tests/test_pull_helpers.py:1813`, `tests/test_source_toggle.py:217`, `tests/test_integration.py:1026`, `tests/test_docs_routing.py:48` — that last docstring is also stale in SUBSTANCE: it says 44A "leaves the mm-owned OpenCode skill link on disk / this loop is the only cleanup", but v0.13.0 ships a reaper that removes the mm-owned link on interactive push/init/install-skills, so the README loop now covers user-made links and never-interactive machines only. Sync surface, now **47A** (was 39A): `src/mind_meld/config.py:56`, which also still says "(blocked-by: 37B)" — an edge discharged 2026-09-03. Version string: `src/mind_meld/cli.py:5716` says the opencode source "was retired in v0.12.54"; the retirement shipped in **v0.12.55** (v0.12.54 was bin/check).
- **Effort:** S
- **Priority:** P3
- **Context:** filed by /ship pre-landing review + red-team pass on the roadmap-regen branch, 2026-09-03. Comment/docstring-only edits, zero runtime effect; grouped so they ride along instead of minting a docs-only code PR.

### [plan-eng-review:severity=critical] `_ensure_inversion_marker` makes every new Mac mis-migrate 100% of its conflict sidecars
- **Description:** `resolveflow._ensure_inversion_marker` mints `time.time()` on FIRST call. Its docstring assumes "every NEW conflict file produced from here on (mtime > now) is correctly skipped" — but `cli._apply_conflict` ends with `_restore_mtime_best_effort(conflict_path, remote_mtime_iso)`, stamping the sidecar with the PEER's mtime, which is essentially always in the past. `conflictmtime._restore_mtime_best_effort` clamps only the FUTURE (`now + 60s`); there is no past floor. So on a freshly-initialized device the marker is "today" and **every** sidecar it writes falls below its own safety gate and is renamed `v0-` by the migration sweep at `cli.py:4006`, which runs at the top of every pull including autopull. `_resolve_interactive_loop` then dispatches `v0-` under pre-inversion semantics where `(l)ocal` renames the sidecar OVER the canonical — so the user picks "keep my version" and mm overwrites their local file with the peer's bytes.
- **Repro:** verified end-to-end on 2026-09-03 at `23b2cc8` / mm 0.13.0. Back-date a post-inversion sidecar to a peer mtime before the marker, run `resolveflow._find_conflict_files(cfg, migrate_pre_inversion=True)` (the exact call `_pull_core` makes): the file is renamed to `...sync-conflict-v0-...`. `mm conflicts` then renders it `Mode: pre-v0.9.2` with the local and remote columns SWAPPED.
- **Effort:** M
- **Priority:** P1
- **Context:** filed by /autoplan on Track 45A, 2026-09-03. Confirmed by all three review voices independently. The fix is NOT to stop back-dating the sidecar: its `st_mtime` is the only surviving carrier of the peer clock, read by `resolveflow.py:809` for the v0.12.5 resolve bump AND by `conflictmtime._stat_mtime_btime` for v0.12.10's `newer_side` / `render_verdict` / `(n)ewer`. Fix is to stop the CONSUMERS misreading the peer clock as the sidecar's own age. `cli.conflict_filename` already stamps `datetime.now(timezone.utc)` into the filename. **Era marker must go at `<stem>.sync-conflict-<ts>-v1-<dev8>`, NOT as a `v0-`-style prefix** — probed 2026-09-03: the prefix form makes `is_conflict_filename` return False, hence `manifest._is_excluded` False, hence conflict copies get UPLOADED to the fleet. See the full analysis at `~/.gstack/projects/kbitz-mind-meld/kbitz-fix-vanishing-conflict-sidecars-track45a-plan.md`.

### [plan-eng-review:severity=critical] README documents the pre-inversion conflict direction, and contradicts itself two paragraphs later
- **Description:** `README.md:62` and `README.md:413` (the "Handling conflicts" section itself) both say "the remote wins the canonical path and your local version is preserved as `<stem>.sync-conflict-...`". That is the PRE-v0.9.2 direction. Since v0.9.2 local stays at canonical and the REMOTE bytes go to the sidecar. `README.md:418` documents `(l)ocal` as "keep your edits" and `(r)emote` as "overwrite with peer's bytes", which IS correct post-inversion — so the README contradicts itself on the one question where being wrong destroys data. A user who believes :413 thinks the sidecar holds their own work; promoting it over the canonical destroys their edits with the peer's bytes.
- **Repro:** `grep -n -i "sync-conflict" README.md` at `23b2cc8`.
- **Effort:** S
- **Priority:** P1
- **Context:** filed by /autoplan on Track 45A, 2026-09-03; found by the Codex DX voice. Same defect class CLAUDE.md already records as a lesson ("`synclog.py` still describing the pre-inversion direction four months after the inversion"). That copy was fixed in v0.12.51 and **nobody grepped for the other instances**. Completeness criterion is the grep, not this list.

### [plan-eng-review:severity=major] `_migrate_pre_inversion_conflict` uses `find()` where every sibling parser uses `rindex()` — unbounded `v0-` accretion
- **Description:** `resolveflow.py:228` does `name.find(CONFLICT_INFIX)`; `manifest.parse_conflict_device_short` uses `rindex` and `resolveflow._canonical_for_conflict` uses `rfind`. mm always appends its infix LAST, so `find` is wrong. On a double-infix name the `v0-` lands before the inner segment instead of before the digits, so `is_pre_inversion_conflict_filename` never latches and the file is renamed once per pull, forever.
- **Repro:** verified 2026-09-03. `notes.sync-conflict-log.md` is a documented NON-conflict canonical name (`tests/test_manifest.py:994`), so it syncs; when it conflicts mm mints `notes.sync-conflict-log.sync-conflict-<ts>-<dev>.md`. Replaying the rename math: `v0-` → `v0-v0-` → `v0-v0-v0-`, `is_v0` False every time.
- **Effort:** S
- **Priority:** P2
- **Context:** filed by /autoplan on Track 45A, 2026-09-03. Each rename bumps the parent dir mtime and leaves nothing at the old name — the exact fingerprint the 2026-09-01 sidecar disappearance was attributed to an unidentified external walker. Test this first in any sidecar-disappearance investigation.

### [plan-eng-review:severity=major] `mm gc --conflicts` can reap a live conflict, prints no paths, and has no preview by default
- **Description:** four gaps in the one reaper that deletes user content. (1) `_gc_old_conflict_files` is called inside `if prune_conflicts:` so a bare `mm gc --dry-run` never previews it. (2) Non-verbose output is `Conflicts: candidates=N deleted=N` with no paths. (3) `CONFLICT_AGE_DAYS = 30` is a module constant with no `--older-than` and no config key. (4) It will reap a sidecar whose canonical still exists and still differs — a live unresolved conflict — on a silent 30-day deadline with no countdown on any surface.
- **Effort:** S
- **Priority:** P2
- **Context:** filed by /autoplan on Track 45A, 2026-09-03; both DX voices independently. Every other consequential mm operation has an escape hatch (`--force` on disable-source, `--dry-run`/`--yes` on migrate-config). Minimum bar: refuse to reap a live conflict, and print paths on delete.

### [plan-eng-review:severity=major] `mm status` and `mm diag` never surface unresolved conflicts
- **Description:** with three conflict sidecars live on disk, `mm status` printed six advisory lines (stale autorun, disabled sources, Codex capture, Grok capture, per-source diffs) and mentioned none of them. `mm diag`'s JSON has no conflicts key. `.mind-meld-log.md` is written only for `type == "claude"` sources (`cli.py:3351` sets `claude_sync_base` on that branch alone), so 22 of the 25 conflicts recorded on 2026-09-01 produced no per-project breadcrumb at all. Conflicts are announced exactly once, in an unattended hook's stderr, then vanish from every surface a human checks later.
- **Repro:** verified 2026-09-03 — planted three correctly-named sidecars, `mm conflicts` listed all three, `mm status` mentioned none.
- **Effort:** S
- **Priority:** P2
- **Context:** filed by /autoplan on Track 45A, 2026-09-03. This is why the 2026-09-01 disappearance was found by accident rather than by mm.

### [plan-eng-review:severity=major] `pullhistory.append_entry` has a `sidecar=` parameter that no caller ever passes
- **Description:** `pullhistory.py:17` documents `"sidecar": "<optional sidecar filename if action=conflicted>"`, `:95` accepts it, `:116-117` writes it when non-None. `grep -rn "sidecar=" src/ tests/` returns zero. So every `conflicted` row records THAT a sidecar was written and never WHICH file, which makes any later reconciliation guess. This is the standing "a write with no reader is a liability" constraint inverted: a parameter with no producer, in the log schema since v0.6.
- **Effort:** M
- **Priority:** P3
- **Context:** filed by /autoplan on Track 45A, 2026-09-03. NOT a 15-line plumb: `_apply_conflict` returns a bare `ApplyOutcome` Literal, `_download_and_apply` gates Track 12A's `_CANONICAL_WRITE_OUTCOMES` invalidation on that exact value, and the row is written two frames up in `_pull_core` from `outcomes[action]: list[str]` (rel_paths only). Also note `pull-history.jsonl` rotates DESTRUCTIVELY at 1 MB (`os.replace` onto `.1`, overwriting the prior `.1`, no gc reaper) and swallows all write failures by design — so it cannot host a conflict lifecycle state machine. If durable conflict state is ever needed, route it through `lockedjson.py` per CLAUDE.md, and name the field `conflict_copy=` (`sidecar.py` already means the manifest cache in this call graph).

### [plan-eng-review:severity=minor] Pull side does not reject conflict-shaped rel_paths from a peer manifest
- **Description:** `manifest._is_excluded` drops conflict-shaped filenames on the PUSH walk. The pull side (`manifest._validate_rel_path`) rejects only NUL / absolute / `..` / drive letters, so a passphrase-holding peer can ship `foo.sync-conflict-19700101-000000-deadbeef.md` and mm will materialize it, after which every conflict-subsystem consumer treats it as mm-minted. Low impact today; becomes load-bearing the moment the gc bar reads the filename timestamp, because the attacker then chooses the reap age directly.
- **Effort:** S
- **Priority:** P3
- **Context:** filed by /autoplan on Track 45A, 2026-09-03. Fix is one `is_conflict_filename(basename)` call in the pull-side filter, mirroring the push-side gate. Related: `is_conflict_filename` validates digit SHAPE not date validity — `a.sync-conflict-20261345-999999-abcd1234.md` passes, so any new timestamp parser must catch `ValueError` and parse as UTC (`conflict_filename` uses `datetime.now(timezone.utc)`).

### [plan-eng-review:severity=minor] Conflict discovery walks trees that `exclude_patterns` removed from sync
- **Description:** `resolveflow._find_conflict_files` walks `_synced_scan_dirs` with `rglob` and filters only on `is_conflict_filename`; it never consults `exclude_patterns`, which live in `manifest.walk_generic_source`. So `mm conflicts`, `mm resolve`, `mm gc --conflicts` and the pull-top migration sweep all still operate on excluded trees. Verified 2026-09-03: a sidecar planted under `~/.codex/skills/.system/` is listed by `mm conflicts` despite `skills/.system/*` being in that source's `exclude_patterns`. Second-order consequence: a sidecar stranded in an excluded tree can never converge, so `mm resolve`'s `(l)`/`(r)` operate on a file that will never sync again.
- **Effort:** S
- **Priority:** P3
- **Context:** filed by /autoplan on Track 45A, 2026-09-03. Document the surface asymmetry in `docs/invariants/conflicts.md`; it is surprising and currently unwritten.

### [plan-eng-review:severity=minor] Conflict GC retention age should be configurable
- **Description:** `retention.CONFLICT_AGE_DAYS = 30` is a module constant. A user who wants conflict copies kept for 90 days has to fork. Add `[retention] conflict_age_days` to `config.toml`, or at minimum a `--older-than` flag on `mm gc --conflicts`.
- **Effort:** S
- **Priority:** P3
- **Context:** filed by /autoplan on Track 45A, 2026-09-03, deferred out of the safety work as a config-schema change.

### [plan-eng-review:severity=minor] `synclog.write_sync_log` writes `.mind-meld-log.md` for claude sources only
- **Description:** `cli.py:3351` sets `per_source.claude_sync_base` only when `src_cfg["type"] == "claude"`, and `synclog.write_sync_log` hardcodes a `projects/<name>/` layout. gstack, codex and grok conflicts therefore leave no per-project breadcrumb. On 2026-09-01 that was 22 of 25 conflicts.
- **Effort:** M
- **Priority:** P3
- **Context:** filed by /autoplan on Track 45A, 2026-09-03. Separate subsystem from the conflict-clock work; generalizing the layout is its own change.

_Otherwise empty. Drained 2026-09-02 by Track 37A implementation: 5 discharged (release.yml guard, width-coupled tests, xdist, CI isolation, bin/check — the six-Track split was killed), 4 placed (36B amendments, unowned OpenCode files, 44A CLI verbs, 44A retirement notice), 4 deferred (see docs/roadmap-future.md)._

### [full-review:severity=critical,files=src/mind_meld/cli.py] Sidecar deduplication deletes another canonical file’s conflict
- **Description:** Per-peer sidecar discovery treats a stem-prefix glob as exact canonical ownership. The supported canonical notes.sync-conflict-log.md creates notes.sync-conflict-log.sync-conflict-<ts>-v1-aaaaaaaa.md, which the helper incorrectly returns as a sidecar of notes.md; a subsequent conflict for notes.md deletes that unrelated sidecar at line 1749. The temporary-directory reproduction confirms the sibling's remote data disappears. This overlaps Track 45A's forensic topic and directly falsifies its explicit assertion that this helper cannot reap a sibling's sidecar; the mechanism remains in HEAD after v0.14.0.
- **Hypothesis (untested):** Investigate replacing glob-based ownership inference with an exact comparison against the canonical name parsed from the final conflict suffix before deduplication or removal. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/cli.py:1662-1677
- **Context:** From /full-review cluster "Conflict deduplication deletes recoverable copies" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=critical,files=src/mind_meld/cli.py] Failed sidecar replacement removes the previous recoverable copy
- **Description:** Replacing a stale sidecar unlinks every previous copy before constructing and writing the replacement. With an existing R1 conflict and a newer peer R2, an ordinary write failure such as ENOSPC returns failed after deleting R1, leaving zero sidecars and losing the only local recoverable peer copy. An isolated injected-write-failure reproduction confirms this; the successful-replacement test intentionally reaps stale snapshots but does not cover preservation on failure. Related to Track 45A's loss investigation, with no specific backlog item for the ordering defect.
- **Hypothesis (untested):** Investigate making replacement success precede stale-copy removal so a failed write leaves the previously recoverable conflict intact. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/cli.py:1743-1777
- **Context:** From /full-review cluster "Conflict deduplication deletes recoverable copies" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=critical,files=src/mind_meld/cli.py] Upload can replace an untouched file with another file’s bytes
- **Description:** Upload ignores the digest returned by read_and_hash and encrypts current bytes under the earlier manifest scan's sha256. A normal edit between scan and upload therefore corrupts content addressing; when two files were scanned with identical bytes, editing the second before upload overwrites their shared blob and a peer receives the edited file's unrelated bytes for the untouched file, reporting both as written. The real encrypted LocalBackend reproduction confirms this cross-file corruption; no existing backlog entry covers it.
- **Hypothesis (untested):** Investigate removing the inconsistent second snapshot so the published manifest metadata and blob key describe the exact uploaded bytes, or refuse publication when a scanned input changed. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/cli.py:1238-1241
- **Context:** From /full-review cluster "Upload bytes disagree with their content hash" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=critical,files=src/mind_meld/storage/local.py] Rejected manifest filenames bypass terminal sanitization
- **Description:** Rejected shared-storage manifest filenames bypass the established terminal-sanitization boundary: find_conflict_copies writes raw candidate paths (and exception tails) to stderr, unlike safety.strip_terminal_escapes/safe_str callers elsewhere; a Dropbox-shaped conflict filename containing OSC 52 passes the candidate regex and the false-validator warning emits its complete control sequence, reachable through every _fetch_remote_manifest call at cli.py:541 even though the ciphertext is rejected; an isolated StringIO repro confirmed the raw sequence and the shared sanitizer's removal, with no existing backlog overlap found.
- **Hypothesis (untested):** Keep the useful validation warning and remove the raw interpolation path by applying the existing plain-terminal sanitizer to filenames and exception text, then verify both rejection branches through the real manifest-fetch caller. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/storage/local.py:213-223
- **Context:** From /full-review cluster "Rejected storage filenames reach the terminal unsanitized" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=necessary,files=src/mind_meld/cli.py] Unreadable existing files become deletion tombstones
- **Description:** Push converts an unreadable existing file into a deletion tombstone: _record_file catches a hash/read OSError and omits that path, but the push consumes the incomplete manifest without filtering failed paths from its deletion comparison; this violates the existing exclusion/symlink tombstone-suppression pattern and cli.py:1044's explicit rule that walker omission is not causal deletion evidence; a temp-only repro leaves notes.md on disk, records its read-error skip, passes the exact push filters, then emits custom:notes.md and makes is_tombstoned suppress peer restoration; on_skip is only displayed interactively and is not an autopush degradation; no existing backlog overlap found.
- **Hypothesis (untested):** First remove the unsafe assumption that a failed walk is a complete deletion snapshot by refusing or narrowing that push's deletion comparison, then pin unreadable-existing-file versus truly-deleted-file behavior before adding recovery machinery. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/cli.py:3107-3114
- **Context:** From /full-review cluster "Read errors become deletion tombstones" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=necessary,files=src/mind_meld/merge.py] Mixed JSONL timestamp types abort the remaining pull batch
- **Description:** _extract_ts returns arbitrary JSON ts values despite its str-or-None contract, and merge_jsonl sorts those raw values together at line 99. A valid JSONL containing a numeric epoch timestamp merged with a peer line using an ISO timestamp raises TypeError; _apply_merge and _download_and_apply do not catch it, so one file aborts the pull batch and later ordinary files are never downloaded. An encrypted temporary-backend reproduction confirms that following.md is skipped by the aborted batch; no existing backlog item or mixed-type timestamp test covers it.
- **Hypothesis (untested):** Investigate restricting timestamp ordering to the supported comparable type and letting other valid JSON lines use the existing deterministic lexical fallback. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/merge.py:141-146
- **Context:** From /full-review cluster "Mixed JSONL timestamp types abort a pull" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=necessary,files=src/mind_meld/host_usage.py] Codex diagnostics report ready after an unsupported read
- **Description:** Codex diagnosis reports state=ready after a permanent unsupported read because it derives readiness solely from cached and on-disk file counts and never persists the latest failure; Grok's parallel path now preserves last_reason and surfaces permanent failures instead of success/retry claims; warming one valid rollout and then appending unsupported counters reproduces read_codex_usage.complete=False/reason=unsupported alongside codex_usage_diag.state=ready/pending=0, and cli.py:4606-4615 emits no dedicated Codex warning for this state; Track 46B's TODO covers reader-agnostic quarantine but does not cover this diagnostic contradiction.
- **Hypothesis (untested):** Treat Codex capture as still-needed functionality and replace the cache-count-only success inference with the same persistent failure-state contract used by Grok, verifying that successful recovery clears it and transient failures do not erase a standing permanent failure. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/host_usage.py:536-553
- **Context:** From /full-review cluster "Codex reports ready after capture fails" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=nice-to-have] Delete the unused pre-refactor push-cursor wrapper
- **Description:** `_last_mm_push_ts` is an unreachable pre-cursor-refactor implementation. `last_push_ts` delegates directly to `resolve_push_cursor`, which reads `_iter_mm_push_objs`; no production or test caller invokes `_last_mm_push_ts`. Its docstring and events-retro invariant falsely claim the existing last-match tests exercise it, leaving a second cursor algorithm that can be edited without affecting actual behavior. No matching current backlog item found.
- **Hypothesis (untested):** Delete the unused private wrapper and update the invariant references to the actual `_iter_mm_push_objs`/`resolve_push_cursor` path while preserving their behavioral tests. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/events.py:1599-1613
- **Context:** From /full-review cluster "Unused helpers survived completed refactors" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=nice-to-have] Delete the unreachable second recapture writer
- **Description:** `_run_events_recapture` is a second recapture writer with no caller in production or tests. The CLI instead calls `_prepare_recapture` and writes `prepared.git_rows` itself at cli.py:6691; this 35-line implementation and its `__all__` export remain after that split. The sole test reference merely asserts autopush does not mention its name. No matching current backlog item found.
- **Hypothesis (untested):** Delete the unreachable writer and its export, retaining `_prepare_recapture` and the exercised CLI orchestration as the sole recapture path. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/events_tail.py:1156-1190
- **Context:** From /full-review cluster "Unused helpers survived completed refactors" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=nice-to-have] Remove the orphan host model-id validator
- **Description:** `_model_id` has no reference anywhere in the repository outside its definition, including tests, exports, and documentation; the live readers use their own current record-specific extraction paths. It is a leftover private validator with no consumer. Track 49A already owns neighboring reader/leaf-helper cleanup, but does not name this dead helper.
- **Hypothesis (untested):** Remove `_model_id` during the already-planned Track 49A cleanup after reconfirming its zero-call-site status. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/host_usage.py:1283-1288
- **Context:** From /full-review cluster "Unused helpers survived completed refactors" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

### [full-review:severity=nice-to-have] Delete the unused retro event iterator
- **Description:** `_read_events` is unused after `aggregate` began listing event files once, deriving the coverage floor, and materializing rows directly at lines 2636-2640. No caller or test imports this private function, while the events-retro invariant still attributes single-glob behavior to it; the duplicate entry point obscures which path the read-once test actually covers. No matching current backlog item found.
- **Hypothesis (untested):** Delete the orphan iterator and correct invariant references to the live single-glob/materialization path without changing the one-read behavior. — re-investigate before implementing; the reviewer did not verify this direction.
- **Found in:** src/mind_meld/skills/retro_fleet/aggregator.py:799-813
- **Context:** From /full-review cluster "Unused helpers survived completed refactors" on branch kbitz/full-review-v2 (2026-09-05 UTC), reviewed commit 8be81ce.
- **Effort:** ? (user triages in /roadmap)

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
