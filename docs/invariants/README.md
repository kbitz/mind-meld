# Module map and invariant routing

Module paths below are relative to `src/mind_meld/` (a bare `aggregator.py` is
`skills/retro_fleet/aggregator.py`); invariant docs, tests, fixtures and other
repository files are relative to the repository root. Before editing code, find its file or
function here and read every invariant doc named by the matching rows.

## Source Layout

One line per module, with what lives there. Grep this table for a filename
before grepping the code. (It used to be a `{a,b,c}.py` brace-expansion
one-liner, which does not match a search for `resolveflow.py`.)

| Module | Owns |
|---|---|
| `cli.py` | Every `@app.command()` shell, `_pull_core` / `_push_core`, (64A) attended host-usage capture inside the push lock (`_capture_attended_usage`) and its publication reporting, the `_apply_*` family, `init`, `status`, `diag`, the `autopull`/`autopush` pair |
| `pullplan.py` | (62A) Read-only virtual local state, pull predictions across peers, symlink/collision/mtime decisions and preview totals; never selects real downloads |
| `manifest.py` | Manifest build/load/diff, rel-path validation, conflict-filename predicates, `_canonical_for_conflict`, tombstones, nested-checkout detection and skip prefixes |
| `crypto.py` | AES-256-GCM envelope, argon2 KDF, keyring, (73A) the prompt-free hook keyring read and the `mm diag` keyring probe, crypto-init bootstrap |
| `store/__init__.py` | Inert v3 package; no eager imports or lifecycle wiring |
| `store/envelope.py` | (71C) Verified fleet header/keycheck, explicit keys/context, envelope 0x03, bounded authenticated decoding and safe store errors |
| `store/format.py` | (71C) Commit/snapshot/presence schemas and frames, host fingerprint, whole-record digest and authenticated multipart assembly |
| `store/layout.py` | (71C) Canonical record keys (commit and snapshot names carry a per-publication family), parsed identity, pure grouping by `(device, kind, seq, family)` and inclusive foreign/fork classification; standard library only |
| `config.py` | `config.toml` load/validate/save, `DEFAULT_SOURCES`, exclude patterns, (64A) the five-state `usage_capture_readiness` verdict and its shared `usage_capture_remedy` text |
| `devices.py` | Device registry, short-id generation and lookup |
| `events.py` | mm-events log: git-root discovery, git/session walkers, budgets; (61A) shared day scan and publication projection; (64A) size- and origin-guarded `write_push_event` (`EventAppendSkipped`, `GIT_SNAPSHOT_ORIGIN_INIT`) |
| `token_usage.py` | Session-jsonl walker, token + skill caches, pricing, incremental resume |
| `host_usage.py` | Local Codex (live and archived rollouts), Grok (`updates.jsonl` terminal records, opt-in) and Muse (`session.jsonl` `model_completed` events, source-gated) usage readers, the Cursor reader (Conductor `runs.ndjson` and SQLite `index.db`, plus standalone completions), strict host-family classifier, and isolated host-token caches; also edits mm's single `stop` entry in the third-party `~/.cursor/hooks.json` (`configure_cursor_hook`) |
| `host_skill_discovery.py` | Read-only `grok inspect --json` probe for `mm diag` (`host_skill_discovery` sibling key). Not a `skill_link` registry. |
| `gitenv.py` | Repository-local Git environment scrub and stable message locale |
| `identity.py` | Author-email set behind a flock-guarded 7d-TTL cache |
| `merge.py` | Merge dispatch (`.jsonl`, `MEMORY.md`) + `lcs_merge` 3-way merge |
| `upgrade.py` | Release check, nudge, transition hook; (v1.3.0) self-update: install classification (`detect_install`), the two pipx subprocess seams, the one-attempt-per-release gate, `update_or_nudge`; `mm update` progress capture: the private PTY for the in-place upgrade (`_PipxOutputStream`), the best-effort `_ProgressRelay`, the foreground hangup guard and `_final_frames` |
| `updateprogress.py` | Read-only installer-output parser for `mm update` progress: measured download/Git/package counters and fixed phase labels; no timing estimates |
| `pullhistory.py` | Forensic table-backed pull/push/self-upgrade history, legacy import, batching and mm log queries |
| `statedb.py` | Local state.db connections, forward migrations, translated errors, transactions and displaced expiry; standard library + fsutil leaf |
| `seen_sources.py` | First-seen source tracking for the enable/disable prompts |
| `synclog.py` | Per-project `.mind-meld-log.md` writer |
| `sidecar.py` | Manifest sidecar read/write |
| `attemptlog.py` | (65A) Local attended-capture outcome holder, atomic private record, closed-vocabulary validation, and write-free attempt projection/render states |
| `lockfile.py` | The mm lockfile |
| `lockedjson.py` | Single-file flock read/modify/write primitive |
| `fsutil.py` | Atomic write, flock-append (61A opt-in strict outcome; (64A) whole-batch size ceiling, torn-row separator, regular-files-only), the rotatable spool pair `append_rotatable_jsonl` / `rotate_jsonl` (identity-checked append, rename-aside rotation), `fsync_dir`, the shared pytest account-state write guard |
| `errors.py` | Exception hierarchy |
| **`consoles.py`** | **(16A)** The two shared Rich `Console` singletons |
| **`conflictmtime.py`** | **(16A)** mtime primitives shared by the apply path and the resolver |
| **`skill_link.py`** | **(16A)** retro-fleet skill installer, the mm-owned `SKILL.md` store at `~/.local/share/mind-meld/agent-skills/` (24A), its 24h drift gate and markers, the `mm status` / `mm diag` link diagnosis, the `AGENT_ROWS` registry — add a new agent HERE — (25C) `consented_agent_keys` / `AgentRow.consent_source` / the installer `declined` status, and (28A) the deletion guard — `_marker_exists`, the `removed-by-user` status, and the rule that an ABSENT link is intent, not damage |
| **`events_tail.py`** | **(16A)** The push/init mm-events tail, its walk budgets, (19A) the host-usage capture, (61A) shared host-only capture/warm helper, (31A) reader-scoped failure isolation, (30A) git-only recapture, and (57A) the later-reader grace floor with full-reader warm/retry |
| **`resolveflow.py`** | **(16A)** Conflict discovery, promotion, the interactive `mm resolve` walk |
| **`retention.py`** | **(16A)** The `mm gc` reapers + crashed-push tmp sweep |
| `safety.py` | Peer-controlled string sanitization |
| `conflictdiff.py` | Pure leaf renderers for the conflict prompts |
| `storage/local.py` | Local backend |
| `storage/keys.py` | Validated storage-key construction (1.x namespace; v3 keys use `store/layout.py`) |
| `skills/retro_fleet/aggregator.py` | Fleet-retro aggregator and its `SKILL.md`; documented invocations use `mm retro-fleet` (`cli.py:retro_fleet_cmd`), and SKILL.md must never fall back to `python -m`. Store, installer and packaging rules: `docs/invariants/events-retro.md` |

