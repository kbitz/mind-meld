# Auto-upgrade nudge + release discipline — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/upgrade.py` — `run_transition_hook` / `emit_nudge_if_due` / `_pick_latest_tag` / `INSTALL_CMD` / upgrade-state cache layout
- `src/mind_meld/cli.py` — the 3 transition-detection hook seams (`_get_config`, `_auto_command_setup`, `init_cmd`); the 2 nudge-emission hook seams (tail of `_pull_core` / `_push_core`); `mm status` upgrade surfacing
- `src/mind_meld/pullhistory.py` — `append_self_upgrade` and `verb: "self-upgrade"` row class
- `pyproject.toml` — version source of truth; bumping triggers the next-tag release
- `.github/workflows/release.yml` — the "Advance latest branch" step (moving ref for upgrades)
- `README.md` — Install / Upgrading sections (must stay `@latest`, never `@vX.Y.Z`)

Tests: `tests/test_upgrade.py`, `tests/test_pullhistory.py` (self-upgrade row class).

---

## Auto-upgrade nudge (v0.9.5)

`mind_meld.upgrade` runs a leading-edge version check to nudge the fleet toward
the latest tag before fleet-version refusal trips. Single cache file at
`~/.config/mind-meld/upgrade-state.json`, fcntl-flocked on every read+modify+write
so transition detection is race-correct under two concurrent mm processes.

**Approach A: nudge-only.** mm NEVER invokes pipx itself. The `mm: notice:` line
prints the upgrade command; the user runs it. Subprocess pipx execution is
deferred (see TODOS) for managed-pipx / rollback / UX reasons, NOT process-
replacement impossibility (`execvp` would work fine).

**Upgrade command tracks the `latest` BRANCH, never a `@vX.Y.Z` TAG (load-bearing).**
`INSTALL_CMD = "pipx install --force git+https://github.com/kbitz/mind-meld.git@latest"`
— a single version-independent constant (NOT a per-version template). The target
version appears only in the nudge *message* text (`format_upgrade_message`), not
in the command.

