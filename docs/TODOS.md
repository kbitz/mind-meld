# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Unprocessed

<!-- /full-review 2026-08-14 (branch kbitz/full-review-v1 @ 901e80e) — 50 findings, 8 clusters.
     Hypotheses below are UNTESTED reviewer-agent directions, not verified fixes. -->

### [full-review:critical,files=src/mind_meld/events.py] One invalid UTF-8 byte silences the entire events tail, permanently

- **Description:** `_read_cwd_from_latest_jsonl` opens Claude Code session jsonls in TEXT mode (`open(jl, encoding="utf-8")`) and catches only `OSError`. A single invalid UTF-8 byte raises `UnicodeDecodeError` (a `ValueError`), which escapes `_scan_one_project`'s `except OSError`, escapes `walk_session_metadata`, and is swallowed by `_run_events_tail`'s forensic wrapper — so the entire events tail (git-snapshot + sessions-snapshot + mm-push cursor row) is lost on every push until that jsonl ages out. The agent reproduced this. It is the identical bug class v0.12.15 fixed by moving `walk_jsonl_segment` to binary mode; the fix reached one of the two readers of the same corpus.
- **Hypothesis (untested):** Read binary (mirroring `walk_jsonl_segment`) or at minimum pass `errors="replace"` and widen the guard to `(OSError, ValueError)`; `events._last_mm_push_ts` and `conflictlog.read_records` share the same text-mode/OSError-only shape and should be checked in the same pass — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/events.py:422-449
- **Context:** From /full-review cluster "Events tail can be silenced by one bad byte" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `discover_git_roots` runs unbudgeted in the events tail, and twice on a cold identity cache

- **Description:** `_run_events_tail` calls `events.discover_git_roots(config)` with no wall-clock budget. That helper runs `_probe_claude` plus one `git rev-parse --show-toplevel` subprocess per deduped candidate at `timeout=2` each, serially — ~107 process spawns on the Mac cited in the v0.12.15 measurements, on every push that uploads bytes, dwarfing the 250ms autopush / 500ms interactive budget. It is structurally invisible to the `events tail budget exceeded` notice because `deadline` is reset after it. On a cold identity cache `identity._gather_per_repo_emails` calls `discover_git_roots` a second time in the same push. `docs/invariants/events-retro.md`'s claim that the identity gather is bounded by "≈ up to 10s" is wrong as written, because `_PER_REPO_BUDGET_S` is only checked in the loop after discovery returns.
- **Hypothesis (untested):** Memoize root discovery per-process so one call serves both the tail and the identity gather, and/or give it the same `total_budget_ms` treatment `walk_git_projects` already has; the memo alone halves the cold-path cost with no new budget machinery — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:3181-3187
- **Context:** From /full-review cluster "Events tail can be silenced by one bad byte" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/events.py] A deadline-truncated walk renders a confident "0 skill invocations"

- **Description:** When the session-walk deadline expires mid-project, `token_usage.get_or_compute` returns `({}, {})` for every not-yet-cached jsonl, yet `_scan_one_project` still sets `meta["skills_by_day"] = {}` because `token_cache_files is not None`. The aggregator's load-bearing D4 discriminator (`"skills_by_day" not in proj`) reads KEY-PRESENT-EMPTY as the content signal "no Skill usage in window" — so a budget-truncated walk renders a confident zero with no `pre_skills_peers` breadcrumb. `docs/invariants/events-retro.md` enumerates exactly two populations for `pre_skills_peers`; deadline exhaustion is an undocumented third, and it is the common case the 250ms autopush budget was designed around. The token side degrades more honestly but still under-reports silently on a partially-walked project. No test covers the propagation.
- **Hypothesis (untested):** Have `_scan_one_project` omit `skills_by_day` (and leave `tokens_by_day` absent) when the deadline was hit for that project, so the existing absent-on-wire breadcrumb covers it — rather than adding a `skills_walk_complete` wire field, which the ROADMAP already defers. Re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/events.py:855-874
- **Context:** From /full-review cluster "Events tail can be silenced by one bad byte" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] `_decide_token_walk_policy` discards the warm-cache counts its callee documents a caller for

- **Description:** `_decide_token_walk_policy` calls `token_usage.warm_token_cache_inline(claude_paths)` and discards the `(walked, skipped)` return value, then returns `True` unconditionally. `warm_token_cache_inline`'s docstring explicitly states "Caller uses the counts to print a one-line user-facing notice when invoked from `mm push`" — no caller does. When the 5s warm budget is exhausted (the exact condition the incremental-resume work was chasing), the user sees `mm: warming token cache (one-time, ~3s)...`, the cache is left partially cold, and the subsequent 500ms session walk silently emits mostly-empty token buckets with no signal that the warm was truncated.
- **Hypothesis (untested):** Either surface the skipped count as a `mm: notice:` (matching the visible-failure contract for degradation signals) or delete the unused return-value contract from the docstring so the two sides stop disagreeing — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:3136-3145
- **Context:** From /full-review cluster "Events tail can be silenced by one bad byte" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Dead `deadline` assignment at the head of both events walkers

- **Description:** `deadline = time.monotonic() + budget_ms / 1000.0` at the head of `_run_events_tail` is dead — nothing reads `deadline` before it is unconditionally reassigned at line 3203 (`walk_git_projects` receives `total_budget_ms` and computes its own deadline internally). A reader following `docs/invariants/events-retro.md` invariant 4, which discusses deadline placement in detail, would reasonably assume the git walk shares this deadline. `_run_events_backfill` carries the identical dead assignment.
- **Hypothesis (untested):** Delete both dead assignments so the single live `deadline` (set after the warm, consumed by the session walk and the `walk_done` comparison) is unambiguous — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:3181 and :3309
- **Context:** From /full-review cluster "Events tail can be silenced by one bad byte" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/config.py] `codex`/`opencode` push a symlinked `AGENTS.md` that the pull path permanently rejects

- **Description:** The new `codex` / `opencode` default sources put `AGENTS.md` in `include_files` at the agent root — exactly the file that host-config tooling (and this project's own documented workflow) symlinks into a config repo. `manifest.walk_generic_source` follows the symlink and PUSHES the content, but `_download_and_apply`'s escape guard resolves the symlink target outside `base_path` and permanently REJECTS it on every pull as "rejected (would escape source root)", counting it in `outcomes["failed"]` — which then trips the autopull `total_failed` stderr warning forever. The agent reproduced this end-to-end: the push manifest contains `AGENTS.md`, and `_download_and_apply` returns `{'failed': ['AGENTS.md']}`. `docs/invariants/sync.md` only reasons about a symlinked source *root*, not a symlinked file *inside* the source.
- **Hypothesis (untested):** Subtraction-first — have the walker skip symlinked entries at push time (never publish a file you structurally cannot apply) rather than adding a symlink carve-out to the escape guard, which would weaken the v0.11.21 traversal defense. Re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/config.py:119-142
- **Context:** From /full-review cluster "New codex/opencode sources push files they cannot pull" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/fsutil.py] `atomic_write_bytes` silently replaces a local symlink with a regular file

- **Description:** `atomic_write_bytes` finishes with `os.replace(tmp_name, path)`, which operates on the link itself, not its target. Any synced path that is locally a symlink pointing inside the source root — or a dangling symlink, which `local_path.exists()` reports as absent so `_apply_write` fires — is silently replaced by a regular file on pull. With `~/.codex/skills`, `~/.codex/plugins`, `~/.config/opencode/skills` and `~/.config/opencode/agents` now in the default sync surface, and mm's own `_ensure_retro_skill_link_at` planting symlinks in exactly those directories, this went from theoretical to routine. There is no `is_symlink` check anywhere in the pull-apply path.
- **Hypothesis (untested):** Have `_apply_incoming_file` refuse (and breadcrumb) when `local_path.is_symlink()`, treating "peer bytes vs. a local symlink" as a conflict rather than a write — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/fsutil.py:129
- **Context:** From /full-review cluster "New codex/opencode sources push files they cannot pull" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/config.py] Both new sources ship empty `exclude_patterns` over locally-generated trees

