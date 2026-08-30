# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Unprocessed


- **The retro aggregator reads synced event lines with no size bound.** `aggregator._iter_event_objects` (the reader added in v0.11.6, `bae6f7c`) iterates `for line in f` and `json.loads` each line in full before ANY validation runs — so a single oversized row is fully decoded before the cardinality caps reject it (measured by Codex: a 2.8 MB / 40,000-model row allocated 10.6 MB during parsing). This predates Track 33A and applies to EVERY field on EVERY row type (`hosts`, `projects`, `skills_by_day`), not just the new sibling — a peer could already do it with a giant `hosts` map. The repo already has bounded readers for exactly this (`token_usage.iter_bounded_lines`, `pullhistory._yield_lines`); the aggregator never got one. Threat model is a compromised peer inside the user's own E2E-encrypted fleet, which is why this is not a release blocker. Fixing it touches a shared read path all consumers use, so it wants its own Track. _Source: Track 33A Codex adversarial review pass 2, 2026-08-30._
- **Host per-model materialization is unbounded until the snapshot cap runs.** `host_usage._add_usage` materializes every distinct model the local corpus has ever seen into `by_day[day]["by_model"]`; the 32/day and 64/row caps only apply later, in `events._cap_by_model`. Track 33A bounded the exposure to the 90-day window (the cap now runs after the day trim), so what remains is per-day model count on a corrupt or rapidly-rotating local cache — memory and CPU inside a 250 ms autopush budget. Capping in the reader is NOT the fix: it would break the "day totals stay whole" invariant that makes `day_total - sum(by_model)` an honest residual. Wants a bound on the cache's interned `models` table instead. _Source: Track 33A Codex adversarial review finding 6, 2026-08-30._
- **Group 32 is still in `docs/ROADMAP.md` `## Current Plan` though v0.12.48 shipped it.** Track 32A merged as PR #149 on 2026-08-28 (CI green on `d0efbbf`) and all four card tasks are present in `host_usage.py`. The v0.12.48 PR's roadmap regen ran *inside* the same PR, so it recorded Groups 29-31 as shipped and structurally could not record 32. Needs a `/roadmap` pass to move Group 32 to `docs/roadmap-shipped.md`. _Source: Track 33A `/autoplan` gate D1, 2026-08-28._
- **Track 34A's `blocked-by: Track 33A` edge is unjustified by 34A's task content.** Flagged independently by both `/autoplan` voices. 34A task 1 renders `degraded_sources` (on the wire since v0.12.47); task 2 adds a coverage state between `complete=True/False` on `HostUsageResult`. Neither reads per-model data. Both Tracks declare `pyproject.toml` so they serialize regardless, but `blocked-by` asserts a direction the tasks do not support — the packer appears to emit the edge from Group numbering. The user ruled 33A ships first; this is filed for `/roadmap` to re-derive the edge, not to reorder anything now. The repo's standing "card premise is checked against HEAD at drain time" constraint should extend to DAG edges. _Source: Track 33A `/autoplan` Phase 1 CEO, 2026-08-28._
- **`_tier()` derives cache rates as Anthropic multiples, which is a Group 35 trap.** Its docstring: *"**Anthropic** publishes input and output per-MTok; cache read and cache write are fixed multiples of input."* Grok emits real `cacheCreationTokens` and xAI does not bill cache writes at 2x input. Group 35 must write literal four-field `PRICING` overrides for `gpt-*` / `grok-*` rather than reaching for `_tier`, or it will misprice non-Anthropic cache tokens. `resolve_prices` stays the sole priced-predicate and `model_family` stays a positional allowlist match. _Source: Track 33A `/autoplan` Phase 1 CEO F10, 2026-08-28._
- **A `--demo` flag over a bundled synthetic corpus would take feature TTHW from 10-30 min to ~3 min.** Measured during Track 33A's DX pass: seeing per-model host data today needs install → `mm init` → enable a host source → an interactive `mm push` to warm → a substantive push → a flag that is not in `--help` or README. Track 33A's own test work builds most of the fixture (a synthetic two-model Grok corpus and a full `make_host_usage_snapshot` → `write_push_event` → `aggregate_host_usage` → `_dump_host_inventory` round-trip), so once those land this is Group-34-sized. `docs/roadmap-future.md` already carries the request. _Source: Track 33A `/autoplan` Phase 3.5 DX, 2026-08-28._
- **E6 (cut from Track 30A, 2026-08-26).** The retro card itself does not name per-device capture gaps. E0–E5 put the new signals on `mm status` / `mm diag`; the card's only git-capture note is `zero_repo_captures`, windowed by the event row's timestamp, so a gap banked three weeks ago is silent on a `7d` card. `git_capture.since` makes uncovered `[since, ts]` intervals computable with no extra wire. Requires a SKILL.md decoder entry. Do not auto-run `mm recapture` from the skill. _Source: Track 30A E6, cut explicitly rather than by omission._
- **Recapture rows pollute the aggregator's zero-capture note.** `aggregate_git` counts git-snapshot rows as *pushes* (`aggregator.py` snap_total / snap_zero), but recapture writes those rows with no mm-push row (marked `origin: recapture`). Needs the aggregator to exclude the marker, or a reworded note. _Source: Track 30A._
- **`WALK_TIME_BUDGET_AUTOPUSH_MS` is the next constant to outgrow its measurement.** At ~60 roots the per-repo timeout hits its 200 ms floor and most repos abort on every push. E1 makes that loud. _Source: Track 30A._
- **`~/.claude/projects` growth makes discovery linear in candidate count.** At ~600 candidates it starts exceeding the 50 ms budget, which would pin the cursor at the floor permanently. _Source: Track 30A._
- **N6 / subdirectory recovery.** A session started in `repo/src` drops `repo`. `_classify_git_root` (Track 29A) replaced `_is_git_toplevel`, which used to compute the git toplevel and then discarded it. Measured 0 of 60 candidates at review time, so latent. The cheap fix is foreclosed: recovery now needs a bounded parent walk with a ceiling. Route to Group 30 alongside the cursor / work-bounds work. _Source: Track 29A /autoplan Phase 3 Eng, 2026-08-25._
- **S2 — git environment not scrubbed.** `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_COMMON_DIR` are inherited by `_walk_one_repo` and `_origin_remote_url`. After Track 29A, classification ignores the env while `git log` still honours it, so every `.git`-bearing candidate can emit the env repo's `(canonical_remote, sha)` paired with its own `local_path` — the aggregator's exact dedup key. Exotic, but `autopush` runs from a hook whose environment mm does not control. _Source: Track 29A /autoplan Phase 3 Eng, 2026-08-25._
- **`~/.claude/projects` grows unbounded.** 88 dirs on the review machine, 30 with no jsonl, 50 of 60 candidates permanently dead. `_probe_claude` is ~95% of post-fix discovery cost and scales with this. mm does not own the directory, so a reaper is refused; a negative-result cache is the v0.12.15-shaped answer if it ever binds. _Source: Track 29A /autoplan Phase 3 Eng H4, 2026-08-25._
- **`mm diag`'s Rich renderer reflows long paths mid-path**, so its "optimized for support-chat paste" claim (`cli.py` diag text path) is already false for paths. _Source: Track 29A /autoplan Phase 3.5 DX, 2026-08-25._

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
