# Sync mechanics — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/cli.py` — `_pull_core` / `_push_core` / `_fetch_remote_manifest` / `_recover_prior_manifest` / `_filter_excluded_paths` / `_filter_disabled_sources` / `_drop_case_collisions_from_manifests`
- `src/mind_meld/fsutil.py` — `atomic_write_bytes` / `_fsync_fd` / `fsync_dir` / (see "Atomic write publication failures" below)
- `src/mind_meld/storage/local.py` — `LocalBackend.put` / `_needs_fsync`
- `src/mind_meld/lockedjson.py` — `locked_json_durable_rmw` / (see "Atomic write publication failures") / `locked_json_rmw` / `locked_json_snapshot` / (see "Shared JSON locking")
- `src/mind_meld/storage/keys.py` — `manifest_key` / `blob_key` / `device_key` / `parse_blob_key` / (see "Validated storage keys")
- `src/mind_meld/cli.py` — `autopull` / `autopush` / `_auto_command_setup` / (see "Visible failures in automatic commands")
- `src/mind_meld/host_usage.py` — `read_cursor_usage` / `configure_cursor_hook`
- `src/mind_meld/cli.py` — `_register_and_save` / `_quarantine_corrupt_manifest` / (also read init-devices.md)
- `src/mind_meld/resolveflow.py` — `_ensure_inversion_marker` / (also read conflicts.md)
- `src/mind_meld/attemptlog.py` — `write` / (also read events-retro.md)
- `src/mind_meld/manifest.py` — `walk_generic_source` / `walk_grok_source` / `load_manifest` / `collect_tombstones` / `generate_tombstones` / `marker_skip_globs`
- `src/mind_meld/manifest.py` — `nested_repo_root` / `nested_repo_skip_prefixes` / `nested_repo_roots_for_paths`
- `src/mind_meld/cli.py` — `_prove_omitted_paths_absent` / `_warn_push_growth` / `_freeze_nested_checkout_entries` / `_drop_unfrozen_checkout_files` / `_incoming_nested_roots` / `_filter_excluded_paths`
- `src/mind_meld/config.py` — `save_config` / `patch_config_on_disk` / `load_config` / the config.toml keys `exclude_patterns`, `disabled_sources`, `seen_sources` (TOML keys, not module symbols) and their consumer paths
- `src/mind_meld/seen_sources.py`
- `src/mind_meld/sidecar.py`
- `src/mind_meld/pullhistory.py`

Tests pinning the invariants below: `tests/test_integration.py::TestExcludePatterns5C`, `tests/test_integration.py::TestDisabledSourcesTombstoneSuppression`, `tests/test_integration.py::TestCompleteSnapshots`, `tests/test_case_collision.py`, `tests/test_recover.py`, `tests/test_recovery.py`, `tests/test_pullhistory.py`, `tests/test_seen_sources.py`, `tests/test_manifest_fuzz.py`.

---

## Shared JSON locking

