# Mind Meld

[![CI](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml/badge.svg)](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml)

Sync AI coding-agent context, skills, and gstack activity across Macs via iCloud Drive. End-to-end encrypted. Supports Claude Code, Codex, and Grok. `mm` maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills` (Claude Code and Codex; verified 2026-08-24). Grok 1.0.5 already loads that directory via its default-on Claude compatibility layer, so `mm diag` reports Grok under `host_skill_discovery`, not as a third `skill_links` row.

## Install

Prerequisites: macOS with iCloud Drive, Python 3.11+, and pipx.

```bash
pipx install git+https://github.com/kbitz/mind-meld.git@latest
```

Not on PyPI — install straight from GitHub. The `@latest` ref is a branch the release workflow force-advances to each tagged release, so you always get the newest *released* version (never untagged work-in-progress off `main`) **and** plain `pipx upgrade` keeps working (see below).

## Upgrading

**v1.0.0:** no action needed beyond upgrading. Mixed 0.14.x/1.0.0 fleets
interoperate with no wire or storage migration. The
[Compatibility (1.x) contract](docs/invariants/auto-upgrade.md#compatibility-1x)
now defines the stable surfaces. `b` / `both` still leave both conflict files
on disk, without the retired alias notice. Newer-format storage refuses with
an upgrade remedy. Once v1.0.0 is tagged, a regression ships as 1.0.1: the
upgrade nudge never downgrades.

**v0.14.9:** the old identity cache becomes stale and refreshes on the next identity read that completes repository discovery; incomplete attempts retry.
The first push needing that refresh prints `mm: notice: refreshing identity cache (one-off)`. [Future capture is corrected; previously published rows stay unchanged.](#dropped-repositories-and-ignored-git-environment-variables)

For `Pull incomplete:` or a per-file warning after upgrading, see [pull failures and remedies](#pull-incomplete--could-not-pull-a-file). No data migration is needed for apply exception containment.

```bash
pipx upgrade mind-meld
```

That's it. Because the install tracks the moving `latest` branch (not a frozen tag), `pipx upgrade` re-resolves it to the newest release and lands it.

**Stuck on an old version?** If you ever installed or upgraded with the old `--force …@vX.Y.Z` form, your install is pinned to that exact tag — pipx re-resolves the frozen ref on every `pipx upgrade` and reports your current version as "latest" forever. A git tag never moves; a branch does. Run this once to switch onto the `latest` branch, after which plain `pipx upgrade mind-meld` works:

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@latest
```

