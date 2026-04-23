# Changelog

All notable changes to Mind Meld will be documented in this file.

## [0.6.0] - 2026-04-23

### Fixed
- **Conflict-copy files propagated fleet-wide on next push.** v0.4.0 shipped Syncthing-style local conflict copies (`<stem>.sync-conflict-<ts>-<device>.<ext>`) but the manifest walker did not exclude them. The next `mm push` walked the conflict file, hashed it, uploaded it, and other devices received it as a regular source file — turning one local conflict into N cross-device conflict files. The walker now skips conflict files via a strict pattern (`*.sync-conflict-[0-9]*-*`) that matches mm's emitted format including extensionless cases like `README.sync-conflict-...`, while leaving user files like `notes.sync-conflict-log.md` alone.
- **`_find_conflict_files` and `mm gc --conflicts` could delete user files.** The previous loose substring check (`CONFLICT_INFIX in name`) matched user files like `notes.sync-conflict-log.md` and the GC reaper would silently delete them after 30 days. Replaced with the strict `is_conflict_filename` predicate.
- **Manifest read-path normalization was correctness-by-vigilance.** Each caller of `_fetch_remote_manifest` had to remember to call `normalize_manifest`. The pull-side `collect_tombstones` over peer manifests bypassed it entirely — a malformed-key tombstone in any peer manifest would silently fail `is_tombstoned`, causing deleted files to re-download. New `load_manifest(bytes) -> dict` (= `deserialize_manifest + normalize_manifest`) is the single load boundary; `_fetch_remote_manifest` and `sidecar.read` route through it. The 6 redundant scattered `normalize_manifest` calls in `cli.py` are removed; the contract is now load-time guaranteed.
- **Defensive: bare-path tombstone migration during v1→v2 promotion.** No shipped mm version emits bare-path tombstone keys (tombstones were introduced after v2 sources), but hand-edited v1 manifests, test fixtures, or external tooling could. `normalize_manifest` now migrates bare-path tombstones to `claude:<path>` only inside the v1→v2 promotion branch, where the source is unambiguously claude. Outside that branch, ambiguous keys are preserved verbatim — `is_tombstoned` returning False is the safe default for adversarial data.
- **`load_manifest` validates inner shapes (cross-model adversarial finding).** Both Claude and Codex independently flagged that the initial `load_manifest` only checked top-level `sources` and `tombstones` were dicts, leaving inner-shape garbage (e.g., `{"sources": {"claude": "x"}}` or non-dict tombstone values) to crash downstream `_merge_manifests`, `collect_tombstones`, or the diff loop with `AttributeError`. `load_manifest` now also rejects non-dict source entries, non-dict `files` dicts, and non-dict tombstone values with `ManifestError`. `_fetch_remote_manifest` already catches `ManifestError` and falls through to the recovery chain, so a malformed peer manifest now degrades to a clean "corrupt" status instead of a hard command crash.
- **Walker conflict-file pattern pinned to mm's exact emitted format (cross-model adversarial finding).** Both reviewers caught that the initial strict pattern `*.sync-conflict-[0-9]*-*` still false-positive-matched user files like `notes.sync-conflict-2024-summary.md` (only 4 digits before the next dash). Pattern is now pinned to the exact 8+6-digit timestamp shape `conflict_filename()` emits, eliminating the false-positive class entirely. User files with `.sync-conflict-` plus partial digits are now correctly synced and never reaped by `mm gc --conflicts`.

### Added
- `is_conflict_filename(name)` predicate in `manifest.py` (with `CONFLICT_INFIX` and `CONFLICT_PATTERN` constants), used by the walker, `mm conflicts`, `_canonical_for_conflict`, and `mm gc --conflicts`.
- `load_manifest(bytes)` in `manifest.py` — single canonical load boundary returning a v2-normalized manifest. Use this instead of `deserialize_manifest` (which stays pure: bytes → dict) for any path that loads a manifest from disk.
- Hypothesis-based property fuzz tests over manifest shapes (`tests/test_manifest_fuzz.py`): normalize idempotency, no-crash on arbitrary dicts, load_manifest invariant preservation, is_conflict_filename never crashes.
- `hypothesis>=6.0` to dev dependencies.

