# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Unprocessed

- **Grok bounded scan has no mid-file checkpoint and wedges below a budget floor.** `_validated_grok_entry` requires `offset == size` (`host_usage.py:753`), so a cache entry exists only for a FULLY parsed file — a ledger that cannot be read end-to-end inside one budget discards all its work, forever. Measured 2026-08-27 on the live 82-ledger / 244 MB corpus with Track 31A's fixes applied: 250 ms converges in 3 passes, 100 ms in 8, **60 ms wedges permanently at 15 files (59 passes, zero progress)**, 40/20 ms wedge at 1 file. Largest ledger is 34.1 MB ~= 89 ms at 2.6 ms/MB. Safe today (Codex warm ~37 ms leaves Grok ~213 ms of the 250 ms autopush budget) but both sides of `remaining_budget < cost_of_next_uncached_file` drift the wrong way as corpora grow, and an autopush-only Mac would then never publish Grok — with the omission declared as a degraded breadcrumb. Same pathology `token_usage.walk_jsonl_segment` + persisted `offset` fixed for Claude in v0.12.15. Changes the cache-entry shape, so it belongs with Track 32A's cache work. Track 31A pins the floor with a convergence test instead. _Source: Track 31A /autoplan Phase 3 Eng E5, 2026-08-27._
- **Grok consent is an indivisible bundle in both directions.** No CLI path yields sync-without-usage-reading or usage-reading-without-sync: `mm enable-source grok` force-sets `[retro].grok_host_usage = true`, `_set_grok_host_usage` is private with every caller `quiet=True`, and the only lever is hand-editing a key that appears nowhere in README. The decomposed pattern already ships one feature over (`mm install-skills --agent <key>`) and is advertised in the same docstring that bundles Grok. Wants `mm enable-source grok --no-usage` / `mm enable-usage grok` / `mm disable-usage grok`. _Source: Track 31A /autoplan Phase 3.5 DX-3, 2026-08-27._
- **`--dump-host-usage` is the subsystem's only forensic tool and is invisible.** `hidden=True` (`cli.py:6208`) + `argparse.SUPPRESS` (`aggregator.py:3586`), absent from README's command table, and structurally cannot report a failure because `consulted` lists contributors, not attempts. Either promote it (unhide, document, note that `consulted` means *contributed*) or fold its job into `mm diag`'s host-usage section. _Source: Track 31A /autoplan Phase 3.5 DX-9, 2026-08-27._
- **Rich markup eats TOML brackets in command help.** Verified live: `mm enable-source --help` renders `[sync].disabled_sources` as `.disabled_sources` and `[[sync.sources]]` as `[]`; `mm --help` renders migrate-config's `exclude_patterns` target as `[]`. Rich parses `[sync.sources]` as a style tag. The one help text carrying the Grok consent disclosure is affected. Fix by escaping the brackets or setting `rich_markup_mode="markdown"`, then audit every docstring containing `[`. _Source: Track 31A /autoplan Phase 3.5 DX-12, 2026-08-27._
- **An empty host-usage row can overwrite a populated one under strict latest-wins.** `aggregate_host_usage` / `_row_replaces` (`aggregator.py:1436`) compare only `as_of` then `tie_key`, with no "an empty must not beat a populated" guard. Live evidence 38 seconds apart on device `3a6c7dc9`: an empty row at 19:15:07 and a populated codex row (43 days) at 19:15:45 — reversed ordering would have erased the populated one. This is the same failure CLAUDE.md documents for `pre_skills_peers` in `aggregate_sessions`, unguarded on the host path. Track 31A adds a fourth cold cache to the sweep and raises its probability. _Source: Track 31A /autoplan Phase 1 CEO F6, 2026-08-27._
- **`usageIsIncomplete` totals are counted as complete.** 3 of 200 Grok turns carry `usageIsIncomplete: true` and drop `costUsdTicks`; one is the largest turn in the corpus. The reader validates required counter *presence* inside `usage` and never inspects the key set, so these are silently accepted as complete accounting. Track 31A states the caveat in CONTRACT.md and the invariant doc; giving the reader a channel to express partial fidelity needs a coverage state between `complete=True` and `complete=False` that `HostUsageResult` does not have. Route with Track 33A/35A coverage work. _Source: Track 31A /autoplan Phase 3 Eng E11, 2026-08-27._

