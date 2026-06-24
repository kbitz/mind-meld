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
"Advance latest branch" step in `release.yml`. It only ever points at *released*
commits, which is how `@latest` reconciles with the "tag = release; `main` may
carry untagged WIP" discipline below: tracking `main` directly would leak WIP on
a `--force` reinstall, tracking `latest` cannot. Keep README Install / Upgrading
and `INSTALL_CMD` on `@latest`; never reintroduce a `@{tag}` pin.

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