Why this matters — the historical lock this fixes: pipx's
`parse_specifier_for_upgrade` strips PEP508 version specifiers but a git `@<ref>`
is a URL *fragment*, so it is kept verbatim on `pipx upgrade`. A tag ref
(`@v0.12.9`) re-resolves to the same frozen commit every time → `pipx upgrade`
reports "already at latest" forever (users concluded releases were broken; see
README #99). A branch ref (`@latest`) re-resolves to its advancing HEAD → upgrade
lands. The `--force` reinstall additionally REWRITES a previously tag-pinned
install's recorded `package_or_url` onto `@latest`, so the one nudge command both
un-sticks the old pin AND makes future plain `pipx upgrade mind-meld` work.

The `latest` branch is force-advanced to each tagged release commit by the
"Advance latest branch" step in `release.yml`. The step compares
`git rev-parse "$tag^{commit}"` against `git rev-parse HEAD` *after* the
tag-creation step and skips with `::warning::` when they differ, so a
non-release `pyproject.toml` edit (dev-dep bump, no version change) cannot
move `latest` onto an untagged commit. `^{commit}` is required because the
tag step creates lightweight tags. Do not replace that comparison with
`if: steps.check.outputs.tag_exists == 'true'` — that inverts the logic and
skips latest-advance on every genuine first release. It only ever points at
*released* commits, which is how `@latest` reconciles with the "tag = release;
`main` may carry untagged WIP" discipline below: tracking `main` directly
would leak WIP on a `--force` reinstall, tracking `latest` cannot. Keep
README Install / Upgrading and `INSTALL_CMD` on `@latest`; never reintroduce
a `@{tag}` pin.

**Version source: tag-based.** `/repos/kbitz/mind-meld/tags?per_page=100` →
`packaging.Version` filter (skip `is_prerelease` AND skip `local is not None` —
the latter because `0.9.4+local > 0.9.4` per packaging) → max-semver. Cap at
100 tags is documented in `upgrade.py`; revisit when fleet has more than 100
releases (~3 years at current velocity). Why tags not raw-pyproject-on-main:
HEAD may be mid-bump or contain WIP that hasn't been tagged for release.

**3 hook seams in cli.py:**
1. **Transition detection** (`upgrade.run_transition_hook`) called AFTER each of
   3 load_config sites: `_get_config`, `_auto_command_setup`, `init`.
   `_get_config(*, read_only: bool)` has no default: every caller states its
   policy. Read-only callers defer the entire transition to the next mutating
   command. The AST gate also confines direct transition calls to these three
   functions, and requires `read_only` at every migration-prompt call. Codex
   outside voice flagged that refactoring all 3 through `_get_config` would break
   `_auto_command_setup`'s silent-on-missing-config contract — preserved by
   shared-helper pattern instead.
2. **Nudge emission** (`upgrade.emit_nudge_if_due`) at the TAIL of
   `_pull_core`/`_push_core` (quiet AND interactive paths) AFTER main work
   completes. Tail position keeps cold-cache HTTP latency (~500ms 1x/24h) from
   stacking on sync latency. Interactive `mm push` emits it from the command's
   `finally` block after `release_lock()`, so a failed attended attempt still
   nudges (64A); `--dry-run` never does.
3. **Status surfacing** in `mm status` — calls `cached_upgrade_view`, reading
   `locked_json_snapshot(blocking=False)` with no HTTP request or cache write.
   It honors dev-build, `--no-check-version`, and `auto_check = false` skips.
   Missing, malformed, or contended caches are unknown; stale results show
   the age of `checked_at`. Push, pull, autopull, and autopush keep the
   refreshing `check_for_upgrade` path.

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

## Read-only callers (Tracks 56A/62A)

`cli._get_config(read_only=True)` skips **all** of `run_transition_hook`; skipping only `append_self_upgrade` would consume `last_seen_self_version` and lose the transition. Push dry-run loads once and gates `emit_nudge_if_due` at its CLI call site: no cache creation/rewrite or HTTP request, even when absent, stale, or due. Status also loads config read-only and uses the cached upgrade view. A pending transition is recorded by a following mutating command; every preview and inspection preserves it. See the [README Previews table](../../README.md#previews) and `COMMAND_INTENTS62` for the complete command contract. `TestPushPreviewNoMutation56A.test_s4b_transition_survives_preview_and_status_then_push_records_once` resets process guards, uses a non-dev version, and proves that preview and status preserve the pending transition before a real push records it exactly once.

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

## Compatibility (1.x)

1.0 freezes the interoperable storage/wire formats, command and flag names,
positional arguments, documented prompt keys, `config.toml` keys, exit-code
meanings, and the machine-readable surfaces listed below. Mixed 0.14.x/1.0.0
fleets interoperate: this release changes no wire or storage format.

| Release | Classification |
|---|---|
| MAJOR | A change after which a 1.x peer cannot read newer storage or sync with its writer; removing/renaming a command, flag, argument, documented prompt key, config key or stable output field; incompatible argument type/arity/requiredness/choices; changing an exit meaning, including making a currently successful outcome fail. |
| MINOR | Compatible additions: commands, flags, optional config keys, output fields and enum values **where existing readers tolerate them**. Also changes needing downgrade care, and raising the Python floor, announced in Upgrade notes. |
| PATCH | Other compatible fixes, including repairs to best-effort vendor usage readers. |

**MAJOR exceptions to additive changes:** a new host family, token counter
field, reordering known token-source names, or changing the device-registry
field contract. Host families are closed (`host_usage.HostFamily` and
`aggregator._accept_hosts_payload`); token buckets require exactly
`TOKEN_FIELDS` (`aggregator._copy_usage_bucket`). Known token-source order is
checked by `_token_sources_subsequence`. The registry's `device_id`,
`device_name`, `registered`, `last_seen`, and `last_seen_version` are pinned;
`_list_devices_impl` requires identity/name and the fleet gate interprets the
last-seen version. A missing last-seen version on a never-pushed legacy device
retains its existing treatment. `test_closed_vocabularies` and
`test_device_registry_writer_fields` enforce these exceptions.

There is no blanket unknown-value promise. Existing behavior is:

- Host snapshot unknown **top-level** fields are ignored by
  `_accept_host_usage_snapshot`; unknown nested families or token keys reject
  the row. A new reader/token-source name is MINOR: bounded unknown names are
  retained by `_token_sources_subsequence`, with known names still ordered.
- `manifest.load_manifest` preserves unknown top-level metadata while
  validating known sources/tombstones; it does not make arbitrary changes
  to known fields safe. Event consumers dispatch by `type` and ignore types
  they do not consume (`aggregate_pushes`, `aggregate_host_usage`).
- Consumers of additive public JSON fields must ignore unfamiliar keys;
  enum additions qualify as MINOR only after checking the affected reader.
  Do not infer tolerance of nested values from top-level tolerance.

**Both sides of a format-changing MAJOR must refuse safely.** The
`mm-crypto-init` version byte is the older-side gate. Any MAJOR changing blob,
manifest, mm-events row, or conflict-filename formats must bump that byte in
the same release, within the reserved **0x03–0x0F** window. Future blob bytes
must also stay in that window. Write new crypto-init before any new-format
manifest/blob. The newer-side fleet gate must refuse while any registered
peer runs below 1.0.0, because 0.14.x lacks the older-side protection; extend
the `_check_fleet_version_or_refuse` pattern for that release.

In 1.0, any observed newer crypto-init copy refuses selection and repair;
repair re-fetches before mutation and again immediately before replacing
canonical. iCloud can deliver files out of order: with an older init still
visible, a manifest byte in 0x03–0x0F is unreadable even beside an older
sibling, so pull skips that peer and GC refuses. Push rewrites only this
Mac's own manifest. A push
can finish publication and then exit 1 when required auto-GC refuses. Newer
blob errors reach the per-file pull warning; manifest validators intentionally
fold them into corruption. These are the implemented limits, not an
iCloud-wide transaction. `TestNewerFormats66A`, `TestNewerStorage66A`, and
[the init invariant](init-devices.md#newer-format-refusal-track-66a-v100)
pin the gate, late-copy recheck and arrival windows.

**Machine-readable surfaces:**

| Surface | Stable scope |
|---|---|
| `diag --json` | Top-level keys pinned by `_DIAG_JSON_TOP_LEVEL` in `tests/test_docs_routing.py`, plus the fields documented in README (including `crypto_init.newer_version`). Other nested diagnostics may change in a MINOR. |
| `refresh-identity --json` | The documented identity result fields. |
| `devices --format json` | Device records and their existing field meanings. |
| `log --format jsonl` | Existing history row fields; row variants retain their own shapes. |
| `retro-fleet --dump-host-usage` | Existing forensic inventory fields and meanings. |
| `MM_THEMES_PROMPT` JSON block in `retro-fleet` | Versioned with SKILL.md; additive fields are MINOR. |

Plain-text status/diag/pull output, prompt wording, defaults (including
`exclude_patterns`), and private vendor usage formats are not stable surfaces.
Host readers remain best-effort: vendor format repairs are PATCH or MINOR.
Framework-owned `--help`, completion options and parse-error presentation are
excluded from the CLI golden; their usage errors remain documented exit 2.
The supported Python floor is `requires-python`; CI qualifies Python 3.13
only. A higher floor is MINOR with Upgrade notes, not evidence that other
interpreters were tested.

**Exit outcomes:** [one README table](../../README.md#exit-codes) is the
operator reference, including post-parse value errors (1), usage errors (2),
pull preflight (3), and partial recapture (4). Valid autopull/autopush
invocations exit 0 even on refusal; per-file pull failures also retain exit 0.
A stricter pull exit policy must be an opt-in flag in a MINOR.
`tests/test_compat_contract.py:EXIT_EVIDENCE` maps every outcome to behavioral
tests and checks those references exist. Its all-module exit AST scan is
supplementary, not a substitute for those tests.

**Deprecation and downgrade:** a deprecated item keeps working until its
named removal MAJOR and emits `mm: notice:` naming that MAJOR when used.
`--no-save` remains a hidden no-op until **2.0**. Retired prompt keys `b`,
`both`, `c`, and `f` are never reassigned during 1.x. The prompt fallback
tests in `test_conflict_copy.py` and no-op tests in
`test_retro_fleet_aggregator.py` pin today's behavior.

Rollback within 1.x is a reinstall unless an intervening MINOR's Upgrade
notes say otherwise. The upgrade nudge selects the highest release tag and
never downgrades (`tests/test_upgrade.py`); a regression after 1.0.0 ships as
1.0.1. Skill stores refresh on init, non-quiet push, or install-skills and
never downgrade. After rollback, move the store aside before reinstalling
the running package's skill; see [the README recipe](../../README.md#upgrading).
`TestDurableStore.test_publish_skipped_when_stored_version_is_newer` pins the
refusal to downgrade, and `test_empty_store_installs_running_package` pins
publication into an empty store.

**Release tripwires:** `tests/test_compat_contract.py` pins format constants,
closed vocabularies, the registry shape, and the detailed CLI surface in
`tests/fixtures/cli_surface_1_x.json` (root options, hidden options, arguments,
names, requiredness, flag/value, multiplicity, arity, types and choices).
Additions and removals both fail: classify before updating the golden.
Frozen `tests/fixtures/compat_1_0/` payloads are decrypted/parsed by the current
readers, never regenerated during tests: a 1.x reader must read what 1.0 wrote.
