# Changelog

All notable changes to Mind Meld will be documented in this file.

## [0.12.49] - 2026-08-28

**Host usage now ships per-model per-day buckets on the wire, derived from the existing v0.12.48 cache with no re-walk.** Family totals in `hosts` are unchanged; the new `tokens_by_day` sibling is the shape Claude's session snapshot already uses. The retro card stays family-only this release — per-model data reaches `mm retro-fleet --dump-host-usage` and local counts on `mm diag`, not the card (that is Group 36). Until every Mac is on ≥ v0.12.49 and has pushed once, `--dump-host-usage` carries per-model detail for some machines and not others — a peer on an older mm sends no sibling at all, and its row is still accepted. `## Agent activity` numbers do NOT diverge: the new quality rank (valid > absent > invalid) sits strictly below the existing tie-break key, which already contains `hosts` verbatim, so it can only re-break ties between rows whose family totals are identical.

### Added

- **`tokens_by_day` on `host-usage-snapshot`.** Additive, no schema bump. Omitted iff `hosts` is empty; otherwise always present, so its absence is the mixed-fleet version discriminator. All three readers emit it from the shared reduction. Older peers still accept; a malformed sibling is dropped and the family row kept, with a detail status and a fix clause in the dump. A day's totals must EQUAL the family totals for that day; its per-model values must be bounded by them — which is what stops a peer attributing more tokens to one model than the whole day recorded.
- **`--dump-host-usage` is in `mm retro-fleet --help` and the README command table.** It was the subsystem's only forensic tool and was hidden twice. The aggregator's argparse usage line still suppresses it so a novice's window-parse error does not advertise a maintainer flag.
- **Local per-model counts on `mm diag`.** Cache-only, passphrase-free, no host store. Distinct from the wire reader. Present in both `mm diag` and `mm diag --json`.
- **Writer-side caps on `by_model`,** mirroring the acceptor's, so a machine that legitimately used more models than the protocol allows still ships a row instead of losing its whole sibling on every peer. The breakdown is capped; the day totals are not, so `day_total - sum(by_model)` is usage not attributed to a shown model. A per-model consumer must carry that residual.

### Changed

- The shared `_add_usage` reduction now updates family totals and per-model day buckets atomically. `_CacheEntry.models` already interned the ids, so a warm v0.12.48 cache produces the new field without reopening source jsonl.
- Host-usage selection at equal `as_of` still uses the existing `tie_key`; a quality rank `valid > absent > invalid` only re-breaks ties older versions broke by file-iteration order. `tie_key` already contains `hosts` verbatim, so the winner's family totals are the same either way — the rank decides which sibling survives, never which numbers render.
- Premise sweep: a day bucket is a day's recorded work. `seen on N days` is still a lower bound, because a peer on an older mm still publishes last-touch totals and a machine that never pushed contributes no days — not because resuming a session erases the old day key.

### Fixed

- The `## Agent activity` footnote no longer claims a resumed session restates its whole total onto its last-active day. That sentence was byte-identical to pre-v0.12.48, four lines under the header that release corrected. It now scopes the caveat to the machines it still applies to: a peer on an older mm reports last-touch totals, so **its** token columns overstate the recent edge, and **its** day counts are lower bounds. The header calls these counters per-turn, which is only true from v0.12.48 forward.

## [0.12.48] - 2026-08-28

**Codex usage is now counted per turn, and stops counting the same work twice. Your Codex token numbers will drop by roughly half, and the smaller number is the correct one — do not read it as a decline in activity, and do not compare a window that straddles this release.** The reader kept one cumulative total per rollout file and attributed it to the day that file was last touched. But a rollout file is not a session: forking, retrying, or resuming a conversation writes a NEW file that repeats the old one's history. Measured on a 746-rollout corpus, 195 `turn_id` values spanned 244 files sharing 85% of their ledger, so **55% of the reported total was the same work counted once per file**. Codex tokens now land on the day they were actually spent, split correctly across a session that runs past midnight, with forked history counted once and divergent branches counted in full. The first `mm push` after upgrading rebuilds the Codex cache once (measured 801 ms for 694 MB); `mm status` and `mm diag` report the progress, and autopush converges over about three pushes without warming.

### Added

- **Cross-file turn dedup.** `turn_context.turn_id` is Codex's equivalent of Claude's `tail_msg_ids` and Grok's `_grok_terminal_key`. Readings are keyed by `(lineage, previous cumulative, current cumulative)`, so a shared prefix collapses while divergent branches both survive. Deduping readings rather than TRANSITIONS would treat one branch's tail as a waypoint on the other's and silently drop its work.
- **`mm diag` `host_usage.codex` block.** Cache state, reader state (`cold` / `migrating` / `ready`), rollouts cached vs on disk, and how many still await the one-off re-walk. Cache-only, so `mm diag` still runs without a passphrase. The block previously had exactly one key, `grok`.
- **`mm status` Codex line** during the rebuild, naming `mm push` as the way to finish it.
- **`migration`-specific failure copy.** That reason had been in the `Reason` vocabulary with no text of its own, so it inherited "A later substantive push will retry" — false on a quiet Mac, where the events tail only runs on a substantive push and autopush never warms the host cache.

### Changed

- **A day bucket is now a day's recorded work.** It used to be "the lifetime totals of every session that last touched this machine on that day", which is why a fixed day's value could DECREASE between snapshots. Buckets are additive and stable now. Grok has been per-turn since v0.12.47; OpenCode rows were always disjoint. All three readers finally agree.
- **A ledger seen before the first `turn_context` is buffered, not dropped**, and attributed to the first model the file names. The old skip was justified by "totals are cumulative, so a later attributable record restates it" — a premise this release deletes. On the live corpus that prefix is 1,557 records across 7 rollouts worth 209,515,399 input tokens. A file whose ledgers are ALL pre-context still refuses the store; the buffer must never rescue that case.
- **A resumed rollout no longer counts its parent's history.** Its opening reading uses `last_token_usage`, which excludes the inherited total (4 rollouts, 65,262,198 input tokens on the corpus).
- **`_aggregate` and `_aggregate_grok` are one reduction.** They differed only in how they reached `(day, model, usage)`.
- **The Codex cache entry stores observed readings plus a four-field resume carry** (`last_total`, `last_model`, `last_turn`, `pending`). Pre-Track entries are detected by the ABSENCE of `states` and re-walked once — deliberately not a `CACHE_VERSION` bump, which is shared with the Grok and OpenCode namespaces and would discard those too. Same call this repo made for `skills_by_day` (v0.11.27) and `offset`/`head` (v0.12.15).

### Fixed

- A repeated `token_count` record no longer inflates the total. Codex emits one on 183 of 746 rollouts (414 records), and also re-emits a turn's final reading verbatim as the next turn's first — 473,932 input tokens over a single 71-record file.
- The wire `hosts` payload is pinned to exactly the four token fields by test. `aggregator._copy_usage_bucket` rejects a day bucket on an exact key-set mismatch, and a rejected bucket fails the WHOLE snapshot row, so widening it would make every peer on an older mm silently drop the row and keep a stale one. Per-model detail ships as an additive sibling key instead.

## [0.12.47] - 2026-08-28

**Enabling Grok usage capture now actually publishes Grok activity, and a Grok read failure no longer freezes the rest of the host view.** Two wire-drift bugs (an absent `updates.jsonl` treated as an I/O error, and a usage-less `turn_completed` treated as an unreadable format) meant `mm enable-source grok` turned on a reader that published nothing. Both are skipped now, with a tally of usage-less turns on the local cache. Host-reader failures are isolated: a Grok format change drops Grok, declared, and Codex still publishes. Each Mac upgrades individually, **and upgrading is not enough** — run `mm enable-source grok` on that Mac, then one interactive `mm push` (the fast path; autopush never warms and converges over about three pushes). The retro card gains a `Grok models: seen on N days` line, not a token magnitude.

### Added

- **Reader-scoped host-usage failure isolation.** A failed reader is listed in additive `degraded_sources` (no schema bump) and named in the autopush `degraded` breadcrumb. A row is omitted only when no consulted reader completed. `no_metadata_ledger` stays silent.
- **`mm diag` `host_usage` block.** Consent, prior-success state, and the usage-less skip tally, from on-disk cache only — no passphrase, no host store.
- **Grok wire-contract census** against Grok 1.0.5 (`tests/fixtures/host_sessions/grok/CONTRACT.md`), with fixtures for a usage-less terminal, a cancelled terminal that does carry usage, `usageIsIncomplete`, and a session dir without `updates.jsonl`.

### Changed

- **An absent optional file is not an I/O error.** `_is_regular_non_symlink` treats `FileNotFoundError` / `NotADirectoryError` as a skip. A permission error on a file that exists is still `io_error`. Shared with the Codex walker, so a rollout reaped between `iterdir()` and `lstat()` no longer takes the Codex scan down.
- **A usage-less `turn_completed` is a zero-token skip**, carved out *before* the exact-match key check (the check would otherwise fire first and the skip would be dead code). Exact-match on extra unknown keys stays fatal. Skip count is stored on the Grok cache for `mm diag`.
- **`mm status` reports Grok capture outcome, not config:** `enabled; a prior scan completed successfully` vs `enabled, but no successful scan yet — run mm push`.
- **`mm enable-source grok` prints the usage-read disclosure** on the fresh-enable path (it already existed, and was unreachable from every path that granted the permission).
- **Permanent host-reader failures carry a fix clause** and never promise a retry. The first-success Grok carve-out is removed from sweep policy; `grok_completed_once()` remains a diagnostic.

### Fixed

- Enabling the Grok source advertised a feature that was guaranteed to publish nothing: one session dir without `updates.jsonl` zeroed the whole scan, and four usage-less cancelled turns would have been `unsupported` even after that.

## [0.12.46] - 2026-08-27

**Fleet retro can now recover omitted git history, and a held cursor no longer pretends recovery is impossible.** Incomplete discovery still holds the git cursor (the next substantive push re-walks from the last complete capture), but the mm-push row is still written so diagnosis and author attribution survive. Walk failures are visible on `mm status` and never hold the cursor — git-walk cost grows with cursor age (48.6 ms @1d → 251.3 ms @30d vs a 250 ms autopush budget), so holding on a walk abort wedges unattended autopush. `mm recapture [WINDOW]` (default 30d) is the dedicated recovery command. The first push after upgrading may take a one-off longer git walk (measured 251 ms) because a held or first-run 30-day cursor escalates from the 250 ms autopush budget to 500 ms so that recovery actually completes.

### Added

- **`git_capture` on the mm-push row.** Optional nested key: `since`, `discovery` (`complete` / `partial` / `empty` / `not-run`), `walk_budget_aborts`, `walk_errors`. Absence is the version discriminator (fail open: ADVANCE).
- **`mm recapture [WINDOW]`.** Git-only recapture at interactive budgets, then the ordinary push path. Default `30d` (same as `mm init`), min `1d`, max `90d`. `--dry-run` walks and reports without writing. Partial recovery exits 4. Zero discovered repositories writes nothing and exits 1. Safe to re-run — commits dedup fleet-wide on `(remote, sha)`. Retros window by the commit's date, not by when mm captured it.
- **`mm diag` `git_capture` block.** Recorded last mm-push projection (`ts`, `mm_version`, capture state, `since`, error counts) beside the fresh discovery probe. Never renders `local_emails`. Peer-controlled strings are sanitized and length-clamped.
- **`mm status` recovery nag.** Names `mm recapture Nd` when the last recorded capture is incomplete.

### Changed

- **Cursor scan is the 90-day retention window**, not 31 days. A complete push 45 days back is found instead of jumping forward to the 30-day floor. `git_capture.since` keeps a still-older gap explicit.
- **Git-walk budget escalates** from 250 ms to 500 ms when the resolved cursor is older than a day. Session-walk budget is unchanged.
- **Discovery-budget phrase** names `mm recapture 30d`. Omitted commits are recoverable.
- **Empty `git init` directories** (`rc=128` + "does not have any commits yet") are a benign `no_commits` skip and do not degrade the push. Per-repo `TimeoutExpired` does.

### Fixed

- Git-walk repository loss (`budget_abort`, timeout, git errors) was computed nowhere the user could read. `walk_exceeded_budget` measured the session-metadata walk (deadline set after the git walk). Renamed to `session_walk_exceeded_budget`.

## [0.12.45] - 2026-08-25

**Fleet retro now finds the repositories that are actually on this Mac.** Git-root discovery no longer spends the autopush budget proving deleted directories are not repositories: a `.git` + `HEAD` / `gitdir:` sniff replaced the per-candidate `git rev-parse` subprocess (the `.git`-file case that used to need the subprocess — Conductor worktrees — is still accepted). The dead gstack prober is gone. `mm diag` reports discovery at the autopush budget; `mm status` nags when a push recorded incomplete discovery; `last-autorun.json` is keyed per verb so autopull cannot erase a degraded push. This fix is not retroactive — earlier windows stay incomplete — and every Mac must upgrade individually.

### Added

- **`mm diag` `discovery` block.** Runs git-root discovery at the 50 ms autopush budget (not the 100 ms interactive one). Text: `~`-relative roots, reject counts by reason (`gone` / `not-a-repo` / `unreadable`), distinct "no prober ran" vs "found nothing". `--json` carries full path arrays. Paths go through `safe_str`.
- **`[retro] repo_roots` validation.** Must be a list of non-empty absolute (or `~`) paths. A bare string used to iterate characters; an empty string used to silently make the process CWD a root.
- **Copy-pasteable `[retro] repo_roots` block in README.** Per-machine, not synced, checked before automatic discovery, verified with `mm diag`. After deleting the gstack prober this is the only discovery mechanism on a machine where `claude` is disabled.
- **Aggregator Notes line** when any device in the window captured 0 repositories on some of its pushes. SKILL.md: a commit count is a lower bound; do not narrate a trend off a zero-repository capture.

### Changed

- **Git-root classification is a stat, not a subprocess.** `_classify_git_root` accepts a `.git` directory containing `HEAD`, or a `.git` file whose first line is `gitdir:` pointing at a git dir with `HEAD`. Wrapped in `try/except OSError` because Python 3.11 `Path.exists()` raises `PermissionError` on an unreadable directory (CI is 3.13 and would not have caught this). Prober `validate()` moved inside the prober `try` so one unreadable candidate cannot swallow the entire events tail.
- **Discovery degradations.** A prober exception (`errors and not exceeded`) and complete discovery that found zero repositories now reach the `degraded` breadcrumb. An ordinary rejected candidate stays silent. The budget notice no longer says "A later substantive push will retry" — a retry does not recover the lost interval. Action is `mm diag`.
- **`last-autorun.json` is keyed per verb** (`{"push": {...}, "pull": {...}}`). `mm status` renders both. Legacy single-object files still read.
- **Aggregator discovery-error Notes** point at `mm diag`, not at `mm: notice:` stderr breadcrumbs that an unattended hook discards.

### Removed

- **`_probe_gstack` and `_is_git_toplevel`.** Every live `repo-mode.json` carries `{mode, top_pct, authors, total, computed}` — none of the keys the prober read. The subprocess pre-gate agreed with the stat classifier on 60/60 live candidates and cost ~8 ms each.

## [0.12.44] - 2026-08-25

**Deleting a `retro-fleet` skill link now sticks.** `rm ~/.codex/skills/retro-fleet` is the whole procedure; the next `mm push` leaves it deleted instead of rebuilding it. `mm` still repairs a link that is present and wrong — dangling after a store move, or pointing at an old package path — so self-heal is unchanged for actual damage. Only an absent link, for an agent `mm` has installed for before, counts as your decision. `mm install-skills` is the undo. The promised `mm uninstall-skills` command is not shipping: it existed to make a removal survive the installer, and an installer that does not resurrect needs no inverse.

### Added

- **`mm diag` splits `removed-by-user` from `absent`.** A link you deleted and one that never existed were the same row before; only one of them is answerable with "run `mm install-skills`". Neither is a broken status.
- **Shell completion.** `mm --install-completion` / `mm --show-completion` now work. Agent keys were previously enumerated to the user only inside an error message.

### Changed

- **`mm push` no longer recreates a skill link you deleted.** The 24h drift gate stops treating an absent target as damage when a success marker proves `mm` installed there. That marker was already written on every successful check, so no new state is introduced. The gate also stops staying permanently open for such a row — previously the absent-target `lstat` failed open on every push forever, for a row whose only possible outcome was a no-op.
- **Consent and presence are independent.** Re-enabling an agent's source, or re-granting it with `mm install-skills --agent`, does not resurrect a link you removed. Flipping a source off and on used to silently undo a deliberate deletion.
- **README:** new *Removing a skill link* section; the *Uninstalling* block now says up front that it is for removing `mm` itself and points single-link removal elsewhere. The copy-pasteable link loop is unchanged and still works with no `mm`, no config, or a broken config.

### Removed

- **The one-time 0.12.42 skill-link policy notice**, its marker check, and the `mm status` line that repeated it. It explained that `mm` no longer repairs an unauthorized agent's link; the README troubleshooting entry says that permanently and was kept. Two of three machines on this fleet never ran a version that could emit it and would have upgraded straight past it — a transient stderr notice is the wrong carrier for a fleet that skips releases. Any leftover `~/.config/mind-meld/.skill-link-policy-v0.12.42` marker is inert.

## [0.12.43] - 2026-08-24

**`mm diag` now reports whether Grok can load `retro-fleet`, and `mm` no longer pretends a skill link is how that host works.** Grok 1.0.5 already discovers `~/.claude/skills` via default-on Claude compatibility, so mm maintains no Grok `AgentRow`. `mm diag --json` gained a sibling key `host_skill_discovery`; `skill_links` stays the three links mm owns. The dead one-agent installer copy is gone, `may_create` is required on the writers, and `mm status` / `mm install-skills` name the restart and the real error.

### Added

- **`mm diag --json` `host_skill_discovery`.** A 2s `grok inspect --json` probe (argv, no shell, capped stdout) extracts the Claude/skills compat bit, whether `retro-fleet` resolved, the resolved path, and the Grok version. Five explicit failure states: `binary-absent`, `timeout`, `nonzero-exit`, `malformed-json`, `unsupported-schema`. `mm diag` only — never status, push, or autopush. Not a `skill_links` row.

### Changed

- **`may_create` is keyword-required on `_ensure_retro_skill_links` and `_skill_links_check_due`.** A forgotten kwarg is a `TypeError`. `None` still means allow-all (fresh-machine intent). `diagnose_skill_links()` with no policy is `unknown (policy not resolved)`, not `enabled`.
- **`mm status` names the first broken link's cause and fix** through `render_skill_status`, including "restart the agent so it reloads SKILL.md". The same restart clause is on the three installer messages that already pointed at `mm install-skills`.
- **`mm install-skills` error lines are `error:`, not `notice:`**, name a config that could not be read, and point at `mm diag`. The nothing-was-installed line is pasteable (`mm install-skills --agent claude`) and points at `mm diag` rather than `<claude|codex|opencode>`.
- **README and `host-parity.md` Plan C** lead with the Claude-compat fact. Grok appears under `host_skill_discovery`. Registry exit criterion: mm maintains a skill link only for hosts that do not discover `~/.claude/skills` (verified 2026-08-24 with `grok inspect --json`).

### Removed

- **`_ensure_skill_target` and `_ensure_retro_skill_link_at`.** Zero production callers. The real-home write guard is now pinned on `_ensure_retro_skill_links`, which is the path `mm push` actually takes.

## [0.12.42] - 2026-08-23

**`mm` now maintains each `retro-fleet` skill link only when that agent is authorized by its local skill-link policy.** Reads of a host's local store were already gated on `get_sources`; writes into that host's skills directory were not. By default, policy derives from enabled sources; `[skills] maintain_links` and the optional exhaustive `[skills] agents` allowlist can override it. `mm install-skills --agent KEY` grants link maintenance without enabling sync or usage reading. Existing links stay in place; `mm uninstall-skills` is next.

### Added

- **`[skills] maintain_links` and `[skills] agents`.** Strictly validated at load. `maintain_links = false` disables every row. An explicit `agents` list suppresses source derivation the same way an explicit `[[sync.sources]]` suppresses `DEFAULT_SOURCES`. `agents = []` is a `ConfigError` pointing at `maintain_links = false`. Unknown names are accepted and inert.
- **`mm install-skills --agent KEY`** (repeatable). Writes `maintain_links = true` and `agents =` the current effective set plus KEY, preserving unknown future-agent grants, then installs every authorized agent. Refuses and exits 1 if it cannot persist the grant. Does not enable source sync or usage reading.
- **One-time upgrade notice** on the first interactive push after 0.12.41, naming every declined agent that still has an mm-owned link. `mm autopush` cannot spend that marker. `mm status` shows the pending transition until acknowledged.

### Changed

- **Installer and TTL gate share one consent set.** A declined row is classified before any `stat`, never touches its success marker, and is silent on push. `declined` is not a broken `mm status` state. `mm diag` carries a separate `maintain_links` policy field; `status: ok` and `maintain_links: disabled` can coexist when a leftover link still resolves.
- **Store publish is not gated on agent consent.** An all-declined machine still refreshes an mm-owned store its surviving links point at, and does not create a store if none exists.
- **`init` and `_push_core` resolve sources before the skill hook** so consent is known. The hook itself stays between device registration and the events tail.
- **`mm install-skills`** skips declined rows with a remedy line and exits 0 when every available agent is declined. A broken config fails closed and exits 1. Bare invocation with no config still installs for every available agent (fresh-machine setup).

### Fixed

- Unconsented writes into `~/.codex/skills/` and `~/.config/opencode/skills/` under the default source-derived policy.

## [0.12.41] - 2026-08-23

**`/retro-fleet` now refuses up front when `mm` is missing or broken, and the README names the missing-block failure that Step 0 cannot reach.** A preflight that lived inside a skippable Step 1 is not a preflight; Step 0 is unskippable and silent on a healthy machine. Step 0 reaches a user only when the store republishes, which requires an `mm` at least as new as the store — a user who never upgrades never receives it. Step 0 prevents future drift; it does not cure current drift. The new README troubleshooting entry is the remedy for that population.

### Changed

- **SKILL.md Step 0 preflight.** Within Step 0, stage 0A (`command -v mm`, then `mm --version`) is the only thing that can stop the run, and a missing or broken binary does not proceed into Steps 1-5. The rule scopes to Step 0 only — Steps 1-5 keep their own failure contracts, so a malformed `mm retro-fleet` in Step 2 is still fatal. Stage 0B relays an upgrade notice from Step 1's `mm push` when one appears, names the consequence (absent blocks are unmeasured, not zero), and quotes `upgrade.INSTALL_CMD`. Silence is not evidence of freshness. Every remediation ends with "restart the agent so it reloads SKILL.md."
- **Step 1's skip clause names Step 1.** An "offline" or "stale" request no longer drops the binary probe. The rotting `v0.12.37` floor literals are gone.
- **`docs/invariants/events-retro.md` records the Step 0 contract.** The terminal rule scopes to Step 0 only, `0B` relays mm's existing upgrade nudge rather than comparing versions, and the cut version-comparison stage is written down with why so it is not reintroduced. `AGENTS.md`'s routing row points SKILL.md edits at it.
- **README troubleshooting.** New first entry for a missing, empty, or older-than-expected retro. The `mm: command not found` symptom is rewritten for the up-front refusal. `mm diag --json` is documented as the fields it actually emits (`key`, `agent`, `target`, `store`, `store_state`, `status`, plus `store_version` on successfully diagnosed rows), not as the loaded SKILL.md or the upgrade-check outcome. Agent-count-bound prose at four sites is gone; the uninstall loop stays literal and admits it.

- **`docs/shared-infra.txt` tells the roadmap packer which paths are not collisions.** `CHANGELOG.md`, `docs/PROGRESS.md`, and the roadmap files are listed. `pyproject.toml` is deliberately excluded: `release.yml` force-advances the `latest` branch unconditionally, so two Tracks claiming one version would publish an untagged commit under a released version. Letting the packer serialize release-bearing Tracks is the guard.

### Fixed

- **A missing `mm` no longer produces a plausible retro.** Step 1 told the agent not to treat a non-zero exit as fatal, then ran `command -v mm` in the same block as `mm push`.

## [0.12.40] - 2026-08-22

**Adding a supported agent is now one `AgentRow` in `skill_link.AGENT_ROWS` — descriptors, test isolation, the real-home guard, diagnosis, and `mm install-skills` all pick it up.** A fourth row can no longer vanish from isolated tests via a silent `zip` truncation, and `mm diag --json` rows gained a `key` field.

### Changed

- **`AGENT_ROWS` is the one table.** Every skill-link consumer derives from it. Canonical `~`-relative roots live on the row; tests redirect via an empty-by-default override map that the real-home guard never reads.
- **`mm diag --json` rows include `key`.** Healthy and `error` branches emit the same schema, so a wedged link still names its registry row.
- **`mm install-skills` names no agent count.** The unavailable message is now "no supported agent skills directory exists", and `--help` says it covers every agent mm supports.

## [0.12.39] - 2026-08-21

**Fleet retros now compare this window to the prior equal period from the synced events corpus, so the trends section is fleet-deterministic for the first time.** The v0.12.0 machine-local snapshot cache compared against whenever you last typed the command, fired twice in 106 days, and was wrong both times. Snapshots were never synced, so an upgraded Mac and an old one produce different trend sections from the same corpus until both upgrade — that is the pre-existing non-determinism being fixed, not a new bug.

### Changed

- **`## Trends vs prior <N>d`.** A four-row `prior | current` table (commits, lines added, lines removed, active days) computed from events already in memory. Renders below Code shipped, only when the window is shorter than 14d (week-over-week already owns longer windows), identically in both skill passes. Unavailable coverage renders the heading with the reason inline rather than fabricating growth from an empty baseline.
- **`--no-save` is a hidden no-op.** Kept so `mm retro-fleet 30d --no-save > /tmp/retro.md` still exits 0 and a stale skill-store copy of SKILL.md does not fail Step 4 once per upgrade. Passing it prints one `mm: notice:` to stderr naming this release as the start of the removal window.
- **`mm gc` reaps leftover snapshot files** matching `YYYY-MM-DD-NNN.json` under `~/.local/share/mind-meld/retros/`, then removes the directory if empty. Never `rm -rf`. Dry-runnable.

### Fixed

- **Trends stay deterministic across timezones and duplicate event copies.** Active-day keys and period labels use UTC, and an out-of-window copy of a SHA can no longer hide its valid current-period copy.
- **Unreadable mm-event records no longer masquerade as a known-zero prior period.** Trends now render unavailable rather than producing a table against incomplete data.
- **Current Typer/Rich help rendering no longer makes the hidden `--no-save` compatibility test fail in CI.**

### Removed

- The snapshot subsystem (`_save_snapshot` / `_load_prior_snapshot` / `MM_RETROS_DIR` / the 365-day snapshot reaper). Trends no longer depend on this machine's command history.

## [0.12.38] - 2026-08-21

**Agent skill links now point at an mm-owned store instead of whichever interpreter ran `mm`, so a destroyed Conductor workspace or a Homebrew Python bump can no longer take retro-fleet offline.**

### Changed

- **Durable skill store.** `mm` copies `SKILL.md` (only) into `~/.local/share/mind-meld/agent-skills/retro-fleet/` and points the Claude Code, Codex, and OpenCode `retro-fleet` links at that constant path. The path does not move when you upgrade mm, switch between a pipx install and a development checkout, or delete a virtualenv.
- **Links mm previously wrote are repaired by liveness.** A dangling link into a deleted install or workspace is re-pointed at the store. A *live* development-checkout link is left alone on `mm push` with a notice naming the remedy; `mm init` and `mm install-skills` re-point it. Anything mm did not write -- your own file, directory, or a symlink it does not recognize -- is never replaced.
- **`mm autopush` classifies but does not rewrite agent config.** A first-of-its-kind mutation to three third-party config directories does not debut on an unattended hook. Run `mm push` or `mm install-skills` to repair.
- **`mm diag` reports skill links.** Passphrase-free, one block, with raw `readlink` output and the store's published version. `mm status` prints one line only when a link is in a state mm can act on.
- **`mm install-skills --help` no longer claims the link auto-updates on `pipx upgrade`.** Because the store is a copy rather than a link into the package, an upgrade no longer refreshes it instantly: the next `mm push` republishes on a version-then-hash compare, or run `mm install-skills` to do it now.

### Fixed

- **A wedged `retro-fleet` link is now a classified, repairable state** (`dangling-ours` / `dangling-ours-legacy`) instead of a generic "exists and is not mm's symlink" refusal that named neither the cause nor the fix. This was the actual outage: on a machine where mm had been run from a Conductor workspace, all three agent links pointed into a directory Conductor had since destroyed, and mm refused to repair them while reporting the user had planted their own file there.

## [0.12.37] - 2026-08-18

**Fleet retros now show which coding agents you actually used, as activity rhythm rather than token counts, because a token count from an agent log cannot be honestly compared to a Claude session total.**

### Added

- **`AGENT LOGS` share-card block.** Reports per-model-family activity rhythm from local Codex, Grok, and OpenCode logs — `Codex models: seen on 5 days` — plus `N of M machines with agent activity`. One family per line, no token magnitude in any state. Rows are canonical model families rather than agents, because the synced snapshot carries no reader-to-family attribution; the Codex and OpenCode readers both classify GPT models into the same family, and a Claude model run through OpenCode renders as `Claude (via agents)` so it can never be confused with the Claude Code row above it.
- **`## Agent activity` body table.** Per-machine `Tokens (last 90 active days)` and `Tokens in this window`, with a row for every known machine including those that have never published a snapshot. Never summed across machines: device ledgers are not provably disjoint, since a home-directory migration plus a fresh `mm init` yields two device ids carrying overlapping history with no signal mm could detect. Cross-machine rhythm uses a day-set union instead, which is idempotent under exactly that duplication.
- **Named causes instead of a silent gap.** When the block is quiet, `## Notes` says which of no-snapshot / no-reader-contribution / all-stale / nothing-active applies, and what to do about it. An empty contributor list may mean no enabled source or no attributable local ledger, so the note names that ambiguity instead of guessing. A vanished block is never the diagnostic.

### Changed

- **`MODELS` block states its own provenance in the header** (`MODELS (Claude Code sessions)`), and the separate `Coverage:` line is gone. A line saying "only" that scopes just the rows above it would contradict the sibling block below it. Its empty state is scoped to match (`No Claude Code model usage observed`).
- **`mm enable-source` documents the consent coupling.** Enabling `codex`, `grok`, or `opencode` also authorizes that host's local usage reader, which is the `AGENT LOGS` block's only precondition and was previously undiscoverable from the CLI.
- **`retro-fleet` checks its own version before reporting an absence.** A stale `mm` renders a valid-looking retro with no `AGENT LOGS` block, which is indistinguishable from no activity; the skill now reads `mm --version` first.

### Fixed

