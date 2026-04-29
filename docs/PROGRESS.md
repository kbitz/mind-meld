# Progress

## Version history

| Version | Released | Headline |
|---------|----------|----------|
| 0.11.2  | 2026-04-29 | **Group 7 hotfix — `mm: warning:` no longer spams on every read-only command.** `_bootstrap_mm_events_path` now consults a module-level `_BOOTSTRAP_WARNED_PATHS: set[str]` and short-circuits before `mkdir` + stderr emit on subsequent calls for any path that already failed in this process. Users with a chmod-restricted `~/.local/share/` (or any environment where the `mm-events` source-dir mkdir fails) previously saw the warning line on every invocation of `mm sources` / `mm status` / `mm conflicts` / `mm diff` / `mm log` (~11 internal call sites of `get_sources()`); now once per process per failing path. First failure still surfaces the breadcrumb (visible-failure contract preserved — monitoring catches the wedge); per-path keying preserves the contract for the unlikely case of two failing mm-internal source paths. Pinned by a new `test_bootstrap_warns_once_per_process` regression test that calls `get_sources()` 5× and asserts exactly 1 warning is emitted. Existing failure-path test resets the cache via `monkeypatch` for ordering independence. |
| 0.11.1  | 2026-04-29 | **Conflict-prompt UX redesign — BREAKING.** Out-of-plan post-Group-8 ship. Default key changed `(b)oth` → `(s)kip`: same on-disk effect (canonical and `.sync-conflict-*` both stay) but the option name now matches the action. Pre-1.0 letters `b`/`both` aliased to skip with a one-time `mm: notice:` so stale scripts continue to work; alias removes at 1.0. Three-number divergence summary `(M, N, K)` from new `count_divergent_lines` helper — honest about replacement semantics (1-line replacement is M=1, N=1, K=2). Banner attribution chain via `parse_conflict_device_short` + `lookup_device_by_short_id` shows `(from <peer_name>)` on REMOTE banners, with explicit `(unknown peer)` / `(ambiguous — N peers match)` paths plus per-prefix `mm: notice:` on collision. Device list cache hoisted at `mm resolve` and `_pull_one_source` so an N-conflict walk doesn't N+1 on iCloud cold-cache `list_devices` reads. Init-time `generate_unique_short_device_id` retries up to 5× on fleet collision; runtime helper defends in depth via the multi-match path. Honest skip-lifecycle copy ("leave both files on disk; run `mm resolve` later or delete manually" — explicit that next pull does NOT re-prompt). Peer-controlled `device_name` extended into the v0.10.1 sanitization sweep — `render_banner` wraps both filename and `device_name` in `safe_text` so a peer planting OSC 52 / CSI / DCS in their own `device_name` cannot reach a puller's terminal (codex outside-voice T6 catch). `Literal['pre_inversion', 'post_inversion']` mode string, not bool — matches the existing typed-Literal pattern, avoids the v0.9.2-class footgun where a bool that flips canonical/sidecar semantics is exactly what the inversion section warns against. Two modules extracted from cli.py to break a circular import: `safety.py` (`safe_str`, `safe_text`, `strip_terminal_escapes` — re-exported from cli.py for backwards compat) and `conflictdiff.py` (`render_prompt`, `render_banner`, `count_divergent_lines` — pure leaf primitives shared by the post-pull walk in `_resolve_interactive_loop` and the inline pull-time `_prompt_conflict_choice`). Site-level dispatch over the four shapes (canonical-exists × pre/post-inversion × canonical-missing) stays at each call site to keep the load-bearing filename-prefix dispatch visible. |
| 0.11.0  | 2026-04-28 | **Group 8 / Track 8A — fleet-aware retro skill.** Claude Code skill that stitches activity from every Mac in the fleet into one paste-ready markdown retro. Reads the synced `mm-events` log (Group 7), dedups commits via `(canonical_remote_url, sha)`, picks the latest sessions snapshot per `(device, claude_dir)`, and renders the gstack `/retro` shape. Three ship surfaces: (1) `src/mind_meld/skills/retro_fleet/{SKILL.md, aggregator.py}` shipped in the wheel; (2) `mm devices --format=json` with stable schema (`device_id`, `device_name`, `last_seen`, `last_seen_version`, `is_self`); (3) `_ensure_retro_skill_link` symlink installer dropped at `mm init` and self-heals every push behind a 24h-TTL marker (5-branch state machine: absent / correct symlink / dangling / wrong target / OSError — pipx-reinstall recovery). `EVENTS_SCHEMA_VERSION` bumped 1 → 2: sessions-snapshot now FULL INVENTORY (every jsonl regardless of mtime) so the aggregator can pick latest-per-tuple and produce honest point-in-time counts. v=1 peers still tolerated — surfaced as "Sessions count incomplete: peer X on pre-v0.11.0" instead of overcounted. Aggregator honors `last_session_at` window (caught in adversarial review — 60d-old session no longer leaks into a 7d retro), survives invalid UTF-8 via `errors="replace"`, counts file-open failures into `skipped_lines`. `mm devices` subprocess invokes via `python -m mind_meld.cli` to sidestep PATH-hijacking. 5 deferred adversarial-review items filed in TODOS.md (concurrent push lock contention, large-fleet aggregator scaling, mm-not-on-PATH guard, more). 1300+ tests pass. |
| 0.10.1  | 2026-04-27 | **Group 7 preflight — security hardening + concurrency safety + correctness fixes + mm-events foundation.** Eight cleanup items spanning the peer-controlled trust boundary, filesystem-identity dedup, device-write concurrency safety, and the mm-events default source needed by the upcoming retro-fleet skill (v0.11.0). All additive. (1) `safe_str()` / `safe_text()` sanitize peer-controlled strings at every render site (~30 print sites swept) — strips CSI / OSC / DCS / C1 terminal escape sequences AND defangs Rich markup. Closes the OSC 52 clipboard-write vector (xterm/iTerm2/kitty/alacritty honor base64-encoded clipboard writes from remote escapes), CSI screen-clear, OSC title spoof. Pinned in `tests/test_safe_str.py`. (2) Pull-time case-collision detection on case-insensitive filesystems — non-invasive `_detect_case_insensitive_fs` swapcase + samefile probe (no writes), bucket peer-manifest paths by casefold per source, drop all-but-lex-first via `_drop_case_collisions_from_manifests` (manifest keys NEVER case-normalized globally — only consumer-side WRITE skipping; cross-platform peers retain distinct casing). Per-cluster `mm: warning:` to stderr. (3) `register_device` is now create-only via `LocalBackend.put_exclusive` (atomic `os.link` + EEXIST detection) — fixes the iCloud `.icloud` placeholder TOCTOU window where `_ensure_device_registered`'s `backend.exists()` check returned False on a present-but-cloud-only entry, silently bumping the `registered:` first-registration timestamp on every push. (4) `update_last_seen` serializes via `_devices_write_lock()` (`fcntl.LOCK_EX | LOCK_NB`, ~750ms retry budget, degrade-with-warning on contention) — forward-defense for the moment a non-deterministic device.json field lands. (5) `walk_generic_source` filesystem-identity dedup via `set[tuple[st_dev, st_ino]]` with deterministic pre-sort (rglob iteration order is FS-dependent on macOS APFS; sort-then-dedup avoids phantom add/delete fleet churn on hardlink/symlink overlap). (6) `_find_conflict_files` dedup key strengthened from `(src_name, Path)` to `(src_name, st_dev, st_ino)` with `(src_name, str)` fallback on stat failure — same shape as walker. (7) `mm-events` default source at `~/.local/share/mind-meld/events/` (mode 0o700) for Group 8's retro-fleet event log; auto-included at `mm init` (mm-internal, no prompt — disable per-machine via `mm disable-source mm-events`); `get_sources()` bootstraps the directory on first call so the source isn't inert between Group 7 and Group 8 ship. (8) `src/mind_meld/skills/` placeholder subpackage ships in the wheel via existing `packages = ["src/mind_meld"]` so Group 8's `retro-fleet/SKILL.md` symlink installer can find resources via `importlib.resources.files("mind_meld") / "skills"`. New `MM_INTERNAL_SOURCE_NAMES = frozenset({"mm-events"})` short-circuits prompts for mm-owned infrastructure; init guard refuses on zero user-facing sources (mm-events doesn't count). 4 new test modules (`test_safe_str.py`, `test_case_collision.py`, `test_devices.py`, `test_wheel.py`) + extensions to `test_config.py` / `test_conflict_copy.py` / `test_manifest.py`. Two adversarial follow-ups filed in TODOS for v0.10.x patch: read-only-command bootstrap warning spam, conftest devices-import coupling. |
| 0.9.4   | 2026-04-25 | **Track 5D — adversarial-review follow-ups for the v0.8.15 Track 5A ship.** Two surgical hardening fixes plus a self-heal hook. (1) `_find_conflict_files` now dedups via a `seen: set[tuple[str, Path]]` accumulator — `mm conflicts` no longer double-counts when an `include_files` entry sits inside an `include_dirs` directory (default config doesn't trigger this; footgun-removal for users customizing). Tuple key (not bare `Path`) preserves source attribution when two configured sources legitimately reference overlapping subtrees. (2) `mm init` order swap: `_save_and_register` → `_register_and_save`. Storage-write (`register_device`) runs FIRST, then local pointer (`save_config`) — canonical "remote first, local pointer last" transaction discipline. A SIGKILL/OOM/power-loss in the post-register, pre-save window now leaves an inert orphan storage entry (recoverable on retry init via `_init_storage_guard`'s orphan-case prompt) instead of the inverse half-state where local config claimed a `device_id` storage didn't recognize. The original local-side rollback try/except is gone; a new best-effort `backend.delete(devices/<id>.json)` cleanup wraps `save_config` so normal save failures (disk full, permissions) don't trip the orphan-case warning on retry. (3) Push-time self-heal — new `_ensure_device_registered` hook at `_push_core` entry recreates `devices/<my_id>.json` if it's absent. Two scenarios converge here: future v0.9.4+ SIGKILL crash mid-init (cosmetic) AND retroactive fix for pre-v0.9.4 victims of the v0.8.15..v0.9.3 inverted half-state (config has `device_id`, storage's `devices/` doesn't — without this hook those users push manifests under an ID no peer recognizes, silently). Gated on `not dry_run` (codex review caught `mm push --dry-run` must not mutate storage). 15 new tests pin the regressions (5 conflict-dedup, 7 register-and-save ordering, 3 self-heal). 789 pass. No fleet-version threshold change — `INVERSION_MIN_VERSION` stays at `"0.9.2"`. |
| 0.9.3   | 2026-04-25 | Hotfix patch (post-Track-5C): added `config.yaml` to the gstack source's default `exclude_patterns`. Track 5C (v0.9.1) covered `projects/*/repo-mode.json` and `projects/*/land-deploy-confirmed` but missed `~/.gstack/config.yaml`, which holds gstack's version-check tracking and other machine-local IDs. Syncing it actively breaks the version mechanism on whichever machine pulls last. Existing installs need `mm migrate-config` (visible-failure contract from v0.9.1 surfaces the missing-excludes signal via `mm status`). Fresh installs get the fixed default automatically. No fleet-version threshold change — `INVERSION_MIN_VERSION` stays at `"0.9.2"`. One-time cleanup note for v0.9.2→v0.9.3 upgraders with a stranded `~/.gstack/config.sync-conflict-*.yaml` sidecar: list + delete via `find ~/.gstack -maxdepth 1 -name 'config.sync-conflict-*'` (the proper depth-0-scan fix lands in a follow-up patch). |
| 0.9.2   | 2026-04-25 | **Track 5E (Conflict default inversion) + 4 ship-fix bug fixes — BREAKING.** Inverted `_apply_conflict`: canonical = LOCAL bytes, REMOTE bytes go to `.sync-conflict-*` sidecar (opposite of every prior version). Strict pull-start fleet-version refusal (`mm pull` exits non-zero before any I/O if any peer's `last_seen_version < 0.9.2` or `device.json` is corrupt). Pre-inversion conflict-file migration to `v0-` prefix (lock-protected, `mm pull` / `mm resolve` only). Dual-mode `_resolve_interactive_loop` dispatch by filename prefix (`v0-` = pre-inversion ops; no prefix = post-inversion ops). `mm conflicts` Mode column + `mm devices` Version column + `update_last_seen` writes `last_seen_version`. Added `packaging>=21.0` for `Version` parsing. /ship pre-landing review caught 1 CRITICAL (silent data loss in resolve via mtime install-marker fix) + 3 HIGH (TOML escape in `migrate-config`, autopull spam on mixed-version fleet routed through typed refusal, fleet-check ordering) — all fixed in the same release. 11 new tests in `TestInversion5E`, 768 pass. |
| 0.9.1   | 2026-04-25 | **Track 5C (`exclude_patterns` + log + migration UX).** Per-source `exclude_patterns: list[str]` of fnmatch globs matched against the relative path. Default `gstack` source ships with `["projects/*/repo-mode.json", "projects/*/land-deploy-confirmed"]` (per-machine artifacts that churn-conflict on every pull). `_filter_excluded_paths` applies at TWO consumer-boundary call sites: `_pull_core` (filters peer manifests in `manifest_cache` BEFORE `collect_tombstones` and the per-source download loop) and `_push_core` (filters the manifest returned by `_recover_prior_manifest` BEFORE `generate_tombstones`). MUST NOT apply at `_fetch_remote_manifest` — `mm gc` reads raw manifests there. Tombstone-suppression invariant: adding a path to `exclude_patterns` does NOT generate a deletion tombstone on next push (2026-04-24 first-pull regression pin). `mm log` JSONL writer/reader at `~/.config/mind-meld/pull-history.jsonl` (mode 0600, `fcntl.flock`-guarded, 1MB cap with line-boundary rotation, torn-first-line tolerance). `mm migrate-config` command (idempotent, appends missing recommended excludes, preserves user-customized globs). Visible-failure contract for migration UX: autopull/autopush record missing-excludes signal to migration-state.json and surface via `mm status` warning (no auto-mutation in hooks). `mm sources` excluded-count column. 38 new tests including 5 IRON RULE regression pins (two-device first-pull case, tombstone-on-exclude-transition, tombstone-on-unexclude-transition, sidecar bypass guard, `mm gc` safety). Originally scoped to "conflict default inversion + real-merge backends"; pivoted via /plan-ceo-review on 2026-04-25 after analysing the 2026-04-24 first-pull data (24/25 divergent files were per-machine artifacts excludes prevent). Real-merge backend deferred to Future. Inversion split out as Track 5E. |
| 0.9.0   | 2026-04-25 | **Track 5B (Pull / resolve / conflicts UX surfaces) — BREAKING.** Vocabulary unified across `mm resolve`, `mm conflicts`, and pull summary: `(c)anonical / (f)orce conflict` → `(l)ocal / (r)emote / (b)oth / (a)bort`. Old letters `c`/`f` rejected loudly to stderr (visible-failure contract; piping legacy scripts errors out instead of silently falling through to default "kept both"). Diff display labels (fromfile/tofile), helper text in `conflicts()`, resolve docstring, and parallel `(p)/(d)/(s)` preface all flipped to the new vocabulary in one PR (per `/plan-ceo-review` D3). `mm conflicts` table renames "Conflict"/"Canonical" columns to "local"/"remote" + per-column wrap (`add_column(no_wrap=False, overflow="fold")`) so long paths no longer truncate at terminal width. `_print_pull_summary` lists conflicted/failed paths inline under each per-source line (cap 20 with overflow marker; `--verbose` unlocks per `/plan-ceo-review` D5). Pre-existing docstring/code mismatch fixed (D11): per-source conflicts/failures now reach stderr in quiet mode (autopull), with `<device>/<source>` prefix because the per-device header is suppressed in quiet — matches CLAUDE.md visible-failure contract. `mm pull` Rich Progress widget for TTY (gates via `console.is_terminal`), plain "downloading N file(s)" banner for non-TTY, silent in autopull (`quiet` threaded through `_pull_one_source` → `_download_and_apply` so progress can't leak in autopull). Empty-`to_download` gate prevents Rich Progress with `total=0`. Variable names in `_resolve_interactive_loop` (`local_text`/`remote_text`) reflect today's _apply_conflict semantics with `5B-5C-REMAP-BOUNDARY` markers throughout (cli.py + test class) so Track 5C's inversion surfaces every assertion that needs to flip — pre-inversion `.sync-conflict-*` files persisted on disk are 5C's problem to handle (timestamp-based detection or migration; filed in 5C handoff per `/plan-ceo-review` D9). 14 new tests pinning today's mapping, quiet contract, cap/verbose, multi-device disambiguation, and Task 4 plumbing. 700 pass. |
| 0.8.15  | 2026-04-24 | Group 5 preflight + Track 5A bundled per `/plan-eng-review` D2. Three bug fixes shipped together: P0 `mm autopull`/`autopush` silent-mode contract regression (binding-vs-attribute mismatch in `_auto_command_setup` preflight — fixed by switching to `_config_module.CONFIG_PATH` so `load_config` and the preflight stay in sync under both production and test); `_synced_scan_dirs` undercount on generic-type sources (depth-0 sibling-glob inlined into `_find_conflict_files` so conflict copies on top-level `include_files` entries like `~/.gstack/config.yaml` are visible to `mm conflicts` / `mm resolve` / `mm gc --conflicts`, gated by `is_conflict_filename` strictness); `_save_and_register` rollback (init now atomic — register failure unlinks the saved config so peers never see a `device_id` claimed in local config but missing from `devices/`, original exception wins even if rollback unlink itself fails). Group 5 preflight bundled: `retro-context.md` + `greptile-history.md` added to gstack `DEFAULT_SOURCES.include_files` for cross-machine memory continuity. Group 1's `constants.py` extraction preflight dropped after `/plan-eng-review` cohesion check (only 2 of 4 candidates were cross-module, would have split `FORMAT_VERSION`/`FORMAT_VERSION_LEGACY_V1`) — Group 1 marked ✓ Complete. 20 new tests (4 regressions pinned), 685 pass. |
| 0.8.14  | 2026-04-24 | Roadmap tidy. Freshness scan marks Groups 2/3/4 ✓ Complete in place (shipped through v0.8.7/v0.8.8/v0.8.10/v0.8.11); Tracks 1A/1B/1C collapsed to one-liners (only Group 1's preflight `constants.py` extraction remains). Triages 11 unprocessed items: 10 kept in current phase as new Group 5 (Conflict UX & first-pull polish) with three serialized Tracks — 5A (auto-command + scope bugs incl. P0 autopull silent-mode regression, ships first), 5B (resolve/conflicts/pull UX relabel), 5C (conflict default inversion + real-merge backends, ships last). One item (cross-device source rename drift) deferred to Future as documented known limitation. PROGRESS.md "Where we are" refreshed to match. |
| 0.8.13  | 2026-04-24 | Docs: log 8 conflict-UX TODOs from the 2026-04-24 first-pull session — `mm resolve` prompt label jargon, pull summary missing inline conflicted filenames, `mm conflicts` table truncation, P0 `mm autopull`/`autopush` silent-mode contract regression on un-initialized machines, `_synced_scan_dirs` missing `include_files` sidecars on generic sources, "real merge" via `git merge-file` + opt-in Claude API for prose, and the load-bearing "invert conflict default" (local stays canonical, remote routes to `.sync-conflict-*`). All triaged into new Group 5 (Conflict UX & first-pull polish) — see ROADMAP.md. |
| 0.8.12  | 2026-04-24 | Docs: README install command corrected to `pip install -e .` from a local clone (project is not on PyPI; PyPI publish is a Future item). |
| 0.8.11  | 2026-04-24 | Group 4 / Track 4A — GitHub Actions CI workflow. Single `macos-latest` + Python 3.13 job runs `ruff check`, `ruff format --check`, `pytest tests/`, wheel build + install + `mm --version` smoke, and asserts the real Keychain backend loads (guards against silent `fail.Keyring` fallback). Ruff pinned at 0.15.12 in dev deps with `[tool.ruff]` config + `E/F/W/I` rule set (isort enforcement locks Group 3's import hoisting). One-time fix-drift commit swept 113 violations clean. README CI badge added. No path filter (avoids the branch-protection pending-forever footgun for path-skipped required checks). |
| 0.8.10  | 2026-04-24 | Group 3 — Test hygiene + style polish. Pre-flight (17 `backend: LocalBackend` hints in cli.py; 6 `Optional[X]` → `X \| None` + `Optional` dropped from typing import; 10 placeholderless f-strings stripped via AST audit; `crypto.py` keyring `except Exception` narrowed to `(KeyringError, ImportError)` + 7 regression pins in `test_crypto.py`). Track 3A (TestPushPullRoundTrip migrated from direct-API to `CliRunner.invoke`; new combined push→pull→conflict→tombstone E2E in test_integration.py; `test_deletion_propagation` renamed to `test_deletion_not_propagated_in_additive_model`; 86 lazy in-function imports hoisted across test_integration.py + test_conflict_copy.py). Codex adversarial during `/review` caught a P0 gap: `_auto_command_setup` + `_get_passphrase_or_exit` caught only `CryptoError`, so non-KeyringError propagation would have crashed uncaught — fixed in-line with 3 more regression pins before merge. 669 tests passing (+11 from baseline 658), zero behavior regressions. |
| 0.8.9   | 2026-04-24 | Docs: multi-machine usage guide (PR #27) — README explains that `mm` reads config from `~/.config/mind-meld/config.toml`, adds "Setting up a second (or third) Mac" bootstrap recipe, and documents three-way convergence (line-union merge for .jsonl/MEMORY.md, mtime-skip with `.sync-conflict-*` for other divergent files, tombstone-propagated deletions). Expanded "Syncing gstack" with default `include_dirs`/`include_files` lists, set-union merge behavior for `analytics/*.jsonl`, machine-local file enumeration (sessions/, builder-profile.jsonl), and the `sync.sources` wholesale-replacement caveat. No code or behavior changes. |
| 0.8.5   | 2026-04-24 | Track 1B (Group 1) — Walker + manifest + merge DRY: `_record_file` helper (collapses walk_claude_source + walk_generic_source per-file blocks), `_is_active_tombstone` helper (collapses generate_tombstones carry-forward + collect_tombstones aggregation), `_merge_strategy` + `_join_lines` helpers. Also drops redundant `normalize_manifest` call at manifest.py:607 — the call was positionally wrong (ran after the carry-forward loop had already consumed tombstone keys) and all three caller paths already satisfy the load-path invariant. `generate_tombstones` now enforces the caller contract at runtime: a v1-shaped dict (no `"sources"` key) raises `ManifestError` rather than silently producing zero tombstones. Two review passes landed corrections pre-merge: /plan-eng-review codex outside-voice (5 design corrections — `_record_file` signature taking `(path, base)`; direct `_merge_strategy -> Callable` instead of a registry; full-predicate `_is_active_tombstone` instead of parse-only helper; exact on_skip reason strings pinned in tests; Task 4 contract change owned with a test); /review cross-model adversarial (Claude + Codex independently confirmed a silent-delete-propagation regression on raw v1 input — fixed by promoting the documented contract to a runtime `ManifestError` guard). 10 new tests, 571 pass, zero regression against any v2-normalized caller. |
| 0.8.4   | 2026-04-23 | Group 2 pre-flight + Track 2A: storage-key helpers (`storage/keys.py` with path-traversal validation) + cli.py decomposition (`_pull_core` → 6 helpers + single print-owner, `_apply_incoming_file` → 3 per-outcome helpers). Internal refactor, zero user-visible behavior change; two codex-found regressions (`had_changes` exclusion of `unchanged`, per-file `blob_key` validation) caught and fixed pre-merge. |
| 0.8.3   | 2026-04-23 | Public-release prep: adds MIT `LICENSE` file (closes the gap where `pyproject.toml` declared `license = "MIT"` but the text wasn't shipped) and scrubs the placeholder `/Users/kb/` username out of SPEC.md, sync-gstack-context design doc, and one test fixture. No runtime behavior change. |
| 0.8.2   | 2026-04-23 | Track 1B (Group 1): manifest dead-code cleanup + v1-holdover removal (delete `walk_directory`/`build_manifest`, drop top-level `"files"` mirror, `diff_manifests` → `diff_files`, `DiffResult` → `@dataclass`) |
| 0.8.1   | 2026-04-23 | Track 1A (Group 1): cli.py surgical hardening (resolve exit-code propagation, conflict_filename ValueError on empty device_id, GC malformed-blob visibility, quiet-path audit for autopull/autopush recovery + no-sources + fsync failures, total_failed surfacing) |
| 0.8.0   | 2026-04-23 | Group 2 pre-flight + Track 2A: error-surface hardening (`_merge_manifests` tiebreak, `mm diag`, `mm init` two-tier guard, `mm recover --abandon-manifest`, `_error()` stderr routing, `list_devices` shape validation) |
| 0.7.1   | 2026-04-23 | Track 1B: config eager validation + legacy cleanup (bad `config.toml` now fails at load time with typed `ConfigError`) |
| 0.7.0   | 2026-04-23 | Track 1A: silent-failure cleanup in `autopull`/`autopush` + `--conflict-mode` unification (BREAKING: `mm pull --no-prompt` / `--resolve-interactive` removed) |
| 0.6.2   | 2026-04-23 | Track 1B: walker conflict-file exclusion + manifest read-path hardening (`load_manifest` boundary) |
| 0.6.1   | 2026-04-23 | Track 1D: storage layer hardening (`fsutil`, `fcntl.flock`, deferred-durability pull) |
| 0.6.0   | 2026-04-22 | Track 1C: crypto v2 — process-scoped `master_key` + HKDF-SHA256 per-file keys |
| 0.5.1   | 2026-04-22 | Corrupt-manifest recovery chain (sidecar → peers → refuse) + `mm --version` |
| 0.5.0   | 2026-04-22 | Rename memsync/msync → mind-meld/mm |
| 0.4.0   | 2026-04-21 | Conflict-copy preservation + mtime-skip for pull |
| 0.3.0   | (pre-v0.4) | Additive-only sync model with tombstones and conflict resolution |
| 0.2.0   | (pre-v0.3) | Multi-source sync with gstack support |
| 0.1.x   | (pre-v0.2) | iCloud-only backend, autopull/autopush for Claude Code; scoped sync to memory/todos |

## Current phase: v0.x → v1.0

The original cleanup-sweep set (Groups 1–5) shipped through v0.9.4. Five
post-cleanup releases shipped outside the original plan: v0.9.5
(auto-upgrade nudge), v0.9.6 (public-readiness scrub), v0.10.0
(per-machine source toggle), v0.10.1 (Group 7 preflight — security /
concurrency / correctness sweep), v0.11.1 (conflict-prompt UX
redesign — `(b)oth` → `(s)kip`, three-number divergence summary,
peer-controlled `device_name` sanitization extension, init-time
device-id collision detection, `safety.py` + `conflictdiff.py` extracted
from cli.py), and v0.11.2 (Group 7 hotfix — warn-once on `mm-events`
bootstrap failure so chmod-restricted homes don't spam `mm: warning:`
on every read-only command). Group 6 (release infrastructure polish —
GitHub Releases backfill) shipped 2026-04-27. Group 7 (mm-events
foundation — Tracks 7A `events.py` foundation + 7B `_push_core` wiring)
shipped through v0.10.2 / v0.10.3. **Group 8 (retro-fleet skill consumer) shipped as v0.11.0 on
2026-04-28** — closes the fleet-retro arc per
`docs/archive/fleet-retro.md`. The `v0.x → v1.0` plan from the
2026-04-22 `/full-review` audit is now complete. **Group 9** (post-v1.0
pull-performance + fresh-Mac onboarding polish) is the only in-flight
work.

See `docs/ROADMAP.md` for the structured Groups > Tracks > Tasks plan.

### Where we are

- **Correctness foundation:** ✅ shipped in v0.5.1–v0.6.2. Tri-state
  `ManifestFetch` recovery chain (v0.5.1), crypto v2 master_key + HKDF
  (v0.6.0), storage layer hardening via `fsutil` + `fcntl.flock` (v0.6.1),
  walker conflict-file exclusion + `load_manifest` read-path boundary
  (v0.6.2). `_merge_manifests` union + tombstones-newest-wins resolved via
  SPEC.md "Merge invariants" (the walker is lossy, so UNION is correct).
- **Error discipline:** ✅ shipped in v0.7.0–v0.7.1. Silent-failure cleanup
  in `autopull`/`autopush` + `--conflict-mode` unification (v0.7.0). Config
  eager validation + legacy cleanup — bad `config.toml` fails at load time
  with typed `ConfigError` (v0.7.1).
- **Post-v0.5.1 follow-ups:** ✅ shipped in v0.8.0. `_merge_manifests`
  tiebreak, `mm diag`, `mm init` two-tier guard, `mm recover
  --abandon-manifest`, `_error()` stderr routing, `list_devices` shape
  validation.
- **cli.py surgical hardening:** ✅ shipped in v0.8.1. `resolve --force`
  exit-code propagation, 16-char device_id in conflict filenames, GC
  malformed-blob visibility, quiet-path audit for autopull/autopush recovery
  + no-sources + fsync failures, `total_failed` surfacing.
- **Manifest dead code + v1 holdovers:** ✅ shipped in v0.8.2. Removed
  `walk_directory`/`build_manifest` backward-compat aliases, dropped top-level
  `"files"` mirror in v2 manifests, converted `DiffResult` to `@dataclass`,
  renamed `diff_manifests` → `diff_files`.
- **Storage-key helpers + cli.py decomposition:** ✅ shipped in v0.8.4.
  `storage/keys.py` (manifest_key/blob_key/device_key/parse_blob_key) with
  path-traversal validation at construction; `_pull_core` decomposed into
  `_select_devices`/`_prefetch_manifests`/`_preflight_conflicts`/
  `_pull_one_source`/`_fsync_touched_parents`/`_print_pull_summary`;
  `_apply_incoming_file` split into `_apply_write`/`_apply_merge`/
  `_apply_conflict`. Two codex-found regressions (had_changes excluding
  `unchanged`, per-file blob_key ValueError isolation) fixed before merge.
- **Roadmap slimmed for v1.0 (2026-04-23).** Groups 5/6/7 (selective sync,
  mtime hash cache, three-way merge base) moved to Future — all were labeled
  P2/P3 with no user demand, and mtime cache's motivating problem was
  already solved by crypto v2. Style nits collapsed into pre-flight. Three
  follow-up items from TODOS.md slotted into new Track 1C. Now 4 groups / 7
  tracks / 19 tasks (down from 8 groups / 12 tracks / 38 tasks).
- **Public-release prep:** ✅ shipped in v0.8.3. MIT `LICENSE` file added
  + `/Users/kb/` placeholder scrubbed from SPEC / design doc / test fixture.
- **Group 1 (Decomposition + DRY):** Tracks 1A ✅ v0.8.4, 1B ✅ v0.8.5, 1C
  ✅ v0.8.6 all shipped. Remaining: only the pre-flight `constants.py`
  extraction (CONFLICT_INFIX, CONFLICT_AGE_DAYS, TOMBSTONE_TTL_DAYS,
  FORMAT_VERSION) — storage-key helpers already shipped in v0.8.4.
- **Group 2 (Init flow + sync_log generalization + config polish):** ✓ Complete
  — Track 2A ✅ v0.8.7 (init decomposed into 4 helpers, `DEFAULT_SOURCES`
  reused, `write_sync_log` keyed off source type), Track 2B ✅ v0.8.8
  (backfill path preservation via `patch_config_on_disk`, ConfigError
  prefix rename `init:` → `config:` on non-init paths).
- **Group 3 (Test hygiene + style polish):** ✓ Complete — pre-flight + Track
  3A ✅ v0.8.10 (CliRunner migration, combined push/pull/conflict E2E, 86
  lazy-import hoists, type hints, `Optional[X]` → `X | None`,
  placeholderless f-string strip, keyring `except Exception` narrowed to
  `(KeyringError, ImportError)`). Codex `/review` caught a P0 keyring-
  propagation gap pre-merge; fix shipped with 3 regression pins.
- **Group 4 (Release infrastructure, GitHub Actions CI):** ✓ Complete —
  Track 4A ✅ v0.8.11 (single macos+py3.13 job: ruff check + format-check
  + pytest + wheel smoke + Keychain-backend load assert; ruff pinned
  0.15.12; 113-violation drift sweep; README badge).
- **Group 5 (Conflict UX & first-pull polish):** ✓ Complete.
  **Track 5A ✅ v0.8.15** (P0 autopull silent-mode contract regression +
  `_synced_scan_dirs` missing `include_files` sidecars + `_save_and_register`
  register-failure rollback) + Group 5 preflight (gstack `include_files`
  default add). **Track 5B ✅ v0.9.0 BREAKING** (`(c)`/`(f)` →
  `(l)/(r)/(b)/(a)` vocabulary unification, inline conflicted filenames in
  pull summary, `mm conflicts` table column rename + per-column wrap, Rich
  Progress widget for TTY, quiet-mode contract fix routing per-source
  conflicts to stderr; legacy `c`/`f` rejected loudly). **Track 5C ✅
  v0.9.1** (per-source `exclude_patterns` glob list, consumer-boundary
  filter at `_pull_core` + `_push_core`, tombstone-suppression invariant,
  `mm log` JSONL writer/reader, `mm migrate-config` command, missing-excludes
  visible-failure breadcrumb, `mm sources` excluded-count column; pivoted
  from "conflict inversion + real-merge" via /plan-ceo-review after the
  2026-04-24 first-pull data showed 24/25 divergent files were per-machine
  artifacts).
  **Track 5E ✅ v0.9.2 BREAKING** (inverted `_apply_conflict` so canonical
  = local; remote → sidecar; strict pull-start fleet-version refusal of
  pre-v0.9.2 peers; pre-inversion `v0-` filename migration; dual-mode
  resolve dispatch by filename prefix). **v0.9.3 hotfix patch**: added
  `config.yaml` to the gstack source's default `exclude_patterns` (gstack
  version-check tracking; existing installs need `mm migrate-config` to
  pick up). **Track 5D ✅ v0.9.4** (adversarial-review follow-ups
  hardening v0.8.15's Track 5A ship: `_find_conflict_files` tuple-key
  dedup; `_save_and_register` → `_register_and_save` order swap with
  best-effort cleanup so a SIGKILL/OOM/power-loss between writes leaves
  an inert orphan instead of an inverse half-state; new
  `_ensure_device_registered` push-time self-heal that retroactively
  fixes any pre-v0.9.4 victims of the v0.8.15..v0.9.3 half-state).
- **Auto-upgrade nudge:** ✅ shipped in v0.9.5. `mind_meld.upgrade` runs a
  leading-edge tag-based version check and emits a `mm: notice:` line
  nudging the fleet to upgrade. Approach A (nudge-only — user runs the
  printed pipx command); subprocess execution (Approach B) deferred per
  /plan-ceo-review. Three hook seams in cli.py: transition detection
  after each `load_config`, nudge emission at `_pull_core` / `_push_core`
  tail, status surfacing in `mm status`. Lock-order invariants pinned in
  CLAUDE.md. New `pullhistory` row class (`verb: "self-upgrade"` with
  `old_version`/`new_version`).
- **Public-readiness scrub:** ✅ shipped in v0.9.6. Final pass before
  flipping the repo to public.
- **Per-machine source toggle:** ✅ shipped in v0.10.0.
  `[sync].disabled_sources: list[str]` lists source names to skip on this
  device only (config.toml is per-machine, never synced). New CLI surface:
  `mm enable-source` / `mm disable-source` / `mm reconfigure-sources`.
  `_filter_disabled_sources` applies at TWO consumer-boundary call sites
  (mirrors `_filter_excluded_paths` shape from Track 5C). Tombstone-
  suppression invariant: disabling a source does NOT propagate fleet-wide
  deletion. New `seen_sources.py` module (mirrors `pullhistory.py`)
  tracks per-machine source acknowledgment with lazy-init seed (migration
  invariant: existing users don't see spurious "new source" hints on
  upgrade). `_prompt_source_toggle` extracted as the single source of
  truth for the per-source Y/N prompt copy + default rule.
- **Group 6 (Release infrastructure polish):** ✓ Complete — Track 6A
  shipped 2026-04-27. 36 GitHub Releases backfilled via `gh release create`,
  one per tag v0.1.0..v0.10.0. Bodies pulled from each `## [X.Y.Z]`
  CHANGELOG section; v0.9.2 carries its `— BREAKING` suffix into the
  release title; v0.8.10 (the one tag without a CHANGELOG entry) falls
  back to its tagged commit subject. v0.10.0 marked Latest. Unlocks the
  RSS feed, in-repo release-notes surfacing, and downloadable-asset UX.
  Zero source-code changes.
- **Group 7 (mm-events foundation, fleet-retro v0.11.0):** ✓ Complete.
  Per-device JSONL event log (`mm-push` + `git-snapshot` +
  `sessions-snapshot` events) written at `_push_core` HEAD position
  inside a hard time budget. New `mm-events` source on synced storage;
  `mm gc` reaps event files older than 90 days via tombstone propagation
  so deletion fans out fleet-wide on the next pull.
  **Pre-flight ✅ shipped in v0.10.1 (2026-04-27).** All 8 preflight items
  landed together: `safe_str()` / `safe_text()` peer-controlled string
  sanitization sweep (~30 print sites — closes the OSC 52 clipboard, CSI
  screen-clear, OSC title spoof vectors); pull-time case-collision
  detection on case-insensitive FS (non-invasive swapcase + samefile
  probe; per-cluster `mm: warning:`; consumer-side WRITE skipping only —
  manifest keys never globally case-normalized); `register_device`
  create-only via `put_exclusive` (fixes iCloud `.icloud` placeholder
  TOCTOU silent-bump of `registered:` timestamp); `update_last_seen`
  flock via `_devices_write_lock` (forward-defense for non-deterministic
  fields); `walk_generic_source` filesystem-identity dedup with
  deterministic pre-sort; `_find_conflict_files` key strengthened to
  `(src_name, st_dev, st_ino)`; `mm-events` default source +
  `MM_INTERNAL_SOURCE_NAMES` frozenset + `get_sources()` bootstrap;
  `src/mind_meld/skills/` placeholder subpackage.
  **Track 7A ✅ shipped in v0.10.2 (2026-04-28).** `src/mind_meld/events.py`
  foundation: six functions plus a TypedDict v=1 schema. URL canonicalization
  strips creds before they reach iCloud-synced JSONL; `discover_git_roots`
  multi-prober registry handles Conductor worktrees (where `.git` is a
  file); `walk_git_projects` enforces a hard wall-time budget via
  `as_completed(timeout=...)`; `walk_session_metadata` 2-level scandir tags
  Conductor workspaces as ephemeral by path-string match.
  **Track 7B ✅ shipped in v0.10.3 (2026-04-28).** `_run_events_tail` at the
  HEAD of `_push_core` (after device self-heal, before `build_manifest_v2`),
  same-push upload (no one-push lag), `mm-events`-resolved gate (covers
  fresh / migrated / un-migrated configs uniformly without a migration
  prompt), wall-clock budget 250ms autopush / 500ms interactive plumbed
  through to `walk_session_metadata`'s `deadline_monotonic` kwarg, dry-run
  no-op preserved, forensic-only failure breadcrumb. `_gc_old_event_files`
  reaps day files older than 90 days by parsing the filename date (NOT
  mtime — iCloud restores rewrite mtimes back to "now" while filename
  date is intrinsic). Fleet retention fans out via tombstone propagation.
  **Hotfix ✅ shipped in v0.11.2 (2026-04-29).** Warn-once cache via
  module-level `_BOOTSTRAP_WARNED_PATHS: set[str]` in
  `_bootstrap_mm_events_path`. First failure still surfaces `mm: warning:`
  (visible-failure contract preserved); subsequent `get_sources()` calls
  in the same process short-circuit silently. Closes the v0.10.1
  adversarial-review followup (read-only-command bootstrap warning spam).
- **Group 8 (retro-fleet skill consumer):** ✓ Complete — Track 8A
  shipped as v0.11.0 on 2026-04-28. Skill ships in the mm wheel under
  `src/mind_meld/skills/retro_fleet/`, symlinked to
  `~/.claude/skills/retro-fleet` at `mm init` and self-healed every push
  behind a 24h-TTL marker (5-branch state machine: target absent /
  correct symlink / dangling symlink / wrong target / OSError — covers
  pipx-reinstall recovery). The aggregator reads the synced event log
  across all devices, dedups commits via `(canonical_remote_url, sha)`,
  picks the latest sessions-snapshot per `(device, claude_dir)`, and
  renders gstack `/retro`-shaped markdown with locked output format owned
  by mm. Two surface companions: `mm devices --format=json` (stable
  schema for the skill's "M of N known devices" breadcrumb) and the
  `EVENTS_SCHEMA_VERSION` 1 → 2 bump (sessions-snapshot now FULL
  INVENTORY so latest-per-tuple is honest). Mixed-fleet handling
  surfaces `Sessions count incomplete: peer X on pre-v0.11.0` instead
  of overcounting.

### Version source of truth

`pyproject.toml` is the single source of truth for the release number.
`src/mind_meld/__init__.py` reads it at import time via
`importlib.metadata.version("mind-meld")` and falls back to `"0.0.0+dev"`
when the package is not installed (source-tree runs). There is no
separate `VERSION` file.