- **Description:** `codex` and `opencode` ship `exclude_patterns: []` while including directories that three separate local generators write into: mm's own skill-link installers plant a symlink in `skills/`, and `bin/apply` + gstack `./setup --host auto` render host-specific generated copies into the same `skills/` (and `commands/`, `agents/`) trees. Generated content whose bytes depend on the local tool version is definitionally per-machine — this is precisely the churn-conflict class that forced `exclude_patterns` onto the gstack source (`config.yaml`, `repo-mode.json`, `analytics/.last-sync-*`), and it will produce a `.sync-conflict-*` sidecar per file per pull whenever two Macs run different mm/gstack versions.
- **Hypothesis (untested):** Exclude the generated subtrees by default, or narrow `include_dirs` to hand-authored surfaces only, rather than waiting for the first fleet-wide conflict storm to discover which paths are per-machine — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/config.py:126-141
- **Context:** From /full-review cluster "New codex/opencode sources push files they cannot pull" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/config.py] `get_sources` solves source auto-detect three different ways, two with shallow-copy aliasing

- **Description:** Three consecutive blocks in `get_sources` solve the identical "append this default source if its dir exists and it's not already resolved" problem three different ways. gstack (334-350) and gstack-extend (352-365) each hand-roll `next((s for s in DEFAULT_SOURCES ...), None)` plus a shallow `{**default, "path": ...}` spread — which aliases `include_dirs` / `exclude_patterns` back into the module-level `DEFAULT_SOURCES`. The newer codex/opencode block (367-379) uses a tuple-driven loop with `get_default_source()`, which deep-copies — the exact aliasing guard that `tests/test_pull_helpers.py:1801` exists to pin. `get_default_source` has been available since v0.8.7.
- **Hypothesis (untested):** Subtraction-first — delete the two older blocks by folding gstack and gstack-extend into the newer tuple loop, which also removes the shallow-copy aliasing inconsistency; verify the `.resolve()`-avoidance comment at 342-344 still holds through the generic path. Re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/config.py:334-379
- **Context:** From /full-review cluster "New codex/opencode sources push files they cannot pull" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/config.py] `DEFAULT_CODEX_DIR` / `DEFAULT_OPENCODE_DIR` are re-hardcoded 40 lines below their own definition

- **Description:** `DEFAULT_CODEX_DIR` / `DEFAULT_OPENCODE_DIR` were added as named constants and used in `DEFAULT_SOURCES`, but the auto-detect loop 40 lines later re-hardcodes the same paths as `Path.home() / ".codex"` and `Path.home() / ".config" / "opencode"`. Two sources of truth for the same path inside one function; a future path change silently updates the default entry but not the detection.
- **Hypothesis (untested):** Read `Path(DEFAULT_CODEX_DIR).expanduser()` / `Path(DEFAULT_OPENCODE_DIR).expanduser()` in the loop, or derive the path from `get_default_source(name)["path"]` and drop the tuple's second element entirely — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/config.py:27-28, 371-374
- **Context:** From /full-review cluster "New codex/opencode sources push files they cannot pull" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] Three skill-link self-heal gates disagree with the installer they gate

- **Description:** The three per-agent self-heal gates use three different pre-checks and none matches the installer. `_codex_skill_link_check_due` / `_opencode_skill_link_check_due` return False when `target.parent` (the *skills* dir) is missing; `_skill_link_check_due` (claude) has no pre-check at all; `_ensure_retro_skill_link_at` gates on the *agent* dir and then mkdirs the skills dir itself; `install_skills_cmd` uses a fourth form (`target.parent.parent.exists()`). Net effect: a user who installs Codex or OpenCode after mm is already healthy for Claude Code never gets the skill link from `mm push` — not for 24h, permanently. The agent reproduced this. `test_skill_link.py` pins the installer branch and the gate branch separately but never their composition.
- **Hypothesis (untested):** Align all gates on the same existence predicate the installer uses (`agent_dir.exists()`), then collapse the three check-due wrappers into one table-driven `(target, marker)` list so a fifth agent is data, not three more functions — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:3010-3065
- **Context:** From /full-review cluster "retro-fleet skill installer after the three-agent split" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `install_skills_cmd` exits 0 on a partial install

- **Description:** `install_skills_cmd` classifies each available target as either `installed` (correct symlink) or `conflicts` (something else exists). A target whose `symlink_to` failed — `PermissionError` on a read-only agent dir, `ENOTSUP` on an exFAT volume, or a failed skills-dir mkdir — lands in neither bucket; if any other target succeeded, the `if installed: ... if not conflicts: return` branch returns exit 0 and never mentions the failed target. Before the three-agent split the single-target form always reached `mm: error: install did not complete` + `Exit(1)`. The only remaining signal is a `mm: notice:` written earlier in the run, which contradicts the visible-failure contract this command's own error branch implements. The tail also encodes three states across four non-adjacent conditionals.
- **Hypothesis (untested):** Add a third `not_installed` bucket (targets in `available_targets` that ended up in neither list) driving a non-zero exit, and flatten the tail to an explicit three-way branch on `(installed, conflicts, not_installed)` — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:6162-6192
- **Context:** From /full-review cluster "retro-fleet skill installer after the three-agent split" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=tests/conftest.py] Running pytest mutates the developer's real `~/.codex` and `~/.config/opencode`

- **Description:** conftest has autouse `_isolate_*` fixtures for pullhistory, identity cache, token cache, conflictlog, mm-events and retros — but none for the skill-link installer, which now writes into three real user directories and *mkdirs* `~/.codex/skills` and `~/.config/opencode/skills`. `_config_dir()` hardcodes `~/.config/mind-meld` rather than reading `config.CONFIG_DIR`, so the 24h markers land in the real config dir too. `tests/test_integration.py` has ~59 un-stubbed `mm init` / `mm push` CliRunner invocations and patches `HOME` in a single test; only `test_init_auto_pin.py` and `test_init_events_backfill.py` stub `_ensure_retro_skill_links`. This breaks the established "one autouse `_isolate_*` fixture per path mm writes under `~`" pattern.
- **Hypothesis (untested):** An autouse `_isolate_skill_links` fixture (stubbing `_ensure_retro_skill_links` + redirecting `_config_dir`) is likely the smallest fix; consider deleting `_config_dir()` in favor of `config.CONFIG_DIR` so one setattr covers the markers — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** tests/conftest.py:91-211
- **Context:** From /full-review cluster "retro-fleet skill installer after the three-agent split" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] The only `mkdir` of 22 without `exist_ok=True` races two mm processes

- **Description:** `skills_dir.mkdir(mode=0o700)` is the only one of 22 `mkdir` call sites in the package without `parents=True, exist_ok=True` (compare `fsutil.py:176`, `config.py:441`, `aggregator.py:2106`, and `_touch_marker` twelve lines below it). A TOCTOU race between the `exists()` check and the mkdir — two mm processes, e.g. an interactive push and a hook-driven autopush — raises `FileExistsError`, which is caught as an `OSError` and emits a spurious "retro-fleet skills directory setup failed" notice plus an unnecessary skip. `mode=0o700` on an agent-owned directory also diverges from the mm-private-dir rationale documented at `config.py:400-404`.
- **Hypothesis (untested):** Add `exist_ok=True` and reconsider whether `0o700` is right for a directory the agent, not mm, owns — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2885-2893
- **Context:** From /full-review cluster "retro-fleet skill installer after the three-agent split" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Three skill-installer failure notices omit which agent failed

