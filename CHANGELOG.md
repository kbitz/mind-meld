# Changelog

All notable changes to Mind Meld will be documented in this file.

## [0.6.2] - 2026-04-23

Track 1B: Walker conflict-file exclusion + manifest read-path hardening.
Continues the Group 1 correctness foundation alongside Track 1C (v0.6.0) and
Track 1D (v0.6.1).

### Fixed
- **Conflict-copy files propagated fleet-wide on next push.** v0.4.0 shipped Syncthing-style local conflict copies (`<stem>.sync-conflict-<ts>-<device>.<ext>`) but the manifest walker did not exclude them. The next `mm push` walked the conflict file, hashed it, uploaded it, and other devices received it as a regular source file — turning one local conflict into N cross-device conflict files. The walker now skips conflict files via a strict pattern pinned to mm's exact emitted format (`*.sync-conflict-[0-9]{8}-[0-9]{6}-*`), eliminating the false-positive class entirely while leaving user files like `notes.sync-conflict-log.md` and `notes.sync-conflict-2024-summary.md` alone.
- **`_find_conflict_files` and `mm gc --conflicts` could delete user files.** The previous loose substring check (`CONFLICT_INFIX in name`) matched user files like `notes.sync-conflict-log.md` and the GC reaper would silently delete them after 30 days. Replaced with the strict `is_conflict_filename` predicate.
- **Manifest read-path normalization was correctness-by-vigilance.** Each caller of `_fetch_remote_manifest` had to remember to call `normalize_manifest`. The pull-side `collect_tombstones` over peer manifests bypassed it entirely — a malformed-key tombstone in any peer manifest would silently fail `is_tombstoned`, causing deleted files to re-download. New `load_manifest(bytes) -> dict` (= `deserialize_manifest + normalize_manifest` + full inner-shape validation) is the single load boundary; `_fetch_remote_manifest` and `sidecar.read` route through it. The 6 redundant scattered `normalize_manifest` calls in `cli.py` are removed; the contract is now load-time guaranteed.
- **`load_manifest` validates inner shapes (cross-model adversarial finding).** Both Claude and Codex independently flagged that a partial top-level shape check still left inner-shape garbage (e.g., `{"sources": {"claude": "x"}}` or non-dict tombstone values) to crash downstream `_merge_manifests`, `collect_tombstones`, or the diff loop with `AttributeError`. `load_manifest` now rejects non-dict source entries, non-dict `files` dicts, and non-dict tombstone values with `ManifestError`. `_fetch_remote_manifest` already catches `ManifestError` and falls through to the recovery chain, so a malformed peer manifest now degrades to a clean "corrupt" status instead of a hard command crash.
- **Defensive: bare-path tombstone migration during v1→v2 promotion.** No shipped mm version emits bare-path tombstone keys (tombstones were introduced after v2 sources), but hand-edited v1 manifests, test fixtures, or external tooling could. `normalize_manifest` now migrates bare-path tombstones to `claude:<path>` only inside the v1→v2 promotion branch, where the source is unambiguously claude. Outside that branch, ambiguous keys are preserved verbatim — `is_tombstoned` returning False is the safe default for adversarial data.

### Added
- `is_conflict_filename(name)` predicate in `manifest.py` (with `CONFLICT_INFIX` and `CONFLICT_PATTERN` constants), used by the walker, `mm conflicts`, `_canonical_for_conflict`, and `mm gc --conflicts`.
- `load_manifest(bytes)` in `manifest.py` — single canonical load boundary returning a v2-normalized manifest with full inner-shape validation. Use this instead of `deserialize_manifest` (which stays pure: bytes → dict) for any path that loads a manifest from disk.
- Hypothesis-based property fuzz tests over manifest shapes (`tests/test_manifest_fuzz.py`): normalize idempotency, no-crash on arbitrary dicts, `load_manifest` invariant preservation, `is_conflict_filename` never crashes.
- `hypothesis>=6.0` to dev dependencies.