- **The host-snapshot acceptor no longer hardcodes the schema version.** It read a literal `2` while the writer used `EVENTS_SCHEMA_VERSION`, so the first bump would have made mm reject its own freshly written rows fleet-wide.
- **`docs/invariants/events-retro.md` no longer mischaracterizes host day keys.** They were documented as "last-touch lifetime totals"; each rollout file actually contributes one cumulative terminal keyed to that file's last-touch day, making the map a per-day distribution. The corrected text names both consequences: a window slice of magnitude over-counts at the recent edge, and a count of active days is a lower bound because resuming a session erases the earlier key.
- **Rhythm counts ignore backdated snapshots.** Day keys are clamped to `min(until, as_of)`, so a peer shipping an old `as_of` alongside in-window day keys — accepted by the wire contract, and verified constructible — cannot inject activity.
- **The rejected-snapshot breadcrumb counts machines, not rows,** because rejects are not window-scoped: one malformed writer would otherwise light a row count on every 7-day retro until 90-day retention reaped the file.
- **Documented that `## Trends vs last retro` only reaches the first-pass output.** README and the skill both described it as appearing on repeat runs.

## [0.12.36] - 2026-08-18

**Fleet retros now retain each device's latest complete host-usage snapshot as inventory, so host totals stay truthful without masquerading as window spend.**

### Added

- **Last-known-good host inventory.** `retro-fleet` accepts strict `host-usage-snapshot` rows and preserves the newest complete per-device view, including source coverage and staleness state. Host-family day maps remain lifetime last-touch totals; they are never sliced or summed into fleet spend.
- **Forensic host-inventory dump.** Hidden `mm retro-fleet --dump-host-usage` prints the inventory JSON and exits before rendering the Markdown retro, giving operators a safe way to inspect accepted snapshots.

### Fixed

- **Fleet-retro isolation.** Host inventory cannot affect Claude session totals, cost estimates, the existing MODELS card, or the normal retro output and prompt sidecar.

## [0.12.35] - 2026-08-17

**Mind Meld can now sync your Grok customizations with `mm enable-source grok`, without syncing Grok sessions, credentials, or prompts.**

### Added

- **Scoped Grok sync source.** The source is limited to `~/.grok/skills/`, `commands/`, and `rules/`. Grok session data, `auth.json`, `config.toml`, bundled files, and linked or hard-linked files stay local.

### Changed

- **One Grok opt-in, with a compatibility bridge.** Enabling Grok adds its one `[[sync.sources]]` row and keeps the existing host-usage opt-in enabled, so prior usage-only consent continues to publish token totals. Fresh installs and legacy fallback only default Grok on when an allowed customization directory exists; a bare `~/.grok` is never consent.
- **Source management preserves existing sync.** Enabling Grok from a legacy configuration materializes the current source set before adding Grok, so existing Claude content remains synced instead of becoming deletion tombstones.

### Fixed

- **Strict Grok source boundaries.** Source aliases cannot bypass the hardcoded allowlist, and reconfiguring sources no longer clears a prior Grok usage opt-in by accepting defaults.

## [0.12.34.1] - 2026-08-17

**Host interchangeability is now a written contract: Claude, Codex, and Grok share one usage card, not one sync tree, and session transcripts stay local on every host.**

### Added

- **Host-parity design.** `docs/designs/host-parity.md` names what 18D/21A already are, why `~/.grok` is not a sync source, and the three follow-ups: MODELS-card rows (22A/23A), a later `grok-custom` allowlist (never named `grok`), and a Grok `retro-fleet` skill-link target. Codex/Grok sessions-snapshot and transcript sync stay refused.

### Changed

- **SPEC and invariants now match that contract.** Goal 8, the enable-source grok note, source-type list, events-retro consent pin, and the Claude-only sessions-snapshot section all point at host-parity so a later track cannot mint a fake `grok` sync source or put encoded cwd on the wire.
- **Current plan is Groups 22 and 23.** Host snapshot merge and grok-custom in parallel, then MODELS card and Grok skill-link. The host-parity inbox is drained.

## [0.12.34] - 2026-08-17

**`mm enable-source grok` now publishes this Mac's Grok Build token totals on the same encrypted host snapshot as Codex — without syncing session files.**

### Added

- **Opt-in Grok v1 usage reader.** After `mm enable-source grok`, attended `mm push` and `mm init` read only `turn_completed` terminal records from local `updates.jsonl` (under `GROK_HOME/sessions` or `~/.grok/sessions`). Each accepted turn is a per-prompt total; `reasoningTokens` stay inside `output`. Session files, prompts, and chat history never leave the Mac.
- **Usage-only enable/disable.** `mm enable-source grok` / `mm disable-source grok` flip `[retro].grok_host_usage`. They do not add a `[[sync.sources]]` row. A config that already has a source named `grok` is refused at load.

### Changed

- **First-success Grok misses no longer take Codex hostage.** Until a consented scan has seen at least one `updates.jsonl`, a Grok `deadline` / lock / I/O miss drops Grok and still publishes the other hosts. After that first success, those reasons veto the whole snapshot again. `malformed` / `unsupported` / `stale` always veto when Grok was invoked.

### Fixed

- **Unknown Grok terminal keys fail closed.** An extra non-content field on `turn_completed` withholds the host snapshot instead of publishing a complete row that silently omitted every real turn.
- **Grok oversize notices no longer print session paths.** Encoded cwd and session ids stay off stderr.

## [0.12.33] - 2026-08-16

**Pushes now publish aggregate non-Claude host token usage to the fleet, for the hosts you actually enabled, and only when the sweep can be trusted.**

### Added

- **`host-usage-snapshot` event row.** The push tail and `mm init` backfill emit an additive row carrying per-host-family, per-UTC-day token totals plus the days present. It ships between the session rows and the terminal `mm-push`, is capped at the same 90-day window as Claude token history, and is omitted entirely when a host could not be read — an absent row means "unknown", never zero.
- **Consent gate on host readers.** A host's local usage records are read only when you have enabled that host as a sync source, matching how the Claude session walk has always been gated. Declining `codex` means `~/.codex/sessions` is never opened.
- **One-off host cache warm.** An attended `mm push` or `mm init` may spend a single bounded warm when the per-push budget cannot fit a cold scan; unattended `autopush` never does, and instead converges across pushes.

### Changed

- **A source that keeps no usage ledger is absent, not failed.** Grok's transcript-only store and OpenCode's legacy message files now report `no_metadata_ledger` and are dropped from the row's `token_sources` instead of suppressing the whole snapshot. Genuine read failures still withhold everything. Before this, merely having Grok installed made the row unpublishable on that machine forever and pinned `mm status` at `degraded`.
- **Ordinary Codex rollout shapes no longer refuse the whole store.** A `token_count` with no usage payload (33% of rollouts on a real machine) and a ledger preceding the first `turn_context` are skipped rather than treated as fatal; a rollout with no ledger contributes nothing. Measured before the fix: 167 of 452 rollouts failed and the reader gave up on the first one in 5ms.
- **An interrupted host scan keeps its per-file progress.** Cache persistence is now separate from whether the scan may be published, so a corpus too large for one bounded read converges over a few pushes instead of re-parsing the same prefix forever.

### Fixed

- **Withheld host snapshots reach `mm status`.** An incomplete sweep appends a safe `degraded` breadcrumb naming only the reader and reason class, and states that content sync and git/session capture are unaffected.

## [0.12.32] - 2026-08-16

**Conflict resolution now keeps its safety contract clear: merge remains an explicit choice, and a failed merge still lets you resolve the next file.**

### Changed

- **Clearer resolver maintenance seam.** The resolver now uses its existing manifest dependency consistently, with flatter merge control flow and matching invariants that preserve the skip-by-default prompt policy.

### Fixed

- **Merge failure progression is pinned.** A failed write leaves its conflict untouched and continues to the next conflict instead of risking a false successful resolution.

## [0.12.31] - 2026-08-16

**Conflict review now has one terminal-safe diff renderer and one compatibility path for the retired `b` / `both` choice.**

### Changed

- **Shared conflict presentation and normalization.** Inline pull and `mm resolve` retain their established 60- and 80-line review windows, respectively, while using the same terminal-safe rendering, binary-diff hint, overflow treatment, and exact-match legacy skip-alias behavior.

## [0.12.30] - 2026-08-16

**Mind Meld can now collect completed OpenCode usage locally through a read-only metadata projection, without inspecting conversation content.**

### Added

- **Safe OpenCode host accounting.** The host reader opens the modern OpenCode SQLite store read-only and query-only, projects only terminal metadata and token counters, attributes usage by UTC day, and combines reasoning with normal output in Mind Meld's single output bucket. It fails closed on locks, malformed rows, unknown terminal states, migrations, changing sources, and zero-usage terminal claims.

### Changed

- **Transcript-only sources stay local.** Grok's persisted conversation/tool-call stream and OpenCode's legacy message files are explicitly refused rather than parsed. Grok usage remains unavailable until it publishes a persisted, versioned metadata-only terminal ledger with model, UTC completion, and token counters.
- **Host-family classification is strict.** Only documented Codex model identifiers enter the Codex bucket; arbitrary names containing `codex` remain unclassified.

## [0.12.29] - 2026-08-16

**Fleet retros now show observed model-family token usage in their second-pass share card, without pretending session snapshots prove model-level delivery.**

### Added

- **Model-family share-card rows.** The `MODELS` block groups existing Claude Code session token buckets through Mind Meld's canonical Claude, Codex, Grok, and Unclassified classifier. It excludes synthetic and empty observations, keeps the card width-pinned, and names Claude Code snapshot coverage directly.

- **Separate delivery context.** The card now includes one global detected GitHub PR-reference total. It remains a fleet aggregate, never a model-family total or verified merge claim.

### Fixed

- **Partial token coverage is visible and actionable.** Older session schemas and cold token caches now mark model totals incomplete, name affected peers compactly in Notes, and direct the operator to run `mm push` or upgrade. Aggregated safe token counters keep their full fleet total rather than being capped again during display.

## [0.12.28] - 2026-08-16

**Fleet-retro capture now honors a small, independent root-discovery budget without pretending an incomplete scan is complete.**

### Changed

- **Bounded, call-scoped git-root discovery.** Push and init reuse one immutable discovery result across snapshot capture and identity gathering. Autopush receives a 50 ms budget and interactive push/init receive 100 ms; successful roots remain usable when the deadline expires, while incomplete observation stays visible in the forensic event and unattended status breadcrumb.

- **No fabricated content heartbeat.** An empty push, including after a UTC rollover, leaves the retro cursor and events untouched. The next substantive push captures the full idle interval exactly once; local autorun freshness still means only that the hook ran.

### Fixed

- **Incomplete identity observation cannot poison the cache.** A partial root result can contribute identities to its current event, but never rewrites the seven-day identity cache or its freshness marker, so a later complete refresh can prune removed identities.

## [0.12.27] - 2026-08-16

**Project instructions now have one shared source for Claude Code, Codex, OpenCode, and Grok Build.**

### Changed

- **Multi-host instruction layout.** `AGENTS.md` is now the canonical project instruction file, while `CLAUDE.md` is a relative symlink to it. Existing instruction text is unchanged.

## [0.12.26] - 2026-08-16

**Mind Meld now treats every peer-reachable diagnostic as literal terminal text, so a synced value cannot rewrite your clipboard, clear your screen, or spoof Rich formatting while you inspect a failure.**

### Fixed

- **Terminal-safe final renderers.** Remaining Rich and plain-stderr sinks now strip terminal control sequences and escape markup at display time, including status rows, dropped-device warnings, typed unattended-hook errors, event discovery failures, and mm-events bootstrap warnings. Raw exception context remains available in private forensic logs.

- **Unattended commands keep their distinct truth.** `autopull` and `autopush` share setup, lock, and error handling without collapsing their verb-specific outcomes. Regression coverage preserves quiet missing-config and lock paths, fleet refusal, no-sources, and degraded-tail reporting.

- **Init no longer acts on stale observations.** Source prompts use one filesystem detection snapshot for their copy and default, and crypto bootstrap losers re-fetch then reject missing or corrupt race winners instead of trusting the stale pre-race result.

## [0.12.25] - 2026-08-15

**`mm push` and `mm init` now capture their fleet-retro snapshots through the same path, so the two flows stay in lockstep without changing their existing behavior.**

### Changed

- **Shared event capture for push and init.** Git discovery, bounded git capture, device stamping, session snapshot assembly, cache-lock lifetime, and walk-budget measurement now live in one private seam. Push keeps its token-cache policy, identity capture, `mm-push` row, and degradation breadcrumb; init keeps its 30-day backfill, token/identity warming, and snapshot-only write.

- **Pinned the refactor's edge cases.** Regression coverage now proves cache warming and identity work do not consume the session budget, one lock spans every enabled Claude root, cache contention remains visible to unattended autopush, and init passes the same explicit cursor to both git and session capture.

## [0.12.24] - 2026-08-15

**The bundled `retro-fleet` skill installer now tells the truth about every supported agent instead of letting one partial failure disappear behind another success.** Claude Code, Codex, and OpenCode each receive an independent outcome, and `mm install-skills` exits nonzero whenever an available agent could not be installed safely.

### Changed

- **Descriptor-driven three-agent installation.** A single call-time target registry now carries every agent root, skills directory, display name, and marker pair. The installer returns one ordered result per agent: `installed`, `unchanged`, `unavailable`, `conflict`, or `failed`.
- **Agent availability and repair gates share one predicate.** An absent agent root is skipped; an installed agent whose `skills/` directory is missing is immediately eligible for repair even with a fresh legacy marker. Dry runs perform no source resolution or filesystem mutation.
- **`mm install-skills` reports partial installs precisely.** Successful agents remain visible alongside conflicts and failures, all user-facing filesystem paths are terminal-safe, and any available conflict or failure returns exit code 1.

### Fixed

- **A dangling skill symlink is never unlinked automatically.** It is now a conflict that requires a deliberate user removal. This preserves the installer’s no-clobber guarantee if a concurrent process replaces the dangling link with a file or foreign symlink.
- **A skill-installer regression cannot abort `mm init` or `mm push`.** Both load-bearing flows retain a forensic notice boundary and continue their normal initialization or event-backfill work.

### Tests

- Added outcome, partial-install, gate, dangling-link no-clobber, CLI exit, and init/push exception-containment regression coverage for all three targets.
## [0.12.23] - 2026-08-15

**`mm gc --dry-run` now shows exactly what retention cleanup would attempt without touching the files it inspects.** Preview temporary upload remnants, stale event files, stale token-cache entries, and, with `--conflicts`, stale conflict sidecars before deletion.

### Fixed

- **Truthful retention previews and outcomes.** Every executed reaper emits a stable result line, including zero counts and planned token-cache repairs. Apply mode distinguishes successful deletions from failed or skipped work; use `mm gc -v`, fix permissions or locks, then rerun if a reaper needs attention. Token-cache inspection holds a shared read-only lock and never creates, rewrites, re-permissions, or normalizes the cache during preview.

- **Best-effort reapers no longer overstate cleanup.** Temporary, event, and conflict deletions count only after `unlink()` succeeds. A failed cleanup does not prevent the remaining reapers or orphan-blob GC from running. `mm push` keeps its concise auto-GC output.

## [0.12.22] - 2026-08-15

### Added

- Retro snapshots now record distinct, repository-qualified GitHub pull requests
  from supported squash/rebase and merge commit subjects. The metric shares the
  existing author, time-window, and commit-dedup eligibility rules; malformed
  subjects and remotes are rejected. It is additive storage for the next retro
  presentation track, not an API-backed throughput or trend metric.

## [0.12.21] - 2026-08-15

**`cli.py` is no longer one 8,840-line file that serialized every plan — it is 6,692 lines plus six focused modules, and the test suite has stopped writing into your real `~/.claude`.** The decomposition itself changes no command, flag, output, exit code, or on-disk format. Three deliberate user-visible changes ship alongside it: one hidden command (`mm conflict-log-backfill`) is gone, `mm status` gained a staleness marker, and `pytest` stopped writing your real config dirs.

### Changed

- **Six modules extracted from `cli.py`.** `consoles.py` (the two shared Rich `Console` singletons), `conflictmtime.py` (mtime primitives the pull/apply path and the conflict resolver both call), `skill_link.py` (retro-fleet installer + its 24h drift gate), `events_tail.py` (the push/init mm-events tail), `resolveflow.py` (conflict discovery, promotion, the interactive `mm resolve` walk), and `retention.py` (the `mm gc` reapers). The `@app.command()` shells all stay in `cli.py`.

  The plan called this "pure movement." It was not. `resolveflow` and `retention` both render through `console`, and `resolveflow` calls two mtime helpers whose other callers stay in `cli` — so a naive extraction raises `ImportError: cannot import name 'console' from partially initialized module` and every `mm` invocation dies. The two leaf modules are the cycle break, which is why they landed first. The plan's cut list also omitted four symbols (`_synced_scan_dirs`, `_inversion_marker_path`, `_ensure_inversion_marker`, `_canonical_for_conflict`); leaving `_ensure_inversion_marker` behind would have split a migration gate from the migration it guards, whose documented failure mode is silent data loss.

- **No compatibility shim.** Moved private names are not re-exported from `cli`. The only in-repo consumer of the CLI is `aggregator.py`, which shells out via `sys.executable -m mind_meld.cli` rather than importing. Dropping the shim is what turns a stale `monkeypatch.setattr(cli, ...)` into an `AttributeError` at collection instead of a green test that patches nothing.

### Fixed

- **`pytest` no longer mutates your real agent config directories.** 67 tests were creating symlinks and marker files under the developer's actual `~/.claude/skills`, `~/.codex/skills`, and `~/.config/opencode/skills`. `conftest.py` had nine autouse isolation fixtures and none covered the skill installer; only `test_skill_link.py` isolated `HOME`, via its own local fixture. This is a pre-existing leak the extraction made visible. Closed with a `SKILL_ROOTS` indirection, a new autouse `_isolate_skill_links` fixture, and a `PYTEST_CURRENT_TEST` guard that raises if a target ever resolves under the real `HOME` — the same shape as the existing guard on `crypto.store_passphrase_in_keyring`. Deliberately not a suite-wide `HOME` move: that degrades `importlib.metadata.version()` to `0.0.0+dev` and trips the mixed-fleet version guard.

- **`mm status` now marks a stale autorun breadcrumb.** The breadcrumb is written from inside the command, so a failure before typer's runner writes nothing at all and `mm status` reported the last `success` indefinitely. Past 48 hours it now says `stale — no autorun in Nh`.

- **`pytest` writes nothing outside `tmp_path` for the manifest sidecar or the lockfile either.** `_redirect_sidecar` / `_redirect_lock` were opt-in helpers most tests remembered to call; **47 did not**, leaving the real `~/.config/mind-meld/last-push.json` holding fixture values. Two consequences on the maintainer's own machine: `_recover_prior_manifest` reads that sidecar when a remote manifest goes corrupt (device-id scoping rejects the fixture value rather than trusting it, so no data loss — but recovery silently degrades to the peer fallback, which does not preserve this device's fresh local deletions), and the new `mm status` staleness marker reads `last-autorun.json`, which the suite rewrote to "now" — the suite forging the exact signal the feature exists to produce. Both are now autouse. Verified by diffing the real config dir's mtimes and checksums across a full run.

### Removed

- **CONFLICT-TELEMETRY.** `conflictlog.py`, the four resolve-site helpers, the 16 in-line sentinels, `merge.similarity_ratio`, and the hidden `mm conflict-log-backfill` command. It shipped 2026-07-30 to build a labeled dataset for the deferred Phase 2 auto-resolver and collected **zero** decisions in the sixteen days it ran — the log file never existed on the fleet, so the >=25-decision trigger never tracked. Deleting it first rather than moving it six weeks before its own scheduled deletion cut ~250 lines off the riskiest extraction.

### Internal

- **Verification is structural, not textual.** New `tests/test_module_boundaries.py`: standalone import of every module in a fresh subprocess in both orders (a partially-initialized cycle only manifests in one), an AST walk asserting nothing under `src/` imports `cli` at module *or* function scope (ruff F811 cannot see function-local shadowing), `Console` identity, mtime-primitive sharing, and a `python -m mind_meld.cli` smoke — the path the retro-fleet skill actually uses and which CI had never covered. New `tests/test_docs_routing.py` asserts every `<file>.py:<symbol>` citation in CLAUDE.md's invariant routing table resolves to a real definition; it immediately caught two citations (`init_cmd`, `_devices_json_cmd`) that had been stale since before this work. Byte-equality of moved function text was dropped as the gate: it proves textual provenance and none of the failure modes above.

- **CLAUDE.md's Source Layout is now a greppable table** with a purpose column, replacing a `{a,b,c}.py` brace-expansion one-liner that could not match a search for a filename. The routing table is re-anchored onto the new owning modules in the same release that moved them, rather than deferred — Group 17 is five agents opening exactly these files.

## [0.12.20] - 2026-08-15

**Mind Meld's local pull-history and source-tracker state now have one call-time path authority.** Removing unused import-time path constants keeps test isolation aligned with the live resolvers, so contributor test runs cannot accidentally bypass a redirected state directory.

### Changed

- **Maintainer hygiene for local state paths.** Pull history and seen-source writes continue to use their existing call-time resolvers; regression tests now pin that post-import directory overrides control where public writes land. Removed an unused upgrade-module import and dead history helper with no CLI, storage-format, or upgrade behavior change.

## [0.12.19] - 2026-08-15

### Changed

- The fleet retro aggregator now makes its event, token-usage, and safety dependencies explicit at module load, while retaining its tolerant optional-config probe.

## [0.12.18] - 2026-08-15

### Removed

- `mind_meld.token_usage.CacheEntry` and `walk_jsonl_token_buckets`, two stale compatibility exports. Code integrating with token usage should use `walk_jsonl_buckets(path)`, which returns token and skill buckets together.

## [0.12.17] - 2026-08-15

**Mind Meld now treats symlinks inside a sync source as local routing rather than portable content.** A linked `AGENTS.md` or generated agent skill could previously be uploaded, then rejected on every peer pull or replaced with a regular file. Push now omits child links without creating deletion tombstones; pull preserves both live and dangling links, including linked directories, while still allowing a source root itself to be linked.

### Fixed

- **Symlinked Codex and OpenCode configuration no longer causes persistent pull failures or link replacement.** The prior manifest is filtered before tombstone generation, so existing peers with ordinary files are not fleet-wide suppressed. Generated skill namespaces are also excluded by default while hand-authored skills remain eligible.
- **The Rich formatting contract test now isolates the process-level `NO_COLOR` preference.** It verifies Rich's forced-color path rather than inheriting the terminal's intentional plain-text setting.

## [0.12.16] - 2026-08-14

**One invalid UTF-8 byte in a Claude Code session file could kill the whole events tail on every push, and `mm status` reported `success` the entire time.** `_read_cwd_from_latest_jsonl` opened session jsonls in text mode and caught only `OSError`. `UnicodeDecodeError` is a `ValueError`, so it escaped through `_scan_one_project`, through `walk_session_metadata`, into `_run_events_tail`'s wrapper — which swallowed it, printed one `mm: notice:` line, and let the push continue. That notice is the interactive signal, but the tail runs from `mm autopush`, which fires unattended from a Claude Code hook, so nobody sees its stderr. Meanwhile `autopush` wrote a `success` breadcrumb unconditionally, so `mm status` kept saying everything was fine while `mm retro-fleet` quietly went stale. This release fixes the crash, the two other readers on the same path, a latent quadratic scan in the same function, a duplicate-emit bug in the git walk, and the reporting gap that hid all of it.

### Fixed

- **Binary-mode reads across every jsonl reader on the push path.** `events._read_cwd_from_latest_jsonl`, `events._last_mm_push_ts`, `token_usage.is_cache_cold`, and `pullhistory._yield_lines` now read bytes and skip a bad line instead of failing the file. Text mode decodes in ~8 KB chunks rather than per line, so a `cwd` on line 1 did not protect against a bad byte on line 2 (measured: raises at 2 lines apart, returns cleanly at 80 KB apart). The session-jsonl reader routes through `token_usage.iter_bounded_lines` — promoted to a public name for this second consumer — so the port also picks up the 16 MiB per-line cap and cannot be OOMed by a single pathological line. `is_cache_cold` additionally had a **dead** `except UnicodeDecodeError` arm (`json.loads` on a `str` cannot raise it) sitting one line below the `read_text` that actually decoded, which is why it read as already-handled and was missed by the original sweep. It runs on the events-tail path via `_decide_token_walk_policy`, so it killed the tail identically.

- **The cwd scan ran once per session file instead of once per project.** `_read_cwd_from_latest_jsonl` takes a project directory and scans every jsonl in it, but the call sat inside `_scan_one_project`'s per-file loop guarded by `if cwd is None` under a comment claiming "first one wins" — true only when a `cwd` was actually found. When no jsonl in a project carried one, the guard never flipped and the helper rescanned the whole directory once per file: 20 jsonls produced **400 file opens**, and the cost scaled to 15 ms at N=10, 1.44 s at N=100, and **13.2 s for a single 300-file project** against a 250 ms autopush budget. Hoisted above the loop. This interacts with the fix above: the old raise is what truncated the quadratic walk, so binary mode without the hoist would have run it to completion on every push — a latency regression wearing a hardening fix's clothes.

- **`walk_git_projects` emitted every completed repo twice on budget abort.** The `as_completed` pump drained and recorded each result, then the timeout handler iterated all futures and re-recorded every one that had finished. Each duplicate carried the repo's full commit list into the git-snapshot row, which is then gzipped, encrypted, uploaded, and replicated to every peer. Measured with 4 roots, 2 slow, 300 ms: 4 rows, 2 unique. `mm retro-fleet` never showed it because the aggregator dedups on `(canonical_remote, sha)`. The existing budget test makes every repo slow, so `projects` came back empty and it structurally could not catch this; the new pin uses a mixed fast/slow set.

### Added

- **`mm autopush` now writes a `degraded` breadcrumb instead of always `success`.** `_run_events_tail` returns its degradation reasons, `_push_core` carries them on `PushResult.events_degradations`, and `autopush` records them so `mm status` shows what happened. Three conditions populate it today: the tail raised, the walk exceeded its budget, or the token cache was cold (tokens and skills omitted for that push). This mirrors the `degradations` list `autopull` has had since v0.8.1 and closes the same gap the `no-sources` breadcrumb closed for a different silent path.

### Changed

- **`/retro-fleet` refreshes with `mm push`, not `mm autopush`.** The skill's own refresh step was the most truncation-prone push in the system: quiet mode gets a 250 ms walk budget instead of 500 ms and takes the cold-cache branch that drops both `tokens_by_day` and `skills_by_day` for every project — immediately before the retro read that snapshot.

- **Redundant exception tuple removed.** `except (CancelledError, FuturesTimeoutError, Exception)` was just `except Exception`; both narrow types are `Exception` subclasses. Two near-misses are now documented in place: `concurrent.futures.CancelledError` is not `asyncio.CancelledError` (which is `BaseException`-derived and would escape), and `FuturesTimeoutError` aliases the builtin `TimeoutError`, an `OSError` subclass, so a guard here must never be narrowed to `OSError`.

### Not done, deliberately

`_scan_one_project`'s original plan also proposed omitting `skills_by_day` on a deadline-truncated project so the retro would stop rendering a confident "0 skill invocations". **Cut during review.** Its premise does not hold: `get_or_compute` returns cached skills and cached tokens from the same entry past the deadline, so the claimed asymmetry between the two fields does not exist in the code. The only branch where skills come back empty is a cold cache, where tokens are empty too and `pre_token_peers` already flags the device. And omitting the key would not have stopped the data loss it flagged — latest-snapshot-wins still discards the prior complete snapshot, converting silent erasure into flagged erasure. The real fix is snapshot-level completeness plus "an incomplete snapshot never replaces a complete one", which needs a wire field `SessionsSnapshot` does not have; it is captured in `docs/ROADMAP.md` under Future and supersedes the older `skills_walk_complete` entry. A deadline parameter for the cwd helper was also cut: measured live, the whole scan is 0.82 ms across 37 project dirs, 0.33% of the autopush budget.

### Caught in review, after the first fix had already landed

`/review` (4 specialists + Claude and Codex adversarial passes) found five defects in the fix itself. Recorded because each is the same shape as the bug this release exists to fix.

- **The new `degraded` breadcrumb fired on healthy machines.** `_decide_token_walk_policy` returns `False` for four reasons, and one of them is "no `claude` source is enabled" — a config shape, not a failure. A gstack-only or codex-only machine wrote `degraded` on every autopush, blaming a token cache that was not the cause. Reproduced end-to-end. That is precisely the misleading-signal class this release removes, reintroduced by the fix for it. The append is now gated on `claude_paths`.
- **Reusing the token walker's reader dropped valid data.** `iter_bounded_lines` treats a trailing chunk with no newline as a partial write and discards it, which is correct for the resumable walker (it re-reads next push) and wrong for a one-shot reader that has no next push. The cwd reader returned `None` for a session whose only line was not newline-terminated yet, where the old text-mode reader returned the cwd. Added `yield_final_partial`, default `False` so the resumable contract is untouched.
- **A degradation with no report.** Warn-mode flock contention on the token cache drops `tokens_by_day` and `skills_by_day` for every project — user-visible outcome identical to the cold-cache case — but left `do_token_walk` `True`, so nothing reached the breadcrumb. It now does.
- **`_last_mm_push_ts` read unbounded.** It reads `mm-events` daily files, which are synced, so their bytes can arrive from a peer. It now goes through the bounded reader like the session reader does. `pullhistory._yield_lines` deliberately does not: the config dir is never synced and mm is its only writer.
- **`is_cache_cold` disagreed with the reader that actually loads the cache.** `json.loads` on bytes accepts a UTF-8 BOM; `lockedjson` decodes strict UTF-8 and rejects it. A BOM-prefixed cache reported warm, so the inline warm was skipped, and then the real read reset it as corrupt: no token data and no warm cache. Now decodes strictly before parsing.

Two test defects were also caught and fixed. The bad-byte pin asserted only "did not crash" — mutation-tested by swapping the per-line `continue` for a per-file `break`, all 84 events tests still passed. And an oversize-line pin asserted the returned value, which the unbounded reader also produces; it now asserts the bounded reader's notice. Both are the "passes but proves nothing" shape this release's own review criticized.

### Tests

Sixteen regression pins. Every pin that claims to catch a regression was verified to fail against the pre-fix tree and pass after; the two coverage pins (walk-budget breadcrumb, healthy-`success` control) pin sites that already existed and are marked as such. Bad-byte fixtures put the invalid byte **before** the valid line, because the reverse ordering passes trivially once the fix is in and proves only "didn't crash". The cwd fix is pinned by call count rather than wall clock. The oversize-line pin asserts the bounded-reader notice rather than just "the cwd was found" — the weaker assertion was written first and confirmed to pass against pre-fix code, which is exactly the shape of test this release is trying to avoid.

## [0.12.15] - 2026-08-13

