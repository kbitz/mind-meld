# Changelog

All notable changes to Mind Meld will be documented in this file.

## [0.8.15.1] - 2026-04-25

Roadmap refresh. Freshness scan marks Track 5A ✓ Complete in place (shipped
v0.8.15 — 3 tasks + Group 5 preflight). Triage drains 2 `[review]` items
from TODOS.md (adversarial follow-ups to v0.8.15's Track 5A ship) into a new
**Track 5D**: `_find_conflict_files` double-count dedup when an `include_files`
entry sits inside an `include_dirs` directory (XS) + `_save_and_register`
crash-safety beyond Python exceptions to close the SIGKILL/OOM/power-loss
window left by v0.8.15's rollback (S-M). 5D ships next, before 5B/5C, so
v0.8.15's just-shipped surface hardens cleanly. PROGRESS.md "Where we are"
refreshed; `docs/designs/crypto-v2.md` archived to `docs/archive/` (referenced
v0.6.0, long-shipped). No code changes.

## [0.8.15] - 2026-04-24

Group 5 preflight + all of Track 5A bundled per `/plan-eng-review` D2.
Three Track 5A bug fixes ship together: the P0 `mm autopull` / `mm autopush`
silent-mode contract regression on un-initialized machines (root cause was
a binding-vs-attribute mismatch — `_auto_command_setup`'s `CONFIG_PATH.exists()`
preflight used cli.py's local import while `load_config()` reads the module
attribute, so monkeypatch divergence let the loud-on-malformed branch fire
on truly-missing configs); the `_synced_scan_dirs` scope bug where conflict
files for top-level `include_files` entries were invisible to `mm conflicts` /
`mm resolve` / `mm gc --conflicts` (kb-mbp 2026-04-24 first-pull saw 5 of 6
conflicts because `~/.gstack/config.sync-conflict-...yaml` lived at depth-0
outside the recursive scan); and `_save_and_register` now rolls back the
saved `config.toml` if `register_device` fails so init either fully succeeds
or leaves no local state — peers no longer see orphan `device_id`s with no
matching `devices/<id>.json` on storage. Group 5 preflight bundled in the
same release: gstack `DEFAULT_SOURCES.include_files` adds `retro-context.md`
and `greptile-history.md` for cross-machine memory continuity. Group 1's
unfinished `constants.py` extraction preflight was dropped after a cohesion
check (only 2 of 4 candidates are cross-module, would have split the
`FORMAT_VERSION`/`FORMAT_VERSION_LEGACY_V1` pair) — Group 1 marked ✓ Complete.
20 new tests (4 regressions pinned), 685 pass.

### Fixed
- `mm autopull` / `mm autopush` now exit silently on un-initialized machines
  again, restoring the documented quiet-on-no-config / loud-on-malformed
  contract. `_auto_command_setup` preflights `CONFIG_PATH.exists()` via
  module-attribute access (`mind_meld.config.CONFIG_PATH`) so monkeypatching
  the source module propagates and the test/production preflight stay in sync.
- `mm conflicts` / `mm resolve` / `mm gc --conflicts` now surface conflict
  copies on top-level `include_files` entries (e.g.
  `~/.gstack/config.sync-conflict-*.yaml`). `_find_conflict_files` adds a
  depth-0 sibling-glob path for generic sources, gated by `is_conflict_filename`'s
  strict pattern so user files like `notes.sync-conflict-log.md` stay filtered.
- `mm init` rolls back the saved `config.toml` if `register_device` fails
  so peers never see a device claimed in local config but missing from
  `devices/`. Original error propagates even if the rollback unlink itself
  fails (no masking).

### Added
- `retro-context.md` and `greptile-history.md` to gstack
  `DEFAULT_SOURCES.include_files` for cross-machine memory continuity.
- Curation comment on `include_files` documenting the three categories
  (gstack config, memory content, onboarding markers).
- 20 new tests across `test_config.py` (`TestDefaultSources`),
  `test_conflict_copy.py` (`TestFindConflictFilesIncludeFiles`),
  `test_integration.py` (breadcrumb assertions on silent-mode), and
  `test_track_2a.py` (`TestSaveAndRegister` rollback paths).

### Changed
- `docs/ROADMAP.md` — Group 1 marked ✓ Complete (constants.py preflight
  dropped with cohesion-check rationale); Group 5 preflight bundled with
  Track 5A; Execution Map updated.
- `_find_conflict_files` sibling-glob gates on `include_files` presence
  rather than `type == "generic"` so a future schema that adds
  `include_files` to other source types doesn't silently lose conflict
  visibility (defensive against the same scope-mismatch class of bug).
- `_save_and_register` rollback narrows `except Exception` to
  `(StorageError, OSError, MindMeldError)` so programming errors
  (AssertionError, AttributeError) propagate instead of silently
  destroying the user's saved config on every retry.
- All `cli.py` `CONFIG_PATH` access goes through `_config_module.CONFIG_PATH`
  uniformly. The local `from mind_meld.config import CONFIG_PATH` binding
  was removed; ~50 dead `monkeypatch.setattr("mind_meld.cli.CONFIG_PATH",
  ...)` lines across 9 test files dropped (they were the symptom of the
  dual-binding footgun, now retired).
- Rollback unlink-failure warning now goes to `stderr_console` per the
  CLAUDE.md visible-failure contract (load-bearing degradation signals
  reach stderr even in quiet mode).
- README.md, SPEC.md, and `docs/designs/sync-gstack-context.md` updated
  to list the two new gstack `include_files` defaults
  (`retro-context.md`, `greptile-history.md`).

## [0.8.14] - 2026-04-24

Roadmap tidy. Freshness scan marks Groups 2/3/4 ✓ Complete in place
(shipped through v0.8.7/v0.8.8/v0.8.10/v0.8.11) and collapses Track
1A/1B/1C detail to one-liners — only Group 1's preflight `constants.py`
extraction remains. Triages the eight conflict-UX TODOs from v0.8.13 (plus
two `[review]` items and one `[manual]`) into new Group 5 with three
serialized Tracks: 5A (auto-command + scope bugs incl. P0 autopull
silent-mode regression, ships first), 5B (resolve/conflicts/pull UX
relabel), 5C (conflict default inversion + real-merge backends, ships
last). One item — cross-device source rename drift — deferred to Future
as a documented known limitation. PROGRESS.md "Where we are" refreshed to
match.

### Changed
- `docs/ROADMAP.md` — Groups 2/3/4 marked ✓ Complete; new Group 5 with
  Tracks 5A/5B/5C + pre-flight; updated Execution Map.
- `docs/PROGRESS.md` — added v0.8.11/v0.8.12/v0.8.13/v0.8.14 rows;
  refreshed "Where we are" to reflect what shipped through v0.8.13.
- `docs/TODOS.md` — Unprocessed inbox drained; ten items routed to
  ROADMAP.md, one to Future.

## [0.8.13] - 2026-04-24

Backlog grooming. Captures eight follow-ups discovered during a real-world
first-pull session on a new Mac: a confirmed `[BUG]` in `_synced_scan_dirs`
where `mm conflicts` / `mm resolve` / `mm gc --conflicts` undercount on
`generic`-type sources because the function never scans top-level
`include_files` paths (reproduced via `~/.gstack/config.yaml` orphan
sidecar); a P0 `[BUG]` for the autopull/autopush silent-mode contract
regressing on un-initialized machines (test_integration failures); and
six UX TODOs around conflict resolution — invert the default so canonical
keeps local bytes, add real merge to `mm resolve` instead of pick-a-winner,
add progress output during the silent download loop, fix the `mm conflicts`
table truncation + jargon labels, relabel the resolve prompt from "(f)orce
conflict → canonical" to plain `(l)ocal / (r)emote`, and list conflicted
paths inline in the pull summary instead of forcing a second command.

### Added
- `docs/TODOS.md` — eight new entries in the Unprocessed inbox covering the
  conflict UX coherence theme, captured for a future implementing session.

## [0.8.12] - 2026-04-24

Docs fix. The README and SPEC both told users to `pipx install mind-meld`,
but the package isn't on PyPI — that command 404s. Swap to
`pipx install git+https://github.com/kbitz/mind-meld.git`, which actually
works today, and add a one-liner noting the package isn't on PyPI.

### Fixed
- README.md install + second-Mac setup commands now use the GitHub URL
- SPEC.md "30-second install" reference matches reality

## [0.8.11] - 2026-04-24

Group 4 — Release infrastructure. Adds GitHub Actions CI on every push to
main and every PR. Single job on `macos-latest` + Python 3.13 — mind-meld
is a macOS tool, so a multi-OS + multi-Python matrix is theater for this
project. The job runs `ruff check`, `ruff format --check`, `pytest tests/`,
and a wheel build + install + `mm --version` smoke. Also asserts the real
Keychain backend loads (guards against silent `fail.Keyring` fallback). No
path filter (avoids the GitHub branch-protection pending-forever footgun).
669 tests passing, ruff clean, zero behavior regressions.

### Added
- `.github/workflows/ci.yml` — single job on `macos-latest` + Python 3.13,
  pip cache via `actions/setup-python@v6`, `timeout-minutes: 20`,
  `permissions: contents: read`, concurrency cancel-in-progress keyed on
  PR number
- Keyring backend assert-smoke: `python -c "import keyring; b =
  str(keyring.get_keyring()); assert 'macOS' in b or 'Keychain' in b;
  print(b)"` — fails loudly if the `fail.Keyring` fallback loads
- Installed-artifact wheel smoke: `python -m build` then `pip install
  --force-reinstall dist/*.whl` then `mm --version`
- CI status badge in README.md (top of file)
- `ruff` (exact-pinned to `0.15.12`) in `dev` optional-dependencies
- `[tool.ruff]` config in pyproject.toml: `line-length = 100`,
  `target-version = "py311"`, rule selection E/F/W/I (isort enforcement
  locks in Group 3's import hoisting)

### Changed
- docs/ROADMAP.md Track 4A section — rewrites the baseline spec to match
  what shipped (filename `ci.yml` not `test.yml`, 5-cell matrix with
  macos-3.12 exclude, ruff lint job, macOS smoke tests, no path filter).
  Deletes the stale `macOS keyring tests need stubbing or skipping on
  Linux runners` note that was already solved by `tests/conftest.py`'s
  autouse keyring stub
- Auto-formatted 28 Python files under `src/` and `tests/` via `ruff
  format` (89 imports reordered by isort, 29 unused imports removed, 9
  unused local variables deleted, 1 line-too-long in `manifest.py` ASCII
  diagram trimmed, 1 `noqa: E402` on the deliberate post-sys.path import
  in `tests/benchmarks/test_kdf_timing.py`, 1 `l` → `line` rename). All
  pure style — zero behavior changes; 669 tests pass unchanged



Group 3 — Test hygiene + style polish. Closes the pre-1.0 cleanup Group
dedicated to CLI-driven end-to-end coverage + lint polish. Pre-flight
items migrate cli.py style to the same shape as devices.py (typed
`backend`, `X | None` everywhere, no dead `f` prefixes) and narrow the
keyring exception catch so non-KeyringError failures stop hiding behind
the env-var fallback. Track 3A migrates `TestPushPullRoundTrip` from
direct-API bypass to `CliRunner.invoke`, adds a combined
push → pull → conflict → tombstone end-to-end test, renames a misleading
test, and hoists 86 lazy in-function imports to module level. 669 tests
passing (was 658), zero behavior regressions. Scope confirmed through
`/plan-eng-review` (2026-04-24) — 3 decisions approved. Codex
adversarial pass during `/review` caught a P0 gap in the keyring
narrowing: the hook wrapper and interactive-command helper both
catch only `CryptoError`, so non-KeyringError propagation would have
crashed uncaught. Fixed in-line with 3 more regression pins before
merge. One TODO captured for later (ruff F541/PYI041 enforcement).

### Added
- 5 `TestGetPassphraseExceptNarrow` regression pins + 2
  `TestStorePassphraseInKeyringExceptNarrow` pins in `test_crypto.py`:
  locks the new catch-set contract. KeyringError + ImportError caught;
  OSError + RuntimeError propagate. Happy-path sanity pin.
- 3 `TestAutoCommands` regression pins for the keyring-propagation
  follow-through (hook breadcrumb outcome, interactive-command stderr
  banner, init graceful-degradation). These pin the boundary behavior
  Codex caught: non-KeyringError exceptions must become visible
  failures, not uncaught tracebacks.
- `test_integration.py::TestPushPullRoundTrip::test_push_pull_conflict_tombstone_combined`:
  new CliRunner E2E walking push → pull → divergent-edit conflict-copy →
  delete + tombstone propagation in a single run. Exercises the
  interaction surface that isolated test_conflict_copy.py and
  test_additive_sync.py didn't cover together.
- `backend: LocalBackend` type hints on 17 cli.py helpers (matches
  `devices.py`'s existing pattern).

### Changed
- `crypto.get_passphrase` and `crypto.store_passphrase_in_keyring`:
  narrowed `except Exception` to `(keyring.errors.KeyringError,
  ImportError)` via try/except/else split. Non-KeyringError failures
  (OSError, RuntimeError, DBus surprises on Linux) now propagate to the
  caller. The three call sites were hardened in this same release so
  the propagation lands in the right place:
  - `_auto_command_setup` (autopull/autopush hook wrapper) gained an
    `except Exception` guard that writes a new `keyring-error`
    breadcrumb outcome and emits `mm: <verb> failed - keyring error`
    to stderr. Honors the v0.8.1 visible-failure contract for hook
    paths; without this the narrowing would have crashed the hook
    uncaught (Codex adversarial review flagged this gap).
  - `_get_passphrase_or_exit` (interactive commands — push, pull, diff,
    gc, recover, resolve) routes non-CryptoError exceptions through
    `_error()` with a `keyring backend failure: <type>: <msg>` banner
    instead of a raw traceback.
  - `_save_and_register` (init) wraps the keyring-write call so a
    post-config-save keyring failure degrades gracefully to the
    env-var-fallback path (yellow warning) rather than leaving the
    user half-initialized.
- `TestPushPullRoundTrip` (test_integration.py): migrated from direct
  `build_manifest_v2` / `encrypt` / `storage.put` wiring to `CliRunner`
  invocations of `mm push` and `mm pull`. Now exercises `_pull_core` →
  `_apply_incoming_file` for real, which is the only path production
  traverses.
- `test_deletion_propagation` → `test_deletion_not_propagated_in_additive_model`:
  body always asserted the additive behavior; name was a pre-additive
  holdover.
- Style cleanup in cli.py: 6 `Optional[X]` → `X | None` (dropped
  `Optional` from the typing import); 10 placeholderless f-strings
  stripped of their `f` prefix (AST-verified; no adjacent-concat groups
  lost interpolation).
- Hoisted 52 in-function imports in `tests/test_integration.py` and 34
  in `tests/test_conflict_copy.py` to module-level (Path, shutil,
  tomllib, hashlib, subprocess, sys, textwrap, typer, and a dozen
  `mind_meld.*` modules). One existing alias `_json` renamed to `json`
  and one `_crypto` renamed to `crypto_module` in-place.
- `test_env_var_fallback` (test_crypto.py) updated: the pre-narrow
  strategy of `delattr("keyring.get_password")` triggered
  `AttributeError` which is no longer swallowed. Now uses a realistic
  `NoKeyringError` raise.

### Fixed
- Two separate lazy imports of `LocalBackend` inside cli.py
  (`init` + `diag`) removed; hoisted to the module-level import alongside
  `storage.keys`. Same symbol, no import-time cost change (already
  transitively loaded).

## [0.8.9] - 2026-04-24

Docs: multi-machine usage guide. README now explains that `mm` reads its config
from `~/.config/mind-meld/config.toml` (install anywhere, run from anywhere) and
adds a "Setting up a second (or third) Mac" section with the bootstrap recipe:
install, `mm init` with the same passphrase, push, pull. Documents the
three-way convergence guarantee — `.jsonl` and `MEMORY.md` line-union merge,
other divergent files use mtime-skip with `.sync-conflict-*` preservation, and
deletions propagate via tombstones — so users know divergent-state first-runs
are safe.

Expanded "Syncing gstack" with the concrete default `include_dirs` /
`include_files` lists, explicit note that `analytics/*.jsonl` and
`projects/<slug>/*.jsonl` set-union merge (why `/retro global` converges across
Macs), the files that are intentionally machine-local (`sessions/`, `builder-profile.jsonl`),
and a TOML snippet for extending `include_files` — with the load-bearing caveat
that `sync.sources` replaces defaults wholesale.

No code or behavior changes. Companion TODOS entry proposes adding
`retro-context.md` and `greptile-history.md` to default gstack `include_files`
so richer retro inputs sync automatically.

### Changed
- `README.md` — new "Setting up a second (or third) Mac" section, expanded
  "Syncing gstack" block with default enumeration and custom-config snippet.

## [0.8.8] - 2026-04-24

Track 2B: Config polish — eng-review follow-ups. Stops `mm` from silently
rewriting your hand-edited `config.toml` paths on first-run-after-upgrade.
If you wrote `storage.path = "~/Library/Mobile Documents/..."` or pointed
at a symlinked storage root, the path now survives crypto-init backfill
verbatim instead of being canonicalized to the resolved absolute form.

Scope refined through `/plan-eng-review` + Codex outside-voice challenge
(2026-04-24). Original plan removed path-mutation from `_apply_defaults`
across six downstream readers; Codex flagged that as over-correction for
a single-writer bug. Narrowed to the actual leak site: the backfill
`save_config` call inside `_init_crypto_session`. One new helper, one
call-site swap, two prefix renames, 37 new tests (21 unit + 4 CLI
integration regressions + 12 from `/review` auto-fixes and follow-ups).

### Added
- `config.patch_config_on_disk(updates, path=None)` — re-reads raw TOML,
  shallow-merges `updates` per field within each section, saves. Bypasses
  `_validate` / `_apply_defaults` by design because the whole point is to
  preserve user-authored text for fields outside the patch. Narrow contract:
  only for partial patches; full writes still go through `save_config`.
  Raises `ConfigError` on missing / malformed TOML / non-table section.
- `tests/test_config.py::TestUpdateConfigOnDisk` (11 tests) — pins the
  helper's contract: tilde paths preserved, symlinks preserved, legacy
  `sync.claude_dir` preserved, sources array preserved, multi-section
  patches merge independently, field overwrites don't disturb siblings,
  missing file / malformed TOML raise `ConfigError`.
- `tests/test_config.py::TestConfigErrorPrefixes` (3 tests) — pins the
  rename: `init:` stays on the missing-file branch (correctly points at
  `mm init`), `config:` on the parse-error and generic-wrap branches.
- `tests/test_integration.py::TestBackfillPreservesRawPaths` (4 tests) —
  CLI-level regressions via `CliRunner`. Headline test writes a config
  with tilde-form paths, runs `mm autopush`, re-reads raw TOML bytes, and
  asserts `storage.path`, `sync.claude_dir`, and `sources[*].path` are
  unchanged. Also covers symlink preservation, push idempotency (second
  push must not rewrite), and graceful degradation when the config file
  disappears between load and backfill.

### Changed
- `_init_crypto_session` (cli.py): the first-run-after-upgrade backfill
  of `crypto.root_salt_fp` + `crypto.argon2_memory_kb` now calls
  `patch_config_on_disk` instead of `save_config(config)`. `_apply_defaults`
  still canonicalizes paths in memory — that's consumed by six downstream
  readers and shouldn't change. Only the on-disk persistence is narrowed.
- Error prefixes: `init: failed to parse` → `config: failed to parse`,
  `init: failed to load` → `config: failed to load`. `init: config not
  found` stays because that branch genuinely tells the user to run
  `mm init`. These fire on every command that calls `_get_config`
  (push, pull, status, diag, recover), not just init.
- `ConfigError` from `patch_config_on_disk` now emits
  `mm: warning: backfill skipped — <error>` to stderr per the v0.8.1
  visible-failure contract for data-at-risk signals. `OSError` stays
  silently swallowed (transient permission issues).

### Fixed
- First-run-after-upgrade silently rewriting hand-edited config paths to
  their canonical resolved forms (Codex flagged during /plan-eng-review
  2026-04-23; v0.7.1's `.resolve()` addition had extended the footgun to
  symlink dereference).

## [0.8.7] - 2026-04-24

Track 2A: Init decomposition + DEFAULT_SOURCES reuse + sync_log generalization.
Five tasks — refined by `/plan-eng-review 2026-04-24` before landing. The review
caught one critical implementation bug in the as-written roadmap (task 4 would
have NameError'd at runtime because `_pull_one_source` had no `src_cfg` handle
to key off `type`) plus several smaller issues. Delivered with 84 new unit
tests across `test_track_2a.py` (init helpers + type-keyed sync log regression
pins) and a fresh `test_synclog.py` (15 tests). Full suite at 639 passing.

### Added
- `config.get_default_source(name) -> dict | None` — returns a deep copy of the
  `DEFAULT_SOURCES` entry matching `name`. Deep-copy guards against aliasing
  pollution when callers mutate the returned dict (inserting it into a user's
  config). Returns None for unknown names.
- `cli.py` init helpers (decomposition of the 213-line `init()` command):
  - `_load_prior_device_metadata() -> tuple[str | None, str | None]` — best-
    effort read of the prior device `(id, name)` from any existing config.
    Malformed or missing config returns `(None, None)` — the orphan-case
    warning just loses the descriptive name.
  - `_prompt_passphrase(is_first_device: bool) -> str` — double-prompts (with
    confirm) on first-device, single-prompts otherwise. Exits via `_error` on
    empty input or mismatch.
  - `_bootstrap_or_verify_crypto(backend, passphrase, is_first_device, fetch) ->
    tuple[bytes, int, bytes]` — owns the first-device-happy-path +
    lost-bootstrap-race-falls-through-to-verify + second-device-verify branches
    that were inline in `init()`. Sets the crypto session as a side effect.
  - `_prompt_sources() -> list[dict]` — loops over `DEFAULT_SOURCES`, Y/n-
    prompts each with `default=Y` iff the source's path exists on disk.
    Returns only enabled entries, deep-copied via `get_default_source()`.
  - `_save_and_register(config, backend, device_id, device_name, passphrase)`
    — persists config → registers device → stores passphrase in keyring.
    Order matters: if config write fails, device is NOT registered and
    keyring does NOT hold an invalid secret.
- `_PerSourceResult.claude_sync_base` is now gated on `src_type == "claude"`
  inside `_pull_one_source` (not `src_name == "claude"`). New `src_type: str`
  keyword-only parameter on `_pull_one_source` carries the local config's
  `type` field through the pull pipeline. `local_sources_map` in `_pull_core`
  widened from `dict[str, Path]` to `dict[str, dict]` carrying both `path`
  (expanded Path) and `type` (str) per source.
- `tests/test_synclog.py` (15 tests, NEW) — direct unit tests for
  `write_sync_log` covering path expansion, `projects/` absence → empty
  return, per-project grouping, all 5 change categories (new / modified /
  deleted / conflicted / skipped), empty-bucket suppression, metadata
  emission, and a regression pin that the old `claude_dir=` kwarg raises
  TypeError (stale callers fail loudly, not silently to the wrong location).
- 28 new tests in `tests/test_track_2a.py` covering the init helpers,
  `_prompt_sources` aliasing guard, `_save_and_register` ordering, the
  `_bootstrap_or_verify_crypto` lost-race branch, and two critical regression
  pins for the type-keyed sync-log gate:
  - `test_renamed_claude_source_still_logs` — renamed `"my-claude"` source
    with `type="claude"` must still set `claude_sync_base` (pre-fix: silently
    broke the sync log for anyone who customized source names).
  - `test_claude_named_generic_does_not_log` — symmetric pin: a source
    named `"claude"` but typed `"generic"` must NOT log.
- `tests/test_integration.py::TestInitFlow` — 3 new integration tests:
  `test_refuses_if_no_sources_enabled`, `test_first_device_gstack_only_init`,
  `test_first_device_both_sources_init`. The gstack-only test pins that
  `DEFAULT_SOURCES`'s `include_dirs` / `include_files` survive the indirection
  through `get_default_source()`.

### Changed
- **BREAKING (CLI-only):** `mm init` now prompts per source type (claude Y/n,
  gstack Y/n) instead of writing `sync.claude_dir = "~/.claude"`
  unconditionally. Default is Y when the source's path exists on disk, N
  otherwise. `init` refuses to finish (exit non-zero) if the user declines
  every source — a config with zero sources left push/pull silently no-op'ing.
  Existing configs are unaffected (`get_sources()` still reads the legacy
  `sync.claude_dir` field as a fallback).
- `cli.py::init()` body shrank from ~213 lines of inline logic to a ~60-line
  orchestration of the five helpers above.
- `cli.py::_pull_one_source` signature adds keyword-only `src_type: str`
  parameter. Required for the type-keyed sync-log gate.
- `cli.py::_preflight_conflicts` parameter `local_sources_map` retyped from
  `dict[str, Path]` to `dict[str, dict[str, Any]]` to match the widened map
  shape in `_pull_core`.
- `synclog.py::write_sync_log` first parameter renamed `claude_dir` →
  `claude_base`. The function is claude-specific (hardcodes the `projects/`
  subdirectory layout); the new name tells the truth — "the on-disk root of
  a claude-type source" — without implying the function generalizes to
  non-claude sources. Docstring explicitly documents the claude-only
  semantic and that the caller owns the type-gate.
- `config.DEFAULT_CLAUDE_DIR` reshaped from the expanded
  `str(Path.home() / ".claude")` to the literal `"~/.claude"`. Matches the
  TOML-round-trip convention `get_sources()` already uses and unblocks
  Track 2B ("stop mutating config in `_apply_defaults`").
- `init()` now reuses `DEFAULT_MAX_FILE_SIZE`, `DEFAULT_ARGON2_MEMORY_KB`,
  and the `DEFAULT_SOURCES` gstack entry (via `get_default_source`) instead
  of re-inlining the hardcoded constants and dict. Previously `init` had a
  ~20-line verbatim copy of the gstack source definition that would drift
  from `config.DEFAULT_SOURCES` if one side updated.

### Deprecated
- The `claude_dir=` keyword argument to `write_sync_log` is removed (not
  aliased). Any out-of-tree caller using the old name now raises
  TypeError at call time — intentional: a silent alias would let a
  stale caller write to the wrong location without noticing. Test
  `tests/test_synclog.py::TestParamRenameRegression::test_old_kwarg_name_rejected`
  pins the loud-failure contract.

## [0.8.6] - 2026-04-24

Track 1C: Post-1A cli.py follow-ups. Three low-risk polish items cleaning up
Track 1A's landing — a shared push/status/diff iterator, stricter safety on
garbage-collected blob paths, and a persistent `degraded` signal on the
autopull breadcrumb. All three tasks went through `/plan-eng-review` with
Codex outside-voice challenge, `/review` with testing + maintainability
specialists, and a fresh Codex adversarial pass before landing. 16 new
tests in `test_track_1c.py`, full suite at 592 passing. Shipped as 0.8.6
because Track 1B (Group 1 walker/manifest/merge DRY) landed as 0.8.5 while
1C was in review.

### Added
- `iter_source_diffs(local_manifest, remote_sources, *, source_filter, skip_unchanged)`
  in `cli.py` — shared generator consolidating the 3-line per-source diff
  boilerplate that lived at 3 call sites (`_push_core`, `status`, `diff_cmd`).
  Pull path intentionally does not use the helper (it calls `diff_files` with
  arguments swapped — see `diff_files` docstring).
- `PullResult.durability_fsync_failures: int` and `PullResult.corrupt_peer_count: int`
  — degradation-signal counters populated from `_pull_core`'s finally block.
  Exposed so `autopull` can persist a "degraded" breadcrumb outcome instead
  of hiding data-at-risk conditions behind `success`.
- `mm autopull` now writes `outcome: "degraded"` to the breadcrumb (readable
  via `mm status` and `mm diag`) when any of four signals fire: fsync failure
  on a touched parent dir, corrupt peer manifest(s), unknown source(s) from a
  peer, or per-file apply failure(s). `detail` enumerates every firing signal
  joined with `"; "`. Mirrors the v0.8.1 `no-sources` breadcrumb precedent —
  stderr warnings surface immediately; the breadcrumb makes the degradation
  state persistent for monitoring. Previously `outcome` stayed `success` for
  degradation cases, making `mm status`-based monitoring selectively honest.
- `tests/conftest.py` — canonical home for shared CLI-integration helpers
  (`_make_config`, `_populate_claude`, `_redirect_sidecar`, `_redirect_lock`,
  `_setup_real_config`, `PASSPHRASE`, `MEMORY_KB`). Previously lived in
  `test_track_1a.py` and were cross-imported; now imported from conftest by
  both `test_track_1a.py` and `test_track_1c.py`.
- `tests/test_track_1c.py` (16 tests) — 7 unit tests for `iter_source_diffs`,
  3 for PullResult degradation fields, 5 for every combination of the 4
  degraded-breadcrumb signals (including a deterministic stub that pins the
  `"; "` join delimiter when all four signals fire together), and a REG-1
  integration test proving `mm gc` never reaps a non-hex-sha blob.
- Sha hex-shape validation in `tests/test_preflight.py` (11 new parametrized
  cases) plus a REG-1 pin: `parse_blob_key("data/dev1/not-a-sha.enc") is None`.

### Changed
- `src/mind_meld/storage/keys.py`: `blob_key(device_id, sha)` now validates
  that `sha` matches `[0-9a-f]{64}` (fullmatch). `parse_blob_key(key)` now
  returns `None` when the leaf sha is non-hex, routing malformed blob paths
  through `_do_gc`'s `malformed_count` path (skipped, never reaped as
  "orphans"). `device_id` remains lax per Codex-surfaced fixture-compat
  audit: production IDs are `uuid4().hex[:8]` but 22 test fixtures and
  historical installs use short non-hex IDs like `dev-a`, `mac-a`.
- `_validate_hex_sha()` converts non-string input (corrupt manifest shipping
  `{"sha256": null}` or numeric) to `ValueError` via an `isinstance` guard,
  so `_download_and_apply`'s `except ValueError` catches it and the pull
  continues per-file instead of crashing on a `TypeError` escape.

### Fixed
- `mm gc` safety: a 3-segment `data/{device}/{non-hex-leaf}.enc` path was
  previously parsed by `parse_blob_key` and then reaped as an "orphan" by
  `_do_gc` since its non-hex leaf could never be in `referenced_hashes`. Now
  the leaf is hex-validated at parse time and the file is routed through
  `malformed_count` (preserved, surfaced in verbose output).
- Autopull breadcrumb selective-honesty: `mm status` would show
  `Last auto-pull: ... (success)` indefinitely after a pull that experienced
  fsync-durability failure, corrupt peer manifest, unknown-source partition,
  or per-file apply failure. All four now flow into `outcome: "degraded"`.

### Known limitations (not addressed in 1C, documented for future tracks)
- `corrupt_peer_count` is only incremented for peers discovered via
  `_list_devices_warn()`. A corrupt or missing `devices/*.json` entry hides
  a peer entirely, so its bad manifest never surfaces in the degradation
  signal. Tracked as "Blob-directory as secondary peer-discovery path" TODO.

## [0.8.5] - 2026-04-24

Track 1B (Group 1) — Walker + manifest + merge DRY. Three private helpers
collapse duplicated logic in `manifest.py` and `merge.py`, plus a load-bearing
contract change on `generate_tombstones`. Zero user-visible behavior change
for any caller that routes input through `load_manifest` (every internal
caller does). Two rounds of adversarial review landed corrections pre-merge:
`/plan-eng-review` with Codex outside voice caught 5 design misses (including
the full-predicate `_is_active_tombstone` vs a parse-only helper, and the
direct `_merge_strategy -> Callable` vs an over-engineered registry);
`/review` with Claude + Codex cross-model adversarial caught a silent
delete-propagation regression on v1-shaped input that made the shipped
contract *enforced at runtime* instead of documented in a docstring.

### Added
- `_record_file(path, base, max_file_size, on_skip) -> tuple[str, dict] | None`
  in manifest.py. Single source of truth for the per-file walker pipeline
  (exclude check → stat → size cap → hash → record mtime/size/sha). Exact
  `on_skip(rel, reason)` strings are now pinned by tests — they surface in
  cli.py verbose walker output so their shape is load-bearing user contract.
  Both `walk_claude_source` and `walk_generic_source` collapse to 3-line
  per-file loops.
- `_is_active_tombstone(info, cutoff) -> bool` in manifest.py. Full predicate
  covering the fromisoformat + tzinfo-None → UTC guard + cutoff compare +
  `(ValueError, TypeError)` fallthrough. `generate_tombstones` (carry-forward)
  and `collect_tombstones` (fleet aggregation) both collapse their 9-line
  duplicated blocks to a single 2-line guard. The naive-datetime → UTC
  repair is the load-bearing bit — prevents a TypeError crash when an older
  client wrote a timezone-naive `deleted_at`.
- `_merge_strategy(rel_path) -> Callable | None` and
  `_join_lines(lines) -> bytes` in merge.py. `should_merge` / `merge_file`
  share a single dispatch predicate; `merge_jsonl` / `merge_lines` share the
  UTF-8 join tail. No behavior change; `.jsonl` still takes precedence over
  `MEMORY.md` basename check.
- Ten new tests in `tests/test_manifest.py`: `TestRecordFile` (happy path,
  silent exclusion, stat PermissionError, size-cap exact reason format,
  hash OSError), `TestIsActiveTombstone` (tz-aware + tz-naive active, expired,
  unparseable), `TestGenerateTombstonesContract` (raises on v1-shaped input,
  allows None).

### Changed
- `generate_tombstones` now raises `ManifestError` at entry if `remote_manifest`
  is non-None and lacks a top-level `"sources"` key. Previously it silently
  produced zero new tombstones on v1-shaped input (the positionally-broken
  `normalize_manifest` call at line 607 had been doing in-line v1 → v2
  promotion right before new-tombstone detection). Dropping the call without
  a runtime guard would have turned that into silent delete-propagation loss
  for any future caller that bypassed `load_manifest`. Cross-model adversarial
  review (Claude + Codex independently) reproduced the regression with
  `generate_tombstones(local, raw_v1_remote, 'dev') == {}`; the fix converts
  the failure mode from silent-loss to loud-fail. Every internal caller
  (`_fetch_remote_manifest` → `load_manifest`; `sidecar.read` → explicit
  normalize; peer-fallback synthetic dict → v2-shaped by construction)
  complies with the enforced contract.

### Removed
- Redundant `normalize_manifest(remote_manifest)` call at manifest.py:607.
  It was positionally wrong (ran AFTER the carry-forward loop had already
  consumed tombstone keys using whatever shape was there) and mutated the
  caller's dict as a side effect. Replaced by the runtime contract guard
  described above.

### Fixed
- `from typing import Callable` → `from collections.abc import Callable` in
  `merge.py`. PEP 585 modernization; no runtime effect under
  `from __future__ import annotations`. Caught by `/review` as a free
  modernization adjacent to the new helper.
- `test_stat_permission_error_emits_on_skip` in `tests/test_manifest.py`
  now scopes its `Path.stat` monkeypatch to the target file only. Previously
  patched globally — would silently mis-attribute coverage if `_record_file`
  ever gained a second stat call (symlink sniff, etc.).

## [0.8.4] - 2026-04-23

Group 2 pre-flight + Track 2A: storage-key helpers and CLI decomposition.
Extracts every storage-key string construction behind a typed helper module,
splits the 393-line `_pull_core` into six focused helpers plus a single
print-owner, and splits the 125-line `_apply_incoming_file` dispatcher into
three per-outcome helpers. Internal refactor — zero user-visible behavior
change. Two codex-found regressions in the initial decomp were caught and
fixed before merge (see Fixed).

### Added
- `src/mind_meld/storage/keys.py` — pure string-construction helpers for every
  storage key used in the repo: `manifest_key(device_id)`,
  `blob_key(device_id, sha)`, `device_key(device_id)`, `parse_blob_key(key)`
  (depth-only parser for `mm gc`), plus `MANIFESTS_PREFIX` / `DATA_PREFIX` /
  `DEVICES_PREFIX` constants and re-exported `CRYPTO_INIT_KEY`. Validates path
  components at construction time — rejects empty, `"."`, `".."`, `/`, `\`,
  null bytes — so a corrupt or malicious peer manifest can't smuggle a
  `sha256: "../../../etc/passwd"` through `backend.get`.
- `_select_devices`, `_prefetch_manifests`, `_preflight_conflicts`,
  `_pull_one_source`, `_fsync_touched_parents`, `_print_pull_summary` helpers
  in cli.py. Each returns structured data (dataclasses for peer warnings,
  predicted conflicts, fsync warnings, per-source results). The dispatcher
  reads as ~50 lines of orchestration + one `_print_pull_summary` call.
- `_apply_write`, `_apply_merge`, `_apply_conflict` helpers in cli.py. The
  `_apply_incoming_file` dispatcher shrank from 125 to ~50 LOC.
- `tests/test_preflight.py` (47 tests) — storage-key helpers including
  path-traversal rejection for all three constructors.
- `tests/test_track_2a.py` (38 tests) — unit pins for each extracted helper,
  load-bearing stderr contracts (corrupt peer, unknown source, fsync failure
  surviving quiet mode), and the two codex-found regression pins.

### Changed
- `_list_devices_warn` is called exactly once per pull (was twice). One
  warning per dropped peer per pull is the correct semantic; the pre-refactor
  double-print was a bug.
- `write_sync_log` and `_cleanup_conflict_copies` are now best-effort in
  `_pull_core` — wrapped in `try/except (OSError, StorageError)` that logs
  to stderr and continues. Pull's outer loop is wrapped in `try/finally` so
  accumulated corrupt-peer / unknown-source / fsync warnings reach stderr
  even if an unexpected exception propagates. Preserves the v0.8.1
  visible-failure contract under partial-pull conditions.
- `_predict_pull_outcome` return vocabulary unchanged (`write` / `merge` /
  `skip` / `conflict` / `unchanged`). Codex adversarial plan review flagged
  the originally-planned past-tense rename as a worse abstraction; reversed.
- `CONFLICT_AGE_DAYS` stays in cli.py (where `mm gc --conflicts` lives).
  Roadmap proposed moving to manifest.py; codex flagged as module-boundary
  mistake; reversed.
- `crypto.py` re-exports `CRYPTO_INIT_KEY` from `storage.keys` for test
  compatibility; constant moved to complete the storage-keys boundary.
- `devices.py` uses `device_key(device_id)` and `DEVICES_PREFIX` helpers.
- Four-line pattern comment above `_pull_core` documents the "helpers
  return data, `_print_pull_summary` owns user-visible output" pattern, and
  flags the style split with the rest of cli.py (push, status, diag,
  recover still use side-effect-during-logic; migrate opportunistically).

### Fixed
- `_PerSourceResult.had_changes` excludes `"unchanged"` outcomes from the
  "device had changes" signal. Found by Codex adversarial review: a
  stale-diff TOCTOU where `_download_and_apply` returned only `unchanged`
  would trigger `_cleanup_conflict_copies`, which deletes iCloud conflict
  copies of the remote manifest. In the recovery scenario where a peer's
  canonical manifest is corrupt and `_fetch_remote_manifest` recovered via
  a valid conflict copy, that cleanup would delete the only good copy,
  leaving only the corrupt canonical and permanent corruption for future
  pulls. Pre-refactor `src_written + src_merged + src_conflicted +
  src_skipped + src_failed > 0` correctly excluded `unchanged`; the
  property-based rewrite regressed this. Regression test locks in the
  exclusion.
- Per-file blob-key validation in `_download_and_apply`: a `ValueError`
  from `blob_key(source_device_id, info["sha256"])` is caught and mapped
  to the `"failed"` outcome, preserving per-file isolation (matches the
  v0.8.1 empty-device_id handling in `_apply_conflict`).

## [0.8.3] - 2026-04-23

Prep pass for public release: adds the MIT `LICENSE` file that `pyproject.toml`
already declares, and scrubs the placeholder user path `/Users/kb/` out of the
spec, design doc, and one test fixture so examples don't leak a real username.
No runtime behavior change.

### Added
- `LICENSE` (MIT) at the repo root. The wheel has declared `license = "MIT"`
  since 0.8.x but shipped without the actual license text; this closes the gap
  and satisfies GitHub's license detector.

### Changed
- `SPEC.md`, `docs/designs/sync-gstack-context.md`, `tests/test_integration.py`:
  replace `/Users/kb/` with `/Users/alice/` in JSON manifest examples and the v1
  backward-compat fixture. Pure string swap; the fixture's input and assertion
  change together, so semantics are unchanged.

## [0.8.2] - 2026-04-23

Track 1B (Group 1): manifest dead-code cleanup + v1-holdover removal. Drops
vestigial back-compat aliases and the redundant top-level `files` key from
v2 manifests. Zero behavior change for current users; tightens the data
model so future contributors have one shape to reason about instead of two.
Lands alongside Track 1A (0.8.1) which hardened cli.py; footprints are
disjoint.

### Added
- `DiffResult.__repr__` now count-formatted (preserved from the pre-dataclass
  version) so logging a diff on a 500-file manifest emits
  `DiffResult(new=3, modified=1, deleted=0, unchanged=496)` instead of a 50KB
  dump of every entry.
- Regression test locking in that `_merge_manifests` output has no top-level
  `files` key, so a future commit can't silently re-introduce the mirror.

### Changed
- `manifest.py:diff_manifests(local, remote)` → `diff_files(local_files, remote_files)`.
  Old signature took `{"files": ...}`-shaped dicts; new one takes raw file dicts.
  Docstring now documents the pull-path arg-swap convention (`_pull_core` calls
  `diff_files(remote_files, local_files)` intentionally — under additive pull,
  `diff.new`/`modified` are files to download and `diff.deleted` is ignored).
- `DiffResult` converted to `@dataclass(eq=False, repr=False)`. Identity-based
  equality and default hashability preserved exactly — the change is purely
  additive (type hints + default_factory + less boilerplate).
- `normalize_manifest` now unconditionally strips the top-level `files` key on
  both v1 promotion and v2 passthrough. Payload is preserved — v1 promotion
  copies it into `sources.claude.files` before the scrub. Makes normalize
  idempotent on v1 input and closes a dict-copy carry-forward path in
  `_merge_manifests` at cli.py:553.
- `build_manifest_v2` no longer writes a redundant top-level `files` mirror.
  v2 manifests now have a single source of truth: `sources[<name>]["files"]`.
- `SPEC.md` updated: manifest schema section no longer describes the dead
  mirror; backward-compat section explains how v1 on-disk manifests are
  auto-promoted on load and how pre-v0.8.2 v2 manifests are auto-scrubbed.
- `TestGCSafety::test_gc_never_deletes_referenced_blobs` migrated from v1-shape
  dict literals to v2 shape + `load_manifest`, so the test exercises the same
  normalization path production `_do_gc` hits.

### Removed
- `manifest.walk_directory` — back-compat alias for `walk_claude_source` with
  zero production callers. Deleted.
- `manifest.build_manifest` — back-compat alias for `build_manifest_v2` with
  zero production callers. Deleted.

### Fixed
- Stale line-number references in `docs/ROADMAP.md` pointing at the old v1
  `"files"` key write sites.

## [0.8.1] - 2026-04-23

Track 1A (Group 1): cli.py surgical hardening. Five surgical fixes plus two
review-driven follow-ups. Close the gaps audit caught, add tests, no new
features. One behavior change for users: `mm resolve` now exits 1 when any
conflict failed to resolve instead of always exiting 0.

### Changed
- **`mm resolve` exits 1 on partial failure.** `_resolve_interactive_loop` returns `(resolved, failed)`; `resolve` propagates the failure count as a non-zero exit so CI / scripts driving the command can detect that some conflicts were not actually resolved (rename / unlink / read errors mid-walk). Walk continues through every conflict so the user can triage everything in one pass — only the exit code reflects partial failure. Previously: any per-conflict OSError printed a red warning and the command exited 0, making automation think everything was clean.
- **`conflict_filename` raises `ValueError` on empty `device_id`.** The previous `(device_id or "unknown")[:8]` fallback silently minted cross-device-colliding filenames whenever a corrupted peer manifest fed an empty id — exactly the silent data-loss footgun Track 1A exists to close. Caller (`_apply_incoming_file`) catches the `ValueError` and treats it as a per-file failure (matches the existing `OSError`/`StorageError` isolation pattern in the same function), so a single corrupted manifest entry no longer aborts the entire pull.
- **GC malformed-blob-path visibility.** `_do_gc` used to silently `continue` on `data/` entries that did not match the expected `data/{device}/{sha}.enc` shape. Now: verbose / dry-run modes print each malformed key, and non-verbose runs emit a one-line summary count with a hint to re-run with `--verbose`. Never auto-reaped — we don't know what these are. `.tmp` artifacts from crashed pushes are still handled separately by `_sweep_local_tmp_files` at the start of GC.
- **Quiet-path audit (autopull / autopush).** Walked all 20 `if not quiet:` and `if quiet:` sites in cli.py and converted four load-bearing warnings that were silently swallowed in autopull / autopush quiet mode:
  - **Corrupt-manifest sidecar recovery** — `_recover_prior_manifest` now surfaces `mm: warning: remote manifest corrupt; recovered prior state from local sidecar` to stderr in quiet mode.
  - **Corrupt-manifest peer-fallback recovery** — same surface for the peer-tombstone aggregation branch (the riskiest recovery branch — recent local deletions can be lost).
  - **No sync sources misconfig** — `_push_core` now warns to stderr when `get_sources` returns empty in autopush, instead of silently no-opping forever. Autopush also writes a `no-sources` breadcrumb instead of `success` so `mm status` and any monitoring on top of it catch the wedge.
  - **Durability fsync failure on pull** — the deferred-durability `fsutil.fsync_dir` failure warning now reaches stderr in autopull. (Per-result `durability_degraded` field for downstream breadcrumb routing is captured as a follow-up TODO.)
- **Autopull surfaces `total_failed` count.** `_pull_core` increments `total_failed` for per-file failures (decrypt, conflict rename, write, ValueError on corrupted device_id), but autopull used to swallow the summary. Now: a one-line stderr summary with a hint to re-run with `--verbose` for details. Same intent as the helper-level audit, applied at the result-summary level.

### Removed
- **Dead `_delete_files` function.** Never called after the additive-only refactor in v0.3.0. Removing it before a future maintainer re-wires delete-on-pull behavior the spec forbids.
- **Unused `TOMBSTONE_TTL_DAYS` import in cli.py.** Imported but never referenced (consumers live in `manifest.py` and `tests/test_additive_sync.py`). Group 2 pre-flight will move the constant to `constants.py` later; dropping the dead import now is mechanical.

### Technical
- 14 new tests covering every new code path: 2 for `conflict_filename` empty/None, 6 for resolve exit-code semantics + per-file failure isolation, 2 for GC malformed-blob handling, 4 for the quiet-mode warnings (sidecar recovery, peer-fallback recovery via unit test, no-sources, fsync failure), 1 for `_apply_incoming_file` ValueError isolation, 1 for `total_failed` autopull surface, 1 for `no-sources` breadcrumb downgrade. 472 tests total in the suite.
- `_resolve_interactive_loop` signature changed from `-> None` to `-> tuple[int, int]`. Existing call sites discarded the return value, so the change is backward-compatible at the Python boundary; the user-visible change is the resolve exit code.

### For contributors
- `/plan-eng-review` on 2026-04-23 dropped Task 2 (16-char `device_short`) as misframed — `init` itself generates 8-hex-char device IDs, so widening the conflict-filename slice was meaningless. Replaced with the empty-`device_id` `ValueError` raise.
- `/review` (pre-landing) caught a per-file isolation gap: the new `ValueError` was uncaught at the call site and would have aborted entire pulls as "unexpected error" if a peer manifest ever had an empty `device_id`. Wrapped at the call site to match existing OSError/StorageError handling.
- Codex adversarial review caught the `total_failed` summary gap and the `no-sources` breadcrumb regression; both fixed in the same PR. Two findings deferred to TODOS.md: stricter GC blob-shape validation (depth check is in; hash-shape check is the obvious next step), and `durability_degraded` field on `PullResult` for breadcrumb routing.

## [0.8.0] - 2026-04-23

Group 2 Pre-flight + Track 2A: error-surface hardening around corrupt-manifest
recovery. Six items, one PR. `mm diag` and `mm recover` are new subcommands;
`mm init` grows a two-tier destructive-op guard.

### Added
- **`mm diag` subcommand.** Support-triage state dump: mm-crypto-init status + root_salt fingerprint + argon2 params, local config state, sidecar presence + device_id match, storage inventory (peer counts, manifest/data prefixes), last-autorun breadcrumb. Explicit secrets allowlist — NEVER emits raw root_salt bytes, master_key, keycheck, passphrase, or peer device_ids. Plain text default + `--json` for scripting.
- **`mm recover --abandon-manifest` subcommand (destructive).** Last-resort escape hatch when `mm push` refuses with "remote manifest corrupt, no local sidecar, and no peer manifests." Quarantines the corrupt manifest to `<key>.corrupt-<ts>` via crash-durable atomic-write + fsync + unlink (NOT plain rename) so power loss mid-quarantine never leaves both copies gone. Requires exact typed `RESET` confirmation (case-sensitive) or `--yes`. Refuses when the normal recovery chain has a viable source (manifest is ok, sidecar present, peer tombstones exist) — running this in those cases would throw away deletion records that push would otherwise preserve. See SPEC.md "Manifest corruption recovery / Last-resort escape hatch."
- **`mm init` two-tier guard.** Pre-flight 3. `mm init` no longer silently re-inits over existing state. Two tiers gated on storage occupancy (authoritative, not `devices/` which can be silently corrupt):
  - **Orphan case** — mm-crypto-init ok + any existing blobs/manifests/devices: warn that a new device entry gets created, orphaning the prior local device. Requires `typer.confirm`.
  - **BRICK case** — mm-crypto-init missing + encrypted blobs/manifests still exist: re-bootstrap would generate a new root_salt and brick every existing blob. Refuse by default; require exact typed `BRICK` (case-sensitive).

### Changed
- **`_merge_manifests` tiebreak is deterministic across devices.** Pre-flight 1. Sort key changed from `timestamp` to `(timestamp, content_hash)` where `content_hash` = SHA-256 of canonical JSON of the manifest body. Without the tiebreak, Python's stable sort preserved `find_conflict_copies` insertion order, which comes from `Path.glob` — filesystem-dependent and not sorted cross-device. Two Macs pulling the same pair of same-second conflict copies could briefly produce different merged states until the next clean push. `device_id` is NOT in the key: every input to `_merge_manifests` is a conflict copy of the same device's manifest, so it'd be a no-op tiebreaker.
- **`_error()` writes to stderr, not stdout.** Track 2A.2. Introduced `stderr_console = Console(stderr=True)` at module level; `_error` uses it. Interactive TTY keeps `[red]Error:[/red]` formatting; autopush/autopull quiet mode now has a clean stdout + one-line stderr per the README "Claude Code Integration" contract. Before this fix, quiet-mode failures emitted both a rich stdout line and the outer plain-text stderr line.
- **`list_devices` now shape-validates entries, with warnings at CLI sites.** Track 2A.3. `devices.py:list_devices` used to silently drop only JSON parse failures; a JSON-valid but shape-invalid entry (non-dict top level, missing `device_id`, non-string `device_name`) would crash callers at `d["device_id"]` indexing. Now drops shape-invalid entries at the load boundary, and `cli.py` calls a new `_list_devices_warn` wrapper that surfaces one warning per dropped entry via `stderr_console`. Library callers (including tests) still import the silent `list_devices` to avoid stderr spam.

### Technical
- New module-level `_StorageOccupancy` dataclass + `_probe_storage_occupancy` helper driving the init guard decisions.
- New `_manifest_content_hash` helper used by the tiebreak; canonical JSON (`sort_keys=True, ensure_ascii=False`).
- `_quarantine_corrupt_manifest` uses `fsutil.atomic_write_bytes(fsync=True)` + `os.unlink` + `fsutil.fsync_dir` (best-effort) for crash durability.
- 34 new tests across 6 files: tiebreak determinism regression (additive_sync), `_error` stderr + Rich-formatting preservation (track_1a), shape validation + warning emission (recovery), init two-tier guard (integration, 7 cases), `mm diag` secrets boundary + degraded scenarios (diag, 9 cases), `mm recover` unit + destructive integration that pins the accepted deletion-history loss as a regression.
- Track 2A.4 (Optional[X] signature audit) dropped — the canonical conflation case was already resolved by the `ManifestFetch` tri-state migration in v0.5.1. Remaining `Optional[]` is 6 typer decorators (cosmetic); cleanup lives in Group 6B.
- Deferred blob-directory-as-secondary-peer-discovery path captured in TODOS.md with observation bar: "first real support case where corrupt devices.json masks a recoverable manifest."

### For contributors
- `/plan-eng-review` run on 2026-04-23 produced 14 findings across architecture, code quality, tests; codex outside-voice round added 5 gaps (all accepted). Notable: codex correctly flagged that `mm recover --reset-manifest` as originally spec'd was "amputation, not recovery" — the integration test here now pins the accepted cost.

## [0.7.1] - 2026-04-23

Track 1B: Config eager validation + legacy cleanup. Malformed `sync.sources`
in `config.toml` now surfaces at load time with a typed `ConfigError` instead
of a raw `TypeError` mid-sync. Complements Track 1A (v0.7.0) — Track 1A rebuilt
the `autopull` / `autopush` error-surface machinery; Track 1B makes sure the
config-loader actually produces typed errors that machinery can surface.

### Changed
- **Eager source validation.** `_validate` now runs `_validate_sources` whenever `sync.sources` is present, so TOML typos surface at the load boundary with a clear `ConfigError` instead of deferring until the first push/pull attempt.
- **Shape + value-type guards on source validation (cross-model adversarial finding).** `_validate_sources` used to trust `sync` to be a dict, `sources` to be a list, each entry to be a dict, and field values to be strings. Bad input (`sources = "claude"`, `sources = [42]`, `name = ["claude"]`) raised raw `TypeError` or crashed at `.expanduser()` — neither was a `MindMeldError`, so Track 1A's new typed-error surface in `autopull` / `autopush` would not have caught them. Every malformed shape now raises `ConfigError` with a pointed message naming the offending field and its actual type.
- **Unexpected load-time errors normalized to `ConfigError`.** `load_config` now wraps `_validate` + `_apply_defaults` so any non-`ConfigError` exception (e.g., `.resolve()` `RuntimeError` on a cyclic symlink) becomes a `ConfigError`. Feeds cleanly into Track 1A's `MindMeldError` branch in `_auto_command_setup`.
- **`.resolve()` parity with the rest of the codebase.** `_apply_defaults` and explicit `sync.sources` paths now call `.expanduser().resolve()` to match the dominant pattern at 11 other call sites across `cli.py`, `manifest.py`, `storage/local.py`, and `synclog.py`. Keeps config-stored paths aligned with walker-emitted paths so symlinked setups don't silently disagree. `DEFAULT_SOURCES` and the auto-detected gstack fallback deliberately skip `.resolve()` here — the walker resolves at use time anyway, and resolving them up front would let a cyclic user symlink at `~/.gstack` break `get_sources` for every command.

### Removed
- **Python 3.10 `tomllib` fallback.** `pyproject.toml` requires Python 3.11+, so the `sys.version_info` gate and `tomli` import branch were unreachable dead code. Replaced with unconditional `import tomllib`.
- **Legacy `claude_dir` default in `_apply_defaults`.** `get_sources` already falls through to `DEFAULT_SOURCES` when neither `sync.sources` nor `sync.claude_dir` is present; the extra `setdefault` was redundant with that fallback and forced every new config through a "legacy" code path. `_apply_defaults` now expands `claude_dir` only when it is actually present.

### For contributors
- 21 new tests in `tests/test_config.py` + 2 regression tests in `tests/test_integration.py` covering: eager validation paths, shape guards (non-list / non-dict / non-string field values), `.resolve()` parity and round-trip idempotency on symlinked paths, `claude_dir` absence, `load_config` error normalization, and `autopull` / `autopush` stderr surfacing on bad configs (verified against Track 1A's `_auto_command_setup`).
- Two follow-up TODOs captured in `docs/TODOS.md`: (1) stop mutating config in `_apply_defaults` — compute expanded paths lazily in `get_sources` to avoid silent realpath rewrite on backfill save, and (2) rich `ConfigError` with TOML line numbers on parse failure.

## [0.7.0] - 2026-04-23

Track 1A: silent-failure cleanup in `autopull`/`autopush` + pull-side conflict-mode
unification. Continues the Group 1 error-discipline theme after Tracks 1B, 1C, 1D.

### BREAKING
- **`mm pull --no-prompt` and `--resolve-interactive` are removed.** Replaced by a single `--conflict-mode {prompt|keep-both|fail}` option (default `keep-both`). Migration:
  - `mm pull` (no flags)              → `mm pull` (unchanged — default is keep-both).
  - `mm pull --no-prompt`             → `mm pull` (the default IS keep-both).
  - `mm pull --resolve-interactive`   → `mm pull --conflict-mode prompt`.
  - *(new)* `mm pull --conflict-mode fail` — preflights every file via `_predict_pull_outcome`; if any would conflict, prints the list and exits **3** (not 2) with no writes. For CI. Exit 3 avoids colliding with typer/click's usage-error exit 2, so a stale script still passing the removed flags can't be misclassified as a conflict refusal.

### Fixed
- **`autopull`/`autopush` silently swallowed bugs.** The outer `except Exception` reduced every unexpected failure to a single cryptic stderr line. On the Claude Code hot path this hid data-integrity issues for days. Now: `FileNotFoundError`-equivalent (missing config) → silent; `MindMeldError` subclasses (`ConfigError`, `CryptoError`, `LockError`) → typed one-line stderr; anything else → one-line stderr + full traceback appended to `~/.config/mind-meld/autopull.log` or `autopush.log` (truncate-tail at 1 MB, keep last 512 KB). Shared prelude extracted into `_auto_command_setup` + `_log_unexpected` helpers so the contract can't drift between the two commands.
- **`autopull`/`autopush` could hang on missing passphrase.** `get_passphrase()` previously fell through to `getpass.getpass()` when neither the keyring nor `MINDMELD_PASSPHRASE` yielded a secret — fine for interactive commands, a hang for hook-path callers. New `non_interactive: bool = False` parameter: when True, raise `CryptoError` instead of prompting. `autopull` and `autopush` pass `non_interactive=True`; every other caller keeps the interactive fallback.
- **Corrupt peer manifests were silent in autopull.** The "manifest is corrupt, skipping pull from this device" warning in `_pull_core` was gated on `not quiet`, so autopull (`quiet=True`) never surfaced a load-bearing corruption signal. Now routed to stderr regardless of quiet — corrupt-manifest recovery is load-bearing (see CLAUDE.md) and silent skip = partial pull dressed up as success.
- **Sidecar write failures were silent in autopush.** Same class: the "failed to write recovery sidecar" warning was gated on `not quiet`, so autopush silently lost its recovery path. Now routed to stderr regardless of quiet.
- **Unknown remote sources silently skipped on pull.** When a peer advertised a source name the local config didn't know about (rename drift, missed migration), the `skipping unknown source '<name>'` message was gated on `--verbose and not quiet` — silent-partition risk. Now always warns, and `PullResult.total_skipped_unknown_source` counts `(device, source)` pairs for the summary line. `autopull` emits a one-line stderr summary when the count is non-zero.
- **`mm devices` showed "Last Seen" but the value was really "last push".** `register_device` used to seed `last_seen` at registration time, so a registered-but-never-pushed device rendered as though it had just pushed. Seed removed: `last_seen` now means exactly what it says ("last push"), registered devices render as em-dash until the first push, and the column header is renamed to "Last Push."

### Added
- `mm pull --conflict-mode {prompt|keep-both|fail}` (default `keep-both`). `fail` mode preflights via `_predict_pull_outcome`, exits **3** on any predicted conflict with no writes. Best-effort — a file edited between preflight and apply may still produce a `.sync-conflict-*` (TOCTOU); re-run pull to surface late conflicts.
- `_log_unexpected(verb, exc)` hand-rolled appender (stdlib-only, no `logging` module — avoids handler-duplication regressions in long-lived test runs). Writes ISO timestamp + mm version + full traceback. Any failure inside the logger itself is swallowed: a broken log file must never crash the hook.
- `PullResult.total_skipped_unknown_source: int` — counts `(device, source_name)` pairs.
- `get_passphrase(non_interactive: bool = False)` — new parameter.
- 28 new tests in `tests/test_track_1a.py` covering: the 14 plan-derived cases (regressions, hook correctness, log rotation, conflict-mode preflight, non-interactive passphrase) plus 14 added during /review pass (typed-error no-log branches, --conflict-mode prompt threading, end-to-end no-passphrase flow, register_device storage-level contract, _log_unexpected swallow-failure, unexpected-crypto-error logging, cross-peer preflight overlap, breadcrumb on success / lock-held, `mm status` surfacing of breadcrumb, concurrent-writer log safety, typed-error-without-cause no-log, _log_unexpected truncate-tail idempotency, unwrapped config error logging). Total suite is now 402 tests.
- `~/.config/mind-meld/last-autorun.json` breadcrumb on every `autopull` / `autopush` invocation (success, lock-skip, config-missing, crypto-error, failed). `mm status` surfaces it as "Last auto-pull: 2026-04-23T..." so a wedged flock is no longer invisible to the user.
- `--conflict-mode fail` preflight now simulates **cross-peer** writes via an in-memory overlay. If peer A would write role.md=Y and peer B ships role.md=Z, the preflight now flags the B-vs-A conflict even though starting local state is empty — previously the contract "no writes on conflict" could be violated during multi-peer pulls.
- `_log_unexpected` writes are serialized with `fcntl.flock(LOCK_EX)` so two concurrent failing hooks can't corrupt each other's traceback.
- Wrapped typed errors (`ConfigError from tomllib.TOMLDecodeError`, future `X from OSError`) now log the full cause chain; pure validation errors (no `__cause__`) stay stderr-only. Preserves forensic value without spamming the log with expected conditions.

### For contributors
- `CLAUDE.md`, `SPEC.md`, `README.md` updated for the `--conflict-mode` unification and the `autopull`/`autopush` error contract.
- Exit code 3 (new, for `--conflict-mode fail`) deliberately avoids typer/click's usage-error exit 2. Scripts that still pass the removed `--no-prompt` / `--resolve-interactive` flags will hit usage-error exit 2 — distinct from conflict refusal.
- `docs/TODOS.md` gets `[plan-eng-review 2026-04-23 Track 1A]`: full `quiet`-path audit — classify every `if not quiet:` in cli.py as "verbose-only" vs "load-bearing." Two known load-bearing gates are patched in this release; the pattern is likely wider.

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
