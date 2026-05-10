# Events / fleet retro — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/cli.py` — `_run_events_tail` / `_run_events_backfill` / `_ensure_retro_skill_link` / `_skill_link_check_due` / `_resolve_retro_skill_src` / `install_skills_cmd` / `refresh_identity_cmd` / `_devices_json_cmd` / `EVENTS_RETENTION_DAYS` / `_gc_old_event_files`
- `src/mind_meld/events.py` — `MmPushEvent` / `make_mm_push_event` / `walk_session_metadata` / `walk_git_projects` / `discover_git_roots` / `last_push_ts` / `EVENTS_SCHEMA_VERSION` / `WALK_TIME_BUDGET_*`
- `src/mind_meld/identity.py` — `gather_local_identities` / `refresh_identity_cache` / `CACHE_PATH` / `TTL_SECONDS`
- `src/mind_meld/skills/retro_fleet/aggregator.py` — `aggregate` / `aggregate_local_emails_from_events` / `aggregate_git` / `aggregate_sessions` / `gather_author_emails` / `_emit_custom_path_notice_if_due`
- `src/mind_meld/config.py` — `MM_INTERNAL_SOURCE_NAMES` / `_bootstrap_mm_events_path` / `DEFAULT_SOURCES`
- `src/mind_meld/token_usage.py` — `walk_session_metadata` token-cache wiring

Tests: `tests/test_events.py`, `tests/test_identity.py`, `tests/test_init_events_backfill.py`, `tests/test_gc_events.py`, `tests/test_retro_fleet_aggregator.py`, `tests/test_skill_link.py`, `tests/test_devices_json.py`, `tests/test_token_usage.py`.

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

4. **Wall-clock budget (Codex C4 + C5).** `WALK_TIME_BUDGET_AUTOPUSH_MS` (250) for `quiet=True` (autopush hook), `WALK_TIME_BUDGET_INTERACTIVE_MS` (500) for interactive `mm push`. The deadline is plumbed through to `walk_session_metadata` via the new keyword-only `deadline_monotonic` param — `_read_cwd_from_latest_jsonl` reads jsonl line-by-line until a `cwd` field appears, so a single pathological project can blow the budget without per-project deadline checks. A tail-position `time.monotonic() > deadline` check emits `mm: notice: events tail budget exceeded` to stderr (visible-failure contract; the push proceeds).

