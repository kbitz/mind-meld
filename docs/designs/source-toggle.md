# Per-Machine Source Toggle + Opt-In Source Onboarding

> Generated from CEO plan review on 2026-04-25.
> Targets v0.10.0 (minor; purely additive). Branch: `kbitz/source-toggle`.
> Supplements `docs/designs/sync-gstack-context.md` (multi-source architecture)
> and CLAUDE.md ("Source Layout").

## Problem

Mind Meld today auto-syncs every source in `DEFAULT_SOURCES` whose path exists
on disk. Once you add a source, the only way to "turn it off" on a specific
machine is destructive (`rm -rf ~/.gstack`) or invasive (hand-edit
`config.toml` to omit a source from `[[sync.sources]]`, which forfeits future
default updates).

A user with mind-meld on a work laptop and a personal Mac may want claude on
both but gstack only on the personal Mac. Today's friction blocks that
workflow.

Forward-looking: when codex (and possibly cursor / aider / shell-history)
ships as a `DEFAULT_SOURCES` entry, the absence of an opt-in mechanism means
every existing user gets auto-synced into a new source on upgrade. That's
unacceptable for sources with privacy implications.

## Solution

1. **`[sync] disabled_sources: list[str]`** — per-machine off-switch. Names in
   the list are filtered out at `get_sources()`. config.toml is per-machine
   (lives at `~/.config/mind-meld/`, never synced), so this is naturally a
   per-device preference.

2. **`mm enable-source <name>` / `mm disable-source <name>`** — flat
   top-level CLI commands (matches existing `mm migrate-config` /
   `mm autopull` kebab-case pattern). Patches via existing
   `patch_config_on_disk()` helper (flat-key shape fits perfectly). Strict
   by default: unknown source name errors with a closest-match hint.
   `--force` flag accepts unknown names for forward-compat use cases
   ("I never want codex, even before it ships").