### For contributors
- Module docstring in `manifest.py` and `sidecar.py` document the read-path invariant: every manifest loaded from bytes/disk MUST go through `load_manifest`. `sidecar.read` uses `deserialize + structural-check on raw + normalize` deliberately, to preserve its anti-tampering guard against tampered sidecars missing structural keys.
- `CLAUDE.md` and `SPEC.md` (Merge invariants section) document the new read-path invariant.
- 49 new tests added (8 fuzz + 41 unit/integration/regression). Total suite is now 279 tests.

## [0.6.1] - 2026-04-23

Track 1D: Storage layer hardening. Crash-safe primitives, kernel-enforced
concurrency, validator-gated conflict detection.

### Added
- **`mind_meld.fsutil`**: unified atomic-write + directory-fsync primitives (`atomic_write_bytes(path, data, *, fsync=False, mode=None)` and `fsync_dir(path)`). On Darwin, `fsync=True` uses `fcntl(fd, F_FULLFSYNC)` with fallback to `os.fsync` — per Apple's `fsync(2)` man page, plain fsync on macOS only pushes to the disk controller, not through the disk cache, so `F_FULLFSYNC` is the correct primitive for power-loss durability. Replaces three separate atomic-write implementations (`sidecar.py`, `storage/local.py:LocalBackend.put`, `cli.py:_atomic_write`). On any write/replace/fsync failure, the tmp file is unlinked before `StorageError` is raised — no orphan `tmp*.tmp` can remain. The `mode` parameter preserves the target's existing permissions by default (or uses `0o666 & ~umask` for new files), so pull-apply writes no longer silently downgrade user files to 0o600.
- **Deferred-durability pull**: pull-apply per-file writes skip fsync; at end of `_pull_core` each unique parent directory is fsynced exactly once via `fsutil.fsync_dir`. A 500-file pull now costs ~3 dir syncs instead of 500 F_FULLFSYNC pairs.
- **`mm gc` tmp sweep**: reaps stale `tmp*.tmp` files left behind by crashed atomic-write calls. Scoped strictly to this device's subtrees (`data/<my_device_id>/`, `manifests/<my_device_id>/`). Peer subtrees are never touched because iCloud may be mid-uploading a peer's tmp file. `devices/` is a flat shared directory with no per-device subdir, so it's intentionally excluded — global orphan reaping is deferred to Track 3A.

### Changed
- **Lockfile**: rewritten to use `fcntl.flock(LOCK_EX|LOCK_NB)` — kernel-enforced, auto-released on process exit. Module-level `_LOCK_FDS: dict[str, int]` keyed by realpath (same physical lockfile via symlink/relative/absolute path correctly collides). The lockfile body still carries the holder's PID for diagnostics: when another process holds the lock, `LockError` surfaces "PID {n}". Crashed processes no longer strand the lock (the kernel releases it on fd close). Stale-PID detection logic deleted (~30 LOC). `EINTR` on `flock()` is retried once. `release_lock` no longer unlinks the lockfile — doing so created the classic advisory-lock race.
- **`LocalBackend.put` durability policy**: writes to `manifests/` and `devices/` keys are now `F_FULLFSYNC`-durable. `data/` blob writes stay non-fsynced (blobs are hash-addressed and self-healing via re-push). Every storage write now passes `mode=0o600` explicitly so new files aren't world-readable via umask.
- **`find_conflict_copies(key, is_valid=None)`** and **`delete_conflict_copies(key, is_valid=None)`**: new optional predicate. When provided, only candidates for which `is_valid(path)` returns True are returned. `cli.py` passes a validator that decrypts + `deserialize_manifest`-shape-checks each candidate so a random file whose name matches the iCloud/Dropbox rename pattern cannot fool `_fetch_remote_manifest` into flipping `status=missing` to `status=corrupt`. Predicate exceptions are caught and logged to stderr. Backward-compatible — crypto-v2's `mm-crypto-init` bootstrap path uses the 1-arg form (it validates each candidate itself via `_parse_crypto_init`).
- **`config.py:save_config`**, **`synclog.py:write_sync_log`**, and **`sidecar.py:write`** all migrated to `fsutil.atomic_write_bytes`. Config and sidecar writes are durable (`fsync=True`); sync-log writes are not (cosmetic, pull-hot-path).