- **Description:** All three per-target failure notices ("skills directory setup failed", "dangling-link cleanup failed", "skill link install failed") omit the target path, so a user cannot tell whether Claude Code, Codex, or OpenCode failed. `_emit_conflict_notice` — in the same function family, called from the same function — does interpolate `safe_str(str(target))`. Before the three-agent split there was one target so the omission was harmless.
- **Hypothesis (untested):** Add `safe_str(str(target))` to the three notices; `target` is already in scope at each site — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2890, 2913, 2944
- **Context:** From /full-review cluster "retro-fleet skill installer after the three-agent split" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] Six near-identical skill-installer wrappers where a table would do

- **Description:** The retro-fleet skill installer is six near-identical wrapper functions (`_ensure_retro_skill_link` / `_ensure_codex_...` / `_ensure_opencode_...`, `_skill_link_check_due` / `_codex_...` / `_opencode_...`), six marker constants, and the three agent skill paths hardcoded at seven separate sites including `install_skills_cmd`. Adding a fourth agent means three more functions instead of one more table row.
- **Hypothesis (untested):** Replace with one module-level `_SKILL_TARGETS` tuple of `(path, success_marker, conflict_marker)` looped over in `_ensure_retro_skill_links` / `_skill_links_check_due` / `install_skills_cmd`, deleting all six wrappers — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2798-2860, 3010-3065, 6137-6141
- **Context:** From /full-review cluster "retro-fleet skill installer after the three-agent split" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `mm gc --conflicts --dry-run` always reports "would reap 0"

- **Description:** `_gc_old_conflict_files` never increments `reaped` on the dry-run path — the increment lives only inside `if not dry_run:` — so `mm gc --conflicts --dry-run` always prints "would reap 0 stale conflict files" regardless of how many stale files exist. Its ~90%-identical mirror `_gc_old_event_files` has the `else: reaped += 1` branch. Mirrored-implementation drift: same age filter, same `would delete`/`deleted` prefixes, same `would reap`/`reaped` label, same OSError-swallowing unlink.
- **Hypothesis (untested):** Collapse the two reapers onto one shared reap loop rather than patching the missing increment, so the next divergence can't happen — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:8098-8123
- **Context:** From /full-review cluster "mm gc reapers disagree with each other and with --dry-run" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=tests/test_conflict_copy.py] The dry-run reaper test discards the return value that carried the bug

- **Description:** The `_gc_old_conflict_files(dry_run=True)` test asserts only that the file still exists and discards the returned count — which is exactly why the always-zero `reaped` count shipped unnoticed. `tests/test_gc_events.py` asserts the count for the events reaper.
- **Hypothesis (untested):** Assert the returned count equals the number of stale files when fixing the cli bug, mirroring the events-reaper test — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** tests/test_conflict_copy.py:606-607
- **Context:** From /full-review cluster "mm gc reapers disagree with each other and with --dry-run" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/cli.py] `_gc_token_cache` breaks the `--dry-run` contract its own comment promises

- **Description:** `_gc_token_cache` breaks the `--dry-run` contract every other reaper in the same `mm gc` command honors. `_gc_old_event_files` prints `would delete (age Nd): <path>` per candidate; `_do_gc` and `_gc_old_conflict_files` report likewise. `_gc_token_cache` prints "Token cache reaper: dry-run; skipping." and reports nothing — and its own comment promises the opposite ("Dry-run: count without mutating. Re-implement the predicate cheaply").
- **Hypothesis (untested):** A read-only pass over the cache can produce a real dry-run count, since the reap predicate is pure (entry non-dict / jsonl missing / stale `by_day`); alternatively correct the comment to match the deliberate no-op — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:5478-5498
- **Context:** From /full-review cluster "mm gc reapers disagree with each other and with --dry-run" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=src/mind_meld/conflictlog.py] `conflict-decisions.jsonl` is the only local append-only artifact with no bound

- **Description:** `conflict-decisions.jsonl` has no size cap and no reaper, while every sibling is bounded: `pullhistory` rotates at `_ROTATE_BYTES` via an `on_locked` closure, mm-events reap at `EVENTS_RETENTION_DAYS`, the token cache reaps in `gc_cache_entries`, and `.sync-conflict-*` reap under `mm gc --conflicts`. `mm gc` has no hook for this file. `_backfill_conflict_log` additionally materializes every prior backfill row into a set on each run. Rows are small and the module is explicitly disposable, but the rip-out is gated on "≥25 real decisions or 60 days" and nothing enforces a ceiling if that trigger slips.
- **Hypothesis (untested):** Subtraction-first — confirm the rip-out trigger is being tracked and accept the growth, or land the rip-out before the file can grow, rather than adding rotation machinery to code slated for deletion; if a bound is wanted, reuse the `pullhistory` rotation closure via `fsutil.flock_append_jsonl`'s `on_locked` hook. Re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/conflictlog.py:46-79
- **Context:** From /full-review cluster "mm gc reapers disagree with each other and with --dry-run" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `status` prints peer-controlled device fields raw into a Rich console

- **Description:** `status` interpolates peer-controlled `d['device_name']` / `d['device_id']` raw into `console.print`, while the `devices` command explicitly `safe_str`s the same two fields with a comment noting that Rich cells interpret markup and pass terminal escapes through. Missed site in the sanitization sweep.
- **Hypothesis (untested):** Wrap both in `safe_str` per `docs/invariants/init-devices.md`, and grep for any other peer-controlled print site missed in the same sweep — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:5035-5039
- **Context:** From /full-review cluster "safe_str sweep missed two peer-controlled print sites" on branch kbitz/full-review-v1 (2026-08-14). Related to the existing ROADMAP Future item "safe_str hardening at three stderr sites", which names three different sites.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `_print_pull_summary` emits peer-controlled strings unsanitized 30 lines below blocks that sanitize them

- **Description:** `_print_pull_summary`'s quiet and non-quiet per-source blocks emit peer-controlled `r.device_name`, `r.src_name`, and per-file `rel_path`s unsanitized, while the corrupt-peer / unknown-source blocks 30 lines above in the same function — and `_print_preflight_conflicts` — all `safe_str` the identical fields with a "peer-controlled — sanitize" comment. Same-function inconsistency, so likely an incomplete sweep rather than a deliberate exemption.
- **Hypothesis (untested):** Sanitize at the four sites and confirm no test pins the raw strings — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:4279-4290, 4352-4398
- **Context:** From /full-review cluster "safe_str sweep missed two peer-controlled print sites" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=docs/invariants/events-retro.md] The skill-installer invariant section describes behavior that no longer exists

- **Description:** The "Group 8 retro-fleet skill — symlink installer" section documents a single Claude Code target and a five-branch state machine whose branch (1) is "skills-dir-absent → silent skip". The Codex/OpenCode commit replaced that with three targets, three marker pairs, a shared `_ensure_retro_skill_link_at`, and a new behavior where a missing `skills/` dir is CREATED rather than skipped; line 108's "exits 1 when `~/.claude/skills` doesn't exist" no longer holds either. CLAUDE.md's pointer row still names only the two pre-split function names, so an agent editing `_ensure_retro_skill_link_at` or `_skill_links_check_due` is never routed to the doc at all. Separately, neither CLAUDE.md nor SPEC.md nor any invariant doc mentions the new `codex` / `opencode` DEFAULT_SOURCES entries or their auto-detect path, despite `config.py:DEFAULT_SOURCES` being an explicitly routed invariant surface. Every other recent release shipped its invariant update in the same PR.
- **Hypothesis (untested):** Regenerate the section and the CLAUDE.md pointer row for the multi-agent shape, listing `_ensure_retro_skill_link_at` / `_skill_links_check_due` / `_skill_link_check_due_at` as routing keys, in the same pass as any fix to the gate asymmetry — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** docs/invariants/events-retro.md:5, 96-108
- **Context:** From /full-review cluster "docs drift" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have] Every line-number citation in CLAUDE.md and the invariant docs has drifted

