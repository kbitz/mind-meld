# Self-update, the upgrade nudge + release discipline — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/upgrade.py` — `run_transition_hook` / `update_or_nudge` / `emit_nudge_if_due` / `detect_install` / `update_argv` / `run_update` / `_claim_install_attempt` / `_run_pipx` / `_spawn_pipx` / `_pick_latest_tag` / `INSTALL_SPEC` / `INSTALL_CMD` / upgrade-state cache layout
- `src/mind_meld/cli.py` — the 3 transition-detection hook seams (`_get_config`, `_auto_command_setup`, `init_cmd`); the 4 self-update seams (tail of `push` / `pull` / `autopull` / `autopush`) and `recapture`'s nudge-only tail; the `update` command; `mm status` upgrade surfacing
- `src/mind_meld/config.py` — the `[upgrade]` defaults in `_apply_defaults` (`auto_check`, `auto_install`)
- `src/mind_meld/pullhistory.py` — `append_self_upgrade` and `verb: "self-upgrade"` row class
- `pyproject.toml` — version source of truth; bumping triggers the next-tag release
- `.github/workflows/release.yml` — the "Advance latest branch" step (moving ref for upgrades)
- `README.md` — Install / Upgrading sections (must stay `@latest`, never `@vX.Y.Z`)

Tests: `tests/test_upgrade.py`, `tests/test_self_update.py`, `tests/test_pullhistory.py` (self-upgrade row class).

---

## Self-update (v1.3.0)

mm runs pipx in two places. They are deliberately **not** the same command.

| Path | Trigger | pipx command | Install shapes it touches |
|---|---|---|---|
| Automatic | tail of `push` / `pull` / `autopull` / `autopush`, when `[upgrade] auto_install` is on (default) | `pipx upgrade <venv>` only | `tracking` only |
| Explicit | `mm update` | `pipx upgrade <venv>`, or `INSTALL_CMD` for a pinned install | `tracking` and `pinned` |

**The automatic path never runs `pipx install --force` (load-bearing).** Read
from pipx 1.17.7's `commands/install.py`: a forced reinstall into an existing
venv that fails calls `venv.remove_venv()` unless the install recorded
`expected_apps`, a lock file, or resources, and a plain mm install records
none. A network drop mid-clone therefore **uninstalls mm**. `pipx upgrade`
runs `pip install --upgrade` inside the existing venv and does not remove the
venv on failure. Interrupting pip during its package swap can still leave
partial files, so interruption messages include a reinstall remedy. The
unattended path is the in-place upgrade or nothing, and the
forced reinstall is reachable only from `mm update`, where a person is present
to read `If mm is now missing, reinstall with: …`. Do not "simplify" the two
paths onto one command.

**Install classification (`detect_install`).** Reads
`<sys.prefix>/pipx_metadata.json` and nothing else:

- `tracking` — recorded spec is exactly `INSTALL_SPEC`. The only kind the
  automatic path acts on.
- `pinned` — this repo at another ref (the README rollback recipe pins a tag
  on purpose). `pipx upgrade` can never move it. The automatic path leaves it
  alone and nudges, so a deliberate rollback is never undone behind the
  user's back. `mm update` is the documented "resume upgrades" step and
  reinstalls it onto the release branch, but only when the forced check
  proves a newer release exists: on an unreachable GitHub it refuses rather
  than force-reinstall on a guess.
- `pipx-pinned`, `foreign`, `not-pipx`, `dev` — never touched by either path.
  `mm update` exits 1 and names the reason.

The spec comparison is exact (`== INSTALL_SPEC` for `tracking`; `== REPO_SPEC`,
or `REPO_SPEC` followed by `@`, for `pinned`, so an install with no ref at all
is `pinned` too), never a prefix or substring match: a fork or a lookalike URL
must not read as ours.

The metadata's package name and suffix must also identify the running venv
under its pipx home's `venvs/` directory. Both subprocess seams derive
`PIPX_HOME` from that directory instead of trusting an ambient selection of
another pipx home. An explicit reinstall preserves the recorded suffix, so
updating a rollback environment cannot overwrite its unsuffixed sibling.
Before a forced reinstall, the subprocess seam also refuses an existing
executable destination that belongs to another install, including an ambient
`PIPX_BIN_DIR` selecting a sibling home's command. It binds `PIPX_BIN_DIR` to
the checked directory so self-managed pipx cannot infer a different destination.

**Completion is verified from metadata.** A successful pipx exit must leave
readable metadata for a release-tracking install. When the check found a newer
tag, a changed install below that version is an incomplete update, even if its
spec moved from a pin to `@latest`. `mm update` exits 1 and automatic updates
record failure; neither reports that intermediate version as completion.
Comparisons use installed metadata rather than the process's imported version;
an install another updater has already advanced is current, not a failure.