**Import direction (Track 16A, load-bearing).** `cli` imports the six modules
above; none of them imports `cli`, at module scope *or* function scope. The
leaves (`consoles`, `conflictmtime`, `safety`, `conflictdiff`, `fsutil`,
`host_skill_discovery`, `gitenv`, `pullplan`, `attemptlog`, `updateprogress`, `statedb`) import nothing from the CLI layer at all. Enforced by
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
| `tests/sim/` replication simulator, any change under `src/mind_meld/store/`, or a store adapter | [Replication fixtures](../../tests/sim/README.md) and `docs/invariants/store.md`; run `./bin/check tests/sim` |
| `store/__init__.py` / `store/envelope.py:FleetKeys` / `RecordContext` / `create_fleet` / `unlock_fleet` / `seal_part` / `open_part` / `StoreError` | `docs/invariants/store.md` |
| `store/format.py:Entry` / `Record` / `IncompleteParts` / `host_fingerprint` / `encode_commit` / `decode_commit` / `encode_snapshot` / `decode_snapshot` / `encode_presence` / `decode_presence` / `seal_record` / `open_record` | `docs/invariants/store.md` |
| `store/layout.py:RecordName` / `KeyClassification` / `record_key` / `parse_record_key` / `classify_keys` / `group_record_names` | `docs/invariants/store.md` |
| `crypto.py:derive_key` | `docs/invariants/store.md` and `docs/invariants/init-devices.md`; legacy defaults/session/cache remain unchanged |
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
| `cli.py:autopull` / `autopush` / `_auto_command_setup` / (tail self-update via `upgrade.update_or_nudge`, unattended) | `docs/invariants/auto-upgrade.md` |
| `config.py:load_config` / `_validate` / `_apply_defaults` / `cli.py:autopush` / `autopull` / `_auto_command_setup` / (`ConfigError` normalization, `no-sources` breadcrumb) | `docs/invariants/sync.md` (Visible failures in automatic commands) |
| `lockedjson.py:locked_json_rmw` / `locked_json_snapshot` | `docs/invariants/sync.md` (Shared JSON locking) |
| `lockedjson.py:locked_json_durable_rmw` | `docs/invariants/sync.md` (Atomic write publication failures) |
| `storage/keys.py:manifest_key` / `blob_key` / `data_prefix` / `device_key` / `parse_blob_key` / `_validate_component` / `_validate_hex_sha` | `docs/invariants/sync.md` (Validated storage keys) |
| `manifest.py:walk_generic_source` / `walk_grok_source` / `walk_muse_source` / `MUSE_SYNCED_SUBDIRS` / `walk_claude_source` / `build_manifest_v2` / (`nested_roots` out-parameter) / `load_manifest` / `_validate_rel_path` / `collect_tombstones` / `generate_tombstones` / `marker_skip_globs` / `nested_repo_root` / `nested_repo_skip_prefixes` / `nested_repo_roots_for_paths` / `_directory_is_git_repo` / `_collect_regular_files_scandir` / `cli.py:_prove_omitted_paths_absent` / `_warn_push_growth` / `_freeze_nested_checkout_entries` / `_drop_unfrozen_checkout_files` / `_incoming_nested_roots` / `_collect_diag_state` / (diag `sync_scope`) | `docs/invariants/sync.md` |
| `config.py` exclude_patterns / disabled_sources / `seen_sources.py` consumer paths | `docs/invariants/sync.md` |
| `config.py:_GENERATED_HOST_SKILL_GLOBS` / the `DEFAULT_SOURCES` `exclude_patterns` lists (adding or removing a glob) | `docs/invariants/sync.md` (generated-files section) |
| `pullhistory.py` (forensic log) / `statedb.py` / `cli.py:log_cmd` | `docs/invariants/sync.md` and `docs/invariants/auto-upgrade.md` |
| `cli.py:_apply_write` / `_apply_merge` / `_apply_conflict` / `_apply_incoming_file` (mtime restore + future-clamp) | `docs/invariants/sync.md` |
| `conflictmtime.py:_restore_mtime_best_effort` / `_MTIME_RESTORE_MAX_SKEW_SECONDS` (future-clamp) | `docs/invariants/sync.md` |
| `cli.py:_apply_conflict` / `_apply_incoming_file` / `_prompt_conflict_choice` / `_check_fleet_version_or_refuse` / `conflict_filename` / `_filter_excluded_paths` | `docs/invariants/conflicts.md` |
| `retention.py:_gc_old_conflict_files` / `_is_live_conflict` | `docs/invariants/conflicts.md` |
| `resolveflow.py:_resolve_interactive_loop` / `_find_conflict_files` / `_migrate_pre_inversion_conflict` / `_ensure_inversion_marker` / `_synced_scan_dirs` / `_promote_target_path` / `_promote_conflict_file` / `_promote_target_will_sync` | `docs/invariants/conflicts.md` |
| `conflictmtime.py:_bump_canonical_mtime_post_resolve` / `_stat_mtime_btime` (both prompt sites share these) | `docs/invariants/conflicts.md` |
| `cli.py:_record_inline_bump` / `_invalidate_inline_bump` / `_drain_inline_bumps` / `_CANONICAL_WRITE_OUTCOMES` / `pending_inline_bumps` plumbing through `_pull_core` / `_pull_one_source` / `_download_and_apply` (outcome-gated invalidation) | `docs/invariants/conflicts.md` |
| `conflictdiff.py` (incl. `format_ts` / `format_age_delta` / `newer_side` / `render_time_line` / `render_verdict` / `merge_has_line_structure`) / `merge.py:lcs_merge` / `merge_jsonl` / `_extract_ts` / `manifest.py:_canonical_for_conflict` / `parse_conflict_device_short` / `parse_conflict_created_at` / `is_v1_conflict_filename` / either prompt site's `merge_available` computation | `docs/invariants/conflicts.md` |
| `cli.py:_register_and_save` / `_ensure_device_registered` / `init` / `_init_storage_guard` / `_do_gc` / (its peer-controlled blob-key prints) | `docs/invariants/init-devices.md` and `docs/invariants/sync.md` (Own-device blob collection) |
| `devices.py` / `storage/local.py:put_exclusive` / `find_conflict_copies` | `docs/invariants/init-devices.md` |
| `safety.py` or any new print site interpolating peer-controlled strings | `docs/invariants/init-devices.md` |
| `crypto.py:store_passphrase_in_keyring` / `get_passphrase` / `_non_interactive_keyring_read` / `_read_secret_service` / `_secret_service_state` / `errors.py:KeyringUnavailableError` / keyring path | `docs/invariants/init-devices.md` |
| `fsutil.py:refuse_real_state_write_under_pytest` / `PytestRealStateWriteError` / `atomic_write_bytes` / `flock_append_jsonl` / `append_rotatable_jsonl` / `rotate_jsonl` / `lockedjson.py:locked_json_rmw` / `locked_json_durable_rmw` (test write protection) | [Test write guard](README.md#test-write-guard); keyring protection: `docs/invariants/init-devices.md` |
| `cli.py:_log_unexpected` / `_write_migration_breadcrumb` / `_apply_incoming_file` / `init` | [Test write guard](README.md#test-write-guard); `test_log_unexpected_refuses_protected_root`, `test_migration_write_refuses_protected_root`, `test_migration_unlink_refuses_protected_root`, `test_pull_apply_refuses_protected_root`, `test_init_storage_refuses_protected_root` |
| `storage/local.py:LocalBackend.put` / `LocalBackend.put_exclusive` / `LocalBackend.delete` / `LocalBackend.delete_conflict_copies` | [Test write guard](README.md#test-write-guard); `test_local_put_refuses_protected_root`, `test_local_put_exclusive_refuses_protected_root`, `test_local_delete_refuses_protected_root`, `test_local_delete_conflict_copies_refuses_protected_root` |
| `seen_sources.py:read` / `acknowledge` / `write` | [Test write guard](README.md#test-write-guard); `test_seen_read_refuses_protected_root`, `test_seen_acknowledge_refuses_protected_root`, `test_seen_write_refuses_protected_root` |
| `upgrade.py:_pipx_install_lock` / `_write_update_log` / `_spawn_pipx` | [Test write guard](README.md#test-write-guard); `test_pipx_install_lock_refuses_protected_root`, `test_update_log_refuses_protected_root`, `test_spawn_pipx_refuses_protected_root` (pipx guard disabled, as `test_self_update` does) |
| `lockfile.py:acquire_lock` | [Test write guard](README.md#test-write-guard); `test_acquire_lock_refuses_protected_root` |
| `devices.py:_devices_write_lock` | [Test write guard](README.md#test-write-guard); `test_devices_write_lock_refuses_protected_root` |
| `config.py:save_config` / `_bootstrap_mm_events_path` | [Test write guard](README.md#test-write-guard); `test_save_config_refuses_protected_root`, `test_events_bootstrap_refuses_protected_root` |
| `sidecar.py:write` | [Test write guard](README.md#test-write-guard); `test_sidecar_write_refuses_protected_root` |
| `attemptlog.py:write` | [Test write guard](README.md#test-write-guard); `test_attemptlog_write_refuses_protected_root` |
| `resolveflow.py:_ensure_inversion_marker` | [Test write guard](README.md#test-write-guard); `test_inversion_marker_refuses_protected_root` |
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
| `host_usage.py:_read_cursor_sqlite` / `_query_cursor_sqlite` / `_cursor_sqlite_select` / `_lstat_sqlite_files` / `_sqlite_identity` / `_sqlite_text` / `_sqlite_json` / `_cursor_store_counts` / `_stage_cursor_directory` / `_sqlite_run` / `_sqlite_model` / `_sqlite_day` / `_cursor_entry` / `_cursor_placeholder` / `_cursor_run` / `_cursor_rank` / `_iter_cursor_directories` / `read_cursor_usage` / `cursor_sqlite_stores` / `unread_sqlite_stores` / `cursor_usage_diag` / `_cursor_read_set` / `_cursor_reason_detail` / `_carry_reason_detail` / `_ReadFailure` / `CURSOR_SQLITE_CENSUS_CONDUCTOR_VERSION` / `CURSOR_SQLITE_READER_CAUSES` / `CURSOR_SQLITE_STORE_CAUSES` / `cli.py:_cursor_store_detail_clause` / `_cursor_unsupported_clause` / `errors.py:CURSOR_SQLITE_URL` | `tests/fixtures/host_sessions/cursor/CONTRACT.md` and `docs/invariants/events-retro.md` (Conductor SQLite stores) |
| `cli.py:recapture` / `events_tail.py:_prepare_recapture` / `events.py:resolve_push_cursor` / `capture_advances_cursor` / `make_git_capture` | `docs/invariants/events-retro.md` (and `docs/invariants/sync.md` for recapture's history scope and hot-journal recovery) |
| `retention.py:EVENTS_RETENTION_DAYS` / `CONFLICT_AGE_DAYS` / `_gc_old_event_files` / `_gc_old_conflict_files` / `_is_live_conflict` / `_gc_token_cache` / `_sweep_local_tmp_files` / `_gc_orphan_retros_dir` | `docs/invariants/events-retro.md` |
| `gitenv.py:scrubbed_git_env` / `GIT_REPO_LOCAL_ENV_VARS` / `events.py:_walk_one_repo` / `_origin_remote_url` / `identity.py:_gather_global_email` / `_gather_per_repo_emails` / `read_cached_identities` / `_normalize_cache` | `docs/invariants/events-retro.md` |
| `events.py` / `identity.py` / `token_usage.py` | `docs/invariants/events-retro.md` |
| `host_usage.py` (incl. `read_codex_usage` / `CODEX_ARCHIVED_SESSIONS_PATH` / `_archive_root_exists` / `_iter_archived_rollouts` / `read_grok_usage` / `grok_completed_once` / `grok_usage_diag` / `_count_two_level_ledgers` / `read_muse_usage` / `muse_usage_diag` / `_scan_muse_root` / `_freeze_muse_day` / `_validated_muse_frozen` / `_iter_muse_day_dirs` / `_muse_day_logs` / `_muse_now` / `_muse_turn_from_record` / `_validate_muse_counters` / `MUSE_USAGE_CENSUS_HOST_VERSION` / `warm_host_cache_inline` / `_scan_codex_root` / `_scan_grok_root` / `_read_rollout` / `_carries_usage` / `_no_ledger_entry`) | `docs/invariants/events-retro.md`; Muse record shapes: `tests/fixtures/host_sessions/muse/CONTRACT.md` |
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
| `lockfile.py:acquire_lock` / `release_lock` | `docs/invariants/auto-upgrade.md` (mm lockfile) |
| `upgrade.py` / `cli.py` upgrade hook seams / `pullhistory.py:append_self_upgrade` | `docs/invariants/auto-upgrade.md` |
| `cli.py:update` / `_update_refusal` / `_run_update_with_progress` / `upgrade.py:update_or_nudge` / `detect_install` / `update_argv` / `run_update` / `_claim_install_attempt` / `_attempt_state` / `_run_pipx` / `_signal_pipx_group` / `_PipxOutputStream` / `_pipx_output_stream` / `_hangup_ignored` / `_ProgressRelay` / `_final_frames` / `_spawn_pipx` / `auto_install_enabled` / `updateprogress.py:PipxProgressParser` / `config.py:_apply_defaults` (the `[upgrade]` keys) | `docs/invariants/auto-upgrade.md` (self-update section) |
| Command/option names, positional arguments, exit codes, format constants, host families, token fields/order, device-registry fields, machine-readable output | `docs/invariants/auto-upgrade.md` “Compatibility (1.x)” |
| `pyproject.toml` version bump / tagging | `docs/invariants/auto-upgrade.md` |

If you're touching multiple areas (e.g., adding a new field to mm-push event that also flows through aggregator + adds a CLI flag), read every applicable invariant file. They're short; bulk-reading is cheap. The cost of skipping one and breaking a load-bearing invariant is much higher.

## Test write guard

`fsutil.py:refuse_real_state_write_under_pytest` checks the nonempty
`PYTEST_CURRENT_TEST` marker at call time, including in marker-inheriting child
processes. It is independent of conftest and its `_isolate_*` fixtures.
The single `_pytest_refused_roots` inventory protects:

- Four account trees: `.config/mind-meld`, `.local/share/mind-meld`,
  `Library/Mobile Documents/com~apple~CloudDocs/mind-meld`, `Sync/mind-meld`.
- Seven agent trees: `.claude`, `.codex`, `.grok`, `.gstack`, `.gstack-extend`,
  `.cursor`, `.local/share/muse`.
- `storage.path`, every explicit `sync.sources[i].path` (including disabled
  sources), and legacy `sync.claude_dir` from the **real account config**
  `<account-home>/.config/mind-meld/config.toml`.

All home-relative roots above use `pwd.getpwuid(os.getuid()).pw_dir`, never
`$HOME`, `Path.home()` or patched config constants. A test that patches pwd
retargets the guard. DEFAULT_SOURCES paths and AGENT_ROWS skills-root **values**
are pinned against this helper by `test_default_source_and_agent_roots_pin`;
its `no_mm_events_isolation` and `owns_skill_paths` opt-outs preserve canonical
inputs, and a value that is not `~`-anchored fails the pin. Adding a default
destination requires updating the helper and its pin.

Configured roots use stdlib `tomllib`, imported lazily only after the active
marker branch. Missing/empty config adds no roots. A non-regular, unreadable,
over-cap (`_PYTEST_CONFIG_MAX_BYTES`, 1 MiB), unparseable or malformed-shape
config refuses every guarded write. `storage`/`sync` must be tables,
`sync.sources` a list of tables with string paths; duplicate keys are invalid
TOML. Missing/non-string/empty `storage.path` adds no storage root. Repairs
name the real config file and the offending path key, never credential values.
The session preflight runs the guard once before workers start, so any lookup
failure (config, configured root, unreachable protected root such as a symlink
loop) fails the run once by name; the writer guard itself remains conftest-free.

| Config spelling | Guard expansion / production relationship |
|---|---|
| Absolute | Keep lexical and resolved spellings for classification. |
| Bare `~`, `~/...` | Normalize like production (`~//x` is `~/x`), then expand against pwd home. Deliberately differs from production `expanduser`, which honors a patched HOME. |
| `~user/...` | `pwd.getpwnam`; lookup failure refuses with a chained cause. |
| Still relative | Resolve against `Path.cwd()`, matching production. |
| Symlinked | Classify lexical location, resolved target and ancestor identity. Production mm-internal source paths retain their absolute, unresolved spelling; the guard additionally classifies the target. |
| Disabled explicit source / legacy claude_dir | Still protected real user data. |

A configured root equal to `/`, the pwd home, or an ancestor of it refuses as
a **configured-root lookup failure**, naming the root and the edit required.
Raw strings cache by `(config path, st_mtime_ns, st_size, pwd home)` and
re-expand per call. Resolved roots/anchors also key on cwd and live ancestor
identities, invalidating stable link retargeting; a root whose first missing
component is a dangling symlink, or whose missing suffix contains `..`, is never
cached, because links it resolves through can change without touching the anchor. Same-size rewrites with a
changed mtime and chdir are tested. Config schema changes must update this
extractor and the scoped production-parity test (absolute, `~user`, and
HOME-equals-pwd-home examples).

Roots and descendants use lexical containment, resolved containment and
read-only existing-ancestor device/inode identity checks anchored on both the
root and its resolved target. The destination entry is also classified inside
its resolved parent without following its final component: helpers create
temps, rename and lock there. Each prefix before `..` is classified because
recursive mkdir can create it before normalization. Stable symlinks into and
out of roots are covered. Existing distinct case variants on a sensitive
filesystem are allowed; missing indistinguishable variants refuse
conservatively. Ordinary nonexistent suffixes, including beneath a regular
file, retain native writer errors. Other lookup failures refuse with chained
causes. No case probes or home-tree scans run. Outside pytest the guard returns
before any account, path or config lookup, or tomllib import.

**C1 closed for the frozen inventory below under an active marker.** Each
writer calls the raising fsutil guard on its destination **before its first
mutation, mkdir included**, without rerouting its write protocol. This
completes retained 71B acceptance only for these named writers under an active
marker; it establishes neither suite-wide safety nor simulator safety.

| Writer | Guard destination and placement | No-op / read edge |
|---|---|---|
| `cli.py:_log_unexpected` | log path before SIDECAR_DIR mkdir and `a+b` open | BaseException escapes its Exception handler; original error stays diagnosable |
| `cli.py:_write_migration_breadcrumb` | breadcrumb path at entry, before unlink or mkdir | even missing-file deletion refuses; docstring has guard carve-out |
| `cli.py:_apply_incoming_file` | local_path once before ancestor mkdir loop | leaf symlink skip remains read-only |
| `cli.py:init` | full storage path before mkdir | CliRunner flow tested |
| `storage/local.py:LocalBackend.put` / `put_exclusive` | key path before mkdir/temp/link | exclusive EEXIST path also refuses |
| `storage/local.py:LocalBackend.delete` / `delete_conflict_copies` | key path at entry | missing key and zero copies still refuse |
| `seen_sources.py:read` / `acknowledge` / `write` | SEEN file before recovery/write mkdir | successful O_RDONLY read never guards |
| `upgrade.py:_pipx_install_lock` / `_write_update_log` / `_spawn_pipx` | lock/log path before mkdir/open | `_install_is_running` read-only; the pipx guard usually refuses spawn first, but `test_self_update` disables it, so the log-path guard has its own refusal test |
| `lockfile.py:acquire_lock` / `devices.py:_devices_write_lock` | lock path before mkdir/open | release preserves lock inode |
| `config.py:save_config` | config path before parent mkdir | — |
| `config.py:_bootstrap_mm_events_path` | p before creation loop and before mode-tightening branch | existing 0700 root and inspection never guard |
| `sidecar.py:write` / `attemptlog.py:write` / `resolveflow.py:_ensure_inversion_marker` | written path before SIDECAR_DIR mkdir | existing inversion marker is a pure read |

Already guarded: `atomic_write_bytes`, nonempty `flock_append_jsonl`,
`append_rotatable_jsonl`, both `rotate_jsonl` paths,
`statedb.connect(read_only=False)`, `locked_json_rmw` and
`locked_json_durable_rmw` (data **and** sibling lock). Empty appends remain
no-ops; snapshots and `fsync_dir` are read-only. Allowed-path lock, permission,
publication, retry and durability contracts remain unchanged.
`test_<writer>_refuses_protected_root` and `test_<writer>_tmp_control` give
each writer its own discoverable refusal/control pair; the
static `tests/test_write_guard_inventory.py` census detects unreviewed raw
sites and checks same-function guard ordering, including seeded violations.
Its primitives cover open/mkdir/makedirs, writes, unlink/rmdir/removedirs,
link/symlink/rename/renames/replace, chmod/chown/utime/truncate, temp-file and
temp-dir creation, `shutil` copy/move/rmtree/archive helpers and
`sqlite3.connect`; fd-only operations ride on their tracked opens. It recognizes stdlib import aliases and
direct method aliases, and rejects writable os flags in reviewed read-only opens. It supplements behavioral
tests; it is not control-flow or sandbox proof.

Copy this pattern for a new writer. Guard the written file (logs/config/
sidecars), lock file, backend key path, apply local_path, or init storage path;
bootstrap guards the root p only on branches that create or tighten it:

```python
from mind_meld import fsutil


def write_state(path, data):
    fsutil.refuse_real_state_write_under_pytest(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_bytes(path, data)
```

Refusal-test skeleton (prepare synthetic sentinels before denying mutations;
retarget destination constants **after** autouse patches):

```python
def test_writer_refuses_real_state(
    synthetic_account_home, monkeypatch, forbid_fs_mutations
):
    root = synthetic_account_home / ".config/mind-meld"
    destination = root / "state.json"
    # Patch this writer's destination here, then assert it classifies under root.
    with pytest.raises(fsutil.PytestRealStateWriteError):
        fsutil.refuse_real_state_write_under_pytest(destination)
    calls = forbid_fs_mutations()
    with pytest.raises(fsutil.PytestRealStateWriteError):
        write_state(destination, b"{}")
    assert calls == [], f"denied mutation attempts: {calls!r}"
```

Ordinary isolated tests stay simple:

```python
from mind_meld import fsutil


def test_state_write(tmp_path):
    fsutil.atomic_write_bytes(tmp_path / "state.json", b"{}")
```

`PytestRealStateWriteError` derives from `BaseException` so forensic
`except Exception` handlers cannot hide it. Diagnostics quote paths and keys,
name the test, matched root/source, real config and pwd home (`$HOME is ignored`),
and distinguish protected hits from lookup failures. Example refusal (wrapped
for readability):

```text
Test write guard refused '/account/.claude/state'; PYTEST_CURRENT_TEST='test_writer (call)':
protected account-state path under '/account/.claude'; matched root='/account/.claude',
source key='.claude', real config='/account/.config/mind-meld/config.toml',
pwd account home='/account'; $HOME is ignored.
Isolate write destinations under tmp_path and keep the isolation fixtures enabled.
No write occurred. Fix the destination/config and rerun; nothing to clean up.
See docs/invariants/README.md#test-write-guard
```

Five-idiom decision rule: use the **raising fsutil guard at write sites**;
`skill_link` keeps its **skip-loudly boolean guard on the push path only**;
keyring returns **False**; upgrade's pipx guard raises **OSError**;
`host_usage` keeps its destination comparators (**PermissionError** for Cursor
hook edits, **RuntimeError** for real Conductor SQLite opens). These contracts
are not interchangeable. The skill_link direct symlink/unlink gap remains;
fsutil does not wrap those raw operations. Host-usage comparators keep their
existing narrow hook-edit/store-open scope, not an eviction guarantee.

Surviving residuals: missing/empty marker incl. collection-time and
env-scrubbed subprocesses; concurrent symlink swap; malicious tests;
worker-thread-local refusals; raw unlinks, renames and links outside the
inventory; future writers that skip the call, which the census flags for its
listed primitives. Module-level test code can run
before a marker exists. A same-size, same-mtime config rewrite can retain the
cached raw strings. A refusal in a worker thread need not fail its parent test.
Raw retention unlinks and empty-directory rmdir, crypto conflict-copy,
host_usage spool, and cli stale-copy/quarantine/migration-cleanup deletes stay
outside this frozen inventory (P3 TODO, revisit on an observed deletion or
near-miss). So do resolveflow's promotion links and migration/resolution
renames, which **overwrite** canonical files, and conflictmtime's mtime-only
`os.utime`; the P3 TODO's revisit covers them too. The os.devnull output open is a process sink, not account state.
New implementation-time finds belong here and in the reviewed census, not in
the writer scope. Track 75A and later writers must adopt the call convention;
82A deletes the temporary LocalBackend rows when that backend retires.
