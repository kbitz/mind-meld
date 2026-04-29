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
src/mind_meld/{cli,manifest,crypto,errors,devices,config,lockfile,synclog,merge,sidecar,pullhistory,upgrade,seen_sources,events,safety,conflictdiff}.py
src/mind_meld/storage/{local,keys}.py
src/mind_meld/skills/retro_fleet/{SKILL.md,aggregator.py,__init__.py}  (Group 8 v0.11.0 — Claude Code skill orchestrator + Python aggregator. Dir on disk is `retro_fleet` (Python identifier, importable as `mind_meld.skills.retro_fleet`); the symlink installer creates `~/.claude/skills/retro-fleet` (hyphen — Claude Code naming convention). SKILL.md invokes the aggregator via `python -m mind_meld.skills.retro_fleet.aggregator`. Ships via `packages = ["src/mind_meld"]` — do NOT add hatchling `force-include` for this subtree, it would double-ship.)

`safety.py` (v0.11.1) holds the peer-controlled string sanitization
helpers — `safe_str`, `safe_text`, `strip_terminal_escapes`. Originally
defined in cli.py through v0.11.x; extracted so `conflictdiff.py` can
import them without a cli.py-conflictdiff.py circular import. cli.py
re-exports these names for backwards compat with any out-of-tree
imports; tests should import from `mind_meld.safety` directly.

`conflictdiff.py` (v0.11.1) holds pure leaf primitives for the conflict
prompts: `render_prompt`, `render_banner`, `count_divergent_lines`. Both
prompt sites (`_resolve_interactive_loop` post-pull walk, and
`_prompt_conflict_choice` inline pull-time) call these helpers; site-
level dispatch (pre-inversion / post-inversion / canonical-missing)
stays at each call site — see "Conflict-direction inversion" section
for why filename-prefix dispatch is load-bearing.

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
mm --version | init | push | pull | status | devices | diff | gc | sources | conflicts | resolve | log | migrate-config | autopull | autopush | enable-source | disable-source | reconfigure-sources

Pull flag: `--conflict-mode {prompt|keep-both|fail}` (default `keep-both`). `prompt` asks per-file; `fail` preflights via `_predict_pull_outcome` and exits 2 (no writes) if any file would conflict — for CI. Replaces the old `--no-prompt` / `--resolve-interactive` pair (v0.6.2 BREAKING).
GC flags: `--conflicts` (also reap `.sync-conflict-*` files older than 30 days).
Log flags: `--source NAME`, `--since DATE`, `--action {written|merged|skipped|conflicted|excluded|uploaded|failed}`, `--verb {pull|push}`, `--limit N`, `--format {jsonl|table}`.
Migrate-config flags: `--yes`, `--dry-run`. Idempotent: appends missing recommended `exclude_patterns` to existing `[[sync.sources]]` entries; preserves user-customized globs.