**One attempt per release per day (`_claim_install_attempt`).** The claim is a
single read-modify-write under the upgrade-state flock, stamped **before**
pipx starts, so two hooks firing in the same second spawn one pipx. Three
cache keys carry it: `install_attempt_version`, `install_attempt_at`,
`install_attempt_outcome`. A claim that cannot be recorded is not a claim —
without the stamp every later pull and push would start its own pipx.

**Installer exclusion is independent of the daily claim.** Both subprocess
seams take a nonblocking kernel flock on `~/.config/mind-meld/install.lock`.
Only that descriptor is passed to pipx; it does not inherit the mm lock.
The parent closes its copy without `LOCK_UN`, so the detached child retains
exclusion after its hook exits and releases it automatically when pipx exits.
Never unlink this lockfile. Explicit updates and automatic retries cannot
start another installer while it is held, even after 24 hours or for a newer
release. The claim checks this live lock before changing the cache. Installer
acquisition retries briefly so a read-only liveness probe does not spuriously
consume the day's attempt. Continuing contention is a `busy` refusal, not a
failed installation; `mm update` exits 1 without suggesting a forced reinstall.

- A live installer is always `in-flight`. Inside `INSTALL_GRACE` (10 min),
  an unfinished cache-only attempt also reads as `in-flight`: later
  runs neither re-spawn nor nudge. A detached install reports nothing back, so
  this window is what stops a second hook calling a healthy install failed.
- Past the grace, with no live installer, still being behind means the attempt failed. The nudge
  resumes (its own 24h gate) with the log path appended, `mm status` prints
  one extra line, and the next attempt waits out `DEFAULT_INSTALL_RETRY_GAP`.
- A newer release resets the gate immediately.

Status probes the installer lock without creating it or waiting. It surfaces
an attempt only for the running release-tracking install while its recorded
version is still behind; a foreign or already-updated install never inherits
another installation's failed-attempt message.

**Attended vs unattended.** `push` / `pull` run pipx in the foreground after
`release_lock()`, re-take the mm lock for the swap, and print
`mm: notice: updating …` then `updated …`. `autopull` / `autopush` spawn pipx
in its own session with output to `~/.config/mind-meld/auto-update.log`, print
nothing, and do not wait. The detached pipx cannot hold the mm lock (its
parent exits), so an mm process starting during the few seconds pip swaps
files can fail once at import. That is the accepted cost of not stalling a
Claude Code hook for the length of a git clone.

Foreground pipx runs in its own process group. A timeout or cancellation
first sends SIGINT, waits at most five seconds, then escalates to SIGKILL
and waits before releasing the mm lock. It checks the process group even
after its parent and pipes exit. Pipe cleanup is bounded too. Recovery
commands bind the same pipx home and preserve the suffix.
If pipx already exited before the pipe timeout, bounded cleanup preserves
that exit result and the caller still verifies the installed metadata.
Ctrl-C during process creation is deferred until the caller owns the process
handle, then follows the same bounded cleanup before releasing the mm lock.
Further Ctrl-C presses are ignored until cleanup finishes; the previous
signal handler is then restored before propagating the original interruption.
Cancelling an optional update preserves the completed sync and marks its
claimed attempt failed without shortening the daily retry gate. Cancelling
the push itself skips the update tail entirely.

**A sync's outcome never depends on the update.** `update_or_nudge` raises
nothing and sets no exit code: a failed install is a `mm: notice:`. The new
version takes effect on the next mm process; nothing re-execs. The existing
transition hook then writes the `self-upgrade` pull-history row, so the
automatic path needed no new logging.
The exception boundary covers the check, install, cache bookkeeping and
fallback nudge, including interrupted HTTP responses and a closed stderr.

**Both opt-outs of the check also stop the install.** `auto_install` acts only
on what the check finds, so `--no-check-version` and `[upgrade] auto_check =
false` disable it too. `auto_install` fails closed: a non-boolean value reads
as off, the opposite of `auto_check`'s lenient default, because this key lets
mm run pipx. `mm update` ignores all three and the 24h throttle — the user
asked.

**`mm update` needs no config and no passphrase.** It must work on a Mac whose
sync is refusing (newer storage format, broken config), which is exactly when
an update is the remedy. It does not call `_get_config`, and must not grow a
dependency on it. **Known limit:** a hook that refuses in `_auto_command_setup`
never reaches the tail, so a Mac refused for newer storage does not update
itself; the refusal message's remedy still applies.

**No test can reach a real pipx.** `_run_pipx` and `_spawn_pipx` refuse under
`PYTEST_CURRENT_TEST` (the same guard as `crypto.store_passphrase_in_keyring`),
and the suite's own interpreter classifies as `not-pipx`.
`test_real_test_environment_is_not_a_pipx_install` pins the second.

## Upgrade nudge (v0.9.5)

`mind_meld.upgrade` runs a leading-edge version check to move the fleet toward
the latest tag before fleet-version refusal trips. Single cache file at
`~/.config/mind-meld/upgrade-state.json`, fcntl-flocked on every read+modify+write
so transition detection is race-correct under two concurrent mm processes.

