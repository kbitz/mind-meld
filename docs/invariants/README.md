# Module map and invariant routing

Paths below are relative to the repository root. Before editing code, find its
file or function here and read every invariant doc named by the matching rows.

## Source Layout

One line per module, with what lives there. Grep this table for a filename
before grepping the code. (It used to be a `{a,b,c}.py` brace-expansion
one-liner, which does not match a search for `resolveflow.py`.)

| Module | Owns |
|---|---|
| `cli.py` | Every `@app.command()` shell, `_pull_core` / `_push_core`, (64A) attended host-usage capture inside the push lock (`_capture_attended_usage`) and its publication reporting, the `_apply_*` family, `init`, `status`, `diag`, the `autopull`/`autopush` pair |
| `pullplan.py` | (62A) Read-only virtual local state, pull predictions across peers, symlink/collision/mtime decisions and preview totals; never selects real downloads |
| `manifest.py` | Manifest build/load/diff, rel-path validation, conflict-filename predicates, `_canonical_for_conflict`, tombstones |
| `crypto.py` | AES-256-GCM envelope, argon2 KDF, keyring, crypto-init bootstrap |
| `config.py` | `config.toml` load/validate/save, `DEFAULT_SOURCES`, exclude patterns, (64A) the five-state `usage_capture_readiness` verdict and its shared `usage_capture_remedy` text |
| `devices.py` | Device registry, short-id generation and lookup |
| `events.py` | mm-events log: git-root discovery, git/session walkers, budgets; (61A) shared day scan and publication projection; (64A) size- and origin-guarded `write_push_event` (`EventAppendSkipped`, `GIT_SNAPSHOT_ORIGIN_INIT`) |
| `token_usage.py` | Session-jsonl walker, token + skill caches, pricing, incremental resume |
| `host_usage.py` | Local Codex and Grok (`updates.jsonl` terminal records, opt-in) usage readers, the Cursor reader (Conductor `runs.ndjson` plus standalone completions), strict host-family classifier, and isolated host-token caches; also edits mm's single `stop` entry in the third-party `~/.cursor/hooks.json` (`configure_cursor_hook`) |
| `host_skill_discovery.py` | Read-only `grok inspect --json` probe for `mm diag` (`host_skill_discovery` sibling key). Not a `skill_link` registry. |
| `gitenv.py` | Repository-local Git environment scrub and stable message locale |
| `identity.py` | Author-email set behind a flock-guarded 7d-TTL cache |
| `merge.py` | Merge dispatch (`.jsonl`, `MEMORY.md`) + `lcs_merge` 3-way merge |
| `upgrade.py` | Release check, nudge, transition hook; (v1.3.0) self-update: install classification (`detect_install`), the two pipx subprocess seams, the one-attempt-per-release gate, `update_or_nudge`; `mm update` progress capture: the private PTY for the in-place upgrade (`_PipxOutputStream`), the best-effort `_ProgressRelay`, the foreground hangup guard and `_final_frames` |
| `updateprogress.py` | Read-only installer-output parser for `mm update` progress: measured download/Git/package counters and fixed phase labels; no timing estimates |
| `pullhistory.py` | Forensic per-file pull log |
| `seen_sources.py` | First-seen source tracking for the enable/disable prompts |
| `synclog.py` | Per-project `.mind-meld-log.md` writer |
| `sidecar.py` | Manifest sidecar read/write |
| `attemptlog.py` | (65A) Local attended-capture outcome holder, atomic private record, closed-vocabulary validation, and write-free attempt projection/render states |
| `lockfile.py` | The mm lockfile |
| `lockedjson.py` | Single-file flock read/modify/write primitive |
| `fsutil.py` | Atomic write, flock-append (61A opt-in strict outcome; (64A) whole-batch size ceiling, torn-row separator, regular-files-only), the rotatable spool pair `append_rotatable_jsonl` / `rotate_jsonl` (identity-checked append, rename-aside rotation), `fsync_dir` |
| `errors.py` | Exception hierarchy |
| **`consoles.py`** | **(16A)** The two shared Rich `Console` singletons |
| **`conflictmtime.py`** | **(16A)** mtime primitives shared by the apply path and the resolver |
| **`skill_link.py`** | **(16A)** retro-fleet skill installer, the mm-owned `SKILL.md` store at `~/.local/share/mind-meld/agent-skills/` (24A), its 24h drift gate and markers, the `mm status` / `mm diag` link diagnosis, the `AGENT_ROWS` registry — add a new agent HERE — (25C) `consented_agent_keys` / `AgentRow.consent_source` / the installer `declined` status, and (28A) the deletion guard — `_marker_exists`, the `removed-by-user` status, and the rule that an ABSENT link is intent, not damage |
| **`events_tail.py`** | **(16A)** The push/init mm-events tail, its walk budgets, (19A) the host-usage capture, (61A) shared host-only capture/warm helper, (31A) reader-scoped failure isolation, (30A) git-only recapture, and (57A) the later-reader grace floor with full-reader warm/retry |
| **`resolveflow.py`** | **(16A)** Conflict discovery, promotion, the interactive `mm resolve` walk |
| **`retention.py`** | **(16A)** The `mm gc` reapers + crashed-push tmp sweep |
| `safety.py` | Peer-controlled string sanitization |
| `conflictdiff.py` | Pure leaf renderers for the conflict prompts |
| `storage/{local,keys}.py` | Local backend + validated storage-key construction |