(This is exactly the command mm's auto-upgrade nudge prints.)

**Need to roll back?** Pin the last 0.x release, then replace the skill store
with the running package's copy. If the store exists, move it aside first:
`mm install-skills` never overwrites a newer stored skill with an older one.

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@v0.14.18
mv "$HOME/.local/share/mind-meld/agent-skills/retro-fleet" "$HOME/.local/share/mind-meld/agent-skills/retro-fleet.pre-rollback-$(date +%Y%m%d-%H%M%S)"
mm install-skills
```

1.0.0 and 0.14.18 use the same storage and wire formats. Rollback within 1.x
is a reinstall unless an intervening MINOR's Upgrade notes say otherwise.
Keep the moved store as a backup. Re-run the `@latest` reinstall above to
resume upgrades; the nudge never downgrades a pinned install for you.

## Versioning and compatibility

The [Compatibility (1.x) contract](docs/invariants/auto-upgrade.md#compatibility-1x) defines stable formats, CLI and machine-readable surfaces, release classifications, and downgrade rules.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Completed successfully. Valid `autopull`/`autopush` invocations also exit 0 on refusal or degradation; inspect stderr and `mm status`. Interactive pull keeps exit 0 when individual files fail. |
| 1 | Stopped or aborted, including EOF/Ctrl-C at a prompt, config/crypto/lock failures, required maintenance failure, and argument values rejected after parsing (`mm log --since bad`, `mm log --format xml`, `mm recapture 999d`). A push may already have published before required GC stops. |
| 2 | Usage error from Typer/Click or retro-fleet's argparse, including unknown options on autorun commands. |
| 3 | `pull --conflict-mode fail` preflight predicts a conflict or local failure, including with `--dry-run`; no files applied. |
| 4 | `recapture` completed only partially (`RECAPTURE_EXIT_PARTIAL`). |

Each outcome maps to a behavioral test in
[`EXIT_EVIDENCE`](tests/test_compat_contract.py); the [Previews](#previews)
table lists the applicable codes per command. Exit 0 from content sync does
not prove host usage was captured or published.

## Quick Start

```bash
mm init    # configure iCloud storage + passphrase
mm push --dry-run   # preview: changes nothing except the lock file
mm push    # upload configured agent context
mm pull --dry-run   # preview: changes nothing except the lock file
mm pull    # download from another device
```

Config lives at `~/.config/mind-meld/config.toml` — not tied to your current directory. Install `mm` anywhere, run from anywhere; it always syncs the sources configured in your global config.

## Setting up a second (or third) Mac

1. `pipx install git+https://github.com/kbitz/mind-meld.git@latest` on the new machine.
2. `mm init` — point it at the **same iCloud folder** as your first Mac and enter the **same passphrase**. This registers the new device against the existing roster.
3. `mm pull` — downloads everything the other machine(s) have pushed.
4. `mm push` — uploads anything this machine has that the others don't.

Push and pull on each Mac to exchange changes according to each file's sync rules:

- **`.jsonl` files** deep-merge by line-union (deduped). Records with a string `ts` sort first by that string, then by the full line; every other line follows in lexical order. **`MEMORY.md`** is a plain lexical line-union and does not consult `ts`. Entries from all machines accumulate — this is why telemetry, learnings, and timeline files stay coherent across devices.
- **Other divergent files** use mtime-skip: if your local file is newer than the remote, pull leaves it alone. Otherwise, with the default `keep-both` mode, your local file stays at the canonical path and the remote version is saved as `<stem>.sync-conflict-<ts>-v1-<device>.<ext>` sitting next to it (Syncthing convention). See [Handling conflicts](#handling-conflicts) below.
- **Deletions** propagate via tombstones in the manifest. A tombstone stops a missing file from being restored from an older peer snapshot; it does not itself delete a file that is already on the receiving Mac.

After the first successful `mm push` on Mac A, open Mac B and `mm pull`, then confirm one selected file you edited on A is present on B.

First-run-from-divergent-state is explicitly supported: if each Mac already has its own memory/todos/analytics before you first run `mm init`, the three-way sync will merge the JSONLs, download missing files, and flag any true content conflicts as `.sync-conflict-*` for you to triage with `mm resolve`.

## Fast pulls (auto-pin)

`mm init` automatically pins your iCloud storage folder so blobs stay resident on this Mac. Without pinning, iCloud may evict cold blobs to save local disk and `mm pull` then blocks on iCloud File Provider materialization — fine over time, but slow on a fresh Mac's first sync.

Auto-pin runs `brctl download <storage_path>` (Apple's iCloud File Provider CLI) once at init. It's non-destructive, idempotent, and asynchronous: brctl returns immediately while iCloud materializes files in the background. You'll see a `Storage pinned for fast pulls` line on success, or a Finder right-click tip if `brctl` errors.

If you ever want to undo the pin (free up local disk):

```bash
brctl evict ~/Library/Mobile\ Documents/com~apple~CloudDocs/mind-meld
```

…or in Finder, right-click the folder and choose **Remove Download**.

If your storage path is **not** under iCloud Drive (e.g. a custom local folder or a different cloud sync), auto-pin is silently skipped — the slow-pull case only applies to iCloud-managed paths.

## Claude Code Integration

Mind Meld includes `autopull` and `autopush` commands designed for Claude Code — they never prompt and stay silent when already in sync. A failed apply prints one `mm: warning:` line per failed file plus a per-source summary and a total count.

Add the following to your **global** `~/.claude/CLAUDE.md` to have Claude automatically sync at the start and end of each conversation:

```markdown
# Mind Meld

At the **start of each conversation**, run:

\`\`\`bash
mm autopull
\`\`\`

- **No output:** Already in sync. Continue silently.
- **Any output:** Tell the user what was synced; surface warnings and their suggested fixes.

At the **end of each conversation** (when the user is wrapping up, says goodbye,
or you've completed the requested task), run:

\`\`\`bash
mm autopush
\`\`\`

- **No output:** Nothing to push. Say goodbye normally.
- **Any output:** Tell the user what was pushed.

If `mm` is not installed, both commands will fail silently — no action needed.
```

### How it works

- `mm autopull` checks all other registered devices for changes and applies them locally. It writes a `.mind-meld-log.md` breadcrumb to each affected project so Claude Code knows what changed.
- `mm autopush` builds a manifest of the configured sync sources, diffs against the last push, and uploads only what changed.
- Both commands acquire a lockfile, never prompt for input, and exit gracefully on any error (so they never block Claude Code).
- "Silent" means no chatter on the happy path. Load-bearing degradation warnings — corrupt-manifest recovery, "no sync sources" misconfig, durability fsync failure, per-file pull failures — still reach stderr. Apply failures print one `mm: warning:` line per failed file plus a per-source summary and a total count so a wedged background sync surfaces instead of rotting. Autopush writes a `no-sources` breadcrumb (separate from `success`) when the config has no sync sources. Both auto commands also write a `degraded` breadcrumb (separate from `success`) when an otherwise-successful run lost data: autopull on fsync durability failure, corrupt peer manifest, unknown source from a peer, or per-file apply failure; autopush (v0.12.16) when the fleet-retro events tail failed, exceeded its walk budget, or published no token/skill data because the token cache was cold or locked. A dropped host-usage reader — Mind Meld isolates host readers, so a source it cannot read is declared and omitted from that row's coverage rather than deleting the others or publishing a silent partial total — is reported the same way in `mm status`, and costs optional fleet-retro analytics only, never content sync. The `detail` field enumerates which signals fired. `mm status` and any monitoring on top of it can catch both wedge and partial-degradation cases. The one wedge no breadcrumb can report is the command never running at all — an `ImportError` at module scope, say, which dies before typer's runner and writes nothing — so since v0.12.21 `mm status` also marks any autorun breadcrumb older than 48 hours as `stale — no autorun in Nh` instead of reporting the last `success` forever.
- Fleet-retro capture is best-effort and never blocks content sync. Git repository discovery gets a small independent time budget; if it expires, autopush records a `degraded` breadcrumb and prints `mm: notice: git repository discovery hit its time budget: this push captured an incomplete repository set. Run mm diag, then mm recapture 30d to recover the omitted commits`. The detail is deliberately generic: it never exposes local paths or probe errors. For Git recovery, do not retry a bare empty push—the events tail runs after a substantive sync change. For host usage, upgrade the producing Mac to v0.14.17+ and run mm push. A later ordinary push does **not** recapture the omitted interval; `mm recapture 30d` on the Mac that owns the repositories does. A healthy no-op autopush can still refresh its local autorun breadcrumb, which proves the hook ran; it does **not** mean fleet retro received a new activity event.
- **Auto-upgrade nudge (v0.9.5).** Once per 24h, `mm pull` / `mm push` (including the autopull/autopush variants) check GitHub for a newer release tag and emit a single `mm: notice: <old> → <new> available — run pipx install --force git+...@latest` line on stderr if you're behind. `mm` never invokes pipx itself; you run the printed command. The command tracks the moving `latest` branch (not a frozen tag), so it always lands the newest release and — crucially — rewrites any previously tag-pinned install's recorded URL onto `@latest`, after which plain `pipx upgrade mind-meld` works (see [Upgrading](#upgrading)). Disable with `--no-check-version` for one invocation, or set `[upgrade] auto_check = false` in `~/.config/mind-meld/config.toml` to disable persistently. The `notice:` prefix is distinct from `warning:` (reserved for data-at-risk signals). This is a leading-edge complement to the v0.9.2 fleet-version refusal, which only fires after a newer peer pushes data — the nudge fires before that, ideally making the refusal a backstop nobody hits.

## Codex Integration

Mind Meld treats Codex as a first-class peer of Claude Code. Grok is the other usage-reader host (see [Grok usage in fleet retro](#grok-usage-in-fleet-retro)).

- `codex` syncs `~/.codex/AGENTS.md`, `skills/`, and `plugins/`.
- Account credentials, session databases, logs, tool output, and whole-file `config.toml` settings are not sync sources. Those settings can contain inline provider or MCP credentials, so they stay local until Mind Meld can safely filter individual fields.
- Enabling the `codex` source also lets the fleet-retro capture read that host's local usage records to publish **aggregate token counts per day** (no prompts, transcripts, paths, or tool output ever leave the machine). Decline the source and its records are never opened. By default that also stops maintenance of a skill link in that host's skills directory; an explicit `[skills] agents` grant is the deliberate exception. `mm enable-source grok` is the same verb: it adds a scoped Grok source (`skills/`, `commands/`, and `rules/` only — same idea as Claude's `memory/` + `todos/`) and opts this Mac into reading terminal token totals from local `updates.jsonl` records. Session files, prompts, and chat history stay on the Mac.
- The bundled `/retro-fleet` skill is installed for an agent when that agent's sync source is enabled — on `mm init`, `mm push`, and `mm install-skills`. To keep a link maintained without enabling sync or usage reading: `mm install-skills --agent <key>`.

Symlinks inside any sync source are local routing, not portable content: Mind Meld does not upload them and leaves existing local links untouched on pull, including dangling links and linked directories. A source root itself may be a symlink.

Fresh installs are asked about the `codex` source during `mm init`. That choice also decides whether mm maintains a `retro-fleet` skill link in that agent's skills directory. Existing installations remain opt-in:

```bash
mm enable-source codex
mm enable-source grok
```

To verify Codex usage capture on an initialized Mac with Codex logs and the
`mm-events` source enabled, first upgrade to v0.14.17+ and verify with `mm --version`:

1. `mm enable-source codex`
2. Run `mm push`, including on a Mac with no synced changes.
3. Run `mm status`: expect a recent host capture, Codex contributed, and
   `Publication: published`. `mm diag` separately describes the cache:

   ```text
   codex cache inventory:   ready
   codex usage read blocker: none
   ```

`ready` describes cached rollout inventory; `none` means no known standing
read blocker. Neither proves a snapshot was published. The device-wide capture
and publication lines in `mm status` provide that evidence. For a blocker, see
[Host usage capture](#host-usage-capture-codex-and-grok).

### Grok usage in fleet retro

`mm enable-source grok` does two things: it syncs `~/.grok` `skills/`, `commands/`, and `rules/` (session files stay local), and it opts this Mac into reading terminal token totals from local `updates.jsonl`. Prompts never leave the Mac. The fleet retro's `AGENTS` block then gains a `Grok` row alongside Claude and Codex, on the same footing.

Upgrade is per Mac, and **upgrading is not enough**. On each Mac, first upgrade
to v0.14.17+ and verify with `mm --version`, then:

1. `mm enable-source grok`
2. Run **`mm push`**. It needs enabled, resolved `mm-events` and host consent, but no Git roots or content changes. Each cold reader may print `mm: reading grok usage beyond [retro] host_usage_interactive_budget_ms (about 5 s of scanning)...`; scanning is cooperative, not a hard ceiling. Autopush never warms.
3. Verify on the **producing Mac** with `mm status`: a recent recorded capture, Grok contributed (or explicitly partial), and `Publication: published`. A prior successful scan or `grok usage read blocker: none` describes only the cache.
4. Optionally verify end to end from a **second Mac** after iCloud delivers the files. Run `mm devices --format=json` to find the producing Mac's id and substitute it for `<id>`:

```sh
mm pull
mm devices --format=json
mm retro-fleet 7d --dump-host-usage | jq '.by_device["<id>"] | {as_of, consulted, partial, degraded}'
mm retro-fleet 7d --dump-host-usage | jq '[.by_device["<id>"].tokens_by_day[]?.by_model | keys[]] | unique'
mm retro-fleet 7d
```

Expected: a new `as_of`, the enabled readers in `consulted`, and their actual workload's models in the model list when usage exists. A completed empty scan is valid; no fixed model id or nonzero counter is required. Readers in `partial` contributed usable but incomplete totals; readers in `degraded` contributed nothing. For example:

```json
{"as_of": "2026-09-15T12:00:00+00:00", "consulted": ["codex", "grok"], "partial": [], "degraded": []}
```

The retro should name Grok's inherent pricing floor. After the next real capture, repeat the second-Mac check: `as_of` must advance with Grok still consulted. Upgrade the **producing Mac** for delivery and the **rendering Mac** for tables and wording. Run `mm install-skills` and restart the **agent** so it loads the updated decoder.

If Grok writes a record this version cannot read, that Mac drops Grok (declared on `mm status` / `mm diag` / push stderr) and keeps publishing Codex. A newer mm may read it: run `pipx upgrade mind-meld`, or `mm disable-source grok` to stop retrying. See [Host usage capture](#host-usage-capture-codex-and-grok) for all blockers.

Then give each agent the same lifecycle contract. For Codex, add this to `~/.codex/AGENTS.md` (or merge it into your existing global guidance):

```markdown
# Mind Meld

At the start of each conversation, run `mm autopull`.
At the end of each completed task or conversation, run `mm autopush`.

If either command has output, summarize it for the user. If it is silent, continue silently.
```

This makes every agent feed the same gstack and `mm-events` history used by `retro-fleet`; Claude-only session and token breakdowns remain explicitly labeled as Claude-only.

### Manual commands

| Command | Description |
|---------|-------------|
| `mm --version` | Print the installed version and exit |
| `mm init` | Configure device, storage path, passphrase |
| `mm push` | Sync content and refresh consented host usage. Exit 0 means content sync succeeded regardless of capture outcome; see [capture outcomes](#host-usage-capture-codex-and-grok) |
| `mm push --dry-run` | Preview publication and deletions; changes nothing except the local lock file |
| `mm recapture [WINDOW]` | Redo this Mac's git capture for WINDOW (default `30d`, same as `mm init`). Safe to re-run — commits dedup fleet-wide on (remote, sha). Restores omitted commits; cannot remove rows already filed under a wrong remote. Partial recovery exits 4. Retros window by the COMMIT's date, not by when mm captured it |
| `mm pull` | Pull with verbose output |
| `mm pull --conflict-mode prompt` | Pick a winner per-file at pull time instead of auto keep-both |
| `mm pull --conflict-mode fail` | Exit 3 before applying any file if conflicts or local failures are predicted. Write-free CI gate: `mm pull --dry-run --conflict-mode fail` (0 no conflicts predicted; 1 stopped; 3 conflicts/failures predicted) |
| `mm status` | Show local vs remote state, plus the last `autopull` / `autopush` breadcrumb — flagged `stale` when nothing has auto-run in 48h. Prints one extra line when a `retro-fleet` skill link is broken |
| `mm diag` | Dump non-secret crypto, sync, breadcrumb, `retro-fleet` skill-link state, Grok `host_skill_discovery`, host-usage reader state (`host_usage`), git-root `discovery`, and recorded-vs-fresh `git_capture` for triage. Runs without a passphrase, and without a valid config. `--json` for machine-readable output. Top-level keys: `mm_version`, `config`, `crypto_init`, `root_salt_drift`, `sidecar`, `storage_inventory`, `last_autorun`, `skill_links`, `host_skill_discovery`, `host_usage`, `host_read_budgets`, `host_publication`, `discovery`, `git_capture`. Each `skill_links` row includes `maintain_links` (`enabled` / `disabled (…)` / `unknown (config invalid: …)` / `unknown (policy not resolved)`). `host_skill_discovery` is not a skill-link row. |
| `mm devices` | List registered devices |
| `mm devices --format=json` | Same data as a JSON array on stdout — for scripting (used by `/retro-fleet`) |
| `mm diff` | Compare local files with this Mac’s last push (or `--from DEVICE`). Changes nothing. For incoming changes, use `mm pull --dry-run` |
| `mm gc` | Delete orphaned blobs and run local retention cleanup |
| `mm gc --dry-run` | Preview orphan blobs plus temporary, events, and token-cache retention cleanup without deleting; each reaper reports candidates and any repairs or skips |
| `mm gc --conflicts` | Also delete redundant `.sync-conflict-*` copies older than 30 days. Live conflicts are never reaped at any age: a sidecar whose canonical file differs, or is missing (the resolver still offers `(p)romote`), or cannot be hashed, is preserved |
| `mm sources` | List configured sync sources |
| `mm log` | Query the per-file pull/push history. Filter with `--source`, `--since`, `--action {written\|merged\|skipped\|conflicted\|excluded\|uploaded\|failed}`, `--verb {pull\|push}`, `--limit`; `--format {jsonl\|table}` |
| `mm migrate-config` | Append any missing recommended `exclude_patterns` to your existing `[[sync.sources]]` entries. Idempotent and preserves your customizations; `--dry-run` to preview, `--yes` to skip the prompt |
| `mm refresh-identity` | Force-refresh the cached author-email set that decides which fleet commits count as yours. Shows `+`/`-` changes when the previous cache is readable. `--json` prints only the resolved set |
| `mm conflicts` | List unresolved `.sync-conflict-*` files with age and canonical sibling |
| `mm resolve [PATH]` | Interactively pick a winner for conflict files (shows unified diff). Exits 1 if any per-conflict rename/unlink/read fails so CI / scripts can detect partial failure (the walk still continues through every conflict). |
| `mm retro-fleet [WINDOW]` | Render the fleet retrospective markdown to stdout (default `7d`). The `/retro-fleet` Claude Code skill calls this under the hood; safe to run directly for scripted exports (`mm retro-fleet 30d > /tmp/retro.md`). `--no-author-filter` renders every fleet commit instead of just yours. `--dump-host-usage` prints forensic JSON of accepted host inventory (family totals, per-model `tokens_by_day`, a detail status per device, and coverage fields `degraded` / `partial` with the reason a coverage field was dropped) and skips the markdown retro. |
| `mm install-skills` | Force-check the `retro-fleet` skill link for every *authorized* agent and report every agent's outcome, including skipped (declined) rows. Creates missing links, repairs dangling ones, and re-points links left over from an old install onto the store at `~/.local/share/mind-meld/agent-skills/retro-fleet/`. A file of your own, or a link to somewhere Mind Meld does not recognize, is never overwritten — it is reported with the cause and the fix. `--agent KEY` (repeatable) grants and persists skill-link maintenance for that agent without enabling sync or usage reading, then installs every authorized agent. Bare invocation with no config is fresh-machine setup (install all available); with a config it honors `[skills]`. Restart the agent afterwards so it reloads SKILL.md. |

### Syncing gstack

If `~/.gstack` is detected during `mm init`, it is automatically added as a sync source. gstack uses a **whitelist walker** — unlike the Claude source (which has hardcoded subdirs), the gstack source only syncs the directories and files you explicitly list.

**Defaults out of the box:**

- `include_dirs`: `projects/`, `analytics/`, `retros/`
- `include_files`: `retro-context.md`, `greptile-history.md`, `.completeness-intro-seen`, `.telemetry-prompted`, `.proactive-prompted`, `.welcome-seen`, `.codex-desc-healed`
- `exclude_patterns`: `config.yaml`, `projects/*/repo-mode.json`, `projects/*/land-deploy-confirmed`, `analytics/.last-sync-*`, `projects/*/decisions.active.json`, `projects/*/brain-cache/*` (per-machine artifacts that churn-conflict on every pull — `config.yaml` holds gstack's version-check tracking; `analytics/.last-sync-*` are per-machine cursor files tracking each device's progress through gstack's local analytics jsonls; `decisions.active.json` is a derived one-line snapshot of `decisions.jsonl`, which **does** sync and merges cleanly, and gstack rebuilds the snapshot from it on demand; `brain-cache/` is gbrain's per-machine cache)

This covers the common cross-machine cases — in particular, `/retro global` sees activity from all your Macs because `analytics/skill-usage.jsonl`, `analytics/eureka.jsonl`, and `projects/<slug>/timeline.jsonl` are all `.jsonl` files that **set-union merge** on pull (deduped; string `ts` first, then full-line lexical fallback). Append-only telemetry from 3 machines converges cleanly into one timeline.

**Not synced by default** (machine-local by design): `sessions/`, `sidebar-sessions/`, `slug-cache/`, `worktrees/`, `builder-profile.jsonl`, `developer-profile.json`. If you want any of these on every Mac, add them to your config (see below).

**Adding files or dirs:** edit `~/.config/mind-meld/config.toml` and extend the gstack source. For example, to sync the writing-style prompt marker and a custom notes file:

```toml
[[sync.sources]]
name = "gstack"
path = "~/.gstack"
type = "generic"
include_dirs = ["projects", "analytics", "retros"]
include_files = [
    "retro-context.md",
    "greptile-history.md",
    ".completeness-intro-seen",
    ".telemetry-prompted",
    ".proactive-prompted",
    ".welcome-seen",
    ".codex-desc-healed",
    ".writing-style-prompted",   # added (your custom extra)
]
exclude_patterns = [
    "config.yaml",
    "projects/*/repo-mode.json",
    "projects/*/land-deploy-confirmed",
]
```

Supplying `sync.sources` replaces the defaults wholesale — copy the full list, don't just add your extras. Run `mm sources` to confirm the resolved source list.

**Useful flags:**

- `mm sources` — show the configured source list with their `Enabled` state and file counts.
- `mm pull --source gstack` — pull only the gstack source (skip Claude).

### Syncing gstack-extend

If `~/.gstack-extend/` is detected during `mm init`, it is automatically added as a sync source — sibling of the `gstack` treatment above. The whitelist walker is scoped to `projects/` only; per-machine bookkeeping at the root (`config`, `just-upgraded-from`, `update-snoozed`) is excluded by construction. Anything `gstack-extend` skills (pair-review, test-plan, full-review) persist under `~/.gstack-extend/projects/<slug>/` rides this same source so cross-machine resume keeps working as the gstack-extend feature surface grows.

Existing installs see this as a `New source available: gstack-extend` hint on next `mm status`. Opt in with `mm enable-source gstack-extend` or dismiss with `mm disable-source gstack-extend` — same shape as every other source toggle.

## Disabling sources per machine

`config.toml` lives at `~/.config/mind-meld/` and is never synced — making it the natural home for per-device preferences. To turn off a source on one machine without affecting the others:

```bash
mm disable-source gstack       # this Mac only; iCloud peers untouched
mm enable-source gstack        # turn it back on
```

The on/off state lives in `[sync].disabled_sources = ["gstack"]`. Disabling does NOT delete your `[[sync.sources]]` entry — re-enabling preserves any customizations like `include_dirs` or `exclude_patterns`. For a supported agent, disabling its source removes the **derived** skill-link grant. It does not override an explicit `[skills] agents` list; edit that list to revoke maintenance. `gstack` has no agent skill-link row.

`mm sources` shows the toggle state as an `Enabled` column. `mm status` calls out disabled sources in a one-line breadcrumb so future-you doesn't forget gstack is off and re-debug "why isn't this syncing". Neither `mm sources` nor `mm status` shows a `[skills] agents` override — `mm diag` is the authoritative resolved view of skill-link policy.

**Forward-compat for not-yet-shipped sources.** When `mm` adds a new source to its defaults, upgraders don't get auto-enrolled — `mm status` surfaces a one-shot enable hint. To pre-disable a name before it ships:

```bash
mm disable-source future-agent --force   # accepts unknown names for forward-compat
```

`mm reconfigure-sources` re-runs the picker against your current config + new defaults, in case you want to revisit every choice at once. Changing which sources are enabled also changes derived skill-link consent on this machine.

## Managing agent skill links per machine

Skill-link maintenance is per-machine and never synced (`config.toml` lives at `~/.config/mind-meld/`). By default mm maintains a `retro-fleet` link for each agent whose sync source is enabled, using the same source bit as host-usage reads.

```toml
[skills]
maintain_links = true          # false disables every row
# agents = ["claude", "codex"] # when present, an exhaustive allowlist
```

1. `maintain_links = false` disables every row.
2. Omitting `agents` derives consent from enabled sources.
3. Providing `agents` creates an exhaustive allowlist and suppresses source derivation, the same way an explicit `[[sync.sources]]` suppresses `DEFAULT_SOURCES` auto-detect.
4. `agents = []` is invalid; use `maintain_links = false` to turn maintenance off. Two encodings of one result is a bug farm.
5. `mm install-skills --agent KEY` turns `maintain_links` back on and preserves prior derived grants (`agents =` the set you already had, plus KEY), then installs every authorized agent — not only KEY. It does not enable source sync or usage reading.
6. Declining does not remove an existing link. **Unmaintained is not dead:** a declined agent's link keeps resolving and that agent keeps offering `/retro-fleet`, because store publish is not consent-gated (rule 7). What you give up is repair — clobber that link or leave it dangling after a future store change, and `mm` will not rebuild it; the agent loses the skill silently. **To actually remove a link, delete it** — since v0.12.44 `mm` leaves a deleted link deleted (see [Removing a skill link](#removing-a-skill-link)), so you do not need to touch this config at all. With source-derived policy, `mm disable-source KEY` also removes the grant and drops sync and usage reading.
7. The mm-owned store at `~/.local/share/mind-meld/agent-skills/` keeps refreshing while any link still points at it, maintained or not. That is deliberate: a surviving link must never resolve to a stale `SKILL.md` just because its row was declined. `mm` will not, however, *create* a store on an all-declined machine that has none.
8. The config is per-machine and never synced.
9. An explicit `agents` list will not automatically include future agents.

`mm diag` is the authoritative resolved view. `mm sources` structurally cannot show a `[skills] agents` override.

Grok is not a skill-link row. Verified 2026-08-24: Grok 1.0.5 discovers `~/.claude/skills` at the same documented priority tier as `~/.grok/skills` (`grok inspect --json`, then `name == "retro-fleet"`). Codex shows no evidence of that discovery (`~/.codex/AGENTS.md` mentions are the user's own instruction prose — and gstack's `setup --host auto` plus `machine-setup` *copy into* that dir, which you would not do for a dir the host already reads). Exit criterion: **mm maintains a skill link only for hosts that do not discover `~/.claude/skills`.** Re-check with `grok inspect --json`.

Two Grok levers, and they are not interchangeable:

- `[skills] ignore` (Grok README, skills config) is the documented lever that *breaks* discovery of `~/.claude/skills`.
- `[skills] paths = ["~/.local/share/mind-meld/agent-skills"]` is the remedy that *adds* the mm-owned store as an extra search path.
- The Claude-compat-off toggle itself is **undocumented** in Grok 1.0.5 — a live `~/.grok/config.toml` can carry `[compat.claude] hooks = false`, and that key appears nowhere in Grok's 109KB README — so `grok inspect --json` → `externalCompat` is the only reliable read. `mm diag --json` reports that under `host_skill_discovery`, never under `skill_links`. `skill_links` is links mm owns and maintains; `host_skill_discovery` is compatibility behaviour owned by a host.

### Removing a skill link

Delete it. Since v0.12.44 `mm` leaves a deleted link deleted.

```bash
rm ~/.codex/skills/retro-fleet
```

Restart the agent so it drops `/retro-fleet` from the session it already loaded.

That is the whole procedure — there is no `mm uninstall-skills`, and you do not
need to edit `config.toml`. Earlier versions treated a missing link as damage
and rebuilt it on the next interactive `mm push`, which is why removing one used
to require a config edit as well.

`mm` still repairs a link that is *present and wrong* — dangling after a store
move, or pointing at an old package path. Only an absent link counts as your
decision, and only for an agent `mm` has installed for before. Confirm with
`mm diag`: the row reads `status: removed-by-user`, distinct from `absent`
(that agent never had one).

Changed your mind:

```bash
mm install-skills          # rebuilds every authorized agent's link
```

`mm init` rebuilds them too — both are explicit acts, and only an unattended
`mm push` respects the deletion.

This is per-machine. `config.toml` is never synced, and deleting a link on one
Mac does not touch the others.

**One trade-off worth knowing:** if an agent app wipes its own skills directory,
`mm` no longer silently rebuilds the link — run `mm install-skills`. Making
deletion stick and healing an agent's self-inflicted wipe are the same
filesystem state, and deletion is the one you do on purpose.

## Fleet retro (`/retro-fleet`)

Mind Meld v0.11.0 ships a Claude Code skill that stitches engineering activity from every Mac in your fleet into one accurate retrospective. Every substantive `mm push` writes a per-device daily JSONL row (commit metadata, sessions count, sync activity) to the synced `mm-events` source, so any machine can read the union and produce a fleet-wide picture. `mm recapture` writes extra git-snapshot rows (not an mm-push) to recover omitted intervals, then runs an ordinary push. For skipped repositories and environment overrides, see [Git capture troubleshooting](#dropped-repositories-and-ignored-git-environment-variables).

Inside Claude Code:

```text
/retro-fleet 7d     # last week, default if you omit the window
/retro-fleet 30d    # last month
/retro-fleet 90d    # last quarter (the retention ceiling)
```

The skill renders a paste-ready markdown retro — drop it into iMessage, Slack, or email. Commits are deduped across machines via `(canonical remote URL, sha)` so the same PR landed once but pushed from two laptops counts as one.

Requires **mm v1.1.0+** on the rendering Mac (below that the aggregator emits
the pre-1.1 sections and no `MM_HEALTH` block). Peers only need v0.14.17+ to
publish a usable capture.

**v1.1 output shape.** A pixel-aligned ASCII card sits at the top, then a short markdown body: `## Code shipped` (volume, top repos, peak hours, ship-of-the-window, weekly buckets when window ≥14d), `## Agents`, `## Skills used`, `## Fleet`. The card carries volume, LOC, an `AGENTS` block, then NOTEWORTHY and up to three TOP WORK bullets the skill synthesizes.

**Every agent is reported identically** — fleet-summed tokens, active days, contributing machines, estimated cost, top model:

```text
| Agent  | Tokens | Days | Machines | Est. cost | Top model          |
|--------|-------:|-----:|---------:|----------:|--------------------|
| Claude |   8.0B |   29 |        2 |   ~$5,590 | Opus 5 (4.8B)      |
| Codex  |   3.3B |   28 |        2 |   ~$2,239 | gpt-5.6-terra (2.0B) |
| Grok   |   1.0B |   11 |        1 |     ≥$546 | grok-4.6-build (830.8M) |
```

Comparing the rows is the point: they share a unit, a window, and a counter basis. Host agents require `mm enable-source codex` (or `grok`) on each machine — that opt-in is also what authorizes the local usage reader — and a `mm push` afterwards. An absent row means unobserved, never zero.

Days are a set union across machines and can only understate. Token sums can double-count a migrated home directory carrying two device ids with overlapping ledger history. mm flags identical host-ledger day counters as possible overlap through health code `duplicate_ledger`; matching totals alone are not proof, so inspect the machines before retiring one. This detector covers host ledgers, including their Claude models; it does not establish that Claude Code session histories are disjoint. Before v1.1 this hazard was handled by refusing to sum host tokens at all, while summing Claude's under the identical risk.

The card is generated via a two-pass flow: the first invocation emits an `MM_THEMES_PROMPT` JSON sidecar, the skill synthesizes themes + noteworthy, then re-invokes `mm retro-fleet <window> --theme … --noteworthy … --name …` to render the final card. `## Trends vs prior <N>d` is a two-column table computed from the synced events corpus (the immediately preceding equal-length window), identical in both passes, and fleet-deterministic — it does not depend on when you last typed the command. Direct CLI users (no skill) get the body without the card. The window argument is `Nd` — `7d`, not `7`.

Under the hood the skill invokes `mm retro-fleet <window>` (v0.11.22+) — the same CLI surface is available directly for scripted exports (`mm retro-fleet 30d > /tmp/retro.md`) or terminal use, just without the LLM judgment layer the skill adds (natural-language window parsing, error translation). The earlier `python -m mind_meld.skills.retro_fleet.aggregator` form is a development-checkout fallback only; pipx-installed mm lives in an isolated venv that bare `python` / `python3` can't import from, so the skill's documented invocation routes through the `mm` console-script (always on PATH wherever mm is installed).

**Token usage and cost (v0.11.14, hosts in v0.12.52, unified in v1.1).** The `## Agents` table answers: how much did each agent consume this window, on which model, and what would that have cost at API list rates. Claude's numbers come from `~/.claude/projects/<encoded>/*.jsonl` plus subagent jsonls under `<session-uuid>/subagents/agent-*.jsonl` (subagents contribute to the parent project's totals — ~50% of usage on a heavy fleet — but don't double-count as separate sessions). The Claude cache lives at `~/.config/mind-meld/session-tokens.json`, warms inline on `mm init` and the first interactive `mm push` (~3 seconds, telegraphed via `mm: warming token cache (one-time, ~3s)...`), and is reaped by `mm gc` once a jsonl disappears or its tokens are older than 90 days.

Cost is not subscription spend: historical tokens are repriced at the rates bundled with this mm release, verified on the dates below. Grok always contributes a base-tier floor because its logs lack per-request prompt sizes. All four token fields (input, cache write, cache read, output) contribute to the Tokens column.

Partial or failed readers conservatively floor every agent they might supply, including agents with no data from that machine: snapshots do not identify which reader supplied each model. Unusable coverage metadata (a reason with an empty source list) does the same, even when that machine recorded no in-window usage. Grok's prompt-size caveat applies only to Grok. Each floor names its causes in `MM_HEALTH` under `cost_floor`; unavailable costs use `cost_unavailable`.

Legend:

- `~` is an estimate; it may use a family-extrapolated rate, named in `MM_HEALTH`.
- `≥` is a floor of the priced subtotal under bundled rate assumptions,
  never a guaranteed billing minimum. Causes include unpriced models,
  incomplete coverage, dropped readers, unattributed tokens and an unknown
  long-context tier, and are named per agent in `MM_HEALTH`.
- `—` is unavailable, not zero.

An agent with no priceable model shows `—`, never a confident `$0`. A priced model stored at zero tokens is not a priced basis, so it cannot turn that row into `$0` either. A snapshot predating the window contributes nothing at all. A Mac on mm older than v0.12.52 reported inclusive counters that would read up to ~2x high, so any agent it contributes to shows `—` until it upgrades and republishes — the one caveat that points the wrong way, and so the one that stays a rendered marker. Grok's cost causes may include an at-most figure for **this model's recorded tokens, in token charges; server-side tool fees excluded**. Any partial/degraded reader or nonzero model cache writes suppress that figure.

`--dump-host-usage` carries the per-machine inputs (`tokens_by_day` and coverage); the rate table is bundled with mm and is not in the dump.

**Rate provenance.** In `token_usage.py`: Anthropic `PRICING_LAST_UPDATED` = 2026-09-14 ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)); OpenAI `PRICING_OPENAI_LAST_UPDATED` = 2026-09-10 ([Standard pricing](https://developers.openai.com/api/docs/pricing)); xAI `PRICING_XAI_LAST_UPDATED` = 2026-09-10 ([grok-4.6 rates](https://docs.x.ai/developers/models/grok-4.6), [Build model identity](https://docs.x.ai/build/overview)). mm has no network, so a rate change is a code change.

Sonnet 5 now uses the standard $2/$10 input/output rates; Sonnet 4.x
stays at $3/$15. Fable/Mythos 5.1 cache reads are $0.25 per MTok; 5.0 stays
at $1.00. Anthropic estimates assume 1h cache writes (2x input), while floors
use 5m writes (1.25x); the wire does not distinguish TTLs. Verified model ids
are recorded separately from the family fallback, which still prices new ids
but names them as extrapolated in `MM_HEALTH`.

Fast-mode turns on Opus 5 / 4.8 bill at 2x and are priced here at standard rates.
The 2026-09-14 local census found 0 fast rows in 22,042 carrying `speed`, and
4,297 rows lacking the field. Absence does not establish chronology. No detector,
cache field or wire flag ships here; fleet exposure cannot be inferred.

**Terminal recipe** (run each command separately):

```bash
mm pull
mm push
mm retro-fleet 7d
```

Pull refreshes peer snapshots; attended push refreshes this Mac's capture.
Direct rendering reads available snapshots and skips the skill's refresh step.
Host logs hold at most 90 active UTC days and can lose old records; observation
dates are endpoints, not proof of continuous coverage. A stale snapshot
contributes nothing to the window, even on its first UTC day.

**Data health.** Degradations do not clutter the report. The body carries one
line (`_Data health: N items worth knowing about — ask me to diagnose._`) and
the detail — every issue, with its remedy — rides a machine-readable
`MM_HEALTH` JSON block that the skill reads and summarizes with judgment.
Machines are named by hostname there, not by device id.

Upgrade roles are separate: upgrade the **producer** and republish for reader
or counter fixes; upgrade the **renderer** for new rates and presentation (old
snapshots are repriced locally); refresh the **decoder** with `mm install-skills`
and restart the agent. `--dump-host-usage` inspects host inputs; inspect
`~/.config/mind-meld/session-tokens.json` for local Claude token inputs.

**Adding an alias or refreshing a rate.** Exact observed model id → `PRICING_FAMILY_BY_MODEL` (never a substring). Family → literal four-field card in `VENDOR_FAMILY_TIERS` (do not use `_tier`; those multipliers are Anthropic). Refresh the matching `PRICING_*_LAST_UPDATED` in the same commit. `resolve_prices` is the only "is this priced" predicate; a test fails the build if an alias points at a missing tier.

Session jsonls only ever grow, so from v0.12.15 each push re-reads only the bytes appended since the last one rather than the whole file. That's what stopped `mm push` periodically printing `mm: notice: events tail budget exceeded` on machines with a lot of large sessions. If your token cache has gone stale from long-deleted workspaces, `mm gc` reaps those entries and shrinks what every push has to read.

**Where the skill lives (v0.12.38).** `mm` copies `SKILL.md` into a store it owns at `~/.local/share/mind-meld/agent-skills/retro-fleet/`, then points every supported agent's `retro-fleet` link at that one constant path (Claude Code's is `~/.claude/skills/retro-fleet`; `mm diag` lists them all). The store is machine-local and never synced. Before v0.12.38 each link pointed straight into whichever Python installation ran `mm`, so deleting a Conductor workspace or bumping a Homebrew Python took the skill offline with a dangling link and no way to repair it. The link target no longer moves. The store refreshes on a version-then-hash compare, so a new `SKILL.md` lands on the next `mm init`, `mm push`, or `mm install-skills` after a `pipx upgrade` — not in place during the upgrade itself. Restart the agent afterwards so it reloads SKILL.md.

The link is created on `mm init` and re-checked by each push **for agents authorized by `[skills]`**: derived from enabled sources when `agents` is omitted, or from that explicit allowlist when it is present. The check is 24h-TTL gated, a handful of syscalls per available consented agent in steady state. Each agent is tracked separately, so a problem on one never suppresses checks for another. Mind Meld repairs its own links without being asked: a link that points at the store but dangles, and a leftover link into an old package or checkout that no longer resolves, are both re-pointed. A link that is simply **gone** is left gone wherever `mm` has resolved that target before: since v0.12.44 push reads an absent link as your decision, not as damage (see [Removing a skill link](#removing-a-skill-link)). `mm init` and `mm install-skills` still put it back, and an agent `mm` has never installed for still gets its link on the next push. A *live* checkout link — the development dogfood case — is left alone by push, and re-pointed by `mm init` or `mm install-skills`. A file of your own, or a symlink to somewhere Mind Meld does not recognize, is never touched: `mm diag` shows it with the `readlink` output and `mm install-skills` names the cause and the fix. A declined row whose mm-owned link still **resolves** is `status: ok` with `maintain_links: disabled` — `mm status` does not nag about a deliberate decline. Background `mm autopush` classifies and warns but never rewrites agent config — run an interactive `mm push` or `mm install-skills` to actually repair a link, then restart the agent so it reloads SKILL.md.

**Caveats the output is honest about:**

- Asking for a window longer than 90 days surfaces a tail breadcrumb — `mm gc` reaps event files older than `EVENTS_RETENTION_DAYS` (90), so that's the data ceiling.
- Peers still on pre-v0.11.0 emit the older v=1 sessions-snapshot schema (delta semantics). The aggregator omits their session counts honestly rather than overcounting; you'll see `Sessions count incomplete: peer X is on pre-v0.11.0` until they upgrade.
- Devices that haven't pushed during the window are flagged as fleet-incomplete instead of silently dropped.

To filter to your own commits only, the skill consults `git config --global user.email` plus any `[retro].author_emails` aliases in `~/.config/mind-meld/config.toml`. Pass `--no-author-filter` to render every fleet commit. To override the events directory (custom `mm-events` path), set `MM_EVENTS_DIR=/path/to/events` before invoking.

**Pin repositories this Mac should capture** with `[retro] repo_roots` in `~/.config/mind-meld/config.toml`. Paths must be absolute (or start with `~`). The setting is per-machine and is not synced. Manual roots are classified before automatic Claude project discovery, and they are the *only* discovery mechanism on a machine where the `claude` source is disabled. Verify with `mm diag`:

```toml
[retro]
repo_roots = [
  "/Users/you/src/mind-meld",
  "/Users/you/src/bolt",
]
```

Historical capture gaps can be repaired on the Mac that owns the repositories: `mm recapture 7d`. Recapture scans local Git history and syncs new snapshot rows. It does not change commit dates: a commit from June appears only in a retro window containing June. Verify a recovery by rendering the same window — `mm recapture 90d` then `mm retro-fleet 90d`. Repeat on each Mac that owns missing repositories. `_coverage_floor_from_files` still uses the event filename date, so `## Trends vs prior Nd` can stay "unavailable" on a young machine even after a successful recovery — that is correct by design.

Discovery is local. Track 29A's live gap was one repo (8 commits, 15%), not "10 repositories, the gap is structural": five of those ten Conductor workspace entries were symlinks that `discover_git_roots` already dedups via `candidate.resolve()`. Do not add machine-specific paths to repo defaults; pin them in `[retro] repo_roots` on that Mac.

## Handling conflicts

For a divergent file that cannot auto-merge, the default `mm pull --conflict-mode keep-both` leaves your local file at the canonical path. It follows the Syncthing convention: your local file stays where it is, and the incoming remote version is saved as `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-v1-<device>.<ext>` sitting next to it. If your local file is newer than the remote (by mtime), pull leaves it alone — convergence happens on the next push.

Managed sidecars are latest-per-peer, not an archive. Unchanged peer content reuses the existing sidecar without cleaning up extras. Changed content publishes a new sidecar first, then removes prior same-peer copies for that file whose contents it could read and verify as different. Unreadable copies remain. If publication fails, local and prior copies remain. If only cleanup fails, the new copy is already saved and extras remain — `mm conflicts` / `mm resolve` can inspect them. Edits made inside a managed sidecar are subject to that replacement; promote or copy the sidecar out of managed naming to keep a durable document.

Sidecars stay local-only. A canonical or promoted file syncs only when the source's include rules cover it. Deleting a sidecar on this Mac does not delete anything on other machines. If a sidecar has no recoverable original filename, `mm resolve` cannot promote it automatically; copy or rename it manually to a name you choose.

Managing conflicts:

- `mm conflicts` — list every `.sync-conflict-*` file across your sources, with conflict age (when mm wrote the copy), peer-edit age (when the other Mac last saved the file), and canonical sibling.
- `mm resolve` — walk each conflict interactively. Shows color LOCAL/REMOTE banners (with peer-name attribution when the conflict file's device prefix matches a registered peer), created/modified timestamps for each side plus a `-> SIDE is newer by N` recency verdict, a 3-number divergence summary, the unified diff, and prompts: `(m)erge` (accept LCS-merged result) / `(l)ocal` (keep your edits) / `(r)emote` (overwrite with peer's bytes) / `(n)ewer` (keep whichever was modified more recently) / `(p)romote` (keep BOTH — give the conflict file its own first-class filename) / `(s)kip` (leave both files) / `(a)bort` (stop the walk). The default key is always `(s)kip` — Enter never auto-accepts a merge or a recency guess. The merge uses LCS(local, remote) as a synthetic ancestor so additive edits on either side land cleanly; same-region edits show as `<<<<<<<` markers and (m) stays available. Binary content suppresses (m); `(n)ewer` is offered only when both sides' mtimes are readable and re-prompts on an exact tie (it never guesses). The remote side's "created" is shown as `pulled` — it is the local sync time, not the peer's real creation (the manifest carries only modified time). Acquires the mm lockfile so autopull can't race your decision. `c` / `f` are rejected with exit 1; other unrecognized input, including `b` / `both` (alias removed in 1.0.0), skips silently.
- `mm pull --conflict-mode prompt` — prompt per-conflict during the pull itself instead of auto keep-both. Shows the same per-side timestamps + recency verdict (display only — no `(n)ewer` shortcut here, since pull already keeps your file when it is the newer one).
- `mm pull --conflict-mode fail` — preflight and exit 3 before applying any file if conflicts or local failures are predicted. For a write-free CI check use `mm pull --dry-run --conflict-mode fail`: 0 no conflicts predicted, 1 stopped, 3 conflicts/failures predicted (2 remains usage error). Two peers changing a mergeable file do not by themselves cause a conflict.
- `mm gc --conflicts` — reap redundant conflict copies older than 30 days. A sidecar is only reapable once the conflict has converged (canonical exists and its bytes are identical); a live conflict, a missing canonical, or a file mm cannot hash is preserved at any age.
- `mm diff` — compares local files with this Mac’s last push, or `--from DEVICE`. Use `mm pull --dry-run` to preview incoming changes from all selected peers.

## Troubleshooting

### Newer or damaged storage format

If mm reports a newer format, leave every `mm-crypto-init` copy in place and
upgrade this Mac with:

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@latest
```

`mm diag --json` reports the detected byte as `crypto_init.newer_version`;
null means no newer version was observed. Any newer canonical or conflict copy
blocks crypto sessions and repair. A newer blob that arrives first is skipped
with an upgrade warning; an unreadable peer manifest is skipped by pull and
blocks GC.

If this Mac is already on the latest release and no newer mm exists, the file
may be damaged. Wait for iCloud to finish syncing and locate a known-valid
crypto-init copy from another Mac or backup. Move the damaged copy aside to a
backup outside the storage folder, then let `mm pull` verify and reconcile the
valid copy. Keep the backup; never delete crypto-init or bootstrap a fresh salt
over existing encrypted data. If no valid copy is available, stop and recover
one before continuing.

**On mm older than v0.14.5, `mm pull` died with `TypeError: '<' not supported between instances of 'str' and 'int'` (or `bool` / `list` / `dict`), or an unattended `mm autopull` reported an unexpected error and a later file never arrived.** A `.jsonl` file mixed a string `ts` with a non-string value. Current `mm` keeps every unique normalized line and continues the pull. On **each** Mac: confirm with `mm --version`, then `pipx upgrade mind-meld` and `mm pull`. If `pipx upgrade` leaves you on an old version, the install is pinned to a frozen tag — see [Upgrading](#upgrading). `mm devices` shows the version at last push, not a live probe of the installed binary; after a successful `mm push` it can corroborate the fleet. Numeric-only timestamps now sort as whole-line text rather than numbers; until every active Mac is upgraded, old and new clients can keep rewriting that order. `mm log --action merged --limit 20` can show repeated merges, but a merged outcome alone does not prove timestamp oscillation.

### Pull incomplete / could not pull a file

One blocked file no longer stops later files, sources, or peers. Each apply warning on stderr names the peer, source, path, cause, and remedy. Completed files stay on disk and in `mm log`; failed files are retried on the next pull. `mm autopull` exits 0 and records `degraded: N file(s) failed`. Interactive `mm pull` also exits 0 for per-file failures; this collision previously crashed with exit 1. Read the warnings and `Pull incomplete:` summary to tell whether everything arrived.

```bash
mm log --verb pull --action failed --limit 10
mm pull --verbose
```

| Warning cause | What to do |
|---|---|
| A peer published a folder where this Mac has a file | Decide which layout you want. Keep your file and exclude that peer path, or move your file aside before retrying. The warning names the actual blocking ancestor, even for a deeply nested path. |
| Permission denied | Check write permission on the named parent folder, then `mm pull`. |
| Read-only filesystem | Restore writable access or choose a writable local source folder, then `mm pull`. |
| Disk full | Free disk space, then `mm pull`. |

For example, if the warning names `~/.claude/projects/-app/memory/bbb` as the blocking file, preserve it with a new name (choose an unused destination):

```bash
mv -n "$HOME/.claude/projects/-app/memory/bbb" "$HOME/.claude/projects/-app/memory/bbb.local"
mm pull
```

Use the real path from your filesystem: displayed nonprintable characters are visible notation, not a shell argument. `mm disable-source <name>` stops a whole source; a precise `exclude_patterns` glob under the existing source configuration can omit just the unwanted path. Neither option deletes your local file.

`Pull interrupted; completed changes were kept.` means an interrupt or unexpected error stopped the batch. Run `mm pull` to continue. Choosing `(a)bort` also keeps and records completed changes; pending keep-local mtime decisions are not broadcast. A `mm: notice:` saying a file was written, merged, or a conflict copy saved means publication completed and the named follow-up step encountered an error.

**Retro output is missing a block, unexpectedly empty, or older than expected.** Treat the missing data as unknown, not zero. Run `command -v mm` and `mm --version`; upgrade to v0.14.17+ before running `mm diag` and `mm push`. If `mm status` or `mm diag` shows an incomplete git capture, recover on that Mac with `mm recapture 30d`, then rerun the retro at a window that includes the recovered commit dates. If push prints an upgrade notice, run its command, then run `mm install-skills` (or `mm install-skills --agent KEY` if `mm diag` shows that agent as `maintain_links: disabled`), **restart the agent**, and rerun the retro. Bare `mm install-skills` skips agents not authorized by the current `[skills]` policy; by default that means sources you declined. If `mm push` fails, its error explains which local data was not refreshed. This cannot tell you whether the SKILL.md the agent loaded matches the store copy — only that the binary and the published store are what they are.

**Why is my host cost missing (`—` on the economics table)?** That Mac reported token counters in an older format (mm < v0.12.52), has not pushed per-model `tokens_by_day` yet (mm < v0.12.49), or its latest snapshot predates the requested window. On **that** Mac: `pipx upgrade mind-meld`, then `mm push`. Confirm capture and publication with `mm status`. Then re-run `mm retro-fleet 30d` here. An upgraded peer's retained 90 days generally **do** become priceable on repush. `—` is unavailable, not zero; do not add the other machines' figures to fill it in.

**I enabled Grok, but no Grok activity appears.** Check the standing blocker
and prior scan in `mm diag`, then follow [Host usage capture](#host-usage-capture-codex-and-grok).
Use the [second-Mac publication check](#grok-usage-in-fleet-retro); cache success alone does not prove that Grok reached the wire.

**Why is my Grok machine always `>=`?** Grok's logs do not record per-request prompt sizes; no action resolves this. Requests reaching 200k prompt tokens pay twice the base rates for all their tokens. Notes can bound that model's recorded token charges, but omit the at-most figure whenever any reader is partial/degraded or the model has cache writes. Never average the bound with the floor or present it as the machine's cost.

**Why grok-4.6 rates, not grok-build-0.1?** xAI's Build overview identifies grok-4.6 as the model powering Build. `grok-4.6-build` is the exact observed CLI id; `grok-build-0.1` is a different model and stays unpriced. Bare `grok-4.6` also stays unpriced until observed. `costUsdTicks` is never decoded.

**An unknown model is unpriced.** Upgrading mm on the machine that renders this report may price it; republishing does not add a rate; do not estimate.

**`mm` is not on PATH after install.** pipx puts console scripts in `~/.local/bin`. If a Homebrew-installed `mm` shadows it, `which -a mm` shows both — fix the PATH order rather than deleting either.

**`mm --version` reports an old version after `pipx upgrade`.** Your install is pinned to a frozen tag. See [Upgrading](#upgrading) for the one-line fix.

**`mm enable-source opencode` says the source is unknown.** The OpenCode sync source was retired in v0.12.55. The next interactive `mm push` or `mm pull` offers `mm migrate-config`, which removes the leftover `[[sync.sources]]` opencode block and records the name in `disabled_sources` so that push does not mint deletion tombstones. mm also removes the `~/.config/opencode/skills/retro-fleet` link it created (v0.13.0); a link you made yourself is left alone. To keep syncing that directory, give the source a name mm does not own — `[[sync.sources]]` accepts any name, so `name = "opencode-local"` with the same `path` and `include_dirs` syncs it as an ordinary generic source today, unchanged.

**`/retro-fleet` is missing from an agent, or the agent sees a dead skill entry.** Run `mm diag` — it prints one row per agent with the link's status, its `maintain_links` policy, plus its `readlink` target (or, when there is no link to read, the reason), and needs no passphrase and no valid config. `mm diag --json` top-level keys are `mm_version`, `config`, `crypto_init`, `root_salt_drift`, `sidecar`, `storage_inventory`, `last_autorun`, `skill_links`, `host_skill_discovery`, `host_usage`, `host_read_budgets`, `host_publication`, `discovery`, and `git_capture`. Each `skill_links` row carries `key`, `agent`, `target`, `store`, `store_state`, `status`, and `maintain_links`, plus `store_version` on rows that were diagnosed successfully — the defensive `status: "error"` row omits it, so read that field defensively. When the config cannot be parsed, `maintain_links` is `unknown (config invalid: …)`, never `disabled`. A bare diagnose with no policy set is `unknown (policy not resolved)`. `host_skill_discovery` (Grok only) carries `host`, `status`, `claude_skills_compat`, `retro_fleet_resolved`, `retro_fleet_path`, and `grok_version`. It is not a fourth `skill_links` row: a probe result is not a link mm owns. `status` there is one of `ok`, `binary-absent`, `timeout`, `nonzero-exit`, `malformed-json`, `unsupported-schema`. It does not carry the SKILL.md the agent loaded, the resolved `mm` path, or whether an upgrade is available. Then run `mm install-skills`, which creates missing links and repairs Mind Meld's own dangling ones **for authorized agents**, and restart the agent so it reloads SKILL.md. If `maintain_links` is disabled, use `mm install-skills --agent KEY` to grant maintenance without enabling sync. If the row says `removed-by-user`, you deleted that link and `mm` is leaving it deleted — `mm install-skills` puts it back (see [Removing a skill link](#removing-a-skill-link)); `absent` means that agent never had one. If the row says the link is a file, or points somewhere Mind Meld does not recognize, that entry is yours: move it aside first, then re-run. `mm status` prints a one-line nag (cause + fix + restart) whenever a link is in a state it can call broken, so you don't have to remember to check — but a *live* entry of your own, and a declined row whose link still resolves, are not those states, so use `mm diag` when the skill is present and simply isn't Mind Meld's. For Grok, read `host_skill_discovery`, not `skill_links`. If `claude_skills_compat` is false, `[skills] ignore` is the documented lever that breaks discovery; `[skills] paths = ["~/.local/share/mind-meld/agent-skills"]` adds the store. The compat-off toggle itself is undocumented in Grok 1.0.5 — trust `grok inspect --json` → `externalCompat`.

**`/retro-fleet` refuses because `mm` is missing, or the leftover skill still offers the command after uninstall.** A current skill stops before running the retro and names a PATH-order check; an older skill (or leftover links after uninstall) still errors mid-run with `mm: command not found`. The skill store outlives `mm` (see [Uninstalling](#uninstalling)). Either reinstall `mm` or remove the leftover links.

**An agent's link is no longer repaired, and you never changed anything.** Nothing is broken. `mm` created that agent's link before 0.12.42 gated link maintenance on `[skills]` policy, and your current policy does not authorize that agent — by default that means its sync source is not enabled. It applies only where the link at the target still `readlink`s to `~/.local/share/mind-meld/agent-skills/retro-fleet`, which is what proves `mm` created it under the old ungated policy.

The link keeps working. Store publish is *not* consent-gated, so `SKILL.md` refreshes for every link still pointing at the store regardless of policy, and the agent keeps offering `/retro-fleet`. What stops is repair: if that link is deleted, clobbered, or left dangling by a future store change, `mm` will not recreate it and the agent loses the skill silently. To keep it maintained, run `mm install-skills --agent KEY` (repeatable) — it grants link maintenance without enabling source sync or usage reading. To accept the decline, do nothing; the link works until something breaks it.

Versions 0.12.42 and 0.12.43 announced this once on stderr; v0.12.44 removed that notice, because a machine that skips those releases never receives it and this entry is the durable explanation. A leftover `~/.config/mind-meld/.skill-link-policy-v0.12.42` marker is inert and safe to ignore. `mm diag` is the authoritative resolved view: a declined row whose link still resolves reads `status: ok` with `maintain_links: disabled`, and that pair is a deliberate decline, not a fault.

**`mm status` says `stale — no autorun in Nh`.** Nothing has run `mm autopull` / `mm autopush` in 48 hours, so your agent's lifecycle hook is not firing. Check the `# Mind Meld` block is still in the global instructions file that agent actually reads.

**`mm status` shows a `degraded` breadcrumb.** Read its `detail`: `file(s) failed` means some content did not arrive — run `mm pull` to see the warnings and retry ([pull failures](#pull-incomplete--could-not-pull-a-file)). Corrupt peers or unknown sources also leave content incomplete; fsync failures mean completed writes may not survive a crash. Fleet-retro capture and host-usage snapshots are best-effort and never block content sync. If the detail mentions git repository discovery, run `mm diag`, then `mm recapture 30d` on that Mac — a later ordinary push does not recapture the omitted interval.

**`mm retro-fleet` under-counts commits, or `mm diag` shows `status: empty` / `exceeded`.** Discovery is local to each Mac. Upgrade that machine, then `mm recapture 30d` (ordinary substantive pushes capture going forward; `mm push` refreshes host usage). Recapture does not change commit dates: verify with `mm retro-fleet` at a window that includes those dates. To force a machine to include a repo Claude Code has no session for, add it to `[retro] repo_roots` (absolute paths) and verify with `mm diag`.

**`mm push` prints `events tail budget exceeded`.** Run `mm gc` to reap token-cache entries for sessions that no longer exist, which shrinks what every push has to read.

**A file came back after you deleted it.** Deletions are recorded as tombstones on the *next* successful push from the machine that deleted it. A tombstone suppresses restoration from peers that still advertise the file; it does not actively delete a copy that is already present. Push on the deleting Mac, then pull elsewhere.

**Conflicts you didn't expect.** `mm conflicts` lists them, `mm pull --dry-run` predicts incoming changes; `mm diff` compares against a stored snapshot, and `mm resolve` walks them interactively. See [Handling conflicts](#handling-conflicts).

**A warning mentions a suspicious storage file.** Mind Meld left that entry in place. Rejection is not proof of malicious content — an older passphrase, a leftover iCloud/Dropbox conflict copy, or an unrelated file whose name happens to match can all produce it. Leaving the entry may repeat the warning on the next scan. `storage.path` in `~/.config/mind-meld/config.toml` identifies the folder. Inspect the original entry in Finder and preserve any uncertain data; do not delete it from a diagnostic path. The path shown in the warning is display text, not a shell argument, and may differ from the on-disk spelling (nonprintable characters become visible notation, so some joined emoji spellings appear split).

### Dropped repositories and ignored git environment variables

`git walk dropped N repositories this push` means some repository history
was omitted. Content sync still proceeds. On the Mac that owns those repos:

```bash
mm recapture --dry-run
```

The preview names skipped paths and reasons: `git_error` (Git could not read
the repository), `timeout`, `budget_abort`, or `raised` (an unexpected walk
exception). `no commits yet (benign)` is an empty repository and needs no fix.
The preview is a fresh scan at interactive budgets over the requested window
(default 30d). A clean preview does not prove the earlier push was complete.

For `git_error`, check for a broken checkout or ownership mismatch. If Git
reports dubious ownership and you trust that specific repository, substitute
its path in this command:

```bash
git config --global --add safe.directory /path/to/repo
```

Then rerun the preview and recover the omitted window:

```bash
mm recapture --dry-run
mm recapture 30d
mm refresh-identity
```

Use the same `Nd` window (1d–90d) for preview and recovery when changing it.
Budget aborts may need a narrower window. Git failures need their underlying
cause fixed; narrowing the window cannot repair a checkout. `refresh-identity`
lists the locally resolved emails and `+`/`-` changes versus a readable prior
cache. A missing cache means the previous set is unknown, not empty.

**Ignored Git environment.** mm's four Git reads select each discovered
repository even when a parent hook or shell exports repository-local state.
They ignore GIT_DIR, GIT_WORK_TREE, GIT_COMMON_DIR, GIT_INDEX_FILE,
GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES, GIT_IMPLICIT_WORK_TREE,
GIT_GRAFT_FILE, GIT_NO_REPLACE_OBJECTS, GIT_REPLACE_REF_BASE, GIT_PREFIX,
GIT_SHALLOW_FILE, GIT_CONFIG, GIT_CONFIG_PARAMETERS, GIT_CONFIG_COUNT, and all
GIT_CONFIG_KEY_*/GIT_CONFIG_VALUE_* entries: the list
`git rev-parse --local-env-vars` prints, plus the numbered configuration entries.

Repository and global configuration define attribution. Inherited `git -c`
and repository-local GIT_CONFIG overrides do not. ~/.gitconfig,
GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM, GIT_CONFIG_NOSYSTEM, XDG_CONFIG_HOME,
HOME, and `includeIf` still work, as do PATH, GIT_EXEC_PATH and GIT_SSH*.
Put lasting overrides in the appropriate configuration file. LC_ALL and
LANGUAGE are C for these subprocesses, so a localized no-commits message
cannot be misclassified as `git_error`.

**Recovery has three separate outcomes.** The local identity cache refreshes
on the next identity read that completes discovery after upgrading. Future
capture uses the correct repository and configured identities. Already
published rows remain unchanged: recapture restores omissions but cannot
retract a wrong remote or identity. Event files become eligible for `mm gc`
after 90 days by their filename date; GC also runs after an interactive
`mm push` that published user-content changes, never from autopush. Upgrading does not
claim to repair historical attribution.

<a id="host-usage-capture-codex-and-grok"></a>

### Host usage capture (Codex, Grok and Cursor via Conductor)

Every attended `mm push` refreshes and publishes consented host usage, including
when user files are already in sync. This requires **mm v0.14.17+ on the producing
Mac**. Older versions can report a successful `Nothing to push` without refreshing
usage. Upgrade, confirm the version, push, then verify:

```sh
pipx upgrade mind-meld
mm --version
mm push
mm status
mm diag
```

If `mm --version` is still below v0.14.17, a tag-pinned pipx install may not have
moved. Use the [upgrade instructions](#upgrading), verify again, then run `mm push`.
In status, check a recent **last recorded capture**, the expected reader coverage
and `Publication: published`. **Exit 0 means content sync succeeded regardless of
capture outcome**. On v0.14.18+, status also shows the **last recorded attended
attempt**, independently of an earlier capture that remains visible after a failed
refresh. Diag exposes the same evidence under `host_publication`, without token
magnitudes or payloads. Local publication evidence proves acceptance by this Mac's
storage backend; it does not prove delivery to another Mac.

Capture requires enabled, available `mm-events` and consented readers. Enable
with `mm enable-source mm-events` and `mm enable-source codex` / `mm enable-source grok`;
Grok also accepts the existing `[retro] grok_host_usage = true` usage-only consent.

Cursor **via Conductor** requires mm v1.2.0+ and uses only local usage consent. In
`~/.config/mind-meld/config.toml`, add the key to the existing `[retro]` table
(or create that table if absent):

```toml
[retro]
cursor_host_usage = true
```

Then run `mm push` and inspect `mm status`; `mm diag --json` includes
`host_usage.cursor` with its own blocker, retained-run count and last complete
read. Set the bit to `false` to stop reading. There is no Cursor sync source
or `mm enable-source cursor` command. Run files remain local; only aggregate
usage crosses the encrypted sync boundary.

Coverage is limited to Conductor's Cursor SDK store. Bare cursor-agent usage
has no persisted billing counters, including on a Mac that also uses Conductor.
Runs are counted when finished, on endedAt's UTC day. mm retains captured runs
for 90 days even after Conductor prunes them, using private durable
`cursor-host-tokens.json`; runs pruned before the first capture cannot be recovered.
Do not delete this file to troubleshoot a slow read: it may hold the only copy.
Repeated short reads need not converge on a rewritten ledger; attended warming
or a larger configured read budget may be necessary.

Standard Grok 4.7 uses Cursor's 2026-09-23 list rates ($2 input / $0.50 cache
read / $6 output per million tokens), with an unknown per-request long-context
tier making the cost a floor. This is list-rate equivalent, not Cursor Ultra
subscription spend. Fast tokens use the unpriced `grok-4.7-fast` id: fast-only
cost is `—`; a mixed row's `≥` subtotal excludes Fast cost. Fast can cost 3x
standard in long context. Cache-write price is unpublished. Sources:
[Cursor pricing](https://cursor.com/docs/models-and-pricing) and
[Grok 4.7](https://cursor.com/docs/models/grok-4-7).

Restore an unavailable custom mm-events folder before retrying. Enabling Codex
sync now authorizes reading its local rollouts on **every attended push**, including
converged pushes; its session transcripts remain local.

Capture and source bootstrap run under the mm lock before the manifest scan.
There is one bounded sweep and at most one warm read per cold reader; that warm
read supplies the result. Allow **about five seconds of scanning per cold reader,
and both readers can be cold**. Budgets are cooperative, not hard timeouts;
content sync follows capture. A smaller interactive budget does not cap the
separate warm allowance. Autopush stays gated on substantive changes and never
warms; it is not an attended substitute with the same failure reporting.

A successful append writes one host row. With no user-source byte, newer-mtime
or selection change, the refresh skips Git/session capture, preserves the Git
cursor and adds nothing to retro push counts. It still re-encrypts and uploads
the growing day file, updates last-seen and rewrites the recovery sidecar.
The command therefore reports a file modification even on a converged Mac.
A content-changing push records activity once and runs auto-GC. Usage-only
publication skips the fleet-wide GC sweep; obsolete blobs wait for the next
content-changing push or an explicit `mm gc`.

Healthy capture adds no per-reader output unless `--verbose` is set. A usage-only
refresh explains its work with:

```text
Refresh mode: usage-only; user content already up to date, Git cursor unchanged. Run mm recapture 30d for Git history.
```

`mm recapture 30d` remains Git-history recovery. There is no opt-out flag or config key
for the automatic attended refresh. Previews never scan, warm or append
host usage, fetch upgrade information or write nudge state.

| Outcome | Content | Activity / Git cursor | Exit |
|---|---|---|---|
| Usage-only refresh published | Already up to date | No activity capture; cursor unchanged | 0 |
| Refresh carrying user-source changes published | Pushed (bytes, metadata or selection) | Ordinary activity tail; one push | 0 |
| Prerequisites unavailable, capture fails, or append skipped | Ordinary content sync continues | Ordinary activity rules; capture is not retried in the tail | 0 if content sync succeeds |
| Push fails before manifest acceptance | Not pushed | Locally recorded rows may remain | 1 |
| Row excluded, file absent from accepted manifest, or row absent from accepted bytes (`not-published`) | Pushed or already up to date | Depends on user-content changes | 0, with a warning |
| Publication evidence changed or cannot be read (`unverified`) | Pushed or already up to date | Depends on user-content changes | 0, with a warning; check `mm status` read-only |
| `mm push --dry-run` | Preview only | Nothing captured | 0 completed; 1 stopped |

Capture problems are always stderr `mm: warning:` lines with stable tokens:
`(prerequisites: <state>)`, `(readers)` (a row was still written, but a consented
reader was partial, dropped or absent), `(no-row)`, `(capture-failed)`,
`(append-failed)`, `(max-file-size)`, `(not-published: <cause>)` or
`(unverified: <reason>)`. Only
`(push-failed)`, a stop before manifest acceptance, is an `Error:` line with
exit 1. `not-published` requires proof: `exclude-patterns`, `include-dirs`,
`file-absent` (no day file in the accepted manifest), or `row-missing` (the accepted
bytes contain no matching row). Fix exclude/include settings before retrying;
preserve a copy outside mm-events before repairing damaged JSONL for `row-missing`.
The old `unreadable-row` token is retired. `unverified` reasons are `missing`,
`unreadable`, `oversized-line`, `changed`, `revision-mismatch`, and `evidence-error`.
They describe gaps in evidence, not proof of non-publication: restore read access
if needed and check `mm status`. A new push writes a new capture, so it cannot
verify the previous attempt. A line over 16 MiB, which mm's writer cannot produce,
leaves publication unverified even if those bytes were accepted.

All four event writers guard each whole batch against `sync.max_file_size`,
including the host batch and the later activity batch. A skipped batch leaves
existing bytes intact. If a day file is **already** over the limit and was
advertised previously, the existing complete-snapshot refusal still applies:
archive older rows outside mm-events to shrink it, increase the limit, or
intentionally exclude that day file. The guard does not repair existing oversize
files. Post-acceptance maintenance errors are reported separately; GC corruption
refusals still stop push when host publication was skipped or failed. Exit 2 is
an argument error; exit 4 is reserved for `mm recapture` partial Git recovery.

Status and diag share a read-only prerequisite check: enable an unselected
mm-events source, restore an unavailable custom folder, or consent a reader
before refreshing. An unreadable config produces an unknown verdict and no
refresh/enable recommendation. An unsupported reader format calls for an upgrade,
including in the publication block.

Upgrade **both producing and rendering Macs** for corrected init counts. Init
now marks its Git rows as `origin: init`; older renderers still count that unknown
origin as a push. Historical unmarked init/refresh rows remain indistinguishable
from older pushes until they leave the queried window (retention is 90 days).
`mm recapture` cannot relabel them. The usage-only guarantee is per invocation:
after a failed upload, the next attended push that appends a host row remains
usage-only if user content is unchanged; autopush recovery runs its ordinary
activity tail and legitimately counts once.

“Last recorded capture” and “Last recorded attended attempt” are separate facts:
a failed attempt may write no row. The local attempt record is
`~/.config/mind-meld/last-attended-capture.json` (mode 0600, never synced).
It records the capture start, appended row timestamp when present, class/cause
and reader outcomes, including a push failure before manifest acceptance.
A later GC failure preserves a computed publication verdict. Dry-run and
autopush leave the record alone. A failed durable record write prints a notice;
status may show the previous or new record. A later capture is noted beside a
failed attempt; healthy published attempts need no such note.

Before the first attended push after upgrading, status says
`unknown (no attended attempt recorded yet — run mm push)`. Unreadable and corrupt
records have distinct remedies; a future timestamp says `in the future`.
The `run mm push` suffix is omitted when prerequisites are unavailable or a
consented reader requires an upgrade; the prerequisite/blocker remedy takes precedence.
Status/diag read the record without locking or writing. Publication is proven
only when the recorded file revision matches the local accepted-manifest sidecar;
a known failed read of that revision says `unverified (<reason>)`, and other gaps
say `unknown`. An absent retained row means “no capture in the last 90 days”, not
“never published”. A failed day-file read makes capture selection uncertain only
if that file could contain a winning row within the allowed clock skew.

Reader coverage is contributed, partial, degraded, or absent. A reader proven
empty within the snapshot's retained days says `completed, no usage in snapshot`.
Mixed-reader empty labels become available after this Mac captures with v0.14.18;
legacy all-empty rows still prove every consulted reader empty. Legacy mixed
rows make no per-reader empty claim. A capture at least one day old gets a
refresh reminder in status.

`mm diag --json` keeps `readers` in its existing vocabulary and the row-level
`empty` boolean (`hosts == {}`). Additive `empty_readers` is `null` when no
per-reader claim is possible, otherwise a list (possibly `[]`). `publication`
adds `unverified` and `publication_reason` names its cause. Attempt fields are
`latest_attempt` (class or `unknown`), `latest_attempt_cause`, `latest_attempt_at`,
`latest_attempt_readers`, `latest_attempt_reason` (`missing`, `unreadable`, or
`corrupt` for an unknown record), and `latest_attempt_superseded`. Attempt reader
values are `contributed`, `empty`, `partial`, `dropped:<reason>`, or `absent`.

#### Host usage looks wrong

Run `mm status` on the producing Mac, then `mm diag` for reader blockers.
These example excerpts use both Codex and Grok, at a fixed UTC time.

Healthy: the recorded capture and attended attempt both have publication evidence.

<!-- usage-example:healthy -->
```text
Host usage last recorded capture: 2026-09-21T12:00:00+00:00 (0d ago)
  Publication: published (accepted manifest evidence)
  codex: contributed
  grok: contributed
  Last recorded attended attempt: 2026-09-21T12:00:00+00:00 (0 s ago) — published
```

Mixed with an empty reader: Grok completed successfully and found no usage in
the retained snapshot. It does not need repair.

<!-- usage-example:mixed-empty -->
```text
Host usage last recorded capture: 2026-09-21T12:00:00+00:00 (0d ago)
  Publication: published (accepted manifest evidence)
  codex: contributed
  grok: completed, no usage in snapshot
  Last recorded attended attempt: 2026-09-21T12:00:00+00:00 (0 s ago) — published
    codex contributed; grok completed, no usage in snapshot
```

Failed refresh: the earlier published capture remains visible. The newer attempt
records why no row was written. Inspect the named readers in `mm diag`; follow
the blocker remedies below before refreshing.

<!-- usage-example:failed-refresh -->
```text
Host usage last recorded capture: 2026-09-21T11:00:00+00:00 (0d ago)
  Publication: published (accepted manifest evidence)
  codex: contributed
  grok: contributed
  Last recorded attended attempt: 2026-09-21T12:00:00+00:00 (0 s ago) — no-row
    codex dropped (unsupported); grok dropped (deadline)
```

Unverified: a line larger than 16 MiB prevents proof from this file, including
certainty about the winning capture. This does not establish non-publication;
check evidence read-only with `mm status`. An attended push creates a new attempt.

<!-- usage-example:unverified -->
```text
Host usage last recorded capture: unknown (~/.local/share/mind-meld/events/local-2026-09-21.jsonl)
  Publication: unverified (oversized-line) (accepted manifest evidence)
  codex: contributed
  grok: contributed
  Last recorded attended attempt: 2026-09-21T12:00:00+00:00 (0 s ago) — unverified (oversized-line)
  Usage refresh: Attended mm push refreshes and publishes host usage automatically.
```

#### Reader blockers

Diag separates `<reader> cache inventory:` from `<reader> usage read blocker:`.
A readable cache with no known blocker says `none`; a blocker can appear beside
a ready inventory. `last_reason` is the standing read blocker, retained until
a read completes inside its budget, with `unsupported` surviving later
incomplete transient failures. A complete read that exceeds its budget
replaces even `unsupported` with `deadline`: its records were readable, but
the read was too slow. `(first observed 2026-09-04 UTC)` dates
this version's first observation of that reason, not the start of an outage.
Older undated blockers gain a date when this version first observes them.

| Reason | Meaning | Remedy |
|---|---|---|
| `unsupported` | The reader wrote a record this mm cannot read. | A newer mm may read it: run `pipx upgrade mind-meld`, or `mm disable-source codex` / `mm disable-source grok` to stop the corresponding source reader. A retry alone cannot fix it. |
| `malformed` | A record or counter relationship could not be interpreted safely. | Let the host finish writing, then `mm push`. If it persists, report the mm version and Host usage diag block. |
| `io_error` | A host log could not be read. | Restore read access, then `mm push`. |
| `stale` | A file changed while it was being read. | Let the host finish writing, then `mm push`. |
| `partial` | A final record is unfinished. | Let the host finish the record, then `mm push`; `mm diag` shows the reader's state. |
| `deadline`, last complete read over the autopush budget | The warm read costs more than this Mac allows. | Raise `[retro] host_usage_autopush_budget_ms` using the last complete read and sweep estimate in `mm diag`; keep it at or below the interactive budget. |
| `deadline`, last complete read within budget or unknown | The latest attempt ran out of time; a cold or changing store may need more work. | Run `mm push` for attended warming (about 5 s of scanning per cold reader, not a hard ceiling), then compare the last complete read and counts in `mm diag`. |

Every attended push attempts a fresh capture. If every consented reader later
fails, no host row is written and this snapshot ages. If another reader
completes, the newer row supersedes the device's coverage and may omit the
failed reader. Status shows the latest coverage. The read-budget lever is
the lasting fix when warm autopush reads consistently exceed their allowance.
Fleet retros flag devices whose last snapshot predates the requested window.

Set budgets in this Mac's `~/.config/mind-meld/config.toml`:

```toml
[retro]
host_usage_autopush_budget_ms = 350
host_usage_interactive_budget_ms = 500
```

Autopush defaults to **250 ms** (range **100–5,000**); interactive push and init default to **500 ms** (range **250–5,000**).
Both must be integers, and effective autopush must not exceed effective
interactive. Invalid values stop sync like any other `[retro]` error. Raising
a budget trades more autopush latency for read headroom. This is per Mac and
per mm version: remeasure after corpus or interpreter changes; do not copy a
fleet-wide number. `mm diag` shows each effective value and its `default` or
`config` source, each reader's **last complete read**, and the combined sweep
estimate. The estimate excludes cache writes and later-reader grace and is
not a publication check. Missing measurements say `unknown`; future dates
say `in the future`. The attended warm remains about 5 s of cooperative
scanning, independent of these settings.

An attended `mm push` re-reads usage even when nothing else needs uploading. A
no-op `mm autopush` still does not: autopush stays change-gated and never warms,
and previews never capture. Orchestration failures (`unavailable`, or expiry
before a reader was invoked) stay on push stderr and the autorun breadcrumb;
they cannot be recorded by a reader that never ran. A no-op autopush may
replace that breadcrumb with `success` while the standing reader blocker
remains. A completed read clears the blocker only if it finishes inside its
read budget; a complete-but-late read records `deadline` and its timing. Inventory plus `none` is not proof of publication.

`mm disable-source codex` also stops syncing Codex customizations and skill-link
maintenance under the default source-derived policy; an explicit `[skills]
agents` grant is the exception for maintenance. There is no independent Codex
usage opt-out yet. Grok's existing `[retro] grok_host_usage = true` remains a
separate opt-in: turn that off too if you want to stop usage after disabling
the source. Content sync and other readers' captures survive one reader's
failure.

If `mm --version` does not change after `pipx upgrade`, see [Upgrading](#upgrading).
No cache migration is needed for 0.14.16. To roll back this change:

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@v0.14.15
```

That pins an older release; use the `@latest` reinstall in Upgrading to resume
updates. Older mm ignores the additive root keys.

`mm diag --json` exposes `host_usage.codex` keys `cache_state`, `state`,
`files_cached`, `files_migrated`, `files_pre_track`, `files_on_disk`, `pending`,
`model_count`, `models`, `last_reason`, and `last_reason_since`.
`host_usage.grok` keys are `consented`, `complete_once`, `usage_less_skipped`,
`cache_state`, `model_count`, `models`, `last_reason`, `last_reason_since`,
`files_cached`, and `files_on_disk`. The text line is `grok ledgers cached: N of M`;
missing, malformed, version-mismatched or locked caches show `unknown`, not 0.
Both reader dictionaries also expose `last_complete_ms`, `last_complete_at`,
and `last_deadline_allotted_ms`. The new top-level `host_read_budgets` contains
`autopush_ms`, `autopush_source`, `interactive_ms`, `interactive_source`, and
`warm_ms` (5000). Timings exclude cache serialization. Dates are normalized
UTC ISO text or null; missing old fields become null,
and an invalid date never erases a valid blocker. Diag reads caches without
opening host logs or needing a passphrase; Codex counts rollout paths and Grok
counts two-level `*/*/updates.jsonl` paths under its resolved sessions root.
`cache_state: missing` or `unreadable` leaves the blocker unknown.

A `mm: notice: host token cache write failed: <ErrnoName>` means the read's
result still stands but its cache could not be saved. The write may leave an
empty or unreadable cache, losing the old blocker. A subsequent failing read
can record it again; this notice does not itself become a stored blocker.

### Snapshot failures

A successful `mm push` publishes a complete snapshot of the **selected** sources: each advertised digest and size describe the accepted file bytes, and mtime describes that same file revision. An unreadable selected file, a file that changes while it is being read, a still-present file omitted only because it exceeds `sync.max_file_size` or shares an inode alias, or a missing user-source root that was previously published, **refuses the whole push** and keeps the previous snapshot. There is no hidden retry; run `mm push` again after the cause is fixed. `mm push --dry-run` previews this scan and deletion proof and **changes nothing except the local lock file**. It reports pending directory creation and shared crypto-init reconciliation without performing them. It uses your current config without prompting for migration; run `mm migrate-config` to review recommended updates. It does not preview host-usage capture, the activity row a real push appends, post-push GC of orphaned blobs, or upload re-reads.

Preview exit codes: **0** completed; **1** stopped (snapshot refusal, crypto/config error, or lock held—the message explains which); **2** usage error. Every successful preview, including “Nothing to push,” ends with the lock-qualified completion message.

See [Previews](#previews) for every command’s write allowance, omitted work and exit codes.

`mm autopush` still exits 0 so an agent hook can continue. Inspect `mm status` (the `last_autorun.detail` field) or run interactive `mm push` if you need an exit status.

| What happened | What to do |
|---|---|
| File or directory could not be read | Restore read access, then `mm push`. `mm disable-source <name>` is a coarse escape hatch if that source should stop syncing. |
| File changed while being read | Wait for the editor/writer to finish, then `mm push`. |
| Previously published file is still present but over `max_file_size` | Raise `sync.max_file_size` in `config.toml`, or add a precise `exclude_patterns` glob **under that source**. Creating a new `[[sync.sources]]` list replaces default auto-detection — keep your other sources. |
| Default mm-events root, `events/`, or event files are missing | Missing files mean deletion: `mm push --dry-run` previews it and `mm push` publishes it (apart from the new activity row omitted by preview). To keep the previously published files, run `mm pull --from <this Mac's device id> --source mm-events` **before the next push**, then verify the files. Other Macs keep existing copies. |
| Custom mm-events folder is missing | Push warns, skips mm-events for that push without new tombstones, and publishes the other sources; autopush records a degraded breadcrumb. Plug in the drive, create the folder, or run `mm disable-source mm-events`. The next push includes it again when it returns. |
| Source folder is missing after it was previously published | Restore the folder, or `mm disable-source <name>` if it should stop syncing. |
| Pull says a file does not match the sending snapshot | Local bytes are kept. Run `mm pull --verbose` for peer/source/path, then `mm log --verb pull --action failed --limit 10`. A metadata-only `touch` or unchanged re-push does not rewrite a blob the diff considers unchanged. |

Do not edit meaningful file contents just to force a new hash. Mixed-version fleets stay compatible, but older writers can still publish inconsistent blobs and older receivers lack this content check.

```bash
mm status
mm log --verb pull --action failed --limit 10
mm pull --verbose
mm push
```

### Previews

Previews use your current config without migration prompts, upgrade checks or
version bookkeeping. They leave shared crypto-init copies and missing
fingerprints untouched. Pending setup is reported for a following real command.
The lock allowance includes creating its parent if absent.

| Command | May touch | Not previewed | Exit codes |
|---|---|---|---|
| `mm push --dry-run` | Local lock only | Host scanning/warming/capture, activity row, post-push GC, upload re-reads | 0 completed; 1 stopped |
| `mm pull --dry-run` | Local lock only | Pre-v0.9.2 conflict renames, pull history, project sync logs, manifest conflict-copy cleanup, blob download/decrypt failures | 0 completed, including incomplete scans; 1 stopped; 3 when fail mode predicts conflicts or failures |
| `mm gc --dry-run` | Local lock only | No cleanup is applied; conflict deletion requires `--conflicts` | 0 completed; 1 stopped |
| `mm recapture --dry-run [WINDOW]` | Local lock only | Writing git-snapshot rows and the full push publishing them with other pending changes | 0 completed, including partial scans; 1 stopped or no repositories |
| `mm migrate-config --dry-run` | Nothing, including no lock | Applying the displayed config changes | 0 completed; 1 config error |
| `mm diff [--from DEVICE]` | Nothing, including no lock | Snapshot comparison, not a forecast of all incoming peers; use `mm pull --dry-run` for that | 0 completed; 1 stopped |

All commands use exit 2 for invalid usage. Pull and recapture print **Preview
incomplete:** when a corrupt peer or incomplete repository scan limits their
preview, while keeping their existing exit codes. Unknown pull sources remain
warnings. Pull totals count writes, merges (an upper bound), conflicts, skips,
and possible local failures across peers; conflicts leave canonical bytes alone.
Files can change between preview and apply.

For a write-free CI gate, run `mm pull --dry-run --conflict-mode fail`: exit 0
means no conflicts or local failures predicted, 1 means stopped, and 3 means
conflicts or failures predicted. Without `--dry-run`, fail mode exits 3 before
applying any file but can still perform setup writes. `--dry-run --conflict-mode
prompt` only predicts; it never prompts.

Inspection commands leave sync data alone. `status` may seed or recover only
`~/.config/mind-meld/seen-sources.json`; steady-state reads write nothing.
Author-filtered `retro-fleet` may write its identity cache on every run,
including a warm hit; `--no-author-filter` avoids it. `diag`'s `grok inspect`
and identity discovery's `gh`/`git` subprocesses may manage their own host
state. Status reads only the cached upgrade result, showing its age when stale.

**Upgrading to v0.14.15:** preview exit codes are unchanged. Pull preview's
“Pull complete.” becomes outcome totals and “Dry run complete. Nothing was
changed except the local lock file.” Migration preview's “Dry run — no changes
written.” becomes “Dry run complete. Nothing was changed.” Fail-mode wording
changes from “(no writes)” to “before applying any file”; add `--dry-run` for
the write-free CI gate above. Mergeable files changed by two peers no longer
cause a false fail-mode exit 3.

### Shell completion

```bash
mm --install-completion      # writes the completion script and sources it
mm --show-completion         # prints it instead, if you would rather install it yourself
```

`--install-completion` edits your shell's startup file — `~/.zshrc` plus
`~/.zfunc/_mm` on zsh, `~/.bashrc` on bash — and on zsh it also enables
`compinit`. Use `--show-completion` if you would rather place the script
yourself.

## Uninstalling

Only want `/retro-fleet` gone from one agent, and keeping `mm`? That is not this section — see [Removing a skill link](#removing-a-skill-link). Everything below is for removing `mm` itself.

`pipx uninstall mind-meld` removes the `mm` command and nothing else. Everything below is deliberate — an uninstall should not delete your data or your synced fleet history — but the agent skill links and the skill store are worth knowing about.

If you remove `~/.local/share/mind-meld` while keeping `~/.config/mind-meld` and continue using or reinstall `mm`, the next push publishes the missing default mm-events files as deletions. Status and other inspection commands do not recreate that data. The skill store shares this root; reinstalling skills does not restore event history.

The link loop below is written to survive the state you are actually in: it needs no `mm` on `PATH`, no config, and no valid config, so it works whether you run it before or after `pipx uninstall`.

```bash
pipx uninstall mind-meld

# Agent skill links. These survive the uninstall by design (they point at the
# store below, not at the deleted pipx venv), so each agent keeps offering
# /retro-fleet even though the `mm` it shells out to is gone. This removes
# only links that actually point at Mind Meld's store — an entry of your own
# at that path is left alone. Installation now also requires source consent
# (or `mm install-skills --agent`); cleanup still matches on readlink.
# Covers every agent mm has ever created a store-backed skill link for,
# including the retired OpenCode path. mm removes its own OpenCode link on
# upgrade (v0.13.0); this loop is belt-and-braces for a machine that
# uninstalls without ever running the retirement release. Do not delete
# the opencode path as a "prose sweep" — it is an explicit exception.
# `mm diag` prints the authoritative live list if you still have `mm`; this
# loop does not need it.
for l in ~/.claude/skills/retro-fleet ~/.codex/skills/retro-fleet ~/.config/opencode/skills/retro-fleet; do
  [ "$(readlink "$l")" = "$HOME/.local/share/mind-meld/agent-skills/retro-fleet" ] && rm -f "$l"
done

# Local data: the skill store and the mm-events log.
# If you ever hand-authored your own skill under agent-skills/retro-fleet/,
# Mind Meld refused to publish over it — move it out before running this.
rm -rf ~/.local/share/mind-meld

# Config, device identity, caches, and TTL markers.
rm -rf ~/.config/mind-meld

# Keychain entry (service `mind-meld`, account `passphrase`).
security delete-generic-password -s mind-meld -a passphrase
```

Your iCloud storage folder is untouched by all of the above. Delete it only when you are retiring the whole fleet — every other Mac pulls from it, and it holds the only copy of anything that machine hasn't pulled yet. The encrypted blobs are unreadable without the passphrase, so leaving it in place is safe.

## Development

From a clone (no environment required):

```
./bin/check
```

That command bootstraps `.venv` if needed, then runs `ruff check .`, `ruff format --check .`, and pytest. Cards describe verification *scope*; they do not know where Python lives:

```
./bin/check tests/test_config.py
```

A scoped pytest still lints the whole repo (ruff is ~0.07s). `--tests` skips lint; `--lint` skips pytest. Use `--serial` to disable pytest-xdist. `MM_PYTHON` selects the bootstrap interpreter; `MM_VENV` validates and uses an existing environment without mutating it; `VIRTUAL_ENV` and `--no-bootstrap` use the current environment. `MM_PYTEST_WORKERS` sets the xdist worker count. See `./bin/check --help`.

Manual fallback (separate lines; quote `'.[dev]'` — macOS zsh globs it):

```
python3.13 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
```

## Architecture

See [SPEC.md](SPEC.md) for full documentation.