- **E6 (cut from Track 30A, 2026-08-26).** The retro card itself does not name per-device capture gaps. E0–E5 put the new signals on `mm status` / `mm diag`; the card's only git-capture note is `zero_repo_captures`, windowed by the event row's timestamp, so a gap banked three weeks ago is silent on a `7d` card. `git_capture.since` makes uncovered `[since, ts]` intervals computable with no extra wire. Requires a SKILL.md decoder entry. Do not auto-run `mm recapture` from the skill. _Source: Track 30A E6, cut explicitly rather than by omission._
- **Recapture rows pollute the aggregator's zero-capture note.** `aggregate_git` counts git-snapshot rows as *pushes* (`aggregator.py` snap_total / snap_zero), but recapture writes those rows with no mm-push row (marked `origin: recapture`). Needs the aggregator to exclude the marker, or a reworded note. _Source: Track 30A._
- **`WALK_TIME_BUDGET_AUTOPUSH_MS` is the next constant to outgrow its measurement.** At ~60 roots the per-repo timeout hits its 200 ms floor and most repos abort on every push. E1 makes that loud. _Source: Track 30A._
- **`~/.claude/projects` growth makes discovery linear in candidate count.** At ~600 candidates it starts exceeding the 50 ms budget, which would pin the cursor at the floor permanently. _Source: Track 30A._
- **N6 / subdirectory recovery.** A session started in `repo/src` drops `repo`. `_classify_git_root` (Track 29A) replaced `_is_git_toplevel`, which used to compute the git toplevel and then discarded it. Measured 0 of 60 candidates at review time, so latent. The cheap fix is foreclosed: recovery now needs a bounded parent walk with a ceiling. Route to Group 30 alongside the cursor / work-bounds work. _Source: Track 29A /autoplan Phase 3 Eng, 2026-08-25._
- **S2 — git environment not scrubbed.** `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_COMMON_DIR` are inherited by `_walk_one_repo` and `_origin_remote_url`. After Track 29A, classification ignores the env while `git log` still honours it, so every `.git`-bearing candidate can emit the env repo's `(canonical_remote, sha)` paired with its own `local_path` — the aggregator's exact dedup key. Exotic, but `autopush` runs from a hook whose environment mm does not control. _Source: Track 29A /autoplan Phase 3 Eng, 2026-08-25._
- **No recapture path.** DISCHARGED in Track 30A (v0.12.46) as `mm recapture [WINDOW]`, not `mm push --recapture`. The push flag was rejected because it would re-open the v0.12.2 phantom-change gate. _Source: Track 29A /autoplan Phase 3.5 DX, 2026-08-25; discharged 2026-08-26._
- **`~/.claude/projects` grows unbounded.** 88 dirs on the review machine, 30 with no jsonl, 50 of 60 candidates permanently dead. `_probe_claude` is ~95% of post-fix discovery cost and scales with this. mm does not own the directory, so a reaper is refused; a negative-result cache is the v0.12.15-shaped answer if it ever binds. _Source: Track 29A /autoplan Phase 3 Eng H4, 2026-08-25._
- **`mm diag`'s Rich renderer reflows long paths mid-path**, so its "optimized for support-chat paste" claim (`cli.py` diag text path) is already false for paths. _Source: Track 29A /autoplan Phase 3.5 DX, 2026-08-25._
- **X6 — `mm diag` should report last-push recorded capture alongside the fresh probe.** DISCHARGED in Track 30A (v0.12.46) as the `git_capture` recorded/fresh block. _Source: Track 29A FINAL GATE, 2026-08-25; discharged 2026-08-26._

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

_Last updated 2026-08-25 by `/roadmap`._
