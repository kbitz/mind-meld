# CLAUDE.md

## Project
Mind Meld (mm) — CLI tool for syncing AI coding-agent context, skills, and gstack activity across Macs via iCloud Drive. Supports Claude Code, Codex, OpenCode, and configurable sync sources.

<!-- roadmap:parallelism_cap=8 -->
Raised from the default 4 on 2026-08-14: work runs in parallel Conductor workspaces, so more Tracks can be genuinely in flight than a single-branch workflow assumes.

## Stack
Python 3.11+, typer, cryptography, argon2-cffi, keyring, rich.

## Key Principles
- No API server. CLI talks directly to iCloud Drive via the local filesystem.
- Single storage backend: local folder at `~/Library/Mobile Documents/com~apple~CloudDocs/mind-meld`, synced by iCloud.
- **End-to-end encrypted.** All synced data (manifests, artifacts, and allowlisted agent context) is encrypted client-side with AES-256-GCM before touching storage. The storage layer never sees plaintext. This is a hard invariant — no code path may write unencrypted sync data to storage.
- **Scoped sync.** Built-in sources are allowlisted: Claude Code syncs its `memory/` and `todos/` project data; Codex and OpenCode sync their documented customizations; Grok syncs only hardcoded `skills/`, `commands/`, and `rules/`. Session databases, credentials, and whole-file settings that may contain credentials stay local.
- **Truth-based manifests.** Manifests are complete snapshots of local state. Deletions propagate automatically — no separate prune step.
- **Conflict resolution.** Detects and resolves iCloud and Dropbox-style conflict copies on manifest files. For source files with divergent local edits, INVERTED in v0.9.2: local stays at canonical, REMOTE bytes go to `<stem>.sync-conflict-<ts>-<device>.<ext>` (Syncthing convention's actual direction — visible sidecar holds the surprising bytes). Mtime-skip: if the local file is newer than remote, pull leaves it alone. Pre-v0.9.2 conflict files are migrated to a `v0-` prefix on first lock-protected discovery (mm pull / mm resolve only); resolve dispatches by filename prefix (`v0-` = pre-inversion semantics, no prefix = post-inversion).
- **Sync log.** After pull, writes `.mind-meld-log.md` per project so Claude Code knows what changed from other machines.
- Manifest-based diffing: SHA-256 hash every file, only upload/download changes.
- Content-addressed storage: blobs stored by hash, not by path.
- Gzip compression before encryption. Versioned blob format (v0x01).

## Source Layout

One line per module, with what lives there. Grep this table for a filename
before grepping the code. (It used to be a `{a,b,c}.py` brace-expansion
one-liner, which does not match a search for `resolveflow.py`.)

| Module | Owns |
|---|---|
| `cli.py` | Every `@app.command()` shell, `_pull_core` / `_push_core`, the `_apply_*` family, `init`, `status`, `diag`, the `autopull`/`autopush` pair |
| `manifest.py` | Manifest build/load/diff, rel-path validation, conflict-filename predicates, tombstones |
| `crypto.py` | AES-256-GCM envelope, argon2 KDF, keyring, crypto-init bootstrap |
| `config.py` | `config.toml` load/validate/save, `DEFAULT_SOURCES`, exclude patterns |
| `devices.py` | Device registry, short-id generation and lookup |
| `events.py` | mm-events log: git-root discovery, git/session walkers, budgets |
| `token_usage.py` | Session-jsonl walker, token + skill caches, pricing, incremental resume |
| `host_usage.py` | Local Codex, Grok (`updates.jsonl` terminal records, opt-in), and privacy-safe OpenCode usage readers, strict host-family classifier, and isolated host-token caches |
| `host_skill_discovery.py` | Read-only `grok inspect --json` probe for `mm diag` (`host_skill_discovery` sibling key). Not a `skill_link` registry. |
| `identity.py` | Author-email set behind a flock-guarded 7d-TTL cache |
| `merge.py` | Merge dispatch (`.jsonl`, `MEMORY.md`) + `lcs_merge` 3-way merge |
| `upgrade.py` | Self-upgrade check, nudge, transition hook |
| `pullhistory.py` | Forensic per-file pull log |
| `seen_sources.py` | First-seen source tracking for the enable/disable prompts |
| `synclog.py` | Per-project `.mind-meld-log.md` writer |
| `sidecar.py` | Manifest sidecar read/write |
| `lockfile.py` | The mm lockfile |
| `lockedjson.py` | Single-file flock read/modify/write primitive |
| `fsutil.py` | Atomic write, flock-append, `fsync_dir` |
| `errors.py` | Exception hierarchy |
| **`consoles.py`** | **(16A)** The two shared Rich `Console` singletons |
| **`conflictmtime.py`** | **(16A)** mtime primitives shared by the apply path and the resolver |
| **`skill_link.py`** | **(16A)** retro-fleet skill installer, the mm-owned `SKILL.md` store at `~/.local/share/mind-meld/agent-skills/` (24A), its 24h drift gate and markers, the `mm status` / `mm diag` link diagnosis, the `AGENT_ROWS` registry — add a new agent HERE — and (25C) `consented_agent_keys` / `AgentRow.consent_source` / the installer `declined` status |
| **`events_tail.py`** | **(16A)** The push/init mm-events tail, its walk budgets, and (19A) the all-or-nothing host-usage capture |
| **`resolveflow.py`** | **(16A)** Conflict discovery, promotion, the interactive `mm resolve` walk |
| **`retention.py`** | **(16A)** The `mm gc` reapers + crashed-push tmp sweep |
| `safety.py` | Peer-controlled string sanitization |
| `conflictdiff.py` | Pure leaf renderers for the conflict prompts |
| `storage/{local,keys}.py` | Local backend + validated storage-key construction |

**Import direction (Track 16A, load-bearing).** `cli` imports the six modules
above; none of them imports `cli`, at module scope *or* function scope. The
leaves (`consoles`, `conflictmtime`, `safety`, `conflictdiff`, `fsutil`) import
nothing from the CLI layer at all. Enforced by
`tests/test_module_boundaries.py` and a CI grep gate — ruff's F811 cannot see
function-local shadowing, so lint alone will never catch a re-introduced cycle.
`aggregator.py` reaches the CLI as a **subprocess**
(`sys.executable -m mind_meld.cli devices --format json`), never as an import.

Call moved symbols module-qualified (`resolveflow.foo(...)`), not via
from-import. A from-import binds `cli`'s own global, so patching the owner in a
test would not reach it — the dead-alias trap in reverse.

CONFLICT-TELEMETRY (`conflictlog.py`, the `_conflict_feature_dict` / `_emit_conflict_decision` / `_conflict_rel_path` helpers, and the hidden `mm conflict-log-backfill` command) was **removed in Track 16A**. It shipped 2026-07-30 as a disposable labeled-dataset collector for the deferred Phase 2 auto-resolver and collected zero decisions in the sixteen days it ran — `~/.config/mind-meld/conflict-decisions.jsonl` never existed on the fleet, so the ≥25-decision trigger never tracked and only the 60-day bar (~2026-09-28) would have fired, with no dataset. It was ripped out ahead of the `resolveflow.py` extraction rather than moved six weeks before its own deletion. Original design: `~/.gstack/projects/kbitz-mind-meld/kb-kbitz-conflict-resolution-log-design-20260730.md`. Do NOT reintroduce a collector without a trigger that demonstrably fires.
src/mind_meld/skills/retro_fleet/{SKILL.md,aggregator.py,__init__.py}  (Group 8 v0.11.0 — Claude Code skill orchestrator + Python aggregator. Dir on disk is `retro_fleet` (Python identifier, importable as `mind_meld.skills.retro_fleet`); the symlink installer creates `~/.claude/skills/retro-fleet` (hyphen — Claude Code naming convention). Since v0.12.38 that link points at the mm-owned store `~/.local/share/mind-meld/agent-skills/retro-fleet/`, which holds a COPY of `SKILL.md` only — never at this package dir, whose path dies with the interpreter that ran `mm`. `aggregator.py` stays in the wheel and is reached through `cli.py:retro_fleet_cmd`, so do NOT add it to the store. SKILL.md invokes the aggregator via `mm retro-fleet <window>` (typer wrapper at `cli.py:retro_fleet_cmd`, v0.11.22) — NOT `python -m mind_meld.skills.retro_fleet.aggregator`, because pipx installs hide mind_meld from any interpreter outside the pipx venv and macOS systems often only have `python3` (not `python`) on PATH. Ships via `packages = ["src/mind_meld"]` — do NOT add hatchling `force-include` for this subtree, it would double-ship.)

`safety.py` (v0.11.1) — peer-controlled string sanitization (`safe_str`, `safe_text`, `strip_terminal_escapes`); cli.py re-exports for backwards compat. New tests should import from `mind_meld.safety` directly. See `docs/invariants/init-devices.md`.

`conflictdiff.py` (v0.11.1, extended v0.12.10 / v0.12.31) — pure leaf primitives for the conflict prompts (`render_prompt`, `render_banner`, `render_capped_diff`, `count_divergent_lines`, plus v0.12.10 timestamp/recency: `format_ts`, `format_age_delta`, `newer_side`, `render_time_line`, `render_verdict`); site-level dispatch stays at each call site. The v0.12.10 `(n)ewer` shortcut is `mm resolve`-only and remaps to the existing `(l)`/`(r)` dispatch (NOT a new apply branch); the inline prompt is display+verdict only because `_apply_incoming_file` already skips when local is newer. See `docs/invariants/conflicts.md`.

`lockedjson.py` (v0.11.14, extended v0.12.22) — extracted single-file flock R/M/W primitive shared by `upgrade.py`, `token_usage.py`, and `identity.py` (v0.11.17). Its read-only shared-lock snapshot serves dry-run planners without creating, rewriting, re-permissioning, or normalizing a cache; R/M/W remains the exclusive mutation path. Three contention modes: `block` / `raise` / `warn`. Do NOT route new flock-guarded JSON caches through ad-hoc fcntl calls; extend `lockedjson` if the contract needs to grow. `devices-write.lock` stays ad-hoc — its multi-file lock-on-sibling shape doesn't fit the single-file R/M/W contract.

`token_usage.py` (v0.11.14, extended v0.11.24 / v0.11.27) — walks Claude Code session jsonls (parent + subagents) in ONE I/O pass producing two views: per-jsonl `message.usage` totals AND per-jsonl `tool_use` Skill-name counts. Both views land in the flock-guarded cache at `~/.config/mind-meld/session-tokens.json` (`by_day` + `skills_by_day` per entry). Subagent jsonls contribute to the parent project's token AND skill totals (parent attribution), but do NOT bump `sessions` / `total_kb` / `last_session_at`. The aggregator slices `tokens_by_day` and `skills_by_day` to the retro window. GC hook in `mm gc` reaps cache entries whose underlying jsonl is gone OR whose most recent `by_day` key is more than 90 days old. Mixed-fleet mode: aggregator's field-presence sniff flags pre-v0.11.14 peers OR cold-cache devices into `pre_token_peers`; both pre-v0.11.27 peers AND v0.11.27+ peers whose skill walk was skipped this push (cold cache + autopush, or warn-mode flock contention) land in `pre_skills_peers`. **D4 discriminator (load-bearing):** `pre_skills_peers` uses `"skills_by_day" not in proj` (key-absence), NOT a falsy-check — distinguishes the union "absent on the wire" cases from sessions that simply had no skill activity (KEY-PRESENT-VALUE-EMPTY). Wire genuinely can't tell pre-v0.11.27-peer apart from skipped-walk; the "Skills incomplete" breadcrumb in `aggregator.format_retro` admits the ambiguity, mirroring `pre_token_peers`'s "OR with cold token cache" phrasing (v0.12.4 post-/plan-eng-review 2026-05-10 — alternative "always set `{}` in events.py" fix was rejected because latest-snapshot-wins in `aggregator.aggregate_sessions` — the `latest = filtered_latest` reduction — would silently overwrite populated skill data with synthetic empty on warm-then-cold push ordering). Cache shape upgrade gate (D2): pre-v0.11.27 entries are detected by `"skills_by_day" not in entry` on the size/mtime cache hit and re-walked once; NOT a `CACHE_VERSION` bump (would invalidate token data). Cost estimation (v0.12.13): `resolve_prices` is the ONLY predicate for "is this model priced" — exact `PRICING` entry wins, else the `MODEL_FAMILY_TIERS` family fallback, else unpriced. Both consumers (`estimate_cost` and `aggregator._unpriced_token_summary`) share it; do NOT reintroduce a second `model in PRICING` test. `model_family` matches positionally against a literal allowlist because peer-controlled ids now drive a pricing decision. Public helpers (Track 10A v0.11.24): `TOKEN_FIELDS` constant + `zero_day_bucket` / `zero_model_bucket` factories + `merge_usage_bucket` / `merge_by_model` (single-bucket) + `merge_token_days` / `merge_skill_days` (whole per-day map, v0.12.15) consolidate the bucket-merge sites (token_usage, events, aggregator); `lock_and_get_files` context manager owns cache-shape invariants for the cli's events tail/backfill. Adding a 5th token field is a one-line change to `TOKEN_FIELDS`. Do NOT hand-roll a per-day merge loop at a new call site — `test_events_aggregator_uses_the_shared_helpers` fails the build if `events.py` regrows one. Incremental resume (v0.12.15): `walk_jsonl_segment` is the canonical parser (`walk_jsonl_buckets` is now a trimming full-file shim over it) and reads in BINARY mode so byte offsets are real and a bad UTF-8 byte skips one line instead of raising through the whole events tail. Cache entries carry `offset` / `head` / `tail_msg_ids` so a warm walk costs O(bytes appended since last push), not O(file size) — this is what fixed the recurring `mm: notice: events tail budget exceeded`; the walk budgets were NOT raised. `_resume_plan` is the single gate on using those fields and falls back to a full walk on any doubt. Absence of `offset`/`head` is the pre-v0.12.15 version discriminator (NOT a `CACHE_VERSION` bump, same reasoning as the D2 skills gate). Any change here must keep merged-incremental output identical to a single full walk. See `docs/invariants/events-retro.md`.

`identity.py` (v0.11.17) — owns the running machine's locally-known author-email set behind a flock-protected 7d-TTL cache. Push tail and retro render share state via this cache. `aggregator.gather_author_emails()` is now a thin shim. See `docs/invariants/events-retro.md`.

Storage keys are constructed via helpers in `storage/keys.py` (`manifest_key`, `blob_key`, `device_key`, `parse_blob_key`) which validate components at construction time — a corrupt or malicious peer manifest cannot smuggle a `sha256: "../../../etc/passwd"` through `backend.get`. Do NOT build storage keys with raw f-strings at new call sites.

Version source of truth: `pyproject.toml` (read by `__init__.py` via `importlib.metadata.version("mind-meld")`, fallback `"0.0.0+dev"` for uninstalled source-tree runs). No `VERSION` file.

## Testing
pytest. Use tmp_path for local backend. Run: `pytest tests/`

Lint/format enforced via ruff (pinned to `ruff==0.15.12` in `dev` deps). Run `ruff check .` and `ruff format --check .` locally before pushing — CI runs both as a separate `lint` job and will block merges on drift. Rule set: `E`/`F`/`W`/`I` (isort enforcement).

The PYTEST_CURRENT_TEST guard on `crypto.store_passphrase_in_keyring` (v0.11.11) is load-bearing — see `docs/invariants/init-devices.md` for the rationale.

## CI
GitHub Actions at `.github/workflows/ci.yml`. Single job on `macos-latest` + Python 3.13 (mind-meld is a macOS tool — multi-OS + multi-Python matrix is theater for this project). Runs ruff check + ruff format --check + pytest + wheel build + `mm --version` smoke. Asserts the real Keychain backend loads (guards against silent `fail.Keyring` fallback). pip cache keyed on `pyproject.toml`. No `paths:` filter — every PR runs CI (avoids the branch-protection pending-forever footgun for path-skipped required checks).

`.github/workflows/release.yml` (v0.11.24+, PROGRESS auto-append removed v0.11.26) — auto-tag + auto-create-Release on push to main, gated on changes to `pyproject.toml` or `CHANGELOG.md`. Each step independently idempotent (re-run-safe after partial failure). Tag detection branches on `git rev-parse "$tag"` and release detection on `gh release view "$tag"`. Bot identity is `github-actions[bot]`. The "Verify PROGRESS.md row exists" tail step emits a warning (not a failure) when the row is missing, so the release still ships and you see the gap.

**PROGRESS row convention (load-bearing).** The PROGRESS.md row goes in the SAME PR as the `pyproject.toml` + `CHANGELOG.md` bump — not a workflow side-effect. The original v0.11.24 design tried to auto-append via `git push` from the workflow, which was rejected by branch protection ("Changes must be made through a pull request") on every release where the row wasn't already in the PR. v0.11.23 only "succeeded" because the row was pre-added in the PR and the script's idempotent-skip exited 0 before the push. v0.11.24 and v0.11.27 both hit the wall and shipped without rows. Lesson: a workflow that pushes to a protected branch is broken by definition; don't reintroduce that step. The row format mirrors what the old auto-append produced — CHANGELOG body lead paragraph (text from `## [version]` to first `### Section` or next `## [`), pipes escaped, single line, inserted directly after the `|---|---|---|` separator (newest at top). **The row is now CI-enforced** (Track 16A): `tests/test_docs_routing.py::test_every_changelog_version_has_a_progress_row` fails any PR that bumps the version without adding the row, enforced from 0.11.0 forward. That closes the recurrence the v0.11.24 auto-append design could not — a workflow that pushes to a protected branch is broken by definition, but a test in the PR is not. Still does NOT solve parallel-workspace version collisions (two open PRs both claiming the same version slot) — that remains deferred.

## Commands
mm --version | init | push | pull | status | diag | devices | diff | gc | sources | conflicts | resolve | log | migrate-config | autopull | autopush | enable-source | disable-source | reconfigure-sources | refresh-identity | install-skills | retro-fleet

Pull flag: `--conflict-mode {prompt|keep-both|fail}` (default `keep-both`). `prompt` asks per-file; `fail` preflights via `_predict_pull_outcome` and exits 3 (no writes) if any file would conflict — for CI. Replaces the old `--no-prompt` / `--resolve-interactive` pair (v0.6.2 BREAKING).
GC flags: `--dry-run` (preview orphan blobs plus retention candidates without mutation; each executed reaper reports candidates, repairs, and skips); `--conflicts` (also reap `.sync-conflict-*` files older than 30 days).
Log flags: `--source NAME`, `--since DATE`, `--action {written|merged|skipped|conflicted|excluded|uploaded|failed}`, `--verb {pull|push}`, `--limit N`, `--format {jsonl|table}`.
Migrate-config flags: `--yes`, `--dry-run`. Idempotent: appends missing recommended `exclude_patterns` to existing `[[sync.sources]]` entries; preserves user-customized globs.

## Invariant pointer table

Load-bearing invariants live in `docs/invariants/<topic>.md`. Read the relevant file BEFORE editing the listed code. The tables below are file-path-keyed routing rules — match the file or function you're about to touch and read the named invariant doc(s) first.

| If you're editing… | READ FIRST |
|---|---|
| `cli.py:_pull_core` / `_push_core` / `_fetch_remote_manifest` / `_recover_prior_manifest` / `_filter_excluded_paths` / `_filter_disabled_sources` / `_drop_case_collisions_from_manifests` | `docs/invariants/sync.md` |
| `cli.py:_download_and_apply` / (rel_path + base_path concatenation site) | `docs/invariants/sync.md` |
| `manifest.py:walk_generic_source` / `walk_grok_source` / `load_manifest` / `_validate_rel_path` / `collect_tombstones` / `generate_tombstones` | `docs/invariants/sync.md` |
| `config.py` exclude_patterns / disabled_sources / `seen_sources.py` consumer paths | `docs/invariants/sync.md` |
| `pullhistory.py` (forensic log) | `docs/invariants/sync.md` |
| `cli.py:_apply_write` / `_apply_merge` / `_apply_conflict` / `_apply_incoming_file` (mtime restore + future-clamp) | `docs/invariants/sync.md` |
| `conflictmtime.py:_restore_mtime_best_effort` / `_MTIME_RESTORE_MAX_SKEW_SECONDS` (future-clamp) | `docs/invariants/sync.md` |
| `cli.py:_apply_conflict` / `_apply_incoming_file` / `_prompt_conflict_choice` / `_check_fleet_version_or_refuse` | `docs/invariants/conflicts.md` |
| `resolveflow.py:_resolve_interactive_loop` / `_find_conflict_files` / `_migrate_pre_inversion_conflict` / `_ensure_inversion_marker` / `_synced_scan_dirs` / `_canonical_for_conflict` / `_promote_target_path` / `_promote_conflict_file` / `_promote_target_will_sync` | `docs/invariants/conflicts.md` |
| `conflictmtime.py:_bump_canonical_mtime_post_resolve` / `_stat_mtime_btime` (both prompt sites share these) | `docs/invariants/conflicts.md` |
| `cli.py:_record_inline_bump` / `_invalidate_inline_bump` / `_drain_inline_bumps` / `_CANONICAL_WRITE_OUTCOMES` / `pending_inline_bumps` plumbing through `_pull_core` / `_pull_one_source` / `_download_and_apply` (outcome-gated invalidation) | `docs/invariants/conflicts.md` |
| `conflictdiff.py` (incl. `format_ts` / `format_age_delta` / `newer_side` / `render_time_line` / `render_verdict`) / `merge.py:lcs_merge` / `manifest.py:parse_conflict_device_short` | `docs/invariants/conflicts.md` |
| `cli.py:_register_and_save` / `_ensure_device_registered` / `init` / `_init_storage_guard` | `docs/invariants/init-devices.md` |
| `devices.py` / `storage/local.py:put_exclusive` | `docs/invariants/init-devices.md` |
| `safety.py` or any new print site interpolating peer-controlled strings | `docs/invariants/init-devices.md` |
| `crypto.py:store_passphrase_in_keyring` / keyring path | `docs/invariants/init-devices.md` |
| `events_tail.py:_run_events_tail` / `_run_events_backfill` / `_decide_token_walk_policy` / `_enabled_claude_paths` | `docs/invariants/events-retro.md` |
| `events_tail.py:_capture_host_usage` / `_default_host_readers` / `_host_skip_phrase` / `_warm_host_cache_with_notice` / `HostUsageCapture` / `HOST_USAGE_READ_BUDGET_*` / `WARMABLE_HOST_READERS` / `events.py:make_host_usage_snapshot` / `HostUsageSnapshot` / `HOST_USAGE_TOKEN_SOURCES` | `docs/invariants/events-retro.md` (host-usage-snapshot section) |
| `cli.py:PushResult.events_degradations` / the `autopush` breadcrumb outcome / `_breadcrumb_staleness_suffix` | `docs/invariants/events-retro.md` |
| `events.py:_read_cwd_from_latest_jsonl` / `_last_mm_push_ts` / `_scan_one_project` cwd-scan site / `walk_git_projects` future-collection blocks / `token_usage.is_cache_cold` / `token_usage.iter_bounded_lines` / `pullhistory._yield_lines` | `docs/invariants/events-retro.md` (tolerant-binary-reads + one-cwd-scan sections) |
| `skill_link.py:SkillTarget` / `SkillInstallResult` / `_ensure_retro_skill_link*` / `_skill_link*_check_due*` / `_resolve_retro_skill_src` / `_marker_dir` / `_marker_exists` / `AGENT_ROWS` / `_descriptor_for` / `_real_guard_paths` / `_refuse_real_home_under_pytest` / `skill_targets` | `docs/invariants/events-retro.md` |
| `skill_link.py:_skill_store_dir` / `_publish_skill_store` / `_prepare_store_dir` / `_should_publish` / `_store_needs_refresh` / `_store_is_healthy` / `_read_store_meta` / `_reject_payload_symlink` / `_store_publish_lock` / `_legacy_shape` / `_points_at_store` / `_symlink_lives` / `_replace_symlink` | `docs/invariants/events-retro.md` |
| `skill_link.py:diagnose_skill_links` / `_diagnose_one` / `render_skill_status` / `_emit_status_notice` / `BROKEN_SKILL_STATUSES` / `SkillInstallStatus` | `docs/invariants/events-retro.md` |
| `skill_link.py:consented_agent_keys` / `_row_is_consented` / `AgentRow.consent_source` / `_owned_store_exists` | `docs/invariants/events-retro.md` |
| `config.py:_validate_skills` / `_validate_str_list` | `docs/invariants/events-retro.md` |
| `cli.py:install_skills_cmd` / `retro_fleet_cmd` (typer shells only) | `docs/invariants/events-retro.md` |
| `cli.py:status` / `diag` / `_collect_diag_state` (their `skill_link.diagnose_skill_links` consumers) | `docs/invariants/events-retro.md` |
| `host_skill_discovery.py:probe_grok_skill_discovery` | `docs/invariants/events-retro.md` |
| `cli.py:refresh_identity_cmd` / `devices` (its `--format json` path) | `docs/invariants/events-retro.md` |
| `retention.py:EVENTS_RETENTION_DAYS` / `CONFLICT_AGE_DAYS` / `_gc_old_event_files` / `_gc_old_conflict_files` / `_gc_token_cache` / `_sweep_local_tmp_files` / `_gc_orphan_retros_dir` | `docs/invariants/events-retro.md` |
| `events.py` / `identity.py` / `token_usage.py` | `docs/invariants/events-retro.md` |
| `host_usage.py` (incl. `read_codex_usage` / `read_grok_usage` / `grok_completed_once` / `warm_host_cache_inline` / `_scan_codex_root` / `_scan_grok_root` / `_read_rollout` / `_carries_usage` / `_no_ledger_entry`) | `docs/invariants/events-retro.md` |
| `token_usage.py:PRICING` / `MODEL_FAMILY_TIERS` / `resolve_prices` / `model_family` / `estimate_cost` / `_CACHE_WRITE_MULT` | `docs/invariants/events-retro.md` (cost-estimation section) |
| `token_usage.py:walk_jsonl_segment` / `walk_jsonl_buckets` / `iter_bounded_lines` / `_drain_to_newline` / `get_or_compute` / `_resume_plan` / `head_fingerprint` / `head_probe_len` / `_carry_tail_ids` / `merge_token_days` / `merge_skill_days` / `TAIL_MSG_ID_LOOKBACK` / `_HEAD_PROBE_BYTES` / `_MAX_TAIL_MSG_ID_LEN` | `docs/invariants/events-retro.md` (incremental-resume section) |
| `aggregator.py:_render_token_block` / `_unpriced_token_summary` / `_short_model_name` / `_format_usd` | `docs/invariants/events-retro.md` (cost-estimation section) |
| `skills/retro_fleet/aggregator.py` (incl. `aggregate_host_usage` / `_accept_host_usage_snapshot` / `_render_ascii_card` / `_aggregate_git_period_pair` / `_classify_commit_subject` / `_detect_bursts` / `_safe_prose`) | `docs/invariants/events-retro.md` |
| `skills/retro_fleet/aggregator.py` 23A renderers (`AgentRhythmView` / `_agent_rhythm_view` / `_render_agent_block` / `_render_agent_inventory` / `_agent_coverage_notes` / `_agent_state_label` / `_window_day_keys` / `AGENT_FAMILY_ROWS` / `MAX_AGENT_INVENTORY_MACHINES` / `_render_models_block`) and `token_usage.sum_bucket` | `docs/invariants/events-retro.md` (Track 23A renderer contract) |
| `skills/retro_fleet/SKILL.md` (two-pass card flow; `## Step 0: preflight` and its terminal rule) | `docs/invariants/events-retro.md` |
| `config.py:MM_INTERNAL_SOURCE_NAMES` / `_bootstrap_mm_events_path` / `DEFAULT_SOURCES` mm-events entry | `docs/invariants/events-retro.md` |
| `upgrade.py` / `cli.py` upgrade hook seams / `pullhistory.py:append_self_upgrade` | `docs/invariants/auto-upgrade.md` |
| `pyproject.toml` version bump / tagging | `docs/invariants/auto-upgrade.md` |

If you're touching multiple areas (e.g., adding a new field to mm-push event that also flows through aggregator + adds a CLI flag), read every applicable invariant file. They're short; bulk-reading is cheap. The cost of skipping one and breaking a load-bearing invariant is much higher.

## Auto Commands (for Claude Code integration)
- `mm autopull` — silent pull, one-line output, never prompts, graceful on errors
- `mm autopush` — silent push, one-line output, never prompts, graceful on errors
- Both exit silently if mm is not initialized (no config) or no changes exist
- `ConfigError` (bad `config.toml`) surfaces as a one-line stderr message — not a silent exit. This is the visible-failure contract: truly unexpected errors still degrade silently via the generic `except Exception` fallback, but malformed config is loud so users don't wedge their background sync without noticing. Relies on `load_config` normalizing non-`ConfigError` exceptions (e.g. cyclic-symlink `.resolve()` failures) into `ConfigError` at the load boundary — do not bypass that by calling `_validate` / `_apply_defaults` directly from a new call site.
- **Load-bearing warnings reach stderr even in quiet mode (v0.8.1).** The visible-failure contract extends beyond `ConfigError` to a curated set of degradation signals that quiet-mode used to swallow: corrupt-manifest sidecar recovery, corrupt-manifest peer-fallback recovery, "no sync sources" misconfig in autopush, durability `fsync_dir` failure on pull, and the autopull `total_failed` per-file summary. Each emits a single `mm: warning: ...` (or `mm: ...`) line to stderr and continues. Do NOT add a new `if not quiet:` gate around a warning that signals data-at-risk degradation — match the established pattern (always-stderr, prefixed `mm:`).
- **`autopush` writes a `no-sources` breadcrumb (v0.8.1)** when `get_sources(config)` returns empty, distinguishing "broken config no-op" from "nothing to push" no-op. Without this, `mm status` only sees `outcome: "success"` forever and monitoring on top of it never catches the wedge.
- **`autopush` writes a `degraded` breadcrumb (v0.12.16)** when the events tail lost data on an otherwise-successful push: tail raised, walk budget exceeded, or no token/skill data published (cold or flock-contended cache). `_run_events_tail` returns `list[str]` of reasons, `_push_core` carries them on `PushResult.events_degradations`, `autopush` joins them into `detail`. Same argument as the `no-sources` breadcrumb, applied to the other silent path — the tail is forensic-only and its `mm: notice:` lines go to an unattended hook's stderr. **Any new degradation detected in the tail MUST be appended to that list, not merely printed.** Note `_decide_token_walk_policy` returning `False` does NOT by itself mean degradation — it also returns `False` when no `claude` source is enabled, which is a config shape; the append is gated on `claude_paths`. See `docs/invariants/events-retro.md`.
- See README.md "Claude Code Integration" section for CLAUDE.md snippet

## Spec
See SPEC.md for full architecture and data model. Two sections of it are deliberately historical and say so inline: the `## Project Structure` tree and `## Implementation Order` are the original v1 build plan. **The Source Layout table above is the authoritative current module map**, not SPEC's tree or its `### Module Architecture` diagram (core sync path only).
See docs/designs/mind-meld-v1.md for design decisions from spec review.
See docs/designs/sync-gstack-context.md for multi-source sync design (gstack support).
See docs/designs/host-parity.md for Claude / Codex / Grok interchangeability (usage card vs sync vs sessions snapshot).
See docs/designs/grok-build-usage-reader.md for the Grok v1 usage reader and 21A consent bit.
See docs/invariants/ for per-topic load-bearing invariants (sync, conflicts, init-devices, events-retro, auto-upgrade).
See docs/ROADMAP.md for the state-organized execution plan, docs/TODOS.md for the deferred-work inbox, and docs/PROGRESS.md for the per-release row (CI-enforced — see the PROGRESS row convention above). docs/archive/ holds superseded design docs.
