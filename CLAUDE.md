# CLAUDE.md

## Project
Mind Meld (mm) — CLI tool for syncing ~/.claude session data and ~/.gstack context across Macs via iCloud Drive. Supports multiple configurable sync sources.

## Stack
Python 3.11+, typer, cryptography, argon2-cffi, keyring, rich.

## Key Principles
- No API server. CLI talks directly to iCloud Drive via the local filesystem.
- Single storage backend: local folder at `~/Library/Mobile Documents/com~apple~CloudDocs/mind-meld`, synced by iCloud.
- **End-to-end encrypted.** All data (sessions, manifests, artifacts) encrypted client-side with AES-256-GCM before touching storage. The storage layer never sees plaintext. This is a hard invariant — no code path may write unencrypted session data to storage.
- **Scoped sync.** Only syncs `memory/` and `todos/` within each project — not sessions, settings, or other git-tracked files.
- **Truth-based manifests.** Manifests are complete snapshots of local state. Deletions propagate automatically — no separate prune step.
- **Conflict resolution.** Detects and resolves iCloud and Dropbox-style conflict copies on manifest files. For source files with divergent local edits, INVERTED in v0.9.2: local stays at canonical, REMOTE bytes go to `<stem>.sync-conflict-<ts>-<device>.<ext>` (Syncthing convention's actual direction — visible sidecar holds the surprising bytes). Mtime-skip: if the local file is newer than remote, pull leaves it alone. Pre-v0.9.2 conflict files are migrated to a `v0-` prefix on first lock-protected discovery (mm pull / mm resolve only); resolve dispatches by filename prefix (`v0-` = pre-inversion semantics, no prefix = post-inversion).
- **Sync log.** After pull, writes `.mind-meld-log.md` per project so Claude Code knows what changed from other machines.
- Manifest-based diffing: SHA-256 hash every file, only upload/download changes.
- Content-addressed storage: blobs stored by hash, not by path.
- Gzip compression before encryption. Versioned blob format (v0x01).

## Source Layout
src/mind_meld/{cli,manifest,crypto,errors,devices,config,lockfile,synclog,merge,sidecar,pullhistory,upgrade,seen_sources,events,safety,conflictdiff,lockedjson,token_usage,identity}.py
src/mind_meld/storage/{local,keys}.py
src/mind_meld/skills/retro_fleet/{SKILL.md,aggregator.py,__init__.py}  (Group 8 v0.11.0 — Claude Code skill orchestrator + Python aggregator. Dir on disk is `retro_fleet` (Python identifier, importable as `mind_meld.skills.retro_fleet`); the symlink installer creates `~/.claude/skills/retro-fleet` (hyphen — Claude Code naming convention). SKILL.md invokes the aggregator via `mm retro-fleet <window>` (typer wrapper at `cli.py:retro_fleet_cmd`, v0.11.22) — NOT `python -m mind_meld.skills.retro_fleet.aggregator`, because pipx installs hide mind_meld from any interpreter outside the pipx venv and macOS systems often only have `python3` (not `python`) on PATH. Ships via `packages = ["src/mind_meld"]` — do NOT add hatchling `force-include` for this subtree, it would double-ship.)

`safety.py` (v0.11.1) — peer-controlled string sanitization (`safe_str`, `safe_text`, `strip_terminal_escapes`); cli.py re-exports for backwards compat. New tests should import from `mind_meld.safety` directly. See `docs/invariants/init-devices.md`.

`conflictdiff.py` (v0.11.1) — pure leaf primitives for the conflict prompts (`render_prompt`, `render_banner`, `count_divergent_lines`); site-level dispatch stays at each call site. See `docs/invariants/conflicts.md`.

`lockedjson.py` (v0.11.14) — extracted single-file flock R/M/W primitive shared by `upgrade.py`, `token_usage.py`, and `identity.py` (v0.11.17). Three contention modes: `block` / `raise` / `warn`. Do NOT route new flock-guarded JSON caches through ad-hoc fcntl calls; extend `lockedjson` if the contract needs to grow. `devices-write.lock` stays ad-hoc — its multi-file lock-on-sibling shape doesn't fit the single-file R/M/W contract.

`token_usage.py` (v0.11.14) — walks Claude Code session jsonls (parent + subagents) and aggregates per-jsonl `message.usage` totals into a flock-guarded cache at `~/.config/mind-meld/session-tokens.json`. Subagent jsonls contribute to the parent project's token totals but do NOT bump `sessions` / `total_kb` / `last_session_at`. The aggregator slices `tokens_by_day` to the retro window. GC hook in `mm gc` reaps cache entries whose underlying jsonl is gone OR whose most recent `by_day` key is more than 90 days old. Mixed-fleet mode: aggregator's field-presence sniff flags pre-v0.11.14 peers OR cold-cache devices into `pre_token_peers`. Public helpers (Track 10A v0.11.24): `TOKEN_FIELDS` constant + `zero_day_bucket` / `zero_model_bucket` factories + `merge_usage_bucket` / `merge_by_model` helpers consolidate the four bucket-merge sites (token_usage, events, aggregator); `lock_and_get_files` context manager owns cache-shape invariants for the cli's events tail/backfill. Adding a 5th token field is a one-line change to `TOKEN_FIELDS`. See `docs/invariants/events-retro.md`.

`identity.py` (v0.11.17) — owns the running machine's locally-known author-email set behind a flock-protected 7d-TTL cache. Push tail and retro render share state via this cache. `aggregator.gather_author_emails()` is now a thin shim. See `docs/invariants/events-retro.md`.

Storage keys are constructed via helpers in `storage/keys.py` (`manifest_key`, `blob_key`, `device_key`, `parse_blob_key`) which validate components at construction time — a corrupt or malicious peer manifest cannot smuggle a `sha256: "../../../etc/passwd"` through `backend.get`. Do NOT build storage keys with raw f-strings at new call sites.

Version source of truth: `pyproject.toml` (read by `__init__.py` via `importlib.metadata.version("mind-meld")`, fallback `"0.0.0+dev"` for uninstalled source-tree runs). No `VERSION` file.

## Testing
pytest. Use tmp_path for local backend. Run: `pytest tests/`

Lint/format enforced via ruff (pinned to `ruff==0.15.12` in `dev` deps). Run `ruff check .` and `ruff format --check .` locally before pushing — CI runs both as a separate `lint` job and will block merges on drift. Rule set: `E`/`F`/`W`/`I` (isort enforcement).

The PYTEST_CURRENT_TEST guard on `crypto.store_passphrase_in_keyring` (v0.11.11) is load-bearing — see `docs/invariants/init-devices.md` for the rationale.

## CI
GitHub Actions at `.github/workflows/ci.yml`. Single job on `macos-latest` + Python 3.13 (mind-meld is a macOS tool — multi-OS + multi-Python matrix is theater for this project). Runs ruff check + ruff format --check + pytest + wheel build + `mm --version` smoke. Asserts the real Keychain backend loads (guards against silent `fail.Keyring` fallback). pip cache keyed on `pyproject.toml`. No `paths:` filter — every PR runs CI (avoids the branch-protection pending-forever footgun for path-skipped required checks).

`.github/workflows/release.yml` (v0.11.24+) — auto-tag + auto-create-Release + auto-append-PROGRESS row on push to main, gated on changes to `pyproject.toml` or `CHANGELOG.md`. Each step independently idempotent (re-run-safe after partial failure). Eliminates the three drift modes that produced the v0.11.0..v0.11.22.1 mess: orphan tags (PR bumps pyproject without cutting a tag), missing GitHub Releases (24-row backfill needed periodically), and PROGRESS.md staleness (table fell 19 rows behind). Does NOT solve parallel-workspace version collisions (two open PRs both claiming the same version slot) — that's a separate `pull_request`-triggered check, deferred. Tag detection branches on `git rev-parse "$tag"` and release detection on `gh release view "$tag"` so re-runs after a partial failure resume cleanly. PROGRESS auto-append uses CHANGELOG body's lead paragraph (text from `## [version]` to first `### Section` or next `## [`), pipes escaped, single line. Loop-safe: workflow's own PROGRESS commit only touches `docs/PROGRESS.md`, which is NOT in the paths filter, plus `[skip ci]` in the commit message belt-and-suspenders. Bot identity is `github-actions[bot]`.

## Commands
mm --version | init | push | pull | status | devices | diff | gc | sources | conflicts | resolve | log | migrate-config | autopull | autopush | enable-source | disable-source | reconfigure-sources | refresh-identity | install-skills | retro-fleet

Pull flag: `--conflict-mode {prompt|keep-both|fail}` (default `keep-both`). `prompt` asks per-file; `fail` preflights via `_predict_pull_outcome` and exits 2 (no writes) if any file would conflict — for CI. Replaces the old `--no-prompt` / `--resolve-interactive` pair (v0.6.2 BREAKING).
GC flags: `--conflicts` (also reap `.sync-conflict-*` files older than 30 days).
Log flags: `--source NAME`, `--since DATE`, `--action {written|merged|skipped|conflicted|excluded|uploaded|failed}`, `--verb {pull|push}`, `--limit N`, `--format {jsonl|table}`.
Migrate-config flags: `--yes`, `--dry-run`. Idempotent: appends missing recommended `exclude_patterns` to existing `[[sync.sources]]` entries; preserves user-customized globs.

## Invariant pointer table

Load-bearing invariants live in `docs/invariants/<topic>.md`. Read the relevant file BEFORE editing the listed code. The tables below are file-path-keyed routing rules — match the file or function you're about to touch and read the named invariant doc(s) first.

| If you're editing… | READ FIRST |
|---|---|
| `cli.py:_pull_core` / `_push_core` / `_fetch_remote_manifest` / `_recover_prior_manifest` / `_filter_excluded_paths` / `_filter_disabled_sources` / `_drop_case_collisions_from_manifests` | `docs/invariants/sync.md` |
| `cli.py:_download_and_apply` rel_path / base_path concatenation site | `docs/invariants/sync.md` |
| `manifest.py:walk_generic_source` / `load_manifest` / `_validate_rel_path` / `collect_tombstones` / `generate_tombstones` | `docs/invariants/sync.md` |
| `config.py` exclude_patterns / disabled_sources / `seen_sources.py` consumer paths | `docs/invariants/sync.md` |
| `pullhistory.py` (forensic log) | `docs/invariants/sync.md` |
| `cli.py:_apply_conflict` / `_apply_incoming_file` / `_resolve_interactive_loop` / `_prompt_conflict_choice` / `_check_fleet_version_or_refuse` / `_find_conflict_files` | `docs/invariants/conflicts.md` |
| `conflictdiff.py` / `merge.py:lcs_merge` / `manifest.py:parse_conflict_device_short` | `docs/invariants/conflicts.md` |
| `cli.py:_register_and_save` / `_ensure_device_registered` / `init_cmd` / `_init_storage_guard` | `docs/invariants/init-devices.md` |
| `devices.py` / `storage/local.py:put_exclusive` | `docs/invariants/init-devices.md` |
| `safety.py` or any new print site interpolating peer-controlled strings | `docs/invariants/init-devices.md` |
| `crypto.py:store_passphrase_in_keyring` / keyring path | `docs/invariants/init-devices.md` |
| `cli.py:_run_events_tail` / `_run_events_backfill` | `docs/invariants/events-retro.md` |
| `cli.py:_ensure_retro_skill_link` / `_skill_link_check_due` / `install_skills_cmd` / `retro_fleet_cmd` | `docs/invariants/events-retro.md` |
| `cli.py:refresh_identity_cmd` / `_devices_json_cmd` / `EVENTS_RETENTION_DAYS` / `_gc_old_event_files` | `docs/invariants/events-retro.md` |
| `events.py` / `identity.py` / `token_usage.py` | `docs/invariants/events-retro.md` |
| `skills/retro_fleet/aggregator.py` | `docs/invariants/events-retro.md` |
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
- See README.md "Claude Code Integration" section for CLAUDE.md snippet

## Spec
See SPEC.md for full architecture and data model.
See docs/designs/mind-meld-v1.md for design decisions from spec review.
See docs/designs/sync-gstack-context.md for multi-source sync design (gstack support).
See docs/invariants/ for per-topic load-bearing invariants (sync, conflicts, init-devices, events-retro, auto-upgrade).