**Import direction (Track 16A, load-bearing).** `cli` imports the six modules
above; none of them imports `cli`, at module scope *or* function scope. The
leaves (`consoles`, `conflictmtime`, `safety`, `conflictdiff`, `fsutil`,
`host_skill_discovery`, `gitenv`, `pullplan`, `attemptlog`, `updateprogress`) import nothing from the CLI layer at all. Enforced by
`tests/test_module_boundaries.py` — ruff's F811 cannot see
function-local shadowing, so lint alone will never catch a re-introduced cycle.
`aggregator.py` reaches the CLI as a **subprocess**
(`sys.executable -m mind_meld.cli devices --format json`), never as an import.

Call moved symbols module-qualified (`resolveflow.foo(...)`), not via
from-import. A from-import binds `cli`'s own global, so patching the owner in a
test would not reach it — the dead-alias trap in reverse.

## Invariant pointer table

Load-bearing invariants live in `docs/invariants/<topic>.md`. Read the relevant file BEFORE editing the listed code. The tables below are file-path-keyed routing rules — match the file or function you're about to touch and read the named invariant doc(s) first.

| If you're editing… | READ FIRST |
|---|---|
| `attemptlog.py` / `events.py:host_reader_outcomes` / `empty_host_readers` / `host_reader_label` / `EventScan` / `RowRevision` / `cli.py:_usage_publication_verdict` / `_publication_remedy` / `push` (attempt finally) / `aggregator.py:_AcceptedHostRow.empty_sources` | `docs/invariants/events-retro.md` and `docs/invariants/sync.md` |
| `config.py:UsageCaptureReadiness` / `usage_capture_readiness` / `usage_capture_remedy` / `cli.py:_reader_capture_readiness` / `_usage_capture_needs_upgrade` / `_usage_capture_remedy` / `_print_usage_push_mode` / `_push_result_or_none` / `PushResult.content_changed` / `PushResult.content_files` / `PushResult.content_accepted` / `PushResult.host_usage_published` / `events.py:GIT_SNAPSHOT_ORIGIN_INIT` / `write_push_event` (batch origin guard) / `events_tail.py:_capture_event_snapshots` (`origin`) / `_run_events_tail` (`capture_activity`) / `cli.py:_push_core` (`attended`, `host_row_appended`, `capture_activity`, content gate) | `docs/invariants/events-retro.md` and `docs/invariants/sync.md` |
| `pullplan.py` / `cli.py:_plan_pull` / `_preflight_conflicts` / `_print_pull_prediction` / preview completion/refusal constants / `diff_cmd` exclude filtering | `docs/invariants/sync.md` |
| `seen_sources.py:read` / `_read_under_lock` / status seed recovery and exemptions | `docs/invariants/sync.md` |
| `cli.py:_capture_attended_usage` / `_report_usage_publication` / `_read_capture_rows` / `_host_publication` / `_print_host_publication` / `_notice_recapture_host_usage` / `events.py:latest_event_rows` / `project_host_publication` / `capture_revision_in_manifest` / `recorded_row_revision` / `events_tail.py:_capture_host_snapshot` / `skills/retro_fleet/aggregator.py:ATTENDED_USAGE_MIN_VERSION` / `_attended_usage_remedy` / `local_host_capture_candidate` / `_host_row_order_key` / `_accept_host_row_at` | `docs/invariants/events-retro.md` and `docs/invariants/sync.md` |
| `fsutil.py:flock_append_jsonl` / `AppendSizeLimit` / `events.py:write_push_event` (strict append, `EventAppendSkipped`, size guard) / `cli.py:_push_core` (inline manifest acceptance) | `docs/invariants/events-retro.md` and `docs/invariants/sync.md` |
| `cli.py:_pull_core` / `_push_core` / `_fetch_remote_manifest` / `_recover_prior_manifest` / `_filter_excluded_paths` / `_filter_disabled_sources` / `_drop_case_collisions_from_manifests` | `docs/invariants/sync.md` |
| `cli.py:_download_and_apply` / (rel_path + base_path concatenation site) | `docs/invariants/sync.md` |
| `cli.py:_ApplyReporter` / `_first_existing_ancestor` / `_pull_one_source` / `_record_source_bookkeeping` / `_fsync_touched_parents` / `_PerSourceResult` / `_print_pull_summary` / `pullplan._predict_pull_outcome` (publication ledger, recovery, and count wording) | `docs/invariants/sync.md` |
| `cli.py:_warn_apply_failure` / `_print_apply_warning` / `errors.py:PULL_FAILURES_URL` (plain stderr uses `safety.safe_terminal_str` on every dynamic field) | `docs/invariants/sync.md` and `docs/invariants/init-devices.md` |
| `cli.py:_ApplyReporter` / (publication-time deferred-bump invalidation) | `docs/invariants/conflicts.md` |
| `cli.py:autopull` / `status` (failed-file count and breadcrumb detail) | `docs/invariants/sync.md` |
| `manifest.py:walk_generic_source` / `walk_grok_source` / `load_manifest` / `_validate_rel_path` / `collect_tombstones` / `generate_tombstones` / `marker_skip_globs` | `docs/invariants/sync.md` |
| `config.py` exclude_patterns / disabled_sources / `seen_sources.py` consumer paths | `docs/invariants/sync.md` |
| `config.py:_GENERATED_HOST_SKILL_GLOBS` / the `DEFAULT_SOURCES` `exclude_patterns` lists (adding or removing a glob) | `docs/invariants/sync.md` (generated-files section) |
| `pullhistory.py` (forensic log) | `docs/invariants/sync.md` |
| `cli.py:_apply_write` / `_apply_merge` / `_apply_conflict` / `_apply_incoming_file` (mtime restore + future-clamp) | `docs/invariants/sync.md` |
| `conflictmtime.py:_restore_mtime_best_effort` / `_MTIME_RESTORE_MAX_SKEW_SECONDS` (future-clamp) | `docs/invariants/sync.md` |
| `cli.py:_apply_conflict` / `_apply_incoming_file` / `_prompt_conflict_choice` / `_check_fleet_version_or_refuse` / `conflict_filename` / `_filter_excluded_paths` | `docs/invariants/conflicts.md` |
| `retention.py:_gc_old_conflict_files` / `_is_live_conflict` | `docs/invariants/conflicts.md` |
| `resolveflow.py:_resolve_interactive_loop` / `_find_conflict_files` / `_migrate_pre_inversion_conflict` / `_ensure_inversion_marker` / `_synced_scan_dirs` / `_promote_target_path` / `_promote_conflict_file` / `_promote_target_will_sync` | `docs/invariants/conflicts.md` |
| `conflictmtime.py:_bump_canonical_mtime_post_resolve` / `_stat_mtime_btime` (both prompt sites share these) | `docs/invariants/conflicts.md` |
| `cli.py:_record_inline_bump` / `_invalidate_inline_bump` / `_drain_inline_bumps` / `_CANONICAL_WRITE_OUTCOMES` / `pending_inline_bumps` plumbing through `_pull_core` / `_pull_one_source` / `_download_and_apply` (outcome-gated invalidation) | `docs/invariants/conflicts.md` |
| `conflictdiff.py` (incl. `format_ts` / `format_age_delta` / `newer_side` / `render_time_line` / `render_verdict` / `merge_has_line_structure`) / `merge.py:lcs_merge` / `merge_jsonl` / `_extract_ts` / `manifest.py:_canonical_for_conflict` / `parse_conflict_device_short` / `parse_conflict_created_at` / `is_v1_conflict_filename` / either prompt site's `merge_available` computation | `docs/invariants/conflicts.md` |
| `cli.py:_register_and_save` / `_ensure_device_registered` / `init` / `_init_storage_guard` / `_do_gc` / (its peer-controlled blob-key prints) | `docs/invariants/init-devices.md` |
| `devices.py` / `storage/local.py:put_exclusive` / `find_conflict_copies` | `docs/invariants/init-devices.md` |
| `safety.py` or any new print site interpolating peer-controlled strings | `docs/invariants/init-devices.md` |
| `crypto.py:store_passphrase_in_keyring` / keyring path | `docs/invariants/init-devices.md` |
| `crypto.py:apply_crypto_init_repair` / `CryptoInitCandidate` / `crypto_init_repair_counts` / `storage/local.py:_needs_fsync` (crypto-init durability) | `docs/invariants/init-devices.md` |
| `upgrade.py:cached_upgrade_view` / `cli.py:status` (cached upgrade view) | `docs/invariants/auto-upgrade.md` |
| `cli.py:_detect_case_insensitive_fs` / `_detect_pull_case_collisions` | `docs/invariants/sync.md` |
| `cli.py:_get_config` / `_init_crypto_session` / `_maybe_prompt_migration` (`read_only=` / `dry_run=` gates) / `crypto.py:fetch_crypto_init` / `CryptoInitRepairPlan` (pure fetch and bound repair plan) | `docs/invariants/sync.md` and `docs/invariants/init-devices.md` |
| `events_tail.py:_run_events_tail` / `_run_events_backfill` / `_prepare_recapture` / `_decide_token_walk_policy` / `_enabled_claude_paths` | `docs/invariants/events-retro.md` |
| `events_tail.py:_capture_host_usage` / `_default_host_readers` / `_host_skip_phrase` / `HostReadEvidence` / `host_read_age` / `resolve_host_read_budget` / `_warm_host_cache_with_notice` / `HostUsageCapture` / `_merge_host_usage_maps` / `_merge_warm_retry_capture` / `HOST_USAGE_READ_BUDGET_*` / `HOST_READER_GRACE_MS` / `WARMABLE_HOST_READERS` / `events.py:make_host_usage_snapshot` / `HostUsageSnapshot` / `ACTIVE_HOST_READERS` / `HOST_USAGE_TOKEN_SOURCES` / `config.py:HOST_USAGE_BUDGET_MAX_MS` | `docs/invariants/events-retro.md` (host-usage-snapshot section) |
| `cli.py:PushResult.events_degradations` / the `autopush` breadcrumb outcome / `_breadcrumb_staleness_suffix` | `docs/invariants/events-retro.md` |
| `events.py:_read_cwd_from_latest_jsonl` / `_iter_mm_push_objs` / `_scan_one_project` cwd-scan site / `walk_git_projects` future-collection blocks / `token_usage.is_cache_cold` / `token_usage.iter_bounded_lines` / `pullhistory._yield_lines` | `docs/invariants/events-retro.md` (tolerant-binary-reads + one-cwd-scan sections) |
| `skill_link.py:SkillTarget` / `SkillInstallResult` / `_ensure_retro_skill_link*` / `_skill_link*_check_due*` / `_resolve_retro_skill_src` / `_marker_dir` / `_marker_exists` / `AGENT_ROWS` / `_descriptor_for` / `_real_guard_paths` / `_refuse_real_home_under_pytest` / `skill_targets` | `docs/invariants/events-retro.md` |
| `skill_link.py:_skill_store_dir` / `_publish_skill_store` / `_prepare_store_dir` / `_should_publish` / `_store_needs_refresh` / `_store_is_healthy` / `_read_store_meta` / `_reject_payload_symlink` / `_store_publish_lock` / `_legacy_shape` / `_points_at_store` / `_symlink_lives` / `_replace_symlink` | `docs/invariants/events-retro.md` |
| `skill_link.py:diagnose_skill_links` / `_diagnose_one` / `render_skill_status` / `_emit_status_notice` / `BROKEN_SKILL_STATUSES` / `SkillInstallStatus` | `docs/invariants/events-retro.md` |
| `skill_link.py:consented_agent_keys` / `_row_is_consented` / `AgentRow.consent_source` / `_owned_store_exists` | `docs/invariants/events-retro.md` |
| `config.py:_validate_skills` / `_validate_str_list` | `docs/invariants/events-retro.md` |
| `cli.py:install_skills_cmd` / `retro_fleet_cmd` (typer shells only) | `docs/invariants/events-retro.md` |
| `cli.py:status` / `diag` / `_collect_diag_state` (their `skill_link.diagnose_skill_links` consumers) | `docs/invariants/events-retro.md` |
| `host_skill_discovery.py:probe_grok_skill_discovery` | `docs/invariants/events-retro.md` |
| `cli.py:refresh_identity_cmd` / `devices` (its `--format json` path) | `docs/invariants/events-retro.md` |
| `cli.py:cursor_agent` / `capture_cursor_usage` / `_toggle_cursor_usage` / `_RawArgsCommand` / `_forwarding_signals` / `host_usage.py:configure_cursor_hook` / `record_cursor_usage` / `cursor_cli_model` / `cursor_hook_state` / `_take_cursor_spool` / `_fold_cursor_spool` / `fsutil.py:append_rotatable_jsonl` / `rotate_jsonl` | `docs/invariants/events-retro.md` (standalone Cursor capture) and `docs/invariants/sync.md` (atomic write publication failures) |
| `cli.py:recapture` / `events_tail.py:_prepare_recapture` / `events.py:resolve_push_cursor` / `capture_advances_cursor` / `make_git_capture` | `docs/invariants/events-retro.md` |
| `retention.py:EVENTS_RETENTION_DAYS` / `CONFLICT_AGE_DAYS` / `_gc_old_event_files` / `_gc_old_conflict_files` / `_is_live_conflict` / `_gc_token_cache` / `_sweep_local_tmp_files` / `_gc_orphan_retros_dir` | `docs/invariants/events-retro.md` |
| `gitenv.py:scrubbed_git_env` / `GIT_REPO_LOCAL_ENV_VARS` / `events.py:_walk_one_repo` / `_origin_remote_url` / `identity.py:_gather_global_email` / `_gather_per_repo_emails` / `read_cached_identities` / `_normalize_cache` | `docs/invariants/events-retro.md` |
| `events.py` / `identity.py` / `token_usage.py` | `docs/invariants/events-retro.md` |
| `host_usage.py` (incl. `read_codex_usage` / `read_grok_usage` / `grok_completed_once` / `grok_usage_diag` / `_count_two_level_ledgers` / `warm_host_cache_inline` / `_scan_codex_root` / `_scan_grok_root` / `_read_rollout` / `_carries_usage` / `_no_ledger_entry`) | `docs/invariants/events-retro.md` |
| `host_usage.py:_grok_turns_from_record` / `_classify_grok_update` / `_validate_grok_counters` / `_GROK_REQUIRED_KEYS` / `_GROK_IGNORABLE_KEYS` / `GROK_USAGE_CENSUS_HOST_VERSION` / `_validated_grok_entry` / `_grok_file_entry` / `_validated_grok_partial_days` / `_grok_partial_days` / `HostUsageResult.partial_days` | `tests/fixtures/host_sessions/grok/CONTRACT.md` and `docs/invariants/events-retro.md` (coverage states) |
| `host_usage.py:PERMANENT_REASONS` / `PERSISTABLE_REASONS` / `events_tail.py:_HOST_PERMANENT_REASONS` | `docs/invariants/events-retro.md` (standing read blockers) |
| `host_usage.py:_carry_reason` / `_carry_read_timing` / `_cached_read_timing` / `_cached_read_ms` / `_skip_failed_cache_write` / `_pause_gc` / `_cached_last_reason` / `_cached_reason_since` / (both cache roots' last_reason and last_reason_since fields) / `cli.py:status` / `diag` / `_host_usage_blocker` / `_host_read_budgets` / `_host_complete_read_line` / `_host_read_sweep_line` | `docs/invariants/events-retro.md` (standing read blockers) |
| `events.py:make_host_usage_snapshot` (`partial_sources` / `degraded_sources` disjointness / `COUNTER_SEMANTICS_DISJOINT_V1`) / `events_tail.py:HostUsageCapture` / `_merge_partial_days` / `_canonical_partial` / `_merge_warm_retry_capture` | `docs/invariants/events-retro.md` (coverage states) |
| `skills/retro_fleet/aggregator.py:_accept_optional_source_list` / `_sibling_tie_key` / `_agent_coverage_notes` / `_host_reader_coverage_notes` / `_dump_host_inventory` / `_project_git_capture` / `_uncovered_intervals` / `aggregate_git` (`origin` guard) | `docs/invariants/events-retro.md` (coverage states) |
| `token_usage.py:PRICING` / `MODEL_FAMILY_TIERS` / `PRICING_FAMILY_BY_MODEL` / `VENDOR_FAMILY_TIERS` / `VENDOR_LONG_CONTEXT_TIERS` / `resolve_prices` / `resolve_long_context_prices` / `model_family` / `estimate_cost` / `_cost_under` / `_CACHE_WRITE_MULT` | `docs/invariants/events-retro.md` (cost-estimation section) |
| `token_usage.py:walk_jsonl_segment` / `walk_jsonl_buckets` / `iter_bounded_lines` / `_drain_to_newline` / `get_or_compute` / `_resume_plan` / `head_fingerprint` / `head_probe_len` / `_carry_tail_ids` / `merge_token_days` / `merge_skill_days` / `TAIL_MSG_ID_LOOKBACK` / `_HEAD_PROBE_BYTES` / `_MAX_TAIL_MSG_ID_LEN` | `docs/invariants/events-retro.md` (incremental-resume section) |
| `aggregator.py:_agent_row_cost` / `agent_row_floor_causes` / `_unpriced_token_summary` / `_short_model_name` / `_format_usd` / `_format_usd_short` / `_long_context_cause` | `docs/invariants/events-retro.md` (cost-estimation section) |
| `events.py:_cap_by_model` / `_model_rank` / `_copy_tokens_by_day` / `MAX_HOST_MODELS_PER_DAY` / `MAX_HOST_MODELS_PER_ROW` (cap runs AFTER the day trim; mirrors the aggregator constants) | `docs/invariants/events-retro.md` |
| `skills/retro_fleet/aggregator.py` (incl. `aggregate_host_usage` / `_accept_host_usage_snapshot` / `_render_ascii_card` / `_aggregate_git_period_pair` / `_classify_commit_subject` / `_detect_bursts` / `_safe_prose`) | `docs/invariants/events-retro.md` |
| `skills/retro_fleet/aggregator.py` unified-agents surface (`FleetAgentRow` / `FleetAgentUsage` / `aggregate_agent_usage` / `_detect_duplicate_ledgers` / `_host_family_day_tuples` / `_render_agents_table` / `_render_agents_card_block` / `AGENT_ROW_ORDER` / `AgentRhythmView` / `_agent_rhythm_view` / `_agent_coverage_notes` / `_window_day_keys` / `device_labels` / `device_label`) and `token_usage.sum_bucket` | `docs/invariants/events-retro.md` (unified-agents renderer contract) |
| `skills/retro_fleet/aggregator.py:_render_health_block` / `_health_summary_line` / every `note(<code>, …)` call in `format_retro` / `SKILL_MIN_VERSION` | `docs/invariants/events-retro.md` (health-payload contract) |
| `skills/retro_fleet/SKILL.md` (two-pass card flow; `## Step 0: preflight` and its terminal rule) | `docs/invariants/events-retro.md` |
| `config.py:MM_INTERNAL_SOURCE_NAMES` / `_bootstrap_mm_events_path` / `_preview_mm_events_bootstrap` / `resolve_sources` / `get_sources` (`bootstrap=` gate, `SourceResolution.would_create`) / `DEFAULT_SOURCES` mm-events entry | `docs/invariants/events-retro.md` |
| `upgrade.py` / `cli.py` upgrade hook seams / `pullhistory.py:append_self_upgrade` | `docs/invariants/auto-upgrade.md` |
| `cli.py:update` / `_update_refusal` / `_run_update_with_progress` / `upgrade.py:update_or_nudge` / `detect_install` / `update_argv` / `run_update` / `_claim_install_attempt` / `_attempt_state` / `_run_pipx` / `_PipxOutputStream` / `_pipx_output_stream` / `_hangup_ignored` / `_ProgressRelay` / `_final_frames` / `_spawn_pipx` / `auto_install_enabled` / `updateprogress.py:PipxProgressParser` / `config.py:_apply_defaults` (the `[upgrade]` keys) | `docs/invariants/auto-upgrade.md` (self-update section) |
| Command/option names, positional arguments, exit codes, format constants, host families, token fields/order, device-registry fields, machine-readable output | `docs/invariants/auto-upgrade.md` “Compatibility (1.x)” |
| `pyproject.toml` version bump / tagging | `docs/invariants/auto-upgrade.md` |

If you're touching multiple areas (e.g., adding a new field to mm-push event that also flows through aggregator + adds a CLI flag), read every applicable invariant file. They're short; bulk-reading is cheap. The cost of skipping one and breaking a load-bearing invariant is much higher.

