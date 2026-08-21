# Events / fleet retro — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/cli.py` — `install_skills_cmd` / `retro_fleet_cmd` / `refresh_identity_cmd` / `devices` (`--format json`) / `status` / `diag` / `_collect_diag_state` / `PushResult.events_degradations` / `_breadcrumb_staleness_suffix`
- `src/mind_meld/events_tail.py` — `_run_events_tail` / `_run_events_backfill` / `_decide_token_walk_policy` / `_enabled_claude_paths` / `_capture_host_usage` / `_default_host_readers` / `_host_skip_phrase` / `_warm_host_cache_with_notice` / `HostUsageCapture` / `HOST_USAGE_READ_BUDGET_*` / `WARMABLE_HOST_READERS`
- `src/mind_meld/skill_link.py` — `_ensure_retro_skill_link*` / `_skill_link*_check_due*` / `_resolve_retro_skill_src` / `_skill_store_dir` / `_publish_skill_store` / `_prepare_store_dir` / `_should_publish` / `_store_needs_refresh` / `diagnose_skill_links` / `render_skill_status` / `BROKEN_SKILL_STATUSES` / `_emit_status_notice` / `_marker_dir` / `SKILL_ROOTS`
- `src/mind_meld/retention.py` — `EVENTS_RETENTION_DAYS` / `CONFLICT_AGE_DAYS` / `_gc_old_event_files` / `_gc_old_conflict_files` / `_gc_token_cache` / `_sweep_local_tmp_files`
- `src/mind_meld/events.py` — `MmPushEvent` / `make_mm_push_event` / `walk_session_metadata` / `walk_git_projects` / `discover_git_roots` / `last_push_ts` / `EVENTS_SCHEMA_VERSION` / `WALK_TIME_BUDGET_*` / `HostUsageSnapshot` / `make_host_usage_snapshot` / `HOST_USAGE_TOKEN_SOURCES`
- `src/mind_meld/host_usage.py` — `read_codex_usage` / `read_grok_usage` / `grok_completed_once` / `warm_host_cache_inline` / `_scan_codex_root` / `_scan_grok_root` / `_read_rollout` / `_carries_usage` / `_no_ledger_entry` / `_NoCacheCommit`
- `src/mind_meld/identity.py` — `gather_local_identities` / `refresh_identity_cache` / `CACHE_PATH` / `TTL_SECONDS`
- `src/mind_meld/skills/retro_fleet/aggregator.py` — `aggregate` / `aggregate_local_emails_from_events` / `aggregate_git` / `aggregate_sessions` / `aggregate_host_usage` / `_accept_host_usage_snapshot` / `gather_author_emails` / `_emit_custom_path_notice_if_due`
- `src/mind_meld/config.py` — `MM_INTERNAL_SOURCE_NAMES` / `_bootstrap_mm_events_path` / `DEFAULT_SOURCES`
- `src/mind_meld/token_usage.py` — `walk_session_metadata` token-cache wiring

Tests: `tests/test_events.py`, `tests/test_identity.py`, `tests/test_init_events_backfill.py`, `tests/test_gc_events.py`, `tests/test_retro_fleet_aggregator.py`, `tests/test_skill_link.py`, `tests/test_devices_json.py`, `tests/test_token_usage.py`, `tests/test_host_usage.py` (readers), `tests/test_host_usage_snapshot.py` (capture policy).

---

## `mm-events` default source + bootstrap (load-bearing, v0.10.1)
`DEFAULT_SOURCES` has a new mm-owned synced source for the per-device daily JSONL event log Group 8's `retro-fleet` skill will read.

```toml
{ name = "mm-events", path = "~/.local/share/mind-meld",
  type = "generic", include_dirs = ["events"], exclude_patterns = [] }
```

Subdir nesting (`include_dirs = ["events"]` rather than `["."]`) plays cleanly with `walk_generic_source` and avoids the `pathlib`-`["."]` quirk. Per-device daily JSONL files land at `events/<device>-<YYYY-MM-DD>.jsonl` under this base path.

`get_sources()` runs a one-shot bootstrap dispatch BEFORE the path-existence filter so mm-internal sources don't fall through as "doesn't exist" on first run. Dispatch table: `{"mm-events": _bootstrap_mm_events_path}`. Adding a new entry to `MM_INTERNAL_SOURCE_NAMES` REQUIRES adding the parallel bootstrap entry here — the dispatch by name keeps the mapping explicit and prevents silent inconsistency between `_prompt_sources` auto-include and bootstrap. Bootstrap is mode 0o700 (events contain device IDs and per-machine activity metadata — not user-secret but per-machine-private). mkdir failures emit `mm: warning:` per the visible-failure contract; the source then drops via the path-existence filter that runs after.

**Warn-once on bootstrap failure (Group 7 hotfix).** `_bootstrap_mm_events_path` keeps a module-level `_BOOTSTRAP_WARNED_PATHS: set[str]` of paths whose mkdir has already failed in this process. First failure emits `mm: warning:` (preserves the visible-failure contract — monitoring catches the wedge); subsequent `get_sources()` calls in the same process short-circuit before mkdir + stderr. Without this, chmod-restricted-home users would see warning spam on every read-only command (`mm sources` / `mm status` / `mm conflicts` / `mm diff` / `mm log` all call `get_sources()`). Per-path keying (not per-process) preserves the contract for the unlikely case of two failing mm-internal source paths. Tests touching the failure path must reset via `monkeypatch.setattr(config, "_BOOTSTRAP_WARNED_PATHS", set())`.

## `MM_INTERNAL_SOURCE_NAMES` + init contract (v0.10.1)
`frozenset({"mm-events"})` in `config.py` enumerates source names that are mm-owned infrastructure, not user-prompted. Two consumer sites:

1. **`_prompt_sources` (init):** mm-internal entries auto-include without a Y/n prompt — they're mm-owned infrastructure for fleet-wide features (retro-fleet) and shouldn't burden the init UX with a question whose only legitimate answer is "yes." Per-machine opt-out remains via `mm disable-source mm-events` post-init (v0.10.0).
2. **`init_cmd` no-sources guard:** an init that produces only mm-internal sources fails the `user_facing_sources` check (refuses with the same "no sync sources enabled" error as the pre-Group-7 zero-sources case). A config with only mm-events is effectively "user wanted nothing synced" — push/pull would silently no-op for the user's own data; better to refuse and let them re-run.

Adding a new mm-internal source name requires updating the frozenset AND the bootstrap dispatch in `get_sources()` AND (if it has a meaningful per-machine state) wiring `mm disable-source` strict-mode allowance. Keep the set small — every entry sidesteps the init-prompt UX, so only mm-owned synced infrastructure qualifies (today: events).

## Events tail in `_push_core` (load-bearing, v0.10.3, gated v0.12.2)