**`mm push` kept printing `mm: notice: events tail budget exceeded`, and the cause was a full re-parse of files that only ever grow.** The events tail walks your Claude Code session jsonls to build the snapshot behind `mm retro-fleet`, under a 500 ms wall-clock cap (250 ms for `mm autopush`). Any session whose size or mtime had drifted since the last push was re-read end to end — so a 10 MB session that gained 300 KB of new turns cost a full 10 MB re-parse. The walk was O(total bytes on disk) when it should have been O(bytes appended). On a 107-project Mac with 410 MB of sessions, a day of Conductor work across several large workspaces put it well past the cap.

**Session jsonls are append-only, so the walk now resumes where it stopped.** Each cache entry carries a byte offset, a fingerprint of the file's head, and the trailing message ids. On the next push mm seeks to the offset and parses only the new bytes. Measured on the eight largest local sessions: 56 MB re-walked for 1.7 MB of new data went from 218 ms to 9 ms — 23x. Cold walks got ~20% faster as a side effect of reading in binary mode.

**Raising the budget was the fix not taken.** The notice is a symptom; the cost curve was the defect. `WALK_TIME_BUDGET_INTERACTIVE_MS` and `WALK_TIME_BUDGET_AUTOPUSH_MS` are unchanged. When the cap does trip, behaviour is as before: the partial snapshot is written, the push proceeds, and projects that got cut keep their previous snapshot rather than being erased.

**Correctness was the acceptance bar, not speed.** A resumed walk must produce exactly what a single full walk produces. Three guards carry that: a partially-written trailing line is never parsed and never counted toward the offset, so a file caught mid-write is re-read once it settles; the trailing message ids seed the next segment's dedup set, because Claude Code writes one line per model iteration under a shared `message.id` and each restates the same cumulative usage; and a head fingerprint distinguishes a genuine append from a rewrite that happens to land at or above the cached size. Anything unexpected — a shrink, a missing field, a malformed seed, an unreadable head — falls back to a full walk. Verified across 358 live session jsonls cut at four points each including mid-line (1,432 cases) and against the pre-change walker over the same corpus: 7,848,746,853 tokens, zero drift.

**The dedup window is measured, not guessed.** Cross-model review flagged the bounded 8-id seed as a possible source of double-counting. Measuring the real corpus settled it: 26,989 assistant lines repeat an already-seen `message.id`, and zero of those repeats are separated by even one other distinct id — every repeated run is strictly contiguous, so a window of 1 would do and 8 is headroom. Duplicate `tool_use.id` values: zero, so the skill side deliberately carries no seed. Both numbers are recorded in the invariants doc with the residual risk stated plainly rather than engineered away.

**Also fixed:** a single invalid UTF-8 byte anywhere in any session jsonl used to raise `UnicodeDecodeError` out through the entire events tail, degrading the whole push to `mm: notice: events tail failed`. Bad lines are now skipped individually like malformed JSON already was. Cache entries written by earlier versions are not force-re-walked — they still hit on matching size and mtime and upgrade shape on their next real miss, so upgrading costs one ordinary walk per actively-appended file rather than a fleet-wide re-parse. New load-bearing section in `docs/invariants/events-retro.md`.

**The tests were then mutation-tested, and six survivors got fixed.** A coverage audit mutated 18 branches of the resume logic and re-ran the suite against each: 12 died, 6 lived. Every survivor was a branch the invariants doc claimed was pinned by name. The whole `size_now < prev_size or offset > size_now or offset > prev_size` guard was deletable with a green suite, because each of the three tests aiming at it actually died on a different check. `head_len`'s equality check, `_drain_to_newline`'s seek-rewind (every oversize test put the junk line at EOF, so the rewind never executed), the `MAX_BY_DAY_DAYS` trim under an incremental merge, and `_carry_tail_ids`' seed-dedup were all likewise unpinned — that last one mattering because emitting a duplicate id poisons the entry and disables resume permanently and silently, the same shape as the short-file bug. All six mutants are now caught, and `_carry_tail_ids` enforces uniqueness itself rather than trusting its caller.

A second cross-model pass over the fixes caught four more, also fixed here: the head probe was read AFTER the stability stat rather than between the two stats, leaving a window where a replaced file could be persisted under the replacement's fingerprint; `head_len` was validated as merely in-range, so a corrupted entry could shrink the probe to one byte and still "pass" while proving nothing; `tail_msg_ids` was silently filtered rather than validated, contradicting the stated "malformed means full walk" contract; and the merge helpers assumed nested dict shapes, so one malformed cache entry would raise through the events tail on every push while the poisoned entry survived.

Reviewed via `/review` (adversarial cross-model pass), which caught five issues fixed here: a fixed-size head probe meant every session file under 4 KiB re-fingerprinted differently on each append and silently degraded to a full walk forever (correct numbers, none of the speedup, no error anywhere — caught by a positive-control test added to the resume-rejection matrix); `message.id` is peer-controlled jsonl input with no length bound, so eight crafted ids could push the cache past `lockedjson`'s 64 MiB read ceiling and wedge the fleet into a permanent cold walk; `_resume_plan` accepted an offset past the size it was recorded against, and treated a malformed seed as an empty one rather than as the corruption it is; a read that failed outright still persisted an entry, because the stat calls bracketing it agree — a permanent cache hit that stops counting a session; and the new merge would have been the fifth hand-rolled copy of a loop `CLAUDE.md` says is consolidated, now `merge_token_days` / `merge_skill_days` with a test that fails the build if `events.py` regrows a local copy.

## [0.12.14] - 2026-08-13

### Added

- Sync Codex and OpenCode customization alongside Claude Code: durable rules, skills, plugins, commands, agents, modes, and tools follow your fleet without copying credentials, session databases, logs, tool output, or whole-file settings that may contain secrets.
- Install and self-heal the bundled `/retro-fleet` skill for Codex and OpenCode as well as Claude Code, including OpenCode-only setups with Claude compatibility disabled.
- Document the shared `mm autopull` / `mm autopush` lifecycle so every agent contributes to the gstack and event context behind fleet retrospectives.

### Changed

- Existing installations can opt into the new sources with `mm enable-source codex` and `mm enable-source opencode`; fresh installs offer both during setup.

## [0.12.13] - 2026-08-11

**`retro-fleet` was understating cost by ~3,000x — a 60-day window that really cost ~$11,015 at list price rendered as `~$3.37`.** Two independent defects that masked each other. First, the entire Claude 5 family (`claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`) and `claude-opus-4-8` were missing from the pricing table, and unknown models were skipped in silence — so only leftover Haiku subagent traffic got priced, and that `$3` was the *whole* basis. Second, `claude-opus-4-7` / `-4-6` / `-4-5` carried `$15/$75`, which are Opus **4.1** rates; those models are `$5/$25`. The two errors push opposite ways, so whether a card over- or under-reported depended entirely on its model mix — worse than being consistently wrong, because one window looks plausible and the next is off by orders of magnitude.

**Root-cause fix, not a table refresh: family-tier fallback.** An exact model entry still wins, but anything unlisted now resolves against its *family* (`opus`/`sonnet`/`haiku`/`fable`/`mythos`), so a model released after this table was written prices in the right ballpark instead of silently costing $0. Only genuinely unparseable ids stay unpriced. The accepted inaccuracy is stated rather than engineered away: family tiers carry current-generation rates, so a retired model would price wrong (Opus 3 was `$15/$75`) — fine, because retired models don't appear in live session data, and being ~3x wrong on a model nobody runs beats being infinitely wrong on the model everybody runs.

**One predicate, so the card can't contradict itself.** The cost path and the "N unpriced model(s)" Notes path each ran their own `model in PRICING` test. Benign while both were plain dict lookups — but family-tier fallback landing in one of them would have made the card price `claude-opus-6` on one line and call it unpriced two lines down. Both now share `token_usage.resolve_prices`. Same failure shape v0.11.23 already fixed once; pinned this time by a regression test.

**Honest degradation, because staleness is permanent.** mm has no network by design, so the pricing table can never self-update — stale is the steady state, not the exception. Three changes follow from that: any unresolvable model flips the cost line's prefix from `~` to `>=` (a floor, not an estimate); the caveat line now carries `PRICING_LAST_UPDATED` so a human can judge freshness; and the dead "refresh if >6 months old" threshold is gone — nothing read it, and it wouldn't have helped (the table was three months into that window while wrong about four models). The pre-existing stderr breadcrumb stays but is explicitly not the load-bearing signal: it fired for four unpriced models across the whole v0.12.x line and nobody saw it.

**Peer-controlled ids now drive a pricing decision, so they're validated like one.** `model_family` matches positionally against a literal allowlist — never a substring, which would bill a planted `claude-haiku-opus-4-5` at Opus rates. Mirrors the validate-at-construction convention in `storage/keys.py`.

**Also fixed:** `_short_model_name` required 4 dash-segments, so every 3-segment Claude 5 id rendered raw (`claude-opus-5`) beside a prettified `Opus 4.8` on the same line. Cache writes now price at 2x input (the 1h TTL Claude Code actually uses) rather than 1.25x (5m) — measured at 83% 1h across a local sample; the exact per-TTL split needs a wire-format change and is tracked in `TODOS.md`. Cost figures drop false-precision cents above $100.

**Peer token counts are now clamped.** `_safe_int` bounds every synced token field to `[0, 2**53]`. Family-tier fallback grew the reachable model set from five hardcoded ids to any `claude-<family>-*`, which widened two pre-existing holes: a 400-digit integer survives `json.loads` and then raises `OverflowError` inside the cost multiply, killing `mm retro-fleet` with a traceback; and a negative count silently subtracts from the fleet total, letting one bad peer shrink or entirely suppress the cost line. `_safe_short` also gained a 128-char cap, mirroring `_safe_prose` — peer-controlled ids reach rendered markdown and the ASCII card, which goes into an LLM context.

Reviewed via `/plan-eng-review` (4 architecture decisions taken interactively, remainder auto-decided) then `/review` (4 specialists + adversarial pass), which caught six issues in the first draft and fixed them here: the renderer's relaxed segment check mangled legacy ids (`claude-3-opus` → `3 opus`, `claude-3-5-sonnet` → `3 5.sonnet`, both reachable since `_normalize_model_id` strips the date suffix) and is now gated on the family allowlist; the `PRICING` table listed six entries all byte-identical to their family tier, recreating the exact multi-site drift this release removes, and now ships empty as a pure override table with a test that fails the build on a redundant entry; the two peer-token clamps above; plus stale comments and two conflicting figures cited for the same incident. New load-bearing section in `docs/invariants/events-retro.md`. Pricing is applied locally at render time, so this fixes the card immediately with no peer migration — other machines can stay on older mm.

## [0.12.12] - 2026-07-30

**Temporary conflict-decision telemetry to inform a future auto-resolver.** `mm resolve` now records each conflict resolution — the features a future auto-resolver would see (LCS similarity, merge-cleanliness, divergence line counts, recency) paired with the choice you actually made — to a local, best-effort JSONL at `~/.config/mind-meld/conflict-decisions.jsonl`. This is the labeled dataset the deferred Phase 2 similarity classifier was blocked on: validating its thresholds against real decisions is the prerequisite before it could ever gate a silent merge. Zero change to the pull/autopull hot path, and logging never raises into a resolution.

**Backfill from existing sidecars.** A hidden, opt-in `mm conflict-log-backfill` seeds the dataset from the `.sync-conflict-*` files already on disk, quarantined as context (not training labels) since a surviving sidecar is an implicit skip. It holds the mm lock only for discovery, then releases it before reads so a slow iCloud placeholder materialization can't wedge background autopull/autopush.

**Disposable by design.** Every call site carries a grep-able `# CONFLICT-TELEMETRY (temporary)` sentinel; the whole collector is scheduled for removal once the Phase 2 thresholds validate (tracked in `TODOS.md`). The log is local-only and never synced.

## [0.12.11] - 2026-06-23

**`pipx upgrade mind-meld` now works — the auto-upgrade nudge and install instructions track a moving `latest` branch instead of pinning to a frozen `@vX.Y.Z` tag.** Previously, every upgrade left your install stuck: the nudge printed `pipx install --force git+...@vX.Y.Z`, which records that exact tag as pipx's `package_or_url`. Because a git `@<ref>` is a URL fragment (not a PEP508 version specifier), pipx's `parse_specifier_for_upgrade` keeps it verbatim, so `pipx upgrade` re-resolves the same frozen commit forever and reports your current version as "latest." Confirmed against pipx 1.14.1's own source. v0.12.10 (#99) only *documented* this footgun; this release removes it.

**The fix.** A git tag never moves; a branch does. The release workflow now force-advances a `latest` branch to each tagged release commit (`.github/workflows/release.yml` "Advance latest branch" step), and the nudge/install commands target `@latest`. A branch ref re-resolves to its advancing HEAD on `pipx upgrade`, so upgrades land. `latest` only ever points at *released* commits — never untagged WIP that may sit on `main` — so it respects the "tag = release" discipline while still being a moving target.

**One-time un-sticking.** The nudge command stays `pipx install --force git+...@latest`. The `--force` reinstall both lands the newest release AND rewrites a previously tag-pinned install's recorded URL onto `@latest`, after which plain `pipx upgrade mind-meld` works for good. If you're currently frozen on an old `@vX.Y.Z` pin, run that command once.

**Scope.** `INSTALL_CMD` in `upgrade.py` is now a single version-independent constant (was a per-version `@{tag}` template); the target version lives only in the nudge message text. `mm status` surfacing flows through unchanged. README Install / Upgrading / second-Mac sections and the auto-upgrade-nudge bullet updated to `@latest`. New load-bearing section in `docs/invariants/auto-upgrade.md` pinning the branch-not-tag invariant. `tests/test_upgrade.py` asserts the command carries `@latest` and never a `@vX.Y.Z` pin.

## [0.12.10] - 2026-06-23

**Conflict-resolution prompts now show created/modified timestamps for each side plus a recency verdict, and `mm resolve` gains an `(n)ewer` shortcut that keeps whichever file was modified more recently.** When two machines diverge on a file, you can now see when each version was created and last modified without leaving the terminal, and a `-> REMOTE is newer by 2d` verdict line states which side is more recent and by how much. In `mm resolve`, pressing `(n)` keeps the more recently modified side.

**Display (both prompt sites).** Under each side's banner, a dim line: the local side shows genuine `created` + `modified`; the remote side shows `modified` plus — at the `mm resolve` site, where the sidecar is on disk — a `pulled` line (the local iCloud-drop time, NOT the peer's real creation, so it is never mislabeled `created`). At the inline `mm pull --conflict-mode prompt` site the remote file is not on disk yet, so only its manifest `modified` time is shown. All timestamps are locally-formatted ASCII dates; no peer-controlled bytes flow into the new lines (filenames/device names stay in the already-sanitized banners). The verdict's `-> SIDE is newer by N` delta is computed BETWEEN THE TWO FILES (not vs wall-clock) so it is deterministic and matches the `mm conflicts` `Nd`/`Nh` Age style, extended with a minutes bucket.

**`(n)ewer` is `mm resolve`-only and never guesses.** It remaps to the existing `(l)`/`(r)` dispatch, so the per-inversion-mode keep-local / keep-remote semantics and the v0.12.6 cross-fleet mtime bump are reused verbatim — no new apply branch. It is offered only when both mtimes are readable; on an exact tie it prints `equal mtime — choose manually` and re-prompts the same conflict rather than guessing a side; a typed `n` while the option is suppressed (unreadable mtime) also re-prompts instead of skipping. The input read is wrapped in a parse-only loop so the existing dispatch (and its `continue` that advances the outer conflict walk) is untouched.

**Why `(n)ewer` is NOT offered inline (Codex eng-review catch).** `_apply_incoming_file` already skips before the inline prompt when the local file is newer (`cli.py` mtime gate), so at the inline prompt the remote side is always newer-or-equal — an inline `(n)ewer` could only ever pick remote, making it a redundant alias of `(r)`. The inline site therefore shows the timestamps + verdict (display only); keeping the more recent file there is just `(r)`.

**Reviewed via `/plan-ceo-review` + `/plan-eng-review`** (both with Codex outside-voice passes). The non-interactive `--keep-newer` batch flag and an `mm conflicts` modified column were considered and skipped; a known future-clamp recency edge case is captured as a `TODOS.md` watch-item; birth/creation time on the manifest wire is out of scope (no schema change). New pure primitives `format_ts` / `format_age_delta` / `newer_side` / `render_time_line` / `render_verdict` in `conflictdiff.py`; shared `_stat_mtime_btime` helper in `cli.py`. **Test coverage:** 18 new pure tests in `tests/test_conflictdiff.py` and 9 integration tests in `tests/test_conflict_copy.py` (all four resolve inversion×newer mapping cases exercised through the dispatch, tie re-prompt, suppressed-`n` re-prompt, `canonical is None` branch stays skip, inline manifest-mtime display + no inline `(n)ewer`). New load-bearing section in `docs/invariants/conflicts.md`.

## [0.12.9] - 2026-06-17

**The `events tail budget exceeded` notice is now scoped to the git+session walk it claims to measure — a cold identity-cache refresh no longer masquerades as a slow walk.** Implements the misleading-notice task from ROADMAP Group 16 / Track 16A.

**The bug.** `_run_events_tail`'s wall-clock budget (`WALK_TIME_BUDGET_INTERACTIVE_MS` 500 / `WALK_TIME_BUDGET_AUTOPUSH_MS` 250) is plumbed as the deadline for `walk_git_projects` and `walk_session_metadata` — but the tail-position `time.monotonic() > deadline` check sat AFTER `identity.gather_local_identities(allow_refresh=True)` and `write_push_event`. The identity gather's cold path runs a synchronous refresh bounded by its OWN timeouts (`_GIT_GLOBAL_TIMEOUT_S` 2s + `_PER_REPO_BUDGET_S` 5s + `_GH_TIMEOUT_S` 3s ≈ up to 10s, 7d TTL). So on any push that triggered a cold identity refresh, the check fired `events tail budget exceeded` even though the actual git+session walk finished in ~200ms — two notices on the same push (`refreshing identity cache (one-off)` then `budget exceeded`), one true and one false.

**The fix.** Snapshot `walk_done = time.monotonic()` the moment the session walk completes — before the identity gather and the event write — and compare `walk_done > deadline`. The budget now bounds, and the notice now reports on, the walk only. The identity gather announces itself separately and self-limits via its own timeouts, so the two concerns are orthogonal. `_run_events_backfill` carries the identical fix: its `walk_done` snapshot precedes the deliberate `refresh_identity_cache(force=True)` init warm, which ALWAYS runs and would otherwise trip `events backfill budget exceeded` on essentially every `mm init`. Budgets are unchanged — the walk genuinely fits in 500ms; the bug was the mismeasurement, not the threshold.

**Scope.** Only the two deadline checks + their `walk_done` snapshots in `cli.py`. The other three Track 16A tasks (autopush `allow_refresh=False`, a `_FULL_GATHER_BUDGET_S` overall deadline in `identity._do_full_gather`, per-jsonl deadline checks in `token_usage`) remain open — they reduce how often / how long the cold gather runs; this change fixes the false signal regardless.

**Test coverage.** New `tests/test_events_budget_scope.py` (4 tests): for both the tail and the backfill, a slow identity gather/refresh that outlasts the budget does NOT emit the notice (the regression pin), and a genuinely slow session walk still DOES (proving the check was narrowed, not removed). **Documentation.** `docs/invariants/events-retro.md` invariant 4 rewritten to mark the check walk-scoped and load-bearing; `_run_events_tail` docstring updated.

## [0.12.8] - 2026-05-14

**Conflict prompts no longer default the Enter key to `(m)erge`, and `mm resolve` gains a `(p)romote` option that keeps BOTH divergent files.** Two changes from the /office-hours → /plan-eng-review design for conflict handling.

**Stop defaulting to `(m)erge`.** Pre-fix, both conflict-prompt sites (`_prompt_conflict_choice` inline at pull time, `_resolve_interactive_loop` in `mm resolve`) flipped the default key to `m` whenever the LCS merge was clean (`merge_available and merge_conflicts == 0`). But a "clean" LCS merge of two genuinely-DIFFERENT documents — two files that merely collided on a filename — has zero conflict markers, so it looks clean while being a Frankenstein concatenation. With the default key on `m`, one Enter keystroke silently accepted that. The default is now always `(s)kip`; `(m)erge` stays a fully available choice the user types explicitly. ~4 lines, removes the entire silent-corruption path without removing the feature.

**`(p)romote` — keep both as separate first-class files.** `mm resolve` already had a `(p)romote` option, but only in the no-base branch (canonical deleted). It now also appears on the canonical-exists path: instead of choosing local OR remote, `(p)romote` renames the `.sync-conflict-*` sidecar to its own first-class filename so both versions survive as separate synced files — the honest resolution for the "two different documents that collided on a name" case. Per-mode naming: a post-inversion sidecar holds the peer's bytes → `<stem>.from-<peer-short-id>-<ts>.<ext>`; a pre-inversion (`v0-`) sidecar holds the user's own local bytes → `<stem>.local-<ts>.<ext>` (naming it `from-<peer>` would lie about provenance). Same-second collisions get a `-<4hex>` suffix; the rename uses `os.link` + `os.unlink` for an atomic no-clobber guarantee (a plain `Path.rename` silently replaces an existing target on POSIX, and the promoted file is a first-class user filename — clobber would be data loss). If the promoted file lands under an `include_files` source — its new name won't match the exact include list — `mm resolve` prints a one-line warning that it won't sync until added to config. `(p)romote` is `mm resolve`-only; it is NOT offered at the inline pull-time prompt, which has no sidecar on disk yet (adding it there would touch the autopull path).

**Scope deliberately reduced.** The /office-hours design originally proposed an LCS-similarity classifier (`classify_divergence`, a `DivergenceClass` enum, a `Class` column on `mm conflicts`). /plan-eng-review's Codex outside voice caught that the classifier is advisory-only in this phase — it gates nothing, the footgun has a ~4-line fix, and the `Class` column is triage tooling for a workload that no longer accumulates (the upstream gstack `pair-review/` filename collision is already fixed). The classifier + similarity-gated silent merge are deferred to a Phase 2 captured in `TODOS.md`.

**Documentation.** New load-bearing section `## (p)romote canonical-exists + never-default-to-(m)erge` in `docs/invariants/conflicts.md`; `CLAUDE.md` invariant pointer table adds `_promote_target_path` / `_promote_conflict_file` / `_promote_target_will_sync` to the conflicts row.

**`(p)romote` bumps canonical mtime post-resolve (post-inversion only).** Caught by Codex /review on the pre-merge pass: the new canonical-exists `(p)romote` branch was missing the `_bump_canonical_mtime_post_resolve` call that `(l)ocal` has at line 7276. Without the bump, the local half of "keep both" silently fails to propagate — the origin peer's next pull mtime-gates this device's local canonical out, and the fleet ends up with one document under two names instead of two distinct documents. Same load-bearing fleet-propagation contract as `(l)ocal`; the fix captures `peer_mtime` from the sidecar before the rename and applies the bump after promote succeeds. Pre-inversion path explicitly opts OUT of the bump (canonical holds peer's bytes intentionally — bumping would lie about when peer's bytes arrived). Two new regression tests in `TestResolvePromote`: `test_promote_post_inversion_bumps_canonical_mtime` (asserts canonical mtime > peer_mtime after promote), `test_promote_pre_inversion_no_mtime_bump` (asserts canonical mtime unchanged for `v0-` sidecar).

**Test coverage.** `tests/test_conflict_copy.py`: `TestNeverDefaultToMerge` (default key is `s` at both sites on a clean merge; `(m)erge` still works when typed), `TestPromoteHelpers` (per-mode naming, none-peer fallback, collision → `-<4hex>`, `os.link` no-clobber, happy path), `TestResolvePromote` (post-inversion + pre-inversion promote, link `OSError` → failed count, `include_files` warning vs `include_dirs` no-warning, no-base promote unchanged, post-inversion mtime bump, pre-inversion no mtime bump). `tests/test_conflictdiff.py`: `render_prompt` `promote_available` line. 1577 tests pass.

## [0.12.7] - 2026-05-14

**`mm pull --conflict-mode prompt` choosing `(l)ocal` now propagates the decision across the fleet — symmetric with `mm resolve`.** Pre-fix (the v0.12.6 state), the inline conflict prompt's `keep-canonical` branch returned `"skipped"` and deliberately did NOT bump canonical's mtime, leaving `mm pull --conflict-mode prompt` users stuck in the same `resolve → pull → resolve → pull` loop that v0.12.6 fixed for `mm resolve`. The v0.12.6 release had reverted an inline bump because bumping canonical's mtime *mid-pull-walk* makes a LATER peer's `_apply_incoming_file` mis-classify on the `local_mtime > remote_mtime` gate and silently skip — hiding a peer whose mtime falls between original-local and the bump value. Track 12A closes the loop the right way: **defer the bump to end-of-pull-batch.**

**Mechanism.** `_pull_core` allocates a shared `pending_inline_bumps: dict[Path, float]` — but only in interactive (`prompt`) mode; non-interactive pulls (default `keep-both`, autopull) pass `None` and the whole machinery is a guarded no-op down the call chain (`_pull_one_source` → `_download_and_apply` → `_apply_incoming_file`). The dict is keyed on the **resolved** path (`_download_and_apply` already computes `resolved_local` for its escape check) so a symlinked source root or path-spelling alias can't leave a stale entry a later write fails to invalidate. `keep-canonical` records `max(existing, peer_mtime)` so the bump beats every peer that conflicted on the same path in one walk, regardless of walk order. After the device loop, `_drain_inline_bumps` applies one `_bump_canonical_mtime_post_resolve` per entry — the same helper, same `now + 60s` future-clamp as the resolve-side bump.

**The eligibility invariant — outcome-gated invalidation.** A path is drain-eligible iff every successful canonical mutation on it this batch was a keep-canonical "skipped". RECORD lives in `_apply_incoming_file`'s keep-canonical branch. INVALIDATION lives ONE level up, at the `_download_and_apply` seam, keyed on the returned outcome via `_CANONICAL_WRITE_OUTCOMES = frozenset({"written", "merged", "merged-via-lcs", "conflicted"})`. After every `_apply_incoming_file` call, if the outcome is in that set, the corresponding entry is popped. "failed" / "skipped" / "unchanged" leave canonical untouched and intentionally do NOT invalidate.

**Why this design (caught by adversarial review post-implementation).** The first implementation scattered `_invalidate_inline_bump` calls across the three prompt branches (keep-remote / merge / keep-both). Codex adversarial + Claude adversarial both surfaced two real holes: (1) `_apply_write` (the `not local_path.exists()` branch at the top of `_apply_incoming_file`) writes a LATER peer's REMOTE bytes when the canonical file vanished mid-walk — user `rm`'d it while the blocking prompt waited, or autopull races. The per-branch approach missed `_apply_write` entirely, so the drain would silently bump peer B's remote bytes as if locally-authored. (2) keep-both's `_invalidate_inline_bump` fired *before* `_apply_conflict`, so a failed sidecar write would still pop a valid keep-canonical decision — violating the success-only rule the design enforces everywhere else. Both holes close uniformly by routing through the outcome enum: ONE site at the `_download_and_apply` seam, success-only by construction (write outcomes are only returned on successful canonical mutation), and it covers `_apply_merge` for free too (it returns `"merged"`).

**Abort skips the drain (intentional).** `_drain_inline_bumps` runs INSIDE `_pull_core`'s `try` block, so a `typer.Abort()` from the inline `(a)bort` choice propagates straight past it to `finally` — abort means the user does not trust this pull, so half-made `keep-canonical` decisions are not broadcast to the fleet.

