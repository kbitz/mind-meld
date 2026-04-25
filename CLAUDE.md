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
- **Conflict resolution.** Detects and resolves iCloud and Dropbox-style conflict copies on manifest files. For source files with divergent local edits, preserves the local version as `<stem>.sync-conflict-<ts>-<device>.<ext>` (Syncthing convention) so edits are never destroyed. Mtime-skip: if the local file is newer than remote, pull leaves it alone.
- **Sync log.** After pull, writes `.mind-meld-log.md` per project so Claude Code knows what changed from other machines.
- Manifest-based diffing: SHA-256 hash every file, only upload/download changes.
- Content-addressed storage: blobs stored by hash, not by path.
- Gzip compression before encryption. Versioned blob format (v0x01).

## Source Layout
src/mind_meld/{cli,manifest,crypto,errors,devices,config,lockfile,synclog,merge,sidecar,pullhistory}.py
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

## exclude_patterns + consumer-boundary filter (load-bearing, v0.9.1)
Per-source `exclude_patterns: list[str]` of fnmatch globs is matched against the relative path. Default `gstack` source ships with `["projects/*/repo-mode.json", "projects/*/land-deploy-confirmed"]` (per-machine artifacts that churn-conflict on every pull). The walker drops excluded paths from the local manifest at push time.

`_filter_excluded_paths(manifest, exclude_map)` applies at TWO consumer-boundary call sites — both AFTER `_fetch_remote_manifest` returns: (1) `_pull_core` filters peer manifests in `manifest_cache` BEFORE `collect_tombstones` and the per-source download loop; (2) `_push_core` filters the manifest returned by `_recover_prior_manifest` (covers ok / sidecar / peer-fallback uniformly) BEFORE `generate_tombstones`. The filter MUST NOT apply at `_fetch_remote_manifest` itself — `mm gc` reads raw manifests via that path to compute referenced blobs, and a filtered manifest there would mark live peer blobs as orphans (codex-2 #1, pinned by `test_mm_gc_does_not_orphan_excluded_path_blobs`).

**Tombstone-suppression invariant.** Adding a path to `exclude_patterns` must NOT generate a deletion tombstone on the next push (kb-mbp 2026-04-24 regression). Removing a glob brings the path back as new. Sidecar recovery is filtered too so a corrupt-manifest recovery on a freshly-migrated config doesn't re-introduce pre-exclude paths via the sidecar (codex-2 #2). All four scenarios (kb-mbp two-device, tombstone-on-exclude, tombstone-on-unexclude, sidecar-bypass-guard) are pinned in `tests/test_integration.py::TestExcludePatterns5C`.

**Visible-failure contract for migration UX (v0.9.1).** Existing configs need to opt in by running `mm migrate-config`. autopull / autopush NEVER auto-mutate config — they record the missing-excludes signal to `~/.config/mind-meld/migration-state.json` and let `mm status` surface it. Interactive `mm pull` / `mm push` prompt-once. Silent config mutation in a hook would be exactly the class of "wedged sync I never noticed" failure the visible-failure contract exists to prevent. Add the new "config missing recommended excludes" warning to the existing curated stderr signal set (corrupt-manifest recovery, fsync failures, no-sources misconfig, etc.).

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

## Spec
See SPEC.md for full architecture and data model.
See docs/designs/mind-meld-v1.md for design decisions from spec review.
See docs/designs/sync-gstack-context.md for multi-source sync design (gstack support).