Track 7B wires `events.py` (Track 7A foundation, v0.10.2) into the push hot path. `_run_events_tail(config, sources, device_id, *, dry_run, quiet)` runs from `_push_core` AFTER the substantive-change gate (v0.12.2): `build_manifest_v2` walks all sources first, the diff against the recovered remote manifest is computed, and only when `iter_source_diffs(local_manifest, remote_sources, skip_unchanged=True)` yields at least one entry (or `fetch.status == "corrupt"`) does the events tail fire. After firing, `_push_core` re-walks the `mm-events` source and folds the just-written row into `local_manifest["sources"]` so it ships in the same push (no one-push lag). Four invariants govern the wiring:

1. **Single call site, gated on substantive change (Codex C4 + v0.12.2).** Pre-v0.12.2 the tail fired at the HEAD of `_push_core` unconditionally (the original Codex C4 fix replaced inline-before-each-early-return with a head-position single call to avoid branch fragility against the "must run on every push attempt" trust boundary). v0.12.2 relaxed the trust boundary to "must run on every push that uploads bytes" — empty pushes were writing a `mm-push` event row, mutating the mm-events file, and reporting "1 file uploaded" forever as the only "change" pushed (phantom-change-on-empty-push regression). The gate counts diffs across ALL sources (user + mm-events), so an un-flushed prior-push event row from a partial-upload failure still triggers a fresh push that drains it. Do NOT add additional call sites; do NOT move the tail back to HEAD.

2. **`dry_run` no-op (preview contract).** `mm push --dry-run` must not mutate disk. The events tail returns immediately when `dry_run=True`, mirroring `_ensure_device_registered`'s same gate (codex review 2026-04-25).

3. **`mm-events`-resolved gate, NOT `disabled_sources` (Codex C1).** The gate is `next((s for s in sources if s.get("name") == "mm-events"), None) is not None`. This covers fresh / migrated / un-migrated configs uniformly: a config that pre-dates v0.10.1 simply has no `mm-events` entry and the tail no-ops, no migration prompt required. Gating only on `disabled_sources` would let pre-v0.10.1 configs accumulate local cruft forever (the `~/.local/share/mind-meld/events/` tree never created, never written). The `_bootstrap_mm_events_path` dispatch in `get_sources()` ensures fresh configs land here with the path materialized.

4. **Wall-clock budget (Codex C4 + C5; walk-scoped v0.12.9).** `WALK_TIME_BUDGET_AUTOPUSH_MS` (250) for `quiet=True` (autopush hook), `WALK_TIME_BUDGET_INTERACTIVE_MS` (500) for interactive `mm push`. The deadline is plumbed through to `walk_session_metadata` via the new keyword-only `deadline_monotonic` param — `_read_cwd_from_latest_jsonl` reads jsonl line-by-line until a `cwd` field appears, so a single pathological project can blow the budget without per-project deadline checks. The `mm: notice: events tail budget exceeded` notice reports specifically on the **session-metadata walk**: `deadline` is reset (events_tail.py) AFTER `walk_git_projects` runs, so the git walk is NOT in this comparison — it self-bounds via its own `total_budget_ms` arg and truncates silently. The check compares a `walk_done = time.monotonic()` snapshot captured the moment the session walk finishes against `deadline`, NOT a post-write `time.monotonic()`. **Load-bearing (v0.12.9):** the snapshot MUST precede `identity.gather_local_identities(allow_refresh=True)` (events_tail.py), whose cold path runs a synchronous refresh bounded by its OWN timeouts (`identity._GIT_GLOBAL_TIMEOUT_S` + `_PER_REPO_BUDGET_S` + `_GH_TIMEOUT_S` ≈ up to 10s, 7d TTL). Pre-v0.12.9 the check sat after the gather + write, so a routine cold identity refresh tripped the walk-budget notice even though the walk itself finished in ~200ms — a misleading signal (the gather emits its own `refreshing identity cache (one-off)` line; the two concerns are now orthogonal). `_run_events_backfill` mirrors this exactly: its `walk_done` snapshot precedes the deliberate `refresh_identity_cache(force=True)` init warm, which ALWAYS runs. Do NOT move either snapshot back below the identity gather. The notice remains a visible-failure-contract signal (the push/init proceeds regardless).

**Git-root discovery budget and reuse (Track 18C).** Root discovery has its own cooperative deadline before the independent git/session walks: `ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS` (50) for autopush and `ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS` (100) for interactive push and init. `events.discover_git_roots()` returns one frozen `GitRootDiscovery(roots, errors, exceeded)` that remains compatible with `roots, errors = ...`; capture retains that exact object and passes it to a cold tail gather or forced init identity refresh so one invocation never repeats the root probes. Explicit `[retro].repo_roots` validate before automatic gstack/Claude probes. The deadline is checked before every registry entry, JSONL file/line, and git-root validation; `git rev-parse` receives only the positive remaining time. It is cooperative rather than a filesystem-interrupt guarantee: a system call already running may finish, but no later discovery step starts after expiry. Preserve successful roots and append the stable `git root discovery exceeded its time budget` forensic error with `exceeded=True`; never report it as a clean no-repositories result.

**Incomplete identity discovery never refreshes cache.** When the supplied `GitRootDiscovery` is incomplete, identity may return `cached identities ∪ newly gathered identities` to that event only, under the phase-3 cache lock. It MUST NOT write cache bytes or `refreshed_at`, even for `refresh_identity_cache(force=True)`: persisting that union would make identities removed from config or a repository survive as trusted local identities. A later complete refresh remains authoritative and may prune them.

**No content heartbeat.** The substantive-change gate remains authoritative across UTC rollover: a no-op push writes no `mm-push` row, does not advance the retro cursor, and must not create a daily event file merely to express liveness. The next substantive push uses the old cursor and captures the idle interval. A no-op `autopush` can still refresh its local `last-autorun.json` success breadcrumb; that means the hook ran, **not** that fleet retro received activity. Liveness needs a separate signal if it is ever required.

