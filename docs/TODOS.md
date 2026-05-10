# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

### [full-review:necessary,files=tests/conftest.py] Token-cache test isolation: missing autouse fixture
- **Description:** Autouse isolation fixtures exist for `identity.CACHE_PATH`, `pullhistory.HISTORY_DIR`, `MM_EVENTS_DIR`, `MM_RETROS_DIR`, `devices.DEVICES_WRITE_LOCK`, and the keyring — but NOT for `token_usage.CACHE_PATH`. Tests that drive `_run_events_backfill` / `_push_core` (e.g. `test_init_events_backfill`, integration push paths) call `warm_token_cache_inline` and `lock_and_get_files("block")` against the user's real `~/.config/mind-meld/session-tokens.json`, polluting it with phantom-device entries and reading whatever was previously cached there. Per-file fixture in `test_token_usage.py` is the only defense.
- **Hypothesis (untested):** Add an autouse `_isolate_token_cache` fixture in `tests/conftest.py` mirroring the `_isolate_identity_cache` shape (`monkeypatch.setattr(token_usage, "CACHE_PATH", tmp_path / "session-tokens.json")` + reset `_WARNED_UNKNOWN_MODELS`); then drop the redundant per-file fixture in `test_token_usage.py` — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** tests/conftest.py:1-175
- **Context:** From /full-review cluster "Token-cache invariant ownership gaps" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/token_usage.py] gc_cache_entries bypasses lock_and_get_files wrapper
- **Description:** `gc_cache_entries` (line 938-978) re-rolls the `locked_json_rmw + version-check + isinstance-check + files-dict` boilerplate that `lock_and_get_files` (same file, line 755) was extracted in v0.11.24 to consolidate. The `lock_and_get_files` docstring explicitly says it "Replaces the locked_json_rmw + version-check + isinstance-check + ljson.data['files'] boilerplate at every cache call site." `warm_token_cache_inline` (line 879) routes through it; `gc_cache_entries` does not. The wrapper's "single owner of cache-shape invariants" claim is now false.
- **Hypothesis (untested):** Refactor `gc_cache_entries` to use `lock_and_get_files("block")` so the cache-shape ownership invariant has a single owner; OR document why GC needs raw access (the keep/drop replacement of `cache["files"]` may justify the deviation, in which case add a comment) — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/token_usage.py:938-978
- **Context:** From /full-review cluster "Token-cache invariant ownership gaps" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/events.py] skills_by_day cold-cache contract violation (D4 discriminator broken)
- **Description:** When `_run_events_tail` runs autopush on a cold token cache with no upgrade transition, `_decide_token_walk_policy` returns False and `walk_session_metadata` is called with `token_cache_files=None`. `_scan_one_project` then skips the `meta["skills_by_day"] = skills_by_day` assignment entirely, so the snapshot row goes out with no `skills_by_day` key on a v0.11.27+ device. The aggregator's `pre_skills_peers` test (`if "skills_by_day" not in proj`) misclassifies this device as a pre-v0.11.27 peer. CLAUDE.md flags D4 as load-bearing: KEY-ABSENT vs EMPTY-DICT semantics are the discriminator. The matching `pre_token_peers` set bundles "pre-v0.11.14 OR cold cache" honestly; `pre_skills_peers` does not.
- **Hypothesis (untested):** Always set `meta["skills_by_day"] = {}` in `_scan_one_project` regardless of `token_cache_files` (subtraction-first: drop the conditional gate around the assignment), OR thread a separate "this peer is on v0.11.27+ schema but skipped the walk" signal that the aggregator's Note can phrase honestly mirroring the token Note — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/events.py:848-858
- **Context:** From /full-review cluster "skills_by_day cold-cache contract violation" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] _run_events_tail / _run_events_backfill 90% structurally duplicated
- **Description:** `_run_events_tail` (cli.py:2814-...) and `_run_events_backfill` are 90% structurally identical: same gate on `mm-events`, same deadline math, same claude_paths walk, same agg_projects collection, same s_rows construction. The only material difference is whether an mm-push row is written and whether identity-cache warm runs at the end. ~80 lines reducible. Future bug fixes in this area (e.g. the skills_by_day key-absence finding above) need to land in two places.
- **Hypothesis (untested):** Extract a shared `_capture_events_snapshot(...)` helper that returns `(g_rows, s_rows)` and have both call sites assemble their own write-list — OR fold the backfill behavior into a `mode={"tail","backfill"}` parameter on a single function — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2814-3041
- **Context:** From /full-review cluster "events-tail/backfill duplication + push integration" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Duplicated walk loop branches differ only by token_cache_files arg
- **Description:** Inside `_run_events_tail`, the two branches under `if do_token_walk` / `else` differ only in whether `token_cache_files=files_dict` or `token_cache_files=None` is passed to `walk_session_metadata`; the loop body is otherwise byte-identical.
- **Hypothesis (untested):** Lift `files_dict` resolution above the loop using a `with token_usage.lock_and_get_files(...) as files_dict if do_token_walk else nullcontext(None)` pattern and have one walk loop — drops ~10 duplicated lines — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2877-2894
- **Context:** From /full-review cluster "events-tail/backfill duplication + push integration" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Substantive-change gate timing: events tail skipped on no-source-change days
- **Description:** The substantive-change gate at v0.12.2 is computed BEFORE `_run_events_tail` runs, so the gate sees the local manifest WITHOUT today's mm-push event row. After the daily filename rolls over (UTC midnight) and the user has zero source changes, the first push of the new day will skip the events tail entirely — no `mm-push` row ever lands for that day. The retro window will still see the device as having pushed (via the prior day's row) and the cursor stays at the prior `last_push_ts` so dedup re-walks correctly. But there is no longer any "I'm alive on day N" signal from a fleet member who pushed nothing for several days — only that they were active on day N-K. Breaks the daily forensic trail across no-op-push days.
- **Hypothesis (untested):** Verify whether monitoring/retro logic depends on a daily mm-push row to detect "machine alive but quiet"; if it does, exempt mm-push-only-rolls from the substantive-change gate (e.g., write a heartbeat row when last_push_ts is more than 24h old). Subtraction-first: confirm via tests whether anything actually depends on the daily row, then choose to either (a) document that no-op pushes do NOT advance the cursor or (b) lift the gate when the cursor is stale — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:3104-3174
- **Context:** From /full-review cluster "events-tail/backfill duplication + push integration" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Source re-walk after _run_events_tail wasted on tail-failure path
- **Description:** After `_run_events_tail` writes the new mm-events row, `_push_core` re-walks `mm_internal_cfgs` and updates `local_manifest["sources"]`. If `_run_events_tail` failed silently (caught in its outer try/except), the re-walk still runs against the same on-disk events tree as before — no events row to upload, but the re-walk and tombstone regeneration are wasted work. Cheap enough to not matter today, but the re-walk isn't gated on the tail succeeding.
- **Hypothesis (untested):** Make `_run_events_tail` return a bool indicating whether it wrote (or threw); skip the re-walk when False. Subtraction-first: confirm the cost is below noise via wall-clock measurement before adding the signal — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:3180-3191
- **Context:** From /full-review cluster "events-tail/backfill duplication + push integration" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Identity gather inside events-tail budget on cold cache
- **Description:** `_run_events_tail` calls `identity.gather_local_identities(allow_refresh=True)` unconditionally inside the events tail's try-block. On a fresh-fleet machine where the cache is stale (>7d) AND the user is on autopush (`quiet=True`), this triggers a synchronous subprocess gather on every autopush hook for one push — `gh api user` 3s timeout, `git config` 5s budget. The resulting wall-clock can blow past the events-tail's 250ms budget by 30x. CLAUDE.md/identity.py docs frame the gather as "user explicitly accepted the one-off slow path" — but autopush is a hot path that runs on every Claude Code session.
- **Hypothesis (untested):** Verify whether autopush-context calls should pass `allow_refresh=False` so cold-cache autopush emits an empty `local_emails: []` and lets the next interactive push do the refresh. The fleet aggregator's mixed-fleet code already tolerates pre-v0.11.17 peers with no field, so an empty list under cold-cache autopush is a legitimate "I have nothing to contribute right now" — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2916-2923
- **Context:** From /full-review cluster "events-tail wall-clock budget collisions on cold caches" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Misleading 'events tail budget exceeded' notice on cold-identity-cache pushes
- **Description:** The events tail's identity gather is the only place `gather_local_identities(allow_refresh=True)` is called from a wall-clock-budgeted path. The 1ms hot path is fine but the 10s cold path collides with the "events tail budget exceeded" notice — the user sees both notices on the same push: "refreshing identity cache (one-off)" then "events tail budget exceeded." The second notice is misleading because the budget overrun was the identity gather, not the git/sessions walks.
- **Hypothesis (untested):** Move the identity gather BEFORE the budget computation, or make the budget exclude the identity-gather wall-clock. Subtraction-first: confirm whether anyone monitors the "budget exceeded" notice for actionable signal — if not, just document the false-positive on cold-identity-cache pushes — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2916
- **Context:** From /full-review cluster "events-tail wall-clock budget collisions on cold caches" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/identity.py] _do_full_gather has no overall deadline; per-step budgets compound
- **Description:** `_do_full_gather` runs subprocesses sequentially (global git config → per-repo git configs with 5s budget → config.toml read → gh api user with 3s timeout). The total wall-clock can easily exceed 10s on a cold network. There is no overall deadline — the per-step budgets are independent. A user on a slow network watching `mm refresh-identity` could see ~10s of stalled output.
- **Hypothesis (untested):** Add a top-level `_FULL_GATHER_BUDGET_S` (e.g. 8s) and short-circuit remaining sources when the budget elapses. `gh api user` is the most likely outlier (network-bound) and could move BEFORE the per-repo loop so a slow gh doesn't starve the per-repo budget — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/identity.py:248-261
- **Context:** From /full-review cluster "events-tail wall-clock budget collisions on cold caches" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/token_usage.py] _aggregate_jsonl_views_for_project missing per-jsonl deadline check
- **Description:** `_aggregate_jsonl_views_for_project` merges each jsonl's day buckets sequentially without checking the deadline between jsonls. A pathological project with many large jsonls AND a cold cache could blow the events-tail wall-clock budget BEFORE `walk_session_metadata`'s project-level deadline check fires. The deadline is passed to `get_or_compute` but only short-circuits when the deadline is already exceeded ON ENTRY — a long-running walk continues to completion.
- **Hypothesis (untested):** Either add a deadline check inside the merge loop in `_aggregate_jsonl_views_for_project`, or reduce the per-jsonl walk budget when cumulative time elapsed approaches the deadline. The current per-project deadline check at the top of `walk_session_metadata` only fires between projects, so one large project can still exceed the budget — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/token_usage.py:879-908
- **Context:** From /full-review cluster "events-tail wall-clock budget collisions on cold caches" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/upgrade.py] Dead fsutil import preserved with placeholder rebinding
- **Description:** `fsutil` import on line 46 is unused; the `_ = fsutil` rebinding on line 541 with comment "kept available for future atomic-write needs" is YAGNI dead code. The comment admits no current use.
- **Hypothesis (untested):** Delete both lines; re-importing when a real call site appears is one line — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/upgrade.py:46, 539-541
- **Context:** From /full-review cluster "cli.py micro-cleanups" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] _download_and_apply rebuilds outcomes dict literal instead of using existing _empty_outcomes() helper
- **Description:** `_download_and_apply` constructs the outcomes dict literal inline at line 1745 instead of calling the existing `_empty_outcomes()` helper (defined for the same purpose at line 3647).
- **Hypothesis (untested):** Replace the 8-line literal with `outcomes: dict[ApplyOutcome, list[str]] = _empty_outcomes()` — direct deletion, no new helper needed (helper exists) — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:1745-1753
- **Context:** From /full-review cluster "cli.py micro-cleanups" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Local re-imports of already-module-imported modules at 8 sites
- **Description:** Local re-imports of `json` (6 sites as `import json as _json`), `secrets` (1 site as `import secrets as _secrets`), `datetime/timezone` (1 site), and `hashlib` (1 site) — all four modules are already imported at module scope.
- **Hypothesis (untested):** Delete the local re-imports and use the module-level names; if there's a deliberate reason for shadowing (e.g. `_json` to disambiguate), the absence of a comment suggests carelessness. Verify no name shadowing was intended before deleting — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:4432, 4580, 4722, 5922, 7094, 7163, 6344-6345, 880
- **Context:** From /full-review cluster "cli.py micro-cleanups" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] mm_events_src lookup duplicated at 3 call sites
- **Description:** `mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)` is duplicated at three call sites; `events_dir = Path(mm_events_src["path"]).expanduser() / "events"` is also duplicated.
- **Hypothesis (untested):** Extract `_resolve_mm_events_dir(sources) -> Path | None` returning the events dir or None when source is absent — single call replaces both lookups at every site — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2837, 2958, 6980
- **Context:** From /full-review cluster "cli.py micro-cleanups" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] install_skills_cmd post-check duplicates _ensure_retro_skill_link state machine
- **Description:** `install_skills_cmd` re-checks the symlink state after calling `_ensure_retro_skill_link`, duplicating ~15 lines of state-machine logic (`is_symlink`/`exists`/`resolve == skill_src.resolve()`) that already exists inside `_ensure_retro_skill_link`'s five-branch state machine.
- **Hypothesis (untested):** Have `_ensure_retro_skill_link` return a status enum (or raise typed exceptions for conflict cases) so `install_skills_cmd` reads the result directly instead of re-walking the filesystem; alternatively delete `install_skills_cmd`'s post-check (the helper already emits notices) — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:5675-5696
- **Context:** From /full-review cluster "cli.py micro-cleanups" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Skill-link marker filenames mix dot-prefix in non-dot config dir
- **Description:** `_ensure_retro_skill_link` and the marker helpers use `_config_dir()` which returns `~/.config/mind-meld`. But the marker filenames have a leading dot (`.skill-link-checked`) inside the un-dotted dir. Mixing dot-prefixed marker files with non-dot config files (`identity-cache.json`, `migration-state.json`, etc.) is inconsistent. Glob commands like `ls ~/.config/mind-meld/*.json` will not list these markers; `ls ~/.config/mind-meld/` will list them sorted at the top.
- **Hypothesis (untested):** Either drop the leading dot (consistent with sibling files) or move markers into a sub-dir like `~/.config/mind-meld/markers/`. Subtraction-first: confirm whether anyone scripts against these markers — if not, leave the dot-prefix as a "this is internal state" signal but document — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2557-2706
- **Context:** From /full-review cluster "cli.py micro-cleanups" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/events.py] walk_git_projects failure breadcrumb missing safe_str(e)
- **Description:** `walk_git_projects` whole-walk failure breadcrumb formats `{e}` directly into stderr, breaking the established `safe_str(e)` pattern used at every other `mm: notice:` site (cli.py:2931, 2808, 2986, 3035, 3041, 5015 all wrap with `safe_str`). Exception strings here come from local subprocess wrappers so peer-escape risk is low, but it's a divergence from the event-tail's own contract.
- **Hypothesis (untested):** Import `safe_str` from `mind_meld.safety` and wrap the `{e}` interpolation; the file already imports `sys`, the cost is one import line — re-investigate before implementing; the consistency-auditor agent did not verify this direction.
- **Found in:** src/mind_meld/events.py:562-565
- **Context:** From /full-review cluster "safe_str discipline drift on stderr exceptions" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] _ensure_device_registered uses Rich stderr_console.print + unsanitized {e}
- **Description:** `_ensure_device_registered` emits its `mm: warning:` via `stderr_console.print(...)` (Rich), while every other `mm: warning:` and `mm: notice:` in the file uses raw `print(..., file=sys.stderr)` or `sys.stderr.write(...)`. Rich interprets `[red]…[/red]` markup tokens in the formatted string, and the embedded `{e}` is not `safe_str`-defended — a peer-controlled exception text containing `[` would be silently mangled or interpreted as Rich markup.
- **Hypothesis (untested):** Switch this single site to `print(f"mm: warning: device entry self-heal failed ({type(e).__name__}): {safe_str(e)}", file=sys.stderr)` to match the surrounding warning sites; verify the `raise` re-throw remains intentional — re-investigate before implementing; the consistency-auditor agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2548-2553
- **Context:** From /full-review cluster "safe_str discipline drift on stderr exceptions" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/config.py] _bootstrap_mm_events_path missing safe_str(e)
- **Description:** `_bootstrap_mm_events_path` writes a `mm: warning: ... ({type(e).__name__}: {e})` to stderr without `safe_str(e)`. The exception comes from a local `mkdir`, so peer-control is not a concern, but every other `mm: warning:` in cli.py that interpolates exception text either uses a sanitized `msg` constant or `safe_str(e)`.
- **Hypothesis (untested):** Add `from mind_meld.safety import safe_str` and wrap `{e}` to bring this file in line with the established contract; the `safety.py` import is cheap — re-investigate before implementing; the consistency-auditor agent did not verify this direction.
- **Found in:** src/mind_meld/config.py:400-407
- **Context:** From /full-review cluster "safe_str discipline drift on stderr exceptions" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/identity.py] _persist_or_yield_concurrent and _persist_force 90% identical
- **Description:** `_persist_or_yield_concurrent` and `_persist_force` are 90% identical — both open the lock, set `version`/`refreshed_at`/`emails`, return list. The only branch difference is `_persist_or_yield_concurrent`'s "use theirs if fresh" check.
- **Hypothesis (untested):** Replace with one helper `_persist(emails, *, force: bool)` that branches on `force` for the concurrent-yield path; saves ~15 lines — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/identity.py:169-203
- **Context:** From /full-review cluster "identity.py micro-DRY" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/identity.py] _do_full_gather loads config twice per refresh
- **Description:** Both `_gather_per_repo_emails` and `_gather_config_author_emails` independently `from mind_meld.config import CONFIG_PATH, load_config` and call `load_config(CONFIG_PATH)` — `_do_full_gather()` ends up loading config twice per refresh.
- **Hypothesis (untested):** Have `_do_full_gather` load config once and pass the dict (or None on failure) to both gather helpers; minor performance win, clearer ownership — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/identity.py:292-301, 333-340
- **Context:** From /full-review cluster "identity.py micro-DRY" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=docs/invariants/events-retro.md] Stale aggregator symbol references in events-retro invariant doc
- **Description:** Doc references `aggregator._read_config_author_emails` / `_per_repo_user_emails` / `_gh_noreply_email` — none of these names exist in the aggregator anymore (they were moved to `identity.py` with different names in v0.11.17). The doc says tests "now monkeypatch the identity-side equivalents" but the dead-name list itself is misleading.
- **Hypothesis (untested):** Update the dead-name list to current `identity.py` symbol names, or remove the parenthetical entirely (the surrounding text already says tests monkeypatch identity-side equivalents) — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** docs/invariants/events-retro.md:286
- **Context:** From /full-review cluster "Documentation drift around v0.11.17 identity extraction" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/skills/retro_fleet/aggregator.py] Stale docstring reference to nonexistent _read_config_author_emails
- **Description:** Docstring of `_read_mm_events_config_path` references `_read_config_author_emails` which no longer exists in this module (it was moved into `identity.py` as `_gather_config_author_emails` in v0.11.17). Stale doc reference will mislead the next reader.
- **Hypothesis (untested):** Rename the docstring reference to `identity._gather_config_author_emails` or simply describe the pattern inline ("tolerant config reader; never raises") — re-investigate before implementing; the hygiene agent did not verify this direction.
- **Found in:** src/mind_meld/skills/retro_fleet/aggregator.py:2164
- **Context:** From /full-review cluster "Documentation drift around v0.11.17 identity extraction" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/events.py] walk_session_metadata since parameter unused with noqa-ARG001
- **Description:** `walk_session_metadata` accepts `since: datetime` purely "for API stability" with `# noqa: ARG001`, but every caller in cli.py still computes a since via `last_push_ts` and passes it. The unused parameter is misleading — a future maintainer adding delta-semantics back will need to thread the value through `_scan_one_project` (currently doesn't accept it). The CLAUDE.md "v=3 schema can re-introduce delta semantics" comment encodes the intent, but the dead parameter makes the call sites look meaningful.
- **Hypothesis (untested):** Either remove the `since` parameter entirely (callers stop computing it for the sessions walk; cli.py's `_run_events_tail` uses `since` only for `walk_git_projects` so the variable is still needed locally) OR rename to `_unused_since` per the v=3-future-defense comment to make the no-op explicit. Subtraction first — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/events.py:700-771
- **Context:** From /full-review cluster "Documentation drift around v0.11.17 identity extraction" on branch kbitz/track-10a-token-dry (2026-05-10).
- **Effort:** ? (user triages in /roadmap)