The `mm: notice:` line prints the upgrade command; the user runs it. Since
v1.3.0 it is the fallback: it prints when the automatic path cannot act (the
setting is off, the install does not track the release branch, pipx is not
found) or when its attempt failed. `recapture` stays nudge-only.

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
2. **Self-update or nudge** (`upgrade.update_or_nudge`) at the TAIL of
   `push` / `pull` / `autopull` / `autopush` AFTER main work completes
   (`recapture` keeps the nudge-only `upgrade.emit_nudge_if_due`). Tail
   position keeps cold-cache HTTP latency (~500ms 1x/24h) from stacking on
   sync latency. Interactive `mm push` calls it from the command's `finally`
   block after `release_lock()`, so a failed attended attempt still updates or
   nudges (64A); `--dry-run` never does either.
3. **Status surfacing** in `mm status` — calls `cached_upgrade_view`, reading
   `locked_json_snapshot(blocking=False)` with no HTTP request or cache write.
   It honors dev-build, `--no-check-version`, and `auto_check = false` skips.
   Missing, malformed, or contended caches are unknown; stale results show
   the age of `checked_at`. Push, pull, autopull, and autopush keep the
   refreshing `check_for_upgrade` path.

**Lock-order invariants (load-bearing):** NEVER acquire mm lockfile while holding
upgrade-state's flock; RELEASE upgrade-state's flock BEFORE appending to
pullhistory. Transition detection runs OUTSIDE the mm lock by design — its
correctness is bounded by upgrade-state's own flock. The attended self-update
takes the mm lock FIRST and claims its attempt second, for the same reason.

**`mm: notice:` prefix is distinct from `mm: warning:`.** Curated stderr taxonomy:
- `mm: warning:` — data-at-risk signals (corrupt-manifest recovery, fsync
  failure, no-sources misconfig, etc.). Reader trains attention on this prefix.
- `mm: notice:` — FYI signals (the upgrade nudge and self-update progress
  today; future "new feature" hints). Adding non-data-at-risk signals to `warning:` would dilute the
  warning class.

**`pullhistory` schema extension.** New `verb: "self-upgrade"` row class peer to
pull/push, with `old_version`/`new_version` (NO source/rel_path/action). Written
via `pullhistory.append_self_upgrade(...)` (NOT extending `append()` — separate
event class, separate function). Contract violations silent-skip (NOT assert) so
forensic log failures don't block sync. `mm log` table renderer adds an `extra`
column showing `OLD → NEW` for self-upgrade rows; pull/push rows leave it empty.

## Read-only callers (Tracks 56A/62A)

`cli._get_config(read_only=True)` skips **all** of `run_transition_hook`; skipping only `append_self_upgrade` would consume `last_seen_self_version` and lose the transition. Push dry-run loads once and gates `update_or_nudge` at its CLI call site: no cache creation/rewrite, HTTP request or pipx run, even when absent, stale, or due. Status also loads config read-only and uses the cached upgrade view. A pending transition is recorded by a following mutating command; every preview and inspection preserves it. See the [README Previews table](../../README.md#previews) and `COMMAND_INTENTS62` for the complete command contract. `TestPushPreviewNoMutation56A.test_s4b_transition_survives_preview_and_status_then_push_records_once` resets process guards, uses a non-dev version, and proves that preview and status preserve the pending transition before a real push records it exactly once.

## Release discipline (enforced by mm auto-upgrade)

**Tag = release. Merge to main alone is not.**

The auto-upgrade feature reads the latest tag from `/repos/kbitz/mind-meld/tags`
and nudges the fleet to upgrade to it. /ship prepares the release PR;
`.github/workflows/release.yml` creates the tag and GitHub Release automatically
after a push to `main` touching `pyproject.toml` or `CHANGELOG.md`.

- **Release PRs:** bump `pyproject.toml`, add the matching `CHANGELOG.md` entry
  and `docs/PROGRESS.md` row, then merge through the approved PR workflow.
  The Release workflow creates missing tag/Release artifacts and advances
  `latest` only when that tag points at the merged commit. Verify the workflow,
  tag and Release after merge; /ship does not manually tag. Fleet sees the
  nudge within 24h.
- **Mid-feature WIP merges to main:** leave the released version in place.
  A change to either trigger file can run the Release workflow, but existing
  tag/Release artifacts are not recreated and `latest` does not advance to an
  untagged commit. Fleet stays on the prior tagged release.
- **Pre-release tags** (containing `-rc`, `-alpha`, `-beta`, `-dev`) and
  **local-version tags** (`+local`) are filtered out by `_pick_latest_tag` —
  tag freely for testing.

A version bump merged to `main` triggers a release automatically. Keep
unfinished release work on the feature branch; omitting a manual tag does not
prevent publication.

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