3. **`mm init` picker — already exists** (`_prompt_sources()` at
   `cli.py:1433` iterates `DEFAULT_SOURCES` with `typer.confirm` per
   source). Default-Y on path-exists, default-N otherwise — kept as-is
   per eng-review D1. We extract a small `_prompt_source_toggle(default,
   *, current_state)` helper for reuse in `mm reconfigure-sources`. Ctrl-C
   atomicity already exists (typer's existing exit-handling).

4. **`mm reconfigure-sources`** — new top-level command for upgraders.
   Mutates an existing config (preserves user-customized `include_dirs` /
   `include_files` / `exclude_patterns` overrides). Iterates DEFAULT_SOURCES
   ∪ user's current sources, prompts per-source with current state as
   default. Updates `disabled_sources` and appends new entries to
   `[[sync.sources]]` for opted-in new sources.

5. **New-source discovery for upgraders** — `seen_sources.json` tracks names
   the user has been shown. New `DEFAULT_SOURCES` entries (e.g. codex)
   surface in `mm status` as `New source available: codex. Run mm
   enable-source codex to sync.` One-shot — once acknowledged via enable
   /disable, the hint stops. `mm reconfigure-sources` re-runs the picker
   over the current config + new defaults.

6. **`seen_sources.py` module** — new file at `src/mind_meld/seen_sources.py`
   (matches `pullhistory.py` / `sidecar.py` / `devices.py` shape — one
   persisted-state concern per module). `read(initial: list[str]) -> set[str]`
   handles lazy init-if-missing under `fcntl.flock`. `write(seen)` uses
   `fsutil.atomic_write_bytes` (existing helper).

7. **Migration invariant** — on first post-v0.10.0 call to
   `seen_sources.read()`, initialize `seen-sources.json` with the names of
   all currently-resolved sources. Without this, every upgrader sees
   spurious `New source: claude!` / `New source: gstack!` hints. Pinned by
   `test_seen_sources_initialized_to_existing_on_upgrade`.

8. **Tombstone-suppression invariant (P0, eng review).** `disabled_sources`
   must apply at TWO consumer boundaries (mirroring the `exclude_patterns`
   pattern from CLAUDE.md kb-mbp 2026-04-24 fix):

   - `_push_core`: drop disabled-source entries from prior_manifest BEFORE
     `generate_tombstones`. Without this, disabling on one machine and
     pushing nukes that source's content fleet-wide.
   - `_pull_core`: drop disabled-source entries from peer manifests BEFORE
     `collect_tombstones`. Without this, peer's disabled-source content
     tries to land on this device.
   - `_fetch_remote_manifest`: filter MUST NOT apply here. `mm gc` reads
     raw peer manifests via this path to compute referenced blobs;
     filtering would orphan live peer blobs.

   Pinned by 5 P0 tests (see test plan).

## Scope Decisions

| # | Proposal | Decision | Reasoning |
|---|----------|----------|-----------|
| Approach | `disabled_sources: list[str]` at `[sync]` (vs per-source `enabled` flag) | ACCEPTED | Composes with implicit DEFAULT_SOURCES; defaults keep evolving; flat key fits patch_config_on_disk |
| `mm sources` Enabled column | Includes all configured sources, not just resolved | ACCEPTED | Self-documenting; one-command answer to "what's syncing on this Mac" |
| `mm status` disabled breadcrumb | Shows `Disabled sources (this device): X, Y` when non-empty | ACCEPTED | Catches "why isn't this syncing" debug loop in one command |
| `mm init` interactive picker | Fresh init asks per-source; `--yes` for non-TTY | ACCEPTED | User-requested opt-in semantics |
| Strict unknown-name | `mm sources disable <unknown>` errors; `--force` accepts | ACCEPTED | Catches typos; --force covers pre-shipping forward-compat |
| New-source discovery | `mm status` hint + `mm sources reconfigure` (both) | ACCEPTED | Mirrors mm migrate-config pattern (status hint + dedicated command) |
| Migration init | `seen_sources.json` initialized to currently-resolved on first post-upgrade run | ACCEPTED-BASELINE | Prevents spurious hints for already-shipped sources |
| Case-insensitive matching | Skipped | SKIPPED | Strict casing matches rest of mm CLI; closest-match hint covers the typo case |

## NOT in scope

- **Per-source push-only / pull-only granularity.** `disabled_sources` blocks both
  directions. Asymmetric per-direction toggles are YAGNI; revisit on real demand.
- **Synced disable list across devices.** Per-machine by design — that's the point.
- **codex source itself.** This PR ships the *mechanism*; codex `DEFAULT_SOURCES`
  entry is a separate ship.
- **Per-project filtering inside a source.** Existing "Selective sync" Future item;
  different feature.

## Architecture

```
config.toml (per-machine, never synced — ~/.config/mind-meld/)
  [sync]
  disabled_sources = ["gstack"]       ◀── new; per-device opt-out

  [[sync.sources]]   OR    DEFAULT_SOURCES (implicit)
       │
       ▼
  get_sources(config)
       │
       ├─ resolve explicit / legacy / DEFAULT_SOURCES + auto-detect (existing)
       ├─ NEW: drop where name in disabled_sources
       └─ filter: path exists on disk (existing)
       │
       ▼
  consumed at 9 call sites (push, pull, sources, status, ...)


CONSUMER-BOUNDARY FILTER (P0 tombstone-suppression invariant)

  _push_core                              _pull_core
  │                                       │
  ▼                                       ▼
  prior_manifest = _recover_prior_manifest()  peer_manifests = _fetch_remote_manifest()
                                              # (filter NOT here — mm gc reads raw)
       │                                       │
       ├─ NEW: drop disabled-source entries     ├─ NEW: drop disabled-source entries
       │       from prior_manifest               │       from each peer manifest
       │                                          │
       ▼                                          ▼
  generate_tombstones(prior, new)            collect_tombstones(peer manifests)
  # → no tombstones for disabled sources    # → no peer disabled-source apply

  Without this filter: disable on machine A → push → tombstones for every gstack
  file → peers pull → fleet-wide gstack data loss. (Mirrors exclude_patterns
  kb-mbp 2026-04-24 invariant; same shape, same fix.)


mm init (existing)                    mm enable-source / mm disable-source <name>
  │                                    │
  ▼                                    ▼
  _prompt_sources()                    patch_config_on_disk(
   (already iterates DEFAULT_SOURCES,     updates={"sync": {"disabled_sources": [...]}}
    typer.confirm per source,           ) — flat field, fits existing helper
    default-Y on path-exists)             — name validation: in defaults ∪ explicit
   build config                          — --force escape for forward-compat
   register device                       — closest-match hint on unknown name


mm reconfigure-sources                seen_sources.py (new module)
  │                                    │
  ▼                                    ▼
  read existing config + DEFAULT_SOURCES   read(initial: list[str]) -> set[str]
  for each in (DEFAULTS ∪ user.sources):    │
    _prompt_source_toggle(                  ├─ flock guard
       default, current_state=enabled)      ├─ if file missing → atomic write `initial`
  apply diffs:                              │     (the migration invariant)
    - update disabled_sources               ├─ if corrupt JSON → warn + reset to initial
    - append new entries to                 └─ return parsed set
      [[sync.sources]] (preserves
       user-customized include_dirs/etc)


mm status
  │
  ▼
  seen = seen_sources.read(initial=[s.name for s in get_sources(config)])
  config_disabled = config.sync.disabled_sources
  diff = DEFAULT_SOURCES.names \ seen \ config_disabled \ user_explicit_sources
       │
       ├─ if diff non-empty: show "New source available: X. Run mm enable-source X."
       └─ if config_disabled non-empty: show "Disabled sources (this device): X, Y"
```

## Error & Rescue Registry

| CODEPATH | FAILURE | EXCEPTION | RESCUED? | USER SEES |
|----------|---------|-----------|----------|-----------|
| `config.load_config` validate | `disabled_sources` not list[str] | `ConfigError` | Y | "config: sync.disabled_sources must be a list of strings" |
| `mm sources disable <unknown>` | name in neither defaults nor explicit | `ConfigError` | Y | "unknown source 'X' — valid: claude, gstack" + closest-match hint |
| `mm sources disable <known>` | already disabled | (no-op) | Y | "source 'X' already disabled" |
| `mm sources enable <known>` | not in disabled_sources | (no-op) | Y | "source 'X' is already enabled" |
| `mm sources enable <unknown>` | name unknown | `ConfigError` | Y | early error |
| `get_sources()` | every source disabled | (none) | Y | reuses existing autopush "no sources" stderr breadcrumb |
| `mm init` interactive | Ctrl-C mid-picker | `KeyboardInterrupt` | Y | atomic — no half-config written |
| `mm init` interactive | zero detected | (prompt) | Y | confirm before init |
| `mm init` interactive | all unselected | (prompt) | Y | typed confirm |
| `mm init` re-run | config exists | `ConfigError` | Y | existing path preserved |
| `seen_sources.json` | corrupt JSON | (caught) | Y | warn + reset to current sources |
| `seen_sources.json` | first run, file missing | (caught) | Y | silent init to current sources |
| `seen_sources` first-init race | concurrent callers double-init | (flock) | Y | (consistent — both write same content) |
| `_push_core` boundary (P0) | tombstones for disabled-source files | (filter) | Y | (silent — files preserved fleet-wide) |
| `_pull_core` boundary (P0) | peer disabled-source applies locally | (filter) | Y | (silent skip) |
| `_fetch_remote_manifest` (P0) | filter applied here would orphan blobs | (NOT applied) | Y | (no orphan — gc stays correct) |
| `mm gc` | disabled blobs orphaned despite peer refs | (peer manifest read bypasses filter) | Y | (no orphan) |

Zero CRITICAL GAPS.

## Test Plan

19 test cases:

1. `test_disabled_sources_filters_get_sources`
2. `test_disabled_sources_field_validates_list_of_str`
3. `test_disabled_sources_unknown_name_strict_errors`
4. `test_disabled_sources_unknown_name_force_accepts`
5. `test_disabled_sources_idempotent_redisable`
6. `test_disabled_sources_enable_unwinds`
7. `test_mm_init_interactive_picker_writes_correct_config`
8. `test_mm_init_interactive_picker_ctrlc_no_partial_write`
9. `test_mm_init_yes_non_tty_default`
10. `test_mm_init_zero_sources_detected_user_confirms`
11. `test_mm_status_disabled_breadcrumb_when_nonempty`
12. `test_mm_status_no_breadcrumb_when_empty`
13. `test_mm_sources_table_shows_enabled_column`
14. `test_mm_sources_reconfigure_command`
15. `test_seen_sources_initialized_to_existing_on_upgrade` ← MIGRATION INVARIANT
16. `test_seen_sources_new_source_surfaces_hint_once`
17. `test_seen_sources_post_acknowledgment_silent`
18. `test_autopull_silent_when_one_source_disabled`
19. `test_autopush_no_sources_breadcrumb_still_fires_when_all_disabled`

Plus a closest-match-hint micro-test for the strict unknown-source error.

## Deployment

- **Version:** v0.9.4 → **v0.10.0** (minor; purely additive)
- **NOT BREAKING.** All existing configs continue to work unchanged.
- **Migration invariant:** on first post-upgrade load, `seen_sources.json` is
  initialized with currently-resolved source names. Without this, every upgrader
  sees spurious "New source: claude!" / "New source: gstack!" hints on next
  `mm status`. Pinned by `test_seen_sources_initialized_to_existing_on_upgrade`.
- **Rollback:** delete the field + file, downgrade pip. Reversibility 5/5.
- CHANGELOG entry, README "Configuration" section update covering the new
  field + commands, CLAUDE.md "Source Layout" mention of `seen_sources`.

## Long-term

- Schema scales linearly with N sources.
- **Codex onboarding pattern** (separate PR/release): append to
  `DEFAULT_SOURCES`, cut release. Existing users see `mm status` hint and run
  `mm sources enable codex` or `mm sources reconfigure`. Privacy-sensitive
  sources never auto-sync on upgrade.
- The `seen_sources` mechanism is the load-bearing piece for clean future-source
  onboarding.

## Eng Review Decisions (locked 2026-04-25)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| `mm init` picker default | Keep current default-Y-on-path-exists | Already opt-in via per-source confirm; tightening adds friction without privacy gain (privacy concern is handled by seen_sources for new sources) |
| CLI command organization | Flat top-level (`mm enable-source`, `mm disable-source`, `mm reconfigure-sources`) | Matches existing `mm migrate-config` kebab-case pattern; no new architectural precedent |
| `seen_sources` module placement | New `src/mind_meld/seen_sources.py` | Mirrors pullhistory/sidecar/devices pattern (one persisted-state concern per module) |
| `seen_sources` first-init trigger | Lazy at module level under `fcntl.flock` | Concentrates the invariant in one function; no surprise side effects on `mm push` / `mm pull` |
| Per-source prompt UI DRY | Extract `_prompt_source_toggle(default, *, current_state)` helper | Single source of truth for prompt copy + default-Y/N rule; `_prompt_sources` (init) and reconfigure both call it |
| Tombstone-suppression invariant | Consumer-boundary filter at `_push_core` + `_pull_core`, NOT at `_fetch_remote_manifest` | Mirrors exclude_patterns kb-mbp 2026-04-24 fix; prevents fleet-wide data loss on first post-disable push |

### Test plan delta vs CEO doc

Test count grew 19 → **37** to cover the consumer-boundary invariants:

**P0 tombstone-suppression (5 new):**
- `test_disable_source_does_not_generate_tombstones_on_next_push` (kb-mbp shape)
- `test_enable_previously_disabled_source_brings_files_back_as_new`
- `test_pull_skips_disabled_source_peer_manifest_entries`
- `test_sidecar_recovery_filters_disabled_sources` (sidecar-bypass guard)
- `test_mm_gc_does_not_orphan_disabled_source_blobs`

**Schema validation (3 new):**
- `test_disabled_sources_field_validates_list_of_str`
- `test_disabled_sources_non_list_errors`
- `test_disabled_sources_non_string_entry_errors`

**CLI commands (split + force-flag coverage, +6):**
- `--force` happy paths for both enable / disable
- closest-match hint micro-test
- `mm reconfigure-sources` no-diff no-op
- `mm reconfigure-sources` Ctrl-C atomicity
- `mm reconfigure-sources` preserves user customizations

**seen_sources concurrency (3 new):**
- `test_seen_sources_lazy_init_on_missing_file_under_flock`
- `test_seen_sources_corrupt_json_warn_and_reset`
- `test_seen_sources_concurrent_callers_serialized`

**Picker helper (1 new):**
- `test_prompt_source_toggle_default_reflects_current_state`

Full list in `.context/2026-04-25-source-toggle-test-plan.md`.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | clean | 4 proposals, 4 accepted, 0 deferred |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | clean | 5 issues found, 0 critical gaps |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **UNRESOLVED:** 0
- **VERDICT:** CEO + ENG CLEARED — ready to implement.
