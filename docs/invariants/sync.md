# Sync mechanics — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/cli.py` — `_pull_core` / `_push_core` / `_fetch_remote_manifest` / `_recover_prior_manifest` / `_filter_excluded_paths` / `_filter_disabled_sources` / `_drop_case_collisions_from_manifests`
- `src/mind_meld/manifest.py` — `walk_generic_source` / `load_manifest` / `collect_tombstones` / `generate_tombstones`
- `src/mind_meld/config.py` — `exclude_patterns` / `disabled_sources` / `seen_sources` consumer paths
- `src/mind_meld/seen_sources.py`
- `src/mind_meld/sidecar.py`
- `src/mind_meld/pullhistory.py`

Tests pinning the invariants below: `tests/test_integration.py::TestExcludePatterns5C`, `tests/test_integration.py::TestDisabledSourcesTombstoneSuppression`, `tests/test_case_collision.py`, `tests/test_recover.py`, `tests/test_recovery.py`, `tests/test_pullhistory.py`, `tests/test_seen_sources.py`, `tests/test_manifest_fuzz.py`.

---

## exclude_patterns + consumer-boundary filter (load-bearing, v0.9.1, v0.9.3, v0.11.13)
Per-source `exclude_patterns: list[str]` of fnmatch globs is matched against the relative path. Default `gstack` source ships with `["config.yaml", "projects/*/repo-mode.json", "projects/*/land-deploy-confirmed", "analytics/.last-sync-*"]` (per-machine artifacts that churn-conflict on every pull — `config.yaml` holds gstack's version-check tracking added in v0.9.3; `analytics/.last-sync-*` are per-machine cursor files that track each device's progress through gstack's local analytics jsonls, added in v0.11.13). The walker drops excluded paths from the local manifest at push time.

`_filter_excluded_paths(manifest, exclude_map)` applies at TWO consumer-boundary call sites — both AFTER `_fetch_remote_manifest` returns: (1) `_pull_core` filters peer manifests in `manifest_cache` BEFORE `collect_tombstones` and the per-source download loop; (2) `_push_core` filters the manifest returned by `_recover_prior_manifest` (covers ok / sidecar / peer-fallback uniformly) BEFORE `generate_tombstones`. The filter MUST NOT apply at `_fetch_remote_manifest` itself — `mm gc` reads raw manifests via that path to compute referenced blobs, and a filtered manifest there would mark live peer blobs as orphans (codex-2 #1, pinned by `test_mm_gc_does_not_orphan_excluded_path_blobs`).

**Tombstone-suppression invariant.** Adding a path to `exclude_patterns` must NOT generate a deletion tombstone on the next push (2026-04-24 first-pull regression). Removing a glob brings the path back as new. Sidecar recovery is filtered too so a corrupt-manifest recovery on a freshly-migrated config doesn't re-introduce pre-exclude paths via the sidecar (codex-2 #2). All four scenarios (two-device first-pull, tombstone-on-exclude, tombstone-on-unexclude, sidecar-bypass-guard) are pinned in `tests/test_integration.py::TestExcludePatterns5C`.

**Visible-failure contract for migration UX (v0.9.1).** Existing configs need to opt in by running `mm migrate-config`. autopull / autopush NEVER auto-mutate config — they record the missing-excludes signal to `~/.config/mind-meld/migration-state.json` and let `mm status` surface it. Interactive `mm pull` / `mm push` prompt-once. Silent config mutation in a hook would be exactly the class of "wedged sync I never noticed" failure the visible-failure contract exists to prevent. Add the new "config missing recommended excludes" warning to the existing curated stderr signal set (corrupt-manifest recovery, fsync failures, no-sources misconfig, etc.).

## disabled_sources + consumer-boundary filter (load-bearing, v0.10.0)
Per-machine source toggle. `[sync].disabled_sources: list[str]` lists source
names to skip on this device only (config.toml is per-machine, never synced).
`get_sources()` filters by name after resolution and before the path-existence
filter. CLI surface: `mm enable-source <name>` / `mm disable-source <name>` /
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
pushing must NOT generate deletion tombstones for that source's files (would
propagate fleet-wide deletion). Re-enabling brings the source's files back as
fresh entries (not tombstones). Sidecar recovery filters too so a corrupt-
manifest recovery on a freshly-disabled config doesn't re-introduce
disabled-source paths via the sidecar. All five scenarios (push, re-enable,
pull, sidecar recovery, gc) pinned in
`tests/test_integration.py::TestDisabledSourcesTombstoneSuppression`.

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

## `walk_generic_source` filesystem-identity dedup (load-bearing, v0.10.1)
Mirror of `_find_conflict_files`'s dedup at the manifest-walk layer. When `include_files` overlaps `include_dirs`, the same on-disk file lands in `collected_paths` twice. Pre-v0.10.1, the second pass got hashed and overwrote the first manifest entry — wasted CPU on identical bytes. On case-insensitive volumes (APFS default) with case-mismatched config, two distinct rel-keys could be created for one inode — a real correctness bug producing phantom add/delete fleet churn.

Dedup uses `set[tuple[int, int]]` keyed on `(st_dev, st_ino)`. Sort `collected_paths` by relative-to-base path BEFORE the dedup pass so the rel-key kept on hardlink/symlink overlap is deterministic across runs and across machines (rglob iteration order is FS-dependent on macOS APFS). Without the sort, two peers walking the same tree could pick different rel keys for the same inode and generate phantom add/delete churn in the manifest diff. Sites: `manifest.py:walk_generic_source` (the pre-hash loop). Stat failures silently skip (consistent with `_record_file`'s race tolerance).

## Pull-time case-collision detection (load-bearing, v0.10.1)
A Linux peer can legitimately have BOTH `Projects/x.md` AND `projects/x.md` (case-sensitive ext4). A macOS APFS puller can only represent one — the second WRITE would silently alias / overwrite the first via inode collision. Pre-v0.10.1, this was a silent data-loss hazard.

`_detect_case_insensitive_fs(path)` is a non-invasive probe (no writes): construct a swapcase variant of the path's own basename and check via `samefile()` whether both names resolve to the same inode. Returns False on any failure (safer default — no spurious case-collision warnings on Linux ext4). Skips paths whose basename has no alphabetic characters or whose swapcase produces the same name.

`_detect_pull_case_collisions(manifest_cache, local_sources_map)` aggregates across ALL peer manifests so a collision between peer A's `"Projects/x.md"` and peer B's `"projects/x.md"` is detected even when neither peer alone exposes both casings. Returns clusters keyed by source name AND casefold key.

`_drop_case_collisions_from_manifests(manifest_cache, collisions)` returns a NEW cache (input not mutated) with all-but-lex-first paths dropped per cluster. Tombstones are NOT touched — collision is about per-pull WRITES on a case-insensitive consumer; tombstones encode prior consensus and stay intact (mirrors the asymmetric `_filter_disabled_sources` invariant). Manifest keys are NOT case-normalized GLOBALLY — only consumer-side WRITE skipping. Cross-platform peers retain their distinct casing in the synced manifest. The raw manifest stays intact for `mm gc` (which reads via `_fetch_remote_manifest`, unfiltered).

Hook site: `_pull_core` BEFORE `collect_tombstones` and the per-source download loop, AFTER the disabled-sources / exclude-patterns filter chain. Per-cluster `mm: warning:` to stderr names the kept and dropped paths so the user sees what was skipped (visible-failure contract).

## Pull/push history log (v0.9.1)
`pullhistory.append(verb, device, source, rel_path, action, ...)` writes one JSONL line to `~/.config/mind-meld/pull-history.jsonl` (mode 0600, fcntl.flock-guarded, 1MB cap with line-boundary rotation to `.1`). Wired into `_pull_core` (per-outcome from `_pull_one_source` + `excluded` from the consumer-boundary filter) and `_upload_changed_blobs` (`uploaded`). Failures are swallowed — history is forensic-only, never block sync. `mm log` queries with `--source / --since / --action / --verb / --limit / --format` filters. Reader tolerates a torn first line in `.1` (crash-mid-rotate fingerprint).

## Corrupt-manifest recovery (load-bearing)
`_fetch_remote_manifest` returns a tri-state `ManifestFetch(status: "ok"|"missing"|"corrupt", manifest)`. On `corrupt`, `push` runs a recovery chain before writing a new manifest: (1) local sidecar at `~/.config/mind-meld/last-push.json` (preserves this device's fresh deletions), (2) peer-manifest tombstone aggregation (propagated deletions only), (3) refuse with actionable error. Never treat corrupt as empty — that silently un-deletes files fleet-wide. `mm gc` refuses when any peer manifest is corrupt (referenced blobs may still be live). See SPEC.md "Manifest corruption recovery" and "Merge invariants" for the full invariant.

## Manifest read-path invariant (load-bearing)
Every manifest loaded from bytes/disk MUST go through `manifest.load_manifest(bytes) -> dict`, which composes `deserialize_manifest + normalize_manifest` plus full inner-shape validation. The function guarantees the returned dict has dict-typed `sources` and `tombstones`, each source has a dict `files`, and each tombstone value is a dict. Malformed manifests raise `ManifestError` at the load boundary instead of crashing downstream consumers (`_merge_manifests`, `collect_tombstones`, `generate_tombstones`, the diff loop) with `AttributeError`. `_fetch_remote_manifest` already catches `ManifestError` and falls through to the recovery chain, so a malformed peer manifest degrades to a clean "corrupt" status. Do NOT add a new manifest-load path that bypasses `load_manifest` (sidecar.read uses `deserialize_manifest + structural-check + normalize_manifest` deliberately, to preserve the anti-tampering guard on raw input).