**Forensic-only invariant, and why stderr is NOT the load-bearing signal (v0.12.16).** The whole block is wrapped in `try / except Exception`; failures emit `mm: notice: events tail failed: <type>: <safe_str(msg)>` to stderr and the push continues. `safe_str(e)` defangs peer-controlled escapes per the v0.10.1 sanitization invariant (a corrupt peer manifest could otherwise smuggle ANSI through an exception's `__str__`).

That stderr line is the *interactive* signal only. `_run_events_tail` runs from `mm autopush`, which fires unattended from a Claude Code hook, so its stderr reaches nobody — and pre-v0.12.16 `autopush` wrote `_write_autorun_breadcrumb("push", "success")` unconditionally, so `mm status` reported success no matter how badly the retro pipeline had degraded. **`_run_events_tail` therefore RETURNS `list[str]`** — one human-readable phrase per degradation, empty when healthy — which `_push_core` carries on `PushResult.events_degradations` and `autopush` turns into `_write_autorun_breadcrumb("push", "degraded", "; ".join(reasons))`. This mirrors the `degradations` list `autopull` has carried since v0.8.1, and it is the same argument CLAUDE.md already makes for the `no-sources` breadcrumb: without it, `mm status` only ever sees `success` and monitoring built on top of it never catches the wedge. Four conditions populate the list today: whole-tail exception, session-walk budget exceeded, token cache cold (tokens + skills omitted), and root discovery time-budget expiry. The root-discovery text is fixed: `git repository discovery hit its time budget: this retro capture may omit repositories. A later substantive push will retry`; it contains no paths or raw probe errors or the `; ` separator used between breadcrumb reasons. Init backfill has no `mm-push` row or autorun breadcrumb, so it prints the equivalent `initial retro capture` notice only. **Any new degradation detected in the tail MUST be appended to the returned list as well as printed** — a `mm: notice:` with no corresponding entry is invisible to the only surface the user actually reads. CHANGELOG v0.12.13 records the cost of getting this wrong: the unpriced-model breadcrumb "fired for four unpriced models across the whole v0.12.x line and nobody saw it." Pinned by `test_silent_failure_contract.py::test_autopush_breadcrumb_degraded_when_events_tail_fails`.

**Tolerant binary reads across every jsonl reader on the push path (load-bearing, v0.12.16).** `_read_cwd_from_latest_jsonl`, `_last_mm_push_ts`, `token_usage.is_cache_cold`, and `pullhistory._yield_lines` all read BINARY and tolerate a bad line rather than a bad file. Text mode decodes in ~8 KB **chunks**, not per line, and `UnicodeDecodeError` is a `ValueError` — NOT an `OSError` — so the `except OSError` these functions carried never caught it and one invalid byte took down the entire events tail on every push. (Chunked decoding is also why a `cwd` on line 1 did not protect against a bad byte on line 2; measured, it raises at 2 lines apart and returns cleanly at 80 KB apart.) `json.loads` accepts bytes, and both malformed JSON and invalid UTF-8 surface as `ValueError`, so the guard is `except ValueError: continue` per line. Two traps:

- **A reader whose bytes can come from a peer MUST be bounded, not merely tolerant.** `open(path, "rb")` + `for line in fp:` lets Python extend its buffer to newline-or-EOF, so one pathological line kills the push — the OOM `token_usage.iter_bounded_lines` exists to prevent. Which readers need it follows from where the bytes originate, so the rule is per-corpus, not blanket:

  | Reader | Corpus | Bounded? |
  |---|---|---|
  | `_read_cwd_from_latest_jsonl` | Claude Code session jsonls | **Yes** — `iter_bounded_lines` |
  | `_last_mm_push_ts` | `mm-events` daily files, **synced**, peer bytes arrive via the pull apply path | **Yes** — `iter_bounded_lines` |
  | `token_usage.is_cache_cold` | local token cache | N/A — whole-file `read_bytes` behind an `st_size` gate, no line iteration |
  | `pullhistory._yield_lines` | `~/.config/mind-meld/pull-history.jsonl` | No, and that is fine — the config dir is **never synced** and the file is written only by `pullhistory.append`. Tolerant reading is the requirement here; bounding would buy nothing and would import `token_usage` into a forensic-log reader for no gain. |

  `iter_bounded_lines` is public since v0.12.16 precisely because it grew consumers outside `token_usage`. Its `label` kwarg names a call site in the oversize notice, so do not hardcode "token walker" back into it — but note the notice is deduped by PATH ONLY, so when two sites read the same file the label shown is whichever reached it first.

- **One-shot readers MUST pass `yield_final_partial=True`.** `iter_bounded_lines` defaults to discarding a trailing chunk with no newline, because for `walk_jsonl_segment` that is a partial write to re-read on the next push. A one-shot reader has no next push: the default silently drops a complete-but-unterminated final record. Caught by Codex adversarial review during `/review`, after the first fix had already landed — porting the cwd reader without the flag made it return `None` for a session whose only line was not newline-terminated yet, where the old text-mode reader returned the cwd. Pinned by `test_unterminated_final_line_is_still_read`.
- **`_last_mm_push_ts` returning `None` is NOT a benign fallback.** It rewinds the cursor to `now - INITIAL_CURSOR_LOOKBACK_DAYS` and re-walks 30 days of git history on every subsequent push, forever. Its pin asserts the timestamp comes back, not merely that nothing raised.

(`conflictlog.read_records` used to be listed here as a deliberate exclusion. `conflictlog.py` was removed in Track 16A; the exclusion is moot.)

**One cwd scan per project (load-bearing, v0.12.16).** `_read_cwd_from_latest_jsonl` takes the PROJECT dir and scans every jsonl in it. `_scan_one_project` calls it EXACTLY ONCE, after the `sessions == 0` early return. It previously sat inside the per-file loop guarded by `if cwd is None` with a comment claiming "first one wins" — true only when a `cwd` was actually found. When no jsonl in the project carries one, the guard never flipped and the helper rescanned the whole directory once per jsonl: N calls x N files. Measured pre-hoist: 20 jsonls → 400 file opens; 15 ms at N=10, 1.44 s at N=100, **13.2 s at N=300 for a single project** against a 250 ms autopush budget. Note the binary port makes this WORSE if the hoist is ever reverted — today's raise is what truncates the quadratic walk, so a tolerant reader without the hoist runs it to completion on every push. Pinned by call-count (`test_no_cwd_anywhere_scans_the_project_exactly_once`), not by wall clock. The measured cost of the scan itself is 0.82 ms across 37 project dirs (0.33% of the autopush budget), so it needs no deadline of its own — the per-project deadline check at the `walk_session_metadata` loop head is the bound.

**`walk_git_projects` emits each repo at most once (v0.12.16).** The `as_completed` pump and the `FuturesTimeoutError` budget-abort handler are two DIFFERENT except sites and are not interchangeable: the inner one guards `fut.result()` per future, the outer guards the pump and is the only one that marks `budget_abort`. But the abort handler iterates all of `futures.items()`, so without a `collected: set[Future]` it re-collected every future the pump had already drained — every completed repo's full commit list serialised twice into the row, then gzipped, encrypted, uploaded, and replicated to every peer. Measured pre-fix with 4 roots / 2 slow / 300 ms: 4 project rows, 2 unique. `aggregate_git` dedups on `(canonical_remote, sha)`, which is why the card never showed it. Do NOT collapse the two blocks into a shared helper without carrying the set — that refactor preserves the bug and hides it. The pre-existing budget pin makes ALL repos slow (`projects == []`) and structurally cannot catch this; the regression pin uses a MIXED fast/slow set.

**`MmPushEvent.sources` schema is `list[str]` (names only) — Codex C2 + C7.** `iter_source_diffs(skip_unchanged=True)` drops unchanged sources from the diff loop, breaking per-source counts on the no-content push path. The retro-fleet skill (Group 8) reads per-source content stats from the synced manifest at retro time, not from the event row. `make_mm_push_event` filters `MM_INTERNAL_SOURCE_NAMES` from the names list — `mm-events` is mm-owned infrastructure, not user-meaningful fleet activity.

**Fleet retention via tombstone propagation (Codex C10).** `_gc_old_event_files` reaps day files older than `EVENTS_RETENTION_DAYS` (90). The retro skill reads the synced manifest, so deletion fans out fleet-wide via the existing tombstone path: this device unlinks → next push generates a tombstone → all peers drop their copy on pull. An offline peer that comes back online sees the tombstone too, suppressing resurrection.

**Reap by FILENAME date, NOT mtime (Codex C5, C6).** iCloud restores can rewrite mtimes back to "now" while the filename date (`<device>-YYYY-MM-DD.jsonl`) is intrinsic to the event-day boundary the file was written for. The mm-events path resolves through `get_sources(config)` so user-customized paths are honored. Always-on (no `--events` flag) — events retention is fleet policy.

**Retention dry-runs are plan-only (Track 17D).** Every retention reaper selects candidates before applying I/O. `mm gc --dry-run` uses that same selection but must not unlink a file, write a cache, or change metadata, and it prints one stable result line for every reaper it executes. The token-cache plan reads under `lockedjson`'s shared read-only snapshot; apply re-plans under the exclusive R/M/W lock so a preview never leaks a stale plan into a write. Failed deletes count as failures, not cleanup, and one best-effort failure never prevents the other reapers or orphan-blob GC from continuing.

**Initial cursor lookback (Codex C9).** `last_push_ts(events_dir, device_id)` returns `now - INITIAL_CURSOR_LOOKBACK_DAYS` (30) when no prior `mm-push` event exists. New fleet members joining mid-quarter scan back 30 days of git history; older context is invisible to retro until a manual backfill. Document the bound in skill output: "First-run window: last 30 days of activity. Older history is intentionally outside the retro window."

## Host-usage snapshot capture (load-bearing, Track 19A)

The tail publishes the local Codex / Grok / OpenCode readers as one additive
`host-usage-snapshot` row. `host_usage` stays the sole reader and
model-family authority; `events.make_host_usage_snapshot` is a pure
constructor; `events_tail._capture_host_usage` owns the timing and the
all-or-nothing decision. No `EVENTS_SCHEMA_VERSION` bump — legacy consumers
already skip unknown types.

**All-or-nothing for FAILURES (premise confirmed 2026-08-16).** A row is
written only when every reader the sweep CONSULTED either returned
`complete=True` or reported a reason in `_HOST_ABSENT_REASONS`. The latter
means the source cannot supply a metadata-only ledger, so it is excluded from
coverage and the sweep continues. Any other incomplete read omits the WHOLE
row — no partial totals, no zero placeholder — and the scan short-circuits at
the first failure. Publishing a total that silently omits real usage is worse
than publishing nothing. The consumer side follows: an ABSENT row means
"unknown", never zero.

**But an ABSENT source is not a failure (premise revised 2026-08-16).** The
original reading treated Grok's refusal as a failure, which meant that merely
having Grok installed made the row unpublishable — forever, on that machine.
Measured live before the revision: `read_grok_usage` returns in 0.039ms and
discarded a complete 6.4B-token Codex scan on every push, while pinning
`mm status` at `degraded` permanently and so destroying that breadcrumb as a
signal for real sync degradation — the exact failure mode the `claude_paths`
guard a few lines away exists to prevent.

`_HOST_ABSENT_REASONS` (today: `no_metadata_ledger`) marks a store that, by
design, holds no metadata-only usage ledger and never will. That reader is
dropped from `token_sources` and the sweep continues. **This is deliberately
not keyed on `unsupported`:** Codex returns `unsupported` for a ledger it
cannot attribute and OpenCode for a malformed row, which both mean "real usage
is here and I could not read it" — those keep the veto. Getting that
distinction backwards silently under-reports the fleet.

**Host readers are consent-gated.** `HOST_READER_SOURCE_GATE` maps Codex, Grok, and
OpenCode to the source name whose being enabled authorizes them.
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
card; a Grok skill-link target is later; session-transcript sync and a
Codex/Grok `sessions-snapshot` are not planned.

**Grok v1 terminal ledger (Track 18D).** When consented, `read_grok_usage`
walks `GROK_HOME/sessions` (else `~/.grok/sessions`) and reads only regular
non-symlink `updates.jsonl` files directly under a session directory. It
accepts a line only when `params.update` is exactly the `turn_completed`
projection (`prompt_id`, `sessionUpdate`, `stop_reason`, `usage`) with no
content-bearing fields. Each accepted record is a per-prompt total attributed
to the UTC day of the outer timestamp. `reasoningTokens` must be a bounded
subset of `outputTokens` and is never added twice. The private
`grok-host-tokens.json` cache stores opaque file keys, fingerprints, offsets,
hashed terminal keys, model IDs, and aggregate counters — never paths,
prompt IDs, or conversation bytes. Equal duplicate
`(workspace, session, prompt_id, model)` records count once; conflicting
duplicates refuse the store. The model is always part of the key so a
later multi-model restatement of the same prompt cannot double-count.

**First-success carve-out (Track 21A).** Until this machine has completed a
consented Grok scan that observed at least one `updates.jsonl`, a Grok
`deadline` / `locked` / `busy` / `io_error` / `partial` / `unavailable`
(returned by the reader, not a pre-invoke sweep expiry) drops Grok and still
publishes Codex/OpenCode. After `complete_once` is set on the Grok cache,
those reasons veto the whole snapshot again. `malformed` / `unsupported` /
`stale` always veto when Grok was invoked. Warm Grok on a Grok `deadline`
before applying the carve-out; autopush never warms.

**`token_sources` is therefore per-push, not the constant.**
`events.HOST_USAGE_TOKEN_SOURCES` is the universe of readers; a row carries the
subset that actually contributed. That is what lets a consumer tell "this host
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
  There is no per-source carry-forward: Codex and OpenCode can already merge
  into one ``codex`` family, so the wire lacks the source-to-family precision
  required to merge partial observations safely.
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
- ``token_sources`` is an order-preserving, duplicate-free subsequence of
  ``HOST_USAGE_TOKEN_SOURCES``. A nonempty ``hosts`` payload requires a
  nonempty ``token_sources``; ``hosts == {}`` with ``token_sources == []`` is
  valid and means no source contributed.
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
``active_days``. Additive top-level fields are deliberately excluded, so they
cannot change a winner they otherwise leave semantically unchanged. A
clock-backdated row remains older by ``as_of``; JSONL encounter order is not a
safe physical-time signal.

### Track 22A consumer: last-known-good inventory

The aggregator is the first consumer of accepted host-usage-snapshot
rows. The acceptor lives in ``aggregator._accept_host_usage_snapshot``,
not in the writer. A complete later row replaces the entire device view;
there is no per-source carry-forward.

The winning row is kept **whole**. ``HostDeviceSnapshot.lifetime_by_family``
is inventory as of ``as_of``, and **do not sum devices into a fleet spend map**
(see the disjointness note below).

**What a day key actually is (corrected in v0.12.37 — the pre-v0.12.37 wording
here said "day keys are last-touch lifetime totals", which is wrong and it
mis-designed Track 23A).** Each *rollout file* contributes ONE cumulative
terminal total, keyed to **that file's** last-touch UTC day
(``host_usage._read_rollout`` → ``_terminal_from_record`` → ``_aggregate``
merging into ``hosts[family][thatDay]``). So a bucket holds *the cumulative
totals of every session that last touched this machine on that day* — a per-day
distribution, not one lifetime figure smeared across days. Measured on a real
corpus: 65 populated day keys across a 140-day span with 75 gap days.

Two consequences a consumer must carry, and they pull in opposite directions:

- **A window slice of MAGNITUDE over-counts at the recent edge.** Resuming a
  session restates its whole cumulative total onto its new last-touch day, so
  the newest bucket is inflated (measured: 34% of a machine's lifetime landed on
  the snapshot day). Label such a column *"tokens from sessions last active in
  this window"*, never "spend".
- **A count of active DAYS under-counts, and is therefore a LOWER BOUND.** The
  same restatement *erases* the old day key, so a day that had real activity can
  vanish. Five weekday sessions resumed on Saturday collapse to one active day,
  and a fixed window can report FEWER days when re-rendered later. Word it as an
  observation (``seen on N days``), never as a census, and never diff or chart it
  — the writer-side note above already forbids treating ``active_days`` as a time
  series.

**Neither is a fleet spend total, and a fleet SUM is separately forbidden**
because device ledgers are not provably disjoint: ``device_id`` lives in local
``config.toml`` while the host stores (``~/.codex/sessions``,
``~/.grok/sessions``) sit outside every mm sync source, so they move between
Macs only by OS-level migration. Migrate a home directory, run ``mm init``
fresh, and two device ids carry overlapping history with no signal the
aggregator could detect. A **day-set union** across devices is safe precisely
because set union is idempotent under duplicated corpora; a token sum is not.

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

Host totals never enter Claude cost estimation or snapshot
``metrics.tokens_total``.

### Track 23A renderer contract

The card carries **rhythm**; the body carries **magnitude**. That split is the
whole design, and it follows from the two consequences above: a day count can
only understate, while a magnitude can overstate without bound and inverts over
time (measured, at the time of writing: 6.9B Claude tokens over 7 days beside
2.2B Codex over 140 — same order of magnitude, so nothing cues the reader that
the bases differ, and the lifetime figure keeps growing while the weekly one
does not).

- **Card, `AGENT LOGS` block** (`_agent_rhythm_view` + `_render_agent_block`):
  per-family count of distinct in-window UTC days, unioned across machines, one
  family per line, plus an `N of M machines with agent activity` scope in the
  header. **No token magnitude in any state.** The block is omitted only when
  **zero** snapshots were accepted; when snapshots exist but nothing was active
  it still renders, so the provenance count cannot vanish exactly when it
  matters.
- **Rows are MODEL FAMILIES, not agents.** The row carries no per-source status
  and `host_usage.host_family` buckets by model-id prefix, so the Codex and
  OpenCode readers both land GPT in the `codex` family. `AGENT_FAMILY_ROWS`
  labels them accordingly (`Codex models`), and labels the legal `claude` family
  `Claude (via agents)` so it cannot be confused with the MODELS block's own
  `Claude` row. Its keys must stay equal to `MODEL_FAMILY_ROWS` and
  `_HOST_FAMILIES`; a test pins all three, because divergence would both
  silently drop host families AND raise `KeyError` out of
  `_aggregate_model_families`, taking down the whole render.
- **Body, `## Agent activity`** (`_render_agent_inventory`): one row per
  `(machine, model family)`, never per `(machine, agent)`. Columns are
  `Tokens (last 90 active days)` — the writer caps the payload at
  `MAX_BY_DAY_DAYS`, so "lifetime" is false past 90 active days (verified: 91 in,
  90 kept, oldest silently dropped) — and `Tokens in this window`. A row for
  every known machine, including `no snapshot`. An accepted-but-idle machine
  renders **`0`, not `—`**: zero is known data, `—` means unavailable. Rows are
  capped (`MAX_AGENT_INVENTORY_MACHINES`) because the registry is loaded
  wholesale and uncapped. Which readers ran is reported per machine, below the
  table, never per row.
- **State strings are display strings**, never raw fields: `current` /
  `current, no agent activity observed` / `last seen before window` /
  `clock ahead (<=24h)` / `no snapshot`. The skew band really is ≤24h and the
  boundary itself is accepted (the rejection test is `>`).
- **Absence is never silent.** `_agent_coverage_notes` names the cause with a
  remedy every time the block is quiet: no snapshot yet, no reader contributed,
  all snapshots stale, or nothing active. `token_sources` is per-push
  contribution state, so the second case may mean no source is enabled **or**
  that each selected reader had no attributable local ledger; the renderer must
  state that ambiguity rather than falsely diagnosing consent. A vanished block
  must never be the diagnostic interface — seven distinct causes would otherwise
  render identically as nothing.
- **The rejected breadcrumb counts DEVICES, not rows.** `aggregate_host_usage`
  applies no window filter to rejects (only accepted rows are compared against
  `until`), so one malformed writer 89 days ago would light a row-count
  breadcrumb on every 7d retro until retention reaped the file. Window-scoping
  the rejects themselves is impossible for a `naive_timestamp` reject, where the
  timestamp IS the malformed field.
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
- **Isolation, pinned by test.** Host data reaches exactly two render sites and
  nothing else: not `sessions.tokens_by_model`, not
  `_aggregate_model_families`, not `estimate_cost`, not
  `_unpriced_token_summary`, not `_render_token_block`, not `PriorRetroDelta`,
  not `_retro_to_snapshot`. `token_usage.sum_bucket` is deliberately NOT shared
  with `_aggregate_model_families`: the two callers sit on opposite sides of a
  trust boundary, and a later hardening for the tolerant caller would otherwise
  silently cap accepted host totals.
- **A card-level change gate is possible (Track 24B).** The v0.12.0 circularity
  (`main` rendered the card iff `has_card_input` and saved iff **not**
  `has_card_input`) is the bug 24B exists to break. Do not re-assert that
  deltas can only live on the save-enabled first pass.

Forbidden: summing `lifetime_by_family[family][day]` buckets as "tokens this
window", summing across machines at all, and rendering any ratio against the
in-window day count.

``mm retro-fleet --dump-host-usage`` is the forensic hatch. It prints
the inventory JSON and skips the markdown retro.

**Its own deadline, started after `walk_done`.**
`HOST_USAGE_READ_BUDGET_AUTOPUSH_MS` (250) / `_INTERACTIVE_MS` (500), passed
explicitly to every reader. Two halves, both load-bearing: capture begins
AFTER the `walk_done` snapshot (invariant 4) so host time can never trip or
redefine the session-walk notice, and the deadline is FRESH rather than the
walk's leftovers — reusing `deadline` would make the row vanish exactly on the
busy machines where it is most interesting. No caller may fall through to
`host_usage.DEFAULT_READ_BUDGET_S` (5s), which is 20x an entire autopush walk
budget spent on optional analytics.

**Codex and OpenCode collide by design.** OpenCode classifies GPT models into
the same canonical `codex` family, so two readers can return the same
`(family, UTC day)` bucket. They are summed with
`token_usage.merge_usage_bucket`, never shallow-updated — an update would drop
whichever reader ran first, silently.

**A reader exception is contained in `_capture_host_usage`, not at the outer
guard.** The tail's `try/except` would also discard the git and session rows
already captured and the terminal `mm-push` with them, so an unreadable host
store would cost the retro its real content AND rewind the cursor into a
30-day re-walk on every subsequent push. Reader exceptions normalize to the
same incomplete outcome as a returned `complete=False`.

**The notice text is a closed vocabulary.** `_host_skip_phrase` names only the
reader and the reason class — never a path, transcript, SQL, model id, or
exception string. Reasons outside `host_usage.Reason` normalize to
`unavailable`. `unsupported` NEVER promises a retry (it is a standing property
of the host's storage); every other reason may. The phrase deliberately
contains no `; `, which is the separator `autopush` joins breadcrumb reasons
with.

**Tail appends a degradation; init prints only.** Same rule as every other
tail degradation: `_run_events_tail` writes the `mm: notice:` AND appends the
phrase to its returned list, because an unattended hook's stderr reaches
nobody and `mm status` is the only surface the user reads. It is NOT
rate-limited — it describes the CURRENT state, and no-op pushes never reach
the tail at all, so a stale `success` would be the misleading outcome.
`_run_events_backfill` has no `mm-push` row and no autorun breadcrumb, so it
emits the notice alone.

**Row order.** Tail: git rows, sessions row, optional host row, `mm-push`
LAST (CT-4 unchanged). Backfill: git rows, sessions row, optional host row,
and never an `mm-push`.

**Zero work when there is nothing to say.** Dry-run, an unresolved/disabled
`mm-events` source, and a no-op push all return before capture, so no reader
opens a host store or touches a host cache. Pinned in
`tests/test_host_usage_snapshot.py` and `tests/test_integration.py`.

**Reader tolerance: an ordinary Codex shape must never refuse the store.**
One unreadable rollout fails the WHOLE `_scan_codex_root`, and all-or-nothing
then publishes nothing — so the blast radius of a too-strict reader is the
entire feature, on every machine, forever. Measured on a live 452-rollout
Mac before the fix: **167 rollouts (37%) failed** and `read_codex_usage`
returned `unsupported` in 5 ms, having died on the first file. Two shapes are
therefore skipped rather than refused:

- a `token_count` whose `payload.info` is null or absent — Codex's
  start-of-turn marker, present in 33% of rollouts, carrying no ledger;
- a ledger that precedes the first `turn_context` (no model yet). Totals are
  cumulative, so a later attributable record restates it. Live sessions open
  this way.

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
scan that staged nothing still escapes via `_NoCacheCommit` rather than
rewriting the file. Measured after the fix: an autopush-only machine converges
in **3 pushes** (264 → 361 → 440 files cached) with no interactive command.

**A day bucket is not a day's spend, and it mutates.** The readers report a
CUMULATIVE total per session file and attribute the whole total to the UTC day
of that file's LAST record. A bucket therefore means "lifetime totals of every
session that last touched this machine on that day" — 63 of 440 rollouts on a
real corpus land on a day they did not start, and one day carried 3.4B tokens
because 91 sessions collapsed onto it. Resuming an old session moves its entire
total into a new day, so a FIXED day's value can DECREASE between consecutive
snapshots. The only safe consumption is latest-row-per-device as a
point-in-time view; diffing, summing, or charting `active_days` as a time
series all produce wrong numbers. Track 20A locks this contract above, before
Track 21 adds the first consumer.

**The payload is capped at `MAX_BY_DAY_DAYS`, because the readers are not.**
`_iter_rollouts` has no `since` and the OpenCode query has no date predicate,
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
`warm_host_cache_inline(reader=...)` warms Codex or Grok
(`WARMABLE_HOST_READERS`). A `deadline` charged to OpenCode cannot be helped
by it. Without the reader half of the gate an interactive push pays
bounded-attempt + up to 5s warm + bounded-retry — ~6s, on every push, forever,
still publishing nothing.
A cold scan does not fit the per-capture budget (573 ms vs 250/500 ms), so
`_capture_event_snapshots` may retry once after `warm_host_cache_inline`, but
ONLY when the first attempt returned reason `deadline` — the only reason a warm
can fix. Gating this way costs nothing on the happy path, needs no persisted
"have I warmed?" marker, and cannot misfire on a machine that legitimately has
no host data: that machine's first attempt COMPLETES, so it never warms. An
entry-count predicate would have asked exactly that machine to warm on every
push forever.

`warm_host_cache` is supplied by the wrapper and is `None` on autopush — an
unattended hook never spends seconds on optional analytics; it converges via
partial commits instead. Interactive push and init supply it. The published row
always comes from a BOUNDED capture, warm or not, so the warm never becomes a
back door around the explicit-deadline rule. Measured on a cold interactive
push: 501 ms bounded miss + 154 ms warm (cheap precisely because the failed
attempt cached most of the corpus) + 28 ms retry = 683 ms once, then ~35 ms per
push forever after.

**Tests must never read a real host store.** `conftest._isolate_host_usage`
redirects all three reader roots and all three caches per test; tests needing
data monkeypatch the reader functions. Without it the suite's result would
depend on which agents are installed on the machine running it — a developer
with `~/.grok/sessions` would see the healthy-tail control pin in
`test_silent_failure_contract.py` fail locally while CI stayed green.

## Init-time event backfill (v0.11.8)

`_run_events_backfill(config, sources, device_id)` runs at the END of `mm init`, AFTER `_register_and_save` and `_ensure_retro_skill_links`. Closes the gap between init and first push: retro-fleet works immediately on a fresh install instead of being empty until the first `mm push` (or autopush hook) fires. Sources are resolved via `get_sources(config)` BEFORE the call so the `mm-events` bootstrap dispatch in `_bootstrap_mm_events_path` materializes the events dir before walk runs.

**No `mm-push` event by design.** The backfill writes only `git-snapshot` + `sessions-snapshot` rows. Two consequences:

1. **Push counts stay honest.** An init-counted-as-push would inflate the per-window mm-push count in every fresh-install retro by exactly 1. A "1 more than expected" lie everywhere is worse than the trade-off below.

2. **Cursor stays at "no prior mm-push".** The first real push's `last_push_ts` returns `now - 30 days` again and re-walks the same 30-day window. Aggregator dedups via `(canonical_remote_url, sha)` so retro output is identical; cost is one extra ~500ms `git log` walk on the first real push, paid once per machine.

**Idempotent at the aggregator layer.** Commits dedup by `(canonical_remote_url, sha)`; sessions latest-wins per `(device, source_root, claude_dir)`. Re-running init (or invoking the backfill twice for any other reason) produces the same retro output, only slightly larger events files.

**`mm-events`-resolved gate, mirrored from `_run_events_tail`.** A user who disabled mm-events per-machine (`disabled_sources: ["mm-events"]`) gets a silent no-op — no events dir created, no rows written. Same gate covers fresh installs (mm-events auto-included via `MM_INTERNAL_SOURCE_NAMES`), un-migrated upgraders (no mm-events source in config → no-op), and explicit opt-outs uniformly.

**Forensic-only on failure.** Same `try / except Exception` wrapper as `_run_events_tail`; failures emit `mm: notice: events backfill failed: <type>: <safe_str(msg)>` to stderr. Init proceeds. A budget overrun emits `mm: notice: events backfill budget exceeded` and the partial events written so far stay on disk.

**No subcommand, no marker.** `mm backfill-events` was considered but deferred — the existing `_run_events_tail` already covers the "user pushed at least once" steady state, and `mm init` runs once per machine so a marker file would prevent re-runs that are otherwise harmless. If a future use case (post-retention refresh, pre-Group-7 migration assistant) needs explicit invocation, expose the helper as a subcommand then; today's surface area is intentionally minimal.

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
`MM_SKILLS_DIR` overrides it and is read on EVERY call, NOT gated on pytest —
set it in a real shell and it relocates the real store and every agent link.
The docstring calls it a test override, but the suite does not use it:
`conftest.py:_isolate_skill_links` monkeypatches `_skill_store_dir` itself.
So it is an undocumented production knob, and the user-facing `--store PATH`
form is deliberately unshipped. Either gate it or document it — do not leave
the docstring claiming a scope the code does not enforce. `mm` copies **only** `SKILL.md` there via
`fsutil.atomic_write_bytes`. `aggregator.py` stays in the wheel and is imported
by `cli.py:retro_fleet_cmd`. Never symlink the store at the package — that
moves the dangle one hop.

Source dir on disk is `retro_fleet/` (underscore — Python identifier so
`mind_meld.skills.retro_fleet.aggregator` is importable); link name is
`retro-fleet` (hyphen — Claude Code skill convention).

**Three targets since v0.12.18.** Claude Code, Codex, and OpenCode, enumerated
in `SKILL_ROOTS` and resolved per call by `skill_targets()`. A call-time
`SkillTarget` descriptor owns each agent root. `SkillInstallResult` reports
`installed`, `unchanged`, `unavailable`, `dangling-ours`,
`dangling-ours-legacy`, `foreign`, or `failed`. `skill_src` is provenance
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

**State machine.** Absent root → `unavailable`; non-directory/I/O → `failed`;
missing skills dir under an available root → `mkdir(mode=0o700)`. Then: store
link that resolves → `unchanged`; store link that dangles → repair if writing
else `dangling-ours`; foreign file/dir/symlink → `foreign`, never unlink;
absent target → symlink to the store → `installed`. `dry_run=True` returns full
classifications with zero writes.

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
`_run_events_tail`, with `allow_mutate=not quiet`. `mm diag` is passphrase-free
and prints the skill-links block. `mm status` prints one line only when a link
is broken.

**`mm install-skills`.** Force-runs the installer with `explicit=True`, ignoring
the TTL gate. Renders `render_skill_status` for foreign/dangling outcomes. Exits
1 for any available foreign/failed result. Leaves user files and foreign
symlinks untouched.

**`mm retro-fleet [window]` typer wrapper (load-bearing, v0.11.22).** SKILL.md's documented invocation is `mm retro-fleet <window>`, NOT `python -m mind_meld.skills.retro_fleet.aggregator`. Reason: the prior `python -m` form failed in real fleet use (user feedback on v0.11.21) on macOS systems where only `python3` is on PATH, and is structurally impossible to fix for the dominant install path — pipx puts mm in `~/.local/pipx/venvs/mind-meld/` and nothing outside that venv can `import mind_meld`. Routing through the `mm` console-script (always on PATH wherever mm is installed) sidesteps both. The typer command is a thin shim: forward-imports `aggregator.main` lazily to keep cli.py module-load fast, builds `argv` from the typer args (positional `window` defaults to `7d`; `--no-author-filter`, `--theme`, `--noteworthy`, `--name`, `--no-save` flags forward verbatim), and `raise typer.Exit(code=...)` so non-zero aggregator exits become the CLI exit code. The aggregator's existing `argparse`-based `main()` is unchanged — direct `python -m` invocation still works from a development checkout, it's just no longer the public surface. Pinned by `tests/test_retro_fleet_cli.py` (TestRetroFleetCommand: positional window, default `7d`, `--no-author-filter` forwarded, theme/noteworthy/name/no-save forwarded, non-zero aggregator exit propagates).

## Two-pass ASCII card + LLM narrative split (load-bearing, v0.12.0)

The retro-fleet output has two artifacts with different production paths:

1. **The ASCII card** — pixel-aligned screenshot artifact rendered by Python. Stats (commits, repos, machines, LOC, streak) come from `RetroData`; `--theme` (×3) and `--noteworthy` flags carry the LLM-synthesized narrative bits in. `_render_ascii_card` pads every line to `CARD_WIDTH` (64) with right border via `╔/╗/║/╝`. `--name` is optional header personalization.

2. **The narrative paragraphs** (praise / level-up / focus) — written by the LLM directly into the conversation, NOT into the card. The SKILL.md instructs one each, anchored in actual commits/stats, framed as investment-advice not criticism.

**Two-pass invocation is load-bearing.** Pass 1 (`mm retro-fleet 7d`) renders the markdown body + a fenced JSON sidecar tagged `<!-- MM_THEMES_PROMPT -->` for theme synthesis. Pass 2 (`mm retro-fleet 7d --theme A --theme B --theme C --noteworthy "..." --name kb --no-save`) re-renders with the card pinned at the top. The LLM never counts characters — Python pads. The single-pass alternative (LLM pads its own card content to width) was rejected because Opus drifts by 1-2 chars often enough to ruin screenshots. Pinned by `TestAsciiCard.test_card_lines_pad_to_fixed_width`.

**`--no-save` on the second pass** prevents the snapshot from being double-written. The first-pass save is the canonical record for trend deltas; the second pass is purely a re-render for presentation. Pinned by `TestMainCliFlags.test_no_save_flag_skips_snapshot`.

**Themes prompt content scope.** The JSON payload includes `window_days` / `since` / `until` / `commits` / `additions` / `deletions` / `top_repos[]` / `ship` (or null). Repo URLs and ship subject pass through `_safe_repo_url` + `_shorten_repo_url` and `_safe_prose` respectively before serialization — the same trust-boundary defenses applied to the markdown body, so a long-canonical-URL or peer-controlled subject doesn't leak into the JSON sidecar. Pinned by `TestThemesPrompt.test_long_repo_url_shortened_in_prompt`.

**`_safe_prose` vs `_safe_short` (v0.12.0).** `_safe_short` whitelists `[A-Za-z0-9._\-() ]` — fine for short identifiers (skill names, model names, sha) but mangles prose punctuation (colons, slashes, hashes, em-dashes). `_safe_prose` strips terminal escapes + Rich markup + C0 controls but preserves printable punctuation — use for commit subjects (peer-controlled) and LLM-supplied theme/noteworthy/name lines. Both call through `safety.safe_str` so the terminal-escape defense is shared.

## Snapshot persistence (v0.12.0)

Local-only JSON snapshots at `~/.local/share/mind-meld/retros/YYYY-MM-DD-N.json` (mode 0o700). NOT synced — fleet determinism (every machine produces identical retros after sync, per the v0.11.17 union filter) makes a local cache sufficient for "trends vs last retro" deltas without cross-fleet snapshot reconciliation. Sequence number defends against multiple retros in one day.

**Saved fields (v1 schema).** `window_days`, `since`, `until`, and a `metrics` block (`commits`, `additions`, `deletions`, `pull_requests`, `streak_days`, `sessions`, `tokens_total`, `push_events`). Tokens are summed across input/cache_create/cache_read/output for a single comparable scalar. Future fields can be added without breaking older readers — `_compute_prior_delta` defaults missing keys to zero.

**`metrics.pull_requests` (Track 17E).** Count of distinct, repository-qualified
GitHub PR references detected from supported commit-subject forms, not an API-backed
repository-throughput total. An identity is `(canonical remote, PR number)` and is
created only after the existing author filter (unless `--no-author-filter`), window,
and `(canonical remote, sha)` commit-dedup gates accept the record. Empty or malformed
remotes and unsupported, malformed, oversized, or non-positive subject markers do not
contribute. Older snapshots legitimately lack this additive field; it is not yet used
for a PR trend delta, so missing history is never presented as zero.

**Load picks most recent matching window.** `_load_prior_snapshot(retros_dir, window_days)` glob-sorts descending and returns the first snapshot with the same `window_days`. A 7d retro never compares against a 30d snapshot. First-run / no-match returns None and the trends section is omitted. Pinned by `TestSnapshotPersistence.test_load_skips_window_mismatch`.

**Write is post-load.** `main()` loads the prior snapshot BEFORE saving the new one so today's retro doesn't compare against itself.

**Save skip on second pass.** `--no-save` is wired AND any of `--theme` / `--noteworthy` / `--name` being set also short-circuits the save (the second-pass call IS the card render; the first-pass call already saved). The second-pass guard is intentional belt-and-braces in case a power user calls the second pass directly without `--no-save`.

**Reap by FILENAME date, NOT mtime.** Same rationale as `_gc_old_event_files` — iCloud restores rewrite mtimes. `_prune_old_snapshots` parses the `YYYY-MM-DD` prefix from `<stem>` and drops files older than `RETROS_RETENTION_DAYS` (365). Best-effort: every step (glob, unlink, parse) is wrapped in try/except. Pinned by `TestSnapshotPruning`.

**Conftest isolation: `_isolate_retros_dir` autouse fixture.** Sets `MM_RETROS_DIR` to a per-test tmp dir so every test invoking `aggregator.main()` gets its own retros dir. Mirrors the `MM_EVENTS_DIR` / identity-cache / pullhistory isolation pattern. Without it, every test run would pollute the user's real `~/.local/share/mind-meld/retros/`.

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
output lists the resolved emails. Exits 1 with a `mm: warning:` when
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
`proj.get("skills_by_day")` falsy-check. **Critical difference vs.
`pre_token_peers`:** every session generates tokens, so the existing
token check (missing OR empty AND sessions > 0) is correct. Skills are
different — a session can legitimately invoke zero skills, so an empty
`{}` is a content signal ("no skills used in window"), not a version
signal. Conflating the two would surface "Skills incomplete" on every
retro for users who don't lean on skills.

**Two populations land in `pre_skills_peers`:** (1) pre-v0.11.27 mm
peers whose code never emits the field, and (2) v0.11.27+ peers whose
skill walk was skipped this push because `events.py:_scan_one_project`
ran with `token_cache_files=None` (cold token cache + autopush gate at
`events_tail.py:_decide_token_walk_policy`, or warn-mode flock contention where
`lock_and_get_files("warn")` yields `None`). The wire genuinely can't
distinguish the two — both ship the field absent. The rendered Notes
breadcrumb, built in `aggregator.format_retro`, mirrors `pre_token_peers`'s
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
`_render_token_block` / `_unpriced_token_summary` / `_short_model_name`.**

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

**`PRICING` is an OVERRIDE table and ships EMPTY.** Every current model
prices at its family's rate, so a per-model entry would be duplication
that recreates the multi-site drift this release removed: an Opus rate
change would need five identical edits plus the tier, and missing one
would silently price some models at the old rate. Two independent
reviewers flagged the first draft (which listed all six) for exactly
this. Add an entry ONLY when a model permanently departs from its tier;
`test_pricing_holds_no_redundant_entries` fails the build if an entry
duplicates its family. (Claude Sonnet 5's introductory `$2/$10` through
2026-08-31 is *not* such a case — mm reports list price.)

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
unresolvable model in the window flips the cost line's prefix from `~` to
`>=`. The stderr breadcrumb in `estimate_cost` is NOT sufficient on its
own: it fired for four unpriced models across the whole v0.12.x line and
nobody saw it. The load-bearing signals are the rendered ones — the `>=`
marker and the Notes line.

**Invariant 5 — `PRICING_LAST_UPDATED` is a fact on the card, not a
threshold.** mm has no network by design (CLAUDE.md: "No API server"), so
this table can never self-update and **stale is the steady state, not the
exception**. The old docstring said "refresh if more than ~6 months old";
nothing read it, and it would not have helped — the table was three
months into that window while wrong about four models. The date now
renders in the caveat line so a human can judge. Do NOT reintroduce a
"warn after N months" rule: it is a verdict the code cannot earn, and it
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