### Fixed
- **Tmp-file leak on crash.** `LocalBackend.put` previously left stranded `tmp*.tmp` siblings in `data/`, `manifests/`, and `devices/` if a write was interrupted. All writes now route through `fsutil.atomic_write_bytes`, which unlinks the tmp on any failure.
- **Lockfile PID race (CLAUDE.md autopull / autopush hot path).** Two concurrent `mm` invocations could both pass the "stale detected" check before one atomically re-created the lock, producing misleading "Another mm operation just started" errors. `fcntl.flock` is kernel-enforced and race-free.
- **Lockfile unlink-on-release race.** `release_lock` used to unlink the lockfile as part of cleanup. This created the classic advisory-lock race: between release and unlink a second process could open the live inode and flock it, then a third process could `O_CREAT` a fresh inode and flock THAT — two "holders" on different inodes. `release_lock` now leaves the lockfile body on disk (diagnostic only); the next `acquire_lock` truncates before writing the new PID.
- **Silent 0o600 downgrade on pull.** `fsutil.atomic_write_bytes` uses `mkstemp` which creates tmp files with mode 0o600; `os.replace` preserves the SOURCE mode. On every pull-apply, user files in `~/.claude/projects/*/memory/*.md` were silently chmodded from their existing mode (typically 0o644) down to 0o600. `atomic_write_bytes` now preserves the target's existing mode (or uses `0o666 & ~umask` for new files) by default; storage-layer writes (encrypted secrets) pass `mode=0o600` explicitly.
- **sidecar.write StorageError not caught on push.** The fsutil migration changed sidecar.write's exception type from OSError to StorageError; the best-effort handler in `_push_core` still caught only OSError, so a failed sidecar write would crash the whole push with an unhandled exception. Handler now catches both.
- **Bogus sibling spoofs corrupt-manifest recovery.** A random file in `manifests/<device>/` whose name happened to match the iCloud conflict pattern flipped `had_any_source` to True, mis-routing `_fetch_remote_manifest` from `status=missing` into `status=corrupt` and invoking the recovery chain when storage was actually fine. Validator gate fixes this.
- **Closes `TODOS.md #1`** (sidecar fsync durability): sidecar writes now use `atomic_write_bytes(fsync=True)`, so a sidecar that was renamed but not flushed can no longer silently vanish on crash.
- **Unbounded Argon2 on conflict-copy validation.** `_fetch_remote_manifest` runs the validator on every regex-matching sibling in `manifests/<device>/`. With 20 stale iCloud conflicts the cost was 4-10s of Argon2 per fetch. The validator now reads the first byte and short-circuits on any value != `FORMAT_VERSION`, bounding non-manifest sibling cost to ~1ms.
- **Validator fragility.** A single malformed candidate (e.g., stale passphrase after `mm init`, unexpected `argon2.exceptions.*`) could crash the whole recovery sweep. The validator now catches `Exception` at its boundary — one bad sibling is skipped, not fatal.
- **Symlinked lockfile aliasing.** `_resolve_key` used `Path.resolve(strict=True)` which only handled parent-dir symlinks. A lockfile that was itself a symlink bypassed the "already holds" guard. Switched to `os.path.realpath` which resolves symlinks across the full path.

### For contributors
- On Darwin, prefer `fcntl(fd, F_FULLFSYNC)` over `fsync(fd)` for power-loss durability. The `fsutil._fsync_fd` helper encapsulates this — all new durability code should route through it, not call `os.fsync` directly.
- `_cleanup_conflict_copies(backend, device_id, passphrase, memory_kb)` signature gained `passphrase`/`memory_kb` so the validator can decrypt + deserialize candidates. Two callers updated (`_push_core`, `_pull_core`).
- The unified atomic-write helper should be the single path for every write primitive going forward. Any new ad-hoc `.write_bytes`/`.write_text` call should route through `fsutil.atomic_write_bytes` instead, with an explicit fsync policy decision (durable state? → `fsync=True`. regenerable output? → `fsync=False`.)

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