**Forensic-only invariant.** The whole block is wrapped in `try / except Exception`; failures emit `mm: notice: events tail failed: <type>: <safe_str(msg)>` to stderr and the push continues. `safe_str(e)` defangs peer-controlled escapes per the v0.10.1 sanitization invariant (a corrupt peer manifest could otherwise smuggle ANSI through an exception's `__str__`).

**`MmPushEvent.sources` schema is `list[str]` (names only) — Codex C2 + C7.** `iter_source_diffs(skip_unchanged=True)` drops unchanged sources from the diff loop, breaking per-source counts on the no-content push path. The retro-fleet skill (Group 8) reads per-source content stats from the synced manifest at retro time, not from the event row. `make_mm_push_event` filters `MM_INTERNAL_SOURCE_NAMES` from the names list — `mm-events` is mm-owned infrastructure, not user-meaningful fleet activity.

**Fleet retention via tombstone propagation (Codex C10).** `_gc_old_event_files` reaps day files older than `EVENTS_RETENTION_DAYS` (90). The retro skill reads the synced manifest, so deletion fans out fleet-wide via the existing tombstone path: this device unlinks → next push generates a tombstone → all peers drop their copy on pull. An offline peer that comes back online sees the tombstone too, suppressing resurrection.

**Reap by FILENAME date, NOT mtime (Codex C5, C6).** iCloud restores can rewrite mtimes back to "now" while the filename date (`<device>-YYYY-MM-DD.jsonl`) is intrinsic to the event-day boundary the file was written for. The mm-events path resolves through `get_sources(config)` so user-customized paths are honored. Always-on (no `--events` flag) — events retention is fleet policy.

**Initial cursor lookback (Codex C9).** `last_push_ts(events_dir, device_id)` returns `now - INITIAL_CURSOR_LOOKBACK_DAYS` (30) when no prior `mm-push` event exists. New fleet members joining mid-quarter scan back 30 days of git history; older context is invisible to retro until a manual backfill. Document the bound in skill output: "First-run window: last 30 days of activity. Older history is intentionally outside the retro window."

## Init-time event backfill (v0.11.8)

`_run_events_backfill(config, sources, device_id)` runs at the END of `mm init`, AFTER `_register_and_save` and `_ensure_retro_skill_link`. Closes the gap between init and first push: retro-fleet works immediately on a fresh install instead of being empty until the first `mm push` (or autopush hook) fires. Sources are resolved via `get_sources(config)` BEFORE the call so the `mm-events` bootstrap dispatch in `_bootstrap_mm_events_path` materializes the events dir before walk runs.

**No `mm-push` event by design.** The backfill writes only `git-snapshot` + `sessions-snapshot` rows. Two consequences:

1. **Push counts stay honest.** An init-counted-as-push would inflate the per-window mm-push count in every fresh-install retro by exactly 1. A "1 more than expected" lie everywhere is worse than the trade-off below.

2. **Cursor stays at "no prior mm-push".** The first real push's `last_push_ts` returns `now - 30 days` again and re-walks the same 30-day window. Aggregator dedups via `(canonical_remote_url, sha)` so retro output is identical; cost is one extra ~500ms `git log` walk on the first real push, paid once per machine.

**Idempotent at the aggregator layer.** Commits dedup by `(canonical_remote_url, sha)`; sessions latest-wins per `(device, source_root, claude_dir)`. Re-running init (or invoking the backfill twice for any other reason) produces the same retro output, only slightly larger events files.

**`mm-events`-resolved gate, mirrored from `_run_events_tail`.** A user who disabled mm-events per-machine (`disabled_sources: ["mm-events"]`) gets a silent no-op — no events dir created, no rows written. Same gate covers fresh installs (mm-events auto-included via `MM_INTERNAL_SOURCE_NAMES`), un-migrated upgraders (no mm-events source in config → no-op), and explicit opt-outs uniformly.

**Forensic-only on failure.** Same `try / except Exception` wrapper as `_run_events_tail`; failures emit `mm: notice: events backfill failed: <type>: <safe_str(msg)>` to stderr. Init proceeds. A budget overrun emits `mm: notice: events backfill budget exceeded` and the partial events written so far stay on disk.

**No subcommand, no marker.** `mm backfill-events` was considered but deferred — the existing `_run_events_tail` already covers the "user pushed at least once" steady state, and `mm init` runs once per machine so a marker file would prevent re-runs that are otherwise harmless. If a future use case (post-retention refresh, pre-Group-7 migration assistant) needs explicit invocation, expose the helper as a subcommand then; today's surface area is intentionally minimal.

## Sessions snapshot v=2 full-inventory (load-bearing, v0.11.0)

`EVENTS_SCHEMA_VERSION` bumped 1 → 2 in Group 8. Pre-v0.11.0, `walk_session_metadata` filtered jsonls by `mtime >= since_ts` — each snapshot was a DELTA. Naive sum of v=1 snapshots double-counted any chat that was touched across pushes; latest-only-wins undercounted by losing prior windows. Codex outside-voice review caught the trap during `/plan-eng-review` for Group 8 (cross-model tension #1).

v=2 sessions-snapshot is FULL INVENTORY: every jsonl in the projects tree is counted regardless of mtime. The aggregator picks the LATEST v=2 snapshot per `(device, source_root, claude_dir)` — produces an accurate point-in-time sessions count for the rendering machine's view of the fleet. mm-push and git-snapshot rows keep delta semantics (commits since last push, dedup-by-sha aggregator side); only sessions-snapshot semantics changed.

**Mixed-fleet transition rule.** Pre-v0.11.0 peers still emit v=1 sessions rows. The retro-fleet aggregator treats v=1 sessions as below-threshold and surfaces "Sessions count incomplete: peer X is on pre-v0.11.0" as part of the fleet-incomplete breadcrumb. Numbers are honestly low, never overcounted. Once the fleet rolls to v0.11.0, every peer emits v=2 and the count is exact.

**`since` parameter retained for API stability.** `walk_session_metadata(claude_dir, since, *, deadline_monotonic)` still accepts `since` to keep the call-site signature stable; the value is now ignored (suppressed via `# noqa: ARG001`). A future v=3 schema can re-introduce delta semantics with a new field name without breaking callers.

**`source_root` field on `SessionMetadata` (load-bearing, post-v0.11.2 Group 8 hotfix).** Every `SessionMetadata` carries a `source_root: str` field equal to `str(claude_dir)` from the `walk_session_metadata` caller. The aggregator keys on the 3-tuple `(device, source_root, claude_dir)` instead of the original 2-tuple — pre-fix, two configured `type: claude` source roots that both contained a project encoded as e.g. `-Users-kb-Documents-foo` silently overwrote each other in `latest`. The schema change is additive (`SessionMetadata` is `TypedDict, total=False` — old readers ignore unknown fields, new readers default missing field to `""`), so no v=3 bump.

**Coalesce pass for the rollout window.** Pre-fix records on synced storage have no `source_root` field (treated as `""`); post-fix records carry the populated path. During the rollout window both shapes coexist for the same project. `aggregate_sessions` runs a coalesce pass between the latest-per-tuple population and the `last_session_at` filter that drops `(device, "", claude_dir)` keys when `(device, "<root>", claude_dir)` exists for the same device. Distinct populated `source_root` values are preserved (the legitimate two-source-root case the fix is for); only the legacy empty key with a populated sibling is collapsed. Pinned by `tests/test_retro_fleet_aggregator.py::TestSessionsSourceRoot` (4 tests including the REGRESSION pin `test_two_distinct_source_roots_kept_separate`).

## Aggregator custom-path notice (post-v0.11.2 Group 8 hotfix)

`_emit_custom_path_notice_if_due(events_dir)` runs from `aggregator.main()` right after `events_dir = _resolve_events_dir()`. Library callers of `aggregate()` never see the notice — the gating is in `main()` only. Three-stage gate: (1) `MM_EVENTS_DIR` set → silent (user is overriding correctly); (2) resolved `events_dir != DEFAULT_EVENTS_DIR` → silent (already non-default via param/env); (3) `_read_mm_events_config_path()` returns the configured `mm-events` path; if it equals `DEFAULT_EVENTS_DIR.parent` → silent (config matches default), else emit one `mm: notice:` to stderr pointing at the env override. `_read_mm_events_config_path` mirrors `_read_config_author_emails` — wraps `from mind_meld.config import CONFIG_PATH, load_config` in `try/except Exception`, returns None on any failure, never raises. Pinned by `tests/test_retro_fleet_aggregator.py::TestCustomPathNotice` (5 tests).

## Group 8 retro-fleet skill — symlink installer (load-bearing, v0.11.0)

`_ensure_retro_skill_link()` symlinks `~/.claude/skills/retro-fleet` → `<wheel>/mind_meld/skills/retro_fleet/`. Source dir is `retro_fleet/` (underscore — Python identifier so `mind_meld.skills.retro_fleet.aggregator` is importable from the typer wrapper at `cli.py:retro_fleet_cmd`); link name is `retro-fleet` (hyphen — Claude Code skill convention). The conventions and importability both resolve cleanly via the rename.

**Five-branch state machine.** `target.exists()` returns False on a dangling symlink while `is_symlink()` returns True — these are checked in this order: (1) skills-dir-absent → silent skip (no Claude Code installed); (2) `target.is_symlink() and not target.exists()` → DANGLING-symlink branch, unlink + recreate (REGRESSION-class for `pipx reinstall` recovery; pre-Group-8 design routed dangling links into "exists, don't replace" forever); (3) `target.is_symlink() and target.resolve() == skill_src.resolve()` → already-correct, no-op; (4) `target.exists()` → conflict-skip with `mm: notice:`; (5) target absent → `target.symlink_to(skill_src)`. Every `OSError` from `symlink_to` is wrapped — TOCTOU `FileExistsError`, `PermissionError` on read-only `~/.claude`, `OSError` on filesystems without symlink support all degrade to a stderr breadcrumb without crashing push.

**Two-marker 24h-TTL gate (cross-model #3).** A single TTL marker can't distinguish "skip until tomorrow because it just succeeded" from "skip until tomorrow because the user has their own file there" — touching the marker on conflict skips silently for 24h, leaving it untouched re-emits the notice every push (hostile noise). Two markers under `~/.config/mind-meld/`: `.skill-link-checked` (success) and `.skill-link-conflict` (deliberate-skip). Transient failures (OSError) touch neither, so next push retries. `_marker_is_fresh()` wraps `os.stat` in try/except and **fail-opens** on EACCES / EIO so a chmod-restricted config dir doesn't crash push (TODO#3 critical-gap fix).

**Drift-aware gate (post-v0.11.5 hotfix).** `_skill_link_check_due()` is no longer marker-only. After `_marker_is_fresh()` returns True, the gate also verifies that `~/.claude/skills/retro-fleet` is a symlink, exists (not dangling), and resolves to `_resolve_retro_skill_src()`. Returns True (run installer) when any of those fail; any I/O or resolver error in the drift check fails open. Pre-fix bug in the wild: pipx-installed mm 0.11.0 created the link successfully and touched the marker; user later removed the link by hand (cleaning up an old conductor workspace path the link used to point at on a previous editable install); next 24h of pushes silently skipped the installer. Cost on the steady-state path: one `lstat` + one `readlink` + `importlib.resources` resolution — negligible vs the rest of push.

**Hook positions.** `mm init` calls `_ensure_retro_skill_link(dry_run=False)` unconditionally at the end. `_push_core` HEAD calls `_ensure_retro_skill_link()` AFTER `_ensure_device_registered` but BEFORE `_run_events_tail` (Architecture #5 lock-in: stacked self-heals before the events tail's load-bearing capture block). Gated by `_skill_link_check_due()` — one `os.stat` (marker freshness) + ~3 syscalls (drift verification) per push on the steady-state path. `dry_run` is plumbed through and gates the install (preview contract; mirrors `_ensure_device_registered`).

**`mm install-skills` user-facing CLI (post-v0.11.5).** Force-runs `_ensure_retro_skill_link(dry_run=False)` ignoring the TTL gate. Use cases: post-cleanup recovery (link manually removed), fresh-machine install before first push, verifying the link state after `pipx upgrade mind-meld`. Reports `Installed: <target> -> <skill_src>` on success; exits 1 with an actionable error when the target is a non-mm file/symlink (the user must remove it themselves — the installer never clobbers a non-mm file at the target) or when `~/.claude/skills` doesn't exist (no Claude Code installed). The CLI surface is intentionally kebab-case-plural to match `migrate-config` / `enable-source` / `reconfigure-sources` and to leave room for future skills mm might ship.

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

**Saved fields (v1 schema).** `window_days`, `since`, `until`, and a `metrics` block (`commits`, `additions`, `deletions`, `streak_days`, `sessions`, `tokens_total`, `push_events`). Tokens are summed across input/cache_create/cache_read/output for a single comparable scalar. Future fields can be added without breaking older readers — `_compute_prior_delta` defaults missing keys to zero.

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
`cli.py:2886-2894`, or warn-mode flock contention where
`lock_and_get_files("warn")` yields `None`). The wire genuinely can't
distinguish the two — both ship the field absent. The rendered Notes
breadcrumb at `aggregator.py:1862-1864` mirrors `pre_token_peers`'s
"OR with cold token cache" phrasing to admit the ambiguity honestly.

**Why not "always set `meta['skills_by_day'] = {}`" (rejected fix,
post-/plan-eng-review 2026-05-10).** A surface-cleaner alternative
would be to drop the `if token_cache_files is not None:` gate at
`events.py:_scan_one_project` and emit `skills_by_day = {}` on every
snapshot.
**Codex outside-voice review caught:** the aggregator picks the LATEST
sessions snapshot per `(device, source_root, claude_dir)` at
`aggregator.py:830`. With the always-set fix, a v0.11.27+ device that
pushes warm at T1 (populated `skills_by_day`) and then cold at T2
(synthetic `{}`) has its T1 skill data silently overwritten by T2's
empty dict, AND `aggregator.py:858` (`skills.available = True`) flips
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