### For contributors
- Module docstring in `manifest.py` and `sidecar.py` document the read-path invariant: every manifest loaded from bytes/disk MUST go through `load_manifest`. Do not add new load paths that bypass it.
- `_fetch_remote_manifest` docstring updated: returned `manifest` is pre-normalized; callers may rely on `sources`/`tombstones` keys + v2 shape.
- 38 new tests added (8 fuzz + 30 unit/integration). Total suite is now 268 tests.

## [0.5.1] - 2026-04-22

### Fixed
- **Silent tombstone loss on corrupt manifest.** When iCloud corrupts this device's manifest, `mm push` used to quietly write a replacement with zero tombstones — silently un-deleting files across the fleet on the next pull. Push now runs a recovery chain: local sidecar (`~/.config/mind-meld/last-push.json`, written atomically at the end of every successful push) → peer-manifest tombstone aggregation → refuse with actionable error if neither is available. Sidecar recovery preserves this device's fresh local deletions; peer fallback preserves only propagated ones (warning fired either way). `mm gc` refuses to reap blobs when any peer has a corrupt manifest (those blobs may still be referenced).
- **First-push refuse.** The fetch API conflated "no manifest yet" with "manifest corrupt." First push on a single-device install would have tripped the new refuse path. `_fetch_remote_manifest` now returns a tri-state `ManifestFetch(status: "ok"|"missing"|"corrupt", manifest)`. All 5 callers (`push`, `pull`, `status`, `diff`, `gc`) updated.
- **Stale-sidecar and cross-device reuse.** `sidecar.read` requires a `device_id` argument and refuses sidecars whose structural shape (`sources`/`tombstones` as dicts) or `device_id` doesn't match — prevents an old `mm init` from bulk-tombstoning the new device's files.
- **Broken recovery on flaky storage.** `_fetch_remote_manifest` now catches `OSError`/`MindMeldError` on `backend.get()` (TOCTOU between `exists()` and `get()`); `_collect_peer_tombstones` wraps per-peer fetches in try/except so one flaky peer can't crash the whole recovery.
- **Corrupt manifest stayed corrupt.** `mm push` after recovery now always rewrites the remote manifest — even when local file diffs are zero — so recovered tombstones actually propagate.
- **Auto-GC swallowed refuse.** Auto-GC after push used to wrap `_do_gc` in a blanket `except Exception: pass` which would silently eat the new refuse-on-corrupt error. Narrowed to let `typer.Exit` propagate.
- **Version-drift across files.** `VERSION` was 0.4.0 while `pyproject.toml` and `__init__.py` were 0.5.0 (the rename PR bumped two of three). `VERSION` file deleted; `__init__.py` now reads `importlib.metadata.version("mind-meld")` with `PackageNotFoundError → "0.0.0+dev"` fallback for source-tree runs. `pyproject.toml` is the single source of truth.

### Added
- `mm --version` prints the installed version and exits.
- `mm status` and `mm diff` now distinguish "no remote manifest yet" from "remote manifest CORRUPT" so users see the actual state.

### For contributors
- `SPEC.md` gains a "Merge invariants" section documenting the load-bearing union-for-files + newest-wins-for-tombstones + `is_tombstoned()`-gate invariant that keeps the lossy manifest walker safe. Every new consumer of a merged manifest MUST check `is_tombstoned(source, rel_path, aggregated_tombstones)` before acting on a file entry.
- `pyproject.toml` is now the single source of truth for the release number; `__init__.py` reads it via `importlib.metadata`. The `VERSION` file is deleted.

## [0.5.0] - 2026-04-22

### Changed
- **Project renamed** from `memsync` / `msync` to `mind-meld` / `mm`. Clean rename: no migration shims.
  - PyPI package: `memsync` → `mind-meld`
  - CLI binary: `msync` → `mm`
  - Python package: `memsync` → `mind_meld`
  - Config dir: `~/.config/memsync/` → `~/.config/mind-meld/`
  - Default storage: `.../CloudDocs/memsync/` → `.../CloudDocs/mind-meld/`
  - Keyring service: `memsync` → `mind-meld`
  - Env var: `MEMSYNC_PASSPHRASE` → `MINDMELD_PASSPHRASE`
  - Per-project sync log: `.memsync-log.md` → `.mind-meld-log.md`
- **Existing installs must:** `pipx uninstall memsync && pipx install mind-meld`, move the iCloud folder, re-run `mm init`, and re-enter the passphrase. Old keyring entry under service `memsync` is orphaned (delete via Keychain Access).

## [0.4.0] - 2026-04-21

