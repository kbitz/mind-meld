# Events / fleet retro — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/cli.py` — `install_skills_cmd` / `retro_fleet_cmd` / `refresh_identity_cmd` / `devices` (`--format json`) / `status` / `diag` / `_collect_diag_state` / `PushResult.events_degradations` / `_breadcrumb_staleness_suffix` / `_host_usage_blocker` / `_host_read_budgets` / `_host_complete_read_line` / `_host_read_sweep_line`
- `src/mind_meld/events_tail.py` — `_run_events_tail` / `_run_events_backfill` / `_decide_token_walk_policy` / `_enabled_claude_paths` / `_capture_host_usage` / `_default_host_readers` / `_host_skip_phrase` / `HostReadEvidence` / `host_read_age` / `resolve_host_read_budget` / `_warm_host_cache_with_notice` / `HostUsageCapture` / `_merge_host_usage_maps` / `_merge_warm_retry_capture` / `HOST_USAGE_READ_BUDGET_*` / `WARMABLE_HOST_READERS` / `_HOST_PERMANENT_REASONS`
- `src/mind_meld/skill_link.py` — `_ensure_retro_skill_links` / `_skill_link*_check_due*` / `_resolve_retro_skill_src` / `_skill_store_dir` / `_publish_skill_store` / `_prepare_store_dir` / `_should_publish` / `_store_needs_refresh` / `diagnose_skill_links` / `render_skill_status` / `BROKEN_SKILL_STATUSES` / `_emit_status_notice` / `_marker_dir` / `AGENT_ROWS` / `_descriptor_for` / `_real_guard_paths` / `consented_agent_keys` / `_row_is_consented` / `AgentRow.consent_source` / `_owned_store_exists` / `_marker_exists`
- `src/mind_meld/host_skill_discovery.py` — `probe_grok_skill_discovery`
- `src/mind_meld/retention.py` — `EVENTS_RETENTION_DAYS` / `CONFLICT_AGE_DAYS` / `_gc_old_event_files` / `_gc_old_conflict_files` / `_gc_token_cache` / `_sweep_local_tmp_files` / `_gc_orphan_retros_dir`
- `src/mind_meld/events.py` — `MmPushEvent` / `make_mm_push_event` / `walk_session_metadata` / `walk_git_projects` / `discover_git_roots` / `last_push_ts` / `EVENTS_SCHEMA_VERSION` / `WALK_TIME_BUDGET_*` / `HostUsageSnapshot` / `make_host_usage_snapshot` / `ACTIVE_HOST_READERS` / `HOST_USAGE_TOKEN_SOURCES`
- `src/mind_meld/host_usage.py` — `read_codex_usage` / `read_grok_usage` / `grok_completed_once` / `grok_usage_diag` / `warm_host_cache_inline` / `_scan_codex_root` / `_scan_grok_root` / `_read_rollout` / `_carries_usage` / `_no_ledger_entry` / `_NoCacheCommit` / `_classify_grok_update` / `_cached_last_reason` / `_cached_reason_since` / `_carry_reason` / `_carry_read_timing` / `_cached_read_timing` / `_cached_read_ms` / `_skip_failed_cache_write` / `_pause_gc` / `PERMANENT_REASONS` / `PERSISTABLE_REASONS` / `_GROK_REQUIRED_KEYS` / `_GROK_IGNORABLE_KEYS` / `GROK_USAGE_CENSUS_HOST_VERSION`
- `src/mind_meld/gitenv.py` — `scrubbed_git_env` / `GIT_REPO_LOCAL_ENV_VARS`
- `src/mind_meld/attemptlog.py` — local attended-attempt writer, validation and read/render states (65A)
- `src/mind_meld/identity.py` — `gather_local_identities` / `refresh_identity_cache` / `read_cached_identities` / `_normalize_cache` / `CACHE_PATH` / `TTL_SECONDS`
- `src/mind_meld/skills/retro_fleet/aggregator.py` — `aggregate` / `aggregate_local_emails_from_events` / `aggregate_git` / `aggregate_sessions` / `aggregate_host_usage` / `_accept_host_usage_snapshot` / `_aggregate_git_period_pair` / `gather_author_emails` / `_emit_custom_path_notice_if_due`
- `src/mind_meld/config.py` — `MM_INTERNAL_SOURCE_NAMES` / `_bootstrap_mm_events_path` / `DEFAULT_SOURCES` / `_validate_skills` / `_validate_str_list` / `HOST_USAGE_BUDGET_MAX_MS`
- `src/mind_meld/token_usage.py` — `walk_session_metadata` token-cache wiring

Tests: `tests/test_events.py`, `tests/test_identity.py`, `tests/test_init_events_backfill.py`, `tests/test_gc_events.py`, `tests/test_retention.py`, `tests/test_retro_fleet_aggregator.py`, `tests/test_skill_link.py`, `tests/test_devices_json.py`, `tests/test_token_usage.py`, `tests/test_host_usage.py` (readers), `tests/test_host_usage_snapshot.py` (capture policy), `tests/test_host_skill_discovery.py`, `tests/test_diag.py`.

## Contents