`lockedjson.py` (v0.11.14, extended v0.12.22) — extracted single-file flock R/M/W primitive shared by `upgrade.py`, `token_usage.py`, and `identity.py` (v0.11.17). Its read-only shared-lock snapshot serves dry-run planners without creating, rewriting, re-permissioning, or normalizing a cache; R/M/W remains the exclusive mutation path. `locked_json_snapshot` (v0.14.11) takes an optional `blocking=False` so a caller (Grok's diag read) can report contention as unknown immediately instead of waiting; existing planners keep the blocking default. Three contention modes: `block` / `raise` / `warn`. Do NOT route new flock-guarded JSON caches through ad-hoc fcntl calls; extend `lockedjson` if the contract needs to grow. `devices-write.lock` stays ad-hoc — its multi-file lock-on-sibling shape doesn't fit the single-file R/M/W contract.

## Validated storage keys

Storage keys are constructed via helpers in `storage/keys.py` (`manifest_key`, `blob_key`, `device_key`, `parse_blob_key`), which validate components at construction time, so a corrupt or malicious peer manifest cannot smuggle a `sha256: "../../../etc/passwd"` through `backend.get`. Do NOT build storage keys with raw f-strings at new call sites.

## Visible failures in automatic commands

`ConfigError` (bad `config.toml`) surfaces as a one-line stderr message — not a silent exit. This is the visible-failure contract: truly unexpected errors still degrade silently via the generic `except Exception` fallback, but malformed config is loud so users don't wedge their background sync without noticing. Relies on `load_config` normalizing non-`ConfigError` exceptions (e.g. cyclic-symlink `.resolve()` failures) into `ConfigError` at the load boundary — do not bypass that by calling `_validate` / `_apply_defaults` directly from a new call site.

**Load-bearing warnings reach stderr even in quiet mode (v0.8.1).** The visible-failure contract extends beyond `ConfigError` to a curated set of degradation signals that quiet-mode used to swallow: corrupt-manifest sidecar recovery, corrupt-manifest peer-fallback recovery, "no sync sources" misconfig in autopush, durability `fsync_dir` failure on pull, and per-file apply failures. Apply failures print one `mm: warning:` line per failed file plus the count line. Do NOT add a new `if not quiet:` gate around a warning that signals data-at-risk degradation — match the established pattern (always-stderr, prefixed `mm:`).

**`autopush` writes a `no-sources` breadcrumb (v0.8.1)** when `get_sources(config)` returns empty, distinguishing "broken config no-op" from "nothing to push" no-op. Without this, `mm status` only sees `outcome: "success"` forever and monitoring on top of it never catches the wedge.

## Atomic write publication failures

[`fsutil.py`](../../src/mind_meld/fsutil.py)'s `atomic_write_bytes` publishes at
successful `os.replace`. Before replacement, this invocation preserves an
existing target's bytes or leaves an absent target absent, assuming no external
writer. Replacement changes the pathname, including replacing a symlink rather
than writing its referent; default mode lookup uses `stat`, which follows that
referent. With `fsync=True`, directory open/flush/close runs after replacement
and can fail while complete new bytes are visible and durability is unconfirmed.
Cleanup never rolls back publication. A caller must follow its own stop/readback
policy before deciding what to write next; a failed durable return alone does not
establish the disk state. With `fsync=False` no fallible step follows
`os.replace`, so an `OSError` / `StorageError` raised by the call means this
invocation did not replace the target (an asynchronous interrupt in the last
bytecodes is not covered). Pull's apply sites rely on that: a raised write
becomes `reporter.failed(...)`, which neither voids a deferred keep-local bump
nor registers a touched parent. Moving an apply site to `fsync=True` first needs
a publication signal from the primitive (see "Residual windows" below).

`fsync=False` is the default and requests no fsync calls or crash persistence.
`fsync=True` flushes the temp file before replacement and the parent afterward;
normal return confirms those platform-reported local flushes. `_fsync_fd`
prefers Darwin `F_FULLFSYNC`, falls back to `os.fsync` on ENOTSUP/EINVAL/EOPNOTSUPP,
and uses `os.fsync` directly elsewhere. Real I/O errors propagate. This is no
physical power-loss, iCloud upload or peer-delivery certification. Readback
establishes visible state only; a later flush does not retroactively establish
the failed operation's guarantee. `fsync_dir` alone does not flush file contents.

`errors.py:StorageError` subclasses `MindMeldError`, not `OSError`.
`atomic_write_bytes` wraps `OSError` inside its temp-write/replacement try, but
mode/stat errors other than FileNotFoundError occur before that try and escape
raw. `fsync_dir` wraps open/flush errors as `StorageError`, which propagates;
its finally-close can raise raw `OSError`, mask a flush error, and be wrapped by
`atomic_write_bytes` **after publication**. Neither type nor message prefix is a
publication-phase API. Only caught `OSError` triggers best-effort unlink of the
owned temp. Unlink failure, process death or other uncaught exceptions can strand
it. `retention.py:_sweep_local_tmp_files` covers only this device's storage
`data/<id>/` and `manifests/<id>/`; no reaper covers other temps (devices,
crypto-init, config, caches, markers, the skill store, or pull/resolve temps in
source trees, which the manifest walker's `*.tmp` exclusion keeps out of sync).

**Point-in-time inventory.** Re-enumerated on 2026-10-04 at HEAD
`e9e56dbe804dd2a109ebb0dc5c5ddc66cc53f583`: search `src/` with
`rg -n 'atomic_write_bytes|locked_json_durable_rmw' src`, resolve imports/aliases
and every call's omitted/literal/conditional `fsync`, then search `backend.put`,
`save_config` and `patch_config_on_disk` and follow their consumers, handlers and
next readers. An ad hoc AST call listing cross-checked the source trace: 19 helper
calls, six literal True, one conditional, eight False, four omitted (default
False). The seven durable writer families below (the six True calls plus the
conditional `LocalBackend.put`, which splits by key prefix into the crypto,
manifest and device rows, so nine rows) cover all currently found durable calls;
seven is an observed count, not an allowlist or an automated inventory gate.
`host_usage.py:configure_cursor_hook` (v1.5.0) adds a second
`locked_json_durable_rmw` call after this inventory; the counts above are not
recomputed, and its row below carries no verdict.
Re-enumerate when changing a writer or wrapper. Verdicts qualify the inspected
policy, not physical crash survival or every CLI retry.

| Durable writer and consumers | Error policy and next reader | Evidence verdict |
|---|---|---|
| `lockedjson.py:locked_json_durable_rmw` → `host_usage.py:read_cursor_usage` | The sibling lock spans read and atomic replacement; errors propagate through context exit/finally unlocking. Cursor catches `OSError` / `StorageError`, returns incomplete `io_error`, and publishes no successful observation. Its next invocation opens actual history again under that lock; `cursor_usage_diag` uses `locked_json_snapshot`. Pruned runs make this authoritative history. A taken standalone-completion batch (`cursor-standalone-spool.jsonl.merging`) is unlinked only after this write returns, so a failed write re-folds it idempotently on the next read. | **No demonstrated defect:** source shows fresh reads and no stale retry; `tests/test_lockedjson.py:TestDurableJson67A` pins exclusion/coherent snapshots. Cursor's existing file-flush test does not qualify its post-replacement CLI outcome. |
| `lockedjson.py:locked_json_durable_rmw` → `host_usage.py:configure_cursor_hook` (`~/.cursor/hooks.json`) | Same sibling-lock primitive. No write when the file already has the wanted state, and a symlinked file is refused before the primitive runs. `cli.py:_toggle_cursor_usage` catches `OSError` / `ValueError` / `RuntimeError` / `StorageError`: enable stops with an error before it writes consent, while disable has already revoked consent and only warns. Each re-run re-reads the actual file under the lock; `cursor_hook_state` reads it without a lock. | **Unassessed:** added after the inventory. Tests pin the symlink, wrong-version and malformed-JSON refusals; none injects a replacement or directory-flush fault. |
| `storage/local.py:LocalBackend.put` via `_needs_fsync` → `crypto.py:apply_crypto_init_repair` (canonical and preserved `mm-crypto-init*`) | `preserve` and canonical puts propagate failures before the conflict-unlink pass. `cli.py:_apply_verified_crypto_repair` only translates `NewerFormatError`; `_init_crypto_session` / `_bootstrap_or_verify_crypto` stop on other failures, with command-specific handling. Next `fetch_crypto_init` reads actual canonical/copies and creates a fresh repair plan, not a stale retry. | **No demonstrated defect:** inspected ordering retains conflict candidates on a failed put and preserves displaced lineage before replacement. This is optimistic local coordination, not a fleet transaction; see init-devices.md's reconciliation contract. |
| Same `LocalBackend.put` → `cli.py:_push_core` (`manifests/`) | Put failure stops before `content_accepted`, sidecar, last_seen and conflict cleanup. `push` catches `OSError` / `MindMeldError`; `_auto_command_scope` records failure for autopush. Push's finally then records any attended capture attempt as `push-failed` via `Attempt.stopped()`, and `_host_publication` judges publication from the unrefreshed sidecar. Next push/pull uses `_fetch_remote_manifest` and `_recover_prior_manifest` on actual storage; a following no-op push returns before `sidecar.write`, so the sidecar can stay one generation behind a visible manifest and later corrupt-manifest recovery would use it as prior state. | **Unknown:** no stale content retry was found, but after a late error that follows visible replacement, `push` says content was not pushed, the attempt record says `push-failed`, and `mm status` reports publication `unknown` for a manifest peers can read. Owner: CLI/sync maintainer; prove pre/post-failure receipt, warning, attempt/status and next-push sidecar behavior with isolated manifest-parent faults before proposing a repair. |
| Same `LocalBackend.put` → `devices.py:update_last_seen` (`devices/`) | Put errors propagate after the read inside `_devices_write_lock`, which yields without the lock once its retries are exhausted (its warning says the update is skipped; it is not). `_push_core` warns/continues for attended maintenance; unattended errors reach `_auto_command_scope`. Next `update_last_seen` re-GETs; `list_devices` / `list_devices_with_drops` read current registry bytes. Registration itself uses the excluded create-only writer below. | **No demonstrated defect** in publication-failure handling: current last_seen/version fields are recomputed from a fresh read, with no cached-state retry. Under lock contention the read-modify-write can run unlocked. `tests/test_storage_local.py:TestFsyncRouting` pins the conditional prefixes and mode, not every failure handler. |
| `sidecar.py:write` → `cli.py:_push_core` | Catches `OSError` / `StorageError`, warns and continues after successful manifest acceptance. It does not restore the old sidecar. Next `_recover_prior_manifest`, `_host_publication`, `_collect_diag_state` or `recover` calls `sidecar.read`, which opens current bytes and validates shape/device identity. | **No demonstrated defect** in the inspected read/write policy. The warning's promised peer fallback overstates what a late error proves: a visible valid sidecar is still eligible. No fresh reproduction of historical sidecar disappearance is established. |
| `config.py:save_config` → `cli.py:_register_and_save`; `config.py:patch_config_on_disk` → CLI patch consumers | Save errors propagate. Init's `except Exception` deletes the device key and re-raises without config readback. Patches first re-read TOML: `_set_grok_host_usage`, `disable_source`, `enable_source`, `reconfigure_sources`, `_migrate_config_core` propagate write errors and stop before success/acknowledgment; migration releases its lock in finally. `install_skills_cmd` catches `ConfigError` / `OSError` / `StorageError` and exits before changing links; its "could not write" message, like the sidecar warning, can follow a visible write. `_init_crypto_session` backfill catches only `OSError` / `ConfigError`; `StorageError` reaches its command's crypto-error handler. Next `load_config` / `_get_config` and init's `_load_prior_device_metadata` read actual config; each later patch also re-reads. | **Reproduced defects** in init cleanup and in the backfill handler (both below). **No demonstrated defect** in the source-toggle/migration/install stop-and-fresh-read policies. No config-family blanket safety claim. |
| `attemptlog.py:write` → `cli.py:push` guarded finally | A separate `except Exception` notices that the attempt could not be durably recorded; outer finally releases the mm lock. No rollback or altered content-sync exit. Next status/diag uses `attemptlog.project` → `read`, with distinct missing/unreadable/corrupt states. | **No demonstrated defect:** existing `tests/test_integration.py:test_attempt_write_failure_keeps_push_exit_and_releases_lock` covers pre/post-rename and mkdir faults, actual record state and released lock. events-retro.md already admits old/new-record uncertainty. |
| `cli.py:_quarantine_corrupt_manifest` → `recover` | Copy errors propagate before source unlink, so a late copy failure can leave both names. `recover` catches FileNotFoundError / `OSError`, not `StorageError`; the post-unlink directory flush separately catches both. A later recover or push re-fetches the canonical through `_fetch_remote_manifest`; no same-call retry. | **Reproduced defect** in presentation (below): the copy's `StorageError` escapes `recover` uncaught instead of its "quarantine failed" error. Source retention holds; a re-run quarantines again and leaves any earlier copy. |
| `resolveflow.py:_ensure_inversion_marker` → `_migrate_pre_inversion_conflict` | Catches `OSError` / `StorageError` / ValueError and returns None; that migration is skipped. A later call reads the actual marker if present, including one published before a flush error, rather than rewriting a cached timestamp. | **No demonstrated defect:** inspected fail-safe gate and fresh read; conflicts.md supplies filename-era/clock rules. This does not certify physical survival of the marker. |

**Existing separate init reproduction.** At this same HEAD, the planning probe
ran the real `_register_and_save` against temporary config and LocalBackend paths,
with Keychain access forbidden. A config-parent `StorageError` left the new
device id in config.toml but deleted its registry entry; the file-flush control
left config absent and removed registration. Both cases passed. The roadmap card
"Preserve coherent state after config publication failures" (Track 71A in
[ROADMAP.md](../ROADMAP.md), drained from the TODOS entry "Init deletes device
registration after a published config save fails") owns repair under the
init/CLI maintainer. Preserve
pre-publication cleanup and investigate `_ensure_device_registered` self-heal,
full CLI retry and passphrase/guard behavior before choosing a repair. Do not
assume the registration is durable or was reported accurately: its
`put_exclusive` write has no parent flush (see Exclusions), and
`register_device` treats every `StorageError` as an existing entry. Those
outcomes remain unqualified; Track 69B changes no consumer or exception API.

**Review reproductions: handlers that catch `OSError` for a `StorageError`.**
Two handlers expect `OSError` from a helper write that raises `StorageError`.
Both were reproduced on the Track 69B branch, whose runtime matches that HEAD,
with tmp_path state under the repo's conftest isolation, each with a file-flush (pre-publication) and a parent-flush
(post-publication) fault. `_init_crypto_session`'s backfill comment says
`except OSError: pass  # non-fatal`, yet `StorageError` escaped in both phases,
which its callers turn into a command error; after the parent fault the
backfilled keys were already on disk. `mm recover --abandon-manifest --yes`
exited 1 on an uncaught `StorageError` rather than "quarantine failed"; the
source stayed, and after the parent fault a quarantine copy existed too. A
fault-free re-run completed and left that copy. The same roadmap card (Track
71A, drained from the TODOS entry "Two handlers catch OSError for helper writes
that raise StorageError") owns repair.

**Exclusions.** Explicit False calls are `cli.py:_apply_write`, `_apply_merge`,
`_apply_conflict`, both writes in `_apply_incoming_file`,
`resolveflow.py:_resolve_interactive_loop`, `seen_sources.py:write` and
`synclog.py:write_sync_log`. Omitted/default-False calls are the two sentinel
writes in `skill_link.py:_prepare_store_dir` and payload/metadata writes in
`_publish_skill_store`. `cli.py:_upload_changed_blobs` selects `data/`, so
`LocalBackend.put` is non-durable there (and for unknown prefixes).
`LocalBackend.put_exclusive` uses its own create-only link protocol, outside this
helper's replacement contract and weaker than its durable prefixes: one
unchecked `os.write`, plain `os.fsync` (not `F_FULLFSYNC`), no parent flush, and
a stranded temp on a raw write/flush `OSError`. Its consumers are
`devices.py:register_device` and `crypto.py:bootstrap_crypto_init`. Ordinary
`lockedjson.py:locked_json_rmw` / `_write_json` forensic caches (identity,
upgrade, token usage, Codex/Grok host usage), `seen_sources` in-place flock
writes, `fsutil.py:flock_append_jsonl` and the Cursor completion spool's
`fsutil.py:append_rotatable_jsonl` / `rotate_jsonl` (an fsynced append and a
rename-aside rotation; between them they call `_fsync_fd` and `fsync_dir`
directly, never `atomic_write_bytes`) are separate protocols, not omitted
durable atomic-write families. Pull's end-of-batch `fsync_dir` has the separate
apply exception boundary below. This audit does not qualify the crash
durability of these `fsync=False` writes: a directory flush covers entries, not
file contents, so `_apply_write`'s "Deferred durability" comment and
`storage/local.py`'s "self-healing via re-push" rationale for `data/` blobs under
a durable manifest remain unverified premises.

**Remaining helper unknown.** If `os.fdopen` fails after `mkstemp` returns,
descriptor ownership/closure is not proven by temp-unlink assertions. Owner:
fsutil maintainer; a bounded descriptor-count/fd-validity fault probe must
establish a leak before filing a repair. No such defect is claimed here.
Existing `tests/test_fsutil.py:TestAtomicWriteBytes`, `TestFsyncFd`, `TestFsyncDir`
and `tests/test_memory_contract.py:test_C2_real_atomic_helper_failure_reopens_actual_coherent_bytes`
retain phase/mode/platform and four-fault readback/stale-refusal coverage. C2 is
a tests-only model, not production caller or cloud qualification. Review found
one concrete missing assertion: the fallback test now covers all three
documented `F_FULLFSYNC` errnos, so on Darwin (where they are distinct)
narrowing that tuple fails a test.

## Complete snapshots (load-bearing, v0.14.3)

Publishing scans (`_push_core`, including the mm-events rescan) use strict mode: directory enumeration, classification, descriptor identity, and hash errors raise `SnapshotError` instead of omitting the path. Diagnostic walkers stay permissive. A published entry's digest and size describe the accepted file bytes and mtime describes that same observed revision. This is not a filesystem-wide atomic snapshot.

Status can supply `diagnostic_hash` to reuse hashes from its single event-file
scan, avoiding a second read just to build the inspection manifest. Reuse
requires unchanged device/inode/size/mtime/ctime; changed files are rehashed.
`_record_file`'s strict branch never consults this callback. Publication always
performs its own descriptor-bound scan and upload verification.

`generate_tombstones` stays a pure comparison. Before each call, `_prove_omitted_paths_absent` checks omitted prior paths against the trusted local source map. ENOENT/ENOTDIR below an accessible root is genuine absence. A still-present regular file omitted by size cap or inode dedup refuses. A missing previously populated selected user-source root refuses the whole push; removing a source from selection drops its file entries, preserves existing tombstones, and is itself a manifest change.

**mm-events ownership (Tracks 59A/60A).** The default `~/.local/share/mind-meld` root is mm-owned: a missing root, a present root with `events/` gone, or individual missing event files represent deletions. A real push creates the default root at 0700 and publishes that snapshot, including its usual new activity row. A missing **custom** mm-events root is never created: push warns with the folder and remedies, drops that source's prior entries without new tombstones, preserves existing tombstones, skips the events tail, and publishes the other sources. Autopush records the same warning as a degraded breadcrumb. When the custom folder returns, the next push publishes it again. User-source missing-root refusal is unchanged.

`_upload_changed_blobs` verifies the upload revision against the scanned digest, size, and mtime **before** `backend.put`. Missing or changed input aborts; it does not `continue`. Earlier correctly keyed encrypted blobs may remain as orphans. The encrypted manifest is the commit boundary; last_seen, sidecar, and conflict cleanup run only after that put.

Attended host capture's publication evidence runs immediately after that
manifest put. `PushResult.content_accepted` is recorded at the put, so a later
maintenance error cannot undo content acceptance; `_report_usage_publication`
then reports the row published only when the exact day-file revision containing
it is in the accepted manifest, not merely because the push returned a truthy
`PushResult`. Status/diag use the existing local accepted-manifest sidecar as
evidence; if the current day-file revision no longer matches it, publication
is unknown. A known failed read of the winning revision is `unverified`.
65A's attended receipt hashes the same bytes it parses, once: post-acceptance
mutation is unverified, never proof the row was not published. Configuration
exclusion, absence of the day file from the accepted manifest, or matching accepted
bytes without the row prove non-publication. The private local
`last-attended-capture.json` records that attempt independently; it never goes to
storage, and cannot prove delivery to another Mac. See events-retro.md for its
atomic write, closed vocabulary and write-free inspection contract. Host rows add
only reader-name `empty_sources` evidence, scoped to retained days.

`_download_and_apply` hashes plaintext after the non-mutating symlink/containment guards and before `_apply_incoming_file`. A mismatch is a per-file `failed` outcome; later valid files continue. Unchanged historical blobs are not rewritten by a no-op push; incoming verification rejects them if encountered.

---

## exclude_patterns + consumer-boundary filter (load-bearing, v0.9.1, v0.9.3, v0.11.13)
Per-source `exclude_patterns: list[str]` of fnmatch globs is matched against the relative path. Default `gstack` source ships with `["config.yaml", "projects/*/repo-mode.json", "projects/*/land-deploy-confirmed", "analytics/.last-sync-*"]` (per-machine artifacts that churn-conflict on every pull — `config.yaml` holds gstack's version-check tracking added in v0.9.3; `analytics/.last-sync-*` are per-machine cursor files that track each device's progress through gstack's local analytics jsonls, added in v0.11.13). The walker drops excluded paths from the local manifest at push time.

`_filter_excluded_paths(manifest, exclude_map)` applies at THREE consumer-boundary call sites — all AFTER `_fetch_remote_manifest` returns: (1) `_pull_core` filters peer manifests in `manifest_cache` BEFORE `collect_tombstones` and the per-source download loop; (2) `_push_core` filters the manifest returned by `_recover_prior_manifest` (covers ok / sidecar / peer-fallback uniformly) BEFORE `generate_tombstones`; (3) `diff_cmd` filters its default or `--from` comparison manifest with both exclude globs and marker skip prefixes. The filter MUST NOT apply at `_fetch_remote_manifest` itself — `mm gc` reads raw manifests via that path to compute referenced blobs, and a filtered manifest there would mark live peer blobs as orphans (codex-2 #1, pinned by `test_mm_gc_does_not_orphan_excluded_path_blobs`).

**Tombstone-suppression invariant.** Adding a path to `exclude_patterns` must NOT generate a deletion tombstone on the next push (2026-04-24 first-pull regression). Removing a glob brings the path back as new. Sidecar recovery is filtered too so a corrupt-manifest recovery on a freshly-migrated config doesn't re-introduce pre-exclude paths via the sidecar (codex-2 #2). All four scenarios (two-device first-pull, tombstone-on-exclude, tombstone-on-unexclude, sidecar-bypass-guard) are pinned in `tests/test_integration.py::TestExcludePatterns5C`.

**Visible-failure contract for migration UX (v0.9.1).** Existing configs need to opt in by running `mm migrate-config`. autopull / autopush NEVER auto-mutate config — they record the missing-excludes signal to `~/.config/mind-meld/migration-state.json` and let `mm status` surface it. Interactive `mm pull` / `mm push` prompt-once. Silent config mutation in a hook would be exactly the class of "wedged sync I never noticed" failure the visible-failure contract exists to prevent. Add the new "config missing recommended excludes" warning to the existing curated stderr signal set (corrupt-manifest recovery, fsync failures, no-sources misconfig, etc.).

## Nested git checkouts are frozen, never published

Every source walker prunes a directory with a regular `.git` file or `.git`
directory strictly below its source root, before enumerating or hashing checkout
contents. The source root itself remains exempt. Check ancestors of configured
includes so a direct include inside a checkout cannot bypass the rule.
`nested_repo_root` probes components in order and stops at the first missing,
non-directory or symlinked one, so a checkout above a descendant link is still
found and nothing is probed through a link; callers check it before the
descendant-symlink omission. Strict publishing refuses an unreadable component
or checkout-marker probe; diagnostic scans remain permissive.

**One observation per command.** `build_manifest_v2(nested_roots=...)` returns the
roots its own walk skipped. Push, status and diff never re-walk for them, and
`_build_exclude_map` takes roots only from its caller. Pull (and `mm diff`'s
remote filter) uses `_incoming_nested_roots` / `nested_repo_roots_for_paths`:
it lstats only the local ancestors of incoming peer files and tombstones, once
per directory, stopping at missing, non-directory or symlinked components. Never
reintroduce a local tree walk into pull/autopull. diag's
`nested_repo_skip_prefixes` is the only inventory walk: write-free, best-effort,
not persisted sync state.

**Freeze, do not exclude, on push.** `_freeze_nested_checkout_entries` copies this
Mac's prior entries under each walker-reported root into the local manifest
unchanged, before the symlink filter and without inspecting what replaced them
on disk. The push prior filter leaves those roots' tombstones in place, and a
path with a prior tombstone is never frozen (merged conflict copies can carry
both; reviving it would advertise a reaped blob). Adding `.git` over published
files therefore uploads nothing, mints no tombstones and keeps peer copies;
deleting the checkout later tombstones them like any deletion. Filtering the
prior instead (exclusion) made deletion stop converging: peers kept republishing
their plain copies and the next pull resurrected them. Upload skips every frozen
path, so their changed local bytes are never read. With `fetch.is_ok` the prior
is this Mac's accepted manifest. A recovered prior freezes only entries whose
blob still exists under this device's key; the rest are excluded (their only
loss is convergence on a later delete). `_drop_unfrozen_checkout_files` then
drops every unfrozen prior file entry under a root (tombstoned or blob-less),
keeping tombstones, so the omission guard never probes checkout contents. The
mm-events rescan carries frozen entries across its source replacement, except
paths it sees again (their `.git` vanished mid-push), which upload normally.
The symlink filter then exempts every root (`exempt_roots`), keeping their
entries and tombstones without probing beneath a frozen checkout. Walkers
report a checkout above an absent or linked include too. Status freezes from
the same exclusion-filtered prior so frozen entries never show as pending
deletions, while an excluded one still does.

`_prove_omitted_paths_absent` has no checkout exemption. Frozen entries are
present in the local manifest; anything else still on disk but omitted (for
example an include deselected while its folder became a checkout, unless another
include still reaches that checkout root) refuses as it always did. An
exemption there minted tombstones for files still on disk.

Frozen entries retire when the checkout is deleted (tombstones everywhere) or an
`exclude_patterns` glob such as `<folder>/*` covers them (dropped without
tombstones; peer copies stay). The freeze also ends silently whenever the prior
no longer lists them: disable then enable of the source, `mm recover
--abandon-manifest`, peer-fallback recovery, blob-less recovery, or adding then
removing a glob. Those paths lose delete convergence like the exclusion they
amount to.

Attended push and preview print one `mm: notice: skipped: nested git repository
source:rel` line per root; autopush stays silent (a skip deletes nothing, so it
is not a data-at-risk warning), and status/diag list roots. Pull logs one
`excluded` record per root and device manifest, not per file, covering skipped
files and skipped tombstones. Pull's probe treats a peer path the filesystem
cannot encode as a stop, never an exception. Conflict-copy discovery
(`resolveflow`) still walks inside checkouts; pull's no-write guarantee covers
applied peer files and deletions. Only sanitized
display copies reach terminals; exclusion keys keep their original bytes.

`.git` is a local selection marker like `.extend-root`: `_filter_excluded_paths`
drops any peer path or tombstone with a `.git` segment in any letter case (APFS
lstat matches `.GIT`), so a peer cannot plant one to switch off sync of a
subtree. It also drops `.` and empty segments, which `Path` would normalize away
after literal prefix matching. This is intentionally stricter than push's
case-sensitive `.git/` exclusion; honest walkers never produce these paths below
a source root.

`_warn_push_growth` uses already-materialized new-file diffs, counts groups by
source and first two directory components, and names up to three groups exceeding
1,000 new files. Its warning never refuses a push, includes no modified files and
is skipped without an accepted manifest of this Mac's own (`fetch.is_ok` false).

## Generated files are not sync data (load-bearing, v0.12.51)

**The rule: if a file has a generator on every machine, it does not belong in `exclude_patterns`' complement.** Derived from a forensic pass over all 88 `conflicted` records in `~/.config/mind-meld/pull-history.jsonl{,.1}` (2026-05-01 → 2026-09-01). **47 of 88 — 53% — were pure generator output** with no author on either machine (derived caches + host-reinstalled payload + per-host renders); counting machine-written session state (`pair-review/session.yaml`, `full-review/session.yaml`) takes it to 56 of 88 (64%). They cannot be fixed by a better merger, because both machines are *correct* and simply regenerated at different times. Adding a glob is the only fix that converges.

Every figure in this section was recomputed by matching globs against the records, not estimated. The first draft claimed "63 of 88 — 72%", which corresponds to no defensible cut of the data, and credited `skills/.system/*` with 17 conflicts by double-counting the `skills/roadmap/SKILL.md` conflict that belongs to the generated-renders group. Caught by `/ship`'s own CHANGELOG-accuracy check on the release that introduced them.

Proof the mechanism works, and the precedent for trusting it: `analytics/.last-sync-line` conflicted 4× on 2026-05-01, was added to the gstack `exclude_patterns` in v0.11.13, and has conflicted **zero** times in the four months since.

The four v0.12.51 additions, each with the observed conflict count:

| glob | source | n | why it can never merge |
|---|---|---|---|
| `projects/*/decisions.active.json` | gstack | 18 | Derived snapshot, ONE minified `JSON.stringify` line |
| `skills/.system/*` | codex | 16 | Codex's own bundled payload, version-stamped in `.codex-system-skills.marker` |
| `projects/*/brain-cache/*` | gstack | 7 | gbrain per-machine cache |
| `_GENERATED_HOST_SKILL_GLOBS` | codex | 2 | gstack-extend renders one shared skill per host, byte-differently. Historically also opencode; that source was retired in Track 44A (numbered 37B before the 2026-09-02 split). |

**`decisions.active.json` — do NOT "fix" this with a JSON merge strategy.** It is a JSON array of records each carrying a unique `id`, so a union-by-`id` merge in `merge.py` looks obviously right and is wrong: `computeActive()` in gstack deliberately **omits superseded decisions**, so a union would resurrect every decision any machine had retired. Exclusion is the only correct resolution. It is also lossless — the source of truth `projects/*/decisions.jsonl` stays in scope and merges cleanly (37 clean merges on record over the same window), and `gstack-decision-search` self-heals via `rebuildSnapshot()` when the snapshot is absent or empty. Pinned by `test_gstack_derived_cache_globs_match_observed_paths`, which asserts `decisions.jsonl` and `decisions.archive.jsonl` are NOT matched by either new glob.

**`_GENERATED_HOST_SKILL_GLOBS` is kept as a shared constant even though only the codex entry consumes it after Track 44A retired the opencode source.** The symmetry with opencode is historical — the same logical skill used to be rendered twice, and excluding it from one host only left the other half of the pair conflicting on every pull. Track 47A (the sync-surface card) inherits this mechanism rather than re-extracting the list. The list is still splatted (`*_GENERATED_HOST_SKILL_GLOBS`) rather than shared by reference because `get_default_source` hands these lists to callers that mutate them — pinned by `test_codex_entry_carries_every_generated_host_skill_glob` and `test_generated_host_skill_lists_are_not_aliased` (`codex["exclude_patterns"] is not _GENERATED_HOST_SKILL_GLOBS`).

**Detection is by name AND by marker (v0.14.0, Track 47A).** gstack-extend drops a `.extend-root` file in each dir it renders. `manifest.marker_skip_globs` returns those directories as **path prefixes, not fnmatch globs** — `_under_skip_prefix` matches `rel_path == prefix` or `rel_path.startswith(prefix + "/")`. A `{rel_dir}/*` glob would be wrong twice over: fnmatch `*` crosses `/`, and a directory literally named `*` carrying a marker would exclude every sibling subtree. `walk_generic_source` and `cli._build_exclude_map` both thread the prefixes alongside `exclude_patterns` so `_filter_excluded_paths` strips them from prior/peer manifests too. A marker skip must not generate deletion tombstones, exactly as adding a glob must not. The name-based `_GENERATED_HOST_SKILL_GLOBS` stay as belt-and-braces for dirs that lost their marker; a new gstack-extend skill no longer needs a new glob to drop out of sync.

The remaining over-exclusion: `roadmap`, `full-review`, `test-plan`, `pair-review`, and `review-apparatus` are **generic names**, and unlike `skills/gstack-*` they carry no distinguishing prefix. A user who hand-authors `~/.codex/skills/roadmap/SKILL.md` on a machine without gstack-extend (and without an `.extend-root` marker) still gets it **silently dropped from sync** by the name glob. Nothing is deleted (exclusion only narrows the manifest), so this is lost propagation rather than lost data, but the user is not told.

**What is deliberately NOT excluded.** `~/.gstack/projects/*/pair-review/` prose artifacts (`deploy.md`, `report.md`, `parked-bugs.md`) stay in scope: pair-review advertises cross-machine resume as a feature. `projects/*/pair-review/session.yaml` IS excluded (v0.14.0) — it is a live per-machine state machine and definitionally cannot be shared. The measurement behind leaving the prose in: the same paths took **31 conflicts against 178 mtime-skips**, so the existing local-is-newer gate already absorbs 85% of these collisions. The fuller fix is device-scoped artifact paths (`pair-review/<device>/`) in gstack, not a blanket mm exclusion.

## disabled_sources + consumer-boundary filter (load-bearing, v0.10.0)
Per-machine source toggle. `[sync].disabled_sources: list[str]` lists source
names to skip on this device only (config.toml is per-machine, never synced).
`get_sources()` filters by name after resolution and before the path-existence
filter. The default `grok` source (`type: "grok"`) is Claude-shaped: the walker
hardcodes `skills/`, `commands/`, and `rules/` at `~/.grok`. The fallback and
auto-detect paths only activate it when one of those dirs exists — `~/.grok`
itself is present on every Grok install and is not consent. CLI surface: `mm enable-source <name>` / `mm disable-source <name>` /
`mm reconfigure-sources` (top-level kebab-case to match `mm migrate-config`
pattern). Strict by default; `--force` accepts unknown names for forward-compat
(pre-disable codex before it ships).

`_filter_disabled_sources(manifest, disabled)` applies at TWO consumer-boundary
call sites — same shape as `_filter_excluded_paths` (the kb-mbp 2026-04-24 fix
template): (1) `_push_core` filters prior_manifest BEFORE `generate_tombstones`,
covering ok-fetch / sidecar / peer-fallback uniformly; (2) `_pull_core` filters
peer manifests in `manifest_cache` BEFORE `collect_tombstones`. Disable-then-
exclude order in both sites: dropping the whole source first avoids walking
soon-to-be-dropped exclude_patterns. The filter MUST NOT apply at
`_fetch_remote_manifest` itself — `mm gc` reads raw manifests via that path
and a filtered manifest there orphans live peer blobs (codex-2 #1 hazard,
mirrored from exclude_patterns).

**Tombstone-suppression invariant.** Disabling a source on machine A and
pushing must NOT generate deletion tombstones for that source's files.
Spurious tombstones suppress restoration and propagation of a missing path
across upgraded and stale peers until expiry (`manifest.TOMBSTONE_TTL_DAYS
= 30`, re-broadcast newest-wins by `collect_tombstones`). Existing local
bytes are never removed. Re-enabling brings the source's files back as
fresh entries (not tombstones). Sidecar recovery filters too so a corrupt-
manifest recovery on a freshly-disabled config doesn't re-introduce
disabled-source paths via the sidecar. All five scenarios (push, re-enable,
pull, sidecar recovery, gc) plus the Track 44A retirement transition pinned
in `tests/test_integration.py::TestDisabledSourcesTombstoneSuppression`.

`seen_sources.py` (new module, mirrors pullhistory.py shape) tracks per-machine
acknowledgment of source names at `~/.config/mind-meld/seen-sources.json`
(0600). `read(initial)` lazy-initializes under `fcntl.flock` on first call,
seeded with the names of currently-resolved sources. **Migration invariant**:
without the lazy-init seed, every existing user's first post-v0.10.0 `mm
status` would surface spurious "New source: claude!" / "New source: gstack!"
hints for sources they're already syncing. Pinned by
`test_seen_sources_initialized_to_existing_on_upgrade`.

`mm status` surfaces two breadcrumbs: "Disabled sources (this device): X, Y"
when the disabled list is non-empty, and "New source available: X" (one-shot
via `seen_sources.compute_new_sources`) when DEFAULT_SOURCES grows on upgrade
and the user hasn't yet enabled or disabled the new name. `mm sources` shows
all configured sources (not just resolved) with an Enabled column; disabled
rows render dimmed.

The `_prompt_source_toggle(source, *, current_state)` helper (extracted from
`_prompt_sources` in v0.10.0) is the single source of truth for the per-source
Y/N prompt copy + default rule. `_prompt_sources` (init) and
`reconfigure_sources` both call it; `mm init`'s default-Y-on-path-exists
behavior is preserved.

**`has_explicit_sources` gates on key presence, not list truthiness (Track 61A
pre-landing review).** `enable_source`'s `currently_active` check and
`reconfigure_sources`'s equivalent both need to tell an explicitly-emptied
`sync.sources: []` apart from an absent key — the two are indistinguishable
under bare `not explicit_sources` (list truthiness), which reads an empty list
as "no explicit sources configured" and falls back to defaulting every prompt
from `default_names`. `enable_source` already computed
`has_explicit_sources = "sources" in sync` for this; `reconfigure_sources`
still used the bare-truthiness form, so a user who explicitly cleared
`sync.sources` had every `DEFAULT_SOURCES` prompt silently default back to Y
on the next `mm reconfigure-sources` run. Fixed by giving `reconfigure_sources`
the same key-presence test. A new `sync.sources` consumer should use it too.

## `walk_generic_source` filesystem-identity dedup (load-bearing, v0.10.1)
Mirror of `_find_conflict_files`'s dedup at the manifest-walk layer. When `include_files` overlaps `include_dirs`, the same on-disk file lands in `collected_paths` twice. Pre-v0.10.1, the second pass got hashed and overwrote the first manifest entry — wasted CPU on identical bytes. On case-insensitive volumes (APFS default) with case-mismatched config, two distinct rel-keys could be created for one inode — a real correctness bug producing phantom add/delete fleet churn.

Dedup uses `set[tuple[int, int]]` keyed on `(st_dev, st_ino)`. Sort `collected_paths` by relative-to-base path BEFORE the dedup pass so the rel-key kept on hardlink/symlink overlap is deterministic across runs and across machines (include_files entries follow include_dirs results in config order, and enumeration order varies across machines). Without the sort, two peers walking the same tree could pick different rel keys for the same inode and generate phantom add/delete churn in the manifest diff. Sites: `manifest.py:walk_generic_source` (the pre-hash loop). Stat failures silently skip (consistent with `_record_file`'s race tolerance).

## Pull-time case-collision detection (load-bearing, v0.10.1)
A Linux peer can legitimately have BOTH `Projects/x.md` AND `projects/x.md` (case-sensitive ext4). A macOS APFS puller can only represent one — the second WRITE would silently alias / overwrite the first via inode collision. Pre-v0.10.1, this was a silent data-loss hazard.

`_detect_case_insensitive_fs(path)` is a non-invasive probe (no writes): when the root is missing, find its nearest existing ancestor, then compare that basename's swapcase variant with `samefile()`. An absent alternate spelling proves case sensitivity; an inconclusive probe (I/O failure or a case-neutral basename) conservatively treats the volume as case-insensitive. This keeps incoming paths that differ only in case from overwriting each other while restoring into a missing root.

`_detect_pull_case_collisions(manifest_cache, local_sources_map)` aggregates across ALL peer manifests so a collision between peer A's `"Projects/x.md"` and peer B's `"projects/x.md"` is detected even when neither peer alone exposes both casings. Returns clusters keyed by source name AND casefold key. It probes `base_path.resolve()`, not the source's on-record path: `local_sources_map` deliberately keeps a symlinked mm-events custom root unresolved (so ownership/bootstrap checks stay symlink-aware, see `_normalized_mm_events_path`), but that same unresolved path is the wrong input here — a source root symlink can cross a volume boundary with different case-sensitivity than the link's own parent, and this probe must judge the filesystem the bytes actually land on.

`_drop_case_collisions_from_manifests(manifest_cache, collisions)` returns a NEW cache (input not mutated) with all-but-lex-first paths dropped per cluster. Tombstones are NOT touched — collision is about per-pull WRITES on a case-insensitive consumer; tombstones encode prior consensus and stay intact (mirrors the asymmetric `_filter_disabled_sources` invariant). Manifest keys are NOT case-normalized GLOBALLY — only consumer-side WRITE skipping. Cross-platform peers retain their distinct casing in the synced manifest. The raw manifest stays intact for `mm gc` (which reads via `_fetch_remote_manifest`, unfiltered).

Hook site: `_pull_core` BEFORE `collect_tombstones` and the per-source download loop, AFTER the disabled-sources / exclude-patterns filter chain. Per-cluster `mm: warning:` to stderr names the kept and dropped paths so the user sees what was skipped (visible-failure contract).

## Pull/push history log (v0.9.1)
`pullhistory.append(verb, device, source, rel_path, action, ...)` writes one JSONL line to `~/.config/mind-meld/pull-history.jsonl` (mode 0600, fcntl.flock-guarded, 1MB cap with line-boundary rotation to `.1`). Wired into `_pull_core` (per-outcome from `_pull_one_source` + `excluded` from the consumer-boundary filter) and `_upload_changed_blobs` (`uploaded`). Failures are swallowed — history is forensic-only, never block sync. `mm log` queries with `--source / --since / --action / --verb / --limit / --format` filters. Reader tolerates a torn first line in `.1` (crash-mid-rotate fingerprint).

## Corrupt-manifest recovery (load-bearing)
`_fetch_remote_manifest` returns a tri-state `ManifestFetch(status: "ok"|"missing"|"corrupt", manifest)`. On `corrupt`, `push` runs a recovery chain before writing a new manifest: (1) local sidecar at `~/.config/mind-meld/last-push.json` (preserves this device's fresh deletions), (2) peer-manifest tombstone aggregation (propagated deletions only), (3) refuse with actionable error. Never treat corrupt as empty — that silently un-deletes files fleet-wide. `mm gc` refuses when any peer manifest is corrupt (referenced blobs may still be live). See SPEC.md "Manifest corruption recovery" and "Merge invariants" for the full invariant.

## Manifest read-path invariant (load-bearing)
Every manifest loaded from bytes/disk MUST go through `manifest.load_manifest(bytes) -> dict`, which composes `deserialize_manifest + normalize_manifest` plus full inner-shape validation. The function guarantees the returned dict has dict-typed `sources` and `tombstones`, each source has a dict `files`, each file value is a dict with a string `sha256`, and each tombstone value is a dict. Non-string file mtimes normalize to `None` only after the file shape is validated. Malformed manifests raise `ManifestError` at the load boundary instead of crashing downstream consumers (`_merge_manifests`, `collect_tombstones`, `generate_tombstones`, the diff loop) with `AttributeError`. `_fetch_remote_manifest` already catches `ManifestError` and falls through to the recovery chain, so a malformed peer manifest degrades to a clean "corrupt" status. Do NOT add a new manifest-load path that bypasses `load_manifest` (sidecar.read uses `deserialize_manifest + structural-check + normalize_manifest` deliberately, to preserve the anti-tampering guard on raw input).

## Pull-time mtime preservation (load-bearing, v0.12.3)
`_apply_write` and `_apply_conflict` (cli.py) MUST call `_restore_mtime_best_effort(path, remote_info.get("mtime"))` after a successful `atomic_write_bytes`. Without it, every pulled file lands with `st_mtime = now-of-pull`, and any downstream consumer that orders by mtime (e.g. gstack skill preambles' `ls -t` recency scan over `~/.gstack/projects/*/checkpoints/`) sees freshly-pulled-old files as "newer" than locally-authored newer ones. The keep-remote interactive branch in `_apply_incoming_file` does the same restore inline.

`_apply_merge` and the merge-via-LCS interactive branch deliberately do NOT restore — line-union merges produce locally-authored content, and backdating to the remote mtime would cause peers' next pull to see `local_mtime <= their remote_mtime` and skip the merged result, losing the union content fleet-wide.

**Future-clamp invariant.** `conflictmtime.py:_restore_mtime_best_effort` caps the applied mtime at `now + _MTIME_RESTORE_MAX_SKEW_SECONDS` (60s). Without the clamp, a peer with a bad clock OR a passphrase-holding attacker minting a manifest dated in 2099 would poison the victim's local mtime into a permanent `local_mtime > remote_mtime` skip at the `cli.py:_apply_incoming_file` mtime gate — silently locking the victim out of all future legitimate updates to that path. The 60s window absorbs normal NTP drift between Macs without admitting year-2099 abuse.

**Defensive parsing.** `_restore_mtime_best_effort`, the apply mtime gate, and `pullplan._predict_pull_outcome` catch `TypeError | ValueError | OverflowError | OSError`, including comparison of a timezone-less remote timestamp with the timezone-aware local mtime. `mtime_from_manifest` rejects timezone-less strings. `load_manifest` rejects non-dict file values and missing/non-string `sha256` with `ManifestError` before coercing any non-string `mtime` to `None`. A malformed entry degrades to the existing corrupt-peer warning; other peers continue. An existing divergent file with `mtime=None` takes the conflict path. Consumer guards also protect direct callers and local-clock overflow. `_upload_changed_blobs` takes freshly scanned local revisions, never loaded peer metadata.

## Apply exception boundary (Track 53A)

`_download_and_apply` contains exactly `(OSError, MindMeldError)` around `_apply_incoming_file`. An unrecorded apply becomes `failed` and later files, sources, and peers continue. `typer.Abort`, `SystemExit`, `KeyboardInterrupt`, programming errors, and every other exception propagate. Never widen this boundary to `except Exception` (Abort is a RuntimeError subclass).

A per-source `_ApplyReporter` owns outcomes, byte accounting, deferred-bump invalidation, and touched parents. Each file starts with `begin`; all five publication sites call `published` immediately after successful `atomic_write_bytes`, before mtime restore, cleanup, or output. Publication and normal return must agree; different words raise `RuntimeError`. Unchanged/skipped/failed and deduplicated conflict returns are recorded at the boundary if nothing was published. No disk reconcile: matching bytes cannot prove this attempt wrote them. Direct helper calls default to diagnostics without a ledger.

Every apply failure uses `_warn_apply_failure`, on stderr in quiet and interactive modes. Interactive progress is constructed with `redirect_stderr=False` so those lines stay on the real stderr while the widget runs. Every dynamic field uses `safety.safe_terminal_str` to enforce one physical line, including device/source/path, operation, preservation text, and exception. Errno comes from the exception or its cause (atomic writes wrap OSError in StorageError). The actual first existing non-directory ancestor is named as a folder/file conflict with the peer. `_print_apply_warning` swallows only output `OSError`. A contained exception after publication preserves the outcome and prints a notice naming the publication and failed follow-up; stale-sidecar cleanup warnings and unreadable-sidecar scan warnings use the same non-raising printer. Directory creation is one component at a time so a later mkdir failure still registers every directory that landed. iCloud/Dropbox manifest conflict-copy cleanup waits until that device had no contained apply failures.

`_pull_core` sets `in_flight` before calling the source and clears it after bookkeeping. Aggregate accounting is separate from `_record_source_bookkeeping`; history and sync-log attempts are tracked so recovery never replays already attempted writes. On `BaseException`, partial outcomes are counted once, `_fsync_touched_parents` runs first (its warnings assigned to the enclosing summary), bookkeeping runs under an `(Exception, SystemExit)` guard, and the original exception is re-raised. A summary or progress teardown failure cannot replace an in-flight exception; a summary failure on normal completion still propagates. Deferred inline bumps drain only in the normal try path.

Directory durability includes each published file's parent and the parent of every newly created directory, including the topmost ancestor. The set is shared with recovery so a later failure cannot discard it. `_fsync_touched_parents` collects both `StorageError` and raw `OSError` (including directory-close failures) as warnings so recovery continues through the remaining parents and bookkeeping. Ctrl-C waits for this bounded fsync work.

**Residual windows.** An interrupt between `atomic_write_bytes` returning and `published()` loses that file's row and its parent fsync registration; its bytes are already on disk. Closing that window requires a callback inside the atomic-write primitive and is outside this contract. A second Ctrl-C during recovery can interrupt bookkeeping and replace the first traceback. No rollback is claimed.

`Pull incomplete:` is reserved for a positive failed-file count; interrupted/aborted summaries say completed changes were kept. Success output never includes "failed". Autopull retains the byte-identical breadcrumb detail `N file(s) failed` and exit 0; its count line explains retry on the next pull. Interactive pull also keeps exit 0 for per-file failures. `mm status` points a degraded failed-file breadcrumb back to `mm pull`.

Tests: `tests/test_pull_helpers.py::TestApplyExceptionBoundary53A`, publication/failure matrix tests in that module, `tests/test_integration.py::TestApplyRecovery53A`, and `tests/test_manifest.py::TestLoadManifestApplyMetadata53A`.


## rel_path traversal defense (load-bearing, v0.11.21, security)
`manifest._validate_rel_path` rejects any `sources[*].files` key (and any tombstone path part) that could escape its source root when concatenated to `base_path`. Rejection criteria: empty string, null bytes, leading `/` or `\` (absolute path — Python's `Path('/base') / '/abs'` returns `Path('/abs')` so the right-hand side wins), Windows drive letters (`C:foo`), or any segment equal to `..` after splitting on either separator. **Do NOT pre-`posixpath.normpath`**: `normpath` collapses `a/..` to `.` and would silently drop the suspicious segment before the check fires.

Threat model: a peer with the storage passphrase can mint an authenticated manifest with arbitrary UTF-8 keys — the GCM authenticator only proves "the bytes came from someone with the passphrase," not "the bytes describe a confined path." Without this guard, `_download_and_apply` (cli.py) would build `local_path = base_path / rel_path` and `mkdir(parents=True, exist_ok=True) + atomic_write_bytes(local_path, ...)` decrypted blob bytes anywhere the user can write — `~/.ssh/authorized_keys`, `~/.zshrc`, `/etc/cron.d/*`, etc. — escalating passphrase + storage-write into RCE on every fleet device that pulls.

Defense-in-depth: `_download_and_apply` ALSO checks `local_path.resolve(strict=False).is_relative_to(base_path.resolve(strict=False))` before calling `_apply_incoming_file`. The dual check exists because `manifest.load_manifest` is the canonical load boundary, but legacy on-disk caches and test fixtures could in principle reach the apply path through a different route; the resolve+is_relative_to assertion catches anything that slips past the load-boundary validator.

## Symlink policy at the manifest and apply boundaries (load-bearing, v0.12.17)

Symlinks **below** a source root are local routing, not synced content. Strict publishing scans omit descendant symlinks for Claude, generic, and Grok; a symlinked source root remains allowed. Prior-state filtering suppresses those omissions for all three types before deletion proof so a converted link is not tombstoned. The source root itself may be a symlink: users can locate an entire source through a link, and resolving that root remains valid. Do not weaken the rel-path traversal defense to admit child links. An unavailable symlink/privacy probe refuses rather than becoming an intentional omission.

The Grok walker is stricter still: it rejects a candidate whose inode has more
than one link. A hard link to `auth.json` or a session transcript inside an
allowlisted `skills/` path has no symlink component, but would otherwise evade
the scoped-source privacy boundary. Do not replace this conservative refusal
with an inode-path search that can race the filesystem walk.

The omission is tombstone-safe only because `_push_core` filters the recovered prior manifest through `_filter_symlinked_paths` before `generate_tombstones`. That filter removes matching file entries and tombstones using the current local source paths. It applies equally to normal fetches, sidecar recovery, and peer-fallback recovery, and protects existing explicit configurations that have not yet migrated to the default `exclude_patterns`. Never move it into `_fetch_remote_manifest`: `mm gc` must keep seeing raw manifests when retaining referenced blobs.

On pull, `_download_and_apply` checks each destination before containment resolution. It returns `skipped` with a breadcrumb when the destination itself is a symlink (including dangling) or any component strictly below the source root is one. This prevents `atomic_write_bytes` from replacing a local link or following it outside the source. `_apply_incoming_file` repeats the leaf check for direct callers. A symlinked source root remains allowed. Skipping is intentional rather than a conflict: a conflict outcome would create a new `.sync-conflict-*` sidecar on every pull beside a link that can never become writable.

Default Codex exclusions additionally cover the known generated skill namespaces (`skills/gstack-*`, `skills/log-work/*`, and `skills/retro-fleet/*`) while leaving hand-authored skill trees syncable. v0.12.51 extended that set: `skills/.system/*` (Codex only — its own bundled payload) and `config._GENERATED_HOST_SKILL_GLOBS` (gstack-extend's per-host renders; historically shared with the opencode source, which Track 44A retired). See "Generated files are not sync data" above for the counts and the reasoning. `manifest.GROK_EXCLUDE_PATTERNS` deliberately did NOT follow; the omission is a measured absence documented at that constant. These globs are migration-safe through the existing exclude consumer-boundary filter; dynamic symlink filtering remains the forward-compatible safety net for new installer layouts and explicit source configurations.

Honest writers (`manifest.walk_*`) build rel keys via `path.relative_to(base)` which by construction NEVER produces `..` segments or absolute paths, so the validator only fires on attacker-crafted manifests; legitimate sync is unaffected. Mirrors `storage/keys.py:_validate_component`'s sibling defense for the sha256 component (sha is hex-bounded, rel_path is free-form, so rel_path is strictly the more reachable surface). Pinned by `tests/test_manifest.py::TestLoadManifestRelPathTraversal` (load-boundary rejection: 11 cases) and `tests/test_pull_helpers.py::TestDownloadAndApplyPathTraversalGuard` (apply-site belt-and-braces: 3 cases). Fuzz strategies in `tests/test_manifest_fuzz.py` were narrowed to the new `valid_rel_path_strategy` for round-trip tests; the wild-input invariant (normalize tolerates garbage) remains covered by `arbitrary_dict_strategy` because `normalize_manifest` itself is unchanged.

## Push dry-run setup contract (Track 56A)

`mm push --dry-run` changes nothing except the local lock file, including the lock parent's creation when needed. `_get_config(read_only=True)` skips the entire transition hook, and push skips its second config load because migration cannot run. Migration emits the existing warning; only when both streams are TTYs does it explain that the preview uses current config and the command without `--dry-run` offers `mm migrate-config` (default yes). No confirm prompt runs. Crypto repair and mm-events bootstrap are planned; fingerprint backfill stays in memory. The update-or-nudge tail (`upgrade.update_or_nudge`, v1.3.0) is gated on `not dry_run` in push's finally block, so a preview never checks, installs or nudges. Previews never scan, warm or capture host usage. `lockfile.py` and `_prove_omitted_paths_absent` retain their existing contracts.

**Missing mm-events root (PC3 reversed by Track 59A).** Missing default event files are deletions, even when the root itself or its `events/` child is gone. Preview completes and reports the deletion a real push would publish, except for the activity row already excluded from preview. A missing default root is omitted from the source list passed to the unchanged deletion proof; it remains an empty source in the local manifest. There is no loss check or automatic restore. Missing custom roots follow the warning-and-skip rule above, in preview and real push. Strict read/access failures still refuse.

Successful previews always print pending setup and “Dry run complete. Nothing was changed except the local lock file.” plus the not-previewed scope (host-usage capture, activity row, GC, upload re-reads). Refusals never print completion and carry the lock-qualified suffix. Exit codes remain 0 completed, 1 stopped, 2 usage error. Tests in `TestPushPreviewNoMutation56A` record filesystem/network/keyring mutation attempts and compare the entire isolated tree with pinned mtimes. Track 62A extends this to the commands and explicit inspection exemptions below.

## Preview and inspection contract (Track 62A)

The [README Previews table](../../README.md#previews) is the operator reference.
`push`, `pull`, `gc`, and `recapture` with `--dry-run` may create/flock only the
local mm lock and its parent. `migrate-config --dry-run` and `diff` do not even
take that lock. No config, keyring, history, upgrade cache, crypto repair,
source bootstrap or network mutation attempt is allowed. Read-only Git walks
remain external to the in-process guarantee. All pending version transitions
survive previews/inspections and are recorded by the next mutating caller.

`_get_config(*, read_only: bool)` and `_maybe_prompt_migration(config, *,
read_only: bool)` require an explicit policy. Interactive previews skip the
post-migration reload and never prompt, even on a TTY; their notice names the
current command without assuming push. Pull also skips its entire conflict
discovery/migration sweep, excluded-path history and the update-or-nudge tail
(no check, install or nudge). Real pull
keeps those writes. Success trailers are owned by command wrappers; interrupted
or refused previews never say they completed. Core setup/crypto/fleet/device,
GC and recapture refusals carry the lock-qualified dry-run suffix; config,
passphrase and held-lock errors retain their existing wording.

The test-only `COMMAND_INTENTS62` table classifies every registered command,
recursing through groups, and requires a lock allowance for any callback with
`dry_run` or a `--dry-run` option. Every preview and inspection runs under the
audit window plus whole-tree comparison. Worker threads started during the
window participate; older workers do not. Bytecode writes are disabled,
subprocess launches recorded, and external launches stubbed at their boundary.
Descriptor exemptions match inodes, never descriptor numbers.

Inspection exemptions are exact owned files: status may seed/recover missing,
empty, malformed or corrupt `seen-sources.json` (and create its parent), while
steady state opens O_RDONLY under LOCK_SH. Recovery reopens O_RDWR|O_CREAT
without truncation, acquires LOCK_EX and re-reads before writing, preserving an
interleaved `acknowledge()`. Author-filtered retro-fleet may write
`identity-cache.json` on fresh, stale and missing-cache runs; no-author-filter
has no exemption. Diag's Grok and identity's Git/GitHub subprocess state is
outside mm's in-process guarantee; the devices child has its own audited entry.

## Pull predictions (Track 62A)

`pullplan.py` imports nothing from cli. `_plan_pull` uses it only for dry-run
or fail-mode preflight; predictions never select real downloads. Diff's
single-file annotation uses the same predictor. Each local file is hashed at
most once per source/path. `_PerSourceResult.predictions` supplies per-source
lines, header suppression, totals and parity evidence; fail-mode uses the same
planned outcomes and prints corrupt-peer and unknown-source warnings before
refusing.

After exclusions/tombstones, decision order is descendant symlink → structural
file/directory collision → absent → unreadable → known digest equality →
mergeable → newer local mtime → conflict. Virtual state tracks files and
directories along ancestors. Write sets the remote digest and future-clamped
mtime; merge sets an UNKNOWN digest; skip/conflict/unchanged leave canonical
state untouched. UNKNOWN never compares equal, so merges are an upper bound.
Fail mode refuses conflicts and possible local failures with exit 3, while
mergeable changes from multiple peers no longer cause false conflicts.

Preview omits blob download/decrypt errors, history, sync logs, manifest
conflict-copy cleanup and old conflict renames. The missing default mm-events
folder note reports its creation without creating it. Pull prints an
incomplete-preview line only for corrupt manifests; unknown sources remain
warnings. No output or exit-code change applies to autopull. Twin-tree parity
tests cover encrypted two-peer input, exclusions/tombstones, symlinks,
unreadability, file/directory collisions in both orders and conflict retries.