### Added
- Conflict-copy preservation on `mm pull`: when local and remote versions of a non-mergeable file diverge, the losing local version is renamed to `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device>.<ext>` (Syncthing convention) and the remote wins the canonical path. Local edits are never destroyed.
- Mtime-based skip: if the local file is newer than remote, pull leaves it untouched. Convergence happens on the next push.
- `mm conflicts` — list every `.sync-conflict-*` file across synced sources with age and canonical sibling.
- `mm resolve [<path>]` — interactive picker showing a unified diff and prompting keep canonical / force conflict to canonical / keep both / abort. Acquires the mm lockfile to race-guard against autopull.
- `mm gc --conflicts` — reap stale conflict files older than 30 days.
- `mm pull --resolve-interactive` — prompt per-conflict during pull instead of defaulting to keep-both.
- `mm pull --no-prompt` — explicit no-prompt mode for scripting.
- `mm diff` now annotates each modified path with its predicted pull outcome (write / merge / skip / conflict).
- `.mind-meld-log.md` now includes `## Conflicts` and `## Skipped (local was newer)` sections so Claude Code sees resolution work when reading cross-machine context.

### Changed
- `PullResult` split counts: `total_written`, `total_merged`, `total_skipped`, `total_conflicted`, `total_failed` replace the single `total_new`/`total_modified` pair. Pull summary and autopull one-liner updated to match.
- Pull re-reads local hash and mtime at apply time so decisions reflect the file's actual state when written (race-safe against concurrent editors during a pull).
- `_download_and_apply` extracted into `_apply_incoming_file` with a documented decision tree (W / U / M / S / C branches).
- `EXCLUDED` patterns now include `*.tmp` so atomic-write leftovers from disk-full failures don't propagate cross-device.
- `_atomic_write` cleans up its `.tmp` sibling on write or rename failure instead of leaving orphan files in the synced tree.

### Fixed
- Pull reporting now fires the iCloud/Dropbox manifest-cleanup path when a device produces only skips or failures, preventing long-term manifest conflict-copy bloat on one-way-sync setups.
- `_canonical_for_conflict` uses `rfind` so a conflict-of-a-conflict file unwinds the outermost layer correctly.
- `gc` command's internal `conflicts` parameter renamed to `prune_conflicts` to stop shadowing the top-level `conflicts` command (CLI flag `--conflicts` unchanged).
- `_find_conflict_files` walks only synced paths (`SYNCED_SUBDIRS` for claude, `include_dirs` for generic) instead of the full source tree, avoiding noise from `.sync-conflict-*` files in unsynced areas.

## [0.3.0] - 2026-04-09

### Added
- Additive-only pull model: pull never deletes local files, only adds new and merges modified
- Tombstone mechanism with 30-day expiry for intentional deletes across machines
- Source-scoped tombstone keys (`source:path`) to prevent cross-source suppression
- MEMORY.md line-based merge on pull (preserves index entries from all machines)
- Additive iCloud/Dropbox conflict manifest resolution (union of all files across conflict copies)
- Auto garbage collection after interactive push (not autopush)
- `merge_file()` dispatcher for extensible per-filetype merge strategies

### Changed
- Extracted `_push_core()` and `_pull_core()` shared by interactive and auto commands (DRY refactor)
- `_fetch_remote_manifest()` is now read-only with separate `_cleanup_conflict_copies()` for write paths
- `_do_gc()` now returns orphan count for auto-GC output
- `normalize_manifest()` now ensures `tombstones` key exists on all manifests

### Fixed
- Dropbox conflict regex now checks base filename (not just extension), preventing false matches
- Pull counts now reflect actual files downloaded (not inflated by tombstone-filtered files)
- `dry_run=True` with `quiet=True` no longer falls through to actual file writes

## [0.2.0] - 2026-04-08

### Added
- Multi-source sync with gstack support
- Configurable sync sources via `[[sync.sources]]` in config
- JSONL merge strategy for append-only files
- Per-source pull/status/diff flags
- `mm sources` command

## [0.1.0] - 2026-04-07

### Added
- Initial release: push, pull, status, devices, diff, gc commands
- iCloud Drive storage backend with end-to-end AES-256-GCM encryption
- Manifest-based diffing with SHA-256 content addressing
- Scoped sync (memory/ and todos/ only)
- Cross-machine sync log (.mind-meld-log.md)
- autopull and autopush for Claude Code integration