- [`mm-events` default source](#mm-events-default-source--bootstrap-load-bearing-v0101)
- [Events tail in `_push_core`](#events-tail-in-_push_core-load-bearing-v0103-gated-v0122)
- [Git subprocess environment](#git-subprocess-environment-load-bearing-track-55a)
- [Cursor gate + recapture](#cursor-gate--recapture-load-bearing-track-30a)
- [Host-usage snapshot capture](#host-usage-snapshot-capture-load-bearing-track-19a)
- [Track 22A consumer](#track-22a-consumer-last-known-good-inventory)
- [Unified-agents renderer contract](#unified-agents-renderer-contract-track-67a-supersedes-23a)
- [Coverage states (Track 34A)](#coverage-states-track-34a)
- [Init-time event backfill](#init-time-event-backfill-v0118)
- [Sessions snapshot v=2](#sessions-snapshot-v2-full-inventory-load-bearing-v0110)
- [Cost estimation](#cost-estimation--one-predicate-honest-degradation-load-bearing-v01213)
- [Fleet-wide author email](#fleet-wide-author-email-trust-set-load-bearing-v01117)

---

## `mm-events` default source + bootstrap (load-bearing, v0.10.1)
`DEFAULT_SOURCES` has a new mm-owned synced source for the per-device daily JSONL event log Group 8's `retro-fleet` skill will read.

```toml
{ name = "mm-events", path = "~/.local/share/mind-meld",
  type = "generic", include_dirs = ["events"], exclude_patterns = [] }
```

Subdir nesting (`include_dirs = ["events"]` rather than `["."]`) plays cleanly with `walk_generic_source` and avoids the `pathlib`-`["."]` quirk. Per-device daily JSONL files land at `events/<device>-<YYYY-MM-DD>.jsonl` under this base path.

**Ownership (Track 59A).** The default `~/.local/share/mind-meld` root is shared with the agent skill store; only its `events/` subtree belongs to mm-events. Removing the root, removing `events/`, or removing an event file means deletion, and the next push publishes it. No loss check or automatic restore runs. A missing custom root is user-managed and never created: push warns and skips that source for this push, preserves existing tombstones without generating new ones, skips the events tail, and records an autopush degradation. Recapture refuses before writing rows; pull skips the source's incoming files with one warning. A returned custom root is included again on the next push.

**Read-only resolution.** `resolve_sources` / `get_sources` default to `bootstrap=False`. A missing default root remains selected and available as an empty walk, with its name in `SourceResolution.would_create`; strict preview checks the nearest existing ancestor with `os.access(W_OK | X_OK)`. `_retain_prior_default_sources` preserves that field. Push preview omits those roots from the unchanged deletion proof and shows their deletions, including after sidecar recovery. Inspection and source-toggle commands create neither the default root nor `events/`. Pull with no mm-events files to apply also creates neither. `mm install-skills` is a writer: its store shares the default root and creates that root at 0700 when absent, even with mm-events disabled.

**Explicit creators.** Init resolves with `bootstrap=True` before backfill; `_push_core` (including autopush) and recapture resolve with `strict=True, bootstrap=not dry_run`. `_pull_one_source` calls `_bootstrap_mm_events_path` once before its first mm-events apply, with the configured path. The bootstrap creates missing components one at a time at 0700, returns them, and reports each to `created_ancestor` through a callback so partial creation is recorded even on a later mkdir failure. Pull fsyncs the existing parent of the topmost new directory. Bootstrap failure fails the source's incoming files with one warning. Recapture catches source-resolution/bootstrap `SnapshotError`, states that no rows were written, and includes the dry-run suffix when applicable; an unavailable source is not called disabled.

**Permission tightening.** Only an existing default root is eligible. Normalize ancestors in both compared paths without following the root itself; leave custom and symlinked roots untouched. Open with `O_NOFOLLOW | O_DIRECTORY`, verify through `fstat` that the descriptor is a directory owned by `os.getuid()`, and `fchmod(0o700)` when group/other bits are present. Failure warns once per normalized path and does not refuse. `_BOOTSTRAP_WARNED_PATHS` suppresses repeated mkdir/tightening warnings; strict creation failures still raise on every attempt. Tests reset that set per case.

## `MM_INTERNAL_SOURCE_NAMES` + init contract (v0.10.1)
`frozenset({"mm-events"})` in `config.py` enumerates source names that are mm-owned infrastructure, not user-prompted. Two consumer sites:

1. **`_prompt_sources` (init):** mm-internal entries auto-include without a Y/n prompt — they're mm-owned infrastructure for fleet-wide features (retro-fleet) and shouldn't burden the init UX with a question whose only legitimate answer is "yes." Per-machine opt-out remains via `mm disable-source mm-events` post-init (v0.10.0).
2. **`init_cmd` no-sources guard:** an init that produces only mm-internal sources fails the `user_facing_sources` check (refuses with the same "no sync sources enabled" error as the pre-Group-7 zero-sources case). A config with only mm-events is effectively "user wanted nothing synced" — push/pull would silently no-op for the user's own data; better to refuse and let them re-run.

Adding a new mm-internal source name requires updating the frozenset AND the explicit bootstrap handling in `resolve_sources()` AND (if it has a meaningful per-machine state) wiring `mm disable-source` strict-mode allowance. Keep the set small — every entry sidesteps the init-prompt UX, so only mm-owned synced infrastructure qualifies (today: events).

## Events tail in `_push_core` (load-bearing, v0.10.3, gated v0.12.2)

Track 7B wires `events.py` (Track 7A foundation, v0.10.2) into the push hot path. `_run_events_tail(config, sources, device_id, *, dry_run, quiet)` runs from `_push_core` AFTER the substantive-change gate (v0.12.2): `build_manifest_v2` walks all sources first, the diff against the recovered remote manifest is computed, and only when `iter_source_diffs(local_manifest, remote_sources, skip_unchanged=True)` yields at least one entry (or `fetch.status == "corrupt"`) does the events tail fire. After firing, `_push_core` re-walks the `mm-events` source and folds the just-written row into `local_manifest["sources"]` so it ships in the same push (no one-push lag). Four invariants govern the wiring:

1. **Single call site, gated on substantive change (Codex C4 + v0.12.2).** Pre-v0.12.2 the tail fired at the HEAD of `_push_core` unconditionally (the original Codex C4 fix replaced inline-before-each-early-return with a head-position single call to avoid branch fragility against the "must run on every push attempt" trust boundary). v0.12.2 relaxed the trust boundary to "must run on every push that uploads bytes" — empty pushes were writing a `mm-push` event row, mutating the mm-events file, and reporting "1 file uploaded" forever as the only "change" pushed (phantom-change-on-empty-push regression). The gate counts diffs across ALL sources (user + mm-events), so an un-flushed prior-push event row from a partial-upload failure still triggers a fresh push that drains it. Do NOT add additional call sites; do NOT move the tail back to HEAD.

2. **`dry_run` no-op (preview contract, Track 56A).** `mm push --dry-run` changes nothing except the local lock file (including creation of its parent when absent). No blob, manifest, last_seen, event, config, history, skill, or mm-cache writes and no upgrade HTTP check. The events tail and device/skill hooks retain their dry-run gates. Push also skips migration prompts, the whole transition hook, fingerprint persistence, mm-events bootstrap, crypto-init repair, and the nudge. Strict scan and deletion-proof failures still refuse. The trailer and pending setup lines belong to `push()`, after every successful `_push_core` return, including no-op/no-sources returns. No trailer on refusal. The preview omits host-usage capture, the activity row a real push appends, post-push GC, and upload re-reads; Track 62A extends the setup guarantee to all previews; see the [README Previews table](../../README.md#previews). Recapture preview writes no rows or config/upgrade state, reports non-benign walk skips separately from discovery errors/budget expiry, and retains exit 0 on partial scans. No repositories exits 1 with a preview-preserving remedy. Disabled and not-configured sources are distinct, with disabled taking precedence.

3. **`mm-events`-resolved gate, NOT `disabled_sources` (Codex C1).** The gate is `next((s for s in sources if s.get("name") == "mm-events"), None) is not None`. This covers fresh / migrated / un-migrated configs uniformly: a config that pre-dates v0.10.1 simply has no `mm-events` entry and the tail no-ops, no migration prompt required. Gating only on `disabled_sources` would let pre-v0.10.1 configs accumulate local cruft forever (the `~/.local/share/mind-meld/events/` tree never created, never written). Real push explicitly bootstraps the default root before reaching the tail; a missing custom root is excluded from the tail entirely.

4. **Wall-clock budget (Codex C4 + C5; walk-scoped v0.12.9).** `WALK_TIME_BUDGET_AUTOPUSH_MS` (250) for `quiet=True` (autopush hook), `WALK_TIME_BUDGET_INTERACTIVE_MS` (500) for interactive `mm push`. The deadline is plumbed through to `walk_session_metadata` via the new keyword-only `deadline_monotonic` param — `_read_cwd_from_latest_jsonl` reads jsonl line-by-line until a `cwd` field appears, so a single pathological project can blow the budget without per-project deadline checks. The `mm: notice: events tail budget exceeded` notice reports specifically on the **session-metadata walk**: `deadline` is reset (events_tail.py) AFTER `walk_git_projects` runs, so the git walk is NOT in this comparison — it self-bounds via its own `total_budget_ms` arg. Git-walk repository loss is a separate degradation (`session_walk_exceeded_budget` is the session-walk flag; do not rename it back). The flag that used to be called `walk_exceeded_budget` measured the session walk and is why git-walk loss was invisible. The check compares a `walk_done = time.monotonic()` snapshot captured the moment the session walk finishes against `deadline`, NOT a post-write `time.monotonic()`. **Load-bearing (v0.12.9):** the snapshot MUST precede `identity.gather_local_identities(allow_refresh=True)` (events_tail.py), whose cold path runs a synchronous refresh bounded by its OWN timeouts (`identity._GIT_GLOBAL_TIMEOUT_S` + `_PER_REPO_BUDGET_S` + `_GH_TIMEOUT_S` ≈ up to 10s, 7d TTL). Pre-v0.12.9 the check sat after the gather + write, so a routine cold identity refresh tripped the walk-budget notice even though the walk itself finished in ~200ms — a misleading signal (the gather emits its own `refreshing identity cache (one-off)` line; the two concerns are now orthogonal). `_run_events_backfill` mirrors this exactly: its `walk_done` snapshot precedes the deliberate `refresh_identity_cache(force=True)` init warm, which ALWAYS runs. Do NOT move either snapshot back below the identity gather. The notice remains a visible-failure-contract signal (the push/init proceeds regardless).

**Git-root discovery budget and reuse (Track 18C, classifier Track 29A).** Root discovery has its own cooperative deadline before the independent git/session walks: `ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS` (50) for autopush and `ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS` (100) for interactive push and init. `events.discover_git_roots()` returns one frozen `GitRootDiscovery(roots, errors, exceeded)` that remains compatible with `roots, errors = ...`; capture retains that exact object and passes it to a cold tail gather or forced init identity refresh so one invocation never repeats the root probes. Explicit `[retro].repo_roots` classify before automatic Claude probes (the gstack prober was deleted in Track 29A — `repo-mode.json` never carried a root path). The deadline is checked before every registry entry, JSONL file/line, and git-root classify step; classification is a `.git` + `HEAD`/`gitdir:` stat, not a `git rev-parse` subprocess. It is cooperative rather than a filesystem-interrupt guarantee: a system call already running may finish, but no later discovery step starts after expiry. Preserve successful roots and append the stable `git root discovery exceeded its time budget` forensic error with `exceeded=True`; never report it as a clean no-repositories result. `Path.exists()` / `Path.is_dir()` raise `PermissionError` on Python 3.11 (the declared floor) and return False on 3.13+; the classifier body is `try/except OSError` and prober `validate()` sits inside the prober `try`, so one unreadable directory cannot abort the events tail. `mm diag` runs discovery at the autopush budget and is the support-facing observation surface.

**Incomplete identity discovery never refreshes cache.** When the supplied `GitRootDiscovery` is incomplete, identity may return `cached identities ∪ newly gathered identities` to that event only, under the phase-3 cache lock. It MUST NOT write cache bytes or `refreshed_at`, even for `refresh_identity_cache(force=True)`: persisting that union would make identities removed from config or a repository survive as trusted local identities. A later complete refresh remains authoritative and may prune them. The version-upgrade normalization below may first mark the cache stale, retaining its old emails; incomplete discovery never persists the newly gathered union.

**No content heartbeat.** The substantive-change gate remains authoritative across UTC rollover: a no-op push without a host append writes no `mm-push` row, does not advance the retro cursor, and must not create a daily activity row merely to express liveness. Attended host usage is a separate observation and may create its daily file. The next substantive push uses the old cursor and captures the idle interval. A no-op `autopush` can still refresh its local `last-autorun.json` success breadcrumb; that means the hook ran, **not** that fleet retro received activity. Liveness needs a separate signal if it is ever required.

**Forensic-only invariant, and why stderr is NOT the load-bearing signal (v0.12.16).** The whole block is wrapped in `try / except Exception`; failures emit `mm: notice: events tail failed: <type>: <safe_str(msg)>` to stderr and the push continues. `safe_str(e)` defangs peer-controlled escapes per the v0.10.1 sanitization invariant (a corrupt peer manifest could otherwise smuggle ANSI through an exception's `__str__`).

That stderr line is the *interactive* signal only. `_run_events_tail` runs from `mm autopush`, which fires unattended from a Claude Code hook, so its stderr reaches nobody — and pre-v0.12.16 `autopush` wrote `_write_autorun_breadcrumb("push", "success")` unconditionally, so `mm status` reported success no matter how badly the retro pipeline had degraded. **`_run_events_tail` therefore RETURNS `list[str]`** — one human-readable phrase per degradation, empty when healthy — which `_push_core` carries on `PushResult.events_degradations` and `autopush` turns into `_write_autorun_breadcrumb("push", "degraded", "; ".join(reasons))`. This mirrors the `degradations` list `autopull` has carried since v0.8.1, and it is the same argument CLAUDE.md already makes for the `no-sources` breadcrumb: without it, `mm status` only ever sees `success` and monitoring built on top of it never catches the wedge. Conditions that populate the list: whole-tail exception, session-walk budget exceeded, token cache cold (tokens + skills omitted), root discovery time-budget expiry, a prober exception (`errors and not exceeded`), and complete discovery that found zero repositories (a prober ran). The budget phrase is fixed: `git repository discovery hit its time budget: this push captured an incomplete repository set. Run mm diag, then mm recapture 30d to recover the omitted commits`; it contains no paths or raw probe errors or the `; ` separator used between breadcrumb reasons. Do not widen the exceeded gate to `bool(errors)` — the budget phrase would then be a lie. An ordinary rejected candidate stays silent. Init backfill has no `mm-push` row or autorun breadcrumb, so it prints the equivalent `initial retro capture` notice only. **Any new degradation detected in the tail MUST be appended to the returned list as well as printed** — a `mm: notice:` with no corresponding entry is invisible to the only surface the user actually reads. CHANGELOG v0.12.13 records the cost of getting this wrong: the unpriced-model breadcrumb "fired for four unpriced models across the whole v0.12.x line and nobody saw it." Pinned by `test_silent_failure_contract.py::test_autopush_breadcrumb_degraded_when_events_tail_fails`. `last-autorun.json` is keyed per verb (`{"push": {...}, "pull": {...}}`) so the documented autopull-at-start / autopush-at-end lifecycle cannot erase a degraded push crumb.

**Tolerant binary reads across every jsonl reader on the push path (load-bearing, v0.12.16).** `_read_cwd_from_latest_jsonl`, `_iter_mm_push_objs`, `token_usage.is_cache_cold`, and `pullhistory._yield_lines` all read BINARY and tolerate a bad line rather than a bad file. Text mode decodes in ~8 KB **chunks**, not per line, and `UnicodeDecodeError` is a `ValueError` — NOT an `OSError` — so the `except OSError` these functions carried never caught it and one invalid byte took down the entire events tail on every push. (Chunked decoding is also why a `cwd` on line 1 did not protect against a bad byte on line 2; measured, it raises at 2 lines apart and returns cleanly at 80 KB apart.) `json.loads` accepts bytes, and both malformed JSON and invalid UTF-8 surface as `ValueError`, so the guard is `except ValueError: continue` per line. Two traps:

- **A reader whose bytes can come from a peer MUST be bounded, not merely tolerant.** `open(path, "rb")` + `for line in fp:` lets Python extend its buffer to newline-or-EOF, so one pathological line kills the push — the OOM `token_usage.iter_bounded_lines` exists to prevent. Which readers need it follows from where the bytes originate, so the rule is per-corpus, not blanket:

  | Reader | Corpus | Bounded? |
  |---|---|---|
  | `_read_cwd_from_latest_jsonl` | Claude Code session jsonls | **Yes** — `iter_bounded_lines` |
  | `_iter_mm_push_objs` | `mm-events` daily files, **synced**, peer bytes arrive via the pull apply path | **Yes** — `iter_bounded_lines` |
  | `token_usage.is_cache_cold` | local token cache | N/A — whole-file `read_bytes` behind an `st_size` gate, no line iteration |
  | `pullhistory._yield_lines` | `~/.config/mind-meld/pull-history.jsonl` | No, and that is fine — the config dir is **never synced** and the file is written only by `pullhistory.append`. Tolerant reading is the requirement here; bounding would buy nothing and would import `token_usage` into a forensic-log reader for no gain. |

  `iter_bounded_lines` is public since v0.12.16 precisely because it grew consumers outside `token_usage`. Its `label` kwarg names a call site in the oversize notice, so do not hardcode "token walker" back into it — but note the notice is deduped by PATH ONLY, so when two sites read the same file the label shown is whichever reached it first.

- **One-shot readers MUST pass `yield_final_partial=True`.** `iter_bounded_lines` defaults to discarding a trailing chunk with no newline, because for `walk_jsonl_segment` that is a partial write to re-read on the next push. A one-shot reader has no next push: the default silently drops a complete-but-unterminated final record. Caught by Codex adversarial review during `/review`, after the first fix had already landed — porting the cwd reader without the flag made it return `None` for a session whose only line was not newline-terminated yet, where the old text-mode reader returned the cwd. Pinned by `test_unterminated_final_line_is_still_read`.
- **`resolve_push_cursor` reaching `CursorResolution.used_floor=True` despite a usable row is NOT a benign fallback.** Losing that row rewinds the cursor to `now - INITIAL_CURSOR_LOOKBACK_DAYS` and re-walks 30 days of git history on every subsequent push, forever. Its pins exercise the bounded `_iter_mm_push_objs` reader through `last_push_ts` and assert the timestamp comes back, not merely that nothing raised.

(`conflictlog.read_records` used to be listed here as a deliberate exclusion. `conflictlog.py` was removed in Track 16A; the exclusion is moot.)

**One cwd scan per project (load-bearing, v0.12.16).** `_read_cwd_from_latest_jsonl` takes the PROJECT dir and scans every jsonl in it. `_scan_one_project` calls it EXACTLY ONCE, after the `sessions == 0` early return. It previously sat inside the per-file loop guarded by `if cwd is None` with a comment claiming "first one wins" — true only when a `cwd` was actually found. When no jsonl in the project carries one, the guard never flipped and the helper rescanned the whole directory once per jsonl: N calls x N files. Measured pre-hoist: 20 jsonls → 400 file opens; 15 ms at N=10, 1.44 s at N=100, **13.2 s at N=300 for a single project** against a 250 ms autopush budget. Note the binary port makes this WORSE if the hoist is ever reverted — today's raise is what truncates the quadratic walk, so a tolerant reader without the hoist runs it to completion on every push. Pinned by call-count (`test_no_cwd_anywhere_scans_the_project_exactly_once`), not by wall clock. The measured cost of the scan itself is 0.82 ms across 37 project dirs (0.33% of the autopush budget), so it needs no deadline of its own — the per-project deadline check at the `walk_session_metadata` loop head is the bound.

**`walk_git_projects` emits each repo at most once (v0.12.16).** The `as_completed` pump and the `FuturesTimeoutError` budget-abort handler are two DIFFERENT except sites and are not interchangeable: the inner one guards `fut.result()` per future, the outer guards the pump and is the only one that marks `budget_abort`. But the abort handler iterates all of `futures.items()`, so without a `collected: set[Future]` it re-collected every future the pump had already drained — every completed repo's full commit list serialised twice into the row, then gzipped, encrypted, uploaded, and replicated to every peer. Measured pre-fix with 4 roots / 2 slow / 300 ms: 4 project rows, 2 unique. `aggregate_git` dedups on `(canonical_remote, sha)`, which is why the card never showed it. Do NOT collapse the two blocks into a shared helper without carrying the set — that refactor preserves the bug and hides it. The pre-existing budget pin makes ALL repos slow (`projects == []`) and structurally cannot catch this; the regression pin uses a MIXED fast/slow set.

**`MmPushEvent.sources` schema is `list[str]` (names only) — Codex C2 + C7.** `iter_source_diffs(skip_unchanged=True)` drops unchanged sources from the diff loop, breaking per-source counts on the no-content push path. The retro-fleet skill (Group 8) reads per-source content stats from the synced manifest at retro time, not from the event row. `make_mm_push_event` filters `MM_INTERNAL_SOURCE_NAMES` from the names list — `mm-events` is mm-owned infrastructure, not user-meaningful fleet activity.

**Fleet retention via tombstone propagation (Codex C10).** `_gc_old_event_files` reaps day files older than `EVENTS_RETENTION_DAYS` (90). The retro skill reads the synced manifest, so deletion fans out fleet-wide via the existing tombstone path: this device unlinks → next push generates a tombstone → peers suppress incoming restoration while the tombstone is unexpired. Existing peer copies remain until their own retention passes; pull does not delete them.

**Reap by FILENAME date, NOT mtime (Codex C5, C6).** iCloud restores can rewrite mtimes back to "now" while the filename date (`<device>-YYYY-MM-DD.jsonl`) is intrinsic to the event-day boundary the file was written for. The mm-events path resolves through `get_sources(config)` so user-customized paths are honored. Always-on (no `--events` flag) — events retention is fleet policy.

**Retention dry-runs are plan-only (Track 17D).** Every retention reaper selects candidates before applying I/O. `mm gc --dry-run` uses that same selection but must not unlink a file, write a cache, or change metadata, and it prints one stable result line for every reaper it executes. The token-cache plan reads under `lockedjson`'s shared read-only snapshot; apply re-plans under the exclusive R/M/W lock so a preview never leaks a stale plan into a write. Bare GC preview labels its conflict-reaper line “deletion requires --conflicts”; passing the flag removes that label. Failed deletes count as failures, not cleanup, and one best-effort failure never prevents the other reapers or orphan-blob GC from continuing.

**Initial cursor lookback (Codex C9).** `last_push_ts(events_dir, device_id)` returns `now - INITIAL_CURSOR_LOOKBACK_DAYS` (30) when no prior `mm-push` event exists. New fleet members joining mid-quarter scan back 30 days of git history; older context is recovered with `mm recapture`. Document the bound in skill output: "First-run window: last 30 days of activity. Older history is outside the automatic window — `mm recapture 90d` on the Mac that owns the repositories."

## Git subprocess environment (load-bearing, Track 55A)

Filesystem discovery selects the repository; inherited Git environment must
not redirect the later subprocess to another repository. All four Git calls
(`events._walk_one_repo`, `events._origin_remote_url`,
`identity._gather_global_email`, `identity._gather_per_repo_emails`) pass
`env=gitenv.scrubbed_git_env()` and explicit `encoding="utf-8"`. The helper is
a leaf importing only `os` and `collections.abc`, returns a fresh dict, reads
the environment at call time, and never mutates the caller's environment.

`GIT_REPO_LOCAL_ENV_VARS` mirrors the list `git rev-parse --local-env-vars`
prints. Remove those names plus every `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*`.
Keep PATH, HOME, GIT_EXEC_PATH, GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM,
GIT_CONFIG_NOSYSTEM, GIT_SSH*, and XDG_CONFIG_HOME. Global and repository
configuration (including `includeIf`) still define configured identities;
inherited `git -c`, GIT_CONFIG_PARAMETERS, and GIT_CONFIG_COUNT/KEY/VALUE
overrides do not. This deliberately differs from pre-commit's environment
policy because these reads decide fleet attribution. Use ~/.gitconfig or
GIT_CONFIG_GLOBAL for supported persistent overrides.

Pin LC_ALL and LANGUAGE to C so Git's localized empty-repository message
cannot become `git_error`. Decode the history read with `errors="replace"`
so a bad display byte cannot abort a walk. Remote URLs and configured emails
use `errors="strict"` and catch `ValueError`: an undecodable identifier is
unreadable (empty remote / absent email), never a lossy new identity.

`tests/test_gitenv.py::test_all_git_subprocesses_use_scrubbed_environment`
checks the AST of every literal Git `subprocess.run`/`Popen` call under src:
the env value must be the canonical helper call, with UTF-8 encoding.
Every subprocess in events.py and identity.py must use list-literal argv,
closing the variable-argv escape there. The detector self-tests missing env,
None, os.environ, and the valid call.
`test_repo_local_env_vars_cover_installed_git` fails when installed Git adds
a repository-local variable outside our set (skips only if Git is absent).

**Cache version is stale, not invalid.** Identity CACHE_VERSION is 2.
`locked_json_rmw` already resets non-dict JSON. `_normalize_cache` resets a
malformed email list (including non-string entries), but preserves old emails
on a version mismatch, sets version 2 and `refreshed_at=None`. Its phase-1
write-on-exit must never erase those emails before a partial gather.

```text
malformed cache -------------> empty stale v2
well-formed old version -----> stale v2, old emails retained
  incomplete discovery -----> return old union partial, keep stale cache
  complete discovery -------> replace with configured identities, fresh TTL
```

Upgrade invalidates the cache on its next identity read; the next read that
completes discovery refreshes it, and incomplete attempts are retried. The
existing one-off notice and gather timeouts apply. Concurrency is unchanged:
the lock is released during subprocess gathering. `read_cached_identities`
uses `lockedjson.locked_json_snapshot`, accepts a string list at any version,
and returns None for an unknown baseline without creating, repairing, or
re-permissioning anything. `mm refresh-identity` shows locally resolved email
changes against that snapshot, including removals before an empty-result
warning. Its JSON output remains only the email list.

**Prevents new mis-attribution; does not repair published rows.** The local
cache can refresh and future capture is corrected, but neither refresh nor
recapture retracts an existing wrong `(remote, sha)` or published identity.
Event day files become eligible for retention after 90 days by filename date,
reaped by `mm gc`, which also runs after an interactive `mm push` that uploaded
changes; never from autopush. No new retraction or generic walker is implied.

## Cursor gate + recapture (load-bearing, Track 30A)

The mm-push row is the sole carrier of `discovery_errors` and `local_emails`. Do **not** hold the cursor by suppressing that row (the CT-4 write-order trick). Keep writing it; gate on the read side via an optional nested `git_capture` key. Absence is the version discriminator (fail open: ADVANCE), same precedent as `local_emails` / `"skills_by_day" not in entry` / `offset`/`head`.

```json
"git_capture": {
  "since": "2026-08-17T02:20:00+00:00",
  "discovery": "complete",
  "walk_budget_aborts": 0,
  "walk_errors": 0
}
```

Do **not** use a single `complete` boolean: discovery and walk fail independently.

**Discovery-vs-walk asymmetry (the design's spine).** Measured 2026-08-25 at HEAD 01726ba:

| measurement | value |
|---|---|
| discovery, any budget 50 ms → 5000 ms | **3.8 ms**, 6 roots, `exceeded=False` (flat in cursor age; **linear in candidate count** — 60 candidates that day) |
| git walk, 1-day cursor, 250 ms autopush budget | **48.6 ms**, 6 projects, 0 aborts |
| git walk, 30-day cursor, 250 ms autopush budget | **251.3 ms**, 5 projects, **1 `budget_abort`** |
| git walk, 30-day cursor, 500 ms interactive budget | **335.1 ms**, 6 projects, **0 aborts** |
| git walk, 30-day cursor, **zero** roots | **0.007 ms** |
| `last_push_ts` at the shipped 31-day bound | 0.30 ms |
| parsing all 34 retained daily files for the busiest device | 19.7 ms |

Git-walk cost is *monotone in cursor age*. Discovery cost and a zero-root walk are *flat* in cursor age. Holding the cursor on a walk failure creates a positive feedback loop (held cursor → longer walk → more aborts → held further back) that permanently wedges unattended autopush. Holding on discovery failure or zero-roots cannot.

`walk_session_metadata` ignores `since` (`events.py`, `# noqa: ARG001`, v=2 full inventory). That is *why* holding on zero roots is free: the session walk does not grow with cursor age.

**Walk failures NEVER affect the cursor.** `capture_advances_cursor` is an allowlist on `discovery` only (`complete` / `not-run` ADVANCE; `partial` / `empty` HOLD; absent / malformed / unknown ADVANCE). A denylist would default every future value to HOLD, which is the wedge direction.

**Budget escalation is required, not optional.** When the resolved cursor is older than `WALK_BUDGET_ESCALATE_AFTER` (1 day), escalate the git walk from 250 ms to 500 ms. Without this, the recovery push that HOLD exists to enable drops a repo (251.3 ms / 1 abort) and then advances past it.

**Retention-bounded latest-row lookup.** `last_push_ts` scans `CURSOR_SCAN_DAYS` (90, equal to `EVENTS_RETENTION_DAYS`) and records `git_capture.since` so a gap older than retention is still explicit. Neither alone is sufficient. Fresh install (no rows at all) returning the 30-day floor is the documented first-run state, **not** a degradation — fire the hold/coverage phrases only when an older complete row existed and was chosen over a newer incomplete one. Future / timezone-naive / malformed `ts` cannot move the cursor forward.

**Git-walk repository loss is visible.** `walk_git_projects` drops repositories into `skipped` with typed reasons (`budget_abort` / `timeout` / `no_commits` / `git_error` / `raised`). Never interpolate a skip reason into a degradation phrase: git stderr carries branch names and absolute paths onto a synced row. `no_commits` (`rc=128` + "does not have any commits yet") is benign — an empty `git init` directory returns it on every push forever. The session-walk budget flag is named `session_walk_exceeded_budget` so it cannot be mistaken for git-walk loss.

The path-free `_GIT_WALK_DEGRADATION` phrase is `git walk dropped {n} repositories this push. Run mm recapture --dry-run to check current repository failures, then mm recapture 30d to recover the omitted commits`. It contains no `; ` separator. Status repeats the current-failures check before its `mm diag` link. Dry-run lists each skipped path via `safe_str` and its typed reason, labels `no_commits` benign, and links `GIT_WALK_FAILURES_URL` for non-benign skips. These paths already ride the synced git-snapshot row. The dry-run is a fresh scan with interactive budgets, not evidence about an earlier push: a clean result cannot establish that the earlier capture was complete. Partial recapture gives a same-window retry for Git failures and narrower-window advice for budget aborts; mixed failures show both with separate counts.

**`mm recapture [WINDOW]`** is the only path that recovers already-orphaned intervals. Shape: write git-snapshot rows (marked `origin: recapture`, no mm-push) first, then run the ordinary push path. Catch ``SnapshotError`` from that push the same way interactive ``mm push`` does: print that the recapture was written locally but not synced, then the four-part refusal, exit 1, no traceback. Do **not** add a `recapture_requested` disjunct to the substantive-change gate (that re-opens the v0.12.2 phantom-change path). Do **not** route through `_run_events_backfill`. Window is `Nd`, default `30d` (= init backfill), min `1d`, max `90d`. Partial recovery exits 4 (exit 3 is `pull --conflict-mode fail`). Zero roots writes nothing and exits 1. Retros window by the COMMIT's date, not by when mm captured it: `mm recapture 90d` produces zero visible change on a `mm retro-fleet 7d` card. `_coverage_floor_from_files` still uses the event filename date by design.

## Host-usage snapshot capture (load-bearing, Track 19A)

The tail publishes the local Codex / Grok readers as one additive
`host-usage-snapshot` row. `host_usage` stays the sole reader and
model-family authority; `events.make_host_usage_snapshot` is a pure
constructor; `events_tail._capture_host_usage` owns the timing and the
publication decision. Additive `tokens_by_day` (Track 33A) is `{UTC-day:
DayBucket}` — omit iff `hosts` is empty, otherwise always present, so its
absence is the mixed-fleet version discriminator. A sibling that would be
EMPTY beside non-empty `hosts` is also omitted: that shape is the one every
peer drops as `active_days_mismatch`, under a remedy blaming the writing
machine's mm version, and absence already means "pre-33A peer OR no per-model
data". No `EVENTS_SCHEMA_VERSION`
bump — legacy consumers already skip unknown types.

**All-or-nothing for FAILURES (premise revised Track 31A, 2026-08-27).**
Sweep-level atomicity is retired. A file/record failure still fails that
whole reader (each reader stays all-or-nothing *internally*). A reader
failure no longer discards the others: the failed reader is dropped from
`token_sources`, listed in additive `degraded_sources` (a subsequence of
`HOST_USAGE_TOKEN_SOURCES`, disjoint from `token_sources`), and named in
the tail's degradation list. A row is omitted only when *no* consulted
reader completed, or the sweep expired before any reader was invoked.
Never publish an undeclared omission. The original argument ("partial
totals are worse than no totals") predates `token_sources`; with per-reader
coverage on the wire, deleting known Codex tokens because Grok failed is
now the less truthful behaviour. An ABSENT row still means "unknown",
never zero. `no_metadata_ledger` stays a silent absence, not a dropped
failure.

**But an ABSENT source is not a failure (premise revised 2026-08-16).** The
original reading treated Grok's refusal as a failure, which meant that merely
having Grok installed made the row unpublishable — forever, on that machine.
Measured live before the revision: `read_grok_usage` returns in 0.039ms and
discarded a complete 6.4B-token Codex scan on every push, while pinning
`mm status` at `degraded` permanently and so destroying that breadcrumb as a
signal for real sync degradation — the exact failure mode the `claude_paths`
guard a few lines away exists to prevent.

`_HOST_ABSENT_REASONS` (today: `no_metadata_ledger`) marks a store that, by
design, holds no metadata-only usage ledger and never will. Grok produces
this on closed-default consent. That reader is dropped from `token_sources`
and the sweep continues. **This is deliberately not keyed on `unsupported`:**
Codex returns `unsupported` for a ledger it cannot attribute, which means
"real usage is here and I could not read it" — that keeps the veto. Getting
that distinction backwards silently under-reports the fleet.

**Host readers are consent-gated.** `HOST_READER_SOURCE_GATE` maps Codex and
Grok to the source name whose being enabled authorizes them. The live set is
`events.ACTIVE_HOST_READERS`. `HOST_USAGE_TOKEN_SOURCES` is the live writer
tuple (same names, same order). Unknown inbound names (retired `opencode`,
or a future reader) are retained by the aggregator, not listed here.
`_default_host_readers(sources, grok_consented=...)` returns only those.
A user who declined the `codex` source does not get `~/.codex/sessions`
parsed — matching `_enabled_claude_paths`. Grok is a scoped sync source
(`type: "grok"`, hardcoded `skills/` / `commands/` / `rules/`).
`HOST_READER_SOURCE_GATE["grok"]` is `"grok"`. The 21A `[retro].grok_host_usage`
bit remains an OR so a prior usage-only opt-in does not go dark.
The callable bound into the sweep passes `read_grok_usage(..., consented=True)`.
The function itself defaults to `consented=False` and does not stat or open
`~/.grok`. Empty sources plus no opt-in and no bit must yield no Grok reader.
The grok *file* walker never opens `sessions/`; the usage reader is a
separate consented walk of `updates.jsonl` only. Host interchangeability vs
Claude is `docs/designs/host-parity.md`: 22A/23A put totals on the MODELS
card; Grok does not get an `AgentRow` (it discovers `~/.claude/skills`; `mm diag` measures that under `host_skill_discovery`); session-transcript sync and a
Codex/Grok `sessions-snapshot` are not planned.

**Grok v1 terminal ledger (Track 18D, ignorable keys Track 46A).** When consented, `read_grok_usage`
walks `GROK_HOME/sessions` (else `~/.grok/sessions`) and reads only regular
non-symlink `updates.jsonl` files directly under a session directory. It
accepts a line when `params.update` is a `turn_completed` whose key set,
minus `_GROK_IGNORABLE_KEYS` (`elapsed_ms`), equals `_GROK_REQUIRED_KEYS`
(`prompt_id`, `sessionUpdate`, `stop_reason`, `usage`) with no
content-bearing fields. Ignorable keys are dropped before the projection
and are never stamped onto the stored turn dict (`{key, day, model, usage}`;
resume compares live == cached). An unknown extra key is still `unsupported`
and still refuses the whole reader (see "Reader-agnostic quarantine and drift
classification" in `docs/roadmap-future.md`). Each accepted record is a per-prompt total attributed
to the UTC day of the outer timestamp. `reasoningTokens` must be a bounded
subset of `outputTokens` and is never added twice. The private
`grok-host-tokens.json` cache stores opaque file keys, fingerprints, offsets,
hashed terminal keys, model IDs, and aggregate counters — never paths,
prompt IDs, or conversation bytes. Equal duplicate
`(workspace, session, prompt_id, model)` records count once; conflicting
duplicates refuse the store. The model is always part of the key so a
later multi-model restatement of the same prompt cannot double-count.

**First-success carve-out (Track 21A) — retired from sweep policy in Track 31A.**
The latch's premise was already false (`complete_once` arms on an empty
`updates.jsonl`, i.e. file existence, not content), and making Grok succeed
once would have armed a permanent fleet-wide veto on the next Grok wire
drift. Reader-scoped isolation replaces it. `host_usage.grok_completed_once()`
is kept as a **diagnostic** latch (and so the three
CI-enforced doc citations to it still resolve). `mm status` / `mm diag`
prefer any standing `last_reason`, then this diagnostic latch. Do not reintroduce it as a sweep gate. Warm a warmable reader on a `deadline` in `dropped` (or on the
sweep-level `reader`/`reason` for a pre-any-reader expiry); autopush never
warms. Warm every deadline-dropped warmable reader in reader order, including
uninvoked readers. Pre-invoke sweep expiry declares every reader dropped and
warms every warmable reader. The attended warm IS the retry (Track 63A):
`warm_host_cache_inline(reader=name)` runs through `_capture_host_usage` on a
singleton reader with an explicit `DEFAULT_READ_BUDGET_S` deadline. Merge its
result, including failures, with the first pass's completed readers — a flaky
second-pass read of an already-completed reader must never erase totals
already captured. If
the retry expires before invoking a reader, it has no replacement outcome, so
retain the initial deadline declaration.

**Grok terminal skip (Track 31A, partial discharged Track 34A, ignorable keys Track 46A).** A `turn_completed` whose `params.update`
key set, minus `_GROK_IGNORABLE_KEYS`, is exactly `_GROK_REQUIRED_KEYS - {usage}` is a zero-token skip,
counted on the Grok cache as `usage_less_skipped`. `_classify_grok_update` is the
single decision; placing a carve-out at `usage = update.get("usage")`
is dead code. The usage-less path MUST keep working with an ignorable key
present (1 of 229 live records at the 2026-09-04 census). `usage` present-but-not-a-dict stays fatal. `usageIsIncomplete`
is an `is True` identity check (never truthiness) and marks that UTC day
partial; see Coverage states below. An absent `updates.jsonl`
is not an I/O error (`FileNotFoundError` / `NotADirectoryError` → skip;
other `OSError` still `io_error`).

**`token_sources` is therefore per-push, not the constant.**
`events.ACTIVE_HOST_READERS` is the live reader universe;
`events.HOST_USAGE_TOKEN_SOURCES` is the live writer tuple. Unknown inbound
names are retained by the aggregator. A row carries the subset that
actually contributed. That is what lets a consumer tell "this host
reported nothing" from "this host was never consulted". A machine with no host
sources enabled still emits a row with `token_sources: []` — absence of the ROW
has to keep meaning "something failed".

**`hosts: {}` is a fact, not a fallback.** A completed scan on a machine with
no host data writes an explicit empty snapshot and emits no notice or
breadcrumb. It is the ONLY empty ``hosts`` shape on the wire, but its meaning
is always scoped by that same row's ``token_sources``: a nonempty list says
those completed sources observed no host data; an empty list says no source
contributed. Neither form is fleet-wide zero. The omission case writes nothing
at all.

**Reader emptiness (65A).** Production capture supplies `usage_days` to
`make_host_usage_snapshot`; it always writes `empty_sources`, possibly `[]`.
The optional argument defaults to None, which omits the key and makes no claim.
Each reader's own `result.hosts` day keys (even all-zero buckets) are recorded
before family merging, retained through the warm retry, and intersected with
the row's `keep` set. No model-family lookup can identify an empty reader.

### Track 20A consumer handoff: complete, coverage-aware snapshots

Track 20A locks this existing writer shape for its future consumer; it does
not add a parser, status row, schema bump, or producer-side validation. A
consumer uses only **accepted** rows and holds one complete, whole-device view
per device.

- An absent ``host-usage-snapshot`` row is no new complete observation. It is
  never zero, never a state update, and never evidence of a specific cause:
  the writer may have been incomplete, legacy, no-op, dry-run, or writing
  init backfill (which has no ``mm-push`` anchor). Do not infer a failure from
  an absent row or correlate it to a later ``mm-push``.
- A complete later row replaces the entire accepted view for that device.
  There is no per-source carry-forward: historically Codex and OpenCode
  merged into one ``codex`` family, and after 36A a legacy peer may still
  emit both, so the wire lacks the source-to-family precision required to
  merge partial observations safely.
- ``ts`` is the snapshot's sole supported ``as_of`` signal. A retained row is
  a last-known-good point-in-time view, not a claim that host state is current.
  Renderers must keep its coverage and ``as_of`` context rather than reducing
  an empty or uncovered row to a bare zero.

**Eligibility is all-or-nothing.** Track 21 accepts a row only when all known
core fields are valid:

- ``v`` is the current event-schema integer, ``type`` is
  ``host-usage-snapshot``, ``ts`` is a timezone-aware ISO-8601 timestamp, and
  ``device`` is a nonempty string. The current short-device-id convention is
  not itself a UUID-format parser requirement.
- ``token_sources`` is an order-preserving, duplicate-free list: known names
  must be a subsequence of ``HOST_USAGE_TOKEN_SOURCES``; unknown names
  (retired or future readers) are retained if they pass the identifier bound.
  A nonempty ``hosts`` payload requires a nonempty ``token_sources``;
  ``hosts == {}`` with ``token_sources == []`` is valid and means no source
  contributed.
- ``hosts`` is either ``{}`` or only canonical, nonempty host-family maps.
  Every key is a real UTC ``YYYY-MM-DD`` date; every bucket contains exactly
  the four ``TOKEN_FIELDS`` as non-boolean integers in ``[0, 2**53]``; and the
  post-cap union contains no more than ``MAX_BY_DAY_DAYS`` days.
- ``active_days`` is derived convenience metadata, not an authority: it must
  equal the sorted post-cap union of UTC-day keys in ``hosts`` exactly.

Unknown additive **top-level** fields are ignored for forward compatibility.
Unknown nested families, malformed days or buckets, invalid known core fields,
or an ``active_days`` mismatch invalidate the whole row; a consumer retains an
earlier accepted row for that device rather than salvaging partial totals.

**Deterministic selection.** For each device, compare accepted rows by parsed
UTC instant and select the greatest. On an exact instant tie, compare the
lexicographically greatest stable compact JSON serialization (sorted keys,
fixed separators) of only the normalized known-core projection: ``v``,
``type``, normalized-UTC ``ts``, ``device``, ``token_sources``, ``hosts``, and
``active_days``. **Neutral** additive top-level fields
(``skills_by_day``, ``offset``/``head``, ``tokens_by_day``, ``partial_days``,
``degraded_sources``, ``partial_sources``) are deliberately excluded from
that core projection, so they cannot change a winner they otherwise leave
semantically unchanged. **Semantic** additive siblings are different:
``counter_semantics`` (Track 35A) changes what the numbers *mean*, so it
participates in ``_sibling_tie_key`` (appended, never prepended). Two
equal-``ts`` rows that differ only in semantics must not select by
encounter order. A clock-backdated row remains older by ``as_of``; JSONL
encounter order is not a safe physical-time signal.

**Counter semantics (Track 35A).** Host readers do not share a counter
schema. Codex CLI and Grok CLI are **inclusive** (``input`` already
contains ``cache_read``; ``cache_create`` is subtracted too, because no
local corpus can prove it is outside ``input``). Claude session jsonl is
**disjoint**. OpenCode was disjoint too; its reader is gone as of v0.12.53.
Semantics is a property of the READER, not the model id — ``grok-4.6``
arrives both ways. Inclusive extractors emit disjoint buckets via
``uncached = input - cache_read - cache_create``. Do **not** call
``_normalize_inclusive_usage`` in ``_add_usage``: that is where readers
converge, and subtracting ``cache_read`` from an already-disjoint bucket
would clamp real billable tokens to zero. The extractor-merge recipe that
once threatened this path (Track 49A under the 2026-09-03 numbering) was
killed on 2026-09-05; what survives is the deferred "Share host-reader
filesystem resume primitives only after measuring duplication cost". A bare
"every current reader is inclusive" assertion still would not survive any
such merge, so keep the qualifier. Malformed inclusive counters
(``cache_read + cache_create > input``) degrade that reader (Track 31A
isolation), not a fabricated zero bucket.

The wire sibling is ``"counter_semantics": "disjoint-v1"``. Key **absent**
means legacy inclusive/unknown. Only that exact string is priceable;
unknown values fail closed. It describes both ``hosts`` and
``tokens_by_day``. Do **not** bump ``EVENTS_SCHEMA_VERSION`` for it, and
do **not** nest it inside ``hosts`` (unknown family keys reject the whole
row). A peer without the marker renders ``—`` for host token columns and
API-list-rate figures, never a number: inclusive counters would be a
ceiling up to ~2x high, the one caveat that points the wrong way.

**Agent economics rendering (1.1).** A snapshot that predates the requested
window contributes no usage to the unified rows: it contains no observation
in that window, so a confident ``~$0`` would be absence-as-zero. An
all-unpriced disjoint row retains its token volume and renders ``—`` for cost
because it has no priced basis; ``cost_unavailable`` in ``MM_HEALTH`` names
the missing basis, with ``unpriced_models`` naming unpriced volume when present.
A partly priced row renders its priced subtotal
with ``≥`` and named ``cost_floor`` causes. The old per-machine economics
table and its row cap no longer render.

### Track 22A consumer: last-known-good inventory

The aggregator is the first consumer of accepted host-usage-snapshot
rows. The acceptor lives in ``aggregator._accept_host_usage_snapshot``,
not in the writer. A complete later row replaces the entire device view;
there is no per-source carry-forward.

The winning row is kept **whole**. ``HostDeviceSnapshot.lifetime_by_family``
is inventory as of ``as_of``. The consumer retains this per-device inventory;
the 1.1 renderer sums its in-window buckets into agent rows and checks for
duplicated ledgers as described in the unified-agents contract below.

**What a day key actually is (corrected TWICE — read both corrections).** The
pre-v0.12.37 wording said "day keys are last-touch lifetime totals", which was
wrong and mis-designed Track 23A. The v0.12.37 replacement described a
CUMULATIVE terminal total per rollout file keyed to that file's last-touch UTC
day, which was accurate for the reader of its time and is **no longer true for
any reader**.

Since Track 32A every reader is per-turn. A day key is *the work actually
recorded on that UTC day*: Codex differences the host's own cumulative counter
between consecutive readings (`host_usage._read_rollout` collects `_TurnState`
readings, `_aggregate` keys transitions by `(lineage, previous, current)` and
sums them), Grok has been per-turn since v0.12.47 (`_aggregate_grok` over
`entry["turns"]`). `_aggregate` passes those already-disjoint Grok turns
through without normalizing again; the obsolete `_Terminal` representation is gone. Buckets are therefore additive and
stable: a fixed day's value no longer moves when an old session is resumed.

**Track 32A retired three derived prohibitions.** Retired: "a window
slice over-counts at the recent edge", "active DAYS is a lower bound because
restatement erases the old key", and "never label a column spend". All three
followed from cumulative-with-restatement, which no longer exists. Migration
can still put overlapping history under two device ids. Track 67A replaced
the resulting no-sum rule with a fleet sum plus ``duplicate_ledger`` disclosure;
it did not claim that changing the counter shape made migration impossible.

**Do not compare a window that straddles v0.12.48.** Codex totals before it
double-counted work shared across forked and resumed rollout files — measured at
roughly 55% of the reported figure on a 746-rollout corpus. A trend computed
across that boundary shows a large fake decline.

- **A count of active DAYS under-counts, and is therefore a LOWER BOUND**, for a
  changed reason. Restatement no longer erases the old day key: Track 32A made
  every reader per-turn, so day keys are stable. What remains: a peer on an
  older mm still publishes the old shape, and a machine that never pushed in a
  window contributes no days at all. The unified table's ``Days`` column is an
  observation, never a census; never diff or chart it. Once ``tokens_by_day``
  is always present when ``hosts`` is non-empty, its *absence* is the mixed-fleet
  version discriminator (same pattern as ``skills_by_day`` / ``offset``/``head``).

**Migration remains a disclosure condition.** ``device_id`` lives in local
``config.toml`` while the host stores (``~/.codex/sessions``,
``~/.grok/sessions``) sit outside every mm sync source. Migrating a home
directory and running ``mm init`` fresh can put copied history under two
device ids. The renderer sums the reported usage and detects matching daily
counter tuples; ``duplicate_ledger`` warns that affected token and cost totals
may include copied history. Matching totals are evidence to inspect, not proof
of migration. A **day-set union** remains unaffected by duplicates.

**Day keys are UTC calendar days; the card header is LOCAL.**
``_render_ascii_card`` builds its date range from ``since.astimezone().date()``
while day keys are UTC, so the two disagree by a full day when the retro runs
late in the evening in a negative-offset zone (verified). The in-window day
filter is an inclusive UTC-date span, hence a strict superset of the instant
window every other card number uses, and its numerator can reach
``window_days + 1``. **Render no ratio against it** — a denominator would
visibly contradict the header. That is why v0.12.37 dropped the denominator
rather than clamping the numerator.

A snapshot cannot have observed activity later than it was taken, but **nothing
enforces that on the wire**: the acceptor validates day-key format and ``ts``
independently and never relates them, so a backdated peer can ship an ``as_of``
before the window WITH in-window day keys (verified constructible). A consumer
must clamp to ``min(until, as_of)`` rather than trusting the property; that also
subsumes the stale case.

Coverage fields are the only honest zero-prevention:
``by_device``, ``consulted``, ``as_of``, ``stale``, ``future_dated``,
``devices_without_accepted_row``, ``rejected``. There is no fleet
``consulted_sources`` union — one Grok-opted-in Mac must not mark the
fleet Grok-covered.

Host totals never mutate ``sessions.tokens_by_model`` or snapshot
``metrics.tokens_total``. Host ``claude-*`` usage does join Claude session usage
in the render-only ``FleetAgentRow`` and shares its cost calculation.

### Unified-agents renderer contract (Track 67A, supersedes 23A)

#### One table, one unit

Claude, Codex and Grok render **identically**: fleet-summed tokens, active
days, contributing machines, estimated cost, top model. The card's `AGENTS`
block and the body's `## Agents` table share that row shape, and comparing the
rows is legitimate — they share a unit, a window, and a counter basis.

The card's `N of M registered machines` scope counts currently registered
machines with observed agent usage. The table's `Machines` column counts
actual contributors: historical Claude session/token/skill data remains after
deregistration, with an `unregistered_devices` health explanation. Do not drop
that usage or reduce its contributor count to make it match the card scope.
When the registry is unavailable, the card counts all observed machines and
omits the denominator.

This replaced a split that had become indefensible. Pre-1.1 the card carried
`MODELS (Claude Code sessions)` (Claude token magnitudes) above `AGENT LOGS`
(Codex/Grok day counts), with a standing prohibition on comparing them, and
the body carried three sections describing one fleet three ways. The split
rested on a claimed asymmetry in what the data could support. There was none:
`CLAUDE_SCOPE` said out loud that Claude's tokens were a "sum of per-machine
inventories, not deduplicated (a migrated home directory can be counted
twice)", while `HOST_SCOPE` refused to sum on that exact hazard. Same risk,
opposite treatment, and the one that got a number was the one whose vendor
also wrote the tool.

#### Summing host tokens, and detecting possible overlap

`_agent_rhythm_view`'s pre-1.1 docstring justified the refusal by asserting
that a migrated home directory yields two device ids with overlapping history
and "the aggregator has no signal that could detect the overlap". Copied
ledger days do provide a signal: their counter tuples can match exactly on
both devices. The signal is not proof. Different model inventories can sum
to the same family/day tuple, and a migrated ledger can subsequently diverge.
Neither a match nor its absence proves whether all retained history overlaps.

`_detect_duplicate_ledgers` (over `_host_family_day_tuples`) is that signal. It
reports affected FAMILIES, and `format_retro` raises health code
`duplicate_ledger`. Probed against a real two-Mac fleet before the change: 14
shared `(family, day)` pairs, zero identical tuples. That probe exercises a
concurrent-use case; it does not establish that false positives are impossible.

Day counts remain a set union and are therefore idempotent under a duplicated
corpus, so possible copied history does not inflate the Days column.
`duplicate_ledger` means the token and cost totals may double-count. Inspect
the named families and devices before deciding whether a device was retired;
do not automatically discard a device or ledger based on equal totals.

#### Row assembly (`aggregate_agent_usage`)

- **Claude** comes from the Claude Code session corpus, not re-bucketed by
  `host_family`. Its `tokens` derive from the same `by_model` map that prices
  it — reading volume from `SessionsAggregate.tokens_input` and cost from
  `tokens_by_model` lets the two drift.
- **Host families** come from `lifetime_by_family` (volume and days, always
  available) and `_windowed_host_by_model` (the priced basis, absent on a
  pre-33A peer). A family keeps its token volume and loses only its cost cell
  when the per-model sibling is missing.
  Missing or rejected detail identifies the contributing machine and preserves
  the acceptor's `detail_reason` when available, so an operator can distinguish
  an older peer without detail from malformed detail that was dropped.
- **Classifier disagreement preserves accepted wire totals.** Before pricing
  a host snapshot, compare locally classified per-model sums with its wire
  family totals for each day and counter. If a local model sum exceeds the
  corresponding wire total, suppress that snapshot's pricing detail rather
  than moving accepted tokens between families or pricing an inconsistent
  basis. Other snapshots can still supply a priced subtotal for the row.
- **`claude` is a legal host family**, so a host ledger carrying `claude-*`
  models merges INTO the Claude row rather than being dropped. The row means
  "usage of this model family across the fleet". The two corpora cannot
  overlap — `host_usage` reads Codex and Grok ledgers, never Claude Code's own
  session jsonls — so this is a sum, not a double count.
- **`AGENT_ROW_ORDER` is the only label registry.** The pre-1.1 pair
  (`MODEL_FAMILY_ROWS` + `AGENT_FAMILY_ROWS`, with deliberately different
  labels like `Claude (via agents)`) existed so two adjacent card blocks could
  not be confused. With one block that distinction has no referent, and a
  second label set is drift waiting to happen. Keys must equal `_HOST_FAMILIES`.
- **Card machine scope is a device-id union.** Include both contributing host
  devices and Claude session `token_devices`; a machine present in both counts
  once. A Claude-only machine counts even when no host snapshot exists.

#### Floors are per agent, and name their cause

`FleetAgentRow.floor_causes` is the fleet-wide successor to the per-device
cause list `_device_economics_cell` built. Collapsing a per-machine table into
one row must not drop the REASON a number is a floor — only the machine it
happened on.

- Causes are attributed **per family, not per machine.** One Mac running both
  readers publishes ONE snapshot; a machine-scoped cause list put Grok's "logs
  do not record per-request prompt sizes" onto the Codex row, which is a claim
  about a different vendor's log format.
- Reader coverage causes (`partial_sources`, `degraded_sources`) apply to
  **all families**, even families this machine contributed no data for. The
  wire carries no reader-to-model attribution, and a failed or partial reader
  cannot establish which families are missing. Do not infer ownership from
  a reader name or from the families that happened to contribute. The simple,
  conservative result is `≥` on every agent's available priced subtotal.
  Only snapshots eligible for the requested window propagate reader causes;
  stale inventory remains available for diagnostics without repricing current rows.
  Unusable coverage metadata is the same scope: a snapshot that carries
  `partial_reason` or `degraded_reason` but empty `partial` / `degraded` lists
  still floors every host family, including when that machine contributed no
  in-window usage. An empty reader list is not "no problem"; a healthy peer
  would otherwise render an ordinary estimate.
  Inherent pricing-tier causes remain specific to the models they describe;
  each machine's recorded-token bounds name that machine in the health payload.
- `residual` (the per-day model cap leaving unattributable tokens) is
  genuinely cross-family — `tokens_by_day` day totals are not
  family-partitioned — so it is shared across every family the snapshot
  touched.
- **Legacy inclusive counters render `—`, never a number.** `counters_known`
  is False when any contributing snapshot lacks `disjoint-v1`. Inclusive
  counters run up to ~2x HIGH: the one caveat that points the wrong way, and
  therefore the one that stays a rendered marker rather than moving into the
  health payload. `legacy_counters` names the affected machine(s) and uses the
  attended-capture version floor in its peer-upgrade remedy. Legacy rows skip
  numeric token/pricing and pricing-extrapolation health details as well.
- **Pricing health matches the visible cost cell.** Emit `cost_floor` only
  when the row has a priced subtotal rendered with `≥`. A visible non-legacy
  row with no priced basis renders `—` and emits `cost_unavailable`, retaining
  the reasons and remedies for missing, dropped, or entirely unpriced detail.
  Do not describe an unavailable cell as a priced floor.
- A priced model retained at zero tokens is not a priced basis.
  `_agent_row_cost` returns unavailable unless some non-excluded model has
  nonzero volume under a resolved card, so a zero-token priced id cannot turn
  an otherwise unpriced row into `≥$0`.

#### The health payload replaced the Notes section

Machine/model pricing details are bounded by `MAX_AGENT_FLOOR_CAUSES`, with
an omitted-count summary. Full causes remain in the aggregate; the skill-facing
payload must not grow one detail per device without a bound.

`_render_health_block` emits `MM_HEALTH` on the FIRST pass only — the second
pass is the shareable artifact. The body carries at most one italic line.

This is an inversion of responsibility, not a deletion. A program must
pre-render every caveat because nothing downstream can judge; this renderer
feeds an LLM skill that can. The current health-code interface preserves
diagnostic meaning and remedies; it does not promise that every old Notes
sentence survives verbatim. `pricing` provenance (vendor rate dates and URLs) rides
the same payload because a dollar column is uninterpretable without it, but it
is reference material rather than something a reader needs every run.

**Health codes are the interface, not the prose.** `test_docs_routing` walks
every `note(<code>, …)` call and requires the code appear in SKILL.md; reword a
detail freely. Two codes change what a number MEANS and must be called out by
name in the skill: `duplicate_ledger` and `zero_repo_capture`.

#### Machines are named, not hashed

`device_labels` / `device_label` resolve `device_id` to the registry's
`device_name` (set from `socket.gethostname()` at `mm init`), falling back to
the short id. The data was always on the wire and in `FleetState`; the retro
simply never used it, and every table keyed on 8-hex ids nobody can read.
Peer-controlled, so it takes the same `_safe_short` path as any wire string.
When multiple devices share a hostname, their labels include short device IDs
so a health remedy can identify the intended machine.

#### What was removed, and why it is not hiding somewhere

- `## Claude Code activity` — a Claude-only token table, session count, and
  cache-hit ratio. Session count and ephemeral-workspace counts have no
  Codex/Grok analogue; cache-hit ratio is not something the user controls.
- `## Agent activity` — per-machine `(machine, family)` inventory.
- `## API list-rate equivalent (per machine)` — per-machine dollars behind a
  `### Do not sum these values` heading and a five-line marker legend. A
  currency table keyed on unreadable ids that warns you not to add it up is a
  table that should not have been rendered.
- The commit-type mix and burst shape moved to `MM_THEMES_PROMPT`: useful for
  synthesizing a theme, near-useless to read (the mix's largest bucket is
  routinely `other`, and the burst count tracks the commit count).

Machine-level forensics belong in `mm diag` and `--dump-host-usage`.

- **Absence is never silent.** `_agent_coverage_notes` still names the cause
  with a remedy every time the block is quiet: no snapshot yet, no reader
  contributed, all snapshots stale, or nothing active. `token_sources` is
  per-push contribution state, so the second case may mean no source is enabled
  **or** that each selected reader had no attributable local ledger; the
  renderer must state that ambiguity rather than falsely diagnosing consent.
- **Two version floors, two constants.** `SKILL_MIN_VERSION` gates the
  RENDERING machine (below it there is no unified table and no `MM_HEALTH`, so
  a 1.1 SKILL.md reads for surfaces that do not exist).
  `ATTENDED_USAGE_MIN_VERSION` gates a PEER publishing a usable capture and is
  the target for peer-binary upgrades needed for host capture. Local pricing
  upgrades and diagnostics have their own remedies. Conflating these scopes
  sends a fine Mac in a circle.

### Coverage states (Track 34A)

Two additive, omit-when-empty source-name lists travel on the
`host-usage-snapshot` row. The local writer emits duplicate-free subsequences
of `HOST_USAGE_TOKEN_SOURCES`; the acceptor also retains identifier-bounded
unknown names without relaxing the known-name order. The fields have the same
shape and opposite disjointness contracts. **Do not unify them.**

- **`degraded_sources`** (shipped v0.12.47): readers that failed this sweep.
  DISJOINT from `token_sources` (enforced at `events.py` by filtering against
  the contributed set). A failed reader contributed nothing.
- **`partial_sources`**: readers that contributed usable totals, but the host
  explicitly declared those totals incomplete. INTERSECTS `token_sources`.
  A well-meant unification that filters partial the same way as degraded
  silently drops every partial signal. That is the 1-year failure mode.
- **`empty_sources`** (65A): consulted readers with no usage day in this
  snapshot's retained days. A subset of `token_sources`, disjoint from
  `partial_sources`; written even when empty when reader-day evidence is supplied.
  It is neither a lifetime claim nor a classification by model family.

**Partial is day-scoped until the writer, then row-scoped.** The Grok reader
carries `HostUsageResult.partial_days` (UTC days that had a `usageIsIncomplete
is True` turn — identity check, never truthiness). Both merges union those
day sets beside `contributed`, never inside `_merge_host_usage_maps` (that
helper never sees reader identity). `make_host_usage_snapshot` intersects
with the same `keep` set `hosts` and `tokens_by_day` use, then reduces to
source names. Coverage fields on the wire are therefore **row-scoped, not
day-scoped**. A future day-scoped coverage map would have to join `keep` in
the same pass; a lifetime source-level boolean would let one incomplete turn
older than `MAX_BY_DAY_DAYS` mark every future snapshot partial forever.

**Cache.** `_validated_grok_entry` persists `partial_days` (possibly empty)
on the Grok cache entry. Pre-34A entries are detected by **key absence** and
re-walked once. Not a `CACHE_VERSION` bump: that constant is shared with the
Codex namespace. Track 46A was retargeted away from an encoding rewrite
(the 25 MB / 100 ms trigger does not fire; measured 4.11 MB / 23.3 ms);
`last_reason` on either cache *root* is independent of that per-file
migration: its absence is documentary, not a discriminator, and never
forces a re-walk of per-file entries. Encoding work stays deferred until
the TODOS trigger fires.

**Acceptor.** Three-way on key presence, never a falsy check, reusing
`_token_sources_subsequence`. Absent = no signal, not a broken peer — do not
nag "too old". Present-but-invalid drops the field, keeps the row, and
records the drop reason so the dump cannot read as "no known coverage
issue". `partial_sources` beside empty `hosts` is rejected as a claim.
`degraded` joins `_sibling_tie_key` (appended, never prepended) so a
degradation cannot make a row win; a malformed sibling MAY change the
winner, which is intended.

65A preserves empty-field presence at its call site, leaving
`_accept_optional_source_list` unchanged for existing consumers.
`_AcceptedHostRow.empty_sources` is None for absent or dropped metadata;
`empty_reason = invalid_coverage` distinguishes a dropped claim. Reject a bad
list, a non-subset of consulted readers, overlap with declared partial readers,
or a mismatch between `hosts == {}` and every consulted reader being listed.
Drop the field, keep the row, and include its reason in local `coverage_invalid`.
Append presence, list and reason to `_sibling_tie_key` after counter semantics.
With absent/invalid metadata, empty hosts still prove every consulted reader
empty; nonempty hosts make no per-reader claim. Local JSON `empty_readers` is
therefore null or an allowlisted, consent-intersected list (possibly empty).
The existing `empty` boolean continues to mean `hosts == {}`.

**The acceptor re-checks BOTH writer contracts against `token_sources`, not
just the pair against each other.** `degraded_sources ∩ token_sources` must
be empty and `partial_sources ⊆ token_sources`; a violation drops that field
alone with `invalid_coverage` and keeps the row. Checking only
`degraded ∩ partial` is not enough — a peer that lists a contributor in
`degraded_sources` makes the card say a reader that plainly contributed
"failed on the latest push", and a `partial_sources` entry outside
`token_sources` claims a reader nobody consulted returned incomplete totals.
Both then route the user to `mm diag` for a reader that is fine, on a surface
whose whole value is that its remedies are true. Found by Greptile on PR #151.

**Git coverage.** `git_capture.since` / `walk_budget_aborts` / `walk_errors`
on `git-snapshot` rows (Track 30A fields, now also attached to the snapshot
row itself). Uncovered `[since, ts]` intervals are clamped to
`_coverage_floor_from_files`. The open interval after a device's latest
capture is not a gap (the next `mm push` covers it). `discovery` in
`DISCOVERY_HOLD` (`partial` / `empty`) does not paint coverage — the cursor
does not advance on those walks either. **But a `partial` capture is still an
OBSERVATION, and the gap loop keys on observations, never on coverage.** The
two maps are separate on purpose: keying on `covered` dropped a held-only
device from the card entirely (no note at all, indistinguishable from a
healthy machine) and clipped `latest_end` to the last *good* capture, hiding
every held push after it — exactly the window `mm recapture` is for. The
trailing clip itself still stands, and is why `latest_end` must come from the
observation map: a held push at T does prove the device reached T. Found by
Greptile on PR #151.

**`DISCOVERY_EMPTY` is the one HOLD value excluded from the observation map**,
and the asymmetry is the whole reason it has a name. `partial` is a LOSS
(budget exceeded, or a prober raised); `empty` is a FACT (a prober ran and
found zero git roots), so there is no history to have missed and `mm recapture`
has nothing to do. Counting it would nag every repo-less Mac on every retro
forever — the idle-Mac false gap in a new costume — and that machine already
gets the zero-repo push note, whose copy is the right one. Do not collapse the
two back into a bare `DISCOVERY_HOLD` test here; the cursor policy and the
gap-reporting policy agree on `partial` and disagree on `empty` on purpose.
A device with no `git_capture` is unknown, never a gap. `origin: recapture` and
`origin: init` rows COVER their interval and are EXCLUDED from the push tally
(`snap_total` / `snap_zero`) — opposite treatment of one field; an absent or
unknown origin still counts (see "Append boundaries and non-push origins"). `walk_budget_aborts` is a budget-exhaustion note, not a gap.
The budget-note remedy is `mm diag` → Git capture → `recorded.walk_budget_aborts`,
not a `last_push` key.

**Renderer.** Two host notes and two git notes live in a flat block after
the existing `_agent_coverage_notes` tree (host) and beside the zero-repo
note (git). They aggregate across machines. Remedy is `mm diag` on the
named machine inspecting `host_usage.<reader>` — never a bare `mm push`.
Absence of the field produces no upgrade nag.

**Two-tier version floor (64A amendment).** `HOST_SNAPSHOT_MIN_VERSION`
(`v0.12.32`) is the first host-snapshot schema release.
`ATTENDED_USAGE_MIN_VERSION` (`v0.14.17`) is the first release that refreshes
usage on every attended push. A converged push below that floor reports
success while doing no capture. Every fleet remedy that calls for a refresh
uses `_attended_usage_remedy`: upgrade the producing Mac to the attended floor,
verify `mm --version`, then run `mm push` and verify with `mm status`.
Tag-pinned pipx installs can remain old after `pipx upgrade`; README supplies
the force-install fallback. Both producer and renderer must upgrade for the
init-origin counting fix.

#### Acceptor and schema

- **The rejected breadcrumb counts DEVICES, not rows.** `aggregate_host_usage`
  applies no window filter to rejects (only accepted rows are compared against
  `until`), so one malformed writer 89 days ago would light a row-count
  breadcrumb on every 7d retro until retention reaped the file. Window-scoping
  the rejects themselves is impossible for a `naive_timestamp` reject, where the
  timestamp IS the malformed field.
- **Bound a peer-controlled map BEFORE copying it.** `_copy_day_bucket` checks
  `by_model`'s cardinality and validates each model id *before* allocating the
  copy, and `_accept_tokens_by_day` rejects on day COUNT before entering the
  day loop (the two day sets must match exactly and both have unique canonical
  keys, so unequal sizes can never reconcile — the check is exact and O(1)).
  Copying first and rejecting one line later does the sender's allocation for
  them. Only the row-wide distinct-model count stays in the caller, because it
  is the one bound that needs cross-day state. Found by Greptile on PR #150.
- **The sibling gets TWO reconciliations, and they are different predicates on
  purpose.** A day's four flat counters must EQUAL the sum of that day's family
  buckets across all families — `_add_usage` builds both views in one call, so
  any inequality is corruption or forgery. The nested `by_model` values must
  only be BOUNDED BY (`<=`) that day total. The asymmetry is not sloppiness:
  the writer caps `by_model` at `events.MAX_HOST_MODELS_PER_DAY` /
  `MAX_HOST_MODELS_PER_ROW` and leaves the day totals whole, so a capped
  machine's sibling legitimately under-attributes, and an equality check would
  drop exactly the rows the writer-side cap exists to make deliverable. `<=`
  is still the property that matters — without it a peer can attribute `2**53`
  tokens to one model on a day whose whole family total is 5, and Group 35
  would price it. Reconcile the running sum, not the final one.
- **The by-model cap runs AFTER the day-window trim, never before.** The host
  readers aggregate the WHOLE local corpus with no time bound, so a
  long-history machine carries models on days `max_days` is about to discard.
  Cap first and those models outrank the current day's under the row-wide cap
  and empty its `by_model` entirely — silently, and worst on exactly the
  machines whose per-model data is most worth having. `_copy_tokens_by_day`
  therefore only COPIES; `make_host_usage_snapshot` applies `_cap_by_model`
  after the `keep` filter. A per-day cap masks the naive repro (64 models on
  one day is trimmed to 32 before the row cap sees them), so the shape to test
  is N days each UNDER the per-day cap.
- **`day_total - sum(by_model)` is a RESIDUAL, not a bug.** It is usage the
  writer did not attribute to a shown model. A per-model consumer must carry
  it rather than treat the shown models as the whole day. Do not "fix" the
  gap by pruning day totals to match `by_model` — that would throw away the
  only complete number on the row.
- **The writer caps mirror the acceptor caps and cannot be shared.** `events`
  must not import the skills package, so `events.MAX_HOST_MODELS_PER_DAY` /
  `_PER_ROW` are a hand-copy of `aggregator.MAX_HOST_MODELS_PER_DAY` /
  `_PER_ROW`, pinned by
  `test_events.py::test_writer_model_caps_mirror_the_acceptor` — the same
  mirroring convention `aggregator.MAX_HOST_MODEL_ID_BYTES` already uses for
  `host_usage._MAX_MODEL_ID_BYTES`. The writer-side half exists so a truthful
  local machine can never emit a row its own fleet drops under a remedy string
  that tells the user to upgrade an already-current mm. Selection under a cap
  is by descending bucket total with the model id as an ASCENDING tie-break,
  so two machines reading one corpus emit the same row.
- **A cap that cannot fire is worse than no cap.** An explicit
  `MAX_HOST_MODEL_KEY_BYTES_PER_ROW = 16_384` shipped briefly and was removed:
  at 64 x 256 it was exactly the product of the count cap and the id-byte cap,
  so the count check always fired first and the byte check was unreachable
  while reading as protection. Re-add a byte budget only BELOW that product.
- **The detail quality rank sits STRICTLY below `tie_key`, and that placement
  is what keeps the card version-independent.** `_row_replaces` compares
  `as_of`, then `tie_key`, then `_detail_rank` (valid > absent > invalid).
  `_tie_break_key` projects `hosts` and `active_days` verbatim, so two rows can
  only reach the rank when their family totals are already byte-identical —
  the rank therefore decides which *sibling* survives and can never change a
  rendered `## Agents` number. Put it above `tie_key` and a v0.12.49
  Mac starts selecting a different winner than a v0.12.48 Mac from the same
  synced corpus, which is a cross-version rendering divergence in the product's
  headline claim of fleet accuracy. Do not "simplify" the ordering.
- **Selection must be TOTAL, and the rank is not the last word.** `tie_key`
  excludes the sibling and `_detail_rank` only grades present/absent/invalid,
  so two rows carrying different VALID siblings — or different REJECTION
  REASONS — compare equal all the way down and the winner falls out of
  file-iteration order. `_sibling_tie_key` closes both: it keys on
  `(detail_reason, tokens_by_day)`, because the reason is what the dump renders
  as the user's remedy, so order would otherwise decide whether a peer is told
  `invalid_counter` or `active_days_mismatch`.
- **The HOST acceptor reads `events.EVENTS_SCHEMA_VERSION`**, never a hardcoded
  `2` — both in `_accept_host_usage_snapshot`'s version check and in
  `_tie_break_key`'s normalized projection, so the two cannot disagree about the
  version one of them just validated. With a literal, the first bump would make
  mm reject its own freshly written rows fleet-wide and light the rejected
  breadcrumb everywhere at once.
  **Scoped to the host path on purpose.** `aggregate_sessions` still compares
  against its own local `V2_SCHEMA_VERSION = 2` literal, because the v=1 → v=2
  sessions transition has *semantics* attached (v=1 rows carry delta semantics
  and are deliberately counted as `pre_v2_peers` contributing zero, not merely
  "a different version"). Pointing it at the writer's constant would silently
  reclassify every fresh `sessions-snapshot` on the next bump. If sessions ever
  needs a v=3, that is a migration with its own compatibility decision, not a
  constant swap — do not "fix" the inconsistency by unifying them.
#### Isolation

- **Isolation, pinned by test.** Host snapshots remain separate from
  `sessions.tokens_by_model`, `PriorPeriod`, and `_aggregate_git_period_pair`.
  `aggregate_agent_usage` combines the two corpora only in render-only rows;
  host `claude-*` models join the Claude row. Every row intentionally shares
  `estimate_cost`, `_unpriced_token_summary`, and `token_usage.sum_bucket`.
  Peer validation and tolerant session parsing remain at their respective
  ingestion boundaries, before these shared helpers. Prior-period isolation
  is pinned by
  `test_host_tokens_do_not_reach_prior_period` (replaces the deleted
  `_retro_to_snapshot` pin).
- **No card trends row (Track 24B, closed).** The card remains
  width-constrained at 64 chars,
  and a down-arrow on an artifact you paste into iMessage is public
  self-flagellation. Do not add a trends line to the card.

#### Sum boundaries

The unified renderer sums in-window `lifetime_by_family[family][day]` buckets
across machines and reports duplicate-ledger evidence in `MM_HEALTH`. Day
counts use a set union; rendering any ratio against that inclusive UTC count
remains forbidden. Host per-model keys in `tokens_by_day` are never merged
into the source `sessions.tokens_by_model` aggregate. They join the matching
render-only agent row, including host `claude-*` models joining Claude session
usage. These rows do not become token trends or `PriorPeriod` fields.

#### Forensic dump

``mm retro-fleet --dump-host-usage`` is the forensic hatch. It prints
the inventory JSON and skips the markdown retro. As of v0.12.49 the dump
emits per-model ``tokens_by_day`` and a detail status per device. The card
stays family-only until the **unified-reporting** work lands (cite it by title, not by ID: it was numbered Group 36 when this was written, Group 43 until 2026-09-03, Group 50 until 2026-09-05, Group 55 / Track 55A until 2026-09-06, and Group 58 / Track 58A today; today's Group 36 is the shipped Three-hosts group, today's Group 50 is the shipped terminal-safe recovery warnings group, and today's Group 55 is the unrelated git-environment cleanup). Model ids are sanitized at DUMP time only
(``_safe_short``), never at accept — and because that truncates at 128 chars
while the acceptor admits 256 bytes, two distinct accepted ids can sanitize
to one key. `_sanitize_tokens_by_day` disambiguates with a `~N` suffix rather
than letting a dict-key collision silently drop a model from the one surface
whose job is showing what actually arrived. **Aliases are assigned once per
ROW, over the sorted raw ids** — never per day off insertion order, which
would let `a_b` mean `a/b` on Monday and `a?b` on Tuesday and read as per-day
movement that never happened.

**Its own deadline, started after `walk_done`.**
`HOST_USAGE_READ_BUDGET_AUTOPUSH_MS` (250) / `_INTERACTIVE_MS` (500) set the
sweep deadline. The first reader receives it unchanged; each later reader
receives `max(sweep_deadline, start_i + HOST_READER_GRACE_MS / 1000)`.
`HOST_READER_GRACE_MS = 50`: Track 57A's E16 probe measured warm Grok at
19–20 ms while Codex consumed 73–80% of the autopush budget. This is a
cooperative grace floor, not a hard elapsed-time ceiling or a full second
reader budget. Two halves remain load-bearing: capture begins
AFTER the `walk_done` snapshot (invariant 4) so host time can never trip or
redefine the session-walk notice, and the deadline is FRESH rather than the
walk's leftovers — reusing `deadline` would make the row vanish exactly on the
busy machines where it is most interesting. No UNATTENDED caller may fall
through to `host_usage.DEFAULT_READ_BUDGET_S` (5s), which is 20x an entire
autopush walk budget spent on optional analytics. Track 63A's attended warm
publishes its own result through the same reader failure boundary.
`resolve_host_read_budget` owns effective budgets and their `default`/`config`
source: `[retro] host_usage_autopush_budget_ms` defaults to 250 (100–5000),
`host_usage_interactive_budget_ms` to 500 (250–5000). Both reject bools,
non-integers and out-of-range values; effective autopush cannot exceed
interactive. Capture defaults, tail, init and attended push all use this
resolver. The independent 5-second warm and 50 ms later-reader grace remain
constants. Invalid config still stops ordinary sync.

**Same-family day collisions still sum.** Historically Codex and OpenCode
classified GPT models into the same canonical `codex` family, so two readers
could return the same `(family, UTC day)` bucket. After 36A no two live
readers collide in production, but the merge still sums
`token_usage.merge_usage_bucket` at every level — a shallow update would drop
whichever reader landed first, and a synthetic or future reader that shares
a family would regress without this. The same rule applies one level down
on `(day, model)` in `tokens_by_day`. `_merge_warm_retry_capture` fuses both
maps, because a sibling that skipped the warm retry would diverge from
`hosts` on the largest-corpus machines and then be dropped by reconciliation.

**A reader exception is contained in `_capture_host_usage`, not at the outer
guard.** The tail's `try/except` would also discard the git and session rows
already captured and the terminal `mm-push` with them, so an unreadable host
store would cost the retro its real content AND rewind the cursor into a
30-day re-walk on every subsequent push. Reader exceptions normalize to
`unavailable` on that reader and the sweep continues.

**The notice text is a closed vocabulary.** `_host_skip_phrase` names only the
reader and the reason class — never a path, transcript, SQL, model id, or
exception string. Reasons outside `host_usage.Reason` normalize to
`unavailable`. `unsupported` says the reader wrote a record this mm cannot
read; a newer mm **may** read it (`pipx upgrade mind-meld`), or the user can
`mm disable-source <reader>`. A retry alone is not a remedy. `partial` takes
the generic retry sentence naming `mm push` and `mm diag`.
`deadline` states that this push/init's read did not finish inside its budget.
Background failures name attended `mm push` warming; an attended caller whose
warm allowance also expired gets that fact and an `mm diag` diagnostic. Status and diag
instead include the last allotted budget, last complete read and age, and
cached/on-disk counts (unknown when unavailable). Their conditional remedy
raises `[retro] host_usage_autopush_budget_ms` if the last complete read is
over budget, otherwise requests a capture. Every deadline remedy links to
`HOST_USAGE_CAPTURE_URL`; diag never sends the user back to `mm diag`. A
deadline is not evidence that a cache is still warming. Attended push replaces the old
`mm recapture 1d` bridge on a converged Mac; it requires enabled, resolved
mm-events and reader consent, but no Git roots. The phrase and the joined breadcrumb
are prose, not a semicolon-delimited schema: the approved retry sentence
contains `; ` too. Nothing parses the detail by that separator. One
degradation is appended per dropped reader.

### Standing read blockers (v0.14.8)

`last_reason` is the **standing read blocker**: the reason of the most recent
read that did not complete inside its budget, retained until an in-budget
completion, with a permanent reason surviving later incomplete transient failures. It is neither the latest attempt
nor the latest publication outcome. `host_usage.PERMANENT_REASONS` owns
`{"unsupported"}`; `events_tail._HOST_PERMANENT_REASONS` derives from it.
Do not widen the set to `malformed` without evidence and a policy decision.
`PERSISTABLE_REASONS` is `Reason` minus `locked` and `no_metadata_ledger`:
contention is not evidence about the store, and no ledger is source absence.

Both roots carry `version`, `files`, `last_reason`, `last_reason_since`.
Grok additionally carries `complete_once` and `usage_less_skipped`. The shared
`_carry_reason(prior_reason, prior_since, result, now, *, over_budget=False)`
returns the new pair. A complete in-budget read clears both fields. A complete
but over-budget read records `deadline`, retaining `since` only if the prior
reason was already `deadline` with a valid date; this completion proves a
prior permanent blocker is gone. An unchanged permanent blocker
keeps its original valid date across transient failures; a changed reason
gets a fresh date. The date is this version's **first observation of the
current reason**, not the outage start. A migrated or corrupt date is unknown
until the next failing read dates it once. Never infer an earlier onset.

`_cached_last_reason` accepts only the persistable vocabulary.
`_cached_reason_since` accepts a bounded, timezone-aware ISO timestamp and
returns normalized UTC ISO text. Invalid dates never erase valid reasons;
without a valid reason, the date is always None. A version mismatch makes
root metadata absent, like `_cached_files`. **Absence is documentary**:
pre-v0.14.8 Codex roots lack `last_reason`, and both older roots lack `since`,
but absent and null mean the same thing to readers. No `CACHE_VERSION` bump
and no per-file re-walk is warranted.

Failed passes skip the unconditional locked-json write via `_NoCacheCommit`
only when no file was learned AND the validated `(reason, since)` pair AND
the persisted timing evidence are unchanged. Comparing reason alone would
leave migrated blockers undated forever, and comparing the pair alone would
keep a stale `last_deadline_allotted_ms` after the operator raised the
budget. Newly learned files still commit despite an unchanged sticky reason.
Prior metadata is read immediately after acquiring the lock, **before** the
post-lock deadline check. Expiry there skips scanning and carries `deadline`
through the same helper: one write on transition, no repeated rewrites.
Pre-lock expiry creates no cache. A completed-but-overbudget scan keeps its
file cache (including deletion pruning), records `deadline` as its blocker,
and records the last complete read before returning `deadline` to the caller.
Serialization occurs after the final deadline check, so the read budget is
not an end-to-end ceiling. Healthy passes still rewrite the cache (the
pre-existing locked-json default), now with compact JSON in both host caches.
Only these callers opt into `locked_json_rmw(compact=True)`; other caches
retain indented JSON. Both encodings remain readable without migration.

**Track 66A probe (2026-09-21, HEAD a975feb, device 889e42c0).** The real
`_capture_host_usage` → both readers ran against the real corpus (841 Codex
rollouts, 213 Grok ledgers), using copies of both caches; the originals were
unchanged. At a 43 ms budget, reader 0 produced 40 consecutive incomplete
`deadline` passes: after the first, **0 cache writes in 39 steady passes**,
with allowance fixed at 43 ms. Over 2,000 entry-latency samples, p50 was
0.5 µs, p99 0.67 µs, max 8.17 µs; none reached the ≥500 µs needed to flip
integer-ms rounding. The claim that entry jitter prevents convergence was
not reproduced. Complete passes wrote 9/9 after the first, as intended:
completed scans prune deleted files and record `last_complete_*`, including
complete-but-late scans. No reader or cache-write code changed for 66A.
Evidence: `~/.gstack/projects/kbitz-mind-meld/66a-probes/probe_failed_write.py`
and `probe-889e42c0-a975feb.json`; the measurements above are retained here
so the conclusion survives workspace and machine changes.

**Optional cache timing fields (Track 63A, no CACHE_VERSION bump).**
`last_complete_ms` is the reader-entry-to-result-ready time, before cache
serialization; `last_complete_at` is UTC ISO seconds. Every completed scan
writes both, even if late; partial writes carry validated prior values.
`last_deadline_allotted_ms` records `round((deadline - reader_entry) * 1000)`
when a written root carries `deadline` from this pass, carries while that
reason remains, and is removed when the reason clears. Millisecond fields
accept only ints in 0–86,400,000. Date validation matches `last_reason_since`;
invalid or absent values are unknown, never inferred from counts.
A later reader's allowance is the sweep time remaining after earlier readers,
floored at `HOST_READER_GRACE_MS` (50 ms); it varies with earlier read times,
not merely entry-latency rounding. That variation could defeat the exact
write-skip comparison if the later reader remains `deadline`-incomplete with
no newly learned file and an unchanged blocker pair. No Mac has shown that
case: Grok warm reads measured about 20 ms (Track 57A E16), below the 50 ms
floor. `host_read_budgets` describes configured sweep budgets, not each
reader's allowance. Complete passes still rewrite by design.
Diag exposes them for both readers and a top-level `host_read_budgets` map.
Its sweep sums the last complete reads of `_default_host_readers` for the
resolved, consented source set only when config state is `ok`. This same set
already supplies `host_publication.readers`; only its keys are used, never
coverage values. It excludes writes and grace and cannot prove publication.

**Write failure is a separate observation.** `_write_json` returns an
`OSError` in `locked.write_error`; it does not raise into the reader's
handler. Both readers inspect it after the block, print one
`mm: notice: host token cache write failed: <ErrnoName>` without a path, and
return the scan result unchanged. Truncate-before-write can lose the prior
root: the next diag says `missing` (empty) or `unreadable` (partial JSON),
with no blocker line. This is existing forensic-cache behavior; the next
failing read can record the blocker again. Only failures before writing,
such as lock acquisition errors, preserve the old cache. The write failure
itself is a notice, not a persisted reason or a changed scan result.

Status reports any standing reason for both readers; the Codex block requires
its source enabled. Blocker, rebuilding, and warming are a strict `elif`
chain, so a blocker never shares the status line with a count-based retry.
Healthy Codex stays silent. Diag labels inventory and blocker separately:
`<reader> cache inventory:` and `<reader> usage read blocker:`; readable
healthy caches say `none`, and a valid date renders
`(first observed YYYY-MM-DD UTC)`. Inventory `ready` plus blocker `none`
still does not prove totals were published. Diag reads cache metadata without
opening host logs or requiring a passphrase; Codex counts rollout paths and
Grok counts two-level `*/*/updates.jsonl` paths under `grok_sessions_root()`
via `os.scandir` (not `Path.glob`, which swallows scan errors on Python 3.13).
`grok_usage_diag.files_cached` counts entries only in a valid current-version
cache with a `files` map; missing, malformed, version-mismatched or locked
caches return None, never a synthetic zero. A missing sessions root is 0;
`files_on_disk` is None on any scan `OSError`. The text surface is `grok ledgers cached: N of M`, with `unknown`
for either missing count. Grok requests `locked_json_snapshot(blocking=False)`
so contention reports unknown immediately; other shared-snapshot callers
retain the blocking default. No ad-hoc flock path is added to the reader.

Orchestration failures (a reader exception normalized to `unavailable`, or a
sweep deadline before invocation) remain per-push stderr/breadcrumb signals.
A no-op autopush may overwrite that breadcrumb with `success` without
reading either host; a persisted blocker remains visible in status and diag.
Attended push refreshes consented host usage even with no user-content changes;
no-op autopush still performs no host reads. See README "Host usage capture (Codex and
Grok)" for remedies and the three existing deferred-work TODOs.

**`degraded_sources` is additive.** No `EVENTS_SCHEMA_VERSION` bump:
`_accept_host_usage_snapshot` does no key-set check and `_tie_break_key`
excludes unknown top-level fields. The field carries reader names only.

**Tail appends a degradation; init prints only.** Same rule as every other
tail degradation: `_run_events_tail` writes the `mm: notice:` AND appends the
phrase to its returned list, because an unattended hook's stderr reaches
nobody and `mm status` is the only surface the user reads. It is NOT
rate-limited — it describes the CURRENT state, and no-op pushes never reach
the tail at all, so a stale `success` would be the misleading outcome.
`_run_events_backfill` has no `mm-push` row and no autorun breadcrumb, so it
emits the notice alone.

**Row order.** Tail: git rows, sessions row, optional host row, `mm-push`
LAST (CT-4 unchanged). Backfill: git, sessions, optional host, never `mm-push`.
Attended push captures one host row before building its local manifest, then
suppresses host capture in the activity tail even if that first attempt failed.
`_capture_host_snapshot` owns the single bounded sweep and warm read shared by
attended push, tail and backfill; no duplicated warm path.

**Attended push refreshes usage; previews never capture (64A amendment).**
A real attended `mm push` refreshes consented readers even when user content is
already in sync. `_push_core(attended=True)` resolves sources once under the
mm lock, recovers the prior Grok source, then captures before `build_manifest_v2`.
The pure prerequisite check uses the final sources and keeps unavailable custom
mm-events roots distinct from disabled ones. Prerequisites unmet means skip and
continue ordinary content sync. An unreadable config or failed source scan is
still a sync failure; optional capture cannot override it. There is no opt-out
flag, alias or config key. Status, diag and push preview never scan or warm host
usage, append events, fetch an upgrade or write nudge state.

This absorbs T3-B's shared host-only capture, consent, dry-run, unresolved-writer
and autopush requirements. It deliberately **publishes** the row, whereas T3-B
proposed a blocker-only cache probe with no publication. The root-only blocker
probe is unnecessary: every ready attended push captures, not just blocked ones.
`/roadmap` must reconcile T3-B later; this Track does not edit its files.

Budgets are cooperative, not hard timeouts. Content sync follows optional
capture. Allow about five seconds of scanning per cold reader, and both readers
can be cold. Lowering `host_usage_interactive_budget_ms` does not cap the separate
warm allowance. Autopush is still change-gated, never warms, and retains its own
quiet error/lock behavior; it is not an equivalent attended no-capture command.

**Publication and content acceptance are independent.** `_capture_attended_usage`
contains exceptions from the whole optional phase, including notices, warming,
snapshot construction, serialization and strict append. A no-row, append failure
or unexpected capture error warns on stderr and continues content sync. The
activity tail cannot retry that failed reader. A successful append retains its
exact day path and row. At `backend.put(manifest_key, ...)`, `PushResult.content_accepted`
is recorded before any later observation/maintenance. `_report_usage_publication`
sets `host_usage_published` only if that row belongs to the accepted file revision.
The former `on_manifest_accepted` callback is gone.

Exit 0 means content sync succeeded regardless of capture outcome; exit 1 means
sync or required maintenance stopped; exit 2 means invalid arguments. Push has
no exit 4. Recapture retains exit 4 for partial Git recovery. A captured row
excluded from a no-op push is warned as unpublished, with content already up to
date. Exclusions and include_dirs are never overridden. Successful reader output
and publication receipts are verbose-only; non-contributing readers share one
stderr summary using `_host_skip_phrase`, and non-publication is always stderr.

**Content gate (64A amendment).** `host_row_appended` and `capture_activity` are
separate facts. When no host row was appended, the existing activity tail stays
enabled for every substantive push, including mm-events-only recovery, internal
selection and internal mtime changes on a no-reader Mac. No capture means no
usage-only mode line. When a row was appended, `capture_activity=content_changed`.
`content_changed` comprises non-internal byte diffs from one materialization of
`iter_source_diffs`, forward-only per-source mtime drift, and the symmetric
source-name selection difference minus `MM_INTERNAL_SOURCE_NAMES`. Removed user
sources count. Mtime is independent of `has_mtime_only`, since the host row itself
makes the substantive gate true. A missing/corrupt real manifest makes local
user files new. Upload diffs are recomputed after the activity rescan.

Known limit: a usage-only refresh republishes the complete local manifest, so a
user file whose mtime drifted BACKWARD with identical bytes (which the bare-push
gate ignores) is published at the older mtime, as any content-changing push
already does. A peer holding divergent bytes with an in-between mtime then treats
its own copy as newer instead of recording a conflict. Restoring identical bytes
with an older mtime is rare; carrying the advertised mtime forward would make the
manifest stop reflecting local truth, so it is not done.

A usage-only refresh skips cursor lookup, Git/session walks, identity gathering,
activity rows and token-cache locks; retro push counts and cursor remain unchanged.
It has no activity degradation and writes no autorun breadcrumb. The usage-only
mode line names the unchanged cursor and `mm recapture 30d`; content-changing
pushes use their ordinary summary. `PushResult.content_files` counts user byte
changes; mtime/selection changes can have zero.

The v0.12.2 **count/cursor** guarantee survives; its no-upload behavior changes:
every appended host row re-encrypts/uploads the day file, refreshes last-seen and
rewrites the sidecar, even with converged user content. Recovery is per invocation:
a later attended push with another appended host row remains usage-only;
autopush recovery still captures ordinary activity and counts once.

**Maintenance and GC cadence.** Auto-GC is gated on `result.content_changed`,
not total modified files. Usage-only obsolete blobs await the next user-content
push or explicit `mm gc`; no fleet manifest decrypt/blob sweep per refresh.
Post-acceptance device/cleanup failures warn without revoking content acceptance,
including failed/skipped captures. GC's corrupt-manifest refusal remains visible
and aborts unless a host row was proven published; only that case can say host
usage was published while GC stopped. Failed attended attempts still nudge once
after releasing the lock; dry-run never nudges.

**Capture failures and remedies.** Always-stderr `mm: warning:` lines carry
`prerequisites`, `readers` (a row was still written), `no-row`, `capture-failed`,
`append-failed`, `max-file-size`, `not-published: <cause>`, or
`unverified: <reason>`; `push-failed` is
the content-sync stop, an `Error:` line with exit 1. A permanent format failure calls for
upgrade; a background deadline can use attended warming, while an exhausted
attended warm allowance needs an honest diagnostic, not a promise to bypass its
own budget. Include/exclude failures name the setting to change. Non-publication
requires proof: config exclusion wins before reading; an absent file in the
accepted manifest is `file-absent`; matching accepted bytes without the row are
`row-missing`. Only that last case calls for JSONL repair with a copy preserved
outside mm-events. The retired `unreadable-row` token must not return.
Missing/unreadable/oversized/changed reads, revision mismatch after acceptance,
and an evidence exception are **unverified**, never proven non-publication.
The literal remedies in `cli._publication_remedy` and the evidence-error warning
point at read-only `mm status`, not another push to verify old bytes. Missing
files additionally explain that the next attended push creates a new row;
oversized lines name the 16 MiB boundary and do not suggest repairing accepted
bytes. `PushResult.host_usage_published` stays False when unverified.
Inspection says automatic refresh is available,
not a daily imperative for an action push already performs.

Capture cadence and publication cadence are separable; the product contract
binds publication to push because manifest acceptance proves local storage acceptance,
not delivery to another Mac. A separate
`mm recapture --usage` was considered and declined; recapture stays Git recovery.
The review measured pushes on 15 of 33 device-days, so cadence limits granularity.
The measured historical miscount was one row in 94; zero observed init-origin
rows. These fixes close a class before it scales, not a claimed larger incident.

**Readiness and coherent remedies (64A).** `config.usage_capture_readiness` is
pure over already-resolved `selected`, `available`, and reader consent. Its five
verdicts are ready, disabled/not configured, unavailable custom root, no-reader,
and unknown (selection is `None` because config was unreadable). Inspection
resolves once with `strict=False, bootstrap=False`; a missing bootstrapable
default root is available, and neither status nor diag creates it. Push keeps its existing strict bootstrap policy under the mm lock. `usage_capture_remedy` supplies the
prerequisite action. `_host_skip_phrase` requires an explicit readiness keyword;
tail/init pass ready because they resolved the writer and readers already.
Status/diag combine that verdict with current reader consent and permanent
blockers, so an old cache cannot grant consent, a missing folder is not called
disabled, and a screen cannot recommend both upgrade and retry. Unknown config
recommends neither enable-source nor refresh. `host_publication.readiness`
exposes the same verdict without publishing config or creating a receipt.

**Append size and boundaries (64A).** `write_push_event(max_file_size=...)`
serializes the whole batch and delegates the exact byte ceiling (including any
separator) to `flock_append_jsonl(max_bytes=...)` under its existing flock. A
size skip returns `EventAppendSkipped`, distinct from a successful strict Path
or a forensic/empty None. All four emitters pass the configured ceiling. The
tail also records the skip as a degradation; recapture refuses rather than
claiming skipped Git rows were written. Neither host nor activity batches may
create a new over-limit file. A file **already** over the limit (lowered config
or an external writer) still triggers `snapshot_refusal` if previously advertised;
shrink it outside the source, raise the limit or intentionally exclude it.
This guard is not a repair for existing oversize files.

65A enforces the row-to-file day contract: the filename day is the greater of
the current UTC date and every aware batch-row timestamp's UTC date. A clock
rollback larger than the diagnostic margin cannot put a row in a file dated
before that row. Pre-65A rows rely on capture and append sharing the same clock;
the diagnostic margin absorbs ordinary clock skew.

**Append boundaries and non-push origins (64A).** Under its existing flock,
`flock_append_jsonl` reads the last byte through an O_RDWR descriptor and prepends
a newline if necessary. This preserves an intact unterminated row and prevents
a torn suffix swallowing the next valid row. It does not reconstruct damaged
JSON, validate the manifest's source files, or promise successful rollback.
The earlier claim that `_restore_prefix` guarantees a transaction is retracted:
it catches and discards `ftruncate` OSError, so a short append plus failed rollback
can still leave a torn row that a later content push publishes as opaque bytes.
`write_push_event` rejects a git-snapshot batch without an mm-push unless each
Git row carries an origin; host-only batches are valid. Init passes
`GIT_SNAPSHOT_ORIGIN_INIT` through `_capture_event_snapshots(origin=...)`.
The aggregator excludes init and recapture from both zero-repository counters,
while retaining their commits/coverage. Absent or unknown origins still count
(pre-30A and forward fail-open); no schema version bump.

Both the **producing and rendering Macs must upgrade** for the init correction.
Older renderers count the new init origin. Historical unmarked init/refresh
rows remain indistinguishable from legacy pushes until they leave the queried
window or age past the 90-day retention. `mm recapture` cannot relabel them;
published history is not speculatively rewritten.

**When to stop reading host logs (TASTE-1).** Retire the affected host-log reader
when either a second consecutive Grok or Codex release breaks its record format
in a way requiring a census re-pin, or that vendor ships a stable machine-readable
usage command covering per-model token counts for the retained window. On either
trigger, delete the reader rather than repair it and defer to vendor reporting.
Track 36A's OpenCode reader deletion is the precedent. One format break is normal
maintenance; slow reads alone are not a trigger (the configurable budgets address
them). This is the stopping criterion for a subsystem that has consumed roughly
18 Tracks.

**Per-reader outcome labels (65A).** `_capture_attended_usage` reports dropped
readers via `_host_skip_phrase(attended=True)`, then partial/absent readers in one
stderr summary. Contributed and completed-empty labels are verbose-only.
`HostUsageCapture.usage_days` replaces the lifetime `empty` field. A reader can
contribute under `other` (for example an unrecognized Codex model id), so reader
names must never be inferred from `host_family()` membership. `host_reader_outcomes`
projects the built or accepted row once for verbose push, the local attempt record,
and status/diag. A trimmed-away partial day cannot keep a reader labelled partial.
`empty` renders as `completed, no usage in snapshot`, replacing the coverage word.
Inspection JSON preserves its existing reader vocabulary; `empty_readers` carries
the separate claim. All displayed names intersect the live allowlist and consent
and go through `safe_str`.

**Local capture evidence (61A).** Status and diag share one filename-scoped,
bounded binary day-file pass for mm-push and host rows. Status reuses its stable
file hashes for the diagnostic manifest only (see sync.md), so it does not open
the same day file again. The cache uses `(st_dev, st_ino)`, verified against
device/inode/size/mtime/ctime; it never resolves paths. The cursor keeps its digest.
Names use
`_safe_device_filename`; row `device` never selects a local file. Cursor
fail-open physical ordering remains unchanged. The CLI injects the aggregator's
actual host acceptance/order selector into events, so events never imports the
skills package. Invalid timestamps, future rows and sibling ties agree with the
fleet consumer. The CLI also injects `_HOST_FUTURE_SKEW` as a per-type day margin.
After either an exists-probe or read error, a selector-ordered type is uncertain
when it has no winner, won in the failing file, or the failing day is on/after
the UTC date of winner `ts` minus the margin. Read `ts` from the projected row,
never the opaque selector key. Margin underflow saturates: every day may matter.
`mm-push` retains filename/line order; an irrelevant old failure cannot poison
current host evidence for 90 days. Host scans that opt into a day margin, and
the push cursor, also open the next UTC day. The writer may name a batch from
a row timestamp one day ahead of a rolled-back clock; the terminal `mm-push`
in that file still advances the cursor when its own timestamp is not after
`now`. A rollback of more than one UTC day can hide the file until the clock
catches up.
`host_publication` contains timestamp/age, allowlisted reader coverage and
publication/attempt states; never hosts, model ids, tokens or peer ids.
The accepted-manifest sidecar proves publication only for matching file bytes.
`recorded_row_revision` returns frozen `(found, digest, reason)` evidence from one
fully exhausted parser pass and compares the requested row after a JSON round-trip.
The parser distinguishes missing before open, unreadable, oversized-line and
changed (including a missing post-read stat). `EventScan.files` stays a 2-tuple;
reasons live in `file_reasons`, and projection passes explicit receipt arguments.
A winning file with a known missing revision renders `unverified (<reason>)`;
other gaps remain unknown. No row means no capture in the retained 90-day window,
not never published. Do not infer an attempt from same-day capture age.
Complete empty scans have a reader in token_sources and hosts:{}; token_sources:[]
means all consulted readers were absent. Status reminds after one day or on
absent/degraded/partial coverage. Fleet notes aggregate snapshots predating the
retro window into one staleness class. Latest-per-device supersession is unchanged.

**Local attended attempt record (65A).** The leaf `attemptlog.py` owns
`sidecar.SIDECAR_DIR / last-attended-capture.json`, resolved at call time, local
only and mode 0600. Every real attended push reaching capture installs a typed
outcome holder before invoking it. All four CLI capture gates test `appended`,
not the existence of that outcome: no-row/prerequisite outcomes must preserve
the ordinary activity walk and cursor advance. The holder records capture-start
UTC `attempted_at`, `row_ts` if appended, separate class/cause, and allowlisted
reader outcomes. Computed publication verdicts win over `push-failed`; the latter
applies only to a core failure before manifest acceptance. Post-acceptance GC
retains the verdict; evidence exceptions set `unverified: evidence-error` before
printing so stderr and the record agree.

Push writes in its finally, before releasing the mm lock, using mkdir then
`fsutil.atomic_write_bytes(fsync=True, mode=0o600)`. A separate exception guard and
outer finally always release the lock. Pre-rename failure preserves the old file;
directory-fsync failure can follow a successful rename. The notice therefore says
"could not durably record this attended capture attempt …; check mm status" and
does not promise which record survived. Record failure does not change push exit.
Dry-run/autopush never write it. Status/diag perform one bounded plain read with
no lock, creation, normalization or permission changes. Validate aware timestamps
and closed class/cause/reader vocabularies. Missing, unreadable and corrupt files
remain distinct unknown states; there is no busy state.
Missing-record advice preserves the existing readiness guards: append "run mm push"
only when ready and no consented reader requires an upgrade. Present records show UTC
timestamp/age (including `in the future`) and non-contributing reader outcomes.
For a non-published attempt only, a capture newer than both `attempted_at` and
`row_ts` adds "a later capture has since been recorded". JSON exposes that as
`latest_attempt_superseded` beside the class/cause/time/readers/read-reason fields.

**Reader tolerance: an ordinary Codex shape must never refuse the store.**
One unreadable rollout fails the WHOLE `_scan_codex_root`, and all-or-nothing
then publishes nothing — so the blast radius of a too-strict reader is the
entire feature, on every machine, forever. Measured on a live 452-rollout
Mac before the fix: **167 rollouts (37%) failed** and `read_codex_usage`
returned `unsupported` in 5 ms, having died on the first file. Two shapes are
therefore skipped rather than refused:

- a `token_count` whose `payload.info` is null or absent — Codex's
  start-of-turn marker, present in 33% of rollouts, carrying no ledger;
- a ledger that precedes the first `turn_context` (no model yet). Live sessions
  open this way. **This is now a BUFFER, not a skip** (Track 32A): the records
  are held and attributed to the first model the file names. The old rationale
  ("totals are cumulative, so a later attributable record restates it") died
  with the cumulative reading — under per-turn accounting a dropped prefix is
  gone, measured at 1,557 records across 7 rollouts worth 209,515,399 input
  tokens. A file whose ledgers are ALL pre-context still refuses; the buffer
  must never rescue that case.

The discriminator is empty-marker vs broken-ledger, and it is load-bearing in
BOTH directions. A ledger that was seen and could not be attributed to any
model still refuses (`saw_usage_ledger`), because dropping it would
under-report real usage — pinned by `test_missing_model_before_token_is_
incomplete`. A present-but-non-dict `info` still refuses. A rollout with no
ledger at all contributes nothing to the aggregate (see the `no_ledger` cache
entry below for how it is stored). Post-fix on the same corpus: 440 OK, 15
no-ledger, 0 failures, 6.4B tokens across 37 active days. Pinned by
`test_host_usage.py::TestOrdinaryCodexShapesAreNotRefused`.

**Cache persistence is DECOUPLED from result validity.** "May this scan be
published?" and "did we learn something durable about individual files?" are
different questions, and conflating them made a large corpus unable to
bootstrap: `_scan_codex_root` discarded its staged entries on any abort, so
every bounded scan re-parsed the same prefix, expired in the same place, and
threw it away — measured as six consecutive scans and **zero bytes cached** on
a 452-rollout Mac. `read_codex_usage` now commits what it staged even when the
result is incomplete. This is sound because each entry is a complete,
fingerprinted parse of ONE stable file, revalidated against dev/ino/size/mtime
plus a head+tail digest before it is ever trusted; partial-across-the-scan is
not partial-within-a-file.

Two rules make it safe, and the older `test_2am_regression_*` pin still holds
for both. A COMPLETE pass replaces the map — that is what prunes entries for
deleted rollouts. A PARTIAL pass must MERGE (`{**cached, **staged}`): replacing
would delete every entry it never reached and the cache would thrash between
prefixes instead of converging, and pruning on a directory listing it never
finished would delete entries for files that were never absent. Entries for
files deleted during a run of partial passes linger until the next complete
pass; they are inert, because aggregation walks the DISK, never the cache. A
failed scan that learned no file and kept the same blocker/date pair escapes
via `_NoCacheCommit` rather than rewriting the file (see standing read blockers above). Measured after the fix: an autopush-only machine converges
in **3 pushes** (264 → 361 → 440 files cached) with no interactive command.

**A day bucket IS a day's recorded work, since Track 32A.** It was not before:
the readers used to report a cumulative total per session file and attribute the
whole thing to the UTC day of that file's LAST record, so a bucket meant
"lifetime totals of every session that last touched this machine on that day",
63 of 440 rollouts landed on a day they did not start, and one day carried 3.4B
tokens because 91 sessions collapsed onto it. Every reader is now per-turn, so
buckets are additive and a fixed day's value no longer decreases between
snapshots. The cross-machine caution below still applies and is not affected by
this change. The only safe consumption is latest-row-per-device as a
point-in-time view; diffing, summing, or charting `active_days` as a time
series all produce wrong numbers. Track 20A locks this contract above, before
Track 21 adds the first consumer.

**The payload is capped at `MAX_BY_DAY_DAYS`, because the readers are not.**
`_iter_rollouts` has no `since` and the Grok walker has no date predicate,
so `hosts` is the machine's LIFETIME activity unless the writer bounds it —
and it would be re-serialized into a synced, content-addressed day file on
every substantive push, growing linearly with calendar time forever. Every
sibling is already bounded (git rows by `since`, `tokens_by_day` by the same
`MAX_BY_DAY_DAYS`, day files by `EVENTS_RETENTION_DAYS`); this row escaped all
three. The cap is applied to the UNION of days, not per family — a per-family
cap would give each host a different window and make cross-host day totals
incomparable — and `active_days` is derived AFTER it, so it can never
advertise a day the payload dropped.

**A ledger-less rollout is cached as identity-only, not skipped.** Caching
nothing for it looks harmless and is not: those files are then re-parsed on
EVERY scan forever, so a corpus whose ledger-less files alone outcost the
250ms budget can never reach a complete pass — the cache is fully warm and the
scan still expires, permanently. The `no_ledger` entry carries identity and
fingerprint ONLY; `_aggregate` skips it (a synthetic model would bucket `""`
into the `other` family) and `_validated_entry` strips any day/model/usage
riding along behind the flag. `_resumable_entry` refuses it too: resuming past
its offset would meet the file's first ledger with no remembered
`turn_context`, and `_read_rollout` would refuse that as unattributable —
turning a file that just gained its first response into a whole-store refusal.
Pinned by `test_uncacheable_rollouts_do_not_block_convergence`.

**The warm is gated on a FAILED bounded attempt AND on the failing reader.**
`warm_host_cache_inline(reader=...)` reads names in `WARMABLE_HOST_READERS`
(today Codex and Grok). Only `deadline` qualifies, including a reader whose
first invocation was prevented by sweep expiry. A healthy empty scan never
warms. Autopush supplies no warm callback and continues to converge through
partial commits.

`warm_host_cache` is now the attended-notice hook only. After
`mm: reading <reader> usage beyond the push budget (about 5 s of scanning)...`,
`_capture_host_snapshot` wraps `warm_host_cache_inline(reader=name)` in a
singleton `_capture_host_usage` call with an explicit 5-second deadline.
Its result merges through `_merge_warm_retry_capture(retried_names={name})`.
There is no second bounded read: a completed warm contributes its totals,
empty/partial state is preserved, `unsupported` replaces the first deadline,
exceptions become `unavailable`, and `no_metadata_ledger` silently omits that
reader. Completed first-pass siblings are never re-read or erased.

An attended capture is a one-time refresh. If all readers later fail, no host
row is written and the previous snapshot ages. A later row where another
reader completes supersedes device coverage and may omit the failed reader.
The budget lever is the lasting remedy for a warm autopush read that is too
slow. Scanning is cooperative; an in-flight filesystem call, one full
reduction, or subsequent cache serialization can exceed the allowance.

**Track 63A qualification, 2026-09-17 16:20 UTC, device `3a6c7dc9`.**
Scratch wheel built from the implementation branch using the pipx interpreter;
never installed into the live pipx environment. `sys.version`:
`3.14.7 (main, Aug  5 2026, 10:29:49) [Clang 21.0.0 (clang-2100.1.1.101)]`.
Probes: `~/.gstack/projects/kbitz-mind-meld/63a-probes/` (`mm_codex_budget_probe.py`,
`mm_codex_phase_split.py`, `mm_codex_constant_factor_proto.py`). Full logs and
the extra pre-serialization/cold/GC/differential harness are under
`~/scratch/track-63a/`. Live logs were read-only; every cache write went to scratch.

The baseline (`70abdee`, v0.14.15) missed all three warm 250 ms attempts
(365/377/380 ms end-to-end). The branch completed all three
(194/200/200 ms end-to-end). The phase probe's comparable second warm pass:

| Phase | Baseline ms | Track 63A ms |
|---|---:|---:|
| Cache load | 33.6 | 21.1 |
| Listing | 4.8 | 4.8 |
| Cache key | 11.8 | 9.1 |
| Entry validation | 106.1 | 32.8 |
| Fingerprint | 32.9 | 23.6 |
| Regular-file stat | 2.7 | 4.1 |
| Reduction | 170.1 | 70.1 |
| Cache serialization/write (outside read budget) | 28.2 | 24.8 |
| Total before serialization (approximately, rounded probe total minus write) | 369.8 | 173.2 |

The phase probe retains historical label strings (`2x open`, `indent=2`);
Track 63A actually uses one fingerprint descriptor and compact writes. The
supplied probe's first pass starts from a live-cache copy missing 13 of 1,066
rollouts: 235 ms total, about 211.9 ms before serialization. The uninstrumented
budget probe's first cold-ish pass took 509 ms, still complete within its 5 s
allowance. Filesystem coldness is not controlled by either probe.

A separate pre-write-boundary measurement on **1,066 rollouts / 84,910 states**
recorded three warm 250 ms attempts at **168.26 / 166.66 / 166.90 ms before
serialization**, all complete (196.13 / 194.52 / 194.65 ms end-to-end).
One empty-cache parse was complete at **2,949.68 ms before serialization**
(2,977.14 ms total). The resulting compact cache was 4,040,684 bytes; the
original 1,053-entry cache was 15,870,126 bytes indented / 4,027,507 compact.

The frozen-oracle live-corpus differential was **byte-identical in ordered
JSON**, both validated entries and both aggregate maps plus unattributable
days. With cyclic GC paused in both versions, validation was 68.82 → 32.60 ms
and reduction 139.81 → 70.09 ms. The unit harness also runs 200 seeded corpora,
explicit fork/resume/re-emission/zero/malformed/Grok fixtures, and tampering.
`tests/_host_usage_oracle.py` is copied verbatim from the shipped contract;
when that contract intentionally changes, delete and re-freeze it from the
new release. Never import the implementation into the oracle.

The reader's GC pause measured **242.42 / 236.62 / 231.13 ms enabled** versus
**165.67 / 165.85 / 166.27 ms paused**, before serialization (median saving
70.77 ms). This is an interpreter-specific result on Python 3.14.7; CI's
Python 3.13 must not be assumed to have the same saving. The context manager
restores the caller's GC state on return, Exception and KeyboardInterrupt.

**Cooperative reduction bound:** `_aggregate` has no deadline check. It runs
one complete reduction before the existing post-scan check can refuse a late
result. The measured phase reductions were 69.3–70.7 ms on this corpus; that
is the measured overshoot component, not a universal ceiling as data grows.
A complete-but-late read still prunes its cache and records timing/deadline.
The budget lever ships, so these measurements document headroom rather than
gate merge. Remeasure on each producing Mac/version before changing its budget.

**Tests must never read a real host store.** `conftest._isolate_host_usage`
redirects all three reader roots and all three caches per test; tests needing
data monkeypatch the reader functions. Without it the suite's result would
depend on which agents are installed on the machine running it — a developer
with `~/.grok/sessions` would see the healthy-tail control pin in
`test_silent_failure_contract.py` fail locally while CI stayed green.

## Init-time event backfill (v0.11.8)

`_run_events_backfill(config, sources, device_id)` runs at the END of `mm init`, AFTER `_register_and_save` and `_ensure_retro_skill_links`. Closes the gap between init and first push: retro-fleet works immediately on a fresh install instead of being empty until the first `mm push` (or autopush hook) fires. Init explicitly resolves sources with `get_sources(config, bootstrap=True)` BEFORE the call, creating the default root at 0700; the backfill writer creates `events/` only when it has rows to append.

**No `mm-push` event by design.** The backfill writes `git-snapshot` rows marked `origin: init`, `sessions-snapshot` rows, and the optional host snapshot. The init origin excludes Git rows from the zero-repository push counters as well as omitting the actual push row (64A). Two consequences:

1. **Push counts stay honest.** An init-counted-as-push would inflate the per-window mm-push count in every fresh-install retro by exactly 1. A "1 more than expected" lie everywhere is worse than the trade-off below.

2. **Cursor stays at "no prior mm-push".** The first real push's `last_push_ts` returns `now - 30 days` again and re-walks the same 30-day window. Aggregator dedups via `(canonical_remote_url, sha)` so retro output is identical; cost is one extra ~500ms `git log` walk on the first real push, paid once per machine.

**Idempotent at the aggregator layer.** Commits dedup by `(canonical_remote_url, sha)`; sessions latest-wins per `(device, source_root, claude_dir)`. Re-running init (or invoking the backfill twice for any other reason) produces the same retro output, only slightly larger events files.

**`mm-events`-resolved gate, mirrored from `_run_events_tail`.** A user who disabled mm-events per-machine (`disabled_sources: ["mm-events"]`) gets a silent no-op — no events dir created, no rows written. Same gate covers fresh installs (mm-events auto-included via `MM_INTERNAL_SOURCE_NAMES`), un-migrated upgraders (no mm-events source in config → no-op), and explicit opt-outs uniformly.

**Forensic-only on failure.** Same `try / except Exception` wrapper as `_run_events_tail`; failures emit `mm: notice: events backfill failed: <type>: <safe_str(msg)>` to stderr. Init proceeds. A budget overrun emits `mm: notice: events backfill budget exceeded` and the partial events written so far stay on disk.

**No init-time subcommand, no marker.** Init backfill stays automatic. `mm recapture [WINDOW]` (Track 30A) is the explicit recovery verb for already-orphaned intervals; it is a git-only primitive, not a wrapper around this backfill (backfill also force-refreshes identity and warms the host cache, and returns `None`).

## Sessions snapshot v=2 full-inventory (load-bearing, v0.11.0)

This walk is Claude-only. Codex rollouts and Grok session directories are
not a metadata-only project ledger; their encoded cwd is a path and does
not go on the wire. Do not extend `walk_session_metadata` to those trees
to chase host parity. Usage interchangeability is Groups 18/21/22/23;
see `docs/designs/host-parity.md`.

`EVENTS_SCHEMA_VERSION` bumped 1 → 2 in Group 8. Pre-v0.11.0, `walk_session_metadata` filtered jsonls by `mtime >= since_ts` — each snapshot was a DELTA. Naive sum of v=1 snapshots double-counted any chat that was touched across pushes; latest-only-wins undercounted by losing prior windows. Codex outside-voice review caught the trap during `/plan-eng-review` for Group 8 (cross-model tension #1).

v=2 sessions-snapshot is FULL INVENTORY: every jsonl in the projects tree is counted regardless of mtime. The aggregator picks the LATEST v=2 snapshot per `(device, source_root, claude_dir)` — produces an accurate point-in-time sessions count for the rendering machine's view of the fleet. mm-push and git-snapshot rows keep delta semantics (commits since last push, dedup-by-sha aggregator side); only sessions-snapshot semantics changed.

**Mixed-fleet transition rule.** Pre-v0.11.0 peers still emit v=1 sessions rows. The retro-fleet aggregator treats v=1 sessions as below-threshold and surfaces "Sessions count incomplete: peer X is on pre-v0.11.0" as part of the fleet-incomplete breadcrumb. Numbers are honestly low, never overcounted. Once the fleet rolls to v0.11.0, every peer emits v=2 and the count is exact.

**`since` parameter retained for API stability.** `walk_session_metadata(claude_dir, since, *, deadline_monotonic)` still accepts `since` to keep the call-site signature stable; the value is now ignored (suppressed via `# noqa: ARG001`). A future v=3 schema can re-introduce delta semantics with a new field name without breaking callers.

**`source_root` field on `SessionMetadata` (load-bearing, post-v0.11.2 Group 8 hotfix).** Every `SessionMetadata` carries a `source_root: str` field equal to `str(claude_dir)` from the `walk_session_metadata` caller. The aggregator keys on the 3-tuple `(device, source_root, claude_dir)` instead of the original 2-tuple — pre-fix, two configured `type: claude` source roots that both contained a project encoded as e.g. `-Users-kb-Documents-foo` silently overwrote each other in `latest`. The schema change is additive (`SessionMetadata` is `TypedDict, total=False` — old readers ignore unknown fields, new readers default missing field to `""`), so no v=3 bump.

**Coalesce pass for the rollout window.** Pre-fix records on synced storage have no `source_root` field (treated as `""`); post-fix records carry the populated path. During the rollout window both shapes coexist for the same project. `aggregate_sessions` runs a coalesce pass between the latest-per-tuple population and the `last_session_at` filter that drops `(device, "", claude_dir)` keys when `(device, "<root>", claude_dir)` exists for the same device. Distinct populated `source_root` values are preserved (the legitimate two-source-root case the fix is for); only the legacy empty key with a populated sibling is collapsed. Pinned by `tests/test_retro_fleet_aggregator.py::TestSessionsSourceRoot` (4 tests including the REGRESSION pin `test_two_distinct_source_roots_kept_separate`).

## Aggregator custom-path notice (post-v0.11.2 Group 8 hotfix)

`_emit_custom_path_notice_if_due(events_dir)` runs from `aggregator.main()` right after `events_dir = _resolve_events_dir()`. Library callers of `aggregate()` never see the notice — the gating is in `main()` only. Three-stage gate: (1) `MM_EVENTS_DIR` set → silent (user is overriding correctly); (2) resolved `events_dir != DEFAULT_EVENTS_DIR` → silent (already non-default via param/env); (3) `_read_mm_events_config_path()` returns the configured `mm-events` path; if it equals `DEFAULT_EVENTS_DIR.parent` → silent (config matches default), else emit one `mm: notice:` to stderr pointing at the env override. `_read_mm_events_config_path` mirrors `_read_config_author_emails` — wraps `from mind_meld.config import CONFIG_PATH, load_config` in `try/except Exception`, returns None on any failure, never raises. Pinned by `tests/test_retro_fleet_aggregator.py::TestCustomPathNotice` (5 tests).

## Group 8 retro-fleet skill — symlink installer (load-bearing, v0.11.0; store, v0.12.38)

Agent links point at an mm-owned **constant** store, not at the running
package. `_skill_store_dir()` is `~/.local/share/mind-meld/agent-skills/retro-fleet/`.
`MM_SKILLS_DIR` is a test-only override, gated on `PYTEST_CURRENT_TEST`.
Set it outside a test and it is ignored, with one `mm: error:` to stderr.
`conftest.py:_isolate_skill_links` sets it via `monkeypatch.setenv`. The
user-facing `--store PATH` form is deliberately unshipped. `mm` copies **only** `SKILL.md` there via
`fsutil.atomic_write_bytes`. `aggregator.py` stays in the wheel and is imported
by `cli.py:retro_fleet_cmd`. Never symlink the store at the package — that
moves the dangle one hop.

Source dir on disk is `retro_fleet/` (underscore — Python identifier so
`mind_meld.skills.retro_fleet.aggregator` is importable); link name is
`retro-fleet` (hyphen — Claude Code skill convention).

**`AGENT_ROWS` is the one table.** Every consumer — descriptors,
`skill_targets()`, the installer, diagnosis, test isolation, and the
real-home guard — derives from it. It lists **active supported agents**;
removal is a deliberate retirement, not a reordering (Track 37C dropped
the OpenCode row). Canonical `~`-relative roots live on
the row; `_TEST_SKILL_ROOT_OVERRIDES` is empty in production and is the
only thing tests patch to redirect paths. The guard derives from
`AGENT_ROWS`, never from the override map. `_real_guard_paths` keeps
`~/.config/opencode/skills` as an explicit extra after that retirement
("retired but still guarded — the link is still on disk"). A call-time `SkillTarget`
descriptor owns each agent root. `SkillInstallResult` reports
`installed`, `unchanged`, `unavailable`, `dangling-ours`,
`dangling-ours-legacy`, `foreign`, `failed`, or `declined`. `skill_src` is provenance
(the package dir); `link_target` is the store path. `_ensure_retro_skill_links`
(plural) is the one every caller uses.

**Publish-before-link.** The link step is gated on `store/SKILL.md` being
verified present and non-empty **in this run** — normally by the publish
itself. One documented exception: when `_resolve_retro_skill_source_once()`
raises against an ALREADY-healthy store, `skill_src` goes `None`, publish is
skipped, and `_store_is_healthy` alone carries the gate (a broken package
source must not take a working store offline). Publish *failure*, and an
unhealthy store with nothing published, both ⇒ every link untouched ⇒
`failed`. Catch `(OSError, StorageError)` at the publish
site (`StorageError` is not an `OSError`). Payload then metadata; hash is
recomputed from the store file on read. Freshness is monotonic
`packaging.version` then hash: never silent-downgrade; equal-version-differing-hash
republishes with a notice. `lstat`-refuse a symlink or regular file at the
store dir and payload. A dedicated flock serializes publishers (`init` and
`install-skills` do not hold the mm lock).

**Store ownership is the `.mm-owned` sentinel or mm's namespaced
`.mm-skill.json` — NEVER the payload.** `SKILL.md` is the canonical Agent
Skills filename, so it is exactly what a user hand-authoring their own
`retro-fleet` skill at that path would name. Counting the payload as proof of
ownership made a payload-only directory read as owned, planted the sentinel,
gave `_should_publish` a `meta is None`, and published over the user's file
with no backup and no notice. A non-empty store directory with neither marker
is `FileExistsError("foreign non-empty skill store")`, which surfaces as
`failed` and leaves every link alone.

**Migration (liveness, not shape alone).**

| resolved | shape | on push | on `mm install-skills` |
|---|---|---|---|
| resolves | checkout | leave alone, notice | migrate |
| resolves | package | migrate | migrate |
| dangling | package | repair | repair |
| dangling | checkout | repair | repair |

`dangling-ours` is claimed only for `os.readlink(target)` byte-equals the store
constant. Package/checkout dangling uses `dangling-ours-legacy`. Live checkout
links are never touched on push. Re-point via `os.symlink(store, tmp)` then
`os.replace`; never `unlink` the skill link first.

**Quiet-gate.** Autopush (`quiet=True`) classifies and notices but does not
rewrite agent config. Interactive `mm push`, `init`, and `install-skills` write.

**State machine.** Consent is checked after the pytest isolation guard and
before any `stat` on that agent root: a declined row is `declined`, costs
zero I/O, and reaches neither `_failed_result` nor `_emit_status_notice`.
Absent root → `unavailable`; non-directory/I/O → `failed`; missing skills dir
under an available **and consented** root → `mkdir(mode=0o700)`. Then: store
link that resolves → `unchanged`; store link that dangles → repair if writing
else `dangling-ours`; foreign file/dir/symlink → `foreign`, never unlink;
absent target → symlink to the store → `installed`. `dry_run=True` returns full
classifications with zero writes.

**Consent (Track 25C).** Writes are gated on the same bit the host-usage read
gate already uses: the source name in `get_sources(config)`. That is symmetry
with the read gate, not ideal consent — on a non-explicit (legacy) config
`get_sources` auto-detects by directory existence, so the gate is a no-op
there and `mm disable-source` is the lever. `AgentRow.consent_source` is a
required `str` (no default, no `None`): a future row must state its policy.
`consented_agent_keys(config, sources)` is the **one derivation**. Do NOT
reintroduce a second `row.consent_source in enabled` test anywhere else.

The helper takes already-resolved sources and does not import `config` or
call `get_sources` — that keeps `skill_link` a near-leaf and keeps one
`get_sources` call per command. It catches nothing; `config.py:_validate`
owns every `[skills]` check. Config absent (`None`) → every registry key
(fresh-pipx `mm install-skills`). `[skills] maintain_links` false → empty.
`agents` present → that list ∩ known keys; use **key-absence** (`"agents"
in skills`), never a falsy check — `[]` is a `ConfigError` pointing at
`maintain_links = false`. Else derive from the passed-in source names.
Unknown names in `agents` are accepted and inert. A non-empty `agents`
list whose intersection with known keys is empty emits one `mm: notice:`
per process (`_EMPTY_AGENTS_NOTICE_EMITTED`); every link is then
`declined`. That is how `[skills] agents = ["opencode"]` behaves after
Track 37C.

`_row_is_consented(key, may_create)` is `may_create is None or key in
may_create`. `None` still means allow-all (fresh-machine intent). The
two writers (`_ensure_retro_skill_links`, `_skill_links_check_due`) take
`may_create` as a keyword-required parameter with no default: a
forgotten production call is a `TypeError` on the first test run
instead of silently authorising every row. `diagnose_skill_links` keeps
the argument optional and maps `None` to
`maintain_links: "unknown (policy not resolved)"` — a diagnostic must
not assert a policy it never resolved.

`_skill_links_check_due` and `_ensure_retro_skill_links` MUST receive the
same `may_create` in one push. A declined row never gets its success marker
touched, so an unfiltered gate stays open and runs the full installer
prologue on every push.

**Store publish is never gated on agent consent.** The store lives at
`~/.local/share/mind-meld/agent-skills/`, an mm-owned path. `_skill_links_check_due`
returns True when the owned store exists and `_store_needs_refresh()`,
independent of any row. `_ensure_retro_skill_links` publishes before its
empty-available return when the store already exists. An all-declined
machine with no store does not create one.

**`declined` is an installer status, not a diagnose `status`.** Link state
and policy are orthogonal: a declined row can still be `status: ok` when
an mm-owned link resolves. `mm diag` carries policy in `maintain_links`
(`enabled` / `disabled (not authorized by skill-link policy)` / `unknown
(config invalid: …)` / `unknown (policy not resolved)`). The renderer receives the resolved consent set, not
the original policy branch, so it must not falsely blame a disabled source
when an explicit `[skills] agents` list made the decision. Never render a
policy from a config you could not parse.
`declined` is not in `BROKEN_SKILL_STATUSES`; `mm status` stays silent for
a deliberate decline.

The one-time 0.12.42 policy-transition notice (`maybe_emit_policy_transition`,
`declined_owned_link_rows`, `policy_transition_text`,
`policy_transition_acknowledged`, `acknowledge_policy_transition`,
`_POLICY_TRANSITION_MARKER`, and the private `_join_display_names` that only it
used) was **deleted in v0.12.44**. It explained one thing: mm no longer repairs
a link whose row is unauthorized. `README.md`'s troubleshooting entry says that
permanently and was deliberately KEPT when the code went — `docs/TODOS.md` said
to delete it too, and that was overruled, because 2 of the 3 fleet machines
(`mm devices`: 0.12.13 and 0.12.34.1) never ran a version that could emit the
notice and will upgrade straight past it. A transient notice they cannot receive
is worth less than a README entry they can. Do NOT reintroduce a one-time
stderr notice for a fleet whose machines skip releases; put it in the README.
The `~/.config/mind-meld/.skill-link-policy-v0.12.42` marker is left in place as
inert state — `mm gc` does not reap it, and a reaper for one dead marker costs
more than the byte it saves.

**`mm install-skills --agent KEY`** writes `maintain_links = true` and
the known entries from `sorted(effective_before | {KEYS})` in registry
order, preserving unknown entries from an existing explicit `agents` list,
then installs every consented row. A bare `[KEY]` would convert the
source-derived fallback into an explicit allowlist and drop the user's
other links; dropping unknown entries would erase a future-agent pre-grant.
It grants only link maintenance — not source sync, not usage reading. It
refuses and exits 1 if it cannot persist (no config, or write failure). Bare
`install-skills` with no config keeps the fresh-machine allow-all; a
present-but-broken config fails closed and exits 1.

**Deletion is intent, not damage (Track 28A, v0.12.44).** An ABSENT target
whose row has a success marker means the user removed a link mm created, and
`mm push` MUST NOT recreate it. This is the whole Track: before it, `rm
~/.codex/skills/retro-fleet` was undone by the next interactive push, which is
why a `mm uninstall-skills` verb, a `[skills] revoked` denylist, and a whole
third policy axis all looked necessary. None of them were. No normal CLI tool
needs a denylist to make `uninstall` stick; it needs an installer that does not
resurrect. Track 37C's one-shot OpenCode reaper is a different path: mm
removing a link **it** created and no longer maintains, guarded by
`readlink(target) == store`. Interactive `mm push` calls it from
`_push_core` even when the 24h skill-link gate is shut; `mm init` and
`mm install-skills` reach it through `_ensure_retro_skill_links`. An
absent link after that reaper is not user intent in the 28A sense, and
28A does not forbid mm from deleting its own retired link.

Absent-plus-marker is the ONLY state that means intent. Every other broken state
keeps the link itself — `dangling-ours` (store gone), `dangling-ours-legacy`
(old package path), `foreign` (someone else's file) are all "a link is present
and wrong", which is damage, and mm still repairs or reports each exactly as
before. A missing link is not damage.

Three load-bearing details:

* **`_marker_exists` is existence, NOT freshness.** `_marker_is_fresh` answers
  "should the gate re-check this row" and has the 24h TTL; `_marker_exists`
  answers "has mm ever resolved this target successfully" and ignores mtime.
  The marker is touched on every successful outcome including `unchanged`.
  Precisely: it records "mm looked and was satisfied", NOT "mm created this
  link" -- the live-checkout branch touches it for a dogfood link mm refuses to
  own, so a deleted checkout link is left deleted too. Defensible (you deleted
  it), but do not restate the guard as resting on a "link mm created" proof. Both **fail OPEN** on an unreadable marker dir. Fail-closed
  was tried and rejected during review: it trades a visible, recoverable
  outcome (a resurrected link you can delete again) for a silent one (self-heal
  suppressed forever with no message), which is the TODO#3 bug `_marker_is_fresh`
  was fixed for and a violation of the visible-failure contract.
* **`_skill_link_check_due_at` does ONE `lstat` and returns the absent case from
  inside its own `FileNotFoundError` handler.** Not a style choice. The gate must
  stay SHUT on an absent target, or it returns True on every push forever for a
  row whose only outcome is a no-op: the installer declines to recreate, so it
  never touches the marker, so the marker goes stale, so the gate stays hot. The
  first draft expressed this as a separate predicate placed above
  `_marker_is_fresh` — correct, but order-dependent, and a ship-review mutant
  that moved it one line down broke the feature while 2741 tests passed.
  Returning from the handler makes that mutation unrepresentable and drops one
  `lstat` per consented row per push. A non-`FileNotFoundError` `OSError` is NOT
  a removal: fail open. Store refresh is unaffected — that path in
  `_skill_links_check_due` is independent of any row. Pinned by
  `test_the_gate_stays_shut_after_the_marker_goes_stale`, which is the test that
  killed the mutant.
* **`explicit=True` skips the guard, and that is the documented undo.**
  `mm install-skills` and `mm init` rebuild. `install_skills_cmd`'s docstring
  already named this exact case as its first use ("post-cleanup recovery (link
  removed by hand...)") three releases before the Track existed.

**Consent and presence are independent switches.** Consent answers "may mm
maintain this row"; presence answers "is there a link". Re-granting consent does
NOT un-delete — otherwise flipping a source off and on would silently undo a
deliberate removal. Pinned by
`test_consent_churn_does_not_resurrect_a_deleted_link`.

**`mm diag` splits `absent` from `removed-by-user`.** Both are
working-as-intended and neither is in `BROKEN_SKILL_STATUSES`, but only one is
answerable with "run `mm install-skills`", and collapsing them left `mm diag`
unable to confirm a deliberate deletion — the read-back hole the review flagged
as the largest DX gap. This is LINK state, computed in `_diagnose_one`, so it
does not touch the `maintain_links` field or the renderer contract below.

The accepted regression: if an agent app wipes its own skills directory, mm no
longer silently heals it and the user runs `mm install-skills`. That is
normal-tool behavior (reinstall the extension) and it is the correct trade for
making deletion mean deletion.

**`BROKEN_SKILL_STATUSES` is an allowlist of broken states, never a denylist of
healthy ones.** `mm status` reads it. `ok` / `live-checkout` (the deliberate
dogfood link the installer preserves) / `foreign` (the user's own file) /
`absent` (agent not installed) are all working-as-intended and permanent, so a
denylist (`status not in ("ok", "absent")`) reported two of them as broken on
every run, forever, and pointed the user at `mm install-skills`, which would
have migrated the checkout link away. An allowlist also defaults any status a
later Track adds to not-broken rather than broken. `foreign-dangling` is split
out of `foreign` and IS in the set: a dangling link to an unrecognized path is
broken from the agent's view even though mm must not touch it.

**`diagnose_skill_links()` must never raise.** `mm status` and `mm diag` both
call it with no enclosing handler — the two commands you run to diagnose a
broken link crashed on the broken link. The per-descriptor body (`_diagnose_one`)
raises freely and the caller degrades that descriptor to an `error` row.
Escapes that got through the first version: `UnicodeDecodeError` (a
`ValueError`, not an `OSError`) from a bad byte in `.mm-skill.json`,
`RuntimeError` from a symlink loop, and `PermissionError` from `Path.exists()`
on an unreadable agent dir.

**Symlink-loop classification is normalized on errno, not exception type.**
`_symlink_lives` treats py3.11/3.12's `RuntimeError("Symlink loop from ...")`
and py3.13+'s `OSError(ELOOP)` identically as "not live". Otherwise the same
filesystem state is a repairable classification on one Python and a hard
`failed` on another — and CI runs 3.13 only, so the worse branch was invisible.

**The push notice renders through `render_skill_status`,** so the push path and
`mm install-skills` state the SAME cause. One hardcoded string said "is not
mm's store link" for every branch, including `dangling-ours` — a link whose
`readlink` byte-equals the store constant, which is the very thing that proves
it IS mm's — and told the user to move it aside, the one action that stops mm
repairing it. `_emit_status_notice(write=False)` must NOT touch the 24h conflict
marker: a classify-only autopush would otherwise spend the notice budget that
the interactive push which could actually repair the link then goes without.

**Two-marker 24h-TTL gate (cross-model #3).** Success vs conflict-skip markers
under `~/.config/mind-meld/`. Transient failures touch neither. `_marker_is_fresh()`
fail-opens on EACCES / EIO.

**Drift-aware gate.** After a fresh success marker, the gate verifies the link
is a symlink to the store constant, is not dangling, and that the store is
not stale vs the running package (size or version). Resolver failure with a
healthy store does not fail the gate open.

**Hook positions.** `mm init` calls `_ensure_retro_skill_links(explicit=True)`.
`_push_core` calls it AFTER `_ensure_device_registered` and BEFORE
`_run_events_tail`, with `allow_mutate=not quiet`. Both call sites hoist
`get_sources` above the hook so consent is known; the hook itself does not
move relative to device registration or the events tail. That hoist also
explicitly creates the default mm-events root before the installer. `mm diag` is
passphrase-free and prints the skill-links block plus `host_skill_discovery`
(Grok inspect probe; sibling key, never a `skill_links` row). `mm status` prints one
line only when a link is broken (the first broken row through
`render_skill_status`, including the restart clause). It is silent for every
working-as-intended state, including `removed-by-user` — a deliberate deletion
is not a fault. The 0.12.42 policy-transition notice that used to ride along
here was deleted in v0.12.44 (see above).

**Host skill discovery (`mm diag` only).** `host_skill_discovery.py:probe_grok_skill_discovery`
runs `grok inspect --json` (argv subprocess, no shell, stdin=DEVNULL, 2s timeout,
capped stdout). It extracts four values — Claude/skills compat bool, whether
`retro-fleet` resolved, the resolved path, and the observed Grok version —
and discards everything else. Failure states are five distinct strings:
`binary-absent`, `timeout`, `nonzero-exit`, `malformed-json`,
`unsupported-schema`. Presence is `shutil.which("grok")`, not
`config.grok_customization_dirs_exist` (that predicate is False when
`~/.grok` has no `skills/`/`commands/`/`rules/`, which is a working Grok
install). `$GROK_HOME` is a `host_usage` sessions-only override and is not
consulted. Never persist or sync the result. Never call this from status,
push, or autopush. Do not append the result to `skill_links` —
`BROKEN_SKILL_STATUSES` filtering and the README uninstall loop treat that
list as link-bearing.

**Registry exit criterion.** mm maintains a skill link only for hosts that
do not discover `~/.claude/skills`. Verified 2026-08-24: Grok 1.0.5 does
(`grok inspect --json`); Codex and OpenCode show no evidence of it. Re-check
with that command, not with static inference.

**`mm install-skills`.** Force-runs the installer with `explicit=True`, ignoring
the TTL gate. Renders `render_skill_status` for foreign/dangling outcomes. Exits
1 for any available foreign/failed result. Leaves user files and foreign
symlinks untouched.

**`mm retro-fleet [window]` typer wrapper (load-bearing, v0.11.22).** SKILL.md's documented invocation is `mm retro-fleet <window>`, NOT `python -m mind_meld.skills.retro_fleet.aggregator`. Reason: the prior `python -m` form failed in real fleet use (user feedback on v0.11.21) on macOS systems where only `python3` is on PATH, and is structurally impossible to fix for the dominant install path — pipx puts mm in `~/.local/pipx/venvs/mind-meld/` and nothing outside that venv can `import mind_meld`. Routing through the `mm` console-script (always on PATH wherever mm is installed) sidesteps both. The typer command is a thin shim: forward-imports `aggregator.main` lazily to keep cli.py module-load fast, builds `argv` from the typer args (positional `window` defaults to `7d`; `--no-author-filter`, `--theme`, `--noteworthy`, `--name`, `--no-save` flags forward verbatim), and `raise typer.Exit(code=...)` so non-zero aggregator exits become the CLI exit code. The aggregator's existing `argparse`-based `main()` is unchanged — direct `python -m` invocation still works from a development checkout, it's just no longer the public surface. Pinned by `tests/test_retro_fleet_cli.py` (TestRetroFleetCommand: positional window, default `7d`, `--no-author-filter` forwarded, theme/noteworthy/name/no-save forwarded, non-zero aggregator exit propagates).

**SKILL.md `## Step 0: preflight` (load-bearing, v0.12.41).** The binary probe sits in its OWN step above `## Step 1`, not inside it. Before v0.12.41 `command -v mm` was the first line of Step 1's four-command block, two paragraphs under prose saying "don't treat a non-zero exit as fatal here" (which is about `mm push` vs `mm autopull`), and Step 1 carried a skip clause for "stale"/"offline" requests that dropped the probe with it. A preflight that is present, discouraged, and skippable is worse than an absent one because it reads as coverage. Step 1's clause now names **Step 1** explicitly; that wording is the whole fix and is asserted positively (`"Skip Step 1 only if" in step1`) because forbidding one phrase passes if the clause is deleted.

**Two stages, and the terminal rule scopes to Step 0 ONLY.** `0A` (`command -v mm`, then `mm --version`) is the only thing that may stop the run; a resolvable-but-broken binary stops too, because later `mm` commands cannot work either. `0B` is informational. The rule must NOT be written as "everything below 0A is informational" — that scopes over Steps 1-5 and licenses an agent to continue past a failed `mm retro-fleet` in Step 2 and synthesize a card from output it never received. Step 0 is silent on a healthy machine; say so, or the agent narrates the preflight on every invocation.

**`0B` relays mm's own upgrade nudge; it does NOT compare versions.** `upgrade.emit_nudge_if_due` already runs at the tail of interactive `mm push` (`cli.py:2778`) and `mm pull` (`cli.py:3271`), checks GitHub `/tags`, and prints to stderr — which terminal collapse hides. That is the only network-authoritative staleness signal the skill can reach, and silence is NOT evidence of freshness (24h `DEFAULT_NUDGE_GAP`, `[upgrade] auto_check = false`, `--no-check-version`, dev-build sentinel, offline). A local `min_mm_version` / `skill_version` comparison was designed and **deliberately cut**: the field is written by the same binary whose freshness is in question (`_publish_skill_store`), it cannot know which SKILL.md the agent loaded (`live-checkout` is a supported status), and two matching stale numbers read as verification rather than as a stale pair. Do not reintroduce it; the negative assertions in the test exist to stop that.

**Every remediation string ends with "restart the agent so it reloads SKILL.md."** Republishing the store does not change instructions already in the running agent's context, so `mm install-skills` alone never fixes the current run. The same clause was added to README's `mm install-skills` row, the store paragraph, and the troubleshooting entries.

**Step 0 cannot reach the users it protects.** It ships through the store republish, which needs an `mm` at least as new as the store, so a user who never upgrades never receives it. Step 0 prevents future drift; it does not cure current drift. README's *"Retro output is missing a block, unexpectedly empty, or older than expected"* troubleshooting entry is the compensating control for that population and must not be deleted as redundant with Step 0.

Pinned by `tests/test_docs_routing.py::test_skill_md_step0_preflight_contract`: both headings exist and are ordered, `mm --version` present, two `STOP`s, `Do not run Steps 1-5`, no escape hatch in Step 0, Step 1's clause present and conditional, `upgrade.INSTALL_CMD` quoted verbatim (bound to the constant so the command cannot rot the way the removed `v0.12.37` floor literals did), and `min_mm_version` / `skill_version` / `sort -V` absent. Assertions run against whitespace-normalized text — the contract is the sentence, not where the line breaks fall. Slice with `str.index` on both boundaries, never `split(heading, 1)[-1]`: that returns the whole document when the heading is missing, and `command -v mm` appears in Step 1 historically, so the naive idiom passes for a MISSING Step 0.

## Two-pass ASCII card + LLM narrative split (load-bearing, v0.12.0)

The retro-fleet output has two artifacts with different production paths:

1. **The ASCII card** — pixel-aligned screenshot artifact rendered by Python. Stats (commits, repos, machines, LOC, streak) come from `RetroData`; `--theme` (×3) and `--noteworthy` flags carry the LLM-synthesized narrative bits in. `_render_ascii_card` pads every line to `CARD_WIDTH` (64) with right border via `╔/╗/║/╝`. `--name` is optional header personalization.

2. **The narrative paragraphs** (praise / level-up / focus) — written by the LLM directly into the conversation, NOT into the card. The SKILL.md instructs one each, anchored in actual commits/stats, framed as investment-advice not criticism.

**Two-pass invocation is load-bearing.** Pass 1 (`mm retro-fleet 7d`) renders the markdown body + a fenced JSON sidecar tagged `<!-- MM_THEMES_PROMPT -->` for theme synthesis. Pass 2 (`mm retro-fleet 7d --theme A --theme B --theme C --noteworthy "..." --name kb`) re-renders with the card pinned at the top. The LLM never counts characters — Python pads. The single-pass alternative (LLM pads its own card content to width) was rejected because Opus drifts by 1-2 chars often enough to ruin screenshots. Pinned by `TestAsciiCard.test_card_lines_pad_to_fixed_width`. `--no-save` is accepted as a hidden no-op (v0.12.39) so a stale skill-store copy of SKILL.md still exits 0; pinned by `test_no_save_is_accepted_as_deprecated_noop`.

**Themes prompt content scope.** The JSON payload includes `window_days` / `since` / `until` / `commits` / `additions` / `deletions` / `top_repos[]` / `ship` (or null). Repo URLs and ship subject pass through `_safe_repo_url` + `_shorten_repo_url` and `_safe_prose` respectively before serialization — the same trust-boundary defenses applied to the markdown body, so a long-canonical-URL or peer-controlled subject doesn't leak into the JSON sidecar. Pinned by `TestThemesPrompt.test_long_repo_url_shortened_in_prompt`.

**`_safe_prose` vs `_safe_short` (v0.12.0).** `_safe_short` whitelists `[A-Za-z0-9._\-() ]` — fine for short identifiers (skill names, model names, sha) but mangles prose punctuation (colons, slashes, hashes, em-dashes). `_safe_prose` strips terminal escapes + Rich markup + C0 controls but preserves printable punctuation — use for commit subjects (peer-controlled) and LLM-supplied theme/noteworthy/name lines. Both call through `safety.safe_str` so the terminal-escape defense is shared.

## Trends vs prior equal period (v0.12.39)

`## Trends vs prior <N>d (A → B)` is a four-row `prior | current` table computed from the already-in-memory synced events corpus. It is fleet-deterministic for the first time: two machines that have pushed-and-pulled produce the same trends section, because the baseline is a function of the corpus, the window, and `now`, not of this machine's command history. The v0.12.0 machine-local snapshot cache (`~/.local/share/mind-meld/retros/`) is gone. Mixed-fleet window: snapshots were never synced, so an upgraded Mac and an old one produce different trend sections from the same corpus until both upgrade — that is the pre-existing non-determinism being fixed, not a new bug.

**Architecture.** `_aggregate_git_period_pair(events, prior_start, boundary, until, author_emails) -> (PriorPeriod, PriorPeriod)`, not `aggregate(compare_prior=True)`. A union scan first rejects out-of-pair occurrences, then dedups `(canonical remote, sha)` GLOBALLY across the eligible copies before updating either bucket. An out-of-window first copy must never consume the key and hide a valid in-window copy; eligible duplicates still cannot enter both periods. Do NOT call `aggregate()` twice: `get_known_devices()` shells out inside it. `aggregate` materializes `_list_event_files` / `_iter_jsonl` once, unwindowed, so the prior period is a second pass over the same in-memory list. Pinned by `test_aggregate_reads_events_dir_once_and_shells_out_once` and `test_out_of_window_duplicate_does_not_hide_current_commit`.

**Half-open periods.** Shared predicates are inclusive on both ends (`since <= x <= until`). A naive adjacent prior period double-counts the boundary. Fix at the call site with `prior_until = since - timedelta(microseconds=1)`. Do NOT edit the shared predicates — that silently moves the current window's numbers. Pinned by `test_commit_at_exactly_since_counts_once`.

**Coverage floor, not arithmetic.** `coverage_floor = min(YYYY-MM-DD parsed from the event filenames `_list_event_files` already globs)`. Gate: `coverage_floor <= prior_start.date()`. Filename date is push day, so a `git-snapshot` row can carry commits older than its file — the floor is a LOWER BOUND on coverage and fails safe (it can refuse a comparison that would have been fine, never the reverse). Do not "optimize" this into using commit dates or file mtimes. The filename proof is valid only when every globbed event file parses cleanly: any skipped event record makes Trends unavailable, because an unreadable prior record must never render as a known zero. `2 * window_days > EVENTS_RETENTION_DAYS` is off-by-one (`age_days >= 90`) and measures a max-age policy that only runs from the manual `mm gc` command; it survives only as a fast path for unavailable-message wording. Pinned by `test_prior_window_before_coverage_floor_is_unavailable`, `test_unreadable_event_records_make_trends_unavailable`, and `test_45d_window_refused_at_retention_boundary`.

**Row set.** `commits`, `additions`, `deletions`, `active_days` — four genuine flows windowed on the commit's own date. Trends use UTC day keys and UTC period labels, unlike the intentionally local streak and weekly views, so the table remains fleet-deterministic across timezones. Dropped, recorded here so nobody re-adds one:

* `streak_days` — state at `until`, not a flow over the window (`↓41` reads as "lost 41 streak days" when it means the streak broke).
* `sessions` — v=2 full inventory restricted to projects whose `last_session_at` is in-window, not a flow. Differencing two inventories is the same category error.
* `tokens` — ~99% `cache_read`; rose in 6 of 9 measured weeks regardless of whether commits rose. Also structurally biased: re-calling `aggregate_sessions` with `until=since` rejects the current snapshot on its envelope before reading `tokens_by_day` buckets that cover the prior period.
* `pushes` — measures sync cadence, not work.

`PriorPeriod` holds integers only, with no reference to the prior `GitAggregate` / `SessionsAggregate`. Pinned by `test_prior_period_holds_only_integers`.

**Render states.** Section renders only when `window_days < 14` (`_render_weekly` owns ≥14d). Below `## Code shipped`. Unavailable (coverage proof unmet **or** unreadable event records) renders the heading with the reason inline — a vanished section never encodes a data-availability state. Current-window-empty suppresses the section entirely (never itemize a week off). `0` is known-zero; `—` is unavailable. No arrow glyphs. Both windows use today's author-email union; `--no-author-filter` is consistent across both. Fleet composition change (`prior.devices_with_pushes != current`) produces the `fleet_changed` issue in `MM_HEALTH`. No card row.

**`--no-save` is a hidden no-op.** Removing it is a silent truncation of `mm retro-fleet 30d --no-save > /tmp/retro.md` (exit 2, 0-byte file) and breaks the `/retro-fleet` skill's Step 4 exactly once per upgrade, because SKILL.md is copied into the skill store and refreshes on `mm init` / non-quiet `mm push` / `mm install-skills`, not on `pipx upgrade`. Keep the flag (`hidden=True` in typer, `help=argparse.SUPPRESS` in argparse), ignore the value, emit one `mm: notice:` to stderr only when actually passed. Snapshot saving was removed in v0.12.39; the notice names **2.0** for removal of the flag. The current SKILL.md Step 4 no longer passes it; stale stores remain supported until 2.0.

**Orphan dir.** `mm gc` runs `retention._gc_orphan_retros_dir`: unlinks only `_SNAPSHOT_FILENAME_RE`-matching files, then `rmdir` if empty. Never `rm -rf`. Dry-runnable, best-effort.

## Aggregate metrics added in v0.12.0

`aggregate_git` collects four additional views in the same per-commit pass — keeping the iteration single-pass (no second walk) and the data dataclass-bound for renderer simplicity:

- **`commit_types: CommitTypes`** — conventional-commit prefix counts (`feat`/`fix`/`refactor`/`test`/`chore`/`docs`/`perf`/`style`/`build`/`ci`/`revert`/`other`). `_classify_commit_subject` matches the regex `^([a-z]+)(?:\([^)]*\))?!?:` so scoped (`fix(cli):`) and breaking (`feat!:`) variants normalize to the bare keyword. Subjects that don't match the pattern bucket as `other`.
- **`hourly: dict[int, int]`** — 24-hour histogram in local time. Renderer caps at TOP_N_HOURS (5) peak rows.
- **`bursts: CommitBursts`** — 45-min-gap clustering. Naming is intentional: "commit bursts" not "sessions" — the heuristic counts commit clusters, not cognitive flow, and a real coding session that stops for lunch / debugging without commits / deep think will fragment into multiple bursts. The honest framing avoids collision with Claude Code "sessions" already counted via `SessionsAggregate.total_sessions`. Buckets: deep ≥50min, medium 20–50min, micro <20min. Single-commit bursts have span 0 and land in micro.
- **`ship: ShipOfWeek`** — single highest-LOC commit (max `add+del`). Pure data; the LLM picks up subject + repo + sha for the card synthesis.
- **`weekly: list[WeeklyBucket]`** — Monday-anchored 7-day buckets. ONLY populated when `window_days >= 14` (the 7d default path emits an empty list). `active_days` per bucket counts unique commit dates within that bucket.

`window_days` is now plumbed to `aggregate_git` (default 0 keeps the unused-by-foreign-callers path safe). The `aggregate()` orchestrator forwards it.

## Fleet-wide author email trust set (load-bearing, v0.11.17)

`mind_meld.identity` owns the running machine's locally-known author-
email set. The cache at `~/.config/mind-meld/identity-cache.json` (mode
0600, fcntl-flocked via `lockedjson.locked_json_rmw`, 7-day TTL) feeds
both push tail and retro render so they share state. Pre-v0.11.17, the
gather lived in `aggregator.py` and ran every retro — different machines
produced different filters from the same synced events, breaking
cross-fleet retro determinism.

**Push-time emit.** `_run_events_tail` calls `identity.gather_local_
identities(allow_refresh=True)` and threads the result into
`make_mm_push_event(local_emails=...)`. Every `mm-push` event row on
synced storage carries the emitting machine's identity set. Cache hit
is ~1ms; cold/stale cache emits a single `mm: notice: refreshing
identity cache (one-off)` and runs a synchronous refresh inline.
**No autopush-budget contortions** — D1 from /plan-eng-review locked
"synchronous refresh on cold cache, tell the user, accept the one-off
slow path." No background threads, no empty-emit-and-self-heal-later.

**Retro-time union.** `aggregator.aggregate_local_emails_from_events`
walks every `mm-push` row in the events dir and unions every peer's
`local_emails` field into a single fleet-wide set. The aggregator
combines this with the running machine's locally-passed `author_emails`
(default: `gather_author_emails()` shim → identity cache) to build the
effective filter. `aggregate_git` filters commits against that union.
Result: machine A and machine B running the same retro after sync
produce byte-identical output (pinned by `TestFleetDeterminism`).

**`author_emails: frozenset[str] | None` semantics.** `None` = filter
explicitly disabled (`--no-author-filter` wires this). Non-None
(possibly empty `frozenset()`) = union with fleet emails, then filter.
The `None` carve-out is load-bearing: an empty `frozenset()` would
silently re-enable the filter via the union if the user's intent
"render every commit" wasn't preserved separately.

**Mixed-fleet rollout (D3 from /plan-eng-review: lockstep upgrade,
no breadcrumb).** A pre-v0.11.17 peer's `mm-push` row has no
`local_emails` key. The union step skips those rows silently — that
peer's identities aren't in the fleet trust set until they upgrade
and push. The running machine's local set still covers itself
(self-fallback). No `pre_emails_peers` Notes counter ships; the user
upgrades all peers in lockstep instead. Pinned by
`TestMixedFleetRegression` — three scenarios: legacy-rows-aggregate-
cleanly, running-machine-local-covers-self, empty-fleet-falls-back-
to-local.

**Init-time cache warm (D5).** `_run_events_backfill` calls
`identity.refresh_identity_cache(force=True)` at the end of `mm init`
so the first push after init has a hot cache and emits no slow-path
notice. Failure is forensic-only via `mm: notice:`; backfill proceeds.
Init isn't time-budgeted — extending it by ~1-5s on cold network for
`gh api user` is invisible.

**Sources unioned at refresh time** (any single source's failure
yields nothing for that source; cache still rebuilds with what was
reachable):

1. `git config --global user.email`
2. Per-repo `git config user.email` for every discovered git root,
   bounded by `_PER_REPO_BUDGET_S` total wall-clock (5s)
3. `[retro].author_emails` from mm `config.toml` — additive (D4 from
   /plan-eng-review: backwards compat with existing configs)
4. `<id>+<login>@users.noreply.github.com` derived from `gh api user`
   when `gh` is authenticated

**Trust-rooted invariant.** Identity gather NEVER walks `git log` for
author emails. Walking commits would pull in collaborator emails on
shared repos (their PRs / pulled-in commits sit in local history) and
silently inflate the trust set. Only configured identities count.
REGRESSION pin: `tests/test_identity.py::TestGatherSources::
test_collaborator_email_in_repo_history_NOT_included` builds a real
git repo with both kb and alice commits and asserts alice doesn't
leak into the gather output.

**`mm refresh-identity` user-facing CLI.** Force-runs
`refresh_identity_cache(force=True)` ignoring the TTL. Use after
editing `[retro].author_emails`, `gh auth login`, or
`git config --global user.email`. `--json` flag for scripting; default
output lists the resolved emails and, when the prior cache is readable, the `+`/`-` changes. Exits 1 with a `mm: warning:` when
no emails resolve. Kebab-case-plural matches `mm install-skills` /
`mm migrate-config` / `mm reconfigure-sources` precedent.

**`MmPushEvent.local_emails` schema is `list[str]` (additive on v=2,
total=False, no schema bump).** Same precedent as v0.11.14's
`tokens_by_day`. Pre-v0.11.17 readers tolerate the unknown field;
post-v0.11.17 readers extract it via `.get("local_emails")` (defensive
parse — non-list, non-string entries are silently skipped). Empty list
is emitted explicitly when the running machine has no configured
identities, distinguishable on the wire from "pre-v0.11.17 peer with
no field at all."

**`SessionMetadata.skills_by_day` schema is `dict[str, dict[str, int]]`
(additive on v=2, total=False, no schema bump).** Same additive-field
precedent as `tokens_by_day` (v0.11.14) and `local_emails` (v0.11.17).
Walked from each Claude Code session jsonl's assistant `tool_use` blocks
where `name == "Skill"`. Subagent invocations attribute to the parent
project's bucket (mirrors token attribution).

**KEY-ABSENT vs EMPTY-DICT discriminator (v0.11.27, semantic widened
v0.12.4 post-/plan-eng-review 2026-05-10).** The aggregator's
`pre_skills_peers` flag uses `"skills_by_day" not in proj`, NOT
`proj.get("skills_by_day")` falsy-check. A session can legitimately invoke
zero skills, so an empty `{}` is a content signal ("no skills used in
window"), not a version signal. Conflating the two would surface "Skills
incomplete" on every retro for users who don't lean on skills.
**Track 58A applies the same empty-map distinction to `pre_token_peers`:**
when `sessions > 0`, only an absent or non-dictionary `tokens_by_day`
counts as missing token coverage. A present `{}` is a completed
observation of zero tokens and does not increment the missing-project
or missing-session counts.

**Two populations land in `pre_skills_peers`:** (1) pre-v0.11.27 mm
peers whose code never emits the field, and (2) v0.11.27+ peers whose
skill walk was skipped this push because `events.py:_scan_one_project`
ran with `token_cache_files=None` (cold token cache + autopush gate at
`events_tail.py:_decide_token_walk_policy`, or warn-mode flock contention where
`lock_and_get_files("warn")` yields `None`). The wire genuinely can't
distinguish the two — both ship the field absent. The `skills_incomplete`
health issue, built in `aggregator.format_retro`, mirrors `pre_token_peers`'s
"OR with cold token cache" phrasing to admit the ambiguity honestly.

**Why not "always set `meta['skills_by_day'] = {}`" (rejected fix,
post-/plan-eng-review 2026-05-10).** A surface-cleaner alternative
would be to drop the `if token_cache_files is not None:` gate at
`events.py:_scan_one_project` and emit `skills_by_day = {}` on every
snapshot.
**Codex outside-voice review caught:** the aggregator picks the LATEST
sessions snapshot per `(device, source_root, claude_dir)` in
`aggregator.aggregate_sessions` (the `latest = filtered_latest`
reduction). With the always-set fix, a v0.11.27+ device that
pushes warm at T1 (populated `skills_by_day`) and then cold at T2
(synthetic `{}`) has its T1 skill data silently overwritten by T2's
empty dict, AND the `skills.available = True` assignment further down
that same function flips
on so the renderer confidently shows "0 skills" instead of the
existing "Skills incomplete" notice. Net regression — visible-
misclassification turned into invisible-data-erasure. Keeping the
absent/empty asymmetry plus the honest breadcrumb text is the correct
tradeoff while pre-v0.11.27 peers age out of the fleet. The longer-
term proper fix (explicit `skills_walk_complete: bool` schema field
that lets the aggregator preserve last-populated-skills) is captured
in `TODOS.md` for future revisit if disambiguation becomes
operationally valuable.

Pinned by `test_skills_by_day_empty_dict_when_no_skill_blocks`,
`test_d4_empty_skills_dict_does_not_flag_pre_skills_peer`, and
`test_skills_incomplete_breadcrumb_admits_cold_cache_ambiguity`
(v0.12.4).

**Cache shape upgrade gate (D2 from /plan-eng-review 2026-05-06).**
`token_usage.get_or_compute` checks `"skills_by_day" in entry` on the
size/mtime cache hit — pre-v0.11.27 entries match size/mtime but lack
the field, so they fall through to a fresh walk. NOT a `CACHE_VERSION`
bump (would invalidate token data fleet-wide unnecessarily). One-time
post-upgrade re-walk gated by the existing 5s warm budget. Token data
is preserved byte-identical because `walk_jsonl_buckets` re-derives
both views from the same source. Pinned by
`test_d2_old_entry_without_skills_field_triggers_rewalk`.

**Incremental resume (v0.12.15) — read before touching
`token_usage.walk_jsonl_segment`, `iter_bounded_lines`,
`_drain_to_newline`, `_resume_plan`, `head_fingerprint`, or
`TAIL_MSG_ID_LOOKBACK`.**

A cache miss no longer means a full re-parse. Session jsonls are
append-only and routinely exceed 10 MB, so re-reading one end to end
because it grew by 300 KB made the events tail cost O(total bytes on
disk). Measured on a 107-project Mac: 56 MB across the eight largest
sessions cost 218 ms to re-walk for 1.7 MB of actual new data; the
resumed walk costs 9 ms. That gap is what produced the recurring
`mm: notice: events tail budget exceeded` — the notice was a symptom,
and raising `WALK_TIME_BUDGET_*` would have treated the symptom.

Five properties are load-bearing. Break any one and the retro silently
reports wrong token counts:

1. **The resume offset only ever advances past COMPLETE lines.** A
   trailing chunk with no newline is Claude Code mid-write:
   `iter_bounded_lines` neither yields it nor counts it (unless the
   caller passes `yield_final_partial=True`, which only one-shot readers
   do), so the next
   walk re-reads it whole. This is the entire basis for persisting an
   offset at all. A skipped OVERSIZE line is yielded as `b""` rather
   than swallowed — the caller advances its offset on every yield, and
   the parse loop already skips blanks. Without that, a trailing
   oversize line pins the offset behind itself forever and re-drains
   the same megabytes every push. Pinned by
   `test_partial_trailing_line_deferred_then_counted_once` and
   `test_resume_after_oversize_line_keeps_offset_aligned`.

2. **`tail_msg_ids` is the only cross-segment dedup state, and its
   bound is MEASURED.** No line is read by two segments. Message ids
   still need a seed: Claude Code writes one line per model iteration
   under a SHARED `message.id`, each restating the same cumulative
   usage, so a resume point landing between two iterations makes an
   unseeded second segment count the message again.

   The measurement that sets the bound (re-run it before changing
   `TAIL_MSG_ID_LOOKBACK`): across 358 live session jsonls, **26,989**
   assistant lines repeat an already-seen `message.id`, and **zero** of
   those repeats are separated by even one other distinct id. Every
   repeated run is strictly contiguous, so a lookback of 1 suffices
   today and 8 is headroom. `_carry_tail_ids` keeps the window in
   RECENCY order — a re-seen id moves to the end — so the trim can't
   evict the id whose message is still in flight. It also preserves the
   prior seed when a segment parses no assistant messages at all, so an
   append of pure user turns can't drop the guard.

   Tool_use ids deliberately get NO seed. The risk is real but
   unobserved: a retry re-emitting the same `tool_use.id` on a LATER
   line, split across a resume boundary, counts twice incrementally
   where a full walk counts once. The same corpus has **zero**
   duplicate `tool_use.id` values, so the seed would be pure cache
   weight. If duplicates ever appear, seed them exactly like
   `tail_msg_ids`. Pinned by
   `test_straddling_message_iterations_not_double_counted`,
   `test_tail_ids_survive_append_with_no_assistant_messages`, and
   `TestCarryTailIds::test_reseen_id_moves_to_the_end`.

   `_MAX_TAIL_MSG_ID_LEN = 128` caps a carried id. `message.id` is
   peer-controlled jsonl input and a line may reach
   `MAX_JSONL_LINE_BYTES`; eight huge ids would serialize a cache past
   `lockedjson`'s 64 MiB read ceiling, after which every read resets
   the cache and the fleet pays a cold walk on every push, forever.
   Over-long ids are DROPPED, never truncated — truncation could alias
   two distinct ids and silently under-count. Real ids measure 36
   chars. Pinned by `test_oversize_id_is_dropped_not_truncated`.

3. **`head_fingerprint` is not optional, and its window must not
   exceed `offset`.** Size-grew alone cannot distinguish an append from
   a rewrite that lands at or above the cached size.

   The window is `head_probe_len(offset) = min(_HEAD_PROBE_BYTES,
   offset)`, persisted per entry as `head_len`. A FIXED 4 KiB window is
   wrong and was a real bug caught pre-merge: for any file shorter than
   the window the read returns "whole file", so every append changes
   the digest, `_resume_plan` rejects, and the entry silently degrades
   to a full walk forever — correct output, none of the speedup, no
   error anywhere. Clamping to `offset` keeps the probe inside bytes
   already accounted for, which are stable under append by definition.
   `head_fingerprint` also returns `None` on a short read: fewer bytes
   present than the probe claims means the file shrank. Pinned by
   `test_short_file_still_resumes_across_appends` and
   `test_probe_len_never_exceeds_offset`.

   The probe read MUST sit between the pre-walk and post-walk stats, so
   the stat pair brackets it. Reading the fingerprint after the final
   stat leaves a window in which the file is replaced and the old
   buckets get persisted under the REPLACEMENT's fingerprint — an entry
   that later licenses a resume into a file none of its buckets ever
   saw. Pinned by
   `test_fingerprint_read_is_bracketed_by_the_stability_stat`.

   `_resume_plan` falls back to a full walk on any doubt, and it
   validates the CANONICAL persisted shape rather than a merely-plausible
   one: shrink, offset past EOF, offset past the size it was RECORDED
   against (bytes no bucket ever saw), `head_len != head_probe_len(offset)`
   (an in-range-but-shrunk probe would still "pass" while proving
   nothing), malformed buckets, and a `tail_msg_ids` that isn't at most
   `TAIL_MSG_ID_LOOKBACK` unique non-empty strings within the length cap.
   That last one is deliberately a rejection rather than a silent filter
   or an empty-seed degradation: quietly handing back a weaker seed than
   the entry claims is exactly what double-counts the straddling message
   the field exists to protect. When the fingerprint can't be read at all,
   `get_or_compute` persists the LEGACY entry shape (no `offset`)
   rather than an offset it cannot later validate. The rejection matrix
   is pinned by `TestResumePlanRejection`, which carries a
   positive-control case (`test_valid_entry_does_resume`) so the matrix
   can't quietly degenerate into "always full walk" — that control is
   what caught the fixed-window bug above.

   What the probe does NOT do, accepted rather than overlooked: it
   bounds identity, not integrity. A rewrite preserving the probed
   prefix and landing at or above the cached size still passes, and the
   ordinary size+mtime cache hit never consults it. This is a forensic
   cache; a false match costs a wrong retro number, not data.

4. **`_resume_plan` returns deep COPIES of the cached buckets, and a
   failed read persists nothing.** The concurrent-append path
   deliberately declines to persist; merging into the live cached dicts
   would leave the surviving entry double-counted anyway.
   `JsonlSegment.ok` is False when the read failed outright — the stat
   calls bracketing the walk would still AGREE in that case, so
   persisting would pin the file's current size/mtime to buckets that
   never saw its current bytes, making it a permanent cache hit that
   silently stops accounting for the session. Pinned by
   `test_concurrent_append_leaves_cached_entry_untouched` and
   `TestReadFailureDoesNotPersist`.

   Merging goes through `token_usage.merge_token_days` /
   `merge_skill_days`, NOT hand-rolled loops. `events.py`'s
   `_aggregate_jsonl_views_for_project` does the identical merge and
   was the second copy; the incremental merge would have been the
   fifth site CLAUDE.md claims are "consolidated". This module has
   already shipped the
   `mirrored-predicate-drifts-when-one-side-gains-logic` bug twice
   (v0.11.23, v0.12.13). Pinned by
   `test_events_aggregator_uses_the_shared_helpers`, which fails the
   build if `events.py` regrows a local copy.

   Those helpers are VALUE-trusting but SHAPE-defensive: ints are
   assumed coerced upstream, while a non-dict day / model / skill bucket
   is skipped rather than raised. Both callers read from the on-disk
   cache, and a single malformed entry raising mid-merge would take down
   the whole events tail on EVERY push while the poisoned entry survives
   — a permanent outage from one bad key. Pinned by
   `test_malformed_nested_buckets_are_skipped_not_raised`.

5. **Pre-v0.12.15 entries are NOT force-re-walked.** Same reasoning as
   the D2 gate above: absence of `offset`/`head` is the version
   discriminator, and such an entry still HITS on matching size/mtime.
   It upgrades shape on its next real miss, so upgrade day costs one
   ordinary walk per actively-appended file — not a fleet-wide
   re-parse storm. Deliberately not a `CACHE_VERSION` bump. Pinned by
   `test_pre_v0_12_14_entry_hits_cache_without_rewalk`.

The walker reads in BINARY mode. Three reasons, all load-bearing: a
text-mode `tell()` cookie is opaque and not comparable against
`st_size`; `MAX_JSONL_LINE_BYTES` becomes a genuine byte cap rather
than a character cap; and `json.loads` on bytes raises `ValueError`
for both malformed JSON and invalid UTF-8, so one bad byte is skipped
as a line instead of raising `UnicodeDecodeError` out through the
whole events tail (a latent crash before v0.12.15). Pinned by
`test_invalid_utf8_line_is_skipped_not_fatal`.

Equivalence is the acceptance bar, not speed: `walk_jsonl_buckets` is
now a trimming shim over `walk_jsonl_segment`, and any change here
must keep merged-incremental output identical to a single full walk.
Verified pre-merge across 358 live session jsonls at four split points
each (1,432 cases, including mid-line cuts) and against the pre-change
walker over the same corpus — 7,848,746,853 tokens, zero drift.

One known, measured-absent divergence from a full walk: a final line
with NO trailing newline is treated as a partial write, so it is not
counted and the offset does not advance past it. If such a file is
never appended to again, that last line stays uncounted. The old
text-mode walker counted it. Under-counting one line is the safer
failure than the double-count the alternative invites, and the same
358-file corpus shows zero drift, so no live file hits it. Binary mode
likewise no longer splits on a lone `\r`; Claude Code writes `\n`.

Cold-cache walks are unaffected in shape (still O(total bytes)) and
remain owned by `_decide_token_walk_policy` + the 5s
`warm_token_cache_inline` budget.

**Cache file mode 0600 (lockedjson contract).** Identity data isn't
secret but is per-user. Mirrors `token_usage` and `upgrade-state`
cache permissions. Tests pin via `os.stat(...).st_mode & 0o777`.

**`aggregator.gather_author_emails()` is a thin shim** that delegates
to `identity.gather_local_identities()`. Backwards-compat preserved
for any out-of-tree library callers. Tests that previously
monkeypatched `aggregator._read_config_author_emails` /
`_per_repo_user_emails` / `_gh_noreply_email` now monkeypatch the
identity-side equivalents (`identity._gather_config_author_emails`,
`identity._gather_per_repo_emails`, `identity._gather_gh_noreply_email`).

**Conftest cache isolation.** `_isolate_identity_cache(monkeypatch,
tmp_path)` in `tests/conftest.py` redirects `identity.CACHE_PATH` to
a per-test temp file. Without it, test runs would pollute the user's
real `~/.config/mind-meld/identity-cache.json` AND read whatever was
previously cached there — non-deterministic. Mirrors `_isolate_pull
history` and `_isolate_devices_write_lock` pattern.

## `mm devices --format=json` (v0.11.0)

JSON formatter alongside the Rich Table renderer. Schema (stable contract for the retro-fleet aggregator's subprocess consumer):

```json
[
  {
    "device_id": "<str>",
    "device_name": "<str|null>",
    "last_seen": "<iso str|null>",
    "last_seen_version": "<str|null>",
    "is_self": <bool>
  },
  ...
]
```

Empty fleet returns `[]`. Sorted alphabetically by `device_id` for cross-platform stability (`list_devices` filesystem iteration is FS-dependent on Linux ext4 vs macOS APFS — without the sort, two peers walking the same fleet could produce different orderings). Plain `print(json.dumps(...))` — Rich injects styling that breaks the JSON contract. Pinned by `tests/test_devices_json.py`.

## Cost estimation — one predicate, honest degradation (load-bearing, v0.12.13)

**Read before touching `token_usage.PRICING`, `MODEL_FAMILY_TIERS`,
`resolve_prices`, `model_family`, `estimate_cost`, or the aggregator's
`_agent_row_cost` / `agent_row_floor_causes` / `_unpriced_token_summary` /
`_short_model_name`.**

**What went wrong.** The v0.12.12 card reported `~$3.37` for a 60-day
window whose real list cost was `~$11,015` — understated ~3,000x. Two
independent defects, which is why neither was caught by the other:

1. The entire Claude 5 family (`claude-opus-5`, `claude-sonnet-5`,
   `claude-fable-5`) plus `claude-opus-4-8` were absent from `PRICING`,
   and `estimate_cost` skipped unknown models silently. Only the Haiku
   subagent traffic was priced — that `$3` *was* the whole basis.
2. `claude-opus-4-7` / `-4-6` / `-4-5` were listed at `$15/$75`, which
   are Opus **4.1** rates. Those models are `$5/$25`. Not staleness — a
   wrong value from day one, never validated.

The two errors push in opposite directions, so whether the card was over
or under depended entirely on the window's model mix. A window heavy on
4.7 over-reported; one heavy on Claude 5 under-reported by orders of
magnitude. That is worse than being consistently wrong.

**Invariant 1 — `resolve_prices` is the ONLY predicate for "is this model
priced."** Both consumers go through it: `estimate_cost` for the rates,
`aggregator._unpriced_token_summary` for the `is None` count. Before
v0.12.13 each ran its own `model in PRICING` test. That duplication is
benign only while the answer is a plain dict lookup — the moment
family-tier fallback landed in one of them, the cost line would price
`claude-opus-6` while the Notes line two rows down still called it
unpriced. Do NOT reintroduce a second membership test. This is the same
failure shape v0.11.23 already fixed once, when top-level token totals
and `estimate_cost` computed the same basis independently and drifted.

**Invariant 2 — family-tier fallback, and the inaccuracy it accepts.**
Exact `PRICING` entry wins; otherwise the family segment resolves against
`MODEL_FAMILY_TIERS`; only genuinely unparseable ids stay unpriced. This
exists because the failure that actually happens is a model *launch*, not
a rate change. The accepted cost: tiers carry CURRENT-generation rates, so
a retired model prices wrong (Opus 3 was `$15/$75`, would resolve to
`$5/$25`). Acceptable because retired models don't appear in live session
data, and being ~3x wrong on a model nobody runs beats being infinitely
wrong on the model everybody runs. Do not "fix" this by adding retired
models to `PRICING` unless they actually show up in fleet data. Pinned by
`test_retired_model_prices_at_current_family_tier`.

**`PRICING` is an OVERRIDE table.** It shipped empty at v0.12.13's first
draft. After Track 58A it carries Opus 4/4.0/4.1, Sonnet 4/4.0/4.5/4.6,
and Fable/Mythos 5.1 cards
whose rates differ from the family fallback. Date-suffix normalization makes
Opus/Sonnet `-4` reachable; the published `-4-0` aliases remain reachable too.
A per-model entry that *duplicates* its family recreates the
multi-site drift this release removed: an Opus rate change would need N
identical edits plus the tier, and missing one would silently price
some models at the old rate. Add an entry ONLY when a model permanently
departs from its tier; `test_pricing_holds_no_redundant_entries` fails
the build if an entry duplicates its family. Sonnet 5's `$2/$10` is now
standard pricing, verified 2026-09-14;
the scheduled `$3/$15` increase was cancelled. Sonnet 4.x retains `$3/$15`
overrides. Fable/Mythos 5.1 uses a 0.025x cache read ($0.25/MTok), while
5.0's family tiers remain at 0.1x ($1/MTok), preserving the conservative
error direction for a future unverified id.

Host model ids (Track 35A) do not go through `model_family`. They resolve
via the curated alias registry `PRICING_FAMILY_BY_MODEL` (exact-key,
never substring) onto `VENDOR_FAMILY_TIERS` literal four-field cards.
Do not reach for `_tier` for non-Anthropic rates: its cache multipliers
are Anthropic-specific. `resolve_prices` stays the single priced-predicate.
Track 57A lifts the Grok hold WITH delivery: the 46A reader repair had never
delivered Grok tokens to the wire (zero across all five local host rows).
The shared deadline and one-reader warm caused starvation. The later-reader
grace floor and every-reader warm/retry ship with the rate table.

**Grok pricing floor and model-scoped ceiling (57A).** Only the exact observed
`grok-4.6-build` alias maps to `grok-4.6`. Bare `grok-4.6`, `grok-build-0.1`,
prefix/suffix variants and case variants stay unpriced. The literal base card
is $2 input / $0.50 cached input / $0 cache writes / $6 output per MTok;
the long card is $4 / $1 / $0 / $12. Requests whose prompt reaches 200k
tokens pay the higher rates for ALL tokens in that request. Aggregate
counters cannot reconstruct request sizes, so the unified row with in-window
Grok tokens renders `≥`. `_cost_under(card, usage)` owns the arithmetic;
`estimate_cost` keeps its signature, priced-predicate calls and warnings.
`resolve_long_context_prices` returns a copy through the same exact alias
registry and is not a priced-predicate. Every xAI family must have a long card.

The inherent cause says: "Grok's logs do not record per-request prompt sizes;
no action resolves this". The base and at-most values cover **this model's
recorded tokens, in token charges; server-side tool fees excluded**. Never
average them or present a machine-level range. The agent row sums usage across
machines; the cause's at-most values describe only their stated recorded-token
basis and must not be promoted to a complete fleet billing range. Omit the at-most
figure if ANY snapshot reader is partial or degraded (the wire cannot map
readers to models), or that model has nonzero `cache_create`. A cache-write-only
bucket is still `>=` and must never claim "at most $0". Missing, stale or
legacy-counter observations retain their unavailable paths: missing or stale
snapshots contribute no usage, while a contributing legacy-counter snapshot
makes its agent row's token and cost cells `—`. No in-window Grok tokens
means no inherent cause. Unpriced models name the rendering-Mac remedy:
upgrading mm there may add a rate; republishing cannot; do not estimate.
At-most figures round upward so display rounding cannot understate the bound; `_format_usd(bound=...)` rounds floors down, ceilings up and estimates
to nearest, including on both sides of the $100 precision transition.

**Evidence, 2026-09-14.** Installed Grok 1.0.30: 188 ledgers, 390.6 MB,
312 terminal records, 308 with usage, 4 usage-less, 4 incomplete. Corpus span
2026-08-17 → 2026-09-14; separately, 33 post-1.0.25-census terminals retain
the same `elapsed_ms` shape. Four terminal key sets (126/182/3/1), two usage
key sets, one model (`grok-4.6-build`), no multi-model turns or nonzero cache
writes. The 18 August 14 records seen September 4 are gone; no retention
policy is inferred. The pin is the census host version, not a compatibility
proof across intervening releases; CONTRACT keeps earlier fixture provenance.
The real reader returned complete, 12 days, 3 partial days, 1.94s cold.
The prior 2026-09-10 census used 1.0.25 (185 ledgers).
https://docs.x.ai/build/overview establishes the model identity/rate level;
https://docs.x.ai/developers/models/grok-4.6 establishes the rate cards and
threshold. The 303-turn `costUsdTicks` census corroborates the rate shape
(modal 1.700e9 ticks per list-USD; 77 turns exactly double), but the CLI
field is 0.17x list at the documented API unit of 1e10 ticks/USD. Never
decode it or use a census-calibrated constant to infer request tiers.
`grok-build-0.1` is a different SKU and a name trap.

**Other vendors' context assumptions.** Codex's observed 258,400-token window
cannot reach OpenAI's >272K prompt threshold: 20,955 events across 789
rollouts, max observed input 244,361. Codex's `~` rests on that assumption;
the window is configurable and absent from the wire, so a tripwire is deferred
in TODOS. Anthropic 4.6+ bills the full 1M context at standard rates. The
Anthropic rate refresh landed in Track 58A, verified 2026-09-14; 57A's
host delivery and model-scoped at-most contract remain in force, with the
details carried by `cost_floor` in `MM_HEALTH`.

Provenance is per vendor, because one date over two vendors' tables is
a lie of composition. Anthropic: `PRICING_LAST_UPDATED` (verified
against Anthropic's public pricing page). OpenAI:
`PRICING_OPENAI_LAST_UPDATED` (verified against
https://developers.openai.com/api/docs/pricing, short-context Standard).
xAI: `PRICING_XAI_LAST_UPDATED` (2026-09-10, model page and Build overview
above). OpenAI's five cards, including `gpt-6-astra` ($10 / $1 / $12.50 / $50),
were all re-read 2026-09-10. Each vendor's date and source URL are carried in
`MM_HEALTH.pricing`, alongside the marker definitions. "Current rates" means rates
bundled with this mm release, verified on those dates. mm does not fetch rates.

**Publication-proof release evidence.** After merge and upgrade on the
Grok-producing Mac, use the attended recapture bridge or a substantive push;
observe the Grok warm notice when cold. On a SECOND Mac, `mm pull` then
`mm retro-fleet 7d --dump-host-usage` must show a new `as_of`, Grok in
`consulted`, and nonzero `grok-4.6-build` counters. The retro must name its
inherent floor. After the next real capture, require an advanced `as_of` with
Grok still consulted. Cache success alone never proves publication. Upgrade
the producing Mac for delivery, the rendering Mac for prices, and refresh
the agent with `mm install-skills` plus restart for the decoder.

**Track 58A presentation and floor arithmetic.** `VERIFIED_MODEL_IDS` is a
separate explicit set covering every override and currently verified family
id, plus the curated vendor ids at their respective dates. It does not gate
pricing. A priced id outside it gets a bounded, sanitized "Models priced by
family extrapolation" detail under `pricing_extrapolated` in `MM_HEALTH`,
never an unpriced label.

`floor_prices` lives beside `resolve_prices`: Anthropic uses the minimum 5m
cache-write rate (1.25x input), vendor literal cards are unchanged. One floor
condition makes **every priced model in that agent row** use floor cards;
`_section_costs` computes models and total from that same basis. Since 1.1 host tokens are summed
fleet-wide per agent rather than kept as per-machine subtotals; see the
unified-agents renderer contract for the possible-overlap detector and its
limits. Floors are computed per AGENT row. Model-specific pricing causes stay
with their family; partial/failed-reader coverage applies to every family
because the missing models cannot be attributed. The markers are `~` estimate, `≥` floor of
a priced subtotal under bundled assumptions — **not a guaranteed billing
minimum** — and `—` unavailable, never zero. Estimates may extrapolate. Claude
coverage gaps floor the Claude row. The five-line `RATE_MARKER_LEGEND` no
longer renders; the same definitions live in `MM_HEALTH`'s `pricing.basis`.
`tokens_incomplete` names missing Claude coverage and its remedy; `cost_floor`
names an available priced lower bound, while `cost_unavailable` names a missing
priced basis. Neither claims to measure pre-v2 omissions.

`window_bounds` rejects instant-stale snapshots before inclusive day slicing;
`contributes_in_window` is shared by the unified rows, host-only coverage view,
and duplicate detector. `as_of` before `since` but on the first UTC day
contributes nothing to any of them. The retained inventory survives for
forensics, stale snapshots contribute no window usage, and exact
`as_of == since` plus the accepted 24h future clamp keep their semantics.

The 1.1 renderer has one `## Agents` table and one card `AGENTS` block.
Each row reports the same token basis, observed days, contributing machines,
and estimated cost; the body also names the top model. Model ids pass through
`_safe_short` and bounded labels. There are no Claude-only counter tables,
per-machine economics tables, or separate source-scope footers. Model-specific
at-most details, pricing provenance, and named floor causes live in
`MM_HEALTH`. Goldens exercise 80 and 120-column terminals.

Fast-mode turns on Opus 5 / 4.8 bill at 2x and are priced here at standard rates.
D1 = B: disclosure only. Local evidence, 2026-09-14: zero fast rows among
22,042 carrying `speed`, 4,297 lacking it (absence is not chronology). No
detector, cache field or wire flag is added; exposure elsewhere is unknown.
Producer, renderer and decoder upgrades are separate, documented in README.
Health issues retain the relevant caveat and remedy information. Codes are the
skill interface: the AST test requires every literal `note(<code>, ...)` code
in SKILL.md, and the skill reports an unfamiliar code verbatim rather than
inventing an interpretation. `SKILL_MIN_VERSION` gates the local
renderer separately from the peer capture floor `ATTENDED_USAGE_MIN_VERSION`.

**Invariant 3 — `model_family` matches POSITIONALLY against a literal
allowlist, never by substring.** Model ids are peer-controlled (peer's
Claude Code jsonl → mm-events → this machine's aggregator) and as of
v0.12.13 they drive a pricing decision, not just display. A substring
test would bill a planted `claude-haiku-opus-4-5` at Opus rates. This
mirrors `storage/keys.py`, which validates components at construction
rather than trusting shape. Pinned by
`test_token_usage.py::TestModelFamily::test_positional_match_not_substring`.
An id scheme that stops fitting `claude-<family>-...` degrades to
unpriced, which is the safe direction — silence is what this fixed.

**Invariant 4 — never print a confident total over incomplete data.** Any
unresolvable model in the window makes any priced subtotal a floor (`≥`);
an entirely unpriced row keeps its cost unavailable (`—`). The stderr
breadcrumb in `estimate_cost` is NOT sufficient on its
own: it fired for four unpriced models across the whole v0.12.x line and
nobody saw it. The load-bearing signals are the rendered ones — the `≥`
marker and its `MM_HEALTH` issue.

**Invariant 5 — `PRICING_LAST_UPDATED` is provenance, not a
threshold.** mm has no network by design (CLAUDE.md: "No API server"), so
this table can never self-update and **stale is the steady state, not the
exception**. The old docstring said "refresh if more than ~6 months old";
nothing read it, and it would not have helped — the table was three
months into that window while wrong about four models. The date is now
carried in `MM_HEALTH.pricing` so the skill can cite it when relevant.
Do NOT reintroduce a "warn after N months" rule: it is a verdict the code cannot earn, and it
is the count-based-threshold anti-pattern this project has rejected
before.

**Invariant 6 — `_short_model_name` handles BOTH id shapes, gated on the
family allowlist.** The pre-v0.12.13 version required 4 dash-segments, so
every 3-segment Claude 5 id (`claude-opus-5`) fell through to the raw
string and rendered beside a prettified `Opus 4.8` on the same line.
`parts[2:4]` joined by `"."` yields `4.7` for a 4-segment id and `5` for a
3-segment one with no branch.

Relaxing the count to `>= 3` is NOT sufficient on its own, and the first
draft shipped that bug: legacy ids put the version where the family
belongs, so `claude-3-opus` rendered as `3 opus` and `claude-3-5-sonnet`
as `3 5.sonnet`. Those ids are reachable — `_normalize_model_id` strips
the `-YYYYMMDD` suffix, so `claude-3-opus-20240229` arrives 3-segment.
The prettify branch therefore also requires `token_usage.model_family(m)
is not None`, which keeps token_usage the single owner of "what is a
family" (this module owns presentation only) and makes anything
unrecognized fall through to the raw defanged string. Pinned by
`test_legacy_id_shapes_are_not_mangled`.

**`model_family` is exported for RENDERING, and is not a priced-
predicate.** `model_family(m) is not None` reads like one and is subtly
wrong — it disagrees with `resolve_prices` for any id carried by a
`PRICING` override. Invariant 1 still stands: `resolve_prices` is the
only correct answer to "is this priced."

**Known approximation (deliberate).** `_CACHE_WRITE_MULT` is `2.0` (the
1h-TTL rate). Anthropic bills 1.25x for 5m and 2x for 1h; the synced wire
format carries one `cache_create` total with no TTL split. Measured on a
24-file local sample: 83% of cache-write tokens were 1h. So 2x overstates
by ~+3.5% of a window's total where 1.25x understated by ~11%. The exact
split IS available in the jsonl
(`message.usage.cache_creation.ephemeral_{5m,1h}_input_tokens`) but
reading it is a wire-format change with a mixed-fleet discriminator —
tracked in `TODOS.md`, deliberately out of v0.12.13's scope.

**Peer token counts are clamped at the trust boundary.**
`aggregator._safe_int` bounds every synced token field to
`[0, _MAX_SAFE_TOKENS]` (`2**53`, the largest float64-exact integer).
Two distinct failures motivate it, both widened by family-tier fallback
because the reachable id set grew from five hardcoded ids to any
`claude-<family>-*`. A 400-digit integer survives `json.loads` and then
raises `OverflowError: int too large to convert to float` inside
`estimate_cost`'s multiply — nothing between there and
`cli.retro_fleet_cmd` catches it, so `mm retro-fleet` dies with a
traceback. And a negative count silently subtracts from the fleet total,
letting one bad peer shrink the cost line or push it to zero and
suppress it entirely. Do not relax these clamps to "preserve" peer
values; a token count outside this range is already garbage. Pinned by
`TestPeerTokenClamping`.

**Not fleet-skewed.** Pricing is applied locally at render time, from
`tokens_by_model` carried over the wire. Fixing the local table fixes the
card immediately, even with other machines on older mm. No peer migration.
