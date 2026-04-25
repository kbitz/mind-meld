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
src/mind_meld/{cli,manifest,crypto,errors,devices,config,lockfile,synclog,merge,sidecar,pullhistory,upgrade}.py
src/mind_meld/storage/{local,keys}.py

Storage keys are constructed via helpers in `storage/keys.py`
(`manifest_key`, `blob_key`, `device_key`, `parse_blob_key`) which validate
components at construction time — a corrupt or malicious peer manifest cannot
smuggle a `sha256: "../../../etc/passwd"` through `backend.get`. Do NOT build
storage keys with raw f-strings at new call sites.

Version source of truth: `pyproject.toml` (read by `__init__.py` via `importlib.metadata.version("mind-meld")`, fallback `"0.0.0+dev"` for uninstalled source-tree runs). No `VERSION` file.

## Testing
pytest. Use tmp_path for local backend. Run: `pytest tests/`

Lint/format enforced via ruff (pinned to `ruff==0.15.12` in `dev` deps). Run `ruff check .` and `ruff format --check .` locally before pushing — CI runs both as a separate `lint` job and will block merges on drift. Rule set: `E`/`F`/`W`/`I` (isort enforcement locks in Group 3's hoisted imports).

## CI
GitHub Actions at `.github/workflows/ci.yml`. Single job on `macos-latest` + Python 3.13 (mind-meld is a macOS tool — multi-OS + multi-Python matrix is theater for this project). Runs ruff check + ruff format --check + pytest + wheel build + `mm --version` smoke. Asserts the real Keychain backend loads (guards against silent `fail.Keyring` fallback). pip cache keyed on `pyproject.toml`. No `paths:` filter — every PR runs CI (avoids the branch-protection pending-forever footgun for path-skipped required checks).

## Commands
mm --version | init | push | pull | status | devices | diff | gc | sources | conflicts | resolve | log | migrate-config | autopull | autopush

Pull flag: `--conflict-mode {prompt|keep-both|fail}` (default `keep-both`). `prompt` asks per-file; `fail` preflights via `_predict_pull_outcome` and exits 2 (no writes) if any file would conflict — for CI. Replaces the old `--no-prompt` / `--resolve-interactive` pair (v0.6.2 BREAKING).
GC flags: `--conflicts` (also reap `.sync-conflict-*` files older than 30 days).
Log flags: `--source NAME`, `--since DATE`, `--action {written|merged|skipped|conflicted|excluded|uploaded|failed}`, `--verb {pull|push}`, `--limit N`, `--format {jsonl|table}`.
Migrate-config flags: `--yes`, `--dry-run`. Idempotent: appends missing recommended `exclude_patterns` to existing `[[sync.sources]]` entries; preserves user-customized globs.

## exclude_patterns + consumer-boundary filter (load-bearing, v0.9.1, v0.9.3)
Per-source `exclude_patterns: list[str]` of fnmatch globs is matched against the relative path. Default `gstack` source ships with `["config.yaml", "projects/*/repo-mode.json", "projects/*/land-deploy-confirmed"]` (per-machine artifacts that churn-conflict on every pull — `config.yaml` holds gstack's version-check tracking added in v0.9.3). The walker drops excluded paths from the local manifest at push time.

`_filter_excluded_paths(manifest, exclude_map)` applies at TWO consumer-boundary call sites — both AFTER `_fetch_remote_manifest` returns: (1) `_pull_core` filters peer manifests in `manifest_cache` BEFORE `collect_tombstones` and the per-source download loop; (2) `_push_core` filters the manifest returned by `_recover_prior_manifest` (covers ok / sidecar / peer-fallback uniformly) BEFORE `generate_tombstones`. The filter MUST NOT apply at `_fetch_remote_manifest` itself — `mm gc` reads raw manifests via that path to compute referenced blobs, and a filtered manifest there would mark live peer blobs as orphans (codex-2 #1, pinned by `test_mm_gc_does_not_orphan_excluded_path_blobs`).

**Tombstone-suppression invariant.** Adding a path to `exclude_patterns` must NOT generate a deletion tombstone on the next push (2026-04-24 first-pull regression). Removing a glob brings the path back as new. Sidecar recovery is filtered too so a corrupt-manifest recovery on a freshly-migrated config doesn't re-introduce pre-exclude paths via the sidecar (codex-2 #2). All four scenarios (two-device first-pull, tombstone-on-exclude, tombstone-on-unexclude, sidecar-bypass-guard) are pinned in `tests/test_integration.py::TestExcludePatterns5C`.

**Visible-failure contract for migration UX (v0.9.1).** Existing configs need to opt in by running `mm migrate-config`. autopull / autopush NEVER auto-mutate config — they record the missing-excludes signal to `~/.config/mind-meld/migration-state.json` and let `mm status` surface it. Interactive `mm pull` / `mm push` prompt-once. Silent config mutation in a hook would be exactly the class of "wedged sync I never noticed" failure the visible-failure contract exists to prevent. Add the new "config missing recommended excludes" warning to the existing curated stderr signal set (corrupt-manifest recovery, fsync failures, no-sources misconfig, etc.).

## Conflict-direction inversion + fleet-version refusal (load-bearing, v0.9.2 BREAKING)
`_apply_conflict` keeps LOCAL bytes at canonical; REMOTE bytes go to `.sync-conflict-*` sidecar. No rename + rollback dance — local is never overwritten in the conflict path. Pre-v0.9.2 produced the opposite mapping; pre-existing files migrate to a `v0-` prefix on first lock-protected discovery in `mm pull` / `mm resolve` (NEVER from `mm conflicts` — lockless, would race autopull, codex-2 #5).

`_resolve_interactive_loop` is dual-mode dispatched BY FILENAME PREFIX (not timestamp — sound, since post-v0.9.2 code never produces a `v0-` file directly). `v0-` files: `(l)ocal` renames sidecar over canonical, `(r)emote` unlinks sidecar. No-prefix files: `(l)ocal` unlinks sidecar, `(r)emote` renames sidecar over canonical. Diff fromfile/tofile labels flip per row to match.

`_check_fleet_version_or_refuse(backend, my_device_id)` runs at the top of `_pull_core` BEFORE any I/O. Per-peer classification via `packaging.version.Version` against `INVERSION_MIN_VERSION = "0.9.2"`: safe (>= 0.9.2 → ALLOW), inactive (last_seen missing → ALLOW), pre-v0.9.2 (last_seen present, version missing or < threshold → REFUSE), dropped (corrupt device.json → REFUSE by storage key). Refusal message names every offending peer; recovery is `pip install --upgrade mind-meld` + `mm push` on each peer. Implementation uses `list_devices_with_drops` (silent variant) so the `_select_devices`-side `_list_devices_warn` only logs once if the fleet check passes.

`update_last_seen` writes `last_seen_version: __version__` alongside `last_seen` on every push. Forward-compatible (older mm tolerates unknown keys). `mm devices` table surfaces it as a column.

## Init order + push-time self-heal (load-bearing, v0.9.4)
`_register_and_save` (renamed from `_save_and_register`) writes the remote first, the local pointer last: `register_device(backend, ...)` → `save_config(...)` → keyring store. Canonical filesystem/DB transaction discipline — a SIGKILL/OOM/power-loss in the window between the two writes leaves an inert orphan storage entry (recoverable on retry init via `_init_storage_guard`'s orphan-case prompt), never the inverse half-state where local config claims a `device_id` storage doesn't contain. Pre-v0.9.4 produced the inverse mapping. Do NOT reorder these calls.

If `save_config` raises (disk full, permissions), a best-effort `backend.delete(device_key(device_id))` cleanup runs before the original exception propagates — keeps orphans from accumulating in storage when normal save failures hit. Cleanup-failure surfaces a `mm: warning:` stderr breadcrumb but does NOT mask the original save error (visible-failure contract). The `device_key(device_id)` storage key is precomputed BEFORE `register_device` so the cleanup-warning f-string can't itself raise from `device_key`'s validation and mask the real cause (codex adversarial 2026-04-25).

`_ensure_device_registered(backend, device_id, device_name, *, dry_run)` runs at the top of `_push_core` BEFORE any push work. If `devices/<my_id>.json` is absent, it recreates it via `register_device`. Two scenarios converge: future v0.9.4+ SIGKILL crash mid-init (cosmetic) AND retroactive fix for pre-v0.9.4 victims of the v0.8.15..v0.9.3 inverted half-state — those users had been pushing manifests under an ID no peer recognized, silently. First push after upgrading to v0.9.4 self-heals. Gated on `not dry_run` (codex review: `mm push --dry-run` must not mutate storage). Register failures emit a `mm: warning:` stderr breadcrumb before re-raising — load-bearing for autopush, whose generic `except Exception` would otherwise swallow the failure and silently no-op every push.

## `_find_conflict_files` tuple-key dedup (load-bearing, v0.9.4)
The function runs two scan strategies that overlap when an `include_files` entry sits inside an `include_dirs` directory: (1) `include_dirs` rglob and (2) depth-0 sibling-glob for `include_files`. Without dedup, a conflict file at e.g. `projects/notes.sync-conflict-...md` is visited twice when a user customizes config with `include_files: ["projects/notes.md"]` AND `include_dirs: ["projects"]` (nested) — duplicate rows in `mm conflicts`, inflated counts in `mm gc --conflicts`, `mm resolve` silent no-op on the second visit. Dedup uses `seen: set[tuple[str, Path]]`. The tuple key (NOT bare `Path`) preserves source attribution when two configured sources legitimately reference overlapping subtrees — bare-Path dedup would incorrectly collapse a conflict file shared between sources. Default config doesn't trigger this (all `include_files` are bare top-level dotfiles), but the dedup is footgun-removal for anyone customizing. Pinned in `tests/test_conflict_copy.py::TestFindConflictFilesNestedDedup`.

## Pull/push history log (v0.9.1)
`pullhistory.append(verb, device, source, rel_path, action, ...)` writes one JSONL line to `~/.config/mind-meld/pull-history.jsonl` (mode 0600, fcntl.flock-guarded, 1MB cap with line-boundary rotation to `.1`). Wired into `_pull_core` (per-outcome from `_pull_one_source` + `excluded` from the consumer-boundary filter) and `_upload_changed_blobs` (`uploaded`). Failures are swallowed — history is forensic-only, never block sync. `mm log` queries with `--source / --since / --action / --verb / --limit / --format` filters. Reader tolerates a torn first line in `.1` (crash-mid-rotate fingerprint).

## Corrupt-manifest recovery (load-bearing)
`_fetch_remote_manifest` returns a tri-state `ManifestFetch(status: "ok"|"missing"|"corrupt", manifest)`. On `corrupt`, `push` runs a recovery chain before writing a new manifest: (1) local sidecar at `~/.config/mind-meld/last-push.json` (preserves this device's fresh deletions), (2) peer-manifest tombstone aggregation (propagated deletions only), (3) refuse with actionable error. Never treat corrupt as empty — that silently un-deletes files fleet-wide. `mm gc` refuses when any peer manifest is corrupt (referenced blobs may still be live). See SPEC.md "Manifest corruption recovery" and "Merge invariants" for the full invariant.

## Manifest read-path invariant (load-bearing)
Every manifest loaded from bytes/disk MUST go through `manifest.load_manifest(bytes) -> dict`, which composes `deserialize_manifest + normalize_manifest` plus full inner-shape validation. The function guarantees the returned dict has dict-typed `sources` and `tombstones`, each source has a dict `files`, and each tombstone value is a dict. Malformed manifests raise `ManifestError` at the load boundary instead of crashing downstream consumers (`_merge_manifests`, `collect_tombstones`, `generate_tombstones`, the diff loop) with `AttributeError`. `_fetch_remote_manifest` already catches `ManifestError` and falls through to the recovery chain, so a malformed peer manifest degrades to a clean "corrupt" status. Do NOT add a new manifest-load path that bypasses `load_manifest` (sidecar.read uses `deserialize_manifest + structural-check + normalize_manifest` deliberately, to preserve the anti-tampering guard on raw input).

## Auto Commands (for Claude Code integration)
- `mm autopull` — silent pull, one-line output, never prompts, graceful on errors
- `mm autopush` — silent push, one-line output, never prompts, graceful on errors
- Both exit silently if mm is not initialized (no config) or no changes exist
- `ConfigError` (bad `config.toml`) surfaces as a one-line stderr message — not a silent exit. This is the visible-failure contract: truly unexpected errors still degrade silently via the generic `except Exception` fallback, but malformed config is loud so users don't wedge their background sync without noticing. Relies on `load_config` normalizing non-`ConfigError` exceptions (e.g. cyclic-symlink `.resolve()` failures) into `ConfigError` at the load boundary — do not bypass that by calling `_validate` / `_apply_defaults` directly from a new call site.
- **Load-bearing warnings reach stderr even in quiet mode (v0.8.1).** The visible-failure contract extends beyond `ConfigError` to a curated set of degradation signals that quiet-mode used to swallow: corrupt-manifest sidecar recovery, corrupt-manifest peer-fallback recovery, "no sync sources" misconfig in autopush, durability `fsync_dir` failure on pull, and the autopull `total_failed` per-file summary. Each emits a single `mm: warning: ...` (or `mm: ...`) line to stderr and continues. Do NOT add a new `if not quiet:` gate around a warning that signals data-at-risk degradation — match the established pattern (always-stderr, prefixed `mm:`).
- **`autopush` writes a `no-sources` breadcrumb (v0.8.1)** when `get_sources(config)` returns empty, distinguishing "broken config no-op" from "nothing to push" no-op. Without this, `mm status` only sees `outcome: "success"` forever and monitoring on top of it never catches the wedge.
- See README.md "Claude Code Integration" section for CLAUDE.md snippet

## Auto-upgrade nudge (v0.9.5)

`mind_meld.upgrade` runs a leading-edge version check to nudge the fleet toward
the latest tag before fleet-version refusal trips. Single cache file at
`~/.config/mind-meld/upgrade-state.json`, fcntl-flocked on every read+modify+write
so transition detection is race-correct under two concurrent mm processes.

**Approach A: nudge-only.** mm NEVER invokes pipx itself. The `mm: notice:` line
prints the upgrade command; the user runs it. Subprocess pipx execution is
deferred (see TODOS) for managed-pipx / rollback / UX reasons, NOT process-
replacement impossibility (`execvp` would work fine).

**Version source: tag-based.** `/repos/kbitz/mind-meld/tags?per_page=100` →
`packaging.Version` filter (skip `is_prerelease` AND skip `local is not None` —
the latter because `0.9.4+local > 0.9.4` per packaging) → max-semver. Cap at
100 tags is documented in `upgrade.py`; revisit when fleet has more than 100
releases (~3 years at current velocity). Why tags not raw-pyproject-on-main:
HEAD may be mid-bump or contain WIP that hasn't been tagged for release.

**3 hook seams in cli.py:**
1. **Transition detection** (`upgrade.run_transition_hook`) called AFTER each of
   3 load_config sites: `_get_config`, `_auto_command_setup`, `init_cmd`. Codex
   outside voice flagged that refactoring all 3 through `_get_config` would break
   `_auto_command_setup`'s silent-on-missing-config contract — preserved by
   shared-helper pattern instead.
2. **Nudge emission** (`upgrade.emit_nudge_if_due`) at the TAIL of
   `_pull_core`/`_push_core` (quiet AND interactive paths) AFTER main work
   completes. Tail position keeps cold-cache HTTP latency (~500ms 1x/24h) from
   stacking on sync latency.
3. **Status surfacing** in `mm status` — reads cache only, no network call,
   no last_nudged_at gate (explicit user check).

**Lock-order invariants (load-bearing):** NEVER acquire mm lockfile while holding
upgrade-state's flock; RELEASE upgrade-state's flock BEFORE appending to
pullhistory. Transition detection runs OUTSIDE the mm lock by design — its
correctness is bounded by upgrade-state's own flock.

**`mm: notice:` prefix is distinct from `mm: warning:`.** Curated stderr taxonomy:
- `mm: warning:` — data-at-risk signals (corrupt-manifest recovery, fsync
  failure, no-sources misconfig, etc.). Reader trains attention on this prefix.
- `mm: notice:` — FYI signals (auto-upgrade nudge today; future "new feature"
  hints). Adding non-data-at-risk signals to `warning:` would dilute the
  warning class.

**`pullhistory` schema extension.** New `verb: "self-upgrade"` row class peer to
pull/push, with `old_version`/`new_version` (NO source/rel_path/action). Written
via `pullhistory.append_self_upgrade(...)` (NOT extending `append()` — separate
event class, separate function). Contract violations silent-skip (NOT assert) so
forensic log failures don't block sync. `mm log` table renderer adds an `extra`
column showing `OLD → NEW` for self-upgrade rows; pull/push rows leave it empty.

## Release discipline (enforced by mm auto-upgrade)

**Tag = release. Merge to main alone is not.**

The auto-upgrade feature reads the latest tag from `/repos/kbitz/mind-meld/tags`
and nudges the fleet to upgrade to it. /ship is responsible for tagging.

- **Non-breaking ships:** bump `pyproject.toml` + commit + tag (`git tag vX.Y.Z`
  + `git push --tags`). Fleet sees the nudge within 24h.
- **Mid-feature WIP merges to main:** land without a tag. Fleet stays on the
  prior tagged version until a fresh tag is pushed.
- **Pre-release tags** (containing `-rc`, `-alpha`, `-beta`, `-dev`) and
  **local-version tags** (`+local`) are filtered out by `_pick_latest_tag` —
  tag freely for testing.

Skipping this discipline does not break sync, but it can leak unfinished
features to the fleet on the next push to main if you forget to NOT tag.

## Spec
See SPEC.md for full architecture and data model.
See docs/designs/mind-meld-v1.md for design decisions from spec review.
See docs/designs/sync-gstack-context.md for multi-source sync design (gstack support).
