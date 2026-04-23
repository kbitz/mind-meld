# Changelog

All notable changes to Mind Meld will be documented in this file.

## [0.6.0] - 2026-04-22

### Changed
- **Crypto rewrite: process-scoped master key + HKDF per file (Track 1C).**
  The per-file Argon2id derivation shipped in 0.5.x cost ~150ms per file. A
  1000-file push burned ~4 minutes of CPU in crypto alone. v0.6 moves to the
  pattern age, restic, and rclone use:
  - `mm init` writes `mm-crypto-init` at the storage root: a single atomic
    blob containing `[version][argon2_memory_kb][root_salt][keycheck_blob]`.
  - Argon2id runs once per process to derive a master_key (cached).
  - Per-file keys are HKDF-SHA256(master_key, per_file_salt, b"mm-file-v2"),
    which takes microseconds.
  - Measured speedup at production Argon2 params (64MB memory cost): encrypt
    per-op 123ms → 0.07ms (~1760x), decrypt per-op 122ms → 0.01ms (~12200x).
    End-to-end 100-file round-trip: 24.4s → 0.14s.
- **Blob format v2.** `[version=0x02][salt:16][nonce:12][ciphertext+gcm_tag]`.
  v1 blobs (format byte 0x01) are recognized and rejected loudly — Mind Meld
  is pre-release and no v1 blobs exist in the wild. Downgrading to 0.5.x after
  any v0.6 push will NOT work; stay on 0.6.x once you upgrade.
- **`argon2_memory_kb` is now stored in `mm-crypto-init`**, not per-device
  config. All devices use the value written by the first-device `mm init`.
  `[crypto].argon2_memory_kb` in local config is a seed used only on
  first-device bootstrap; subsequent devices read the authoritative value
  from storage. Prevents silent key-derivation drift between devices.
- **`mm init` now branches first-device vs second-device.** First device
  double-prompts (set a new secret), generates mm-crypto-init, bootstraps.
  Subsequent devices single-prompt, decrypt the keycheck blob to verify the
  passphrase, and only then write local config + register the device +
  store the passphrase in the keyring. A typo'd passphrase on a second device
  aborts cleanly with no local state written.

### Added
- `LocalBackend.put_exclusive(key, data)` — atomic create-only primitive
  implemented as temp-write + `os.link` (atomic AND EEXIST-exclusive). Used
  by `bootstrap_crypto_init` for race-safe mm-crypto-init creation.
- iCloud conflict resolution for `mm-crypto-init`. Two devices running
  `mm init` simultaneously both write locally; iCloud reconciles later by
  renaming one to `mm-crypto-init 2`. `fetch_crypto_init` picks the
  deterministic winner (lex-smallest root_salt), canonicalizes it, and
  deletes the loser. Every command runs this path at start so state stays
  convergent.
- `[crypto].root_salt_fp` in local config — 16-char hex fingerprint of the
  storage's root_salt. On every command, we compare this to the current
  storage fingerprint. Drift → refuse with actionable error ("Another device
  may have bootstrapped concurrently. Re-run mm init.").
- `tests/benchmarks/test_kdf_timing.py` — ad-hoc benchmark for before/after
  crypto timing. Run locally via `python -m tests.benchmarks.test_kdf_timing`;
  paste numbers in the PR description.

### Fixed
- Extensionless iCloud conflict copies (e.g. `mm-crypto-init 2`) are now
  detected. Previously `_ICLOUD_CONFLICT_RE` required a file extension.
- GCM tag mismatch error message now names all three causes (wrong
  passphrase, wrong root_salt, corrupt blob) and suggests verifying
  mm-crypto-init integrity.
- Argon2 out-of-memory errors are translated to a user-actionable
  `CryptoError` pointing at `[crypto].argon2_memory_kb`.

### For contributors
- 45 new tests under `tests/test_crypto.py`, `tests/test_storage_local.py`,
  and `tests/test_integration.py` cover: master-key cache hits/misses,
  HKDF determinism, mm-crypto-init tri-state fetch, bootstrap race,
  deterministic winner + canonicalization, extensionless conflict regex,
  first-device + second-device init paths, wrong-passphrase abort,
  v1-blob refusal regression.
- `tests/conftest.py` centralizes: default crypto session for tests that
  call `encrypt`/`decrypt` directly, plus keyring isolation so the real OS
  Keychain can't leak into tests.
- See `docs/designs/crypto-v2.md` for the decision record, including the
  alternatives considered and why the `LRU by (passphrase, salt)` proposal
  in the original Track 1C entry was structurally broken (random per-file
  salts mean ~0% cache-hit rate).

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