**Honest limit (Codex outside-voice review #7).** A peer whose manifest mtime is missing or malformed never reaches the mtime gate at all (the gate requires `remote_mtime is not None`), so the bump cannot close the `resolve → pull` loop for THAT peer. The `keep-canonical` branch records `0.0` in that case — the correct, non-crashing mechanical choice, mirroring the resolve-side stat-failure degradation — but it only helps every *mtime-bearing* peer. This is a known limit, not a silent failure.

**Pre-existing on-disk sidecar interaction (acknowledged, deferred).** The end-of-batch bump can mtime-skip a peer whose OLD, still-unresolved `.sync-conflict-*` sidecar predates this pull. `/plan-eng-review` chose NOT to suppress this: the sidecar stays on disk as the durable signal (`mm conflicts` / `mm resolve` still surface it), v0.12.6's resolve-side bump has the identical property and is considered correct, and suppressing would weaken the propagation fix. Captured as a `[plan-eng-review]` watch-item in the new `TODOS.md` — revisit only if dogfood shows it bites.

**Documentation.** New load-bearing section `## Deferred inline keep-canonical mtime bump` in `docs/invariants/conflicts.md` covers the eligibility invariant, the success-only invalidation placement, the abort-skips-drain rule, and both acknowledged limits. `CLAUDE.md`'s invariant pointer table adds a row routing `_record_inline_bump` / `_invalidate_inline_bump` / `_drain_inline_bumps` + the `pending_inline_bumps` plumbing to `conflicts.md`.

**Test coverage.** 13 tests in `tests/test_conflict_copy.py::TestResolveLocalMtimeBump`: back-compat (`None` dict → no record, no mutation), records-but-no-mid-walk-mutation, `None`-mtime → `0.0`, cross-peer `max()` (newer-then-older walk order), `_drain_inline_bumps` bumps past peer + future-clamp, gate-invalidation pins for all three prompt outcomes (keep-remote → "written", merge → "merged-via-lcs", keep-both → "conflicted") asserting each outcome is in `_CANONICAL_WRITE_OUTCOMES`, success-only pins for failed keep-remote write AND failed merge write (both return "failed" — not in the gate set, dict entry retained), the `_apply_write` file-vanished-mid-walk regression pin (the Codex HIGH catch — peer A records, canonical deleted, peer B same path → "written" via `_apply_write` → gate invalidates), a `_download_and_apply` integration pin proving the real gate is wired (drives the function with a `MagicMock` backend + monkeypatched `_apply_incoming_file` for "written" / "failed" / "skipped"), `_pull_core` abort skips the drain, and `_pull_core` drains after the device loop. The pre-v12A `test_inline_pull_keep_local_does_not_mutate_canonical_mtime` was retargeted (not deleted) into the back-compat pin.

## [0.12.6] - 2026-05-12

**`mm resolve` picking `(l)ocal` now bumps canonical's mtime so the decision propagates across the fleet.** Pre-fix, "keep local" deleted the sidecar (post-inversion) or renamed the `v0-` sidecar onto canonical (pre-inversion) but never touched canonical's mtime — which `_apply_incoming_file`'s mtime gate at `cli.py:1633` had just classified as `<=` peer's, otherwise the conflict path wouldn't have fired. The next pull from the same peer re-ran the conflict path because the dedup signal (`_existing_post_inversion_sidecars_from_peer`) was gone (we just unlinked it) and the mtime gate still failed. Users hit a permanent `resolve → pull → resolve → pull` loop where the same file kept conflicting against the same peer with no on-disk signal that they had repeatedly chosen local. v0.11.5 fixed the same-session same-peer sidecar-stamping spam, but its dedup only fires when an existing sidecar is on disk — `mm resolve`'s unlink deleted that signal. **Cross-fleet impact:** with no mtime bump on resolve, the user's "I picked local" decision was machine-local; the next push uploaded a manifest entry with local's *unchanged* (and `<=` peer's) mtime, so peers pulling never saw local as authoritative and the original publisher kept winning the mtime race indefinitely.

**New helper `_bump_canonical_mtime_post_resolve(canonical, peer_mtime)`** stamps canonical with `max(now, peer_mtime + 1.0)` capped at `now + _MTIME_RESTORE_MAX_SKEW_SECONDS` (60s — symmetric with `_restore_mtime_best_effort`'s future-clamp so downstream peers don't re-clamp our pushed mtime and create a fresh divergence). `peer_mtime` is captured BEFORE the unlink/rename in `_resolve_interactive_loop`: post-inversion from the sidecar (whose mtime was restored from the peer's manifest by `_restore_mtime_best_effort` during the producing pull); pre-inversion from the original canonical (which held peer/remote bytes prior to v0- sidecar promotion — without this read, the renamed v0- file would inherit a pre-v0.9.2-era mtime guaranteed older than any active peer's, locking the user into a fresh conflict immediately). Stat failure degrades to `peer_mtime = 0.0` so the bump still happens — worst case the canonical's mtime is `now`, which is still greater than any peer's past mtime in practice.

**Cross-fleet propagation story (the load-bearing why).** After bump, this device's next push publishes a manifest entry with the bumped mtime + local-canonical's bytes. Other peers pull: they see `peer_mtime (bumped) > their_local_mtime` + different bytes, hit the conflict path against THEIR local, write a sidecar of OUR bytes, and their user picks `(r)emote` to converge — OR `(l)ocal` again to disagree, which loops the bumped-mtime back to us as a conscious counter-resolution instead of the silent perpetual cycle. Either way the fleet reaches consensus instead of cycling indefinitely on the original publisher.

**Future-clamp edge case (acknowledged).** When peer's mtime is itself at the 60s clamp ceiling, `max(now, peer_mtime + 1.0) = peer_mtime + 1.0` gets capped back to `now + 60s = peer_mtime` — local-mtime is not strictly greater. Cycle persists one more pull and self-heals on peer's next legitimate push (peer's mtime drops back to normal). A non-symmetric local violation of the clamp invariant (writing > now+60s on our own canonical) would propagate worse: downstream peers re-clamp on receive, creating a fresh local/remote divergence and a *new* conflict. Not worth the workaround for the 1-extra-pull edge case.

**Codex P1 caught during /review's adversarial pass: mtime bump alone doesn't propagate.** First pass of this fix bumped canonical's mtime on disk but didn't reach peers because `_push_core`'s v0.12.2 substantive-change gate uses `diff_files` (sha256-keyed). A push after `mm resolve` (l)ocal would print "Nothing to push" and never upload the manifest — kb-ms's local cycle break worked but kb-mbp's view stayed wedged. Fixed by adding `_has_mtime_only_changes_vs_remote(local_manifest, remote_sources, source_filter=None)`: walks the intersection of local-and-remote files where sha256 matches and reports True iff any has STRICTLY-NEWER local mtime. Wired as `has_mtime_only` into the gate alongside `has_substantive` and `recovering_from_corrupt`. Per-source upload loop still uses `skip_unchanged=True` so no blob bytes upload (sha matches → `put_exclusive` dedup). The manifest itself uploads with refreshed mtimes. New "Refreshing manifest (metadata-only changes)..." console message distinguishes this case from the recovery rewrite and the normal-upload path. `mm status` and `mm push --dry-run` gained symmetric metadata-only branches via the same helper (additional Codex P2/P3 catches) so users running preflight checks see "Metadata-only changes pending (run `mm push` to publish updated mtimes)" instead of the misleading "All sources in sync".

**Codex P2: forward-only invariant (downgrade hazard).** Second adversarial pass caught that the helper's initial `!=` mtime comparison would also republish DOWNGRADED mtimes (e.g. after `git checkout` / file-restore / `touch -t` to a past date). Downgrading the manifest's recorded mtime is a silent-skip hazard: a peer with different bytes and mtime BETWEEN the old-remote and the downgraded value would now hit `local_mtime > remote_mtime` on pull and skip — losing the conflict surface that resolves divergent state. Fixed by gating on `local_mtime > remote_mtime` (strict greater-than, lexicographic string compare on ISO-8601 — sorts correctly for any well-formed manifest). Forward-only matches the helper's actual user-driven trigger (`_bump_canonical_mtime_post_resolve` always bumps to `max(now, peer_mtime + 1.0)`) and rules out the downgrade hazard for all benign filesystem-metadata operations.

**Codex P3: status source filter scoping.** `_has_mtime_only_changes_vs_remote` originally walked every source unconditionally, but `mm status --source <name>` is supposed to scope to that one source. Drift in source Y would surface a "Metadata-only changes pending" hint under `mm status --source X` even though X itself was in sync. Added `source_filter: str | None = None` parameter; status passes the same `source` it passes to `iter_source_diffs` so the metadata-only check matches the filter scope.

**Codex P2 (fifth pass): parse-before-compare on mtimes.** Raw lexicographic `>` on the manifest's stored mtime string would crash on a peer-controlled non-string value (`mtime: 1234` int — `load_manifest` doesn't type-check this field) AND misorder valid-but-different ISO-8601 spellings (`Z` vs `+00:00` represent the same instant but sort differently as strings). Helper now parses both sides through `mtime_from_manifest(...).timestamp()` (same defensive path `_restore_mtime_best_effort` uses) and on any `TypeError | ValueError | OverflowError | OSError` returns False for that file — conservative: better to under-publish a metadata refresh than crash `mm push` / `mm status` on a peer-crafted value.

**Codex P2 (fourth pass): inline pull-time prompt deliberately NOT bumping.** An earlier iteration of this fix added the bump symmetrically to `_apply_incoming_file`'s `keep-canonical` branch (the inline `mm pull --conflict-mode prompt` path). Codex caught the regression: when multiple peers conflict on the same file in one pull walk, bumping canonical's mtime mid-walk causes the next peer's `_apply_incoming_file` to mis-classify on the mtime gate (`local_mtime > remote_mtime`) and silently skip — hiding that peer's diverging bytes without surfacing a conflict. Reverted the inline bump. Users on `--conflict-mode prompt` who want fleet propagation should use `mm resolve` (deferred). Filed as TODO: defer the inline bump to end-of-pull-batch using `max(peer_mtime)` across all peers that conflicted on the same path, so the bump value beats every peer that was just walked. Pinned-as-intentional by `tests/test_conflict_copy.py::TestResolveLocalMtimeBump::test_inline_pull_keep_local_does_not_mutate_canonical_mtime`.

**Documentation.** New load-bearing section `## resolve(local) mtime bump` in `docs/invariants/conflicts.md` covers the cross-fleet propagation contract, peer_mtime capture timing, future-clamp symmetry rationale, and the cap edge case. `CLAUDE.md`'s invariant pointer table adds `_bump_canonical_mtime_post_resolve` to the conflicts row so future edits route here first.

**Test coverage.** 4 new tests in `tests/test_conflict_copy.py::TestResolveLocalMtimeBump` and 10 in `tests/test_integration.py::TestPushMtimeOnlyPropagation`. Resolve-side: post-inversion bump past sidecar mtime, pre-inversion bump past original-canonical mtime AND past ancient v0- sidecar mtime, end-to-end loop closure (after resolve(local), `_apply_incoming_file` with same peer manifest returns "skipped" not "conflicted"; no new sidecar written), and future-clamp cap when peer's sidecar mtime is 1h in the future. Push-side: helper unit cases (drift detected, false on sha mismatch, false on local-only, false on identical, **false on local-older** for the P2 forward-only invariant, **source_filter scoping** for the P3 hint), end-to-end manifest republish (`test_push_uploads_manifest_when_only_mtime_changed`), v0.12.2 regression pin (`test_push_still_bails_when_truly_nothing_changed`), and the status/dry-run UX gates (`test_status_flags_pending_mtime_only_changes`, `test_dry_run_push_flags_pending_mtime_only_changes`). 1541/1541 (excluding 1 pre-existing TCC sandbox failure on `test_diag_handles_missing_config` unrelated to this change — the test falls back to the real iCloud Drive path which macOS TCC blocks; passes in CI).

## [0.12.5] - 2026-05-10

**Track 11A from `docs/ROADMAP.md` Group 11 — token-cache invariant ownership consolidation.** Closes two pre-existing gaps: tests outside `test_token_usage.py` no longer pollute the user's real `~/.config/mind-meld/session-tokens.json`, and `gc_cache_entries` now routes through the `lock_and_get_files` wrapper so the version-check + files-isinstance-check normalization lives in ONE place — the docstring's "single owner of cache-shape invariants" claim is now literally true.

**Pytest isolation.** New autouse `_isolate_token_cache` fixture in `tests/conftest.py` (mirrors `_isolate_identity_cache`'s lazy-import shape; resets `CACHE_PATH`, `_WARNED_UNKNOWN_MODELS`, and `_WARNED_OVERSIZE_PATHS` per test). The redundant per-file fixture in `test_token_usage.py:71-75` is removed. Other test files that drive `mm push` / `_run_events_tail` / `warm_token_cache_inline` (`test_integration`, `test_init_events_backfill`, `test_silent_failure_contract`) used to leak into the real config dir on every local pytest run — now bounded to per-test `tmp_path`.

**`gc_cache_entries` refactor.** Swaps `locked_json_rmw + _normalize_cache + ljson.data.clear() + ljson.data.update(cache)` for `lock_and_get_files("block")` + in-place `files.clear() + files.update(keep)`. The `_normalize_cache` helper is deleted (no remaining callers). The `block` mode is the right choice — `mm gc` is a user-invoked maintenance command and waiting for contention is correct; an inline comment guards against future copy-paste from `_run_events_tail`'s `"warn"` mode.

**Cross-model HIGH caught and fixed during /review's adversarial pass.** The naive refactor would have shipped a regression where `gc_cache_entries` no longer stripped unknown top-level cache-root keys. Pre-refactor, `_normalize_cache + ljson.data.clear() + update(cache)` implicitly wiped any non-canonical top-level keys. Post-refactor, `lock_and_get_files` only normalized when version mismatched, leaving a cache like `{"version": 1, "files": {...}, "padding": "<huge>"}` to survive every `mm gc` run forever. Both Codex (HIGH) and Claude (INVESTIGATE) flagged this independently. Fixed in `lock_and_get_files`: when version matches, drop unknown top-level keys at lock-acquisition time. Now ALL cache acquisitions heal root bloat, not just gc — the wrapper truly owns the cache-shape invariant. The "future-version downgrade artifact" or "peer-injected bloat" recovery hatch is restored AND extended.

**Test coverage.** 5 new pinning tests in `TestGcCacheEntries`: `test_reaps_entry_not_a_dict`, `test_reaps_entry_with_missing_by_day`, `test_reaps_entry_with_empty_by_day`, `test_strips_unknown_top_level_keys_on_gc` (regression pin for the cross-model HIGH), `test_wrong_version_cache_normalized_to_empty`. Each asserts on-disk JSON state after gc, not just the `reaped` return value — the in-place mutation contract of `lock_and_get_files` (yields the `files` sub-dict, not the cache root) makes return-value-only assertions miss the regression class where `keep` is built but never persisted. All 1527 tests pass; pre-flight (pytest twice on a clean machine) confirmed identical output across runs.

**Two follow-ups deferred to `docs/TODOS.md`** (deliberately out-of-scope per the eng-review): (1) `gc_cache_entries`'s `int(max_age_s / 86400)` floor doesn't validate `max_age_s=0` (effectively reaps everything) or fractional-day inputs; (2) a direct positive cache-isolation test that stat-snapshots the real `~/.config/mind-meld/session-tokens.json` before/after the suite would be a stronger regression pin than the chosen "pytest twice" pre-flight.

## [0.12.4] - 2026-05-10

**`mm retro-fleet` "Skills incomplete" breadcrumb now admits cold-cache push as a second cause, mirroring `pre_token_peers`'s "OR with cold token cache" phrasing.** Pre-fix, the notes line read `"Skills incomplete: N peer(s) on pre-v0.11.27 — upgrade for accurate skill totals."` — accurate when every flagged peer was actually pre-v0.11.27, but a v0.11.27+ peer that pushed via the cold-cache code path (`cli.py:2886-2894` autopush gate skipping the token walk, or warn-mode flock contention yielding `files_dict=None`) emitted a sessions-snapshot whose project rows omit `skills_by_day` AND got falsely labeled "pre-v0.11.27" by that same breadcrumb. The wire genuinely can't distinguish "pre-v0.11.27 peer" from "v0.11.27+ peer with skipped walk" — both ship the field absent. The new text says both populations explicitly so the user can run `mm push` interactively (warms the cache, re-emits the field next push) on the named peer instead of chasing a non-existent upgrade.

**Track 11B as originally written was rejected during /plan-eng-review 2026-05-10.** ROADMAP Track 11B proposed a 3-LOC fix in `events.py:_scan_one_project`: drop the `if token_cache_files is not None:` gate so cold-cache snapshots emit `meta["skills_by_day"] = {}` (KEY-PRESENT-VALUE-EMPTY) instead of omitting the key entirely. **Codex outside-voice review caught:** the aggregator picks the LATEST sessions snapshot per `(device, source_root, claude_dir)` at `aggregator.py:830`. With the always-set fix, a v0.11.27+ device that pushes warm at T1 (populated `skills_by_day`) and then cold at T2 (synthetic `{}`) silently overwrites the T1 data, AND `aggregator.py:858` (`skills.available = True`) flips on so the renderer confidently shows "0 skills" instead of any "Skills incomplete" notice. The visible-misclassification bug becomes invisible-data-erasure — net regression. Cosmetic-only fix (this release) admits the wire ambiguity in the breadcrumb instead.

**Aggregator change scope.** Only `src/mind_meld/skills/retro_fleet/aggregator.py` — the `notes.append(...)` block at `:1862-1865`, the comment block at `:851-866` documenting the discriminator semantic, the `pre_skills_peers` field docstring on `SkillsAggregate` at `:253-265`, and the top-of-file aggregation rules at `:30-37`. `events.py:_scan_one_project` is deliberately untouched — the existing absent-on-cold behavior is the data-correct semantic. `pre_skills_peers` field name kept as-is for stability; semantic drift documented.

**Test coverage.** New `tests/test_retro_fleet_aggregator.py::TestFleetSkillsAggregation::test_skills_incomplete_breadcrumb_admits_cold_cache_ambiguity` pins the new wording — asserts `format_retro` output contains both `"pre-v0.11.27"` AND `"cold token cache"` AND `"mm push"` (recovery action). Existing `test_d4_empty_skills_dict_does_not_flag_pre_skills_peer`, `test_skills_by_day_empty_dict_when_no_skill_blocks`, `test_mixed_fleet_pre_skills_peers_flagged_correctly`, and `test_no_token_cache_means_no_tokens_field` stay green — flag behavior is unchanged; only the rendered text moves.

**Future option captured in `TODOS.md`.** The proper architectural fix — explicit `skills_walk_complete: bool` field on `SessionMetadata` (additive, total=False, no schema bump), aggregator preserves last-populated-skills per device-project, three-state discriminator — is filed as deferred work. Re-evaluate when pre-v0.11.27 peers age out and cold-cache-push disambiguation becomes operationally valuable.

## [0.12.3] - 2026-05-09

**`mm pull` now preserves the original source mtime instead of stamping every pulled file with `now-of-pull`.** Pre-fix, `_apply_write` and `_apply_conflict` (and the interactive keep-remote branch) wrote bytes via `fsutil.atomic_write_bytes` and never called `os.utime`, so every file landed with `st_mtime = the moment mm wrote it`. The manifest's recorded mtime was used for the "local newer" skip decision but dropped on the floor at write time. Result: any downstream consumer that orders by mtime got the wrong answer once locally-authored and remotely-pulled files were interleaved on disk. The most visible symptom: gstack skill preambles do `find ~/.gstack/projects/*/checkpoints -type f | xargs ls -t` for the "RECENT ARTIFACTS" hint, so a context-save authored on Mac A on Monday and pulled to Mac B on Friday showed as "newer" than a Mac-B-local context-save from Wednesday. (`/context-restore` itself was always safe — it filename-sorts deliberately.)

New helper `_restore_mtime_best_effort(path, mtime_iso)` parses the manifest's ISO-8601 string and calls `os.utime`. Plumbed through `_apply_write` and `_apply_conflict` (the sidecar gets remote mtime so users sorting `.sync-conflict-*` siblings see them in authorship order) and the interactive `keep-remote` branch in `_apply_incoming_file`. **`_apply_merge` and the merge-via-LCS interactive branch deliberately do NOT restore** — line-union merges produce locally-authored content, and backdating to remote mtime would cause peers' next pull to see `local_mtime <= remote_mtime` and skip the merged result, losing the union content fleet-wide.

**Future-clamp invariant (load-bearing).** The helper caps the applied mtime at `now + _MTIME_RESTORE_MAX_SKEW_SECONDS` (60s). Without the clamp, a peer with a bad clock OR a passphrase-holding attacker minting a manifest dated in 2099 would poison the victim's local mtime into a permanent `local_mtime > remote_mtime` skip at `_apply_incoming_file`'s mtime gate, silently locking the victim out of all future legitimate updates to that path. The 60s window absorbs normal NTP drift between Macs without admitting year-2099 abuse. Caught by /review's Codex adversarial pass before merge.

**Defensive parsing.** Catches `TypeError | ValueError | OverflowError | OSError` from both `datetime.fromisoformat(...).timestamp()` and the subsequent `os.utime`. `load_manifest` validates `rel_path` keys but does NOT type-check `files[*].mtime` values, so a peer can publish `mtime: 1234` (int) and drive `fromisoformat` into `TypeError`; pre-fix this would propagate and abort the pull with a partial write already on disk.

**Test coverage.** 11 new tests in `tests/test_pull_helpers.py`: write-side mtime restore happy path + None-leaves-now + unparseable-doesn't-fail-write; conflict sidecar mtime matches remote; merge does NOT inherit remote mtime (pins the deliberate non-restore); `TestRestoreMtimeBestEffort` × 6 covering None / empty / unparseable / missing-file / future-clamp / non-string. All 1521 tests pass.

## [0.12.2] - 2026-05-08

**Empty `mm push` no longer writes a phantom event row, no longer reports "1 file uploaded".** Pre-fix, every push (interactive or autopush) ran `_run_events_tail` at the HEAD of `_push_core` unconditionally — which always wrote a fresh `mm-push` row to the per-device daily events file, mutating its bytes, which then showed up as the only "change" the push uploaded. Result: `mm push; mm push; mm push` reported "1 modified" three times in a row even with zero user-data changes, and peers saw 1+ events-file change per pull from any idle-but-`mm push`-ed peer. v0.12.2 adds a substantive-change gate at the head of `_push_core`: build the manifest, fetch + recover remote, run `iter_source_diffs(skip_unchanged=True)`. If no source has any diff and `fetch.status != "corrupt"`, bail with `Nothing to push — everything is up to date.` BEFORE the events tail fires. Otherwise events tail runs, mm-events is re-walked to fold the just-written row into the manifest, tombstones regenerate, and the push proceeds. The `events.py:19-22` trust boundary is relaxed from "every push attempt" to "every push that uploads bytes" — the cursor stays accurate because no-op pushes never advanced it anyway. The gate counts ALL sources (user + mm-events), so an un-flushed event row from a prior partial-upload still triggers a fresh push that drains it. Pinned by `tests/test_integration.py::TestTrack7BEventsTail::test_events_tail_skips_on_no_content_push` (3-step: real push fires, empty push doesn't, real push fires again).

## [0.12.1] - 2026-05-07

**`/retro-fleet` skill output now renders inline instead of getting buried in a collapsed bash tool result.** Pre-fix, Step 4 of `SKILL.md` said "show the output verbatim" — interpretive guidance that left the agent with the bash tool result alone, which Claude Code collapses behind Ctrl-O. The user had to dig for the screenshot-ready ASCII card on every retro. SKILL.md now explicitly directs the agent to paste stdout into the assistant message, split into two pieces so both render correctly: the ASCII card (` ╔═══╗ ` through ` ╚═══╝ `) inside a fenced ` ```text ` block to preserve box-drawing alignment, and the markdown body that follows pasted unwrapped so headers and lists render. Doc-only change — no aggregator, CLI, or test changes. Ships fleet-wide once peers `mm autopull` and the bundled `~/.claude/skills/retro-fleet/SKILL.md` symlink resolves to the new version.

## [0.12.0] - 2026-05-07

**`/retro-fleet` borrows the gstack `/retro` shape: ASCII screenshot card, commit-type mix, peak hours, commit bursts, ship-of-the-window, week-over-week deltas, snapshot persistence, and an LLM-driven praise/level-up/focus narrative.** Pre-fix, the skill rendered a flat "stats and notes" markdown body and told the LLM to paste it verbatim — which left the gstack-style judgment layer (themes, ship of the week, narrative) entirely on the table. The aggregator now does the deterministic work and the SKILL.md hands off the narrative pieces to the LLM with a tone block opinionated enough to keep the output specific instead of fluffy.

The card is rendered through a **two-pass CLI flow**. Pass 1 (`mm retro-fleet 7d`) emits the markdown body plus a `MM_THEMES_PROMPT` JSON sidecar; the LLM reads it, synthesizes a noteworthy line + 3 themes, then re-invokes with `mm retro-fleet 7d --theme A --theme B --theme C --noteworthy "..." --name kb --no-save`. Pass 2 re-renders with a pixel-aligned ASCII card pinned at the top:

```
╔══════════════════════════════════════════════════════════════╗
║  kb · 2026-04-30 → 2026-05-07                                ║
╠══════════════════════════════════════════════════════════════╣
║  118 commits · 8 repos · 3 machines                          ║
║  +31k / -11k LOC · 37-day streak                             ║
║                                                              ║
║  NOTEWORTHY                                                  ║
║  Shipped fleet-wide skill counts (mm v0.11.27)               ║
║                                                              ║
║  TOP WORK                                                    ║
║  • Fleet retro polish + token usage rollup                   ║
║  • Locked-JSON contention primitive extraction               ║
║  • Release workflow auto-tag                                 ║
╚══════════════════════════════════════════════════════════════╝
```

The two-pass split is load-bearing: LLM-padded right borders drift by a char or two often enough to ruin screenshots. Routing the card through Python's deterministic padding (`CARD_WIDTH = 64`, `_render_ascii_card`) solves it without making the card content dumber. `--no-save` on the second pass prevents a duplicate snapshot write.

**Aggregator additions in the same single-pass loop.** `aggregate_git` now also collects:

- **Commit-type mix.** `_classify_commit_subject` matches `^([a-z]+)(?:\([^)]*\))?!?:` so `fix(cli):` and `feat!:` normalize to bare keywords. Renders as `Mix: feat 12 (40%) · fix 8 (27%) · ...`.
- **Hourly distribution.** Local-time histogram, top-5 peak rows shown with bar visualization.
- **Commit bursts.** 45-min-gap clustering into deep / medium / micro buckets. Named "bursts" not "sessions" intentionally — the heuristic counts commit clusters, not cognitive flow, and a real coding session that stops for lunch / debugging without commits will fragment into multiple bursts. Honest framing avoids collision with Claude Code "sessions" we already count.
- **Ship of the window.** Single highest-LOC commit (max `add+del`); subject preserved through `_safe_prose` so `feat(cli): /retro --foo` renders with punctuation intact instead of getting bucketed by `_safe_short`'s tight whitelist.
- **Week-over-week buckets.** Monday-anchored 7-day buckets when `window_days >= 14`. Markdown table with per-bucket commits, +/- LOC, and active-day counts.

**Snapshot persistence + trends-vs-last-retro deltas.** The aggregator writes a JSON snapshot to `~/.local/share/mind-meld/retros/YYYY-MM-DD-N.json` (mode 0o700, local-only, NOT synced) after every save-enabled run. On subsequent runs, the most recent matching-window prior loads via `_load_prior_snapshot` and renders as a `## Trends vs last retro` section — only when something actually changed (no stranded "no metric changed" bullet on identical retros). Snapshots reap by filename date at 365 days. The `MM_RETROS_DIR` env hook mirrors `MM_EVENTS_DIR` for power-user override and test isolation.

**`_safe_prose` (new).** A prose-friendly defang pass — strips terminal escapes + Rich markup + C0 controls but preserves printable punctuation (colons, slashes, hashes, em-dashes). Used for commit subjects (peer-controlled) and LLM-supplied theme/noteworthy/name lines where readability matters and the existing `_safe_short` whitelist would over-mangle. The trust boundary stays the same; the bucketing just stops mangling readable prose.

**SKILL.md updates** include the two-pass flow, theme synthesis instructions (one-line noteworthy + three themes ≤55 chars each, lead with the verb, name the artifact not the commit), and the praise / level-up / focus paragraph contract for the conversation-side narrative — anchored in actual commits, framed as investment-advice not criticism, with the gstack tone block (specific, earned, no coddling) carried over.

**Pre-landing review hardening (49 total new tests, 14 from review-gate).** The /ship pre-landing review caught several issues addressed in this same release rather than deferred:

- **Snapshot race + lex-sort bug.** `_save_snapshot` now uses `O_CREAT|O_EXCL` and bumps the sequence number on collision, so two concurrent retros can't silently overwrite each other on the same `seq=N+1`. Filename format moves from `YYYY-MM-DD-N.json` to `YYYY-MM-DD-NNN.json` (3-digit zero-pad) AND `_load_prior_snapshot` now sorts by parsed `(date, seq)` tuple instead of lexical filename. Pre-fix, lex-sort with `reverse=True` ordered `-9.json` AFTER `-10.json`, so once a single day produced 10+ retros the loader returned a stale snapshot as "most recent."
- **BiDi smuggling defense.** `_PROSE_CTRL_RE` now strips Unicode line/paragraph separators (U+2028/2029), NEL (U+0085), and the BiDi formatting characters U+202A–U+202E + U+2066–U+2069. Previously, a peer commit subject containing U+202E (RIGHT-TO-LEFT OVERRIDE) would flip downstream rendered text in iMessage / Slack / terminals — exactly the kind of trust-boundary leak the ASCII card flow widens.
- **Length caps on peer-controlled prose.** `_safe_prose` truncates input at 4 KiB before regex sanitization; `_classify_commit_subject` only inspects the first 256 chars. Defends against a peer planting a 10 MB commit subject that would otherwise burn CPU on every retro render. `_load_prior_snapshot` skips files >1 MiB before parse.
- **Theme count cap.** `_render_ascii_card` caps theme bullets at `MAX_THEMES = 3`. SKILL.md asks the LLM for "up to 3"; the renderer now enforces it so a misbehaving caller passing 50 `--theme` flags can't blow up the card height.
- **Window arg cap.** `_parse_window` rejects values >`_MAX_WINDOW_DAYS` (3650 = 10 years) so `mm retro-fleet 1000000000d` exits with a clean usage error instead of crashing on `timedelta` `OverflowError`.
- **Header date timezone consistency.** The markdown header now uses `astimezone().date()` to match the card's local-time framing. Pre-fix, the header showed the naive UTC date, which diverged from the card by a day near UTC boundaries.

49 total new tests cover everything above plus the original v0.12.0 surface: commit-type classification, burst gap heuristic, ship-of-the-window selection, weekly bucketing, snapshot roundtrip + window-mismatch + corruption tolerance, prior-delta computation, ASCII card padding (every line exact-width), truncation, terminal-escape defense across LLM inputs, themes-prompt JSON sanitization, and the second-pass `--no-save` short-circuit. The autouse `_isolate_retros_dir` conftest fixture redirects `MM_RETROS_DIR` to a per-test tmp dir, mirroring the existing events-dir / identity-cache isolation pattern.

## [0.11.30] - 2026-05-07

**Sync `~/.gstack-extend/` alongside `~/.gstack/`.** mind-meld already syncs
`~/.gstack/projects/<slug>/checkpoints/*.md` so `/context-restore` can pick
up where another Mac left off — but `gstack-extend` skills (pair-review,
test-plan, full-review) live in their own `~/.gstack-extend/` tree, which
was never synced. Resuming a `pair-review` session on a second Mac was
therefore impossible: the checkpoint markdown survived, but the
session-state machine (`session.yaml`, group progress, parked-bug ledger)
didn't follow.

This release adds `gstack-extend` as a default sync source mirroring the
`gstack` treatment: same auto-detect-on-`~/.gstack-extend/`-existence
behavior, same opt-in path for upgraders via `mm enable-source
gstack-extend`, same per-machine off-switch via `mm disable-source
gstack-extend`. The whitelist walker is scoped to `projects/` so
gstack-extend's per-machine root files (`config`, `just-upgraded-from`,
`update-snoozed`) are excluded by construction — only forward-compat
per-project state syncs.

**New installs:** `mm init` prompts Y/n for `gstack-extend` after `gstack`,
default Y when `~/.gstack-extend/` exists on disk.

**Existing installs:** `mm status` surfaces the standard "New source
available: gstack-extend" hint via the seen-sources mechanism. Run
`mm enable-source gstack-extend` to opt in or `mm disable-source
gstack-extend` to dismiss.

**Caveat (gstack-extend-side gap, not solved here).** This sync source
covers anything gstack-extend writes to `~/.gstack-extend/projects/`. As
of writing, `pair-review` still persists session state to
`<workspace>/.context/pair-review/` (workspace-local, ephemeral under
Conductor). Closing the resume-loop fully requires a parallel change in
gstack-extend to mirror gstack's `~/.gstack/projects/<slug>/checkpoints/`
layout — that lives in the gstack-extend repo, not here. Once it ships,
no further mind-meld change is needed.

## [0.11.29] - 2026-05-07

**`mm retro-fleet` drops the misleading "across N projects" stat; ephemeral-workspace count moves inline with sessions.** User feedback: a 7d retro reported "171 sessions across 89 projects" against a fleet that has only ever worked on ~10 real repos. Root cause: Claude Code keys session storage by encoded cwd (`~/.claude/projects/-Users-kb-conductor-workspaces-mind-meld-pangyo-v1/`), so every Conductor workspace and git worktree gets its own dir on disk and the aggregator's `(device, source_root, claude_dir)` tuple counted each as a distinct project. The number was correct as "unique session-storage paths" but useless as "projects." The repo count under `## Code shipped` already covers the meaningful signal — it dedups by canonical git remote URL.

`format_retro` now renders one of:

- `- 171 sessions, 83 of which are in ephemeral Conductor workspaces` (when any sessions are ephemeral)
- `- 171 sessions` (when none are ephemeral)

The Notes section no longer carries the "X of those sessions are in ephemeral Conductor workspaces" aside — folded into the inline qualifier so the user reads it once at point-of-use. `data.sessions.projects` is still computed and exposed on the dataclass for any external consumer; just unrendered. Pinned by `tests/test_retro_fleet_aggregator.py::TestTokenRender::test_render_hidden_when_no_tokens` (asserts `5 sessions` present, `across 1 projects` absent).

## [0.11.28] - 2026-05-06

**`/retro-fleet` skill auto-syncs the fleet before aggregating.** Pre-fix, the
skill ran `mm retro-fleet <window>` against whatever events were already on
local disk — peer activity since the last `mm autopull` was invisible to the
retro, and today's local commits / sessions / skill counts hadn't landed in
the events JSONL yet because `_run_events_tail` only fires on push. Result:
"I just pushed from the other Mac, why isn't it in the retro" and "I made
3 commits today, the streak counter says 0."

`SKILL.md` now adds a Step 1 that runs `mm autopush` (refreshes today's local
events file with current commits/sessions/skills) followed by `mm autopull`
(collects what other Macs have pushed since last sync) before invoking the
aggregator in Step 2. Both commands are silent, never prompt, and exit
gracefully on errors or when mm isn't initialized — safe to run
unconditionally per the v0.8.1 contract. The old Step 1 (aggregator) becomes
Step 2; the old Step 2 (present output) becomes Step 3.

**CLI behavior unchanged.** `mm retro-fleet` and `retro_fleet_cmd` in
`cli.py` are deliberately untouched — scripted exports
(`mm retro-fleet 30d > /tmp/retro.md`) stay fast and deterministic. Only the
skill (the LLM judgment layer) does the autopush/autopull wrap, and the user
can skip it by saying "stale retro" or "offline retro" or by having just run
`mm push` and `mm pull` manually.

**No code changes.** `src/mind_meld/skills/retro_fleet/SKILL.md` only.
`tests/test_wheel.py` only checks SKILL.md *exists* in the wheel; content is
intentionally not pinned.

## [0.11.27] - 2026-05-06

**Fleet-wide skill-invocation counts via Claude Code session jsonls.**
`mm retro-fleet` was reporting `0` skill invocations on machines where
the user had obviously been using skills heavily. Root cause: gstack's
`~/.gstack/analytics/skill-usage.jsonl` (the prior data source) silently
stopped logging `skill_run` events on 2026-04-26. That source was also
this-machine-only — peers' skill activity never landed in the retro.

Replaced with Claude Code's own session jsonls — every Skill tool
invocation is recorded as an assistant `tool_use` block with
`name:"Skill"`, `input.skill`, and a UTC timestamp. Same jsonl tree
`token_usage` already walks. The walker now produces two views in one
I/O pass: `tokens_by_day` (existing) AND `skills_by_day` (new). Both
views land on the v=2 `sessions-snapshot` event row per project; the
aggregator slices `skills_by_day` to the retro window exactly like it
slices tokens.

Skill counts are now FLEET-WIDE — the locked output drops the
"this machine only" caveat from the Skills section header.

**Schema additions (additive on v=2, no schema bump):**
- `SessionMetadata.skills_by_day: dict[str, dict[str, int]]`
- `CacheEntry.skills_by_day` + new public `SkillBuckets` type alias
- `SkillsAggregate.pre_skills_peers: set[str]`

**Mixed-fleet rollout — load-bearing discriminator (D4 from
/plan-eng-review 2026-05-06):** `pre_skills_peers` detection uses
`"skills_by_day" not in proj` (KEY-ABSENT), NOT a falsy-check. Empty
`{}` is a content signal ("no skills used in window"), not a version
signal. Distinct from `pre_token_peers` semantics where every session
generates tokens.

**Cache shape upgrade gate (D2):** `token_usage.get_or_compute` checks
`"skills_by_day" in entry` on the size/mtime cache hit — pre-v0.11.27
entries match size/mtime but lack the field, so they fall through to
a fresh walk. NOT a `CACHE_VERSION` bump (would invalidate token data
fleet-wide unnecessarily).

**Skill dedup is by `tool_use.id`** (Anthropic `toolu_*` format),
independent of the existing `message.id` token dedup. Claude Code
emits each model iteration as a separate jsonl line under the same
`message.id` — iterations have DIFFERENT content blocks, so message-id
dedup drops legitimate skill calls. Caught at smoke-test time on real
data.

**Trust boundary (defense-in-depth):** skill names are sanitized at
RENDER time via `_safe_short` (mirrors v0.11.14 model-name sanitization).

**API changes:**
- New: `walk_jsonl_buckets(path) -> tuple[dict[str, DayBucket], SkillBuckets]`
- `walk_jsonl_token_buckets` retained as back-compat shim
- `get_or_compute(...)` returns tuple of both views
- `events._aggregate_tokens_for_project` renamed to
  `_aggregate_jsonl_views_for_project`; returns both views
- `aggregator.aggregate_sessions(...)` returns
  `tuple[SessionsAggregate, SkillsAggregate]`

**Removed:** the gstack-analytics reader path (`_iter_json_stream`,
`_read_skill_usage`, the standalone `aggregate_skills` function,
`GSTACK_ANALYTICS_DIR`, `JSON_STREAM_MAX_BYTES`,
`SKIP_CATEGORY_SKILL_USAGE`, `RetroData.skill_usage_path`, the
`aggregate(... skill_usage_path=...)` parameter). ~80 lines deleted.

**Test coverage:** 17 new tests across `tests/test_token_usage.py`,
`tests/test_events.py`, and `tests/test_retro_fleet_aggregator.py`,
covering skill detection, tool-use-id dedup, malformed-shape
tolerance, the D2 cache-upgrade gate, the D4 absent-vs-empty
discriminator, subagent skill attribution, the trust-boundary
sanitizer, and the "this machine only" caveat absence. 6 obsolete
gstack-analytics tests removed (event-side parse-error tolerance
still covered by `TestTolerantReader`).

## [0.11.26] - 2026-05-06

**Release workflow fix: drop dead PROGRESS auto-append; backfill v0.11.24 + v0.11.25 rows.** The v0.11.24+ release workflow's "Append PROGRESS.md row" step was rejected by branch protection on every release where the row wasn't already in the PR — the workflow tried to `git push` a chore commit directly to `main`, but the ruleset requires PRs. v0.11.23 only "succeeded" because the row was pre-added in PR #74 and the script's idempotent skip exited 0 before reaching the push. v0.11.24 and v0.11.25 both hit the wall and shipped tagged + released but without their PROGRESS rows.

The step was architecturally incompatible with branch protection — a workflow pushing to a protected branch is broken by definition. Removed entirely. Replaced with a tail "Verify PROGRESS.md row exists" check that emits a `::warning::` (not a failure) when the row is missing, so future releases ship cleanly even on a missed row. Convention is now documented in CLAUDE.md: **the PROGRESS row goes in the same PR as the pyproject + CHANGELOG bump.** This PR backfills both missing rows (v0.11.24 internal hygiene refactor; v0.11.25 commit streak counter) using the same lead-paragraph extraction the workflow used.

## [0.11.25] - 2026-05-06

**Commit streak counter in fleet retro.** Adds a one-line "N-day commit
streak" to the `## Code shipped` section of `mm retro-fleet`, computed
fleet-wide from the events buffer. Streak counts consecutive local-day
stretches ending at (or one day before) the retro window's end with at
least one author-matched commit. Deduped across machines via
`(canonical_remote_url, sha)` so a commit captured by two devices counts
once. Window-independent — a 7d retro on a 30-day streak shows 30
(capped only by the 90-day events retention).

GitHub-style grace day: if today has no commits but yesterday does, the
streak still counts (an in-progress workday doesn't break it). The
author filter applies — a third-party PR-merge commit on a quiet day
won't keep your personal streak alive. Hides cleanly when the streak is
zero. Day keys use the system's local timezone so a late-night commit
shows up "today" instead of leaking into "tomorrow" via UTC drift.

## [0.11.24] - 2026-05-06

**Internal hygiene refactor: Track 10A — token-usage DRY + perf polish.**
Pure refactor; no user-visible behavior change. Consolidates the four
token-bucket merge sites (`token_usage._accumulate`, `slice_window`,
`events._aggregate_tokens_for_project`, `aggregator._merge_token_window`)
behind shared `merge_usage_bucket` / `merge_by_model` helpers + a
`TOKEN_FIELDS` constant + `zero_day_bucket` / `zero_model_bucket`
factories — all now public on `mind_meld.token_usage`. Adding a 5th
token field is a one-line change to the constant. Aggregator keeps its
bespoke filtered loop with `_safe_int` hardening intact, preserving the
peer-controlled-events trust boundary.

`_run_events_tail` and `_run_events_backfill` collapse from inline
`locked_json_rmw + version-check + isinstance-check` blocks to a single
`with token_usage.lock_and_get_files(mode) as files:` call. The new
context manager owns cache-shape invariants in one place; `None` yield
signals warn-mode contention.

`is_cache_cold` now short-circuits on `stat.st_size < 64` before
parsing, saving the read+parse cost on missing or near-empty caches.
The structural `json.loads` + `version`/`files` check stays — Codex
adversarial review caught two correctness regressions in earlier
optimization attempts (regex byte-scan substring match, nested-version
collision); the structural parse is the only sound approach.

A skip-unchanged-write optimization on `lockedjson.locked_json_rmw`
was prototyped and reverted: empirical measurement showed the
`sha256(json.dumps(...))` snapshot cost ~3.3ms per context, exceeding
the ~2.0ms cost of an always-write since `_write_json` doesn't fsync.

### Changed

- Extracted `TOKEN_FIELDS`, `merge_usage_bucket`, `merge_by_model`,
  `zero_day_bucket`, `zero_model_bucket`, `lock_and_get_files` as
  public API on `mind_meld.token_usage`.
- Refactored `_accumulate`, `slice_window`,
  `events._aggregate_tokens_for_project`, and the cli's
  `_run_events_tail` / `_run_events_backfill` / `warm_token_cache_inline`
  to use the shared helpers.
- Aggregator adopts the new constants but keeps its bespoke loop +
  `_safe_int` hardening on peer-controlled events.

### Added

- 30+ new tests: TOKEN_FIELDS schema-stability pin, helper unit tests,
  fleet-retro determinism golden + totals fixtures, `_safe_int`
  retention regression pin, version-mismatch and corrupt-cache pins
  in `is_cache_cold`, perf-pin benchmark for `merge_usage_bucket`.

## [0.11.23] - 2026-05-06

**Auto-pin iCloud storage on `mm init`.** Fresh Macs no longer wait for
iCloud File Provider materialization on the first `mm pull`. Init now
calls `brctl download <storage_path>` (Apple's iCloud File Provider
CLI) once at the end of registration, asking iCloud to keep storage
blobs resident locally. brctl is non-destructive, idempotent, and
async — it queues the request and returns immediately while iCloud
materializes files in the background. Surfaces a one-line `Storage
pinned for fast pulls.` confirmation on success; falls back silently
to a Finder right-click tip on any error (brctl missing, timeout,
non-zero exit, non-iCloud storage path).

Track 9A originally bundled this with a 150-line parallelization of
`_download_and_apply` for a measured 7.3× speedup on a fresh-Mac
1449-blob iCloud-cold pull. Dogfooding showed the parallelization
solved a problem the auto-pin prevents at the source — once the
storage folder is pinned, sequential reads are already fast (<5s on
the same workload). /plan-eng-review reduced scope to the auto-pin;
parallel-fetch is preserved in `docs/ROADMAP.md` Future with a clear
revisit trigger (sustained slow pull AFTER auto-pin, OR 10k+ blob
fleet with user-visible memory pressure).

### Added
- `mm init` auto-pins iCloud storage via `brctl download` so first
  pull on a fresh Mac reads resident blobs instead of blocking on
  iCloud materialization (`src/mind_meld/cli.py`,
  `_auto_pin_storage_for_icloud` helper at the end of `init()`).
- README "Fast pulls (auto-pin)" section documenting the auto-pin
  behavior, the `brctl evict` undo path, and the non-iCloud-path
  silent-skip case.
- 6 new tests in `tests/test_init_auto_pin.py`: brctl success path,
  non-iCloud skip, brctl missing (FileNotFoundError),
  TimeoutExpired, non-zero exit, init-wiring smoke test.

### Changed
- `docs/ROADMAP.md` Track 9A renamed to "Auto-pin storage on init"
  (1 task, ~30 min CC, low risk). Parallel-fetch task moved to
  Future with revisit trigger and architecture notes from D1 (in-
  order outcomes preservation) and D2 (submit-all-upfront pattern
  mirroring `events.py:walk_git_projects`).
- Group 10's serialization-after-Group-9 dependency note removed —
  Track 10A's `_run_events_tail` / `_run_events_backfill` edits no
  longer collide with Track 9A (different cli.py regions).

## [0.11.22.1] - 2026-05-05

**Roadmap reassessment.** Docs-only ship: drained 5 inbox items from
`docs/TODOS.md ## Unprocessed` and added Group 10 (token-usage
post-ship cleanup) to `docs/ROADMAP.md`. Group 10 is a single Track
10A with 4 tasks deferred from /ship pre-landing reviews of the
v0.11.14+ token_usage.py + lockedjson.py work: DRY for
`_merge_usage_bucket`, dirty-flag write skip in
`lockedjson.locked_json_rmw`, `is_cache_cold` cheap stat-heuristic,
and DRY for the `cli.py` token-cache lock+normalize block. Group 10
serializes after Group 9 (cli.py file collision). The
`[retro].deny_emails` subtractive override (plan-eng-review,
explicit `defer` tag) was triaged to Future. No code, test, or
behavior changes — `## TODO_FORMAT` audit goes from fail → pass.

### Changed

- **`docs/ROADMAP.md`** — added Group 10 with Track 10A (4 tasks);
  updated intro paragraph to mention Group 10 queued behind Group 9;
  appended Group 10 to the Execution Map adjacency list and Track
  detail; added `[retro].deny_emails` to the Future section.
- **`docs/TODOS.md`** — drained `## Unprocessed` (5 legacy bullets →
  0); section now empty awaiting next inbox item.

## [0.11.23] - 2026-05-05

**Token totals in `retro-fleet` now share the cost-estimate basis,
and unpriced-model volume is surfaced in Notes.** Prior versions summed
top-level day-bucket totals — which include `<synthetic>` (Claude Code's
internal tool-execution turns) — into the displayed "Tokens this window"
line, while `estimate_cost` correctly excluded them. The result was a
displayed token count larger than the cost line implied. Now both share
the same per-model basis. As a follow-on, models present in the fleet's
`tokens_by_model` but missing from `PRICING` are flagged in a Notes line
so the cost line is honestly an under-estimate rather than silently
missing volume.

### Fixed

- **`_merge_token_window` derives top-level totals from `by_model`
  excluding `COST_EXCLUDED_MODELS` (aggregator.py).** `<synthetic>` rows
  no longer contribute to `tokens_input` / `tokens_cache_create` /
  `tokens_cache_read` / `tokens_output`. `tokens_by_model` retains every
  peer-reported entry so render-side filters (per-model line, cost
  estimate, unpriced-model breadcrumb) keep operating on the full set.
  Pinned by `tests/test_retro_fleet_aggregator.py::TestSyntheticAndUnpricedTokens`
  with a bucket whose top-level numbers are deliberately wrong vs. the
  by_model truth so a future regression that re-introduces top-level
  summing fails the test loudly.

### Added

- **Unpriced-model breadcrumb in `format_retro` Notes (aggregator.py).**
  New `_unpriced_token_summary` walks `tokens_by_model`, counts entries
  that are neither in `PRICING` nor in `COST_EXCLUDED_MODELS`, and sums
  their token volume. When non-zero, format_retro emits a single Notes
  line: *"`6.0M` tokens from `1` unpriced model(s) excluded from cost
  estimate."* Renders only when there is real unpriced volume — a fleet
  whose only non-priced model is `<synthetic>` (cost-excluded by design,
  not unpriced) gets no note. The stderr breadcrumb in
  `token_usage.estimate_cost` is preserved for runtime visibility; the
  Notes line is the at-rest record in the rendered retro. Pinned by
  three cases in the same test class (renders, hidden-when-clean,
  synthetic-alone-doesn't-trigger).

## [0.11.22] - 2026-05-05

**Fix: `mm retro-fleet` CLI subcommand replaces the `python -m
mind_meld.skills.retro_fleet.aggregator` invocation in SKILL.md.** Real
fleet feedback on v0.11.21: the documented `python -m ...` form failed on
macOS systems where only `python3` is on PATH (no `python`), forcing the
user to fall back to invoking the pipx venv's interpreter directly. Switching
SKILL.md to `python3 -m ...` would only paper over the symptom — the
dominant install path is `pipx install mind-meld`, which puts mind_meld in
`~/.local/pipx/venvs/mind-meld/` where neither `python` nor `python3` can
import it. The aggregator's own subprocess invocation already recognized
this and uses `sys.executable -m mind_meld.cli` (aggregator.py:680). This
release applies the same insight in the other direction: route through the
`mm` console-script (always on PATH wherever mm is installed).

### Added

- **`mm retro-fleet [window]` typer subcommand (cli.py:retro_fleet_cmd).**
  Thin shim that lazy-imports `mind_meld.skills.retro_fleet.aggregator.main`,
  forwards the positional `window` arg (default `"7d"`) and the
  `--no-author-filter` flag, and propagates the aggregator's exit code via
  `typer.Exit`. The aggregator's `argparse`-based `main()` is unchanged —
  direct `python -m mind_meld.skills.retro_fleet.aggregator` still works
  from a development checkout, it's just no longer the public surface.
  Listed in `mm --help` (matches the `autopull` / `autopush` / `install-skills`
  precedent of leaving "designed for Claude Code" commands discoverable for
  debugging and direct power-user use). Pinned by
  `tests/test_retro_fleet_cli.py::TestRetroFleetCommand` (default 7d,
  explicit window, `--no-author-filter` forwarding, exit-code propagation,
  --help discoverability).

### Changed

- **SKILL.md (retro-fleet) tells Claude Code to invoke `mm retro-fleet
  <window>`.** All three documented invocations updated (default,
  `--no-author-filter`, `MM_EVENTS_DIR=...` override). The error-handling
  guidance now says: do NOT fall back to `python -m
  mind_meld.skills.retro_fleet.aggregator` — explains the `python` vs
  `python3` PATH inconsistency and the pipx-venv isolation that hide
  mind_meld from any other interpreter.

- **CLAUDE.md / docs/invariants/events-retro.md.** Source-layout entry,
  `Commands:` line, and invariants pointer table all reflect the new
  `mm retro-fleet` route. New invariants block "mm retro-fleet [window]
  typer wrapper (load-bearing, v0.11.22)" documents the routing rationale
  so a future contributor doesn't reintroduce the `python -m` form.

## [0.11.21] - 2026-05-04

**Security: close pull-side path-traversal gap on peer-crafted manifest
`rel_path` keys.** A peer with the storage passphrase could mint an
authenticated manifest whose `sources[*].files` keys contained `..`
segments or absolute paths; on `mm pull`, `_download_and_apply` would
build `local_path = base_path / rel_path` (cli.py:1763) and write
decrypted blob bytes anywhere the user could write — `~/.ssh/authorized_keys`,
`~/.zshrc`, `/etc/cron.d/*`, etc. — escalating passphrase + storage-write
into RCE on every fleet device that pulls. The sibling defense for the
`sha256` storage-key component already existed (`storage/keys.py:_validate_component`,
v0.8.x); this extends the same pattern to `rel_path`, which is the more
reachable surface (sha is hex-bounded, rel_path is free-form UTF-8).

### Fixed

- **`manifest._validate_rel_path` rejects unsafe rel-paths at the load
  boundary (manifest.py).** New helper called from `load_manifest` over
  every `sources[*].files` key and every tombstone path part. Rejects:
  empty string, null bytes, leading `/` or `\` (absolute path — Python's
  `Path('/base') / '/abs'` returns `Path('/abs')`), Windows drive letters
  (`C:foo`), and any segment equal to `..` after splitting on either
  separator. Honest writers (`manifest.walk_*`) build rel keys via
  `path.relative_to(base)`, which by construction NEVER produces `..`
  segments or absolute paths — so the validator only fires on
  attacker-crafted manifests; legitimate sync is unaffected.
  `_fetch_remote_manifest` already catches `ManifestError` and falls
  through to the sidecar/peer recovery chain, so a malicious manifest
  degrades to a clean "corrupt" status rather than crashing the pull.
  Pinned by `tests/test_manifest.py::TestLoadManifestRelPathTraversal`
  (11 cases covering `..`/absolute/drive-letter/null-byte/empty/tombstone
  variants plus a sanity case for legitimate nested paths).

- **`_download_and_apply` belt-and-braces `is_relative_to(base_path)`
  guard (cli.py).** Even though `load_manifest` is the canonical load
  boundary, a future load path that bypasses it (legacy on-disk cache,
  hand-built test fixture) must STILL not let a peer-controlled rel_path
  escape. Before calling `_apply_incoming_file`, resolve both
  `local_path` and `base_path` with `resolve(strict=False)` (handles
  not-yet-created files and normalizes symlinks consistently on both
  sides — symlinked source roots stay legitimate) and assert
  `local_path.is_relative_to(base_path)`. Rejected files surface as a
  per-file `failed` outcome (matching the v0.8.1 bad-blob-key isolation
  pattern), so per-file isolation is preserved and the rest of the pull
  continues. Pinned by
  `tests/test_pull_helpers.py::TestDownloadAndApplyPathTraversalGuard`
  (3 cases: `..`-escape, absolute-path override, legitimate nested-path
  sanity check).

- **Fuzz strategies narrowed for round-trip tests
  (`tests/test_manifest_fuzz.py`).** The pre-v0.11.21
  `tombstone_key_strategy` and `source_files_strategy` generated
  arbitrary text (including `\x00`, `..`, absolute paths) and flowed
  into `test_load_manifest_round_trip_preserves_keys` and
  `test_v1_promotion_migrates_bare_tombstone_keys`, which now pass
  through the new validator. Replaced with `valid_rel_path_strategy`
  (path-safe-alphabet segments, no `..`/`.` segments) so round-trip
  tests fuzz the happy path. The wild-input invariant (normalize
  tolerates garbage) is still covered by `arbitrary_dict_strategy`
  because `normalize_manifest` itself is unchanged; the load-side
  rejection of garbage is covered by the existing
  `test_load_manifest_either_returns_normalized_or_raises`.

### Documented

- **New invariant in `docs/invariants/sync.md`: rel_path traversal
  defense (load-bearing, v0.11.21, security).** Captures the threat
  model (passphrase-holder escalation), the rejection grammar, the
  explicit "do NOT pre-`posixpath.normpath`" footgun (normpath collapses
  `a/..` to `.` and would silently drop the suspicious segment), and
  the defense-in-depth split between the load-boundary validator and
  the apply-site `is_relative_to` assertion. Routing entry added to
  `CLAUDE.md` invariant table for `cli.py:_download_and_apply` and
  `manifest.py:_validate_rel_path`.

## [0.11.20] - 2026-05-04

**Fix `mm pull` re-merging the same files on every invocation.** Three
consecutive `mm pull`s on identical inputs reliably reported new
"merged" files because `merge_jsonl`'s sort key was non-deterministic
across processes for tied timestamps — defeating the no-op suppression
in `_apply_merge` and causing fleet-wide ping-pong re-uploads.

### Fixed

- **`merge_jsonl` tied-`ts` sort is now deterministic across processes
  (merge.py).** The merge built its line collection by iterating a
  `set`, then sorted with `key=lambda x: x[0]` — `ts` only. Tied-`ts`
  lines retained set-iteration order, which is hash-randomized per
  Python process via `PYTHONHASHSEED`. Each `mm pull` is a fresh
  process → fresh seed → different ordering → merged bytes ≠ local
  bytes → `_apply_merge`'s no-op suppression failed → "merged" outcome
  fired forever. The downstream effect: every pull rewrote the file,
  every push shipped a new SHA, and other devices saw the file as
  "modified" → re-merged on their next pull, generating a perpetual
  fleet-wide loop on any jsonl with sub-second `ts` collisions
  (`analytics/skill-usage.jsonl`, `projects/*/resources-shown.jsonl`,
  etc). Fix: tie-break on full line content
  (`key=lambda x: (x[0], x[1])`). Pinned by
  `test_tied_ts_ordered_by_full_line_content` (contract) and
  `test_jsonl_tied_ts_deterministic_across_hash_seeds` (subprocess
  cross-seed determinism). One final canonicalizing merge will fire
  per affected file on each device's next pull, after which the file
  stabilizes. `merge_lines` (MEMORY.md) was already deterministic —
  it sorts on full line content via `sorted(merged)`.

## [0.11.19] - 2026-05-03

**Group 8 hotfix sweep — three bugs caught during a forward-looking
review of the retro-fleet code paths.** One residual trust-boundary
gap missed during the v0.11.14 model-string defang, one concurrency
anti-pattern in the v0.11.17 identity cache, and one GHE schema-variant
intolerance in the noreply email derivation.

### Fixed

- **Peer-controlled repo URLs are now defanged before render
  (aggregator.py).** `canonicalize_remote_url` preserves ANSI / OSC /
  DCS escape sequences and bell characters from peer-controlled
  `git remote get-url origin` output. The shortening helper then
  rendered that string directly into the LLM-consumed retro markdown —
  same trust-boundary class as v0.11.14's model-string defang, just
  missed for repo URLs. New `_safe_repo_url` helper
  (`strip_terminal_escapes` + URL-safe whitelist) is applied BEFORE
  `_shorten_repo_url` so the trusted `[...]` placeholder isn't itself
  bucketed by the whitelist. Pinned by `TestSafeRepoUrl` (CSI strip,
  OSC 52 clipboard escape strip, bare control byte bucket, markdown
  breakers bucket, end-to-end `format_retro` escape strip).
- **`identity` flock released during slow subprocess gather
  (identity.py).** `gather_local_identities` and
  `refresh_identity_cache` previously held the
  `identity-cache.json` flock for the entire `_do_full_gather`
  walk — up to ~10s wall-clock (`_GIT_GLOBAL_TIMEOUT_S` +
  `_PER_REPO_BUDGET_S` + `_GH_TIMEOUT_S`). A concurrent autopush hook
  would block on the lock for that duration, blowing past the 250ms
  events-tail budget for no benefit. Refactored to release-acquire:
  brief read under flock → release → slow gather → brief write under
  flock. Phase-3 write splits into `_persist_or_yield_concurrent`
  (defers to a peer writer that landed a fresh cache during the
  gather) and `_persist_force` (always overwrites, used by
  `mm refresh-identity`). Pinned by `TestLockDiscipline`: a
  non-blocking flock probe inside `_do_full_gather` confirms the
  lock is released for both code paths; concurrent-writer freshness
  re-check; force-mode override of peer writer.
- **`gh api user` `id` now accepts decimal-digit `str` shape
  (identity.py).** `_gather_gh_noreply_email` previously required
  `data["id"]` to be `int`, which is canonical for github.com but
  not for some GitHub Enterprise instances that return string-encoded
  ids when the underlying numeric value would overflow a JSON Number
  safely-representable bound. Dropping those uids on the floor lost
  the user's PR-merge attribution. New `_coerce_gh_uid` accepts
  non-negative `int` OR decimal-digit-only `str` (via `.isdigit` +
  `int()` round-trip — the strict whitelist guards against payloads
  like `"99999\nINJECTED"`). Rejects `bool` (subclass of `int` in
  Python — explicit), negative `int`, and non-digit `str`. Pinned by
  4 new gh tests covering accept and reject paths.

## [0.11.18] - 2026-05-01

**Fleet-wide author email trust set: identical retros across every
machine after sync.** Pre-v0.11.18 the retro-fleet aggregator built
its author-email filter from the running machine's local state only —
`git config --global user.email`, per-repo overrides, gh noreply
form, `[retro].author_emails`. Two machines with different identities
produced different retros from the same synced events. The fix:
each push embeds the locally-known emails in a new `local_emails`
field on the `mm-push` event row; the aggregator unions across every
peer's rows at retro time. After push+pull, every machine sees the
same union and renders identical output.

### Added

- **`mind_meld.identity` module.** Single owner of the locally-known
  author-email set. Cached at `~/.config/mind-meld/identity-cache.json`
  (mode 0600, fcntl-flocked via the existing `lockedjson` primitive),
  7-day TTL. Cold/stale paths emit a single `mm: notice: refreshing
  identity cache (one-off)` to stderr and run a synchronous refresh
  inline. The user explicitly accepted the one-off slow path during
  /plan-eng-review (D1) — no autopush-budget contortions, no
  background threads. Sources unioned: global git config, per-repo
  git config (5s wall-clock budget), `[retro].author_emails`,
  `gh api user`-derived noreply form. Trust-rooted: never walks
  `git log` so collaborator emails on shared repos can't leak in.
- **`local_emails` field on `MmPushEvent`.** Additive on the v=2
  TypedDict (no schema bump — same precedent as v0.11.14's
  `tokens_by_day`). Absent on pre-v0.11.18 peer rows;
  aggregator silently skips those at the union step. Empty list is
  emitted explicitly when the running machine has no configured
  identities, distinguishable on the wire from "pre-v0.11.18 peer."
- **`mm refresh-identity` CLI subcommand.** Force-refreshes the
  cache. Use after editing `[retro].author_emails`, running
  `gh auth login`, or changing `git config --global user.email`.
  `--json` flag for scripting; default output lists the resolved
  emails. Exits 1 with a `mm: warning:` when no emails resolve.
- **Identity cache warm at `mm init`.** `_run_events_backfill` now
  calls `identity.refresh_identity_cache(force=True)` at the end of
  init so the first push has a hot cache and emits no slow-path
  notice. Init isn't time-budgeted; this is free.

### Changed

- **`aggregator.aggregate(author_emails=...)` signature.** Now accepts
  `frozenset[str] | None`. `None` means filter explicitly disabled
  (used by `--no-author-filter`). Non-None is unioned with every
  peer's `local_emails` from on-disk events to build the fleet-wide
  trust set. Empty `frozenset()` with no fleet activity preserves the
  pre-v0.11.18 behavior of "filter disabled."
- **`aggregator.gather_author_emails()` is now a thin shim** that
  delegates to `mind_meld.identity.gather_local_identities()`.
  Backwards-compat preserved for any out-of-tree library callers
  importing the function directly.

### Migration

- **Lockstep upgrade recommended.** During the rollout window, peers
  on v0.11.17 emit no `local_emails` field; their identities aren't
  in the fleet union until they upgrade and push. The running
  machine's local set still covers self-emitted commits via the
  fallback path. Per /plan-eng-review (D3), no `pre_emails_peers`
  Notes breadcrumb ships — upgrade all peers in lockstep instead.
- **Existing `[retro].author_emails` configs keep working unchanged.**
  Per /plan-eng-review (D4), the knob is additive: local config
  unions with the fleet trust set rather than replacing it.

### Documentation

- **CLAUDE.md split into `docs/invariants/`.** CLAUDE.md grew to 584
  lines as load-bearing invariants accumulated across 17 minor
  releases. Split per-topic invariant blocks into 5 files under
  `docs/invariants/` (sync, conflicts, init-devices, events-retro,
  auto-upgrade). CLAUDE.md keeps orientation, Auto Commands, Spec
  pointer, and a new file-path-keyed routing-rules table that names
  which invariant doc to read FIRST when editing each code area.
  CLAUDE.md drops 584 → 98 lines (83% reduction).

## [0.11.17] - 2026-05-01

**Suppress phantom `merged` reports when pull merge is a no-op.** Every
`mm pull` was reporting "1 merged" (or more) for `.jsonl` and
`MEMORY.md` files even when the line-union merge produced bytes
byte-identical to what was already on disk — the dominant case when
local already contains everything remote has. Users felt like new
content was arriving on every pull when in fact nothing had changed.
`_apply_merge` now compares the merged result to local bytes; on
equality it skips the write entirely (preserving mtime, so the next
push doesn't manufacture a phantom modified entry from the touched
file) and returns `unchanged` instead of `merged`. The `unchanged`
outcome was already excluded from pull-summary totals, pullhistory,
and the per-project `.mind-meld-log.md` sync log, so the noise
disappears across all reporting surfaces with no further plumbing.
Real merges (where remote contributes lines local doesn't have) still
write and report `merged` as before. Dry-run preview
(`_predict_pull_outcome`) may slightly over-count merges by comparison
since it can't run the merge without downloading the blob — documented
inline.

### Fixed

- **Phantom merge reports on `mm pull`.** `.jsonl` and `MEMORY.md`
  files where local was a strict superset of remote no longer report
  as `merged`. The merge result is compared to local bytes; when
  equal, the write is skipped and the outcome is `unchanged`. Three
  regression tests pinned in
  `tests/test_pull_helpers.py::TestApplyMerge`: a JSONL no-op (with
  `atomic_write_bytes` monkeypatched to a sentinel that asserts on
  call), a `MEMORY.md` no-op, and a counter-test that real merges
  still write.

## [0.11.16] - 2026-05-01

**Scrub real email fixtures from public test suite + sync docs to
v0.11.13 default.** v0.11.10's trust-rooted-author work added real
personal/work email addresses (`kb@wardbitz.com`, `kb@cnyfeeds.com`)
as test fixtures in `tests/test_retro_fleet_aggregator.py`. The
personal address was already exposed via legacy commit author
metadata, but the work address had never appeared in the public repo
until that PR. Replaced all 26 occurrences with `example.com` /
`example.org` equivalents that preserve the test's two-distinct-
identities semantics. Also normalized 3 occurrences of
`kb@personal.com` (real domain, not the user's) to
`kb-personal@example.com` for fixture consistency. CLAUDE.md and
README.md were missing the v0.11.13 default exclude
`analytics/.last-sync-*` (gstack per-machine analytics cursor files);
both now match `src/mind_meld/config.py:DEFAULT_SOURCES`.

### Fixed

- **Test fixtures use clearly-fake email domains.** All 29 real-domain
  email occurrences in `tests/test_retro_fleet_aggregator.py` now use
  `@example.com` / `@example.org` per RFC 2606. The 71 retro-fleet
  aggregator tests still pass — replacements are byte-equivalent at
  the assertion layer.
- **`exclude_patterns` doc drift.** `CLAUDE.md` (`## exclude_patterns`
  section) and `README.md` (gstack defaults bullet) now list
  `analytics/.last-sync-*` alongside the v0.9.3-shipped excludes. The
  doc bump retroactively pulls v0.11.13's addition into the prose
  surface that explains gstack's default-source contract.

## [0.11.15] - 2026-05-01

**Shorten long repo URLs in retro-fleet output.** `retro-fleet`'s "Top
repos" section now compresses long enterprise-style URLs (host with 3+
path segments and over 60 chars) to `<host>/[...]/<last-segment>` so
UUID-laden repository identifiers stop bloating the rendered retro and
leaking instance/tenant detail into output that may be shared publicly.

### Changed

- **`format_retro` Top repos rendering applies `_shorten_repo_url`** to
  each canonical URL before display. The canonical URL itself stays the
  dedup key in `repos_by_count` — only the rendered markdown changes.
- **GitHub / Bitbucket / basic GitLab URLs always pass through** unchanged
  regardless of length. The compression gate requires both length > 60
  chars AND 3+ path segments after the host, so canonical 2-path-segment
  shapes (`host/org/repo`) are exempt by construction. Pinned by
  `TestShortenRepoUrl::test_github_long_url_passthrough` and
  `test_bitbucket_long_url_passthrough`.

## [0.11.14] - 2026-05-01

**Token usage measurement in retro-fleet.** The "Claude Code activity"
section in `/retro-fleet` now answers the question kb actually asks at
retro time: how much did Claude Code consume this week, was it Sonnet or
Opus heavy, did the cache do its job, what would this have cost at API
list rates. Real numbers from this Mac's 30-day window: 12.4M input,
8.8B cache_read, 18.9M output, 98% cache hit ratio, ~$17,749 list-price
equivalent across Opus 4.7 / 4.6 / 4.5 / Sonnet 4.6 / Haiku 4.5.

### Added

- **Per-message token aggregation across parent + subagent jsonls.**
  `walk_jsonl_token_buckets` sums `message.usage.{input, cache_create,
  cache_read, output}` from every assistant message in
  `~/.claude/projects/<encoded>/*.jsonl` and
  `<session-uuid>/subagents/agent-*.jsonl`. Subagent jsonls
  contribute to the parent project's token totals (~50% of fleet
  usage on this Mac) but do NOT bump `sessions`/`total_kb`/
  `last_session_at` — those preserve parent-only semantics.
- **Day-bucketed token snapshot field on `SessionMetadata`.** New
  `tokens_by_day: dict[YYYY-MM-DD, {input, cache_create, cache_read,
  output, by_model}]` field, capped at 90 days per entry. Aggregator
  slices to the retro window — `/retro-fleet weekly` and
  `/retro-fleet quarterly` see distinct, honest totals from the same
  snapshot.
- **Per-jsonl token cache at `~/.config/mind-meld/session-tokens.json`.**
  flock-guarded R/M/W via the new `mind_meld.lockedjson` helper; size
  + mtime keyed; concurrent-append safety via re-stat after read;
  oversize line guard (16 MiB cap, warn-once breadcrumb); message-level
  dedup by `message.id` UUID for retries / compaction artifacts.
- **Inline cache warm at `mm init` and first interactive `mm push`.**
  ~3-second one-time walk, telegraphed via `mm: warming token cache
  (one-time, ~3s)...`. Autopush hooks skip the token walk when the
  cache is cold and emit `mm: notice: token cache not warm; run
  'mm push' to populate` instead. First push after a detected upgrade
  transition (via `upgrade.last_transition_seen()`) triggers an
  inline warm even on autopush so machines warm automatically after
  upgrade.
- **Token cache GC.** `mm gc` reaps cache entries whose underlying
  jsonl is gone OR whose most recent `by_day` key is more than 90
  days old. Bounded growth per machine (~125 KB / week / project).
- **Token-usage render in retro output.** Under "Claude Code
  activity": `Tokens this window: 12.4M in / 8.8B cache_read / 18.9M
  out`, `Cache hit ratio: 98%`, `Estimated cost: ~$17,749 (Opus 4.7
  $11,460, Opus 4.6 $6,261, ...)`, `Per-model: Haiku 4.5, Opus 4.5,
  Opus 4.6, Opus 4.7, Sonnet 4.6`, plus a section caveat: *Cost
  estimates do not account for subscription plan pricing.*
- **`mind_meld.lockedjson` — extracted single-file flock R/M/W
  primitive.** Three contention modes (`block` / `raise` / `warn`)
  with a shared retry budget. `upgrade.py` retrofitted to use it;
  `token_usage.py` uses it from day one. `devices-write.lock` stays
  ad-hoc — its multi-file lock-on-sibling shape doesn't fit cleanly.

### Changed

- **Mixed-fleet handling for token-aware peers.** Field-presence sniff
  in `aggregate_sessions`: any project with `sessions > 0` and no
  `tokens_by_day` flags the device into `pre_token_peers`. Surfaces
  in Notes as: `Tokens incomplete: N peer(s) on pre-v0.11.14 OR with
  cold token cache — upgrade and/or run mm push on those machines for
  accurate token totals.` Same shape as the existing v=1 sessions
  handling. No `EVENTS_SCHEMA_VERSION` bump — additive on v=2.

### Fixed

- Peer-controlled model strings now sanitized via `safety.safe_str`
  before render (terminal-escape + Rich-markup defang). Closes a
  trust-boundary gap in the markdown retro output where a corrupted
  peer jsonl could plant control characters in the LLM-consumed
  rendering.

## [0.11.13] - 2026-05-01

**No more `analytics/.last-sync-line` conflicts on every gstack pull.** The
file is a per-machine cursor (a single integer tracking each device's
progress through its own gstack analytics jsonl) and was syncing on every
push, producing a `.sync-conflict-*` file every time two machines pulled
from each other.

### Changed

- **`analytics/.last-sync-*` added to the gstack source's default
  `exclude_patterns`.** Single fnmatch glob covers both observed cursor
  files (`.last-sync-line`, `.last-sync-time`) and any future cursor in the
  same family without another mm-side migration. Same hazard class as the
  existing `config.yaml` / `projects/*/repo-mode.json` /
  `projects/*/land-deploy-confirmed` exclusions: per-machine state, churns
  on every pull, definitionally not meaningful to peers.
- New mm installs pick up the exclude automatically. Existing users with
  explicit `[[sync.sources]]` entries see the migration nudge via
  `mm status` and the interactive `mm pull` / `mm push` prompt; running
  `mm migrate-config` appends the missing glob.

## [0.11.12] - 2026-05-01

**Retro-fleet output polish.** Six pieces of user feedback on the v0.11.10
shape addressed in one pass: noise cut, signal kept, phantoms hidden.

### Changed

- **All notes consolidated into a single tail `## Notes` section.** Pre-fix
  asides were sprinkled through every section body (fleet-incomplete and
  pre-v2 banners under the header, cherry-pick note inside Code shipped,
  "counted separately" parenthetical inside Claude Code activity, parse-
  error breadcrumbs at the document tail). Post-fix every aside lives in
  one `## Notes` block at the end; the section is omitted entirely when
  there's nothing to surface.
- **Code shipped — Top repos render as a sub-bulleted list** instead of a
  comma-joined run-on line. With many active repos the prior format wrapped
  to 3+ display lines and lost legibility.
- **Code shipped — cherry-pick "informational" line dropped.** "N commit
  subject(s) appear under multiple SHAs (cherry-picks counted separately)"
  was unclear and not actionable; the commit count is honest at the
  fleet-canonical `(remote, sha)` key.
- **Claude Code activity slimmed to the one useful line.** Dropped the
  total-MB-of-session-content line, the "(N in ephemeral Conductor
  workspaces, counted separately)" parenthetical, and the Most-active
  project list — all three were noise the user explicitly called out.
  The ephemeral-session count survives as a one-line Notes entry when
  non-zero.
- **Eureka section removed.** It rendered "0 / No eureka moments captured"
  in practice while emitting 28+ skipped-record breadcrumbs from the
  gstack-side `eureka.jsonl` format. The dataclass, reader, aggregator,
  and the dependent skip-counter category were deleted.
- **Phantom-event filter at the aggregator.** Event-producing device IDs
  are now intersected with the registered fleet from `mm devices
  --format=json` so de-registered or test-leaked phantom IDs drop out of
  the rendered count rather than surfacing as a "33 machine(s) (3
  currently registered)" banner. Stale on-disk event files keep aging out
  via the existing 90-day TTL in `_gc_old_event_files`. Falls back to the
  raw set when `mm devices` fails (transient registry failures must not
  zero the retro).

### Added

- `FleetState.unregistered_event_devices` — count of phantom IDs filtered
  out, surfaced as a single Notes-section line so the user sees that disk
  cleanup is happening on its own.
- Regression pins for the new filter: events from unregistered IDs drop
  out, the unregistered count flows to the Notes section, and the filter
  falls back to the raw set when `mm devices` fails.

### Removed

- `EurekaAggregate`, `aggregate_eureka`, `_read_eureka`,
  `RetroData.eureka`, `RetroData.eureka_path`, `aggregate(eureka_path=...)`
  parameter, `SKIP_CATEGORY_EUREKA`, `GSTACK_RETROS_DIR` constant.
- `SessionsAggregate.total_kb` and `SessionsAggregate.most_active`
  (their renderings were dropped from `format_retro`).
- `GitAggregate.cherrypick_pairs`.
- The `TOP_N_PROJECTS` constant.

### Known follow-up

- Token-usage measurement under Claude Code activity. Adding it requires a
  v=3 sessions-snapshot schema bump (sum `message.usage` from each session
  jsonl into the snapshot) plus probably a per-jsonl sidecar cache to stay
  under the 250ms autopush wall-clock budget. Deferred.

## [0.11.11] - 2026-05-01

**Dev work can no longer poison your real macOS Keychain.** A test-fixture
passphrase (`pw123`) reached a real fleet's Keychain via a path that bypassed
conftest's `_isolate_keyring` fixture (most likely a non-pytest harness or a
manual init against a placeholder storage path), wedging `mm pull` until
the entry was manually overwritten.

### Changed

- **`crypto.store_passphrase_in_keyring` now short-circuits when
  `PYTEST_CURRENT_TEST` is set in the environment.** Belt-and-suspenders
  behind conftest's existing `keyring.set_password` stub: pytest sets
  `PYTEST_CURRENT_TEST` for every test phase and the variable is inherited
  by subprocesses, so any test-orchestrated path — in-process, subprocess,
  `python -c '...'` from a test, ad-hoc REPL session under pytest — can no
  longer reach the real Keychain regardless of which test layer remembers
  to stub. Real-CLI `mm init` is unaffected (the variable is not set in a
  user shell). Failure mode under the guard is loud and recoverable
  ("No keyring available" yellow warning, set `MINDMELD_PASSPHRASE` or
  re-run init in a clean env), versus the silent test-passphrase poisoning
  the guard prevents.
- `tests/test_crypto.py::TestStorePassphraseInKeyringExceptNarrow` now
  `monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)` in both
  pre-existing pins so they exercise the real-CLI write path past the new
  guard.

### Added

- `tests/test_crypto.py::TestStorePassphrasePytestGuard` — two regression
  pins: (1) guard fires under pytest, `keyring.set_password` is never
  reached even when monkey-patched to raise; (2) guard does NOT fire when
  `PYTEST_CURRENT_TEST` is absent, real-CLI write reaches keyring.

## [0.11.10] - 2026-05-01

**Retro-fleet now counts the work you actually did and stops being inflated
by test-leaked phantom devices.** Three independent fixes that were stacking
on each other to make retros look broken:

- Tests that drove `mm init` end-to-end via the typer runner were writing
  the v0.11.8 init backfill (a `git-snapshot` + `sessions-snapshot` row
  per init) to the user's real `~/.local/share/mind-meld/events/`. Each
  test run minted a fresh random device id, so 30+ phantom device files
  accumulated locally and showed up in retro as e.g. "Activity across
  33 of 3 known machines."
- `gather_author_emails()` only consulted `git config --global user.email`,
  so commits authored under any other configured identity (per-repo
  override like a dotfiles repo using a personal email; GitHub
  web-merge UI setting author to `<id>+<login>@users.noreply.github.com`)
  silently fell out of the filter. Real-world impact: a 7-day window
  on this repo went from "9 commits / +71 -61 LOC" to "48 commits /
  +35,009 / -9,335 LOC" after the fix.
- The header rendering said "N of M known machines" even when N > M,
  reading as a counting bug rather than the data inconsistency it was.

### Added

- **`_isolate_mm_events_path` autouse fixture (tests/conftest.py).**
  Mirrors the existing `_isolate_pullhistory` / `_isolate_devices_write_lock`
  fixtures. Patches `DEFAULT_SOURCES['mm-events'].path` to a per-test
  tmp directory via `monkeypatch.setitem` (mutates the existing dict in
  place so `from mind_meld.config import DEFAULT_SOURCES` consumers in
  cli.py see the patched path through their existing binding —
  `setattr` would have only updated `config`'s namespace and left
  `cli`'s reference stale).
- **`TestEventsDirIsolation` regression pin (tests/test_init_events_backfill.py).**
  Two tests: one asserts the fixture is active and the path is
  redirected; the other drives `mm init` end-to-end via the runner
  WITHOUT stubbing `_run_events_backfill` and verifies that no events
  file appears in the real `~/.local/share/mind-meld/events/` after.
- **Trust-rooted email gathering (aggregator.py).**
  `gather_author_emails()` now unions four sources, all of which are
  CONFIGURED identities on machines the user controls — never a
  `git log` walk. Walking commits would harvest collaborator emails
  on shared repos (their work sits in local history once you pull),
  silently inflating retros with their commits as yours. Trust-
  rooted scoping eliminates that class of false-positive entirely.

  - `git config --global user.email` (global identity).
  - `git config user.email` per discovered repo
    (`_per_repo_user_emails`) — captures per-repo overrides where
    the user has explicitly configured a different identity for a
    specific project (e.g., a dotfiles repo using a personal email
    where the global default is a work email).
  - `[retro].author_emails` in mm config.toml (manual override —
    canonical place to list identities you've used historically that
    aren't currently configured anywhere on this machine).
  - `<id>+<login>@users.noreply.github.com` derived from
    `gh api user` (`_gh_noreply_email`) — PR-merges via the GitHub
    web UI / `gh pr merge` set author to this form regardless of
    local git config. Uniquely yours (the `<id>+<login>` pair can't
    collide with a collaborator's noreply form), so including it in
    the trust set does NOT open the collaborator-leak hole that a
    broad `git log` walk would. Best-effort: missing/unauth `gh`,
    malformed JSON, or unexpected shape all return None and the
    function falls back without it.

  Trade-off: this set may *under*-count if you have identities in use
  on machines outside this one's reach (committing as
  `karl@personal` on the iMac but the MacBook only has `kb@work`
  configured, then running retro on the MacBook). Workaround: list
  every identity in `[retro].author_emails` on each machine. Future
  improvement: sync the trust set across the fleet via mm-events
  (deferred).
- **Wall-clock-bounded per-repo scan.** Total budget
  `_PER_REPO_SCAN_BUDGET_SECONDS = 5.0` and per-repo timeout
  `_PER_REPO_GIT_TIMEOUT_SECONDS = 2.0` keep the gather from
  becoming a multi-second wait on a slow filesystem. Budget
  exhaustion returns whatever was collected so far. Per-repo
  failures (rc != 0, timeout, missing binary) skip silently.
- **`TestGatherAuthorEmails` (11 tests, tests/test_retro_fleet_aggregator.py).**
  Pins per-repo override union, gh noreply auto-derive, all four
  gh-soft-fail modes (missing binary, unauth, malformed JSON,
  unexpected shape), no-repos fallback, per-repo failure tolerance,
  config-load failure fallback, and wall-clock budget enforcement.
  Critically includes
  `test_collaborator_email_in_shared_repo_history_NOT_included` which
  builds a real local git repo with a user commit and a synthetic
  collaborator commit, verifies the collaborator's email IS in the
  local `git log`, and asserts it is NOT in the trust set —
  load-bearing regression pin against any future drift toward a
  log-walking design. Plus
  `TestGitAggregationWithBroadenedFilter::test_noreply_commits_counted`
  end-to-end check.

### Changed

- **Header rendering when `n_in_events > m_known` (aggregator.py).**
  Renders "Activity from N machine(s) (M currently registered)" plus a
  "Fleet inconsistency: K device id(s) in events but not in
  `mm devices`" breadcrumb naming the delta and pointing the user at
  the cleanup path. The pre-v0.11.9 "N of M known machines" wording is
  preserved when `n_in_events <= m_known` (where it reads accurately).
  Pinned by `TestFleetCount::test_more_events_than_known_renders_inconsistency`.

## [0.11.9] - 2026-05-01

**Test suite cleanup — no behavior changes, no user-visible impact.** Pruned
four duplicate tests, migrated two unique assertions into their behaviorally-
named neighbors, and renamed three "Track" test files (named after merged
refactor tracks) to behavior names so future contributors don't mistake them
for stale archaeology.

### Changed

- **`tests/test_track_1a.py` → `tests/test_silent_failure_contract.py`.** Pins
  the visible-failure contract for autopull/autopush (silent on missing config,
  loud on corrupt config, breadcrumb on lock-held, etc.).
- **`tests/test_track_1c.py` → `tests/test_pull_result.py`.** Pins
  `iter_source_diffs`, `PullResult` degradation counters, autopull `degraded`
  breadcrumb, GC malformed-sha safety, and autopull's typer.Exit handling for
  fleet-version refusals.
- **`tests/test_track_2a.py` → `tests/test_pull_helpers.py`.** Unit tests for
  the helpers underneath `_pull_core` / `_apply_incoming_file`.
- **`PASSPHRASE` constant in `tests/conftest.py`** renamed from
  `"track-1a-test-passphrase"` to `"shared-cli-test-passphrase"` (no test
  asserted the literal value).

### Removed

- **4 duplicate tests:** `test_register_device_does_not_seed_last_seen`
  (covered by `test_devices.py::TestRegisterDeviceCreateOnly`),
  `TestApplyConflict::test_happy_path_keeps_local_writes_remote_to_sidecar`
  + `TestApplyConflict::test_empty_device_id_returns_failed` (covered through
  the dispatcher in `test_conflict_copy.py::TestApplyIncomingFile`), and
  `TestEmptyOutcomes::test_has_all_outcome_keys` (trivial structural check
  exhaustively covered elsewhere).

### Added

- **Stderr emptiness assertions** in
  `test_integration.py::TestAutoCommands::test_autopull_no_config_exits_silently`
  + autopush twin (migrated from the deleted track_1a duplicate; integration
  versions previously asserted `result.output == ""` only, missing stderr).
- **Explicit JSONL set-membership assertions** in
  `test_conflict_copy.py::TestApplyIncomingFile::test_merge_wins_for_jsonl_even_when_local_newer`
  (migrated from the deleted track_2a `_apply_merge` happy-path test; the
  weaker substring check it had before would let a regression silently drop
  one side's overlap rows).

## [0.11.8] - 2026-04-30

**Fresh-install machines see their past 30 days of activity in retro-fleet
immediately, instead of an empty retro until the first push.** `mm init` now
runs an event backfill at the end of setup that captures 30 days of git
commits + a full sessions inventory and writes them to the local events log.
The first real push uploads them, and other peers pick them up on the next
pull. Closes the gap between init and first push, where retro-fleet would
otherwise show zero activity for the new machine.

### Added

- **`_run_events_backfill` helper in `cli.py`.** Mirrors `_run_events_tail`
  but writes only `git-snapshot` + `sessions-snapshot` rows — no `mm-push`
  event. Push counts in retro stay honest (an init-counted-as-push would
  inflate the per-window count by 1 on every fresh machine), and the
  cursor stays at "no prior push" so the first real push re-walks the
  same 30-day window. Aggregator dedups via
  `(canonical_remote_url, sha)`, so the trade-off is a one-time ~500ms
  duplicate `git log` walk on the first push, paid once per machine.
- **Init wiring at end of `init`.** Runs after `_register_and_save` and
  `_ensure_retro_skill_link`, resolves sources via `get_sources(config)`
  so `mm-events` is bootstrapped before walk runs. Forensic-only on
  failure: a single `mm: notice:` to stderr, init proceeds.
- **Six tests in `tests/test_init_events_backfill.py`.** Pin the helper
  shape (no `mm-push` row, `mm-events`-resolved gate, failure
  breadcrumb, claude-source-optional, 30-day since window) plus one
  init-wiring smoke test that confirms `init` actually calls the helper.

### Changed

- **`events.py:14-17` docstring corrected.** The pre-Track-7B comment
  about events being "local-only until the next push" was stale —
  Track 7B wired the call at the HEAD of `_push_core` (BEFORE
  `build_manifest_v2`), so events upload same-push. Docstring now
  describes the actual semantics and references the init-time backfill.

## [0.11.7] - 2026-04-30

**Retro-fleet aggregator no longer conflates foreign-file parse errors
with mm event corruption; tests no longer pollute the user's real
pull-history.** Two diagnostic-quality fixes for the retro-fleet skill.
The aggregator's tail breadcrumb used to render `N event(s) skipped due
to parse errors` regardless of which file failed — pointing the user at
mm-owned data when the actual culprit was a malformed
`~/.gstack/analytics/eureka.jsonl`. The breadcrumb now names the
affected file and labels the cause (mm vs gstack). Independently, an
autouse pytest fixture redirects `pullhistory.HISTORY_DIR` to a tmp
path so test fixture device names (`dev-a`, `peerA`, etc.) stop leaking
into the real `~/.config/mind-meld/pull-history.jsonl`.

### Added

- **`_iter_json_stream` tolerant reader (aggregator.py).** Walks files
  with `json.JSONDecoder.raw_decode` so multi-line pretty-printed JSON
  parses cleanly alongside canonical JSONL. Used for foreign-format
  gstack files (`skill-usage.jsonl`, `eureka.jsonl`) where the on-disk
  format isn't fully under mm's control. Bad chunks advance to the next
  newline and recovery continues — one broken record doesn't poison the
  rest of the file.
- **`JSON_STREAM_MAX_BYTES = 50 MB` cap on the foreign reader.** A
  runaway gstack file beyond the cap surfaces as a skip rather than
  slurping into memory. Mirrors mm's default `max_file_size`.
- **`SKIP_CATEGORY_*` constants.** Three categories — `events`,
  `skill_usage`, `eureka` — keyed into the per-source skip counter so
  call sites stay consistent and the breadcrumb maps each category to a
  specific message naming the affected file.
- **`RetroData.skipped_per_source: dict[str, int]`,
  `skill_usage_path`, `eureka_path` fields.** Per-category breakdown
  exposes the skip distribution; the path fields thread the actual
  configured paths through to `format_retro` so breadcrumbs name the
  real file even when callers use custom analytics paths.
- **`_isolate_pullhistory` autouse fixture (tests/conftest.py).**
  Redirects `pullhistory.HISTORY_DIR` to a per-test tmp path so
  `pullhistory.append` calls during CLI-driven tests stop polluting the
  user's real history. Mirrors the existing `_isolate_devices_write_lock`
  pattern. Per-test override via explicit monkeypatch still works.

### Changed

- **`format_retro` renders one breadcrumb per affected source.** Was a
  single `N event(s) skipped due to parse errors` line regardless of
  source. Now renders up to three lines, each naming the actual file
  and labeling the cause (`gstack file format issue, not mm` for
  foreign files). A backward-compat fallback keeps the contract for
  manually-constructed `RetroData` with `skipped_lines` set but no
  per-source breakdown.
- **`_read_skill_usage` / `_read_eureka` route through the tolerant
  reader.** `_read_events` keeps the strict JSONL reader because
  `events.py` writes pure single-line JSONL.
- **`RetroData.skipped_lines` is now the sum of `skipped_per_source`.**
  Existing tests that assert on the total continue to pass; new tests
  drill into per-category counts to verify discriminated behavior.

## [0.11.6] - 2026-04-30

**`mm install-skills` — explicit user-facing command for the retro-fleet
Claude Code skill symlink, plus a drift-aware steady-state gate.** The
push-time self-heal at `_ensure_retro_skill_link()` already auto-installs
the symlink and tracks the wheel via `pipx upgrade`, but two gaps showed
up in the field: (1) no discoverable way to install on demand (e.g. on
a fresh machine before the first push, or after manual cleanup of an
old workspace path), and (2) the 24h success-marker silently suppressed
self-heal when the link drifted out of sync (user removed it by hand,
pipx venv rebuild at a different path) — the marker promised "link is
good" without verifying.

### Added

- **`mm install-skills`** — force-runs the installer ignoring the 24h
  TTL gate. Reports `Installed: <target> -> <skill_src>` on success;
  exits 1 with an actionable error when the target is a non-mm file or
  symlink, or when `~/.claude/skills` doesn't exist (no Claude Code
  installed).

### Changed

- **`_skill_link_check_due()` verifies link state before short-circuiting
  on a fresh marker.** Returns True when the link is missing, dangling,
  or pointing somewhere other than our resolved skill source — even if
  the success marker is fresh. Adds one `lstat` + one `readlink` +
  `importlib.resources.files()` call to the steady-state push path
  (negligible). Any I/O or resolver error in the drift check fails open
  (returns True) so the installer runs and emits its own forensic
  notice.

### Tests

- `tests/test_skill_link.py::TestSkillLinkCheckDue`: 4 new cases covering
  fresh-marker-but-link-missing (REGRESSION pin for the post-cleanup
  recovery bug), fresh-marker-but-link-dangling, fresh-marker-but-link-
  wrong-target, and resolver-failure fail-open. Existing
  `test_fresh_marker_means_not_due` updated to also set up a correct
  symlink — the steady-state gate now requires both conditions.
- `tests/test_skill_link.py::TestInstallSkillsCommand`: 6 new cases for
  the new CLI command — creates-when-absent, idempotent-on-correct,
  self-heals-dangling, errors-on-conflict, errors-when-no-claude-code,
  and bypasses-TTL-gate.

## [0.11.5] - 2026-04-30

**Pull-time conflict sidecars no longer accumulate across pulls; conflict
prompt shows the consequential drop count up-front.** Two fixes wrapped
together. The bug: every `mm pull` where a peer's bytes still differed
from local stamped a fresh timestamped `.sync-conflict-*` sidecar,
even when an earlier sidecar from that same peer already carried the
same bytes. Users hitting `mm resolve` saw the same canonical filename
in three consecutive prompts and worried `(m)erge` was duplicating
content. The dedup keeps at most one current-state sidecar per peer.
The UX fix: `(l)ocal` and `(r)emote` lines now end with `(drops N peer
line(s))` / `(drops N of your line(s))` so the consequence is visible
inline without translating the diff to "what do I lose."

### Added

- **`_existing_post_inversion_sidecars_from_peer(canonical, device_short)`.**
  Lists post-inversion `.sync-conflict-*` siblings of a canonical from
  one peer device. Skips `v0-` pre-inversion sidecars (those hold local
  bytes from a pre-v0.9.2 conflict and must never be reaped by the
  apply path). Used by `_apply_conflict` for per-peer dedup.
- **`(drops N ...)` annotations on `(l)ocal` / `(r)emote` in
  `conflictdiff.render_prompt`.** New `local_only_lines` /
  `remote_only_lines` kwargs append a dim-styled drop count to each
  destructive choice. `(l)ocal` end-state = local bytes (drops the
  peer-only lines); `(r)emote` end-state = remote bytes (drops the
  user's local-only lines). Suppressed when the diff is empty (binary
  content) — annotating "drops 0 lines" without a real comparison
  would be a false reassurance.

### Changed

- **`_apply_conflict` deduplicates by peer before writing.** Before
  stamping a fresh sidecar, scans existing post-inversion sidecars from
  the same peer for the same canonical. If one already holds bytes
  equal to `plain_data`: skip the write (idempotent — outcome stays
  `"conflicted"`). If existing sidecars hold stale bytes (peer pushed
  something newer): unlink them, then write the current sidecar. Fleet
  user sees one current-state sidecar per peer, never a per-pull
  timeline. Empty/`None` `remote_device_id` falls through to the
  existing ValueError branch.
- **Divergence summary copy now uses semantic local/remote labels.** Was
  `M removed-or-replaced lines on local side; N added-or-replaced on
  remote side` — sloppy in `pre_inversion` mode where canonical = remote
  and the raw `m`/`n` from `count_divergent_lines` swap meaning. New
  copy: `N unique line(s) of yours; M unique line(s) from peer; K total
  diff lines.` Mode-correct in both pre- and post-inversion. Both
  prompt sites updated.

### Tests

- `tests/test_conflictdiff.py::TestRenderPrompt`: 6 new cases covering
  drop-count annotations (default omission, post/pre inversion mappings,
  pluralization, zero-zero, asymmetric kwargs).
- `tests/test_conflict_copy.py::TestApplyIncomingFile`: 3 new cases —
  `test_pull_replaces_stale_sidecar_when_peer_pushes_new_bytes`,
  `test_dedup_does_not_collapse_sidecars_from_different_peers`,
  `test_dedup_does_not_reap_pre_inversion_sidecar_from_same_peer`.
  Existing `test_pull_is_idempotent_after_conflict` rewritten — pre-fix
  asserted `len == 2` (documenting the bug); now asserts `len == 1`
  with peer's current bytes.

## [0.11.4] - 2026-04-30

**`(m)erge` option in conflict-resolution prompt — LCS-as-synthetic-base
3-way merge for the trivial-additive case.** Most cross-fleet conflict
files (memory entry markdown, prose docs, append-mostly notes) end up as
divergence-pairs where one side is a strict superset of the other or
the additions land in non-overlapping regions. Today's prompt forces a
manual `(l)ocal` / `(r)emote` / `(s)kip` choice on every one. The new
`(m)erge` option offers a user-confirmed 3-way merge using
``LCS(local, remote)`` as a synthetic ancestor — additive edits on
either side land cleanly; only same-region replace edits emit
`<<<<<<<` markers and stay manual. Default key flips from `s` to `m`
when the merged result is clean (zero markers); user just hits Enter.

### Added

- **`mind_meld.merge.lcs_merge(local_bytes, remote_bytes) -> (bytes, int)`.**
  Pure-Python 3-way merge using `difflib.SequenceMatcher.get_opcodes()`.
  ``equal`` runs are kept; one-sided ``insert`` / ``delete`` are
  treated as additive (lossless union); ``replace`` runs become
  git-style conflict markers. Returns ``conflict_count = -1`` for
  binary input (NUL byte present) so callers suppress the (m) option.
  Trailing newlines preserved when either input had one.
- **`(m)erge` option in `_resolve_interactive_loop` and
  `_prompt_conflict_choice`.** Both prompt sites attempt
  ``lcs_merge`` before showing the prompt; ``conflictdiff.render_prompt``
  takes new ``merge_available`` and ``merge_conflicts`` keyword args
  that control whether the option line renders and whether the default
  flips to (m). On (m): canonical receives merged bytes via
  ``atomic_write_bytes``; sidecar is best-effort unlinked (failure
  surfaces as `mm: warning:` and falls back to the existing 30-day
  `mm gc --conflicts` reaper). Mode-symmetric across pre-inversion
  (`v0-`) and post-inversion sidecars.
- **`merged-via-lcs` `ApplyOutcome`.** Distinct from `merged` (which
  covers the `.jsonl` / `MEMORY.md` line-union path) so `mm log
  --action merged-via-lcs` post-dogfood gives an honest count of how
  often the LCS path fires. Schema-additive in `pullhistory.jsonl`.

### Notes

- LCS-as-synthetic-base sidesteps the missing-stored-base problem
  (the deferred Future TODO "Three-way merge base") with the trick:
  lines both sides agree on form the LCS = base; one-sided edits are
  lossless additive; same-region edits show as conflict markers.
  Conservative enough to ship behind an explicit user confirmation.
- Pure Python: no subprocess to `git`, no new PyPI dep. If dogfood
  reveals misalignment on pathological prose with lots of repeated
  short lines, the implementation can swap to subprocess-`git
  merge-file` behind the same `lcs_merge` signature.

## [0.11.3] - 2026-04-29

**Group 8 hotfixes — retro-fleet skill correctness + ergonomic notice.**
Two fixes from the v0.11.0 adversarial review against the retro-fleet
aggregator. The data-loss fix is the load-bearing one: two configured
`type: claude` source roots that share an encoded project name silently
overwrote each other in the aggregator's `latest` dict. The notice is
the smaller piece — power users with a custom `mm-events` `path:` got
silently-empty retros unless they also set `MM_EVENTS_DIR`.

### Fixed

- **Sessions dedup key now includes `source_root` (Group 8 hotfix #4).**
  `aggregate_sessions` keys snapshots by `(device, source_root,
  claude_dir)` instead of `(device, claude_dir)`. `walk_session_metadata`
  threads the parent `claude_dir` (the source-root path) through to
  `_scan_one_project`, which writes it to each emitted `SessionMetadata`
  as a new `source_root: str` field. Pre-fix, two `type: claude` source
  roots that both contained a project encoded as e.g.
  `-Users-kb-Documents-foo` silently overwrote each other; now they are
  preserved as distinct entries and their session counts sum correctly.
  `SessionMetadata` is `TypedDict, total=False` so the schema change is
  additive — no v=3 bump.
- **Coalesce pass merges legacy keys during the rollout window.**
  Records on synced storage from pre-fix builds have no `source_root`
  field (treated as `""`); records from post-fix builds carry the
  populated path. During the rollout window both shapes coexist for the
  same project. The aggregator now drops `(device, "", claude_dir)`
  keys when `(device, "<root>", claude_dir)` exists for the same device
  AND the populated sibling is at least as fresh as the legacy record
  (codex adversarial caught the freshness gap — without the timestamp
  guard, a downgrade or interleaved-fleet push could erase newer legacy
  data with a stale populated sibling that itself gets window-filtered
  out, returning zero sessions for an active project). Distinct
  populated `source_root` values are preserved (the legitimate
  two-source-root case the fix is for).
- **Custom-path notice respects `disabled_sources` (codex adversarial
  catch).** When `[sync].disabled_sources` contains `mm-events`, the
  user has opted out per-machine; `_read_mm_events_config_path` now
  returns None so the notice stays silent. Without this, users who
  disabled `mm-events` saw a recurring nudge to set `MM_EVENTS_DIR` for
  a source the CLI no longer writes — fails the visible-failure
  contract.
- **`mm: notice:` for custom `mm-events` path without env override
  (Group 8 hotfix #1).** When the retro-fleet skill runs from the CLI
  with `MM_EVENTS_DIR` unset and the user has an `mm-events` source
  configured at a non-default path, the aggregator now writes one line
  to stderr pointing at the env override. Notice fires only from
  `main()` — library callers of `aggregate()` never see it. Tolerant
  of missing / malformed config (no notice, no crash).

### Added

- **5 new tests pinning the custom-path notice gating logic**
  (`TestCustomPathNotice` in `tests/test_retro_fleet_aggregator.py`).
- **4 new tests pinning the source_root dedup + coalesce invariants**
  (`TestSessionsSourceRoot`), including the REGRESSION pin
  `test_two_distinct_source_roots_kept_separate`.
- **`test_source_root_field_emitted`** in
  `tests/test_events.py::TestWalkSessionMetadata`.

### Deferred (with concrete re-open triggers)

- **Aggregator memory streaming** — observation-bar; revisit on first
  user-visible memory pressure signal (peer reporting OOM or
  `events_dir` cumulative size > ~200MB).
- **Events tail budget tuning** — speculative; needs benchmark on a
  heavy-fleet machine (200+ Claude project dirs) before deciding.
  Re-open when a real user hits `mm: notice: events tail budget
  exceeded` repeatedly.
- **Pre-v0.11.0 breadcrumb persistence cleanup** — naturally
  self-correcting within the 7-30 day retro window.

## [0.11.2] - 2026-04-29

**Group 7 hotfix — `mm: warning:` no longer spams on every read-only command.**
Users with a chmod-restricted `~/.local/share/` (or any environment where
`mkdir` for the `mm-events` source dir fails) saw the
`mm: warning: could not create mm-events source dir ...` line on every
invocation of `mm sources` / `mm status` / `mm conflicts` / `mm diff` /
`mm log` (~11 internal call sites of `get_sources()`). The warning now
fires once per process per failing path; subsequent calls in the same
process short-circuit silently.

### Fixed

- **Bootstrap warning warns once per process per path.**
  `_bootstrap_mm_events_path` now consults a module-level
  `_BOOTSTRAP_WARNED_PATHS: set[str]` and skips both the `mkdir` retry
  and the `mm: warning:` emit on subsequent calls for any path that
  already failed in the current process. First failure still surfaces
  the breadcrumb (visible-failure contract preserved — monitoring
  catches the wedge); per-path keying preserves the contract for the
  unlikely case of two failing mm-internal source paths. Pinned by a
  new `test_bootstrap_warns_once_per_process` regression test that
  calls `get_sources()` 5× and asserts exactly 1 warning is emitted.
  Existing failure-path test now resets the cache via `monkeypatch`
  for ordering independence.

## [0.11.1] - 2026-04-29 — BREAKING (interactive prompt)

**Conflict resolution prompt UX overhaul.** The `mm resolve` and inline
`mm pull --conflict-mode prompt` interactive prompts were unreadable —
users could not tell which side was local vs remote, the option labels
referenced internal jargon (canonical/sidecar), and `(b)oth` looked like
a merge but was actually a skip. This release redesigns the prompt around
concrete file actions, color-coded LOCAL/REMOTE banners, peer-name
attribution, and a 3-number divergence summary.

### Added

- **Color LOCAL/REMOTE banners above the diff.** Each conflict prompt
  prints a red `LOCAL ▌ <filename>` and green `REMOTE ▌ <filename>
  (from <peer_name>)` banner so visual identification doesn't depend on
  parsing unified-diff `+`/`-` prefixes. Banners go through
  `safe_text(...)` so peer-controlled paths AND peer-controlled
  `device_name` strings can't smuggle ANSI/OSC/CSI/DCS escapes to the
  terminal.

- **Peer-name attribution.** The REMOTE banner resolves the
  conflict-filename's 8-char device prefix against the registered
  devices list and renders `(from <peer_name>)` when known. Falls back
  to `(unknown peer)` when no peer matches and `(ambiguous -- N peers
  match this prefix)` on collision (T4 cross-model finding).

- **Three-number divergence summary.** Pre-diff line shows
  `M removed-or-replaced lines on local side; N added-or-replaced on
  remote side; K total diff lines.` Honest about unified-diff semantics
  (a 1-line replacement is M=1, N=1, K=2 — not "two independent edits").

- **Init-time device-id collision regenerate.** `mm init` now scans
  existing peers for the new device's 8-char prefix and regenerates on
  collision (up to 5 retries, then `mm: warning:` and proceed). UUID4
  prefix collisions on 32 bits are extremely unlikely under healthy RNG
  but a cloned-from-snapshot peer or deterministic-RNG bug could collide
  reproducibly. Forward-defense plus a runtime fallback.

- **`src/mind_meld/conflictdiff.py`** module: leaf primitives
  `render_prompt`, `render_banner`, `count_divergent_lines`. Pure
  functions, easy to unit-test without CLI fixtures.

- **`src/mind_meld/safety.py`** module: extracted `safe_str`, `safe_text`,
  `strip_terminal_escapes` from cli.py so `conflictdiff.py` can import
  them without circular dependency. Re-exported from cli.py for
  backwards compat with any out-of-tree imports; tests now import
  directly from `mind_meld.safety`.

- **`devices.lookup_device_by_short_id(devices, short_id)`** pure helper
  + `manifest.parse_conflict_device_short(name)` parser used by both
  prompt sites to attribute the REMOTE side.

- **`devices.generate_unique_short_device_id(devices)`** for init-time
  collision prevention.

### Changed (BREAKING — interactive prompt only)

- **Default key flipped from `b` to `s`.** Pressing Enter at a conflict
  prompt now skips the conflict (leaves both files on disk) instead of
  defaulting to `(b)oth`. Same on-disk effect as before; the rename
  reflects what the option actually does. The pre-1.0 `b`/`both`
  letters are accepted as deprecation aliases with a one-time
  `mm: notice:` until 1.0 — no silent-data-loss risk in mapping them
  through (this is unlike v0.9.0's loud rejection of `c`/`f`, which
  encoded directional ambiguity).

- **Honest skip lifecycle copy.** The prompt now reads
  `(s)kip → leave both files on disk; run `mm resolve` later or delete
  manually` instead of "decide on the next pull" — pulling again does
  NOT re-prompt unless remote changes again, so the old wording was
  misleading (T2 cross-model finding).

- **Concrete-action option copy.** Each option now reads as the file
  action it performs, e.g. `(l)ocal → discard <conflict-name>, keep
  <canonical-name> as-is`. Canonical/sidecar terminology removed from
  user-facing output.

- **Banner sanitization extended to `device_name`.** `device_name` is
  set via `typer.prompt` at peer init, plaintext-synced via
  `devices/<id>.json`, and peer-controlled at the rendering machine.
  Banner rendering now strips terminal escapes from this input too —
  closes the same trust-boundary leak v0.10.1 closed for filenames
  (T6 cross-model finding).

- **Device-list cache hoisted at loop entry.** `_resolve_interactive_loop`
  now accepts the device list as a parameter, populated once by `mm
  resolve` from `list_devices(backend)`. Avoids N storage hits on a
  multi-conflict walk; iCloud cold-cache reads can otherwise stack to
  multi-second latency per conflict.

### Migration

Stale scripts piping `b\n` into `mm resolve` continue to work — they
trigger the deprecation alias and print `mm: notice: 'b' / 'both' now
means 'skip'; use 's' going forward (alias removed at 1.0).` to stderr.
On-disk effect is identical to the pre-v0.12 behavior. Update such
scripts to pipe `s\n` to silence the notice.

The pre-v0.9.0 letters `c` (keep canonical) and `f` (from-other) remain
loud-rejected with `mm: error:` and `Exit(1)`. Those encoded directional
ambiguity that the v0.9.2 inversion broke; a silent map-through would
risk data loss.

## [0.11.0] - 2026-04-28

**Group 8 / Track 8A: fleet-aware retro skill.** Adds a Claude Code
skill that stitches activity from every Mac in your mind-meld fleet
into one accurate, paste-ready markdown retro. Reads the synced
`mm-events` log (Group 7), dedups commits across machines via
canonical remote URL + sha, and renders a single-message retro you
can paste into iMessage or email.

### Added

- **`/retro-fleet` Claude Code skill.** New skill at
  `src/mind_meld/skills/retro_fleet/{SKILL.md,aggregator.py}`. Run it
  in Claude Code as `/retro-fleet 7d` (or `30d`, etc.) to get a
  fleet-wide retro that mirrors the gstack `/retro` shape but stitches
  activity across every Mac in your fleet. Output is paste-ready for
  iMessage / email / Slack — not a dashboard.

- **`mm devices --format=json` flag.** New JSON formatter alongside
  the existing Rich Table renderer. Stable schema: `[{device_id,
  device_name, last_seen, last_seen_version, is_self}, ...]`. Sorted
  alphabetically by `device_id` for cross-platform stability. Empty
  fleet returns `[]`. Used by `/retro-fleet` for the "M of N known
  devices" breadcrumb; usable by anyone who wants to script against
  device state.

- **`_ensure_retro_skill_link` symlink installer.** Drops the
  `~/.claude/skills/retro-fleet` symlink at `mm init` and self-heals
  on every push (24h-TTL gated, ~1 syscall in steady state). Five-
  branch state machine: target absent → create; correct symlink →
  no-op; dangling symlink → unlink + replace (covers `pipx reinstall`
  recovery); user's own file at the target → leave alone, notice
  once per day; `OSError` on creation → forensic-only breadcrumb,
  push continues.

### Changed

- **Sessions-snapshot schema bumped v=1 → v=2.** Pre-v0.11.0
  `walk_session_metadata` filtered jsonls by mtime, making each
  snapshot a delta. v=2 emits a full inventory so the retro
  aggregator can pick the latest snapshot per `(device, claude_dir)`
  and produce honest point-in-time numbers. Mixed-fleet handling:
  pre-v0.11.0 peers still emit v=1 rows; the aggregator surfaces them
  as "Sessions count incomplete: peer X is on pre-v0.11.0" rather
  than overcounting.

- **mm-push event sources field is now names-only.** The `sources`
  field on `mm-push` events is `list[str]` (source names only); the
  retro skill reads per-source content stats from the synced manifest
  at retro time. `MM_INTERNAL_SOURCE_NAMES` (today: `mm-events`) are
  filtered out so they don't show up as user-meaningful fleet activity.

### Fixed

- **Sessions retro window now scoped by `last_session_at`.** Pre-fix
  (caught in adversarial review), a 7d retro could include a 60d-old
  session as long as the device pushed today. Aggregator now filters
  projects whose `last_session_at` falls outside the requested window.
  Numbers stop silently lying.

- **Tolerant reader survives invalid UTF-8.** Aggregator opens JSONL
  files with `errors="replace"` so a corrupt-byte run in one event
  doesn't crash the whole retro. Lines that won't parse are counted
  in the visible-failure tail breadcrumb.

- **File-open failures bumped into `skipped_lines`.** Pre-fix, an
  unreadable events file (EACCES, transient EIO) was silently dropped
  from the retro with no breadcrumb. Now counted; user sees `Note: N
  events skipped` in the tail.

- **`mm devices` subprocess invokes via `python -m mind_meld.cli`.**
  Sidesteps PATH-hijacking and venv-version skew when the aggregator's
  parent venv has a different `mm` than the one earlier on PATH.

## [0.10.3] - 2026-04-28

**Track 7B: events tail wired into the push hot path.** Track 7A's
`events.py` now runs at the HEAD of `_push_core` on every push attempt,
writing per-device daily JSONL rows (git-snapshot, sessions-snapshot,
mm-push) to the synced `mm-events` source. `mm gc` now reaps event files
older than 90 days, and tombstone propagation handles fleet-wide
retention so peers drop their copies on pull. Patch bump because there's
still no consumer until Group 8's `retro-fleet` skill ships at v0.11.0;
this PR makes the data start flowing locally so the foundation is real
when the retro skill lands.

### Added

- **`_run_events_tail` at the head of `_push_core`** (`src/mind_meld/cli.py`).
  Runs after `_ensure_device_registered`'s self-heal and the no-sources
  guard, BEFORE `build_manifest_v2` — the event file lands on disk in
  time to be uploaded same push (no one-push lag). Gate is "mm-events
  resolved in `get_sources()`", which covers fresh / migrated / un-
  migrated configs uniformly without a migration prompt. Wall-clock
  budget is 250ms for `mm autopush` and 500ms for interactive `mm push`,
  enforced via `time.monotonic()` and plumbed into `walk_session_metadata`
  through a new `deadline_monotonic` keyword-only parameter so a
  pathological project (large jsonls, no `cwd` field anywhere) can't
  blow past the budget. Failures inside the tail are forensic-only —
  caught and breadcrumbed via `mm: notice:` to stderr; the push proceeds.
  `mm push --dry-run` is a no-op (preview contract).

- **`_gc_old_event_files` reaper at `mm gc`** (`src/mind_meld/cli.py`).
  Always-on (events retention is fleet policy, not opt-in). Reaps day
  files older than 90 days by parsing the `<device>-YYYY-MM-DD.jsonl`
  filename — NOT mtime, because iCloud restores can rewrite mtimes back
  to "now" while the filename date is intrinsic to the event-day boundary.
  Path resolves through `get_sources()` so user-customized mm-events
  paths are honored. Honors `--dry-run` and `--verbose`. Fleet retention
  fans out via tombstone propagation: this device unlinks → next push
  generates a tombstone → all peers drop their copy on pull, including
  offline peers when they come back online.

### Changed

- **`MmPushEvent.sources` schema simplified to `list[str]`** (names only).
  Codex C2 caught that `iter_source_diffs(skip_unchanged=True)` drops
  unchanged sources from the diff loop, breaking per-source counts on
  the no-content push path. Group 8's retro skill enumerates per-source
  content stats from the synced manifest at retro time, not from the
  event row.

- **`make_mm_push_event` filters `MM_INTERNAL_SOURCE_NAMES`** from the
  sources list. `mm-events` is mm-owned infrastructure, not user-
  meaningful fleet activity, so it never appears in the retro skill's
  source enumeration.

### Tests

- 28 new tests across `tests/test_events.py` (+7), `tests/test_integration.py`
  (`TestTrack7BEventsTail`, +9), and the new `tests/test_gc_events.py`
  (+12). Pins the four load-bearing invariants (head-position single-call
  -site, dry_run no-op, mm-events resolved gate, wall-clock budget),
  multi-claude aggregation into one sessions-snapshot row, the IRON RULE
  (events tail fires on no-content pushes), reap-by-filename-date with
  misleading mtime, and reap → next push generates tombstone. Total:
  1052 pass, 0 fail.

## [0.10.2] - 2026-04-28

**Track 7A: events.py foundation for fleet-aware retro.** Internal module
that captures per-push event metadata (git activity, session metadata, mm
sync activity) to a per-device daily JSONL file. Nothing user-visible
until Track 7B wires `_push_core` and Group 8 ships the `retro-fleet`
skill (both targeting v0.11.0). Foundation lands now to unblock parallel
work; fleet stays on v0.10.1 behavior until v0.11.0 is tagged.

### Added

- **`src/mind_meld/events.py`** — six functions plus a TypedDict v=1
  schema for the synced event log. `canonicalize_remote_url` strips
  credentials, userinfo, query auth, and fragments so tokens never
  reach iCloud-synced JSONL. `discover_git_roots` runs a multi-prober
  registry (gstack `repo-mode.json`, claude `cwd` field from session
  jsonls, manual `[retro].repo_roots`) and filters via
  `git rev-parse --show-toplevel` so Conductor worktrees (where `.git`
  is a file) aren't silently excluded. `walk_git_projects` enforces a
  hard wall-time budget via `as_completed(timeout=...)`, with per-repo
  timeout `max(200, (budget * 8) // repos)` capped at 2000ms.
  `walk_session_metadata` does an `os.scandir` 2-level walk and tags
  Conductor workspaces as ephemeral by path-string match (not existence
  check). `last_push_ts` derives the cursor from the events log itself
  by reverse-scanning up to 30 daily files for a `mm-push` row,
  defaulting to `now - 30d` on first run. `write_push_event` appends
  N events under one flock window; the contract requires the mm-push
  event LAST so a partial write doesn't advance the cursor.

### Changed

- **Extracted `fsutil.flock_append_jsonl(path, lines, *, mode, on_locked)`**
  as the single source of truth for the flock-protected JSONL append
  pattern. `pullhistory._append_payload` now routes through it with
  rotation passed in as an `on_locked` closure. Future flock bugs get
  fixed in one place.

### Tests

- 67 new tests across `tests/test_events.py` (58) and
  `tests/test_fsutil.py` (9). 38 existing pullhistory tests still pass
  unchanged after the retrofit. Total: 1024 pass, 0 fail.

## [0.10.1] - 2026-04-27

**Group 7 preflight: hygiene + security hardening before fleet-retro
foundation.** Eight cleanup items spanning peer-controlled trust boundary,
filesystem-identity dedup, device-write concurrency safety, and the
mm-events default source needed by the upcoming retro-fleet skill
(v0.11.0). All additive; no behavior changes for users who don't run the
new code paths.

### Security

- **Peer-controlled string sanitization at every render site.** A peer can
  put Rich markup or terminal escape sequences (CSI clear-screen, OSC 52
  base64 clipboard write, OSC title spoof, DCS, C1) in any synced filename
  OR file body. Without sanitization those bytes reached your terminal
  during `mm pull`/`mm conflicts`/`mm resolve`/`mm devices`/`mm status`
  and silently changed your clipboard, cleared your screen, or hid output.
  New `safe_str()` (escape strip + Rich markup escape) wraps every
  peer-controlled interpolation; new `safe_text()` strips escapes from
  diff content lines before wrapping in Rich `Text()`. Sweep covers ~30
  print sites including the `mm devices` table, `mm status` peer listings,
  conflict-prediction messages, and pull/merge feedback.

- **Pull-time case-collision detection on case-insensitive filesystems.**
  When a Linux peer legitimately has `Projects/x.md` AND `projects/x.md`,
  a macOS APFS puller can only represent one. The other would silently
  overwrite the first via inode aliasing. New non-invasive case-detection
  (swapcase + samefile probe, no writes) buckets peer-manifest paths by
  casefold; collision clusters emit `mm: warning:` per cluster, drop
  all-but-lex-first from each peer manifest. Manifest keys are NOT
  case-normalized — only consumer-side WRITE skipping. Cross-platform
  peers retain their distinct casing in the manifest.

### Fixed

- **`register_device` is now create-only.** Pre-fix, an iCloud `.icloud`
  placeholder TOCTOU could trick `_ensure_device_registered`'s
  `backend.exists()` check into running `register_device` against an
  entry that already existed, silently bumping the `registered:` first-
  registration timestamp on every self-heal. Now uses
  `LocalBackend.put_exclusive` (atomic `os.link` with `EEXIST` detection)
  so the create-only invariant holds at the filesystem layer regardless
  of placeholder state. Original `registered:` timestamps are preserved
  across re-registration.

- **`update_last_seen` serializes via `fcntl.flock`.** Concurrent autopush
  + interactive push could race on the read-modify-write of
  `devices/<id>.json`. Today's deterministic fields don't lose data, but
  any future non-deterministic field would. New `_devices_write_lock()`
  context manager (LOCK_EX | LOCK_NB with brief retry budget, degrade
  with `mm: warning:` stderr breadcrumb on contention) wraps the RMW.
  Lock file at `~/.config/mind-meld/devices-write.lock`.

- **`walk_generic_source` filesystem-identity dedup.** When custom config
  put an `include_files` entry inside an `include_dirs` directory (e.g.
  `include_files: ["projects/notes.md"]` AND `include_dirs: ["projects"]`),
  the same on-disk file got hashed twice. On case-insensitive volumes
  with case-mismatched config (`["projects"]` + `["Projects/notes.md"]`)
  it produced two distinct manifest entries for one inode — a real
  correctness bug. New dedup keys on `(st_dev, st_ino)`. Also sorts
  `collected_paths` lex before dedup so hardlink/symlink overlap picks
  the same rel-key on every machine (no phantom add/delete fleet churn).

- **`_find_conflict_files` filesystem-identity dedup.** Same shape change
  as `walk_generic_source` — extends the existing tuple-key dedup
  (Track 5D) to `(src_name, st_dev, st_ino)` so case-mismatched config
  on APFS produces single conflict-table rows.

### Added

- **`mm-events` default source + bootstrap.** New mm-internal source at
  `~/.local/share/mind-meld/events/` (mode 0o700) for the per-device
  daily event log Group 8's `retro-fleet` skill will read. Auto-included
  at `mm init` (mm-internal, no prompt — disable per-machine via
  `mm disable-source mm-events` if not wanted). `get_sources()`
  bootstraps the directory on first call so the source isn't inert
  between Group 7 and Group 8 ship. mkdir failures emit `mm: warning:`
  to stderr per the visible-failure contract.

- **`src/mind_meld/skills/` placeholder subpackage.** Empty package
  (`__init__.py` + `.gitkeep`) that ships in the wheel via existing
  `packages = ["src/mind_meld"]` so Group 8's `retro-fleet/SKILL.md`
  symlink installer can find the resources via
  `importlib.resources.files("mind_meld") / "skills"`.

### Changed

- **`mm init` no longer prompts for mm-internal sources.** New
  `MM_INTERNAL_SOURCE_NAMES` frozenset short-circuits prompts for
  mm-owned infrastructure (today: `mm-events`). Init guard refuses on
  zero user-facing sources (mm-events doesn't count). Per-machine
  opt-out remains via `mm disable-source`.

## [0.10.0] - 2026-04-25

**Per-machine source toggle.** New `[sync].disabled_sources` field plus
three CLI commands (`mm enable-source`, `mm disable-source`,
`mm reconfigure-sources`) for opting individual sync sources in or out on
a per-device basis. `config.toml` is per-machine (never synced), so the
toggle naturally lives there. Forward-looking groundwork for codex (and
future) sources to ship as opt-in additions to `DEFAULT_SOURCES` without
auto-enrolling existing users.

Purely additive; no breaking changes. Upgraders keep their existing
behavior — auto-detection of `~/.gstack` still fires, no retroactive
prompts.

### Added

- **`mm disable-source <name> [--force]`** — adds the name to
  `[sync].disabled_sources`, removing the source from `get_sources()`'s
  resolution. Strict by default: unknown names error with a closest-match
  hint (`gstck` → suggests `gstack`). `--force` accepts unknown names so
  you can pre-disable a source that hasn't shipped yet (`mm disable-source
  codex --force`). Per-machine: only this device is affected.

- **`mm enable-source <name> [--force]`** — removes the name from
  `disabled_sources`. If the name is in `DEFAULT_SOURCES` but absent from
  the user's `[[sync.sources]]` (e.g. a freshly-shipped codex), appends
  the default config so the source actually starts syncing.

- **`mm reconfigure-sources`** — re-runs the picker against the current
  config + new defaults. Use after `mm` ships a new source to walk
  through enable/disable for every known one. Preserves user
  customizations on existing `[[sync.sources]]` entries (`include_dirs`,
  `exclude_patterns`). Atomic: Ctrl-C aborts without writing.

- **`mm sources`** now lists ALL configured sources (not just resolved)
  with a new `Enabled` column. Disabled rows are dimmed so the toggle
  state is obvious at a glance.

- **`mm status`** surfaces two new breadcrumbs:
  - `Disabled sources (this device): X, Y` when the list is non-empty,
    so future-you doesn't forget gstack is off and re-debug "why isn't
    this syncing".
  - `New source available: X. Run mm enable-source X to sync.` when
    `DEFAULT_SOURCES` grows post-upgrade. One-shot via `seen-sources.json`
    — once the user enables / disables / reconfigures, the hint stops.

- **`src/mind_meld/seen_sources.py`** — new module tracking per-machine
  acknowledgment of source names. `read(initial)` lazy-initializes under
  `fcntl.flock` on first call, seeded with the names of currently-resolved
  sources. Without this seed, every upgrader would see spurious "New
  source: claude!" / "New source: gstack!" hints for sources they're
  already syncing — the migration invariant pinned by
  `test_seen_sources_initialized_to_existing_on_upgrade`.

### Load-bearing — consumer-boundary tombstone-suppression invariant

Disabling a source MUST NOT generate deletion tombstones for that source's
files on the next push, or the disable-on-one-machine action propagates
fleet-wide deletion. Same shape as the kb-mbp 2026-04-24
`exclude_patterns` regression fix; same fix pattern.

`disabled_sources` applies at TWO consumer boundaries (`_push_core`
before `generate_tombstones`; `_pull_core` before `collect_tombstones`)
and MUST NOT apply at `_fetch_remote_manifest` — `mm gc` reads raw peer
manifests via that path to compute referenced blobs, and a filtered
manifest there would orphan live peer blobs. Pinned by 5 tests in
`tests/test_integration.py::TestDisabledSourcesTombstoneSuppression`:

- `test_disable_source_does_not_generate_tombstones_on_next_push`
- `test_enable_previously_disabled_source_brings_files_back_as_new`
- `test_pull_skips_disabled_source_peer_manifest_entries`
- `test_sidecar_recovery_filters_disabled_sources`
- `test_mm_gc_does_not_orphan_disabled_source_blobs`

### Changed

- **`_prompt_sources()` extracted `_prompt_source_toggle(source, *,
  current_state)` helper.** Single source of truth for the per-source
  Y/N prompt copy + default rule; reused by `_prompt_sources` (init flow)
  and `reconfigure_sources`. `mm init`'s default-Y-on-path-exists
  behavior is unchanged.

- **`get_sources()`** now applies the `disabled_sources` filter after
  resolution and before the path-existence filter.

### Tests

848 pass (789 baseline + 59 new). New test files:
`tests/test_seen_sources.py` (16 tests) and
`tests/test_source_toggle.py` (26 tests). Plus 13 new schema /
get_sources tests in `test_config.py` and 5 P0 integration pins in
`test_integration.py`.

See `docs/designs/source-toggle.md` for the full design rationale.

## [0.9.6] - 2026-04-25

Public-readiness scrub before flipping the GitHub repo from private to public.
Cosmetic-only — zero behavioral changes, all 836 tests still pass.

### Changed

- `pyproject.toml` `authors` field set to `Karl Bitz` (was placeholder
  `Mind Meld Contributors`).
- README's "How it works" section now documents the v0.9.5 auto-upgrade nudge
  (24h check, `mm: notice:` stderr prefix, `--no-check-version` opt-out flag,
  `[upgrade] auto_check = false` config opt-out, relationship to v0.9.2 fleet
  refusal). Closes the doc gap where v0.9.5's user-visible behavior was only
  described in CHANGELOG and CLAUDE.md.
- Internal commentary across CHANGELOG / CLAUDE / SPEC / ROADMAP / PROGRESS /
  cli.py / config.py / `tests/test_*.py` rephrased to remove `kb-mbp` /
  `kb-mac` personal-machine identifiers from case-study descriptions
  (replaced with `2026-04-24 first-pull` and `machine-a` / `machine-b`).
  Test fixture strings only — no semantic meaning, all assertions still pass.
- `src/mind_meld/upgrade.py` module docstring no longer references a
  personal local-path archive (`~/.gstack/projects/.../ceo-plans/`).

### Preserved

- All `kbitz/mind-meld` references in URL / repo-slug contexts kept
  (`api.github.com/repos/kbitz/mind-meld/...` auto-upgrade endpoint, `pipx
  install git+https://github.com/kbitz/mind-meld.git@vX.Y.Z` install
  commands, README CI badge, CHANGELOG bootstrap recipe). These are
  load-bearing and would break the auto-upgrade nudge if changed.

## [0.9.5] - 2026-04-25

Auto-upgrade nudge. mm now checks `/repos/kbitz/mind-meld/tags` once per 24h
and prints a `mm: notice:` line on stderr when a newer tag is available.
The user runs the printed `pipx install --force git+...@vX.Y.Z` themselves
— mm never invokes pipx itself (deferred for managed-pipx / rollback / UX
reasons; see CEO plan for analysis).

This is a leading-edge complement to the v0.9.2 fleet-version refusal:
refusal fires only AFTER a newer peer pushes data, so a user only learned
they were stale by losing a sync round. The nudge fires BEFORE that, ideally
making the refusal a backstop nobody hits.

### What's new

- New module `src/mind_meld/upgrade.py` with `check_for_upgrade`,
  `detect_self_version_transition`, `format_upgrade_message`,
  `emit_nudge_if_due`, and `run_transition_hook`.
- New CLI flag: `mm --no-check-version` skips the check + transition
  detection for one invocation. Force-skips regardless of config.
- New config key: `[upgrade] auto_check = true` (default). Set to `false`
  to disable the check persistently. Lenient validation — unknown keys
  under `[upgrade]` are silently ignored so a typo never crashes a hook.
- `mm: notice:` is a NEW stderr prefix, distinct from `mm: warning:`.
  `warning:` is reserved for data-at-risk signals (corrupt-manifest
  recovery, fsync failure, etc.); `notice:` is for FYI signals. Keeping
  the distinction preserves reader trust in `warning:`.
- `mm log` gains a new row class: `verb: "self-upgrade"` rows with
  `old_version` / `new_version` (no source/rel_path/action). `mm log`
  table renderer adds an `extra` column showing `OLD → NEW` for these
  rows; pull/push rows leave it empty. `mm log --verb self-upgrade`
  filters to transition rows.
- `mm status` surfaces the cached upgrade signal (no network call).

### Bootstrap (one-time)

The fleet won't see the nudge until each machine upgrades to v0.9.5 first
(older versions don't have the check). Run on each machine once:

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@v0.9.5
```

After that, every fleet machine self-nudges on the next 0.9.6 / 0.9.7 / etc.

### Architecture notes

- Single cache file at `~/.config/mind-meld/upgrade-state.json`, fcntl-flocked
  on every read+modify+write. Codex outside voice flagged that an earlier
  two-file split design had a read-modify-write race on transition detection;
  the single-file design is race-correct under concurrent mm processes
  (pinned by `tests/test_upgrade.py::TestRacePin` using subprocess.Popen).
- Pre-release tags (`-rc`, `-alpha`, `-beta`, `-dev`) AND local-version tags
  (`+local`) are filtered via `Version.is_prerelease` and `Version.local`.
  The latter matters because packaging sorts `0.9.4+local > 0.9.4`.
- 3 hook seams in `cli.py`: transition detection in `_get_config` /
  `_auto_command_setup` / `init_cmd` (shared helper preserves each caller's
  distinct error policy); nudge emission at the TAIL of `_pull_core` /
  `_push_core` (interactive AND quiet); status surfacing in `mm status`.
  Codex outside voice rejected an earlier "refactor through `_get_config`"
  approach because it would have broken `_auto_command_setup`'s
  silent-on-missing-config contract.
- Lock-order invariants: NEVER acquire mm lockfile while holding
  upgrade-state's flock; RELEASE upgrade-state's flock BEFORE appending to
  pullhistory. Documented in `upgrade.py` module docstring.

### Release discipline (CLAUDE.md)

This release establishes `tag = release. merge alone is not.` /ship is
responsible for tagging on release; mid-feature WIP merges to main land
without a tag (fleet stays on the prior tagged version). See
CLAUDE.md "Release discipline" section.

### Tests

47 new tests in `tests/test_upgrade.py`. Full suite: 873 passing
(826 from auto-upgrade work + 47 new + ~ already-counted Track 5D).

## [0.9.4] - 2026-04-25

**Track 5D — adversarial-review follow-ups for the v0.8.15 Track 5A ship.**
Two surgical hardening fixes plus a self-heal hook for any user already
in a half-initialized state from earlier versions.

### Fixed

- **`mm conflicts` no longer double-counts when an `include_files` entry
  sits inside an `include_dirs` directory.** Surfaced by the v0.8.15
  `/review` adversarial pass: a user customizing their gstack source
  with `include_files: ["projects/notes.md"]` AND `include_dirs:
  ["projects"]` (nested) would see duplicate rows in `mm conflicts`,
  inflated counts in `mm gc --conflicts`, and `mm resolve` silently
  no-opping on the second visit. `_find_conflict_files` now dedups via
  a `seen: set[tuple[str, Path]]` accumulator. Tuple key (not bare
  `Path`) preserves source attribution when two configured sources
  legitimately reference overlapping subtrees. Default config doesn't
  trigger this — all `include_files` entries are bare top-level
  dotfiles — but the dedup is a footgun-removal for anyone customizing.

- **`mm init` is now crash-safe between `register_device` and
  `save_config`.** Surfaced by the same v0.8.15 adversarial pass: the
  Track 5A rollback try/except handled Python exceptions only. A
  SIGKILL/OOM/power loss in the window between `save_config()` returning
  and `register_device()` either succeeding or raising left the user
  with a local config claiming a `device_id` that storage's
  `devices/<id>.json` didn't contain — peers never discovered the
  device, and every subsequent push wrote manifests under an ID no
  one was listening for.

  v0.9.4 swaps the order: `register_device` runs FIRST (storage write),
  then `save_config` (local pointer). The "remote first, local pointer
  last" pattern is canonical filesystem/DB transaction discipline. A
  crash after register but before save now leaves an inert orphan
  storage entry, recoverable on retry init via `_init_storage_guard`'s
  orphan-case prompt. The original local-side rollback try/except is
  removed; a new best-effort `backend.delete(devices/<id>.json)` wraps
  `save_config` so normal save failures (disk full, permissions) don't
  trip the orphan-case warning on retry init. The original `save_config`
  exception always wins — cleanup failures land as a `mm: warning:`
  stderr breadcrumb without masking the real cause.

  Function renamed `_save_and_register` → `_register_and_save` to
  match the new ordering. Init's docstring updated.

### Added

- **Push-time self-heal for missing device entries.** New
  `_ensure_device_registered` hook at `_push_core` entry: if
  `devices/<my_id>.json` is absent, recreate it via `register_device`
  before any push work runs. Two scenarios converge here:
    - Future v0.9.4+ SIGKILL crash mid-init (cosmetic, accepted by the
      order swap above).
    - Pre-v0.9.4 victims of the v0.8.15..v0.9.3 inverted half-state
      (config has `device_id`, storage's `devices/` doesn't). Without
      this hook those users push manifests under an ID no peer
      recognizes, silently. The fix is retroactive: first push after
      upgrade self-heals.

  Gated on `not dry_run` (codex review caught: `mm push --dry-run`
  must not mutate storage). Register failures land a stderr `mm:
  warning:` breadcrumb before re-raising — load-bearing for autopush,
  whose generic `except Exception` would otherwise swallow the failure
  and silently no-op every push.

### Tests

15 new tests pin the regressions:
- 5 in `tests/test_conflict_copy.py::TestFindConflictFilesNestedDedup`
  (overlap dedup, canonical preserved, distinct-not-collapsed, gc E2E,
  pre-inversion migration path).
- 7 in `tests/test_track_2a.py::TestRegisterAndSave` (new ordering,
  no-keyring, register-failure-no-save, save-failure-cleanup,
  cleanup-failure-doesnt-mask-save-error, dry_run skip, no-committed-messages).
- 3 in `tests/test_track_2a.py::TestEnsureDeviceRegistered` (self-heal
  on missing, no-op when present, register-failure stderr+propagate +
  dry-run skip).

789 tests pass. No fleet-version threshold change —
`INVERSION_MIN_VERSION` stays at `"0.9.2"`, v0.9.4 is no harder to
roll out than v0.9.3 was.

## [0.9.3] - 2026-04-25

Small follow-up patch caught immediately after v0.9.2 ship: add
`config.yaml` to the gstack source's default `exclude_patterns`. Track 5C
(v0.9.1) covered `projects/*/repo-mode.json` and
`projects/*/land-deploy-confirmed` but missed `~/.gstack/config.yaml`,
which holds gstack's version-check tracking (the "last successful gstack
version" per machine, plus other machine-local IDs). Syncing it actively
breaks the version mechanism on whichever machine pulls last.

Existing installs need to run `mm migrate-config` to pick up the new
recommended exclude — autopull/autopush surface the missing-excludes
signal via `mm status` (the visible-failure contract from v0.9.1).
Fresh installs get the fixed default automatically.

No fleet-version threshold change — `INVERSION_MIN_VERSION` stays
at `"0.9.2"`, same as v0.9.2. v0.9.3 is no harder to roll out than
v0.9.2 was: a v0.9.0/v0.9.1 peer was already a refusal under v0.9.2
and remains so under v0.9.3.

### One-time cleanup for v0.9.2 → v0.9.3 upgraders

If you ran v0.9.2 long enough to hit a `config.yaml` conflict (and have
a stranded `~/.gstack/config.sync-conflict-<ts>-<device>.yaml` sidecar
on disk), v0.9.3 can no longer surface it via `mm conflicts` / `mm
resolve` / `mm gc --conflicts` — the conflict-file walker iterates
`include_files`, which no longer contains `config.yaml`. List + clean
up by hand once:

```bash
find ~/.gstack -maxdepth 1 -name 'config.sync-conflict-*' -ls
find ~/.gstack -maxdepth 1 -name 'config.sync-conflict-*' -delete
```

Adversarial review (Claude + Codex) flagged this on /ship; the proper
fix (depth-0 scan of source root in `_synced_scan_dirs`) lands in a
follow-up patch.

## [0.9.2] - 2026-04-25 — BREAKING

**Track 5E (Conflict default inversion) + 4 ship-fix bug fixes caught by
the /ship pre-landing review.** Headline change: `_apply_conflict` now
keeps LOCAL bytes at canonical and writes REMOTE bytes to the
`.sync-conflict-*` sidecar — the opposite of every prior version.

### Pre-landing review fixes (ship-fix bundle)

The /ship workflow's pre-landing review found 1 CRITICAL + 3 HIGH bugs
in the Track 5E implementation. All four are fixed in the same release:

- **F1 CRITICAL — silent data loss in resolve.** The pre-inversion
  conflict-file migration sweep at `_pull_core` and `mm resolve` couldn't
  distinguish pre-v0.9.2 conflict files (no `v0-` prefix) from fresh
  post-inversion conflict files produced by THIS version's
  `_apply_conflict` (which also has no prefix). On consecutive pulls,
  every fresh post-inversion sidecar got false-tagged `v0-` and
  `_resolve_interactive_loop` then dispatched it under inverted
  semantics — picking `(l)ocal` would silently overwrite local edits
  with remote bytes. Fix: one-shot install marker file at
  `~/.config/mind-meld/inversion-installed-at`. The migration sweep
  now skips files whose mtime is at-or-after the marker (i.e.
  produced by this version's writer). Fail-safe: if the marker is
  unreadable/unwriteable, refuse to migrate rather than risk
  mass re-tagging.
- **F2 HIGH — `migrate-config` could brick `config.toml`.**
  `_toml_value` did no escaping on string literals; a user-customized
  `exclude_patterns` glob containing `"`, `\`, or a newline would
  round-trip through `mm migrate-config --yes` as malformed TOML and
  wedge the next `mm` invocation on parse error. Fix: escape `\`, `"`,
  `\n`, `\r` per the TOML basic-string spec.
- **F3 HIGH — autopull spammed `autopull.log` on mixed-version fleet.**
  `_check_fleet_version_or_refuse` raises via `_error()` →
  `typer.Exit(1)`, which is a `RuntimeError` subclass, not a
  `MindMeldError`. autopull's `except MindMeldError` branch missed it
  and the generic `except Exception` treated the typed refusal as an
  unexpected error — writing the full multi-line refusal text to
  `autopull.log` and a "failed" breadcrumb on every Claude Code
  session start. Fix: explicit `except typer.Exit` branch in
  autopull/autopush BEFORE the generic catch; outcome is now
  `fleet-refused` (autopull) / `refused` (autopush).
- **F4 HIGH — pullhistory self-DOS from autopull excluded logging.**
  `_pull_core`'s exclude-filter loop wrote one `pullhistory.append(action="excluded")`
  record per peer × source × excluded-rel_path tuple. At ~100
  projects × hourly autopull hooks, the 1MB `pull-history.jsonl` cap
  rotated within hours and evicted real `written / merged / conflicted /
  failed` records to `.1`. The audit-log feature became useless. Fix:
  skip excluded-path logging when `quiet=True` (autopull/autopush);
  interactive `mm pull` still logs the full set so users can audit
  their excludes via `mm log --action excluded`.

11 new IRON RULE regression tests pin all four fixes (post-inversion
file consecutive-pull safety, mtime-gate migration when older than
marker, marker-failure fail-safe degrades to no-migration, autopull
no-excluded-logging in quiet, interactive pull DOES log excluded,
TOML escape round-trip for `"`/`\`/newline, autopull fleet-refusal
breadcrumb is `fleet-refused` not `failed`).

### Track 5E (original scope)

Two reasons for the inversion: (1)
asymmetric blast radius — local is the known-working version on this
machine, remote is the unknown-from-elsewhere version; (2) the visible
sidecar should hold the *surprising* bytes, not the working ones.
Mtime-skip already handles "local newer," so the conflict path only
fires when remote is newer or mtimes are equal — but "remote newer" never
meant "remote correct for this machine."

**Strict pull-start fleet-version refusal (load-bearing).** `mm pull`
now exits non-zero BEFORE any download/write if any peer device's
`last_seen_version < 0.9.2`, OR if any peer's `device.json` is corrupt
(can't read its version → can't trust its conflict files). Per-peer
classification: safe / inactive (registered, never pushed → ALLOW) /
pre-v0.9.2 (REFUSE) / dropped (REFUSE by storage key). The refusal
message names every offending peer and points at `mm devices` for the
version table. Recovery: upgrade peers and have each push once before
re-pulling. Last-resort: hand-edit `device.json` to add
`"last_seen_version": "0.9.2"` (only after verifying peer is actually
upgraded).

**Pre-inversion conflict-file migration.** Existing `.sync-conflict-*`
files produced by pre-v0.9.2 code carry no marker. The first
lock-protected discovery in `mm pull` or `mm resolve` migrates them by
renaming to `.sync-conflict-v0-<ts>-<dev>.<ext>`. Idempotent (`v0-`
prefix is its own no-op signal); collision-safe (target exists → leave
both, don't overwrite); per-file rename failure logs and continues.
`mm conflicts` does NOT migrate — it's lockless and would race autopull
(codex-2 #5).

**Dual-mode resolve dispatch by filename prefix (NOT timestamp).** `v0-`
files dispatch under PRE-inversion semantics (sidecar = local bytes;
canonical = remote): `(l)ocal` renames sidecar over canonical, `(r)emote`
unlinks sidecar. Files without the prefix dispatch under POST-inversion
semantics (canonical = local; sidecar = remote): `(l)ocal` unlinks
sidecar, `(r)emote` renames sidecar over canonical. Per-file diff labels
flip per row; the prompt copy clarifies which file currently holds which
side.

**`mm conflicts` table** gains a "Mode" column (`pre-v0.9.2` /
`v0.9.2+`). Per-row "local" / "remote" column meanings derive from the
prefix. Footer hint when pre-inversion files are present nudges users to
`mm resolve` for migration.

**`mm devices` table** gains a "Version" column showing each peer's
`last_seen_version`. `update_last_seen` writes `last_seen_version:
__version__` alongside `last_seen` on every push (forward-compatible —
older mm tolerates unknown keys).

**`packaging>=21.0`** added to `dependencies` for `Version` parsing in
the fleet-version comparator.

**11 new tests** in `tests/test_integration.py::TestInversion5E` pinning
the IRON RULE regressions: inversion correctness (canonical = local,
sidecar = remote, no `v0-` prefix on fresh files), mixed-version refusal
correctness (4 sub-cases: pre-v0.9.2 explicit, missing field, inactive
peer permitted, corrupt device.json refuses by key), self-exclusion
from scan (no peers → no self-refusal), pre-inversion file resolves
under v0- dispatch (both `(l)` and `(r)` ops), `mm conflicts` is
read-only (no rename), `mm resolve` migrates pre-inversion files. 768
pass.

### Breaking
- Conflict-direction inversion: pre-existing `.sync-conflict-*` files
  produced by pre-v0.9.2 code now require the migration step before
  resolve dispatches them correctly. `mm pull` and `mm resolve` perform
  the migration automatically; manual migration via `mv` is also safe
  (rename `<x>.sync-conflict-<ts>-<dev>.<ext>` →
  `<x>.sync-conflict-v0-<ts>-<dev>.<ext>`).
- `mm pull` REFUSES when any peer reports a pre-v0.9.2
  `last_seen_version` (or none, if last_seen is present). Upgrade peers
  to v0.9.2 and have each push at least once before pulling here.
- `_apply_conflict` no longer renames the local file. Callers that
  assumed the rename + rollback dance must be updated; the in-tree
  `_resolve_interactive_loop` and the test suite were both updated.

### Added
- `INVERSION_MIN_VERSION = "0.9.2"` constant in `cli.py`.
- `is_pre_inversion_conflict_filename(name) -> bool` in `manifest.py`;
  `CONFLICT_PATTERN_V0` / `CONFLICT_V0_PREFIX` constants.
- `list_devices_with_drops(backend)` in `devices.py` — variant of
  `list_devices` returning `(valid, dropped)` so the fleet refusal
  gate can name the offending storage key (codex-2 #3).
- `_check_fleet_version_or_refuse(backend, my_device_id)` in `cli.py`,
  invoked at the top of `_pull_core` before any I/O.
- `_migrate_pre_inversion_conflict(path)` helper in `cli.py`;
  `_find_conflict_files(config, *, migrate_pre_inversion=False)` opt-in
  flag wired into `mm pull` / `mm resolve` (lock-protected callers only).
- `mm conflicts` "Mode" column; `mm devices` "Version" column.
- 11 new tests in `TestInversion5E`.

### Changed
- `_apply_conflict` body: writes remote to sidecar (no rename, no
  rollback); local stays at canonical.
- `_resolve_interactive_loop`: dual-mode dispatch by filename prefix.
  Diff labels flip per row.
- `_predict_pull_outcome` user-facing string updated to "would write
  remote to .sync-conflict-*".
- `_apply_incoming_file` decision-tree comment updated to reflect
  inversion + per-row label note.
- Track 5B's `5B-5C-REMAP-BOUNDARY` markers throughout `cli.py` and
  `tests/test_conflict_copy.py` removed; the inversion is now in place.
- `update_last_seen` writes `last_seen_version` on every push.
- `pyproject.toml`: `packaging>=21.0` added to dependencies.

### Recovery from a stuck "Mixed-version fleet detected" refusal
1. Check `mm devices` — the offending peer(s) show `—` or a pre-v0.9.2
   value in the Version column.
2. Upgrade each peer (`pip install --upgrade mind-meld`) and run
   `mm push` from each one.
3. Re-run `mm pull` here. The fleet check should now pass.

If a peer is permanently offline / decommissioned, hand-edit its
`devices/<id>.json` in the storage root to add
`"last_seen_version": "0.9.2"` — but only do this if you're certain the
peer will never push again (or has been verified-upgraded out-of-band).
The check exists to prevent dual-semantics conflict-file production
that this puller would silently mis-resolve.

## [0.9.1] - 2026-04-25

**Track 5C (exclude_patterns + log + migration UX) — additive.** Per-source
`exclude_patterns` glob list lets gstack and other sources opt out of syncing
per-machine artifacts (gstack `repo-mode.json` 7-day TTL caches and
`land-deploy-confirmed` deploy markers, both recomputed locally on every
machine). Default gstack source ships with the recommended globs. Existing
configs need to opt in via the new `mm migrate-config` command (idempotent,
acquires the mm lockfile, preserves user-customized excludes by appending).

**Consumer-boundary filter wiring (codex-2 #1 + #2 fix).** New
`_filter_excluded_paths(manifest, exclude_map)` helper applies at TWO call
sites: `_pull_core` (peer manifests, before `collect_tombstones` and per-
source download) and `_push_core` (the manifest returned from
`_recover_prior_manifest`, before `generate_tombstones`). Critically NOT at
`_fetch_remote_manifest` — `mm gc` reads raw manifests via that path to
compute referenced blobs, and a filtered manifest there would mark live peer
blobs as orphans (the gc-bypass hazard pinned by `test_mm_gc_does_not_orphan_
excluded_path_blobs`).

**Tombstone-suppression invariant.** Adding a path to `exclude_patterns`
must NOT generate a deletion tombstone on the next push (2026-04-24
first-pull regression: without consumer-boundary filtering, every newly-excluded path
ships a tombstone that propagates to peers). Removing a glob brings the
path back as new (no spurious tombstone). Sidecar recovery is filtered too
so a corrupt-manifest recovery on a freshly-migrated config doesn't re-
introduce pre-exclude paths via the sidecar.

**`mm log` subcommand.** Append-only JSONL log at
`~/.config/mind-meld/pull-history.jsonl` records every per-file pull/push
action ("written" / "merged" / "skipped" / "conflicted" / "excluded" /
"uploaded" / "failed"). 1MB cap with line-boundary rotation to `.1` (no
byte-tail truncate; reader tolerates a torn first line in `.1` as the
crash-mid-rotate fingerprint). Filters: `--source`, `--since`, `--action`,
`--verb`, `--limit`, `--format {jsonl|table}`. Useful for "what conflicted
on date X" audits even after the conflict files are resolved, and for
"what is my exclude_patterns actually filtering" via `mm log --action
excluded`. fcntl.flock on every append; mode 0600.

**`mm migrate-config` command.** Diffs current `[[sync.sources]]` against
`DEFAULT_SOURCES` and proposes adding any missing recommended
`exclude_patterns` globs. Idempotent. `--yes` skips the inner confirm prompt
for scripted invocation; `--dry-run` shows the diff without writing. Wholesale
replaces the `[[sync.sources]]` array (per `patch_config_on_disk`'s contract);
other `[sync]` keys (`max_file_size`) survive via per-field merge.

**Migration UX (visible-failure contract).** Interactive `mm pull` / `mm push`
prompt-once if recommended excludes are missing. autopull/autopush NEVER
auto-mutate config — they record the missing-excludes signal to
`~/.config/mind-meld/migration-state.json` and let `mm status` surface it
on the next interactive run. Without this, a wedged config would silently
keep producing conflict copies forever with no signal that
`mm migrate-config` would fix it.

**`mm sources` extension.** Adds an "Excluded" column counting how many files
each source's `exclude_patterns` actually matched on this scan. Diagnostic
only — sanity-check an over-broad glob (e.g. `**/*.json` skipping
everything) before pulling on every machine.

**`mm status` extension.** Surfaces "Config missing recommended excludes for
source(s) X — run `mm migrate-config` to add" so users notice their config
drift even when only autopull/autopush is running.

38 new tests (5 IRON RULE regression pins: two-device first-pull case,
tombstone-on-exclude-transition, tombstone-on-unexclude-transition, sidecar
bypass guard, mm gc safety). 758 pass.

### Added
- `exclude_patterns: list[str]` field on `[[sync.sources]]` entries.
  `_validate_exclude_patterns` rejects non-list and non-string-element
  shapes at config load time (not mid-sync).
- `gstack` `DEFAULT_SOURCES` entry now includes
  `["projects/*/repo-mode.json", "projects/*/land-deploy-confirmed"]`.
- `_filter_excluded_paths(manifest, exclude_map) -> dict` consumer-boundary
  helper; `_build_exclude_map(config)` companion that walks `get_sources`.
- `src/mind_meld/pullhistory.py` — append-only JSONL writer + reader
  with fcntl flock, 0600 perms, line-boundary rotation at 1MB.
- `mm log` subcommand with `--source`, `--since`, `--action`, `--verb`,
  `--limit`, `--format` flags.
- `mm migrate-config` command with `--yes` and `--dry-run` flags.
- Interactive `mm pull` / `mm push` migration prompt + auto-command
  breadcrumb at `~/.config/mind-meld/migration-state.json`.
- `mm sources` "Excluded" column.
- `mm status` "missing recommended excludes" warning.

### Changed
- `manifest._is_excluded(rel_path, exclude_patterns=None)` — extended to
  accept per-source globs; backward-compatible default.
- `manifest._record_file` and `walk_claude_source` now accept
  `exclude_patterns`; `walk_source` threads it for `claude` types and
  `walk_generic_source` reads it from the source dict.
- `_upload_changed_blobs` accepts `src_name` for `pullhistory.append`
  bookkeeping; legacy callers without source context still work
  (None → no log entry).

### Codex review notes (out of scope; tracked separately)
- Codex-1 #14 left two `mm conflicts` count-diff tests in test_conflict_copy.py
  asserting "6 vs 9" depending on platform fnmatch. Out of 5C scope; the
  count diff is documented in the resolve-mode flow but not regression-pinned.

## [0.9.0] - 2026-04-25

**Track 5B (Pull / resolve / conflicts UX surfaces) — BREAKING.** Vocabulary
unified across `mm resolve`, `mm conflicts`, and pull summary: `(c)anonical /
(f)orce conflict` becomes `(l)ocal / (r)emote / (b)oth / (a)bort`. Old letters
`c` and `f` are rejected loudly to stderr (visible-failure contract; piping
legacy scripts now errors instead of silently falling through to the default
"kept both"). Diff display labels (fromfile/tofile), helper text in
`mm conflicts`, the `mm resolve` docstring, and the parallel `(p)/(d)/(s)`
preface all flipped to the new vocabulary in one PR (per `/plan-ceo-review`
D3 scope expansion).

`mm conflicts` table renames "Conflict" / "Canonical" columns to "local" /
"remote" plus per-column wrap (`add_column(no_wrap=False, overflow="fold")`)
so long paths no longer truncate at terminal width. `_print_pull_summary`
now lists conflicted/failed paths inline under each per-source line (cap 20
with overflow marker; `--verbose` unlocks the cap per `/plan-ceo-review` D5).

Pre-existing docstring/code mismatch fixed (D11): per-source conflicts and
failures now reach stderr in quiet mode (autopull), with `<device>/<source>`
prefix because the per-device header is suppressed in quiet — matches
CLAUDE.md "Load-bearing warnings" contract.

`mm pull` Rich Progress widget for TTY (`console.is_terminal` gate), plain
"downloading N file(s)" banner for non-TTY, silent in autopull (`quiet`
threaded through `_pull_one_source` → `_download_and_apply` so progress
can't leak in autopull). Empty-`to_download` gate prevents Rich Progress
with `total=0`.

Variable names in `_resolve_interactive_loop` (`local_text`/`remote_text`)
reflect today's `_apply_conflict` semantics with `5B-5C-REMAP-BOUNDARY`
markers throughout cli.py + the test class so Track 5C's inversion will
surface every assertion that needs to flip. Pre-inversion `.sync-conflict-*`
files persisted on disk are 5C's problem to handle (timestamp-based
detection or one-time migration; filed in the 5C handoff per
`/plan-ceo-review` D9).

14 new tests pinning today's mapping, the quiet contract, the cap/verbose
behavior, multi-device disambiguation, and Task 4 quiet plumbing.
700 pass.

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
`mm resolve` / `mm gc --conflicts` (the 2026-04-24 first-pull session saw 5 of 6
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