- **Description:** CLAUDE.md and the invariant docs pin load-bearing behavior to specific line numbers that have all drifted: `aggregator.py:1862` (breadcrumb text) is now ~1969-1975; `aggregator.py:830` (latest-snapshot-wins) is ~814; `cli.py:2886-2894` (the autopush token gate) is `_decide_token_walk_policy` at 3094; `cli.py:5688` (`_resolve_interactive_loop`) is 7407; `cli.py:1115` (`_prompt_conflict_choice`) is 1164; `cli.py:1581` (the mtime gate, cited in `sync.md:106`) is 1834; TODOS.md cites `cli.py:6624` for `_promote_target_will_sync`, which is 6810. A maintainer following the routing table to a cited line lands on unrelated code.
- **Hypothesis (untested):** Replace line-number citations with function-name citations (which the routing table already uses successfully) so the docs survive the next refactor without a sweep — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** CLAUDE.md:34 and docs/invariants/*.md
- **Context:** From /full-review cluster "docs drift" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=CLAUDE.md] The Source Layout inventory omits `fsutil.py`

- **Description:** The Source Layout module inventory omits `fsutil.py`, the only module in `src/mind_meld/` absent from it (`conflictlog.py` even gets its own paragraph). fsutil owns the flock-append, atomic-write and `fsync_dir` conventions that `pullhistory`, `events`, `config` and `storage/local` all route through, so a contributor grepping the layout for "where does append-under-flock live" won't find it and is likelier to hand-roll — which `conflictlog.py` already documents itself as doing.
- **Hypothesis (untested):** Add `fsutil` to the module list plus a one-line note that new flock/atomic-write call sites route through it, mirroring the existing `lockedjson` and `storage/keys.py` notes — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** CLAUDE.md:22
- **Context:** From /full-review cluster "docs drift" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:nice-to-have,files=docs/PROGRESS.md] v0.12.12 has no PROGRESS row — third occurrence of the same miss

- **Description:** v0.12.12 (the conflict-telemetry collector) has a CHANGELOG entry but no PROGRESS.md row — the table jumps 0.12.13 → 0.12.11. CLAUDE.md calls the same-PR PROGRESS row a load-bearing convention precisely because the release workflow can only warn, not enforce, and names v0.11.24 / v0.11.27 as prior misses. This is the third occurrence.
- **Hypothesis (untested):** Backfill the row from the CHANGELOG lead paragraph per the documented format; the repeat suggests the durable fix is a `pull_request`-triggered check rather than the workflow's post-hoc warning — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** docs/PROGRESS.md:9-10
- **Context:** From /full-review cluster "docs drift" on branch kbitz/full-review-v1 (2026-08-14).
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] Four interactive commands repeat the preamble `_auto_command_setup` already solved for the auto pair

- **Description:** Four interactive commands repeat the same preamble — `_get_config` / `_maybe_prompt_migration` / re-`_get_config` / `_get_passphrase_or_exit` / `acquire_lock` (push, pull) and `_get_config` / passphrase / device fields / `get_backend` / `_init_crypto_session` (status, diff_cmd). `_auto_command_setup` already exists for the autopull/autopush pair; the interactive half of that migration was never done.
- **Hypothesis (untested):** Add the interactive counterpart of `_auto_command_setup` (or extend it with an `interactive` flag) and delete the four copies — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2706-2715, 3796-3805, 4867-4877, 5361-5371
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] The diff-colouring loop is duplicated across both conflict prompts with drifted caps

- **Description:** The unified-diff colouring loop (`+`/`-` → green/red via `safe_text`, plus the "...(N more diff lines)" tail) is duplicated verbatim in `_prompt_conflict_choice` and `_resolve_interactive_loop`, with silently drifted caps (60 vs 80 lines) and no documented reason for the difference.
- **Hypothesis (untested):** This is the leaf-rendering shape `conflictdiff.py` was created for — move it there as `render_diff_lines(diff, cap)` and pick one cap, since the site-level dispatch CLAUDE.md protects is the choice logic, not the colouring — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:1282-1296, 7675-7689
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `_push_core` runs the full `iter_source_diffs` walk twice per push

- **Description:** `_push_core` runs the full `iter_source_diffs` walk twice — once inside `any(True for _, _, _, _ in ...)` purely to compute `has_substantive`, then again to drive the upload loop. Every source's `diff_files` runs twice per push.
- **Hypothesis (untested):** Materialise the diff list once and derive `has_substantive` from `bool(diffs)`; confirm nothing between the two calls mutates `local_manifest["sources"]` in a way the second walk depends on — the events re-walk at 3620-3630 does, so this needs checking rather than assuming. Re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:3600-3603, 3638-3640
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `_resolve_interactive_loop` is 630 lines and 74 branch nodes

- **Description:** `_resolve_interactive_loop` is the largest function in the codebase: 630 lines with 74 branch nodes. It contains four separable phases (canonical-missing sub-flow, read+diff+banner rendering, the keystroke parse loop, the six-way apply dispatch) plus interleaved CONFLICT-TELEMETRY row construction, and the (l)/(r) arms duplicate the rename/unlink success+failure blocks.
- **Hypothesis (untested):** Split the canonical-missing branch (7470-7543) and the prompt-rendering block (7558-7728) into helpers first — those are the two segments with no shared mutable state beyond `resolved`/`failed`; the telemetry weaving disappears on its own when the collector is ripped out. Re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:7407-8036
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] The `b`/`both` deprecation alias is copy-pasted into both conflict prompts

- **Description:** The pre-1.0 `b` / `both` → `skip` deprecation alias, notice text included, is copy-pasted into both prompt sites, and both comments say "alias removed at 1.0".
- **Hypothesis (untested):** Since the removal condition is a version bump, consider whether the alias can be deleted now; if not, one shared `_normalize_conflict_choice(choice)` replaces both copies — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:1325-1334, 7799-7811
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `_download_and_apply` hand-rolls the outcomes dict `_empty_outcomes()` returns

- **Description:** `_download_and_apply` hand-rolls the seven-key outcomes dict that `_empty_outcomes()` already returns; the helper has exactly one caller. Adding an eighth `ApplyOutcome` requires editing two places.
- **Hypothesis (untested):** Call `_empty_outcomes()` here and delete the literal — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:1977-1985
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage. Also named in the existing ROADMAP Future item "cli.py micro-cleanups (old 14A/14B)".
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] The crypto lost-race branch reimplements the fall-through it says it falls through to

- **Description:** In `_bootstrap_or_verify_crypto`, the lost-bootstrap-race branch reimplements the second-device verify path line-for-line — same asserts, `set_crypto_session`, `load_master_key`, `verify_passphrase`, `_error` — despite its own comment saying "fall through to second-device verify".
- **Hypothesis (untested):** Have the race branch replace `fetch` with `retry_fetch` and actually fall through to the shared tail, deleting ~15 duplicated lines — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:2349-2365
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] `enable_source` / `disable_source` share an identical 7-line preamble

- **Description:** `disable_source` and `enable_source` share an identical 7-line preamble (`_get_config`, `_validate_source_name` in try/except ConfigError → `_error`, `sync = dict(...)`, `disabled = list(...)`).
- **Hypothesis (untested):** Extract a `_load_source_toggle_state(name, force)` helper returning `(config, sync, disabled)` — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:5817-5824, 5853-5860
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] The `autopull` / `autopush` exception tails are structurally identical

- **Description:** The `except typer.Exit / MindMeldError / Exception` tails of `autopull` and `autopush` are structurally identical, differing only in the verb string and one breadcrumb outcome label (`fleet-refused` vs `refused`).
- **Hypothesis (untested):** One `_auto_command_tail(verb, refused_outcome)` context manager or decorator replaces both; verify the differing outcome labels aren't asserted separately in `test_silent_failure_contract.py` — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:8558-8579, 8640-8657
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] A pointless `else` wraps 40 lines of the merge branch

- **Description:** `if not merge_available: ... continue` is followed by an `else:` that wraps the remaining 40 lines of the merge branch — the `else` is unreachable-as-a-branch and only adds an indentation level.
- **Hypothesis (untested):** Drop the `else` and dedent — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:7888-7907
- **Context:** From /full-review cluster "cli.py structural duplication" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] Nine function-local imports shadow module-scope bindings, invisible to ruff

- **Description:** Nine function-local imports re-import names already bound at module scope: `hashlib`/`json` (module level), `json as _json` at six separate sites, `secrets as _secrets`, and `from datetime import datetime, timezone`. Ruff's F811 does not catch function-local shadowing of module-level imports, so these survive lint indefinitely.
- **Hypothesis (untested):** Delete all nine and use the module-level bindings; verify no local variable named `json`/`secrets`/`datetime` was the reason for the aliasing — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:899-900, 4901, 5060, 5202, 6418, 6917-6918, 8172, 8241
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage. Also named in the existing ROADMAP Future item "cli.py micro-cleanups (old 14A/14B)".
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] Four local re-imports of conflict-filename helpers already bound at module scope

- **Description:** `is_pre_inversion_conflict_filename` and `parse_conflict_device_short` are imported at module level and then re-imported locally at four more sites, shadowing the same names. `CONFLICT_V0_PREFIX` is the only name in that group not already at module scope.
- **Hypothesis (untested):** Delete the four local re-imports — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:6566-6569, 6854, 7168, 7445-7448
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/cli.py] A loop variable named `sidecar` shadows the `sidecar` module for the rest of `_apply_conflict`

- **Description:** `_apply_conflict`'s dedup loops use `for sidecar in existing:`, shadowing the module-level `sidecar` module import for the rest of the function. Harmless today only because `_apply_conflict` doesn't call `sidecar.write` / `sidecar.read`; a future edit that does would fail confusingly.
- **Hypothesis (untested):** Rename the loop variable to `existing_sidecar` — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/cli.py:1698-1727
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/token_usage.py] `CacheEntry` is both dead and stale — it misdescribes the shape `get_or_compute` writes

- **Description:** The `CacheEntry` TypedDict has zero references anywhere in src or tests (only its own definition and its `__all__` entry), and it is stale: it documents `size` / `mtime` / `by_day` / `skills_by_day` but not the v0.12.15 `offset` / `head` / `head_len` / `tail_msg_ids` fields `get_or_compute` actually writes and `_resume_plan` reads. The module's own convention states the opposite — the `TOKEN_FIELDS` comment says a schema addition "requires updating this tuple AND the `Usage`/`DayBucket` TypedDicts". A reader consulting `CacheEntry` for the cache contract now gets a wrong answer, and `_resume_plan`'s field-by-field isinstance gauntlet is the only shape documentation left. Both the hygiene and consistency agents flagged this independently.
- **Hypothesis (untested):** Either delete `CacheEntry` and its `__all__` entry, or add the four optional fields (it is already `total=False`) and annotate the entry built in `get_or_compute` so it can't drift again; consider whether the module docstring and CLAUDE.md should also name `head_len`, which both currently omit despite it being a load-bearing rejection gate. Re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/token_usage.py:350-356
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/token_usage.py] `walk_jsonl_token_buckets` is a back-compat shim over a shim, called only from tests

- **Description:** `walk_jsonl_token_buckets` is a "backwards-compat shim" for pre-v0.11.27 callers, but mind-meld is a single-repo CLI with no external library consumers — every remaining caller is in `tests/test_token_usage.py`. It is now a shim over a shim (`walk_jsonl_buckets` over `walk_jsonl_segment`).
- **Hypothesis (untested):** Delete it, update the ~10 test call sites to `walk_jsonl_buckets(path)[0]`, and drop the `__all__` entry — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/token_usage.py:804-813
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/events.py] `except (CancelledError, FuturesTimeoutError, Exception)` has two dead members

- **Description:** Both `concurrent.futures.CancelledError` and `concurrent.futures.TimeoutError` are `Exception` subclasses (verified against the running interpreter), so the tuple's first two members are dead and the handler is just `except Exception`.
- **Hypothesis (untested):** Reduce to `except Exception`, or keep the narrow types and drop `Exception` if the intent was actually to be selective — the current form silently means the latter never happened. Re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/events.py:544
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/events.py] The future-collection block in `walk_git_projects` is written twice with drifting except clauses

- **Description:** In `walk_git_projects`, the future-result collection block (`fut.result(timeout=0)` → append to `skipped` on error → append `err` → append `proj`) is written twice: once in the `as_completed` pump and again in the budget-exhausted drain, with slightly different `except` clauses between the copies.
- **Hypothesis (untested):** Extract a `_collect(fut, root)` closure used by both arms so the two paths can't diverge further — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/events.py:540-566
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/skills/retro_fleet/aggregator.py] `_import_canonicalize`'s stated rationale no longer holds

- **Description:** `_import_canonicalize` exists so "tests can run without the full mind_meld install", but the module already does `from mind_meld import identity` at top level, so that rationale no longer holds; it is called on every `aggregate_git` invocation. Its return annotation is the string literal `"callable"` — the builtin function, not `Callable`.
- **Hypothesis (untested):** Import `canonicalize_remote_url` at module scope and delete the indirection; check whether any test monkeypatches `_import_canonicalize` before removing — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/skills/retro_fleet/aggregator.py:535-541
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/skills/retro_fleet/aggregator.py] Eight function-local imports in the aggregator, three importing overlapping names

- **Description:** Eight function-local `from mind_meld.token_usage import ...` / `from mind_meld.safety import ...` / `from mind_meld.config import ...` statements, three of which import overlapping names (`COST_EXCLUDED_MODELS` twice, `safe_str` three times). The module already imports `mind_meld.identity` at top level, so no import cycle justifies the pattern.
- **Hypothesis (untested):** Hoist to module scope; if one genuinely breaks (cycle via `config` → `events` → `token_usage`), keep only that one and comment why — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/skills/retro_fleet/aggregator.py:962, 1312, 1401, 1445, 1476, 1523, 1546, 2284
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/upgrade.py] A dead `fsutil` import kept alive by a dead `_ = fsutil` statement

- **Description:** `_ = fsutil` at module tail exists solely to defeat ruff F401 for an import kept "for future atomic-write needs (e.g., a self-version split file if we ever revisit D14)". The feature was never revisited; this is a dead import plus a dead statement plus a comment explaining why the dead code is there.
- **Hypothesis (untested):** Delete the `fsutil` import and the `_ = fsutil` line; re-add when D14 is actually built — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/upgrade.py:550-552
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage. Also named in the existing ROADMAP Future item "cli.py micro-cleanups (old 14A/14B)".
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/pullhistory.py] Dead `HISTORY_PATH` constant beside the call-time resolver the isolation fixture depends on

- **Description:** `HISTORY_PATH` has zero references in src, tests, or docs — it was superseded by the call-time `history_path()` that the test-isolation fixture depends on, and leaving both invites a future caller to pick the import-time-frozen one and break `_isolate_pullhistory`. `_rotated_path()` is production code referenced only from tests.
- **Hypothesis (untested):** Delete `HISTORY_PATH`; for `_rotated_path`, check whether the tests can derive the path from `history_path()` directly rather than keeping a production-side helper alive for them — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/pullhistory.py:63, 87
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/seen_sources.py] Dead `SEEN_PATH` constant, same import-time-freeze hazard

- **Description:** `SEEN_PATH` has zero references anywhere — the same dead-constant-beside-a-call-time-resolver shape as `pullhistory.HISTORY_PATH`, and the same import-time-freeze hazard for the isolation pattern. `seen_path()` is the live accessor.
- **Hypothesis (untested):** Delete it — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/seen_sources.py:44
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=src/mind_meld/merge.py] `similarity_ratio` hand-enforces a "MUST match `lcs_merge` exactly" preamble copy

- **Description:** `similarity_ratio` copies `lcs_merge`'s NUL guard, strict-UTF-8 decode, `splitlines()` and `autojunk=False` preamble, with a docstring stating these "MUST match `lcs_merge` exactly" — a hand-enforced invariant where a shared helper would make drift impossible.
- **Hypothesis (untested):** Extract `_split_for_lcs(local, remote) -> tuple[list[str], list[str]] | None` used by both; note this whole function is scheduled for removal with the CONFLICT-TELEMETRY rip-out, so deleting it later may be cheaper than refactoring it now — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** src/mind_meld/merge.py:250-273
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [full-review:necessary,files=tests/test_pull_result.py] The two-machine test bootstrap is duplicated verbatim across two test modules

- **Description:** A 12-line two-machine bootstrap (two claude dirs, two configs, `bootstrap_crypto_init`, two `register_device`, CONFIG_PATH swap, `_redirect_lock`, passphrase env, push) is duplicated verbatim in `tests/test_silent_failure_contract.py:974-991`.
- **Hypothesis (untested):** `conftest.py` already owns `_setup_real_config` for the single-machine case — add the two-machine sibling there and delete both copies — re-investigate before implementing; the reviewer agent did not verify this direction.
- **Found in:** tests/test_pull_result.py:174-191
- **Context:** From /full-review cluster "dead constants, stale shims, stray function-local imports" on branch kbitz/full-review-v1 (2026-08-14). Cluster reclassified nice-to-have → necessary at triage.
- **Effort:** ? (user triages in /roadmap)

### [review] Two accepted divergences between an incremental resume and a full walk
- **What:** v0.12.15's incremental token walk is equivalent to a full walk on every measured file, but two cases diverge by construction. (1) **Tool_use dedup has no cross-segment seed.** A retry re-emitting the same `tool_use.id` on a LATER line, split across a resume boundary, counts twice incrementally where a full walk counts once. (2) **A final line with no trailing newline is never counted** — it reads as a partial write, so the offset doesn't advance past it, and if the file is never appended to again that line stays uncounted forever. The old text-mode walker counted it.
- **Why:** Both were raised by the Codex adversarial pass and accepted on evidence, not hand-waving. Measured across 358 live session jsonls: **zero** duplicate `tool_use.id` values exist, so a seed would be pure cache weight for a case that does not occur; and zero files lack a trailing newline, so case (2) never fires. For (2) the alternative (count it AND advance the offset) invites a double-count, and under-counting one line is the safer failure. Binary mode also stopped splitting on a lone `\r`; Claude Code writes `\n`.
- **Pros of doing it:** Closes the last gap between "equivalent on every file we've seen" and "equivalent by construction". The tool-id seed is mechanical — mirror `tail_msg_ids` exactly.
- **Cons:** Cache weight for an unobserved case. Case (2) needs a settled-file heuristic (mtime older than N seconds ⇒ treat the tail as complete) which adds a time-dependent branch to a hot path for a case with zero occurrences.
- **Trigger:** Re-run `.context/measure_dup_gaps.py`-style census (recorded in `docs/invariants/events-retro.md`). If duplicate `tool_use.id` values ever appear, do (1). If a real session file ever lacks a trailing newline, do (2).
- **Context:** `docs/invariants/events-retro.md`, incremental-resume section, invariants 2 and the "known, measured-absent divergence" note. Start at `token_usage.walk_jsonl_segment` (`seen_tool_ids` init) and `_iter_bounded_lines` (the short-read-at-EOF branch).
- **Depends on / blocked by:** None — both are watch-items with explicit evidence triggers.

### [plan-eng-review] Rip out the temporary conflict-decision collector once Phase 2 thresholds validate
- **What:** Remove the disposable `CONFLICT-TELEMETRY` instrumentation: delete `src/mind_meld/conflictlog.py`, `grep -rn "CONFLICT-TELEMETRY"` and remove each call site (the `_resolve_interactive_loop` hooks + `_conflict_feature_dict` / `_conflict_rel_path` / `_MAX_FEATURE_BYTES` helpers + the `device_id` param), remove `merge.similarity_ratio` if unused by the shipped Phase 2 classifier (it may graduate into it), delete the hidden `conflict-log-backfill` command, the `_isolate_conflictlog` conftest fixture, `tests/test_conflictlog.py`, and the `~/.config/mind-meld/conflict-decisions.jsonl` files on each Mac. No schema/sync/config state to unwind.
- **Why:** It exists only to gather the labeled dataset that unblocks Phase 2 (similarity classifier + silent merge). Once the similarity bands are validated against real decisions, the collector has served its purpose and "temporary" code that lingers becomes cruft. Ripping it out is the removal trigger that keeps it honest.
- **Pros:** Removes ~250 lines of throwaway instrumentation + the resolve-loop hooks; shrinks the conflict hot path back to its shipped shape.
- **Cons:** None once the data has been analyzed. Don't rip it out before the D4 evidence threshold (≥25 real non-backfill decisions, or 60 days) is met.
- **Context:** Design + review at `~/.gstack/projects/kbitz-mind-meld/kb-kbitz-conflict-resolution-log-design-20260730.md`. Collector is resolve-site-only (E1) + opt-in `mm conflict-log-backfill`. Analyze with: read the jsonl (per-Mac; not synced), check whether `similarity` + `merge_conflicts` + `newer_side` separate the `choice` values, then set the Phase 2 bands. `merge.similarity_ratio` was built to match `lcs_merge`'s representation exactly so its numbers are the classifier's numbers.
- **Depends on / blocked by:** The Phase 2 similarity-classifier work (the other `[plan-eng-review]` entry below) consuming this data first.

### [plan-eng-review] Future-clamped peer mtime can mislead the `(n)ewer` recency verdict
- **What:** The conflict-prompt recency verdict and the `mm resolve` `(n)ewer` shortcut (v0.12.10) key off modified time. A peer with a clock ahead of real time has its restored sidecar mtime capped at `now + 60s` by `_restore_mtime_best_effort`'s future-clamp (`cli.py`). In the rare case where a peer's true mtime is >60s in the future AND the local file is also future-dated, the clamp can flatten or invert the "which is newer" comparison, so the verdict / `(n)ewer` could point at the wrong side.
- **Why:** `/plan-ceo-review` T2 + Codex CEO-pass #3 (2026-06-23). Chose to keep the verdict ADVISORY rather than detect/suppress on clamp: the real timestamps are visible to the user, `(n)ewer` never auto-commits a tie, and the clamp exists precisely to neutralize bad-clock peers. Clamp-detection is also only partially possible (the `mm resolve` site can't see the original manifest mtime). Logged so dogfood can surface a real case before adding complexity.
- **Pros of doing it:** Closes the last "recency is wrong" corner if it ever bites in practice.
- **Cons:** Adds branching to detect "was this clamped" (only feasible at the inline site, which doesn't even offer `(n)ewer`); over-engineering for a near-impossible clock-skew case. Recency is already framed as a heuristic, not correctness, in the UI copy.
- **Context:** Same family as the existing "peers we never consciously resolved against can be mtime-skipped by the drain" watch-item. Start at `_restore_mtime_best_effort` (the clamp) + `conflictdiff.render_verdict` / `newer_side`. Revisit only if dogfood shows real misdirection.
- **Depends on / blocked by:** None — informational watch-item.

### [review] `_promote_target_will_sync` ignores `exclude_patterns`
- **What:** `_promote_target_will_sync(src_cfg, target)` in `cli.py:6624` returns True if the promoted file lives inside one of the source's `_synced_scan_dirs`. But `walk_generic_source` (`manifest.py:435`) also applies `exclude_patterns` before recording files in the manifest. A target that sits in an included dir but matches an exclude glob (e.g. user configured `exclude_patterns: ["*.from-*"]`) would be reported as syncable here, the `mm: warning: ... will not sync` line would be suppressed, and the user would silently end up with a local-only "resolved" file.
- **Why:** Codex /review 2026-05-15 (P2 finding on PR #97). The visible-failure contract says load-bearing warnings about sync degradation must surface — the helper's logic is too narrow to honor that contract in the exclude-pattern case.
- **Pros of doing it:** Closes the silent failure mode. Aligns the helper with what the manifest walker actually does.
- **Cons:** Re-implementing the manifest walker's pattern logic in a second site risks drift. Better shape: reuse the actual pattern check from `manifest.py` rather than duplicate. Low-priority because the failure surface (a user configuring an `exclude_patterns` that catches `*.from-*` / `*.local-*`) is rare in practice — default configs don't.
- **Context:** Start at `cli.py:6624` (`_promote_target_will_sync`) and `manifest.py:_compile_excludes` / `_path_excluded` (or wherever the walker applies patterns). One option: thread the compiled exclude matcher through to the helper. Another: just call `walk_generic_source` with the target's parent dir and check if the target appears in the result.
- **Depends on / blocked by:** None — independent cleanup.

### [plan-eng-review] Phase 2: similarity classifier + similarity-gated silent merge
- **What:** Build the LCS-similarity classifier (`classify_divergence` in `merge.py` → `DivergenceResult` with a 4-state `DivergenceClass`: same_document / different_document / ambiguous / not_mergeable), its `conflictdiff.py` leaf primitives (`render_classification_summary`, `recommended_default_key`), an opt-in `Class` column on `mm conflicts`, and similarity-gated silent merge in `_apply_incoming_file` (`same_document` + `conflict_count == 0` → silent apply; everything else → conflict-copy). Full spec: the "Deferred to Phase 2" section of the design doc `~/.gstack/projects/kbitz-mind-meld/kb-kbitz-conflict-merge-design-design-20260514-200134.md`.
- **Why:** The office-hours/eng-review chain shipped "Option 2" (footgun fix + `(p)romote`) and deliberately deferred the classifier — it is advisory-only until silent merge is its consumer, so building it now is premature abstraction. Phase 2 is when the classifier earns its keep: gating silent prose merge on the autopull path.
- **Pros of doing it:** Closes the loop on the original "conflicts mostly resolve themselves" ask — same-document additive divergence merges with no prompt.
- **Cons / risks (all from Codex eng-review 2026-05-14, captured in the doc):** (#10) the naive `SequenceMatcher.ratio()` is weak — blank lines, headings, table separators inflate it; must be validated/strengthened before it gates anything silent. (#7) `conflict_count` must come from `lcs_merge`, not be recomputed — single source of truth. (#12) the `Class` column hydrates iCloud placeholders — make it opt-in (`mm conflicts --classify`), not always-on. (#8) `not_mergeable` is a real 4th enum state. (#9) reconcile the small-file-guard vs test contract. The binary-guard extraction from `lcs_merge` needs a byte-identical regression test. Silent merge touches the autopull hot path + Track 12A mtime machinery (`_CANONICAL_WRITE_OUTCOMES`, mtime restore) — Med risk.
- **Context:** Approach A / Option 2 ships first (Component 1: stop defaulting Enter to `(m)erge`; Component 2: `(p)romote`). Phase 2 is only worth starting if real usage shows same-document divergence is common enough to be worth the autopull-path risk. Start at the design doc's "Deferred to Phase 2" section — it is the spec.
- **Depends on / blocked by:** Option 2 (this branch) landing first.

### [plan-eng-review] Peers we never consciously resolved against can be mtime-skipped by the drain
- **What:** Two related watch-items in the same family. The Track 12A end-of-pull-batch bump (`_bump_canonical_mtime_post_resolve` drain in `_pull_core`) bumps canonical past the keep-local-resolved peers. If that mtime exceeds an unrelated peer's manifest mtime, the next pull from that peer hits the mtime gate and skips instead of re-conflicting. Two trigger surfaces:
  1. **Pre-existing on-disk sidecars** (Codex /plan-eng-review #2, 2026-05-14): a peer whose OLD, still-unresolved `.sync-conflict-*` predates this pull.
  2. **Failed-download peers** (Codex /review P2, 2026-05-14): a peer whose blob download fails this batch (bad blob key, missing blob, decrypt error, path rejection) never reaches `_apply_incoming_file` and never invalidates. Once the transient failure clears on a later pull, that peer's manifest mtime may sit below the bump.
- **Why:** `/plan-eng-review` 2026-05-14 chose NOT to suppress (T4-A) — the sidecar-on-disk is already the durable signal (`mm conflicts` / `mm resolve` still surface it), v0.12.6's resolve-side bump has the identical property, and suppressing would weaken the propagation fix Track 12A exists to deliver. The failed-download case (Codex P2, GATE: PASS) is the same family: bytes still on the peer's manifest, will resolve on next successful pull, just deferred. Both surface as "peer we never consciously resolved against gets mtime-skipped once."
- **Pros of revisiting:** Eliminates narrow accidental-mtime-resolution paths if dogfood shows they actually bite.
- **Cons:** Adds a disk scan to the drain (#1) and per-rel-path failed-download tracking (#2); weakens fleet propagation; precise variants (suppress only for different-peer sidecars; track failed paths per resolved key) are non-trivial code.
- **Context:** Only the pull-time re-conflict / re-download is delayed, never the bytes themselves. Revisit only if dogfood surfaces real cases. Start at the `_pull_core` drain loop + `_find_conflict_files` (#1) or the `_download_and_apply` per-file failure paths (#2). See the deferred-inline subsection in `docs/invariants/conflicts.md`.
- **Depends on / blocked by:** Track 12A landing first (the drain doesn't exist yet).

### [review] Abort transactionality (pre-existing torn-state, surfaced by Track 12A)
- **What:** `typer.Abort()` from the inline `(a)bort` choice propagates out of `_pull_core`'s `try` block. The drain is skipped (Track 12A T2-A intentional). BUT earlier keep-remote / merge writes in the same `_download_and_apply` (and earlier peers within this `_pull_core` invocation) already hit disk — those writes are not rolled back, AND `_pull_one_source` only records outcomes/touched-parents/audit-log AFTER `_download_and_apply` returns, so abort also drops the audit log + fsync for those partial writes. Net: abort can leave changed files plus discarded keep-local decisions plus no audit trail for the partial writes.
- **Why:** Codex adversarial (HIGH 2026-05-14) + Claude adversarial (Finding 5) both flagged. This is **pre-existing** behavior (the abort path has worked this way since the inline prompt shipped) made slightly more asymmetric by Track 12A (now the propagation half drops too). The Track 12A T2-A decision intentionally accepts the symmetry break ("abort = the user does not trust this pull").
- **Pros of revisiting:** True abort-transactionality (all-or-nothing per pull): collect outcomes in `_download_and_apply` without applying, then either commit-all on completion or discard-all on abort.
- **Cons:** Significant restructure of `_pull_one_source` and `_download_and_apply` to defer writes; loses per-file isolation on errors; not aligned with how the rest of `mm pull` handles partial failures (corrupt peers, blob misses, etc.).
- **Context:** This is the broader pre-existing abort semantics, not a Track 12A regression. Revisit if user abort flow becomes a common pattern. Start at `_pull_one_source`'s outcome accumulation + `_download_and_apply`'s per-file write side-effects.
- **Depends on / blocked by:** None.

### [plan-eng-review] Price cache writes per-TTL instead of assuming 1h for everything
- **What:** Split the synced `cache_create` token bucket into its 5-minute and 1-hour TTL halves so cache writes price exactly, instead of pricing the whole bucket at the 1h multiplier (2x input). Anthropic bills a cache write at 1.25x input for the 5m TTL and 2x for the 1h TTL; `token_usage.PRICING` currently applies 2x to all of it via `_CACHE_WRITE_MULT`.
- **Why:** v0.12.13 moved the multiplier from 1.25x to 2x because Claude Code writes at the 1h TTL by default. Measured against a random sample of 24 local session jsonls: **83% of `cache_create` tokens were 1h, 17% were 5m** (sessions drop to the 5m TTL under usage overage). So 2x overstates the 5m slice by 0.75x — roughly **+3.5% of a typical window's total cost**, where the old 1.25x understated the whole cache-write line by ~11%. Strictly better, still not exact.
- **The data already exists.** Claude Code's jsonl carries the split verbatim: `message.usage.cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`. Verified present on every sampled record — no inference needed, `parse_usage` just doesn't read it.
- **Pros of doing it:** Cost line becomes exact for cache writes, which are the second-largest line item after cache reads. Removes the last deliberate approximation in the estimate.
- **Cons / why it was deferred:** This is a **wire-format change**, not a table edit, and it lands squarely on the fiddliest machinery in the repo. It needs: a 5th/6th entry in `TOKEN_FIELDS` (CLAUDE.md flags this as the designed extension point) → `zero_day_bucket` / `zero_model_bucket` / `merge_usage_bucket` / `merge_by_model`; a `parse_usage` change; a cache-shape upgrade gate in `get_or_compute` (the same shape as the v0.11.27 `skills_by_day` D2 gate — a re-walk, NOT a `CACHE_VERSION` bump, which would discard token history); and a **mixed-fleet discriminator** in the aggregator, because peers on older mm send a `cache_create` total with no split. That last one is the `pre_token_peers` / `pre_skills_peers` pattern, including the load-bearing D4 key-absence-vs-falsy distinction — the exact area CLAUDE.md marks as easy to get subtly wrong. Well outside the "< 5 files, no new infra" auto-approve bound.
- **Context:** Start at `token_usage.parse_usage` (reads `message.usage`) and `token_usage._CACHE_WRITE_MULT` (the constant carrying the approximation, with the measurement recorded in its comment). The mixed-fleet half is `aggregator._merge_token_window` + the `pre_*_peers` breadcrumb sites around `aggregator.py:1862`. Read `docs/invariants/events-retro.md` first.
- **Depends on / blocked by:** None. Independent of v0.12.13, which deliberately kept the wire format untouched.

### [review] `test_gc_events.py` touches the real `~/.config/mind-meld/` lock
- **What:** `TestGcEventsIronRule::test_reap_triggers_tombstone_on_next_push` invokes `mm push` through `CliRunner` and acquires the REAL `~/.config/mind-meld/mind-meld.lock` rather than a `tmp_path` one. The test monkeypatches `mind_meld.config.CONFIG_PATH` but not the lockfile path.
- **Why:** Observed failing during `/review` on v0.12.13: `Error: Another mm operation is running (PID 21528)`. The colliding process was the developer's own background `mm autopush` hook. The test passes in isolation, so this presents as a flake with a confusing error, and on a machine that runs mm on a timer it can fail repeatedly. CLAUDE.md's testing rule is explicit: "Use tmp_path for local backend."
- **Pros of doing it:** Removes a real flake, and removes the (small) chance a test run interferes with the developer's actual synced data.
- **Cons:** Need to find every lock-path entry point — `lockfile.py` resolves the path itself, so the fix is either a monkeypatchable module constant or a fixture that patches it for the whole suite. Worth auditing whether other CLI-invoking tests have the same gap rather than patching this one test.
- **Context:** Start at `tests/test_gc_events.py:241` and `src/mind_meld/lockfile.py`. A suite-wide autouse fixture redirecting the lock into `tmp_path` is probably the right shape, since any test invoking a CLI command through `CliRunner` has this exposure.
- **Depends on / blocked by:** None. Pre-existing, unrelated to v0.12.13 — surfaced by it, not caused by it.

### [review] Model-id variant suffixes alias onto their base model, in both the label and the price
- **What:** `_short_model_name` builds its version from `parts[2:4]`, discarding everything past segment 4, and `model_family` keys only on segment 1. So `claude-opus-4-5` and a hypothetical `claude-opus-4-5-thinking-16k` render as the SAME label and resolve to the SAME family tier.
- **Why:** Two distinct failure shapes. Cosmetic: the per-model breakdown can print `(Opus 4.5 $2,000, Opus 4.5 $1,500)` — two entries, one label, no way to tell them apart. Substantive: if a long-context or premium variant ever bills above its base tier, it prices LOW and silently, under a confident `~`. Related mangles: `claude-opus-5-thinking-16k` → `Opus 5.thinking`, `claude-opus-5[1m]` → `Opus 5_1m_`.
- **Pros of doing it:** Removes the last "silently prices low under `~`" path that v0.12.13 didn't close.
- **Cons / why deferred:** Entirely forward-looking. A census of this Mac's session jsonls found only `claude-opus-5`, `claude-opus-4-8`, `claude-fable-5`, and `<synthetic>` — no variant-suffixed id exists in real data today, and the correct handling depends on what a future suffix actually means (a rendering hint? a distinct SKU?). Guessing now risks encoding the wrong rule.
- **Context:** Surfaced by the `/review` adversarial pass on v0.12.13 (F4). Start at `aggregator._short_model_name` (the `parts[2:4]` join) and `token_usage.model_family`. Fix the label collision first — it is unambiguous — and only add pricing semantics once a real variant id with a real rate exists.
- **Depends on / blocked by:** None.

### [review] No sanity ceiling on the rendered cost figure
- **What:** `_safe_int` clamps each individual peer token field to `2**53`, but `_merge_token_window` then sums clamped values across ~90 days x N devices x M models with no bound on the total. One corrupt or hostile peer can still drive the headline to an absurd figure (measured: `~$364,791,569,817`).
- **Why:** The v0.12.13 clamp fixed the *crash* (`OverflowError` in the cost multiply) but not the *display*. A wrong-but-plausible number is the exact failure this release exists to prevent; a wrong-and-absurd number is less dangerous but still unlabelled.
- **Pros of doing it:** Cheap. A crude absurdity threshold that flips the line to "suspect — check for a corrupt peer" costs a few lines and converts a nonsense figure into a diagnostic.
- **Cons:** Picking the threshold is a judgment call, and a hard cap could mask a genuinely enormous (but real) window. Prefer annotating over clamping. Threat model is also mild: every machine in the fleet is the user's own, so this needs a compromised personal Mac.
- **Context:** Surfaced by the `/review` adversarial pass on v0.12.13 (F5). Start at `aggregator._render_token_block`, after `estimate_cost` returns. Note the existing `_MAX_SAFE_TOKENS` per-field clamp is the right layer for correctness — this is a separate presentation-layer concern.
- **Depends on / blocked by:** None.