## exclude_patterns + consumer-boundary filter (load-bearing, v0.9.1, v0.9.3)
Per-source `exclude_patterns: list[str]` of fnmatch globs is matched against the relative path. Default `gstack` source ships with `["config.yaml", "projects/*/repo-mode.json", "projects/*/land-deploy-confirmed"]` (per-machine artifacts that churn-conflict on every pull — `config.yaml` holds gstack's version-check tracking added in v0.9.3). The walker drops excluded paths from the local manifest at push time.

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

## Conflict-direction inversion + fleet-version refusal (load-bearing, v0.9.2 BREAKING)
`_apply_conflict` keeps LOCAL bytes at canonical; REMOTE bytes go to `.sync-conflict-*` sidecar. No rename + rollback dance — local is never overwritten in the conflict path. Pre-v0.9.2 produced the opposite mapping; pre-existing files migrate to a `v0-` prefix on first lock-protected discovery in `mm pull` / `mm resolve` (NEVER from `mm conflicts` — lockless, would race autopull, codex-2 #5).

`_resolve_interactive_loop` is dual-mode dispatched BY FILENAME PREFIX (not timestamp — sound, since post-v0.9.2 code never produces a `v0-` file directly). `v0-` files: `(l)ocal` renames sidecar over canonical, `(r)emote` unlinks sidecar. No-prefix files: `(l)ocal` unlinks sidecar, `(r)emote` renames sidecar over canonical. Diff fromfile/tofile labels flip per row to match.

## Conflict-prompt UX (load-bearing, v0.11.1 BREAKING — interactive prompt)

The two interactive prompt sites (`_resolve_interactive_loop` post-pull walk in cli.py:5688, `_prompt_conflict_choice` inline pull-time in cli.py:1115) share leaf primitives in `src/mind_meld/conflictdiff.py`: `render_prompt`, `render_banner`, `count_divergent_lines`. Site-level dispatch over the four shapes (canonical-exists × pre-inversion / post-inversion × canonical-missing) stays at each call site — burying it in a helper would hide the load-bearing filename-prefix dispatch.

**`(b)oth` → `(s)kip` rename + alias.** Default key changed from `b` to `s` in v0.11.1. Same on-disk effect — both leave the canonical and `.sync-conflict-*` files in place — but the option name now matches the action. The pre-1.0 letters `b` / `both` are aliased to skip with a one-time `mm: notice:` so stale scripts continue to work; alias removes at 1.0. **Exact-match dispatch** (`if choice in ("b", "both")`): `back`/`browse`/`between` must NOT silently trigger the alias.

The pre-v0.9.0 letters `c` / `f` remain LOUD-rejected (real silent-data-loss risk in mapping them through post-inversion). The asymmetry is deliberate: `c`/`f` encoded directional ambiguity that the v0.9.2 inversion broke; `b` does not.

**Honest skip-lifecycle copy.** The `(s)kip` line reads `leave both files on disk; run `mm resolve` later or delete manually` — explicit that the next pull does NOT re-prompt unless remote changes again, so the conflict file persists indefinitely. Codex outside-voice review (T2) caught the misleading prior wording ("decide on the next pull").

**Three-number divergence summary.** `count_divergent_lines` counts `-` / `+` lines in the unified diff (excluding the `---` / `+++` headers) and returns `(M, N, K)` where M = removed-or-replaced, N = added-or-replaced, K = M + N. Wording is honest about replacement semantics: a 1-line replacement is M=1, N=1, K=2 (counting both old and new). Codex T1 caught the original "unique to local/remote" wording as pseudo-precision.

**Banner attribution chain.** `parse_conflict_device_short(name)` (manifest.py) extracts the 8-char device prefix from the conflict filename. `lookup_device_by_short_id(devices, short_id)` (devices.py) is a pure function over the existing devices list returning `(device | None, count)`:
* `(None, 0)` — no peer matches; banner shows `(unknown peer)`.
* `(device, 1)` — exact attribution; banner shows `(from <device_name>)`.
* `(None, N)` for N > 1 — collision; banner shows `(ambiguous -- N peers match this prefix)` AND emits a one-shot per-prefix `mm: notice:` to stderr (forensic breadcrumb for fleet-config issues; codex T4 caught the stderr-only-is-too-quiet hazard).

**Device list cache hoisted.** `mm resolve` calls `list_devices(backend)` ONCE before entering the walk and threads the resulting list into `_resolve_interactive_loop`. iCloud cold-cache reads can stack to multi-second per `list_devices` call; without hoisting, an N-conflict walk would N+1 on storage. The same pattern flows through `_pull_one_source` → `_download_and_apply` → `_apply_incoming_file` → `_prompt_conflict_choice` for the inline pull-time prompt.

**Init-time device-id collision regenerate.** `generate_unique_short_device_id(devices, max_retries=5)` (devices.py) draws `uuid.uuid4().hex[:8]` and retries on collision against the existing fleet. After exhaustion, returns the last drawn id and emits `mm: warning:` — runtime `lookup_device_by_short_id` defends in depth via the multi-match path, so a colliding install is degraded for attribution but not catastrophic. Wired into `_register_and_save` at init; runtime helper handles legacy collisions on already-registered fleets.

**`Literal['pre_inversion', 'post_inversion']` not `bool`.** `render_prompt(canonical, conflict, mode)` takes a typed mode string, not a True/False flag. A boolean that flips canonical/sidecar semantics is exactly the footgun class the v0.9.2 inversion section warns about; codex T6 caught the smell. Matches existing pattern (`ManifestFetch.status: Literal['ok','missing','corrupt']`).

**Inline pull-time prompt is post-inversion only.** `_prompt_conflict_choice` is called from `_apply_incoming_file` during pull, BEFORE `_apply_conflict` writes the sidecar — and `_apply_conflict` (post-v0.9.2) only ever produces post-inversion files. Pre-inversion files surface only later, in `mm resolve`'s discovery walk. So the inline path passes `mode="post_inversion"` unconditionally; the four-shape dispatch lives in `_resolve_interactive_loop` only.

`_check_fleet_version_or_refuse(backend, my_device_id)` runs at the top of `_pull_core` BEFORE any I/O. Per-peer classification via `packaging.version.Version` against `INVERSION_MIN_VERSION = "0.9.2"`: safe (>= 0.9.2 → ALLOW), inactive (last_seen missing → ALLOW), pre-v0.9.2 (last_seen present, version missing or < threshold → REFUSE), dropped (corrupt device.json → REFUSE by storage key). Refusal message names every offending peer; recovery is `pip install --upgrade mind-meld` + `mm push` on each peer. Implementation uses `list_devices_with_drops` (silent variant) so the `_select_devices`-side `_list_devices_warn` only logs once if the fleet check passes.

`update_last_seen` writes `last_seen_version: __version__` alongside `last_seen` on every push. Forward-compatible (older mm tolerates unknown keys). `mm devices` table surfaces it as a column.

## Init order + push-time self-heal (load-bearing, v0.9.4)
`_register_and_save` (renamed from `_save_and_register`) writes the remote first, the local pointer last: `register_device(backend, ...)` → `save_config(...)` → keyring store. Canonical filesystem/DB transaction discipline — a SIGKILL/OOM/power-loss in the window between the two writes leaves an inert orphan storage entry (recoverable on retry init via `_init_storage_guard`'s orphan-case prompt), never the inverse half-state where local config claims a `device_id` storage doesn't contain. Pre-v0.9.4 produced the inverse mapping. Do NOT reorder these calls.

If `save_config` raises (disk full, permissions), a best-effort `backend.delete(device_key(device_id))` cleanup runs before the original exception propagates — keeps orphans from accumulating in storage when normal save failures hit. Cleanup-failure surfaces a `mm: warning:` stderr breadcrumb but does NOT mask the original save error (visible-failure contract). The `device_key(device_id)` storage key is precomputed BEFORE `register_device` so the cleanup-warning f-string can't itself raise from `device_key`'s validation and mask the real cause (codex adversarial 2026-04-25).

`_ensure_device_registered(backend, device_id, device_name, *, dry_run)` runs at the top of `_push_core` BEFORE any push work. If `devices/<my_id>.json` is absent, it recreates it via `register_device`. Two scenarios converge: future v0.9.4+ SIGKILL crash mid-init (cosmetic) AND retroactive fix for pre-v0.9.4 victims of the v0.8.15..v0.9.3 inverted half-state — those users had been pushing manifests under an ID no peer recognized, silently. First push after upgrading to v0.9.4 self-heals. Gated on `not dry_run` (codex review: `mm push --dry-run` must not mutate storage). Register failures emit a `mm: warning:` stderr breadcrumb before re-raising — load-bearing for autopush, whose generic `except Exception` would otherwise swallow the failure and silently no-op every push.

## `_find_conflict_files` tuple-key dedup (load-bearing, v0.9.4 + v0.10.1)
The function runs two scan strategies that overlap when an `include_files` entry sits inside an `include_dirs` directory: (1) `include_dirs` rglob and (2) depth-0 sibling-glob for `include_files`. Without dedup, a conflict file at e.g. `projects/notes.sync-conflict-...md` is visited twice when a user customizes config with `include_files: ["projects/notes.md"]` AND `include_dirs: ["projects"]` (nested) — duplicate rows in `mm conflicts`, inflated counts in `mm gc --conflicts`, `mm resolve` silent no-op on the second visit. Default config doesn't trigger this (all `include_files` are bare top-level dotfiles), but the dedup is footgun-removal for anyone customizing.

v0.9.4 keyed dedup on `set[tuple[str, Path]]`. v0.10.1 strengthened the key to filesystem identity: `(src_name, st_dev, st_ino)` when stat succeeds, `(src_name, str(path))` fallback when stat fails (race window between glob and dedup — never silently drop a conflict file just because of a transient stat error). Filesystem identity handles the case-mismatched-config-on-APFS hazard (`include_dirs: ["projects"]` AND `include_files: ["Projects/notes.md"]` resolve to the same inode but distinct path strings; bare-string keys would let both through). The `src_name` component still preserves source attribution when two configured sources legitimately reference overlapping subtrees. Pinned in `tests/test_conflict_copy.py::TestFindConflictFilesNestedDedup` (v0.9.4) and `TestFindConflictFilesIdentityDedup` (v0.10.1).

## `walk_generic_source` filesystem-identity dedup (load-bearing, v0.10.1)
Mirror of `_find_conflict_files`'s dedup at the manifest-walk layer. When `include_files` overlaps `include_dirs`, the same on-disk file lands in `collected_paths` twice. Pre-v0.10.1, the second pass got hashed and overwrote the first manifest entry — wasted CPU on identical bytes. On case-insensitive volumes (APFS default) with case-mismatched config, two distinct rel-keys could be created for one inode — a real correctness bug producing phantom add/delete fleet churn.

Dedup uses `set[tuple[int, int]]` keyed on `(st_dev, st_ino)`. Sort `collected_paths` by relative-to-base path BEFORE the dedup pass so the rel-key kept on hardlink/symlink overlap is deterministic across runs and across machines (rglob iteration order is FS-dependent on macOS APFS). Without the sort, two peers walking the same tree could pick different rel keys for the same inode and generate phantom add/delete churn in the manifest diff. Sites: `manifest.py:walk_generic_source` (the pre-hash loop). Stat failures silently skip (consistent with `_record_file`'s race tolerance).

## Pull-time case-collision detection (load-bearing, v0.10.1)
A Linux peer can legitimately have BOTH `Projects/x.md` AND `projects/x.md` (case-sensitive ext4). A macOS APFS puller can only represent one — the second WRITE would silently alias / overwrite the first via inode collision. Pre-v0.10.1, this was a silent data-loss hazard.

`_detect_case_insensitive_fs(path)` is a non-invasive probe (no writes): construct a swapcase variant of the path's own basename and check via `samefile()` whether both names resolve to the same inode. Returns False on any failure (safer default — no spurious case-collision warnings on Linux ext4). Skips paths whose basename has no alphabetic characters or whose swapcase produces the same name.

`_detect_pull_case_collisions(manifest_cache, local_sources_map)` aggregates across ALL peer manifests so a collision between peer A's `"Projects/x.md"` and peer B's `"projects/x.md"` is detected even when neither peer alone exposes both casings. Returns clusters keyed by source name AND casefold key.

`_drop_case_collisions_from_manifests(manifest_cache, collisions)` returns a NEW cache (input not mutated) with all-but-lex-first paths dropped per cluster. Tombstones are NOT touched — collision is about per-pull WRITES on a case-insensitive consumer; tombstones encode prior consensus and stay intact (mirrors the asymmetric `_filter_disabled_sources` invariant). Manifest keys are NOT case-normalized GLOBALLY — only consumer-side WRITE skipping. Cross-platform peers retain their distinct casing in the synced manifest. The raw manifest stays intact for `mm gc` (which reads via `_fetch_remote_manifest`, unfiltered).

Hook site: `_pull_core` BEFORE `collect_tombstones` and the per-source download loop, AFTER the disabled-sources / exclude-patterns filter chain. Per-cluster `mm: warning:` to stderr names the kept and dropped paths so the user sees what was skipped (visible-failure contract).

## Peer-controlled string sanitization (load-bearing, v0.10.1, security)
Every synced filename AND file body crosses an untrusted trust boundary. Without sanitization, a peer can plant Rich markup (`[/red]…[red]`) or terminal escape sequences in any synced filename or file body and have them rendered as control output during `mm pull` / `mm conflicts` / `mm resolve` / `mm devices` / `mm status`. The OSC 52 vector is particularly nasty — many terminals (xterm, iTerm2, kitty, alacritty) honor base64-encoded clipboard writes from remote-controlled escape sequences, silently changing the user's clipboard. CSI `\x1b[2J` clears the screen; OSC 0/2 spoofs the title; DCS / C1 8-bit are also covered.

`strip_terminal_escapes(s)` removes the full common-grammar set: CSI `\x1b[…[\x40-\x7e]`, OSC `\x1b]…(BEL|ST)`, DCS, single-byte `\x1b[\x40-\x5f]`, and the rarely-used 0x9b 8-bit C1 CSI variant. Apply BEFORE rendering any peer-controlled string to a real terminal — Rich's `Text()` does NOT strip these.

`safe_str(s)` composes `strip_terminal_escapes` with `rich.markup.escape` and returns a plain `str`, so f-string composition with Rich markup tags continues to work: `f"[red]write failed:[/red] {safe_str(rel_path)}"`. Use at every print site interpolating a peer-controlled string (filenames, paths, source names, device names, error message tails — including exceptions whose `str(e)` echoes peer-supplied bytes).

`safe_text(s, **kwargs) -> rich.text.Text` is the diff-content variant. Use for diff CONTENT lines (peer-controlled file bytes printed via `console.print`). `Text()` alone defangs Rich markup but passes raw ANSI/OSC/DCS through to the terminal — same trust-boundary leak `safe_str` closes for filenames. Strip escapes first.

Sweep covers ~30 print sites: pull-prediction widget, upload progress, conflict prompts, write/merge/conflict apply paths, all `_apply_*` / `_pull_*` error tails, `mm devices` table cells, `mm diff` per-source headers, fleet-version refusal listing, `_print_pull_summary` warnings, `_resolve_interactive_loop` headers + prompts + diff labels + diff content + outcome lines. `mm devices` Rich Table cells are sanitized too — Table cells interpret markup AND pass raw escapes through (verified). All sites pinned in `tests/test_safe_str.py`.

**v0.11.1 extension — banner trust boundary covers `device_name` too.** The conflict-prompt LOCAL/REMOTE banners (Track 12A, conflictdiff.py) interpolate two peer-controlled strings: the conflict filename AND the peer's `device_name` (set via `typer.prompt` at peer init, plaintext-synced via `devices/<id>.json`, and rendered as `(from <peer_name>)` on the REMOTE banner). `render_banner` wraps both inputs in `safe_text` BEFORE composition so a peer planting OSC 52 / CSI / DCS in their own `device_name` cannot reach the terminal of any peer that pulls. Codex outside-voice review (T6) caught this — pre-v0.11.1 the sanitization sweep covered filenames but not `device_name`. Pinned by `tests/test_safe_str.py::TestConflictBannerSanitization`.

**v0.11.1 module move.** `safe_str`, `safe_text`, `strip_terminal_escapes` live in `mind_meld.safety` (extracted from cli.py to break the cli↔conflictdiff circular import). cli.py re-exports the names for backwards compat; new tests should import from `mind_meld.safety` directly.

## `register_device` create-only contract (load-bearing, v0.10.1)
Pre-v0.10.1, `register_device` always wrote `backend.put(key, ...)`. The push-time `_ensure_device_registered` self-heal (v0.9.4) called `register_device` whenever `backend.exists(key)` returned False. iCloud's `.icloud` placeholder (cloud-only, lazy-materialized) creates a TOCTOU window where `backend.exists()` reports False but the entry actually exists on storage — the self-heal re-registered, silently bumping the `registered:` first-registration timestamp on every push.

v0.10.1 routes `register_device` through `LocalBackend.put_exclusive(key, data)` (atomic `os.link` with `EEXIST` detection) so the create-only invariant holds at the filesystem layer regardless of placeholder state. Existing entries surface as `StorageError`, which the function swallows + returns. Original `registered:` timestamps are preserved across re-registration. Idempotent: self-heal callers can re-register safely.

## Devices write lock (load-bearing, v0.10.1)
`update_last_seen` does a read-modify-write of `devices/<id>.json` on every push (mutates `last_seen` + `last_seen_version`). Concurrent autopush + interactive push could race on the RMW. Today's deterministic fields (`last_seen`, `last_seen_version`) don't lose data because both writers compute the same effective state, but any FUTURE non-deterministic field (e.g. per-machine notes, error counters, partial-progress markers) would lose interleaved updates.

`_devices_write_lock()` is a `contextlib.contextmanager` wrapping the RMW in `fcntl.LOCK_EX | LOCK_NB` against `~/.config/mind-meld/devices-write.lock` (mode 0o600, parent dir auto-created). Brief retry budget on contention — `_LOCK_RETRY_INTERVALS_S = (0.05, 0.1, 0.2, 0.4)` (~750ms total before degrading). Total acquire wait stays well under 1 second since the critical section is one storage GET + one storage PUT.

On exhausted retries, degrade to executing without the lock and emit one `mm: warning: device write lock contended; skipping last_seen update for this push` line to stderr (visible-failure contract). Today's deterministic fields are safe under degraded operation; the warning lets the user catch a stuck-process scenario before any future non-deterministic field starts losing data.

The lock is LOCAL (per-machine config dir) — `fcntl.flock` is a local-process primitive and never reaches synced storage. All RMW callers MUST hold the flock for the read AND write so an interleaved read can't observe a partial state. Routing field-adders through this lock is forward-defense for concurrency safety.

## `mm-events` default source + bootstrap (load-bearing, v0.10.1)
`DEFAULT_SOURCES` has a new mm-owned synced source for the per-device daily JSONL event log Group 8's `retro-fleet` skill will read.

```toml
{ name = "mm-events", path = "~/.local/share/mind-meld",
  type = "generic", include_dirs = ["events"], exclude_patterns = [] }
```

Subdir nesting (`include_dirs = ["events"]` rather than `["."]`) plays cleanly with `walk_generic_source` and avoids the `pathlib`-`["."]` quirk. Per-device daily JSONL files land at `events/<device>-<YYYY-MM-DD>.jsonl` under this base path.

`get_sources()` runs a one-shot bootstrap dispatch BEFORE the path-existence filter so mm-internal sources don't fall through as "doesn't exist" on first run. Dispatch table: `{"mm-events": _bootstrap_mm_events_path}`. Adding a new entry to `MM_INTERNAL_SOURCE_NAMES` REQUIRES adding the parallel bootstrap entry here — the dispatch by name keeps the mapping explicit and prevents silent inconsistency between `_prompt_sources` auto-include and bootstrap. Bootstrap is mode 0o700 (events contain device IDs and per-machine activity metadata — not user-secret but per-machine-private). mkdir failures emit `mm: warning:` per the visible-failure contract; the source then drops via the path-existence filter that runs after.

**Warn-once on bootstrap failure (Group 7 hotfix).** `_bootstrap_mm_events_path` keeps a module-level `_BOOTSTRAP_WARNED_PATHS: set[str]` of paths whose mkdir has already failed in this process. First failure emits `mm: warning:` (preserves the visible-failure contract — monitoring catches the wedge); subsequent `get_sources()` calls in the same process short-circuit before mkdir + stderr. Without this, chmod-restricted-home users would see warning spam on every read-only command (`mm sources` / `mm status` / `mm conflicts` / `mm diff` / `mm log` all call `get_sources()`). Per-path keying (not per-process) preserves the contract for the unlikely case of two failing mm-internal source paths. Tests touching the failure path must reset via `monkeypatch.setattr(config, "_BOOTSTRAP_WARNED_PATHS", set())`.

## `MM_INTERNAL_SOURCE_NAMES` + init contract (v0.10.1)
`frozenset({"mm-events"})` in `config.py` enumerates source names that are mm-owned infrastructure, not user-prompted. Two consumer sites:

1. **`_prompt_sources` (init):** mm-internal entries auto-include without a Y/n prompt — they're mm-owned infrastructure for fleet-wide features (retro-fleet) and shouldn't burden the init UX with a question whose only legitimate answer is "yes." Per-machine opt-out remains via `mm disable-source mm-events` post-init (v0.10.0).
2. **`init_cmd` no-sources guard:** an init that produces only mm-internal sources fails the `user_facing_sources` check (refuses with the same "no sync sources enabled" error as the pre-Group-7 zero-sources case). A config with only mm-events is effectively "user wanted nothing synced" — push/pull would silently no-op for the user's own data; better to refuse and let them re-run.

Adding a new mm-internal source name requires updating the frozenset AND the bootstrap dispatch in `get_sources()` AND (if it has a meaningful per-machine state) wiring `mm disable-source` strict-mode allowance. Keep the set small — every entry sidesteps the init-prompt UX, so only mm-owned synced infrastructure qualifies (today: events).

## Events tail in `_push_core` (load-bearing, v0.10.3)

Track 7B wires `events.py` (Track 7A foundation, v0.10.2) into the push hot path. `_run_events_tail(config, sources, device_id, *, dry_run, quiet)` runs at the **HEAD** of `_push_core` — AFTER `_ensure_device_registered`'s self-heal (v0.9.4) and the no-sources guard, BEFORE `build_manifest_v2`. The events file lands on disk in time to be uploaded same push (no one-push lag). Four invariants govern the wiring:

1. **Head-position single-call-site (Codex C4).** Inline-before-each-early-return inside the diff loop was the original plan and got reverted: branch fragility was the wrong tradeoff against `events.py:19-22`'s "must run on every push attempt" trust boundary. The HEAD position fires before any control flow could divert and reuses the existing no-sources guard for filtering. Do NOT add additional call sites.

2. **`dry_run` no-op (preview contract).** `mm push --dry-run` must not mutate disk. The events tail returns immediately when `dry_run=True`, mirroring `_ensure_device_registered`'s same gate (codex review 2026-04-25).

3. **`mm-events`-resolved gate, NOT `disabled_sources` (Codex C1).** The gate is `next((s for s in sources if s.get("name") == "mm-events"), None) is not None`. This covers fresh / migrated / un-migrated configs uniformly: a config that pre-dates v0.10.1 simply has no `mm-events` entry and the tail no-ops, no migration prompt required. Gating only on `disabled_sources` would let pre-v0.10.1 configs accumulate local cruft forever (the `~/.local/share/mind-meld/events/` tree never created, never written). The `_bootstrap_mm_events_path` dispatch in `get_sources()` ensures fresh configs land here with the path materialized.

4. **Wall-clock budget (Codex C4 + C5).** `WALK_TIME_BUDGET_AUTOPUSH_MS` (250) for `quiet=True` (autopush hook), `WALK_TIME_BUDGET_INTERACTIVE_MS` (500) for interactive `mm push`. The deadline is plumbed through to `walk_session_metadata` via the new keyword-only `deadline_monotonic` param — `_read_cwd_from_latest_jsonl` reads jsonl line-by-line until a `cwd` field appears, so a single pathological project can blow the budget without per-project deadline checks. A tail-position `time.monotonic() > deadline` check emits `mm: notice: events tail budget exceeded` to stderr (visible-failure contract; the push proceeds).

**Forensic-only invariant.** The whole block is wrapped in `try / except Exception`; failures emit `mm: notice: events tail failed: <type>: <safe_str(msg)>` to stderr and the push continues. `safe_str(e)` defangs peer-controlled escapes per the v0.10.1 sanitization invariant (a corrupt peer manifest could otherwise smuggle ANSI through an exception's `__str__`).

**`MmPushEvent.sources` schema is `list[str]` (names only) — Codex C2 + C7.** `iter_source_diffs(skip_unchanged=True)` drops unchanged sources from the diff loop, breaking per-source counts on the no-content push path. The retro-fleet skill (Group 8) reads per-source content stats from the synced manifest at retro time, not from the event row. `make_mm_push_event` filters `MM_INTERNAL_SOURCE_NAMES` from the names list — `mm-events` is mm-owned infrastructure, not user-meaningful fleet activity.

**Fleet retention via tombstone propagation (Codex C10).** `_gc_old_event_files` reaps day files older than `EVENTS_RETENTION_DAYS` (90). The retro skill reads the synced manifest, so deletion fans out fleet-wide via the existing tombstone path: this device unlinks → next push generates a tombstone → all peers drop their copy on pull. An offline peer that comes back online sees the tombstone too, suppressing resurrection.

**Reap by FILENAME date, NOT mtime (Codex C5, C6).** iCloud restores can rewrite mtimes back to "now" while the filename date (`<device>-YYYY-MM-DD.jsonl`) is intrinsic to the event-day boundary the file was written for. The mm-events path resolves through `get_sources(config)` so user-customized paths are honored. Always-on (no `--events` flag) — events retention is fleet policy.

**Initial cursor lookback (Codex C9).** `last_push_ts(events_dir, device_id)` returns `now - INITIAL_CURSOR_LOOKBACK_DAYS` (30) when no prior `mm-push` event exists. New fleet members joining mid-quarter scan back 30 days of git history; older context is invisible to retro until a manual backfill. Document the bound in skill output: "First-run window: last 30 days of activity. Older history is intentionally outside the retro window."

## Sessions snapshot v=2 full-inventory (load-bearing, v0.11.0)

`EVENTS_SCHEMA_VERSION` bumped 1 → 2 in Group 8. Pre-v0.11.0, `walk_session_metadata` filtered jsonls by `mtime >= since_ts` — each snapshot was a DELTA. Naive sum of v=1 snapshots double-counted any chat that was touched across pushes; latest-only-wins undercounted by losing prior windows. Codex outside-voice review caught the trap during `/plan-eng-review` for Group 8 (cross-model tension #1).

v=2 sessions-snapshot is FULL INVENTORY: every jsonl in the projects tree is counted regardless of mtime. The aggregator picks the LATEST v=2 snapshot per `(device, source_root, claude_dir)` — produces an accurate point-in-time sessions count for the rendering machine's view of the fleet. mm-push and git-snapshot rows keep delta semantics (commits since last push, dedup-by-sha aggregator side); only sessions-snapshot semantics changed.

**Mixed-fleet transition rule.** Pre-v0.11.0 peers still emit v=1 sessions rows. The retro-fleet aggregator treats v=1 sessions as below-threshold and surfaces "Sessions count incomplete: peer X is on pre-v0.11.0" as part of the fleet-incomplete breadcrumb. Numbers are honestly low, never overcounted. Once the fleet rolls to v0.11.0, every peer emits v=2 and the count is exact.

**`since` parameter retained for API stability.** `walk_session_metadata(claude_dir, since, *, deadline_monotonic)` still accepts `since` to keep the call-site signature stable; the value is now ignored (suppressed via `# noqa: ARG001`). A future v=3 schema can re-introduce delta semantics with a new field name without breaking callers.

**`source_root` field on `SessionMetadata` (load-bearing, post-v0.11.2 Group 8 hotfix).** Every `SessionMetadata` carries a `source_root: str` field equal to `str(claude_dir)` from the `walk_session_metadata` caller. The aggregator keys on the 3-tuple `(device, source_root, claude_dir)` instead of the original 2-tuple — pre-fix, two configured `type: claude` source roots that both contained a project encoded as e.g. `-Users-kb-Documents-foo` silently overwrote each other in `latest`. The schema change is additive (`SessionMetadata` is `TypedDict, total=False` — old readers ignore unknown fields, new readers default missing field to `""`), so no v=3 bump.

**Coalesce pass for the rollout window.** Pre-fix records on synced storage have no `source_root` field (treated as `""`); post-fix records carry the populated path. During the rollout window both shapes coexist for the same project. `aggregate_sessions` runs a coalesce pass between the latest-per-tuple population and the `last_session_at` filter that drops `(device, "", claude_dir)` keys when `(device, "<root>", claude_dir)` exists for the same device. Distinct populated `source_root` values are preserved (the legitimate two-source-root case the fix is for); only the legacy empty key with a populated sibling is collapsed. Pinned by `tests/test_retro_fleet_aggregator.py::TestSessionsSourceRoot` (4 tests including the REGRESSION pin `test_two_distinct_source_roots_kept_separate`).

## Aggregator custom-path notice (post-v0.11.2 Group 8 hotfix)

`_emit_custom_path_notice_if_due(events_dir)` runs from `aggregator.main()` right after `events_dir = _resolve_events_dir()`. Library callers of `aggregate()` never see the notice — the gating is in `main()` only. Three-stage gate: (1) `MM_EVENTS_DIR` set → silent (user is overriding correctly); (2) resolved `events_dir != DEFAULT_EVENTS_DIR` → silent (already non-default via param/env); (3) `_read_mm_events_config_path()` returns the configured `mm-events` path; if it equals `DEFAULT_EVENTS_DIR.parent` → silent (config matches default), else emit one `mm: notice:` to stderr pointing at the env override. `_read_mm_events_config_path` mirrors `_read_config_author_emails` — wraps `from mind_meld.config import CONFIG_PATH, load_config` in `try/except Exception`, returns None on any failure, never raises. Pinned by `tests/test_retro_fleet_aggregator.py::TestCustomPathNotice` (5 tests).

## Group 8 retro-fleet skill — symlink installer (load-bearing, v0.11.0)

`_ensure_retro_skill_link()` symlinks `~/.claude/skills/retro-fleet` → `<wheel>/mind_meld/skills/retro_fleet/`. Source dir is `retro_fleet/` (underscore — Python identifier so `python -m mind_meld.skills.retro_fleet.aggregator` works); link name is `retro-fleet` (hyphen — Claude Code skill convention). The conventions and importability both resolve cleanly via the rename.

**Five-branch state machine.** `target.exists()` returns False on a dangling symlink while `is_symlink()` returns True — these are checked in this order: (1) skills-dir-absent → silent skip (no Claude Code installed); (2) `target.is_symlink() and not target.exists()` → DANGLING-symlink branch, unlink + recreate (REGRESSION-class for `pipx reinstall` recovery; pre-Group-8 design routed dangling links into "exists, don't replace" forever); (3) `target.is_symlink() and target.resolve() == skill_src.resolve()` → already-correct, no-op; (4) `target.exists()` → conflict-skip with `mm: notice:`; (5) target absent → `target.symlink_to(skill_src)`. Every `OSError` from `symlink_to` is wrapped — TOCTOU `FileExistsError`, `PermissionError` on read-only `~/.claude`, `OSError` on filesystems without symlink support all degrade to a stderr breadcrumb without crashing push.

**Two-marker 24h-TTL gate (cross-model #3).** A single TTL marker can't distinguish "skip until tomorrow because it just succeeded" from "skip until tomorrow because the user has their own file there" — touching the marker on conflict skips silently for 24h, leaving it untouched re-emits the notice every push (hostile noise). Two markers under `~/.config/mind-meld/`: `.skill-link-checked` (success) and `.skill-link-conflict` (deliberate-skip). Transient failures (OSError) touch neither, so next push retries. `_marker_is_fresh()` wraps `os.stat` in try/except and **fail-opens** on EACCES / EIO so a chmod-restricted config dir doesn't crash push (TODO#3 critical-gap fix).

**Hook positions.** `mm init` calls `_ensure_retro_skill_link(dry_run=False)` unconditionally at the end. `_push_core` HEAD calls `_ensure_retro_skill_link()` AFTER `_ensure_device_registered` but BEFORE `_run_events_tail` (Architecture #5 lock-in: stacked self-heals before the events tail's load-bearing capture block). Gated by `_skill_link_check_due()` — one `os.stat` syscall per push on the steady-state path. `dry_run` is plumbed through and gates the install (preview contract; mirrors `_ensure_device_registered`).

## `mm devices --format=json` (v0.11.0)

JSON formatter alongside the Rich Table renderer. Schema (stable contract for the retro-fleet aggregator's subprocess consumer):

```json
[
  {
    "device_id": "<str>",
    "device_name": "<str|null>",
    "last_seen": "<iso str|null>",
    "last_seen_version": "<str|null>",
    "is_self": <bool>
  },
  ...
]
```

Empty fleet returns `[]`. Sorted alphabetically by `device_id` for cross-platform stability (`list_devices` filesystem iteration is FS-dependent on Linux ext4 vs macOS APFS — without the sort, two peers walking the same fleet could produce different orderings). Plain `print(json.dumps(...))` — Rich injects styling that breaks the JSON contract. Pinned by `tests/test_devices_json.py`.

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
